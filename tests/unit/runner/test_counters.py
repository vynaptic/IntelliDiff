"""Cycle counters (§7.1): verdict buckets, outstanding and cancelled keys, rates, exact reconciliation."""

from __future__ import annotations

from typing import Any

import pytest

from deltasleuth.core.types import Outcome
from deltasleuth.runner.counters import ReconciliationError, Tally, count_cycle

DUE = (Outcome.MISMATCH,)


def row(key: str, outcome: str, *, confirmation: str = "provisional", reason: str | None = None,
        drift: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"key_masked": key, "outcome": outcome, "confirmation": confirmation, "first_outcome": outcome,
            "inconclusive_reason": reason, "drift_reason": drift}  # fmt: skip


def finding(
    rule: str | None = None, taxonomy: str = "VALUE_MISMATCH", classification: str | None = None
) -> dict:
    return {"rule_id": rule, "rule_classification": classification, "taxonomy_code": taxonomy}


RESULTS = [
    row("K1", "MATCH"),
    row("K2", "MISMATCH", confirmation="rechecked"),
    row("K3", "MISMATCH"),  # live and provisional: owed a recheck
    row("K4", "KNOWN_DIFF"),
    row("K5", "KNOWN_DIFF"),  # a bug rule: different
    row("K6", "INCONCLUSIVE", reason="truncated"),
    row("K7", "DRIFT", drift="b_converged", confirmation="rechecked"),
    row("K8", "NOT_FOUND_B", confirmation="rechecked"),
]
FINDINGS = {"K4": [finding("KD-1")], "K5": [finding("KD-2", classification="bug")],
            "K1": [finding(taxonomy="NORMALIZATION_ONLY")]}  # fmt: skip


def test_keys_are_bucketed_by_verdict_and_every_identity_holds() -> None:
    c = count_cycle(RESULTS, FINDINGS, total=9, live=True, recheck_outcomes=DUE)
    buckets = ("total", "match", "different", "indeterminate", "cancelled", "outstanding", "compared")
    assert [c[name] for name in buckets] == [9, 2, 3, 2, 1, 1, 5]
    assert (c["different_provisional"], c["suppressed"], c["api_uncomparable"]) == (1, 2, 1)
    assert c["suppressed_by_rule"] == {"KD-1": 1, "KD-2": 1} and c["normalization_only_keys"] == 1
    assert (c["mismatch_rate"], c["coverage"]) == ("0.6", "0.555556")
    assert all(i["holds"] for i in c["reconciliation"])
    # File samples cannot be re-read: a provisional discrepancy is different, never outstanding.
    filed = count_cycle(RESULTS, FINDINGS, total=9, live=False, recheck_outcomes=DUE)
    assert (filed["different"], filed["outstanding"], filed["different_provisional"]) == (4, 0, 2)


def test_a_scans_identical_rows_count_as_matches_confirmed_in_bulk() -> None:
    c = count_cycle(RESULTS, FINDINGS, total=9 + 1000, live=True, recheck_outcomes=DUE, bulk_match=1000)
    assert (c["total"], c["match"], c["cancelled"]) == (1009, 1002, 1)
    assert (c["by_outcome"]["MATCH"], c["by_confirmation"]["bulk_exhaustive"]) == (1001, 1000)
    assert all(i["holds"] for i in c["reconciliation"])


def test_rates_are_unavailable_without_a_denominator() -> None:
    c = count_cycle(
        [row("K1", "INCONCLUSIVE", reason="error_a")], {}, total=1, live=True, recheck_outcomes=DUE
    )
    assert (c["mismatch_rate"], c["coverage"]) == ("unavailable", "0")
    assert count_cycle([], {}, total=0, live=True, recheck_outcomes=DUE)["coverage"] == "unavailable"


@pytest.mark.parametrize(
    ("results", "total", "broken"),
    [
        ([row("K1", "INCONCLUSIVE")], 1, "INCONCLUSIVE = keys by inconclusive reason"),
        ([row("K1", "DRIFT")], 1, "DRIFT = keys by drift reason"),
    ],
)
def test_counters_that_do_not_reconcile_are_refused_by_name(results: list, total: int, broken: str) -> None:
    with pytest.raises(ReconciliationError, match=broken):
        count_cycle(results, {}, total=total, live=True, recheck_outcomes=DUE)


@pytest.mark.parametrize("live", [True, False])
def test_a_cycle_tallied_a_page_at_a_time_counts_as_the_whole_cycle_does(live: bool) -> None:
    """SCALE S2: every outcome, an accepted and a bug rule, normalisation-only keys, schema drift (a reason
    and a finding), a scan's bulk match and cancelled keys; each page brings only its own keys' findings."""
    results = [
        *RESULTS,
        row("K9", "NOISE_ONLY"),
        row("K10", "NOT_FOUND_A"),
        row("K11", "INCONCLUSIVE", reason="schema_drift"),
        row("K12", "MATCH", confirmation="frozen_cut"),
        row("K13", "KNOWN_DIFF", confirmation="rechecked"),
    ]
    findings = FINDINGS | {
        "K9": [finding(taxonomy="NORMALIZATION_ONLY"), finding(taxonomy="NORMALIZATION_ONLY")],
        "K12": [finding(taxonomy="SCHEMA_DRIFT")],
        "K13": [finding("KD-1"), finding("KD-1"), finding("KD-2", classification="bug")],
    }
    assert {r["outcome"] for r in results} == {o.value for o in Outcome}
    whole = count_cycle(results, findings, total=1015, live=live, recheck_outcomes=DUE, bulk_match=1000)
    assert (whole["cancelled"], whole["schema_drift"], whole["normalization_only_keys"]) == (2, 2, 2)
    assert whole["suppressed_by_rule"] == {"KD-1": 2, "KD-2": 2}
    for size in (1, 3, 5, len(results)):
        tally = Tally(live=live, recheck_outcomes=DUE)
        for start in range(0, len(results), size):
            page = results[start : start + size]
            of_page = {key: findings[key] for r in page if (key := r["key_masked"]) in findings}
            for r in page:
                tally.add(r, of_page.get(r["key_masked"], ()))
        assert tally.counters(total=1015, bulk_match=1000) == whole
