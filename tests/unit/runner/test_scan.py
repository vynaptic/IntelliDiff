"""The table scan's sealed, numbered checkpoints, the
cut its maxKeys stop records, a scan stopped before the end refused a
sign-off, the guard on each side's key order and what the worker does with
a pass an engine's error ends: resumed, stalled, paused or failed."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth import approvals
from deltasleuth.connectors import tables
from deltasleuth.connectors.base import UnsafeTargetError, utc_now_iso
from deltasleuth.connectors.postgres import engine as postgresql
from deltasleuth.connectors.tables import duck
from deltasleuth.core.types import Side
from deltasleuth.runner import run_worker
from deltasleuth.runner import scan as scanning
from deltasleuth.runner.compare import LiveReader
from deltasleuth.runner.cycle import finish_cycle
from deltasleuth.runner.runs import AUTH_PAUSE, cancel_run, create_run, pause_run, stop_run
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import Harness, OpenHarness, until
from .test_approvals import OIDC, approve

pytestmark = pytest.mark.db

ROWS = 35  # more than 3 x BATCH once BATCH is 10
SEED = f"""
    CREATE TABLE users (userid int PRIMARY KEY, email text);
    INSERT INTO users SELECT g, 'u' || g FROM generate_series(1, {ROWS}) g;
