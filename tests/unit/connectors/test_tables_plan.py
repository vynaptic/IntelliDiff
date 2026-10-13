"""The engine seam (docs/mvp/ENGINES.md §5) without a database: a PostgreSQL pair keeps the plan, the plan
hash and every statement it had before the seam (D3), a plan without an engine is PostgreSQL's, and the
column plan and the per-key connector work through any ``Engine``, showing its errors by code only. A pair
with a side on another engine stores each side's engine and classes, and is checked across the pair (§7).
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from deltasleuth import approvals
from deltasleuth.connectors.base import KeyRejectedError, UnsafeTargetError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.postgres.engine import PostgreSQL
from deltasleuth.connectors.tables import (
    ENGINES,
    Planned,
    Refusal,
    SnapshotLost,
    StatementTimeout,
    TableConnector,
    TargetAuthRefused,
    TargetDenied,
    TargetRefused,
    TargetTempSpace,
    TargetUnreachable,
    compose,
    duck,
    issues,
    key_plan,
    mysql,
    named,
    of,
    oracle,
    pins,
    plan,
    planning,
    preflight,
    resolved,
    sealed,
    side_values,
    unsealed,
)
from deltasleuth.connectors.tables.engine import Column
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.convert import ConversionError
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.spec import TargetRegistry, compile_spec, key_table, load_targets
from deltasleuth.core.types import ExecutionConfig, Side

from . import ducked
from .engines import FakeEngine, Recorder
from .test_postgres import USERS_SIDE, compiled

GOLDEN = json.loads((Path(__file__).parents[2] / "golden" / "engines" / "postgres_pair.json").read_text())
USERS = {"name": "users", "keyColumns": ["userid"], "columns": ["email", {"name": "full_name", "as": "name"}]}
USERS_V = {"name": "users_v", "keyColumns": ["user_id"], "columns": ["Email", "name"]}
RELATIONS = {
    "users": ["userid", "email", "full_name", "legacy_flag"],
    "users_v": ["user_id", "Email", "name"],
}


@pytest.fixture
def targets(tmp_path: Path) -> TargetRegistry:
    """db-a and db-b: database targets whose DSN no test reads."""
    entry = {"dsn_ref": "file:dsn"}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    return load_targets(tmp_path / "targets.yaml")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    """Every database target's engine, and the engine of a plan side that names ``fake``."""
    engine = FakeEngine(RELATIONS)
    monkeypatch.setitem(ENGINES, "postgresql", engine)
    monkeypatch.setitem(ENGINES, "fake", engine)
    return engine


async def _drained(batches: AsyncIterator[Any]) -> None:
    async for _ in batches:
        pass


async def _sent(side: dict[str, Any], keys: list[str], after: list[bytes]) -> dict[str, list[Any]]:
    """What PostgreSQL's adapter sends for each read of ``side``, by the read's name."""
    engine = PostgreSQL()
    asked = {key: side_values(key, side) for key in keys}
    reads = {
        "rows": lambda conn: engine.rows(conn, side, asked),
        "scan": lambda conn: engine.scan(conn, side, None),
        "scan_after": lambda conn: engine.scan(conn, side, after),
        "census": lambda conn: _drained(engine.census(conn, side)),
        "frame": lambda conn: _drained(engine.frame(conn, side)),
        "null_keys": lambda conn: engine.null_keys(conn, side),
        "probe": lambda conn: engine.probe(conn, side),
    }
    sent = {}
    for name, read in reads.items():
        conn = Recorder()
        await read(conn)
        sent[name] = conn.sent
    return sent


async def test_postgres_pair_plan_unchanged(targets: TargetRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _sent(GOLDEN["pair"]["a"], ["7", "o'x"], [b"41"]) == GOLDEN["sent"]["pair"]
    mapped = await _sent(GOLDEN["mapped"], ["BR0007|42"], [b"BR0007", b"42"])  # key maps, a filter, a typeMap
    assert {name: mapped[name] for name in GOLDEN["sent"]["mapped"]} == GOLDEN["sent"]["mapped"]
    monkeypatch.setitem(ENGINES, "postgresql", FakeEngine(RELATIONS))
    stored = await plan(targets, compiled(USERS, USERS_V))
    assert stored == GOLDEN["pair"]  # no engine, server, classes or rendering: the stored form of TABLES §3
    assert await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]})) == GOLDEN["mixed"]
    assert sha256_hex(approvals.plan(GOLDEN["params"] | {"tables": stored})) == GOLDEN["plan_sha256"]


def test_a_plan_without_an_engine_is_postgresql(targets: TargetRegistry) -> None:
    engine = ENGINES["postgresql"]
    assert isinstance(engine, PostgreSQL) and (engine.name, engine.arms) == ("postgresql", 200)
    assert named(GOLDEN["pair"]["a"]) is engine and named({"engine": "postgresql"}) is engine
    assert of(targets.get("db-a")) is engine
    with pytest.raises(KeyError):
        named({"engine": "db2"})


async def test_the_plan_reads_the_catalogue_through_its_target_s_engine(
    targets: TargetRegistry, fake: FakeEngine
) -> None:
    stored = await plan(targets, compiled(USERS, USERS_V))
    assert stored is not None and fake.probed == [stored["a"], stored["b"]]
    assert fake.opened == fake.closed == 4  # a session a side for the catalogue, then one for the probe


@pytest.mark.parametrize(
    ("failing", "message"),
    [
        ({"connect": [TargetUnreachable("ORA-12541")]}, "side a: cannot connect to target db-a: ORA-12541"),
        # P14: once raised as it came, a traceback from `ds run` and a 500 from run creation (an invalid
        # view answers ORA-04063). A session the server ended, and PostgreSQL's error by its class alone.
        (
            {"describe": [TargetRefused("ORA-04063")]},
            "side a: table or view users cannot be described: ORA-04063",
        ),
        (
            {"describe": [TargetUnreachable("ORA-03113")]},
            "side a: table or view users cannot be described: ORA-03113",
        ),
        (
            {"describe": [psycopg.errors.QueryCanceled("canceling statement of key 4111")]},
            "side a: table or view users cannot be described: QueryCanceled",
        ),
        (
            {"probe": [TargetRefused("ORA-01722")]},
            "side a: the database refuses the filter or key map: ORA-01722",
        ),
    ],
    ids=["connect", "describe", "describe unreachable", "describe postgresql", "probe"],
)
async def test_an_engine_s_error_shows_its_code_and_leaves_no_session_open(
    targets: TargetRegistry, monkeypatch: pytest.MonkeyPatch, failing: dict[str, Any], message: str
) -> None:
    engine = FakeEngine(RELATIONS, **failing)
    monkeypatch.setitem(ENGINES, "postgresql", engine)
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(USERS, USERS_V))
    assert str(exc.value) == message and exc.value.__cause__ is None
    assert engine.opened == engine.closed


