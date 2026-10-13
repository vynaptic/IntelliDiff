"""Normalisers: one row per behaviour (totality and idempotence are property-tested)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from deltasleuth.core.canonical.errors import ExpressionError
from deltasleuth.core.canonical.normalisers import (
    ADDRESS_ABBREVIATIONS,
    NORMALISER_NAMES,
    apply_chain,
    read_decimal,
)
from deltasleuth.core.types import Normalizer as N

D = Decimal
GB, US = {"defaultRegion": "GB"}, {"defaultRegion": "US"}


def test_names_match_the_schema_enum_and_object_forms(repo_root: Path) -> None:
    schema = json.loads((repo_root / "deltasleuth/core/spec/spec.schema.json").read_text())
    forms = schema["$defs"]["normalizer"]["oneOf"]
    assert set(forms[0]["enum"]) | {next(iter(f["required"])) for f in forms[1:]} == NORMALISER_NAMES


@pytest.mark.parametrize(
    ("name", "params", "value", "expected"),
    [
        ("trim", {}, "  a b \t\n", "a b"),
        ("collapse_ws", {}, " a \t\n b  c ", " a b c "),
        ("nfkc", {}, "ﬁ①Ａ", "fi1A"),
        ("casefold", {}, "Straße", "strasse"),
        ("upper", {}, "straße", "STRASSE"),
        ("lower", {}, "ÀBC", "àbc"),
        ("email_basic", {}, "  John.Doe@Example.COM ", "john.doe@example.com"),
        ("email_basic", {}, " two@@signs ", " two@@signs "),
        ("digits_only", {}, "+44 (0)20-7946 \u0663\u0664", "440207946"),  # ASCII digits only
        ("null_if_empty", {}, "", None),
        ("null_if_empty", {}, [], None),
        ("null_if_empty", {}, {}, None),
        ("null_if_empty", {}, " ", " "),
        ("null_if_empty", {}, D(0), D(0)),
        ("address_std", {}, "12  North st. O'Connell Road,  Apt #4", "12 N ST OCONNELL RD APT #4"),
        ("address_std", {}, "\uff11\uff12 \uff2dain Street", "12 MAIN ST"),  # fullwidth forms
        ("address_std", {}, "Flat 3/2 Smith-Jones Lane", "FLAT 3/2 SMITH-JONES LN"),
        ("address_std", {}, "O\u2019Connell Street", "OCONNELL ST"),
        ("e164", GB, "(020) 7946-0018", "+442079460018"),
        ("e164", GB, "0044 20 7946 0018", "+442079460018"),
        ("e164", GB, "+44 (0) 20 7946 0018", "+442079460018"),
        ("e164", GB, "\uff10\uff12\uff10 7946 0018", "+442079460018"),  # Unicode digits
        ("e164", US, "(202) 555-0143", "+12025550143"),
        # An extension is content: two lines of one switchboard are different numbers.
        ("e164", GB, "+44 20 7946 0018 ext. 201", "+442079460018;ext=201"),
        ("e164", GB, "020 7946 0018 x201", "+442079460018;ext=201"),
        ("e164", GB, "020 7946 0018 #201", "+442079460018;ext=201"),
        ("e164", GB, "+442079460018;ext=201", "+442079460018;ext=201"),
        ("e164", GB, "+99 999", "+99 999"),
        # Letters mean an id or serial, not a phone number: never merge two of them into one.
        ("e164", GB, "SN-2079460000-B", "SN-2079460000-B"),
        ("e164", GB, "tel 07700900123", "tel 07700900123"),
        ("tax_id", {"country": "GB"}, "GB 980 7806 84", "980780684"),
        ("tax_id", {"country": "DE"}, "DE 136,695 976", "136695976"),
        ("tax_id", {"country": "US"}, "12-3456789", "123456789"),  # the second (TIN) module
        ("tax_id", {"country": "GB"}, "GB 980 7806 85", "GB 980 7806 85"),  # bad check digits
        ("tax_id", {"country": "ZZ"}, "zz-12 34/ab", "ZZ1234AB"),  # no stdnum module: fallback
        ("tax_id", {"country": "GB"}, D(980780684), D(980780684)),
        ("decimal", {"scale": 2}, "1.5", D("1.50")),
        ("decimal", {"scale": 2, "rounding": "half_up"}, " 1.005 ", D("1.01")),
        ("decimal", {"scale": 2}, D("1.005"), D("1.00")),  # half_even by default
        ("decimal", {"scale": 2, "rounding": "floor"}, D("-1.005"), D("-1.01")),
        ("decimal", {"scale": 2, "rounding": "ceiling"}, D("1.001"), D("1.01")),
        ("decimal", {"scale": 2, "rounding": "down"}, D("1.009"), D("1.00")),
        ("decimal", {"scale": 2, "rounding": "up"}, D("1.001"), D("1.01")),
        ("decimal", {"scale": 0}, "1e3", D("1000")),
        ("decimal", {"scale": 1}, ".5", D("0.5")),
        (
            "decimal",
            {"scale": 2},
            "123456789012345678901234567890.129",
            D("123456789012345678901234567890.13"),
        ),
        ("decimal", {"scale": D(2)}, "1", D("1.00")),  # a scale read from JSON is a Decimal
        ("decimal", {"scale": 2}, "1e999999", "1e999999"),  # out of range
        ("decimal", {"scale": 1}, "\u0661.\u0665", "\u0661.\u0665"),  # not a JSON numeral
        ("timestamp_utc", {"precision": "s"}, "2024-01-01T10:00:00+02:00", "2024-01-01T08:00:00Z"),
        ("timestamp_utc", {"precision": "ms"}, "2024-01-01T10:00:00.123456789Z", "2024-01-01T10:00:00.123Z"),
        (
            "timestamp_utc",
            {"precision": "ms", "mode": "round"},
            "2024-12-31T23:59:59.9996Z",
            "2025-01-01T00:00:00.000Z",
        ),
        ("timestamp_utc", {"precision": "ms"}, "2024-12-31T23:59:59.9996Z", "2024-12-31T23:59:59.999Z"),
        (
            "timestamp_utc",
            {"precision": "s", "mode": "round"},
            "2024-01-01T10:00:00.5Z",
            "2024-01-01T10:00:01Z",
        ),
        ("timestamp_utc", {"precision": "us"}, "2024-01-01T10:00:00.1Z", "2024-01-01T10:00:00.100000Z"),
        ("timestamp_utc", {"precision": "s"}, "2024-02-03", "2024-02-03T00:00:00Z"),
        ("timestamp_utc", {"precision": "s"}, "2024-02-03 04:05", "2024-02-03T04:05:00Z"),
        ("timestamp_utc", {"precision": "s"}, "2024-02-03t04:05:06z", "2024-02-03T04:05:06Z"),
        ("timestamp_utc", {"precision": "s"}, "2024-01-01T00:30:00-0130", "2024-01-01T02:00:00Z"),
        ("timestamp_utc", {"precision": "s"}, "2024-01-01T00:30:00+01", "2023-12-31T23:30:00Z"),
        ("timestamp_utc", {"precision": "ms"}, "20240203T040506,5+0100", "2024-02-03T03:05:06.500Z"),
        ("timestamp_utc", {"precision": "s"}, "0001-01-01T05:00:00+01:00", "0001-01-01T04:00:00Z"),
    ],
)
def test_normaliser(name, params, value, expected) -> None:
    out = apply_chain(value, [N(name, params)])
    assert out == expected and type(out) is type(expected)
    if isinstance(expected, Decimal):
        assert str(out) == str(expected)  # the quantized scale is kept


@pytest.mark.parametrize(
    "value",
    [
        "2024-02-30",
        "2024-01-01T24:00:00Z",
        "2024-06-30T23:59:60Z",
        "2024-01-01T10:00:00+25:00",
        "0001-01-01T00:00:00+01:00",
        "2024/01/01",
        "\u0662\u0660\u0662\u0664-01-01",
    ],
)
def test_timestamps_that_are_no_instant_are_unchanged(value) -> None:
    assert apply_chain(value, [N("timestamp_utc", {"precision": "s"})]) == value


def test_abbreviations_are_fixed_points() -> None:
    assert not set(ADDRESS_ABBREVIATIONS.values()) & set(ADDRESS_ABBREVIATIONS)


def test_chains_apply_in_order() -> None:
    assert apply_chain("  a  b ", [N("collapse_ws"), N("trim"), N("upper")]) == "A B"
    assert apply_chain("  ", [N("trim"), N("null_if_empty")]) is None
    assert apply_chain("  ", [N("null_if_empty"), N("trim")]) == ""
    assert apply_chain(" x ", []) == " x "


def test_an_unknown_normaliser_is_a_spec_error() -> None:
    with pytest.raises(ExpressionError):
        apply_chain("x", [N("rot13")])


def test_read_decimal() -> None:
    assert (read_decimal("1.50"), read_decimal(D(2)), read_decimal(3)) == (D("1.50"), D(2), D(3))
    assert all(read_decimal(v) is None for v in (True, None, "x", D("NaN"), [1], "1e99999", "1_000"))
