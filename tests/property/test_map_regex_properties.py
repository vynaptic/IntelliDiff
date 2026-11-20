"""A map's regular expression runs on the ``regex`` engine, which stops at the map's time bound, written so
that it matches exactly what Python's ``re`` matches. These properties hold the two engines to that: every
class escape over every code point, and generated patterns over generated strings, through each call
JSONata makes (search, finditer, sub, split).

The alphabet is small on purpose and holds the characters where the engines' own class escapes or case
folding differ: a combining mark, a superscript digit, a digit of another script, an information separator,
an ideographic space, the Kelvin sign, a long s, a sharp s, a dotted and a dotless i, a final sigma, a
character outside the Basic Multilingual Plane and a lone surrogate.
"""

from __future__ import annotations

import re
import sys

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from deltasleuth.core.canonical import mapping
from deltasleuth.core.canonical.errors import ExpressionError

ALPHABET = ["a", "b", "Z", "_", "1", "-", " ", "\n", "é", "́", "²", "٣", "\x1c", "　"]
ALPHABET += [
    "k",
    "s",
    "\u212a",
    "\u017f",
    "\u00df",
    "\u0130",
    "\u0131",
    "\u03c3",
    "\u03c2",
    "\U0001f600",
    "\ud800",
]
ESCAPES = [r"\d", r"\D", r"\s", r"\S", r"\w", r"\W"]
EVERY = "".join(map(chr, range(sys.maxunicode + 1)))


def _native(pattern: str, flags: int = 0) -> mapping._Bounded:
    bounded = mapping._bounded(pattern, flags)
    assert bounded._native is not None, f"{pattern!r} runs on re"
    return bounded


@pytest.mark.parametrize("ascii", [False, True], ids=["unicode", "ascii"])
@pytest.mark.parametrize("escape", ESCAPES)
def test_each_class_escape_matches_the_code_points_re_gives_it(escape: str, ascii: bool) -> None:
    prefix = "(?a)" if ascii else ""
    for pattern in (
        f"{prefix}{escape}",
        f"{prefix}[{escape}]",
        f"{prefix}[^{escape}]",
        f"{prefix}[{escape}_.]",
    ):
        bounded = _native(pattern)
        assert bounded._native.findall(EVERY) == re.findall(pattern, EVERY), pattern  # type: ignore[union-attr]


@pytest.mark.parametrize("prefix", ["", "(?a)"], ids=["unicode", "ascii"])
def test_a_word_boundary_is_where_re_puts_it(prefix: str) -> None:
    text = "".join(ALPHABET) + "a²b ́x٣ Z"
    bounded = _native(prefix + r"\b")
    found = [m.span() for m in bounded._native.finditer(text)]  # type: ignore[union-attr]
    assert found == [m.span() for m in re.finditer(prefix + r"\b", text)]


def test_what_cannot_be_written_runs_on_re() -> None:
    for pattern in [r"\Ba", r"(a)\1", "(a)?(?(1)b|c)", r"(?a:\w)"]:
        assert mapping._bounded(pattern, 0)._native is None, pattern


@pytest.mark.parametrize("flags", [re.IGNORECASE, re.IGNORECASE | re.ASCII], ids=["unicode", "ascii"])
@pytest.mark.parametrize(
    "pattern", ["k", "s", "\u00df", "i", "\u0130", "\u03c3", "[a-z]", "[^k]", r"[\w-]", r"\W"]
)
def test_a_case_insensitive_character_matches_what_re_folds_to_it(pattern: str, flags: int) -> None:
    bounded = _native(pattern, flags)
    assert bounded._native.findall(EVERY) == re.findall(pattern, EVERY, flags), pattern  # type: ignore[union-attr]


_LITERAL = st.sampled_from(ALPHABET).map(re.escape)
_CLASS_ITEM = st.one_of(
    _LITERAL,
    st.sampled_from(ESCAPES),
    st.tuples(st.sampled_from(ALPHABET), st.sampled_from(ALPHABET)).map(
        lambda pair: "-".join(re.escape(c) for c in sorted(pair))
    ),
)
_ATOM = st.one_of(
    _LITERAL,
    st.sampled_from([".", "^", "$", r"\A", r"\Z", r"\b", *ESCAPES]),
    st.tuples(st.booleans(), st.lists(_CLASS_ITEM, min_size=1, max_size=3)).map(
        lambda c: f"[{'^' if c[0] else ''}{''.join(c[1])}]"
    ),
)
_QUANTIFIER = st.tuples(
    st.sampled_from(["*", "+", "?", "{0,2}", "{1,3}", "{2}", "{2,}"]), st.sampled_from(["", "?"])
)


def _compound(inner: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.one_of(
        st.lists(inner, min_size=2, max_size=3).map("".join),
        st.lists(inner, min_size=2, max_size=3).map(lambda parts: "(?:" + "|".join(parts) + ")"),
        inner.map(lambda p: f"({p})"),
        st.tuples(inner, _QUANTIFIER).map(lambda q: f"(?:{q[0]}){q[1][0]}{q[1][1]}"),
        st.tuples(st.sampled_from(["(?=", "(?!", "(?>", "(?m:", "(?s:", "(?a:", "(?i:", "(?-i:"]), inner).map(
            lambda g: f"{g[0]}{g[1]})"
        ),
        _LITERAL.map(lambda c: f"(?<={c})"),
        _LITERAL.map(lambda c: f"(?<!{c})"),
    )


_PATTERN = st.tuples(
    st.sampled_from(["", "(?m)", "(?s)", "(?a)", "(?i)", "(?ai)"]),
    st.recursive(_ATOM, _compound, max_leaves=6),
)
_TEXT = st.lists(st.sampled_from(ALPHABET), max_size=8).map("".join)


_FLAGS = st.sampled_from([0, re.MULTILINE, re.IGNORECASE, re.IGNORECASE | re.MULTILINE])  # JSONata's m and i


def _matched(match: re.Match[str] | None) -> object:
    return None if match is None else (match.span(), match.groups())


@settings(
    max_examples=400, deadline=None, suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow]
)
@given(_PATTERN, st.lists(_TEXT, min_size=1, max_size=4), _FLAGS)
def test_a_written_pattern_matches_what_re_matches(
    pattern: tuple[str, str], texts: list[str], flags: int
) -> None:
    source = "".join(pattern)
    try:
        bounded = mapping._bounded(source, flags)
    except ExpressionError:  # it does not compile, or a repeat inside a repeat
        assume(False)
        return
    assume(bounded._native is not None)
    native, plain = bounded._native, bounded._re
    for text in texts:
        assert _matched(native.search(text)) == _matched(plain.search(text)), (source, text)
        assert [_matched(m) for m in bounded.finditer(text)] == [_matched(m) for m in plain.finditer(text)]
        assert bounded.sub(r"<\g<0>>", text) == plain.sub(r"<\g<0>>", text)
        assert bounded.sub(lambda m: f"[{m.start()}]", text, 1) == plain.sub(
            lambda m: f"[{m.start()}]", text, 1
        )
        assert bounded.split(text) == plain.split(text)
