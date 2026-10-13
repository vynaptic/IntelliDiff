"""Table sides on a real MySQL (docs/mvp/ENGINES.md §6 to §11): the session, the column plan, per-key
reads, keys the column cannot hold, and what a scan is read in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import time
import tracemalloc
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pymysql
import pytest
import yaml

from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.postgres import engine as postgresql
from deltasleuth.connectors.tables import (
    ENGINES,
    Plan,
    StatementTimeout,
    TableConnector,
    TableError,
    TargetRefused,
    TargetTempSpace,
    TargetUnreachable,
    figures,
    plan,
)
from deltasleuth.connectors.tables import mysql as engine_module
from deltasleuth.core import precheck
from deltasleuth.core.spec import TargetRegistry, load_targets
from deltasleuth.core.types import ExecutionConfig, Observation, SecurityPolicy
from tests.conftest import MYSQL_IMAGE, MYSQL_PASSWORD, MySQLServer, _free_port, _unavailable

from .test_postgres import USERS_SIDE, compiled

pytestmark = [pytest.mark.db, pytest.mark.mysql]

MYSQL = ENGINES["mysql"]
# ``ci`` has the default, case- and accent-insensitive collation; ``Mixed`` is spelled in two cases.
# ``there`` holds the rows of ``here`` under other declarations (a latin1 text, a wider decimal) but row 8.
SEED = """
    CREATE TABLE users (userid INT PRIMARY KEY, email VARCHAR(100), name VARCHAR(100), seen TIMESTAMP NULL,
                        status CHAR(3), doc LONGTEXT, shape POINT);
    INSERT INTO users (userid, email, name, seen, status) VALUES
        (1, 'ann@example.com', 'Ann', '2024-05-01 10:00:00', 'ok'), (2, 'bob@example.com', 'Bob', NULL, 'no');
    CREATE VIEW users_v AS
        SELECT userid AS user_id, email AS Email, name AS full_name FROM users
        UNION ALL SELECT 2, 'bob@example.org', 'Bob';
    CREATE TABLE Mixed (Id INT PRIMARY KEY, EMail VARCHAR(20));
    CREATE TABLE ci (k VARCHAR(20) PRIMARY KEY, v INT);
    INSERT INTO ci VALUES ('e', 1), ('E2', 2), ('é3', 3), ('x1 ', 4), ('007', 5);
    CREATE TABLE latin (k VARCHAR(20) CHARACTER SET latin1 PRIMARY KEY, v INT);
    CREATE TABLE narrow (k VARCHAR(20) CHARACTER SET utf8mb3 PRIMARY KEY, v INT);
    CREATE TABLE moments (k DATETIME PRIMARY KEY, v INT);
    INSERT INTO moments VALUES ('2024-02-29 00:00:00', 1);
    CREATE TABLE heap (id INT PRIMARY KEY, v INT) ENGINE = MyISAM;
    INSERT INTO heap VALUES (1, 1), (2, 2);
    CREATE TABLE prices (k DECIMAL(10, 2) PRIMARY KEY, v INT);
    INSERT INTO prices VALUES (1.50, 1), (2.00, 2);
    CREATE TABLE tags (k VARBINARY(4) PRIMARY KEY, v INT);
    INSERT INTO tags VALUES (X'00ff', 1), (X'01', 2);
    CREATE VIEW slow AS
        SELECT userid, email, (SELECT COUNT(*) FROM users b WHERE SLEEP(1) = 0) AS s FROM users;
    CREATE TABLE here (k INT PRIMARY KEY, x VARCHAR(10), y VARCHAR(10), raw VARBINARY(4), n DECIMAL(6, 2),
                       seen DATETIME, doc TEXT);
    INSERT INTO here VALUES (1, 'ab', 'c', X'00ff', 1.50, '2024-02-29 10:00:00', 'd'),
        (2, 'a', 'bc', X'00ff', 1.50, '2024-02-29 10:00:00', 'd'), (3, NULL, 'a', NULL, NULL, NULL, NULL),
        (4, 'a', NULL, NULL, NULL, NULL, NULL), (5, '', NULL, NULL, NULL, NULL, NULL),
        (6, NULL, '', NULL, NULL, NULL, NULL), (7, 'ab', 'c', X'00ff', 1.50, '2024-02-29 10:00:00', 'd'),
        (8, 'é', 'c', X'00', 2.00, NULL, NULL), (9, 'é', 'c', X'00', 2.00, NULL, 'other');
    CREATE TABLE there (k INT PRIMARY KEY, x VARCHAR(10) CHARACTER SET latin1, y VARCHAR(10),
                        raw VARBINARY(4), n DECIMAL(8, 4), seen DATETIME, doc TEXT);
    INSERT INTO there SELECT * FROM here;
    UPDATE there SET raw = X'01' WHERE k = 8;
