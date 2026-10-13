"""Table sides on a real Oracle: the session, the column plan, per-key
reads, keys the column cannot hold, and what a scan is read in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tracemalloc
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import oracledb
import psycopg
import pytest
import yaml
from cryptography.hazmat.primitives import serialization

from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.tables import (
    ENGINES,
    Plan,
    Refusal,
    SnapshotLost,
    StatementTimeout,
    TableConnector,
    TableError,
    TargetRefused,
    compose,
    figures,
    plan,
    preflight,
)
from deltasleuth.connectors.tables import oracle as engine_module
from deltasleuth.core.spec import TargetRegistry, load_targets
from deltasleuth.core.types import ExecutionConfig, Observation
from tests.conftest import OracleServer, make_ca

from .test_guards import kept_alive
from .test_postgres import USERS_SIDE, compiled

pytestmark = [pytest.mark.db, pytest.mark.oracle]

ORACLE = ENGINES["oracle"]
# Unquoted names are stored in upper case; "Mixed" is spelled in two cases. ``note`` is invisible to
# SELECT *, and the index makes the server add a column of its own. ``there`` holds the rows of ``here``
# with a wider number, but row 8.
SEED = """
    CREATE TABLE users (userid NUMBER(10) PRIMARY KEY, email VARCHAR2(100), name VARCHAR2(100 CHAR),
                        seen TIMESTAMP WITH TIME ZONE, status CHAR(3), doc CLOB, shape XMLTYPE,
                        note VARCHAR2(10) INVISIBLE);
    CREATE INDEX users_email ON users (UPPER(email));
    INSERT INTO users (userid, email, name, seen, status) VALUES
        (1, 'ann@example.com', 'Ann', TIMESTAMP '2024-05-01 12:00:00 +02:00', 'ok'),
        (2, 'bob@example.com', 'Bob', NULL, 'no');
    CREATE VIEW users_v AS
        SELECT userid AS user_id, email AS "Email", name AS full_name FROM users
        UNION ALL SELECT 2, 'bob@example.org', 'Bob' FROM DUAL;
    CREATE SYNONYM people FOR users;
    CREATE TABLE "Mixed" ("Id" NUMBER(5) PRIMARY KEY, "EMail" VARCHAR2(20));
    CREATE TABLE keyed (k VARCHAR2(20) PRIMARY KEY, v NUMBER(5));
    INSERT INTO keyed VALUES ('e', 1), ('E2', 2), ('é3', 3), ('x1 ', 4), ('007', 5);
    CREATE TABLE codes (k CHAR(4) PRIMARY KEY, v NUMBER(5));
    INSERT INTO codes VALUES ('ABC', 1), ('ABCD', 2), ('x', 3);
    CREATE TABLE numbered (k NUMBER PRIMARY KEY, v NUMBER(5));
    INSERT INTO numbered SELECT LEVEL, 1 FROM DUAL CONNECT BY LEVEL <= 198;
    INSERT INTO numbered VALUES (0.5, 2);
    CREATE TABLE moments (k DATE PRIMARY KEY, v NUMBER(5));
    INSERT INTO moments VALUES (TIMESTAMP '2024-02-29 00:00:00', 1),
                               (TO_DATE('-0044-03-15', 'SYYYY-MM-DD'), 2);
    CREATE TABLE raws (k RAW(4) PRIMARY KEY, flag BOOLEAN);
    INSERT INTO raws VALUES (HEXTORAW('00ff'), TRUE), (HEXTORAW('01'), FALSE);
    CREATE TABLE wide (k VARCHAR2(10) PRIMARY KEY, nv NVARCHAR2(10), longer VARCHAR2(1000));
    INSERT INTO wide VALUES ('1', 'B', 'x'), ('2', '\U0001f600', 'x'), ('3', '\uff5e', 'x'), ('4', 'é', 'x'),
                            ('5', 'a', 'x'), ('6', 'Z', 'x');
    CREATE TABLE writeonly (id NUMBER(5) PRIMARY KEY, v VARCHAR2(5));
    CREATE TABLE named (k NUMBER(5) PRIMARY KEY, s CHAR(3));
    INSERT INTO named VALUES (1, 'a'), (2, 'zz');
    CREATE TABLE lettered (s VARCHAR2(10) PRIMARY KEY, v NUMBER(5));
    INSERT INTO lettered VALUES ('x', 1);
    CREATE VIEW slow AS
        SELECT userid, email, (SELECT COUNT(*) FROM all_objects a, all_objects b, all_objects c) AS s
        FROM users;
    CREATE TABLE here (k NUMBER(5) PRIMARY KEY, x VARCHAR2(10), y VARCHAR2(10), tag RAW(4), n NUMBER(6, 2),
                       seen DATE, wide NVARCHAR2(10), doc CLOB);
    INSERT INTO here VALUES (1, 'ab', 'c', HEXTORAW('00ff'), 1.50, DATE '2024-02-29', 'é', 'd'),
        (2, 'a', 'bc', HEXTORAW('00ff'), 1.50, DATE '2024-02-29', 'é', 'd'),
        (3, NULL, 'a', NULL, NULL, NULL, NULL, NULL), (4, 'a', NULL, NULL, NULL, NULL, NULL, NULL),
        (5, '', NULL, NULL, NULL, NULL, NULL, NULL), (6, NULL, '', NULL, NULL, NULL, NULL, NULL),
        (7, 'ab', 'c', HEXTORAW('00ff'), 1.50, DATE '2024-02-29', 'é', 'd'),
        (8, 'é', 'c', HEXTORAW('00'), 2.00, NULL, NULL, NULL),
        (9, 'é', 'c', HEXTORAW('00'), 2.00, NULL, NULL, 'other');
    CREATE TABLE there (k NUMBER(5) PRIMARY KEY, x VARCHAR2(10), y VARCHAR2(10), tag RAW(4), n NUMBER(8, 4),
                        seen DATE, wide NVARCHAR2(10), doc CLOB);
    INSERT INTO there SELECT * FROM here;
    UPDATE there SET tag = HEXTORAW('01') WHERE k = 8;
"""
BATCH_KEYS = [str(i) for i in range(1, 199)]  # with two more, a batch of 200: four statements of 50 arms
Connect = Callable[..., TableConnector]


@pytest.fixture(scope="module")
def schema(oracle_server: OracleServer) -> str:
    return oracle_server.schema(SEED)


def _registry(oracle_server: OracleServer, user: str, tmp_path: Path, **members: Any) -> TargetRegistry:
    """db-a and db-b: both the account ``user``."""
    entry = oracle_server.target(user, **members)
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    return load_targets(tmp_path / "targets.yaml")


@pytest.fixture
def targets(oracle_server: OracleServer, schema: str, tmp_path: Path) -> TargetRegistry:
    return _registry(oracle_server, schema, tmp_path)


def _as(oracle_server: OracleServer, schema: str, tmp_path: Path, grants: str) -> TargetRegistry:
    """db-a and db-b as a new account holding ``grants`` (statements with {schema} and {user})."""
    user = oracle_server.schema()
    oracle_server.execute(grants.format(schema=schema, user=user))
    return _registry(oracle_server, user, tmp_path)


async def _planned(targets: TargetRegistry, table: dict[str, Any]) -> Plan:
    """Side a's plan of a pair of ``table`` on both sides."""
    stored = await plan(targets, compiled(table, table))
    assert stored is not None
    return stored["a"]


