"""``ds licence``: a vendor build's licence, and the installation's id."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import REASON, ConfigOpt, _a, _at, _errors, _fail, _in_context, _json, _local, _o

if TYPE_CHECKING:
    from deltasleuth import licence
    from deltasleuth.context import Context


licence_app = typer.Typer(invoke_without_command=True)


@licence_app.callback()
def licence_status(
    command: typer.Context,
    as_json: Annotated[
        bool, _o("Print GET /v1/licence's body and the audit head: the true-up.", "--json")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Show the installation's licence: its id, the licence in force, its state and warnings, what it
    allows and the pairs counted with their first runs and seats; or print the id, show a licence file
    offline, install one or withdraw one that has not started. Exits 0 in every state. A build without a
    vendor key needs no licence: it says so, and shows and installs none."""
    if command.invoked_subcommand is not None:
        if as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    from deltasleuth import licence
    from deltasleuth.store import repo

    async def work(ctx: Context) -> dict[str, Any]:
        found = await licence.status(ctx)
        if as_json:
            async with ctx.pool.connection() as conn:
                found["audit_head"] = await repo.audit_head(conn)
        return found

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    named, warnings = found["licence"], found["warnings"]
    typer.echo(f"installation {found['installation_id'] or '-'}")
    if found["state"] == "unknown":  # not an evaluation: an installed licence may be in force
        typer.echo("licence unknown: the standing could not be read (see the server log)")
    elif found["state"] == licence.OPEN:
        typer.echo("licence none: this build needs no licence")
    elif named is None:
        typer.echo("licence none: an unlicensed evaluation")
    else:
        typer.echo(
            f"licence {named['licence_id']}: {named['customer']} (order {named['order']}), {named['tier']},"
            f" {_pairs(named['pairs'])}, {named['starts']} to {named['ends']}"
        )
    typer.echo(f"state {found['state']}" + (f": {warnings[0]}" if warnings else ""))
    for line in warnings[1:]:
        typer.echo(f"  {line}")
    if (allows := found["allows"]) is not None:
        typer.echo("allows: " + ", ".join(f"{name} {'yes' if ok else 'no'}" for name, ok in allows.items()))
    for pair in (found["pairs"] or {"counted": []})["counted"]:
        targets, seat = ", ".join(pair["targets"]) or "-", "seated" if pair["seated"] else "no seat"
        typer.echo(f"pair {pair['name']}  first run {_at(pair['first_at'])}  {targets}  {seat}")


def _pairs(pairs: int | None) -> str:
    return "unlimited pairs" if pairs is None else f"{pairs} pair{'s' if pairs != 1 else ''}"


def _licence_file(path: str) -> tuple[bytes, str]:
    """A licence file's bytes (one past the most a licence holds, so that a longer file is refused) and its
    name in messages; ``-``: the standard input."""
    from deltasleuth import licence

    if path == "-":
        return typer.get_binary_stream("stdin").read(licence.MAX_BYTES + 1), "stdin"
    with Path(path).open("rb") as file:
        return file.read(licence.MAX_BYTES + 1), path


@licence_app.command("id")
def licence_id(config: ConfigOpt = None) -> None:
    """Print this installation's id alone, which a vendor build's licence names; opening the store
    makes it (migration 010)."""
    from deltasleuth.store import repo

    async def work(ctx: Context) -> str:
        async with ctx.pool.connection() as conn:
            return (await repo.licence_facts(conn))["installation_id"]

    typer.echo(_in_context(config, work))


@licence_app.command("show")
def licence_show(path: Annotated[str, _a("A licence file (LI-….txt), or - for the standard input.")]) -> None:
    """Show a licence file's fields and check its signature offline, without settings; exit 2 when this
    build would refuse it, as a build without a vendor key refuses every file: it needs no licence."""
    from deltasleuth import licence

    with _errors():
        data, name = _licence_file(path)
        found = licence.parse(data, name=name)
    typer.echo(found.body.decode(), nl=False)
    typer.echo(f"verifies with key {found.key[:12]}… (trusted by this build)")


@licence_app.command("install")
def licence_install(
    path: Annotated[str, _a("The licence file as received, or - for the standard input.")],
    config: ConfigOpt = None,
) -> None:
    """Install a licence file for this installation: it applies at once in every process, or from its
    starts. Installing it again changes nothing. It resumes nothing: resume each run paused for the
    licence on its Run screen. A build without a vendor key refuses every file (exit 2): it needs no
    licence."""
    from deltasleuth import licence

    who = _local(config)
    with _errors():
        data, name = _licence_file(path)

    async def work(ctx: Context) -> tuple[dict[str, Any], list[str]]:
        from deltasleuth.runner.runs import resumable

        done, paused = await licence.install(ctx, data, who=who, name=name), []
        if done["applies"] == "now" and done["after"].refusal("run") is None:
            paused = await resumable(ctx)
        return done, paused

    done, paused = _in_context(config, work)
    found, before, after, applies = done["licence"], done["before"], done["after"], done["applies"]
    if not done["installed"]:
        typer.echo(f"licence {found.licence_id} is installed already")
        return
    installed = (
        f"installed {found.licence_id} ({found.tier}, {found.customer}, {found.starts} to {found.ends},"
        f" {_pairs(found.pairs)})"
    )
    if applies == "now":
        typer.echo(f"{installed}: in force now")
    elif applies.startswith("from "):
        named = after.licence
        until = "an unlicensed evaluation" if named is None else f"{named.licence_id} applies"
        typer.echo(f"{installed}: in force from {found.starts}; {until} until then")
    else:  # started, while a later-starting licence is in force
        typer.echo(f"{installed}: not in force; {applies.removeprefix('no: ')}")
    if applies == "now":
        changes = [
            f"{what} {was} -> {now}"
            for what, was, now in (
                ("tier", _tier(before), _tier(after)),
                ("pairs", _limit(before.limit), _limit(after.limit)),
                ("state", before.state, after.state),
            )
            if was != now
        ]
        if changes:
            typer.echo(f"changes now: {'; '.join(changes)}")
        if unseated := [name for name in after.names if name not in after.seats]:
            typer.echo(
                f"{', '.join(unseated)} have no seat: no new run, and their soaks pause at their next cycle"
            )
    if found.tier != "enterprise" and after.sso:
        typer.echo("runs under SSO stay refused")
    if paused:
        typer.echo(
            f"{len(paused)} run(s) paused for the licence: {', '.join(paused)}; resume each on its Run screen"
        )


def _tier(current: licence.Standing) -> str:
    return "evaluation" if current.licence is None else current.licence.tier


def _limit(limit: int | None) -> str:
    return "unlimited" if limit is None else str(limit)


@licence_app.command("withdraw")
def licence_withdraw(
    licence_id: Annotated[str, _a("The licence id (LI-…).")],
    reason: Annotated[str, _o(REASON)],
    config: ConfigOpt = None,
) -> None:
    """Withdraw a genuine but wrong licence before it starts; a started licence is only replaced by a new
    one, so no withdrawal returns the installation to an evaluation."""
    from deltasleuth import licence

    who = _local(config)
    _in_context(config, lambda ctx: licence.withdraw(ctx, licence_id, reason, who=who))
    typer.echo(f"withdrew licence {licence_id}: it is not installed again")
