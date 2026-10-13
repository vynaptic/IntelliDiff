"""Enrichment: taxonomy, severity, value class, signature and known-difference rules."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from deltasleuth.core.classify.enrich import classify, enrich
from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.classify.taxonomy import TaxonomyError, installed_packs
from deltasleuth.core.spec.expressions import CEL_VARIABLES, cel_error
from deltasleuth.core.types import (
    ArrayRule,
    ChangeType,
    CompareConfig,
    FieldRule,
    KnownDiffRule,
    RuleClassification,
    Severity,
)

MISSING, EXTRA = ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B
CHANGED, TYPED, ORDER = ChangeType.VALUE_CHANGED, ChangeType.TYPE_CHANGED, ChangeType.ORDER_CHANGED
PHONE_PARTS = (("field", "phones"), ("member", "+447700900123"), ("field", "kind"))


def rule(
    rule_id: str, when: str, cls: RuleClassification = RuleClassification.ACCEPTED, **kw
) -> KnownDiffRule:
    return KnownDiffRule(rule_id, when, cls, "a reason long enough", "TICKET-1", **kw)


def enrich_one(f, spec, today=date(2026, 9, 24), **kw):
    [got] = enrich([f], spec, today=today, **kw)
    return got


# --- taxonomy and severity -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "change", "a", "b", "code", "severity"),
    [
        ("$.segment", CHANGED, "RETAIL", "SME", "VALUE_MISMATCH", Severity.MEDIUM),
        ("$.nickname", MISSING, "Ada", None, "FIELD_MISSING", Severity.MEDIUM),
        ("$.nickname", EXTRA, None, "Ada", "FIELD_EXTRA", Severity.MEDIUM),
        ("$.createdAt", TYPED, "2019", D(2019), "TYPE_MISMATCH", Severity.MEDIUM),
        ("$.addresses[*].lines", ORDER, ["a", "b"], ["b", "a"], "ORDER_MISMATCH", Severity.MEDIUM),
        # field rules: for value changes at the exact path, and for nothing else
        ("$.accounts[*].balance", CHANGED, D("1.1"), D("1.2"), "VALUE_MISMATCH", Severity.HIGH),
        ("$.accounts[*].balance", MISSING, D("1.1"), None, "FIELD_MISSING", Severity.MEDIUM),
        ("$.accounts[*].balance", TYPED, D("1.1"), "1.1", "TYPE_MISMATCH", Severity.MEDIUM),
        ("$.consents[*].granted", CHANGED, True, False, "VALUE_MISMATCH", Severity.CRITICAL),
        # array rules: one-sided members only, not fields inside them
        ("$.accounts[*]", MISSING, {"accountNo": "1"}, None, "FIELD_MISSING", Severity.HIGH),
        ("$.consents[*]", EXTRA, None, {"purpose": "X"}, "FIELD_EXTRA", Severity.HIGH),
        ("$.tags[*]", MISSING, "vip", None, "FIELD_MISSING", Severity.MEDIUM),
        ("$.accounts[*].status", MISSING, "OPEN", None, "FIELD_MISSING", Severity.MEDIUM),
    ],
)
def test_core_codes_and_severities(spec, make_finding, path, change, a, b, code, severity) -> None:
    got = classify(make_finding(path, change, a, b), spec, None)
    assert (got.taxonomy_code, got.severity) == (code, severity)


def test_pack_codes_from_field_and_array_rules(make_spec, make_finding) -> None:
    compare = CompareConfig(
        arrays=(ArrayRule("$.links", "keyed", ("id",), taxonomy_missing_in_b="MISSING_LINK"),),
        fields=(
            FieldRule("$.flagged", taxonomy_code="FLAG_MISMATCH"),
            FieldRule("$.status", taxonomy_code="ATTRIBUTE_MISMATCH", severity=Severity.LOW),
        ),
    )
    spec = make_spec(compare=compare, domain_pack="entity-linking", known_diffs=())
    cases = [
        (make_finding("$.flagged", CHANGED, True, False), "FLAG_MISMATCH", Severity.CRITICAL),
        (make_finding("$.status", CHANGED, "A", "B"), "ATTRIBUTE_MISMATCH", Severity.LOW),
        # the field rule is for value changes only: a retyped or vanished field is core
        (make_finding("$.status", TYPED, "1", D(1)), "TYPE_MISMATCH", Severity.MEDIUM),
        (make_finding("$.status", MISSING, "A", None), "FIELD_MISSING", Severity.MEDIUM),
        (make_finding("$.links[*]", MISSING, {"id": "E1"}, None), "MISSING_LINK", Severity.HIGH),
        (make_finding("$.links[*]", EXTRA, None, {"id": "E1"}), "FIELD_EXTRA", Severity.MEDIUM),  # no code
    ]
    for f, code, severity in cases:
        got = enrich_one(f, spec)
        assert (got.taxonomy_code, got.severity) == (code, severity), f.path_pattern


def test_array_path_spelling_does_not_matter(make_spec, make_finding) -> None:
    arrays = (ArrayRule("$['phones']", "keyed", ("n",), severity=Severity.LOW),)
    spec = make_spec(compare=CompareConfig(arrays=arrays))
    assert (
        classify(make_finding("$.phones[*]", MISSING, {"n": "1"}, None), spec, None).severity is Severity.LOW
    )


def test_info_field_rule_never_hides_a_missing_field_or_member(make_spec, make_finding) -> None:
    """A field rule down-rated to INFO covers value changes only (spec.schema.json)."""
    spec = make_spec(
        compare=CompareConfig(
            arrays=(ArrayRule("$.tags", "set"), ArrayRule("$.accounts", "keyed", ("id",))),
            fields=(
                FieldRule("$.lastLogin", severity=Severity.INFO),
                FieldRule("$.tags[*]", severity=Severity.INFO, taxonomy_code="VALUE_MISMATCH"),
                FieldRule("$.accounts[*]", severity=Severity.INFO),
            ),
        )
    )
    cases = [
        (make_finding("$.lastLogin", MISSING, "2026-09-01", None), "FIELD_MISSING"),
        (make_finding("$.lastLogin", EXTRA, None, "2026-09-01"), "FIELD_EXTRA"),
        (make_finding("$.lastLogin", TYPED, "2026-09-01", None), "TYPE_MISMATCH"),
        (make_finding("$.tags[*]", MISSING, "blocked", None), "FIELD_MISSING"),
        (make_finding("$.accounts[*]", EXTRA, None, {"id": "A2"}), "FIELD_EXTRA"),
    ]
    for f, code in cases:
        got = classify(f, spec, None)
        assert (got.taxonomy_code, got.severity) == (code, Severity.MEDIUM), f.change_type
    changed = classify(make_finding("$.lastLogin", CHANGED, "2026-09-01", "2026-09-02"), spec, None)
    assert changed.severity is Severity.INFO  # the override still applies where it is meant to


def test_one_sided_members_take_the_array_rule_not_the_field_rule(make_spec, make_finding) -> None:
    compare = CompareConfig(
        arrays=(ArrayRule("$.tags", "set", severity=Severity.HIGH),),
        fields=(FieldRule("$.tags[*]", severity=Severity.INFO),),
    )
    got = classify(make_finding("$.tags[*]", MISSING, "vip", None), make_spec(compare=compare), None)
    assert (got.taxonomy_code, got.severity) == ("FIELD_MISSING", Severity.HIGH)


def test_presets_are_kept(spec, make_finding) -> None:
    norm = make_finding(
        "$.accounts[*].balance",
        CHANGED,
        "1.10",
        "1.1",
        taxonomy_code="NORMALIZATION_ONLY",
        severity=Severity.INFO,
    )
    got = classify(norm, spec, None)
    assert (got.taxonomy_code, got.severity) == ("NORMALIZATION_ONLY", Severity.INFO)
    filt = classify(
        make_finding("$.accounts[*]", MISSING, {"a": 1}, None, taxonomy_code="FILTER_MEMBERSHIP"), spec, None
    )
    assert (filt.taxonomy_code, filt.severity) == ("FILTER_MEMBERSHIP", Severity.MEDIUM)


def test_unknown_codes_and_presence_violations_raise(make_spec, make_finding) -> None:
    pack = installed_packs()["entity-linking"]
    bad_array = make_spec(
        compare=CompareConfig(arrays=(ArrayRule("$.links", "set", taxonomy_missing_in_b="EXTRA_LINK"),))
    )
    with pytest.raises(TaxonomyError, match="presence extra"):
        classify(make_finding("$.links[*]", MISSING, "x", None), bad_array, pack)
    bad_field = make_spec(compare=CompareConfig(fields=(FieldRule("$.x", taxonomy_code="MISSING_LINK"),)))
    with pytest.raises(TaxonomyError, match="presence missing"):
        classify(make_finding("$.x", CHANGED, "a", "b"), bad_field, pack)
    pack_code = make_spec(
        compare=CompareConfig(fields=(FieldRule("$.x", taxonomy_code="ATTRIBUTE_MISMATCH"),))
    )
    with pytest.raises(TaxonomyError, match="unknown taxonomy code"):
        classify(make_finding("$.x", CHANGED, "a", "b"), pack_code, None)  # no pack is active


def test_value_class_and_signature(spec, make_finding) -> None:
    got = classify(make_finding("$.segment", CHANGED, "retail", "RETAIL"), spec, None)
    assert (got.value_class, got.signature_hash) == ("case_only", signature_hash(got, spec))
    dup = classify(
        make_finding("$.phones[*]", MISSING, {"n": 1}, None, value_class="duplicate_key"), spec, None
    )
    assert dup.value_class == "duplicate_key"


def test_enrich_returns_copies(spec, make_finding) -> None:
    original = make_finding("$.segment", CHANGED, "PRIVATE", "WEALTH")
    got = enrich_one(original, spec)
    assert got is not original and got.rule_id == "KD-CP-0001"
    assert original.taxonomy_code is None and original.rule_id is None


def test_the_pack_aliases_document_codes_stably(make_spec, make_finding) -> None:
    doc = make_finding("$", MISSING, {"id": "E1"}, None, parts=(), taxonomy_code="MISSING_DOCUMENT")
    assert enrich_one(doc, make_spec(known_diffs=())).taxonomy_code == "MISSING_DOCUMENT"
    aliased_spec = make_spec(known_diffs=(), domain_pack="entity-linking")
    aliased = enrich_one(doc, aliased_spec)
    assert (aliased.taxonomy_code, aliased.severity) == ("MISSING_ENTITY", Severity.CRITICAL)
    again = enrich_one(aliased, aliased_spec)  # the signature follows the displayed name
    assert (again.taxonomy_code, again.signature_hash) == ("MISSING_ENTITY", aliased.signature_hash)


def test_re_enrich_drops_a_rule_that_no_longer_applies(spec, make_finding) -> None:
    stale = make_finding("$.segment", CHANGED, "RETAIL", "SME", rule_id="KD-OLD-1")
    got = enrich_one(replace(stale, rule_classification=RuleClassification.ACCEPTED), spec)
    assert got.rule_id is None and got.rule_classification is None


# --- known-difference rules ----------------------------------------------------------------------


def test_cel_variables_match_the_schema(repo_root: Path) -> None:
    schema = json.loads((repo_root / "docs/architecture/spec.schema.json").read_text(encoding="utf-8"))
    text = schema["$defs"]["knownDiff"]["properties"]["when"]["description"]
    listed = re.sub(r"\([^()]*\)", "", text.split("Variables:", 1)[1]).rstrip(". ")
    names = [n.strip() for part in listed.split(",") for n in part.split(" and ")]
    assert list(CEL_VARIABLES["known_diff"]) == names


@pytest.fixture
def matched(make_spec, make_finding):
    """The id of the rule that matches ``f`` (a $.segment change by default) among ``rules``."""

    def matched(*rules: KnownDiffRule, f=None, **kw) -> str | None:
        f = f or make_finding("$.segment", CHANGED, "A", "B")
        return enrich_one(f, make_spec(known_diffs=rules), **kw).rule_id

    return matched


@pytest.mark.parametrize("name", list(CEL_VARIABLES["known_diff"]))
def test_every_declared_variable_is_bound(matched, name: str) -> None:
    """The evaluator binds every name the loader compiles rules against."""
    errors: list = []
    assert (matched(rule("KD-V-0001", f"{name} == {name}"), rule_errors=errors), errors) == ("KD-V-0001", [])


def test_every_variable_reaches_the_rule(matched, make_finding, tok) -> None:
    when = (
        "key == 'C00000001' && path == '$.phones[*].kind' && pointer.startsWith('/phones/tok:phone:v1:')"
        " && change_type == 'value_changed' && direction == 'both' && a == 'MOBILE' && b == 'HOME'"
        " && value_class == 'other' && taxonomy_code == 'VALUE_MISMATCH' && severity == 'MEDIUM'"
        " && stratum == null && size(strata) == 0 && link_reason == null && hop_count == null"
        " && size(attrs) == 0"
    )
    f = make_finding("$.phones[*].kind", CHANGED, "MOBILE", "HOME", parts=PHONE_PARTS)
    assert matched(rule("KD-ALL-0001", when), f=f, tokeniser=tok) == "KD-ALL-0001"


def test_rules_are_evaluated_in_the_loaders_typed_environment(matched) -> None:
    assert matched(rule("KD-T-0001", "key.startsWith('C') && path.endsWith('segment')")) == "KD-T-0001"
    typo = "key + 1 == 2"  # key is a string: a type error for the loader and the evaluator alike
    assert cel_error(typo, "known_diff") is not None
    with pytest.raises(RuntimeError):
        matched(rule("KD-T-0002", typo))


def test_rule_sees_normalised_values(matched, make_finding) -> None:
    """CEL ``a``/``b`` are the normalised values ($defs/knownDiff), never the raw ones."""
    f = make_finding("$.status", CHANGED, "OLD", "NEW", a_raw=" old", b_raw="new ")
    assert (
        matched(rule("KD-NORM-0001", "path == '$.status' && a == 'OLD' && b == 'NEW'"), f=f) == "KD-NORM-0001"
    )
    assert matched(rule("KD-RAW-0001", "path == '$.status' && a == ' old' && b == 'new '"), f=f) is None


def test_the_example_rules(spec, make_finding) -> None:
    """KD-CP-0001 matches the renamed segment only; KD-CP-0002 is a bug rule until it expires."""
    renamed, other = (make_finding("$.segment", CHANGED, a, "WEALTH") for a in ("PRIVATE", "RETAIL"))
    assert (enrich_one(renamed, spec).rule_id, enrich_one(other, spec).rule_id) == ("KD-CP-0001", None)
    assert enrich_one(renamed, spec).rule_classification is RuleClassification.EXPECTED_BY_DESIGN
    sms = make_finding("$.preferences.marketing.sms", MISSING, False, None)
    on_day = enrich_one(sms, spec, today=date(2027, 1, 31))
    assert (on_day.rule_id, on_day.rule_classification) == ("KD-CP-0002", RuleClassification.BUG)
    assert enrich_one(sms, spec, today=date(2027, 2, 1)).rule_id is None
    assert enrich_one(make_finding("$.preferences.marketing.sms", CHANGED, True, False), spec).rule_id is None


def test_bug_rules_match_first_then_by_rule_id(matched) -> None:
    bug = RuleClassification.BUG
    assert matched(
        rule("KD-A-0001", "true"), rule("KD-Z-0009", "true", bug), rule("KD-M-0005", "true", bug)
    ) == ("KD-M-0005")


def test_expired_rules_never_match(matched) -> None:
    assert matched(
        rule("KD-OLD-1", "true", expires="2026-09-23"), rule("KD-ON-1", "true", expires="2026-09-24")
    ) == ("KD-ON-1")


def test_evaluation_errors_are_reported_and_later_rules_still_apply(matched, make_finding) -> None:
    rules = (
        rule("KD-A-0001", "a.status == 'CLOSED'"),  # a is a string here: SELECT on a non-map
        rule("KD-B-0001", "int(a) == 1"),  # a conversion error
        rule("KD-C-0001", "a"),  # not a boolean
        rule("KD-D-0001", "a == 'secret-value'"),
    )
    errors: list = []
    f = make_finding("$.segment", CHANGED, "secret-value", "x")
    assert matched(*rules, f=f, rule_errors=errors) == "KD-D-0001"
    base = {"path_pattern": "$.segment", "change_type": "value_changed"}
    assert errors == [
        {"rule_id": "KD-A-0001", **base, "code": "INVALID_ARGUMENT"},
        {"rule_id": "KD-B-0001", **base, "code": "INVALID_ARGUMENT"},
        {"rule_id": "KD-C-0001", **base, "code": "non_boolean_result"},
    ]
    assert "secret-value" not in repr(errors)


def test_error_codes_never_quote_payload_map_keys(matched, make_finding) -> None:
    errors: list = []
    matched(
        rule("KD-A-0001", "a[b.k] == 1"),
        f=make_finding("$.x", CHANGED, {"k": "v"}, {"k": "w"}),
        rule_errors=errors,
    )
    assert [e["code"] for e in errors] == ["NOT_FOUND"]


def test_the_pointer_variable_never_carries_a_raw_member_key(matched, make_finding, tok) -> None:
    f = make_finding("$.phones[*].kind", CHANGED, "MOBILE", "HOME", parts=PHONE_PARTS)
    assert matched(rule("KD-P-0001", "pointer == '/phones/tok:phone:unkeyed/kind'"), f=f) == "KD-P-0001"
    keyed = rule("KD-P-0002", f"pointer == '/phones/{tok.token('phone', '+447700900123')}/kind'")
    assert matched(keyed, f=f, tokeniser=tok) == "KD-P-0002"


def test_numbers_reach_cel_as_numbers(matched, make_finding) -> None:
    f = make_finding("$.x", CHANGED, D("1250.10"), D("1250.00"))
    assert matched(rule("KD-N-0001", "a == 1250.1 && b == 1250"), f=f) == "KD-N-0001"


def test_a_rule_that_depends_on_digits_a_double_drops_never_matches(matched, make_finding) -> None:
    """``b == 0.1`` is false for 0.10000000000000001, whose nearest double is 0.1."""
    errors: list = []
    f = make_finding("$.rate", CHANGED, D("0.2"), D("0.10000000000000001"))
    rules = rule("KD-R-0001", "b == 0.1"), rule("KD-R-0002", "b < 0.2")
    assert matched(*rules, f=f, rule_errors=errors) == "KD-R-0002"
    assert [e["code"] for e in errors] == ["inexact_number"]
