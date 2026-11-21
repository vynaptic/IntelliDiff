"""The queries of a connection's session: the locks it holds, probes and transaction settings."""

from __future__ import annotations

from deltasleuth.store.repo.base import Conn, _value

WORKER_LOCK = 0x44535752  # pg advisory lock "DSWR": the one worker of a database, or ds keys migrate
PURGE_LOCK = 0x44535047  # pg advisory lock "DSPG": one purger per database


async def try_worker_lock(conn: Conn) -> bool:
    """Take the worker lock for the session of ``conn``, without waiting: False while another holds it.
    It is held until it is unlocked or the session ends."""
    return bool(await _value(conn, "SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)))


async def try_purge_lock(conn: Conn) -> bool:
    """Take the purge lock for the session of ``conn``, without waiting: False while another holds it."""
    return bool(await _value(conn, "SELECT pg_try_advisory_lock(%s)", (PURGE_LOCK,)))


async def ping(conn: Conn) -> None:
    """One round trip on ``conn``: whether its session still answers."""
    await conn.execute("SELECT 1")


async def read_one_snapshot(conn: Conn) -> None:
    """Make the transaction of ``conn`` read only, every statement of it on one snapshot. It is the
    transaction's first statement."""
    await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


async def bound_notify_feed(conn: Conn) -> None:
    """Bound the notifier's feed transaction: a statement of it ends after 30 s, a lock wait after 5 s."""
    await conn.execute("SET LOCAL statement_timeout = '30s'")
    await conn.execute("SET LOCAL lock_timeout = '5s'")
