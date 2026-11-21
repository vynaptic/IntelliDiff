"""The queries of the data keys."""

from __future__ import annotations

from deltasleuth.store.repo.base import Conn, Row, _row, _rows


async def data_keys(conn: Conn) -> list[Row]:
    """Every data key, oldest first, as ``wrapped`` by the KEK ``kek``."""
    return await _rows(
        conn,
        "SELECT scope, kek, wrapped, created_at, rewrapped_at FROM ds.data_keys ORDER BY created_at, scope",
    )


async def data_key(conn: Conn, scope: str) -> Row | None:
    return await _row(conn, "SELECT scope, kek, wrapped FROM ds.data_keys WHERE scope = %s", (scope,))


async def insert_data_key(conn: Conn, scope: str, kek: str, wrapped: bytes) -> None:
    await conn.execute(
        "INSERT INTO ds.data_keys (scope, kek, wrapped) VALUES (%s, %s, %s)", (scope, kek, wrapped)
    )


async def rewrap_data_key(conn: Conn, scope: str, kek: str, wrapped: bytes) -> None:
    await conn.execute(
        "UPDATE ds.data_keys SET kek = %s, wrapped = %s, rewrapped_at = now() WHERE scope = %s",
        (kek, wrapped, scope),
    )
