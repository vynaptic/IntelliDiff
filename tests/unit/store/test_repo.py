"""The store's behaviour through the repo API, on a real PostgreSQL."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.api.deps import shown
from deltasleuth.auth import sessions, tokens
from deltasleuth.core.types import (
    ChangeType,
    CompareConfig,
    CompiledSpec,
    Confirmation,
    Direction,
    Finding,
    KeyConfig,
    KeyResult,
    Observation,
    Outcome,
    RootCause,
    Severity,
    Side,
    SideState,
    SideStateKind,
)
from deltasleuth.runner.scan import new_scan
from deltasleuth.security.tokenise import Tokeniser, mask_finding
from deltasleuth.store import repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.conftest import TEST_DATA_KEY, TEST_SEALER, TESTER, evidence_store

pytestmark = pytest.mark.db

SPEC = CompiledSpec(
    name="demo",
    version="1.0.0",
    content_hash="sha256:" + "0" * 64,
    normalizer_version="1.0.0",
    key=KeyConfig(name="id"),
    sides={},
    compare=CompareConfig(),
    raw={"metadata": {"name": "demo", "version": "1.0.0"}, "tolerance": Decimal("0.01")},
)
TOKENISER = Tokeniser(b"k" * 32)
COMPLETE = SideState(SideStateKind.COMPLETE)
LATER = timedelta(hours=1)
MakeRun = Callable[..., Awaitable[str]]


@pytest.fixture
def make_run(pool: AsyncConnectionPool) -> MakeRun:
    """A run of the demo spec over ``keys`` (key ``k`` shows as ``m-k``), running by default; with
    ``scan``, a table scan's run in that state."""

    async def make(
        keys: Sequence[str], mode: str = "batch", status: str = "running", scan: dict[str, Any] | None = None
    ) -> str:
        async with pool.connection() as conn:
            spec_id = await repo.save_spec(conn, SPEC, created_by=TESTER.actor)
            run_id = repo.new_run_id()
            await repo.create_run(
                conn,
                spec_id,
                mode,
                TEST_SEALER.seal_keys({k: f"m-{k}" for k in keys}),
                {"samples_dir": None},
                scan,
                run_id=run_id,
                created_by=TESTER.actor,
                retention="standard",
                data_key=TEST_DATA_KEY,
            )
            if status != "queued":
                await repo.set_run_status(conn, run_id, status)
        return run_id

    return make


def result(
    key: str, outcome: Outcome = Outcome.MATCH, severities: Sequence[Severity] = (), **kw: Any
) -> KeyResult:
    """A key result with one value finding at ``$.f<i>`` per severity."""
    findings = [
        Finding(
            key=key,
            json_pointer=f"/f{i}",
            path_pattern=f"$.f{i}",
            change_type=ChangeType.VALUE_CHANGED,
            a_value="x",
            b_value="y",
            pointer_parts=(("field", f"f{i}"),),
            taxonomy_code="VALUE_MISMATCH",
            severity=severity,
        )
        for i, severity in enumerate(severities)
    ]
    return KeyResult(key, outcome, COMPLETE, COMPLETE, findings=findings, **kw)


async def save(conn: repo.Conn, run_id: str, r: KeyResult, cycle: int = 1) -> int:
    return await repo.save_key_result(
        conn, run_id, cycle, f"m-{r.key}", r, [mask_finding(f, SPEC, TOKENISER) for f in r.findings]
    )