"""
BATCH_KEYS = [f"k{i:03d}" for i in range(198)]  # with two more, a batch of 200
Connect = Callable[..., TableConnector]


@pytest.fixture(scope="module")
def database(mysql_server: MySQLServer) -> str:
    name = mysql_server.database(SEED)
    values = ", ".join(f"('{key}', 1)" for key in BATCH_KEYS)
    script = f"INSERT INTO latin VALUES {values}; INSERT INTO narrow VALUES {values}"  # noqa: S608
    mysql_server.execute(script, name)
    return name


@pytest.fixture
def targets(mysql_server: MySQLServer, database: str, tmp_path: Path) -> TargetRegistry:
    """db-a and db-b: both the seeded database."""
    entry = mysql_server.target(database)
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    return load_targets(tmp_path / "targets.yaml")


def _as(mysql_server: MySQLServer, database: str, tmp_path: Path, grants: str) -> TargetRegistry:
    """db-a and db-b as a new account holding ``grants`` (statements with {db} and {user})."""
    user = f"reader_{uuid.uuid4().hex[:8]}"
    mysql_server.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY 'r'")
    if grants:
        mysql_server.execute(grants.format(db=f"`{database}`", user=f"'{user}'@'%'"))
    (tmp_path / "password").write_text("r")
    entry = mysql_server.target(database, user=user, password_ref="file:password")  # noqa: S106
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    return load_targets(tmp_path / "targets.yaml")


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


# --- the session (§6) ------------------------------------------------------------------------------


async def test_a_session_is_read_only_in_utc_and_encrypted(targets: TargetRegistry) -> None:
    session = await MYSQL.connect(targets.get("db-a"), timeout_s=7)
    try:
        settings = await session.query(
            "SELECT @@sql_mode, @@time_zone, @@wait_timeout, @@net_write_timeout, @@net_read_timeout,"
            " @@max_sort_length, @@max_execution_time, @@transaction_read_only, @@autocommit,"
            " @@character_set_client, @@character_set_results, @@local_infile"
        )
        cipher = await session.query("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        with pytest.raises(TargetRefused) as exc:
            await session.query("INSERT INTO ci VALUES ('w', 0)")
    finally:
        await session.close()
    assert settings == [
        ("", "+00:00", "86400", "86400", "86400", "8388608", "7000", "1", "1", "utf8mb4", "utf8mb4", "0")
    ]
    assert cipher[0][1] and session.encrypted
    assert str(exc.value) == "MySQL 1792"  # a write is refused; the code alone, never the server's text


async def test_a_snapshot_does_not_see_a_later_write(
    mysql_server: MySQLServer, database: str, targets: TargetRegistry
) -> None:
    mysql_server.execute("CREATE TABLE IF NOT EXISTS seen (id INT PRIMARY KEY); DELETE FROM seen", database)
    async with MYSQL.snapshot(targets.get("db-a")) as session:
        level = await session.query("SELECT @@transaction_isolation")
        mysql_server.execute("INSERT INTO seen VALUES (1)", database)
        inside = await session.query("SELECT COUNT(*) FROM seen")
    assert (level, inside) == ([("REPEATABLE-READ",)], [("0",)])
    assert mysql_server.execute("SELECT COUNT(*) FROM seen", database) == [(1,)]


# --- the column plan (§7) --------------------------------------------------------------------------


async def test_a_pair_stores_each_side_s_engine_classes_and_declared_types(targets: TargetRegistry) -> None:
    spec = compiled(
        {"name": "USERS", "keyColumns": ["UserId"], "columns": ["EMAIL", "name", "seen", "status"]},
        {
            "name": "users_v",
            "keyColumns": ["user_id"],
            "columns": ["email", {"name": "full_name", "as": "name"}],
        },
    )
    with pytest.raises(ValueError) as exc:
        await plan(targets, spec)
    assert str(exc.value) == "side a: column 'seen' has no counterpart on side b"
    spec = compiled(
        {"name": "USERS", "keyColumns": ["UserId"], "columns": ["EMAIL", "name"], "where": "Status = 'ok'"},
        {
            "name": "users_v",
            "keyColumns": ["user_id"],
            "columns": ["email", {"name": "full_name", "as": "name"}],
        },
    )
    stored = await plan(targets, spec)
    assert stored is not None
    a, b = stored["a"], stored["b"]
    schema = a["schema"]
    assert re.fullmatch("t_[0-9a-f]{12}", schema) and (a["name"], b["name"]) == ("users", "users_v")
    assert (a["keys"], a["columns"], a["where"]) == (
        ["userid"],
        {"email": "email", "name": "name"},
        "\"status\" = 'ok'",
    )
    assert (b["keys"], b["columns"]) == (["user_id"], {"email": "Email", "name": "full_name"})
    assert {name: a[name] for name in ("engine", "server", "consistency", "tls")} == {
        "engine": "mysql", "server": "8.4", "consistency": "snapshot", "tls": True,
    }  # fmt: skip
    assert b["consistency"] == "unverified"  # a view: its storage engine is NULL
    assert a["classes"] == {"userid": "integer", "email": "text", "name": "text", "status": "char"}
    assert a["declared"] == {
        "userid": "int",
        "email": "varchar(100)",
        "name": "varchar(100)",
        "status": "char(3)",
    }
    # Both sides declare both columns alike: the engine hashes them, the worker none (§10).
    assert (a["digest"], b["digest"], stored["rendering"]) == ([], [], 1)


async def test_the_engine_describes_a_relation_as_the_catalogue_holds_it(targets: TargetRegistry) -> None:
    session = await MYSQL.connect(targets.get("db-a"))
    try:
        mixed = await MYSQL.describe(session, None, "MIXED")  # the one name equal ignoring case
        heap = await MYSQL.describe(session, None, "heap")
        users = await MYSQL.describe(session, None, "users")
        assert await MYSQL.describe(session, "nowhere", "users") is None
        assert await MYSQL.describe(session, None, "\U0001f600") is None  # no catalogue name holds it
    finally:
        await session.close()
    assert mixed is not None and heap is not None and users is not None
    assert mixed.name == "Mixed" and [c.name for c in mixed.columns] == ["Id", "EMail"]
    assert heap.facts["consistency"] == "none"  # MyISAM accepts the snapshot statement and pins nothing
    assert users.facts == {
        "kind": "table", "key": ["userid"], "server": "8.4", "consistency": "snapshot", "tls": True,
        "charset": "utf8mb4",
    }  # fmt: skip
    assert [(c.name, c.declared, c.cls, c.likeable, c.keyable) for c in users.columns] == [
        ("userid", "int", "integer", False, True),
        ("email", "varchar(100)", "text", True, True),
        ("name", "varchar(100)", "text", True, True),
        ("seen", "timestamp", "instant", False, False),
        ("status", "char(3)", "char", True, True),
        ("doc", "longtext", "text", True, False),
        ("shape", "point", None, False, False),
    ]


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"name": "nope", "keyColumns": ["id"]}, "side a: table or view nope not found or not visible"),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email", "shape"]},
            "side a: column 'shape' (point) cannot be compared: compare a view that casts it",
        ),
        (
            {"name": "users", "keyColumns": ["seen"], "columns": ["email"]},
            "side a: key column 'seen' (timestamp) cannot be a key: compare a view that casts it",
        ),
        (
            {
                "name": "users",
                "keyColumns": ["userid"],
                "columns": ["email"],
                "where": "email = 'x' AND nope = 1",
            },
            None,  # names the database: asserted below
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "userid > 'abc'"},
            "side a: the filter compares column 'userid' (int) with a literal that is not a number",
        ),
        (
            {
                "name": "users",
                "keyColumns": ["userid"],
                "columns": ["email"],
                "where": "userid > 1e999999999999999999999999999999",
            },
            "side a: the filter compares column 'userid' (int) with a literal that is not a number",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "seen < 'soon'"},
            "side a: the filter compares column 'seen' (timestamp) with a literal that is not a timestamp",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "userid = TRUE"},
            "side a: the filter tests column 'userid' (int) as a boolean: compare it with 1 or 0",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "userid"},
            "side a: the filter tests column 'userid' (int) as a boolean: compare it with 1 or 0",
        ),
    ],
    ids=[
        *("P12", "P7", "P6", "P12 column", "P11 number", "P11 exponent", "P11 timestamp", "P11 TRUE"),
        "P11 bare",
    ],
)
async def test_a_plan_mysql_cannot_read_as_written_is_refused(
    targets: TargetRegistry, database: str, table: dict[str, Any], message: str | None
) -> None:
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(table, table))
    hidden = f"side a: column 'nope' is not in {database}.users, or the role may not read it: grant it SELECT"
    assert str(exc.value) == (message or hidden)


async def test_an_account_sees_only_the_columns_it_is_granted(
    mysql_server: MySQLServer, database: str, tmp_path: Path
) -> None:
    targets = _as(mysql_server, database, tmp_path, "GRANT SELECT (userid, email) ON {db}.users TO {user}")
    table = {"name": "users", "keyColumns": ["userid"]}
    stored = await plan(targets, compiled(table, table))
    assert stored is not None and stored["a"]["columns"] == {"email": "email"}  # the others are not listed
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(table | {"columns": ["email", "name"]}, table))
    assert str(exc.value) == (
        f"side a: column 'name' is not in {database}.users, or the role may not read it: grant it SELECT"
    )
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "ci", "keyColumns": ["k"]}))
    assert str(exc.value) == "side a: table or view ci not found or not visible"


async def test_a_table_the_account_may_only_write_is_refused(
    mysql_server: MySQLServer, database: str, tmp_path: Path
) -> None:
    targets = _as(mysql_server, database, tmp_path, "GRANT INSERT ON {db}.users TO {user}")
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]}))
    assert str(exc.value) == f"side a: the role may not read {database}.users: grant it SELECT"


async def test_a_refused_login_names_the_endpoint_and_glosses_the_code(
    mysql_server: MySQLServer, database: str, tmp_path: Path
) -> None:
    (tmp_path / "password").write_text("wrong-s3cret")
    entry = mysql_server.target(database, password_ref="file:password")  # noqa: S106
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry}}))
    targets = load_targets(tmp_path / "targets.yaml")
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]}))
    endpoint = f"db-a (mysql 127.0.0.1:{mysql_server.port}/{database} as root)"
    assert str(exc.value) == (
        f"side a: cannot connect to target {endpoint}: MySQL 1045: the credential was refused"
    )
    side = await _planned(_registry(mysql_server, database, tmp_path), {"name": "ci", "keyColumns": ["k"]})
    target, execution = targets.get("db-a"), ExecutionConfig()
    table = TableConnector(target, execution, side, gate=TargetGate.for_target(target))
    refused = await table.fetch("e", USERS_SIDE)
    await table.prefetch(["e", "E2"], USERS_SIDE)
    again = await table.fetch("E2", USERS_SIDE)
    await table.aclose()
    assert (refused.error_class, refused.error_detail) == ("auth_error", "MySQL 1045")  # the run pauses
    assert (again.error_class, again.error_detail) == ("auth_error", "MySQL 1045")


def _registry(mysql_server: MySQLServer, database: str, tmp_path: Path, **members: Any) -> TargetRegistry:
    entry = mysql_server.target(database, **members)
    (tmp_path / "good.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    return load_targets(tmp_path / "good.yaml")


async def test_an_account_reads_without_tls_when_its_target_allows_it(
    mysql_server: MySQLServer, database: str, tmp_path: Path
) -> None:
    # A new account, which the server has not cached: the full authentication of caching_sha2_password.
    _as(mysql_server, database, tmp_path, "GRANT SELECT ON {db}.* TO {user}")
    entry = yaml.safe_load((tmp_path / "targets.yaml").read_text())["targets"]["db-a"]
    del entry["tls_ca_ref"]
    plain = {"targets": {"db-a": entry | {"allow_unencrypted": True}}}
    (tmp_path / "plain.yaml").write_text(yaml.safe_dump(plain))
    session = await MYSQL.connect(load_targets(tmp_path / "plain.yaml").get("db-a"))
    try:
        cipher = await session.query("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        relation = await MYSQL.describe(session, None, "users")
    finally:
        await session.close()
    assert cipher == [("Ssl_cipher", "")] and not session.encrypted
    assert relation is not None and relation.facts["tls"] is False


# --- per-key reads (§8, §9) ------------------------------------------------------------------------


def _row(obs: Observation) -> Any:
    return None if obs.body is None else json.loads(obs.body)


async def test_a_key_reads_its_row_in_the_canonical_rendering(
    targets: TargetRegistry, connector: Connect
) -> None:
    table = {"name": "users", "keyColumns": ["userid"], "columns": ["email", "seen", "status"]}
    users = connector(await _planned(targets, table))
    found, missing, letters = [await users.fetch(key, USERS_SIDE) for key in ("1", "9", "x")]
    assert (found.http_status, found.request_url) == (
        200,
        f"mysql://db-a/{targets.get('db-a').database.name}.users",
    )
    assert _row(found) == {"email": "ann@example.com", "seen": "2024-05-01T10:00:00+00:00", "status": "ok"}
    assert (missing.http_status, missing.body) == (404, None)
    assert (letters.http_status, letters.body) == (404, None)  # letters are no integer: on no row


async def test_a_view_repeating_a_key_is_ambiguous(targets: TargetRegistry, connector: Connect) -> None:
    side = await _planned(targets, {"name": "users_v", "keyColumns": ["user_id"], "columns": ["email"]})
    twice = await connector(side).fetch("2", USERS_SIDE)
    assert (twice.http_status, twice.error_class) == (200, "identity_ambiguous")


async def test_a_key_is_its_text_exactly_whatever_the_collation_finds(
    targets: TargetRegistry, connector: Connect
) -> None:
    ci = connector(await _planned(targets, {"name": "ci", "keyColumns": ["k"]}))
    keys = ["e", "E", "é", "e2", "E2", "é3", "e3", "x1", "x1 ", "007", "7"]
    await ci.prefetch(keys, USERS_SIDE)
    found = {key: _row(await ci.fetch(key, USERS_SIDE)) for key in keys}
    assert found == {
        "e": {"v": 1}, "E": None, "é": None,  # the default collation finds e for all three
        "e2": None, "E2": {"v": 2}, "é3": {"v": 3}, "e3": None,
        "x1": None, "x1 ": {"v": 4},  # a trailing space is part of the key
        "007": {"v": 5}, "7": None,
    }  # fmt: skip


async def test_a_statement_past_its_timeout_is_a_timeout(targets: TargetRegistry, connector: Connect) -> None:
    side = await _planned(targets, {"name": "slow", "keyColumns": ["userid"], "columns": ["email", "s"]})
    slow = connector(side, total_timeout_s=0.5)
    obs = await slow.fetch("1", USERS_SIDE)
    assert (obs.error_class, obs.error_detail, obs.http_status) == ("timeout", "MySQL 3024", None)
    session = await MYSQL.connect(targets.get("db-a"), timeout_s=0.3)
    with pytest.raises(StatementTimeout):
        await session.query("SELECT SLEEP(2) FROM ci")
    await session.close()


@pytest.mark.parametrize(
    ("relation", "unfit", "served"),
    [
        ("latin", ["\U0001f600", "ж"], {}),  # neither an emoji nor a Cyrillic letter is latin1
        ("narrow", ["\U0001f600"], {"ж": 404}),  # utf8mb3 holds the letter, not the emoji
        ("moments", ["2024-02-31 00:00:00"], {"2024-02-29 00:00:00": 200, "soon": 404}),
        ("ci", ["\ud800"], {"e": 200}),  # a lone surrogate has no UTF-8
        # A number's form with an exponent no decimal holds is on no row, and fails nothing.
        ("prices", [], {"1e999999999999999999999999999999": 404, "1.50": 200}),
    ],
    ids=["latin1", "utf8mb3", "an impossible date", "a lone surrogate", "an exponent no decimal holds"],
)
async def test_a_key_its_column_cannot_hold_is_rejected_alone_in_its_batch(
    targets: TargetRegistry, connector: Connect, relation: str, unfit: list[str], served: dict[str, int]
) -> None:
    table = connector(await _planned(targets, {"name": relation, "keyColumns": ["k"]}))
    others = BATCH_KEYS[: 200 - len(unfit) - len(served)]
    await table.prefetch([*others[:100], *unfit, *served, *others[100:]], USERS_SIDE)
    expected = 200 if relation in ("latin", "narrow") else 404
    assert {(await table.fetch(key, USERS_SIDE)).http_status for key in others} == {expected}
    assert {key: (await table.fetch(key, USERS_SIDE)).http_status for key in served} == served
    for key in unfit:
        with pytest.raises(KeyRejectedError):
            await table.fetch(key, USERS_SIDE)


async def test_a_key_no_decimal_holds_is_on_no_row_read_alone(
    targets: TargetRegistry, connector: Connect
) -> None:
    prices = connector(await _planned(targets, {"name": "prices", "keyColumns": ["k"]}))
    missing = await prices.fetch("1e999999999999999999999999999999", USERS_SIDE)
    assert (missing.http_status, missing.body, missing.error_class) == (404, None, None)


async def test_a_session_the_server_ended_is_read_again_once(
    targets: TargetRegistry, connector: Connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        engine_module, "_SESSION", engine_module._SESSION.replace("wait_timeout = 86400", "wait_timeout = 1")
    )
    ci = connector(await _planned(targets, {"name": "ci", "keyColumns": ["k"]}))
    await ci.prefetch(["e", "E2"], USERS_SIDE)
    first = [(await ci.fetch(key, USERS_SIDE)).http_status for key in ("e", "E2")]
    await asyncio.sleep(2.5)  # the server ends the idle session
    await ci.prefetch(["e", "E2"], USERS_SIDE)
    assert first == [(await ci.fetch(key, USERS_SIDE)).http_status for key in ("e", "E2")] == [200, 200]


async def test_a_row_with_large_values_stays_in_its_database(
    mysql_server: MySQLServer, database: str, targets: TargetRegistry, connector: Connect
) -> None:
    mysql_server.execute(
        "CREATE TABLE docs (id INT PRIMARY KEY, doc LONGTEXT, note VARCHAR(10));"
        " INSERT INTO docs VALUES (1, 'small', 'a'), (2, REPEAT('x', 20 * 1024 * 1024), 'b'), (3, NULL, 'c')",
        database,
    )
    side = await _planned(targets, {"name": "docs", "keyColumns": ["id"]})
    docs = connector(side)
    tracemalloc.start()
    await docs.prefetch(["1", "2", "3"], USERS_SIDE)
    read = [await docs.fetch(key, USERS_SIDE) for key in ("1", "2", "3")]
    async with MYSQL.snapshot(targets.get("db-a")) as session:
        scanned = [row async for batch in await MYSQL.scan(session, side, None) for row in batch]
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
    assert docs.large and not connector(await _planned(targets, {"name": "ci", "keyColumns": ["k"]})).large


async def test_a_lookup_uses_the_key_s_index(targets: TargetRegistry) -> None:
    from deltasleuth.connectors.tables import compose

    session = await MYSQL.connect(targets.get("db-a"))
    try:
        for relation, key in (
            ("ci", "e"),  # the hexadecimal digits of a text
            ("heap", "1"),  # digits
            ("prices", "1.50"),  # digits, as the key spells them
            ("tags", "\\x00ff"),  # the hexadecimal digits of bytes
            ("moments", "2024-02-29 00:00:00"),
            ("latin", "k001"),  # utf8mb4 digits against a latin1 column
        ):
            column = "id" if relation == "heap" else "k"
            side = await _planned(targets, {"name": relation, "keyColumns": [column]})
            statement, _ = compose.rows(engine_module.PRIMITIVES, side, {key: [[key]]})
            explained = await session.query(f"EXPLAIN {statement}")
            assert [(row[4], row[6]) for row in explained] == [("const", "PRIMARY")], relation
            assert [found[0] for found in await session.query(statement)] == ["0"], relation  # and its row
    finally:
        await session.close()


# --- what a scan is read in (§6, §8, §11) ------------------------------------------------------------


async def test_a_scan_orders_keys_by_code_point_whatever_the_collation(targets: TargetRegistry) -> None:
    side = await _planned(targets, {"name": "ci", "keyColumns": ["k"]})
    async with MYSQL.snapshot(targets.get("db-a")) as session:
        keys = [row[0] async for batch in await MYSQL.scan(session, side, None) for row in batch]
        after = [row[0] async for batch in await MYSQL.scan(session, side, [b"e"]) for row in batch]
        census = [row async for batch in MYSQL.census(session, side) for row in batch]
        nulls = await MYSQL.null_keys(session, side)
    assert keys == sorted(keys) == ["007", "E2", "e", "x1 ", "é3"]  # the column's own order starts 007, e
    assert after == ["x1 ", "é3"] and census == [(key,) for key in keys] and nulls == 0


async def test_a_statement_killed_while_the_scan_waits_is_unreachable(
    mysql_server: MySQLServer, targets: TargetRegistry
) -> None:
    side = await _planned(targets, {"name": "slow", "keyColumns": ["userid"], "columns": ["email", "s"]})
    async with MYSQL.snapshot(targets.get("db-a")) as session:
        thread = (await session.query("SELECT CONNECTION_ID()"))[0][0]
        batches = await MYSQL.scan(session, side, None)  # the statement is sent: the server sleeps
        await asyncio.sleep(0.5)
        mysql_server.execute(f"KILL QUERY {int(thread)}")
        with pytest.raises(TableError) as exc:
            await anext(batches)
    assert (type(exc.value).__name__, str(exc.value)) == ("TargetUnreachable", "MySQL 1317")  # it resumes


async def test_closing_a_session_cancels_its_statement(
    mysql_server: MySQLServer, targets: TargetRegistry
) -> None:
    side = await _planned(targets, {"name": "slow", "keyColumns": ["userid"], "columns": ["email", "s"]})
    session = await MYSQL.connect(targets.get("db-a"))
    thread = (await session.query("SELECT CONNECTION_ID()"))[0][0]
    await MYSQL.scan(session, side, None)
    await asyncio.sleep(0.3)
    running = "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID = {} AND INFO LIKE 'SELECT%'"
    assert mysql_server.execute(running.format(int(thread))) == [(1,)]
    await asyncio.wait_for(session.close(), 3)  # not the 4 s the statement would sleep on
    await asyncio.sleep(0.3)
    assert mysql_server.execute(running.format(int(thread))) == [(0,)]


# --- rows hashed in the engine (§10) -----------------------------------------------------------------


async def _digests(targets: TargetRegistry, target: str, side: Plan) -> dict[str, bytes]:
    async with MYSQL.snapshot(targets.get(target)) as session:
        return {row[0]: row[1] async for batch in await MYSQL.scan(session, side, None) for row in batch}


async def test_the_engine_s_row_hash_tells_boundaries_and_nulls_apart(targets: TargetRegistry) -> None:
    both = {"name": "here", "keyColumns": ["k"], "columns": ["x", "y"]}
    side = await _planned(targets, both)
    assert side["digest"] == []  # the engine hashes both columns; it sorts keys and 32 bytes
    hashed = await _digests(targets, "db-a", side)
    # ('ab', 'c') is not ('a', 'bc'), (NULL, 'a') not ('a', NULL), ('', NULL) not (NULL, ''); row 7 is row 1.
    assert len({hashed[key] for key in "123456"}) == 6 and hashed["7"] == hashed["1"]
    assert hashed["1"] == hashlib.sha256(b'["ab", "c"]').digest()  # SHA2 of the JSON array of the texts
    assert hashed["5"] == hashlib.sha256(b'["", null]').digest()
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
    # hashes the others, a latin1 text against a utf8mb4 one among them, and bytes.
    assert stored["a"]["digest"] == stored["b"]["digest"] == ["doc", "n", "y"]
    here = await _digests(targets, "db-a", stored["a"])
    there = await _digests(targets, "db-b", stored["b"])
    assert {key for key in here if here[key] != there[key]} == {"8"} and len(here) == 9
    # Every column hashed by the worker gives the same answer.
    worker = sorted(stored["a"]["columns"])
    here = await _digests(targets, "db-a", stored["a"] | {"digest": worker})
    there = await _digests(targets, "db-b", stored["b"] | {"digest": worker})
    assert {key for key in here if here[key] != there[key]} == {"8"}


async def test_a_double_is_rendered_whole_in_a_batch_in_a_view_and_in_the_engine_s_hash(
    mysql_server: MySQLServer, database: str, targets: TargetRegistry, connector: Connect
) -> None:
    # A cast of a DOUBLE to CHAR is 22 characters wide. In a batch (a UNION of arms) the server cut a
    # longer text to them, so a value and its tenth read as one (a false MATCH); a view's expression it
    # rounded to fit, so two doubles that differ in the last digit read, and hashed, as one.
    values = [-2.2062116443042876e-16, -1.1102230246251565e-16, 1.9114234241396806e-147]
    values += [-0.000012345678901234568, -2.2250738585072014e-308 * 10, 0.5]
    rows = ", ".join(f"({i}, {value!r}, {value / 10!r})" for i, value in enumerate(values))
    table = "CREATE TABLE IF NOT EXISTS doubles (k INT PRIMARY KEY, d DOUBLE, tenth DOUBLE)"
    twins = "(9, -1.1102230246251565e-16, -1.1102230246251568e-16)"
    view = "CREATE OR REPLACE VIEW doubles_v AS SELECT k, d * 1 AS d, tenth * 1 AS tenth FROM doubles"
    seed = f"{table}; DELETE FROM doubles; INSERT INTO doubles VALUES {rows}, {twins}; {view}"  # noqa: S608
    mysql_server.execute(seed, database)
    keys = [str(i) for i in range(len(values))]
    for relation in ("doubles", "doubles_v"):
        side = await _planned(targets, {"name": relation, "keyColumns": ["k"]})
        assert (side["classes"]["d"], side["digest"]) == ("float", [])  # the engine hashes both columns
        table = connector(side)
        await table.prefetch([*keys, "9"], USERS_SIDE)
        read = {key: _row(await table.fetch(key, USERS_SIDE)) for key in [*keys, "9"]}
        alone = {key: _row(await connector(side).fetch(key, USERS_SIDE)) for key in [*keys, "9"]}
        assert read == alone
        assert [(read[key]["d"], read[key]["tenth"]) for key in keys] == [
            (value, value / 10) for value in values
        ], relation
        assert read["9"] == {"d": -1.1102230246251565e-16, "tenth": -1.1102230246251568e-16}
        # The scan's hash of each column alone: no value hashes as its tenth, nor as its neighbour.
        ours = await _digests(targets, "db-a", side | {"columns": {"d": "d"}})
        tenths = await _digests(targets, "db-a", side | {"columns": {"d": "tenth"}})
        assert not [key for key in ours if ours[key] == tenths[key]], relation
        assert ours["0"] == hashlib.sha256(b'["-2.2062116443042876e-16"]').digest()


# --- a filter and a key map behave as PostgreSQL's (§8, §10) -------------------------------------------


async def test_a_char_filter_and_prefix_pad_select_as_on_postgresql(
    mysql_server: MySQLServer, targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    rows = "('1', 'ab'), ('2', 'ab '), ('3', 'abc'), ('4', 'AB'), ('12\n', 'ab'), ('x', 'ab'), ('', 'ab')"
    table = "CREATE TABLE IF NOT EXISTS pads (k VARCHAR(10) PRIMARY KEY, st CHAR(3) COLLATE utf8mb4_bin)"
    seed = f"{table}; DELETE FROM pads; INSERT INTO pads VALUES {rows}"  # noqa: S608 (constants)
    mysql_server.execute(seed, targets.get("db-a").database.name)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE pads (k text PRIMARY KEY, st char(3)); INSERT INTO pads VALUES {rows}")  # noqa: S608
    (tmp_path / "dsn").write_text(pg_dsn)
    both = yaml.safe_load((tmp_path / "targets.yaml").read_text())
    both["targets"]["db-b"] = {"dsn_ref": "file:dsn"}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump(both))
    registry = load_targets(tmp_path / "targets.yaml")
    key_map = {"kind": "prefix_pad", "prefix": "P-", "width": 3, "sideType": "string"}
    pads = {"name": "pads", "keyColumns": [{"name": "k", "keyMap": key_map}], "where": "st = 'ab'"}
    stored = await plan(registry, compiled(pads, pads))
    assert stored is not None
    found = {}
    for name, engine in (("a", MYSQL), ("b", ENGINES["postgresql"])):
        async with engine.snapshot(registry.get(f"db-{name}")) as session:
            keys = [row[0] async for batch in engine.census(session, stored[name]) for row in batch]
            found[name] = (keys, await engine.null_keys(session, stored[name]))
    # ab and 'ab ' are one CHAR(3) value; 12 with a newline, x and '' have no canonical key: skipped.
    assert found["a"] == found["b"] == (["P-001", "P-002"], 3)


# --- what a full or cramped server answers (§11) -------------------------------------------------------


async def test_an_account_at_its_connection_limit_is_unreachable(
    mysql_server: MySQLServer, database: str, tmp_path: Path
) -> None:
    # DEPLOY tells the DBA to set the account's max_user_connections: over it the server answers 1226,
    # not the 1203 of its own limit, which once failed a scan at its third try, three sweeps later.
    targets = _as(mysql_server, database, tmp_path, "GRANT SELECT ON {db}.* TO {user}")
    user = targets.get("db-a").database.user  # type: ignore[union-attr]
    mysql_server.execute(f"ALTER USER '{user}'@'%' WITH MAX_USER_CONNECTIONS 1")
    held = await MYSQL.connect(targets.get("db-a"))
    try:
        with pytest.raises(TableError) as exc:
            await MYSQL.connect(targets.get("db-a"))
    finally:
        await held.close()
    assert (type(exc.value), str(exc.value)) == (TargetUnreachable, "MySQL 1226")  # waited out, as 1203


TMPFS = "/tmp:size=16m"  # noqa: S108 (the server's temporary directory, in its container)


@pytest.fixture(scope="module")
def cramped(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MySQLServer]:
    """A second MySQL server, whose temporary directory holds 16 MiB; it presents its stock certificate."""
    if not shutil.which("docker"):
        _unavailable("docker is not available")
    port, name = _free_port(), f"ds-test-mysql-cramped-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--pull", "never", "--name", name, "--tmpfs", TMPFS,
         "-e", f"MYSQL_ROOT_PASSWORD={MYSQL_PASSWORD}", "-p", f"127.0.0.1:{port}:3306", MYSQL_IMAGE,
         "--skip-log-bin"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if run.returncode != 0:
        _unavailable(f"cannot start {MYSQL_IMAGE}: {run.stderr.strip()[:200]}")
    secrets = tmp_path_factory.mktemp("cramped")
    (secrets / "password").write_text(MYSQL_PASSWORD)
    server = MySQLServer(name, port, secrets)
    try:
        deadline = time.monotonic() + 120
        while True:
            try:
                server.execute("SELECT 1")
                break
            except pymysql.MySQLError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)


async def test_a_sort_that_fills_the_temporary_directory_is_temp_space(
    cramped: MySQLServer, tmp_path: Path
) -> None:
    # 32 MiB of two compared columns the worker hashes, sorted in a directory of 16 MiB: the server's
    # code is 3 (observed on 8.4.11), none of 1021, 1028 and 1114. As any other code it was tried three
    # times, each a whole sort, before the run failed as refused.
    database = cramped.database(
        "CREATE TABLE wide (id INT PRIMARY KEY, a VARCHAR(400), b VARCHAR(400));"
        " SET SESSION cte_max_recursion_depth = 100000;"
        " INSERT INTO wide WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 40000)"
        " SELECT n, REPEAT(MD5(n), 12), REPEAT(MD5(n + 1), 12) FROM g"
    )
    entry = cramped.target(database, allow_unencrypted=True)
    del entry["tls_ca_ref"]
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    targets = load_targets(tmp_path / "targets.yaml")
    table = {"name": "wide", "keyColumns": ["id"]}
    mapped = {"a": {"as": "string"}, "b": {"as": "string"}}
    stored = await plan(targets, compiled(table, table, types={"a": mapped, "b": mapped}))
    assert stored is not None and stored["a"]["digest"] == ["a", "b"]
    async with MYSQL.snapshot(targets.get("db-a")) as session:
        batches = await MYSQL.scan(session, stored["a"], None)
        with pytest.raises(TableError) as exc:
            await anext(batches)
    assert (type(exc.value), str(exc.value)) == (TargetTempSpace, "MySQL 3")  # F3, at first sight


# --- the PostgreSQL side of a pair with MySQL (§6) ----------------------------------------------------


async def test_the_postgresql_side_of_a_pair_across_engines_is_fetched_by_bytes(
    mysql_server: MySQLServer, targets: TargetRegistry, pg_dsn: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    # Across engines every compared column comes with each row: 10,000 rows a fetch, whatever they
    # held, once read 600 documents of 100 KB in one batch.
    rows, width = 600, 100_000
    mysql_server.execute(
        "CREATE TABLE IF NOT EXISTS papers (id INT PRIMARY KEY, body MEDIUMTEXT)",
        targets.get("db-a").database.name,  # type: ignore[union-attr]
    )
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        seed = f"INSERT INTO papers SELECT g, repeat('x', {width}) FROM generate_series(1, {rows}) g"  # noqa: S608
        conn.execute(f"CREATE TABLE papers (id int PRIMARY KEY, body text); {seed}")
    (tmp_path / "dsn").write_text(pg_dsn)
    both = yaml.safe_load((tmp_path / "targets.yaml").read_text())
    both["targets"]["db-b"] = {"dsn_ref": "file:dsn"}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump(both))
    registry = load_targets(tmp_path / "targets.yaml")
    papers = {"name": "papers", "keyColumns": ["id"]}
    stored = await plan(registry, compiled(papers, papers))
    assert stored is not None and stored["b"]["digest"] == ["body"] and stored["b"]["classes"]
    monkeypatch.setattr(postgresql, "BATCH_BYTES", 4 * 1024 * 1024)
    engine = ENGINES["postgresql"]

    async def fetched(side: Plan) -> tuple[list[int], int]:
        """The rows of each batch of the side's scan, and the most memory it held."""
        tracemalloc.start()
        async with engine.snapshot(registry.get("db-b")) as session:
            sizes = [len(batch) async for batch in await engine.scan(session, side, None)]
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return sizes, peak

    sizes, peak = await fetched(stored["b"])
    # 100 rows whose width is not known yet, then as many as hold 4 MiB at the widest row's width.
    assert sizes[0] == 100 and set(sizes[1:-1]) == {41} and sum(sizes) == rows
    assert peak < rows * width / 2
    # Between two PostgreSQL tables the fetch is what it was (D3), also where the worker hashes a column.
    mapped = {"body": {"as": "string"}}
    both["targets"]["db-a"] = both["targets"]["db-b"]
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump(both))
    frozen = await plan(
        load_targets(tmp_path / "targets.yaml"), compiled(papers, papers, types={"a": mapped, "b": mapped})
    )
    assert frozen is not None and frozen["b"]["digest"] == ["body"] and "classes" not in frozen["b"]
    assert (await fetched(frozen["b"]))[0] == [rows]


