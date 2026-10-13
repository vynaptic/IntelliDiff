"""CSV exports of a run cycle, masked and safe to open in a spreadsheet (OWASP CSV injection).

A cell starting with ``= + - @``, a tab, CR, LF or ``'`` (or a full-width ``= + - @``) is written
quoted with a leading ``'``, which ``read_csv`` strips again, so the escaping round-trips exactly.
A purely numeric cell (``-0.01``) stays a number. Rows end with CRLF (RFC 4180).

Findings go to ``discrepancies`` (discrepant ones, plus every suppressed one with the spec's
``reporting.includeSuppressed``), ``suppressed`` and ``normalization`` (NORMALIZATION_ONLY);
``retry`` lists INCONCLUSIVE and DRIFT keys, live discrepant keys still owed a recheck and cancelled
keys (with the run's stop reason); ``graph`` holds a graph parity run's L2 rows (docs/mvp/GRAPH.md §4).
An evidence pack's ``root_causes`` and ``graph`` leave out their per-key tokens (docs/mvp/PACK.md §2.3).
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from deltasleuth.context import Context
from deltasleuth.core.jsonio import number_text
from deltasleuth.core.types import INCONCLUSIVE_PRECEDENCE, Confirmation, DriftReason, Outcome
from deltasleuth.reports.data import (
    RunData,
    compared_columns,
    filters,
    is_discrepant,
    load_run,
    rank,
    result_pages,
)
from deltasleuth.security.tokenise import mask_path_pattern
from deltasleuth.store import repo
from deltasleuth.store.repo import Row

_TRIGGERS = frozenset("=+-@\t\r\n'" + "\uff1d\uff0b\uff0d\uff20")  # + full-width = + - @
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_NEEDS_QUOTES = re.compile(r'[",\r\n]|^\s|\s$')

FINDING_COLUMNS = tuple(
    "run_id cycle_no key outcome verdict confirmation taxonomy_code severity path_pattern json_pointer "
    "change_type direction value_a_tok value_b_tok value_class member_key_tok root_cause_id signature_hash "
    "rule_id rule_classification mask_id obs_a_id obs_b_id recheck_obs_a_id recheck_obs_b_id "
    "spec_version".split()
)
RETRY_COLUMNS = tuple(
    "run_id key outcome reason side_a_state side_a_reason side_b_state side_b_reason attempts_a attempts_b "
    "detail obs_a_id obs_b_id".split()
)
_FINDING_FIELDS = (
    "taxonomy_code", "severity", "path_pattern", "json_pointer", "change_type", "direction", "value_class",
    "member_key_tok", "root_cause_id", "signature_hash", "rule_id", "rule_classification",
)  # fmt: skip
_RESULT_FIELDS = ("outcome", "verdict", "confirmation", "side_a_state", "side_a_reason", "side_b_state",
                  "side_b_reason", "obs_a_id", "obs_b_id")  # fmt: skip
ROOT_CAUSE_COLUMNS = tuple(
    "root_cause_id kind taxonomy_code max_severity path_pattern change_type direction affected_keys "
    "finding_count member_key_tok value_class signature_hash sample_keys bug_rule_id".split()
)
_IDENTITY = "stratum run_id mode claim_level time_policy spec_name spec_version spec_hash".split()
_COUNTED = "attempted compared matched mismatched mismatched_provisional indeterminate".split()
_COUNTED_MORE = (
    "cancelled outstanding suppressed absent_both api_uncomparable schema_drift normalization_only_keys "
    "mismatch_rate coverage".split()
)
_RENAMED = {"attempted": "total", "matched": "match", "mismatched": "different",
            "mismatched_provisional": "different_provisional"}  # fmt: skip
_BREAKDOWNS = (
    ("outcome", "by_outcome", [o.value for o in Outcome]),
    ("inconclusive", "by_inconclusive_reason", list(INCONCLUSIVE_PRECEDENCE)),
    ("drift", "by_drift_reason", [d.value for d in DriftReason]),
    ("confirmation", "by_confirmation", [c.value for c in Confirmation]),
)
COVERAGE_COLUMNS = (
    *_IDENTITY, *_COUNTED, "inconclusive", "drift", *_COUNTED_MORE, "reconciled",
    *(f"{prefix}_{name}" for prefix, _, names in _BREAKDOWNS for name in names),
    "suppressed_by_rule", "suppressed_by_mask", "selected_fields", "excluded_fields",
    "field_patterns_compared",
    "rows_a", "rows_b", "rows_skipped_a", "rows_skipped_b", "columns_only_a", "columns_only_b",
    "filter_a", "filter_b", "engine_a", "engine_b",
    "rows_rejected_a", "rows_rejected_b", "keys_repeated_a", "keys_repeated_b",
)  # fmt: skip
# A sampled run's coverage appends these, filled on each sign-off stratum's row (docs/mvp/SAMPLING.md §11.2).
SAMPLE_COLUMNS = tuple(
    "tier target basis frame_keys drawn contrast unresolved ub95_one_sided ub95_one_sided_all_suppressed "
    "ub95_one_sided_worst_case verdict".split()
)
GRAPH_COLUMNS = ("seq", "code", "label", "count_a", "count_b", "only_a", "only_b", "detail")
KINDS = ("discrepancies", "retry", "coverage", "normalization", "root_causes", "suppressed", "graph")
# The per-key tokens an evidence pack's root causes and graph rows leave out (docs/mvp/PACK.md §2.3): the
# columns, and a graph row's masked members in its detail; their counts stay.
UNKEYED_COLUMNS = ("member_key_tok", "sample_keys", "label")
_MEMBERS = ("only_a", "only_b", "sample")


def cell_text(value: Any) -> str:
    """None is empty, booleans are lower-case, numbers are exact."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:  # its own text is exact: most cells of a counts export are integers
        return str(value)
    if isinstance(value, float):
        value = Decimal(repr(value))
    return number_text(Decimal(value)) if isinstance(value, Decimal | int) else str(value)


