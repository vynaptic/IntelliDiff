"""Retention and purge: whole ended cycles are deleted once the days of
their run's retention class have passed, by an audited pass that is safe to interrupt.

A pass (``purge``: hourly in the worker, or ``ds purge``) holds the purge lock on a connection of its
own, so one purger runs at a time. It ends the runs past the ceiling first (``abandon``: one the schema
guard paused among them, as any paused run), drains the
blob queue, purges each due cycle (an interrupted purge first) and drains again. A cycle's purge is
recorded before anything irreversible (from then on its reads answer 410), then deletes its keys a
chunk per transaction in dependency order, queuing the blobs of the deleted observations, and ends
under the run's row lock. A crash leaves the cycle purging, and the next pass resumes it first. A
blob goes 24 h after its last reference, under the blob lock writers share (``repo.lock_blobs``). An
erasure (``erase``) purges one run, or one of its ended cycles, on demand. A pass ends by deleting the
table pre-checks made over ``retention.standard_days`` ago: they hold no key and
no value, so no hold or erasure applies to them, and their audit rows stay.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import asdict, dataclass, field

import psycopg

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal, printable
from deltasleuth.context import Context
from deltasleuth.evidence.store import EvidenceStore
from deltasleuth.runner import sample
from deltasleuth.runner.cycle import ERASURE, fail_run, failure
from deltasleuth.runner.runs import run_status, stop_run
from deltasleuth.security import rawvalues
from deltasleuth.store import repo

LOCK_ID = 0x44535047  # pg advisory lock "DSPG": one purger per database
CHUNK = 5_000  # keys (run_keys.seq) purged per transaction: well under a second each
DRAIN = 500  # blob addresses per blob-lock transaction
PRECHECKS = 1_000  # table pre-checks deleted per transaction
ORPHAN_AGE_S = 3600  # an unreferenced blob file this new may belong to a write in flight
REASON_CHARS = 200  # a hold's or an erasure's reason
ABANDONED = "abandoned"  # the stop reason of a run the ceiling cancels
BUSY = "another purge is running; try again later"
_LIVE = ("queued", "running", "paused", "cancelling")
log = logging.getLogger(__name__)


class CyclePurged(Exception):
    """A read of a cycle whose purge has started: the API answers 410."""


class AbandonedRun(Exception):
    """A one-shot run queued for 24 h, failed by the purge's ceiling."""


class PurgeBusy(ValueError):
    """Another purger holds the purge lock."""


@dataclass
class Summary:
    """What a pass or an erasure did."""

    cycles: int = 0
    runs: set[str] = field(default_factory=set)
    keys: int = 0
    blobs_queued: int = 0
    blobs_deleted: int = 0
    blobs_shared: int = 0  # queued blobs kept: another observation names them
    bytes: int = 0
    orphans: int = 0
    held: int = 0  # cycles past their retention, kept by a hold
    abandoned: int = 0
    prechecks: int = 0  # table pre-checks past the standard retention, deleted

    def finished(self) -> dict[str, int]:
        """The ``retention.finished`` detail."""
        names = (
            "cycles", "blobs_deleted", "blobs_shared", "bytes", "orphans", "held", "abandoned", "prechecks",
        )  # fmt: skip
        return {name: getattr(self, name) for name in names} | {"runs": len(self.runs)}


def days(ctx: Context) -> dict[str, int]:
    """The retention days of each class, as the repo's retention queries take them."""
    return asdict(ctx.settings.retention)


def reason_error(text: str) -> str | None:
    """Why ``text`` cannot be a hold's or an erasure's reason; None when it can."""
    if 1 <= len(text) <= REASON_CHARS and printable(text):
        return None
    return f"--reason must be 1 to {REASON_CHARS} characters without control characters"


def _purged_on(row: repo.Row) -> str:
    return f"{row['purging_at']:%Y-%m-%d}"