# --- what a table pre-check reads (docs/mvp/PRECHECKS.md §4, §5) ----------------------------------------

# One row a class holds at its edge: a CHAR padded and blank, texts that differ by case alone in the default,
# case-insensitive collation and by a trailing space alone under its PAD SPACE, an emoji, an empty text, a
# TINYINT(1) and moments of each class. ``loose`` repeats keys, holds one without a part, and is filtered.
FIGURES = """
    CREATE TABLE figures (
        id INT PRIMARY KEY, code CHAR(4), email VARCHAR(40), amount DECIMAL(12, 2), qty BIGINT,
        opened DATE, seen TIMESTAMP(6) NULL, at DATETIME(3), span TIME(3), flag TINYINT(1),
        raw VARBINARY(8), kind ENUM('ok', 'sad'), doc TEXT, rate DOUBLE);
    INSERT INTO figures VALUES
        (1, 'ab', 'A@x.com', 1.50, 10, '2024-05-01', '2024-05-01 10:00:00', '2024-05-01 10:00:00.5',
         '03:04:05.120', 1, X'00ff', 'ok', 'd', 1.5),
        (2, '   ', 'a@x.com', -0.50, 9007199254740993, '2024-05-02', NULL, NULL, '23:59:59', 0, X'', 'sad',
         NULL, NULL),
        (3, 'abcd', '', 2.00, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
        (4, NULL, 'a ', NULL, -3, NULL, NULL, NULL, NULL, 1, X'010203', 'ok', NULL, NULL),
        (5, 'x', 'x\U0001f600', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
    CREATE TABLE loose (k VARCHAR(10), part INT, v VARCHAR(10));
    INSERT INTO loose VALUES ('X1', 1, 'a'), ('x1', 1, 'b'), ('X1', 1, 'c'), ('X1', 1, 'd'), ('Y', 2, 'e'),
                             ('Y', 2, 'f'), (NULL, 3, 'g'), ('Z', NULL, 'h'), ('Z', 4, 'i');
    CREATE VIEW figures_v AS SELECT * FROM figures;
    CREATE TABLE ordered (region VARCHAR(4), id INT, secret INT, PRIMARY KEY (secret, region, id));
    CREATE TABLE written (id INT PRIMARY KEY);
"""


