"""The queries of a run's keys: leases, completion, rechecks, retries and sweeps."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from deltasleuth.store.repo.base import (
    _CENSUS_PENDING,
    Conn,
    Page,
    Row,
    _after,
    _changed,
    _current,
    _page,
    _row,
    _rows,
    _value,
)


async def lease_due(
    conn: Conn,
    owner: str,
    limit: int,
    lease_seconds: float,
    *,
    skip: Sequence[str] = (),
    run_id: str | None = None,
    census: bool = False,
    busy: Mapping[str, int] | None = None,
) -> list[Row]:
    """Lease up to ``limit`` due keys of running runs, except the ``skip`` runs, to ``owner``,
    expired leases included.

    Concurrent callers never get the same key (SKIP LOCKED). Keys are looked up run by run, so
    the keys of paused runs are never scanned. ``run_id``: that run's keys only (a batch). ``census``
    (packs or readiness configured): no key of a run whose cycle awaits its key census. ``busy``: the
    groups the worker has in flight, by run; the keys of the run with the fewest come first, so runs share
    the slots in turn, and a run's own keys go by when they fell due, then in its order. Rows: run_id,
    cycle, key_masked, key_enc, pass_no (1 for the first reads, 2+ for a recheck), signoff_member and
    retries (a sample's), refused (``hand_back``) and spec_id. Each leased row is updated where the lease
    locked it (its ctid), so no plan of the update looks a key up among all of its cycle's keys.
    """
    busy = busy or {}
    return await _rows(
        conn,
        f"""WITH due AS (
                SELECT k.locked, k.run_id, k.cycle, k.key_masked, r.spec_id, k.available_at, k.seq,
                    coalesce(b.groups, 0) AS groups
                FROM ds.runs r
                LEFT JOIN unnest(%(busy)s::text[], %(groups)s::int[]) AS b (run_id, groups)
                    ON b.run_id = r.run_id
                CROSS JOIN LATERAL (
                    SELECT k.ctid AS locked, k.* FROM ds.run_keys k
                    WHERE k.run_id = r.run_id AND k.state IN ('pending', 'leased', 'recheck')
                      AND k.available_at <= now()
                    ORDER BY k.available_at, k.seq LIMIT %(limit)s FOR UPDATE SKIP LOCKED) k
                WHERE r.status = 'running' AND r.run_id <> ALL(%(skip)s::text[])
                  AND (%(run_id)s::text IS NULL OR r.run_id = %(run_id)s)
                  AND NOT coalesce(%(census)s AND {_CENSUS_PENDING}, false)
                ORDER BY groups, k.available_at, k.seq LIMIT %(limit)s)
            UPDATE ds.run_keys k SET state = 'leased', lease_owner = %(owner)s,
                lease_until = now() + %(seconds)s * interval '1 second',
                available_at = now() + %(seconds)s * interval '1 second'
            FROM due WHERE k.ctid = due.locked
              AND (k.run_id, k.cycle, k.key_masked) = (due.run_id, due.cycle, due.key_masked)
            RETURNING k.run_id, k.cycle, k.key_masked, k.key_enc, k.pass_no, k.signoff_member, k.retries,
                k.refused, due.spec_id""",  # noqa: S608
        {
            "owner": owner, "limit": limit, "seconds": lease_seconds, "skip": list(skip), "run_id": run_id,
            "census": census, "busy": list(busy), "groups": list(busy.values()),
        },
    )  # fmt: skip


async def renew_lease(conn: Conn, owner: str, lease_seconds: float) -> bool:
    """Extend ``owner``'s leases (a key, or a group of one run) while its run is running. False: every
    lease was lost, or the run was paused or stopped meanwhile, so the keys must not be read now; a
    group that lost some is rolled back when saved."""
    renewed = await _changed(
        conn,
        """UPDATE ds.run_keys k SET lease_until = now() + %(seconds)s * interval '1 second',
               available_at = now() + %(seconds)s * interval '1 second'
           FROM ds.runs r
           WHERE r.run_id = k.run_id AND r.status = 'running' AND k.state = 'leased'
             AND k.lease_owner = %(owner)s""",
        {"owner": owner, "seconds": lease_seconds},
    )
    return renewed > 0


async def complete_key(conn: Conn, run_id: str, cycle: int, key_masked: str, owner: str) -> bool:
    """Mark a key ``owner`` holds done. False: the lease was lost, so roll back what was saved."""
    return await complete_keys(conn, run_id, cycle, [key_masked], owner) == 1


async def complete_keys(conn: Conn, run_id: str, cycle: int, keys_masked: Sequence[str], owner: str) -> int:
    """``complete_key`` for each key; how many ``owner`` still held."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys SET state = 'done', lease_owner = NULL, lease_until = NULL
           WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s) AND state = 'leased'
             AND lease_owner = %s""",
        (run_id, cycle, list(keys_masked), owner),
    )


async def complete_inline(conn: Conn, run_id: str, cycle: int, key_masked: str) -> bool:
    """Mark done a key read inline, never leased: a one-shot run's. False: it was cancelled."""
    done = await _changed(
        conn,
        """UPDATE ds.run_keys SET state = 'done'
           WHERE run_id = %s AND cycle = %s AND key_masked = %s AND state = 'pending'""",
        (run_id, cycle, key_masked),
    )
    return done == 1


