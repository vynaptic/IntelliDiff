"""Loader rules report a precise pointer and code; MVP gating rejects deferred features, never silently."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth.connectors.templating import template_variables
from deltasleuth.core.spec import (
    IGNORED,
    RENDERED_PLACEHOLDERS,
    SpecError,
    SpecIssue,
    TargetRegistry,
    compile_spec,
    key_table,
    load_spec_file,
    validate_spec,
)
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    DatabaseTarget,
    NotFoundRule,
    Side,
    SideConfig,
    TableConfig,
    Target,
    TargetCapabilities,
)
from deltasleuth.runner.keys import normalise_keys

Example = Callable[[str], dict[str, Any]]
_RULE = {
    "id": "KD-1",
    "when": "true",
    "classification": "accepted",
    "reason": "a long reason",
    "ticket": "THING-1",
}
_LISTING = {
    "kind": "paginated_endpoint",
    "target": "things-v1",
    "request": {"method": "GET", "path": "/v1/things"},
    "pagination": {"kind": "cursor", "itemsPath": "$.items", "cursorPath": "$.next", "cursorParam": "after"},
    "keyPath": "$.id",
}


def _errors(doc: Any, **kwargs: Any) -> set[tuple[str, str]]:
    return {(i.pointer, i.code) for i in validate_spec(doc, **kwargs) if i.severity == "error"}


def _warnings(doc: Any, **kwargs: Any) -> set[tuple[str, str]]:
    return {(i.pointer, i.code) for i in validate_spec(doc, **kwargs) if i.severity == "warning"}


def _set(doc: Any, location: str, value: Any) -> dict[str, Any]:
    """Set (or append, at a list's end) the value at ``location``, creating missing objects."""
    *parents, last = (int(step) if step.isdigit() else step for step in location.split("/"))
    node = doc
    for step in parents:
        node = node[step] if isinstance(step, int) else node.setdefault(step, {})
    if isinstance(node, list) and last == len(node):
        node.append(value)
    else:
        node[last] = value
    return doc


def test_minimal_spec_is_clean(minimal_spec: dict[str, Any], things_targets: TargetRegistry) -> None:
    assert validate_spec(minimal_spec, targets=things_targets) == []
    assert validate_spec(["not", "a", "spec"]) == [
        SpecIssue("", "the spec document must be a mapping", "document_not_mapping")
    ]


@pytest.mark.parametrize(
    ("location", "value", "pointer", "code"),
    [
        ("sides/b/select", "$.data[", "/sides/b/select", "invalid_jsonpath"),
        (
            "sides/b/truncation/countPath",
            "$.meta[?@.x ==]",
            "/sides/b/truncation/countPath",
            "invalid_jsonpath",
        ),
        ("compare/fields/0/path", "$.parts[*", "/compare/fields/0/path", "invalid_jsonpath"),
        ("compare/fields/0/path", "$.parts[0].sku", "/compare/fields/0/path", "path_not_pattern"),
        ("compare/fields/0/path", "$..sku", "/compare/fields/0/path", "path_not_pattern"),
        ("compare/fields/0/path", "$.parts[?@.sku].qty", "/compare/fields/0/path", "path_not_pattern"),
        ("compare/fields/0/path", "$['parts','x']", "/compare/fields/0/path", "path_not_pattern"),
        ("key/source", {**_LISTING, "keyPath": "$.id["}, "/key/source/keyPath", "invalid_jsonpath"),
        ("sides/b/map", '{"id": id, "parts": [components.{"sku": code}', "/sides/b/map", "invalid_jsonata"),
        ("knownDiffs", [dict(_RULE, when="path == '$.x' &&")], "/knownDiffs/0/when", "invalid_cel"),
        ("compare/valueFilter", "change_type != 'missing_in_b'", "/compare/valueFilter", "invalid_cel"),
        ("sides/a/notFound/when", "'absent'", "/sides/a/notFound/when", "invalid_cel"),  # not a bool
        ("key/pattern", "^T[0-9{4}$", "/key/pattern", "invalid_key_pattern"),
        ("compare/arrays/0/key", ["sku", "sku"], "/compare/arrays/0/key", "duplicate_array_key"),
        (
            "compare/arrays/1",
            {"path": "$['parts']", "mode": "set"},
            "/compare/arrays/1/path",
            "duplicate_array_path",
        ),
        (
            "compare/fields/2",
            {"path": '$["parts"][*]["sku"]'},
            "/compare/fields/2/path",
            "duplicate_field_path",
        ),
        ("compare/arrays/0/mode", "set", "/compare/arrays/0/key", "schema.not"),  # a key on an unkeyed array
        ("knownDiffs", [_RULE, _RULE], "/knownDiffs/1/id", "duplicate_known_diff_id"),
        (
            "security",
            {"tokenize": [{"path": "$.parts[*].sku", "class": "identifier"}], "clear": ["$['parts'][*].sku"]},
            "/security/clear/0",
            "security_path_conflict",
        ),
        (
            "sides/a/request/path",
            "//evil.example/things/{{ key }}",
            "/sides/a/request/path",
            "schema.pattern",
        ),
    ],
)
def test_rule_errors(
    minimal_spec: dict[str, Any], location: str, value: Any, pointer: str, code: str
) -> None:
    assert (pointer, code) in _errors(_set(minimal_spec, location, value))


def test_predicates_over_every_declared_variable_validate(minimal_spec: dict[str, Any]) -> None:
    # The loader and the engine share CEL_VARIABLES; these forms must validate.
    minimal_spec["sides"]["a"]["notFound"]["when"] = "response.status == 200 && !has(response.body.data)"
    minimal_spec["compare"]["valueFilter"] = (
        "!pointer.startsWith('/tags') && (size(strata) == 0 || stratum == null)"
    )
    when = "link_reason == null && hop_count == null && attrs == {} && pointer != ''"
    minimal_spec["knownDiffs"] = [dict(_RULE, when=when)]
    assert _errors(minimal_spec) == set()
    minimal_spec["sides"]["a"]["notFound"]["when"] = "body.status"  # a DYN result is decided at runtime
    assert _errors(minimal_spec) == set()


def test_cel_of_a_key_source_is_checked(example: Example) -> None:
    doc = example("entity-linking-services")
    doc["key"]["source"]["signoffStratum"] = "strata.flag_status == 'FLAGGED'"  # must return a string
    assert ("/key/source/signoffStratum", "invalid_cel") in _errors(doc)


def test_path_literal_not_canonical_is_a_warning(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["compare"]["valueFilter"] = "path != \"$['parts'][*]\" && '$.parts[0]' != path"
    assert _warnings(minimal_spec) == {("/compare/valueFilter", "path_literal_not_canonical")}
    assert _errors(minimal_spec) == set()


# -- keys -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["T12", "..", " . ", "T00\n01", "X0001"])
def test_inline_key_must_match_pattern_after_normalisation(minimal_spec: dict[str, Any], raw: str) -> None:
    minimal_spec["key"]["source"]["keys"] = ["T0001", raw]
    assert ("/key/source/keys/1", "invalid_key") in _errors(minimal_spec)


def test_inline_keys_repeating_after_normalisation_warn(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["key"]["source"]["keys"] = ["T0001", " t0001"]
    assert _warnings(minimal_spec) == {("/key/source/keys/1", "duplicate_key")}
    del minimal_spec["key"]["pattern"]
    assert ("/key", "schema.required") in _errors(minimal_spec)  # a service spec needs key.pattern


@pytest.mark.parametrize("raw", [1000.0, 2.0])
def test_float_inline_key_rejected(minimal_spec: dict[str, Any], raw: float) -> None:
    # The schema accepts an integral float as an integer, but its written text (1E3, 2.0) is
    # gone; the run used to request "1000.0".
    minimal_spec["key"]["pattern"] = "^[0-9.E]+$"
    minimal_spec["key"]["source"]["keys"] = ["12", raw]
    assert _errors(minimal_spec) == {("/key/source/keys/1", "invalid_key")}


def test_file_keys_are_compared_as_written(
    minimal_spec: dict[str, Any], tmp_path: Path, write_yaml: Callable[[str, Any], Path]
) -> None:
    # JSON keeps an integer's exact text; YAML inline keys keep the text they were written with.
    minimal_spec["key"].update(pattern="^[0-9A-Fa-fEeox.]+$", normalize=[])
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(minimal_spec).replace('["T0001", " t0002 "]', "[12001, 1E3]"), "utf-8")
    doc = load_spec_file(path)
    assert doc["key"]["source"]["keys"] == [12001, 1000.0]
    assert _errors(doc) == {("/key/source/keys/1", "invalid_key")}
    text = yaml.safe_dump(minimal_spec, sort_keys=False).replace(
        "- T0001\n    - ' t0002 '", "- 00012001\n    - 1E3\n    - 0x1F\n    - 2.0"
    )
    doc = load_spec_file(write_yaml("spec.yaml", text))
    assert doc["key"]["source"]["keys"] == ["00012001", "1E3", "0x1F", "2.0"]
    assert _errors(doc) == set()


# -- targets and 2xx not-found ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda d: d["sides"]["b"].update(notFound={"status": [404]}),
            {("/sides/b/notFound", "not_found_undeclared")},
        ),
        (
            lambda d: _set(d, "sides/a/notFound/status", [404, 410]),
            {("/sides/a/notFound/status/1", "not_found_undeclared")},
        ),
        (lambda d: d["sides"]["a"].pop("truncation"), {("/sides/a", "truncation_undeclared")}),
        (lambda d: d["sides"]["b"].pop("truncation"), set()),  # the target declares truncation
        (lambda d: _set(d, "sides/a/target", "things-v9"), {("/sides/a/target", "unknown_target")}),
        (
            lambda d: _set(d, "key/source", dict(_LISTING, target="things-v9")),
            {("/key/source/target", "unknown_target")},
        ),
    ],
)
def test_target_rules(
    minimal_spec: dict[str, Any],
    things_targets: TargetRegistry,
    mutate: Callable[..., Any],
    expected: set[Any],
) -> None:
    mutate(minimal_spec)
    assert _errors(minimal_spec, targets=things_targets) == expected
    assert _errors(minimal_spec) == set()  # the target rules need a registry


