"""The census of a cycle (docs/mvp/PACK.md §1.2, §1.3): a key-list run's table sides counted exactly in
one snapshot each (key maps, composite keys, a filter), an out-of-filter key never standing in for an
omitted one, a timeout and a removed target recorded and warned while the cycle goes on, each cycle of a
key-list soak counted again before a key of it is leased, a resumed scan's snapshots, and a listing's
walks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth.connectors.tables import RowsMoved, duck
from deltasleuth.core.spec import TargetRegistry
from deltasleuth.runner import census, run_worker
from deltasleuth.runner import scan as scanning
from deltasleuth.runner.cycle import finish_cycle
from deltasleuth.runner.runs import create_run
from deltasleuth.store import repo
from tests.conftest import TESTER, MySQLServer, OracleServer
from tests.unit.store.four_eyes import approved, person

from .conftest import Harness, OpenHarness, ScriptedService, doc
from .test_runs import listed
from .test_scan import SEED as USERS
from .test_scan import SPEC as SCANNED
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db

# One key is NULL and one ('x7') fits no key map but the identity; '3' is outside the filter 'active'.
ACCOUNTS = """
    CREATE TABLE accounts (id text, email text, active boolean);
    INSERT INTO accounts VALUES ('1', 'a', true), ('2', 'b', true), ('3', 'c', false), ('4', 'd', true),
                                (NULL, 'n', true), ('x7', 'e', true);
"""
LINKS = """
    CREATE TABLE links (entity text, kind text, value text, note text);
    INSERT INTO links VALUES ('e1', 'phone', 'p1', 'x'), ('e1', 'mail', 'm|1', 'y'),
                             ('e1', 'mail', 'm|1', 'z'), ('e2', 'phone', 'p2', 'w');