async def schedule_recheck(
    conn: Conn, run_id: str, cycle: int, key_masked: str, owner: str, wait_s: float
) -> bool:
    """Give up ``owner``'s lease and make the key due ``wait_s`` seconds after the database's now for its
    next pass."""
    return await schedule_rechecks(conn, run_id, cycle, {key_masked: wait_s}, owner) == 1


async def schedule_rechecks(
    conn: Conn, run_id: str, cycle: int, due: Mapping[str, float], owner: str
) -> int:
    """``schedule_recheck`` for each masked key and its wait in seconds, on the database's clock; how many
    ``owner`` still held."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys k SET state = 'recheck', pass_no = pass_no + 1,
               available_at = now() + d.wait * interval '1 second', lease_owner = NULL, lease_until = NULL
           FROM unnest(%s::text[], %s::float8[]) AS d (key_masked, wait)
           WHERE k.run_id = %s AND k.cycle = %s AND k.key_masked = d.key_masked AND k.state = 'leased'
             AND k.lease_owner = %s""",
        (list(due), list(due.values()), run_id, cycle, owner),
    )


async def schedule_retries(
    conn: Conn, run_id: str, cycle: int, due: Mapping[str, float], owner: str
) -> int:
    """Give up ``owner``'s lease of each masked key and read it again from its first pass its wait in seconds
    after the database's now, one more retry, its stored result's reason appended to ``attrs.retried``;
    how many ``owner`` still held."""
    moved = await _rows(
        conn,
        """UPDATE ds.run_keys k SET state = 'pending', pass_no = 1, retries = retries + 1,
               available_at = now() + d.wait * interval '1 second', lease_owner = NULL, lease_until = NULL
           FROM unnest(%s::text[], %s::float8[]) AS d (key_masked, wait)
           WHERE k.run_id = %s AND k.cycle = %s AND k.key_masked = d.key_masked AND k.state = 'leased'
             AND k.lease_owner = %s
           RETURNING k.key_masked""",
        (list(due), list(due.values()), run_id, cycle, owner),
    )
    await conn.execute(
        """UPDATE ds.results SET attrs = jsonb_set(attrs, '{retried}',
               coalesce(attrs->'retried', '[]') || to_jsonb(inconclusive_reason))
           WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s)""",
        (run_id, cycle, [row["key_masked"] for row in moved]),
    )
    return len(moved)