async def refuse_purged(conn: repo.Conn, run_id: str, cycle: int, *, lock: bool = False) -> None:
    """CyclePurged once the cycle's purge has started; ``lock`` reads the cycle FOR SHARE
    (``repo.get_cycle``), so that a purge starting meanwhile waits for the caller's transaction."""
    row = await repo.get_cycle(conn, run_id, cycle, lock=lock)
    if row is not None and row["purging_at"] is not None:
        reason = row["purge_reason"]
        why = ERASURE if reason.startswith(f"{ERASURE}:") else reason
        raise CyclePurged(
            f"cycle {cycle} of run {run_id} was purged on {_purged_on(row)} ({why}): its keys, results,"
            " findings and evidence are deleted"
        )


async def refuse_chat(conn: repo.Conn, run: repo.Row) -> None:
    """CyclePurged once the run's current cycle is purging: AI answers read it."""
    row = await repo.get_cycle(conn, run["run_id"], run["cycle"])
    if row is not None and row["purging_at"] is not None:
        raise CyclePurged(f"the run's evidence was purged on {_purged_on(row)}; AI answers need it")


@contextlib.asynccontextmanager
async def _sole(ctx: Context) -> AsyncIterator[None]:
    """Hold the purge lock for the block, on a connection of its own closed however the block ends
    (which frees the lock); PurgeBusy while another purger holds it."""
    async with await psycopg.AsyncConnection.connect(ctx.pool.conninfo, autocommit=True) as lock:
        taken = (await (await lock.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))).fetchone())[0]  # type: ignore[index]
        if not taken:
            raise PurgeBusy(BUSY)
        yield


# -- the pass -----------------------------------------------------------------------------------------


async def purge(ctx: Context, who: Principal, *, limit: int | None = None, orphans: bool = False) -> Summary:
    """One pass as ``who``: end the abandoned runs, drain the blob queue, purge up to ``limit`` due
    cycles, drain again, (``orphans``) delete orphaned files and delete the table pre-checks past the
    standard retention; audited ``retention.finished`` when it did anything. PurgeBusy while another
    purger runs."""
    async with _sole(ctx):
        done = Summary(abandoned=await abandon(ctx, who))
        await _drain(ctx, done)
        async with ctx.pool.connection() as conn:
            due = await repo.due_cycles(conn, **days(ctx), limit=limit)
        for row in due:
            await _purge_cycle(ctx, who, row["run_id"], row["cycle"], f"retention {row['retention']}", done)
        await _drain(ctx, done)
        if orphans:
            done.orphans = await _orphans(ctx)
        async with ctx.pool.connection() as conn:
            kept = await repo.kept_cycles(conn, **days(ctx))
        done.held = sum(row["hold_reason"] is not None for row in kept)
        done.prechecks = await _prechecks(ctx)
        await _finished(ctx, who, done)
    return done


async def abandon(ctx: Context, who: Principal) -> int:
    """The ceiling: fail each one-shot run queued for 24 h; cancel each run paused or queued longer
    than its retention days, its cycle ended when the run last moved, so that it is due at once. Each
    is audited ``retention.abandoned``; how many. A run that cannot be ended is logged, and the pass
    goes on."""
    async with ctx.pool.connection() as conn:
        rows = await repo.abandoned_runs(conn, **days(ctx))
    count = 0
    for row in rows:
        try:
            count += await _abandon(ctx, who, row)
        except Exception as exc:  # the next pass tries again
            log.warning("run %s not abandoned: %s", row["run_id"], failure(exc))
    return count


