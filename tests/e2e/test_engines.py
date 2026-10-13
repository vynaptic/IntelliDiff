"""Table pairs across engines: PostgreSQL, MySQL and Oracle against each other,
MySQL and Oracle against themselves and MySQL against a service find exactly the seeded differences, by a
scan whose rows the worker hashes, or in one engine the engine itself, and by per-key reads of canonical
rendering 1; a scan resumes, also past a key the other side cannot hold and past an empty key part, and key
maps, composite keys and soak cycles run as between two PostgreSQL tables. A scan's statements end on their
servers when its run is cancelled or paused and when its worker is terminated, keys of rows that may hold
large values are read four at a time, and a cycle records the server version it read. The acceptance kits of
examples/tables/ give their expected results, and with DS_PERF=1 the scans and batched reads are timed. A
three-part key pairs a MySQL table with a service that takes its parts in its path and query, and a key table
on MySQL or Oracle gives the three-part keys of a run between two services.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from aiohttp import web

from deltasleuth.core.spec import compile_spec, load_targets
from deltasleuth.runner import census, run_worker
from deltasleuth.runner.keys import resolve
from deltasleuth.runner.runs import cancel_run, compare_one, create_run, pause_run
from deltasleuth.store import repo
from tests.conftest import MYSQL_NAME, REPO_ROOT, TESTER, MySQLServer, OracleServer
from tests.e2e.conftest import Stack, both, finish, store_spec, table_spec
from tests.e2e.test_table_features import results, scanned
from tools import bench

pytestmark = [pytest.mark.e2e, pytest.mark.mysql]
oracle = pytest.mark.oracle  # a test, or a pairing of one, that also needs the Oracle server

ROWS = 1000
# The table dataset. Side a: every row. Side b, a view over a table without a key: row 5's email
# differs in case, row 6's amount only in scale, row 7 is missing, row 8 is there twice, row 1001 is extra,
# one row has no key.
USERS = {
    "postgresql": f"""
        CREATE TABLE users (userid int PRIMARY KEY, email text, name text, amount numeric(10, 2),
                            legacy_flag bool);
        INSERT INTO users SELECT g, 'user' || g || '@example.com', 'Name ' || g, g * 1.25, true
        FROM generate_series(1, {ROWS}) g;
    """,  # noqa: S608 (ROWS is a constant)
    "mysql": f"""
        SET SESSION cte_max_recursion_depth = 100000;
        CREATE TABLE users (userid INT PRIMARY KEY, email VARCHAR(100), name VARCHAR(100),
                            amount DECIMAL(10, 2), legacy_flag TINYINT(1));
        INSERT INTO users WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < {ROWS})
        SELECT n, CONCAT('user', n, '@example.com'), CONCAT('Name ', n), n * 1.25, 1 FROM g;
    """,  # noqa: S608 (ROWS is a constant)
    "oracle": f"""
        CREATE TABLE users (userid NUMBER(10) PRIMARY KEY, email VARCHAR2(100), name VARCHAR2(100),
                            amount NUMBER(10, 2), legacy_flag NUMBER(1));
        INSERT INTO users SELECT LEVEL, 'user' || LEVEL || '@example.com', 'Name ' || LEVEL, LEVEL * 1.25, 1
        FROM DUAL CONNECT BY LEVEL <= {ROWS};
    """,  # noqa: S608 (ROWS is a constant)
}
USERS_V = {
    "postgresql": f"""
        CREATE TABLE users_raw (user_id bigint, email text, full_name text, amount numeric);
        INSERT INTO users_raw SELECT g, 'user' || g || '@example.com', 'Name ' || g, round(g * 1.25, 2)
        FROM generate_series(1, {ROWS}) g WHERE g <> 7;
        UPDATE users_raw SET email = 'USER5@example.com' WHERE user_id = 5;
        UPDATE users_raw SET amount = 7.5 WHERE user_id = 6;
        INSERT INTO users_raw SELECT * FROM users_raw WHERE user_id = 8;
        INSERT INTO users_raw VALUES ({ROWS + 1}, 'new@example.com', 'New', 1), (NULL, 'x', 'x', 0);
        CREATE VIEW users_v AS SELECT * FROM users_raw;
    """,  # noqa: S608 (ROWS is a constant)
    "mysql": f"""
        SET SESSION cte_max_recursion_depth = 100000;
        CREATE TABLE users_raw (user_id BIGINT, email VARCHAR(100), full_name VARCHAR(100),
                                amount DECIMAL(12, 4));
        INSERT INTO users_raw
        WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < {ROWS})
        SELECT n, CONCAT('user', n, '@example.com'), CONCAT('Name ', n), n * 1.25 FROM g WHERE n <> 7;
        UPDATE users_raw SET email = 'USER5@example.com' WHERE user_id = 5;
        INSERT INTO users_raw SELECT * FROM users_raw WHERE user_id = 8;
        INSERT INTO users_raw VALUES ({ROWS + 1}, 'new@example.com', 'New', 1), (NULL, 'x', 'x', 0);
        CREATE VIEW users_v AS SELECT * FROM users_raw;
    """,  # noqa: S608 (ROWS is a constant)
    "oracle": f"""
        CREATE TABLE users_raw (user_id NUMBER(19), email VARCHAR2(100), full_name VARCHAR2(100),
                                amount NUMBER);
        INSERT INTO users_raw SELECT LEVEL, 'user' || LEVEL || '@example.com', 'Name ' || LEVEL, LEVEL * 1.25
        FROM DUAL WHERE LEVEL <> 7 CONNECT BY LEVEL <= {ROWS};
        UPDATE users_raw SET email = 'USER5@example.com' WHERE user_id = 5;
        INSERT INTO users_raw SELECT * FROM users_raw WHERE user_id = 8;
        INSERT INTO users_raw VALUES ({ROWS + 1}, 'new@example.com', 'New', 1), (NULL, 'x', 'x', 0);
        CREATE VIEW users_v AS SELECT * FROM users_raw;
    """,  # noqa: S608 (ROWS is a constant)
}


USERS_SIDES = {
    "a": {"role": "legacy", "target": "db-a", "table": {"name": "users", "keyColumns": ["userid"]}},
    "b": {
        "role": "new",
        "target": "db-b",
        "table": {
            "name": "users_v",
            "keyColumns": ["user_id"],
            "columns": ["email", {"name": "full_name", "as": "name"}, "amount"],
        },
    },
}


# In one engine the engine hashes the columns both sides declare alike (email and name) and the worker
# the amount, declared at two scales; a typeMap that names the two makes them the worker's as well.
ALL, MAPPED = ["amount", "email", "name"], {"email": {"as": "string"}, "full_name": {"as": "string"}}


@pytest.mark.parametrize(
    ("a", "b", "type_map", "worker"),
    [
        ("postgresql", "mysql", None, ALL),
        ("mysql", "postgresql", None, ALL),
        ("mysql", "mysql", None, ["amount"]),
        ("mysql", "mysql", MAPPED, ALL),
        pytest.param("postgresql", "oracle", None, ALL, marks=oracle),
        pytest.param("oracle", "mysql", None, ALL, marks=oracle),
        pytest.param("mysql", "oracle", None, ALL, marks=oracle),
        pytest.param("oracle", "oracle", None, ["amount"], marks=oracle),
        pytest.param("oracle", "oracle", MAPPED, ALL, marks=oracle),
    ],
    ids=[
        *("postgresql-mysql", "mysql-postgresql", "mysql-mysql-engine", "mysql-mysql-worker"),
        *("postgresql-oracle", "oracle-mysql", "mysql-oracle"),
        *("oracle-oracle-engine", "oracle-oracle-worker"),
    ],
)
async def test_a_scan_across_engines_finds_exactly_the_seeded_differences(
    engines: Callable[..., Stack], a: str, b: str, type_map: dict[str, Any] | None, worker: list[str]
) -> None:
    side_b = USERS_SIDES["b"] | ({"typeMap": type_map} if type_map else {})
    async with engines((a, USERS[a]), (b, USERS_V[b])).context() as ctx:
        spec_id = await store_spec(ctx, table_spec(sides=USERS_SIDES | {"b": side_b}))
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        one = await compare_one(ctx, spec_id, "5", who=TESTER)
    plan = run["params"]["tables"]
    assert (run["status"], plan["only_a"], plan["rendering"]) == ("completed", ["legacy_flag"], 1)
    assert [(plan[side]["engine"], plan[side]["consistency"]) for side in "ab"] == [
        (a, "snapshot"),
        (b, "unverified" if b == "mysql" else "snapshot"),  # a MySQL view's snapshot cannot be observed
    ]
    assert [plan[side].get("server") for side in "ab" if plan[side]["engine"] == "oracle"] == ["23.26"] * (
        [a, b].count("oracle")
    )
    assert plan["a"]["digest"] == plan["b"]["digest"] == worker  # the columns the worker hashes
    # The worker hashes numbers as numbers: 7.50 and 7.5 (row 6) are identical to the scan too. Whichever
    # hashes a column, the counters and the findings are the same.
    assert scanned(run) == (ROWS, ROWS + 2, 0, 1, ROWS - 3, 4)
    assert {key: (r["outcome"], r["inconclusive_reason"]) for key, r in found.items()} == {
        "5": ("MISMATCH", None),
        "7": ("NOT_FOUND_B", None),
        "8": ("INCONCLUSIVE", "identity_ambiguous"),
        str(ROWS + 1): ("NOT_FOUND_A", None),
    }
    assert [(f["path_pattern"], f["change_type"]) for f in found["5"]["findings"]] == [
        ("$.email", "value_changed")
    ]
    counters = run["counters"]
    assert (counters["total"], counters["match"], counters["by_confirmation"]["bulk_exhaustive"]) == (
        ROWS + 1, ROWS - 3, ROWS - 3,
    )  # fmt: skip
    assert all(i["holds"] for i in counters["reconciliation"]) and one.outcome.value == "MISMATCH"


async def test_a_scan_across_engines_stores_nothing_of_the_rows_it_found_identical(
    engines: Callable[..., Stack],
) -> None:
    # The worker reads and hashes every compared column of every row: of the
    # rows both sides hold alike, neither the store nor the evidence keeps a value.
    stack = engines(("postgresql", USERS["postgresql"]), ("mysql", USERS_V["mysql"]))
    async with stack.context() as ctx:
        run = await finish(
            ctx,
            await create_run(ctx, await store_spec(ctx, table_spec(sides=USERS_SIDES)), "batch", who=TESTER),
        )
        async with ctx.pool.connection() as conn:
            names = await (
                await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'ds'")
            ).fetchall()
            kept = [
                text
                for (name,) in names
                for (text,) in await (
                    await conn.execute(f'SELECT row_to_json(t)::text FROM ds."{name}" t'.encode())  # noqa: S608
                ).fetchall()
            ]
    kept += [
        path.read_bytes().decode("latin-1") for path in stack.settings.parent.rglob("*") if path.is_file()
    ]
    assert run["scan"]["matched"] == ROWS - 3 and any(run["run_id"] in text for text in kept)
    assert [text[:80] for text in kept if "user500@example.com" in text or "Name 500" in text] == []


# --- values and keys that differ by engine ------------------------------------------------------------

# 'pad': a CHAR padded on PostgreSQL only. 'zero': MySQL's zero date, which no PostgreSQL date equals.
# 'max': the largest BIGINT UNSIGNED. An emoji key. 'gone' is on side a only.
EDGES = {
    "postgresql": """
        CREATE TABLE edges (k text PRIMARY KEY, code char(5), opened date, big numeric(20, 0), at time(3));
        INSERT INTO edges VALUES ('pad', 'ab', '2024-02-29', 1, '03:04:05.120'),
            ('zero', 'x', '2024-01-01', 2, NULL), ('max', NULL, NULL, 18446744073709551615, '00:00:00'),
            ('\U0001f600', 'e', '2000-01-01', 3, '23:59:59.999'), ('gone', 'g', NULL, 4, NULL);
    """,
    "mysql": """
        SET SESSION sql_mode = '';
        CREATE TABLE edges (k VARCHAR(20) PRIMARY KEY, code CHAR(5), opened DATE, big BIGINT UNSIGNED,
                            at TIME(3));
        INSERT INTO edges VALUES ('pad', 'ab', '2024-02-29', 1, '03:04:05.120'),
            ('zero', 'x', '0000-00-00', 2, NULL), ('max', NULL, NULL, 18446744073709551615, '00:00:00'),
            ('\U0001f600', 'e', '2000-01-01', 3, '23:59:59.999');
    """,
}
AMOUNTS = {
    "postgresql": """
        CREATE TABLE amounts (k numeric(10, 2) PRIMARY KEY, v int);
        INSERT INTO amounts VALUES (1.50, 1), (2.00, 2), (-0.50, 3), (10.00, 4);
    """,
    "mysql": """
        CREATE TABLE amounts (k DECIMAL(10, 2) PRIMARY KEY, v INT);
        INSERT INTO amounts VALUES (1.50, 1), (2.00, 22), (-0.50, 3), (10.00, 4);
    """,
}


async def test_values_and_keys_render_alike_across_engines(engines: Callable[..., Stack]) -> None:
    seeds = {engine: EDGES[engine] + AMOUNTS[engine] for engine in EDGES}
    edges = table_spec(sides=both({"name": "edges", "keyColumns": ["k"]}), security={"unlisted": "clear"})
    amounts = table_spec(sides=both({"name": "amounts", "keyColumns": ["k"]}), security={"unlisted": "clear"})
    amounts["metadata"] = amounts["metadata"] | {"name": "amounts"}
    async with engines(("postgresql", seeds["postgresql"]), ("mysql", seeds["mysql"])).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, edges), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        keyed = await finish(ctx, await create_run(ctx, await store_spec(ctx, amounts), "batch", who=TESTER))
        priced = await results(ctx, keyed["run_id"])
    # The padded CHAR, the largest unsigned number, the times and the emoji key are identical to the scan.
    assert scanned(run) == (5, 4, 0, 0, 3, 2)
    assert {key: r["outcome"] for key, r in found.items()} == {"zero": "MISMATCH", "gone": "NOT_FOUND_B"}
    assert [(f["path_pattern"], f["change_type"]) for f in found["zero"]["findings"]] == [
        ("$.opened", "value_changed")
    ]
    assert run["params"]["tables"]["b"]["classes"] == {
        "k": "text", "code": "char", "opened": "date", "big": "integer", "at": "time",
    }  # fmt: skip
    # Decimal keys pair by their text, 1.50 with 1.50, and are found per key by it.
    assert scanned(keyed) == (4, 4, 0, 0, 3, 1)
    assert {key: r["outcome"] for key, r in priced.items()} == {"2.00": "MISMATCH"}


# 'pad': a CHAR padded on both, and a DATE that holds a time there. 'empty': '' here, NULL there. 'max':
# beyond a 64-bit integer. An emoji key. 'gone' is on side a only.
ORACLE_EDGES = {
    "postgresql": """
        CREATE TABLE edges (k text PRIMARY KEY, code char(5), note text, opened date, big numeric(20, 0),
                            seen timestamptz);
        INSERT INTO edges VALUES ('pad', 'ab', 'n', '2024-02-29', 1, '2024-05-01 14:00:00.5+02'),
            ('empty', 'x', '', '2024-01-01', 2, NULL), ('max', NULL, NULL, NULL, 18446744073709551615, NULL),
            ('\U0001f600', 'e', 'emoji', '2000-01-01', 3, NULL), ('gone', 'g', NULL, NULL, 4, NULL);
    """,
    "oracle": """
        CREATE TABLE edges (k VARCHAR2(20) PRIMARY KEY, code CHAR(5), note VARCHAR2(20), opened DATE,
                            big NUMBER(20), seen TIMESTAMP WITH TIME ZONE);
        INSERT INTO edges VALUES
            ('pad', 'ab', 'n', TIMESTAMP '2024-02-29 13:14:15', 1, TIMESTAMP '2024-05-01 12:00:00.5 +00:00'),
            ('empty', 'x', '', DATE '2024-01-01', 2, NULL),
            ('max', NULL, NULL, NULL, 18446744073709551615, NULL),
            ('\U0001f600', 'e', 'emoji', DATE '2000-01-01', 3, NULL);
    """,
}
AMOUNTS["oracle"] = """
    CREATE TABLE amounts (k NUMBER(10, 2) PRIMARY KEY, v NUMBER(10));
    INSERT INTO amounts VALUES (1.50, 1), (2.00, 22), (-0.50, 3), (10.00, 4);
