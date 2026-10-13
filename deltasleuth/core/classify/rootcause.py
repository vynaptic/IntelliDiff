"""Root causes: a run's discrepant findings grouped into probable causes, deterministically.

The same member of a keyed/set array missing (or extra) under two or more keys is one cause;
every other finding groups by signature. Causes are published masked, and their ids
(``RC-<12 hex>``) hash masked parts only, so re-runs agree and no id hashes a raw value.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.types import (
    ChangeType,
    CompiledSpec,
    Direction,
    Finding,
    KeyResult,
    RootCause,
    RuleClassification,
)
from deltasleuth.security.tokenise import Tokeniser, mask_key, mask_member_key, mask_path_pattern

SAMPLE_KEYS = 5
MIN_FANOUT_KEYS = 2

Group = tuple[RootCause, list[Finding]]


def root_causes(results: Iterable[KeyResult], *, spec: CompiledSpec, tokeniser: Tokeniser) -> list[RootCause]:
    """Root causes of a run: fan-out groups first, then by severity and reach."""
    return [rc for rc, _ in groups(results, spec=spec, tokeniser=tokeniser)]


def _bug_rule(f: Finding) -> str | None:
    return f.rule_id if f.rule_classification is RuleClassification.BUG else None


def groups(results: Iterable[KeyResult], *, spec: CompiledSpec, tokeniser: Tokeniser) -> list[Group]:
    """``root_causes``, each with its findings."""
    # A bug rule's finding is still discrepant: grouped apart, under its rule.
    discrepant = [
        f
        for r in results
        for f in r.findings
        if f.severity.rank > 0 and (not f.suppressed or _bug_rule(f) is not None)
    ]
    fanout: dict[tuple[tuple[str, ...], str | None], list[Finding]] = defaultdict(list)
    for f in discrepant:
        one_sided = f.change_type in (ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B)
        # array_path excludes the document-level finding, whose member key is the request key
        if one_sided and f.array_path is not None and f.member_key is not None:
            member = mask_member_key(f.member_key, f.array_path, spec, tokeniser, key=f.key)
            pattern = mask_path_pattern(f.path_pattern, spec)
            ident = (mask_path_pattern(f.array_path, spec), pattern, member, f.change_type.value)
            fanout[(ident, _bug_rule(f))].append(f)
    causes = [
        _cause("member_key", ident, fs, spec, tokeniser, member=ident[2])
        for (ident, _), fs in fanout.items()
        if len({f.key for f in fs}) >= MIN_FANOUT_KEYS
    ]
    claimed = {id(f) for _, fs in causes for f in fs}
    by_signature: dict[tuple[str, str | None], list[Finding]] = defaultdict(list)
    for f in discrepant:
        if id(f) not in claimed:
            by_signature[(signature_hash(f, spec), _bug_rule(f))].append(f)
    causes += [_cause("signature", (sig,), fs, spec, tokeniser) for (sig, _), fs in by_signature.items()]
    return sorted(causes, key=_rank)


def _cause(
    kind: Literal["member_key", "signature"],
    ident: tuple[str, ...],
    fs: list[Finding],
    spec: CompiledSpec,
    tok: Tokeniser,
    member: str | None = None,
) -> Group:
    bug = _bug_rule(fs[0])
    # The rule is part of the identity of bug-suppressed groups only, so an unsuppressed group
    # keeps its id when a bug rule suppresses the same difference elsewhere.
    parts = (kind, *ident, *(("bug_rule", bug) if bug is not None else ()))
    rc = RootCause(
        root_cause_id="RC-" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12],
        kind=kind,
        path_pattern=mask_path_pattern(fs[0].path_pattern, spec),
        change_type=fs[0].change_type,
        direction=_uniform(f.direction for f in fs) or Direction.BOTH,
        affected_keys=len({f.key for f in fs}),
        finding_count=len(fs),
        max_severity=max((f.severity for f in fs), key=lambda s: s.rank),
        member_key_tok=member,
        signature_hash=_uniform(signature_hash(f, spec) for f in fs),
        value_class=_uniform(f.value_class for f in fs),
        taxonomy_code=_uniform(f.taxonomy_code for f in fs),
        sample_keys=tuple(sorted({mask_key(f.key, spec, tok) for f in fs})[:SAMPLE_KEYS]),
    )
    return rc, fs


def _uniform[T](values: Iterable[T]) -> T | None:
    """The one value all of ``values`` share, else None."""
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else None


def _rank(group: Group) -> tuple[bool, int, int, int, str]:
    rc = group[0]
    return (
        rc.kind != "member_key",
        -rc.max_severity.rank,
        -rc.affected_keys,
        -rc.finding_count,
        rc.root_cause_id,
    )
