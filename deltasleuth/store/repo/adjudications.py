"""The queries of adjudications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, LiteralString

from deltasleuth.store.repo.base import Conn, Row, _jsonb, _one, _row, _rows, _sortable_id, _value

_ADJUDICATION_COLUMNS: LiteralString = """j.*, ds.actor_name(j.drawn_by) AS drawn_by_name,
    ds.actor_name(j.scored_by) AS scored_by_name"""


async def insert_adjudication(
    conn: Conn,
    run_id: str,
    cycle: int,
    *,
    salt: str,
    limit: int,
    selection: Mapping[str, Any],
    sheet: str,
    drawn_by: str,
) -> Row:
    """A reviewer sheet (``J-`` id) drawn from the ended cycle: its selection and blank sheet."""
    return await _one(
        conn,
        """INSERT INTO ds.adjudications (adjudication_id, run_id, cycle, salt, sample_limit, selection, sheet,
               drawn_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (_sortable_id("J-"), run_id, cycle, salt, limit, _jsonb(selection), sheet, drawn_by),
    )


async def get_adjudication(conn: Conn, adjudication_id: str) -> Row | None:
    """The adjudication with its actors' names."""
    return await _row(
        conn,
        f"SELECT {_ADJUDICATION_COLUMNS} FROM ds.adjudications j WHERE j.adjudication_id = %s",  # noqa: S608
        (adjudication_id,),
    )


async def list_adjudications(conn: Conn, run_id: str, cycle: int | None = None) -> list[Row]:
    """Every adjudication of the ended cycle (of every cycle of the run by default), newest first, with its
    actors' names."""
    return await _rows(
        conn,
        f"""SELECT {_ADJUDICATION_COLUMNS} FROM ds.adjudications j
            WHERE j.run_id = %(run_id)s AND (%(cycle)s::int IS NULL OR j.cycle = %(cycle)s)
            ORDER BY j.adjudication_id DESC""",  # noqa: S608
        {"run_id": run_id, "cycle": cycle},
    )


async def score_adjudication(
    conn: Conn,
    adjudication_id: str,
    *,
    sheet: str,
    upload_sha256: str,
    sheet_sha256: str,
    score: Mapping[str, Any],
    score_sha256: str,
    passed: bool,
    scored_by: str,
) -> Row | None:
    """Record the adjudication's one score and its reviewed sheet; None when it is scored already."""
    return await _row(
        conn,
        """UPDATE ds.adjudications SET sheet = %s, upload_sha256 = %s, sheet_sha256 = %s, score = %s,
               score_sha256 = %s, passed = %s, scored_by = %s, scored_at = now()
           WHERE adjudication_id = %s AND scored_at IS NULL RETURNING *""",
        (sheet, upload_sha256, sheet_sha256, _jsonb(score), score_sha256, passed, scored_by, adjudication_id),
    )


async def open_signoff(conn: Conn, run_id: str, cycle: int) -> str | None:
    """The ended cycle's requested or approved sign-off, if any, read under the cycle's row lock: a draw or
    score holding it cannot miss a sign-off request, which waits for it (and it for the request)."""
    await conn.execute("SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle = %s FOR UPDATE", (run_id, cycle))
    return await _value(
        conn,
        """SELECT approval_id FROM ds.approvals
           WHERE kind = 'signoff' AND run_id = %s AND cycle = %s AND status IN ('requested', 'approved')
           ORDER BY approval_id LIMIT 1""",
        (run_id, cycle),
    )


