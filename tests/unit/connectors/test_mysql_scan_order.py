"""A MySQL scan's key order. A key column whose own order is its key text's code-point order, with no
padding, orders and bounds the scan itself, so the primary key or unique index that holds it streams the rows
and resumes by range, with no sort: a VARCHAR in utf8mb4_0900_bin, which compares a text's UTF-8 bytes as they
are, and a BINARY or VARBINARY, whose bytes sort as their key text (\\x and lower-case hexadecimal digits)
does. A key part the plan orders as an integer orders and bounds the scan by its column, also beside such a
text, and past a bound no column of its type holds. Any other key, and one whose collation the run's plan did
not record or this pass's catalogue names otherwise, keeps the order of its key text's bytes, which the
server sorts."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pymysql
import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pymysql.constants import CLIENT

from deltasleuth.connectors.tables import ENGINES, Plan, plan
from deltasleuth.connectors.tables import mysql as engine_module
from deltasleuth.core.spec import load_targets
from tests.conftest import MYSQL_PASSWORD, MySQLServer

from .test_postgres import compiled

MYSQL = ENGINES["mysql"]
UTF8_BIN = ["utf8mb4", "utf8mb4_0900_bin"]  # NO PAD: a text's UTF-8 bytes, compared as they are
BINARY = ["binary", "binary"]  # a binary string's, which the catalogue leaves NULL
BY_BYTES = "ORDER BY CAST("  # the order of the key texts' bytes, which the server sorts
Key = tuple[str, str, str, "list[str] | None"]  # a key column, its class, declared type and collation
VARCHAR: Key = ("k", "text", "varchar(20)", UTF8_BIN)
VARBINARY: Key = ("b", "bytes", "varbinary(4)", BINARY)


def side(
    *keys: Key,
    seen: Any = True,
    key_maps: Any = None,
    schema: str = "s",
    name: str = "keys",
    integers: list[int] | None = None,
) -> Plan:
    """A MySQL side's plan of ``keys`` and one compared integer ``n``, each key's collation as its plan
    recorded it, and as this pass's catalogue names it (``seen``: True, the same; None, not told; else what
    it names), as a scan pass tells its engine."""
    columns = {column: (cls, declared) for column, cls, declared, _ in keys} | {"n": ("integer", "int")}
    recorded = {column: collation for column, _, _, collation in keys if collation}
    found: Plan = {
        "schema": schema, "name": name, "keys": [key[0] for key in keys],
        "key_maps": key_maps or [None] * len(keys), "columns": {"n": "n"}, "where": None, "types": {},
        "digest": [], "engine": "mysql", "classes": {c: cls for c, (cls, _) in columns.items()},
        "declared": {c: declared for c, (_, declared) in columns.items()},
    }  # fmt: skip
    if recorded:
        found["collations"] = recorded
    if seen is not None:
        found["seen_collations"] = recorded if seen is True else seen
    if integers:
        found["integers"] = integers
    return found


def _order(statement: str) -> str:
    return statement[statement.rindex("ORDER BY") :]


@pytest.mark.parametrize(
    ("keys", "seen", "native"),
    [
        ([VARCHAR], True, True),
        ([("k", "bytes", "varbinary(16)", BINARY)], True, True),
        ([("k", "bytes", "binary(16)", BINARY)], True, True),  # its zero padding is its value's, read back
        ([VARCHAR, VARBINARY], True, True),
        # PAD SPACE: 'a' and 'a ' compare equal, and a\t sorts before a.
        ([("k", "text", "varchar(20)", ["utf8mb4", "utf8mb4_bin"])], True, False),
        ([("k", "text", "varchar(20)", ["utf8mb4", "utf8mb4_0900_ai_ci"])], True, False),
        ([("k", "text", "varchar(20)", ["latin1", "latin1_bin"])], True, False),
        ([("k", "char", "char(4)", UTF8_BIN)], True, False),  # a MEMORY table's index compares its padding
        ([("k", "text", "enum", UTF8_BIN)], True, False),  # ordered by its members' places
        ([("k", "integer", "int", None)], True, False),  # 10 sorts before 9 as text
        ([VARCHAR, ("i", "integer", "int", None)], True, False),
        ([VARCHAR], None, False),  # this pass did not say what its catalogue names
        ([VARCHAR], {"k": ["utf8mb4", "utf8mb4_0900_ai_ci"]}, False),  # the column changed since the plan
        ([("k", "text", "varchar(20)", None)], {"k": UTF8_BIN}, False),  # a plan made before collations
    ],
)
def test_only_keys_whose_own_order_is_their_text_s_order_the_scan(
    keys: list[Key], seen: Any, native: bool
) -> None:
    order = _order(engine_module.scan_sql(side(*keys, seen=seen), None))
    expected = "ORDER BY " + ", ".join(f"t.`{key[0]}`" for key in keys)
    assert order == expected if native else order.startswith(BY_BYTES), order


def test_a_key_read_through_its_key_map_keeps_the_order_of_its_text() -> None:
    padded = {"kind": "prefix_pad", "prefix": "ORG-", "width": 6, "sideType": "string"}
    assert _order(engine_module.scan_sql(side(VARCHAR, key_maps=[padded]), None)).startswith(BY_BYTES)


@pytest.mark.parametrize(
    ("keys", "after", "bound"),
    [
        ([VARCHAR], ["é"], "((t.`k` > _utf8mb4 X'c3a9'))"),
        ([VARCHAR], ["\\x00"], "((t.`k` > _utf8mb4 X'5c783030'))"),  # any text: a text column's value
        ([VARBINARY], ["\\x00ff"], "((t.`b` > X'00ff'))"),
        ([VARBINARY], ["\\x"], "((t.`b` > X''))"),
        (
            [VARCHAR, VARBINARY],
            ["a", "\\x01"],
            "((t.`k` > _utf8mb4 X'61') OR (t.`k` = _utf8mb4 X'61' AND t.`b` > X'01'))",
        ),
        # No key text of bytes (another side's text, upper-case digits, half a byte): by the texts' bytes.
        ([VARBINARY], ["ORG-1"], None),
        ([VARBINARY], ["\\x0F"], None),
        ([VARBINARY], ["\\x0"], None),
        ([VARCHAR, VARBINARY], ["a", "b"], None),
    ],
)
def test_a_scan_resumes_by_its_key_columns_where_the_bound_is_their_values(
    keys: list[Key], after: list[str], bound: str | None
) -> None:
    statement = engine_module.scan_sql(side(*keys), [part.encode() for part in after])
    if bound is None:
        assert _order(statement).startswith(BY_BYTES), statement
    else:
        assert f" AND {bound} ORDER BY t.`k`" in statement or f" AND {bound} ORDER BY t.`b`" in statement, (
            statement
        )


INT: Key = ("i", "integer", "int", None)
WIDE: Key = ("w", "integer", "bigint unsigned", None)
CI: Key = ("k", "text", "varchar(20)", ["utf8mb4", "utf8mb4_0900_ai_ci"])  # its own order is not its text's


@pytest.mark.parametrize(
    ("keys", "integers", "after", "order", "bound"),
    [
        ([INT], [0], None, "t.`i`", None),
        ([INT], [0], ["-5"], "t.`i`", "((t.`i` > -5))"),
        ([WIDE], [0], ["18446744073709551616"], "t.`w`", "((t.`w` > 18446744073709551616))"),
        (
            [VARCHAR, INT],
            [1],
            ["a", "7"],
            "t.`k`, t.`i`",
            "((t.`k` > _utf8mb4 X'61') OR (t.`k` = _utf8mb4 X'61' AND t.`i` > 7))",
        ),
        (
            [CI, INT],
            [1],
            ["a", "7"],
            "CAST(CONVERT(t.`k` USING utf8mb4) AS BINARY), t.`i`",
            "(CAST(CONVERT(t.`k` USING utf8mb4) AS BINARY), t.`i`) > (X'61', 7)",
        ),
        ([INT, VARCHAR], [], ["7", "a"], BY_BYTES, None),  # a plan that orders no part as an integer
    ],
    ids=["one", "resumed", "past its type", "beside a text", "beside a sorted text", "unrecorded"],
)
def test_an_integer_part_orders_and_bounds_the_scan_by_its_column(
    keys: list[Key], integers: list[int], after: list[str] | None, order: str, bound: str | None
) -> None:
    statement = engine_module.scan_sql(side(*keys, integers=integers), after and [p.encode() for p in after])
    if order == BY_BYTES:
        assert _order(statement).startswith(BY_BYTES) and "t.`i` >" not in statement, statement
    else:
        assert _order(statement) == f"ORDER BY {order}", statement
    if bound is not None:
        assert f" AND {bound} ORDER BY " in statement, statement


# --- on MySQL ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database(mysql_server: MySQLServer) -> str:
    return mysql_server.database("SELECT 1")


@pytest.fixture
def conn(mysql_server: MySQLServer, database: str) -> Iterator[pymysql.connections.Connection]:
    """A session as the engine's sorts: a sort key as long as its longest text."""
    connection = pymysql.connect(
        host="127.0.0.1", port=mysql_server.port, user="root", password=MYSQL_PASSWORD, database=database,
        charset="utf8mb4", autocommit=True, client_flag=CLIENT.MULTI_STATEMENTS,
    )  # fmt: skip
    _rows(connection, "SET SESSION max_sort_length = 8388608, cte_max_recursion_depth = 200000")
    try:
        yield connection
    finally:
        connection.close()


