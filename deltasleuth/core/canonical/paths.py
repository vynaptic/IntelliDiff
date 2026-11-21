"""RFC 9535 paths, path patterns and DeltaSleuth pointers: the one JSONPath implementation
(strict RFC 9535, no python-jsonpath extensions) for specs, targets, connectors and the engine.

Locations are concrete tuples (``("a", 0, "b")``); patterns replace every index or member key
with ``[*]`` (``$.a[*].b``) so rule paths and finding patterns compare by string equality;
pointers are RFC 6901 plus member keys, ``@i`` duplicate segments and a doubled leading ``@``.

A filter's ``match()`` (a full match) and ``search()`` (a partial one) read a pattern as python-jsonpath
reads an I-Regexp (its dot never a line break) and run it inside the shared server process, so they run it
within the bounds ``regexes`` sets: on the ``regex`` engine, the regular expressions of one evaluation sharing
``PATH_REGEX_TIMEOUT_MS``, and on at most ``MAX_REGEX_INPUT`` characters of one string. A pattern is checked
as the path compiles when the path writes it, and as it runs when the data supplies it: one too long, too
much compilation work, with a repeat inside a repeat, or in syntax Python's ``re`` cannot parse to check
that is refused. A pattern that is not valid is refused when the path writes it and matches nothing when the
data supplies it (RFC 9535). A bound reached as the path runs fails the evaluation with a ``MappingError``,
never a match, and its message never quotes the data.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

from jsonpath import JSONPath, JSONPathEnvironment, JSONPathError, JSONPathMatch, JSONPathTypeError
from jsonpath import function_extensions as functions
from jsonpath.filter import StringLiteral
from jsonpath.segments import JSONPathChildSegment
from jsonpath.selectors import IndexSelector, NameSelector, WildcardSelector
from jsonpath.token import Token

from deltasleuth.core.canonical import regexes
from deltasleuth.core.canonical.errors import MappingError, PathError
from deltasleuth.core.types import JSON

Location = tuple[str | int, ...]
PointerPart = tuple[str, str]  # ("field", name) | ("member", key) | ("dup", "i") | ("index", "i")
Parts = tuple[PointerPart, ...]

ROOT_PATTERN = "$"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# RFC 9535 §2.7 normalised-path escapes for a single-quoted name.
_NAME_ESCAPES = {"'": "\\'", "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
# The regular expression time the filters of one evaluation share; a real filter takes microseconds.
PATH_REGEX_TIMEOUT_MS = 1000
_REGEX_LEFT: ContextVar[list[float] | None] = ContextVar("path_regex_left", default=None)  # seconds
# An escape of a pattern, an I-Regexp category escape (\p{Lu}, \P{L}) among them.
_ESCAPE = re.compile(r"\\(?:[pP](?:\{[^}]*\}|.)|.)", re.DOTALL)
_IREGEXP = "write it as an RFC 9485 I-Regexp"


class _Bounded:
    """python-jsonpath's ``match()`` or ``search()`` within the bounds of ``regexes``: the same compiled
    pattern, run on the ``regex`` engine with a timeout. Mixed in before the class it bounds."""

    full: bool  # match(): the whole string
    check_cache: Callable[[str], Any]  # the bounded class's: the pattern compiled, None when not valid

    def checked(self, pattern: str) -> tuple[Any, str | None]:
        """``pattern`` compiled (None when it is not valid) and why it may not run (None when it may)."""
        if why := regexes.too_long(pattern):
            return None, why
        compiled = self.check_cache(pattern)
        return compiled, None if compiled is None else _unbounded(pattern)

    def __call__(self, value: object, pattern: object) -> bool:
        if not isinstance(value, str) or not isinstance(pattern, str):
            return False
        compiled, why = self.checked(pattern)
        if why is not None:
            raise MappingError(f"a JSONPath filter's regular expression {why}")
        if compiled is None:  # RFC 9535: a pattern that is not valid matches nothing
            return False
        if len(value) > regexes.MAX_REGEX_INPUT:
            limit = f"{regexes.MAX_REGEX_INPUT:,} characters"
            raise MappingError(f"a JSONPath filter's regular expression read a string longer than {limit}")
        return _timed(compiled.fullmatch if self.full else compiled.search, value) is not None


class _Match(_Bounded, functions.Match):
    full = True


class _Search(_Bounded, functions.Search):
    full = False


@lru_cache(maxsize=1024)
def _unbounded(pattern: str) -> str | None:
    """Why a valid filter ``pattern`` may not run, or None. Python's ``re`` parses it, a category escape read
    as one character; one it cannot parse cannot be checked."""
    try:
        parsed = regexes.parse(_ESCAPE.sub(_one_character, pattern), 0)
    except (re.error, OverflowError, RecursionError):
        return f"uses syntax that cannot be checked for a repeat inside a repeat; {_IREGEXP}"
    return regexes.unbounded(parsed)


def _one_character(escape: re.Match[str]) -> str:
    return r"\w" if escape.group()[1] in "pP" else escape.group()


def _timed(run: Callable[..., Any], value: str) -> Any:
    """``run(value)`` within what is left of the evaluation's regular expression time."""
    left = _REGEX_LEFT.get()
    budget = PATH_REGEX_TIMEOUT_MS / 1000 if left is None else left[0]
    started = time.monotonic()
    try:
        if budget <= 0:
            raise TimeoutError
        return run(value, concurrent=True, timeout=budget)
    except TimeoutError:
        bound = f"the path's time bound ({PATH_REGEX_TIMEOUT_MS} ms)"
        raise MappingError(f"a JSONPath filter's regular expressions ran past {bound}") from None
    finally:
        if left is not None:
            left[0] -= time.monotonic() - started


