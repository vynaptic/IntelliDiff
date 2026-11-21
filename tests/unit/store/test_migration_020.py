"""Migration 020: the blob drain reads the queue's due addresses, oldest first, by an index on when each was
queued, not by a scan and a sort of the whole queue."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.store import repo
from deltasleuth.store.db import migrate

pytestmark = pytest.mark.db

QUEUE_INDEX = 20
QUEUED = 20_000  # a purged cycle's addresses, a third of them not yet due


async def _plan(conn: repo.Conn, monkeypatch: pytest.MonkeyPatch) -> str:
    """The plan of the drain's own statement, as ``repo.gc_due`` sends it, for a batch of 500."""
    sent: dict[str, Any] = {}
    rows = repo._rows

    async def kept(conn: repo.Conn, sql: Any, params: Any = None) -> list[repo.Row]:
        sent.update(sql=sql, params=params)
        return await rows(conn, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(repo.retention, "_rows", kept)
        assert len(await repo.gc_due(conn, 500)) == 500
    cur = await conn.execute(b"EXPLAIN " + sent["sql"].encode(), sent["params"])
    return "\n".join(line for (line,) in await cur.fetchall())


async def test_020_reads_the_due_blobs_by_the_queue_index(
    empty_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await migrate(empty_pool, upto=QUEUE_INDEX - 1) == list(range(1, QUEUE_INDEX))
    async with empty_pool.connection() as conn:
        await conn.execute(
            """INSERT INTO ds.blob_gc (content_hash, queued_at)
               SELECT 'hk1:' || lpad(to_hex(n), 64, '0'), now() - (n %% 72) * interval '1 hour'
               FROM generate_series(1, %s) AS n""",
            (QUEUED,),
        )
        await conn.execute("ANALYZE ds.blob_gc")
        before = await _plan(conn, monkeypatch)
    assert "Seq Scan on blob_gc" in before and "Sort" in before
    assert await migrate(empty_pool, upto=QUEUE_INDEX) == [QUEUE_INDEX]
    async with empty_pool.connection() as conn:
        after = await _plan(conn, monkeypatch)
    assert "Index Scan using blob_gc_queued_idx on blob_gc" in after
    assert "Sort" not in after and "Seq Scan" not in after
