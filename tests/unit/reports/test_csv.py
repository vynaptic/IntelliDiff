"""CSV exports: OWASP escaping that round-trips, and each export's rows from a stored run."""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from deltasleuth.context import Context
from deltasleuth.core.spec import TargetRegistry, compile_spec
from deltasleuth.core.types import Confirmation
from deltasleuth.reports import FORMATS
from deltasleuth.reports.csv import (
    COVERAGE_COLUMNS,
    FINDING_COLUMNS,
    GRAPH_COLUMNS,
    KINDS,
    ROOT_CAUSE_COLUMNS,
    escape_cell,
    read_csv,
    render_csv,
    write_csv,
)
from deltasleuth.reports.measure import measure_run
from deltasleuth.reports.pdf import report_document
from deltasleuth.store import repo

from .conftest import SECRET_NAME, TARGETS, compare_key, doc, graph_row, seed_graph_run, seed_run, unit_spec

FORMULA = "=cmd|' /C calc'!A0"
TABLE_COLUMNS = COVERAGE_COLUMNS[-14:]
TABLE_SPEC = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: unit-tables, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key: {name: userid, type: string, class: business_code}
sides:
  a: {role: legacy, target: db-a, table: {name: users, keyColumns: [userid]}}
  b: {role: new, target: db-b, table: {name: users_v, keyColumns: [user_id]}}
