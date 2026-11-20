"""The queries of root causes and soak changes."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, LiteralString

from deltasleuth.core.types import RootCause
from deltasleuth.store.repo.base import (
    Conn,
    Page,
    Row,
    _after,
    _current,
    _page,
    _rows,
    _value,
    pages,
    storable,
)


def _cause_params(rc: RootCause) -> dict[str, Any]:
    return storable(
        dataclasses.asdict(rc)
        | {
            "change_type": rc.change_type.value,
            "direction": rc.direction.value,
            "max_severity": rc.max_severity.value,
        }
    )


async def replace_root_causes(
    conn: Conn, run_id: str, cycle: int, causes: Sequence[RootCause], finding_causes: Mapping[str, str]
) -> None:
    """Replace a cycle's root causes (in rank order) and point each finding at its cause
    (``finding_causes``: finding_id -> root_cause_id), in one transaction, which analyses the findings'
    ``root_cause_id`` again when its statistics hold under half the findings linked now: a new store's
    first cycle end analysed them before any was linked (``analyse_new``), the links may be too few for
    autovacuum to analyse again, and the planner would take the cycle's linked findings for one row."""
    params = {"run_id": run_id, "cycle": cycle}
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM ds.root_causes WHERE run_id = %(run_id)s AND cycle = %(cycle)s", params
        )
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO ds.root_causes (run_id, cycle, seq, root_cause_id, kind, taxonomy_code,
                       max_severity, path_pattern, change_type, direction, affected_keys, finding_count,
                       member_key_tok, value_class, signature_hash, sample_keys)
                   VALUES (%(run_id)s, %(cycle)s, %(seq)s, %(root_cause_id)s, %(kind)s, %(taxonomy_code)s,
                       %(max_severity)s, %(path_pattern)s, %(change_type)s, %(direction)s, %(affected_keys)s,
                       %(finding_count)s, %(member_key_tok)s, %(value_class)s, %(signature_hash)s,
                       %(sample_keys)s)""",
                [_cause_params(rc) | params | {"seq": seq} for seq, rc in enumerate(causes)],
            )
        await conn.execute(
            """UPDATE ds.findings SET root_cause_id = NULL
               WHERE run_id = %(run_id)s AND cycle = %(cycle)s AND root_cause_id IS NOT NULL""",
            params,
        )
        await conn.execute(
            """UPDATE ds.findings f SET root_cause_id = m.rc
               FROM unnest(%(ids)s::text[], %(rcs)s::text[]) AS m (id, rc)
               WHERE f.finding_id = m.id AND f.run_id = %(run_id)s AND f.cycle = %(cycle)s""",
            params | {"ids": list(finding_causes), "rcs": list(finding_causes.values())},
        )
        await analyse_cause_links(conn, len(finding_causes))


async def analyse_cause_links(conn: Conn, linked: int) -> None:
    """Refresh statistics when a cycle links over twice the planner's estimate of all linked findings."""
    if linked and await _value(
        conn,
        """SELECT (1 - s.null_frac) * c.reltuples * 2 < %s
           FROM pg_class c JOIN pg_stats s ON s.schemaname = 'ds' AND s.tablename = c.relname
           WHERE c.oid = 'ds.findings'::regclass AND s.attname = 'root_cause_id'""",
        (linked,),
    ):
        await conn.execute("ANALYZE ds.findings (root_cause_id)")


async def list_root_causes(conn: Conn, run_id: str, cycle: int | None = None) -> list[Row]:
    """Most probable cause first."""
    return await _rows(
        conn,
        "SELECT * FROM ds.root_causes WHERE run_id = %s AND cycle = %s ORDER BY seq",
        (run_id, await _current(conn, run_id, cycle)),
    )


