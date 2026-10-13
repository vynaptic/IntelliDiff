"""The hash-chained audit log (docs/mvp/PACK.md §4.1, §8): every audit row, whoever writes it, gets a
chain row in commit order; concurrent writers stay contiguous, a stale REPEATABLE READ writer fails; the
stored text is the same in every TimeZone, and ``ds audit verify`` agrees with it in each; 009 chains
the rows already written."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from deltasleuth.auth.principal import WORKER
from deltasleuth.store import chain, repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.conftest import TESTER

pytestmark = pytest.mark.db

ZERO = "0" * 64
ROW = """INSERT INTO ds.audit_log (id, at, actor, actor_name, action, object, detail, request_id, session_id,
             client_addr)
         VALUES (%s, '2026-09-28 03:32:03.4+02:30', 'user:U-0123456789abcdef', 'alice', 'approval.grant',
             'A-01J0000000000000000000000Q', '{"b": [1, 2.50, "é"], "a": null, "c": {"z": 1, "y": true}}',
             'Q-0123456789abcdef', 'W-0123456789abcdef', '10.1.2.3')"""
BARE = """INSERT INTO ds.audit_log (id, at, actor, action, object)
          VALUES (%s, '2026-09-28 01:02:03+00', 'system:worker', 'run.cycle', 'R-1')"""
# ds.audit_text of ROW and BARE: jsonb's key order (shorter keys first), `at` in UTC to the microsecond.
GOLDEN = (
    '{{"at": "2026-09-28T01:02:03.400000Z", "id": {id}, "actor": "user:U-0123456789abcdef", "action":'
    ' "approval.grant", "detail": {{"a": null, "b": [1, 2.50, "é"], "c": {{"y": true, "z": 1}}}}, "object":'
    ' "A-01J0000000000000000000000Q", "actor_name": "alice", "request_id": "Q-0123456789abcdef",'
    ' "session_id": "W-0123456789abcdef", "client_addr": "10.1.2.3"}}'
)
GOLDEN_BARE = (
    '{{"at": "2026-09-28T01:02:03.000000Z", "id": {id}, "actor": "system:worker", "action": "run.cycle",'
    ' "detail": {{}}, "object": "R-1", "actor_name": null, "request_id": null, "session_id": null,'
    ' "client_addr": null}}'
)
TIME_ZONES = ("UTC", "Asia/Kolkata", "America/St_Johns")


async def _chain(conn: repo.Conn) -> list[dict[str, Any]]:
    """Every chain row in seq order, with its audit row's text as computed now."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """SELECT c.*, ds.audit_text(l) AS now_text FROM ds.audit_chain c JOIN ds.audit_log l USING (id)
               ORDER BY c.seq"""
        )
        return await cur.fetchall()


def _linked(rows: list[dict[str, Any]]) -> bool:
    """Contiguous from 1, each row hashing to its entry, following the row before it and its object's
    previous row, its text its audit row's."""
    prev, last = ZERO, {}
    for n, row in enumerate(rows, 1):
        obj_prev = last.get(row["object"], ZERO)
        if (row["seq"], row["prev"], row["obj_prev"], row["text"]) != (n, prev, obj_prev, row["now_text"]):
            return False
        if row["hash"] != chain.chain_hash(prev, obj_prev, row["text"]):
            return False
        prev = last[row["object"]] = row["hash"]
    return True


