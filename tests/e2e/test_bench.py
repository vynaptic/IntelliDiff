"""The benchmark harness for real (docs/mvp/BENCHMARK.md §13): its own throwaway postgres:16-alpine (the local
image, never pulled), or the test server under --server, and the checkout's ds. ``pg_server`` gates Docker:
these skip without it, and fail under DS_REQUIRE_DB=1. With DS_PERF=1 also the creation of a run whose key
table holds a million keys."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner, Result

from deltasleuth.runner import create_run
from deltasleuth.store import repo
from tests.conftest import TESTER
from tools import bench

from .conftest import Stack, store_spec, table_spec

pytestmark = [pytest.mark.e2e, pytest.mark.db]


def run(tmp: Path, *args: str) -> Result:
    (tmp / "out").mkdir(exist_ok=True)
    (tmp / "work").mkdir(exist_ok=True)
    argv = [
        "run",
        "--out",
        str(tmp / "out"),
        "--work",
        str(tmp / "work"),
        "--repeat",
        "1",
        "--allow-dirty",
        *args,
    ]
    return CliRunner().invoke(bench.app, argv)


def kept(tmp: Path) -> tuple[Path, dict[str, Any]]:
    [path] = (tmp / "out").iterdir()  # the result file only: no temporary file is left
    return path, json.loads(path.read_text())


def leftovers(tmp: Path) -> list[str]:
    argv = ["docker", "ps", "-aq", "--filter", "label=deltasleuth.bench"]
    labelled = subprocess.run(argv, capture_output=True, text=True, check=True).stdout.split()
    return labelled + [p.name for p in (tmp / "work").iterdir()]


def test_harness_end_to_end_small(pg_server: str, tmp_path: Path) -> None:
    dirty = bool(bench._dirty(bench._env()))  # --allow-dirty: a clean checkout's file is clean
    done = run(tmp_path, "--rows", "20k")
    assert done.exit_code == 0, done.output
    assert leftovers(tmp_path) == []  # no labelled container, volume or work folder
    path, result = kept(tmp_path)
    assert re.fullmatch(r"bench-\d{8}T\d{6}Z-[0-9a-f]{12}\.json", path.name)
    assert (result["complete"], result["commit"]["dirty"], result["problems"]) == (True, dirty, [])
    said = [why for why in bench._unpublishable(result) if why != "on battery"]  # a laptop unplugged
    assert said == ["uncommitted changes"] * dirty
    [size] = result["sizes"]
    assert [r["scenario"] for r in size["scenarios"]] == ["sparse", "dense", "clustered", "graph"]
    for record in size["scenarios"]:
        [rep] = record["repetitions"]
        assert rep["correct"] and (rep["exit"], rep["status"], rep["cache"]) == (0, "completed", "restarted")
        assert (
            rep["restart_clean"] is True
            and {k: rep["observed"][k] for k in record["truth"]} == record["truth"]
        )
        s = rep["seconds"]
        graph = record["scenario"] == "graph"
        assert all(s[k] is not None for k in s if graph or k not in ("l1", "l2")), record["scenario"]
        assert s["run"] >= s["scan"] + s["reads_tail"] + s["cycle_end"] - 0.3 and s["run"] <= s["process"]
        assert (
            rep["memory"]["peak_rss_bytes"] > 10**7
            and rep["cpu"]["ds_s"] > 0
            and rep["cpu"]["database_s"] > 0
        )
        assert rep["stored"]["store_bytes"] > 0 and rep["stored"]["evidence_files"] > 0
        assert (
            rep["reads"]["keys"] == record["truth"]["candidates"]
        )  # every differing key read once on side a
        assert rep["reads"]["keys_per_s"] is None  # under 2,000 keys
        assert set(rep["sources"]["a"]) == set(bench.STATS) and rep["noise"]["slept_s"] < 5
        assert rep["temp_peak_bytes"] >= 0 and set(rep["statistics"]["tables"]) == set(bench.STORE)
        # The product analyses a new store's tables at its first cycle end: one statistics state throughout.
        assert record["summary"]["restarted"]["n"] == 1
        assert record["summary"]["restarted"]["statistics"] == "analysed", rep["statistics"]
    tables = {"users.a", "users.b.sparse", "users.b.dense", "users.b.clustered", "links.a", "links.b.graph"}
    assert set(size["tables"]) == tables
    assert result["environment"]["postgres"]["version"].startswith("16.")
    text = path.read_text()
    assert [s for s in (socket.gethostname(), getpass.getuser(), str(Path.home()), str(tmp_path), "password=")
            if s in text] == []  # fmt: skip
    assert re.search(r"^20k graph 1/1 \(restarted\): run \d+\.\d s \(scan", done.stdout, re.M)
    drafted = CliRunner().invoke(bench.app, ["report", "--draft", str(path)])
    assert drafted.exit_code == 0 and "| `dense` | 20k | 200 | restarted (only) |" in drafted.stdout


def _pairs(dsn: str, table: str) -> dict[Any, tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - a constant
    return {row if table == "links" else row[0]: row for row in rows}


def test_truth_sql_equals_python(databases: Callable[..., str]) -> None:
    """The generated tables differ exactly as truth() says: read back and merged in Python."""
    for scenario, rows, shape in (("sparse", 120_000, "narrow"), ("dense", 20_000, "wide"),
                                  ("clustered", 30_000, "narrow"), ("graph", 20_000, "narrow")):  # fmt: skip
        dsns = {
            side: databases(side, ";".join(bench._statements(side, scenario, rows, shape))) for side in "ab"
        }
        table = bench.SCENARIOS[scenario].table
        a, b = (_pairs(dsns[side], table) for side in "ab")
        found = {"rows_a": len(a), "rows_b": len(b), "matched": sum(b.get(k) == v for k, v in a.items()),
                 "MISMATCH": sum(k in b and b[k] != v for k, v in a.items()),
                 "NOT_FOUND_B": len(a.keys() - b.keys()),
                 "NOT_FOUND_A": len(b.keys() - a.keys())}  # fmt: skip
        expected = bench.truth(scenario, rows)
        assert found == {k: expected[k] for k in found}, scenario
        if scenario == "graph":
            groups = [{(t, v) for _, t, v in side} for side in (a, b)]
            assert len(groups[0]) == len(groups[1]) == expected["groups_a"]
    with psycopg.connect(databases("wide", "SELECT 1")) as conn:  # g * k holds in bigint at 2^28 and 10^9
        conn.execute(f"CREATE TABLE users ({', '.join(bench.SHAPES['wide'])})")
        row, values = (
            ", ".join(bench._row("g", "wide")),
            "(VALUES (268435456::bigint), (1000000000::bigint)) v(g)",
        )
        conn.execute(f"INSERT INTO users SELECT {row} FROM {values}")  # noqa: S608 - constants
        found = conn.execute("SELECT userid, i4, n4, d2 FROM users ORDER BY userid").fetchall()
    assert [(g, i) for g, i, *_ in found] == [(g, (4 * g) % 2147483647) for g in (268435456, 10**9)]


def test_a_paused_scan_aborts_the_repetition(
    pg_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate = bench.generate

    def limited(b: bench.Bench, side: str, scenario: str, rows: int) -> dict[str, Any]:
        facts = generate(b, side, scenario, rows)
        if side == "a":  # the scan's sort spills, and no temporary file is allowed
            with psycopg.connect(b.db.dsn(), autocommit=True) as conn:
                conn.execute(f"ALTER DATABASE {b.database('a')} SET work_mem = '64kB'")
                conn.execute(f"ALTER DATABASE {b.database('a')} SET temp_file_limit = 0")
        return facts

    monkeypatch.setattr(bench, "generate", limited)
    done = run(tmp_path, "--rows", "20k", "--scenarios", "sparse")
    assert done.exit_code == 1, done.output
    assert "20k sparse 1/1 (restarted): aborted (scan paused: ConfigurationLimitExceeded)" in done.stdout
    _, result = kept(tmp_path)
    [rep] = result["sizes"][0]["scenarios"][0]["repetitions"]
    assert (rep["aborted"], rep["correct"], result["publishable"]) == (
        "scan paused: ConfigurationLimitExceeded", False, False
    )  # fmt: skip
    assert leftovers(tmp_path) == []


def test_corrupted_truth_is_incorrect(
    pg_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth = bench.truth
    monkeypatch.setattr(bench, "truth", lambda scenario, n: truth(scenario, n) | {"MISMATCH": 1})
    monkeypatch.setattr(bench.Container, "restart", lambda self, started: False)  # as after crash recovery
    done = run(tmp_path, "--rows", "10k", "--scenarios", "sparse")
    assert done.exit_code == 1, done.output
    assert "10k sparse 1/1 (restarted): incorrect (MISMATCH 0, truth 1)" in done.stdout
    path, result = kept(tmp_path)
    assert result["publishable"] is False and "a repetition not correct" in bench._unpublishable(result)
    [rep] = result["sizes"][0]["scenarios"][0]["repetitions"]
    assert rep["problems"] == ["MISMATCH 0, truth 1", "restart was not clean (crash recovery)"]
    assert "an unclean restart" in bench._unpublishable(result)
    refused = CliRunner().invoke(bench.app, ["report", str(path)])
    assert refused.exit_code == 2 and "is not publishable (" in refused.stderr
    assert leftovers(tmp_path) == []


def _databases(admin: str) -> list[str]:
    with psycopg.connect(admin) as conn:
        return [row[0] for row in conn.execute("SELECT datname FROM pg_database WHERE NOT datistemplate")]


def test_server_mode_guard_and_clean(pg_server: str, tmp_path: Path) -> None:
    (tmp_path / "admin").write_text(f"{pg_server}\n")
    server = f"file:{tmp_path / 'admin'}"
    planted = f"planted_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{planted}"')
        conn.execute("CREATE ROLE bench_reader LOGIN PASSWORD 'reader'")
    try:
        done = run(tmp_path, "--rows", "10k", "--scenarios", "sparse", "--server", server)
        assert done.exit_code == 2 and re.fullmatch(
            r"error: the server holds \d+ other databases: give the benchmark a PostgreSQL server of its"
            r" own\n",
            done.stderr,
        )
        (tmp_path / "reader").write_text(
            make_conninfo(pg_server, user="bench_reader", password="reader") + "\n"  # noqa: S106 - a test role
        )
        done = run(
            tmp_path, "--rows", "10k", "--scenarios", "sparse", "--server", f"file:{tmp_path / 'reader'}"
        )
        assert (done.exit_code, done.stderr) == (2, (
            "error: the server's role is not a superuser: the benchmark creates, checkpoints and drops"
            " databases\n"
        ))  # fmt: skip
        assert not [name for name in _databases(pg_server) if name.startswith("dsbench_")]
    finally:
        with psycopg.connect(pg_server, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{planted}"')
            conn.execute("DROP ROLE bench_reader")
    done = run(tmp_path, "--rows", "10k", "--scenarios", "sparse", "--server", server)
    assert done.exit_code == 0, done.output
    _, result = kept(tmp_path)
    [rep] = result["sizes"][0]["scenarios"][0]["repetitions"]
    assert rep["correct"] and rep["cache"] == "first" and rep["restart_clean"] is None
    environment = result["environment"]
    assert (result["options"]["server"], environment["topology"], environment["docker"]) == (
        True,
        "loopback",
        None,
    )
    assert (
        not [name for name in _databases(pg_server) if name.startswith("dsbench_")]
        and leftovers(tmp_path) == []
    )
    with psycopg.connect(pg_server, autocommit=True) as conn:
        for name, comment in (
            ("dsbench_0123abcd_a", "deltasleuth-bench 0123abcd"),
            ("dsbench_cafebabe_a", None),
        ):
            conn.execute(f'CREATE DATABASE "{name}"')
            if comment:
                conn.execute(f"COMMENT ON DATABASE \"{name}\" IS '{comment}'")
    try:
        cleaned = CliRunner().invoke(
            bench.app, ["clean", "--server", server, "--work", str(tmp_path / "work")]
        )
        assert (cleaned.exit_code, cleaned.stdout) == (
            0,
            "removed 0 container(s), 1 database(s) and 0 work folder(s)\n",
        )
        assert [name for name in _databases(pg_server) if name.startswith("dsbench_")] == [
            "dsbench_cafebabe_a"
        ]
    finally:
        with psycopg.connect(pg_server, autocommit=True) as conn:
            for name in ("dsbench_0123abcd_a", "dsbench_cafebabe_a"):
                conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
def test_the_harness_at_a_million_rows(pg_server: str, tmp_path: Path) -> None:
    done = run(tmp_path, "--rows", "1M")
    assert done.exit_code == 0, done.output
    print(f"\n{done.stdout}")
    _, result = kept(tmp_path)
    assert all(rep["correct"] for rep in bench._repetitions(result)) and result["complete"]
    assert leftovers(tmp_path) == []


KEYS = 1_000_000  # the most a key table gives a run


@pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
async def test_million_keys(
    make_stack: Callable[..., Stack],
    databases: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key table of 1,000,000 two-part keys in PostgreSQL: a run between two services is created within
    300 s, its keys stored by one insert statement, and the work on the keys before it (the read, the rule,
    masking, sealing) never holds the event loop over a second. The statement's own parameters are adapted
    on the loop's thread: that hold is printed, not bounded. No key is read: nothing past the creation is
    measured here."""
    seed = f"SELECT 'r' || g % 1000 AS region, g AS code FROM generate_series(1, {KEYS}) g"  # noqa: S608
    (tmp_path / "dsn_keys").write_text(databases("keys", f"CREATE TABLE shelf_keys AS {seed}"))
    service = {"base_url": "http://127.0.0.1:9", "allow_private_network": True}  # never called
    targets = {"keys-db": {"dsn_ref": "file:dsn_keys", "rate_per_s": 1000}, "svc": service}
    parts = ["region", "code"]
    sides = {
        side: {
            "role": role, "target": "svc", "truncation": {"itemsPath": "$.tags", "cap": 50},
            "request": {"method": "GET", "path": "/{{ keyParts.region }}/{{ keyParts.code }}"},
        }
        for side, role in (("a", "legacy"), ("b", "new"))
    }  # fmt: skip
    source = {"kind": "table", "target": "keys-db", "table": {"name": "shelf_keys", "keyColumns": parts}}
    key = {"name": "shelf", "type": "composite", "parts": parts, "class": "business_code",
           "pattern": "^[a-z0-9]+$", "source": source}  # fmt: skip
    insert, inserted, ticks = repo._insert_keys, [], []

    async def counted(conn: repo.Conn, run_id: str, cycle: int, keys: Any, *args: Any) -> None:
        inserted.append((len(keys), time.monotonic()))
        await insert(conn, run_id, cycle, keys, *args)

    async def tick() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    monkeypatch.setattr(repo, "_insert_keys", counted)
    async with make_stack(yaml.safe_dump({"targets": targets})).context() as ctx:
        spec_id = await store_spec(ctx, table_spec(sides=sides, key=key))
        ticker = asyncio.create_task(tick())
        started = time.monotonic()
        run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
        seconds = time.monotonic() - started
        ticker.cancel()
        async with ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
    [(stored, at)] = inserted
    before = max(later - earlier for earlier, later in pairwise(ticks) if later <= at)
    held = max(later - earlier for earlier, later in pairwise(ticks))
    print(f"\n{KEYS:,} keys: created in {seconds:.1f} s, the loop held {before:.2f} s at most before the")
    print(f"insert statement and {held:.2f} s at most in all")
    assert stored == KEYS and run["params"]["keys_source"] == "table"
    assert seconds < 300 and before < 1
