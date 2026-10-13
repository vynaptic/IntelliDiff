"""A revoke against a run's creation, its resume, a soak's boundary attach and a sign-off decision, on
two connections in both lock orders: each reads the spec approval FOR SHARE
first, so it either commits first and the revoke's cascade catches it, or waits for the revoke and
finds the approval revoked. Never a running run, nor an approved sign-off, under a revoked approval."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth import approvals
from deltasleuth.core.types import SecurityPolicy
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import (
    OIDC,
    approved,
    audit_as,
    counters,
    end,
    person,
    request_signoff,
    spec,
    start,
    upload,
)

pytestmark = pytest.mark.db

VERSION = spec(security=SecurityPolicy(clear=("$.status",)))
SCOPES = approvals.needs(VERSION)


@dataclass
class Scene:
    """What an operation acts on: the spec approval a revoke withdraws, and the run or sign-off."""

    spec_id: int
    approval_id: str
    run_id: str | None = None
    signoff_id: str | None = None
    approver: str = ""


Step = Callable[[repo.Conn, Scene, Any], Awaitable[Any]]


async def _gate(conn: repo.Conn, scene: Scene) -> Any:
    return await approvals.gate(OIDC, conn, scene.spec_id, VERSION, lock=True)


async def _create(conn: repo.Conn, scene: Scene, gated: Any) -> None:
    approval_id, record = gated
    scene.run_id = await start(conn, scene.spec_id, "user:U-" + "d" * 16, approval_id=approval_id,
                               params={"approval": record})  # fmt: skip


async def _resume(conn: repo.Conn, scene: Scene, gated: Any) -> None:
    approval_id, _ = gated
    assert scene.run_id is not None
    cleared = (approvals.APPROVAL_PAUSE,)
    assert await repo.set_run_status(conn, scene.run_id, "running", when=("paused",), cleared=cleared)
    await repo.set_run_approval(conn, scene.run_id, approval_id)


async def _gate_or_pause(conn: repo.Conn, scene: Scene) -> Any:
    try:
        return await _gate(conn, scene)
    except approvals.ApprovalRequired:
        return None


async def _attach(conn: repo.Conn, scene: Scene, gated: Any) -> None:
    """A soak's boundary attaches the active approval, or starts its next cycle paused."""
    assert scene.run_id is not None
    if gated is None:
        await repo.set_run_status(conn, scene.run_id, "paused", stop_reason=approvals.APPROVAL_PAUSE)
    else:
        await repo.set_run_approval(conn, scene.run_id, gated[0])


async def _share(conn: repo.Conn, scene: Scene) -> Any:
    return await repo.get_approval(conn, scene.approval_id, lock=True)


async def _decide(conn: repo.Conn, scene: Scene, _: Any) -> None:
    """The decision goes ahead whatever it read, as a writer other than the route would: the trigger
    refuses it too."""
    assert scene.signoff_id is not None
    await repo.decide_approval(conn, scene.signoff_id, scene.approver, approve=True)


# operation -> (its first step, which reads the approval FOR SHARE; its write)
OPERATIONS: dict[str, tuple[Callable[[repo.Conn, Scene], Awaitable[Any]], Step]] = {
    "create": (_gate, _create),
    "resume": (_gate, _resume),
    "attach": (_gate_or_pause, _attach),
    "decision": (_share, _decide),
}


@pytest.fixture
async def scene(pool: AsyncConnectionPool, request: pytest.FixtureRequest) -> Scene:
    """An approved version of a spec that needs one, and what the operation under test acts on."""
    operation = request.node.callspec.params["operation"]
    async with pool.connection() as conn:
        bob, carol, dave, olga = [await person(conn, n) for n in ("bob", "carol", "dave", "olga")]
        spec_id = await upload(conn, VERSION, bob)
        if operation == "resume":  # paused when its first approval was revoked, then approved again
            first = await approved(conn, spec_id, bob, carol, scopes=SCOPES)
            run_id = await start(conn, spec_id, dave, approval_id=first)
            await approvals.revoke(conn, audit_as(conn), first, "wrong clear path", who=carol)
        elif operation == "attach":  # a soak started before 008, at a boundary
            run_id = await start(conn, spec_id, dave, mode="soak")
        elif operation == "decision":
            first = await approved(conn, spec_id, bob, carol, scopes=SCOPES)
            run_id = await start(conn, spec_id, dave, approval_id=first, status="completed")
            await end(conn, run_id, counters(1))
            signoff = await request_signoff(conn, run_id, 1, olga, VERSION)
            return Scene(spec_id, first, run_id, signoff, carol.actor)
        else:
            run_id = None
        approval_id = await approved(conn, spec_id, bob, carol, scopes=SCOPES)
    return Scene(spec_id, approval_id, run_id)