async def _abandon(ctx: Context, who: Principal, row: repo.Row) -> bool:
    """End the run of an ``abandoned_runs`` row; False when it moved meanwhile."""
    run_id = row["run_id"]
    detail = {
        "status": row["status"],
        "since": row["updated_at"].isoformat(),
        "retention": row["retention"],
        "days": row["days"],
    }
    if row["mode"] == "one_shot" and row["status"] == "queued":
        await fail_run(ctx, run_id, AbandonedRun(), who=who)
        if await run_status(ctx, run_id) != "failed":
            return False
        async with repo.audited(ctx.pool) as (_, audit):
            await audit(who, "retention.abandoned", run_id, detail)
        return True
    # Cancelled and ended in one transaction, unsummarised, so it needs no key; a worker's sweep never
    # sees it cancelling, so nothing re-dates its cycle, which ended when the run last moved. A drawn cycle
    # is judged, from stored rows.
    async with repo.audited(ctx.pool) as (conn, audit):
        stopped = await repo.set_run_status(
            conn, run_id, "cancelled", when=(row["status"],), stop_reason=ABANDONED
        )
        if not stopped:
            return False
        await repo.cancel_keys(conn, run_id)
        ended: repo.Row = await repo.get_run(conn, run_id)  # type: ignore[assignment]
        _, judgement = await sample.ending(ctx, conn, ended)
        await repo.end_cycle(conn, run_id, row["updated_at"], sample=judgement)
        await audit(who, "cancelling", run_id, {"reason": ABANDONED})
        await audit(who, "run.finished", run_id, {"status": "cancelled", "reason": ABANDONED})
        await audit(who, "retention.abandoned", run_id, detail)
    return True


async def _purge_cycle(
    ctx: Context, who: Principal, run_id: str, cycle: int, reason: str, done: Summary
) -> None:
    """Purge one ended cycle: record the decision (revoking the cycle's sign-off, whose facts go, unless an
    issued evidence pack cites it: the pack holds them), delete its keys a chunk at
    a time, then the rest (its stored pack zips among them)."""
    async with repo.audited(ctx.pool) as (conn, audit):
        row = await repo.start_purge(conn, run_id, cycle, reason)
        run = await repo.get_run(conn, run_id)
        if row is None:  # held since the pass read it (one purger runs, so none purged it meanwhile)
            if run is not None and run["hold_reason"] is not None:
                done.held += 1
            return
        signoffs = await repo.approval_history(conn, run_id=run_id, cycle=cycle)
        for signoff in signoffs:
            # Approved: its hold was released, or the cycle would not be due.
            if signoff["status"] == "approved" and not await repo.issued_packs(conn, signoff["approval_id"]):
                await approvals.revoke(conn, audit, signoff["approval_id"], f"cycle {cycle} purged", who=who)
        detail = {
            "cycle": cycle,
            "reason": row["purge_reason"],  # an interrupted purge's own
            "retention": run["retention"],  # type: ignore[index]
            "ended_at": row["ended_at"].isoformat(),
        }
        await audit(who, "retention.expire", run_id, detail)
    deleted: Counter[str] = Counter()
    async with ctx.pool.connection() as conn:
        span = await repo.key_span(conn, run_id, cycle)
    if span is not None:
        for lo in range(span[0], span[1] + 1, CHUNK):
            async with ctx.pool.connection() as conn:
                deleted.update(await repo.purge_keys(conn, run_id, cycle, lo, lo + CHUNK))
    async with repo.audited(ctx.pool) as (conn, audit):
        ended = await repo.finish_purge(conn, run_id, cycle, who.actor)
        detail = {"cycle": cycle} | {n: deleted[n] for n in ("keys", "observations", "results", "findings")}
        detail |= {"chat_messages": ended["chat_messages"], "blobs_queued": deleted["blobs_queued"]}
        detail |= {"data_key_destroyed": ended["data_key_destroyed"], "packs": ended["packs"]}
        await audit(who, "retention.purged", run_id, detail)
    if ended["data_key_destroyed"]:
        ctx.forget(run_id)
    done.cycles += 1
    done.runs.add(run_id)
    done.keys += deleted["keys"]
    done.blobs_queued += deleted["blobs_queued"]


