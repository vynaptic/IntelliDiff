"""Workers beside the one that sweeps (``ds worker --slots-only``): each registers and drops out once it stops
beating; each takes its share of every target's rate budget and in-flight cap, so the sum holds the target's
own; a Retry-After or an open breaker one worker meets pauses the others; a stop condition is judged from
every worker's saved groups; every key gets one result, as one worker gives it; and a slots-only worker never
sweeps or takes the lock."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from aiohttp import web
from typer.testing import CliRunner

from deltasleuth.cli.main import app
from deltasleuth.config import load_settings
from deltasleuth.connectors.breaker import CircuitBreaker
from deltasleuth.connectors.http import Gates, TargetGate, share
from deltasleuth.context import Context
from deltasleuth.runner import create_run, fleet, run_worker, worker
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import UNAVAILABLE, Harness, OpenHarness, ScriptedService, doc, spec_text, until

TARGETS = """\
targets:
  svc-a:
    base_url: {base}
    allow_private_network: true
    allow_insecure_http: true
    rate_per_s: {rate}
    max_concurrency: {cap}
    capabilities:
      authoritative_absence: {{status: [404]}}
  svc-b:
    base_url: {base}
    allow_private_network: true
    allow_insecure_http: true
    rate_per_s: {rate}
    max_concurrency: {cap}
    capabilities:
      authoritative_absence: {{status: [404]}}
