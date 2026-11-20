"""DRIFT from real reads: two reads of one key, each observed and compared as the worker does, then
judged by the recheck. DRIFT is the only outcome a single read cannot give."""

from __future__ import annotations

from datetime import date

import pytest

from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    CompareConfig,
    CompiledSpec,
    Confirmation,
    DriftReason,
    FieldRule,
    KeyConfig,
    KeyResult,
    Normalizer,
    NotFoundRule,
    Observation,
    Outcome,
    Side,
    SideConfig,
    TargetCapabilities,
)
from deltasleuth.runner.recheck import INTERMITTENT_B, confirm

KEY, TODAY = "ORG-000123", date(2026, 9, 24)
NOT_FOUND = NotFoundRule((404,))
SPEC = CompiledSpec(
    name="drift-from-reads",
    version="1.0.0",
    content_hash="0" * 64,
    normalizer_version="1.0.0",
    key=KeyConfig(name="code"),
    sides={
        side: SideConfig(side, "new" if side is Side.B else "legacy", side.value, not_found=NOT_FOUND)
        for side in Side
    },
    compare=CompareConfig(fields=(FieldRule("$.name", (Normalizer("trim"),)),)),
)
CAPABILITIES = TargetCapabilities(authoritative_absence=AuthoritativeAbsence((404,)))
ABSENT = None  # a read answered 404, which both sides declare authoritative


def read(a: str | None, b: str | None) -> KeyResult:
    """One read of the key: each side's body (or ABSENT) observed, then compared."""
    states = {
        side: observe(
            Observation(
                key=KEY,
                side=side,
                http_status=404 if body is ABSENT else 200,
                body=b'{"error": "not found"}' if body is ABSENT else body.encode(),
                observed_at="2026-09-24T00:00:00Z",
            ),
            SPEC.sides[side],
            SPEC,
            CAPABILITIES,
        )
        for side, body in ((Side.A, a), (Side.B, b))
    }
    return compare_pair(KEY, states[Side.A], states[Side.B], SPEC, today=TODAY)


ACTIVE = '{"name": "Acme Trading", "status": "ACTIVE"}'
CLOSED = '{"name": "Acme Trading", "status": "CLOSED"}'
SUSPENDED = '{"name": "Acme Trading", "status": "SUSPENDED"}'


@pytest.mark.parametrize(
    ("first", "second", "reason"),
    [
        # A moved between the reads: the first read compared a document A no longer serves.
        ((ACTIVE, CLOSED), (CLOSED, CLOSED), DriftReason.A_CHANGED),
        ((ACTIVE, CLOSED), (SUSPENDED, CLOSED), DriftReason.A_CHANGED),
        # B caught up with A: the difference was replication lag, not a defect.
        ((ACTIVE, CLOSED), (ACTIVE, ACTIVE), DriftReason.B_CONVERGED),
        ((ACTIVE, ABSENT), (ACTIVE, ACTIVE), DriftReason.B_CONVERGED),
    ],
    ids=["a-changed-to-agree", "a-changed-to-differ", "b-converged", "b-appeared"],
)
def test_a_recheck_that_reads_a_side_changed_is_drift(
    first: tuple[str | None, str | None], second: tuple[str | None, str | None], reason: DriftReason
) -> None:
    provisional = read(*first)
    assert provisional.outcome in (Outcome.MISMATCH, Outcome.NOT_FOUND_B)
    judged = confirm(provisional, read(*second), spec=SPEC)
    assert (judged.outcome, judged.drift_reason, judged.first_outcome) == (
        Outcome.DRIFT,
        reason,
        provisional.outcome,
    )
    assert (judged.findings, judged.suppressed_count, judged.confirmation) == ([], 0, Confirmation.RECHECKED)


def test_b_still_differing_after_a_change_is_the_re_read_s_mismatch_marked_intermittent() -> None:
    judged = confirm(read(ACTIVE, CLOSED), read(ACTIVE, SUSPENDED), spec=SPEC)
    assert (judged.outcome, judged.drift_reason, judged.confirmation) == (
        Outcome.MISMATCH,
        None,
        Confirmation.RECHECKED,
    )
    assert [(f.json_pointer, f.b_value, f.value_class) for f in judged.findings] == [
        ("/status", "SUSPENDED", INTERMITTENT_B)
    ]


def test_a_change_a_normaliser_removes_is_no_drift() -> None:
    # The recheck compares the normalised document: padding the trim rule removes is not a change of A.
    first = read(ACTIVE, CLOSED)
    judged = confirm(first, read('{"name": "  Acme Trading ", "status": "ACTIVE"}', CLOSED), spec=SPEC)
    assert (judged.outcome, judged.drift_reason, judged.confirmation) == (
        Outcome.MISMATCH,
        None,
        Confirmation.RECHECKED,
    )
    assert judged.findings == first.findings


@pytest.mark.parametrize(
    ("first_a", "second_a"),
    [
        (ACTIVE, '{"name": "Acme Trading", "status": "ACTIVE", "note": null}'),  # gains a null member
        ('{"name": "Acme Trading", "status": "ACTIVE", "note": null}', ACTIVE),  # loses one
    ],
)
def test_a_null_member_that_null_equals_absent_ignores_is_no_change_of_a_side(
    first_a: str, second_a: str
) -> None:
    """Under ``nullEqualsAbsent`` a member holding null is no member: a re-read of A that only gains or
    loses one compares as the same document, so the first read's mismatch is confirmed, not drift."""
    import dataclasses

    import deltasleuth.core.classify.outcome as outcome_module

    spec = dataclasses.replace(SPEC, compare=dataclasses.replace(SPEC.compare, null_equals_absent=True))

    def read_with(a: str, b: str) -> KeyResult:
        states = {
            side: observe(
                Observation(key=KEY, side=side, http_status=200, body=body.encode(),
                            observed_at="2026-09-24T00:00:00Z"),
                spec.sides[side], spec, CAPABILITIES,
            )
            for side, body in ((Side.A, a), (Side.B, b))
        }  # fmt: skip
        return outcome_module.compare_pair(KEY, states[Side.A], states[Side.B], spec, today=TODAY)

    first, second = read_with(first_a, CLOSED), read_with(second_a, CLOSED)
    assert (first.outcome, second.outcome) == (Outcome.MISMATCH, Outcome.MISMATCH)
    judged = confirm(first, second, spec=spec)
    assert (judged.outcome, judged.confirmation) == (Outcome.MISMATCH, Confirmation.RECHECKED)
