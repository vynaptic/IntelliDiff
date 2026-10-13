"""Judging each stratum (docs/mvp/SAMPLING.md §8): counts over a stratum's members that reconcile, a bug
rule's KNOWN_DIFF discrepant, every gate of a sampled and a census stratum with the refusal it words, the
failures certain while the cycle is live, bounds reported but never gated; and a drawn cycle judged however
it ends (a failed run, an erasure, an abandoned run), unjudged when judging raises, its stored judgement
the one judging the ended cycle again makes."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Context, Decimal
from fractions import Fraction
from typing import Any

import pytest

from deltasleuth import approvals
from deltasleuth.core import sampling
from deltasleuth.core.spec import compile_spec
from deltasleuth.runner import purge, run_worker, sample
from deltasleuth.runner.counters import ReconciliationError
from deltasleuth.runner.cycle import fail_run
from deltasleuth.runner.runs import create_run, pause_run, stop_run
from deltasleuth.runner.stop import STOP_PREFIX
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import UNAVAILABLE, Harness, ScriptedService, doc
from .sampled import KEYS, cycle_row, drawn, keys, listed

SPEC = compile_spec(json.loads(listed(resolve={"times": 3, "delay": "0s"})))
MILLE, HIGH = sampling.TARGETS["standard"], sampling.TARGETS["high"]
ONE_IN_100 = sampling.percent(sampling.shown(1, 100, MILLE, False))


def group(
    n: int = 1, outcome: str | None = "MATCH", stratum: str | None = "s", **fields: Any
) -> dict[str, Any]:
    """``n`` keys of ``repo.stratum_counts``: done members agreeing, unless ``fields`` say otherwise."""
    row = {"stratum": stratum, "member": True, "done": outcome is not None, "retried": False}
    row |= {"outcome": outcome, "first_outcome": outcome, "confirmation": "rechecked"}
    row |= {"inconclusive_reason": None, "drift_reason": None, "truncated": None, "bug": False, "n": n}
    return row | fields


def unresolved(n: int, reason: str = "timeout", **fields: Any) -> dict[str, Any]:
    return group(n, "INCONCLUSIVE", inconclusive_reason=reason, **fields)


def record(
    drawn_: int, frame_keys: int | None = None, target: Fraction = MILLE, label: str = "s"
) -> dict[str, Any]:
    """A draw's record of a sign-off stratum: sampled from ten times its size, or a census."""
    frame_keys = frame_keys or 10 * drawn_
    census = frame_keys <= drawn_
    x_max = sampling.share(target, frame_keys) if census else sampling.x_max(drawn_, target)
    return {
        "label": label, "tier": "standard" if target == MILLE else "high",
        "target": str(Decimal(target.numerator) / Decimal(target.denominator)), "size": drawn_,
        "basis": "census" if census else "sample", "frame_keys": frame_keys, "drawn": drawn_, "contrast": 0,
        "u_max": "1", "x_max": x_max,
    }  # fmt: skip


def judged(stratum: dict[str, Any], *groups: dict[str, Any]) -> dict[str, Any]:
    keys_ = sum(g["n"] for g in groups)
    return sample.judged({"draw_id": "D-X", "keys": keys_, "strata": [stratum]}, list(groups), SPEC)