def test_2xx_not_found_needs_when_and_a_target_declaring_it(minimal_spec: dict[str, Any]) -> None:
    graphql = TargetCapabilities(
        authoritative_absence=AuthoritativeAbsence(status=(404, 200), shapes=("null_data",)),
        absence_shapes={"null_data": "body.data.thing == null"},
    )
    targets = TargetRegistry(
        {
            "things-v1": Target("things-v1", "https://v1.example", capabilities=graphql),
            "things-v2": Target("things-v2", "https://v2.example", capabilities=TargetCapabilities()),
        }
    )
    minimal_spec["sides"]["a"]["notFound"] = {"status": [404, 200]}
    assert _errors(minimal_spec) == {("/sides/a/notFound/status/1", "not_found_2xx_needs_when")}
    minimal_spec["sides"]["a"]["notFound"]["when"] = "has(body.data) && body.data.thing == null"
    assert _errors(minimal_spec, targets=targets) == set()
    minimal_spec["sides"]["b"]["notFound"] = {"status": [200], "when": "!has(body.data)"}
    assert ("/sides/b/notFound", "not_found_undeclared") in _errors(minimal_spec, targets=targets)


# -- credentials and request templates ------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "value", "pointer"),
    [
        (
            "sides/a/request/headers/X-Upstream",
            "Bearer eyJhbGciOi.secretpart",
            "/sides/a/request/headers/X-Upstream",
        ),
        ("sides/a/request/query", {"q": "Basic dXNlcjpwYXNz"}, "/sides/a/request/query/q"),
        ("sides/a/request/query", {"q": ["ok", "Digest username=x"]}, "/sides/a/request/query/q/1"),
        (
            "sides/b/request",
            {
                "method": "POST",
                "path": "/v2/q",
                "body": {"context": [{"a": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"}]},
            },
            "/sides/b/request/body/context/0/a",
        ),
        (
            "key/source",
            dict(_LISTING, request={"method": "GET", "path": "/v1", "query": {"t": "token abc123"}}),
            "/key/source/request/query/t",
        ),
    ],
)
def test_inline_credentials_are_errors_that_never_quote_them(
    minimal_spec: dict[str, Any], location: str, value: Any, pointer: str
) -> None:
    issues = validate_spec(_set(minimal_spec, location, value))
    assert (pointer, "inline_credential") in {(i.pointer, i.code) for i in issues}
    assert not any("secretpart" in i.message or "abc123" in i.message for i in issues)


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("sides/a/request/headers/Authorization", "schema.propertyNames"),  # reported once, by the schema
        ("sides/a/request/headers/Cookie", "schema.propertyNames"),
        ("sides/a/request/headers/X-Api-Key", "schema.propertyNames"),
        ("sides/a/request/headers/X-Session-Token", "credential_header"),
        ("sides/a/request/headers/X_Client_Secret", "credential_header"),
        ("sides/a/request/headers/Proxy-Credentials", "credential_header"),
        ("sides/a/request/query/api_key", "schema.propertyNames"),
        ("sides/a/request/query/refresh_token", "schema.propertyNames"),  # as in bodies (secretLikeName)
    ],
)
def test_credential_member_names(minimal_spec: dict[str, Any], location: str, code: str) -> None:
    assert _errors(_set(minimal_spec, location, "x")) == {("/" + location, code)}


