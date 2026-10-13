"""Side shaping: RFC 9535 ``select`` then JSONata ``map`` with exact numbers.

Empty data is never a document: a select finding no node or only nulls, and a map yielding
nothing from a non-empty input (an error envelope read through the success map), raise
``MappingError``, so two failed responses never compare as equal documents.

JSONata only knows int/float. Integral Decimals go in as ints and Decimals a double holds as
floats; any other number goes in as a stand-in string (derived from a digest of the input, so
data cannot forge it) and is swapped back afterwards. A stand-in is exact only while moved, so
a map that reads one is evaluated again with nearest doubles and both results must agree.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal
from functools import lru_cache
from typing import Any

import jsonata
from jsonata.utils import Utils

from deltasleuth.core.canonical.encoding import exact_json, sha256_hex
from deltasleuth.core.canonical.errors import ExpressionError, MappingError
from deltasleuth.core.canonical.paths import find
from deltasleuth.core.jsonio import number_text
from deltasleuth.core.types import JSON

_MAP_EMPTY = "map produced an empty document"
# A runaway map (a huge range, deep recursion) fails the side instead of holding the process;
# real maps take milliseconds.
MAP_TIMEOUT_MS = 2000
MAP_MAX_DEPTH = 500


def apply_select(select: str, doc: JSON) -> JSON:
    """The node ``select`` finds, or a list when it finds several."""
    nodes = [value for value, _ in find(select, doc)]
    if all(node is None for node in nodes):  # also true when nothing matched
        raise MappingError("select matched no data")
    return nodes[0] if len(nodes) == 1 else nodes


@lru_cache(maxsize=256)
def _compiled(expression: str) -> Any:
    try:
        compiled = jsonata.Jsonata(expression)
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
    try:
        return compiled.evaluate(data, frame)
    except Exception as exc:  # JException (D1012 timeout, D1011 depth), ZeroDivisionError, ...
        raise MappingError(f"map expression failed: {_code(exc)}") from exc


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


def _to_jsonata(value: JSON, high_precision: Callable[[Decimal], Any]) -> Any:
    if isinstance(value, Decimal):
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