def _rows(conn: pymysql.connections.Connection, statement: str) -> list[tuple[Any, ...]]:
    """The last statement's rows."""
    with conn.cursor() as cursor:
        cursor.execute(statement)
        rows = list(cursor.fetchall())
        while cursor.nextset():
            rows = list(cursor.fetchall())
    return rows


@pytest.mark.db
@pytest.mark.mysql
async def test_the_catalogue_and_the_plan_record_each_key_column_s_collation(
    mysql_server: MySQLServer, database: str, tmp_path: Path
) -> None:
    mysql_server.execute(
        """
        CREATE TABLE recorded (k VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin,
                               raw VARBINARY(8), fixed BINARY(4), n INT, plain VARCHAR(20),
                               padded CHAR(4) CHARACTER SET latin1 COLLATE latin1_bin, PRIMARY KEY (k, raw));
        CREATE VIEW recorded_v AS SELECT * FROM recorded;
        """,
        database,
    )
    entry = mysql_server.target(database)
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    targets = load_targets(tmp_path / "targets.yaml")
    session = await MYSQL.connect(targets.get("db-a"))
    try:
        relations = [await MYSQL.describe(session, None, name) for name in ("recorded", "recorded_v")]
    finally:
        await session.close()
    for relation in relations:
        assert relation is not None
        assert {c.name: c.collation for c in relation.columns} == {
            "k": ("utf8mb4", "utf8mb4_0900_bin"), "raw": ("binary", "binary"), "fixed": ("binary", "binary"),
            "n": None, "plain": ("utf8mb4", "utf8mb4_0900_ai_ci"), "padded": ("latin1", "latin1_bin"),
        }  # fmt: skip
    for key, recorded in (("k", {"k": UTF8_BIN}), ("raw", {"raw": BINARY}), ("n", None)):
        table = {"name": "recorded", "keyColumns": [key], "columns": ["plain"]}
        stored = await plan(targets, compiled(table, table | {"name": "recorded_v"}))
        assert stored is not None
        assert stored["a"].get("collations") == stored["b"].get("collations") == recorded  # key columns only


