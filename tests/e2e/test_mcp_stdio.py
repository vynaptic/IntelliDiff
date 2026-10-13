"""``ds mcp`` in a process of its own (docs/mvp/MCP.md §5.2, §12): over a store whose migrations are still
pending, its stdout holds the protocol's frames and nothing else, whatever the process and a child of it
write, and its stderr another logger's name and level alone; it keeps one store connection while idle; it
ends, exit 0, at the end of its input and at a SIGTERM; a SIGTERM also ends it once its serving has ended
otherwise, and a second one when its loop is blocked; and the MCP library's own stdio client reads a run of
a mock pair through it and compares a key, each call audited as the asserted local caller."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp import Client, StdioServerParameters, stdio_client

from deltasleuth.mcp import tools
from deltasleuth.runner import create_run
from mocks.registry import pair_dir
from tests.conftest import TESTER

from .conftest import finish, raw_pii, store_spec

pytestmark = [pytest.mark.e2e, pytest.mark.db]
DS = "from deltasleuth.cli.main import app; app()"
# ``ds`` that takes an interrupt whatever started the tests (a background job's children ignore it).
INTERRUPTED = f"import signal; signal.signal(signal.SIGINT, signal.default_int_handler); {DS}"
# A store's address, account and database, as psycopg words a connection in its loggers' lines.
STORE = "(host=db.internal.example port=6432 user=Account-Canary database=Database-Canary)"
# ``ds``, each of whose tool calls first writes where a careless tool might: to sys.stdout, to fd 1, and from
# a child process that inherits fd 1; and logs as the driver and a service do when the store fails them.
NOISY = f"""\
import logging, os, subprocess
from deltasleuth.mcp import tools

call = tools.call

async def noisy(*args, **kwargs):
    print("a stray print", flush=True)
    os.write(1, b"a stray write\\n")
    subprocess.run(["/bin/sh", "-c", "echo a stray child; cat"], check=True)
    logging.getLogger("psycopg.pool").warning("error connecting in 'pool-1': %s", {STORE!r})
    try:
        raise RuntimeError({STORE!r})
    except RuntimeError:
        logging.getLogger("deltasleuth.readiness").warning("the standing could not be read", exc_info=True)
    return await call(*args, **kwargs)

tools.call = noisy
from deltasleuth.cli.main import app
app()
"""
STRAYS = ("a stray print", "a stray write", "a stray child")
BARE = ("psycopg.pool WARNING", "deltasleuth.readiness WARNING")  # all that stderr holds of those records
MCP = {"data_classes": [tools.AGGREGATES, tools.TOKENISED, tools.CLEAR], "work": ["compare_key"]}
HELLO = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "raw", "version": "0"}}
# Answers no client reads, beyond what a pipe and this test's reader hold; calls whose lines fill a pipe.
UNREAD, CALLS = 400, 20_000
TARGETS = "targets:\n  svc: {base_url: 'http://127.0.0.1:9/', allow_private_network: true}\n"  # never called


def frame(method: str, params: dict[str, Any] | None = None, *, n: int | None = None) -> bytes:
    message = {"jsonrpc": "2.0", "method": method} | ({} if n is None else {"id": n})
    return json.dumps(message | ({} if params is None else {"params": params})).encode() + b"\n"


async def started(
    repo_root: Path, settings: Path, code: str = DS, *args: str, stderr: int = asyncio.subprocess.PIPE
) -> asyncio.subprocess.Process:
    """``ds mcp ARGS`` with pipes for its three streams, as an agent's host starts it."""
    argv = [sys.executable, "-c", code, "mcp", *args, "--config", str(settings)]
    env = {name: value for name, value in os.environ.items() if name not in ("DS_OPERATOR", "DS_CONFIG")}
    pipe = asyncio.subprocess.PIPE
    return await asyncio.create_subprocess_exec(
        *argv, cwd=repo_root, env=env, stdin=pipe, stdout=pipe, stderr=stderr
    )


async def answered(process: asyncio.subprocess.Process, sent: bytes) -> dict[str, Any]:
    """The next line of the process's stdout, once ``sent`` was written to its stdin."""
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(sent)
    await process.stdin.drain()
    return json.loads(await asyncio.wait_for(process.stdout.readline(), 60))


async def ended(process: asyncio.subprocess.Process) -> tuple[int, str, str]:
    """Its exit code, and what is left of its stdout and stderr."""
    assert process.stdout is not None and process.stderr is not None
    out, err = await asyncio.wait_for(asyncio.gather(process.stdout.read(), process.stderr.read()), 30)
    return await asyncio.wait_for(process.wait(), 30), out.decode(), err.decode()


