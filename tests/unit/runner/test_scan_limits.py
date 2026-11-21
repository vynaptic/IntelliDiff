"""Duplicate groups retain one digest and cannot defer automatic scan limits."""

from __future__ import annotations

import asyncio
import copy
import tracemalloc
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from deltasleuth.connectors.http import Gates, TargetGate
from deltasleuth.connectors.tables.connector import scan_slots
from deltasleuth.core.types import Side, Target
from deltasleuth.runner import scan as scanning
from deltasleuth.store import repo

Rows = list[tuple[str, bytes]]
Source = Callable[[str, Any], AsyncIterator[Rows]]
DIGEST = b"d" * 32


async def test_duplicate_group_memory_does_not_grow_with_its_rows() -> None:
    async def batches() -> AsyncIterator[Rows]:
        for _ in range(200):
            yield [("same", DIGEST)] * 1000

    stream = scanning._Stream("a", {"keys": ["id"]}, None, None, None, None)
    stream._batches = batches()
    tracemalloc.start()
    try:
        rows = await stream.take(("same",))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256_000, f"one duplicate group retained {peak:,} bytes"
    assert rows == (200_000, None)


def _ranked(plan: dict[str, Any], rows: list[tuple[str, ...]], after: tuple[str, ...] | None = None) -> Any:
    """A side of ``plan`` streaming ``rows`` (each its key parts) from the resume bound ``after``."""

    async def batches() -> AsyncIterator[list[tuple[Any, ...]]]:
        yield [(*row, DIGEST) for row in rows]

    stream = scanning._Stream("a", plan, None, None, after, None)
    stream._batches = batches()
    return stream


async def _heads(stream: Any) -> list[tuple[tuple[str, ...], tuple[Any, ...]]]:
    found = []
    while (key := await stream.head()) is not None:
        found.append((key, stream.rank))
        await stream.take(key)
    return found


async def test_a_key_part_the_plan_orders_as_an_integer_ranks_by_its_value() -> None:
    """A part the plan orders as an integer ranks by its value, -5 before 9 before 10, the key's other parts
    by their texts, and so does the resume bound: a first key not ranked after it, or a text that is no whole
    number written once (it would rank alike with another), fails the run. Without the record every part
    ranks by its text."""
    plan = {"keys": ["k", "n"], "integers": [1]}
    rows = [("x", "-5"), ("x", "9"), ("x", "10"), ("x", "100"), ("y", "-7")]
    assert [rank for _, rank in await _heads(_ranked(plan, rows))] == [
        ("x", -5), ("x", 9), ("x", 10), ("x", 100), ("y", -7),
    ]  # fmt: skip
    assert [key for key, _ in await _heads(_ranked(plan, rows[2:], ("x", "9")))] == rows[2:]
    with pytest.raises(scanning.KeysOutOfOrder, match="came out of order"):
        await _heads(_ranked(plan, rows[1:], ("x", "9")))  # the bound itself
    with pytest.raises(scanning.KeysOutOfOrder, match="came out of order"):
        await _heads(_ranked({"keys": ["k", "n"]}, rows))  # by their texts, 10 is before 9
    for text in ("007", "-0", "+5", " 5", "1_000", "\u0665", "x"):
        with pytest.raises(scanning.KeysOutOfOrder) as exc:
            await _heads(_ranked(plan, [("x", "1"), ("x", text)]))
        assert str(exc.value) == "side a: a key part the plan orders as an integer is not one"  # no key