def test_credential_body_member_name_and_allowed_headers(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["sides"]["a"]["request"]["headers"]["X-Idempotency-Token"] = "ds-{{ run_id }}"
    assert _errors(minimal_spec) == set()
    minimal_spec["sides"]["b"]["request"] = {
        "method": "POST",
        "path": "/v2/q",
        "body": {"filter": {"clientSecret": "x"}},
    }
    assert _errors(minimal_spec) == {("/sides/b/request/body/filter/clientSecret", "schema.propertyNames")}


@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("sides/a/request/path", "/v1/things/{{ secret }}"),
        ("sides/a/request/headers/X-Trace", "{{ env.TOKEN }}"),
        ("sides/a/request/query/r", "{{keyParts.region}}"),  # the key declares no parts
    ],
)
def test_unknown_placeholder(minimal_spec: dict[str, Any], location: str, value: str) -> None:
    assert ("/" + location, "unknown_placeholder") in _errors(_set(minimal_spec, location, value))


# -- domain packs and taxonomy --------------------------------------------------------------------


def _with_pack(spec: dict[str, Any], **config: Any) -> dict[str, Any]:
    base = {"hopDepth": 1, "rollUpMembersToParent": False, "linkingIdentifierTypes": ["PHONE"]}
    spec["domainPack"] = {"name": "entity-linking", "version": "1.0.0", "config": {**base, **config}}
    return spec


def test_pack_config_is_flagged_as_not_applied(minimal_spec: dict[str, Any]) -> None:
    # Only a pack's taxonomy codes and aliases apply; each configured behaviour is flagged.
    _with_pack(
        minimal_spec,
        existenceDoubleCheck={"enabled": True, "methods": ["lookup_by_code"]},
        validityFilter={"mode": "current"},
        hotIdentifiers={"degreeCap": 5},
        provenance={"side": "a", "viaField": "via"},
    )
    members = minimal_spec["domainPack"]["config"]
    assert {(i.pointer, i.code, i.severity) for i in validate_spec(minimal_spec)} == {
        (f"/domainPack/config/{member}", "ignored", "warning") for member in members
    }
    assert compile_spec(minimal_spec).domain_pack == "entity-linking"


@pytest.mark.parametrize(
    ("methods", "ok"),
    [(["lookup_by_code", "search_v2"], True), (["Index"], False), (["primary"], False), (["a" * 33], False)],
)
def test_existence_probe_names(minimal_spec: dict[str, Any], methods: list[str], ok: bool) -> None:
    # Probe names the target declares (no backend vocabulary); "primary" names the ordinary read.
    _with_pack(minimal_spec, existenceDoubleCheck={"enabled": True, "methods": methods})
    errors = {pointer for pointer, _ in _errors(minimal_spec)}
    assert errors == (set() if ok else {"/domainPack/config/existenceDoubleCheck/methods/0"})


def test_pack_rules(minimal_spec: dict[str, Any], example: Example) -> None:
    minimal_spec["domainPack"] = {"name": "other-pack", "version": "1.0.0", "config": {}}
    assert _errors(minimal_spec) == {("/domainPack/name", "unknown_domain_pack")}
    doc = example("entity-linking-services")
    config = doc["domainPack"]["config"]
    config.update(hopDepth=9, linksPath="edges.LINKED_TO")  # linksPath is a core $defs/jsonPath
    del config["rollUpMembersToParent"]
    config["validityFilter"] = {"mode": "as_of", "clockSkew": "5 minutes"}  # as_of needs asOf
    config["provenance"]["unknown"] = True
    doc["domainPack"]["version"] = "2.0.0"
    assert {
        ("/domainPack/version", "unsupported_domain_pack_version"),
        ("/domainPack/config/hopDepth", "schema.maximum"),
        ("/domainPack/config", "schema.required"),
        ("/domainPack/config/validityFilter", "schema.required"),
        ("/domainPack/config/validityFilter/clockSkew", "schema.pattern"),
        ("/domainPack/config/provenance/unknown", "schema.additionalProperties"),
        ("/domainPack/config/linksPath", "schema.pattern"),
    } <= _errors(doc)
    del doc["domainPack"]["config"]
    assert ("/domainPack/config", "schema.required") in _errors(doc)  # an absent config is validated as {}


@pytest.mark.parametrize(
    ("location", "code", "pack", "expected"),
    [
        ("fields/0/taxonomyCode", "SEGMENT_MISMATCH", False, "unknown_taxonomy_code"),  # a typo
        ("fields/0/taxonomyCode", "FLAG_MISMATCH", False, "unknown_taxonomy_code"),  # no domainPack
        ("fields/0/taxonomyCode", "NO_SUCH_CODE", True, "unknown_taxonomy_code"),
        ("arrays/0/taxonomy/missing_in_b", "MISSING_LINKS", True, "unknown_taxonomy_code"),
        ("fields/0/taxonomyCode", "FIELD_MISSING", True, "taxonomy_presence_mismatch"),
        ("fields/0/taxonomyCode", "EXTRA_LINK", True, "taxonomy_presence_mismatch"),
        ("arrays/0/taxonomy/missing_in_b", "FIELD_EXTRA", True, "taxonomy_presence_mismatch"),
        ("arrays/0/taxonomy/missing_in_b", "VALUE_MISMATCH", True, "taxonomy_presence_mismatch"),
        ("arrays/0/taxonomy/extra_in_b", "MISSING_LINK", True, "taxonomy_presence_mismatch"),
    ],
)
def test_taxonomy_codes_must_exist_and_fit_their_rule(
    minimal_spec: dict[str, Any], location: str, code: str, pack: bool, expected: str
) -> None:
    # The run used to stop with a TaxonomyError on the first matching finding.
    if pack:
        _with_pack(minimal_spec)
    _set(minimal_spec["compare"], location, code)
    assert _errors(minimal_spec) == {(f"/compare/{location}", expected)}