async def hand_back(conn: Conn, run_id: str, cycle: int, due: Mapping[str, float], owner: str) -> int:
    """Give up ``owner``'s lease of each masked key whose reads were refused unsent, one more refusal
    counted, and make it due its wait in seconds after the database's now, in the same pass: nothing of it
    was saved; how many ``owner`` still held."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys k SET state = CASE WHEN pass_no > 1 THEN 'recheck' ELSE 'pending' END,
               refused = refused + 1, available_at = now() + d.wait * interval '1 second',
               lease_owner = NULL, lease_until = NULL
           FROM unnest(%s::text[], %s::float8[]) AS d (key_masked, wait)
           WHERE k.run_id = %s AND k.cycle = %s AND k.key_masked = d.key_masked AND k.state = 'leased'
             AND k.lease_owner = %s""",
        (list(due), list(due.values()), run_id, cycle, owner),
    )


async def release_leases(conn: Conn, owner: str) -> int:
    """Hand back every key ``owner`` holds (a clean shutdown); return how many."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys SET state = CASE WHEN pass_no > 1 THEN 'recheck' ELSE 'pending' END,
               lease_owner = NULL, lease_until = NULL, available_at = now()
           WHERE state = 'leased' AND lease_owner = %s""",
        (owner,),
    )


async def cancel_keys(conn: Conn, run_id: str) -> int:
    """Cancel the run's unfinished keys, except those under a live lease; return how many."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys SET state = 'cancelled', lease_owner = NULL, lease_until = NULL
           WHERE run_id = %s AND (state IN ('pending', 'recheck')
                                  OR (state = 'leased' AND lease_until <= now()))""",
        (run_id,),
    )


async def mark_sweep(conn: Conn, holder: str) -> None:
    """Record that ``holder`` (a host name and a process id) swept now, in the one row of
    ``ds.worker_sweep``."""
    await conn.execute(
        """INSERT INTO ds.worker_sweep (holder, swept_at) VALUES (%s, now())
           ON CONFLICT (one) DO UPDATE SET holder = EXCLUDED.holder, swept_at = EXCLUDED.swept_at""",
        (holder,),
    )


async def last_sweep(conn: Conn) -> Row | None:
    """The worker's last sweep: its ``holder``, ``swept_at`` and ``age_s``, its age in seconds on the
    database's clock; None before any worker swept."""
    return await _row(
        conn,
        """SELECT holder, swept_at, extract(epoch FROM now() - swept_at)::float8 AS age_s
           FROM ds.worker_sweep""",
        None,
    )


async def cycle_done(conn: Conn, run_id: str, cycle: int) -> bool:
    """True when every key of the cycle is done or cancelled, no running scan still adds keys, no running
    run's sample is still to be drawn (a stopped run ends its cycle undrawn) and the sweep has judged the stop
    conditions of every group saved (``ds.stop_feed``)."""
    return await _value(
        conn,
        """SELECT NOT EXISTS (SELECT 1 FROM ds.run_keys WHERE run_id = %(run_id)s AND cycle = %(cycle)s
                                AND state IN ('pending', 'leased', 'recheck'))
              AND NOT EXISTS (SELECT 1 FROM ds.stop_feed WHERE run_id = %(run_id)s)
              AND NOT EXISTS (SELECT 1 FROM ds.scans sc JOIN ds.runs r USING (run_id)
                              WHERE sc.run_id = %(run_id)s AND sc.cycle = %(cycle)s AND r.status = 'running'
                                AND NOT (sc.state->>'done')::boolean)
              AND NOT EXISTS (SELECT 1 FROM ds.runs WHERE run_id = %(run_id)s AND status = 'running'
                                AND draw IS NOT NULL)""",
        {"run_id": run_id, "cycle": cycle},
    )


