"""CEL predicates (notFound.when, absence shapes, compare.valueFilter) and value expressions (a hash
sample's stratum dimensions and sign-off labels).

An evaluation error raises ``PredicateError`` instead of reading as false: each caller decides
what a failing predicate means. Callers bind every variable the spec loader declares for their
kind (``spec.expressions.CEL_VARIABLES``), so an accepted expression always compiles; a ``datetime``
is bound as a CEL timestamp, anything else as JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

from cel_expr_python import cel

from deltasleuth.core.canonical.errors import ExpressionError
from deltasleuth.core.jsonio import cel_data
from deltasleuth.core.types import JSON


class PredicateError(ValueError):
    """A predicate failed at runtime or is not boolean. Carries no payload values."""


@lru_cache(maxsize=512)
def _compile(source: str, variables: tuple[tuple[str, bool], ...]) -> Any:
    """``variables``: (name, whether it is a timestamp) pairs; the others are DYN."""
    types = {name: cel.Type.TIMESTAMP if timestamp else cel.Type.DYN for name, timestamp in variables}
    try:
        return cel.NewEnv(variables=types).compile(source)
    except RuntimeError as exc:  # the binding reports compile errors as RuntimeError
        raise ExpressionError(f"CEL expression does not compile (variables {list(types)})") from exc


def test(source: str, bindings: Mapping[str, Any]) -> bool:
    """Evaluate a boolean predicate over JSON bindings (Decimals allowed). A result that depends on
    digits a double cannot hold raises (``cel_data``): a guess could decide two different values alike."""
    kind, value = _evaluate(source, bindings)
    if kind != cel.Type.BOOL:
        raise PredicateError(f"CEL predicate returned {kind.name()}, not bool")
    return bool(value)


def label(source: str, bindings: Mapping[str, Any]) -> JSON:
    """``test``'s twin for a value expression: its string, boolean, number (a ``Decimal``) or null
    result. Any other result raises, as does one that depends on digits a double cannot hold."""
    kind, value = _evaluate(source, bindings)
    if kind in (cel.Type.STRING, cel.Type.BOOL, cel.Type.NULL):
        return value
    if kind in (cel.Type.INT, cel.Type.UINT, cel.Type.DOUBLE):
        return Decimal(value if isinstance(value, int) else repr(value))
    raise PredicateError(f"CEL expression returned {kind.name()}, not a value")


def _evaluate(source: str, bindings: Mapping[str, Any]) -> tuple[Any, Any]:
    """The result type and value of ``source``, on which every double a bound number may read as agrees."""
    times = {name: value for name, value in bindings.items() if isinstance(value, datetime)}
    json = {name: value for name, value in bindings.items() if name not in times}
    expression = _compile(source, tuple(sorted((name, name in times) for name in bindings)))
    first, *other = (_result(expression, data | times) for data in cel_data(json))
    if other and other[0] != first:
        raise PredicateError("CEL expression depends on digits a double cannot hold")
    return first


def _result(expression: Any, data: dict[str, Any]) -> tuple[Any, Any]:
    try:
        result = expression.eval(data=data)
    except RuntimeError as exc:
        raise PredicateError("CEL evaluation failed") from exc
    if result.type() == cel.Type.ERROR:
        raise PredicateError("CEL evaluation returned an error")
    return result.type(), result.plain_value()


def response_variables(status: int | None, headers: Mapping[str, str], body: JSON) -> dict[str, Any]:
    """Variables of a predicate over one HTTP response."""
    return {"body": body, "status": status, "headers": dict(headers)}
