"""The queries of cut-over readiness."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, LiteralString

from psycopg.rows import dict_row

from deltasleuth.core.types import Verdict
from deltasleuth.store.repo.base import (
    _DIFFERENT,
    Conn,
    Page,
    Row,
    _after,
    _changed,
    _jsonb,
    _one,
    _page,
    _rows,
    _value,
    _verdict_is,
)

# An ended cycle readiness has still to measure, as cycles_unmeasured_idx holds them: never measured, or
# failed under five times; the second is due again 1, 2, 4 and 8 hours after its last try.
_UNMEASURED: LiteralString = "(c.readiness IS NULL OR (c.readiness ? 'error' AND NOT c.readiness ? 'final'))"
_RETRY_DUE: LiteralString = """(c.readiness->>'measured_at')::timestamptz
    + interval '1 hour' * (1 << ((c.readiness->>'attempts')::int - 1)) <= now()"""
_BEFORE_014: LiteralString = "c.ended_at < (SELECT applied_at FROM ds.schema_version WHERE version = 14)"
_TIERS = ("standard", "high")  # core.sampling.TARGETS: each has its breaks index
COUNTED = 100  # a pair's cycles are counted up to this many (core.readiness.COUNTED), and one more
OWED = 100_000  # the installation's cycles still to measure, likewise
# Measures as this build reads them (core.readiness.FORMAT): an evidence point, as the predicate of
# cycles_readiness_evidence_idx says it, and a record of another format, an unknown point that
# cycles_readiness_unread_idx holds.
_EVIDENCE: LiteralString = """c.readiness @> '{"evidence": true, "format": 1}'"""
_UNREAD: LiteralString = "c.readiness ? 'pair' AND c.readiness->'format' <> '1'"
# The newest points of the pair of the row ``a`` (pair, tier, until) failing at its tier, a probe of that
# tier's breaks index; ``{more}`` narrows it and ``{also}`` adds a column.
_FAILING: LiteralString = """(SELECT c.run_id, c.cycle, c.ended_at{also} FROM ds.cycles c
         WHERE a.tier = '{tier}' AND c.readiness->>'pair' = a.pair AND c.readiness->'fails'->'{tier}' <> '[]'
           AND c.ended_at <= coalesce(a.until, 'infinity'){more}
         ORDER BY c.ended_at DESC LIMIT {limit})"""
_FAILING_GATE: LiteralString = """ AND c.readiness->'fails'->'{tier}' ? g.gate AND c.readiness->'format' = '1'
           AND c.readiness->>'spec_id' = a.spec AND c.ended_at > a.evidence_at"""
# Whether the point fails a per-cycle gate, ``unknown`` aside: a record of another format never tells.
_FAILED: LiteralString = """, CASE WHEN c.readiness->'format' = '1'
                THEN (c.readiness->'fails'->'{tier}') - 'unknown' <> '[]' ELSE false END"""


def _failing(more: str = "", also: str = "", limit: str = "1") -> str:
    return " UNION ALL ".join(
        _FAILING.format(tier=tier, more=more.format(tier=tier), also=also.format(tier=tier), limit=limit)
        for tier in _TIERS
    )


def _timed(facts: Row | None, *names: str) -> Row | None:
    """Facts read from jsonb, the times among them as datetimes again."""
    if facts is None:
        return None
    return facts | {name: datetime.fromisoformat(facts[name]) for name in names if facts.get(name)}


async def unmeasured_cycles(conn: Conn, *, backfill: bool = False) -> list[Row]:
    """The next ended cycles readiness measures, newest first: those ended since 014, up to 10; or,
    ``backfill``, before it: up to 10, and with those whose purge started, which are read from the
    tombstone alone, up to 100. Each ``run_id``, ``cycle``, ``ended_at``, the run's ``pair`` (its spec's
    name) and ``spec_id``. A cycle whose measure failed comes again 1, 2, 4 and 8 hours later."""
    return await _rows(
        conn,
        f"""SELECT run_id, cycle, ended_at, pair, spec_id
            FROM (SELECT c.run_id, c.cycle, c.ended_at, s.name AS pair, r.spec_id,
                         count(*) FILTER (WHERE c.purging_at IS NULL)
                             OVER (ORDER BY c.ended_at DESC, c.run_id, c.cycle) AS whole
                  FROM ds.cycles c JOIN ds.runs r USING (run_id) JOIN ds.specs s USING (spec_id)
                  WHERE {_UNMEASURED} AND (c.readiness IS NULL OR {_RETRY_DUE})
                    AND ({_BEFORE_014}) = %(backfill)s
                  ORDER BY c.ended_at DESC, c.run_id, c.cycle LIMIT %(most)s) due
            WHERE whole <= 10 ORDER BY ended_at DESC, run_id, cycle""",  # noqa: S608 - fixed SQL fragments only
        {"backfill": backfill, "most": 100 if backfill else 10},
    )


async def backfill_owed(conn: Conn) -> bool:
    """Whether a cycle ended before 014 is still to measure (no transition is written until none is):
    one probe of cycles_unmeasured_idx."""
    return await _value(
        conn,
        f"SELECT EXISTS (SELECT 1 FROM ds.cycles c WHERE {_UNMEASURED} AND {_BEFORE_014})",  # noqa: S608
        None,
    )


async def unmeasured_pairs(conn: Conn, pairs: Sequence[str]) -> tuple[Row, dict[str, Row]]:
    """The ended cycles readiness has still to measure. Of the installation, how many were never measured
    (``cycles``) and how many ended before 014 (``backfill``: those and the ones to be tried again; while
    any is left, no transition is written), each counted up to ``OWED`` and one more. And each of
    ``pairs`` with cycles never measured: how many (``cycles``, up to ``COUNTED`` and one more) and the
    ``oldest`` (``{run_id, cycle, ended_at}``). The cycles ended since 014 that wait are few, and are read
    whole; while a backfill is owed, each run of the pairs is probed instead (cycles_unmeasured_run_idx),
    so that the read does not grow with what is owed."""
    waiting: LiteralString = "FROM ds.cycles c WHERE c.run_id = r.run_id AND c.readiness IS NULL"
    owed = await _one(
        conn,
        f"""SELECT (SELECT count(*) FROM (SELECT 1 FROM ds.cycles c WHERE c.readiness IS NULL
                                           LIMIT %(most)s) n) AS cycles,
                   (SELECT count(*) FROM (SELECT 1 FROM ds.cycles c WHERE {_UNMEASURED} AND {_BEFORE_014}
                                           LIMIT %(most)s) b) AS backfill""",  # noqa: S608 - fixed fragments
        {"most": OWED + 1},
    )
    if owed["backfill"]:
        rows = await _rows(
            conn,
            f"""SELECT a.pair, least(sum(o.cycles), %(most)s)::int AS cycles, min(o.ended_at) AS ended_at,
                       (array_agg(ARRAY[o.run_id, o.cycle::text] ORDER BY o.ended_at, o.run_id, o.cycle))[1:1]
                           AS oldest
                FROM unnest(%(pairs)s::text[]) AS a (pair)
                JOIN ds.specs s ON s.name = a.pair JOIN ds.runs r ON r.spec_id = s.spec_id
                CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle, c.ended_at,
                           (SELECT count(*) FROM (SELECT 1 {waiting} LIMIT %(most)s) m) AS cycles
                    {waiting} ORDER BY c.cycle LIMIT 1) o
                GROUP BY a.pair""",  # noqa: S608 - fixed SQL fragments only
            {"pairs": list(pairs), "most": COUNTED + 1},
        )
    else:
        rows = await _rows(
            conn,
            """SELECT s.name AS pair, count(*) AS cycles, min(c.ended_at) AS ended_at,
                      (array_agg(ARRAY[c.run_id, c.cycle::text] ORDER BY c.ended_at, c.run_id, c.cycle))[1:1]
                          AS oldest
               FROM ds.cycles c JOIN ds.runs r USING (run_id) JOIN ds.specs s USING (spec_id)
               WHERE c.readiness IS NULL GROUP BY s.name""",
        )
    found = {}
    for row in rows:
        (run_id, cycle), ended_at = row["oldest"][0], row["ended_at"]
        oldest = {"run_id": run_id, "cycle": int(cycle), "ended_at": ended_at}
        found[row["pair"]] = {"cycles": row["cycles"], "oldest": oldest}
    return owed, found


async def set_readiness(conn: Conn, run_id: str, cycle: int, record: Mapping[str, Any]) -> bool:
    """Store an ended cycle's readiness measures, once: over none, or over a failed try's; False when the
    cycle holds measures already."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.cycles SET readiness = %s
               WHERE run_id = %s AND cycle = %s AND (readiness IS NULL OR readiness ? 'error')""",
            (_jsonb(record), run_id, cycle),
        )
    )


_PREV_DIFFERENT_K = _verdict_is(Verdict.DIFFERENT, "k", "%(p)s")  # a result ``k`` of cycle ``%(p)s``


async def cycle_changes(conn: Conn, run_id: str, cycle: int) -> Row:
    """How the cycle's DIFFERENT keys (MISMATCH, NOT_FOUND_A or NOT_FOUND_B, or KNOWN_DIFF under a bug rule:
    those still owed a recheck too) moved since the cycle before: ``new`` in this one only,
    ``persistent`` in both, and of those in the one before only, ``fixed`` read MATCH in this one and
    ``other`` the rest (read otherwise, or not read: a table scan reads again every key that did not match
    before, so one without a result left both tables). Each cycle's discrepant keys come from
    results_outcome_idx; its joins are hash or merge joins whatever rows the planner expects
    (``enable_nestloop`` off for this statement, in the caller's transaction): the statistics do not know a
    cycle that has just ended, and a nested loop over its results is quadratic. So the cost is the
    discrepant keys of both cycles and, at worst, one pass over the cycle's results."""
    planned = await _value(
        conn, "SELECT current_setting('enable_nestloop'), set_config('enable_nestloop', 'off', true)", ()
    )
    found = await _one(
        conn,
        f"""WITH cur AS (SELECT k.key_masked FROM ds.results k
                         WHERE k.run_id = %(r)s AND k.cycle = %(c)s AND {_DIFFERENT}),
                 prev AS (SELECT k.key_masked FROM ds.results k
                          WHERE k.run_id = %(r)s AND k.cycle = %(p)s AND {_PREV_DIFFERENT_K})
            SELECT count(*) FILTER (WHERE prev.key_masked IS NULL) AS new,
                   count(*) FILTER (WHERE cur.key_masked IS NOT NULL AND prev.key_masked IS NOT NULL)
                       AS persistent,
                   count(*) FILTER (WHERE cur.key_masked IS NULL AND read.outcome = 'MATCH') AS fixed,
                   count(*) FILTER (WHERE cur.key_masked IS NULL AND read.outcome IS DISTINCT FROM 'MATCH')
                       AS other
            FROM cur FULL JOIN prev USING (key_masked)
            LEFT JOIN ds.results read
                 ON cur.key_masked IS NULL
                AND (read.run_id, read.cycle, read.key_masked) = (%(r)s, %(c)s, prev.key_masked)""",  # noqa: S608
        {"r": run_id, "c": cycle, "p": cycle - 1},
    )
    await conn.execute("SELECT set_config('enable_nestloop', %s, true)", (planned,))
    return found


async def graph_counts(conn: Conn, run_id: str, cycle: int) -> dict[str, int]:
    """The cycle's graph parity rows, counted by code."""
    rows = await _rows(
        conn,
        "SELECT code, count(*) AS n FROM ds.graph_diffs WHERE run_id = %s AND cycle = %s GROUP BY code",
        (run_id, cycle),
    )
    return {row["code"]: row["n"] for row in rows}


async def readiness_series(
    conn: Conn, pairs: Sequence[Mapping[str, Any]], *, whole: bool = False
) -> dict[str, dict[str, Any]]:
    """What each of ``pairs`` (``{pair, tier, needed, window, until}``: judged at ``tier``, needing
    ``needed`` evidence cycles in a row and counting ``window`` at most, on cycles ended up to ``until``,
    None for now) is judged from, in one statement whatever the history, each cycle as ``{run_id,
    cycle, ended_at, readiness}`` (a point's ``readiness`` only when ``whole``: a streak counts its points,
    and only a pair's detail lists them):

    - ``versions``: the newest evidence point of each version, newest first (a skip scan of
      cycles_readiness_evidence_idx); the first is the evidence cycle, its version the current one;
    - ``breaks``: the newest points failing at the tier, up to ``needed``, newest first (a probe of its
      breaks index), those whose measures are of another format among them (an unknown point each);
    - ``points``: the current version's evidence points after both the newest of the breaks that fails a
      gate other than ``unknown`` and the newest evidence point of another version, newest first, up to
      the window: ``core.readiness.streak`` then applies the unknown breaks;
    - ``trend``: the pair's last 10 evidence points, newest first (the last 10 of each of its 10 newest
      versions, merged);
    - ``critical`` and ``targets``: the newest point of the current version after the evidence cycle failing
      that gate at the tier (a probe each);
    - ``newest`` and ``cycles``: for a pair without evidence, its newest point and how many it has, counted
      up to ``COUNTED`` and one more.

    The evidence cycle, ``critical`` and ``targets`` also carry ``sample``, their stored judgement. The
    statement probes indexes a pair at a time, which the planner prices high enough to compile (``jit``),
    at several times what it then takes to run: so ``jit`` is off for the rest of the caller's transaction,
    an evaluation's other reads among it. It expects far more cycles than it finds, too, so each is read
    by its key (``OFFSET 0`` keeps that read a probe: joined, the planner would scan the table)."""
    newer = _failing(_FAILING_GATE)
    measured: LiteralString = """c.readiness->>'pair' = a.pair AND c.readiness ? 'pair'
                      AND c.ended_at <= coalesce(a.until, 'infinity')"""
    await conn.execute("SELECT set_config('jit', 'off', true)")
    rows = await _rows(
        conn,
        f"""WITH RECURSIVE asked AS (
                SELECT * FROM unnest(%(pairs)s::text[], %(tiers)s::text[], %(needed)s::int[],
                                     %(windows)s::int[], %(untils)s::timestamptz[])
                    AS a (pair, tier, k, w, until)),
            versions AS (
                SELECT a.pair, a.until, v.* FROM asked a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle, c.ended_at, c.readiness->>'spec_id' AS spec FROM ds.cycles c
                    WHERE c.readiness->>'pair' = a.pair AND {_EVIDENCE}
                      AND c.ended_at <= coalesce(a.until, 'infinity')
                    ORDER BY c.readiness->>'spec_id', c.ended_at DESC LIMIT 1) v
                UNION ALL
                SELECT a.pair, a.until, v.* FROM versions a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle, c.ended_at, c.readiness->>'spec_id' AS spec FROM ds.cycles c
                    WHERE c.readiness->>'pair' = a.pair AND {_EVIDENCE}
                      AND c.readiness->>'spec_id' > a.spec AND c.ended_at <= coalesce(a.until, 'infinity')
                    ORDER BY c.readiness->>'spec_id', c.ended_at DESC LIMIT 1) v),
            ranked AS (
                SELECT v.*, row_number() OVER (PARTITION BY v.pair
                                               ORDER BY v.ended_at DESC, v.run_id DESC, v.cycle DESC) AS n
                FROM versions v),
            breaks AS (
                SELECT a.pair, b.* FROM asked a CROSS JOIN LATERAL (
                    SELECT * FROM (
                        {_failing(also=_FAILED, limit="a.k")}
                        UNION
                        (SELECT c.run_id, c.cycle, c.ended_at, false FROM ds.cycles c
                         WHERE c.readiness->>'pair' = a.pair AND {_UNREAD}
                           AND c.ended_at <= coalesce(a.until, 'infinity')
                         ORDER BY c.ended_at DESC LIMIT a.k)) f (run_id, cycle, ended_at, failed)
                    ORDER BY f.ended_at DESC, f.run_id DESC, f.cycle DESC LIMIT a.k) b),
            failures AS (SELECT pair, max(ended_at) AS ended_at FROM breaks WHERE failed GROUP BY pair),
            bounds AS (
                SELECT a.*, cur.spec, cur.ended_at AS evidence_at,
                       greatest(b.ended_at, other.ended_at) AS after
                FROM asked a JOIN ranked cur ON cur.pair = a.pair AND cur.n = 1
                LEFT JOIN ranked other ON other.pair = a.pair AND other.n = 2
                LEFT JOIN failures b ON b.pair = a.pair),
            found AS (
                SELECT 'version' AS kind, pair, run_id, cycle, n FROM ranked
                UNION ALL
                SELECT 'break', pair, run_id, cycle, 0 FROM breaks
                UNION ALL
                SELECT 'point', a.pair, p.run_id, p.cycle, 0 FROM bounds a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle FROM ds.cycles c
                    WHERE c.readiness->>'pair' = a.pair AND c.readiness->>'spec_id' = a.spec
                      AND {_EVIDENCE}
                      AND c.ended_at > coalesce(a.after, '-infinity')
                      AND c.ended_at <= coalesce(a.until, 'infinity')
                    ORDER BY c.ended_at DESC LIMIT a.w) p
                UNION ALL
                SELECT 'trend', t.pair, t.run_id, t.cycle, 0 FROM (
                    SELECT a.pair, p.run_id, p.cycle,
                           row_number() OVER (PARTITION BY a.pair
                                              ORDER BY p.ended_at DESC, p.run_id DESC, p.cycle DESC) AS n
                    FROM ranked a CROSS JOIN LATERAL (
                        SELECT c.run_id, c.cycle, c.ended_at FROM ds.cycles c
                        WHERE c.readiness->>'pair' = a.pair AND c.readiness->>'spec_id' = a.spec
                          AND {_EVIDENCE}
                          AND c.ended_at <= coalesce(a.until, 'infinity')
                        ORDER BY c.ended_at DESC LIMIT 10) p
                    WHERE a.n <= 10) t
                WHERE t.n <= 10
                UNION ALL
                SELECT g.gate, a.pair, f.run_id, f.cycle, 0
                FROM bounds a CROSS JOIN (VALUES ('critical'), ('targets')) AS g (gate)
                CROSS JOIN LATERAL ({newer}) f
                UNION ALL
                SELECT 'newest', a.pair, p.run_id, p.cycle, p.cycles FROM asked a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle,
                           (SELECT count(*) FROM (SELECT 1 FROM ds.cycles c WHERE {measured}
                                                  ORDER BY c.ended_at DESC LIMIT %(most)s) m) AS cycles
                    FROM ds.cycles c
                    WHERE {measured}
                      AND NOT EXISTS (SELECT 1 FROM ranked v WHERE v.pair = a.pair)
                    ORDER BY c.ended_at DESC LIMIT 1) p)
            SELECT f.kind, f.pair, f.n, c.*
            FROM found f CROSS JOIN LATERAL (
                SELECT c.run_id, c.cycle, c.ended_at,
                       CASE WHEN f.kind <> 'point' OR %(whole)s THEN c.readiness END AS readiness,
                       CASE WHEN f.kind IN ('critical', 'targets') OR (f.kind = 'version' AND f.n = 1)
                            THEN c.sample END AS sample
                FROM ds.cycles c WHERE (c.run_id, c.cycle) = (f.run_id, f.cycle) OFFSET 0) c
            ORDER BY f.pair, f.kind, f.n, c.ended_at DESC, c.run_id DESC, c.cycle DESC""",  # noqa: S608
        {
            "pairs": [p["pair"] for p in pairs],
            "tiers": [p["tier"] for p in pairs],
            "needed": [p["needed"] for p in pairs],
            "windows": [p["window"] for p in pairs],
            "untils": [p["until"] for p in pairs],
            "whole": whole,
            "most": COUNTED + 1,
        },
    )
    lists = {"version": "versions", "break": "breaks", "point": "points", "trend": "trend"}
    empty = {"critical": None, "targets": None, "newest": None, "cycles": 0}
    found: dict[str, dict[str, Any]] = {
        p["pair"]: {"versions": [], "breaks": [], "points": [], "trend": []} | empty for p in pairs
    }
    for row in rows:
        kind, pair, n = row.pop("kind"), row.pop("pair"), row.pop("n")
        if kind in lists:
            found[pair][lists[kind]].append(row)
        else:
            found[pair] |= {kind: row} | ({"cycles": n} if kind == "newest" else {})
    return found


async def readiness_governance(conn: Conn, pairs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """What each of ``pairs`` (``{pair, spec_id, after, until, run_id, cycle}``: its current version, when
    its streak began, None for ever, the end it is judged up to, None for now, and its evidence cycle) has
    stored of governance and evidence, in one statement:

    - ``signoffs``: of the pair's sign-offs requested, approved or revoked, the newest of each status in
      the streak, of the current version outside it and of other versions, of evidence cycles and of
      others apart, and every one of the candidate cycles: ``{approval_id, status, run_id, cycle, spec_id,
      version, ended_at, decided_at, revoked_at, evidence, readiness}`` (``evidence``: its cycle is an
      evidence point, which alone is in a streak; ``readiness``: the measures of one that is not);
    - ``cycles``: the cycles the candidate is chosen from, evidence points all: those of the streak's
      newest approved sign-off and open request, its newest cycle holding a passed adjudication a person
      scored or a labelling a person labelled, and the evidence cycle. Each ``{run_id, cycle, ended_at,
      spec_id, readiness, sample, started, worked, cited, adjudication, labelling, packs}``: the auth mode
      its run ``started`` in; whether it holds such work; the spec approval a sign-off would ``cite`` and
      its status; its newest ``adjudication`` a person scored and why it did not pass; its ``labelling``,
      the one its open or approved sign-off attests (``attested``: that sign-off), else the newest a
      person labelled, else the newest labelled, as ``accuracy.gate`` reads it, without its actors' names;
      and its ``packs``, newest first;
    - ``soaks``: the live soaks of the current version, with their spec's ``execution`` (no compile)."""
    rows = await _rows(
        conn,
        """WITH asked AS (
               SELECT * FROM unnest(%(pairs)s::text[], %(specs)s::bigint[], %(afters)s::timestamptz[],
                                    %(untils)s::timestamptz[], %(runs)s::text[], %(cycles)s::int[])
                   AS a (pair, spec_id, after, until, run_id, cycle)),
           signoffs AS (
               SELECT a.pair, g.approval_id, g.status, g.run_id, g.cycle, g.spec_id, s.version, c.ended_at,
                      g.decided_at, g.revoked_at, e.evidence, h.held,
                      row_number() OVER (PARTITION BY a.pair, g.status, g.spec_id = a.spec_id, e.evidence,
                                                      h.held
                                         ORDER BY c.ended_at DESC, g.approval_id DESC) AS n
               FROM asked a JOIN ds.specs s ON s.name = a.pair
               JOIN ds.approvals g ON g.spec_id = s.spec_id AND g.kind = 'signoff'
                                  AND g.status IN ('requested', 'approved', 'revoked')
               JOIN ds.cycles c ON (c.run_id, c.cycle) = (g.run_id, g.cycle),
               LATERAL (SELECT coalesce(c.readiness @> '{"evidence": true, "format": 1}', false))
                   e (evidence),
               LATERAL (SELECT e.evidence AND g.spec_id = a.spec_id
                               AND c.ended_at > coalesce(a.after, '-infinity')) h (held)
               WHERE c.ended_at <= coalesce(a.until, 'infinity')),
           work AS (
               SELECT j.run_id, j.cycle FROM ds.adjudications j WHERE j.passed AND j.scored_by LIKE 'user:%%'
               UNION
               SELECT v.run_id, v.cycle FROM ds.labellings v WHERE v.labelled_by LIKE 'user:%%'),
           worked AS (
               SELECT DISTINCT ON (a.pair) a.pair, c.run_id, c.cycle
               FROM asked a JOIN ds.runs r ON r.spec_id = a.spec_id JOIN work w ON w.run_id = r.run_id
               JOIN ds.cycles c ON (c.run_id, c.cycle) = (w.run_id, w.cycle)
               WHERE c.readiness @> '{"evidence": true, "format": 1}'
                 AND c.ended_at > coalesce(a.after, '-infinity')
                 AND c.ended_at <= coalesce(a.until, 'infinity')
               ORDER BY a.pair, c.ended_at DESC, c.run_id DESC, c.cycle DESC),
           candidates AS (
               SELECT pair, run_id, cycle FROM signoffs WHERE n = 1 AND held AND status <> 'revoked'
               UNION SELECT pair, run_id, cycle FROM worked
               UNION SELECT pair, run_id, cycle FROM asked WHERE run_id IS NOT NULL)
           SELECT 'signoffs' AS kind, s.pair, jsonb_build_object(
                      'approval_id', s.approval_id, 'status', s.status, 'run_id', s.run_id, 'cycle', s.cycle,
                      'spec_id', s.spec_id, 'version', s.version, 'ended_at', s.ended_at,
                      'decided_at', s.decided_at, 'revoked_at', s.revoked_at, 'evidence', s.evidence,
                      'readiness', CASE WHEN NOT s.evidence THEN (
                          SELECT c.readiness FROM ds.cycles c
                          WHERE (c.run_id, c.cycle) = (s.run_id, s.cycle)) END) AS facts
           FROM signoffs s
           WHERE s.n = 1 OR EXISTS (SELECT 1 FROM candidates k
                                    WHERE (k.pair, k.run_id, k.cycle) = (s.pair, s.run_id, s.cycle))
           UNION ALL
           SELECT 'cycles', k.pair, jsonb_build_object(
                      'run_id', c.run_id, 'cycle', c.cycle, 'ended_at', c.ended_at, 'spec_id', r.spec_id,
                      'readiness', c.readiness, 'sample', c.sample,
                      'started', r.params->'approval'->>'mode',
                      'worked', EXISTS (SELECT 1 FROM work w WHERE (w.run_id, w.cycle) = (c.run_id, c.cycle)),
                      'cited', (
                          SELECT jsonb_build_object('approval_id', p.approval_id, 'status', p.status)
                          FROM ds.approvals p
                          WHERE p.approval_id = coalesce(c.approval_id, CASE
                              WHEN c.readiness->'needs_approval' = '[]' THEN (
                                  SELECT a.approval_id FROM ds.approvals a
                                  WHERE a.spec_id = r.spec_id AND a.kind = 'spec' AND a.status = 'approved')
                              END)),
                      'adjudication', (
                          SELECT jsonb_build_object('adjudication_id', j.adjudication_id, 'passed', j.passed,
                                                    'reasons', j.score->'exit'->'reasons')
                          FROM ds.adjudications j
                          WHERE (j.run_id, j.cycle) = (c.run_id, c.cycle) AND j.scored_at IS NOT NULL
                            AND j.scored_by LIKE 'user:%%'
                          ORDER BY j.adjudication_id DESC LIMIT 1),
                      'labelling', (
                          SELECT jsonb_build_object(
                              'labelling_id', v.labelling_id, 'run_id', v.run_id, 'cycle', v.cycle,
                              'drawn_at', v.drawn_at, 'labelled_at', v.labelled_at,
                              'labelled_by', v.labelled_by,
                              'truth_source', v.truth_source, 'truth_as_of', v.truth_as_of,
                              'role_a', s.body #>> '{sides,a,role}', 'role_b', s.body #>> '{sides,b,role}',
                              'accuracy', v.accuracy #- '{discrepant,by_cause}' #- '{accepted,by_rule}',
                              'attested', g.approval_id)
                          FROM ds.labellings v JOIN ds.specs s ON s.spec_id = r.spec_id
                          LEFT JOIN ds.approvals g
                               ON g.kind = 'signoff' AND (g.run_id, g.cycle) = (v.run_id, v.cycle)
                              AND g.status IN ('requested', 'approved')
                              AND g.attests->'accuracy'->>'labelling_id' = v.labelling_id
                          WHERE (v.run_id, v.cycle) = (c.run_id, c.cycle) AND v.labelled_at IS NOT NULL
                          ORDER BY g.approval_id IS NULL, v.labelled_by NOT LIKE 'user:%%',
                                   v.labelling_id DESC
                          LIMIT 1),
                      'packs', coalesce((
                          SELECT jsonb_agg(jsonb_build_object(
                                     'pack_id', p.pack_id, 'signoff_id', p.signoff_id, 'status', p.status,
                                     'issued_at', p.issued_at, 'revoked_at', p.revoked_at)
                                     ORDER BY p.pack_id DESC)
                          FROM ds.packs p WHERE (p.run_id, p.cycle) = (c.run_id, c.cycle)), '[]'))
           FROM candidates k JOIN ds.cycles c USING (run_id, cycle) JOIN ds.runs r USING (run_id)
           UNION ALL
           SELECT 'soaks', a.pair, jsonb_build_object(
                      'run_id', r.run_id, 'cycle', r.cycle, 'status', r.status,
                      'stop_reason', r.stop_reason, 'started_at', r.started_at,
                      'execution', s.body->'execution')
           FROM asked a JOIN ds.runs r ON r.spec_id = a.spec_id AND r.mode = 'soak'
                                      AND r.status IN ('running', 'paused')
           JOIN ds.specs s ON s.spec_id = r.spec_id""",
        {
            "pairs": [p["pair"] for p in pairs],
            "specs": [p["spec_id"] for p in pairs],
            "afters": [p["after"] for p in pairs],
            "untils": [p["until"] for p in pairs],
            "runs": [p["run_id"] for p in pairs],
            "cycles": [p["cycle"] for p in pairs],
        },
    )
    found: dict[str, dict[str, Any]] = {p["pair"]: {"signoffs": [], "cycles": [], "soaks": []} for p in pairs}
    for row in rows:
        facts = _timed(row["facts"], "ended_at", "decided_at", "revoked_at", "started_at")
        assert facts is not None
        if row["kind"] == "cycles":
            facts["labelling"] = _timed(facts["labelling"], "drawn_at", "labelled_at")
            facts["packs"] = [_timed(pack, "issued_at", "revoked_at") for pack in facts["packs"]]
        found[row["pair"]][row["kind"]].append(facts)
    return found


async def last_readiness(conn: Conn, pairs: Sequence[str], *, at: datetime | None = None) -> dict[str, Row]:
    """Each pair's newest ``readiness.changed`` row (a LATERAL on audit_log_object_idx), or its newest
    written by ``at``: its ``id``, ``at``, ``detail``, chain ``seq``, the ``spec_id`` of its evidence
    cycle (None without one), which tells a changed version, and ``since``, when the pair entered the
    state it records: the ``at`` of its newest row by then that changed the state (the rows since changed
    a gate only); a pair without one is left out."""
    rows = await _rows(
        conn,
        """SELECT p.pair, l.id, l.at, l.detail, (SELECT c.seq FROM ds.audit_chain c WHERE c.id = l.id) AS seq,
                  (SELECT (c.readiness->>'spec_id')::bigint FROM ds.cycles c
                   WHERE c.run_id = l.detail #>> '{evidence,run_id}'
                     AND c.cycle = (l.detail #>> '{evidence,cycle}')::int) AS spec_id,
                  (SELECT e.at FROM ds.audit_log e
                   WHERE e.object = 'readiness:' || p.pair AND e.action = 'readiness.changed'
                     AND e.id <= l.id AND e.detail->>'from' IS DISTINCT FROM e.detail->>'to'
                   ORDER BY e.id DESC LIMIT 1) AS since
           FROM unnest(%(pairs)s::text[]) AS p (pair) CROSS JOIN LATERAL (
               SELECT id, at, detail FROM ds.audit_log
               WHERE object = 'readiness:' || p.pair AND action = 'readiness.changed'
                 AND (%(at)s::timestamptz IS NULL OR at <= %(at)s)
               ORDER BY id DESC LIMIT 1) l""",
        {"pairs": list(pairs), "at": at},
    )
    return {row.pop("pair"): row for row in rows}


async def readiness_pairs(conn: Conn, names: Sequence[str]) -> Row:
    """The database's ``now`` and the ``pairs`` readiness judges that are stored: the spec names with
    a batch or soak run that read no captured samples, and those of ``names`` (the settings', or one asked
    for) a stored spec carries."""
    return await _one(
        conn,
        """SELECT now() AS now, ARRAY(
               SELECT DISTINCT s.name FROM ds.specs s
               WHERE s.name = ANY(%s) OR EXISTS (
                   SELECT 1 FROM ds.runs r
                   WHERE r.spec_id = s.spec_id AND r.mode IN ('batch', 'soak')
                     AND r.params->>'samples_dir' IS NULL)
               ORDER BY s.name) AS pairs""",
        (list(names),),
    )


async def readiness_entered(conn: Conn, after: int, actions: Sequence[str]) -> Row:
    """What entered the audit chain after row ``after``: its ``head`` now (``after`` when nothing
    did) and the last row of ``actions`` (``seq``; None without one), which tells an evaluation is due.
    The chain's seq is commit order, so a row committed after one of a higher id is still found."""
    return await _one(
        conn,
        """SELECT coalesce(max(c.seq), %(after)s) AS head,
                  max(c.seq) FILTER (WHERE l.action = ANY(%(actions)s)) AS seq
           FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id WHERE c.seq > %(after)s""",
        {"after": after, "actions": list(actions)},
    )


async def readiness_transitions(
    conn: Conn, *, pair: str | None = None, programme: str | None = None, limit: int = 100,
    cursor: str | None = None,
) -> Page:  # fmt: skip
    """The ``readiness.changed`` rows, newest first, of one pair or one programme's pairs if given: each
    row's ``id``, chain ``seq``, ``at`` and ``detail`` (ids, states, counts and slugs: no actor, address or
    request data)."""
    before = _after(cursor, 1)[0]
    rows = await _rows(
        conn,
        """SELECT l.id, (SELECT c.seq FROM ds.audit_chain c WHERE c.id = l.id) AS seq, l.at, l.detail
           FROM ds.audit_log l
           WHERE l.action = 'readiness.changed' AND (%(before)s < 0 OR l.id < %(before)s)
             AND (%(pair)s::text IS NULL OR l.object = 'readiness:' || %(pair)s)
             AND (%(programme)s::text IS NULL OR l.detail->>'programme' = %(programme)s)
           ORDER BY l.id DESC LIMIT %(limit)s""",
        {"before": before, "pair": pair, "programme": programme, "limit": limit + 1},
    )
    return _page(rows, limit, "id")


# A pair's points as its trend reads them: every measured cycle, or by day or ISO week (UTC) each
# bucket's last evidence point.
_POINTS: LiteralString = """SELECT c.readiness->>'pair' AS pair, c.run_id, c.cycle, c.ended_at, c.readiness
    FROM ds.cycles c
    WHERE c.readiness->>'pair' = ANY(%(pairs)s) AND c.readiness ? 'pair'
      AND (%(at)s::timestamptz IS NULL OR (c.ended_at, c.run_id, c.cycle) < (%(at)s, %(run)s, %(cycle)s))
    ORDER BY 1, c.ended_at DESC, c.run_id DESC, c.cycle DESC"""
_BUCKETS: LiteralString = """SELECT DISTINCT ON (1, date_trunc(%(bucket)s, c.ended_at, 'UTC'))
           c.readiness->>'pair' AS pair, c.run_id, c.cycle, c.ended_at, c.readiness
    FROM ds.cycles c
    WHERE c.readiness->>'pair' = ANY(%(pairs)s) AND c.readiness ? 'pair'
      AND c.readiness @> '{"evidence": true, "format": 1}'
      AND (%(at)s::timestamptz IS NULL OR c.ended_at < date_trunc(%(bucket)s, %(at)s::timestamptz, 'UTC'))
    ORDER BY 1, date_trunc(%(bucket)s, c.ended_at, 'UTC') DESC, c.ended_at DESC, c.run_id DESC,
             c.cycle DESC"""


def _points(pairs: Sequence[str], bucket: str, before: tuple[datetime, str, int] | None) -> dict[str, Any]:
    at, run_id, cycle = before or (None, None, None)
    return {"pairs": list(pairs), "bucket": bucket, "at": at, "run": run_id, "cycle": cycle}


async def readiness_points(
    conn: Conn,
    pairs: Sequence[str],
    *,
    bucket: str = "cycle",
    before: tuple[datetime, str, int] | None = None,
    limit: int | None = None,
) -> list[Row]:
    """The points of ``pairs`` (``{pair, run_id, cycle, ended_at, readiness}``), by pair and newest first:
    every measured cycle, or by ``day`` or ``week`` each bucket's last evidence point; those before the
    point ``before`` (``(ended_at, run_id, cycle)``; by bucket, before its bucket), ``limit`` at most. One
    range of cycles_readiness_pair_idx a pair."""
    query = _POINTS if bucket == "cycle" else _BUCKETS
    return await _rows(conn, query + " LIMIT %(limit)s", _points(pairs, bucket, before) | {"limit": limit})


async def count_points(conn: Conn, pairs: Sequence[str], *, bucket: str = "cycle") -> int:
    """How many points ``readiness_points`` gives of ``pairs``, counted on the index."""
    query = _POINTS if bucket == "cycle" else _BUCKETS
    counted: LiteralString = "SELECT count(*) FROM (" + query + ") p"  # noqa: S608 - fixed SQL fragments only
    return await _value(conn, counted, _points(pairs, bucket, None))


async def brief_points(conn: Conn, pair: str, until: datetime | None, limit: int) -> list[Row]:
    """The pair's newest measured cycles ended by ``until`` (None: now), newest first, ``limit`` at most,
    as its brief reads them: each ``{run_id, cycle, ended_at, readiness, counters, sample, purging_at,
    root_causes}``, the last how many root-cause groups it holds, None once its purge started (they go
    with it). One range of cycles_readiness_pair_idx, then each cycle found counted by its key."""
    return await _rows(
        conn,
        """SELECT p.*, CASE WHEN p.purging_at IS NULL THEN (
                      SELECT count(*) FROM ds.root_causes g
                      WHERE (g.run_id, g.cycle) = (p.run_id, p.cycle)) END AS root_causes
           FROM (SELECT c.run_id, c.cycle, c.ended_at, c.readiness, c.counters, c.sample, c.purging_at
                 FROM ds.cycles c
                 WHERE c.readiness->>'pair' = %(pair)s AND c.readiness ? 'pair'
                   AND c.ended_at <= coalesce(%(until)s::timestamptz, 'infinity')
                 ORDER BY c.ended_at DESC, c.run_id DESC, c.cycle DESC LIMIT %(limit)s) p
           ORDER BY p.ended_at DESC, p.run_id DESC, p.cycle DESC""",
        {"pair": pair, "until": until, "limit": limit},
    )


async def brief_causes(conn: Conn, cycles: Sequence[Mapping[str, Any]], limit: int | None) -> Row:
    """The root-cause groups of ``cycles`` (``{run_id, cycle}``, oldest first: a brief's complete cycles
    whose groups are kept, the last of them its reported one), in one statement that groups their rows by
    ``root_cause_id``: how many groups there are (``total``), how many the last cycle ``opened`` (in it, in
    no earlier one) and how many are ``still_open`` (in it and in an earlier one), the rest being in
    earlier ones only; and ``items``, ``limit`` of them (None: every one): the last cycle's first, in its
    rank order, then the others by the last cycle holding them, newest first, then by id. Each
    ``{root_cause_id, first, last, seen, max_severity, taxonomy_code, change_type, affected_keys}``: the
    places in ``cycles`` (from 1) of the first and the last cycle holding it, how many hold it, and what
    its row of that last one says, read by its key. No path, key or member token is read. Each cycle's
    groups are read by its key (``OFFSET 0`` keeps that a probe: the cost is those cycles' rows, whatever
    the table holds)."""
    rows = await _rows(
        conn,
        """WITH asked AS (
               SELECT * FROM unnest(%(runs)s::text[], %(cycles)s::int[]) WITH ORDINALITY
                   AS a (run_id, cycle, n)),
           groups AS (
               SELECT g.root_cause_id, min(a.n) AS first, max(a.n) AS last, count(*) AS seen,
                      min(g.seq) FILTER (WHERE a.n = %(newest)s) AS seq
               FROM asked a CROSS JOIN LATERAL (
                   SELECT g.root_cause_id, g.seq FROM ds.root_causes g
                   WHERE (g.run_id, g.cycle) = (a.run_id, a.cycle) OFFSET 0) g
               GROUP BY g.root_cause_id),
           listed AS (
               SELECT g.*, count(*) OVER () AS total,
                      count(*) FILTER (WHERE g.first = %(newest)s) OVER () AS opened,
                      count(*) FILTER (WHERE g.last = %(newest)s AND g.first < %(newest)s) OVER ()
                          AS still_open
               FROM groups g
               ORDER BY g.seq NULLS LAST, g.last DESC, g.root_cause_id LIMIT %(limit)s)
           SELECT l.*, c.max_severity, c.taxonomy_code, c.change_type, c.affected_keys
           FROM listed l JOIN asked a ON a.n = l.last
           CROSS JOIN LATERAL (
               SELECT c.max_severity, c.taxonomy_code, c.change_type, c.affected_keys
               FROM ds.root_causes c
               WHERE (c.run_id, c.cycle, c.root_cause_id) = (a.run_id, a.cycle, l.root_cause_id)
               OFFSET 0) c
           ORDER BY l.seq NULLS LAST, l.last DESC, l.root_cause_id""",
        {
            "runs": [c["run_id"] for c in cycles],
            "cycles": [c["cycle"] for c in cycles],
            "newest": len(cycles),
            "limit": limit,
        },
    )
    counted = {name: rows[0][name] if rows else 0 for name in ("total", "opened", "still_open")}
    names = (
        "root_cause_id", "first", "last", "seen", "max_severity", "taxonomy_code", "change_type",
        "affected_keys",
    )  # fmt: skip
    return counted | {"items": [{name: row[name] for name in names} for row in rows]}


async def readiness_measured(conn: Conn) -> AsyncIterator[Row]:
    """Every cycle holding readiness measures or a marker, and every cycle a ``readiness.measured`` row
    names, streamed (``ds readiness verify``): its ``readiness`` (None: a named cycle that holds none, or
    is gone), its run's ``mode`` and ``samples_dir``, and of the ``readiness.measured`` rows naming it how
    many (``rows``; 0 without one) and the newest one's hash (``sha256``) and chain ``seq``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        async for row in cur.stream(
            """WITH named AS (
                   SELECT e->>0 AS run_id, (e->>1)::int AS cycle, count(*) AS rows, max(l.id) AS id,
                          (array_agg(e->>2 ORDER BY l.id DESC))[1] AS sha256
                   FROM ds.audit_log l CROSS JOIN LATERAL jsonb_array_elements(l.detail->'cycles') e
                   WHERE l.action = 'readiness.measured' GROUP BY 1, 2),
               held AS (
                   SELECT c.run_id, c.cycle, c.readiness, r.mode, r.params->>'samples_dir' AS samples_dir
                   FROM ds.cycles c JOIN ds.runs r ON r.run_id = c.run_id WHERE c.readiness IS NOT NULL)
               SELECT run_id, cycle, c.readiness, c.mode, c.samples_dir, coalesce(n.rows, 0) AS rows,
                      n.sha256, (SELECT h.seq FROM ds.audit_chain h WHERE h.id = n.id) AS seq
               FROM held c FULL JOIN named n USING (run_id, cycle) ORDER BY run_id, cycle"""
        ):
            yield row
