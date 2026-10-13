"""Table pre-check reports (docs/mvp/PRECHECKS.md §11): each export headed by what a pre-check is not, a
catalogue's hostile names escaped in every CSV cell, the cells of a withheld figure empty, and the PDF's
pairs worst first with a section for each of the first that show a difference."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from deltasleuth.core import precheck
from deltasleuth.core.precheck import Pair
from deltasleuth.core.types import SecurityPolicy
from deltasleuth.reports import precheck as reports
from deltasleuth.reports.render import render_pdf
from tests.unit.core.test_precheck import SPEC, col, judged, read, relation

HOSTILE = "=cmd|' /C calc'!A0"  # a column a spreadsheet would run
AT = datetime(2026, 10, 7, 10, 2, tzinfo=UTC)
N1 = re.compile(r"\b(match|matches|matched|identical|passed|verified|validated|reconciled)\b|[✓✔%]", re.I)


def made(*pairs: dict[str, Any], **members: Any) -> dict[str, Any]:
    """A pre-check as ``repo.get_precheck`` gives it, of ``pairs``."""
    verdicts: dict[str, int] = {}
    for pair in pairs:
        if pair["verdict"] is not None:
            verdicts[pair["verdict"]] = verdicts.get(pair["verdict"], 0) + 1
    return {
        "precheck_id": "PC-" + "0" * 25 + "1", "source": "specs", "level": "columns", "pairs": len(pairs),
        "skipped": 0, "evaluation": False, "created_by": "user:U-1", "created_by_name": "jdoe",
        "created_at": AT, "cancelled_by": None, "cancelled_by_name": None, "cancelled_at": None,
        "finished_at": AT, "settled_at": AT, "now": AT, "verdicts": verdicts,
        "progress": {"done": sum(pair["status"] == "done" for pair in pairs)},
    } | members  # fmt: skip


def stored(n: int, label: str, found: precheck.Judged | None = None, **members: Any) -> dict[str, Any]:
    """Pair ``n`` as the store keeps it once judged (None: still pending)."""
    pair = {
        "precheck_id": "PC-" + "0" * 25 + "1", "n": n, "spec_id": 12, "label": label, "target_a": "db-a",
        "relation_a": "customers", "target_b": "db-b", "relation_b": "customers", "status": "pending",
        "verdict": None, "differing": None, "rows_a": None, "rows_b": None, "line": None, "result": None,
    }  # fmt: skip
    if found is not None:
        keys = found.result["keys"]
        pair |= {"status": "done", "verdict": found.verdict, "differing": found.differing}
        pair |= {"rows_a": keys["a"]["rows"], "rows_b": keys["b"]["rows"]}
        pair |= {"line": found.line, "result": found.result}
    return pair | members


def differing(pair: Pair = SPEC) -> precheck.Judged:
    """A pair whose hostile column differs in its NULLs and whose balance in its sum and its minimum."""
    columns = (
        col(HOSTILE, "text"),
        col("balance", "numeric(12,2)", "decimal", size=("digits", 12, 2)),
        col("note", "text"),
    )
    hostile = {"nulls": "1", "empty": "0", "shortest": "2", "longest": "9"}
    balance = {"nulls": "0", "sum": "60.00", "min": "10.00", "max": "30.00"}
    note = {"nulls": "0", "empty": "3", "shortest": None, "longest": None}  # no value: no length
    a = read(**{HOSTILE.lower(): hostile, "balance": balance, "note": note})
    b = read(
        **{HOSTILE.lower(): hostile | {"nulls": "4"}, "note": note},
        balance=balance | {"sum": "47.50", "min": "9.00"},
    )
    return judged(relation(*columns), relation(*columns), (a, b), pair=pair)


def table(text: str) -> list[list[str]]:
    """A CSV's records after its head: its header, then its rows."""
    assert text.startswith(f'"{precheck.NOT}"\r\n')
    return list(csv.reader(io.StringIO(text.split("\r\n", 1)[1], newline="")))


