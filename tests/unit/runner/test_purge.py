"""Retention and purge (docs/mvp/RETENTION.md §4, §5, §10): due cycles by class and days, the worker's
grace, holds, the dry run, erasures and their refusals, a live soak's last ended cycle, abandoned runs,
the actors, the blob GC (the 24 h delay, shared blobs, a late writer, the writer-drain race, orphans), a
crash after a chunk, and the purge lock."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from deltasleuth.auth.principal import WORKER
from deltasleuth.core.types import Observation, Side
from deltasleuth.reports.data import load_run
from deltasleuth.runner import compare, create_run, cycle, pause_run, purge, run_worker
from deltasleuth.runner.compare import LiveReader, evaluate, save_passes
from deltasleuth.runner.purge import CyclePurged, PurgeBusy
from deltasleuth.runner.runs import stop_run
from deltasleuth.runner.worker import PURGE_EVERY_S, _Worker
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.conftest import create_run as queued_run

from .conftest import Harness, ScriptedService, doc, spec_text

pytestmark = pytest.mark.db
KEYS = ["K001", "K002", "K003", "K004"]
SOAK = "concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 50}"
TABLES = ("run_keys", "observations", "results", "findings", "root_causes", "scans")
LOST = "K-" + "0" * 16  # a KEK no keyring holds
LOCAL = {"asserted": True, "os_account": "tester"}  # what every row by the tester's local principal adds


async def rows(h: Harness, query: str, *params: Any) -> list[dict[str, Any]]:
    async with h.ctx.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query.encode(), params)
        return await cur.fetchall() if cur.description else []


async def counts(h: Harness, run_id: str, cycle_no: int | None = None) -> dict[str, int]:
    """The run's rows in each table a purge deletes from (of one cycle, if given)."""
    found = {}
    for table in TABLES:
        [row] = await rows(
            h,
            f"SELECT count(*) AS n FROM ds.{table} WHERE run_id = %s AND (%s::int IS NULL OR cycle = %s)",  # noqa: S608
            run_id, cycle_no, cycle_no,
        )  # fmt: skip
        found[table] = row["n"]
    return found


async def aged(h: Harness, run_id: str, days: float, cycle_no: int | None = None) -> None:
    """Move the ended cycles of the run (or one of them) ``days`` into the past."""
    await rows(
        h,
        """UPDATE ds.cycles SET ended_at = now() - %s * interval '1 day'
           WHERE run_id = %s AND (%s::int IS NULL OR cycle = %s)""",
        days, run_id, cycle_no, cycle_no,
    )  # fmt: skip


async def queue_aged(h: Harness, hours: float) -> None:
    await rows(h, "UPDATE ds.blob_gc SET queued_at = now() - %s * interval '1 hour'", hours)


async def audited(h: Harness, action: str) -> list[dict[str, Any]]:
    return await rows(
        h, "SELECT actor, object, detail FROM ds.audit_log WHERE action = %s ORDER BY id", action
    )


async def blobs(h: Harness, run_id: str) -> set[str]:
    found = await rows(h, "SELECT DISTINCT content_hash FROM ds.observations WHERE run_id = %s", run_id)
    return {row["content_hash"] for row in found}


def blob_path(h: Harness, address: str) -> Path:
    digest = address.removeprefix("hk1:")
    return h.ctx.settings.evidence_dir / "blobs" / "hk1" / digest[:2] / f"{digest}.dse"


async def finished(h: Harness, service: ScriptedService, keys: list[str], extra: str = "") -> str:
    """A completed batch run of ``keys``, each differing in its status (findings)."""
    for key in keys:
        service.both(key, doc(key), doc(key, status="NEW"))
    run_id = await create_run(h.ctx, await h.spec(spec_text(keys, extra=extra)), "batch", who=TESTER)
    assert (await h.finish(run_id))["status"] == "completed"
    return run_id


async def live_soak(h: Harness, service: ScriptedService, keys: list[str]) -> repo.Row:
    """A soak paused after at least two cycles ended: live, its last ended cycle still read."""
    for key in keys:
        service.both(key, doc(key), doc(key))
    run_id = await create_run(h.ctx, await h.spec(spec_text(keys, execution=SOAK)), "soak", who=TESTER)
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
    try:
        while (await h.run(run_id))["cycle"] < 3:
            await asyncio.sleep(0.02)
        await pause_run(h.ctx, run_id, who=TESTER)
    finally:
        stop.set()
        await worker
    return await h.run(run_id)


