"""Two PostgreSQL tables in two databases: a scan run finds exactly the seeded
differences, reads them per key through the engine, and resumes after a kill or a pause without counting
twice. It honours maxKeys and time limits, scans composite keys, and pairs keys across encodings.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from deltasleuth.runner import run_worker
from deltasleuth.runner.runs import compare_one, create_run, pause_run, resume_run, run_status
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.e2e.conftest import Stack, both, finish, store_spec, table_spec

pytestmark = pytest.mark.e2e

ROWS = 1000
# Side a: every row. Side b, a view over a table without a key: row 5's email differs in case, row 6's
# amount only in scale, row 7 is missing, row 8 is there twice, row 1001 is extra, one row has no key.
SEED_A = f"""
    CREATE TABLE users (userid int PRIMARY KEY, email text, name text, amount numeric(10, 2),
                        legacy_flag bool);
    INSERT INTO users SELECT g, 'user' || g || '@example.com', 'Name ' || g, g * 1.25, true
    FROM generate_series(1, {ROWS}) g;
"""  # noqa: S608 (ROWS is a constant)
SEED_B = f"""
    CREATE TABLE users_raw (user_id bigint, email text, full_name text, amount numeric);
    INSERT INTO users_raw SELECT g, 'user' || g || '@example.com', 'Name ' || g, round(g * 1.25, 2)
    FROM generate_series(1, {ROWS}) g WHERE g <> 7;
    UPDATE users_raw SET email = 'USER5@example.com' WHERE user_id = 5;
    UPDATE users_raw SET amount = 7.5 WHERE user_id = 6;
    INSERT INTO users_raw SELECT * FROM users_raw WHERE user_id = 8;
    INSERT INTO users_raw VALUES ({ROWS + 1}, 'new@example.com', 'New', 1), (NULL, 'x', 'x', 0);
    CREATE VIEW users_v AS SELECT * FROM users_raw;
