"""Workers beside the one that sweeps (``ds worker --slots-only``), over the generated sources: the seeded
discrepancies and nothing else, as one worker finds them; and a slots-only worker killed with keys in flight
loses only its leases, which the sweeping worker completes once they expire, nothing lost or duplicated."""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.runner import create_run, run_worker
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.support import records

from .conftest import finish, raw_keys, store_spec
from .test_rest_sources import DEFAULT, key_problems

pytestmark = [pytest.mark.e2e, pytest.mark.db]


@pytest.mark.parametrize("pair", records.PAIRS)
async def test_slot_workers_find_the_seeded_discrepancies(serve: Any, make_stack: Any, pair: str) -> None:
    """The sweeping worker and two slots-only workers, each a Context of its own: every key gets one result,
    and the results are the test fixture's, as a worker alone gives them."""
    expected = records.expected(pair)
    stack = make_stack(serve(pair).targets())
    async with AsyncExitStack() as contexts:
        ctx, *others = [await contexts.enter_async_context(stack.context()) for _ in range(3)]
        spec_id = await store_spec(ctx, records.spec(pair))
        stop = asyncio.Event()
        beside = [asyncio.create_task(run_worker(o, stop=stop, slots_only=True)) for o in others]
        try:
            run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        finally:
            stop.set()
            await asyncio.gather(*beside)
        assert all(o.gates.count == 1 for o in others)  # each left as it stopped
        async with ctx.pool.connection() as conn:
            results = await repo.pages(repo.list_results, conn, run["run_id"])
            findings = await repo.pages(repo.list_findings, conn, run["run_id"])
            observations = await repo.list_observations(conn, run["run_id"])
            assert await repo.live_workers(conn) == []
        raw = await raw_keys(ctx, run["run_id"])
    keys = records.keys(pair)
    expect = {key: expected.get(key, {}).get("live", DEFAULT) for key in keys}
    by_key = {
        raw[r["key_masked"]]: r | {"findings": [f for f in findings if f["key_masked"] == r["key_masked"]]}
        for r in results
    }
    problems = [
        f"{key}: {p}"
        for key in keys
        for p in key_problems(
            by_key[key], expect[key], [o for o in observations if raw[o["key_masked"]] == key], True
        )
    ]
    assert problems == [] and sorted(by_key) == sorted(keys)
    c = run["counters"]
    outcomes = Counter(e["outcome"] for e in expect.values())
    assert {k: v for k, v in c["by_outcome"].items() if v} == dict(outcomes)
    assert (c["cancelled"], c["outstanding"], run["status"]) == (0, 0, "completed")
    assert all(i["holds"] for i in c["reconciliation"])


async def test_a_killed_slot_worker_loses_only_its_leases(
    serve: Any, make_stack: Any, repo_root: Path
) -> None:
    """kill -9 of a ``ds worker --slots-only`` that holds leases: the sweeping worker reads those keys again
    once the leases expire, every key gets one result, and the killed worker's registration expires, its share
    of each target's budget back with the sweeping worker."""
    stack = make_stack(serve("group").targets(rate=10), lease_seconds=2)  # slow enough to be killed mid-run
    keys = records.keys("group")
    async with stack.context() as ctx:
        run_id = await create_run(
            ctx, await store_spec(ctx, records.spec("group")), "batch", who=TESTER
        )
        ds = "from deltasleuth.cli.main import app; app()"
        slots = await asyncio.create_subprocess_exec(
            sys.executable, "-c", ds, "worker", "--slots-only", "--config", str(stack.settings), cwd=repo_root
        )
        owned = f"%:{slots.pid}:%"
        held = "SELECT count(*) FROM ds.run_keys WHERE state = 'leased' AND lease_owner LIKE %s"
        stop = asyncio.Event()
        sweeping = asyncio.create_task(run_worker(ctx, stop=stop))
        try:
            leased, deadline = 0, asyncio.get_running_loop().time() + 60
            while not (leased and ctx.gates.count == 2):  # both registered, and it holds keys
                assert slots.returncode is None, "the slots-only worker exited"
                assert asyncio.get_running_loop().time() < deadline, "the slots-only worker leases no key"
                await asyncio.sleep(0.05)
                async with ctx.pool.connection() as conn:
                    leased = (await (await conn.execute(held, (owned,))).fetchone())[0]  # type: ignore[index]
            slots.kill()
            await slots.wait()
            run = await asyncio.wait_for(_ended(ctx, run_id), 120)  # the expired leases taken again
            await asyncio.wait_for(_alone(ctx), 15)
        finally:
            stop.set()
            await sweeping
        async with ctx.pool.connection() as conn:
            results, _ = await repo.list_results(conn, run_id, limit=1000)
            states = (await repo.cycle_counts(conn, run_id))["keys"]
            assert (await (await conn.execute(held, (owned,))).fetchone())[0] == 0  # type: ignore[index]
        raw = await raw_keys(ctx, run_id)
    assert run["status"] == "completed" and states == {"done": len(keys)}
    assert sorted(raw[r["key_masked"]] for r in results) == sorted(keys)
    assert all(r["revision"] <= 2 for r in results)  # a first read, at most one recheck
    c = run["counters"]
    assert (c["total"], c["cancelled"]) == (len(keys), 0) and all(i["holds"] for i in c["reconciliation"])


async def _ended(ctx: Any, run_id: str) -> repo.Row:
    """The run's row once it ended."""
    while True:
        async with ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
        if run["status"] in repo.TERMINAL:
            return run
        await asyncio.sleep(0.05)


async def _alone(ctx: Any) -> None:
    """Until the killed worker's registration expired and this process holds every target's whole budget."""
    while ctx.gates.count != 1:
        await asyncio.sleep(0.1)
