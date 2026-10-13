"""Schedules end to end (docs/mvp/SCHEDULES.md §15): the real worker starts a schedule's runs. Three due
times of a clean table pair each start one batch run, and the three runs meet the pair's convergence gate
(a scan is a census; the mock pairs list their keys inline, which no convergence counts); and a ``ds
worker`` killed after it started a mock pair's run loses and duplicates nothing: one run for the due time,
which another worker completes."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from deltasleuth import readiness, schedules
from deltasleuth.context import Context
from deltasleuth.core import cron
from deltasleuth.runner import run_worker
from deltasleuth.store import repo
from mocks.registry import pair_dir
from tests.conftest import TESTER

from .conftest import Stack, finish, raw_keys, store_spec, table_spec

pytestmark = [pytest.mark.e2e, pytest.mark.db]
DS = "from deltasleuth.cli.main import app; app()"
ROWS = 40
SEED_A = f"""
    CREATE TABLE users (userid int PRIMARY KEY, email text, name text, amount numeric(10, 2));
    INSERT INTO users SELECT g, 'user' || g || '@example.com', 'Name ' || g, g * 1.25
    FROM generate_series(1, {ROWS}) g;
"""  # noqa: S608 (ROWS is a constant)
SEED_B = f"""
    CREATE TABLE users_v (user_id bigint PRIMARY KEY, email text, full_name text, amount numeric);
    INSERT INTO users_v SELECT g, 'user' || g || '@example.com', 'Name ' || g, g * 1.25
    FROM generate_series(1, {ROWS}) g;
"""  # noqa: S608 (ROWS is a constant)


async def nightly(ctx: Context, spec_id: int) -> str:
    """A schedule of the spec's batch runs, declared as a person declares one; its id."""
    declared = {"name": "nightly", "spec_id": spec_id, "run": {"mode": "batch"}}
    row, _ = await schedules.create(ctx, declared | {"cron": "0 2 * * *", "time_zone": "UTC"}, TESTER)
    return row["schedule_id"]


async def due_now(ctx: Context, schedule_id: str, minutes: int) -> datetime:
    """The schedule's cron set to the daily time ``minutes`` before the database's minute, and its anchor
    and next due time back-dated to it by SQL: that time is due now, inside its window."""
    async with ctx.pool.connection() as conn:
        now = (await repo.count_schedules(conn))["now"].replace(second=0, microsecond=0)
        at = now - timedelta(minutes=minutes)
        await conn.execute(
            "UPDATE ds.schedules SET cron = %s, anchor_at = %s, next_due = %s WHERE schedule_id = %s",
            (f"{at.minute} {at.hour} * * *", at - timedelta(minutes=1), at, schedule_id),
        )
    return at


async def started(ctx: Context, schedule_id: str) -> list[repo.Row]:
    """The runs the schedule started, oldest first."""
    async with ctx.pool.connection() as conn:
        return (await repo.pages(repo.list_runs, conn, schedule_id=schedule_id))[::-1]


async def audited(ctx: Context, schedule_id: str) -> list[str]:
    async with ctx.pool.connection() as conn:
        rows = await repo._rows(
            conn, "SELECT action FROM ds.audit_log WHERE object = %s ORDER BY id", (schedule_id,)
        )
    return [row["action"] for row in rows]