"""  # noqa: S608 (ROWS is a constant)


@pytest.fixture
def stack(tables: Callable[..., Stack]) -> Stack:
    return tables(SEED_A, SEED_B)


async def test_a_scan_run_finds_exactly_the_seeded_differences(stack: Stack) -> None:
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, table_spec()), "batch", who=TESTER)
        run = await finish(ctx, run_id)
        async with ctx.pool.connection() as conn:
            results = {r["key_masked"]: r for r in await repo.pages(repo.list_results, conn, run_id, 1)}
    assert run["status"] == "completed"
    assert run["params"]["tables"]["only_a"] == ["legacy_flag"]
    scan = run["scan"]
    assert (scan["done"], scan["rows_a"], scan["rows_b"], scan["skipped_a"], scan["skipped_b"]) == (
        True, ROWS, ROWS + 2, 0, 1,  # b: 1,001 keyed rows and one without a key
    )  # fmt: skip
    assert (scan["matched"], scan["candidates"]) == (ROWS - 4, 5)
    outcomes = {key: (r["outcome"], r["inconclusive_reason"]) for key, r in results.items()}
    assert outcomes == {
        "5": ("MISMATCH", None),
        "6": ("MATCH", None),  # 7.50 and 7.5 are the same number
        "7": ("NOT_FOUND_B", None),
        "8": ("INCONCLUSIVE", "identity_ambiguous"),
        str(ROWS + 1): ("NOT_FOUND_A", None),
    }
    counters = run["counters"]
    assert (counters["total"], counters["match"], counters["by_confirmation"]["bulk_exhaustive"]) == (
        ROWS + 1, ROWS - 4 + 1, ROWS - 4,
    )  # fmt: skip
    assert all(i["holds"] for i in counters["reconciliation"])


async def test_a_killed_scan_resumes_without_counting_twice(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.runner import scan as scanning

    monkeypatch.setattr(scanning, "BATCH", 100)  # checkpoints every 100 keys
    saves = 0
    real_save = repo.save_scan

    async def dying_save(*args: Any, **kwargs: Any) -> bool:
        nonlocal saves
        saves += 1
        if saves == 4:  # the worker dies before its fourth checkpoint commits
            raise psycopg.OperationalError("killed")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(repo, "save_scan", dying_save)
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, table_spec()), "batch", who=TESTER)
        run = await finish(ctx, run_id)
    assert saves > 4
    assert (run["scan"]["matched"], run["scan"]["candidates"], run["scan"]["rows_a"]) == (ROWS - 4, 5, ROWS)
    assert run["counters"]["total"] == ROWS + 1


async def test_one_key_and_explicit_keys_read_per_key(stack: Stack) -> None:
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, table_spec())
        assert (await compare_one(ctx, spec_id, "5", who=TESTER)).outcome.value == "MISMATCH"
        assert (await compare_one(ctx, spec_id, "9", who=TESTER)).outcome.value == "MATCH"
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", keys=["1", "7"], who=TESTER))
    assert run["scan"] is None
    assert run["counters"]["by_outcome"]["NOT_FOUND_B"] == 1


async def test_max_keys_caps_the_rows_to_compare(stack: Stack) -> None:
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, table_spec(execution={"stop": {"maxKeys": 2}}))
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
    scan = run["scan"]
    assert (scan["done"], scan["candidates"], run["stop_reason"]) == (True, 2, "stop_condition:maxKeys")
    assert scan["matched"] < ROWS - 4
    assert run["counters"]["total"] == scan["matched"] + 2


async def test_a_paused_scan_stops_and_resumes_to_the_same_counts(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.runner import scan as scanning

    monkeypatch.setattr(scanning, "BATCH", 100)
    saves = 0
    real_save = repo.save_scan
    async with stack.context() as ctx:

        async def pausing_save(conn: Any, run_id: str, *args: Any) -> bool:
            nonlocal saves
            saves += 1
            if saves == 3:  # the user pauses the run while it scans: this checkpoint is refused
                await pause_run(ctx, run_id, who=TESTER)
            return await real_save(conn, run_id, *args)

        monkeypatch.setattr(repo, "save_scan", pausing_save)
        run_id = await create_run(ctx, await store_spec(ctx, table_spec()), "batch", who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(ctx, stop=stop))
        try:
            async with asyncio.timeout(60):
                while await run_status(ctx, run_id) != "paused":
                    await asyncio.sleep(0.05)
        finally:
            stop.set()
            await worker
        async with ctx.pool.connection() as conn:
            paused = await repo.get_run(conn, run_id)
        assert not paused["scan"]["done"] and paused["scan"]["rows_a"] < ROWS
        await resume_run(ctx, run_id, who=TESTER)
        run = await finish(ctx, run_id)
    assert (run["scan"]["matched"], run["scan"]["candidates"], run["scan"]["rows_a"]) == (ROWS - 4, 5, ROWS)
    assert run["counters"]["total"] == ROWS + 1


# A composite key (region, code): a region holds the separator, (us|x, 1) differs and (eu, 2) is missing in b.
STOCK_A = """
    CREATE TABLE stock (region text, code int, qty int, PRIMARY KEY (region, code));
    INSERT INTO stock VALUES ('eu', 1, 10), ('eu', 2, 20), ('us|x', 1, 30);
"""
STOCK_B = """
    CREATE TABLE stock (region text, code int, qty int);
    INSERT INTO stock VALUES ('eu', 1, 10), ('us|x', 1, 31);
