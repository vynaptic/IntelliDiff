"""A table column's declared canonical type (typeMap, docs/mvp/TABLES.md §10): what each type accepts,
what it produces, and what it refuses, naming the type and the column, never the value."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from deltasleuth.core.canonical.convert import ConversionError, convert, convert_row

TYPES = ("string", "integer", "decimal", "boolean", "date", "timestamp", "json", "bytes")
YN = {"trueValues": ["Y", 1], "falseValues": ["N", 0]}


@pytest.mark.parametrize(
    ("value", "spec", "expected"),
    [
        (Decimal("1.50"), {"as": "string"}, "1.5"),
        (True, {"as": "string"}, "true"),
        ("", {"as": "string"}, ""),
        ("", {"as": "string", "emptyAsNull": False}, ""),
        (" ", {"as": "string", "emptyAsNull": True}, " "),  # only the empty string
        ({"b": 1, "a": [False]}, {"as": "string"}, '{"a": [false], "b": 1}'),
        (" 12 ", {"as": "integer"}, Decimal(12)),
        (Decimal("12.0"), {"as": "integer"}, Decimal(12)),
        ("2.5", {"as": "decimal", "scale": 0}, Decimal(2)),  # half to even
        (Decimal("3.5"), {"as": "decimal", "scale": 0}, Decimal(4)),
        (Decimal("1.005"), {"as": "decimal", "scale": 2}, Decimal("1.00")),
        (Decimal("7.50"), {"as": "decimal"}, Decimal("7.50")),
        pytest.param(  # more digits than Python's default decimal context holds
            Decimal("1234567890123456789012.1234567895"),
            {"as": "decimal", "scale": 9},
            Decimal("1234567890123456789012.123456790"),
            id="numeric(38, 9)",
        ),
        (" Yes ", {"as": "boolean"}, True),
        ("OFF", {"as": "boolean"}, False),
        (Decimal(0), {"as": "boolean"}, False),
        ("Y", {"as": "boolean", **YN}, True),
        (Decimal(0), {"as": "boolean", **YN}, False),  # a number is compared as text
        (False, {"as": "boolean", **YN}, False),
        ("2024-05-01T23:30:00+00:00", {"as": "date", "naiveTimezone": "Europe/Paris"}, "2024-05-02"),
        ("2024-05-01 23:30:00", {"as": "date", "naiveTimezone": "Europe/Paris"}, "2024-05-01"),
        ("2024-05-01 12:00:00", {"as": "timestamp"}, "2024-05-01T12:00:00.000000Z"),
        (
            "2024-05-01T12:00:00",
            {"as": "timestamp", "naiveTimezone": "Europe/Paris"},
            "2024-05-01T10:00:00.000000Z",
        ),
        ("2024-05-01T12:00:00.5+02:00", {"as": "timestamp"}, "2024-05-01T10:00:00.500000Z"),
        (
            "2024-05-01T12:00:00Z",
            {"as": "timestamp", "naiveTimezone": "Asia/Tokyo"},
            "2024-05-01T12:00:00.000000Z",
        ),
        ("2024-05-01", {"as": "timestamp", "naiveTimezone": "Europe/London"}, "2024-04-30T23:00:00.000000Z"),
        ("2024-05-01 10:00:00.987654", {"as": "timestamp", "precision": 3}, "2024-05-01T10:00:00.987Z"),
        ("2024-05-01 10:00:00.987654", {"as": "timestamp", "precision": 0}, "2024-05-01T10:00:00Z"),
        ('{"a": 1.10, "b": [null]}', {"as": "json"}, {"a": Decimal("1.10"), "b": [None]}),
        ([Decimal(1)], {"as": "json"}, [Decimal(1)]),
        ("\\xDEad", {"as": "bytes"}, "\\xdead"),
        ("\\x", {"as": "bytes"}, "\\x"),
    ],
)
def test_a_value_converts_to_the_canonical_form_of_its_type(
    value: Any, spec: dict[str, Any], expected: Any
) -> None:
    converted = convert(value, spec)
    assert (converted, type(converted)) == (expected, type(expected))


@pytest.mark.parametrize("kind", TYPES)
def test_null_stays_null_for_every_type(kind: str) -> None:
    assert convert(None, {"as": kind}) is None


@pytest.mark.parametrize(
    ("value", "spec"),
    [
        ("12.5", {"as": "integer"}),
        (True, {"as": "integer"}),
        ("twelve", {"as": "integer"}),
        ("NaN", {"as": "decimal"}),
        ([1], {"as": "decimal"}),
        ("maybe", {"as": "boolean"}),
        ("y", {"as": "boolean", **YN}),  # the lists are compared exactly
        ("true", {"as": "boolean", **YN}),  # the lists replace the defaults
        ("2024-13-01", {"as": "date"}),
        (Decimal(20240501), {"as": "date"}),
        ("yesterday", {"as": "timestamp"}),
        ('{"a": 1, "a": 2}', {"as": "json"}),  # a repeated member has no meaning
        ("{bad", {"as": "json"}),
        ("\\xABC", {"as": "bytes"}),
        ("DEAD", {"as": "bytes"}),
    ],
)
def test_a_value_its_type_cannot_hold_is_refused_naming_the_type_only(
    value: Any, spec: dict[str, Any]
) -> None:
    with pytest.raises(ConversionError) as exc:
        convert(value, spec)
    assert str(exc.value) == f"does not convert to {spec['as']}"


def test_an_empty_string_declared_as_null_reads_as_one() -> None:
    # The other side's database stores '' as NULL (docs/mvp/ENGINES.md §9): in digests and per-key rows.
    spec = {"as": "string", "emptyAsNull": True}
    assert convert("", spec) is None and convert(None, spec) is None
    assert convert_row({"note": "", "name": ""}, {"note": spec}) == {"note": None, "name": ""}


def test_every_precision_the_schema_allows_converts() -> None:
    for precision in range(10):  # typeSpec.precision: 0 to 9
        converted = convert("2024-05-01 10:00:00.123456", {"as": "timestamp", "precision": precision})
        assert isinstance(converted, str) and converted.startswith("2024-05-01T10:00:00"), precision


def test_a_row_converts_its_mapped_columns_and_names_the_one_that_fails() -> None:
    row = {"flag": "Y", "n": " 7 ", "other": "Y"}
    assert convert_row(row, {"flag": {"as": "boolean", **YN}, "n": {"as": "integer"}}) == {
        "flag": True,
        "n": Decimal(7),
        "other": "Y",
    }
    with pytest.raises(ConversionError) as exc:
        convert_row(row, {"n": {"as": "integer"}, "other": {"as": "date"}})
    assert str(exc.value) == "column 'other' does not convert to date"


@pytest.mark.parametrize(
    ("value", "spec", "expected"),
    [
        ("31/12/2024", {"as": "date", "format": "%d/%m/%Y"}, "2024-12-31"),
        ("2024.02.29", {"as": "date", "format": "%Y.%m.%d"}, "2024-02-29"),
        (
            "29/02/2024 13:14:15",
            {"as": "timestamp", "format": "%d/%m/%Y %H:%M:%S"},
            "2024-02-29T13:14:15.000000Z",
        ),
        (
            "2024-02-29T13:14:15.5",
            {"as": "timestamp", "format": "%Y-%m-%dT%H:%M:%S.%f"},
            "2024-02-29T13:14:15.500000Z",
        ),
        (
            "2024-02-29 13:14:15+0200",
            {"as": "timestamp", "format": "%Y-%m-%d %H:%M:%S%z"},
            "2024-02-29T11:14:15.000000Z",
        ),
        (
            "01/02/2024",
            {"as": "timestamp", "format": "%d/%m/%Y", "naiveTimezone": "Europe/London"},
            "2024-02-01T00:00:00.000000Z",
        ),
        ("1,50", {"as": "decimal", "decimalSeparator": ","}, Decimal("1.50")),
        ("-0,5", {"as": "decimal", "decimalSeparator": ",", "scale": 2}, Decimal("-0.50")),
        ("1.50", {"as": "decimal", "decimalSeparator": "."}, Decimal("1.50")),
        ("42,0", {"as": "integer", "decimalSeparator": ","}, Decimal(42)),
        ("00123", {"as": "integer"}, Decimal(123)),  # a text's leading zeros: the number declared
    ],
)
def test_a_text_reads_by_its_declared_format_or_separator(
    value: str, spec: dict[str, Any], expected: Any
) -> None:
    """docs/mvp/FILES.md §5: format parsed whole by strptime; decimalSeparator, the text's own."""
    assert convert(value, spec) == expected


