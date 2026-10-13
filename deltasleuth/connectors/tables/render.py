"""Canonical rendering 1 (docs/mvp/ENGINES.md §7, §9): the classes a declared type falls in, and for each
the JSON value PostgreSQL's ``to_jsonb`` gives, built from the text or bytes another engine renders.

An engine renders, its driver carries ``str`` and ``bytes`` only, and this module canonicalises: a row's
``document`` is what the per-key engine compares and what a scan's ``digest`` hashes (§10), so equal
digests mean equal documents. A rendered text that is not of its class's form does not convert
(``ConversionError``, naming the column and the class, never the value).
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from deltasleuth.connectors.tables.keys import Plan
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.convert import ConversionError, convert_row
from deltasleuth.core.types import JSON

RENDERING = 1  # stored in a plan that holds a class: another rendering is another plan
EMPTY = hashlib.sha256(b"{}").digest()  # the row hash of a side whose engine hashes no column
UNCONVERTIBLE = b"unconvertible:"  # a digest no row has: the scan adds its side, so it never matches
RESOURCE_LIMIT = b"resource_limit:"  # a row whose large values stay in its database (§6)

# Every class, by family: only values of one family compare (§7). A class not listed is its own.
_FAMILIES = dict.fromkeys(("text", "char"), "text") | dict.fromkeys(
    ("integer", "decimal", "float", "float32"), "number"
)
KEYS = ("integer", "decimal", "text", "char", "date", "timestamp", "bytes")  # classes with a key text (§8)
# A typeMap entry's family: its converted value's. A mapped timestamp (its Z form) equals only another.
_MAPPED = {"string": "text", "integer": "number", "decimal": "number", "timestamp": "mapped timestamp"}
# What a filter's literal must be for a column of the class (§12 P11); any text for the classes not listed.
LITERALS = dict.fromkeys(("integer", "decimal", "float", "float32"), "a number") | {
    "date": "a date",
    "timestamp": "a timestamp",
    "instant": "a timestamp",
    "time": "a time",
    "bytes": "bytes (\\x and hexadecimal digits)",
    "boolean": "TRUE or FALSE",
}

_INTEGER = re.compile(r"-?[0-9]+")
_NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NOT_FINITE = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIMESTAMP = re.compile(r"([ -]?)([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?")
_TIME = re.compile(r"(-?[0-9]{2,3}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?")


def family(cls: str, spec: Mapping[str, Any] | None = None) -> str:
    """The family of a column of class ``cls``, or of its typeMap entry ``spec`` when it has one."""
    if spec is not None:
        return _MAPPED.get(spec["as"], spec["as"])
    return _FAMILIES.get(cls, cls)


def number(text: str) -> Decimal:
    """An engine's text of a number, exactly; ``ValueError`` when it is not one."""
    if not _NUMBER.fullmatch(text):
        raise ValueError
    try:
        found = Decimal(text)
    except ArithmeticError:  # an exponent Decimal cannot hold
        raise ValueError from None
    if found and abs(found.adjusted()) > jsonio.MAX_EXPONENT:  # as a parsed body's: written out in full
        raise jsonio.ResourceLimitError("number magnitude out of range")
    return found


def _integer(text: str) -> Decimal:
    if not _INTEGER.fullmatch(text):
        raise ValueError
    return Decimal(text)


def _float(text: str) -> JSON:
    """The shortest text that reads back as the same double, as an exact number."""
    if text.lower() in _NOT_FINITE:
        return _NOT_FINITE[text.lower()]
    return _shortest(number(text))


def _float32(text: str) -> JSON:
    """The same for 32 bits: the engine renders the value widened to a double, narrowed here."""
    if text.lower() in _NOT_FINITE:
        return _NOT_FINITE[text.lower()]
    return _shortest32(number(text))


def _shortest(found: Decimal) -> Decimal:
    """The shortest number that reads back as the double ``found`` reads as."""
    return Decimal(repr(float(found)))


def _shortest32(found: Decimal) -> Decimal:
    """The same for the 32-bit float ``found`` reads as; of two such numbers, the nearer to the float."""
    single = _single(found)
    exact = Decimal(single)
    for digits in range(1, 10):  # nine digits tell every 32-bit float apart
        rounded = Decimal(f"{single:.{digits - 1}e}")
        step = Decimal(1).scaleb(rounded.adjusted() - digits + 1)
        # Just above a power of two the nearest text of this length may read back as its neighbour.
        fits = [d for d in (rounded, rounded - step, rounded + step) if _single(d) == single]
        if fits:
            return min(fits, key=lambda d: abs(d - exact))
    raise ValueError


