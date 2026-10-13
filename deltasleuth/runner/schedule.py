"""Firing schedules (docs/mvp/SCHEDULES.md §4, §5): the worker starts a run at each due time, or records
why it did not.

``fire`` judges one schedule's due times against the clock (``decide``, pure): only the newest is ever
started, and only inside its window, which closes ``start_within`` minutes after it or at the next due
time; the older ones, and a newest whose window has closed, are settled as missed in one transaction. A
firing is ``create_run`` as ``system:worker``, nothing more: the four-eyes gate, the licence and a sample's
refusals are the run's own, and each refusal becomes a closed reason, never its message. The run, the
schedule's advance and their audit rows commit together, so a kill, a timeout or a lost race leaves the
time due. A reason that may pass (a live run of the schedule, a listing that failed, an export a file side
reads that is not ready: docs/mvp/FILES.md §12) is tried again while the window is open (``Retry``); a
standing one (the auth mode, who saved it, the approval, the licence, an unclosed draw) chains its first
time, then holds its later times as one range, chained within 24 hours. A
cron or zone this build cannot load pauses the schedule (T6); ``rezone`` recomputes each active schedule's
next due time once a worker takes the lock, so an upgrade's zone rules apply at the restart.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

import psycopg

from deltasleuth import approvals, licence, schedules
from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors.base import ConnectorError
from deltasleuth.connectors.tables import NotReady, TableError
from deltasleuth.context import Context
from deltasleuth.core import cron
from deltasleuth.core.spec.errors import SpecError
from deltasleuth.runner import sample
from deltasleuth.runner.cycle import failure
from deltasleuth.runner.runs import Firing, ScheduleMoved, SetterGone, create_run
from deltasleuth.store import repo

RETRY_S = 60  # a time waiting for a live run to end is tried again this often
RETRY_SLOW_S = 300  # and one whose run could not be created, or whose firing met a fault
TRIES = 3  # attempts to create a time's run
HELD = timedelta(hours=24)  # a standing refusal's range is chained this long after its first time
STANDING = (("mode", None), ("setter", None), ("approval", None), ("sample", "unclosed"))  # and the licence
# What a run that could not be created raises, a bug aside: bad input, an unknown spec or target, a side or
# the store that cannot be read.
NOT_STARTED = (
    ValueError, LookupError, SpecError, ConnectorError, TableError, psycopg.OperationalError, TimeoutError,
)  # fmt: skip
log = logging.getLogger("deltasleuth.schedules")
Why = tuple[str, str | None, dict[str, Any]]  # a closed reason, its code, and the ids, type or mode it names
Queued = tuple[datetime, datetime]  # a due time the worker left without a slot, and the database's time then


@dataclass(frozen=True)
class Retry:
    """A due time (``due``) left due: tried again after ``seconds``; ``tried`` attempts to create its run
    failed."""

    seconds: float
    tried: int
    due: datetime


@dataclass(frozen=True)
class Due:
    """A schedule's due times at an instant: the ``newest``, the ``older`` ones, when the newest's window
    ``closes`` and whether it has, and the time due ``next`` (None: the schedule ends)."""

    newest: datetime
    older: list[datetime]
    closes: datetime
    closed: bool
    next: datetime | None


def decide(row: repo.Row, now: datetime) -> Due | None:
    """The due times of the schedule ``row`` at ``now``: every cron time after its anchor, up to ``now``
    and its ``ends_at``; None when there is none."""
    until = now if row["ends_at"] is None else min(now, row["ends_at"])
    times = cron.between(row["cron"], row["time_zone"], row["anchor_at"], until)
    if not times:
        return None
    after = cron.after(row["cron"], row["time_zone"], times[-1], until=row["ends_at"])
    closes = times[-1] + timedelta(minutes=row["start_within"])
    closes = closes if after is None else min(closes, after)
    return Due(times[-1], times[:-1], closes, now >= closes, after)


def _unloadable(row: repo.Row) -> str | None:
    """T6: ``zone`` or ``cron`` when this build cannot load the schedule's; else None."""
    try:
        cron.zone(row["time_zone"])
    except cron.CronError:
        return "zone"
    try:
        cron.parse(row["cron"])
    except cron.CronError:
        return "cron"
    return None


