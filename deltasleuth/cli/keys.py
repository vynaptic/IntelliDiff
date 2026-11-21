"""``ds keys``: the keyring, a keyring file's fingerprint, and what releases before 006 stored in clear
sealed."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import ConfigOpt, _a, _errors, _fail, _in_context, _json, _local, _o, _run, _siem
from deltasleuth.config import load_settings

if TYPE_CHECKING:
    from deltasleuth.context import Context


keys_app = typer.Typer(no_args_is_help=True, help="Encryption at rest.")


@keys_app.command("status")
def keys_status(
    verify: Annotated[bool, _o("Also read back every blob (the restore check).", "--verify")] = False,
    as_json: Annotated[bool, _o("Print the status as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Each KEK of the keyring with the data keys it wraps, and the KEKs in use the keyring lacks. Exit 2
    while one is missing, or (--verify) while a blob is corrupted or unreadable for a missing KEK; missing
    blobs are counted, not failed."""
    from deltasleuth.security import keys
    from deltasleuth.store import repo

    async def work(ctx: Context) -> tuple[Any, Counter[str], dict[str, int] | None]:
        async with ctx.pool.connection() as conn:
            used = Counter(row["kek"] for row in await repo.data_keys(conn))
        return ctx.keyring, used, await keys.verify(ctx.pool, ctx.evidence) if verify else None

    keyring, used, blobs = _in_context(config, work)
    present = {kek: used[kek] for kek in keyring.ids}
    missing = {kek: n for kek, n in used.items() if kek not in keyring.ids}
    unread = blobs and blobs["corrupted"] + blobs["key_missing"]  # a missing blob is counted, not failed
    if as_json:
        status = {"current": keyring.current, "keks": present, "missing": missing}
        typer.echo(_json(status | ({} if blobs is None else {"blobs": blobs})))
    else:
        for kek, n in present.items():
            typer.echo(f"{kek}: {n:,} data key(s)" + (" (current)" if kek == keyring.current else ""))
        for kek, n in missing.items():
            typer.echo(f"missing {kek}: {n:,} data key(s); restore the line from the vault and restart")
        if blobs is not None:
            typer.echo(
                f"blobs: {blobs['checked']:,} checked: {blobs['readable']:,} readable, {blobs['missing']:,}"
                f" missing, {blobs['corrupted']:,} corrupted, {blobs['key_missing']:,} unreadable (a KEK is"
                " missing)"
            )
    if missing:
        _fail(f"the keyring lacks {len(missing)} KEK(s) in use")
    if unread:
        _fail(f"{unread:,} blob(s) do not decrypt")


@keys_app.command("fingerprint")
def keys_fingerprint(path: Annotated[Path, _a("A keyring file, such as a vault copy.")]) -> None:
    """Print the ids of a keyring file's KEKs, oldest first, to check a copy against 'ds keys status'."""
    from deltasleuth.security.crypto import fingerprint

    with _errors():
        keyring = fingerprint(path)
    for kek in keyring.ids:
        typer.echo(kek + (" (current)" if kek == keyring.current else ""))


@keys_app.command("migrate")
def keys_migrate(
    accept_missing: Annotated[
        bool, _o("Record every legacy blob as missing when blobs/sha256 holds none.", "--accept-missing")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Seal the raw keys, headers, chat, scan points and blobs that releases before 006 stored in clear,
    then end plaintext storage (migration 007). Offline: stop the app first. Safe to interrupt and run
    again."""
    from deltasleuth.security import legacy

    who = _local(config)
    _siem()

    async def main() -> dict[str, int]:
        return await legacy.migrate(load_settings(config), who, accept_missing=accept_missing)

    n = _run(main(), legacy.INTERRUPTED)
    typer.echo(
        f"sealed: {n['keys']} key(s), {n['headers']} header(s), {n['chat_messages']} chat message(s) and"
        f" {n['scans']} scan point(s) under {n['runs']} new run data key(s); {n['blobs']} blob(s)"
        f" re-encrypted, {n['missing_blobs']} missing; plaintext storage ended (migration 007)"
    )
