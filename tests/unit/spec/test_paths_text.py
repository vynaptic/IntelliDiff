"""Canonical JSONPath text: rule paths compare with finding path patterns by string equality."""

from __future__ import annotations

import pytest

from deltasleuth.core.spec import PathError, normalise_pattern, normalise_query


@pytest.mark.parametrize(
    ("path", "canonical"),
    [
        ("$", "$"),
        ("$['a']", "$.a"),
        ('$["phones"][*]', "$.phones[*]"),
        ("$.a.*", "$.a[*]"),
        ("$['a b'][*].c", "$['a b'][*].c"),
        ("$.x-y", "$['x-y']"),  # python-jsonpath accepts it; stored in the one canonical form
        ("$['1abc']", "$['1abc']"),
        ("$._private", "$._private"),
        ("$['é']", "$['é']"),
        ("$['it\\'s']", "$['it\\'s']"),
        ("$['back\\\\slash']", "$['back\\\\slash']"),
        ('$["quote\\"d"]', "$['quote\"d']"),
        ("$['a\\tb']['c\\b']", "$['a\\tb']['c\\b']"),  # control characters stay escaped
        ("$ .a", "$.a"),
    ],
)
def test_normalise_pattern(path: str, canonical: str) -> None:
    assert normalise_pattern(path) == canonical
    assert normalise_pattern(canonical) == canonical  # idempotent


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("$.items[0].name", "array positions"),
        ("$..name", "descendant"),
        ("$.items[?@.active]", "filter"),
        ("$['a','b']", "unions"),
        ("$.items[1:3]", "slice"),
        ("$.a[", "unexpected end"),
        ("a.b", "TOKEN_ROOT"),
        ("$.a|$.b", "unexpected token"),
        ("$.a ", "trailing whitespace"),
    ],
)
def test_not_a_pattern(path: str, reason: str) -> None:
    with pytest.raises(PathError, match=reason) as exc:
        normalise_pattern(path)
    assert "\n" not in str(exc.value)


@pytest.mark.parametrize(
    ("path", "canonical"),
    [
        ("$['data']", "$.data"),
        ("$.data.items[0]", "$.data.items[0]"),
        ("$.items[?@.active]", "$.items[?@.active]"),  # richer queries stay as written
        ("$..id", "$..id"),
    ],
)
def test_normalise_query(path: str, canonical: str) -> None:
    assert normalise_query(path) == canonical
