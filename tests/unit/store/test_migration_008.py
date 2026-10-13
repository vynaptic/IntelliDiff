"""Migration 008 (docs/mvp/APPROVALS.md §7) over a store 001-007 left with specs, a run and its ended
cycle, and on an empty one: it refuses and backfills nothing, and reports the stored bodies that do not
re-hash to their content hash."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.store import repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.conftest import TESTER

pytestmark = pytest.mark.db

RUN = "R-" + "S" * 26
GOOD = {"metadata": {"name": "customer-profile", "version": "1.0.0"}}
EDITED = {"metadata": {"name": "customer-profile", "version": "1.1.0"}, "knownDiffs": []}
HUGE = {
    "metadata": {"name": "customer-profile", "version": "1.2.0"},
    "tolerance": 1e100,
}  # read back as an int
# spec_id, the body stored, and the body its content hash was computed from.
SPECS = [(1, GOOD, GOOD), (2, EDITED, {"metadata": EDITED["metadata"]}), (3, HUGE, HUGE)]


@pytest.fixture
async def store_007(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool on a store migrated to 007 holding three specs, a finished run and its ended cycle."""
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute(
            "CREATE SCHEMA ds; CREATE TABLE ds.schema_version"
            " (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(MIGRATIONS.glob("00[1-7]_*.sql")):
            await conn.execute(path.read_bytes())
            await conn.execute("INSERT INTO ds.schema_version (version) VALUES (%s)", (int(path.name[:3]),))
        for spec_id, body, hashed in SPECS:
            await conn.execute(
                "INSERT INTO ds.specs (spec_id, name, version, content_hash, body, created_by)"
                " VALUES (%s, 'customer-profile', %s, %s, %s, %s)",
                (spec_id, body["metadata"]["version"], content_hash(hashed), Jsonb(body), TESTER.actor),
            )
        await conn.execute(
            "INSERT INTO ds.runs (run_id, spec_id, mode, status, counters, finished_at)"
            " VALUES (%s, 1, 'batch', 'completed', '{\"cycle\": 1}', now())",
            (RUN,),
        )
        await conn.execute(
            "INSERT INTO ds.cycles (run_id, cycle, ended_at, counters)"
            " VALUES (%s, 1, now(), '{\"cycle\": 1}')",
            (RUN,),
        )
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    yield pool
    await pool.close()


async def _all(conn: repo.Conn, sql: str) -> list[tuple]:
    return await (await conn.execute(sql)).fetchall()  # type: ignore[arg-type]


async def test_008_reports_bodies_that_do_not_rehash_and_backfills_nothing(
    store_007: AsyncConnectionPool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="deltasleuth.store.db")
    assert await migrate(store_007, upto=8) == [8]
    async with store_007.connection() as conn:
        audited = await _all(conn, "SELECT actor, action, object, detail FROM ds.audit_log ORDER BY id")
        runs = await _all(conn, "SELECT approval_id, approval_kind, keys_cut FROM ds.runs")
        cycles = await _all(conn, "SELECT approval_id, keys_cut, counters FROM ds.cycles")
    assert audited == [
        (
            "system:migration",
            "spec.rehash",
            "specs",
            {"spec_ids": [2, 3]},
        ),  # edited; not storable as uploaded
        ("system:migration", "system.migrate", "schema", {"versions": [8]}),
    ]
    assert [r.getMessage() for r in caplog.records] == [
        f"spec {n} (customer-profile 1.{n - 1}.0): its stored body does not re-hash to its content hash;"
        " upload a changed version (a new version)"
        for n in (2, 3)
    ]
    assert runs == [(None, "spec", None)] and cycles == [(None, None, {"cycle": 1})]
    assert await migrate(store_007, upto=8) == []
    async with store_007.connection() as conn:
        assert len(await _all(conn, "SELECT 1 FROM ds.audit_log")) == 2
    async with store_007.connection() as conn:  # the stored versions are immutable from now on
        with pytest.raises(psycopg.errors.RestrictViolation, match="spec 2 cannot be changed or deleted"):
            await conn.execute("UPDATE ds.specs SET body = %s WHERE spec_id = 2", (Jsonb(SPECS[1][2]),))


async def test_an_empty_store_takes_008_with_the_others_and_rehashes_nothing(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        versions = await _all(conn, "SELECT version FROM ds.schema_version ORDER BY version")
        audited = await _all(conn, "SELECT action, detail FROM ds.audit_log ORDER BY id")
    assert versions == [(v,) for v in range(1, 18)]
    assert audited[1:] == [("system.migrate", {"versions": list(range(1, 18))})]
    assert audited[0][0] == "installation.created"


async def test_008_adds_its_constraints_indexes_and_triggers(store_007: AsyncConnectionPool) -> None:
    await migrate(store_007, upto=8)
    async with store_007.connection() as conn:
        constraints = await _all(
            conn,
            """SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
               WHERE t.relnamespace = 'ds'::regnamespace AND c.contype IN ('p', 'u', 'f')
                 AND (t.relname = 'approvals' OR c.conname LIKE '%approval%'
                      OR c.conname = 'runs_run_id_spec_id_key')
               ORDER BY 1""",
        )
        indexes = await _all(
            conn, "SELECT indexname FROM pg_indexes WHERE indexname LIKE '%approv%' ORDER BY 1"
        )
        triggers = await _all(
            conn,
            """SELECT tgname FROM pg_trigger WHERE NOT tgisinternal
                 AND tgrelid IN ('ds.approvals'::regclass, 'ds.specs'::regclass) ORDER BY 1""",
        )
    assert [name for (name,) in constraints] == [
        "approvals_approval_id_spec_id_kind_key",
        "approvals_pkey",
        "approvals_run_id_cycle_fkey",
        "approvals_run_id_spec_id_fkey",
        "approvals_spec_id_fkey",
        "cycles_approval_id_fkey",
        "runs_approval_id_spec_id_approval_kind_fkey",
        "runs_run_id_spec_id_key",
    ]
    assert [name for (name,) in indexes] == [
        "approvals_active_spec",
        "approvals_approval_id_spec_id_kind_key",
        "approvals_open_signoff",
        "approvals_open_spec",
        "approvals_pkey",
        "approvals_signed_cycle",
        "runs_approval_idx",
    ]
    assert [name for (name,) in triggers] == [
        "approvals_lifecycle",
        "approvals_no_truncate",
        "specs_immutable",
        "specs_no_truncate",
    ]
