"""The query of the calls limited to so many a minute per caller, counted in the store so that a limit holds
across the app's processes and its restarts."""

from __future__ import annotations

from deltasleuth.store.repo.base import Conn, _value


async def hit_rate(conn: Conn, name: str, actor: str, limit: int, window_s: float = 60.0) -> bool:
    """Count one hit of ``actor`` under ``name`` when fewer than ``limit`` of its hits fell within the last
    ``window_s`` seconds; whether it did. A caller's hits are judged one at a time (a transaction lock on the
    name and caller), so two at once never both take the last place; its hits past the window go first."""
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"rate\x1f{name}\x1f{actor}",)
        )
        await conn.execute(
            """DELETE FROM ds.rate_hits WHERE name = %s AND actor = %s
                   AND at < clock_timestamp() - %s * interval '1 second'""",
            (name, actor, window_s),
        )
        within: bool = await _value(
            conn,
            "SELECT count(*) < %s FROM ds.rate_hits WHERE name = %s AND actor = %s",
            (limit, name, actor),
        )
        if within:
            await conn.execute("INSERT INTO ds.rate_hits (name, actor) VALUES (%s, %s)", (name, actor))
        return within