# Each key: its declaration, its values (n is 1 to 100,000), its plan and the key a resume bound is.
SEEDED = {
    "varchar": (
        "k VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin PRIMARY KEY",
        "CONCAT('ORG-', LPAD(n, 7, '0'))",
        [("k", "text", "varchar(20)", UTF8_BIN)],
        ["ORG-0090000"],
    ),
    "varbinary": (
        "k VARBINARY(8) PRIMARY KEY",
        "UNHEX(LPAD(HEX(n), 8, '0'))",
        [("k", "bytes", "varbinary(8)", BINARY)],
        ["\\x00015f90"],
    ),
    "binary": (
        "k BINARY(4) PRIMARY KEY",
        "UNHEX(LPAD(HEX(n), 8, '0'))",
        [("k", "bytes", "binary(4)", BINARY)],
        ["\\x00015f90"],
    ),
    "composite": (
        "k VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin, b VARBINARY(8), PRIMARY KEY (k, b)",
        "CONCAT('R', n % 50), UNHEX(LPAD(HEX(n), 8, '0'))",
        [("k", "text", "varchar(20)", UTF8_BIN), ("b", "bytes", "varbinary(8)", BINARY)],
        ["R7", "\\x00000c"],
    ),
}


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.parametrize("key", list(SEEDED))
def test_a_key_in_its_byte_order_streams_from_its_primary_key_and_resumes_by_range(
    conn: pymysql.connections.Connection, database: str, key: str
) -> None:
    declared, values, keys, bound = SEEDED[key]
    numbered = "WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 100000)"
    seed = f"INSERT INTO streamed {numbered} SELECT {values}, n FROM g"  # noqa: S608 (constants)
    _rows(conn, f"DROP TABLE IF EXISTS streamed; CREATE TABLE streamed ({declared}, n INT); {seed}")
    _rows(conn, "ANALYZE TABLE streamed")
    for after in (None, [part.encode() for part in bound]):
        planned = side(*keys, schema=database, name="streamed")
        explained = _rows(conn, f"EXPLAIN {engine_module.scan_sql(planned, after)}")
        ((*_, access, _, used, _, _, _, _, extra),) = explained
        assert (access, used) == ("range" if after else "index", "PRIMARY"), (access, used, extra)
        assert "filesort" not in (extra or ""), extra
        # By the key texts' bytes, as before: the whole table, sorted.
        unseen = side(*keys, seen=None, schema=database, name="streamed")
        ((*_, extra),) = _rows(conn, f"EXPLAIN {engine_module.scan_sql(unseen, after)}")
        assert "Using filesort" in extra, extra


