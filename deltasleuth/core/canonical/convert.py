"""A column's declared canonical type (a table side's ``typeMap``).

``convert`` turns one JSON value, as a database renders it, into the canonical form of its type, or
raises ``ConversionError``, whose message names the type, never the value. NULL stays null.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from deltasleuth.core import jsonio
from deltasleuth.core.types import JSON

_TRUE = {"t", "true", "y", "yes", "on", "1"}  # PostgreSQL's boolean words
_FALSE = {"f", "false", "n", "no", "off", "0"}
_HEX = re.compile(r"\\x(?:[0-9a-fA-F]{2})*")
# A number's text: ASCII digits, a sign, one point, an exponent. Decimal() itself also reads underscores
# between digits and the digits of every script.
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_EXACT = Context(prec=2 * jsonio.MAX_EXPONENT + 100)  # rounding to a scale never loses other digits


class ConversionError(ValueError):
    """The value is not a value of its declared type."""


def convert(value: JSON, spec: Mapping[str, Any]) -> JSON:
    """``value`` as its type ``spec["as"]`` says (the schema's typeSpec)."""
    if value is None:
        return None
    kind = spec["as"]
    try:
        return _CONVERTERS[kind](value, spec)
    except (ValueError, ArithmeticError, TypeError):  # decimal.InvalidOperation is an ArithmeticError
        raise ConversionError(f"does not convert to {kind}") from None


def convert_text(text: str, types: Mapping[str, Mapping[str, Any]]) -> dict[str, JSON]:
    """A row's JSON text with each member ``types`` names converted. Raises ``ConversionError``, or
    ``jsonio.ResourceLimitError`` for a row beyond a parse limit (both are ValueErrors)."""
    try:
        row, _ = jsonio.parse_json(text)
    except jsonio.JSONParseError:  # its message may quote the text
        raise ConversionError("the row is not JSON") from None
    return convert_row(row, types)


def convert_row(doc: dict[str, JSON], types: Mapping[str, Mapping[str, Any]]) -> dict[str, JSON]:
    """``doc`` with each member ``types`` names converted; raises ``ConversionError`` naming it."""
    out = dict(doc)
    for name, spec in types.items():
        try:
            out[name] = convert(doc.get(name), spec)
        except ConversionError as exc:
            raise ConversionError(f"column {name!r} {exc}") from None
    return out


def _text(value: JSON) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return jsonio.number_text(value)
    if isinstance(value, str):
        return value
    return jsonio.dumps(value, sort_keys=True)


def _decimal(value: JSON, spec: Mapping[str, Any]) -> Decimal:
    """A number, or its text; with ``decimalSeparator`` a text holding the other
    separator does not convert, and no group separator is read: neither a space, an apostrophe or the other
    separator, nor an underscore; nor any digit but 0 to 9."""
    if isinstance(value, bool) or not isinstance(value, Decimal | str):
        raise ValueError
    if isinstance(value, str) and (separator := spec.get("decimalSeparator")):
        if ("," if separator == "." else ".") in value:
            raise ValueError
        value = value.replace(separator, ".")
    if isinstance(value, str) and not _NUMBER.fullmatch(value.strip()):
        raise ValueError
    number = value if isinstance(value, Decimal) else Decimal(value.strip())
    if not number.is_finite():
        raise ValueError
    return number


def _integer(value: JSON, spec: Mapping[str, Any]) -> Decimal:
    number = _decimal(value, spec)
    if number != number.to_integral_value():
        raise ValueError
    return number.to_integral_value()


def _number(value: JSON, spec: Mapping[str, Any]) -> Decimal:
    number = _decimal(value, spec)
    scale = spec.get("scale")
    return number if scale is None else number.quantize(Decimal(1).scaleb(-scale), ROUND_HALF_EVEN, _EXACT)


def _boolean(value: JSON, spec: Mapping[str, Any]) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value)
    if "trueValues" in spec or "falseValues" in spec:
        truthy, falsy = ({str(v) for v in spec.get(k, ())} for k in ("trueValues", "falseValues"))
    else:
        text, truthy, falsy = text.strip().lower(), _TRUE, _FALSE
    if text in truthy:
        return True
    if text in falsy:
        return False
    raise ValueError


def _instant(value: JSON, spec: Mapping[str, Any]) -> datetime:
    """An aware datetime: a naive value (or a date, at midnight) is read in ``naiveTimezone``; with
    ``format`` the text is parsed whole by it."""
    if not isinstance(value, str):
        raise ValueError
    zone = ZoneInfo(spec.get("naiveTimezone", "UTC"))
    if "format" in spec:
        moment = datetime.strptime(value, spec["format"])
        return moment if moment.tzinfo else moment.replace(tzinfo=zone)
    text = value.strip().replace(" ", "T", 1)
    if "T" not in text:
        return datetime.combine(date.fromisoformat(text), time(), zone)
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def _date(value: JSON, spec: Mapping[str, Any]) -> str:
    moment = _instant(value, spec)
    return moment.astimezone(ZoneInfo(spec.get("naiveTimezone", "UTC"))).date().isoformat()


def _timestamp(value: JSON, spec: Mapping[str, Any]) -> str:
    moment = _instant(value, spec).astimezone(UTC)
    digits = min(int(spec.get("precision", 6)), 6)  # PostgreSQL keeps microseconds: 7 to 9 keep them all
    micro = moment.microsecond // 10 ** (6 - digits) * 10 ** (6 - digits)
    fraction = f".{micro:06d}"[: digits + 1] if digits else ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def _json(value: JSON, spec: Mapping[str, Any]) -> JSON:
    if not isinstance(value, str):
        return value
    parsed, duplicate = jsonio.parse_json(value)
    if duplicate:
        raise ValueError
    return parsed


def _bytes(value: JSON, spec: Mapping[str, Any]) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError
    return value.lower()


def _string(value: JSON, spec: Mapping[str, Any]) -> str | None:
    text = _text(value)
    return None if text == "" and spec.get("emptyAsNull") else text  # a database that stores '' as NULL


_CONVERTERS = {
    "string": _string,
    "integer": _integer,
    "decimal": _number,
    "boolean": _boolean,
    "date": _date,
    "timestamp": _timestamp,
    "json": _json,
    "bytes": _bytes,
}
