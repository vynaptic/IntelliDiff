"""The helpers every query module shares: the connection and row types, the statement runners, keyset
cursors and paging, sortable ids, the verdict SQL fragments and what several sections read."""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, LiteralString

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from deltasleuth.core import jsonio
from deltasleuth.core.types import Outcome, Verdict, derive_verdict

Conn = AsyncConnection[Any]
Row = dict[str, Any]
Page = tuple[list[Row], str | None]

TERMINAL = ("cancelled", "completed", "completed_with_issues", "failed")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# A key-list run with a table side whose current cycle lacks a key census of each table side
# (runner.census.pending says the same in Python). A hash sample's frame is its
# census: a key census would count every unsampled key missing.
_CENSUS_PENDING: LiteralString = """(r.mode <> 'one_shot'
    AND coalesce(r.params->>'keys_source', '') NOT IN ('table_scan', 'hash_sample')
    AND EXISTS (SELECT 1 FROM unnest(ARRAY['a', 'b']) AS s (side)
                WHERE r.params->'tables'->s.side IS NOT NULL
                  AND (r.census->>'cycle' IS DISTINCT FROM r.cycle::text
                       OR jsonb_typeof(r.census->s.side) IS DISTINCT FROM 'object')))"""


def storable(value: Any) -> Any:
    """``value`` with NUL, which PostgreSQL text and jsonb cannot hold, as the visible text ``\\u0000``
    (a legacy side's NUL padding is data; its raw bytes stay in the evidence)."""
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, Mapping):
        return {storable(k): storable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [storable(v) for v in value]
    return value


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(storable(value), dumps=jsonio.dumps)  # exact Decimals


async def _rows(conn: Conn, sql: LiteralString, params: Any = None) -> list[Row]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def _row(conn: Conn, sql: LiteralString, params: Any = None) -> Row | None:
    rows = await _rows(conn, sql, params)
    return rows[0] if rows else None


async def _one(conn: Conn, sql: LiteralString, params: Any = None) -> Row:
    """The row of a statement that always returns one: an aggregate, an ``INSERT ... RETURNING``, an
    update of a row the transaction holds. ``LookupError`` if it returns none."""
    if (row := await _row(conn, sql, params)) is None:
        raise LookupError("a statement that returns one row returned none")
    return row


async def _value(conn: Conn, sql: LiteralString, params: Any) -> Any:
    """The first column of the first row (a RETURNING value or a scalar); None without rows."""
    row = await (await conn.execute(sql, params)).fetchone()
    return row[0] if row else None


async def _changed(conn: Conn, sql: LiteralString, params: Any) -> int:
    return (await conn.execute(sql, params)).rowcount


def lost(conn: Conn, exc: BaseException) -> bool:
    """Whether ``exc`` is a lost connection, which nothing on ``conn`` outlives. Any other error, a
    statement the server cancelled or refused among them, ends only the savepoint it was raised in."""
    if conn.broken:
        return True
    return isinstance(exc, errors.Error) and (exc.sqlstate is None or exc.sqlstate.startswith("08"))


async def _current(conn: Conn, run_id: str, cycle: int | None) -> int | None:
    if cycle is not None:
        return cycle
    return await _value(conn, "SELECT cycle FROM ds.runs WHERE run_id = %s", (run_id,))


def _after(cursor: str | None, parts: int) -> list[int]:
    """The keyset position a cursor encodes; before everything when None."""
    if cursor is None:
        return [-1] * parts
    values = [int(part) for part in cursor.split(".")]
    if len(values) != parts:
        raise ValueError("malformed cursor")
    return values


def _page(rows: list[Row], limit: int, *cursor_fields: str) -> Page:
    """``rows`` holds one row past the page when there is a next page."""
    if len(rows) <= limit:
        return rows, None
    return rows[:limit], ".".join(str(rows[limit - 1][f]) for f in cursor_fields)


async def pages(fetch: Callable[..., Awaitable[Page]], *args: Any, **kwargs: Any) -> list[Row]:
    """Every row of a paged query (``fetch(*args, **kwargs)``), a thousand at a time."""
    rows, cursor = await fetch(*args, limit=1000, **kwargs)
    while cursor is not None:
        more, cursor = await fetch(*args, limit=1000, cursor=cursor, **kwargs)
        rows += more
    return rows


def _sortable_id(prefix: str) -> str:
    """``prefix`` + 26 Crockford base32 characters (microseconds, then 76 random bits): ids sort by age."""
    n = (time.time_ns() // 1000) << 76 | secrets.randbits(76)
    return prefix + "".join(_CROCKFORD[(n >> shift) & 31] for shift in range(125, -1, -5))


_LIVE: LiteralString = "r.status IN ('queued', 'running', 'paused', 'cancelling')"


def _bug(row: LiteralString = "k", cycle: LiteralString = "%(c)s") -> LiteralString:
    """Whether a finding of the key of the result ``row`` is under a bug rule, the ``bug`` that
    ``types.derive_verdict`` reads. ``row`` is a result of cycle ``cycle`` of run ``%(r)s``: named, they keep
    the subquery to that cycle's findings (hashed over the row's columns it would read every run's)."""
    return f"""EXISTS (SELECT 1 FROM ds.findings f
                       WHERE f.run_id = %(r)s AND f.cycle = {cycle} AND f.key_masked = {row}.key_masked
                         AND f.rule_id IS NOT NULL AND f.rule_classification = 'bug')"""  # noqa: S608


def _verdict_is(verdict: Verdict, row: LiteralString = "k", cycle: LiteralString = "%(c)s") -> LiteralString:
    """True where ``types.derive_verdict`` gives the result ``row`` (as ``_bug``'s) ``verdict``, false
    elsewhere (NULL without a result). Its outcome lists are that function's answers, taken at import, so
    no statement can read an outcome otherwise. The outcomes that can have the verdict come first, for
    results_outcome_idx; the bug rule's subquery is asked only of an outcome whose verdict it decides."""
    plain = [o for o in Outcome if derive_verdict(o, False) is verdict]
    bugged = [o for o in Outcome if derive_verdict(o, True) is verdict]

    def among(outcomes: Sequence[Outcome]) -> str:
        assert all(o.value.isupper() for o in outcomes)  # a fixed word: safe as a literal
        return f"{row}.outcome IN ({', '.join(f"'{o.value}'" for o in outcomes)})" if outcomes else "false"

    arms = [among([o for o in plain if o in bugged])]
    if with_bug := [o for o in bugged if o not in plain]:
        arms.append(f"{among(with_bug)} AND {_bug(row, cycle)}")
    if without_bug := [o for o in plain if o not in bugged]:
        arms.append(f"{among(without_bug)} AND NOT {_bug(row, cycle)}")
    either = [o for o in Outcome if o in plain or o in bugged]
    return f"({among(either)} AND ({' OR '.join(arms)}))"


_BUG = _bug()
# The result ``k`` of cycle ``%(c)s`` of run ``%(r)s`` is DIFFERENT, or MATCH, as ``types.derive_verdict``
# reads it; and its verdict. Every statement that tells discrepant keys apart reads these.
_DIFFERENT = _verdict_is(Verdict.DIFFERENT)
_MATCH = _verdict_is(Verdict.MATCH)
_VERDICT: LiteralString = f"""CASE WHEN {_DIFFERENT} THEN 'DIFFERENT' WHEN {_MATCH} THEN 'MATCH'
                                   ELSE 'INDETERMINATE' END"""