"""


@oracle
async def test_values_render_alike_on_oracle_once_its_differences_are_declared(
    engines: Callable[..., Stack],
) -> None:
    def spec(name: str, **type_maps: dict[str, Any]) -> dict[str, Any]:
        doc = table_spec(sides=both({"name": "edges", "keyColumns": ["k"]}), security={"unlisted": "clear"})
        for side, type_map in type_maps.items():
            doc["sides"][side]["typeMap"] = type_map
        return doc | {"metadata": doc["metadata"] | {"name": name}}

    seeds = [("postgresql", ORACLE_EDGES["postgresql"]), ("oracle", ORACLE_EDGES["oracle"])]
    date, empty = {"opened": {"as": "date"}}, {"note": {"as": "string", "emptyAsNull": True}}
    async with engines(*seeds).context() as ctx:
        with pytest.raises(ValueError) as exc:  # an Oracle DATE is a timestamp
            await create_run(ctx, await store_spec(ctx, spec("undeclared")), "batch", who=TESTER)
        run = await finish(
            ctx, await create_run(ctx, await store_spec(ctx, spec("dated", b=date)), "batch", who=TESTER)
        )
        found = await results(ctx, run["run_id"])
        declared = await store_spec(ctx, spec("declared", a=empty, b=date))
        bridged = await finish(ctx, await create_run(ctx, declared, "batch", who=TESTER))
        bridged_found = await results(ctx, bridged["run_id"])
    assert str(exc.value) == (
        "side a: column 'opened' is a date (date) and side b's a timestamp: declare typeMap {OPENED: {as:"
        " date}} on side b, or {as: timestamp} on both"
    )
    # The padded CHAR, the DATE with its time dropped, the large number, the instant and the emoji key are
    # identical to the scan; Oracle's NULL against '' is a reported difference ...
    assert scanned(run) == (5, 4, 0, 0, 3, 2)
    assert {key: r["outcome"] for key, r in found.items()} == {"empty": "MISMATCH", "gone": "NOT_FOUND_B"}
    assert [(f["path_pattern"], f["change_type"]) for f in found["empty"]["findings"]] == [
        ("$.note", "type_changed")
    ]
    assert run["params"]["tables"]["b"]["classes"] == {
        "K": "text", "CODE": "char", "NOTE": "text", "OPENED": "timestamp", "BIG": "integer",
        "SEEN": "instant",
    }  # fmt: skip
    # ... until the other side declares its empty strings NULL: then the rows match in bulk.
    assert scanned(bridged) == (5, 4, 0, 0, 4, 1)
    assert {key: r["outcome"] for key, r in bridged_found.items()} == {"gone": "NOT_FOUND_B"}


@oracle
@pytest.mark.parametrize("a", ["postgresql", "mysql"])
async def test_a_decimal_key_pairs_with_an_oracle_number_of_its_scale(
    engines: Callable[..., Stack], a: str
) -> None:
    amounts = table_spec(sides=both({"name": "amounts", "keyColumns": ["k"]}), security={"unlisted": "clear"})
    async with engines((a, AMOUNTS[a].replace("22", "2")), ("oracle", AMOUNTS["oracle"])).context() as ctx:
        spec_id = await store_spec(ctx, amounts)
        keyed = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        priced = await results(ctx, keyed["run_id"])
        one = await compare_one(ctx, spec_id, "-0.50", who=TESTER)
    # NUMBER(10,2) keys pair by PostgreSQL's text, 1.50 with 1.50, and are found per key by it.
    assert scanned(keyed) == (4, 4, 0, 0, 3, 1) and one.outcome.value == "MATCH"
    assert {key: r["outcome"] for key, r in priced.items()} == {"2.00": "MISMATCH"}


# --- a MySQL table against a service --------------------------------------------------------------------

SERVED = {"1": {"email": "ann@example.com", "amount": 1.5}, "2": {"email": "BOB@example.com", "amount": 2}}


async def test_a_mysql_table_against_a_service(
    make_stack: Callable[..., Stack], mysql_server: MySQLServer
) -> None:
    async def user(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        return web.json_response(SERVED[key]) if key in SERVED else web.json_response({}, status=404)

    app = web.Application()
    app.router.add_get("/users/{key}", user)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    database = mysql_server.database("""
        CREATE TABLE users (userid INT PRIMARY KEY, email VARCHAR(100), amount DECIMAL(10, 2));
        INSERT INTO users VALUES (1, 'ann@example.com', 1.50), (2, 'bob@example.com', 2.00),
                                 (3, 'cy@example.com', 3.00);
    """)
    service = {
        "base_url": f"http://127.0.0.1:{runner.addresses[0][1]}", "allow_private_network": True,
        "allow_insecure_http": True, "capabilities": {"authoritative_absence": {"status": [404]}},
    }  # fmt: skip
    targets = {"db-a": mysql_server.target(database), "svc": service}
    sides = {
        "a": {"role": "legacy", "target": "db-a", "table": {"name": "users", "keyColumns": ["userid"]}},
        "b": {
            "role": "new", "target": "svc", "request": {"method": "GET", "path": "/users/{{ key }}"},
            "notFound": {"status": [404]}, "truncation": {"itemsPath": "$.tags", "cap": 50},
        },
    }  # fmt: skip
    listed = {"kind": "list", "keys": ["1", "2", "3", "4"]}
    key = {
        "name": "userid",
        "type": "string",
        "class": "business_code",
        "pattern": "^[0-9]+$",
        "source": listed,
    }
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"})
    try:
        async with make_stack(yaml.safe_dump({"targets": targets})).context() as ctx:
            spec_id = await store_spec(ctx, doc)
            run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
            found = await results(ctx, run["run_id"])
    finally:
        await runner.cleanup()
    plan = run["params"]["tables"]
    assert (run["status"], run["scan"], plan["a"]["engine"], plan["rendering"]) == (
        "completed",
        None,
        "mysql",
        1,
    )
    assert {key: r["outcome"] for key, r in found.items()} == {
        "1": "MATCH",  # 1.50 is the service's 1.5
        "2": "MISMATCH",
        "3": "NOT_FOUND_B",
        "4": "INCONCLUSIVE",  # on neither side
    }
    assert [(f["path_pattern"], f["change_type"]) for f in found["2"]["findings"]] == [
        ("$.email", "value_changed")
    ]


STOCKED = {("eu", "s1", "1"): 10, ("eu", "s1", "2"): 21, ("us", "a|b", "1"): 30, ("us", "s/2", "7"): 40}


async def test_mixed_pair_three_part_key(make_stack: Callable[..., Stack], mysql_server: MySQLServer) -> None:
    """A three-part key between a MySQL table and a service: the table's key columns are the parts, the
    service takes two in its path and one in its query, each encoded as a key is, and the key census
    merges the table's keys with the run's by their parts."""
    asked: list[str] = []

    async def item(request: web.Request) -> web.Response:
        asked.append(request.raw_path)
        key = (request.match_info["region"], request.match_info["shelf"], request.query["code"])
        if key not in STOCKED:
            return web.json_response({}, status=404)
        return web.json_response({"qty": STOCKED[key]})

    app = web.Application()
    app.router.add_get("/r/{region}/s/{shelf}/items", item)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    database = mysql_server.database("""
        CREATE TABLE stock (region VARCHAR(10), shelf VARCHAR(10), code INT, qty INT,
                            PRIMARY KEY (region, shelf, code));
        INSERT INTO stock VALUES ('eu', 's1', 1, 10), ('eu', 's1', 2, 20), ('us', 'a|b', 1, 30),
                                 ('us', 's/2', 7, 40), ('us', 's9', 9, 90), ('zz', 's0', 0, 1);
    """)
    service = {
        "base_url": f"http://127.0.0.1:{runner.addresses[0][1]}", "allow_private_network": True,
        "allow_insecure_http": True, "capabilities": {"authoritative_absence": {"status": [404]}},
    }  # fmt: skip
    parts = ["region", "shelf", "code"]
    request = {"method": "GET", "path": "/r/{{ keyParts.region }}/s/{{ keyParts.shelf }}/items",
               "query": {"code": "{{ keyParts.code }}"}}  # fmt: skip
    sides = {
        "a": {"role": "legacy", "target": "db-a", "table": {"name": "stock", "keyColumns": parts}},
        "b": {"role": "new", "target": "svc", "request": request, "notFound": {"status": [404]},
              "truncation": {"itemsPath": "$.tags", "cap": 50}},
    }  # fmt: skip
    keys = ["eu|s1|1", "eu|s1|2", "us|a\\|b|1", "us|s/2|7", "us|s9|9", "eu|s1|404"]
    key = {"name": "item", "type": "composite", "parts": parts, "class": "business_code",
           "pattern": "^[a-z0-9|/]+$", "source": {"kind": "list", "keys": keys}}  # fmt: skip
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"})
    targets = {"db-a": mysql_server.target(database), "svc": service}
    try:
        async with make_stack(yaml.safe_dump({"targets": targets})).context() as ctx:
            spec_id = await store_spec(ctx, doc)
            run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
            counted = await census.record(ctx, run_id)
            run = await finish(ctx, run_id)
            found = await results(ctx, run_id)
            one = await compare_one(ctx, spec_id, "us|a\\|b|1", who=TESTER)
    finally:
        await runner.cleanup()
    assert (run["status"], run["params"]["tables"]["a"]["keys"]) == ("completed", parts)
    assert {key: r["outcome"] for key, r in found.items()} == {
        "eu|s1|1": "MATCH",
        "eu|s1|2": "MISMATCH",
        "us|a\\|b|1": "MATCH",  # the part a|b
        "us|s/2|7": "MATCH",  # one path segment
        "us|s9|9": "NOT_FOUND_B",
        "eu|s1|404": "INCONCLUSIVE",  # on neither side
    }
    assert one.outcome.value == "MATCH"
    assert {"/r/us/s/a%7Cb/items?code=1", "/r/us/s/s%2F2/items?code=7"} <= set(asked)
    # zz|s0|0 is in the table alone, eu|s1|404 in the run alone.
    assert counted is not None
    assert {name: counted["a"][name] for name in ("rows", "skipped", "missing", "extra")} == {
        "rows": 6,
        "skipped": 0,
        "missing": 1,
        "extra": 1,
    }


SHELF_KEYS = """
    CREATE TABLE shelf_keys (region {text}(10), shelf {text}(10), code {number}, live {number});
    INSERT INTO shelf_keys VALUES ('us', 's/2', 7, 1), ('eu', 's1', 2, 1), ('eu', 's1', 1, 1),
        ('eu', 's1', 1, 1), ('us', 'a|b', 1, 1), ('zz', 's0', 0, 0), ('eu', NULL, 5, 0);
