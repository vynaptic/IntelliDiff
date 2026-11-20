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

    @pytest.mark.parametrize(
        ("expression", "one", "other"),
        [
            ("{'n': v * 1}", "1234567890123456789", "1234567890123456800"),  # beyond a double's integers
            ("{'n': $sum([v, 0])}", "9007199254740993", "9007199254740992"),
            ("{'s': $string(v)}", "1234567890123456", "1234567890123457"),  # 16 digits, written with 15
            ("{'s': v & ''}", "1234567890123456", "1234567890123457"),
            ("{'s': $string(v)}", "0.1234567890123456", "0.1234567890123457"),
        ],
    )
    def test_two_long_numbers_never_compute_to_one_result(self, expression, one, other) -> None:
        results = []
        for text in (one, other):
            try:
                results.append(apply_map(expression, {"v": D(text)}))
            except MappingError:
                results.append(text)  # refused: the side is not judged
        assert results[0] != results[1]

    def test_a_long_number_is_moved_exactly_and_refused_when_read(self) -> None:
        long = D("1234567890123456789")
        assert apply_map("{'id': v}", {"v": long}) == {"id": long}
        with pytest.raises(MappingError):
            apply_map("{'s': $string(v)}", {"v": long})

    def test_a_number_of_fifteen_digits_is_computed_and_written_exactly(self) -> None:
        assert apply_map("{'s': $string(v)}", {"v": D("123456789012345")}) == {"s": "123456789012345"}
        assert apply_map("{'n': v + 1}", {"v": D("123456789012345")}) == {"n": D("123456789012346")}
        assert apply_map("{'s': $string(v)}", {"v": D("0.12345678901234")}) == {"s": "0.12345678901234"}

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


class TestRegexBounds:
    """A map's regular expression runs inside the shared server process: one that can backtrack for hours
    is refused when the map compiles, and what remains is bounded by the map's time bound and by the
    length of the string it reads. An ordinary one maps as Python's re reads it."""

    @pytest.mark.parametrize(
        "expression",
        ["$contains(a, /^(x+)+$/)", "$match(a, /(\\w+\\s?)*$/)", "$split(a, /(?:[a-z]+,?){2,}/i)"],
    )
    def test_a_repeat_inside_a_repeat_is_refused_when_the_map_compiles(self, expression: str) -> None:
        with pytest.raises(ExpressionError, match="repeat inside a repeat"):
            apply_map(expression, {"a": "x"})

    @pytest.mark.parametrize(
        ("expression", "construct"),
        [
            ("$contains(a, /\\Ba*a*a*b/)", "\\B"),  # polynomial: hours on 10,000 characters on re
            ("$replace(a, /(a)\\1/, '')", "a back-reference"),
            ("$contains(a, /(?P<x>a)(?P=x)/)", "a back-reference"),
            ("$contains(a, /(a)?(?(1)b|c)/)", "a conditional"),
            ("$contains(a, /(?a:\\w)+/)", "a scoped (?a:...) or (?u:...) flag"),
        ],
    )
    def test_a_construct_that_runs_without_a_time_bound_is_refused_when_the_map_compiles(
        self, expression: str, construct: str
    ) -> None:
        started = time.monotonic()
        with pytest.raises(ExpressionError, match="runs without a time bound") as refused:
            apply_map(expression, {"a": "a" * 60})
        assert time.monotonic() - started < 0.5
        assert f"uses {construct}," in str(refused.value)
        assert "rewrite it without \\B, back-references, conditionals or scoped flags" in str(refused.value)

    @pytest.mark.parametrize("expression", ["$contains(a, /^(x++)+$/)", "$contains(a, /^(?>x+)+$/)"])
    def test_a_possessive_or_atomic_inner_repeat_is_accepted(self, expression: str) -> None:
        assert apply_map(expression, {"a": "x" * 40 + "!"}) is False

    def test_a_longer_input_fails_the_side_at_once(self) -> None:
        started = time.monotonic()
        with pytest.raises(MappingError, match="10,000 characters"):
            apply_map("$contains(a, /y/)", {"a": "x" * (mapping.MAX_REGEX_INPUT + 1)})
        assert time.monotonic() - started < 0.5
        assert apply_map("$contains(a, /y/)", {"a": "x" * mapping.MAX_REGEX_INPUT}) is False

    def test_backtracking_stops_at_the_map_time_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Exponential in both Python's re and the bounded engine: only a time bound ends it.
        monkeypatch.setattr(mapping, "MAP_TIMEOUT_MS", 300)
        started = time.monotonic()
        with pytest.raises(MappingError) as failed:
            apply_map("$contains(a, /^(?:a.|.b)+$/)", {"a": "ab" * 28 + "!"})
        assert time.monotonic() - started < 1.5
        assert "time bound" in str(failed.value)

    def test_a_pattern_python_re_backtracks_on_answers_in_time(self) -> None:
        started = time.monotonic()
        assert apply_map("$contains(a, /^(ab|a|b)+c/)", {"a": "ab" * 26 + "!"}) is False
        assert time.monotonic() - started < 1

    @pytest.mark.parametrize(
        ("expression", "body", "expected"),
        [
            ('$replace(link, /^.*\\/([0-9]+)\\/?$/, "$1")', {"link": "https://x.example/people/12/"}, "12"),
            # \d and \w read Unicode digits and letters as Python's re does, combining marks excepted.
            ("$match(s, /(\\d+)-(\\w+)/).groups", {"s": "ORG-000123 45-ab ٣-é"}, ["45", "ab", "٣", "e"]),
            ("$split(s, /\\s*,\\s*/)", {"s": "a , b,　c"}, ["a", "b", "c"]),
            ("$contains(s, /^mbr-\\d+$/i)", {"s": "MBR-0045123"}, True),
            ("$replace(s, /\\bx\\b/, '_')", {"s": "x xx éx x"}, "_ xx éx _"),
            ("$replace(s, /(a)(b)?/, '[$2$1]')", {"s": "ab a"}, "[ba] [a]"),
            ("$replace(s, /o/, function($m) { $uppercase($m.match) })", {"s": "foo"}, "fOO"),
            ("$split(s, /x*/)", {"s": "abxd"}, ["", "a", "b", "", "d", ""]),
            ("$contains(s, /^[^\\W\\d]+$/)", {"s": "été"}, True),
            ("$contains(s, /^a.c$/m)", {"s": "x\na\nc\nabc"}, True),
            ("$match(s, /\\ud83d\\ude00|\\U0001F600/).match", {"s": "a\U0001f600"}, "\U0001f600"),
        ],
    )
    def test_an_ordinary_regex_maps_as_before(self, expression: str, body: dict, expected: object) -> None:
        assert apply_map(expression, body) == expected
