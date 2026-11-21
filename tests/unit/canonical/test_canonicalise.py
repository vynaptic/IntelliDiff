"""canonicalise(): shaping, projection, array rewriting and valueFilter beyond the golden corpus."""

from __future__ import annotations

import re
import time
from decimal import Decimal

import pytest

from deltasleuth.core.canonical import predicates
from deltasleuth.core.canonical.canonicalise import canonicalise, shape
from deltasleuth.core.canonical.encoding import canonical_json, exact_json, sha256_hex
from deltasleuth.core.canonical.errors import CanonicalError, ExpressionError
from deltasleuth.core.spec.expressions import CEL_VARIABLES
from deltasleuth.core.types import (
    ArrayRule,
    CompareConfig,
    Counted,
    FieldCompare,
    FieldRule,
    KeyedMap,
    Normalizer,
    Side,
    SideConfig,
)

D = Decimal
KEYED_XS = (ArrayRule("$.xs", "keyed", ("id",)),)


@pytest.fixture
def run(make_spec, canon):
    """Canonical (doc, raw_doc) of side a for a compare block given as keyword arguments."""

    def go(text: str, **compare):
        result = canon(text, make_spec(CompareConfig(**compare)))
        return result.doc, result.raw_doc

    return go


def test_hashes_cover_both_trees(make_spec, canon) -> None:
    result = canon(
        '{"n": " x "}', make_spec(CompareConfig(fields=(FieldRule("$.n", (Normalizer("trim"),)),)))
    )
    assert (result.doc, result.raw_doc) == ({"n": "x"}, {"n": " x "})
    assert result.hash == sha256_hex(canonical_json({"n": "x"}))
    assert result.raw_hash == sha256_hex(exact_json({"n": " x "}))


def test_shape_never_shares_nodes_with_the_body() -> None:
    body = {"result": {"xs": [1]}}
    shape(body, SideConfig(side=Side.A, role="r", target="t", select="$.result"))["xs"].append(2)
    assert body == {"result": {"xs": [1]}}


def test_canonical_documents_are_fixed_points(make_spec, canon) -> None:
    compare = CompareConfig(
        fields=(
            FieldRule("$.name", (Normalizer("trim"), Normalizer("casefold"))),
            FieldRule("$.amount", (Normalizer("decimal", {"scale": 2}),)),
            FieldRule("$", (Normalizer("null_if_empty"),)),  # a root rule replaces the root
        )
    )
    spec = make_spec(compare)
    first = canon('{"name": "  ANN ", "amount": "1.5"}', spec)
    assert first.doc == {"name": "ann", "amount": D("1.50")}
    assert canonicalise(first.doc, spec.sides[Side.A], spec, key="K1").hash == first.hash
    assert canon("{}", spec).doc is None


def test_field_rules_apply_in_order_at_every_location(make_spec, canon) -> None:
    fields = (
        FieldRule("$.xs[*].v"),
        FieldRule("$.xs[*].v", (Normalizer("trim"),)),
        FieldRule("$..v", (Normalizer("upper"),)),
    )
    result = canon(
        '{"xs": [{"v": " a "}, {"v": "b"}, {"w": " c "}], "v": " d "}',
        make_spec(CompareConfig(fields=fields)),
    )
    assert result.doc == {"xs": [{"v": "A"}, {"v": "B"}, {"w": " c "}], "v": " D "}


class TestToleranceValues:
    compare = CompareConfig(fields=(FieldRule("$.amt", compare=FieldCompare("tolerance", abs_tol=D("0.1"))),))

    @pytest.mark.parametrize("text", ['{"amt": 1.5}', '{"amt": "1.5"}', '{"amt": null}', '{"other": 1}'])
    def test_readable_values_pass(self, make_spec, canon, text) -> None:
        canon(text, make_spec(self.compare))

    @pytest.mark.parametrize(
        "text", ['{"amt": "secret"}', '{"amt": true}', '{"amt": [1]}', '{"amt": {"v": 1}}']
    )
    def test_unreadable_values_raise_without_the_value(self, make_spec, canon, text) -> None:
        with pytest.raises(CanonicalError) as info:
            canon(text, make_spec(self.compare))
        assert info.value.pattern == "$.amt" and "secret" not in str(info.value)  # the caller renders it