@pytest.fixture
async def connections(pg_dsn: str) -> AsyncIterator[tuple[psycopg.AsyncConnection, ...]]:
    """Two connections, and a third that watches them."""
    opened = [await psycopg.AsyncConnection.connect(pg_dsn, autocommit=i == 2) for i in range(3)]
    yield tuple(opened)
    for conn in opened:
        await conn.close()


async def _waiting(watcher: psycopg.AsyncConnection, blocked: psycopg.AsyncConnection) -> None:
    """Return once ``blocked``'s backend waits on a lock."""
    for _ in range(500):
        cur = await watcher.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (blocked.info.backend_pid,)
        )
        if (await cur.fetchone()) == ("Lock",):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the second transaction never waited for the first")


async def _revoke(conn: repo.Conn, scene: Scene) -> dict[str, Any]:
    """The CLI's break-glass revoke: it leaves an open sign-off to be refused at its decision."""
    return await approvals.revoke(conn, audit_as(conn), scene.approval_id, "wrong clear path", who=TESTER)


async def _state(pool: AsyncConnectionPool, scene: Scene) -> tuple[Any, ...]:
    """Running runs and approved sign-offs under a revoked approval; the run's status and the sign-off's."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT (SELECT count(*) FROM ds.runs r JOIN ds.approvals a USING (approval_id)
                       WHERE r.status = 'running' AND a.status <> 'approved'),
                      (SELECT count(*) FROM ds.approvals s JOIN ds.approvals p
                           ON p.approval_id = s.attests->>'spec_approval_id'
                       WHERE s.status = 'approved' AND p.status <> 'approved'),
                      (SELECT status FROM ds.runs WHERE run_id = %s),
                      (SELECT status FROM ds.approvals WHERE approval_id = %s)""",
            (scene.run_id, scene.signoff_id),
        )
        return await cur.fetchone()  # type: ignore[return-value]


@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_an_operation_that_locks_first_is_caught_by_the_revoke(
    pool: AsyncConnectionPool, scene: Scene, connections: tuple[psycopg.AsyncConnection, ...], operation: str
) -> None:
    first, then = OPERATIONS[operation]
    acting, revoking, watcher = connections
    shared = await first(acting, scene)
    revoke = asyncio.create_task(_revoke(revoking, scene))
    await _waiting(watcher, revoking)
    await then(acting, scene, shared)
    await acting.commit()
    done = await revoke
    await revoking.commit()
    state = await _state(pool, scene)
    if operation == "decision":  # the sign-off approved first, then revoked with its spec approval
        assert done["revoked"] == [scene.signoff_id] and state == (0, 0, "completed", "revoked")
    else:  # the run started, resumed or attached, then paused by the revoke
        assert done["paused"] == [scene.run_id] and state == (0, 0, "paused", None)


@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_an_operation_that_waits_for_a_revoke_finds_the_approval_revoked(
    pool: AsyncConnectionPool, scene: Scene, connections: tuple[psycopg.AsyncConnection, ...], operation: str
) -> None:
    first, then = OPERATIONS[operation]
    acting, revoking, watcher = connections
    await _revoke(revoking, scene)

    async def act() -> None:
        await then(acting, scene, await first(acting, scene))

    task = asyncio.create_task(act())
    await _waiting(watcher, acting)
    await revoking.commit()
    if operation == "decision":
        with pytest.raises(repo.ApprovalRefused, match="cites is not approved"):
            await task
    elif operation == "attach":  # its next cycle starts paused
        await task
        await acting.commit()
    else:
        with pytest.raises(approvals.ApprovalRequired, match=f"{scene.approval_id} was revoked"):
            await task
    await acting.rollback()
    state = await _state(pool, scene)
    expected = {"create": None, "resume": "paused", "attach": "paused", "decision": "completed"}[operation]
    assert state == (0, 0, expected, "requested" if operation == "decision" else None)