@pytest.mark.parametrize(
    ("failing", "table", "message"),
    [
        (
            {"server": [TargetUnreachable("ORA-03113")]},
            None,
            "target db-a: the session's account of its server could not be read: ORA-03113",
        ),
        (
            {"server": [TargetRefused("MySQL 1193")]},
            "users",
            "target db-a: the session's account of its server could not be read: MySQL 1193",
        ),
        (
            {"describe": [TargetRefused("ORA-04063")]},
            "app.users",
            "target db-a: table or view app.users cannot be described: ORA-04063",
        ),
    ],
    ids=["server", "server with a table", "describe"],
)
async def test_a_pre_flight_words_an_engine_s_error_after_the_connect(
    targets: TargetRegistry,
    monkeypatch: pytest.MonkeyPatch,
    failing: dict[str, Any],
    table: str | None,
    message: str,
) -> None:
    engine = FakeEngine(RELATIONS, **failing)
    monkeypatch.setitem(ENGINES, "postgresql", engine)
    with pytest.raises(ValueError) as exc:
        await preflight(targets.get("db-a"), table)
    assert str(exc.value) == message and exc.value.__cause__ is None
    assert engine.opened == engine.closed == 1


async def test_a_relation_the_engine_does_not_find_is_refused(
    targets: TargetRegistry, fake: FakeEngine
) -> None:
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"schema": "crm", "name": "customers", "keyColumns": ["id"]}))
    assert str(exc.value) == "side a: table or view crm.customers not found or not visible"
    assert fake.opened == fake.closed == 1


# --- per-key reads through an engine ---------------------------------------------------------------


def _connector(targets: TargetRegistry) -> TableConnector:
    target, execution = targets.get("db-a"), ExecutionConfig()
    side = GOLDEN["pair"]["a"]  # no engine: its target's, which the fixture stands in for
    return TableConnector(target, execution, side, gate=TargetGate.for_target(target))


async def test_a_read_names_its_engine_and_asks_it_for_the_rows(
    targets: TargetRegistry, fake: FakeEngine
) -> None:
    table = _connector(targets)
    obs = await table.fetch("K1", USERS_SIDE)
    await table.aclose()
    assert (obs.http_status, obs.body, obs.side) == (200, b'{"key": "K1"}', Side.A)
    assert obs.request_url == "fake://db-a/public.users"
    assert (fake.asked, fake.opened, fake.closed) == ([["K1"]], 1, 1)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TargetUnreachable("ORA-03113"), ("connect_error", "ORA-03113")),
        (StatementTimeout("MySQL 3024"), ("timeout", "MySQL 3024")),
        (SnapshotLost("MySQL 1412"), ("connect_error", "MySQL 1412")),
        (TargetRefused("MySQL 1105"), ("connect_error", "MySQL 1105")),  # any other server code
        (TargetAuthRefused("MySQL 1045"), ("auth_error", "MySQL 1045")),  # the run pauses
        (UnsafeTargetError("10.1.2.3 is a private address"), ("connect_error", "UnsafeTargetError")),
    ],
    ids=["unreachable", "timeout", "snapshot lost", "another code", "login refused", "the guard"],
)
async def test_an_unreachable_engine_is_a_coded_error_and_the_next_read_opens_a_session(
    targets: TargetRegistry, fake: FakeEngine, error: Exception, expected: tuple[str, str]
) -> None:
    fake.failing["rows"] = [error]
    table = _connector(targets)
    lost = await table.fetch("K1", USERS_SIDE)
    assert (lost.error_class, lost.error_detail, lost.http_status) == (*expected, None)
    assert (fake.opened, fake.closed) == (1, 1)  # the session is dropped
    assert (await table.fetch("K1", USERS_SIDE)).http_status == 200
    await table.aclose()
    assert (fake.opened, fake.closed) == (2, 2)


@pytest.mark.parametrize("error", [TargetDenied("MySQL 1142"), TargetTempSpace("MySQL 1114")])
async def test_a_privilege_gone_or_a_full_disk_fails_the_read(
    targets: TargetRegistry, fake: FakeEngine, error: Exception
) -> None:
    fake.failing["rows"] = [error]
    table = _connector(targets)
    with pytest.raises(type(error)):  # the run fails, showing the class and the code (§11)
        await table.fetch("K1", USERS_SIDE)
    await table.aclose()


async def test_a_row_its_engine_could_not_render_is_recorded_without_a_body(
    targets: TargetRegistry, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def rows(session: Any, side: Any, asked: Any) -> list[Any]:
        return [
            (0, ConversionError("column 'amount' does not read as decimal"), True),
            (1, jsonio.ResourceLimitError("the row's large values pass the fetch limit"), True),
            (2, '{"amount": 1}', False),
        ]

    monkeypatch.setattr(fake, "rows", rows)
    table = _connector(targets)
    await table.prefetch(["K1", "K2", "K3"], USERS_SIDE)
    read = [await table.fetch(key, USERS_SIDE) for key in ("K1", "K2", "K3")]
    await table.aclose()
    assert [(o.http_status, o.error_class, o.error_detail) for o in read[:2]] == [
        (200, "unconvertible", "column 'amount' does not read as decimal"),
        (200, "resource_limit", "the row passes a parse limit"),
    ]
    assert (read[2].body, read[2].headers) == (b'{"amount": 1}', {"selected": "false"})


async def test_a_key_the_engine_calls_unfit_is_rejected_alone(
    targets: TargetRegistry, fake: FakeEngine
) -> None:
    table = _connector(targets)
    await table.prefetch(["K1", "unfit", "K2", "K3"], USERS_SIDE)
    assert fake.asked == [["K1", "unfit", "K2", "K3"], ["K1", "unfit"], ["K1"], ["unfit"], ["K2", "K3"]]
    assert [(await table.fetch(key, USERS_SIDE)).http_status for key in ("K1", "K2", "K3")] == [200] * 3
    with pytest.raises(KeyRejectedError):
        await table.fetch("unfit", USERS_SIDE)
    await table.aclose()


# --- a pair with a side on another engine (§7, §8, §12 P1 to P13) -------------------------------------


def _column(name: str, declared: str, cls: str | None, keyable: bool = True) -> Column:
    return Column(name, declared, cls, True, cls in ("text", "char"), keyable and cls is not None)


LEDGER = [
    _column("Id", "int", "integer"),
    _column("Code", "char(3)", "char"),
    _column("Amount", "decimal(10,2)", "decimal"),
    _column("Opened", "datetime", "timestamp"),
    _column("Flag", "tinyint(1)", "integer"),
    _column("Seen", "timestamp(6)", "instant", keyable=False),
    _column("Rate", "double", "float", keyable=False),
    _column("Padded", "decimal(8,2) unsigned zerofill", "decimal", keyable=False),
    _column("Shape", "point", None),
    _column("Span", "interval", "other", keyable=False),
]
BOOKS = [
    _column("id", "bigint", "integer"),
    _column("code", "text", "text"),
    _column("amount", "numeric(12,4)", "decimal"),
    _column("opened", "date", "date"),
    _column("flag", "boolean", "boolean", keyable=False),
    _column("seen", "timestamp without time zone", "timestamp", keyable=False),
    _column("rate", "numeric", "decimal"),
    _column("padded", "numeric(8,2)", "decimal"),
    _column("shape", "text", "text"),
    _column("span", "interval", "other", keyable=False),
]


@pytest.fixture
def across(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TargetRegistry, FakeEngine, FakeEngine]:
    """db-a on MySQL and db-b on PostgreSQL, each engine a fake holding ``ledger`` and ``books``."""
    mysql = {"engine": "mysql", "host": "ledger-db.example", "database": "ledger", "user": "ds_reader"}
    entries = {"db-a": mysql | {"password_ref": "file:password"}, "db-b": {"dsn_ref": "file:dsn"}}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": entries}))
    relations = {"ledger": LEDGER, "books": BOOKS}
    a, b = FakeEngine(relations), FakeEngine(relations)  # type: ignore[arg-type]
    a.name, a.fold, a.hides = "mysql", None, True  # type: ignore[misc, assignment]
    monkeypatch.setitem(ENGINES, "mysql", a)
    monkeypatch.setitem(ENGINES, "postgresql", b)
    return load_targets(tmp_path / "targets.yaml"), a, b


def _pair(a: dict[str, Any], b: dict[str, Any], types: dict[str, Any] | None = None) -> Any:
    table_a = {"name": "ledger", "keyColumns": ["id"]} | a
    return compiled(table_a, {"name": "books", "keyColumns": ["id"]} | b, types=types)


async def test_a_pair_across_engines_stores_engines_classes_and_one_rendering(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine],
) -> None:
    targets, _, _ = across
    spec = _pair({"columns": ["code", "AMOUNT"], "where": "flag = 1"}, {"columns": ["code", "amount"]})
    stored = await plan(targets, spec)
    assert stored is not None
    assert stored["a"] == {
        "schema": "public", "name": "ledger", "keys": ["Id"], "key_maps": [None],
        "columns": {"code": "Code", "amount": "Amount"}, "where": '"Flag" = 1', "types": {},
        "digest": ["amount", "code"], "engine": "mysql", "server": "8.4", "consistency": "snapshot",
        "tls": True,
        "classes": {"Id": "integer", "Code": "char", "Amount": "decimal", "Flag": "integer"},
        "declared": {"Id": "int", "Code": "char(3)", "Amount": "decimal(10,2)", "Flag": "tinyint(1)"},
    }  # fmt: skip
    assert {name: stored["b"][name] for name in ("engine", "classes", "digest")} == {
        "engine": "postgresql",
        "classes": {"id": "integer", "code": "text", "amount": "decimal"},
        "digest": ["amount", "code"],  # the worker hashes every compared column on both sides
    }
    assert (stored["rendering"], stored["only_a"], stored["only_b"]) == (1, [], [])
    mixed = await plan(targets, compiled({"name": "ledger", "keyColumns": ["id"], "columns": ["code"]}))
    assert mixed is not None and (mixed["a"]["engine"], mixed["a"]["digest"], mixed["rendering"]) == (
        "mysql",
        [],
        1,
    )