"""
KEY_TYPES = {
    "mysql": {"text": "VARCHAR", "number": "INT"},
    "oracle": {"text": "VARCHAR2", "number": "NUMBER(5)"},
}


@pytest.mark.parametrize("engine", ["mysql", pytest.param("oracle", marks=oracle)])
async def test_key_table_mysql_oracle(
    make_stack: Callable[..., Stack], mysql_server: MySQLServer, request: pytest.FixtureRequest, engine: str
) -> None:
    """A key table on MySQL and on Oracle gives the keys of a run between two services: its key columns are
    the three parts, a number among them by its key text, each key once and in key order, and its filter
    leaves rows out. A row without a key refuses the run, and the run stores no plan of the table."""

    async def item(request: web.Request) -> web.Response:
        key = (request.match_info["region"], request.match_info["shelf"], request.query["code"])
        more = request.match_info["side"] == "b" and key == ("eu", "s1", "2")
        return web.json_response({"qty": STOCKED[key] + more})

    app = web.Application()
    app.router.add_get("/{side}/r/{region}/s/{shelf}/items", item)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    seed = SHELF_KEYS.format(**KEY_TYPES[engine])
    if engine == "mysql":
        keyed = mysql_server.target(mysql_server.database(seed))
    else:
        server: OracleServer = request.getfixturevalue("oracle_server")
        keyed = server.target(server.schema(seed))
    service = {
        "base_url": f"http://127.0.0.1:{runner.addresses[0][1]}", "allow_private_network": True,
        "allow_insecure_http": True, "capabilities": {"authoritative_absence": {"status": [404]}},
    }  # fmt: skip
    parts = ["region", "shelf", "code"]
    sides = {
        side: {
            "role": role, "target": "svc", "notFound": {"status": [404]},
            "request": {"method": "GET", "query": {"code": "{{ keyParts.code }}"},
                        "path": f"/{side}/r/{{{{ keyParts.region }}}}/s/{{{{ keyParts.shelf }}}}/items"},
            "truncation": {"itemsPath": "$.tags", "cap": 50},
        }
        for side, role in (("a", "legacy"), ("b", "new"))
    }  # fmt: skip
    table = {"name": "shelf_keys", "keyColumns": parts}
    key = {"name": "item", "type": "composite", "parts": parts, "class": "business_code",
           "pattern": "^[a-z0-9|/]+$"}  # fmt: skip
    source = {"kind": "table", "target": "keys-db", "table": table | {"where": "live = 1"}}
    doc = table_spec(sides=sides, key=key | {"source": source}, security={"unlisted": "clear"})
    unfiltered = table_spec(sides=sides, key=key | {"source": source | {"table": table}})
    keys = ["eu|s1|1", "eu|s1|2", "us|a\\|b|1", "us|s/2|7"]
    try:
        async with make_stack(
            yaml.safe_dump({"targets": {"keys-db": keyed, "svc": service}})
        ).context() as ctx:
            with pytest.raises(ValueError) as keyless:
                await resolve(ctx, compile_spec(unfiltered, targets=ctx.targets))
            listed, read = await resolve(ctx, compile_spec(doc, targets=ctx.targets))
            run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
            run = await finish(ctx, run_id)
            found = await results(ctx, run_id)
            async with ctx.pool.connection() as conn:
                (created,), _ = await repo.list_audit(conn, action="create_run", obj=run_id)
    finally:
        await runner.cleanup()
    assert (
        str(keyless.value) == "the key source holds 1 row(s) without a key: leave them out with table.where"
    )
    assert listed == keys and read is not None
    assert (read["basis"], read["target"], read["rows"], read["keys"]) == ("key_table", "keys-db", 5, 4)
    assert read["relation"].lower().endswith(".shelf_keys")
    assert (run["status"], run["params"]["keys_source"], run["params"]["tables"]) == (
        "completed",
        "table",
        None,
    )
    assert {key: r["outcome"] for key, r in found.items()} == dict.fromkeys(keys, "MATCH") | {
        "eu|s1|2": "MISMATCH"
    }
    assert created["detail"]["key_source"] | {"at": read["at"]} == read


# --- a scan resumes, also past a key the other side cannot hold ----------------------------------------


async def test_a_killed_scan_resumes_once_its_host_resolves_again(
    make_stack: Callable[..., Stack],
    mysql_server: MySQLServer,
    databases: Callable[..., str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deltasleuth.connectors import netguard
    from deltasleuth.runner import scan as scanning

    monkeypatch.setattr(scanning, "BATCH", 100)  # checkpoints every 100 keys
    saves, resolves, real_save = 0, 0, repo.save_scan

    async def dying_save(*args: Any, **kwargs: Any) -> bool:
        nonlocal saves
        saves += 1
        if saves == 4:  # the worker dies before its fourth checkpoint commits
            raise psycopg.OperationalError("killed")
        return await real_save(*args, **kwargs)

    async def resolve(host: str, port: int) -> list[str]:
        nonlocal resolves
        resolves += 1
        if resolves == 4:  # the plan's two sessions, the first pass, then the pass that resumes
            raise socket.gaierror(socket.EAI_NONAME, "no such host")
        return ["127.0.0.1"]

    monkeypatch.setattr(repo, "save_scan", dying_save)
    monkeypatch.setattr(netguard, "system_resolve", resolve)
    (tmp_path / "dsn_b").write_text(databases("b", USERS_V["postgresql"]))
    targets = {
        "db-a": mysql_server.target(mysql_server.database(USERS["mysql"]), host=MYSQL_NAME),
        "db-b": {"dsn_ref": "file:dsn_b", "rate_per_s": 1000},
    }
    async with make_stack(yaml.safe_dump({"targets": targets}), lease_seconds=2).context() as ctx:
        run_id = await create_run(
            ctx, await store_spec(ctx, table_spec(sides=USERS_SIDES)), "batch", who=TESTER
        )
        run = await finish(ctx, run_id)
    assert saves > 4 and resolves >= 5  # a pass was lost, the next could not resolve its host, a third ended
    assert (run["status"], scanned(run)) == ("completed", (ROWS, ROWS + 2, 0, 1, ROWS - 3, 4))
    assert run["counters"]["total"] == ROWS + 1 and run["scan"]["snapshots_a"] == 2


class _Relay:
    """A TCP relay to a database server that cuts its ``cut``-th connection once it carried ``after`` bytes
    back to the client."""

    def __init__(self, upstream: int, cut: int, after: int) -> None:
        self.upstream, self.cut, self.after, self.accepted, self.cuts = upstream, cut, after, 0, 0

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        limit = self.after if self.accepted == self.cut else None
        back, forth = await asyncio.open_connection("127.0.0.1", self.upstream)

        async def pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter, limit: int | None) -> None:
            carried = 0
            try:
                while data := await source.read(4096):
                    sink.write(data)
                    await sink.drain()
                    carried += len(data)
                    if limit is not None and carried >= limit:
                        self.cuts += 1
                        break
            except OSError:
                pass
            finally:
                writer.close()
                forth.close()

        await asyncio.gather(pump(reader, forth, None), pump(back, writer, limit))


@pytest.mark.parametrize(
    ("engine", "code"),
    [("mysql", "MySQL 2013"), pytest.param("oracle", "DPY-4011", marks=oracle)],  # the drivers' own codes
)
async def test_a_flow_cut_mid_scan_pauses_the_scan_which_resumes(
    make_stack: Callable[..., Stack],
    mysql_server: MySQLServer,
    request: pytest.FixtureRequest,
    databases: Callable[..., str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    engine: str,
    code: str,
) -> None:
    from deltasleuth.connectors.tables import mysql
    from deltasleuth.runner import scan as scanning

    monkeypatch.setattr(scanning, "BATCH", 100)
    monkeypatch.setattr(mysql, "BATCH", 100)  # the scan reads on while the server still sends
    if engine == "mysql":
        side_a = mysql_server.target(mysql_server.database(USERS["mysql"]))
    else:
        oracle_server: OracleServer = request.getfixturevalue("oracle_server")
        side_a = oracle_server.target(oracle_server.schema(USERS["oracle"]))
    # Side a's third connection is its scan's (the plan opened two): it is cut after 20,000 bytes of rows.
    relay = _Relay(side_a["port"], cut=3, after=20_000)
    server = await asyncio.start_server(relay.serve, "127.0.0.1", 0)
    (tmp_path / "dsn_b").write_text(databases("b", USERS_V["postgresql"]))
    targets = {
        "db-a": side_a | {"port": server.sockets[0].getsockname()[1]},
        "db-b": {"dsn_ref": "file:dsn_b", "rate_per_s": 1000},
    }
    try:
        with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.worker"):
            async with make_stack(yaml.safe_dump({"targets": targets}), lease_seconds=2).context() as ctx:
                spec_id = await store_spec(ctx, table_spec(sides=USERS_SIDES))
                run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
    finally:
        server.close()
    paused = f"scan of run {run['run_id']} paused: {code}"  # no server said it
    assert relay.cuts == 1 and [r.getMessage() for r in caplog.records].count(paused) == 1
    assert (run["status"], scanned(run)) == ("completed", (ROWS, ROWS + 2, 0, 1, ROWS - 3, 4))
    assert run["counters"]["total"] == ROWS + 1


async def test_a_key_a_latin1_column_cannot_hold_is_skipped_and_the_scan_resumes_past_it(
    engines: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.runner import scan as scanning

    # Side a is a latin1 MySQL column holding k01..k05; side b, PostgreSQL, also holds nine keys with an
    # emoji, which latin1 cannot hold. The scan dies after checkpointing past one of them and resumes.
    latin = """
        CREATE TABLE users (userid VARCHAR(20) CHARACTER SET latin1 PRIMARY KEY, email VARCHAR(20));
        INSERT INTO users VALUES ('k01', 'x'), ('k02', 'x'), ('k03', 'x'), ('k04', 'x'), ('k05', 'x');
    """
    wide = """
        CREATE TABLE users (userid text PRIMARY KEY, email text);
        INSERT INTO users SELECT 'k0' || g, 'x' FROM generate_series(1, 5) g;
        INSERT INTO users SELECT '\U0001f6000' || g, 'y' FROM generate_series(1, 9) g;
    """
    monkeypatch.setattr(scanning, "BATCH", 5)
    saves, real_save = 0, repo.save_scan

    async def dying_save(*args: Any, **kwargs: Any) -> bool:
        nonlocal saves
        saves += 1
        if saves == 3:  # after the checkpoint past the fifth emoji key
            raise psycopg.OperationalError("killed")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(repo, "save_scan", dying_save)
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with engines(("mysql", latin), ("postgresql", wide), lease_seconds=2).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
    assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == ("completed", 5, 9)
    assert run["counters"]["by_inconclusive_reason"]["skipped"] == 9  # side a cannot even be asked
    assert run["counters"]["by_outcome"]["MATCH"] == 5  # every other key compared


@oracle
async def test_a_killed_scan_resumes_past_an_empty_key_part_on_oracle(
    engines: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.runner import scan as scanning

    # A composite key whose first part is '' on PostgreSQL: Oracle cannot hold it (its '' is NULL, and its
    # row with a NULL part has no key). The scan dies once it saved a checkpoint after ('', 2), so it
    # resumes after a bound with an empty part, which no comparison of Oracle's is ever after.
    seed_a = """
        CREATE TABLE stock (region text, code int, qty int, PRIMARY KEY (region, code));
        INSERT INTO stock VALUES ('', 1, 5), ('', 2, 6), ('eu', 1, 10), ('eu', 2, 20), ('us|x', 1, 30);
    """
    seed_b = """
        CREATE TABLE stock (region VARCHAR2(10), code NUMBER(5), qty NUMBER(5));
        INSERT INTO stock VALUES ('', 1, 5), ('eu', 1, 10), ('us|x', 1, 31);
    """
    monkeypatch.setattr(scanning, "BATCH", 2)
    saves, real_save = 0, repo.save_scan

    async def dying_save(*args: Any, **kwargs: Any) -> bool:
        nonlocal saves
        saves += 1
        if saves == 2:  # the worker dies before its second checkpoint commits
            raise psycopg.OperationalError("killed")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(repo, "save_scan", dying_save)
    stock = table_spec(
        key={"name": "sku", "type": "composite", "parts": ["region", "code"], "class": "business_code"},
        sides=both({"name": "stock", "keyColumns": ["region", "code"]}),
        security={"unlisted": "tokenize", "clear": ["$.qty"]},
    )
    async with engines(("postgresql", seed_a), ("oracle", seed_b), lease_seconds=2).context() as ctx:
        spec_id = await store_spec(ctx, stock)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        one = await compare_one(ctx, spec_id, "us\\|x|1", who=TESTER)
    assert saves > 2 and (run["status"], run["scan"]["snapshots_b"]) == ("completed", 2)
    assert scanned(run) == (5, 3, 0, 1, 1, 4) and one.outcome.value == "MISMATCH"  # nothing counted twice
    assert {key: r["outcome"] for key, r in found.items()} == {
        "|1": "NOT_FOUND_B",  # asked per key without a statement: no row there has an empty part
        "|2": "NOT_FOUND_B",
        "eu|2": "NOT_FOUND_B",
        "us\\|x|1": "MISMATCH",
    }


@oracle
async def test_a_key_an_oracle_number_cannot_hold_is_skipped_and_the_scan_resumes_past_it(
    engines: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.runner import scan as scanning

    # Side a is an Oracle NUMBER key holding 1 to 5; side b, PostgreSQL, also holds nine keys of 200
    # digits, which no NUMBER holds (ORA-01426). The scan dies after checkpointing past some and resumes.
    numbers = """
        CREATE TABLE users (userid NUMBER PRIMARY KEY, email VARCHAR2(20));
        INSERT INTO users SELECT LEVEL, 'x' FROM DUAL CONNECT BY LEVEL <= 5;
    """
    wide = """
        CREATE TABLE users (userid text PRIMARY KEY, email text);
        INSERT INTO users SELECT g::text, 'x' FROM generate_series(1, 5) g;
        INSERT INTO users SELECT repeat('9', 199) || g, 'y' FROM generate_series(1, 9) g;
    """
    monkeypatch.setattr(scanning, "BATCH", 5)
    saves, real_save = 0, repo.save_scan

    async def dying_save(*args: Any, **kwargs: Any) -> bool:
        nonlocal saves
        saves += 1
        if saves == 3:  # after the checkpoint past the fifth key of 200 digits
            raise psycopg.OperationalError("killed")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(repo, "save_scan", dying_save)
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with engines(("oracle", numbers), ("postgresql", wide), lease_seconds=2).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
    assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == ("completed", 5, 9)
    assert run["counters"]["by_inconclusive_reason"]["skipped"] == 9  # side a cannot even be asked
    assert run["counters"]["by_outcome"]["MATCH"] == 5  # every other key compared


# --- composite keys, key maps, soak cycles and the order of keys ---------------------------------------


STOCK_B = {
    "mysql": """
        CREATE TABLE stock (region VARCHAR(10), code INT, qty INT);
        INSERT INTO stock VALUES ('eu', 1, 10), ('us|x', 1, 31);
        CREATE TABLE entities (code VARCHAR(10) PRIMARY KEY, name VARCHAR(20));
        INSERT INTO entities VALUES ('1', 'Acme'), ('2', 'Bolt Ltd'), ('123', 'Crane'), ('300', 'Echo'),
                                    ('-5', 'Minus'), ('7\n', 'Newline');
    """,
    "oracle": """
        CREATE TABLE stock (region VARCHAR2(10), code NUMBER(5), qty NUMBER(5));
        INSERT INTO stock VALUES ('eu', 1, 10), ('us|x', 1, 31);
        CREATE TABLE entities (code VARCHAR2(10) PRIMARY KEY, name VARCHAR2(20));
        INSERT INTO entities VALUES ('1', 'Acme'), ('2', 'Bolt Ltd'), ('123', 'Crane'), ('300', 'Echo'),
                                    ('-5', 'Minus'), ('7\n', 'Newline');
    """,
}


@pytest.mark.parametrize("b", ["mysql", pytest.param("oracle", marks=oracle)])
async def test_a_composite_key_and_a_key_map_pair_across_engines(
    engines: Callable[..., Stack], b: str
) -> None:
    # (us|x, 1) differs and (eu, 2) is missing in b; ORG-000002 differs, 200 and 300 are one-sided and b's
    # -5 and its key with a trailing newline have no canonical key.
    seed_a = """
        CREATE TABLE stock (region text, code int, qty int, PRIMARY KEY (region, code));
        INSERT INTO stock VALUES ('eu', 1, 10), ('eu', 2, 20), ('us|x', 1, 30);
        CREATE TABLE entities (code text PRIMARY KEY, name text);
        INSERT INTO entities VALUES ('ORG-000001', 'Acme'), ('ORG-000002', 'Bolt'), ('ORG-000123', 'Crane'),
                                    ('ORG-000200', 'Delta');
    """
    stock = table_spec(
        key={"name": "sku", "type": "composite", "parts": ["region", "code"], "class": "business_code"},
        sides=both({"name": "stock", "keyColumns": ["region", "code"]}),
        security={"unlisted": "tokenize", "clear": ["$.qty"]},
    )
    key_map = {"kind": "prefix_pad", "prefix": "ORG-", "width": 6, "sideType": "string"}
    entities = table_spec(sides=both({"name": "entities", "keyColumns": ["code"]}))
    entities["sides"]["b"]["table"] = {
        "name": "entities",
        "keyColumns": [{"name": "code", "keyMap": key_map}],
    }
    entities["metadata"] = entities["metadata"] | {"name": "entities"}
    async with engines(("postgresql", seed_a), (b, STOCK_B[b])).context() as ctx:
        spec_id = await store_spec(ctx, stock)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        one = await compare_one(ctx, spec_id, "us\\|x|1", who=TESTER)
        mapped = await finish(
            ctx, await create_run(ctx, await store_spec(ctx, entities), "batch", who=TESTER)
        )
        named = await results(ctx, mapped["run_id"])
    assert scanned(run) == (3, 2, 0, 0, 1, 2) and one.outcome.value == "MISMATCH"
    assert {key: r["outcome"] for key, r in found.items()} == {"us\\|x|1": "MISMATCH", "eu|2": "NOT_FOUND_B"}
    assert scanned(mapped) == (4, 6, 0, 2, 2, 3)
    assert {key: r["outcome"] for key, r in named.items()} == {
        "ORG-000002": "MISMATCH",
        "ORG-000200": "NOT_FOUND_B",
        "ORG-000300": "NOT_FOUND_A",
    }


@pytest.mark.parametrize(
    ("b", "table", "changed"),
    [
        (
            "mysql",
            "users (userid VARCHAR(20) COLLATE utf8mb4_0900_ai_ci, email VARCHAR(20))",
            ["E", "e", "é"],
        ),
        pytest.param("oracle", "users (userid VARCHAR2(20), email VARCHAR2(20))", ["E"], marks=oracle),
    ],
)
async def test_keys_merge_in_code_point_order_whatever_the_column_s_collation(
    engines: Callable[..., Stack], b: str, table: str, changed: list[str]
) -> None:
    # The default MySQL collation orders these keys a, "a ", é, B, the emoji, e, Z, and takes e, E and é
    # for one; the scan reads them by their UTF-8 bytes, as PostgreSQL does, so every key pairs. Oracle
    # compares and orders them by their bytes itself.
    keys = ["B", "Z", "a", "a ", "e", "é", "\U0001f600", "E", "x1", "X1"]
    values = ", ".join(f"('{key}', 'v')" for key in keys)
    pg = f"CREATE TABLE users (userid text PRIMARY KEY, email text); INSERT INTO users VALUES {values}"  # noqa: S608
    update = "UPDATE users SET email = 'changed' WHERE userid = 'E'"
    there = f"CREATE TABLE {table}; INSERT INTO users VALUES {values}; {update}"  # noqa: S608
    doc = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}))
    async with engines(("postgresql", pg), (b, there)).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
    # MySQL's update changed e, E and é at once, by its own comparison: each is read by its own key.
    same = 10 - len(changed)
    assert (run["status"], scanned(run)) == ("completed", (10, 10, 0, 0, same, len(changed)))
    outcomes = run["counters"]["by_outcome"]
    assert (outcomes["MATCH"], outcomes["MISMATCH"], run["counters"]["total"]) == (same, len(changed), 10)
    assert sorted(found) == changed and {r["outcome"] for r in found.values()} == {"MISMATCH"}


@pytest.mark.parametrize("engine", ["mysql", pytest.param("oracle", marks=oracle)])
async def test_a_soak_over_two_tables_of_one_engine_scans_them_anew_every_cycle(
    engines: Callable[..., Stack], mysql_server: MySQLServer, request: pytest.FixtureRequest, engine: str
) -> None:
    kinds = (
        "INT PRIMARY KEY, email VARCHAR(20)"
        if engine == "mysql"
        else "NUMBER(5) PRIMARY KEY, email VARCHAR2(20)"
    )
    users = f"""
        CREATE TABLE users (userid {kinds});
        INSERT INTO users VALUES (1, 'u1'), (2, 'u2'), (3, 'u3'), (4, 'u4'), (5, 'u5'), (6, 'u6');
    """  # noqa: S608 (kinds is one of two constants)
    doc = table_spec(
        sides=both({"name": "users", "keyColumns": ["userid"]}),
        execution={"cycleInterval": "3s", "stop": {"maxCycles": 2}},
    )
    differs = "UPDATE users SET email = 'x' WHERE userid = 5;"
    stack = engines((engine, users), (engine, users + differs))
    target = yaml.safe_load((stack.settings.parent / "targets.yaml").read_text())["targets"]["db-b"]
    server = mysql_server if engine == "mysql" else request.getfixturevalue("oracle_server")
    side_b = target["database" if engine == "mysql" else "user"]  # its database, or its account's schema
    async with stack.context() as ctx:
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
            # Between the cycles, 5 is fixed and 3 starts to differ.
            server.execute("UPDATE users SET email = 'u5' WHERE userid = 5", side_b)
            server.execute("UPDATE users SET email = 'y' WHERE userid = 3", side_b)
        finally:
            stop.set()
            await worker
        run = await finish(ctx, run_id)
        first, second = await results(ctx, run_id, 1), await results(ctx, run_id, 2)
    assert (run["status"], run["cycle"], run["stop_reason"]) == ("completed", 2, "stop_condition:maxCycles")
    assert {k: r["outcome"] for k, r in first.items()} == {"5": "MISMATCH"}
    assert {k: r["outcome"] for k, r in second.items()} == {"5": "MATCH", "3": "MISMATCH"}
    assert run["scan"]["matched"] == 4  # 5 had a result: it is read again, though identical now
    assert run["params"]["tables"]["a"]["digest"] == []  # declared alike: the engine hashed every column


# --- a scan's statements end with its run, and with its worker -----------------------------------

# A view that takes about half a minute to read: while it does, its statement is the scan's sort.
SLOW = """
    SET SESSION cte_max_recursion_depth = 100000;
    CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20));
    INSERT INTO t WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 15000)
    SELECT n, CONCAT('v', n) FROM g;
    CREATE VIEW slow AS SELECT id, v FROM t WHERE SLEEP(0.002) = 0;
