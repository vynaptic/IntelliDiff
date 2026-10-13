"""A key table (a ``table`` key source with a target and a table of its own): its key columns give the keys
of a run between two services, a multi-part key's parts among them, each key once and in key order; rows
without a key, a CSV file's lines that are not rows, more keys than a sample may hold and whatever keeps it
from being planned or read refuse the read as a ValueError that holds no key; the read holds one slot of its
target's gate, and neither its plan nor a census is stored."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth.connectors import tables
from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.core import sampling
from deltasleuth.core.spec import TargetRegistry, compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import CompiledSpec
from deltasleuth.runner.keys import KeyAuthRefused, resolve
from deltasleuth.runner.runs import compare_one, create_run
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import Harness, spec_text
from .sampled import (
    Tables,
    audited,
    tables_fixture,  # noqa: F401 (the on_tables fixture)
)
from .sampled import keys as run_keys
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
AT = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z"
PARTS = ["region", "shelf", "code"]
# Five keys in six rows: one key holds two rows, one part holds a |, and one row is not live.
SHELVES = """
    CREATE TABLE shelf_keys (region text, shelf text, code text, live boolean);
    INSERT INTO shelf_keys VALUES
        ('us', 's-2', 't0003', true), ('eu', 's-1', 't0002', true), ('eu', 's-1', 't0001', true),
        ('eu', 's-1', 't0001', true), ('e|u', 's-3', 't0004', true), ('us', 's-9', 't0009', false);
"""
SHELF_KEYS = {"name": "shelf_keys", "keyColumns": PARTS}
SHELF = {"type": "composite", "parts": PARTS, "pattern": "^[A-Z0-9|-]+$", "normalize": ["upper"]}
ITEMS = (
    "CREATE TABLE item_keys (id text); INSERT INTO item_keys SELECT 'K00' || g FROM generate_series(1, 5) g;"
)
ITEM_KEYS = {"name": "item_keys", "keyColumns": ["id"]}
K2 = "the key source holds 1 row(s) without a key: leave them out with table.where"
K3 = "the key source holds over {:,} keys: narrow it with table.where"
M2 = "are not 3 parts joined by |, or hold a part that fails key.pattern after key.normalize"


def keyed(table: dict[str, Any], key: dict[str, Any] | None = None, *, execution: str = "") -> str:
    """The unit spec between its two services, its keys those of ``table`` on target db-a; ``key`` replaces
    members of its key."""
    doc = parse_yaml_text(spec_text(execution=execution))
    doc["key"] |= (key or {}) | {"source": {"kind": "table", "target": "db-a", "table": table}}
    return json.dumps(doc)


def compiled(h: Harness, text: str) -> CompiledSpec:
    return compile_spec(parse_yaml_text(text), targets=h.ctx.targets)


async def test_parts_from_columns(on_tables: Tables) -> None:
    """A key table's key columns are a multi-part key's parts, in ``key.parts`` order: a key several rows
    hold comes once, the keys in key order, each part through ``key.normalize`` and joined as a key is
    written; its filter leaves rows out. A key the rule refuses is named by its rank in that order."""
    _, harness = on_tables(SHELVES)
    async with harness as h:
        found, record = await resolve(h.ctx, compiled(h, keyed(SHELF_KEYS | {"where": "live"}, SHELF)))
        assert found == ["EU|S-1|T0001", "EU|S-1|T0002", "E\\|U|S-3|T0004", "US|S-2|T0003"]
        assert record is not None and re.fullmatch(AT, record.pop("at"))
        assert record == {
            "basis": "key_table", "target": "db-a", "relation": "public.shelf_keys", "rows": 5, "keys": 4,
        }  # fmt: skip
        unfiltered, record = await resolve(h.ctx, compiled(h, keyed(SHELF_KEYS, SHELF)))
        assert unfiltered == [*found, "US|S-9|T0009"] and (record or {})["rows"] == 6
        narrow = SHELF | {"pattern": "^[A-Z0-9-]+$"}  # the part that holds a | fails it: the third key
        with pytest.raises(ValueError) as refused:
            await resolve(h.ctx, compiled(h, keyed(SHELF_KEYS, narrow)))
        assert str(refused.value) == f"1 key(s) {M2}, or are over 1024 bytes or hold NUL (positions 3)"
        # A key of one part is its column's text: here the shelves, each once.
        one = {"pattern": "^S-[0-9]$"}
        shelves, record = await resolve(
            h.ctx, compiled(h, keyed(SHELF_KEYS | {"keyColumns": ["shelf"]}, one))
        )
        assert shelves == ["S-1", "S-2", "S-3", "S-9"] and (record or {})["keys"] == 4


async def test_rows_without_a_key_refuse(on_tables: Tables) -> None:
    """A row whose key column is NULL is no key: the read refuses it rather than leave it out unsaid, and
    the spec's filter is how such rows are left out."""
    dsn, harness = on_tables(SHELVES)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO shelf_keys VALUES ('eu', NULL, 't0007', true)")
    async with harness as h:
        with pytest.raises(ValueError) as refused:
            await resolve(h.ctx, compiled(h, keyed(SHELF_KEYS, SHELF)))
        assert str(refused.value) == K2
        left_out = SHELF_KEYS | {"where": "shelf IS NOT NULL"}
        found, _ = await resolve(h.ctx, compiled(h, keyed(left_out, SHELF)))
        assert len(found) == 5