@pytest.mark.parametrize(
    ("a", "b", "types", "message"),
    [
        (
            {"keyColumns": ["rate"], "columns": ["code"]},
            {"columns": ["code"]},
            {},
            "side a: key column 'Rate' (double) cannot be a key: compare a view that casts it",
        ),
        (
            {"keyColumns": ["padded"], "columns": ["code"]},
            {"columns": ["code"]},
            {},
            "side a: key column 'Padded' (decimal(8,2) unsigned zerofill) cannot be a key:"
            " compare a view that casts it",
        ),
        (
            {"columns": ["code"]},
            {"keyColumns": ["seen"], "columns": ["code"]},
            {},
            "side b: key column 'seen' (timestamp without time zone) cannot be a key:"
            " compare a view that casts it",
        ),
        (
            {"keyColumns": ["amount"], "columns": ["code"]},
            {"keyColumns": ["amount"], "columns": ["code"]},
            {},
            "key column 'Amount' is decimal(10,2) on side a and numeric(12,4) on side b: a number key needs"
            " one scale on both sides (compare a view that casts it)",
        ),
        (
            {"columns": ["code"]},
            {"keyColumns": ["rate"], "columns": ["code"]},
            {},  # an integer's scale is 0; a numeric without a scale declares none
            "key column 'Id' is int on side a and numeric on side b: a number key needs one scale on both"
            " sides (compare a view that casts it)",
        ),
        (
            {"columns": ["code", "shape"]},
            {"columns": ["code", "shape"]},
            {},
            "side a: column 'Shape' (point) cannot be compared: compare a view that casts it",
        ),
        (
            {"columns": ["code"], "where": "shape IS NULL"},
            {"columns": ["code"]},
            {},
            "side a: column 'Shape' (point) cannot be compared: compare a view that casts it",
        ),
        (
            {"columns": ["span"]},
            {"columns": ["span"]},
            {},
            "side a: column 'span' (interval) cannot be compared with another engine:"
            " compare a view that casts it",
        ),
        (
            {"columns": ["opened"]},
            {"columns": ["opened"]},
            {},
            "side a: column 'opened' is a timestamp (datetime) and side b's a date: declare typeMap"
            " {Opened: {as: date}} on side a, or {as: timestamp} on both",
        ),
        (
            {"columns": ["flag"]},
            {"columns": ["flag"]},
            {},
            "side a: column 'flag' is a number (tinyint(1)) and side b's a boolean: declare typeMap"
            " {Flag: {as: boolean}} on side a",
        ),
        (
            {"columns": ["seen"]},
            {"columns": ["seen"]},
            {},
            "side a: column 'seen' is a zoned timestamp (timestamp(6)) and side b's a timestamp: declare"
            " typeMap {as: timestamp, naiveTimezone: ...} on both",
        ),
        (
            {"columns": ["code", {"name": "amount", "as": "shape"}]},
            {"columns": ["code", "shape"]},
            {},
            "side a: column 'shape' is a number (decimal(10,2)) and side b's text: declare a typeMap that"
            " gives both sides one type, or compare a view that casts it",
        ),
        (
            {"columns": ["opened"]},
            {"columns": ["opened"]},
            {"a": {"opened": {"as": "timestamp"}}},  # its Z form equals only another mapped timestamp
            "side a: column 'opened' is a mapped timestamp (datetime) and side b's a date: declare a typeMap"
            " that gives both sides one type, or compare a view that casts it",
        ),
    ],
    ids=[
        *("P6", "P6 zerofill", "P6 postgresql", "P6 scale", "P6 no scale", "P7", "P7 filter", "P8"),
        *("P9 date", "P9 boolean", "P9 zone", "P9 other", "P9 mapped"),
    ],
)
async def test_a_pair_across_engines_that_cannot_compare_is_refused(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine],
    a: dict[str, Any],
    b: dict[str, Any],
    types: dict[str, Any],
    message: str,
) -> None:
    targets, mysql, postgresql = across
    with pytest.raises(ValueError) as exc:
        await plan(targets, _pair(a, b, types))
    assert str(exc.value) == message
    assert mysql.probed == postgresql.probed == []  # refused before any statement is planned


@pytest.mark.parametrize(
    "types",
    [
        {"a": {"Opened": {"as": "date"}}},
        {"a": {"opened": {"as": "timestamp"}}, "b": {"opened": {"as": "timestamp"}}},
        {"a": {"OPENED": {"as": "string"}}, "b": {"opened": {"as": "string"}}},
    ],
)
async def test_a_type_map_gives_both_sides_one_family(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine], types: dict[str, Any]
) -> None:
    types = types | {"a": types["a"] | {"flag": {"as": "boolean"}}}
    stored = await plan(
        across[0], _pair({"columns": ["opened", "flag"]}, {"columns": ["opened", "flag"]}, types)
    )
    assert stored is not None and stored["a"]["digest"] == ["flag", "opened"]
    assert stored["a"]["types"]["flag"] == {"as": "boolean"} and "opened" in stored["a"]["types"]


