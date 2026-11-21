"""``ds packs``: evidence packs, the signing key's register entry, the revocation list and the offline
check of a pack."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from deltasleuth.cli.common import (
    EXIT_DIFF,
    ConfigOpt,
    OutOpt,
    _a,
    _at,
    _errors,
    _fail,
    _in_context,
    _json,
    _local,
    _named,
    _o,
    _write,
)
from deltasleuth.config import SettingsError, load_settings
from deltasleuth.security.signing import PackSigner

if TYPE_CHECKING:
    from deltasleuth.context import Context


packs_app = typer.Typer(invoke_without_command=True)


@packs_app.callback()
def packs_list(
    command: typer.Context,
    run_id: Annotated[str | None, _o("Only this run's packs (R-…).", "--run")] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the evidence packs, newest first; or file the signing key's register entry, sign the
    revocation list, download a pack or verify one offline. Only a signed-in person issues a pack, on the
    Run screen."""
    if command.invoked_subcommand is not None:
        if as_json or run_id is not None or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    from deltasleuth.store import repo

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            return await repo.pages(repo.list_packs, conn, run_id=run_id)

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for p in rows:
        line = f"{p['pack_id']}  {p['status']}  cycle {p['cycle']} of run {p['run_id']}"
        line += f"  sign-off {p['signoff_id']}"
        line += f"  issued by {_named(p['issued_by'], p['issued_by_name'])} {_at(p['issued_at'])}"
        line += f"  key {p['key_fingerprint']}  sums {p['sums_sha256']}"
        if p["revoked_at"] is not None:
            by = _named(p["revoked_by"], p["revoked_by_name"])
            line += f"  revoked by {by} {_at(p['revoked_at'])}: {p['revoke_reason']!r}"
        if p["purged_at"] is not None:
            line += f"  stored copy purged {_at(p['purged_at'])}"
        typer.echo(line)
    if not rows:
        typer.echo("no evidence pack")


def _signer(config: Path | None) -> tuple[str, PackSigner]:
    """The installation's name and its signing key, from the pack settings."""
    from deltasleuth.reports import pack

    settings = load_settings(config)
    if settings.packs is None:
        raise SettingsError(pack.NOT_CONFIGURED)
    return settings.packs.installation, PackSigner.load(settings.packs.signing_key_ref)


@packs_app.command("key")
def packs_key(out: OutOpt, config: ConfigOpt = None) -> None:
    """Write the signing key's entry for the site's key register: fingerprint.txt (the installation and
    the key's fingerprint) and procedure.txt (the verification procedure every pack's VERIFY.txt must
    match), to be filed, dated, when the key is generated."""
    from deltasleuth.reports import pack

    with _errors():
        installation, signer = _signer(config)
        _write(out, pack.key_files(installation, signer))
    typer.echo(
        f"wrote fingerprint.txt and procedure.txt to {out}: file both in the key register, dated"
        f" (installation {installation}, fingerprint {signer.fingerprint})"
    )


@packs_app.command("revocations")
def packs_revocations(out: OutOpt, config: ConfigOpt = None) -> None:
    """Write the signed revocation list: revoked.txt (every revoked pack of every key used), its signature
    and the current signing key, which a pack's step 8 checks."""
    from deltasleuth.reports import pack

    with _errors():
        _, signer = _signer(config)
    files = _in_context(config, lambda ctx: pack.revocations(ctx, signer))
    with _errors():
        _write(out, files)
    revoked = files["revoked.txt"].count(b"\n") - 2
    typer.echo(f"wrote revoked.txt ({revoked} revoked pack(s)), revoked.txt.sig and signing-key.pem to {out}")


@packs_app.command("download")
def packs_download(
    pack_id: Annotated[str, _a("The pack id (P-…).")], out: OutOpt, config: ConfigOpt = None
) -> None:
    """Write a pack's stored ZIP (P-….zip) into the directory, audited once written. A purged cycle's pack
    is gone from the store: copies downloaded before still verify."""
    from deltasleuth.reports import pack

    who = _local(config)

    async def work(ctx: Context) -> Path:
        found, data = await pack.stored(ctx, pack_id)
        (path,) = _write(out, {f"{pack_id}.zip": data})
        await pack.downloaded(ctx, who, found)
        return path

    path = _in_context(config, work)
    typer.echo(f"wrote {path}: check it with 'ds packs verify {path.name} --fingerprint <the register's>'")


@packs_app.command("verify")
def packs_verify(
    zip_file: Annotated[Path, _a("The pack (P-….zip).")],
    fingerprint: Annotated[
        str | None, _o("The signing key's fingerprint your key register lists for the installation.")
    ] = None,
    revocations: Annotated[
        Path | None,
        _o("The custodian's latest revoked.txt, revoked.txt.sig and signing-key.pem (a directory)."),
    ] = None,
) -> None:
    """Check a pack offline, as its VERIFY.txt does, without settings: steps 0 to 6 and 8 (0 against this
    build's procedure) and a sampled pack's hand check of its draw, each ok or FAILED, and the objects step
    7 checks against your SIEM. Exits 1 on a failure. A zip over 64 entries or 200 MiB, compressed over
    100:1, with a symlink, an absolute path or '..', a name twice, a manifest it cannot read, or of another
    format is refused."""
    from deltasleuth.reports import pack_verify

    if fingerprint is None:
        _fail("--fingerprint is required: the fingerprint your key register lists for the installation")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
        _fail("--fingerprint must be 64 hex characters (a SHA-256)")
    with _errors():
        steps = pack_verify.verify(zip_file, fingerprint.lower(), revocations)
    for step in steps:
        typer.echo(step.line)
    if any(step.ok is False for step in steps):
        raise typer.Exit(EXIT_DIFF)
