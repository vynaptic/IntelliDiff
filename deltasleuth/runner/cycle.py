"""The end of a cycle: root causes and counters from the stored results, then the run's next step.

The worker calls ``finish_cycle`` for every running or cancelling run; the first to find the cycle done
takes the run's row lock and ends it, the others find it ended. A soak's next cycle is due
``execution.cycleInterval`` after the previous one's nominal start, or after the boundary when that start has
passed, so the starts a late soak missed are skipped (the same keys, or re-resolved when ``soak.rolling``)
unless a stop condition ends the soak at the boundary; any other run gets its final status. A rolling
listing or key table is read before the row lock is taken, and when it fails the soak goes on with the same
keys: an outage at a boundary never ends a long soak. A key table whose target refuses the
credential is raised instead (``KeyAuthRefused``): the worker pauses the run. A soak over two tables opens
its next cycle with a fresh scan, due at the cycle's start. The run's final status is audited as
``run.finished``, and each soak cycle that starts as ``run.cycle``, by whoever ended the cycle. Every ended
cycle, a failed run's too, gets its ``ds.cycles`` row: its retention runs from then, and a sign-off judges
it by the approval it ran under and how maxKeys cut its keys. Under SSO a soak without the approval its
version needs attaches the active one at a boundary, or starts its next cycle paused; so does a soak the
licence does not allow, or whose name it does not seat, as of when that cycle falls due
(``licence.stopped``). Every end of a drawn cycle, a failed run's too, stores its sample's judgement
(``runner.sample``); a sampled soak's next cycle opens with no keys and its own draw pending, paused when
the ending cycle's ``bottom_k`` draw did not pass. A cycle end on a store whose tables were never analysed
analyses them first (``repo.analyse_new``), so the planner joins its rows by key; the root causes it then
links are analysed with them (``repo.replace_root_causes``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from deltasleuth import approvals, licence
from deltasleuth.auth.principal import WORKER, Principal
from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.connectors.tables import TableError, shown
from deltasleuth.connectors.tables.reader import passed
from deltasleuth.context import Capabilities, Context
from deltasleuth.core.classify import rootcause
from deltasleuth.core.secrets import SecretError
from deltasleuth.core.spec import parse_duration
from deltasleuth.core.types import CompiledSpec, Finding, Verdict
from deltasleuth.errors import ours, told
from deltasleuth.runner import census, sample
from deltasleuth.runner.compare import rederive_all
from deltasleuth.runner.counters import Tally
from deltasleuth.runner.keys import KeyAuthRefused, keys_cut, resolve, run_keys
from deltasleuth.runner.recheck import INTERMITTENT_B, RecheckPolicy
from deltasleuth.runner.scan import new_scan
from deltasleuth.runner.stop import StopConditions
from deltasleuth.security.crypto import MaskedAlike
from deltasleuth.security.tokenise import mask_key, mask_pointer
from deltasleuth.store import repo

CANCELLED = "cancelled"  # the stop reason of a cancel asked for through the API
ERASURE = "erasure"  # an erasure's: its cycle ends cancelled, unsummarised
_ACTIVE = ("running", "cancelling")
FRAMES = 3  # the innermost frames of this package a failure names
_PACKAGE = os.path.dirname(os.path.dirname(__file__))  # as a frame of one of its modules names its file
log = logging.getLogger(__name__)


def final_status(counters: dict[str, Any]) -> str:
    return "completed_with_issues" if counters["cancelled"] or counters["outstanding"] else "completed"


async def summarise(ctx: Context, conn: repo.Conn, spec: CompiledSpec, run: repo.Row) -> dict[str, Any]:
    """Save the cycle's root causes and counters; return the counters. The results are read, counted and
    their discrepant keys derived again a page at a time; of those keys only each finding's grouping facts
    (``rootcause.Member``) are kept, never their documents or values."""
    run_id, cycle, caps = run["run_id"], run["cycle"], ctx.capabilities(run)
    live, due = run["params"].get("samples_dir") is None, RecheckPolicy.from_spec(spec.raw).outcomes
    tally = Tally(live=live, recheck_outcomes=due)
    grouped: list[rootcause.Member] = []
    async for results, found in repo.result_pages(conn, run_id, cycle):
        discrepant = []
        for r in results:
            fs = found.get(r["key_masked"], [])
            # Root causes group discrepant findings only, so only discrepant results are re-derived.
            if tally.add(r, fs) is Verdict.DIFFERENT:
                discrepant.append((r, fs))
        grouped += await _members(ctx, conn, spec, run_id, discrepant, caps)
    groups = await asyncio.to_thread(rootcause.group_members, grouped)
    links = {m.ref: rc.root_cause_id for rc, ms in groups for m in ms if m.ref is not None}
    await repo.replace_root_causes(conn, run_id, cycle, [rc for rc, _ in groups], links)
    states = await repo.cycle_counts(conn, run_id, cycle)
    bulk = (run["scan"] or {}).get("matched", 0)  # a table scan's identical rows
    counters = tally.counters(total=sum(states["keys"].values()) + bulk, bulk_match=bulk)
    await repo.set_counters(conn, run_id, counters | {"cycle": cycle})
    return counters


async def _members(
    ctx: Context,
    conn: repo.Conn,
    spec: CompiledSpec,
    run_id: str,
    discrepant: list[tuple[repo.Row, list[repo.Row]]],
    caps: Capabilities,
) -> list[rootcause.Member]:
    """One page's discrepant (result, stored findings), derived again from their evidence, as members of
    root causes. A member's ``ref`` is its stored finding's id (known by its masked key and pointer), or
    None. Keys whose stored findings a recheck tagged intermittent are tagged again."""
    if not discrepant:
        return []
    tagged = {r["key_masked"] for r, fs in discrepant if any(f["value_class"] == INTERMITTENT_B for f in fs)}
    derived = await rederive_all(ctx, conn, spec, run_id, [r for r, _ in discrepant], caps, tagged)
    ids = {
        (f["key_masked"], f["json_pointer"], f["change_type"]): f["finding_id"]
        for _, fs in discrepant
        for f in fs
    }
    tok = ctx.tokeniser

    def stored(f: Finding) -> str | None:
        pointer = repo.storable(mask_pointer(f.pointer_parts, spec, tok, key=f.key))
        return ids.get((mask_key(f.key, spec, tok), pointer, f.change_type.value))

    def read() -> list[rootcause.Member]:
        return [m for r in derived for m in rootcause.members(r, spec=spec, tokeniser=tok, ref=stored)]

    return await asyncio.to_thread(read)


async def finish_cycle(
    ctx: Context, run_id: str, *, who: Principal, ended_at: datetime | None = None
) -> None:
    """End the run's current cycle if all its keys are done or cancelled (idempotent), recording its end
    (``ended_at``, default now) for retention."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if (
            run is None
            or run["status"] not in _ACTIVE
            or not await repo.cycle_done(conn, run_id, run["cycle"])
        ):
            return
        spec = await ctx.spec(conn, run["spec_id"])
        await repo.analyse_new(conn)  # a new store's first cycle end, before summarise joins its rows
    soak = run["status"] == "running" and run["mode"] == "soak"
    next_at, reason = _boundary(spec, run) if soak else (None, None)
    going_on = soak and reason is None
    rescan = run["scan"] is not None  # a soak over two tables scans them again
    keys, cut, walk = None, None, None  # a new key set (None: the same), how maxKeys cut it, its listing
    if going_on:
        keys, cut, walk = ({}, None, None) if rescan else await _rolled(ctx, spec, run)
    attach = going_on and _unapproved(ctx, spec, run)
    stopped = await licence.stopped(ctx, run, spec, next_at) if going_on else None  # its reason wins
    async with repo.audited(ctx.pool) as (conn, audit):
        # The approval is read FOR SHARE before the run's row lock.
        attached = await _attached(ctx, conn, spec, run) if attach else None
        # Takes the run's row lock: a concurrent ender waits here, then finds the cycle ended.
        if not await repo.set_run_status(conn, run_id, run["status"], when=[run["status"]]):
            return
        latest = await repo.get_run(conn, run_id)
        if latest["cycle"] != run["cycle"]:
            return
        erased = latest["stop_reason"] == ERASURE  # its data is erased next: ending it so needs no key
        counters = {} if erased else await summarise(ctx, conn, spec, latest)  # before the audit row
        # A drawn cycle is judged from stored rows: an erasure's too, before its data goes.
        drawn, judgement = await sample.ending(ctx, conn, latest, spec)
        await repo.end_cycle(conn, run_id, ended_at, sample=judgement)
        if run["status"] == "cancelling":
            cancelled = latest["stop_reason"] in (CANCELLED, ERASURE)
            ended = "cancelled" if cancelled else "completed_with_issues"
            await repo.set_run_status(conn, run_id, ended)
            await audit(who, "run.finished", run_id, {"status": ended, "reason": latest["stop_reason"]})
        elif going_on:
            scan = new_scan() | {"due": next_at.isoformat()} if rescan else None
            sealed = None
            if keys is not None:  # off the event loop: a rolled key set may hold a million keys
                sealed = await asyncio.to_thread((await ctx.sealer(conn, run_id)).seal_keys, keys)
            cycle = await repo.start_next_cycle(conn, run_id, next_at, sealed, scan)
            if keys is not None:  # rolled or rescanned: its own cut, or none
                await repo.set_keys_cut(conn, run_id, cut)
            if (listed := census.listed(spec, walk, cycle)) is not None:  # walked again: counted again
                await repo.set_census(conn, run_id, listed)
            if run["params"]["keys_source"] == "hash_sample":  # each cycle draws its own sample
                await repo.set_draw(conn, run_id, {"due": next_at.isoformat()})
            unpassed = drawn is not None and drawn["method"] == "bottom_k"
            unpassed = unpassed and (judgement or {}).get("status") != "passed"
            # It waits for the licence, an approval or the close of a draw that did not pass, then a resume.
            why = (
                licence.LICENCE_PAUSE if stopped is not None
                else approvals.APPROVAL_PAUSE if attach and attached is None
                else sample.SAMPLE_PAUSE if unpassed
                else None
            )  # fmt: skip
            if why is not None:
                await repo.set_run_status(conn, run_id, "paused", stop_reason=why)
            if attached is not None:
                await repo.set_run_approval(conn, run_id, attached)
            read = {"key_source": walk} if walk is not None and walk["basis"] == "key_table" else {}
            await audit(who, "run.cycle", run_id, {"cycle": cycle} | read)
            if why is not None:
                detail = {"reason": why, "approval_id": attached}
                if stopped is not None:
                    detail["licence"] = stopped  # the refusal as data: its message is logged
                if why == sample.SAMPLE_PAUSE:
                    detail["draw_id"] = drawn["draw_id"]  # type: ignore[index]
                await audit(WORKER, "paused", run_id, detail)
        else:
            status = final_status(counters)
            await repo.set_run_status(conn, run_id, status, stop_reason=reason)
            detail = {"status": status, "reason": reason or latest["stop_reason"]}  # a set reason is kept
            await audit(who, "run.finished", run_id, detail)


