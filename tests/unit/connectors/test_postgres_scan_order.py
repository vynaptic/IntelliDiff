"""A PostgreSQL scan's key order. In a UTF-8 database the "C" collation compares a text's UTF-8 bytes, so the
canonical key text in that collation is already in the canonical byte order: the scan orders and resumes by
it, and where it is a "C"-collation text or varchar key column a primary-key or unique index streams the rows
and resumes by range, with no sort. In another encoding it keeps ordering by the text's UTF-8 bytes. A key
part the plan orders as an integer is ordered and bounded by its column itself, in any encoding, so an
integer primary key streams and resumes by range too, also past a bound no bigint holds."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from functools import partial
from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg import sql

from deltasleuth.connectors.postgres import queries
from deltasleuth.connectors.tables import Plan

KEYS = {
    "text_c": 'text COLLATE "C"',
    "varchar_c": 'varchar(40) COLLATE "C"',
    "text_default": "text",
    "integer": "bigint",
}


def side(
    *keys: str, charset: str | None = "UTF8", relation: str = "keys", integers: list[int] | None = None
) -> Plan:
    plan = {
        "schema": "public", "name": relation, "keys": list(keys), "key_maps": [None] * len(keys),
        "columns": {"v": "v"}, "where": None, "types": {}, "digest": [],
    }  # fmt: skip
    return plan | ({"charset": charset} if charset else {}) | ({"integers": integers} if integers else {})


@pytest.mark.parametrize(
    ("charset", "ordered_by_bytes"), [("UTF8", False), ("LATIN1", True), ("SQL_ASCII", True), (None, True)]
)
def test_only_a_utf8_database_orders_by_the_key_text_itself(
    charset: str | None, ordered_by_bytes: bool
) -> None:
    statement, params = queries.scan_sql(side("a", "b", charset=charset), [b"ORG-1", "é".encode()])
    text = statement.as_string(None)
    order = text[text.rindex("ORDER BY") :]
    assert ("convert_to" in order) is ordered_by_bytes
    bound = [params["a0"], params["a1"]]
    assert bound == ([b"ORG-1", "é".encode()] if ordered_by_bytes else ["ORG-1", "é"])


@pytest.mark.parametrize("charset", ["UTF8", "LATIN1"])
def test_an_integer_part_is_ordered_and_bounded_by_its_column(charset: str) -> None:
    statement, params = queries.scan_sql(side("a", "n", charset=charset, integers=[1]), [b"ORG-1", b"-42"])
    text = statement.as_string(None)
    key = '(t."a"::text COLLATE "C")'
    first = key if charset == "UTF8" else f"convert_to({key}, 'UTF8')"
    assert text.endswith(f'ORDER BY {first}, t."n"'), text
    assert f'({first}, t."n") > (%(a0)s{"::text" if charset == "UTF8" else ""}, %(a1)s::int8)' in text
    assert params["a1"] == -42


@pytest.mark.parametrize(
    ("after", "bound"),
    [
        ([b"9223372036854775807"], '(t."n") > (%(a0)s::int8)'),  # a bigint's largest: still a bigint
        ([b"9223372036854775808"], "false"),  # past every bigint: no key is after it
        ([b"-9223372036854775809"], None),  # before every bigint: every key is
        ([b"7", b"9223372036854775808"], '(t."m") > (%(a0)s::int8)'),  # after 7's: past them all
        ([b"7", b"-9223372036854775809"], '(t."m") >= (%(a0)s::int8)'),  # 7's are, all of them
    ],
)
def test_a_bound_no_bigint_holds_ends_the_row_comparison(after: list[bytes], bound: str | None) -> None:
    keys = ("m", "n") if len(after) == 2 else ("n",)
    statement, _ = queries.scan_sql(side(*keys, integers=list(range(len(keys)))), after)
    text = statement.as_string(None)
    where = text[text.index(" WHERE ") : text.index(" ORDER BY ")]
    if bound is None:
        assert ">" not in where and "false" not in where, where
    else:
        assert where.endswith(f" AND {bound}"), where


@pytest.fixture
def conn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        yield conn


def _plan(conn: psycopg.Connection[Any], statement: sql.Composable, params: dict[str, Any]) -> str:
    """The plan of the statement as the scan runs it: through a cursor, which plans for its first rows."""
    with conn.transaction():
        found = conn.execute(
            sql.SQL("EXPLAIN DECLARE ds_scan NO SCROLL CURSOR FOR {}").format(statement), params
        )
        return "\n".join(row[0] for row in found)


@pytest.mark.db
@pytest.mark.parametrize("key", ["text_c", "varchar_c"])
def test_a_c_collation_text_key_streams_from_its_index_and_resumes_by_range(
    conn: psycopg.Connection[Any], key: str
) -> None:
    conn.execute(f"CREATE TABLE keys (k {KEYS[key]} PRIMARY KEY, v int)")
    conn.execute(
        "INSERT INTO keys SELECT 'ORG-' || lpad(g::text, 7, '0'), g FROM generate_series(1, 100000) g"
    )
    conn.execute("ANALYZE keys")
    for after in (None, [b"ORG-0090000"]):
        plan = _plan(conn, *queries.scan_sql(side("k"), after))
        assert "Index Scan using keys_pkey" in plan and "Sort" not in plan, plan
        if after:
            assert "Index Cond" in plan and "> 'ORG-0090000'" in plan, plan
        by_bytes = _plan(conn, *queries.scan_sql(side("k", charset=None), after))
        assert "Sort" in by_bytes, by_bytes  # the order the scan read before: the whole table, sorted


@pytest.mark.db
def test_a_composite_c_collation_key_resumes_by_a_row_comparison_its_index_serves(
    conn: psycopg.Connection[Any],
) -> None:
    conn.execute(
        'CREATE TABLE keys (a text COLLATE "C", b varchar(20) COLLATE "C", v int, PRIMARY KEY (a, b))'
    )
    conn.execute("INSERT INTO keys SELECT 'R' || (g % 50), 'K' || g, g FROM generate_series(1, 100000) g")
    conn.execute("ANALYZE keys")
    plan = _plan(conn, *queries.scan_sql(side("a", "b"), [b"R7", b"K12"]))
    assert "Index Scan using keys_pkey" in plan and "Sort" not in plan and "ROW(" in plan, plan


@pytest.mark.db
@pytest.mark.parametrize("key", ["text_default", "integer"])
def test_a_key_no_index_orders_so_is_sorted_as_before(conn: psycopg.Connection[Any], key: str) -> None:
    conn.execute(f"CREATE TABLE keys (k {KEYS[key]} PRIMARY KEY, v int)")
    value = "g" if key == "integer" else "'K' || g"
    conn.execute(f"INSERT INTO keys SELECT {value}, g FROM generate_series(1, 20000) g")  # noqa: S608
    conn.execute("ANALYZE keys")
    assert "Sort" in _plan(conn, *queries.scan_sql(side("k"), None))


@pytest.mark.db
@pytest.mark.parametrize("kind", ["smallint", "integer", "bigint"])
def test_an_integer_key_streams_from_its_index_and_resumes_by_range(
    conn: psycopg.Connection[Any], kind: str
) -> None:
    conn.execute(f"CREATE TABLE keys (k {kind} PRIMARY KEY, v int)")
    conn.execute("INSERT INTO keys SELECT g - 15000, g FROM generate_series(1, 30000) g")
    conn.execute("ANALYZE keys")
    for after in (None, [b"9000"]):
        plan = _plan(conn, *queries.scan_sql(side("k", integers=[0]), after))
        assert "Index Scan using keys_pkey" in plan and "Sort" not in plan, plan
        if after:
            assert "Index Cond: (k > '9000'::bigint)" in plan, plan
        assert "Sort" in _plan(conn, *queries.scan_sql(side("k"), after))  # by its text, as before


@pytest.mark.db
def test_a_composite_key_with_an_integer_part_resumes_by_a_row_comparison_its_index_serves(
    conn: psycopg.Connection[Any],
) -> None:
    conn.execute('CREATE TABLE keys (a text COLLATE "C", n bigint, v int, PRIMARY KEY (a, n))')
    conn.execute("INSERT INTO keys SELECT 'R' || (g % 50), g, g FROM generate_series(1, 100000) g")
    conn.execute("ANALYZE keys")
    plan = _plan(conn, *queries.scan_sql(side("a", "n", integers=[1]), [b"R7", b"12"]))
    assert "Index Scan using keys_pkey" in plan and "Sort" not in plan and "ROW(" in plan, plan


# Text a key may hold (no NUL, no lone surrogate): ASCII, Latin-1, combining marks, other scripts, emoji.
TEXT = st.text(
    st.characters(codec="utf-8", exclude_characters="\x00")
    | st.sampled_from(["a", "A", "Z", "z", "-", " ", "\u00e9", "\u0301", "\uffff", "\U0001f600", "\u00ff"]),
    max_size=6,
)
NUMBER = st.integers(min_value=-(2**63), max_value=2**63 - 1)


def _scanned(conn: psycopg.Connection[Any], plan: Plan, after: list[bytes] | None) -> list[tuple[str, ...]]:
    statement, params = queries.scan_sql(plan, after)
    return [row[: len(plan["keys"])] for row in conn.execute(statement, params)]


@pytest.mark.db
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(st.tuples(TEXT, TEXT, NUMBER), min_size=1, max_size=40, unique_by=lambda r: (r[0], r[1])),
    st.data(),
)
def test_the_key_text_order_is_the_canonical_order(
    conn: psycopg.Connection[Any], rows: list[tuple[str, str, int]], data: st.DataObject
) -> None:
    """For each kind of key column, ordering and bounding by the key text in the "C" collation reads the
    rows in the order and from the point the UTF-8 bytes of the key texts give, which Python's own order of
    the texts is."""
    conn.execute("DROP TABLE IF EXISTS keys")
    columns = ", ".join(f"{name} {kind}" for name, kind in KEYS.items())
    conn.execute(f'CREATE TABLE keys ({columns}, second text COLLATE "C", v int)')
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO keys VALUES (%s, %s, %s, %s, %s, 0)", [(a, a, a, n, b) for a, b, n in rows]
        )
    for keys in (["text_c"], ["varchar_c"], ["text_default"], ["integer"], ["text_c", "second"]):
        canonical = _scanned(conn, side(*keys, charset=None), None)
        assert _scanned(conn, side(*keys), None) == canonical == sorted(canonical)
        after = list(data.draw(st.sampled_from(canonical)))
        bound = [part.encode() for part in after]
        resumed = _scanned(conn, side(*keys), bound)
        assert (
            resumed
            == _scanned(conn, side(*keys, charset=None), bound)
            == [k for k in canonical if k > tuple(after)]
        )


def _ranked(integers: list[int], key: Sequence[str]) -> tuple[int | str, ...]:
    """What the scan's merge ranks a key by: an integer part's value, any other's text."""
    return tuple(int(part) if i in integers else part for i, part in enumerate(key))


