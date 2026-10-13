"""Firing schedules (docs/mvp/SCHEDULES.md §3, §5 to §7): a due time starts one run as ``system:worker``,
with the schedule's advance and their audit rows in one transaction, or is settled as missed with a closed
reason; the window, the overlap, missed and late times, a person's write settling what is due, the standing
refusals held and chained as ranges, the tries, the setter and the auth mode, the gate and the licence at
each firing, a sample's ledger, and a cron or zone this build cannot load. The clock is the database's, or
an instant injected into ``fire``."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth import approvals, schedules
from deltasleuth.auth.principal import WORKER, expand
from deltasleuth.config import FilesSettings
from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import duck, manifest
from deltasleuth.context import Context
from deltasleuth.core import cron, notify
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.types import Target
from deltasleuth.runner import create_run, pause_run, purge, runs, schedule
from deltasleuth.runner.schedule import RETRY_S, RETRY_SLOW_S, Retry
from deltasleuth.schedules import Refused
from deltasleuth.store import repo
from tests.conftest import TESTER, licensed, unlicensed
from tests.conftest import create_run as stored_run
from tests.unit.store.four_eyes import person, token

from .conftest import TARGETS, Harness, OpenHarness, ScriptedService, doc, spec_text
from .measuring import measure, measures
from .notifying import deliveries, hook, notifying, receiving, rule
from .sampled import (
    KEYS,
    Tables,
    close,
    drawn,
    listed,
    tables_fixture,  # noqa: F401 (the on_tables fixture)
)
from .scheduling import (
    DAY,
    HOUR,
    MINUTE,
    PLAIN,
    anchored,
    audited,
    daily,
    ended,
    fire,
    hourly,
    minute,
    missed,
    scheduled,
    shown,
    started,
)
from .test_approvals import OIDC, TOKENS, approve, revoke
from .test_files import SERVICE as FILE_SERVICE
from .test_files import spec as file_side_spec
from .test_files import table as file_table
from .test_keys_table import (
    ITEM_KEYS,
    ITEMS,
    Failing,
    engine,  # noqa: F401 (a fixture)
    keyed,
)
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
WORKER_ACTOR, TESTER_ACTOR = WORKER.actor, TESTER.actor
SECOND = timedelta(seconds=1)


def targets(service: ScriptedService) -> str:
    assert service.server is not None
    return TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)


@pytest.fixture
async def sso(open_harness: OpenHarness, service: ScriptedService) -> AsyncIterator[Harness]:
    async with open_harness(targets(service), auth=OIDC) as h:
        yield h


def body(row: repo.Row, **changes: Any) -> dict[str, Any]:
    """The schedule as a ``PUT`` sends it back, with ``changes``."""
    names = ("name", "run", "cron", "time_zone", "start_within", "revision")
    sent = {name: row[name] for name in names}
    sent |= {"spec_id": row["spec_id"]} if row["spec_id"] is not None else {"spec_name": row["spec_name"]}
    return sent | ({"ends_at": row["ends_at"]} if row["ends_at"] else {}) | changes


def reasons(details: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [(d["reason"], d["code"], d["due"], d["to"], d["count"]) for d in details]


async def fillers(h: Harness, count: int) -> None:
    """``count`` schedules stored by SQL, towards the 200 an installation holds."""
    async with h.ctx.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO ds.schedules (schedule_id, name, spec_name, run, cron, time_zone, auth_mode,
                   created_by, set_by)
               SELECT 'SC-' || lpad(n::text, 26, '0'), 'filler-' || n, 'unit-runner', '{"mode": "batch"}',
                      '0 2 * * *', 'UTC', 'none', 'local:tester@test', 'local:tester@test'
               FROM generate_series(1, %s) n""",
            (count,),
        )


# -- what a schedule is, and what saving one checks (§3, §9) -------------------------------------------


async def test_what_saving_a_schedule_checks(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "tzdata", lambda: "2026c")
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    endless = await harness.spec(spec_text(execution="cycleInterval: 1h, concurrency: 2"))
    ending = await harness.spec(
        spec_text(execution="cycleInterval: 1h, stop: {maxCycles: 2}, concurrency: 2")
    )
    sampled = await harness.spec(listed(name="unit-sampled", execution="cycleInterval: 1h, concurrency: 2"))
    good = {
        "name": "nightly",
        "spec_id": spec_id,
        "run": {"mode": "batch"},
        "cron": daily(due),
        "time_zone": "UTC",
    }
    soak = {"mode": "soak"}
    slug = (
        "name must be a slug of 1 to 63 characters (lower-case letters, digits and single hyphens, starting"
        " with a letter)"
    )
    interval = "a soak run needs the spec's execution.cycleInterval"
    rolling = (
        "a soak over a hash sample draws a new sample each cycle: start it rolling (ds run --soak --rolling)"
    )
    endless_ = (
        f"a scheduled soak must end by itself: spec {endless} (unit-runner 1.0.0) needs"
        " execution.stop.maxCycles, maxDuration or until, or every later firing is skipped while it runs"
    )
    spaced = (
        "cron fires at 02:00 and 02:05: a schedule fires at most every 15 minutes (a soak's"
        " execution.cycleInterval repeats faster)"
    )
    renamed = "'Asia/Calcutta' is not a time zone this build uses (tzdata 2026c): use Asia/Kolkata"
    bare = "time_zone must be UTC or an IANA region name such as Europe/London: 'localtime' is not one"
    refusals: list[tuple[dict[str, Any], str]] = [
        *(({"name": name}, slug) for name in ("", "Nightly", "9-to-5", "a--b", "a_b", "a" * 64, None)),
        *(
            ({"name": name}, f"name must not be {name}: /v1/schedules/{name} is a route of the API's own")
            for name in ("zones", "preview")  # GET /v1/schedules/zones would never read such a schedule
        ),
        ({"run": {"mode": "one_shot"}}, runs.MODE),
        ({"run": {"mode": "batch", "soak": {"rolling": True}}}, runs.MODE),
        ({"run": {}}, "mode must be batch or soak, and only a soak run takes soak settings"),
        ({"run": soak}, interval),
        ({"run": soak, "spec_id": sampled}, rolling),
        ({"run": soak, "spec_id": endless}, endless_),
        *(({"start_within": n}, "start_within must be 15 to 1440 minutes") for n in (14, 1441, "60", True)),
        ({"cron": "*/5 2 * * *"}, spaced),
        ({"cron": "0 2 * *"}, "cron must have 5 fields (minute hour day-of-month month day-of-week), not 4"),
        ({"time_zone": "Asia/Calcutta"}, renamed),
        ({"time_zone": "localtime"}, bare),
    ]
    for changes, said in refusals:
        with pytest.raises(Refused) as refused:
            await schedules.create(harness.ctx, good | changes, TESTER)
        assert (refused.value.status, str(refused.value)) == (422, said), changes
    async with harness.ctx.pool.connection() as conn:
        assert (await repo.count_schedules(conn))["count"] == 0  # nothing was written, or audited
        assert await repo._rows(conn, "SELECT 1 FROM ds.audit_log WHERE action LIKE 'schedule.%%'") == []
    compiled = Context.spec

    async def keyless(ctx: Context, conn: Any, found: int, **options: Any) -> Any:
        spec = await compiled(ctx, conn, found, **options)  # as only an earlier build could have stored one
        return dataclasses.replace(spec, raw=spec.raw | {"key": {"name": "item_id"}})

    with monkeypatch.context() as patched, pytest.raises(Refused) as refused:
        patched.setattr(Context, "spec", keyless)
        await schedules.create(harness.ctx, good, TESTER)
    assert str(refused.value) == (
        f"spec {spec_id} (unit-runner 1.0.0) has no key source and is not a pair of tables: a schedule's runs"
        " need one of them"
    )
    async with harness.ctx.pool.connection() as conn:
        assert (await repo.count_schedules(conn))["count"] == 0
    london = {"cron": " 30  1 * * * ", "time_zone": "Europe/London", "start_within": 15}
    row, warnings = await schedules.create(
        harness.ctx, good | london | {"run": soak, "spec_id": ending}, TESTER
    )
    assert (row["cron"], row["run"], row["start_within"], row["state"]) == ("30 1 * * *", soak, 15, "active")
    assert row["next_due"] == cron.after("30 1 * * *", "Europe/London", row["anchor_at"]) and row["anchor_at"]
    assert warnings[0] == "developer mode has no approvers: its runs are labelled unapproved"
    assert warnings[1:] == cron.notes("30 1 * * *", "Europe/London", row["anchor_at"]) and len(warnings) == 3
    ((actor, action, detail),) = await audited(harness, row["schedule_id"])
    assert (actor, action) == (TESTER_ACTOR, "schedule.create") and detail == {
        "name": "nightly", "spec_name": "unit-runner", "spec_id": ending, "run": soak, "cron": "30 1 * * *",
        "time_zone": "Europe/London", "start_within": 15, "ends_at": None, "auth_mode": "none", "revision": 1,
        "next_due": cron.utc(row["next_due"]),
    }  # fmt: skip
    with pytest.raises(Refused) as refused:  # one live schedule of a name
        await schedules.create(harness.ctx, good, TESTER)
    assert (refused.value.status, str(refused.value)) == (
        409,
        f"a schedule named nightly exists already ({row['schedule_id']})",
    )
    other, warnings = await schedules.create(  # a zone named by its offset is taken, and warned of
        harness.ctx, good | {"name": "other", "time_zone": "Etc/GMT+5"}, TESTER
    )
    assert warnings[1:] == [
        "the name of 'Etc/GMT+5' holds a number: under a key pattern that matches a short number, the runs of"
        " a schedule in it get no evidence pack: use a region's zone, or UTC"
    ]
    with pytest.raises(Refused, match=r"^a schedule named nightly exists already \(SC-"):
        await schedules.replace(harness.ctx, "other", body(other, name="nightly"), TESTER)
    gone = await schedules.delete(harness.ctx, "nightly", TESTER)  # a tombstone frees its name
    assert (gone["schedule_id"], gone["deleted_by"], gone["next_due"]) == (
        row["schedule_id"],
        TESTER_ACTOR,
        None,
    )
    for write in (schedules.pause, schedules.resume, schedules.delete):
        with pytest.raises(Refused, match=f"^schedule {row['schedule_id']} is deleted$") as refused:
            await write(harness.ctx, row["schedule_id"], TESTER)
        assert refused.value.status == 409
    with pytest.raises(Refused, match=r"^schedule nightly not found$") as refused:
        await schedules.pause(harness.ctx, "nightly", TESTER)
    assert refused.value.status == 404
    again, _ = await schedules.create(harness.ctx, good, TESTER)
    assert again["schedule_id"] != row["schedule_id"]  # the live one of the name; the tombstone by its id
    assert (await shown(harness, "nightly"))["schedule_id"] == again["schedule_id"]
    assert (await shown(harness, row["schedule_id"]))["state"] == "deleted"
    await fillers(harness, 198)  # an installation holds 200, the deleted aside
    with pytest.raises(Refused) as refused:
        await schedules.create(harness.ctx, good | {"name": "one-more"}, TESTER)
    assert (refused.value.status, str(refused.value)) == (
        409,
        "this installation holds 200 schedules, the most it takes: delete one first",
    )
    await schedules.delete(harness.ctx, "other", TESTER)
    assert (await schedules.create(harness.ctx, good | {"name": "one-more"}, TESTER))[0]["state"] == "active"
    async with harness.ctx.pool.connection() as conn:
        assert len((await repo.list_schedules(conn, limit=500))[0]) == 200
        assert len((await repo.list_schedules(conn, state="deleted"))[0]) == 2
        page, cursor = await repo.list_schedules(
            conn, spec_name="unit-runner", set_by=TESTER_ACTOR, limit=150
        )
        rest, end = await repo.list_schedules(conn, spec_name="unit-runner", limit=150, cursor=cursor)
        assert (len(page), len(rest), end) == (150, 50, None) and page[0]["name"] == "one-more"
        assert (await repo.list_schedules(conn, set_by="user:U-0123456789abcdef"))[0] == []
        assert await repo.overdue_schedules(conn, 10) == 0


