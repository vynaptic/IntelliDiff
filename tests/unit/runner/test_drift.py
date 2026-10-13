"""The schema guard on PostgreSQL (docs/mvp/PRECHECKS.md §7, §13): what it records of a run's tables and
where it looks (V1 to V5), what pauses a run and what only warns, the two modes and what a resume accepts,
the basis of a new run, a run from before 016, a look that cannot be made, and its audit rows; and on MySQL
and Oracle, a column dropped under per-key reads (V5: MySQL 1054, ORA-00904)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth import approvals
from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors import tables
from deltasleuth.connectors.postgres import engine as postgresql
from deltasleuth.connectors.tables.engine import Column, Relation
from deltasleuth.core import precheck
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.spec import compile_spec
from deltasleuth.runner import census, drift
from deltasleuth.runner import scan as scanning
from deltasleuth.runner.runs import cancel_run, create_run, resume_run
from deltasleuth.runner.schedule import RETRY_S, RETRY_SLOW_S, Retry
from deltasleuth.store import repo
from tests.conftest import TESTER, MySQLServer, OracleServer

from .conftest import Harness, OpenHarness
from .prechecking import table_spec
from .scheduling import DAY, MINUTE, fire, minute, missed, scheduled, started
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
SECOND = timedelta(seconds=1)

SEED = """
    CREATE TABLE customers (
        id int PRIMARY KEY, email text NOT NULL, amount numeric(12,2), legacy_flag boolean);
    INSERT INTO customers VALUES (1, 'a@x', 10.50, true), (2, 'b@x', 20.00, false), (3, 'c@x', 5.25, NULL);
"""
UNCHANGED = (
    "no change observed against the run's plan at its creation and each cycle's start (table sides only; a"
    " view's own tables, constraints and defaults are not read)"
)
FIRST = "no earlier run of this version recorded its tables' shapes: this run records them"
SOAK = {"concurrency": 2, "cycleInterval": "0s", "stop": {"maxCycles": 9}}
ROLES = {"id": "key", "email": "compared", "amount": "compared", "legacy_flag": "compared"}


@dataclass
class Pair:
    """Two databases holding ``customers``, read by the targets db-a and db-b, and a harness on them."""

    h: Harness
    dsn: dict[str, str]
    files: dict[str, Path]

    def alter(self, side: str, statement: str) -> None:
        with psycopg.connect(self.dsn[side], autocommit=True) as conn:
            conn.execute(statement)  # type: ignore[arg-type]

    async def spec(self, **members: Any) -> int:
        return await self.h.spec(table_spec("customers", **members))

    async def audited(self, run_id: str, *actions: str) -> list[tuple[str, str, dict[str, Any]]]:
        """The run's audit rows of ``actions`` (every one without any) in order: action, actor, detail
        (without what a row says of the CLI's principal)."""
        async with self.h.ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT action, actor, detail - 'asserted' - 'os_account' FROM ds.audit_log"
                " WHERE object = %s ORDER BY id",
                (run_id,),
            )
            return [found for found in await cur.fetchall() if not actions or found[0] in actions]

    async def next_cycle(self, run_id: str) -> None:
        """Open the run's next cycle, as the end of the one before does."""
        async with self.h.ctx.pool.connection() as conn:
            now = await (await conn.execute("SELECT now()")).fetchone()
            await repo.start_next_cycle(conn, run_id, now[0], {}, scanning.new_scan())  # type: ignore[index]

    async def before_016(self, run_id: str) -> None:
        """The run as one planned before the guard existed: nothing is held of its tables."""
        async with self.h.ctx.pool.connection() as conn:
            await conn.execute("UPDATE ds.runs SET guard = NULL WHERE run_id = %s", (run_id,))


@pytest.fixture
async def pair(open_harness: OpenHarness, databases: Callable[[str], str], tmp_path: Path) -> Any:  # noqa: F811
    dsn, files, targets = {}, {}, {}
    for side in "ab":
        dsn[side], files[side] = databases(SEED), tmp_path / f"dsn_{side}"
        files[side].write_text(dsn[side])
        targets[f"db-{side}"] = {"dsn_ref": f"file:{files[side]}", "rate_per_s": 1000}
    async with open_harness(json.dumps({"targets": targets}), readiness={}, lease_seconds=1) as h:
        yield Pair(h, dsn, files)


def logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the schema guard logged: ids, cycles, counts and an exception's class."""
    return [record.getMessage() for record in caplog.records if record.name == "deltasleuth.guard"]