def test_counts_reconcile_and_a_bug_rules_known_diff_is_discrepant() -> None:
    rows = [
        group(3000), group(2, "KNOWN_DIFF"), group(1, "KNOWN_DIFF", bug=True), group(3, "NOISE_ONLY"),
        group(1, "MISMATCH"), group(1, "NOT_FOUND_A"), unresolved(1, "absent_both"),
        unresolved(1, "truncated", truncated="b"), unresolved(1), group(5, member=False, stratum="s"),
    ]  # fmt: skip
    judgement = judged(record(3011), *rows)
    [s] = judgement["strata"]
    assert (s["n"], s["x"], s["unresolved"], s["absent_both"], s["api_uncomparable"]) == (3008, 3, 1, 1, 1)
    assert (s["counters"]["total"], s["counters"]["match"], s["counters"]["suppressed"]) == (3011, 3005, 6)
    assert all(i["holds"] for i in s["counters"]["reconciliation"])
    assert judgement["contrast"]["total"] == 5 and judgement["contrast"]["match"] == 5
    bound = sampling.percent(sampling.shown(3, 3008, MILLE, False))
    assert (s["verdict"], s["why"]) == (
        "failed",
        [
            f"is over its 0.1% target: its one-sided 95% upper bound is {bound} (3 discrepant of 3,008"
            " cleanly compared keys)",
            "has 1 key(s) above the API cap that were not compared (truncated): declare pagination for"
            " side b's fetch in a new version and draw again",
        ],
    )
    assert judgement["status"] == "failed"
    assert judgement["weighted_rate"] == format(Context(prec=6).divide(Decimal(3), Decimal(3008)), "f")
    with pytest.raises(ReconciliationError):
        sample.judged({"draw_id": "D-X", "keys": 3017, "strata": [record(3011)]}, rows, SPEC)
    with pytest.raises(ReconciliationError):  # a member of no stratum of the draw
        judged(record(3011), *rows, group(1, stratum="t"))


G7 = "has {} unresolved key(s) after 3 retries, over the {} its 0.1% target allows (of {} cleanly compared"
G7 += " keys)"
G8 = "has {} key(s) absent on both sides, over the 3 its {} target allows of {} drawn: the frame was stale;"
G8 += " draw again"
G11 = (
    "has 1 key(s) above the API cap that were not compared (truncated): declare pagination for {} fetch in a"
)
G11 += " new version and draw again"
CASES = {
    "clean": (record(3011), [group(3011)], "passed", []),
    "G5": (record(2995), [group(2993), group(2, "MISMATCH")], "failed", [
        "is over its 0.1% target: its one-sided 95% upper bound is 0.2101% (2 discrepant of 2,995"
        " cleanly compared keys)"]),
    "G7": (record(3011), [group(3006), unresolved(5)], "failed", [G7.format(5, 3, "3,006")]),
    "G7 within": (record(3011), [group(3008), unresolved(3)], "passed", []),
    "G8": (record(3011), [group(2999), unresolved(12, "absent_both")], "failed",
           [G8.format(12, "0.1%", "3,011")]),
    "G8 at 0.01%": (record(30107, target=HIGH), [group(30103), unresolved(4, "absent_both")], "failed",
                    [G8.format(4, "0.01%", "30,107")]),
    "G8 within at 0.01%": (record(30107, target=HIGH), [group(30104), unresolved(3, "absent_both")], "passed",
                           []),
    "G11": (record(3011), [group(3010), unresolved(1, "truncated", truncated="ab")], "failed",
            [G11.format("side a's and side b's")]),
    "live": (record(3011), [group(100), group(2911, None)], "unfinished", []),
    "live, discrepant": (record(3011), [group(99), group(1, "MISMATCH"), group(2911, None)], "failed", [
        f"is over its 0.1% target: its one-sided 95% upper bound is {ONE_IN_100} (1 discrepant of 100"
        " cleanly compared keys)"]),
    "live, unresolved": (record(3011), [group(6), unresolved(4), group(3001, None)], "failed",
                         [G7.format(4, 0, 6)]),
    "live, retrying": (record(3011), [group(6), unresolved(4, done=False), group(3001, None)], "unfinished",
                       []),
    # A truncated key whose recheck was truncated again ends recheck_failed: still above the API cap (D13).
    "G11 after its recheck": (record(3011), [group(3010), unresolved(1, "recheck_failed", truncated="b")],
                              "failed", [G11.format("side b's")]),
    # First reads still due a recheck, which may find the key: never a certain failure.
    "live, absent awaiting its recheck": (
        record(3011), [group(1500), unresolved(4, "absent_both", done=False), group(1507, None)],
        "unfinished", []),
    "live, truncated awaiting its recheck": (
        record(3011), [group(1500), unresolved(1, "truncated", truncated="b", done=False), group(1510, None)],
        "unfinished", []),
}  # fmt: skip


