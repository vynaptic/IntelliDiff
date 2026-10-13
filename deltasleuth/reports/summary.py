"""The P0 contract report of a run: Markdown and self-contained HTML, written into its P0 directory.

Built from the run's masked rows, the spec's declarations and what the P0 tools wrote into the
directory (``measure.json``, the adjudication sheet and score); no evidence body is read. A run
that stopped early or cancelled keys is marked PARTIAL, and the adjudication check passes only on
a score of the sheet the directory holds now (same run, same SHA-256): any other score is stale.
Without one, it reads the stored adjudication the cycle's sign-off attests (docs/mvp/PACK.md §5).
The four-eyes section states the spec approval the cycle ran under and its sign-off
(docs/mvp/APPROVALS.md §4); an ended cycle's labelled accuracy is stated as its labelling stored it
(docs/mvp/ACCURACY.md §7); a run made in an unlicensed evaluation or a licence's grace says so in the
warning (docs/mvp/LICENCE.md §10).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from deltasleuth.context import Context
from deltasleuth.reports.adjudicate import EXIT_MINIMUM, SCORE_NAME, SHEET_NAME, sheet_sha256
from deltasleuth.reports.data import (
    RunData,
    accuracy_section,
    approval_lines,
    counters_section,
    graph_section,
    licence_line,
    load_run,
    read_json,
    root_causes_section,
    strata_section,
    tables_line,
)
from deltasleuth.reports.measure import MEASURE_NAME
from deltasleuth.reports.render import Document, Section, render_html, render_markdown

PASS, FAIL, PENDING, MANUAL = "pass", "fail", "pending", "manual"
FINISHED = ("completed", "completed_with_issues")
_OPEN = (
    ("cancelled", "{} key(s) were cancelled and not compared"),
    ("indeterminate", "{} unresolved key(s) (INCONCLUSIVE or DRIFT) are listed in retry.csv"),
    ("outstanding", "{} discrepant key(s) are still owed a recheck"),
    ("different_provisional", "{} discrepant key(s) are provisional (never re-read)"),
)


async def summary_report(
    ctx: Context, run_id: str, out_dir: Path, *, cycle: int | None = None
) -> dict[str, Path]:
    """Write report.md and report.html into ``out_dir``; return their paths."""
    doc = summary_document(await load_run(ctx, run_id, cycle), out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {"report.md": render_markdown(doc), "report.html": render_html(doc)}
    for name, text in outputs.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    return {name: out_dir / name for name in outputs}


def summary_document(data: RunData, out_dir: Path) -> Document:
    run, counters = data.run, data.counters
    measure = _for_run(out_dir / MEASURE_NAME, run["run_id"])
    score = _for_run(out_dir / SCORE_NAME, run["run_id"])
    stale = score is not None and (
        not (out_dir / SHEET_NAME).exists() or score["sheet_sha256"] != sheet_sha256(out_dir / SHEET_NAME)
    )
    attested = None
    if score is None and data.adjudication is not None:  # bound to its sign-off by hash: never stale
        attested, score = data.adjudication["adjudication_id"], data.adjudication["score"]
    cancelled = counters.get("cancelled", 0)
    partial = run["status"] not in FINISHED or bool(run["stop_reason"]) or cancelled > 0
    warning = None
    if partial:
        stopped = f"The run stopped early ({run['stop_reason']}). " if run["stop_reason"] else ""
        warning = (
            f"PARTIAL RUN ({run['status']}). {stopped}{cancelled} of {counters.get('total', 0)} key(s) were "
            "not compared; every figure covers the compared keys only."
        )
    if (run["params"].get("licence") or {}).get("state") in ("evaluation", "grace"):
        warning = " ".join(filter(None, [warning, licence_line(data)]))
    return Document(
        title=f"P0 contract report{' (PARTIAL)' if partial else ''}: {data.spec.name}",
        meta=f"Run {run['run_id']} · {run['mode']} · cycle {data.cycle} · every value is masked "
        "(equal tokens mean equal values)",
        warning=warning,
        sections=[
            _checklist(data, partial, measure, score, stale, attested),
            Section("Four-eyes approval", approval_lines(data)),
            counters_section(counters),
            *filter(None, [strata_section(data), accuracy_section(data)]),
            _spec(data),
            _measured(measure),
            root_causes_section(data, top=10),
            *filter(None, [graph_section(data)]),
            _normalization(data),
            Section("Open items", _open_items(data, measure, score, stale) or ["None."]),
        ],
    )


def _for_run(path: Path, run_id: str) -> Any:
    """A P0 tool's output for this run, else None (absent, or written for another run)."""
    found = read_json(path) if path.exists() else None
    return found if found is not None and found["run_id"] == run_id else None


