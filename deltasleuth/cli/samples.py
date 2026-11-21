"""``ds samples`` and ``ds labellings``: sign-off samples, and labelled accuracy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import ConfigOpt, _at, _errors, _fail, _in_context, _json, _named, _o
from deltasleuth.core.spec import compile_spec
from deltasleuth.errors import NotFound

if TYPE_CHECKING:
    from deltasleuth.context import Context


samples_app = typer.Typer(invoke_without_command=True)


@samples_app.callback()
def samples_list(
    command: typer.Context,
    spec_name: Annotated[
        str | None, _o("Only this spec name's ledger (its newest version's population).", "--spec")
    ] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List every draw of a sign-off sample, or a spec name's ledger (every spec's draws of its
    population), in commit order: status, whether it blocks a later sign-off, and its close. The CLI
    closes no draw: a signed-in person does, with a note of the fix (POST /v1/samples/close)."""
    if command.invoked_subcommand is not None:
        if spec_name is not None or as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    from deltasleuth.runner import sample
    from deltasleuth.store import repo

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            if spec_name is None:
                return await repo.pages(repo.draws, conn)
            stored = await repo.newest_spec(conn, spec_name)
            if stored is None:
                raise NotFound(f"spec {spec_name} not found")
            found = compile_spec(stored["body"]).sample
            return [] if found is None else await repo.ledger(conn, found.population)

    rows = [{n: v for n, v in d.items() if n not in ("strata", "frame")} for d in _in_context(config, work)]
    for d in rows:
        d["blocking"] = sample.blocking(d)
    if as_json:
        typer.echo(_json(rows))
        return
    for d in rows:
        line = (f"{d['draw_id']}  spec {d['spec_name']}  run {d['run_id']} cycle {d['cycle']}  {d['method']}"
                f"  {d['keys']:,} keys  drawn {_at(d['drawn_at'])}  {d['status']}")  # fmt: skip
        if d["status"] == "failed":
            line += f" ({', '.join(d['failed_strata'])})"
        if d["closed_at"] is not None:
            by = _named(d["closed_by"], d["closed_by_name"])
            line += f"  closed by {by} {_at(d['closed_at'])}: {d['close_note']!r}"
        typer.echo(line + ("  blocking: close it with the fix" if d["blocking"] else ""))


@samples_app.command("size")
def samples_size(
    tier: Annotated[str, _o("The stratum's tier: standard (≤ 0.1%) or high (≤ 0.01%).", "--tier")],
    failures: Annotated[int, typer.Option(min=0, help="Discrepant keys it may still pass with.")] = 0,
) -> None:
    """Print how many cleanly compared keys a sign-off stratum needs to pass the one-sided 95% bound with
    at most N discrepant, and how many to draw, without settings: fix the size in the spec
    before any key is compared."""
    from deltasleuth.core import sampling

    if tier not in sampling.TARGETS:
        _fail(f"--tier is standard or high, not {tier!r}")
    target = sampling.TARGETS[tier]
    with _errors():
        n = sampling.needed(failures, target)
    margin = sampling.percent(sampling.MARGIN)
    typer.echo(
        f"{tier} (≤ {sampling.percent(target)}, one-sided 95%): {n:,} cleanly compared keys pass with at most"
        f" {failures:,} discrepant; draw {sampling.attempted(n):,} ({margin} margin); fix the size before any"
        " key is compared, never extend it"
    )


def labellings(
    run_id: Annotated[str | None, _o("Only this run's labellings (R-…).", "--run")] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the labellings of ended cycles, newest first, each in one line: what its labels count against
    its truth source and whether a sign-off attests it. The CLI draws and labels nothing: an operator does,
    on the Run screen or over the API (POST /v1/runs/{id}/cycles/{n}/labellings)."""
    from deltasleuth.reports import accuracy
    from deltasleuth.store import repo

    async def work(ctx: Context) -> list[dict[str, Any]]:
        async with ctx.pool.connection() as conn:
            if run_id is not None and await repo.get_run(conn, run_id) is None:
                raise NotFound(f"run {run_id} not found")
            rows = await repo.list_labellings(conn, run_id)
            signoffs = await repo.pages(repo.list_approvals, conn, kind="signoff", run_id=run_id)
        attested = accuracy.attesting(signoffs)
        return [accuracy.listed(row, attested.get(row["labelling_id"])) for row in rows]

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    for item in found:
        typer.echo(item["summary"])
