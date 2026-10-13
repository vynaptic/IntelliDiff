"""A run cycle as every report reads it from the store, and the sections reports share.

Rows are the repo's: already masked (findings, root causes, ``key_masked``). Raw keys and headers
stay sealed in them, except in the observations opened for a report that needs their bodies; nothing
here or downstream publishes a raw key.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deltasleuth import approvals, licence
from deltasleuth.connectors.tables import relation
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.types import CompiledSpec, Observation, Outcome, Severity
from deltasleuth.reports.render import Section
from deltasleuth.store import repo
from deltasleuth.store.repo import Row

TOP_ROOT_CAUSES = 20
TOP_GRAPH_ROWS = 20  # graph parity rows a report lists of each code; graph.csv holds them all
# jsonb keeps no key order: the familiar counters first, any others after them by name.
_COUNTER_ORDER = (
    "total", "compared", "match", "different", "different_provisional", "indeterminate", "cancelled",
    "outstanding", "suppressed", "mismatch_rate", "coverage",
)  # fmt: skip


@dataclass(frozen=True)
class RunData:
    run: Row
    spec: CompiledSpec
    cycle: int
    results: list[Row]  # manifest order, each with its ``findings`` and derived ``verdict``; [] unless read
    root_causes: list[Row]  # most probable first
    counters: Row  # the cycle's; empty until it ends
    live: bool  # read from the endpoints, not from sample files
    graph: list[Row]  # the cycle's graph parity rows; none for other runs
    retention_days: int  # of the run's retention class
    expires_at: datetime | None  # when the cycle's retention ends; None while it is live
    approval: Row  # the spec approval the cycle ran under (approvals.run_approval)
    signoffs: list[Row]  # the cycle's sign-off requests, newest first
    adjudication: Row | None = None  # the stored one its requested or approved sign-off attests
    draw: Row | None = None  # a sampled cycle's draw
    sample: Row | None = None  # its judgement, stored when the cycle ended; None while it is live
    labelling: Row | None = None  # the ended cycle's labelling reports state
    labelling_attested: str | None = None  # the requested or approved sign-off attesting it
    labellings: int = 0  # without one: the labellings drawn of the cycle, none of them labelled


def retention_line(data: RunData) -> str:
    """Until when the cycle's raw evidence is kept."""
    run, kept = data.run, f"(retention {data.run['retention']})"
    if run["hold_reason"] is not None:
        return f"Evidence held until released {kept}"
    if data.expires_at is None:
        return f"Evidence retained until {data.retention_days} days after the cycle ends {kept}"
    return f"Evidence retained until {data.expires_at:%Y-%m-%d} {kept}"


def approval_lines(data: RunData) -> list[str]:
    """What the spec approval the cycle ran under and its sign-off say."""
    return [approvals.label(data.approval), *filter(None, [approvals.signoff_label(data.signoffs)])]


def licence_line(data: RunData) -> str | None:
    """What the licence the run was made under says; None without a record."""
    return licence.run_label(data.run)


def rank(f: Mapping[str, Any]) -> int:
    return Severity(f["severity"]).rank if f["severity"] else 0


def is_discrepant(f: Mapping[str, Any]) -> bool:
    """Above INFO and unsuppressed, or suppressed by a ``bug`` rule (a known bug)."""
    return rank(f) > 0 and (not f["suppressed"] or f["rule_classification"] == "bug")


def compared_columns(tables: Mapping[str, Any]) -> list[str]:
    """A table run's compared canonical columns: side a's plan, or the only table side's."""
    return sorted((tables.get("a") or tables["b"])["columns"])  # jsonb keeps no key order


def filters(spec: CompiledSpec) -> dict[str, str]:
    """Each table side's filter as the spec writes it, by side."""
    return {s.value: c.table.where for s, c in spec.sides.items() if c.table is not None and c.table.where}


def _engine(side: str, plan: Mapping[str, Any], scan: Mapping[str, Any] | None) -> str:
    """The engine a side's plan names; a file side's with its format, its files, its lines that were not
    rows and, once scanned, its keys held by several rows."""
    if plan.get("engine") != "files":
        return f" ({plan['engine']})" if "engine" in plan else ""
    counts = f"{plan['file_count']:,} file(s), {plan['rejected']:,} line(s) not rows"
    repeated = (scan or {}).get(f"repeated_{side}")
    counts += f", {repeated:,} repeated key(s)" if repeated is not None else ""
    return f" (files: {plan['file']['format']}, {counts})"