@pytest.fixture
async def connector(targets: TargetRegistry) -> AsyncIterator[Connect]:
    """``connector(side, **execution)``: a TableConnector reading a plan's side on db-a."""
    made: list[TableConnector] = []

    def make(side: Plan, **options: Any) -> TableConnector:
        target, execution = targets.get("db-a"), ExecutionConfig(**options)
        made.append(TableConnector(target, execution, side, gate=TargetGate.for_target(target)))
        return made[-1]

    yield make
    for table in made:
        await table.aclose()


# --- the session ------------------------------------------------------------------------------


async def test_a_session_s_settings_are_read_back_and_a_write_is_refused(targets: TargetRegistry) -> None:
    session = await ORACLE.connect(targets.get("db-a"), timeout_s=7)
    try:
        settings = await session.query(
            "SELECT parameter, value FROM nls_session_parameters WHERE parameter IN ('NLS_SORT', 'NLS_COMP',"
            " 'NLS_CALENDAR', 'NLS_NUMERIC_CHARACTERS') ORDER BY parameter"
        )
        zone = await session.query("SELECT SESSIONTIMEZONE FROM DUAL")
        with pytest.raises(TargetRefused) as exc:
            await session.query("INSERT INTO keyed VALUES ('w', 0)")
        again = await session.query("SELECT COUNT(*) FROM keyed")  # the refused write ended its transaction
        timeout = session._conn.call_timeout
    finally:
        await session.close()
    assert dict(settings) == {
        "NLS_CALENDAR": "GREGORIAN", "NLS_COMP": "BINARY", "NLS_NUMERIC_CHARACTERS": ".,",
        "NLS_SORT": "BINARY",
    }  # fmt: skip
    assert (zone, again, timeout) == ([("UTC",)], [(5,)], 7000)
    assert str(exc.value) == "ORA-01456"  # no session switch exists: each read is a read-only transaction
    assert (session.charset, session.encrypted) == ("AL32UTF8", False) and oracledb.is_thin_mode()


async def test_a_session_s_socket_sends_keepalive_probes(
    oracle_server: OracleServer, targets: TargetRegistry
) -> None:
    # A scan has no statement timeout: the probes are what ends a flow a firewall dropped silently. The
    # driver's expire_time sets no idle time where the platform names it TCP_KEEPALIVE: it was the
    # system's two hours there. The driver shows no socket, so this also holds that it is still found.
    session = await ORACLE.connect(targets.get("db-a"))
    try:
        sock = engine_module._socket(session._conn)
        assert sock is not None and sock.getpeername() == ("127.0.0.1", oracle_server.port)
        probes = kept_alive(sock)
    finally:
        await session.close()
    assert probes == [60, 10, 6]  # idle seconds, interval, probes: 120 s, on the platform the test runs on


async def test_a_setting_that_reads_back_differently_is_refused(
    targets: TargetRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    overridden = engine_module._SESSION.replace("NLS_SORT = BINARY", "NLS_SORT = BINARY_CI")
    monkeypatch.setattr(engine_module, "_SESSION", overridden)  # as a logon trigger would leave it
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "keyed", "keyColumns": ["k"]}))
    assert str(exc.value) == (
        "side a: the session could not be set (NLS_SORT is BINARY_CI): a logon trigger overrides it"
    )


async def test_a_snapshot_does_not_see_a_later_write_and_loses_a_table_changed_since(
    oracle_server: OracleServer, schema: str, targets: TargetRegistry
) -> None:
    # A row is there before the pass: a table's first row makes its segment, which no earlier moment reads
    # (ORA-08176, a snapshot lost as well).
    seen = "CREATE TABLE IF NOT EXISTS seen (id NUMBER(5) PRIMARY KEY); DELETE FROM seen"
    oracle_server.execute(f"{seen}; INSERT INTO seen VALUES (0)", schema)  # noqa: S608 (constants)
    async with ORACLE.snapshot(targets.get("db-a")) as session:
        before = await session.query("SELECT COUNT(*) FROM seen")
        oracle_server.execute("INSERT INTO seen VALUES (1)", schema)
        inside = await session.query("SELECT COUNT(*) FROM seen")
        oracle_server.execute("CREATE TABLE IF NOT EXISTS later (id NUMBER(5) PRIMARY KEY)", schema)
        with pytest.raises(SnapshotLost) as exc:
            await session.query("SELECT COUNT(*) FROM later")
    oracle_server.execute("DROP TABLE later PURGE", schema)
    assert (before, inside) == ([(1,)], [(1,)])
    assert oracle_server.execute("SELECT COUNT(*) FROM seen", schema) == [(2,)]
    assert str(exc.value) == "ORA-01466"  # the table's definition is later than the pass's moment


# --- the column plan --------------------------------------------------------------------------


async def test_a_pair_stores_each_side_s_engine_classes_and_declared_types(
    targets: TargetRegistry, schema: str
) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["UserId"], "columns": ["EMAIL", "name"], "where": "Status = 'ok'"},
        {
            "name": "users_v",
            "keyColumns": ["user_id"],
            "columns": ["Email", {"name": "full_name", "as": "name"}],
        },
    )
    stored = await plan(targets, spec)
    assert stored is not None
    a, b = stored["a"], stored["b"]
    assert (a["schema"], a["name"], b["name"]) == (schema, "USERS", "USERS_V")  # as the catalogue folds
    assert (a["keys"], a["columns"], a["where"]) == (
        ["USERID"],
        {"email": "EMAIL", "name": "NAME"},
        "\"STATUS\" = 'ok'",
    )
    assert (b["keys"], b["columns"]) == (["USER_ID"], {"email": "Email", "name": "FULL_NAME"})  # as spelled
    assert {name: a[name] for name in ("engine", "server", "consistency", "tls")} == {
        "engine": "oracle", "server": "23.26", "consistency": "snapshot", "tls": False,
    }  # fmt: skip
    assert a["classes"] == {"USERID": "integer", "EMAIL": "text", "NAME": "text", "STATUS": "char"}
    assert a["declared"] == {
        "USERID": "NUMBER(10)", "EMAIL": "VARCHAR2(100)", "NAME": "VARCHAR2(100 CHAR)", "STATUS": "CHAR(3)",
    }  # fmt: skip
    # Both sides declare email alike, and the engine hashes it; name is declared in two ways there.
    assert (a["digest"], b["digest"], stored["rendering"]) == (["name"], ["name"], 1)


