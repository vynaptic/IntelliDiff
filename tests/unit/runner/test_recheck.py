"""Confirm-on-mismatch on hand-built results: how a recheck judges the first read."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import (
    Canonical,
    ChangeType,
    Confirmation,
    DriftReason,
    Finding,
    KeyResult,
    Outcome,
    RuleClassification,
    Severity,
    Side,
    SideState,
)
from deltasleuth.runner.recheck import INTERMITTENT_B, RecheckPolicy, confirm, freeze

from .conftest import SPEC as SPEC_TEXT

SPEC = compile_spec(parse_yaml_text(SPEC_TEXT))


def complete(h: str, raw: str | None = None) -> SideState:
    return SideState.complete(Canonical(doc={}, hash=h, raw_doc={}, raw_hash=raw or "r" + h))


def finding(
    severity: Severity = Severity.HIGH,
    *,
    rule: str | None = None,
    bug: bool = False,
    pattern: str = "$.status",
) -> Finding:
    f = Finding(
        key="K1",
        json_pointer="/status",
        path_pattern=pattern,
        change_type=ChangeType.VALUE_CHANGED,
        severity=severity,
        taxonomy_code="VALUE_MISMATCH",
        value_class="other",
        rule_id=rule,
        rule_classification=(RuleClassification.BUG if bug else RuleClassification.ACCEPTED)
        if rule
        else None,
    )
    f.signature_hash = signature_hash(f, SPEC)
    return f


def result(outcome: Outcome, a: SideState, b: SideState, *findings: Finding, **kw: Any) -> KeyResult:
    ids = {"obs_a_id": "O-a2", "obs_b_id": "O-b2"} | kw
    return KeyResult("K1", outcome, a, b, findings=list(findings), first_outcome=outcome, **ids)


A1, A2, B1, B2 = complete("A1"), complete("A2"), complete("B1"), complete("B2")
ERROR_B = SideState.error("error_b")
FIRST = result(Outcome.MISMATCH, A1, B1, finding(), obs_a_id="O-a1", obs_b_id="O-b1")


def test_unchanged_sides_confirm_the_first_read() -> None:
    # A raw-only change (a normaliser removes it) is not a change of A: a recheck hashes the normalised
    # document.
    judged = confirm(FIRST, result(Outcome.MISMATCH, complete("A1", raw="jitter"), B1, finding()), spec=SPEC)
    assert (judged.outcome, judged.confirmation, judged.drift_reason) == (
        Outcome.MISMATCH,
        Confirmation.RECHECKED,
        None,
    )
    assert (judged.obs_a_id, judged.obs_b_id, judged.findings) == ("O-a1", "O-b1", FIRST.findings)


@pytest.mark.parametrize(
    ("second", "outcome", "reason"),
    [
        (
            result(Outcome.INCONCLUSIVE, A1, ERROR_B, inconclusive_reason="error_b"),
            Outcome.INCONCLUSIVE,
            "recheck_failed",
        ),
        (result(Outcome.MISMATCH, A2, B1, finding()), Outcome.DRIFT, DriftReason.A_CHANGED),
        (result(Outcome.MATCH, A1, B2, finding(Severity.INFO)), Outcome.DRIFT, DriftReason.B_CONVERGED),
        (result(Outcome.KNOWN_DIFF, A1, B2, finding(rule="KD-1")), Outcome.DRIFT, DriftReason.B_CONVERGED),
    ],
)
def test_judgements_without_findings(second: KeyResult, outcome: Outcome, reason: object) -> None:
    judged = confirm(FIRST, second, spec=SPEC)
    assert judged.outcome is outcome and reason in (judged.inconclusive_reason, judged.drift_reason)
    assert (judged.findings, judged.suppressed_count, judged.confirmation) == ([], 0, Confirmation.RECHECKED)
    assert (judged.first_outcome, judged.obs_b_id) == (Outcome.MISMATCH, "O-b2")  # it cites the re-reads


def test_b_changed_but_still_different_is_intermittent() -> None:
    second = result(Outcome.MISMATCH, A1, B2, finding(), finding(Severity.INFO))
    discrepant, info = confirm(FIRST, second, spec=SPEC).findings
    assert (discrepant.value_class, info.value_class) == (INTERMITTENT_B, "other")
    assert discrepant.signature_hash == signature_hash(discrepant, SPEC) != FIRST.findings[0].signature_hash
    assert second.findings[0].value_class == "other"  # the input is not mutated


def test_intermittent_findings_are_signed_over_the_published_pattern() -> None:
    # $.name is tokenised, so a member name below it is data: never hashed as it is.
    member = finding(pattern="$.name['alice@example.com']")
    first = result(Outcome.MISMATCH, A1, B1, member)
    (tagged,) = confirm(first, result(Outcome.MISMATCH, A1, B2, member), spec=SPEC).findings
    published = dataclasses.replace(tagged, path_pattern="$.name[*]")
    assert tagged.signature_hash == signature_hash(published) != signature_hash(tagged)


@pytest.mark.parametrize(
    ("second", "confirmation"),
    [
        (result(Outcome.MATCH, A1, complete("B9")), Confirmation.RECHECKED),
        (result(Outcome.KNOWN_DIFF, A1, B2, finding(rule="KD-1")), Confirmation.RECHECKED),
        (result(Outcome.MISMATCH, A1, B2, finding()), Confirmation.PROVISIONAL),
        (result(Outcome.NOT_FOUND_B, A1, SideState.absent(), finding()), Confirmation.PROVISIONAL),
        (result(Outcome.KNOWN_DIFF, A1, B2, finding(rule="KD-1", bug=True)), Confirmation.PROVISIONAL),
    ],
)
def test_a_failed_first_read_makes_the_recheck_the_first_comparable_read(
    second: KeyResult, confirmation: Confirmation
) -> None:
    # No first document to compare with, so no drift and nothing intermittent; one comparable
    # read cannot confirm a discrepancy, which stays owed a recheck.
    first = result(Outcome.INCONCLUSIVE, A1, ERROR_B, inconclusive_reason="error_b")
    judged = confirm(first, second, spec=SPEC)
    assert (judged.outcome, judged.drift_reason, judged.confirmation) == (second.outcome, None, confirmation)
    assert judged.first_outcome is Outcome.INCONCLUSIVE and judged.findings == second.findings


def test_the_policy_rechecks_provisional_results_of_its_outcomes() -> None:
    policy = RecheckPolicy.from_spec(
        SPEC.raw | {"recheck": {"delay": "1s", "times": 2, "outcomes": ["MISMATCH"]}}
    )
    assert (policy.delay_s, policy.times, policy.one_shot_delay_s, policy.one_shot_times) == (1.0, 2, 2.0, 1)
    assert policy.due(FIRST) and not policy.due(
        dataclasses.replace(FIRST, confirmation=Confirmation.RECHECKED)
    )
    truncated = result(Outcome.INCONCLUSIVE, A1, ERROR_B, inconclusive_reason="truncated")
    assert not policy.due(truncated)
    default = RecheckPolicy.from_spec(SPEC.raw)  # INCONCLUSIVE is rechecked, unless a side cannot be read
    assert default.due(truncated) and not default.due(
        dataclasses.replace(truncated, side_a=SideState.incomplete("skipped"))
    )


def test_fixed_files_are_read_once() -> None:
    """A recheck reads no side of fixed files again, so none is due where their
    bytes alone would answer it: between two fixed sides, or when the fixed side is the unusable one (a line
    that was not a row, a key several rows hold). A read that failed read no bytes: it is still due. A result
    both of whose sides read fixed files is final as read (frozen_cut)."""
    policy = RecheckPolicy.from_spec(SPEC.raw)
    a, b, both = {Side.A}, {Side.B}, {Side.A, Side.B}
    assert policy.due(FIRST) and policy.due(FIRST, a) and policy.due(FIRST, b) and not policy.due(FIRST, both)
    not_rows = SideState.incomplete("unparseable", "no row read holds the key")
    ambiguous = SideState.incomplete("identity_ambiguous")
    for unusable in (not_rows, ambiguous):
        on_a = result(Outcome.INCONCLUSIVE, unusable, B1, inconclusive_reason=unusable.reason)
        assert policy.due(on_a) and policy.due(on_a, b) and not policy.due(on_a, a)
    failed = result(Outcome.INCONCLUSIVE, SideState.error("error_a"), B1, inconclusive_reason="error_a")
    assert policy.due(failed, a) and policy.due(failed, both)  # the side is read again
    absent = result(Outcome.NOT_FOUND_A, SideState.absent(), B1)
    assert policy.due(absent, a) and not policy.due(absent, both)  # the live side may still catch up
    assert freeze(dataclasses.replace(FIRST), both).confirmation is Confirmation.FROZEN_CUT
    assert freeze(dataclasses.replace(absent), both).confirmation is Confirmation.FROZEN_CUT
    for kept, fixed in ((FIRST, a), (FIRST, ()), (failed, both)):
        assert freeze(dataclasses.replace(kept), fixed).confirmation is Confirmation.PROVISIONAL
