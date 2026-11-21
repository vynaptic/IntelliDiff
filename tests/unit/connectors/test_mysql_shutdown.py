"""Close a MySQL session without abandoning a result while its cursor still creates it."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pymysql
import pytest

from deltasleuth.connectors.tables import mysql


async def test_cancel_during_result_creation_disarms_the_late_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PyMySQL creates the result locally, then assigns it to the connection and finally its cursor.
    # Hold precisely the interval after metadata arrived but before either assignment.
    metadata, released = threading.Event(), threading.Event()
    results: list[pymysql.connections.MySQLResult] = []

    def initialise(result: pymysql.connections.MySQLResult) -> None:
        result.unbuffered_active = True
        result.affected_rows = 0
        results.append(result)
        metadata.set()
        assert released.wait(3), "shutdown did not wake the pending driver call"

    monkeypatch.setattr(pymysql.connections.MySQLResult, "init_unbuffered_query", initialise)
    monkeypatch.setattr(mysql._Connection, "_execute_command", lambda *args: None)
    conn = mysql._Connection(defer_connect=True)
    thread = ThreadPoolExecutor(1)
    session = mysql._Session(SimpleNamespace(target=None), thread, 0)
    session._conn = conn
    session._sock = SimpleNamespace(shutdown=lambda how: released.set(), close=lambda: None)
    monkeypatch.setattr(session, "_kill", lambda: None)
    batches = session.stream("SELECT 1")
    fetching = asyncio.create_task(anext(batches))
    try:
        assert await asyncio.to_thread(metadata.wait, 3)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fetching
        await session.close()
        await asyncio.to_thread(thread.shutdown, wait=True)
        assert len(results) == 1 and conn._result is results[0]
        assert results[0].unbuffered_active is False
    finally:
        released.set()
        fetching.cancel()
        await asyncio.gather(fetching, return_exceptions=True)
        await asyncio.to_thread(thread.shutdown, wait=True)
        # A failing regression must not leave an unraisable destructor exception in another test.
        for result in results:
            result.unbuffered_active = False
        await batches.aclose()


async def test_closing_an_open_stream_disarms_it_on_its_driver_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = ThreadPoolExecutor(1)
    conn = mysql._Connection(defer_connect=True)
    result = pymysql.connections.MySQLResult(conn)
    result.unbuffered_active = True
    conn._result = result
    cursor = conn.cursor(mysql._Stream)
    cursor._result = result
    events: list[tuple[str, int]] = []

    def record(name: str) -> None:
        events.append((name, threading.get_ident()))

    session = mysql._Session(SimpleNamespace(target=None), thread, 0)
    session._conn, session._stream = conn, cursor
    session._sock = SimpleNamespace(shutdown=lambda how: record("shutdown"), close=lambda: record("socket"))
    monkeypatch.setattr(session, "_kill", lambda: record("kill"))
    abandon, close = cursor.abandon, conn.close

    def abandoned() -> None:
        record("abandon")
        abandon()

    def closed() -> None:
        record("connection")
        close()

    monkeypatch.setattr(cursor, "abandon", abandoned)
    monkeypatch.setattr(conn, "close", closed)
    try:
        await session.close()
        await asyncio.to_thread(thread.shutdown, wait=True)
        assert [name for name, _ in events] == ["kill", "shutdown", "socket", "abandon", "connection"]
        assert events[-2][1] == events[-1][1] != threading.get_ident()
        assert not result.unbuffered_active and cursor.connection is None and session._stream is None
    finally:
        result.unbuffered_active = False
        await asyncio.to_thread(thread.shutdown, wait=True)
