"""``ds precheck``: table pre-checks."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

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
    _issues,
    _json,
    _local,
    _named,
    _o,
    _save_spec,
    _until,
    _write,
)
from deltasleuth.core import precheck as prechecking
from deltasleuth.core.spec import SpecIssue

if TYPE_CHECKING:
    from deltasleuth.context import Context


precheck_app = typer.Typer(invoke_without_command=True)


PrecheckArg = Annotated[str, _a("The table pre-check's id (PC-…).")]
_FAIL_ON = {"refused": 2, "differs": 7, "incomplete": 8}  # the rank from which, towards the worst, each fails


def _precheck_line(item: dict[str, Any]) -> str:
    """A pre-check in one line: its level and source, who made it and when, and whether it has ended."""
    ended = "cancelled" if item["cancelled_at"] else "finished" if item["finished_at"] else "not finished"
    made = f"by {_named(item['created_by'], item['created_by_name'])} {_at(item['created_at'])}"
    line = f"{item['precheck_id']}  {item['source']}  level {item['level']}  {made}  {ended}"
    return line + (f"  {prechecking.EVALUATION}" if item["evaluation"] else "")


def _precheck_shown(found: dict[str, Any]) -> None:
    """Print a pre-check: what it is not, the label of an unlicensed evaluation, its line, its banner, its
    totals, then each pair's line worst first (a pair not read yet says so by its status)."""
    typer.echo(found["not"])
    if found["evaluation"]:
        typer.echo(prechecking.EVALUATION)
    for line in (_precheck_line(found | {"evaluation": False}), *found["banners"], found["totals"]):
        typer.echo(line)
    for pair in found["items"]:
        typer.echo(f"{pair['n']}  {pair['label']}  {pair['text'] or pair['status']}")