"""
SLOW_SPEC = table_spec(sides=both({"name": "slow", "keyColumns": ["id"], "columns": ["v"]}))


def _sessions(server: MySQLServer, stack: Stack) -> list[str]:
    """What each session on the stack's databases is doing: its statement, '' when it runs none."""
    targets = yaml.safe_load((stack.settings.parent / "targets.yaml").read_text())["targets"]
    names = ", ".join(f"'{target['database']}'" for target in targets.values())
    return [
        info or ""
        for (info,) in server.execute(
            "SELECT IF(COMMAND = 'Query', INFO, NULL) FROM information_schema.PROCESSLIST"  # noqa: S608
            f" WHERE DB IN ({names}) AND ID <> CONNECTION_ID()"
        )
    ]


async def _until(check: Callable[[], bool], timeout: float, why: str) -> float:
    """Wait until ``check()`` holds; the seconds it took."""
    started = time.monotonic()
    while not check():
        assert time.monotonic() - started < timeout, why
        await asyncio.sleep(0.1)
    return time.monotonic() - started


@pytest.mark.parametrize("stop", [cancel_run, pause_run], ids=["cancel", "pause"])
async def test_a_run_cancelled_or_paused_during_the_sort_ends_its_statements(
    engines: Callable[..., Stack], mysql_server: MySQLServer, stop: Callable[..., Any]
) -> None:
    # The scan learnt of its run's state only at a checkpoint, after the sort: the run read cancelled, or
    # paused, while both statements, their snapshots and their sessions stayed for the half minute.
    stack = engines(("mysql", SLOW), ("mysql", SLOW))
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, SLOW_SPEC), "batch", who=TESTER)
        halt = asyncio.Event()
        worker = asyncio.create_task(run_worker(ctx, stop=halt))
        try:
            sorting = lambda: [s for s in _sessions(mysql_server, stack) if "`slow`" in s]  # noqa: E731
            await _until(lambda: len(sorting()) == 2, 30, "no scan started")
            await asyncio.sleep(0.5)
            status = await stop(ctx, run_id, who=TESTER)
            took = await _until(lambda: _sessions(mysql_server, stack) == [], 10, "the statements run on")
            await asyncio.sleep(0.5)  # several sweeps: a run that does not run is not scanned again
            left = _sessions(mysql_server, stack)
            async with ctx.pool.connection() as conn:
                run = await repo.get_run(conn, run_id)
        finally:
            halt.set()
            await worker
    assert status in ("cancelled", "paused") and run["status"] == status
    assert took < 5 and left == [] and run["scan"]["checkpoint"] == 0


