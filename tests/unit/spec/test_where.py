"""A table side's row filter: the predicate language, its canonical text, and
the filters it refuses, each naming where it went wrong."""

from __future__ import annotations

import pytest

from deltasleuth.core.spec import where


@pytest.mark.parametrize(
    "text",
    [
        "amount = 1",
        "amount <> -1.5e3",
        "amount != .5",
        "amount < 10",
        "amount <= 10.",
        "amount > 0",
        "amount >= 0",
        "closed IS NULL",
        "closed is not null",
        "status IN ('active', 'it''s')",
        "status NOT IN ('closed')",
        "amount BETWEEN 1 AND 2",
        "amount NOT BETWEEN 1 AND 2",
        "name LIKE 'A%'",
        "name NOT ILIKE '%x'",
        "active",
        "active = TRUE AND deleted = false",
        "NOT active",
        "a = 1 OR b = 2 AND NOT (c = 3 OR d = 4)",
        "a AND (b AND c) OR (d OR e)",
        "NOT NOT (a OR b) AND c",
        '"Weird ""col""" = \'x\'',
    ],
)
def test_each_construct_reads_back_from_its_canonical_text(text: str) -> None:
    node = where.parse(text)
    assert where.parse(where.text(node)) == node


@pytest.mark.parametrize(
    "text", ["NOT " * 100 + "a", "(" * 100 + "a" + ")" * 100, "NOT (" * 50 + "a OR b" + ")" * 50]
)
def test_the_canonical_text_nests_no_deeper_than_the_filter(text: str) -> None:
    assert where.parse(where.text(where.parse(text))) == where.parse(text)  # a stored plan re-parses


def test_the_canonical_text_quotes_columns_and_upper_cases_keywords() -> None:
    node = where.parse("status in ('a') and not flag or amount between 1 and 2")
    assert where.text(node) == (""""status" IN ( 'a' ) AND NOT "flag" OR "amount" BETWEEN 1 AND 2""")
    assert where.columns(node) == {"status", "flag", "amount"}


def test_a_literal_is_rendered_with_the_column_it_is_compared_with() -> None:
    node = where.parse("st in ('a', 'b') and not (amount between 1 and 2 or name like 'x%') or flag = true")
    seen: list[tuple[str, str]] = []

    def literal(value: where.Value, column: str) -> str:
        seen.append((column, value.text))
        return "?"

    written = where.render(node, str.upper, literal, " ".join)
    assert written == "ST IN ( ? , ? ) AND NOT ( AMOUNT BETWEEN ? AND ? OR NAME LIKE ? ) OR FLAG = ?"
    assert seen == [
        ("st", "a"), ("st", "b"), ("amount", "1"), ("amount", "2"), ("name", "x%"), ("flag", "true"),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("lower(name) = 'x'", r"unexpected '\(' at position 5"),
        ("a = b", r"expected a literal \(string, number, TRUE or FALSE\), found 'b' at position 4"),
        ("a =", r"expected a literal \(string, number, TRUE or FALSE\), found the end"),
        ("a = 1 b", "unexpected 'b' at position 6"),
        ("a = 'x", "unexpected text at position 4"),
        ("a NOT = 1", "NOT after a column needs IN, BETWEEN, LIKE or ILIKE.*at position 6"),
        ("a LIKE 5", "LIKE needs a string pattern.*at position 7"),
        ("a ILIKE 'x\\'", r"the ILIKE pattern at position 8 ends in an escape \(\\\)"),
        ("(" * 101 + "a" + ")" * 101, "the filter nests more than 100 levels deep"),
        ("NOT " * 101 + "a", "the filter nests more than 100 levels deep"),
    ],
    ids=[
        "function call",
        "column to column",
        "missing literal",
        "trailing text",
        "open string",
        "NOT",
        "LIKE",
        "LIKE escape at the end",
        "parentheses too deep",
        "NOT too deep",
    ],
)
def test_a_filter_outside_the_language_is_refused_where_it_goes_wrong(text: str, message: str) -> None:
    with pytest.raises(where.WhereError, match=f"^{message}$"):
        where.parse(text)