async def test_the_limit_holds_under_concurrent_creates(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    await fillers(harness, 199)
    good = {"spec_id": spec_id, "run": {"mode": "batch"}, "cron": daily(due), "time_zone": "UTC"}
    creates = [schedules.create(harness.ctx, good | {"name": f"racer-{n}"}, TESTER) for n in range(4)]
    answers = await asyncio.gather(*creates, return_exceptions=True)  # each on a connection of its own
    refused = [answer for answer in answers if isinstance(answer, Refused)]
    assert len(refused) == 3 and {(answer.status, str(answer)) for answer in refused} == {
        (409, "this installation holds 200 schedules, the most it takes: delete one first")
    }
    assert [answer[0]["state"] for answer in answers if not isinstance(answer, BaseException)] == ["active"]
    async with harness.ctx.pool.connection() as conn:  # counted one at a time: never over the limit
        assert (await repo.count_schedules(conn))["count"] == 200


# -- a firing ------------------------------------------------------------------------------------------


async def test_fires_once(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due)
    assert (row["state"], row["next_due"], row["auth_mode"], row["revision"]) == ("active", due, "none", 1)
    assert await fire(harness, row) is None
    (run,) = await started(harness, row)
    record = {"schedule_id": row["schedule_id"], "due": cron.utc(due), "revision": 1, "time_zone": "UTC"}
    assert (run["status"], run["mode"], run["created_by"], run["params"]["schedule"]) == (
        "running",
        "batch",
        "system:worker",  # no person is recorded as doing what no session did
        record,
    )
    after = await shown(harness, row["schedule_id"])
    assert (after["anchor_at"], after["next_due"], after["last_run_id"]) == (due, due + DAY, run["run_id"])
    assert after["revision"] == 1 and after["last_at"] is not None and after["held"] is None
    fired = {
        "due": cron.utc(due), "run_id": run["run_id"], "spec_id": spec_id, "spec_name": "unit-runner",
        "mode": "batch", "revision": 1, "time_zone": "UTC", "next": cron.utc(due + DAY),
    }  # fmt: skip
    late = after["last"]["late_s"]
    assert after["last"] == fired | {"outcome": "fire", "late_s": late} and 0 <= late < 120
    created = await audited(harness, run["run_id"])
    assert [(actor, action) for actor, action, _ in created] == [(WORKER_ACTOR, "create_run")]
    detail = created[0][2]
    assert detail["schedule"] == record  # and the plan's hash binds it
    assert detail["plan_sha256"] == sha256_hex(approvals.plan(run["params"]))
    assert json.loads(approvals.plan(run["params"]))["schedule"] == record
    rows = await audited(harness, row["schedule_id"])
    assert rows == [
        (TESTER_ACTOR, "schedule.create", rows[0][2]),
        (WORKER_ACTOR, "schedule.fire", fired | {"late_s": late}),
    ]
    for at in (None, due + HOUR, due + DAY - SECOND):  # exactly once: the time is settled
        assert await fire(harness, row, at) is None
    assert len(await started(harness, row)) == 1 and len(await audited(harness, row["schedule_id"])) == 2


async def test_two_firers_race_one_run(harness: Harness) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    assert await asyncio.gather(*(fire(harness, row) for _ in range(4))) == [None] * 4
    (run,) = await started(harness, row)  # the compare-and-set and the unique index: one run
    rows = await audited(harness, row["schedule_id"])
    assert [action for _, action, _ in rows] == ["schedule.create", "schedule.fire"]
    assert (await shown(harness, row["schedule_id"]))["last_run_id"] == run["run_id"]
    async with harness.ctx.pool.connection() as conn:
        assert (await repo.list_runs(conn))[0] == [run]  # the losers' runs were rolled back


async def test_forged_second_run_is_fired_already(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due)
    forged = {"schedule": {"schedule_id": row["schedule_id"], "due": cron.utc(due)}}
    async with harness.ctx.pool.connection() as conn:  # a hand-made run of this time
        await stored_run(harness.ctx, conn, spec_id, "batch", {}, forged)
    assert await fire(harness, row) is None
    assert reasons(await missed(harness, row)) == [("late", "fired_already", cron.utc(due), None, None)]
    after = await shown(harness, row["schedule_id"])
    assert (after["anchor_at"], after["next_due"], after["last_run_id"]) == (due, due + DAY, None)
    assert len(await started(harness, row)) == 1  # the forged one: no second run of the time


async def test_noncanonical_due_refused(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due)
    for other in (due.isoformat(), f"{due:%Y-%m-%dT%H:%M:%SZ}", f"{due:%Y-%m-%d %H:%MZ}"):
        forged = {"schedule": {"schedule_id": row["schedule_id"], "due": other}}
        async with harness.ctx.pool.connection() as conn:  # another spelling would be another index key
            with pytest.raises(psycopg.errors.CheckViolation, match="runs_schedule"):
                await stored_run(harness.ctx, conn, spec_id, "batch", {}, forged)
    assert await fire(harness, row) is None
    assert [run["params"]["schedule"]["due"] for run in await started(harness, row)] == [cron.utc(due)]


async def test_kill_before_commit_refires_in_window(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    insert = repo.insert_audit

    async def killed(conn: Any, who: Any, action: str, *args: Any) -> Any:
        if action == "schedule.fire":  # the last statement before the commit
            raise asyncio.CancelledError
        return await insert(conn, who, action, *args)

    monkeypatch.setattr(repo, "insert_audit", killed)
    with pytest.raises(asyncio.CancelledError):
        await fire(harness, row)
    after = await shown(harness, row["schedule_id"])
    assert (after["next_due"], after["anchor_at"], after["last"]) == (due, row["anchor_at"], None)
    assert await started(harness, row) == []  # the run and its rows went with the advance
    assert [action for _, action, _ in await audited(harness, row["schedule_id"])] == ["schedule.create"]
    monkeypatch.undo()
    assert await fire(harness, row) is None  # the time was left due, and its window is open
    assert len(await started(harness, row)) == 1


@pytest.mark.parametrize("write", ["update", "pause", "delete"])
async def test_put_pause_delete_during_firing_leave_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    resolve, reading, written = runs.resolve, asyncio.Event(), asyncio.Event()

    async def slow(*args: Any, **kwargs: Any) -> Any:
        reading.set()  # the firing resolves its keys, holding no store connection
        await written.wait()
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(runs, "resolve", slow)
    firing = asyncio.create_task(fire(harness, row))
    await reading.wait()
    if write == "update":
        await schedules.replace(harness.ctx, row["schedule_id"], body(row, start_within=30), TESTER)
    else:
        await getattr(schedules, write)(harness.ctx, row["schedule_id"], TESTER)
    written.set()
    assert await firing is None  # the person's write won the compare-and-set
    assert await started(harness, row) == []
    rows = await audited(harness, row["schedule_id"])
    assert [(actor, action) for actor, action, _ in rows[1:]] == [
        (TESTER_ACTOR, "schedule.missed"),  # what the write left unstarted, by the caller
        (TESTER_ACTOR, f"schedule.{write}"),
    ]
    assert reasons([rows[1][2]]) == [("superseded", write, cron.utc(due), cron.utc(due), 1)]


@pytest.mark.parametrize("write", ["update", "pause", "delete"])
async def test_put_pause_delete_settle_due_times(harness: Harness, write: str) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due, back=3 * DAY + MINUTE)  # four times, none started
    if write == "update":
        saved, warnings = await schedules.replace(
            harness.ctx, "nightly", body(row, cron=hourly(due), start_within=30), TESTER
        )
        assert warnings == ["developer mode has no approvers: its runs are labelled unapproved"]
        assert saved["next_due"] == due + HOUR and saved["anchor_at"] > due
    elif write == "pause":
        saved = await schedules.pause(harness.ctx, row["schedule_id"], TESTER)
    else:
        saved = await schedules.delete(harness.ctx, row["schedule_id"], TESTER)
    state = {"update": "active", "pause": "paused", "delete": "deleted"}[write]
    after = await shown(harness, row["schedule_id"])
    assert (after["state"], after["revision"], saved["revision"]) == (state, 2, 2)
    superseded = {
        "reason": "superseded", "code": write, "due": cron.utc(due - 3 * DAY), "to": cron.utc(due),
        "count": 4, "spec_name": "unit-runner", "mode": "batch", "spec_id": spec_id, "run_id": None,
        "draw_id": None, "error_type": None, "revision": 1, "time_zone": "UTC", "start_within": 60,
        "auth_mode": "none", "worker_mode": None, "tzdata": None,
        "next": cron.utc(due + HOUR) if write == "update" else None,
    }  # fmt: skip
    rows = await audited(harness, row["schedule_id"])
    assert rows[1:-1] == [(TESTER_ACTOR, "schedule.missed", superseded)]  # before the caller's own row
    actor, action, detail = rows[-1]
    assert (actor, action, detail["revision"]) == (TESTER_ACTOR, f"schedule.{write}", 2)
    if write == "update":
        assert detail["changed"] == ["cron", "start_within"] and detail["next_due"] == cron.utc(due + HOUR)
        assert (detail["cron"], detail["start_within"], detail["auth_mode"]) == (hourly(due), 30, "none")
    else:
        assert detail == {"revision": 2, "next_due": None} and after["next_due"] is None
    assert after["last"] == superseded | {"outcome": "missed"} and after["held"] is None
    assert await fire(harness, row, due + 30 * MINUTE) is None and await started(harness, row) == []
    assert len(await audited(harness, row["schedule_id"])) == len(rows)  # never dropped, never started


async def test_put_on_paused_stays_paused(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due + HOUR)  # not due yet
    paused = await schedules.pause(harness.ctx, "nightly", TESTER)
    assert (paused["state"], paused["next_due"], paused["paused_by"]) == ("paused", None, TESTER_ACTOR)
    with pytest.raises(Refused, match=f"^schedule {row['schedule_id']} is paused already$") as refused:
        await schedules.pause(harness.ctx, "nightly", TESTER)
    assert refused.value.status == 409
    with pytest.raises(Refused, match=r"changed since you loaded it \(revision 2\): reload and edit again$"):
        await schedules.replace(harness.ctx, "nightly", body(row), TESTER)  # loaded at revision 1
    saved, _ = await schedules.replace(harness.ctx, "nightly", body(paused, cron=hourly(due)), TESTER)
    assert (saved["state"], saved["next_due"], saved["revision"], saved["cron"]) == (
        "paused",
        None,
        3,
        hourly(due),
    )
    assert await fire(harness, row, due + DAY) is None and await started(harness, row) == []
    resumed, warnings = await schedules.resume(harness.ctx, "nightly", TESTER)
    assert (resumed["state"], resumed["next_due"], resumed["revision"], warnings) == (
        "active",
        due + HOUR,
        4,
        [],
    )
    with pytest.raises(Refused, match=f"^schedule {row['schedule_id']} is not paused$"):
        await schedules.resume(harness.ctx, "nightly", TESTER)
    actions = [action for _, action, _ in await audited(harness, row["schedule_id"])]
    assert actions == ["schedule.create", "schedule.pause", "schedule.update", "schedule.resume"]


async def test_resume_past_ends_is_ended(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due + HOUR, ends_at=(due + 2 * DAY).isoformat())
    await schedules.pause(harness.ctx, "nightly", TESTER)
    async with harness.ctx.pool.connection() as conn:  # its end passes while it is paused
        await conn.execute("UPDATE ds.schedules SET ends_at = now() - interval '1 minute'")
        ends = (await repo.get_schedule(conn, "nightly"))["ends_at"]  # type: ignore[index]
    resumed, warnings = await schedules.resume(harness.ctx, "nightly", TESTER)
    assert (resumed["state"], resumed["next_due"], resumed["paused_at"]) == ("ended", None, None)
    assert warnings == [f"the schedule has no time before its ends_at ({cron.utc(ends)})"]
    with pytest.raises(Refused, match=f"^schedule {row['schedule_id']} has ended$") as refused:
        await schedules.pause(harness.ctx, "nightly", TESTER)
    assert refused.value.status == 409
    saved, _ = await schedules.replace(  # active again once the body leaves a time
        harness.ctx, "nightly", body(resumed, ends_at=(due + 3 * DAY).isoformat()), TESTER
    )
    assert (saved["state"], saved["next_due"]) == ("active", due + HOUR)


# -- the window, the overlap, missed and late times ----------------------------------------------------


async def test_overlap_waits_then_starts(harness: Harness) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    await fire(harness, row)
    (first,) = await started(harness, row)
    for at in (due + DAY + SECOND, due + DAY + 30 * MINUTE):  # its run is still live: at most one
        assert await fire(harness, row, at) == Retry(RETRY_S, 0, due + DAY)
    assert await missed(harness, row) == [] and len(await started(harness, row)) == 1  # a try writes nothing
    await pause_run(harness.ctx, first["run_id"], who=TESTER)
    assert await fire(harness, row, due + DAY + 31 * MINUTE) == Retry(RETRY_S, 0, due + DAY)  # paused is live
    await ended(harness, first["run_id"])
    assert await fire(harness, row, due + DAY + 32 * MINUTE) is None
    _, second = await started(harness, row)
    assert second["params"]["schedule"]["due"] == cron.utc(due + DAY) and await missed(harness, row) == []


async def test_overlap_settled_when_window_closes(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due)
    await fire(harness, row)
    (first,) = await started(harness, row)
    at = due + DAY + 59 * MINUTE + 30 * SECOND  # the last try that fits in the window settles it
    assert await fire(harness, row, at) is None
    (detail,) = await missed(harness, row)
    assert detail == {
        "reason": "overlap", "code": None, "due": cron.utc(due + DAY), "to": None, "count": None,
        "spec_name": "unit-runner", "mode": "batch", "spec_id": spec_id, "run_id": first["run_id"],
        "draw_id": None, "error_type": None, "revision": 1, "time_zone": "UTC", "start_within": 60,
        "auth_mode": "none", "worker_mode": None, "tzdata": None, "next": cron.utc(due + 2 * DAY),
    }  # fmt: skip
    after = await shown(harness, row["schedule_id"])
    assert (after["anchor_at"], after["next_due"]) == (due + DAY, due + 2 * DAY)
    assert after["last_run_id"] == first["run_id"] and after["last"]["outcome"] == "missed"  # a miss keeps it
    assert await fire(harness, row, due + 2 * DAY + 59 * MINUTE + SECOND) is None  # each time is its own row
    assert [d["due"] for d in await missed(harness, row)] == [cron.utc(due + DAY), cron.utc(due + 2 * DAY)]
    assert len(await started(harness, row)) == 1


async def test_abandoned_run_ends_overlap(harness: Harness) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    await fire(harness, row)
    (first,) = await started(harness, row)
    await pause_run(harness.ctx, first["run_id"], who=TESTER)
    assert await fire(harness, row, due + DAY + SECOND) == Retry(RETRY_S, 0, due + DAY)
    async with harness.ctx.pool.connection() as conn:  # paused for longer than its retention
        await conn.execute("UPDATE ds.runs SET updated_at = now() - interval '400 days'")
    assert await purge.abandon(harness.ctx, WORKER) == 1
    assert await fire(harness, row, due + DAY + 2 * SECOND) is None
    assert [run["status"] for run in await started(harness, row)] == ["cancelled", "running"]


async def test_thirty_days_down_one_range_row_no_run(harness: Harness) -> None:
    due = await minute(harness) - 2 * HOUR  # its 60 minutes are over
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due, back=30 * DAY + MINUTE)
    assert await fire(harness, row) is None
    (detail,) = await missed(harness, row)  # one counted row: never a burst, never a backfill
    assert reasons([detail]) == [("late", "down", cron.utc(due - 30 * DAY), cron.utc(due), 31)]
    assert (detail["next"], detail["run_id"], detail["error_type"]) == (cron.utc(due + DAY), None, None)
    after = await shown(harness, row["schedule_id"])
    assert (after["anchor_at"], after["next_due"], after["last_run_id"]) == (due, due + DAY, None)
    assert after["last"] == detail | {"outcome": "missed"} and await started(harness, row) == []
    assert await fire(harness, row) is None and len(await missed(harness, row)) == 1