def due(spec: CompiledSpec, run: repo.Row, cycle: int) -> datetime:
    """When a soak's cycle ``cycle`` falls due, at the earliest: ``cycleInterval`` after the previous
    one's nominal start."""
    interval = timedelta(seconds=parse_duration(spec.raw["execution"]["cycleInterval"]))
    return run["started_at"] + interval * (cycle - 1)


def _boundary(spec: CompiledSpec, run: repo.Row) -> tuple[datetime, str | None]:
    """A soak's next cycle start and the stop condition (if any) that ends the soak there instead. On time,
    the next cycle starts at its nominal start (``due``); once that has passed (a pause, a worker outage, an
    overrun), ``cycleInterval`` after now: the starts it missed are skipped, never run back to back."""
    nominal, now = due(spec, run, run["cycle"] + 1), datetime.now(UTC)
    interval = timedelta(seconds=parse_duration(spec.raw["execution"]["cycleInterval"]))
    next_at = nominal if nominal >= now else now + interval
    return next_at, StopConditions.from_spec(spec.raw).at_boundary(run["cycle"], run["started_at"], next_at)


async def _rolled(
    ctx: Context, spec: CompiledSpec, run: repo.Row
) -> tuple[dict[str, str] | None, dict[str, int] | None, dict[str, Any] | None]:
    """A rolling soak's key set, read again, how maxKeys cut it and its read's record (a listing's census,
    a key table's read); ``(None, None, None)`` keeps the previous cycle's keys and cut, as does a failed
    read. A key table's read holds the worker's sweep, so the cycle interval bounds it, an hour at most; one
    whose target refuses the credential is raised, never tried again in a loop."""
    params = run["params"]
    if params["keys_source"] == "hash_sample":  # no keys: its next cycle draws its own sample
        return {}, None, None
    if not params["soak"]["rolling"] or params["keys_source"] == "explicit":
        return None, None, None
    interval = parse_duration(spec.raw["execution"]["cycleInterval"])
    try:
        resolved, walk = await resolve(ctx, spec, bound_s=min(census.TIMEOUT_S, interval or census.TIMEOUT_S))
    except KeyAuthRefused:
        raise
    except ValueError as exc:  # a refusal's message names positions and statuses, never keys
        # a refused connect's brief names no host and no account; a fault is told by its type alone
        error = getattr(exc, "brief", None) or told(exc)
        log.warning("run %s keeps its key set: %s", run["run_id"], error if ours(exc) else located(exc))
        async with repo.audited(ctx.pool) as (_, audit):
            await audit(WORKER, "rolling_resolve_failed", run["run_id"], {"error": error})
        return None, None, None
    taken = await asyncio.to_thread(run_keys, resolved, spec, ctx.tokeniser)
    return taken, keys_cut(resolved, taken), walk


