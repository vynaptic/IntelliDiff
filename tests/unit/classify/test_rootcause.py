"""Root causes: member-key fan-out, signature groups, bug rules, determinism and masking."""

from __future__ import annotations

import itertools
import re
from dataclasses import replace

import pytest

from deltasleuth.core.classify.enrich import classify
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.classify.rootcause import groups, root_causes
from deltasleuth.core.types import (
    ChangeType,
    Direction,
    KeyResult,
    Outcome,
    RuleClassification,
    Severity,
    SideState,
)

MISSING, EXTRA, CHANGED = ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B, ChangeType.VALUE_CHANGED
PHONE = "+447700900123"


@pytest.fixture
def causes(spec, tok):
    """(root causes, bug-rule causes, finding -> cause) of results, masked as reports publish them."""

    def causes(results):
        masked = {"spec": spec, "tokeniser": tok}
        mapped = {
            (f.key, f.json_pointer, f.change_type.value): rc.root_cause_id
            for rc, fs in groups(results, **masked)
            for f in fs
        }
        bugs = {  # as the root-cause CSV derives bug_rule_id
            rc: f.rule_id
            for r in results
            for f in r.findings
            if (f.suppressed, f.rule_classification) == (True, RuleClassification.BUG)
            and (rc := mapped.get((f.key, f.json_pointer, f.change_type.value)))
        }
        return root_causes(results, **masked), bugs, mapped

    return causes


@pytest.fixture
def phone_missing(make_finding, spec):
    def build(key: str, number: str = PHONE):
        parts = (("field", "phones"), ("member", number))
        f = make_finding("$.phones[*]", MISSING, {"number": number}, None, key=key, parts=parts)
        return classify(replace(f, member_key=number, array_path="$.phones"), spec, None)

    return build


@pytest.fixture
def changed(make_finding, spec):
    def build(key: str, path: str = "$.segment", **fields):
        return classify(make_finding(path, CHANGED, "A", "B", key=key, **fields), spec, None)

    return build


def result(key: str, *findings) -> KeyResult:
    return KeyResult(key, Outcome.MISMATCH, SideState.absent(), SideState.absent(), findings=list(findings))


def bug(f, rule_id: str = "KD-BUG-0001"):
    return replace(f, rule_id=rule_id, rule_classification=RuleClassification.BUG)


def test_the_same_member_missing_under_several_keys_is_one_cause(causes, phone_missing, tok) -> None:
    results = [result(k, phone_missing(k)) for k in ("C00000003", "C00000001", "C00000002")]
    [rc], _, _ = causes(results)
    assert (rc.kind, rc.path_pattern, rc.change_type, rc.direction) == (
        "member_key",
        "$.phones[*]",
        MISSING,
        Direction.A_ONLY,
    )
    assert (rc.affected_keys, rc.finding_count, rc.max_severity) == (3, 3, Severity.MEDIUM)
    assert (rc.taxonomy_code, rc.value_class) == ("FIELD_MISSING", "other")
    assert re.fullmatch(r"RC-[0-9a-f]{12}", rc.root_cause_id)
    # masked: the member key and sample keys are tokens, never raw values
    assert rc.member_key_tok == tok.token("phone", PHONE)
    assert rc.sample_keys == tuple(sorted(tok.token("identifier", f"C0000000{i}") for i in (1, 2, 3)))
    assert PHONE not in repr(rc) and "C0000000" not in repr(rc)
    assert causes(results)[0][0].root_cause_id == rc.root_cause_id


def test_singletons_fall_to_signatures(causes, phone_missing, spec) -> None:
    results = [result(k, phone_missing(k)) for k in ("C00000001", "C00000002")]
    results.append(result("C00000003", phone_missing("C00000003", "+447700900999")))
    (fan, sig), _, _ = causes(results)
    assert (fan.kind, fan.affected_keys) == ("member_key", 2)
    assert (sig.kind, sig.affected_keys, sig.member_key_tok) == ("signature", 1, None)
    assert sig.signature_hash == results[2].findings[0].signature_hash


def test_two_findings_in_one_key_are_not_fan_out(causes, phone_missing) -> None:
    dup = replace(phone_missing("C00000001"), json_pointer=f"/phones/{PHONE}/@1", value_class="duplicate_key")
    rcs, _, _ = causes([result("C00000001", phone_missing("C00000001"), dup)])
    assert [rc.kind for rc in rcs] == ["signature", "signature"]  # their value classes differ


def test_info_and_explained_findings_are_not_causes(causes, phone_missing, make_finding, spec) -> None:
    norm = make_finding(
        "$.email", CHANGED, "A@x", "a@x", taxonomy_code="NORMALIZATION_ONLY", severity=Severity.INFO
    )
    accepted = replace(
        phone_missing("C00000002"), rule_id="KD-X-0001", rule_classification=RuleClassification.ACCEPTED
    )
    results = [
        result("C00000001", phone_missing("C00000001"), classify(norm, spec, None)),
        result("C00000002", accepted),
        result("C00000003", replace(phone_missing("C00000003"), mask_id="9")),
    ]
    [rc], _, _ = causes(results)
    assert (rc.kind, rc.affected_keys) == ("signature", 1)