def _harness(monkeypatch: pytest.MonkeyPatch, source: Source, stop: dict[str, Any]) -> SimpleNamespace:
    run = {"scan": scanning.new_scan(), "spec_id": 1, "params": {}, "cycle": 1,
           "started_at": datetime.now(UTC)}
    plans = {side: {"keys": ["id"], "pinned": True} for side in "ab"}
    spec = SimpleNamespace(raw={"execution": {"stop": stop}, "recheck": {"delay": "0s", "times": 1}}, sides={
        side: SimpleNamespace(target=side.value) for side in Side
    })
    sealer = SimpleNamespace(seal=lambda *args: args[-1], open=lambda *args: args[-1],
                             seal_keys=lambda keys: dict(keys))
    saved: list[dict[str, Any]] = []
    keys: dict[str, str] = {}
    waits: list[Any] = []  # each save's delays
    bounds: list[Any] = []

    @asynccontextmanager
    async def connection() -> AsyncIterator[None]:
        yield None

    async def get_run(*args: Any) -> Any:
        return run

    async def get_spec(*args: Any, **kwargs: Any) -> Any:
        return spec

    async def get_sealer(*args: Any) -> Any:
        return sealer

    async def save(
        conn: Any, run_id: str, cycle: int, state: Any, found: Any, checkpoint: int, delays: Any = None
    ) -> bool:
        assert checkpoint == run["scan"]["checkpoint"]
        assert not (keys.keys() & found.keys()), "a resumed key was queued twice"
        keys.update(found)
        waits.append(delays)
        run["scan"] = copy.deepcopy(state) | {"checkpoint": checkpoint + 1}
        saved.append(copy.deepcopy(run["scan"]))
        return True

    async def status(*args: Any, **kwargs: Any) -> None:
        run.update(kwargs)

    async def cut(conn: Any, run_id: str, value: Any) -> None:
        run["keys_cut"] = value

    async def drift(*args: Any) -> bool:
        return False

    @asynccontextmanager
    async def snapshot(side: str, target: Any, plan: Any, after: Any) -> AsyncIterator[Any]:
        bounds.append((side, after))

        class Engine:
            async def scan(self, *args: Any) -> Any:
                return source(side, after)

            async def null_keys(self, *args: Any) -> int:
                return 0

        yield scanning._Stream(side, plan, Engine(), None, after, None)

    monkeypatch.setattr(repo, "get_run", get_run)
    monkeypatch.setattr(repo, "save_scan", save)
    monkeypatch.setattr(repo, "set_run_status", status)
    monkeypatch.setattr(repo, "set_keys_cut", cut)
    monkeypatch.setattr(scanning, "unsealed", lambda *args: plans)
    monkeypatch.setattr(scanning, "_snapshot", snapshot)
    monkeypatch.setattr(scanning.drift, "check", drift)
    monkeypatch.setattr(scanning, "mask_key", lambda key, *args, **kwargs: key)
    def target(target_id: str) -> Target:
        return Target(target_id, "", rate_per_s=10_000, max_concurrency=1)

    ctx = SimpleNamespace(pool=SimpleNamespace(connection=connection), spec=get_spec, sealer=get_sealer,
                          targets=SimpleNamespace(live=target), gates=Gates(), tokeniser=None)
    return SimpleNamespace(
        ctx=ctx, run=run, saved=saved, keys=keys, bounds=bounds, spec=spec, sealer=sealer, plans=plans,
        waits=waits,
    )  # fmt: skip


async def test_same_target_scan_shares_one_slot_with_key_reads_and_serialises_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    reading = peak = 0

    async def source(side: str, after: Any) -> AsyncIterator[Rows]:
        nonlocal reading, peak
        reading += 1
        peak = max(peak, reading)
        try:
            entered.set()
            await release.wait()
        finally:
            reading -= 1
        yield [("key", DIGEST)]

    harness = _harness(monkeypatch, source, {})
    harness.spec.sides[Side.B].target = "a"
    gate = harness.ctx.gates["a"] = TargetGate.for_target(harness.ctx.targets.live("a"))
    async with asyncio.timeout(5):
        async with gate.semaphore:  # a per-key connector is reading
            scan = asyncio.create_task(scanning.scan(harness.ctx, "run"))
            await asyncio.sleep(0)
            started_early = bool(harness.bounds)
        await entered.wait()
        held = gate.semaphore.locked()
        release.set()
        await scan
    assert not started_early and held and not gate.semaphore.locked()
    assert peak == 1 and harness.run["scan"]["matched"] == 1


