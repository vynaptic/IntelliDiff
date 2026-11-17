"""Reviewer CSV limits apply before short rows are expanded, without changing report readers."""

from __future__ import annotations

from pathlib import Path

import pytest

from deltasleuth.reports import adjudicate
from deltasleuth.reports.adjudicate import AdjudicationError, _read_sheet, has_reviewer_input
from deltasleuth.reports.csv import parse_csv, render_csv


def test_a_wide_short_row_upload_is_refused_before_expansion() -> None:
    header = [*adjudicate.SHEET_COLUMNS, *(f"extra{i}" for i in range(300))]
    data = (",".join(header) + "\n" + "K\n" * 1000).encode()
    assert len(data) < 10_000
    with pytest.raises(AdjudicationError, match="more than 256 columns"):
        _read_sheet(data, "the sheet")
    # Report readers can still read exports wider than a reviewer sheet.
    assert len(parse_csv((",".join(header) + "\nK\n").encode())[1][0]) == len(header)


@pytest.mark.parametrize(
    ("header", "expected"),
    [("key,key", "names column\\(s\\) key more than once"), ("key", "lacks column")],
)
def test_sheet_headers_are_checked_before_the_first_row(header: str, expected: str) -> None:
    with pytest.raises(AdjudicationError, match=expected):
        _read_sheet((header + "\n1,2,3\n").encode(), "the sheet")


def test_sheet_cell_budget_counts_padding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adjudicate, "_MAX_SHEET_CELLS", 24, raising=False)
    data = (",".join(adjudicate.SHEET_COLUMNS) + "\nK\n" * 3).encode()
    with pytest.raises(AdjudicationError, match="more than 24 cells"):
        _read_sheet(data, "the sheet")


def test_sheet_row_budget_ignores_blank_lines_and_keeps_spreadsheet_edits() -> None:
    header = [*adjudicate.SHEET_COLUMNS, "", ""]
    data = ("\ufeff" + render_csv(header, [{"key": "=K", "reviewer_notes": "quoted\nnotes"}])
            + "\r\n , \r\n" + "K2\r\n").encode()
    rows = _read_sheet(data, "the sheet", max_rows=2)
    assert len(rows) == 2 and rows[0]["key"] == "=K" and rows[0]["reviewer_notes"] == "quoted\nnotes"
    assert rows[1]["reviewer_verdict"] == ""
    with pytest.raises(AdjudicationError, match="more than 2 rows"):
        _read_sheet(data + b"K3\r\n", "the sheet", max_rows=2)


def test_a_wide_existing_sheet_is_preserved_as_reviewed(tmp_path: Path) -> None:
    sheet = tmp_path / "adjudication.csv"
    sheet.write_text(",".join(f"extra{i}" for i in range(300)) + "\nK\n")
    assert has_reviewer_input(sheet)