def test_taxonomy_codes_that_fit_their_rules(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["compare"]["fields"][0]["taxonomyCode"] = "VALUE_MISMATCH"
    minimal_spec["compare"]["arrays"][0]["taxonomy"] = {
        "missing_in_b": "FIELD_MISSING",
        "extra_in_b": "FIELD_EXTRA",
    }
    assert _errors(minimal_spec) == set()
    _with_pack(minimal_spec)
    minimal_spec["compare"]["fields"][1]["taxonomyCode"] = "FLAG_MISMATCH"
    minimal_spec["compare"]["arrays"][0]["taxonomy"] = {
        "missing_in_b": "MISSING_LINK",
        "extra_in_b": "EXTRA_ENTITY",
    }
    compile_spec(minimal_spec)


# -- MVP scope ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "value", "pointer"),
    [
        ("time", {"mode": "frozen"}, "/time/mode"),
        ("time", {"mode": "live", "watermark": {"a": "$.meta.offset", "b": "$.offset"}}, "/time/watermark"),
        ("noise", {"calibration": {"sampleSize": 10}}, "/noise/calibration"),
        (
            "noise",
            {"masks": [{"path": "$.updatedAt", "side": "b", "reason": "stamped per read"}]},
            "/noise/masks",
        ),
        # Nothing reads a CSV object, so the declared population is never replaced silently.
        (
            "key/source",
            {"kind": "csv", "uri": "s3://bucket/keys.csv", "column": "thing_id"},
            "/key/source/kind",
        ),
        ("key/source", {"kind": "run_keys", "runId": 7}, "/key/source/kind"),
        (
            "key/source",
            {"kind": "union", "sources": [{"kind": "list", "keys": ["T0001"]}, _LISTING]},
            "/key/source/kind",
        ),
        ("key/source", {"kind": "table", "side": "a"}, "/key/source/kind"),  # only a hash sample's frame
        ("sampling", {"mode": "hash", "rate": 0.1, "salt": "mvp-sample"}, "/sampling/mode"),
        (
            "identity",
            {"crosswalk": {"target": "xwalk-ro", "template": {"id": "xwalk", "version": 1}}},
            "/identity/crosswalk",
        ),
    ],
)
def test_deferred_features_are_unsupported(
    minimal_spec: dict[str, Any], location: str, value: Any, pointer: str
) -> None:
    _set(minimal_spec, location, value)
    issues = validate_spec(minimal_spec)
    assert [(i.pointer, i.code, i.severity) for i in issues] == [(pointer, "unsupported", "error")]
    assert issues[0].message.startswith("not supported: ")
    with pytest.raises(SpecError) as exc:
        compile_spec(minimal_spec)
    assert exc.value.issues == tuple(issues)


def test_the_inferred_schema_is_ignored_and_pause_needs_a_table_side(minimal_spec: dict[str, Any]) -> None:
    """The inferred-schema guard for service sides is not built, and saying so is
    all a spec author can act on; ``schemaDrift`` stands without ``inferredSchema``, and between two
    service sides it may not ask for a pause."""
    minimal_spec["schemaDrift"] = {"mode": "finding"}
    assert validate_spec(minimal_spec) == []  # schemaDrift needs no inferredSchema beside it
    minimal_spec["inferredSchema"] = {"ref": "things-v1-vs-v2@1"}
    (issue,) = validate_spec(minimal_spec)
    assert (issue.pointer, issue.code) == ("/inferredSchema", IGNORED)
    assert issue.message == (
        "/inferredSchema is not applied: the inferred-schema guard for service sides is not built;"
        " schemaDrift acts on table sides only"
    )
    del minimal_spec["inferredSchema"]
    minimal_spec["schemaDrift"] = {"mode": "pause"}
    (refused,) = validate_spec(minimal_spec)
    assert (refused.pointer, refused.code) == ("/schemaDrift/mode", "unsupported")
    assert refused.message == "not supported: pausing a run on schema drift between two service sides"


def test_raw_clear_is_ignored_with_a_warning(minimal_spec: dict[str, Any]) -> None:
    # Nothing shows a raw body, so rawClear has no effect; it must not pass silently.
    minimal_spec["sides"]["a"]["rawClear"] = ["$.thing.status"]
    assert [(i.pointer, i.code) for i in validate_spec(minimal_spec)] == [("/sides/a/rawClear", IGNORED)]


@pytest.mark.parametrize("name", ["cycle_no", "keyParts.region"])
@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("sides/a/request/path", "/v1/things/{}"),
        ("sides/a/request/headers/X-Part", "{}"),
        ("sides/a/request/query/p", ["x", "{}"]),
        ("sides/a/request/body", {"filter": {"p": "{}"}}),
    ],
)
def test_placeholders_the_connectors_do_not_render_are_unsupported(
    minimal_spec: dict[str, Any], location: str, value: Any, name: str
) -> None:
    # Requests render key, run_id and call_seq only; the connector used to refuse the others
    # on the first live request, after the spec had compiled. A key that is not composite has one part:
    # the parts it names are no placeholders.
    minimal_spec["key"]["parts"] = ["region", "code"]
    minimal_spec["sides"]["a"]["request"]["method"] = "POST"
    _set(minimal_spec, location, json.loads(json.dumps(value).replace("{}", f"{{{{ {name} }}}}")))
    pointer = (
        "/"
        + location
        + {"sides/a/request/query/p": "/1", "sides/a/request/body": "/filter/p"}.get(location, "")
    )
    assert _errors(minimal_spec) == {(pointer, "unsupported")}


def test_rendered_placeholders_match_what_connectors_render() -> None:
    assert set(template_variables("T0001", "run-1", 3)) == RENDERED_PLACEHOLDERS


