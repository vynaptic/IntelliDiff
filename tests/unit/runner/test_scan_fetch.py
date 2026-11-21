"""How a scan pass fetches: both sides' next batches at once, each while the merge reads the one before, none
still in flight when a side's session closes; and a PostgreSQL side is told its server's character set, by
which it orders and resumes its key texts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.connectors.postgres import engine as postgresql
from deltasleuth.connectors.postgres import queries
from deltasleuth.runner import scan as scanning
from deltasleuth.runner.runs import create_run
from tests.conftest import TESTER

from .conftest import OpenHarness
from .test_scan import CHANGES, ROWS, SEED, SPEC, databases  # noqa: F401

pytestmark = pytest.mark.db


async def _pair(open_harness: OpenHarness, databases: Callable[[str], str], tmp_path: Path) -> dict[str, Any]:  # noqa: F811
    targets = {}
    for side, seed in (("a", SEED), ("b", SEED + CHANGES)):
        (tmp_path / f"dsn_{side}").write_text(databases(seed))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    return {"targets": targets}


def _slow_batches(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, str]]) -> dict[str, int]:
    """Each side's batches arrive 20 ms after they are asked for; ``events`` records each fetch's start and
    end and each session's close, by side. Returns the most fetches in flight at once."""
    flight = {"now": 0, "most": 0}
    real_open, real_snapshot = scanning._Stream.open, scanning._snapshot

    async def open_(self: Any) -> None:
        await real_open(self)
        batches, side = self._batches, self._side

        async def slow() -> Any:
            while True:
                events.append(("fetch", side))
                flight["now"] += 1
                flight["most"] = max(flight["most"], flight["now"])
                try:
                    await asyncio.sleep(0.02)
                    batch = await anext(batches, None)
                finally:
                    flight["now"] -= 1
                    events.append(("fetched", side))
                if batch is None:
                    return
                yield batch

        self._batches = slow()

    def snapshot(side: str, *args: Any) -> Any:
        opened = real_snapshot(side, *args)

        class Recorded:
            async def __aenter__(self) -> Any:
                return await opened.__aenter__()

            async def __aexit__(self, *exc: Any) -> Any:
                events.append(("closed", side))
                return await opened.__aexit__(*exc)

        return Recorded()

    monkeypatch.setattr(scanning._Stream, "open", open_)
    monkeypatch.setattr(scanning, "_snapshot", snapshot)
    return flight


async def test_both_sides_fetch_their_next_batch_at_once(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanning, "BATCH", 10)
    monkeypatch.setattr(postgresql, "BATCH", 10)  # and as many rows a fetch
    events: list[tuple[str, str]] = []
    flight = _slow_batches(monkeypatch, events)
    async with open_harness(json.dumps(await _pair(open_harness, databases, tmp_path))) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
        assert await scanning.scan(h.ctx, run_id) is None
        state = (await h.run(run_id))["scan"]
    assert (state["done"], state["rows_a"], state["rows_b"], state["candidates"]) == (True, ROWS, ROWS, 2)
    assert flight["most"] == 2  # a side's fetch overlapped the other's


async def test_a_pass_that_stops_early_leaves_no_fetch_in_flight_when_its_sessions_close(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanning, "BATCH", 10)
    monkeypatch.setattr(postgresql, "BATCH", 10)
    events: list[tuple[str, str]] = []
    _slow_batches(monkeypatch, events)
    spec = SPEC | {"execution": {"stop": {"maxKeys": 1}}}  # the first differing key ends the pass
    async with open_harness(json.dumps(await _pair(open_harness, databases, tmp_path))) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(spec)), "batch", who=TESTER)
        assert await scanning.scan(h.ctx, run_id) is None
        state = (await h.run(run_id))["scan"]
    assert (state["done"], state["candidates"]) == (True, 1)
    for side in "ab":
        mine = [kind for kind, who in events if who == side]
        closed = mine.index("closed")
        assert mine[:closed].count("fetch") == mine[:closed].count("fetched"), mine


async def test_a_postgresql_side_is_told_its_servers_character_set(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    told: list[Any] = []
    real = queries.scan_sql

    def scan_sql(side: Any, after: Any) -> Any:
        told.append(side.get("charset"))
        return real(side, after)

    monkeypatch.setattr(queries, "scan_sql", scan_sql)
    async with open_harness(json.dumps(await _pair(open_harness, databases, tmp_path))) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
        assert await scanning.scan(h.ctx, run_id) is None
    assert told == ["UTF8", "UTF8"]
