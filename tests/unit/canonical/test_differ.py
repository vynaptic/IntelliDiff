"""diff(): finding attributes and compare-mode edges beyond the golden corpus."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltasleuth.core.canonical.errors import PathError
from deltasleuth.core.diff.differ import diff
from deltasleuth.core.types import (
    ArrayRule,
    ChangeType,
    CompareConfig,
    Counted,
    Direction,
    FieldCompare,
    FieldRule,
    Finding,
    Normalizer,
    Severity,
    Side,
)

D = Decimal
MISSING, EXTRA = ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B
VALUE, TYPE = ChangeType.VALUE_CHANGED, ChangeType.TYPE_CHANGED
KEYED = CompareConfig(arrays=(ArrayRule("$.xs", "keyed", ("id",)),))


@pytest.fixture
def run(make_spec, canon):
    def go(a: str, b: str, compare: CompareConfig | None = None) -> list[Finding]:
        spec = make_spec(compare)
        return diff(canon(a, spec, Side.A), canon(b, spec, Side.B), spec, key="K1")

    return go


def summary(findings: list[Finding]) -> list[tuple]:
    return [(f.json_pointer, f.path_pattern, f.change_type, f.taxonomy_code) for f in findings]


def tolerance(**bounds) -> CompareConfig:
    return CompareConfig(fields=(FieldRule("$.x", compare=FieldCompare("tolerance", **bounds)),))


def test_finding_attributes(run) -> None:
    found = run('{"a": 1, "b": 2, "a/b": 1}', '{"b": 3, "e": null, "a/b": 2}')
    assert summary(found) == [
        ("/a", "$.a", MISSING, None),
        ("/a~1b", "$['a/b']", VALUE, None),
        ("/b", "$.b", VALUE, None),
        ("/e", "$.e", EXTRA, None),
    ]
    missing, escaped, changed, extra = found
    assert (missing.a_value, missing.b_value, missing.direction) == (D(1), None, Direction.A_ONLY)
    assert (changed.a_value, changed.b_value, changed.direction) == (D(2), D(3), Direction.BOTH)
    assert extra.direction is Direction.B_ONLY and escaped.pointer_parts == (("field", "a/b"),)
    assert all(
        f.key == "K1" and f.severity is None and not f.raw_only and f.member_key is None for f in found
    )


def test_a_root_type_change_has_the_root_pointer(run) -> None:
    assert summary(run("[1]", '{"a": 1}')) == [("", "$", TYPE, None)]


def test_keyed_member_context(run) -> None:
    found = run('{"xs": [{"id": "1", "v": 1}]}', '{"xs": [{"id": "1", "v": 3}, {"id": "2"}]}', KEYED)
    assert summary(found) == [("/xs/1/v", "$.xs[*].v", VALUE, None), ("/xs/2", "$.xs[*]", EXTRA, None)]
    changed, extra = found
    assert (changed.member_key, changed.array_path) == ("1", "$.xs")
    assert changed.pointer_parts == (("field", "xs"), ("member", "1"), ("field", "v"))
    assert extra.b_value == {"id": "2"} and extra.value_class is None


def test_one_sided_key_with_several_members_is_no_duplicate(run) -> None:
    found = run('{"xs": [{"id": "1", "v": 1}, {"id": "1", "v": 2}]}', '{"xs": []}', KEYED)
    assert summary(found) == [("/xs/1/@0", "$.xs[*]", MISSING, None), ("/xs/1/@1", "$.xs[*]", MISSING, None)]
    assert all(f.value_class is None for f in found)


def test_a_ruled_array_against_an_object_is_a_type_change(run) -> None:
    assert summary(run('{"xs": [{"id": "1"}]}', '{"xs": {"id": "1"}}', KEYED)) == [
        ("/xs", "$.xs", TYPE, None)
    ]


def test_multiset_counts_are_decimal_values(run) -> None:
    compare = CompareConfig(arrays=(ArrayRule("$.t", "multiset"),))
    found = run('{"t": ["a", "a", "b"]}', '{"t": ["a", "b", "c"]}', compare)
    assert summary(found) == [('/t/"a"', "$.t[*]", VALUE, None), ('/t/"c"', "$.t[*]", EXTRA, None)]
    assert (found[0].a_value, found[0].b_value, found[0].b_raw) == (D(2), D(1), D(1))
    assert found[1].b_value == Counted("c", 1)


class TestCompareModes:
    @pytest.mark.parametrize(
        ("bounds", "a", "b", "taxonomy"),
        [
            (
                {"abs_tol": D(1)},
                "1234567890123456789012345678.5",
                "1234567890123456789012345679.5",
                "NORMALIZATION_ONLY",
            ),
            ({"abs_tol": D(1)}, "1234567890123456789012345678.5", "1234567890123456789012345679.6", None),
            # |a - b| is exactly rel * |b|: nothing is rounded to a 28-digit context
            ({"rel_tol": D(1)}, "0", "12345678901234567890123456781111111", "NORMALIZATION_ONLY"),
        ],
    )
    def test_tolerance_is_exact_for_huge_numbers(self, run, bounds, a, b, taxonomy) -> None:
        found = run(f'{{"x": {a}}}', f'{{"x": {b}}}', tolerance(**bounds))
        assert summary(found) == [("/x", "$.x", VALUE, taxonomy)]

    def test_tolerance_reads_numeric_strings_but_never_coerces_types(self, run) -> None:
        compare = tolerance(abs_tol=D("0.05"))
        assert summary(run('{"x": "1.00"}', '{"x": "1.04"}', compare))[0][3] == "NORMALIZATION_ONLY"
        assert summary(run('{"x": 1}', '{"x": "1"}', compare)) == [("/x", "$.x", TYPE, None)]
        assert run('{"x": 1.0}', '{"x": 1}', compare) == []

    def test_accepted_difference_keeps_the_normalised_values(self, run) -> None:
        compare = CompareConfig(
            arrays=(ArrayRule("$.xs", "keyed", ("id",)),),
            fields=(FieldRule("$.xs[*].amt", compare=FieldCompare("tolerance", abs_tol=D("0.5"))),),
        )
        (found,) = run('{"xs": [{"id": "a", "amt": 1}]}', '{"xs": [{"id": "a", "amt": 1.4}]}', compare)
        assert (found.taxonomy_code, found.severity, found.raw_only) == (
            "NORMALIZATION_ONLY",
            Severity.INFO,
            False,
        )
        assert (found.a_value, found.b_value) == (D(1), D("1.4"))

    def test_similarity_on_non_strings_is_exact(self, run) -> None:
        compare = CompareConfig(fields=(FieldRule("$.s", compare=FieldCompare("similarity", threshold=0.0)),))
        assert summary(run('{"s": 1}', '{"s": 2}', compare)) == [("/s", "$.s", VALUE, None)]

    def test_rule_path_is_matched_by_normalised_pattern(self, run) -> None:
        compare = CompareConfig(
            fields=(FieldRule("$['o'][*]", compare=FieldCompare("tolerance", abs_tol=D(1))),)
        )
        assert [f.taxonomy_code for f in run('{"o": [1, 5]}', '{"o": [2, 6]}', compare)] == [
            "NORMALIZATION_ONLY"
        ] * 2

    def test_raw_walk_ignores_compare_modes(self, run) -> None:
        # Normalisation made the values equal; the raw difference is reported whatever the mode says.
        rule = FieldRule(
            "$.x", (Normalizer("decimal", {"scale": 0}),), compare=FieldCompare("tolerance", abs_tol=D(1))
        )
        (found,) = run('{"x": 1.2}', '{"x": 1.4}', CompareConfig(fields=(rule,)))
        assert found.raw_only and (found.a_raw, found.b_raw) == (D("1.2"), D("1.4"))

    def test_non_pattern_path_with_a_compare_mode_is_a_spec_error(self, make_spec, canon) -> None:
        spec = make_spec(
            CompareConfig(fields=(FieldRule("$..x", compare=FieldCompare("tolerance", abs_tol=1)),))
        )
        side = canon('{"o": [1]}', spec)
        with pytest.raises(PathError):
            diff(side, side, spec, key="K1")


class TestRawValues:
    def test_both_walks_fill_raw_and_normalised_values(self, run) -> None:
        compare = CompareConfig(fields=(FieldRule("$.n", (Normalizer("trim"),)),))
        (changed,) = run('{"n": " a "}', '{"n": " b"}', compare)
        assert (changed.a_value, changed.b_value, changed.a_raw, changed.b_raw) == ("a", "b", " a ", " b")
        (raw_only,) = run('{"n": " a "}', '{"n": "a"}', compare)
        assert (raw_only.a_value, raw_only.b_value, raw_only.a_raw, raw_only.b_raw) == ("a", "a", " a ", "a")
        assert (raw_only.taxonomy_code, raw_only.severity, raw_only.raw_only) == (
            "NORMALIZATION_ONLY",
            Severity.INFO,
            True,
        )

    def test_multiset_representatives_differ_only_raw(self, run) -> None:
        compare = CompareConfig(
            arrays=(ArrayRule("$.t", "multiset"),), fields=(FieldRule("$.t[*]", (Normalizer("upper"),)),)
        )
        (found,) = run('{"t": ["a"]}', '{"t": ["A"]}', compare)
        assert summary([found]) == [('/t/"A"', "$.t[*]", VALUE, "NORMALIZATION_ONLY")]
        assert (found.a_raw, found.b_raw) == ("a", "A")

    def test_null_if_empty_versus_absent(self, run) -> None:
        compare = CompareConfig(
            null_equals_absent=True, fields=(FieldRule("$.x", (Normalizer("null_if_empty"),)),)
        )
        assert summary(run('{"x": ""}', "{}", compare)) == [("/x", "$.x", MISSING, "NORMALIZATION_ONLY")]

    def test_emptied_arrays_under_a_rule_walk_without_a_twin(self, run) -> None:
        compare = CompareConfig(
            arrays=(ArrayRule("$.xs", "keyed", ("id",)),),
            fields=(FieldRule("$.xs", (Normalizer("null_if_empty"),)),),
        )
        found = run('{"xs": []}', '{"xs": null}', compare)
        assert summary(found) == [("/xs", "$.xs", TYPE, "NORMALIZATION_ONLY")]
        assert (found[0].a_value, found[0].a_raw) == (None, [])