def drifts(rows: list[tuple[str, str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [detail for action, _, detail in rows if action == "schema.drift"]


def kinds(found: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(change["kind"], change["column"]) for change in found]


async def paused_seq(pair: Pair, run_id: str) -> int:
    """The chain seq of the run's last ``paused`` row, which the ``schema.drift`` of its trip names."""
    async with pair.h.ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT c.seq FROM ds.audit_chain c JOIN ds.audit_log l USING (id)"
            " WHERE l.object = %s AND l.action = 'paused' ORDER BY c.seq DESC LIMIT 1",
            (run_id,),
        )
        return (await cur.fetchone())[0]  # type: ignore[index]


# -- V1: creation ------------------------------------------------------------------------------------------


async def test_a_first_run_records_its_tables_beside_its_plan_and_has_no_basis(pair: Pair) -> None:
    """V1: the guard's record is ``ds.runs.guard``, outside ``params``: the plan a pack's facts hold and
    ``create_run`` hashes is what it was, and nothing is audited of a run whose tables did not change."""
    run_id = await create_run(pair.h.ctx, await pair.spec(), "batch", who=TESTER)
    run = await pair.h.run(run_id)
    guard = run["guard"]
    assert (guard["format"], guard["mode"], guard["baseline"], guard["basis"]) == (1, "finding", True, None)
    assert (guard["adopted"], guard["since"], guard["accepted"]) == (None, None, None)
    assert guard["checked"]["cycle"] == 1 and guard["checked"]["where"] == "creation"
    for side in "ab":
        held = guard[side]
        assert (held["relation"], held["roles"], held["common"]) == ("public.customers", ROLES, True)
        assert held["seen"] is None and held["changes"] == []  # as planned: its shape is held once
        assert held["planned"]["columns"]["amount"] == ["numeric(12,2)", "decimal", True]
        assert (held["planned"]["key"], held["planned"]["kind"]) == (["id"], "table")
        assert held["sha256"] == held["seen_sha256"] == precheck.sha256(held["planned"])
    assert guard["a"]["sha256"] == guard["b"]["sha256"]  # two tables of one shape
    assert "guard" not in run["params"] and "guard" not in json.dumps(run["params"])
    [(_, _, created)] = await pair.audited(run_id, "create_run")
    assert created["plan_sha256"] == sha256_hex(approvals.plan(run["params"]))
    assert [action for action, _, _ in await pair.audited(run_id)] == ["create_run"]
    assert drift.lines(run) == [UNCHANGED, FIRST] and drift.warnings(run) == []
    told = drift.told(run)
    assert (told["guarded"], told["mode"], told["basis"], told["paused"], told["accepts"]) == (
        True, "finding", None, False, False,
    )  # fmt: skip
    assert told["checked"]["where"] == "creation" and told["lines"] == [UNCHANGED, FIRST]


async def test_a_run_without_a_table_side_is_not_guarded(harness: Harness) -> None:
    """D14: service sides are not guarded."""
    from .conftest import spec_text

    run_id = await create_run(harness.ctx, await harness.spec(spec_text()), "batch", who=TESTER)
    run = await harness.run(run_id)
    assert run["guard"] is None and not drift.due(run) and drift.lines(run) == []
    assert drift.told(run) == {
        "guarded": False, "mode": None, "basis": None, "checked": None, "paused": False, "accepts": False,
        "lines": [],
    }  # fmt: skip


async def test_narrowing_is_created_paused_and_a_resume_accepts_it(pair: Pair) -> None:
    """D10 under ``finding``: a new run that would not compare what its predecessor compared is created
    paused, before any table row is read; its audit rows come last and in order; a resume accepts the
    tables as they are, and from then on the run is the version's baseline."""
    ctx, spec_id = pair.h.ctx, await pair.spec()
    first = await create_run(ctx, spec_id, "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    pair.alter("a", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
    run = await pair.h.run(run_id)
    assert (run["status"], run["stop_reason"], run["started_at"]) == ("paused", drift.SCHEMA_PAUSE, None)
    assert run["scan"]["rows_a"] == 0 and run["scan"]["snapshots_a"] == 0  # no table row was read
    guard = run["guard"]
    assert (guard["baseline"], guard["basis"]["run_id"], guard["basis"]["predates"]) == (False, first, False)
    lost = "column 'amount' was compared by run " + first + " of this version and is not compared now"
    gained = f"side a gained column 'loyalty_tier' since run {first}: this version does not compare it"
    paused = (
        f"Paused by the schema guard before any table row was read: {lost}. Resume to accept the tables as"
        " they are, or cancel."
    )
    gone = "side b: column 'amount', which this run does not read, is gone"
    assert drift.lines(run) == [paused, gained]
    assert drift.warnings(run) == [paused, gained] and gone not in drift.lines(run)  # it was compared: lost
    told = drift.told(run)
    assert (told["paused"], told["accepts"], told["basis"]) == (True, True, first)
    rows = await pair.audited(run_id)
    assert [action for action, _, _ in rows] == ["create_run", "paused", "schema.drift"]
    assert {actor for _, actor, _ in rows} == {TESTER.actor}  # the caller's, at V1
    hashes = {side: guard[side]["sha256"] for side in "ab"}
    assert rows[1][2] == {"reason": drift.SCHEMA_PAUSE, "schema": hashes}
    was = (await pair.h.run(first))["guard"]
    assert rows[2][2] == {
        "cycle": 1, "where": "creation", "mode": "finding", "checked": True, "paused": True,
        "paused_seq": await paused_seq(pair, run_id), "basis": first, "error_type": None,
        "sides": {
            "a": {"breaking": 1, "other": 1, "kinds": {"compared_lost": 1, "column_added": 1},
                  "from": was["a"]["sha256"], "to": hashes["a"]},
            "b": {"breaking": 1, "other": 0, "kinds": {"compared_lost": 1}, "from": was["b"]["sha256"],
                  "to": hashes["b"]},
        },
    }  # fmt: skip
    assert "amount" not in json.dumps(rows[1][2]) + json.dumps(rows[2][2])  # no name leaves the guard

    assert await resume_run(ctx, run_id, who=TESTER) == "running"
    run = await pair.h.run(run_id)
    guard = run["guard"]
    assert (run["stop_reason"], guard["baseline"]) == (None, True) and run["started_at"] is not None
    assert guard["accepted"] == hashes | {
        "by": TESTER.actor, "name": "tester", "at": guard["accepted"]["at"], "cycle": 1, "where": "creation",
    }  # fmt: skip
    [*_, (_, _, running)] = await pair.audited(run_id, "running")
    assert (running["reason"], running["schema"]) == (None, {"accepted": hashes})
    day = guard["accepted"]["at"][:10]
    accepted = [
        f"the change of side {side} observed at cycle 1 was accepted by tester on {day}; the run goes on"
        " with its plan"
        for side in "ab"
    ]
    assert drift.lines(run) == [lost, gained, *accepted] and not drift.told(run)["accepts"]
    assert await scanning.scan(ctx, run_id) is None  # an accepted shape never trips again
    # The accepted run is the baseline of the next one, whose tables are the same: nothing to say.
    third = await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER))
    assert (third["status"], third["guard"]["basis"]["run_id"]) == ("running", run_id)
    assert third["guard"]["basis"]["changes"] == {"a": [], "b": []} and drift.lines(third) == [UNCHANGED]


async def test_narrowing_is_refused_at_creation_when_the_spec_pauses_on_schema_drift(pair: Pair) -> None:
    """D10 under ``pause`` (E6): 422, nothing written. A run that could only be cancelled would hold its
    schedule in ``overlap``."""
    ctx, spec_id = pair.h.ctx, await pair.spec(schemaDrift={"mode": "pause"})
    first = await create_run(ctx, spec_id, "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    pair.alter("b", "ALTER TABLE customers DROP COLUMN legacy_flag")
    async with ctx.pool.connection() as conn:
        before = await (await conn.execute("SELECT count(*) FROM ds.audit_log")).fetchone()
    with pytest.raises(drift.Refused) as refused:
        await create_run(ctx, spec_id, "batch", who=TESTER)
    assert refused.value.status == 422 and isinstance(refused.value, ValueError)
    assert str(refused.value) == (
        f"spec {spec_id} (customers 1.0.0) pauses on schema drift and a run of it would not compare what run"
        f" {first} compared: column 'amount' was compared by run {first} of this version and is not compared"
        " now (and 1 more). Upload a new version to compare the tables as they are."
    )
    async with ctx.pool.connection() as conn:
        runs, _ = await repo.list_runs(conn)
        audited = await (await conn.execute("SELECT count(*) FROM ds.audit_log")).fetchone()
    assert [run["run_id"] for run in runs] == [first]  # no run row
    assert audited == before  # nothing was audited: no run, no pair seated


async def test_a_change_that_narrows_nothing_warns_at_creation_or_creates_paused_under_pause(
    pair: Pair,
) -> None:
    ctx = pair.h.ctx
    finding, pausing = await pair.spec(), await pair.spec(version="1.1.0", schemaDrift={"mode": "pause"})
    firsts = [await create_run(ctx, spec_id, "batch", who=TESTER) for spec_id in (finding, pausing)]
    for side in "ab":
        pair.alter(side, "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    pair.alter("b", "ALTER TABLE customers ALTER COLUMN email DROP NOT NULL")
    run = await pair.h.run(await create_run(ctx, finding, "batch", who=TESTER))
    gained = [
        f"side {side} gained column 'loyalty_tier' since run {{}}: it is compared from this run on"
        for side in "ab"
    ]
    nullable = "side b: column 'email' was NOT NULL and is nullable now"
    said = [gained[0].format(firsts[0]), nullable, gained[1].format(firsts[0])]
    assert run["status"] == "running" and run["guard"]["baseline"]
    assert drift.warnings(run) == said and drift.lines(run) == said
    [(_, actor, detail)] = await pair.audited(run["run_id"], "paused", "schema.drift")
    assert (actor, detail["paused"], detail["sides"]["b"]["other"]) == (TESTER.actor, False, 2)
    paused = await pair.h.run(await create_run(ctx, pausing, "batch", who=TESTER))
    assert (paused["status"], paused["stop_reason"]) == ("paused", drift.SCHEMA_PAUSE)
    assert drift.lines(paused)[0] == (
        f"Paused by the schema guard before any table row was read: {gained[0].format(firsts[1])}. This spec"
        " pauses on schema drift: resume to accept the tables as they are, or cancel."
    )
    assert drift.told(paused)["accepts"] and not paused["guard"]["baseline"]
    assert await resume_run(ctx, paused["run_id"], who=TESTER) == "running"
    assert (await pair.h.run(paused["run_id"]))["guard"]["baseline"]


async def test_cancelled_paused_run_is_no_basis(pair: Pair) -> None:
    """A run created paused and never accepted is no baseline: cancelling it hides no change from the
    next run, which is judged against the same run as it was."""
    ctx, spec_id = pair.h.ctx, await pair.spec()
    first = await create_run(ctx, spec_id, "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    second = await create_run(ctx, spec_id, "batch", who=TESTER)
    assert await cancel_run(ctx, second, who=TESTER) == "cancelled"
    third = await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER))
    assert (third["status"], third["stop_reason"]) == ("paused", drift.SCHEMA_PAUSE)
    assert third["guard"]["basis"]["run_id"] == first


async def test_pre_016_basis_judges_compared_lost(pair: Pair) -> None:
    """When no earlier run of the version recorded a shape, the newest one's stored plan is the basis:
    only the columns it compared are judged."""
    ctx, spec_id = pair.h.ctx, await pair.spec()
    first = await create_run(ctx, spec_id, "batch", who=TESTER)
    await pair.before_016(first)
    pair.alter("a", "ALTER TABLE customers ALTER COLUMN legacy_flag TYPE text")  # a class: not judged
    pair.alter("b", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    kept = await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER))
    predates = f"run {first} of this version recorded no shape of its tables at its creation: only its"
    predates += " compared columns were"
    assert kept["status"] == "running" and kept["guard"]["basis"] == {
        "run_id": first, "predates": True, "changes": {"a": [], "b": []},
    }  # fmt: skip
    assert drift.lines(kept) == [UNCHANGED, f"{predates} checked against this run"]
    await pair.before_016(kept["run_id"])
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    lost = await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER))
    assert lost["status"] == "paused" and lost["guard"]["basis"]["run_id"] == kept["run_id"]
    [_, (_, _, detail)] = await pair.audited(lost["run_id"], "paused", "schema.drift")
    assert detail["sides"]["a"] == {
        "breaking": 1, "other": 0, "kinds": {"compared_lost": 1}, "from": None,
        "to": lost["guard"]["a"]["sha256"],
    }  # fmt: skip


