"""Migrations 006 and 007 over a store 001-005 left with data, and on an empty
one: 006 keeps the legacy plaintext for 'ds keys migrate', and 007 refuses while any of it remains."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.store import db
from deltasleuth.store.db import MIGRATIONS, MigrationError, migrate

pytestmark = pytest.mark.db

T0 = datetime(2026, 9, 1, tzinfo=UTC)
SOAK, FAILED, QUEUED, IDLE = ("R-" + c * 26 for c in "SFQI")
SPINE = ("run_keys", "observations", "results", "findings", "llm_calls")
# The primary, unique and foreign keys of the spine, as PostgreSQL names them by default.
CONSTRAINTS = {
    ("findings", "findings_finding_id_key", "u"),
    ("findings", "findings_pkey", "p"),
    ("findings", "findings_run_id_cycle_key_masked_fkey", "f"),
    ("llm_calls", "llm_calls_pkey", "p"),
    ("llm_calls", "llm_calls_session_id_fkey", "f"),
    ("observations", "observations_pkey", "p"),
    ("observations", "observations_run_id_cycle_key_masked_fkey", "f"),
    ("results", "results_pkey", "p"),
    ("results", "results_run_id_cycle_key_masked_fkey", "f"),
    ("results", "results_run_id_obs_a_id_fkey", "f"),
    ("results", "results_run_id_obs_b_id_fkey", "f"),
    ("run_keys", "run_keys_pkey", "p"),
    ("run_keys", "run_keys_run_id_cycle_seq_key", "u"),
    ("run_keys", "run_keys_run_id_fkey", "f"),
}


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


async def _legacy(conn: psycopg.AsyncConnection) -> None:
    """What 005 left: a soak at cycle 3 with results in cycles 1 and 2, its scans and a chat across its
    cycles; a failed run of an audit spec; a queued run; a soak at cycle 2 that saved no result."""
    await conn.execute(
        "INSERT INTO ds.specs (spec_id, name, version, content_hash, body) VALUES"
        " (1, 'd', '1', 'h1', '{}'), (2, 'd', '2', 'h2', '{\"retention\": \"audit\"}')"
    )
    runs = [
        (SOAK, 1, "soak", "running", 3, {"cycle": 2, "total": 2}, at(0), None),
        (FAILED, 2, "batch", "failed", 1, {}, at(1), at(2)),
        (QUEUED, 1, "batch", "queued", 1, {}, None, None),
        (IDLE, 1, "soak", "running", 2, {}, at(3), None),
    ]
    for run_id, spec_id, mode, status, cycle, counters, started, finished in runs:
        await conn.execute(
            "INSERT INTO ds.runs (run_id, spec_id, mode, status, cycle, counters, created_at, started_at,"
            " finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, spec_id, mode, status, cycle, Jsonb(counters), T0, started, finished),
        )
    await conn.execute(
        "INSERT INTO ds.run_keys (run_id, cycle, key, key_masked, seq, state)"
        " SELECT %s, c, k, 'm-' || k, n, 'done' FROM generate_series(1, 3) c,"
        " unnest('{k1,k2}'::text[]) WITH ORDINALITY AS u (k, n)",
        (SOAK,),
    )
    await conn.execute(
        "INSERT INTO ds.run_keys (run_id, cycle, key, key_masked, seq) VALUES (%s, 1, 'k9', 'm-k9', 0)",
        (FAILED,),
    )
    for cycle, obs_id in ((1, "O-" + "1" * 16), (2, "O-" + "2" * 16)):
        await conn.execute(
            "INSERT INTO ds.observations (run_id, obs_id, cycle, key, side, pass_no, attempts, pages,"
            " pages_eof, pagination_unstable, headers, observed_at)"
            " VALUES (%s, %s, %s, 'k1', 'a', 1, 1, 1, true, false, '{\"etag\": \"e\"}', %s)",
            (SOAK, obs_id, cycle, at(cycle)),
        )
    for cycle, key, updated in ((1, "k1", at(5)), (1, "k2", at(6)), (2, "k1", at(30))):
        await conn.execute(
            "INSERT INTO ds.results (run_id, cycle, key, outcome, side_a_state, side_b_state, confirmation,"
            " first_outcome, obs_a_id, updated_at)"
            " VALUES (%s, %s, %s, 'MISMATCH', 'COMPLETE', 'COMPLETE', 'provisional', 'MISMATCH', %s, %s)",
            (SOAK, cycle, key, "O-" + str(cycle) * 16 if key == "k1" else None, updated),
        )
    await conn.execute(
        "INSERT INTO ds.findings (run_id, cycle, key, seq, finding_id, change_type, direction, path_pattern,"
        " json_pointer, suppressed) VALUES (%s, 2, 'k1', 0, %s, 'value_changed', 'both', '$.x', '/x', false)",
        (SOAK, "F-" + "0" * 16),
    )
    await conn.execute(
        "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 2, %s), (%s, 3, %s)",
        (SOAK, Jsonb({"done": True, "after": ["k2"]}), SOAK, Jsonb({"done": False, "after": ["k1"]})),
    )
    await conn.execute("INSERT INTO ds.chat_sessions (session_id, run_id) VALUES ('S-1', %s)", (SOAK,))
    for seq, when in ((1, at(4)), (2, at(10)), (3, at(40))):  # cycle 1 ended at 6h, cycle 2 at 30h
        await conn.execute(
            "INSERT INTO ds.chat_messages (session_id, seq, role, content, created_at)"
            " VALUES ('S-1', %s, 'user', 'why?', %s)",
            (seq, when),
        )
    await conn.execute(
        "INSERT INTO ds.llm_calls (call_id, session_id, profile_id) VALUES ('L-1', 'S-1', 'p')"
    )


@pytest.fixture
async def legacy_pool(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool on a store migrated to 005 and holding legacy rows; 006 not yet applied."""
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute("CREATE SCHEMA ds; CREATE TABLE ds.schema_version (version integer PRIMARY KEY)")
        for path in sorted(MIGRATIONS.glob("00[1-5]_*.sql")):
            await conn.execute(path.read_bytes())
            await conn.execute("INSERT INTO ds.schema_version VALUES (%s)", (int(path.name[:3]),))
        await _legacy(conn)
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    yield pool
    await pool.close()


