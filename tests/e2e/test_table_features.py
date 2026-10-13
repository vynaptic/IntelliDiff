"""Table pairs with row filters, key maps and type maps, a soak that scans its tables every cycle, and
batched per-key reads, each run through ``create_run`` and the worker."""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth.context import Context
from deltasleuth.reports.csv import write_csv
from deltasleuth.reports.data import load_run
from deltasleuth.runner import run_worker
from deltasleuth.runner.runs import compare_one, create_run
from deltasleuth.runner.worker import READ_BATCH
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.e2e.conftest import Stack, both, finish, store_spec, table_spec

pytestmark = pytest.mark.e2e


async def results(ctx: Context, run_id: str, cycle: int = 1) -> dict[str, repo.Row]:
    """The cycle's results by key (a business code, shown as it is), each with its ``findings``."""
    async with ctx.pool.connection() as conn:
        rows = await repo.pages(repo.list_results, conn, run_id, cycle)
        findings = await repo.pages(repo.list_findings, conn, run_id, cycle)
    return {
        r["key_masked"]: r | {"findings": [f for f in findings if f["key_masked"] == r["key_masked"]]}
        for r in rows
    }


def scanned(run: repo.Row) -> tuple[int, ...]:
    scan = run["scan"]
    return tuple(
        scan[name] for name in ("rows_a", "rows_b", "skipped_a", "skipped_b", "matched", "candidates")
    )


# --- row filters ---------------------------------------------------------------------------------

# On side b, customer 2 closed (it leaves b's population), 3 is missing, 4 is closed on both sides (in
# neither population), and 5's email changed.
CUSTOMERS_A = """
    CREATE TABLE customers (customer_id int PRIMARY KEY, status text, email text);
    INSERT INTO customers VALUES (1, 'active', 'a@x'), (2, 'active', 'b@x'), (3, 'dormant', 'c@x'),
                                 (4, 'closed', 'd@x'), (5, 'active', 'e@x');
"""
CUSTOMERS_B = """
    CREATE TABLE customers (customer_id int PRIMARY KEY, status text, email text);
    INSERT INTO customers VALUES (1, 'active', 'a@x'), (2, 'closed', 'b@x'), (4, 'closed', 'd@x'),
                                 (5, 'active', 'E@x');
"""
WHERE = "status IN ('active', 'dormant')"


async def test_a_row_that_leaves_one_side_s_filter_is_filter_membership_never_missing(
    tables: Callable[..., Stack],
) -> None:
    doc = table_spec(
        sides=both({"name": "customers", "keyColumns": ["customer_id"], "where": WHERE}),
        security={"unlisted": "tokenize", "clear": ["$.status"]},
    )
    async with tables(CUSTOMERS_A, CUSTOMERS_B).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        coverage = next(
            csv.DictReader(io.StringIO((await write_csv(ctx, run["run_id"], None, "coverage")).decode()))
        )
    assert scanned(run) == (4, 2, 0, 0, 1, 3)  # each side's population only
    assert {key: r["outcome"] for key, r in found.items()} == {
        "2": "MISMATCH",
        "3": "NOT_FOUND_B",
        "5": "MISMATCH",
    }
    moved = {
        (f["taxonomy_code"], f["path_pattern"], f["change_type"], f["severity"])
        for f in found["2"]["findings"]
    }
    assert moved == {
        ("FILTER_MEMBERSHIP", "$", "value_changed", "MEDIUM"),
        ("VALUE_MISMATCH", "$.status", "value_changed", "MEDIUM"),
    }
    assert (coverage["filter_a"], coverage["filter_b"]) == (WHERE, WHERE)  # the predicates as written


# --- key maps -------------------------------------------------------------------------------

ENTITIES_A = """
    CREATE TABLE entities (code text PRIMARY KEY, name text);
    INSERT INTO entities VALUES ('ORG-000001', 'Acme'), ('ORG-000002', 'Bolt'), ('ORG-000123', 'Crane'),
                                ('ORG-000200', 'Delta');
"""
ENTITIES_B = """
    CREATE TABLE entities (code int PRIMARY KEY, name text);
    INSERT INTO entities VALUES (1, 'Acme'), (2, 'Bolt Ltd'), (123, 'Crane'), (300, 'Echo'), (-5, 'Minus');
"""
ACCOUNTS_A = """
    CREATE TABLE entities (code text PRIMARY KEY, name text);
    INSERT INTO entities VALUES ('A1', 'x'), ('A2', 'y'), ('A42', 'z');
"""
ACCOUNTS_B = """
    CREATE TABLE entities (code text PRIMARY KEY, name text);
    INSERT INTO entities VALUES ('acct-1', 'x'), ('acct-2', 'changed'), ('acct-42', 'z'), ('junk', 'w');
"""
PREFIX_PAD = {"kind": "prefix_pad", "prefix": "ORG-", "width": 6, "sideType": "integer"}
REGEX = {
    "kind": "regex",
    "toCanonical": {"pattern": "acct-([0-9]+)", "replacement": "A$1"},
    "toSide": {"pattern": "A([0-9]+)", "replacement": "acct-$1"},
    "sideType": "string",
}