async def attested_adjudication(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The adjudication a sign-off request of the ended cycle attests: its newest scored by a person. The
    cycle's row is read FOR KEY SHARE first, so a draw or score in flight commits before this read."""
    await conn.execute(
        "SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle = %s FOR KEY SHARE", (run_id, cycle)
    )
    return await _row(
        conn,
        """SELECT * FROM ds.adjudications
           WHERE run_id = %s AND cycle = %s AND scored_at IS NOT NULL AND scored_by LIKE 'user:%%'
           ORDER BY adjudication_id DESC LIMIT 1""",
        (run_id, cycle),
    )


async def cycle_adjudication(conn: Conn, run_id: str, cycle: int, adjudication_id: str) -> Row | None:
    """The adjudication of that id, if it is the ended cycle's."""
    return await _row(
        conn,
        "SELECT * FROM ds.adjudications WHERE adjudication_id = %s AND run_id = %s AND cycle = %s",
        (adjudication_id, run_id, cycle),
    )


# A finding ``f``'s severity as ``reports.data.rank`` ranks it: CRITICAL 4 to INFO 0, none 0.
_RANK: LiteralString = """CASE f.severity WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2
    WHEN 'LOW' THEN 1 ELSE 0 END"""
# A key's findings ``f`` in the order whose first with a root cause names its primary one
# (``adjudicate.primary_root_cause``): unsuppressed above INFO, then suppressed, then the rest; the most
# severe first; then by pointer, compared by code point, and in finding order.
_PRIMARY_ORDER: LiteralString = f"""CASE WHEN f.suppressed THEN 1 WHEN {_RANK} > 0 THEN 0 ELSE 2 END,
    {_RANK} DESC, f.json_pointer COLLATE "C", f.seq"""
# A key ``k`` hashed with ``%(salt)s`` as ``adjudicate._hash`` hashes it.
_SHEET_DIGEST: LiteralString = "sha256(convert_to(%(salt)s || chr(31) || k.key_masked, 'UTF8'))"


async def adjudication_causes(conn: Conn, run_id: str, cycle: int) -> list[Row]:
    """Each root cause the cycle's findings name (``root_cause_id``; an empty one names none), the highest
    rank of their severities (``severity``) and how many keys hold it (``spread``): one row per root
    cause."""
    return await _rows(
        conn,
        f"""SELECT f.root_cause_id, max({_RANK}) AS severity, count(DISTINCT f.key_masked) AS spread
            FROM ds.findings f WHERE f.run_id = %s AND f.cycle = %s AND f.root_cause_id <> ''
            GROUP BY f.root_cause_id""",  # noqa: S608 - fixed SQL fragments only
        (run_id, cycle),
    )


async def adjudication_draw(
    conn: Conn, run_id: str, cycle: int, *, salt: str, limit: int, match_quota: int, prefetch: int
) -> list[Row]:
    """The keys a reviewer sheet's draw chooses among, in one statement that reads the cycle's results and
    its findings with a root cause once: each key with its ``outcome``, its primary root cause
    (``primary_cause``) and its ``digest`` under ``salt``. Its joins are hash or merge joins whatever rows
    the planner expects (``enable_nestloop`` off until the caller's transaction ends, which holds this
    statement alone): a nested loop over the cycle's rows is quadratic. Rows by ``kind``, each in digest
    order: ``first`` (of each root cause, the first key whose primary root cause it is), ``holding`` (of
    each root cause that is no key's primary one, the first ``prefetch`` keys holding it), ``outcome`` (of
    MATCH the first ``match_quota`` keys, of each other outcome the first) and ``stratum`` (the first
    ``limit`` keys taken in turn from each stratum, outcome and primary root cause: the first key of each
    stratum by name, then the second, and so on; with its ``stratum`` and ``place`` in it)."""
    await conn.execute("SELECT set_config('enable_nestloop', 'off', true)")
    return await _rows(
        conn,
        f"""WITH keyed AS MATERIALIZED (
                SELECT k.key_masked, k.outcome::text AS outcome, p.primary_cause, {_SHEET_DIGEST} AS digest,
                       k.outcome || '/' || coalesce(p.primary_cause, '-') AS stratum
                FROM ds.results k LEFT JOIN (
                    SELECT DISTINCT ON (f.key_masked) f.key_masked, f.root_cause_id AS primary_cause
                    FROM ds.findings f
                    WHERE f.run_id = %(r)s AND f.cycle = %(c)s AND f.root_cause_id <> ''
                    ORDER BY f.key_masked, {_PRIMARY_ORDER}) AS p USING (key_masked)
                WHERE k.run_id = %(r)s AND k.cycle = %(c)s),
            held AS MATERIALIZED (
                SELECT DISTINCT f.key_masked, f.root_cause_id FROM ds.findings f
                WHERE f.run_id = %(r)s AND f.cycle = %(c)s AND f.root_cause_id <> ''
                  AND f.root_cause_id NOT IN (
                      SELECT primary_cause FROM keyed WHERE primary_cause IS NOT NULL))
            (SELECT DISTINCT ON (primary_cause) 'first' AS kind, primary_cause AS root_cause_id, key_masked,
                    outcome, primary_cause, digest, NULL::text AS stratum, NULL::bigint AS place
             FROM keyed WHERE primary_cause IS NOT NULL
             ORDER BY primary_cause, digest, key_masked COLLATE "C")
            UNION ALL
            (SELECT 'holding', root_cause_id, key_masked, outcome, primary_cause, digest, NULL, NULL
             FROM (SELECT h.root_cause_id, k.*,
                          row_number() OVER (PARTITION BY h.root_cause_id
                                             ORDER BY k.digest, k.key_masked COLLATE "C") AS place
                   FROM held h JOIN keyed k USING (key_masked)) AS h
             WHERE place <= %(prefetch)s)
            UNION ALL
            (SELECT 'outcome', NULL, key_masked, outcome, primary_cause, digest, NULL, NULL
             FROM (SELECT k.*, row_number() OVER (PARTITION BY outcome
                                                  ORDER BY digest, key_masked COLLATE "C") AS place
                   FROM keyed k) AS k
             WHERE place <= CASE WHEN outcome = 'MATCH' THEN %(match_quota)s ELSE 1 END)
            UNION ALL
            (SELECT 'stratum', NULL, key_masked, outcome, primary_cause, digest, stratum, place
             FROM (SELECT k.*, row_number() OVER (PARTITION BY stratum
                                                  ORDER BY digest, key_masked COLLATE "C") AS place
                   FROM keyed k) AS k
             ORDER BY place, stratum COLLATE "C" LIMIT %(limit)s)""",  # noqa: S608 - fixed SQL fragments only
        {"r": run_id, "c": cycle, "salt": salt, "limit": limit, "match_quota": match_quota,
         "prefetch": prefetch},
    )  # fmt: skip


async def adjudication_holders(
    conn: Conn, run_id: str, cycle: int, root_cause_id: str, *, salt: str, limit: int
) -> list[Row]:
    """The first ``limit`` keys in digest order of those holding ``root_cause_id``, as
    ``adjudication_draw`` gives a key. The root cause's findings are read by its index, and each of its
    keys' result and findings by key."""
    return await _rows(
        conn,
        f"""SELECT k.key_masked, r.outcome, p.primary_cause, {_SHEET_DIGEST} AS digest
            FROM (SELECT DISTINCT f.run_id, f.cycle, f.key_masked FROM ds.findings f
                  WHERE f.run_id = %(r)s AND f.cycle = %(c)s AND f.root_cause_id = %(rc)s) AS k
            CROSS JOIN LATERAL (
                SELECT r.outcome::text AS outcome FROM ds.results r
                WHERE (r.run_id, r.cycle, r.key_masked) = (k.run_id, k.cycle, k.key_masked) OFFSET 0) r
            CROSS JOIN LATERAL (
                SELECT f.root_cause_id AS primary_cause FROM ds.findings f
                WHERE (f.run_id, f.cycle, f.key_masked) = (k.run_id, k.cycle, k.key_masked)
                  AND f.root_cause_id <> ''
                ORDER BY {_PRIMARY_ORDER} LIMIT 1) p
            ORDER BY digest, k.key_masked COLLATE "C" LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"r": run_id, "c": cycle, "rc": root_cause_id, "salt": salt, "limit": limit},
    )