async def test_wide_window_starts_newest_only(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due, back=3 * DAY + MINUTE, start_within=1440)
    assert await fire(harness, row) is None  # a day's window never reaches past the next due time
    assert reasons(await missed(harness, row)) == [
        ("late", "down", cron.utc(due - 3 * DAY), cron.utc(due - DAY), 3)
    ]
    (run,) = await started(harness, row)
    assert run["params"]["schedule"]["due"] == cron.utc(due)
    rows = [action for _, action, _ in await audited(harness, row["schedule_id"])]
    assert rows == ["schedule.create", "schedule.missed", "schedule.fire"]
    async with harness.ctx.pool.connection() as conn:  # its history is those rows, newest first: no actor
        newest, cursor = await repo.schedule_firings(conn, row["schedule_id"], limit=1)
        older, end = await repo.schedule_firings(conn, row["schedule_id"], cursor=cursor)
    assert [(f["action"], sorted(f)) for f in newest] == [
        ("schedule.fire", ["action", "at", "detail", "id", "seq"])
    ]
    assert (
        [f["action"] for f in older] == ["schedule.missed"]
        and end is None
        and older[0]["seq"] < newest[0]["seq"]
    )
    await ended(harness, run["run_id"])
    # The window closes at the next due time: 23 h 59 min late still starts, a day late is missed.
    assert await fire(harness, row, due + 2 * DAY - MINUTE) is None
    assert len(await started(harness, row)) == 2 and len(await missed(harness, row)) == 1
    await ended(harness, (await started(harness, row))[1]["run_id"])
    assert await fire(harness, row, due + 4 * DAY + HOUR) is None  # two older times, and the newest
    assert reasons(await missed(harness, row))[1:] == [
        ("late", "down", cron.utc(due + 2 * DAY), cron.utc(due + 3 * DAY), 2)
    ]
    assert [run["params"]["schedule"]["due"] for run in await started(harness, row)][2:] == [
        cron.utc(due + 4 * DAY)
    ]


async def test_clock_forward_and_back(harness: Harness) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due + DAY)
    assert (
        await fire(harness, row, due + 11 * DAY + 2 * HOUR) is None
    )  # forward: the times between are missed
    assert reasons(await missed(harness, row)) == [
        ("late", "down", cron.utc(due + DAY), cron.utc(due + 11 * DAY), 11)
    ]
    for at in (due + 5 * DAY, due + 11 * DAY, due + 12 * DAY - SECOND):  # back: nothing fires twice
        assert await fire(harness, row, at) is None
    assert len(await missed(harness, row)) == 1 and await started(harness, row) == []
    assert (await shown(harness, row["schedule_id"]))["next_due"] == due + 12 * DAY
    assert await fire(harness, row, due + 12 * DAY + SECOND) is None  # until the clock passes next_due
    assert [run["params"]["schedule"]["due"] for run in await started(harness, row)] == [
        cron.utc(due + 12 * DAY)
    ]


async def test_save_after_clock_back_refires_nothing(harness: Harness) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due + 10 * DAY)
    await fire(harness, row, due + 10 * DAY + SECOND)  # settled ten days ahead of the database's clock
    (run,) = await started(harness, row)
    before = await shown(harness, row["schedule_id"])
    assert (before["anchor_at"], before["next_due"]) == (due + 10 * DAY, due + 11 * DAY)
    saved, _ = await schedules.replace(harness.ctx, "nightly", body(before, start_within=30), TESTER)
    assert (saved["anchor_at"], saved["next_due"]) == (due + 10 * DAY, due + 11 * DAY)  # never before it
    await schedules.pause(harness.ctx, "nightly", TESTER)
    resumed, _ = await schedules.resume(harness.ctx, "nightly", TESTER)
    assert (resumed["anchor_at"], resumed["next_due"]) == (due + 10 * DAY, due + 11 * DAY)
    await ended(harness, run["run_id"])
    for at in (None, due + 10 * DAY, due + 11 * DAY - SECOND):
        assert await fire(harness, row, at) is None
    assert len(await started(harness, row)) == 1 and await missed(harness, row) == []