@pytest.mark.parametrize(
    ("seed_a", "seed_b", "key_map", "counts", "outcomes", "same"),
    [
        (
            ENTITIES_A,
            ENTITIES_B,
            PREFIX_PAD,
            (4, 5, 0, 1, 2, 3),  # b's -5 matches no canonical key
            {"ORG-000002": "MISMATCH", "ORG-000200": "NOT_FOUND_B", "ORG-000300": "NOT_FOUND_A"},
            "ORG-000123",
        ),
        (ACCOUNTS_A, ACCOUNTS_B, REGEX, (3, 4, 0, 1, 2, 1), {"A2": "MISMATCH"}, "A42"),
    ],
    ids=["prefix_pad", "regex"],
)
async def test_a_key_map_pairs_side_values_with_their_canonical_keys(
    tables: Callable[..., Stack],
    seed_a: str,
    seed_b: str,
    key_map: dict[str, Any],
    counts: tuple[int, ...],
    outcomes: dict[str, str],
    same: str,
) -> None:
    doc = table_spec(
        key={"name": "code", "type": "string", "class": "business_code"},
        sides={
            "a": {"role": "legacy", "target": "db-a", "table": {"name": "entities", "keyColumns": ["code"]}},
            "b": {
                "role": "new",
                "target": "db-b",
                "table": {"name": "entities", "keyColumns": [{"name": "code", "keyMap": key_map}]},
            },
        },
    )
    async with tables(seed_a, seed_b).context() as ctx:
        spec_id = await store_spec(ctx, doc)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        one = await compare_one(ctx, spec_id, same, who=TESTER)  # a per-key read maps the canonical key back
    assert scanned(run) == counts
    assert {key: r["outcome"] for key, r in found.items()} == outcomes
    assert one.outcome.value == "MATCH"


# --- type maps ------------------------------------------------------------------------------

# Side a is a legacy table: Y/N flags, naive timestamps and unrounded amounts. Side b: booleans,
# instants and two decimals. Row 3's flag is neither Y nor N, and row 4's differs.
LEDGER_A = """
    CREATE TABLE ledger (id int PRIMARY KEY, active text, opened timestamp, balance numeric);
    INSERT INTO ledger VALUES (1, 'Y', '2024-05-01 12:00:00', 10.504),
                              (2, 'N', '2024-05-02 08:30:00.123456', 3.14159),
                              (3, 'maybe', '2024-05-03 00:00:00', 1), (4, 'Y', '2024-05-04 00:00:00', 2);
"""
LEDGER_B = """
    CREATE TABLE ledger (id int PRIMARY KEY, active boolean, opened timestamptz, balance numeric(12, 2));
    INSERT INTO ledger VALUES (1, true, '2024-05-01 12:00:00+00', 10.50),
                              (2, false, '2024-05-02 10:30:00.123456+02', 3.14),
                              (3, true, '2024-05-03 00:00:00+00', 1), (4, false, '2024-05-04 00:00:00+00', 2);
"""


async def test_type_maps_make_legacy_values_compare_equal(tables: Callable[..., Stack]) -> None:
    shared = {"opened": {"as": "timestamp"}, "balance": {"as": "decimal", "scale": 2}}
    doc = table_spec(sides=both({"name": "ledger", "keyColumns": ["id"]}), security={"unlisted": "clear"})
    doc["sides"]["a"]["typeMap"] = shared | {
        "active": {"as": "boolean", "trueValues": ["Y"], "falseValues": ["N"]}
    }
    doc["sides"]["b"]["typeMap"] = shared
    async with tables(LEDGER_A, LEDGER_B).context() as ctx:
        spec_id = await store_spec(ctx, doc)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        one = await compare_one(ctx, spec_id, "2", who=TESTER)
    assert (run["scan"]["matched"], run["scan"]["candidates"]) == (2, 2)
    assert {key: (r["outcome"], r["inconclusive_reason"]) for key, r in found.items()} == {
        "3": ("INCONCLUSIVE", "unparseable"),
        "4": ("MISMATCH", None),
    }
    assert found["3"]["attrs"]["side_a_detail"] == "typeMap: column 'active' does not convert to boolean"
    assert one.outcome.value == "MATCH"  # the per-key read converts as the scan does


# --- a soak over a scan ---------------------------------------------------------------------

USERS = """
    CREATE TABLE users (userid int PRIMARY KEY, email text);
    INSERT INTO users SELECT g, 'u' || g FROM generate_series(1, 10) g;
"""
FIVE_DIFFERS = "UPDATE users SET email = 'x' WHERE userid = 5;"
NINE_DIFFERS = (
    "UPDATE users SET email = 'u5' WHERE userid = 5; UPDATE users SET email = 'x' WHERE userid = 9;"
)


