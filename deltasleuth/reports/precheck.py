"""Table pre-check reports: ``precheck.csv``, a row a pair;
``precheck-columns.csv``, a row a pair, compared column and figure; and ``precheck.pdf``, the pairs worst
first with a section for each of the first ``SECTIONS`` that show a difference.

Every export is headed by what a pre-check is not (``precheck.NOT``), and one made in an unlicensed
evaluation by that label under it: a CSV's first record is that sentence, its second the label, then come
its header and its rows. They hold what the store holds of a pre-check: counts, length ranges, verdicts,
closed codes, relation and column names, and the sums that are shown; a withheld figure's cells are empty.
A catalogue's names are untrusted strings: every cell goes through ``reports.csv.escape_cell``. No text
of a pair is built here (``core.precheck``'s lines), and no run's report carries a pre-check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from deltasleuth import prechecks
from deltasleuth.context import Context
from deltasleuth.core import precheck
from deltasleuth.reports.csv import escape_cell, render_csv
from deltasleuth.reports.render import Document, Section, render_pdf
from deltasleuth.store import repo

NAMES = ("precheck.csv", "precheck-columns.csv", "precheck.pdf")
SECTIONS = 100  # pairs with a difference precheck.pdf gives a section each
CLAIM = "observed; not parity"
PAIRS = (
    "precheck_id", "n", "label", "spec_id", "target_a", "relation_a", "target_b", "relation_b", "status",
    "verdict", "differing", "rows_a", "rows_b", "text", "claim",
)  # fmt: skip
COLUMNS = (
    "precheck_id", "n", "label", "column", "class_a", "class_b", "figure", "a", "b", "verdict", "reason",
)  # fmt: skip
NOT = (  # what the PDF's last section says: what a pre-check is not, and its limits
    "Agreeing figures are not parity: offsetting differences cancel in a sum, and swapped values change"
    " nothing that is counted.",
    "Differing figures are not a defect: a spec's bridges and rules are not applied, and the two sides"
    " are read seconds apart.",
    "No minimum, maximum or distinct value is stored, shown or exported: same says the two were equal,"
    " never what they were.",
    "A check that was not made reads not checked or not compared with its reason, never as agreeing.",
    "It never says that a run will succeed, and its totals count pairs by verdict, worst first.",
    "Rows and values are as the account reads them.",
    "It is observed, not evidence, readiness or a sign-off, and no evidence pack contains it.",
)


async def loaded(ctx: Context, precheck_id: str) -> tuple[repo.Row, list[repo.Row]]:
    """The pre-check and its pairs worst first, each with its ``result``; refused when there is
    none."""
    async with ctx.pool.connection() as conn:
        row = await prechecks.stored(ctx, conn, precheck_id)
        pairs = await repo.pages(repo.precheck_pairs, conn, precheck_id, results=True)
    return row, pairs


def _headed(row: Mapping[str, Any], header: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    """A CSV under its head: what a pre-check is not, then the label of an unlicensed evaluation."""
    head = [precheck.NOT] + [precheck.EVALUATION] * bool(row["evaluation"])
    return "".join(escape_cell(line) + "\r\n" for line in head) + render_csv(header, rows)


def pairs_csv(row: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]]) -> str:
    """``precheck.csv``: a row a pair, worst first, each with its verdict's line and its claim."""
    return _headed(row, PAIRS, [dict(found) | {"text": found["line"], "claim": CLAIM} for found in pairs])