async def test_the_engine_describes_a_relation_as_the_catalogue_holds_it(
    targets: TargetRegistry, schema: str
) -> None:
    session = await ORACLE.connect(targets.get("db-a"))
    try:
        mixed = await ORACLE.describe(session, None, "Mixed")  # as spelled
        users = await ORACLE.describe(session, schema.lower(), "users")  # upper-cased
        people = await ORACLE.describe(session, None, "people")  # a synonym: the real owner and name
        assert await ORACLE.describe(session, None, "mixed") is None  # MIXED is no relation
        assert await ORACLE.describe(session, "NOWHERE", "users") is None
    finally:
        await session.close()
    assert mixed is not None and users is not None and people == users
    assert mixed.name == "Mixed" and [c.name for c in mixed.columns] == ["Id", "EMail"]
    assert users.facts == {
        "kind": "table", "key": ["USERID"], "server": "23.26", "consistency": "snapshot", "tls": False,
        "charset": "AL32UTF8", "national": "AL16UTF16",
    }  # fmt: skip
    # The invisible column is one the role reads by name; the index's own column is not listed.
    assert [(c.name, c.declared, c.cls, c.likeable, c.keyable) for c in users.columns] == [
        ("USERID", "NUMBER(10)", "integer", False, True),
        ("EMAIL", "VARCHAR2(100)", "text", True, True),
        ("NAME", "VARCHAR2(100 CHAR)", "text", True, True),
        ("SEEN", "TIMESTAMP(6) WITH TIME ZONE", "instant", False, False),
        ("STATUS", "CHAR(3)", "char", True, True),
        ("DOC", "CLOB", "text", True, False),
        ("SHAPE", "XMLTYPE", None, False, False),
        ("NOTE", "VARCHAR2(10)", "text", True, True),
    ]


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"name": "nope", "keyColumns": ["id"]}, "side a: table or view nope not found or not visible"),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email", "shape"]},
            "side a: column 'SHAPE' (XMLTYPE) cannot be compared: compare a view that casts it",
        ),
        (
            {"name": "users", "keyColumns": ["userid"]},  # a refused type among the discovered columns
            "side a: column 'SHAPE' (XMLTYPE) cannot be compared: compare a view that casts it",
        ),
        (
            {"name": "users", "keyColumns": ["seen"], "columns": ["email"]},
            "side a: key column 'SEEN' (TIMESTAMP(6) WITH TIME ZONE) cannot be a key:"
            " compare a view that casts it",
        ),
        (
            {"name": "wide", "keyColumns": ["nv"], "columns": ["longer"]},
            "side a: key column 'NV' (NVARCHAR2(10)) cannot be a key: compare a view that casts it",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "nope = 1"},
            None,  # names the schema: asserted below
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "userid > 'abc'"},
            "side a: the filter compares column 'USERID' (NUMBER(10)) with a literal that is not a number",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "seen < 'soon'"},
            "side a: the filter compares column 'SEEN' (TIMESTAMP(6) WITH TIME ZONE) with a literal that is"
            " not a timestamp",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "userid = TRUE"},
            "side a: the filter tests column 'USERID' (NUMBER(10)) as a boolean: compare it with 1 or 0",
        ),
        (
            {
                "name": "users",
                "keyColumns": ["userid"],
                "columns": ["email"],
                "where": f"status = '{'x' * 2001}'",
            },
            "side a: the filter compares column 'STATUS' (CHAR(3)) with a literal over 2000 bytes",
        ),
        (
            {"name": "raws", "keyColumns": ["k"], "where": "flag = 1"},
            "side a: the filter compares column 'FLAG' (BOOLEAN) with a literal that is not TRUE or FALSE",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "doc = 'x'"},
            "side a: the database refuses the filter or key map: ORA-22848",  # a CLOB is not compared
        ),
    ],
    ids=[
        *("P12", "P7", "P7 discovered", "P6", "P6 national", "P12 column", "P11 number", "P11 timestamp"),
        *("P11 TRUE", "P11 CHAR", "P11 boolean", "P13"),
    ],
)
async def test_a_plan_oracle_cannot_read_as_written_is_refused(
    targets: TargetRegistry, schema: str, table: dict[str, Any], message: str | None
) -> None:
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(table, table))
    assert str(exc.value) == (message or f"side a: column 'nope' is not in {schema}.USERS")


async def test_a_synonym_over_a_database_link_is_not_read(
    oracle_server: OracleServer, schema: str, targets: TargetRegistry
) -> None:
    oracle_server.execute(f"GRANT CREATE DATABASE LINK TO {schema}")
    oracle_server.execute(
        f'CREATE DATABASE LINK IF NOT EXISTS far CONNECT TO {schema} IDENTIFIED BY "test"'
        " USING '//localhost:1521/FREEPDB1'; CREATE OR REPLACE SYNONYM remote FOR users@far",
        schema,
    )
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "remote", "keyColumns": ["userid"]}))
    assert str(exc.value) == (
        "side a: table or view remote not found or not visible: a synonym over a database link is not read"
    )


async def test_a_view_the_database_cannot_read_is_refused_by_its_code(
    oracle_server: OracleServer, tmp_path: Path
) -> None:
    # A view whose table was dropped is INVALID, a routine state after DDL. Its ORA-04063 once left the
    # plan as it came: a traceback from `ds run` and `ds target check`, a 500 from run creation.
    broken = "CREATE TABLE base_t (id NUMBER(5) PRIMARY KEY, v VARCHAR2(5));"
    broken += " CREATE VIEW broken_v AS SELECT id, v FROM base_t; DROP TABLE base_t PURGE"
    targets = _registry(oracle_server, oracle_server.schema(broken), tmp_path)
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "broken_v", "keyColumns": ["id"]}))
    assert str(exc.value) == "side a: table or view broken_v cannot be described: ORA-04063"
    with pytest.raises(ValueError) as exc:
        await preflight(targets.get("db-a"), "broken_v")
    assert str(exc.value) == "target db-a: table or view broken_v cannot be described: ORA-04063"


async def test_an_account_reads_what_it_is_granted_read_on(
    oracle_server: OracleServer, schema: str, tmp_path: Path
) -> None:
    grants = "GRANT READ ON {schema}.keyed TO {user}; GRANT INSERT ON {schema}.writeonly TO {user}"
    targets = _as(oracle_server, schema, tmp_path, grants)
    keyed = {"schema": schema, "name": "keyed", "keyColumns": ["k"]}
    stored = await plan(targets, compiled(keyed, keyed))
    assert stored is not None and stored["a"]["columns"] == {"v": "V"}
    session = await ORACLE.connect(targets.get("db-a"))
    try:
        with pytest.raises(TableError) as locked:  # READ grants no lock on a row
            await session.query(f'SELECT k FROM "{schema}"."KEYED" FOR UPDATE')  # noqa: S608
    finally:
        await session.close()
    assert (type(locked.value).__name__, str(locked.value)) == ("TargetDenied", "ORA-41900")
    with pytest.raises(ValueError) as exc:  # the catalogue lists a table the account may only write
        await plan(targets, compiled({"schema": schema, "name": "writeonly", "keyColumns": ["id"]}))
    assert str(exc.value) == f"side a: the role may not read {schema}.WRITEONLY: grant it READ"
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"schema": schema, "name": "codes", "keyColumns": ["k"]}))
    assert str(exc.value) == f"side a: table or view {schema}.codes not found or not visible"
    with pytest.raises(ValueError) as exc:  # without its schema it is looked for in the account's own
        await plan(targets, compiled({"name": "keyed", "keyColumns": ["k"]}))
    assert str(exc.value) == "side a: table or view keyed not found or not visible"


