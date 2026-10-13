"""Table pre-checks end to end (docs/mvp/PRECHECKS.md §13), on the local images: a pair of tables with a
repeated key, rows without a key, a filter, an empty text Oracle stores as NULL, CHAR padding, a float and a
typeMap column, between PostgreSQL, MySQL and Oracle and on each against itself, figures and verdicts exact;
two schemas of one database read as one target; two schemas paired by name, a name of one side alone and two
names alike ignoring case, then a draft that validates and runs; and a role's own limits. With DS_PERF=1 the
reads of §12 are timed.
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient

from deltasleuth import prechecks
from deltasleuth.api import create_app
from deltasleuth.config import load_settings
from deltasleuth.core.spec import compile_spec, validate_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.e2e.conftest import Stack, both, finish, store_spec, table_spec
from tests.unit.runner.prechecking import ended, pairs

pytestmark = [pytest.mark.e2e, pytest.mark.mysql]
oracle = pytest.mark.oracle  # a pairing that also needs the Oracle server

# Side a: users, read where userid < 5 (row 5 is left out). Side b: a view over a table without a key, which
# repeats key 2, holds a row without a key, and spells c@x.com in upper case. A {t} is a moment.
ROWS_A = [
    "1, 'a@x.com', 'ab', 'n1', 1.50, 1.5, {t1}", "2, 'b@x.com', 'cd', '', 2.00, 2.5, {t2}",
    "3, 'c@x.com', NULL, NULL, 3.25, NULL, NULL", "4, 'd@x.com', '  ', 'n4', 4.00, 4.5, {t4}",
    "5, 'e@x.com', 'gh', 'n5', 5.00, 5.5, {t4}",
]  # fmt: skip
ROWS_B = [
    "1, 'a@x.com', 'ab', 'n1', 1.50, 1.5, {t1}", "2, 'b@x.com', 'cd', '', 2.00, 2.5, {t2}",
    "2, 'b@x.com', 'cd', 'n2', 2.00, 2.5, {t2}", "3, 'C@x.com', NULL, NULL, 3.25, NULL, NULL",
    "4, 'd@x.com', '  ', 'n4', 4.00, 4.5, {t4}", "NULL, 'x@x.com', 'zz', 'nx', 9.00, 1.0, {t4}",
]  # fmt: skip
# Each engine's types of (key, email, code, note, amount, rate, joined), and how it writes a moment.
TYPES = {
    "postgresql": ("int", "text", "char(4)", "text", "numeric(10, 2)", "double precision", "timestamp"),
    "mysql": ("INT", "VARCHAR(100)", "CHAR(4)", "VARCHAR(100)", "DECIMAL(10, 2)", "DOUBLE", "DATETIME"),
    "oracle": (
        "NUMBER(10)", "VARCHAR2(100)", "CHAR(4)", "VARCHAR2(100)", "NUMBER(10, 2)", "BINARY_DOUBLE", "DATE",
    ),
}  # fmt: skip
MOMENTS = {"t1": "2024-01-01 10:00:00", "t2": "2024-01-02 10:00:00", "t4": "2024-01-04 10:00:00"}


def _seed(engine: str, table: str, key: str, rows: list[str], primary: bool) -> str:
    """A table of ``rows`` on ``engine``, one INSERT a row (Oracle takes no list)."""
    types = TYPES[engine]
    names = (key, "email", "code", "note", "amount", "rate", "joined")
    declared = [f"{name} {kind}" for name, kind in zip(names, types, strict=True)]
    declared[0] += " PRIMARY KEY" * primary
    moment = "TIMESTAMP '{}'" if engine == "oracle" else "'{}'"
    values = [row.format(**{name: moment.format(text) for name, text in MOMENTS.items()}) for row in rows]
    inserts = "".join(f"INSERT INTO {table} VALUES ({row});\n" for row in values)  # noqa: S608 (constants)
    return f"CREATE TABLE {table} ({', '.join(declared)});\n{inserts}"


USERS = {engine: _seed(engine, "users", "userid", ROWS_A, True) for engine in TYPES}
VIEW = "CREATE VIEW users_v AS SELECT * FROM users_raw"
USERS_V = {engine: _seed(engine, "users_raw", "user_id", ROWS_B, False) + VIEW for engine in TYPES}
COLUMNS = ["email", "code", "note", "amount", "rate", "joined"]
SPEC = table_spec(
    sides={
        "a": {
            "role": "legacy", "target": "db-a", "typeMap": {"joined": {"as": "string"}},
            "table": {"name": "users", "keyColumns": ["userid"], "where": "userid < 5", "columns": COLUMNS},
        },
        "b": {
            "role": "new", "target": "db-b", "typeMap": {"joined": {"as": "string"}},
            "table": {"name": "users_v", "keyColumns": ["user_id"], "columns": COLUMNS},
        },
    },
    security={"unlisted": "tokenize", "clear": ["$.amount", "$.email", "$.code"]},
)  # fmt: skip
# Under side a's filter a protected column's counts beside its NULLs are verdicts (PRECHECKS.md §6).
WITHHELD = "figures withheld: side a declares a filter and $.note is protected by the spec's security policy"
# What each side is read as, by its engine's kind (Oracle stores '' as NULL: its texts have none).
TEXTS = {
    ("a", False): {
        "email": (0, 0, [7, 7], 4), "code": (1, 1, [0, 2], 3), "note": (1, 1, [0, 2], 3),
    },
    ("a", True): {
        "email": (0, 0, [7, 7], 4), "code": (1, 1, [0, 2], 3), "note": (2, 0, [2, 2], 2),
    },
    ("b", False): {
        "email": (0, 0, [7, 7], 5), "code": (1, 1, [0, 2], 4), "note": (1, 1, [0, 2], 5),
    },
    ("b", True): {
        "email": (0, 0, [7, 7], 5), "code": (1, 1, [0, 2], 4), "note": (2, 0, [2, 2], 4),
    },
}  # fmt: skip


def _verdict(a: Any, b: Any) -> str:
    return "same" if a == b else "differs"


def _expected(engine_a: str, engine_b: str) -> dict[str, Any]:
    """The pair's columns as a pre-check at level distinct reads them: each figure's two values where they
    are kept, and its verdict."""
    oracles = {"a": engine_a == "oracle", "b": engine_b == "oracle"}
    found: dict[str, Any] = {}
    for column in ("email", "code", "note"):
        a, b = TEXTS["a", oracles["a"]][column], TEXTS["b", oracles["b"]][column]
        reason = None
        for side, other in (("a", "b"), ("b", "a")):
            if column == "note" and oracles[side] and not oracles[other]:  # protected: no count told
                bridge = f"declare typeMap {{note: {{as: string, emptyAsNull: true}}}} on side {other}"
                reason = (
                    f"Oracle (side {side}) stores as NULL any empty string side {other} holds: {bridge} to"
                    " compare them as equal"
                )
        found[column] = {
            "nulls": {"a": a[0], "b": b[0], "verdict": _verdict(a[0], b[0]), "reason": reason},
            "empty": {"a": a[1], "b": b[1], "verdict": _verdict(a[1], b[1])},
            "length": {"a": a[2], "b": b[2], "verdict": _verdict(a[2], b[2])},
            "distinct": {"a": a[3], "b": b[3], "verdict": _verdict(a[3], b[3])},
        }
        if column == "note":  # protected: its verdicts alone
            for figure in ("empty", "length", "distinct"):
                found[column][figure] = {"verdict": found[column][figure]["verdict"], "reason": WITHHELD}
    found["amount"] = {
        "nulls": {"a": 0, "b": 0, "verdict": "same", "reason": None},
        "sum": {"verdict": "differs", "reason": "figures withheld: side a declares a filter"},
        "min": {"verdict": "same"}, "max": {"verdict": "differs"},
        "distinct": {"a": 4, "b": 5, "verdict": "differs"},
    }  # fmt: skip
    for column in ("rate", "joined"):
        found[column] = {"nulls": {"a": 1, "b": 1, "verdict": "same", "reason": None}}
    return found


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("postgresql", "postgresql"),
        ("postgresql", "mysql"),
        ("mysql", "mysql"),
        pytest.param("postgresql", "oracle", marks=oracle),
        pytest.param("mysql", "oracle", marks=oracle),
        pytest.param("oracle", "oracle", marks=oracle),
    ],
    ids=["postgresql-postgresql", "postgresql-mysql", "mysql-mysql", "postgresql-oracle", "mysql-oracle",
         "oracle-oracle"],
)  # fmt: skip
async def test_a_pair_across_engines_reads_exactly_its_figures(
    engines: Callable[..., Stack], a: str, b: str
) -> None:
    async with engines((a, USERS[a]), (b, USERS_V[b])).context() as ctx:
        spec_id = await store_spec(ctx, SPEC)
        made = await prechecks.create(ctx, {"spec_ids": [spec_id], "level": "distinct"}, TESTER)
        await ended(ctx, made["precheck_id"], timeout=180)
        [pair] = await pairs(ctx, made["precheck_id"])
    result = pair["result"]
    figures = {column: found["figures"] for column, found in result["columns"].items()}
    assert (pair["verdict"], pair["rows_a"], pair["rows_b"], result["refused"]) == ("rows_differ", 4, 6, [])
    assert result["keys"]["a"] | {"primary": None} == {
        "rows": 4, "null_keys": 0, "duplicate_keys": 0, "rows_in_duplicates": 0, "primary": None,
    }  # fmt: skip
    assert result["keys"]["b"] == {
        "rows": 6, "null_keys": 1, "duplicate_keys": 1, "rows_in_duplicates": 2, "primary": None,
    }  # fmt: skip
    assert figures == _expected(a, b)
    assert result["lines"][:3] == [
        "rows differ: side a 4, side b 6 (2 fewer on side a)",
        "keys: 1 row(s) of side b have no key (NULL, or its key map gives none): a scan does not compare"
        " them",
        "keys: 1 key(s) of side b are held by more than one row (2 rows): per key they read"
        " identity_ambiguous",
    ]
    assert result["columns"]["rate"]["reason"] == "a float sum depends on the order rows are added in"
    assert result["columns"]["joined"]["reason"] == "typeMap converts this column per row"
    assert [result["sides"][side]["engine"] for side in "ab"] == [a, b]
    assert result["lines"][-1].endswith(": not read at one moment")


SCHEMAS = """
    CREATE SCHEMA crm; CREATE SCHEMA app;
    CREATE TABLE crm.accounts (id int PRIMARY KEY, email text, balance numeric(12, 2));
    CREATE TABLE app.accounts (id int PRIMARY KEY, email text, balance numeric(12, 2));
    INSERT INTO crm.accounts SELECT g, 'u' || g || '@x.com', g * 1.25 FROM generate_series(1, 50) g;
    INSERT INTO app.accounts SELECT * FROM crm.accounts;
    CREATE TABLE crm."Cards" (id int PRIMARY KEY); CREATE TABLE crm.cards (id int PRIMARY KEY);
    CREATE TABLE app.cards (id int PRIMARY KEY);
    CREATE TABLE crm.ledger (id int PRIMARY KEY); CREATE VIEW app.loans AS SELECT 1 AS id;
    CREATE TABLE crm.contacts (id int PRIMARY KEY, "Email" text, email text, amount numeric(10, 2));
    INSERT INTO crm.contacts VALUES (1, 'a@x.com', NULL, 1.50);
    CREATE TABLE app.contacts (id int PRIMARY KEY, email text, amount numeric(10, 2));
    INSERT INTO app.contacts VALUES (1, 'a@x.com', 1.50);
