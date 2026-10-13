"""Migration 016 (docs/mvp/PRECHECKS.md §8) over a populated 015 store with runs: the two pre-check tables,
each of their checks and the cascade, ``ds.runs.guard`` added without rewriting or scanning a table, and the
repo functions of a pre-check: its pairs claimed and settled once, a target's pairs settled in one
statement, a cancel, the end, the worst-first list that reads no result, pruning, and the schema guard's
record and basis."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.store import repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.unit.runner.conftest import spec_text

pytestmark = pytest.mark.db

PC = "PC-" + "0" * 26
RUN = """INSERT INTO ds.runs (run_id, spec_id, mode, status, params, started_at)
         VALUES (%s, 1, %s, 'running', %s, now())"""
ROW = """INSERT INTO ds.prechecks (precheck_id, source, level, request, pairs, created_by)
         VALUES (%(precheck_id)s, %(source)s, %(level)s, %(request)s, %(pairs)s, 'local:tester@test')"""
PAIR = """INSERT INTO ds.precheck_pairs (precheck_id, n, spec_id, label, target_a, target_b, status,
              verdict, rank, line, result)
          VALUES (%(precheck_id)s, %(n)s, %(spec_id)s, 'unit-runner', 'db-a', 'db-b', %(status)s,
              %(verdict)s, %(rank)s, %(line)s, %(result)s)"""
READ = {"verdict": "rows_differ", "rank": 4, "differing": 1, "line": "rows differ", "rows_a": 3, "rows_b": 2}
UNREAD = ("not checked: side a could not be reached", {"format": 1, "sides": {"a": {"failed": {"a": "x"}}}})


def row(**fields: Any) -> dict[str, Any]:
    return {
        "precheck_id": repo.new_precheck_id(), "source": "specs", "level": "columns",
        "request": Jsonb({"spec_ids": [1]}), "pairs": 1,
    } | fields  # fmt: skip


def pair(**fields: Any) -> dict[str, Any]:
    return {
        "precheck_id": PC, "n": 1, "spec_id": 1, "status": "pending", "verdict": None, "rank": None,
        "line": None, "result": None,
    } | fields  # fmt: skip


def made(precheck_id: str = PC, **fields: Any) -> dict[str, Any]:
    """A pre-check as ``repo.insert_precheck`` takes it."""
    return {
        "precheck_id": precheck_id, "source": "specs", "level": "columns", "request": {"spec_ids": [1]},
        "skipped": 0, "evaluation": False, "created_by": "local:tester@test",
    } | fields  # fmt: skip


def pairs(count: int, **fields: Any) -> list[dict[str, Any]]:
    """``count`` pending pairs as ``repo.insert_precheck`` takes them, from db-a to db-b."""
    return [
        {"n": n, "spec_id": 1, "label": f"pair-{n:03}", "target_a": "db-a", "relation_a": None,
         "target_b": "db-b", "relation_b": None} | fields
        for n in range(1, count + 1)
    ]  # fmt: skip


async def _all(conn: repo.Conn, sql: str, params: Any = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


@pytest.fixture
async def stored(empty_pool: AsyncConnectionPool) -> AsyncConnectionPool:
    """A 015 store holding a spec and three runs, then migrated to 016."""
    assert await migrate(empty_pool, upto=15) == list(range(1, 16))
    body = parse_yaml_text(spec_text())
    tables = (
        "SELECT relname, relfilenode FROM pg_class WHERE relnamespace = 'ds'::regnamespace AND relkind = 'r'"
    )
    triggers = "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname"
    functions = "SELECT count(*) FROM pg_proc WHERE pronamespace = 'ds'::regnamespace"
    constraints = "SELECT conname FROM pg_constraint WHERE conrelid = 'ds.runs'::regclass ORDER BY conname"
    plan = {"a": {"columns": {"email": "email"}}, "b": {"columns": {"email": "email"}}}
    async with empty_pool.connection() as conn:
        await conn.execute(
            """INSERT INTO ds.specs (spec_id, name, version, content_hash, body, created_by)
               VALUES (1, 'unit-runner', '1.0.0', %s, %s, 'local:tester@test')""",
            (content_hash(body), Jsonb(body)),
        )
        for n, (mode, params) in enumerate([("batch", {}), ("batch", {"tables": plan}), ("one_shot", {})]):
            await conn.execute(RUN, ("R-" + str(n) * 26, mode, Jsonb(params)))  # type: ignore[arg-type]
        before, fired = dict(await _all(conn, tables)), await _all(conn, triggers)
        made_, held = await _all(conn, functions), await _all(conn, constraints)
    assert await migrate(empty_pool) == [16, 17]
    async with empty_pool.connection() as conn:
        after = dict(await _all(conn, tables))
        assert {name: after[name] for name in before} == before  # no table is rewritten: not ds.runs
        assert set(after) - set(before) == {"prechecks", "precheck_pairs"}
        assert await _all(conn, triggers) == fired and await _all(conn, functions) == made_
        assert await _all(conn, "SELECT count(*), count(guard) FROM ds.runs") == [(3, 0)]
        # Nor scanned: the column is nullable, has no default and brings no constraint to validate.
        described = await _all(
            conn,
            """SELECT format_type(atttypid, atttypmod), attnotnull, atthasdef FROM pg_attribute
               WHERE attrelid = 'ds.runs'::regclass AND attname = 'guard'""",
        )
        assert described == [("jsonb", False, False)] and await _all(conn, constraints) == held
    return empty_pool


def test_the_migration_is_the_contract_s() -> None:
    """§8's statements, word for word, as the contract prints them."""
    contract = (MIGRATIONS.parents[2] / "docs" / "mvp" / "PRECHECKS.md").read_text()
    printed = contract.split("```sql\n", 1)[1].split("```", 1)[0]
    assert (MIGRATIONS / "016_prechecks.sql").read_text() == printed
    assert printed.rstrip().endswith("ALTER TABLE ds.runs ADD COLUMN guard jsonb;")