async def test_ends_at(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    declared = {"name": "ending", "spec_id": spec_id, "run": {"mode": "batch"}, "cron": daily(due)}
    declared |= {"time_zone": "UTC"}
    said = "^ends_at must be an ISO-8601 time with a UTC offset, later than now$"
    for ends in ("tomorrow", f"{due + DAY:%Y-%m-%dT%H:%M}", (due - DAY).isoformat(), 7):
        with pytest.raises(Refused, match=said):
            await schedules.create(harness.ctx, declared | {"ends_at": ends}, TESTER)
    soon = due + 5 * MINUTE  # later than now, and before the cron's next time
    with pytest.raises(Refused) as refused:
        await schedules.create(harness.ctx, declared | {"ends_at": soon.isoformat()}, TESTER)
    assert str(refused.value) == f"the schedule has no time before its ends_at ({cron.utc(soon)})"
    row = await scheduled(harness, spec_id, due, ends_at=(due + DAY).isoformat())  # its last time, inclusive
    await fire(harness, row)
    (first,) = await started(harness, row)
    await ended(harness, first["run_id"])
    assert (await shown(harness, row["schedule_id"]))["next_due"] == due + DAY
    assert await fire(harness, row, due + DAY + SECOND) is None
    after = await shown(harness, row["schedule_id"])
    assert (after["state"], after["next_due"], len(await started(harness, row))) == ("ended", None, 2)
    assert (await audited(harness, row["schedule_id"], "schedule.fire"))[-1][2]["next"] is None
    assert await fire(harness, row, due + 9 * DAY) is None and await missed(harness, row) == []


async def test_resume_without_catch_up(harness: Harness) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    await schedules.pause(harness.ctx, "nightly", TESTER)  # the time due at the pause is its own row
    async with harness.ctx.pool.connection() as conn:  # and five days pass while it is paused
        await conn.execute("UPDATE ds.schedules SET anchor_at = anchor_at - interval '5 days'")
    resumed, _ = await schedules.resume(harness.ctx, "nightly", TESTER)
    assert resumed["next_due"] == due + DAY and resumed["anchor_at"] > due
    rows = await audited(harness, row["schedule_id"])
    assert [action for _, action, _ in rows] == [
        "schedule.create", "schedule.missed", "schedule.pause", "schedule.resume",
    ]  # fmt: skip
    assert reasons([rows[1][2]]) == [("superseded", "pause", cron.utc(due), cron.utc(due), 1)]
    assert rows[3][2] == {"revision": 3, "next_due": cron.utc(due + DAY)}  # its pause's times: not missed
    assert await fire(harness, row) is None and await started(harness, row) == []


# -- actors, the auth mode, the gate and the licence at each firing (§6, §7) ---------------------------


async def test_other_mode_missed(open_harness: OpenHarness, service: ScriptedService) -> None:
    async with open_harness(targets(service), auth=TOKENS) as h:  # saved in tokens mode
        due = await minute(h)
        row = await scheduled(h, await h.spec(PLAIN), due)
        assert row["auth_mode"] == "tokens"
    async with open_harness(targets(service), auth=OIDC) as h:  # and met by a worker under SSO
        assert await fire(h, row) is None
        assert await fire(h, row, due + DAY + SECOND) is None
        assert reasons(await missed(h, row)) == [("mode", None, cron.utc(due), None, None)]  # the first
        (first,) = await missed(h, row)  # the modes its text names, as they were at that time
        assert (first["auth_mode"], first["worker_mode"], first["start_within"]) == ("tokens", "oidc", 60)
        assert schedules.line("missed", first) == (
            "not started: the schedule was saved in tokens mode and this worker runs under SSO: save it"
            " again, signed in, to run it"
        )
        held = (await shown(h, row["schedule_id"]))["held"]  # the next is held, unchained
        assert (held["reason"], held["due"], held["to"], held["count"]) == (
            "mode",
            cron.utc(due + DAY),
            cron.utc(due + DAY),
            1,
        )
        assert await started(h, row) == []  # no ungated run of it starts
        before = await shown(h, row["schedule_id"])
        assert before["last"] == held | {"outcome": "missed"} and before["auth_mode"] == "tokens"
        saved, warnings = await schedules.replace(h.ctx, "nightly", body(before), TESTER)  # saved again
        assert (saved["auth_mode"], saved["held"], saved["next_due"]) == ("oidc", None, due + 2 * DAY)
        rows = await audited(h, row["schedule_id"])
        assert [(actor, action) for actor, action, _ in rows[1:]] == [
            (WORKER_ACTOR, "schedule.missed"),
            (TESTER_ACTOR, "schedule.missed"),  # the held range, chained by the write that flushed it
            (TESTER_ACTOR, "schedule.update"),
        ]
        assert rows[2][2] == held and rows[3][2]["changed"] == ["auth_mode"] and warnings == []
        assert await fire(h, row, due + 2 * DAY + SECOND) is None
        (run,) = await started(h, row)
        assert run["params"]["approval"] == {"needs": [], "mode": "oidc"}
        assert run["params"]["schedule"]["revision"] == 2


async def test_disabled_or_demoted_setter(sso: Harness) -> None:
    due = await minute(sso)
    spec_id = await sso.spec(PLAIN)
    async with sso.ctx.pool.connection() as conn:
        olga, pete = await person(conn, "olga", "operator"), await person(conn, "pete", "operator")
    row = await scheduled(sso, spec_id, due, who=olga)
    assert (row["set_by"], row["created_by"], row["set_by_name"]) == (olga.actor, olga.actor, "olga")
    await fire(sso, row)
    (run,) = await started(sso, row)
    assert run["created_by"] == "system:worker" and run["params"]["approval"]["mode"] == "oidc"
    await ended(sso, run["run_id"])
    async with sso.ctx.pool.connection() as conn:  # the leaver
        await repo.set_user_disabled(conn, olga.actor.removeprefix("user:"), TESTER_ACTOR)
    assert await fire(sso, row, due + DAY + SECOND) is None
    async with sso.ctx.pool.connection() as conn:  # enabled again, and no longer an operator
        await repo.set_user_disabled(conn, olga.actor.removeprefix("user:"), None)
        await repo.upsert_user(
            conn, "https://idp.test", "olga", "olga", None, expand(["viewer"]), signed_in=True
        )
    assert await fire(sso, row, due + 2 * DAY + SECOND) is None
    assert reasons(await missed(sso, row)) == [("setter", None, cron.utc(due + DAY), None, None)]
    assert (await shown(sso, row["schedule_id"]))["held"]["count"] == 1 and len(await started(sso, row)) == 1
    saved, _ = await schedules.replace(  # another operator saves it again
        sso.ctx, "nightly", body(await shown(sso, row["schedule_id"])), pete
    )
    assert (saved["set_by"], saved["created_by"], saved["revision"]) == (pete.actor, olga.actor, 2)
    assert await fire(sso, row, due + 3 * DAY + SECOND) is None
    assert [r["params"]["schedule"]["due"] for r in await started(sso, row)][1:] == [cron.utc(due + 3 * DAY)]
    actors = [actor for actor, _, _ in await audited(sso, row["schedule_id"], "schedule.missed")]
    assert actors == [WORKER_ACTOR, pete.actor]


@pytest.mark.parametrize("left", ["disabled", "demoted"])
async def test_setter_gone_during_the_listing_starts_no_run(
    sso: Harness, monkeypatch: pytest.MonkeyPatch, left: str
) -> None:
    due = await minute(sso)
    spec_id = await sso.spec(PLAIN)
    async with sso.ctx.pool.connection() as conn:
        olga = await person(conn, "olga", "operator")
    row = await scheduled(sso, spec_id, due, who=olga)
    resolve = runs.resolve

    async def listing(*args: Any, **kwargs: Any) -> Any:
        async with sso.ctx.pool.connection() as conn:  # the leaver step, while the firing reads its keys
            assert await repo.setter_live(conn, olga.actor)  # the firing judged her live before it
            if left == "disabled":
                await repo.set_user_disabled(conn, olga.actor.removeprefix("user:"), TESTER_ACTOR)
            else:
                roles = expand(["viewer"])
                await repo.upsert_user(conn, "https://idp.test", "olga", "olga", None, roles, signed_in=True)
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(runs, "resolve", listing)
    assert await fire(sso, row) is None
    assert reasons(await missed(sso, row)) == [("setter", None, cron.utc(due), None, None)]
    async with sso.ctx.pool.connection() as conn:  # judged again in the insert transaction: no run
        assert (await repo.list_runs(conn))[0] == []
    after = await shown(sso, row["schedule_id"])
    assert (after["next_due"], after["last_run_id"], after["last"]["reason"]) == (due + DAY, None, "setter")


async def test_token_rotation_keeps_schedules(sso: Harness) -> None:
    due = await minute(sso)
    spec_id = await sso.spec(PLAIN)
    async with sso.ctx.pool.connection() as conn:
        rita, olga = await person(conn, "rita", "spec_author"), await person(conn, "olga", "operator")
        ci = await token(conn, "ci-token", rita, owner=olga)
        unowned = await token(conn, "old-token", rita)
    row = await scheduled(sso, spec_id, due, who=ci)
    other = await scheduled(sso, spec_id, due + DAY, who=unowned, name="unowned")
    token_id = ci.actor.removeprefix("token:")
    changes = [  # expired, then revoked: rotation is routine, and stops nothing
        """UPDATE ds.api_tokens SET created_at = now() - interval '100 days',
               expires_at = now() - interval '9 days'""",
        "UPDATE ds.api_tokens SET revoked_at = now(), revoked_by = 'local:tester@test'",
    ]
    for day, change in enumerate(["SELECT 1", *changes]):
        async with sso.ctx.pool.connection() as conn:
            await conn.execute(change)  # type: ignore[arg-type]
            assert await repo.setter_live(conn, ci.actor)
        assert await fire(sso, row, due + day * DAY + SECOND if day else None) is None
        await ended(sso, (await started(sso, row))[-1]["run_id"])
    assert len(await started(sso, row)) == 3 and await missed(sso, row) == []
    async with sso.ctx.pool.connection() as conn:  # its accountable person leaves
        await repo.set_user_disabled(conn, olga.actor.removeprefix("user:"), TESTER_ACTOR)
        assert not await repo.setter_live(conn, ci.actor) and await repo.setter_live(conn, unowned.actor)
        assert await repo.setter_live(conn, f"token:{'T-' + '0' * 16}") and token_id in ci.actor
        assert await repo.setter_live(conn, TESTER_ACTOR) and await repo.setter_live(conn, "dev:loopback")
        assert not await repo.setter_live(conn, olga.actor) and not await repo.setter_live(
            conn, rita.actor[:-1] + "x"
        )
    assert await fire(sso, row, due + 3 * DAY + SECOND) is None
    assert reasons(await missed(sso, row)) == [("setter", None, cron.utc(due + 3 * DAY), None, None)]
    assert await fire(sso, other, due + DAY + SECOND) is None  # a token with no accountable person
    assert len(await started(sso, other)) == 1


async def test_unapproved_then_granted_then_revoked(sso: Harness) -> None:
    due = await minute(sso)
    spec_id = await sso.spec(spec_text())  # values in clear: its runs need an approval
    declared = {"name": "nightly", "spec_id": spec_id, "run": {"mode": "batch"}, "cron": daily(due)}
    row, warnings = await schedules.create(sso.ctx, declared | {"time_zone": "UTC"}, TESTER)
    assert warnings == [  # saving is allowed, and warned
        f"spec {spec_id} (unit-runner 1.0.0) needs a four-eyes approval of clear before it runs: its firings"
        " are not started until it is approved"
    ]
    row = await anchored(sso, row["schedule_id"], due - MINUTE)
    assert await fire(sso, row) is None
    assert reasons(await missed(sso, row)) == [("approval", None, cron.utc(due), None, None)]
    approval_id = await approve(sso, spec_id)  # the next due time is judged anew: no other act
    assert await fire(sso, row, due + DAY + SECOND) is None
    (run,) = await started(sso, row)
    assert (run["approval_id"], run["params"]["approval"]) == (
        approval_id,
        {"needs": ["clear"], "mode": "oidc"},
    )
    await ended(sso, run["run_id"])
    await revoke(sso, approval_id)
    assert await fire(sso, row, due + 2 * DAY + SECOND) is None
    assert reasons(await missed(sso, row))[1:] == [("approval", None, cron.utc(due + 2 * DAY), None, None)]
    assert len(await started(sso, row)) == 1  # a revoked version never runs


@pytest.mark.usefixtures("vendor_keys")
async def test_licence_expired_sso_pairs_then_renewal_no_backlog(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    async with open_harness(targets(service), auth=OIDC, tier=None) as h:  # an unlicensed evaluation
        due = await minute(h)
        spec_id = await h.spec(PLAIN)
        other = await h.spec(PLAIN.replace("name: unit-runner", "name: unit-other"))
        row = await scheduled(h, spec_id, due)
        assert await fire(h, row) is None  # M5: SSO without an Enterprise licence
        await licensed(h.ctx, tier="enterprise", pairs=1)
        await create_run(h.ctx, other, "batch", who=TESTER)  # the one seat
        assert await fire(h, row, due + DAY + SECOND) is None  # M1: a new pair past the limit
        await unlicensed(h.ctx)
        await licensed(h.ctx, tier="enterprise")
        assert await fire(h, row, due + 2 * DAY + SECOND) is None
        (run,) = await started(h, row)
        assert run["params"]["licence"]["state"] == "valid"
        await ended(h, run["run_id"])
        await unlicensed(h.ctx, ended="enterprise")  # past its grace
        assert await fire(h, row, due + 3 * DAY + SECOND) is None
        assert await fire(h, row, due + 4 * DAY + SECOND) is None
        assert reasons(await missed(h, row)) == [
            ("licence", "sso", cron.utc(due), None, None),
            ("licence", "pairs", cron.utc(due + DAY), None, None),
            ("licence", "expired", cron.utc(due + 3 * DAY), None, None),  # then held: one row a day
        ]
        await unlicensed(h.ctx)
        await licensed(h.ctx, tier="enterprise")  # a renewal: the next due time runs, with no backlog
        assert await fire(h, row, due + 5 * DAY + SECOND) is None
        assert [r["params"]["schedule"]["due"] for r in await started(h, row)] == [
            cron.utc(due + 2 * DAY),
            cron.utc(due + 5 * DAY),
        ]
        assert reasons(await missed(h, row))[3:] == [
            ("licence", "expired", cron.utc(due + 4 * DAY), cron.utc(due + 4 * DAY), 1)
        ]
        async with h.ctx.pool.connection() as conn:  # no message of a refusal was stored
            stored = await repo._rows(
                conn, "SELECT detail::text AS text FROM ds.audit_log WHERE object LIKE 'SC-%%'"
            )
        assert not any("Enterprise" in found["text"] or "docs/mvp" in found["text"] for found in stored)


async def test_standing_refusal_one_row_then_one_a_day(sso: Harness) -> None:
    due = await minute(sso)
    spec_id = await sso.spec(spec_text())  # unapproved
    row = await scheduled(sso, spec_id, due, cron=hourly(due))
    for hours in range(25):
        assert await fire(sso, row, due + hours * HOUR + SECOND) is None
    assert reasons(await missed(sso, row)) == [("approval", None, cron.utc(due), None, None)]
    after = await shown(sso, row["schedule_id"])
    assert (after["held"]["due"], after["held"]["to"], after["held"]["count"]) == (
        cron.utc(due + HOUR),
        cron.utc(due + 24 * HOUR),
        24,
    )
    assert after["last"] == after["held"] | {"outcome": "missed"} and after["anchor_at"] == due + 24 * HOUR
    for hours in (25, 26):  # 24 h after the first held time, the range is chained and a new one begins
        assert await fire(sso, row, due + hours * HOUR + SECOND) is None
    assert reasons(await missed(sso, row))[1:] == [
        ("approval", None, cron.utc(due + HOUR), cron.utc(due + 24 * HOUR), 24)
    ]
    held = (await shown(sso, row["schedule_id"]))["held"]
    assert (held["due"], held["count"], held["next"]) == (
        cron.utc(due + 25 * HOUR),
        2,
        cron.utc(due + 27 * HOUR),
    )
    # A sweep 24 h after a held range's first time chains it though nothing is due: a weekly schedule's.
    weekly = await scheduled(
        sso, spec_id, due, name="weekly", cron=f"{due.minute} {due.hour} * * {due.isoweekday() % 7}"
    )
    for at in (due + SECOND, due + 7 * DAY + SECOND, due + 8 * DAY - SECOND):
        assert await fire(sso, weekly, at) is None
    assert (
        len(await missed(sso, weekly)) == 1
        and (await shown(sso, weekly["schedule_id"]))["held"]["count"] == 1
    )
    async with sso.ctx.pool.connection() as conn:
        assert await repo.schedules_due(conn) == []  # by the database's clock nothing is due, or held a day
        await conn.execute(
            """UPDATE ds.schedules SET held = jsonb_set(held, '{due}', to_jsonb(to_char(
                   now() - interval '25 hours', 'YYYY-MM-DD"T"HH24:MI"Z"'))) WHERE name = 'weekly'"""
        )
    async with sso.ctx.pool.connection() as conn:
        assert [found["schedule_id"] for found in await repo.schedules_due(conn)] == [weekly["schedule_id"]]
    assert await fire(sso, weekly, due + 8 * DAY + SECOND) is None
    assert reasons(await missed(sso, weekly))[1:] == [
        ("approval", None, (await missed(sso, weekly))[1]["due"], cron.utc(due + 7 * DAY), 1)
    ]
    assert (await shown(sso, weekly["schedule_id"]))["held"] is None
    assert all(
        actor == WORKER_ACTOR for actor, _, _ in await audited(sso, weekly["schedule_id"], "schedule.missed")
    )


# -- sampling's ledger (§7) ----------------------------------------------------------------------------


def served(service: ScriptedService, *discrepant: str) -> None:
    """The service lists ``KEYS`` on every walk and serves each on both sides, ``discrepant`` differing."""
    service.listings = [KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key, status="CLOSED" if key in discrepant else "ACTIVE"))


def by_rate(name: str = "unit-runner") -> str:
    """The sampled unit spec, drawing a monitoring sample of every listed key."""
    found = json.loads(listed(name=name))
    source = found["key"]["source"]
    del source["strata"], source["resolve"]
    found["key"]["source"] = source | {"method": "rate", "rate": 1}
    return json.dumps(found)


async def test_new_nonce_each_firing(harness: Harness, service: ScriptedService) -> None:
    served(service)
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(listed()), due)
    nonces = []
    for day in range(2):
        assert await fire(harness, row, due + day * DAY + SECOND if day else None) is None
        run = (await started(harness, row))[-1]
        assert run["draw"].keys() == {"due"} and run["params"]["keys_source"] == "hash_sample"  # undrawn
        assert (await harness.finish(run["run_id"]))["status"] == "completed"  # drawn by the worker
        nonces.append((await drawn(harness, run["run_id"]))["nonce"])  # type: ignore[index]
    assert len(set(nonces)) == 2  # a new nonce each firing: the schedule holds no salt, nonce or selection
    stored = json.dumps(await shown(harness, row["schedule_id"]), default=str)
    assert not any(nonce in stored for nonce in nonces) and "unit-q4" not in stored


