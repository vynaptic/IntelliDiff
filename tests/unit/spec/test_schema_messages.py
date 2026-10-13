"""Schema diagnostics: the intended oneOf branch, precise pointers, and no document values in messages."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from deltasleuth.core.spec import SpecIssue, validate_spec

Example = Callable[[str], dict[str, Any]]
_RULE = {
    "id": "KD-1",
    "when": "true",
    "classification": "accepted",
    "reason": "a long reason",
    "ticket": "T-1",
}


def _schema_issues(doc: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(i.pointer, i.code, i.message) for i in validate_spec(doc) if i.code.startswith("schema.")]


def test_an_unknown_member_is_reported_in_the_intended_branch(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["sides"]["a"]["selectt"] = "$.x"
    minimal_spec["key"]["source"]["keyz"] = []
    assert _schema_issues(minimal_spec) == [
        ("/key/source/keyz", "schema.additionalProperties", "unexpected member 'keyz'"),
        ("/sides/a/selectt", "schema.additionalProperties", "unexpected member 'selectt'"),
    ]


def test_alternatives_that_differ_by_one_member(minimal_spec: dict[str, Any], example: Example) -> None:
    minimal_spec["sides"]["a"]["truncation"] = {"cap": 10}
    assert _schema_issues(minimal_spec) == [
        ("/sides/a/truncation", "schema.anyOf", "requires one of: countPath, itemsPath")
    ]
    doc = example("pg-vs-oracle-tables")
    del doc["sides"]["a"]["table"]
    assert [(p, m) for p, _, m in _schema_issues(doc)] == [("/sides/a", "requires one of: table, template")]


@pytest.mark.parametrize(
    ("location", "value", "pointer", "code"),
    [
        ("compare/fields/0/normalize", ["trimm"], "/compare/fields/0/normalize/0", "schema.enum"),
        (
            "compare/fields/0/normalize",
            [{"e164": {"defaultRegion": "gb"}}],
            "/compare/fields/0/normalize/0/e164/defaultRegion",
            "schema.pattern",
        ),
        (
            "compare/fields/0/normalize",
            [{"timestamp_utc": {}}],
            "/compare/fields/0/normalize/0/timestamp_utc",
            "schema.required",
        ),
        (
            "compare/fields/0/normalize",
            [{"trim": {}}],
            "/compare/fields/0/normalize/0/trim",
            "schema.additionalProperties",
        ),
        (
            "compare/fields/1/compare",
            {"tolerance": {"abs": -1}},
            "/compare/fields/1/compare/tolerance/abs",
            "schema.minimum",
        ),
        (
            "compare/fields/1/compare",
            {"tolerance": {}},
            "/compare/fields/1/compare/tolerance",
            "schema.anyOf",
        ),
        (
            "compare/fields/1/compare",
            {"similarity": {"threshold": 1.5}},
            "/compare/fields/1/compare/similarity/threshold",
            "schema.maximum",
        ),
        ("compare/fields/1/compare", "exactly", "/compare/fields/1/compare", "schema.const"),
        ("compare/strategy", "auto", "/compare/strategy", "schema.not"),  # a forbidden member
        ("sides/a/request/body", {"id": "{{ key }}"}, "/sides/a/request/body", "schema.not"),  # a GET body
        ("sides/a/notFound/status", [500], "/sides/a/notFound/status/0", "schema.enum"),
        ("knownDiffs", [dict(_RULE, expires="2027-02-30")], "/knownDiffs/0/expires", "schema.format"),
        ("execution", {"stop": {"until": "2026-13-01T00:00:00Z"}}, "/execution/stop/until", "schema.format"),
        ("execution", {"stop": {"until": "tomorrow"}}, "/execution/stop/until", "schema.format"),
    ],
)
def test_errors_point_inside_the_intended_branch(
    minimal_spec: dict[str, Any], location: str, value: Any, pointer: str, code: str
) -> None:
    _set(minimal_spec, location, value)
    assert (pointer, code) in {(p, c) for p, c, _ in _schema_issues(minimal_spec)}


def test_forbidden_member_message_explains_the_rule(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["compare"]["strategy"] = "auto"
    ((pointer, code, message),) = _schema_issues(minimal_spec)
    assert (pointer, code) == ("/compare/strategy", "schema.not")
    assert "only when both sides are tables" in message


def test_similarity_severity_cap(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["compare"]["fields"][1] = {
        "path": "$.name",
        "compare": {"similarity": {"threshold": 0.9}},
        "severity": "CRITICAL",
    }
    assert ("/compare/fields/1/severity", "schema.enum") in {
        (p, c) for p, c, _ in _schema_issues(minimal_spec)
    }


def test_valid_date_time_with_long_fraction(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["execution"] = {"stop": {"until": "2026-09-27T06:00:00.123456789+01:00"}}
    assert _schema_issues(minimal_spec) == []


_CREDENTIAL_LIKE = "Bearer s3cr3t-t0ken-value"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["metadata"].update(version=_CREDENTIAL_LIKE),
        lambda d: d["sides"]["a"].update(role=_CREDENTIAL_LIKE),
        lambda d: d["sides"]["a"]["request"].update(method=_CREDENTIAL_LIKE),
        lambda d: d["sides"]["a"]["request"]["headers"].update({"X-A": [_CREDENTIAL_LIKE]}),
        lambda d: d["sides"]["a"]["request"]["headers"].update({"X-B": _CREDENTIAL_LIKE}),
        lambda d: d["key"]["source"].update(keys=[_CREDENTIAL_LIKE, _CREDENTIAL_LIKE]),
        lambda d: d["recheck"].update(times=_CREDENTIAL_LIKE),
        lambda d: d.update(knownDiffs=[dict(_RULE, ticket=_CREDENTIAL_LIKE)]),
    ],
)
def test_messages_never_quote_document_values(
    minimal_spec: dict[str, Any], mutate: Callable[..., None]
) -> None:
    mutate(minimal_spec)
    issues = validate_spec(minimal_spec)
    assert issues, "the mutation must produce at least one issue"
    assert not any("s3cr3t" in issue.message for issue in issues)


# -- robustness: rules never assume shapes the schema has already rejected -------------------------

_WRONG_VALUES: list[Any] = [None, 7, "text", [], {}, [1, "x"], {"x": 1}, True]
_LOCATIONS = """
    key key/source key/source/keys key/normalize key/pattern key/parts
    sides sides/a sides/a/request sides/a/request/headers sides/a/request/query sides/a/request/path
    sides/a/notFound sides/a/notFound/status sides/a/truncation sides/b/map sides/b/select
    compare compare/arrays compare/fields compare/valueFilter knownDiffs security domainPack
    canonical time noise identity inferredSchema
""".split()


@pytest.mark.parametrize("location", _LOCATIONS)
def test_wrongly_typed_documents_never_crash(
    minimal_spec: dict[str, Any], things_targets: Any, location: str
) -> None:
    for value in _WRONG_VALUES:
        doc = copy.deepcopy(minimal_spec)
        _set(doc, location, value)
        assert all(isinstance(issue, SpecIssue) for issue in validate_spec(doc, targets=things_targets))


def test_wrongly_typed_example_members_never_crash(example: Example) -> None:
    for stem in ("generic-customer-services", "entity-linking-services", "pg-vs-oracle-tables"):
        original = example(stem)
        for member in original:
            for value in _WRONG_VALUES:
                validate_spec({**copy.deepcopy(original), member: value})


def _set(doc: Any, location: str, value: Any) -> None:
    *parents, last = (int(step) if step.isdigit() else step for step in location.split("/"))
    for step in parents:
        doc = doc[step]
    doc[last] = value