"""


@pytest.fixture
def schemas(make_stack: Callable[..., Stack], databases: Callable[..., str], tmp_path: Any) -> Stack:
    """One PostgreSQL database holding schemas crm and app, read by the target db-a alone."""
    (tmp_path / "dsn_a").write_text(databases("schemas", SCHEMAS))
    return make_stack(yaml.safe_dump({"targets": {"db-a": {"dsn_ref": "file:dsn_a", "rate_per_s": 1000}}}))


async def test_two_schemas_of_one_database_are_read_as_one_target(schemas: Stack) -> None:
    accounts = {"name": "accounts", "keyColumns": ["id"]}
    spec = table_spec(
        sides={
            "a": {"role": "legacy", "target": "db-a", "table": accounts | {"schema": "crm"}},
            "b": {"role": "new", "target": "db-a", "table": accounts | {"schema": "app"}},
        },
        security={"unlisted": "clear"},
    )
    async with schemas.context() as ctx:
        made = await prechecks.create(ctx, {"spec_ids": [await store_spec(ctx, spec)]}, TESTER)
        await ended(ctx, made["precheck_id"])
        [pair] = await pairs(ctx, made["precheck_id"])
    result = pair["result"]
    assert pair["verdict"] == "no_difference_observed" and result["apart_s"] is not None
    assert [result["sides"][side]["relation"] for side in "ab"] == ["crm.accounts", "app.accounts"]
    assert result["columns"]["balance"]["figures"]["sum"] == {
        "a": "1593.75", "b": "1593.75", "verdict": "same", "reason": None,
    }  # fmt: skip


async def test_two_schemas_paired_by_name_then_a_draft_that_validates_and_runs(schemas: Stack) -> None:
    body = {
        "a": {"target": "db-a", "schema": "crm"},
        "b": {"target": "db-a", "schema": "app"},
        "level": "columns",
    }
    async with schemas.context() as ctx:
        made = await prechecks.catalogue(ctx, body, TESTER)
        await ended(ctx, made["precheck_id"])
        found = {pair["label"]: pair for pair in await pairs(ctx, made["precheck_id"])}
        text, issues = await prechecks.draft(ctx, made["precheck_id"], found["accounts"]["n"])
        draft = parse_yaml_text(text)
        # Valid, with one warning: no catalogue gives a key pattern, and identifier keys need one for a pack.
        assert validate_spec(draft, targets=ctx.targets) == issues
        assert [(issue.severity, issue.code) for issue in issues] == [("warning", "key_pattern_empty")]
        async with ctx.pool.connection() as conn:
            spec_id = await repo.save_spec(
                conn, compile_spec(draft, targets=ctx.targets), created_by=TESTER.actor
            )
        from deltasleuth.runner.runs import create_run

        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
    assert {label: pair["verdict"] for label, pair in found.items()} == {
        "accounts": "no_difference_observed", "cards": "unpaired", "contacts": "columns_not_compared",
        "ledger": "unpaired", "loans": "unpaired",
    }  # fmt: skip
    alike = "and another column of side a both compare as email: list each in a spec under a name of its own"
    assert found["contacts"]["line"] == f"2 columns would not be compared: Email {alike}; email {alike}"
    assert found["cards"]["line"] == (
        "two tables of side a are named alike ignoring case (Cards, cards): compare them in a spec"
    )
    assert found["loans"]["line"] == "no table on side a is named loans (side b: app.loans; paired by name)"
    assert draft["sides"]["a"]["table"] == {
        "schema": "crm", "name": "accounts", "keyColumns": ["id"], "columns": ["balance", "email"],
    }  # fmt: skip
    assert run["status"] == "completed" and run["scan"]["matched"] == 50


async def test_a_role_s_own_limits(
    make_stack: Callable[..., Stack], databases: Callable[..., str], tmp_path: Any
) -> None:
    """A role's own statement_timeout is the pre-check's (its sessions set precheck.statement_timeout_s),
    and its temp_file_limit ends a sort: the distinct values are not counted, what was read before stays."""
    seed = """
        CREATE TABLE wide (id int PRIMARY KEY, v text);
        INSERT INTO wide SELECT g, md5(g::text) || md5((g + 1)::text) FROM generate_series(1, 200000) g;
    """
    dsn = databases("limits", seed)
    role = "limited_" + tmp_path.name[-8:].replace("-", "_").lower()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'r'")
        conn.execute(f"GRANT SELECT ON wide TO {role}")
        for setting in ("statement_timeout = '1ms'", "work_mem = '64kB'", "temp_file_limit = '64kB'"):
            conn.execute(f"ALTER ROLE {role} SET {setting}")
    (tmp_path / "dsn_a").write_text(dsn.replace("postgres:test@", f"{role}:r@"))
    stack = make_stack(yaml.safe_dump({"targets": {
        side: {"dsn_ref": "file:dsn_a", "rate_per_s": 1000} for side in ("db-a", "db-b")}}))  # fmt: skip
    wide = {"name": "wide", "keyColumns": ["id"]}
    spec = table_spec(sides={"a": {"role": "legacy", "target": "db-a", "table": wide},
                             "b": {"role": "new", "target": "db-b", "table": wide}})  # fmt: skip
    async with stack.context() as ctx:
        made = await prechecks.create(
            ctx, {"spec_ids": [await store_spec(ctx, spec)], "level": "distinct"}, TESTER
        )
        await ended(ctx, made["precheck_id"])
        [pair] = await pairs(ctx, made["precheck_id"])
    result = pair["result"]
    assert (pair["verdict"], pair["rows_a"], pair["rows_b"]) == ("not_read", 200000, 200000)
    assert result["sides"]["a"]["failed"] == {"duplicates": "temp_space"}
    assert pair["line"] == "duplicate keys not counted: side a ran out of temporary space (53400)"
    assert result["columns"]["v"]["figures"]["nulls"] == {"a": 0, "b": 0, "verdict": "same", "reason": None}


# --- performance (PRECHECKS.md §12): DS_PERF=1 runs these; §12 publishes what they measured ---------------

perf = pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
CLEAR = table_spec(sides=both({"name": "users", "keyColumns": ["userid"]}), security={"unlisted": "clear"})
# Each engine's types of (key, email, name, code, amount, visits, joined).
WIDE = {
    "postgresql": ("bigint", "text", "text", "char(4)", "numeric(12, 2)", "int", "timestamp"),
    "mysql": ("BIGINT", "VARCHAR(100)", "VARCHAR(100)", "CHAR(4)", "DECIMAL(12, 2)", "INT", "DATETIME"),
    "oracle": (
        "NUMBER(19)", "VARCHAR2(100)", "VARCHAR2(100)", "CHAR(4)", "NUMBER(12, 2)", "NUMBER(10)", "TIMESTAMP",
    ),
}  # fmt: skip


def people(engine: str, rows: int) -> str:
    """The seed of ``rows`` users (a multiple of 1,000) on ``engine``: a key and six compared columns, two
    texts, a char, a decimal, an integer and a timestamp."""
    names = ("userid", "email", "name", "code", "amount", "visits", "joined")
    declared = [f"{name} {kind}" for name, kind in zip(names, WIDE[engine], strict=True)]
    declared[0] += " PRIMARY KEY"
    table = f"CREATE TABLE users ({', '.join(declared)});"
    if engine == "postgresql":
        return f"""{table}
            INSERT INTO users SELECT n, 'user' || n || '@example.com', 'Name ' || n,
                lpad((n % 10000)::text, 4, '0'), n * 1.25, n % 1000,
                timestamp '2024-01-01' + n * interval '1 second'
            FROM generate_series(1, {rows}) n;
        """  # noqa: S608 (rows is a number)
    if engine == "mysql":
        return f"""
            SET SESSION cte_max_recursion_depth = 100000;
            CREATE TABLE g (n INT PRIMARY KEY);
            INSERT INTO g WITH RECURSIVE s (n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM s WHERE n < 9999)
            SELECT n FROM s;
            {table}
            INSERT INTO users SELECT n, CONCAT('user', n, '@example.com'), CONCAT('Name ', n),
                LPAD(n % 10000, 4, '0'), n * 1.25, n % 1000,
                TIMESTAMP '2024-01-01 00:00:00' + INTERVAL n SECOND
            FROM (
                SELECT a.n * 1000 + b.n + 1 AS n FROM g a JOIN g b WHERE a.n < {rows // 1000} AND b.n < 1000
            ) m;
        """  # noqa: S608 (rows is a number)
    return f"""{table}
        INSERT INTO users SELECT n, 'user' || n || '@example.com', 'Name ' || n, LPAD(MOD(n, 10000), 4, '0'),
            n * 1.25, MOD(n, 1000), TIMESTAMP '2024-01-01 00:00:00' + NUMTODSINTERVAL(n, 'SECOND')
        FROM (
            SELECT (a.n - 1) * 1000 + b.n AS n
            FROM (SELECT LEVEL AS n FROM DUAL CONNECT BY LEVEL <= {rows // 1000}) a,
                 (SELECT LEVEL AS n FROM DUAL CONNECT BY LEVEL <= 1000) b
        )
    """  # noqa: S608 (rows is a number)


async def _timed(
    stack: Stack, spec: dict[str, Any], level: str, timeout: float = 600
) -> tuple[repo.Row, float]:
    """A pre-check of ``spec`` at ``level``, from its creation until the worker ended it; its one pair and
    the seconds."""
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, spec)
        started = time.monotonic()
        made = await prechecks.create(ctx, {"spec_ids": [spec_id], "level": level}, TESTER)
        await ended(ctx, made["precheck_id"], timeout=timeout)
        elapsed = time.monotonic() - started
        [pair] = await pairs(ctx, made["precheck_id"])
    return pair, elapsed


class _Sampled:
    """A thread asking the PostgreSQL server ``dsn`` ``query`` every ``every`` seconds until it is stopped;
    each answer's row."""

    def __init__(self, dsn: str, query: str, every: float = 0.02) -> None:
        self.rows: list[tuple[Any, ...]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._ask, args=(dsn, query, every), daemon=True)

    def _ask(self, dsn: str, query: str, every: float) -> None:
        with psycopg.connect(dsn, autocommit=True) as conn:
            while not self._stop.is_set():
                self.rows.append(conn.execute(query).fetchone() or ())  # type: ignore[arg-type]
                self._stop.wait(every)

    def __enter__(self) -> _Sampled:
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        self._thread.join(10)


@perf
@pytest.mark.parametrize(
    ("a", "b", "limit"),
    [
        ("postgresql", "postgresql", 10),
        ("postgresql", "mysql", 20),
        pytest.param("postgresql", "oracle", 20, marks=oracle),
        pytest.param("mysql", "oracle", 20, marks=oracle),
    ],
)
async def test_columns_a_million_rows(engines: Callable[..., Stack], a: str, b: str, limit: int) -> None:
    rows = 1_000_000
    pair, elapsed = await _timed(engines((a, people(a, rows)), (b, people(b, rows))), CLEAR, "columns")
    print(f"\n{rows} rows a side, {a} against {b}, level columns: {elapsed:.1f} s ({pair['verdict']})")
    assert (pair["rows_a"], pair["rows_b"], len(pair["result"]["columns"])) == (rows, rows, 6)
    assert pair["verdict"] in ("no_difference_observed", "schema_differs"), pair["line"]
    assert elapsed < limit


@pytest.fixture(scope="module")
def ten_million(pg_server: str) -> Iterator[list[str]]:
    """Two PostgreSQL databases of 10M users each, seeded at once, for this module's tests; their DSNs."""
    names = [f"t_ten_million_{side}_{os.getpid()}" for side in "ab"]

    def seeded(name: str) -> str:
        with psycopg.connect(pg_server, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')  # type: ignore[arg-type]
        dsn = pg_server.rsplit("/", 1)[0] + f"/{name}"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(people("postgresql", 10_000_000))  # type: ignore[arg-type]
            conn.execute("VACUUM ANALYZE users")
        return dsn

    with ThreadPoolExecutor(2) as pool:
        yield list(pool.map(seeded, names))
    with psycopg.connect(pg_server, autocommit=True) as conn:
        for name in names:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # type: ignore[arg-type]


def _two(make_stack: Callable[..., Stack], tmp_path: Path, dsns: list[str]) -> Stack:
    """A stack whose targets db-a and db-b are the databases ``dsns``."""
    for side, dsn in zip("ab", dsns, strict=True):
        (tmp_path / f"dsn_{side}").write_text(dsn)
    targets = {f"db-{side}": {"dsn_ref": f"file:dsn_{side}", "rate_per_s": 1000} for side in "ab"}
    return make_stack(yaml.safe_dump({"targets": targets}))


@perf
async def test_columns_ten_million_rows(
    make_stack: Callable[..., Stack], ten_million: list[str], tmp_path: Path
) -> None:
    pair, elapsed = await _timed(_two(make_stack, tmp_path, ten_million), CLEAR, "columns")
    print(f"\n10000000 rows a side, PostgreSQL, level columns: {elapsed:.1f} s ({pair['verdict']})")
    assert (pair["rows_a"], pair["rows_b"]) == (10_000_000, 10_000_000)
    assert pair["verdict"] == "no_difference_observed"
    assert elapsed < 30


@perf
async def test_distinct_ten_million_rows(
    make_stack: Callable[..., Stack], ten_million: list[str], pg_server: str, tmp_path: Path
) -> None:
    """Both sides' distinct counts and duplicate keys sort on one server at once: its temporary files'
    size, sampled every 0.1 s (the peak), and what its databases' statistics say was written."""
    written = "SELECT sum(temp_bytes) FROM pg_stat_database"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        before = conn.execute(written).fetchone()[0]  # type: ignore[index]
    with _Sampled(pg_server, "SELECT coalesce(sum(size), 0) FROM pg_ls_tmpdir()", 0.1) as sampled:
        pair, elapsed = await _timed(_two(make_stack, tmp_path, ten_million), CLEAR, "distinct")
    with psycopg.connect(pg_server, autocommit=True) as conn:
        total = conn.execute(written).fetchone()[0] - before  # type: ignore[index]
    peak = max(row[0] for row in sampled.rows)
    print(
        f"\n10000000 rows a side, PostgreSQL, level distinct: {elapsed:.1f} s ({pair['verdict']}); temporary"
        f" files at most {peak / 2**20:.0f} MiB at once ({len(sampled.rows)} samples), {total / 2**20:.0f}"
        " MiB written in all"
    )
    assert (pair["rows_a"], pair["verdict"], pair["result"]["skipped"]) == (
        10_000_000, "no_difference_observed", [],
    )  # fmt: skip
    assert pair["result"]["keys"]["a"]["duplicate_keys"] == 0
    assert elapsed < 120


def _tables(count: int, columns: str, rows: str = "") -> str:
    """A seed of tables t1 to t``count`` of ``columns``, each filled by ``rows`` (a SELECT over g)."""
    filled = f"EXECUTE format('INSERT INTO t%s SELECT {rows} FROM generate_series(1, 100) g', i);"  # noqa: S608
    return f"""DO $$ BEGIN FOR i IN 1..{count} LOOP
        EXECUTE format('CREATE TABLE t%s ({columns})', i); {filled if rows else ""}
    END LOOP; END $$"""


@perf
async def test_three_hundred_pairs_schema(
    make_stack: Callable[..., Stack], databases: Callable[..., str], pg_server: str, tmp_path: Path
) -> None:
    """300 stored specs at level schema, each a table of each of two targets: one session a target at
    most, as pg_stat_activity shows it every 20 ms."""
    count = 300
    seed = _tables(count, "id int PRIMARY KEY, email text, amount numeric(12, 2)")
    dsns = [databases(side, seed) for side in "ab"]
    query = psycopg.sql.SQL(
        "SELECT count(*) FILTER (WHERE datname = {}), count(*) FILTER (WHERE datname = {})"
        " FROM pg_stat_activity WHERE backend_type = 'client backend'"
    ).format(*(psycopg.sql.Literal(dsn.rsplit("/", 1)[1]) for dsn in dsns))
    async with _two(make_stack, tmp_path, dsns).context() as ctx:
        spec_ids = []
        for n in range(1, count + 1):
            named = {"name": f"pair-{n:03d}", "version": "1.0.0", "owner": "tests"}
            sides = both({"name": f"t{n}", "keyColumns": ["id"]})
            spec_ids.append(await store_spec(ctx, table_spec(sides=sides, metadata=named)))
        with _Sampled(pg_server, query.as_string()) as sampled:
            started = time.monotonic()
            made = await prechecks.create(ctx, {"spec_ids": spec_ids, "level": "schema"}, TESTER)
            await ended(ctx, made["precheck_id"], timeout=300)
            elapsed = time.monotonic() - started
        verdicts = {pair["verdict"] for pair in await pairs(ctx, made["precheck_id"])}
    most = [max(row[side] for row in sampled.rows) for side in (0, 1)]
    print(
        f"\n{count} pairs at level schema: {elapsed:.1f} s; at most {most} sessions on targets a and b"
        f" ({len(sampled.rows)} samples)"
    )
    assert verdicts == {"no_difference_observed"}
    assert most == [1, 1]
    assert elapsed < 60


@perf
async def test_get_five_hundred_pairs(
    make_stack: Callable[..., Stack], databases: Callable[..., str], tmp_path: Path
) -> None:
    """GET /v1/prechecks/{id} with its 500 pairs, each two tables of 20 columns read at level columns (two
    schemas paired by name), asked 20 times: it selects no result."""
    count = 500
    kinds = ("int", "text", "numeric(12, 2)", "timestamp")
    rendered = ("g * 7", "''v'' || g", "g * 1.25", "timestamp ''2024-01-01'' + g * interval ''1 day''")
    columns = ", ".join(["id int PRIMARY KEY", *(f"c{i} {kinds[i % 4]}" for i in range(19))])
    rows = ", ".join(["g", *(rendered[i % 4] for i in range(19))])
    stack = _two(make_stack, tmp_path, [databases(side, _tables(count, columns, rows)) for side in "ab"])
    body = {"a": {"target": "db-a"}, "b": {"target": "db-b"}, "level": "columns"}
    async with stack.context() as ctx:
        started = time.monotonic()
        made = await prechecks.catalogue(ctx, body, TESTER)
        await ended(ctx, made["precheck_id"], timeout=600)
        read = time.monotonic() - started
        verdicts = {pair["verdict"] for pair in await pairs(ctx, made["precheck_id"])}
    assert verdicts == {"no_difference_observed"}
    local = {"base_url": "http://127.0.0.1", "client": ("127.0.0.1", 50000)}
    with TestClient(create_app(load_settings(stack.settings)), **local) as client:
        path = f"/v1/prechecks/{made['precheck_id']}?limit={count}"
        assert len(client.get(path).json()["items"]) == count  # and warm
        took = []
        for _ in range(20):
            began = time.perf_counter()
            assert client.get(path).status_code == 200
            took.append(time.perf_counter() - began)
    median = statistics.median(took)
    print(
        f"\n{count} pairs read at level columns in {read:.1f} s; GET /v1/prechecks/{{id}} median"
        f" {median * 1000:.0f} ms, max {max(took) * 1000:.0f} ms (20 asked)"
    )
    assert median < 0.3