async def fire(
    ctx: Context,
    schedule_id: str,
    *,
    now: datetime | None = None,
    queued: Queued | None = None,
    tried: int = 0,
) -> Retry | None:
    """Start the run of the schedule's newest due time, or settle why not; ``now``: the database's (tests
    inject it); ``queued``: the due time this worker left without a slot, and when; ``tried``: the attempts
    to create the run of the schedule's next due time that failed (a newer time's count starts at none). A
    ``Retry`` when the time stays due, to be tried again; else None."""
    async with ctx.pool.connection() as conn:
        row = await repo.get_schedule(conn, schedule_id)
    if row is None or row["state"] == "deleted":
        return None
    now, read = now or row["now"], monotonic()
    if row["next_due"] is None or row["next_due"] > now:
        if row["held"] is not None and now - datetime.fromisoformat(row["held"]["due"]) >= HELD:
            await _chain(ctx, row)
        return None
    if (code := _unloadable(row)) is not None:
        await _pause(ctx, row, code, now)
        return None
    if await _rezone(ctx, row) or (found := decide(row, now)) is None:
        return None
    if found.older or found.closed:
        row = await _late(ctx, row, found, queued)  # type: ignore[assignment]
        if row is None or found.closed:
            return None
        tried = 0  # the count was an older time's
    window, final = asyncio.timeout((found.closes - now).total_seconds()), False
    try:
        async with ctx.pool.connection() as conn:
            why = await _refused(ctx, conn, row)
        if why is None:
            firing = Firing(
                schedule_id, found.newest, row["revision"], row["time_zone"], row["set_by"], found.closes,
                found.next, row["held"],
            )  # fmt: skip
            run = row["run"]
            async with window:  # no listing outlives its window
                run_id = await create_run(
                    ctx, row["runs_spec_id"], run["mode"], soak=run.get("soak"), who=WORKER, schedule=firing
                )
            log.info("schedule %s started run %s for %s", schedule_id, run_id, cron.utc(found.newest))
            return None
    except Exception as exc:
        why = await _reason(ctx, exc)
        final = isinstance(exc, ScheduleMoved) or window.expired()  # its window closed at the commit
    reason, code, _ = why
    passing = _passing(reason, code)
    wait = RETRY_S if passing else RETRY_SLOW_S if reason == "not_started" else None
    tried += reason == "not_started" and not passing  # an export not ready is waited for, not tried 3 times
    now += timedelta(seconds=monotonic() - read)  # the clock after the attempt, however long it took
    again = timedelta(seconds=(wait or 0) + ctx.settings.worker.poll_seconds)  # the wait, then a sweep
    if wait and not final and tried < TRIES and now + again < found.closes:
        return Retry(wait, tried, found.newest)  # a try that waits writes nothing
    await _miss(ctx, row, found, now, why)  # no other try fits in the window: its reason, at once
    return None


def _passing(reason: str, code: str | None) -> bool:
    """A refusal that ends by itself: a live run of the schedule, or of its sample's population, or an
    export a file side reads that is not ready yet (docs/mvp/FILES.md §12: ``not_started`` with its code)."""
    return (
        reason == "overlap"
        or (reason, code) == ("sample", "live")
        or (reason == "not_started" and code is not None)
    )


async def _refused(ctx: Context, conn: repo.Conn, row: repo.Row) -> Why | None:
    """Step 3: why the time is not started before a run is created, the first that holds; None: none."""
    if row["auth_mode"] != ctx.settings.auth_mode:
        return "mode", None, {"worker_mode": ctx.settings.auth_mode}
    if not await repo.setter_live(conn, row["set_by"]):
        return "setter", None, {}
    if row["last_run_id"] is not None and row["last_run_status"] not in repo.TERMINAL:
        return "overlap", None, {"run_id": row["last_run_id"]}
    if row["runs_spec_id"] is None:
        raise LookupError(f"no spec is named {row['spec_name']}")
    spec = await ctx.spec(conn, row["runs_spec_id"])
    if spec.sample is not None and spec.sample.method == "bottom_k":  # SAMPLING D7, unattended
        for draw in await repo.ledger(conn, spec.sample.population):
            if sample.blocking(draw):
                return "sample", "unclosed", {"draw_id": draw["draw_id"]}
    return None