"""
PACKS = {"installation": "test-1", "signing_key_ref": "file:/run/secrets/unused"}
OIDC = {"issuer": "http://127.0.0.1:1", "client_id": "deltasleuth", "client_secret_ref": "file:oidc_secret",
        "roles": {"rule_approver": ["G-approver"]}}  # fmt: skip
SSO = {"public_url": "https://ds.test", "oidc": OIDC}  # packs need it; the worker never contacts the IdP
PREFIX_PAD = {"kind": "prefix_pad", "prefix": "A-", "width": 3, "sideType": "string"}
REGEX = {
    "kind": "regex",
    "toCanonical": {"pattern": "([0-9]+)", "replacement": "K$1"},
    "toSide": {"pattern": "K([0-9]+)", "replacement": "$1"},
    "sideType": "string",
}
MOMENT = r"\d{4}-\d\d-\d\d \d\d:\d\d UTC"
Tables = Callable[..., tuple[str, AbstractAsyncContextManager[Harness]]]


def spec(a: dict[str, Any], b: dict[str, Any], **members: Any) -> str:
    """Table side ``a`` on db-a against ``b`` on db-b; ``members`` replace or add top-level members."""
    return json.dumps(
        {
            "apiVersion": "deltasleuth/v1",
            "kind": "ComparisonSpec",
            "metadata": {"name": "accounts", "version": "1.0.0", "owner": "tests"},
            "normalizerVersion": "1.1.0",
            "key": {"name": "id", "type": "string", "class": "business_code"},
            "sides": {
                "a": {"role": "legacy", "target": "db-a", "table": a},
                "b": {"role": "new", "target": "db-b", "table": b},
            },
            "compare": {},
            "recheck": {"delay": "0s", "times": 1},
            "security": {"unlisted": "tokenize"},
        }
        | members
    )


def accounts(**options: Any) -> dict[str, Any]:
    return {"name": "accounts", "keyColumns": ["id"], "columns": ["email"]} | options


@pytest.fixture
def on_tables(open_harness: OpenHarness, databases: Callable[[str], str], tmp_path: Path) -> Tables:  # noqa: F811
    """``dsn, harness = on_tables(seed, **settings)``: a fresh database seeded with ``seed``, which targets
    db-a and db-b both read, and a harness on it."""

    def open_(seed: str, **settings: Any) -> tuple[str, AbstractAsyncContextManager[Harness]]:
        dsn = databases(seed)
        (tmp_path / "dsn_tables").write_text(dsn)
        entry = {"dsn_ref": f"file:{tmp_path / 'dsn_tables'}", "rate_per_s": 1000}
        return dsn, open_harness(json.dumps({"targets": {"db-a": entry, "db-b": entry}}), **settings)

    return open_


def _counted(found: dict[str, Any]) -> dict[str, Any]:
    """A table record without its time."""
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", found["at"])
    return {name: value for name, value in found.items() if name != "at"}


async def _audited(h: Harness, run_id: str) -> list[dict[str, Any]]:
    async with h.ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT actor, detail FROM ds.audit_log WHERE action = 'run.census' AND object = %s ORDER BY id",
            (run_id,),
        )
        return [{"actor": actor} | detail for actor, detail in await cur.fetchall()]


@pytest.mark.parametrize(
    ("key_map", "keys", "missing", "skipped"),
    [
        (None, ["1", "2", "4", "9"], 2, 1),  # 3 and x7 are missing, 9 is outside, the NULL key is skipped
        (PREFIX_PAD, ["A-001", "A-002", "A-004", "A-009"], 1, 2),  # x7 maps to nothing: skipped too
        (REGEX, ["K1", "K2", "K4", "K9"], 1, 2),
    ],
    ids=["identity", "prefix_pad", "regex"],
)
async def test_the_key_census_merges_each_table_with_the_key_set_exactly(
    on_tables: Tables, key_map: dict[str, Any] | None, keys: list[str], missing: int, skipped: int
) -> None:
    table = accounts(keyColumns=["id" if key_map is None else {"name": "id", "keyMap": key_map}])
    _, harness = on_tables(ACCOUNTS)
    async with harness as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table, table)), "batch", keys, who=TESTER)
        counted = await census.record(h.ctx, run_id)
        again = await census.record(h.ctx, run_id)  # the cycle is counted
        run = await h.run(run_id)
        audited = await _audited(h, run_id)
    assert counted is not None and counted["cycle"] == 1
    side = {"basis": "table", "target": "db-a", "relation": "public.accounts", "where": None, "rows": 6}
    side |= {"skipped": skipped, "missing": missing, "extra": 1}
    assert _counted(counted["a"]) == side and _counted(counted["b"]) == side | {"target": "db-b"}
    assert again is None and run["census"] == counted and census.pending(run) == []
    detail = {"actor": "system:worker", "cycle": 1, "basis": "table", "rows": 6, "missing": missing}
    detail |= {"extra": 1, "snapshots": 1}
    assert audited == [detail | {"side": "a"}, detail | {"side": "b"}]
    assert census.described(counted["a"]) == (
        f"its key set checked against public.accounts in 1 snapshot at {census._moment(counted['a']['at'])}:"
        f" {missing} key(s) missing, 1 outside it"
    )


@pytest.mark.mysql
async def test_the_key_census_of_a_mysql_side_adds_its_engine_and_consistency(
    open_harness: OpenHarness, mysql_server: MySQLServer
) -> None:
    database = mysql_server.database("""
        CREATE TABLE accounts (id VARCHAR(10), email VARCHAR(10), active TINYINT(1));
        INSERT INTO accounts VALUES ('1', 'a', 1), ('2', 'b', 1), ('3', 'c', 0), ('4', 'd', 1),
                                    (NULL, 'n', 1), ('x7', 'e', 1);
        CREATE VIEW accounts_v AS SELECT * FROM accounts;
    """)
    entry = mysql_server.target(database)
    table, view = accounts(where="active = 1"), accounts(name="accounts_v")
    async with open_harness(json.dumps({"targets": {"db-a": entry, "db-b": entry}})) as h:
        run_id = await create_run(
            h.ctx, await h.spec(spec(table, view)), "batch", ["1", "2", "4", "9"], who=TESTER
        )
        counted = await census.record(h.ctx, run_id)
        audited = await _audited(h, run_id)
    assert counted is not None
    assert _counted(counted["a"]) == {
        "basis": "table", "target": "db-a", "relation": f"{database}.accounts", "where": '"active" = 1',
        "rows": 5, "skipped": 1, "missing": 1, "extra": 1, "engine": "mysql", "consistency": "snapshot",
        "server": "8.4",  # the version the count's session met, which the plan's may no longer be
    }  # fmt: skip
    assert (counted["b"]["rows"], counted["b"]["missing"], counted["b"]["consistency"]) == (
        6,
        2,
        "unverified",
    )
    # The audit row names the engine and how the side was read; the certificate says a view's snapshot
    # cannot be observed, warns of it, and says whose view of the rows a MySQL side's is.
    assert [(row["side"], row["engine"], row["consistency"]) for row in audited] == [
        ("a", "mysql", "snapshot"),
        ("b", "mysql", "unverified"),
    ]
    at = census._moment(counted["b"]["at"])
    assert census.described(counted["b"]) == (
        f"its key set checked against {database}.accounts_v in 1 transaction, a snapshot not observable (a"
        f" MySQL view) at {at}: 2 key(s) missing, 1 outside it; rows and values as the account reads them"
        " (redaction, masking and row policies apply)"
    )
    assert census.warning("a", counted["a"]) is None
    assert census.warning("b", counted["b"]) == (
        "SIDE B NOT READ IN A SNAPSHOT (a snapshot not observable (a MySQL view)): rows written during the"
        " read may be missing or read twice"
    )


def test_a_side_its_engine_could_not_pin_says_so_wherever_a_snapshot_stood() -> None:
    at = "2026-10-03T04:00:00.000000Z"
    scan = {"basis": "scan", "target": "db-a", "relation": "ledger.t", "rows": 9, "skipped": 0}
    scan |= {"first_at": at, "last_at": at, "engine": "mysql"}
    frame = {"basis": "frame", "relation": "ledger.t", "at": at, "engine": "mysql"}
    as_read = "; rows and values as the account reads them (redaction, masking and row policies apply)"
    when = "2026-10-03 04:00 UTC"
    view, other = "a snapshot not observable (a MySQL view)", "not a snapshot (a MySQL table outside InnoDB)"

    def said(found: dict[str, Any], consistency: str, **more: Any) -> str:
        return census.described(found | {"consistency": consistency} | more)

    assert said(scan, "snapshot", snapshots=1) == f"read to its end in 1 snapshot at {when}{as_read}"
    assert (
        said(scan, "unverified", snapshots=1)
        == f"read to its end in 1 transaction, {view} at {when}{as_read}"
    )
    assert said(scan, "none", snapshots=1) == f"read to its end in 1 pass, {other} at {when}{as_read}"
    assert said(scan, "none", snapshots=3) == (
        f"read to its end in 3 passes, {other} between {when} and {when}{as_read}"
    )
    assert said(frame, "unverified") == (
        f"its population read to its end in 1 transaction, {view} at {when} as the sampling frame{as_read}"
    )
    # An Oracle side is read in a snapshot: it warns of nothing, and still says whose view it is.
    oracle = scan | {"engine": "oracle", "consistency": "snapshot", "snapshots": 1}
    assert census.warning("a", oracle) is None and census.described(oracle).endswith(as_read)
    not_pinned = "rows written during the read may be missing or read twice"
    assert census.warning("a", scan | {"consistency": "none", "snapshots": 1}) == (
        f"SIDE A NOT READ IN A SNAPSHOT ({other}): {not_pinned}"
    )
    assert census.warning("b", scan | {"consistency": "unverified", "snapshots": 2}) == (
        f"SIDE B READ IN 2 TRANSACTIONS between {when} and {when}: rows added behind a resume point meanwhile"
        f" were not read\nSIDE B NOT READ IN A SNAPSHOT ({view}): {not_pinned}"
    )
    # A PostgreSQL side's record names no engine: its wording is unchanged.
    plain = {k: v for k, v in scan.items() if k != "engine"} | {"snapshots": 1}
    assert census.described(plain) == f"read to its end in 1 snapshot at {when}"
    assert census.warning("a", plain) is None and census.warning("a", {"failed": "TargetDenied"}) is None
    # The server version a read met is recorded for every side whose plan names its engine, PostgreSQL's
    # in such a pair too; a PostgreSQL pair's record gains nothing.
    assert census.engined({"engine": "mysql", "consistency": "none"}, "8.4") == {
        "engine": "mysql", "consistency": "none", "server": "8.4",
    }  # fmt: skip
    assert census.engined({"engine": "postgresql", "consistency": "snapshot"}, "16.4") == {"server": "16.4"}
    assert census.engined({"keys": ["id"]}, "16.4") == census.engined({"engine": "postgresql"}) == {}


@pytest.mark.oracle
async def test_the_key_census_of_an_oracle_side_adds_its_engine_and_consistency(
    open_harness: OpenHarness, oracle_server: OracleServer
) -> None:
    schema = oracle_server.schema("""
        CREATE TABLE accounts (id VARCHAR2(10), email VARCHAR2(10), active NUMBER(1));
        INSERT INTO accounts VALUES ('1', 'a', 1), ('2', 'b', 1), ('3', 'c', 0), ('4', 'd', 1),
                                    ('', 'n', 1), ('x7', 'e', 1);
        CREATE VIEW accounts_v AS SELECT * FROM accounts;
    """)
    entry = oracle_server.target(schema)
    table, view = accounts(where="active = 1"), accounts(name="accounts_v")
    async with open_harness(json.dumps({"targets": {"db-a": entry, "db-b": entry}})) as h:
        run_id = await create_run(
            h.ctx, await h.spec(spec(table, view)), "batch", ["1", "2", "4", "9"], who=TESTER
        )
        counted = await census.record(h.ctx, run_id)
    assert counted is not None
    assert _counted(counted["a"]) == {
        "basis": "table",
        "target": "db-a",
        "relation": f"{schema}.ACCOUNTS",
        "where": '"ACTIVE" = 1',
        "rows": 5,
        "skipped": 1,
        "missing": 1,
        "extra": 1,
        "engine": "oracle",
        "consistency": "snapshot",
        "server": "23.26",
    }  # fmt: skip  (the empty string is a NULL key there: skipped)
    assert (counted["b"]["rows"], counted["b"]["missing"], counted["b"]["consistency"]) == (
        6,
        2,
        "snapshot",  # a view is read in the pass's one read-only transaction too
    )


async def test_composite_keys_are_merged_by_their_parts(on_tables: Tables) -> None:
    table = {"name": "links", "keyColumns": ["entity", "kind", "value"], "columns": ["note"]}
    key = {
        "name": "link",
        "type": "composite",
        "parts": ["entity", "kind", "value"],
        "class": "business_code",
    }
    # e2|phone|p2 is missing; e1's mail key, held by two rows, is counted once; e3's is outside.
    keys = ["e1|phone|p1", "e1|mail|m\\|1", "e3|phone|p3"]
    _, harness = on_tables(LINKS)
    async with harness as h:
        run_id = await create_run(h.ctx, await h.spec(spec(table, table, key=key)), "batch", keys, who=TESTER)
        counted = await census.record(h.ctx, run_id)
    assert counted is not None
    assert {name: counted["a"][name] for name in ("rows", "skipped", "missing", "extra")} == {
        "rows": 4,
        "skipped": 0,
        "missing": 1,
        "extra": 1,
    }


async def test_a_key_outside_the_filter_never_stands_in_for_an_omitted_one(on_tables: Tables) -> None:
    # The key set holds 3, which side a's filter leaves out, in place of x7, which it holds.
    _, harness = on_tables(ACCOUNTS)
    async with harness as h:
        text = spec(accounts(where="active"), accounts())
        run_id = await create_run(h.ctx, await h.spec(text), "batch", ["1", "2", "3", "4"], who=TESTER)
        counted = await census.record(h.ctx, run_id)
        run = await h.run(run_id)
    assert counted is not None
    assert _counted(counted["a"]) == {
        "basis": "table",
        "target": "db-a",
        "relation": "public.accounts",
        "where": run["params"]["tables"]["a"]["where"],
        "rows": 5,  # the active rows, the NULL key among them
        "skipped": 1,
        "missing": 1,  # x7: not observed, so no pack covers side a
        "extra": 1,  # 3
    }
    assert counted["a"]["where"] is not None and (counted["b"]["missing"], counted["b"]["extra"]) == (1, 0)


async def test_a_census_that_times_out_is_recorded_and_warned_and_the_cycle_goes_on(
    on_tables: Tables, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(census, "TIMEOUT_S", 0.2)
    caplog.set_level(logging.WARNING, logger="deltasleuth.runner.census")
    dsn, harness = on_tables(ACCOUNTS)
    async with harness as h:
        run_id = await create_run(
            h.ctx, await h.spec(spec(accounts(), accounts())), "batch", ["1"], who=TESTER
        )
        with psycopg.connect(dsn) as holder:  # the table stays locked while the census waits for it
            holder.execute("LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE")
            counted = await census.record(h.ctx, run_id)
        run = await h.run(run_id)
        audited = await _audited(h, run_id)
    assert counted is not None
    assert (counted["a"], counted["b"]) == ({"failed": "QueryCanceled"}, {"failed": "QueryCanceled"})
    assert census.pending(run) == []  # recorded: the keys are leased now
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", f"census of side {side} of run {run_id} failed: QueryCanceled") for side in "ab"
    ]
    failed = {"actor": "system:worker", "cycle": 1, "basis": "table", "failed": "QueryCanceled"}
    assert audited == [failed | {"side": "a"}, failed | {"side": "b"}]


@pytest.mark.parametrize("packs", [True, False], ids=["packs", "no_packs"])
async def test_a_census_whose_target_was_removed_is_recorded_and_the_run_fails_as_without_packs(
    on_tables: Tables, packs: bool
) -> None:
    _, harness = on_tables(ACCOUNTS, **({"auth": SSO, "packs": PACKS} if packs else {}))
    async with harness as h:
        spec_id = await h.spec(spec(accounts(), accounts()))
        if packs:  # under SSO its column list needs a four-eyes approval
            async with h.ctx.pool.connection() as conn:
                people = [await person(conn, name) for name in ("bob", "carol", "erin")]
                await approved(conn, spec_id, *people, scopes=["population_filters"])
        run_id = await create_run(h.ctx, spec_id, "batch", ["1", "2"], who=TESTER)
        removed = Harness(replace(h.ctx, targets=TargetRegistry({})))  # db-a and db-b left targets.yaml
        run = await removed.finish(run_id)
        audited = await _audited(h, run_id)
    assert (run["status"], run["error"]) == ("failed", "SpecError")
    if packs:  # recorded, so its keys were leased
        failed = {"actor": "system:worker", "cycle": 1, "basis": "table", "failed": "UnknownTargetError"}
        assert audited == [failed | {"side": "a"}, failed | {"side": "b"}]
        assert (run["census"]["a"], run["census"]["b"]) == ({"failed": "UnknownTargetError"},) * 2
    else:
        assert audited == [] and run["census"] is None


async def test_each_cycle_of_a_key_list_soak_is_counted_before_a_key_of_it_is_leased(
    on_tables: Tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, harness = on_tables(ACCOUNTS, auth=SSO, packs=PACKS)
    counting, go_on = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    record = census.record

    async def held(ctx: Any, run_id: str, **options: Any) -> Any:
        calls.append(run_id)
        counting.set()
        await go_on.wait()
        if len(calls) == 2:  # a row added after cycle 1 is missing from cycle 2's key set
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("INSERT INTO accounts VALUES ('5', 'f', true)")
        return await record(ctx, run_id, **options)

    monkeypatch.setattr(census, "record", held)
    execution = {"concurrency": 2, "cycleInterval": "0s", "stop": {"maxCycles": 2}}
    async with harness as h:
        text = spec(accounts(), accounts(), execution=execution)
        keys = ["1", "2", "3", "4"]
        spec_id = await h.spec(text)
        async with h.ctx.pool.connection() as conn:  # under SSO its column list needs a four-eyes approval
            people = [await person(conn, name) for name in ("bob", "carol", "erin")]
            await approved(conn, spec_id, *people, scopes=["population_filters"])
        run_id = await create_run(h.ctx, spec_id, "soak", keys, soak={"rolling": False}, who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
        try:
            await asyncio.wait_for(counting.wait(), 30)
            await asyncio.sleep(0.5)  # ten polls of every slot
            async with h.ctx.pool.connection() as conn:
                waiting = await repo.cycle_counts(conn, run_id)
            go_on.set()
            run = await h.wait(run_id)
        finally:
            stop.set()
            await worker
        async with h.ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT cycle, census FROM ds.cycles WHERE run_id = %s ORDER BY cycle", (run_id,)
            )
            cycles = await cur.fetchall()
        audited = await _audited(h, run_id)
    assert waiting == {"keys": {"pending": 4}, "outcomes": {}}  # no key leased before the census
    assert (run["status"], run["cycle"]) == ("completed", 2) and calls == [run_id, run_id]
    assert [
        (cycle, found["cycle"], found["a"]["missing"], found["b"]["missing"]) for cycle, found in cycles
    ] == [
        (1, 1, 1, 1),  # x7
        (2, 2, 2, 2),  # x7 and 5
    ]
    assert [(r["cycle"], r["side"], r["missing"]) for r in audited] == [
        (1, "a", 1),
        (1, "b", 1),
        (2, "a", 2),
        (2, "b", 2),
    ]


@pytest.mark.parametrize("lost", [0, 2])
async def test_a_scan_counts_the_snapshots_it_read_each_side_in(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost: int,
) -> None:
    monkeypatch.setattr(scanning, "BATCH", 10)  # 35 rows: three checkpoints, then the end
    save, failures = scanning._save, []

    async def saved_then_lost(*args: Any, **options: Any) -> bool:
        done = await save(*args, **options)
        if len(failures) < lost and not options.get("done"):
            failures.append(True)  # the checkpoint is saved; the scan dies before its next batch
            raise psycopg.OperationalError("the connection was lost")
        return done

    monkeypatch.setattr(scanning, "_save", saved_then_lost)
    targets = {}
    for side in "ab":
        (tmp_path / f"dsn_{side}").write_text(databases(USERS))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    async with open_harness(json.dumps({"targets": targets})) as h:
        spec_id = await h.spec(json.dumps(SCANNED))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        for _ in range(lost):
            with pytest.raises(psycopg.OperationalError):
                await scanning.scan(h.ctx, run_id)
        assert await scanning.scan(h.ctx, run_id) is None
        await finish_cycle(h.ctx, run_id, who=TESTER)
        async with h.ctx.pool.connection() as conn:
            run, ended = await repo.get_run(conn, run_id), await repo.get_cycle(conn, run_id, 1)
            scan = await repo.get_scan(conn, run_id, 1)
            compiled = await h.ctx.spec(conn, spec_id)
    assert run is not None and ended is not None and scan is not None
    counted = census.of_cycle(run, ended, scan, compiled)
    snapshots = lost + 1
    first, last = census._moment(scan["first_at"]), census._moment(scan["last_at"])
    assert counted == {
        "cycle": 1,
        "at": scan["last_at"],
        **{
            side: {
                "basis": "scan", "target": f"db-{side}", "relation": "public.users", "rows": 35, "skipped": 0,
                "snapshots": snapshots, "first_at": scan["first_at"], "last_at": scan["last_at"],
            }
            for side in "ab"
        },
    }  # fmt: skip
    if lost:
        assert scan["first_at"] < scan["last_at"]
        assert census.described(counted["a"]) == f"read to its end in 3 snapshots between {first} and {last}"
        assert census.warning("a", counted["a"]) == (
            f"SIDE A READ IN 3 SNAPSHOTS between {first} and {last}: rows added behind a resume point"
            " meanwhile were not read"
        )
    else:
        assert census.described(counted["a"]) == f"read to its end in 1 snapshot at {first}"
        assert census.warning("a", counted["a"]) is None


async def test_a_scan_stopped_before_the_end_counts_no_side(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
) -> None:
    targets = {}
    for side in "ab":
        (tmp_path / f"dsn_{side}").write_text(databases(USERS))
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    async with open_harness(json.dumps({"targets": targets})) as h:
        spec_id = await h.spec(json.dumps(SCANNED))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        async with h.ctx.pool.connection() as conn:  # cancelled before its scan ran
            await repo.set_run_status(conn, run_id, "cancelling")
        await finish_cycle(h.ctx, run_id, who=TESTER)
        async with h.ctx.pool.connection() as conn:
            run, ended = await repo.get_run(conn, run_id), await repo.get_cycle(conn, run_id, 1)
            scan = await repo.get_scan(conn, run_id, 1)
            compiled = await h.ctx.spec(conn, spec_id)
    assert run is not None and ended is not None
    assert census.of_cycle(run, ended, scan, compiled) == {"cycle": 1, "at": None, "a": None, "b": None}


@pytest.mark.parametrize("rolling", [True, False])
async def test_a_complete_key_listing_counts_side_a_in_each_cycle_it_walks(
    harness: Harness, service: ScriptedService, rolling: bool
) -> None:
    service.listings = [["K001", "K002", "K003"], ["K002", "K003"]]
    for key in ("K001", "K002", "K003"):
        service.both(key, doc(key), doc(key))
    text = listed("concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 2}")
    run_id = await create_run(
        harness.ctx, await harness.spec(text), "soak", soak={"rolling": rolling}, who=TESTER
    )
    run = await harness.finish(run_id)
    async with harness.ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT cycle, census FROM ds.cycles WHERE run_id = %s ORDER BY cycle", (run_id,)
        )
        cycles = dict(await cur.fetchall())
    assert (run["status"], run["cycle"]) == ("completed", 2)
    walked = {1: 3, 2: 2} if rolling else {1: 3}  # a listing that is not rolled is walked once
    assert [cycle for cycle, found in cycles.items() if found is not None] == list(walked)
    for cycle, items in walked.items():
        walk = cycles[cycle]["a"]
        assert cycles[cycle] == {"cycle": cycle, "at": walk["last_at"], "a": walk}  # side b not covered
        assert {name: walk[name] for name in ("basis", "target", "items")} == {
            "basis": "listing",
            "target": "svc-a",
            "items": items,
        }
        assert walk["first_at"] <= walk["last_at"]
        assert re.fullmatch(
            f"a paginated walk between {MOMENT} and {MOMENT} \\(ordering declared stable by the target\\),"
            " not a snapshot",
            census.described(walk),
        )


async def test_the_key_census_of_a_file_side(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """docs/mvp/FILES.md §9, §11: a file side's key census counts its rows, the keys several of its rows hold
    and its lines that were not rows, hashes its files again when its read ends and records no snapshot; a
    file changed since its run was created is the count's failure."""
    exports = tmp_path_factory.mktemp("exports").resolve()  # outside the harness's own directories (T10)
    export = exports / "accounts.csv"
    export.write_bytes(b"id|email\n1|a\n2|b\n2|b2\n4|d\n|n\nx7|e\nshort\n")  # 2 twice, no key, no row
    (tmp_path / "dsn_tables").write_text(databases(ACCOUNTS))
    targets = {
        "db-a": {"engine": "files", "root": str(exports), "settle_s": 0, "allow_writable": True},
        "db-b": {"dsn_ref": f"file:{tmp_path / 'dsn_tables'}", "rate_per_s": 1000},
    }
    dialect = {"columns": ["id", "email"], "delimiter": "|", "header": True}
    table = accounts(file={"path": "accounts.csv", "format": "csv", "csv": dialect})
    async with open_harness(json.dumps({"targets": targets}), files={"roots_under": str(exports)}) as h:
        spec_id = await h.spec(spec(table, accounts()))
        run_id = await create_run(h.ctx, spec_id, "batch", ["1", "2", "4", "9"], who=TESTER)
        later = await create_run(h.ctx, spec_id, "batch", ["1"], who=TESTER)
        counted = await census.record(h.ctx, run_id)
        audited = await _audited(h, run_id)
        export.write_bytes(export.read_bytes() + b"5|f\n")
        failed = await census.record(h.ctx, later)
    assert counted is not None and failed is not None and failed["a"] == {"failed": "FilesChanged"}
    side = counted["a"]
    held = {
        "basis": "table", "target": "db-a", "relation": "accounts", "where": None, "rows": 6, "skipped": 1,
        "missing": 1, "extra": 1, "engine": "files", "consistency": "fixed", "files": 1, "bytes": 40,
        "as_of": None, "mount": "writable", "rejected": 1, "repeated": 1,
    }  # fmt: skip
    assert {name: side[name] for name in held} == held and "snapshots" not in side
    assert side["verified_at"][:19] >= side["pinned_at"][:19] and side["server"].startswith("duckdb ")
    assert audited[0] == {
        "actor": "system:worker", "cycle": 1, "side": "a", "basis": "table", "rows": 6, "missing": 1,
        "extra": 1, "consistency": "fixed", "files": 1, "bytes": 40, "rejected": 1, "repeated": 1,
    }  # fmt: skip
    assert re.fullmatch(
        rf"its key set checked against accounts from 1 fixed file\(s\), 40 bytes, pinned {MOMENT} and hashed"
        rf" the same when the read ended at {MOMENT}; an export, not the system it came from: its own time is"
        rf" not observed; newest file modified {MOMENT} on the mount: 1 key\(s\) missing, 1 outside it",
        census.described(side),
    )
    assert census.warning("a", side) == (
        "SIDE A: 1 KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive, never paired\n"
        "SIDE A WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO"
    )