async def test_r1_waits(harness: Harness, service: ScriptedService) -> None:
    served(service)
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(listed()), due)
    other = await harness.spec(listed(name="unit-runner-2"))  # samples the same population
    live = await create_run(harness.ctx, other, "batch", who=TESTER)
    assert await fire(harness, row) == Retry(RETRY_S, 0, due)  # R1: waits while that run is live
    assert await missed(harness, row) == [] and await started(harness, row) == []
    assert await fire(harness, row, due + 59 * MINUTE + 30 * SECOND) is None  # its last try in the window
    assert reasons(await missed(harness, row)) == [("sample", "live", cron.utc(due), None, None)]
    assert await fire(harness, row, due + DAY + SECOND) == Retry(RETRY_S, 0, due + DAY)
    await ended(harness, live)
    assert await fire(harness, row, due + DAY + MINUTE) is None
    assert len(await started(harness, row)) == 1 and len(await missed(harness, row)) == 1


async def test_unclosed_draw_blocks(harness: Harness, service: ScriptedService) -> None:
    served(service, "K001")
    due = await minute(harness)
    spec_id = await harness.spec(listed())
    failed = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
    await harness.finish(failed)
    draw_id = (await drawn(harness, failed))["draw_id"]  # type: ignore[index]
    row = await scheduled(harness, spec_id, due)
    assert await fire(harness, row) is None  # SAMPLING D7, for an unattended firing
    assert await fire(harness, row, due + DAY + SECOND) is None
    (detail,) = await missed(harness, row)
    assert reasons([detail]) == [("sample", "unclosed", cron.utc(due), None, None)]
    assert detail["draw_id"] == draw_id and await started(harness, row) == []
    assert (await shown(harness, row["schedule_id"]))["held"]["draw_id"] == draw_id
    await close(harness, draw_id)  # closed with the fix: the next firing draws
    served(service)
    assert await fire(harness, row, due + 2 * DAY + SECOND) is None
    (run,) = await started(harness, row)
    assert (await harness.finish(run["run_id"]))["status"] == "completed"
    assert [d["count"] for d in await missed(harness, row)] == [
        None,
        1,
    ]  # the held day, chained by the firing


async def test_rate_sample_free(harness: Harness, service: ScriptedService) -> None:
    served(service, "K001")
    due = await minute(harness)
    spec_id = await harness.spec(by_rate())
    row = await scheduled(harness, spec_id, due)
    other = await create_run(harness.ctx, await harness.spec(by_rate("unit-runner-2")), "batch", who=TESTER)
    assert await fire(harness, row) is None  # a live rate run of its population refuses nothing
    (first,) = await started(harness, row)
    assert (await harness.finish(first["run_id"]))["counters"]["different"] == 1
    await harness.finish(other)  # a worker runs until it ends: the last one stopped with the first
    assert await fire(harness, row, due + DAY + SECOND) is None  # nor does a rate draw that found gaps
    assert len(await started(harness, row)) == 2 and await missed(harness, row) == []


# -- readiness (§7) ------------------------------------------------------------------------------------


async def test_scheduled_runs_are_evidence_points(harness: Harness, service: ScriptedService) -> None:
    for key in ("K001", "K002", "K003"):
        service.both(key, doc(key), doc(key))
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due)
    for day in range(2):  # nothing is built for them: every batch run is measured (READINESS D13)
        assert await fire(harness, row, due + day * DAY + SECOND if day else None) is None
        assert (await harness.finish((await started(harness, row))[-1]["run_id"]))["status"] == "completed"
    await measure(harness)
    points = [await measures(harness, run["run_id"]) for run in await started(harness, row)]
    assert [(p["pair"], p["evidence"], p["spec_id"]) for p in points] == [("unit-runner", True, spec_id)] * 2  # type: ignore[index]


# -- pinned or followed (§6) ---------------------------------------------------------------------------

NEWER = spec_text().replace('version: "1.0.0"', 'version: "1.1.0"')