@pytest.fixture(scope="module")
def figured(mysql_server: MySQLServer) -> str:
    return mysql_server.database(FIGURES)


def _figures_targets(
    mysql_server: MySQLServer, database: str, tmp_path: Path, **members: Any
) -> TargetRegistry:
    entry = mysql_server.target(database, **members)
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry}}))
    return load_targets(tmp_path / "targets.yaml")


async def _classed(session: Any, relation: str, key: list[str], where: str | None = None) -> Plan:
    """A plan side of every non-key column of ``relation`` under its own name, with its classes."""
    found = await MYSQL.describe(session, None, relation)
    columns = {c.name: c.name for c in found.columns if c.name not in key}
    return {
        "schema": found.schema, "name": found.name, "keys": key, "key_maps": [None] * len(key),
        "columns": columns, "where": where, "types": {}, "digest": [], "engine": "mysql",
        "classes": {c.name: c.cls for c in found.columns},
        "declared": {c.name: c.declared for c in found.columns},
    }  # fmt: skip


async def test_the_catalogue_says_what_a_pre_check_compares(
    mysql_server: MySQLServer, figured: str, tmp_path: Path
) -> None:
    targets = _figures_targets(mysql_server, figured, tmp_path)
    async with MYSQL.snapshot(targets.get("db-a"), timeout_s=30) as session:
        table = await MYSQL.describe(session, None, "figures")
        view = await MYSQL.describe(session, None, "FIGURES_V")
        ordered = await MYSQL.describe(session, None, "ordered")
        loose = await MYSQL.describe(session, None, "loose")
    assert {c.name: (c.cls, c.nullable, c.size) for c in table.columns} == {
        "id": ("integer", False, None), "code": ("char", True, ("characters", 4)),
        "email": ("text", True, ("characters", 40)), "amount": ("decimal", True, ("digits", 12, 2)),
        "qty": ("integer", True, None), "opened": ("date", True, None),
        "seen": ("instant", True, ("fraction", 6)),
        "at": ("timestamp", True, ("fraction", 3)), "span": ("time", True, ("fraction", 3)),
        "flag": ("integer", True, None), "raw": ("bytes", True, None), "kind": ("text", True, None),
        "doc": ("text", True, None), "rate": ("float", True, None),
    }  # fmt: skip
    assert (table.facts["kind"], table.facts["key"], loose.facts["key"]) == ("table", ["id"], None)
    assert (view.facts["kind"], view.facts["key"]) == ("view", None)
    assert {c.nullable for c in view.columns} == {None}  # a view's is the server's guess: not said
    assert ordered.facts["key"] == ["secret", "region", "id"]  # in the key's order
    assert (MYSQL.sum_digits, MYSQL.empty_null) == (65, False)