@pytest.mark.parametrize(("command", "code"), [("worker", 0), ("run", 130)])
async def test_a_terminated_worker_ends_its_scan_s_statements(
    engines: Callable[..., Stack], mysql_server: MySQLServer, repo_root: Path, command: str, code: int
) -> None:
    # ``ds worker`` had no SIGTERM handler: a stop of its container, or of its service, ended it where it
    # stood (or, as a container's first process, after the grace period), and both statements ran on.
    stack = engines(("mysql", SLOW), ("mysql", SLOW))
    ds = [sys.executable, "-c", "from deltasleuth.cli.main import app; app()"]
    if command == "worker":
        async with stack.context() as ctx:
            await create_run(ctx, await store_spec(ctx, SLOW_SPEC), "batch", who=TESTER)
        argv = [*ds, "worker", "--config", str(stack.settings)]
    else:  # the worker inside ``ds run``
        (stack.settings.parent / "spec.yaml").write_text(yaml.safe_dump(SLOW_SPEC))
        argv = [*ds, "run", str(stack.settings.parent / "spec.yaml"), "--config", str(stack.settings)]
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        sorting = lambda: [s for s in _sessions(mysql_server, stack) if "`slow`" in s]  # noqa: E731
        await _until(lambda: len(sorting()) == 2 or process.returncode is not None, 60, "no scan started")
        await asyncio.sleep(0.5)
        process.terminate()
        said, _ = await asyncio.wait_for(process.communicate(), 30)
        left = sorting()  # at once: a server does not notice a client that went away while it sorts
    finally:
        if process.returncode is None:
            process.kill()
    assert (process.returncode, left) == (code, []), said.decode()[-2000:]
    assert command == "worker" or said.decode().rstrip().endswith("interrupted")