@pytest.mark.parametrize(
    ("location", "value", "pointer"),
    [
        ("reporting", {"formats": ["csv", "pdf"]}, "/reporting/formats"),
        ("reporting", {"fields": ["key", "outcome"]}, "/reporting/fields"),
        ("reporting", {"interim": {"everyCycles": 6}}, "/reporting/interim"),
        ("time", {"mode": "live", "skewBudget": "30s"}, "/time/skewBudget"),
        (
            "sampling",
            {"mode": "all", "strata": [{"name": "segment", "column": "segment"}]},
            "/sampling/strata",
        ),
    ],
)
def test_accepted_features_not_applied_are_flagged(
    minimal_spec: dict[str, Any], location: str, value: Any, pointer: str
) -> None:
    # Accepted, never blocking, never silent.
    _set(minimal_spec, location, value)
    assert [(i.pointer, i.code, i.severity) for i in validate_spec(minimal_spec)] == [
        (pointer, "ignored", "warning")
    ]
    compile_spec(minimal_spec)


@pytest.mark.parametrize("retention", ["standard", "audit"])
def test_a_retention_class_is_applied_not_ignored(minimal_spec: dict[str, Any], retention: str) -> None:
    # The purge applies the class, so the loader no longer flags it.
    minimal_spec["retention"] = retention
    assert validate_spec(minimal_spec) == []
    assert compile_spec(minimal_spec).raw["retention"] == retention


@pytest.mark.parametrize("value_class", ["account_number", "identifier"])
def test_inline_keys_of_a_pii_class_are_flagged_as_stored_in_clear(
    minimal_spec: dict[str, Any], value_class: str
) -> None:
    # The spec keeps them in clear and is never purged.
    minimal_spec["key"]["class"] = value_class
    [issue] = validate_spec(minimal_spec)
    assert (issue.pointer, issue.code, issue.severity) == (
        "/key/source/keys",
        "inline_keys_stored",
        "warning",
    )
    assert issue.message == (
        f"inline keys of class {value_class} are stored in clear with the spec and never purged; pass them"
        " with 'ds run --keys'"
    )
    minimal_spec["key"]["source"] = {"kind": "hash_sample", "source": minimal_spec["key"]["source"]}
    assert ("/key/source/source/keys", "inline_keys_stored") in _warnings(minimal_spec)
    minimal_spec["key"]["class"] = "business_code"  # codes are not PII: shown in clear everywhere
    assert "inline_keys_stored" not in {i.code for i in validate_spec(minimal_spec)}


@pytest.mark.parametrize("pattern", ["", "^[A-Z0-9]*$", "T[0-9]{4}|"])
def test_keys_of_a_value_class_that_their_pattern_does_not_tell_are_warned_of(
    minimal_spec: dict[str, Any], pattern: str
) -> None:
    # A pack's gate refuses every run of such a spec, and nothing said so before its
    # cycle was signed off. A warning: the spec stays valid, and runs.
    minimal_spec["key"] |= {"class": "identifier", "pattern": pattern}
    told = [issue for issue in validate_spec(minimal_spec) if issue.code == "key_pattern_empty"]
    assert [(issue.pointer, issue.severity, issue.message) for issue in told] == [
        (
            "/key/pattern",
            "warning",
            "key.pattern matches the empty string: a raw key cannot be told from other text, so no evidence"
            " pack is issued for a run of this spec; declare a pattern that fits its"
            " keys only, or class keys that are not personal business_code",
        )
    ]
    assert compile_spec(minimal_spec).key.pattern == pattern
    minimal_spec["key"]["class"] = "business_code"  # shown in clear by design: never searched for
    assert validate_spec(minimal_spec) == []
    del minimal_spec["key"]["pattern"]  # beside a service side the schema requires one: said once
    minimal_spec["key"]["class"] = "identifier"
    assert [(i.pointer, i.code) for i in validate_spec(minimal_spec) if "pattern" in i.message] == [
        ("/key", "schema.required")
    ]


def test_mvp_features_compile_without_issues(
    minimal_spec: dict[str, Any], things_targets: TargetRegistry
) -> None:
    # The MVP scope: stop conditions, soak pacing, recheck, known diffs, security,
    # includeSuppressed, POST templates and a paginated key listing are all supported.
    minimal_spec["key"]["source"] = _LISTING
    minimal_spec["sides"]["b"]["request"] = {
        "method": "POST",
        "path": "/graphql",
        "body": {"id": "{{ key }}"},
    }
    minimal_spec["execution"] = {
        "cycleInterval": "1h",
        "stop": {
            "maxKeys": 5,
            "maxCycles": 1,
            "maxDuration": "10m",
            "until": "2030-01-01T00:00:00Z",
            "maxErrorRate": {"rate": 0.1, "window": 10},
            "maxInconclusiveRate": {"rate": 0.5, "window": 10},
            "maxConsecutiveErrors": 2,
            "maxUnexplainedCritical": 3,
        },
    }
    minimal_spec["recheck"] = {"delay": "30s", "times": 2, "jitter": "5s"}
    minimal_spec["knownDiffs"] = [_RULE]
    minimal_spec["security"] = {"tokenize": [{"path": "$.id", "class": "identifier"}], "unlisted": "tokenize"}
    minimal_spec["reporting"] = {"includeSuppressed": True}
    minimal_spec["sampling"] = {"mode": "all"}
    assert validate_spec(minimal_spec, targets=things_targets) == []
    assert compile_spec(minimal_spec, targets=things_targets).sides[Side.B].request.method == "POST"
    with pytest.raises(SpecError):
        compile_spec({**minimal_spec, "compare": {"valueFilter": "("}})  # errors always block


def test_a_spec_runs_only_with_the_normaliser_version_it_declares(
    minimal_spec: dict[str, Any], things_targets: TargetRegistry
) -> None:
    minimal_spec["normalizerVersion"] = "1.0.0"
    assert _errors(minimal_spec, targets=things_targets) == {("/normalizerVersion", "unsupported")}
    assert _errors(minimal_spec) == set()  # a stored spec still reads, so its runs' history stays visible


def test_blanket_clear_is_a_warning_not_an_error(minimal_spec: dict[str, Any]) -> None:
    # `unlisted: clear` is accepted but never silently: under SSO its runs wait for an approval.
    issues = validate_spec(minimal_spec | {"security": {"unlisted": "clear"}})
    assert [(i.pointer, i.code, i.severity, i.message) for i in issues] == [
        (
            "/security/unlisted",
            "unapproved_clear",
            "warning",
            "under SSO a run needs a four-eyes approval of unlisted_clear",
        )
    ]