async def test_each_figure_of_each_class_is_the_database_s_own_aggregate(
    mysql_server: MySQLServer, figured: str, tmp_path: Path
) -> None:
    targets = _figures_targets(mysql_server, figured, tmp_path)
    wanted = {
        "code": ["nulls", "empty", "shortest", "longest", "distinct"],
        "email": ["nulls", "empty", "shortest", "longest", "distinct"],
        "amount": ["nulls", "sum", "min", "max", "distinct"],
        "qty": ["nulls", "sum", "min", "max"],
        "opened": ["nulls", "min", "max", "distinct"],
        "seen": ["min", "max"],
        "at": ["min", "max"],
        "span": ["min", "max", "distinct"],
        "flag": ["nulls", "sum", "distinct"],
        "raw": ["nulls", "shortest", "longest", "distinct"],
        "kind": ["nulls", "empty", "shortest", "longest", "distinct"],
        "doc": ["nulls"],
    }
    asked = [("rows", None), ("null_keys", None), *((f, c) for c, names in wanted.items() for f in names)]
    async with MYSQL.snapshot(targets.get("db-a"), timeout_s=30) as session:
        planned = await _classed(session, "figures", ["id"])
        read = await figures.read(
            MYSQL, session, planned, asked, duplicates=True, distinct_max_rows=10, timeout_s=30
        )
    assert (read["failed"], read["skipped"]) == ({}, [])
    assert (read["rows"], read["null_keys"], read["duplicate_keys"], read["rows_in_duplicates"]) == (
        5,
        0,
        0,
        0,
    )
    assert read["figures"] == {
        # The server strips a CHAR's padding: a blank one is empty, 0 long, as PostgreSQL's.
        "code": {"nulls": "1", "empty": "1", "shortest": "0", "longest": "4", "distinct": "4"},
        # Bytes: A@x.com and a@x.com are one value to the default collation, a and 'a ' to its PAD SPACE,
        # and '' is not 'a ' either; an emoji is one code point.
        "email": {"nulls": "0", "empty": "1", "shortest": "0", "longest": "7", "distinct": "5"},
        "amount": {"nulls": "2", "sum": "3.00", "min": "-0.50", "max": "2.00", "distinct": "3"},
        "qty": {"nulls": "2", "sum": "9007199254741000", "min": "-3", "max": "9007199254740993"},
        "opened": {"nulls": "3", "min": "2024-05-01", "max": "2024-05-02", "distinct": "2"},
        # Canonical rendering 1, as PostgreSQL's to_jsonb writes it: no trailing zero, an instant's zone.
        "seen": {"min": "2024-05-01T10:00:00+00:00", "max": "2024-05-01T10:00:00+00:00"},
        "at": {"min": "2024-05-01T10:00:00.5", "max": "2024-05-01T10:00:00.5"},
        "span": {"min": "03:04:05.12", "max": "23:59:59", "distinct": "2"},
        "flag": {"nulls": "2", "sum": "2", "distinct": "2"},  # a TINYINT(1) is a number
        "raw": {"nulls": "2", "shortest": "0", "longest": "3", "distinct": "3"},  # octets
        "kind": {"nulls": "2", "empty": "0", "shortest": "2", "longest": "3", "distinct": "2"},
        "doc": {"nulls": "4"},
    }


