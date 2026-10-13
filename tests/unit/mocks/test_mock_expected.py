"""expected.json: well-formed, complete, and consistent with the mocks, the specs and the targets.

These tests do not run the engine. Instead a deliberately small
oracle re-derives, from each recorded reply and the declared contract, what §7.1 fixes without
any diffing: the side states, hence INCONCLUSIVE / NOT_FOUND outcomes and their reasons; and for
two complete sides, whether the mapped documents differ at all. The end-to-end tests
then check the findings themselves.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from deltasleuth.core.types import (
    ChangeType,
    DriftReason,
    Outcome,
    RuleClassification,
    Severity,
    Verdict,
)
from mocks.common import JSONBody, Reply, ScriptedDataset, Variant
from mocks.fixtures import recorded_reply
from mocks.registry import PAIRS

DDL = "docs/architecture/control-plane.sql"
# The INCONCLUSIVE reasons in §7.1 (docs/architecture.md) precedence order, copied as a literal on
# purpose: an oracle ordered by types.INCONCLUSIVE_PRECEDENCE would agree with any reordering of
# the tuple the engine itself uses.
SECTION_7_1_PRECEDENCE = (
    "both_error",
    "error_a",
    "error_b",
    "timeout",
    "rate_limited",
    "circuit_open",
    "truncated",
    "unstable_pagination",
    "unparseable",
    "resource_limit",
    "schema_drift",
    "absence_unconfirmed",
    "watermark_missing",
    "duplicate_member_name",
    "identity_ambiguous",
    "absent_both",
    "skipped",
    "recheck_failed",
)
MODES = ("file", "live")
MODE_FIELDS = {"outcome", "verdict", "inconclusive_reason", "drift_reason", "suppressed_count", "findings"}
LIVE_ONLY_FIELDS = {"rechecked", "first_outcome", "first_findings", "attempts"}
FINDING_FIELDS = {
    "taxonomy_code",
    "change_type",
    "path_pattern",
    "severity",
    "rule_id",
    "rule_classification",
    "count",
}
CONTRACT_SEVERITY = {
    "NORMALIZATION_ONLY": "INFO",
    "MISSING_DOCUMENT": "CRITICAL",
    "EXTRA_DOCUMENT": "CRITICAL",
}

Mapped = Callable[[str, Variant, JSONBody], Any]


@pytest.fixture(scope="module")
def core_codes(repo_root: Any) -> set[str]:
    """Core taxonomy codes, read from the DDL seed rows (the source of truth)."""
    text = (repo_root / DDL).read_text(encoding="utf-8")
    return set(re.findall(r"\('([A-Z][A-Z0-9_]*)',\s*'core'", text))


def _entries(expected: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(pair, key, entry) for pair in PAIRS for key, entry in expected[pair]["keys"].items()]


def _all_findings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for mode in MODES for f in entry[mode]["findings"] + entry[mode].get("first_findings", [])]


def _side_state(
    pair: str, variant: Variant, reply: Reply, spec: dict[str, Any], absence_shape: Callable[..., bool]
) -> tuple[str, str | None]:
    """(state, reason) per the §8.1 observe order, for what the mocks can produce."""
    side = spec["sides"][variant]
    declared = reply.status in side["notFound"]["status"]
    if declared or reply.status in (404, 410):
        if declared and absence_shape(pair, side["target"], reply.body):
            return "ABSENT", None
        return "INCOMPLETE", "absence_unconfirmed"
    if reply.status != 200:
        return "ERROR", f"error_{variant}"
    trunc = side["truncation"]
    items = _at(reply.body, trunc["itemsPath"])
    total = _at(reply.body, trunc["countPath"])
    if total > len(items) or total > trunc["cap"]:
        return "INCOMPLETE", "truncated"
    return "COMPLETE", None


def _at(body: JSONBody, path: str) -> Any:
    node: Any = body
    for part in path.removeprefix("$.").split("."):
        node = node[part]
    return node


def _file_outcome(states: dict[str, tuple[str, str | None]]) -> tuple[str, str | None]:
    """The outcome class and reason §7.1 fixes before any diff (None class = both complete)."""
    kinds = {v: s for v, (s, _) in states.items()}
    if kinds["a"] == kinds["b"] == "ERROR":
        return "INCONCLUSIVE", "both_error"
    reasons = [r for (s, r) in states.values() if s in ("INCOMPLETE", "ERROR")]
    if reasons:
        return "INCONCLUSIVE", min(reasons, key=SECTION_7_1_PRECEDENCE.index)
    if kinds["a"] == kinds["b"] == "ABSENT":
        return "INCONCLUSIVE", "absent_both"
    if kinds["a"] == "ABSENT":
        return "NOT_FOUND_A", None
    if kinds["b"] == "ABSENT":
        return "NOT_FOUND_B", None
    return "COMPARED", None


def _verdict(outcome: str, findings: list[dict[str, Any]]) -> str:
    if outcome in ("MATCH", "NOISE_ONLY"):
        return Verdict.MATCH
    if outcome == "KNOWN_DIFF":
        return (
            Verdict.DIFFERENT
            if any(f.get("rule_classification") == "bug" for f in findings)
            else Verdict.MATCH
        )
    if outcome in ("MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"):
        return Verdict.DIFFERENT
    return Verdict.INDETERMINATE


@pytest.mark.parametrize("pair", PAIRS)
def test_top_level(pair: str, expected: dict[str, Any], datasets: dict[str, ScriptedDataset]) -> None:
    doc = expected[pair]
    assert doc["pair"] == pair
    assert doc["all_other_keys"] == "MATCH" and doc["all_other_keys_findings"] == []
    assert set(doc["modes"]) == set(MODES)
    assert (doc["spec"], doc["targets"], doc["keys_file"]) == (
        f"mocks/{pair}/spec.yaml",
        f"mocks/{pair}/targets.yaml",
        f"mocks/{pair}/keys.txt",
    )


@pytest.mark.parametrize("pair", PAIRS)
def test_lists_every_seeded_key_and_only_sampled_keys(
    pair: str,
    expected: dict[str, Any],
    datasets: dict[str, ScriptedDataset],
    keys_files: dict[str, list[str]],
) -> None:
    keys = expected[pair]["keys"]
    assert set(keys) <= set(keys_files[pair])
    assert {key: entry["seed"] for key, entry in keys.items()} == {
        k: s for s, k in datasets[pair].seeded.items()
    }


def test_entries_are_well_formed(expected: dict[str, Any], core_codes: set[str]) -> None:
    for pair, key, entry in _entries(expected):
        where = f"{pair} {key}"
        assert entry["why"].strip(), where
        for mode in MODES:
            result = entry[mode]
            allowed = MODE_FIELDS | (LIVE_ONLY_FIELDS if mode == "live" else set())
            assert MODE_FIELDS <= set(result) <= allowed, where
            outcome = Outcome(result["outcome"])
            assert (result["inconclusive_reason"] is not None) == (outcome is Outcome.INCONCLUSIVE), where
            if result["inconclusive_reason"] is not None:
                assert result["inconclusive_reason"] in SECTION_7_1_PRECEDENCE, where
            assert (result["drift_reason"] is not None) == (outcome is Outcome.DRIFT), where
            if result["drift_reason"] is not None:
                DriftReason(result["drift_reason"])
            assert result["verdict"] == _verdict(result["outcome"], result["findings"]), where
            suppressed = sum(f.get("count", 1) for f in result["findings"] if "rule_id" in f)
            assert result["suppressed_count"] == suppressed, where
            if mode == "live":
                assert isinstance(result["rechecked"], bool), where
        for finding in _all_findings(entry):
            assert set(finding) <= FINDING_FIELDS and "path_pattern" in finding, where
            assert "taxonomy_code" in finding or "change_type" in finding, where
            if "taxonomy_code" in finding:
                assert finding["taxonomy_code"] in core_codes, where
            if "change_type" in finding:
                ChangeType(finding["change_type"])
            if "severity" in finding:
                Severity(finding["severity"])
            if "rule_classification" in finding:
                RuleClassification(finding["rule_classification"])
                assert "rule_id" in finding, where
            assert finding.get("count", 1) >= 1, where


def test_outcomes_agree_with_their_findings(expected: dict[str, Any]) -> None:
    for pair, key, entry in _entries(expected):
        for mode in MODES:
            result = entry[mode]
            findings = result["findings"]
            above_info = [f for f in findings if f.get("taxonomy_code") != "NORMALIZATION_ONLY"]
            outcome = result["outcome"]
            if outcome == "MATCH":
                assert above_info == [], (pair, key, mode)
            elif outcome == "KNOWN_DIFF":
                assert above_info and all("rule_id" in f for f in above_info), (pair, key, mode)
            elif outcome == "MISMATCH":
                assert any("rule_id" not in f for f in above_info), (pair, key, mode)
            elif outcome in ("NOT_FOUND_A", "NOT_FOUND_B"):
                code = "MISSING_DOCUMENT" if outcome == "NOT_FOUND_B" else "EXTRA_DOCUMENT"
                assert [(f["taxonomy_code"], f["path_pattern"]) for f in findings] == [(code, "$")], (
                    pair,
                    key,
                )
            else:  # INCONCLUSIVE and DRIFT keep no finding of their own
                assert findings == [], (pair, key, mode)


def test_severities_come_from_the_spec_or_the_contract(
    expected: dict[str, Any], specs: dict[str, Any]
) -> None:
    for pair, key, entry in _entries(expected):
        compare = specs[pair]["compare"]
        field_sev = {r["path"]: r.get("severity") for r in compare["fields"]}
        array_sev = {r["path"] + "[*]": r.get("severity") for r in compare["arrays"]}
        for finding in _all_findings(entry):
            code = finding.get("taxonomy_code")
            if code in CONTRACT_SEVERITY:
                want = CONTRACT_SEVERITY[code]
            elif finding.get("change_type") in ("missing_in_b", "extra_in_b"):
                want = array_sev.get(finding["path_pattern"])
            else:
                want = field_sev.get(finding["path_pattern"])
            assert finding.get("severity") == want, (pair, key, finding)


def test_known_diff_findings_name_real_rules(expected: dict[str, Any], specs: dict[str, Any]) -> None:
    for pair, key, entry in _entries(expected):
        rules = {r["id"]: r["classification"] for r in specs[pair].get("knownDiffs", [])}
        for finding in _all_findings(entry):
            if "rule_id" in finding:
                assert rules[finding["rule_id"]] == finding["rule_classification"], (pair, key)


def test_rechecked_follows_the_spec(expected: dict[str, Any], specs: dict[str, Any]) -> None:
    for pair, key, entry in _entries(expected):
        first = entry["live"].get("first_outcome", entry["live"]["outcome"])
        assert entry["live"]["rechecked"] == (first in specs[pair]["recheck"]["outcomes"]), (pair, key)


@pytest.mark.parametrize("pair", PAIRS)
def test_live_equals_file_unless_the_key_changes_between_calls(
    pair: str, expected: dict[str, Any], datasets: dict[str, ScriptedDataset]
) -> None:
    ds = datasets[pair]
    for key, entry in expected[pair]["keys"].items():
        time_dependent = any(len(ds.script(v, key)) > 1 for v in ("a", "b"))
        live = {k: v for k, v in entry["live"].items() if k not in LIVE_ONLY_FIELDS}
        if time_dependent:
            assert live != entry["file"] or "attempts" in entry["live"], key
        else:
            assert live == entry["file"], key


@pytest.mark.parametrize("pair", PAIRS)
def test_file_mode_outcomes_follow_from_the_recorded_replies(
    pair: str,
    expected: dict[str, Any],
    datasets: dict[str, ScriptedDataset],
    specs: dict[str, Any],
    mapped: Mapped,
    absence_shape: Callable[..., bool],
) -> None:
    ds, truth = datasets[pair], expected[pair]
    for key in ds.keys:
        replies = {v: recorded_reply(ds.script(v, key)) for v in ("a", "b")}
        states = {v: _side_state(pair, v, r, specs[pair], absence_shape) for v, r in replies.items()}  # type: ignore[arg-type]
        want = (
            truth["keys"]
            .get(key, {})
            .get("file", {"outcome": "MATCH", "inconclusive_reason": None, "findings": []})
        )
        outcome, reason = _file_outcome(states)
        if outcome != "COMPARED":
            assert (want["outcome"], want["inconclusive_reason"]) == (outcome, reason), key
            continue
        assert want["outcome"] in ("MATCH", "MISMATCH", "KNOWN_DIFF"), key
        differs = mapped(pair, "a", replies["a"].body) != mapped(pair, "b", replies["b"].body)
        assert differs == bool(want["findings"]), key


@pytest.mark.parametrize("pair", PAIRS)
def test_finding_paths_exist_in_the_canonical_documents(
    pair: str,
    expected: dict[str, Any],
    datasets: dict[str, ScriptedDataset],
    mapped: Mapped,
    strict_jsonpath: Any,
) -> None:
    ds = datasets[pair]
    for key, entry in expected[pair]["keys"].items():
        docs = [
            mapped(pair, v, reply.body)
            for v in ("a", "b")
            for reply in ds.script(v, key)  # type: ignore[arg-type]
            if reply.status == 200
        ]
        for finding in _all_findings(entry):
            if finding["path_pattern"] == "$":
                continue
            path = strict_jsonpath.compile(finding["path_pattern"])
            assert any(path.findall(doc) for doc in docs), (key, finding["path_pattern"])