async def test_a_refused_connect_names_the_endpoint_and_glosses_the_code(
    oracle_server: OracleServer, schema: str, tmp_path: Path
) -> None:
    (tmp_path / "password").write_text("wrong-s3cret")
    (tmp_path / "ca.pem").write_bytes(make_ca("some CA")[0].public_bytes(serialization.Encoding.PEM))
    port = oracle_server.port
    table = compiled({"name": "keyed", "keyColumns": ["k"]})
    for members, endpoint, why in (
        (
            {"password_ref": "file:password"},
            f"127.0.0.1:{port}/FREEPDB1",
            "ORA-01017: the credential was refused",
        ),
        (
            {"service": "NOPE"},
            f"127.0.0.1:{port}/NOPE",
            "DPY-6001: the listener does not know the service",
        ),
        # TLS, the default, declared against the plain listener, which resets the handshake's first bytes.
        (
            {"allow_unencrypted": False, "tls_verify_name": False, "tls_ca_ref": "file:ca.pem"},
            f"127.0.0.1:{port}/FREEPDB1",
            "ConnectionResetError: TLS: the handshake failed",
        ),
        (
            {"allow_unencrypted": False, "tls_server_dn": "CN=crm-db,O=Bank", "tls_ca_ref": "file:ca.pem"},
            f"127.0.0.1:{port}/FREEPDB1",
            "ConnectionResetError: TLS: the handshake failed",
        ),
    ):
        targets = _registry(oracle_server, schema, tmp_path, **members)
        with pytest.raises(ValueError) as exc:
            await plan(targets, table)
        assert (
            str(exc.value) == f"side a: cannot connect to target db-a (oracle {endpoint} as {schema}): {why}"
        )
    side = await _planned(_registry(oracle_server, schema, tmp_path), {"name": "keyed", "keyColumns": ["k"]})
    refused = _registry(oracle_server, schema, tmp_path, password_ref="file:password")  # noqa: S106
    target, execution = refused.get("db-a"), ExecutionConfig()
    table = TableConnector(target, execution, side, gate=TargetGate.for_target(target))
    read = await table.fetch("e", USERS_SIDE)
    await table.aclose()
    assert (read.error_class, read.error_detail) == ("auth_error", "ORA-01017")  # the run pauses


# --- per-key reads ------------------------------------------------------------------------


def _row(obs: Observation) -> Any:
    return None if obs.body is None else json.loads(obs.body)


async def test_a_key_reads_its_row_in_the_canonical_rendering(
    targets: TargetRegistry, schema: str, connector: Connect
) -> None:
    table = {"name": "users", "keyColumns": ["userid"], "columns": ["email", "seen", "status", "note"]}
    users = connector(await _planned(targets, table))
    found, missing, letters = [await users.fetch(key, USERS_SIDE) for key in ("1", "9", "x")]
    assert (found.http_status, found.request_url) == (200, f"oracle://db-a/{schema}.USERS")
    assert _row(found) == {
        "email": "ann@example.com", "seen": "2024-05-01T10:00:00+00:00", "status": "ok", "note": None,
    }  # fmt: skip
    assert (missing.http_status, missing.body) == (404, None)
    assert (letters.http_status, letters.body) == (404, None)  # letters are no number: on no row


async def test_a_view_repeating_a_key_is_ambiguous(targets: TargetRegistry, connector: Connect) -> None:
    side = await _planned(targets, {"name": "users_v", "keyColumns": ["user_id"], "columns": ["Email"]})
    twice = await connector(side).fetch("2", USERS_SIDE)
    assert (twice.http_status, twice.error_class) == (200, "identity_ambiguous")


async def test_a_key_is_its_text_exactly(targets: TargetRegistry, connector: Connect) -> None:
    keyed = connector(await _planned(targets, {"name": "keyed", "keyColumns": ["k"]}))
    keys = ["e", "E", "E2", "é3", "x1", "x1 ", "007", "7", ""]
    await keyed.prefetch(keys, USERS_SIDE)
    assert {key: _row(await keyed.fetch(key, USERS_SIDE)) for key in keys} == {
        "e": {"v": 1}, "E": None, "E2": {"v": 2}, "é3": {"v": 3},
        "x1": None, "x1 ": {"v": 4},  # a trailing space is part of the key
        "007": {"v": 5}, "7": None,
        "": None,  # the empty string is NULL there: no row's key
    }  # fmt: skip
    # A CHAR key is its text without the padding; a longer value is not cut to the column's length.
    codes = connector(await _planned(targets, {"name": "codes", "keyColumns": ["k"]}))
    keys = ["ABC", "ABCD", "ABCDE", "ABC ", "AB", "x"]
    await codes.prefetch(keys, USERS_SIDE)
    assert {key: _row(await codes.fetch(key, USERS_SIDE)) for key in keys} == {
        "ABC": {"v": 1}, "ABCD": {"v": 2}, "ABCDE": None, "ABC ": None, "AB": None, "x": {"v": 3},
    }  # fmt: skip
    # An unconstrained NUMBER's key text is a whole number's; any other row has none.
    numbered = connector(await _planned(targets, {"name": "numbered", "keyColumns": ["k"]}))
    huge = "1e999999999999999999999999999999"  # a number's form with an exponent no decimal holds: on no row
    found = {key: _row(await numbered.fetch(key, USERS_SIDE)) for key in ("7", "7.0", "0.5", ".5", huge)}
    assert found == {"7": {"v": 1}, "7.0": None, "0.5": None, ".5": None, huge: None}
    moments = connector(await _planned(targets, {"name": "moments", "keyColumns": ["k"]}))
    found = {key: _row(await moments.fetch(key, USERS_SIDE)) for key in ("2024-02-29 00:00:00", "soon")}
    assert found == {"2024-02-29 00:00:00": {"v": 1}, "soon": None}
    # A RAW key is \x and lower-case hex, as PostgreSQL writes a bytea.
    raws = connector(await _planned(targets, {"name": "raws", "keyColumns": ["k"], "where": "flag"}))
    read = {key: await raws.fetch(key, USERS_SIDE) for key in ("\\x00ff", "\\x00FF", "\\x01", "00ff")}
    assert {key: _row(obs) for key, obs in read.items()} == {
        "\\x00ff": {"flag": True}, "\\x00FF": None, "\\x01": {"flag": False}, "00ff": None,
    }  # fmt: skip
    assert read["\\x01"].headers == {"selected": "false"}  # outside the filter, a bare BOOLEAN column


async def test_a_column_named_s_is_read_per_key(targets: TargetRegistry, connector: Connect) -> None:
    # Each arm puts its selected row first by the flag's select position: ordered by an alias s, the server
    # refused the statement beside a column of that name (ORA-00960) and every key read INCONCLUSIVE.
    named = connector(await _planned(targets, {"name": "named", "keyColumns": ["k"], "where": "s <> 'zz'"}))
    await named.prefetch(["1", "2", "3"], USERS_SIDE)
    read = {key: await named.fetch(key, USERS_SIDE) for key in ("1", "2", "3")}
    assert {key: (obs.http_status, obs.error_class, _row(obs)) for key, obs in read.items()} == {
        "1": (200, None, {"s": "a"}), "2": (200, None, {"s": "zz"}), "3": (404, None, None),
    }  # fmt: skip
    assert read["2"].headers == {"selected": "false"}  # outside the filter
    lettered = connector(await _planned(targets, {"name": "lettered", "keyColumns": ["s"]}))
    assert _row(await lettered.fetch("x", USERS_SIDE)) == {"v": 1}


async def test_a_statement_past_its_timeout_is_a_timeout(targets: TargetRegistry, connector: Connect) -> None:
    side = await _planned(targets, {"name": "slow", "keyColumns": ["userid"], "columns": ["email", "s"]})
    slow = connector(side, total_timeout_s=0.5)
    obs = await slow.fetch("1", USERS_SIDE)
    assert (obs.error_class, obs.error_detail, obs.http_status) == ("timeout", "DPY-4024", None)
    session = await ORACLE.connect(targets.get("db-a"), timeout_s=0.3)
    with pytest.raises(StatementTimeout):
        await session.query("SELECT COUNT(*) FROM all_objects a, all_objects b, all_objects c")
    await session.close()


