"""Limits and concurrency (docs/mvp/MCP.md §10): each caller's calls a minute and its compares and readiness
judgements, the process's slots and a caller's share of them, each with its message, on a clock the test
moves; a compare that its caller's cancel does not leave queued; and a cancelled read whose statement does
not run on in the store."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import anyio
import psycopg
import pytest
from mcp import Client

from deltasleuth.mcp import server, tools
from deltasleuth.store import repo
from tests.unit.runner.conftest import until

from .conftest import EVERY, OPERATOR, VIEWER, World, ask, audited, token


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_rate_slides_and_forgets_the_callers_longest_idle() -> None:
    clock = Clock()
    rate = tools.Rate(2, window=60, size=2, clock=clock)
    assert [rate.hit("a"), rate.hit("a"), rate.hit("a")] == [True, True, False]
    clock.now += 59.9
    assert rate.hit("a") is False  # refused, and not counted: the caller is served once the minute has slid
    clock.now += 0.1
    assert [rate.hit("a"), rate.hit("a"), rate.hit("a")] == [True, True, False]
    assert rate.hit("b") and rate.hit("c") and len(rate._keys) == 2  # a, the longest idle, is forgotten
    assert [rate.hit("a"), rate.hit("a"), rate.hit("a")] == [True, True, False]
    assert len(tools.Limits(EVERY).calls._keys) == 0 and tools.Limits(EVERY).calls.size == 10_000


async def test_a_callers_calls_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """T5: every call counts, a refused one too; and T6: ten compares and ten readiness judgements."""
    clock = Clock()
    limits = tools.Limits(replace(EVERY, calls_per_minute=3), clock=clock)
    over = ("rate limited: at most 3 MCP tool calls a minute per caller", True)
    assert [await ask(None, VIEWER, "nope", limits=limits) for _ in range(3)] == [
        ("unknown tool 'nope'", True)
    ] * 3  # type: ignore[arg-type]
    assert await ask(None, VIEWER, "nope", limits=limits) == over  # type: ignore[arg-type]
    assert (await ask(None, OPERATOR, "nope", limits=limits))[0] == "unknown tool 'nope'"  # type: ignore[arg-type]
    clock.now += 60
    assert (await ask(None, VIEWER, "nope", limits=limits))[0] == "unknown tool 'nope'"  # type: ignore[arg-type]

    async def run(ctx: Any, asked: Any, **args: Any) -> Any:
        return {}

    for name in tools.LIMITED:
        monkeypatch.setitem(tools.CATALOGUE, name, replace(tools.CATALOGUE[name], run=run))
    limits = tools.Limits(replace(EVERY, calls_per_minute=600), clock=clock)
    asked = {"compare_key": {"run_id": "R-1", "key": "k"}, "get_readiness": {}}
    assert tools.LIMITED == ("compare_key", "get_readiness") and tools.COMPARE_LIMIT == 10
    for name, args in asked.items():
        said = (f"rate limited: at most 10 {name} calls a minute per caller", True)
        assert await ask(None, OPERATOR, name, {"junk": 1}, limits=limits) != said  # type: ignore[arg-type]
        answers = [await ask(None, OPERATOR, name, args, limits=limits) for _ in range(11)]  # type: ignore[arg-type]
        assert answers == [({}, False)] * 10 + [said]  # bad arguments (T4) were not counted
        assert await ask(None, token("operator", 9), name, args, limits=limits) == ({}, False)  # type: ignore[arg-type]
    clock.now += 60
    assert await ask(None, OPERATOR, "compare_key", asked["compare_key"], limits=limits) == ({}, False)  # type: ignore[arg-type]


async def test_slots_and_a_callers_share_of_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """T7: ``mcp.concurrency`` calls at once, one caller holding all but one of them at most."""
    held, entered = asyncio.Event(), 0

    async def run(ctx: Any, asked: Any, **args: Any) -> Any:
        nonlocal entered
        entered += 1
        await held.wait()
        return {"runs": [], "next_cursor": None}

    monkeypatch.setitem(tools.CATALOGUE, "list_runs", replace(tools.CATALOGUE["list_runs"], run=run))
    monkeypatch.setattr(tools, "WAIT", 0.05)
    limits = tools.Limits(replace(EVERY, concurrency=3))
    a, b, c = (token("viewer", n) for n in (11, 12, 13))

    def calling(who: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(ask(None, who, "list_runs", limits=limits))  # type: ignore[arg-type]

    first, second = calling(a), calling(a)
    await asyncio.sleep(0.01)
    assert entered == 2  # a holds its share: two of three
    assert await calling(a) == ("busy: 2 of your MCP tool calls are running; call again", True)
    third = calling(b)
    await asyncio.sleep(0.01)
    assert entered == 3 and await calling(c) == ("busy: 3 MCP tool calls are running; call again", True)
    monkeypatch.setattr(tools, "WAIT", 5.0)
    waiting = calling(c)  # a slot given back within the wait is taken
    await asyncio.sleep(0.01)
    first.cancel()  # a cancelled call gives its slot back
    assert (await asyncio.gather(first, return_exceptions=True))[0].__class__ is asyncio.CancelledError
    await asyncio.sleep(0.01)
    assert entered == 4
    held.set()
    empty = ({"runs": [], "next_cursor": None}, False)
    assert await asyncio.gather(second, third, waiting) == [empty] * 3 and not limits._running
    alone = tools.Limits(replace(EVERY, concurrency=1))  # one slot: its one caller may hold it
    held.clear()
    only = asyncio.create_task(ask(None, a, "list_runs", limits=alone))  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    monkeypatch.setattr(tools, "WAIT", 0.05)
    assert await ask(None, a, "list_runs", limits=alone) == (  # type: ignore[arg-type]
        "busy: 1 of your MCP tool calls are running; call again", True,
    )  # fmt: skip
    assert await ask(None, b, "list_runs", limits=alone) == (  # type: ignore[arg-type]
        "busy: 1 MCP tool calls are running; call again", True,
    )  # fmt: skip
    held.set()
    assert await only == empty and tools.WAIT == 0.05 and EVERY.concurrency == 4


@pytest.mark.db
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_cancelled_compare_finishes_its_run(world: World, mode: str) -> None:
    """A client's cancel cannot leave a one-shot run queued: the compare is shielded, and ends its run."""
    ctx, asked = world.ctx, {"run_id": world.run_id, "key": world.key}
    world.service.slow, read = 0.3, len(world.service.log)
    async with Client(server.build(ctx, EVERY, who=OPERATOR), mode=mode) as client:
        async with anyio.create_task_group() as calls:
            calls.start_soon(client.call_tool, "compare_key", asked)
            await until(lambda: len(world.service.log) > read)  # a side is being read: mid-compare
            calls.cancel_scope.cancel()  # the shield holds the cancel off until the run has ended
    world.service.slow = 0.0
    async with ctx.pool.connection() as conn:
        runs = [run for run in (await repo.list_runs(conn))[0] if run["mode"] == "one_shot"]
    assert [run["status"] for run in runs] == ["completed"]  # ended, not queued for the purge to fail
    finished = [row for row in await audited(ctx, "run.finished") if row["object"] == runs[0]["run_id"]]
    assert [(row["actor"], row["detail"]["via"]) for row in finished] == [(OPERATOR.actor, "mcp:http")]


