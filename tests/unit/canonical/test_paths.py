"""Path patterns and pointers: rule paths and finding patterns must agree by string equality."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltasleuth.core.canonical.errors import PathError
from deltasleuth.core.canonical.paths import find, normalise_pattern, pattern_of, render_pointer


def test_find_returns_values_with_concrete_locations() -> None:
    doc = {"a": [{"k": Decimal("1.0")}, {"k": Decimal(2)}, {"x": 0}], "b c": {"é": 1}}
    assert find("$.a[*].k", doc) == [(Decimal("1.0"), ("a", 0, "k")), (Decimal(2), ("a", 1, "k"))]
    assert [loc for _, loc in find("$.a[?@.k == 1]", doc)] == [("a", 0)]
    assert find("$['b c'].é", doc) == [(1, ("b c", "é"))]
    assert find("$", doc) == [(doc, ())]


@pytest.mark.parametrize("path", ["$.a[", "a.b"])
def test_invalid_syntax_is_a_path_error(path) -> None:
    with pytest.raises(PathError):
        find(path, {})


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ((), "$"),
        (("a", 0, "b"), "$.a[*].b"),
        (("_x9", 3), "$._x9[*]"),
        (("a b",), "$['a b']"),
        (("9lives",), "$['9lives']"),
        (("it's",), "$['it\\'s']"),
        (("back\\slash",), "$['back\\\\slash']"),
        (("tab\there",), "$['tab\\there']"),
        (("bell\x07",), "$['bell\\u0007']"),
        (("é", "@type"), "$['é']['@type']"),
    ],
)
def test_pattern_of(location, expected) -> None:
    assert pattern_of(location) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", "$"),
        ("$['a'][*]['b']", "$.a[*].b"),
        ('$["a"].*.b', "$.a[*].b"),
        ("$['a b'].c", "$['a b'].c"),
        ("$['it\\'s']", "$['it\\'s']"),
    ],
)
def test_normalise_pattern(path, expected) -> None:
    assert normalise_pattern(path) == expected


@pytest.mark.parametrize("path", ["$..a", "$.a[1:2]", "$.a[?@.x == 1]", "$['a','b']", "$.a[0].b", "$.a[-1]"])
def test_selectors_without_a_pattern_form_are_refused(path) -> None:
    # An index would silently widen a rule to every element; the others select sets.
    with pytest.raises(PathError):
        normalise_pattern(path)


def test_render_pointer_escapes_and_tells_duplicates_from_literal_at() -> None:
    parts = (("field", "accounts"), ("member", "K/1"), ("dup", "0"), ("field", "@type"), ("index", "2"))
    assert render_pointer(parts) == "/accounts/K~11/@0/@@type/2"
    assert render_pointer((("field", "~/"), ("field", "x@"), ("field", "@"), ("field", ""))) == "/~0~1/x@/@@/"
    assert render_pointer(()) == ""
