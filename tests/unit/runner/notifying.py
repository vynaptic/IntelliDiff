"""The notifier's runner tests: channels to local receivers, audit rows written as their writers write them,
runs with ended cycles, and what the notifier stored."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth import notifications
from deltasleuth.auth.principal import WORKER
from deltasleuth.runner.counters import count_cycle
from deltasleuth.runner.notify import Notifier
from deltasleuth.store import repo
from mocks.notify import Receiver
from tests.conftest import create_run

from .conftest import Harness, OpenHarness, spec_text

HEALTH = [
    "run.paused",
    "run.stopped",
    "run.failed",
    "run.finished",
    "sample.draw_failed",
    "notify.dead",
    "notify.skipped",
]
EMPTY = count_cycle([], {}, total=0, live=True, recheck_outcomes=[])


def key(path: Path, raw: bytes | None = None) -> tuple[str, bytes]:
    """A signing key file: its reference and the raw key."""
    raw = raw or os.urandom(32)
    path.write_text(f"whsec_{base64.b64encode(raw).decode()}\n")
    return f"file:{path}", raw


def hook(base: str, ref: str, **options: Any) -> dict[str, Any]:
    return {
        "kind": "webhook",
        "url": f"{base}/webhook",
        "signing_key_refs": [ref],
        "allow_private_network": True,
    } | options


def rule(name: str, events: list[str], channels: list[str], **fields: Any) -> dict[str, Any]:
    return {"name": name, "events": events, "channels": channels} | fields


@asynccontextmanager
async def receiving(tmp_path: Path, name: str = "hook") -> AsyncIterator[tuple[Receiver, str, str]]:
    """A receiver verifying one signing key: it, its base URL and the key's reference."""
    ref, raw = key(tmp_path / f"{name}.key")
    found = Receiver([raw])
    base = await found.start()
    try:
        yield found, base, ref
    finally:
        await found.close()


@asynccontextmanager
async def notifying(
    open_harness: OpenHarness,
    service_base: str,
    channels: Mapping[str, Any],
    rules: list[dict[str, Any]],
    **options: Any,
) -> AsyncIterator[tuple[Harness, Notifier]]:
    """A harness whose notify settings are ``channels`` and ``rules``, and a notifier on it, closed after."""
    from .conftest import TARGETS

    notify = {"label": "test", "channels": dict(channels), "rules": rules} | options
    async with open_harness(TARGETS.format(base=service_base, rate=500), notify=notify) as h:
        notifier = Notifier(h.ctx)
        try:
            yield h, notifier
        finally:
            await notifier.close()


async def write(h: Harness, action: str, obj: str, detail: Mapping[str, Any]) -> int:
    """Audit a row as its writer does, last in its transaction; its chain seq."""
    async with repo.audited(h.ctx.pool) as (_, audit):
        row = await audit(WORKER, action, obj, detail)
    return row["chain"]["seq"]


async def queued(h: Harness, **params: Any) -> str:
    """A queued run of the unit spec with keys K001 to K003 (masked as they are)."""
    spec_id = await h.spec(spec_text())
    async with h.ctx.pool.connection() as conn:
        run_id, _ = await create_run(
            h.ctx,
            conn,
            spec_id,
            "soak",
            {k: k for k in ("K001", "K002", "K003")},
            {"keys_source": "list"} | params,
        )
    return run_id


async def ended(h: Harness, run_id: str, cycle: int, counters: Mapping[str, Any] | None = None) -> None:
    """End the run's cycle ``cycle`` as the worker does: its counters, its tombstone."""
    async with h.ctx.pool.connection() as conn:
        await conn.execute("UPDATE ds.runs SET cycle = %s WHERE run_id = %s", (cycle, run_id))
        await repo.set_counters(conn, run_id, dict(counters or EMPTY) | {"cycle": cycle})
        await repo.end_cycle(conn, run_id)


async def deliveries(h: Harness) -> list[repo.Row]:
    async with h.ctx.pool.connection() as conn:
        return await repo._rows(conn, "SELECT * FROM ds.notify_deliveries ORDER BY delivery_id")


async def audits(h: Harness, action: str) -> list[repo.Row]:
    async with h.ctx.pool.connection() as conn:
        rows, _ = await repo.list_audit(conn, action=action, limit=1000)
    return rows[::-1]


async def cursor(h: Harness) -> int:
    async with h.ctx.pool.connection() as conn:
        return (await repo.notify_cursor(conn))["seq"]


def stall_in_digest(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Hold the next digest's first savepoint just after its SAVEPOINT ran, inside ``__aenter__``: where
    psycopg leaves a savepoint that a cancel lands on (it lets the command finish, then raises). The list
    gains an item once one is held."""
    held: list[int] = []
    armed = False
    enter, digest = psycopg.AsyncTransaction.__aenter__, notifications.digest

    async def entered(self: psycopg.AsyncTransaction) -> Any:
        nonlocal armed
        found = await enter(self)
        if armed and self.savepoint_name:
            armed = False
            held.append(1)
            await asyncio.sleep(3600)
        return found

    async def digesting(*args: Any, **kwargs: Any) -> Any:
        nonlocal armed
        armed = True
        return await digest(*args, **kwargs)

    monkeypatch.setattr(psycopg.AsyncTransaction, "__aenter__", entered)
    monkeypatch.setattr(notifications, "digest", digesting)
    return held
