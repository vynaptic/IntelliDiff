"""Store fixtures: a connection pool on a fresh database, migrated or not."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.store.db import migrate


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(pg_dsn, open=False, max_size=8)
    await pool.open(wait=True)
    await migrate(pool)
    yield pool
    await pool.close()


@pytest.fixture
async def empty_pool(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool on a fresh database no migration has touched, for a test that migrates it in steps."""
    pool = AsyncConnectionPool(pg_dsn, open=False, max_size=8)
    await pool.open(wait=True)
    yield pool
    await pool.close()
