"""Compile checks for the expression languages a spec embeds: CEL, JSONata and key regexes. A JSONata
expression compiles with the map's regular expression engine, so a regular expression a map may not run is
refused when the spec loads.

``CEL_VARIABLES`` is the single source of truth for the names each kind of CEL predicate may use:
the loader compiles with exactly these, and every engine evaluator binds all of them, so a
predicate that validated never meets an undeclared name at run time.
"""

from __future__ import annotations

import functools
import re
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from re import _constants, _parser  # type: ignore[attr-defined]  # typeshed omits re's private modules
from typing import Literal

import jsonata
import regex
from cel_expr_python import cel
from jsonata.jexception import JException

from deltasleuth.core.canonical.mapping import regex_engine
from deltasleuth.core.canonical.regexes import RegexLimitError, RegexRefused, regex_work
from deltasleuth.core.spec import _keypattern_process

CelKind = Literal[
    "known_diff",
    "value_filter",
    "not_found",
    "absence_shape",
    "signoff_stratum",
    "stratum_dimension",
    "sampling_stratum",
]

# Values that may be null or structured are DYN; always-string values are STRING so type errors
# surface at validation time.
CEL_VARIABLES: dict[CelKind, dict[str, str]] = {
    "known_diff": {
        "key": "STRING",
        "stratum": "DYN",
        "strata": "DYN",
        "path": "STRING",
        "pointer": "STRING",
        "change_type": "STRING",
        "direction": "STRING",
        "a": "DYN",
        "b": "DYN",
        "value_class": "DYN",
        "taxonomy_code": "DYN",
        "severity": "DYN",
        "link_reason": "DYN",
        "hop_count": "DYN",
        "attrs": "DYN",
    },
    "value_filter": {
        "key": "STRING",
        "side": "STRING",
        "path": "STRING",
        "pointer": "STRING",
        "value": "DYN",
        "stratum": "DYN",
        "strata": "DYN",
    },
    # The schema documents response.{status, headers, body}; the bare names are bound too.
    "not_found": {"response": "DYN", "status": "INT", "headers": "DYN", "body": "DYN"},
    "absence_shape": {"body": "DYN", "status": "INT", "headers": "DYN"},
    # A hash sample's frame row, and now, when the frame was read.
    "signoff_stratum": {
        "key": "STRING",
        "row": "DYN",
        "strata": "DYN",
        "riskTier": "STRING",
        "now": "TIMESTAMP",
    },
    "stratum_dimension": {"key": "STRING", "row": "DYN", "now": "TIMESTAMP"},
    "sampling_stratum": {"row": "DYN", "attrs": "DYN"},
}

# The result type each kind must produce (None = any); a DYN result is decided at run time.
_RESULT: dict[CelKind, str | None] = {
    "known_diff": "BOOL",
    "value_filter": "BOOL",
    "not_found": "BOOL",
    "absence_shape": "BOOL",
    "signoff_stratum": "STRING",
    "stratum_dimension": None,
    "sampling_stratum": None,
}


@functools.cache
def cel_environment(kind: CelKind) -> cel.Env:
    """The typed CEL environment of ``kind``: the loader checks, and evaluators compile, in it."""
    return cel.NewEnv(variables={name: getattr(cel.Type, t) for name, t in CEL_VARIABLES[kind].items()})


def cel_error(source: str, kind: CelKind) -> str | None:
    """Why ``source`` does not compile as a CEL predicate of ``kind``, or None."""
    try:
        compiled = cel_environment(kind).compile(source)
    except (RuntimeError, ValueError) as exc:
        line = str(exc).splitlines()[0].removeprefix("INVALID_ARGUMENT: ").removeprefix("ERROR: <input>:")
        return line.removesuffix(" [INVALID_ARGUMENT]").strip()
    wanted, produced = _RESULT[kind], compiled.return_type()
    if wanted is not None and produced not in (getattr(cel.Type, wanted), cel.Type.DYN):
        return f"must evaluate to {wanted.lower()}, but evaluates to {produced}"
    return None


def reads(source: str, kind: CelKind, name: str) -> bool:
    """True when the valid predicate ``source`` reads the variable ``name`` (it needs it to compile)."""
    variables = {n: getattr(cel.Type, t) for n, t in CEL_VARIABLES[kind].items() if n != name}
    try:
        cel.NewEnv(variables=variables).compile(source)
    except (RuntimeError, ValueError):
        return cel_error(source, kind) is None
    return False


def jsonata_error(source: str) -> str | None:
    """Why ``source`` does not parse as JSONata, or None."""
    try:
        jsonata.Jsonata(source, regex_engine)
    except JException as exc:
        return f"{exc.error}: {exc} (at character {exc.location})"
    except RegexRefused as exc:
        return str(exc)
    except RecursionError:
        return "the expression nests too deeply"
    return None


MAX_KEY_PATTERN_CHARS = 4000
MAX_KEY_PATTERN_WORK = 16_000  # finite-repeat expansion, before the native compiler sees it
MAX_KEY_SEARCH_CHARS = 2**20
MAX_KEY_SEARCH_WORK = 2_000_000  # also bounds native capture-history growth for long inputs
KEY_SEARCH_SECONDS = 0.02
KEY_AUTHORING_SECONDS = 2.0
_KEY_DEADLINE: ContextVar[float | None] = ContextVar("key_pattern_deadline", default=None)


