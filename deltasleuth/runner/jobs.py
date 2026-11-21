"""The worker's jobs: what its sweep does each poll beside the slots, one object a job.

A job may read what is due on the sweep's connection (``look``) and then start it (``act``), and keeps its
tasks in a ``Tasks`` table, one task a key (a run, a schedule, a pre-check's pair) at most. Every task runs
under one wrapper (``guarded``): a failure goes to the job's own rule (``Job.failed``), which logs it and
pauses, fails or postpones what it must, and a postponed key keeps its place for a lease period so that no
sweep starts it again before then. A job without a rule of its own, and a rule that fails in turn, has the
failure logged by its type and where it was raised, and the task ends: no job's failure reaches the sweep or
the slots.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Coroutine, Hashable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import psycopg

from deltasleuth import licence
from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors.tables import (
    UNREACHABLE,
    KeyUnfit,
    LimitPassed,
    RowsMoved,
    SnapshotLost,
    TargetAuthRefused,
    TargetRefused,
    code,
    manifest,
)
from deltasleuth.context import Context
from deltasleuth.core.spec.errors import SpecError
from deltasleuth.runner import census, drift, notify, precheck, purge, readiness, sample, schedule
from deltasleuth.runner.cycle import due, fail_run, finish_cycle, located
from deltasleuth.runner.keys import KeyAuthRefused
from deltasleuth.runner.runs import AUTH_PAUSE, pause_run, stop_run
from deltasleuth.runner.scan import scan
from deltasleuth.runner.slots import KEY_MISSING, Slots, key_missing
from deltasleuth.runner.stop import Passed, StopConditions, StopMonitor
from deltasleuth.store import chain, repo

STALLS = (SnapshotLost, TargetRefused, KeyUnfit)  # a scan resumes after them, unless it saved no checkpoint
STALLED = 3  # scan passes in a row that saved none: the run fails
FIRING = 2  # schedules fired at once
PURGE_EVERY_S = 3600  # a purge pass at most hourly
WATCH_EVERY_S = 3600  # and a look at the licence's state
ORPHANS_EVERY_S = 7 * 86400  # with the orphan sweep weekly
GRACE_DAYS = 7  # no pass within this many days of 006: holds can follow an upgrade
log = logging.getLogger("deltasleuth.runner.worker")  # the worker's lines, whichever of its parts writes them


class Job:
    """One of the sweep's jobs. Each sweep calls ``before`` ahead of the sweep's connection, ``look`` on it
    and ``act`` once it is given back; each does nothing unless the job says otherwise."""

    name = ""  # what a log line calls the job
    unread = ""  # what a log line says when its look fails; "<name> job failed" when empty

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.tasks: Tasks[Any] = Tasks(self)
        # When the sweep began to read what is due (``time.monotonic``); None outside a sweep
        self.read_at: float | None = None

    async def before(self) -> None:
        """Work done ahead of the sweep's reads, on connections of its own."""

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        """Read what is due on the sweep's connection, inside a savepoint of its own; ``active``: the runs
        running or being cancelled. A store that cannot be reached fails the sweep; any other failure leaves
        the job with nothing due this sweep."""

    async def act(self, active: list[repo.Row]) -> None:
        """Start what is due."""

    async def failed(self, key: Any, exc: Exception) -> bool:
        """The job's rule for a failure of its task for ``key``; True: postponed, the key kept a lease
        period. Unless the job says otherwise, it is logged and the next sweep starts it again."""
        self.complain(exc, key)
        return False

    async def close(self) -> None:
        """Release what the job holds once its tasks are cancelled."""

    def complain(self, exc: BaseException, key: Any = None) -> None:
        """Log a failure no rule of the job's took, by its type and where it was raised."""
        if key is None:
            log.warning("%s job failed: %s", self.name, located(exc))
        else:
            log.warning("%s job failed for %s: %s", self.name, key, located(exc))