def test_every_csv_cell_is_escaped_and_each_export_is_headed() -> None:
    found = differing()
    pairs = [stored(1, "+SUM(1)", found), stored(2, "loans")]
    row = made(*pairs)
    listed, columns = reports.pairs_csv(row, pairs), reports.columns_csv(row, pairs)
    canonical = HOSTILE.lower()  # a compared column goes by its lower-cased name
    assert f'"\'{canonical}",' in columns and f",{canonical}," not in columns  # never a formula
    assert '"\'+SUM(1)"' in listed and '"\'+SUM(1)"' in columns
    header, first, second = table(listed)
    assert tuple(header) == reports.PAIRS and len(first) == len(second) == len(header)
    assert dict(zip(header, first, strict=True)) == {
        "precheck_id": row["precheck_id"], "n": "1", "label": "'+SUM(1)", "spec_id": "12", "target_a": "db-a",
        "relation_a": "customers", "target_b": "db-b", "relation_b": "customers", "status": "done",
        "verdict": "values_differ", "differing": "3", "rows_a": "10", "rows_b": "10",
        "text": f"2 of 3 compared columns differ in a figure: {HOSTILE.lower()} (nulls), balance (sum, min)",
        "claim": "observed; not parity",
    }  # fmt: skip
    assert (second[8], second[9], second[13], second[14]) == ("pending", "", "", "observed; not parity")
    evaluation = reports.pairs_csv(row | {"evaluation": True}, pairs)
    assert evaluation.startswith(f'"{precheck.NOT}"\r\nUnlicensed evaluation\r\nprecheck_id,n,label,')
    assert reports.render("precheck.csv", row, pairs) == ("text/csv", listed.encode())
    assert reports.render("precheck-columns.csv", row, pairs) == ("text/csv", columns.encode())


def test_withheld_cells_are_empty() -> None:
    """A minimum and a maximum are verdicts only; so is a sum the spec's policy protects. A sum that is
    shown, a count and a length range are numbers."""
    shown = stored(1, "customers", differing())
    protected = Pair(12, "customer-profile 1.4.0", False, SecurityPolicy(), True, False, ("db-a", "db-b"))
    withheld = stored(2, "accounts", differing(protected))
    pairs = [shown, withheld]
    header, *rows = table(reports.columns_csv(made(*pairs), pairs))
    assert tuple(header) == reports.COLUMNS
    cells = {(row[1], row[3], row[6]): row[7:] for row in rows}
    assert cells["1", "balance", "sum"] == ["60.00", "47.50", "differs", "side b is 12.50 lower"]
    assert cells["2", "balance", "sum"] == [
        "", "", "differs", "figures withheld: $.balance is protected by the spec's security policy",
    ]  # fmt: skip
    for n in "12":
        assert cells[n, "balance", "min"] == ["", "", "differs", ""]
        assert cells[n, "balance", "max"] == ["", "", "same", ""]
        assert cells[n, "balance", "nulls"] == ["0", "0", "same", ""]
        assert cells[n, "'" + HOSTILE.lower(), "nulls"] == ["1", "4", "differs", ""]
        assert cells[n, "'" + HOSTILE.lower(), "shortest"] == ["2", "2", "same", ""]
        assert cells[n, "'" + HOSTILE.lower(), "longest"] == ["9", "9", "same", ""]
        assert cells[n, "note", "longest"] == ["", "", "same", ""]  # a side without a value has no length
    assert [row[6] for row in rows if row[1] == "1" and row[3] == "balance"] == ["nulls", "sum", "min", "max"]
    assert not [cell for row in rows for cell in row if cell in ("10.00", "30.00", "9.00")]  # never a bound