async def test_indexes(stored: AsyncConnectionPool) -> None:
    async with stored.connection() as conn:
        found = dict(
            await _all(
                conn,
                """SELECT indexname, indexdef FROM pg_indexes
                   WHERE schemaname = 'ds' AND tablename IN ('prechecks', 'precheck_pairs')""",
            )
        )
        runs = await _all(conn, "SELECT count(*) FROM pg_indexes WHERE indexdef LIKE '%%guard%%'")
    assert sorted(found) == [
        "precheck_pairs_due_idx", "precheck_pairs_pkey", "precheck_pairs_worst_idx", "prechecks_created_idx",
        "prechecks_open_idx", "prechecks_pkey",
    ]  # fmt: skip
    assert found["prechecks_open_idx"].endswith("(precheck_id) WHERE (finished_at IS NULL)")
    assert found["prechecks_created_idx"].endswith("(created_at)")
    assert "(precheck_id, n) WHERE (status = ANY" in found["precheck_pairs_due_idx"]
    assert found["precheck_pairs_worst_idx"].endswith("(precheck_id, rank, differing DESC, label, n)")
    assert runs == [(0,)]  # ds.runs.guard is indexed by nothing


async def test_each_check_of_a_precheck(stored: AsyncConnectionPool) -> None:
    async with stored.connection() as conn:
        await conn.execute(ROW, row(precheck_id=PC))  # type: ignore[arg-type]
        await conn.execute(ROW, row(source="catalogue", level="schema", pairs=500))  # type: ignore[arg-type]
    bad = [
        {"precheck_id": "PC-short"}, {"precheck_id": "R-" + "0" * 26}, {"source": "files"},
        {"level": "values"}, {"request": Jsonb([1])}, {"pairs": 0}, {"pairs": 501},
    ]  # fmt: skip
    for fields in bad:
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(ROW, row() | fields)  # type: ignore[arg-type]
    for change in ("cancelled_at = now()", "cancelled_by = 'local:tester@test'"):
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(f"UPDATE ds.prechecks SET {change}")  # type: ignore[arg-type]  # noqa: S608
    async with stored.connection() as conn:  # cancelled by someone at some time, and ended: each holds
        await conn.execute(
            "UPDATE ds.prechecks SET cancelled_at = now(), cancelled_by = 'x', finished_at = now()"
        )
        defaults = await _all(conn, "SELECT skipped, evaluation FROM ds.prechecks")
    assert defaults == [(0, False), (0, False)]