async def until_waiting(h: Harness) -> None:
    """Until a transaction waits for an advisory lock (the blob lock)."""
    for _ in range(500):
        [row] = await rows(
            h, "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        )
        if row["n"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no transaction waits for the blob lock")


# -- what is due --------------------------------------------------------------------------------------


async def test_cycles_are_due_by_class_and_days(harness: Harness, service: ScriptedService) -> None:
    standard = await finished(harness, service, KEYS[:2])
    audit = await finished(harness, service, KEYS[:2], extra="retention: audit\n")
    [tombstone] = await rows(harness, "SELECT counters FROM ds.cycles WHERE run_id = %s", standard)
    assert tombstone["counters"] == (await harness.run(standard))["counters"]  # the ended cycle's own
    await aged(harness, standard, 89)
    await aged(harness, audit, 364)
    assert (await purge.purge(harness.ctx, TESTER)).cycles == 0
    before = await counts(harness, standard)
    queued = await blobs(harness, standard)
    await aged(harness, standard, 91)
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.cycles, done.runs, done.keys, done.blobs_queued) == (1, {standard}, 2, len(queued))
    assert await counts(harness, standard) == dict.fromkeys(TABLES, 0)
    assert (await counts(harness, audit))["results"] == 2  # a year, for the audit class
    [ended] = await rows(harness, "SELECT * FROM ds.cycles WHERE run_id = %s", standard)
    assert (ended["purge_reason"], ended["purged_by"]) == ("retention standard", TESTER.actor)
    assert ended["purging_at"] <= ended["purged_at"]
    assert await rows(harness, "SELECT scope FROM ds.data_keys WHERE scope = %s", standard) == []
    assert {row["content_hash"] for row in await rows(harness, "SELECT * FROM ds.blob_gc")} == queued
    [expire] = await audited(harness, "retention.expire")
    assert expire == {
        "actor": TESTER.actor,
        "object": standard,
        "detail": {
            "cycle": 1,
            "reason": "retention standard",
            "retention": "standard",
            "ended_at": ended["ended_at"].isoformat(),
        }
        | LOCAL,
    }
    [purged] = await audited(harness, "retention.purged")
    assert (
        purged["detail"]
        == {
            "cycle": 1,
            "keys": before["run_keys"],
            "observations": before["observations"],
            "results": before["results"],
            "findings": 2,
            "chat_messages": 0,
            "blobs_queued": len(queued),
            "data_key_destroyed": True,
            "packs": [],
        }
        | LOCAL
    )
    [finished_row] = await audited(harness, "retention.finished")
    assert (finished_row["object"], finished_row["detail"]) == ("purge", LOCAL | {
        "cycles": 1, "runs": 1, "blobs_deleted": 0, "blobs_shared": 0, "bytes": 0, "orphans": 0,
        "held": 0, "abandoned": 0, "prechecks": 0,
    })  # fmt: skip
    with pytest.raises(CyclePurged) as gone:
        await load_run(harness.ctx, standard)
    assert str(gone.value) == (
        f"cycle 1 of run {standard} was purged on {ended['purging_at']:%Y-%m-%d} (retention standard): its"
        " keys, results, findings and evidence are deleted"
    )
    await aged(harness, audit, 366)
    assert (await purge.purge(harness.ctx, TESTER)).runs == {audit}


async def test_the_worker_purges_hourly_once_seven_days_passed_since_006(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await finished(harness, service, KEYS[:1])
    await aged(harness, run_id, 91)
    worker = _Worker(harness.ctx)
    await worker._sweep_once()
    assert worker.purging is not None
    await worker.purging  # within the grace: nothing
    assert (await counts(harness, run_id))["results"] == 1 and worker.swept_at < 0
    await rows(
        harness, "UPDATE ds.schema_version SET applied_at = now() - interval '8 days' WHERE version = 6"
    )
    await worker._sweep_once()
    assert worker.purging is None  # the hour has not passed
    worker.purged_at -= PURGE_EVERY_S
    await worker._sweep_once()
    assert worker.purging is not None
    await worker.purging
    assert (await counts(harness, run_id))["results"] == 0 and worker.swept_at > 0  # orphans swept too
    await worker.close()
    actors = {
        row["actor"]
        for action in ("expire", "purged", "finished")
        for row in await audited(harness, f"retention.{action}")
    }
    assert actors == {"system:worker"}


async def test_a_held_run_is_kept_past_its_retention_until_released(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await finished(harness, service, KEYS[:1])
    await aged(harness, run_id, 91)
    await purge.hold(harness.ctx, TESTER, run_id, "legal hold LH-7")
    with pytest.raises(ValueError, match=f"^run {run_id} is already held$"):
        await purge.hold(harness.ctx, TESTER, run_id, "again")
    held = await harness.run(run_id)
    assert (held["hold_reason"], held["hold_by"]) == ("legal hold LH-7", TESTER.actor)
    [kept] = (await purge.preview(harness.ctx))["kept"]
    assert (kept["run_id"], kept["hold_reason"]) == (run_id, "legal hold LH-7")
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.cycles, done.held) == (0, 1) and (await counts(harness, run_id))["results"] == 1
    assert await audited(harness, "retention.finished") == []  # a hold alone does nothing to record
    await purge.hold(harness.ctx, TESTER, run_id, None)
    with pytest.raises(ValueError, match=f"^run {run_id} is not held$"):
        await purge.hold(harness.ctx, TESTER, run_id, None)
    released = await harness.run(run_id)
    assert (released["hold_reason"], released["hold_by"], released["hold_at"]) == (None, None, None)
    assert (await purge.purge(harness.ctx, TESTER)).cycles == 1
    trail = [(row["actor"], row["detail"]["reason"]) for row in await audited(harness, "retention.hold")]
    trail += [(row["actor"], row["detail"]["reason"]) for row in await audited(harness, "retention.release")]
    assert trail == [(TESTER.actor, "legal hold LH-7")] * 2
    with pytest.raises(LookupError, match=r"^run R-0 not found$"):
        await purge.hold(harness.ctx, TESTER, "R-0", "x")


async def test_a_hold_placed_after_the_pass_read_its_due_cycles_keeps_them(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await finished(harness, service, KEYS[:1])
    await aged(harness, run_id, 91)
    due_cycles = repo.due_cycles

    async def then_held(*args: Any, **kwargs: Any) -> list[repo.Row]:
        due = await due_cycles(*args, **kwargs)
        await purge.hold(harness.ctx, TESTER, run_id, "legal hold LH-7")  # while the pass runs
        return due

    monkeypatch.setattr(repo, "due_cycles", then_held)
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.cycles, done.held) == (0, 1) and (await counts(harness, run_id))["results"] == 1
    [kept] = await rows(harness, "SELECT purging_at, purged_at FROM ds.cycles WHERE run_id = %s", run_id)
    assert kept == {"purging_at": None, "purged_at": None}
    assert await audited(harness, "retention.expire") == []


async def test_a_dry_run_writes_nothing(harness: Harness, service: ScriptedService) -> None:
    run_id = await finished(harness, service, KEYS[:1])
    await aged(harness, run_id, 91)
    async with harness.ctx.pool.connection() as conn:
        one_shot, _ = await queued_run(harness.ctx, conn, (await harness.run(run_id))["spec_id"], "one_shot",
                                       {"K009": "K009"}, {})  # fmt: skip
        await conn.execute(
            "UPDATE ds.runs SET updated_at = now() - interval '25 hours' WHERE run_id = %s", (one_shot,)
        )

    async def snapshot() -> list[Any]:
        return [
            await rows(harness, "SELECT * FROM ds.cycles ORDER BY run_id, cycle"),
            await rows(harness, "SELECT run_id, status, updated_at FROM ds.runs ORDER BY run_id"),
            await rows(harness, "SELECT count(*) FROM ds.audit_log"),
            await rows(harness, "SELECT count(*) FROM ds.run_keys"),
            await rows(harness, "SELECT * FROM ds.blob_gc"),
        ]

    before = await snapshot()
    plan = await purge.preview(harness.ctx)
    assert [r["run_id"] for r in plan["due"]] == [run_id] and plan["kept"] == []
    assert [(r["run_id"], r["mode"], r["status"]) for r in plan["abandon"]] == [
        (one_shot, "one_shot", "queued")
    ]
    assert await snapshot() == before


# -- erasure ------------------------------------------------------------------------------------------


async def test_erasures_are_refused_without_writing_a_row(harness: Harness, service: ScriptedService) -> None:
    soak = await live_soak(harness, service, KEYS[:1])
    run_id, current = soak["run_id"], soak["cycle"]

    async def refused(message: str, cycle_no: int | None, target: str = run_id) -> None:
        with pytest.raises(ValueError) as refusal:
            await purge.erase(harness.ctx, TESTER, target, "a customer's request", cycle_no)
        assert str(refusal.value) == message

    await refused(f"cycle {current} of run {run_id} has not ended", current)
    await refused(f"cycle {current + 1} of run {run_id} has not ended", current + 1)
    await refused(
        f"cycle {current - 1} of run {run_id} is still read by the running soak: erase the whole run",
        current - 1,
    )
    await purge.erase(harness.ctx, TESTER, run_id, "a customer's request", 1)
    [first] = await rows(harness, "SELECT purging_at FROM ds.cycles WHERE run_id = %s AND cycle = 1", run_id)
    await refused(f"cycle 1 of run {run_id} was purged on {first['purging_at']:%Y-%m-%d}", 1)
    await purge.hold(harness.ctx, TESTER, run_id, "investigation")
    await refused(f"run {run_id} is held", None)
    await refused(f"run {run_id} is held", 2)
    batch = await finished(harness, service, KEYS[1:2])
    await rows(harness, """UPDATE ds.run_keys SET state = 'leased', lease_owner = 'slow',
                           lease_until = now() + interval '1 minute' WHERE run_id = %s""", batch)  # fmt: skip
    await refused(f"run {batch} still has keys in flight; try again in 60 s", 1, batch)
    await refused(f"run {batch} still has keys in flight; try again in 60 s", None, batch)
    with pytest.raises(LookupError, match=r"^run R-0 not found$"):
        await purge.erase(harness.ctx, TESTER, "R-0", "x")
    expired = [row["detail"]["cycle"] for row in await audited(harness, "retention.expire")]
    assert expired == [1]  # the one erasure that was not refused
    assert (await harness.run(batch))["status"] == "completed"


async def test_a_refused_erasure_leaves_a_live_run_untouched(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await create_run(harness.ctx, await harness.spec(spec_text(KEYS[:2])), "batch", who=TESTER)
    await rows(
        harness,
        """UPDATE ds.run_keys SET state = 'leased', lease_owner = 'slow',
               lease_until = now() + interval '1 minute' WHERE run_id = %s AND seq = 0""",
        run_id,
    )
    with pytest.raises(ValueError, match=f"^run {run_id} still has keys in flight; try again in 60 s$"):
        await purge.erase(harness.ctx, TESTER, run_id, "a customer's request")
    run = await harness.run(run_id)
    states = await rows(harness, "SELECT state FROM ds.run_keys WHERE run_id = %s ORDER BY seq", run_id)
    assert (run["status"], run["stop_reason"]) == ("running", None)
    assert [row["state"] for row in states] == ["leased", "pending"]
    assert await audited(harness, "cancelling") == [] and await rows(harness, "SELECT * FROM ds.cycles") == []


async def test_an_erased_run_s_cycle_ends_cancelled_without_its_key(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await create_run(harness.ctx, await harness.spec(spec_text(KEYS[:2])), "batch", who=TESTER)
    await stop_run(harness.ctx, run_id, purge.ERASURE, who=TESTER)  # then refused: a key was in flight
    await rows(harness, "UPDATE ds.data_keys SET kek = %s WHERE scope = %s", LOST, run_id)
    harness.ctx.forget(run_id)  # its KEK is lost: the erasure's reason is "key lost: …"

    async def unread(*_: Any) -> None:
        raise AssertionError("an erased run is not summarised")

    monkeypatch.setattr(cycle, "summarise", unread)
    await cycle.finish_cycle(harness.ctx, run_id, who=WORKER)  # the worker's sweep, once the key finished
    run = await harness.run(run_id)
    assert (run["status"], run["stop_reason"]) == ("cancelled", "erasure")
    [finished_row] = await audited(harness, "run.finished")
    assert finished_row["detail"] == {"status": "cancelled", "reason": "erasure"}
    assert len(await rows(harness, "SELECT * FROM ds.cycles WHERE run_id = %s", run_id)) == 1
    assert (await purge.erase(harness.ctx, TESTER, run_id, "key lost: vault copy destroyed")).cycles == 1


async def test_a_live_soak_keeps_its_last_ended_cycle(harness: Harness, service: ScriptedService) -> None:
    soak = await live_soak(harness, service, KEYS[:1])
    run_id, current = soak["run_id"], soak["cycle"]
    await aged(harness, run_id, 91)
    done = await purge.purge(harness.ctx, TESTER)
    assert done.cycles == current - 2  # every ended cycle but the last, which the next scan reads
    ended = await rows(
        harness, "SELECT cycle, purged_at FROM ds.cycles WHERE run_id = %s ORDER BY cycle", run_id
    )
    assert [row["cycle"] for row in ended if row["purged_at"] is None] == [current - 1]
    [kept] = (await purge.preview(harness.ctx))["kept"]
    assert (kept["run_id"], kept["cycle"], kept["hold_reason"]) == (run_id, current - 1, None)
    assert (await counts(harness, run_id, current - 1))["results"] == 1  # outcome_changes compares with it
    assert (await harness.run(run_id))["status"] == "paused"  # expiry never fails a soak


async def test_erasing_the_last_cycle_of_a_failed_soak_leaves_the_earlier_ones_readable(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in KEYS[:2]:
        service.both(key, doc(key), doc(key, status="NEW"))
    spec_id = await harness.spec(spec_text(KEYS[:2], execution=SOAK))
    run_id = await create_run(harness.ctx, spec_id, "soak", who=TESTER)
    async with harness.ctx.pool.connection() as conn:
        session = await repo.create_chat_session(conn, run_id, created_by=TESTER.actor)
    summarise = cycle.summarise

    async def asked_then_summarised(*args: Any) -> dict[str, Any]:
        ctx, run = args[0], args[3]
        async with ctx.pool.connection() as conn:  # a question in each cycle
            payload = {"content": f"why, in cycle {run['cycle']}?", "tool_calls": []}
            await repo.append_chat_message(conn, session, "user", payload, await ctx.sealer(conn, run_id))
        if run["cycle"] == 3:
            raise RuntimeError("a bug")  # the soak fails, and its cycle 3 ends with it
        return await summarise(*args)

    monkeypatch.setattr(cycle, "summarise", asked_then_summarised)
    failed = await harness.finish(run_id)
    assert (failed["status"], failed["cycle"], failed["error"]) == ("failed", 3, "RuntimeError")
    tombstones = await rows(
        harness, "SELECT cycle, counters FROM ds.cycles WHERE run_id = %s ORDER BY cycle", run_id
    )
    assert [(row["cycle"], row["counters"].get("cycle")) for row in tombstones] == [(1, 1), (2, 2), (3, None)]
    done = await purge.erase(harness.ctx, TESTER, run_id, "DSAR-12", 3)
    assert (done.cycles, done.keys) == (1, 2)
    assert await counts(harness, run_id, 3) == dict.fromkeys(TABLES, 0)
    for number in (1, 2):
        assert (await counts(harness, run_id, number))["results"] == 2
        assert len((await load_run(harness.ctx, run_id, number)).results) == 2
    with pytest.raises(CyclePurged, match=r"^cycle 3 of run \S+ was purged on \S+ \(erasure\): its keys"):
        await load_run(harness.ctx, run_id, 3)
    async with harness.ctx.pool.connection() as conn:
        chat = await repo.get_chat_session(conn, session)
        sealer = await harness.ctx.sealer(conn, run_id)
    assert [sealer.open_row(m)["content"] for m in chat["messages"]] == [
        "why, in cycle 1?",
        "why, in cycle 2?",
    ]
    [purged] = await audited(harness, "retention.purged")
    assert (purged["detail"]["chat_messages"], purged["detail"]["data_key_destroyed"]) == (1, False)
    done = await purge.erase(harness.ctx, TESTER, run_id, "DSAR-12")
    assert (done.cycles, done.runs) == (2, {run_id})
    assert await rows(harness, "SELECT * FROM ds.chat_sessions WHERE run_id = %s", run_id) == []
    assert await rows(harness, "SELECT * FROM ds.data_keys WHERE scope = %s", run_id) == []
    reasons = {row["detail"]["reason"] for row in await audited(harness, "retention.expire")}
    assert reasons == {"erasure: DSAR-12"}
    assert [row["detail"]["data_key_destroyed"] for row in await audited(harness, "retention.purged")] == [
        False, False, True,
    ]  # fmt: skip


async def test_erasing_a_live_run_cancels_it_first(harness: Harness, service: ScriptedService) -> None:
    soak = await live_soak(harness, service, KEYS[:1])
    run_id = soak["run_id"]
    done = await purge.erase(harness.ctx, TESTER, run_id, "a customer's request")
    assert done.cycles == soak["cycle"]  # the live cycle ended, then every cycle went
    run = await harness.run(run_id)
    assert (run["status"], run["stop_reason"]) == ("cancelled", "erasure")
    async with harness.ctx.pool.connection() as conn:
        trail, _ = await repo.list_audit(conn, obj=run_id)
    actions = [row["action"] for row in reversed(trail)]
    assert actions[actions.index("cancelling") :][:2] == ["cancelling", "run.finished"]


# -- abandoned runs -----------------------------------------------------------------------------------


async def test_abandoned_runs_are_ended_and_a_cancelled_one_is_due_at_once(
    harness: Harness, service: ScriptedService
) -> None:
    spec_id = await harness.spec(spec_text(["K001"]))
    async with harness.ctx.pool.connection() as conn:
        one_shot, _ = await queued_run(harness.ctx, conn, spec_id, "one_shot", {"K001": "K001"}, {})
        await conn.execute(
            "UPDATE ds.runs SET updated_at = now() - interval '25 hours' WHERE run_id = %s", (one_shot,)
        )
    paused, recent = [await create_run(harness.ctx, spec_id, "batch", who=TESTER) for _ in range(2)]
    for run_id in (paused, recent):
        await pause_run(harness.ctx, run_id, who=TESTER)
    [since] = await rows(
        harness,
        "UPDATE ds.runs SET updated_at = now() - interval '91 days' WHERE run_id = %s RETURNING updated_at",
        paused,
    )
    done = await purge.purge(harness.ctx, TESTER)
    assert done.abandoned == 2 and done.runs == {paused}  # the cancelled run's cycle ended long ago
    failed, cancelled = await harness.run(one_shot), await harness.run(paused)
    assert (failed["status"], failed["error"]) == ("failed", "AbandonedRun")
    assert (cancelled["status"], cancelled["stop_reason"]) == ("cancelled", "abandoned")
    assert (await harness.run(recent))["status"] == "paused"
    [ended] = await rows(harness, "SELECT ended_at, purged_at FROM ds.cycles WHERE run_id = %s", paused)
    assert ended["ended_at"] == since["updated_at"] and ended["purged_at"] is not None
    [kept] = await rows(harness, "SELECT purged_at FROM ds.cycles WHERE run_id = %s", one_shot)
    assert kept["purged_at"] is None  # failed now: its retention starts now
    abandoned = {row["object"]: row["detail"] for row in await audited(harness, "retention.abandoned")}
    assert {run_id: (d["status"], d["retention"], d["days"]) for run_id, d in abandoned.items()} == {
        one_shot: ("queued", "standard", 90),
        paused: ("paused", "standard", 90),
    }
    assert abandoned[paused]["since"] == since["updated_at"].isoformat()
    async with harness.ctx.pool.connection() as conn:
        trail, _ = await repo.list_audit(conn, obj=paused)
    actions = [row["action"] for row in reversed(trail)]
    assert actions[actions.index("cancelling") :][:3] == ["cancelling", "run.finished", "retention.abandoned"]


async def test_a_run_paused_for_a_missing_kek_past_its_retention_is_cancelled_and_purged(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await finished(harness, service, KEYS[:2])  # its keys differ: a summary would need the key
    [since] = await rows(
        harness,
        """UPDATE ds.runs SET status = 'paused', stop_reason = 'run_paused_key_missing', finished_at = NULL,
               updated_at = now() - interval '91 days' WHERE run_id = %s RETURNING updated_at""",
        run_id,
    )  # paused before its cycle ended, and never resumed
    await rows(harness, "DELETE FROM ds.cycles WHERE run_id = %s", run_id)
    await rows(harness, "UPDATE ds.data_keys SET kek = %s WHERE scope = %s", LOST, run_id)
    harness.ctx.forget(run_id)
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.abandoned, done.cycles, done.runs) == (1, 1, {run_id})
    run = await harness.run(run_id)
    assert (run["status"], run["stop_reason"]) == ("cancelled", "abandoned")
    [ended] = await rows(harness, "SELECT ended_at, purged_at FROM ds.cycles WHERE run_id = %s", run_id)
    assert ended["ended_at"] == since["updated_at"] and ended["purged_at"] is not None
    assert await counts(harness, run_id) == dict.fromkeys(TABLES, 0)


async def test_a_racing_sweep_keeps_the_abandoned_date_and_a_failing_run_stops_no_pass(
    harness: Harness,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec_id = await harness.spec(spec_text(["K001"]))
    broken, paused = [await create_run(harness.ctx, spec_id, "batch", who=TESTER) for _ in range(2)]
    for run_id in (broken, paused):
        await pause_run(harness.ctx, run_id, who=TESTER)
    [since] = await rows(
        harness,
        "UPDATE ds.runs SET updated_at = now() - interval '91 days' WHERE run_id = %s RETURNING updated_at",
        paused,
    )
    await rows(
        harness, "UPDATE ds.runs SET updated_at = now() - interval '92 days' WHERE run_id = %s", broken
    )
    abandoned_runs, end_cycle = repo.abandoned_runs, repo.end_cycle

    async def then_swept(*args: Any, **kwargs: Any) -> list[repo.Row]:
        found = await abandoned_runs(*args, **kwargs)
        for row in found:  # the worker's sweep, between the pass's read and its move
            await cycle.finish_cycle(harness.ctx, row["run_id"], who=WORKER)
        return found

    async def failing(conn: Any, run_id: str, ended_at: Any = None, sample: Any = None) -> None:
        if run_id == broken:
            raise RuntimeError("a bug")
        await end_cycle(conn, run_id, ended_at, sample)

    monkeypatch.setattr(repo, "abandoned_runs", then_swept)
    monkeypatch.setattr(repo, "end_cycle", failing)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.purge"):
        done = await purge.purge(harness.ctx, TESTER)
    await cycle.finish_cycle(harness.ctx, paused, who=WORKER)  # and after it
    assert (done.abandoned, done.runs) == (1, {paused})
    [ended] = await rows(harness, "SELECT ended_at, purged_at FROM ds.cycles WHERE run_id = %s", paused)
    assert ended["ended_at"] == since["updated_at"] and ended["purged_at"] is not None
    assert (await harness.run(broken))["status"] == "paused"  # rolled back: the next pass tries again
    assert f"run {broken} not abandoned: RuntimeError" in caplog.messages


# -- blobs --------------------------------------------------------------------------------------------


async def test_a_shared_blob_goes_24_h_after_its_last_reference_and_requeuing_restarts_the_delay(
    harness: Harness, service: ScriptedService
) -> None:
    first, second, third = [await finished(harness, service, KEYS[:1]) for _ in range(3)]
    shared = await blobs(harness, first)
    assert shared == await blobs(harness, second) == await blobs(harness, third)  # the same bodies
    await purge.erase(harness.ctx, TESTER, first, "test")
    await queue_aged(harness, 23)
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.blobs_deleted, done.blobs_shared) == (0, 0)  # not yet 24 h
    await queue_aged(harness, 25)
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.blobs_deleted, done.blobs_shared) == (0, len(shared))  # the second and third name them
    assert await rows(harness, "SELECT * FROM ds.blob_gc") == []
    await purge.erase(harness.ctx, TESTER, second, "test")
    await queue_aged(harness, 25)
    await purge.erase(harness.ctx, TESTER, third, "test")  # queued again: the delay restarts
    queued = await rows(harness, "SELECT queued_at > now() - interval '1 minute' AS fresh FROM ds.blob_gc")
    assert [row["fresh"] for row in queued] == [True] * len(shared)
    assert (await purge.purge(harness.ctx, TESTER)).blobs_deleted == 0
    assert all(blob_path(harness, address).exists() for address in shared)
    await queue_aged(harness, 25)
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.blobs_deleted, done.blobs_shared) == (len(shared), 0) and done.bytes > 0
    assert not any(blob_path(harness, address).exists() for address in shared)
    assert await rows(harness, "SELECT * FROM ds.blob_gc") == []


async def _pass(h: Harness, run_id: str, key: str) -> tuple[Any, Any]:
    """One pass over ``key`` of the run, read and put as a worker does, not yet saved."""
    run = await h.run(run_id)
    async with h.ctx.pool.connection() as conn:
        spec = await h.ctx.spec(conn, run["spec_id"], live=True)
    reader = LiveReader(h.ctx, spec, run["params"].get("tables"))
    try:
        result, reads = await evaluate(
            h.ctx, spec, h.ctx.capabilities(run), reader, key, run_id=run_id, pass_no=1
        )
    finally:
        await reader.aclose()
    return spec, (result, reads)


async def _save(h: Harness, run_id: str, spec: Any, passed: tuple[Any, Any], pass_no: int = 1) -> None:
    result, reads = passed
    async with h.ctx.pool.connection() as conn:
        sealer = await h.ctx.sealer(conn, run_id)
        await save_passes(h.ctx, conn, spec, run_id, 1, [(pass_no, result, reads)], sealer)


async def test_a_late_writer_s_observation_is_deleted_and_its_blob_queued(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await finished(harness, service, KEYS[:2])
    await aged(harness, run_id, 91)
    service.both("K001", doc("K001", name="Late Writer"), doc("K001", name="Late Writer"))
    spec, late = await _pass(harness, run_id, "K001")
    written = {obs.content_hash for obs in late[1].values()}
    assert not written & await blobs(harness, run_id)
    key_span = repo.key_span

    async def slow_writer(conn: repo.Conn, run: str, number: int) -> Any:
        await _save(harness, run_id, spec, late, pass_no=3)  # after the purge started, before its chunks
        return await key_span(conn, run, number)

    monkeypatch.setattr(repo, "key_span", slow_writer)
    await purge.purge(harness.ctx, TESTER)
    assert await counts(harness, run_id) == dict.fromkeys(TABLES, 0)
    assert written <= {row["content_hash"] for row in await rows(harness, "SELECT * FROM ds.blob_gc")}


async def test_a_crash_after_a_chunk_leaves_the_cycle_purging_and_the_next_pass_resumes_it_first(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await finished(harness, service, KEYS)
    await aged(harness, run_id, 100)
    monkeypatch.setattr(purge, "CHUNK", 1)  # a key per chunk
    purge_keys, chunks = repo.purge_keys, []

    async def crashing(*args: Any) -> dict[str, int]:
        if len(chunks) == 2:
            raise RuntimeError("the process was killed")
        chunks.append(await purge_keys(*args))
        return chunks[-1]

    monkeypatch.setattr(repo, "purge_keys", crashing)
    with pytest.raises(RuntimeError, match="killed"):
        await purge.purge(harness.ctx, TESTER)
    [cut] = await rows(harness, "SELECT * FROM ds.cycles WHERE run_id = %s", run_id)
    assert cut["purging_at"] is not None and cut["purged_at"] is None
    assert (await counts(harness, run_id))["run_keys"] == 2  # two chunks of four committed
    with pytest.raises(CyclePurged):  # its reads answer 410 already
        await load_run(harness.ctx, run_id)
    monkeypatch.setattr(repo, "purge_keys", purge_keys)
    newer = await finished(harness, service, KEYS[:1])
    await aged(harness, newer, 300)  # due, and ended earliest: after the interrupted one all the same
    done = await purge.purge(harness.ctx, TESTER, limit=1)
    assert (done.runs, done.keys) == ({run_id}, 2)
    assert await counts(harness, run_id) == dict.fromkeys(TABLES, 0)
    expires = [
        row["detail"]["reason"]
        for row in await audited(harness, "retention.expire")
        if row["object"] == run_id
    ]
    purged = [
        row["detail"]["keys"] for row in await audited(harness, "retention.purged") if row["object"] == run_id
    ]
    assert (expires, purged) == (["retention standard"] * 2, [2])  # this invocation's keys
    [done_row] = await rows(harness, "SELECT purging_at, purged_at FROM ds.cycles WHERE run_id = %s", run_id)
    assert done_row["purging_at"] == cut["purging_at"] and done_row["purged_at"] is not None


@pytest.mark.parametrize("first", ["drain", "writer"])
async def test_a_writer_racing_a_drain_keeps_its_blobs(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    erased = await finished(harness, service, KEYS[:1])
    await purge.erase(harness.ctx, TESTER, erased, "test")
    await queue_aged(harness, 25)  # its blobs are unreferenced and due
    run_id = await create_run(
        harness.ctx, (await harness.run(erased))["spec_id"], "batch", keys=KEYS[:1], who=TESTER
    )
    spec, passed = await _pass(harness, run_id, "K001")  # the same bodies: the files exist
    addresses = {obs.content_hash for obs in passed[1].values()}
    assert all(blob_path(harness, address).exists() for address in addresses)
    racing: list[asyncio.Task[Any]] = []
    if first == "drain":  # it deletes the files, then a writer waits for its lock
        gc_done = repo.gc_done

        async def writer_waits(conn: repo.Conn, done: Any) -> None:
            if done and not racing:
                assert not any(blob_path(harness, address).exists() for address in addresses)
                racing.append(asyncio.create_task(_save(harness, run_id, spec, passed)))
                await until_waiting(harness)
            await gc_done(conn, done)

        monkeypatch.setattr(repo, "gc_done", writer_waits)
        drained = await purge.purge(harness.ctx, TESTER)
        await racing[0]
        assert drained.blobs_deleted == len(addresses)
    else:  # its rows are in, uncommitted, when a drain comes
        insert = repo.insert_observations

        async def drain_waits(conn: repo.Conn, *args: Any) -> None:
            await insert(conn, *args)
            racing.append(asyncio.create_task(purge.purge(harness.ctx, TESTER)))
            await until_waiting(harness)

        monkeypatch.setattr(repo, "insert_observations", drain_waits)
        await _save(harness, run_id, spec, passed)
        drained = await racing[0]
        assert (drained.blobs_deleted, drained.blobs_shared) == (0, len(addresses))
    assert await blobs(harness, run_id) == addresses
    for address in addresses:  # ensure rewrote them before the insert, or the drain kept them
        assert harness.ctx.evidence.get_body(address)
    assert await rows(harness, "SELECT * FROM ds.blob_gc") == []


async def test_orphans_are_deleted_an_hour_on_unless_named_or_queued(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await finished(harness, service, KEYS[:1])
    evidence = harness.ctx.evidence
    named = sorted(await blobs(harness, run_id))
    orphan, fresh, queued = (evidence.put_body(body) for body in (b'{"o": 1}', b'{"f": 1}', b'{"q": 1}'))
    await rows(harness, "INSERT INTO ds.blob_gc (content_hash) VALUES (%s)", queued)
    directory = blob_path(harness, orphan).parent
    stale, writing = directory / f".{orphan[4:]}.dse.abc.tmp", directory / ".x.dse.def.tmp"
    for path in (stale, writing):
        path.write_bytes(b"partial")
    hours_ago = time.time() - 2 * 3600
    for path in (*(blob_path(harness, a) for a in (orphan, queued, *named)), stale):
        os.utime(path, (hours_ago, hours_ago))
    done = await purge.purge(harness.ctx, TESTER, orphans=True)
    assert done.orphans == 2
    assert not blob_path(harness, orphan).exists() and not stale.exists()
    assert all(blob_path(harness, a).exists() for a in (fresh, queued, *named)) and writing.exists()
    [row] = await audited(harness, "retention.finished")
    assert row["detail"]["orphans"] == 2
    assert (await purge.purge(harness.ctx, TESTER)).orphans == 0  # weekly in the worker; on demand here


# -- the purge lock -----------------------------------------------------------------------------------


async def test_the_purge_lock_is_freed_after_a_failed_pass(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await finished(harness, service, KEYS[:1])
    await aged(harness, run_id, 91)

    async def broken(*_: Any, **__: Any) -> list[repo.Row]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(repo, "due_cycles", broken)
    with pytest.raises(RuntimeError, match="a bug"):
        await purge.purge(harness.ctx, TESTER)
    monkeypatch.undo()
    async with await psycopg.AsyncConnection.connect(harness.ctx.pool.conninfo, autocommit=True) as other:
        taken = await (await other.execute("SELECT pg_try_advisory_lock(%s)", (purge.LOCK_ID,))).fetchone()
        assert taken == (True,)  # freed with the failed pass's connection
        for attempt in (purge.purge(harness.ctx, TESTER), purge.erase(harness.ctx, TESTER, run_id, "x")):
            with pytest.raises(PurgeBusy, match=r"^another purge is running; try again later$"):
                await attempt
    assert (await purge.purge(harness.ctx, TESTER)).runs == {run_id}  # freed with that connection too


# -- reads of a purged cycle, and of a missing blob ---------------------------------------------------


async def test_a_missing_blob_reads_as_missing_evidence(harness: Harness, service: ScriptedService) -> None:
    run_id = await finished(harness, service, KEYS[:1])
    async with harness.ctx.pool.connection() as conn:
        [row] = [r for r in await repo.list_observations(conn, run_id) if r["side"] == Side.A.value][:1]
    blob_path(harness, row["content_hash"]).unlink()  # lost, or deleted by hand
    obs = Observation("K001", Side.A, 200, None, "2026-01-01T00:00:00Z", obs_id=row["obs_id"],
                      content_hash=row["content_hash"])  # fmt: skip
    with pytest.raises(
        compare.EvidenceMissing, match=rf"^evidence of {row['obs_id']} is missing \(purged or lost\)$"
    ):
        compare.body_of(harness.ctx, obs)


def test_the_summary_counts_runs_once() -> None:
    done = purge.Summary(cycles=3, runs={"R-1", "R-2"}, held=1)
    assert done.finished() == {
        "cycles": 3, "runs": 2, "blobs_deleted": 0, "blobs_shared": 0, "bytes": 0, "orphans": 0, "held": 1,
        "abandoned": 0, "prechecks": 0,
    }  # fmt: skip


async def test_a_pass_that_deletes_only_pre_checks_is_audited(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Table pre-checks go ``retention.standard_days`` after they were made, a batch a transaction, with
    their pairs; their audit rows stay (docs/mvp/PRECHECKS.md §8)."""
    made = {"source": "specs", "level": "schema", "request": {}, "skipped": 0, "evaluation": False}
    pair = {"n": 1, "spec_id": None, "label": "accounts", "target_a": "db-a", "target_b": "db-b"}
    pair |= {"relation_a": "accounts", "relation_b": "accounts"}
    async with repo.audited(harness.ctx.pool) as (conn, audit):
        ids = [repo.new_precheck_id() for _ in range(4)]
        for precheck_id in ids:
            await repo.insert_precheck(
                conn, made | {"precheck_id": precheck_id, "created_by": TESTER.actor}, [pair]
            )
            await audit(TESTER, "precheck.create", precheck_id, {"source": "specs"})
    await rows(
        harness,
        "UPDATE ds.prechecks SET created_at = now() - interval '91 days' WHERE precheck_id <> %s",
        ids[3],
    )
    monkeypatch.setattr(purge, "PRECHECKS", 2)  # three of them take two transactions
    done = await purge.purge(harness.ctx, TESTER)
    assert (done.prechecks, done.cycles) == (3, 0)
    assert await rows(harness, "SELECT precheck_id FROM ds.prechecks") == [{"precheck_id": ids[3]}]
    assert await rows(harness, "SELECT precheck_id FROM ds.precheck_pairs") == [{"precheck_id": ids[3]}]
    [finished] = await audited(harness, "retention.finished")
    assert finished["detail"] == LOCAL | {
        "cycles": 0, "runs": 0, "blobs_deleted": 0, "blobs_shared": 0, "bytes": 0, "orphans": 0, "held": 0,
        "abandoned": 0, "prechecks": 3,
    }  # fmt: skip
    assert len(await audited(harness, "precheck.create")) == 4  # the audit rows stay
    assert (await purge.purge(harness.ctx, TESTER)).prechecks == 0
    assert len(await audited(harness, "retention.finished")) == 1  # a pass that did nothing writes no row


# -- scale (opt-in) -----------------------------------------------------------------------------------

CLONES = {  # how a clone g of each purged table's row differs from the row it copies
    "run_keys": {"key_masked": "key_masked || '-' || g", "seq": f"seq + g * {len(KEYS)}"},  # dense, as runs
    "observations": {
        "key_masked": "key_masked || '-' || g",
        "obs_id": "'O-' || substr(md5(obs_id || g), 1, 16)",
    },
    "results": {
        "key_masked": "key_masked || '-' || g",
        "obs_a_id": "'O-' || substr(md5(obs_a_id || g), 1, 16)",
        "obs_b_id": "'O-' || substr(md5(obs_b_id || g), 1, 16)",
    },
    "findings": {
        "key_masked": "key_masked || '-' || g",
        "finding_id": "'F-' || substr(md5(finding_id || g), 1, 16)",
    },
}


async def _clone(h: Harness, run_id: str, copies: int) -> None:
    """Fill the run's cycle with ``copies`` clones of each key, in foreign-key order."""
    for table, changed in CLONES.items():
        found = await rows(
            h,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'ds' AND table_name = %s"
            " ORDER BY ordinal_position",
            table,
        )
        columns = [row["column_name"] for row in found]
        values = ", ".join(changed.get(c, c) for c in columns)
        await rows(
            h,
            f"INSERT INTO ds.{table} ({', '.join(columns)}) SELECT {values}"  # noqa: S608 (catalogue names)
            f" FROM ds.{table}, generate_series(1, %s) g WHERE run_id = %s",
            copies, run_id,
        )  # fmt: skip


@pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
async def test_a_large_cycle_purges_at_five_thousand_keys_a_second(
    harness: Harness, service: ScriptedService
) -> None:
    run_id = await finished(harness, service, KEYS)
    await _clone(harness, run_id, 50_000)  # 200,004 keys, each with its observations, result and findings
    [row] = await rows(harness, "SELECT count(*) AS n FROM ds.run_keys WHERE run_id = %s", run_id)
    await rows(harness, "ANALYZE")
    await aged(harness, run_id, 91)
    started = time.monotonic()
    done = await purge.purge(harness.ctx, TESTER)
    elapsed = time.monotonic() - started
    print(f"\npurged {done.keys} keys in {elapsed:.1f} s: {done.keys / elapsed:,.0f} keys/s")
    assert done.keys == row["n"] == 200_004
    assert await counts(harness, run_id) == dict.fromkeys(TABLES, 0)
    assert done.keys / elapsed >= 5_000  # the results -> observations FK checks dominate
