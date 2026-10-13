"""Runs: create one, pause, resume, stop or cancel it, list its outcome changes, compare one key now.

A batch or soak run is ``running`` from its creation and processed by the workers; a batch over two
tables without keys takes its keys from a table scan (``runner.scan``). A one-shot run
(``compare_one``) stays ``queued``, so no worker leases its key: it is read inline, with the
``recheck.oneShot`` passes while the result is due one, and then its key and the run are done
(unless it was cancelled meanwhile). A run keeps the capabilities its targets declare at its
creation (``params["capabilities"]``) and its table sides' column plan (``params["tables"]``), so
it reads the same once they change. A run's raw keys are sealed under a data key of its own
(docs/mvp/RETENTION.md §3); a complete key listing is its first cycle's census (docs/mvp/PACK.md §1.2), and
a key table's read is audited with the run, a key list as any other. Its key set is normalised, masked and
sealed off the event loop. A run is written while no target of the product's list changes
(``targets.unmoved``): one whose target was moved or removed since its spec was judged, while its keys were
listed, is refused.
Under SSO a spec version that needs a four-eyes approval starts, compares and resumes only under one
(``approvals.gate``, docs/mvp/APPROVALS.md §4): refused before its keys are resolved, then read FOR
SHARE in the transaction that writes the run, so a revoke either waits for it or is waited for. The
licence admits the spec's name as one of its pairs, or refuses the run, likewise (``licence.admit``,
docs/mvp/LICENCE.md §7): a new pair is counted again under the pair lock, and the run records the
licence it was made under. A hash sample's run is created with no keys, its first cycle's draw pending, and
resumes only once the draw before passed or was closed (``runner.sample``, docs/mvp/SAMPLING.md §5). A
schedule's firing is ``create_run`` with its ``Firing``: the run, the schedule's advance past the due time
and their audit rows commit together or not at all (docs/mvp/SCHEDULES.md §5). A run with a table side is
judged by the schema guard at its creation against its version's newest earlier run (``runner.drift``,
docs/mvp/PRECHECKS.md §7): one that would not compare what that run compared is created paused, before any
table row is read, or refused when its spec pauses on schema drift; a run the guard paused resumes only while
no change that breaks its plan stands. Each call takes ``who``, the principal that asks for it, and audits
as that principal; the API checks the roles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deltasleuth import approvals, licence
from deltasleuth import targets as product
from deltasleuth.auth.principal import Principal
from deltasleuth.connectors import tables
from deltasleuth.context import Capabilities, Context
from deltasleuth.core import cron
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.types import CompiledSpec, KeyResult
from deltasleuth.runner import census, drift, sample
from deltasleuth.runner.compare import FileReader, LiveReader, evaluate, save_pass
from deltasleuth.runner.cycle import CANCELLED, due, fail_run, final_status, finish_cycle, summarise
from deltasleuth.runner.drift import SCHEMA_PAUSE
from deltasleuth.runner.keys import keys_cut, normalise_keys, resolve, run_keys
from deltasleuth.runner.recheck import RecheckPolicy, confirm, freeze
from deltasleuth.runner.scan import new_scan
from deltasleuth.runner.stop import STOP_PREFIX
from deltasleuth.security.crypto import Sealer
from deltasleuth.store import repo

AUTH_PAUSE = "run_paused_auth_error"  # a side refused the credential; a resume means it was fixed
KEY_PAUSE = "run_paused_key_missing"  # the keyring lacks a KEK the run needs; resumed once it is back
MODE = "mode must be batch or soak, and only a soak run takes soak settings"
INTERVAL = "a soak run needs the spec's execution.cycleInterval"


@dataclass(frozen=True)
class Firing:
    """The due time a schedule's run is created for (docs/mvp/SCHEDULES.md §5): the schedule as the worker
    read it, who last saved it, the end of the time's window, the due time after it (None: the schedule
    ends) and the standing refusal's range the firing chains first."""

    schedule_id: str
    due: datetime
    revision: int
    time_zone: str
    set_by: str
    closes: datetime
    next: datetime | None
    held: Mapping[str, Any] | None = None


