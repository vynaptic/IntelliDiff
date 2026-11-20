"""The queries of retention and purge."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, LiteralString

from deltasleuth.store.repo.base import (
    _LIVE,
    TERMINAL,
    Conn,
    Page,
    Row,
    _after,
    _changed,
    _jsonb,
    _page,
    _row,
    _rows,
    _value,
)

# A run of retention class r keeps a cycle's raw data this long after the cycle ends (days read at purge
# time: the queries take standard_days and audit_days).
_DAYS: LiteralString = """CASE r.retention WHEN 'audit' THEN %(audit_days)s::int
                                             ELSE %(standard_days)s::int END"""
_KEPT: LiteralString = f"make_interval(days => {_DAYS})"

# One chunk of a cycle's keys (k), and a row (t) of one of them.
_CHUNK: LiteralString = "k.run_id = %(r)s AND k.cycle = %(c)s AND k.seq >= %(lo)s AND k.seq < %(hi)s"
_CHUNK_ROW: LiteralString = "(t.run_id, t.cycle, t.key_masked) = (k.run_id, k.cycle, k.key_masked)"


async def end_cycle(
    conn: Conn, run_id: str, ended_at: datetime | None = None, sample: Mapping[str, Any] | None = None
) -> None:
    """Record the end of the run's current cycle (``ended_at``, default now), with its counters and its
    census if they are the cycle's, the spec approval it ran under (none before
    ``params["approval"]["from_cycle"]``, set by ``set_run_approval``), how maxKeys cut it and a drawn
    cycle's ``sample`` judgement; the tombstone the retention clock starts from and a sign-off judges. A
    one-shot or captured-sample run's cycle is marked excluded from readiness; any other is measured by the
    worker. Once per cycle."""
    await conn.execute(
        """INSERT INTO ds.cycles (run_id, cycle, ended_at, counters, approval_id, keys_cut, census, sample,
               readiness)
           SELECT run_id, cycle, coalesce(%s::timestamptz, now()),
                  CASE WHEN counters->>'cycle' = cycle::text THEN counters ELSE '{}' END,
                  CASE WHEN cycle < coalesce((params->'approval'->>'from_cycle')::int, 0) THEN NULL
                       ELSE approval_id END,
                  keys_cut,
                  CASE WHEN census->>'cycle' = cycle::text THEN census END,
                  %s,
                  CASE WHEN mode = 'one_shot' THEN '{"format": 1, "excluded": "one_shot"}'::jsonb
                       WHEN params->>'samples_dir' IS NOT NULL
                           THEN '{"format": 1, "excluded": "captured_samples"}'::jsonb END
           FROM ds.runs WHERE run_id = %s
           ON CONFLICT DO NOTHING""",
        (ended_at, None if sample is None else _jsonb(sample), run_id),
    )


async def get_cycle(conn: Conn, run_id: str, cycle: int, *, lock: bool = False) -> Row | None:
    """The cycle's ``ds.cycles`` row; None while it is live. ``lock`` reads it FOR SHARE: a purge's start
    (``start_purge``) then waits for this transaction, or this read waits for it and finds it purging."""
    return await _row(
        conn,
        f"SELECT * FROM ds.cycles WHERE run_id = %s AND cycle = %s {'FOR SHARE' if lock else ''}",  # noqa: S608 - fixed SQL fragments only
        (run_id, cycle),
    )


async def list_cycles(conn: Conn, run_id: str, *, standard_days: int, audit_days: int) -> list[Row]:
    """The run's ended cycles in order, each with ``expires_at``, when its retention ends."""
    return await _rows(
        conn,
        f"""SELECT c.*, c.ended_at + {_KEPT} AS expires_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.run_id = %(run_id)s ORDER BY c.cycle""",  # noqa: S608 - fixed SQL fragments only
        {"run_id": run_id, "standard_days": standard_days, "audit_days": audit_days},
    )


