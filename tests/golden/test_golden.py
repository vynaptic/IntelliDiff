"""Golden corpus: observe + canonicalise + diff on hand-checked paired responses, and, where a case
states the outcome, the per-key comparison that classifies the findings and judges the key."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.classify.taxonomy import CORE_CODES
from deltasleuth.core.diff.differ import diff
from deltasleuth.core.types import Finding, KeyResult, Outcome, Side, SideState, SideStateKind

from .conftest import DIFFER_ATTRIBUTES, REPO_ROOT, TODAY, GoldenCase

CASES = sorted((Path(__file__).parent / "cases").glob("*.yaml"))

# Registered codes that no comparison of the engine's own emits; only a spec's array or field rule that
# names one would. Graph mode's node and edge codes, the existence double-check's INTERNAL_INCONSISTENCY
# and the service-side inferred-schema guard's SCHEMA_DRIFT belong to parts that are not built.
RESERVED_CODES = frozenset(
    {"MISSING_NODE", "EXTRA_NODE", "MISSING_EDGE", "EXTRA_EDGE", "INTERNAL_INCONSISTENCY", "SCHEMA_DRIFT"}
)
# NOISE_ONLY needs a noise mask, which the spec loader refuses as unsupported. DRIFT is a recheck's
# judgement of two reads of one key (tests/unit/classify/test_drift_from_reads.py).
RESERVED_OUTCOMES = frozenset({Outcome.NOISE_ONLY, Outcome.DRIFT})


def test_corpus_size() -> None:
    assert len(CASES) >= 40


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_golden_case(path: Path, load) -> None:
    case = load(path)
    states = _observe(case)
    for side in Side:
        got = (states[side].kind.value, states[side].reason)
        assert got == case.expect_states[side], f"side {side.value}: {states[side].detail}"

    both_complete = all(states[s].kind is SideStateKind.COMPLETE for s in Side)
    if both_complete:
        _check_differ(case, states)
    elif case.expect_outcome is None:
        assert not case.expect_findings, "findings without an outcome need both sides COMPLETE"
    if case.expect_outcome is not None:
        _check_outcome(case, states, both_complete)


def _check_differ(case: GoldenCase, states: dict[Side, SideState]) -> None:
    findings = diff(states[Side.A].canonical, states[Side.B].canonical, case.spec, key=case.key)
    got = Counter(_row(f) for f in findings)
    want = Counter(item["finding"] for item in case.expect_findings)
    assert got == want, _explain(got, want)
    for item in case.expect_findings:
        _check_attributes(item, next(f for f in findings if _row(f) == item["finding"]))

    again = diff(states[Side.A].canonical, states[Side.B].canonical, case.spec, key=case.key)
    assert [_row(f) for f in again] == [_row(f) for f in findings], "diff must be deterministic"


def _check_outcome(case: GoldenCase, states: dict[Side, SideState], both_complete: bool) -> None:
    result = _judge(case, states)
    assert (result.outcome.value, result.inconclusive_reason) == case.expect_outcome
    assert "known_diff_errors" not in result.attrs, result.attrs["known_diff_errors"]
    # Classification relabels a finding; it never adds, drops or moves one.
    got = Counter(_row(f)[:3] for f in result.findings)
    want = Counter(item["finding"][:3] for item in case.expect_findings)
    assert got == want, _explain(got, want)
    for item in case.expect_findings:
        finding = next(f for f in result.findings if _row(f)[:3] == item["finding"][:3])
        if not both_complete:  # the document finding of a one-sided key, which the differ never sees
            assert _row(finding) == item["finding"]
            _check_attributes(item, finding)
        classified = [finding.taxonomy_code, _plain(finding.severity)]
        assert classified == item["taxonomy"], f"{item['finding']}"
        assert finding.rule_id == item.get("rule"), f"{item['finding']}"
    assert result.suppressed_count == sum(1 for item in case.expect_findings if item.get("rule"))


def _check_attributes(item: dict, finding: Finding) -> None:
    extras = {k: v for k, v in item.items() if k in DIFFER_ATTRIBUTES}
    if extras:
        actual = {k: _plain(getattr(finding, k)) for k in extras}
        assert actual == extras, f"{item['finding']}"


def test_the_corpus_reaches_every_built_code_and_outcome_and_no_reserved_one(load) -> None:
    """Every core code and outcome the engine produces for one key appears in the corpus; the reserved
    ones never do (none of the cases names one in a rule)."""
    codes: set[str | None] = set()
    outcomes: set[Outcome] = set()
    for path in CASES:
        case = load(path)
        result = _judge(case, _observe(case))
        outcomes.add(result.outcome)
        codes.update(f.taxonomy_code for f in result.findings)
    assert RESERVED_CODES <= CORE_CODES.keys()
    assert codes == CORE_CODES.keys() - RESERVED_CODES
    assert outcomes == set(Outcome) - RESERVED_OUTCOMES


def test_no_module_emits_a_reserved_code() -> None:
    """Outside the registry, a reserved code appears in the package only where a reader counts it."""
    readers = {"core/classify/taxonomy.py": RESERVED_CODES, "runner/counters.py": {"SCHEMA_DRIFT"}}
    package = REPO_ROOT / "deltasleuth"
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        named = {code for code in RESERVED_CODES if f'"{code}"' in text or f"'{code}'" in text}
        assert named <= readers.get(path.relative_to(package).as_posix(), set()), path


def _observe(case: GoldenCase) -> dict[Side, SideState]:
    return {
        side: observe(case.observations[side], case.spec.sides[side], case.spec, case.capabilities[side])
        for side in Side
    }


def _judge(case: GoldenCase, states: dict[Side, SideState]) -> KeyResult:
    return compare_pair(case.key, states[Side.A], states[Side.B], case.spec, today=TODAY)


def _row(f: Finding) -> tuple:
    return (f.json_pointer, f.path_pattern, f.change_type.value, f.taxonomy_code)


def _plain(value: object) -> object:
    return getattr(value, "value", value)


def _explain(got: Counter, want: Counter) -> str:
    return f"unexpected: {sorted((got - want).elements())}\nmissing: {sorted((want - got).elements())}"
