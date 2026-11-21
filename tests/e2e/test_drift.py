"""A table that changes under a soak, on each engine: between the cycles of
a soak over two tables a column is added (warned), a key column widened (warned, the soak goes on) and a
compared column dropped (paused, not failed); retyped, it keeps the run from resuming, and a new run plans
the table as it is now. With DS_PERF=1 the guard's looks at a scan pass and at a cycle's start are
timed.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from deltasleuth.connectors.tables import ENGINES
from deltasleuth.runner import drift, run_worker
from deltasleuth.runner.runs import cancel_run, create_run, resume_run
from deltasleuth.store import repo
from tests.conftest import TESTER, MySQLServer, OracleServer
from tests.e2e.conftest import Stack, both, finish, store_spec, table_spec

pytestmark = pytest.mark.e2e

USERS = """
    CREATE TABLE users (userid varchar(10) PRIMARY KEY, email text, amount numeric(10, 2));
    INSERT INTO users SELECT 'u' || g, 'user' || g || '@example.com', g * 1.25 FROM generate_series(1, 20) g;
"""
SPEC = table_spec(
    sides=both({"name": "users", "keyColumns": ["userid"]}),
    execution={"cycleInterval": "3s", "stop": {"maxCycles": 9}},
)


async def _run(ctx: Any, run_id: str) -> repo.Row:
    async with ctx.pool.connection() as conn:
        return await repo.get_run(conn, run_id)


async def _opened(ctx: Any, run_id: str, cycle: int) -> None:
    """Wait for the soak to open ``cycle``: its scan is due a cycle interval after the one before."""
    async with asyncio.timeout(60):
        while (await _run(ctx, run_id))["cycle"] < cycle:
            await asyncio.sleep(0.02)


async def _drifted(ctx: Any, run_id: str) -> list[dict[str, Any]]:
    async with ctx.pool.connection() as conn:
        rows = await repo.pages(repo.list_audit, conn, action="schema.drift", obj=run_id)
    return [row["detail"] for row in rows[::-1]]


async def test_a_table_soak_meets_a_table_that_changes_between_its_cycles(
    tables: Callable[..., Stack], tmp_path: Any
) -> None:
    stack = tables(USERS, USERS)

    def alter(side: str, statement: str) -> None:
        with psycopg.connect((tmp_path / f"dsn_{side}").read_text(), autocommit=True) as conn:
            conn.execute(statement)  # type: ignore[arg-type]

    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, SPEC)
        run_id = await create_run(ctx, spec_id, "soak", who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(ctx, stop=stop))
        try:
            await _opened(ctx, run_id, 2)
            alter("b", "ALTER TABLE users ADD COLUMN loyalty_tier text")  # warned
            await _opened(ctx, run_id, 3)
            alter("a", "ALTER TABLE users ALTER COLUMN userid TYPE varchar(20)")  # warned: the soak goes on
            await _opened(ctx, run_id, 4)
            alter("b", "ALTER TABLE users DROP COLUMN amount")  # paused, not failed
            async with asyncio.timeout(60):
                while (run := await _run(ctx, run_id))["status"] == "running":
                    await asyncio.sleep(0.02)
        finally:
            stop.set()
            await worker
        drifted = await _drifted(ctx, run_id)
        lines = drift.lines(run)
        # Retyped, the column is back under another class: the run's plan would render other values.
        alter("b", "ALTER TABLE users ADD COLUMN amount text")
        with pytest.raises(drift.Refused) as refused:
            await resume_run(ctx, run_id, who=TESTER)
        # A new run plans the table as it is now: it would not compare what the soak compared, so it is
        # created paused, and a resume accepts its tables.
        planned = await create_run(ctx, spec_id, "batch", who=TESTER)
        created = await _run(ctx, planned)
        assert await resume_run(ctx, planned, who=TESTER) == "running"
        ended = await finish(ctx, planned)
        assert await cancel_run(ctx, run_id, who=TESTER) == "cancelled"

    assert (run["status"], run["stop_reason"], run["error"], run["cycle"]) == (
        "paused", drift.SCHEMA_PAUSE, None, 4,
    )  # fmt: skip
    assert [(d["cycle"], d["where"], d["paused"]) for d in drifted] == [
        (2, "scan", False), (3, "scan", False), (4, "scan", True),
    ]  # fmt: skip
    assert [d["sides"][side]["kinds"] for d, side in zip(drifted, "bab", strict=True)] == [
        {"column_added": 1}, {"type_changed": 1}, {"column_missing": 1},
    ]  # fmt: skip
    assert lines[:3] == [
        "Paused by the schema guard at the start of cycle 4: side b: column 'amount' of public.users is gone,"
        " or the role may no longer read it. Put it back and resume the run, or cancel it and start a new"
        " run, which plans the table as it is now.",
        "side a: column 'userid' was character varying(10) and is character varying(20) now (still text)",
        "side b gained column 'loyalty_tier' since this run was planned: this run does not compare it",
    ]
    assert refused.value.status == 409 and str(refused.value) == (
        f"run {run_id} cannot resume: side b: column 'amount' of public.users was numeric(10,2) (decimal)"
        " when this run was planned and is text (text) now: its values would no longer render as planned"
    )
    assert (created["status"], created["stop_reason"]) == ("paused", drift.SCHEMA_PAUSE)
    assert drift.lines(created)[0] == (
        "Paused by the schema guard before any table row was read: column 'amount' was numeric(10,2)"
        f" (decimal) in run {run_id} of this version and is text (text) now. Resume to accept the tables as"
        " they are, or cancel."
    )
    assert ended["status"] in ("completed", "completed_with_issues") and ended["scan"]["rows_b"] == 20


# The same table, the same changes, on MySQL and on Oracle (Oracle's unquoted names are upper case). The
# column comes back as an integer: a number and a text are two families, which their plan refuses.
ENGINE_USERS = {
    "mysql": """
        SET SESSION cte_max_recursion_depth = 100;
        CREATE TABLE users (userid VARCHAR(10) PRIMARY KEY, email VARCHAR(100), amount DECIMAL(10, 2));
        INSERT INTO users WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 20)
        SELECT CONCAT('u', n), CONCAT('user', n, '@example.com'), n * 1.25 FROM g;
    """,
    "oracle": """
        CREATE TABLE users (userid VARCHAR2(10) PRIMARY KEY, email VARCHAR2(100), amount NUMBER(10, 2));
        INSERT INTO users SELECT 'u' || LEVEL, 'user' || LEVEL || '@example.com', LEVEL * 1.25
        FROM DUAL CONNECT BY LEVEL <= 20
    """,
}
CHANGES = {
    "mysql": (
        "ALTER TABLE users ADD COLUMN loyalty_tier TEXT", "ALTER TABLE users MODIFY userid VARCHAR(20)",
        "ALTER TABLE users DROP COLUMN amount", "ALTER TABLE users ADD COLUMN amount BIGINT",
    ),
    "oracle": (
        "ALTER TABLE users ADD (loyalty_tier VARCHAR2(20))", "ALTER TABLE users MODIFY (userid VARCHAR2(20))",
        "ALTER TABLE users DROP COLUMN amount", "ALTER TABLE users ADD (amount NUMBER(10))",
    ),
}  # fmt: skip


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param("mysql", marks=pytest.mark.mysql),
        pytest.param("oracle", marks=[pytest.mark.mysql, pytest.mark.oracle]),
    ],
)
async def test_a_table_soak_meets_a_table_that_changes_between_its_cycles_on_each_engine(
    engines: Callable[..., Stack], request: pytest.FixtureRequest, engine: str
) -> None:
    added, widened, dropped, retyped = CHANGES[engine]
    stack = engines((engine, ENGINE_USERS[engine]), (engine, ENGINE_USERS[engine]))
    async with stack.context() as ctx:
        database = {side: ctx.targets.get(f"db-{side}").database for side in "ab"}

        def alter(side: str, statement: str) -> None:
            if engine == "mysql":
                server: MySQLServer = request.getfixturevalue("mysql_server")
                server.execute(statement, database[side].name)  # type: ignore[union-attr]
            else:
                oracle: OracleServer = request.getfixturevalue("oracle_server")
                oracle.execute(statement, database[side].user)  # type: ignore[union-attr]

        spec_id = await store_spec(ctx, SPEC)
        run_id = await create_run(ctx, spec_id, "soak", who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(ctx, stop=stop))
        try:
            await _opened(ctx, run_id, 2)
            alter("b", added)  # warned
            await _opened(ctx, run_id, 3)
            alter("a", widened)  # warned: the soak goes on
            await _opened(ctx, run_id, 4)
            alter("b", dropped)  # paused, not failed
            async with asyncio.timeout(90):
                while (run := await _run(ctx, run_id))["status"] == "running":
                    await asyncio.sleep(0.02)
        finally:
            stop.set()
            await worker
        drifted = await _drifted(ctx, run_id)
        lines = drift.lines(run)
        alter("b", retyped)
        with pytest.raises(drift.Refused) as refused:
            await resume_run(ctx, run_id, who=TESTER)
        planned = await create_run(ctx, spec_id, "batch", who=TESTER)
        created = await _run(ctx, planned)
        assert await resume_run(ctx, planned, who=TESTER) == "running"
        ended = await finish(ctx, planned)
        assert await cancel_run(ctx, run_id, who=TESTER) == "cancelled"

    oracle_ = engine == "oracle"
    relation = f"{database['b'].user}.USERS" if oracle_ else f"{database['b'].name}.users"  # type: ignore[union-attr]
    amount, userid = ("AMOUNT", "USERID") if oracle_ else ("amount", "userid")
    was, now = ("VARCHAR2(10)", "VARCHAR2(20)") if oracle_ else ("varchar(10)", "varchar(20)")
    assert (run["status"], run["stop_reason"], run["error"]) == ("paused", drift.SCHEMA_PAUSE, None)
    assert [(d["where"], d["paused"]) for d in drifted][-3:] == [
        ("scan", False),
        ("scan", False),
        ("scan", True),
    ]
    assert [d["sides"][side]["kinds"] for d, side in zip(drifted[-3:], "bab", strict=True)] == [
        {"column_added": 1}, {"type_changed": 1}, {"column_missing": 1},
    ]  # fmt: skip
    assert lines[:2] == [
        f"Paused by the schema guard at the start of cycle {run['cycle']}: side b: column '{amount}' of"
        f" {relation} is gone, or the role may no longer read it. Put it back and resume the run, or cancel"
        " it and start a new run, which plans the table as it is now.",
        f"side a: column '{userid}' was {was} and is {now} now (still text)",
    ]
    assert refused.value.status == 409 and str(refused.value).startswith(
        f"run {run_id} cannot resume: side b: column '{amount}' of {relation} was "
    )
    assert (created["status"], created["stop_reason"]) == ("paused", drift.SCHEMA_PAUSE)
    assert ended["status"] in ("completed", "completed_with_issues") and ended["scan"]["rows_b"] == 20


# --- performance: DS_PERF=1 runs it ---------------------------------------------------------------------

perf = pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
KINDS = ("int", "text", "numeric(12, 2)", "timestamp")
WIDE = f"""
    CREATE TABLE wide (userid bigint PRIMARY KEY, {", ".join(f"c{i} {KINDS[i % 4]}" for i in range(199))});
    INSERT INTO wide (userid) SELECT g FROM generate_series(1, 1000) g;