async def test_every_writer_is_chained(pool: AsyncConnectionPool, pg_dsn: str) -> None:
    async with repo.audited(pool) as (_, audit):  # the app: several rows in one transaction
        first = await audit(TESTER, "spec.create", "1", {"content_hash": "sha256:0"})
        second = await audit(WORKER, "run.cycle", "R-1", {"cycle": 2})
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as psql:  # anyone else
        await psql.execute(
            "INSERT INTO ds.audit_log (actor, action, object) VALUES ('system:worker', 'x', '1')"
        )
        await psql.execute(
            "INSERT INTO ds.audit_log (actor, action, object)"
            " SELECT 'system:server', 'y', o FROM unnest(ARRAY['R-1', '2', 'R-1']) AS o"
        )
        async with psql.cursor().copy("COPY ds.audit_log (actor, action, object) FROM STDIN") as copy:
            await copy.write_row(("system:worker", "z", "1"))
        await psql.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")  # not stale: nothing committed since
        await psql.execute(
            "INSERT INTO ds.audit_log (actor, action, object) VALUES ('system:worker', 'w', '2')"
        )
        await psql.execute("COMMIT")
    async with pool.connection() as conn:
        rows = await _chain(conn)
        [logged] = await (await conn.execute("SELECT count(*) FROM ds.audit_log")).fetchone()  # type: ignore[misc]
        listed, _ = await repo.list_audit(conn, limit=2)
    async with pool.connection() as conn:
        done = await chain.verify(conn)
    assert len(rows) == logged == 10 and _linked(rows)  # the migration's two, then the eight above
    assert [(r["object"], r["obj_prev"] == ZERO) for r in rows[2:]] == [
        ("1", True), ("R-1", True), ("1", False), ("R-1", False), ("2", True), ("R-1", False), ("1", False),
        ("2", False),
    ]  # fmt: skip
    assert (first["chain"], second["chain"]) == (
        {"seq": 3, "hash": rows[2]["hash"]},
        {"seq": 4, "hash": rows[3]["hash"]},
    )
    assert repo.siem_line(first).endswith(f'"chain": {{"seq": 3, "hash": "{rows[2]["hash"]}"}}}}')
    assert [item["chain"] for item in listed] == [{"seq": r["seq"], "hash": r["hash"]} for r in rows[:-3:-1]]
    assert done == {"head": {"seq": 10, "hash": rows[-1]["hash"]}, "rows": 10, "upgrade_seq": 0, "siem": None}


async def test_concurrent_writers_are_chained_contiguously(pool: AsyncConnectionPool) -> None:
    async def writer(n: int) -> list[repo.Row]:
        written = []
        for i in range(250):
            async with repo.audited(pool) as (_, audit):
                written.append(await audit(TESTER, "key.view", f"R-{n}", {"cycle": 1, "i": i}))
        return written

    writers = await asyncio.gather(*(writer(n) for n in range(4)))
    async with pool.connection() as conn:
        rows = await _chain(conn)
    async with pool.connection() as conn:
        done = await chain.verify(conn)
    assert len(rows) == 2 + 4 * 250 and _linked(rows) and done["head"]["seq"] == 1002  # type: ignore[index]
    entries = {r["id"]: {"seq": r["seq"], "hash": r["hash"]} for r in rows}
    for written in writers:  # each row returned its entry; a writer's rows in its order
        assert [r["chain"] for r in written] == [entries[r["id"]] for r in written]
        assert [r["chain"]["seq"] for r in written] == sorted(r["chain"]["seq"] for r in written)
    assert len({seq for written in writers for seq in (r["chain"]["seq"] for r in written)}) == 1000


