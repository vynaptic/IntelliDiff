"""Golden corpus: observe + canonicalise + diff on hand-checked paired responses."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.diff.differ import diff
from deltasleuth.core.types import Finding, Side, SideStateKind

CASES = sorted((Path(__file__).parent / "cases").glob("*.yaml"))
_EXTRA_ATTRIBUTES = ("value_class", "member_key", "array_path", "direction", "raw_only", "severity")


def test_corpus_size() -> None:
    assert len(CASES) >= 40


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_golden_case(path: Path, load) -> None:
    case = load(path)
    states = {
        side: observe(case.observations[side], case.spec.sides[side], case.spec, case.capabilities[side])
        for side in Side
    }
    for side in Side:
        got = (states[side].kind.value, states[side].reason)
        assert got == case.expect_states[side], f"side {side.value}: {states[side].detail}"

    both_complete = all(states[s].kind is SideStateKind.COMPLETE for s in Side)
    if not both_complete:
        assert not case.expect_findings, "findings are only defined when both sides are COMPLETE"
        return

    findings = diff(states[Side.A].canonical, states[Side.B].canonical, case.spec, key=case.key)
    got = Counter(_row(f) for f in findings)
    want = Counter(item["finding"] for item in case.expect_findings)
    assert got == want, _explain(got, want)
    for item in case.expect_findings:
        extras = {k: v for k, v in item.items() if k in _EXTRA_ATTRIBUTES}
        if extras:
            finding = next(f for f in findings if _row(f) == item["finding"])
            actual = {k: _plain(getattr(finding, k)) for k in extras}
            assert actual == extras, f"{item['finding']}"

    again = diff(states[Side.A].canonical, states[Side.B].canonical, case.spec, key=case.key)
    assert [_row(f) for f in again] == [_row(f) for f in findings], "diff must be deterministic"


def _row(f: Finding) -> tuple:
    return (f.json_pointer, f.path_pattern, f.change_type.value, f.taxonomy_code)


def _plain(value: object) -> object:
    return getattr(value, "value", value)


def _explain(got: Counter, want: Counter) -> str:
    return f"unexpected: {sorted((got - want).elements())}\nmissing: {sorted((want - got).elements())}"
