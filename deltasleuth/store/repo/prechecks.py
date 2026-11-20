"""The queries of table pre-checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, LiteralString

from deltasleuth.store.repo.base import (
    Conn,
    Page,
    Row,
    _after,
    _changed,
    _jsonb,
    _one,
    _page,
    _row,
    _rows,
    _sortable_id,
    _value,
)

# A pre-check with the database's ``now``, its actors' names, when a pair of it was last settled, when a
# worker last took or renewed a running pair of it, and its pairs counted by status (``progress``) and by
# verdict (``verdicts``): no pair's ``result`` is read.
_PRECHECK: LiteralString = """SELECT p.*, now() AS now, ds.actor_name(p.created_by) AS created_by_name,
        ds.actor_name(p.cancelled_by) AS cancelled_by_name,
        (SELECT max(finished_at) FROM ds.precheck_pairs WHERE precheck_id = p.precheck_id) AS settled_at,
        (SELECT max(claimed_at) FROM ds.precheck_pairs
         WHERE precheck_id = p.precheck_id AND status = 'running') AS claimed_at,
        (SELECT coalesce(jsonb_object_agg(s.status, s.n), '{}')
         FROM (SELECT status, count(*) AS n FROM ds.precheck_pairs
               WHERE precheck_id = p.precheck_id GROUP BY status) s) AS progress,
        (SELECT coalesce(jsonb_object_agg(v.verdict, v.n), '{}')
         FROM (SELECT verdict, count(*) AS n FROM ds.precheck_pairs
               WHERE precheck_id = p.precheck_id AND verdict IS NOT NULL GROUP BY verdict) v) AS verdicts
    FROM ds.prechecks p"""
# Every column of a pair but ``result`` and ``claimed_at``.
_PAIR_COLUMNS: LiteralString = """precheck_id, n, spec_id, label, target_a, relation_a, target_b, relation_b,
    status, tries, verdict, rank, differing, rows_a, rows_b, line, finished_at"""
_PRECHECKS_LOCK = 0x44535043  # pg advisory lock "DSPC": creates of pre-checks, counted against their limit


def new_precheck_id() -> str:
    return _sortable_id("PC-")


async def precheck_specs(
    conn: Conn, *, spec_ids: Sequence[int] | None = None, names: Sequence[str] | None = None
) -> list[Row]:
    """What a pre-check pairs of stored specs, read from their bodies without compiling one: the versions
    ``spec_ids``, else the newest version of each of ``names``, else of every name. Each one's spec_id,
    name, version, each side's target (``target_a``, ``target_b``) and its ``table`` member (``table_a``,
    ``table_b``; None: a service side)."""
    return await _rows(
        conn,
        """SELECT s.spec_id, s.name, s.version, s.body->'sides'->'a'->>'target' AS target_a,
                  s.body->'sides'->'b'->>'target' AS target_b, s.body->'sides'->'a'->'table' AS table_a,
                  s.body->'sides'->'b'->'table' AS table_b
           FROM ds.specs s
           WHERE CASE WHEN %(spec_ids)s::bigint[] IS NOT NULL THEN s.spec_id = ANY(%(spec_ids)s)
                      ELSE s.spec_id IN (SELECT max(spec_id) FROM ds.specs
                                         WHERE %(names)s::text[] IS NULL OR name = ANY(%(names)s)
                                         GROUP BY name) END
           ORDER BY s.name, s.spec_id""",
        {"spec_ids": None if spec_ids is None else list(spec_ids),
         "names": None if names is None else list(names)},
    )  # fmt: skip


async def insert_precheck(conn: Conn, precheck: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]]) -> Row:
    """Store a pre-check (precheck_id, source, level, request, skipped, evaluation, created_by) and its
    pairs, each pending: n, spec_id (None: paired by name), label, target_a, relation_a, target_b and
    relation_b. A pair that comes with a ``verdict`` is settled as it is made (a name without a pair),
    with its rank, differing, line and result. The stored row."""
    stored = await _one(
        conn,
        """INSERT INTO ds.prechecks (precheck_id, source, level, request, pairs, skipped, evaluation,
               created_by)
           VALUES (%(precheck_id)s, %(source)s, %(level)s, %(request)s, %(pairs)s, %(skipped)s,
               %(evaluation)s, %(created_by)s)
           RETURNING *""",
        dict(precheck) | {"request": _jsonb(precheck["request"]), "pairs": len(pairs)},
    )
    settled = dict.fromkeys(("verdict", "rank", "differing", "line", "result"))
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.precheck_pairs (precheck_id, n, spec_id, label, target_a, relation_a, target_b,
                   relation_b, status, verdict, rank, differing, line, result, finished_at)
               VALUES (%(precheck_id)s, %(n)s, %(spec_id)s, %(label)s, %(target_a)s, %(relation_a)s,
                   %(target_b)s, %(relation_b)s,
                   CASE WHEN %(verdict)s::text IS NULL THEN 'pending' ELSE 'done' END, %(verdict)s, %(rank)s,
                   %(differing)s, %(line)s, %(result)s,
                   CASE WHEN %(verdict)s::text IS NULL THEN NULL ELSE now() END)""",
            [
                settled
                | dict(pair)
                | {"precheck_id": precheck["precheck_id"]}
                | {"result": _jsonb(pair["result"]) if pair.get("result") is not None else None}
                for pair in pairs
            ],
        )
    return stored