class Tasks[K: Hashable](dict[K, asyncio.Task[None]]):
    """A job's tasks by key, one a key, each run under ``guarded`` and dropped from the table when it ends."""

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.ended: dict[K, float] = {}  # by key: when its last task ended (``time.monotonic``)

    def start(
        self,
        key: K,
        work: Coroutine[Any, Any, object],
        around: AbstractAsyncContextManager[None] | None = None,
    ) -> None:
        """Run ``work`` as the task of ``key``, inside ``around`` when given: the key's place is held until
        it ends, a postponement's wait included. A task cancelled before it started closes ``work`` unrun.
        A key whose last task ended after the sweep read what is due is not started from that reading: it
        may be done already (a run's census recorded, its scan failed), which the next sweep reads."""
        read_at = self.job.read_at
        if read_at is not None:
            self.ended = {k: at for k, at in self.ended.items() if at >= read_at}
            if key in self.ended:
                work.close()
                return
        task = asyncio.create_task(self._ran(key, work, around or contextlib.nullcontext()))
        task.add_done_callback(lambda _: work.close())
        self[key] = task

    async def _ran(
        self, key: K, work: Coroutine[Any, Any, object], around: AbstractAsyncContextManager[None]
    ) -> None:
        try:
            async with around:
                await guarded(self.job, key, work)
        finally:
            self.pop(key, None)
            self.ended[key] = time.monotonic()


async def guarded(job: Job, key: Any, work: Coroutine[Any, Any, object]) -> None:
    """Run one task of ``job``: a failure goes to the job's rule, and a postponed key waits a lease period;
    a failure of the rule itself is logged by its type and place. Nothing but a cancellation escapes."""
    try:
        await work
    except Exception as exc:
        try:
            if await job.failed(key, exc):
                await asyncio.sleep(job.ctx.settings.worker.lease_seconds)
        except Exception as again:
            job.complain(again, key)


# -- the jobs of runs ----------------------------------------------------------------------------------


class Cancellations(Job):
    """Cancels the keys of each run being cancelled, at every sweep until its expired leases are cancelled
    too, ahead of the look for ended cycles that ends it."""

    name = "cancellations"

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        for run in active:
            if run["status"] == "cancelling":
                await repo.cancel_keys(conn, run["run_id"])


class StopFeed(Job):
    """Judges the stop conditions of the runs whose saved groups fed them to the store (``ds.stop_feed``),
    which their slots do while more than one worker processes keys: in the order the groups were saved, one
    monitor a run, as its slots judge them alone. A run that meets one is stopped, whichever worker read its
    keys. Each sweep takes the feed first, on a connection of its own, so a group is judged once; a cycle
    does not end while a group of it waits to be judged (``repo.cycle_done``)."""

    name = "stop conditions"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.monitors: dict[str, StopMonitor] = {}  # by run, while it runs
        self.met: list[tuple[str, str]] = []  # (run id, reason): the runs to stop this sweep

    async def before(self) -> None:
        async with self.ctx.pool.connection() as conn:
            fed = await repo.take_stop_feed(conn)
        for row in fed:
            monitor = self.monitors.get(row["run_id"]) or await self._monitor(row["run_id"])
            if monitor is not None:  # else the run no longer runs
                for item in row["passes"]:
                    monitor.record_pass(Passed.loaded(item))

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        running = {run["run_id"] for run in active if run["status"] == "running"}
        self.monitors = {run_id: m for run_id, m in self.monitors.items() if run_id in running}
        now = datetime.now(UTC)
        self.met = [(run_id, reason) for run_id, m in self.monitors.items() if (reason := m.reason(now))]

    async def act(self, active: list[repo.Row]) -> None:
        for run_id, reason in self.met:
            log.info("run %s stopped: %s, judged across the workers", run_id, reason)
            await stop_run(self.ctx, run_id, reason, who=WORKER)

    async def _monitor(self, run_id: str) -> StopMonitor | None:
        """A new monitor of the run, with its CRITICAL findings as the store holds them; None when it no
        longer runs."""
        async with self.ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
            if run is None or run["status"] != "running":
                return None
            conditions = StopConditions.from_spec((await self.ctx.run_spec(conn, run)).raw)
            counts = conditions.max_unexplained_critical is not None
            critical = await repo.unexplained_critical(conn, run_id) if counts else None
        monitor = self.monitors[run_id] = StopMonitor(conditions, run["started_at"], critical)
        return monitor