async def test_each_check_of_a_pair(stored: AsyncConnectionPool) -> None:
    done = {"status": "done", "verdict": "refused", "rank": 2, "line": "refused", "result": Jsonb({})}
    async with stored.connection() as conn:
        await conn.execute(ROW, row(precheck_id=PC))  # type: ignore[arg-type]
        await conn.execute(PAIR, pair())  # type: ignore[arg-type]
        await conn.execute(PAIR, pair(n=2, spec_id=None, **done))  # type: ignore[arg-type]
        # A cancelled pair says why on its line, with no verdict.
        cancelled = pair(n=3, status="cancelled", line="not checked: the pre-check was cancelled")
        await conn.execute(PAIR, cancelled)  # type: ignore[arg-type]
        assert await _all(conn, "SELECT status, tries FROM ds.precheck_pairs WHERE n = 1") == [("pending", 0)]
    bad = [
        {"n": 0}, {"status": "failed"}, {"verdict": "identical"}, {"verdict": "matched"},
        {"result": Jsonb([])}, {"status": "done"}, done | {"status": "running"},
        *({**done, name: None} for name in ("verdict", "rank", "line", "result")),
    ]  # fmt: skip
    for fields in bad:
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(PAIR, pair(n=9) | fields)  # type: ignore[arg-type]
    async with stored.connection() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(PAIR, pair())  # type: ignore[arg-type]
    for fields in ({"spec_id": 99}, {"precheck_id": "PC-" + "9" * 26}):
        async with stored.connection() as conn:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                await conn.execute(PAIR, pair(n=9) | fields)  # type: ignore[arg-type]
    async with stored.connection() as conn:  # the cascade: a pre-check goes with its pairs
        await conn.execute("DELETE FROM ds.prechecks WHERE precheck_id = %s", (PC,))
        assert await _all(conn, "SELECT count(*) FROM ds.precheck_pairs") == [(0,)]


async def test_pairs_are_claimed_and_settled_once(stored: AsyncConnectionPool) -> None:
    result = {"format": 1, "level": "columns", "lines": ["rows differ"], "sides": {}}
    async with stored.connection() as conn:
        assert (await repo.insert_precheck(conn, made(), pairs(3)))["pairs"] == 3
        assert await repo.count_open_prechecks(conn, lock=True) == 1
        due = await repo.prechecks_due(conn)
        assert [(d["precheck_id"], d["n"], d["status"], d["tries"]) for d in due] == [
            (PC, n, "pending", 0) for n in (1, 2, 3)
        ]
        assert (due[0]["target_a"], due[0]["target_b"]) == ("db-a", "db-b")
        claimed = await repo.claim_pair(conn, PC, 1)
        assert claimed is not None and "result" not in claimed
        assert (claimed["tries"], claimed["source"], claimed["level"]) == (1, "specs", "columns")
        assert (claimed["spec_id"], claimed["label"]) == (1, "pair-001")
        assert await repo.claim_pair(conn, PC, 1) is None and await repo.claim_pair(conn, PC, 9) is None
        assert not await repo.save_pair(conn, PC, 2, result=result, **READ)  # pending: not this task's
        assert await repo.save_pair(conn, PC, 1, result=result, **READ)
        assert not await repo.save_pair(conn, PC, 1, result=result, **READ)  # settled once
        assert await repo.finish_precheck(conn, PC) is None  # two pairs are open
        # A worker that takes the lock finds another's running pair, which is read again.
        assert await repo.claim_pair(conn, PC, 2) is not None and await repo.requeue_prechecks(conn) == 1
        assert not await repo.save_pair(conn, PC, 2, result=result, **READ)  # the first task's write
        again = await repo.claim_pair(conn, PC, 2)
        assert again is not None and again["tries"] == 2
        assert await repo.save_pair(conn, PC, 2, result=result, **READ | {"verdict": "refused", "rank": 2})
        saved = await repo.precheck_pair(conn, PC, 1)
        assert saved is not None and saved["result"] == result and saved["finished_at"] is not None
        assert (saved["status"], saved["rows_a"], saved["rows_b"]) == ("done", 3, 2)
        found = await repo.get_precheck(conn, PC)
        assert found is not None and found["finished_at"] is None
        assert (found["progress"], found["verdicts"]) == (
            {"done": 2, "pending": 1},
            {"rows_differ": 1, "refused": 1},
        )
        assert found["created_by_name"] == "local:tester@test" and found["now"] >= found["created_at"]


