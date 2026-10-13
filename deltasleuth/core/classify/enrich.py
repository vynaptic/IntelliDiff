"""Enrich differ findings (§8.2): taxonomy, severity, value class, signature and known-diff rule.

Code and severity come from a preset code, else the array rule of a one-sided member, else the
field rule of a value change, else the core default; the pack's aliases apply last. Known-diff
rules (CEL) are tried ``bug`` first, then by rule_id, and the first match suppresses the finding.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from functools import lru_cache
from typing import Any

from cel_expr_python import cel

from deltasleuth.core.classify.patterns import WILD, segments
from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.classify.taxonomy import DEFAULT_CODES, DomainPack, TaxonomyError, lookup, resolve_pack
from deltasleuth.core.classify.value_class import value_class
from deltasleuth.core.jsonio import cel_data
from deltasleuth.core.spec.expressions import cel_environment
from deltasleuth.core.types import (
    ChangeType,
    CompiledSpec,
    Finding,
    KnownDiffRule,
    RuleClassification,
    Severity,
)
from deltasleuth.security.tokenise import Tokeniser, mask_path_pattern, mask_pointer

_STATUS = re.compile(r"[A-Z][A-Z_]*")


def enrich(
    findings: list[Finding],
    spec: CompiledSpec,
    *,
    today: date,
    tokeniser: Tokeniser | None = None,
    rule_errors: list[dict[str, str]] | None = None,
) -> list[Finding]:
    """Classified copies of ``findings``, each with the known-diff rule that matches it, if any.

    ``tokeniser`` masks the CEL ``pointer`` variable (without it, protected segments show as a
    non-token marker). Failed rule evaluations are appended to ``rule_errors`` without values.
    """
    pack = resolve_pack(spec)
    rules = sorted(
        (r for r in spec.known_diffs if r.expires is None or date.fromisoformat(r.expires) >= today),
        key=lambda r: (r.classification is not RuleClassification.BUG, r.rule_id),
    )
    errors = [] if rule_errors is None else rule_errors
    out = []
    for f in findings:
        g = classify(f, spec, pack)
        rule = _first_match(rules, g, spec, tokeniser, errors) if rules else None
        # Set or cleared: re-enriching with today's rules drops a rule that no longer applies.
        g.rule_id = None if rule is None else rule.rule_id
        g.rule_classification = None if rule is None else rule.classification
        out.append(g)
    return out


def classify(f: Finding, spec: CompiledSpec, pack: DomainPack | None) -> Finding:
    """A copy of ``f`` with taxonomy code, severity, value class and signature set."""
    code, severity = (f.taxonomy_code, f.severity) if f.taxonomy_code is not None else _rule_code(f, spec)
    code = code or DEFAULT_CODES[f.change_type]
    row = lookup(code, pack)
    if not row.allows(f.change_type):
        where = mask_path_pattern(f.path_pattern, spec)
        raise TaxonomyError(
            f"taxonomy code {code} (presence {row.presence}) cannot classify a {f.change_type.value} "
            f"finding at {where}"
        )
    out = replace(
        f,
        taxonomy_code=pack.aliases.get(code, code) if pack else code,
        severity=severity or row.default_severity,
        value_class=f.value_class or value_class(f.a_raw, f.b_raw, f.a_value, f.b_value),
    )
    out.signature_hash = signature_hash(out, spec)
    return out


def _rule_code(f: Finding, spec: CompiledSpec) -> tuple[str | None, Severity | None]:
    """(code, severity) of the array or field rule that claims ``f``; (None, None) if none does.

    Field rules cover value changes only: a rule that down-rates noise never hides a vanished or
    retyped field.
    """
    segs = segments(f.path_pattern)
    if f.change_type in (ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B) and segs[-1:] == (WILD,):
        for r in spec.compare.arrays:
            if segments(r.path) == segs[:-1]:
                missing = f.change_type is ChangeType.MISSING_IN_B
                return (r.taxonomy_missing_in_b if missing else r.taxonomy_extra_in_b), r.severity
    if f.change_type is ChangeType.VALUE_CHANGED:
        for r in spec.compare.fields:
            if segments(r.path) == segs:
                return r.taxonomy_code, r.severity
    return None, None


@lru_cache(maxsize=4096)
def _compiled(when: str) -> cel.Expression:
    return cel_environment("known_diff").compile(when)  # the loader compiled it already, in this environment


def _first_match(
    rules: list[KnownDiffRule],
    f: Finding,
    spec: CompiledSpec,
    tokeniser: Tokeniser | None,
    errors: list[dict[str, str]],
) -> KnownDiffRule | None:
    # bug rules first: a defect being fixed is never hidden behind an accepted rule that matches too
    brackets = cel_data(_variables(f, mask_pointer(f.pointer_parts, spec, tokeniser, key=f.key)))
    for rule in rules:
        result, *others = (_compiled(rule.when).eval(data=data) for data in brackets)
        value = result.plain_value()
        if result.type() == cel.Type.ERROR:
            code = _status_code(value)
        elif not isinstance(value, bool):
            code = "non_boolean_result"
        elif any(other.plain_value() is not value for other in others):
            code = "inexact_number"  # the result depends on digits a double drops (cel_data)
        elif value:
            return rule
        else:
            continue
        errors.append(
            {
                "rule_id": rule.rule_id,
                "path_pattern": mask_path_pattern(f.path_pattern, spec),
                "change_type": f.change_type.value,
                "code": code,
            }
        )
    return None


def _status_code(message: object) -> str:
    """The CEL status code (INVALID_ARGUMENT, ...) heading an error; the rest may quote payload keys."""
    head = str(message).split(":", 1)[0].strip()
    return head if _STATUS.fullmatch(head) else "EVAL_ERROR"


def _variables(f: Finding, pointer: str) -> dict[str, Any]:
    """Every CEL variable the loader declares for ``known_diff``, so no rule meets an unbound
    name. ``a`` and ``b`` are the normalised values; ``path`` is the engine's pattern."""
    return {
        "key": f.key,
        "stratum": None,  # strata, link reasons and hop counts are not computed
        "strata": {},
        "path": f.path_pattern,
        "pointer": pointer,
        "change_type": f.change_type.value,
        "direction": f.direction.value,
        "a": f.a_value,
        "b": f.b_value,
        "value_class": f.value_class,
        "taxonomy_code": f.taxonomy_code,
        "severity": None if f.severity is None else f.severity.value,
        "link_reason": None,
        "hop_count": None,
        "attrs": {},
    }
