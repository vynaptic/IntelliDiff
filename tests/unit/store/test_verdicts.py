"""The verdict as the store's statements read it: every outcome, with and without a finding under a bug
rule, is what ``types.derive_verdict`` makes of it, in the shared fragment and in each count built on it."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.core.types import (
    ChangeType,
    Confirmation,
    DriftReason,
    Finding,
    KeyResult,
    Outcome,
    RuleClassification,
    Severity,
    SideState,
    SideStateKind,
    Verdict,
    derive_verdict,
)
from deltasleuth.security.tokenise import VALUE_FORM, mask_finding
from deltasleuth.store import repo
from tests.conftest import TEST_DATA_KEY, TEST_SEALER, TESTER
from tests.unit.store.test_repo import SPEC, TOKENISER

pytestmark = pytest.mark.db

COMPLETE = SideState(SideStateKind.COMPLETE)
COMBOS = [(outcome, bug) for outcome in Outcome for bug in (False, True)]


def _name(prefix: str, outcome: Outcome, bug: bool) -> str:
    return f"{prefix}-{outcome.value.lower()}-{'bug' if bug else 'ok'}"


def _result(key: str, outcome: Outcome, bug: bool) -> KeyResult:
    """A result of ``outcome`` with one finding under a rule: a bug rule's, or an accepted one's."""
    reasons: dict[str, Any] = {}
    if outcome is Outcome.INCONCLUSIVE:
        reasons["inconclusive_reason"] = "recheck_failed"
    if outcome is Outcome.DRIFT:
        reasons["drift_reason"] = DriftReason.A_CHANGED
    finding = Finding(
        key=key, json_pointer="/f", path_pattern="$.f", change_type=ChangeType.VALUE_CHANGED, a_value="x",
        b_value="y", pointer_parts=(("field", "f"),), taxonomy_code="VALUE_MISMATCH", severity=Severity.HIGH,
        rule_id="KD-BUG" if bug else "KD-OK",
        rule_classification=RuleClassification.BUG if bug else RuleClassification.ACCEPTED,
    )  # fmt: skip
    return KeyResult(
        key, outcome, COMPLETE, COMPLETE, confirmation=Confirmation.RECHECKED, findings=[finding], **reasons
    )


async def _run(pool: AsyncConnectionPool, cycles: list[dict[str, tuple[Outcome, bool]]]) -> str:
    """A run whose cycles hold these results (masked key -> outcome and bug), from cycle 1 on."""
    async with pool.connection() as conn:
        spec_id = await repo.save_spec(conn, SPEC, created_by=TESTER.actor)
        run_id = repo.new_run_id()
        await repo.create_run(
            conn, spec_id, "soak", TEST_SEALER.seal_keys({k: k for k in cycles[0]}), {"samples_dir": None},
            None, run_id=run_id, created_by=TESTER.actor, retention="standard", data_key=TEST_DATA_KEY,
        )  # fmt: skip
        for cycle, results in enumerate(cycles, 1):
            if cycle > 1:
                sealed = TEST_SEALER.seal_keys({k: k for k in results})
                await repo._insert_keys(conn, run_id, cycle, sealed, None)
            for key, (outcome, bug) in results.items():
                r = _result(key, outcome, bug)
                masked = [mask_finding(f, SPEC, TOKENISER, form=VALUE_FORM) for f in r.findings]
                await repo.save_key_result(conn, run_id, cycle, key, r, masked)
    return run_id


def _each(prefix: str) -> Iterator[tuple[str, Outcome, bool]]:
    for outcome, bug in COMBOS:
        yield _name(prefix, outcome, bug), outcome, bug


def _count(verdicts: set[Verdict], negate: bool = False) -> int:
    return sum((derive_verdict(o, b) in verdicts) != negate for o, b in COMBOS)


async def test_the_shared_fragment_reads_every_outcome_as_derive_verdict(pool: AsyncConnectionPool) -> None:
    run_id = await _run(pool, [{key: (outcome, bug) for key, outcome, bug in _each("k")}])
    async with pool.connection() as conn:
        rows = await repo._rows(
            conn,
            f"SELECT k.key_masked, {repo._VERDICT} AS verdict FROM ds.results k"  # noqa: S608
            " WHERE k.run_id = %(r)s AND k.cycle = %(c)s",
            {"r": run_id, "c": 1},
        )
    read = {row["key_masked"]: row["verdict"] for row in rows}
    assert read == {key: derive_verdict(outcome, bug).value for key, outcome, bug in _each("k")}


@pytest.mark.parametrize(
    ("group", "before", "after"),
    [
        ("new", "MATCH", None),  # read identical, then each combination: newly discrepant when DIFFERENT
        ("fixed", "MISMATCH", None),  # discrepant, then each: no longer discrepant when MATCH
        ("was", None, "MATCH"),  # each, then identical: no longer discrepant when it was DIFFERENT
        ("now", None, "MISMATCH"),  # each, then discrepant: newly discrepant unless it was DIFFERENT
    ],
)
async def test_the_digest_counts_a_key_as_the_counters_do(
    pool: AsyncConnectionPool, group: str, before: str | None, after: str | None
) -> None:
    """A key that moves from MISMATCH to a KNOWN_DIFF under a bug rule is still discrepant, as the counters
    and the readiness gate count it, so the digest names it neither newly nor no longer discrepant."""
    first, second = {}, {}
    for key, outcome, bug in _each(group):
        first[key] = (Outcome(before), False) if before else (outcome, bug)
        second[key] = (Outcome(after), False) if after else (outcome, bug)
    run_id = await _run(pool, [first, second])
    async with pool.connection() as conn:
        found = await repo.discrepant_changes(conn, run_id, 2, read_all_before=False, read_all=False)
    different, match = {Verdict.DIFFERENT}, {Verdict.MATCH}
    expected = {
        "new": (_count(different), 0),
        "fixed": (0, _count(match)),
        "was": (0, _count(different)),
        "now": (_count(different, negate=True), 0),
    }[group]
    assert (found["newly"], found["no_longer"]) == expected


async def test_readiness_counts_the_same_different_keys(pool: AsyncConnectionPool) -> None:
    """The keys readiness tracks from one cycle to the next are those ``derive_verdict`` calls DIFFERENT."""
    first = {key: (Outcome.MATCH, False) for key, _, _ in _each("new")}
    first |= {key: (outcome, bug) for key, outcome, bug in _each("was")}
    second = {key: (outcome, bug) for key, outcome, bug in _each("new")}
    second |= {key: (Outcome.MATCH, False) for key, _, _ in _each("was")}
    run_id = await _run(pool, [first, second])
    async with pool.connection() as conn:
        found = await repo.cycle_changes(conn, run_id, 2)
    different = _count({Verdict.DIFFERENT})
    assert found == {"new": different, "persistent": 0, "fixed": different, "other": 0}