"""


async def test_a_composite_key_pair_scans_and_reads_per_key(tables: Callable[..., Stack]) -> None:
    doc = table_spec(
        key={"name": "sku", "type": "composite", "parts": ["region", "code"], "class": "business_code"},
        sides=both({"name": "stock", "keyColumns": ["region", "code"]}),
        security={"unlisted": "tokenize", "clear": ["$.qty"]},
    )
    async with tables(STOCK_A, STOCK_B).context() as ctx:
        spec_id = await store_spec(ctx, doc)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        async with ctx.pool.connection() as conn:
            results = {
                r["key_masked"]: r["outcome"]
                for r in await repo.pages(repo.list_results, conn, run["run_id"], 1)
            }
        one = await compare_one(ctx, spec_id, "us\\|x|1", who=TESTER)
    assert (run["scan"]["matched"], run["scan"]["candidates"]) == (1, 2)
    assert results == {"us\\|x|1": "MISMATCH", "eu|2": "NOT_FOUND_B"}
    assert one.outcome.value == "MISMATCH"


async def test_a_database_in_another_encoding_pairs_keys_in_code_point_order(
    tables: Callable[..., Stack],
) -> None:
    # EUC_JP bytes order katakana A (a5a2) before alpha (a6c1); code points, and UTF-8, order alpha first.
    seed_a = """
        CREATE TABLE users (userid text PRIMARY KEY, email text);
        INSERT INTO users VALUES ('\u30a2', 'x'), ('\u03b1', 'y');
    """
    seed_b = """
        CREATE TABLE users (userid text PRIMARY KEY, email text);
        INSERT INTO users VALUES ('\u30a2', 'x'), ('\u03b1', 'changed');
    """
    euc_jp = "ENCODING 'EUC_JP' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with tables(seed_a, seed_b, euc_jp).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
    assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == ("completed", 1, 1)
    assert run["counters"]["by_outcome"]["MISMATCH"] == 1


async def test_keys_no_run_key_can_hold_are_skipped(tables: Callable[..., Stack]) -> None:
    seed = """
        CREATE TABLE users (userid text PRIMARY KEY, email text);
        INSERT INTO users VALUES ('', 'x'), (repeat('k', 1100), 'y'), ('k1', 'z');
    """
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with tables(seed, seed).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
    scan = run["scan"]
    assert (scan["rows_a"], scan["skipped_a"], scan["skipped_b"], scan["matched"], scan["candidates"]) == (
        3, 2, 2, 1, 0,
    )  # fmt: skip
    assert (run["status"], run["counters"]["total"]) == ("completed", 1)


async def test_listing_the_key_column_alone_compares_which_keys_exist(stack: Stack) -> None:
    doc = table_spec()
    doc["sides"]["a"]["table"]["columns"] = ["userid"]
    doc["sides"]["b"]["table"]["columns"] = [{"name": "user_id", "as": "userid"}]
    async with stack.context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
    assert (run["scan"]["matched"], run["scan"]["candidates"]) == (ROWS - 2, 3)  # 7, 8 and 1001
    outcomes = run["counters"]["by_outcome"]
    assert (outcomes["MATCH"], outcomes["NOT_FOUND_A"], outcomes["NOT_FOUND_B"]) == (ROWS - 2, 1, 1)


# A column named like the row alias of the SQL (r): the whole row is still compared.
ALIAS_A = """
    CREATE TABLE t (r int PRIMARY KEY, v text); INSERT INTO t VALUES (1, 'x'), (2, 'y');
    CREATE TABLE u (id int PRIMARY KEY, r int, g int); INSERT INTO u VALUES (1, 10, 20), (2, 10, 20);
"""
ALIAS_B = """
    CREATE TABLE t (r int PRIMARY KEY, v text); INSERT INTO t VALUES (1, 'changed'), (2, 'y');
    CREATE TABLE u (id int PRIMARY KEY, r int, g int); INSERT INTO u VALUES (1, 10, 99), (2, 10, 20);