@pytest.mark.parametrize("case", CASES)
def test_a_sampled_stratum_passes_only_within_every_gate(case: str) -> None:
    stratum, rows, verdict, why = CASES[case]
    [s] = judged(stratum, *rows)["strata"]
    assert (s["verdict"], s["why"]) == (verdict, why)
    if verdict == "passed":
        assert Decimal(s["ub95_one_sided"]) <= Decimal(stratum["target"])


def test_an_ended_cycle_words_why_its_unfinished_strata_would_not_pass() -> None:
    """Members waiting for a retry when the cycle ended: unfinished, never failed, and the judgement its end
    stores words each gate the stratum does not hold, so its refusal shows why; live, only certain
    failures."""
    rows = [group(2991), unresolved(20, done=False)]
    live = judged(record(3011), *rows)
    ended = sample.judged({"draw_id": "D-X", "keys": 3011, "strata": [record(3011)]}, rows, SPEC, True)
    bound = sampling.percent(sampling.shown(0, 2991, MILLE, False))
    assert [(s["verdict"], s["why"]) for s in live["strata"]] == [("unfinished", [])]
    assert [(s["verdict"], s["why"]) for s in ended["strata"]] == [("unfinished", [
        f"is over its 0.1% target: its one-sided 95% upper bound is {bound} (0 discrepant of 2,991 cleanly"
        " compared keys)",
        G7.format(20, 2, "2,991"),
    ])]  # fmt: skip
    assert (live["status"], ended["status"], bound) == ("incomplete", "incomplete", "0.1002%")


def test_a_stopped_stratum_that_would_pass_is_unfinished() -> None:
    # 3,010 keys compared clean pass on their own (n ≥ 2,995), but a member was never read: no pass.
    [s] = judged(record(3011), group(3010), group(1, None))["strata"]
    assert (s["verdict"], s["why"], s["counters"]["cancelled"]) == ("unfinished", [], 1)
    judgement = judged(record(3011), group(3010), group(1, None))
    assert judgement["status"] == "incomplete"


def test_a_done_members_only_comparable_read_that_found_a_discrepancy_counts_as_discrepant() -> None:
    # Its first read unusable, its recheck discrepant and none left: counted outstanding, never left out.
    owed = group(1, "MISMATCH", first_outcome="INCONCLUSIVE", confirmation="provisional")
    [s] = judged(record(3011), group(3010), owed)["strata"]
    assert (s["counters"]["outstanding"], s["n"], s["x"], s["verdict"]) == (1, 3011, 1, "failed")


G6 = "is over its {} target: compared whole, its {} keys allow {} discrepant or unresolved key(s); it has {}"


@pytest.mark.parametrize(
    ("frame_keys", "target", "rows", "verdict", "why"),
    [
        (4210, HIGH, [group(4209), group(1, "MISMATCH")], "failed", [G6.format("0.01%", "4,210", 0, 1)]),
        (5000, HIGH, [group(5000)], "passed", []),  # its binomial bound, 0.05990%, would fail 0.01%
        (2000, MILLE, [group(1998), unresolved(1), unresolved(1, "absent_both")], "passed", []),
        (2000, MILLE, [group(1997), group(1, "MISMATCH"), unresolved(1), unresolved(1, "absent_both")],
         "failed", [G6.format("0.1%", "2,000", 1, 2)]),
        # Allowed on N_h - absent_both: 1,000 keys alone would allow 1.
        (1000, MILLE, [group(998), group(1, "MISMATCH"), unresolved(1, "absent_both")], "failed",
         [G6.format("0.1%", "1,000", 0, 1)]),
        (4210, HIGH, [group(4209), group(1, None)], "unfinished", []),
        (4210, HIGH, [group(4200), unresolved(1), group(9, None)], "failed",
         [G6.format("0.01%", "4,210", 0, 1)]),
    ],
    ids=["G6", "no binomial bound", "allowance", "allowance spent", "less the absent", "live", "certain"],
)  # fmt: skip
def test_a_census_stratum_is_judged_on_its_exact_count(
    frame_keys: int, target: Fraction, rows: list[dict[str, Any]], verdict: str, why: list[str]
) -> None:
    [s] = judged(record(frame_keys, frame_keys, target), *rows)["strata"]
    assert (s["basis"], s["verdict"], s["why"]) == ("census", verdict, why)
    assert s["ub95_one_sided"] is s["ub95_one_sided_all_suppressed"] is s["ub95_one_sided_worst_case"] is None


