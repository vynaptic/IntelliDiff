"""Duplicate groups retain one digest and cannot defer automatic scan limits."""

from __future__ import annotations

import copy
import tracemalloc
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from deltasleuth.core.types import Side
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


def _harness(monkeypatch: pytest.MonkeyPatch, source: Source, stop: dict[str, Any]) -> SimpleNamespace:
    run = {"scan": scanning.new_scan(), "spec_id": 1, "params": {}, "cycle": 1,
           "started_at": datetime.now(UTC)}
    plans = {side: {"keys": ["id"], "pinned": True} for side in "ab"}
    spec = SimpleNamespace(raw={"execution": {"stop": stop}}, sides={
        side: SimpleNamespace(target=side.value) for side in Side
    })
    sealer = SimpleNamespace(seal=lambda *args: args[-1], open=lambda *args: args[-1],
                             seal_keys=lambda keys: dict(keys))
    saved: list[dict[str, Any]] = []
    keys: dict[str, str] = {}
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

    async def save(conn: Any, run_id: str, cycle: int, state: Any, found: Any, checkpoint: int) -> bool:
        assert checkpoint == run["scan"]["checkpoint"]
        assert not (keys.keys() & found.keys()), "a resumed key was queued twice"
        keys.update(found)
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
    monkeypatch.setattr(scanning, "mask_key", lambda key, *args: key)
    ctx = SimpleNamespace(pool=SimpleNamespace(connection=connection), spec=get_spec, sealer=get_sealer,
                          targets=SimpleNamespace(live=lambda target: target), tokeniser=None)
    return SimpleNamespace(ctx=ctx, run=run, saved=saved, keys=keys, bounds=bounds, spec=spec, sealer=sealer)


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
    assert max(fetched.values()) <= 2
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