# -- multi-part keys ------------------------------------------------------------------------------

PARTS = ("region", "shelf", "code")


def _multi_part(spec: dict[str, Any]) -> dict[str, Any]:
    """``minimal_spec`` keyed by three parts: side a takes two in its path and one in its query, side b one
    in a POST body beside the joined key."""
    spec["key"] = {
        "name": "shelf_item",
        "type": "composite",
        "parts": list(PARTS),
        "pattern": "^([A-Z]{2}|[a-z0-9-]{1,20})$",
        "normalize": ["trim"],
        "source": {"kind": "list", "keys": ["EU|s-1|t0001", " EU | s-2 | t0002 "]},
    }
    spec["sides"]["a"]["request"] |= {
        "path": "/v1/{{ keyParts.region }}/shelves/{{ keyParts.shelf }}/things",
        "query": {"code": "{{ keyParts.code }}"},
    }
    body = {"item": "{{ keyParts.code }}", "whole": "{{ key }}"}
    spec["sides"]["b"]["request"] = {"method": "POST", "path": "/v2/q", "body": body}
    return spec


def _two_tables(spec: dict[str, Any], **key: Any) -> dict[str, Any]:
    """``minimal_spec`` between two tables keyed by (region, code), its key's members replaced by ``key``."""
    spec["key"] = {"name": "sku", "type": "composite", "parts": ["region", "code"]} | key
    table = {"name": "stock", "keyColumns": ["region", "code"]}
    spec["sides"] = {
        side: {"role": role, "target": f"db-{side}", "table": dict(table)}
        for side, role in (("a", "legacy"), ("b", "new"))
    }
    return spec


def test_composite_service_spec_loads(minimal_spec: dict[str, Any], things_targets: TargetRegistry) -> None:
    """A composite key beside a service side is a multi-part key: each part is a placeholder, and normalised
    and matched on its own; a repeated key is one after ``key.normalize`` of its parts."""
    doc = _multi_part(minimal_spec)
    assert validate_spec(doc, targets=things_targets) == []
    spec = compile_spec(doc, targets=things_targets)
    assert spec.key_parts == PARTS and {side.request.key_parts for side in spec.sides.values()} == {PARTS}
    assert normalise_keys(doc["key"]["source"]["keys"], spec) == ["EU|s-1|t0001", "EU|s-2|t0002"]
    doc["key"]["source"]["keys"].append("EU|s-1| t0001")
    assert _warnings(doc) == {("/key/source/keys/2", "duplicate_key")} and _errors(doc) == set()
    # Beside a table side too: its key columns are the parts, and key.normalize stays refused there.
    doc["sides"]["a"] = {"role": "legacy", "target": "db", "table": {"name": "t", "keyColumns": list(PARTS)}}
    assert _errors(doc) == {("/key/normalize", "unsupported")}
    doc["key"] = doc["key"] | {"normalize": [], "source": {"kind": "list", "keys": ["EU|s-1|t0001"]}}
    assert validate_spec(doc) == []
    mixed = compile_spec(doc)
    assert mixed.key_parts == PARTS and mixed.sides[Side.B].request.key_parts == PARTS


def test_stored_two_table_composite_specs_still_load_and_compare(minimal_spec: dict[str, Any]) -> None:
    """Between two tables a composite key's text is one part, as before multi-part keys: its pattern fits
    the joined text, a part may be empty (``|1``), an inline list is judged whole and a part named twice
    still loads."""
    keys = ["eu|1", "|1", "us\\|x|2"]
    doc = _two_tables(minimal_spec, pattern=r"^[a-z|\\]*\|[0-9]+$", source={"kind": "list", "keys": keys})
    doc["key"]["parts"] = ["region", "region"]
    assert validate_spec(doc) == []
    spec = compile_spec(doc)
    assert spec.key_parts == () and spec.key.parts == ("region", "region")
    assert normalise_keys(keys, spec) == keys
    with pytest.raises(ValueError, match=r"^1 key\(s\) do not match key.pattern after key.normalize, or"):
        normalise_keys(["eu|1", "eu"], spec)
    doc["key"]["source"]["keys"] = ["eu|1", "eu"]
    assert [(i.pointer, i.message) for i in validate_spec(doc)] == [
        (
            "/key/source/keys/1",
            "after key.normalize the key is empty, '.', '..', spans lines or fails key.pattern",
        )
    ]


def test_string_spec_with_parts_stays_single(minimal_spec: dict[str, Any]) -> None:
    """A key that is not composite has one part, whatever ``parts`` it carries: its text is judged whole
    and the parts it names are no placeholders."""
    minimal_spec["key"]["parts"] = ["region", "region"]
    assert validate_spec(minimal_spec) == []
    spec = compile_spec(minimal_spec)
    assert spec.key_parts == () and spec.sides[Side.A].request.key_parts == ()
    assert normalise_keys([" t0002 "], spec) == ["T0002"]
    minimal_spec["sides"]["a"]["request"]["query"] = {"r": "{{ keyParts.region }}"}
    assert _errors(minimal_spec) == {("/sides/a/request/query/r", "unsupported")}


def test_composite_hash_sample_refused(minimal_spec: dict[str, Any], things_targets: TargetRegistry) -> None:
    """A sample's frame gives one text a key, never its parts."""
    doc = _multi_part(minimal_spec)
    doc["key"]["source"] = {"kind": "hash_sample", "salt": "q4-stock", "rate": 0.5, "source": _LISTING}
    refused = [(i.pointer, i.code, i.message) for i in validate_spec(doc, targets=things_targets)]
    assert refused == [
        (
            "/key/source/kind",
            "unsupported",
            "not supported: hash_sample with a composite key beside a service side",
        )
    ]
    tabled = _two_tables(doc, source=doc["key"]["source"])  # one part: the two tables' own refusal
    assert [i.message for i in validate_spec(tabled) if i.pointer == "/key/source/kind"] == [
        "not supported: hash_sample between two table sides: their scan compares every row"
    ]