def test_the_reported_bounds_are_never_gated() -> None:
    rows = [group(3000), group(8, "KNOWN_DIFF"), group(3, "NOISE_ONLY")]
    [s] = judged(record(3011), *rows)["strata"]
    assert (s["verdict"], s["x"], s["ub95_one_sided"]) == ("passed", 0, sampling.shown(0, 3011, MILLE, True))
    assert s["ub95_one_sided_all_suppressed"] == sampling.shown(
        11, 3011, MILLE, False
    )  # every suppressed key
    [w] = judged(record(3011), group(3009), unresolved(2))["strata"]
    assert (w["verdict"], w["ub95_one_sided_worst_case"]) == ("passed", sampling.shown(2, 3011, MILLE, False))
    assert Decimal(w["ub95_one_sided_worst_case"]) > Decimal("0.001") >= Decimal(w["ub95_one_sided"])


def test_a_rate_sample_without_strata_judges_its_keys_as_contrast() -> None:
    rows = [group(7, stratum=None, member=False), group(1, "MISMATCH", stratum=None, member=False)]
    judgement = sample.judged({"draw_id": "D-X", "keys": 8, "strata": []}, rows, SPEC)
    assert (judgement["strata"], judgement["weighted_rate"], judgement["contrast"]["different"]) == (
        [],
        None,
        1,
    )


# -- every end of a drawn cycle (§5 Ends) -------------------------------------------------------------


async def _drawn_run(h: Harness, service: ScriptedService) -> str:
    """A run whose sample is drawn, no key of it compared."""
    service.listings = [KEYS]
    run_id = await create_run(h.ctx, await h.spec(listed()), "batch", who=TESTER)
    await sample.draw(h.ctx, run_id)
    return run_id


def _unfinished(ended: repo.Row | None) -> list[tuple[str, str]]:
    assert ended is not None and ended["sample"]["status"] == "incomplete"
    return [(s["label"], s["verdict"]) for s in ended["sample"]["strata"]]


