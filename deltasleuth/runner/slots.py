"""The worker's slots: each leases a group of a running run's keys, reads both sides, judges and saves them.

Each of ``worker.concurrency`` slots leases a group of keys of one run, under a lease owner of its own so the
group can be handed back alone: ``READ_BATCH`` keys of a run whose sides are all tables, read with one
statement a side; ``FILE_BATCH`` keys of a run one of whose sides reads fixed files, one pass over them a
group, then the other side read (a service key by key in the same slot), judged and saved ``READ_BATCH`` keys
at a time; else one key; ``LOB_BATCH`` keys where a table side's rows may be large, but beside fixed files
``READ_BATCH``: one pass over the files, then the other side read, judged and saved ``LOB_BATCH`` keys at a
time. It renews the leases while the keys wait and are read: A then B, or B then A on a recheck pass, which
``confirm`` judges against the stored first read; a recheck reads no side of fixed files again, whose first
observation stands. The group's observations, results, masked findings and next states (done, or a recheck
due ``recheck.delay`` + jitter after the database's now) are saved in one transaction; a lost lease rolls it
back, so a kill -9 costs at most the keys in flight and never duplicates a result. A table scan queues its
candidates for the same first read and recheck as other live keys.

Politeness: one gate per target for the process (``ctx.gates``: rate budget, in-flight cap, and a breaker
for each ``execution.circuitBreaker`` its specs declare, so a run is governed by its own spec's; where more
than one worker processes keys, this process's share of the target's budget), at most
``execution.concurrency`` groups of a live run in flight, each pausing ``execution.delay`` plus jitter (from
a hash of a key) before its next group; no slot leases keys of a run with none of those free, or of a run
that reads a target whose in-flight cap leaves this process no share, and keys are read only if their run
still runs. A free slot leases a key of the run with the fewest groups in flight. A live 401/403 pauses
the run and hands the group back unjudged, to be read again once resumed, with the credentials read again;
so does a key the keyring lacks (the run's data key, or an evidence key), and an evidence write the disk
refuses; a stop condition stops the run; a column or a table the plan reads that is
gone pauses it through the schema guard; any other failure fails it. With key censuses counted, no slot
leases the keys of a cycle whose census is not recorded. A worker alone judges a run's stop conditions as
its slots save groups; where more than one processes keys, each group's save feeds them to the store in its
transaction, for the sweep to judge (``runner.stop``).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import secrets
import socket
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeGuard

import psycopg

from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors.tables import unsealed
from deltasleuth.context import Capabilities, Context
from deltasleuth.core.types import CompiledSpec, KeyResult, Side, SideStateKind
from deltasleuth.evidence.store import EvidenceKeyError, EvidenceStore
from deltasleuth.runner import drift, sample
from deltasleuth.runner.compare import (
    FileReader,
    LiveReader,
    Reader,
    Reads,
    evaluate_many,
    rederive_all,
    save_passes,
)
from deltasleuth.runner.cycle import fail_run, located
from deltasleuth.runner.judges import Judges
from deltasleuth.runner.recheck import RecheckPolicy, confirm, freeze, key_fraction
from deltasleuth.runner.runs import AUTH_PAUSE, EVIDENCE_PAUSE, KEY_PAUSE, pause_run, run_status, stop_run
from deltasleuth.runner.stop import Passed, StopConditions, StopMonitor
from deltasleuth.security.crypto import KeyringError, Sealer
from deltasleuth.security.tokenise import KeyForm, key_form
from deltasleuth.store import repo

READ_BATCH = 200  # keys a slot reads at once from a run that reads in groups (``LiveReader.batches``)
FILE_BATCH = 2000  # beside fixed files whose rows are not large: one pass over them, judged READ_BATCH a time
LOB_BATCH = 4  # of a run whose rows may hold large values: 16 MiB of them a row
KEY_MISSING = (KeyringError, EvidenceKeyError)  # a KEK the keyring lacks: the run waits for it
# The evidence store's writes: an OSError raised under one (a full disk, an I/O error, too many open files)
# pauses the run until a person puts it right.
EVIDENCE_WRITES = frozenset(
    f.__code__ for f in (EvidenceStore.put_blob, EvidenceStore.put_all, EvidenceStore.ensure)
)
REFUSALS = 10  # times a key goes back unread for reads refused unsent before it is judged as read
REFUSED_WAIT_S = 1.0  # the least wait of a key handed back so
log = logging.getLogger("deltasleuth.runner.worker")  # the worker's lines, whichever of its parts writes them


class _LeaseLost(Exception):
    """Another worker holds the key now: this pass is rolled back."""


@dataclass
class _Run:
    spec: CompiledSpec
    caps: Capabilities
    reader: Reader
    live: bool
    policy: RecheckPolicy
    monitor: StopMonitor
    lanes: asyncio.Semaphore | None  # live runs: execution.concurrency groups of keys in flight
    form: KeyForm  # how its keys are masked, as it recorded at its creation
    halted: bool = False  # this process paused or stopped the run
    feeding: bool = False  # its last group fed its stop conditions to the store, for the sweep to judge


async def key_missing(ctx: Context, run_id: str, exc: Exception) -> None:
    """Pause a running run that needs a KEK the keyring lacks; ``resume_run`` clears the reason once the line
    is back and the app restarted. Its message names ids only."""
    log.warning("run %s paused: %s", run_id, exc)
    await pause_run(ctx, run_id, reason=KEY_PAUSE, who=WORKER)


class Slots:
    """The slots of one worker and the runs they hold: each run is loaded by the first slot that meets it,
    and its reader closed once the run has ended (``forget``)."""

    def __init__(self, ctx: Context, judges: Judges | None = None) -> None:
        self.ctx, self.settings = ctx, ctx.settings.worker
        self.judges = judges  # the worker's judge processes; None: each group is judged in a thread
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self.runs: dict[str, _Run] = {}
        self.loading: dict[str, asyncio.Lock] = {}  # by run: held while a slot loads it into ``runs``
        self.busy: Counter[str] = Counter()  # by run: its groups in flight, which leases share out
        # key censuses gate the leases
        self.counting = ctx.settings.packs is not None or ctx.settings.readiness is not None

    async def slot(self) -> None:
        """Lease and process groups until cancelled; a store that cannot be reached is tried again after a
        poll, and any other failure ends the slot (``run_worker`` restarts the slots)."""
        while True:
            owner = f"{self.name}:{secrets.token_hex(4)}"
            full = [
                run_id
                for run_id, run in self.runs.items()
                if (run.lanes and run.lanes.locked()) or self._starved(run)
            ]
            try:
                async with self.ctx.pool.connection() as conn:
                    leased = await repo.lease_due(
                        conn,
                        owner,
                        1,
                        self.settings.lease_seconds,
                        skip=full,
                        census=self.counting,
                        busy=+self.busy,
                    )
                if leased:
                    await self._handle(leased, owner)
                    continue
            except psycopg.Error as exc:  # the store is unreachable: try again after a pause
                log.warning("worker slot: %s", type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

    async def forget(self, live: set[str]) -> None:
        """Close the reader of each run this worker holds that is no longer ``live`` (running or cancelling)
        and has ended."""
        for run_id in [r for r in self.runs if r not in live]:
            if await run_status(self.ctx, run_id) in repo.TERMINAL:
                self.loading.pop(run_id, None)
                await self.runs.pop(run_id).reader.aclose()

    async def close(self) -> None:
        for run in self.runs.values():
            await run.reader.aclose()

    async def _unwritten(self, run_id: str, exc: OSError) -> None:
        """Pause a running run whose evidence the store could not write; ``resume_run`` clears the reason
        once there is room again. Logged by its type, its errno's name and where it was raised: its message
        names a path."""
        code = errno.errorcode.get(exc.errno or 0, "no errno")
        log.warning("run %s paused, its evidence not written: %s (%s)", run_id, located(exc), code)
        await pause_run(self.ctx, run_id, reason=EVIDENCE_PAUSE, who=WORKER)

    async def _handle(self, rows: list[repo.Row], owner: str) -> None:
        run_id, finished = rows[0]["run_id"], False
        renewing = asyncio.create_task(self._keep(owner))
        self.busy[run_id] += 1
        try:
            run = await self._run(rows[0])
            if self._starved(run):  # its keys go back, for a worker with a share of its targets' slots
                return
            if run.reader.batches:  # a group
                async with self.ctx.pool.connection() as conn:
                    # Few keys where a row may be large; beside fixed files FILE_BATCH (READ_BATCH where a
                    # row may be large), whose every read of them is a pass: ``_process`` reads the other
                    # side, judges and saves in slices.
                    few = run.reader.large and not run.reader.fixed
                    many = run.reader.fixed and not run.reader.large
                    more = (LOB_BATCH if few else FILE_BATCH if many else READ_BATCH) - len(rows)
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
                    run.reader.reconnect()  # the credential file was fixed: read it again
                finished = await self._process(run, rows, owner)
                if run.lanes is not None:
                    execution = run.spec.execution
                    wait = execution.delay_s + execution.jitter_s * key_fraction(rows[0]["key_masked"])
                    await asyncio.sleep(wait)
        except _LeaseLost:  # rolled back: the keys it still holds go back below, due now
            pass
        except KEY_MISSING as exc:  # the keys go back, unread or unsaved, until the KEK is restored
            await key_missing(self.ctx, run_id, exc)
        except psycopg.OperationalError as exc:  # the store is unreachable: the keys are read again
            log.warning("keys of run %s not saved: %s", run_id, type(exc).__name__)
        except Exception as exc:  # a bug, or data the store refuses: never read again in a loop
            if _unwritable(exc):  # the keys go back unsaved, until the evidence store can write again
                await self._unwritten(run_id, exc)
                return
            finished = await self._release(owner)  # first, so the failed run cancels this key too
            # A column or a table the plan reads is gone, which pauses the run, its keys handed back.
            if isinstance(exc, drift.DENIED) and await drift.failed(self.ctx, run_id):
                if run_id in self.runs:
                    self.runs[run_id].halted = True  # it reads on a fresh session once resumed
            else:
                keys = [row["key_masked"] for row in rows]  # as stored for display: never a raw key
                log.warning("run %s failed: %s; keys %s", run_id, located(exc), keys)
                await fail_run(self.ctx, run_id, exc, who=WORKER)
        finally:
            self.busy[run_id] -= 1
            renewing.cancel()
            if not finished:
                await self._release(owner)

    def _starved(self, run: _Run) -> bool:
        """Whether this process holds no share of the in-flight cap of a target the run reads: more workers
        process keys than the target admits at once."""
        gates = self.ctx.gates
        targets = {side.target for side in run.spec.sides.values()} if run.live else set()
        return any(gates[t].max_concurrency == 0 for t in targets if t in gates)

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
        False: a side refused the credential, and the keys go back. Where a side reads fixed files, the files
        are read for the group's first passes in one pass over them, then the group is read, judged and saved
        ``READ_BATCH`` keys at a time, or ``LOB_BATCH`` where the other's rows may be large: no more judged
        keys, or large rows, are held than a group of a side of tables alone holds; False also when the run
        stopped before its last keys, which go back."""
        async with self.ctx.pool.connection() as conn:
            sealer = await self.ctx.sealer(conn, rows[0]["run_id"])
        rows = [sealer.open_row(row) for row in rows]
        reader = run.reader
        if not (reader.batches and reader.fixed):
            return await self._passed(run, rows, owner, sealer)
        ahead = [row["key"] for row in rows if row["pass_no"] == 1]
        await reader.ahead(ahead)
        step = LOB_BATCH if reader.large else READ_BATCH
        for start in range(0, len(rows), step):
            last = start + step >= len(rows)
            if not await self._passed(run, rows[start : start + step], owner, sealer, ahead):
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
                found = [stored[k] for k in masked]
                derived = await rederive_all(ctx, conn, spec, run_id, found, run.caps, judges=self.judges)
            firsts = dict(zip(masked, derived, strict=True))
        held = {}
        if run.reader.batches:  # a recheck reads B first, as evaluate
            # but a side of fixed files, whose first observation stands
            held = await run.reader.standing({row["key"]: firsts[row["key_masked"]] for row in rechecks})
            first = [row["key"] for row in rows if row["pass_no"] == 1]
            again = [row["key"] for row in rechecks]
            await run.reader.prefetch([(first, (Side.A, Side.B)), (again, (Side.B, Side.A))], held, ahead)
        keys = [(row["key"], row["pass_no"]) for row in rows]
        evaluated = await evaluate_many(
            ctx, spec, run.caps, run.reader, keys, run_id=run_id, held=held, judges=self.judges
        )
        if run.live and any(
            obs.error_class == "auth_error" for _, reads in evaluated for obs in reads.values()
        ):
            run.halted = True  # a refused credential is not hammered, and it judges nothing
            await pause_run(ctx, run_id, reason=AUTH_PAUSE, who=WORKER)
            return False
        passes, due, retry = [], {}, {}  # due and retry: the seconds after the database's now to wait
        refused: dict[str, float] = {}  # and of the keys handed back unjudged
        resolve = spec.sample.resolve if spec.sample else None
        for row, (read, reads) in zip(rows, evaluated, strict=True):
            masked = row["key_masked"]
            # A read refused unsent (an open breaker, a target's pause) took no time and observed nothing:
            # the key goes back, due once its target may be read, up to REFUSALS times.
            if row["refused"] < REFUSALS:  # captured samples are never refused
                if (wait := run.reader.refused(reads)) is not None:
                    refused[masked] = max(wait, REFUSED_WAIT_S)
                    continue
            result = _confirmed(run, row["pass_no"], read, firsts.get(masked))
            if run.live and row["pass_no"] <= run.policy.times and run.policy.due(result, run.reader.fixed):
                due[masked] = run.policy.wait_s(masked)
            elif (
                resolve is not None
                and row["signoff_member"]
                and row["retries"] < resolve.times
                and sample.retriable(result, firsts.get(masked))
            ):
                retry[masked] = resolve.delay_s + run.policy.jitter_s * key_fraction(masked)
            passes.append((row, read, result, reads))
        tally = [_tallied(row, read, result, reads) for row, read, result, reads in passes]
        async with ctx.pool.connection() as conn:
            fed = await _fed(ctx, conn, run, run_id, tally)
            if passes:
                await save_passes(
                    ctx,
                    conn,
                    spec,
                    run_id,
                    cycle,
                    [(row["pass_no"], result, reads) for row, _, result, reads in passes],
                    sealer,
                    form=run.form,
                )
            later = due | retry
            done = [row["key_masked"] for row, *_ in passes if row["key_masked"] not in later]
            moved = await repo.schedule_rechecks(conn, run_id, cycle, due, owner) if due else 0
            moved += await repo.schedule_retries(conn, run_id, cycle, retry, owner) if retry else 0
            moved += await repo.complete_keys(conn, run_id, cycle, done, owner) if done else 0
            moved += await repo.hand_back(conn, run_id, cycle, refused, owner) if refused else 0
            if moved != len(passes) + len(refused):
                raise _LeaseLost  # another worker holds a key now: the whole group rolls back
        if refused:
            log.info(
                "run %s: %d key(s) handed back unread, a target refusing reads for %.1f s",
                run_id, len(refused), max(refused.values()),
            )  # fmt: skip
        for passed in [] if fed else tally:
            run.monitor.record_pass(passed)
        if reason := run.monitor.reason(datetime.now(UTC)):
            run.halted = True
            await stop_run(ctx, run_id, reason, who=WORKER)
        return True

    async def _run(self, row: repo.Row) -> _Run:
        """The run as this worker holds it, loaded by the first slot that meets it; a slot that meets it
        while it loads waits for that load."""
        run_id = row["run_id"]
        if run_id in self.runs:
            return self.runs[run_id]
        async with self.loading.setdefault(run_id, asyncio.Lock()):
            if run_id not in self.runs:
                self.runs[run_id] = await self._load(run_id)
        return self.runs[run_id]

    async def _load(self, run_id: str) -> _Run:
        async with self.ctx.pool.connection() as conn:
            await repo.analyse_keys(conn)  # a new store's: before its groups' saves look keys up
            stored = await repo.get_run(conn, run_id)
            assert stored is not None  # a run is never deleted while its keys are leased
            samples = stored["params"].get("samples_dir")
            spec = await self.ctx.run_spec(conn, stored, live=not samples)  # live: calls targets
            conditions = StopConditions.from_spec(spec.raw)
            critical = (
                await repo.unexplained_critical(conn, run_id) if conditions.max_unexplained_critical else None
            )
            tables = stored["params"].get("tables")
            if stored["params"].get("pinned"):  # a file side's pins
                tables = unsealed(await self.ctx.sealer(conn, run_id), stored["params"])
        caps = self.ctx.capabilities(stored)
        reader = FileReader(samples, spec) if samples else LiveReader(self.ctx, spec, tables, caps=caps)
        monitor = StopMonitor(conditions, stored["started_at"], critical)
        lanes = None if samples else asyncio.Semaphore(spec.execution.concurrency)
        policy = RecheckPolicy.from_spec(spec.raw)
        form = key_form(stored["params"])
        return _Run(spec, caps, reader, not samples, policy, monitor, lanes, form)