async def test_reversed_scan_pairs_share_slots_and_release_a_partial_reservation_on_cancel() -> None:
    a, b = (Target(name, "", max_concurrency=1) for name in ("a", "b"))
    gates = Gates()
    gates.update({t.target_id: TargetGate.for_target(t) for t in (a, b)})
    entered, release = asyncio.Event(), asyncio.Event()

    async def scan(pair: tuple[Target, Target]) -> None:
        async with scan_slots(gates, pair) as serial:
            assert serial is None
            assert all(g.semaphore.locked() for g in gates.values())
            entered.set()
            await release.wait()

    async with asyncio.timeout(5):
        async with gates["b"].semaphore:  # both orders reserve A first
            reversed_scan = asyncio.create_task(scan((b, a)))
            await asyncio.sleep(0)
            assert gates["a"].semaphore.locked() and not entered.is_set()
            reversed_scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reversed_scan
            assert not gates["a"].semaphore.locked()
        forward = asyncio.create_task(scan((a, b)))
        await entered.wait()
        entered.clear()
        reverse = asyncio.create_task(scan((b, a)))
        await asyncio.sleep(0)
        assert not entered.is_set()
        release.set()
        await asyncio.gather(forward, reverse)
    assert entered.is_set() and all(not g.semaphore.locked() for g in gates.values())