async def analyse_new(conn: Conn) -> bool:
    """ANALYZE the tables a cycle end joins when one of them holds rows but no column statistics, as in a
    new store autovacuum has not analysed yet (its insert-driven VACUUM counts the rows, reltuples, but
    gathers no statistics), or one whose rows came after a worker analysed it empty or analysed only the run's
    keys (``analyse_keys``): without them the planner takes a run's rows for one and joins each key to all
    of the run's findings and results, quadratic in differing keys. An empty table has no rows to look at.
    Whether it analysed."""
    if not await _value(
        conn,
        """SELECT EXISTS (SELECT 1 FROM (VALUES ('run_keys', EXISTS (SELECT 1 FROM ds.run_keys)),
                                             ('results', EXISTS (SELECT 1 FROM ds.results)),
                                             ('findings', EXISTS (SELECT 1 FROM ds.findings)),
                                             ('observations', EXISTS (SELECT 1 FROM ds.observations)))
                                     AS t (name, held)
                  WHERE t.held AND NOT EXISTS (SELECT 1 FROM pg_stats s WHERE s.schemaname = 'ds'
                                               AND s.tablename = t.name))""",
        None,
    ):
        return False
    await conn.execute("ANALYZE ds.run_keys, ds.results, ds.findings, ds.observations")
    return True


async def analyse_keys(conn: Conn) -> bool:
    """ANALYZE ds.run_keys when it holds rows but no column statistics, as in a new store autovacuum has not
    analysed yet: until then a plan may find a key among all of its cycle's keys, as the foreign keys of each
    observation and result check it, which every group of a long run pays. Whether it analysed."""
    if not await _value(
        conn,
        """SELECT EXISTS (SELECT 1 FROM ds.run_keys)
              AND NOT EXISTS (SELECT 1 FROM pg_stats WHERE schemaname = 'ds' AND tablename = 'run_keys')""",
        None,
    ):
        return False
    await conn.execute("ANALYZE ds.run_keys")
    return True


async def cycle_counts(conn: Conn, run_id: str, cycle: int | None = None) -> dict[str, dict[str, int]]:
    """Progress of a cycle: ``{"keys": {state: n}, "outcomes": {outcome: n}}``."""
    rows = await _rows(
        conn,
        """SELECT 'keys' AS kind, state AS name, count(*) AS n FROM ds.run_keys
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s GROUP BY state
           UNION ALL
           SELECT 'outcomes', outcome, count(*) FROM ds.results
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s GROUP BY outcome""",
        {"run_id": run_id, "cycle": await _current(conn, run_id, cycle)},
    )
    counts: dict[str, dict[str, int]] = {"keys": {}, "outcomes": {}}
    for row in rows:
        counts[row["kind"]][row["name"]] = row["n"]
    return counts


async def list_keys(
    conn: Conn,
    run_id: str,
    cycle: int | None = None,
    *,
    state: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """A cycle's keys in manifest order: seq, key_masked, key_enc, state, pass_no."""
    rows = await _rows(
        conn,
        """SELECT seq, key_masked, key_enc, state, pass_no FROM ds.run_keys
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s AND seq > %(after)s
             AND (%(state)s::text IS NULL OR state = %(state)s)
           ORDER BY seq LIMIT %(limit)s""",
        {
            "run_id": run_id,
            "cycle": await _current(conn, run_id, cycle),
            "state": state,
            "after": _after(cursor, 1)[0],
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "seq")


async def keys_without_result(
    conn: Conn, run_id: str, cycle: int, *, state: str, limit: int = 1000, cursor: str | None = None
) -> Page:
    """The cycle's keys in ``state`` that hold no result, in manifest order: seq and key_masked."""
    rows = await _rows(
        conn,
        """SELECT k.seq, k.key_masked FROM ds.run_keys k
           WHERE k.run_id = %(run_id)s AND k.cycle = %(cycle)s AND k.seq > %(after)s AND k.state = %(state)s
             AND NOT EXISTS (SELECT 1 FROM ds.results r
                             WHERE (r.run_id, r.cycle, r.key_masked) = (k.run_id, k.cycle, k.key_masked))
           ORDER BY k.seq LIMIT %(limit)s""",
        {"run_id": run_id, "cycle": cycle, "state": state, "after": _after(cursor, 1)[0], "limit": limit + 1},
    )
    return _page(rows, limit, "seq")


async def limit_statements(conn: Conn, seconds: int) -> None:
    """Have the store cancel each statement of the transaction ``conn`` is in, until it ends, that runs
    longer than ``seconds`` (``statement_timeout``): psycopg raises ``errors.QueryCanceled``."""
    await conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{seconds}s",))