async def test_stdout_holds_only_frames(make_stack: Any, repo_root: Path, pg_dsn: str) -> None:
    stack = make_stack(TARGETS, mcp=MCP)  # a fresh database: every migration is pending
    process = await started(repo_root, stack.settings, NOISY)
    try:
        hello = await answered(process, frame("initialize", HELLO, n=1))
        listed = await answered(process, frame("notifications/initialized") + frame("tools/list", n=2))
        called = await answered(process, frame("tools/call", {"name": "list_runs"}, n=3))
        refused = await answered(process, b"not a frame\n" + frame("tools/call", {"name": "nope"}, n=4))
        async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
            idle = await conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid()"
            )
            (connections,) = await idle.fetchone()  # type: ignore[misc]
        assert process.stdin is not None
        process.stdin.close()  # the end of input
        code, rest, err = await ended(process)
    finally:
        if process.returncode is None:
            process.kill()
    assert (code, rest) == (0, "")  # the four answers read above were everything it wrote there
    assert [answer["id"] for answer in (hello, listed, called)] == [1, 2, 3]
    assert hello["result"]["serverInfo"]["name"] == "deltasleuth"
    assert [tool["name"] for tool in listed["result"]["tools"]] == list(tools.CATALOGUE)[:-1]  # a viewer's
    assert json.loads(called["result"]["content"][0]["text"]) == {
        "tool": "list_runs",
        "data": {"runs": [], "next_cursor": None},
    }
    # A line that is no frame is the library's to answer, and the server goes on: the next call is refused
    # as a tool error.
    assert refused["id"] == 4 and json.loads(refused["result"]["content"][0]["text"]) == {
        "tool": "nope",
        "error": "unknown tool 'nope'",
    }
    lines = err.splitlines()
    (serving,) = [line for line in lines if line.startswith("mcp: ")]
    assert re.fullmatch(
        r"mcp: serving stdio as local:[A-Za-z0-9._-]+@[A-Za-z0-9._-]+, role viewer; data classes aggregates,"
        r" production_tokenised, production_clear_declared; work tools compare_key",
        serving,
    )
    # What the process, its descriptor and its child wrote to "stdout" is on stderr, once for each call.
    assert [sum(line == stray for line in lines) for stray in STRAYS] == [2, 2, 2]
    migrated = [json.loads(line) for line in lines if line.startswith("{")]
    assert "system.migrate" in {row["action"] for row in migrated}  # the SIEM copy of its own audit rows
    calls = [line for line in lines if line.startswith("tool=")]
    shape = r"tool=(\S+) actor=local:\S+ run=- bytes=\d+ ms=\d+ error=([01]) request_id=(C-[0-9a-f]{16})"
    seen = [re.fullmatch(shape, line).groups() for line in calls]  # type: ignore[union-attr]
    assert [(name, failed) for name, failed, _ in seen] == [("list_runs", "0"), ("?", "1")]
    assert seen[0][2] != seen[1][2]  # a request id a call
    # Another logger's record is its name and level alone: no word of the driver's for the store, no trace.
    assert [sum(line == bare for line in lines) for bare in BARE] == [2, 2]
    assert "Traceback" not in err and "Canary" not in err and "db.internal" not in err and connections == 1


async def test_a_sigterm_ends_it_as_the_end_of_input_does(make_stack: Any, repo_root: Path) -> None:
    """§12: exit 0, and promptly, though the client has not closed the stream: the library's reader, a
    thread waiting for a line, is not waited for."""
    process = await started(repo_root, make_stack(TARGETS, mcp=MCP).settings)
    try:
        hello = await answered(process, frame("initialize", HELLO, n=1))
        process.send_signal(signal.SIGTERM)
        code, rest, err = await ended(process)
    finally:
        if process.returncode is None:
            process.kill()
    assert (hello["id"], code, rest) == (1, 0, "") and "Traceback" not in err


async def gone(process: asyncio.subprocess.Process, within: float) -> int | None:
    """Its exit code once it has ended, None when it still runs ``within`` seconds from now. Not ``wait``,
    which also waits for the end of a stdout whose reader has stopped reading."""
    with suppress(TimeoutError):
        async with asyncio.timeout(within):
            while process.returncode is None:
                await asyncio.sleep(0.05)
    return process.returncode


@pytest.mark.parametrize("ending", ["an interrupt", "unread answers"])
async def test_a_sigterm_ends_it_once_its_serving_has_ended(
    make_stack: Any, repo_root: Path, ending: str
) -> None:
    """§5.2: after an interrupt, and at the end of input while the client has not read its answers, the
    serving has ended but the process still waits to leave the library's transport, where nothing waits for
    a SIGTERM's event any more. The signal is without a handler by then, and ends the process."""
    process = await started(repo_root, make_stack(TARGETS, mcp=MCP).settings, INTERRUPTED)
    try:
        hello = await answered(process, frame("initialize", HELLO, n=1))
        assert process.stdin is not None
        if ending == "an interrupt":
            process.send_signal(signal.SIGINT)
        else:  # more answers than the pipe and this test's reader hold between them
            listing = [frame("tools/list", n=n) for n in range(2, 2 + UNREAD)]
            process.stdin.write(frame("notifications/initialized") + b"".join(listing))
            await process.stdin.drain()
            process.stdin.close()
        waiting = await gone(process, 1) is None
        process.send_signal(signal.SIGTERM)
        code = await gone(process, 30)
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), 30)
    assert (hello["id"], waiting, code) == (1, True, -signal.SIGTERM)


