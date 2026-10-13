"""Files as table sides (docs/mvp/FILES.md §18's e2e row): TABLES §7's dataset as CSV and as Parquet files
against PostgreSQL, MySQL and Oracle, a CSV side against a Parquet one, and a CSV side against a service find
exactly the seeded differences, by a scan of fixed files whose rows the worker hashes and by per-key reads of
canonical rendering 1; a declared dialect (a decimal comma, DD/MM/YYYY, a zero-padded key) bridges a
PostgreSQL side; lines that are not rows leave a key inconclusive, never missing, and side a no census; a soak
reads the same pinned bytes every cycle and fails once a file is replaced; a crafted page ends the reader's
process under ``ds serve``, never the server's (docs/mvp/READER.md §15). A key file, CSV or Parquet, gives the
keys of a rolling soak between two services, read anew at each roll.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiohttp import web

from deltasleuth.connectors.tables import duck, manifest, reader
from deltasleuth.core.spec import load_targets
from deltasleuth.runner import run_worker
from deltasleuth.runner.runs import compare_one, create_run
from deltasleuth.store import repo
from tests.conftest import REPO_ROOT, TESTER, MySQLServer, OracleServer
from tests.e2e.conftest import Stack, finish, store_spec, table_spec
from tests.e2e.test_engines import ROWS, USERS, USERS_SIDES, USERS_V, _call, people
from tests.e2e.test_table_features import results, scanned
from tests.support import crafted, faults
from tests.unit.connectors.ducked import unrestricted
from tools import bench

pytestmark = pytest.mark.e2e
mysql, oracle = pytest.mark.mysql, pytest.mark.oracle

Rows = Sequence[Sequence[Any]]
# TABLES.md §7's dataset. Side a: every row. Side b: row 5's email differs in case, row 6's amount only in
# scale, row 7 is missing, row 8 is there twice, row 1001 is extra, one row has no key.
USERS_A = [
    (g, f"user{g}@example.com", f"Name {g}", f"{Decimal(g) * Decimal('1.25'):.2f}", "true")
    for g in range(1, ROWS + 1)
]
USERS_B: list[tuple[Any, ...]] = [
    (
        g,
        "USER5@example.com" if g == 5 else f"user{g}@example.com",
        f"Name {g}",
        "7.5" if g == 6 else f"{g * 1.25:.2f}",
    )
    for g in range(1, ROWS + 1)
    if g != 7
]
USERS_B += [
    USERS_B[6],
    (ROWS + 1, "new@example.com", "New", "1"),
    (None, "x", "x", "0"),
]  # 8 twice, 1001, a NULL
A_COLUMNS, B_COLUMNS = (
    ["userid", "email", "name", "amount", "legacy_flag"],
    ["user_id", "email", "full_name", "amount"],
)
A_TYPES = {"amount": {"as": "decimal"}, "legacy_flag": {"as": "boolean"}}  # side a's CSV against a database
CHANGED = "UPDATE users SET email = 'x' WHERE userid = 5;"


def write_csv(
    path: Path, columns: Sequence[str], rows: Rows, *, delimiter: str = "|", header: bool = True
) -> None:
    """``rows`` as a CSV file: a NULL as an empty field."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    if header:
        writer.writerow(columns)
    writer.writerows(["" if value is None else value for value in row] for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out.getvalue())