async def test_a_column_is_found_in_any_case_on_mysql_and_the_catalogue_may_hide_it(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine],
) -> None:
    targets, mysql, _ = across  # both fakes hold the same relations
    mysql.relations["twins"] = ["id", "Email", "EMAIL"]
    for table, message in (
        ({"name": "ledger", "columns": ["nope"]}, "column 'nope' is not in public.ledger"),
        ({"name": "twins", "columns": ["email"]}, "column 'email' is not in public.twins"),  # two match
    ):
        with pytest.raises(ValueError) as exc:
            await plan(targets, _pair(table, {"columns": ["code"]}))
        assert str(exc.value) == f"side a: {message}, or the role may not read it: grant it SELECT"
    with pytest.raises(ValueError) as exc:  # PostgreSQL folds to lower case and lists every column
        await plan(targets, _pair({"columns": ["code"]}, {"name": "twins", "columns": ["eMail"]}))
    assert str(exc.value) == "side b: column 'eMail' is not in public.twins"
    stored = await plan(
        targets,
        _pair({"name": "twins", "columns": ["Email"]}, {"columns": [{"name": "code", "as": "email"}]}),
    )
    assert stored is not None and stored["a"]["columns"] == {"email": "Email"}  # as spelled first


@pytest.mark.parametrize(
    ("failing", "message"),
    [
        (
            {"connect": [TargetAuthRefused("MySQL 1045", "the credential was refused")]},
            "cannot connect to target db-a (mysql ledger-db.example:3306/ledger as ds_reader):"
            " MySQL 1045: the credential was refused",
        ),
        (
            {"connect": [TargetUnreachable("MySQL 1049")]},
            "cannot connect to target db-a (mysql ledger-db.example:3306/ledger as ds_reader): MySQL 1049",
        ),
        (
            {"connect": [None, TargetUnreachable("MySQL 2013")]},  # the session of the probe
            "cannot connect to target db-a (mysql ledger-db.example:3306/ledger as ds_reader): MySQL 2013",
        ),
        (
            {
                "connect": [
                    UnsafeTargetError(
                        "10.1.2.3 is a private address: set allow_private_network on the target"
                    )
                ]
            },
            "cannot connect to target db-a (mysql ledger-db.example:3306/ledger as ds_reader):"
            " 10.1.2.3 is a private address: set allow_private_network on the target",
        ),
        (
            {
                "connect": [
                    Refusal("target db-a is MariaDB 11.4: MySQL 8.0.17 or later is supported, MariaDB is not")
                ]
            },
            "target db-a is MariaDB 11.4: MySQL 8.0.17 or later is supported, MariaDB is not",
        ),
        (
            {"probe": [Refusal("the filter compares column 'Id' (int) with a literal that is not a number")]},
            "the filter compares column 'Id' (int) with a literal that is not a number",
        ),
        (
            {"probe": [TargetRefused("MySQL 1525")]},
            "the database refuses the filter or key map: MySQL 1525",
        ),
    ],
    ids=["P1 gloss", "P1 code", "P1 probe", "P1 guard", "P2", "P11", "P13"],
)
async def test_a_side_on_another_engine_is_refused_in_its_words(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine], failing: dict[str, Any], message: str
) -> None:
    targets, mysql, _ = across
    mysql.failing |= failing
    with pytest.raises(ValueError) as exc:
        await plan(targets, _pair({"columns": ["code"]}, {"columns": ["code"]}))
    assert str(exc.value) == f"side a: {message}"
    assert mysql.opened == mysql.closed


# One relation declared twice: ``wider`` in two ways, ``doc`` a large value on both sides.
HERE = [
    _column("id", "int", "integer"),
    _column("same", "varchar(10)", "text"),
    _column("raw", "varbinary(4)", "bytes"),
    _column("wider", "decimal(10,2)", "decimal"),
    _column("mapped", "varchar(10)", "text"),
    replace(_column("doc", "longtext", "text", keyable=False), lob=True),
]
THERE = [replace(c, declared="decimal(12,4)") if c.name == "wider" else c for c in HERE]


@pytest.fixture
def within(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], tuple[TargetRegistry, FakeEngine]]:
    """``within(engine)``: db-a and db-b both on that engine, a fake holding ``here`` and ``there``."""

    def make(engine: str) -> tuple[TargetRegistry, FakeEngine]:
        name = (
            {"database": "ledger"} if engine == "mysql" else {"service": "LEDGER", "allow_unencrypted": True}
        )
        entry = {"engine": engine, "host": "db.example", "user": "ds_reader", "password_ref": "file:password"}
        entries = {"db-a": entry | name, "db-b": entry | name}
        (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": entries}))
        fake = FakeEngine({"here": HERE, "there": THERE})  # type: ignore[dict-item]
        fake.name = engine
        monkeypatch.setitem(ENGINES, engine, fake)
        return load_targets(tmp_path / "targets.yaml"), fake

    return make


@pytest.mark.parametrize(
    ("engine", "facts", "worker"),
    [
        ("mysql", {}, ["doc", "mapped", "wider"]),
        ("mysql", {"charset": "latin1"}, ["doc", "mapped", "wider"]),  # its hash reads utf8mb4 texts
        ("oracle", {"charset": "AL32UTF8", "national": "AL16UTF16"}, ["doc", "mapped", "wider"]),
        ("oracle", {"charset": "AL32UTF8", "national": "UTF8"}, ["doc", "mapped", "raw", "same", "wider"]),
        ("oracle", {"charset": "WE8MSWIN1252", "national": "AL16UTF16"}, None),
    ],
    ids=["mysql", "mysql charsets", "oracle", "oracle national", "oracle charset"],
)
async def test_one_engine_hashes_the_columns_both_sides_declare_alike(
    within: Callable[[str], tuple[TargetRegistry, FakeEngine]],
    engine: str,
    facts: dict[str, str],
    worker: list[str] | None,
) -> None:
    targets, fake = within(engine)
    fake.facts = {"here": {"charset": "AL32UTF8", "national": "AL16UTF16"} if engine == "oracle" else {}}
    fake.facts["there"] = facts
    spec = compiled(
        {"name": "here", "keyColumns": ["id"]}, {"name": "there", "keyColumns": ["id"]},
        types={"b": {"mapped": {"as": "string"}}},
    )  # fmt: skip
    stored = await plan(targets, spec)
    assert stored is not None
    # The worker hashes a column declared in two ways, one a typeMap names and one that may hold a large
    # value; the engine the others, unless the two Oracle databases differ in a character set (§10).
    everything = ["doc", "mapped", "raw", "same", "wider"]
    assert stored["a"]["digest"] == stored["b"]["digest"] == (worker or everything)
    assert [stored[side]["engine"] for side in "ab"] == [engine, engine] and stored["rendering"] == 1


@pytest.mark.parametrize(
    ("a", "b", "refused"),
    [
        ("NUMBER", "bigint", False),  # an unconstrained NUMBER's key text is a whole number's: scale 0
        ("NUMBER(10)", "int", False),
        ("NUMBER(*,2)", "numeric(12,2)", False),
        ("NUMBER(10,2)", "decimal(10,2)", False),
        ("NUMBER", "numeric(10,2)", True),
        ("NUMBER(10,2)", "numeric(12,4)", True),
        ("NUMBER", "numeric", True),  # PostgreSQL's declares no scale
    ],
)
async def test_a_number_key_pairs_only_at_one_scale(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine], a: str, b: str, refused: bool
) -> None:
    targets, engine, _ = across

    def keyed(declared: str) -> list[Any]:
        integer = declared in ("bigint", "int", "NUMBER(10)")
        return [_column("k", declared, "integer" if integer else "decimal"), _column("v", "text", "text")]

    engine.relations |= {"left": keyed(a), "right": keyed(b)}
    spec = compiled({"name": "left", "keyColumns": ["k"]}, {"name": "right", "keyColumns": ["k"]})
    if not refused:
        assert await plan(targets, spec) is not None
        return
    with pytest.raises(ValueError) as exc:
        await plan(targets, spec)
    assert str(exc.value) == (
        f"key column 'k' is {a} on side a and {b} on side b: a number key needs one scale on both sides"
        " (compare a view that casts it)"
    )