# Keys whose own order is not their texts' (a\t before a): each one's declaration, its table's options and
# its plan.
UNLIKE: dict[str, tuple[str, str, Key]] = {
    # Its index compares the padding it stores, 'a   ' after 'a\t  ', where its own comparison does not.
    "char": (
        "CHAR(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin",
        "ENGINE = MEMORY",
        ("k", "char", "char(4)", UTF8_BIN),
    ),
    # PAD SPACE: a compares as a followed by spaces, after a\t.
    "pad_space": (
        "VARCHAR(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin",
        "",
        ("k", "text", "varchar(4)", ["utf8mb4", "utf8mb4_bin"]),
    ),
}


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.parametrize("key", list(UNLIKE))
def test_a_key_whose_own_order_is_not_its_text_s_is_scanned_in_its_text_s_order(
    conn: pymysql.connections.Connection, database: str, key: str
) -> None:
    declared, options, planned = UNLIKE[key]
    table = f"CREATE TABLE unlike (k {declared}, n INT, PRIMARY KEY USING BTREE (k)) {options}"
    _rows(conn, f"DROP TABLE IF EXISTS unlike; {table}")
    _rows(conn, "INSERT INTO unlike VALUES ('a', 1), (CONCAT('a', CHAR(9)), 2), ('b', 3)")
    assert _rows(conn, "SELECT k FROM unlike FORCE INDEX (PRIMARY) ORDER BY k") == [("a\t",), ("a",), ("b",)]
    scanned = side(planned, schema=database, name="unlike")
    assert [row[0] for row in _rows(conn, engine_module.scan_sql(scanned, None))] == ["a", "a\t", "b"]
    assert [row[0] for row in _rows(conn, engine_module.scan_sql(scanned, [b"a"]))] == ["a\t", "b"]