async def _reason(ctx: Context, exc: Exception) -> Why:
    """Step 5: what kept a run from being created, as a closed reason: never its message."""
    kind = {"error_type": type(exc).__name__}
    if isinstance(exc, ScheduleMoved):  # judged once more as the miss is settled: unchanged, it is late
        return "not_started", None, {"error_type": "TimeoutError"}
    if isinstance(exc, SetterGone):
        return "setter", None, {}
    if isinstance(exc, approvals.ApprovalRequired):
        return "approval", None, {}
    if isinstance(exc, licence.LicenceRefused):
        async with ctx.pool.connection() as conn:
            current = await licence.standing(ctx, conn)
        return (
            "licence",
            "expired" if current.state == "expired" else "sso" if current.sso_fenced else "pairs",
            {},
        )
    if isinstance(exc, sample.SampleRefused):
        return "sample", "live" if exc.status == 409 else "refused", {}
    if isinstance(exc, NotReady):  # before ValueError: its closed code, tried until the window closes
        return "not_started", exc.code, kind
    if (
        isinstance(exc, psycopg.errors.UniqueViolation)
        and exc.diag.constraint_name == "runs_schedule_due_idx"
    ):
        return "late", "fired_already", {}
    if isinstance(exc, KeyError | IndexError) or not isinstance(exc, NOT_STARTED):  # a bug
        return "error", None, kind
    return "not_started", None, kind


async def _settle(
    ctx: Context,
    row: repo.Row,
    chain: list[dict[str, Any]],
    last: dict[str, Any],
    *,
    anchor: datetime,
    next_due: datetime | None,
    held: dict[str, Any] | None = None,
) -> bool:
    """Settle the schedule's due times up to ``anchor`` as missed in one transaction whose last statements
    are its audit rows: ``chain``, each a ``schedule.missed`` detail; ``last`` is what the schedule shows
    and ``held`` the range it holds unchained. False, nothing written, when it moved meanwhile."""
    schedule_id = row["schedule_id"]
    async with repo.audited(ctx.pool) as (conn, audit):
        moved = await repo.settle_schedule(
            conn, schedule_id, due=row["next_due"], revision=row["revision"], anchor=anchor,
            next_due=next_due, last=last | {"outcome": "missed"}, held=held,
        )  # fmt: skip
        if moved is None:
            return False
        for detail in chain:
            await audit(WORKER, "schedule.missed", schedule_id, detail)
    said = ": ".join(filter(None, [last["reason"], last["code"] or last["error_type"]]))
    log.warning("schedule %s did not start a run for %s (%s)", schedule_id, cron.utc(anchor), said)
    return True


async def _late(ctx: Context, row: repo.Row, found: Due, queued: Queued | None) -> repo.Row | None:
    """Step 2: settle the older due times, and the newest once its window has closed, as ``late``: counted
    ``down`` ranges, but the time this worker left without a slot while its window was open, which is its
    own row, ``busy``. Each row's ``next`` is the time after its last. The schedule as it then stands, None
    when it moved meanwhile."""
    times = [*found.older, found.newest]
    within = timedelta(minutes=row["start_within"])
    closes = [min(at + within, later) for at, later in itertools.pairwise(times)] + [found.closes]
    late = times if found.closed else found.older
    after = [*late[1:], found.next if found.closed else found.newest]  # the time after each
    busy = len(late)  # the place of the time left without a slot while its window was open: none
    if queued is not None and queued[0] in late and queued[1] < closes[late.index(queued[0])]:
        busy = late.index(queued[0])
    chain = [row["held"]] if row["held"] is not None else []
    for first, last in ((0, busy), (busy, busy + 1), (busy + 1, len(late))):
        span, then = late[first:last], after[min(last, len(late)) - 1]
        if span and first == busy:
            chain.append(schedules.missed(row, "late", "busy", due=span[0], next=then))
        elif span:
            chain.append(
                schedules.missed(row, "late", "down", due=span[0], to=span[-1], count=len(span), next=then)
            )
    anchor = late[-1]
    if not await _settle(ctx, row, chain, chain[-1], anchor=anchor, next_due=after[-1]):
        return None
    return row | {
        "anchor_at": anchor,
        "next_due": after[-1],
        "held": None,
        "last": chain[-1] | {"outcome": "missed"},
    }


