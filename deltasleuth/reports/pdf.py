"""The run report (PDF): after the title block (which states the evidence's retention, the spec approval
the cycle ran under, its sign-off, the licence the run was made under and, of a run whose tables the schema
guard recorded a change of, what it recorded through the cycle: docs/mvp/PRECHECKS.md §11), root causes
first, then a compact grid of the cycle's counters, a sampled cycle's strata and bounds, an ended cycle's
labelled
accuracy, the trend against the previous run of the same spec, its pair's readiness gates as of the cycle
(docs/mvp/READINESS.md §11), soak outcome
changes, graph parity (for a run whose spec has ``graph``), the top findings (masked) and, last, the
run's chat answers in an annex labelled as AI output. Model text never appears outside that annex.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from deltasleuth.context import Context
from deltasleuth.core.types import Severity
from deltasleuth.reports import readiness
from deltasleuth.reports.csv import cell_text
from deltasleuth.reports.data import (
    RunData,
    accuracy_section,
    approval_lines,
    counters_section,
    graph_section,
    licence_line,
    load_run,
    retention_line,
    root_causes_section,
    scalar_counters,
    strata_section,
    tables_line,
)
from deltasleuth.reports.render import Document, Section, render_pdf
from deltasleuth.runner import drift
from deltasleuth.store import repo
from deltasleuth.store.repo import Row

AI_LABEL = "AI-generated, not evidence"
TOP_FINDINGS = 25
TOP_CHANGES = 100
_CELL_CHARS = 120
_GRID_PAIRS = 4  # counter/value pairs per grid row, so the counters fit on one page
_TIMES = ("created_at", "started_at", "finished_at")


async def report_document(ctx: Context, run_id: str, cycle: int | None = None) -> Document:
    data = await load_run(ctx, run_id, cycle, results=False)
    async with ctx.pool.connection() as conn:
        previous = await repo.previous_run(conn, run_id)
        changes = await repo.outcome_changes(conn, run_id)
        if answers := await repo.chat_answers(conn, run_id):
            sealer = await ctx.sealer(conn, run_id)
            answers = [sealer.open_row(a) for a in answers]
        ranked = [severity.value for severity in Severity if severity.rank]
        # The discrepant ones (``is_discrepant``), most severe first, then in manifest order.
        found, discrepant = await repo.discrepant_findings(conn, run_id, data.cycle, ranked, TOP_FINDINGS)
        ready = await readiness.run_section(ctx, conn, data.run, data.cycle)
        # The audit rows, never the run's guard: a cycle's report reads the same whenever it is made.
        drifted = await repo.pages(repo.list_audit, conn, action="schema.drift", obj=run_id)
    return Document(
        title=f"DeltaSleuth run report: {data.spec.name}",
        meta=_title_block(data, drift.sentence(drifted, data.cycle)),
        sections=[
            root_causes_section(data),
            _counters_grid(counters_section(data.counters)),
            *filter(None, [strata_section(data), accuracy_section(data)]),
            _trend(data.counters, previous),
            ready,
            _changes(data.run["mode"], changes),
            *filter(None, [graph_section(data)]),
            _top_findings(found, discrepant),
            _annex(answers),
        ],
    )


async def pdf_report(ctx: Context, run_id: str, cycle: int | None = None) -> bytes:
    return render_pdf(await report_document(ctx, run_id, cycle))


def _title_block(data: RunData, guarded: str | None = None) -> str:
    run, spec = data.run, data.spec
    times = (f"{name.replace('_', ' ')} {run[name]:%Y-%m-%d %H:%M:%S %Z}" for name in _TIMES if run[name])
    lines = [
        f"Run {run['run_id']} · {run['mode']} · status {run['status']}"
        f" · cycle {data.cycle} of {run['cycle']}",
        # Never read as a census. Every run of this release is observed; certified needs a frozen cut
        # (deferred).
        "Claim level: observed (live reads of each key, not a certified census)",
        f"Spec {spec.name} {spec.version}, content hash {spec.content_hash}; "
        f"key {spec.key.name} (class {spec.key.value_class})",
        " · ".join(f"side {s.value}: target {c.target} ({c.role})" for s, c in spec.sides.items()),
        tables_line(data),
        guarded,
        " · ".join(times),
        retention_line(data),
        *approval_lines(data),
        licence_line(data),
        *(f"{name.replace('_', ' ')}: {run[name]}" for name in ("stop_reason", "error") if run[name]),
    ]
    return "\n".join(line for line in lines if line)


def _counters_grid(section: Section) -> Section:
    """The counter/value rows laid out ``_GRID_PAIRS`` to a row."""
    if not section.rows:
        return section
    cells = [cell for row in section.rows for cell in row]
    width = 2 * _GRID_PAIRS
    cells += [""] * (-len(cells) % width)
    rows = [cells[i : i + width] for i in range(0, len(cells), width)]
    return replace(section, header=("Counter", "Value") * _GRID_PAIRS, rows=rows)


def _trend(counters: Mapping[str, Any], previous: Row | None) -> Section:
    title = "Trend against the previous run"
    if previous is None or not counters:
        return Section(title, ["No earlier completed run of this spec to compare with."])
    before = previous["counters"]
    rows = [
        (name, before.get(name), value, _delta(before.get(name), value))
        for name, value in scalar_counters(counters)
    ]
    header = ("Counter", "Previous", "Now", "Delta")
    return Section(title, [f"Previous run {previous['run_id']}"], header, rows)


def _delta(before: Any, now: Any) -> str:
    if type(before) is not int or type(now) is not int:
        return ""
    return f"{now - before:+d}"


def _changes(mode: str, changes: list[Row]) -> Section:
    title = "Outcome changes across soak cycles"
    if mode != "soak":
        return Section(title, ["Not a soak run."])
    if not changes:
        return Section(title, ["No key changed its outcome between cycles."])
    rows = [(c["cycle"], c["key_masked"], c["from_outcome"], c["to_outcome"]) for c in changes[:TOP_CHANGES]]
    lines = [f"{len(changes)} change(s); the first {len(rows)} are listed."]
    return Section(title, lines, ("Cycle", "Key", "From", "To"), rows)


def _top_findings(found: list[Row], discrepant: int) -> Section:
    """The first ``TOP_FINDINGS`` discrepant findings (``found``) of the cycle's ``discrepant``."""
    rows = [
        (f["key_masked"], f["severity"], f["taxonomy_code"], f["change_type"], f["json_pointer"],
         _short(f["value_a"]), _short(f["value_b"]), f["root_cause_id"])
        for f in found
    ]  # fmt: skip
    if not rows:
        return Section("Top findings", ["No discrepant finding."])
    header = ("Key", "Severity", "Code", "Change", "Pointer", "A", "B", "Root cause")
    lines = [f"{len(rows)} of {discrepant} discrepant findings (masked)."]
    return Section("Top findings", lines, header, rows)


def _short(value: Any) -> str:
    text = cell_text(value)
    return text if len(text) <= _CELL_CHARS else text[: _CELL_CHARS - 1] + "…"


def _annex(answers: list[Row]) -> Section:
    lines = [
        f"{AI_LABEL}: answers a language model gave about this run from its masked data. "
        "Check every claim against the findings and keys it cites."
    ]
    lines += [
        f"{a['created_at']:%Y-%m-%d %H:%M} · {a['key_masked'] or 'run'} · {a['session_id']}:\n{a['content']}"
        + (f"\nCites: {', '.join(a['citations'])}" if a["citations"] else "")
        for a in answers
    ] or ["No chat answers were given about this run."]
    return Section(f"Annex: {AI_LABEL}", lines)