class CycleEnds(Job):
    """Ends the cycle of each run whose keys are all done, as a task of its own: a slow end (a rolling soak's
    key source read again, for up to its cycle interval) holds up no other run's end, no cancellation, scan
    or schedule. What it meets is logged by its type and where it was raised; only a bug or data the store
    refuses fails the run, and a store that cannot record why leaves the end to a later sweep."""

    name = "cycle ends"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.ended: list[str] = []

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        self.ended = []  # none, unless the look completes
        self.ended = [  # each one's end is a task of its own, one a run
            run_id
            for run in active
            if (run_id := run["run_id"]) not in self.tasks
            and await repo.cycle_done(conn, run_id, run["cycle"])
        ]

    async def act(self, active: list[repo.Row]) -> None:
        for run_id in self.ended:
            self.tasks.start(run_id, finish_cycle(self.ctx, run_id, who=WORKER))

    async def failed(self, run_id: str, exc: Exception) -> bool:
        if isinstance(exc, psycopg.OperationalError):  # the store is unreachable: a later sweep tries again
            log.warning("cycle end of run %s postponed: %s", run_id, located(exc))
        elif isinstance(exc, KEY_MISSING):
            await key_missing(self.ctx, run_id, exc)
        elif isinstance(exc, KeyAuthRefused):  # a rolling key table's: never tried again in a loop
            log.warning("run %s paused at its roll: %s", run_id, exc)
            await pause_run(self.ctx, run_id, reason=AUTH_PAUSE, who=WORKER)
        else:  # a bug, or data the store refuses: this run only
            log.warning("run %s cannot end its cycle: %s", run_id, located(exc))
            await fail_run(self.ctx, run_id, exc, who=WORKER)
        return False


class SchemaGuard(Job):
    """The schema guard (``runner.drift``) looks at the tables of a running key-list run once its cycle fell
    due, as a task of its own, which records what it meets: a refused login pauses the run, and only a store
    that cannot record the look leaves the cycle's start unlooked at, to be looked at again after a lease
    period. The cycle's census and draw wait for it (``holds``), two tries at most, and go on unchecked
    after them."""

    name = "schema guard"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.starts: dict[str, tuple[int, datetime]] = {}  # by run: its cycle, and when that falls due
        self.unguarded: dict[str, bool] | None = None  # by run: whether its unlooked-at cycle fell due

    def holds(self, run_id: str) -> bool:
        """Whether the run's census and draw wait for the guard: its cycle's start is not looked at, or the
        runs it is due to look at could not be read this sweep."""
        return self.unguarded is None or run_id in self.unguarded

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        """The running key-list runs whose cycle start the guard has not looked at, each with whether that
        cycle has fallen due: a soak's next cycle is opened when the one before ends. A due time that cannot
        be told counts as fallen: the look records why it was not made."""
        self.unguarded = None
        found, now = {}, datetime.now(UTC)
        for run in active:
            run_id, cycle = run["run_id"], run["cycle"]
            if run["status"] != "running" or not drift.due(run):
                continue
            if self.starts.get(run_id, (0, now))[0] != cycle:
                at = run["started_at"] or now
                with contextlib.suppress(LookupError, ValueError, SpecError):  # one that no longer compiles
                    at = at if cycle == 1 else due(await self.ctx.spec(conn, run["spec_id"]), run, cycle)
                self.starts[run_id] = (cycle, at)
            found[run_id] = self.starts[run_id][1] <= now
        self.starts = {run_id: start for run_id, start in self.starts.items() if run_id in found}
        self.unguarded = found

    async def act(self, active: list[repo.Row]) -> None:
        for run_id, fallen in (self.unguarded or {}).items():
            if fallen and run_id not in self.tasks:
                self.tasks.start(run_id, drift.cycle_start(self.ctx, run_id))

    async def failed(self, run_id: str, exc: Exception) -> bool:
        if isinstance(exc, TargetAuthRefused):  # never tried again in a loop: the account could lock
            log.warning("schema guard of run %s paused it: %s", run_id, code(exc))
            await pause_run(self.ctx, run_id, reason=AUTH_PAUSE, who=WORKER)
            return False
        log.warning("schema guard of run %s postponed: %s", run_id, located(exc))
        return True