async def cycle_page(
    conn: Conn,
    run_id: str,
    *,
    standard_days: int,
    audit_days: int,
    limit: int = 20,
    cursor: str | None = None,
) -> Page:
    """The run's ended cycles newest first, as ``list_cycles`` gives them, a page at a time; a page's cursor
    is the oldest cycle it holds, and the next page holds the cycles before it."""
    before = None
    if cursor is not None:
        before = _after(cursor, 1)[0]
        if not 0 < before < 2**31:
            raise ValueError("malformed cursor")
    rows = await _rows(
        conn,
        f"""SELECT c.*, c.ended_at + {_KEPT} AS expires_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.run_id = %(run_id)s AND (%(before)s::int IS NULL OR c.cycle < %(before)s)
            ORDER BY c.cycle DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {
            "run_id": run_id,
            "before": before,
            "limit": limit + 1,
            "standard_days": standard_days,
            "audit_days": audit_days,
        },
    )
    return _page(rows, limit, "cycle")


async def cycle_retention(conn: Conn, run_id: str, *, standard_days: int, audit_days: int) -> list[Row]:
    """The run's ended cycles in order with what their retention shows and nothing else: ``cycle``,
    ``ended_at``, ``expires_at``, ``purging_at`` and ``purged_at``."""
    return await _rows(
        conn,
        f"""SELECT c.cycle, c.ended_at, c.ended_at + {_KEPT} AS expires_at, c.purging_at, c.purged_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.run_id = %(run_id)s ORDER BY c.cycle""",  # noqa: S608 - fixed SQL fragments only
        {"run_id": run_id, "standard_days": standard_days, "audit_days": audit_days},
    )


async def due_cycles(
    conn: Conn, *, standard_days: int, audit_days: int, limit: int | None = None
) -> list[Row]:
    """The cycles to purge, an interrupted purge first: those whose retention ended, except a held
    run's and the cycles a live run still reads (its current one and the last ended)."""
    return await _rows(
        conn,
        f"""SELECT c.run_id, c.cycle, c.ended_at, r.retention, r.params->>'samples_dir' AS samples_dir,
                   c.purging_at, c.ended_at + {_KEPT} AS expires_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.purged_at IS NULL
              AND (c.purging_at IS NOT NULL
                   OR (r.hold_reason IS NULL AND c.ended_at + {_KEPT} <= now()
                       AND NOT ({_LIVE} AND c.cycle >= r.cycle - 1)))
            ORDER BY c.purging_at NULLS LAST, c.ended_at
            LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"standard_days": standard_days, "audit_days": audit_days, "limit": limit},
    )


async def kept_cycles(conn: Conn, *, standard_days: int, audit_days: int) -> list[Row]:
    """The cycles whose retention ended but that are not due: a held run's (``hold_reason`` set) and a
    live run's last ended cycle, which its next cycle still reads."""
    return await _rows(
        conn,
        f"""SELECT c.run_id, c.cycle, c.ended_at, r.retention, r.hold_reason, r.hold_by, r.hold_at,
                   c.ended_at + {_KEPT} AS expires_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.purged_at IS NULL AND c.purging_at IS NULL AND c.ended_at + {_KEPT} <= now()
              AND (r.hold_reason IS NOT NULL OR ({_LIVE} AND c.cycle >= r.cycle - 1))
            ORDER BY c.ended_at""",  # noqa: S608 - fixed SQL fragments only
        {"standard_days": standard_days, "audit_days": audit_days},
    )


async def abandoned_runs(conn: Conn, *, standard_days: int, audit_days: int) -> list[Row]:
    """Runs past the purge's ceiling: a one-shot run queued for 24 h, and any run paused or queued
    (since ``updated_at``) longer than its retention ``days``."""
    return await _rows(
        conn,
        f"""SELECT r.run_id, r.mode, r.status, r.updated_at, r.retention, {_DAYS} AS days
            FROM ds.runs r
            WHERE (r.mode = 'one_shot' AND r.status = 'queued'
                   AND r.updated_at <= now() - interval '24 hours')
               OR (r.status IN ('paused', 'queued') AND r.updated_at + {_KEPT} <= now())
            ORDER BY r.updated_at, r.run_id""",  # noqa: S608 - fixed SQL fragments only
        {"standard_days": standard_days, "audit_days": audit_days},
    )


async def start_purge(conn: Conn, run_id: str, cycle: int, reason: str) -> Row | None:
    """Mark the ended cycle purging (its reads answer 410 from now on) for ``reason``; an interrupted
    purge keeps its start and reason. The run's row is share-locked first, so a hold committed before
    is seen and one placed meanwhile waits for this transaction. The row; None when the cycle is purged
    or has not ended, or its run is held and its purge has not started."""
    held = await _value(
        conn, "SELECT hold_reason IS NOT NULL FROM ds.runs WHERE run_id = %s FOR SHARE", (run_id,)
    )
    return await _row(
        conn,
        """UPDATE ds.cycles SET purging_at = coalesce(purging_at, now()),
               purge_reason = coalesce(purge_reason, %s)
           WHERE run_id = %s AND cycle = %s AND purged_at IS NULL AND (purging_at IS NOT NULL OR NOT %s)
           RETURNING *""",
        (reason, run_id, cycle, bool(held)),
    )


async def key_span(conn: Conn, run_id: str, cycle: int) -> tuple[int, int] | None:
    """The lowest and highest ``seq`` of the cycle's keys; None when it has none."""
    row = await (
        await conn.execute(
            "SELECT min(seq), max(seq) FROM ds.run_keys WHERE run_id = %s AND cycle = %s", (run_id, cycle)
        )
    ).fetchone()
    return None if row is None or row[0] is None else (row[0], row[1])


async def purge_keys(conn: Conn, run_id: str, cycle: int, lo: int, hi: int) -> dict[str, int]:
    """Delete the cycle's keys of ``seq`` in [lo, hi) with their findings, results and observations, in
    that order, never through the cascade, and queue the blobs of the deleted observations for the GC
    (one queued already waits 24 h again); the rows deleted and blobs queued, by kind."""
    params = {"r": run_id, "c": cycle, "lo": lo, "hi": hi}
    done = {
        "findings": await _changed(
            conn,
            f"DELETE FROM ds.findings t USING ds.run_keys k WHERE {_CHUNK} AND {_CHUNK_ROW}",  # noqa: S608
            params,
        ),
        "results": await _changed(
            conn,
            f"DELETE FROM ds.results t USING ds.run_keys k WHERE {_CHUNK} AND {_CHUNK_ROW}",  # noqa: S608
            params,
        ),
    }
    row = await (
        await conn.execute(
            f"""WITH gone AS (DELETE FROM ds.observations t USING ds.run_keys k
                              WHERE {_CHUNK} AND {_CHUNK_ROW} RETURNING t.content_hash),
                queued AS (INSERT INTO ds.blob_gc (content_hash)
                           SELECT DISTINCT content_hash FROM gone WHERE content_hash IS NOT NULL
                           ON CONFLICT (content_hash) DO UPDATE SET queued_at = now()
                           RETURNING 1)
                SELECT (SELECT count(*) FROM gone), (SELECT count(*) FROM queued)""",  # noqa: S608
            params,
        )
    ).fetchone()
    assert row is not None  # two counts: one row
    done["observations"], done["blobs_queued"] = row
    done["keys"] = await _changed(conn, f"DELETE FROM ds.run_keys k WHERE {_CHUNK}", params)  # noqa: S608
    return done


async def finish_purge(conn: Conn, run_id: str, cycle: int, purged_by: str) -> dict[str, Any]:
    """End the cycle's purge under the run's row lock: delete its root causes, graph rows, scan, chat
    messages and stored pack zips (their pack records stay, marked purged), tombstone its adjudications
    (their sheets, selected keys and disagreements go; hashes and counts stay) and its labellings (their
    sheets and drawn items go; hashes, counts, truth and accuracy stay) and mark it purged by
    ``purged_by``. A terminal run's chat sessions and data key go with its last unpurged cycle. The chat
    messages deleted, whether the data key was destroyed and the packs whose zip was deleted."""
    status = await _value(conn, "SELECT status FROM ds.runs WHERE run_id = %s FOR UPDATE", (run_id,))
    where = (run_id, cycle)
    await conn.execute("DELETE FROM ds.root_causes WHERE run_id = %s AND cycle = %s", where)
    await conn.execute("DELETE FROM ds.graph_diffs WHERE run_id = %s AND cycle = %s", where)
    await conn.execute("DELETE FROM ds.scans WHERE run_id = %s AND cycle = %s", where)
    await conn.execute(
        """DELETE FROM ds.pack_zips z USING ds.packs p
           WHERE p.pack_id = z.pack_id AND p.run_id = %s AND p.cycle = %s""",
        where,
    )
    packs = await _rows(
        conn,
        """UPDATE ds.packs SET purged_at = now() WHERE run_id = %s AND cycle = %s AND purged_at IS NULL
           RETURNING pack_id""",
        where,
    )
    await conn.execute(
        """UPDATE ds.adjudications SET purged_at = now(), sheet = NULL,
               selection = jsonb_set(selection, '{keys}', 'null'),
               score = jsonb_set(score, '{disagreements}', 'null')
           WHERE run_id = %s AND cycle = %s AND purged_at IS NULL""",
        where,
    )
    await conn.execute(
        """UPDATE ds.labellings SET purged_at = now(), sheet = NULL,
               selection = jsonb_set(selection, '{items}', 'null')
           WHERE run_id = %s AND cycle = %s AND purged_at IS NULL""",
        where,
    )
    messages = await _changed(
        conn,
        """DELETE FROM ds.chat_messages m USING ds.chat_sessions s
           WHERE s.session_id = m.session_id AND s.run_id = %s AND m.cycle = %s""",
        where,
    )
    last = status in TERMINAL and not await _value(
        conn,
        "SELECT EXISTS (SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle <> %s AND purged_at IS NULL)",
        where,
    )
    if last:
        await conn.execute("DELETE FROM ds.chat_sessions WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM ds.data_keys WHERE scope = %s", (run_id,))
    await conn.execute(
        "UPDATE ds.cycles SET purged_at = now(), purged_by = %s WHERE run_id = %s AND cycle = %s",
        (purged_by, *where),
    )
    return {
        "chat_messages": messages,
        "data_key_destroyed": last,
        "packs": sorted(row["pack_id"] for row in packs),
    }


async def keys_in_flight(conn: Conn, run_id: str, cycle: int | None = None) -> bool:
    """Whether a worker holds a live lease on one of the run's keys (of ``cycle`` only, if given)."""
    return await _value(
        conn,
        """SELECT EXISTS (SELECT 1 FROM ds.run_keys
                          WHERE run_id = %s AND (%s::int IS NULL OR cycle = %s)
                            AND state = 'leased' AND lease_until > now())""",
        (run_id, cycle, cycle),
    )


async def set_hold(conn: Conn, run_id: str, reason: str | None, by: str | None) -> Row | None:
    """Hold the run's evidence past its retention (``reason`` and ``by``), or release it (None); the
    run's hold columns as they were, None when the run is unknown or already in that state."""
    return await _row(
        conn,
        """UPDATE ds.runs r SET hold_reason = %(reason)s, hold_by = %(by)s,
               hold_at = CASE WHEN %(reason)s::text IS NULL THEN NULL ELSE now() END
           FROM ds.runs old
           WHERE r.run_id = %(run_id)s AND old.run_id = r.run_id
             AND (old.hold_reason IS NULL) = (%(reason)s::text IS NOT NULL)
           RETURNING old.hold_reason, old.hold_by, old.hold_at""",
        {"run_id": run_id, "reason": reason, "by": by},
    )


async def grace_over(conn: Conn, days: int) -> bool:
    """Whether 006 (retention) was applied at least ``days`` days ago."""
    return await _value(
        conn,
        """SELECT coalesce(bool_or(applied_at + make_interval(days => %s) <= now()), false)
           FROM ds.schema_version WHERE version = 6""",
        (days,),
    )


async def gc_due(conn: Conn, limit: int) -> list[str]:
    """Up to ``limit`` blob addresses queued over 24 h ago, oldest first."""
    rows = await _rows(
        conn,
        """SELECT content_hash FROM ds.blob_gc WHERE queued_at <= now() - interval '24 hours'
           ORDER BY queued_at LIMIT %s""",
        (limit,),
    )
    return [row["content_hash"] for row in rows]


async def referenced(conn: Conn, addresses: Sequence[str]) -> set[str]:
    """Those of ``addresses`` an observation still names."""
    rows = await _rows(
        conn,
        "SELECT DISTINCT content_hash FROM ds.observations WHERE content_hash = ANY(%s)",
        (list(addresses),),
    )
    return {row["content_hash"] for row in rows}


async def queued_blobs(conn: Conn, addresses: Sequence[str]) -> set[str]:
    """Those of ``addresses`` the GC queue holds."""
    rows = await _rows(
        conn, "SELECT content_hash FROM ds.blob_gc WHERE content_hash = ANY(%s)", (list(addresses),)
    )
    return {row["content_hash"] for row in rows}


async def gc_done(conn: Conn, addresses: Sequence[str]) -> None:
    await conn.execute("DELETE FROM ds.blob_gc WHERE content_hash = ANY(%s)", (list(addresses),))


async def blob_addresses(conn: Conn, after: str = "", limit: int = 10_000) -> list[str]:
    """The distinct blob addresses observations name, in order, after ``after``: a page of them."""
    rows = await _rows(
        conn,
        """SELECT DISTINCT content_hash FROM ds.observations
           WHERE content_hash IS NOT NULL AND content_hash > %s ORDER BY content_hash LIMIT %s""",
        (after, limit),
    )
    return [row["content_hash"] for row in rows]
