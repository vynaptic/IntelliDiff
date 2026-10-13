"""Per-key outcomes, inconclusive precedence and verdicts."""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.types import (
    ERROR_REASONS,
    INCOMPLETE_REASONS,
    INCONCLUSIVE_PRECEDENCE,
    ChangeType,
    Counted,
    Direction,
    KeyedMap,
    KnownDiffRule,
    Outcome,
    RuleClassification,
    Severity,
    SideState,
    Verdict,
)

CHANGED, MISSING = ChangeType.VALUE_CHANGED, ChangeType.MISSING_IN_B
DOC = {"customerId": "C00000001", "status": "CLOSED"}
ABSENT, INC, ERR = SideState.absent(), SideState.incomplete, SideState.error

# The precedence as a literal on purpose: an expectation derived from
# types.INCONCLUSIVE_PRECEDENCE would agree with any reordering of that tuple.
ERROR_ORDER = ("both_error", "error_a", "error_b", "timeout", "rate_limited", "circuit_open")
INCOMPLETE_ORDER = (
    *("truncated", "unstable_pagination", "unparseable", "resource_limit", "schema_drift"),
    *("absence_unconfirmed", "watermark_missing", "duplicate_member_name", "identity_ambiguous"),
)
ORDER = (*ERROR_ORDER, *INCOMPLETE_ORDER, "absent_both", "skipped", "recheck_failed")


@pytest.fixture
def run(spec, today, make_canonical):
    """compare_pair for key C00000001; a dict or None side is a COMPLETE document."""

    def run(a, b, *, spec=spec, differ=None):
        a = SideState.complete(make_canonical(a)) if isinstance(a, dict | None) else a
        b = SideState.complete(make_canonical(b)) if isinstance(b, dict | None) else b
        return compare_pair("C00000001", a, b, spec, today=today, differ=differ)

    return run


def test_precedence_tuples_are_section_7_1() -> None:
    assert (ERROR_REASONS, INCOMPLETE_REASONS) == (ERROR_ORDER, INCOMPLETE_ORDER)
    assert INCONCLUSIVE_PRECEDENCE == ORDER


def test_precedence_over_every_pair_of_reasons(run, stub_differ) -> None:
    def state(reason: str) -> SideState:
        return ERR(reason) if reason in ERROR_ORDER else INC(reason)

    for ra, rb in itertools.product(ORDER[1:], repeat=2):
        differ = stub_differ()
        r = run(state(ra), state(rb), differ=differ)
        both_error = ra in ERROR_ORDER and rb in ERROR_ORDER
        expected = "both_error" if both_error else min((ra, rb), key=ORDER.index)
        assert (r.outcome, r.inconclusive_reason, r.first_outcome) == (
            Outcome.INCONCLUSIVE,
            expected,
            r.outcome,
        )
        assert r.findings == [] and r.verdict is Verdict.INDETERMINATE and differ.calls == []


@pytest.mark.parametrize(
    ("a", "b", "reason"),
    [
        (ABSENT, INC("absence_unconfirmed"), "absence_unconfirmed"),
        (ERR("rate_limited"), ABSENT, "rate_limited"),
        (ABSENT, ABSENT, "absent_both"),
    ],
)
def test_an_absent_side_rescues_nothing(run, a, b, reason) -> None:
    r = run(a, b)
    assert (r.outcome, r.inconclusive_reason) == (Outcome.INCONCLUSIVE, reason)


@pytest.mark.parametrize("reason", (*INCOMPLETE_REASONS, "skipped", "recheck_failed"))
def test_one_complete_side_does_not_rescue_the_other(run, reason) -> None:
    for a, b in ((DOC, INC(reason)), (INC(reason), DOC)):
        assert run(a, b).inconclusive_reason == reason