def write_parquet(path: Path, select: str, rows: Rows, columns: Sequence[str], options: str = "") -> None:
    """``rows`` written by DuckDB's own writer (§18: an unrestricted, test-only connection), typed by
    ``select`` over them (its columns named as ``columns``), with the writer's other ``options``."""
    con = unrestricted()
    con.execute(f"CREATE TABLE raw ({', '.join(f'{c} VARCHAR' for c in columns)})")
    con.executemany(
        f"INSERT INTO raw VALUES ({', '.join('?' for _ in columns)})",  # noqa: S608 (placeholders)
        [[None if v is None else str(v) for v in r] for r in rows],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({select} FROM raw) TO '{path}' (FORMAT parquet{options})")


def csv_side(
    name: str, path: str, columns: Sequence[str], key: str, compared: Sequence[Any] | None, **csv_: Any
) -> dict[str, Any]:
    table: dict[str, Any] = {
        "name": name,
        "keyColumns": [key],
        "file": {
            "path": path,
            "format": "csv",
            "csv": {
                "columns": list(columns),
                "delimiter": "|",
                "header": True,
                "keys": {key: {"as": "integer"}},
            }
            | csv_,
        },
    }
    return table | ({"columns": list(compared)} if compared is not None else {})


B_CSV = csv_side(
    "users_v", "users_v.csv", B_COLUMNS, "user_id", ["email", {"name": "full_name", "as": "name"}, "amount"]
)
B_PARQUET = {
    "name": "users_v",
    "keyColumns": ["user_id"],
    "columns": ["email", {"name": "full_name", "as": "name"}, "amount"],
    "file": {"path": "users_v.parquet", "format": "parquet"},
}
B_SELECT = (
    "SELECT CAST(user_id AS BIGINT) AS user_id, email, full_name, CAST(amount AS DECIMAL(12, 4)) AS amount"
)


@pytest.fixture
def exports(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``files.roots_under``: a directory of its own, outside the stack's (T10), as its real path (P14)."""
    return tmp_path_factory.mktemp("exports").resolve()


def files_target(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    return {"engine": "files", "root": str(root), "settle_s": 0, "allow_writable": True}


@pytest.fixture
def stacked(
    make_stack: Callable[..., Stack],
    databases: Callable[..., str],
    exports: Path,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Callable[..., Stack]:
    """``stacked(a, b, files=None, **worker)``: a stack whose targets db-a and db-b are each a fresh database
    seeded on its engine (``("postgresql", seed)``), or a files target whose root is ``exports / side``
    (``("files",)``); ``files``: the file reader's budget in place of the tests' small one."""

    def make(
        a: tuple[str, ...], b: tuple[str, ...], files: dict[str, Any] | None = None, **worker: Any
    ) -> Stack:
        targets = {}
        for side, (engine, *seed) in (("a", a), ("b", b)):
            if engine == "files":
                targets[f"db-{side}"] = files_target(exports / side)
            elif engine == "mysql":
                server: MySQLServer = request.getfixturevalue("mysql_server")
                targets[f"db-{side}"] = server.target(server.database(seed[0]))
            elif engine == "oracle":
                oracle_server: OracleServer = request.getfixturevalue("oracle_server")
                targets[f"db-{side}"] = oracle_server.target(oracle_server.schema(seed[0]))
            else:
                (tmp_path / f"dsn_{side}").write_text(databases(side, seed[0]))
                targets[f"db-{side}"] = {"dsn_ref": f"file:dsn_{side}", "rate_per_s": 1000}
        budget = {"roots_under": str(exports)} | (files or {"memory": "256MB", "spill": "1GB"})
        return make_stack(yaml.safe_dump({"targets": targets}), files=budget, **worker)

    return make


def _passes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every per-key statement a file side is sent from here on: each is one pass over its files (§9)."""
    passes: list[str] = []
    execute = duck.Cursor.execute

    async def counted(self: Any, statement: str, *args: Any, **kwargs: Any) -> None:
        passes.extend([statement] if "QUALIFY" in statement else [])
        await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(duck.Cursor, "execute", counted)
    return passes


def users_spec(table_a: dict[str, Any], table_b: dict[str, Any], **sides: Any) -> dict[str, Any]:
    """A spec of the two tables; ``a``, ``b``: more of each side's members (a typeMap)."""
    side_a = {"role": "legacy", "target": "db-a", "table": table_a} | sides.get("a", {})
    side_b = {"role": "new", "target": "db-b", "table": table_b} | sides.get("b", {})
    return table_spec(sides={"a": side_a, "b": side_b})


def assert_seeded(run: repo.Row, found: dict[str, repo.Row]) -> None:
    """TABLES §7's dataset's counters and findings, whoever hashes the rows."""
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
    assert all(i["holds"] for i in run["counters"]["reconciliation"])


@pytest.mark.parametrize(
    ("engine", "form"),
    [
        ("postgresql", "csv"),
        ("postgresql", "parquet"),
        pytest.param("mysql", "csv", marks=mysql),
        pytest.param("mysql", "parquet", marks=mysql),
        pytest.param("oracle", "csv", marks=oracle),
        pytest.param("oracle", "parquet", marks=oracle),
    ],
)
async def test_a_database_against_files_finds_exactly_the_seeded_differences(
    stacked: Callable[..., Stack], exports: Path, engine: str, form: str
) -> None:
    stack = stacked((engine, USERS[engine]), ("files",))
    if form == "csv":
        write_csv(exports / "b" / "users_v.csv", B_COLUMNS, USERS_B)
        doc = users_spec(
            {"name": "users", "keyColumns": ["userid"]}, B_CSV, b={"typeMap": {"amount": {"as": "decimal"}}}
        )
    else:
        write_parquet(exports / "b" / "users_v.parquet", B_SELECT, USERS_B, B_COLUMNS)
        doc = users_spec({"name": "users", "keyColumns": ["userid"]}, B_PARQUET)
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, doc)
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        one = await compare_one(ctx, spec_id, "5", who=TESTER)
    plan = run["params"]["tables"]
    assert (run["status"], plan["only_a"], plan["rendering"]) == ("completed", ["legacy_flag"], 1)
    assert (plan["b"]["engine"], plan["b"]["consistency"], plan["b"]["schema"]) == ("files", "fixed", None)
    assert (plan["b"]["file_count"], plan["b"]["rows"], plan["b"]["selected"], plan["b"]["rejected"]) == (
        1,
        ROWS + 2,
        ROWS + 2,
        0,
    )
    assert plan["a"]["digest"] == plan["b"]["digest"] == ["amount", "email", "name"]  # the worker's
    assert set(run["params"]["pinned"]) == {"b"} and set(run["params"]["pinned"]["b"]) == {"enc"}
    assert run["scan"]["verified_b"] and "repeated_a" not in run["scan"] and run["scan"]["repeated_b"] == 1
    assert_seeded(run, found)
    assert one.outcome.value == "MISMATCH"


async def test_a_csv_side_against_a_parquet_side(stacked: Callable[..., Stack], exports: Path) -> None:
    stack = stacked(("files",), ("files",))
    write_csv(exports / "a" / "users.csv", A_COLUMNS, USERS_A)
    write_parquet(exports / "b" / "users_v.parquet", B_SELECT, USERS_B, B_COLUMNS)
    a = csv_side("users", "users.csv", A_COLUMNS, "userid", None)
    doc = users_spec(a, B_PARQUET, a={"typeMap": {"amount": {"as": "decimal"}}})
    async with stack.context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
    plan = run["params"]["tables"]
    assert (run["status"], plan["only_a"]) == ("completed", ["legacy_flag"])
    # A CSV field and a Parquet column are declared apart: the worker's columns (§9).
    assert plan["a"]["digest"] == ["amount", "email", "name"]
    assert run["scan"]["verified_a"] and run["scan"]["verified_b"]
    assert_seeded(run, found)


A_SELECT = (
    "SELECT CAST(userid AS BIGINT) AS userid, email, name, CAST(amount AS DECIMAL(12, 2)) AS amount,"
    " CAST(legacy_flag AS BOOLEAN) AS legacy_flag"
)


@pytest.mark.parametrize("form", ["csv", "parquet"])
async def test_two_file_sides_of_one_format_are_hashed_in_duckdb(
    stacked: Callable[..., Stack], exports: Path, form: str
) -> None:
    """D7, §9: TABLES §7's dataset as two CSV sides and as two Parquet sides. The columns both sides declare
    alike (email, name) are hashed by DuckDB and never fetched by the scan; the amount, which a typeMap names
    (CSV) or whose two decimals differ in scale (Parquet), is the worker's. Counters and findings are exact,
    and each per-key result of two fixed sides is final as read."""
    stack = stacked(("files",), ("files",))
    if form == "csv":
        write_csv(exports / "a" / "users.csv", A_COLUMNS, USERS_A)
        write_csv(exports / "b" / "users_v.csv", B_COLUMNS, USERS_B)
        decimal = {"typeMap": {"amount": {"as": "decimal"}}}
        doc = users_spec(
            csv_side("users", "users.csv", A_COLUMNS, "userid", None), B_CSV, a=decimal, b=decimal
        )
    else:
        write_parquet(exports / "a" / "users.parquet", A_SELECT, USERS_A, A_COLUMNS)
        write_parquet(exports / "b" / "users_v.parquet", B_SELECT, USERS_B, B_COLUMNS)
        a = {
            "name": "users",
            "keyColumns": ["userid"],
            "file": {"path": "users.parquet", "format": "parquet"},
        }
        doc = users_spec(a, B_PARQUET)
    async with stack.context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
    plan = run["params"]["tables"]
    assert (run["status"], plan["only_a"], plan["rendering"]) == ("completed", ["legacy_flag"], 1)
    assert plan["a"]["digest"] == plan["b"]["digest"] == ["amount"]
    assert run["scan"]["verified_a"] and run["scan"]["verified_b"] and run["scan"]["repeated_b"] == 1
    assert_seeded(run, found)
    assert {r["confirmation"] for r in found.values()} == {"frozen_cut"}
    assert run["counters"]["outstanding"] == 0


SERVED = {"1": {"email": "ann@example.com", "amount": 1.5}, "2": {"email": "BOB@example.com", "amount": 2}}


async def test_a_csv_side_against_a_service(make_stack: Callable[..., Stack], exports: Path) -> None:
    async def user(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        return web.json_response(SERVED[key]) if key in SERVED else web.json_response({}, status=404)

    app = web.Application()
    app.router.add_get("/users/{key}", user)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    rows = [(1, "ann@example.com", "1.50"), (2, "bob@example.com", "2.00"), (3, "cy@example.com", "3.00")]
    write_csv(exports / "a" / "users.csv", ["userid", "email", "amount"], [*rows, ("bad",)])  # one not a row
    service = {
        "base_url": f"http://127.0.0.1:{runner.addresses[0][1]}", "allow_private_network": True,
        "allow_insecure_http": True, "capabilities": {"authoritative_absence": {"status": [404]}},
    }  # fmt: skip
    targets = {"db-a": files_target(exports / "a"), "svc": service}
    a = csv_side("users", "users.csv", ["userid", "email", "amount"], "userid", None)
    sides = {
        "a": {"role": "legacy", "target": "db-a", "table": a, "typeMap": {"amount": {"as": "decimal"}}},
        "b": {
            "role": "new", "target": "svc", "request": {"method": "GET", "path": "/users/{{ key }}"},
            "notFound": {"status": [404]}, "truncation": {"itemsPath": "$.tags", "cap": 50},
        },
    }  # fmt: skip
    key = {"name": "userid", "type": "string", "class": "business_code", "pattern": "^[0-9]+$"}
    doc = table_spec(sides=sides, key=key | {"source": {"kind": "list", "keys": ["1", "2", "3", "4"]}})
    files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    try:
        async with make_stack(yaml.safe_dump({"targets": targets}), files=files).context() as ctx:
            run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
            found = await results(ctx, run["run_id"])
    finally:
        await runner.cleanup()
    plan = run["params"]["tables"]
    assert (run["status"], run["scan"], plan["a"]["engine"], plan["a"]["rejected"]) == (
        "completed",
        None,
        "files",
        1,
    )
    assert plan["a"]["kinds"] == {"columns": 1}
    # 4 is on neither side's rows: with a line of side a that was not a row, it is inconclusive (M1).
    assert {key: (r["outcome"], r["inconclusive_reason"]) for key, r in found.items()} == {
        "1": ("MATCH", None), "2": ("MISMATCH", None), "3": ("NOT_FOUND_B", None),
        "4": ("INCONCLUSIVE", "unparseable"),
    }  # fmt: skip


@pytest.mark.parametrize("form", ["csv", "parquet"])
async def test_key_table_csv_parquet(make_stack: Callable[..., Stack], exports: Path, form: str) -> None:
    """A key file on a files target of its own gives the two-part keys of a rolling soak between two
    services: each key once, its filter leaving rows out, pinned and read anew at each roll, so an export
    replaced between two cycles is the next cycle's keys and fails no run, whose plan holds no file."""

    async def item(request: web.Request) -> web.Response:
        return web.json_response({"shelf": f"{request.match_info['region']}/{request.match_info['code']}"})

    app = web.Application()
    app.router.add_get("/{side}/{region}/{code}", item)
    runner, service = await _service(app)
    columns, parts = ["region", "code", "tier"], ["region", "code"]

    def export(rows: Rows) -> None:
        if form == "csv":
            write_csv(exports / "keys" / "keys.csv", columns, rows)
        else:
            write_parquet(exports / "keys" / "keys.parquet", "SELECT region, code, tier", rows, columns)

    export([("us", "t3", "gold"), ("eu", "t2", "gold"), ("eu", "t1", "gold"), ("eu", "t1", "gold"),
            ("zz", "t9", "tin")])  # fmt: skip
    file = {"path": f"keys.{form}", "format": form}
    file |= {"csv": {"columns": columns, "delimiter": "|", "header": True}} if form == "csv" else {}
    table = {"name": "shelf_keys", "keyColumns": parts, "where": "tier = 'gold'", "file": file}
    path = "/{}/{{{{ keyParts.region }}}}/{{{{ keyParts.code }}}}"
    sides = {
        side: {
            "role": role, "target": "svc", "notFound": {"status": [404]},
            "request": {"method": "GET", "path": path.format(side)},
            "truncation": {"itemsPath": "$.tags", "cap": 50},
        }
        for side, role in (("a", "legacy"), ("b", "new"))
    }  # fmt: skip
    source = {"kind": "table", "target": "keys", "table": table}
    key = {"name": "shelf", "type": "composite", "parts": parts, "class": "business_code",
           "pattern": "^[a-z0-9]+$", "source": source}  # fmt: skip
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"},
                     execution={"cycleInterval": "0s", "stop": {"maxCycles": 2}})  # fmt: skip
    targets = {"keys": files_target(exports / "keys"), "svc": service}
    files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    try:
        async with make_stack(yaml.safe_dump({"targets": targets}), files=files).context() as ctx:
            spec_id = await store_spec(ctx, doc)
            run_id = await create_run(ctx, spec_id, "soak", soak={"rolling": True}, who=TESTER)
            export([("eu", "t2", "gold"), ("us", "t4", "gold"), ("us", "t4", "gold")])  # the next export
            run = await finish(ctx, run_id)
            first, second = await results(ctx, run_id, 1), await results(ctx, run_id, 2)
            async with ctx.pool.connection() as conn:
                (created,), _ = await repo.list_audit(conn, action="create_run", obj=run_id)
                (rolled,), _ = await repo.list_audit(conn, action="run.cycle", obj=run_id)
    finally:
        await runner.cleanup()
    assert (run["status"], run["cycle"], run["error"]) == ("completed", 2, None)
    assert (run["params"]["keys_source"], run["params"]["tables"]) == ("table", None)
    assert "pinned" not in run["params"] and "keys." not in json.dumps(run["params"])
    assert {key: r["outcome"] for key, r in first.items()} == dict.fromkeys(
        ["eu|t1", "eu|t2", "us|t3"], "MATCH"
    )
    assert {key: r["outcome"] for key, r in second.items()} == dict.fromkeys(["eu|t2", "us|t4"], "MATCH")
    reads = [row["detail"]["key_source"] for row in (created, rolled)]
    assert [(r["basis"], r["target"], r["relation"], r["rows"], r["keys"]) for r in reads] == [
        ("key_table", "keys", "shelf_keys", 4, 3),
        ("key_table", "keys", "shelf_keys", 3, 2),
    ]


async def test_a_key_files_lines_that_are_not_rows_refuse_its_read(
    make_stack: Callable[..., Stack], exports: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A CSV key file with lines that are not rows of its declared dialect (a field short, a field over):
    their keys are in no key list, so its read is refused by their count rather than compare the others
    with nothing said. A roll keeps the previous keys and audits why, a creation is refused; the reader's
    log line, which says where the lines are, names the key source."""

    async def item(request: web.Request) -> web.Response:
        return web.json_response({"shelf": f"{request.match_info['region']}/{request.match_info['code']}"})

    app = web.Application()
    app.router.add_get("/{side}/{region}/{code}", item)
    runner, service = await _service(app)
    columns, parts = ["region", "code", "tier"], ["region", "code"]
    rows = [("us", "t3"), ("eu", "t2"), ("eu", "t1"), ("de", "t7"), ("fr", "t8"), ("nl", "t5")]
    write_csv(exports / "keys" / "keys.csv", columns, [(*row, "gold") for row in rows])
    broken = "region|code|tier\nus|t3|gold\neu|t2|gold\neu|t1|gold\nde|t7\nfr|t8|gold|extra\nnl|t5|gold\n"
    file = {
        "path": "keys.csv",
        "format": "csv",
        "csv": {"columns": columns, "delimiter": "|", "header": True},
    }
    path = "/{}/{{{{ keyParts.region }}}}/{{{{ keyParts.code }}}}"
    sides = {
        side: {
            "role": role, "target": "svc", "notFound": {"status": [404]},
            "request": {"method": "GET", "path": path.format(side)},
            "truncation": {"itemsPath": "$.tags", "cap": 50},
        }
        for side, role in (("a", "legacy"), ("b", "new"))
    }  # fmt: skip
    source = {"kind": "table", "target": "keys", "table": {"name": "shelf_keys", "keyColumns": parts}}
    source["table"]["file"] = file
    key = {"name": "shelf", "type": "composite", "parts": parts, "class": "business_code",
           "pattern": "^[a-z0-9]+$", "source": source}  # fmt: skip
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"},
                     execution={"cycleInterval": "0s", "stop": {"maxCycles": 2}})  # fmt: skip
    targets = {"keys": files_target(exports / "keys"), "svc": service}
    files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    unrowed = (
        "the key source holds 2 line(s) that are not rows of its declared CSV dialect: fix the export or its"
        " csv declaration"
    )
    try:
        async with make_stack(yaml.safe_dump({"targets": targets}), files=files).context() as ctx:
            spec_id = await store_spec(ctx, doc)
            run_id = await create_run(ctx, spec_id, "soak", soak={"rolling": True}, who=TESTER)
            (exports / "keys" / "keys.csv").write_text(broken)  # the next export
            run = await finish(ctx, run_id)
            first, second = await results(ctx, run_id, 1), await results(ctx, run_id, 2)
            with pytest.raises(ValueError) as refused:
                await create_run(ctx, spec_id, "batch", who=TESTER)
            async with ctx.pool.connection() as conn:
                (failed,), _ = await repo.list_audit(conn, action="rolling_resolve_failed", obj=run_id)
                rolled, _ = await repo.list_audit(conn, action="run.cycle", obj=run_id)
                made, _ = await repo.list_runs(conn, spec_id=spec_id)
    finally:
        await runner.cleanup()
    assert (run["status"], run["cycle"], run["error"]) == ("completed", 2, None)
    assert sorted(first) == sorted(second) == sorted("|".join(row) for row in rows)  # the keys before it
    assert failed["detail"] == {"error": unrowed} and "key_source" not in rolled[0]["detail"]
    assert str(refused.value) == unrowed and [row["run_id"] for row in made] == [run_id]
    assert [message for message in caplog.messages if "of its files are not rows" in message] == [
        "the key source: 2 line(s) of its files are not rows: file 1 line 5: columns; file 1 line 6: columns"
    ]


# --- a declared dialect bridges another engine (§5, §7) ---------------------------------------------------

LEDGER = """
    CREATE TABLE ledger (id int PRIMARY KEY, balance numeric(12, 2), opened date, note text);
    INSERT INTO ledger VALUES (123, 1.50, '2024-02-29', 'a'), (124, -0.50, '2023-12-31', ''),
        (125, 10.00, '2024-01-01', 'c'), (126, 2.00, '2024-01-02', 'd');
"""
LEDGER_COLUMNS = ["id", "balance", "opened", "note"]


async def test_a_declared_dialect_bridges_postgres(stacked: Callable[..., Stack], exports: Path) -> None:
    stack = stacked(("postgresql", LEDGER), ("files",))
    rows = [
        ("00123", "1,50", "29/02/2024", "a"),
        ("124", "-0,50", "31/12/2023", ""),
        ("125", "10,5", "01/01/2024", "c"),
        ("126", "2,00", "02/01/2024", "d"),
    ]
    write_csv(exports / "b" / "ledger.csv", LEDGER_COLUMNS, rows, delimiter=";", header=False)
    dialect = {"delimiter": ";", "null": "NULL", "keys": {"id": {"as": "integer"}}}
    b = {
        "name": "ledger", "keyColumns": ["id"],
        "file": {"path": "ledger.csv", "format": "csv", "csv": {"columns": LEDGER_COLUMNS} | dialect},
    }  # fmt: skip
    types = {
        "balance": {"as": "decimal", "decimalSeparator": ","},
        "opened": {"as": "date", "format": "%d/%m/%Y"},
    }
    doc = users_spec({"name": "ledger", "keyColumns": ["id"]}, b, b={"typeMap": types})
    doc["security"] = {"unlisted": "clear"}
    async with stack.context() as ctx:
        undeclared = users_spec({"name": "ledger", "keyColumns": ["id"]}, b)
        undeclared["metadata"] |= {"name": "undeclared"}
        with pytest.raises(ValueError) as refused:
            await create_run(ctx, await store_spec(ctx, undeclared), "batch", who=TESTER)
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
    assert str(refused.value) == (
        "side b: column 'balance' is text (a CSV field) and side a's a number (numeric(12,2)): declare"
        " typeMap {balance: {as: decimal}} on side b"
    )
    # 00123 is the key 123; the decimal commas and the dates read as declared; '' is the empty string.
    assert scanned(run) == (4, 4, 0, 0, 3, 1)
    assert {key: r["outcome"] for key, r in found.items()} == {"125": "MISMATCH"}
    assert [(f["path_pattern"], f["change_type"]) for f in found["125"]["findings"]] == [
        ("$.balance", "value_changed")
    ]


async def test_empty_as_null_bridges_a_null_field_and_an_empty_string(
    stacked: Callable[..., Stack], exports: Path
) -> None:
    """§5, §7: an unquoted empty field is NULL and PostgreSQL holds '': the difference is reported until the
    approved spec declares emptyAsNull on the side that holds the empty string."""
    stack = stacked(("postgresql", LEDGER), ("files",))
    rows = [(123, "1.50", "2024-02-29", "a"), (124, "-0.50", "2023-12-31", None)]
    rows += [(125, "10.00", "2024-01-01", "c"), (126, "2.00", "2024-01-02", "d")]
    write_csv(exports / "b" / "ledger.csv", LEDGER_COLUMNS, rows)
    b = csv_side("ledger", "ledger.csv", LEDGER_COLUMNS, "id", None)
    types = {"balance": {"as": "decimal"}, "opened": {"as": "date"}}
    doc = users_spec({"name": "ledger", "keyColumns": ["id"]}, b, b={"typeMap": types})
    doc["security"] = {"unlisted": "clear"}
    bridged = users_spec(
        {"name": "ledger", "keyColumns": ["id"]}, b, b={"typeMap": types},
        a={"typeMap": {"note": {"as": "string", "emptyAsNull": True}}},
    )  # fmt: skip
    bridged["security"], bridged["metadata"] = doc["security"], doc["metadata"] | {"version": "1.1.0"}
    async with stack.context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
        again = await finish(ctx, await create_run(ctx, await store_spec(ctx, bridged), "batch", who=TESTER))
    assert {key: r["outcome"] for key, r in found.items()} == {"124": "MISMATCH"}
    assert [(f["path_pattern"], f["change_type"]) for f in found["124"]["findings"]] == [
        ("$.note", "type_changed")  # a string against null
    ]
    assert scanned(again) == (4, 4, 0, 0, 4, 0)


async def test_a_scan_of_files_killed_mid_pass_resumes_after_its_checkpoint(
    stacked: Callable[..., Stack], exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11: a pass that ends mid-scan (a kill, a stop) resumes after its last saved key on the same pinned
    bytes: nothing is counted twice, and the files are hashed again when the resumed pass's rows end."""
    from deltasleuth.runner import scan as scanning

    monkeypatch.setattr(scanning, "BATCH", 100)
    stack = stacked(("files",), ("postgresql", USERS_V["postgresql"]))
    write_csv(exports / "a" / "users.csv", A_COLUMNS, USERS_A)
    a = csv_side("users", "users.csv", A_COLUMNS, "userid", None)
    doc = users_spec(a, USERS_SIDES["b"]["table"], a={"typeMap": {"amount": {"as": "decimal"}}})
    save, stop, saved = scanning._save, asyncio.Event(), []

    async def killed(*args: Any, **kwargs: Any) -> bool:
        ok = await save(*args, **kwargs)
        saved.append(1)
        if len(saved) == 3:  # the worker is stopped here: its scan's task is cancelled
            stop.set()
            await asyncio.sleep(3600)
        return ok

    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
        monkeypatch.setattr(scanning, "_save", killed)
        await run_worker(ctx, stop=stop)
        async with ctx.pool.connection() as conn:
            killed_at = await repo.get_run(conn, run_id)
        monkeypatch.setattr(scanning, "_save", save)
        run = await finish(ctx, run_id)
        found = await results(ctx, run_id)
    assert (killed_at["status"], killed_at["scan"]["checkpoint"], killed_at["scan"]["done"]) == (
        "running",
        3,
        False,
    )
    # 300 keys merged in text order, 1001 among them, which side a lacks.
    assert killed_at["scan"]["rows_a"] == 299 and "verified_a" not in killed_at["scan"]
    assert run["status"] == "completed" and run["scan"]["verified_a"]
    assert_seeded(run, found)


# --- lines that are not rows (D6) and a soak of fixed files (§11) ----------------------------------------


async def test_lines_that_are_not_rows_on_side_a_leave_no_census(
    stacked: Callable[..., Stack], exports: Path
) -> None:
    stack = stacked(("files",), ("postgresql", USERS["postgresql"]))
    lines = [*USERS_A[:9], ("trailer", "9"), *USERS_A[10:]]  # row 10's line is not a row of the dialect
    write_csv(exports / "a" / "users.csv", A_COLUMNS, lines)
    a = csv_side("users", "users.csv", A_COLUMNS, "userid", None)
    doc = users_spec(a, {"name": "users", "keyColumns": ["userid"]}, a={"typeMap": A_TYPES})
    async with stack.context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
    plan = run["params"]["tables"]["a"]
    assert (plan["rows"], plan["selected"], plan["rejected"], plan["kinds"]) == (
        ROWS - 1,
        ROWS - 1,
        1,
        {"columns": 1},
    )
    # Key 10 is on side b only: with a line of side a that was not a row, it is inconclusive, never missing.
    assert {key: (r["outcome"], r["inconclusive_reason"]) for key, r in found.items()} == {
        "10": ("INCONCLUSIVE", "unparseable")
    }
    assert scanned(run)[:2] == (ROWS - 1, ROWS)


async def test_a_soak_reads_the_same_pinned_files_and_fails_once_one_is_replaced(
    stacked: Callable[..., Stack], exports: Path
) -> None:
    stack = stacked(("files",), ("postgresql", USERS["postgresql"] + CHANGED))
    write_csv(exports / "a" / "users.csv", A_COLUMNS, USERS_A)
    a = csv_side("users", "users.csv", A_COLUMNS, "userid", None)
    doc = users_spec(a, {"name": "users", "keyColumns": ["userid"]}, a={"typeMap": A_TYPES})
    doc["execution"] = {"cycleInterval": "3s", "stop": {"maxCycles": 3}}
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "soak", who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(ctx, stop=stop))
        try:
            async with asyncio.timeout(60):
                while True:
                    async with ctx.pool.connection() as conn:
                        opened = await repo.get_run(conn, run_id)
                    if opened["cycle"] == 3:
                        break
                    await asyncio.sleep(0.02)
            # Before the third cycle's scan the export is written again: its content changes, not its size.
            write_csv(
                exports / "a" / "users.csv",
                A_COLUMNS,
                [(*row[:2], row[2].upper(), *row[3:]) for row in USERS_A],
            )
        finally:
            stop.set()
            await worker
        run = await finish(ctx, run_id)
        first, second = await results(ctx, run_id, 1), await results(ctx, run_id, 2)
    assert (
        {k: r["outcome"] for k, r in first.items()}
        == {k: r["outcome"] for k, r in second.items()}
        == {"5": "MISMATCH"}
    )
    assert run["status"] == "failed" and run["error"].startswith(
        "FilesChanged: side a: file 1 of 1 is not the file"
    )


async def test_a_scan_that_does_not_meet_the_plan_s_rows_fails_the_run(
    stacked: Callable[..., Stack], exports: Path
) -> None:
    """§7's rows guard (F10): a read to its end met another number of rows than the plan counted. Here the
    stored plan is made to disagree with the files, as a parser that disagreed with itself would."""
    stack = stacked(("postgresql", USERS["postgresql"]), ("files",))
    write_csv(exports / "b" / "users_v.csv", B_COLUMNS, USERS_B)
    doc = users_spec(
        {"name": "users", "keyColumns": ["userid"]}, B_CSV, b={"typeMap": {"amount": {"as": "decimal"}}}
    )
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
        async with ctx.pool.connection() as conn:
            moved = (
                "UPDATE ds.runs SET params = jsonb_set(params, '{tables,b,selected}', to_jsonb(%(n)s::int))"
                " WHERE run_id = %(id)s"
            )
            await conn.execute(moved, {"n": ROWS + 3, "id": run_id})
        run = await finish(ctx, run_id)
    assert (run["status"], run["error"]) == (
        "failed", f"RowsMoved: side b: {ROWS + 2:,} rows were read where the plan counted {ROWS + 3:,}",
    )  # fmt: skip


async def test_a_directory_that_is_not_there_pauses_the_scan_until_it_is(
    stacked: Callable[..., Stack], exports: Path
) -> None:
    """§10's table: a root that cannot be listed is TargetUnreachable: the pass is paused and resumed without
    limit, never failing the run."""
    stack = stacked(("postgresql", USERS["postgresql"]), ("files",), lease_seconds=1)
    write_csv(exports / "b" / "users_v.csv", B_COLUMNS, USERS_B)
    doc = users_spec(
        {"name": "users", "keyColumns": ["userid"]}, B_CSV, b={"typeMap": {"amount": {"as": "decimal"}}}
    )
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER)
        (exports / "b").rename(exports / "b.away")
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(ctx, stop=stop))
        try:
            await asyncio.sleep(3)  # passes paused, each followed by a lease period
            async with ctx.pool.connection() as conn:
                waiting = await repo.get_run(conn, run_id)
        finally:
            stop.set()
            await worker
        (exports / "b.away").rename(exports / "b")
        run = await finish(ctx, run_id)
        found = await results(ctx, run_id)
    assert (waiting["status"], waiting["scan"]["checkpoint"]) == ("running", 0)
    assert run["status"] == "completed"
    assert_seeded(run, found)


async def test_a_damaged_page_fails_the_run_at_its_third_stalled_pass(
    stacked: Callable[..., Stack], exports: Path
) -> None:
    """§10's table: a damaged page of a compared column, which the plan's pass over the key columns does not
    read, fails every pass the same way on the same bytes: the files are stat-checked, unchanged, and the
    third pass that saves no checkpoint fails the run (F4), by the error's class, never DuckDB's text."""
    stack = stacked(("postgresql", USERS["postgresql"]), ("files",), lease_seconds=1)
    path = exports / "b" / "users_v.parquet"
    write_parquet(path, B_SELECT, USERS_B, B_COLUMNS, ", COMPRESSION zstd")
    pages = unrestricted().execute(
        "SELECT data_page_offset, total_compressed_size FROM parquet_metadata($f)"
        " WHERE path_in_schema = 'email'",
        {"f": str(path)},
    )
    ((offset, size),) = pages.fetchall()
    data = bytearray(path.read_bytes())
    data[offset + size // 2 : offset + size // 2 + 64] = b"\xff" * 64
    path.write_bytes(bytes(data))
    doc = users_spec({"name": "users", "keyColumns": ["userid"]}, B_PARQUET)
    async with stack.context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
    assert (run["status"], run["scan"]["checkpoint"]) == ("failed", 0)
    assert re.fullmatch(r"TargetRefused: [A-Za-z]+", run["error"])  # 1.5.5: Error, its damaged zstd page's


@contextmanager
def _pages(
    make_stack: Callable[..., Stack], exports: Path
) -> Iterator[tuple[Stack, int, Callable[[str, str], Any]]]:
    """``ds serve`` over a Parquet side a against a CSV side b of the same four rows, side a's files
    ``clean.parquet`` and ``crafted.parquet``, the kept mutant: the stack, the server's port and what
    reads one of side a's files in a run of a version of its own, ``/v1/health`` answering until the run
    ends."""
    targets = {f"db-{side}": files_target(exports / side) for side in "ab"}
    budget = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    stack = make_stack(yaml.safe_dump({"targets": targets}), files=budget, lease_seconds=1)
    rows = [(n, "x") for n in range(4)]
    (exports / "a" / "crafted.parquet").write_bytes(crafted.PAGE)
    write_parquet(exports / "a" / "clean.parquet", "SELECT CAST(id AS BIGINT) AS id, v", rows, ["id", "v"])
    write_csv(exports / "b" / "t.csv", ["id", "v"], rows)

    def doc(path: str, release: str) -> str:
        a = {"name": "t", "keyColumns": ["id"], "file": {"path": path, "format": "parquet"}}
        made = users_spec(a, csv_side("t", "t.csv", ["id", "v"], "id", None))
        made["metadata"] = made["metadata"] | {"version": release}
        key = {"name": "id", "type": "string", "class": "business_code"}
        return json.dumps(made | {"key": key, "security": {"unlisted": "clear"}})

    with _served(stack) as (port, _):

        def ran(path: str, release: str) -> Any:
            spec_id = _call(port, "POST", "/v1/specs", doc(path, release))["spec_id"]
            asked = json.dumps({"spec_id": spec_id, "mode": "batch"})
            run_id, deadline = _call(port, "POST", "/v1/runs", asked)["run_id"], time.monotonic() + 120
            while (run := _call(port, "GET", f"/v1/runs/{run_id}"))["status"] not in repo.TERMINAL:
                assert _call(port, "GET", "/v1/health") and time.monotonic() < deadline
                time.sleep(0.1)
            return run

        yield stack, port, ran


def test_the_server_s_log_says_how_its_reader_started(
    make_stack: Callable[..., Stack], exports: Path
) -> None:
    """READER §12: the reader's start line is in the log of ``ds serve``, which configures no logger of
    its own, whatever held: the line is a warning, the level every ``ds`` process prints. On Linux it says
    the data limit and, on a machine of ``reader.FILTERED``, that sockets are denied; on macOS that the
    platform has neither."""
    with _pages(make_stack, exports) as (stack, _, ran):
        clean = ran("clean.parquet", "1.0.0")
    assert (clean["status"], clean["scan"]["matched"]) == ("completed", 4)
    none = "not on this platform"
    filtered = reader.LINUX and os.uname().machine in reader.FILTERED
    held = (
        f"{'data limit [0-9,]+ MiB' if reader.LINUX else re.escape(f'no data limit ({none})')},"
        f" {'sockets denied' if filtered else re.escape(f'sockets not denied ({none})')}"
    )
    started = rf"the file reader started: duckdb {re.escape(version('duckdb'))} in process [0-9]+, {held}"
    said = (stack.settings.parent / "ds-serve.log").read_text()
    assert len(re.findall(f"^{started}$", said, re.MULTILINE)) == 1


@faults.real
def test_a_crafted_page_fails_its_run_and_no_other(make_stack: Callable[..., Stack], exports: Path) -> None:
    """READER §9, §15 (FILES §14 point 7): the kept mutant under ``ds serve``. A read of its changed page
    ends the reader's process, which is not the server's: that one stands, ``/v1/health`` answers
    throughout, the run's pass is paused twice and the run fails at the third end with F12, a closed text
    that names the cause and no file; a run of clean files completes after it, on a new reader. Elsewhere
    than macOS arm64 with DuckDB 1.5.5, where its scan may be a read or a closed refusal, each passes; on
    macOS, where the fault is a crash to the system, it runs with ``DS_FAULTS=1`` alone."""

    def serving(port: int) -> list[str]:
        listed = subprocess.run(["pgrep", "-f", f"serve --port {port} "], capture_output=True, text=True)
        return listed.stdout.split()

    with _pages(make_stack, exports) as (stack, port, ran):
        server = serving(port)
        ended, clean = ran("crafted.parquet", "1.0.0"), ran("clean.parquet", "1.1.0")
        assert len(server) == 1 and serving(port) == server and _call(port, "GET", "/v1/health")
    f12 = (
        r"ReaderDied: side a: the file reader's process ended 3 times \((SIGBUS|SIGSEGV)\) while only this"
        r" side's files were being read: they are read no more until DeltaSleuth restarts"
    )
    said = (stack.settings.parent / "ds-serve.log").read_text()
    if (sys.platform, platform.machine(), version("duckdb")) == ("darwin", "arm64", "1.5.5"):
        assert ended["status"] == "failed" and re.fullmatch(f12, ended["error"])
        assert said.count(f"scan of run {ended['run_id']} paused: Died") == 2
    else:
        assert ended["error"] is None or re.fullmatch(f12 + "|TargetRefused: [A-Za-z]+", ended["error"])
    assert (clean["status"], clean["scan"]["matched"]) == ("completed", 4)
    assert "crafted" not in said and str(exports) not in said


ACCOUNTS = """
    CREATE TABLE accounts (branch text, acct text, amount numeric(10, 2), PRIMARY KEY (branch, acct));
    INSERT INTO accounts VALUES ('eu', 'A-000001', 1.00), ('eu', 'A-000002', 2.00), ('us', 'A-000001', 3.00),
        ('us', 'A-000002', 4.50), ('fr', 'A-000001', 9.00);
"""


async def test_partitioned_files_with_a_composite_key_and_a_key_map(
    stacked: Callable[..., Stack], exports: Path
) -> None:
    """A glob of three Parquet files under key=value directories (§5: partitions read as text), a composite
    key whose second part a prefix_pad map derives from a number, against PostgreSQL."""
    stack = stacked(("files",), ("postgresql", ACCOUNTS))
    files = {
        "2024-01-01": [("eu", 1, "1.00"), ("eu", 2, "2.00")],
        "2024-01-02": [("us", 1, "3.00"), ("us", 2, "4.00")],
    }
    files["2024-01-03"] = [("eu", 3, "5.00")]
    select = "SELECT branch, CAST(no AS BIGINT) AS no, CAST(amount AS DECIMAL(10, 2)) AS amount"
    for day, rows in files.items():
        write_parquet(exports / "a" / f"dt={day}" / "part.parquet", select, rows, ["branch", "no", "amount"])
    pad = {"kind": "prefix_pad", "prefix": "A-", "width": 6, "sideType": "integer"}
    a = {
        "name": "accounts", "keyColumns": ["branch", {"name": "no", "keyMap": pad}], "columns": ["amount"],
        "file": {"path": "dt=*/part.parquet", "format": "parquet", "partitions": ["dt"]},
    }  # fmt: skip
    b = {"name": "accounts", "keyColumns": ["branch", "acct"], "columns": ["amount"]}
    doc = users_spec(a, b)
    doc["key"] = {"name": "acct", "type": "composite", "parts": ["branch", "acct"], "class": "business_code"}
    async with stack.context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        found = await results(ctx, run["run_id"])
    plan = run["params"]["tables"]["a"]
    assert (run["status"], plan["file_count"], plan["rows"]) == ("completed", 3, 5)
    assert scanned(run) == (5, 5, 0, 0, 3, 3)
    assert {key: r["outcome"] for key, r in found.items()} == {
        "us|A-000002": "MISMATCH", "eu|A-000003": "NOT_FOUND_B", "fr|A-000001": "NOT_FOUND_A",
    }  # fmt: skip


async def test_a_hash_sample_framed_by_a_file_side(
    make_stack: Callable[..., Stack], exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mixed pair's hash sample whose frame is a CSV side (SAMPLING §3, FILES.md §11): drawn by its strata
    column from the files, read whole, its census record says what it was read from; its drawn keys are read
    from the files in one pass for the group (§12), not one a key."""
    passes = _passes(monkeypatch)
    customers = [(f"C{n:03d}", f"n{n}", "A", "R" if n <= 10 else "B") for n in range(1, 21)]
    write_csv(exports / "a" / "customers.csv", ["id", "name", "status", "segment"], customers)

    async def customer(request: web.Request) -> web.Response:
        n = int(request.match_info["key"][1:])
        return web.json_response({"name": f"n{n}", "status": "A"})

    app = web.Application()
    app.router.add_get("/b/{key}", customer)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    service = {
        "base_url": f"http://127.0.0.1:{runner.addresses[0][1]}", "allow_private_network": True,
        "allow_insecure_http": True, "capabilities": {"authoritative_absence": {"status": [404]}},
    }  # fmt: skip
    a = csv_side("customers", "customers.csv", ["id", "name", "status", "segment"], "id", ["name", "status"])
    a["file"]["csv"].pop("keys")
    buckets = [{"label": "retail", "targetSize": 3, "values": ["R"]}, {"label": "other", "targetSize": 3}]
    strata = [{"name": "segment", "column": "segment", "buckets": buckets}]
    frame = {"kind": "table", "side": "a", "strataColumns": ["segment"]}
    sample = {
        "kind": "hash_sample",
        "method": "bottom_k",
        "salt": "files-q4",
        "strata": strata,
        "source": frame,
    }
    sides = {
        "a": {"role": "legacy", "target": "db-a", "table": a},
        "b": {
            "role": "new", "target": "svc", "request": {"method": "GET", "path": "/b/{{ key }}"},
            "notFound": {"status": [404]}, "truncation": {"itemsPath": "$.tags", "cap": 50},
        },
    }  # fmt: skip
    key = {
        "name": "id",
        "type": "string",
        "class": "business_code",
        "pattern": "^C[0-9]{3}$",
        "source": sample,
    }
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"})
    files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    targets = {"db-a": files_target(exports / "a"), "svc": service}
    try:
        stack = make_stack(yaml.safe_dump({"targets": targets}), files=files, concurrency=1)
        async with stack.context() as ctx:
            run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
            found = await results(ctx, run["run_id"])
            async with ctx.pool.connection() as conn:
                drawn = await repo.get_draw(conn, run["run_id"], 1)
                ended = await repo.get_cycle(conn, run["run_id"], 1)
    finally:
        await runner.cleanup()
    assert run["status"] == "completed" and drawn is not None and drawn["keys"] == 6 and len(passes) == 1
    assert len(found) == 6 and {r["outcome"] for r in found.values()} == {"MATCH"}
    record = ended["census"]["a"]
    assert (record["basis"], record["engine"], record["consistency"], record["files"]) == (
        "frame",
        "files",
        "fixed",
        1,
    )
    assert record["rows"] == 20 and record["verified_at"] and "snapshots" not in record


# --- the acceptance kit (examples/tables/files) ---------------------------------------------------------


async def test_the_files_kit_gives_its_expected_result(
    make_stack: Callable[..., Stack], exports: Path
) -> None:
    kit = REPO_ROOT / "examples" / "tables" / "files"
    subprocess.run([sys.executable, str(kit / "generate.py"), str(exports / "kit")], check=True)
    [target_id] = load_targets(kit / "targets.yaml")  # as shipped, it declares one files target
    entry = yaml.safe_load((kit / "targets.yaml").read_text())["targets"][target_id]
    targets = {target_id: entry | {"root": str(exports / "kit")}}
    stack = make_stack(yaml.safe_dump({"targets": targets}), files={"roots_under": str(exports)})
    async with stack.context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, kit / "spec.yaml"), "batch", who=TESTER)
        run = await finish(ctx, run_id)
        found = await results(ctx, run_id)
    counters, scan = run["counters"], ("rows_a", "rows_b", "skipped_a", "skipped_b", "matched", "candidates")
    printed = ("total", "compared", "match", "different", "indeterminate", "cancelled", "outstanding")
    plan = run["params"]["tables"]
    actual = {
        "status": run["status"],
        "scan": {name: run["scan"][name] for name in (*scan, "repeated_a", "repeated_b")},
        "worker_columns": plan["a"]["digest"],
        "files": {
            side: [plan[side]["file_count"], plan[side]["rows"], plan[side]["rejected"]] for side in "ab"
        },
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
    assert plan["a"]["as_of"] and plan["b"]["as_of"] is None  # side a's stamp file declares its time
    assert actual == json.loads((kit / "expected.json").read_text())


# --- performance (FILES.md §17): DS_PERF=1 runs these; TABLES.md §14 publishes what they measured --------

perf = pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
PEOPLE = ["userid", "email", "name", "amount"]
BUDGET = {"memory": "2GB", "spill": "20GB"}  # the files settings' defaults (§4)
GIB = 2**30
SERVICE = {
    "role": "new", "target": "svc", "request": {"method": "GET", "path": "/users/{{ key }}"},
    "notFound": {"status": [404]}, "truncation": {"itemsPath": "$.tags", "cap": 50},
}  # fmt: skip


def write_people(
    path: Path, rows: int, every: int = 0, *, form: str, order: str = "n", more: str = ""
) -> None:
    """``rows`` users of four columns (``test_engines.people``'s rows) written by DuckDB as ``form``, in
    ``order``; with ``every``, side b's: each ``every``-th e-mail in upper case and user 3 gone; ``more``:
    further columns."""
    email = "'user' || n || '@example.com'"
    if every:
        email = f"CASE WHEN n % {every} = 0 THEN upper({email}) ELSE {email} END"
    select = (
        f"SELECT n AS userid, {email} AS email, 'Name ' || n AS name,"  # noqa: S608 (numbers, constants)
        f" CAST(n * 1.25 AS DECIMAL(12, 2)) AS amount{more} FROM range(1, {rows + 1}) t(n)"
        f" WHERE n <> {3 if every else 0} ORDER BY {order}"
    )
    options = "FORMAT parquet" if form == "parquet" else "FORMAT csv, DELIMITER '|', HEADER true"
    path.parent.mkdir(parents=True, exist_ok=True)
    unrestricted().execute(f"COPY ({select}) TO '{path}' ({options})")


def people_spec(a: str, b: str, **overrides: Any) -> dict[str, Any]:
    """A spec of the people's two sides, each ``postgresql``, ``csv`` or ``parquet``. A CSV side's amount is
    declared a decimal against a side that types it; two CSV sides compare their fields as text."""
    tables: dict[str, dict[str, Any]] = {}
    more: dict[str, dict[str, Any]] = {}
    for side, form, other in (("a", a, b), ("b", b, a)):
        tables[side] = {"name": "users", "keyColumns": ["userid"]}
        if form == "csv":
            tables[side] = csv_side("users", "users.csv", PEOPLE, "userid", None)
            more |= {side: {"typeMap": {"amount": {"as": "decimal"}}}} if other != "csv" else {}
        elif form == "parquet":
            tables[side] |= {"file": {"path": "users.parquet", "format": "parquet"}}
    return users_spec(tables["a"], tables["b"], **more) | overrides


def people_stack(
    stacked: Callable[..., Stack], exports: Path, a: str, b: str, rows: int, every: int, **worker: Any
) -> Stack:
    """A stack of the people's two sides under the default files budget: side b's differ in ``every``."""
    seeds: list[tuple[str, ...]] = []
    for side, form, each in (("a", a, 0), ("b", b, every)):
        if form == "postgresql":
            seeds.append(("postgresql", people("postgresql", rows, each)))
        else:
            write_people(exports / side / f"users.{form}", rows, each, form=form)
            seeds.append(("files",))
    return stacked(*seeds, files=BUDGET, **worker)


async def _ran(stack: Stack, doc: dict[str, Any], rows: int, differing: int) -> tuple[repo.Row, float, float]:
    """A batch run of ``doc`` over ``rows`` a side: the run, the seconds its creation took (the plan of its
    file sides) and the seconds from then to its end."""
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, doc)
        started = time.monotonic()
        run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
        planned = time.monotonic() - started
        run = await finish(ctx, run_id, timeout=1800)
        elapsed = time.monotonic() - started - planned
    assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == (
        "completed", rows - differing, differing,
    )  # fmt: skip
    return run, planned, elapsed


def _spill_bytes(stack: Stack) -> int:
    """The bytes under the stack's spill directory now."""
    total = 0
    for folder, _, names in os.walk(stack.settings.parent / "evidence" / "spill"):
        for name in names:
            with suppress(OSError):
                total += os.stat(os.path.join(folder, name)).st_size
    return total


@contextmanager
def _spill_watch(stack: Stack) -> Iterator[list[int]]:
    """The most bytes seen under the stack's spill directory meanwhile, looked at twice a second."""
    peak, stop = [0], threading.Event()

    def watch() -> None:
        while not stop.wait(0.5):
            peak[0] = max(peak[0], _spill_bytes(stack))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield peak
    finally:
        stop.set()
        watcher.join()


def _keys_read(passes: Sequence[str]) -> int:
    """The keys the per-key statements asked a file side for, all together."""
    return sum(len(re.findall(r"\(\d+, \$v\d+", statement)) for statement in passes)


def _resident(pid: int) -> int:
    """The resident bytes of process ``pid`` and of its children, its file readers, now."""
    listed = subprocess.run(["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=False)
    found = (line.split() for line in listed.stdout.splitlines())
    return 1024 * sum(int(rss) for own, parent, rss in found if pid in (int(own), int(parent)))


@contextmanager
def _served(stack: Stack, beside: bool = False) -> Iterator[tuple[int, list[Any]]]:
    """``ds serve`` in developer mode on a free loopback port, its output in ``ds-serve.log`` beside the
    settings (``beside``: with ``--no-worker``, and a ``ds worker`` beside it); the port and, once stopped,
    the rusage of the process the worker ran in, then the most resident bytes seen of that process and its
    readers together, looked at once a second (READER.md §14)."""

    def ds(*args: str) -> subprocess.Popen[bytes]:
        argv = [str(Path(sys.executable).with_name("ds")), *args, "--config", str(stack.settings)]
        with (stack.settings.parent / f"ds-{args[0]}.log").open("ab") as log:
            return subprocess.Popen(argv, cwd=stack.settings.parent, stdout=log, stderr=subprocess.STDOUT)

    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    server = ds("serve", "--port", str(port), *(["--no-worker"] if beside else []))
    worker = ds("worker") if beside else server
    usage: list[Any] = []
    peak, stop = [0], threading.Event()

    def watch() -> None:
        while not stop.wait(1):
            peak[0] = max(peak[0], _resident(worker.pid))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                _call(port, "GET", "/v1/health")
                break
            except OSError:
                assert time.monotonic() < deadline, "ds serve did not start"
                time.sleep(0.5)
        yield port, usage
    finally:
        stop.set()
        watcher.join()
        usage += [bench._stop(worker), peak[0]]
        if beside:
            bench._stop(server)


def _p95(latencies: Sequence[float]) -> float:
    return statistics.quantiles(latencies, n=20, method="inclusive")[-1]


@perf
@pytest.mark.parametrize(
    ("a", "b", "limit"),
    [
        ("parquet", "postgresql", 120),
        ("csv", "postgresql", 120),
        ("parquet", "parquet", 30),
        ("csv", "csv", 60),
    ],
)
async def test_a_million_rows_of_files(
    stacked: Callable[..., Stack], exports: Path, a: str, b: str, limit: int
) -> None:
    rows = 1_000_000
    stack = people_stack(stacked, exports, a, b, rows, rows // 10)
    run, planned, elapsed = await _ran(stack, people_spec(a, b), rows, 11)
    print(f"\n{rows} rows a side, {a} against {b}: plan {planned:.1f} s, run {elapsed:.1f} s")
    # Two file sides alike: DuckDB hashes every column; across engines the worker does.
    assert run["params"]["tables"]["a"]["digest"] == ([] if a == b else ["amount", "email", "name"])
    assert run["counters"]["by_outcome"]["MISMATCH"] == 10
    assert planned + elapsed < limit


@perf
def test_ten_million_parquet_against_postgres(
    make_stack: Callable[..., Stack], databases: Callable[..., str], exports: Path, tmp_path: Path
) -> None:
    """10M rows a side under ``ds serve`` with the default files settings, a Parquet side against PostgreSQL
    (the worker hashes), the worker in the server's process and beside it: the plan's seconds and the rate
    its file hashes at, the scan's seconds, the worker process's peak resident memory, the most bytes seen
    in the spill (looked at once a second) and the p95 latency of the run's page, asked once a second."""
    rows = 10_000_000
    path = exports / "a" / "users.parquet"
    write_people(path, rows, form="parquet")
    (tmp_path / "dsn_b").write_text(databases("b", people("postgresql", rows, rows // 100)))
    targets = {"db-a": files_target(exports / "a"), "db-b": {"dsn_ref": "file:dsn_b", "rate_per_s": 1000}}
    stack = make_stack(yaml.safe_dump({"targets": targets}), files={"roots_under": str(exports)})
    size, started = path.stat().st_size, time.monotonic()
    asyncio.run(manifest._hashed(str(path), end=None, cached=False))
    hashed = size / (time.monotonic() - started) / 2**20
    for beside in (False, True):
        named = {"name": f"people-{'beside' if beside else 'inside'}", "version": "1.0.0", "owner": "tests"}
        doc = people_spec("parquet", "postgresql", metadata=named)
        with _served(stack, beside) as (port, usage):
            spec_id = _call(port, "POST", "/v1/specs", json.dumps(doc))["spec_id"]
            started = time.monotonic()
            run_id = _call(port, "POST", "/v1/runs", json.dumps({"spec_id": spec_id, "mode": "batch"}))[
                "run_id"
            ]
            planned = time.monotonic() - started
            started, latencies, scan_s, spill = time.monotonic(), [], None, 0
            while True:
                asked = time.monotonic()
                run = _call(port, "GET", f"/v1/runs/{run_id}")
                if scan_s is None and (run["scan"] or {}).get("done"):
                    scan_s = asked - started
                if scan_s is None:
                    latencies.append(time.monotonic() - asked)
                spill = max(spill, _spill_bytes(stack))
                if run["status"] in repo.TERMINAL:
                    break
                assert time.monotonic() - started < 3600, "the run did not end"
                time.sleep(max(0.0, 1 - (time.monotonic() - asked)))
            total = time.monotonic() - started
        own, peak = bench._rss_bytes(usage[0].ru_maxrss), usage[1]
        sort_bytes = run["params"]["tables"]["a"]["sort_bytes"]
        print(
            f"\n{rows} rows a side, parquet ({size / 2**20:.0f} MiB, hashed at {hashed:.0f} MiB/s) against"
            f" postgresql, the worker {'beside' if beside else 'in'} ds serve: plan {planned:.1f} s, scan"
            f" {scan_s or 0:.0f} s ({(scan_s or 0) / 77.5:.1f} times PostgreSQL's 77.5 s), run {total:.0f} s,"
            f" worker and reader peak {peak / 2**20:.0f} MiB (the worker's own {own / 2**20:.0f} MiB), spill"
            f" peak {spill / 2**20:.0f} MiB (sort_bytes"
            f" {sort_bytes / 2**20:.0f} MiB), GET /v1/runs/{{id}} p95 {_p95(latencies) * 1000:.0f} ms (max"
            f" {max(latencies) * 1000:.0f} ms, {len(latencies)} asked)"
        )
        assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == (
            "completed", rows - 101, 101,
        )  # fmt: skip
        assert peak < 3 * GIB and spill < 4 * GIB  # files.memory plus 1 GiB, the reader's process counted
        assert scan_s is not None and scan_s < 600


@perf
@pytest.mark.parametrize(("form", "limit"), [("parquet", 180), ("csv", 300)])
async def test_ten_million_rows_of_files(
    stacked: Callable[..., Stack], exports: Path, form: str, limit: int
) -> None:
    rows = 10_000_000
    stack = people_stack(stacked, exports, form, form, rows, rows // 100)
    with _spill_watch(stack) as spill:
        run, planned, elapsed = await _ran(stack, people_spec(form, form), rows, 101)
    plan = run["params"]["tables"]["a"]
    print(
        f"\n{rows} rows a side, {form} against {form} ({plan['bytes'] / 2**20:.0f} MiB a side): plan"
        f" {planned:.1f} s (pass {plan['pass_s']} s), run {elapsed:.1f} s, spill peak {spill[0] / 2**20:.0f}"
        f" MiB ({spill[0] / rows:.0f} bytes a row; sort_bytes {plan['sort_bytes'] / rows:.0f} a row)"
    )
    assert plan["digest"] == []  # DuckDB hashes every column
    assert planned + elapsed < limit


@perf
@pytest.mark.parametrize(
    ("form", "order", "limit"), [("parquet", "n", 5), ("csv", "n", 30), ("parquet", "hash(n)", None)]
)
async def test_a_group_against_ten_million_rows(
    stacked: Callable[..., Stack], exports: Path, monkeypatch: pytest.MonkeyPatch, form: str, order: str,
    limit: int | None,
) -> None:  # fmt: skip
    """One group of 200 keys read from 10M rows of files: one statement, one pass over them."""
    rows = 10_000_000
    write_people(exports / "a" / f"users.{form}", rows, form=form, order=order)
    stack = stacked(("files",), ("postgresql", people("postgresql", 100)), files=BUDGET, concurrency=1)
    took: list[float] = []
    read = duck.Files.rows

    async def timed(self: Any, session: Any, side: Any, asked: Any) -> Any:
        started = time.monotonic()
        try:
            return await read(self, session, side, asked)
        finally:
            took.append(time.monotonic() - started)

    monkeypatch.setattr(duck.Files, "rows", timed)
    keys = [str(n * (rows // 200)) for n in range(1, 201)]
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, people_spec(form, "postgresql"))
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", keys=keys, who=TESTER), timeout=600)
    plan = run["params"]["tables"]["a"]
    sorted_ = "sorted by key" if order == "n" else "not sorted"
    print(
        f"\na group of 200 keys of {rows} rows, {form} {sorted_}: {took[0]:.1f} s (the plan's pass"
        f" {plan['pass_s']} s)"
    )
    # Side b holds none of them; its recheck reads side b again and keeps the files' first observation.
    assert (run["status"], run["counters"]["by_outcome"]["NOT_FOUND_B"], len(took)) == ("completed", 200, 1)
    assert limit is None or took[0] < limit


@perf
@pytest.mark.parametrize(("form", "limit"), [("parquet", 120), ("csv", 240)])
async def test_batches_on_files(
    stacked: Callable[..., Stack], exports: Path, monkeypatch: pytest.MonkeyPatch, form: str, limit: int
) -> None:
    # 20,000 of 100,000 rows differ; each is read, confirmed and saved (two PostgreSQL tables: 43 s).
    rows = 100_000
    write_people(exports / "a" / f"users.{form}", rows, form=form)
    seed = people("postgresql", rows, 5).replace("DELETE FROM users WHERE userid = 3;", "")
    stack = stacked(("files",), ("postgresql", seed), files=BUDGET)
    passes = _passes(monkeypatch)
    doc = people_spec(form, "postgresql", execution={"concurrency": 8})
    run, planned, elapsed = await _ran(stack, doc, rows, rows // 5)
    print(
        f"\n{rows // 5} differing rows of {rows}, {form} against postgresql: plan {planned:.1f} s, run"
        f" {elapsed:.1f} s, {len(passes)} file statements"
    )
    assert run["counters"]["by_outcome"]["MISMATCH"] == rows // 5
    assert run["counters"]["by_confirmation"]["rechecked"] == rows // 5
    assert _keys_read(passes) == rows // 5  # each key read from the files once: no recheck reads them
    assert planned + elapsed < limit


def _users(differ: int) -> web.Application:
    """A service of the people: every ``differ``-th one's e-mail in upper case."""

    async def user(request: web.Request) -> web.Response:
        n = int(request.match_info["key"])
        email = f"user{n}@example.com"
        found = {
            "email": email.upper() if n % differ == 0 else email,
            "name": f"Name {n}",
            "amount": n * 1.25,
        }
        return web.json_response(found)

    app = web.Application()
    app.router.add_get("/users/{key}", user)
    return app


async def _service(app: web.Application) -> tuple[web.AppRunner, dict[str, Any]]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, {
        "base_url": f"http://127.0.0.1:{runner.addresses[0][1]}", "allow_private_network": True,
        "allow_insecure_http": True, "capabilities": {"authoritative_absence": {"status": [404]}},
        "rate_per_s": 1000, "max_concurrency": 8,
    }  # fmt: skip


@perf
async def test_a_mixed_pair_on_files(
    make_stack: Callable[..., Stack], exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """20,000 keys of a 1M-row CSV side against a service: a pass over the files a group of 200, and none
    for the 200 keys whose difference is rechecked."""
    rows, wanted = 1_000_000, 20_000
    write_people(exports / "a" / "users.csv", rows, form="csv")
    runner, service = await _service(_users(5000))
    a = csv_side("users", "users.csv", PEOPLE, "userid", None)
    sides = {
        "a": {"role": "legacy", "target": "db-a", "table": a, "typeMap": {"amount": {"as": "decimal"}}},
        "b": SERVICE,
    }
    keys = [str(n) for n in range(rows // wanted, rows + 1, rows // wanted)]
    key = {"name": "userid", "type": "string", "class": "business_code", "pattern": "^[0-9]+$"}
    doc = table_spec(
        sides=sides, key=key | {"source": {"kind": "list", "keys": keys}}, execution={"concurrency": 8}
    )
    targets = {"db-a": files_target(exports / "a"), "svc": service}
    passes = _passes(monkeypatch)
    try:
        stack = make_stack(yaml.safe_dump({"targets": targets}), files={"roots_under": str(exports)})
        async with stack.context() as ctx:
            spec_id = await store_spec(ctx, doc)
            started = time.monotonic()
            run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
            planned = time.monotonic() - started
            run = await finish(ctx, run_id, timeout=1800)
            elapsed = time.monotonic() - started - planned
    finally:
        await runner.cleanup()
    print(
        f"\n{wanted} keys of a {rows}-row CSV against a service: plan {planned:.1f} s (pass"
        f" {run['params']['tables']['a']['pass_s']} s), run {elapsed:.1f} s, {len(passes)} file statements"
    )
    outcomes = run["counters"]["by_outcome"]
    assert (run["status"], outcomes["MATCH"], outcomes["MISMATCH"]) == ("completed", wanted - 200, 200)
    # Each key is read from the files once, 200 a statement: a lane's first group of a sweep may be smaller.
    assert _keys_read(passes) == wanted and len(passes) <= wanted // 200 + 4
    assert planned + elapsed < 300


@perf
async def test_a_frame_of_a_file(
    make_stack: Callable[..., Stack], exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bottom_k sample of 3,000 keys framed by a 10M-row Parquet side against a service: the seconds from
    the run's creation to its draw (the frame read whole), and its keys read in 15 passes at most."""
    rows = 10_000_000
    segment = ", CASE WHEN n % 2 = 0 THEN 'R' ELSE 'B' END AS segment"
    write_people(exports / "a" / "users.parquet", rows, form="parquet", more=segment)
    runner, service = await _service(_users(rows + 1))
    a = {
        "name": "users", "keyColumns": ["userid"], "columns": ["email", "name", "amount"],
        "file": {"path": "users.parquet", "format": "parquet"},
    }  # fmt: skip
    buckets = [
        {"label": "retail", "targetSize": 1500, "values": ["R"]},
        {"label": "other", "targetSize": 1500},
    ]
    sample = {
        "kind": "hash_sample", "method": "bottom_k", "salt": "files-perf",
        "strata": [{"name": "segment", "column": "segment", "buckets": buckets}],
        "source": {"kind": "table", "side": "a", "strataColumns": ["segment"]},
    }  # fmt: skip
    key = {
        "name": "userid",
        "type": "string",
        "class": "business_code",
        "pattern": "^[0-9]+$",
        "source": sample,
    }
    sides = {"a": {"role": "legacy", "target": "db-a", "table": a}, "b": SERVICE}
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"}, execution={"concurrency": 8})
    targets = {"db-a": files_target(exports / "a"), "svc": service}
    passes = _passes(monkeypatch)
    try:
        stack = make_stack(yaml.safe_dump({"targets": targets}), files={"roots_under": str(exports)})
        async with stack.context() as ctx:
            started = time.monotonic()
            run = await finish(
                ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER), timeout=1800
            )
            elapsed = time.monotonic() - started
            async with ctx.pool.connection() as conn:
                drawn = await repo.get_draw(conn, run["run_id"], 1)
    finally:
        await runner.cleanup()
    assert run["status"] == "completed" and drawn is not None
    framed = (drawn["drawn_at"] - run["created_at"]).total_seconds()
    print(
        f"\na sample of {drawn['keys']} keys framed by {rows} rows of parquet: frame {framed:.0f} s, run"
        f" {elapsed:.0f} s, {len(passes)} file statements"
    )
    assert drawn["keys"] == 3000 and run["counters"]["by_outcome"]["MATCH"] == 3000
    assert _keys_read(passes) == 3000 and len(passes) <= 15


@perf
def test_plan_of_a_gigabyte(make_stack: Callable[..., Stack], exports: Path) -> None:
    """A 1 GiB CSV side planned inside ``POST /v1/runs`` (hashed, its first record read, counted, its end
    checked), the page of another run asked ten times a second meanwhile."""
    rows = 18_700_000
    path = exports / "a" / "users.csv"
    write_people(path, rows, form="csv")
    write_people(exports / "b" / "users.csv", 1000, form="csv")
    assert path.stat().st_size >= GIB
    targets = {"db-a": files_target(exports / "a"), "db-b": files_target(exports / "b")}
    stack = make_stack(yaml.safe_dump({"targets": targets}), files={"roots_under": str(exports)})
    small = people_spec("csv", "csv", metadata={"name": "small", "version": "1.0.0", "owner": "tests"})
    small["sides"]["a"]["target"] = "db-b"
    large = people_spec("csv", "csv", execution={"timeouts": {"total": "600s"}})
    with _served(stack, beside=True) as (port, _):
        asked_for = [_call(port, "POST", "/v1/specs", json.dumps(doc))["spec_id"] for doc in (small, large)]
        run_id = _call(port, "POST", "/v1/runs", json.dumps({"spec_id": asked_for[0], "mode": "batch"}))[
            "run_id"
        ]
        planned: list[Any] = []

        def plan() -> None:
            started = time.monotonic()
            conn_run = _call(port, "POST", "/v1/runs", json.dumps({"spec_id": asked_for[1], "mode": "batch"}))
            planned.extend([time.monotonic() - started, conn_run["run_id"]])

        posting = threading.Thread(target=plan)
        posting.start()
        latencies = []
        while posting.is_alive():
            asked = time.monotonic()
            _call(port, "GET", f"/v1/runs/{run_id}")
            latencies.append(time.monotonic() - asked)
            time.sleep(0.1)
        posting.join()
        side = _call(port, "GET", f"/v1/runs/{planned[1]}")["params"]["tables"]["a"]
    print(
        f"\nthe plan of a {path.stat().st_size / GIB:.2f} GiB CSV of {side['rows']} rows: {planned[0]:.1f} s"
        f" (its counting pass {side['pass_s']} s), GET /v1/runs/{{id}} p95 {_p95(latencies) * 1000:.0f} ms"
        f" (max {max(latencies) * 1000:.0f} ms, {len(latencies)} asked)"
    )
    assert side["rows"] == rows and planned[0] < 30 and _p95(latencies) < 1


@perf
def test_two_scans_share_one_budget(
    make_stack: Callable[..., Stack], databases: Callable[..., str], exports: Path, tmp_path: Path
) -> None:
    """Two runs of 1M rows a side, Parquet against PostgreSQL, started together under one ``ds serve``: both
    complete, the second's sort waiting for the first's, in one process's memory budget."""
    rows = 1_000_000
    write_people(exports / "a" / "users.parquet", rows, form="parquet")
    (tmp_path / "dsn_b").write_text(databases("b", people("postgresql", rows, rows // 10)))
    targets = {"db-a": files_target(exports / "a"), "db-b": {"dsn_ref": "file:dsn_b", "rate_per_s": 1000}}
    stack = make_stack(yaml.safe_dump({"targets": targets}), files={"roots_under": str(exports)})
    with _served(stack) as (port, usage):
        run_ids = []
        for name in ("first", "second"):
            named = {"name": name, "version": "1.0.0", "owner": "tests"}
            doc = people_spec("parquet", "postgresql", metadata=named)
            spec_id = _call(port, "POST", "/v1/specs", json.dumps(doc))["spec_id"]
            created = _call(port, "POST", "/v1/runs", json.dumps({"spec_id": spec_id, "mode": "batch"}))
            run_ids.append(created["run_id"])
        started = time.monotonic()
        while True:
            runs = [_call(port, "GET", f"/v1/runs/{run_id}") for run_id in run_ids]
            if all(run["status"] in repo.TERMINAL for run in runs):
                break
            assert time.monotonic() - started < 900, "the runs did not end"
            time.sleep(1)
        total = time.monotonic() - started
    own, peak = bench._rss_bytes(usage[0].ru_maxrss), usage[1]
    waited = "a sort of a file side waits for another" in (tmp_path / "ds-serve.log").read_text()
    print(
        f"\ntwo runs of {rows} rows a side together: {total:.0f} s, server and reader peak"
        f" {peak / 2**20:.0f} MiB (the server's own {own / 2**20:.0f} MiB)"
    )
    for run in runs:
        assert (run["status"], run["scan"]["matched"], run["scan"]["candidates"]) == (
            "completed", rows - 11, 11,
        )  # fmt: skip
    assert waited and peak < 3 * GIB  # one sort at a time, in files.memory plus 1 GiB