class _Environment(JSONPathEnvironment):
    """Strict RFC 9535 with bounded ``match()`` and ``search()``; a pattern the path writes is checked as the
    path compiles."""

    def setup_function_extensions(self) -> None:
        super().setup_function_extensions()
        self.function_extensions["match"] = _Match(thread_safe=True)
        self.function_extensions["search"] = _Search(thread_safe=True)

    def validate_function_extension_signature(self, token: Token, args: list[Any]) -> list[Any]:
        args = super().validate_function_extension_signature(token, args)
        function = self.function_extensions[token.value]
        if isinstance(function, _Bounded) and isinstance(args[1], StringLiteral):
            compiled, why = function.checked(args[1].value)
            if why is None and compiled is None:
                why = "is not valid"
            if why is not None:
                raise JSONPathTypeError(str(regexes.refused(args[1].value, why)), token=token)
        return args


class _Path(JSONPath):
    """A compiled path whose filters' regular expressions share ``PATH_REGEX_TIMEOUT_MS`` in one evaluation,
    which is therefore read whole before it is returned."""

    __slots__ = ()

    def finditer(self, data: Any, *, filter_context: Any = None) -> Iterator[JSONPathMatch]:
        token = _REGEX_LEFT.set([PATH_REGEX_TIMEOUT_MS / 1000])
        try:
            return iter(list(super().finditer(data, filter_context=filter_context)))
        finally:
            _REGEX_LEFT.reset(token)


_ENV = _Environment(strict=True)


@lru_cache(maxsize=1024)
def compile_path(path: str) -> JSONPath:
    try:
        compiled = _ENV.compile(path)
    except JSONPathError as exc:
        raise PathError(str(exc).splitlines()[0]) from None
    except RecursionError:
        raise PathError("the path nests too deeply") from None
    if not isinstance(compiled, JSONPath):
        raise PathError("compound paths are not allowed here")
    return _Path(env=_ENV, segments=compiled.segments, pseudo_root=compiled.pseudo_root)


def jsonpath_error(path: str) -> str | None:
    """Why ``path`` is not an RFC 9535 query, or None when it compiles."""
    try:
        compile_path(path)
    except PathError as exc:
        return str(exc)
    return None


