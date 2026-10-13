"""Compile checks for CEL, JSONata and key regexes."""

from __future__ import annotations

import pytest

from deltasleuth.core.spec import CEL_VARIABLES
from deltasleuth.core.spec.expressions import cel_error, jsonata_error, regex_error


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("path == '$.segment' && a == 'PRIVATE' && b == 'WEALTH'", "known_diff"),
        ("change_type == 'missing_in_b' && direction == 'a_only' && attrs.x == 1", "known_diff"),
        ("!(path == '$.accounts[*]' && value.status == 'CLOSED')", "value_filter"),
        ("strata.flag == 'X' ? 'flag=X' : 'degree=' + strata.degree", "signoff_stratum"),
        ("row.degree", "stratum_dimension"),
        ("row.kind + attrs.x", "sampling_stratum"),
        # Forms that validated and then crashed the run
        ("response.status == 200 && !has(response.body.data)", "not_found"),
        ("status == 200 && has(headers.etag) && !has(body.data)", "not_found"),
        ("!pointer.startsWith('/tags')", "value_filter"),
        ("stratum == null && side == 'a' && key != '' && size(strata) == 0", "value_filter"),
        ("status == 404 && has(body.error) && has(headers.x)", "absence_shape"),
        ("hop_count == null && link_reason == null && attrs == {} && strata == {}", "known_diff"),
    ],
)
def test_valid_cel(source: str, kind: str) -> None:
    assert cel_error(source, kind) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("source", "kind", "fragment"),
    [
        ("path == ", "known_diff", "Syntax error"),
        ("chnage_type == 'x'", "known_diff", "undeclared reference"),
        ("value == 1", "known_diff", "undeclared reference"),  # a valueFilter variable
        ("path + 1 == 2", "known_diff", "no matching overload"),
        ("path", "known_diff", "must evaluate to bool"),
        ("1 + 2", "signoff_stratum", "must evaluate to string"),
    ],
)
def test_invalid_cel_is_one_clean_line(source: str, kind: str, fragment: str) -> None:
    problem = cel_error(source, kind)  # type: ignore[arg-type]
    assert problem is not None and fragment in problem
    assert "INVALID_ARGUMENT" not in problem and "\n" not in problem


def test_engine_kinds_declare_the_documented_variables() -> None:
    # CEL_VARIABLES is the single source of truth every engine evaluator binds.
    assert set(CEL_VARIABLES["not_found"]) == {"response", "status", "headers", "body"}
    assert set(CEL_VARIABLES["absence_shape"]) == {"body", "status", "headers"}
    assert set(CEL_VARIABLES["value_filter"]) == {
        "key",
        "side",
        "path",
        "pointer",
        "value",
        "stratum",
        "strata",
    }
    assert set(CEL_VARIABLES["known_diff"]) == {
        *("key", "stratum", "strata", "path", "pointer", "change_type", "direction", "a", "b"),
        *("value_class", "taxonomy_code", "severity", "link_reason", "hop_count", "attrs"),
    }


@pytest.mark.parametrize("kind", ["known_diff", "value_filter", "not_found", "absence_shape"])
def test_a_predicate_using_every_declared_variable_compiles(kind: str) -> None:
    source = " && ".join(f"{name} == {name}" for name in CEL_VARIABLES[kind])  # type: ignore[index]
    assert cel_error(source, kind) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "source", ['{"a": b}', '(\n  $r := a;\n  {"x": $r}\n)', 'points[kind = "EMAIL"][0].v']
)
def test_valid_jsonata(source: str) -> None:
    assert jsonata_error(source) is None


@pytest.mark.parametrize("source", ['{"a": ', "foo(", ")", '"unterminated'])
def test_invalid_jsonata(source: str) -> None:
    problem = jsonata_error(source)
    assert problem is not None and problem.startswith("S0") and "at character" in problem


@pytest.mark.parametrize(
    ("pattern", "ok"),
    [("^C[0-9]{8}$", True), (r"^\d+$", True), ("^C[0-9", False), ("*a", False), ("a{2,1}", False)],
)
def test_key_regex(pattern: str, ok: bool) -> None:
    problem = regex_error(pattern)
    assert problem is None if ok else problem.startswith("invalid regular expression")