class ScheduleMoved(Exception):
    """The time is no longer due as the worker read the schedule (settled, or the schedule written, paused
    or deleted since), or its window has closed: the run is rolled back."""


class SetterGone(Exception):
    """Who last saved the schedule no longer answers for it (disabled, or no longer an operator: §6), as
    read in the run's transaction: the run is rolled back."""


def retention(spec: CompiledSpec) -> str:
    """The spec's retention class (docs/mvp/RETENTION.md §4.1)."""
    return spec.raw.get("retention", "standard")


async def create_run(
    ctx: Context,
    spec_id: int,
    mode: str,
    keys: Sequence[str] | None = None,
    samples_dir: str | Path | None = None,
    soak: Mapping[str, Any] | None = None,
    *,
    who: Principal,
    schedule: Firing | None = None,
) -> str:
    """Resolve the key set once, insert the run (running) and its keys, and audit the call.

    ``samples_dir`` runs a batch in file mode (captured samples, no recheck); ``soak`` is
    ``{"rolling": bool}`` (re-resolve the key source every cycle). Bad input raises ValueError, an
    unknown spec LookupError, a spec version lacking its four-eyes approval ApprovalRequired, a run the
    licence does not allow LicenceRefused, a hash sample's refusals SampleRefused (R1-R4); its keys are
    drawn by the worker. ``schedule``: the run is a schedule's firing, recorded in ``params["schedule"]``
    and committed with the schedule's advance, or not at all (ScheduleMoved, SetterGone). The schema guard
    records its tables' shapes beside ``params`` (V1): a run that would not compare what its version's run
    before it compared is created paused, or refused when its spec pauses on schema drift (``drift.Refused``,
    a ValueError), nothing written. A target the spec names that was moved or removed meanwhile (its keys
    may take long to list) is a ValueError too.
    """
    if mode not in ("batch", "soak") or (soak is not None and mode != "soak"):
        raise ValueError(MODE)
    if mode == "soak" and samples_dir is not None:
        raise ValueError("a soak run reads the targets: captured samples do not change between cycles")
    async with ctx.pool.connection() as conn:
        spec = await ctx.spec(conn, spec_id, live=True)
        read = product.reading(ctx, spec)  # its targets as it was judged: one moved since refuses the run
        _, approval = await approvals.gate(ctx, conn, spec_id, spec)  # refused before the keys are read
        await licence.admit(ctx, conn, spec)
        if spec.sample is not None:
            sample.refuse(mode, keys, samples_dir, soak)
            await sample.overlapping(conn, spec)
    if mode == "soak" and "cycleInterval" not in spec.raw.get("execution", {}):
        raise ValueError(INTERVAL)
    tabled = sum(cfg.table is not None for cfg in spec.sides.values())
    scanning = tabled == 2 and keys is None and "source" not in spec.raw["key"]  # a soak scans every cycle
    if samples_dir is not None:
        if tabled:
            raise ValueError("captured samples replay service sides only; a table side is read live")
        samples_dir = str(Path(samples_dir).resolve())
        FileReader(samples_dir, spec)  # refuses an unreadable or ambiguous sample directory now
    planned = await tables.planning(ctx.targets, spec, scanning=scanning)  # refuses a missing table or column
    plan = planned.plan
    resolved, walk = ([], None) if scanning or spec.sample else await resolve(ctx, spec, keys)
    taken = await asyncio.to_thread(run_keys, resolved, spec, ctx.tokeniser)
    params = {
        "keys_source": "table_scan"
        if scanning
        else "explicit"
        if keys is not None
        else spec.raw["key"]["source"]["kind"],
        "samples_dir": samples_dir,
        "soak": {"rolling": bool((soak or {}).get("rolling"))} if mode == "soak" else None,
        "tables": plan,
        "approval": approval,
    }
    if spec.sample is not None:
        params |= {"method": spec.sample.method, "population": list(spec.sample.population)}
    if schedule is not None:  # the canonical text 015's check and unique index rely on
        params["schedule"] = {
            "schedule_id": schedule.schedule_id, "due": cron.utc(schedule.due),
            "revision": schedule.revision, "time_zone": schedule.time_zone,
        }  # fmt: skip
    cut = keys_cut(resolved, taken)
    capped = STOP_PREFIX + "maxKeys" if cut else None
    scan = new_scan() if scanning else None
    run_id = repo.new_run_id()
    kek, wrapped, sealer = ctx.new_sealer(run_id)
    sealed = await asyncio.to_thread(sealer.seal_keys, taken)
    if planned.pinned:  # a file side's pins, sealed: no pack or plan_sha256 holds them (FILES.md §6)
        params["pinned"] = tables.sealed(sealer, planned.pinned)
    # A listing may take its whole window, and a target's 409 counts runs: no target changes while this one
    # is written, and one changed since the spec was judged refuses it.
    async with product.unmoved(ctx, read), repo.audited(ctx.pool) as (conn, audit):
        params["capabilities"] = ctx.declared(spec)
        approval_id, _ = await approvals.gate(ctx, conn, spec_id, spec, lock=True)
        params["licence"], pair = await licence.admit(ctx, conn, spec, lock=True)
        await sample.overlapping(conn, spec, lock=True)
        # V1: against the version's newest earlier baseline run; a refusal (E6) writes nothing.
        guard, drifted = drift.created(spec, spec_id, planned, await repo.guard_basis(conn, spec_id, run_id))
        stored = await repo.create_run(
            conn, spec_id, mode, sealed, params, scan, run_id=run_id, created_by=who.actor,
            retention=retention(spec), data_key=(kek, wrapped), approval_id=approval_id, keys_cut=cut,
        )  # fmt: skip
        if drifted is not None and drifted["paused"]:  # before any table row is read
            await repo.set_run_status(conn, run_id, "paused", stop_reason=SCHEMA_PAUSE)
        else:
            await repo.set_run_status(conn, run_id, "running", stop_reason=capped)
        if guard is not None:
            await repo.set_guard(conn, run_id, guard)
        if spec.sample is not None:
            await repo.set_draw(conn, run_id, {"due": datetime.now(UTC).isoformat()})
        if (listed := census.listed(spec, walk, 1)) is not None:
            await repo.set_census(conn, run_id, listed)
        detail = {"spec_id": spec_id, "mode": mode, "keys": len(taken), "file_mode": samples_dir is not None}
        detail |= {"approval_id": approval_id, "approval": approval, "licence": params["licence"]}
        detail["plan_sha256"] = sha256_hex(approvals.plan(stored))  # as stored: a pack's facts/plan.json
        if walk is not None and walk["basis"] == "key_table":
            detail["key_source"] = walk
        if schedule is not None:  # the schedule's row lock, then every audit row
            fired = await _fired(conn, schedule, run_id, detail | {"spec_name": spec.name})
            detail["schedule"] = params["schedule"]
            if schedule.held is not None:
                await audit(who, "schedule.missed", schedule.schedule_id, schedule.held)
        await audit(who, "create_run", run_id, detail)
        if pair is not None:  # a new pair (docs/mvp/LICENCE.md §11)
            await audit(who, "licence.pair", pair[0], pair[1] | {"run_id": run_id})
        if schedule is not None:
            await audit(who, "schedule.fire", schedule.schedule_id, fired)
        if drifted is not None:
            if drifted["paused"]:
                hashes = {side: (guard.get(side) or {}).get("sha256") for side in "ab"}  # type: ignore[union-attr]
                row = await audit(who, "paused", run_id, {"reason": SCHEMA_PAUSE, "schema": hashes})
                drifted |= {"paused_seq": row["chain"]["seq"]}  # one dedup key: one delivery a channel
            await audit(who, "schema.drift", run_id, drifted)
    return run_id


