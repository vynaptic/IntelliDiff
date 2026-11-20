"""Bounds on the regular expressions a spec, or the data it reads, gives the engine: a map's (``mapping``) and
a JSONPath filter's ``match()`` and ``search()`` (``paths``).

Python's ``re`` cannot be interrupted and holds the interpreter lock, so a pattern that backtracks for hours
holds the whole server process: every one of them runs on the ``regex`` engine, which stops at a time bound.
Before it runs, a pattern is refused when it is longer than ``MAX_REGEX_CHARS``, when compiling it is more
work than ``MAX_REGEX_WORK`` (finite repeats expanded), or when it holds a repeat inside a repeat, which can
backtrack for hours on a long string; an inner repeat that is possessive or atomic gives nothing back and is
accepted. A pattern reads at most ``MAX_REGEX_INPUT`` characters of one string.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from re import _constants as _sre  # type: ignore[attr-defined]  # typeshed omits re's private modules
from re import _parser  # type: ignore[attr-defined]  # as above
from typing import Any

from deltasleuth.core.canonical.errors import ExpressionError

# The characters of one string a pattern reads, and a key pattern's bounds on its size and on its compilation
# work.
MAX_REGEX_INPUT = 10_000
MAX_REGEX_CHARS = 4000
MAX_REGEX_WORK = 16_000

NESTED = "has a repeat inside a repeat, which can backtrack for hours on a long string"
_NESTED_FIX = "make the inner repeat possessive (x++) or atomic ((?>x+)), or write it without nesting"


class RegexRefused(ExpressionError):
    """A regular expression the spec writes that may not run: its message names the pattern, never a value."""


class RegexLimitError(ValueError):
    """A parsed pattern past its compilation work bound."""


def shown(pattern: str) -> str:
    """``pattern`` as a message shows it."""
    return f"/{pattern if len(pattern) <= 40 else pattern[:40] + '…'}/"


def refused(pattern: str, why: str) -> RegexRefused:
    """The refusal of a pattern the spec writes, for the reason ``why`` (``too_long``, ``unbounded``)."""
    fix = f": {_NESTED_FIX}" if why == NESTED else ""
    return RegexRefused(f"regular expression {shown(pattern)} {why}{fix}")


def too_long(pattern: str) -> str | None:
    """Why ``pattern`` is too long to run, or None."""
    if len(pattern) > MAX_REGEX_CHARS:
        return f"is too long: a pattern is at most {MAX_REGEX_CHARS} characters"
    return None


def parse(pattern: str, flags: int) -> _parser.SubPattern:
    """``pattern`` as Python's ``re`` parses it; ``re.error`` when it does not."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)  # re.compile surfaces them
        return _parser.parse(pattern, flags)


def unbounded(parsed: _parser.SubPattern) -> str | None:
    """Why a parsed pattern may not run, or None. The reason quotes no part of the pattern."""
    try:
        regex_work(parsed, MAX_REGEX_WORK)
    except RegexLimitError:
        return "exceeds its compilation work limit; simplify its repeats"
    return NESTED if nested_repeat(parsed, False) else None


def regex_work(nodes: _parser.SubPattern, limit: int) -> int:
    """The work of compiling a parsed pattern: each operation once, a finite repeat's body as often as it may
    repeat; ``RegexLimitError`` once it passes ``limit``. Python is pinned to 3.13: counting the parsed
    operations reads escapes, classes, comments and inline flags as the compiler does, where braces in the
    source would be misread as repeats."""
    work = 0
    for op, value in nodes:
        cost = 1
        if op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT, _sre.POSSESSIVE_REPEAT):
            low, high, child = value
            cost += regex_work(child, limit) * (max(1, low) if high == _sre.MAXREPEAT else max(1, high))
        elif op == _sre.SUBPATTERN:
            cost += regex_work(value[-1], limit)
        elif op == _sre.BRANCH:
            cost += sum(regex_work(branch, limit) for branch in value[1])
        elif op in (_sre.ASSERT, _sre.ASSERT_NOT):
            cost += regex_work(value[1], limit)
        elif op == _sre.ATOMIC_GROUP:
            cost += regex_work(value, limit)
        elif op == _sre.GROUPREF_EXISTS:
            cost += regex_work(value[1], limit) + (regex_work(value[2], limit) if value[2] else 0)
        work += cost
        if work > limit:
            raise RegexLimitError
    return work


def nested_repeat(nodes: Sequence[Any], repeated: bool) -> bool:
    """Whether a repeat that may run more than once (``repeated``: one encloses ``nodes``) holds an unbounded
    repeat that gives back what it took. A possessive repeat's body and an atomic group give nothing back
    once matched: only what nests inside them counts."""
    for op, value in nodes:
        if op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT):
            _, high, child = value
            if (repeated and high == _sre.MAXREPEAT) or nested_repeat(child, repeated or high > 1):
                return True
        elif op == _sre.POSSESSIVE_REPEAT:
            if nested_repeat(value[2], False):
                return True
        elif op == _sre.ATOMIC_GROUP:
            if nested_repeat(value, False):
                return True
        elif op == _sre.SUBPATTERN:
            if nested_repeat(value[-1], repeated):
                return True
        elif op == _sre.BRANCH:
            if any(nested_repeat(branch, repeated) for branch in value[1]):
                return True
        elif op in (_sre.ASSERT, _sre.ASSERT_NOT):
            if nested_repeat(value[1], repeated):
                return True
        elif op == _sre.GROUPREF_EXISTS:
            if any(nested_repeat(branch, repeated) for branch in value[1:] if branch is not None):
                return True
    return False