def _unapproved(ctx: Context, spec: CompiledSpec, run: repo.Row) -> bool:
    """Under SSO, a run whose version needs an approval, started without one (before 008, or in
    another mode)."""
    return ctx.settings.auth_mode == "oidc" and run["approval_id"] is None and bool(approvals.needs(spec))


async def _attached(ctx: Context, conn: repo.Conn, spec: CompiledSpec, run: repo.Row) -> str | None:
    """The active approval (read FOR SHARE) an unapproved soak attaches at its boundary, for its next
    cycle; None: it has none, and its next cycle starts paused."""
    try:
        approval_id, _ = await approvals.gate(ctx, conn, run["spec_id"], spec, lock=True)
    except approvals.ApprovalRequired:
        return None
    return approval_id


def failure(exc: BaseException) -> str:
    """What a failed run shows: an unreadable secret names its file (never its content), a table side's
    error its class and code (``tables.shown``), the SSRF guard's its text, an address, and keys that mask
    alike their count; anything else only its type (a message may hold keys or values)."""
    if isinstance(exc, TableError):
        return shown(exc)
    if isinstance(exc, UnsafeTargetError | MaskedAlike):
        return f"{type(exc).__name__}: {exc}"
    return str(exc) if isinstance(exc, SecretError) else type(exc).__name__