@precheck_app.callback()
def precheck_list(
    command: typer.Context,
    as_json: Annotated[bool, _o("Print GET /v1/prechecks's body, every pre-check in it.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the table pre-checks, newest first, each with its pairs counted by verdict, worst first; or
    show one, run one, cancel one or write its reports. A table pre-check counts and adds up the two tables
    of each pair; it does not compare rows, and it is observed, never evidence or readiness. Only a worker
    reads the targets (ds worker, ds serve, or ds precheck run)."""
    if command.invoked_subcommand is not None:
        if as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    from deltasleuth import prechecks

    async def work(ctx: Context) -> dict[str, Any]:
        found = page = await prechecks.overview(ctx, limit=500)
        while page["next_cursor"] is not None:
            page = await prechecks.overview(ctx, limit=500, cursor=page["next_cursor"])
            found["items"] += page["items"]
        return found | {"next_cursor": None}

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    for line in (found["not"], *found["banners"]):
        typer.echo(line)
    if not found["items"]:
        typer.echo("no table pre-checks")
    for item in found["items"]:
        typer.echo(_precheck_line(item))
        typer.echo(f"  {item['totals']}")


@precheck_app.command("show")
def precheck_show(
    precheck_id: PrecheckArg,
    pair: Annotated[int | None, _o("Show this pair: its lines, then each column's figures.")] = None,
    verdict: Annotated[str | None, _o("Only the pairs of this verdict.")] = None,
    as_json: Annotated[bool, _o("Print the API's body.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show one table pre-check: what it is not, its totals and each pair's line, worst first; or, with
    --pair, everything read of one pair: its lines, then each compared column's figures."""
    from deltasleuth import prechecks

    if verdict is not None and verdict not in prechecking.VERDICTS:
        _fail(f"--verdict is one of {', '.join(prechecking.VERDICTS)}")
    if pair is None:
        found = _in_context(
            config, lambda ctx: prechecks.described(ctx, precheck_id, verdict=verdict, limit=prechecks.PAIRS)
        )
        if as_json:
            typer.echo(_json(found))
        else:
            _precheck_shown(found)
        return
    found = _in_context(config, lambda ctx: prechecks.pair(ctx, precheck_id, pair))
    if as_json:
        typer.echo(_json(found))
        return
    typer.echo(found["not"])
    if found["evaluation"]:
        typer.echo(prechecking.EVALUATION)
    sides = ", ".join(f"{t} {r or '-'}" for t, r in zip(found["targets"], found["relations"], strict=True))
    said = found["said"] or found["status"]
    typer.echo(f"pair {found['n']} of {precheck_id}: {found['label']} ({sides}): {said}")
    for line in (found["result"] or {}).get("lines", [found["text"]] if found["text"] else []):
        typer.echo(line)
    for column in found["columns"]:
        for line in column["lines"]:
            typer.echo(f"  {column['column']}: {line}")


@precheck_app.command("run")
def precheck_run(
    specs: Annotated[list[Path] | None, _a("Specs of two tables (YAML or JSON), stored first.")] = None,
    spec_id: Annotated[list[int] | None, _o("A stored spec version (repeatable).", "--spec-id")] = None,
    spec_name: Annotated[
        list[str] | None, _o("A spec name's newest stored version (repeatable).", "--spec")
    ] = None,
    all_specs: Annotated[
        bool, _o("The newest version of every stored spec whose sides are both tables.", "--all-specs")
    ] = False,
    side_a: Annotated[
        str | None, _o("Side a of two schemas paired by name: a database target, then :schema.", "--a")
    ] = None,
    side_b: Annotated[str | None, _o("Side b of two schemas paired by name, as --a.", "--b")] = None,
    include: Annotated[
        list[str] | None, _o("Only the tables whose lower-cased name this glob matches (repeatable).")
    ] = None,
    exclude: Annotated[list[str] | None, _o("Not the tables this glob matches (repeatable).")] = None,
    level: Annotated[
        str | None, _o("What it reads: schema, counts, columns or distinct (columns; counts for --a, --b).")
    ] = None,
    wait: Annotated[bool, _o("Read it here until it ends.")] = True,
    fail_on: Annotated[
        str | None,
        _o(
            "Exit 1 when a pair is this bad or worse (refused, differs: a pair not read too, or incomplete),"
            " or when the pre-check was cancelled.",
            "--fail-on",
        ),
    ] = None,
    as_json: Annotated[bool, _o("Print the pre-check as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Make a table pre-check of stored specs, or of two schemas' tables paired by name (--a, --b), and,
    without --no-wait, read it here with an in-process worker: each pair's two tables are counted and added
    up inside one snapshot a side, and no row is read. It says where to look first; it does not compare
    rows."""
    from deltasleuth import prechecks

    if fail_on is not None and fail_on not in _FAIL_ON:
        _fail(f"--fail-on is one of {', '.join(_FAIL_ON)}")
    if fail_on is not None and not wait:
        _fail("--fail-on waits for the pre-check's end: leave out --no-wait")
    named = side_a is not None or side_b is not None
    if named and (side_a is None or side_b is None or specs or spec_id or spec_name or all_specs):
        _fail("--a and --b come together, and without a spec")
    if (include or exclude) and not named:
        _fail("--include and --exclude take --a and --b")
    ids = [0] * len(specs or ()) + list(spec_id or ())  # a file's id is its stored version's
    body: dict[str, Any] = {"spec_ids": ids or None, "spec_names": spec_name, "all_specs": all_specs}
    if named:
        for side, given in (("a", side_a), ("b", side_b)):
            assert given is not None  # both, as checked above
            target, _, schema = given.partition(":")
            body[side] = {"target": target, "schema": schema or None}
        body = {side: body[side] for side in "ab"} | {"include": include, "exclude": exclude}
    body["level"] = level or ("counts" if named else "columns")
    with _errors():
        (prechecks.refuse_catalogue if named else prechecks.refuse)(body)  # before a file is stored
    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        if named:
            made = await prechecks.catalogue(ctx, body, who)
        else:
            stored = [(await _save_spec(ctx, path, who, gated=False))[0] for path in specs or ()]
            ids_ = (stored + ids[len(stored) :]) or None
            made = await prechecks.create(ctx, body | {"spec_ids": ids_}, who)
        precheck_id = made["precheck_id"]

        async def finished() -> tuple[bool, dict[str, Any]]:
            found = await prechecks.described(ctx, precheck_id, limit=prechecks.PAIRS)
            return found["finished_at"] is not None, found

        return await _until(ctx, wait, finished)

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
    else:
        _precheck_shown(found)
    if not wait:
        typer.echo("read by 'ds worker' or 'ds serve'", err=as_json)
    worst = _FAIL_ON.get(fail_on or "", 0)
    bad = [p for p in found["items"] if p["verdict"] and prechecking.rank(p["verdict"]) <= worst]
    if fail_on is not None and (bad or found["cancelled_at"] is not None):
        raise typer.Exit(EXIT_DIFF)


@precheck_app.command("cancel")
def precheck_cancel(precheck_id: PrecheckArg, config: ConfigOpt = None) -> None:
    """Cancel an unfinished table pre-check: its pending and running pairs are cancelled, and what was read
    stays."""
    from deltasleuth import prechecks

    who = _local(config)
    _in_context(config, lambda ctx: prechecks.cancel(ctx, precheck_id, who))
    typer.echo(f"cancelled {precheck_id}")


@precheck_app.command("draft")
def precheck_draft(
    precheck_id: PrecheckArg,
    out: OutOpt,
    pair: Annotated[int | None, _o("Draft this pair.")] = None,
    every: Annotated[bool, _o("Draft every pair of two tables that was read.", "--all")] = False,
    config: ConfigOpt = None,
) -> None:
    """Write a starter spec of a pair of two schemas' tables paired by name, as PC-…-pair-N.draft.yaml: both
    tables, their common primary key and the columns both hold of one family, each column left out as a
    comment; its validate_spec issues head it. Never stored: when it is ready, upload it on the Specs screen
    or with POST /v1/specs, or, outside SSO, give the file to ds run, which stores the spec it runs."""
    from deltasleuth import prechecks

    if (pair is None) == (not every):
        _fail("give --pair N or --all, and only one of them")

    async def work(ctx: Context) -> list[tuple[int, str, list[SpecIssue]]]:
        if pair is not None:
            return [(pair, *await prechecks.draft(ctx, precheck_id, pair))]
        found = await prechecks.described(ctx, precheck_id, limit=prechecks.PAIRS)
        drafted = []
        for item in sorted(found["items"], key=lambda item: item["n"]):
            with suppress(prechecks.Refused):  # a stored spec, an unpaired name, a pair not read
                drafted.append((item["n"], *await prechecks.draft(ctx, precheck_id, item["n"])))
        return drafted

    drafted = _in_context(config, work)
    if not drafted:
        _fail(f"no pair of {precheck_id} can be drafted: none is two tables paired by name and read")
    for n, text, issues in drafted:
        with _errors():
            path = _write(out, {f"{precheck_id}-pair-{n}.draft.yaml": text.encode()})[0]
        typer.echo(str(path))
        _issues(issues)


@precheck_app.command("report")
def precheck_report(precheck_id: PrecheckArg, out: OutOpt, config: ConfigOpt = None) -> None:
    """Write a table pre-check's reports as PC-…-<name> (PC-…-unlicensed-evaluation-<name> for one made
    in an unlicensed evaluation): precheck.csv, precheck-columns.csv and precheck.pdf; each file is
    audited as a download."""
    from deltasleuth import prechecks
    from deltasleuth.reports import precheck as reports
    from deltasleuth.store import repo

    who = _local(config)

    async def work(ctx: Context) -> list[Path]:
        row, pairs = await reports.loaded(ctx, precheck_id)
        rendered = {name: reports.render(name, row, pairs)[1] for name in reports.NAMES}
        written = _write(out, {prechecks.file_name(row, name): data for name, data in rendered.items()})
        async with repo.audited(ctx.pool) as (_, audit):
            for name in reports.NAMES:
                await audit(who, "report.download", precheck_id, {"name": name})
        return written

    for path in _in_context(config, work):
        typer.echo(str(path))