def tables_line(data: RunData) -> str | None:
    """A table run's plan and scan counts as one line, each side with the engine
    its plan names (a file side has no schema); None for other runs."""
    run = data.run
    tables, scan = run["params"].get("tables"), run["scan"]
    if tables is None:
        return None
    parts = [
        ", ".join(f"side {s} {relation(t)}{_engine(s, t, scan)}" for s in "ab" if (t := tables.get(s))),
        f"compared columns: {', '.join(compared_columns(tables)) or 'none (keys only)'}",
        *(f"only on side {s}: {', '.join(tables[f'only_{s}'])}" for s in "ab" if tables[f"only_{s}"]),
        *(f"filter on side {s}: {text}" for s, text in filters(data.spec).items()),
    ]
    if scan:
        parts += [
            f"rows read: a {scan['rows_a']}, b {scan['rows_b']}",
            f"not compared (NULL, empty or over-long key): a {scan['skipped_a']}, b {scan['skipped_b']}",
        ]
    return "Tables: " + "; ".join(parts)


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(jsonio.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


async def load_run(ctx: Context, run_id: str, cycle: int | None = None, *, results: bool = True) -> RunData:
    """The run, its spec and one cycle (the current one by default); LookupError if unknown,
    ``runner.purge.CyclePurged`` once the cycle's purge has started.

    ``results=False``, for a report that lists none or reads them a page at a time (``result_pages``),
    reads none. The counters are those the cycle's end stored (the run's ``counters``, which name their
    cycle) whenever the run holds them, and are never counted again then. A run holds its latest summarised
    cycle's only: a cycle before the run's current one whose counters it no longer holds (a soak's) is
    counted again from its stored rows, a page at a time; the current cycle has none until its end stores
    them.
    """
    from deltasleuth.reports import accuracy  # it reads sheets through adjudicate, which reads runs here
    from deltasleuth.runner.counters import Tally, verdict
    from deltasleuth.runner.purge import days, refuse_purged
    from deltasleuth.runner.recheck import RecheckPolicy

    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        cycle = cycle or run["cycle"]
        await refuse_purged(conn, run_id, cycle)
        ended = await repo.list_cycles(conn, run_id, **days(ctx))
        expires = next((c["expires_at"] for c in ended if c["cycle"] == cycle), None)
        spec = await ctx.spec(conn, run["spec_id"])  # not live: a removed target keeps its history
        counters = run["counters"] if run["counters"].get("cycle") == cycle else {}
        recount = not counters and cycle < run["cycle"]  # an ended cycle: the run keeps its latest only
        rows = await repo.pages(repo.list_results, conn, run_id, cycle) if results else []
        findings = await repo.pages(repo.list_findings, conn, run_id, cycle) if results else []
        causes = await repo.list_root_causes(conn, run_id, cycle)
        run["scan"] = await repo.get_scan(conn, run_id, cycle)  # the reported cycle's
        live = run["params"].get("samples_dir") is None
        if recount:
            tally = Tally(live=live, recheck_outcomes=RecheckPolicy.from_spec(spec.raw).outcomes)
            async for page, found in repo.result_pages(conn, run_id, cycle):
                for r in page:
                    tally.add(r, found.get(r["key_masked"], ()))
            total = sum((await repo.cycle_counts(conn, run_id, cycle))["keys"].values())
            bulk = (run["scan"] or {}).get("matched", 0)
            counters = tally.counters(total=total + bulk, bulk_match=bulk) | {"cycle": cycle}
        graph = await repo.pages(repo.list_graph_diffs, conn, run_id, cycle)
        approval = await approvals.run_approval(conn, run, cycle)
        signoffs = await repo.approval_history(conn, run_id=run_id, cycle=cycle)
        active = next((s for s in signoffs if s["status"] in ("requested", "approved")), None)
        fixed = active and active["attests"].get("adjudication")  # attests format 1
        adjudication = fixed and await repo.cycle_adjudication(conn, run_id, cycle, fixed["adjudication_id"])
        drawn = await repo.get_draw(conn, run_id, cycle)
        judged = (await repo.get_cycle(conn, run_id, cycle) or {}).get("sample")
        labelling, attested, unlabelled = None, None, 0
        if expires is not None:  # an ended cycle's; a live one has none
            labelling, attested = await accuracy.reported(conn, run_id, cycle, signoffs)
            unlabelled = 0 if labelling else len(await repo.list_labellings(conn, run_id, cycle))
    by_key: dict[str, list[Row]] = defaultdict(list)
    for f in findings:
        by_key[f["key_masked"]].append(f)
    for r in rows:
        r["findings"] = by_key[r["key_masked"]]
        r["verdict"] = verdict(r, r["findings"]).value
    kept = days(ctx)[f"{run['retention']}_days"]
    return RunData(
        run, spec, cycle, rows, causes, counters, live, graph, kept, expires, approval, signoffs,
        adjudication, drawn, judged, labelling, attested, unlabelled,
    )  # fmt: skip


async def result_pages(conn: repo.Conn, data: RunData) -> AsyncIterator[list[Row]]:
    """The cycle's results as ``RunData.results`` holds them (each with its ``findings`` and derived
    ``verdict``), a page at a time: an export of a cycle's rows never holds them all."""
    from deltasleuth.runner.counters import verdict

    async for results, findings in repo.result_pages(conn, data.run["run_id"], data.cycle):
        for r in results:
            r["findings"] = findings.get(r["key_masked"], [])
            r["verdict"] = verdict(r, r["findings"]).value
        yield results


async def observations(ctx: Context, data: RunData) -> list[Observation]:
    """Every observation of the cycle with its body from the evidence store."""
    from deltasleuth.runner.compare import body_of

    source = "live" if data.live else "file"
    async with ctx.pool.connection() as conn:
        rows = await repo.list_observations(conn, data.run["run_id"], data.cycle)
        sealer = await ctx.sealer(conn, data.run["run_id"])
    found = []
    for obs in (repo.as_observation(sealer.open_row(row)) for row in rows):
        found.append(replace(obs, body=body_of(ctx, obs), source=source))
    return found


def scalar_counters(counters: Mapping[str, Any]) -> list[tuple[str, Any]]:
    scalars = {name: v for name, v in counters.items() if not isinstance(v, dict | list) and name != "cycle"}
    names = [n for n in _COUNTER_ORDER if n in scalars] + sorted(scalars.keys() - set(_COUNTER_ORDER))
    return [(name, scalars[name]) for name in names]


def counters_section(counters: Mapping[str, Any]) -> Section:
    """Scalar counters, then the non-zero keys by outcome, inconclusive reason and drift reason."""
    if not counters:
        return Section("Coverage counters", ["Counters are computed when the cycle ends."])
    outcomes = counters.get("by_outcome", {})
    rows = scalar_counters(counters)
    rows += [(f"outcome {o.value}", outcomes[o.value]) for o in Outcome if outcomes.get(o.value)]
    for group, label in (("by_inconclusive_reason", "inconclusive"), ("by_drift_reason", "drift")):
        rows += [(f"{label} {name}", n) for name, n in sorted(counters.get(group, {}).items()) if n]
    return Section("Coverage counters", header=("Counter", "Value"), rows=rows)


def root_causes_section(data: RunData, top: int = TOP_ROOT_CAUSES) -> Section:
    rows = [
        (rc["seq"] + 1, rc["root_cause_id"], rc["taxonomy_code"], rc["max_severity"],
         f"{rc['change_type']} ({rc['direction']})", rc["path_pattern"], rc["affected_keys"],
         rc["finding_count"])
        for rc in data.root_causes[:top]
    ]  # fmt: skip
    if not rows:
        return Section("Root causes", ["No discrepancy to group."])
    more = len(data.root_causes) - len(rows)
    header = ("Rank", "Root cause", "Code", "Severity", "Change", "Path", "Keys", "Findings")
    return Section("Root causes", [f"{more} more in root_causes.csv."] if more else [], header, rows)


def strata_section(data: RunData) -> Section | None:
    """A sampled cycle's draw and each sign-off stratum's counts, bounds and verdict, as its end stored
    them, never recounted; None for a cycle without a draw."""
    from deltasleuth.core.sampling import percent

    drawn, judged = data.draw, data.sample
    if drawn is None:
        return None
    title, frame = "Sample and bounds (observed)", drawn["frame"]
    origin = frame["origin"]
    lines = [
        f"Draw {drawn['draw_id']} at {drawn['drawn_at'].astimezone(UTC):%Y-%m-%d %H:%M} UTC, by"
        f" {drawn['method']}: salt {drawn['salt_label']} with a nonce the installation drew; selection"
        f" SHA-256 {drawn['selection_sha256']}.",
        f"Frame: {frame['keys']:,} distinct keys (a only {origin['a']:,}; b only {origin['b']:,}; both"
        f" {origin['both']:,}); {frame['unfit']:,} outside key.pattern.",
    ]
    if judged is None and data.expires_at is None:  # live: no ds.cycles row yet
        return Section(title, [*lines, "Bounds are judged when the cycle ends."])
    if judged is None:  # its judgement raised when it ended (unjudged)
        unjudged = "the cycle ended without a judgement and cannot be signed off: draw again."
        return Section(title, [*lines, f"Judgement: none: {unjudged}"])
    lines += [f"Judgement: {judged['status']}.", "No bound is given for the strata pooled."]
    rows = [
        (s["label"], s["tier"], percent(s["target"]), s["basis"], s["frame_keys"], s["drawn"], s["n"], s["x"],
         s["unresolved"], s["absent_both"], s["api_uncomparable"], s["retried"],
         s["ub95_one_sided"] and percent(s["ub95_one_sided"]),
         s["ub95_one_sided_all_suppressed"] and percent(s["ub95_one_sided_all_suppressed"]),
         s["ub95_one_sided_worst_case"] and percent(s["ub95_one_sided_worst_case"]), s["verdict"])
        for s in judged["strata"]
    ]  # fmt: skip
    contrast = judged["contrast"]
    lines.append(
        f"Contrast keys (compared, never bounded): {contrast['total']:,}, {contrast['compared']:,} compared,"
        f" {contrast['different']:,} discrepant."
    )
    header = ("Stratum", "Tier", "Target", "Basis", "Frame keys", "Drawn", "Compared", "Discrepant",
              "Unresolved", "Absent both", "Truncated", "Retried", "Bound", "All suppressed", "Worst case",
              "Verdict")  # fmt: skip
    return Section(title, lines, header, rows)


def accuracy_section(data: RunData) -> Section | None:
    """An ended cycle's labelled accuracy: what its labelling says
    (``accuracy.lines``, whole) and may not be read as (``LIMITS``), or that none was labelled, with how
    many were drawn; None for a live cycle. The labelling is the one its sign-off attests, else the newest
    a person labelled, else the newest labelled."""
    from deltasleuth.reports import accuracy

    if data.expires_at is None:  # live: no ds.cycles row yet
        return None
    title = "Labelled accuracy (observed)"
    if data.labelling is None:
        return Section(title, [accuracy.not_measured(data.labellings)])
    return Section(title, [*accuracy.lines(data.labelling, signoff=data.labelling_attested), accuracy.LIMITS])


def graph_section(data: RunData) -> Section | None:
    """Bulk graph parity of a run whose spec has ``graph``; None for others."""
    if not data.spec.raw.get("graph"):
        return None
    title, g = "Graph parity", (data.run["scan"] or {}).get("graph")
    if g is None:
        return Section(title, ["Not computed: L2 runs at the end of a complete table scan."])
    by_code: dict[str, list[Row]] = defaultdict(list)
    for row in data.graph:
        by_code[row["code"]].append(row)
    shown = [row for rows in by_code.values() for row in rows[:TOP_GRAPH_ROWS]]
    lines = [
        f"Groups: {g['groups_a']} on side a, {g['groups_b']} on side b; {g['groups_equal']} link the same "
        f"members, {g['groups_different']} differ.",
        f"Components: {g['split']} split, {g['merge']} merge, {g['rearrange']} rearranged, {g['oversized']} "
        "too large to expand.",
        # A global index needs every component, and L2 expands only the affected ones.
        "ARI and VI are not reported: L2 expands only the components the differing groups touch.",
        "Counts are members for a group or an oversized component, and components for a split, merge or "
        "rearrange.",
    ]
    if more := len(data.graph) - len(shown):
        lines.append(f"The first {TOP_GRAPH_ROWS} rows of each code are listed; {more} more in graph.csv.")
    rows = [
        (r["code"], r["label"], r["count_a"], r["count_b"], r["only_a"], r["only_b"],
         jsonio.dumps(r["detail"], sort_keys=True))
        for r in shown
    ]  # fmt: skip
    return Section(title, lines, ("Code", "Label", "Count A", "Count B", "Only A", "Only B", "Detail"), rows)