def _single(found: Decimal) -> float:
    """The 32-bit float nearest ``found``, a tie going to the even one. Through a double the number is
    rounded twice: where the double is the midpoint of two 32-bit floats, ``found`` itself says which
    side of it it lies on."""
    wide = float(found)
    near = struct.unpack("f", struct.pack("f", wide))[0]
    other = 2 * wide - near  # the float on the double's other side, when the double is such a midpoint
    if other != near and struct.unpack("f", struct.pack("f", other))[0] == other:
        middle = Decimal(wide)
        if found != middle and (found > middle) == (other > near):
            return other
    return near


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _date(text: str) -> str:
    if not _DATE.fullmatch(text):
        raise ValueError
    return text


def _timestamp(text: str, zone: str = "") -> str:
    """``YYYY-MM-DDTHH:MM:SS[.f]``, trailing zeros dropped; a year before 1 as PostgreSQL writes it."""
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        raise ValueError
    era, moment, fraction = match.groups()
    fraction = (fraction or "").rstrip("0")
    return moment + (f".{fraction}" if fraction else "") + zone + (" BC" if era == "-" else "")


def _time(text: str) -> str:
    match = _TIME.fullmatch(text)
    if match is None:
        raise ValueError
    fraction = (match.group(2) or "").rstrip("0")
    return match.group(1) + (f".{fraction}" if fraction else "")


def _boolean(text: str) -> bool:
    return {"TRUE": True, "FALSE": False}[text]


def _bytes(value: bytes) -> str:
    return "\\x" + value.hex()


def _json(text: str) -> JSON:
    return jsonio.parse_json(text)[0]


# The canonical value of an engine's rendered text or bytes, by class.
_CANONICAL: dict[str, Callable[[Any], JSON]] = {
    "integer": _integer,
    "decimal": number,
    "float": _float,
    "float32": _float32,
    "text": _text,
    "char": lambda text: _text(text).rstrip(" "),
    "boolean": _boolean,
    "date": _date,
    "timestamp": _timestamp,
    "instant": lambda text: _timestamp(text, "+00:00"),
    "time": _time,
    "other": _text,
    "bytes": _bytes,
    "json": _json,
}
# What PostgreSQL's own document lacks of the canonical form, in a pair across engines: only these, each
# for a value of its type (a float that is not finite is a string already). Its text of a float is the
# shortest that reads back alone, which is at times a digit longer than the shortest.
_POSTGRESQL: dict[str, tuple[type, Callable[[Any], JSON]]] = {
    "char": (str, lambda text: text.rstrip(" ")),
    "time": (str, _time),
    "float": (Decimal, _shortest),
    "float32": (Decimal, _shortest32),
}


def value(cls: str, rendered: str | bytes | None, name: str) -> JSON:
    """The canonical value of column ``name``, of class ``cls``, as its engine rendered it."""
    if rendered is None:
        return None
    try:
        return _CANONICAL[cls](rendered)
    except jsonio.ResourceLimitError:
        raise
    except (ValueError, ArithmeticError, KeyError, TypeError, AttributeError, struct.error):
        raise ConversionError(f"column {name!r} does not read as {cls}") from None


def document(side: Plan, names: Sequence[str], rendered: Sequence[str | bytes | None]) -> dict[str, JSON]:
    """A row's canonical document: its compared columns ``names``, from their rendered values."""
    classes, columns = side["classes"], side["columns"]
    return {
        name: value(classes[columns[name]], found, name) for name, found in zip(names, rendered, strict=True)
    }


def parsed(side: Plan, text: str) -> dict[str, JSON]:
    """PostgreSQL's row (``to_jsonb``'s text) as its document; in a pair across engines its ``char``,
    ``time`` and float values take the canonical form. Raises ``ConversionError``, or
    ``jsonio.ResourceLimitError`` for a row beyond a parse limit (both are ValueErrors)."""
    try:
        row, _ = jsonio.parse_json(text)
    except jsonio.JSONParseError:  # its message may quote the text
        raise ConversionError("the row is not JSON") from None
    classes = side.get("classes")
    if classes:
        for name, column in side["columns"].items():
            of, rule = _POSTGRESQL.get(classes[column], (None, None))
            if rule is not None and isinstance(row.get(name), of):
                row[name] = rule(row[name])
    return row


def digest(side: Plan, plain: bytes, row: dict[str, JSON]) -> bytes:
    """A scanned row's digest (§10): the engine's hash of the columns it hashed (``EMPTY`` when it hashed
    none), extended with the worker's columns of ``row`` converted to their typeMap types, as canonical
    JSON. Raises ``ValueError`` for a value that does not convert."""
    converted = jsonio.dumps(convert_row(row, side["types"]), sort_keys=True)
    return hashlib.sha256(plain + converted.encode()).digest()
