"""``ds compare``, ``run``, ``runs`` and ``report``: one key now, a run, the runs and their reports."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import (
    EXIT_DIFF,
    ConfigOpt,
    CycleOpt,
    FailOpt,
    OutOpt,
    RunArg,
    SpecFile,
    _a,
    _errors,
    _fail,
    _in_context,
    _local,
    _notify_warned,
    _o,
    _read_keys,
    _save_spec,
    _until,
    _warned,
    _wrote,
)
from deltasleuth.core.types import CompiledSpec, Verdict
from deltasleuth.errors import NotFound

if TYPE_CHECKING:
    from deltasleuth.context import Context


VALUE_WIDTH = 60
SUMMARY = "summary"  # the contract report (report.md, report.html); the other kinds are reports.FORMATS
_COUNTERS = ("total", "compared", "match", "different", "indeterminate", "cancelled", "outstanding")


def _unapproved(ctx: Context, spec_id: int, spec: CompiledSpec, run_id: str) -> None:
    """Warn that a run in tokens mode, which has no approvers, is labelled unapproved."""
    from deltasleuth import approvals

    if ctx.settings.auth_mode == "tokens" and (needed := approvals.needs(spec)):
        typer.echo(
            f"warning: spec {spec_id} ({spec.name} {spec.version}) needs a four-eyes approval of"
            f" {', '.join(needed)}; tokens mode has no approvers, so run {run_id} is labelled unapproved",
            err=True,
        )


def _short(value: Any) -> str:
    text = "<absent>" if value is None else str(value)
    return text if len(text) <= VALUE_WIDTH else text[: VALUE_WIDTH - 1] + "…"


def compare(
    key: Annotated[str, _a("The key to compare.")],
    spec: Annotated[Path, _o("Comparison spec (YAML or JSON).")],
    as_json: Annotated[bool, _o("Print the masked result as JSON.", "--json")] = False,
    fail_on_diff: FailOpt = False,
    config: ConfigOpt = None,
) -> None:
    """Compare one key now (rechecked when discrepant): outcome and differing fields, masked."""
    from deltasleuth.runner import compare_one
    from deltasleuth.runner.counters import verdict
    from deltasleuth.store import repo

    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        await _warned(ctx)
        spec_id, compiled = await _save_spec(ctx, spec, who)
        result = await compare_one(ctx, spec_id, key, who=who)
        run_id, masked = result.attrs["run_id"], result.attrs["key_masked"]
        _unapproved(ctx, spec_id, compiled, run_id)
        for line in result.attrs["warnings"]:  # the schema guard's
            typer.echo(f"warning: {line}", err=True)
        async with ctx.pool.connection() as conn:
            detail = await repo.get_key_detail(conn, run_id, masked)
        assert detail is not None  # the key just compared
        result, findings = detail["result"], detail["findings"]
        return {"run_id": run_id, "key_masked": masked, "result": result, "findings": findings}

    view = _in_context(config, work)
    r, findings = view["result"], view["findings"]
    judged = verdict(r, findings)
    if as_json:
        typer.echo(json.dumps(view, default=str, indent=2, sort_keys=True))
    else:
        reason = r["inconclusive_reason"] or r["drift_reason"]
        typer.echo(
            f"{view['key_masked']}: {r['outcome']} ({judged.value}, {r['confirmation']})"
            + (f" reason {reason}" if reason else "")
        )
        for f in findings:
            if f["severity"] != "INFO" or f["suppressed"]:
                rule = f" suppressed by {f['rule_id']}" if f["suppressed"] else ""
                where = f"{f['severity']} {f['taxonomy_code']} {f['change_type']} {f['json_pointer'] or '/'}"
                typer.echo(
                    f"  {where}{rule}\n      A: {_short(f['value_a'])}\n      B: {_short(f['value_b'])}"
                )
        typer.echo(f"run {view['run_id']}; observations a {r['obs_a_id']} b {r['obs_b_id']}")
    if fail_on_diff and judged is Verdict.DIFFERENT:
        raise typer.Exit(EXIT_DIFF)


def run(
    spec: SpecFile,
    keys: Annotated[Path | None, _o("Key file, one per line (default: the spec's key source).")] = None,
    samples: Annotated[Path | None, _o("File mode: a captured samples directory.")] = None,
    soak: Annotated[bool, _o("A soak run: a cycle every execution.cycleInterval.", "--soak")] = False,
    rolling: Annotated[bool, _o("Soak: read the key source again each cycle.", "--rolling")] = False,
    wait: Annotated[bool, _o("Process the run here until it ends.")] = True,
    fail_on_diff: FailOpt = False,
    config: ConfigOpt = None,
) -> None:
    """Start a batch (or soak) run; without --no-wait, process it here with an in-process worker. What the
    schema guard says of its tables at its creation is printed as warnings; a run it paused prints its
    lines and exits 2."""
    from deltasleuth.runner import create_run, drift, sample
    from deltasleuth.store import repo

    who = _local(config)
    with _errors():
        key_list = _read_keys(keys) if keys else None

    async def work(ctx: Context) -> tuple[repo.Row, repo.Row | None]:
        await _warned(ctx)
        _notify_warned(ctx.settings)
        spec_id, compiled = await _save_spec(ctx, spec, who)
        mode, settings = ("soak", {"rolling": rolling}) if soak else ("batch", None)
        run_id = await create_run(
            ctx, spec_id, mode, keys=key_list, samples_dir=samples, soak=settings, who=who
        )
        typer.echo(f"run {run_id} started")
        _unapproved(ctx, spec_id, compiled, run_id)
        async with ctx.pool.connection() as conn:
            created = await repo.get_run(conn, run_id)
        assert created is not None  # created above
        if created["status"] != "paused":  # one created paused prints its lines below
            for line in drift.warnings(created):
                typer.echo(f"warning: {line}", err=True)

        async def ended() -> tuple[bool, repo.Row]:
            async with ctx.pool.connection() as conn:
                row = await repo.get_run(conn, run_id)
            assert row is not None  # created above; a purge never deletes a running run
            return row["status"] in (*repo.TERMINAL, "paused"), row

        row = await _until(ctx, wait, ended)
        async with ctx.pool.connection() as conn:
            return row, await repo.get_cycle(conn, run_id, row["cycle"])

    row, ended = _in_context(config, work)
    guarded = row["status"] == "paused" and row["stop_reason"] == drift.SCHEMA_PAUSE
    if not wait and not guarded:
        typer.echo("processed by 'ds worker' or 'ds serve'")
        return
    c, why = row["counters"], row["stop_reason"] or row["error"]
    typer.echo(row["status"] + (f" ({why})" if why else ""))
    if refused := (row.get("draw") or {}).get("error"):  # its frame refused it
        typer.echo(f"  {refused}")
    for line in drift.lines(row) if guarded else ():  # the schema guard paused it
        typer.echo(f"  {line}")
    if row["status"] in ("paused", "failed"):  # a paused run resumes from the UI or the API
        _fail(f"the run is {row['status']}")
    typer.echo("  " + ", ".join(f"{name} {c[name]}" for name in _COUNTERS))
    typer.echo("  " + ", ".join(f"{o} {n}" for o, n in sorted(c["by_outcome"].items()) if n))
    for line in sample.lines((ended or {}).get("sample") or {"strata": []}):
        typer.echo(f"  {line}")
    if fail_on_diff and (c["different"] or c["outstanding"]):
        raise typer.Exit(EXIT_DIFF)


def runs(limit: int = 20, config: ConfigOpt = None) -> None:
    """List the latest runs, newest first, each with what its spec approval says.

    A run made in a build that enforces a licence also shows the licence it was made under; a build without
    a vendor key enforces none.
    """
    from deltasleuth import approvals, licence
    from deltasleuth.runner import drift
    from deltasleuth.store import repo

    async def work(ctx: Context) -> list[tuple[repo.Row, str, repo.Row | None]]:
        async with ctx.pool.connection() as conn:
            found = (await repo.list_runs(conn, limit=limit))[0]
            return [
                (r, approvals.label(await approvals.run_approval(conn, r)),
                 await repo.get_draw(conn, r["run_id"], r["cycle"]))
                for r in found
            ]  # fmt: skip

    for r, said, drawn in _in_context(config, work):
        c = r["counters"]
        found = f"  different {c['different']} of {c['total']}" if c else ""
        typer.echo(f"{r['run_id']}  {r['spec_name']} {r['spec_version']}  {r['mode']}  {r['status']}"
                   f"  cycle {r['cycle']}{found}")  # fmt: skip
        typer.echo(f"  {said}")
        if drawn is not None:  # a sampled run's current draw
            strata = len(drawn["strata"])
            typer.echo(f"  sample {drawn['draw_id']}: {strata} {'stratum' if strata == 1 else 'strata'},"
                       f" {drawn['keys']:,} keys")  # fmt: skip
        elif r["params"].get("keys_source") == "hash_sample" and r.get("draw") is not None:
            if r["status"] == "running" and not r["draw"].get("error"):  # a stopped run's draw never runs
                typer.echo("  drawing sample")
            elif r["status"] == "paused":
                typer.echo("  sample to be drawn after resume")
        if label := licence.run_label(r):
            typer.echo(f"  {label}")
        if r["status"] == "paused" and r["stop_reason"] == licence.LICENCE_PAUSE:  # its next cycle waits
            typer.echo(f"  Paused for the licence at the end of cycle {r['cycle'] - 1}")
        if r["status"] == "paused" and r["stop_reason"] == drift.SCHEMA_PAUSE:
            for line in drift.lines(r)[:1]:  # the pause's
                typer.echo(f"  {line}")


def report(
    run_id: RunArg,
    out: OutOpt,
    kind: Annotated[list[str] | None, _o("A report the API serves, or summary (repeatable).")] = None,
    cycle: CycleOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Write a run's reports, masked, as <run>-<name>: the CSV exports and the PDF the API serves, or the
    contract report (summary: report.md and report.html).

    A run made in the unlicensed evaluation of a build that enforces a licence is written as
    <run>-unlicensed-evaluation-<name>; a build without a vendor key enforces none.
    """
    from deltasleuth.reports import FORMATS, download
    from deltasleuth.reports.summary import summary_report
    from deltasleuth.store import repo

    kinds = kind or ["discrepancies.csv", "retry.csv", "coverage.csv", "report.pdf"]
    if unknown := sorted(set(kinds) - {*FORMATS, SUMMARY}):
        _fail(f"unknown report {', '.join(unknown)}; one of {', '.join([*FORMATS, SUMMARY])}")
    who = _local(config)

    async def work(ctx: Context) -> list[Path]:
        async with ctx.pool.connection() as conn:
            if await repo.get_run(conn, run_id) is None:
                raise NotFound(f"run {run_id} not found")
        out.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for k in kinds:
            if k == SUMMARY:
                written += (await summary_report(ctx, run_id, out, cycle=cycle)).values()
                await _wrote(ctx, who, run_id, k, cycle)
                continue
            got = await download(ctx, who, run_id, k, cycle)  # audited before it is written
            written.append(path := out / got.file_name)
            if isinstance(got.content, bytes):
                path.write_bytes(got.content)
                continue
            await _write_chunks(path, got.content)
        return written

    for path in _in_context(config, work):
        typer.echo(str(path))


async def _write_chunks(path: Path, chunks: AsyncGenerator[bytes]) -> None:
    """Install a complete streamed export, keeping an existing report if the read or write fails."""
    async with aclosing(chunks):
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                async for chunk in chunks:
                    file.write(chunk)
                file.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