"""


class Counted(ScriptedService):
    """The scripted service that counts the requests in flight at once (``most``) and, with ``slow_down``
    set, answers the first side-a request it gets, whatever its key, 429 with that many seconds of
    Retry-After."""

    def __init__(self) -> None:
        super().__init__()
        self.flight = self.most = 0
        self.slow_down: int | None = None

    async def _key(self, request: web.Request) -> web.Response:
        if request.match_info["side"] == "a" and self.slow_down is not None:
            wait, self.slow_down = self.slow_down, None
            self.log.append(("a", request.match_info["key"], asyncio.get_running_loop().time()))
            return web.json_response({"error": "SLOW_DOWN"}, status=429, headers={"Retry-After": str(wait)})
        self.flight += 1
        self.most = max(self.most, self.flight)
        try:
            return await super()._key(request)
        finally:
            self.flight -= 1


@pytest.fixture
async def services() -> AsyncIterator[tuple[Counted, Counted]]:
    """Two services alike, one for each worker: what each receives is what that worker sent."""
    pair = (Counted(), Counted())
    for svc in pair:
        await svc.start()
    yield pair
    for svc in pair:
        assert svc.server is not None
        await svc.server.close()


def _targets(svc: ScriptedService, rate: float = 500, cap: int = 4) -> str:
    assert svc.server is not None
    return TARGETS.format(base=f"http://127.0.0.1:{svc.server.port}", rate=rate, cap=cap)


@asynccontextmanager
async def beside(tmp_path: Path, targets: str, name: str = "2") -> AsyncIterator[Context]:
    """Another process's Context on the harness's database, with the same settings, workspace and evidence
    folder, and ``targets`` as its targets file: the same targets at its own service."""
    settings = json.loads((tmp_path / "deltasleuth.yaml").read_text())
    (tmp_path / f"targets-{name}.yaml").write_text(targets)
    path = tmp_path / f"deltasleuth-{name}.yaml"
    path.write_text(json.dumps(settings | {"targets_file": f"targets-{name}.yaml"}))
    async with Context.open(load_settings(path)) as ctx:
        yield ctx


@asynccontextmanager
async def working(sweeping: Context, *slots_only: Context) -> AsyncIterator[None]:
    """The sweeping worker of ``sweeping`` and a slots-only worker of each of ``slots_only``, once all have
    registered, until the block ends."""
    stop = asyncio.Event()
    tasks = [asyncio.create_task(run_worker(sweeping, stop=stop))]
    tasks += [asyncio.create_task(run_worker(ctx, stop=stop, slots_only=True)) for ctx in slots_only]
    everyone = (sweeping, *slots_only)
    try:
        await until(lambda: all(ctx.gates.count == len(everyone) for ctx in everyone))
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks)


def _script(services: tuple[ScriptedService, ...], keys: list[str], **replies: Any) -> None:
    """Every key the same on both sides at each service, but those ``replies`` names (key -> side a's)."""
    for svc in services:
        for key in keys:
            svc.both(key, replies.get(key, doc(key)), doc(key))


# -- shares --------------------------------------------------------------------------------------------


def test_a_cap_is_shared_out_whole_in_the_workers_order() -> None:
    """The parts of a cap sum to it, the first ranks taking what does not divide; a worker past the cap gets
    none. The rate is divided by the count, and a gate made after a share takes it too."""
    assert [share(4, 3, rank) for rank in range(3)] == [2, 1, 1]
    assert [share(2, 3, rank) for rank in range(3)] == [1, 1, 0]
    assert all(sum(share(cap, n, r) for r in range(n)) == cap for cap in range(1, 9) for n in range(1, 6))
    gates = Gates()
    gate = gates["svc-a"] = TargetGate(20, 4)
    assert (gate.max_concurrency, gate.limiter.time_period) == (4, 1 / 20)
    gates.share(3, 2)
    assert (gate.max_concurrency, gate.limiter.time_period) == (1, 3 / 20)
    gate.rebudget(30, 2)  # a target changed: its new budget, still shared
    assert (gate.max_concurrency, gate.limiter.time_period) == (0, 3 / 30) and gate.semaphore.locked()
    gates.share(1, 0)
    assert (gate.max_concurrency, gate.limiter.time_period) == (2, 1 / 30)


def test_a_pause_and_an_opening_reach_another_process_s_gates() -> None:
    """What one process's gates met (a Retry-After, an open breaker) is untold once; another process's gate
    and its breaker of the same declaration hold it, and tell it no further."""
    first, second = Gates(), Gates()
    for gates in (first, second):
        gates["svc-a"] = TargetGate(20, 4)
    declared = (0.5, 2, 30.0)
    opened, closed = CircuitBreaker(*declared), CircuitBreaker(*declared)
    first["svc-a"]._breakers[declared] = opened
    second["svc-a"]._breakers[declared] = closed
    first["svc-a"].not_before = time.monotonic() + 5  # a Retry-After of 5 s
    opened.record("call", True)
    opened.record("call", True)
    told = first.untold()
    assert set(told) == {("svc-a", ""), ("svc-a", "0.5/2/30.0")} and 4 < told[("svc-a", "")] <= 5
    assert first.untold() == {}  # told once
    second.hold([{"target_id": t, "breaker": b, "left_s": s} for (t, b), s in told.items()])
    second.hold([{"target_id": "elsewhere", "breaker": "", "left_s": 9.0}])  # no gate of it here
    assert 4 < second["svc-a"].paused_for() <= 5 and closed.state == "open" and closed.admit() is None
    assert 29 < closed.closed_for() <= 30 and second.untold() == {}


# -- registration --------------------------------------------------------------------------------------


async def test_a_worker_that_stops_beating_drops_out(harness: Harness) -> None:
    """The sweep's holder ranks first, the latest registered of two holders first; the others by when each
    registered. A registration that expired is dropped by the next beat, and one that leaves goes at once."""
    async with harness.ctx.pool.connection() as conn:
        assert await repo.beat_worker(conn, "slots-1", False, 30) == ["slots-1"]
    async with harness.ctx.pool.connection() as conn:
        assert await repo.beat_worker(conn, "sweep-1", True, 0.3) == ["sweep-1", "slots-1"]
    async with harness.ctx.pool.connection() as conn:
        assert await repo.beat_worker(conn, "sweep-2", True, 30) == ["sweep-2", "sweep-1", "slots-1"]
    await asyncio.sleep(0.5)
    async with harness.ctx.pool.connection() as conn:
        assert [r["worker_id"] for r in await repo.live_workers(conn)] == ["sweep-2", "slots-1"]
        assert await repo.beat_worker(conn, "slots-1", False, 30) == ["sweep-2", "slots-1"]
        await repo.leave_worker(conn, "sweep-2")
        assert await repo.beat_worker(conn, "slots-1", False, 30) == ["slots-1"]


async def test_a_killed_worker_s_share_returns_to_the_others(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose registration expires (a kill) leaves its share to the others within the expiry."""
    monkeypatch.setattr(fleet, "EXPIRES_S", 0.5)
    monkeypatch.setattr(fleet, "BEAT_S", 0.05)
    async with harness.ctx.pool.connection() as conn:
        await repo.beat_worker(conn, "killed", False, 0.5)
    roster = fleet.Roster(harness.ctx, sweep=True)
    await roster.beat()
    assert (harness.ctx.gates.count, harness.ctx.gates.rank) == (2, 0)
    beating = asyncio.create_task(roster.beating())
    try:
        await until(lambda: harness.ctx.gates.count == 1, timeout=5)
    finally:
        beating.cancel()
        await roster.leave()
    async with harness.ctx.pool.connection() as conn:
        assert await repo.live_workers(conn) == []


# -- workers beside the sweep ---------------------------------------------------------------------------


async def test_every_key_gets_one_result_as_one_worker_gives_it(
    open_harness: OpenHarness, services: tuple[Counted, Counted], tmp_path: Path
) -> None:
    """A sweeping worker and a slots-only one share a run's keys: each key is read once a pass, by one of
    them, and gets one result, the one a worker alone gives."""
    keys = [f"K{n:03}" for n in range(1, 61)]
    differ = {key: doc(key, status="GONE") for key in keys[::7]}
    _script(services, keys, **differ)
    for svc in services:
        svc.slow = 0.01
    async with open_harness(_targets(services[0])) as h, beside(tmp_path, _targets(services[1])) as other:
        spec_id = await h.spec(spec_text(keys))
        async with working(h.ctx, other):
            shared = await create_run(h.ctx, spec_id, "batch", who=TESTER)
            run = await h.wait(shared)
        first, second = (svc.calls.copy() for svc in services)  # the shared run's reads
        alone = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        assert (await h.finish(alone))["status"] == run["status"] == "completed"
        both, single = await h.results(shared), await h.results(alone)
    assert sorted(both) == keys

    def seen(results: dict[str, repo.Row]) -> dict[str, tuple[Any, ...]]:
        shown = ("outcome", "first_outcome", "revision")
        return {k: (*(r[name] for name in shown), len(r["findings"])) for k, r in results.items()}

    assert seen(both) == seen(single)
    assert {k for k, r in both.items() if r["outcome"] == "MISMATCH"} == set(differ)
    for side in "ab":  # each pass of a key read once, by one worker or the other
        read = [k for svc in (first, second) for (s, k), n in svc.items() if s == side for _ in range(n)]
        assert sorted(read) == sorted([*keys, *differ])  # the differing keys' recheck too
    assert all(sum(n for (s, _), n in calls.items() if s == "a") > 5 for calls in (first, second))
    counters = run["counters"]
    assert counters["total"] == len(keys) and all(i["holds"] for i in counters["reconciliation"])


async def test_a_target_s_budget_holds_across_workers(
    open_harness: OpenHarness, services: tuple[Counted, Counted], tmp_path: Path
) -> None:
    """Two workers read a target of 20 requests a second, two at once: the sum of what both send stays
    within it, at the services, while each sends its part."""
    keys = [f"K{n:03}" for n in range(1, 81)]
    _script(services, keys)
    for svc in services:
        svc.slow = 0.05
    spec = spec_text(keys, execution="concurrency: 4")
    async with (
        open_harness(_targets(services[0], rate=20, cap=2)) as h,
        beside(tmp_path, _targets(services[1], rate=20, cap=2)) as other,
    ):
        assert (h.ctx.gates.count, other.gates.count) == (1, 1)
        spec_id = await h.spec(spec)
        async with working(h.ctx, other):
            run = await h.wait(await create_run(h.ctx, spec_id, "batch", who=TESTER), timeout=60)
            gates = [ctx.gates["svc-a"] for ctx in (h.ctx, other)]
            assert sorted(g.max_concurrency for g in gates) == [1, 1]
    assert run["status"] == "completed"
    arrivals = sorted(t for svc in services for side, _, t in svc.log if side == "a")
    assert len(arrivals) == len(keys)
    most = max(sum(1 for t in arrivals if start <= t < start + 1) for start in arrivals)
    assert 15 <= most <= 22  # 20 a second, and the first token of each worker's budget
    assert all(len([1 for side, *_ in svc.log if side == "a"]) >= 20 for svc in services)
    assert all(svc.most <= 2 for svc in services)
    assert services[0].most + services[1].most <= 2 + 2  # each side at most one at once a worker


async def test_a_retry_after_one_worker_meets_pauses_the_other(
    open_harness: OpenHarness,
    services: tuple[Counted, Counted],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target that answers one worker's first read 429 with a Retry-After of 3 s gets nothing more from
    either worker until it ends: the other worker learns it at its next beat, and both read on after it."""
    monkeypatch.setattr(fleet, "BEAT_S", 0.1)
    keys = [f"K{n:03}" for n in range(1, 121)]
    _script(services, keys)
    for svc in services:
        svc.slow = 0.01
    met, heard = services
    met.slow_down = 3
    async with (
        open_harness(_targets(services[0], rate=40)) as h,
        beside(tmp_path, _targets(services[1], rate=40)) as other,
    ):
        spec_id = await h.spec(spec_text(keys))
        async with working(h.ctx, other):
            run = await h.wait(await create_run(h.ctx, spec_id, "batch", who=TESTER), timeout=60)
    assert run["status"] == "completed"
    at = next(t for side, _, t in met.log if side == "a")
    sent = [t - at for side, _, t in heard.log if side == "a"]
    assert [t for t in sent if 0.5 < t < 2.8] == []  # paused once its next beat came
    assert [t for t in sent if t > 2.8]  # with keys left to read once it ended


async def test_a_stop_condition_is_judged_across_the_workers(
    open_harness: OpenHarness, services: tuple[Counted, Counted], tmp_path: Path
) -> None:
    """Side a fails every key: 30 INCONCLUSIVE first reads stop the run, though neither worker read 30, and
    neither worker's slots read on once the sweep stopped it."""
    keys = [f"K{n:03}" for n in range(1, 101)]
    for svc in services:
        for key in keys:
            svc.script("a", key, UNAVAILABLE)
            svc.script("b", key, (200, doc(key)))
        svc.slow = 0.02
    stop = "concurrency: 1, stop: {maxInconclusiveRate: {rate: 0.5, window: 30}}"
    spec = spec_text(keys, execution=stop).replace("recheck: {delay: 0s,", "recheck: {delay: 60s,")
    async with open_harness(_targets(services[0])) as h, beside(tmp_path, _targets(services[1])) as other:
        spec_id = await h.spec(spec)
        async with working(h.ctx, other):
            run = await h.wait(await create_run(h.ctx, spec_id, "batch", who=TESTER), timeout=60)
        async with h.ctx.pool.connection() as conn:
            fed = await repo.take_stop_feed(conn)
    stopped = ("completed_with_issues", "stop_condition:maxInconclusiveRate")
    assert (run["status"], run["stop_reason"]) == stopped
    firsts = [sum(1 for side, *_ in svc.log if side == "a") for svc in services]
    assert all(0 < n < 30 for n in firsts) and 30 <= sum(firsts) < len(keys)
    assert run["counters"]["cancelled"] > 0 and fed == []


async def test_a_slots_only_worker_never_sweeps(
    harness: Harness, service: ScriptedService, pg_dsn: str
) -> None:
    """A slots-only worker reads and saves a run's keys but takes no lock: no cycle ends and no sweep is
    recorded until a worker that sweeps comes; the worker lock stays free for it."""
    keys = ["K001", "K002", "K003"]
    _script((service,), keys)
    run_id = await create_run(harness.ctx, await harness.spec(spec_text(keys)), "batch", who=TESTER)
    stop = asyncio.Event()
    only = asyncio.create_task(run_worker(harness.ctx, stop=stop, slots_only=True))
    try:
        await until(lambda: len(service.calls) == 2 * len(keys))
        await asyncio.sleep(0.5)
        async with harness.ctx.pool.connection() as conn:
            assert (await repo.cycle_counts(conn, run_id))["keys"] == {"done": 3}
            assert await repo.last_sweep(conn) is None
        assert (await harness.run(run_id))["status"] == "running"
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as other:
            assert await repo.try_worker_lock(other)  # free: no slots-only worker holds it
    finally:
        stop.set()
        await only
    assert (await harness.finish(run_id))["status"] == "completed"


async def test_a_slots_only_worker_refuses_an_evidence_folder_it_does_not_share(
    harness: Harness, service: ScriptedService, tmp_path: Path
) -> None:
    """``ds worker --slots-only`` whose evidence folder lacks the evidence the store names exits 2 with one
    line, before it registers: it would write evidence the others never read."""
    service.both("K001", doc("K001"), doc("K001"))
    run_id = await create_run(harness.ctx, await harness.spec(spec_text(["K001"])), "batch", who=TESTER)
    assert (await harness.finish(run_id))["status"] == "completed"
    settings = json.loads((tmp_path / "deltasleuth.yaml").read_text())
    path = tmp_path / "elsewhere.yaml"
    path.write_text(json.dumps(settings | {"evidence_dir": "evidence-elsewhere"}))
    args = ["worker", "--slots-only", "--config", str(path)]
    result = await asyncio.to_thread(CliRunner().invoke, app, args)
    assert result.exit_code == 2, result.output
    assert result.output.splitlines()[-1] == (
        f"error: the evidence folder {tmp_path / 'evidence-elsewhere'} does not hold the evidence the store"
        " names: a worker beside others needs the folder they use (evidence_dir, shared with them)"
    )
    async with harness.ctx.pool.connection() as conn:
        assert await repo.live_workers(conn) == []
    assert worker.LOCK_ID == repo.WORKER_LOCK  # the sweep's lock is the one it was


async def test_a_new_store_s_keys_are_analysed_as_a_worker_loads_a_run(harness: Harness) -> None:
    """A worker that loads a run of a store whose keys were never analysed analyses them, so that a key of
    a group is found by its own index, not among all of its cycle's keys; with statistics, it leaves them."""
    planned = (
        "EXPLAIN (FORMAT JSON) SELECT 1 FROM ds.run_keys"
        " WHERE run_id = %s AND cycle = 1 AND key_masked = 'K1'"
    )
    async with harness.ctx.pool.connection() as conn:
        await conn.execute("ALTER TABLE ds.run_keys SET (autovacuum_enabled = off)")
    keys = [f"K{n:03}" for n in range(1, 301)]
    run_id = await create_run(harness.ctx, await harness.spec(spec_text(keys)), "batch", who=TESTER)
    stats = "SELECT count(*) FROM pg_stats WHERE schemaname = 'ds' AND tablename = 'run_keys'"
    async with harness.ctx.pool.connection() as conn:
        assert (await (await conn.execute(stats)).fetchone())[0] == 0  # type: ignore[index]
    sweeper = worker._Worker(harness.ctx)
    try:
        await sweeper.slots._load(run_id)
    finally:
        await sweeper.close()
    async with harness.ctx.pool.connection() as conn:
        assert (await (await conn.execute(stats)).fetchone())[0] > 0  # type: ignore[index]
        [[plan]] = await (await conn.execute(planned, (run_id,))).fetchall()  # type: ignore[arg-type]
        assert not await repo.analyse_keys(conn)  # analysed already
    node = plan[0]["Plan"]
    assert "key_masked" in node["Index Cond"] and "Filter" not in node