@pytest.mark.parametrize(
    ("relation", "unfit", "served"),
    [
        # ORA-01426; 1e0 is the number 1, whose key text is 1; no decimal holds the last one's exponent
        ("numbered", ["9" * 200], {"1e0": 404, "1e999999999999999999999999999999": 404}),
        ("moments", ["2024-02-31 00:00:00"], {"2024-02-29 00:00:00": 200, "soon": 404}),  # ORA-01839
        ("keyed", ["\ud800"], {"e": 200}),  # a lone surrogate: no bind takes it
    ],
    ids=["200 digits", "an impossible date", "a lone surrogate"],
)
async def test_a_key_its_column_cannot_hold_is_rejected_alone_in_its_batch(
    targets: TargetRegistry, connector: Connect, relation: str, unfit: list[str], served: dict[str, int]
) -> None:
    table = connector(await _planned(targets, {"name": relation, "keyColumns": ["k"]}))
    others = BATCH_KEYS[: 200 - len(unfit) - len(served)]
    await table.prefetch([*others[:100], *unfit, *served, *others[100:]], USERS_SIDE)
    expected = 200 if relation == "numbered" else 404
    assert {(await table.fetch(key, USERS_SIDE)).http_status for key in others} == {expected}
    assert {key: (await table.fetch(key, USERS_SIDE)).http_status for key in served} == served
    for key in unfit:
        with pytest.raises(KeyRejectedError):
            await table.fetch(key, USERS_SIDE)


async def test_a_group_is_read_in_statements_of_fifty_keys_at_most(
    targets: TargetRegistry, connector: Connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[int, int]] = []
    real = compose.rows

    def counted(dialect: Any, side: Plan, asked: Any) -> tuple[str, Any]:
        statement, binds = real(dialect, side, asked)
        sent.append((len(asked), statement.count("FETCH FIRST 2 ROWS ONLY")))
        return statement, binds

    monkeypatch.setattr(compose, "rows", counted)
    numbered = connector(await _planned(targets, {"name": "numbered", "keyColumns": ["k"]}))
    keys = [*BATCH_KEYS[:120], "x", "500"]  # letters have no arm; 500 is on no row
    await numbered.prefetch(keys, USERS_SIDE)
    read = {key: (await numbered.fetch(key, USERS_SIDE)).http_status for key in keys}
    # 50, 50 and 22 keys: the last statement's 21 arms are padded to 32, so a plan has seven texts.
    assert sent == [(50, 50), (50, 50), (22, 32)]
    assert read == dict.fromkeys(BATCH_KEYS[:120], 200) | {"x": 404, "500": 404}


async def test_a_row_with_large_values_stays_in_its_database(
    oracle_server: OracleServer, schema: str, targets: TargetRegistry, connector: Connect
) -> None:
    oracle_server.execute(
        "CREATE TABLE IF NOT EXISTS docs (id NUMBER(5) PRIMARY KEY, doc CLOB, note VARCHAR2(10));"
        " DELETE FROM docs; INSERT INTO docs VALUES (1, 'small', 'a');"
        " INSERT INTO docs VALUES (3, NULL, 'c'); INSERT INTO docs VALUES (2, :doc, 'b')",
        schema,
        doc="x" * (20 * 1024 * 1024),
    )
    side = await _planned(targets, {"name": "docs", "keyColumns": ["id"]})
    docs = connector(side)
    tracemalloc.start()
    await docs.prefetch(["1", "2", "3"], USERS_SIDE)
    read = [await docs.fetch(key, USERS_SIDE) for key in ("1", "2", "3")]
    async with ORACLE.snapshot(targets.get("db-a")) as session:
        scanned = [row async for batch in await ORACLE.scan(session, side, None) for row in batch]
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert [(o.http_status, o.error_class) for o in read] == [
        (200, None),
        (200, "resource_limit"),
        (200, None),
    ]
    assert (_row(read[0]), _row(read[2])) == ({"doc": "small", "note": "a"}, {"doc": None, "note": "c"})
    assert [(key, len(digest)) for key, digest in scanned] == [("1", 32), ("2", 15), ("3", 32)]
    assert scanned[1][1] == b"resource_limit:" and peak < 8 * 1024 * 1024  # the 20 MiB never came
    # A row of this plan may hold 16 MiB: the worker reads its keys four at a time (e2e/test_engines).
    assert docs.large and not connector(await _planned(targets, {"name": "keyed", "keyColumns": ["k"]})).large
    # A table with a LOB column is fetched four rows at a time; one without, by its declared width.
    assert (
        engine_module._fetch(side, ["ID", "DOC", "NOTE"]) == 3
        and engine_module._fetch(side, ["ID"]) == 10_000
    )


async def test_a_lookup_uses_the_key_s_index(
    oracle_server: OracleServer, schema: str, targets: TargetRegistry
) -> None:
    for relation, key in (
        ("keyed", "e"),
        ("numbered", "7"),
        ("codes", "ABC"),  # through the CHAR(2000) cast
        ("moments", "2024-02-29 00:00:00"),  # through TO_DATE
        ("raws", "\\x00ff"),  # through HEXTORAW
    ):
        side = await _planned(targets, {"name": relation, "keyColumns": ["k"]})
        statement, binds = compose.rows(engine_module.PRIMITIVES, side, {key: [[key]]})
        explained = oracle_server.execute(
            f"EXPLAIN PLAN FOR {statement}; SELECT operation, options FROM plan_table"  # noqa: S608
            " WHERE object_name LIKE 'SYS_C%'",
            schema,
            **binds,
        )
        assert explained == [("INDEX", "UNIQUE SCAN")], relation


# --- what a scan is read in ------------------------------------------------------------


async def test_a_scan_orders_keys_by_code_point(targets: TargetRegistry) -> None:
    side = await _planned(targets, {"name": "keyed", "keyColumns": ["k"]})
    async with ORACLE.snapshot(targets.get("db-a")) as session:
        keys = [row[0] async for batch in await ORACLE.scan(session, side, None) for row in batch]
        after = [row[0] async for batch in await ORACLE.scan(session, side, [b"e"]) for row in batch]
        census = [row async for batch in ORACLE.census(session, side) for row in batch]
        nulls = await ORACLE.null_keys(session, side)
    assert keys == sorted(keys) == ["007", "E2", "e", "x1 ", "é3"]
    assert after == ["x1 ", "é3"] and census == [(key,) for key in keys] and nulls == 0
    # A DATE before year 1 would print as its AD twin: it has no key text, and its row is skipped.
    side = await _planned(targets, {"name": "moments", "keyColumns": ["k"]})
    async with ORACLE.snapshot(targets.get("db-a")) as session:
        census = [row async for batch in ORACLE.census(session, side) for row in batch]
        nulls = await ORACLE.null_keys(session, side)
    assert (census, nulls) == ([("2024-02-29 00:00:00",)], 1)


async def test_outside_al32utf8_a_text_key_is_ordered_by_its_utf8_bytes(targets: TargetRegistry) -> None:
    # The image's database is AL32UTF8; its national character set is not, so an NVARCHAR2 column shows
    # what another character set does: its own binary order puts the emoji before U+FF5E.
    wide = await _planned(targets, {"name": "wide", "keyColumns": ["k"], "columns": ["nv"]})
    side = wide | {"keys": ["NV"], "columns": {"k": "K"}, "digest": ["k"]}
    side["classes"] = wide["classes"] | {"NV": "text"}
    async with ORACLE.snapshot(targets.get("db-a")) as session:
        own = await session.query('SELECT t."NV" FROM "WIDE" t ORDER BY t."NV"')
        session.dialect = engine_module._Primitives(utf8=False)
        keys = [row[0] async for batch in ORACLE.census(session, side) for row in batch]
        after = [row[0] async for batch in await ORACLE.scan(session, side, ["é".encode()]) for row in batch]
        rows = await ORACLE.rows(session, side, {"\U0001f600": [["\U0001f600"]], "q": [["q"]]})
    assert [key for (key,) in own] == ["B", "Z", "a", "é", "\U0001f600", "\uff5e"]
    assert keys == sorted(keys) == ["B", "Z", "a", "é", "\uff5e", "\U0001f600"]  # the code points' order
    assert after == ["\uff5e", "\U0001f600"] and rows == [(0, '{"k": "2"}', True)]