async def _miss(ctx: Context, row: repo.Row, found: Due, now: datetime, why: Why) -> None:
    """Settle the newest due time as missed for ``why``. A standing reason chains its first time; each
    later consecutive one joins the range the schedule holds, chained once its first time is 24 hours old
    or another outcome, or a person's write, follows (step 7)."""
    reason, code, facts = why
    detail = schedules.missed(row, reason, code, due=found.newest, next=found.next, **facts)
    held, last = row["held"], row["last"] or {}
    was = (last.get("outcome"), last.get("reason"), last.get("code"), last.get("revision"))
    standing = reason == "licence" or (reason, code) in STANDING
    if not (standing and was == ("missed", reason, code, row["revision"])):
        chain = [detail] if held is None else [held, detail]
        await _settle(ctx, row, chain, detail, anchor=found.newest, next_due=found.next)
        return
    chain = []
    if held is not None and now - datetime.fromisoformat(held["due"]) >= HELD:
        chain, held = [held], None
    first = found.newest if held is None else datetime.fromisoformat(held["due"])
    count = 1 if held is None else held["count"] + 1
    held = schedules.missed(
        row, reason, code, due=first, to=found.newest, count=count, next=found.next, **facts
    )
    await _settle(ctx, row, chain, held, anchor=found.newest, next_due=found.next, held=held)


async def _chain(ctx: Context, row: repo.Row) -> None:
    """Chain the range the schedule holds, its first time 24 hours old."""
    async with repo.audited(ctx.pool) as (conn, audit):
        if await repo.release_held(conn, row["schedule_id"], row["held"]):
            await audit(WORKER, "schedule.missed", row["schedule_id"], row["held"])


async def _pause(ctx: Context, row: repo.Row, code: str, now: datetime) -> None:
    """T6: pause the schedule whose zone or cron this build cannot load, as ``system:worker``, and chain
    the time it leaves unstarted if that time is due at ``now`` (a time not yet due is no missed time: the
    pause's own row says why, ``missed: false``, and is what the notifier then sends, matched by the spec's
    name and the mode it holds), in one transaction; a ``PUT`` that validates, then a resume, clears it."""
    schedule_id, data = row["schedule_id"], cron.tzdata()
    async with repo.audited(ctx.pool) as (conn, audit):
        locked = await repo.get_schedule(conn, schedule_id, lock=True)
        if locked is None or (locked["revision"], locked["next_due"]) != (row["revision"], row["next_due"]):
            return  # written or settled meanwhile
        detail = None
        if locked["next_due"] <= now:
            detail = schedules.missed(locked, "zone", code, due=locked["next_due"], tzdata=data)
        await repo.pause_schedule(
            conn, schedule_id, WORKER.actor, revision=locked["revision"],
            last=detail and detail | {"outcome": "missed"},
        )  # fmt: skip
        for chained in (locked["held"], detail):
            if chained is not None:
                await audit(WORKER, "schedule.missed", schedule_id, chained)
        paused = {
            "revision": locked["revision"], "next_due": None, "reason": "zone", "code": code, "tzdata": data,
            "missed": detail is not None, "spec_name": locked["spec_name"], "mode": locked["run"]["mode"],
        }  # fmt: skip
        await audit(WORKER, "schedule.pause", schedule_id, paused)
    log.warning("schedule %s paused: this build cannot load its %s", schedule_id, code)


async def _rezone(ctx: Context, row: repo.Row) -> bool:
    """Move the schedule's next due time to what this build's tz data makes of its cron after its anchor,
    audited ``schedule.rezone``; False when it stands."""
    was = row["next_due"]
    to = cron.after(row["cron"], row["time_zone"], row["anchor_at"], until=row["ends_at"])
    if to == was:
        return False
    async with repo.audited(ctx.pool) as (conn, audit):
        if await repo.rezone_schedule(conn, row["schedule_id"], row["revision"], was, to):
            detail = {"from": cron.utc(was), "to": to and cron.utc(to), "tzdata": cron.tzdata()}
            await audit(WORKER, "schedule.rezone", row["schedule_id"], detail)
    return True


async def rezone(ctx: Context) -> bool:
    """Recompute each active schedule's next due time from its anchor (§4), each guarded: one this build
    cannot load is paused (T6), any other fault logged and the schedule left. False when the schedules
    could not be read: the next sweep tries again. Never raises but its cancellation."""
    try:
        async with ctx.pool.connection() as conn:
            rows = await repo.pages(repo.list_schedules, conn, state="active")
    except Exception as exc:
        log.warning("schedules not rezoned: %s", failure(exc))
        return False
    for row in rows:
        try:
            if (code := _unloadable(row)) is not None:
                await _pause(ctx, row, code, row["now"])
            else:
                await _rezone(ctx, row)
        except Exception as exc:
            log.warning("schedule %s not rezoned: %s", row["schedule_id"], failure(exc))
    return True
