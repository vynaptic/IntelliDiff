"""A measurement, run by hand (``DS_PERF=1``): one table scan of 1,000,000 rows a side, then the same scan
killed at 90% and resumed. It prints the rows a second, the time to the first row and the temporary bytes
each side's server wrote (its sort's spill), for a key a "C"-collation text primary key serves and for an
integer key, whose text order no index gives."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth.runner import scan as scanning
from deltasleuth.runner.runs import create_run
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import OpenHarness
from .test_scan import SPEC, databases  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")]
ROWS = int(os.environ.get("DS_PERF_ROWS", "1000000"))
KEYS = {
    "text": ('text COLLATE "C" PRIMARY KEY', "'ORG-' || lpad(g::text, 7, '0')"),
    "integer": ("bigint PRIMARY KEY", "g"),
}


class Killed(Exception):
    """The scan's process ended at a checkpoint."""


async def _lag(longest: list[float]) -> None:
    """The longest the event loop took to wake a 1 ms sleep, past that 1 ms."""
    while True:
        asked = time.perf_counter()
        await asyncio.sleep(0.001)
        longest[0] = max(longest[0], time.perf_counter() - asked - 0.001)


def _temp_bytes(dsn: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_stat_clear_snapshot()")
        found = conn.execute("SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database()")
        return int(found.fetchone()[0])  # type: ignore[index]


@pytest.mark.parametrize("key", list(KEYS))
async def test_a_million_row_scan_and_its_resume(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    column, value = KEYS[key]
    seed = f"""
        CREATE TABLE users (userid {column}, email text, qty int);
        INSERT INTO users SELECT {value}, 'u' || g || '@example.com', g % 97
        FROM generate_series(1, {ROWS}) g;
        ANALYZE users;
    """  # noqa: S608 (constants)
    changes = "UPDATE users SET qty = -1 WHERE email LIKE 'u%0000@%';"  # one row in 10,000
    dsns, targets = {}, {}
    for side, text in (("a", seed), ("b", seed + changes)):
        dsns[side] = databases(text)
        (tmp_path / f"dsn_{side}").write_text(dsns[side])
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    first: dict[str, float] = {}
    real_open = scanning._Stream.open

    async def open_(self: Any) -> None:  # when each side's first batch arrives: the later of the two
        await real_open(self)
        batches = self._batches

        async def timed() -> Any:
            async for batch in batches:
                first[self._side] = first.get(self._side) or time.perf_counter()
                yield batch

        self._batches = timed()

    monkeypatch.setattr(scanning._Stream, "open", open_)
    real_save = repo.save_scan
    kill_at: list[int] = []

    async def save(conn: repo.Conn, run_id: str, cycle: int, state: Any, *args: Any) -> bool:
        ok = await real_save(conn, run_id, cycle, state, *args)
        if kill_at and state["rows_a"] >= kill_at[0] and not state["done"]:
            kill_at.clear()
            raise Killed
        return ok

    monkeypatch.setattr(repo, "save_scan", save)
    measured = []
    async with open_harness(json.dumps({"targets": targets})) as h:
        spec_id = await h.spec(json.dumps(SPEC))
        for label, kill in (("whole", None), ("killed at 90%", ROWS * 9 // 10), ("resumed", None)):
            if label != "resumed":
                run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
            kill_at[:] = [kill] if kill else []
            temp = {side: _temp_bytes(dsn) for side, dsn in dsns.items()}
            first.clear()
            lag = [0.0]
            probe = asyncio.create_task(_lag(lag))
            started, cpu = time.perf_counter(), time.process_time()
            try:
                assert await scanning.scan(h.ctx, run_id) is None
            except Killed:
                pass
            took, cpu = time.perf_counter() - started, time.process_time() - cpu
            probe.cancel()
            spill = {side: _temp_bytes(dsn) - temp[side] for side, dsn in dsns.items()}
            state = (await h.run(run_id))["scan"]
            to_first = max(first.values(), default=started) - started
            measured.append((label, took, to_first, spill, state, cpu, lag[0]))
    for label, took, to_first, spill, state, cpu, longest in measured:
        rows = state["rows_a"] if label != "resumed" else ROWS - ROWS * 9 // 10
        print(
            f"\n{key} key, {label}: {took:.2f} s, {rows / took:,.0f} rows/s a side, first row after"
            f" {to_first:.2f} s, temporary bytes a {spill['a'] / 2**20:,.0f} MiB"
            f" b {spill['b'] / 2**20:,.0f} MiB, this process's CPU {cpu:.2f} s, longest event-loop stall"
            f" {longest * 1000:.0f} ms, done {state['done']}, candidates {state['candidates']}"
        )
    assert measured[0][4]["done"] and measured[2][4]["done"]
    assert measured[0][4]["candidates"] == measured[2][4]["candidates"]