async def test_follow_takes_new_version(harness: Harness) -> None:
    due = await minute(harness)
    first = await harness.spec(spec_text())
    declared = {"run": {"mode": "batch"}, "cron": daily(due), "time_zone": "UTC", "name": "nightly"}
    for unknown, said in (
        ({"spec_name": "nope"}, "no spec is named nope"),
        ({"spec_id": 99}, "spec 99 not found"),
    ):
        with pytest.raises(Refused, match=f"^{said}$") as refused:
            await schedules.create(harness.ctx, declared | unknown, TESTER)
        assert refused.value.status == 404
    either = (
        "^give spec_id [(]a pinned version[)] or spec_name [(]the newest version at each firing[)], not both$"
    )
    for both in ({}, {"spec_id": first, "spec_name": "unit-runner"}):
        with pytest.raises(Refused, match=either):
            await schedules.create(harness.ctx, declared | both, TESTER)
    row = await scheduled(harness, None, due, spec_name="unit-runner")
    assert (row["spec_id"], row["spec_name"], row["runs_spec_id"], row["runs_version"]) == (
        None,
        "unit-runner",
        first,
        "1.0.0",
    )
    await fire(harness, row)
    await ended(harness, (await started(harness, row))[0]["run_id"])
    second = await harness.spec(NEWER)  # the name's newest version, at each firing
    after = await shown(harness, row["schedule_id"])
    assert (after["runs_spec_id"], after["runs_version"], after["newest_spec_id"]) == (
        second,
        "1.1.0",
        second,
    )
    await fire(harness, row, due + DAY + SECOND)
    assert [run["spec_id"] for run in await started(harness, row)] == [first, second]
    fired = await audited(harness, row["schedule_id"], "schedule.fire")
    assert [detail["spec_id"] for _, _, detail in fired] == [first, second]


async def test_pinned_keeps_version(harness: Harness) -> None:
    due = await minute(harness)
    first = await harness.spec(spec_text())
    row = await scheduled(harness, first, due)
    second = await harness.spec(NEWER)
    after = await shown(harness, row["schedule_id"])  # the newer version is named, and nothing changes
    assert (after["spec_id"], after["runs_spec_id"], after["newest_spec_id"], after["newest_version"]) == (
        first,
        first,
        second,
        "1.1.0",
    )
    await fire(harness, row)
    assert [run["spec_id"] for run in await started(harness, row)] == [first]
    saved, _ = await schedules.replace(harness.ctx, "nightly", body(after, spec_id=second), TESTER)  # moving
    assert (saved["spec_id"], saved["runs_version"], saved["revision"]) == (second, "1.1.0", 2)
    detail = (await audited(harness, row["schedule_id"], "schedule.update"))[0][2]
    assert detail["changed"] == ["spec_id"] and detail["spec_id"] == second


# -- a run that cannot be created (§5 steps 5 and 6) ---------------------------------------------------

CANARY = "K999-4929123456789012"  # a key, as a refusal's message may hold one


def failing(monkeypatch: pytest.MonkeyPatch, *raised: BaseException) -> list[BaseException]:
    """Each resolve of a run's keys raises the next of ``raised``."""
    left = list(raised)

    async def resolve(*_: Any, **__: Any) -> Any:
        raise left.pop(0)

    monkeypatch.setattr(runs, "resolve", resolve)
    return left