async def test_an_adopted_run_s_plan_is_the_basis_while_no_run_is_a_baseline(
    pair: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run from before 016 that the guard adopted is no baseline, nor is one whose look at its creation
    failed; while no earlier run of the version is one, the next run is judged by the newest stored plan's
    compared columns, so a narrowing at the upgrade is created paused whether a live run was looked at or
    not, and no line says that no earlier run recorded its tables."""
    ctx, spec_id = pair.h.ctx, await pair.spec()
    first = await create_run(ctx, spec_id, "batch", who=TESTER)
    await pair.before_016(first)
    soak = await create_run(ctx, spec_id, "batch", who=TESTER)
    await pair.before_016(soak)
    assert await scanning.scan(ctx, soak) is None  # V2 adopts it
    assert (await pair.h.run(soak))["guard"]["adopted"] == 1
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    run = await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER))
    guard = run["guard"]
    assert (run["status"], run["stop_reason"], guard["baseline"]) == ("paused", drift.SCHEMA_PAUSE, False)
    assert (guard["basis"]["run_id"], guard["basis"]["predates"]) == (soak, True)
    assert [kinds(guard["basis"]["changes"][side]) for side in "ab"] == [[("compared_lost", "amount")]] * 2
    lost = f"column 'amount' was compared by run {soak} of this version and is not compared now"
    assert drift.lines(run) == [
        f"Paused by the schema guard before any table row was read: {lost}. Resume to accept the tables as"
        " they are, or cancel.",
        f"run {soak} of this version recorded no shape of its tables at its creation: only its compared"
        " columns were checked against this run",
    ]
    # A first run whose look at its creation failed holds no shape either: its plan is the basis too.
    other = await pair.spec(version="1.1.0")

    def broken(*args: Any) -> Any:
        raise KeyError("a bug")

    with monkeypatch.context() as patched:
        patched.setattr(precheck, "roles", broken)
        faulted = await create_run(ctx, other, "batch", who=TESTER)
    assert (await pair.h.run(faulted))["guard"]["checked"]["error_type"] == "KeyError"
    pair.alter("b", "ALTER TABLE customers DROP COLUMN legacy_flag")
    run = await pair.h.run(await create_run(ctx, other, "batch", who=TESTER))
    assert (run["status"], run["guard"]["basis"]["run_id"]) == ("paused", faulted)
    assert kinds(run["guard"]["basis"]["changes"]["b"]) == [("compared_lost", "legacy_flag")]


async def test_a_cancelled_paused_run_after_runs_from_before_016_hides_nothing(pair: Pair) -> None:
    """A run created paused against the plan of a run from before 016, then cancelled, is no basis: the
    run after it is judged against the same plan, and created paused again."""
    ctx, spec_id = pair.h.ctx, await pair.spec()
    first = await create_run(ctx, spec_id, "batch", who=TESTER)
    await pair.before_016(first)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    second = await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER))
    assert (second["status"], second["guard"]["basis"]["run_id"]) == ("paused", first)
    assert await cancel_run(ctx, second["run_id"], who=TESTER) == "cancelled"
    third = await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER))
    assert (third["status"], third["stop_reason"], third["guard"]["baseline"]) == (
        "paused", drift.SCHEMA_PAUSE, False,
    )  # fmt: skip
    assert (third["guard"]["basis"]["run_id"], third["guard"]["basis"]["predates"]) == (first, True)
    assert FIRST not in drift.lines(third)


async def test_the_max_keys_reason_returns(pair: Pair) -> None:
    """A run capped at its creation and created paused gets its cap's reason back when it resumes."""
    ctx = pair.h.ctx
    execution = {"concurrency": 2, "stop": {"maxKeys": 2}}
    spec_id = await pair.spec(execution=execution)
    await create_run(ctx, spec_id, "batch", ["1", "2", "3"], who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    run_id = await create_run(ctx, spec_id, "batch", ["1", "2", "3"], who=TESTER)
    run = await pair.h.run(run_id)
    assert (run["status"], run["stop_reason"], run["keys_cut"]) == (
        "paused", drift.SCHEMA_PAUSE, {"resolved": 3, "taken": 2},
    )  # fmt: skip
    assert await resume_run(ctx, run_id, who=TESTER) == "running"
    assert (await pair.h.run(run_id))["stop_reason"] == "stop_condition:maxKeys"


async def test_a_one_shot_compare_warns_and_is_neither_paused_nor_a_basis(pair: Pair) -> None:
    from deltasleuth.runner.runs import compare_one

    ctx, spec_id = pair.h.ctx, await pair.spec()
    assert (await compare_one(ctx, spec_id, "1", who=TESTER)).attrs["warnings"] == []  # no basis yet
    first = await create_run(ctx, spec_id, "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    result = await compare_one(ctx, spec_id, "1", who=TESTER)
    assert result.attrs["warnings"] == [
        f"column 'amount' was compared by run {first} of this version and is not compared now"
    ]
    one_shot = await pair.h.run(result.attrs["run_id"])
    assert one_shot["status"] == "completed" and one_shot["guard"] is None
    async with ctx.pool.connection() as conn:
        basis = await repo.guard_basis(conn, spec_id, repo.new_run_id())
    assert basis is not None and basis["run_id"] == first  # never the one-shot run


async def test_a_schedule_s_firing_meets_the_schema_guard(pair: Pair) -> None:
    """docs/mvp/SCHEDULES.md §5: a firing the guard creates paused is a live run of its schedule, whose
    later times are ``overlap`` until a person resumes or cancels it, its audit rows in order after the
    firing's; one the guard refuses (E6) starts no run, and its time is ``not_started``."""
    ctx = pair.h.ctx
    finding, pausing = await pair.spec(), await pair.spec(version="1.1.0", schemaDrift={"mode": "pause"})
    for spec_id in (finding, pausing):
        await create_run(ctx, spec_id, "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    due = await minute(pair.h)
    nightly = await scheduled(pair.h, finding, due)
    assert await fire(pair.h, nightly) is None
    (fired,) = await started(pair.h, nightly)
    assert (fired["status"], fired["stop_reason"], fired["created_by"]) == (
        "paused", drift.SCHEMA_PAUSE, WORKER.actor,
    )  # fmt: skip
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT action, actor FROM ds.audit_log WHERE object = ANY(%s) ORDER BY id",
            ([fired["run_id"], nightly["schedule_id"]],),
        )
        rows = await cur.fetchall()
    assert rows == [
        ("schedule.create", TESTER.actor), ("create_run", WORKER.actor), ("schedule.fire", WORKER.actor),
        ("paused", WORKER.actor), ("schema.drift", WORKER.actor),
    ]  # fmt: skip
    assert await fire(pair.h, nightly, due + DAY + SECOND) == Retry(RETRY_S, 0, due + DAY)  # it is live
    assert await resume_run(ctx, fired["run_id"], who=TESTER) == "running"
    # Under pause the firing is refused: nothing is written but, once its tries are spent, the missed time.
    refused = await scheduled(pair.h, pausing, due, name="refused")
    assert await fire(pair.h, refused) == Retry(RETRY_SLOW_S, 1, due)
    assert await fire(pair.h, refused, due + 10 * MINUTE, tried=2) is None
    (detail,) = await missed(pair.h, refused)
    assert (detail["reason"], detail["error_type"], detail["run_id"]) == ("not_started", "Refused", None)
    assert await started(pair.h, refused) == [] and "amount" not in json.dumps(detail)


# -- V2 and V4: a scan pass, and a resume --------------------------------------------------------------------


async def test_a_dropped_compared_column_pauses_before_the_cursor_opens_and_resumes_once_restored(
    pair: Pair, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """V2 and V4: the pass ends before either side's scan statement is sent; the run is paused, not
    failed; a resume is refused while the change stands (U1) and goes on once the column is back."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(), "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    pair.alter("b", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    scans: list[str] = []
    real = postgresql.PostgreSQL.scan

    async def scan(self: Any, conn: Any, side: Any, after: Any) -> Any:
        scans.append(side["name"])
        return await real(self, conn, side, after)

    monkeypatch.setattr(postgresql.PostgreSQL, "scan", scan)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.guard"), pytest.raises(drift.Tripped):
        await scanning.scan(ctx, run_id)
    run = await pair.h.run(run_id)
    assert scans == [] and (run["status"], run["stop_reason"], run["error"]) == (
        "paused", drift.SCHEMA_PAUSE, None,
    )  # fmt: skip
    assert logged(caplog) == [f"schema guard: run {run_id} paused at cycle 1 (side b: 1 breaking, 1 other)"]
    gone = (
        "Paused by the schema guard at the start of cycle 1: side b: column 'amount' of public.customers is"
        " gone, or the role may no longer read it. Put it back and resume the run, or cancel it and start a"
        " new run, which plans the table as it is now."
    )
    gained = "side b gained column 'loyalty_tier' since this run was planned: this run does not compare it"
    assert drift.lines(run) == [gone, gained, FIRST]
    told = drift.told(run)
    assert (told["paused"], told["accepts"]) == (True, False)  # a resume would be refused, not accept
    rows = await pair.audited(run_id, "paused", "schema.drift")
    assert [(action, actor) for action, actor, _ in rows] == [
        ("paused", WORKER.actor), ("schema.drift", WORKER.actor),
    ]  # fmt: skip
    guard = run["guard"]
    assert rows[0][2]["schema"] == {"a": guard["a"]["sha256"], "b": guard["b"]["seen_sha256"]}
    assert rows[1][2]["sides"]["b"] == {
        "breaking": 1, "other": 1, "kinds": {"column_missing": 1, "column_added": 1},
        "from": guard["b"]["sha256"], "to": guard["b"]["seen_sha256"],
    }  # fmt: skip
    assert (rows[1][2]["where"], rows[1][2]["paused"], rows[1][2]["basis"]) == ("scan", True, None)
    assert rows[1][2]["paused_seq"] == await paused_seq(pair, run_id)  # its trip's pause: one dedup key
    assert guard["since"]["cycle"] == 1 and guard["since"]["where"] == "scan"

    with pytest.raises(drift.Refused) as refused:
        await resume_run(ctx, run_id, who=TESTER)
    assert refused.value.status == 409 and str(refused.value) == (
        f"run {run_id} cannot resume: side b: column 'amount' of public.customers is gone, or the role may no"
        " longer read it"
    )
    assert (await pair.h.run(run_id))["status"] == "paused"
    assert len(await pair.audited(run_id, "paused", "schema.drift", "running")) == 2  # nothing was written

    pair.alter("b", "ALTER TABLE customers ADD COLUMN amount numeric(12,2)")
    assert await resume_run(ctx, run_id, who=TESTER) == "running"
    [(_, _, running)] = await pair.audited(run_id, "running")
    assert "schema" not in running  # under finding nothing needed accepting
    run = await pair.h.run(run_id)
    assert run["guard"]["accepted"] is None and drift.lines(run) == [gained, FIRST]
    assert await scanning.scan(ctx, run_id) is None and scans == ["customers", "customers"]
    assert (await pair.h.run(run_id))["scan"]["rows_a"] == 3
    assert len(drifts(await pair.audited(run_id))) == 1  # the gained column stands: one row a state


@pytest.mark.parametrize(
    ("statement", "kinds", "line"),
    [
        (
            "ALTER TABLE customers ALTER COLUMN amount TYPE text",
            {"class_changed": 1},
            "side a: column 'amount' of public.customers was numeric(12,2) (decimal) when this run was"
            " planned and is text (text) now: its values would no longer render as planned. Cancel the run"
            " and start a new one, which plans the table as it is now, or change the column back and"
            " resume.",
        ),
        (
            "ALTER TABLE customers RENAME TO clients",
            {"relation_missing": 1},
            "side a: table or view public.customers is gone or no longer visible. Put it back and resume"
            " the run, or cancel it.",
        ),
    ],
    ids=["class", "relation"],
)
async def test_a_change_that_breaks_the_plan_pauses(
    pair: Pair, statement: str, kinds: dict[str, int], line: str
) -> None:
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(), "batch", who=TESTER)
    pair.alter("a", statement)
    with pytest.raises(drift.Tripped):
        await scanning.scan(ctx, run_id)
    run = await pair.h.run(run_id)
    assert drift.lines(run)[0] == f"Paused by the schema guard at the start of cycle 1: {line}"
    [detail] = drifts(await pair.audited(run_id))
    assert detail["sides"]["a"]["kinds"] == kinds and detail["sides"]["b"]["kinds"] == {}


async def test_pause_mode_pauses_on_any_other_change_and_a_resume_accepts(pair: Pair) -> None:
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(schemaDrift={"mode": "pause"}), "batch", who=TESTER)
    pair.alter("a", "ALTER TABLE customers ALTER COLUMN email TYPE varchar(254)")
    with pytest.raises(drift.Tripped):
        await scanning.scan(ctx, run_id)
    run = await pair.h.run(run_id)
    widened = "side a: column 'email' was text and is character varying(254) now (still text)"
    assert drift.lines(run)[0] == (
        f"Paused by the schema guard at the start of cycle 1: {widened}. This spec pauses on schema drift:"
        " resume to go on with the run's plan, or cancel."
    )
    assert drift.told(run)["accepts"] and run["guard"]["mode"] == "pause"
    [_, (_, _, detail)] = await pair.audited(run_id, "paused", "schema.drift")
    assert (detail["paused"], detail["sides"]["a"]["kinds"]) == (True, {"type_changed": 1})
    assert await resume_run(ctx, run_id, who=TESTER) == "running"
    run = await pair.h.run(run_id)
    hashes = {"a": run["guard"]["a"]["seen_sha256"], "b": run["guard"]["b"]["sha256"]}
    [(_, _, running)] = await pair.audited(run_id, "running")
    assert running["schema"] == {"accepted": hashes} and run["guard"]["accepted"]["where"] == "resume"
    day = run["guard"]["accepted"]["at"][:10]
    assert drift.lines(run) == [
        widened,
        f"the change of side a observed at cycle 1 was accepted by tester on {day}; the run goes on with"
        " its plan",
        FIRST,
    ]
    assert await scanning.scan(ctx, run_id) is None  # an accepted shape never trips again
    await pair.next_cycle(run_id)
    pair.alter("a", "ALTER TABLE customers ALTER COLUMN email DROP NOT NULL")  # another state: it trips
    with pytest.raises(drift.Tripped):
        await scanning.scan(ctx, run_id)
    assert len(drifts(await pair.audited(run_id))) == 2


async def test_a_resume_that_accepts_is_named_so_while_a_breaking_change_stands(pair: Pair) -> None:
    """Under ``pause`` a dropped column and a gained one pause the run. Once the column is put back, the
    resume accepts the gained one in the person's name, so the block says a resume accepts from the pause
    on, while the dropped column still refuses it (U1)."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(schemaDrift={"mode": "pause"}), "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    pair.alter("b", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    with pytest.raises(drift.Tripped):
        await scanning.scan(ctx, run_id)
    told = drift.told(await pair.h.run(run_id))
    assert (told["paused"], told["accepts"]) == (True, True)
    with pytest.raises(drift.Refused):
        await resume_run(ctx, run_id, who=TESTER)
    pair.alter("b", "ALTER TABLE customers ADD COLUMN amount numeric(12,2)")
    assert await resume_run(ctx, run_id, who=TESTER) == "running"
    accepted = (await pair.h.run(run_id))["guard"]["accepted"]
    assert (accepted["by"], accepted["where"]) == (TESTER.actor, "resume")  # as the block said it would


async def test_a_resume_is_refused_while_a_side_cannot_be_looked_at(pair: Pair) -> None:
    """U2."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(schemaDrift={"mode": "pause"}), "batch", who=TESTER)
    pair.alter("a", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    with pytest.raises(drift.Tripped):
        await scanning.scan(ctx, run_id)
    pair.files["a"].write_text("postgresql://nobody@127.0.0.1:1/none")
    with pytest.raises(drift.Refused) as refused:
        await resume_run(ctx, run_id, who=TESTER)
    assert refused.value.status == 409 and str(refused.value) == (
        f"run {run_id} cannot resume: side a's table could not be checked now (OperationalError): try again"
        " once the target answers"
    )


async def test_one_event_per_state(pair: Pair) -> None:
    """A standing change is one ``schema.drift`` row, however often it is met: a run that is running
    again without the guard's own resume is paused again, by a ``paused`` row alone."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(), "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")
    for _ in range(2):
        with pytest.raises(drift.Tripped):
            await scanning.scan(ctx, run_id)
        async with ctx.pool.connection() as conn:
            await repo.set_run_status(conn, run_id, "running", when=("paused",))
    rows = await pair.audited(run_id, "paused", "schema.drift")
    assert [action for action, _, _ in rows] == ["paused", "schema.drift", "paused"]


# -- V3: a key-list cycle's start ----------------------------------------------------------------------------


async def test_an_added_column_warns_once_across_three_cycles(pair: Pair) -> None:
    """V3: a change that breaks nothing is recorded with one row and the run goes on with its plan; the
    start of its first cycle was looked at when it was created."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(execution=SOAK), "soak", ["1", "2"], who=TESTER)
    assert not drift.due(await pair.h.run(run_id))
    pair.alter("b", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    for cycle in (2, 3, 4):
        await pair.next_cycle(run_id)
        assert drift.due(await pair.h.run(run_id))
        await drift.cycle_start(ctx, run_id)
        run = await pair.h.run(run_id)
        assert run["status"] == "running" and not drift.due(run)
        assert (run["guard"]["checked"]["cycle"], run["guard"]["checked"]["where"]) == (cycle, "cycle")
    rows = await pair.audited(run_id, "paused", "schema.drift")
    assert [(action, actor) for action, actor, _ in rows] == [("schema.drift", WORKER.actor)]
    detail = rows[0][2]
    assert (detail["cycle"], detail["where"], detail["paused"], detail["checked"]) == (
        2,
        "cycle",
        False,
        True,
    )
    assert detail["sides"]["b"]["kinds"] == {"column_added": 1} and detail["sides"]["a"]["other"] == 0
    gained = "side {} gained column 'loyalty_tier' since this run was planned: this run does not compare it"
    assert drift.lines(run) == [gained.format("b"), FIRST] and run["guard"]["since"]["cycle"] == 2
    # Once side a holds it too, a new run would compare it: another state, another row.
    pair.alter("a", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    await pair.next_cycle(run_id)
    await drift.cycle_start(ctx, run_id)
    run = await pair.h.run(run_id)
    would = [gained.format(side) + "; a new run would" for side in "ab"]
    assert drift.lines(run) == [*would, FIRST]
    [_, second] = drifts(await pair.audited(run_id))
    assert second["sides"]["a"]["other"] == 1 and second["sides"]["b"]["other"] == 0  # what the state adds


async def test_a_run_from_before_016_is_adopted(pair: Pair) -> None:
    """Its first look records the shape seen then, as no baseline; only what is missing is judged."""
    ctx = pair.h.ctx
    spec_id = await pair.spec()
    scanned = await create_run(ctx, spec_id, "batch", who=TESTER)
    listed = await create_run(ctx, spec_id, "batch", ["1", "2"], who=TESTER)
    for run_id in (scanned, listed):
        await pair.before_016(run_id)
    pair.alter("b", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    assert await scanning.scan(ctx, scanned) is None  # V2 adopts it: a gained column is not judged
    run = await pair.h.run(scanned)
    guard = run["guard"]
    assert (guard["adopted"], guard["baseline"], guard["basis"], guard["since"]) == (1, False, None, None)
    assert "loyalty_tier" in guard["b"]["planned"]["columns"] and guard["b"]["changes"] == []
    assert drift.lines(run) == [
        "planned before the schema guard existed: its tables' shapes were first recorded at cycle 1"
    ]
    assert drifts(await pair.audited(scanned)) == []
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")  # a planned column is missing: judged
    assert drift.due(await pair.h.run(listed))
    await drift.cycle_start(ctx, listed)
    run = await pair.h.run(listed)
    assert (run["status"], run["stop_reason"], run["guard"]["adopted"]) == ("paused", drift.SCHEMA_PAUSE, 1)
    [detail] = drifts(await pair.audited(listed))
    assert detail["sides"]["b"] == {
        "breaking": 1, "other": 0, "kinds": {"column_missing": 1}, "from": None,
        "to": run["guard"]["b"]["seen_sha256"],
    }  # fmt: skip
    # An adopted run is no baseline: the next run of the version is judged by its plan's compared columns.
    after = (await pair.h.run(await create_run(ctx, spec_id, "batch", who=TESTER)))["guard"]
    assert (after["basis"]["run_id"], after["basis"]["predates"], after["baseline"]) == (listed, True, False)


async def test_a_fault_of_the_guard_fails_open_with_its_row(
    pair: Pair, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A fault of the guard's own code is never silent and never stops a run: the look is recorded as
    not made, with the fault's class, and the pass goes on."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(), "batch", who=TESTER)
    pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")

    def broken(*args: Any, **options: Any) -> Any:
        raise KeyError("a bug")

    monkeypatch.setattr(precheck, "changes", broken)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.guard"), pytest.raises(psycopg.Error):
        await scanning.scan(ctx, run_id)  # the pass went on, and met the dropped column itself
    assert logged(caplog) == [f"schema guard: run {run_id} not checked: KeyError"]
    run = await pair.h.run(run_id)
    assert run["status"] == "running" and run["guard"]["checked"]["error_type"] == "KeyError"
    [detail] = drifts(await pair.audited(run_id))
    assert detail == {
        "cycle": 1, "where": "scan", "mode": "finding", "checked": False, "paused": False,
        "paused_seq": None, "basis": None, "sides": {}, "error_type": "KeyError",
    }  # fmt: skip
    assert drift.lines(run)[0] == (
        "the schema guard could not check the run's tables at the start of cycle 1 (KeyError): the cycle went"
        " on unchecked"
    )


async def test_a_fault_of_the_guard_refuses_no_run_and_keeps_none_paused(
    pair: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At a creation and at a resume too, a fault of the guard's own code is recorded and stops nothing:
    the run is created with nothing held of its tables, which its next look adopts, and a run the guard
    paused resumes."""
    ctx, spec_id = pair.h.ctx, await pair.spec(schemaDrift={"mode": "pause"})

    def broken(*args: Any, **options: Any) -> Any:
        raise KeyError("a bug")

    with monkeypatch.context() as patched:
        patched.setattr(precheck, "roles", broken)
        run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
    run = await pair.h.run(run_id)
    assert (run["status"], run["guard"]["checked"]["error_type"]) == ("running", "KeyError")
    [(_, actor, detail)] = await pair.audited(run_id, "schema.drift")
    assert (actor, detail["where"], detail["checked"], detail["error_type"], detail["sides"]) == (
        TESTER.actor, "creation", False, "KeyError", {},
    )  # fmt: skip
    assert not drift.told(run)["guarded"] and drift.lines(run) == [
        "the schema guard could not check the run's tables before any table row was read (KeyError): the"
        " cycle went on unchecked"
    ]
    assert await scanning.scan(ctx, run_id) is None  # V2 adopts it
    assert (await pair.h.run(run_id))["guard"]["adopted"] == 1
    await pair.next_cycle(run_id)
    pair.alter("a", "ALTER TABLE customers ADD COLUMN loyalty_tier text")
    with pytest.raises(drift.Tripped):
        await scanning.scan(ctx, run_id)
    monkeypatch.setattr(drift, "_looked", broken)
    assert await resume_run(ctx, run_id, who=TESTER) == "running"
    [*_, (_, _, running), (_, _, unchecked)] = await pair.audited(run_id, "running", "schema.drift")
    assert "schema" not in running and (unchecked["where"], unchecked["checked"]) == ("resume", False)
    assert (await pair.h.run(run_id))["guard"]["checked"]["error_type"] == "KeyError"


async def test_unreachable_at_cycle_start_does_not_stall_leases(
    pair: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V3's faults: a side that cannot be reached is tried twice, a lease period apart; then one row says
    the cycle's start was not checked, and the census, the leases and the stop conditions go on as they
    did before the guard existed."""
    ctx = pair.h.ctx
    execution = {"concurrency": 1, "stop": {"maxConsecutiveErrors": 2}}
    run_id = await create_run(ctx, await pair.spec(execution=execution), "batch", ["1", "2", "3"], who=TESTER)
    await pair.before_016(run_id)  # so that its first cycle's start is still to be looked at
    pair.files["b"].write_text("postgresql://nobody@127.0.0.1:1/none")
    looks: list[float] = []
    real, counted = drift.observed, census.record

    async def observed(*args: Any) -> Any:
        looks.append(asyncio.get_running_loop().time())
        return await real(*args)

    async def record(*args: Any, **options: Any) -> Any:
        assert len(looks) == 2  # the census waited for the guard's two tries
        return await counted(*args, **options)

    monkeypatch.setattr(drift, "observed", observed)
    monkeypatch.setattr(census, "record", record)
    run = await pair.h.finish(run_id)
    assert len(looks) == 2 and looks[1] - looks[0] >= 1  # a lease period apart
    [detail] = drifts(await pair.audited(run_id))
    assert detail == {
        "cycle": 1, "where": "cycle", "mode": "finding", "checked": False, "paused": False,
        "paused_seq": None, "basis": None, "sides": {}, "error_type": "TargetUnreachable",
    }  # fmt: skip
    checked = run["guard"]["checked"]
    assert (checked["cycle"], checked["error_type"], checked["side"]) == (1, "TargetUnreachable", "b")
    assert drift.lines(run) == [
        "the schema guard could not check side b at the start of cycle 1 (TargetUnreachable): the cycle went"
        " on unchecked"
    ]
    censused = {detail["side"]: detail for _, _, detail in await pair.audited(run_id, "run.census")}
    assert censused["a"]["rows"] == 3 and censused["b"]["failed"] == "OperationalError"  # counted, then
    assert (run["status"], run["stop_reason"]) == (
        "completed_with_issues", "stop_condition:maxConsecutiveErrors",
    )  # fmt: skip
    assert await pair.h.results(run_id)  # its keys were leased and read


async def test_a_cycle_start_not_checked_is_never_claimed(pair: Pair) -> None:
    """The guard keeps each look it could not make, across the looks after it: once a cycle's start went
    unchecked, no line says it looked at each cycle's start, then or after a later look succeeded."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(execution=SOAK), "soak", ["1", "2"], who=TESTER)
    reachable = pair.files["a"].read_text()
    pair.files["a"].write_text("postgresql://nobody@127.0.0.1:1/none")
    await pair.next_cycle(run_id)
    await drift.cycle_start(ctx, run_id)  # two tries, a lease period apart
    run = await pair.h.run(run_id)
    unmade = (
        "no change observed against the run's plan where the schema guard looked (table sides only; a view's"
        " own tables, constraints and defaults are not read); it could not look at the start of cycle 2"
    )
    assert drift.lines(run) == [
        "the schema guard could not check side a at the start of cycle 2 (TargetUnreachable): the cycle went"
        " on unchecked",
        unmade,
        FIRST,
    ]
    pair.files["a"].write_text(reachable)
    await pair.next_cycle(run_id)
    await drift.cycle_start(ctx, run_id)
    run = await pair.h.run(run_id)
    assert (run["guard"]["checked"]["cycle"], run["guard"]["checked"]["error_type"]) == (3, None)
    assert run["guard"]["unchecked"] == [{"cycle": 2, "where": "cycle"}]
    assert drift.lines(run) == [unmade, FIRST]


# -- V5: a failed read ---------------------------------------------------------------------------------------


async def test_a_column_dropped_under_per_key_reads_pauses_the_run_at_v5(
    pair: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5: a read that failed on a column has the guard look before the run is failed: a change that
    breaks its plan pauses it, its keys handed back, and it goes on once the column is back."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(), "batch", ["1", "2", "3"], who=TESTER)
    real = census.record

    async def then_dropped(*args: Any, **options: Any) -> Any:
        found = await real(*args, **options)
        pair.alter("b", "ALTER TABLE customers DROP COLUMN amount")  # after the census, before a read
        return found

    monkeypatch.setattr(census, "record", then_dropped)
    run = await pair.h.finish(run_id, (*repo.TERMINAL, "paused"))
    assert (run["status"], run["stop_reason"], run["error"]) == ("paused", drift.SCHEMA_PAUSE, None)
    assert drift.lines(run)[0].startswith(
        "Paused by the schema guard in cycle 1: side b: column 'amount' of public.customers is gone, or"
    )
    [paused, drifted] = await pair.audited(run_id, "paused", "schema.drift", "run.finished")
    assert paused[0] == "paused" and (drifted[2]["where"], drifted[2]["paused"]) == ("read", True)
    async with ctx.pool.connection() as conn:
        assert (await repo.cycle_counts(conn, run_id))["keys"] == {"pending": 3}  # handed back, unjudged
    monkeypatch.setattr(census, "record", real)
    pair.alter("b", "ALTER TABLE customers ADD COLUMN amount numeric(12,2)")
    assert await resume_run(ctx, run_id, who=TESTER) == "running"
    run = await pair.h.finish(run_id)
    assert run["status"] in ("completed", "completed_with_issues") and len(await pair.h.results(run_id)) == 3


# Each engine's customers, as SEED's; a column dropped and put back.
ENGINE_SEEDS = {
    "mysql": (
        """CREATE TABLE customers (id INT PRIMARY KEY, email VARCHAR(100) NOT NULL, amount DECIMAL(12, 2),
                                   legacy_flag TINYINT(1));
           INSERT INTO customers VALUES (1, 'a@x', 10.50, 1), (2, 'b@x', 20.00, 0), (3, 'c@x', 5.25, NULL)""",
        "ALTER TABLE customers DROP COLUMN amount",
        "ALTER TABLE customers ADD COLUMN amount DECIMAL(12, 2)",
    ),
    "oracle": (
        """CREATE TABLE customers (id NUMBER(10) PRIMARY KEY, email VARCHAR2(100) NOT NULL,
                                   amount NUMBER(12, 2), legacy_flag NUMBER(1));
           INSERT INTO customers VALUES (1, 'a@x', 10.50, 1);
           INSERT INTO customers VALUES (2, 'b@x', 20.00, 0);
           INSERT INTO customers VALUES (3, 'c@x', 5.25, NULL)""",
        "ALTER TABLE customers DROP COLUMN amount",
        "ALTER TABLE customers ADD (amount NUMBER(12, 2))",
    ),
}


def engine_database(
    engine: str, request: pytest.FixtureRequest, seed: str
) -> tuple[dict[str, Any], Callable[[str], Any], str]:
    """A fresh database (MySQL) or account (Oracle) of the session's server, seeded with ``seed``: its
    targets-file entry, what runs a statement in it, and its schema as the catalogue names it."""
    if engine == "mysql":
        server: MySQLServer = request.getfixturevalue("mysql_server")
        database = server.database(seed)
        return server.target(database), lambda statement: server.execute(statement, database), database
    oracle: OracleServer = request.getfixturevalue("oracle_server")
    account = oracle.schema(seed)
    return oracle.target(account), lambda statement: oracle.execute(statement, account), account


@pytest.mark.parametrize(
    "engine",
    [pytest.param("mysql", marks=pytest.mark.mysql), pytest.param("oracle", marks=pytest.mark.oracle)],
)
async def test_a_column_dropped_under_per_key_reads_pauses_the_run_at_v5_on_each_engine(
    engine: str, open_harness: OpenHarness, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5 on MySQL and Oracle: a per-key read of a dropped column (MySQL 1054, ORA-00904: the denied class)
    has the guard look first, and the run is paused, not failed."""
    seed, dropped, restored = ENGINE_SEEDS[engine]
    entry, alter, schema = engine_database(engine, request, seed)
    relation = f"{schema}.{'CUSTOMERS' if engine == 'oracle' else 'customers'}"
    targets = json.dumps({"targets": {"db-a": entry, "db-b": entry}})
    real = census.record

    async def then_dropped(*args: Any, **options: Any) -> Any:
        found = await real(*args, **options)
        alter(dropped)  # after the census, before a read
        return found

    async with open_harness(targets, readiness={}, lease_seconds=1) as h:
        spec_id = await h.spec(table_spec("customers"))
        run_id = await create_run(h.ctx, spec_id, "batch", ["1", "2", "3"], who=TESTER)
        monkeypatch.setattr(census, "record", then_dropped)
        run = await h.finish(run_id, (*repo.TERMINAL, "paused"))
        monkeypatch.setattr(census, "record", real)
        alter(restored)
        resumed = await resume_run(h.ctx, run_id, who=TESTER)
        ended = await h.finish(run_id)
    column = "AMOUNT" if engine == "oracle" else "amount"
    assert (run["status"], run["stop_reason"], run["error"]) == ("paused", drift.SCHEMA_PAUSE, None)
    assert drift.lines(run)[0].startswith(
        f"Paused by the schema guard in cycle 1: side a: column '{column}' of {relation} is gone, or"
    )
    assert run["guard"]["checked"]["where"] == "read" and resumed == "running"
    assert ended["status"] in ("completed", "completed_with_issues")


# A planned column's type changed within its class under a running plan, on each engine: its seed, the
# statements each side then runs, the change's kind and its clause (the {} is side a's schema).
WIDENED = "ALTER TABLE events MODIFY (at TIMESTAMP(6));"
WIDENED += " UPDATE events SET at = TIMESTAMP '2024-01-01 10:00:00.{}'"
RENDERED = "now: its values would no longer render as planned"
RETYPED = {
    "oracle-date-to-timestamp": (
        "oracle",
        "CREATE TABLE events (id NUMBER(10) PRIMARY KEY, at DATE);"
        " INSERT INTO events VALUES (1, TO_DATE('2024-01-01 10:00:00', 'YYYY-MM-DD HH24:MI:SS'))",
        {"a": WIDENED.format(5), "b": WIDENED.format(7)},  # two fractions a DATE's rendering leaves out
        "render_changed",
        f"column 'AT' of {{}}.EVENTS was DATE (timestamp) when this run was planned and is TIMESTAMP(6)"
        f" (timestamp) {RENDERED}",
    ),
    "oracle-timestamp-to-date": (
        "oracle",
        "CREATE TABLE events (id NUMBER(10) PRIMARY KEY, at TIMESTAMP(6));"
        " INSERT INTO events (id) VALUES (1)",
        {"a": "ALTER TABLE events MODIFY (at DATE); UPDATE events SET at = SYSDATE"},
        "render_changed",
        f"column 'AT' of {{}}.EVENTS was TIMESTAMP(6) (timestamp) when this run was planned and is DATE"
        f" (timestamp) {RENDERED}",
    ),
    "mysql-tinyint-to-bit": (
        "mysql",
        "CREATE TABLE events (id INT PRIMARY KEY, flags TINYINT); INSERT INTO events VALUES (1, 7), (2, 3)",
        {"a": "ALTER TABLE events MODIFY flags BIT(8)"},
        "render_changed",
        f"column 'flags' of {{}}.events was tinyint (integer) when this run was planned and is bit(8)"
        f" (integer) {RENDERED}",
    ),
    "oracle-number-38-to-star-key": (
        "oracle",
        "CREATE TABLE events (id NUMBER(38,2) PRIMARY KEY, v VARCHAR2(10));"
        " INSERT INTO events VALUES (1.5, 'x')",
        {"a": "ALTER TABLE events MODIFY (id NUMBER(*,2))"},
        "type_changed",
        "column 'ID' was NUMBER(38,2) and is NUMBER(*,2) now (still decimal)",
    ),
}


@pytest.mark.parametrize(
    ("engine", "seed", "changed", "kind", "said"),
    [pytest.param(*found, marks=getattr(pytest.mark, found[0]), id=name) for name, found in RETYPED.items()],
)
async def test_a_type_its_engine_reads_otherwise_pauses_the_run_on_each_engine(
    engine: str,
    seed: str,
    changed: dict[str, str],
    kind: str,
    said: str,
    open_harness: OpenHarness,
    request: pytest.FixtureRequest,
) -> None:
    """A planned column's type changed within its class under a running plan: where the engine builds other
    statements of the new type (Oracle's DATE and TIMESTAMP, MySQL's BIT), the scan's statements would read
    it otherwise, and V2 pauses the run before its cursor opens, rather than letting it read a wrong match
    or fail. Oracle's NUMBER(*,2) is its NUMBER(38,2): one key text, a warning, and the run goes on."""
    sides = {side: engine_database(engine, request, seed) for side in "ab"}
    targets = json.dumps({"targets": {f"db-{side}": found[0] for side, found in sides.items()}})
    async with open_harness(targets, readiness={}, lease_seconds=1) as h:
        run_id = await create_run(h.ctx, await h.spec(table_spec("events")), "batch", who=TESTER)
        for side, statement in changed.items():
            sides[side][1](statement)
        run = await h.finish(run_id, (*repo.TERMINAL, "paused"))
        async with h.ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT detail FROM ds.audit_log WHERE object = %s AND action = 'schema.drift'", (run_id,)
            )
            [(detail,)] = await cur.fetchall()
    assert detail["sides"]["a"]["kinds"] == {kind: 1} and detail["paused"] == (kind == "render_changed")
    if kind == "render_changed":
        assert (run["status"], run["stop_reason"], run["error"]) == ("paused", drift.SCHEMA_PAUSE, None)
        assert drift.lines(run)[0].startswith(
            f"Paused by the schema guard at the start of cycle 1: side a: {said.format(sides['a'][2])}."
        )
    else:  # a warning: the run read its keys as planned
        assert run["status"] in ("completed", "completed_with_issues") and run["error"] is None
        assert drift.lines(run)[0] == f"side a: {said}"


async def test_a_misclassified_error_at_v5_still_fails(pair: Pair, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read that raises a denied class while nothing that breaks the plan is seen fails the run, as
    it did: the guard pauses only for what it can show."""
    ctx = pair.h.ctx
    run_id = await create_run(ctx, await pair.spec(), "batch", ["1", "2"], who=TESTER)

    async def rows(self: Any, conn: Any, side: Any, asked: Any) -> Any:
        raise psycopg.errors.UndefinedColumn("a function's column, not the table's")

    monkeypatch.setattr(postgresql.PostgreSQL, "rows", rows)
    run = await pair.h.finish(run_id)
    assert (run["status"], run["error"], run["stop_reason"]) == ("failed", "UndefinedColumn", None)
    assert drifts(await pair.audited(run_id)) == [] and run["guard"]["checked"]["where"] == "read"


# -- every text (§10) ----------------------------------------------------------------------------------------

BASIS = "R-" + "0" * 25 + "1"


def _column(name: str, declared: str, cls: str, nullable: bool | None = True) -> Column:
    return Column(name, declared, cls, True, cls == "text", nullable=nullable)


def _relation(schema: str, name: str, *columns: Column, key: str = "id", kind: str = "table") -> Relation:
    return Relation(schema, name, columns, {"kind": kind, "key": [key]})


def _guarded(
    a: tuple[Column, ...], b: tuple[Column, ...], *, mode: str = "finding", basis: Any = None
) -> dict[str, Any]:
    """A run of APP.CUSTOMERS on Oracle against public.customers, each side listing the columns it
    compares (``ID``, ``EMAIL`` and ``AMOUNT`` by their lower-cased names), as the guard holds it at its
    creation; ``basis``: what ``repo.guard_basis`` gave."""
    listed = ["id", "email", "amount"]
    spec = compile_spec(
        json.loads(
            table_spec(
                "customers",
                a={"schema": "APP", "name": "CUSTOMERS", "columns": listed},
                b={"columns": listed},
                schemaDrift={"mode": mode},
            )
        )
    )
    held = {"key_maps": [None], "where": None, "types": {}, "digest": []}
    plan = {
        "a": held | {"schema": "APP", "name": "CUSTOMERS", "keys": ["ID"], "engine": "oracle"},
        "b": held | {"schema": "public", "name": "customers", "keys": ["id"], "engine": "postgresql"},
    }
    plan["a"]["columns"] = {"email": "EMAIL", "amount": "AMOUNT"}
    plan["b"]["columns"] = {"email": "email", "amount": "amount"}
    relations = {
        "a": _relation("APP", "CUSTOMERS", *a, key="ID"),
        "b": _relation("public", "customers", *b),
    }
    guard, _ = drift.created(spec, 12, tables.Planned(plan | {"only_a": [], "only_b": []}, relations), basis)
    return {
        "run_id": "R-" + "0" * 25 + "2", "cycle": 7, "status": "running", "stop_reason": None, "guard": guard,
        "params": {"tables": plan}, "spec": spec,
    }  # fmt: skip


def _seen(run: dict[str, Any], a: tuple[Column, ...] | None, b: tuple[Column, ...], **facts: Any) -> None:
    """The run after a look at the start of its cycle that saw these columns (``a`` None: no table), and
    paused it when the changes that stand do."""
    relations = {
        "a": a and _relation("APP", "CUSTOMERS", *a, key="ID", **facts),
        "b": _relation("public", "customers", *b),
    }
    run["guard"], _ = drift._looked(run, run["spec"], relations, "cycle")
    if drift._stops(run["guard"]):
        run |= {"status": "paused", "stop_reason": drift.SCHEMA_PAUSE}


ORACLE = (
    _column("ID", "NUMBER(10,2)", "decimal", False),
    _column("EMAIL", "VARCHAR2(120 CHAR)", "text"),
    _column("AMOUNT", "NUMBER(12,2)", "decimal"),
)
POSTGRES = (
    _column("id", "numeric(10,2)", "decimal", False),
    _column("email", "character varying(120)", "text"),
    _column("amount", "numeric(12,2)", "decimal"),
    _column("opened", "date", "date", False),
    _column("legacy_flag", "boolean", "boolean"),
)


def test_messages_golden() -> None:
    """Every text of the schema guard that the runs above do not print (§10)."""
    lead = "Paused by the schema guard at the start of cycle 7: "
    replan = (
        "Cancel the run and start a new one, which plans the table as it is now, or change the column back"
        " and resume."
    )
    # E4: a number key whose text is built from its scale and, on Oracle, its precision.
    run = _guarded(ORACLE, POSTGRES)
    _seen(run, (_column("ID", "NUMBER(10,4)", "decimal", False), *ORACLE[1:]), POSTGRES)
    assert drift.lines(run)[0] == (
        f"{lead}side a: key column 'ID' of APP.CUSTOMERS was NUMBER(10,2) when this run was planned and is"
        f" NUMBER(10,4) now: its key text would render differently. {replan}"
    )
    # W: what breaks nothing, a line each; a view, a key, a type within its class, a column it never read.
    run = _guarded(ORACLE, POSTGRES)
    wider = (ORACLE[0], _column("EMAIL", "VARCHAR2(254 CHAR)", "text"), ORACLE[2])
    nullable = (*POSTGRES[:3], _column("opened", "date", "date"))
    _seen(run, wider, nullable, kind="view")
    run["guard"]["b"]["changes"].append(precheck._change("key_changed", None, ["id"], ["id", "region"]))
    assert drift.lines(run) == [
        "side a: column 'EMAIL' was VARCHAR2(120 CHAR) and is VARCHAR2(254 CHAR) now (still text)",
        "side a: APP.CUSTOMERS was a table and is a view now",
        "side b: column 'legacy_flag', which this run does not read, is gone",
        "side b: column 'opened' was NOT NULL and is nullable now",
        "side b: the primary key of public.customers changed",
        "no earlier run of this version recorded its tables' shapes: this run records them",
    ]
    assert drift.told(run)["accepts"] is False and run["status"] == "running"
    # E3 and E1 at once: the first stops the pause's line, the other follows it.
    run = _guarded(ORACLE, POSTGRES)
    _seen(run, None, POSTGRES[:2])
    assert drift.lines(run)[:2] == [
        f"{lead}side a: table or view APP.CUSTOMERS is gone or no longer visible. Put it back and resume the"
        " run, or cancel it.",
        "side b: column 'amount' of public.customers is gone, or the role may no longer read it",
    ]
    # E5 and E6: a column both runs plan whose class or key rendering differs, against the basis.
    was = _guarded(ORACLE, POSTGRES)["guard"]
    retyped = (_column("ID", "NUMBER(10,4)", "decimal", False), ORACLE[1], _column("AMOUNT", "CLOB", "text"))
    run = _guarded(retyped, POSTGRES, basis={"run_id": BASIS, "guard": was, "tables": None})
    key = f"key column 'ID' was NUMBER(10,2) in run {BASIS} of this version and is NUMBER(10,4) now"
    amount = (
        f"column 'AMOUNT' was NUMBER(12,2) (decimal) in run {BASIS} of this version and is CLOB (text) now"
    )
    created = run | {"status": "paused", "stop_reason": drift.SCHEMA_PAUSE}
    assert drift.lines(created) == [
        f"Paused by the schema guard before any table row was read: {amount}. Resume to accept the tables as"
        " they are, or cancel.",
        key,
    ]
    with pytest.raises(drift.Refused) as refused:
        _guarded(retyped, POSTGRES, mode="pause", basis={"run_id": BASIS, "guard": was, "tables": None})
    assert str(refused.value) == (
        f"spec 12 (customers 1.0.0) pauses on schema drift and a run of it would not compare what run {BASIS}"
        f" compared: {amount} (and 1 more). Upload a new version to compare the tables as they are."
    )
    # E7 during a run: any other change pauses a spec that asks for it.
    run = _guarded(ORACLE, POSTGRES, mode="pause")
    _seen(run, wider, POSTGRES)
    assert drift.lines(run)[0] == (
        f"{lead}side a: column 'EMAIL' was VARCHAR2(120 CHAR) and is VARCHAR2(254 CHAR) now (still text)."
        " This spec pauses on schema drift: resume to go on with the run's plan, or cancel."
    )


def test_the_report_s_sentence_adds_up_the_rows_through_its_cycle() -> None:
    def row(
        cycle: int, paused: bool = False, checked: bool = True, mode: str = "finding", where: str = "cycle",
        **sides: tuple[int, int],
    ) -> Any:  # fmt: skip
        counted = {side: {"breaking": found[0], "other": found[1]} for side, found in sides.items()}
        detail = {"cycle": cycle, "paused": paused, "checked": checked, "mode": mode, "where": where}
        return {"detail": detail | {"sides": counted}}

    rows = [row(2, b=(0, 2)), row(5, checked=False), row(7, True, b=(1, 0)), row(8, a=(0, 1), b=(0, 0))]
    assert drift.sentence(rows, 1) is None  # with no row of the cycle or an earlier one it says nothing
    assert (
        drift.sentence(rows, 2) == "Schema guard (observed): through cycle 2 it recorded 2 changes on side b."
    )
    assert drift.sentence(rows, 7) == (
        "Schema guard (observed): through cycle 7 it recorded 1 change that paused the run and 2 others on"
        " side b, and could not check 1 cycle start."
    )
    assert drift.sentence(rows, 9) == (
        "Schema guard (observed): through cycle 9 it recorded 1 change on side a and 1 change that paused the"
        " run and 2 others on side b, and could not check 1 cycle start."
    )
    assert drift.sentence([row(3, checked=False)], 3) == (
        "Schema guard (observed): through cycle 3 it could not check 1 cycle start."
    )
    # Under ``pause`` the change that paused the run is one that breaks nothing.
    assert drift.sentence([row(2, True, mode="pause", a=(0, 1))], 2) == (
        "Schema guard (observed): through cycle 2 it recorded 1 change that paused the run on side a."
    )
    # Under ``finding`` a side's change that breaks nothing paused nothing, though its row paused the run.
    assert drift.sentence([row(2, True, a=(0, 1), b=(1, 0))], 2) == (
        "Schema guard (observed): through cycle 2 it recorded 1 change on side a and 1 change that paused the"
        " run on side b."
    )
    # A look not made at a resume or after a failed read is no cycle start's.
    unmade = [row(3, checked=False, where=where) for where in ("creation", "resume", "read")]
    assert drift.sentence(unmade, 3) == (
        "Schema guard (observed): through cycle 3 it could not check 1 cycle start and 2 other looks."
    )


def test_never_told_that_a_schema_is_unchanged() -> None:
    """N9: the guard says ``no change observed``, with what it reads and when it looked, never that a
    schema is unchanged; and none of its texts uses a word a reader could take for parity (N1)."""
    tree = ast.parse(inspect.getsource(drift))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module)
    }
    texts = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings
    ]
    never = r"\bunchanged\b|\b(match|matches|matched|identical|passed|verified|validated|reconciled)\b|[✓✔]"
    assert texts and not [text for text in texts if re.search(never, text, re.I)]
    told = drift.told(_guarded(ORACLE, POSTGRES))
    assert told["lines"][0] == UNCHANGED and UNCHANGED.startswith("no change observed against the run's plan")
    assert "a view's own tables, constraints and defaults are not read" in UNCHANGED
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", told["checked"]["at"])  # when it looked