"""  # noqa: S608 (ROWS is a constant)
CHANGES = "UPDATE users SET email = 'x' WHERE userid IN (7, 23);"  # two rows differ
SPEC = {
    "apiVersion": "deltasleuth/v1",
    "kind": "ComparisonSpec",
    "metadata": {"name": "users-tables", "version": "1.0.0", "owner": "tests"},
    "normalizerVersion": "1.1.0",
    "key": {"name": "userid", "type": "string", "class": "business_code"},
    "sides": {
        side: {"role": role, "target": f"db-{side}", "table": {"name": "users", "keyColumns": ["userid"]}}
        for side, role in (("a", "legacy"), ("b", "new"))
    },
    "compare": {},
    "recheck": {"delay": "0s", "times": 1},
    "security": {"unlisted": "tokenize"},
}


@pytest.fixture
def databases(pg_server: str) -> Iterator[Callable[[str], str]]:
    """``create(seed)``: a fresh database seeded with ``seed``; its DSN."""
    made: list[str] = []

    def create(seed: str) -> str:
        db = f"t_scan_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(pg_server, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{db}"')
        made.append(db)
        dsn = pg_server.rsplit("/", 1)[0] + f"/{db}"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(seed)  # type: ignore[arg-type]
        return dsn

    yield create
    with psycopg.connect(pg_server, autocommit=True) as conn:
        for db in made:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


async def test_a_scan_over_more_than_three_batches_completes_in_one_snapshot(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scanning, "BATCH", 10)
    monkeypatch.setattr(postgresql, "BATCH", 10)  # and as many rows a fetch
    targets = {}
    for side, seed in (("a", SEED), ("b", SEED + CHANGES)):
        (tmp_path / f"dsn_{side}").write_text(databases(seed))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    snapshots: list[str] = []
    real_snapshot = scanning._snapshot

    def snapshot(side: str, *args: Any) -> Any:
        snapshots.append(side)
        return real_snapshot(side, *args)

    saves: list[tuple[int, Any, int]] = []
    real_save = repo.save_scan

    async def save(conn: repo.Conn, run_id: str, cycle: int, state: Any, keys: Any, saved: int) -> bool:
        saves.append((saved, state["after"], len(keys)))
        return await real_save(conn, run_id, cycle, state, keys, saved)

    monkeypatch.setattr(scanning, "_snapshot", snapshot)
    monkeypatch.setattr(repo, "save_scan", save)
    async with open_harness(json.dumps({"targets": targets})) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
        assert await scanning.scan(h.ctx, run_id) is None
        run = await h.run(run_id)
        async with h.ctx.pool.connection() as conn:
            rows, _ = await repo.list_keys(conn, run_id)
            sealer = await h.ctx.sealer(conn, run_id)
    assert snapshots == ["a", "b"]  # one snapshot a side: no checkpoint made the scan start again
    assert [saved for saved, _, _ in saves] == [0, 1, 2, 3]
    assert (
        all(set(after) == {"enc"} for _, after, _ in saves[:3]) and saves[3][1] is None
    )  # sealed, then done
    assert sum(found for _, _, found in saves) == 2
    state = run["scan"]
    assert (state["done"], state["checkpoint"], state["after"]) == (True, 4, None)
    assert "server_a" not in state and "server_b" not in state  # a PostgreSQL pair's state is what it was
    assert (state["rows_a"], state["rows_b"], state["matched"], state["candidates"]) == (
        ROWS,
        ROWS,
        ROWS - 2,
        2,
    )
    assert sorted(sealer.open_row(row)["key"] for row in rows) == ["23", "7"]
    snapshots.clear()
    async with open_harness(json.dumps({"targets": targets})) as h:
        # A sweep that read the run before its scan finished starts a second scanner: it scans nothing
        # again, where it once found the keys again and failed the run on their primary key.
        assert await scanning.scan(h.ctx, run_id) is None
    assert snapshots == [] and len(saves) == 4


async def test_a_scan_stopped_by_max_keys_records_its_cut_until_the_next_cycle_scans_again(
    open_harness: OpenHarness, databases: Callable[[str], str], tmp_path: Path
) -> None:
    changes = "UPDATE users SET email = 'x' WHERE userid IN (7, 23, 30);"  # three rows differ
    targets = {}
    for side, seed in (("a", SEED), ("b", SEED + changes)):
        (tmp_path / f"dsn_{side}").write_text(databases(seed))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    spec = SPEC | {"execution": {"cycleInterval": "1h", "stop": {"maxKeys": 2, "maxCycles": 2}}}
    async with open_harness(json.dumps({"targets": targets})) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(spec)), "soak", who=TESTER)
        assert await scanning.scan(h.ctx, run_id) is None
        cut = await h.run(run_id)
        async with h.ctx.pool.connection() as conn:
            await repo.cancel_keys(conn, run_id)  # its keys are done
        await finish_cycle(h.ctx, run_id, who=TESTER)
        rescanning = await h.run(run_id)
        async with h.ctx.pool.connection() as conn:
            ended = await repo.get_cycle(conn, run_id, 1)
    assert (cut["stop_reason"], cut["scan"]["candidates"], cut["keys_cut"]) == (
        "stop_condition:maxKeys", 2, {"taken": 2},
    )  # fmt: skip
    assert ended["keys_cut"] == {"taken": 2}  # type: ignore[index]
    assert (rescanning["cycle"], rescanning["scan"]["done"], rescanning["keys_cut"]) == (2, False, None)


@pytest.mark.parametrize("stop", ["until", "maxDuration", "cancel"])
async def test_a_scan_stopped_before_the_end_is_not_signed_off(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    monkeypatch.setattr(scanning, "BATCH", 10)
    targets = {}
    for side in "ab":  # the same rows on both sides
        (tmp_path / f"dsn_{side}").write_text(databases(SEED))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    conditions = {"until": {"until": "2000-01-01T00:00:00Z"}, "maxDuration": {"maxDuration": "0s"}}
    spec = SPEC | ({"execution": {"stop": conditions[stop]}} if stop in conditions else {})
    save_scan = repo.save_scan

    async def cancelled_meanwhile(conn: repo.Conn, run_id: str, cycle: int, *args: Any) -> bool:
        if args[-1] == 1:  # between the first checkpoint and the second
            await cancel_run(h.ctx, run_id, who=TESTER)
        return await save_scan(conn, run_id, cycle, *args)

    async with open_harness(json.dumps({"targets": targets}), auth=OIDC) as h:
        spec_id = await h.spec(json.dumps(spec))
        await approve(h, spec_id)
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        if stop == "cancel":
            monkeypatch.setattr(repo, "save_scan", cancelled_meanwhile)
            assert await scanning.scan(h.ctx, run_id) is None
        else:
            reason = await scanning.scan(h.ctx, run_id)
            assert reason == f"stop_condition:{stop}"
            await stop_run(h.ctx, run_id, reason, who=TESTER)  # as the worker does
            await finish_cycle(h.ctx, run_id, who=TESTER)
        run = await h.run(run_id)
        async with h.ctx.pool.connection() as conn:
            problems = await approvals.signoff_problems(conn, run, 1, await h.ctx.spec(conn, spec_id))
    assert run["status"] == ("cancelled" if stop == "cancel" else "completed_with_issues")
    assert problems == [
        f"cycle 1 of run {run_id} scanned only part of its tables (rows_a 10, rows_b 10 read; the scan"
        " stopped before the end)"
    ]


async def test_a_new_store_s_first_cycle_end_analyses_its_tables(
    open_harness: OpenHarness, databases: Callable[[str], str], tmp_path: Path
) -> None:
    """A store whose tables were never analysed gets them analysed at its first cycle end, before summarise
    pages their rows (``repo.analyse_new``), also once a VACUUM counted their rows
    (reltuples) without statistics, as autovacuum's insert-driven VACUUM does; autovacuum is off here."""
    targets = {}
    for side, seed in (("a", SEED), ("b", SEED + CHANGES)):
        (tmp_path / f"dsn_{side}").write_text(databases(seed))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    tables = ("run_keys", "results", "findings", "observations")
    counted = "SELECT count(DISTINCT tablename) FROM pg_stats WHERE schemaname = 'ds' AND tablename = ANY(%s)"
    async with open_harness(json.dumps({"targets": targets})) as h:
        async with h.ctx.pool.connection() as conn:
            for table in tables:
                await conn.execute(f"ALTER TABLE ds.{table} SET (autovacuum_enabled = off)")  # type: ignore[arg-type]
        run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
        assert await scanning.scan(h.ctx, run_id) is None
        async with h.ctx.pool.connection() as conn:
            await conn.set_autocommit(True)
            # Rows counted (reltuples) without statistics, as autovacuum's insert-driven VACUUM leaves them
            await conn.execute("VACUUM ds.run_keys, ds.results, ds.findings, ds.observations")
            before = (await (await conn.execute(counted, (list(tables),))).fetchone())[0]  # type: ignore[index]
        run = await h.finish(run_id)
        async with h.ctx.pool.connection() as conn:
            after = (await (await conn.execute(counted, (list(tables),))).fetchone())[0]  # type: ignore[index]
    assert run["status"] == "completed" and run["counters"]["by_outcome"]["MISMATCH"] == 2
    assert (before, after) == (0, 4)  # no statistics, then every table's