async def _fired(conn: repo.Conn, schedule: Firing, run_id: str, run: Mapping[str, Any]) -> dict[str, Any]:
    """Advance the schedule past the due time ``run_id`` was created for, in the run's transaction; its
    ``schedule.fire`` detail. ScheduleMoved when the time is no longer due or its window has closed;
    SetterGone when who saved it was disabled or demoted since the worker judged them (a listing may take
    its whole window): read once the schedule's row is locked, so no other save is in between."""
    fired = {
        "due": cron.utc(schedule.due), "run_id": run_id, "spec_id": run["spec_id"],
        "spec_name": run["spec_name"], "mode": run["mode"], "revision": schedule.revision,
        "time_zone": schedule.time_zone, "next": schedule.next and cron.utc(schedule.next),
    }  # fmt: skip
    late = await repo.settle_schedule(
        conn, schedule.schedule_id, due=schedule.due, revision=schedule.revision, anchor=schedule.due,
        next_due=schedule.next, last=fired | {"outcome": "fire"}, run_id=run_id, closes=schedule.closes,
    )  # fmt: skip
    if late is None:
        raise ScheduleMoved(schedule.schedule_id)
    if not await repo.setter_live(conn, schedule.set_by):
        raise SetterGone(schedule.schedule_id)
    return fired | {"late_s": late}


