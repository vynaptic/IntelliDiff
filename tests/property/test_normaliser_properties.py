"""Every normaliser is total (never raises on data) and idempotent."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from deltasleuth.core.canonical.encoding import exact_json
from deltasleuth.core.canonical.normalisers import compile_chain
from deltasleuth.core.types import Normalizer

N = Normalizer
# Local settings (a global profile would leak into other Hypothesis tests).
PROPERTY = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

ANY_TEXT = st.text(max_size=12)
DIGITS = st.text(alphabet="0123456789 -+()./", max_size=16)
PHONES_WITH_EXTENSIONS = st.builds(
    "{}{}{}".format,
    st.sampled_from(["+44 20 7946 0018", "020 7946 0018", "+1 650-253-0000", "(202) 555-0143"]),
    st.sampled_from([" ext. ", " x", ";ext=", " #", " extension "]),
    st.text(alphabet="0123456789", min_size=1, max_size=6),
)
EMAILS = st.from_regex(r"\s?[A-Za-z0-9.]{1,6}@[A-Za-z0-9.]{1,6}\s?", fullmatch=True)
TIMESTAMPS = st.one_of(
    st.from_regex(
        r"\d{4}-[01]\d-[0-3]\d([T ][0-2]\d:[0-5]\d(:[0-6]\d([.,]\d{1,9})?)?(Z|[+-][0-2]\d(:?[0-5]\d)?)?)?",
        fullmatch=True,
    ),
    st.from_regex(r"\d{8}(T\d{4}(\d{2}([.,]\d{1,9})?)?(Z|[+-]\d{2}(\d{2})?)?)?", fullmatch=True),
)
NUMERIC_TEXT = st.from_regex(
    r"\s?[+-]?(\d{1,25}(\.\d{0,12})?|\.\d{1,12})([eE][+-]?\d{1,3})?\s?", fullmatch=True
)
NUMBERS = st.decimals(allow_nan=False, allow_infinity=False)
NON_TEXT = st.one_of(st.none(), st.booleans(), NUMBERS, st.lists(st.integers(), max_size=2), st.just({}))

CASES: list[tuple[Normalizer, Any]] = [
    (N("trim"), ANY_TEXT),
    (N("collapse_ws"), ANY_TEXT),
    (N("nfkc"), ANY_TEXT),
    (N("casefold"), ANY_TEXT),
    (N("upper"), ANY_TEXT),
    (N("lower"), ANY_TEXT),
    (N("email_basic"), st.one_of(EMAILS, ANY_TEXT)),
    (N("digits_only"), st.one_of(DIGITS, ANY_TEXT)),
    (N("null_if_empty"), st.one_of(ANY_TEXT, NON_TEXT)),
    (N("address_std"), ANY_TEXT),
    (N("e164", {"defaultRegion": "GB"}), st.one_of(DIGITS, ANY_TEXT, PHONES_WITH_EXTENSIONS)),
    (N("e164", {"defaultRegion": "US"}), st.one_of(DIGITS, PHONES_WITH_EXTENSIONS)),
    (N("tax_id", {"country": "GB"}), st.one_of(DIGITS, st.text(alphabet="GB0123456789 ", max_size=14))),
    (N("tax_id", {"country": "US"}), DIGITS),
    (N("tax_id", {"country": "DE"}), st.text(alphabet="DE0123456789 ,.", max_size=14)),
    (N("tax_id", {"country": "ZZ"}), ANY_TEXT),
    (N("decimal", {"scale": 2}), st.one_of(NUMERIC_TEXT, NUMBERS, ANY_TEXT)),
    (N("decimal", {"scale": 0, "rounding": "half_up"}), st.one_of(NUMERIC_TEXT, NUMBERS)),
    (N("decimal", {"scale": 5, "rounding": "floor"}), st.one_of(NUMERIC_TEXT, NUMBERS)),
    (N("timestamp_utc", {"precision": "s"}), st.one_of(TIMESTAMPS, ANY_TEXT)),
    (N("timestamp_utc", {"precision": "ms", "mode": "round"}), TIMESTAMPS),
    (N("timestamp_utc", {"precision": "us", "mode": "round"}), TIMESTAMPS),
]


def same(x: Any, y: Any) -> bool:
    return type(x) is type(y) and exact_json(x) == exact_json(y)


@pytest.mark.parametrize(("normalizer", "values"), CASES, ids=lambda c: getattr(c, "name", ""))
def test_idempotent_and_total(normalizer: Normalizer, values: Any) -> None:
    step = compile_chain([normalizer])

    @PROPERTY
    @given(values)
    def check(value: Any) -> None:
        once = step(value)
        assert same(step(once), once)

    check()


@pytest.mark.parametrize(
    "normalizer",
    [n for n, _ in CASES if n.name not in ("null_if_empty", "decimal")],
    ids=lambda n: n.name,
)
def test_non_strings_are_unchanged(normalizer: Normalizer) -> None:
    step = compile_chain([normalizer])

    @PROPERTY
    @given(NON_TEXT)
    def check(value: Any) -> None:
        assert same(step(value), value)

    check()


def test_decimal_output_is_quantized() -> None:
    step = compile_chain([N("decimal", {"scale": 3})])
    assert step(Decimal("1.23456")).as_tuple().exponent == -3
