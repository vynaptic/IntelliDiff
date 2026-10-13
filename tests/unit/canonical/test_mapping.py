"""select and JSONata map: no data is never a document, and numbers stay exact."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from deltasleuth.core.canonical import mapping
from deltasleuth.core.canonical.errors import ExpressionError, MappingError
from deltasleuth.core.canonical.mapping import apply_map, apply_select

D = Decimal
HP1 = D("0.12345678901234567890123")  # more digits than a double holds
HP2 = D("0.98765432109876543210987")
HP1_NEAR = D("0.12345678901234567890124")  # the same nearest double as HP1


class TestSelect:
    def test_one_node_as_is_several_as_a_list(self) -> None:
        doc = {"result": {"a": 1}, "items": [{"v": None}, {"v": 1}]}
        assert apply_select("$.result", doc) is doc["result"]
        assert apply_select("$.items[*].v", doc) == [None, 1]  # nulls among data are kept
        assert apply_select("$.result.missing_or_empty", {"result": {"missing_or_empty": []}}) == []

    @pytest.mark.parametrize(
        ("path", "doc"),
        [
            ("$.missing", {"a": 1}),
            ("$.data", {"data": None, "errors": [{"message": "timeout"}]}),  # a GraphQL failure
            ("$.items[*].v", {"items": [{"v": None}, {"v": None}]}),
            ("$", None),
        ],
    )
    def test_no_node_or_only_nulls_is_no_data(self, path, doc) -> None:
        with pytest.raises(MappingError, match="select matched no data"):
            apply_select(path, doc)


class TestMapShapes:
    def test_rename_restructure_and_keep_null_apart_from_undefined(self) -> None:
        body = {"custId": "C1", "tags": ["a"], "a": None, "t": True}
        out = apply_map("{'id': custId, 'tags': tags, 'a': a, 'b': b, 't': t}", body)
        assert out == {"id": "C1", "tags": ["a"], "a": None, "t": True} and type(out["tags"]) is list
        assert apply_map("[a, null, id]", {"a": None, "id": 1}) == [None, None, D(1)]

    @pytest.mark.parametrize(
        ("expression", "body"),
        [
            ("missing", {"a": 1}),  # undefined
            ("a", {"a": None, "b": 1}),  # null
            # An error envelope read through the success map: every member is undefined.
            ("{'id': customer.id, 'name': customer.name}", {"error": {"code": "DB_DOWN"}}),
            ("{'customer': data.customer}", {"data": {"customer": None}, "errors": [{"m": "x"}]}),
            ("{'ids': [customer.id], 'n': {'x': customer.name}}", {"error": "x"}),
        ],
    )
    def test_no_value_from_a_non_empty_input_is_no_data(self, expression, body) -> None:
        with pytest.raises(MappingError) as info:
            apply_map(expression, body)
        assert str(info.value) == "map produced an empty document"  # names no payload value

    def test_an_empty_input_may_map_to_an_empty_document(self) -> None:
        assert apply_map("{'id': id}", {}) == {} and apply_map("{'tags': tags}", []) == {}

    def test_runtime_errors_name_the_code_not_the_values(self) -> None:
        with pytest.raises(MappingError) as info:
            apply_map("a + b", {"a": D(1), "b": "secret-text"})
        assert "T2002" in str(info.value) and "secret-text" not in str(info.value)
        with pytest.raises(MappingError, match="non-JSON"):
            apply_map("function($x){$x}", {})
        with pytest.raises(MappingError):
            apply_map("1/0", {})  # not a finite number

    def test_a_syntax_error_is_a_spec_error(self) -> None:
        with pytest.raises(ExpressionError):
            apply_map("{ 'a': ", {})


class TestNumbers:
    def test_moved_numbers_survive_exactly(self) -> None:
        big = D("123456789012345678901234567890123456789")
        body = {"big": big, "p": HP1, "t": D("1E-400"), "f": D("1.50")}
        out = apply_map("{'big': big, 'l': [p, t], 'n': {'f': f}}", body)
        assert out == {"big": big, "l": [HP1, D("1E-400")], "n": {"f": D("1.5")}}
        assert str(out["l"][0]) == str(HP1)
        assert body == {"big": big, "p": HP1, "t": D("1E-400"), "f": D("1.50")}  # the input is untouched

    def test_computed_numbers_become_decimals(self) -> None:
        assert apply_map("a + b", {"a": D(2), "b": D("0.5")}) == D("2.5")
        assert apply_map("0.1 + 0.2", {}) == D("0.30000000000000004")  # shortest float text

    def test_equal_high_precision_numbers_compare_equal(self) -> None:
        assert apply_map("{'same': x = y}", {"x": HP1, "y": HP1}) == {"same": True}
        assert apply_map("{'same': x = y}", {"x": HP1, "y": HP2}) == {"same": False}
        assert apply_map("$count($distinct([x, y]))", {"x": HP1, "y": HP1}) == D(1)

    def test_an_order_the_nearest_doubles_decide_is_kept(self) -> None:
        assert apply_map("$sort([y, x])", {"x": HP1, "y": HP2}) == [HP1, HP2]

    @pytest.mark.parametrize(
        ("expression", "body"),
        [
            ("{'same': x = y}", {"x": HP1, "y": HP1_NEAR}),  # a double cannot decide it
            ("{'s': $substring(x, 0, 4)}", {"x": HP2, "y": HP1}),
            ("{'s': $string(x)}", {"x": HP2, "y": HP1}),
            ("{'n': $length($string(x))}", {"x": HP2, "y": HP1}),
            ("{'lt': y < x}", {"x": HP2, "y": HP1}),
            ("$sort([x, y])", {"x": HP2, "y": HP1}),  # x is met first but is the larger
            ("{'n': $number(x)}", {"x": HP2, "y": HP1}),
            ("'id-' & x", {"x": HP1}),  # as text
            ("{ $string(x): 1 }", {"x": HP1}),  # as a member name
        ],
    )
    def test_reading_a_high_precision_number_is_refused(self, expression, body) -> None:
        with pytest.raises(MappingError):
            apply_map(expression, body)

    def test_stand_ins_are_deterministic_and_cannot_be_forged(self) -> None:
        forged = "\x00ds-number:0000000000000000:0\x00"
        expression = "{'s': s, 'l': [y, x], 'same': x = y}"
        expected = {"s": forged, "l": [HP2, HP1], "same": False}
        assert apply_map(expression, {"s": forged, "x": HP1, "y": HP2}) == expected
        assert apply_map(expression, {"s": forged, "x": HP1, "y": HP2}) == expected


def test_concurrent_evaluations_of_one_expression_are_isolated() -> None:
    # The compiled expression is cached and shared; each call must see only its own input.
    expression = "{'id': $.id, 'double': [$.id, id]}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(lambda i: apply_map(expression, {"id": D(i)}), range(200)))
    assert outputs == [{"id": D(i), "double": [D(i), D(i)]} for i in range(200)]


@pytest.mark.parametrize(
    "expression",
    [
        "{'n': $count([1..3000000].($string($)))}",  # seconds of work: the time bound
        "{'n': ($f := function($n) { $n = 0 ? 0 : 1 + $f($n - 1) }; $f(100000))}",  # the depth bound
    ],
)
def test_runaway_map_is_a_mapping_error(monkeypatch: pytest.MonkeyPatch, expression: str) -> None:
    # A map runs inside the shared server process, so it must not run unbounded.
    monkeypatch.setattr(mapping, "MAP_TIMEOUT_MS", 200)
    started = time.monotonic()
    with pytest.raises(MappingError, match=r"D1012|D1011"):
        apply_map(expression, {"id": 1})
    assert time.monotonic() - started < 5