# Two BIT(64) columns whose rows agree in count, NULLs, minimum and maximum: side a's sum is 3 * 2^63, past
# what an UNSIGNED cast holds, which would clip it to side b's 2^64 - 1.
BITS = """
    CREATE TABLE bits_a (id INT PRIMARY KEY, c BIT(64));
    INSERT INTO bits_a VALUES (1, 0), (2, 1 << 63), (3, 1 << 63), (4, 1 << 63);
    CREATE TABLE bits_b (id INT PRIMARY KEY, c BIT(64));
    INSERT INTO bits_b VALUES (1, 0), (2, 1 << 62), (3, (1 << 62) - 1), (4, 1 << 63);
"""


async def test_a_bit_column_s_sum_is_its_exact_decimal(mysql_server: MySQLServer, tmp_path: Path) -> None:
    targets = _figures_targets(mysql_server, mysql_server.database(BITS), tmp_path)
    asked = [
        ("rows", None),
        ("null_keys", None),
        *((figure, "c") for figure in ("nulls", "sum", "min", "max")),
    ]
    read, relations, plan_ = {}, {}, {}
    async with MYSQL.snapshot(targets.get("db-a"), timeout_s=30) as session:
        for side, name in (("a", "bits_a"), ("b", "bits_b")):
            relations[side] = await MYSQL.describe(session, None, name)
            plan_[side] = await _classed(session, name, ["id"])
            read[side] = await figures.read(
                MYSQL, session, plan_[side], asked, duplicates=False, distinct_max_rows=9, timeout_s=30
            )
    top = str(2**63)
    assert read["a"]["figures"]["c"] == {"nulls": "0", "sum": str(3 * 2**63), "min": "0", "max": top}
    assert read["b"]["figures"]["c"] == {"nulls": "0", "sum": str(2**64 - 1), "min": "0", "max": top}
    clear = precheck.Pair(1, "bits 1.0.0", security=SecurityPolicy(unlisted="clear"))
    at = "2026-10-07T10:02:11.000000Z"
    sides = {s: read[s] | {"engine": "mysql", "at": at, "sum_digits": 65, "empty_null": False} for s in "ab"}
    found = precheck.compare(
        "columns", "columns", clear, plan_ | {"only_a": [], "only_b": []}, relations, sides
    )
    assert found.result["columns"]["c"]["figures"]["sum"] == {
        "a": str(3 * 2**63), "b": str(2**64 - 1), "verdict": "differs",
        "reason": f"side b is {3 * 2**63 - 2**64 + 1} lower",
    }  # fmt: skip
    assert found.verdict == "values_differ"


