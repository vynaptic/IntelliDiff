"""Side shaping: RFC 9535 ``select`` then JSONata ``map`` with exact numbers.

Empty data is never a document: a select finding no node or only nulls, and a map yielding
nothing from a non-empty input (an error envelope read through the success map), raise
``MappingError``, so two failed responses never compare as equal documents.

JSONata only knows int/float: it computes in doubles and writes a number as text with 15
significant digits. A number of at most 15 significant digits survives both, and goes in as an
int or, where a double holds it, as a float. Any other number goes in as a stand-in string
(derived from a digest of the input, so data cannot forge it) and is swapped back afterwards.
A stand-in is exact only while moved, so a map that reads one is evaluated again with nearest
doubles and both results must agree.

A map runs inside the shared server process, and a regular expression can backtrack for hours, so every
regular expression literal of a map compiles through ``regex_engine``. It refuses a pattern that does not
compile and one ``regexes`` refuses: too long, too much compilation work, or a repeat inside a repeat
(``RegexRefused``). The pattern then runs on the ``regex`` engine, which stops at the map's time bound,
written so that it matches exactly what Python's ``re`` matches: each ``\\d``, ``\\s``, ``\\w`` and ``\\b``
spelled out as the code points ``re`` gives it, and a case-insensitive character as the characters ``re``
folds to it. A pattern that cannot be written so (``\\B``, a back-reference, a conditional, a scoped
``(?a:...)``) would run on ``re`` without a time bound, so it is refused too. A pattern reads at most
``MAX_REGEX_INPUT`` characters of one string: a longer one fails the side.
"""

from __future__ import annotations

import math
import re
import sys
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from decimal import Decimal
from functools import cache, lru_cache, partial
from re import _compiler, _parser
from re import _constants as _sre
from typing import Any

import jsonata
import regex
from jsonata.regex_engine import RegexFlags
from jsonata.utils import Utils

from deltasleuth.core.canonical.encoding import exact_json, sha256_hex
from deltasleuth.core.canonical.errors import ExpressionError, MappingError
from deltasleuth.core.canonical.paths import find
from deltasleuth.core.canonical.regexes import (
    MAX_REGEX_INPUT,
    RegexRefused,
    parse,
    refused,
    shown,
    too_long,
    unbounded,
)
from deltasleuth.core.jsonio import number_text
from deltasleuth.core.types import JSON

_MAP_EMPTY = "map produced an empty document"
# A runaway map (a huge range, deep recursion) fails the side instead of holding the process;
# real maps take milliseconds.
MAP_TIMEOUT_MS = 2000
MAP_MAX_DEPTH = 500
# The significant digits JSONata keeps when it writes a number as text; a double holds them all.
EXACT_DIGITS = 15
_DEADLINE: ContextVar[float | None] = ContextVar("map_deadline", default=None)  # the running map's


def apply_select(select: str, doc: JSON) -> JSON:
    """The node ``select`` finds, or a list when it finds several."""
    nodes = [value for value, _ in find(select, doc)]
    if all(node is None for node in nodes):  # also true when nothing matched
        raise MappingError("select matched no data")
    return nodes[0] if len(nodes) == 1 else nodes


@lru_cache(maxsize=256)
def _compiled(expression: str) -> Any:
    try:
        compiled = jsonata.Jsonata(expression, regex_engine)
    except RegexRefused:
        raise
    except Exception as exc:  # the parser raises JException or plain Python errors
        raise ExpressionError(f"JSONata expression does not compile: {_code(exc)}") from exc
    compiled.set_output_convert_nulls(False)  # keep JSONata null distinct from undefined
    return compiled


def apply_map(expression: str, node: JSON) -> JSON:
    compiled = _compiled(expression)
    codec = _Codec(node)
    result = _evaluate(compiled, _to_jsonata(node, codec.stand_in))
    if result is None or result is Utils.NULL_VALUE:
        raise MappingError(_MAP_EMPTY)
    out = codec.decode(result)
    if codec.held and not codec.agrees(result, _evaluate(compiled, _to_jsonata(node, float))):
        raise MappingError("map expression computed with a high-precision number (only moving it is exact)")
    if not _has_value(out) and node not in (None, {}, []):  # an empty input may map to nothing
        raise MappingError(_MAP_EMPTY)
    return out


