"""Migration 014 over a populated 013 store holding cycles ended before 006,
008 and 009, a purged one, a one-shot's and a captured-sample run's: the column and its indexes are added
without rewriting a table or adding a trigger, the excluded cycles are marked, and the worker's backfill
then measures every other cycle, ``unknown`` where its fields are missing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.runner import readiness
from deltasleuth.store import db, repo
from deltasleuth.store.db import migrate
from tests.unit.runner.conftest import spec_text
from tests.unit.store.four_eyes import counters

pytestmark = pytest.mark.db

SOAK, FAILED, BATCH, SCAN, ONE, CAPTURED, PURGED, LATEST = ("R-" + c * 26 for c in "SFBTOCPL")
INDEXES = [
    "cycles_readiness_breaks_high_idx", "cycles_readiness_breaks_standard_idx",
    "cycles_readiness_evidence_idx", "cycles_readiness_pair_idx", "cycles_readiness_unread_idx",
    "cycles_unmeasured_idx", "cycles_unmeasured_run_idx",
]  # fmt: skip
TABLES = {side: {"schema": "public", "name": "accounts"} for side in "ab"}
# A scan's state as 008 saved one: without the snapshots and times 009 added.
SCANNED = {"done": True, "after": None, "rows_a": 10, "rows_b": 10, "skipped_a": 0, "skipped_b": 0,
           "matched": 8, "candidates": 2, "checkpoint": 1}  # fmt: skip
RUN = """INSERT INTO ds.runs (run_id, spec_id, mode, status, cycle, counters, params, started_at, finished_at)
         VALUES (%s, 1, %s, %s, %s, %s, %s, now() - interval '9 days',
                 CASE WHEN %s IN ('completed', 'failed') THEN now() - interval '8 days' END)"""
ENDED = """INSERT INTO ds.cycles (run_id, cycle, ended_at, counters)
           VALUES (%s, %s, now() - interval '8 days', %s)"""


async def _all(conn: repo.Conn, sql: str, params: Any = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


async def _run(
    conn: repo.Conn,
    run_id: str,
    mode: str,
    status: str,
    cycle: int = 1,
    *,
    params: Any = None,
    ran: Any = None,
) -> None:
    values = (run_id, mode, status, cycle, Jsonb(ran or {}), Jsonb(params or {}), status)
    await conn.execute(RUN, values)  # type: ignore[arg-type]


async def test_014_over_a_populated_013_store(
    empty_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The spec of a soak names its interval, as creating one has always required.
    body, whole = parse_yaml_text(spec_text(execution="cycleInterval: 1h, concurrency: 2")), counters(1)
    assert await migrate(empty_pool, upto=5) == [1, 2, 3, 4, 5]
    async with empty_pool.connection() as conn:  # before 006: only a run's latest counters are known
        await conn.execute(
            """INSERT INTO ds.specs (spec_id, name, version, content_hash, body, created_by)
               VALUES (1, 'unit-runner', '1.0.0', %s, %s, 'local:tester@test')""",
            (content_hash(body), Jsonb(body)),
        )
        await _run(conn, SOAK, "soak", "running", 3, ran={"cycle": 2, "total": 2})  # an older counters shape
        await _run(conn, FAILED, "batch", "failed")
    assert await migrate(empty_pool, upto=7) == [6, 7]  # 006 gave each ended cycle its tombstone
    async with empty_pool.connection() as conn:  # before 008: no approval, no cut
        await _run(conn, BATCH, "batch", "completed", ran=whole)
        await conn.execute(ENDED, (BATCH, 1, Jsonb(whole)))
    assert await migrate(empty_pool, upto=8) == [8]
    async with empty_pool.connection() as conn:  # before 009: no census, and a scan without snapshots
        await _run(
            conn,
            SCAN,
            "batch",
            "completed",
            params={"keys_source": "table_scan", "tables": TABLES},
            ran=whole,
        )
        await conn.execute(
            "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 1, %s)", (SCAN, Jsonb(SCANNED))
        )
        await conn.execute(ENDED, (SCAN, 1, Jsonb(whole)))
    assert await migrate(empty_pool, upto=db.ACCURACY) == [9, 10, 11, 12, 13]
    async with empty_pool.connection() as conn:
        await _run(conn, ONE, "one_shot", "completed", ran=whole)
        await _run(conn, CAPTURED, "batch", "completed", params={"samples_dir": "/captured"}, ran=whole)
        await _run(conn, PURGED, "batch", "completed", params={"keys_source": "list"}, ran=whole)
        await _run(
            conn, LATEST, "batch", "completed", params={"keys_source": "list", "samples_dir": None}, ran=whole
        )
        for run_id in (ONE, CAPTURED, PURGED, LATEST):
            await conn.execute(ENDED, (run_id, 1, Jsonb(whole)))
        await conn.execute(
            """UPDATE ds.cycles SET purging_at = now(), purged_at = now(), purged_by = 'system:worker',
                   purge_reason = 'retention standard' WHERE run_id = %s""",
            (PURGED,),
        )
        tables = """SELECT relname, relfilenode FROM pg_class
                    WHERE relnamespace = 'ds'::regnamespace AND relkind = 'r'"""
        triggers = "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname"
        before, fired = dict(await _all(conn, tables)), await _all(conn, triggers)
        cycles = await _all(conn, "SELECT run_id, cycle FROM ds.cycles ORDER BY run_id, cycle")
    assert len(cycles) == 9  # the soak's cycles 1 and 2, and one of each other run
    assert await migrate(empty_pool, upto=db.READINESS) == [db.READINESS]
    async with empty_pool.connection() as conn:
        assert dict(await _all(conn, tables)) == before  # no table is rewritten, none added
        assert await _all(conn, triggers) == fired  # and no trigger
        indexes = await _all(
            conn,
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'ds' AND indexname LIKE 'cycles\\_%%\\_idx'",
        )
        assert sorted(name for (name,) in indexes if "readiness" in name or "unmeasured" in name) == INDEXES
        marked = await _all(
            conn, "SELECT run_id, readiness FROM ds.cycles WHERE readiness IS NOT NULL ORDER BY 1"
        )
        audited = await _all(
            conn, "SELECT detail FROM ds.audit_log WHERE action = 'system.migrate' ORDER BY id"
        )
        assert await _all(conn, "SELECT count(*) FROM ds.cycles") == [(9,)]
    for refused in ('{"pair": "x"}', '{"format": 1}'):  # no format; neither measures nor a marker
        async with empty_pool.connection() as conn:
            with pytest.raises(Exception, match="cycles_readiness_check"):
                await conn.execute("UPDATE ds.cycles SET readiness = %s WHERE run_id = %s", (refused, LATEST))
    assert marked == [
        (CAPTURED, {"format": 1, "excluded": "captured_samples"}),
        (ONE, {"format": 1, "excluded": "one_shot"}),
    ]
    assert audited[-1] == ({"versions": [db.READINESS]},)

    async def spec(conn: repo.Conn, spec_id: int) -> Any:
        return compile_spec((await repo.get_spec(conn, spec_id))["body"])  # type: ignore[index]

    # The worker's backfill measures each cycle that ended before 014, the purged one from its tombstone.
    settings = SimpleNamespace(readiness=None, auth_mode="none", packs=None)
    ctx: Any = SimpleNamespace(pool=empty_pool, spec=spec, settings=settings)
    monkeypatch.setattr(readiness, "BACKFILL_EVERY_S", 0)
    state = readiness.State()
    await readiness.watch(ctx, state)
    async with empty_pool.connection() as conn:
        assert await repo.unmeasured_cycles(conn, backfill=True) == []  # 6 in full and the purged one
        rows = await repo._rows(conn, "SELECT run_id, cycle, readiness FROM ds.cycles ORDER BY run_id, cycle")
        [measured] = await _all(conn, "SELECT detail FROM ds.audit_log WHERE action = 'readiness.measured'")
        changed = await _all(conn, "SELECT detail FROM ds.audit_log WHERE action = 'readiness.changed'")
    # Then, and only then, the pair's baseline: its newest scan observed side a whole, once.
    assert [(c["pair"], c["from"], c["to"], c["causes"]) for (c,) in changed] == [
        ("unit-runner", None, "not_ready", ["baseline"])
    ]
    found = {(row["run_id"], row["cycle"]): row["readiness"] for row in rows}
    assert len(measured[0]["cycles"]) == 7 and found[ONE, 1] == {"format": 1, "excluded": "one_shot"}
    assert {record["pair"] for at, record in found.items() if at[0] not in (ONE, CAPTURED)} == {"unit-runner"}

    def read(run_id: str, cycle: int = 1) -> tuple[Any, ...]:
        names = ("basis", "incomplete", "evidence", "late", "fails")
        return tuple(found[run_id, cycle][name] for name in names)

    nothing, unknown = {"standard": [], "high": []}, {"standard": ["unknown"], "high": ["unknown"]}
    listed = {"standard": ["population"], "high": ["population"]}
    assert read(SOAK, 1) == read(FAILED) == ("keys", ["no_counters"], False, False, nothing)
    assert read(SOAK, 2) == ("unknown", ["nothing_compared"], False, False, unknown)  # its fields are missing
    assert found[SOAK, 2]["total"] == 2 and found[SOAK, 2]["compared"] is None
    assert read(BATCH) == ("keys", [], True, False, listed)
    assert found[BATCH, 1]["needs_approval"] == ["clear"] and found[BATCH, 1]["keys_source"] is None
    assert read(SCAN) == ("census", [], True, False, nothing)
    assert found[SCAN, 1]["basis_text"] == "read to its end; snapshots not recorded (before 009)"
    late = {"standard": ["population", "unknown"], "high": ["population", "unknown"]}
    assert read(PURGED) == ("keys", [], True, True, late)
    assert found[PURGED, 1]["unexplained_critical"] is None and found[PURGED, 1]["results"] is None
    assert read(LATEST) == ("keys", [], True, False, listed)
    assert found[LATEST, 1]["unexplained_critical"] == 0 and found[LATEST, 1]["total"] == 2