class KeyPatternLimitError(ValueError):
    """The key pattern cannot be evaluated within its resource limits; never a non-match."""


@contextmanager
def key_pattern_budget() -> Iterator[None]:
    """Bound a synchronous authoring operation's aggregate matching work, including cheap misses."""
    until = time.monotonic() + KEY_AUTHORING_SECONDS
    current = _KEY_DEADLINE.get()
    token = _KEY_DEADLINE.set(until if current is None else min(current, until))
    try:
        yield
    finally:
        _KEY_DEADLINE.reset(token)


def _pattern_work(nodes: _parser.SubPattern) -> int:
    """Bound finite repeats and nested products, as a map's regular expressions are bounded."""
    try:
        return regex_work(nodes, MAX_KEY_PATTERN_WORK)
    except RegexLimitError:
        message = "key.pattern exceeds its compilation work limit; simplify its repeats"
        raise KeyPatternLimitError(message) from None


def _native_safe(nodes: _parser.SubPattern, flags: int) -> bool:
    """Only the shared ASCII grammar uses regex's fast path.

    VERSION0 still differs for Unicode word characters, empty-string \\B and literal syntax
    that regex extends (fuzzy braces and POSIX classes). Other constructs use CPython itself.
    """
    if flags & re.UNICODE:
        return False
    for op, value in nodes:
        if op in (_constants.LITERAL, _constants.NOT_LITERAL):
            if value in (ord("{"), ord("}")):
                return False
        elif op == _constants.IN:
            if any(inner == _constants.LITERAL and char == ord("[") for inner, char in value):
                return False
        elif op in (_constants.MAX_REPEAT, _constants.MIN_REPEAT, _constants.POSSESSIVE_REPEAT):
            if not _native_safe(value[2], flags):
                return False
        elif op == _constants.SUBPATTERN:
            _, added, removed, child = value
            if not _native_safe(child, (flags | added) & ~removed):
                return False
        elif op == _constants.BRANCH:
            if not all(_native_safe(branch, flags) for branch in value[1]):
                return False
        elif op == _constants.AT:
            if value == _constants.AT_NON_BOUNDARY:
                return False
        elif op != _constants.ANY:
            return False
    return True


@dataclass(frozen=True)
class KeyPattern:
    pattern: str
    _compiled: regex.Pattern[str] | None = field(repr=False)
    _flags: int = field(repr=False)
    _work: int = field(repr=False)

    def search(self, text: str) -> object | None:
        deadline = _KEY_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise KeyPatternLimitError("key.pattern exceeds its authoring work limit; simplify the pattern")
        if len(text) > MAX_KEY_SEARCH_CHARS:
            raise KeyPatternLimitError("key.pattern input exceeds its search size limit")
        if len(text) * self._work > MAX_KEY_SEARCH_WORK:
            message = "key.pattern input exceeds its search work limit; simplify the pattern"
            raise KeyPatternLimitError(message)
        if self._compiled is None:
            try:
                return True if _keypattern_process.search(self.pattern, self._flags, text) else None
            except _keypattern_process.WorkerError as exc:
                raise KeyPatternLimitError(str(exc)) from exc
        try:
            return self._compiled.search(text, timeout=KEY_SEARCH_SECONDS)
        except TimeoutError:
            message = "key.pattern exceeds its search time limit; simplify the pattern"
            raise KeyPatternLimitError(message) from None


@functools.lru_cache(maxsize=128)
def compile_key_pattern(pattern: str, flags: int = re.ASCII) -> KeyPattern:
    """A bounded Python-compatible search; ASCII keeps ``\\d`` and ``\\w`` ECMA-262-like.

    The stdlib syntax gate and shared-grammar check preserve Python semantics. Native matching or
    isolated CPython workers enforce the timeout; a thread deadline cannot interrupt stdlib re.
    """
    if len(pattern) > MAX_KEY_PATTERN_CHARS:
        raise KeyPatternLimitError(f"key.pattern is at most {MAX_KEY_PATTERN_CHARS} characters")
    re.compile(pattern, flags)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)  # the syntax gate already surfaced the same warnings
        parsed = _parser.parse(pattern, flags)
    work = _pattern_work(parsed)
    if not _native_safe(parsed, parsed.state.flags):
        return KeyPattern(pattern, None, flags, work)
    bounded_flags = regex.VERSION0
    for source, bounded in (
        (re.ASCII, regex.ASCII), (re.IGNORECASE, regex.IGNORECASE), (re.MULTILINE, regex.MULTILINE),
        (re.DOTALL, regex.DOTALL), (re.VERBOSE, regex.VERBOSE), (re.UNICODE, regex.UNICODE),
    ):
        if flags & source:
            bounded_flags |= bounded
    return KeyPattern(pattern, regex.compile(pattern, bounded_flags, cache_pattern=False), flags, work)


def regex_error(pattern: str) -> str | None:
    """Why ``pattern`` does not compile as a key regex, or None."""
    try:
        compile_key_pattern(pattern)
    except (re.error, regex.error, ValueError, OverflowError, RecursionError) as exc:
        return f"invalid regular expression: {exc}"
    return None