async def run_status(ctx: Context, run_id: str) -> str:
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
    if run is None:
        raise LookupError(f"no run {run_id}")
    return run["status"]


async def _move(
    ctx: Context,
    run_id: str,
    status: str,
    when: Sequence[str],
    reason: str | None = None,
    cleared: Sequence[str] = (),
    *,
    who: Principal,
) -> str:
    """Move the run to ``status`` if it is in one of ``when``; return its status now."""
    async with repo.audited(ctx.pool) as (conn, audit):
        if await repo.set_run_status(conn, run_id, status, when=when, stop_reason=reason, cleared=cleared):
            if status == "cancelling":
                await repo.cancel_keys(conn, run_id)
            await audit(who, status, run_id, {"reason": reason})
    return await run_status(ctx, run_id)


async def pause_run(ctx: Context, run_id: str, *, reason: str | None = None, who: Principal) -> str:
    """No key of a paused run is leased; keys in flight finish."""
    return await _move(ctx, run_id, "paused", ("running",), reason, who=who)


async def resume_run(ctx: Context, run_id: str, *, who: Principal) -> str:
    """Run a paused run again, under its spec version's approval (ApprovalRequired when it needs one it
    lacks); a refused credential's, a missing KEK's, a revoked approval's, the licence's or a sample's pause
    reason goes, as it was fixed, and a refused draw is drawn again. A soak paused for the licence resumes
    only once the licence allows its runs and seats its name as of when its cycle falls due, or now if later
    (LicenceRefused); a sampled one only once the draw before passed or was closed (SampleRefused, R5); one
    the schema guard paused only once its tables, looked at again, hold no change that breaks its plan
    (``drift.Refused``, V4), which accepts the changes that stand, and a cap at its creation is its reason
    again.
    The run's first approval covers its next cycle on: the current one read keys without it, unless a
    soak's boundary or its draw paused that cycle before it read any."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"no run {run_id}")
        if run["status"] != "paused":
            return run["status"]
        spec = await ctx.spec(conn, run["spec_id"])
    guarded = run["stop_reason"] == SCHEMA_PAUSE
    guard, accepted, drifted = await drift.resuming(ctx, run, who) if guarded else (None, None, None)
    capped = STOP_PREFIX + "maxKeys" if guarded and run["keys_cut"] else None
    async with repo.audited(ctx.pool) as (conn, audit):
        approval_id, _ = await approvals.gate(ctx, conn, run["spec_id"], spec, lock=True)
        if run["stop_reason"] == licence.LICENCE_PAUSE:  # as the boundary judged it (docs/mvp/LICENCE.md §8)
            prefix, at = f"run {run_id} cannot resume: ", due(spec, run, run["cycle"])
            await licence.need(ctx, conn, "run", name=spec.name, prefix=prefix, at=at)
        await sample.refuse_resume(conn, run)
        unread = (approvals.APPROVAL_PAUSE, licence.LICENCE_PAUSE, sample.DRAW_PAUSE, sample.SAMPLE_PAUSE)
        cleared = (AUTH_PAUSE, KEY_PAUSE, SCHEMA_PAUSE, *unread)
        if await repo.set_run_status(
            conn, run_id, "running", when=("paused",), stop_reason=capped, cleared=cleared
        ):
            first = run["approval_id"] is None and approval_id is not None
            # No approval and one of these reasons: a boundary or a draw paused the run before its cycle
            # read a key.
            from_cycle = run["cycle"] + 1 if first and run["stop_reason"] not in unread else None
            await repo.set_run_approval(conn, run_id, approval_id, from_cycle=from_cycle)
            if (run["draw"] or {}).get("error"):  # drawn again, with a new nonce
                await repo.set_draw(conn, run_id, {"due": run["draw"]["due"]})
            detail = {"reason": None, "approval_id": approval_id}
            if guard is not None:
                await repo.set_guard(conn, run_id, guard)
            await audit(who, "running", run_id, detail | ({"schema": accepted} if accepted else {}))
            if drifted is not None:
                await audit(who, "schema.drift", run_id, drifted)
    return await run_status(ctx, run_id)


async def resumable(ctx: Context) -> list[str]:
    """The runs paused for the licence that ``resume_run`` lets resume now, judged as it judges them."""
    found = []
    async with ctx.pool.connection() as conn:
        for run in await repo.pages(repo.list_runs, conn, status="paused"):
            if run["stop_reason"] == licence.LICENCE_PAUSE:
                spec = await ctx.spec(conn, run["spec_id"])
                try:
                    await licence.need(ctx, conn, "run", name=spec.name, at=due(spec, run, run["cycle"]))
                except licence.LicenceRefused:
                    continue
                found.append(run["run_id"])
    return sorted(found)


async def stop_run(ctx: Context, run_id: str, reason: str, *, who: Principal) -> str:
    """Cancel the keys not started; the cycle ends once the keys in flight finish."""
    return await _move(ctx, run_id, "cancelling", ("queued", "running", "paused"), reason, who=who)


async def cancel_run(ctx: Context, run_id: str, *, who: Principal) -> str:
    await stop_run(ctx, run_id, CANCELLED, who=who)
    await finish_cycle(ctx, run_id, who=who)
    return await run_status(ctx, run_id)


async def outcome_changes(ctx: Context, run_id: str) -> list[dict[str, Any]]:
    """Keys whose outcome differs from the previous soak cycle's, by masked key."""
    async with ctx.pool.connection() as conn:
        rows = await repo.outcome_changes(conn, run_id)
    return [{name: r[name] for name in ("cycle", "key_masked", "from_outcome", "to_outcome")} for r in rows]