async def test_figures_count_rows_without_a_key_and_duplicates_do_not(
    mysql_server: MySQLServer, figured: str, tmp_path: Path
) -> None:
    targets = _figures_targets(mysql_server, figured, tmp_path)
    asked = [("rows", None), ("null_keys", None), ("nulls", "v"), ("distinct", "v")]
    async with MYSQL.snapshot(targets.get("db-a"), timeout_s=30) as session:
        planned = await _classed(session, "loose", ["k", "part"])
        read = await figures.read(
            MYSQL, session, planned, asked, duplicates=True, distinct_max_rows=9, timeout_s=30
        )
        filtered = await _classed(session, "loose", ["k", "part"], "part < 3 AND v <> 'a'")
        narrowed = await figures.read(
            MYSQL, session, filtered, asked, duplicates=True, distinct_max_rows=9, timeout_s=30
        )
    # Nine rows, two of them without a whole key; X1|1 thrice and Y|2 twice, grouped by the key texts'
    # bytes: x1 is another key, though the column's collation calls it X1.
    assert (read["rows"], read["null_keys"], read["figures"]["v"]) == (9, 2, {"nulls": "0", "distinct": "9"})
    assert (read["duplicate_keys"], read["rows_in_duplicates"]) == (2, 5)
    assert (narrowed["rows"], narrowed["null_keys"], narrowed["duplicate_keys"]) == (5, 0, 2)