class Censuses(Job):
    """With packs or readiness configured, a key-list run with a table side has its cycle's key census
    counted first (``runner.census``), as a task: no slot leases its keys until the census is recorded. A
    failed count is recorded, so only a store that cannot record it leaves the cycle uncounted, to be counted
    again after a lease period. A file side whose rows are not the ones its plan counted fails the run."""

    name = "key censuses"

    def __init__(self, ctx: Context, guard: SchemaGuard, counting: bool) -> None:
        super().__init__(ctx)
        self.guard, self.counting = guard, counting
        self.due: list[str] = []

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        self.due = []
        if self.counting:
            self.due = await repo.census_due(conn)

    async def act(self, active: list[repo.Row]) -> None:
        for run_id in self.due:  # a cycle's census waits for the schema guard's look at its start
            if run_id not in self.tasks and not self.guard.holds(run_id):
                self.tasks.start(run_id, census.record(self.ctx, run_id))

    async def failed(self, run_id: str, exc: Exception) -> bool:
        if isinstance(exc, RowsMoved):
            log.warning("census of run %s failed: %s", run_id, located(exc))
            await fail_run(self.ctx, run_id, exc, who=WORKER)
            return False
        log.warning("census of run %s not recorded: %s", run_id, located(exc))
        return True


class Draws(Job):
    """A sampled run's pending draw is drawn as a task of its own (``runner.sample``), once the schema guard
    looked at its cycle's start: a frame's refusal is recorded by the draw, which pauses the run, as the
    guard does when the frame met a column or a table the plan reads that is gone; an unreachable database
    draws again after a lease period; a key the keyring lacks pauses the run; any other failure fails it."""

    name = "draws"

    def __init__(self, ctx: Context, guard: SchemaGuard) -> None:
        super().__init__(ctx)
        self.guard = guard
        self.due: list[str] = []

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        self.due = []
        self.due = await repo.draws_due(conn)

    async def act(self, active: list[repo.Row]) -> None:
        for run_id in self.due:  # a cycle's draw waits for the schema guard's look at its start
            if run_id not in self.tasks and not self.guard.holds(run_id):
                self.tasks.start(run_id, sample.draw(self.ctx, run_id))

    async def failed(self, run_id: str, exc: Exception) -> bool:
        if isinstance(exc, KEY_MISSING):
            await key_missing(self.ctx, run_id, exc)
        elif isinstance(exc, (*UNREACHABLE, psycopg.OperationalError)):  # a side or the store
            log.warning("draw of run %s postponed: %s", run_id, code(exc))
            return True
        elif not (isinstance(exc, drift.DENIED) and await drift.failed(self.ctx, run_id)):
            log.warning("draw of run %s failed: %s", run_id, located(exc))
            await fail_run(self.ctx, run_id, exc, who=WORKER)
        return False