async def test_a_second_sigterm_ends_it_while_its_stderr_is_unread(make_stack: Any, repo_root: Path) -> None:
    """§5.2: a client is to read stderr or let ``ds mcp`` inherit its own. Behind one that does neither the
    pipe fills, the next log line blocks the loop, no call is answered and a SIGTERM's event is never set:
    the first SIGTERM leaves the signal without a handler, so the second ends the process."""
    unread, given = os.pipe()
    try:
        process = await started(repo_root, make_stack(TARGETS, mcp=MCP).settings, stderr=given)
    finally:
        os.close(given)
    answers, left = 0, []
    try:
        await answered(process, frame("initialize", HELLO, n=1))
        assert process.stdin is not None and process.stdout is not None
        with suppress(TimeoutError):  # every call leaves its line there, a refused one too
            for n in range(2, 2 + CALLS):
                process.stdin.write(frame("tools/call", {"name": "nope"}, n=n))
                await process.stdin.drain()
                await asyncio.wait_for(process.stdout.readline(), 5)
                answers += 1
        for _ in range(2):
            process.send_signal(signal.SIGTERM)
            left.append(await gone(process, 2))
    finally:
        if process.returncode is None:
            process.kill()
        os.close(unread)
        await asyncio.wait_for(process.communicate(), 30)
    assert 0 < answers < CALLS and left == [None, -signal.SIGTERM]


@pytest.mark.parametrize("era", ["auto", "legacy"])
async def test_the_library_s_client_reads_a_run_and_compares_a_key(
    serve: Any, make_stack: Any, repo_root: Path, tmp_path: Path, pg_dsn: str, era: str
) -> None:
    stack = make_stack(serve("customer").targets(), mcp=MCP)
    async with stack.context() as ctx:
        spec_id = await store_spec(ctx, pair_dir("customer") / "spec.yaml")
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
    run_id, faults = run["run_id"], []

    async def heard(message: Any) -> None:
        if isinstance(message, Exception):  # a line of the server's stdout that is no frame
            faults.append(message)

    argv = ["-c", NOISY, "mcp", "--role", "operator", "--config", str(stack.settings)]
    server = StdioServerParameters(command=sys.executable, args=argv, cwd=repo_root)
    said: list[str] = []
    with (tmp_path / "stderr").open("w+") as errlog:
        async with Client(stdio_client(server, errlog=errlog), mode=era, message_handler=heard) as client:
            offered = [tool.name for tool in (await client.list_tools()).tools]

            async def ask(name: str, **args: Any) -> Any:
                result = await client.call_tool(name, {"run_id": run_id} | args)
                said.append(result.content[0].text)  # type: ignore[union-attr]
                assert not result.is_error, said[-1]
                return json.loads(said[-1])["data"]

            summary = await ask("get_run_summary")
            findings = await ask("get_findings", limit=1)
            key = findings["findings"][0]["key_masked"]
            viewed, compared = await ask("get_observations", key=key), await ask("compare_key", key=key)
        errlog.seek(0)
        err = errlog.read()
    assert offered == list(tools.CATALOGUE) and faults == []
    assert (summary["run_id"], summary["status"]) == (run_id, "completed")
    assert summary["counters"]["different"] == run["counters"]["different"] > 0
    assert (viewed["key_masked"], viewed["cycle"], compared["key_masked"]) == (key, 1, key)
    assert compared["outcome"] == viewed["result"]["outcome"] == "MISMATCH"
    assert [sum(line == stray for line in err.splitlines()) for stray in STRAYS] == [4, 4, 4]
    published = [*said, err]
    assert [value[:3] for value in raw_pii("customer") if any(value in text for text in published)] == []
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT action, actor, request_id, detail FROM ds.audit_log WHERE detail ->> 'via' = 'mcp:stdio'"
            " ORDER BY id"
        ).fetchall()
    assert [row[0] for row in rows] == ["mcp.read", "mcp.read", "key.view", "compare_one", "run.finished"]
    assert {row[1].split(":")[0] for row in rows} == {"local"} and all(row[3]["asserted"] for row in rows)
    # Each call is a request of its own (the compare's two rows are one call's).
    assert len({row[2] for row in rows}) == 4 and all(re.fullmatch(r"C-[0-9a-f]{16}", row[2]) for row in rows)