async def test_a_stale_repeatable_read_writer_fails_on_the_primary_key(
    pool: AsyncConnectionPool, pg_dsn: str
) -> None:
    async with (
        await psycopg.AsyncConnection.connect(pg_dsn) as stale,
        await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as other,
    ):
        await stale.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        await stale.execute("SELECT 1 FROM ds.runs")  # its snapshot
        await other.execute(
            "INSERT INTO ds.audit_log (actor, action, object) VALUES ('system:worker', 'x', 'y')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as refused:
            await stale.execute(
                "INSERT INTO ds.audit_log (actor, action, object) VALUES ('system:worker', 'x', 'z')"
            )
        await stale.rollback()
    assert refused.value.diag.constraint_name == "audit_chain_pkey"
    async with pool.connection() as conn:
        rows = await _chain(conn)
    assert rows[0]["object"].startswith("I-") and _linked(rows)  # installation.created
    assert [r["object"] for r in rows[1:]] == ["schema", "y"]  # nothing of the stale one


async def test_the_stored_text_is_the_same_in_every_time_zone(pool: AsyncConnectionPool, pg_dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        for n, zone in enumerate(TIME_ZONES):  # each row written under another TimeZone
            await conn.execute(f"SET TimeZone = '{zone}'")
            await conn.execute(ROW, (1000 + n,))  # type: ignore[arg-type]
            await conn.execute(BARE, (2000 + n,))  # type: ignore[arg-type]
        read = {}
        for zone in TIME_ZONES:
            await conn.execute(f"SET TimeZone = '{zone}'")
            cur = await conn.execute(
                "SELECT l.id, c.text, ds.audit_text(l) FROM ds.audit_chain c JOIN ds.audit_log l USING (id)"
                " WHERE l.id >= 1000 ORDER BY l.id"
            )
            read[zone] = await cur.fetchall()
    golden = [(1000 + n, GOLDEN.format(id=1000 + n)) for n in range(3)]
    golden += [(2000 + n, GOLDEN_BARE.format(id=2000 + n)) for n in range(3)]
    assert all(rows == [(i, text, text) for i, text in golden] for rows in read.values()), read
    for zone in TIME_ZONES:  # the check compares each text with its row's columns in Python, in any zone
        async with pool.connection() as conn:
            await conn.execute(f"SET TimeZone = '{zone}'")
            await conn.commit()
            assert (await chain.verify(conn))["rows"] == 8


async def test_009_chains_the_rows_already_written(empty_pool: AsyncConnectionPool) -> None:
    await migrate(empty_pool, upto=4)
    async with empty_pool.connection() as conn:  # a row before 005 named its actor only
        await conn.execute(
            "INSERT INTO ds.audit_log (actor, action, object) VALUES ('api', 'spec.create', '1')"
        )
    await migrate(empty_pool, upto=8)  # system.migrate [5, 6, 7, 8]
    async with repo.audited(empty_pool) as (_, audit):
        before = [await audit(TESTER, "spec.create", obj) for obj in ("2", "1", "2")]
    async with empty_pool.connection() as conn:
        ids = [i for (i,) in await (await conn.execute("SELECT id FROM ds.audit_log ORDER BY id")).fetchall()]
    assert [r["chain"] for r in before] == [None] * 3  # no chain yet
    assert await migrate(empty_pool, upto=9) == [9]
    async with empty_pool.connection() as conn:
        rows = await _chain(conn)
        upgrade = await repo.audit_upgrade_seq(conn)
        last = await repo.list_audit(conn, limit=1)
        with pytest.raises(psycopg.errors.RaiseException, match="audit_chain_upgrade is append-only"):
            await conn.execute("UPDATE ds.audit_chain_upgrade SET upgrade_seq = 0")
    assert _linked(rows) and [r["id"] for r in rows[:5]] == ids and upgrade == 5  # in id order
    assert [(r["object"], r["obj_prev"] == ZERO) for r in rows] == [
        ("1", True), ("schema", True), ("2", True), ("1", False), ("2", False), ("schema", False),
    ]  # fmt: skip
    assert (
        last[0][0]["action"] == "system.migrate" and last[0][0]["chain"]["seq"] == 6
    )  # chained as it commits


async def test_an_insert_waiting_on_009_is_chained_after_the_backfill(
    empty_pool: AsyncConnectionPool, pg_dsn: str
) -> None:
    await migrate(empty_pool, upto=8)
    upgrade = (MIGRATIONS / "009_packs.sql").read_bytes()
    async with (
        await psycopg.AsyncConnection.connect(pg_dsn) as migrating,
        await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as writer,
        await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as watcher,
    ):
        await migrating.execute(upgrade)
        insert = asyncio.create_task(
            writer.execute(
                "INSERT INTO ds.audit_log (actor, action, object) VALUES ('system:worker', 'x', 'y')"
            )
        )
        for _ in range(500):
            cur = await watcher.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (writer.info.backend_pid,)
            )
            if await cur.fetchone() == ("Lock",):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the insert never waited for 009")
        await migrating.commit()
        await insert
    async with empty_pool.connection() as conn:
        rows = await _chain(conn)
    assert [(r["seq"], r["object"]) for r in rows] == [(1, "schema"), (2, "y")] and _linked(rows)
