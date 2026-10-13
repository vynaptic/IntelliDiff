"""Audit and logs (docs/mvp/MCP.md §9): a key view and a compare audited as the API audits them, as their
caller and with ``via``; ``mcp.read`` once an hour per caller, tool and run, and no read answered before
the row that covers it is committed; a role refusal once a minute, the others logged; nothing for a list;
and one log line a call, which holds no argument."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import psycopg
import pytest
from mcp import Client

from deltasleuth.auth.principal import Throttle
from deltasleuth.mcp import server, tools
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.runner.conftest import until

from .conftest import EVERY, OPERATOR, VIEWER, World, ask, audited, token
from .test_limits import Clock
from .test_tools import READS, args_of

pytestmark = pytest.mark.db
READ = ("get_run_summary", "get_root_causes", "get_findings")
# A store that serves reads and takes no ``mcp.read`` row, in a read-only store's words.
REFUSING = """
    CREATE FUNCTION public.no_read_rows() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.action = 'mcp.read' THEN
        RAISE EXCEPTION 'cannot execute INSERT in a read-only transaction' USING ERRCODE = '25006';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER no_read_rows BEFORE INSERT ON ds.audit_log
      FOR EACH ROW EXECUTE FUNCTION public.no_read_rows();
"""


def told(row: repo.Row) -> tuple[str, str, str, str, dict[str, Any], str]:
    return row["actor"], row["actor_name"], row["action"], row["object"], row["detail"], row["request_id"]


async def test_a_key_view_is_audited_once_its_result_is_built(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, asked = world.ctx, {"run_id": world.run_id, "key": world.key}
    before = len(await audited(ctx))
    assert (await ask(ctx, VIEWER, "get_observations", asked))[1] is False
    monkeypatch.setattr(tools, "CAP", 2500)  # a cut result is a key view all the same
    cut, _ = await ask(ctx, VIEWER, "get_observations", asked)
    monkeypatch.undo()
    assert cut["truncated"] and (await ask(ctx, VIEWER, "get_observations", asked | {"key": "tok:x"}))[1]
    viewed = {"key_masked": world.key, "cycle": 1, "via": "mcp:http"}
    assert [told(row) for row in (await audited(ctx))[before:]] == [
        (VIEWER.actor, "agent-1", "key.view", world.run_id, viewed, VIEWER.request_id)
    ] * 2  # none for the key that is not the run's

    async def unwritable(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the audit log is away")

    monkeypatch.setattr(repo, "insert_audit", unwritable)  # no row, no result
    assert await ask(ctx, VIEWER, "get_observations", asked) == (
        f"internal error: request {VIEWER.request_id} names it in the server log", True,
    )  # fmt: skip


async def test_mcp_read_once_an_hour_per_caller_tool_and_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, clock = world.ctx, Clock()
    limits = tools.Limits(EVERY, clock=clock)

    async def read(who: Any, name: str, run_id: str = world.run_id) -> bool:
        return (await ask(ctx, who, name, {"run_id": run_id}, limits=limits))[1]

    async def rows() -> list[Any]:
        return [(row["actor"], row["object"], row["detail"]) for row in await audited(ctx, "mcp.read")]

    for name in READ:
        assert [await read(VIEWER, name) for _ in range(3)] == [False] * 3
    first = [(VIEWER.actor, world.run_id, {"tool": name, "calls": 1, "via": "mcp:http"}) for name in READ]
    assert await rows() == first  # the first call of an hour: when this agent began to read the run
    assert not await read(OPERATOR, "get_findings") and not await read(
        VIEWER, "get_findings", world.clear_run
    )
    assert await read(VIEWER, "get_findings", "R-" + "0" * 26)  # a refused read marks nothing
    other = [
        (OPERATOR.actor, world.run_id, {"tool": "get_findings", "calls": 1, "via": "mcp:http"}),
        (VIEWER.actor, world.clear_run, {"tool": "get_findings", "calls": 1, "via": "mcp:http"}),
    ]
    assert await rows() == first + other
    clock.now += 3599
    assert not await read(VIEWER, "get_findings") and await rows() == first + other
    clock.now += 1  # the next hour's row counts the calls since the row before, this one included
    assert not await read(VIEWER, "get_findings")
    again = (VIEWER.actor, world.run_id, {"tool": "get_findings", "calls": 4, "via": "mcp:http"})
    assert await rows() == [*first, *other, again]
    insert, clock.now = repo.insert_audit, clock.now + 3600

    async def unwritable(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the audit log is away")

    monkeypatch.setattr(repo, "insert_audit", unwritable)
    assert await read(VIEWER, "get_run_summary")  # the row cannot be written: the call fails
    monkeypatch.setattr(repo, "insert_audit", insert)
    assert not await read(VIEWER, "get_run_summary")  # and the next read is due it
    assert (await rows())[-1] == (
        VIEWER.actor,
        world.run_id,
        {"tool": "get_run_summary", "calls": 1, "via": "mcp:http"},
    )
    assert [row["request_id"] for row in await audited(ctx, "mcp.read")] == [VIEWER.request_id] * 3 + [
        OPERATOR.request_id, *[VIEWER.request_id] * 3,
    ]  # fmt: skip


@pytest.mark.parametrize("ending", ["commits", "fails", "cancelled"])
async def test_a_read_waits_for_the_row_that_covers_it(
    world: World, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """A read that writes no row is answered once the row of its caller, tool and run is committed, never
    while it is being written; when that row is not written, because its write fails or its call is
    cancelled, a read that waited for it is due the row."""
    ctx, limits = world.ctx, tools.Limits(EVERY)
    insert, writing, release = repo.insert_audit, asyncio.Event(), asyncio.Event()
    key = (VIEWER.actor, "get_findings", world.run_id)

    async def held(conn: Any, who: Any, action: str, *rest: Any) -> Any:
        if action == "mcp.read" and not writing.is_set():  # the hour's first row: held, then as ``ending``
            writing.set()
            await release.wait()
            if ending == "fails":
                raise psycopg.OperationalError("the connection is lost")
        return await insert(conn, who, action, *rest)

    def reading() -> asyncio.Task[tuple[Any, bool]]:
        return asyncio.create_task(ask(ctx, VIEWER, "get_findings", {"run_id": world.run_id}, limits=limits))

    monkeypatch.setattr(repo, "insert_audit", held)
    first = reading()
    await writing.wait()
    second, third = reading(), reading()
    await until(lambda: limits._reading[key][1] == 3)  # both have their results, and wait for the row
    await asyncio.sleep(0.05)
    assert not second.done() and not third.done() and await audited(ctx, "mcp.read") == []
    if ending == "cancelled":
        first.cancel()
        assert (await asyncio.gather(first, return_exceptions=True))[0].__class__ is asyncio.CancelledError
    else:
        release.set()
        told = ("the database is unreachable; call again", True) if ending == "fails" else None
        assert await first == (told or await second)
    served = await asyncio.gather(second, third)
    assert [failed for _, failed in served] == [False, False] and served[0][0]["findings"]
    assert [(row["actor"], row["object"], row["detail"]) for row in await audited(ctx, "mcp.read")] == [
        (VIEWER.actor, world.run_id, {"tool": "get_findings", "calls": 1, "via": "mcp:http"})
    ]  # the first call's, or the row of the read that waited longest
    assert not limits._reading  # a key's lock goes with its last call


async def test_no_read_is_served_while_its_row_cannot_be_written(world: World) -> None:
    """Of reads made at once on a store that takes no ``mcp.read`` row, each one fails: none is answered
    under another's row that was not written."""
    ctx, limits = world.ctx, tools.Limits(EVERY)
    async with ctx.pool.connection() as conn:
        await conn.execute(REFUSING)
    for name in READ:
        for at_once in (2, 3):
            calls = [ask(ctx, VIEWER, name, {"run_id": world.run_id}, limits=limits) for _ in range(at_once)]
            assert [failed for _, failed in await asyncio.gather(*calls)] == [True] * at_once, name
    assert await audited(ctx, "mcp.read") == [] and not limits._reading
    assert not (await ask(ctx, VIEWER, "list_runs", limits=limits))[1]  # a read that writes no row is served