async def get_precheck(conn: Conn, precheck_id: str) -> Row | None:
    """The pre-check with its pairs counted (``_PRECHECK``)."""
    return await _row(conn, f"{_PRECHECK} WHERE p.precheck_id = %s", (precheck_id,))


async def list_prechecks(conn: Conn, *, limit: int = 50, cursor: str | None = None) -> Page:
    """The pre-checks newest first, each with its pairs counted (``_PRECHECK``)."""
    rows = await _rows(
        conn,
        f"""{_PRECHECK} WHERE (%(after)s::text IS NULL OR p.precheck_id < %(after)s)
            ORDER BY p.precheck_id DESC LIMIT %(limit)s""",
        {"after": cursor, "limit": limit + 1},
    )
    return _page(rows, limit, "precheck_id")


async def count_open_prechecks(conn: Conn, *, lock: bool = False) -> int:
    """How many pre-checks are unfinished (prechecks_open_idx); ``lock`` first takes the pre-checks lock
    until the transaction ends, so that creates count one at a time."""
    if lock:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PRECHECKS_LOCK,))
    return await _value(conn, "SELECT count(*) FROM ds.prechecks WHERE finished_at IS NULL", None)


async def precheck_pairs(
    conn: Conn,
    precheck_id: str,
    *,
    verdict: str | None = None,
    results: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """The pre-check's pairs worst first (precheck_pairs_worst_idx: by rank, then the most differing
    figures, then label; a pair not settled comes last), without their ``result`` unless ``results`` (a
    report's); those of ``verdict``, if given. The cursor is a position in that order, which a pair
    settled between two pages moves."""
    start = max(_after(cursor, 1)[0], 0)
    columns: LiteralString = _PAIR_COLUMNS + (", result" if results else "")
    rows = await _rows(
        conn,
        f"""SELECT {columns} FROM ds.precheck_pairs
            WHERE precheck_id = %(precheck_id)s AND (%(verdict)s::text IS NULL OR verdict = %(verdict)s)
            ORDER BY rank, differing DESC, label, n LIMIT %(limit)s OFFSET %(start)s""",  # noqa: S608
        {"precheck_id": precheck_id, "verdict": verdict, "limit": limit + 1, "start": start},
    )
    return rows[:limit], str(start + limit) if len(rows) > limit else None


async def precheck_pair(conn: Conn, precheck_id: str, n: int) -> Row | None:
    """One pair, with its ``result``."""
    return await _row(
        conn, "SELECT * FROM ds.precheck_pairs WHERE precheck_id = %s AND n = %s", (precheck_id, n)
    )


async def prechecks_due(conn: Conn) -> list[Row]:
    """The pairs the worker has to read or is reading (precheck_pairs_due_idx), the oldest pre-check's
    first: each one's precheck_id, n, status, tries and two targets. A pair of a cancelled pre-check is
    not among them."""
    return await _rows(
        conn,
        """SELECT precheck_id, n, status, tries, target_a, target_b FROM ds.precheck_pairs
           WHERE status IN ('pending', 'running') ORDER BY precheck_id, n""",
    )


async def claim_pair(conn: Conn, precheck_id: str, n: int) -> Row | None:
    """Take the pending pair of an unfinished pre-check to read it (one more of its ``tries``, claimed now):
    the pair without its ``result``, with its pre-check's ``source``, ``level`` and ``request`` (the schemas
    of two paired by name). None when it is not pending."""
    return await _row(
        conn,
        """UPDATE ds.precheck_pairs q SET status = 'running', tries = q.tries + 1, claimed_at = now()
           FROM ds.prechecks p
           WHERE q.precheck_id = %s AND q.n = %s AND q.status = 'pending'
             AND p.precheck_id = q.precheck_id AND p.finished_at IS NULL
           RETURNING q.precheck_id, q.n, q.spec_id, q.label, q.target_a, q.relation_a, q.target_b,
                     q.relation_b, q.tries, p.source, p.level, p.request""",
        (precheck_id, n),
    )


async def hold_pair(conn: Conn, precheck_id: str, n: int) -> None:
    """Renew the claim of the running pair: a worker reads it still."""
    await conn.execute(
        """UPDATE ds.precheck_pairs SET claimed_at = now()
           WHERE precheck_id = %s AND n = %s AND status = 'running'""",
        (precheck_id, n),
    )


async def save_pair(
    conn: Conn,
    precheck_id: str,
    n: int,
    *,
    verdict: str,
    rank: int,
    differing: int,
    line: str,
    result: Mapping[str, Any],
    rows_a: int | None = None,
    rows_b: int | None = None,
) -> bool:
    """Settle the running pair with what was read of it, replacing its result whole, in one statement.
    It takes the pre-check's row lock first, until the transaction ends, so that ``finish_precheck`` in
    the same transaction sees every pair another worker task settled. False, nothing written, when the
    pair is no longer running (its pre-check was cancelled, or its items were requeued)."""
    settled = await _changed(
        conn,
        """WITH p AS (SELECT precheck_id FROM ds.prechecks WHERE precheck_id = %(precheck_id)s FOR UPDATE)
           UPDATE ds.precheck_pairs q SET status = 'done', verdict = %(verdict)s, rank = %(rank)s,
               differing = %(differing)s, rows_a = %(rows_a)s, rows_b = %(rows_b)s, line = %(line)s,
               result = %(result)s, finished_at = now()
           FROM p WHERE q.precheck_id = p.precheck_id AND q.n = %(n)s AND q.status = 'running'""",
        {"precheck_id": precheck_id, "n": n, "verdict": verdict, "rank": rank, "differing": differing,
         "rows_a": rows_a, "rows_b": rows_b, "line": line, "result": _jsonb(result)},
    )  # fmt: skip
    return bool(settled)


async def settle_target(
    conn: Conn,
    target: str,
    verdict: str,
    rank: int,
    a: tuple[str, Mapping[str, Any]],
    b: tuple[str, Mapping[str, Any]],
    *,
    precheck_id: str | None = None,
) -> list[Row]:
    """Settle, in one statement, every pending pair that names ``target`` of every unfinished pre-check
    (of ``precheck_id`` alone, if given), unread: ``a`` is the line and result of a pair whose side a is
    on the target, ``b`` those of any other. It takes those pre-checks' row locks first, as ``save_pair``
    does. Each settled pair's precheck_id and n."""
    return await _rows(
        conn,
        """WITH p AS (SELECT precheck_id FROM ds.prechecks
                      WHERE finished_at IS NULL
                        AND (%(precheck_id)s::text IS NULL OR precheck_id = %(precheck_id)s)
                      ORDER BY precheck_id FOR UPDATE)
           UPDATE ds.precheck_pairs q SET status = 'done', verdict = %(verdict)s, rank = %(rank)s,
               differing = 0, finished_at = now(),
               line = CASE WHEN q.target_a = %(target)s THEN %(line_a)s ELSE %(line_b)s END,
               result = CASE WHEN q.target_a = %(target)s THEN %(result_a)s::jsonb
                             ELSE %(result_b)s::jsonb END
           FROM p
           WHERE q.precheck_id = p.precheck_id AND q.status = 'pending'
             AND %(target)s IN (q.target_a, q.target_b)
           RETURNING q.precheck_id, q.n""",
        {"precheck_id": precheck_id, "target": target, "verdict": verdict, "rank": rank,
         "line_a": a[0], "result_a": _jsonb(a[1]), "line_b": b[0], "result_b": _jsonb(b[1])},
    )  # fmt: skip


async def requeue_prechecks(conn: Conn, pairs: Sequence[tuple[str, int]] | None = None) -> int:
    """Set every running pair back to pending, to be read again: the worker that read them is gone (this
    one has just taken the worker lock). With ``pairs`` (each a precheck_id and n), those of them that
    are running: no task of this worker reads them, or this worker stops reading them. How many."""
    ids, ns = [p for p, _ in pairs or ()], [n for _, n in pairs or ()]
    return await _changed(
        conn,
        """UPDATE ds.precheck_pairs SET status = 'pending'
           WHERE status = 'running' AND (%(all)s OR (precheck_id, n) IN (
               SELECT * FROM unnest(%(ids)s::text[], %(ns)s::int[])))""",
        {"all": pairs is None, "ids": ids, "ns": ns},
    )


async def cancel_precheck(conn: Conn, precheck_id: str, by: str, line: str) -> Row | None:
    """Cancel the unfinished pre-check as ``by``: it ends now, and its pending and running pairs are
    cancelled, each saying ``line``. Its ``cancelled_at`` and how many pairs were ``pending`` and
    ``running``; None when it has ended, or does not exist."""
    cancelled = await _row(
        conn,
        """UPDATE ds.prechecks SET cancelled_by = %s, cancelled_at = now(), finished_at = now()
           WHERE precheck_id = %s AND finished_at IS NULL RETURNING precheck_id, cancelled_at""",
        (by, precheck_id),
    )
    if cancelled is None:
        return None
    counts = await _row(
        conn,
        """WITH open AS (SELECT n, status FROM ds.precheck_pairs
                         WHERE precheck_id = %(precheck_id)s AND status IN ('pending', 'running') FOR UPDATE),
                done AS (UPDATE ds.precheck_pairs q SET status = 'cancelled', line = %(line)s,
                             finished_at = now()
                         FROM open WHERE q.precheck_id = %(precheck_id)s AND q.n = open.n
                         RETURNING open.status)
           SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                  count(*) FILTER (WHERE status = 'running') AS running FROM done""",
        {"precheck_id": precheck_id, "line": line},
    )
    return cancelled | (counts or {})


async def finish_precheck(conn: Conn, precheck_id: str) -> Row | None:
    """End the pre-check once none of its pairs is pending or running: what ``precheck.finished`` records
    (its ``pairs``, its ``verdicts`` counted, the pairs each failure code of a side's read was met in,
    ``failed``, and the ``seconds`` since it was made). None while a pair is open, or when it has ended."""
    return await _row(
        conn,
        """UPDATE ds.prechecks p SET finished_at = now()
           WHERE p.precheck_id = %s AND p.finished_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM ds.precheck_pairs
                             WHERE precheck_id = p.precheck_id AND status IN ('pending', 'running'))
           RETURNING p.pairs, floor(extract(epoch FROM now() - p.created_at))::int AS seconds,
               (SELECT coalesce(jsonb_object_agg(v.verdict, v.n), '{}')
                FROM (SELECT verdict, count(*) AS n FROM ds.precheck_pairs
                      WHERE precheck_id = p.precheck_id AND verdict IS NOT NULL GROUP BY verdict) v
               ) AS verdicts,
               (SELECT coalesce(jsonb_object_agg(f.code, f.n), '{}')
                FROM (SELECT c.value AS code, count(DISTINCT q.n) AS n
                      FROM ds.precheck_pairs q, jsonb_each(q.result->'sides') s,
                           jsonb_each_text(s.value->'failed') c
                      WHERE q.precheck_id = p.precheck_id GROUP BY c.value) f) AS failed""",
        (precheck_id,),
    )


async def prune_prechecks(conn: Conn, days: int, limit: int = 1000) -> int:
    """Delete the pre-checks made over ``days`` days ago with their pairs (prechecks_created_idx), the
    oldest ``limit`` of them; how many. Their audit rows stay."""
    return await _changed(
        conn,
        """DELETE FROM ds.prechecks WHERE precheck_id IN (
               SELECT precheck_id FROM ds.prechecks WHERE created_at < now() - %s * interval '1 day'
               ORDER BY created_at LIMIT %s)""",
        (days, limit),
    )