async def _constraints(conn: psycopg.AsyncConnection) -> set[tuple[str, str, str]]:
    cur = await conn.execute(
        """SELECT t.relname, c.conname, c.contype FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
           WHERE t.relnamespace = 'ds'::regnamespace AND t.relname = ANY(%s)
             AND c.contype IN ('p', 'u', 'f')""",
        (list(SPINE),),
    )
    return set(await cur.fetchall())


async def _all(conn: psycopg.AsyncConnection, sql: str) -> list[tuple]:
    return await (await conn.execute(sql)).fetchall()  # type: ignore[arg-type]


async def test_006_moves_the_spine_to_masked_keys_and_keeps_the_legacy_plaintext(
    legacy_pool: AsyncConnectionPool,
) -> None:
    assert await migrate(legacy_pool, upto=6) == [6]
    async with legacy_pool.connection() as conn:
        constraints = await _constraints(conn)
        observations = await _all(
            conn, "SELECT obs_id, key_masked, headers, headers_enc FROM ds.observations"
        )
        results = await _all(conn, "SELECT cycle, key_masked FROM ds.results ORDER BY cycle, key_masked")
        findings = await _all(conn, "SELECT cycle, key_masked FROM ds.findings")
        keys = await _all(conn, "SELECT DISTINCT key, key_masked, key_enc FROM ds.run_keys ORDER BY key")
        cur = await conn.execute(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'ds' AND column_name = 'key'"""
        )
        with_key = sorted(await cur.fetchall())
        scans = await _all(conn, "SELECT cycle, state FROM ds.scans ORDER BY cycle")
        retention = await _all(conn, "SELECT run_id, retention FROM ds.runs ORDER BY run_id")
    assert constraints == CONSTRAINTS
    assert sorted(observations) == [
        ("O-" + "1" * 16, "m-k1", {"etag": "e"}, None),
        ("O-" + "2" * 16, "m-k1", {"etag": "e"}, None),
    ]
    assert results == [(1, "m-k1"), (1, "m-k2"), (2, "m-k1")] and findings == [(2, "m-k1")]
    assert keys == [
        ("k1", "m-k1", None),
        ("k2", "m-k2", None),
        ("k9", "m-k9", None),
    ]  # sealed by 'ds keys migrate'
    assert with_key == [("chat_sessions", "key"), ("run_keys", "key")]  # a session's key is masked
    assert scans == [
        (2, {"done": True, "after": None, "checkpoint": 0}),  # a finished scan resumes nowhere
        (3, {"done": False, "after": ["k1"], "checkpoint": 0}),
    ]
    assert retention == sorted(
        [(SOAK, "standard"), (FAILED, "audit"), (QUEUED, "standard"), (IDLE, "standard")]
    )


async def test_006_backfills_the_ended_cycles_and_each_message_s_cycle(
    legacy_pool: AsyncConnectionPool,
) -> None:
    await migrate(legacy_pool, upto=6)
    async with legacy_pool.connection() as conn:
        cycles = await _all(
            conn, "SELECT run_id, cycle, ended_at, counters, purged_at FROM ds.cycles ORDER BY run_id, cycle"
        )
        messages = await _all(conn, "SELECT seq, cycle, content FROM ds.chat_messages ORDER BY seq")
    assert cycles == sorted([
        (SOAK, 1, at(6), {}, None),  # its latest result; the run keeps only its latest counters
        (SOAK, 2, at(30), {"cycle": 2, "total": 2}, None),
        (FAILED, 1, at(2), {}, None),  # a finished run's last cycle: when it finished
        (IDLE, 1, at(3), {}, None),  # no result: when the run started
    ])  # fmt: skip
    assert messages == [(1, 1, "why?"), (2, 2, "why?"), (3, 3, "why?")]  # the last one: the current cycle


async def test_006_refuses_a_mismatched_masked_key_and_keeps_llm_calls_of_deleted_sessions(
    legacy_pool: AsyncConnectionPool,
) -> None:
    await migrate(legacy_pool, upto=6)
    async with legacy_pool.connection() as conn:
        await conn.execute("DELETE FROM ds.chat_sessions")
        assert await _all(conn, "SELECT call_id, session_id FROM ds.llm_calls") == [("L-1", None)]
    async with legacy_pool.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match="results_run_id_cycle_key_masked_fkey"):
            await conn.execute(
                "INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,"
                " confirmation, first_outcome)"
                " VALUES (%s, 3, 'k1', 'MATCH', 'COMPLETE', 'COMPLETE', 'provisional', 'MATCH')",
                (SOAK,),
            )


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("UPDATE ds.runs SET retention = 'forever'", ()),
        ("UPDATE ds.runs SET hold_reason = 'audit', hold_by = 'local:a@b'", ()),  # hold_at missing
        ("UPDATE ds.runs SET hold_reason = %s, hold_by = 'x', hold_at = now()", ("a\x1bb",)),
        ("UPDATE ds.runs SET hold_reason = %s, hold_by = 'x', hold_at = now()", ("x" * 201,)),
        ("UPDATE ds.run_keys SET key_enc = %s", (b"\x01" * 29,)),
        ("INSERT INTO ds.blob_gc (content_hash) VALUES ('sha256:' || repeat('0', 64))", ()),
        ("INSERT INTO ds.cycles (run_id, cycle, ended_at, purging_at) VALUES (%s, 5, now(), now())", (SOAK,)),
        ("INSERT INTO ds.cycles (run_id, cycle, ended_at, purged_at, purged_by)"
         " VALUES (%s, 5, now(), now(), 'x')", (SOAK,)),
        ("INSERT INTO ds.cycles (run_id, cycle, ended_at, purging_at, purge_reason)"
         " VALUES (%s, 5, now(), now(), %s)", (SOAK, "x" * 221)),
    ],
)  # fmt: skip
async def test_006_checks_refuse_bad_rows(legacy_pool: AsyncConnectionPool, sql: str, params: tuple) -> None:
    await migrate(legacy_pool, upto=6)
    async with legacy_pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(sql, params)  # type: ignore[arg-type]


# -- 007 -----------------------------------------------------------------------------------------------

REFUSED = "legacy plaintext remains: stop the app and run 'ds keys migrate'"
SEALED = b"\x01" + b"\x00" * 29  # a sealed value's shape; 007 checks only that one is there
# Once 007 applied, of the plaintext and the sealed columns only the sealed are left, NOT NULL.
SEALED_COLUMNS = [("chat_messages", "sealed", "NO"), ("observations", "headers_enc", "NO"),
                  ("run_keys", "key_enc", "NO")]  # fmt: skip
# What 'ds keys migrate' does to each kind of legacy value, as 007 sees it.
SEAL = {
    "keys": "UPDATE ds.run_keys SET key_enc = %(sealed)s, key = NULL",
    "headers": "UPDATE ds.observations SET headers_enc = %(sealed)s, headers = NULL",
    "blobs": "UPDATE ds.observations SET content_hash = 'hk1:' || substr(content_hash, 8)",
    "chat": "UPDATE ds.chat_messages SET sealed = %(sealed)s, content = NULL, tool_calls = NULL",
    "scans": """UPDATE ds.scans SET state = state || '{"after": {"enc": "AQ=="}}'
                WHERE jsonb_typeof(state->'after') = 'array'""",
}


async def _columns(conn: psycopg.AsyncConnection) -> tuple[list[tuple], str]:
    """The plaintext and sealed columns there are, each with its nullability; the content_hash check."""
    cur = await conn.execute(
        """SELECT table_name, column_name, is_nullable FROM information_schema.columns
           WHERE table_schema = 'ds' AND (table_name, column_name) IN (('run_keys', 'key'),
               ('run_keys', 'key_enc'), ('observations', 'headers'), ('observations', 'headers_enc'),
               ('chat_messages', 'content'), ('chat_messages', 'tool_calls'), ('chat_messages', 'sealed'))
           ORDER BY table_name, column_name"""
    )
    columns = await cur.fetchall()
    cur = await conn.execute(
        """SELECT pg_get_constraintdef(oid) FROM pg_constraint
           WHERE conname = 'observations_content_hash_check'"""
    )
    [(check,)] = await cur.fetchall()
    return columns, check


async def test_007_refuses_legacy_plaintext_and_006_rolls_back_with_it(
    legacy_pool: AsyncConnectionPool,
) -> None:
    with pytest.raises(MigrationError) as refused:
        await migrate(legacy_pool)
    async with legacy_pool.connection() as conn:
        versions = await _all(conn, "SELECT version FROM ds.schema_version ORDER BY version")
        audited = await _all(conn, "SELECT action FROM ds.audit_log")
    assert (str(refused.value), versions, audited) == (REFUSED, [(1,), (2,), (3,), (4,), (5,)], [])


async def test_a_store_before_006_holding_plaintext_is_refused_before_006_runs(
    legacy_pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in MIGRATIONS.glob("*.sql"):  # the same migrations, but an 006 that would say it ran
        (tmp_path / path.name).write_bytes(path.read_bytes())
    (tmp_path / "006_retention.sql").write_text("DO $$ BEGIN RAISE EXCEPTION '006 ran'; END $$;")
    monkeypatch.setattr(db, "MIGRATIONS", tmp_path)
    with pytest.raises(MigrationError) as refused:
        await migrate(legacy_pool)  # a start: nothing of 006 is written only to roll back
    assert str(refused.value) == REFUSED
    with pytest.raises(MigrationError, match=r"^006 ran$"):
        await migrate(legacy_pool, upto=6)  # 'ds keys migrate' applies it


@pytest.mark.parametrize("left", [*SEAL, None])
async def test_007_refuses_while_any_legacy_value_remains(
    legacy_pool: AsyncConnectionPool, left: str | None
) -> None:
    await migrate(legacy_pool, upto=6)
    async with legacy_pool.connection() as conn:
        # One observation named a legacy blob; the fixture's hold no body.
        await conn.execute("UPDATE ds.observations SET content_hash = 'sha256:' || repeat('a', 64)")
        for kind, sql in SEAL.items():
            if kind != left:
                await conn.execute(sql, {"sealed": SEALED})  # type: ignore[arg-type]
    if left is not None:
        with pytest.raises(MigrationError, match="legacy plaintext remains"):
            await migrate(legacy_pool)
        return
    assert await migrate(legacy_pool) == [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
    async with legacy_pool.connection() as conn:
        columns, check = await _columns(conn)
    assert columns == SEALED_COLUMNS and "hk1" in check and "sha256" not in check
    async with legacy_pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="observations_content_hash_check"):
            await conn.execute("UPDATE ds.observations SET content_hash = 'sha256:' || repeat('b', 64)")


async def test_an_empty_store_takes_006_and_007_at_once(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        versions = await _all(conn, "SELECT version FROM ds.schema_version ORDER BY version")
        [(detail,)] = await _all(conn, "SELECT detail FROM ds.audit_log WHERE action = 'system.migrate'")
        columns, check = await _columns(conn)
    assert versions == [(v,) for v in range(1, 18)] and detail == {"versions": list(range(1, 18))}
    assert columns == SEALED_COLUMNS and "hk1" in check and "sha256" not in check


async def test_an_empty_store_gets_the_same_spine(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        assert await _constraints(conn) == CONSTRAINTS
        cur = await conn.execute(
            """SELECT indexname FROM pg_indexes WHERE schemaname = 'ds' AND indexname = ANY(%s)""",
            ([
                "observations_key_idx", "observations_blob_idx", "results_obs_a_idx", "results_obs_b_idx",
                "cycles_due_idx", "data_keys_kek_idx",
            ],),
        )  # fmt: skip
        assert len(await cur.fetchall()) == 6