class TestArrays:
    def test_keyed_members_are_grouped_by_key_in_canonical_order(self, run) -> None:
        doc, _ = run('{"xs": [{"id": "b"}, {"id": "a", "v": 9}, {"id": "a", "v": 1}]}', arrays=KEYED_XS)
        assert isinstance(doc["xs"], KeyedMap) and doc["xs"].mode == "keyed"
        assert dict(doc["xs"]) == {"a": [{"id": "a", "v": 1}, {"id": "a", "v": 9}], "b": [{"id": "b"}]}

    def test_members_without_a_key_field_read_it_as_null(self, run) -> None:
        doc, _ = run('{"xs": [{"v": 1}, "scalar"]}', arrays=KEYED_XS)
        assert list(doc["xs"]) == ["null"] and len(doc["xs"]["null"]) == 2

    def test_rule_order_does_not_matter(self, make_spec, canon) -> None:
        rules = (ArrayRule("$.hs[*].ac", "keyed", ("no",)), ArrayRule("$.hs", "keyed", ("id",)))
        text = '{"hs": [{"id": "h1", "ac": [{"no": "2"}, {"no": "1"}]}]}'
        forward = canon(text, make_spec(CompareConfig(arrays=rules)))
        backward = canon(text, make_spec(CompareConfig(arrays=rules[::-1])))
        assert forward.hash == backward.hash and list(forward.doc["hs"]["h1"][0]["ac"]) == ["1", "2"]

    def test_numbers_are_one_set_member_but_strings_are_not_numbers(self, run) -> None:
        doc, _ = run('{"t": [1.0, 1, "1"]}', arrays=(ArrayRule("$.t", "set"),))
        assert sorted(doc["t"]) == ['"1"', "1"]

    def test_unruled_and_non_array_nodes_stay_as_they_are(self, run) -> None:
        rules = (ArrayRule("$.o", "ordered"), ArrayRule("$.xs", "keyed", ("id",)), ArrayRule("$.m[*]", "set"))
        doc, _ = run('{"o": [2, 1], "u": [3], "xs": {"id": "a"}, "m": [["b", "a"], ["c"]]}', arrays=rules)
        assert (doc["o"], doc["u"], doc["xs"]) == ([D(2), D(1)], [D(3)], {"id": "a"})
        assert type(doc["m"]) is list and [list(x) for x in doc["m"]] == [['"a"', '"b"'], ['"c"']]

    def test_root_array_rule(self, run) -> None:
        doc, _ = run('[{"id": "b"}, {"id": "a"}]', arrays=(ArrayRule("$", "keyed", ("id",)),))
        assert isinstance(doc, KeyedMap) and list(doc) == ["a", "b"]

    def test_container_set_members_are_keyed_by_a_short_digest(self, run) -> None:
        doc, _ = run('{"t": [{"b": 1, "a": 2}, {"a": 2, "b": 1}, [1]]}', arrays=(ArrayRule("$.t", "set"),))
        assert len(doc["t"]) == 2 and all(
            re.fullmatch(r"(\{#[0-9a-f]{16}\}|\[#[0-9a-f]{16}\])", k) for k in doc["t"]
        )

    def test_nested_multiset_member_keys_stay_bounded(self, run) -> None:
        # Keys that embedded their members' canonical form grew threefold per level (a 256-byte
        # body took gigabytes at 18 levels).
        levels = 20
        text = '{"items": [' * levels + '{"v": 1}' + "]}" * levels
        rules = tuple(ArrayRule("$" + ".items[*]" * depth + ".items", "multiset") for depth in range(levels))
        started = time.perf_counter()
        doc, _ = run(text, arrays=rules)
        assert time.perf_counter() - started < 1.0
        assert len(canonical_json(doc)) < 100 * levels

    def test_raw_twin_is_keyed_and_ordered_by_normalised_values(self, run) -> None:
        fields = (
            FieldRule("$.xs[*].id", (Normalizer("upper"),)),
            FieldRule("$.xs[*].v", (Normalizer("trim"),)),
        )
        text = (
            '{"xs": [{"id": "k", "v": "  z"}, {"id": "K", "v": "a  "},'
            ' {"id": "k", "v": "b "}, {"id": "K", "v": " b"}]}'
        )
        doc, raw = run(text, arrays=KEYED_XS, fields=fields)
        assert list(raw["xs"]) == ["K"]
        assert [m["v"] for m in doc["xs"]["K"]] == ["a", "b", "b", "z"]
        assert [m["v"] for m in raw["xs"]["K"]] == ["a  ", " b", "b ", "  z"]  # ties by raw form

    def test_raw_set_and_multiset_representatives(self, run) -> None:
        fields = (FieldRule("$.t[*]", (Normalizer("upper"),)),)
        _, raw = run('{"t": ["b", "B", "a"]}', arrays=(ArrayRule("$.t", "set"),), fields=fields)
        assert dict(raw["t"]) == {'"A"': ["a"], '"B"': ["B"]}  # "B" sorts before "b"
        _, raw = run('{"t": ["b", "B", "a"]}', arrays=(ArrayRule("$.t", "multiset"),), fields=fields)
        assert raw["t"]['"B"'] == [Counted("B", 2)]