async def test_a_soak_over_two_tables_scans_them_anew_every_cycle(
    tables: Callable[..., Stack], tmp_path: Path
) -> None:
    doc = table_spec(
        sides=both({"name": "users", "keyColumns": ["userid"]}),
        execution={"cycleInterval": "3s", "stop": {"maxCycles": 2}},
    )
    async with tables(USERS, USERS + FIVE_DIFFERS).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "soak", who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(ctx, stop=stop))
        try:
            async with asyncio.timeout(30):
                while True:
                    async with ctx.pool.connection() as conn:
                        opened = await repo.get_run(conn, run_id)
                    if opened["cycle"] == 2:
                        break
                    await asyncio.sleep(0.02)
            with psycopg.connect((tmp_path / "dsn_b").read_text(), autocommit=True) as conn:
                conn.execute(NINE_DIFFERS)  # between the cycles, 5 is fixed and 9 starts to differ
            due = datetime.fromisoformat(opened["scan"]["due"])
            assert datetime.now(UTC) < due, "cycle 1 took longer than the cycle interval"
        finally:
            stop.set()
            await worker
        run = await finish(ctx, run_id)
        first, second = await results(ctx, run_id, 1), await results(ctx, run_id, 2)
        async with ctx.pool.connection() as conn:
            changes = await repo.outcome_changes(conn, run_id)
            reads = await repo.list_observations(conn, run_id, 2)
            first_scan = await repo.get_scan(conn, run_id, 1)
        was, now = (await load_run(ctx, run_id, cycle=1)).counters, (await load_run(ctx, run_id)).counters
    assert (run["status"], run["cycle"], run["stop_reason"]) == ("completed", 2, "stop_condition:maxCycles")
    assert {k: r["outcome"] for k, r in first.items()} == {"5": "MISMATCH"}
    assert {k: r["outcome"] for k, r in second.items()} == {"5": "MATCH", "9": "MISMATCH"}
    assert [(c["cycle"], c["key_masked"], c["from_outcome"], c["to_outcome"]) for c in changes] == [
        (2, "5", "MISMATCH", "MATCH")
    ]
    assert (first_scan["matched"], run["scan"]["matched"]) == (9, 8)  # 5 had a result: read again
    assert all(r["observed_at"] >= due for r in reads)  # the second scan waited for its cycle
    assert [(c["total"], c["match"], c["by_confirmation"]["bulk_exhaustive"]) for c in (was, now)] == [
        (10, 9, 9),
        (10, 9, 8),
    ]


# --- batched per-key reads ------------------------------------------------------------------

ROWS = 500
MANY = f"""
    CREATE TABLE users (userid int PRIMARY KEY, email text);
    INSERT INTO users SELECT g, 'u' || g FROM generate_series(1, {ROWS}) g;
"""  # noqa: S608 (ROWS is a constant)
# Every even row differs and rows 1 and 3 are missing: more keys to read than one group holds.
MANY_CHANGES = (
    "UPDATE users SET email = upper(email) WHERE userid % 2 = 0; DELETE FROM users WHERE userid IN (1, 3);"
)


async def test_many_differing_keys_are_read_in_groups_and_scanned_keys_confirmed_in_one_pass(
    tables: Callable[..., Stack],
) -> None:
    assert ROWS // 2 > READ_BATCH
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with tables(MANY, MANY + MANY_CHANGES).context() as ctx:
        spec_id = await store_spec(ctx, doc)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        explicit = await finish(ctx, await create_run(ctx, spec_id, "batch", keys=["2", "4"], who=TESTER))
        async with ctx.pool.connection() as conn:
            scan_reads = await repo.list_observations(conn, run["run_id"], 1)
            explicit_reads = await repo.list_observations(conn, explicit["run_id"], 1)
        rechecked = await results(ctx, explicit["run_id"])
    counters = run["counters"]
    assert (counters["by_outcome"]["MISMATCH"], counters["by_outcome"]["NOT_FOUND_B"]) == (ROWS // 2, 2)
    assert (counters["total"], counters["match"]) == (ROWS, ROWS // 2 - 2)
    assert counters["by_confirmation"]["rechecked"] == ROWS // 2 + 2  # the scan was the first read
    assert (len(scan_reads), {r["pass_no"] for r in scan_reads}) == (2 * (ROWS // 2 + 2), {1})
    assert {k: (r["outcome"], r["confirmation"]) for k, r in rechecked.items()} == {
        "2": ("MISMATCH", "rechecked"),
        "4": ("MISMATCH", "rechecked"),
    }
    assert sorted(r["pass_no"] for r in explicit_reads if r["key_masked"] == "2") == [
        1,
        1,
        2,
        2,
    ]  # read again
