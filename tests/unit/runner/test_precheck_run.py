"""Table pre-checks as the worker reads them (docs/mvp/PRECHECKS.md §5, §6): the sessions it holds on a
target, each side in one snapshot, what a failed read keeps and settles, the checks a limit skips, a
cancel, a worker that is gone, a fault that touches no run, what a pre-check writes, and that no value it
reads reaches a row, an export or a log."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from deltasleuth import prechecks
from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors import tables as table_sides
from deltasleuth.connectors.tables import (
    ENGINES,
    StatementTimeout,
    TargetAuthRefused,
    TargetTempSpace,
    TargetUnreachable,
)
from deltasleuth.core import precheck
from deltasleuth.core.spec import TargetRegistry
from deltasleuth.reports import precheck as reports
from deltasleuth.runner import create_run, run_worker
from deltasleuth.runner import precheck as checking
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import Harness, OpenHarness, ScriptedService, doc, spec_text
from .prechecking import FakeEngine, column, database_targets, ended, made, pairs, relation, table_spec
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
ACCOUNTS = relation(
    "accounts", column("email"), column("balance", "numeric(12,2)", "decimal", size=("digits", 12, 2))
)
NAMES = ("accounts", "cards", "loans", "users")
READ = "side a: postgresql 16.4, public.{0}, in 1 snapshot at "
Tables = Callable[..., AsyncIterator[Harness]]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    """Every database target answers as the engine in memory: a table of each of ``NAMES``."""
    engine = FakeEngine(*(relation(name, *ACCOUNTS.columns[1:]) for name in NAMES), pause=0.02)
    monkeypatch.setitem(ENGINES, "postgresql", engine)
    return engine


@pytest.fixture
async def tables(open_harness: OpenHarness, tmp_path: Path) -> Tables:
    """``async with tables(**settings) as h``: a harness whose targets db-a, db-b and db-c are databases."""
    (tmp_path / "dsn_tables").write_text("postgresql://unused")

    def open_(**settings: Any) -> Any:
        return open_harness(database_targets(tmp_path / "dsn_tables", "db-a", "db-b", "db-c"), **settings)

    return open_


async def checked(h: Harness, *specs: str, level: str = "columns") -> str:
    """A pre-check of the stored ``specs``, as an operator makes one."""
    spec_ids = [await h.spec(text) for text in specs]
    return (await prechecks.create(h.ctx, {"spec_ids": spec_ids, "level": level}, TESTER))["precheck_id"]


async def audited(h: Harness, action: str) -> list[dict[str, Any]]:
    async with h.ctx.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT actor, object, detail FROM ds.audit_log WHERE action = %s ORDER BY id", (action,)
        )
        return await cur.fetchall()


@pytest.mark.parametrize("sessions", [1, 2])
async def test_the_worker_holds_at_most_its_sessions_a_target(
    tables: Tables, fake: FakeEngine, sessions: int
) -> None:
    async with tables(precheck={"sessions_per_target": sessions}) as h:
        precheck_id = await checked(h, *(table_spec(name) for name in NAMES))
        [row] = await ended(h.ctx, precheck_id)
        found = await pairs(h.ctx, precheck_id)
        [finished] = await audited(h, "precheck.finished")
    assert dict(fake.most) == {"db-a": sessions, "db-b": sessions} and not +fake.open
    assert row["progress"] == {"done": 4} and row["verdicts"] == {"no_difference_observed": 4}
    assert [(pair["status"], pair["tries"], pair["rows_a"], pair["rows_b"]) for pair in found] == [
        ("done", 1, 3, 3)
    ] * 4
    seconds = finished["detail"].pop("seconds")
    assert isinstance(seconds, int) and (finished["actor"], finished["object"]) == (WORKER.actor, precheck_id)
    assert finished["detail"] == {"pairs": 4, "verdicts": {"no_difference_observed": 4}, "failed": {}}
    # Each side in one snapshot: a pair opens one session a target, and asks everything of its side on it.
    assert fake.count("snapshot", "db-a") == fake.count("snapshot", "db-b") == 4
    asked = [(detail[0], event) for event, _, detail in fake.events if isinstance(detail, tuple)]
    by_session = {n: [event for m, event in asked if m == n] for n, _ in asked}
    assert list(by_session.values()) == [["describe", "probe", "figures"]] * 8


async def test_same_target_pair_runs_under_one_session(tables: Tables, fake: FakeEngine) -> None:
    async with tables() as h:
        precheck_id = await checked(h, table_spec(targets=("db-a", "db-a")), level="distinct")
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
    assert dict(fake.most) == {"db-a": 1}  # side a, then side b
    sent = [(event, detail if isinstance(detail, int) else detail[0]) for event, _, detail in fake.events]
    assert sent == [
        ("snapshot", 900), ("describe", 1), ("describe", 1), ("probe", 1), ("probe", 1), ("figures", 1),
        ("duplicates", 1), ("distinct", 1), ("close", 1),
        ("snapshot", 900), ("figures", 2), ("duplicates", 2), ("distinct", 2), ("close", 2),
    ]  # fmt: skip
    sides = pair["result"]["sides"]
    assert isinstance(pair["result"]["apart_s"], int) and sides["a"]["at"] <= sides["b"]["at"]
    assert sides["a"]["sha256"] == sides["b"]["sha256"] and pair["verdict"] == "no_difference_observed"
    assert pair["result"]["keys"]["b"] == {
        "rows": 3, "null_keys": 0, "duplicate_keys": 0, "rows_in_duplicates": 0, "primary": ["id"],
    }  # fmt: skip


async def test_a_timeout_keeps_what_was_read_before_it(tables: Tables, fake: FakeEngine) -> None:
    fake.fail("distinct", "db-b", StatementTimeout("MySQL 3024"))
    fake.values["db-b", "nulls", "email"] = "2"
    async with tables() as h:
        precheck_id = await checked(h, table_spec(), level="distinct")
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
    result = pair["result"]
    hint = "raise precheck.statement_timeout_s, or pre-check a filtered view"
    assert (pair["verdict"], pair["tries"], pair["line"]) == (
        "not_read", 1, f"not checked: side b's distinct values timed out after 900 s ({hint})",
    )  # fmt: skip
    assert fake.count("snapshot", "db-b") == 1  # never read again
    assert result["sides"]["b"]["failed"] == {"distinct": "timeout"} and result["sides"]["a"]["failed"] == {}
    assert result["keys"]["b"]["rows"] == 3 and result["keys"]["b"]["duplicate_keys"] == 0
    email = result["columns"]["email"]["figures"]
    assert email["nulls"] == {"a": 0, "b": 2, "verdict": "differs", "reason": None}
    assert email["distinct"] == {
        "verdict": "not checked", "reason": f"side b's distinct values timed out after 900 s ({hint})",
    }  # fmt: skip
    assert "1 of 2 compared columns differ in a figure: email (nulls)" in result["lines"]


async def test_unreachable_twice_settles_its_target_s_pairs(tables: Tables, fake: FakeEngine) -> None:
    fake.fail("snapshot", "db-b", TargetUnreachable("ORA-12541"), always=True)
    async with tables(lease_seconds=1) as h:
        precheck_id = await checked(h, *(table_spec(name) for name in NAMES[:3]))
        other = await checked(h, table_spec("users", targets=("db-a", "db-c")))
        rows = await ended(h.ctx, precheck_id, other)
        found = await pairs(h.ctx, precheck_id)
        [untouched] = await pairs(h.ctx, other)
        finished = await audited(h, "precheck.finished")
    assert fake.count("snapshot", "db-b") == 2  # once, and once more a lease period later
    line = "not checked: side b could not be reached (ORA-12541)"
    assert [(pair["verdict"], pair["line"], pair["tries"]) for pair in found] == [
        ("not_read", line, 1), ("not_read", line, 0), ("not_read", line, 0),
    ]  # fmt: skip
    assert found[0]["result"]["sides"]["a"]["relation"] == "public.accounts"  # what was read stays
    assert found[1]["result"]["lines"] == [line, "side a: not read; side b: not read: not read at one moment"]
    assert rows[0]["verdicts"] == {"not_read": 3} and untouched["verdict"] == "no_difference_observed"
    assert {row["object"]: row["detail"]["failed"] for row in finished} == {
        precheck_id: {"unreachable": 3}, other: {},
    }  # fmt: skip


@pytest.mark.parametrize("sessions", [1, 2])
async def test_refused_login_settles_every_open_precheck_of_the_target(
    tables: Tables, fake: FakeEngine, sessions: int
) -> None:
    fake.fail("snapshot", "db-b", TargetAuthRefused("ORA-01017"), always=True)
    async with tables(precheck={"sessions_per_target": sessions}) as h:
        first = await checked(h, table_spec("accounts"), table_spec("cards"))
        second = await checked(h, table_spec("loans", targets=("db-b", "db-a")), table_spec("users"))
        other = await checked(h, table_spec("users", targets=("db-a", "db-c"), version="2.0.0"))
        rows = await ended(h.ctx, first, second, other)
        found = [pair for precheck_id in (first, second) for pair in await pairs(h.ctx, precheck_id)]
    # Never tried again: a stale password costs at most the sessions in flight, whatever waits.
    assert 1 <= fake.count("snapshot", "db-b") <= sessions
    untried = "refused the credential (ORA-01017); no other pair of target db-b was tried"
    assert sorted((pair["label"], pair["verdict"], pair["line"]) for pair in found) == [
        ("accounts", "not_read", f"not checked: side b {untried}"),
        ("cards", "not_read", f"not checked: side b {untried}"),
        ("loans", "not_read", f"not checked: side a {untried}"),
        ("users", "not_read", f"not checked: side b {untried}"),
    ]
    assert [row["verdicts"] for row in rows] == [
        {"not_read": 2}, {"not_read": 2}, {"no_difference_observed": 1},
    ]  # fmt: skip


async def test_distinct_counts_over_the_row_limit_are_skipped(tables: Tables, fake: FakeEngine) -> None:
    async with tables(precheck={"distinct_max_rows": 2}) as h:
        precheck_id = await checked(h, table_spec(), level="distinct")
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
    assert fake.count("duplicates") == fake.count("distinct") == 0  # they sort or hash
    assert pair["verdict"] == "incomplete" and pair["result"]["skipped"] == ["distinct_rows"] * 2
    assert pair["line"] == (  # a duplicate key not counted is never read as none (N3)
        "duplicate keys and distinct values not counted: 3 rows on side a is over precheck.distinct_max_rows"
        " (2)"
    )
    assert pair["result"]["keys"]["a"]["duplicate_keys"] is None


async def test_a_temporary_space_failure_names_its_check(tables: Tables, fake: FakeEngine) -> None:
    fake.fail("duplicates", "db-a", TargetTempSpace("ORA-01652"))
    async with tables() as h:
        precheck_id = await checked(h, table_spec(), level="distinct")
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
    assert pair["line"] == "duplicate keys not counted: side a ran out of temporary space (ORA-01652)"
    assert pair["result"]["sides"]["a"]["failed"] == {"duplicates": "temp_space"}
    assert fake.count("distinct", "db-a") == 0 and fake.count("distinct", "db-b") > 0  # its read ended


async def test_a_refusal_ends_the_read_and_quotes_the_plan(tables: Tables, fake: FakeEngine) -> None:
    async with tables() as h:
        missing = table_spec("accounts", a={"name": "gone"})
        column_ = table_spec("cards", a={"columns": ["email", "nope"]}, b={"columns": ["email", "nope"]})
        neither = table_spec("loans", a={"name": "gone"}, b={"name": "gone"})
        one_target = table_spec("users", a={"name": "gone"}, b={"name": "gone"}, targets=("db-c", "db-c"))
        precheck_id = await checked(h, missing, column_, neither, one_target)
        await ended(h.ctx, precheck_id)
        found = {pair["label"]: pair for pair in await pairs(h.ctx, precheck_id)}
    assert fake.count("figures") == fake.count("probe") == 0
    # Two targets are described at once and each refusal is listed, side a's first; on one target the
    # first refusal ends the session's read, as it ends a plan's.
    gone = "table or view gone not found or not visible"
    assert found["loans"]["result"]["refused"] == [f"side a: {gone}", f"side b: {gone}"]
    assert found["users"]["result"]["refused"] == [f"side a: {gone}"] and fake.count("describe", "db-c") == 1
    assert found["accounts"]["line"] == (
        "a run of this spec would be refused: side a: table or view gone not found or not visible"
    )
    assert found["cards"]["result"]["refused"] == [
        "side a: column 'nope' is not in public.cards",
        "side b: column 'nope' is not in public.cards",
    ]
    assert found["cards"]["line"] == (
        "a run of this spec would be refused: side a: column 'nope' is not in public.cards (and 1 more)"
    )
    assert {pair["verdict"] for pair in found.values()} == {"refused"}


async def test_precheck_with_a_file_side_refused(
    open_harness: OpenHarness, fake: FakeEngine, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """docs/mvp/FILES.md §12, K1: a stored spec that names a file side is refused at its look, and nothing of
    its files is read: its run counts rows, lines that are not rows and repeated keys."""
    exports = tmp_path_factory.mktemp("exports").resolve()  # outside the harness's own directories (T10)
    (exports / "accounts.csv").write_bytes(b"id|email\n1|a\n")
    (tmp_path / "dsn_tables").write_text("postgresql://unused")
    targets = {
        "db-a": {"engine": "files", "root": str(exports), "settle_s": 0, "allow_writable": True},
        "db-b": {"dsn_ref": f"file:{tmp_path / 'dsn_tables'}", "rate_per_s": 1000},
    }
    dialect = {"columns": ["id", "email"], "delimiter": "|", "header": True}
    filed = table_spec(a={"file": {"path": "accounts.csv", "format": "csv", "csv": dialect}})
    async with open_harness(json.dumps({"targets": targets}), files={"roots_under": str(exports)}) as h:
        precheck_id = await checked(h, filed)
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
    assert (pair["verdict"], fake.count("figures"), fake.count("probe")) == ("refused", 0, 0)
    assert pair["line"] == precheck.FILE_SIDE and pair["result"]["refused"] == [f"side a: {pair['line']}"]


async def test_cancel_sends_the_engine_s_cancel(tables: Tables, fake: FakeEngine) -> None:
    fake.hold = asyncio.Event()  # never released: each statement runs until it is cancelled
    async with tables() as h:
        precheck_id = await checked(h, table_spec("accounts"), table_spec("cards"))
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop, schedules=False))
        try:
            while fake.count("figures") < 2:  # both sides of the first pair are being read
                await asyncio.sleep(0.02)
            done = await prechecks.cancel(h.ctx, precheck_id, TESTER)
            while fake.count("cancelled") < 2:
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await worker
        row = await made(h.ctx, precheck_id)
        found = await pairs(h.ctx, precheck_id)
        [cancelled] = await audited(h, "precheck.cancel")
        with pytest.raises(prechecks.Refused, match=f"^table pre-check {precheck_id} has ended$") as again:
            await prechecks.cancel(h.ctx, precheck_id, TESTER)
        assert await audited(h, "precheck.finished") == []
    assert again.value.status == 409 and not +fake.open
    assert (
        row["cancelled_by"] == TESTER.actor
        and row["finished_at"] == row["cancelled_at"] == done["cancelled_at"]
    )
    assert row["progress"] == {"cancelled": 2} and row["verdicts"] == {}
    assert {(pair["status"], pair["verdict"], pair["line"]) for pair in found} == {
        ("cancelled", None, precheck.CANCELLED)
    }
    assert (cancelled["actor"], cancelled["object"]) == (TESTER.actor, precheck_id)
    assert cancelled["detail"]["pending"] == cancelled["detail"]["running"] == 1


async def test_a_worker_that_stops_mid_pair_sets_it_back_after_renewing_its_claim(
    tables: Tables, fake: FakeEngine
) -> None:
    """``ds precheck run`` interrupted, or ``ds worker`` stopped, while a pair is read: the pair is pending
    again, not running for ever with no reader, which would keep the stalled banner from standing. While it
    was read its claim was renewed every third of a lease period."""
    fake.hold = asyncio.Event()  # never released: each statement runs until it is cancelled
    async with tables(lease_seconds=1) as h:
        precheck_id = await checked(h, table_spec())
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop, schedules=False))
        try:
            while fake.count("figures") < 1:
                await asyncio.sleep(0.02)
            claimed = (await made(h.ctx, precheck_id))["claimed_at"]
            await asyncio.sleep(1)
            renewed = await made(h.ctx, precheck_id)
        finally:
            stop.set()
            await worker
        row = await made(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
    assert claimed is not None and renewed["progress"] == {"running": 1} and renewed["claimed_at"] > claimed
    assert (pair["status"], pair["tries"], row["progress"], row["claimed_at"]) == (
        "pending",
        1,
        {"pending": 1},
        None,
    )
    assert fake.count("cancelled") >= 1  # its statement was ended on its server


async def test_a_killed_worker_s_items_finish_once(tables: Tables, fake: FakeEngine) -> None:
    async with tables() as h:
        precheck_id = await checked(h, table_spec("accounts"), table_spec("cards"))
        async with h.ctx.pool.connection() as conn:  # a worker that claimed a pair, then was killed
            assert (await repo.claim_pair(conn, precheck_id, 1))["tries"] == 1  # type: ignore[index]
        [row] = await ended(h.ctx, precheck_id)
        found = await pairs(h.ctx, precheck_id)
        finished = await audited(h, "precheck.finished")
    assert {pair["n"]: (pair["status"], pair["tries"]) for pair in found} == {1: ("done", 2), 2: ("done", 1)}
    assert row["progress"] == {"done": 2} and len(finished) == 1
    assert fake.count("figures", "db-a") == 2  # each pair's side read once


async def test_a_pair_claimed_and_never_settled_is_not_read_for_ever(
    tables: Tables, fake: FakeEngine
) -> None:
    async with tables() as h:
        precheck_id = await checked(h, table_spec())
        async with h.ctx.pool.connection() as conn:
            await conn.execute("UPDATE ds.precheck_pairs SET tries = %s", (checking.TRIES,))
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
    assert fake.events == [] and (pair["verdict"], pair["tries"]) == ("not_read", checking.TRIES + 1)
    assert pair["line"] == "not checked: side a's read failed (NotSettled)"


async def test_fault_never_touches_a_run_or_the_sweep(
    harness: Harness,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pre-check that cannot be read, and a list of them that cannot be read, fail no run: the sweep goes
    on with its runs."""
    for key in ("K001", "K002", "K003"):
        service.both(key, doc(key), doc(key))
    spec_id = await harness.spec(spec_text())
    made_ = {"precheck_id": repo.new_precheck_id(), "source": "specs", "level": "columns", "request": {}}
    made_ |= {"skipped": 0, "evaluation": False, "created_by": TESTER.actor}
    pair = {"n": 1, "spec_id": spec_id, "label": "unit-runner", "target_a": "svc-a", "target_b": "svc-b"}
    async with harness.ctx.pool.connection() as conn:
        await repo.insert_precheck(conn, made_, [pair | {"relation_a": None, "relation_b": None}])
    calls: list[str] = []
    due = repo.prechecks_due

    async def broken(*_: Any) -> None:
        calls.append("check")
        raise RuntimeError("a bug that names public.accounts")

    async def unread(conn: repo.Conn) -> list[repo.Row]:
        calls.append("due")
        if calls.count("due") > 1:  # the first sweep reads them; the later ones cannot
            raise ValueError("a bug")
        return await due(conn)

    monkeypatch.setattr(checking, "check", broken)
    monkeypatch.setattr(repo, "prechecks_due", unread)
    with caplog.at_level(logging.WARNING):
        run_id = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
        run = await harness.finish(run_id)
    assert run["status"] == "completed" and run["error"] is None
    assert "check" in calls and calls.count("due") > 1
    logged = [record.getMessage() for record in caplog.records]
    assert f"table pre-check {made_['precheck_id']} pair 1 not settled: RuntimeError" in logged
    assert "due table pre-checks not read: ValueError" in logged
    assert "public.accounts" not in caplog.text  # a fault is logged by its class alone
    assert (await made(harness.ctx, str(made_["precheck_id"])))["finished_at"] is None


