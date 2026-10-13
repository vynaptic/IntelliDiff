"""The end of a cycle: root causes and counters from the stored results, then the run's next step.

The worker calls ``finish_cycle`` for every running or cancelling run; the first to find the cycle done
takes the run's row lock and ends it, the others find it ended. A soak's next cycle is due
``execution.cycleInterval`` after the previous one's nominal start (the same keys, or re-resolved when
``soak.rolling``) unless a stop condition ends the soak at the boundary; any other run gets its final
status. A rolling listing or key table is read before the row lock is taken, and when it fails the soak goes
on with the same keys: an outage at a boundary never ends a long soak. A key table whose target refuses the
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
from datetime import UTC, datetime, timedelta
from typing import Any

from deltasleuth import approvals, licence
from deltasleuth.auth.principal import WORKER, Principal
from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.connectors.tables import TableError
from deltasleuth.context import Context
from deltasleuth.core.classify import rootcause
from deltasleuth.core.secrets import SecretError
from deltasleuth.core.spec import parse_duration
from deltasleuth.core.types import CompiledSpec, Verdict
from deltasleuth.runner import census, sample
from deltasleuth.runner.compare import rederive_all
from deltasleuth.runner.counters import Tally
from deltasleuth.runner.keys import KeyAuthRefused, keys_cut, resolve, run_keys
from deltasleuth.runner.recheck import INTERMITTENT_B, RecheckPolicy
from deltasleuth.runner.scan import new_scan
from deltasleuth.runner.stop import StopConditions
from deltasleuth.security.tokenise import mask_key, mask_pointer
from deltasleuth.store import repo

CANCELLED = "cancelled"  # the stop reason of a cancel asked for through the API
ERASURE = "erasure"  # an erasure's: its cycle ends cancelled, unsummarised
_ACTIVE = ("running", "cancelling")
log = logging.getLogger(__name__)


def final_status(counters: dict[str, Any]) -> str:
    return "completed_with_issues" if counters["cancelled"] or counters["outstanding"] else "completed"


async def summarise(ctx: Context, conn: repo.Conn, spec: CompiledSpec, run: repo.Row) -> dict[str, Any]:
    """Save the cycle's root causes and counters; return the counters. The results are read and counted a
    page at a time; only the discrepant keys' results and findings are kept, for the root causes."""
    run_id, cycle, caps = run["run_id"], run["cycle"], ctx.capabilities(run)
    live, due = run["params"].get("samples_dir") is None, RecheckPolicy.from_spec(spec.raw).outcomes
    tally = Tally(live=live, recheck_outcomes=due)
    discrepant: list[repo.Row] = []
    findings: dict[str, list[repo.Row]] = {}
    async for results, found in repo.result_pages(conn, run_id, cycle):
        for r in results:
            fs = found.get(r["key_masked"], [])
            # Root causes group discrepant findings only, so only discrepant results are re-derived.
            if tally.add(r, fs) is Verdict.DIFFERENT:
                discrepant.append(r)
                findings[r["key_masked"]] = fs
    tagged = {
        r["key_masked"]
        for r in discrepant
        if any(f["value_class"] == INTERMITTENT_B for f in findings[r["key_masked"]])
    }
    derived = await rederive_all(ctx, conn, spec, run_id, discrepant, caps, tagged)
    groups = rootcause.groups(derived, spec=spec, tokeniser=ctx.tokeniser)
    causes = [rc for rc, _ in groups]
    await repo.replace_root_causes(conn, run_id, cycle, causes, _links(ctx, spec, groups, findings))
    states = await repo.cycle_counts(conn, run_id, cycle)
    bulk = (run["scan"] or {}).get("matched", 0)  # a table scan's identical rows
    counters = tally.counters(total=sum(states["keys"].values()) + bulk, bulk_match=bulk)
    await repo.set_counters(conn, run_id, counters | {"cycle": cycle})
    return counters


def _links(
    ctx: Context, spec: CompiledSpec, groups: list[rootcause.Group], findings: dict[str, list[repo.Row]]
) -> dict[str, str]:
    """finding_id -> root_cause_id; a stored finding is known by its masked key and pointer."""
    ids = {
        (f["key_masked"], f["json_pointer"], f["change_type"]): f["finding_id"]
        for fs in findings.values()
        for f in fs
    }
    links = {}
    for rc, fs in groups:
        for f in fs:
            key = mask_key(f.key, spec, ctx.tokeniser)
            pointer = repo.storable(mask_pointer(f.pointer_parts, spec, ctx.tokeniser, key=f.key))
            if finding_id := ids.get((key, pointer, f.change_type.value)):
                links[finding_id] = rc.root_cause_id
    return links


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
    """A soak's next cycle start and the stop condition (if any) that ends the soak there instead."""
    next_at = max(due(spec, run, run["cycle"] + 1), datetime.now(UTC))
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
    except ValueError as exc:  # its message names positions and statuses, never keys
        log.warning("run %s keeps its key set: %s", run["run_id"], exc)
        error = getattr(exc, "brief", None) or str(exc)  # a refused connect's: no host, no account
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
    error its class and code and the SSRF guard's its text, an address, anything
    else only its type (a message may hold keys or values)."""
    if isinstance(exc, TableError | UnsafeTargetError):
        return f"{type(exc).__name__}: {exc}"
    return str(exc) if isinstance(exc, SecretError) else type(exc).__name__


async def fail_run(ctx: Context, run_id: str, exc: BaseException, *, who: Principal) -> None:
    """End a run that cannot go on (a bug, lost evidence, an unreadable secret).

    Every caller cancels the keys it can: slots failing together each hand back their own key. A drawn
    cycle is judged first, on a connection of its own."""
    judgement = await sample.judgement(ctx, run_id)
    async with repo.audited(ctx.pool) as (conn, audit):
        when = ("queued", "running", "paused", "cancelling")
        moved = await repo.set_run_status(conn, run_id, "failed", when=when, error=failure(exc))
        await repo.cancel_keys(conn, run_id)
        if moved:
            await repo.end_cycle(conn, run_id, sample=judgement)
            await audit(who, "run.finished", run_id, {"status": "failed", "reason": failure(exc)})
