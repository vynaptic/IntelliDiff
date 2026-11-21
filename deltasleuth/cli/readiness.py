"""``ds readiness``: cut-over readiness."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import (
    EXIT_DIFF,
    ConfigOpt,
    OutOpt,
    _a,
    _at,
    _fail,
    _in_context,
    _json,
    _local,
    _o,
    _write,
)
from deltasleuth.core.readiness import STATES, config_sha256, worded

if TYPE_CHECKING:
    from deltasleuth.context import Context


readiness_app = typer.Typer(invoke_without_command=True)


def _version(item: dict[str, Any]) -> str:
    """A pair's current version as readiness names one; nothing without evidence."""
    return f" {item['version']} (spec {item['spec_id']})" if item.get("spec_id") is not None else ""


def _pair_line(item: dict[str, Any]) -> str:
    """A pair in one line: its version, entity type, state, gates and evidence cycle."""
    score, at = item["score"], item["evidence"]
    line = f"{item['pair']}{_version(item)}  {item['entity'] or '-'}  {worded(item['state'])}"
    line += f"  {score['passed']} of {score['required']} gates"
    if at is not None:
        ended = f" ({at['ended_at']:%Y-%m-%d})" if "ended_at" in at else ""
        line += f"  cycle {at['cycle']} of {at['run_id']}{ended}"
    return line + ("  regressed" if item["regressed"] else "")