# --- keys of rows that may hold large values -----------------------------------------------------


async def test_keys_of_rows_that_may_hold_large_values_are_read_four_at_a_time(
    engines: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.connectors.tables import TableConnector

    # Twelve documents of 2 MiB that all differ: one group of 200 keys held every row of both sides at
    # once, with its documents and bodies. The bound of a row is 16 MiB of them.
    docs = """
        SET SESSION cte_max_recursion_depth = 100000;
        CREATE TABLE docs (id INT PRIMARY KEY, body LONGTEXT);
        INSERT INTO docs WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 12)
        SELECT n, REPEAT(CHAR({first} + n), 2 * 1024 * 1024) FROM g;
    """
    groups: list[int] = []
    prefetch = TableConnector.prefetch

    async def counted(self: Any, keys: Any, side_cfg: Any) -> None:
        groups.append(len(keys))
        await prefetch(self, keys, side_cfg)

    monkeypatch.setattr(TableConnector, "prefetch", counted)
    doc = table_spec(sides=both({"name": "docs", "keyColumns": ["id"]}))
    async with engines(("mysql", docs.format(first=64)), ("mysql", docs.format(first=96))).context() as ctx:
        keys = [str(n) for n in range(1, 13)]
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", keys=keys, who=TESTER)
        tracemalloc.start()
        run = await finish(ctx, run_id)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert (run["status"], run["counters"]["by_outcome"]["MISMATCH"]) == ("completed", 12)
    assert max(groups) == 4 and sum(groups) >= 24  # each key on both sides, first and second read
    assert peak < 110 * 1024 * 1024


# --- the server version a cycle read ------------------------------------------------------------


async def test_a_cycle_records_the_server_version_it_read_whatever_the_plan_s(
    engines: Callable[..., Stack],
) -> None:
    # A run planned before a server's upgrade and scanned after it completed without a notice, and its
    # certificate named the version of the plan.
    async with engines(("mysql", USERS["mysql"]), ("postgresql", USERS_V["postgresql"])).context() as ctx:
        spec_id = await store_spec(ctx, table_spec(sides=USERS_SIDES))
        run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
        async with ctx.pool.connection() as conn:
            planned = dict((await repo.get_run(conn, run_id))["params"]["tables"]["b"])
            for side, server in (("a", "8.0"), ("b", "15.2")):  # as if planned before each upgrade
                await conn.execute(
                    "UPDATE ds.runs SET params = jsonb_set(params, %s, %s) WHERE run_id = %s",
                    (["tables", side, "server"], json.dumps(server), run_id),
                )
        run = await finish(ctx, run_id)
        async with ctx.pool.connection() as conn:
            spec = await ctx.spec(conn, spec_id)
    tables, scan = run["params"]["tables"], run["scan"]
    assert (run["status"], run["error"]) == ("completed", None)  # another version does not fail a run
    assert (tables["a"]["server"], tables["b"]["server"]) == ("8.0", "15.2")  # the plan is what it was
    assert (scan["server_a"], scan["server_b"]) == ("8.4", planned["server"])
    records = {side: census.scanned(side, tables[side], scan, spec) for side in "ab"}
    assert records["a"]["server"] == "8.4" and records["a"]["engine"] == "mysql"
    # PostgreSQL's record of such a pair names the version too, and nothing else new.
    assert records["b"]["server"] == planned["server"] and "engine" not in records["b"]


# --- the acceptance kit (examples/tables/{mysql,oracle}) ------------------------------------------------


@pytest.mark.parametrize("engine", ["mysql", pytest.param("oracle", marks=oracle)])
async def test_the_acceptance_kit_gives_its_expected_result(
    make_stack: Callable[..., Stack], mysql_server: MySQLServer, request: pytest.FixtureRequest, engine: str
) -> None:
    kit = REPO_ROOT / "examples" / "tables" / engine
    seed = (kit / "seed.sql").read_text()
    [target_id] = load_targets(kit / "targets.yaml")  # as shipped, it declares one engine target
    if engine == "mysql":
        mysql_server.execute(seed)
        entry = mysql_server.target("ds_kit")
    else:  # a fresh account's schema; the server is sent no comment line
        server: OracleServer = request.getfixturevalue("oracle_server")
        entry = server.target(server.schema(re.sub(r"^--.*$", "", seed, flags=re.MULTILINE)))
    async with make_stack(yaml.safe_dump({"targets": {target_id: entry}})).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, kit / "spec.yaml"), "batch", who=TESTER)
        run = await finish(ctx, run_id)
        found = await results(ctx, run_id)
    counters, scan = run["counters"], ("rows_a", "rows_b", "skipped_a", "skipped_b", "matched", "candidates")
    printed = ("total", "compared", "match", "different", "indeterminate", "cancelled", "outstanding")
    actual = {
        "status": run["status"],
        "scan": {name: run["scan"][name] for name in scan},
        "worker_columns": run["params"]["tables"]["a"]["digest"],
        "counters": {name: counters[name] for name in printed},  # the two lines `ds run` prints
        "outcomes": {outcome: n for outcome, n in sorted(counters["by_outcome"].items()) if n},
        "discrepancies": {
            key: [
                r["inconclusive_reason"] or r["outcome"],
                *sorted({f["path_pattern"] for f in r["findings"]}),
            ]
            for key, r in sorted(found.items())
        },
    }
    assert actual == json.loads((kit / "expected.json").read_text())


