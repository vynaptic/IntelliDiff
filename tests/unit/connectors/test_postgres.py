"""Table sides on a real PostgreSQL: the column plan a run reads by, and the
per-key reads of ``TableConnector``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.tables import ENGINES, Plan, TableConnector, figures, plan, render
from deltasleuth.core import precheck
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.spec import TargetRegistry, compile_spec, load_targets
from deltasleuth.core.types import CompiledSpec, ExecutionConfig, Observation, Side, SideStateKind

pytestmark = pytest.mark.db

# users_v repeats key 2; "Mixed" is spelled quoted, with two columns that lower-case alike.
SEED = """
    CREATE TABLE users (userid int PRIMARY KEY, email text, name text, seen timestamptz, legacy_flag bool);
    INSERT INTO users VALUES (1, 'ann@example.com', 'Ann', '2024-05-01 12:00:00+02', true),
                             (2, 'bob@example.com', 'Bob', NULL, false);
    CREATE VIEW users_v AS
        SELECT userid AS user_id, email AS "Email", name AS full_name, 1 AS version FROM users
        UNION ALL SELECT 2, 'bob@example.org', 'Bob', 1;
    CREATE TABLE "Mixed" ("Id" int, "Email" text, email text);
    CREATE TABLE keys_only (id int PRIMARY KEY);
    CREATE TABLE pct (id int PRIMARY KEY, email text, "note%" text);
    CREATE VIEW slow_users AS SELECT userid, email, pg_sleep(0.5)::text AS s FROM users;
    CREATE COLLATION ci (provider = icu, locale = 'und-u-ks-level2', deterministic = false);
    CREATE TABLE ci_keys (k text COLLATE ci PRIMARY KEY, v text);
    INSERT INTO ci_keys VALUES ('X1', 'upper');
    CREATE TABLE ci_keys_too (k text COLLATE ci, v text);
    INSERT INTO ci_keys_too VALUES ('X1', 'upper'), ('x1', 'lower');
"""
Connect = Callable[[Plan], TableConnector]


def side(relation: str, key: str, /, **columns: str) -> Plan:
    """One side of a plan as runs store it (canonical name -> column), in schema public."""
    return {
        "schema": "public", "name": relation, "keys": [key], "key_maps": [None], "columns": columns,
        "where": None, "types": {}, "digest": [],
    }  # fmt: skip


def compiled(
    a: dict[str, Any], b: dict[str, Any] | None = None, *, types: dict[str, dict[str, Any]] | None = None
) -> CompiledSpec:
    """Table side ``a`` on db-a against table side ``b`` on db-b, or against a service when ``b`` is None;
    ``types`` holds each side's typeMap."""
    key: dict[str, Any] = {"name": "userid", "type": "string"}
    service = {"role": "new", "target": "svc", "request": {"method": "GET", "path": "/users/{{ key }}"}}
    if b is None:
        key |= {"pattern": "^[0-9]+$", "source": {"kind": "list", "keys": ["1"]}}
    sides = {
        "a": {"role": "legacy", "target": "db-a", "table": a},
        "b": service if b is None else {"role": "new", "target": "db-b", "table": b},
    }
    for name, type_map in (types or {}).items():
        sides[name]["typeMap"] = type_map
    return compile_spec({
        "apiVersion": "deltasleuth/v1", "kind": "ComparisonSpec",
        "metadata": {"name": "tables", "version": "1.0.0", "owner": "tests"}, "normalizerVersion": "1.1.0",
        "key": key, "sides": sides, "compare": {}, "recheck": {"delay": "0s", "times": 1},
    })  # fmt: skip


USERS = side("users", "userid", email="email", seen="seen")
USERS_SIDE = compiled({"name": "users", "keyColumns": ["userid"]}).sides[Side.A]