def test_not_found_b_emits_missing_document(run, stub_differ) -> None:
    differ = stub_differ()
    r = run(DOC, ABSENT, differ=differ)
    assert (r.outcome, r.verdict, differ.calls) == (Outcome.NOT_FOUND_B, Verdict.DIFFERENT, [])
    [f] = r.findings
    assert (f.taxonomy_code, f.severity, f.change_type, f.direction) == (
        "MISSING_DOCUMENT",
        Severity.CRITICAL,
        MISSING,
        Direction.A_ONLY,
    )
    assert (f.path_pattern, f.json_pointer, f.member_key, f.pointer_parts) == ("$", "", "C00000001", ())
    assert (f.a_value, f.b_value, f.rule_id) == (DOC, None, None) and f.signature_hash is not None


def test_not_found_a_emits_extra_document(run) -> None:
    r = run(ABSENT, DOC)
    [f] = r.findings
    assert (r.outcome, f.taxonomy_code, f.change_type, f.direction) == (
        Outcome.NOT_FOUND_A,
        "EXTRA_DOCUMENT",
        ChangeType.EXTRA_IN_B,
        Direction.B_ONLY,
    )
    assert (f.a_value, f.b_value) == (None, DOC)


@pytest.mark.parametrize(
    ("classification", "verdict"),
    [(RuleClassification.ACCEPTED, Verdict.MATCH), (RuleClassification.BUG, Verdict.DIFFERENT)],
)
def test_known_diff_suppresses_a_one_sided_key(run, make_spec, classification, verdict) -> None:
    when = "taxonomy_code == 'MISSING_DOCUMENT' && a.status == 'CLOSED'"
    kd = KnownDiffRule("KD-DOC-0001", when, classification, "v2 does not serve closed customers", "CUST-2001")
    r = run(DOC, ABSENT, spec=make_spec(known_diffs=(kd,)))
    assert (r.outcome, r.suppressed_count, r.verdict, r.findings[0].rule_id) == (
        Outcome.KNOWN_DIFF,
        1,
        verdict,
        "KD-DOC-0001",
    )


def test_the_pack_aliases_the_document_codes_and_rules_see_the_alias(run, make_spec) -> None:
    assert run(DOC, ABSENT, spec=make_spec(domain_pack="entity-linking")).findings[0].taxonomy_code == (
        "MISSING_ENTITY"
    )
    kd = KnownDiffRule(
        "KD-EL-0009", "taxonomy_code == 'EXTRA_ENTITY'", RuleClassification.ACCEPTED, "r", "L-1"
    )
    r = run(ABSENT, DOC, spec=make_spec(domain_pack="entity-linking", known_diffs=(kd,)))
    assert (r.outcome, r.findings[0].taxonomy_code) == (Outcome.KNOWN_DIFF, "EXTRA_ENTITY")


def test_no_findings_is_match(run, spec, stub_differ, make_canonical) -> None:
    differ = stub_differ()
    r = run(DOC, {"x": 1}, differ=differ)
    assert (r.outcome, r.verdict, r.suppressed_count) == (Outcome.MATCH, Verdict.MATCH, 0)
    canonicals = {"a": make_canonical(DOC), "b": make_canonical({"x": 1})}
    assert differ.calls == [{**canonicals, "spec": spec, "key": "C00000001"}]


def test_the_default_differ_is_the_engines(run) -> None:
    [f] = run({"status": "OPEN"}, {"status": "CLOSED"}).findings
    assert (f.path_pattern, f.change_type, f.taxonomy_code) == ("$.status", CHANGED, "VALUE_MISMATCH")


def _norm(make_finding):
    return make_finding(
        "$.email", CHANGED, "A@x", "a@x", taxonomy_code="NORMALIZATION_ONLY", severity=Severity.INFO
    )


def test_info_only_findings_stay_match(run, make_finding, stub_differ) -> None:
    r = run(DOC, DOC, differ=stub_differ(_norm(make_finding)))
    assert (r.outcome, [f.taxonomy_code for f in r.findings]) == (Outcome.MATCH, ["NORMALIZATION_ONLY"])