@pytest.mark.parametrize(
    ("a", "b", "never"),
    [
        # A date key moved to Oracle, which has no date-only type: 2024-01-01 against 2024-01-01 00:00:00.
        (("date", "date"), ("DATE", "timestamp"), "a date is never that of a timestamp"),
        (("datetime", "timestamp"), ("date", "date"), "a timestamp is never that of a date"),
        (("RAW(16)", "bytes"), ("bigint", "integer"), "bytes is never that of a number"),
        (("date", "date"), ("numeric(8,0)", "decimal"), "a date is never that of a number"),
        (("DATE", "timestamp"), ("bytea", "bytes"), "a timestamp is never that of bytes"),
        (("date", "date"), ("text", "text"), None),  # a text may be any key's text
        (("char(10)", "char"), ("DATE", "timestamp"), None),
        (("varchar(10)", "text"), ("bigint", "integer"), None),
        (("date", "date"), ("date", "date"), None),
        (("DATE", "timestamp"), ("datetime", "timestamp"), None),
        (("RAW(16)", "bytes"), ("bytea", "bytes"), None),
    ],
)
async def test_two_key_columns_whose_texts_are_never_equal_are_refused(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine], a: tuple[str, str], b: tuple[str, str], never: str
) -> None:
    # Planned, every row would be reported missing on both sides.
    targets, engine, _ = across
    engine.relations |= {"left": [_column("k", *a), "v"], "right": [_column("k", *b), "v"]}
    spec = compiled({"name": "left", "keyColumns": ["k"]}, {"name": "right", "keyColumns": ["k"]})
    if never is None:
        assert await plan(targets, spec) is not None
        return
    with pytest.raises(ValueError) as exc:
        await plan(targets, spec)
    assert str(exc.value) == (
        f"key column 'k' is {a[0]} on side a and {b[0]} on side b: the key text of {never}"
        " (compare a view that casts it)"
    )
    assert engine.probed == []  # refused before any statement is planned


async def test_a_key_map_bridges_two_key_columns_of_two_families(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine],
) -> None:
    # A mapped key's text is its map's, so the columns' own texts need not be equal.
    targets, engine, _ = across
    engine.relations |= {
        "left": [_column("k", "bigint", "integer"), "v"],
        "right": [_column("k", "DATE", "timestamp"), "v"],
    }
    padded = {
        "name": "k",
        "keyMap": {"kind": "prefix_pad", "prefix": "D-", "width": 8, "sideType": "integer"},
    }
    right = {"name": "right", "keyColumns": ["k"]}
    assert await plan(targets, compiled({"name": "left", "keyColumns": [padded]}, right)) is not None
    with pytest.raises(ValueError, match="the key text of a number is never that of a timestamp"):
        await plan(targets, compiled({"name": "left", "keyColumns": ["k"]}, right))