async def compare_one(ctx: Context, spec_id: int, key: str, *, who: Principal) -> KeyResult:
    """Compare one key now; the result's ``attrs["run_id"]`` names the one-shot run holding it, and its
    ``attrs["warnings"]`` say how its tables differ from those the version's newest earlier run read (the
    schema guard's lines: a one-shot run is never paused by it and records no shape).
    ApprovalRequired when the spec version lacks its four-eyes approval, or when a revoke of it stopped
    the run meanwhile; LicenceRefused when the licence does not allow the run."""
    async with ctx.pool.connection() as conn:
        spec = await ctx.spec(conn, spec_id, live=True)
        _, approval = await approvals.gate(ctx, conn, spec_id, spec)  # refused before the key is read
        await licence.admit(ctx, conn, spec)
    taken = run_keys(normalise_keys([key], spec), spec, ctx.tokeniser)
    key, masked = next(iter(taken.items()))
    planned = await tables.planning(ctx.targets, spec)
    plan = planned.plan
    reader = LiveReader(ctx, spec, tables.pins(plan, planned.pinned) if planned.pinned else plan)
    params = {
        "keys_source": "explicit",
        "capabilities": ctx.declared(spec),
        "tables": plan,
        "approval": approval,
    }
    run_id = repo.new_run_id()
    kek, wrapped, sealer = ctx.new_sealer(run_id)
    if planned.pinned:
        params["pinned"] = tables.sealed(sealer, planned.pinned)
    async with repo.audited(ctx.pool) as (conn, audit):
        approval_id, _ = await approvals.gate(ctx, conn, spec_id, spec, lock=True)
        params["licence"], pair = await licence.admit(ctx, conn, spec, lock=True)
        await repo.create_run(
            conn, spec_id, "one_shot", sealer.seal_keys(taken), params, run_id=run_id, created_by=who.actor,
            retention=retention(spec), data_key=(kek, wrapped), approval_id=approval_id,
        )  # fmt: skip
        run = await repo.get_run(conn, run_id)
        warnings = drift.compared(spec, planned, await repo.guard_basis(conn, spec_id, run_id))
        detail = {"spec_id": spec_id, "approval_id": approval_id, "approval": approval}
        detail["licence"] = params["licence"]
        await audit(who, "compare_one", run_id, detail)
        if pair is not None:  # a new pair (docs/mvp/LICENCE.md §11)
            await audit(who, "licence.pair", pair[0], pair[1] | {"run_id": run_id})
    try:
        result = await _read_now(ctx, spec, ctx.capabilities(run), reader, run_id, key, sealer)
    except Exception as exc:
        await fail_run(ctx, run_id, exc, who=who)
        raise
    finally:
        await reader.aclose()
    completed = False
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            # The run's row lock first, in the order a stop takes its locks: a stop meanwhile has moved
            # the run on (and finishes it itself), else it waits for this transaction.
            if await repo.set_run_status(
                conn, run_id, "queued", when=("queued",)
            ) and await repo.complete_inline(conn, run_id, 1, masked):
                status = final_status(await summarise(ctx, conn, spec, run))
                await repo.set_run_status(conn, run_id, status, when=("queued",))
                await repo.end_cycle(conn, run_id)
                await audit(who, "run.finished", run_id, {"status": status, "reason": None})
                completed = True
    except Exception as exc:
        await fail_run(ctx, run_id, exc, who=who)
        raise
    if not completed and approval_id is not None:
        async with ctx.pool.connection() as conn:
            stopped = await repo.get_run(conn, run_id)
        if stopped["stop_reason"] == approvals.APPROVAL_REVOKED:  # its result is no evidence
            raise approvals.revoked(spec_id, spec, approval_id)
    result.attrs["run_id"], result.attrs["warnings"] = run_id, warnings
    return result


async def _read_now(
    ctx: Context,
    spec: CompiledSpec,
    caps: Capabilities,
    reader: LiveReader,
    run_id: str,
    key: str,
    sealer: Sealer,
) -> KeyResult:
    """The first pass, then up to ``oneShot.times`` recheck passes while the result is due one; a recheck
    reads no side of fixed files again, and a read of them on both sides is final (docs/mvp/FILES.md §12)."""
    policy = RecheckPolicy.from_spec(spec.raw)
    result, reads = await evaluate(ctx, spec, caps, reader, key, run_id=run_id, pass_no=1)
    for pass_no in range(1, policy.one_shot_times + 2):
        if pass_no > 1:
            await asyncio.sleep(policy.one_shot_delay_s)
            held = (await reader.standing({key: result}))[key]
            second, reads = await evaluate(
                ctx, spec, caps, reader, key, run_id=run_id, pass_no=pass_no, held=held
            )
            result = confirm(result, second, spec=spec, last=pass_no > policy.one_shot_times)
        result = freeze(result, reader.fixed)
        async with ctx.pool.connection() as conn:
            await save_pass(ctx, conn, spec, run_id, 1, pass_no, result, reads, sealer)
        if not policy.due(result, reader.fixed):
            break
    return result