# Text a key may hold: any character but a lone surrogate, with trailing spaces and tabs, the empty text,
# combining marks and characters outside the BMP.
TEXT = st.text(
    st.characters(codec="utf-8")
    | st.sampled_from(["a", "A", "Z", "z", "-", " ", "\t", "é", "́", "￿", "\U0001f600"]),
    max_size=6,
)
BYTES = st.binary(max_size=4) | st.sampled_from([b"", b"\x00", b"a", b"a ", b"a\x00", b"\xff"])
HEXED = BYTES.map(lambda found: "\\x" + found.hex())  # bytes' key text
FIXED = 3  # the BINARY's width
# Each key set, by the index that holds it.
INDEXED = {
    "by_v": ["v"], "by_vb": ["vb"], "by_b": ["b"], "by_w_second": ["w", "second"], "by_w_vb": ["w", "vb"],
}  # fmt: skip
PLANNED: dict[str, Key] = {
    "v": ("v", "text", "varchar(8)", UTF8_BIN),
    "w": ("w", "text", "varchar(8)", UTF8_BIN),
    "second": ("second", "text", "varchar(8)", UTF8_BIN),
    "vb": ("vb", "bytes", "varbinary(4)", BINARY),
    "b": ("b", "bytes", f"binary({FIXED})", BINARY),
}


def _keys(conn: pymysql.connections.Connection, statement: str, parts: int) -> list[tuple[str, ...]]:
    return [tuple(row[:parts]) for row in _rows(conn, statement)]


def _forced(statement: str, index: str) -> str:
    """The statement read through ``index`` whatever the optimiser would choose."""
    return statement.replace(" AS t WHERE ", f" AS t FORCE INDEX ({index}) WHERE ", 1)


@pytest.mark.db
@pytest.mark.mysql
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(
        st.tuples(TEXT, st.sampled_from(["", "a", "a ", "é"]), TEXT, BYTES, st.binary(max_size=FIXED)),
        min_size=1,
        max_size=40,
        unique_by=(
            lambda r: r[0], lambda r: (r[1], r[2]), lambda r: r[3], lambda r: r[4].ljust(FIXED, b"\0"),
            lambda r: (r[1], r[3]),
        ),
    ),
    st.data(),
)
def test_the_native_order_is_the_canonical_order(
    conn: pymysql.connections.Connection,
    database: str,
    rows: list[tuple[str, str, str, bytes, bytes]],
    data: st.DataObject,
) -> None:
    """For each key the scan orders by its columns, the rows come in the order, and from the point, of
    the UTF-8 bytes of their key texts, which Python's order of the texts is, through the key's index and
    as the optimiser reads it; and as they come when ordered by the key texts' bytes."""
    _rows(
        conn,
        "DROP TABLE IF EXISTS drawn; CREATE TABLE drawn ("
        " v VARCHAR(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,"
        " w VARCHAR(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,"
        " second VARCHAR(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,"
        f" vb VARBINARY(4) NOT NULL, b BINARY({FIXED}) NOT NULL, n INT,"
        + "".join(f" UNIQUE KEY {index} ({', '.join(keys)})," for index, keys in INDEXED.items())[:-1]
        + ")",
    )
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO drawn VALUES (%s, %s, %s, %s, %s, 0)", [tuple(row) for row in rows]
        )
    for index, keys in INDEXED.items():
        planned = side(*(PLANNED[key] for key in keys), schema=database, name="drawn")
        unseen = side(*(PLANNED[key] for key in keys), seen=None, schema=database, name="drawn")
        statement = engine_module.scan_sql(planned, None)
        assert _order(statement) == "ORDER BY " + ", ".join(f"t.`{key}`" for key in keys)
        canonical = _keys(conn, engine_module.scan_sql(unseen, None), len(keys))
        assert canonical == sorted(canonical)
        read = _keys(conn, statement, len(keys))
        assert read == _keys(conn, _forced(statement, index), len(keys)) == canonical
        # A bound this side read, and one another side may have: any text, or bytes' text of another width.
        other = [data.draw(TEXT if PLANNED[key][1] == "text" else HEXED | TEXT) for key in keys]
        for after in (list(data.draw(st.sampled_from(canonical))), other):
            bound = [part.encode() for part in after]
            expected = [key for key in canonical if key > tuple(after)]
            resumed = engine_module.scan_sql(planned, bound)
            assert (
                _keys(conn, resumed, len(keys))
                == _keys(conn, _forced(resumed, index), len(keys))
                == _keys(conn, engine_module.scan_sql(unseen, bound), len(keys))
                == expected
            ), resumed



