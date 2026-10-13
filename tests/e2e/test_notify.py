"""Alerts and notifications end to end: a three-cycle soak on the customer mock pair
notifies its first cycle's gaps once, signed, with the cycle's own counts, and nothing for gaps that do not
change; hanging and failing channels leave the run as it is without notifications; a worker killed mid-send
delivers at most twice under one webhook-id; and the audit chain still verifies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.context import Context
from deltasleuth.core.notify import COUNTERS
from deltasleuth.core.spec import load_spec_file
from deltasleuth.runner import create_run, run_worker
from deltasleuth.store import repo
from mocks.notify import SmtpSink
from mocks.registry import pair_dir
from tests.conftest import TESTER, receiving_thread

from .conftest import finish, store_spec

pytestmark = [pytest.mark.e2e, pytest.mark.db]
DS = "from deltasleuth.cli.main import app; app()"


def soak(cycles: int) -> dict[str, Any]:
    spec = load_spec_file(pair_dir("customer") / "spec.yaml")
    spec["execution"] |= {"cycleInterval": "0s", "stop": {"maxCycles": cycles}}
    return spec


def hook(base: str, ref: str) -> dict[str, Any]:
    return {
        "kind": "webhook",
        "url": f"{base}/webhook",
        "signing_key_refs": [ref],
        "allow_private_network": True,
    }


async def worked(ctx: Context, done: Callable[[], Awaitable[bool]], timeout: float = 60) -> None:
    """Run a worker, and so its notifier, until ``done()``."""
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(ctx, stop=stop))
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not await done():
            assert asyncio.get_running_loop().time() < deadline, "not notified in time"
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await worker


async def deliveries(ctx: Context) -> list[repo.Row]:
    async with ctx.pool.connection() as conn:
        return await repo._rows(conn, "SELECT * FROM ds.notify_deliveries ORDER BY delivery_id")


async def settled(ctx: Context) -> bool:
    """Every chain row fed, and no delivery pending."""
    async with ctx.pool.connection() as conn:
        [row] = await repo._rows(
            conn,
            """SELECT (SELECT seq FROM ds.notify_cursor) = (SELECT max(seq) FROM ds.audit_chain) AS fed,
                      NOT EXISTS (SELECT 1 FROM ds.notify_deliveries WHERE state = 'pending') AS sent""",
        )
    return bool(row["fed"] and row["sent"])


async def verified(settings: Path, repo_root: Path) -> int:
    """``ds audit verify``'s exit code."""
    verify = await asyncio.create_subprocess_exec(
        sys.executable, "-c", DS, "audit", "verify", "--config", str(settings), cwd=repo_root,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    return await verify.wait()


@pytest.mark.usefixtures("loopback")
async def test_a_soak_notifies_its_first_cycle_s_gaps_once_signed_with_its_counts(
    serve: Any, make_stack: Any, tmp_path: Path, repo_root: Path
) -> None:
    gaps = {"name": "new-gaps", "events": ["cycle.ended"], "channels": ["owners-hook"],
            "when": {"new_root_causes_at_least": 1}}  # fmt: skip
    ends = {"name": "ends", "events": ["run.finished"], "channels": ["owners-hook"]}
    with receiving_thread(tmp_path) as (receiver, base, ref):
        notify = {"label": "e2e", "channels": {"owners-hook": hook(base, ref)}, "rules": [gaps, ends]}
        stack = make_stack(serve("customer").targets(), notify=notify)
        async with stack.context() as ctx:
            run_id = await create_run(ctx, await store_spec(ctx, soak(3)), "soak", who=TESTER)

            async def ended() -> bool:  # one worker throughout: none is stopped mid-send, which re-sends
                async with ctx.pool.connection() as conn:
                    status = (await repo.get_run(conn, run_id))["status"]  # type: ignore[index]
                return status in repo.TERMINAL and await settled(ctx)

            await worked(ctx, ended, timeout=120)
            async with ctx.pool.connection() as conn:
                run = await repo.get_run(conn, run_id)
                first = (await repo.get_cycle(conn, run_id, 1))["counters"]  # type: ignore[index]
            stored = await deliveries(ctx)
        received = list(receiver.received)
        audit = await verified(stack.settings, repo_root)
    docs = [json.loads(r.body) for r in received]
    assert (run["status"], run["cycle"]) == ("completed", 3)
    # Cycles 2 and 3 observed the same root causes as cycle 1: nothing new, nothing sent.
    assert [(d["event"], d["facts"]["cycle"]) for d in docs] == [("cycle.ended", 1), ("run.finished", 3)]
    assert [(row["event"], row["state"]) for row in stored] == [
        ("cycle.ended", "sent"),
        ("run.finished", "sent"),
    ]
    ended = docs[0]
    assert ended["facts"]["counters"] == {name: first[name] for name in COUNTERS}
    assert ended["facts"]["new_root_causes"] >= 1 and ended["rules"] == ["new-gaps"]
    assert "observed in the first cycle" in ended["text"] and ended["run"]["run_id"] == run_id
    assert all(r.verified for r in received)
    by_id = {row["delivery_id"]: row for row in stored}
    assert [by_id[r.headers["webhook-id"]]["body_sha256"] for r in received] == [
        hashlib.sha256(r.body).hexdigest() for r in received
    ]
    assert audit == 0


async def _timed(ctx: Context, spec: dict[str, Any]) -> tuple[str, list[dict[str, Any]], float]:
    """A soak run to its end: its status, each cycle's counters, and how long it took."""
    started = time.monotonic()
    run_id = await create_run(ctx, await store_spec(ctx, spec), "soak", who=TESTER)
    run = await finish(ctx, run_id)
    async with ctx.pool.connection() as conn:
        counters = [(await repo.get_cycle(conn, run_id, c))["counters"] for c in range(1, run["cycle"] + 1)]  # type: ignore[index]
    return run["status"], counters, time.monotonic() - started


def _rules(channels: list[str]) -> list[dict[str, Any]]:
    """Every cycle's end and the run's to every channel, five a rule."""
    events = ["cycle.ended", "run.finished"]
    return [
        {"name": f"all-{n}", "events": events, "channels": channels[n : n + 5]}
        for n in range(0, len(channels), 5)
    ]


@pytest.mark.usefixtures("loopback")
async def test_hanging_and_failing_channels_leave_a_run_as_it_is_without_notifications(
    serve: Any, make_stack: Any, tmp_path: Path
) -> None:
    served = serve("customer")
    async with make_stack(served.targets()).context() as ctx:
        baseline = await _timed(ctx, soak(3))
    sink = SmtpSink(hang=True)
    port = await sink.start()
    relay = {"kind": "email", "smtp_host": "127.0.0.1", "smtp_port": port, "tls": "none",
             "allow_private_network": True, "sender": "ds@test.example",
             "recipients": ["owners@test.example"]}  # fmt: skip
    (tmp_path / "hang").mkdir()
    (tmp_path / "fail").mkdir()
    runs, tried = [], []
    try:
        with (
            receiving_thread(tmp_path / "hang") as (hanging, hang_base, hang_ref),
            receiving_thread(tmp_path / "fail") as (failing, fail_base, fail_ref),
        ):
            hanging.hangs, failing.failures = 1000, [(503, None)] * 1000
            hooks = {"hanging": hook(hang_base, hang_ref), "failing": hook(fail_base, fail_ref)}
            relays = {f"relay-{i}": relay for i in range(20)}  # the most channels a notify block holds
            for channels in (hooks, relays):
                notify = {
                    "label": "e2e",
                    "timeout_seconds": 2,
                    "channels": channels,
                    "rules": _rules(list(channels)),
                }
                async with make_stack(served.targets(), notify=notify).context() as ctx:
                    runs.append(await _timed(ctx, soak(3)))
                    tried.append(sum(1 for row in await deliveries(ctx) if row["attempts"]))
            answered = len(hanging.received) + len(failing.received)
    finally:
        await sink.close()
    for notified in runs:
        assert notified[:2] == baseline[:2] and notified[0] == "completed" and len(notified[1]) == 3
        assert notified[2] < baseline[2] * 2 + 10  # the run's pace is its own
    assert all(tried) and answered  # the channels were being tried meanwhile


async def test_a_worker_killed_mid_send_delivers_at_most_twice_under_one_webhook_id(
    serve: Any, make_stack: Any, tmp_path: Path, repo_root: Path
) -> None:
    ends = {"name": "ends", "events": ["run.finished"], "channels": ["ops-hook"]}
    with receiving_thread(tmp_path) as (receiver, base, ref):
        receiver.hangs = 1  # the first send never answers
        notify = {
            "label": "e2e",
            "timeout_seconds": 1,
            "channels": {"ops-hook": hook(base, ref)},
            "rules": [ends],
        }
        stack = make_stack(serve("customer").targets(), notify=notify)
        async with stack.context() as ctx:
            spec_id = await store_spec(ctx, pair_dir("customer") / "spec.yaml")
            run_id = await create_run(ctx, spec_id, "batch", who=TESTER)
            worker = await asyncio.create_subprocess_exec(
                sys.executable, "-c", DS, "worker", "--config", str(stack.settings), cwd=repo_root,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            deadline = asyncio.get_running_loop().time() + 60
            while not receiver.received:  # the run ended and its notification is being sent
                assert worker.returncode is None, "the worker exited"
                assert asyncio.get_running_loop().time() < deadline, "nothing was sent"
                await asyncio.sleep(0.02)
            worker.kill()  # kill -9, mid-send
            await worker.wait()

            async def sent() -> bool:
                return [row["state"] for row in await deliveries(ctx)] == ["sent"]

            await worked(ctx, sent)  # its lease ends: another worker sends it again
            [row] = await deliveries(ctx)
        received = list(receiver.received)
    assert len(received) == 2 and {r.headers["webhook-id"] for r in received} == {row["delivery_id"]}
    assert received[0].body == received[1].body and all(r.verified for r in received)
    assert (row["event"], row["run_id"], row["attempts"]) == ("run.finished", run_id, 2)