@pytest.mark.parametrize("side", ["a", "b"])
async def test_rows_moved_in_the_key_census_fails_the_run(
    open_harness: OpenHarness,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    """docs/mvp/FILES.md §7, F10: a file side whose census read met another number of rows than its plan
    counted fails the run, as a scan's does (here its count of rows without a key is made to disagree). The
    count was recorded as failed and the cycle went on: the run completed on a reader that disagreed with
    itself, its keys MATCH, and no later read checks rows."""
    exports = tmp_path_factory.mktemp("exports").resolve()  # outside the harness's own directories (T10)
    targets = {}
    for name in "ab":
        (exports / name).mkdir()
        (exports / name / "accounts.csv").write_bytes(b"id|email\n1|a\n2|b\n4|d\n")
        root = {"engine": "files", "root": str(exports / name), "settle_s": 0, "allow_writable": True}
        targets[f"db-{name}"] = root
    dialect = {"columns": ["id", "email"], "delimiter": "|", "header": True}
    table = accounts(file={"path": "accounts.csv", "format": "csv", "csv": dialect})
    real = duck.Files.null_keys

    async def disagreeing(self: duck.Files, session: Any, plan: Any) -> int:
        return await real(self, session, plan) + (plan["pinned"]["side"] == side)

    files = {"roots_under": str(exports)}
    opened = open_harness(json.dumps({"targets": targets}), files=files, readiness={}, lease_seconds=1)
    async with opened as h:
        run_id = await create_run(
            h.ctx, await h.spec(spec(table, table)), "batch", ["1", "2", "4"], who=TESTER
        )
        monkeypatch.setattr(duck.Files, "null_keys", disagreeing)
        with pytest.raises(RowsMoved):
            await census.record(h.ctx, run_id)
        run = await h.finish(run_id)
        results = await h.results(run_id)
    assert (run["status"], run["error"]) == (
        "failed",
        f"RowsMoved: side {side}: 4 rows were read where the plan counted 3",
    )
    assert results == {} and run["census"] is None  # no key was leased before the census


def test_a_file_side_s_record_says_what_it_was_read_from() -> None:
    """docs/mvp/FILES.md §11: a file side's census record holds its files' count, bytes and times, its mount,
    its lines that were not rows and its repeated keys, never a snapshot or a digest; its wording is an
    export's, not the system's; its warnings W1 to W3; its run.census detail; side a's lines that were not
    rows are no census (C1), and a whole scan's basis then keys (READINESS Y2)."""
    from types import SimpleNamespace

    from deltasleuth.core.types import Side
    from deltasleuth.reports.pack import census_problem
    from tests.unit.core.ready import measures

    plan = {
        "schema": None, "name": "customers", "engine": "files", "server": "duckdb 1.5",
        "consistency": "fixed", "file_count": 12, "bytes": 3_650_722_201, "files_sha256": "ab" * 32,
        "pinned_at": "2026-10-01T02:14:07Z",
        "as_of": "2026-09-30T23:59:00+00:00", "modified_last": "2026-10-01T01:58:11Z", "mount": "read-only",
        "rejected": 0,
    }  # fmt: skip
    state = {
        "rows_a": 9, "skipped_a": 0, "snapshots_a": 1, "first_at": "2026-10-01T02:15:00Z",
        "last_at": "2026-10-01T02:20:00Z", "verified_a": "2026-10-01T02:20:00Z", "repeated_a": 0,
    }  # fmt: skip
    spec = SimpleNamespace(
        sides={Side.A: SimpleNamespace(target="exports"), Side.B: SimpleNamespace(target="db")}
    )
    found = census.scanned("a", plan, state, spec)  # type: ignore[arg-type]
    assert "snapshots" not in found and "files_sha256" not in found
    held = {"files": 12, "bytes": 3_650_722_201, "verified_at": "2026-10-01T02:20:00Z", "rejected": 0}
    assert {name: found[name] for name in held} == held and (found["repeated"], found["engine"]) == (
        0,
        "files",
    )
    assert census.described(found) == (
        "read to its end from 12 fixed file(s), 3,650,722,201 bytes, pinned 2026-10-01 02:14 UTC and hashed"
        " the same when the read ended at 2026-10-01 02:20 UTC; an export, not the system it came from:"
        " declared as of 2026-09-30 23:59 UTC by its stamp file, not observed; newest file modified"
        " 2026-10-01 01:58 UTC on the mount"
    )
    assert "its own time is not observed" in census.described(found | {"as_of": None})
    # The lines its spec's csv.skip left unread are said, never passed over: they are no part of the read.
    assert found["lines_skipped"] == 0 and census.described(found | {"lines_skipped": 24}).startswith(
        "read to its end from 12 fixed file(s), 3,650,722,201 bytes (24 line(s) at the files' starts skipped"
        " as the spec declares, not read), pinned 2026-10-01 02:14 UTC and hashed the same"
    )
    assert census.scanned("a", plan | {"lines_skipped": 24}, state, spec)["lines_skipped"] == 24  # type: ignore[arg-type]
    assert census.warning("a", found) is None
    lines = found | {"rejected": 3, "repeated": 5, "mount": "writable"}
    assert census.warning("b", lines) == (
        "SIDE B: 3 LINE(S) OF ITS FILES WERE NOT ROWS and were not compared: a key on such a line is not"
        " compared, and a key not found on this side is inconclusive\n"
        "SIDE B: 5 KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive, never paired\n"
        "SIDE B WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO"
    )
    assert "LINE(S)" not in (census.warning("a", lines) or "")  # side a's are C1, never a warning
    table = found | {"basis": "table", "missing": 0, "extra": 1, "at": "2026-10-01T02:20:00Z"}
    assert census._detail(1, "a", table) == {
        "cycle": 1, "side": "a", "basis": "table", "rows": 9, "missing": 0, "extra": 1,
        "consistency": "fixed", "files": 12, "bytes": 3_650_722_201, "rejected": 0, "repeated": 0,
    }  # fmt: skip
    run = {"run_id": "R-1", "params": {"keys_source": "table_scan", "tables": {"a": plan, "b": {}}}}
    counted = {"cycle": 1, "a": found | {"rejected": 3}, "b": found}
    problem = census_problem(run, 1, spec, counted, {"done": True}, {"keys_cut": None}, None)  # type: ignore[arg-type]
    assert problem == "cycle 1 of run R-1 is not a census: side a's files hold 3 line(s) that were not rows"
    assert (
        census_problem(run, 1, spec, counted | {"a": found}, {"done": True}, {"keys_cut": None}, None) is None
    )  # type: ignore[arg-type]
    assert measures(problem=problem, whole=True)["basis"] == "keys"
