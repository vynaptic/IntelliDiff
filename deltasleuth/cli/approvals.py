"""``ds approvals``: four-eyes approvals and sign-offs, and the break-glass revocation of one."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import REASON, ConfigOpt, _a, _at, _fail, _in_context, _json, _local, _named, _o
from deltasleuth.errors import Invalid
from deltasleuth.security import rawvalues

if TYPE_CHECKING:
    from deltasleuth.context import Context


approvals_app = typer.Typer(invoke_without_command=True)


_STATUSES = ("requested", "approved", "rejected", "revoked")
_KINDS = ("spec", "signoff")
_LIVE = ("queued", "running", "paused")  # the runs --needed looks at: they run again


@approvals_app.callback()
def approvals_list(
    command: typer.Context,
    status: Annotated[str | None, _o("Only this status: requested, approved, rejected or revoked.")] = None,
    kind: Annotated[str | None, _o("Only this kind: spec or signoff.")] = None,
    needed: Annotated[
        bool,
        _o("List instead the spec versions live runs use that need an approval and have none.", "--needed"),
    ] = False,
    limit: Annotated[int | None, typer.Option(min=1, max=500, help="At most this many (default 50).")] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the four-eyes approvals and sign-offs, newest first; or revoke one. The CLI can neither
    request nor grant one: people do, signed in."""
    if command.invoked_subcommand is not None:
        if needed or as_json or any(option is not None for option in (status, kind, limit, config)):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    if status is not None and status not in _STATUSES:
        _fail(f"--status is one of {', '.join(_STATUSES)}")
    if kind is not None and kind not in _KINDS:
        _fail(f"--kind is one of {', '.join(_KINDS)}")
    if needed:
        if status is not None or kind is not None or limit is not None:
            _fail("--needed lists spec versions: it takes no --status, --kind or --limit")
        _needed(_in_context(config, _unapproved_versions), as_json)
        return

    from deltasleuth.store import repo

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            return (await repo.list_approvals(conn, status=status, kind=kind, limit=limit or 50))[0]

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for a in rows:
        spec = f"spec {a['spec_id']} ({a['spec_name']} {a['spec_version']})"
        what = (
            f"cycle {a['cycle']} of run {a['run_id']}" if a["kind"] == "signoff" else ", ".join(a["scopes"])
        )
        line = f"{a['approval_id']}  {a['kind']}  {a['status']}  {spec}  {what or 'no scopes'}"
        line += f"  requested by {_named(a['requested_by'], a['requested_by_name'])} {_at(a['requested_at'])}"
        if a["first_by"] is not None:
            line += f"  step 1 by {_named(a['first_by'], a['first_by_name'])} {_at(a['first_at'])}"
        if a["decided_by"] is not None:
            line += f"  decided by {_named(a['decided_by'], a['decided_by_name'])} {_at(a['decided_at'])}"
        if a["revoked_by"] is not None:
            by = _named(a["revoked_by"], a["revoked_by_name"])
            line += f"  revoked by {by} {_at(a['revoked_at'])}: {a['revoke_reason']!r}"
        typer.echo(line)


async def _unapproved_versions(ctx: Context) -> list[dict[str, Any]]:
    """The spec versions of live runs that need an approval their active one (if any) does not give,
    each with those runs."""
    from deltasleuth import approvals
    from deltasleuth.store import repo

    async with ctx.pool.connection() as conn:
        live = [r for status in _LIVE for r in await repo.pages(repo.list_runs, conn, status=status)]
        versions = []
        for spec_id in sorted({r["spec_id"] for r in live}):
            needs = approvals.needs(await ctx.spec(conn, spec_id))
            active = await repo.active_spec_approval(conn, spec_id)
            if not needs or (active is not None and set(needs) <= set(active["scopes"])):
                continue
            runs = sorted((r for r in live if r["spec_id"] == spec_id), key=lambda r: r["run_id"])
            versions.append({
                "spec_id": spec_id, "name": runs[0]["spec_name"], "version": runs[0]["spec_version"],
                "needs": needs, "approval_id": active and active["approval_id"],
                "runs": [{"run_id": r["run_id"], "status": r["status"]} for r in runs],
            })  # fmt: skip
    return versions


def _needed(versions: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        typer.echo(_json(versions))
        return
    for v in versions:
        runs = ", ".join(f"run {r['run_id']} ({r['status']})" for r in v["runs"])
        covers = f" ({v['approval_id']} does not cover them all)" if v["approval_id"] else ""
        typer.echo(
            f"spec {v['spec_id']} ({v['name']} {v['version']}) needs {', '.join(v['needs'])}{covers}: {runs}"
        )
    if not versions:
        typer.echo("every live run's spec version has the approval it needs")


@approvals_app.command("revoke")
def approvals_revoke(
    approval_id: Annotated[str, _a("The approval id (A-…).")],
    reason: Annotated[str, _o(REASON)],
    config: ConfigOpt = None,
) -> None:
    """Break-glass: revoke an approved spec approval or sign-off. A spec approval's running runs pause,
    its queued one-shot runs stop and the sign-offs citing it are revoked; open ones are refused at
    their decision."""
    from deltasleuth import approvals
    from deltasleuth.runner import purge as purger
    from deltasleuth.store import repo

    if problem := purger.reason_error(reason):
        _fail(problem)
    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        async with repo.audited(ctx.pool) as (conn, audit):
            approval = await repo.get_approval(conn, approval_id)
            if approval is not None and rawvalues.key_shaped(
                await ctx.spec(conn, approval["spec_id"]), reason
            ):
                raise Invalid(rawvalues.KEY_SHAPED)  # audit rows and packs carry it forever
            return await approvals.revoke(conn, audit, approval_id, reason, who=who)

    done = _in_context(config, work)
    typer.echo(
        f"revoked {approval_id}; runs paused: {', '.join(done['paused']) or '-'}; runs stopped:"
        f" {', '.join(done['stopped']) or '-'}; sign-offs revoked: {', '.join(done['revoked']) or '-'}"
    )