def _figures(read: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A compared column's rows: one a figure with the two values the store kept of it (a length as its
    shortest and its longest), its verdict and its reason; last, why no other figure is compared."""
    rows: list[dict[str, Any]] = []
    for figure in (listed for listed in precheck.ORDER if listed in read["figures"]):
        judged = read["figures"][figure]
        told = {"verdict": judged["verdict"], "reason": judged.get("reason")}
        if figure == "length":  # [shortest, longest] a side; None: the side holds no value
            spans = [judged.get(side) or [None, None] for side in "ab"]
            rows += [
                told | {"figure": name, "a": spans[0][i], "b": spans[1][i]}
                for i, name in enumerate(("shortest", "longest"))
            ]
        else:
            rows.append(told | {"figure": figure, "a": judged.get("a"), "b": judged.get("b")})
    if read.get("reason"):
        rows.append({"verdict": "not compared", "reason": read["reason"]})
    return rows


def columns_csv(row: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]]) -> str:
    """``precheck-columns.csv``: a row a pair, compared column and figure; a minimum, a maximum, a sum that
    is not shown and a count or length a filter withholds have no value, only their verdict."""
    rows = []
    for found in pairs:
        about = {name: found[name] for name in ("precheck_id", "n", "label")}
        for column, read in sorted(((found["result"] or {}).get("columns") or {}).items()):
            classes = dict(zip(("class_a", "class_b"), read["classes"], strict=True))
            rows += [about | {"column": column} | classes | figure for figure in _figures(read)]
    return _headed(row, COLUMNS, rows)


def _when(at: datetime | None) -> str:
    return "-" if at is None else f"{at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def _section(found: Mapping[str, Any]) -> Section:
    """A pair's section: its lines worst first, then each compared column's figures as lines."""
    result = found["result"]
    rows = [
        (column, " / ".join(str(cls) for cls in read["classes"]), "\n".join(precheck.column_lines(read)))
        for column, read in sorted(result["columns"].items())
    ]
    title = f"Pair {found['n']}, {found['label']}: {precheck.worded(found['verdict'])}"
    return Section(title, result["lines"], ("Column", "Classes", "Figures") if rows else (), rows)


def document(row: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]]) -> Document:
    """``precheck.pdf``'s document: what a pre-check is not, who made it and when, its totals, its pairs
    worst first, a section for each of the first ``SECTIONS`` pairs that show a difference, and what the
    report is not."""
    item = prechecks.item(row)
    ended = "cancelled" if row["cancelled_at"] else "finished" if row["finished_at"] else "not finished"
    about = [
        f"Table pre-check {row['precheck_id']} of {row['source']} at level {row['level']}, made by"
        f" {row['created_by_name'] or row['created_by']} {_when(row['created_at'])}; {ended}"
        f" {_when(row['cancelled_at'] or row['finished_at'])}; this report was made {_when(row['now'])}.",
        item["totals"],
    ]
    differing = [
        found for found in pairs if found["result"] is not None and found["verdict"] != precheck.VERDICTS[-1]
    ]
    listed = [
        (found["n"], found["label"], found["verdict"] and precheck.worded(found["verdict"]),
         found["differing"], found["rows_a"], found["rows_b"], found["line"])
        for found in pairs
    ]  # fmt: skip
    return Document(
        "Table pre-check (observed)",
        precheck.NOT,
        [
            Section("The pre-check", about),
            Section(
                "Pairs, worst first", (),
                ("Pair", "Label", "Verdict", "Differing figures", "Rows a", "Rows b", "What was observed"),
                listed,
            ),
            *map(_section, differing[:SECTIONS]),
            Section("What this report is not", NOT),
        ],
        precheck.EVALUATION if row["evaluation"] else None,
    )  # fmt: skip


def render(name: str, row: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]]) -> tuple[str, bytes]:
    """The report ``name`` of ``NAMES``: its media type and its bytes."""
    if name == "precheck.pdf":
        return "application/pdf", render_pdf(document(row, pairs))
    text = pairs_csv(row, pairs) if name == "precheck.csv" else columns_csv(row, pairs)
    return "text/csv", text.encode()


async def download(ctx: Context, precheck_id: str, name: str) -> tuple[str, bytes, str]:
    """A pre-check's report by its published name: its media type, its bytes and the name it downloads
    under, which says so when the pre-check was made in an unlicensed evaluation."""
    row, pairs = await loaded(ctx, precheck_id)
    return *render(name, row, pairs), prechecks.file_name(row, name)