class Scans(Job):
    """Runs each due table scan (``runner.scan``, a soak's next one once its cycle starts) as a task of its
    own, cancelled once its run no longer runs, so its statements end then and not at its next checkpoint.
    An unreachable database pauses a scan for a lease period; a refused login pauses its run; a key the
    keyring lacks pauses it; a server's code a resume may not meet again resumes it, a lease period later
    when the pass saved no checkpoint, until three passes in a row saved none (a read error of fixed files
    that did not change counts with or without a checkpoint); a pass the schema guard paused ends without
    failing its run; any other failure fails its run, unless the guard finds that a column or a table the
    plan reads is gone, which pauses it."""

    name = "scans"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.stalled: dict[str, int] = {}  # by run: its scan's stalled passes in a row
        self.started: dict[str, int] = {}  # by run with a pass under way: the checkpoint it started from

    async def act(self, active: list[repo.Row]) -> None:
        running = {run["run_id"] for run in active if run["status"] == "running"}
        for run_id, scanning in self.tasks.items():
            if run_id not in running:  # paused, cancelled or stopped: its statements end now, not when
                scanning.cancel()  # its next checkpoint would have told it
        now = datetime.now(UTC)
        for run in active:
            state = run["scan"] if run["status"] == "running" else None
            fallen = (
                state is not None
                and not state["done"]
                and datetime.fromisoformat(state.get("due") or now.isoformat()) <= now
            )
            if fallen and state is not None and run["run_id"] not in self.tasks:
                self.started[run["run_id"]] = state["checkpoint"]
                self.tasks.start(run["run_id"], self._scan(run["run_id"]))
        live = {run["run_id"] for run in active}
        self.stalled = {run_id: passes for run_id, passes in self.stalled.items() if run_id in live}
        self.started = {run_id: start for run_id, start in self.started.items() if run_id in self.tasks}

    async def _scan(self, run_id: str) -> None:
        """Scan the run on from the last checkpoint its scan saved, unless it stopped since the sweep read
        it (a pass that failed meanwhile has failed it): a pass of a run that ended opens nothing."""
        async with self.ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
        if run is None or run["status"] != "running":
            return
        if reason := await scan(self.ctx, run_id):  # until or maxDuration: the run stops
            await stop_run(self.ctx, run_id, reason, who=WORKER)
        self.stalled.pop(run_id, None)

    async def failed(self, run_id: str, exc: Exception) -> bool:
        if isinstance(exc, drift.Tripped):  # the schema guard paused the run before a cursor opened, and
            self.stalled.pop(run_id, None)  # logged it
        elif isinstance(exc, KEY_MISSING):
            await key_missing(self.ctx, run_id, exc)
        elif isinstance(exc, TargetAuthRefused):  # never tried again in a loop: the account could lock
            log.warning("scan of run %s paused: %s", run_id, code(exc))
            await pause_run(self.ctx, run_id, reason=AUTH_PAUSE, who=WORKER)
        else:
            # A database unreachable, a side's or the store: a later sweep resumes it, as it does after a
            # server's code a resume may not meet again, until three passes in a row saved no checkpoint;
            # such a pass waits a lease period too, so the three span minutes. Data past a server limit
            # would fail every resume, so it fails the run, as a bug, an unreadable DSN, a privilege or a
            # scan error do: unless the schema guard finds that a column or a table the plan reads is gone.
            limit = isinstance(exc, LimitPassed | psycopg.errors.ProgramLimitExceeded)
            if isinstance(exc, (*UNREACHABLE, psycopg.OperationalError)) and not limit:
                log.warning("scan of run %s paused: %s", run_id, code(exc))
                return True
            if isinstance(exc, STALLS) and not await self._stalled(run_id, getattr(exc, "fixed", False)):
                log.warning("scan of run %s resumes: %s", run_id, code(exc))
                return bool(self.stalled[run_id])
            if not (isinstance(exc, drift.DENIED) and await drift.failed(self.ctx, run_id)):
                log.warning("scan of run %s failed: %s", run_id, located(exc))
                await fail_run(self.ctx, run_id, exc, who=WORKER)
        return False

    async def _stalled(self, run_id: str, fixed: bool = False) -> bool:
        """Whether the scan's pass was the third in a row to save no checkpoint past the one it started from:
        a code every resume would meet again cannot loop for ever. A read error of ``fixed`` files that did
        not change counts whether or not its pass saved one: the same bytes fail the next pass the same
        way."""
        checkpoint = self.started[run_id]
        async with self.ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
        saved = run is not None and run["scan"] is not None and run["scan"]["checkpoint"] > checkpoint
        self.stalled[run_id] = 0 if saved and not fixed else self.stalled.get(run_id, 0) + 1
        return self.stalled[run_id] >= STALLED


