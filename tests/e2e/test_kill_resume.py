"""kill -9 of a ``ds worker`` mid-run: another worker completes the run, and no result is lost or
duplicated (a lost lease rolls its key back; an expired lease is leased again). Killed in the middle of a
file side's scan, it leaves no file reader behind."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth.connectors.tables import manifest
from deltasleuth.runner import create_run
from deltasleuth.store import repo
from mocks.registry import pair_dir
from tests.conftest import TESTER

from .conftest import finish, raw_keys, store_spec, table_spec

pytestmark = [pytest.mark.e2e, pytest.mark.db]


async def test_a_killed_worker_loses_and_duplicates_nothing(
    serve: Any, make_stack: Any, repo_root: Path
) -> None:
    stack = make_stack(serve("linker").targets(rate=20), lease_seconds=2)  # slow enough to be killed mid-run
    keys = (pair_dir("linker") / "keys.txt").read_text().split()
    async with stack.context() as ctx:
        run_id = await create_run(
            ctx, await store_spec(ctx, pair_dir("linker") / "spec.yaml"), "batch", who=TESTER
        )
        ds = "from deltasleuth.cli.main import app; app()"
        worker = await asyncio.create_subprocess_exec(
            sys.executable, "-c", ds, "worker", "--config", str(stack.settings), cwd=repo_root
        )
        done, deadline = 0, asyncio.get_running_loop().time() + 60
        while done < 5:
            assert worker.returncode is None, "the worker exited"
            assert asyncio.get_running_loop().time() < deadline, "the worker completes no key"
            await asyncio.sleep(0.05)
            async with ctx.pool.connection() as conn:
                done = (await repo.cycle_counts(conn, run_id))["keys"].get("done", 0)
        worker.kill()
        await worker.wait()
        async with ctx.pool.connection() as conn:
            killed_at = (await repo.cycle_counts(conn, run_id))["keys"].get("done", 0)
        run = await finish(ctx, run_id)  # a new worker takes over, the killed one's leases once expired
        async with ctx.pool.connection() as conn:
            results, _ = await repo.list_results(conn, run_id, limit=1000)
            states = (await repo.cycle_counts(conn, run_id))["keys"]
        raw = await raw_keys(ctx, run_id)
    assert 0 < killed_at < len(keys) and run["status"] == "completed"
    assert sorted(raw[r["key_masked"]] for r in results) == sorted(keys) and states == {"done": len(keys)}
    assert all(r["revision"] <= 2 for r in results)  # a first read, at most one recheck
    c = run["counters"]
    assert (c["total"], c["cancelled"]) == (len(keys), 0) and all(i["holds"] for i in c["reconciliation"])


def _readers(parent: int) -> list[int]:
    """The file readers a process started: its children run as a reader is."""
    listed = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    found = (line.split(None, 2) for line in listed.stdout.splitlines())
    return [int(pid) for pid, ppid, command in found if int(ppid) == parent and " -I -X utf8 " in command]


async def test_a_worker_killed_mid_scan_leaves_no_reader(
    make_stack: Any, repo_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Kill -9 of a ``ds worker`` in the middle of a scan of two file sides. Its
    reader's input ended with it, so the reader is gone; the kernel freed its spill directory's lock, and
    the next sweep removes it. Another worker resumes the scan after its checkpoint, on a reader of its
    own, and nothing is counted twice."""
    exports, rows = tmp_path_factory.mktemp("exports").resolve(), 300_000
    targets = {}
    for side in "ab":
        (exports / side).mkdir()
        lines = (f"{n:07d}|{'x' * 60}\n" for n in range(rows))
        (exports / side / "t.csv").write_text("id|v\n" + "".join(lines))
        root = str(exports / side)
        targets[f"db-{side}"] = {"engine": "files", "root": root, "settle_s": 0, "allow_writable": True}
    budget = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    stack = make_stack(yaml.safe_dump({"targets": targets}), files=budget, lease_seconds=2)
    dialect = {"columns": ["id", "v"], "delimiter": "|", "header": True}
    table = {"name": "t", "keyColumns": ["id"], "file": {"path": "t.csv", "format": "csv", "csv": dialect}}
    sides = {
        "a": {"role": "legacy", "target": "db-a", "table": table},
        "b": {"role": "new", "target": "db-b", "table": table},
    }
    key = {"name": "id", "type": "string", "class": "business_code"}
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"})
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
        ds = "from deltasleuth.cli.main import app; app()"
        worker = await asyncio.create_subprocess_exec(
            sys.executable, "-c", ds, "worker", "--config", str(stack.settings), cwd=repo_root
        )
        checkpoint, deadline = 0, asyncio.get_running_loop().time() + 120
        while checkpoint < 3:  # in the middle of its scan
            assert worker.returncode is None, "the worker exited"
            assert asyncio.get_running_loop().time() < deadline, "the worker saves no checkpoint"
            await asyncio.sleep(0.05)
            async with ctx.pool.connection() as conn:
                checkpoint = (await repo.get_run(conn, run_id))["scan"]["checkpoint"]
        (reader,) = _readers(worker.pid)
        worker.kill()
        await worker.wait()
        async with asyncio.timeout(10):
            while True:  # its input ended: so does it
                try:
                    os.kill(reader, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
        assert manifest.sweep(ctx.settings.spill_dir) == 1  # the killed worker's reader's directory
        async with ctx.pool.connection() as conn:
            killed_at = (await repo.get_run(conn, run_id))["scan"]
        run = await finish(ctx, run_id)  # a new worker takes over, on this process's reader
    scan = run["scan"]
    assert not killed_at["done"] and 0 < killed_at["rows_a"] < rows
    assert run["status"] == "completed" and (scan["rows_a"], scan["rows_b"], scan["matched"]) == (rows,) * 3
    assert scan["checkpoint"] >= rows // 10_000 and scan["snapshots_a"] == 2
