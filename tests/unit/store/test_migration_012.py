"""Migration 012 (docs/mvp/ALERTS.md §13): the feed's cursor, the deliveries and the mutes, touching no
existing table; their constraints; a mute only ever ended, once, as of now(); and the repo functions the
feed needs over the chain."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.auth.principal import WORKER
from deltasleuth.store import db, repo
from deltasleuth.store.db import migrate

pytestmark = pytest.mark.db

ENDED_ONCE = r"ds.notify_mutes: mute M-\w+ is only ever ended, once, as of now\(\)"
MUTE = "M-" + "0" * 26


def delivery(**fields: Any) -> dict[str, Any]:
    return {
        "delivery_id": repo.new_delivery_id(), "event_id": "seq:1:run.failed", "seq": 1,
        "event": "run.failed",
        "severity": "alert", "channel": "ops-hook", "rules": ["run-health"], "dedup_key": "run.end|R-x",
        "run_id": None, "body": "{}", "body_sha256": "0" * 64, "render": "webhook:1:1", "why": None,
        "mute_id": None,
    } | fields  # fmt: skip


async def _all(conn: repo.Conn, sql: str, params: tuple = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


async def test_012_touches_no_existing_table(empty_pool: AsyncConnectionPool) -> None:
    assert await migrate(empty_pool, upto=db.SAMPLING) == list(range(1, db.NOTIFY))
    tables = (
        "SELECT relname, relfilenode FROM pg_class WHERE relnamespace = 'ds'::regnamespace AND relkind = 'r'"
    )
    triggers = "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname"
    async with empty_pool.connection() as conn:
        before, fired = dict(await _all(conn, tables)), await _all(conn, triggers)
    assert await migrate(empty_pool, upto=db.NOTIFY) == [db.NOTIFY]
    async with empty_pool.connection() as conn:
        after = dict(await _all(conn, tables))
        assert {name: after[name] for name in before} == before  # no rewrite
        assert set(after) - set(before) == {"notify_cursor", "notify_deliveries", "notify_mutes"}
        assert await _all(conn, triggers) == sorted([*fired, ("notify_mutes_end_only",)])
        refs = """SELECT count(*) FROM pg_constraint
                  WHERE contype = 'f' AND conrelid::regclass::text LIKE 'ds.notify%%'"""
        assert await _all(conn, refs) == [(0,)]


async def test_deliveries_hold_ids_counts_and_codes(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        assert await repo.insert_delivery(conn, delivery())
        assert not await repo.insert_delivery(conn, delivery())  # one per event and channel
        assert await repo.insert_delivery(conn, delivery(channel="owners-mail"))
        assert await repo.insert_delivery(
            conn, delivery(event_id="daily:2026-10-03", seq=None, why="duplicate")
        )
        [(state, due)] = await _all(
            conn, "SELECT state, due_at IS NULL FROM ds.notify_deliveries WHERE why IS NOT NULL"
        )
        assert (state, due) == ("suppressed", True)
    bad = [
        {"delivery_id": "N-short"}, {"event_id": "seq:1:Run.Failed"}, {"event_id": "test:N-x"},
        {"channel": "Ops_Hook"}, {"severity": "critical"}, {"body_sha256": "x"}, {"run_id": "R-1"},
        {"body": "x" * 65537}, {"render": "slack:1"}, {"why": "muted"},  # muted without its mute
    ]  # fmt: skip
    for fields in bad:
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await repo.insert_delivery(conn, delivery(event_id="seq:2:run.failed") | fields)
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):  # a pending delivery is due
            await conn.execute("UPDATE ds.notify_deliveries SET due_at = NULL WHERE state = 'pending'")
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):  # a sent one is done
            await conn.execute("UPDATE ds.notify_deliveries SET state = 'sent', due_at = NULL")
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("UPDATE ds.notify_deliveries SET last_error = 'HTTP 500 at /secret'")


async def _mute(conn: repo.Conn, scope: str, target: str, hours: float) -> None:
    await conn.execute(
        """INSERT INTO ds.notify_mutes (mute_id, scope, target, until, reason, created_by)
           VALUES (%s, %s, %s, now() + make_interval(secs => %s), 'maintenance',
                   'user:U-0123456789abcdef')""",
        (MUTE, scope, target, hours * 3600),
    )


async def test_a_mute_is_only_ended_once_as_of_now(pool: AsyncConnectionPool) -> None:
    for scope, target, hours in (
        ("run", "ops-hook", 1),
        ("channel", "R-" + "0" * 26, 1),
        ("channel", "ops-hook", 169),
    ):
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await _mute(conn, scope, target, hours)
    async with pool.connection() as conn:
        await _mute(conn, "channel", "ops-hook", 1)
        assert await repo.active_mute(conn, "ops-hook", None) == MUTE
        assert await repo.active_mute(conn, "owners-mail", "R-" + "1" * 26) is None
    for change in ("until = until + interval '1 hour'", "ended_by = 'x', ended_at = now() - interval '1 s'"):
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.RestrictViolation, match=ENDED_ONCE):
                await conn.execute(f"UPDATE ds.notify_mutes SET {change}")  # type: ignore[arg-type]  # noqa: S608
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.notify_mutes SET ended_by = 'user:U-0123456789abcdef', ended_at = now()"
        )
    async with pool.connection() as conn:
        assert await repo.active_mute(conn, "ops-hook", None) is None
        with pytest.raises(psycopg.errors.RestrictViolation, match=ENDED_ONCE):
            await conn.execute("UPDATE ds.notify_mutes SET ended_by = 'x', ended_at = now()")
    async with pool.connection() as conn:  # the prune deletes
        assert (await conn.execute("DELETE FROM ds.notify_mutes")).rowcount == 1


async def test_the_cursor_starts_at_the_head(pool: AsyncConnectionPool) -> None:
    async with repo.audited(pool) as (conn, audit):
        for n in range(5):
            await audit(WORKER, "notify.test", "notify", {"n": n})
    async with pool.connection() as conn:
        head = await repo.chain_head(conn)
        assert (await repo.notify_cursor(conn))["seq"] == head
        await repo.move_cursor(conn, head - 2)
    async with pool.connection() as conn:
        assert (await repo.notify_cursor(conn))["seq"] == head - 2
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute("INSERT INTO ds.notify_cursor (seq) VALUES (0)")
    async with pool.connection() as conn:
        rows = await repo.audit_chain(conn, after=head - 4, upto=head - 1, actions=["notify.test"])
        assert [r["seq"] for r in rows] == [head - 3, head - 2, head - 1]
        assert await repo.audit_chain(conn, after=0, actions=["run.cycle"]) == []
        last = await repo.last_audit(conn, "notify.test", obj="notify")
        assert last is not None and last["detail"]["n"] == 4
        assert await repo.last_audit(conn, "notify.test", obj="other") is None


async def test_chain_before_bisects_on_seq(pool: AsyncConnectionPool) -> None:
    """Rows an hour apart: the last row before a moment, found by seq; a run's cycle as of a row."""
    run_id = "R-" + "0" * 26
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO ds.audit_log (at, actor, action, object, detail)
               SELECT timestamptz '2026-01-01 00:00Z' + make_interval(hours => n), 'system:worker',
                      CASE WHEN n %% 10 = 9 THEN 'run.cycle' ELSE 'running' END, %s,
                      jsonb_build_object('cycle', n / 10 + 2)
               FROM generate_series(0, 39) AS n""",
            (run_id,),
        )
        rows = (await repo.audit_chain(conn, after=0))[-40:]  # after the migration's
        low, high = rows[0]["seq"], rows[-1]["seq"]
        assert (
            await repo.chain_before(conn, low, high, rows[25]["audit_at"] + timedelta(minutes=30)) == low + 25
        )
        assert await repo.chain_before(conn, low, high, rows[-1]["audit_at"] + timedelta(hours=1)) == high
        assert await repo.chain_before(conn, low, high, rows[0]["audit_at"] + timedelta(minutes=1)) == low
        assert [await repo.cycle_at(conn, run_id, low + n) for n in (0, 9, 10, 19, 20, 39)] == [
            1,
            1,
            2,
            2,
            3,
            4,
        ]