@pytest.fixture
def targets(pg_dsn: str, tmp_path: Path) -> TargetRegistry:
    """db-a and db-b: both the test database, seeded, whose sessions default to another time zone."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(SEED)
        conn.execute(f"ALTER DATABASE \"{pg_dsn.rsplit('/', 1)[1]}\" SET TimeZone = 'Asia/Kolkata'")
    (tmp_path / "dsn").write_text(pg_dsn)
    entry = {"dsn_ref": "file:dsn"}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    return load_targets(tmp_path / "targets.yaml")


# --- the column plan --------------------------------------------------------------------------


async def test_explicit_columns_pair_by_canonical_name(targets: TargetRegistry) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["userid"], "columns": ["email", "name"]},
        {
            "name": "users_v",
            "keyColumns": ["user_id"],
            "columns": ["Email", {"name": "full_name", "as": "name"}],
        },
    )
    assert await plan(targets, spec) == {
        "a": side("users", "userid", email="email", name="name"),
        "b": side("users_v", "user_id", email="Email", name="full_name"),
        "only_a": [],
        "only_b": [],
    }


async def test_omitted_columns_compare_the_names_both_sides_have(targets: TargetRegistry) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["userid"]}, {"name": "users_v", "keyColumns": ["user_id"]}
    )
    assert await plan(targets, spec) == {
        "a": side("users", "userid", email="email"),
        "b": side("users_v", "user_id", email="Email"),
        "only_a": ["name", "seen", "legacy_flag"],
        "only_b": ["full_name", "version"],
    }


async def test_names_match_as_spelled_else_as_postgresql_folds_them(targets: TargetRegistry) -> None:
    spec = compiled(
        {"schema": "PUBLIC", "name": "Users", "keyColumns": ["UserId"], "columns": ["EMAIL"]},
        {"name": "Mixed", "keyColumns": ["Id"], "columns": ["Email"]},
    )
    assert await plan(targets, spec) == {
        "a": side("users", "userid", email="email"),  # the catalogue's spelling
        "b": side("Mixed", "Id", email="Email"),
        "only_a": [],
        "only_b": [],
    }


async def test_a_mixed_pair_plans_its_table_side_only(targets: TargetRegistry) -> None:
    assert await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]})) == {
        "a": side("users", "userid", email="email", name="name", seen="seen", legacy_flag="legacy_flag"),
        "only_a": [],
        "only_b": [],
    }


@pytest.mark.parametrize(
    ("a", "b", "message"),
    [
        (
            {"schema": "public", "name": "nope", "keyColumns": ["userid"]},
            {"name": "users_v", "keyColumns": ["user_id"]},
            "side a: table or view public.nope not found or not visible",
        ),
        (
            {"name": "users", "keyColumns": ["userid"]},
            {"name": "users_v", "keyColumns": ["nope"]},
            "side b: column 'nope' is not in public.users_v",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email", "legacy_flag"]},
            {"name": "users_v", "keyColumns": ["user_id"], "columns": ["Email"]},
            "side a: column 'legacy_flag' has no counterpart on side b",
        ),
        (
            {"name": "users", "keyColumns": ["userid"]},
            {"name": "Mixed", "keyColumns": ["Id"]},
            "side b: columns 'Email' and 'email' both compare as 'email'",
        ),
        (
            {"name": "users", "keyColumns": ["userid"]},
            {"name": "keys_only", "keyColumns": ["id"]},
            "side a: no column to compare: list the columns to compare;"
            " a key column listed alone compares which keys exist",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "where": "legacy_flag AND nope = 1"},
            {"name": "users_v", "keyColumns": ["user_id"]},
            "side a: column 'nope' is not in public.users",
        ),
    ],
    ids=[
        "missing relation",
        "missing key column",
        "no counterpart",
        "collision",
        "no column in common",
        "missing filter column",
    ],
)
async def test_a_plan_that_cannot_be_read_as_written_is_refused(
    targets: TargetRegistry, a: dict[str, Any], b: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(a, b))
    assert str(exc.value) == message


async def test_a_name_holding_a_percent_is_refused_only_when_it_is_read(targets: TargetRegistry) -> None:
    one_sided = await plan(
        targets, compiled({"name": "pct", "keyColumns": ["id"]}, {"name": "users", "keyColumns": ["userid"]})
    )
    assert one_sided["only_a"] == ["note%"]  # never in a statement
    with pytest.raises(ValueError) as exc:
        await plan(
            targets, compiled({"name": "pct", "keyColumns": ["id"]}, {"name": "pct", "keyColumns": ["id"]})
        )
    assert str(exc.value) == "side a: the name 'note%' holds a %, which cannot be read; use a view"


async def test_a_column_on_one_side_only_needs_no_grant(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    role = f"reader_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'r'; GRANT SELECT (id, email) ON pct TO {role};")
        conn.execute(f"GRANT SELECT ON users TO {role}")
    (tmp_path / "dsn").write_text(pg_dsn.replace("postgres:test@", f"{role}:r@"))
    spec = compiled({"name": "pct", "keyColumns": ["id"]}, {"name": "users", "keyColumns": ["userid"]})
    assert (await plan(targets, spec))["only_a"] == ["note%"]  # never read, so never refused


async def test_a_wrong_password_names_the_side_and_target_never_the_password(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    (tmp_path / "dsn").write_text(pg_dsn.replace(":test@", ":wrong-s3cret@"))
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]}))
    assert str(exc.value).startswith("side a: cannot connect to target db-a: ")
    assert "wrong-s3cret" not in str(exc.value)


@pytest.mark.parametrize("password", ["te@st-s3cret", "te/st-s3cret"], ids=["unencoded @", "unencoded /"])
async def test_a_dsn_libpq_would_quote_names_only_the_target(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path, password: str
) -> None:
    (tmp_path / "dsn").write_text(pg_dsn.replace(":test@", f":{password}@"))
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]}))
    assert str(exc.value) == "the DSN of target db-a is not a valid connection string"


async def test_a_keyword_dsn_may_name_a_database_with_an_at_sign(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    (tmp_path / "dsn").write_text(f"{pg_dsn.rsplit('/', 1)[0]}/my%40db")  # no such database
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]}))
    assert str(exc.value).startswith("side a: cannot connect to target db-a: ")


async def test_a_table_the_role_may_not_read_is_refused(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    role = f"reader_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'r'")
    (tmp_path / "dsn").write_text(pg_dsn.replace("postgres:test@", f"{role}:r@"))
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]}))
    assert str(exc.value) == "side a: the role may not read public.users: grant it SELECT"


async def test_a_column_the_role_may_not_read_is_refused(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    role = f"reader_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE {role} LOGIN PASSWORD 'r'; GRANT SELECT (userid, email) ON users TO {role}"
        )
    (tmp_path / "dsn").write_text(pg_dsn.replace("postgres:test@", f"{role}:r@"))
    spec = compiled({"name": "users", "keyColumns": ["userid"], "columns": ["email"]})
    assert (await plan(targets, spec))["a"]["columns"] == {"email": "email"}
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"name": "users", "keyColumns": ["userid"]}))  # every column
    assert str(exc.value) == "side a: the role may not read column 'name' of public.users: grant it SELECT"


async def test_a_schema_the_role_may_not_use_is_not_visible(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    role = f"reader_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA crm; CREATE TABLE crm.customers (id int PRIMARY KEY)")
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'r'")
    (tmp_path / "dsn").write_text(pg_dsn.replace("postgres:test@", f"{role}:r@"))
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled({"schema": "crm", "name": "customers", "keyColumns": ["id"]}))
    assert str(exc.value) == "side a: table or view crm.customers not found or not visible"


async def test_the_engine_describes_a_relation_as_the_catalogue_holds_it(targets: TargetRegistry) -> None:
    engine = ENGINES["postgresql"]
    session = await engine.connect(targets.get("db-a"))
    try:
        users = await engine.describe(session, None, "USERS")  # folded, as an unquoted name
        ci_keys = await engine.describe(session, "public", "ci_keys")
        assert await engine.describe(session, "crm", "customers") is None
    finally:
        await session.close()
    assert users is not None and (users.schema, users.name) == ("public", "users")
    assert [(c.name, c.declared, c.readable, c.likeable) for c in users.columns] == [
        ("userid", "integer", True, False),
        ("email", "text", True, True),
        ("name", "text", True, True),
        ("seen", "timestamp with time zone", True, False),
        ("legacy_flag", "boolean", True, False),
    ]
    assert ci_keys is not None and [c.likeable for c in ci_keys.columns] == [False, True]  # k's collation
    server = users.facts.pop("server")
    assert re.fullmatch(r"16\.[0-9]+", server)  # the major and minor version only
    assert users.facts == {
        "kind": "table", "key": ["userid"], "consistency": "snapshot", "tls": False, "charset": "UTF8",
    }  # fmt: skip
    assert [c.nullable for c in users.columns] == [False, True, True, True, True]  # the key's NOT NULL


# --- per-key reads ----------------------------------------------------------------------------


@pytest.fixture
async def connector(targets: TargetRegistry) -> AsyncIterator[Connect]:
    """``connector(side)``: a TableConnector reading a side of a plan on db-a; closed after the test."""
    made: list[TableConnector] = []

    def make(side: Plan) -> TableConnector:
        target, execution = targets.get("db-a"), ExecutionConfig()
        made.append(TableConnector(target, execution, side, gate=TargetGate.for_target(target)))
        return made[-1]

    yield make
    for table in made:
        await table.aclose()


async def test_a_row_is_its_canonical_columns_as_json(connector: Connect) -> None:
    obs = await connector(USERS).fetch("1", USERS_SIDE)
    assert (obs.http_status, obs.request_url) == (200, "postgresql://db-a/public.users")
    assert obs.body is not None
    assert json.loads(obs.body) == {"email": "ann@example.com", "seen": "2024-05-01T10:00:00+00:00"}  # UTC


async def test_no_row_is_a_404(connector: Connect) -> None:
    obs = await connector(USERS).fetch("3", USERS_SIDE)
    assert (obs.http_status, obs.body, obs.error_class) == (404, None, None)


async def test_a_key_selecting_two_rows_is_ambiguous(connector: Connect, targets: TargetRegistry) -> None:
    spec = compiled({"name": "users_v", "keyColumns": ["user_id"]})
    obs = await connector(side("users_v", "user_id", email="Email")).fetch("2", spec.sides[Side.A])
    assert (obs.error_class, obs.body) == ("identity_ambiguous", None)
    state = observe(obs, spec.sides[Side.A], spec, targets.get("db-a").capabilities)
    assert (state.kind, state.reason) == (SideStateKind.INCOMPLETE, "identity_ambiguous")


async def test_a_row_is_read_only_by_its_own_key_text(connector: Connect) -> None:
    # '01' casts to 1, but row 1's key text is '1', so the scan never pairs them either
    assert (await connector(USERS).fetch("01", USERS_SIDE)).http_status == 404


async def test_an_ampersand_in_a_replacement_is_literal_in_both_directions(connector: Connect) -> None:
    rewrite = {"kind": "regex", "sideType": "integer"}
    rewrite |= {"toCanonical": {"pattern": "([0-9]+)", "replacement": "U&$1"}}
    rewrite |= {"toSide": {"pattern": "U&([0-9]+)", "replacement": "$1"}}
    table = connector(USERS | {"key_maps": [rewrite]})
    assert [(await table.fetch(key, USERS_SIDE)).http_status for key in ("U&1", "U&01")] == [200, 404]


async def test_keys_equal_only_in_a_nondeterministic_collation_are_different_keys(connector: Connect) -> None:
    table = connector(side("ci_keys_too", "k", v="v"))
    read = [await table.fetch(key, USERS_SIDE) for key in ("X1", "x1")]
    assert [email_or_status(o, "v") for o in read] == ["upper", "lower"]  # as the scan sees them


def email_or_status(obs: Observation, column: str = "email") -> str | int | None:
    return json.loads(obs.body)[column] if obs.body is not None else obs.http_status


async def test_a_key_the_column_type_rejects_is_refused(connector: Connect) -> None:
    with pytest.raises(KeyRejectedError) as exc:
        await connector(USERS).fetch("not-a-number", USERS_SIDE)
    assert "not-a-number" not in str(exc.value)


async def test_a_lost_connection_is_a_connect_error_and_the_next_read_reconnects(
    connector: Connect, pg_server: str, pg_dsn: str
) -> None:
    table = connector(USERS)
    assert (await table.fetch("1", USERS_SIDE)).http_status == 200
    with psycopg.connect(pg_server, autocommit=True) as admin:  # the server ends the session
        admin.execute(
            "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity"
            " WHERE application_name = 'deltasleuth' AND datname = %s",
            (pg_dsn.rsplit("/", 1)[1],),
        )
    lost = await table.fetch("1", USERS_SIDE)
    assert (lost.error_class, lost.http_status, lost.body) == ("connect_error", None, None)
    assert (await table.fetch("1", USERS_SIDE)).http_status == 200


# --- filters, key maps and type maps in the plan -------------------------------------------


async def test_filter_columns_resolve_as_columns_do(targets: TargetRegistry) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "LEGACY_FLAG"},
        {
            "name": "Mixed",
            "keyColumns": ["Id"],
            "columns": ["Email"],
            "where": "EMAIL LIKE '%x' AND \"Email\" IS NOT NULL",  # folded, then as spelled
        },
    )
    tables = await plan(targets, spec)
    assert (tables["a"]["where"], tables["b"]["where"]) == (
        '"legacy_flag"',
        """"email" LIKE '%x' AND "Email" IS NOT NULL""",
    )


async def test_a_filter_column_the_role_may_not_read_is_refused(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    role = f"reader_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE {role} LOGIN PASSWORD 'r'; GRANT SELECT (userid, email) ON users TO {role}"
        )
    (tmp_path / "dsn").write_text(pg_dsn.replace("postgres:test@", f"{role}:r@"))
    spec = compiled({"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "name = 'x'"})
    with pytest.raises(ValueError) as exc:
        await plan(targets, spec)
    assert str(exc.value) == "side a: the role may not read column 'name' of public.users: grant it SELECT"


async def test_a_type_map_column_must_be_compared(targets: TargetRegistry) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["userid"], "columns": ["email"]},
        {"name": "users_v", "keyColumns": ["user_id"], "columns": ["Email"]},
        types={"a": {"legacy_flag": {"as": "boolean"}}},  # a column of the table, not compared
    )
    with pytest.raises(ValueError) as exc:
        await plan(targets, spec)
    assert str(exc.value) == "side a: typeMap column 'legacy_flag' is not compared"


async def test_digest_columns_are_the_columns_either_side_maps(targets: TargetRegistry) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["userid"], "columns": ["email", "name"]},
        {
            "name": "users_v",
            "keyColumns": ["user_id"],
            "columns": ["Email", {"name": "full_name", "as": "name"}],
        },
        types={"a": {"email": {"as": "string"}}, "b": {"full_name": {"as": "string"}}},
    )
    tables = await plan(targets, spec)
    assert (tables["a"]["types"], tables["b"]["types"]) == (
        {"email": {"as": "string"}},
        {"name": {"as": "string"}},
    )
    assert tables["a"]["digest"] == tables["b"]["digest"] == ["email", "name"]


async def test_a_postgresql_pair_s_mapped_columns_are_hashed_on_the_event_loop(
    targets: TargetRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Handing each fetched batch to a thread cost a PostgreSQL pair with typeMap columns about a sixth
    # of its scan (1M rows a side: 33 s on the loop, as before the seam, 39 s through a thread).
    users = {"name": "users", "keyColumns": ["userid"], "columns": ["email", "name"]}
    mapped = {"email": {"as": "string"}}
    tables = await plan(targets, compiled(users, users, types={"a": mapped, "b": mapped}))
    assert tables is not None and tables["a"]["digest"] == ["email"] and "classes" not in tables["a"]
    threads: list[int] = []
    digest = render.digest
    monkeypatch.setattr(render, "digest", lambda *row: threads.append(threading.get_ident()) or digest(*row))
    engine = ENGINES["postgresql"]

    async def scanned(side: Plan) -> list[tuple[Any, ...]]:
        async with engine.snapshot(targets.get("db-a")) as session:
            return [row async for batch in await engine.scan(session, side, None) for row in batch]

    rows = await scanned(tables["a"])
    assert len(rows) == 2 and threads == [threading.get_ident()] * 2
    # A side of a pair with another engine holds its columns' classes: every compared column of every
    # row is the worker's there, and a batch is hashed off the loop.
    threads.clear()
    classes = {"userid": "integer", "email": "text", "name": "text"}
    assert await scanned(tables["a"] | {"classes": classes}) == rows
    assert len(threads) == 2 and threading.get_ident() not in threads


ARE_ONLY = {  # valid for Python's re, which the loader checks; PostgreSQL's ARE refuses (?P<...>)
    "kind": "regex",
    "toCanonical": {"pattern": "(?P<n>[0-9]+)", "replacement": "U$1"},
    "toSide": {"pattern": "U([0-9]+)", "replacement": "$1"},
    "sideType": "integer",
}


@pytest.mark.parametrize(
    ("a", "b", "message"),
    [
        (
            {"name": "users", "keyColumns": [{"name": "userid", "keyMap": ARE_ONLY}], "columns": ["email"]},
            {"name": "users_v", "keyColumns": ["user_id"], "columns": ["Email"]},
            "side a: the database refuses the filter or key map: invalid regular expression",
        ),
        (
            {"name": "users", "keyColumns": ["userid"], "columns": ["email"]},
            {
                "name": "users_v",
                "keyColumns": ["user_id"],
                "columns": ["Email"],
                "where": "user_id LIKE '1%'",
            },
            "side b: column 'user_id' cannot be matched: LIKE and ILIKE match text",
        ),
        (
            {"name": "ci_keys", "keyColumns": ["k"], "columns": ["v"], "where": "k LIKE 'X%'"},
            {"name": "ci_keys", "keyColumns": ["k"], "columns": ["v"]},
            "side a: column 'k' cannot be matched: ",  # PostgreSQL 16 refuses LIKE there, row by row
        ),
    ],
    ids=["pattern", "LIKE on an integer", "LIKE in a nondeterministic collation"],
)
async def test_what_the_database_cannot_run_is_refused_when_the_run_is_created(
    targets: TargetRegistry, a: dict[str, Any], b: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(a, b))
    assert str(exc.value).startswith(message)


# --- per-key and batched reads with filters, key maps and type maps --------------------

PREFIX_PAD = {"kind": "prefix_pad", "prefix": "U-", "width": 3, "sideType": "integer"}


async def test_a_row_outside_the_filter_is_read_and_recorded_as_not_selected(
    targets: TargetRegistry, connector: Connect
) -> None:
    spec = compiled({"name": "users", "keyColumns": ["userid"], "columns": ["email"], "where": "legacy_flag"})
    table, cfg = connector((await plan(targets, spec))["a"]), spec.sides[Side.A]
    inside, outside = await table.fetch("1", cfg), await table.fetch("2", cfg)  # user 2's flag is false
    assert [(o.http_status, o.headers) for o in (inside, outside)] == [
        (200, {}),
        (200, {"selected": "false"}),
    ]
    assert outside.body is not None and json.loads(outside.body) == {"email": "bob@example.com"}
    states = [observe(o, cfg, spec, targets.get("db-a").capabilities) for o in (inside, outside)]
    assert [(s.kind, s.selected) for s in states] == [
        (SideStateKind.COMPLETE, True),
        (SideStateKind.COMPLETE, False),
    ]


async def test_type_map_columns_are_recorded_converted(targets: TargetRegistry, connector: Connect) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["userid"], "columns": ["email", "seen", "legacy_flag"]},
        types={
            "a": {
                "seen": {"as": "date", "naiveTimezone": "Pacific/Kiritimati"},
                "legacy_flag": {"as": "string"},
            }
        },
    )
    table = connector((await plan(targets, spec))["a"])
    ann, bob = [await table.fetch(key, spec.sides[Side.A]) for key in ("1", "2")]
    assert ann.body is not None and bob.body is not None
    assert json.loads(ann.body) == {"email": "ann@example.com", "seen": "2024-05-02", "legacy_flag": "true"}
    assert json.loads(bob.body) == {"email": "bob@example.com", "seen": None, "legacy_flag": "false"}


async def test_a_value_that_does_not_convert_is_unparseable_naming_its_column(
    targets: TargetRegistry, connector: Connect
) -> None:
    spec = compiled(
        {"name": "users", "keyColumns": ["userid"], "columns": ["email"]},
        types={"a": {"email": {"as": "integer"}}},
    )
    obs = await connector((await plan(targets, spec))["a"]).fetch("1", spec.sides[Side.A])
    assert (obs.http_status, obs.body, obs.error_class) == (200, None, "unconvertible")
    assert obs.error_detail == "column 'email' does not convert to integer"
    state = observe(obs, spec.sides[Side.A], spec, targets.get("db-a").capabilities)
    assert (state.kind, state.reason) == (SideStateKind.INCOMPLETE, "unparseable")
    assert state.detail == "typeMap: column 'email' does not convert to integer"


def change_emails(dsn: str) -> None:
    """Change every row after a prefetch: a read served from the prefetch still shows the old values."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE users SET email = 'changed'")