def test_an_unsuppressed_finding_is_a_mismatch(run, make_finding, stub_differ) -> None:
    segment = make_finding("$.segment", CHANGED, "PRIVATE", "WEALTH")  # KD-CP-0001 suppresses it
    r = run(DOC, DOC, differ=stub_differ(segment, make_finding("$.status", CHANGED, "ACTIVE", "CLOSED")))
    assert (r.outcome, r.suppressed_count, r.verdict) == (Outcome.MISMATCH, 1, Verdict.DIFFERENT)


@pytest.mark.parametrize(
    ("finding", "verdict"),
    [
        (("$.segment", CHANGED, "PRIVATE", "WEALTH"), Verdict.MATCH),  # KD-CP-0001, expected_by_design
        (("$.preferences.marketing.sms", MISSING, False, None), Verdict.DIFFERENT),  # KD-CP-0002, bug
    ],
)
def test_all_suppressed_by_rules_is_known_diff(run, make_finding, stub_differ, finding, verdict) -> None:
    r = run(DOC, DOC, differ=stub_differ(make_finding(*finding), _norm(make_finding)))
    assert (r.outcome, r.suppressed_count, r.verdict) == (Outcome.KNOWN_DIFF, 1, verdict)


@pytest.mark.parametrize(
    ("others", "outcome", "suppressed"),
    [
        ((), Outcome.NOISE_ONLY, 1),
        ((("$.segment", "PRIVATE", "WEALTH"),), Outcome.KNOWN_DIFF, 2),  # a mask and a rule
        ((("$.segment", "X", "Y"),), Outcome.MISMATCH, 1),  # a mask and an unsuppressed finding
    ],
)
def test_mask_suppression(run, make_finding, stub_differ, others, outcome, suppressed) -> None:
    masked = make_finding("$.status", CHANGED, "A", "B", mask_id="7")
    r = run(DOC, DOC, differ=stub_differ(masked, *(make_finding(p, CHANGED, a, b) for p, a, b in others)))
    assert (r.outcome, r.suppressed_count) == (outcome, suppressed)


def test_a_rule_on_an_info_finding_alone_is_match(run, make_spec, make_finding, stub_differ) -> None:
    kd = KnownDiffRule(
        "KD-I-0001", "taxonomy_code == 'NORMALIZATION_ONLY'", RuleClassification.ACCEPTED, "r", "T-1"
    )
    r = run(DOC, DOC, spec=make_spec(known_diffs=(kd,)), differ=stub_differ(_norm(make_finding)))
    assert (r.outcome, r.suppressed_count) == (Outcome.MATCH, 1)


@pytest.mark.parametrize(
    ("doc_a", "doc_b"),
    [
        pytest.param({}, {}, id="empty-objects"),
        pytest.param(
            {"accounts": KeyedMap(), "consents": []}, {"accounts": KeyedMap()}, id="empty-containers"
        ),
        pytest.param(None, {}, id="null-root"),
        pytest.param({"a": {"b": [{}]}}, {"a": {}}, id="nested-empty"),
    ],
)
def test_two_documents_without_data_are_never_a_match(run, stub_differ, doc_a, doc_b) -> None:
    """Include/exclude/valueFilter that select nothing on either side hash equal."""
    differ = stub_differ()
    r = run(doc_a, doc_b, differ=differ)
    assert (r.outcome, r.inconclusive_reason, r.verdict) == (
        Outcome.INCONCLUSIVE,
        "unparseable",
        Verdict.INDETERMINATE,
    )
    assert r.attrs["inconclusive_detail"] == "nothing selected on either side"
    assert r.findings == [] and differ.calls == []