"""


@pytest.mark.parametrize(("table", "key"), [("t", "r"), ("u", "id")])
async def test_a_column_named_r_does_not_hide_the_row(
    tables: Callable[..., Stack], table: str, key: str
) -> None:
    doc = table_spec(sides=both({"name": table, "keyColumns": [key]}), security={"unlisted": "clear"})
    async with tables(ALIAS_A, ALIAS_B).context() as ctx:
        spec_id = await store_spec(ctx, doc)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        one = await compare_one(ctx, spec_id, "1", who=TESTER)
    assert (run["scan"]["matched"], run["scan"]["candidates"]) == (1, 1)
    assert (run["counters"]["by_outcome"]["MISMATCH"], one.outcome.value) == (1, "MISMATCH")


async def test_a_time_limit_stops_the_scan(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    from deltasleuth.runner import scan as scanning

    monkeypatch.setattr(scanning, "BATCH", 100)
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, table_spec(execution={"stop": {"until": "2000-01-01T00:00:00Z"}}))
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
    assert (run["status"], run["stop_reason"]) == ("completed_with_issues", "stop_condition:until")
    assert not run["scan"]["done"] and run["scan"]["rows_a"] < ROWS


async def test_a_scan_resumes_past_a_key_another_database_encoding_cannot_hold(
    tables: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.runner import scan as scanning

    # Side a is LATIN1 and holds k01..k05; side b also holds nine keys with the euro sign, which LATIN1
    # cannot encode. The scan dies after checkpointing past one of them and resumes from it.
    table = "CREATE TABLE users (userid text PRIMARY KEY, email text);"
    common = "INSERT INTO users SELECT 'k0' || g, 'x' FROM generate_series(1, 5) g;"
    euro = "INSERT INTO users SELECT '\u20ac0' || g, 'y' FROM generate_series(1, 9) g;"
    latin1 = "ENCODING 'LATIN1' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
    monkeypatch.setattr(scanning, "BATCH", 5)
    saves, real_save = 0, repo.save_scan

    async def dying_save(*args: Any, **kwargs: Any) -> bool:
        nonlocal saves
        saves += 1
        if saves == 3:  # after the checkpoint past the fifth euro key
            raise psycopg.OperationalError("killed")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(repo, "save_scan", dying_save)
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with tables(table + common, table + common + euro, latin1, lease_seconds=3).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
    assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == ("completed", 5, 9)
    assert run["counters"]["by_inconclusive_reason"]["skipped"] == 9  # side a cannot even be asked


@pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
async def test_a_million_rows_a_side_finish_in_a_minute(tables: Callable[..., Stack]) -> None:
    rows = 1_000_000
    seed = f"""
        CREATE TABLE users (userid bigint PRIMARY KEY, email text, name text, amount numeric(12, 2));
        INSERT INTO users SELECT g, 'user' || g || '@example.com', 'Name ' || g, g * 1.25
        FROM generate_series(1, {rows}) g;
    """  # noqa: S608 (rows is a constant)
    changes = """
        UPDATE users SET email = upper(email) WHERE userid % 100000 = 0;
        DELETE FROM users WHERE userid = 3;
    """
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with tables(seed, seed + changes).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
        started = time.monotonic()
        run = await finish(ctx, run_id, timeout=300)
        elapsed = time.monotonic() - started
    print(f"\n{rows} rows a side: {elapsed:.1f} s")
    assert (run["scan"]["matched"], run["scan"]["candidates"]) == (rows - 11, 11)
    assert run["counters"]["by_outcome"]["MISMATCH"] == 10
    assert elapsed < 60


@pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
async def test_many_differing_rows_are_read_in_batches(tables: Callable[..., Stack]) -> None:
    # 20,000 of 100,000 rows differ; each is read, confirmed and saved. Before batched reads (with a
    # recheck pass each) this took 205 s; batched it takes 43 s on a laptop.
    rows = 100_000
    seed = f"""
        CREATE TABLE users (userid bigint PRIMARY KEY, email text, name text, amount numeric(12, 2));
        INSERT INTO users SELECT g, 'user' || g || '@example.com', 'Name ' || g, g * 1.25
        FROM generate_series(1, {rows}) g;
    """  # noqa: S608 (rows is a constant)
    changes = "UPDATE users SET email = upper(email) WHERE userid % 5 = 0;"
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}), execution={"concurrency": 8})
    async with tables(seed, seed + changes).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
        started = time.monotonic()
        run = await finish(ctx, run_id, timeout=600)
        elapsed = time.monotonic() - started
    print(f"\n{rows // 5} differing rows of {rows}: {elapsed:.1f} s")
    assert run["counters"]["by_outcome"]["MISMATCH"] == rows // 5
    assert run["counters"]["by_confirmation"]["rechecked"] == rows // 5
    assert elapsed < 60