@pytest.mark.parametrize("side", ["a", "b", "ab"])
async def test_max_keys_stops_after_duplicate_proof_across_batches(
    monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    fetched = {"a": 0, "b": 0}

    async def source(which: str, after: Any) -> AsyncIterator[Rows]:
        for _ in range(100 if which in side else 1):
            fetched[which] += 1
            yield [("key", DIGEST)]

    harness = _harness(monkeypatch, source, {"maxKeys": 1})
    assert await scanning.scan(harness.ctx, "run") is None
    state = harness.run["scan"]
    assert max(fetched.values()) <= 3  # the two that prove the duplicate, and one fetched ahead
    assert state["done"] and state["after"] is None and state["candidates"] == 1
    assert harness.run["keys_cut"] == {"taken": 1} and harness.keys == {"key": "key"}
    for which in "ab":
        assert state[f"rows_{which}"] == (2 if which in side else 1)
        assert state[f"repeated_{which}"] == (1 if which in side else 0)


@pytest.mark.parametrize("condition", ["until", "maxDuration"])
async def test_time_stop_inside_a_group_saves_only_complete_keys_and_resumes(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    fetched = 0
    expired = True

    async def source(side: str, after: Any) -> AsyncIterator[Rows]:
        nonlocal fetched
        if after is None:
            yield [("a", DIGEST)]
        for _ in range(20):
            fetched += 1
            yield [("b", DIGEST)] * 10
        yield [("c", DIGEST if side == "a" else b"e" * 32)]

    def expiry(*args: Any) -> str | None:
        return f"stop_condition:{condition}" if expired and fetched >= 4 else None

    monkeypatch.setattr(scanning.StopConditions, "expired", expiry)
    harness = _harness(monkeypatch, source, {})
    assert await scanning.scan(harness.ctx, "run") == f"stop_condition:{condition}"
    assert fetched <= 5
    state = harness.run["scan"]
    assert not state["done"] and state["rows_a"] == state["rows_b"] == state["matched"] == 1
    assert scanning._opened_after(harness.sealer, 1, state["after"]) == ["a"]
    expired = False
    assert await scanning.scan(harness.ctx, "run") is None
    state = harness.run["scan"]
    assert state["done"] and state["rows_a"] == state["rows_b"] == 202
    assert (state["matched"], state["candidates"], state["repeated_a"], state["repeated_b"]) == (1, 2, 1, 1)
    assert harness.keys == {"b": "b", "c": "c"}
    assert harness.bounds[-2:] == [("a", ("a",)), ("b", ("a",))]


async def test_group_summaries_preserve_classification_and_marker_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def source(side: str, after: Any) -> AsyncIterator[Rows]:
        yield [("", DIGEST), ("duplicate", DIGEST), ("duplicate", DIGEST)]
        yield [("marker", b"?")] + ([("missing", DIGEST)] if side == "a" else [])
        yield [("same", DIGEST), ("x" * 1025, DIGEST)]

    harness = _harness(monkeypatch, source, {"maxKeys": 10})
    assert await scanning.scan(harness.ctx, "run") is None
    state = harness.run["scan"]
    assert (state["rows_a"], state["rows_b"], state["skipped_a"], state["skipped_b"]) == (7, 6, 2, 2)
    assert (state["matched"], state["candidates"], state["repeated_a"], state["repeated_b"]) == (1, 3, 1, 1)
    assert harness.keys == {key: key for key in ("duplicate", "marker", "missing")}


async def test_a_long_group_checkpoints_earlier_keys_before_a_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanning, "BATCH", 10)
    failing = True

    async def source(side: str, after: Any) -> AsyncIterator[Rows]:
        if after is None:
            yield [("a", DIGEST)]
        for n in range(5):
            if failing and n == 3:
                raise ConnectionError("lost")
            yield [("b", DIGEST)] * 10

    harness = _harness(monkeypatch, source, {})
    with pytest.raises(ConnectionError, match="lost"):
        await scanning.scan(harness.ctx, "run")
    state = harness.run["scan"]
    assert state["checkpoint"] == 1 and state["rows_a"] == state["rows_b"] == 1
    assert scanning._opened_after(harness.sealer, 1, state["after"]) == ["a"]
    failing = False
    assert await scanning.scan(harness.ctx, "run") is None
    assert harness.run["scan"]["rows_a"] == harness.run["scan"]["rows_b"] == 51
    assert harness.keys == {"b": "b"}


async def test_final_key_limit_skips_unusable_duplicates_and_keeps_singleton_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def source(side: str, after: Any) -> AsyncIterator[Rows]:
        yield [("", DIGEST)] * 5
        yield [("a", DIGEST)]
        yield [("b", DIGEST)] * 20

    harness = _harness(monkeypatch, source, {"maxKeys": 1})
    assert await scanning.scan(harness.ctx, "run") is None
    state = harness.run["scan"]
    assert state["rows_a"] == state["rows_b"] == 8
    assert state["skipped_a"] == state["skipped_b"] == 5
    assert (state["matched"], state["candidates"], state["repeated_a"], state["repeated_b"]) == (1, 1, 2, 2)
    assert harness.keys == {"b": "b"}


@pytest.mark.parametrize("fixed", ["", "a", "ab"])
async def test_scan_candidates_are_due_immediately_for_their_first_observations(
    monkeypatch: pytest.MonkeyPatch, fixed: str
) -> None:
    """The recheck delay starts after the first observed comparison has produced its result."""

    async def source(side: str, after: Any) -> AsyncIterator[Rows]:
        yield [("key", DIGEST if side == "a" else b"e" * 32)]

    harness = _harness(monkeypatch, source, {})
    harness.spec.raw["recheck"] = {"delay": "2s", "times": 1}
    for side in fixed:
        harness.plans[side]["consistency"] = "fixed"
    assert await scanning.scan(harness.ctx, "run") is None
    assert harness.keys == {"key": "key"} and harness.waits == [None]


async def test_a_long_batch_is_merged_in_slices_between_which_the_event_loop_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = 50 * scanning.SLICE

    async def source(which: str, after: Any) -> AsyncIterator[Rows]:
        yield [(f"k{i:06}", DIGEST) for i in range(rows)]  # one fetched batch a side

    harness = _harness(monkeypatch, source, {})
    turns = 0

    async def other() -> None:  # whatever else the process serves meanwhile
        nonlocal turns
        while True:
            await asyncio.sleep(0)
            turns += 1

    serving = asyncio.create_task(other())
    try:
        assert await scanning.scan(harness.ctx, "run") is None
    finally:
        serving.cancel()
    assert harness.run["scan"]["matched"] == rows
    assert turns >= rows // scanning.SLICE
