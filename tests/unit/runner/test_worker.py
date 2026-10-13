"""The worker keeps processing: a key a side cannot read, and a slot that meets a bug; a recheck's jitter
reveals nothing of the raw key; its sweep audits each change of the licence's state, hourly at most, and
runs the notifier, whose faults never touch a run and which holds two pooled connections at most; and it
fires the due schedules, two at a time, whose faults never fail a run, end the sweep or loop at the poll
rate, only while it holds the lock and never as ``ds run``'s worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from deltasleuth import licence, schedules
from deltasleuth.cli.main import app
from deltasleuth.core import jsonio
from deltasleuth.runner import create_run, notify, runs, schedule, worker
from deltasleuth.store import repo
from tests.conftest import TESTER, licensed

from .conftest import TARGETS, UNAVAILABLE, Harness, OpenHarness, ScriptedService, doc, spec_text, until
from .notifying import ended, hook, notifying, queued, receiving, rule, stall_in_digest, write
from .scheduling import HOUR, MINUTE, anchored, audited, minute, missed, scheduled, shown, started

pytestmark = pytest.mark.db


async def test_a_key_one_side_cannot_send_is_not_rechecked(
    harness: Harness, service: ScriptedService
) -> None:
    key = "K\t01"  # B sends the key in a header, which refuses a control character: B is skipped
    spec = spec_text(["K001"]).replace('pattern: "^K[0-9]{3}$"', 'pattern: "^K"')
    spec = spec.replace('path: "/b/{{ key }}"}', 'path: "/b/{{ key }}", headers: {X-Key: "{{ key }}"}}')
    service.script("a", key, UNAVAILABLE)
    run_id = await create_run(harness.ctx, await harness.spec(spec), "batch", keys=[key], who=TESTER)
    run = await harness.finish(run_id)
    (result,) = (await harness.results(run_id)).values()
    assert (run["status"], result["outcome"], result["inconclusive_reason"]) == (
        "completed",
        "INCONCLUSIVE",
        "error_a",
    )
    assert dict(service.calls) == {("a", key): 1}  # a recheck could not make B readable
    assert result["attrs"]["side_b_detail"] == "the key cannot be sent to side b"


async def test_a_slot_that_meets_a_bug_restarts(
    open_harness: OpenHarness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    lease_due, calls = repo.lease_due, []

    async def once_broken(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a bug")  # not a store error: the slot ends
        return await lease_due(*args, **kwargs)

    monkeypatch.setattr(repo, "lease_due", once_broken)
    assert service.server is not None
    base = f"http://127.0.0.1:{service.server.port}"
    async with open_harness(TARGETS.format(base=base, rate=500), concurrency=1, lease_seconds=3) as h:
        run_id = await create_run(h.ctx, await h.spec(spec_text(["K001"])), "batch", who=TESTER)
        assert (await h.finish(run_id, timeout=20))["status"] == "completed"


async def test_a_recheck_s_jitter_comes_from_the_masked_key(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    spread: list[str] = []

    def fraction(key_masked: str) -> float:
        spread.append(key_masked)
        return 0.0

    monkeypatch.setattr(worker, "key_fraction", fraction)
    spec = spec_text(["K001"]).replace("class: business_code", "class: identifier")  # masked: a token
    spec = spec.replace("recheck: {delay: 0s,", "recheck: {delay: 0s, jitter: 1s,")
    service.both("K001", doc("K001"), doc("K001", status="NEW"))  # differs: a recheck is due
    run_id = await create_run(harness.ctx, await harness.spec(spec), "batch", who=TESTER)
    assert (await harness.finish(run_id))["status"] == "completed"
    [masked] = await harness.results(run_id)
    assert masked != "K001" and "K001" not in spread and spread.count(masked) >= 2  # the recheck's too


@pytest.mark.usefixtures("vendor_keys")
async def test_watch_once_per_change(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sweeper = worker._Worker(harness.ctx)
    await sweeper._sweep_once()  # the first look records the state: evaluation
    await sweeper._sweep_once()  # within the hour: no second look
    await licence.watch(harness.ctx)  # the next hour's: nothing changed, nothing recorded
    pro = await licensed(harness.ctx, tier="pro")
    await sweeper._sweep_once()  # within the hour still
    sweeper.watched_at -= worker.WATCH_EVERY_S  # an hour later
    await sweeper._sweep_once()
    replaced = await licensed(harness.ctx, tier="pro", issued="2099-01-01T00:00:00Z")  # the same state
    await licence.watch(harness.ctx)

    async def broken(*_: Any) -> Any:
        raise RuntimeError("a bug")

    monkeypatch.setattr(licence, "standing", broken)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.licence"):
        await licence.watch(harness.ctx)  # never raises: the next hour tries again
    async with harness.ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT actor, object, detail FROM ds.audit_log WHERE action = 'licence.state' ORDER BY id"
        )
        rows = await cur.fetchall()
        installation = (await repo.licence_facts(conn))["installation_id"]
    ends = str(pro.ends)
    assert rows == [
        ("system:worker", installation, {"from": None, "to": "evaluation", "licence_id": None, "ends": None}),
        ("system:worker", installation, {"from": "evaluation", "to": "valid", "licence_id": pro.licence_id,
                                         "ends": ends}),
        ("system:worker", installation, {"from": "valid", "to": "valid", "licence_id": replaced.licence_id,
                                         "ends": ends}),
    ]  # fmt: skip
    assert "licence state not watched" in caplog.messages


# -- the notifier -----------------------------------------------------------------


def _notify(base: str, ref: str, events: list[str], **options: Any) -> dict[str, Any]:
    hook = {
        "kind": "webhook",
        "url": f"{base}/webhook",
        "signing_key_refs": [ref],
        "allow_private_network": True,
    }
    return {
        "label": "test",
        "channels": {"ops-hook": hook},
        "rules": [{"name": "all", "events": events, "channels": ["ops-hook"], "modes": ["batch", "soak"]}],
    } | options


@pytest.mark.usefixtures("loopback")
async def test_a_run_s_end_is_notified_signed_with_its_counts(
    open_harness: OpenHarness, service: ScriptedService, tmp_path: Path
) -> None:
    service.both("K001", doc("K001"), doc("K001", status="NEW"))
    service.both("K002", doc("K002"), doc("K002"))
    assert service.server is not None
    async with receiving(tmp_path) as (receiver, base, ref):
        targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
        async with open_harness(targets, notify=_notify(base, ref, ["cycle.ended", "run.finished"])) as h:
            run_id = await create_run(h.ctx, await h.spec(spec_text(["K001", "K002"])), "batch", who=TESTER)
            assert (await h.finish(run_id))["status"] == "completed"
            stop = asyncio.Event()
            running = asyncio.create_task(worker.run_worker(h.ctx, stop=stop))  # the notifier sends
            await until(lambda: len(receiver.received) >= 2)
            stop.set()
            await running
            async with h.ctx.pool.connection() as conn:
                counters = (await repo.get_cycle(conn, run_id, 1))["counters"]  # type: ignore[index]
    ended, finished = (json.loads(r.body) for r in receiver.received)
    assert all(r.verified for r in receiver.received)
    assert (ended["event"], finished["event"]) == ("cycle.ended", "run.finished")
    assert ended["facts"]["counters"] == {k: counters[k] for k in ended["facts"]["counters"]}
    assert ended["facts"]["counters"]["different"] == 1 and ended["facts"]["newly_discrepant"] == 1


@pytest.mark.usefixtures("loopback")
async def test_a_raising_notifier_never_fails_a_run(
    open_harness: OpenHarness,
    service: ScriptedService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*_: Any) -> None:
        raise RuntimeError("a notifier bug")

    for step in ("feed", "scan", "housekeep"):
        monkeypatch.setattr(notify.Notifier, step, broken)
    service.both("K001", doc("K001"), doc("K001"))
    assert service.server is not None
    async with receiving(tmp_path) as (_, base, ref):
        targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
        async with open_harness(targets, notify=_notify(base, ref, ["run.finished"])) as h:
            run_id = await create_run(h.ctx, await h.spec(spec_text(["K001"])), "batch", who=TESTER)
            with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.notify"):
                assert (await h.finish(run_id))["status"] == "completed"
    assert "notifier broken failed: RuntimeError" in caplog.messages  # each step logs and goes on


@pytest.mark.usefixtures("loopback")
async def test_cycles_end_on_time_with_the_notify_tables_locked(
    open_harness: OpenHarness, service: ScriptedService, tmp_path: Path, pg_dsn: str
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    assert service.server is not None
    async with receiving(tmp_path) as (_, base, ref):
        targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
        async with open_harness(targets, notify=_notify(base, ref, ["run.finished"])) as h:
            spec_id = await h.spec(spec_text(["K001"]))
            async with await psycopg.AsyncConnection.connect(pg_dsn) as lock:
                await lock.execute("LOCK TABLE ds.notify_cursor, ds.notify_deliveries, ds.notify_mutes")
                run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
                started = asyncio.get_running_loop().time()
                assert (await h.finish(run_id, timeout=20))["status"] == "completed"
                assert asyncio.get_running_loop().time() - started < 15


@pytest.mark.usefixtures("loopback")
async def test_a_stop_inside_a_digest_savepoint_ends_the_worker(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = stall_in_digest(monkeypatch)
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, "http://127.0.0.1:9", {"ops-hook": hook(base, ref)},
            [rule("gaps", ["cycle.ended"], ["ops-hook"])],
        ) as (h, first),
    ):  # fmt: skip
        run_id = await queued(h)
        async with h.ctx.pool.connection() as conn:  # no key is processed: only the notifier works
            await conn.execute("UPDATE ds.runs SET status = 'paused' WHERE run_id = %s", (run_id,))
        await first.feed()
        await ended(h, run_id, 1)
        await write(h, "run.cycle", run_id, {"cycle": 2})
        stop = asyncio.Event()
        running = asyncio.create_task(worker.run_worker(h.ctx, stop=stop))
        await until(lambda: held)
        stop.set()  # the worker's notifier is cancelled inside the digest's savepoint
        await asyncio.wait([running], timeout=5)
        assert running.done()


@pytest.mark.usefixtures("loopback")
async def test_the_notifier_holds_two_connections_at_most_none_while_sending(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with receiving(tmp_path) as (receiver, base, ref):
        hook = {
            "kind": "webhook",
            "url": f"{base}/webhook",
            "signing_key_refs": [ref],
            "allow_private_network": True,
        }
        settings = {
            "label": "test",
            "channels": {f"c{i}": hook for i in range(6)},
            "rules": [],
            "timeout_seconds": 1,
        }
        async with open_harness(TARGETS.format(base="http://127.0.0.1:9", rate=500), notify=settings) as h:
            async with h.ctx.pool.connection() as conn:
                for i in range(6):
                    await repo.insert_delivery(
                        conn,
                        {
                            "delivery_id": repo.new_delivery_id(),
                            "event_id": "daily:2026-10-03",
                            "seq": None,
                            "event": "notify.daily",
                            "severity": "info",
                            "channel": f"c{i}",
                            "rules": [],
                            "dedup_key": "notify.daily|2026-10-03",
                            "run_id": None,
                            "body": "{}",
                            "body_sha256": "0" * 64,
                            "render": "webhook:1:1",
                            "why": None,
                            "mute_id": None,
                        },
                    )
            held, peak = 0, 0
            connection = h.ctx.pool.connection

            @contextlib.asynccontextmanager
            async def counted(*args: Any, **kwargs: Any) -> Any:
                nonlocal held, peak
                async with connection(*args, **kwargs) as conn:
                    held += 1
                    peak = max(peak, held)
                    try:
                        yield conn
                    finally:
                        held -= 1

            monkeypatch.setattr(h.ctx.pool, "connection", counted)
            receiver.hangs = 6
            notifier = notify.Notifier(h.ctx)
            try:
                await notifier.scan()
                await until(lambda: len(receiver.received) == 6)  # every channel is sending
                assert held == 0  # no pooled connection during a send
                for task in list(notifier.sending.values()):
                    await task
            finally:
                await notifier.close()
    assert peak <= 2 and len(receiver.received) == 6


def test_only_the_worker_imports_the_notifier(repo_root: Path) -> None:
    importers = sorted(
        str(path.relative_to(repo_root))
        for path in (repo_root / "deltasleuth").rglob("*.py")
        if re.search(
            r"from deltasleuth\.runner import [^\n]*\bnotify\b|deltasleuth\.runner\.notify", path.read_text()
        )
    )
    assert importers == ["deltasleuth/runner/worker.py"]


async def test_notify_config_on_change_and_removal(open_harness: OpenHarness, tmp_path: Path) -> None:
    (tmp_path / "hook.key").write_text("whsec_" + "a" * 32 + "\n")
    ref = f"file:{tmp_path / 'hook.key'}"
    first = _notify("https://events.bank.example", ref, ["run.failed"])
    changed = _notify("https://events.bank.example/other-path", ref, ["run.failed"])
    for settings in (first, first, changed, None, None):
        async with open_harness(TARGETS.format(base="http://127.0.0.1:9", rate=500), notify=settings) as h:
            await notify.record_config(h.ctx)
            async with h.ctx.pool.connection() as conn:
                rows, _ = await repo.list_audit(conn, action="notify.config")
    details = [r["detail"] for r in rows[::-1]]
    assert [d["routing"] is None for d in details] == [False, False, True]
    assert details[0]["routing"] != details[1]["routing"] and details[0]["routing"].startswith(
        "tok:notify_routing:"
    )
    assert details[0]["channels"] == {
        "ops-hook": {
            "kind": "webhook",
            "host": "events.bank.example",
            "proxy": False,
            "allow_private_network": True,
            "labels": True,
            "links": True,
            "recipients": 0,
        }
    }
    assert all("other-path" not in json.dumps(d) and str(tmp_path) not in json.dumps(d) for d in details)
    assert all(r["actor"] == "system:worker" and r["object"] == "notify" for r in rows)


async def test_an_upgrade_with_unchanged_settings_records_no_notify_config(
    open_harness: OpenHarness, tmp_path: Path
) -> None:
    """The routing token is computed over a form that leaves out a rule's keys a later build added while
    they are unset: the token a build before ``programmes``, ``gates`` and ``causes`` computed is still the
    settings', so the first start after the upgrade audits no change of the routing. Setting one is one."""
    (tmp_path / "hook.key").write_text("whsec_" + "a" * 32 + "\n")
    settings = _notify("https://events.bank.example", f"file:{tmp_path / 'hook.key'}", ["run.failed"])
    # The rule as that build's settings held it: the nine keys of then.
    before = {
        "name": "all", "events": ["run.failed"], "channels": ["ops-hook"], "specs": None,
        "modes": ["batch", "soak"], "when": None, "reasons": None, "states": None, "statuses": None,
    }  # fmt: skip
    ready = {
        "name": "cutover",
        "events": ["readiness.changed"],
        "channels": ["ops-hook"],
        "causes": ["cycle"],
    }
    targets = TARGETS.format(base="http://127.0.0.1:9", rate=500)
    async with open_harness(targets, notify=settings) as h:
        document = asdict(h.ctx.settings.notify) | {"rules": [before]}  # type: ignore[arg-type]
        routing = h.ctx.tokeniser.token("notify_routing", jsonio.dumps(document, sort_keys=True))
        await write(h, "notify.config", "notify", {"routing": routing})  # as that build recorded it
        await notify.record_config(h.ctx)
        async with h.ctx.pool.connection() as conn:
            rows, _ = await repo.list_audit(conn, action="notify.config")
        assert [row["detail"] for row in rows] == [{"routing": routing}]
    async with open_harness(targets, notify=settings | {"rules": [*settings["rules"], ready]}) as h:
        await notify.record_config(h.ctx)
        async with h.ctx.pool.connection() as conn:
            rows, _ = await repo.list_audit(conn, action="notify.config")
    assert len(rows) == 2 and rows[0]["detail"]["routing"] != routing
    assert rows[0]["detail"]["rules"] == [before, before | ready | {"modes": ["batch", "soak"]}]