# -- the jobs of the worker's process -------------------------------------------------------------------


class Schedules(Job):
    """Fires schedules (``runner.schedule``), never as ``ds run``'s worker: its first sweep recomputes each
    active schedule's next due time, and every sweep starts a task for each due schedule, at most ``FIRING``
    at once and never two for one schedule. A firing that waits, or meets a fault, is not tried again before
    its time: nothing loops at the poll rate, and it never fails a run. The attempts counted are those of
    the schedule's next due time as the sweep read it: the sweep forgets another time's."""

    name, unread = "schedules", "due schedules not read"

    def __init__(self, ctx: Context, fires: bool) -> None:
        super().__init__(ctx)
        self.fires = fires
        self.rezoned = False  # each active schedule's next due time was recomputed since the lock was taken
        self.due: list[repo.Row] | None = []  # None: not read this sweep
        self.waiting: dict[str, float] = {}  # by schedule: monotonic, no firing before it
        self.queued: dict[str, schedule.Queued] = {}  # by schedule: the due time left without a slot, when
        self.tried: dict[str, tuple[datetime, int]] = {}  # by schedule: a due time, the tries to start it

    async def before(self) -> None:
        if self.fires and not self.rezoned:
            self.rezoned = await schedule.rezone(self.ctx)  # never raises

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        self.due = None
        self.due = await repo.schedules_due(conn) if self.fires else []

    async def act(self, active: list[repo.Row]) -> None:
        if self.due is not None:  # what is kept is a due time's own; a list not read forgets nothing
            due = {row["schedule_id"]: row["next_due"] for row in self.due}
            for kept in (self.queued, self.tried):
                for schedule_id in [s for s, was in kept.items() if s not in due or due[s] != was[0]]:
                    del kept[schedule_id]
        for row in self.due or ():
            schedule_id = row["schedule_id"]
            if schedule_id in self.tasks or self.waiting.get(schedule_id, 0) > time.monotonic():
                continue
            if len(self.tasks) >= FIRING:  # left without a slot: its window may close meanwhile
                self.queued.setdefault(schedule_id, (row["next_due"], row["now"]))
                continue
            queued = self.queued.pop(schedule_id, None)
            self.tasks.start(schedule_id, self._fire(schedule_id, queued))

    async def _fire(self, schedule_id: str, queued: schedule.Queued | None) -> None:
        """Fire the schedule's due time, or settle why not (``schedule.fire``)."""
        _, tried = self.tried.get(schedule_id, (None, 0))
        retry = await schedule.fire(self.ctx, schedule_id, queued=queued, tried=tried)
        if retry is None:
            self.waiting.pop(schedule_id, None)
            self.tried.pop(schedule_id, None)
        else:
            self.waiting[schedule_id] = time.monotonic() + retry.seconds
            self.tried[schedule_id] = (retry.due, retry.tried)

    async def failed(self, schedule_id: str, exc: Exception) -> bool:
        log.warning("schedule %s not fired: %s", schedule_id, located(exc))
        self.waiting[schedule_id] = time.monotonic() + schedule.RETRY_SLOW_S  # its count stands
        return False