def test_servicekeys_messages(minimal_spec: dict[str, Any]) -> None:
    """The texts of a multi-part key's refusals, none of which holds a key or a value."""
    doc = _multi_part(minimal_spec)
    spec = compile_spec(doc)
    doc["key"]["source"]["keys"] = ["EU|s-1", "EU|..|t0001", "EU|s-1|T0001", "EU|s-1|t0001"]
    m1 = "after key.normalize the key is not 3 parts joined by |, or a part is empty, '.', '..', spans lines"
    assert [(i.pointer, i.code, i.message) for i in validate_spec(doc)] == [
        (f"/key/source/keys/{n}", "invalid_key", m1 + " or fails key.pattern") for n in range(3)
    ]
    m2 = (
        "3 key(s) are not 3 parts joined by |, or hold a part that fails key.pattern after key.normalize,"
        " or are over 1024 bytes or hold NUL (positions 1, 2, 3)"
    )
    with pytest.raises(ValueError) as refused:
        normalise_keys(doc["key"]["source"]["keys"], spec)
    assert str(refused.value) == m2 and "EU" not in m2
    request = {"method": "GET", "path": "/v1/things", "query": {"r": "{{ keyParts.region }}"}}
    doc["key"]["source"] = {**_LISTING, "request": request, "keyPath": "$['region','shelf','code']"}
    assert [(i.pointer, i.code, i.message) for i in validate_spec(doc)] == [
        (
            "/key/source/request/query/r",
            "unsupported",
            "not supported: a keyParts placeholder in a key listing's request",
        )
    ]
    doc["key"]["source"] = {"kind": "run_keys", "runId": 7}
    doc["key"]["parts"] = [*PARTS, "code"]
    assert [(i.pointer, i.code, i.message) for i in validate_spec(doc)] == [
        ("/key/parts", "invalid_key", "key.parts names a part twice"),
        (
            "/key/source/kind",
            "unsupported",
            "not supported: key source 'run_keys'; use list, paginated_endpoint or a key table",
        ),
    ]


def test_a_lone_backslash_in_a_multi_part_key_is_refused(minimal_spec: dict[str, Any]) -> None:
    """``EU\\west|c1|r2`` typed with one backslash: the loader and a run refuse it by position, as any
    key that is not its parts, instead of comparing ``EUwest|c1|r2`` with nothing said, and beside that
    very key it is no duplicate of it. Doubled, the pattern judges the backslash."""
    doc = _multi_part(minimal_spec)
    doc["key"] |= {"parts": ["region", "code", "rev"], "pattern": "^[A-Za-z0-9_-]{1,200}$"}
    for side in "ab":
        doc["sides"][side]["request"] = {"method": "GET", "path": "/v1/{{ keyParts.region }}"}
    doc["key"]["source"]["keys"] = given = ["EU\\west|c1|r2", "EUwest|c1|r2", "a\\b\\c|c1|r2"]
    assert [(i.pointer, i.code) for i in validate_spec(doc)] == [
        (f"/key/source/keys/{n}", "invalid_key") for n in (0, 2)
    ]
    spec = compile_spec(doc | {"key": doc["key"] | {"source": {"kind": "list", "keys": given[1:2]}}})
    with pytest.raises(ValueError) as refused:
        normalise_keys(given, spec)
    assert str(refused.value) == (
        "2 key(s) are not 3 parts joined by |, or hold a part that fails key.pattern after key.normalize,"
        " or are over 1024 bytes or hold NUL (positions 1, 3)"
    )
    assert normalise_keys(given[1:2], spec) == ["EUwest|c1|r2"]
    with pytest.raises(ValueError, match="positions 1\\)"):  # written doubled: no backslash fits the pattern
        normalise_keys(["EU\\\\west|c1|r2"], spec)


# -- truncation flags -----------------------------------------------------------------------------


def test_flag_paths_schema(minimal_spec: dict[str, Any]) -> None:
    """A side's ``truncation`` is its count members (a cap with a path), its flag paths, or both; a flag
    path is a JSONPath of the raw response, checked and normalised as the count members' are."""

    def issues(truncation: Any) -> list[tuple[str, str, str]]:
        minimal_spec["sides"]["a"]["truncation"] = truncation
        found = validate_spec(minimal_spec)
        return [(i.pointer.removeprefix("/sides/a/truncation"), i.code, i.message) for i in found]

    assert issues({"flagPaths": ["$..more"]}) == []
    assert issues({"itemsPath": "$.thing.parts", "cap": 100, "flagPaths": ["$.more", "$['page'].cut"]}) == []
    side = compile_spec(minimal_spec).sides[Side.A]
    assert side.truncation.cap == 100 and side.truncation_flags == ("$.more", "$.page.cut")
    one_of = "requires one of: countPath, itemsPath"
    needs_cap = [("", "schema.required", "'cap' is a required property")]
    assert issues({}) == [("", "schema.anyOf", one_of + ", flagPaths")]
    assert issues({"cap": 10, "flagPaths": ["$.more"]}) == [("", "schema.anyOf", one_of)]
    assert issues({"itemsPath": "$.x", "flagPaths": ["$.more"]}) == needs_cap
    assert issues({"countPath": "$.n", "itemsPath": "$.x"}) == needs_cap
    assert issues({"flagPaths": []}) == [("/flagPaths", "schema.minItems", "must have at least 1 item(s)")]
    assert issues({"flagPaths": ["$.more", "$.more"]}) == [
        ("/flagPaths", "schema.uniqueItems", "items must be unique")
    ]
    assert [i[:2] for i in issues({"flagPaths": "$.more"})] == [("/flagPaths", "schema.type")]
    assert [i[:2] for i in issues({"flagPaths": ["more", "$.more["]})] == [
        ("/flagPaths/0", "schema.pattern"),
        ("/flagPaths/1", "invalid_jsonpath"),
    ]


def test_flags_only_side_meets_x33(minimal_spec: dict[str, Any], things_targets: TargetRegistry) -> None:
    """Flags alone are a declared truncation, whether or not the side's target declares a count
    rule; the side then has no count members of its own, so the target's stay in force."""
    minimal_spec["sides"]["a"]["truncation"] = {"flagPaths": ["$..more"]}  # its target declares none
    minimal_spec["sides"]["b"]["truncation"] = {"flagPaths": ["$.meta.more"]}
    assert validate_spec(minimal_spec, targets=things_targets) == []
    sides = compile_spec(minimal_spec, targets=things_targets).sides
    assert [(s.truncation, s.truncation_flags) for s in sides.values()] == [
        (None, ("$..more",)),
        (None, ("$.meta.more",)),
    ]
    del minimal_spec["sides"]["a"]["truncation"]
    assert _errors(minimal_spec, targets=things_targets) == {("/sides/a", "truncation_undeclared")}