async def test_a_compare_is_audited_as_its_caller(world: World) -> None:
    ctx = world.ctx
    before = len(await audited(ctx))
    data, failed = await ask(ctx, OPERATOR, "compare_key", {"run_id": world.run_id, "key": world.key})
    assert not failed
    made = [row for row in (await audited(ctx))[before:]]
    assert [(row["action"], row["actor"], row["actor_name"], row["request_id"]) for row in made] == [
        ("compare_one", OPERATOR.actor, "agent-2", OPERATOR.request_id),
        ("run.finished", OPERATOR.actor, "agent-2", OPERATOR.request_id),
    ]  # the runner's rows, and no licence.pair: the name held its seat
    assert all(row["detail"]["via"] == "mcp:http" for row in made) and made[0]["object"] == made[1]["object"]
    assert made[0]["detail"]["spec_id"] == data["spec_id"] == world.spec_id


async def test_a_role_refusal_is_audited_once_a_minute(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    ctx, clock = world.ctx, Clock()
    limits = tools.Limits(EVERY, denials=Throttle(60, clock=clock), clock=clock)  # ds serve hands its own
    asked = {"run_id": world.run_id, "key": world.key}
    refused = ("compare_key needs the operator role", True)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.auth"):
        thrice = [await ask(ctx, VIEWER, "compare_key", asked, limits=limits) for _ in range(3)]
        assert thrice == [refused] * 3
        clock.now += 60
        assert await ask(ctx, VIEWER, "compare_key", asked, limits=limits) == refused
        assert await ask(ctx, token("viewer", 7), "compare_key", asked, limits=limits) == refused
    # The refusals that write no row are WARNING lines on the logger of the routes' own (AUTH §6).
    suppressed = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "deltasleuth.auth"]
    line = f"authz.denied actor={VIEWER.actor} route=MCP compare_key needed=operator (suppressed)"
    assert suppressed == [("WARNING", line)] * 2
    denied = {"needed": "operator", "via": "mcp:http"}
    assert [told(row)[:5] for row in await audited(ctx, "authz.denied")] == [
        (VIEWER.actor, "agent-1", "authz.denied", "MCP compare_key", denied | {"count": 1}),
        (VIEWER.actor, "agent-1", "authz.denied", "MCP compare_key", denied | {"count": 3}),
        ("token:T-0000000000000007", "agent-7", "authz.denied", "MCP compare_key", denied | {"count": 1}),
    ]
    assert not await audited(ctx, "compare_one", "key.view", "mcp.read")