@pytest.mark.usefixtures("loopback")
async def test_no_raw_key_reaches_a_payload(
    open_harness: OpenHarness, service: ScriptedService, tmp_path: Path
) -> None:
    """A run's raw keys (tokenised, class identifier) never reach a delivery, what a receiver gets or an audit
    row the notifier writes: events concern cycles and runs, never a key."""
    keys = ["K314", "K271"]
    for key in keys:
        service.both(key, doc(key), doc(key, status="NEW"))
    spec = spec_text(keys).replace("class: business_code", "class: identifier")
    everything = ["cycle.ended", "sample.judged", "run.finished", "run.failed", "run.stopped", "run.paused"]
    assert service.server is not None
    async with receiving(tmp_path) as (receiver, base, ref):
        targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
        async with open_harness(targets, notify=_notify(base, ref, everything)) as h:
            run_id = await create_run(h.ctx, await h.spec(spec), "batch", who=TESTER)
            assert (await h.finish(run_id))["status"] == "completed"
            stop = asyncio.Event()
            running = asyncio.create_task(worker.run_worker(h.ctx, stop=stop))
            await until(lambda: len(receiver.received) >= 2)
            stop.set()
            await running
            async with h.ctx.pool.connection() as conn:
                stored = await (await conn.execute("SELECT body FROM ds.notify_deliveries")).fetchall()
                audited = await (
                    await conn.execute("SELECT detail::text FROM ds.audit_log WHERE action LIKE 'notify.%'")
                ).fetchall()
    texts = [b for (b,) in stored] + [r.body.decode() for r in receiver.received] + [d for (d,) in audited]
    assert len(stored) >= 2 and not [key for key in keys for text in texts if key in text]