def escape_cell(value: Any) -> str:
    text = cell_text(value)
    if _NUMBER.fullmatch(text):
        return text
    if text and text[0] in _TRIGGERS:
        return '"' + ("'" + text).replace('"', '""') + '"'
    return '"' + text.replace('"', '""') + '"' if _NEEDS_QUOTES.search(text) else text


def render_csv(header: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> str:
    return _lines([header]) + render_rows(header, rows)


def render_rows(header: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> str:
    """The rows' lines without the header's: a file written a page at a time."""
    return _lines([row.get(name) for name in header] for row in rows)


def _lines(lines: Iterable[Sequence[Any]]) -> str:
    return "".join(",".join(escape_cell(cell) for cell in line) + "\r\n" for line in lines)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Header and rows of a CSV (as written here, or edited by a reviewer), unescaped."""
    return parse_csv(path.read_bytes(), path.name)


def parse_csv(data: bytes, name: str = "the upload") -> tuple[list[str], list[dict[str, str]]]:
    """``read_csv`` of an uploaded file's bytes, ``name`` naming it in errors: UTF-8 (a BOM dropped),
    newlines read as a text file reads them, a cell as long as the file allows. UnicodeDecodeError for
    other text; ValueError for what the CSV reader refuses."""
    text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig").read()
    reader = csv.reader(io.StringIO(text, newline=""))
    limit = csv.field_size_limit(max(csv.field_size_limit(), len(text)))  # the reader's own is 131,072
    try:
        header = [_unescape(column) for column in next(reader, None) or []]
        if not header:
            raise ValueError(f"{name} is empty")
        rows = []
        for line_no, cells in enumerate(reader, start=2):
            if not any(cell.strip() for cell in cells):
                continue
            if len(cells) > len(header):
                raise ValueError(f"{name} line {line_no}: more cells than header columns")
            padded = cells + [""] * (len(header) - len(cells))
            rows.append({column: _unescape(cell) for column, cell in zip(header, padded, strict=True)})
    except csv.Error as exc:
        raise ValueError(f"{name} line {reader.line_num}: {exc}") from None
    finally:
        csv.field_size_limit(limit)
    return header, rows


def _unescape(cell: str) -> str:
    return cell[1:] if cell.startswith("'") else cell


async def write_csv(ctx: Context, run_id: str, cycle: int | None, kind: str, *, keyed: bool = True) -> bytes:
    """One export of a run cycle (``KINDS``) as UTF-8 CSV bytes; ``keyed=False``, an evidence pack's,
    leaves out ``UNKEYED_COLUMNS`` and a graph row's masked members. An export of findings, whose rows may
    be every finding of the cycle, is written a page of results at a time."""
    if kind not in KINDS:
        raise ValueError(f"unknown CSV export {kind!r}; one of {', '.join(KINDS)}")
    data = await load_run(ctx, run_id, cycle, results=False)
    paged = False  # an export of findings: its rows follow the header, a page at a time
    if kind == "graph":
        gone = () if keyed else _MEMBERS
        rows = [r | {"detail": {n: v for n, v in r["detail"].items() if n not in gone}} for r in data.graph]
        header, rows = GRAPH_COLUMNS, [r | {"detail": _compact(r["detail"])} for r in rows]
    elif kind == "retry":
        header, rows = RETRY_COLUMNS, await _retry_rows(ctx, data)
    elif kind == "coverage":
        header, rows = COVERAGE_COLUMNS, [_coverage_row(data)] if data.counters else []
        if data.run["params"].get("keys_source") == "hash_sample":
            header, rows = (*header, *SAMPLE_COLUMNS), rows + _strata_rows(data, rows)
    elif kind == "root_causes":
        header, rows = ROOT_CAUSE_COLUMNS, await _root_cause_rows(ctx, data)
    else:
        header, rows, paged = FINDING_COLUMNS, [], True
    if not keyed:
        header = tuple(name for name in header if name not in UNKEYED_COLUMNS)
    out = io.BytesIO()
    out.write(render_csv(header, rows).encode("utf-8"))
    if paged:
        async with ctx.pool.connection() as conn:
            async for page in _finding_rows(conn, data, kind):
                out.write(render_rows(header, page).encode("utf-8"))
    return out.getvalue()


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _routes(f: Row, include_suppressed: bool) -> set[str]:
    if f["suppressed"]:
        return {"suppressed", *(["discrepancies"] if include_suppressed or is_discrepant(f) else [])}
    if rank(f) > 0:
        return {"discrepancies"}
    return {"normalization"} if f["taxonomy_code"] == "NORMALIZATION_ONLY" else set()


async def _finding_rows(conn: repo.Conn, data: RunData, kind: str) -> AsyncIterator[list[Row]]:
    """The export's rows, a page of the cycle's results at a time."""
    include = bool(data.spec.raw.get("reporting", {}).get("includeSuppressed", False))
    run_id = data.run["run_id"]
    async for results in result_pages(conn, data):
        picked = [(r, f) for r in results for f in r["findings"] if kind in _routes(f, include)]
        seqs = list({r["seq"] for r, _ in picked})
        passes = await repo.rechecks(conn, run_id, data.cycle, seqs) if seqs else {}
        rows = []
        for r, f in picked:
            recheck = passes.get(r["key_masked"], {})
            row = {"run_id": run_id, "cycle_no": data.cycle, "key": r["key_masked"],
                   "value_a_tok": f["value_a"], "value_b_tok": f["value_b"],
                   "recheck_obs_a_id": recheck.get("a"), "recheck_obs_b_id": recheck.get("b"),
                   "spec_version": data.spec.version}  # fmt: skip
            rows.append(
                row
                | {n: f[n] for n in _FINDING_FIELDS}
                | {n: r[n] for n in _RESULT_FIELDS if n in FINDING_COLUMNS}
            )
        yield rows


async def _retry_rows(ctx: Context, data: RunData) -> list[Row]:
    from deltasleuth.runner.counters import owed
    from deltasleuth.runner.recheck import RecheckPolicy

    rechecked = {o.value for o in RecheckPolicy.from_spec(data.spec.raw).outcomes}
    run_id, rows = data.run["run_id"], []
    async with ctx.pool.connection() as conn:
        keys = await repo.pages(repo.list_keys, conn, run_id, data.cycle, state="cancelled")
        cancelled = [k["key_masked"] for k in keys]
        unread = set(cancelled)
        async for results in result_pages(conn, data):  # a page of the cycle's results at a time
            unread.difference_update(r["key_masked"] for r in results)
            due = [
                r
                for r in results
                if r["outcome"] in ("INCONCLUSIVE", "DRIFT")
                or owed(r, r["verdict"], live=data.live, due=rechecked)
            ]
            ids = [r[f"obs_{s}_id"] for r in due for s in "ab" if r[f"obs_{s}_id"]]
            read = await repo.get_observations(conn, run_id, ids) if ids else {}
            for r in due:
                attempts = {s: read.get(r[f"obs_{s}_id"], {}).get("attempts") for s in "ab"}
                attrs = r["attrs"]
                sides = sorted(
                    f"{s}: {attrs[f'side_{s}_detail']}" for s in "ab" if attrs.get(f"side_{s}_detail")
                )
                detail = "; ".join(
                    ([attrs["inconclusive_detail"]] if attrs.get("inconclusive_detail") else []) + sides
                )
                row = {"run_id": run_id, "key": r["key_masked"], "detail": detail or None,
                       "reason": r["inconclusive_reason"] or r["drift_reason"] or "recheck_owed",
                       "attempts_a": attempts["a"], "attempts_b": attempts["b"]}  # fmt: skip
                rows.append(row | {n: r[n] for n in _RESULT_FIELDS if n in RETRY_COLUMNS})
    reason = data.run["stop_reason"] or "cancelled"
    return rows + [
        {"run_id": run_id, "key": key, "outcome": "CANCELLED", "reason": reason}
        for key in cancelled
        if key in unread  # a key cancelled before its recheck keeps its first result
    ]


async def _root_cause_rows(ctx: Context, data: RunData) -> list[Row]:
    async with ctx.pool.connection() as conn:
        bug = await repo.bug_rules(conn, data.run["run_id"], data.cycle)
    return [
        {name: rc.get(name) for name in ROOT_CAUSE_COLUMNS}
        | {"sample_keys": ";".join(rc["sample_keys"]), "bug_rule_id": bug.get(rc["root_cause_id"])}
        for rc in data.root_causes
    ]


def _coverage_row(data: RunData) -> Row:
    """The ``*`` stratum row: the counters the runner recorded, plus the run's identity."""
    c, spec = data.counters, data.spec
    row = {
        "stratum": "*", "run_id": data.run["run_id"], "mode": data.run["mode"],
        "claim_level": "observed",  # the live time policy; `certified` needs a frozen cut (deferred)
        "time_policy": spec.raw.get("time", {}).get("mode", "live"),
        "spec_name": spec.name, "spec_version": spec.version, "spec_hash": spec.content_hash,
        **_counted(c),
        "suppressed_by_rule": ";".join(f"{k}={v}" for k, v in c["suppressed_by_rule"].items()),
        "suppressed_by_mask": ";".join(f"{k}={v}" for k, v in c.get("suppressed_by_mask", {}).items()),
        "selected_fields": "; ".join(mask_path_pattern(p, spec) for p in spec.compare.include) or "*",
        "excluded_fields": "; ".join(mask_path_pattern(p, spec) for p in spec.compare.exclude),
    }  # fmt: skip
    return row | _table_cells(data)


def _counted(c: Mapping[str, Any]) -> Row:
    """A row's counter cells: those ``c`` holds (a stratum's lack the counts only findings give)."""
    row = {column: c.get(_RENAMED.get(column, column)) for column in (*_COUNTED, *_COUNTED_MORE)}
    row |= {
        "inconclusive": c["by_outcome"].get("INCONCLUSIVE", 0),
        "drift": c["by_outcome"].get("DRIFT", 0),
        "reconciled": "yes" if all(i["holds"] for i in c["reconciliation"]) else "no",
    }
    for prefix, group, names in _BREAKDOWNS:
        row.update({f"{prefix}_{name}": c[group].get(name, 0) for name in names})
    return row


def _strata_rows(data: RunData, star: Sequence[Row]) -> list[Row]:
    """A sampled cycle's sign-off strata (their members) and its contrast keys, from the judgement its end
    stored (docs/mvp/SAMPLING.md §11.2): none while the cycle is live."""
    if data.sample is None or not star:
        return []
    identity = {name: star[0][name] for name in _IDENTITY}
    strata = [
        identity | _counted(s["counters"]) | {n: s[n] for n in SAMPLE_COLUMNS} | {"stratum": s["label"]}
        for s in data.sample["strata"]
    ]
    return [*strata, identity | _counted(data.sample["contrast"]) | {"stratum": "(contrast)"}]


def _table_cells(data: RunData) -> Row:
    """A table run's plan and scan counts (column names are schema, not data), and each table side's
    engine (a plan that names none is PostgreSQL's); a file side's lines that were not rows and, once
    scanned, its keys held by several rows (docs/mvp/FILES.md §16); none for other runs."""
    tables, scan = data.run["params"].get("tables"), data.run["scan"] or {}
    if tables is None:
        return {}
    return {
        "selected_fields": "; ".join(compared_columns(tables)),
        "rows_a": scan.get("rows_a"), "rows_b": scan.get("rows_b"),
        "rows_skipped_a": scan.get("skipped_a"), "rows_skipped_b": scan.get("skipped_b"),
        "columns_only_a": "; ".join(tables["only_a"]), "columns_only_b": "; ".join(tables["only_b"]),
        **{f"filter_{s}": filters(data.spec).get(s, "") for s in "ab"},
        **{f"engine_{s}": t.get("engine", "postgresql") if (t := tables.get(s)) else "" for s in "ab"},
        **{f"rows_rejected_{s}": (tables.get(s) or {}).get("rejected") for s in "ab"},
        **{f"keys_repeated_{s}": scan.get(f"repeated_{s}") for s in "ab"},
    }  # fmt: skip
