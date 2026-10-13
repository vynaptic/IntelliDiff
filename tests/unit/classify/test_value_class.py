"""Value classes of differences."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deltasleuth.core.classify.value_class import value_class
from deltasleuth.core.types import VALUE_CLASSES, KeyedMap


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("Ada", "ADA", "case_only"),
        ("straße", "STRASSE", "case_only"),
        ("07700 900123", "07700900123", "whitespace"),
        ("a\tb", "a  b", "whitespace"),
        ("00-00-00", "000000", "format_only"),
        ("SW1A-1AA", "SW1A 1AA.", "format_only"),
        ("12345678", "12354678", "digit_transposition"),
        ("AB-1290", "AB-1209", "digit_transposition"),
        ("0.0125", "0.01250001", "numeric_precision"),
        ("12345678", "12345687", "digit_transposition"),
        ("12345678", "12345679", "other"),
        ("12345678", "13245768", "other"),  # two swaps
        ("1234", "1432", "other"),  # a non-adjacent swap
        ("ab", "ba", "other"),  # letters are not digits
        ("Ada Lovelace", "Grace Hopper", "other"),
        ("sw1a 1aa", "SW1A1AA", "other"),  # case and separator together
    ],
)
def test_string_classes(a: str, b: str, expected: str) -> None:
    assert value_class(a, b, a, b) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (D("0.0125"), D("0.01250001"), "numeric_precision"),
        (D("1250.1"), D("1250.13"), "numeric_precision"),  # truncation
        (D("1250.1"), D("1250.06"), "numeric_precision"),  # rounding
        (D("3"), D("2.9999"), "numeric_precision"),
        (D("100"), D("123"), "other"),  # same precision: a real difference
        (D("1.25"), D("1.52"), "digit_transposition"),
        (D("12"), D("21"), "digit_transposition"),
        (D("1250.1"), D("1250.2"), "other"),
        (D("1E+3"), D("1000.4"), "numeric_precision"),
        (3, 4, "other"),  # multiset counts arrive as ints
    ],
)
def test_number_classes(a: object, b: object, expected: str) -> None:
    assert value_class(a, b, a, b) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("12", D("12"), "type_only"),
        (D("12"), "12", "type_only"),
        ("1250.10", D("1250.1"), "type_only"),
        ("true", True, "type_only"),
        (False, "false", "type_only"),
        ("null", None, "type_only"),
        ("12", D("13"), "other"),
        ("abc", D("1"), "other"),
        (True, D("1"), "other"),
        ({"a": 1}, "x", "other"),
        (None, "x", "other"),
        ("x", None, "other"),
    ],
)
def test_type_classes(a: object, b: object, expected: str) -> None:
    assert value_class(a, b, a, b) == expected


def test_normalised_values_win_when_they_differ_else_the_raw_ones_explain() -> None:
    assert value_class("Ab12", "aB12", "12", "21") == "digit_transposition"
    assert value_class("Ada", "ADA", "ada", "ada") == "case_only"
    assert value_class(" x ", "x", "x", "x") == "whitespace"


km_a, km_b = KeyedMap({"k": [D(1)]}, mode="set"), KeyedMap({"j": [D(1)]}, mode="set")


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (None, None, "null_vs_absent"),  # a finding holds None for an absent side
        ([1, 2], [1, 2], "other"),  # equal values: the difference is elsewhere (an order change)
        ("x", "x", "other"),
        ({"a": D(1)}, {"a": D(2)}, "other"),
        (km_a, km_b, "other"),
        ({"n": "1"}, None, "other"),
    ],
)
def test_other_classes(a: object, b: object, expected: str) -> None:
    assert value_class(a, b, a, b) == expected


_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.text(max_size=12),
    st.decimals(allow_nan=False, allow_infinity=False, places=4),
    st.integers(min_value=-(10**6), max_value=10**6),
)


@given(_scalars, _scalars)
def test_result_is_always_a_known_class_and_symmetric(a: object, b: object) -> None:
    got = value_class(a, b, a, b)
    assert got in VALUE_CLASSES
    assert got not in ("duplicate_key", "intermittent_b")  # set by the differ / recheck only
    assert value_class(b, a, b, a) == got
