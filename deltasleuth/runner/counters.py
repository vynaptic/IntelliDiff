"""A cycle's counters (§7.1) from its stored results; every identity must hold exactly.

``total = match + different + indeterminate + cancelled + outstanding``: cancelled keys never got a
result, and outstanding keys are provisional discrepancies still owed a recheck. File evidence cannot
be re-read, so a file-mode discrepancy counts as ``different`` (``different_provisional`` says how
many were never re-read). ``compared = match + different``; the mismatch rate is
``different / compared`` and coverage ``compared / total``, "unavailable" without a denominator.
A table scan's identical rows have no result rows: they count as MATCH confirmed ``bulk_exhaustive``.
``Tally`` counts a result at a time, for a cycle read in pages; ``count_cycle`` is it over a list.
``count_groups`` counts the same from grouped rows: a sample's strata (docs/mvp/SAMPLING.md §8.1).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence, Set
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from deltasleuth.core.types import (
    INCONCLUSIVE_PRECEDENCE,
    Confirmation,
    DriftReason,
    Outcome,
    Verdict,
    derive_verdict,
)

Row = Mapping[str, Any]
UNAVAILABLE = "unavailable"
_COMPARABLE = ("MATCH", "MISMATCH", "KNOWN_DIFF", "NOISE_ONLY", "NOT_FOUND_A", "NOT_FOUND_B")


class ReconciliationError(RuntimeError):
    """The counters do not add up; a report built on them would be wrong."""


def verdict(result: Row, findings: Iterable[Row]) -> Verdict:
    """The derived verdict of a stored result."""
    bug = any(f["rule_id"] and f["rule_classification"] == "bug" for f in findings)
    return derive_verdict(result["outcome"], bug)


def owed(result: Row, verdict: str, *, live: bool, due: Set[str]) -> bool:
    """A live discrepancy still provisional after an outcome the spec rechecks (``due``): owed a recheck."""
    provisional = verdict == Verdict.DIFFERENT and result["confirmation"] == Confirmation.PROVISIONAL.value
    return live and provisional and bool({result["outcome"], result["first_outcome"]} & due)


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return UNAVAILABLE
    value = (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001"), ROUND_HALF_EVEN)
    return format(value.normalize(), "f")


def _zeroed(names: Iterable[str], counts: Counter[str]) -> dict[str, int]:
    return {name: counts[name] for name in names}


class Tally:
    """A cycle's counters, tallied one result at a time (``add``): whoever reads the results a page at a
    time holds a page, never the cycle. ``counters`` then gives what ``count_cycle`` gives of them all."""

    def __init__(self, *, live: bool, recheck_outcomes: Iterable[Outcome]) -> None:
        self._live, self._due = live, {o.value for o in recheck_outcomes}
        self._results = self._never_reread = self._normalization_only = self._schema_drift = 0
        self._buckets, self._rules = Counter[str](), Counter[str]()
        self._outcomes, self._reasons = Counter[str](), Counter[str]()
        self._drifts, self._confirmations = Counter[str](), Counter[str]()

    def add(self, r: Row, findings: Sequence[Row]) -> Verdict:
        """Count a result with its ``findings``; its verdict."""
        v = verdict(r, findings)
        provisional = r["confirmation"] == Confirmation.PROVISIONAL.value
        outstanding = owed(r, v, live=self._live, due=self._due)
        self._results += 1
        self._buckets["outstanding" if outstanding else v.value] += 1
        self._never_reread += v is Verdict.DIFFERENT and provisional and not outstanding
        self._rules.update({f["rule_id"] for f in findings if f["rule_id"]})
        self._outcomes[r["outcome"]] += 1
        self._reasons[r["inconclusive_reason"]] += 1
        self._drifts[r["drift_reason"]] += 1
        self._confirmations[r["confirmation"]] += 1
        self._normalization_only += any(f["taxonomy_code"] == "NORMALIZATION_ONLY" for f in findings)
        self._schema_drift += r["inconclusive_reason"] == "schema_drift" or any(
            f["taxonomy_code"] == "SCHEMA_DRIFT" for f in findings
        )
        return v

    def counters(self, *, total: int, bulk_match: int = 0) -> dict[str, Any]:
        """The counters of the results added: ``total`` keys in the cycle, the ``bulk_match`` keys a
        table scan found identical included.

        Raises ``ReconciliationError`` naming every identity that does not hold.
        """
        buckets = self._buckets
        c: dict[str, Any] = {
            "total": total,
            "match": buckets[Verdict.MATCH.value] + bulk_match,
            "different": buckets[Verdict.DIFFERENT.value],
            "different_provisional": self._never_reread,
            "indeterminate": buckets[Verdict.INDETERMINATE.value],
            "cancelled": total - self._results - bulk_match,
            "outstanding": buckets["outstanding"],
            "by_outcome": _zeroed((o.value for o in Outcome), self._outcomes + Counter(MATCH=bulk_match)),
            "by_inconclusive_reason": _zeroed(INCONCLUSIVE_PRECEDENCE, self._reasons),
            "by_drift_reason": _zeroed((d.value for d in DriftReason), self._drifts),
            "by_confirmation": _zeroed(
                (x.value for x in Confirmation), self._confirmations + Counter(bulk_exhaustive=bulk_match)
            ),
            "suppressed_by_rule": dict(sorted(self._rules.items())),
            "normalization_only_keys": self._normalization_only,
            "schema_drift": self._schema_drift,
        }
        return _reconciled(c)


def count_cycle(
    results: Sequence[Row],
    findings: Mapping[str, Sequence[Row]],
    *,
    total: int,
    live: bool,
    recheck_outcomes: Iterable[Outcome],
    bulk_match: int = 0,
) -> dict[str, Any]:
    """Counters of one cycle: ``findings`` by masked key; ``total`` keys in the cycle, the
    ``bulk_match`` keys a table scan found identical included.

    Raises ``ReconciliationError`` naming every identity that does not hold.
    """
    tally = Tally(live=live, recheck_outcomes=recheck_outcomes)
    for r in results:
        tally.add(r, findings.get(r["key_masked"], ()))
    return tally.counters(total=total, bulk_match=bulk_match)


def count_groups(groups: Iterable[Row], *, live: bool, recheck_outcomes: Iterable[Outcome]) -> dict[str, Any]:
    """``count_cycle`` over grouped rows (``repo.stratum_counts``: ``n`` keys sharing a result's outcome,
    first outcome, confirmation, reasons, truncated sides and ``bug`` flag; a NULL outcome, keys without a
    result), which ``total`` counts; without the counts only findings give (by rule, normalisation only,
    schema drift). ``api_uncomparable`` counts the INCONCLUSIVE keys truncated on a side, whatever their
    reason: a recheck cannot lift an API cap (docs/mvp/SAMPLING.md D13).

    Raises ``ReconciliationError`` naming every identity that does not hold.
    """
    due = {o.value for o in recheck_outcomes}
    buckets: Counter[str] = Counter()
    outcomes, reasons, drifts, confirmations = Counter[str](), Counter[str](), Counter[str](), Counter[str]()
    total = never_reread = truncated = 0
    for g in groups:
        n, total = g["n"], total + g["n"]
        if g["outcome"] is None:
            buckets["cancelled"] += n
            continue
        v = derive_verdict(g["outcome"], g["bug"])
        outstanding = owed(g, v, live=live, due=due)
        buckets["outstanding" if outstanding else v.value] += n
        provisional = g["confirmation"] == Confirmation.PROVISIONAL.value
        never_reread += n * (v is Verdict.DIFFERENT and provisional and not outstanding)
        truncated += n * bool(g["truncated"])
        outcomes[g["outcome"]] += n
        reasons[g["inconclusive_reason"]] += n
        drifts[g["drift_reason"]] += n
        confirmations[g["confirmation"]] += n
    c: dict[str, Any] = {
        "total": total,
        "match": buckets[Verdict.MATCH.value],
        "different": buckets[Verdict.DIFFERENT.value],
        "different_provisional": never_reread,
        "indeterminate": buckets[Verdict.INDETERMINATE.value],
        "cancelled": buckets["cancelled"],
        "outstanding": buckets["outstanding"],
        "by_outcome": _zeroed((o.value for o in Outcome), outcomes),
        "by_inconclusive_reason": _zeroed(INCONCLUSIVE_PRECEDENCE, reasons),
        "by_drift_reason": _zeroed((d.value for d in DriftReason), drifts),
        "by_confirmation": _zeroed((x.value for x in Confirmation), confirmations),
    }
    return _reconciled(c) | {"api_uncomparable": truncated}


def _reconciled(c: dict[str, Any]) -> dict[str, Any]:
    """The counters with the sums and ratios derived from them, their identities checked."""
    o, total = c["by_outcome"], c["total"]
    c |= {
        "compared": c["match"] + c["different"],
        "suppressed": o["KNOWN_DIFF"] + o["NOISE_ONLY"],
        "absent_both": c["by_inconclusive_reason"]["absent_both"],
        "api_uncomparable": c["by_inconclusive_reason"]["truncated"],
    }
    c |= {"mismatch_rate": _ratio(c["different"], c["compared"]), "coverage": _ratio(c["compared"], total)}
    c["reconciliation"] = [
        {"identity": name, "left": left, "right": right, "holds": left == right}
        for name, left, right in _identities(c)
    ]
    broken = [f"{i['identity']}: {i['left']} != {i['right']}" for i in c["reconciliation"] if not i["holds"]]
    if broken:
        raise ReconciliationError("cycle counters do not reconcile: " + "; ".join(broken))
    return c


def _identities(c: Mapping[str, Any]) -> list[tuple[str, int, int]]:
    o, evaluated = c["by_outcome"], c["total"] - c["cancelled"]
    parts = ("match", "different", "indeterminate", "cancelled", "outstanding")
    return [
        (
            "total = match + different + indeterminate + cancelled + outstanding",
            c["total"],
            sum(c[p] for p in parts),
        ),
        ("keys by outcome = total - cancelled", sum(o.values()), evaluated),
        ("keys by confirmation = total - cancelled", sum(c["by_confirmation"].values()), evaluated),
        ("indeterminate = INCONCLUSIVE + DRIFT", c["indeterminate"], o["INCONCLUSIVE"] + o["DRIFT"]),
        (
            "INCONCLUSIVE = keys by inconclusive reason",
            o["INCONCLUSIVE"],
            sum(c["by_inconclusive_reason"].values()),
        ),
        ("DRIFT = keys by drift reason", o["DRIFT"], sum(c["by_drift_reason"].values())),
        (
            "match + different + outstanding = comparable outcomes",
            c["match"] + c["different"] + c["outstanding"],
            sum(o[name] for name in _COMPARABLE),
        ),
    ]