async def _drain(ctx: Context, done: Summary) -> None:
    """Delete the blobs queued over 24 h ago that no observation names, ``DRAIN`` per transaction under
    the exclusive blob lock; the others stay (shared), and every drained address leaves the queue."""
    while True:
        async with ctx.pool.connection() as conn:
            await repo.lock_blobs(conn, exclusive=True)
            queued = await repo.gc_due(conn, DRAIN)
            kept = await repo.referenced(conn, queued)
            for address in queued:
                if address not in kept and (freed := await asyncio.to_thread(ctx.evidence.delete, address)):
                    done.blobs_deleted += 1
                    done.bytes += freed
            done.blobs_shared += len(kept)
            await repo.gc_done(conn, queued)
        if len(queued) < DRAIN:
            return


def _aged(evidence: EvidenceStore) -> Iterator[str]:
    for address, _, _ in evidence.walk():
        if (age := evidence.age(address)) is not None and age > ORPHAN_AGE_S:
            yield address


async def _orphans(ctx: Context) -> int:
    """Delete the blob files over an hour old that no observation names and the queue does not hold,
    ``DRAIN`` per transaction under the exclusive blob lock, and the stale temporary files; how many."""
    deleted = 0
    found = _aged(ctx.evidence)
    while batch := await asyncio.to_thread(lambda: list(itertools.islice(found, DRAIN))):
        async with ctx.pool.connection() as conn:
            await repo.lock_blobs(conn, exclusive=True)
            kept = await repo.referenced(conn, batch) | await repo.queued_blobs(conn, batch)
            for address in batch:
                if address not in kept and await asyncio.to_thread(ctx.evidence.delete, address):
                    deleted += 1
    return deleted + await asyncio.to_thread(ctx.evidence.delete_stale_temps, ORPHAN_AGE_S)


async def _prechecks(ctx: Context) -> int:
    """Delete the table pre-checks made over ``retention.standard_days`` ago with their pairs,
    ``PRECHECKS`` per transaction; how many."""
    deleted = 0
    while True:
        async with ctx.pool.connection() as conn:
            batch = await repo.prune_prechecks(conn, ctx.settings.retention.standard_days, PRECHECKS)
        deleted += batch
        if batch < PRECHECKS:
            return deleted


async def _finished(ctx: Context, who: Principal, done: Summary) -> None:
    """Audit ``retention.finished`` when the pass purged, deleted, drained or abandoned anything (held
    cycles alone would add a row every hour of a hold)."""
    did = done.cycles or done.blobs_deleted or done.blobs_shared or done.orphans or done.abandoned
    if did or done.prechecks:
        async with repo.audited(ctx.pool) as (_, audit):
            await audit(who, "retention.finished", "purge", done.finished())


# -- erasure ------------------------------------------------------------------------------------------


async def erase(ctx: Context, who: Principal, run_id: str, text: str, cycle: int | None = None) -> Summary:
    """Purge one ended ``cycle`` of the run, or the whole run (cancelled first if live), now, for the
    reason ``erasure: <text>``. A refusal (ValueError) writes no retention row. PurgeBusy while another
    purger runs."""
    reason = f"{ERASURE}: {text}"
    async with _sole(ctx):
        async with ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            if run["hold_reason"] is not None:
                raise ValueError(f"run {run_id} is held")
            await _unkeyed(ctx, conn, run, text)
            if cycle is not None:
                await _erasable(conn, run, cycle)
        if cycle is None:
            await _end_for_erasure(ctx, who, run)
            async with ctx.pool.connection() as conn:
                rows = await repo.list_cycles(conn, run_id, **days(ctx))
            cycles = [row["cycle"] for row in rows if row["purged_at"] is None]
        else:
            cycles = [cycle]
        done = Summary()
        for number in cycles:
            await _purge_cycle(ctx, who, run_id, number, reason, done)
        await _finished(ctx, who, done)
    return done


def _in_flight(run_id: str) -> ValueError:
    return ValueError(f"run {run_id} still has keys in flight; try again in 60 s")


