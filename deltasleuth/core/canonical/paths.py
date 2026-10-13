"""RFC 9535 paths, path patterns and DeltaSleuth pointers (§6): the one JSONPath implementation
(strict RFC 9535, no python-jsonpath extensions) for specs, targets, connectors and the engine.

Locations are concrete tuples (``("a", 0, "b")``); patterns replace every index or member key
with ``[*]`` (``$.a[*].b``) so rule paths and finding patterns compare by string equality;
pointers are RFC 6901 plus member keys, ``@i`` duplicate segments and a doubled leading ``@``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from jsonpath import JSONPath, JSONPathEnvironment, JSONPathError
from jsonpath.segments import JSONPathChildSegment
from jsonpath.selectors import IndexSelector, NameSelector, WildcardSelector

from deltasleuth.core.canonical.errors import PathError
from deltasleuth.core.types import JSON

Location = tuple[str | int, ...]
PointerPart = tuple[str, str]  # ("field", name) | ("member", key) | ("dup", "i") | ("index", "i")
Parts = tuple[PointerPart, ...]

ROOT_PATTERN = "$"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV = JSONPathEnvironment(strict=True)
# RFC 9535 §2.7 normalised-path escapes for a single-quoted name.
_NAME_ESCAPES = {"'": "\\'", "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


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
    return compiled


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