"""  # noqa: S608 (constants)


@perf
async def test_guard_costs_a_catalogue_read(
    tables: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema guard's looks at two tables of 200 columns: at a scan pass, each side's catalogue read on
    its pass's session and the judgement recorded, at each of a table soak's three passes; at a cycle's
    start, a short session a side, its catalogue read and the judgement recorded, at each of a key-list
    soak's two later cycle starts."""
    engine = ENGINES["postgresql"]
    describe, check, cycle_start = engine.describe, drift.check, drift.cycle_start
    described: list[float] = []
    v2: list[float] = []
    v3: list[float] = []

    async def timed_describe(*args: Any) -> Any:
        began = time.perf_counter()
        try:
            return await describe(*args)
        finally:
            described.append(time.perf_counter() - began)

    async def timed_check(ctx: Any, run: Any, spec: Any, relations: Any, where: str, **kwargs: Any) -> bool:
        began = time.perf_counter()
        try:
            return await check(ctx, run, spec, relations, where, **kwargs)
        finally:
            if where in ("scan", "resume"):  # the pass described both sides just before
                v2.append(time.perf_counter() - began + sum(described[-2:]))

    async def timed_cycle_start(ctx: Any, run_id: str) -> None:
        began = time.perf_counter()
        try:
            await cycle_start(ctx, run_id)
        finally:
            v3.append(time.perf_counter() - began)

    monkeypatch.setattr(engine, "describe", timed_describe)
    monkeypatch.setattr(drift, "check", timed_check)
    monkeypatch.setattr(drift, "cycle_start", timed_cycle_start)
    sides = both({"name": "wide", "keyColumns": ["userid"]})
    soak = table_spec(sides=sides, execution={"cycleInterval": "1s", "stop": {"maxCycles": 3}})
    async with tables(WIDE, WIDE).context() as ctx:
        spec_id = await store_spec(ctx, soak)
        scanned = await finish(ctx, await create_run(ctx, spec_id, "soak", who=TESTER), timeout=300)
        keyed = await create_run(ctx, spec_id, "soak", keys=[str(key) for key in range(1, 21)], who=TESTER)
        listed = await finish(ctx, keyed, timeout=300)
    print(
        f"\nschema guard, two tables of 200 columns: V2 {', '.join(f'{s * 1000:.0f}' for s in v2)} ms;"
        f" V3 {', '.join(f'{s * 1000:.0f}' for s in v3)} ms"
    )
    assert (scanned["cycle"], listed["cycle"], len(v2), len(v3)) == (3, 3, 3, 2)
    assert max(v2 + v3) < 1