# -- key tables -----------------------------------------------------------------------------------

NOT = "not supported: "


def _key_table(**table: Any) -> dict[str, Any]:
    """A key table on target keys-db: ``keys`` by its column ``id``, with ``table``'s members."""
    return {"kind": "table", "target": "keys-db", "table": {"name": "keys", "keyColumns": ["id"]} | table}


def test_key_table_gates(minimal_spec: dict[str, Any], things_targets: TargetRegistry) -> None:
    """A ``table`` key source with a target and a table of its own is a key table: ``key.source`` itself,
    beside a service side, its table judged as a table side's and its key columns the key's parts. It takes
    no member of a frame's table source, gives keys only, and reads a database or a files target."""

    def issues(source: Any, **kwargs: Any) -> list[tuple[str, str, str]]:
        minimal_spec["key"]["source"] = source
        found = validate_spec(minimal_spec, **kwargs)
        return [(i.pointer.removeprefix("/key/source"), i.code, i.message) for i in found]

    regex = {"kind": "regex", "sideType": "string", "toSide": {"pattern": "T([0-9]+)", "replacement": "$1"}}
    regex |= {"toCanonical": {"pattern": "([0-9]+)", "replacement": "T$1"}}
    where = {"where": "code ILIKE 't%'", "keyColumns": [{"name": "id", "keyMap": regex}]}
    assert issues(_key_table(schema="ref", **where)) == []
    spec = compile_spec(minimal_spec)
    assert spec.sample is None and key_table(spec.raw["key"]["source"]) == SideConfig(
        Side.A, "keys", "keys-db", not_found=NotFoundRule(),
        table=TableConfig("keys", ("id",), (), "ref", "code ILIKE 't%'", (regex,)),
    )  # fmt: skip
    # The schema: one form or the other, never members of both.
    mixed = "A key table takes target and table, and neither side nor strataColumns."
    assert issues({"kind": "table", "target": "keys-db"}) == [
        ("", "schema.required", "'table' is a required property")
    ]
    assert issues({"kind": "table", "table": _key_table()["table"]}) == [
        ("", "schema.required", "'target' is a required property")
    ]
    assert issues(_key_table() | {"strataColumns": ["tier"]}) == [
        ("/strataColumns", "schema.not", f"member 'strataColumns' is not allowed here: {mixed}")
    ]
    assert ("/side", "schema.not", f"member 'side' is not allowed here: {mixed}") in issues(
        _key_table() | {"side": "a"}
    )
    # It is key.source itself, and gives keys only.
    nested = [
        ("/source/kind", "unsupported", NOT + "a key table inside hash_sample; a frame's table is a side's")
    ]
    assert issues({"kind": "hash_sample", "salt": "q4-stock", "rate": 0.5, "source": _key_table()}) == nested
    assert issues(_key_table(columns=["tier"])) == [
        ("/table/columns", "unsupported", NOT + "columns in a key table; it gives keys only")
    ]
    # Its table is judged as a side's, under /key/source/table.
    lookup = {"kind": "lookup", "target": "keys-db", "template": {"id": "x", "version": 1}}
    columns = ["id", {"name": "branch", "part": "branch", "keyMap": lookup}]
    assert issues(_key_table(keyColumns=columns, where="id ==== 1")) == [
        ("/table/keyColumns", "key_columns_mismatch", "2 key column(s) for a key of 1 part(s)"),
        ("/table/keyColumns/1/keyMap", "unsupported", NOT + "a lookup key map, which needs SQL templates"),
        (
            "/table/keyColumns/1/part",
            "unsupported",
            NOT + "a key column's part; list key columns in key.parts order",
        ),
        (
            "/table/where",
            "invalid_where",
            "expected a literal (string, number, TRUE or FALSE), found '=' at position 4",
        ),
    ]
    filed = _key_table(schema="ref", file={"path": "keys/*.csv", "format": "csv"}, **where)
    assert {(at, code) for at, code, _ in issues(filed)} == {
        ("/table/file/format", "invalid_file"),  # format csv needs csv
        ("/table/schema", "unsupported"),
        ("/table/keyColumns/0/keyMap", "unsupported"),
        ("/table/where", "unsupported"),
    }
    # Its target is in the registry, and a database or a files target that goes with its table.
    ledger = DatabaseTarget("mysql", "ledger.example", 3306, "ledger", "reader", "file:/run/secrets/ledger")
    registry = TargetRegistry(
        {target_id: things_targets.get(target_id) for target_id in things_targets}
        | {
            "keys-db": Target("keys-db", "", dsn_ref="file:/run/secrets/keys"),
            "exports": Target("exports", "", root="/var/lib/deltasleuth/files/x"),
            "ledger": Target("ledger", "", database=ledger),
        }
    )
    assert issues(_key_table(**where), targets=registry) == []
    assert issues(_key_table(), targets=things_targets) == [
        ("/target", "unknown_target", "target 'keys-db' is not in the registry")
    ]
    kinds = [
        ({"target": "things-v1"}, "a key table needs a database target (dsn_ref or engine)"),
        ({"target": "exports"}, "a files target needs table.file (path, format)"),
        ({"table": filed["table"]}, "table.file is for a files target (engine: files)"),
    ]
    for members, message in kinds:
        found = issues(_key_table() | members, targets=registry)
        assert ("/target", "target_kind", message) in found, message
    assert issues(_key_table(**where) | {"target": "ledger"}, targets=registry) == [
        (
            "/table/keyColumns/0/keyMap",
            "unsupported",
            NOT + "a regex key map on a MySQL or Oracle side; compare a view that derives the key",
        ),
        ("/table/where", "unsupported", NOT + "ILIKE on a MySQL or Oracle side; use LIKE"),
    ]
    # Its key columns number a multi-part key's parts; two tables are scanned, never listed.
    minimal_spec = _multi_part(minimal_spec)
    assert issues(_key_table()) == [
        ("/table/keyColumns", "key_columns_mismatch", "1 key column(s) for a key of 3 part(s)")
    ]
    assert issues(_key_table(keyColumns=list(PARTS))) == []
    minimal_spec = _two_tables(minimal_spec)
    assert issues(_key_table(keyColumns=["region", "code"])) == [
        ("/kind", "unsupported", NOT + "a key table between two table sides; their scan compares every row")
    ]
