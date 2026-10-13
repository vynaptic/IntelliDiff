"""Per-key outcome (§7.1): two side states in, one ``KeyResult`` out.

An unusable side makes the key INCONCLUSIVE, and so do two absent sides or two documents holding
no value (they hash equal, but nothing was compared). A one-sided key is NOT_FOUND unless a rule
explains its document-level finding; otherwise the findings above INFO decide.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from deltasleuth.core.classify.enrich import enrich
from deltasleuth.core.diff.differ import diff
from deltasleuth.core.types import (
    INCONCLUSIVE_PRECEDENCE,
    JSON,
    Canonical,
    ChangeType,
    CompiledSpec,
    Counted,
    Direction,
    Finding,
    KeyResult,
    Outcome,
    Severity,
    Side,
    SideState,
    SideStateKind,
)
from deltasleuth.security.tokenise import Tokeniser

Differ = Callable[..., list[Finding]]  # diff(a: Canonical, b: Canonical, spec, *, key) -> findings

NOTHING_COMPARED = "nothing selected on either side"
_ABSENT, _COMPLETE = SideStateKind.ABSENT, SideStateKind.COMPLETE


def compare_pair(
    key: str,
    side_a: SideState,
    side_b: SideState,
    spec: CompiledSpec,
    *,
    today: date,
    differ: Differ | None = None,
    tokeniser: Tokeniser | None = None,
) -> KeyResult:
    """Compare one key's two side states into a provisional result.

    ``differ`` defaults to the engine's; ``tokeniser`` masks the known-diff ``pointer`` variable.
    ``attrs`` records ``inconclusive_detail`` and failed rule evaluations (``known_diff_errors``).
    """
    attrs: dict[str, Any] = {}
    reason = _inconclusive_reason(side_a, side_b)
    if reason is None and _nothing_compared(side_a, side_b):
        reason, attrs["inconclusive_detail"] = "unparseable", NOTHING_COMPARED
    if reason is not None:
        return KeyResult(
            key,
            Outcome.INCONCLUSIVE,
            side_a,
            side_b,
            inconclusive_reason=reason,
            first_outcome=Outcome.INCONCLUSIVE,
            attrs=attrs,
        )
    if side_a.kind is _ABSENT:
        findings = [_document_finding(key, Side.B, side_b.canonical)]
    elif side_b.kind is _ABSENT:
        findings = [_document_finding(key, Side.A, side_a.canonical)]
    else:
        findings = list((differ or diff)(side_a.canonical, side_b.canonical, spec, key=key))
        if side_a.selected != side_b.selected:  # the key left one side's filtered population
            findings.append(_membership_finding(key, side_a.selected, side_b.selected))
    errors: list[dict[str, str]] = []
    findings = enrich(findings, spec, today=today, tokeniser=tokeniser, rule_errors=errors)
    if errors:
        attrs["known_diff_errors"] = errors
    outcome = _outcome(side_a, side_b, findings)
    return KeyResult(
        key,
        outcome,
        side_a,
        side_b,
        first_outcome=outcome,
        findings=findings,
        suppressed_count=sum(f.suppressed for f in findings),
        attrs=attrs,
    )


def _inconclusive_reason(a: SideState, b: SideState) -> str | None:
    if a.kind is SideStateKind.ERROR and b.kind is SideStateKind.ERROR:
        return "both_error"
    reasons = [s.reason for s in (a, b) if s.kind in (SideStateKind.INCOMPLETE, SideStateKind.ERROR)]
    if reasons:  # one ABSENT side does not rescue the other
        return min(reasons, key=INCONCLUSIVE_PRECEDENCE.index)
    return "absent_both" if a.kind is _ABSENT and b.kind is _ABSENT else None


def _nothing_compared(a: SideState, b: SideState) -> bool:
    """Both COMPLETE, and select, map or filters left neither document a value (a null root none)."""
    docs = [s.canonical.doc for s in (a, b) if s.kind is _COMPLETE]
    return len(docs) == 2 and not any(doc is not None and _holds_value(doc) for doc in docs)


def _holds_value(node: JSON) -> bool:
    """True when ``node`` is, or contains at any depth, a scalar (null included)."""
    if isinstance(node, Counted):
        return _holds_value(node.value)
    if isinstance(node, dict):  # a KeyedMap too: member key -> members
        return any(map(_holds_value, node.values()))
    if isinstance(node, list | tuple):
        return any(map(_holds_value, node))
    return True


def _document_finding(key: str, present: Side, canonical: Canonical) -> Finding:
    """The one root finding of a one-sided key. Its value is the present document, which
    rules may test (``a.status == 'CLOSED'``); its member key is the key itself."""
    in_a = present is Side.A
    return Finding(
        key=key,
        json_pointer="",
        path_pattern="$",
        change_type=ChangeType.MISSING_IN_B if in_a else ChangeType.EXTRA_IN_B,
        a_value=canonical.doc if in_a else None,
        b_value=None if in_a else canonical.doc,
        a_raw=canonical.raw_doc if in_a else None,
        b_raw=None if in_a else canonical.raw_doc,
        member_key=key,
        taxonomy_code="MISSING_DOCUMENT" if in_a else "EXTRA_DOCUMENT",
        severity=Severity.CRITICAL,
        direction=Direction.A_ONLY if in_a else Direction.B_ONLY,
    )


def _membership_finding(key: str, selected_a: bool, selected_b: bool) -> Finding:
    """One side's filter selects the key's row, the other's does not (TABLES.md §9)."""
    return Finding(
        key=key,
        json_pointer="",
        path_pattern="$",
        change_type=ChangeType.VALUE_CHANGED,
        a_value=selected_a,
        b_value=selected_b,
        a_raw=selected_a,
        b_raw=selected_b,
        taxonomy_code="FILTER_MEMBERSHIP",
    )


def _outcome(a: SideState, b: SideState, findings: list[Finding]) -> Outcome:
    discrepant = [f for f in findings if f.severity.rank > 0]
    if any(not f.suppressed for f in discrepant):
        outcome = Outcome.MISMATCH
    elif not discrepant:
        outcome = Outcome.MATCH
    elif any(f.rule_id is not None for f in discrepant):
        outcome = Outcome.KNOWN_DIFF
    else:
        outcome = Outcome.NOISE_ONLY
    if _ABSENT not in (a.kind, b.kind) or outcome is Outcome.KNOWN_DIFF:
        return outcome
    # Only a rule explains a one-sided key: masks describe value jitter, not a missing document.
    return Outcome.NOT_FOUND_A if a.kind is _ABSENT else Outcome.NOT_FOUND_B
