"""Adjudication: stratified selection, the reviewer sheet and its score."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.context import Context
from deltasleuth.reports.adjudicate import (
    EXIT_MINIMUM,
    SCORE_NAME,
    SHEET_COLUMNS,
    AdjudicationError,
    adjudicate,
    findings_summary,
    score,
    select,
    unrepresented,
)
from deltasleuth.reports.csv import read_csv, render_csv
from deltasleuth.reports.data import read_json

from .conftest import SECRET_NAME, doc, seed_run


def record(key: str, outcome: str, *causes: str, severity: str = "HIGH") -> dict[str, Any]:
    common = {"suppressed": False, "severity": severity, "rule_id": None, "path_pattern": "$.x"}
    finding = common | {"taxonomy_code": "VALUE_MISMATCH", "change_type": "value_changed"}
    findings = [finding | {"root_cause_id": rc, "json_pointer": f"/x{i}"} for i, rc in enumerate(causes)]
    return {"key_masked": key, "outcome": outcome, "findings": findings}


def population(causes: int) -> list[dict[str, Any]]:
    records = [record(f"M{i}", "MATCH") for i in range(900)]
    records += [record(f"I{i}", "INCONCLUSIVE") for i in range(40)] + [
        record(f"D{i}", "DRIFT") for i in range(5)
    ]
    return records + [record(f"X{i}", "MISMATCH", f"RC-{i:03d}") for i in range(causes)]


def test_selection_covers_every_root_cause_and_outcome_deterministically() -> None:
    records = [record(f"M{i}", "MATCH") for i in range(80)]
    records += [
        record("X1", "MISMATCH", "RC-1"),
        record("X2", "MISMATCH", "RC-1"),
        record("X3", "MISMATCH", "RC-2"),
    ]
    records += [record("I1", "INCONCLUSIVE"), record("N1", "NOT_FOUND_B", "RC-3")]
    chosen = select(records, limit=10, salt="s")
    reasons = {r["key_masked"]: why for r, why in chosen}
    assert len(chosen) == 10 and {"X3", "N1", "I1"} <= reasons.keys() and reasons.keys() & {"X1", "X2"}
    assert reasons["X3"] == "root cause RC-2" and reasons["I1"] == "outcome INCONCLUSIVE"
    assert select(records, limit=10, salt="s") == chosen and select(records, limit=10, salt="t") != chosen
    assert len(select(records[:3], limit=50)) == 3
    with pytest.raises(AdjudicationError):
        select(records, limit=0)


@pytest.mark.parametrize("causes", [48, 49, 60])
def test_selection_keeps_match_keys_and_every_outcome_when_causes_exceed_the_limit(causes: int) -> None:
    records = population(causes)
    outcomes = [r["outcome"] for r, _ in select(records, limit=50, salt="s")]
    assert len(outcomes) == 50 and set(outcomes) == {"MATCH", "INCONCLUSIVE", "DRIFT", "MISMATCH"}
    assert outcomes.count("MATCH") == 10  # the reserved share: 20% of the limit


def test_match_share_is_capped_by_the_population() -> None:
    records = [record("M0", "MATCH")] + [record(f"X{i}", "MISMATCH", f"RC-{i}") for i in range(30)]
    outcomes = [r["outcome"] for r, _ in select(records, limit=50, salt="s")]
    assert outcomes.count("MATCH") == 1 and len(outcomes) == 31


def test_what_the_limit_leaves_out_is_reported() -> None:
    records = population(60)
    chosen = {r["key_masked"]: why for r, why in select(records, limit=50, salt="s")}
    covered = {f["root_cause_id"] for r in records if r["key_masked"] in chosen for f in r["findings"]}
    left = unrepresented(records, chosen)
    assert left["outcomes"] == [] and len(left["root_causes"]) == 60 - len(covered) > 0
    assert set(left["root_causes"]).isdisjoint(covered)


def test_a_secondary_root_cause_gets_its_own_key_and_severe_causes_come_first() -> None:
    x0 = record("X0", "MISMATCH", "RC-A", "RC-B")
    x0["findings"][1]["severity"] = "LOW"
    records = [
        x0,
        record("Y0", "MISMATCH", "RC-B", severity="LOW"),
        *(record(f"M{i}", "MATCH") for i in range(40)),
    ]
    reasons = {r["key_masked"]: why for r, why in select(records, limit=5, salt="s")}
    assert reasons["X0"] == "root cause RC-A" and reasons["Y0"] == "root cause RC-B"
    assert unrepresented(records, {"X0": "root cause RC-A"})["root_causes"] == ["RC-B"]
    assert unrepresented([x0, *records[2:]], {"X0": "root cause RC-A"})["root_causes"] == []
    low = [record(f"L{i}", "MISMATCH", f"RC-L{i}", severity="LOW") for i in range(5)]
    short = [
        *(record(f"M{i}", "MATCH") for i in range(20)),
        *low,
        record("C0", "MISMATCH", "RC-C", severity="CRITICAL"),
    ]
    assert "C0" in {r["key_masked"] for r, _ in select(short, limit=3, salt="s")}


def test_findings_summary_lists_the_top_findings() -> None:
    r = record("X", "MISMATCH", "RC-1", "RC-2", "RC-3", "RC-4")
    r["findings"][1].update(suppressed=True, rule_id="KD-1")
    r["findings"][2].update(severity="INFO", taxonomy_code="NORMALIZATION_ONLY")
    summary = findings_summary(r)
    assert summary.startswith("HIGH VALUE_MISMATCH value_changed $.x") and summary.endswith("+1 more")
    assert "(suppressed by KD-1)" in summary


async def _run(ctx: Context, n: int) -> str:
    """n keys: K002 MISMATCH, K003 INCONCLUSIVE (side b timed out), the rest MATCH."""
    samples = {f"K{i:03d}": (doc(f"K{i:03d}"), doc(f"K{i:03d}")) for i in range(1, n + 1)}
    samples["K002"] = (doc("K002", status="A"), doc("K002", status="B"))
    samples["K003"] = (doc("K003"), "timeout")
    return await seed_run(ctx, samples)


def fill(sheet: Path, *, notes: str = "", verdicts: dict[str, str] | None = None, skip: str = "") -> Path:
    header, rows = read_csv(sheet)
    for row in rows:
        verdict = (verdicts or {}).get(row["key"], row["engine_verdict"])
        row["reviewer_verdict"], row["reviewer_notes"] = (
            ("", "") if row["engine_outcome"] == skip else (verdict, notes)
        )
    sheet.write_text(render_csv(header, rows), newline="")
    return sheet


@pytest.mark.db
async def test_full_agreement_on_fifty_keys_passes_the_exit(ctx: Context, tmp_path: Path) -> None:
    run_id = await _run(ctx, EXIT_MINIMUM + 5)
    selection = await adjudicate(ctx, run_id, tmp_path)
    assert len(selection["keys"]) == EXIT_MINIMUM and selection["candidates"] == EXIT_MINIMUM + 5
    header, rows = read_csv(tmp_path / "adjudication.csv")
    assert header == list(SHEET_COLUMNS) and all(r["reviewer_verdict"] == "" for r in rows)
    assert {"K002", "K003"} <= {r["key"] for r in rows} and SECRET_NAME not in (
        tmp_path / "adjudication.csv"
    ).read_text()
    fill(tmp_path / "adjudication.csv")
    report = await score(ctx, tmp_path)
    assert (report["adjudicated"], report["agreed"], report["agreement_rate"]) == (50, 50, "1.0000")
    assert report["exit"] == {"passed": True, "minimum": EXIT_MINIMUM, "reasons": []}
    assert read_json(tmp_path / SCORE_NAME) == report


@pytest.mark.db
async def test_disagreements_and_missing_outcomes_fail_the_exit(ctx: Context, tmp_path: Path) -> None:
    run_id = await _run(ctx, 6)
    await adjudicate(ctx, run_id, tmp_path)
    fill(tmp_path / "adjudication.csv", verdicts={"K002": "match"})
    report = await score(ctx, tmp_path)
    assert (
        report["disagreements"][0]["key"] == "K002"
        and report["disagreements"][0]["reviewer_verdict"] == "MATCH"
    )
    assert report["confusion"]["DIFFERENT"] == {"MATCH": 1}
    assert report["per_outcome"]["MISMATCH"] == {"adjudicated": 1, "agreed": 0}
    reasons = " | ".join(report["exit"]["reasons"])
    assert "at least 50" in reasons and "disagreement" in reasons
    fill(tmp_path / "adjudication.csv", skip="MATCH")  # reviewers only looked at the discrepant keys
    report = await score(ctx, tmp_path)
    assert "no adjudicated key has engine outcome MATCH (4 in the run)" in report["exit"]["reasons"]


@pytest.mark.db
async def test_a_reviewed_sheet_is_never_overwritten_and_a_new_one_voids_the_score(
    ctx: Context, tmp_path: Path
) -> None:
    run_id = await _run(ctx, 6)
    await adjudicate(ctx, run_id, tmp_path)
    await adjudicate(ctx, run_id, tmp_path)  # a blank sheet is simply rewritten
    reviewed = fill(tmp_path / "adjudication.csv", notes="checked by alice").read_bytes()
    await score(ctx, tmp_path)
    with pytest.raises(AdjudicationError, match="already holds reviewer"):
        await adjudicate(ctx, run_id, tmp_path, salt="other")
    assert (tmp_path / "adjudication.csv").read_bytes() == reviewed
    selection = await adjudicate(ctx, run_id, tmp_path, force=True)
    assert (
        selection["archived_sheet"] == "adjudication.1.csv"
        and (tmp_path / "adjudication.1.csv").read_bytes() == reviewed
    )
    assert not (tmp_path / SCORE_NAME).exists()


@pytest.mark.db
async def test_score_accepts_only_the_selected_keys_once_with_the_engine_columns(
    ctx: Context, tmp_path: Path
) -> None:
    run_id = await _run(ctx, 8)
    await adjudicate(ctx, run_id, tmp_path, limit=4)
    sheet = fill(tmp_path / "adjudication.csv")
    header, rows = read_csv(sheet)

    async def rejects(bad_rows: list[dict[str, str]], match: str) -> None:
        sheet.write_text(render_csv(header, bad_rows), newline="")
        with pytest.raises(AdjudicationError, match=match):
            await score(ctx, tmp_path)

    await rejects(rows + rows[:1], "repeats key")
    await rejects([*rows, {**rows[0], "key": "BOGUS-1"}], "did not select")
    flipped = "MATCH" if rows[0]["engine_verdict"] != "MATCH" else "DIFFERENT"
    await rejects([{**rows[0], "engine_verdict": flipped}, *rows[1:]], "engine columns differ")
    await rejects([{**rows[0], "reviewer_verdict": "PROBABLY"}, *rows[1:]], "reviewer_verdict must be one of")
    sheet.write_bytes(render_csv(header, [{**rows[0], "reviewer_notes": "vérifié"}]).encode("cp1252"))
    with pytest.raises(AdjudicationError, match="save it as 'CSV UTF-8'"):
        await score(ctx, tmp_path)
    sheet.write_text(render_csv(["key"], [{"key": rows[0]["key"]}]))
    with pytest.raises(AdjudicationError, match="lacks column"):
        await score(ctx, tmp_path)
    assert not (tmp_path / SCORE_NAME).exists()


P0_GOLDEN = Path(__file__).with_name("adjudicate_p0.golden.json")


@pytest.mark.db
async def test_the_p0_commands_write_what_they_wrote_before_the_store_split(
    ctx: Context, tmp_path: Path
) -> None:
    run_id = await _run(ctx, 12)
    await adjudicate(ctx, run_id, tmp_path, limit=8)
    notes, verdicts = "checked by alice", {"K002": "match"}
    fill(tmp_path / "adjudication.csv", notes=notes, verdicts=verdicts, skip="INCONCLUSIVE")
    await score(ctx, tmp_path)
    written = {
        name: (tmp_path / name).read_text(encoding="utf-8").replace(run_id, "R-RUN")
        for name in ("adjudication.csv", "adjudication.json", SCORE_NAME)
    }
    assert json.dumps(written, indent=2, sort_keys=True) + "\n" == P0_GOLDEN.read_text()