# Each integer key: its declaration and its plan; n is 1 to 100,000.
WHOLE = {
    "int": ("k INT PRIMARY KEY", "n - 50000", [("k", "integer", "int", None)], ["40000"]),
    "unsigned": (
        "k BIGINT UNSIGNED PRIMARY KEY", "CAST(n AS UNSIGNED) * 184467440737095",
        [("k", "integer", "bigint unsigned", None)], ["18446744073709500000"],
    ),
    "composite": (
        "k VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin, i INT, PRIMARY KEY (k, i)",
        "CONCAT('R', n % 50), n - 50000",
        [("k", "text", "varchar(20)", UTF8_BIN), ("i", "integer", "int", None)],
        ["R7", "-40000"],
    ),
}  # fmt: skip


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.parametrize("key", list(WHOLE))
def test_an_integer_key_streams_from_its_primary_key_and_resumes_by_range(
    conn: pymysql.connections.Connection, database: str, key: str
) -> None:
    declared, values, keys, bound = WHOLE[key]
    numbered = "WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 100000)"
    seed = f"INSERT INTO whole {numbered} SELECT {values}, n FROM g"  # noqa: S608 (constants)
    _rows(conn, f"DROP TABLE IF EXISTS whole; CREATE TABLE whole ({declared}, n INT); {seed}")
    _rows(conn, "ANALYZE TABLE whole")
    integers = [i for i, part in enumerate(keys) if part[1] == "integer"]
    for after in (None, [part.encode() for part in bound]):
        planned = side(*keys, schema=database, name="whole", integers=integers)
        explained = _rows(conn, f"EXPLAIN {engine_module.scan_sql(planned, after)}")
        ((*_, access, _, used, _, _, _, _, extra),) = explained
        assert (access, used) == ("range" if after else "index", "PRIMARY"), (access, used, extra)
        assert "filesort" not in (extra or ""), extra
        # By the key texts' bytes, as a plan without the record reads: the whole table, sorted.
        unrecorded = side(*keys, schema=database, name="whole")
        ((*_, extra),) = _rows(conn, f"EXPLAIN {engine_module.scan_sql(unrecorded, after)}")
        assert "Using filesort" in extra, extra


SIGNED = st.integers(min_value=-(2**63), max_value=2**63 - 1)
UNSIGNED = st.integers(min_value=0, max_value=2**64 - 1)
FAR = st.integers(min_value=-(2**70), max_value=2**70)  # a bound another side may hold, past either type


@pytest.mark.db
@pytest.mark.mysql
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(
        st.tuples(SIGNED, UNSIGNED), min_size=1, max_size=40, unique_by=(lambda r: r[0], lambda r: r[1])
    ),
    st.data(),
)
def test_the_integer_order_is_the_order_of_the_values(
    conn: pymysql.connections.Connection,
    database: str,
    rows: list[tuple[int, int]],
    data: st.DataObject,
) -> None:
    """A signed and an unsigned BIGINT key are read in the order of their values, through their index, and
    from the point a bound gives: one of their keys, or a number neither type holds."""
    _rows(
        conn,
        "DROP TABLE IF EXISTS valued; CREATE TABLE valued (s BIGINT NOT NULL, u BIGINT UNSIGNED NOT NULL,"
        " n INT, UNIQUE KEY by_s (s), UNIQUE KEY by_u (u))",
    )
    with conn.cursor() as cursor:
        cursor.executemany("INSERT INTO valued VALUES (%s, %s, 0)", rows)
    for position, (column, declared) in enumerate((("s", "bigint"), ("u", "bigint unsigned"))):
        index = f"by_{column}"
        planned = side((column, "integer", declared, None), schema=database, name="valued", integers=[0])
        read = [int(key) for (key,) in _keys(conn, engine_module.scan_sql(planned, None), 1)]
        assert read == sorted(row[position] for row in rows)
        for after in (data.draw(st.sampled_from(read)), data.draw(FAR)):
            resumed = engine_module.scan_sql(planned, [str(after).encode()])
            expected = [value for value in read if value > after]
            assert [int(key) for (key,) in _keys(conn, resumed, 1)] == expected, resumed
            assert [int(key) for (key,) in _keys(conn, _forced(resumed, index), 1)] == expected
