"""JSON input/output with exact numbers.

* ``parse_json`` turns response bytes into JSON values where every number is a ``Decimal``
  (never float), detects duplicate member names (RFC 8259 leaves their meaning undefined, so
  the caller marks the side INCOMPLETE(duplicate_member_name)), and enforces resource limits.
* ``cel_data`` / ``to_cel_value`` convert JSON values for the CEL runtime (which has no Decimal
  type), bracketing a number no double equals so that callers can fail closed.
* ``to_serialisable`` / ``dumps`` render any engine value (including ``KeyedMap`` and
  ``Counted``) as plain JSON for reports and evidence.
* ``plain`` renders a store row's times and addresses as the text every answer shows them as.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from deltasleuth.core.types import JSON, Counted, KeyedMap

DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # a larger body is INCOMPLETE(resource_limit)
DEFAULT_MAX_DEPTH = 256
MAX_EXPONENT = 4000  # |adjusted exponent| above this is rejected (avoids huge expansions)


class JSONParseError(ValueError):
    """The body is not valid UTF-8 JSON (side state INCOMPLETE(unparseable))."""


class LoneSurrogateError(JSONParseError):
    """A string (a value or a member name) holds a lone UTF-16 surrogate: a ``\\uD800``-``\\uDFFF``
    escape without its pair.

    RFC 8259 §8.2 leaves the meaning of such a string unpredictable and I-JSON (RFC 7493 §2.1)
    forbids it: it is not Unicode text, so it has no UTF-8 form, canonical form, hash or CEL
    value (the CEL runtime crashed the interpreter on one). Rejecting it here protects every
    consumer of parsed bodies, not only the ones that happen to encode them.
    """


class ResourceLimitError(ValueError):
    """The body exceeds a size, depth or number-magnitude limit (INCOMPLETE(resource_limit))."""


def _parse_number(text: str) -> Decimal:
    try:
        d = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - json only hands us valid numerals
        raise JSONParseError(f"invalid number {text!r}") from exc
    if d.is_finite() and d != 0 and abs(d.adjusted()) > MAX_EXPONENT:
        raise ResourceLimitError(f"number magnitude out of range: exponent {d.adjusted()}")
    return d


def _reject_constant(name: str) -> Any:
    raise JSONParseError(f"{name} is not valid JSON")


def _depth(value: Any, limit: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        v, d = stack.pop()
        if d > limit:
            raise ResourceLimitError(f"nesting deeper than {limit}")
        if isinstance(v, dict):
            stack.extend((x, d + 1) for x in v.values())
        elif isinstance(v, list):
            stack.extend((x, d + 1) for x in v)


# UTF-8 cannot encode a surrogate, so in bytes one can only come from a \uD800-\uDFFF escape;
# a body without such an escape (the usual case) is not walked.
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F]")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _has_lone_surrogate(value: JSON) -> bool:
    # The JSON decoder joins an escaped pair into one character, so any surrogate left is lone.
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if _SURROGATE.search(node):
                return True
        elif isinstance(node, dict):
            stack.extend(node)  # member names
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def parse_json(
    body: bytes | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> tuple[JSON, bool]:
    """Parse ``body``; return ``(value, duplicate_member_found)``.

    Raises ``JSONParseError`` for invalid JSON or non-UTF-8 bytes (``LoneSurrogateError`` for a
    string that is not Unicode text) and ``ResourceLimitError`` for oversized input. Numbers
    are ``Decimal``; ``NaN``/``Infinity`` are rejected.
    """
    try:
        raw = body.encode("utf-8") if isinstance(body, str) else body
    except UnicodeEncodeError:  # only a surrogate code point has no UTF-8 form
        raise LoneSurrogateError("the text holds a lone UTF-16 surrogate (not Unicode text)") from None
    if len(raw) > max_bytes:
        raise ResourceLimitError(f"body of {len(raw)} bytes exceeds {max_bytes}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JSONParseError(f"body is not UTF-8: {exc}") from exc
    if text.startswith("\ufeff"):
        raise JSONParseError("byte order mark is not allowed (RFC 8259 §8.1)")

    duplicate = False

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                duplicate = True
            out[k] = v
        return out

    try:
        value = json.loads(
            text,
            parse_float=_parse_number,
            parse_int=_parse_number,
            parse_constant=_reject_constant,
            object_pairs_hook=pairs_hook,
        )
    except RecursionError as exc:
        raise ResourceLimitError("nesting too deep for the parser") from exc
    except (ResourceLimitError, JSONParseError):
        raise
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise JSONParseError(str(exc)) from exc
    _depth(value, max_depth)
    if _SURROGATE_ESCAPE.search(raw) and _has_lone_surrogate(value):
        raise LoneSurrogateError("a string holds a lone UTF-16 surrogate escape (not Unicode text)")
    return value, duplicate


def cel_data(bindings: Mapping[str, JSON]) -> list[dict[str, Any]]:
    """CEL data for JSON ``bindings``: ``[data]``, or ``[data, other]`` when a bound number has no
    equal double. ``data`` holds such a number as its nearest double and ``other`` as the adjacent
    double on the number's other side, so a result that differs between the two depends on digits
    a double drops and the caller fails closed (CEL has no decimals)."""
    inexact = False

    def nearest(d: Decimal) -> float:
        nonlocal inexact
        inexact = True
        return float(d)

    data = {k: to_cel_value(v, nearest) for k, v in bindings.items()}
    return [data, {k: to_cel_value(v, _other_double) for k, v in bindings.items()}] if inexact else [data]


def _other_double(d: Decimal) -> float:
    nearest = float(d)  # a double reads as its shortest decimal, as a CEL literal is written
    return math.nextafter(nearest, math.inf if Decimal(repr(nearest)) < d else -math.inf)


def to_cel_value(value: JSON, inexact: Callable[[Decimal], float] = float) -> Any:
    """Convert a JSON value for CEL: an int64 integer -> int, a number that is the shortest decimal
    of a double -> that double, any other number -> ``inexact(number)``."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value() and -(2**63) <= value < 2**63:
            return int(value)
        as_float = float(value)
        return as_float if Decimal(repr(as_float)) == value else inexact(value)
    if isinstance(value, Counted):
        return {"value": to_cel_value(value.value, inexact), "count": value.n}
    if isinstance(value, KeyedMap):
        return [to_cel_value(m, inexact) for members in value.values() for m in members]
    if isinstance(value, dict):
        return {str(k): to_cel_value(v, inexact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_cel_value(v, inexact) for v in value]
    if isinstance(value, (int, float)):
        return value
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def number_text(d: Decimal) -> str:
    """Canonical text of a number: exact (no context rounding), no exponent, ``-0`` -> ``0``."""
    if not d.is_finite():
        raise ValueError(f"not a finite JSON number: {d}")
    sign, digits, exp = d.as_tuple()
    ds = "".join(map(str, digits)).lstrip("0")
    if not ds:
        return "0"
    exp = int(exp)
    stripped = ds.rstrip("0")
    exp += len(ds) - len(stripped)
    ds = stripped
    if exp >= 0:
        body = ds + "0" * exp
    elif -exp >= len(ds):
        body = "0." + "0" * (-exp - len(ds)) + ds
    else:
        body = ds[:exp] + "." + ds[exp:]
    return ("-" if sign else "") + body


def to_serialisable(value: Any, _num: Any = None) -> Any:
    """Plain-JSON rendering of engine values. Decimals become ``_num(text)`` if given, else str."""
    conv = _num or number_text
    if isinstance(value, Decimal):
        return conv(value) if _num is None else _num(number_text(value))
    if isinstance(value, Counted):
        return {"value": to_serialisable(value.value, _num), "count": value.n}
    if isinstance(value, KeyedMap):
        return {str(k): [to_serialisable(m, _num) for m in ms] for k, ms in sorted(value.items())}
    if isinstance(value, dict):
        return {str(k): to_serialisable(v, _num) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_serialisable(v, _num) for v in value]
    return value


def plain(value: Any) -> Any:
    """``value`` with every date and time as ISO-8601 text and every IP address as its text, as the API's
    answers and an MCP tool's result show them (docs/mvp/MCP.md §7)."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):  # an inet column
        return str(value)
    if isinstance(value, dict):
        return {name: plain(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    return value


# A number's placeholder string; the nonce keeps a string that merely looks like one intact.
_PLACEHOLDER = re.compile(r'"\\u0000([0-9a-f]{16}):(-?[0-9]+(?:\.[0-9]+)?)\\u0000"')


def dumps(value: Any, *, indent: int | None = None, sort_keys: bool = False) -> str:
    """Serialise any engine value to JSON text with exact numbers (no float rounding)."""
    nonce = secrets.token_hex(8)

    def placeholder(text: str) -> str:
        return f"\x00{nonce}:{text}\x00"

    text = json.dumps(
        to_serialisable(value, placeholder),
        indent=indent,
        sort_keys=sort_keys,
        ensure_ascii=False,
        allow_nan=False,
    )
    return _PLACEHOLDER.sub(lambda m: m.group(2) if m.group(1) == nonce else m.group(0), text)