@pytest.mark.parametrize(
    ("value", "spec"),
    [
        ("2024-12-31", {"as": "date", "format": "%d/%m/%Y"}),  # another form
        ("31/12/2024 extra", {"as": "date", "format": "%d/%m/%Y"}),  # parsed whole
        ("1.000,50", {"as": "decimal", "decimalSeparator": ","}),  # no group separator is read
        ("1,50", {"as": "decimal", "decimalSeparator": "."}),  # the other separator
        ("1,50", {"as": "decimal"}),
        # Python's Decimal reads an underscore between digits, and the digits of every script: neither is the
        # number in the export, so neither converts (1_000 read as 1000 matched a side holding 1000).
        ("1_000", {"as": "decimal"}),
        ("1__0", {"as": "decimal"}),
        ("_1", {"as": "integer"}),
        ("1_", {"as": "integer"}),
        ("1_000,50", {"as": "decimal", "decimalSeparator": ","}),
        ("\uff11\uff12", {"as": "decimal"}),  # full-width 12
        ("\u0661\u0662", {"as": "integer"}),  # Arabic-Indic 12
        ("1 000", {"as": "decimal"}),
        ("1'000", {"as": "integer"}),
        ("Infinity", {"as": "decimal"}),
        ("NaN", {"as": "decimal"}),
    ],
)
def test_a_text_of_another_form_does_not_convert(value: str, spec: dict[str, Any]) -> None:
    with pytest.raises(ConversionError) as exc:
        convert(value, spec)
    assert value not in str(exc.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("+1.5", "1.5"), (" -12 ", "-12"), ("1e3", "1E+3"), (".5", "0.5"), ("5.", "5"), ("1.5E-2", "0.015")],
)
def test_a_number_s_text_is_its_ascii_form(value: str, expected: str) -> None:
    """A sign, digits 0 to 9, one point and an exponent, as every engine writes a number."""
    assert convert(value, {"as": "decimal"}) == Decimal(expected)