@pytest.mark.parametrize(
    ("doc_a", "doc_b"),
    [
        ({}, {"x": 1}),  # one side with data is a real difference
        ({"flag": None}, {"flag": None}),  # a null value is a value both sides agree on
        ({"tags": KeyedMap({'"vip"': ["vip"]}, mode="set")}, {}),
        ({"n": [Counted(False, 2)]}, {}),
    ],
)
def test_a_document_with_any_value_is_compared(run, stub_differ, doc_a, doc_b) -> None:
    differ = stub_differ()
    assert run(doc_a, doc_b, differ=differ).outcome is Outcome.MATCH and len(differ.calls) == 1


def test_an_empty_document_against_an_absent_side_is_still_not_found(run) -> None:
    assert run({}, ABSENT).outcome is Outcome.NOT_FOUND_B


def test_rule_errors_land_on_the_result(run, make_spec, make_finding, stub_differ) -> None:
    kd = KnownDiffRule("KD-E-0001", "a.status == 'X'", RuleClassification.ACCEPTED, "reason", "T-3")
    r = run(
        DOC,
        DOC,
        spec=make_spec(known_diffs=(kd,)),
        differ=stub_differ(make_finding("$.s", CHANGED, "A", "B")),
    )
    assert r.outcome is Outcome.MISMATCH
    assert r.attrs["known_diff_errors"] == [
        {
            "rule_id": "KD-E-0001",
            "path_pattern": "$.s",
            "change_type": "value_changed",
            "code": "INVALID_ARGUMENT",
        }
    ]


@pytest.mark.parametrize(
    ("outcome", "verdict"),
    [
        (Outcome.MATCH, Verdict.MATCH),
        (Outcome.NOISE_ONLY, Verdict.MATCH),
        (Outcome.MISMATCH, Verdict.DIFFERENT),
        (Outcome.NOT_FOUND_A, Verdict.DIFFERENT),
        (Outcome.NOT_FOUND_B, Verdict.DIFFERENT),
        (Outcome.INCONCLUSIVE, Verdict.INDETERMINATE),
        (Outcome.DRIFT, Verdict.INDETERMINATE),
    ],
)
def test_verdicts(run, outcome, verdict) -> None:
    assert replace(run(ABSENT, ABSENT), outcome=outcome).verdict is verdict


@pytest.mark.parametrize(("selected_a", "selected_b"), [(True, False), (False, True)])
def test_a_row_selected_on_one_side_only_is_filter_membership(
    run, make_canonical, selected_a, selected_b
) -> None:
    """The key left one side's filtered population; its values are compared all the same."""
    a = SideState.complete(make_canonical({"status": "ACTIVE"}), selected=selected_a)
    b = SideState.complete(make_canonical({"status": "CLOSED"}), selected=selected_b)
    r = run(a, b)
    assert r.outcome is Outcome.MISMATCH
    status, membership = r.findings
    assert (status.path_pattern, status.taxonomy_code) == ("$.status", "VALUE_MISMATCH")
    assert (membership.taxonomy_code, membership.severity, membership.change_type) == (
        "FILTER_MEMBERSHIP",
        Severity.MEDIUM,
        CHANGED,
    )
    assert (membership.path_pattern, membership.json_pointer) == ("$", "")
    assert (membership.a_value, membership.b_value) == (selected_a, selected_b)


@pytest.mark.parametrize(
    ("a", "b", "outcome"),
    [
        (True, True, Outcome.MISMATCH),
        (False, False, Outcome.MISMATCH),  # both rows left the population: still compared
        (False, ABSENT, Outcome.NOT_FOUND_B),  # a missing row is not a membership change
    ],
)
def test_no_filter_membership_unless_exactly_one_present_row_is_selected(
    run, make_canonical, a, b, outcome
) -> None:
    def state(selected, doc):
        return (
            selected
            if isinstance(selected, SideState)
            else SideState.complete(make_canonical(doc), selected=selected)
        )

    r = run(state(a, {"status": "ACTIVE"}), state(b, {"status": "CLOSED"}))
    assert r.outcome is outcome
    assert "FILTER_MEMBERSHIP" not in [f.taxonomy_code for f in r.findings]