class TestInclude:
    def test_keeps_selected_nodes_their_containers_and_key_fields(self, run) -> None:
        doc, _ = run(
            '{"a": {"x": 1, "y": 2}, "xs": [{"id": "a", "v": 1, "w": 2}, {"id": "b", "w": 3}], "c": 3}',
            include=("$.a.x", "$.xs[*].v"),
            arrays=KEYED_XS,
        )
        assert doc == {
            "a": {"x": 1},
            "xs": KeyedMap({"a": [{"id": "a", "v": 1}], "b": [{"id": "b"}]}, mode="keyed"),
        }

    def test_members_lacking_the_included_field_keep_their_presence(self, make_spec, canon) -> None:
        # A closed account without a balance, present on one side only, must not vanish;
        # nor may an array whose members all lack the field be mistaken for a missing array.
        spec = make_spec(
            CompareConfig(
                include=("$.accounts[*].balance",), arrays=(ArrayRule("$.accounts", "keyed", ("id",)),)
            )
        )
        both = canon('{"accounts": [{"id": "A1", "balance": 10}, {"id": "A2", "status": "CLOSED"}]}', spec)
        assert list(both.doc["accounts"]) == ["A1", "A2"] and both.doc["accounts"]["A2"] == [{"id": "A2"}]
        lacking = canon('{"accounts": [{"id": "A2"}]}', spec)
        assert (list(lacking.doc["accounts"]), lacking.include_matched) == (["A2"], (False,))
        assert canon('{"accounts": []}', spec).doc == {"accounts": KeyedMap(mode="keyed")}

    def test_pass_through_keeps_container_members_only(self, run) -> None:
        assert run('{"xs": [1, {"v": 2, "w": 3}, {"w": 4}]}', include=("$.xs[*].v",))[0] == {
            "xs": [{"v": 2}, {}]
        }

    def test_key_fields_are_kept_for_keyed_arrays_only(self, run) -> None:
        doc, _ = run(
            '{"xs": [{"id": "a", "v": 1}]}', include=("$.xs[*].v",), arrays=(ArrayRule("$.xs", "set"),)
        )
        assert list(doc["xs"].values()) == [[{"v": D(1)}]]

    @pytest.mark.parametrize(("text", "empty"), [('{"a": 1}', {}), ("[1, 2]", []), ('"scalar"', None)])
    def test_no_match_leaves_nothing(self, run, text, empty) -> None:
        assert run(text, include=("$.zz",))[0] == empty

    def test_a_match_counts_even_if_exclude_removes_it(self, make_spec, canon) -> None:
        result = canon('{"a": {"x": 1}}', make_spec(CompareConfig(include=("$.a",), exclude=("$.a",))))
        assert (result.doc, result.include_matched) == ({}, (True,))


class TestExclude:
    def test_removes_nodes_without_shifting_list_elements(self, run) -> None:
        assert run('{"xs": [0, 1, 2, 3, 4]}', exclude=("$.xs[1]", "$.xs[3]"))[0] == {"xs": [0, 2, 4]}
        text = '{"xs": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}'
        assert run(text, exclude=("$.xs[*].b", "$.xs[0]", "$.xs[0].a"))[0] == {"xs": [{"a": 3}]}

    @pytest.mark.parametrize(("text", "empty"), [('{"a": 1}', {}), ("[1]", []), ("1", None)])
    def test_excluding_the_root_leaves_an_empty_document(self, run, text, empty) -> None:
        assert run(text, exclude=("$",))[0] == empty

    def test_a_bad_path_is_a_spec_error(self, run) -> None:
        with pytest.raises(ExpressionError):
            run("{}", exclude=("$[",))


