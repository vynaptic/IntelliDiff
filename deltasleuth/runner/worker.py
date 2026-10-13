"""The worker (``ds worker``, also inside ``ds serve``): processes the due keys of running runs.

One worker per database processes keys (it holds a Postgres advisory lock); another waits until it
stops, so a target's rate budget holds across processes. Each of ``worker.concurrency`` slots
leases a group of keys of one run, under a lease owner of its own so the group can be handed back
alone: ``READ_BATCH`` keys of a run whose sides are all tables, read with one statement a side
(TABLES.md §12), or one of whose sides reads fixed files, a pass over them a group, its service side read
key by key in the same slot (docs/mvp/FILES.md §12), else one key; ``LOB_BATCH`` keys where a table side's
rows may be large, but beside fixed files, whose group stays whole: one pass over the files, then the other
side read, judged and saved ``LOB_BATCH`` keys at a time. It renews the leases while the keys wait
and are read: A then B, or B then A on a recheck pass, which ``confirm`` judges against the stored first
read; a recheck reads no side of fixed files again, whose first observation stands. The group's
observations, results, masked findings and next states (done, or a recheck due after
``recheck.delay`` + jitter) are saved in one transaction; a lost lease rolls it back, so a kill -9
costs at most the keys in flight and never duplicates a result.

Politeness: one gate per target for the process (``ctx.gates``: rate budget, in-flight cap, and a
breaker for each ``execution.circuitBreaker`` its specs declare, so a run is governed by its own spec's),
at most ``execution.concurrency`` groups of a live run in flight, each pausing
``execution.delay`` plus jitter (from a hash of a key) before its next group; no slot leases keys
of a run with none of those free, and keys are read only if their run still runs. A live 401/403
pauses the run and hands the group back unjudged, to be read again once resumed, with the
credentials read again; so does a key the keyring lacks (the run's data key, or an evidence key:
docs/mvp/RETENTION.md §6.1); a stop condition stops the run. A sweeper ends cycles, drives
cancellations, closes the readers of finished runs and runs each due table scan (``runner.scan``,
a soak's next one once its cycle starts) as a task of its own, cancelled once its run no longer runs:
an unreachable database pauses a scan for a lease period, a refused login pauses its run (as it does a
rolling soak whose key table's target refuses it at a roll, until a resume reads it again), a server's code
a resume may not meet again resumes it, a lease period later when the pass saved no checkpoint, until
three passes in a row saved none (docs/mvp/ENGINES.md §11; a read error of fixed files that did not change
counts with or without a checkpoint: docs/mvp/FILES.md §10), any other failure fails its run. With packs
or readiness configured, a key-list
run with a table side has its cycle's key census counted first (``runner.census``, docs/mvp/PACK.md §1.3),
also as a task: no slot leases its keys until the census is recorded. A sampled run's pending draw is drawn
as a task of its own too (``runner.sample``, docs/mvp/SAMPLING.md §5): a frame's refusal pauses the run, an
unreachable database draws again after a lease period, any other failure fails the run; and a sign-off
member no read compared is read again from its first pass, at most ``resolve.times``, instead of being
done (§7). It also starts a purge pass at most hourly
(``runner.purge``, weekly with the orphan sweep, after a sweep of the files reader's stale spill
directories: docs/mvp/FILES.md §10), none within 7 days of migration 006, so that holds
can follow an upgrade (docs/mvp/RETENTION.md §5.1), and audits a change of the licence's state at most
hourly (``licence.watch``, docs/mvp/LICENCE.md §11). After ending cycles, the sweep starts the notifier
(``runner.notify``, docs/mvp/ALERTS.md §9) as a task of its own when none runs, awaiting nothing: it feeds
the audit chain into deliveries and sends them on threads of its own with at most two pooled connections,
so no notification fault keeps a cycle from ending; with ``notify`` absent it only records that. It then
starts a readiness pass (``runner.readiness``, docs/mvp/READINESS.md §4, §8) as a task of its own when none
runs, which measures the cycles that ended, chains each pair's change of readiness and never fails a run.
The worker of ``ds worker`` or ``ds serve`` also fires schedules (``runner.schedule``,
docs/mvp/SCHEDULES.md §5): its first sweep recomputes each active schedule's next due time, and every sweep
starts a task for each due schedule, at most ``FIRING`` at once and never two for one schedule; a firing
that waits, or meets a fault, is not tried again before its time, and never fails a run or ends the sweep.
``ds run``'s in-process worker fires none (``run_worker(schedules=False)``). Each sweep also starts a task
for each pending pair of a table pre-check (``runner.precheck``, docs/mvp/PRECHECKS.md §5) whose targets
each have a pre-check session free, at most ``precheck.sessions_per_target`` a target, sets back to pending
the pairs a worker that is gone was reading, and cancels the tasks of a cancelled pre-check, whose
statements end on their servers. A task renews its pair's claim while it reads it, and a worker that stops
sets the pairs it was reading back to pending. A pre-check is no run: a fault of one fails no run and never
ends the sweep.
The schema guard (``runner.drift``, docs/mvp/PRECHECKS.md §7) looks at the tables of a running key-list run
once its cycle fell due, as a task of its own (V3): the cycle's census and draw wait for it, two tries at
most, and go on unchecked after them. A scan pass the guard paused ends without failing its run (V2); and a
scan, a read or a draw's frame that failed on a column or a table has the guard look before the run is
failed (V5): a change that breaks the run's plan pauses it instead, its keys handed back.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import secrets
import socket
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from deltasleuth import licence
from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors.tables import (
    UNREACHABLE,
    KeyUnfit,
    RowsMoved,
    SnapshotLost,
    TargetAuthRefused,
    TargetRefused,
    code,
    manifest,
    unsealed,
)
from deltasleuth.context import Capabilities, Context
from deltasleuth.core.spec.errors import SpecError
from deltasleuth.core.types import CompiledSpec, Confirmation, KeyResult, Outcome, Side, SideStateKind
from deltasleuth.evidence.store import EvidenceKeyError
from deltasleuth.runner import census, drift, notify, precheck, purge, readiness, sample, schedule
from deltasleuth.runner.compare import (
    FileReader,
    LiveReader,
    Reader,
    evaluate_many,
    rederive_all,
    save_passes,
)
from deltasleuth.runner.cycle import due, fail_run, failure, finish_cycle
from deltasleuth.runner.keys import KeyAuthRefused
from deltasleuth.runner.recheck import RecheckPolicy, confirm, freeze
from deltasleuth.runner.runs import AUTH_PAUSE, KEY_PAUSE, pause_run, run_status, stop_run
from deltasleuth.runner.scan import scan
from deltasleuth.runner.stop import StopConditions, StopMonitor
from deltasleuth.security.crypto import KeyringError, Sealer
from deltasleuth.store import repo

LOCK_ID = 0x44535752  # pg advisory lock "DSWR": the one worker of a database, or ds keys migrate
READ_BATCH = 200  # keys a slot reads at once from a run that reads in groups (``LiveReader.batches``)
LOB_BATCH = 4  # of a run whose rows may hold large values: 16 MiB of them a row (ENGINES.md §6)
KEY_MISSING = (KeyringError, EvidenceKeyError)  # a KEK the keyring lacks: the run waits for it
STALLS = (SnapshotLost, TargetRefused, KeyUnfit)  # a scan resumes after them, unless it saved no checkpoint
STALLED = 3  # scan passes in a row that saved none: the run fails
FIRING = 2  # schedules fired at once (docs/mvp/SCHEDULES.md §5)
PURGE_EVERY_S = 3600  # a purge pass at most hourly
WATCH_EVERY_S = 3600  # and a look at the licence's state
ORPHANS_EVERY_S = 7 * 86400  # with the orphan sweep weekly
GRACE_DAYS = 7  # no pass within this many days of 006: holds can follow an upgrade
log = logging.getLogger(__name__)


class _LeaseLost(Exception):
    """Another worker holds the key now: this pass is rolled back."""


def key_fraction(key_masked: str) -> float:
    """A stable number in [0, 1) from the masked key (keyed by the tokeniser, so a stored due time
    reveals nothing of the raw key): jitter spreads keys without randomness."""
    return int.from_bytes(hashlib.sha256(key_masked.encode()).digest()[:8], "big") / 2**64


@dataclass
class _Run:
    spec: CompiledSpec
    caps: Capabilities
    reader: Reader
    live: bool
    policy: RecheckPolicy
    monitor: StopMonitor
    lanes: asyncio.Semaphore | None  # live runs: execution.concurrency groups of keys in flight
    scanned: bool = False  # its keys come from a table scan, which read each of them once already
    halted: bool = False  # this process paused or stopped the run


class _Worker:
    def __init__(self, ctx: Context, schedules: bool = True) -> None:
        self.ctx, self.settings = ctx, ctx.settings.worker
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self.runs: dict[str, _Run] = {}
        self.scans: dict[str, asyncio.Task[None]] = {}
        self.stalled: dict[str, int] = {}  # by run: its scan's stalled passes in a row
        self.censuses: dict[str, asyncio.Task[None]] = {}
        self.draws: dict[str, asyncio.Task[None]] = {}
        # key censuses gate the leases (PACK.md §1.3; READINESS.md §4)
        self.counting = ctx.settings.packs is not None or ctx.settings.readiness is not None
        self.purging: asyncio.Task[None] | None = None
        self.purged_at = self.swept_at = -float("inf")  # monotonic: the last pass and orphan sweep
        self.watched_at = -float("inf")  # monotonic: the last licence.watch
        self.notifier = notify.Notifier(ctx)
        self.notifying: asyncio.Task[None] | None = None
        self.readiness = readiness.State()
        self.measuring: asyncio.Task[None] | None = None
        self.schedules = schedules  # it fires schedules: never ds run's worker (docs/mvp/SCHEDULES.md §5)
        self.rezoned = False  # each active schedule's next due time was recomputed since the lock was taken
        self.firing: dict[str, asyncio.Task[None]] = {}
        self.waiting: dict[str, float] = {}  # by schedule: monotonic, no firing before it
        self.queued: dict[str, schedule.Queued] = {}  # by schedule: the due time left without a slot, when
        self.tried: dict[str, tuple[datetime, int]] = {}  # by schedule: a due time, the tries to start it
        self.guards: dict[str, asyncio.Task[None]] = {}  # by run: the schema guard's look at a cycle's start
        self.starts: dict[str, tuple[int, datetime]] = {}  # by run: its cycle, and when that falls due
        self.checks: dict[tuple[str, int], asyncio.Task[None]] = {}  # by pre-check and pair
        self.reading: Counter[str] = Counter()  # by target: the pre-check sessions held

    async def slot(self) -> None:
        while True:
            owner = f"{self.name}:{secrets.token_hex(4)}"
            full = [run_id for run_id, run in self.runs.items() if run.lanes and run.lanes.locked()]
            try:
                async with self.ctx.pool.connection() as conn:
                    leased = await repo.lease_due(
                        conn, owner, 1, self.settings.lease_seconds, skip=full, census=self.counting
                    )
                if leased:
                    await self._handle(leased, owner)
                    continue
            except psycopg.Error as exc:  # the store is unreachable: try again after a pause
                log.warning("worker slot: %s", type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

    async def sweep(self) -> None:
        while True:
            try:
                await self._sweep_once()
            except psycopg.Error as exc:
                log.warning("sweep failed: %s", type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

    async def _sweep_once(self) -> None:
        if self.schedules and not self.rezoned:
            self.rezoned = await schedule.rezone(self.ctx)  # never raises
        async with self.ctx.pool.connection() as conn:
            firings = await self._due(conn)
            checks = await self._checks_due(conn)
            active = [
                r
                for status in ("cancelling", "running")
                for r in await repo.pages(repo.list_runs, conn, status=status)
            ]
            for run in active:
                if run["status"] == "cancelling":  # again until the expired leases are cancelled too
                    await repo.cancel_keys(conn, run["run_id"])
            uncounted = await repo.census_due(conn) if self.counting else []
            undrawn = await repo.draws_due(conn)
            unguarded = await self._unguarded(conn, active)
        for run_id, fallen in unguarded.items():
            if fallen and run_id not in self.guards:
                self.guards[run_id] = asyncio.create_task(self._guard(run_id))
        for run_id in uncounted:  # a cycle's census and draw wait for the schema guard's look at its start
            if run_id not in self.censuses and run_id not in unguarded:
                self.censuses[run_id] = asyncio.create_task(self._census(run_id))
        for run_id in undrawn:
            if run_id not in self.draws and run_id not in unguarded:
                self.draws[run_id] = asyncio.create_task(self._draw(run_id))
        self._check_due(checks)
        if firings is not None:  # what is kept is a due time's own; a list not read forgets nothing
            due = {row["schedule_id"]: row["next_due"] for row in firings}
            self.queued = {s: was for s, was in self.queued.items() if s in due and due[s] == was[0]}
            self.tried = {s: was for s, was in self.tried.items() if s in due and due[s] == was[0]}
        for row in firings or ():
            schedule_id = row["schedule_id"]
            if schedule_id in self.firing or self.waiting.get(schedule_id, 0) > time.monotonic():
                continue
            if len(self.firing) >= FIRING:  # left without a slot: its window may close meanwhile
                self.queued.setdefault(schedule_id, (row["next_due"], row["now"]))
                continue
            queued = self.queued.pop(schedule_id, None)
            self.firing[schedule_id] = asyncio.create_task(self._fire(schedule_id, queued))
        running = {run["run_id"] for run in active if run["status"] == "running"}
        for run_id, scanning in self.scans.items():
            if run_id not in running:  # paused, cancelled or stopped: its statements end now, not when
                scanning.cancel()  # its next checkpoint would have told it (ENGINES.md §6)
        now = datetime.now(UTC)
        for run in active:
            scan = run["scan"] if run["status"] == "running" else None
            due = (
                scan is not None
                and not scan["done"]
                and datetime.fromisoformat(scan.get("due") or now.isoformat()) <= now
            )
            if due and run["run_id"] not in self.scans:
                self.scans[run["run_id"]] = asyncio.create_task(self._scan(run["run_id"], scan["checkpoint"]))
        if time.monotonic() - self.watched_at >= WATCH_EVERY_S:
            self.watched_at = time.monotonic()
            await licence.watch(self.ctx)  # never raises
        if self.purging is None and time.monotonic() - self.purged_at >= PURGE_EVERY_S:
            self.purged_at = time.monotonic()
            self.purging = asyncio.create_task(self._purge())
        for run in active:
            try:
                await finish_cycle(self.ctx, run["run_id"], who=WORKER)
            except psycopg.OperationalError:
                raise  # the store is unreachable: the sweep tries again, the run is fine
            except KEY_MISSING as exc:
                await self._key_missing(run["run_id"], exc)
            except KeyAuthRefused as exc:  # a rolling key table's: never tried again in a loop
                log.warning("run %s paused at its roll: %s", run["run_id"], exc)
                await pause_run(self.ctx, run["run_id"], reason=AUTH_PAUSE, who=WORKER)
            except Exception as exc:  # a bug, or data the store refuses: this run only
                log.warning("run %s cannot end its cycle: %s", run["run_id"], failure(exc))
                await fail_run(self.ctx, run["run_id"], exc, who=WORKER)
        self._notify()
        if self.measuring is None:
            self.measuring = asyncio.create_task(self._readiness())
        live = {r["run_id"] for r in active}
        self.stalled = {run_id: passes for run_id, passes in self.stalled.items() if run_id in live}
        for run_id in [r for r in self.runs if r not in live]:
            if await run_status(self.ctx, run_id) in repo.TERMINAL:
                await self.runs.pop(run_id).reader.aclose()

    async def _due(self, conn: repo.Conn) -> list[repo.Row] | None:
        """The schedules to fire now, none unless this worker fires schedules; a fault reading them is
        logged and leaves the sweep to its runs (None: not read)."""
        if not self.schedules:
            return []
        try:
            async with conn.transaction():
                return await repo.schedules_due(conn)
        except psycopg.OperationalError:
            raise  # the store is unreachable: the sweep tries again
        except Exception as exc:
            log.warning("due schedules not read: %s", failure(exc))
            return None

    async def _unguarded(self, conn: repo.Conn, active: list[repo.Row]) -> dict[str, bool]:
        """The running key-list runs whose cycle start the schema guard has not looked at (V3), each with
        whether that cycle has fallen due: a soak's next cycle is opened when the one before ends. A due
        time that cannot be told counts as fallen: the look records why it was not made."""
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
        return found

    async def _checks_due(self, conn: repo.Conn) -> list[repo.Row] | None:
        """The pairs of table pre-checks to read or being read. One that is running without a task of this
        worker is set back to pending first: the worker that read it is gone (on the first sweep after the
        lock was taken, every running one), or its task ended without settling it. A fault reading them is
        logged and leaves the sweep to its runs (None: not read)."""
        try:
            async with conn.transaction():
                due = await repo.prechecks_due(conn)
                lost = [
                    key
                    for row in due
                    if row["status"] == "running" and (key := _pair(row)) not in self.checks
                ]
                if lost and await repo.requeue_prechecks(conn, lost):
                    due = await repo.prechecks_due(conn)
                return due
        except psycopg.OperationalError:
            raise  # the store is unreachable: the sweep tries again
        except Exception as exc:
            log.warning("due table pre-checks not read: %s", failure(exc))
            return None

    def _check_due(self, due: list[repo.Row] | None) -> None:
        """Start a task for each pending pair whose targets each have a pre-check session free, holding
        one a target, and cancel the tasks whose pair is no longer due: its pre-check was cancelled, and
        its statements end now."""
        if due is None:
            return
        listed = {_pair(row) for row in due}
        for key, checking in self.checks.items():
            if key not in listed:
                checking.cancel()
        most = self.ctx.settings.precheck.sessions_per_target
        for row in due:
            key, targets = _pair(row), {row["target_a"], row["target_b"]}
            busy = any(self.reading[target] >= most for target in targets)
            if row["status"] == "pending" and key not in self.checks and not busy:
                self.reading.update(targets)
                self.checks[key] = asyncio.create_task(self._check(key, targets))

    async def _check(self, key: tuple[str, int], targets: set[str]) -> None:
        """Read one pair of a table pre-check and settle it, its claim renewed meanwhile. Whatever escapes
        (the store unreachable, a bug) is logged and the pair read again a lease period later: no run fails
        for it. Cancelled (the worker stops, or the pre-check was cancelled), it sets its pair back to
        pending if it is running: the next worker reads it, and no pair stays running without a reader."""
        holding = asyncio.create_task(self._hold(key))
        try:
            await precheck.check(self.ctx, *key)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                async with self.ctx.pool.connection() as conn:
                    await repo.requeue_prechecks(conn, [key])
            raise
        except Exception as exc:
            log.warning("table pre-check %s pair %s not settled: %s", *key, failure(exc))
            await asyncio.sleep(self.settings.lease_seconds)
        finally:
            holding.cancel()
            self.checks.pop(key, None)
            self.reading.subtract(targets)

    async def _hold(self, key: tuple[str, int]) -> None:
        """Renew the pair's claim while it is read, as ``_keep`` renews a lease: a running pair whose claim
        is older than ``prechecks.HELD`` lease periods is read by no worker (PRECHECKS.md §10's banner)."""
        while True:
            await asyncio.sleep(self.settings.lease_seconds / 3)
            with contextlib.suppress(psycopg.Error):
                async with self.ctx.pool.connection() as conn:
                    await repo.hold_pair(conn, *key)

    async def _fire(self, schedule_id: str, queued: schedule.Queued | None) -> None:
        """Fire the schedule's due time, or settle why not (``schedule.fire``). A time that waits, and
        whatever escapes (the store unreachable, a bug), is not tried again before its time: nothing loops
        at the poll rate, and no run fails for it. The attempts counted are those of the schedule's next
        due time as the sweep read it: the sweep forgets another time's."""
        _, tried = self.tried.get(schedule_id, (None, 0))
        try:
            retry = await schedule.fire(self.ctx, schedule_id, queued=queued, tried=tried)
        except Exception as exc:
            log.warning("schedule %s not fired: %s", schedule_id, failure(exc))
            self.waiting[schedule_id] = time.monotonic() + schedule.RETRY_SLOW_S  # its count stands
            return
        finally:
            self.firing.pop(schedule_id, None)
        if retry is None:
            self.waiting.pop(schedule_id, None)
            self.tried.pop(schedule_id, None)
        else:
            self.waiting[schedule_id] = time.monotonic() + retry.seconds
            self.tried[schedule_id] = (retry.due, retry.tried)

    def _notify(self) -> None:
        """Start the notifier when none runs (with ``notify`` absent, once): no query, nothing awaited."""
        ended = self.notifying
        if ended is not None and ended.done() and not ended.cancelled() and (exc := ended.exception()):
            log.warning("notifier stopped: %s", failure(exc))
        if ended is None or (ended.done() and self.ctx.settings.notify is not None):
            self.notifying = asyncio.create_task(self.notifier.run())

    async def _guard(self, run_id: str) -> None:
        """The schema guard's look at the start of the run's cycle (V3), which records what it meets: a
        refused login pauses the run, and only a store that cannot record the look leaves the cycle's
        start unlooked at, to be looked at again after a lease period."""
        try:
            await drift.cycle_start(self.ctx, run_id)
        except TargetAuthRefused as exc:  # never tried again in a loop: the account could lock
            log.warning("schema guard of run %s paused it: %s", run_id, code(exc))
            await pause_run(self.ctx, run_id, reason=AUTH_PAUSE, who=WORKER)
        except Exception as exc:
            log.warning("schema guard of run %s postponed: %s", run_id, failure(exc))
            await asyncio.sleep(self.settings.lease_seconds)
        finally:
            self.guards.pop(run_id, None)

    async def _scan(self, run_id: str, checkpoint: int) -> None:
        """Scan the run on from ``checkpoint``, the last its scan saved."""
        try:
            if reason := await scan(self.ctx, run_id):  # until or maxDuration: the run stops
                await stop_run(self.ctx, run_id, reason, who=WORKER)
            self.stalled.pop(run_id, None)
        except drift.Tripped:  # the schema guard paused the run before a cursor opened, and logged it
            self.stalled.pop(run_id, None)
        except KEY_MISSING as exc:
            await self._key_missing(run_id, exc)
        except TargetAuthRefused as exc:  # never tried again in a loop: the account could lock
            log.warning("scan of run %s paused: %s", run_id, code(exc))
            await pause_run(self.ctx, run_id, reason=AUTH_PAUSE, who=WORKER)
        except Exception as exc:
            # A database unreachable: a later sweep resumes it, as it does after a server's code a resume
            # may not meet again, until three passes in a row saved no checkpoint; such a pass waits a
            # lease period too, so the three span minutes. Data past a server limit would fail every
            # resume, so it fails the run, as a bug, an unreadable DSN, a privilege or a scan error do:
            # unless the schema guard finds that a column or a table the plan reads is gone (V5).
            limit = isinstance(exc, psycopg.errors.ProgramLimitExceeded)
            if isinstance(exc, UNREACHABLE) and not limit:
                log.warning("scan of run %s paused: %s", run_id, code(exc))
                await asyncio.sleep(self.settings.lease_seconds)
            elif isinstance(exc, STALLS) and not await self._stalled(
                run_id, checkpoint, getattr(exc, "fixed", False)
            ):
                log.warning("scan of run %s resumes: %s", run_id, code(exc))
                if self.stalled[run_id]:
                    await asyncio.sleep(self.settings.lease_seconds)
            elif not (isinstance(exc, drift.DENIED) and await drift.failed(self.ctx, run_id)):
                log.warning("scan of run %s failed: %s", run_id, failure(exc))
                await fail_run(self.ctx, run_id, exc, who=WORKER)
        finally:
            self.scans.pop(run_id, None)

    async def _stalled(self, run_id: str, checkpoint: int, fixed: bool = False) -> bool:
        """Whether the scan's pass from ``checkpoint`` was the third in a row to save none: a code every
        resume would meet again cannot loop for ever (docs/mvp/ENGINES.md §11). A read error of ``fixed``
        files that did not change counts whether or not its pass saved one: the same bytes fail the next
        pass the same way (docs/mvp/FILES.md §10)."""
        async with self.ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
        saved = run is not None and run["scan"] is not None and run["scan"]["checkpoint"] > checkpoint
        self.stalled[run_id] = 0 if saved and not fixed else self.stalled.get(run_id, 0) + 1
        return self.stalled[run_id] >= STALLED

    async def _draw(self, run_id: str) -> None:
        """Draw the run's sample; a frame's refusal is recorded by the draw, which pauses the run, as the
        schema guard does when the frame met a column or a table the plan reads that is gone (V5)."""
        try:
            await sample.draw(self.ctx, run_id)
        except KEY_MISSING as exc:
            await self._key_missing(run_id, exc)
        except UNREACHABLE as exc:  # a database unreachable: the next sweep draws again
            log.warning("draw of run %s postponed: %s", run_id, code(exc))
            await asyncio.sleep(self.settings.lease_seconds)
        except Exception as exc:
            if not (isinstance(exc, drift.DENIED) and await drift.failed(self.ctx, run_id)):
                log.warning("draw of run %s failed: %s", run_id, failure(exc))
                await fail_run(self.ctx, run_id, exc, who=WORKER)
        finally:
            self.draws.pop(run_id, None)

    async def _census(self, run_id: str) -> None:
        """Count the run's cycle; a failed count is recorded, so only a store that cannot record it leaves
        the cycle uncounted, to be counted again after a lease period. A file side whose rows are not the
        ones its plan counted fails the run (docs/mvp/FILES.md §7, F10)."""
        try:
            await census.record(self.ctx, run_id)
        except RowsMoved as exc:
            log.warning("census of run %s failed: %s", run_id, failure(exc))
            await fail_run(self.ctx, run_id, exc, who=WORKER)
        except Exception as exc:
            log.warning("census of run %s not recorded: %s", run_id, failure(exc))
            await asyncio.sleep(self.settings.lease_seconds)
        finally:
            self.censuses.pop(run_id, None)

    async def _purge(self) -> None:
        """A purge pass as ``system:worker``, after the grace; skipped while another purger runs. The spill
        directories of processes that ended are swept first, grace or not (docs/mvp/FILES.md §10)."""
        try:
            await manifest.call(manifest.sweep, self.ctx.settings.spill_dir)
        except Exception as exc:  # the next hour tries again
            log.warning("spill not swept: %s", failure(exc))
        try:
            async with self.ctx.pool.connection() as conn:
                if not await repo.grace_over(conn, GRACE_DAYS):
                    return
            orphans = time.monotonic() - self.swept_at >= ORPHANS_EVERY_S
            await purge.purge(self.ctx, WORKER, orphans=orphans)
            if orphans:
                self.swept_at = time.monotonic()
        except purge.PurgeBusy:
            log.info("purge skipped this hour: %s", purge.BUSY)
        except Exception as exc:  # the next hour tries again; an interrupted cycle resumes first
            log.warning("purge pass failed: %s", failure(exc))
        finally:
            self.purging = None

    async def _readiness(self) -> None:
        """A readiness pass, which logs what it meets; the next sweep starts the next."""
        try:
            await readiness.watch(self.ctx, self.readiness)
        finally:
            self.measuring = None

    async def _key_missing(self, run_id: str, exc: Exception) -> None:
        """Pause a running run that needs a KEK the keyring lacks; ``resume_run`` clears the reason once
        the line is back and the app restarted. Its message names ids only."""
        log.warning("run %s paused: %s", run_id, exc)
        await pause_run(self.ctx, run_id, reason=KEY_PAUSE, who=WORKER)

    async def _handle(self, rows: list[repo.Row], owner: str) -> None:
        run_id, finished = rows[0]["run_id"], False
        renewing = asyncio.create_task(self._keep(owner))
        try:
            run = await self._run(rows[0])
            if isinstance(run.reader, LiveReader) and run.reader.batches:  # a group (TABLES.md §12)
                async with self.ctx.pool.connection() as conn:
                    # Few keys where a row may be large; beside fixed files the whole group, whose every read
                    # of them is a pass (docs/mvp/FILES.md §12): ``_process`` reads the large side in slices.
                    few = run.reader.large and not run.reader.fixed
                    more = (LOB_BATCH if few else READ_BATCH) - len(rows)
                    rows += await repo.lease_due(
                        conn, owner, more, self.settings.lease_seconds, run_id=run_id, census=self.counting
                    )
            async with run.lanes or contextlib.nullcontext():
                if not await self._renewed(owner):
                    return  # the run was paused or stopped while the key waited: it is handed back
                if run.halted:  # this process paused the run on a refused credential; it runs again
                    run.halted = False
                    m = run.monitor  # judged afresh, but the CRITICAL findings still exist
                    run.monitor = StopMonitor(m.conditions, m.started_at, m.critical)
                    if isinstance(run.reader, LiveReader):
                        run.reader.reconnect()  # the credential file was fixed: read it again
                finished = await self._process(run, rows, owner)
                if run.lanes is not None:
                    execution = run.spec.execution
                    wait = execution.delay_s + execution.jitter_s * key_fraction(rows[0]["key_masked"])
                    await asyncio.sleep(wait)
        except _LeaseLost:
            finished = True
        except KEY_MISSING as exc:  # the keys go back, unread or unsaved, until the KEK is restored
            await self._key_missing(run_id, exc)
        except psycopg.OperationalError as exc:  # the store is unreachable: the keys are read again
            log.warning("keys of run %s not saved: %s", run_id, type(exc).__name__)
        except Exception as exc:  # a bug, or data the store refuses: never read again in a loop
            finished = await self._release(owner)  # first, so the failed run cancels this key too
            # V5: a column or a table the plan reads is gone, which pauses the run, its keys handed back.
            if isinstance(exc, drift.DENIED) and await drift.failed(self.ctx, run_id):
                if run_id in self.runs:
                    self.runs[run_id].halted = True  # it reads on a fresh session once resumed
            else:
                log.warning("run %s failed: %s", run_id, failure(exc))
                await fail_run(self.ctx, run_id, exc, who=WORKER)
        finally:
            renewing.cancel()
            if not finished:
                await self._release(owner)

    async def _renewed(self, owner: str) -> bool:
        async with self.ctx.pool.connection() as conn:
            return await repo.renew_lease(conn, owner, self.settings.lease_seconds)

    async def _keep(self, owner: str) -> None:
        """Renew the leases while the keys wait and are read: a slow key is never read twice."""
        while True:
            await asyncio.sleep(self.settings.lease_seconds / 3)
            with contextlib.suppress(psycopg.Error):
                await self._renewed(owner)

    async def _release(self, owner: str) -> bool:
        with contextlib.suppress(Exception):
            async with self.ctx.pool.connection() as conn:
                await repo.release_leases(conn, owner)
        return True

    async def _process(self, run: _Run, rows: list[repo.Row], owner: str) -> bool:
        """Open the group's leased rows, then read, judge and save one pass of each key, saved together;
        False: a side refused the credential, and the keys go back. Where a side reads fixed files and the
        other's rows may be large (docs/mvp/FILES.md §12), the files are read for the group's first passes
        in one pass over them, then the group is read, judged and saved ``LOB_BATCH`` keys at a time, so no
        more large rows are held than a group of such a side alone holds; False also when the run stopped
        before its last keys, which go back."""
        async with self.ctx.pool.connection() as conn:
            sealer = await self.ctx.sealer(conn, rows[0]["run_id"])
        rows = [sealer.open_row(row) for row in rows]
        reader = run.reader
        if not (isinstance(reader, LiveReader) and reader.batches and reader.large and reader.fixed):
            return await self._passed(run, rows, owner, sealer)
        ahead = [row["key"] for row in rows if row["pass_no"] == 1]
        await reader.ahead(ahead)
        for start in range(0, len(rows), LOB_BATCH):
            last = start + LOB_BATCH >= len(rows)
            if not await self._passed(run, rows[start : start + LOB_BATCH], owner, sealer, ahead):
                return False
            if run.halted and not last:
                return False
        return True

    async def _passed(
        self, run: _Run, rows: list[repo.Row], owner: str, sealer: Sealer, ahead: Sequence[str] = ()
    ) -> bool:
        """``_process`` of opened rows; ``ahead``: the keys whose sides of fixed files are read already."""
        ctx, spec = self.ctx, run.spec
        run_id, cycle = rows[0]["run_id"], rows[0]["cycle"]
        firsts = {}
        rechecks = [row for row in rows if row["pass_no"] > 1]
        if rechecks:
            masked = [row["key_masked"] for row in rechecks]
            async with ctx.pool.connection() as conn:
                stored = await repo.get_results(conn, run_id, cycle, masked)
                derived = await rederive_all(ctx, conn, spec, run_id, [stored[k] for k in masked], run.caps)
            firsts = dict(zip(masked, derived, strict=True))
        held = {}
        if isinstance(run.reader, LiveReader) and run.reader.batches:  # a recheck reads B first, as evaluate
            # but a side of fixed files, whose first observation stands (docs/mvp/FILES.md §12)
            held = await run.reader.standing({row["key"]: firsts[row["key_masked"]] for row in rechecks})
            first = [row["key"] for row in rows if row["pass_no"] == 1]
            again = [row["key"] for row in rechecks]
            await run.reader.prefetch([(first, (Side.A, Side.B)), (again, (Side.B, Side.A))], held, ahead)
        keys = [(row["key"], row["pass_no"]) for row in rows]
        evaluated = await evaluate_many(ctx, spec, run.caps, run.reader, keys, run_id=run_id, held=held)
        if run.live and any(
            obs.error_class == "auth_error" for _, reads in evaluated for obs in reads.values()
        ):
            run.halted = True  # a refused credential is not hammered, and it judges nothing
            await pause_run(ctx, run_id, reason=AUTH_PAUSE, who=WORKER)
            return False
        now, passes, due, retry = datetime.now(UTC), [], {}, {}
        resolve = spec.sample.resolve if spec.sample else None
        for row, (read, reads) in zip(rows, evaluated, strict=True):
            masked = row["key_masked"]
            result = _confirmed(run, row["pass_no"], read, firsts.get(masked))
            if run.live and row["pass_no"] <= run.policy.times and run.policy.due(result, run.reader.fixed):
                wait = run.policy.delay_s + run.policy.jitter_s * key_fraction(masked)
                due[masked] = now + timedelta(seconds=wait)
            elif (
                resolve is not None
                and row["signoff_member"]
                and row["retries"] < resolve.times
                and sample.retriable(result, firsts.get(masked))
            ):
                wait = resolve.delay_s + run.policy.jitter_s * key_fraction(masked)
                retry[masked] = now + timedelta(seconds=wait)
            passes.append((row, read, result, reads))
        async with ctx.pool.connection() as conn:
            await save_passes(
                ctx,
                conn,
                spec,
                run_id,
                cycle,
                [(row["pass_no"], result, reads) for row, _, result, reads in passes],
                sealer,
            )
            later = due | retry
            done = [row["key_masked"] for row, *_ in passes if row["key_masked"] not in later]
            moved = await repo.schedule_rechecks(conn, run_id, cycle, due, owner) if due else 0
            moved += await repo.schedule_retries(conn, run_id, cycle, retry, owner) if retry else 0
            moved += await repo.complete_keys(conn, run_id, cycle, done, owner) if done else 0
            if moved != len(passes):
                raise _LeaseLost  # another worker holds a key now: the whole group rolls back
        for row, read, result, reads in passes:
            for side in reads:
                state = read.side_a if side is Side.A else read.side_b
                run.monitor.record_call(failed=state.kind is SideStateKind.ERROR)
            first_read = row["pass_no"] == 1 and row["retries"] == 0  # a retry's read is counted once
            run.monitor.record_result(row["key_masked"], result, first_read=first_read)
        if reason := run.monitor.reason(datetime.now(UTC)):
            run.halted = True
            await stop_run(ctx, run_id, reason, who=WORKER)
        return True

    async def _run(self, row: repo.Row) -> _Run:
        run_id = row["run_id"]
        if run_id not in self.runs:
            async with self.ctx.pool.connection() as conn:
                stored = await repo.get_run(conn, run_id)
                samples = stored["params"].get("samples_dir")
                spec = await self.ctx.spec(conn, row["spec_id"], live=not samples)  # live: calls targets
                critical = await repo.unexplained_critical(conn, run_id)  # saved by an earlier worker too
                tables = stored["params"].get("tables")
                if stored["params"].get("pinned"):  # a file side's pins (docs/mvp/FILES.md §6)
                    tables = unsealed(await self.ctx.sealer(conn, run_id), stored["params"])
            reader = FileReader(samples, spec) if samples else LiveReader(self.ctx, spec, tables)
            monitor = StopMonitor(StopConditions.from_spec(spec.raw), stored["started_at"], critical)
            lanes = None if samples else asyncio.Semaphore(spec.execution.concurrency)
            policy = RecheckPolicy.from_spec(spec.raw)
            caps = self.ctx.capabilities(stored)
            scanned = stored["scan"] is not None
            self.runs.setdefault(
                run_id, _Run(spec, caps, reader, not samples, policy, monitor, lanes, scanned)
            )
        return self.runs[run_id]

    async def close(self) -> None:
        tasks = [*self.scans.values(), *self.censuses.values(), *self.draws.values(), *self.firing.values()]
        tasks += [*self.checks.values(), *self.guards.values(), *filter(None, [self.purging, self.measuring])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.notifier.close(self.notifying)  # waits a bounded time: never holds a stop
        for run in self.runs.values():
            await run.reader.aclose()


def _pair(row: repo.Row) -> tuple[str, int]:
    """A pre-check's pair as the worker's tasks are keyed."""
    return row["precheck_id"], row["n"]


def _confirmed(run: _Run, pass_no: int, read: KeyResult, first: KeyResult | None) -> KeyResult:
    """A pass's result: a recheck is judged against the first read. A scanned key's usable first read
    is confirmed already: the scan read it once before (TABLES.md §12); a read of fixed files on both sides
    is final, since reading them again is a no-op."""
    result = read if first is None else confirm(first, read, spec=run.spec, last=pass_no > run.policy.times)
    result = freeze(result, run.reader.fixed)  # docs/mvp/FILES.md §12
    if run.scanned and result.outcome is not Outcome.INCONCLUSIVE and run.policy.due(result):
        result.confirmation = Confirmation.RECHECKED
    return result


async def _sole(lock: psycopg.AsyncConnection, stop: asyncio.Event, poll_s: float) -> bool:
    """Take the database's worker lock, waiting while another worker holds it; False: stopped first."""

    async def taken() -> bool:
        return (await (await lock.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))).fetchone())[0]

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


