"""Migration 015 (docs/mvp/SCHEDULES.md §12) over a populated 014 store with runs: the schedules table,
each of its checks, the one live schedule per name, and on ``ds.runs`` the unique index that refuses a
second run of one schedule and due time and the check on what a run records of its schedule, added without
rewriting a table or adding a trigger."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.store import db, repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.unit.runner.conftest import spec_text

pytestmark = pytest.mark.db

SCHEDULE = "SC-" + "0" * 26
RUN = """INSERT INTO ds.runs (run_id, spec_id, mode, status, params, started_at)
         VALUES (%s, 1, 'batch', 'running', %s, now())"""
ROW = """INSERT INTO ds.schedules (schedule_id, name, spec_name, spec_id, run, cron, time_zone, auth_mode,
             created_by, set_by, next_due)
         VALUES (%(schedule_id)s, %(name)s, 'unit-runner', %(spec_id)s, %(run)s, %(cron)s, %(time_zone)s,
             %(auth_mode)s, 'local:tester@test', 'local:tester@test', now() + interval '1 hour')"""


def row(**fields: Any) -> dict[str, Any]:
    return {
        "schedule_id": repo.new_schedule_id(), "name": "nightly", "spec_id": 1,
        "run": Jsonb({"mode": "batch"}), "cron": "0 2 * * *", "time_zone": "Europe/London",
        "auth_mode": "tokens",
    } | fields  # fmt: skip


def fired(schedule_id: str, due: str) -> Jsonb:
    return Jsonb({"schedule": {"schedule_id": schedule_id, "due": due, "revision": 1, "time_zone": "UTC"}})


async def _all(conn: repo.Conn, sql: str, params: Any = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


@pytest.fixture
async def stored(empty_pool: AsyncConnectionPool) -> AsyncConnectionPool:
    """A 014 store holding a spec and three runs, one of them naming a schedule as no build ever wrote it,
    then migrated to 015."""
    assert await migrate(empty_pool, upto=db.READINESS) == list(range(1, db.READINESS + 1))
    body = parse_yaml_text(spec_text())
    tables = (
        "SELECT relname, relfilenode FROM pg_class WHERE relnamespace = 'ds'::regnamespace AND relkind = 'r'"
    )
    triggers = "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname"
    async with empty_pool.connection() as conn:
        await conn.execute(
            """INSERT INTO ds.specs (spec_id, name, version, content_hash, body, created_by)
               VALUES (1, 'unit-runner', '1.0.0', %s, %s, 'local:tester@test')""",
            (content_hash(body), Jsonb(body)),
        )
        for n, params in enumerate([{}, {"keys_source": "list"}, {"schedule": {"due": "tomorrow"}}]):
            await conn.execute(RUN, ("R-" + str(n) * 26, Jsonb(params)))  # type: ignore[arg-type]
        before, fired_ = dict(await _all(conn, tables)), await _all(conn, triggers)
    assert await migrate(empty_pool, upto=15) == [15]
    async with empty_pool.connection() as conn:
        after = dict(await _all(conn, tables))
        assert {name: after[name] for name in before} == before  # no table is rewritten: not ds.runs
        assert set(after) - set(before) == {"schedules"}
        assert await _all(conn, triggers) == fired_  # and no trigger
        assert await _all(conn, "SELECT count(*) FROM ds.runs") == [(3,)]
        [(valid,)] = await _all(
            conn, "SELECT convalidated FROM pg_constraint WHERE conname = 'runs_schedule'"
        )
        assert valid is False  # NOT VALID: no row was scanned under its lock
    return empty_pool


def test_the_index_is_built_before_the_check() -> None:
    sql = (MIGRATIONS / "015_schedules.sql").read_text()
    index, check = (
        sql.index("CREATE UNIQUE INDEX runs_schedule_due_idx"),
        sql.index("ADD CONSTRAINT runs_schedule"),
    )
    assert 0 < index < check and sql.rstrip().endswith("NOT VALID;")


async def test_each_check_of_a_schedule(stored: AsyncConnectionPool) -> None:
    async with stored.connection() as conn:
        await conn.execute(ROW, row())  # type: ignore[arg-type]
        await conn.execute(ROW, row(name="utc", time_zone="UTC", spec_id=None))  # type: ignore[arg-type]
        await conn.execute(ROW, row(name="deep", time_zone="America/Argentina/Buenos_Aires"))  # type: ignore[arg-type]
    bad = [
        {"schedule_id": "SC-short"}, {"schedule_id": "R-" + "0" * 26}, {"name": "Nightly"}, {"name": "a--b"},
        {"name": "a" * 64}, {"run": Jsonb(["batch"])}, {"run": Jsonb({"mode": "one_shot"})},
        {"run": Jsonb({"mode": "batch", "keys": ["K1"]})}, {"run": Jsonb({"mode": "batch", "spec_id": 1})},
        {"cron": "0 2 * * *; DROP"}, {"cron": "0 " * 50 + "0"}, {"time_zone": "localtime"},
        {"time_zone": "GMT"}, {"time_zone": "Europe/"}, {"time_zone": "E/" + "x" * 63},
        {"auth_mode": "sso"},
    ]  # fmt: skip
    for fields in bad:
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(ROW, row(name="other") | fields)  # type: ignore[arg-type]
    changes = [
        "start_within = 14", "start_within = 1441", "revision = 0", "paused_at = now()",
        "deleted_by = 'local:tester@test'", "paused_at = now(), paused_by = 'system:worker'",  # still due
        "deleted_at = now(), deleted_by = 'local:tester@test'", "next_due = anchor_at",
        "last = '[]', last_at = now()", "last = '{}'", "last_at = now()", "held = '1'",
    ]  # fmt: skip
    for change in changes:
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(f"UPDATE ds.schedules SET {change} WHERE name = 'nightly'")  # type: ignore[arg-type]  # noqa: S608
    async with stored.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(ROW, row(name="other", spec_id=99))  # type: ignore[arg-type]
    async with stored.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute("UPDATE ds.schedules SET last_run_id = %s", ("R-" + "9" * 26,))
    async with stored.connection() as conn:  # paused, a paused one deleted, and settled: each holds
        await conn.execute(
            """UPDATE ds.schedules SET paused_at = now(), paused_by = 'system:worker', next_due = NULL,
                   last = '{"outcome": "missed"}', last_at = now(), held = '{}', last_run_id = %s
               WHERE name = 'nightly'""",
            ("R-" + "0" * 26,),
        )
        await conn.execute(
            """UPDATE ds.schedules SET deleted_at = now(), deleted_by = 'local:tester@test'
               WHERE name = 'nightly'"""
        )


async def test_one_live_schedule_of_a_name(stored: AsyncConnectionPool) -> None:
    async with stored.connection() as conn:
        await conn.execute(ROW, row(schedule_id=SCHEDULE))  # type: ignore[arg-type]
    async with stored.connection() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation, match="schedules_name_idx"):
            await conn.execute(ROW, row())  # type: ignore[arg-type]
    async with stored.connection() as conn:  # a tombstone frees its name
        await conn.execute(
            "UPDATE ds.schedules SET deleted_at = now(), deleted_by = 'local:tester@test', next_due = NULL"
        )
        await conn.execute(ROW, row())  # type: ignore[arg-type]
        assert await _all(conn, "SELECT count(*) FROM ds.schedules WHERE name = 'nightly'") == [(2,)]


async def test_one_run_of_a_schedule_and_due_time_whoever_writes_it(stored: AsyncConnectionPool) -> None:
    due = "2026-10-07T01:00Z"
    async with stored.connection() as conn:
        await conn.execute(RUN, ("R-" + "A" * 26, fired(SCHEDULE, due)))  # type: ignore[arg-type]
        await conn.execute(RUN, ("R-" + "B" * 26, fired(SCHEDULE, "2026-10-08T01:00Z")))  # type: ignore[arg-type]
        await conn.execute(RUN, ("R-" + "C" * 26, fired("SC-" + "1" * 26, due)))  # type: ignore[arg-type]
    async with stored.connection() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation, match="runs_schedule_due_idx"):
            await conn.execute(RUN, ("R-" + "D" * 26, fired(SCHEDULE, due)))  # type: ignore[arg-type]
    # The canonical text only: another spelling of the instant would be another key of the index.
    for other in ("2026-10-07T01:00:00Z", "2026-10-07T01:00+00:00", "2026-10-07 01:00Z", "tomorrow"):
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation, match="runs_schedule"):
                await conn.execute(RUN, ("R-" + "D" * 26, fired(SCHEDULE, other)))  # type: ignore[arg-type]
    for schedule_id in ("SC-1", "R-" + "0" * 26):
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation, match="runs_schedule"):
                await conn.execute(RUN, ("R-" + "D" * 26, fired(schedule_id, due)))  # type: ignore[arg-type]
    async with stored.connection() as conn:  # a row from before 015 is checked once it is written again
        with pytest.raises(psycopg.errors.CheckViolation, match="runs_schedule"):
            await conn.execute("UPDATE ds.runs SET updated_at = now() WHERE run_id = %s", ("R-" + "2" * 26,))
    async with stored.connection() as conn:
        plan = await _all(
            conn,
            """EXPLAIN SELECT run_id FROM ds.runs
               WHERE params ? 'schedule' AND params->'schedule'->>'schedule_id' = 'SC-x'""",
        )
        await conn.execute("SET enable_seqscan = off")
        forced = await _all(
            conn,
            """EXPLAIN SELECT run_id FROM ds.runs
               WHERE params ? 'schedule' AND params->'schedule'->>'schedule_id' = 'SC-x'""",
        )
    assert plan and "runs_schedule_due_idx" in "\n".join(
        line for (line,) in forced
    )  # list_runs(schedule_id=)
