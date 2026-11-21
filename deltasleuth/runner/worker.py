"""The worker (``ds worker``, also inside ``ds serve``): processes the due keys of running runs.

One worker per database sweeps (it holds a Postgres advisory lock); another waits until it stops. While it
holds the lock it runs two parts: its slots (``runner.slots``), which lease, read, judge and save keys, and
its sweep (``runner.supervisor``), which each poll gives every job (``runner.jobs``) its turn: cancellations,
stop conditions, cycle ends (each run's end a task of its own), table scans, key censuses, sample draws, the
schema guard, schedules, table pre-checks, purge, the licence watch, the notifier, readiness, the sweep
record, the audit anchor and the readers of ended runs. A job's failure is logged by its type and where it
was raised, and costs that job's turn alone: it never ends the sweep or restarts the slots. The worker of
``ds worker`` or ``ds serve`` fires schedules; ``ds run``'s in-process worker fires none
(``run_worker(schedules=False)``). A slots-only worker (``ds worker --slots-only``) never takes the lock: its
slots lease, read, judge and save keys beside the sweeping worker's, and it closes the readers of ended runs
itself. Every worker that processes keys registers among them (``runner.fleet``) and holds its share of each
target's budget, so a target's limits hold for the whole installation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import psycopg
from psycopg.conninfo import make_conninfo

from deltasleuth.context import Context
from deltasleuth.runner.cycle import located
from deltasleuth.runner.fleet import Roster
from deltasleuth.runner.jobs import (
    FIRING,
    GRACE_DAYS,
    ORPHANS_EVERY_S,
    PURGE_EVERY_S,
    STALLED,
    STALLS,
    WATCH_EVERY_S,
)
from deltasleuth.runner.judges import Judges
from deltasleuth.runner.slots import (
    EVIDENCE_WRITES,
    FILE_BATCH,
    KEY_MISSING,
    LOB_BATCH,
    READ_BATCH,
    REFUSALS,
    REFUSED_WAIT_S,
    Slots,
    _LeaseLost,
)
from deltasleuth.runner.supervisor import Supervisor
from deltasleuth.store import repo

__all__ = [
    "EVIDENCE_WRITES",
    "FILE_BATCH",
    "FIRING",
    "GRACE_DAYS",
    "KEY_MISSING",
    "LOB_BATCH",
    "LOCK_ID",
    "ORPHANS_EVERY_S",
    "PURGE_EVERY_S",
    "READ_BATCH",
    "REFUSALS",
    "REFUSED_WAIT_S",
    "STALE_S",
    "STALLED",
    "STALLS",
    "WATCH_EVERY_S",
    "LockSilent",
    "_LeaseLost",
    "_Worker",
    "run_worker",
    "stale_after",
    "stalled",
]

LOCK_ID = repo.WORKER_LOCK  # the one worker of a database, or ds keys migrate
STALE_S = 30.0  # a sweep's age health tolerates at least, beside three polls: a busy store slows a sweep
log = logging.getLogger(__name__)
_STARTED: dict[int, float] = {}  # by the id of a Context: when this process started its worker (monotonic)


class LockSilent(Exception):
    """The worker lock's connection did not answer a check within the check period: the lock counts as
    lost, as a database that failed over or a half-open socket may hold it no longer."""


class _Worker:
    """The worker while it holds the lock: its slots, and the supervisor of its jobs, whose sweep runs every
    poll beside them."""

    def __init__(self, ctx: Context, schedules: bool = True, judges: Judges | None = None) -> None:
        self.ctx = ctx
        self.slots = Slots(ctx, judges)
        self.supervisor = jobs = Supervisor(ctx, self.slots, schedules)
        self.counting = self.slots.counting  # key censuses gate the leases
        # The per-run task tables, and what the schedules job keeps of each schedule, as the jobs hold them.
        self.ending, self.scans = jobs.ends.tasks, jobs.scans.tasks
        self.censuses, self.draws = jobs.censuses.tasks, jobs.draws.tasks
        self.firing, self.queued = jobs.schedules.tasks, jobs.schedules.queued
        self.waiting, self.tried = jobs.schedules.waiting, jobs.schedules.tried

    async def slot(self) -> None:
        await self.slots.slot()

    async def sweep(self) -> None:
        """Sweep every poll; a store that cannot be reached while the sweep reads is tried again a poll
        later."""
        while True:
            try:
                await self._sweep_once()
            except psycopg.Error as exc:
                log.warning("sweep failed: %s", type(exc).__name__)
            await asyncio.sleep(self.ctx.settings.worker.poll_seconds)

    async def _sweep_once(self) -> None:
        await self.supervisor.sweep()

    async def close(self) -> None:
        await self.supervisor.close()
        await self.slots.close()


async def _sole(lock: psycopg.AsyncConnection, stop: asyncio.Event, poll_s: float) -> bool:
    """Take the database's worker lock, waiting while another worker holds it; False: stopped first."""

    async def taken() -> bool:
        return await repo.try_worker_lock(lock)

    if not await taken():
        log.warning(
            "another worker is processing keys (a ds serve, ds worker or ds run on this database): it does"
            " the work, and this one takes over only if it stops"
        )
        while not await taken():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll_s)
            if stop.is_set():
                return False
    return True


