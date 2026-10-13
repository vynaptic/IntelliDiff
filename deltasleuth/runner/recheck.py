"""The recheck policy (spec ``recheck``) and the confirm-on-mismatch rules.

A provisional result is re-read, B first, and ``confirm`` judges it against the first read:

* a re-read with an unusable side never confirms: the first read stays provisional for the next
  recheck, and once the rechecks (``times``) are spent it is INCONCLUSIVE(recheck_failed);
* a first read with an unusable side leaves nothing to compare with: the re-read is the first
  comparable read, final when it agrees, still provisional when it finds a discrepancy;
* A changed (state, hash of its normalised canonical document, or filter membership):
  DRIFT(a_changed);
* B changed: DRIFT(b_converged) when nothing discrepant is left, else the re-read's outcome with
  its discrepant findings tagged ``intermittent_b``;
* nothing changed: the first outcome is confirmed.

Fixed files are read once: a recheck reads no side of them again, so none is due
where their bytes alone would answer it, and a result both of whose sides read them is final as read
(``freeze``). A read that failed read no bytes: it is read again.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.spec import parse_duration
from deltasleuth.core.types import (
    CompiledSpec,
    Confirmation,
    DriftReason,
    Finding,
    KeyResult,
    Outcome,
    Side,
    SideState,
    SideStateKind,
    Verdict,
)

DEFAULT_OUTCOMES = frozenset(
    {Outcome.MISMATCH, Outcome.NOT_FOUND_A, Outcome.NOT_FOUND_B, Outcome.INCONCLUSIVE}
)
INTERMITTENT_B = "intermittent_b"
_UNUSABLE = (SideStateKind.INCOMPLETE, SideStateKind.ERROR)


@dataclass(frozen=True)
class RecheckPolicy:
    outcomes: frozenset[Outcome]
    delay_s: float
    jitter_s: float
    times: int
    one_shot_delay_s: float
    one_shot_times: int

    @classmethod
    def from_spec(cls, raw: Mapping[str, Any]) -> RecheckPolicy:
        entry = raw["recheck"]  # required by the schema
        one_shot = entry.get("oneShot", {})
        listed = entry.get("outcomes")
        return cls(
            outcomes=DEFAULT_OUTCOMES if listed is None else frozenset(map(Outcome, listed)),
            delay_s=parse_duration(entry["delay"]),
            jitter_s=parse_duration(entry.get("jitter", "0s")),
            times=entry["times"],
            one_shot_delay_s=parse_duration(one_shot.get("delay", "2s")),
            one_shot_times=one_shot.get("times", 1),
        )

    def due(self, result: KeyResult, fixed: Collection[Side] = ()) -> bool:
        """A provisional result the policy re-reads; a key a side cannot read (skipped) has nothing
        to re-read, nor has one whose every side read fixed files (``fixed``: the sides that read them),
        or whose unusable side did (a line that was not a row, a key several rows hold): those bytes
        cannot answer differently."""
        read = _read(result, fixed)
        return (
            result.confirmation is Confirmation.PROVISIONAL
            and result.outcome in self.outcomes
            and "skipped" not in (result.side_a.reason, result.side_b.reason)
            and len(read) < 2
            and not any(state.kind is SideStateKind.INCOMPLETE for state in read)
        )


def _read(result: KeyResult, fixed: Collection[Side]) -> list[SideState]:
    """The states of the result's sides that read fixed files; a read that failed read none."""
    states = {Side.A: result.side_a, Side.B: result.side_b}
    return [states[side] for side in fixed if states[side].kind is not SideStateKind.ERROR]


def freeze(result: KeyResult, fixed: Collection[Side]) -> KeyResult:
    """``result``, final as read when both of its sides read fixed files: reading them again is a no-op, so
    its confirmation is ``frozen_cut``."""
    if len(_read(result, fixed)) == 2:
        result.confirmation = Confirmation.FROZEN_CUT
    return result


def _discrepant(f: Finding) -> bool:
    return not f.suppressed and f.severity is not None and f.severity.rank > 0


def _usable(result: KeyResult) -> bool:
    return result.side_a.kind not in _UNUSABLE and result.side_b.kind not in _UNUSABLE


def _fingerprint(state: SideState) -> tuple[SideStateKind, str | None, bool]:
    """A side's read as a recheck compares it; a row entering or leaving its filter changed."""
    return state.kind, state.canonical.hash if state.canonical is not None else None, state.selected


def tag_intermittent(findings: list[Finding], spec: CompiledSpec) -> list[Finding]:
    """Discrepant findings re-classed ``intermittent_b`` and re-signed as enrich signs them."""
    tagged = []
    for f in findings:
        if _discrepant(f):
            f = dataclasses.replace(f, value_class=INTERMITTENT_B)
            f.signature_hash = signature_hash(f, spec)
        tagged.append(f)
    return tagged


def _judged(first: KeyResult, base: KeyResult, **changes: Any) -> KeyResult:
    fields = {"confirmation": Confirmation.RECHECKED, "first_outcome": first.outcome, **changes}
    return dataclasses.replace(base, revision=first.revision + 1, **fields)


def _bare(first: KeyResult, recheck: KeyResult, outcome: Outcome, **reason: Any) -> KeyResult:
    """A judgement that is not a comparison outcome: no findings, only its reason."""
    fields = {"inconclusive_reason": None, "drift_reason": None, **reason}
    return _judged(first, recheck, outcome=outcome, findings=[], suppressed_count=0, **fields)


def confirm(first: KeyResult, recheck: KeyResult, *, spec: CompiledSpec, last: bool = True) -> KeyResult:
    """Judge ``first`` (provisional) with ``recheck``, the same key read again (see the module doc);
    ``last``: no recheck is left after this one."""
    if not _usable(recheck):
        if not last:
            return first
        return _bare(first, recheck, Outcome.INCONCLUSIVE, inconclusive_reason="recheck_failed")
    if not _usable(first):
        # One comparable read cannot confirm a discrepancy: it stays owed a recheck.
        owed = recheck.verdict is Verdict.DIFFERENT
        return _judged(
            first, recheck, confirmation=Confirmation.PROVISIONAL if owed else Confirmation.RECHECKED
        )
    if _fingerprint(recheck.side_a) != _fingerprint(first.side_a):
        return _bare(first, recheck, Outcome.DRIFT, drift_reason=DriftReason.A_CHANGED)
    if _fingerprint(recheck.side_b) != _fingerprint(first.side_b):
        if not any(_discrepant(f) for f in recheck.findings):
            return _bare(first, recheck, Outcome.DRIFT, drift_reason=DriftReason.B_CONVERGED)
        return _judged(first, recheck, findings=tag_intermittent(recheck.findings, spec))
    return _judged(first, first)