def _unwritable(exc: BaseException) -> TypeGuard[OSError]:
    """Whether ``exc`` is an ``OSError`` that one of the evidence store's writes met. Of its traceback only
    each frame's code is looked at: nothing a frame holds is read."""
    trace = exc.__traceback__ if isinstance(exc, OSError) else None
    while trace is not None:
        if trace.tb_frame.f_code in EVIDENCE_WRITES:
            return True
        trace = trace.tb_next
    return False


async def _fed(ctx: Context, conn: repo.Conn, run: _Run, run_id: str, tally: list[Passed]) -> bool:
    """Feed the group's ``tally`` to the store, in its transaction, where the run's stop conditions count
    calls or keys and more than one worker processes keys: the sweep judges them; whether it did. A run whose
    last group was fed and whose worker is alone again has them judged afresh by its slots, the CRITICAL
    findings read again from the store."""
    conditions = run.monitor.conditions
    feeding = conditions.counted and ctx.gates.count > 1
    if feeding and not run.feeding:
        log.info("run %s: its stop conditions are judged by the sweep, beside other workers", run_id)
    elif run.feeding and not feeding:
        counts = conditions.max_unexplained_critical is not None
        critical = await repo.unexplained_critical(conn, run_id) if counts else None
        run.monitor = StopMonitor(conditions, run.monitor.started_at, critical)
    run.feeding = feeding
    if feeding and tally:
        keyed = conditions.max_unexplained_critical is not None
        await repo.feed_stops(conn, run_id, [passed.stored(keyed) for passed in tally])
    return feeding


def _tallied(row: repo.Row, read: KeyResult, result: KeyResult, reads: Reads) -> Passed:
    """What a saved pass gives the stop conditions: each side it read failed or not, and its result."""
    states = {Side.A: read.side_a, Side.B: read.side_b}
    calls = tuple(states[side].kind is SideStateKind.ERROR for side in reads)
    first_read = row["pass_no"] == 1 and row["retries"] == 0  # a retry's read is counted once
    return Passed.of(row["key_masked"], calls, result, first_read=first_read)


def _confirmed(run: _Run, pass_no: int, read: KeyResult, first: KeyResult | None) -> KeyResult:
    """Judge a recheck against its first observed documents; two fixed sides are final on the first read."""
    result = read if first is None else confirm(first, read, spec=run.spec, last=pass_no > run.policy.times)
    return freeze(result, run.reader.fixed)