async def test_lines_that_are_not_rows_refuse(on_tables: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CSV key file's lines that are not rows of its dialect are in no key list. Its plan counts them
    and the read refuses by that count, rather than leave their keys out unsaid: the file
    itself is e2e/test_files.py's."""
    _, harness = on_tables(SHELVES)
    plan = tables.key_plan

    async def counted(*args: Any) -> Any:  # as a files target's plan counts them
        return await plan(*args) | {"rejected": 1002, "kinds": {"columns": 1002}}

    monkeypatch.setattr(tables, "key_plan", counted)
    async with harness as h:
        with pytest.raises(ValueError) as refused:
            await resolve(h.ctx, compiled(h, keyed(SHELF_KEYS, SHELF)))
    assert str(refused.value) == (
        "the key source holds 1,002 line(s) that are not rows of its declared CSV dialect: fix the export or"
        " its csv declaration"
    )


async def test_over_the_limit_refuses_before_max_keys(
    on_tables: Tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key table holds at most as many keys as a sample (``sampling.LIMIT``): more refuse the run whatever
    ``maxKeys`` would have left of them, and ``maxKeys`` takes the lowest keys of a table within it."""
    assert K3.format(sampling.LIMIT) == "the key source holds over 1,000,000 keys: narrow it with table.where"
    _, harness = on_tables(ITEMS)
    async with harness as h:
        spec_id = await h.spec(keyed(ITEM_KEYS, execution="concurrency: 2, stop: {maxKeys: 2}"))
        monkeypatch.setattr(sampling, "LIMIT", 4)
        with pytest.raises(ValueError) as over:
            await create_run(h.ctx, spec_id, "batch", who=TESTER)
        assert str(over.value) == K3.format(4)
        monkeypatch.setattr(sampling, "LIMIT", 5)
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        run = await h.run(run_id)
        assert (run["keys_cut"], run["stop_reason"]) == (
            {"resolved": 5, "taken": 2},
            "stop_condition:maxKeys",
        )
        assert [row["key"] for row in await run_keys(h, run_id)] == ["K001", "K002"]


Failing = Callable[..., Any]


@pytest.fixture
def engine() -> Iterator[Failing]:
    """``engine(method, error, after=0)``: PostgreSQL's ``method`` raises ``error``, a census once it gave
    ``after`` batches; ``engine(method)`` restores every method."""
    real, patch = tables.ENGINES["postgresql"], pytest.MonkeyPatch()

    def fail(method: str, error: BaseException | None = None, after: int = 0) -> None:
        async def refused(*_: Any, **__: Any) -> Any:
            raise error  # type: ignore[misc]

        @asynccontextmanager
        async def unopened(*_: Any, **__: Any) -> AsyncIterator[Any]:
            raise error  # type: ignore[misc]
            yield

        async def cut(*args: Any) -> AsyncIterator[Any]:
            given = 0
            async for batch in type(real).census(real, *args):
                if given == after:
                    raise error  # type: ignore[misc]
                given += 1
                yield batch
            raise error  # type: ignore[misc]

        if error is None:
            patch.undo()
        else:
            patch.setattr(real, method, {"census": cut, "snapshot": unopened}.get(method, refused))

    yield fail
    patch.undo()


async def test_read_refusals_are_value_errors(
    on_tables: Tables, engine: Failing, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whatever keeps a key table from being planned or read is a ValueError, a run's refusal: a plan's in a
    table side's words, ``the key source`` where its side stands; a fault of its target by its code alone,
    never its text; a refused credential one a roll can tell (``KeyAuthRefused``)."""
    _, harness = on_tables(SHELVES)

    async def refusal(h: Harness, table: dict[str, Any] = SHELF_KEYS, **options: Any) -> ValueError:
        with pytest.raises(ValueError) as refused:
            await resolve(h.ctx, compiled(h, keyed(table, SHELF)), **options)
        return refused.value

    async with harness as h:
        # A plan's refusals: the relation, a column, the filter.
        assert str(await refusal(h, SHELF_KEYS | {"name": "nope"})) == (
            "the key source: table or view nope not found or not visible"
        )
        assert str(await refusal(h, SHELF_KEYS | {"keyColumns": ["region", "shelf", "sku"]})) == (
            "the key source: column 'sku' is not in public.shelf_keys"
        )
        unbound = await refusal(h, SHELF_KEYS | {"where": "live = 7"})
        assert str(unbound).startswith("the key source: the database refuses the filter or key map: ")
        # An export that is not ready stays what a schedule waits for, its code kept.
        engine("describe", tables.NotReady("stale", "side a: its newest file is older than maxAge allows"))
        stale = await refusal(h)
        assert isinstance(stale, tables.NotReady) and stale.code == "stale"
        assert str(stale) == "the key source: its newest file is older than maxAge allows"
        engine("describe")
        # A fault of the target, by its code alone.
        faults = [
            ("census", psycopg.errors.QueryCanceled("x"), "timed out after 3600 s (QueryCanceled)"),
            ("census", tables.StatementTimeout("MySQL 3024"), "timed out after 3600 s (MySQL 3024)"),
            ("census", tables.SnapshotLost("ORA-01555"), "could not be held in one snapshot (ORA-01555)"),
            ("null_keys", tables.TargetRefused("MySQL 1105"), "could not be read (MySQL 1105)"),
            ("census", psycopg.OperationalError("the server at db.bank.example closed"), "could not be read"
             " (OperationalError)"),
            ("snapshot", UnsafeTargetError("10.1.2.3 is a private address"), "could not be read"
             " (UnsafeTargetError)"),
            ("snapshot", tables.TargetAuthRefused("ORA-01017"), "could not be read: the target refused the"
             " credential (ORA-01017)"),
            ("connect", tables.TargetAuthRefused("MySQL 1045", "the credential was refused"), "could not be"
             " read: the target refused the credential (MySQL 1045)"),
        ]  # fmt: skip
        for method, error, said in faults:
            engine(method, error)
            found = await refusal(h)
            assert str(found) == f"the key source {said}", method
            assert type(found) is (
                KeyAuthRefused if isinstance(error, tables.TargetAuthRefused) else ValueError
            )
            engine(method)

        # A read past its bound, as a statement past it.
        async def slow(*_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(30)
            yield []

        monkeypatch.setattr(tables.ENGINES["postgresql"], "census", slow)
        assert str(await refusal(h, bound_s=1)) == "the key source timed out after 1 s (TimeoutError)"
        monkeypatch.undo()
        # A file's rows guard: its reader met other rows than its plan counted.
        plan = tables.key_plan

        async def counted(*args: Any) -> Any:
            return await plan(*args) | {"selected": 99}

        monkeypatch.setattr(tables, "key_plan", counted)
        moved = "the key source could not be read (6 rows were read where the plan counted 99)"
        assert str(await refusal(h)) == moved
        monkeypatch.undo()
        # A target that left the registry, and one that refuses the connect: its endpoint for whoever creates
        # the run, its id alone in ``brief``.
        gone = replace(h.ctx, targets=TargetRegistry({"svc-a": h.ctx.targets.get("svc-a")}))
        with pytest.raises(ValueError) as unknown:
            await resolve(gone, compiled(h, keyed(SHELF_KEYS, SHELF)))
        assert str(unknown.value) == "the key source could not be read (UnknownTargetError)"
        (tmp_path / "dsn_tables").write_text("postgresql://reader@127.0.0.1:1/keys?connect_timeout=2")
        unconnected = await refusal(h)
        assert isinstance(unconnected, tables.Unconnected)
        assert str(unconnected).startswith("the key source: cannot connect to target db-a: ")
        assert unconnected.brief == "the key source: cannot connect to target db-a"


async def test_one_gate_slot_is_held(on_tables: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
    """The read takes one slot and one tick of its target's gate, the process's one for the target, and
    holds the slot until its last statement ended."""
    _, harness = on_tables(SHELVES)
    real = tables.ENGINES["postgresql"]
    census, null_keys = type(real).census, type(real).null_keys
    held: list[bool] = []
    async with harness as h:
        gate = h.ctx.gates["db-a"] = TargetGate(1000, 1)  # one slot: held is locked
        ticks = 0

        class Budget:
            async def acquire(self) -> None:
                nonlocal ticks
                ticks += 1

        async def stream(*args: Any) -> AsyncIterator[Any]:
            async for batch in census(real, *args):
                held.append(gate.semaphore.locked())
                yield batch

        async def count(*args: Any) -> int:
            held.append(gate.semaphore.locked())
            return await null_keys(real, *args)

        monkeypatch.setattr(gate, "limiter", Budget())
        monkeypatch.setattr(real, "census", stream)
        monkeypatch.setattr(real, "null_keys", count)
        await resolve(h.ctx, compiled(h, keyed(SHELF_KEYS, SHELF)))
        assert (held, ticks, gate.semaphore.locked()) == ([True, True], 1, False)
        monkeypatch.setattr(real, "null_keys", lambda *_: 1 / 0)  # a read that fails gives its slot back
        with pytest.raises(ZeroDivisionError):
            await resolve(h.ctx, compiled(h, keyed(SHELF_KEYS, SHELF)))
        assert (ticks, gate.semaphore.locked()) == (2, False)


async def test_no_plan_or_census_is_stored(on_tables: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key table is a key list, not a census of a side: its run stores no plan of it and counts no side,
    and each read plans again; the run's creation audits what the read met. Keys given with a run, and a
    compare of one key, never read it."""
    _, harness = on_tables(SHELVES)
    plan, planned = tables.key_plan, 0

    async def counted(*args: Any) -> Any:
        nonlocal planned
        planned += 1
        return await plan(*args)

    monkeypatch.setattr(tables, "key_plan", counted)
    async with harness as h:
        spec_id = await h.spec(keyed(SHELF_KEYS, SHELF))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        again = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        run = await h.run(run_id)
        async with h.ctx.pool.connection() as conn:
            keys, _ = await repo.list_keys(conn, run_id, 1)
        ((_, created),) = await audited(h, run_id, "create_run")
        ((_, second),) = await audited(h, again, "create_run")
        assert await audited(h, run_id, "run.census") == []
        given = await create_run(h.ctx, spec_id, "batch", keys=["eu|s-1|t0001"], who=TESTER)
        one = await compare_one(h.ctx, spec_id, "eu|s-1|t0001", who=TESTER)
        assert ((await h.run(given))["params"]["keys_source"], one.key, planned) == (
            "explicit",
            "EU|S-1|T0001",
            2,
        )
    params = run["params"]
    assert (params["keys_source"], params["tables"], run["census"], planned) == ("table", None, None, 2)
    assert "pinned" not in params and "shelf_keys" not in json.dumps(params)
    read = created["key_source"]
    assert re.fullmatch(AT, read.pop("at")) and read == {
        "basis": "key_table", "target": "db-a", "relation": "public.shelf_keys", "rows": 6, "keys": 5,
    }  # fmt: skip
    assert (created["keys"], len(keys)) == (5, 5)
    assert created["plan_sha256"] == second["plan_sha256"]  # the plan a pack holds knows no key table