@readiness_app.callback()
def readiness_list(
    command: typer.Context,
    programme: Annotated[str | None, _o("Only this programme's pairs.")] = None,
    entity: Annotated[str | None, _o("Only this entity type's pairs.")] = None,
    state: Annotated[str | None, _o("Only the pairs in this state.")] = None,
    as_of: Annotated[
        str | None, _o("What was recorded by this ISO-8601 time with a UTC offset.", "--as-of")
    ] = None,
    as_json: Annotated[bool, _o("Print GET /v1/readiness's body, every pair in it.", "--json")] = False,
    fail_unless: Annotated[
        str | None, _o("Exit 1 when a listed pair is below this state.", "--fail-unless")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Show cut-over readiness: each programme with how many of its open pairs are parity-ready or better,
    then each pair, worst first, with the gates it does not meet; or show one pair, write the reports, check
    the settings or verify the stored measures. Readiness is named gates, each met or not on observed
    evidence: never a probability, a forecast or sign-off evidence."""
    if command.invoked_subcommand is not None:
        options = (programme, entity, state, as_of, fail_unless, config)
        if as_json or any(option is not None for option in options):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    from deltasleuth import readiness

    if fail_unless is not None and fail_unless not in STATES:
        _fail(f"state must be one of {', '.join(STATES)}")

    async def work(ctx: Context) -> dict[str, Any]:
        asked = partial(
            readiness.overview, ctx, programme=programme, entity=entity, state=state, as_of=as_of, limit=500
        )
        found = page = await asked()
        while page["next_cursor"] is not None:
            page = await asked(cursor=page["next_cursor"])
            found["items"] += page["items"]
        return found | {"next_cursor": None}

    found = _in_context(config, work)
    below = [
        item["pair"]
        for item in found["items"]
        if fail_unless is not None and STATES.index(item["state"]) < STATES.index(fail_unless)
    ]
    if as_json:
        typer.echo(_json(found))
    else:
        typer.echo(found["definition"])
        typer.echo(f"readiness settings {found['config_sha256']}")
        for note in found["notes"]:
            typer.echo(note)
        if as_of is not None:
            typer.echo(f"as recorded by {_at(found['as_of'])}")
        for rolled in found["programmes"]:
            mine = [item for item in found["items"] if item["programme"] == rolled["programme"]]
            worst = worded(rolled["worst"] or "-")
            typer.echo(
                f"{rolled['programme'] or '(no programme)'}: {rolled['ready']} of {rolled['pairs']} pairs"
                f" parity-ready or better (worst: {worst})"
            )
            for item in mine:
                typer.echo(_pair_line(item))
                for gate in item["gates"] if as_of is None else ():
                    if gate["gate"] in item["blocking"]:
                        typer.echo(f"  {gate['gate']} {worded(gate['status'])}: {gate['text']}")
                for deviation in item.get("deviations", ()):
                    typer.echo(f"  deviation: {deviation}")
    if below:
        raise typer.Exit(EXIT_DIFF)


@readiness_app.command("pair")
def readiness_pair(
    name: Annotated[str, _a("The pair: a spec name.")],
    bucket: Annotated[str | None, _o("Its trend by cycle (the default), day or week.")] = None,
    precheck: Annotated[
        bool, _o("Also compute what a sign-off of its candidate would be refused for.", "--precheck")
    ] = False,
    as_json: Annotated[
        bool, _o("Print GET /v1/readiness/pairs/NAME's body, with its trend.", "--json")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Show one pair: every gate by group with what was observed, its convergence, its newest points and,
    with --precheck, what a sign-off of its candidate cycle would be refused for now."""
    from deltasleuth import readiness

    async def work(ctx: Context) -> dict[str, Any]:
        found = await readiness.pair(ctx, name, precheck=precheck)
        return found | {"cycles": (await readiness.cycles(ctx, name, bucket=bucket))["items"]}

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    typer.echo(found["definition"])
    typer.echo(_pair_line(found))
    for note in (*found["notes"], *(f"deviation: {text}" for text in found["deviations"])):
        typer.echo(note)
    for gate in found["gates"]:
        required = "" if gate["required"] else " (not required)"
        typer.echo(f"{gate['group']}  {gate['gate']}{required}  {worded(gate['status'])}: {gate['text']}")
    held = found["convergence"]
    typer.echo(f"convergence: {held['streak']} of {held['needed']} evidence cycles in a row")
    for point in found["cycles"]:
        counts = "not measured" if point["unresolved"] is None else (  # None when any count is
            f"{point['different']:,} different, {point['unresolved']:,} unresolved,"
            f" {point['suppressed']:,} suppressed of {point['total']:,}"
            + (" in its sample" if point["sample_status"] else "")
        )  # fmt: skip
        fails = ", ".join(point["fails"]) or "-"
        typer.echo(
            f"{_at(point['ended_at'])}  cycle {point['cycle']} of {point['run_id']}  spec {point['spec_id']}"
            f"  {point['basis'] or '-'}  {counts}  fails: {fails}"
            + ("  (not evidence)" if point["skipped"] else "")
        )
    if (checked := found.get("precheck")) is not None:
        at = f"cycle {checked['cycle']} of run {checked['run_id']}"
        typer.echo(f"precheck of {at} ({checked['label']}):")
        if checked["purged"]:
            typer.echo("  its evidence is purged: a sign-off is no longer possible")
        for problem in checked["problems"]:
            typer.echo(f"  {problem}")
        if not (checked["purged"] or checked["problems"]):
            typer.echo("  nothing refuses its sign-off")


@readiness_app.command("report")
def readiness_report(
    out: OutOpt,
    programme: Annotated[str | None, _o("Only this programme's pairs.")] = None,
    pair: Annotated[str | None, _o("This pair's brief instead: a spec name.")] = None,
    config: ConfigOpt = None,
) -> None:
    """Write the readiness reports: every dashboard-<n>.pdf volume (100 pairs each, worst first),
    dashboard.csv and trends.csv, or a trends-<pair>.csv per pair when they hold over 100,000 rows; or,
    with --pair, that pair's brief on one page (brief-<pair>.pdf), the brief's points (brief-<pair>.csv)
    and every root-cause group of it (causes-<pair>.csv). Each file is audited as a download."""
    from deltasleuth import readiness
    from deltasleuth.reports import readiness as reports
    from deltasleuth.reports.render import render_pdf
    from deltasleuth.store import repo

    if pair is not None and programme is not None:
        _fail("--pair and --programme cannot be given together")
    who = _local(config)

    async def briefed(ctx: Context, pair: str) -> dict[str, bytes]:
        found = await readiness.brief(ctx, pair)
        return {
            f"brief-{pair}.pdf": await reports.brief_pdf(ctx, found),
            f"brief-{pair}.csv": reports.brief_csv(found).encode(),
            f"causes-{pair}.csv": (await reports.causes_csv(ctx, pair)).encode(),
        }

    async def dashboards(ctx: Context) -> dict[str, bytes]:
        chosen = await reports.selection(ctx, programme=programme)
        files = {
            f"dashboard-{n}.pdf": render_pdf(reports.document(chosen, n))
            for n in range(1, reports.volumes(len(chosen.items)) + 1)
        }
        files["dashboard.csv"] = reports.dashboard_csv(chosen).encode()
        try:
            whole = await reports.trends_csv(ctx, programme=programme, most=reports.TRENDS_ROWS)
            files["trends.csv"] = whole.encode()
        except readiness.Refused:  # over its rows: a file a pair, each whole
            for item in chosen.items:
                files[f"trends-{item['pair']}.csv"] = (
                    await reports.trends_csv(ctx, pair=item["pair"])
                ).encode()
        return files

    async def work(ctx: Context) -> list[Path]:
        files = await (dashboards(ctx) if pair is None else briefed(ctx, pair))
        written = _write(out, files)
        detail = {"programme": programme, "config_sha256": config_sha256(ctx.settings.readiness)}
        detail |= {"pair": pair} if pair is not None else {}
        async with repo.audited(ctx.pool) as (_, audit):
            for name in files:
                await audit(who, "report.download", "readiness", {"name": name} | detail)
        return written

    for path in _in_context(config, work):
        typer.echo(str(path))


@readiness_app.command("check")
def readiness_check(config: ConfigOpt = None) -> None:
    """Print each pair's effective gates, each tagged with where it comes from (default, installation,
    programme or pair), what deviates from the built-in defaults and the settings hash; warn of a listed
    spec that is not stored. The settings apply at the next restart of the server and the worker."""
    from deltasleuth import readiness

    lines, warnings = _in_context(config, readiness.check)
    for line in lines:
        typer.echo(line)
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)


@readiness_app.command("verify")
def readiness_verify(config: ConfigOpt = None) -> None:
    """Check each cycle's stored measures against the readiness.measured audit row naming it, each such
    row against the cycle it names, and each excluded marker against its run; exit 2 names the first that
    does not agree. The database owner can edit, remove or replace audited measures: this detects it, with
    the audit chain (ds audit verify); it does not see what was changed before a cycle was measured."""
    from deltasleuth import readiness

    found = _in_context(config, readiness.verify)
    typer.echo(
        f"readiness: {found['measured']:,} measured cycle(s) match their readiness.measured rows;"
        f" {found['excluded']:,} excluded, {found['errors']:,} not measured"
    )