async def test_a_target_s_pending_pairs_are_settled_in_one_statement(stored: AsyncConnectionPool) -> None:
    other, ended = "PC-" + "1" * 26, "PC-" + "2" * 26
    mixed = pairs(4)
    mixed[1] |= {"target_a": "db-c"}  # its side b is on db-b
    mixed[2] |= {"target_b": "db-c"}
    mixed[3] |= {"target_a": "db-c", "target_b": "db-c"}
    async with stored.connection() as conn:
        await repo.insert_precheck(conn, made(), mixed)
        await repo.insert_precheck(conn, made(other), pairs(2))
        await repo.insert_precheck(conn, made(ended), pairs(1))
        assert await repo.cancel_precheck(conn, ended, "local:tester@test", "cancelled") is not None
        assert await repo.claim_pair(conn, other, 1) is not None  # running: its own read settles it
        line_b, result_b = "not checked: side b", {"format": 1, "sides": {"b": {"failed": {"b": "y"}}}}
        unread = (line_b, result_b)
        only = await repo.settle_target(conn, "db-b", "not_read", 3, UNREAD, unread, precheck_id=PC)
        assert [(s["precheck_id"], s["n"]) for s in only] == [(PC, 1), (PC, 2)]
        settled_ = "SELECT n, line, verdict, rank, differing FROM ds.precheck_pairs WHERE status = 'done'"
        assert sorted(await _all(conn, settled_)) == [(n, line_b, "not_read", 3, 0) for n in (1, 2)]
        every = await repo.settle_target(conn, "db-a", "not_read", 3, UNREAD, unread)
        assert sorted((s["precheck_id"], s["n"]) for s in every) == [(PC, 3), (other, 2)]
        settled = await repo.precheck_pair(conn, PC, 3)
        assert settled is not None and (settled["line"], settled["result"]) == UNREAD
        assert await repo.settle_target(conn, "db-a", "not_read", 3, UNREAD, UNREAD) == []
        assert await repo.finish_precheck(conn, PC) is None  # pair 4 names neither target
        assert await repo.claim_pair(conn, PC, 4) is not None
        assert await repo.save_pair(conn, PC, 4, result={"format": 1}, **READ)
        finished = await repo.finish_precheck(conn, PC)
        assert finished is not None and finished["seconds"] >= 0
        assert (finished["pairs"], finished["verdicts"]) == (4, {"not_read": 3, "rows_differ": 1})
        assert finished["failed"] == {"x": 1, "y": 2}  # the pairs each code was met in
        assert await repo.finish_precheck(conn, PC) is None  # once
        assert await repo.claim_pair(conn, PC, 4) is None
        assert await repo.count_open_prechecks(conn) == 1