# --- performance: DS_PERF=1 runs these ---------------------------------------------------------------

perf = pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
PEOPLE = both({"name": "users", "keyColumns": ["userid"]})
PAIRS = [
    ("postgresql", "mysql"),
    pytest.param("postgresql", "oracle", marks=oracle),
    pytest.param("mysql", "oracle", marks=oracle),
]


def people(engine: str, rows: int, every: int = 0) -> str:
    """The seed of ``rows`` users (a multiple of 1,000) of four columns on ``engine``; with ``every``,
    side b's: each ``every``-th e-mail in upper case and user 3 gone."""
    row = "n, CONCAT('user', n, '@example.com'), CONCAT('Name ', n), n * 1.25"
    if engine == "postgresql":
        seed = f"""
            CREATE TABLE users (userid bigint PRIMARY KEY, email text, name text, amount numeric(12, 2));
            INSERT INTO users SELECT {row} FROM generate_series(1, {rows}) n;
        """  # noqa: S608 (rows is a number)
    elif engine == "mysql":
        seed = f"""
            SET SESSION cte_max_recursion_depth = 100000;
            CREATE TABLE g (n INT PRIMARY KEY);
            INSERT INTO g WITH RECURSIVE s (n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM s WHERE n < 9999)
            SELECT n FROM s;
            CREATE TABLE users (userid BIGINT PRIMARY KEY, email VARCHAR(100), name VARCHAR(100),
                                amount DECIMAL(12, 2));
            INSERT INTO users SELECT {row} FROM (
                SELECT a.n * 1000 + b.n + 1 AS n FROM g a JOIN g b WHERE a.n < {rows // 1000} AND b.n < 1000
            ) numbered;
        """  # noqa: S608 (rows is a number)
    else:
        seed = f"""
            CREATE TABLE users (userid NUMBER(19) PRIMARY KEY, email VARCHAR2(100), name VARCHAR2(100),
                                amount NUMBER(12, 2));
            INSERT INTO users SELECT n, 'user' || n || '@example.com', 'Name ' || n, n * 1.25 FROM (
                SELECT (a.n - 1) * 1000 + b.n AS n
                FROM (SELECT LEVEL AS n FROM DUAL CONNECT BY LEVEL <= {rows // 1000}) a,
                     (SELECT LEVEL AS n FROM DUAL CONNECT BY LEVEL <= 1000) b
            );
        """  # noqa: S608 (rows is a number)
    if not every:
        return seed
    multiple = f"MOD(userid, {every}) = 0" if engine == "oracle" else f"userid % {every} = 0"
    changed = f"UPDATE users SET email = UPPER(email) WHERE {multiple}; DELETE FROM users WHERE userid = 3;"  # noqa: S608
    return seed + changed


