"""Calls limited to so many a minute per caller, counted in the store: one pool stands for one process, so a
limit holds across the app's processes and its restarts."""

from __future__ import annotations

import asyncio

import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.store import repo

pytestmark = pytest.mark.db


async def _hit(pool: AsyncConnectionPool, name: str, actor: str, limit: int = 3) -> bool:
    async with pool.connection() as conn:
        return await repo.hit_rate(conn, name, actor, limit)


async def test_a_rate_holds_per_name_and_caller_across_processes(
    pool: AsyncConnectionPool, pg_dsn: str
) -> None:
    """Three a minute: the fourth is refused, whichever process counts it; another caller or another name
    has places of its own; a minute on, the oldest hit's place is free again."""
    assert [await _hit(pool, "chat.ask", "user:U-1") for _ in range(3)] == [True, True, True]
    async with AsyncConnectionPool(pg_dsn, open=False, min_size=1, max_size=1) as other:  # a restart, say
        assert not await _hit(other, "chat.ask", "user:U-1")
        assert await _hit(other, "chat.ask", "user:U-2") and await _hit(other, "mcp.compare_key", "user:U-1")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.rate_hits SET at = at - interval '61 seconds' WHERE at = (SELECT min(at) FROM"
            " ds.rate_hits WHERE name = 'chat.ask' AND actor = 'user:U-1')"
        )
    assert await _hit(pool, "chat.ask", "user:U-1") and not await _hit(pool, "chat.ask", "user:U-1")


async def test_hits_at_once_never_take_more_places_than_the_limit(pool: AsyncConnectionPool) -> None:
    """Twenty hits of one caller at once: three get a place."""
    hits = await asyncio.gather(*(_hit(pool, "chat.ask", "user:U-1") for _ in range(20)))
    assert sum(hits) == 3