class TestValueFilter:
    def test_removes_members_from_both_trees_and_records_their_keys(self, make_spec, canon) -> None:
        rules = (*KEYED_XS, ArrayRule("$.s", "set"), ArrayRule("$.m", "multiset"))
        expr = (
            "!(path == '$.xs[*]' && value.v == 1) && !(path == '$.s[*]' && value.x == 1)"
            " && !(path == '$.m[*]' && value == 'a') && !(path == '$.o[*]' && value == 2)"
        )
        text = (
            '{"xs": [{"id": "a", "v": 1}, {"id": "a", "v": 2}], "s": [{"x": 1}, {"x": 2}],'
            ' "m": ["a", "a", "b"], "o": [1, 2]}'
        )
        result = canon(text, make_spec(CompareConfig(arrays=rules, value_filter=expr)))
        assert result.doc["xs"]["a"] == result.raw_doc["xs"]["a"] == [{"id": "a", "v": D(2)}]
        assert list(result.doc["m"]) == list(result.raw_doc["m"]) == ['"b"'] and len(result.doc["s"]) == 1
        assert result.doc["o"] == result.raw_doc["o"] == [D(1)]
        assert set(result.filtered_members) == {"/xs", "/s", "/m"} and result.filtered_members["/xs"] == (
            "a",
        )

    @pytest.mark.parametrize(
        ("expression", "text", "side", "kept"),
        [
            ("value != 'GONE'", '{"state": "gone", "k": 1}', Side.A, {"k": D(1)}),  # normalised values
            ("value != 1.5", '{"n": 1.50, "k": 1}', Side.A, {"k": D(1)}),  # exact numbers
            ("!(key == 'K9' && side == 'b')", '{"k": 1}', Side.B, {}),
        ],
    )
    def test_predicate_variables(self, make_spec, canon, expression, text, side, kept) -> None:
        compare = CompareConfig(
            fields=(FieldRule("$.state", (Normalizer("upper"),)),), value_filter=expression
        )
        result = canon(text, make_spec(compare), side, key="K9")
        assert result.doc == result.raw_doc == kept

    def test_binds_the_declared_names(self, make_spec, canon, monkeypatch) -> None:
        seen: list[set[str]] = []

        def spy(source: str, bindings: dict) -> bool:
            seen.append(set(bindings))
            return True

        monkeypatch.setattr(predicates, "test", spy)
        canon(
            '{"xs": [{"id": "a", "v": 1}], "o": [1]}',
            make_spec(CompareConfig(arrays=KEYED_XS, value_filter="true")),
        )
        assert seen and all(names == set(CEL_VARIABLES["value_filter"]) for names in seen)

    def test_pointer_is_the_differs_pointer(self, make_spec, canon, monkeypatch) -> None:
        seen: list[str] = []

        def spy(source: str, bindings: dict) -> bool:
            seen.append(bindings["pointer"])
            return True

        monkeypatch.setattr(predicates, "test", spy)
        text = (
            '{"xs": [{"id": "a/b"}, {"id": "d", "v": 2}, {"id": "d", "v": 3}], "o": [10, {"n": 1}], "@f": 1}'
        )
        canon(text, make_spec(CompareConfig(arrays=KEYED_XS, value_filter="true")))
        assert sorted(seen) == [
            "/@@f",
            "/o/0",
            "/o/1",
            "/o/1/n",
            "/xs/a~1b",
            "/xs/a~1b/id",
            "/xs/d/@0",
            "/xs/d/@0/id",
            "/xs/d/@0/v",
            "/xs/d/@1",
            "/xs/d/@1/id",
            "/xs/d/@1/v",
        ]

    def test_pointers_number_the_survivors(self, run) -> None:
        # 1 is removed, so 2 is /o/0 and 3 is /o/1, as the differ will report them.
        assert run('{"o": [1, 2, 3]}', value_filter="value != 1 && pointer != '/o/1'") == ({"o": [D(2)]},) * 2
        assert run('{"t": {"a": 1}, "k": 1}', value_filter="!pointer.startsWith('/t')")[0] == {
            "t": {},
            "k": D(1),
        }

    def test_nested_keyed_pointer_uses_member_keys(self, make_spec, canon) -> None:
        rules = (ArrayRule("$.hs", "keyed", ("id",)), ArrayRule("$.hs[*].ac", "keyed", ("no",)))
        compare = CompareConfig(arrays=rules, value_filter="!(path == '$.hs[*].ac[*]' && value.no == 'x')")
        result = canon('{"hs": [{"id": "h/1", "ac": [{"no": "x"}, {"no": "y"}]}]}', make_spec(compare))
        assert result.filtered_members == {"/hs/h~11/ac": ("x",)}
        assert list(result.raw_doc["hs"]["h/1"][0]["ac"]) == ["y"]

    @pytest.mark.parametrize("expression", ["value.field == 1", "value + 1"])
    def test_an_error_or_non_boolean_raises_with_the_pattern_apart(
        self, make_spec, canon, expression
    ) -> None:
        with pytest.raises(CanonicalError) as info:
            canon('{"a": "text"}', make_spec(CompareConfig(value_filter=expression)))
        assert info.value.pattern == "$.a"