compare: {}
recheck: {delay: 0s, times: 1}
"""


@pytest.mark.parametrize(
    "value", ['=HYPERLINK("http://x.invalid")', "+1+2", "-1+2", "@SUM(A1)", "\tTAB", "\rCR", "\nLF",
              "\uff1dFULL", "\uff0b1", "\uff0d1", "\uff20x", "'quoted", "-1e5", "-"],
)  # fmt: skip
def test_formula_triggers_are_prefixed_and_quoted(value: str) -> None:
    escaped = escape_cell(value)
    assert escaped.startswith("\"'") and next(csv.reader(io.StringIO(escaped))) == ["'" + value]


@pytest.mark.parametrize("value", ["-0.01", "42", "0", "3.14159", Decimal("-0.010"), 12, -3, 0.1])
def test_purely_numeric_cells_stay_numbers(value: object) -> None:
    assert escape_cell(value)[0] not in "'\""


def test_round_trip_restores_every_value(tmp_path: Path) -> None:
    rows = [{"a": FORMULA, "b": "-0.01", "c": "'x", "d": 'q"uote', "e": None}, {"a": "@x", "b": "two\nlines"}]
    path = tmp_path / "t.csv"
    path.write_text(render_csv(["a", "b", "c", "d", "e"], rows), newline="")
    assert path.read_bytes().endswith(b"\r\n")
    header, back = read_csv(path)
    assert header == ["a", "b", "c", "d", "e"]
    assert back == [
        {"a": FORMULA, "b": "-0.01", "c": "'x", "d": 'q"uote', "e": ""},
        {"a": "@x", "b": "two\nlines", "c": "", "d": "", "e": ""},
    ]


def test_read_csv_tolerates_reviewer_edits_and_rejects_overlong_rows(tmp_path: Path) -> None:
    path = tmp_path / "edited.csv"
    path.write_text("\ufeffkey,verdict,notes\r\nK1,MATCH\r\n\r\nK2,DIFFERENT,odd\r\n", encoding="utf-8")
    assert read_csv(path)[1] == [
        {"key": "K1", "verdict": "MATCH", "notes": ""},
        {"key": "K2", "verdict": "DIFFERENT", "notes": "odd"},
    ]
    path.write_text("a\r\n1,2\r\n", encoding="utf-8")
    with pytest.raises(ValueError, match="more cells"):
        read_csv(path)


async def _exports(ctx: Context, run_id: str, tmp_path: Path) -> dict[str, list[dict[str, str]]]:
    found = {}
    for kind in KINDS:
        data = await write_csv(ctx, run_id, None, kind)
        assert SECRET_NAME.encode() not in data
        (tmp_path / kind).write_bytes(data)
        found[kind] = read_csv(tmp_path / kind)[1]
    return found


@pytest.mark.db
async def test_exports_route_findings_and_keep_values_masked(ctx: Context, tmp_path: Path) -> None:
    run_id = await seed_run(
        ctx,
        {
            "K001": (doc("K001"), doc("K001", name=SECRET_NAME + " ")),  # NORMALIZATION_ONLY
            "K002": (doc("K002", status="OLD"), doc("K002", status="NEW")),  # known diff (accepted)
            "K003": (doc("K003"), "timeout"),  # INCONCLUSIVE
            "K004": (doc("K004", status="A"), doc("K004", status=FORMULA)),  # MISMATCH
            "K005": (doc("K005"), doc("K005")),
        },
    )
    csvs = await _exports(ctx, run_id, tmp_path)
    (row,) = csvs["discrepancies"]
    assert (row["key"], row["outcome"], row["verdict"], row["value_b_tok"]) == (
        "K004",
        "MISMATCH",
        "DIFFERENT",
        FORMULA,
    )
    assert b"\"'" + FORMULA.encode() in (tmp_path / "discrepancies").read_bytes()  # escaped on disk
    assert row["root_cause_id"].startswith("RC-") and read_csv(tmp_path / "discrepancies")[0] == list(
        FINDING_COLUMNS
    )
    assert [(r["key"], r["rule_id"]) for r in csvs["suppressed"]] == [("K002", "KD-UNIT-1")]
    assert [r["key"] for r in csvs["normalization"]] == ["K001"]
    (retry,) = csvs["retry"]
    assert (retry["key"], retry["reason"], retry["attempts_b"], retry["side_b_state"]) == (
        "K003",
        "timeout",
        "3",
        "ERROR",
    )
    (cause,) = csvs["root_causes"]
    assert (cause["root_cause_id"], cause["sample_keys"], cause["path_pattern"]) == (
        row["root_cause_id"],
        "K004",
        "$.status",
    )
    # An evidence pack's leaves out the key tokens; the counts stay.
    (tmp_path / "unkeyed").write_bytes(await write_csv(ctx, run_id, None, "root_causes", keyed=False))
    header, (unkeyed,) = read_csv(tmp_path / "unkeyed")
    assert header == [name for name in ROOT_CAUSE_COLUMNS if name not in ("member_key_tok", "sample_keys")]
    assert unkeyed == {name: cause[name] for name in header}
    (coverage,) = csvs["coverage"]
    assert list(coverage) == list(COVERAGE_COLUMNS)
    assert (coverage["attempted"], coverage["outcome_MISMATCH"], coverage["reconciled"]) == ("5", "1", "yes")
    assert [coverage[name] for name in TABLE_COLUMNS] == [""] * len(TABLE_COLUMNS)  # a service run has none
    with pytest.raises(ValueError, match="unknown CSV export"):
        await write_csv(ctx, run_id, None, "everything")


@pytest.mark.db
async def test_live_discrepancy_is_owed_a_recheck_until_rechecked(ctx: Context, tmp_path: Path) -> None:
    pair = (doc("K001", status="A"), doc("K001", status="B"))
    run_id = await seed_run(ctx, {"K001": pair}, live=True)
    (owed,) = (await _exports(ctx, run_id, tmp_path))["retry"]
    assert (owed["key"], owed["reason"]) == ("K001", "recheck_owed")
    async with ctx.pool.connection() as conn:  # the recheck pass re-reads and confirms
        await compare_key(
            ctx, conn, unit_spec(), run_id, "K001", pair, pass_no=2, confirmation=Confirmation.RECHECKED
        )
        second = await repo.get_key_detail(conn, run_id, "K001")
    csvs = await _exports(ctx, run_id, tmp_path)
    assert csvs["retry"] == []
    (row,) = csvs["discrepancies"]
    rechecked = {o["side"]: o["obs_id"] for o in second["observations"] if o["pass_no"] == 2}
    assert (row["recheck_obs_a_id"], row["recheck_obs_b_id"]) == (rechecked["a"], rechecked["b"])


@pytest.mark.db
async def test_the_spec_can_list_suppressed_findings_with_the_discrepancies(
    ctx: Context, tmp_path: Path
) -> None:
    spec = unit_spec(reporting={"includeSuppressed": True})
    run_id = await seed_run(ctx, {"K002": (doc("K002", status="OLD"), doc("K002", status="NEW"))}, spec=spec)
    (row,) = (await _exports(ctx, run_id, tmp_path))["discrepancies"]
    assert (row["key"], row["outcome"], row["rule_id"]) == ("K002", "KNOWN_DIFF", "KD-UNIT-1")


@pytest.mark.db
async def test_set_array_values_show_as_arrays(ctx: Context, tmp_path: Path) -> None:
    # A whole `mode: set` array read as its internal member map ({} for an empty list).
    empty, two = doc("K001", tags=()), doc("K002")
    run_id = await seed_run(
        ctx,
        {"K001": (empty, {k: v for k, v in empty.items() if k != "tags"}), "K002": (two, {**two, "tags": 1})},
    )
    rows = (await _exports(ctx, run_id, tmp_path))["discrepancies"]
    assert [(r["key"], r["json_pointer"], r["value_a_tok"]) for r in rows] == [
        ("K001", "/tags", "[]"),
        ("K002", "/tags", '["x", "y"]'),
    ]


@pytest.mark.db
async def test_every_report_of_a_run_survives_the_removal_of_its_targets(
    ctx: Context, tmp_path: Path
) -> None:
    # A stored run is read with its spec and the capabilities it ran with, not today's
    # targets; FORMATS lists every downloadable report.
    run_id = await seed_run(ctx, {"K001": (doc("K001"), doc("K001", status="B"))})
    gone = replace(ctx, targets=TargetRegistry({}))
    assert set(FORMATS) == {"report.pdf", "readiness.csv", *(f"{kind}.csv" for kind in KINDS)}
    for media, render in FORMATS.values():
        data = await render(gone, run_id, None)
        assert data.startswith(b"%PDF-") if media == "application/pdf" else data.endswith(b"\r\n")
    assert (await measure_run(gone, run_id, tmp_path))["sides"]["b"]["observations"] == 1


@pytest.mark.db
async def test_reports_of_a_table_run_list_its_columns_filters_and_scan_counts(
    ctx: Context, tmp_path: Path
) -> None:
    side = {"schema": "public", "keys": ["userid"], "columns": {"name": "name", "email": "email"}}
    tables = {"a": side | {"name": "users", "where": "\"status\" = ( 'active' )"},  # the plan's form
              "b": side | {"name": "users_v", "keys": ["user_id"]},
              "only_a": ["legacy_flag"], "only_b": []}  # fmt: skip
    scan = {"done": True, "after": ["1001"], "rows_a": 1000, "rows_b": 1001, "skipped_a": 0, "skipped_b": 1,
            "matched": 996, "candidates": 1}  # fmt: skip
    pair = ({"email": "user5@example.com"}, {"email": "USER5@example.com"})
    doc = yaml.safe_load(TABLE_SPEC)
    doc["sides"]["a"]["table"]["where"] = "status = 'active'"
    spec = compile_spec(doc, targets=TARGETS)
    run_id = await seed_run(ctx, {"5": pair}, spec=spec, live=True, tables=tables, scan=scan)
    (coverage,) = (await _exports(ctx, run_id, tmp_path))["coverage"]
    assert [coverage[name] for name in ("selected_fields", *TABLE_COLUMNS)] == [
        "email; name",
        "1000",
        "1001",
        "0",
        "1",
        "legacy_flag",
        "",
        "status = 'active'",
        "",
        "postgresql",
        "postgresql",
        "",
        "",
        "",
        "",
    ]  # each filter as the spec writes it; a plan that names no engine is PostgreSQL's; no file side
    assert (
        "Tables: side a public.users, side b public.users_v; compared columns: email, name; only on side a: "
        "legacy_flag; filter on side a: status = 'active'; rows read: a 1000, b 1001; not compared "
        "(NULL, empty or over-long key): a 0, b 1"
    ) in (await report_document(ctx, run_id)).meta.splitlines()
    # A pair with a side on another engine names each side's.
    mixed = tables | {"a": tables["a"] | {"engine": "mysql"}, "b": tables["b"] | {"engine": "oracle"}}
    other = await seed_run(ctx, {"5": pair}, spec=spec, live=True, tables=mixed, scan=scan)
    (coverage,) = (await _exports(ctx, other, tmp_path))["coverage"]
    assert (coverage["engine_a"], coverage["engine_b"]) == ("mysql", "oracle")
    [line] = [m for m in (await report_document(ctx, other)).meta.splitlines() if m.startswith("Tables: ")]
    assert line.startswith("Tables: side a public.users (mysql), side b public.users_v (oracle); compared")
    # A file side has no schema: its format, its files, its lines that were not rows
    # and, once scanned, its keys held by several rows.
    files = {"schema": None, "engine": "files", "file": {"path": "users/*.csv", "format": "csv"},
             "file_count": 12, "rejected": 3}  # fmt: skip
    filed = tables | {"a": tables["a"] | files, "b": tables["b"] | {"engine": "postgresql"}}
    fixed = await seed_run(
        ctx, {"5": pair}, spec=spec, live=True, tables=filed, scan=scan | {"repeated_a": 5}
    )
    (coverage,) = (await _exports(ctx, fixed, tmp_path))["coverage"]
    assert [coverage[name] for name in COVERAGE_COLUMNS[-6:]] == ["files", "postgresql", "3", "", "5", ""]
    [line] = [m for m in (await report_document(ctx, fixed)).meta.splitlines() if m.startswith("Tables: ")]
    assert line.startswith(
        "Tables: side a users (files: csv, 12 file(s), 3 line(s) not rows, 5 repeated key(s)), side b"
        " public.users_v (postgresql); compared"
    )
    # Scanned, with no repeated key: 0, in the cell and on the line (they were empty and absent, as for a
    # side that did not scan or is no file side).
    none = await seed_run(ctx, {"5": pair}, spec=spec, live=True, tables=filed, scan=scan | {"repeated_a": 0})
    (coverage,) = (await _exports(ctx, none, tmp_path))["coverage"]
    assert [coverage[name] for name in COVERAGE_COLUMNS[-4:]] == ["3", "", "0", ""]
    [line] = [m for m in (await report_document(ctx, none)).meta.splitlines() if m.startswith("Tables: ")]
    assert line.startswith(
        "Tables: side a users (files: csv, 12 file(s), 3 line(s) not rows, 0 repeated key(s))"
    )
    unscanned = await seed_run(ctx, {"5": pair}, spec=spec, live=True, tables=filed)
    [line] = [m for m in (await report_document(ctx, unscanned)).meta.splitlines() if m.startswith("Tables")]
    assert line.startswith("Tables: side a users (files: csv, 12 file(s), 3 line(s) not rows), side b ")


@pytest.mark.db
async def test_graph_csv_lists_a_graph_runs_rows_and_only_its_header_for_other_runs(
    ctx: Context, tmp_path: Path
) -> None:
    sample = {"members": 2, "sample": ["tok:m2", "tok:m3"]}
    rows = [
        graph_row("GROUP_MEMBERS_DIFFER", "tok:g1", detail={"only_a": ["tok:m1"], "only_b": []}),
        graph_row("CLUSTER_SPLIT", "tok:m2", count_a=1, count_b=2, only_a=0, detail=sample),
    ]
    graphed, other = (
        await seed_graph_run(ctx, rows),
        await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}),
    )
    (tmp_path / "graph.csv").write_bytes(await write_csv(ctx, graphed, None, "graph"))
    header, found = read_csv(tmp_path / "graph.csv")
    assert header == list(GRAPH_COLUMNS)
    assert [list(r.values()) for r in found] == [
        ["0", "GROUP_MEMBERS_DIFFER", "tok:g1", "2", "1", "1", "0", '{"only_a":["tok:m1"],"only_b":[]}'],
        ["1", "CLUSTER_SPLIT", "tok:m2", "1", "2", "0", "0", '{"members":2,"sample":["tok:m2","tok:m3"]}'],
    ]
    assert await write_csv(ctx, other, None, "graph") == (",".join(GRAPH_COLUMNS) + "\r\n").encode()
    # An evidence pack's: no masked label or members; the counts stay.
    (tmp_path / "unkeyed.csv").write_bytes(await write_csv(ctx, graphed, None, "graph", keyed=False))
    header, found = read_csv(tmp_path / "unkeyed.csv")
    assert header == [name for name in GRAPH_COLUMNS if name != "label"]
    assert [list(r.values()) for r in found] == [
        ["0", "GROUP_MEMBERS_DIFFER", "2", "1", "1", "0", "{}"],
        ["1", "CLUSTER_SPLIT", "1", "2", "0", "0", '{"members":2}'],
    ]
