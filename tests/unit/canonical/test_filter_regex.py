"""A JSONPath filter's match() and search() run inside the shared server process. Each runs on the regex
engine within the time bound of the path's evaluation and reads at most 10,000 characters of one string. A
pattern with a repeat inside a repeat is refused: when the spec loads if the path writes it, at run time if
the data supplies it. A run-time bound fails the evaluation, so a side's select leaves the side INCOMPLETE
and is never a match. An ordinary pattern selects what RFC 9535 selects: match() a full match, search() a
partial one."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from deltasleuth.core.canonical import paths
from deltasleuth.core.canonical.errors import MappingError, PathError
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.canonical.paths import compile_path, find, jsonpath_error
from deltasleuth.core.types import Side, SideConfig, SideStateKind, TargetCapabilities

# Exponential on the regex engine: about a second on 23 characters, twice as long with each one more, so
# minutes on the 31 these tests give it.
MEASURED = "$.items[?match(@.a, '(a|a)+')]"
FROM_DATA = "$.items[?match(@.a, @.p)]"


def _codes(path: str, items: list[dict]) -> list[object]:
    return [value["code"] for value, _ in find(path, {"items": items})]


class TestTimeBound:
    def test_the_measured_pattern_stops_at_the_time_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(paths, "PATH_REGEX_TIMEOUT_MS", 300)
        started = time.monotonic()
        with pytest.raises(MappingError, match="time bound"):
            find(MEASURED, {"items": [{"a": "a" * 30 + "!"}]})
        assert time.monotonic() - started < 1.5

    def test_the_regular_expressions_of_one_evaluation_share_the_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        one = {"a": "a" * 20 + "!"}
        started = time.monotonic()
        assert find(MEASURED, {"items": [one]}) == []
        each = time.monotonic() - started
        monkeypatch.setattr(paths, "PATH_REGEX_TIMEOUT_MS", int(each * 3000) + 1)  # three strings' time
        with pytest.raises(MappingError, match="time bound"):
            find(MEASURED, {"items": [one] * 10})

    def test_a_compiled_path_is_bounded_as_find_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(paths, "PATH_REGEX_TIMEOUT_MS", 300)
        started = time.monotonic()
        with pytest.raises(MappingError, match="time bound"):
            list(compile_path(MEASURED).finditer({"items": [{"a": "a" * 30 + "!"}]}))
        assert time.monotonic() - started < 1.5

    def test_a_pattern_from_the_data_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(paths, "PATH_REGEX_TIMEOUT_MS", 300)
        started = time.monotonic()
        with pytest.raises(MappingError, match="time bound") as failed:
            find(FROM_DATA, {"items": [{"a": "a" * 30 + "!", "p": "(a|a)+"}]})
        assert time.monotonic() - started < 1.5
        assert "(a|a)" not in str(failed.value)  # the data's pattern is a value: never in a message

    def test_a_select_past_the_bound_leaves_the_side_incomplete(self, make_spec, make_observation) -> None:
        cfg = SideConfig(side=Side.A, role="legacy", target="t", select=MEASURED)
        body = {"items": [{"a": "a" * 30 + "!"}, {"a": "aa"}]}
        started = time.monotonic()
        state = observe(make_observation(body), cfg, make_spec(side_a=cfg), TargetCapabilities())
        assert (state.kind, state.reason) == (SideStateKind.INCOMPLETE, "unparseable")
        assert "time bound" in state.detail
        assert time.monotonic() - started < paths.PATH_REGEX_TIMEOUT_MS / 1000 + 1


class TestRefusal:
    @pytest.mark.parametrize(
        "path",
        [
            "$.items[?match(@.a, '(a+)+')]",
            "$.items[?search(@.a, '(?:\\\\d+,?)*;')]",
            "$.items[?@.x && !search(@.a, '^(\\\\p{L}+\\\\s?)*$')]",
            "$.items[?match(@.a, '(x*)*')].b",
        ],
    )
    def test_a_literal_repeat_inside_a_repeat_is_refused_when_the_path_compiles(self, path: str) -> None:
        problem = jsonpath_error(path)
        assert problem is not None and problem.startswith("regular expression /")
        assert "repeat inside a repeat" in problem
        with pytest.raises(PathError, match="repeat inside a repeat"):
            compile_path(path)

    @pytest.mark.parametrize(
        ("path", "fragment"),
        [
            ("$.items[?match(@.a, '(')]", "is not valid"),
            pytest.param(
                "$.items[?search(@.a, '" + "a" * 4001 + "')]", "at most 4000 characters", id="too-long"
            ),
            ("$.items[?search(@.a, '(?:(?:a{100}){100}){100}')]", "compilation work limit"),
        ],
    )
    def test_a_literal_pattern_past_its_bounds_is_refused(self, path: str, fragment: str) -> None:
        problem = jsonpath_error(path)
        assert problem is not None and fragment in problem

    @pytest.mark.parametrize("pattern", ["(a++)+", "(?>a+)+"])
    def test_a_possessive_or_atomic_inner_repeat_is_accepted(self, pattern: str) -> None:
        path = f"$.items[?match(@.a, '{pattern}')]"
        assert jsonpath_error(path) is None
        started = time.monotonic()
        assert find(path, {"items": [{"a": "a" * 40 + "!"}]}) == []
        assert time.monotonic() - started < 0.5

    @pytest.mark.parametrize(
        ("pattern", "fragment"),
        [
            ("(a+)+", "repeat inside a repeat"),
            pytest.param("a" * 4001, "at most 4000 characters", id="too-long"),
            ("(?|(a)|(b))+", "cannot be checked"),  # regex reads a branch reset; Python's re does not
        ],
    )
    def test_a_refused_pattern_from_the_data_fails_the_evaluation_at_once(
        self, pattern: str, fragment: str
    ) -> None:
        started = time.monotonic()
        with pytest.raises(MappingError, match=fragment) as failed:
            find(FROM_DATA, {"items": [{"a": "a" * 18 + "!", "p": pattern}]})
        assert time.monotonic() - started < 0.5
        assert pattern not in str(failed.value)


class TestInputBound:
    def test_a_longer_string_fails_the_evaluation_at_once(self) -> None:
        started = time.monotonic()
        with pytest.raises(MappingError, match="10,000 characters"):
            find("$.items[?search(@.a, 'y')]", {"items": [{"a": "x" * 10_001}]})
        assert time.monotonic() - started < 0.5
        assert find("$.items[?search(@.a, 'y')]", {"items": [{"a": "x" * 10_000}]}) == []


class TestOrdinaryFilters:
    @pytest.mark.parametrize(
        ("path", "items", "expected"),
        [
            (
                "$.items[?match(@.code, 'ORG-[0-9]+')]",
                [{"code": "ORG-000123"}, {"code": "xORG-1"}, {"code": "ORG-12x"}],
                ["ORG-000123"],
            ),
            (
                "$.items[?search(@.code, 'ORG-[0-9]+')]",
                [{"code": "ORG-000123"}, {"code": "xORG-1"}, {"code": "ORG-x"}],
                ["ORG-000123", "xORG-1"],
            ),
            (
                "$.items[?!match(@.code, 'MBR-[0-9]{7}')]",
                [{"code": "MBR-0045123"}, {"code": "MBR-45"}],
                ["MBR-45"],
            ),
            # An I-Regexp dot is any one character but a line break, one outside the BMP included.
            (
                "$.items[?match(@.code, 'a.c')]",
                [{"code": "abc"}, {"code": "a\nc"}, {"code": "a\U0001f600c"}, {"code": "a\rc"}],
                ["abc", "a\U0001f600c"],
            ),
            (
                "$.items[?match(@.code, '\\\\p{Lu}\\\\p{Ll}+')]",
                [{"code": "Émile"}, {"code": "émile"}, {"code": "ÉMILE"}, {"code": "Ωμέγα"}],
                ["Émile", "Ωμέγα"],
            ),
            ("$.items[?search(@.code, 'é')]", [{"code": "café"}, {"code": "café"}], ["café"]),
            (
                "$.items[?match(@.code, '[^0-9]+')]",
                [{"code": "ab"}, {"code": "a1"}, {"code": "٣"}],
                ["ab", "٣"],
            ),
            (
                "$.items[?match(@.code, @.pattern)]",
                [
                    {"code": "MBR-0045123", "pattern": "MBR-[0-9]{7}"},
                    {"code": "MBR-45", "pattern": "MBR-[0-9]{7}"},
                    {"code": "(", "pattern": "("},  # not a valid pattern: RFC 9535 selects nothing
                    {"code": "x", "pattern": 1},
                ],
                ["MBR-0045123"],
            ),
            (
                "$.items[?match(@.n, '1')]",
                [{"code": "A", "n": Decimal(1)}, {"code": "B", "n": "1"}, {"code": "C"}],
                ["B"],
            ),
        ],
    )
    def test_an_ordinary_filter_selects_as_before(self, path: str, items: list, expected: list) -> None:
        assert jsonpath_error(path) is None
        assert _codes(path, items) == expected


class TestEveryPathOfASide:
    def test_a_truncation_flag_past_its_bound_leaves_the_side_incomplete(
        self, make_spec, make_observation
    ) -> None:
        """A side's truncation flags are paths too: one whose pattern the answer supplies and that reads a
        string past the bound leaves the side INCOMPLETE, as a select does, never an exception."""
        flag = "$.items[?match(@.a, @.p)]"
        cfg = SideConfig(side=Side.A, role="legacy", target="t", truncation_flags=(flag,))
        body = {"items": [{"a": "a" * 10_001, "p": "a+"}]}
        state = observe(make_observation(body), cfg, make_spec(side_a=cfg), TargetCapabilities())
        assert (state.kind, state.reason) == (SideStateKind.INCOMPLETE, "unparseable")
