"""The open build in the runner (docs/mvp/LICENCE.md §19): every spec name runs, in oidc mode too, no pair
is counted, no run records a licence and no soak pauses for one; on a store an evaluation left, its runs
keep their own records, its licence-paused soak resumes, the range of licence misses a schedule held is
chained once and the worker audits the change of state; and a vendor key brings the evaluation back."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from deltasleuth import licence, schedules
from deltasleuth.core import cron
from deltasleuth.licence import LICENCE_PAUSE
from deltasleuth.runner import compare_one, create_run, resume_run
from deltasleuth.security.signing import PackSigner
from deltasleuth.store import repo
from tests.conftest import TESTER, VENDOR_KEY

from .conftest import Harness, OpenHarness, ScriptedService, doc
from .notifying import deliveries, hook, notifying, receiving, rule
from .scheduling import DAY, HOUR, fire, minute, scheduled, shown, started
from .test_approvals import OIDC, approve, audited, sql, targets
from .test_licence_admission import EVALUATION, KEYS, SOAK, _unseated_soak, named, refusal, standing

pytestmark = pytest.mark.db
OPEN = (None, "open", None, None)  # licence_id, state, limit, since


async def recorded(h: Harness) -> list[dict | None]:
    """Each run's licence record, oldest run first."""
    rows = await sql(h, "SELECT params->'licence' AS licence FROM ds.runs ORDER BY created_at, run_id")
    return [row["licence"] for row in rows]


async def test_a_third_and_a_tenth_name_run(harness: Harness, service: ScriptedService) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    ids = [await harness.spec(named(f"pair-{n}")) for n in range(1, 12)]
    runs = [await create_run(harness.ctx, spec_id, "batch", who=TESTER) for spec_id in ids[:10]]
    assert {(await harness.finish(run_id))["status"] for run_id in (runs[2], runs[9])} == {"completed"}
    shot = await compare_one(harness.ctx, ids[10], "K001", who=TESTER)  # an eleventh, as a one-shot
    assert shot.verdict == "MATCH"
    assert await recorded(harness) == [None] * 11  # no record: no label on a run or its reports
    created = await audited(harness, "create_run", "compare_one")
    assert [row["detail"]["licence"] for row in created] == [None] * 11
    assert await audited(harness, "licence.pair") == []  # and no pair is counted
    current = await standing(harness)
    assert (current.licence, current.state, current.limit, current.since) == OPEN and current.counted == ()
    (state,) = await audited(harness, "licence.state")  # the worker's one row
    assert (state["actor"], state["detail"]) == (
        "system:worker", {"from": None, "to": "open", "licence_id": None, "ends": None},
    )  # fmt: skip