async def test_a_pair_no_run_could_read_is_refused_or_says_its_class(
    tables: Tables, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version whose target is no longer declared is refused as a run of it would be; a bug in the read
    is the pair's failure, by its class alone. Neither is read again, and no session is opened for the
    first."""
    async with tables() as h:
        precheck_id = await checked(h, table_spec())
        without = TargetRegistry({"db-a": h.ctx.targets.get("db-a")})
        await ended(replace(h.ctx, targets=without), precheck_id)
        [undeclared] = await pairs(h.ctx, precheck_id)
        assert fake.events == []

        def bug(*_: Any) -> None:
            raise KeyError("public.accounts")

        monkeypatch.setattr(table_sides, "issues", bug)
        precheck_id = await checked(h, table_spec())
        await ended(h.ctx, precheck_id)
        [broken] = await pairs(h.ctx, precheck_id)
    assert (undeclared["verdict"], undeclared["tries"]) == ("refused", 1)
    assert undeclared["line"] == "a run of this spec would be refused: target 'db-b' is not in the registry"
    assert (broken["verdict"], broken["tries"]) == ("not_read", 1)
    assert broken["result"]["lines"][:2] == [
        "not checked: side a's read failed (KeyError)", "not checked: side b's read failed (KeyError)",
    ]  # fmt: skip
    assert broken["result"]["sides"]["a"]["failed"] == {"session": "error"}


async def test_only_the_pairs_named_are_set_back(tables: Tables, fake: FakeEngine) -> None:
    """A pair a task of this worker still reads stays its own: only the lost ones are requeued."""
    async with tables() as h:
        precheck_id = await checked(h, table_spec("accounts"), table_spec("cards"))
        async with h.ctx.pool.connection() as conn:
            for n in (1, 2):
                assert await repo.claim_pair(conn, precheck_id, n) is not None
            assert await repo.requeue_prechecks(conn, [(precheck_id, 2), (precheck_id, 9)]) == 1
            due = await repo.prechecks_due(conn)
            assert [(row["n"], row["status"]) for row in due] == [(1, "running"), (2, "pending")]
            assert await repo.requeue_prechecks(conn, []) == 0 and await repo.requeue_prechecks(conn) == 1


async def counts(h: Harness) -> dict[str, int]:
    """How many rows each table of the store holds."""
    async with h.ctx.pool.connection() as conn:
        cur = await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'ds'")
        names = [name for (name,) in await cur.fetchall()]
        found = {}
        for name in names:
            found[name] = (await (await conn.execute(f"SELECT count(*) FROM ds.{name}")).fetchone())[0]  # type: ignore[index] # noqa: S608
    return found


async def test_writes_only_its_tables_and_audit(tables: Tables, fake: FakeEngine) -> None:
    fake.values["db-b", "rows", None] = "4"
    async with tables() as h:
        await ended(h.ctx, await checked(h, table_spec("users")))  # a worker's first sweep audits its own
        spec_ids = [await h.spec(table_spec(name)) for name in NAMES[:2]]
        before = await counts(h)
        made_ = await prechecks.create(h.ctx, {"spec_ids": spec_ids, "level": "distinct"}, TESTER)
        await ended(h.ctx, made_["precheck_id"])
        after = await counts(h)
        async with h.ctx.pool.connection() as conn:
            cur = await conn.execute("SELECT action FROM ds.audit_log ORDER BY id")
            actions = [action for (action,) in await cur.fetchall()]
    changed = {name: after[name] - before[name] for name in after if after[name] != before[name]}
    assert changed == {"prechecks": 1, "precheck_pairs": 2, "audit_log": 2, "audit_chain": 2}
    assert actions[-2:] == ["precheck.create", "precheck.finished"]  # no run, no licence pair, no event
    assert made_ == {
        "not": precheck.NOT, "precheck_id": made_["precheck_id"], "pairs": 2, "skipped": 0,
        "evaluation": False,
    }  # fmt: skip


PLANTED = ("ZEBRA-PLANTED-9731", "987654321", "987654324", "987654328", "4086-11-23", "planted@example.test")
SEED = """
    CREATE TABLE accounts (id int PRIMARY KEY, email text, note text, balance bigint, opened date);
    INSERT INTO accounts VALUES
        (1, 'planted@example.test', 'ZEBRA-PLANTED-9731', 987654321, '4086-11-23'),
        (2, 'b@x', 'n', 1, '2024-05-01'), (3, NULL, '', 2, '2024-05-02');
"""


async def test_planted_value_reaches_no_row_export_log_or_delivery(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On PostgreSQL itself: a text value, a maximum, a year and a sum the security policy protects are
    compared, and none of them is stored, exported or logged; counts and length ranges are."""
    dsn = databases(SEED)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE accounts_new AS TABLE accounts")
        conn.execute("ALTER TABLE accounts_new ADD PRIMARY KEY (id)")
        conn.execute("UPDATE accounts_new SET balance = 5 WHERE id = 2")
    (tmp_path / "dsn_tables").write_text(dsn)
    with caplog.at_level(logging.DEBUG):
        async with open_harness(database_targets(tmp_path / "dsn_tables")) as h:
            precheck_id = await checked(h, table_spec(b={"name": "accounts_new"}), level="distinct")
            await ended(h.ctx, precheck_id)
            [pair] = await pairs(h.ctx, precheck_id)
            row, read = await reports.loaded(h.ctx, precheck_id)
            exports = [reports.pairs_csv(row, read), reports.columns_csv(row, read)]
            exports += [str(reports.document(row, read))]
            async with h.ctx.pool.connection() as conn:
                stored = ""
                for name in await counts(h):
                    dump = f"SELECT coalesce(string_agg(t::text, ' '), '') FROM ds.{name} t"  # noqa: S608
                    stored += (await (await conn.execute(dump)).fetchone())[0]  # type: ignore[arg-type,index]
                deliveries = (await counts(h))["notify_deliveries"]
    figures = pair["result"]["columns"]
    assert pair["verdict"] == "values_differ" and pair["line"] == (
        "1 of 4 compared columns differ in a figure: balance (sum, min)"
    )
    assert figures["balance"]["figures"] == {
        "nulls": {"a": 0, "b": 0, "verdict": "same", "reason": None},
        "sum": {
            "verdict": "differs",
            "reason": "figures withheld: $.balance is protected by the spec's security policy",
        },
        "min": {"verdict": "differs"},
        "max": {"verdict": "same"},
        "distinct": {"a": 3, "b": 3, "verdict": "same"},
    }
    assert figures["note"]["figures"]["length"] == {"a": [0, 18], "b": [0, 18], "verdict": "same"}
    assert figures["email"]["figures"]["nulls"] == {"a": 1, "b": 1, "verdict": "same", "reason": None}
    assert figures["opened"]["figures"]["max"] == {"verdict": "same"}
    for text in (stored, caplog.text, *exports):
        assert not [value for value in PLANTED if value in text]
    assert "public.accounts_new" in stored and "balance" in exports[1] and deliveries == 0


SWAPPED = """
    CREATE TABLE accounts (id int PRIMARY KEY, holder text, balance numeric(12,2));
    INSERT INTO accounts VALUES (1, 'ann', 100.00), (2, 'bob', 250.50);
    CREATE TABLE accounts_new (id int PRIMARY KEY, holder text, balance numeric(12,2));
    INSERT INTO accounts_new VALUES (1, 'ann', 250.50), (2, 'bob', 100.00);
"""


async def test_agreeing_figures_are_not_parity(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
) -> None:
    """Two rows with swapped balances: every count, sum, bound and distinct count agrees, and a comparison
    of the same spec finds both rows different. A pre-check says where to look first, never that the
    tables hold the same rows."""
    (tmp_path / "dsn_tables").write_text(databases(SWAPPED))
    async with open_harness(database_targets(tmp_path / "dsn_tables")) as h:
        text = table_spec(b={"name": "accounts_new"}, security={"unlisted": "clear"})
        precheck_id = await checked(h, text, level="distinct")
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
        run = await h.finish(await create_run(h.ctx, await h.spec(text), "batch", who=TESTER))
    observed = "no difference observed in the checks made (level distinct): not parity"
    best = f"{observed}; run a comparison to compare rows"
    assert (pair["verdict"], pair["differing"], pair["line"]) == ("no_difference_observed", 0, best)
    balance = pair["result"]["columns"]["balance"]["figures"]
    assert balance["sum"] == {"a": "350.50", "b": "350.50", "verdict": "same", "reason": None}
    assert {figure: judged["verdict"] for figure, judged in balance.items()} == dict.fromkeys(
        ("nulls", "sum", "min", "max", "distinct"), "same"
    )
    assert pair["result"]["keys"]["a"] == {
        "rows": 2, "null_keys": 0, "duplicate_keys": 0, "rows_in_duplicates": 0, "primary": ["id"],
    }  # fmt: skip
    assert (run["counters"]["total"], run["counters"]["different"]) == (2, 2)


UNMET = """
    CREATE TABLE ledger (id numeric(10,2) PRIMARY KEY, v text);
    INSERT INTO ledger VALUES (1, 'x'), (2, 'y');
    CREATE TABLE ledger_new (id integer PRIMARY KEY, v text);
    INSERT INTO ledger_new VALUES (1, 'x'), (2, 'y');
"""


async def test_keys_whose_texts_never_meet_are_not_read_as_no_difference(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
) -> None:
    """Every figure agrees, but side a's key reads 1.00 where side b's reads 1: a comparison of the same
    spec finds no key of one side on the other, and the pre-check says so first, never ``no difference
    observed``."""
    (tmp_path / "dsn_tables").write_text(databases(UNMET))
    async with open_harness(database_targets(tmp_path / "dsn_tables")) as h:
        text = table_spec("ledger", b={"name": "ledger_new"}, security={"unlisted": "clear"})
        precheck_id = await checked(h, text, level="distinct")
        await ended(h.ctx, precheck_id)
        [pair] = await pairs(h.ctx, precheck_id)
        run = await h.finish(await create_run(h.ctx, await h.spec(text), "batch", who=TESTER))
    assert (pair["verdict"], pair["differing"], pair["line"]) == (
        "columns_not_compared",
        0,
        "key column id is numeric(10,2) on side a and integer on side b: a number key's text holds its"
        " scale, so no key of one side is found on the other: compare a view that casts it",
    )
    assert pair["result"]["columns"]["v"]["figures"]["distinct"] == {"a": 2, "b": 2, "verdict": "same"}
    assert (run["counters"]["match"], run["counters"]["by_outcome"]["NOT_FOUND_A"]) == (0, 2)


async def test_two_relations_paired_by_name_are_read_by_their_implied_plan(
    tables: Tables, fake: FakeEngine
) -> None:
    """A pair of two schemas' tables has no spec: each side is the relation of its name in its schema,
    read by the plan the two catalogues imply, with no probe; one of a target is read side a, then side b,
    on one session slot. A table gone since the schemas were listed, or a target no longer declared, is
    refused as a run of them would be."""
    fake.held["CARDS"] = replace(relation("CARDS", *ACCOUNTS.columns[1:]), schema="APP")
    fake.listed = {"db-a": [("CARDS", "table"), ("accounts", "table"), ("gone", "table")]}
    async with tables() as h:
        body = {"a": {"target": "db-a", "schema": "app"}, "b": {"target": "db-a"}, "level": "columns"}
        made_ = await prechecks.catalogue(h.ctx, body, TESTER)
        [row] = await ended(h.ctx, made_["precheck_id"])
        found = {pair["label"]: pair for pair in await pairs(h.ctx, made_["precheck_id"])}
        with pytest.raises(prechecks.Refused) as undrafted:  # a refused pair has nothing to draft (K12)
            await prechecks.draft(h.ctx, made_["precheck_id"], found["gone"]["n"])
    assert (undrafted.value.status, str(undrafted.value)) == (
        409, f"pair {found['gone']['n']} was not read: there is nothing to draft yet",
    )  # fmt: skip
    assert (row["source"], row["request"]["a"], row["request"]["b"]) == (
        "catalogue", {"target": "db-a", "schema": "app"}, {"target": "db-a", "schema": None},
    )  # fmt: skip
    cards, accounts, gone = found["cards"], found["accounts"], found["gone"]
    assert (cards["relation_a"], cards["relation_b"], cards["verdict"]) == (
        "CARDS",
        "CARDS",
        "no_difference_observed",
    )
    assert cards["result"]["names"]["a"]["keys"] == ["id"] and set(cards["result"]["columns"]) == {
        "email",
        "balance",
    }
    figures = cards["result"]["columns"]["balance"]["figures"]
    assert figures["sum"] == {
        "verdict": "same",
        "reason": "figures withheld: paired by name, no security policy",
    }
    assert fake.count("probe") == 0  # no spec, so no filter or key map to probe
    assert accounts["verdict"] == "no_difference_observed"
    assert (gone["verdict"], gone["line"]) == (
        "refused",
        "a run of these tables would be refused: side a: table or view app.gone not found or not visible",
    )
    # One target: both catalogues on side a's session, then side b's figures on a snapshot of its own;
    # nothing more once a table is refused.
    snapshots = [target for event, target, _ in fake.events if event == "snapshot"]
    assert snapshots == ["db-a"] * 5 and fake.most["db-a"] == 1


async def test_a_pair_by_name_whose_target_is_no_longer_declared_is_refused_unread(
    tables: Tables, fake: FakeEngine
) -> None:
    """A target removed from the registry after its schema was listed, before the worker read the pair: the
    pair is refused as a run of its tables would be, and no session is opened."""
    async with tables() as h:
        body = {"a": {"target": "db-a"}, "b": {"target": "db-b"}, "include": ["accounts"]}
        made_ = await prechecks.catalogue(h.ctx, body, TESTER)
        listed = len(fake.events)
        without = TargetRegistry({"db-a": h.ctx.targets.get("db-a")})
        await ended(replace(h.ctx, targets=without), made_["precheck_id"])
        [pair] = await pairs(h.ctx, made_["precheck_id"])
    assert (pair["verdict"], pair["tries"]) == ("refused", 1)
    assert pair["line"] == "a run of these tables would be refused: target db-b is not declared as a database"
    assert fake.events[listed:] == []