def find(path: str, doc: JSON) -> list[tuple[JSON, Location]]:
    """Every node ``path`` selects with its concrete location, in document order."""
    return [(match.value, tuple(match.parts)) for match in compile_path(path).finditer(doc)]


def get_at(doc: JSON, location: Location) -> JSON:
    for step in location:
        doc = doc[step]
    return doc


def set_at(doc: JSON, location: Location, value: JSON) -> JSON:
    """Replace the node at ``location``; returns the (possibly new) root."""
    if not location:
        return value
    get_at(doc, location[:-1])[location[-1]] = value
    return doc


def delete_at(doc: JSON, location: Location) -> None:
    del get_at(doc, location[:-1])[location[-1]]


def child_pattern(pattern: str, name: str) -> str:
    if _IDENTIFIER.fullmatch(name):
        return f"{pattern}.{name}"
    quoted = "".join(_NAME_ESCAPES.get(c) or (f"\\u{ord(c):04x}" if ord(c) < 0x20 else c) for c in name)
    return f"{pattern}['{quoted}']"


def member_pattern(pattern: str) -> str:
    return f"{pattern}[*]"


def pattern_of(location: Location) -> str:
    """Path pattern of a concrete location (``("a", 0, "b")`` -> ``$.a[*].b``)."""
    pattern = ROOT_PATTERN
    for step in location:
        pattern = member_pattern(pattern) if isinstance(step, int) else child_pattern(pattern, step)
    return pattern


def parts_pattern(parts: Iterable[PointerPart]) -> str:
    """Path pattern of a pointer (members and indexes -> ``[*]``, duplicate segments dropped)."""
    pattern = ROOT_PATTERN
    for kind, text in parts:
        if kind == "field":
            pattern = child_pattern(pattern, text)
        elif kind != "dup":
            pattern = member_pattern(pattern)
    return pattern


@lru_cache(maxsize=1024)
def normalise_pattern(user_path: str) -> str:
    """Pattern text of a rule path, the text rule paths and finding patterns compare by. Only names
    and wildcards have one: an index would silently widen to every element, and filters, slices,
    unions and descendants select sets."""
    return _render(user_path, allow_index=False)


def normalise_query(path: str) -> str:
    """Canonical text of a query path of names, wildcards and indexes only; else ``path`` as written."""
    try:
        return _render(path, allow_index=True)
    except PathError:
        return path


def _render(path: str, *, allow_index: bool) -> str:
    pattern = ROOT_PATTERN
    for segment in compile_path(path).segments:
        if not isinstance(segment, JSONPathChildSegment):
            raise PathError("descendant segments (..) are not allowed in a path pattern")
        if len(segment.selectors) != 1:
            raise PathError("a path pattern selects one member per segment; unions are not allowed")
        match segment.selectors[0]:
            case WildcardSelector():
                pattern = member_pattern(pattern)
            case NameSelector(name=name):
                pattern = child_pattern(pattern, name)
            case IndexSelector(index=index) if allow_index:
                pattern = f"{pattern}[{index}]"
            case IndexSelector():
                raise PathError(
                    "array positions are written [*] in a path pattern ([n] would widen to every element)"
                )
            case selector:
                kind = type(selector).__name__.removesuffix("Selector").lower()
                raise PathError(f"{kind} selectors are not allowed in a path pattern")
    return pattern


def member_parts(parts: Parts, member_key: str, index: int, several: bool) -> Parts:
    """A keyed member's parts: its index is its own ``@i`` segment when its key holds several."""
    at = (*parts, ("member", member_key))
    return (*at, ("dup", str(index))) if several else at


def render_pointer(parts: Iterable[PointerPart]) -> str:
    return "".join("/" + _segment(kind, text) for kind, text in parts)


def _segment(kind: str, text: str) -> str:
    if kind == "dup":
        return "@" + text
    if kind == "index":
        return text
    escaped = text.replace("~", "~0").replace("/", "~1")  # RFC 6901
    return "@" + escaped if escaped.startswith("@") else escaped  # a literal leading @ is doubled