async def test_runs_compares_and_soak_cycles_under_sso_need_no_licence(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    async with open_harness(targets(service), auth=OIDC) as h:  # oidc mode, and no licence in the store
        assert await sql(h, "SELECT 1 FROM ds.licences") == []
        spec_id = await h.spec(named("accounts", ["K001"], execution=SOAK))
        await approve(h, spec_id)  # the four-eyes gate is not the licence's: it stays
        run_id = await create_run(h.ctx, spec_id, "soak", who=TESTER)
        run = await h.finish(run_id)  # both cycles: no boundary pauses it
        shot = await compare_one(h.ctx, spec_id, "K001", who=TESTER)
        current = await standing(h)
        assert (run["status"], run["cycle"], run["stop_reason"]) == (
            "completed", 2, "stop_condition:maxCycles",
        )  # fmt: skip
        assert shot.verdict == "MATCH" and await audited(h, "paused") == []
        assert (current.state, current.allows()) == (
            "open", {"runs": True, "sso": True, "signoff": True, "packs": True},
        )  # fmt: skip
        assert await recorded(h) == [None, None]


async def test_a_store_an_evaluation_left_goes_on_under_an_open_build(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = (PackSigner(VENDOR_KEY).public_pem,)
    monkeypatch.setattr(licence, "VENDOR_KEYS", key)  # the older build: an unlicensed evaluation
    run_id, m3 = await _unseated_soak(harness, service)
    paused = await harness.finish(run_id, ("paused", *repo.TERMINAL))
    assert (paused["status"], paused["stop_reason"]) == ("paused", LICENCE_PAUSE)
    assert await refusal(resume_run(harness.ctx, run_id, who=TESTER)) == f"run {run_id} cannot resume: {m3}"
    monkeypatch.setattr(licence, "VENDOR_KEYS", ())  # the open build starts on that store
    assert await resume_run(harness.ctx, run_id, who=TESTER) == "running"
    run = await harness.finish(run_id)
    assert (run["status"], run["cycle"], run["stop_reason"]) == ("completed", 2, "stop_condition:maxCycles")
    third = await create_run(harness.ctx, paused["spec_id"], "soak", who=TESTER)  # its name held no seat
    await harness.finish(third)
    # History is not rewritten: the evaluation's run keeps its record, and so its label; the new run has
    # none, as the two runs before it, which a release before 010 stored.
    assert await recorded(harness) == [None, None, EVALUATION, None]
    assert licence.run_label(run).startswith("Unlicensed evaluation: run ")  # type: ignore[union-attr]
    assert licence.run_label(await harness.run(third)) is None
    assert [row["detail"]["reason"] for row in await audited(harness, "paused")] == [LICENCE_PAUSE]
    states = [(row["detail"]["from"], row["detail"]["to"]) for row in await audited(harness, "licence.state")]
    assert states == [(None, "evaluation"), ("evaluation", "open")]
    monkeypatch.setattr(licence, "VENDOR_KEYS", key)  # a vendor build again: the evaluation is back
    assert await refusal(create_run(harness.ctx, paused["spec_id"], "soak", who=TESTER)) == m3


async def test_a_range_of_licence_misses_an_evaluation_held_is_chained_once(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule whose times an older build refused for the licence holds the later ones unchained
    (SCHEDULES §5 step 7). The open build misses no time for a licence: its worker chains that range once,
    at the schedule's next firing, which runs, and its notifier sends the row as any other."""
    second, nowhere = timedelta(seconds=1), "http://127.0.0.1:9"
    rules = [rule("schedules", ["schedule.missed"], ["ops"])]
    async with (  # no dedup: the misses a day apart are written seconds apart here
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, nowhere, {"ops": hook(base, ref)}, rules, dedup_hours=0) as (h, notifier),
    ):
        monkeypatch.setattr(licence, "VENDOR_KEYS", (PackSigner(VENDOR_KEY).public_pem,))  # the older build
        due = await minute(h)
        for name in ("pair-1", "pair-2"):  # the two pairs an unlicensed evaluation runs
            await create_run(h.ctx, await h.spec(named(name)), "batch", who=TESTER)
        row = await scheduled(h, await h.spec(named("pair-3")), due)
        assert await fire(h, row) is None  # its first time: refused for the licence (pairs), and chained
        assert await fire(h, row, due + DAY + second) is None  # its second: held, unchained
        await licence.watch(h.ctx)
        await notifier.feed()
        held, sent = (await shown(h, row["schedule_id"]))["held"], len(await deliveries(h))
        assert (held["reason"], held["code"], held["due"], held["count"]) == (
            "licence", "pairs", cron.utc(due + DAY), 1,
        )  # fmt: skip
        assert [missed["detail"]["due"] for missed in await audited(h, "schedule.missed")] == [cron.utc(due)]

        monkeypatch.setattr(licence, "VENDOR_KEYS", ())  # the open build starts on that store
        await licence.watch(h.ctx)
        assert await fire(h, row, due + DAY + HOUR) is None  # no time is due: nothing changes
        waiting = schedules.item(await shown(h, row["schedule_id"]))
        assert waiting["last"]["text"] == (  # the older build's refusal, until the schedule's next firing
            f"not started: 1 more time since {cron.utc(due + DAY)} for the same reason, chained as one row"
            " within 24 h"
        )
        assert await fire(h, row, due + 2 * DAY + second) is None  # its next time: it runs
        await notifier.feed()
        (run,) = await started(h, row)
        assert (run["params"]["schedule"]["due"], run["params"]["licence"]) == (cron.utc(due + 2 * DAY), None)
        chained = await audited(h, "licence.state", "schedule.missed", "schedule.fire")
        assert [(found["actor"], found["action"]) for found in chained[2:]] == [
            ("system:worker", "licence.state"),
            ("system:worker", "schedule.missed"),  # the range the older build held: the open build's row
            ("system:worker", "schedule.fire"),
        ]  # after the older build's two, and no other miss for a licence
        assert (chained[2]["detail"]["from"], chained[2]["detail"]["to"]) == ("evaluation", "open")
        assert chained[3]["detail"] == held
        after = schedules.item(await shown(h, row["schedule_id"]))
        assert after["held"] is None and after["last"]["text"] == f"started run {run['run_id']} at its time"
        (warned,) = (await deliveries(h))[sent:]
        assert (warned["event"], warned["severity"]) == ("schedule.missed", "warning")
        assert (
            f"did not start a run for 1 more time since {cron.utc(due + DAY)}, for the same reason: the"
            " licence refused the run (pairs)."
        ) in json.loads(warned["body"])["text"]