async def test_lists_write_nothing_and_a_log_line_holds_no_argument(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = world.ctx
    before = len(await audited(ctx))
    asked = {name: args_of(world, name) for name in READS if name not in (*READ, "get_observations")}
    asked["list_runs"] = {"spec_id": 424242, "schedule_id": "SC-ARGUMENT", "cursor": "R-ARGUMENT"}
    asked["get_readiness"] = {"entity": "argument-entity"}
    asked["list_schedules"] = {"spec_name": "argument-spec"}
    asked |= {"nope": {"argument": "x"}, "get_findings": {"run_id": "R-ARGUMENT", "key": "argument-key"}}
    with caplog.at_level(logging.INFO, logger="deltasleuth.mcp"):
        failed = {name: (await ask(ctx, VIEWER, name, args))[1] for name, args in asked.items()}
        assert not (await ask(ctx, VIEWER, "get_run_summary", {"run_id": world.run_id}))[1]
    assert [name for name, refused in failed.items() if refused] == ["nope", "get_findings"]
    lines = [record.getMessage() for record in caplog.records if record.name == "deltasleuth.mcp"]
    assert len(lines) == len(asked) + 1 and not [line for line in lines if "argument" in line.lower()]
    shape = rf"tool=(\S+) actor={VIEWER.actor} run=(\S+) bytes=(\d+) ms=\d+ error=([01]) request_id=(\S+)"
    seen = [re.fullmatch(shape, line).groups() for line in lines]  # type: ignore[union-attr]
    assert [(tool, run, error) for tool, run, _, error, _ in seen] == [
        *[(name, "-", "0") for name in asked if name not in ("nope", "get_findings")],
        ("?", "-", "1"),  # an unknown tool's name is its caller's text
        ("get_findings", "-", "1"),  # a run id is logged once it named a stored run
        ("get_run_summary", world.run_id, "0"),
    ]
    assert {request_id for *_, request_id in seen} == {VIEWER.request_id} and all(
        int(n) > 0 for _, _, n, _, _ in seen
    )
    assert len(await audited(ctx)) == before + 1  # the summary's mcp.read alone


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_over_stdio_each_call_has_its_own_request_id(world: World, mode: str) -> None:
    """§5.2: the stdio caller is the CLI's, asserted; each of its calls is a request of its own."""
    ctx, asked = world.ctx, {"run_id": world.run_id, "key": world.key}
    local = tools.caller(TESTER, tools.STDIO)
    assert local.roles == {"viewer", "operator"} and local.request_id is None
    async with Client(server.build(ctx, EVERY, who=local), mode=mode) as client:
        for _ in range(2):
            assert not (await client.call_tool("get_observations", asked)).is_error
        refused = await client.call_tool("get_schedule", {"schedule": "nope"})
    assert refused.is_error and '"error": "schedule nope not found"' in refused.content[0].text
    first, second = [row for row in await audited(ctx, "key.view") if row["actor"] == TESTER.actor]
    assert first["request_id"] != second["request_id"]
    assert all(re.fullmatch(r"C-[0-9a-f]{16}", row["request_id"]) for row in (first, second))
    assert first["detail"] == {
        "key_masked": world.key, "cycle": 1, "via": "mcp:stdio", "asserted": True, "os_account": "tester",
    }  # fmt: skip