async def run_worker(ctx: Context, *, stop: asyncio.Event | None = None, schedules: bool = True) -> None:
    """Process keys until ``stop`` is set or the task is cancelled; unfinished leases are handed back.

    The worker lock lives on its own connection. If that connection is lost (a database restart),
    another worker may take the lock, so this one stops its slots at once and waits to retake it;
    a slot or the sweep that meets a bug restarts the same way, logged. Without ``schedules`` it neither
    rezones nor fires them (``ds run``'s worker, whose config and targets are one invocation's).
    """
    stop = stop or asyncio.Event()
    poll_s = ctx.settings.worker.poll_seconds
    while not stop.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(ctx.pool.conninfo, autocommit=True) as lock:
                if not await _sole(lock, stop, poll_s):
                    return
                await _work(ctx, lock, stop, schedules)
        except Exception as exc:  # the lock lost, or a slot or the sweep ended on a bug
            log.warning("worker stopped (%s): slots restart once the lock is retaken", failure(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll_s)


async def _work(ctx: Context, lock: psycopg.AsyncConnection, stop: asyncio.Event, schedules: bool) -> None:
    """Run the slots and the sweep while holding the lock; raise when it is lost or one of them ends."""
    worker = _Worker(ctx, schedules)
    tasks = [asyncio.create_task(worker.slot()) for _ in range(ctx.settings.worker.concurrency)]
    tasks.append(asyncio.create_task(worker.sweep()))
    check_s = ctx.settings.worker.lease_seconds / 3  # well before any lease this worker holds expires
    try:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), check_s)
            await lock.execute("SELECT 1")
            for task in tasks:
                if task.done():  # they loop forever: an ended one met a bug
                    task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await worker.close()