async def test_a_cancel_ends_the_precheck_and_its_open_pairs(stored: AsyncConnectionPool) -> None:
    line = "not checked: the pre-check was cancelled"
    async with stored.connection() as conn:
        await repo.insert_precheck(conn, made(), pairs(4))
        assert [await repo.claim_pair(conn, PC, n) is not None for n in (1, 2)] == [True, True]
        assert await repo.save_pair(conn, PC, 1, result={"format": 1}, **READ)
        cancelled = await repo.cancel_precheck(conn, PC, "local:tester@test", line)
        assert cancelled is not None and cancelled["cancelled_at"] is not None
        assert (cancelled["pending"], cancelled["running"]) == (2, 1)
        assert await repo.cancel_precheck(conn, PC, "local:tester@test", line) is None  # it has ended
        assert await repo.cancel_precheck(conn, "PC-" + "9" * 26, "local:tester@test", line) is None
        assert not await repo.save_pair(conn, PC, 2, result={"format": 1}, **READ)  # its task's late write
        assert await repo.prechecks_due(conn) == [] and await repo.count_open_prechecks(conn) == 0
        assert await repo.requeue_prechecks(conn) == 0 and await repo.finish_precheck(conn, PC) is None
        found = await repo.get_precheck(conn, PC)
        assert found is not None and found["progress"] == {"done": 1, "cancelled": 3}
        assert found["cancelled_by"] == "local:tester@test" and found["finished_at"] == found["cancelled_at"]
        listed, _ = await repo.precheck_pairs(conn, PC)
        said = [(p["n"], p["status"], p["line"]) for p in listed]
        assert said == [(1, "done", "rows differ"), *((n, "cancelled", line) for n in (2, 3, 4))]


async def test_pairs_are_listed_worst_first_without_their_result(stored: AsyncConnectionPool) -> None:
    settled = [
        {"n": 1, "verdict": "no_difference_observed", "rank": 10, "differing": 0},
        {"n": 2, "verdict": "values_differ", "rank": 7, "differing": 2},
        {"n": 3, "verdict": "values_differ", "rank": 7, "differing": 5},
        {"n": 4, "verdict": "unpaired", "rank": 1, "differing": 0},
    ]
    made_ = pairs(6)
    for found in settled:  # pairs 5 and 6 stay pending
        made_[found["n"] - 1] |= found | {"line": found["verdict"], "result": {"format": 1, "big": "x" * 100}}
    made_[4]["label"], made_[5]["label"] = "zeta", "alpha"
    async with stored.connection() as conn:
        await repo.insert_precheck(conn, made(source="catalogue"), made_)
        page, cursor = await repo.precheck_pairs(conn, PC, limit=4)
        assert [p["n"] for p in page] == [4, 3, 2, 1] and cursor == "4"
        assert all("result" not in p for p in page) and page[0]["finished_at"] is not None
        rest, end = await repo.precheck_pairs(conn, PC, limit=4, cursor=cursor)
        assert [p["n"] for p in rest] == [6, 5] and end is None  # not settled: last, by label
        differing, _ = await repo.precheck_pairs(conn, PC, verdict="values_differ")
        assert [p["n"] for p in differing] == [3, 2]
        with pytest.raises(ValueError, match="malformed cursor"):
            await repo.precheck_pairs(conn, PC, cursor="1.2")
        explained = await _all(
            conn,
            f"""EXPLAIN SELECT {repo._PAIR_COLUMNS} FROM ds.precheck_pairs WHERE precheck_id = '{PC}'
                ORDER BY rank, differing DESC, label, n LIMIT 51""",  # type: ignore[arg-type]  # noqa: S608
        )
        assert "precheck_pairs_worst_idx" in "\n".join(line for (line,) in explained)
        newest = repo.new_precheck_id()
        await repo.insert_precheck(conn, made(newest), pairs(1))
        listed, cursor = await repo.list_prechecks(conn, limit=1)
        assert [p["precheck_id"] for p in listed] == [newest] and cursor == newest
        older, end = await repo.list_prechecks(conn, limit=1, cursor=cursor)
        assert [p["precheck_id"] for p in older] == [PC] and end is None
        assert older[0]["verdicts"] == {"no_difference_observed": 1, "values_differ": 2, "unpaired": 1}
        assert older[0]["progress"] == {"done": 4, "pending": 2} and "result" not in older[0]