def where(exc: BaseException) -> str:
    """Where ``exc`` was raised: the innermost ``FRAMES`` frames of this package its traceback passed, the
    innermost first, each as its module's path in the package, its line and its function; empty when it
    passed none. Nothing a frame holds is read: no message, local or argument."""
    return ", from ".join(
        f"{file[len(_PACKAGE) + 1 :].replace(os.sep, '/')}:{line} in {function}"
        for file, function, line in passed(exc, _PACKAGE)[:FRAMES]
    )


def located(exc: BaseException) -> str:
    """What a log line says of a failure: ``failure(exc)``, then where it was raised (``where``)."""
    at = where(exc)
    return f"{failure(exc)} at {at}" if at else failure(exc)


def recorded(exc: BaseException) -> str:
    """What a failed run stores as its error: ``failure(exc)``, with where it was raised when that is its
    type alone, so that a bug is found by its file and line. A table side's code, the guard's text and a
    secret's file say what to put right as they are."""
    said = failure(exc)
    return located(exc) if said == type(exc).__name__ else said


async def fail_run(ctx: Context, run_id: str, exc: BaseException, *, who: Principal) -> None:
    """End a run that cannot go on (a bug, lost evidence, an unreadable secret).

    Every caller cancels the keys it can: slots failing together each hand back their own key. A drawn
    cycle is judged first, on a connection of its own. The run's error is ``recorded(exc)``; its audit
    row's reason stays ``failure(exc)``, whose type a notification sends."""
    judgement = await sample.judgement(ctx, run_id)
    async with repo.audited(ctx.pool) as (conn, audit):
        when = ("queued", "running", "paused", "cancelling")
        moved = await repo.set_run_status(conn, run_id, "failed", when=when, error=recorded(exc))
        await repo.cancel_keys(conn, run_id)
        if moved:
            await repo.end_cycle(conn, run_id, sample=judgement)
            await audit(who, "run.finished", run_id, {"status": "failed", "reason": failure(exc)})