def test_a_column_s_reason_is_a_row_of_its_own() -> None:
    columns = (col("geom", "SDO_GEOMETRY", ""), col("rate", "double precision", "float"))
    sides = (read(rate={"nulls": "0"}), read(rate={"nulls": "0"}))
    found = judged(relation(*columns), relation(*columns), sides)
    pairs = [stored(1, "customers", found)]
    _, *rows = table(reports.columns_csv(made(*pairs), pairs))
    assert [row[3:] for row in rows] == [
        ["geom", "", "", "", "", "", "not compared",
         "geom (SDO_GEOMETRY) cannot be compared: compare a view that casts it"],
        ["rate", "float", "float", "nulls", "0", "0", "same", ""],
        ["rate", "float", "float", "", "", "", "not compared",
         "a float sum depends on the order rows are added in"],
    ]  # fmt: skip


def test_the_pdf_lists_the_pairs_worst_first_and_gives_the_differing_ones_a_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same = read(email={"nulls": "0", "empty": "0", "shortest": "1", "longest": "5"})
    agreeing = judged(relation(col("email", "text")), relation(col("email", "text")), (same, same))
    pairs = [
        stored(3, "customers", differing()),
        stored(1, "accounts", differing()),
        stored(2, "cards", agreeing),
        stored(4, "loans"),
        stored(5, "users", status="cancelled", line=precheck.CANCELLED),
    ]
    row = made(*pairs, cancelled_at=AT, cancelled_by="user:U-2", cancelled_by_name="asmith")
    row["progress"] = {"done": 3, "pending": 1, "cancelled": 1}
    document = reports.document(row, pairs)
    assert (document.title, document.meta, document.warning) == (
        "Table pre-check (observed)", precheck.NOT, None,
    )  # fmt: skip
    titles = [section.title for section in document.sections]
    assert titles == [
        "The pre-check", "Pairs, worst first", "Pair 3, customers: values differ",
        "Pair 1, accounts: values differ", "What this report is not",
    ]  # fmt: skip
    assert list(document.sections[0].lines) == [
        f"Table pre-check {row['precheck_id']} of specs at level columns, made by jdoe 2026-10-07 10:02 UTC;"
        " cancelled 2026-10-07 10:02 UTC; this report was made 2026-10-07 10:02 UTC.",
        "5 pairs: 2 with differing figures, 1 with no difference observed (not parity); 1 pending, 1"
        " cancelled",
    ]
    listed = document.sections[1]
    assert tuple(listed.header)[:3] == ("Pair", "Label", "Verdict") and [r[:4] for r in listed.rows] == [
        (3, "customers", "values differ", 3),
        (1, "accounts", "values differ", 3),
        (2, "cards", "no difference observed in the checks made (not parity)", 0),
        (4, "loans", None, None),
        (5, "users", None, None),
    ]
    assert listed.rows[4][6] == precheck.CANCELLED
    section = document.sections[2]
    assert list(section.lines) == pairs[0]["result"]["lines"] and tuple(section.header) == (
        "Column", "Classes", "Figures",
    )  # fmt: skip
    assert section.rows[1] == (
        "balance",
        "decimal / decimal",
        "nulls: side a 0, side b 0 (same)\nsum: side a 60.00, side b 47.50 (side b is 12.50 lower)\n"
        "min: differs; max: same (values are never shown)",
    )
    assert tuple(document.sections[-1].lines) == reports.NOT
    assert not [line for line in reports.NOT if N1.search(line)]
    assert reports.render("precheck.pdf", row, pairs)[0] == "application/pdf"
    assert render_pdf(document).startswith(b"%PDF")
    monkeypatch.setattr(reports, "SECTIONS", 1)  # the first that differ, never all of a large pre-check
    assert [section.title for section in reports.document(row, pairs).sections][2:] == [
        "Pair 3, customers: values differ", "What this report is not",
    ]  # fmt: skip
    labelled = reports.document(row | {"evaluation": True}, pairs)
    assert labelled.warning == "Unlicensed evaluation"