async def root_cause_page(conn: Conn, run_id: str, cycle: int, *, cursor: str | None = None) -> Page:
    """Most probable causes first, in pages for an export."""
    after = _after(cursor, 1)[0]
    rows = await _rows(
        conn,
        """SELECT * FROM ds.root_causes WHERE run_id = %s AND cycle = %s AND seq > %s
           ORDER BY seq LIMIT 1001""",
        (run_id, cycle, after),
    )
    return _page(rows, 1000, "seq")


# A scan records identical rows only as a count. A new per-key discrepancy therefore has no
# known prior outcome: the key may have matched, or may be new to the population.
_CHANGED: LiteralString = """NOT EXISTS (
    SELECT 1 FROM ds.cycles WHERE run_id = %(run_id)s AND cycle = %(cycle)s - 1 AND purging_at IS NOT NULL
) AND (cur.outcome <> prev.outcome OR (
    prev.outcome IS NULL AND cur.outcome <> 'MATCH' AND EXISTS (
        SELECT 1 FROM ds.scans WHERE run_id = %(run_id)s AND cycle = %(cycle)s - 1)))"""


async def list_outcome_changes(
    conn: Conn, run_id: str, cycle: int | None = None, *, limit: int = 100, cursor: str | None = None
) -> Page:
    """The cycle's keys whose outcome differs from the cycle before's, in manifest order: cycle,
    key_masked, from_outcome and to_outcome. A page walks the cycle's keys by their order from where the
    page before ended, reading each key's result of the cycle before and then of the cycle by key, and
    stops at its last change. A new non-MATCH after a table scan has a null from_outcome."""
    after = _after(cursor, 1)[0]
    cycle = await _current(conn, run_id, cycle)
    if cycle is None or cycle <= 1:
        return [], None
    rows = await _rows(
        conn,
        f"""SELECT k.seq, k.cycle, k.key_masked, prev.outcome AS from_outcome, cur.outcome AS to_outcome
           FROM ds.run_keys k
           LEFT JOIN LATERAL (
               SELECT r.outcome FROM ds.results r
               WHERE (r.run_id, r.cycle, r.key_masked) = (k.run_id, k.cycle - 1, k.key_masked)
               OFFSET 0) prev ON true
           CROSS JOIN LATERAL (
               SELECT r.outcome FROM ds.results r
               WHERE (r.run_id, r.cycle, r.key_masked) = (k.run_id, k.cycle, k.key_masked) OFFSET 0) cur
           WHERE k.run_id = %(run_id)s AND k.cycle = %(cycle)s AND k.seq > %(after)s
             AND {_CHANGED}
           ORDER BY k.seq LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragment
        {"run_id": run_id, "cycle": cycle, "after": after, "limit": limit + 1},
    )
    changed, next_cursor = _page(rows, limit, "seq")
    names = ("cycle", "key_masked", "from_outcome", "to_outcome")
    return [{name: row[name] for name in names} for row in changed], next_cursor


async def count_outcome_changes(conn: Conn, run_id: str, cycle: int) -> int:
    """How many of the cycle's keys have an outcome that differs from the cycle before's: the cycle's
    results are read once, and each key's result of the cycle before by key."""
    if cycle <= 1:
        return 0
    return await _value(
        conn,
        f"""SELECT count(*)
           FROM ds.results cur LEFT JOIN LATERAL (
               SELECT r.outcome FROM ds.results r
               WHERE (r.run_id, r.cycle, r.key_masked) = (cur.run_id, cur.cycle - 1, cur.key_masked)
               OFFSET 0) prev ON true
           WHERE cur.run_id = %(run_id)s AND cur.cycle = %(cycle)s AND {_CHANGED}""",  # noqa: S608
        {"run_id": run_id, "cycle": cycle},
    )


async def outcome_changes(conn: Conn, run_id: str, cycle: int | None = None) -> list[Row]:
    """Every row of ``list_outcome_changes`` for one cycle, read a page at a time."""
    return await pages(list_outcome_changes, conn, run_id, await _current(conn, run_id, cycle))