# -- schedules ----------------------------------------------------------------


async def test_two_firings_at_most(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    due = await minute(harness)
    spec_id = await harness.spec(spec_text())
    rows = [await scheduled(harness, spec_id, due, name=f"nightly-{n}") for n in range(4)]
    ids = [row["schedule_id"] for row in rows]
    firing: set[str] = set()
    fired: list[tuple[str, Any]] = []
    most, settle = 0, asyncio.Event()

    async def fire(ctx: Any, schedule_id: str, *, queued: Any = None, tried: int = 0) -> None:
        nonlocal most
        assert schedule_id not in firing  # never two for one schedule
        firing.add(schedule_id)
        fired.append((schedule_id, queued and queued[0]))
        most = max(most, len(firing))
        await settle.wait()
        await schedules.pause(ctx, schedule_id, TESTER)  # settled: no longer due
        firing.discard(schedule_id)

    monkeypatch.setattr(schedule, "fire", fire)
    sweeper = worker._Worker(harness.ctx)
    try:
        for _ in range(3):
            await sweeper._sweep_once()
            await asyncio.sleep(0.05)
        assert sorted(sweeper.firing) == ids[:2] and firing == set(ids[:2])
        # Left without a slot, each with its due time and when: the first sweep that found no slot for it.
        assert {found: at for found, (at, _) in sweeper.queued.items()} == dict.fromkeys(ids[2:], due)
        (since,) = {since for _, since in sweeper.queued.values()}
        assert due <= since < due + 2 * MINUTE
        settle.set()
        await asyncio.gather(*sweeper.firing.values())
        await sweeper._sweep_once()
        await asyncio.gather(*sweeper.firing.values())
        await sweeper._sweep_once()
    finally:
        await sweeper.close()
    assert (
        fired == [(ids[0], None), (ids[1], None), (ids[2], due), (ids[3], due)] and most == worker.FIRING == 2
    )
    assert (sweeper.firing, sweeper.queued, sweeper.waiting, sweeper.tried) == ({}, {}, {}, {})


async def test_times_lost_to_an_outage_are_down_never_busy(harness: Harness) -> None:
    due = await minute(harness) - 2 * HOUR  # no worker ran in their 60 minutes: five schedules, two slots
    spec_id = await harness.spec(spec_text())
    rows = [await scheduled(harness, spec_id, due, name=f"nightly-{n}") for n in range(5)]
    sweeper = worker._Worker(harness.ctx)
    try:
        for _ in range(4):
            await sweeper._sweep_once()
            await asyncio.gather(*sweeper.firing.values())
    finally:
        await sweeper.close()
    found = [await missed(harness, row) for row in rows]
    assert [[(d["reason"], d["code"], d["count"]) for d in details] for details in found] == [
        [("late", "down", 1)]  # those left without a slot too: their windows had closed before they were
    ] * 5
    texts = [schedules.item(await shown(harness, row["schedule_id"]))["last"]["text"] for row in rows]
    said = "(no worker that fires schedules ran, the store was down, or the clock moved)"
    assert [text.endswith(said) for text in texts] == [True] * 5
    assert (sweeper.queued, sweeper.tried) == ({}, {})


async def test_a_count_of_tries_is_its_due_time_s_own(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    schedule_id, attempts = row["schedule_id"], []

    async def unlisted(*_: Any, **__: Any) -> Any:
        attempts.append(schedule_id)
        raise ValueError("the source is down")

    async def swept() -> None:
        sweeper.waiting.pop(schedule_id, None)  # its wait is over
        await sweeper._sweep_once()
        await asyncio.gather(*sweeper.firing.values())

    monkeypatch.setattr(runs, "resolve", unlisted)
    sweeper = worker._Worker(harness.ctx)
    try:
        await swept()
        await swept()
        assert sweeper.tried == {schedule_id: (due, 2)} and await missed(harness, row) == []
        read = repo.schedules_due

        async def unread(*_: Any, **__: Any) -> Any:
            raise psycopg.DataError("a fault reading what is due")

        monkeypatch.setattr(repo, "schedules_due", unread)
        await sweeper._sweep_once()  # a sweep that could not read what is due forgets no count
        monkeypatch.setattr(repo, "schedules_due", read)
        assert sweeper.tried == {schedule_id: (due, 2)}
        names = ("name", "spec_id", "run", "cron", "time_zone", "start_within", "revision")
        await schedules.replace(harness.ctx, schedule_id, {name: row[name] for name in names}, TESTER)
        await swept()  # a person saved it: its time is settled, and its count goes with it
        assert sweeper.tried == {} and len(attempts) == 2
        await anchored(harness, schedule_id, due - MINUTE)  # as its next time comes due
        await swept()
        assert sweeper.tried == {schedule_id: (due, 1)} and len(attempts) == 3  # the first of its three
    finally:
        await sweeper.close()
    assert [d["reason"] for d in await missed(harness, row)] == ["superseded"]  # never not_started at once


async def test_firing_fault_never_fails_a_run_or_the_sweep(
    harness: Harness,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    spec_id = await harness.spec(spec_text(["K001"]))
    row = await scheduled(harness, spec_id, await minute(harness))
    due, listed = repo.schedules_due, repo.list_schedules

    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("a firing bug")

    async def unreadable(conn: Any) -> Any:
        if not faults:
            return await due(conn)
        raise faults.pop()

    faults: list[Exception] = []
    monkeypatch.setattr(schedule, "fire", broken)
    monkeypatch.setattr(repo, "schedules_due", unreadable)
    with caplog.at_level(logging.WARNING):
        run_id = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
        assert (await harness.finish(run_id))["status"] == "completed"
        assert f"schedule {row['schedule_id']} not fired: RuntimeError" in caplog.messages
        faults += [psycopg.errors.DataError("a held range no time"), KeyError("next_due")]
        monkeypatch.setattr(repo, "list_schedules", broken)  # nor does a fault of rezoning
        run_id = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
        assert (await harness.finish(run_id))["status"] == "completed"
    monkeypatch.setattr(repo, "list_schedules", listed)
    for said in ("due schedules not read: DataError", "due schedules not read: KeyError"):
        assert said in caplog.messages
    assert "schedules not rezoned: RuntimeError" in caplog.messages and faults == []
    assert not [m for m in caplog.messages if m.startswith(("sweep failed", "worker stopped"))]
    assert await started(harness, row) == []  # and the time is left due
    assert [action for _, action, _ in await audited(harness, row["schedule_id"])] == ["schedule.create"]


async def test_escaped_fault_waits_no_poll_loop(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    row = await scheduled(harness, await harness.spec(spec_text()), await minute(harness))
    schedule_id, calls = row["schedule_id"], []
    due = row["next_due"]
    answers: list[Any] = [
        psycopg.OperationalError("the store is unreachable"),
        schedule.Retry(60, 1, due),
        None,
    ]

    async def fire(_: Any, found: str, *, queued: Any = None, tried: int = 0) -> Any:
        calls.append((found, tried))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def swept(times: int = 4) -> float:
        for _ in range(times):
            await sweeper._sweep_once()
            await asyncio.sleep(0.02)
        return sweeper.waiting.get(schedule_id, 0) - time.monotonic()

    monkeypatch.setattr(schedule, "fire", fire)
    sweeper = worker._Worker(harness.ctx)
    try:
        wait = await swept()  # four sweeps, one firing: nothing loops at the poll rate
        assert calls == [(schedule_id, 0)] and schedule.RETRY_SLOW_S - 5 < wait <= schedule.RETRY_SLOW_S
        sweeper.waiting[schedule_id] = time.monotonic()  # its time has come: a try that waits, counted
        wait = await swept()
        assert (
            calls[1:] == [(schedule_id, 0)] and 55 < wait <= 60 and sweeper.tried == {schedule_id: (due, 1)}
        )
        sweeper.waiting[schedule_id] = time.monotonic()
        await swept(1)
        assert calls[2:] == [(schedule_id, 1)] and (sweeper.waiting, sweeper.tried) == ({}, {})
    finally:
        await sweeper.close()


async def test_no_lock_no_firing(harness: Harness, pg_dsn: str, caplog: pytest.LogCaptureFixture) -> None:
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)
    stop = asyncio.Event()
    caplog.set_level(logging.WARNING, logger=worker.log.name)
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as holder:
        await holder.execute("SELECT pg_advisory_lock(%s)", (worker.LOCK_ID,))  # another worker, or ds run's
        waiting = asyncio.create_task(worker.run_worker(harness.ctx, stop=stop))
        await asyncio.sleep(0.5)
        assert (
            await started(harness, row) == []
            and (await shown(harness, row["schedule_id"]))["next_due"] == due
        )
        await holder.execute("SELECT pg_advisory_unlock(%s)", (worker.LOCK_ID,))
        deadline = asyncio.get_running_loop().time() + 30
        while not await started(harness, row):  # judged once the lock is taken, inside its window
            assert asyncio.get_running_loop().time() < deadline, "timed out"
            await asyncio.sleep(0.05)
    stop.set()
    await waiting
    assert len(await started(harness, row)) == 1
    assert [r.getMessage() for r in caplog.records if r.name == worker.log.name] == [
        "another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the"
        " work, and this one takes over only if it stops"
    ]


async def test_ds_run_worker_fires_nothing(
    harness: Harness, service: ScriptedService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    due = await minute(harness)
    row = await scheduled(harness, await harness.spec(spec_text()), due)  # due as ds run's worker sweeps
    run_worker, passed, rezoned = worker.run_worker, [], []

    async def recording(ctx: Any, *, stop: Any = None, schedules: bool = True) -> None:
        passed.append(schedules)
        await run_worker(ctx, stop=stop, schedules=schedules)

    async def rezone(ctx: Any) -> bool:
        rezoned.append(ctx)
        return True

    monkeypatch.setattr("deltasleuth.runner.run_worker", recording)
    monkeypatch.setattr(schedule, "rezone", rezone)
    (tmp_path / "spec.yaml").write_text(spec_text(["K001"]))
    args = ["run", str(tmp_path / "spec.yaml"), "--config", str(tmp_path / "deltasleuth.yaml")]
    result = await asyncio.to_thread(CliRunner().invoke, app, args)
    assert result.exit_code == 0 and "completed" in result.output, result.output
    assert passed == [False] and rezoned == []  # it neither rezones nor fires
    assert await started(harness, row) == [] and (await shown(harness, row["schedule_id"]))["next_due"] == due
    assert [action for _, action, _ in await audited(harness, row["schedule_id"])] == ["schedule.create"]