async def test_outside_al32utf8_a_long_text_key_is_refused(
    targets: TargetRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = await _planned(targets, {"name": "wide", "keyColumns": ["k"], "columns": ["nv"]})
    longer = await _planned(targets, {"name": "wide", "keyColumns": ["longer"], "columns": ["nv"]})
    session = await ORACLE.connect(targets.get("db-a"))
    try:
        session.charset, session.dialect = "WE8MSWIN1252", engine_module._Primitives(utf8=False)
        await ORACLE.probe(session, short)  # the role may execute the function, and the statement parses
        with pytest.raises(Refusal) as exc:
            await ORACLE.probe(session, longer)
        monkeypatch.setattr(engine_module, "_ORDERS", "SELECT nope.f('a') FROM DUAL")
        with pytest.raises(Refusal) as denied:
            await ORACLE.probe(session, short)
    finally:
        await session.close()
    assert str(exc.value) == (
        "key column 'LONGER' is VARCHAR2(1000) in a WE8MSWIN1252 database: a scan orders text keys of up to"
        " 500 bytes there (compare a view that shortens it)"
    )
    assert str(denied.value) == (
        "the role may not execute UTL_I18N, which orders text keys in a WE8MSWIN1252 database:"
        " grant it EXECUTE"
    )


async def test_a_session_killed_while_the_scan_waits_is_unreachable(
    oracle_server: OracleServer, targets: TargetRegistry
) -> None:
    side = await _planned(targets, {"name": "slow", "keyColumns": ["userid"], "columns": ["email", "s"]})
    async with ORACLE.snapshot(targets.get("db-a")) as session:
        sid = (await session.query("SELECT SYS_CONTEXT('USERENV', 'SID') FROM DUAL"))[0][0]
        batches = await ORACLE.scan(session, side, None)  # the statement is sent: the server counts
        await asyncio.sleep(0.5)
        serial = oracle_server.execute("SELECT serial# FROM v$session WHERE sid = :sid", sid=int(sid))[0][0]
        with pytest.raises(oracledb.DatabaseError):  # the kill is answered as a closed connection
            oracle_server.execute(f"ALTER SYSTEM KILL SESSION '{int(sid)},{int(serial)}' IMMEDIATE")
        with pytest.raises(TableError) as exc:
            await anext(batches)
    assert (type(exc.value).__name__, str(exc.value)) == ("TargetUnreachable", "ORA-00028")  # it resumes


async def test_closing_a_session_cancels_its_statement(
    oracle_server: OracleServer, targets: TargetRegistry
) -> None:
    side = await _planned(targets, {"name": "slow", "keyColumns": ["userid"], "columns": ["email", "s"]})
    session = await ORACLE.connect(targets.get("db-a"))
    sid = int((await session.query("SELECT SYS_CONTEXT('USERENV', 'SID') FROM DUAL"))[0][0])
    serial = oracle_server.execute("SELECT serial# FROM v$session WHERE sid = :sid", sid=sid)[0][0]
    await ORACLE.scan(session, side, None)
    await asyncio.sleep(0.5)
    running = "SELECT COUNT(*) FROM v$session WHERE sid = :sid AND serial# = :serial AND status = 'ACTIVE'"
    assert oracle_server.execute(running, sid=sid, serial=serial) == [(1,)]
    await asyncio.wait_for(session.close(), 3)
    await asyncio.sleep(0.3)
    assert oracle_server.execute(running, sid=sid, serial=serial) == [(0,)]


# --- rows hashed in the engine -----------------------------------------------------------------


async def _digests(targets: TargetRegistry, target: str, side: Plan) -> dict[str, bytes]:
    async with ORACLE.snapshot(targets.get(target)) as session:
        return {row[0]: row[1] async for batch in await ORACLE.scan(session, side, None) for row in batch}


async def test_the_engine_s_row_hash_tells_boundaries_and_nulls_apart(targets: TargetRegistry) -> None:
    side = await _planned(targets, {"name": "here", "keyColumns": ["k"], "columns": ["x", "y"]})
    assert side["digest"] == []  # the engine hashes both columns; it sorts keys and 32 bytes
    hashed = await _digests(targets, "db-a", side)
    # ('ab', 'c') is not ('a', 'bc') and (NULL, 'a') not ('a', NULL); row 7 is row 1. The empty string is
    # NULL there, so rows 5 and 6 are one row twice.
    assert len({hashed[key] for key in "12345"}) == 5 and (hashed["7"], hashed["6"]) == (
        hashed["1"],
        hashed["5"],
    )

    def sha256(data: bytes) -> bytes:
        return hashlib.sha256(data).digest()

    # STANDARD_HASH of each column, joined as hexadecimal digits and hashed again; one column's as it is.
    assert hashed["1"] == sha256((sha256(b"ab").hex() + sha256(b"c").hex()).upper().encode())
    one = await _planned(targets, {"name": "here", "keyColumns": ["k"], "columns": ["x"]})
    assert (await _digests(targets, "db-a", one))["1"] == sha256(sha256(b"ab"))
    # The same rows hashed by the worker, and by both, are equal and differ in the same places.
    for worker in (["x", "y"], ["y"]):
        digests = await _digests(targets, "db-a", side | {"digest": worker})
        assert [hashed[key] == hashed[other] for key in hashed for other in hashed] == [
            digests[key] == digests[other] for key in hashed for other in hashed
        ]


async def test_two_sides_of_one_engine_hash_alike_what_they_declare_alike(targets: TargetRegistry) -> None:
    spec = compiled(
        {"name": "here", "keyColumns": ["k"]}, {"name": "there", "keyColumns": ["k"]},
        types={"b": {"y": {"as": "string"}}},
    )  # fmt: skip
    stored = await plan(targets, spec)
    assert stored is not None
    # The worker hashes the column declared in two ways, the mapped one and the large one; the engine
    # hashes the others, bytes and a text in the national character set among them.
    assert stored["a"]["digest"] == stored["b"]["digest"] == ["doc", "n", "y"]
    here = await _digests(targets, "db-a", stored["a"])
    there = await _digests(targets, "db-b", stored["b"])
    assert {key for key in here if here[key] != there[key]} == {"8"} and len(here) == 9
    # Every column hashed by the worker gives the same answer.
    worker = sorted(stored["a"]["columns"])
    here = await _digests(targets, "db-a", stored["a"] | {"digest": worker})
    there = await _digests(targets, "db-b", stored["b"] | {"digest": worker})
    assert {key for key in here if here[key] != there[key]} == {"8"}


# --- a filter and a key map behave as PostgreSQL's -------------------------------------------


async def test_a_char_filter_and_prefix_pad_select_as_on_postgresql(
    oracle_server: OracleServer, schema: str, pg_dsn: str, tmp_path: Path
) -> None:
    rows = "('1', 'ab'), ('2', 'ab '), ('3', 'abc'), ('4', 'AB'), ('12\n', 'ab'), ('x', 'ab'), ('', 'ab')"
    table = "CREATE TABLE IF NOT EXISTS pads (k VARCHAR2(10), st CHAR(3))"  # its '' is a NULL key
    oracle_server.execute(f"{table}; DELETE FROM pads; INSERT INTO pads VALUES {rows}", schema)  # noqa: S608
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE pads (k text PRIMARY KEY, st char(3)); INSERT INTO pads VALUES {rows}")  # noqa: S608
    (tmp_path / "dsn").write_text(pg_dsn)
    targets = {"db-a": oracle_server.target(schema), "db-b": {"dsn_ref": "file:dsn"}}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": targets}))
    registry = load_targets(tmp_path / "targets.yaml")
    key_map = {"kind": "prefix_pad", "prefix": "P-", "width": 3, "sideType": "string"}
    for where, keys in (
        ("st = 'ab'", ["P-001", "P-002"]),  # ab and 'ab ' are one CHAR(3) value: a plain bind finds neither
        ("st IN ('abc', 'AB')", ["P-003", "P-004"]),
        ("st BETWEEN 'ab' AND 'abc' AND st < 'abc'", ["P-001", "P-002"]),
        ("st LIKE 'ab%' AND st NOT LIKE 'abc'", ["P-001", "P-002"]),
    ):
        pads = {"name": "pads", "keyColumns": [{"name": "k", "keyMap": key_map}], "where": where}
        stored = await plan(registry, compiled(pads, pads))
        assert stored is not None
        found = {}
        for name, engine in (("a", ORACLE), ("b", ENGINES["postgresql"])):
            async with engine.snapshot(registry.get(f"db-{name}")) as session:
                found[name] = [
                    row[0] async for batch in engine.census(session, stored[name]) for row in batch
                ]
                found[name] = (found[name], await engine.null_keys(session, stored[name]))
        # 12 with a newline, x and '' (a NULL key there) have no canonical key: skipped on both, where the
        # filter selects them.
        assert found["a"] == found["b"] == (keys, 0 if " IN " in where else 3), where


async def test_a_date_key_against_a_date_that_holds_a_time_is_refused(
    oracle_server: OracleServer, schema: str, pg_dsn: str, tmp_path: Path
) -> None:
    # A date key moved to Oracle is a DATE, which holds a time: its key text is never the date's. Planned,
    # every row of two identical tables was reported missing on both sides.
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE moments (k date PRIMARY KEY, v integer)")
        conn.execute("CREATE VIEW moments_v AS SELECT k::text AS k, v FROM moments")
    (tmp_path / "dsn").write_text(pg_dsn)
    targets = {"db-a": {"dsn_ref": "file:dsn"}, "db-b": oracle_server.target(schema)}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": targets}))
    registry = load_targets(tmp_path / "targets.yaml")
    moments = {"name": "moments", "keyColumns": ["k"]}
    with pytest.raises(ValueError) as exc:
        await plan(registry, compiled(moments, moments))
    assert str(exc.value) == (
        "key column 'k' is date on side a and DATE on side b: the key text of a date is never that of a"
        " timestamp (compare a view that casts it)"
    )
    assert await plan(registry, compiled(moments | {"name": "moments_v"}, moments)) is not None  # a text key


# --- what a table pre-check reads ----------------------------------------

# One row a class holds at its edge: a CHAR padded and blank, an emoji in an NVARCHAR2 (LENGTH counts it
# twice), a CLOB (COUNT of one raises ORA-22849), a BOOLEAN, RAW bytes, a date before year 1, an instant
# stored at another offset, and an unconstrained NUMBER whose sum Oracle rounds. ``loose`` repeats keys and
# holds one without a part: '' is NULL there.
FIGURES = """
    CREATE TABLE figures (
        id NUMBER(10) PRIMARY KEY, code CHAR(4), email VARCHAR2(40 CHAR), nm NVARCHAR2(20), note CLOB,
        amount NUMBER(12, 2), big NUMBER, opened DATE, seen TIMESTAMP(6) WITH TIME ZONE, flag BOOLEAN,
        tag RAW(8), bytes_text VARCHAR2(10));
    INSERT INTO figures VALUES (1, 'ab', 'A@x.com', N'x\U0001f600', 'hello', 1.50, 1E30, DATE '2024-05-01',
                                TIMESTAMP '2024-05-01 12:00:00 +02:00', TRUE, HEXTORAW('00ff'), 'x');
    INSERT INTO figures VALUES (2, '    ', 'a@x.com', NULL, NULL, -0.50, 1E-20,
                                TO_DATE('-0044-03-15', 'SYYYY-MM-DD'), NULL, FALSE, HEXTORAW('010203'), NULL);
    INSERT INTO figures VALUES (3, 'abcd', '', NULL, NULL, 2.00, 5, NULL, NULL, NULL, NULL, NULL);
    INSERT INTO figures VALUES (4, NULL, 'a ', NULL, NULL, NULL, 1.5, NULL, NULL, TRUE, NULL, NULL);
    CREATE TABLE loose (k VARCHAR2(10), part NUMBER(5), v VARCHAR2(10));
    INSERT INTO loose VALUES ('X1', 1, 'a'); INSERT INTO loose VALUES ('x1', 1, 'b');
    INSERT INTO loose VALUES ('X1', 1, 'c'); INSERT INTO loose VALUES ('X1', 1, 'd');
    INSERT INTO loose VALUES ('Y', 2, 'e'); INSERT INTO loose VALUES ('Y', 2, 'f');
    INSERT INTO loose VALUES ('', 3, 'g'); INSERT INTO loose VALUES ('Z', NULL, 'h');
    INSERT INTO loose VALUES ('Z', 4, 'i');
    CREATE VIEW figures_v AS SELECT * FROM figures;
    CREATE TABLE ordered (region VARCHAR2(4), id NUMBER(5), CONSTRAINT ordered_pk PRIMARY KEY (region, id));
    CREATE TABLE written (id NUMBER(5) PRIMARY KEY)
"""


@pytest.fixture(scope="module")
def figured(oracle_server: OracleServer) -> str:
    return oracle_server.schema(FIGURES)


async def _classed(session: Any, relation: str, key: list[str], where: str | None = None) -> Plan:
    """A plan side of every non-key column of ``relation`` under its own name, with its classes."""
    found = await ORACLE.describe(session, None, relation)
    columns = {c.name: c.name for c in found.columns if c.name not in key}
    return {
        "schema": found.schema, "name": found.name, "keys": key, "key_maps": [None] * len(key),
        "columns": columns, "where": where, "types": {}, "digest": [], "engine": "oracle",
        "classes": {c.name: c.cls for c in found.columns},
        "declared": {c.name: c.declared for c in found.columns},
    }  # fmt: skip


async def test_the_catalogue_says_what_a_pre_check_compares(
    oracle_server: OracleServer, figured: str, tmp_path: Path
) -> None:
    targets = _registry(oracle_server, figured, tmp_path)
    async with ORACLE.snapshot(targets.get("db-a"), timeout_s=30) as session:
        table = await ORACLE.describe(session, None, "figures")
        view = await ORACLE.describe(session, None, "figures_v")
        ordered = await ORACLE.describe(session, None, "ordered")
        loose = await ORACLE.describe(session, None, "loose")
    assert {c.name: (c.cls, c.nullable, c.size) for c in table.columns} == {
        "ID": ("integer", False, None), "CODE": ("char", True, ("octets", 4)),
        "EMAIL": ("text", True, ("characters", 40)), "NM": ("text", True, ("characters", 20)),
        "NOTE": ("text", True, None), "AMOUNT": ("decimal", True, ("digits", 12, 2)),
        "BIG": ("decimal", True, None), "OPENED": ("timestamp", True, None),
        "SEEN": ("instant", True, ("fraction", 6)), "FLAG": ("boolean", True, None),
        "TAG": ("bytes", True, None), "BYTES_TEXT": ("text", True, ("octets", 10)),
    }  # fmt: skip
    assert (table.facts["kind"], table.facts["key"], loose.facts["key"]) == ("table", ["ID"], None)
    assert (view.facts["kind"], view.facts["key"]) == ("view", None)
    assert {c.nullable for c in view.columns} == {None}  # a view's is the server's guess: not said
    assert ordered.facts["key"] == ["REGION", "ID"]  # in the key's order
    assert (ORACLE.sum_digits, ORACLE.empty_null) == (38, True)


async def test_each_figure_of_each_class_is_the_database_s_own_aggregate(
    oracle_server: OracleServer, figured: str, tmp_path: Path
) -> None:
    targets = _registry(oracle_server, figured, tmp_path)
    wanted = {
        "CODE": ["nulls", "empty", "shortest", "longest", "distinct"],
        "EMAIL": ["nulls", "empty", "shortest", "longest", "distinct"],
        "NM": ["nulls", "shortest", "longest"],
        "NOTE": ["nulls"],
        "AMOUNT": ["nulls", "sum", "min", "max", "distinct"],
        "BIG": ["nulls", "sum", "min", "max"],
        "OPENED": ["nulls", "min", "max", "distinct"],
        "SEEN": ["min", "max"],
        "FLAG": ["nulls", "true", "distinct"],
        "TAG": ["nulls", "shortest", "longest", "distinct"],
    }
    asked = [("rows", None), ("null_keys", None), *((f, c) for c, names in wanted.items() for f in names)]
    async with ORACLE.snapshot(targets.get("db-a"), timeout_s=30) as session:
        planned = await _classed(session, "FIGURES", ["ID"])
        read = await figures.read(
            ORACLE, session, planned, asked, duplicates=True, distinct_max_rows=10, timeout_s=30
        )
    assert (read["failed"], read["skipped"]) == ({}, [])
    assert (read["rows"], read["null_keys"], read["duplicate_keys"], read["rows_in_duplicates"]) == (
        4,
        0,
        0,
        0,
    )
    assert read["figures"] == {
        # A CHAR without its padding: a blank one is empty and 0 long, a value as stored, as PostgreSQL's.
        "CODE": {"nulls": "1", "empty": "1", "shortest": "0", "longest": "4", "distinct": "3"},
        # '' is NULL there: no empty text; binary under the session's NLS_COMP: A@x.com is not a@x.com.
        "EMAIL": {"nulls": "1", "empty": "0", "shortest": "2", "longest": "7", "distinct": "3"},
        "NM": {"nulls": "3", "shortest": "2", "longest": "2"},  # LENGTH4: an emoji is one code point
        "NOTE": {"nulls": "3"},
        "AMOUNT": {"nulls": "1", "sum": "3", "min": "-.5", "max": "2", "distinct": "3"},
        # Oracle's sum lost the 1E-20: the pure module never compares a sum of a NUMBER without a scale.
        "BIG": {
            "nulls": "0",
            "sum": "1000000000000000000000000000006.5",
            "min": "." + "0" * 19 + "1",
            "max": "1" + "0" * 30,
        },
        # Canonical rendering 1, as PostgreSQL's to_jsonb writes it: a year before 1, an instant in UTC.
        "OPENED": {
            "nulls": "2",
            "min": "0044-03-15T00:00:00 BC",
            "max": "2024-05-01T00:00:00",
            "distinct": "2",
        },
        "SEEN": {"min": "2024-05-01T10:00:00+00:00", "max": "2024-05-01T10:00:00+00:00"},
        "FLAG": {"nulls": "1", "true": "2", "distinct": "2"},
        "TAG": {"nulls": "2", "shortest": "2", "longest": "3", "distinct": "2"},  # octets
    }


async def test_figures_count_rows_without_a_key_and_duplicates_do_not(
    oracle_server: OracleServer, figured: str, tmp_path: Path
) -> None:
    targets = _registry(oracle_server, figured, tmp_path)
    asked = [("rows", None), ("null_keys", None), ("nulls", "V"), ("distinct", "V")]
    async with ORACLE.snapshot(targets.get("db-a"), timeout_s=30) as session:
        planned = await _classed(session, "LOOSE", ["K", "PART"])
        read = await figures.read(
            ORACLE, session, planned, asked, duplicates=True, distinct_max_rows=9, timeout_s=30
        )
        filtered = await _classed(session, "LOOSE", ["K", "PART"], "PART < 3 AND V <> 'a'")
        narrowed = await figures.read(
            ORACLE, session, filtered, asked, duplicates=True, distinct_max_rows=9, timeout_s=30
        )
    # Nine rows, two without a whole key ('' is NULL); X1|1 thrice and Y|2 twice: x1 is another key.
    assert (read["rows"], read["null_keys"], read["figures"]["V"]) == (9, 2, {"nulls": "0", "distinct": "9"})
    assert (read["duplicate_keys"], read["rows_in_duplicates"]) == (2, 5)
    assert (narrowed["rows"], narrowed["null_keys"], narrowed["duplicate_keys"]) == (5, 0, 2)


async def test_a_pre_check_statement_past_its_timeout_is_a_timeout(targets: TargetRegistry) -> None:
    asked = [("rows", None), ("nulls", "S")]
    async with ORACLE.snapshot(targets.get("db-a"), timeout_s=30) as session:
        slow = await _classed(session, "SLOW", ["USERID"])
    async with ORACLE.snapshot(targets.get("db-a"), timeout_s=0.3) as session:
        read = await figures.read(
            ORACLE, session, slow, asked, duplicates=False, distinct_max_rows=9, timeout_s=0.3
        )
    assert read == {"figures": {}, "failed": {"figures": ("timeout", "0.3")}, "skipped": []}  # DPY-4024


async def test_a_read_only_account_lists_and_describes_what_it_may_read(
    oracle_server: OracleServer, figured: str, tmp_path: Path
) -> None:
    grants = (
        "GRANT READ ON {schema}.figures TO {user}; GRANT READ ON {schema}.figures_v TO {user};"
        " GRANT INSERT ON {schema}.written TO {user}; CREATE ROLE {user}_r; GRANT READ ON {schema}.ordered"
        " TO {user}_r; GRANT {user}_r TO {user}"
    )
    targets = _as(oracle_server, figured, tmp_path, grants)
    async with ORACLE.snapshot(targets.get("db-a"), timeout_s=30) as session:
        listed = await ORACLE.relations(session, figured.lower())  # as spelled, else upper-cased
        own = await ORACLE.relations(session, None)  # its own schema: nothing in it
        ordered = await ORACLE.describe(session, figured, "ordered")
    # A table it may only write is not listed; one a role it holds may read is.
    assert listed == [("FIGURES", "table"), ("FIGURES_V", "view"), ("ORDERED", "table")]
    assert own == [] and ordered.facts["key"] == ["REGION", "ID"]