async def test_a_statement_past_its_timeout_is_a_timeout_under_3024_and_1317(
    mysql_server: MySQLServer, database: str, targets: TargetRegistry
) -> None:
    asked = [("rows", None), ("nulls", "s")]
    async with MYSQL.snapshot(targets.get("db-a"), timeout_s=0.3) as session:
        slow = await _classed(session, "slow", ["userid"])  # a second a row
        read = await figures.read(
            MYSQL, session, slow, asked, duplicates=False, distinct_max_rows=9, timeout_s=0.3
        )
    assert read == {"figures": {}, "failed": {"figures": ("timeout", "0.3")}, "skipped": []}  # 3024
    # The server interrupting a statement once its timeout has passed (1317) is a timeout too: here a KILL
    # QUERY ends it, the session's own limit being off.
    async with MYSQL.snapshot(targets.get("db-a")) as session:
        slow = await _classed(session, "slow", ["userid"])
        thread = (await session.query("SELECT CONNECTION_ID()"))[0][0]

        async def killed() -> None:
            await asyncio.sleep(0.5)
            mysql_server.execute(f"KILL QUERY {int(thread)}")

        killing = asyncio.create_task(killed())
        read = await figures.read(
            MYSQL, session, slow, asked, duplicates=False, distinct_max_rows=9, timeout_s=0.3
        )
        await killing
    assert read["failed"] == {"figures": ("timeout", "0.3")}


async def test_a_read_only_account_lists_and_describes_what_it_may_read(
    mysql_server: MySQLServer, figured: str, tmp_path: Path
) -> None:
    user = f"reader_{uuid.uuid4().hex[:8]}"
    mysql_server.execute(
        f"CREATE USER '{user}'@'%' IDENTIFIED BY 'r';"
        f" GRANT SELECT ON `{figured}`.figures TO '{user}'@'%';"
        f" GRANT SELECT ON `{figured}`.figures_v TO '{user}'@'%';"
        f" GRANT SELECT (region, id) ON `{figured}`.ordered TO '{user}'@'%';"
        f" GRANT INSERT ON `{figured}`.written TO '{user}'@'%'"
    )
    (tmp_path / "password").write_text("r")
    targets = _figures_targets(mysql_server, figured, tmp_path, user=user, password_ref="file:password")  # noqa: S106
    async with MYSQL.snapshot(targets.get("db-a"), timeout_s=30) as session:
        own = await MYSQL.relations(session, None)  # the target's database
        spelled = await MYSQL.relations(session, figured.upper())  # else the one equal ignoring case
        none = await MYSQL.relations(session, "nowhere")
        ordered = await MYSQL.describe(session, None, "ordered")
    # A table it may only write is not listed; one of whose columns it may read some is.
    assert own == spelled == [("figures", "table"), ("figures_v", "view"), ("ordered", "table")]
    assert none == [] and [c.name for c in ordered.columns] == ["region", "id"]
    assert ordered.facts["key"] == ["secret", "region", "id"]  # a key column it may not read among them