def test_other_changes_group_by_signature(causes, changed, make_finding, spec) -> None:
    parts = (("field", "phones"), ("member", PHONE), ("field", "kind"))
    kind = [
        changed(k, "$.phones[*].kind", parts=parts, member_key=PHONE, array_path="$.phones")
        for k in ("C1", "C2")
    ]
    [rc], _, _ = causes([result("C1", kind[0]), result("C2", kind[1])])
    assert (rc.kind, rc.affected_keys, rc.direction) == ("signature", 2, Direction.BOTH)
    nickname = [
        classify(make_finding("$.nickname", MISSING, "x", None, key=k), spec, None) for k in ("C1", "C2")
    ]
    [rc], _, _ = causes([result("C1", nickname[0]), result("C2", nickname[1])])
    assert rc.kind == "signature"  # a one-sided field is not an array member


def test_extra_members_fan_out_separately(causes, make_finding, spec) -> None:
    def extra(key: str):
        f = make_finding(
            "$.tags[*]", EXTRA, None, "vip", key=key, parts=(("field", "tags"), ("member", '"vip"'))
        )
        return classify(replace(f, member_key='"vip"', array_path="$.tags"), spec, None)

    [rc], _, _ = causes([result("C1", extra("C1")), result("C2", extra("C2"))])
    assert (rc.kind, rc.direction, rc.change_type) == ("member_key", Direction.B_ONLY, EXTRA)


def test_document_findings_group_by_signature(causes, spec, make_canonical, today) -> None:
    present = SideState.complete(make_canonical({"a": 1}))
    results = [compare_pair(k, present, SideState.absent(), spec, today=today) for k in ("C1", "C2")]
    [rc], _, _ = causes(results)
    assert (rc.kind, rc.affected_keys, rc.taxonomy_code, rc.max_severity) == (
        "signature",
        2,
        "MISSING_DOCUMENT",
        Severity.CRITICAL,
    )


def test_order_is_fan_out_then_severity_and_deterministic(causes, phone_missing, changed) -> None:
    results = [
        result("C00000001", phone_missing("C00000001"), changed("C00000001", "$.accounts[*].balance")),
        result("C00000002", phone_missing("C00000002")),
        result("C00000004", changed("C00000004", "$.accounts[*].balance")),
        result("C00000005", changed("C00000005")),
    ]
    first = causes(results)
    assert [(rc.kind, rc.max_severity) for rc in first[0]] == [
        ("member_key", Severity.MEDIUM),
        ("signature", Severity.HIGH),
        ("signature", Severity.MEDIUM),
    ]
    for ordering in itertools.permutations(results):
        assert causes(list(ordering)) == first


def test_sample_keys_are_capped(causes, phone_missing, tok) -> None:
    keys = [f"C0000001{i}" for i in range(8)]
    [rc], _, _ = causes([result(k, phone_missing(k)) for k in keys])
    assert rc.affected_keys == 8
    assert rc.sample_keys == tuple(sorted(tok.token("identifier", k) for k in keys))[:5]


def test_every_grouped_finding_maps_to_its_cause(causes, phone_missing, changed) -> None:
    results = [result(k, phone_missing(k)) for k in ("C00000001", "C00000002")]
    results.append(result("C00000003", changed("C00000003")))
    rcs, _, mapped = causes(results)
    ids = {rc.kind: rc.root_cause_id for rc in rcs}
    assert mapped == {
        ("C00000001", f"/phones/{PHONE}", "missing_in_b"): ids["member_key"],
        ("C00000002", f"/phones/{PHONE}", "missing_in_b"): ids["member_key"],
        ("C00000003", "/segment", "value_changed"): ids["signature"],
    }
    assert causes([]) == ([], {}, {})


def test_bug_suppressed_findings_are_root_causes_of_their_own(causes, phone_missing, changed, tok) -> None:
    """A bug rule's KNOWN_DIFF is discrepant, so its findings have a cause."""
    results = [
        result("C00000001", bug(changed("C00000001"))),
        result("C00000002", bug(changed("C00000002"))),
        result("C00000003", changed("C00000003")),  # the same difference, unsuppressed
        result("C00000004", bug(phone_missing("C00000004"))),
        result("C00000005", bug(phone_missing("C00000005"))),
    ]
    rcs, flagged, mapped = causes(results)
    by_id = {rc.root_cause_id: rc for rc in rcs}
    assert set(flagged.values()) == {"KD-BUG-0001"}
    assert sorted((by_id[i].kind, by_id[i].affected_keys) for i in flagged) == [
        ("member_key", 2),
        ("signature", 2),
    ]
    [plain] = [rc for rc in rcs if rc.root_cause_id not in flagged]
    assert (plain.kind, plain.affected_keys, plain.sample_keys) == (
        "signature",
        1,
        (tok.token("identifier", "C00000003"),),
    )
    assert mapped[("C00000001", "/segment", "value_changed")] in flagged
    assert mapped[("C00000003", "/segment", "value_changed")] == plain.root_cause_id


def test_unsuppressed_cause_ids_do_not_depend_on_bug_rules(causes, changed) -> None:
    [alone], flagged, _ = causes([result("C1", changed("C1"))])
    with_bug, _, _ = causes([result("C1", changed("C1")), result("C2", bug(changed("C2")))])
    assert alone.root_cause_id in {rc.root_cause_id for rc in with_bug} and flagged == {}


def test_a_bug_rule_on_an_info_finding_is_not_a_cause(causes, make_finding, spec) -> None:
    norm = make_finding("$.email", CHANGED, "A@x", "a@x", taxonomy_code="NORMALIZATION_ONLY")
    assert causes([result("C1", bug(classify(norm, spec, None)))]) == ([], {}, {})