class Prechecks(Job):
    """Starts a task for each pending pair of a table pre-check (``runner.precheck``) whose targets each have
    a pre-check session free, at most ``precheck.sessions_per_target`` a target, sets back to pending the
    pairs a worker that is gone was reading, and cancels the tasks of a cancelled pre-check, whose statements
    end on their servers. A task renews its pair's claim while it reads it; whatever escapes (the store
    unreachable, a bug) is logged and the pair read again a lease period later. A pre-check is no run: a
    fault of one fails no run."""

    name, unread = "table pre-checks", "due table pre-checks not read"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.due: list[repo.Row] | None = None  # None: not read this sweep
        self.reading: Counter[str] = Counter()  # by target: the pre-check sessions held

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        """The pairs to read or being read. One that is running without a task of this worker is set back
        to pending first: the worker that read it is gone (on the first sweep after the lock was taken,
        every running one), or its task ended without settling it."""
        self.due = None
        due = await repo.prechecks_due(conn)
        lost = [key for row in due if row["status"] == "running" and (key := _pair(row)) not in self.tasks]
        if lost and await repo.requeue_prechecks(conn, lost):
            due = await repo.prechecks_due(conn)
        self.due = due

    async def act(self, active: list[repo.Row]) -> None:
        """Start a task for each pending pair whose targets each have a session free, holding one a target,
        and cancel the tasks whose pair is no longer due: its pre-check was cancelled, and its statements end
        now."""
        if self.due is None:
            return
        listed = {_pair(row) for row in self.due}
        for key, checking in self.tasks.items():
            if key not in listed:
                checking.cancel()
        most = self.ctx.settings.precheck.sessions_per_target
        for row in self.due:
            key, targets = _pair(row), {row["target_a"], row["target_b"]}
            busy = any(self.reading[target] >= most for target in targets)
            if row["status"] == "pending" and key not in self.tasks and not busy:
                self.reading.update(targets)
                self.tasks.start(key, self._check(key), around=self._holding(key, targets))

    async def _check(self, key: tuple[str, int]) -> None:
        """Read one pair and settle it. Cancelled (the worker stops, or the pre-check was cancelled), it sets
        its pair back to pending if it is running: the next worker reads it, and no pair stays running
        without a reader."""
        try:
            await precheck.check(self.ctx, *key)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                async with self.ctx.pool.connection() as conn:
                    await repo.requeue_prechecks(conn, [key])
            raise

    @contextlib.asynccontextmanager
    async def _holding(self, key: tuple[str, int], targets: set[str]) -> AsyncIterator[None]:
        """The pair's claim renewed, and a session of each of its targets held, while its task lasts."""
        holding = asyncio.create_task(self._hold(key))
        try:
            yield
        finally:
            holding.cancel()
            self.reading.subtract(targets)

    async def _hold(self, key: tuple[str, int]) -> None:
        """Renew the pair's claim while it is read, as a slot renews a lease: a running pair whose claim is
        older than ``prechecks.HELD`` lease periods is read by no worker."""
        while True:
            await asyncio.sleep(self.ctx.settings.worker.lease_seconds / 3)
            with contextlib.suppress(psycopg.Error):
                async with self.ctx.pool.connection() as conn:
                    await repo.hold_pair(conn, *key)

    async def failed(self, key: tuple[str, int], exc: Exception) -> bool:
        log.warning("table pre-check %s pair %s not settled: %s", *key, located(exc))
        return True


class SweepRecord(Job):
    """Records the sweep's holder and time once it has read what is due: what health shows of the
    worker."""

    name = "sweep record"

    def __init__(self, ctx: Context, holder: str) -> None:
        super().__init__(ctx)
        self.holder = holder

    async def look(self, conn: repo.Conn, active: list[repo.Row]) -> None:
        await repo.mark_sweep(conn, self.holder)


class AuditAnchor(Job):
    """Writes the audit chain's head outside the database (``chain.anchored``), which logs what it meets."""

    name = "audit anchor"

    async def act(self, active: list[repo.Row]) -> None:
        await chain.anchored(self.ctx)