def opened(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [TEST_SEALER.open_row(row) for row in rows]


def keys(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(row["key"] for row in opened(rows))


def headers_enc(cycle: int, obs: Observation) -> bytes:
    return TEST_SEALER.seal("headers", cycle, str(obs.obs_id), json.dumps(obs.headers).encode())


def payload(content: str, tool_calls: list[Any] | None = None) -> dict[str, Any]:
    return {"content": content, "tool_calls": tool_calls or []}


async def test_migrations_apply_once(pool: AsyncConnectionPool) -> None:
    assert await migrate(pool) == []
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT version FROM ds.schema_version")
        assert await cur.fetchall() == [(v,) for v in range(1, 18)]


async def test_a_spec_is_stored_once_per_content_hash(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        spec_id = await repo.save_spec(conn, SPEC, created_by=TESTER.actor)
        assert await repo.save_spec(conn, SPEC, created_by=TESTER.actor) == spec_id
        assert (await repo.get_spec(conn, spec_id))["body"] == {**SPEC.raw, "tolerance": 0.01}
        assert [s["spec_id"] for s in await repo.list_specs(conn)] == [spec_id]


async def test_keys_are_leased_completed_and_rechecked(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run(["a", "b", "c"], status="queued")
    now = datetime.now(UTC)
    async with pool.connection() as conn:
        assert await repo.lease_due(conn, "w1", 10, 60) == []  # only running runs are worked
        await repo.set_run_status(conn, run_id, "running")
        first = opened(await repo.lease_due(conn, "w1", 2, 60))
        assert [(r["key"], r["key_masked"], r["pass_no"]) for r in sorted(first, key=lambda r: r["key"])] == [
            ("a", "m-a", 1),
            ("b", "m-b", 1),
        ]
        assert keys(await repo.lease_due(conn, "w1", 10, 60)) == ["c"]
        assert await repo.lease_due(conn, "w1", 10, 60) == []  # every key is held
        assert await repo.complete_key(conn, run_id, 1, "m-a", "w1")
        assert not await repo.complete_key(conn, run_id, 1, "m-a", "w1")  # no longer held
        assert await repo.schedule_recheck(conn, run_id, 1, "m-b", "w1", now + LATER)
        assert await repo.schedule_recheck(conn, run_id, 1, "m-c", "w1", now - LATER)
        [recheck] = opened(await repo.lease_due(conn, "w2", 10, 60))  # b is not due yet
        assert (recheck["key"], recheck["pass_no"]) == ("c", 2)
        assert not await repo.cycle_done(conn, run_id, 1)


async def test_leases_are_released_and_open_keys_cancelled(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id = await make_run(["a", "b", "c"])
    async with pool.connection() as conn:
        assert keys(await repo.lease_due(conn, "w1", 2, 60)) == ["a", "b"]
        assert await repo.schedule_recheck(conn, run_id, 1, "m-b", "w1", datetime.now(UTC) + LATER)
        assert await repo.release_leases(conn, "w1") == 1  # a is pending again, due after c
        assert keys(await repo.lease_due(conn, "w2", 1, 60)) == ["c"]
        assert await repo.cancel_keys(conn, run_id) == 2  # a and b; c is under a live lease
        assert not await repo.cycle_done(conn, run_id, 1)
        assert await repo.complete_key(conn, run_id, 1, "m-c", "w2")
        assert await repo.cycle_done(conn, run_id, 1)
        assert (await repo.cycle_counts(conn, run_id))["keys"] == {"done": 1, "cancelled": 2}


async def test_a_running_scan_keeps_its_cycle_open(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run([], scan=new_scan())
    async with pool.connection() as conn:
        assert not await repo.cycle_done(conn, run_id, 1)  # no key yet, but the scan may add some
        assert await repo.save_scan(conn, run_id, 1, new_scan() | {"done": True}, {}, 0)
        assert await repo.cycle_done(conn, run_id, 1)


async def test_a_scan_checkpoint_saves_nothing_once_its_run_stopped_running(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id = await make_run([], scan=new_scan())
    first = new_scan() | {"after": {"enc": "a"}, "candidates": 1}
    async with pool.connection() as conn:
        assert await repo.save_scan(conn, run_id, 1, first, TEST_SEALER.seal_keys({"a": "m-a"}), 0)
        await repo.set_run_status(conn, run_id, "paused")
        second = first | {"after": {"enc": "b"}, "candidates": 2}
        assert not await repo.save_scan(conn, run_id, 1, second, TEST_SEALER.seal_keys({"b": "m-b"}), 1)
        assert (await repo.get_run(conn, run_id))["scan"] == first | {"checkpoint": 1}
        assert (await repo.cycle_counts(conn, run_id))["keys"] == {"pending": 1}


async def test_a_scan_checkpoint_saves_nothing_once_another_scanner_saved(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id = await make_run([], scan=new_scan())
    mine = new_scan() | {"after": {"enc": "a"}, "candidates": 1}
    found = TEST_SEALER.seal_keys({"a": "m-a"})
    async with pool.connection() as conn:
        assert await repo.save_scan(conn, run_id, 1, mine, found, 0)
        assert not await repo.save_scan(conn, run_id, 1, mine, found, 0)  # built on the old one
        assert (await repo.cycle_counts(conn, run_id))["keys"] == {"pending": 1}


async def test_concurrent_leasers_never_get_the_same_key(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    all_keys = [f"k{i}" for i in range(10)]
    await make_run(all_keys)
    async with pool.connection() as first, pool.connection() as second:
        mine = await repo.lease_due(first, "w1", 6, 60)  # uncommitted: its rows stay locked
        theirs = await asyncio.wait_for(repo.lease_due(second, "w2", 6, 60), timeout=10)
    assert len(mine) == 6 and len(theirs) == 4
    assert sorted(keys(mine) + keys(theirs)) == sorted(all_keys)


async def test_leasing_never_scans_the_keys_of_paused_runs(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    await make_run([f"p{i}" for i in range(5000)], status="paused")
    await make_run(["a"])
    async with await psycopg.AsyncConnection.connect(pool.conninfo, autocommit=True) as conn:
        await conn.execute("ANALYZE ds.runs, ds.run_keys")  # as autovacuum would: plans follow statistics

        async def rows_read() -> int:
            await conn.execute("SELECT pg_stat_force_next_flush()")  # flushed once this statement ends
            cur = await conn.execute(
                """SELECT (SELECT seq_tup_read FROM pg_stat_user_tables WHERE relid = 'ds.run_keys'::regclass)
                        + (SELECT sum(idx_tup_read) FROM pg_stat_user_indexes
                           WHERE relid = 'ds.run_keys'::regclass)"""
            )
            return (await cur.fetchone())[0]

        before = await rows_read()
        assert keys(await repo.lease_due(conn, "w1", 1, 60)) == ["a"]
        assert await rows_read() - before < 100


async def test_renewing_a_lease_reads_the_leased_index(
    pool: AsyncConnectionPool, make_run: MakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 017: a renewal seeks its owner's leases. Before it, every renewal of every worker slot
    scanned ``ds.run_keys`` whole, the pending keys of a large run among them."""
    plans, changed = [], repo._changed

    async def explained(conn: repo.Conn, sql: Any, params: Any) -> int:
        found = await (await conn.execute("EXPLAIN " + sql, params)).fetchall()
        plans.append("\n".join(line for (line,) in found))
        return await changed(conn, sql, params)

    run_id = await make_run([f"k{i}" for i in range(3)])
    async with await psycopg.AsyncConnection.connect(pool.conninfo, autocommit=True) as conn:
        await conn.execute(
            """INSERT INTO ds.run_keys (run_id, cycle, key_enc, key_masked, seq)
               SELECT %s, 1, decode(repeat('00', 30), 'hex'), 'm-p' || g, g
               FROM generate_series(3, 20002) g""",
            (run_id,),
        )
        assert len(await repo.lease_due(conn, "w1", 3, 60)) == 3
        await conn.execute("ANALYZE ds.runs, ds.run_keys")  # as autovacuum would: plans follow statistics
        monkeypatch.setattr(repo, "_changed", explained)
        assert await repo.renew_lease(conn, "w1", 60) and not await repo.renew_lease(conn, "w2", 60)
    assert len(plans) == 2 and all("run_keys_leased_idx" in plan for plan in plans)
    assert not any("Seq Scan on run_keys" in plan for plan in plans)


async def test_an_expired_lease_is_leased_again(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run(["a"])
    async with pool.connection() as conn:
        await repo.lease_due(conn, "crashed", 1, 0)
    async with pool.connection() as conn:
        assert keys(await repo.lease_due(conn, "w2", 1, 60)) == ["a"]
        assert not await repo.complete_key(conn, run_id, 1, "m-a", "crashed")  # it lost the lease
        assert await repo.complete_key(conn, run_id, 1, "m-a", "w2")


async def test_a_result_upsert_counts_revisions_and_replaces_findings(
    pool: AsyncConnectionPool, make_run: MakeRun, tmp_path: Path
) -> None:
    run_id = await make_run(["a"])
    store = evidence_store(tmp_path)
    obs_a, obs_b = (
        store.put_blob(
            Observation("a", side, 200, b'{"x": 1}', "2026-09-24T10:00:00.123456Z", 12.5, {"etag": "e"})
        )
        for side in Side
    )
    first = result(
        "a", Outcome.MISMATCH, [Severity.HIGH, Severity.LOW], obs_a_id=obs_a.obs_id, obs_b_id=obs_b.obs_id
    )
    recheck = result("a", Outcome.KNOWN_DIFF, [Severity.LOW], confirmation=Confirmation.RECHECKED)
    async with pool.connection() as conn:
        for obs in (obs_a, obs_b, obs_a):  # a repeated insert is a no-op
            await repo.insert_observation(conn, run_id, 1, 1, "m-a", obs, headers_enc(1, obs))
        assert await save(conn, run_id, first) == 1
        before = await repo.get_key_detail(conn, run_id, "m-a")
        assert await save(conn, run_id, recheck) == 2
        after = await repo.get_key_detail(conn, run_id, "m-a")
        row = await repo.get_observation(conn, run_id, obs_a.obs_id)
        assert repo.as_observation(TEST_SEALER.open_row(row)) == dataclasses.replace(obs_a, body=None)
    assert [f["severity"] for f in before["findings"]] == ["HIGH", "LOW"]
    assert {k: after["result"][k] for k in ("outcome", "first_outcome", "confirmation", "revision")} == {
        "outcome": "KNOWN_DIFF",
        "first_outcome": "MISMATCH",
        "confirmation": "rechecked",
        "revision": 2,
    }
    assert [f["severity"] for f in after["findings"]] == ["LOW"]
    # The finding at /f0 is revised, not replaced: a citation of its id stays valid; /f1's is gone.
    assert [f["finding_id"] for f in after["findings"]] == [before["findings"][0]["finding_id"]]
    assert after["findings"][0]["value_a"].startswith("tok:")  # masked cells, never raw values
    assert sorted(o["obs_id"] for o in after["observations"]) == sorted([obs_a.obs_id, obs_b.obs_id])


async def test_results_and_findings_filter_and_page(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    all_keys = [f"k{i}" for i in range(5)]
    run_id = await make_run(all_keys)
    async with pool.connection() as conn:
        for i, key in enumerate(all_keys):
            await save(
                conn,
                run_id,
                result(key, Outcome.MISMATCH, [Severity.HIGH, Severity.LOW]) if i % 2 == 0 else result(key),
            )
        page, cursor = await repo.list_results(conn, run_id, outcome="MISMATCH", limit=2)
        rest, end = await repo.list_results(conn, run_id, outcome="MISMATCH", limit=2, cursor=cursor)
        findings, cursor = [], None
        while True:
            rows, cursor = await repo.list_findings(
                conn, run_id, limit=3, cursor=cursor
            )  # a page ends mid-key
            findings += rows
            if cursor is None:
                break
        high, _ = await repo.list_findings(conn, run_id, severity="HIGH")
        one_key, _ = await repo.list_findings(conn, run_id, key_masked="m-k2")
        with pytest.raises(ValueError):
            await repo.list_results(conn, run_id, cursor="1.2")
    assert [r["key_masked"] for r in page + rest] == ["m-k0", "m-k2", "m-k4"] and end is None
    assert [(f["key_masked"], f["severity"]) for f in findings] == [
        (f"m-k{i}", severity) for i in (0, 2, 4) for severity in ("HIGH", "LOW")
    ]
    assert [f["key_masked"] for f in high] == ["m-k0", "m-k2", "m-k4"]
    assert [(f["key_masked"], f["outcome"]) for f in one_key] == [("m-k2", "MISMATCH")] * 2


def _read(plan: dict[str, Any]) -> float:
    """The rows a plan's nodes produced and removed by filters, over all their loops."""
    removed = plan.get("Rows Removed by Filter", 0) + plan.get("Rows Removed by Join Filter", 0)
    here = (plan["Actual Rows"] + removed) * plan["Actual Loops"]
    return here + sum(_read(child) for child in plan.get("Plans", []))


def _explained(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The plan of every statement ``repo._rows`` runs from now on, as it ran."""
    plans: list[dict[str, Any]] = []
    rows = repo._rows

    async def explained(conn: repo.Conn, sql: Any, params: Any = None) -> list[dict[str, Any]]:
        found = await (await conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)).fetchone()
        plans.append(found[0][0]["Plan"])  # type: ignore[index]
        return await rows(conn, sql, params)

    monkeypatch.setattr(repo, "_rows", explained)
    return plans


async def test_a_page_of_findings_reads_its_own_rows_once_a_new_store_is_analysed(
    pool: AsyncConnectionPool, make_run: MakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a store whose tables were never analysed the planner takes a run's rows for one, and a page of
    findings joined each key to all of the run's findings. ``analyse_new``, which
    the cycle end calls, gives it statistics, and a later page seeks its first key: timing-free, the rows
    each page's plan reads."""
    n, page, run_id, plans = 5000, 100, await make_run([]), _explained(monkeypatch)
    where = {"run_id": run_id, "n": n}
    async with pool.connection() as conn:
        for table in ("run_keys", "results", "findings", "observations"):  # no autovacuum meanwhile
            await conn.execute(f"ALTER TABLE ds.{table} SET (autovacuum_enabled = off)")  # type: ignore[arg-type]
        await conn.execute(
            """INSERT INTO ds.run_keys (run_id, cycle, key_enc, key_masked, seq, state)
               SELECT %(run_id)s, 1, decode(repeat('00', 30), 'hex'), 'm-' || g, g, 'done'
               FROM generate_series(0, %(n)s - 1) g""",
            where,
        )
        await conn.execute(
            """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
                   confirmation, first_outcome)
               SELECT %(run_id)s, 1, 'm-' || g, 'MISMATCH', 'COMPLETE', 'COMPLETE', 'provisional', 'MISMATCH'
               FROM generate_series(0, %(n)s - 1) g""",
            where,
        )
        await conn.execute(
            """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, change_type, direction,
                   path_pattern, json_pointer, suppressed)
               SELECT %(run_id)s, 1, 'm-' || g, 0, 'F-' || lpad(to_hex(g), 16, '0'), 'value_changed', 'both',
                   '$.x', '/x', false
               FROM generate_series(0, %(n)s - 1) g""",
            where,
        )
        first, _ = await repo.list_findings(conn, run_id, limit=page)
        assert await repo.analyse_new(conn) and not await repo.analyse_new(conn)  # once, then analysed
        again, _ = await repo.list_findings(conn, run_id, limit=page)
        later, _ = await repo.list_findings(conn, run_id, limit=page, cursor="1999.0")
    assert _read(plans[0]) > n * page  # unanalysed: each of the page's keys met every finding
    assert _read(plans[1]) < 10 * page and _read(plans[2]) < 10 * page  # its own rows, not 2,000 keys' more
    assert [f["key_seq"] for f in first] == [f["key_seq"] for f in again] == list(range(page))
    assert [f["key_seq"] for f in later] == list(range(2000, 2100))


async def _cycle(conn: repo.Conn, run_id: str, n: int, first: int) -> None:
    """Cycle 1 of ``run_id`` as a worker leaves it: ``n`` keys, each with a result, one finding (of every
    fifty: one discrepant, and one a ``bug`` rule suppressed, linked to a root cause) and a first read and a
    re-read of each side. ``first`` keeps the finding ids of two runs apart."""
    where = {"run_id": run_id, "n": n, "first": first}
    await conn.execute(
        """INSERT INTO ds.run_keys (run_id, cycle, key_enc, key_masked, seq, state)
           SELECT %(run_id)s, 1, decode(repeat('00', 30), 'hex'), 'm-' || g, g, 'done'
           FROM generate_series(0, %(n)s - 1) g""",
        where,
    )
    await conn.execute(
        """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
               confirmation, first_outcome)
           SELECT %(run_id)s, 1, 'm-' || g, 'MISMATCH', 'COMPLETE', 'COMPLETE', 'provisional', 'MISMATCH'
           FROM generate_series(0, %(n)s - 1) g""",
        where,
    )
    await conn.execute(
        """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, change_type, direction,
               path_pattern, json_pointer, severity, suppressed, rule_id, rule_classification, root_cause_id)
           SELECT %(run_id)s, 1, 'm-' || g, 0, 'F-' || lpad(to_hex(%(first)s + g), 16, '0'), 'value_changed',
               'both', '$.x', '/x', 'HIGH', g %% 50 <> 0, CASE WHEN g %% 50 <> 0 THEN 'KD-1' END,
               CASE g %% 50 WHEN 0 THEN NULL WHEN 1 THEN 'bug' ELSE 'accepted' END,
               CASE WHEN g %% 50 = 1 THEN 'RC-' || g %% 3 END
           FROM generate_series(0, %(n)s - 1) g""",
        where,
    )
    await conn.execute(
        """INSERT INTO ds.observations (run_id, cycle, pass_no, obs_id, key_masked, side, attempts, pages,
               pages_eof, pagination_unstable, headers_enc, observed_at)
           SELECT %(run_id)s, 1, pass, 'O-' || lpad(to_hex(8 * g + 2 * pass + side), 16, '0'), 'm-' || g,
               chr(97 + side), 1, 1, true, false, decode('00', 'hex'), now()
           FROM generate_series(0, %(n)s - 1) g, generate_series(1, 2) pass, generate_series(0, 1) side""",
        where,
    )


@pytest.mark.parametrize("held", [True, False])
async def test_a_cycle_the_statistics_do_not_hold_is_read_by_key(
    pool: AsyncConnectionPool, make_run: MakeRun, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    """Autovacuum analyses a table once a tenth of it changed, so the statistics do not hold a run that is
    small beside the store, nor a soak's later cycle (``held``: they hold another run alone); a new store's
    hold nothing until its first cycle ends. The planner then takes the cycle for one row. Written as plain
    joins, a page of a cycle's end and of its reports joined on (run_id, cycle) alone and read the whole
    cycle, and so did each discrepant finding of a report. Each of these reads probes by key whatever the
    estimates: timing-free, the rows each statement's plan reads."""
    n, page = 5000, 100
    known, new = await make_run([]), await make_run([])
    async with pool.connection() as conn:
        for table in ("run_keys", "results", "findings", "observations"):  # no autovacuum meanwhile
            await conn.execute(f"ALTER TABLE ds.{table} SET (autovacuum_enabled = off)")  # type: ignore[arg-type]
        if held:
            await _cycle(conn, known, n, 0)
            await conn.execute("ANALYZE ds.run_keys, ds.results, ds.findings, ds.observations")
        await _cycle(conn, new, n, n)
        assert not held or not await repo.analyse_new(conn)  # the end analyses a store with none only
        plans = _explained(monkeypatch)
        walked = [walk async for walk in repo.result_pages(conn, new, 1, size=page)]
        paged, plans[:] = list(plans), []
        discrepant, count = await repo.discrepant_findings(conn, new, 1, ["CRITICAL", "HIGH"], 25)
        bugs = await repo.bug_rules(conn, new, 1)
        again = await repo.rechecks(conn, new, 1, range(2000, 2000 + page))
    assert [r["seq"] for results, _ in walked for r in results] == list(range(n))
    assert all(set(found) == {r["key_masked"] for r in results} for results, found in walked)
    assert len(paged) == 2 * n // page and max(map(_read, paged)) < 10 * page  # a page's own rows
    assert count == 2 * n // 50 and [f["key_seq"] for f in discrepant] == [
        g // 2 * 50 + g % 2 for g in range(25)
    ]
    assert bugs == {f"RC-{g}": "KD-1" for g in range(3)}
    assert again == {
        f"m-{g}": {side: f"O-{8 * g + 4 + n:016x}" for n, side in enumerate("ab")} for g in range(2000, 2100)
    }
    # The findings of the cycle once, then the discrepant ones' keys and results; a page's keys' re-reads.
    assert [_read(plan) < limit for plan, limit in zip(plans, (2 * n, 2 * n, 20 * page), strict=True)] == [
        True
    ] * 3


async def test_pages_drains_every_page() -> None:
    async def fetch(prefix: str, *, limit: int, cursor: str | None = None) -> repo.Page:
        start = int(cursor or 0)
        rows = [{"n": f"{prefix}{i}"} for i in range(start, min(start + limit, 2500))]
        return rows, str(start + limit) if start + limit < 2500 else None

    assert [r["n"] for r in await repo.pages(fetch, "k")] == [f"k{i}" for i in range(2500)]


async def test_root_causes_are_replaced_and_linked_to_findings(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id = await make_run(["k0", "k1"])
    cause = RootCause(
        root_cause_id="RC-" + "a" * 12,
        kind="signature",
        path_pattern="$.f0",
        change_type=ChangeType.VALUE_CHANGED,
        direction=Direction.BOTH,
        affected_keys=2,
        finding_count=2,
        max_severity=Severity.HIGH,
        sample_keys=("m-k0", "m-k1"),
    )
    async with pool.connection() as conn:
        for key in ("k0", "k1"):
            await save(conn, run_id, result(key, Outcome.MISMATCH, [Severity.HIGH]))
        findings, _ = await repo.list_findings(conn, run_id)
        await repo.replace_root_causes(
            conn, run_id, 1, [cause], {f["finding_id"]: cause.root_cause_id for f in findings}
        )
        await repo.replace_root_causes(
            conn, run_id, 1, [cause], {findings[0]["finding_id"]: cause.root_cause_id}
        )
        causes = await repo.list_root_causes(conn, run_id)
        linked, _ = await repo.list_findings(conn, run_id, root_cause_id=cause.root_cause_id)
    assert [(c["root_cause_id"], c["max_severity"], c["sample_keys"]) for c in causes] == [
        (cause.root_cause_id, "HIGH", ["m-k0", "m-k1"])
    ]
    assert [f["finding_id"] for f in linked] == [findings[0]["finding_id"]]


async def test_soak_cycles_list_outcome_changes(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run(["k1", "k2"], mode="soak")
    async with pool.connection() as conn:
        for key in ("k1", "k2"):
            await save(conn, run_id, result(key))
        assert await repo.start_next_cycle(conn, run_id, datetime.now(UTC)) == 2  # same key set
        await save(conn, run_id, result("k1"), cycle=2)
        await save(conn, run_id, result("k2", Outcome.MISMATCH, [Severity.HIGH]), cycle=2)
        changes = await repo.outcome_changes(conn, run_id)
        current, _ = await repo.list_results(conn, run_id)
        k3 = TEST_SEALER.seal_keys({"k3": "m-k3"})
        assert await repo.start_next_cycle(conn, run_id, datetime.now(UTC) + LATER, k3) == 3
        rolled = await repo.cycle_counts(conn, run_id)
    assert [(c["cycle"], c["key_masked"], c["from_outcome"], c["to_outcome"]) for c in changes] == [
        (2, "m-k2", "MATCH", "MISMATCH")
    ]
    assert [(r["cycle"], r["outcome"]) for r in current] == [(2, "MATCH"), (2, "MISMATCH")]
    assert rolled == {"keys": {"pending": 1}, "outcomes": {}}


async def test_run_status_moves_only_from_the_expected_states(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    first, second = await make_run(["a"]), await make_run(["a"])
    async with pool.connection() as conn:
        assert await repo.set_run_status(conn, second, "paused", when=["running"])
        assert not await repo.set_run_status(conn, second, "running", when=["queued"])
        assert await repo.set_run_status(conn, first, "completed", stop_reason="max_mismatch_rate")
        run = await repo.get_run(conn, first)
        previous = await repo.previous_run(conn, second)
        page, cursor = await repo.list_runs(conn, limit=1)
        rest, end = await repo.list_runs(conn, limit=1, cursor=cursor)
        paused, _ = await repo.list_runs(conn, status="paused")
    assert (run["status"], run["stop_reason"], run["spec_name"]) == ("completed", "max_mismatch_rate", "demo")
    assert run["started_at"] <= run["finished_at"]
    assert previous["run_id"] == first
    assert [r["run_id"] for r in page + rest] == [second, first] and end is None
    assert [r["run_id"] for r in paused] == [second]


async def test_chat_messages_are_numbered_in_order(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run(["a"])
    async with pool.connection() as conn:
        session_id = await repo.create_chat_session(conn, run_id, "m-a", created_by=TESTER.actor)

    async def ask(i: int) -> int:
        async with pool.connection() as conn:
            return await repo.append_chat_message(
                conn, session_id, "user", payload(f"question {i}"), TEST_SEALER
            )

    assert sorted(await asyncio.gather(*(ask(i) for i in range(5)))) == [1, 2, 3, 4, 5]
    async with pool.connection() as conn:
        answer = "Two keys differ [F-1].\x00"  # NUL is kept as the text \\u0000
        seq = await repo.append_chat_message(
            conn, session_id, "assistant", payload(answer, [{"n": 1}]), TEST_SEALER, citations=["F-1"]
        )
        session = await repo.get_chat_session(conn, session_id)
        answers = opened(await repo.chat_answers(conn, run_id))
    messages = opened(session["messages"])
    assert seq == 6 and session["key_masked"] == "m-a"
    assert [(m["seq"], m["cycle"]) for m in messages] == [(n, 1) for n in range(1, 7)]
    assert (messages[-1]["citations"], messages[-1]["tool_calls"]) == (["F-1"], [{"n": 1}])
    assert [(a["content"], a["key_masked"]) for a in answers] == [("Two keys differ [F-1].\\u0000", "m-a")]


async def test_llm_calls_are_recorded_and_the_audit_log_is_append_only(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id = await make_run(["a"])
    call = {
        "profile_id": "p",
        "model_reported": "m",
        "usage_input": 10,
        "usage_output": 5,
        "cost_micros": 1500,
    }
    async with pool.connection() as conn:
        session_id = await repo.create_chat_session(conn, run_id, created_by=TESTER.actor)
        call_id = await repo.insert_llm_call(conn, session_id, call | {"latency_ms": 42})
        await repo.insert_audit(conn, TESTER, "run.create", run_id, {"keys": 1})
        cur = await conn.execute(
            "SELECT usage, cost, latency_ms FROM ds.llm_calls WHERE call_id = %s", (call_id,)
        )
        assert await cur.fetchone() == ({"input": 10, "output": 5}, Decimal("0.0015"), 42)
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await conn.execute("DELETE FROM ds.audit_log")


async def test_a_soak_scan_opens_each_cycle_with_its_own_scan_state(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id = await make_run([], mode="soak", scan=new_scan())
    ended = new_scan() | {"done": True, "matched": 9, "candidates": 1}
    due = datetime.now(UTC) + LATER
    async with pool.connection() as conn:
        assert await repo.save_scan(conn, run_id, 1, ended, TEST_SEALER.seal_keys({"k1": "m-k1"}), 0)
        assert await repo.start_next_cycle(conn, run_id, due, {}, new_scan() | {"due": due.isoformat()}) == 2
        run = await repo.get_run(conn, run_id)
        first, second = await repo.get_scan(conn, run_id, 1), await repo.get_scan(conn, run_id, 2)
        assert await repo.get_scan(conn, run_id, 3) is None
        assert (await repo.cycle_counts(conn, run_id))["keys"] == {}  # the next scan finds its keys
        assert not await repo.cycle_done(conn, run_id, 2)  # its scan has not run
    assert first == ended | {"checkpoint": 1}
    assert run["scan"] == second == new_scan() | {"due": due.isoformat()}


async def test_a_scan_checkpoint_saves_on_its_own_cycle(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run([], mode="soak", scan=new_scan())
    first = new_scan() | {"done": True}
    async with pool.connection() as conn:
        assert await repo.save_scan(conn, run_id, 1, first, {}, 0)
        assert await repo.cycle_done(conn, run_id, 1)
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC), {}, new_scan())
        second = new_scan() | {"after": {"enc": "k2"}, "candidates": 1}
        assert await repo.save_scan(conn, run_id, 2, second, TEST_SEALER.seal_keys({"k2": "m-k2"}), 0)
        assert await repo.get_scan(conn, run_id, 1) == first | {"checkpoint": 1}
        assert (await repo.get_run(conn, run_id))["scan"] == second | {"checkpoint": 1}
        assert keys((await repo.list_keys(conn, run_id, 2))[0]) == ["k2"]
        assert (await repo.list_keys(conn, run_id, 1))[0] == []
        assert not await repo.cycle_done(conn, run_id, 2)
        assert await repo.cycle_done(conn, run_id, 1)  # each cycle judged by its own scan and keys


def graph_row(label: str, code: str = "GROUP_MEMBERS_DIFFER", **fields: Any) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "count_a": 2,
        "count_b": 1,
        "only_a": 1,
        "only_b": 0,
        "detail": {},
    } | fields


async def test_graph_diffs_are_replaced_per_cycle_and_paged(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id = await make_run([], mode="soak", scan=new_scan())
    async with pool.connection() as conn:
        await repo.replace_graph_diffs(conn, run_id, 1, [graph_row("tok:old")])
        replaced = [graph_row("tok:g1", detail={"only_a": ["tok:m1"]}), graph_row("tok:c1", "CLUSTER_SPLIT")]
        await repo.replace_graph_diffs(conn, run_id, 1, replaced)
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC), {}, new_scan())
        await repo.replace_graph_diffs(conn, run_id, 2, [graph_row("tok:g2")])
        page, cursor = await repo.list_graph_diffs(conn, run_id, 1, limit=1)
        rest, end = await repo.list_graph_diffs(conn, run_id, 1, limit=1, cursor=cursor)
        current, _ = await repo.list_graph_diffs(conn, run_id)
    assert page + rest == [row | {"seq": i} for i, row in enumerate(replaced)]
    assert end is None
    assert [(r["seq"], r["label"]) for r in current] == [(0, "tok:g2")]


async def test_results_are_read_by_key_for_a_cycle(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run(["a", "b"], mode="soak")
    async with pool.connection() as conn:
        await save(conn, run_id, result("a", Outcome.MISMATCH, [Severity.HIGH]))
        await save(conn, run_id, result("b"))
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        await save(conn, run_id, result("a"), cycle=2)
        found = await repo.get_results(conn, run_id, 1, ["m-a", "m-zz"])
        assert keys(await repo.unmatched_keys(conn, run_id, 1)) == ["a"]  # b matched
        assert await repo.unmatched_keys(conn, run_id, 2) == []
    assert list(found) == ["m-a"]
    assert (found["m-a"]["cycle"], found["m-a"]["outcome"]) == (1, "MISMATCH")


async def test_a_group_of_keys_moves_on_only_while_its_owner_holds_them(
    pool: AsyncConnectionPool, make_run: MakeRun
) -> None:
    run_id, other = await make_run(["a", "b", "c", "d"]), await make_run(["x"])
    async with pool.connection() as conn:
        assert keys(await repo.lease_due(conn, "w1", 10, 60, run_id=other)) == ["x"]
        assert keys(await repo.lease_due(conn, "w1", 3, 60, run_id=run_id)) == ["a", "b", "c"]
        assert await repo.schedule_rechecks(conn, run_id, 1, {"m-a": datetime.now(UTC) - LATER}, "w1") == 1
        assert await repo.complete_keys(conn, run_id, 1, ["m-b", "m-c"], "w2") == 0  # w1 holds them
        assert await repo.complete_keys(conn, run_id, 1, ["m-a", "m-b", "m-c", "m-d"], "w1") == 2  # b and c
        assert await repo.schedule_rechecks(conn, run_id, 1, {"m-b": datetime.now(UTC)}, "w1") == 0
        again = opened(await repo.lease_due(conn, "w3", 10, 60, run_id=run_id))
    assert sorted((r["key"], r["pass_no"]) for r in again) == [("a", 2), ("d", 1)]


async def test_migration_4_moves_a_run_s_scan_to_its_current_cycle(pg_dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute("CREATE SCHEMA ds; CREATE TABLE ds.schema_version (version integer PRIMARY KEY)")
        for path in sorted(MIGRATIONS.glob("00[123]_*.sql")):  # the store as the previous release left it
            await conn.execute(path.read_bytes())
            await conn.execute("INSERT INTO ds.schema_version VALUES (%s)", (int(path.name[:3]),))
        await conn.execute(
            "INSERT INTO ds.specs (spec_id, name, version, content_hash, body)"
            " VALUES (1, 'd', '1', 'h', '{}')"
        )
        await conn.execute(
            "INSERT INTO ds.runs (run_id, spec_id, mode, cycle, scan) VALUES (%s, 1, 'soak', 2, %s)",
            ("R-" + "0" * 26, Jsonb(new_scan() | {"matched": 3})),
        )
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    try:
        assert await migrate(pool) == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
        async with pool.connection() as conn:
            run = await repo.get_run(conn, "R-" + "0" * 26)
            assert await repo.get_scan(conn, run["run_id"], 1) is None
    finally:
        await pool.close()
    assert (run["cycle"], run["scan"]) == (2, new_scan() | {"matched": 3})


# -- identity ------------------------------------------------------------------


async def test_migration_5_keeps_legacy_rows_and_checks_new_ones(pg_dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute("CREATE SCHEMA ds; CREATE TABLE ds.schema_version (version integer PRIMARY KEY)")
        for path in sorted(MIGRATIONS.glob("00[1234]_*.sql")):  # the store as release 004 left it
            await conn.execute(path.read_bytes())
            await conn.execute("INSERT INTO ds.schema_version VALUES (%s)", (int(path.name[:3]),))
        await conn.execute(
            "INSERT INTO ds.specs (spec_id, name, version, content_hash, body)"
            " VALUES (1, 'd', '1', 'h', '{}')"
        )
        await conn.execute(
            "INSERT INTO ds.runs (run_id, spec_id, mode) VALUES (%s, 1, 'batch')", ("R-" + "0" * 26,)
        )
        await conn.execute(
            "INSERT INTO ds.chat_sessions (session_id, run_id) VALUES ('S-1', %s)", ("R-" + "0" * 26,)
        )
        await conn.execute(
            "INSERT INTO ds.audit_log (actor, action, object) VALUES ('api', 'spec.create', '1')"
        )
        await conn.execute(
            "INSERT INTO ds.audit_log (actor, action, object) VALUES ('ds', 'create_run', 'R-1')"
        )
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    try:
        assert await migrate(pool) == [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
        async with pool.connection() as conn:
            run = await repo.get_run(conn, "R-" + "0" * 26)
            cur = await conn.execute("SELECT actor, actor_name FROM ds.audit_log ORDER BY id")
            legacy = await cur.fetchall()
            session = await repo.get_chat_session(conn, "S-1")
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation, match="audit_actor"):
                await conn.execute(
                    "INSERT INTO ds.audit_log (actor, action, object) VALUES ('api', 'x', 'y')"
                )
    finally:
        await pool.close()
    assert (run["created_by"], run["created_by_name"], session["created_by"]) == (None, None, None)
    migration = ("system:migration", "migration")  # spec.rehash, installation.created, system.migrate
    assert legacy == [("api", None), ("ds", None), migration, migration, migration]


async def test_creators_are_kept_and_named(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        user = await repo.upsert_user(
            conn, "https://idp", "sub-1", "jdoe", "J. Doe", ["viewer"], signed_in=True
        )
        token, _ = await tokens.create(
            conn, TESTER, name="ci-bot", owner="team", role="operator", max_days=30
        )
        actors = [f"user:{user['user_id']}", f"token:{token['token_id']}", TESTER.actor, "user:U-" + "0" * 16]
        spec_id = await repo.save_spec(conn, SPEC, created_by=actors[0])
        assert await repo.save_spec(conn, SPEC, created_by=actors[1]) == spec_id  # the first uploader stays
        runs = [repo.new_run_id() for _ in actors]
        for run_id, actor in zip(runs, actors, strict=True):
            await repo.create_run(
                conn, spec_id, "batch", {}, {}, run_id=run_id, created_by=actor, retention="standard",
                data_key=TEST_DATA_KEY,
            )  # fmt: skip
        named = [(await repo.get_run(conn, run_id))["created_by_name"] for run_id in runs]
        listed = {r["run_id"]: r["created_by_name"] for r in (await repo.list_runs(conn))[0]}
        session_id = await repo.create_chat_session(conn, runs[0], created_by=actors[0])
        spec = await repo.get_spec(conn, spec_id)
        chat = await repo.get_chat_session(conn, session_id)
    assert (spec["created_by"], chat["created_by"]) == (actors[0], actors[0])
    assert named == ["jdoe", "ci-bot", TESTER.actor, "user:U-" + "0" * 16] == [listed[r] for r in runs]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("INSERT INTO ds.users (user_id, issuer, subject, username, roles) VALUES (%s, 'i', 's', %s, '{}')",
         ("U-" + "1" * 16, "jdoe\x1b[31m")),
        ("INSERT INTO ds.users (user_id, issuer, subject, username, roles) VALUES (%s, 'i', 's', %s, '{}')",
         ("U-" + "1" * 16, "jdoe\u0085")),
        ("INSERT INTO ds.users (user_id, issuer, subject, username, roles) VALUES (%s, 'i', 's', %s, '{}')",
         ("U-" + "1" * 16, "jdoe\x7f")),
        ("INSERT INTO ds.users (user_id, issuer, subject, username, display_name, roles)"
         " VALUES (%s, 'i', 's', 'jdoe', %s, '{}')", ("U-" + "1" * 16, "J\x9bDoe")),
        ("INSERT INTO ds.users (user_id, issuer, subject, username, roles)"
         " VALUES (%s, 'i', %s, 'jdoe', '{}')", ("U-" + "1" * 16, "é" * 200)),  # 200 characters, 400 octets
        ("INSERT INTO ds.users (user_id, issuer, subject, username, roles) VALUES (%s, 'i', 's', 'jdoe', %s)",
         ("U-" + "1" * 16, ["root"])),
        ("INSERT INTO ds.audit_log (actor, actor_name, action, object) VALUES (%s, %s, 'x', 'y')",
         ("local:a@b", "a\nb")),
        ("INSERT INTO ds.audit_log (actor, action, object) VALUES (%s, 'x', 'y')", ("local:a b@host",)),
        ("INSERT INTO ds.api_tokens (token_id, name, owner, role, secret_hash, created_by, expires_at)"
         " VALUES (%s, 'ci-bot', 'o', 'admin', %s, 'x', now() + interval '1 day')",
         ("T-" + "1" * 16, b"h" * 32)),
    ],
)  # fmt: skip
async def test_the_identity_checks_refuse_bad_rows(pool: AsyncConnectionPool, sql: str, params: Any) -> None:
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(sql, params)  # type: ignore[arg-type]


async def test_a_user_is_written_only_when_it_changes_or_signs_in(pool: AsyncConnectionPool) -> None:
    async def version(conn: repo.Conn) -> int:  # the row's transaction id changes with every write
        return (await (await conn.execute("SELECT xmin::text::bigint FROM ds.users")).fetchone())[0]

    async with pool.connection() as conn:
        new = await repo.upsert_user(conn, "https://idp", "sub-1", "jdoe", None, ["viewer"], signed_in=False)
    async with pool.connection() as conn:
        written = await version(conn)
        again = await repo.upsert_user(
            conn, "https://idp", "sub-1", "jdoe", None, ["viewer"], signed_in=False
        )
    async with pool.connection() as conn:
        unchanged = await version(conn) == written
        overage = await repo.upsert_user(conn, "https://idp", "sub-1", "jdoe", None, None, signed_in=True)
        renamed = await repo.upsert_user(
            conn, "https://idp", "sub-1", "jd", "J", ["operator"], signed_in=False
        )
        other = await repo.upsert_user(conn, "https://idp", "sub-2", "ann", None, None, signed_in=False)
    assert (new["previous_roles"], new["last_login_at"], unchanged) == (None, None, True)
    assert again["user_id"] == new["user_id"] and again["previous_roles"] == ["viewer"]
    assert (overage["roles"], overage["last_login_at"] is not None) == (["viewer"], True)  # kept
    assert (renamed["username"], renamed["display_name"], renamed["roles"]) == ("jd", "J", ["operator"])
    assert renamed["last_login_at"] == overage["last_login_at"]  # set by a sign-in only
    assert other["roles"] == []  # an overage on a first sign-in grants nothing


async def _session(conn: repo.Conn, user_id: str | None, mapping: str | None, **fields: Any) -> bytes:
    secret = sessions.secret_hash(sessions.new_secret())
    who = {"actor": f"user:{user_id}", "user_id": user_id} if user_id else {}
    await repo.create_session(
        conn, secret_hash=secret, name="jdoe", roles=["viewer"], max_hours=8, roles_sha256=mapping,
        **who | fields,
    )  # fmt: skip
    return secret


async def test_a_session_is_valid_until_it_ends_idles_or_its_user_or_mapping_changes(
    pool: AsyncConnectionPool,
) -> None:
    mapping = "a" * 64
    async with pool.connection() as conn:
        user = await repo.upsert_user(conn, "https://idp", "sub-1", "jdoe", None, ["viewer"], signed_in=True)
        uid = user["user_id"]
        live, idle, revoked, ended = [await _session(conn, uid, mapping) for _ in range(4)]
        await conn.execute(
            "UPDATE ds.sessions SET last_seen_at = now() - interval '31 minutes' WHERE secret_hash = %s",
            (idle,),
        )
        await conn.execute(
            "UPDATE ds.sessions SET created_at = now() - interval '9 hours',"
            " expires_at = now() - interval '1 hour' WHERE secret_hash = %s",
            (ended,),
        )
        one = await repo.session_by_secret(conn, revoked, mapping, idle_minutes=30)
        assert await repo.revoke_sessions(conn, "user:U-" + "f" * 16, session_id=one["session_id"]) == 1

        async def valid(secret: bytes, roles_sha256: str | None = mapping) -> bool:
            return await repo.session_by_secret(conn, secret, roles_sha256, idle_minutes=30) is not None

        states = [await valid(s) for s in (live, idle, revoked, ended)]
        edited = await valid(live, "b" * 64)  # the role mapping changed
        found = await repo.session_by_secret(conn, live, mapping, idle_minutes=30)
        listed = [r["session_id"] for r in await repo.list_sessions(conn)]
        disabled = await repo.set_user_disabled(conn, uid, "user:U-" + "f" * 16)
        after = await valid(live)
        again = await repo.set_user_disabled(conn, uid, "user:U-" + "f" * 16)
        enabled = await repo.set_user_disabled(conn, uid, None)
        with pytest.raises(ValueError, match="revoke a session"):
            await repo.revoke_sessions(conn, "x")
    assert (states, edited) == ([True, False, False, False], False)
    assert found["actor"] == f"user:{uid}" and "secret_hash" not in found
    assert (
        len(listed) == 2 and found["session_id"] in listed and one["session_id"] not in listed
    )  # live, idle
    assert (disabled["sessions_revoked"], after, again) == (2, False, None)
    assert (enabled["disabled_at"], enabled["disabled_by"], enabled["sessions_revoked"]) == (None, None, 0)


async def test_a_token_session_ends_with_its_token_and_touches_are_rate_limited(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        token, secret = await tokens.create(
            conn, TESTER, name="ci-bot", owner="team", role="viewer", max_days=90
        )
        tid = token["token_id"]
        session = await _session(conn, None, None, actor=f"token:{tid}", token_id=tid)
        in_oidc = await repo.session_by_secret(conn, session, "a" * 64, idle_minutes=30)
        row = await repo.session_by_secret(conn, session, None, idle_minutes=30)  # tokens mode: no mapping
        seen = row["last_seen_at"]
        await repo.touch_session(conn, row["session_id"])
        touched = (await repo.session_by_secret(conn, session, None, idle_minutes=30))["last_seen_at"]
        verified = await tokens.verify(conn, secret)
        wrong = await tokens.verify(conn, secret[:-1] + ("0" if secret[-1] != "0" else "1"))
        revoked = await repo.revoke_token(conn, tid, TESTER.actor)
        twice = await repo.revoke_token(conn, tid, TESTER.actor)
        gone = await repo.session_by_secret(conn, session, None, idle_minutes=30)
        refused = await tokens.verify(conn, secret)
        [listed] = await repo.list_tokens(conn)
    assert in_oidc is None  # a token's browser session ends when the deployment moves to SSO
    assert touched == seen  # used within the minute
    assert (verified["name"], verified["last_used_at"] is None, wrong) == ("ci-bot", True, None)
    assert (revoked["state"], twice, gone, refused, listed["state"]) == (
        "revoked",
        None,
        None,
        None,
        "revoked",
    )
    assert "secret_hash" not in listed and "secret_hash" not in revoked


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"name": "CI bot"}, "name must match"),
        ({"owner": ""}, "owner must be 1 to 200 characters without control characters"),
        ({"owner": "team\x1b"}, "owner must be 1 to 200 characters without control characters"),
        ({"role": "admin"}, "role must be one of operator, spec_author, viewer"),
        ({"days": 0}, "days must be from 1 to 30"),
        ({"days": 31}, "days must be from 1 to 30"),
        ({"owner_user_id": "U-" + "0" * 16}, "owner_user_id U-0000000000000000 is not an enabled user"),
    ],
)
async def test_a_token_is_refused_bad_fields(
    pool: AsyncConnectionPool, fields: dict[str, Any], refusal: str
) -> None:
    good = {"name": "ci-bot", "owner": "team", "role": "viewer", "max_days": 30}
    async with pool.connection() as conn:
        with pytest.raises(ValueError, match=re.escape(refusal)):
            await tokens.create(conn, TESTER, **good | fields)


async def test_a_users_tokens_are_listed_and_revoked_with_them(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        user = await repo.upsert_user(conn, "https://idp", "sub-1", "jdoe", None, ["admin"], signed_in=True)
        uid, person = user["user_id"], dataclasses.replace(TESTER, actor=f"user:{user['user_id']}")
        made, _ = await tokens.create(conn, person, name="made", owner="t", role="viewer", max_days=90)
        owned, _ = await tokens.create(
            conn, TESTER, name="owned", owner="t", role="operator", max_days=90, owner_user_id=uid
        )
        await tokens.create(conn, TESTER, name="other", owner="t", role="viewer", max_days=90)
        listed = await repo.user_tokens(conn, uid)
        revoked = await repo.revoke_user_tokens(conn, uid, TESTER.actor)
        after = await repo.user_tokens(conn, uid)
        default = (await tokens.create(conn, TESTER, name="dflt", owner="t", role="viewer", max_days=30))[0]
    assert sorted((t["name"], t["state"]) for t in listed) == [("made", "active"), ("owned", "active")]
    assert revoked == sorted([made["token_id"], owned["token_id"]])
    assert {t["state"] for t in after} == {"revoked"}
    assert (default["expires_at"] - default["created_at"]).days == 30  # min(90, token_max_days)


async def test_ended_sessions_are_deleted_after_30_days(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        user = await repo.upsert_user(conn, "https://idp", "sub-1", "jdoe", None, ["viewer"], signed_in=True)
        old, _ = [await _session(conn, user["user_id"], "a" * 64) for _ in range(2)]
        await conn.execute(
            "UPDATE ds.sessions SET revoked_at = now() - interval '31 days', revoked_by = 'x'"
            " WHERE secret_hash = %s",
            (old,),
        )
        assert await repo.delete_ended_sessions(conn) == 1
        cur = await conn.execute("SELECT count(*) FROM ds.sessions")
        assert (await cur.fetchone())[0] == 1


# -- data keys ------------------------------------------------------


async def test_data_keys_are_rewrapped_in_place_and_their_checks_refuse_bad_rows(
    pool: AsyncConnectionPool,
) -> None:
    kek, other = "K-" + "1" * 16, "K-" + "2" * 16
    async with pool.connection() as conn:
        await repo.insert_data_key(conn, "address", kek, b"w" * 40)
        await repo.rewrap_data_key(conn, "address", other, b"v" * 40)
        [row] = await repo.data_keys(conn)
        assert (await repo.data_key(conn, "address"), await repo.data_key(conn, "E-" + "0" * 16)) == (
            {"scope": "address", "kek": other, "wrapped": b"v" * 40},
            None,
        )
    assert row["rewrapped_at"] is not None and row["created_at"] <= row["rewrapped_at"]
    for scope, kek_id, wrapped in [
        ("R-1", kek, b"w" * 40),  # a run id is R- and 26 characters
        ("E-" + "0" * 16, "kek", b"w" * 40),
        ("E-" + "0" * 16, kek, b"w" * 32),  # AES-KW of a 32-byte key is 40 bytes
    ]:
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await repo.insert_data_key(conn, scope, kek_id, wrapped)


async def test_observations_take_hk1_addresses(pool: AsyncConnectionPool, make_run: MakeRun) -> None:
    run_id = await make_run(["a"])
    obs = Observation("a", Side.A, 200, None, "2026-09-27T10:00:00.000000Z", obs_id="O-" + "0" * 16)
    async with pool.connection() as conn:
        await repo.insert_observation(
            conn,
            run_id,
            1,
            1,
            "m-a",
            dataclasses.replace(obs, content_hash="hk1:" + "a" * 64),
            headers_enc(1, obs),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            await repo.insert_observation(
                conn,
                run_id,
                1,
                1,
                "m-a",
                dataclasses.replace(obs, content_hash="md5:ab"),
                headers_enc(1, obs),
            )


# -- the sealed spine ------------------------------------------------------


async def test_a_run_stores_its_data_key_and_retention_and_no_plaintext(
    pool: AsyncConnectionPool, make_run: MakeRun, tmp_path: Path
) -> None:
    run_id = await make_run(["a"], mode="soak")
    obs = evidence_store(tmp_path).put_blob(
        Observation("a", Side.A, 200, b"{}", "2026-09-27T10:00:00.000000Z", headers={"etag": "secret-etag"})
    )
    async with pool.connection() as conn:
        await repo.insert_observation(conn, run_id, 1, 1, "m-a", obs, headers_enc(1, obs))
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC))  # copies the sealed keys
        session_id = await repo.create_chat_session(conn, run_id, created_by=TESTER.actor)
        await repo.append_chat_message(conn, session_id, "user", payload("why a?"), TEST_SEALER)
        run, data_key = await repo.get_run(conn, run_id), await repo.data_key(conn, run_id)
        plaintext = await (  # 007 dropped the plaintext columns; the sealed ones are NOT NULL
            await conn.execute(
                """SELECT count(*) FROM information_schema.columns
                   WHERE table_schema = 'ds' AND (table_name, column_name) IN (('run_keys', 'key'),
                       ('observations', 'headers'), ('chat_messages', 'content'),
                       ('chat_messages', 'tool_calls'))"""
            )
        ).fetchone()
        copied = keys((await repo.list_keys(conn, run_id, 2))[0])
    assert (run["retention"], data_key) == ("standard", {"scope": run_id, "kek": TEST_DATA_KEY[0],
                                                         "wrapped": TEST_DATA_KEY[1]})  # fmt: skip
    assert plaintext == (0,) and copied == ["a"]


async def test_an_unopened_row_names_no_raw_value_and_shown_drops_the_sealed_columns(
    pool: AsyncConnectionPool, make_run: MakeRun, tmp_path: Path
) -> None:
    run_id = await make_run(["a"])
    obs = evidence_store(tmp_path).put_blob(
        Observation("a", Side.A, 200, b"{}", "2026-09-27T10:00:00.000000Z", headers={"etag": "e"})
    )
    async with pool.connection() as conn:
        [leased] = await repo.lease_due(conn, "w1", 1, 60)
        await repo.insert_observation(conn, run_id, 1, 1, "m-a", obs, headers_enc(1, obs))
        detail = await repo.get_key_detail(conn, run_id, "m-a")
        stored = await repo.get_observation(conn, run_id, str(obs.obs_id))
        session_id = await repo.create_chat_session(conn, run_id, "m-a", created_by=TESTER.actor)
        await repo.append_chat_message(conn, session_id, "user", payload("why a?"), TEST_SEALER)
        session = await repo.get_chat_session(conn, session_id)
    [message] = session["messages"]
    for row, name in ((leased, "key"), (detail, "key"), (stored, "headers"), (message, "content")):
        with pytest.raises(KeyError):
            row[name]
    with pytest.raises(KeyError):
        repo.as_observation(stored)
    assert TEST_SEALER.open_row(leased)["key"] == TEST_SEALER.open_row(detail)["key"] == "a"
    assert TEST_SEALER.open_row(stored)["headers"] == {"etag": "e"}
    assert TEST_SEALER.open_row(message)["content"] == "why a?"
    sealed = {"key", "key_enc", "headers", "headers_enc", "content", "sealed", "tool_calls"}
    views = [
        shown(detail),
        *map(shown, detail["observations"]),
        shown(stored),
        shown(message),
        shown(session),
    ]
    assert all(not sealed & view.keys() for view in views)
    assert "key" not in shown(TEST_SEALER.open_row(leased)) and shown(detail)["key_masked"] == "m-a"
