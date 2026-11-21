"""The queries of the worker processes: their registrations, the targets' pauses they share, and the feed
of the stop conditions the sweep judges while more than one processes keys."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from deltasleuth.store.repo.base import Conn, Row, _jsonb, _rows, _value


async def beat_worker(conn: Conn, worker_id: str, sweep: bool, expires_s: float) -> list[str]:
    """Register ``worker_id``, or renew its registration, for ``expires_s`` seconds of the database's clock,
    as the sweep's holder when ``sweep``; drop the registrations that expired. The live workers' ids in
    their order: the sweep's holders first, the latest registered of them first (a holder that stopped
    leaves an expiring row behind), then the others by when each registered."""
    await conn.execute(
        """INSERT INTO ds.workers (worker_id, sweep, expires_at)
           VALUES (%s, %s, now() + %s * interval '1 second')
           ON CONFLICT (worker_id) DO UPDATE SET sweep = EXCLUDED.sweep, expires_at = EXCLUDED.expires_at""",
        (worker_id, sweep, expires_s),
    )
    await conn.execute("DELETE FROM ds.workers WHERE expires_at < now()")
    rows = await _rows(
        conn,
        """SELECT worker_id FROM ds.workers
           ORDER BY sweep DESC, CASE WHEN sweep THEN started_at END DESC, started_at, worker_id""",
    )
    return [row["worker_id"] for row in rows]


async def leave_worker(conn: Conn, worker_id: str) -> None:
    """Drop the registration of ``worker_id``, which stops processing keys."""
    await conn.execute("DELETE FROM ds.workers WHERE worker_id = %s", (worker_id,))


async def live_workers(conn: Conn) -> list[Row]:
    """The registrations that have not expired: worker_id, sweep and started_at, in ``beat_worker``'s
    order."""
    return await _rows(
        conn,
        """SELECT worker_id, sweep, started_at FROM ds.workers WHERE expires_at >= now()
           ORDER BY sweep DESC, CASE WHEN sweep THEN started_at END DESC, started_at, worker_id""",
    )


async def stored_evidence(conn: Conn) -> str | None:
    """The evidence address of an observation the store holds, any one; None before the first."""
    return await _value(
        conn, "SELECT content_hash FROM ds.observations WHERE left(content_hash, 4) = 'hk1:' LIMIT 1", None
    )


async def pause_targets(conn: Conn, pauses: Mapping[tuple[str, str], float]) -> None:
    """Record each pause, by (target id, breaker: '' for a Retry-After, else the breaker's declaration),
    its seconds from the database's now, keeping a later end already recorded. In key order, so that two
    workers recording the same pauses never wait on each other in turn."""
    items = sorted(pauses.items())
    await conn.execute(
        """INSERT INTO ds.target_pauses (target_id, breaker, until)
           SELECT p.target_id, p.breaker, now() + p.seconds * interval '1 second'
           FROM unnest(%s::text[], %s::text[], %s::float8[])
               WITH ORDINALITY AS p (target_id, breaker, seconds, n)
           ORDER BY p.n
           ON CONFLICT (target_id, breaker) DO UPDATE
               SET until = greatest(ds.target_pauses.until, EXCLUDED.until)""",
        ([t for (t, _), _ in items], [b for (_, b), _ in items], [s for _, s in items]),
    )


async def target_pauses(conn: Conn) -> list[Row]:
    """The pauses not yet ended: target_id, breaker and left_s, the seconds left of each on the database's
    clock."""
    return await _rows(
        conn,
        """SELECT target_id, breaker, extract(epoch FROM until - now())::float8 AS left_s
           FROM ds.target_pauses WHERE until > now() ORDER BY target_id, breaker""",
    )


async def feed_stops(conn: Conn, run_id: str, passes: Sequence[Any]) -> None:
    """Add what one saved group of the run gives its stop conditions, in the group's transaction."""
    await conn.execute(
        "INSERT INTO ds.stop_feed (run_id, passes) VALUES (%s, %s)", (run_id, _jsonb(list(passes)))
    )


async def take_stop_feed(conn: Conn) -> list[Row]:
    """Delete and return the stop conditions' feed committed so far: seq, run_id and passes, in the order the
    groups were saved."""
    rows = await _rows(conn, "DELETE FROM ds.stop_feed RETURNING seq, run_id, passes")
    return sorted(rows, key=lambda row: row["seq"])