@pytest.mark.db
async def test_a_failed_run_is_judged_on_a_connection_of_its_own(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await _drawn_run(harness, service)
    await fail_run(harness.ctx, run_id, RuntimeError("a bug"), who=TESTER)
    assert (await harness.run(run_id))["status"] == "failed"
    assert _unfinished(await cycle_row(harness, run_id)) == [
        ("half=low", "unfinished"),
        ("half=high", "unfinished"),
    ]


@pytest.mark.db
async def test_an_erased_run_is_judged_before_its_data_goes(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await _drawn_run(harness, service)
    await purge.erase(harness.ctx, TESTER, run_id, "a data subject's request")
    ended = await cycle_row(harness, run_id)
    assert _unfinished(ended) == [("half=low", "unfinished"), ("half=high", "unfinished")]
    async with harness.ctx.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM ds.run_keys WHERE run_id = %s", (run_id,))
        assert ended is not None and ended["purged_at"] is not None and await cur.fetchone() == (0,)
    assert await drawn(harness, run_id) is not None  # the ledger outlives purges


@pytest.mark.db
async def test_an_abandoned_run_is_judged(harness: Harness, service: ScriptedService) -> None:
    run_id = await _drawn_run(harness, service)
    await pause_run(harness.ctx, run_id, who=TESTER)
    async with harness.ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.runs SET updated_at = now() - interval '91 days' WHERE run_id = %s", (run_id,)
        )
    assert await purge.abandon(harness.ctx, TESTER) == 1
    assert (await harness.run(run_id))["status"] == "cancelled"
    assert _unfinished(await cycle_row(harness, run_id)) == [
        ("half=low", "unfinished"),
        ("half=high", "unfinished"),
    ]


@pytest.mark.db
async def test_a_cycle_stopped_while_a_member_waits_for_its_retry_is_never_signed_off(
    harness: Harness, service: ScriptedService
) -> None:
    """Every member holds a result, so the cycle's counters cancel and owe nothing, but K001 waits for its
    retry: the judgement its end stores is incomplete, its unfinished stratum words the gate it does not
    hold, and the sign-off is refused (R-b)."""
    service.listings = [KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    service.script("a", "K001", UNAVAILABLE)
    body = json.loads(listed(resolve={"times": 2, "delay": "1h"}))
    body["recheck"]["outcomes"] = ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"]  # its first read is final
    spec_id = await harness.spec(json.dumps(body))
    run_id = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(harness.ctx, stop=stop))
    try:
        for _ in range(600):
            taken = {k["key"]: k for k in await keys(harness, run_id)}
            if taken and taken["K001"]["retries"] and all(k["state"] == "done" for k in taken.values()
                                                          if k["key"] != "K001"):  # fmt: skip
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("K001 never waited for its retry")
        await stop_run(harness.ctx, run_id, STOP_PREFIX + "maxDuration", who=TESTER)
        run = await harness.wait(run_id)
    finally:
        stop.set()
        await worker
    ended, stored = await cycle_row(harness, run_id), await drawn(harness, run_id)
    assert ended is not None and stored is not None
    assert (ended["counters"]["cancelled"], ended["counters"]["outstanding"]) == (0, 0)
    assert ended["sample"]["status"] == "incomplete"
    async with harness.ctx.pool.connection() as conn:
        spec = await harness.ctx.spec(conn, spec_id)
        found = await sample.problems(conn, run, 1, spec)
        refused = await approvals.signoff_problems(conn, run, 1, spec)
    at = f"cycle 1 of run {run_id}"
    assert (
        found
        == refused[-2:]
        == [
            f"stratum half=low of {at} {G6.format('0.1%', 4, 0, 1)}",
            f"the sample {stored['draw_id']} of {at} ended incomplete (stratum half=low unfinished): its"
            " cycle ended before every key was final; draw again",
        ]
    )


@pytest.mark.db
async def test_a_judgement_that_raises_leaves_the_cycle_unjudged(
    harness: Harness,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raising(*_: Any) -> dict[str, Any]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(sample, "judged", raising)
    service.listings = [KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    run_id = await create_run(harness.ctx, await harness.spec(listed()), "batch", who=TESTER)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.sample"):
        run = await harness.finish(run_id)
    ended = await cycle_row(harness, run_id)
    assert (run["status"], ended is not None and ended["sample"]) == ("completed", None)
    assert f"cycle 1 of run {run_id} left unjudged: RuntimeError" in caplog.messages


@pytest.mark.db
async def test_the_stored_judgement_is_the_one_judging_again_makes(
    harness: Harness, service: ScriptedService
) -> None:
    service.listings = [KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    service.script("a", "K001", UNAVAILABLE, UNAVAILABLE, (200, doc("K001")))  # retried once, then compared
    run_id = await create_run(harness.ctx, await harness.spec(listed()), "batch", who=TESTER)
    await harness.finish(run_id)
    stored, ended = await drawn(harness, run_id), await cycle_row(harness, run_id)
    assert stored is not None and ended is not None
    async with harness.ctx.pool.connection() as conn:
        again = await sample.judge(conn, stored, await harness.ctx.spec(conn, stored["spec_id"]))
    assert json.loads(json.dumps(again)) == ended["sample"]
    low = ended["sample"]["strata"][0]
    assert (low["retried"], low["n"], low["counters"]["total"], ended["sample"]["status"]) == (
        1,
        4,
        4,
        "passed",
    )
