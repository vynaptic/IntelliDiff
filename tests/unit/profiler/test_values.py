"""Light tokens (what "differs only in presentation" means) and example-free value shapes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltasleuth.profiler.values import light_token, value_shape


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1250.10", Decimal("1250.1")),
        (Decimal("5"), 5),
        ("07700 900123", "+447700900123"),
        ("020 7946 0123", "+442079460123"),
        ("2019-03-02T10:11:12Z", "2019-03-02T10:11:12.123456789Z"),
        ("2025-01-01T01:00:00+01:00", "2025-01-01T00:00:00Z"),
        ("2020-01-01", "2020-01-01T00:00:00Z"),
        ("SW1A 1AA", "sw1a1aa"),
        ("Ada.Lovelace@Example.org", "ada.lovelace@example.org"),
        ("40-47-84", "404784"),
        ("en-GB", "en_gb"),
    ],
)
def test_presentation_differences_agree(a: object, b: object) -> None:
    assert light_token(a) == light_token(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("12", "13"),
        (True, "true"),
        ("abc", "abd"),
        (Decimal("1"), True),
        ("2020-01-01", "2020-01-02"),
        ("2020-02-30", "2020-03-01"),
        ("2024-01-02T01:00:00+25:00", "2024-01-01T00:00:00Z"),  # no such offset: not an instant
    ],
)
def test_real_differences_stay_apart(a: object, b: object) -> None:
    assert light_token(a) != light_token(b)


@pytest.mark.parametrize("value", [None, {}, [], {"a": 1}, [1]])
def test_only_scalars_have_a_token(value: object) -> None:
    assert light_token(value) is None


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        ("C00012345", "A9{8}"),
        ("00-00-00", "9{2}-9{2}-9{2}"),
        ("sw1a 1aa", "a{2}9a 9a{2}"),
        ("tk_fake_Zq81xVv3K9aJ4mT2pW7c", "a{2}_a{4}_Aa9{2}aAa9A9aA9aA9aA9a"),
        ("x" * 100, "a{48}…"),
    ],
)
def test_shapes_hold_classes_never_values(text: str, shape: str) -> None:
    assert value_shape(text) == shape