SMALL = st.integers(min_value=-(2**15), max_value=2**15 - 1)
# A bound another side may hold: any of its keys, or a number past a bigint's range.
FAR = st.integers(min_value=-(2**70), max_value=2**70)


@pytest.mark.db
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(st.tuples(SMALL, TEXT), min_size=1, max_size=40, unique_by=lambda r: r[0]),
    st.data(),
)
def test_the_integer_order_is_the_order_of_the_values(
    conn: psycopg.Connection[Any], rows: list[tuple[int, str]], data: st.DataObject
) -> None:
    """For each integer type, alone and after a "C"-collation text, ordering and bounding a part the plan
    orders as an integer reads the rows in the order of the values, and from the point a bound gives,
    whatever the bound: one of the keys, or a number no bigint holds."""
    conn.execute("DROP TABLE IF EXISTS keys")
    conn.execute('CREATE TABLE keys (small smallint, plain integer, big bigint, t text COLLATE "C", v int)')
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO keys VALUES (%s, %s, %s, %s, 0)", [(n, n, n * 2**40, t) for n, t in rows]
        )
    for keys, integers in ((["small"], [0]), (["plain"], [0]), (["big"], [0]), (["t", "big"], [1])):
        ranked = partial(_ranked, integers)
        read = _scanned(conn, side(*keys, integers=integers), None)
        assert read == sorted(read, key=ranked) and len(read) == len(rows)
        canonical = [ranked(key) for key in read]
        for after in (list(data.draw(st.sampled_from(read))), [*read[0][:-1], str(data.draw(FAR))]):
            bound = [part.encode() for part in after]
            resumed = _scanned(conn, side(*keys, integers=integers), bound)
            assert [ranked(key) for key in resumed] == [key for key in canonical if key > ranked(after)]