def _checklist(
    data: RunData, partial: bool, measure: Any, score: Any, stale: bool, attested: str | None
) -> Section:
    c = data.counters
    compared = c.get("compared", 0)
    identities = c.get("reconciliation", [])
    reconciled = bool(identities) and all(i["holds"] for i in identities)
    broken = [f"{i['identity']}: {i['left']} != {i['right']}" for i in identities if not i["holds"]]
    if score is None:
        adjudicated = (PENDING, "adjudicate the run, then score the filled sheet")
    elif stale:
        adjudicated = (FAIL, "score is stale: the sheet changed since it was scored")
    else:
        detail = f"{score['adjudicated']} adjudicated, agreement {score['agreement_rate']}"
        if attested:
            detail += f" (adjudication {attested}, attested by the cycle's sign-off)"
        adjudicated = (PASS if score["exit"]["passed"] else FAIL, detail)
    rows = [
        ("Paired responses ingested and compared", PASS if compared and not partial else FAIL,
         f"{c.get('total', 0)} keys, {compared} compared, {c.get('cancelled', 0)} cancelled"),
        ("Spec validates", PASS, f"content hash {data.spec.content_hash[:12]}"),
        ("Run counters reconcile exactly", PASS if reconciled else FAIL, "; ".join(broken) or "all hold"),
        ("Contract measured and capabilities proposed", PASS if measure else PENDING,
         "measure.json present" if measure else "measure the run"),
        (f"{EXIT_MINIMUM} adjudicated keys agree with reviewers", *adjudicated),
        ("Profiler draft reviewed and LLM profiles probed", MANUAL, "outside this report"),
        ("LLM profiles reviewed by the site", MANUAL, "outside this report"),
    ]  # fmt: skip
    return Section("P0 exit checklist", header=("Check", "Status", "Detail"), rows=rows)


def _spec(data: RunData) -> Section:
    spec = data.spec
    tokenised = ", ".join(f"{t.path} ({t.value_class})" for t in spec.security.tokenize)
    parts = f": {'|'.join(spec.key_parts)}" if spec.key_parts else ""  # a multi-part key's, in key order
    lines = [
        f"{spec.name} {spec.version}, content hash {spec.content_hash}, normalizer {spec.normalizer_version}",
        f"Key {spec.key.name} ({spec.key.type}{parts}, class {spec.key.value_class})",
        f"Selected fields: {'; '.join(spec.compare.include) or '* (every mapped field)'}"
        + (f"; excluded: {'; '.join(spec.compare.exclude)}" if spec.compare.exclude else ""),
        f"Tokenised: {tokenised or 'nothing'}; unlisted paths: {spec.security.unlisted}",
        *(
            f"Known diff {r.rule_id} ({r.classification.value}): {r.reason} [{r.ticket}]"
            for r in spec.known_diffs
        ),
    ]
    lines += [
        f"Side {side.value}: target {cfg.target} ({cfg.role}), select {cfg.select or '$'}"
        + (", mapped" if cfg.map else "")
        + (f", notFound {', '.join(map(str, cfg.not_found.status))}" if cfg.not_found else "")
        + (f", truncation cap {cfg.truncation.cap}" if cfg.truncation else "")
        + (f", {len(cfg.truncation_flags)} truncation flag(s)" if cfg.truncation_flags else "")
        for side, cfg in spec.sides.items()
    ]
    if tables := tables_line(data):
        lines.append(tables)
    return Section("Spec summary", lines)


def _measured(measure: Any) -> Section:
    title = "Measured contract and proposed capabilities"
    if measure is None:
        return Section(title, ["Not measured yet: measure the run."])
    lines = [_side_line(side, m) for side, m in measure["sides"].items()]
    rows = [
        (target, p["field"], "not proposed" if p["value"] is None else json.dumps(p["value"], sort_keys=True),
         " / ".join(p["evidence"] + p["flags"]))
        for target, proposals in measure["proposed_capabilities"].items()
        for p in proposals
    ]  # fmt: skip
    return Section(title, lines, ("Target", "Field", "Proposal", "Evidence and flags"), rows)


def _side_line(side: str, m: dict[str, Any]) -> str:
    statuses = ", ".join(f"{status} x{n}" for status, n in m["statuses"].items())
    return f"Side {side} ({m['target']}): {m['observations']} observations of {m['keys']} keys ({statuses})"


def _normalization(data: RunData) -> Section:
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for r in data.results:
        for f in r["findings"]:
            if f["taxonomy_code"] == "NORMALIZATION_ONLY":
                groups[(f["path_pattern"], f["change_type"], f["value_class"])].append(r["seq"])
    rows = [
        (*ident, len(seqs), len(set(seqs)))
        for ident, seqs in sorted(groups.items(), key=lambda g: -len(g[1]))
    ]
    title = "NORMALIZATION_ONLY summary"
    if not rows:
        return Section(title, ["None."])
    lines = ["Raw values differ but compare equal after the spec's normalisers (INFO, never a mismatch)."]
    return Section(title, lines, ("Path", "Change", "Value class", "Findings", "Keys"), rows)


def _open_items(data: RunData, measure: Any, score: Any, stale: bool) -> list[str]:
    c, run = data.counters, data.run
    items = [f"the run stopped early ({run['stop_reason']})"] if run["stop_reason"] else []
    items += [text.format(c[name]) for name, text in _OPEN if c.get(name)]
    items += [
        f"known diff {rule} suppressed {n} key(s)" for rule, n in c.get("suppressed_by_rule", {}).items()
    ]
    if measure:
        items += [
            f"{target}: {flag}"
            for target, ps in measure["proposed_capabilities"].items()
            for p in ps
            for flag in p["flags"]
        ]
    if stale:
        items.append("adjudication: the score is stale; score the directory's sheet again")
    elif score:
        items += [f"adjudication: {reason}" for reason in score["exit"]["reasons"]]
        if left := score["root_causes_not_adjudicated"]:
            items.append(
                f"adjudication: {len(left)} root cause(s) have no adjudicated key ({', '.join(left[:10])})"
            )
    return items