def email(obs: Observation) -> str | int | None:
    return json.loads(obs.body)["email"] if obs.body is not None else obs.http_status


async def test_prefetch_reads_a_batch_that_fetch_then_serves_once(connector: Connect, pg_dsn: str) -> None:
    table = connector(USERS)
    await table.prefetch(["1", "2", "3"], USERS_SIDE)
    change_emails(pg_dsn)
    served = [email(await table.fetch(key, USERS_SIDE)) for key in ("1", "2", "3")]
    assert served == ["ann@example.com", "bob@example.com", 404]
    assert email(await table.fetch("1", USERS_SIDE)) == "changed"  # a later pass reads again


async def test_a_batch_with_a_key_its_column_cannot_hold_still_serves_the_others(
    connector: Connect, pg_dsn: str
) -> None:
    table = connector(USERS)
    await table.prefetch(["1", "not-a-number", "2"], USERS_SIDE)
    change_emails(pg_dsn)
    assert [email(await table.fetch(key, USERS_SIDE)) for key in ("1", "2")] == [
        "ann@example.com",
        "bob@example.com",
    ]
    with pytest.raises(KeyRejectedError):
        await table.fetch("not-a-number", USERS_SIDE)


async def test_a_batch_that_times_out_reads_each_key_alone_once(
    targets: TargetRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, execution = targets.get("db-a"), ExecutionConfig(total_timeout_s=0.2)
    table = TableConnector(
        target,
        execution,
        side("slow_users", "userid", email="email"),
        gate=TargetGate.for_target(target),
    )
    sizes, query = [], TableConnector._query

    async def counted(self: TableConnector, asked: dict[str, Any], observation: Any) -> Any:
        sizes.append(len(asked))
        return await query(self, asked, observation)

    monkeypatch.setattr(TableConnector, "_query", counted)
    try:
        await table.prefetch(["1", "2", "3"], USERS_SIDE)
        read = [await table.fetch(key, USERS_SIDE) for key in ("1", "2", "3")]
    finally:
        await table.aclose()
    assert sizes == [3, 1, 1, 1]  # never halved: a slow key costs two timeouts, not log2(batch) + 1
    timeout = (None, "timeout")  # rows 1 and 2 sleep; key 3 has no row, so its read alone is quick
    assert [(o.http_status, o.error_class) for o in read] == [timeout, timeout, (404, None)]


async def test_a_batch_on_a_session_the_server_ended_is_read_again_on_a_new_one(
    connector: Connect, pg_server: str, pg_dsn: str
) -> None:
    table = connector(USERS)
    assert (await table.fetch("1", USERS_SIDE)).http_status == 200
    with psycopg.connect(pg_server, autocommit=True) as admin:  # an idle session ends, as between cycles
        admin.execute(
            "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity"
            " WHERE application_name = 'deltasleuth' AND datname = %s",
            (pg_dsn.rsplit("/", 1)[1],),
        )
    await table.prefetch(["1", "2", "3"], USERS_SIDE)
    assert [email(await table.fetch(key, USERS_SIDE)) for key in ("1", "2", "3")] == [
        "ann@example.com",
        "bob@example.com",
        404,
    ]


async def test_a_key_its_side_cannot_map_back_is_404_without_a_query(
    targets: TargetRegistry, tmp_path: Path
) -> None:
    (tmp_path / "closed_dsn").write_text("postgresql://postgres:test@127.0.0.1:1/nothing")  # nothing listens
    target = dataclasses.replace(targets.get("db-a"), dsn_ref=f"file:{tmp_path / 'closed_dsn'}")
    execution = ExecutionConfig()
    mapped = side("users", "userid", email="email") | {"key_maps": [PREFIX_PAD]}
    table = TableConnector(target, execution, mapped, gate=TargetGate.for_target(target))
    try:
        await table.prefetch(["X-1"], USERS_SIDE)
        unmapped = [await table.fetch(key, USERS_SIDE) for key in ("X-1", "U-1x")]
        mappable = await table.fetch("U-001", USERS_SIDE)
    finally:
        await table.aclose()
    assert [(o.http_status, o.error_class) for o in unmapped] == [(404, None), (404, None)]
    assert mappable.error_class == "connect_error"  # its read was sent


# --- what a table pre-check reads ----------------------------------------

# One row a class holds at its edge: a padded and a blank ``character``, texts that differ by case
# alone in a case-insensitive collation, an emoji, an empty text, and a numeric that holds NaN.
FIGURES = """
    CREATE TABLE figures (
        id int PRIMARY KEY, code char(4), email text COLLATE ci, amount numeric(12,2), rate numeric,
        qty bigint, opened date, seen timestamptz, at time, flag boolean, raw bytea, kind mood, tag uuid);
    INSERT INTO figures VALUES
        (1, 'ab', 'A@x.com', 1.50, 1, 10, '2024-05-01', '2024-05-01 12:00:00+02', '03:04:05.120', true,
         '\\x00ff', 'ok', 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'),
        (2, '', 'a@x.com', -0.50, 'NaN', 9007199254740993, '0044-03-15 BC', '2024-05-01 09:59:59.5+00',
         '23:59:59', false, '', 'sad', NULL),
        (3, 'abcd', '', 2.00, 2.5, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
        (4, NULL, 'x😀', NULL, NULL, -3, '2024-05-02', NULL, '03:04:05.12', true, '\\x010203', 'ok', NULL);
    CREATE TABLE loose (k text COLLATE ci, part int, v text);
    INSERT INTO loose VALUES ('X1', 1, 'a'), ('x1', 1, 'b'), ('X1', 1, 'c'), ('X1', 1, 'd'), ('Y', 2, 'e'),
                             ('Y', 2, 'f'), (NULL, 3, 'g'), ('Z', NULL, 'h'), ('Z', 4, 'i');
    CREATE VIEW figures_v AS SELECT * FROM figures;
"""


@pytest.fixture
async def checked(targets: TargetRegistry, pg_dsn: str) -> AsyncIterator[tuple[Any, Any]]:
    """PostgreSQL's engine and a snapshot session on db-a, seeded with ``FIGURES``."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("CREATE TYPE mood AS ENUM ('ok', 'sad')")
        conn.execute(FIGURES)
    engine = ENGINES["postgresql"]
    async with engine.snapshot(targets.get("db-a"), timeout_s=30) as session:
        yield engine, session


async def _classed(
    engine: Any, session: Any, relation: str, key: list[str], where: str | None = None
) -> Plan:
    """A plan side of every non-key column of ``relation`` under its own name, with its classes."""
    found = await engine.describe(session, None, relation)
    columns = {c.name: c.name for c in found.columns if c.name not in key}
    planned = side(relation, key[0], **columns) | {"keys": key, "key_maps": [None] * len(key), "where": where}
    return figures.classed(planned, found)


async def test_the_catalogue_says_what_a_pre_check_compares(checked: tuple[Any, Any]) -> None:
    engine, session = checked
    table, view = (
        await engine.describe(session, None, "figures"),
        await engine.describe(session, None, "figures_v"),
    )
    assert {c.name: (c.cls, c.nullable, c.size) for c in table.columns} == {
        "id": ("integer", False, None), "code": ("char", True, ("characters", 4)),
        "email": ("text", True, None), "amount": ("decimal", True, ("digits", 12, 2)),
        "rate": ("decimal", True, None), "qty": ("integer", True, None), "opened": ("date", True, None),
        "seen": ("instant", True, None), "at": ("time", True, None), "flag": ("boolean", True, None),
        "raw": ("bytes", True, None), "kind": ("text", True, None), "tag": ("text", True, None),
    }  # fmt: skip
    assert (table.facts["kind"], table.facts["key"]) == ("table", ["id"])
    assert (view.facts["kind"], view.facts["key"]) == ("view", None)
    assert {c.nullable for c in view.columns} == {None}  # no view's column says whether it may hold NULL
    loose = await engine.describe(session, None, "loose")
    assert loose.facts["key"] is None and precheck.shape(table) != precheck.shape(view)
    assert (engine.sum_digits, engine.empty_null) == (None, False)


async def test_each_figure_of_each_class_is_the_database_s_own_aggregate(checked: tuple[Any, Any]) -> None:
    engine, session = checked
    planned = await _classed(engine, session, "figures", ["id"])
    wanted = {
        "code": ["nulls", "empty", "shortest", "longest", "distinct"],
        "email": ["nulls", "empty", "shortest", "longest", "distinct"],
        "amount": ["nulls", "sum", "min", "max", "distinct"],
        "rate": ["nulls", "sum", "min", "max"],
        "qty": ["nulls", "sum", "min", "max"],
        "opened": ["nulls", "min", "max", "distinct"],
        "seen": ["min", "max"],
        "at": ["min", "max", "distinct"],
        "flag": ["nulls", "true", "distinct"],
        "raw": ["nulls", "shortest", "longest", "distinct"],
        "kind": ["nulls", "empty", "shortest", "longest", "distinct"],
        "tag": ["nulls", "shortest", "longest"],
    }
    asked = [("rows", None), ("null_keys", None), *((f, c) for c, names in wanted.items() for f in names)]
    read = await figures.read(
        engine, session, planned, asked, duplicates=True, distinct_max_rows=10, timeout_s=30
    )
    assert (read["failed"], read["skipped"]) == ({}, [])
    assert (read["rows"], read["null_keys"], read["duplicate_keys"], read["rows_in_duplicates"]) == (
        4,
        0,
        0,
        0,
    )
    assert read["figures"] == {
        # A character(4) without its padding: '' and 'ab'; the blank one is empty.
        "code": {"nulls": "1", "empty": "1", "shortest": "0", "longest": "4", "distinct": "3"},
        # Binary on the text: 'A@x.com' and 'a@x.com' are one value to the column's own collation.
        "email": {"nulls": "0", "empty": "1", "shortest": "0", "longest": "7", "distinct": "4"},
        "amount": {"nulls": "1", "sum": "3.00", "min": "-0.50", "max": "2.00", "distinct": "3"},
        "rate": {"nulls": "1", "sum": "NaN", "min": "1", "max": "NaN"},  # a numeric holds NaN
        "qty": {"nulls": "1", "sum": "9007199254741000", "min": "-3", "max": "9007199254740993"},
        "opened": {"nulls": "1", "min": "0044-03-15 BC", "max": "2024-05-02", "distinct": "3"},
        # Canonical rendering 1, in UTC whatever the database's own time zone.
        "seen": {"min": "2024-05-01T09:59:59.5+00:00", "max": "2024-05-01T10:00:00+00:00"},
        "at": {"min": "03:04:05.12", "max": "23:59:59", "distinct": "2"},
        "flag": {"nulls": "1", "true": "2", "distinct": "2"},
        "raw": {"nulls": "1", "shortest": "0", "longest": "3", "distinct": "3"},  # octets
        "kind": {"nulls": "1", "empty": "0", "shortest": "2", "longest": "3", "distinct": "2"},
        "tag": {"nulls": "3", "shortest": "36", "longest": "36"},
    }
    assert read["figures"]["email"]["longest"] == "7" and len("x😀") == 2  # code points, not bytes


async def test_figures_count_rows_without_a_key_and_duplicates_do_not(checked: tuple[Any, Any]) -> None:
    engine, session = checked
    planned = await _classed(engine, session, "loose", ["k", "part"])
    asked = [("rows", None), ("null_keys", None), ("nulls", "v"), ("distinct", "v")]
    read = await figures.read(
        engine, session, planned, asked, duplicates=True, distinct_max_rows=9, timeout_s=30
    )
    # Nine rows, two of them without a whole key: a scan's rows_a, and its skipped_a.
    assert (read["rows"], read["null_keys"], read["figures"]["v"]) == (9, 2, {"nulls": "0", "distinct": "9"})
    # X1|1 thrice and Y|2 twice, grouped by the key texts: x1 is another key, as it is to the scan's merge;
    # the row whose key is NULL and the one whose part is are not in the population.
    assert (read["duplicate_keys"], read["rows_in_duplicates"]) == (2, 5)
    assert (
        await engine.duplicates(session, planned) == (2, 5) and await engine.null_keys(session, planned) == 2
    )
    filtered = await _classed(engine, session, "loose", ["k", "part"], "part < 3 AND v <> 'a'")
    read = await figures.read(
        engine, session, filtered, asked, duplicates=True, distinct_max_rows=9, timeout_s=30
    )
    assert (read["rows"], read["null_keys"]) == (5, 0)  # the rows the filter selects
    assert (read["duplicate_keys"], read["rows_in_duplicates"]) == (2, 4)
    # Over distinct_max_rows the statements that sort or hash are not sent: no duplicate key, no distinct.
    read = await figures.read(
        engine, session, planned, asked, duplicates=True, distinct_max_rows=8, timeout_s=30
    )
    assert (read["rows"], read["skipped"], read["figures"]["v"]) == (9, ["distinct_rows"], {"nulls": "0"})
    assert "duplicate_keys" not in read and read["failed"] == {}
    mapped = planned | {"keys": ["part"], "key_maps": [PREFIX_PAD], "where": None}
    read = await figures.read(
        engine, session, mapped, asked[:2], duplicates=True, distinct_max_rows=9, timeout_s=30
    )
    assert (read["rows"], read["null_keys"]) == (9, 1)  # a key map's constants are bound, as a scan's
    assert (read["duplicate_keys"], read["rows_in_duplicates"]) == (2, 6)


async def test_a_statement_past_its_timeout_is_a_timeout(targets: TargetRegistry) -> None:
    engine = ENGINES["postgresql"]
    asked = [("rows", None), ("nulls", "email")]
    async with engine.snapshot(targets.get("db-a"), timeout_s=0.3) as session:
        users = await _classed(engine, session, "users", ["userid"])
        slow = await _classed(engine, session, "slow_users", ["userid"])  # half a second a row
        quick = await figures.read(
            engine, session, users, asked, duplicates=True, distinct_max_rows=9, timeout_s=0.3
        )
        read = await figures.read(
            engine, session, slow, asked, duplicates=True, distinct_max_rows=9, timeout_s=0.3
        )
    assert quick == {
        "rows": 2, "figures": {"email": {"nulls": "0"}}, "duplicate_keys": 0, "rows_in_duplicates": 0,
        "failed": {}, "skipped": [],
    }  # fmt: skip
    assert read == {"figures": {}, "failed": {"figures": ("timeout", "0.3")}, "skipped": []}  # 57014


async def test_a_read_only_account_lists_and_describes_what_it_may_read(
    targets: TargetRegistry, pg_dsn: str, tmp_path: Path
) -> None:
    role = f"reader_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            f"""CREATE ROLE {role} LOGIN PASSWORD 'r';
                GRANT SELECT ON users, users_v, "Mixed" TO {role}; GRANT SELECT (id) ON keys_only TO {role};
                CREATE SCHEMA crm; CREATE TABLE crm.customers (id int PRIMARY KEY, region text)"""
        )
        conn.execute(
            f"""CREATE SCHEMA "Sales"; GRANT USAGE ON SCHEMA "Sales" TO {role};
                CREATE TABLE "Sales".orders (region text, id int, PRIMARY KEY (region, id));
                CREATE MATERIALIZED VIEW "Sales".totals AS SELECT 1 AS n;
                GRANT SELECT ON ALL TABLES IN SCHEMA "Sales" TO {role}"""
        )
    (tmp_path / "dsn").write_text(pg_dsn.replace("postgres:test@", f"{role}:r@"))
    engine = ENGINES["postgresql"]
    async with engine.snapshot(targets.get("db-a"), timeout_s=30) as session:
        own = await engine.relations(session, None)  # the session's current schema
        sales = await engine.relations(session, "SALES")  # as spelled, else folded: neither is "Sales"
        spelled = await engine.relations(session, "Sales")
        hidden = await engine.relations(session, "crm")  # no USAGE
        users = await engine.describe(session, None, "users")
        orders = await engine.describe(session, "Sales", "orders")
        read = await figures.read(
            engine, session, figures.classed(USERS, users), [("rows", None), ("nulls", "email")],
            duplicates=True, distinct_max_rows=9, timeout_s=30,
        )  # fmt: skip
    assert own == [("Mixed", "table"), ("keys_only", "table"), ("users", "table"), ("users_v", "view")]
    assert (sales, hidden) == ([], [])
    assert spelled == [("orders", "table"), ("totals", "materialized view")]
    assert users.facts["key"] == ["userid"] and orders.facts["key"] == ["region", "id"]  # in the key's order
    assert (read["rows"], read["duplicate_keys"], read["failed"]) == (2, 0, {})


async def test_a_partitioned_table_is_listed_once_and_reads_every_partition(
    targets: TargetRegistry, pg_dsn: str
) -> None:
    """MySQL and Oracle list a partitioned table once: a PostgreSQL partition, one partitioned again
    among them, is not listed beside its table, whose statements read its rows."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            """CREATE SCHEMA ledger;
               CREATE TABLE ledger.entries (id int, booked date, amount numeric(12, 2),
                                            PRIMARY KEY (id, booked)) PARTITION BY RANGE (booked);
               CREATE TABLE ledger.entries_2026_01 PARTITION OF ledger.entries
                   FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
               CREATE TABLE ledger.entries_2026_02 PARTITION OF ledger.entries
                   FOR VALUES FROM ('2026-02-01') TO ('2026-03-01') PARTITION BY RANGE (id);
               CREATE TABLE ledger.entries_2026_02_low PARTITION OF ledger.entries_2026_02
                   FOR VALUES FROM (0) TO (100);
               CREATE TABLE ledger.plain (id int PRIMARY KEY);
               INSERT INTO ledger.entries VALUES (1, '2026-01-05', 1.50), (2, '2026-02-05', 2.00)"""
        )
    engine = ENGINES["postgresql"]
    async with engine.snapshot(targets.get("db-a"), timeout_s=30) as session:
        listed = await engine.relations(session, "ledger")
        entries = await engine.describe(session, "ledger", "entries")
        side_ = side("entries", "id", amount="amount") | {
            "schema": "ledger",
            "keys": ["id", "booked"],
            "key_maps": [None] * 2,
        }
        read = await figures.read(
            engine, session, figures.classed(side_, entries), [("rows", None), ("sum", "amount")],
            duplicates=True, distinct_max_rows=9, timeout_s=30,
        )  # fmt: skip
    assert listed == [("entries", "table"), ("plain", "table")]
    assert (entries.facts["kind"], entries.facts["key"]) == ("table", ["id", "booked"])
    assert (read["rows"], read["figures"]["amount"]["sum"], read["duplicate_keys"]) == (2, "3.50", 0)