async def test_not_started_three_tries(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    left = failing(
        monkeypatch,
        ValueError(f"key {CANARY} is not listed"),
        psycopg.OperationalError(f"connection refused for {CANARY}"),  # a side or the store: not told apart
        LookupError(f"no target for {CANARY}"),
        ValueError(CANARY),
    )
    assert await fire(harness, row) == Retry(RETRY_SLOW_S, 1, due)
    assert await fire(harness, row, due + 5 * MINUTE, tried=1) == Retry(RETRY_SLOW_S, 2, due)
    assert await missed(harness, row) == [] and len(left) == 2  # a try that waits writes nothing
    assert await fire(harness, row, due + 10 * MINUTE, tried=2) is None  # the third settles it
    (detail,) = await missed(harness, row)
    assert reasons([detail]) == [("not_started", None, cron.utc(due), None, None)]
    assert (detail["error_type"], detail["run_id"]) == ("LookupError", None)
    # A try whose next would fall outside the window settles the time with its reason at once.
    assert await fire(harness, row, due + DAY + 56 * MINUTE) is None
    assert [d["error_type"] for d in await missed(harness, row)] == ["LookupError", "ValueError"]
    assert await started(harness, row) == [] and left == []


async def test_an_absent_export_is_missed_with_its_code(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """docs/mvp/FILES.md §12: an export a file side reads that is not ready yet (the manifest's ``NotReady``)
    is tried again every RETRY_S until the window closes, never counted among three tries, and settled as
    ``not_started`` with its code, which the alert's model and the firing text take."""
    exports = tmp_path.resolve()
    with pytest.raises(tables.NotReady) as absent:
        await manifest.pin(
            Target("legacy", "", root=str(exports), settle_s=0, allow_writable=True), "e/*.csv", "side a",
            FilesSettings(),
        )  # fmt: skip
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    left = failing(monkeypatch, *(absent.value for _ in range(6)))
    for n in range(4):  # well past three tries, while the window is open
        assert await fire(harness, row, due + n * 10 * MINUTE) == Retry(RETRY_S, 0, due)
    assert await missed(harness, row) == []
    assert await fire(harness, row, due + 59 * MINUTE) is None  # no other try fits in its window
    (detail,) = await missed(harness, row)
    assert reasons([detail]) == [("not_started", "export_absent", cron.utc(due), None, None)]
    assert detail["error_type"] == "NotReady" and len(left) == 1 and await started(harness, row) == []
    assert schedules.line("missed", detail, at=due) == (
        "not started: the export a file side reads was not ready before its window closed (export_absent): ds"
        " target check shows its state"
    )
    event = notify.ScheduleMissed(
        schedule_id=row["schedule_id"], reason="not_started", code="unstamped", due=detail["due"], to=None,
        count=None, run_id=None, draw_id=None, error_type="NotReady",
    )  # fmt: skip
    assert event.code == "unstamped"


async def test_a_late_export_starts_in_its_window(
    open_harness: OpenHarness, service: ScriptedService, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """docs/mvp/FILES.md §12, §18: a schedule fired against a real files target, whose spec's file side
    declares a stamp file. No test stub raises here: ``create_run`` plans the files and the manifest finds
    the export absent, then written but unstamped, and the due time stays due (tried every RETRY_S, no try
    counted, no miss row). The stamp lands 30 minutes after the due time under start_within 60: one run, no
    miss row. The next day's stamp never lands: missed at its window's end as ``unstamped`` (F11, whose text
    names what shows that cause: not ``ds target check``, which reads no stamp)."""
    exports = tmp_path_factory.mktemp("exports").resolve()  # outside the harness's own directories (T10)
    (exports / "legacy" / "daily").mkdir(parents=True)
    legacy = {"engine": "files", "root": str(exports / "legacy"), "settle_s": 0, "allow_writable": True}
    declared = "targets:\n  fa: " + json.dumps(legacy) + targets(service).removeprefix("targets:")
    table = file_table("daily/export.csv")
    table["file"]["asOfFile"] = "_AS_OF"
    spec = file_side_spec(table, FILE_SERVICE, ["K001"])
    files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    async with open_harness(declared, files=files) as h:
        due = await minute(h)
        row = await scheduled(h, await h.spec(spec), due)
        assert await fire(h, row, due) == Retry(RETRY_S, 0, due)  # export_absent: waited for, not tried
        (exports / "legacy" / "daily" / "export.csv").write_text("id|name|status\nK001|n1|A\n")
        assert await fire(h, row, due + 10 * MINUTE) == Retry(RETRY_S, 0, due)  # written, not yet stamped
        assert await missed(h, row) == [] and await started(h, row) == []
        (exports / "legacy" / "daily" / "_AS_OF").write_text("2026-09-30T23:59:00+00:00\n")
        assert await fire(h, row, due + 30 * MINUTE) is None
        (run,) = await started(h, row)
        assert await missed(h, row) == [] and run["status"] == "running"
        async with h.ctx.pool.connection() as conn:
            plan = (await repo.get_run(conn, run["run_id"]))["params"]["tables"]["a"]  # type: ignore[index]
        assert (plan["as_of"], plan["rows"], plan["file_count"]) == ("2026-09-30T23:59:00+00:00", 1, 1)
        await ended(h, run["run_id"])
        (exports / "legacy" / "daily" / "_AS_OF").unlink()  # the next day's export never completes
        assert await fire(h, row, due + DAY + 30 * MINUTE) == Retry(RETRY_S, 0, due + DAY)
        assert await fire(h, row, due + DAY + 59 * MINUTE) is None  # no other try fits in its window
        (detail,) = await missed(h, row)
        assert reasons([detail]) == [("not_started", "unstamped", cron.utc(due + DAY), None, None)]
        assert detail["error_type"] == "NotReady" and len(await started(h, row)) == 1
        # F11 points to what shows the cause. ``ds target check`` reads no stamp file: of this export it
        # prints the file and ``settled`` and says nothing of the stamp; a run's creation says the cause.
        assert schedules.line("missed", detail, at=due + DAY) == (
            "not started: the export a file side reads was not ready before its window closed (unstamped):"
            " starting a run on the Runs screen shows the cause"
        )
        checked = await duck.check(h.ctx.targets.get("fa"), "daily/export.csv", "csv")
        assert checked[0].endswith(", settled") and "daily/export.csv" in checked[1]
        assert not [line for line in checked if "stamp" in line or "_AS_OF" in line]
        with pytest.raises(tables.NotReady, match="side a: the stamp file is not there") as refused:
            await create_run(h.ctx, row["spec_id"], "batch", who=TESTER)
        assert refused.value.code == "unstamped"


async def test_a_try_is_judged_on_the_clock_after_the_attempt(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    clock, took = [0.0], [20.0]

    async def listing(*_: Any, **__: Any) -> Any:
        clock[0] += took.pop(0)  # the listing ran this long before it failed
        raise ValueError(f"key {CANARY} is not listed")

    monkeypatch.setattr(runs, "resolve", listing)
    monkeypatch.setattr(schedule, "monotonic", lambda: clock[0])
    poll = harness.ctx.settings.worker.poll_seconds
    left = RETRY_SLOW_S + 100  # of its window, as each attempt begins
    at = due + HOUR - left * SECOND
    assert await fire(harness, row, at) == Retry(RETRY_SLOW_S, 1, due)  # 20 s, the wait and a sweep fit
    took.append(100 - poll / 2)  # this attempt, the wait and a sweep do not: the wait alone would have
    assert await fire(harness, row, at, tried=1) is None
    (detail,) = await missed(harness, row)  # settled at once, with its reason: never late, code down
    assert reasons([detail]) == [("not_started", None, cron.utc(due), None, None)]
    assert detail["error_type"] == "ValueError" and await started(harness, row) == []
    took.append(120.0)  # a slow failure with time for no other try, on its first attempt
    assert await fire(harness, row, at + DAY) is None
    assert reasons(await missed(harness, row))[1:] == [("not_started", None, cron.utc(due + DAY), None, None)]


async def test_a_newer_time_counts_its_own_tries(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    first = await minute(harness)
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, first, cron=hourly(first))
    left = failing(monkeypatch, *(ValueError(CANARY) for _ in range(5)))
    assert await fire(harness, row, first + 10 * SECOND) == Retry(RETRY_SLOW_S, 1, first)
    assert await fire(harness, row, first + 6 * MINUTE, tried=1) == Retry(RETRY_SLOW_S, 2, first)
    # Its window closed at the next time before its third try: that time's tries are its own, three again.
    second = first + HOUR
    assert await fire(harness, row, second + 30 * SECOND, tried=2) == Retry(RETRY_SLOW_S, 1, second)
    assert reasons(await missed(harness, row)) == [("late", "down", cron.utc(first), cron.utc(first), 1)]
    assert await fire(harness, row, second + 6 * MINUTE, tried=1) == Retry(RETRY_SLOW_S, 2, second)
    assert await fire(harness, row, second + 12 * MINUTE, tried=2) is None
    assert [(d["reason"], d["due"]) for d in await missed(harness, row)][1:] == [
        ("not_started", cron.utc(second))
    ]
    assert left == [] and await started(harness, row) == []


async def test_listing_past_window_times_out(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)

    async def listing(*_: Any, **__: Any) -> Any:
        await asyncio.sleep(60)  # a listing that outlives the window

    monkeypatch.setattr(runs, "resolve", listing)
    began = asyncio.get_running_loop().time()
    assert await fire(harness, row, due + HOUR - SECOND / 4) is None
    assert asyncio.get_running_loop().time() - began < 10  # cut at the window's end
    (detail,) = await missed(harness, row)
    assert (detail["reason"], detail["error_type"], detail["due"]) == (
        "not_started",
        "TimeoutError",
        cron.utc(due),
    )
    assert await started(harness, row) == []


async def test_commit_after_window_not_started(harness: Harness) -> None:
    due = await minute(harness) - 90 * MINUTE  # by the database's clock its 60 minutes are over
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    assert await fire(harness, row, due + MINUTE) is None  # judged in the window, committed after it
    (detail,) = await missed(harness, row)
    assert (detail["reason"], detail["error_type"], detail["due"]) == (
        "not_started",
        "TimeoutError",
        cron.utc(due),
    )
    assert await started(harness, row) == []  # no run commits outside its window
    async with harness.ctx.pool.connection() as conn:
        assert (await repo.list_runs(conn))[0] == []
    assert (await shown(harness, row["schedule_id"]))["next_due"] == due + DAY


async def test_queue_past_window_is_busy(harness: Harness) -> None:
    due = await minute(harness) - 2 * HOUR
    spec_id = await harness.spec(spec_text())
    row = await scheduled(harness, spec_id, due, back=DAY + MINUTE)
    queued = (due, due + 59 * MINUTE)  # this worker had left it without a slot, its window still open
    assert await fire(harness, row, queued=queued) is None
    assert reasons(await missed(harness, row)) == [
        ("late", "down", cron.utc(due - DAY), cron.utc(due - DAY), 1),
        ("late", "busy", cron.utc(due), None, None),
    ]
    assert (await shown(harness, row["schedule_id"]))["last"]["code"] == "busy"
    # Left without a slot only after its window had closed: no worker ran in it, which is what its row says.
    outage = await scheduled(harness, spec_id, due, back=DAY + MINUTE, name="outage")
    assert await fire(harness, outage, queued=(due, due + HOUR)) is None
    assert reasons(await missed(harness, outage)) == [("late", "down", cron.utc(due - DAY), cron.utc(due), 2)]
    other = await scheduled(harness, spec_id, due, back=DAY + MINUTE, name="unqueued")
    assert await fire(harness, other, queued=(due + DAY, due)) is None  # another time was queued: not these
    assert reasons(await missed(harness, other)) == [("late", "down", cron.utc(due - DAY), cron.utc(due), 2)]


async def test_queued_time_closed_by_the_next_is_busy(harness: Harness) -> None:
    second = await minute(harness)
    first = second - HOUR  # an hourly time's window closes at the next one: by then it is an older time
    row = await scheduled(harness, await harness.spec(spec_text()), first, cron=hourly(first))
    assert await fire(harness, row, queued=(first, first + 10 * MINUTE)) is None
    (detail,) = await missed(harness, row)  # its own row, never one of a range no worker could start
    assert reasons([detail]) == [("late", "busy", cron.utc(first), None, None)]
    assert detail["next"] == cron.utc(second) and schedules.line("missed", detail) == (
        f"not started: its time {cron.utc(first)} passed its 60 min while the worker was still starting other"
        " schedules (two at a time)"
    )
    (run,) = await started(harness, row)  # and the newest, inside its window, is started
    assert run["params"]["schedule"]["due"] == cron.utc(second)
    # With older times before and after it, each range is counted by itself.
    daily_ = await scheduled(harness, row["spec_id"], second, back=3 * DAY + MINUTE, name="daily")
    queued = (second - 2 * DAY, second - 2 * DAY + MINUTE)
    assert await fire(harness, daily_, second + 2 * HOUR, queued=queued) is None
    assert reasons(await missed(harness, daily_)) == [
        ("late", "down", cron.utc(second - 3 * DAY), cron.utc(second - 3 * DAY), 1),
        ("late", "busy", cron.utc(second - 2 * DAY), None, None),
        ("late", "down", cron.utc(second - DAY), cron.utc(second), 2),
    ]
    assert [d["next"] for d in await missed(harness, daily_)] == [
        cron.utc(second - 2 * DAY),  # each row's next is the time after its last
        cron.utc(second - DAY),
        cron.utc(second + DAY),
    ]


async def test_bug_is_error(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    failing(monkeypatch, KeyError(CANARY), IndexError(CANARY), RuntimeError(CANARY))
    for day in range(3):  # a bug is settled at once: never tried again
        assert await fire(harness, row, due + day * DAY + SECOND if day else None) is None
    found = await missed(harness, row)
    assert [(d["reason"], d["error_type"]) for d in found] == [
        ("error", "KeyError"),
        ("error", "IndexError"),
        ("error", "RuntimeError"),
    ]
    assert CANARY not in json.dumps(found) and await started(harness, row) == []


@pytest.mark.usefixtures("vendor_keys")
async def test_audit_rows_last_in_order(harness: Harness) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    for name in ("unit-one", "unit-two"):  # the two pairs an unlicensed evaluation runs
        other = await harness.spec(spec_text().replace("name: unit-runner", f"name: {name}"))
        await create_run(harness.ctx, other, "batch", who=TESTER)
    row = await scheduled(harness, spec_id, due)
    for day in range(2):
        assert await fire(harness, row, due + day * DAY + SECOND if day else None) is None
    held = (await shown(harness, row["schedule_id"]))["held"]
    assert reasons(await missed(harness, row)) == [("licence", "pairs", cron.utc(due), None, None)]
    found = await licensed(harness.ctx, tier="pro")
    async with harness.ctx.pool.connection() as conn:
        head = (await repo.audit_head(conn))["seq"]  # type: ignore[index]
    assert await fire(harness, row, due + 2 * DAY + SECOND) is None
    (run,) = await started(harness, row)
    async with harness.ctx.pool.connection() as conn:
        chained = await repo.audit_chain(conn, after=head)
    assert [(c["seq"] - head, c["audit_actor"], c["action"], c["object"]) for c in chained] == [
        (1, WORKER_ACTOR, "schedule.missed", row["schedule_id"]),  # the held range, flushed
        (2, WORKER_ACTOR, "create_run", run["run_id"]),
        (3, WORKER_ACTOR, "licence.pair", found.licence_id),
        (4, WORKER_ACTOR, "schedule.fire", row["schedule_id"]),
    ]
    assert chained[0]["audit_detail"] == held and held["count"] == 1
    assert chained[1]["audit_detail"]["schedule"] == run["params"]["schedule"]
    after = await shown(harness, row["schedule_id"])
    assert after["held"] is None and after["last"]["outcome"] == "fire"


async def test_planted_key_reaches_no_row_log_or_delivery(
    open_harness: OpenHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    loopback: Any,
) -> None:
    rules = [
        rule("missed", ["schedule.missed"], ["ops-hook"], specs=["unit-*"]),
        rule("others", ["schedule.missed"], ["ops-hook"], specs=["cards-*"]),
        rule("soaks", ["schedule.missed"], ["ops-hook"], modes=["soak"]),
    ]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, "http://127.0.0.1:9", {"ops-hook": hook(base, ref)}, rules) as (h, notifier),
    ):
        due = await minute(h)
        row = await scheduled(h, await h.spec(spec_text()), due)
        await notifier.feed()
        failing(monkeypatch, *(ValueError(f"customer {CANARY} has no account") for _ in range(3)))
        with caplog.at_level(logging.DEBUG):
            for tried in range(3):
                await fire(h, row, due + tried * 5 * MINUTE, tried=tried)
        await notifier.feed()
        (delivery,) = await deliveries(h)
        (detail,) = await missed(h, row)
        async with h.ctx.pool.connection() as conn:
            stored = await repo._rows(conn, "SELECT s::text AS text FROM ds.schedules s")
            logged = await repo._rows(conn, "SELECT a::text AS text FROM ds.audit_log a")
            installation = await repo.installation_id(conn)
    schedule_id, at = row["schedule_id"], cron.utc(due)
    said = f"schedule {schedule_id} did not start a run for {at} (not_started: ValueError)"
    assert said in caplog.messages  # codes and types only
    texts = [delivery["body"], caplog.text, *(found["text"] for found in [*stored, *logged])]
    assert not any(CANARY in text or "has no account" in text for text in texts)
    sent = json.loads(delivery["body"])
    assert (delivery["event"], delivery["severity"], delivery["rules"], delivery["dedup_key"]) == (
        "schedule.missed",
        "alert",
        ["missed"],  # the spec's name and the mode are matched from the row's detail
        f"schedule.missed|{schedule_id}|not_started",
    )
    assert sent["facts"] == {
        "schedule_id": schedule_id, "reason": "not_started", "code": None, "due": at, "to": None,
        "count": None, "run_id": None, "draw_id": None, "error_type": "ValueError",
    }  # fmt: skip
    assert sent["run"] is None and "unit-runner" not in delivery["body"]  # and no payload carries them
    seq = sent["audit"]["seq"]
    assert sent["text"] == (
        f"[test] schedule {schedule_id} did not start a run for {at}: the run could not be created"
        " (ValueError); starting one on the Runs screen shows the cause. Observed by DeltaSleuth"
        f" (installation {installation}); not evidence and not a sign-off. Audit row {seq}."
    )
    assert (
        detail["error_type"] == "ValueError"
        and sent["title"] == f"schedule {schedule_id} did not start a run"
    )


async def test_unscheduled_run_unchanged(harness: Harness) -> None:
    run_id = await create_run(harness.ctx, await harness.spec(spec_text()), "batch", who=TESTER)
    run = await harness.run(run_id)
    assert list(run["params"]) == [  # as before schedules: no member, no audit detail of one
        "soak", "tables", "licence", "approval", "keys_source", "samples_dir", "capabilities",
    ]  # fmt: skip
    ((_, _, detail),) = await audited(harness, run_id, "create_run")
    assert sorted(detail) == [
        "approval", "approval_id", "file_mode", "keys", "licence", "mode", "plan_sha256", "spec_id",
    ]  # fmt: skip
    assert detail["plan_sha256"] == sha256_hex(approvals.plan(run["params"]))
    async with harness.ctx.pool.connection() as conn:
        assert (await repo.list_runs(conn, schedule_id="SC-" + "0" * 26))[0] == []
        assert (await repo.count_schedules(conn))["count"] == 0
        assert await repo._rows(conn, "SELECT 1 FROM ds.audit_log WHERE action LIKE 'schedule.%%'") == []


# -- zone data (§4) ------------------------------------------------------------------------------------


async def test_rezone_audited(harness: Harness) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due + HOUR)  # not due yet
    assert await schedule.rezone(harness.ctx) is True and len(await audited(harness, row["schedule_id"])) == 1
    async with harness.ctx.pool.connection() as conn:  # as an upgrade's zone rules would move its times
        await conn.execute("UPDATE ds.schedules SET next_due = next_due + interval '1 hour'")
    assert await schedule.rezone(harness.ctx) is True
    moved = {"from": cron.utc(due + 2 * HOUR), "to": cron.utc(due + HOUR), "tzdata": cron.tzdata()}
    assert (await audited(harness, row["schedule_id"]))[1:] == [(WORKER_ACTOR, "schedule.rezone", moved)]
    after = await shown(harness, row["schedule_id"])
    assert (after["next_due"], after["revision"], after["last"]) == (due + HOUR, 1, None)
    assert await schedule.rezone(harness.ctx) is True and len(await audited(harness, row["schedule_id"])) == 2
    async with harness.ctx.pool.connection() as conn:  # a next_due no time of its cron: never fired
        await conn.execute(
            "UPDATE ds.schedules SET anchor_at = %s, next_due = %s", (due - 2 * MINUTE, due - MINUTE)
        )
    assert await fire(harness, row) is None
    again = {"from": cron.utc(due - MINUTE), "to": cron.utc(due + HOUR), "tzdata": cron.tzdata()}
    assert (await audited(harness, row["schedule_id"]))[2:] == [(WORKER_ACTOR, "schedule.rezone", again)]
    assert await started(harness, row) == [] and await missed(harness, row) == []


async def test_unknown_zone_pauses_never_stops_sweep(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text(["K001"]))
    service.both("K001", doc("K001"), doc("K001"))
    good = await scheduled(harness, spec_id, due + HOUR)
    dropped = await scheduled(harness, spec_id, due + HOUR, name="dropped")
    overdue = await scheduled(harness, spec_id, due, name="overdue")
    unread = await scheduled(harness, spec_id, due, name="unread")
    swept = await scheduled(harness, spec_id, due, name="swept")
    async with harness.ctx.pool.connection() as conn:  # a zone a later tz release dropped; an unreadable cron
        await conn.execute(
            "UPDATE ds.schedules SET time_zone = 'Mars/Olympus' WHERE name IN ('dropped', 'overdue', 'swept')"
        )
        await conn.execute("UPDATE ds.schedules SET cron = '61 2 * * *' WHERE name = 'unread'")
        await conn.execute(
            "UPDATE ds.schedules SET paused_at = now(), paused_by = 'x', next_due = NULL WHERE name = 'swept'"
        )
    assert await schedule.rezone(harness.ctx) is True  # in rezone: paused, never raised
    assert await fire(harness, unread) is None  # in fire
    for row, code, at in ((dropped, "zone", None), (overdue, "zone", due), (unread, "cron", due)):
        after = await shown(harness, row["schedule_id"])
        assert (after["state"], after["paused_by"], after["next_due"], after["revision"]) == (
            "paused",
            "system:worker",
            None,
            1,
        )
        rows = await audited(harness, row["schedule_id"])
        paused = {
            "revision": 1, "next_due": None, "reason": "zone", "code": code, "tzdata": cron.tzdata(),
            "missed": at is not None, "spec_name": "unit-runner", "mode": "batch",
        }  # fmt: skip
        assert rows[-1] == (WORKER_ACTOR, "schedule.pause", paused)
        if at is None:  # a time not yet due is no missed time: the pause's row alone, which is what is sent
            assert len(rows) == 2 and after["last"] is None
            continue
        assert [(actor, action) for actor, action, _ in rows[1:-1]] == [(WORKER_ACTOR, "schedule.missed")]
        assert reasons([rows[1][2]]) == [("zone", code, cron.utc(at), None, None)]
        assert rows[1][2]["tzdata"] == cron.tzdata()  # the worker's, for the row's text
        assert after["last"] == rows[1][2] | {"outcome": "missed"}
    assert (await shown(harness, good["schedule_id"]))["state"] == "active"
    assert len(await audited(harness, good["schedule_id"])) == 1 and await started(harness, unread) == []
    # A worker's sweep that meets one goes on: its run ends, and the schedule is paused.
    async with harness.ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.schedules SET paused_at = NULL, paused_by = NULL, next_due = %s WHERE name = 'swept'",
            (due,),
        )

    async def unrezoned(_: Any) -> bool:
        return True

    monkeypatch.setattr(schedule, "rezone", unrezoned)
    run_id = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
    assert (await harness.finish(run_id))["status"] == "completed"
    assert (await shown(harness, swept["schedule_id"]))["paused_by"] == "system:worker"
    # A PUT that validates, then a resume, clears it.
    paused = await shown(harness, dropped["schedule_id"])
    with pytest.raises(Refused, match=r"^'Mars/Olympus' is not a time zone this build knows") as refused:
        await schedules.resume(harness.ctx, "dropped", TESTER)
    assert refused.value.status == 422
    await schedules.replace(harness.ctx, "dropped", body(paused, time_zone="Europe/London"), TESTER)
    resumed, _ = await schedules.resume(harness.ctx, "dropped", TESTER)
    assert (resumed["state"], resumed["paused_by"], resumed["time_zone"]) == ("active", None, "Europe/London")
    assert resumed["next_due"] is not None and resumed["revision"] == 3
    # The time it was paused before then starts its run: its history holds no missed row beside the firing.
    assert await fire(harness, dropped, resumed["next_due"] + SECOND) is None
    actions = [action for _, action, _ in await audited(harness, dropped["schedule_id"])]
    assert actions[1:] == ["schedule.pause", "schedule.update", "schedule.resume", "schedule.fire"]


async def test_a_pause_before_a_time_is_due_is_notified(
    open_harness: OpenHarness, tmp_path: Path, loopback: Any
) -> None:
    """T6 at a rezone (§4, §8): a schedule paused before its time is due has no missed row, so its pause's
    own row is delivered; one paused as its time was due is delivered by its missed row alone, and a
    person's pause not at all."""
    rules = [
        rule("missed", ["schedule.missed"], ["ops-hook"], specs=["unit-*"]),
        rule("others", ["schedule.missed"], ["ops-hook"], specs=["cards-*"]),
        rule("soaks", ["schedule.missed"], ["ops-hook"], modes=["soak"]),
    ]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, "http://127.0.0.1:9", {"ops-hook": hook(base, ref)}, rules) as (h, notifier),
    ):
        due = await minute(h)
        spec_id = await h.spec(spec_text())
        dropped = await scheduled(h, spec_id, due + HOUR)  # not due yet
        overdue = await scheduled(h, spec_id, due, name="overdue")
        await scheduled(h, spec_id, due + HOUR, name="rested")
        async with h.ctx.pool.connection() as conn:  # a zone a later tz release dropped
            await conn.execute("UPDATE ds.schedules SET time_zone = 'Mars/Olympus' WHERE name <> 'rested'")
        await notifier.feed()
        await schedules.pause(h.ctx, "rested", TESTER)
        assert await schedule.rezone(h.ctx) is True
        await notifier.feed()
        found = {json.loads(d["body"])["facts"]["schedule_id"]: d for d in await deliveries(h)}
        (_, _, paused), (_, _, late) = [
            (await audited(h, row["schedule_id"], action))[-1]
            for row, action in ((dropped, "schedule.pause"), (overdue, "schedule.missed"))
        ]
        async with h.ctx.pool.connection() as conn:
            installation = await repo.installation_id(conn)
    assert sorted(found) == sorted([dropped["schedule_id"], overdue["schedule_id"]])  # one each, none else
    assert (paused["missed"], late["due"]) == (False, cron.utc(due))
    schedule_id = dropped["schedule_id"]
    delivery = found[schedule_id]
    sent = json.loads(delivery["body"])
    assert (delivery["event"], delivery["severity"], delivery["rules"], delivery["dedup_key"]) == (
        "schedule.missed",
        "alert",
        ["missed"],  # the spec's name and the mode are matched from the pause row's detail
        f"schedule.missed|{schedule_id}|zone",
    )
    assert sent["event_id"] == f"seq:{sent['audit']['seq']}:schedule.missed"
    assert sent["facts"] == {
        "schedule_id": schedule_id, "reason": "zone", "code": "zone", "due": None, "to": None, "count": None,
        "run_id": None, "draw_id": None, "error_type": None,
    }  # fmt: skip
    assert sent["run"] is None and "unit-runner" not in delivery["body"]
    assert sent["title"] == f"schedule {schedule_id} is paused by the worker"
    assert sent["text"] == (
        f"[test] schedule {schedule_id} is paused by the worker before a time of it came due: the worker's"
        " time zone data did not hold its zone. A person saves the schedule with a zone the build holds, then"
        f" resumes it. Observed by DeltaSleuth (installation {installation}); not evidence and not a"
        f" sign-off. Audit row {sent['audit']['seq']}."
    )
    other = json.loads(found[overdue["schedule_id"]]["body"])
    assert other["facts"]["due"] == cron.utc(due) and other["title"].endswith("did not start a run")