class LicenceWatch(Job):
    """Audits a change of the licence's state at most hourly (``licence.watch``, which never raises)."""

    name = "licence watch"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.watched_at = -float("inf")  # monotonic: the last look

    async def act(self, active: list[repo.Row]) -> None:
        if time.monotonic() - self.watched_at >= WATCH_EVERY_S:
            self.watched_at = time.monotonic()
            await licence.watch(self.ctx)


class Purge(Job):
    """Starts a purge pass at most hourly (``runner.purge``, as ``system:worker``, weekly with the orphan
    sweep), after a sweep of the files reader's stale spill directories, grace or not; none within 7 days of
    migration 006, so that holds can follow an upgrade. A pass another purger holds up is skipped; any other
    failure waits for the next hour, when an interrupted cycle resumes first."""

    name = "purge"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.purged_at = self.swept_at = -float("inf")  # monotonic: the last pass and orphan sweep

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The pass under way, if any."""
        return self.tasks.get(None)

    async def act(self, active: list[repo.Row]) -> None:
        if not self.tasks and time.monotonic() - self.purged_at >= PURGE_EVERY_S:
            self.purged_at = time.monotonic()
            self.tasks.start(None, self._pass())

    async def _pass(self) -> None:
        try:
            await manifest.call(manifest.sweep, self.ctx.settings.spill_dir)
        except Exception as exc:  # the next hour tries again
            log.warning("spill not swept: %s", located(exc))
        async with self.ctx.pool.connection() as conn:
            if not await repo.grace_over(conn, GRACE_DAYS):
                return
        orphans = time.monotonic() - self.swept_at >= ORPHANS_EVERY_S
        await purge.purge(self.ctx, WORKER, orphans=orphans)
        if orphans:
            self.swept_at = time.monotonic()

    async def failed(self, key: None, exc: Exception) -> bool:
        if isinstance(exc, purge.PurgeBusy):
            log.info("purge skipped this hour: %s", purge.BUSY)
        else:
            log.warning("purge pass failed: %s", located(exc))
        return False


class Notifications(Job):
    """Starts the notifier (``runner.notify``) as a task of its own when none runs, awaiting nothing: it
    feeds the audit chain into deliveries and sends them on threads of its own with at most two pooled
    connections, each of its steps logging what it meets and going on, so no notification fault keeps a cycle
    from ending. One that stopped on a fault is logged and started again; with ``notify`` absent it only
    records that, once. Its close waits a bounded time: it never holds up a stop."""

    name = "notifier"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.notifier = notify.Notifier(ctx)
        self.task: asyncio.Task[None] | None = None

    async def act(self, active: list[repo.Row]) -> None:
        ended = self.task
        if ended is not None and ended.done() and not ended.cancelled() and (exc := ended.exception()):
            log.warning("notifier stopped: %s", located(exc))
        if ended is None or (ended.done() and self.ctx.settings.notify is not None):
            self.task = asyncio.create_task(self.notifier.run())

    async def close(self) -> None:
        await self.notifier.close(self.task)


class Readiness(Job):
    """Starts a readiness pass (``runner.readiness``) as a task of its own when none runs: it measures the
    cycles that ended, chains each pair's change of readiness, logs what it meets and never fails a run."""

    name = "readiness"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.state = readiness.State()

    async def act(self, active: list[repo.Row]) -> None:
        if not self.tasks:
            self.tasks.start(None, readiness.watch(self.ctx, self.state))


class Readers(Job):
    """Closes the readers the slots hold of runs that ended."""

    name = "readers"

    def __init__(self, ctx: Context, slots: Slots) -> None:
        super().__init__(ctx)
        self.slots = slots

    async def act(self, active: list[repo.Row]) -> None:
        await self.slots.forget({run["run_id"] for run in active})


def _pair(row: repo.Row) -> tuple[str, int]:
    """A pre-check's pair as the job's tasks are keyed."""
    return row["precheck_id"], row["n"]
