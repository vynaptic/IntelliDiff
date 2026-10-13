from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deltasleuth.core.canonical import predicates
from deltasleuth.core.canonical.errors import ExpressionError


def test_boolean_predicates_over_exact_numbers() -> None:
    assert predicates.test("status == 404", {"status": 404}) is True
    assert predicates.test("body.n > 1 && body.i == 3", {"body": {"n": Decimal("1.5"), "i": Decimal(3)}})
    assert predicates.test("has(body.error) && body.error == 'nf'", {"body": {}}) is False


@pytest.mark.parametrize("source", ["body.secret_field.x == 1", "1 + 1"])
def test_a_runtime_error_or_non_boolean_raises_without_values(source) -> None:
    with pytest.raises(predicates.PredicateError) as info:
        predicates.test(source, {"body": {"secret_field": "hunter2"}})
    assert "hunter2" not in str(info.value)


@pytest.mark.parametrize("source", ["body.(", "unknown_variable == 1"])
def test_a_compile_error_is_a_spec_error(source) -> None:
    with pytest.raises(ExpressionError):
        predicates.test(source, {"body": {}})


@pytest.mark.parametrize(
    ("source", "number", "expected"),
    [
        ("value == 0.1", "0.10000000000000001", None),  # its nearest double is 0.1
        ("value <= 0.1", "0.10000000000000001", None),
        ("value > 0.1", "0.100000000000000003", None),  # above 0.1, below the binary value of 0.1
        ("value > 0", "1e-400", None),  # its nearest double is 0
        ("value > 0 && value < 1", "0.10000000000000001", True),
        ("value < 0", "12345678901234567890.1", False),
    ],
)
def test_a_result_that_depends_on_digits_a_double_drops_fails_closed(source, number, expected) -> None:
    """CEL has no decimals: a predicate that no double can decide for this number raises (None)."""
    if expected is None:
        with pytest.raises(predicates.PredicateError):
            predicates.test(source, {"value": Decimal(number)})
    else:
        assert predicates.test(source, {"value": Decimal(number)}) is expected


def test_a_value_expression_returns_its_value_exactly() -> None:
    """A hash sample's dimension or sign-off label: a string, boolean, null or
    exact number, over JSON and a timestamp ``now``."""
    now = datetime(2026, 10, 1, 9, 12, tzinfo=UTC)
    row = {"kind": "ORG", "n": Decimal(2), "x": Decimal("0.5"), "at": "2026-09-30T00:00:00Z", "none": None}
    recent = "row.at != null && timestamp(row.at) >= now - duration('168h')"
    assert [predicates.label(source, {"row": row, "now": now, "key": "C1"}) for source in (
        "row.kind", "row.n + 1", "row.x * 3.0", recent, "row.none", "key + '=' + row.kind",
    )] == ["ORG", Decimal(3), Decimal("1.5"), True, None, "C1=ORG"]  # fmt: skip
    assert predicates.test(recent, {"row": row, "now": now}) is True


@pytest.mark.parametrize("source", ["row.items", "row", "now", "row.n"])
def test_a_value_expression_fails_closed(source) -> None:
    """A list, map or timestamp is no label, and a value no double decides is refused."""
    bindings = {"row": {"items": [1], "n": Decimal("0.10000000000000001")}, "now": datetime.now(UTC)}
    with pytest.raises(predicates.PredicateError):
        predicates.label(source, bindings)