async def test_pruning_deletes_old_prechecks_and_their_pairs(stored: AsyncConnectionPool) -> None:
    ids = ["PC-" + str(n) * 26 for n in range(4)]
    async with stored.connection() as conn:
        for age, precheck_id in zip((200, 100, 91, 89), ids, strict=True):
            await repo.insert_precheck(conn, made(precheck_id), pairs(2))
            await conn.execute(
                "UPDATE ds.prechecks SET created_at = now() - %s * interval '1 day' WHERE precheck_id = %s",
                (age, precheck_id),
            )
        assert await repo.prune_prechecks(conn, 90, limit=2) == 2  # the oldest first, a batch at a time
        assert await _all(conn, "SELECT precheck_id FROM ds.prechecks ORDER BY 1") == [(ids[2],), (ids[3],)]
        assert await repo.prune_prechecks(conn, 90) == 1 and await repo.prune_prechecks(conn, 90) == 0
        assert await _all(conn, "SELECT DISTINCT precheck_id FROM ds.precheck_pairs") == [(ids[3],)]
        assert await _all(conn, "SELECT count(*) FROM ds.runs") == [(3,)]


async def test_the_schema_guard_s_record_and_basis(stored: AsyncConnectionPool) -> None:
    old, planned_, one_shot = ("R-" + str(n) * 26 for n in range(3))
    new = "R-" + "9" * 26
    guard = {"format": 1, "mode": "finding", "baseline": True, "basis": None}
    async with stored.connection() as conn:
        # No run of the version has a guard: the newest batch or soak run that stored a plan.
        basis = await repo.guard_basis(conn, 1, new)
        assert basis is not None and (basis["run_id"], basis["guard"]) == (planned_, None)
        assert basis["tables"]["a"]["columns"] == {"email": "email"}
        assert await repo.guard_basis(conn, 1, planned_) is None  # the run before it stored none
        assert await repo.guard_basis(conn, 2, new) is None
        assert await repo.set_guard(conn, old, guard, cycle=1)
        assert not await repo.set_guard(conn, old, guard | {"mode": "pause"}, cycle=2)  # another cycle now
        assert not await repo.set_guard(conn, new, guard)
        found = await repo.get_run(conn, old)
        assert found is not None and found["guard"] == guard  # the sweep reads it with the run
        basis = await repo.guard_basis(conn, 1, new)
        assert basis is not None and (basis["run_id"], basis["guard"]) == (old, guard)
        # A run created paused and not accepted is no baseline: it hides nothing from the next run.
        assert await repo.set_guard(conn, planned_, guard | {"baseline": False})
        assert await repo.set_guard(conn, one_shot, guard)
        assert (await repo.guard_basis(conn, 1, new))["run_id"] == old  # type: ignore[index]
        assert await repo.set_guard(conn, old, guard | {"baseline": False})
        # Guards exist, none a baseline: the newest stored plan of a run the guard did not create paused,
        # unaccepted (one it adopted, or whose look at its creation failed, holds no basis either).
        basis = await repo.guard_basis(conn, 1, new)
        assert basis is not None and (basis["run_id"], basis["tables"]["a"]["columns"]) == (
            planned_,
            {"email": "email"},
        )
        assert await repo.set_guard(conn, planned_, guard | {"baseline": False, "basis": {"run_id": old}})
        assert await repo.guard_basis(conn, 1, new) is None  # its only plan is a created-paused run's
        adopted = guard | {"baseline": False, "basis": {"run_id": old}, "adopted": 3}
        assert await repo.set_guard(conn, planned_, adopted)
        assert (await repo.guard_basis(conn, 1, new))["run_id"] == planned_  # type: ignore[index]
        listed, _ = await repo.list_runs(conn, spec_id=1)
        assert all("guard" in run for run in listed)
