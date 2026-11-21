"""``ds audit``: the audit log, and its hash chain verified."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import ConfigOpt, _at, _errors, _fail, _in_context, _json, _local, _named, _o
from deltasleuth.errors import Invalid

if TYPE_CHECKING:
    from deltasleuth.context import Context


audit_app = typer.Typer(invoke_without_command=True)


_SEEN = (("request", "request_id"), ("session", "session_id"), ("from", "client_addr"))


def _when(text: str | None, option: str) -> datetime | None:
    """An ISO 8601 time, UTC when it names no offset."""
    if text is None:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise Invalid(f"{option} {text!r} is not an ISO 8601 time, e.g. 2026-09-26T08:00:00Z") from None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


@audit_app.callback()
def audit_log(
    command: typer.Context,
    actor: Annotated[str | None, _o("Only this actor.")] = None,
    action: Annotated[str | None, _o("Only this action.")] = None,
    obj: Annotated[str | None, _o("Only this object.", "--object")] = None,
    session: Annotated[str | None, _o("Only this session's rows (W-…).")] = None,
    since: Annotated[str | None, _o("From this time on (ISO 8601; UTC without an offset).")] = None,
    until: Annotated[str | None, _o("Before this time (ISO 8601; UTC without an offset).")] = None,
    limit: Annotated[int | None, typer.Option(min=1, max=500, help="Rows to show (default 100).")] = None,
    as_json: Annotated[bool, _o("Print the rows as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Read the audit log, newest first, each filter exact; or verify its hash chain. The read is audited
    with its filters."""
    if command.invoked_subcommand is not None:
        options = (actor, action, obj, session, since, until, limit, config)
        if as_json or any(option is not None for option in options):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    from deltasleuth.store import repo

    who = _local(config)
    with _errors():
        start, end = _when(since, "--since"), _when(until, "--until")
    given = {"actor": actor, "action": action, "object": obj, "session_id": session}
    given |= {
        "since": None if start is None else start.isoformat(),
        "until": None if end is None else end.isoformat(),
    }
    filters = {name: value for name, value in given.items() if value is not None}

    async def work(ctx: Context) -> list[repo.Row]:
        async with repo.audited(ctx.pool) as (conn, audit):
            rows, _ = await repo.list_audit(
                conn,
                actor=actor,
                action=action,
                obj=obj,
                session_id=session,
                since=start,
                until=end,
                limit=limit or 100,
            )
            await audit(who, "audit.read", "audit", {"filters": filters})
        return rows

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for r in rows:
        seen = "".join(f"  {label} {r[column]}" for label, column in _SEEN if r[column] is not None)
        detail = json.dumps(r["detail"], sort_keys=True, default=str)
        typer.echo(
            f"{r['id']}  {_at(r['at'])}  {_named(r['actor'], r['actor_name'])}  {r['action']}  {r['object']}"
            f"  {detail}{seen}"
        )


@audit_app.command("verify")
def audit_verify(
    siem: Annotated[
        Path | None,
        _o("A SIEM export of the deltasleuth.audit lines (JSON lines): compare the log with it both ways."),
    ] = None,
    anchor: Annotated[
        Path | None,
        _o("A copy of the audit anchor kept elsewhere (its file, or its log line): checked beside the one"
           " in the workspace."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Check the audit log's hash chain: contiguous rows, both links, each hash,
    each row's text against its columns, every row chained; with --siem, the rows its lines span; against
    the anchor the app keeps in its workspace, and with --anchor a copy of one, that the chain still holds
    the anchored row as it was. Says when nothing outside the database was checked, and whether the app's
    database role can switch the audit log's triggers off. Prints the head, for the daily ticket; a break
    exits 2 naming the first row that fails."""
    from deltasleuth.store import chain

    with _errors():
        lines = None if siem is None else chain.read_siem(siem)

    async def work(ctx: Context) -> tuple[dict[str, Any], Path, list[tuple[Path, chain.Anchor]], str]:
        kept = chain.anchor_path(ctx.settings)
        paths = [path for path in (kept, anchor) if path is not None and (path != kept or kept.exists())]
        anchors = [(path, chain.read_anchor(path, ctx.anchor_key)) for path in dict.fromkeys(paths)]
        async with ctx.pool.connection() as conn:
            done = await chain.verify(conn, lines, [held for _, held in anchors])
        async with ctx.pool.connection() as conn:
            _, roles = await chain.roles(conn, ctx.settings)
        return done, kept, anchors, roles

    done, kept, anchors, roles = _in_context(config, work)
    upgraded = f", 1 to {done['upgrade_seq']} chained by 009" if done["upgrade_seq"] else ""
    typer.echo(f"chain: {done['rows']:,} row(s) verified{upgraded}")
    if (matched := done["siem"]) is not None:
        span = f": rows {matched['first']} to {matched['last']}" if matched["lines"] else ""
        typer.echo(f"siem: {matched['lines']:,} line(s) match{span}")
    for path, held in anchors:
        typer.echo(f"anchor: row {held.seq} {held.hash}, written {held.at} ({path}): the chain holds it")
    if not anchors:
        typer.echo(f"anchor: none ({kept} is absent)")
        if matched is None:
            typer.echo(chain.NOTHING_OUTSIDE)
    typer.echo(roles)
    head = done["head"]
    typer.echo(f"head: row {head['seq']} {head['hash']}" if head else "head: none (the audit log is empty)")