@pytest.mark.db
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_cancelled_call_cancels_its_statement(world: World, mode: str) -> None:
    """A call cancelled while PostgreSQL runs its statement leaves no backend running it on: the library
    cancels through an anyio scope, under which psycopg's own cancel request is never sent, so the server
    sends it from a shielded scope before the pool takes the connection back, closes it and opens another."""
    ctx = world.ctx
    waiting = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        " AND wait_event_type = 'Lock'"
    )
    async with (
        await psycopg.AsyncConnection.connect(ctx.pool.conninfo) as lock,
        await psycopg.AsyncConnection.connect(ctx.pool.conninfo, autocommit=True) as watch,
        Client(server.build(ctx, EVERY, who=VIEWER), mode=mode) as client,
    ):

        async def waits(backends: int) -> None:
            """Until ``backends`` of the store's wait for a lock."""
            deadline = asyncio.get_running_loop().time() + 10
            while await (await watch.execute(waiting)).fetchone() != (backends,):
                assert asyncio.get_running_loop().time() < deadline, f"not {backends} waiting"
                await asyncio.sleep(0.02)

        await lock.execute("LOCK TABLE ds.runs IN ACCESS EXCLUSIVE MODE")
        for _ in range(3):  # each on a connection the pool opened for the one before
            async with anyio.create_task_group() as calls:
                calls.start_soon(client.call_tool, "list_runs", {})
                await waits(1)  # its statement waits for the table
                calls.cancel_scope.cancel()
            await waits(0)
        await lock.rollback()
        assert not (await client.call_tool("list_runs", {})).is_error  # on a connection the pool opened