async def waited(read: Callable[[], Any], what: str, timeout: float = 90) -> Any:
    """What ``read()`` finds, once it finds something."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not (found := await read()):
        assert asyncio.get_running_loop().time() < deadline, what
        await asyncio.sleep(0.05)
    return found


async def test_three_scheduled_runs_of_a_clean_pair_meet_the_convergence_gate(
    tables: Callable[..., Stack],
) -> None:
    async with tables(SEED_A, SEED_B).context() as ctx:
        spec_id = await store_spec(ctx, table_spec())
        schedule_id = await nightly(ctx, spec_id)
        stop, dues = asyncio.Event(), []
        worker = asyncio.create_task(run_worker(ctx, stop=stop))  # as ds worker runs it: it fires schedules
        try:
            for round_ in range(3):  # a daily time at a different minute just past, each round
                at = await due_now(ctx, schedule_id, 3 - round_)
                dues.append(cron.utc(at))

                async def ended(count: int = round_ + 1) -> bool:
                    runs = await started(ctx, schedule_id)
                    return len(runs) == count and runs[-1]["status"] in repo.TERMINAL

                await waited(ended, f"no run ended for {dues[-1]}")

            async def converged() -> dict[str, Any] | None:
                found = await readiness.pair(ctx, "users-tables")
                gates = {gate["gate"]: gate for gate in found["gates"]}
                return found | {"by": gates} if gates["convergence"]["status"] == "met" else None

            pair = await waited(converged, "the three runs do not converge")
        finally:
            stop.set()
            await worker
        runs, actions = await started(ctx, schedule_id), await audited(ctx, schedule_id)
        async with ctx.pool.connection() as conn:
            schedule = await repo.get_schedule(conn, schedule_id)
    assert [(run["status"], run["mode"], run["created_by"]) for run in runs] == [
        ("completed", "batch", "system:worker")
    ] * 3
    assert [run["params"]["schedule"]["due"] for run in runs] == dues and len(set(dues)) == 3
    assert all(run["counters"]["match"] == run["counters"]["total"] == ROWS for run in runs)
    assert actions == ["schedule.create", "schedule.fire", "schedule.fire", "schedule.fire"]  # none missed
    assert (schedule["last_run_id"], schedule["last"]["outcome"]) == (runs[-1]["run_id"], "fire")  # type: ignore[index]
    assert pair["by"]["convergence"]["text"] == (
        f"the newest 3 evidence cycles of 1.0.0 (spec {spec_id}) meet every per-cycle gate: at least the 3"
        " the gate needs"
    )
    assert (pair["state"], pair["blocking"], pair["evidence"]["run_id"], pair["evidence"]["basis"]) == (
        "parity_ready",
        [],
        runs[-1]["run_id"],
        "census",
    )


async def test_a_killed_worker_loses_and_duplicates_nothing(
    serve: Any, make_stack: Any, repo_root: Path
) -> None:
    stack = make_stack(serve("linker").targets(rate=20), lease_seconds=2)  # slow enough to be killed mid-run
    keys = (pair_dir("linker") / "keys.txt").read_text().split()
    async with stack.context() as ctx:
        schedule_id = await nightly(ctx, await store_spec(ctx, pair_dir("linker") / "spec.yaml"))
        at = await due_now(ctx, schedule_id, 1)
        worker = await asyncio.create_subprocess_exec(
            sys.executable, "-c", DS, "worker", "--config", str(stack.settings), cwd=repo_root,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip

        async def reading() -> str | None:
            """The run the worker started for the due time, once it completed some of its keys."""
            assert worker.returncode is None, "the worker exited"
            runs = await started(ctx, schedule_id)
            if not runs:
                return None
            async with ctx.pool.connection() as conn:
                done = (await repo.cycle_counts(conn, runs[0]["run_id"]))["keys"].get("done", 0)
            return runs[0]["run_id"] if done >= 5 else None

        try:
            run_id = await waited(reading, "the worker started no run")
        finally:
            worker.kill()  # kill -9, mid-run
            await worker.wait()
        async with ctx.pool.connection() as conn:
            killed_at = (await repo.cycle_counts(conn, run_id))["keys"].get("done", 0)
        run = await finish(ctx, run_id)  # another worker takes over: it fires schedules too, and none is due
        async with ctx.pool.connection() as conn:
            results, _ = await repo.list_results(conn, run_id, limit=1000)
            schedule = await repo.get_schedule(conn, schedule_id)
        raw = await raw_keys(ctx, run_id)
        runs, actions = await started(ctx, schedule_id), await audited(ctx, schedule_id)
    assert 0 < killed_at < len(keys) and run["status"] == "completed"
    assert [(r["run_id"], r["params"]["schedule"]["due"]) for r in runs] == [(run_id, cron.utc(at))]
    assert sorted(raw[r["key_masked"]] for r in results) == sorted(keys)  # no key lost, none twice
    assert run["counters"]["total"] == len(keys) and run["counters"]["cancelled"] == 0
    assert actions == ["schedule.create", "schedule.fire"]  # fired once, nothing missed
    assert (schedule["anchor_at"], schedule["next_due"], schedule["last_run_id"]) == (  # type: ignore[index]
        at,
        at + timedelta(days=1),
        run_id,
    )
