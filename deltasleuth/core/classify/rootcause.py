"""Root causes: a run's discrepant findings grouped into probable causes, deterministically.

The same member of a keyed/set array missing (or extra) under two or more keys is one cause;
every other finding groups by signature. Causes are published masked, and their ids
(``RC-<12 hex>``) hash masked parts only, so re-runs agree and no id hashes a raw value.

Grouping reads each discrepant finding as a ``Member``: the few facts it groups by, without values or
documents, so a cycle's end can derive its keys again a page at a time and keep only these.
"""

from __future__ import annotations

import hashlib
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.masking import KeyForm, Tokeniser, mask_key, mask_member_key, mask_path_pattern
from deltasleuth.core.types import (
    ChangeType,
    CompiledSpec,
    Direction,
    Finding,
    KeyResult,
    RootCause,
    RuleClassification,
    Severity,
)

SAMPLE_KEYS = 5
MIN_FANOUT_KEYS = 2

Group = tuple[RootCause, list[Finding]]


@dataclass(frozen=True, slots=True)
class Member:
    """One discrepant finding as grouping reads it. ``key`` (the request key) only tells keys apart;
    everything published is masked. ``fanout``: the masked array pattern, path pattern, member key and
    change type of a member one side lacks. ``ref``: what the caller knows the finding by."""

    key: str
    key_masked: str
    fanout: tuple[str, str, str, str] | None
    signature: str
    bug_rule: str | None
    path_pattern: str
    change_type: ChangeType
    direction: Direction
    severity: Severity
    value_class: str | None
    taxonomy_code: str | None
    ref: Any = None


def root_causes(
    results: Iterable[KeyResult], *, spec: CompiledSpec, tokeniser: Tokeniser, form: KeyForm
) -> list[RootCause]:
    """Root causes of a run whose keys are masked in ``form``: fan-out groups first, then by severity and
    reach."""
    return [rc for rc, _ in groups(results, spec=spec, tokeniser=tokeniser, form=form)]


def _bug_rule(f: Finding) -> str | None:
    return f.rule_id if f.rule_classification is RuleClassification.BUG else None


def groups(
    results: Iterable[KeyResult], *, spec: CompiledSpec, tokeniser: Tokeniser, form: KeyForm
) -> list[Group]:
    """``root_causes``, each with its findings."""
    found = [m for r in results for m in members(r, spec=spec, tokeniser=tokeniser, form=form)]
    return [(rc, [m.ref for m in ms]) for rc, ms in group_members(found)]


def members(
    result: KeyResult,
    *,
    spec: CompiledSpec,
    tokeniser: Tokeniser,
    form: KeyForm,
    ref: Callable[[Finding], Any] = lambda f: f,
) -> list[Member]:
    """The result's discrepant findings as members, in finding order, each key masked in the run's ``form``
    (``masking.key_form``); each member's ``ref`` is ``ref`` of its finding (by default the finding). A bug
    rule's finding is still discrepant: grouped apart, under its rule. Shared texts (patterns, signatures)
    are interned: a cycle repeats them across keys."""
    masked: dict[str, str] = {}
    found = []
    for f in result.findings:
        bug = _bug_rule(f)
        if f.classified_severity.rank <= 0 or (f.suppressed and bug is None):
            continue
        if f.key not in masked:
            masked[f.key] = mask_key(f.key, spec, tokeniser, form=form)
        pattern = sys.intern(mask_path_pattern(f.path_pattern, spec))
        fanout = None
        one_sided = f.change_type in (ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B)
        # array_path excludes the document-level finding, whose member key is the request key
        if one_sided and f.array_path is not None and f.member_key is not None:
            member = mask_member_key(f.member_key, f.array_path, spec, tokeniser, key=f.key)
            array = sys.intern(mask_path_pattern(f.array_path, spec))
            fanout = (array, pattern, sys.intern(member), f.change_type.value)
        found.append(
            Member(
                key=f.key,
                key_masked=masked[f.key],
                fanout=fanout,
                signature=sys.intern(signature_hash(f, spec)),
                bug_rule=bug,
                path_pattern=pattern,
                change_type=f.change_type,
                direction=f.direction,
                severity=f.classified_severity,
                value_class=f.value_class,
                taxonomy_code=f.taxonomy_code,
                ref=ref(f),
            )
        )
    return found


def group_members(found: Sequence[Member]) -> list[tuple[RootCause, list[Member]]]:
    """The causes of discrepant findings read as members, each with its members, in rank order."""
    fanout: dict[tuple[tuple[str, ...], str | None], list[Member]] = defaultdict(list)
    for m in found:
        if m.fanout is not None:
            fanout[(m.fanout, m.bug_rule)].append(m)
    claimed = {group for group, ms in fanout.items() if len({m.key for m in ms}) >= MIN_FANOUT_KEYS}
    causes = [
        _cause("member_key", group[0], ms, member=group[0][2])
        for group, ms in fanout.items()
        if group in claimed
    ]
    by_signature: dict[tuple[str, str | None], list[Member]] = defaultdict(list)
    for m in found:
        if m.fanout is None or (m.fanout, m.bug_rule) not in claimed:
            by_signature[(m.signature, m.bug_rule)].append(m)
    causes += [_cause("signature", (sig,), ms) for (sig, _), ms in by_signature.items()]
    return sorted(causes, key=_rank)


def _cause(
    kind: Literal["member_key", "signature"],
    ident: tuple[str, ...],
    ms: list[Member],
    member: str | None = None,
) -> tuple[RootCause, list[Member]]:
    first = ms[0]
    # The rule is part of the identity of bug-suppressed groups only, so an unsuppressed group
    # keeps its id when a bug rule suppresses the same difference elsewhere.
    parts = (kind, *ident, *(("bug_rule", first.bug_rule) if first.bug_rule is not None else ()))
    rc = RootCause(
        root_cause_id="RC-" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12],
        kind=kind,
        path_pattern=first.path_pattern,
        change_type=first.change_type,
        direction=_uniform(m.direction for m in ms) or Direction.BOTH,
        affected_keys=len({m.key for m in ms}),
        finding_count=len(ms),
        max_severity=max((m.severity for m in ms), key=lambda s: s.rank),
        member_key_tok=member,
        signature_hash=_uniform(m.signature for m in ms),
        value_class=_uniform(m.value_class for m in ms),
        taxonomy_code=_uniform(m.taxonomy_code for m in ms),
        sample_keys=tuple(sorted({m.key_masked for m in ms})[:SAMPLE_KEYS]),
    )
    return rc, ms


def _uniform[T](values: Iterable[T]) -> T | None:
    """The one value all of ``values`` share, else None."""
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else None


def _rank(group: tuple[RootCause, list[Any]]) -> tuple[bool, int, int, int, str]:
    rc = group[0]
    return (
        rc.kind != "member_key",
        -rc.max_severity.rank,
        -rc.affected_keys,
        -rc.finding_count,
        rc.root_cause_id,
    )