async def test_unreadable_key_table_is_not_started(
    on_tables: Tables,
    engine: Failing,  # noqa: F811
) -> None:
    """A schedule whose spec reads its keys from a key table that cannot be read starts no run: a refused
    read, its target's refused credential among them, is ``not_started`` and tried as any run that could
    not be created; an export that is not ready is waited for, with its code."""
    _, harness = on_tables(ITEMS)
    async with harness as h:
        due = await minute(h)
        row = await scheduled(h, await h.spec(keyed(ITEM_KEYS)), due)
        engine("describe", tables.NotReady("unsettled", "side a: its files were modified too recently"))
        assert await fire(h, row) == Retry(RETRY_S, 0, due)  # waited for, never counted
        engine("describe")
        engine("snapshot", tables.TargetAuthRefused("ORA-01017"))
        assert await fire(h, row) == Retry(RETRY_SLOW_S, 1, due)
        engine("snapshot")
        engine("census", psycopg.OperationalError("the server at keys.bank.example closed the connection"))
        assert await fire(h, row, due + 5 * MINUTE, tried=1) == Retry(RETRY_SLOW_S, 2, due)
        assert await missed(h, row) == [] and await started(h, row) == []
        assert await fire(h, row, due + 10 * MINUTE, tried=2) is None  # the third try settles it
        (detail,) = await missed(h, row)
        assert reasons([detail]) == [("not_started", None, cron.utc(due), None, None)]
        assert (detail["error_type"], detail["run_id"]) == ("ValueError", None)
        engine("census")  # readable again: the next due time starts its run
        assert await fire(h, row, due + DAY) is None
        (run,) = await started(h, row)
        assert (run["status"], run["params"]["keys_source"]) == ("running", "table")