async def test_a_relation_the_role_may_not_read_names_the_engine_s_grant(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    targets, engine, _ = across
    engine.relations["vault"] = [Column("id", "NUMBER(5)", "integer", False, False)]
    monkeypatch.setattr(engine, "grant", "READ", raising=False)
    with pytest.raises(ValueError) as exc:
        await plan(targets, _pair({"name": "vault"}, {"columns": ["code"]}))
    assert str(exc.value) == "side a: the role may not read public.vault: grant it READ"
    engine.failing["describe"] = [
        Refusal("table or view far not found or not visible: a synonym over a database link is not read")
    ]
    with pytest.raises(ValueError) as exc:  # what an engine does not follow is said with its side
        await plan(targets, _pair({"name": "vault"}, {"columns": ["code"]}))
    assert str(exc.value) == (
        "side a: table or view far not found or not visible: a synonym over a database link is not read"
    )
    assert engine.opened == engine.closed


# --- the plan from the two catalogues alone, and every refusal of a step (docs/mvp/PRECHECKS.md §5) ----


async def _held(spec: Any, **engines: FakeEngine) -> dict[str, Any]:
    """Each table side's relation, as its side's engine describes it."""
    tables = {cfg.side.value: cfg.table for cfg in spec.sides.values() if cfg.table is not None}
    return {
        side: await engines[side].describe(None, table.schema, table.name) for side, table in tables.items()
    }


async def _refusals(targets: TargetRegistry, spec: Any, **engines: FakeEngine) -> list[str]:
    """``issues`` of the pair; ``plan`` and ``resolved`` raise the first of them."""
    relations = await _held(spec, **engines)
    found = issues(targets, spec, relations)
    for raised in (lambda: plan(targets, spec), lambda: _resolved(targets, spec, relations)):
        with pytest.raises(ValueError) as exc:
            await raised()
        assert str(exc.value) == found[0]
    return found


async def _resolved(targets: TargetRegistry, spec: Any, relations: dict[str, Any]) -> Any:
    return resolved(targets, spec, relations)


async def test_issues_lists_every_refusal_of_a_step_and_plan_raises_the_first(
    targets: TargetRegistry, fake: FakeEngine
) -> None:
    secret = Column("secret", "text", "text", False, True)
    fake.relations = RELATIONS | {"users": [*RELATIONS["users"], secret]}  # type: ignore[dict-item]
    # Each side's names and grants: a key, two listed columns and a filter column that are none, a
    # column the role may not read, and on side b one more. A name that is no column is judged no further.
    users = {"keyColumns": ["nokey"], "columns": ["email", "ghost", "Phantom", "secret"], "where": "gone = 1"}
    spec = compiled(
        {"name": "users"} | users, {"name": "users_v", "keyColumns": ["user_id"], "columns": ["nope"]}
    )
    assert await _refusals(targets, spec, a=fake, b=fake) == [
        "side a: column 'nokey' is not in public.users",
        "side a: column 'ghost' is not in public.users",
        "side a: column 'Phantom' is not in public.users",
        "side a: column 'gone' is not in public.users",
        "side a: the role may not read column 'secret' of public.users: grant it SELECT",
        "side b: column 'nope' is not in public.users_v",
    ]
    # The pair's counterparts, judged once both sides resolve: every listed column without one.
    listed = ["email", {"name": "full_name", "as": "x"}, "legacy_flag"]
    spec = compiled(USERS | {"columns": listed}, USERS_V)
    assert await _refusals(targets, spec, a=fake, b=fake) == [
        "side a: column 'x' has no counterpart on side b",
        "side a: column 'legacy_flag' has no counterpart on side b",
        "side b: column 'name' has no counterpart on side a",
    ]
    # What each side compares: a typeMap column that is not compared, on both sides.
    types = {"a": {"legacy_flag": {"as": "boolean"}}, "b": {"name": {"as": "string"}}}
    spec = compiled(USERS | {"columns": ["email"]}, USERS_V | {"columns": ["Email"]}, types=types)
    assert await _refusals(targets, spec, a=fake, b=fake) == [
        "side a: typeMap column 'legacy_flag' is not compared",
        "side b: typeMap column 'name' is not compared",
    ]
    assert fake.probed == []  # none was planned


async def test_issues_across_engines_lists_each_class_then_each_family(
    across: tuple[TargetRegistry, FakeEngine, FakeEngine],
) -> None:
    targets, a, b = across
    compared = ["code", "shape", "span", "opened", "flag"]
    spec = _pair({"keyColumns": ["rate"], "columns": compared}, {"columns": compared})
    assert await _refusals(targets, spec, a=a, b=b) == [  # the classes a side reads: P7, P6
        "side a: column 'Shape' (point) cannot be compared: compare a view that casts it",
        "side a: key column 'Rate' (double) cannot be a key: compare a view that casts it",
    ]
    spec = _pair({"columns": compared[2:]}, {"columns": compared[2:]})
    assert await _refusals(targets, spec, a=a, b=b) == [  # the pair's families: P8, then each P9
        "side a: column 'span' (interval) cannot be compared with another engine: compare a view that"
        " casts it",
        "side a: column 'opened' is a timestamp (datetime) and side b's a date: declare typeMap {Opened:"
        " {as: date}} on side a, or {as: timestamp} on both",
        "side a: column 'flag' is a number (tinyint(1)) and side b's a boolean: declare typeMap {Flag: {as:"
        " boolean}} on side a",
    ]


async def test_a_pair_the_catalogues_refuse_nothing_of_is_planned_without_a_session(
    targets: TargetRegistry, fake: FakeEngine
) -> None:
    spec = compiled(USERS, USERS_V)
    relations = await _held(spec, a=fake, b=fake)
    opened = fake.opened
    assert issues(targets, spec, relations) == []
    found = resolved(targets, spec, relations)
    assert (fake.opened, fake.probed) == (opened, [])  # pure: no session, no probe
    made = await planning(targets, spec)
    assert made.plan == found == GOLDEN["pair"] and made.relations == relations
    assert fake.probed == [found["a"], found["b"]] and fake.opened == fake.closed == opened + 4
    mixed = await planning(targets, compiled({"name": "users", "keyColumns": ["userid"]}))
    assert mixed.plan == GOLDEN["mixed"] and list(mixed.relations) == ["a"]
    service = replace(spec, sides={side: replace(cfg, table=None) for side, cfg in spec.sides.items()})
    assert await planning(targets, service) == Planned(None, {})  # no side is a table


# --- a file side's plan (docs/mvp/FILES.md §6) ------------------------------------------------------------

DECLARED = [
    Column("id", "integer", "integer", True, False),
    Column("balance", "numeric(12,2)", "decimal", True, False),
]


def _ledger(base: Path, data: bytes = b"id|balance\n1|1,50\n2|2,00\n") -> None:
    root = base / "files" / "root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "ledger.csv").write_bytes(data)


def _against(base: Path, monkeypatch: pytest.MonkeyPatch, table: dict[str, Any], **side: Any) -> Any:
    """A CSV side a against a PostgreSQL side b (a fake engine's ``ledger``): its targets and its spec."""
    monkeypatch.setitem(ENGINES, "postgresql", FakeEngine({"ledger": list(DECLARED)}))
    found = ducked.registry(base, {"db-b": {"dsn_ref": "file:dsn"}})
    doc = ducked.file_spec(
        table, {"role": "new", "target": "db-b", "table": {"name": "ledger", "keyColumns": ["id"]}}, **side
    )
    return found, compile_spec(doc, targets=found)


async def test_a_csv_field_against_a_number_needs_its_declared_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENGINES §7's check runs with a file side: P6 and P9 worded for a CSV field (FILES.md §15); the
    PostgreSQL side stores its classes."""
    _ledger(tmp_path)
    table = ducked.csv_table("ledger.csv", ["id", "balance"])
    found, spec = _against(tmp_path, monkeypatch, table)
    with pytest.raises(ValueError) as refused:
        await planning(found, spec)
    assert str(refused.value) == (
        "side a: key column 'id' is text (a CSV field) and side b's a number: declare csv.keys {id: {as:"
        " integer}}"
    )
    table["file"]["csv"]["keys"] = {"id": {"as": "integer"}}
    found, spec = _against(tmp_path, monkeypatch, table)
    with pytest.raises(ValueError) as refused:
        await planning(found, spec)
    assert str(refused.value) == (
        "side a: column 'balance' is text (a CSV field) and side b's a number (numeric(12,2)): declare"
        " typeMap {balance: {as: decimal}} on side a"
    )
    found, spec = _against(
        tmp_path, monkeypatch, table, typeMap={"balance": {"as": "decimal", "decimalSeparator": ","}}
    )
    made = await planning(found, spec, scanning=True)
    assert made.plan is not None and set(made.pinned) == {"a"} and "pinned" not in made.plan["a"]
    assert (
        made.plan["b"]["classes"] == {"id": "integer", "balance": "decimal"} and made.plan["rendering"] == 1
    )
    assert made.plan["a"]["declared"] == {"id": "a CSV field as integer", "balance": "a CSV field"}
    assert made.relations["a"].facts == {
        "kind": "files",
        "key": None,
        "server": made.plan["a"]["server"],
        "consistency": "fixed",
        "charset": None,
    }


async def test_a_parquet_key_of_another_class_cannot_be_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    select = "SELECT TIMESTAMP '2024-01-01' AS id, 1.5::DECIMAL(12,2) AS balance"
    ducked.unrestricted().execute(f"COPY ({select}) TO '{root}/l.parquet' (FORMAT parquet)")
    found, spec = _against(
        tmp_path,
        monkeypatch,
        {"name": "l", "keyColumns": ["id"], "file": {"path": "l.parquet", "format": "parquet"}},
    )
    with pytest.raises(ValueError) as refused:
        await planning(found, spec)
    assert str(refused.value) == (
        "side a: key column 'id' (TIMESTAMP) cannot be a key in a file: export it as text, a whole number, a"
        " decimal or a date"
    )


async def test_two_file_sides_are_checked_as_two_engines_are(tmp_path: Path) -> None:
    """ENGINES §7's check runs between two file sides too: a CSV field against a Parquet number (P6, P9),
    two Parquet columns of two families (ENGINES' P9); declared alike, the pair is planned with both sides'
    classes and rendering 1."""
    root = tmp_path / "files" / "root"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "l.csv").write_bytes(b"id|amount|seen\n1|1.50|2024-01-01\n")
    typed = "SELECT 1::BIGINT AS id, 1.5::DECIMAL(12,2) AS amount, {seen} AS seen"
    for name, seen in (("b/l.parquet", "TIMESTAMP '2024-01-01'"), ("a/l.parquet", "DATE '2024-01-01'")):
        ducked.unrestricted().execute(f"COPY ({typed.format(seen=seen)}) TO '{root / name}' (FORMAT parquet)")
    other = {"engine": "files", "root": str((root / "b").resolve()), "settle_s": 0, "allow_writable": True}
    found = ducked.registry(tmp_path, {"other": other}, root=str((root / "a").resolve()))
    b = {"name": "l", "keyColumns": ["id"], "file": {"path": "l.parquet", "format": "parquet"}}
    side_b = {"role": "new", "target": "other", "table": b}

    async def refused(table: dict[str, Any], **side: Any) -> str:
        with pytest.raises(ValueError) as error:
            await planning(found, compile_spec(ducked.file_spec(table, side_b, **side), targets=found))
        return str(error.value)

    table = ducked.csv_table("l.csv", ["id", "amount", "seen"])
    assert await refused(table) == (
        "side a: key column 'id' is text (a CSV field) and side b's a number: declare csv.keys {id: {as:"
        " integer}}"
    )
    table["file"]["csv"]["keys"] = {"id": {"as": "integer"}}
    assert await refused(table, typeMap={"seen": {"as": "timestamp"}}) == (
        "side a: column 'amount' is text (a CSV field) and side b's a number (DECIMAL(12,2)): declare typeMap"
        " {amount: {as: decimal}} on side a"
    )
    both = (await refused(b)).split(":")
    assert both[:2] == ["side a", " column 'seen' is a date (DATE) and side b's a timestamp"]
    types = {"amount": {"as": "decimal"}, "seen": {"as": "timestamp"}}
    doc = ducked.file_spec(table, side_b | {"typeMap": {"seen": {"as": "timestamp"}}}, typeMap=types)
    made = await planning(found, compile_spec(doc, targets=found), scanning=True)
    assert made.plan is not None and set(made.pinned) == {"a", "b"} and made.plan["rendering"] == 1
    assert made.plan["b"]["classes"] == {"id": "integer", "amount": "decimal", "seen": "timestamp"}
    # A CSV field and a Parquet column are declared apart, and a typeMap names both: the worker's columns.
    assert made.plan["a"]["digest"] == made.plan["b"]["digest"] == ["amount", "seen"]
    # Two Parquet sides that declare a column alike leave it to DuckDB's hash (D7), 32 bytes a row to sort:
    # beside the key's text three times, 24 bytes for each of the four values carried and 16 a row (step 8).
    alike = dict(b, columns=["amount"])
    doc = ducked.file_spec(alike, side_b | {"table": alike})
    made = await planning(found, compile_spec(doc, targets=found), scanning=True)
    assert made.plan is not None and made.plan["a"]["digest"] == made.plan["b"]["digest"] == []
    assert made.plan["a"]["sort_bytes"] == math.ceil(1.15 * (3 * 1 + 24 * 4 + 32 + 16))


async def test_a_pass_must_fit_the_timeout_and_a_scan_s_sort_the_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P31 (a per-key read is one pass), and P32 when the run scans; the counts of the same bytes under the
    same declaration are counted once a process."""
    _ledger(tmp_path)
    table = ducked.csv_table("ledger.csv", ["id", "balance"], keys={"id": {"as": "integer"}})
    found, spec = _against(
        tmp_path, monkeypatch, table, typeMap={"balance": {"as": "decimal", "decimalSeparator": ","}}
    )
    counted: list[int] = []
    real = duck._count
    monkeypatch.setattr(duck, "_counted", {})  # this process's counts of these bytes so far

    async def count(session: Any, side: Any, who: str) -> dict[str, Any]:
        counted.append(1)
        return await real(session, side, who)

    monkeypatch.setattr(duck, "_count", count)
    first = await planning(found, spec, scanning=True)
    again = await planning(found, spec, scanning=True)
    assert counted == [1] and first.plan == again.plan | {
        "a": again.plan["a"] | {"pinned_at": first.plan["a"]["pinned_at"]}
    }
    duck._counted.clear()

    async def slow(session: Any, side: Any, who: str) -> dict[str, Any]:
        return await real(session, side, who) | {"pass_s": 48.0}

    monkeypatch.setattr(duck, "_count", slow)
    with pytest.raises(ValueError) as refused:
        await planning(found, spec)
    assert str(refused.value) == (
        "side a: one pass over its files took 48 s and a per-key read is one pass: set the spec's"
        " execution.timeouts.total to 96s or more (it is 60s), or export Parquet"
    )
    duck._counted.clear()

    async def wide(session: Any, side: Any, who: str) -> dict[str, Any]:
        return await real(session, side, who) | {"sort_bytes": 3 * 2**30}

    monkeypatch.setattr(duck, "_count", wide)
    assert (await planning(found, spec)).plan is not None  # no scan, no sort
    duck._counted.clear()
    with pytest.raises(ValueError) as refused:
        await planning(found, spec, scanning=True)
    assert str(refused.value) == (
        "side a: sorting its 2 rows needs about 3 GiB of temporary space and files.spill allows 1 GiB: raise"
        " files.spill with the volume under files.spill_dir, list fewer columns, or filter the rows"
    )
    duck._counted.clear()


async def test_a_scan_planned_just_under_the_spill_limit_completes(tmp_path: Path) -> None:
    """P32, §6 step 8: sort_bytes projects what DuckDB's sort of the side spills, and the same figure is
    judged against files.spill when the run is created. Two million rows of a 44-character key spilled 288
    MiB under 128 MiB of memory (observed on 1.5.5); their texts and 16 bytes a row, which P32 judged before,
    are 175 MiB, so a limit of 200 MiB was accepted and the scan failed with F3 once every row was read. It
    is refused now, and a side planned just under its limit is scanned to its end."""
    root, rows = tmp_path / "files" / "root", 2_000_000
    root.mkdir(parents=True)
    key = "to_base64(unhex(sha256(CAST(i AS VARCHAR))))"  # 44 characters DuckDB's spill does not compress
    ducked.unrestricted().execute(
        f"COPY (SELECT {key} AS id, 'x' AS v FROM range({rows}) t(i)) TO '{root}/t.csv'"  # noqa: S608
        " (FORMAT csv, DELIMITER '|', HEADER true)"
    )
    found = ducked.registry(tmp_path)
    spec = compile_spec(ducked.file_spec(ducked.csv_table("t.csv", ["id", "v"])), targets=found)

    async def planned(spill: str) -> Planned:
        duck.configure(ducked.settings(tmp_path, memory="128MB", spill=spill), found)
        return await planning(found, spec, scanning=True)

    with pytest.raises(ValueError) as refused:
        await planned("200MB")
    assert str(refused.value) == (
        "side a: sorting its 2,000,000 rows needs about 1 GiB of temporary space and files.spill allows 0"
        " GiB: raise files.spill with the volume under files.spill_dir, list fewer columns, or filter the"
        " rows"
    )
    need = (await planned("1GB")).plan["a"]["sort_bytes"]
    assert need == math.ceil(1.15 * rows * (3 * 44 + 24 * 4 + 32 + 16))  # 605 MiB
    made = await planned(f"{math.ceil(need / 2**20)}MB")
    side, engine = pins(made.plan, made.pinned)["a"], ENGINES["files"]
    async with engine.snapshot(found.get("exports")) as session:
        assert sum([len(batch) async for batch in await engine.scan(session, side, None)]) == rows


def test_a_file_side_s_pins_are_sealed_and_left_out_of_what_is_shown(tmp_path: Path) -> None:
    """§6: params.pinned is sealed by the run's sealer; no plan_sha256, pack or answer holds it, and no answer
    each side's files_sha256."""
    from deltasleuth.api.deps import shown
    from deltasleuth.security.crypto import Sealer

    sealer = Sealer(b"\x01" * 32, "R-1")
    held = {"a": {"files": ["x.csv"], "pins": [[1, 2, 3, 4, "ab" * 32]]}}
    params = {
        "tables": {"a": {"name": "t", "files_sha256": "cd" * 32}},
        "pinned": sealed(sealer, held),
    }
    assert set(params["pinned"]["a"]) == {"enc"} and "x.csv" not in json.dumps(params)
    assert unsealed(sealer, params)["a"]["pinned"] == held["a"] | {"side": "a"}
    assert "pinned" not in json.loads(approvals.plan(params))
    view = shown({"run_id": "R-1", "params": params})
    assert view["params"] == {"tables": {"a": {"name": "t"}}}


# --- a key table's plan (``key_plan``) ----------------------------------------------------------------------

KEYS_TABLE = {"name": "shelf_keys", "keyColumns": ["region", "code"], "where": "tier = 'gold'"}
SHELF_KEYS = [
    _column("region", "varchar(2)", "text"),
    _column("code", "varchar(12)", "text"),
    _column("tier", "varchar(8)", "text"),
    replace(_column("note", "longtext", "text", keyable=False), lob=True),
]
KEY_TARGETS = {
    "postgresql": {"dsn_ref": "file:dsn"},
    "mysql": {"engine": "mysql", "host": "db.example", "user": "ds_reader", "database": "ref"},
    "oracle": {"engine": "oracle", "host": "db.example", "user": "ds_reader", "service": "REF"},
}


def _key_source(target: str, **table: Any) -> Any:
    return key_table({"kind": "table", "target": target, "table": KEYS_TABLE | table})


@pytest.mark.parametrize("engine", ["postgresql", "mysql", "oracle"])
async def test_key_plan_reads_no_column(engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key table's plan is a table side's with no compared column: its relation described and its plan
    probed on a session each, its key columns and its filter's resolved and granted, and off PostgreSQL only
    those classed. No statement made of it names another column, and a refusal says ``the key source``."""
    entry = KEY_TARGETS[engine] | ({} if engine == "postgresql" else {"password_ref": "file:password"})
    entry |= {"allow_unencrypted": True} if engine == "oracle" else {}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"keys-db": entry}}))
    targets = load_targets(tmp_path / "targets.yaml")
    fake = FakeEngine({"shelf_keys": SHELF_KEYS})  # type: ignore[dict-item]
    fake.name = engine
    monkeypatch.setitem(ENGINES, engine, fake)
    found = await key_plan(targets, _key_source("keys-db"))
    read = ["region", "code", "tier"]  # its keys, then its filter's column
    classed = {
        "engine": engine, "server": "8.4", "consistency": "snapshot", "tls": True,
        "classes": dict.fromkeys(read, "text"),
        "declared": {"region": "varchar(2)", "code": "varchar(12)", "tier": "varchar(8)"},
    }  # fmt: skip
    assert found == {
        "schema": "public", "name": "shelf_keys", "keys": ["region", "code"], "key_maps": [None, None],
        "columns": {}, "where": '"tier" = \'gold\'', "types": {}, "digest": [],
    } | ({} if engine == "postgresql" else classed)  # fmt: skip
    assert fake.probed == [found] and fake.opened == fake.closed == 2
    if engine == "postgresql":
        sent = await _sent(found, ["EU|t1"], [b"EU", b"t1"])
        statements = [text for name in ("census", "null_keys", "probe") for _, text, _ in sent[name]]
    else:
        primitives = {"mysql": mysql.PRIMITIVES, "oracle": oracle.PRIMITIVES}[engine]
        statements = [
            make(primitives, found)[0] for make in (compose.census, compose.null_keys, compose.probe)
        ]
    assert len(statements) == 3 and not any("note" in text for text in statements)
    assert all(all(column in text for column in read) for text in statements)
    with pytest.raises(ValueError) as refused:
        await key_plan(targets, _key_source("keys-db", keyColumns=["region", "sku"]))
    assert str(refused.value) == "the key source: column 'sku' is not in public.shelf_keys"
    fake.failing["connect"] = [TargetUnreachable("ORA-12541", "no server answered")]
    with pytest.raises(ValueError) as unconnected:
        await key_plan(targets, _key_source("keys-db"))
    assert unconnected.value.brief == "the key source: cannot connect to target keys-db"  # type: ignore[attr-defined]
    assert str(unconnected.value).startswith("the key source: cannot connect to target keys-db")


async def test_key_plan_reads_no_column_of_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A key table of files: pinned and counted as a file side's, its pins left in its plan as its readers
    take them, and no column selected but its key's. Its lines that are not rows are counted into its plan,
    and the reader's log line, which says where they are, names the key source: no side reads the file."""
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    (root / "keys.csv").write_bytes(b"region|code|tier|note\neu|t2|gold|x\neu|t1|gold|y\nus|t3|tin|z\n")
    found = ducked.registry(tmp_path)
    table = ducked.csv_table("keys.csv", ["region", "code", "tier", "note"]) | KEYS_TABLE
    plan = await key_plan(found, _key_source("exports", **table))
    assert (plan["columns"], plan["digest"], plan["keys"]) == ({}, [], ["region", "code"])
    assert plan["classes"] == dict.fromkeys(["region", "code", "tier"], "text")
    assert (plan["engine"], plan["selected"], plan["consistency"]) == ("files", 2, "fixed")
    assert set(plan["pinned"]) == {"side", "files", "pins"} and plan["pinned"]["files"] == ["keys.csv"]
    census, _ = compose.census(duck.PRIMITIVES, plan)  # the reader parses every declared field of a line
    assert census.startswith('SELECT t."region", t."code" FROM (SELECT * FROM read_csv(')
    engine = ENGINES["files"]
    async with engine.snapshot(found.get("exports"), timeout_s=30) as session:
        rows = [tuple(row) async for batch in engine.census(session, plan) for row in batch]
        assert rows == [("eu", "t1"), ("eu", "t2")] and await engine.null_keys(session, plan) == 0
    with pytest.raises(ValueError) as refused:
        await key_plan(found, _key_source("exports", **table | {"where": "grade = 'gold'"}))
    assert str(refused.value) == "the key source: column 'grade' is not in shelf_keys"
    assert plan["rejected"] == 0 and not any("not rows" in message for message in caplog.messages)
    (root / "keys.csv").write_bytes(b"region|code|tier|note\neu|t2|gold|x\nde|t7|gold\nfr|t8|gold|y|z\n")
    plan = await key_plan(found, _key_source("exports", **table))
    assert (plan["selected"], plan["rejected"], plan["kinds"]) == (1, 2, {"columns": 2})
    assert set(plan["pinned"]) == {"side", "files", "pins"}
    assert [message for message in caplog.messages if "not rows" in message] == [
        "the key source: 2 line(s) of its files are not rows: file 1 line 3: columns; file 1 line 4: columns"
    ]