def _probes(check_s: float) -> dict[str, int]:
    """The lock connection's TCP options: keepalive probes find a peer gone silent while the connection idles
    within about ``check_s``, and data left unacknowledged that long closes it. A system without one of
    them ignores it."""
    probe = max(1, int(check_s / 4))
    return {
        "keepalives": 1,
        "keepalives_idle": probe,
        "keepalives_interval": probe,
        "keepalives_count": 3,
        "tcp_user_timeout": max(1000, int(check_s * 1000)),
    }


async def run_worker(
    ctx: Context, *, stop: asyncio.Event | None = None, schedules: bool = True, slots_only: bool = False
) -> None:
    """Process keys until ``stop`` is set or the task is cancelled; unfinished leases are handed back.
    ``slots_only``: never take the lock or sweep, only process keys beside the worker that does.

    The worker lock lives on its own connection, with TCP keepalive probes. If that connection is lost (a
    database restart), or a check on it is not answered within the check period (a failover, a half-open
    socket), another worker may take the lock, so this one stops its slots at once and waits to retake it;
    a slot that meets a bug, or a sweep whose own read of the runs does, restarts the same way, logged (a
    job's bug costs that job's turn alone). Without ``schedules`` it neither
    rezones nor fires them (``ds run``'s worker, whose config and targets are one invocation's). With
    ``worker.judge_processes`` its slots' groups are judged by judge processes (``runner.judges``), which
    start as groups need them, outlive a lost lock and end as the worker stops.
    """
    stop = stop or asyncio.Event()
    poll_s = ctx.settings.worker.poll_seconds
    probes = _probes(ctx.settings.worker.lease_seconds / 3)
    processes = ctx.settings.worker.judge_processes
    judges = Judges(processes) if processes else None
    _STARTED[id(ctx)] = time.monotonic()
    try:
        while not stop.is_set():
            try:
                if slots_only:
                    await _work(ctx, None, stop, schedules, judges)
                    continue
                address = ctx.pool.conninfo
                assert isinstance(address, str)  # the store's address, as the pool was given it
                with_probes = make_conninfo(address, **probes)
                connecting = psycopg.AsyncConnection.connect(with_probes, autocommit=True)
                async with await connecting as lock:
                    if not await _sole(lock, stop, poll_s):
                        return
                    await _work(ctx, lock, stop, schedules, judges)
            except Exception as exc:  # the lock lost, or a slot or the sweep ended on a bug
                again = "the slots restart" if slots_only else "slots restart once the lock is retaken"
                log.warning("worker stopped (%s): %s", located(exc), again)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), poll_s)
    finally:
        _STARTED.pop(id(ctx), None)
        if judges is not None:
            judges.close()


def stale_after(ctx: Context) -> float:
    """The age past which the worker's last sweep counts as stale: three polls, ``STALE_S`` at least."""
    return max(3 * ctx.settings.worker.poll_seconds, STALE_S)


def stalled(ctx: Context, age_s: float | None) -> bool:
    """Whether this process runs a worker on ``ctx`` (``ds serve`` without ``--no-worker``) and the database
    has seen no sweep for longer than ``stale_after``: neither this worker's nor that of the worker
    holding the lock. ``age_s``: the last sweep's age, None before any. A worker started more recently than
    that has had no time to sweep."""
    started, limit = _STARTED.get(id(ctx)), stale_after(ctx)
    if started is None or time.monotonic() - started <= limit:
        return False
    return age_s is None or age_s > limit


async def _work(
    ctx: Context,
    lock: psycopg.AsyncConnection | None,
    stop: asyncio.Event,
    schedules: bool,
    judges: Judges | None = None,
) -> None:
    """Run the slots, and the sweep while holding the lock on ``lock`` (None: the slots alone, which close
    the readers of ended runs themselves); raise when the lock is lost or one of them ends. The process
    registers among the workers before its slots lease a key, and beats beside them."""
    roster = Roster(ctx, sweep=lock is not None)
    worker = _Worker(ctx, schedules, judges) if lock is not None else None
    slots = worker.slots if worker is not None else Slots(ctx, judges)
    tasks: list[asyncio.Task[None]] = []
    check_s = ctx.settings.worker.lease_seconds / 3  # well before any lease this worker holds expires
    try:
        await roster.beat()
        slot = worker.slot if worker is not None else slots.slot
        tasks += [asyncio.create_task(slot()) for _ in range(ctx.settings.worker.concurrency)]
        if worker is not None:
            tasks.append(asyncio.create_task(worker.sweep()))
        tasks.append(asyncio.create_task(roster.beating()))
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), check_s)
            if lock is not None:
                try:
                    await asyncio.wait_for(repo.ping(lock), check_s)
                except TimeoutError:
                    raise LockSilent(f"the lock's connection did not answer within {check_s:g} s") from None
            else:
                with contextlib.suppress(psycopg.Error):  # a store out of reach: at the next check
                    await slots.forget(set())  # the readers of runs that ended
            for task in tasks:
                if task.done():  # they loop forever: an ended one met a bug
                    task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await (worker.close() if worker is not None else slots.close())
        await roster.leave()