def _evaluate(compiled: Any, data: Any) -> Any:
    # A fresh bindings frame per call keeps "$" out of the cached expression's shared environment,
    # so concurrent evaluations cannot see each other's input, and starts this call's time bound.
    frame = compiled.create_frame()
    frame.set_runtime_bounds(MAP_TIMEOUT_MS, MAP_MAX_DEPTH)
    deadline = _DEADLINE.set(time.monotonic() + MAP_TIMEOUT_MS / 1000)  # its regular expressions'
    try:
        return compiled.evaluate(data, frame)
    except MappingError:  # a regular expression's bound
        raise
    except Exception as exc:  # JException (D1012 timeout, D1011 depth), ZeroDivisionError, ...
        raise MappingError(f"map expression failed: {_code(exc)}") from exc
    finally:
        _DEADLINE.reset(deadline)


def _code(exc: Exception) -> str:
    """Error code or class name only: JSONata messages can quote input values."""
    code = getattr(exc, "error", None)
    return code if isinstance(code, str) and code else exc.__class__.__name__


def _has_value(node: JSON) -> bool:
    """Whether ``node`` holds a non-null scalar."""
    if isinstance(node, dict):
        return any(_has_value(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_value(v) for v in node)
    return node is not None


def _significant(value: Decimal) -> int:
    """The significant digits of ``value``: its coefficient's, without trailing zeros."""
    digits = value.as_tuple().digits
    count = len(digits)
    while count > 1 and digits[count - 1] == 0:
        count -= 1
    return count


def _to_jsonata(value: JSON, high_precision: Callable[[Decimal], Any]) -> Any:
    if isinstance(value, Decimal):
        if _significant(value) > EXACT_DIGITS:
            return high_precision(value)
        if value == value.to_integral_value():
            return int(value)
        as_float = float(value)
        return as_float if Decimal(repr(as_float)) == value else high_precision(value)
    if isinstance(value, dict):
        return {k: _to_jsonata(v, high_precision) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonata(v, high_precision) for v in value]
    return value


class _Codec:
    """Stand-ins for the high-precision numbers of one input."""

    def __init__(self, node: JSON) -> None:
        self._node = node
        self._prefix: str | None = None  # derived on first use: most inputs never need one
        self._tokens: dict[str, str] = {}  # number text -> stand-in (equal numbers share one)
        self.held: dict[str, Decimal] = {}  # stand-in -> number

    def stand_in(self, value: Decimal) -> str:
        text = number_text(value)
        if text not in self._tokens:
            if self._prefix is None:
                self._prefix = f"\x00ds-number:{sha256_hex(exact_json(self._node))[:16]}:"
            token = f"{self._prefix}{len(self._tokens)}\x00"
            self._tokens[text] = token
            self.held[token] = value
        return self._tokens[text]

    def decode(self, value: Any) -> JSON:
        if value is Utils.NULL_VALUE:
            return None
        if value is None or isinstance(value, bool):  # a nested None is an input null
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise MappingError("map expression produced a non-finite number")
            return Decimal(repr(value))
        if isinstance(value, str):
            return self.held[value] if value in self.held else self._plain(value, "text")
        if isinstance(value, dict):
            return {self._plain(k, "a member name"): self.decode(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        raise MappingError(f"map expression produced a non-JSON value ({type(value).__name__})")

    def _plain(self, text: str, where: str) -> str:
        if self._prefix is not None and self._prefix in text:
            raise MappingError(f"map expression used a high-precision number as {where}")
        return text

    def agrees(self, exact: Any, approximate: Any) -> bool:
        """Whether the nearest-double run moved every stand-in's number alike."""
        if isinstance(exact, str) and exact in self.held:
            return type(approximate) in (int, float) and approximate == float(self.held[exact])
        if isinstance(exact, dict):
            return (
                isinstance(approximate, dict)
                and exact.keys() == approximate.keys()
                and all(self.agrees(v, approximate[k]) for k, v in exact.items())
            )
        if isinstance(exact, list):
            return (
                isinstance(approximate, list)
                and len(exact) == len(approximate)
                and all(map(self.agrees, exact, approximate))
            )
        return type(exact) is type(approximate) and exact == approximate


# -- regular expressions ----------------------------------------------------------------------------------


class _Unwritable(Exception):
    """A construct the ``regex`` engine cannot be given with ``re``'s meaning: its message names it."""


def regex_engine(pattern: str, flags: RegexFlags) -> _Bounded:
    """JSONata's regular expression engine for maps (``jsonata.Jsonata(expression, regex_engine)``)."""
    case = re.IGNORECASE if flags.case_insensitive else 0
    return _bounded(pattern, case | (re.MULTILINE if flags.multiline else 0))


@lru_cache(maxsize=256)
def _bounded(pattern: str, flags: int) -> _Bounded:
    if why := too_long(pattern):
        raise refused(pattern, why)
    try:
        compiled = re.compile(pattern, flags)
        parsed = parse(pattern, flags)
    except (re.error, OverflowError, RecursionError) as exc:
        raise RegexRefused(f"regular expression {shown(pattern)} is not valid: {exc}") from None
    if why := unbounded(parsed):
        raise refused(pattern, why)
    try:
        return _Bounded(compiled, _native(parsed))
    except _Unwritable as exc:
        rewrite = "rewrite it without \\B, back-references, conditionals or scoped flags"
        why = f"uses {exc}, which runs without a time bound: {rewrite}"
        raise RegexRefused(f"regular expression {shown(pattern)} {why}") from None


class _Bounded:
    """A map's compiled regular expression, as JSONata calls one (``jsonata.regex_engine.CompiledPattern``):
    the pattern on the ``regex`` engine, and ``re``'s, which reads a replacement template."""

    __slots__ = ("_native", "_re")

    def __init__(self, compiled: re.Pattern[str], native: regex.Pattern[str]) -> None:
        self._re, self._native = compiled, native

    def search(self, string: str) -> Any:
        return self._run(lambda p, **bound: p.search(string, **bound), string)

    def finditer(self, string: str) -> Any:
        return iter(self._run(lambda p, **bound: list(p.finditer(string, **bound)), string))

    def sub(self, repl: Any, string: str, count: int = 0) -> str:
        if isinstance(repl, str):  # read as re reads a template, with its errors, which JSONata parses
            repl = partial(_expand, _parser.parse_template(repl, self._re))
        return self._run(lambda p, **bound: p.sub(repl, string, count, **bound), string)

    def split(self, string: str, maxsplit: int = 0) -> list[str]:
        return self._run(lambda p, **bound: p.split(string, maxsplit, **bound), string)

    def _run(self, call: Callable[..., Any], string: str) -> Any:
        if len(string) > MAX_REGEX_INPUT:
            limit = f"{MAX_REGEX_INPUT:,} characters"
            raise MappingError(f"map regular expression read a string longer than {limit}")
        deadline = _DEADLINE.get()
        left = MAP_TIMEOUT_MS / 1000 if deadline is None else deadline - time.monotonic()
        try:
            if left <= 0:
                raise TimeoutError
            return call(self._native, concurrent=True, timeout=left)
        except TimeoutError:
            bound = f"the map's time bound ({MAP_TIMEOUT_MS} ms)"
            raise MappingError(f"map regular expression ran past {bound}") from None


def _expand(template: list[Any], match: Any) -> str:
    """A template ``_parser.parse_template`` read (texts and group numbers) filled from ``match``."""
    return "".join(part if isinstance(part, str) else (match.group(part) or "") for part in template)


def _native(parsed: _parser.SubPattern) -> regex.Pattern[str]:
    """The pattern on the ``regex`` engine, matching exactly what ``re`` matches; ``_Unwritable`` where it
    cannot be."""
    flags = parsed.state.flags
    names = {number: name for name, number in parsed.state.groupdict.items()}
    source = _written(parsed, flags & (re.IGNORECASE | re.ASCII), names)
    native = regex.VERSION0 | (regex.MULTILINE if flags & re.MULTILINE else 0)
    try:
        return regex.compile(source, native | (regex.DOTALL if flags & re.DOTALL else 0), cache_pattern=False)
    except regex.error:
        raise _Unwritable("a construct the bounded engine does not read") from None


_ANCHORS = {
    _sre.AT_BEGINNING: "^", _sre.AT_BEGINNING_STRING: r"\A", _sre.AT_END: "$", _sre.AT_END_STRING: r"\Z",
}  # fmt: skip
_LOOK = {
    (_sre.ASSERT, 1): "(?=", (_sre.ASSERT, -1): "(?<=",
    (_sre.ASSERT_NOT, 1): "(?!", (_sre.ASSERT_NOT, -1): "(?<!",
}  # fmt: skip
_SUFFIX = {_sre.MAX_REPEAT: "", _sre.MIN_REPEAT: "?", _sre.POSSESSIVE_REPEAT: "+"}
_SCOPED = ((re.MULTILINE, "m"), (re.DOTALL, "s"))
_SINGLE = (_sre.LITERAL, _sre.NOT_LITERAL, _sre.IN)  # the operations that match one character
# What the parser gives that the regex engine cannot be given with re's meaning: \B (re never matches it in
# an empty string), a back-reference and a conditional.
_UNWRITABLE = {_sre.AT: "\\B", _sre.GROUPREF: "a back-reference", _sre.GROUPREF_EXISTS: "a conditional"}


def _written(nodes: Sequence[Any], flags: int, names: dict[int, str]) -> str:
    """Parsed ``nodes`` as ``regex`` source of the same meaning, under ``re``'s ``IGNORECASE`` and ``ASCII``
    as ``flags`` holds them. A character case-insensitive ``re`` matches is written as the set it matches."""
    out = []
    for op, value in nodes:
        if op in _SINGLE and flags & re.IGNORECASE:
            out.append(_set(_folded(op, tuple(value) if op == _sre.IN else value, flags), False))
        elif op == _sre.LITERAL:
            out.append(_char(value))
        elif op == _sre.NOT_LITERAL:
            out.append(_set([(value, value)], True))
        elif op == _sre.ANY:
            out.append(".")
        elif op == _sre.IN:
            out.append(_class(value, bool(flags & re.ASCII)))
        elif op == _sre.AT and value in _ANCHORS:
            out.append(_ANCHORS[value])
        elif op == _sre.AT and value == _sre.AT_BOUNDARY:  # re's \w on one side only, whatever the case
            word = _class([(_sre.CATEGORY, _sre.CATEGORY_WORD)], bool(flags & re.ASCII))
            out.append(f"(?:(?<={word})(?!{word})|(?<!{word})(?={word}))")
        elif op in _SUFFIX:
            low, high, child = value
            if high == _sre.MAXREPEAT:
                count = {0: "*", 1: "+"}.get(low, f"{{{low},}}")
            else:
                count = "?" if (low, high) == (0, 1) else f"{{{low},{high}}}"
            out.append(f"(?:{_written(child, flags, names)}){count}{_SUFFIX[op]}")
        elif op == _sre.SUBPATTERN:
            number, added, removed, child = value
            if added & (re.ASCII | re.UNICODE):  # re reads a scoped (?a:\w) as neither class
                raise _Unwritable("a scoped (?a:...) or (?u:...) flag")
            inner = _written(child, (flags | (added & re.IGNORECASE)) & ~removed, names)
            on = "".join(letter for flag, letter in _SCOPED if added & flag)
            off = "".join(letter for flag, letter in _SCOPED if removed & flag)
            if on or off:
                inner = f"(?{on}{'-' + off if off else ''}:{inner})"
            if number is None:
                out.append(f"(?:{inner})")
            else:
                out.append(f"(?P<{names[number]}>{inner})" if number in names else f"({inner})")
        elif op == _sre.BRANCH:
            out.append("(?:" + "|".join(_written(branch, flags, names) for branch in value[1]) + ")")
        elif op in (_sre.ASSERT, _sre.ASSERT_NOT):
            out.append(_LOOK[op, value[0]] + _written(value[1], flags, names) + ")")
        elif op == _sre.ATOMIC_GROUP:
            out.append(f"(?>{_written(value, flags, names)})")
        else:
            raise _Unwritable(_UNWRITABLE.get(op, "a construct the bounded engine does not read"))
    return "".join(out)


def _char(point: int) -> str:
    """A code point as ``regex`` source: a letter or digit as itself (no engine reads one as syntax), any
    other by its number."""
    return chr(point) if chr(point).isalnum() else f"\\U{point:08x}"


def _class(items: Sequence[Any], ascii: bool) -> str:
    negated, ranges = False, []
    for op, value in items:
        if op == _sre.NEGATE:
            negated = True
        elif op == _sre.LITERAL:
            ranges.append((value, value))
        elif op == _sre.RANGE:
            ranges.append(value)
        elif op == _sre.CATEGORY and value in _ESCAPES:
            ranges += _code_points(*_ESCAPES[value], ascii)
        else:
            raise _Unwritable("a class item the bounded engine does not read")
    return _set(ranges, negated)


def _set(ranges: Sequence[tuple[int, int]], negated: bool) -> str:
    """Code points as a class that is never negated: the ``regex`` engine misreads an alternation of
    negated classes (``[^a]|[^b]`` does not match ``a``), so a negated one is written as its complement."""
    merged: list[tuple[int, int]] = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
        else:
            merged.append((low, high))
    written = _gaps(merged) if negated else merged
    if not written:
        return "(?!)"
    return "[" + "".join(_char(lo) if lo == hi else f"{_char(lo)}-{_char(hi)}" for lo, hi in written) + "]"


def _gaps(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """The code points sorted, disjoint ``ranges`` leave out."""
    gaps, start = [], 0
    for low, high in ranges:
        gaps += [(start, low - 1)] if low > start else []
        start = high + 1
    return gaps + ([(start, sys.maxunicode)] if start <= sys.maxunicode else [])


_ESCAPES = {
    _sre.CATEGORY_DIGIT: (r"\d", False), _sre.CATEGORY_NOT_DIGIT: (r"\d", True),
    _sre.CATEGORY_SPACE: (r"\s", False), _sre.CATEGORY_NOT_SPACE: (r"\s", True),
    _sre.CATEGORY_WORD: (r"\w", False), _sre.CATEGORY_NOT_WORD: (r"\w", True),
}  # fmt: skip


@cache
def _code_points(escape: str, complement: bool, ascii: bool) -> tuple[tuple[int, int], ...]:
    """The code points ``re`` matches with a class escape, or those it does not, as sorted ranges, asked of
    ``re`` itself."""
    if complement:
        return tuple(_gaps(_code_points(escape, False, ascii)))
    runs: list[list[int]] = []
    for point in sorted({ord(c) for c in re.findall(escape, _every(), re.ASCII if ascii else 0)}):
        if runs and runs[-1][1] == point - 1:
            runs[-1][1] = point
        else:
            runs.append([point, point])
    return tuple((low, high) for low, high in runs)


@lru_cache(maxsize=4096)
def _folded(op: Any, value: Any, flags: int) -> tuple[tuple[int, int], ...]:
    """The code points one parsed character operation matches under ``flags``, case-insensitive among them,
    as ranges, asked of ``re`` itself: its case folding is its own (the Kelvin sign is a k, ß is no ss)."""
    state = _parser.State()
    state.flags = flags | (0 if flags & re.ASCII else re.UNICODE)
    node = _parser.SubPattern(state, [(op, list(value) if op == _sre.IN else value)])
    found = {ord(c) for c in _compiler.compile(node, state.flags).findall(_every())}
    return tuple((point, point) for point in sorted(found))


@cache
def _every() -> str:
    """Every code point, in order: what a character class is read over."""
    return "".join(map(chr, range(sys.maxunicode + 1)))
