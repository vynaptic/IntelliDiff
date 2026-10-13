"""A hash sample's frame (docs/mvp/SAMPLING.md §3): a table side's population in one snapshot (NULL keys
skipped, a key's several rows once, keys out of order refused, no temporary table), a listing through the
scripted service, a union of both (origins, the first source's strata), a key listed twice on adjacent or
distant pages of either source (W2), unfit keys counted and never shown, each side's census record, and no
key census or census-gated lease for a sampled cycle."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
import yaml
from psycopg import sql

from deltasleuth.connectors import tables
from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.connectors.postgres import engine as postgresql
from deltasleuth.connectors.postgres import queries, session
from deltasleuth.connectors.tables import Plan
from deltasleuth.core import sampling
from deltasleuth.runner import census, sample
from deltasleuth.runner.runs import create_run
from deltasleuth.store import repo
from tests.conftest import TESTER, MySQLServer, OracleServer

from .conftest import TARGETS, Harness, OpenHarness, ScriptedService, doc
from .sampled import (
    CUSTOMER_KEYS,
    DUPLICATED,
    KEYS,
    Tables,
    audited,
    bucket,
    drawn,
    halves,
    keys,
    listed,
    listed_source,
    segments,
    served_source,
    table_source,
    tabled,
    tables_fixture,  # noqa: F401 (the on_tables fixture)
)
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
AT = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z"


def _timed(record: dict[str, Any], *names: str) -> dict[str, Any]:
    """A census record without its times, each checked."""
    for name in names:
        assert re.fullmatch(AT, record[name])
    return {name: value for name, value in record.items() if name not in names}


async def _draw(h: Harness, spec: str) -> repo.Row:
    """Create a run of ``spec`` and draw its sample, no worker running."""
    run_id = await create_run(h.ctx, await h.spec(spec), "batch", who=TESTER)
    await sample.draw(h.ctx, run_id)
    return await h.run(run_id)


def _counting(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> list[Any]:
    calls: list[Any] = []
    real = getattr(module, name)

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


async def test_a_table_is_read_in_one_snapshot_its_several_rows_of_a_key_once(
    on_tables: Tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = _counting(monkeypatch, session, "snapshot")
    sessions = _counting(monkeypatch, sample, "_session")
    _, harness = on_tables(DUPLICATED)
    async with harness as h:
        run = await _draw(h, tabled())
        stored = await drawn(h, run["run_id"])
        labelled = {k["key"]: k["stratum"] for k in await keys(h, run["run_id"])}
    assert (len(snapshots), sessions) == (1, [])  # census order makes a table's keys distinct
    assert stored is not None and stored["frame"] == {
        "keys": 20,
        "origin": {"a": 20, "b": 0, "both": 0},
        "unfit": 0,
    }
    record = run["census"]["a"]
    assert _timed(record, "at") == {
        "basis": "frame", "target": "db-a", "relation": "public.customers", "where": None, "rows": 22,
        "skipped": 1, "keys": 20, "unfit": 0,
    }  # fmt: skip
    assert "b" not in run["census"]  # no source lists side b
    at = census._moment(record["at"])
    assert (
        census.described(record)
        == f"its population read to its end in 1 snapshot at {at} as the sampling frame"
    )
    assert sorted(labelled) == CUSTOMER_KEYS  # every stratum compared whole
    assert labelled["C003"] == "segment=retail|flagged=N"  # its first row's, not its second's (segment S)
    assert run["params"]["tables"]["a"]["strata"] == {"segment": "segment", "flagged": "flagged"}


async def test_strata_read_the_key_the_row_and_the_frames_read_start(on_tables: Tables) -> None:
    recent = "row.changed_at != null && timestamp(row.changed_at) >= now - duration('108h')"
    strata = [
        {"name": "recent", "cel": recent,
         "buckets": [bucket("Y", 10, values=[True], riskTier="high"), bucket("N")]},
        {"name": "even", "cel": "key.matches('[02468]$')",
         "buckets": [bucket("Y", 20, values=[True]), bucket("N", 20)]},
    ]  # fmt: skip
    signoff = "riskTier == 'high' ? 'recent=Y' : 'even=' + strata.even"
    _, harness = on_tables()
    async with harness as h:
        frame = table_source(strataColumns=["changed_at"])
        run = await _draw(h, tabled(frame, strata=strata, signoff=signoff))
        labelled = {k["key"]: k["signoff_stratum"] for k in await keys(h, run["run_id"])}
    assert [k for k, label in sorted(labelled.items()) if label == "recent=Y"] == [
        "C001",
        "C002",
        "C003",
        "C004",
    ]
    assert (labelled["C005"], labelled["C006"]) == ("even=N", "even=Y")


async def test_keys_out_of_order_are_refused(on_tables: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
    def descending(side: Plan) -> tuple[Any, Any]:
        statement, params = frame_sql(side)
        return statement + sql.SQL(" DESC"), params  # its one ORDER BY term: the key

    frame_sql = queries.frame_sql
    monkeypatch.setattr(queries, "frame_sql", descending)
    strata = [{"name": "all", "cel": "true", "buckets": [bucket("all", 30, values=[True])]}]
    _, harness = on_tables()
    async with harness as h:
        run = await _draw(h, tabled(table_source(strataColumns=[]), strata=strata, signoff=None))
        assert await drawn(h, run["run_id"]) is None and await keys(h, run["run_id"]) == []
    assert (run["status"], run["stop_reason"]) == ("paused", sample.DRAW_PAUSE)
    assert run["draw"]["error"] == "the population's keys came out of order"


async def test_a_frame_that_times_out_pauses_the_run(
    on_tables: Tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(census, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(queries, "frame_sql", lambda side: (sql.SQL("SELECT pg_sleep(5)"), {}))
    _, harness = on_tables()
    async with harness as h:
        run = await _draw(h, tabled())
    assert (run["stop_reason"], run["draw"]["error"]) == (
        sample.DRAW_PAUSE,
        "the frame of side a timed out after 1 s (QueryCanceled)",
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            tables.SnapshotLost("ORA-01555"),
            "the frame of side a could not be held in one snapshot (ORA-01555)",
        ),
        (
            tables.TargetAuthRefused("ORA-01017"),
            "the frame of side a could not be read: the target refused the credential (ORA-01017)",
        ),
        (tables.TargetRefused("MySQL 1105"), "the frame of side a could not be read (MySQL 1105)"),
        (tables.KeyUnfit("MySQL 1366"), "the frame of side a could not be read (MySQL 1366)"),
        (
            UnsafeTargetError("10.1.2.3 is a private address: set allow_private_network on the target"),
            "the frame of side a could not be read (UnsafeTargetError)",
        ),
    ],
    ids=["snapshot lost", "login refused", "another code", "unfit", "the guard"],
)
async def test_a_frame_an_engine_s_error_ends_pauses_the_run(
    on_tables: Tables, monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    async def failing(self: Any, conn: Any, side: Plan) -> AsyncIterator[Any]:
        raise error
        yield  # an async generator, as an engine's frame is

    monkeypatch.setattr(postgresql.PostgreSQL, "frame", failing)
    _, harness = on_tables()
    async with harness as h:
        run = await _draw(h, tabled())
    assert (run["status"], run["stop_reason"]) == ("paused", sample.DRAW_PAUSE)
    assert run["draw"]["error"] == message  # a code, never the server's text


@pytest.mark.mysql
async def test_a_mysql_table_is_a_frame_its_strata_built_by_the_worker(
    open_harness: OpenHarness, service: ScriptedService, mysql_server: MySQLServer
) -> None:
    # The seeded customers, a NULL key, and a second row of C003 whose strata sort after its first row's.
    database = mysql_server.database("""
        SET SESSION cte_max_recursion_depth = 1000;
        CREATE TABLE customers (id VARCHAR(10), name VARCHAR(20), status CHAR(1), segment VARCHAR(5),
                                flagged CHAR(1), changed_at TIMESTAMP);
        INSERT INTO customers WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 20)
            SELECT CONCAT('C', LPAD(n, 3, '0')), CONCAT('n', n), 'A', IF(n <= 10, 'R', 'B'),
                   IF(n % 7 = 0, 'Y', 'N'), NOW() - INTERVAL n DAY FROM g;
        INSERT INTO customers VALUES (NULL, 'x', 'A', 'R', 'N', NOW()), ('C003', 'n3', 'A', 'S', 'N', NOW());
    """)
    assert service.server is not None
    served = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
    registry = json.dumps(
        {"targets": yaml.safe_load(served)["targets"] | {"db-a": mysql_server.target(database)}}
    )
    async with open_harness(registry) as h:
        run = await _draw(h, tabled())
        stored = await drawn(h, run["run_id"])
        labelled = {k["key"]: k["stratum"] for k in await keys(h, run["run_id"])}
    assert stored is not None and stored["frame"] == {
        "keys": 20,
        "origin": {"a": 20, "b": 0, "both": 0},
        "unfit": 0,
    }
    assert _timed(run["census"]["a"], "at") == {
        "basis": "frame", "target": "db-a", "relation": f"{database}.customers", "where": None, "rows": 22,
        "skipped": 1, "keys": 20, "unfit": 0, "engine": "mysql", "consistency": "snapshot", "server": "8.4",
    }  # fmt: skip
    assert sorted(labelled) == CUSTOMER_KEYS and labelled["C003"] == "segment=retail|flagged=N"
    assert labelled["C007"] == "segment=retail|flagged=Y" and labelled["C014"] == "segment=other|flagged=Y"


@pytest.mark.oracle
async def test_an_oracle_table_is_a_frame_its_strata_built_by_the_worker(
    open_harness: OpenHarness, service: ScriptedService, oracle_server: OracleServer
) -> None:
    # The seeded customers, a NULL key, and a second row of C003 whose strata sort after its first row's.
    schema = oracle_server.schema("""
        CREATE TABLE customers (id VARCHAR2(10), name VARCHAR2(20), status CHAR(1), segment VARCHAR2(5),
                                flagged CHAR(1), changed_at TIMESTAMP WITH TIME ZONE);
        INSERT INTO customers
            SELECT 'C' || LPAD(LEVEL, 3, '0'), 'n' || LEVEL, 'A', CASE WHEN LEVEL <= 10 THEN 'R' ELSE 'B' END,
                   CASE WHEN MOD(LEVEL, 7) = 0 THEN 'Y' ELSE 'N' END, SYSTIMESTAMP - LEVEL
            FROM DUAL CONNECT BY LEVEL <= 20;
        INSERT INTO customers VALUES (NULL, 'x', 'A', 'R', 'N', SYSTIMESTAMP),
                                     ('C003', 'n3', 'A', 'S', 'N', SYSTIMESTAMP);
    """)
    assert service.server is not None
    served = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)
    registry = json.dumps(
        {"targets": yaml.safe_load(served)["targets"] | {"db-a": oracle_server.target(schema)}}
    )
    async with open_harness(registry) as h:
        run = await _draw(h, tabled())
        stored = await drawn(h, run["run_id"])
        labelled = {k["key"]: k["stratum"] for k in await keys(h, run["run_id"])}
    assert stored is not None and stored["frame"] == {
        "keys": 20,
        "origin": {"a": 20, "b": 0, "both": 0},
        "unfit": 0,
    }
    assert _timed(run["census"]["a"], "at") == {
        "basis": "frame", "target": "db-a", "relation": f"{schema}.CUSTOMERS", "where": None, "rows": 22,
        "skipped": 1, "keys": 20, "unfit": 0, "engine": "oracle", "consistency": "snapshot",
        "server": "23.26",
    }  # fmt: skip
    assert sorted(labelled) == CUSTOMER_KEYS and labelled["C003"] == "segment=retail|flagged=N"
    assert labelled["C007"] == "segment=retail|flagged=Y" and labelled["C014"] == "segment=other|flagged=Y"


@pytest.mark.parametrize("listing", [[], ["X1", "Y2"]], ids=["no item", "no fit key"])
async def test_a_frame_without_keys_pauses_the_run(
    harness: Harness, service: ScriptedService, listing: list[str]
) -> None:
    service.listings = [listing]
    run = await _draw(harness, listed())
    assert (run["status"], run["draw"]["error"]) == ("paused", "the frame is empty")


async def test_a_frame_whose_target_left_the_registry_pauses_the_run(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    assert service.server is not None
    async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)) as h:
        run_id = await create_run(h.ctx, await h.spec(listed()), "batch", who=TESTER)
    async with open_harness('targets:\n  other: {base_url: "https://other.example.com"}\n') as h:
        await sample.draw(h.ctx, run_id)
        run = await h.run(run_id)
    assert (run["status"], run["draw"]["error"]) == ("paused", "target svc-a is not in the registry")


async def test_a_listing_is_walked_through_the_mocks_its_fields_read_as_strata(
    harness: Harness, service: ScriptedService
) -> None:
    items = [{"id": key, "kind": "ORG" if key < "K003" else "PERSON"} for key in KEYS]
    service.listings = [items]
    strata = [
        {"name": "kind", "column": "kind", "buckets": [bucket("org", 8, values=["ORG"]), bucket("person", 8)]}
    ]
    run = await _draw(harness, listed(listed_source(strataFields={"kind": "$.kind"}), strata=strata))
    labelled = {k["key"]: k["stratum"] for k in await keys(harness, run["run_id"])}
    assert labelled == {key: "kind=org" if key < "K003" else "kind=person" for key in KEYS}
    assert _timed(run["census"]["a"], "first_at", "last_at") == {
        "basis": "listing", "target": "svc-a", "items": 8, "keys": 8, "unfit": 0,
    }  # fmt: skip
    assert run["census"]["a"]["first_at"] <= run["census"]["a"]["last_at"] <= run["census"]["at"]
    assert service.walks == 1


async def test_a_union_counts_origins_and_takes_the_first_sources_strata(
    on_tables: Tables, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C001 is held by both (the table's segment wins), C021 and C022 by side b only.
    service.listings = [[
        {"id": "C001", "segment": "B", "flagged": "Y"}, {"id": "C021", "segment": "R", "flagged": "N"},
        {"id": "C022", "segment": "B", "flagged": "Y"}, "C007",
    ]]  # fmt: skip
    monkeypatch.setattr(postgresql, "BATCH", 4)  # table fetches, each fed to the temporary table on its own
    sessions = _counting(monkeypatch, sample, "_session")
    _, harness = on_tables()
    async with harness as h:
        frame = {"kind": "union", "sources": [table_source(), served_source()]}
        run = await _draw(h, tabled(frame, strata=segments(flagged=3)))
        stored = await drawn(h, run["run_id"])
        labelled = {k["key"]: (k["stratum"], k["signoff_stratum"]) for k in await keys(h, run["run_id"])}
    assert len(sessions) == 1 and stored is not None
    assert stored["frame"] == {"keys": 22, "origin": {"a": 18, "b": 2, "both": 2}, "unfit": 0}
    assert (run["census"]["a"]["keys"], run["census"]["b"]["keys"], run["census"]["b"]["items"]) == (20, 4, 4)
    assert labelled["C001"] == ("segment=retail|flagged=N", "segment=retail")
    assert labelled["C021"] == ("segment=retail|flagged=N", "segment=retail")
    assert labelled["C022"] == ("segment=other|flagged=Y", "flagged=Y")
    assert stored["population"] == ["listing:svc-b:GET /list", "table:db-a/customers"]


REPEATED = {
    "adjacent pages": ["K001", "K002", "K002", "K003", "K004", "K005"],
    "distant pages": ["K001", "K002", "K003", "K004", "K005", "K001"],
    "one page": ["K001", "K001", "K002", "K003", "K004", "K005"],
}


@pytest.mark.parametrize("where", REPEATED)
@pytest.mark.parametrize("member", [0, 1])
async def test_a_listing_that_lists_a_key_twice_is_refused(
    harness: Harness, service: ScriptedService, where: str, member: int
) -> None:
    clean = ["K003", "K004", "K005", "K006", "K007", "K008"]
    service.listings = [REPEATED[where], clean] if member == 0 else [clean, REPEATED[where]]
    frame = {"kind": "union", "sources": [listed_source(), listed_source("svc-b")]}
    run = await _draw(harness, listed(frame))
    target = "svc-a" if member == 0 else "svc-b"
    assert (run["status"], run["stop_reason"]) == ("paused", sample.DRAW_PAUSE)
    assert run["draw"]["error"] == (
        f"the key listing of target {target} lists 1 key(s) twice: its pages shifted while they were read, so"
        " keys may be missing (declare stable_ordering, or use cursor pagination)"
    )
    assert await drawn(harness, run["run_id"]) is None


async def test_a_listing_repeating_a_key_the_first_source_holds_is_refused(
    harness: Harness, service: ScriptedService
) -> None:
    """The second source lists K003, which the first holds, on pages 1 and 3: its second listing is a
    repeat (W2), never a second key both hold, and the origins count each key once."""
    service.listings = [
        ["K003", "K004", "K005", "K006", "K007", "K008"],
        ["K001", "K003", "K002", "K004", "K005", "K003"],
    ] * 2  # drawn, then read again
    spec = listed({"kind": "union", "sources": [listed_source(), listed_source("svc-b")]})
    run = await _draw(harness, spec)
    assert (run["status"], run["draw"]["error"]) == (
        "paused",
        "the key listing of target svc-b lists 1 key(s) twice: its pages shifted while they were read, so"
        " keys may be missing (declare stable_ordering, or use cursor pagination)",
    )
    async with harness.ctx.pool.connection() as conn:
        compiled = await harness.ctx.spec(conn, run["spec_id"])
    frame = sample._Frame(harness.ctx, compiled, run, sampling.salt("unit-q4", sampling.nonce()))
    with pytest.raises(sampling.DrawError):
        await frame.read()
    assert frame.summary(frame.selection().result())["origin"] == {"a": 3, "b": 2, "both": 3}


async def test_a_key_both_listings_hold_counts_once(harness: Harness, service: ScriptedService) -> None:
    service.listings = [["K001", "K002", "K003", "K004"], ["K003", "K004", "K005", "K006", "K007", "K008"]]
    run = await _draw(
        harness, listed({"kind": "union", "sources": [listed_source(), listed_source("svc-b")]})
    )
    stored = await drawn(harness, run["run_id"])
    assert stored is not None and stored["frame"] == {
        "keys": 8,
        "origin": {"a": 2, "b": 4, "both": 2},
        "unfit": 0,
    }


async def test_unfit_keys_are_counted_never_shown(harness: Harness, service: ScriptedService) -> None:
    # "zz-9" holds a dash no generated id or digest does, so a random D- id never contains it
    service.listings = [[*KEYS[:3], " k004 ", "zz-9", {"id": 5}, {"no": "id"}, *KEYS[4:], "K00\u0000"]]
    run = await _draw(harness, listed(strata=halves(4, 4)))
    stored = await drawn(harness, run["run_id"])
    assert stored is not None and stored["frame"] == {
        "keys": 8,
        "origin": {"a": 8, "b": 0, "both": 0},
        "unfit": 4,
    }
    assert (run["census"]["a"]["items"], run["census"]["a"]["keys"], run["census"]["a"]["unfit"]) == (
        12,
        8,
        4,
    )
    assert "K004" in {k["key"] for k in await keys(harness, run["run_id"])}  # normalised: trim, upper
    shown = json.dumps(
        [
            stored["frame"],
            stored["strata"],
            run["census"],
            await audited(harness, run["run_id"], "sample.draw"),
        ]
    )
    assert "ZZ-9" not in shown.upper() and '"5"' not in shown


async def test_no_key_census_counts_a_sampled_cycle_nor_gates_its_leases(
    harness: Harness, service: ScriptedService
) -> None:
    service.listings = [KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    run = await _draw(harness, listed())
    async with harness.ctx.pool.connection() as conn:
        assert run["run_id"] not in await repo.census_due(conn)
        assert census.pending(run) == []
        leased = await repo.lease_due(conn, "w1", 20, 60, census=True)
    assert sorted(row["key_masked"] for row in leased) == KEYS