async def _scanned(engines: Callable[..., Stack], a: str, b: str, rows: int) -> tuple[repo.Row, float]:
    """A batch run over ``rows`` users a side, 11 of them differing; the run and its seconds."""
    async with engines((a, people(a, rows)), (b, people(b, rows, rows // 10))).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, table_spec(sides=PEOPLE)), "batch", who=TESTER)
        started = time.monotonic()
        run = await finish(ctx, run_id, timeout=900)
        elapsed = time.monotonic() - started
    print(f"\n{rows} rows a side, {a} against {b}: {elapsed:.1f} s")
    assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == ("completed", rows - 11, 11)
    assert run["counters"]["by_outcome"]["MISMATCH"] == 10
    return run, elapsed


@perf
@pytest.mark.parametrize(("a", "b"), PAIRS)
async def test_a_million_rows_across_engines(engines: Callable[..., Stack], a: str, b: str) -> None:
    run, elapsed = await _scanned(engines, a, b, 1_000_000)
    assert run["params"]["tables"]["a"]["digest"] == ["amount", "email", "name"]  # the worker hashes them
    assert elapsed < (180 if b == "oracle" else 120)  # Oracle Free runs on two threads


@perf
@pytest.mark.parametrize(("engine", "limit"), [("mysql", 60), pytest.param("oracle", 120, marks=oracle)])
async def test_a_million_rows_in_one_engine(engines: Callable[..., Stack], engine: str, limit: int) -> None:
    run, elapsed = await _scanned(engines, engine, engine, 1_000_000)
    assert run["params"]["tables"]["a"]["digest"] == []  # the engine hashes them
    assert elapsed < limit


@perf
@pytest.mark.parametrize(("a", "b"), PAIRS)
async def test_batches_across_engines(engines: Callable[..., Stack], a: str, b: str) -> None:
    # 20,000 of 100,000 rows differ; each is read, confirmed and saved (two PostgreSQL tables: 43 s).
    rows = 100_000
    doc = table_spec(sides=PEOPLE, execution={"concurrency": 8})
    seeds = (a, people(a, rows)), (b, people(b, rows, 5).replace("DELETE FROM users WHERE userid = 3;", ""))
    async with engines(*seeds).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
        started = time.monotonic()
        run = await finish(ctx, run_id, timeout=900)
        elapsed = time.monotonic() - started
    print(f"\n{rows // 5} differing rows of {rows}, {a} against {b}: {elapsed:.1f} s")
    assert run["counters"]["by_outcome"]["MISMATCH"] == rows // 5
    assert run["counters"]["by_confirmation"]["rechecked"] == rows // 5
    assert elapsed < 120


def _call(port: int, method: str, path: str, body: str | None = None) -> Any:
    """One request to a ``ds serve`` in developer mode on loopback; its JSON."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        conn.request(
            method, path, body=body, headers={"X-CSRF-Token": "dev", "Content-Type": "application/json"}
        )
        reply = conn.getresponse()
        found = json.loads(reply.read())
        assert reply.status < 300, found
        return found
    finally:
        conn.close()


def _ds(stack: Stack, *args: str) -> subprocess.Popen[bytes]:
    argv = [str(Path(sys.executable).with_name("ds")), *args, "--config", str(stack.settings)]
    return subprocess.Popen(
        argv, cwd=stack.settings.parent, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
    )


@perf
def test_ten_million_rows(
    make_stack: Callable[..., Stack], databases: Callable[..., str], mysql_server: MySQLServer, tmp_path: Path
) -> None:
    """10M rows a side under ``ds serve``, PostgreSQL against MySQL (the worker hashes) and MySQL against
    MySQL (the engine hashes), with the worker in the server's process and beside it: each scan's seconds,
    the worker process's peak resident memory and the p95 latency of the run's page, asked once a second."""
    rows = 10_000_000
    (tmp_path / "dsn_pg").write_text(databases("a", people("postgresql", rows)))
    targets = {
        "db-postgresql": {"dsn_ref": "file:dsn_pg", "rate_per_s": 1000},
        "db-mysql": mysql_server.target(mysql_server.database(people("mysql", rows))),
        "db-b": mysql_server.target(mysql_server.database(people("mysql", rows, rows // 10))),
    }
    stack = make_stack(yaml.safe_dump({"targets": targets}))
    for a in ("postgresql", "mysql"):
        sides = PEOPLE | {"a": PEOPLE["a"] | {"target": f"db-{a}"}}
        doc = table_spec(sides=sides, metadata={"name": f"people-{a}", "version": "1.0.0", "owner": "tests"})
        for beside in (False, True):
            with socket.create_server(("127.0.0.1", 0)) as free:
                port = free.getsockname()[1]
            server = _ds(stack, "serve", "--port", str(port), *(["--no-worker"] if beside else []))
            worker = _ds(stack, "worker") if beside else server
            try:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        _call(port, "GET", "/v1/health")
                        break
                    except OSError:
                        assert time.monotonic() < deadline, "ds serve did not start"
                        time.sleep(0.5)
                spec_id = _call(port, "POST", "/v1/specs", json.dumps(doc))["spec_id"]
                run_id = _call(port, "POST", "/v1/runs", json.dumps({"spec_id": spec_id, "mode": "batch"}))[
                    "run_id"
                ]
                started, latencies, scan_s = time.monotonic(), [], None
                while True:
                    asked = time.monotonic()
                    run = _call(port, "GET", f"/v1/runs/{run_id}")
                    if scan_s is None and (run["scan"] or {}).get("done"):
                        scan_s = asked - started
                    if scan_s is None:
                        latencies.append(time.monotonic() - asked)
                    if run["status"] in repo.TERMINAL:
                        break
                    assert time.monotonic() - started < 3600, "the run did not end"
                    time.sleep(max(0.0, 1 - (time.monotonic() - asked)))
                total = time.monotonic() - started
            finally:
                usage = bench._stop(worker)
                if beside:
                    bench._stop(server)
            peak = bench._rss_bytes(usage.ru_maxrss)
            p95 = statistics.quantiles(latencies, n=20, method="inclusive")[-1]
            print(
                f"\n{rows} rows a side, {a} against mysql, the worker {'beside' if beside else 'in'} ds"
                f" serve: scan {scan_s:.0f} s ({(scan_s or 0) / 77.5:.1f} times PostgreSQL's 77.5 s), run"
                f" {total:.0f} s, worker peak {peak / 2**20:.0f} MiB, GET /v1/runs/{{id}} p95"
                f" {p95 * 1000:.0f} ms (max {max(latencies) * 1000:.0f} ms, {len(latencies)} asked)"
            )
            assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == (
                "completed", rows - 11, 11,
            )  # fmt: skip
            assert run["params"]["tables"]["a"]["digest"] == (
                [] if a == "mysql" else ["amount", "email", "name"]
            )
            assert peak < 2**30