async def _erasable(conn: repo.Conn, run: repo.Row, cycle: int) -> None:
    """Refuse the erasure of a cycle that has not ended, was purged, is still read by the live run, or
    has keys a worker still holds."""
    run_id = run["run_id"]
    row = await repo.get_cycle(conn, run_id, cycle)
    if row is None:
        raise ValueError(f"cycle {cycle} of run {run_id} has not ended")
    if row["purged_at"] is not None:
        raise ValueError(f"cycle {cycle} of run {run_id} was purged on {_purged_on(row)}")
    if run["status"] in _LIVE and cycle >= run["cycle"] - 1:
        raise ValueError(
            f"cycle {cycle} of run {run_id} is still read by the running soak: erase the whole run"
        )
    if await repo.keys_in_flight(conn, run_id, cycle):
        raise _in_flight(run_id)


async def _end_for_erasure(ctx: Context, who: Principal, run: repo.Row) -> None:
    """Cancel a live run and end its cycle now, unsummarised: its data is erased next, and ending it so
    needs no key (a drawn cycle is judged from stored rows first). Refused, changing nothing, while a worker
    still holds one of the run's keys."""
    run_id = run["run_id"]
    async with ctx.pool.connection() as conn:
        if await repo.keys_in_flight(conn, run_id):
            raise _in_flight(run_id)
    if run["status"] in ("queued", "running", "paused"):
        await stop_run(ctx, run_id, ERASURE, who=who)
    async with repo.audited(ctx.pool) as (conn, audit):
        if await repo.keys_in_flight(conn, run_id):  # leased meanwhile: the sweep ends it cancelled
            raise _in_flight(run_id)
        if await repo.set_run_status(conn, run_id, "cancelled", when=("cancelling",)):
            await repo.cancel_keys(conn, run_id)
            stopped: repo.Row = await repo.get_run(conn, run_id)  # type: ignore[assignment]
            _, judgement = await sample.ending(ctx, conn, stopped)
            await repo.end_cycle(conn, run_id, sample=judgement)
            await audit(
                who, "run.finished", run_id, {"status": "cancelled", "reason": stopped["stop_reason"]}
            )


async def _unkeyed(ctx: Context, conn: repo.Conn, run: repo.Row, text: str) -> None:
    """Refuse a hold's or an erasure's reason holding a raw key-shaped value of the run's spec: audit rows,
    and the evidence packs citing them, carry it forever."""
    if rawvalues.key_shaped(await ctx.spec(conn, run["spec_id"]), text):
        raise ValueError(rawvalues.KEY_SHAPED)


# -- holds and the dry run ----------------------------------------------------------------------------


async def hold(ctx: Context, who: Principal, run_id: str, text: str | None) -> None:
    """Hold the run's evidence past its retention with the reason ``text`` (audited
    ``retention.hold``), or release it (None; ``retention.release``)."""
    async with repo.audited(ctx.pool) as (conn, audit):
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        if text is not None:
            await _unkeyed(ctx, conn, run, text)
        before = await repo.set_hold(conn, run_id, text, None if text is None else who.actor)
        if before is None:
            raise ValueError(f"run {run_id} is {'already' if text is not None else 'not'} held")
        if text is None:
            await audit(who, "retention.release", run_id, {"reason": before["hold_reason"]})
        else:
            await audit(who, "retention.hold", run_id, {"reason": text})


async def preview(ctx: Context, *, limit: int | None = None) -> dict[str, list[repo.Row]]:
    """What a pass would do, writing nothing: the ``due`` cycles, the ``kept`` ones past their retention
    (held, or read by a live soak) and the runs it would ``abandon``."""
    async with ctx.pool.connection() as conn:
        return {
            "abandon": await repo.abandoned_runs(conn, **days(ctx)),
            "due": await repo.due_cycles(conn, **days(ctx), limit=limit),
            "kept": await repo.kept_cycles(conn, **days(ctx)),
        }
