"""PostgreSQL behind the engine seam: today's sessions and statements, unchanged, and the catalogue query
of the column plan. In a pair with another engine its rows take the canonical form of rendering 1 and the
worker hashes them.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg

# The modules, not their names: the seam's registry imports this module while ``queries`` may be loading.
from deltasleuth.connectors.postgres import queries, session
from deltasleuth.connectors.tables import render
from deltasleuth.connectors.tables.engine import Asked, Column, Figure, Relation, Row, Rows, sized
from deltasleuth.connectors.tables.keys import Plan
from deltasleuth.core import jsonio
from deltasleuth.core.types import Target

BATCH = 10_000  # rows per fetch
BATCH_BYTES = 64 * 1024 * 1024  # in a pair with another engine a fetch is sized to hold about this much
FIRST = 100  # rows of such a scan's first fetch, whose width is not known yet
# The class of each declared type, as ``format_type`` names it without its modifier; an
# enum is text, any other type is ``other``.
_CLASSES = {
    **dict.fromkeys(("smallint", "integer", "bigint"), "integer"),
    "numeric": "decimal",
    "double precision": "float",
    "real": "float32",
    **dict.fromkeys(("text", "character varying", "uuid"), "text"),
    "character": "char",
    "boolean": "boolean",
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "instant",
    "time without time zone": "time",
    "bytea": "bytes",
    **dict.fromkeys(("json", "jsonb"), "json"),
}

# The relation as spelled, else as PostgreSQL folds unquoted names; its live columns in order, those
# the role may read, those LIKE can match (text, in a deterministic collation before PostgreSQL 18),
# their declared types, which of them are enums, what kind of relation it is, which columns may hold
# NULL and its primary key's columns in the key's order.
_KINDS = {"v": "view", "m": "materialized view", "f": "foreign table"}  # any other is a table
_RELATION = """
    SELECT n.nspname::text, c.relname::text,
           array_agg(a.attname::text ORDER BY a.attnum) FILTER (WHERE a.attnum > 0 AND NOT a.attisdropped),
           array_agg(a.attname::text) FILTER (
               WHERE a.attnum > 0 AND NOT a.attisdropped AND has_column_privilege(c.oid, a.attnum, 'SELECT')),
           array_agg(a.attname::text) FILTER (
               WHERE a.attnum > 0 AND NOT a.attisdropped AND ty.typcategory = 'S'
                 AND (current_setting('server_version_num')::int >= 180000
                      OR coalesce(co.collisdeterministic, true))),
           array_agg(format_type(a.atttypid, a.atttypmod) ORDER BY a.attnum) FILTER (
               WHERE a.attnum > 0 AND NOT a.attisdropped),
           array_agg(a.attname::text) FILTER (WHERE a.attnum > 0 AND NOT a.attisdropped AND ty.typtype = 'e'),
           c.relkind::text,
           array_agg(NOT a.attnotnull ORDER BY a.attnum) FILTER (WHERE a.attnum > 0 AND NOT a.attisdropped),
           (SELECT array_agg(k.attname::text ORDER BY x.n)
            FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS x (attnum, n)
                 JOIN pg_attribute k ON k.attrelid = i.indrelid AND k.attnum = x.attnum
            WHERE i.indrelid = c.oid AND i.indisprimary)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid
         JOIN pg_type ty ON ty.oid = a.atttypid LEFT JOIN pg_collation co ON co.oid = a.attcollation
    WHERE c.oid = coalesce(
        to_regclass(concat_ws('.', quote_ident(%(schema)s), quote_ident(%(name)s))),
        to_regclass(concat_ws('.', quote_ident(lower(%(schema)s)), quote_ident(lower(%(name)s)))))
    GROUP BY c.oid, n.nspname, c.relname, c.relkind"""
# The tables and views of a schema the role may read: the schema as spelled, else as PostgreSQL folds
# it; the session's current schema when none is named. A partition is not listed: its partitioned table
# reads its rows, as MySQL's and Oracle's catalogues list a partitioned table once.
_RELATIONS = """
    SELECT c.relname::text, c.relkind::text FROM pg_class c
    WHERE c.relnamespace = CASE
              WHEN %(schema)s::text IS NULL THEN to_regnamespace(quote_ident(current_schema()))
              ELSE coalesce(to_regnamespace(quote_ident(%(schema)s)),
                            to_regnamespace(quote_ident(lower(%(schema)s)))) END
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
      AND has_schema_privilege(c.relnamespace, 'USAGE')
      AND has_any_column_privilege(c.oid, 'SELECT')
    ORDER BY convert_to(c.relname::text, 'UTF8')"""


class PostgreSQL:
    """``connectors.tables.Engine`` over psycopg; a session is a ``psycopg.AsyncConnection``."""

    name = "postgresql"
    arms = 200
    unfit = session.UNFIT
    fold = staticmethod(str.lower)
    hides = False
    grant = "SELECT"
    sum_digits = None  # a numeric sum is exact
    empty_null = False
    retry_alone = True

    def built(self, column: str, cls: str, declared: str, key: bool) -> str | None:
        return None  # a key's text is its ::text, a row its to_jsonb, of whatever type the column holds

    async def connect(self, target: Target, *, timeout_s: float = 0) -> psycopg.AsyncConnection:
        return await session.connect(target, timeout_s=timeout_s)

    def snapshot(
        self, target: Target, *, timeout_s: float = 0
    ) -> AbstractAsyncContextManager[psycopg.AsyncConnection]:
        return session.snapshot(target, timeout_s=timeout_s)

    async def server(self, conn: psycopg.AsyncConnection) -> dict[str, Any]:
        return {
            "version": (conn.info.parameter_status("server_version") or "").split(" ")[0],
            "charset": conn.info.parameter_status("server_encoding"),
            "tls": bool(conn.info.pgconn.ssl_in_use),
        }

    def release(self, conn: psycopg.AsyncConnection) -> str:
        version = conn.info.server_version
        return f"{version // 10000}.{version % 10000}"

    async def describe(
        self, conn: psycopg.AsyncConnection, schema: str | None, name: str, *, side: Any = None
    ) -> Relation | None:
        try:
            cursor = await conn.execute(_RELATION, {"schema": schema, "name": name})
        except psycopg.errors.InsufficientPrivilege:  # no USAGE on the schema
            return None
        row = await cursor.fetchone()
        if row is None:
            return None
        readable, likeable, enums = set(row[3] or ()), set(row[4] or ()), set(row[6] or ())
        kind = _KINDS.get(row[7], "table")
        columns = []
        for column, declared, nullable in zip(row[2] or (), row[5] or (), row[8] or (), strict=True):
            cls = "text" if column in enums else _CLASSES.get(re.sub(r"\([0-9,]*\)", "", declared), "other")
            # A key text every engine renders alike: a timestamp's only without fractional seconds.
            keyable = cls in render.KEYS and (cls != "timestamp" or declared.startswith("timestamp(0)"))
            read, like = column in readable, column in likeable
            says = None if kind.endswith("view") else nullable  # no view's column says whether it may
            columns.append(
                Column(column, declared, cls, read, like, keyable, False, says, sized(cls, declared))
            )
        facts = {
            "kind": kind,
            "key": row[9],
            "server": self.release(conn),
            "consistency": "snapshot",
            "tls": bool(conn.info.pgconn.ssl_in_use),
            "charset": conn.info.parameter_status("server_encoding"),
        }
        return Relation(row[0], row[1], tuple(columns), facts)

    async def probe(self, conn: psycopg.AsyncConnection, side: Plan) -> None:
        """Also compile the side's key maps' patterns (planning alone does not)."""
        await conn.execute(*queries.probe_sql(side))
        for rule in [m["toCanonical"] for m in side["key_maps"] if m and m["kind"] == "regex"]:
            await conn.execute(
                "SELECT regexp_replace('', %s, %s)", (f"^(?:{rule['pattern']})$", rule["replacement"])
            )

    def large(self, side: Plan) -> bool:
        return False  # no type is classed a LOB here

    async def rows(self, conn: psycopg.AsyncConnection, side: Plan, asked: Asked) -> list[Row]:
        found = await (await conn.execute(*queries.rows_sql(side, asked))).fetchall()
        if not side.get("classes"):
            return found
        return [(i, _canonical(side, text), selected) for i, text, selected in found]

    async def scan(
        self, conn: psycopg.AsyncConnection, side: Plan, after: Sequence[bytes] | None
    ) -> AsyncIterator[Rows]:
        """The digest of a plan with digest columns is the server's hash extended by the worker: off the
        event loop in a pair with another engine, on it in a PostgreSQL pair, as before the seam."""
        cursor = conn.cursor("ds_scan")
        await cursor.execute(*queries.scan_sql(side, after))  # declared now: it takes the snapshot
        if not side.get("digest"):
            return _fetched(cursor)
        # In a pair with another engine every compared column comes with each row.
        sized = _sized(cursor, len(side["keys"]) + 1) if side.get("classes") else _fetched(cursor)
        return _digested(side, sized)

    def census(self, conn: psycopg.AsyncConnection, side: Plan) -> AsyncIterator[Rows]:
        return _stream(conn, "ds_census", *queries.census_sql(side))

    def frame(self, conn: psycopg.AsyncConnection, side: Plan) -> AsyncIterator[Rows]:
        return _stream(conn, "ds_frame", *queries.frame_sql(side))

    async def null_keys(self, conn: psycopg.AsyncConnection, side: Plan) -> int:
        statement, params = queries.null_keys_sql(side)
        return (await (await conn.execute(statement, params)).fetchone())[0]  # type: ignore[index]

    async def relations(self, conn: psycopg.AsyncConnection, schema: str | None) -> list[tuple[str, str]]:
        found = await (await conn.execute(_RELATIONS, {"schema": schema})).fetchall()
        return [(name, _KINDS.get(kind, "table")) for name, kind in found]

    async def figures(
        self, conn: psycopg.AsyncConnection, side: Plan, wanted: Sequence[Figure]
    ) -> list[str | None]:
        statement, params = queries.figures_sql(side, wanted)
        # Without a parameter the driver reads no placeholder: a plan made of a catalogue alone, which
        # has neither filter nor key map, may name a column that holds a %.
        return list(await (await conn.execute(statement, params or None)).fetchone())  # type: ignore[arg-type]

    async def duplicates(self, conn: psycopg.AsyncConnection, side: Plan) -> tuple[int, int]:
        statement, params = queries.duplicates_sql(side)
        keys, rows = await (await conn.execute(statement, params or None)).fetchone()  # type: ignore[misc]
        return keys, int(rows)


async def _fetched(cursor: psycopg.AsyncServerCursor) -> AsyncIterator[Rows]:
    while batch := await cursor.fetchmany(BATCH):
        yield batch


async def _sized(cursor: psycopg.AsyncServerCursor, text: int) -> AsyncIterator[Rows]:
    """A scan's rows in a pair with another engine, where a row comes with its compared columns' JSON
    text (item ``text``): each fetch is of as many rows as hold ``BATCH_BYTES`` at the width of the widest
    row of the fetch before it, ``BATCH`` at most."""
    size = FIRST
    while batch := await cursor.fetchmany(size):
        yield batch
        size = max(1, min(BATCH, BATCH_BYTES // max(len(row[text]) for row in batch)))


def _canonical(side: Plan, text: str) -> str | ValueError:
    """A per-key row of a pair across engines, in the canonical form."""
    try:
        return jsonio.dumps(render.parsed(side, text))
    except ValueError as exc:  # it names a column and a class, or a parse limit: never a value
        return exc


async def _digested(side: Plan, batches: AsyncIterator[Rows]) -> AsyncIterator[Rows]:
    """Each fetched row's (key parts, row hash, digest columns' JSON text) as (key parts, digest). A
    PostgreSQL pair's few typeMap columns are hashed where they were before the seam, on the event loop:
    the hand-off to a thread cost such a scan about a sixth of its time."""
    n = len(side["keys"])

    def digests(batch: Rows) -> Rows:
        out = []
        for row in batch:
            try:
                found = render.digest(side, row[n], render.parsed(side, row[n + 1]))
            except ValueError:  # it does not convert, or passes a parse limit: its per-key read says which
                found = render.UNCONVERTIBLE
            out.append((*row[:n], found))
        return out

    async for batch in batches:
        yield await asyncio.to_thread(digests, batch) if side.get("classes") else digests(batch)


async def _stream(
    conn: psycopg.AsyncConnection, name: str, statement: Any, params: queries.Params
) -> AsyncIterator[Rows]:
    """The statement's rows in fetched batches, through a named server-side cursor."""
    cursor = conn.cursor(name)
    await cursor.execute(statement, params)
    async for batch in _fetched(cursor):
        yield batch
