"""Structural path patterns: rule paths and finding patterns match by position, not spelling."""

from __future__ import annotations

import pytest

from deltasleuth.core.classify.patterns import DESC, WILD, PatternError, name, segments


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", ()),
        ("$.a[*].b", (name("a"), WILD, name("b"))),
        ("$['a'][0]['b']", (name("a"), WILD, name("b"))),  # an index is any position
        ('$["we ird"][-1]', (name("we ird"), WILD)),
        ("$.a.*", (name("a"), WILD)),
        ("$..email", (DESC, name("email"))),
        ("$..[*]", (DESC, WILD)),
        ("$['it\\'s']", (name("it's"),)),
        ("$['\\u0041\\n\\u0001']", (name("A\n\x01"),)),  # control characters, as the differ escapes them
        ("$['*']", (name("*"),)),  # a member named "*" is not a wildcard
        ("  $.a  ", (name("a"),)),
    ],
)
def test_segments(path: str, expected: tuple) -> None:
    assert segments(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "a.b",
        "$.a[?@.x == 1]",
        "$.a[0:2]",
        "$.a['x','y']",
        "$.a['open",
        "$.a['bad\\q']",
        "$.a['\\u00zz']",
        "$.",
        "$.1a",
        "$a",
    ],
)
def test_unsupported_paths_raise(path: str) -> None:
    with pytest.raises(PatternError):
        segments(path)