# --- the engine seam: the key order guard and the passes an engine's error ends ----------------------

Batches = AsyncIterator[list[tuple[Any, ...]]]
Play = Callable[[Batches, int, Sequence[bytes] | None], Batches]


def _targets(databases: Callable[[str], str], tmp_path: Path) -> str:
    """The targets file of two databases of SEED, side b's with CHANGES."""
    targets = {}
    for side, seed in (("a", SEED), ("b", SEED + CHANGES)):
        (tmp_path / f"dsn_{side}").write_text(databases(seed))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    return json.dumps({"targets": targets})


def _played(monkeypatch: pytest.MonkeyPatch, play: Play) -> list[int]:
    """PostgreSQL's engine with the stream of side a of every pass played through ``play(batches, pass
    number, bound)``, in fetches and checkpoints of 10 rows; the passes opened so far, by number."""

    class Played(postgresql.PostgreSQL):
        async def scan(self, conn: Any, side: tables.Plan, after: Sequence[bytes] | None) -> Batches:
            opened.append(len(opened) // 2)  # a pass opens side a, then side b
            batches = await super().scan(conn, side, after)
            return play(batches, opened[-1], after) if len(opened) % 2 else batches

    opened: list[int] = []
    monkeypatch.setattr(scanning, "BATCH", 10)
    monkeypatch.setattr(postgresql, "BATCH", 10)
    monkeypatch.setitem(tables.ENGINES, "postgresql", Played())
    return opened


def _ending(error: Exception, passes: int, *, fetches: int) -> Play:
    """Side a's stream ends on ``error`` after ``fetches`` fetches, in each of the first ``passes``. The
    merge looks one row ahead, so two fetches of 10 rows merge 19 keys: one checkpoint."""

    async def play(batches: Batches, number: int, after: Sequence[bytes] | None) -> Batches:
        fetched = 0
        async for batch in batches:
            if number < passes and fetched == fetches:
                raise error
            fetched += 1
            yield batch
        if number < passes:
            raise error

    return play


async def _scanned(h: Harness) -> repo.Row:
    """A scan run of the two tables, driven by a worker to its end, or to its pause."""
    run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
    return await h.finish(run_id, (*repo.TERMINAL, "paused"))


async def test_keys_out_of_order_fail_the_run(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def swapped(batches: Batches, number: int, after: Sequence[bytes] | None) -> Batches:
        async for batch in batches:  # each fetch's second row before its first
            yield [batch[1], batch[0], *batch[2:]]

    opened = _played(monkeypatch, swapped)
    async with open_harness(_targets(databases, tmp_path)) as h:
        run = await _scanned(h)
    assert (run["status"], run["error"]) == ("failed", "KeysOutOfOrder")  # on the first pass
    assert opened == [0, 0] and (run["scan"]["matched"], run["scan"]["candidates"]) == (0, 0)


@pytest.mark.parametrize("first", ["below the bound", "the bound itself"])
async def test_first_key_must_follow_the_resume_bound(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    class Unbounded(postgresql.PostgreSQL):  # its statement forgets the bound, or holds >= for >
        async def scan(self, conn: Any, side: tables.Plan, after: Sequence[bytes] | None) -> Batches:
            batches = await super().scan(conn, side, None)
            return self._from(batches, after if first == "the bound itself" else None)

        async def _from(self, batches: Batches, bound: Sequence[bytes] | None) -> Batches:
            async for batch in batches:
                yield [row for row in batch if bound is None or row[0].encode() >= bound[0]]

    monkeypatch.setattr(scanning, "BATCH", 10)
    save = scanning._save

    async def saved_then_lost(*args: Any, **options: Any) -> bool:
        await save(*args, **options)
        raise psycopg.OperationalError("the connection was lost")

    async with open_harness(_targets(databases, tmp_path)) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
        with monkeypatch.context() as lost, pytest.raises(psycopg.OperationalError):
            lost.setattr(scanning, "_save", saved_then_lost)
            await scanning.scan(h.ctx, run_id)  # its first checkpoint is saved: the next pass has a bound
        monkeypatch.setitem(tables.ENGINES, "postgresql", Unbounded())
        with pytest.raises(tables.KeysOutOfOrder) as exc:
            await scanning.scan(h.ctx, run_id)
        scan = (await h.run(run_id))["scan"]
    assert str(exc.value) == "side a: the population's keys came out of order"  # it names no key
    assert (scan["checkpoint"], scan["rows_a"], scan["done"]) == (1, 10, False)  # nothing merged again


async def test_snapshot_lost_with_progress_resumes(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Three passes in a row end on the code, each after a checkpoint: none is stalled.
    opened = _played(monkeypatch, _ending(tables.SnapshotLost("ORA-01555"), 3, fetches=2))
    async with open_harness(_targets(databases, tmp_path)) as h:
        run = await _scanned(h)
    scan = run["scan"]
    assert (run["status"], run["counters"]["by_outcome"]["MISMATCH"]) == ("completed", 2)
    assert opened == [0, 0, 1, 1, 2, 2, 3, 3] and (scan["snapshots_a"], scan["snapshots_b"]) == (4, 4)
    assert (scan["rows_a"], scan["rows_b"], scan["matched"], scan["candidates"]) == (ROWS, ROWS, ROWS - 2, 2)


@pytest.mark.parametrize(
    "error", [tables.SnapshotLost("ORA-01555"), tables.TargetRefused("ORA-00600")], ids=["F2", "F4"]
)
async def test_three_stalled_passes_fail(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: tables.TableError,
) -> None:
    # A pass that saved a checkpoint, then three that end on the code before their first: the third fails.
    # The pass after the checkpoint starts at the next sweep; each stalled one is followed by a lease
    # period, so a server busy for a moment (MySQL 1226 before it was an outage's code) is not met three
    # times in three sweeps.
    fetches = iter([2, 0, 0, 0])
    starts: list[float] = []

    def play(batches: Batches, number: int, after: Sequence[bytes] | None) -> Batches:
        starts.append(time.monotonic())
        return _ending(error, number + 1, fetches=next(fetches))(batches, number, after)

    opened = _played(monkeypatch, play)
    async with open_harness(_targets(databases, tmp_path), lease_seconds=1) as h:
        run = await _scanned(h)
    assert (run["status"], run["error"]) == ("failed", f"{type(error).__name__}: {error}")
    assert opened == [0, 0, 1, 1, 2, 2, 3, 3] and run["scan"]["checkpoint"] == 1
    waits = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert waits[0] < 0.9 and min(waits[1:]) >= 1  # the lease period of this harness


async def test_unreachable_passes_never_fail(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Four passes in a row end before their first checkpoint: each is waited out a lease period.
    opened = _played(monkeypatch, _ending(tables.TargetUnreachable("ORA-03113"), 4, fetches=0))
    with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.worker"):
        async with open_harness(_targets(databases, tmp_path), lease_seconds=1) as h:
            run = await _scanned(h)
    assert run["status"] == "completed" and opened == [n for n in range(5) for _ in "ab"]
    paused = f"scan of run {run['run_id']} paused: ORA-03113"
    assert [r.getMessage() for r in caplog.records].count(paused) == 4


async def test_temp_space_fails_the_run(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _played(monkeypatch, _ending(tables.TargetTempSpace("ORA-01652"), 9, fetches=2))
    async with open_harness(_targets(databases, tmp_path)) as h:
        run = await _scanned(h)
    assert (run["status"], run["error"]) == ("failed", "TargetTempSpace: ORA-01652")
    assert opened == [0, 0] and run["scan"]["checkpoint"] == 1  # no resume, though it saved a checkpoint


async def test_refused_login_pauses_the_scan(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _played(monkeypatch, _ending(tables.TargetAuthRefused("ORA-01017"), 9, fetches=0))
    async with open_harness(_targets(databases, tmp_path)) as h:
        run = await _scanned(h)
        async with h.ctx.pool.connection() as conn:
            rows, _ = await repo.list_audit(conn, action="paused", obj=run["run_id"])
    assert (run["status"], run["stop_reason"], run["error"]) == ("paused", AUTH_PAUSE, None)
    assert [r["detail"] for r in rows] == [{"reason": AUTH_PAUSE}]
    assert opened == [0, 0]  # the login is not tried again


async def test_engine_changed_fails(
    open_harness: OpenHarness, databases: Callable[[str], str], tmp_path: Path
) -> None:
    # The run was planned for another engine than its target declares now: no statement of the plan is
    # sent to it, by a scan or by a per-key read.
    async with open_harness(_targets(databases, tmp_path)) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
        async with h.ctx.pool.connection() as conn:
            planned = "jsonb_set(params, '{tables,b,engine}', '\"mysql\"')"
            await conn.execute(f"UPDATE ds.runs SET params = {planned} WHERE run_id = %s", (run_id,))  # noqa: S608
        run = await h.finish(run_id)
        reader = LiveReader(h.ctx, await _spec(h, run), run["params"]["tables"])
        with pytest.raises(tables.EngineChanged):
            await reader.fetch("7", Side.B, run_id=run_id, call_seq=0)
        await reader.aclose()
    changed = "side b: target db-b is postgresql now; this run was planned for mysql"
    assert (run["status"], run["error"]) == ("failed", f"EngineChanged: {changed}")
    assert run["scan"]["checkpoint"] == 0


async def _spec(h: Harness, run: repo.Row) -> Any:
    async with h.ctx.pool.connection() as conn:
        return await h.ctx.spec(conn, run["spec_id"], live=True)


async def test_unsafe_target_fails_the_scan(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = UnsafeTargetError("10.1.2.3 is a private address: set allow_private_network on the target")
    opened = _played(monkeypatch, _ending(refused, 9, fetches=0))
    async with open_harness(_targets(databases, tmp_path)) as h:
        run = await _scanned(h)
    assert (run["status"], run["error"]) == ("failed", f"UnsafeTargetError: {refused}")  # an address
    assert opened == [0, 0]  # the guard's refusal is not waited out


@pytest.mark.parametrize("stop", [cancel_run, pause_run], ids=["cancel", "pause"])
async def test_a_scan_ends_when_its_run_no_longer_runs(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: Callable[..., Any],
) -> None:
    # Side a's first fetch never comes, as while a server sorts: the scan once learnt that its run was
    # cancelled or paused only at its next checkpoint, so its statements ran on to their end.
    waiting, ended = asyncio.Event(), asyncio.Event()

    async def sorting(batches: Batches, number: int, after: Sequence[bytes] | None) -> Batches:
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()  # the scan's task was cancelled: its sessions close as it unwinds
        yield []

    opened = _played(monkeypatch, sorting)
    async with open_harness(_targets(databases, tmp_path)) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(SPEC)), "batch", who=TESTER)
        halt = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=halt))
        try:
            await asyncio.wait_for(waiting.wait(), 30)
            await stop(h.ctx, run_id, who=TESTER)
            await asyncio.wait_for(ended.wait(), 5)
            run = await h.wait(run_id, ("cancelled", "paused"))
            await asyncio.sleep(0.3)  # several sweeps: no scan starts for a run that does not run
        finally:
            halt.set()
            await worker
    assert opened == [0, 0] and (run["scan"]["checkpoint"], run["error"]) == (0, None)


MIXED = """
    CREATE TABLE users (userid text PRIMARY KEY, email text);
    INSERT INTO users SELECT g::text, 'u' || g FROM generate_series(1, 5) g;
"""
SESSIONS = "SELECT state FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"


@pytest.mark.parametrize("database", ["a", "b"])
async def test_a_pass_takes_the_one_sort_before_a_database_snapshot_opens(
    open_harness: OpenHarness,
    databases: Callable[[str], str],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    database: str,
) -> None:
    """A pass of a database against files takes the file reader's one sort before the
    database side's snapshot opens, whichever side the database is. While it waits for another run's sort
    the database has no session of it (none idle in a transaction, its horizon pinned for as long as that
    sort lasts: a server that ends such sessions failed the run), a row committed meanwhile is read, and
    first_at, which the census prints as the snapshot's time, is when the snapshots opened, not when the
    file side's own sort returned."""
    exports = tmp_path_factory.mktemp("exports").resolve()  # outside the harness's own directories
    (exports / "users.csv").write_text("userid|email\n" + "".join(f"{n}|u{n}\n" for n in range(1, 7)))
    dsn = databases(MIXED)
    (tmp_path / "dsn_t").write_text(dsn)
    targets = {
        "db": {"dsn_ref": f"file:{tmp_path / 'dsn_t'}", "rate_per_s": 1000},
        "fx": {"engine": "files", "root": str(exports), "settle_s": 0, "allow_writable": True},
    }
    table = {"name": "users", "keyColumns": ["userid"]}
    dialect = {"columns": ["userid", "email"], "delimiter": "|", "header": True}
    files = {
        "target": "fx",
        "table": table | {"file": {"path": "users.csv", "format": "csv", "csv": dialect}},
    }
    a, b = ({"target": "db", "table": table}, files)[:: 1 if database == "a" else -1]
    doc = SPEC | {"sides": {"a": {"role": "legacy"} | a, "b": {"role": "new"} | b}}
    sorting: list[str] = []
    real = duck.Files.scan

    async def scan(self: duck.Files, *args: Any) -> Any:
        sorting.append(utc_now_iso())
        await asyncio.sleep(0.3)  # the file side's own sort
        return await real(self, *args)

    monkeypatch.setattr(duck.Files, "scan", scan)
    async with open_harness(json.dumps({"targets": targets}), files={"roots_under": str(exports)}) as h:
        run_id = await create_run(h.ctx, await h.spec(json.dumps(doc)), "batch", who=TESTER)
        with caplog.at_level(logging.WARNING, logger="deltasleuth.files"):
            async with duck.SORT.held():  # another run's scan pass, census or frame is sorting
                scanner = asyncio.create_task(scanning.scan(h.ctx, run_id))
                await until(lambda: "a sort of a file side waits for another" in caplog.messages)
                with psycopg.connect(dsn, autocommit=True) as conn:
                    held = conn.execute(SESSIONS).fetchall()
                    conn.execute("INSERT INTO users VALUES ('6', 'u6')")
                waited = utc_now_iso()
            await scanner
        state = (await h.run(run_id))["scan"]
    assert held == [] and len(sorting) == 1
    assert (state["rows_a"], state["rows_b"], state["matched"], state["done"]) == (6, 6, 6, True)
    assert waited <= state["first_at"] <= sorting[0]
