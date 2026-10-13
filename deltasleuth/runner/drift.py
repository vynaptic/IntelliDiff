"""The schema guard: a run does not read a table that is no longer what its plan,
or its version's run before it, read.

What it holds of a run is ``ds.runs.guard`` (format 1): each table side's shape as its plan was made of it
(``planned``) and as it was last seen where that is another (``seen``, with the ``changes`` between them;
the two hashes tell a shape that is gone from one that is as planned), what the plan reads each column as
(``roles``), the run of the version it was judged against at its creation (``basis``, with the changes
since), who accepted a change (``accepted``), when it last looked (``checked``) and each look it could not
make (``unchecked``). It looks when a run is created (``created``), on each scan pass's own sessions before
a cursor opens, at a key-list cycle's start (``cycle_start``), at the resume of a run it paused
(``resuming``) and after a read or a draw's frame failed on a column or a table (``failed``). A change that
alters what the plan's statements read, or how a key renders, pauses the run; any other warns, or pauses
under ``schemaDrift: {mode: pause}`` (``core.precheck.changes`` and ``verdict``). It fails no run: a look
that cannot be made is recorded and the run goes on. ``lines`` words everything a surface prints of it;
names and declared types leave ``guard`` in those lines only, and an audit row holds counts, closed kinds,
ids and hashes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import psycopg

from deltasleuth.auth.principal import WORKER, Principal
from deltasleuth.connectors import tables
from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.context import Context
from deltasleuth.core import precheck
from deltasleuth.core.types import CompiledSpec, Side
from deltasleuth.store import repo

FORMAT = 1  # of ds.runs.guard
SCHEMA_PAUSE = "run_paused_schema_drift"  # a table changed under the run: a person resumes or cancels it
TIMEOUT_S = 30.0  # each statement of a look on a session of its own, as a run's creation plans
# What a read of a dropped or revoked column or table raises.
DENIED = (
    psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege,
    tables.TargetDenied,
)  # fmt: skip
_READ = "(table sides only; a view's own tables, constraints and defaults are not read)"
_UNCHANGED = f"no change observed against the run's plan at its creation and each cycle's start {_READ}"
_REPLAN = "Cancel the run and start a new one, which plans the table as it is now"
# What a person does about a change that stops the run, by its kind.
_ADVICE = {
    "column_missing": "Put it back and resume the run, or cancel it and start a new run, which plans the"
    " table as it is now.",
    "relation_missing": "Put it back and resume the run, or cancel it.",
    "class_changed": f"{_REPLAN}, or change the column back and resume.",
    "key_render_changed": f"{_REPLAN}, or change the column back and resume.",
    "render_changed": f"{_REPLAN}, or change the column back and resume.",
}
_STARTS = ("creation", "scan", "cycle")  # where a look is at a cycle's start (creation: cycle 1's)
log = logging.getLogger("deltasleuth.guard")


class Tripped(Exception):
    """The schema guard paused the run before its scan pass opened a cursor: the pass ends, the run stays."""


class Refused(ValueError):
    """A run the schema guard does not create or resume; ``status`` is the API's answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Unread(Exception):
    """A side's catalogue could not be read on a session of the look's own; ``cause`` is why."""

    def __init__(self, side: str, cause: Exception) -> None:
        super().__init__(side)
        self.side, self.cause = side, cause


def mode(spec: CompiledSpec) -> str:
    """The spec's ``schemaDrift.mode``: ``finding`` unless it asks for ``pause``."""
    return (spec.raw.get("schemaDrift") or {}).get("mode", precheck.MODES[0])


def seen(relation: tables.Relation | None) -> dict[str, Any] | None:
    """A relation's shape as the account reads it; None for one that is gone, or none of whose columns it
    may read."""
    return precheck.shape(relation) if relation and any(c.readable for c in relation.columns) else None


def _sides(guard: Mapping[str, Any]) -> dict[str, Any]:
    return {side: guard[side] for side in "ab" if guard.get(side)}


def _held(spec: CompiledSpec, side: str, plan: tables.Plan, held: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the guard holds of a table side whose shape is ``held``: no change yet, so the shape seen is
    the planned one, and is not held twice."""
    table, sha256 = spec.sides[Side(side)].table, held and precheck.sha256(held)
    return {
        "relation": tables.relation(plan), "roles": precheck.roles(plan),
        "common": table is not None and table.columns is None,  # it compares the columns both sides have
        "planned": held, "sha256": sha256, "seen": None, "seen_sha256": sha256, "changes": [],
    }  # fmt: skip


def _checked(cycle: int, where: str, error: str | None = None, side: str | None = None) -> dict[str, Any]:
    return {"cycle": cycle, "at": utc_now_iso(), "where": where, "error_type": error, "side": side}


def _unmade(
    guard: Mapping[str, Any], cycle: int, where: str, error: str, side: str | None = None
) -> dict[str, Any]:
    """``guard`` after a look that could not be made: its last look, and every look not made (``unchecked``,
    kept across the looks after it), so that no line claims a look that was not made."""
    unchecked_ = [*(guard.get("unchecked") or ()), {"cycle": cycle, "where": where}]
    return dict(guard) | {"checked": _checked(cycle, where, error, side), "unchecked": unchecked_}


def _blank(mode_: str) -> dict[str, Any]:
    """The guard of a run planned before it existed: no side is held until its first look adopts it."""
    members = ("basis", "adopted", "since", "accepted", "checked")
    return {"format": FORMAT, "mode": mode_, "baseline": False} | dict.fromkeys(members)


def _baseline(basis: Mapping[str, Any] | None) -> bool:
    """Whether the basis a run is created against (``repo.guard_basis``) is a baseline run: else it is the
    newest earlier run that stored a plan, judged by the columns that plan compared alone."""
    return bool(((basis or {}).get("guard") or {}).get("baseline"))


def _counted(found: Sequence[Mapping[str, Any]], was: str | None, to: str | None) -> dict[str, Any]:
    """A side's part of a ``schema.drift`` row: its changes counted, and the hashes it went between."""
    breaking = sum(change["breaking"] for change in found)
    kinds = dict(Counter(change["kind"] for change in found))
    return {"breaking": breaking, "other": len(found) - breaking, "kinds": kinds, "from": was, "to": to}


def _detail(
    guard: Mapping[str, Any],
    where: str,
    moved: Mapping[str, Any],
    *,
    paused: bool = False,
    seq: int | None = None,
) -> dict[str, Any]:
    """A ``schema.drift`` row's detail: counts, closed kinds, ids and hashes only; ``seq``: the chain seq
    of the ``paused`` row written before it in its trip, whose dedup key it shares."""
    checked, basis = guard["checked"], guard["basis"]
    return {
        "cycle": checked["cycle"], "where": where, "mode": guard["mode"],
        "checked": not checked["error_type"], "paused": paused, "paused_seq": seq,
        "basis": basis and basis["run_id"], "sides": dict(moved), "error_type": checked["error_type"],
    }  # fmt: skip


def _hashes(guard: Mapping[str, Any]) -> dict[str, str | None]:
    """Each side's shape as last seen, by its hash: what a ``paused`` row and an accept name."""
    return {side: (guard.get(side) or {}).get("seen_sha256") for side in "ab"}


def _unaccepted(guard: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The changes that stand against the run's plan, each with its side, but those of a shape a person
    accepted: an accepted shape never trips again."""
    accepted = guard["accepted"] or {}
    return [
        (side, change)
        for side, held in _sides(guard).items()
        if accepted.get(side) != held["seen_sha256"]
        for change in held["changes"]
    ]


def _narrowed(guard: Mapping[str, Any]) -> bool:
    """Whether the run was created paused and no person has accepted its tables yet."""
    return not guard["baseline"] and guard["adopted"] is None and guard["basis"] is not None


# -- a run's creation ------------------------------------------------------------------------------------


def _against(
    spec: CompiledSpec, planned: tables.Planned, basis: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Each table side as the guard holds it at a run's creation, and how it differs from the basis: the
    newest earlier baseline run of the version, or, when none is one, the newest one that stored a plan
    (one from before the guard, one it adopted, or one whose look at its creation failed), against which
    only the columns it compared are judged."""
    sides, against, baseline = {}, {}, _baseline(basis)
    for side, relation in planned.relations.items():
        plan = planned.plan[side]  # type: ignore[index]
        sides[side] = _held(spec, side, plan, precheck.shape(relation))
        was = basis["guard"].get(side) if baseline else None  # type: ignore[index]
        if basis is not None and not baseline and (basis["tables"] or {}).get(side):
            was = {"planned": None, "roles": precheck.roles(basis["tables"][side])}
        if was is not None:
            against[side] = precheck.changes(
                was["planned"], sides[side]["planned"], sides[side]["roles"], basis=was["roles"],
                built=tables.named(plan).built,
            )  # fmt: skip
    return sides, against


def created(
    spec: CompiledSpec, spec_id: int, planned: tables.Planned, basis: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """What the guard holds of a new run whose plan is ``planned``, and its ``schema.drift`` detail
    when its tables differ from the ``basis`` (``repo.guard_basis``); ``(None, None)`` for a run without a
    table side. A run that would not compare what the basis compared is created paused (the detail says
    so, and the run is no baseline until a person accepts its tables), or refused when the spec pauses on
    schema drift. A fault of the guard's own code refuses no run: it is created with nothing held of
    its tables, its look recorded as not made, and the guard's next look at it adopts it."""
    if not planned.relations:
        return None, None
    try:
        return _created(spec, spec_id, planned, basis)
    except Refused:
        raise
    except Exception as exc:
        log.warning("schema guard: a run of spec %s not checked: %s", spec_id, type(exc).__name__)
        guard = _unmade(_blank(mode(spec)), 1, "creation", type(exc).__name__)
        return guard, _detail(guard, "creation", {})


def _created(
    spec: CompiledSpec, spec_id: int, planned: tables.Planned, basis: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    sides, against = _against(spec, planned, basis)
    found = [change for side in against.values() for change in side]
    what = precheck.verdict(mode(spec), found, creation=True)
    if what == "refused":
        named, of = f"spec {spec_id} ({spec.name} {spec.version})", basis["run_id"]  # type: ignore[index]
        narrowing = _listed([_narrowing(c, of) for c in found if c["breaking"]])
        raise Refused(
            422,
            f"{named} pauses on schema drift and a run of it would not compare what run {of} compared:"
            f" {narrowing}. Upload a new version to compare the tables as they are.",
        )
    told = basis and {"run_id": basis["run_id"], "predates": not _baseline(basis), "changes": against}
    guard = _blank(mode(spec)) | sides | {"baseline": what != "paused", "basis": told}
    guard["checked"] = _checked(1, "creation")
    if not found:
        return guard, None
    was = basis["guard"] if _baseline(basis) else {}  # type: ignore[index]
    moved = {
        side: _counted(against.get(side, ()), (was.get(side) or {}).get("sha256"), held["sha256"])
        for side, held in sides.items()
    }
    return guard, _detail(guard, "creation", moved, paused=what == "paused")


def compared(spec: CompiledSpec, planned: tables.Planned, basis: Mapping[str, Any] | None) -> list[str]:
    """A one-shot compare's warnings: how its fresh plan differs from the basis. It never pauses, records
    no guard and is no basis."""
    if basis is None:
        return []
    sides, against = _against(spec, planned, basis)
    guard = sides | {"basis": {"run_id": basis["run_id"], "changes": against}}
    return list(dict.fromkeys(text for _, text in _since(guard)))


# -- a run's own plan ------------------------------------------------------------------------------------


def due(run: Mapping[str, Any]) -> bool:
    """Whether the start of the run's cycle has not been looked at: a key-list run with a table side
    whose last look was in an earlier cycle, or that was planned before the guard existed."""
    planned = run["params"].get("tables") or {}
    listed = run["mode"] != "one_shot" and run["params"].get("keys_source") != "table_scan"
    looked = ((run["guard"] or {}).get("checked") or {}).get("cycle", 0)
    return listed and any(planned.get(side) for side in "ab") and looked < run["cycle"]


async def observed(ctx: Context, run: Mapping[str, Any]) -> tuple[CompiledSpec, dict[str, Any]]:
    """Each table side's relation as its catalogue holds it now (None: gone), read on a short session of
    its own, and the run's spec; ``_Unread`` names the side whose catalogue gave none."""
    async with ctx.pool.connection() as conn:
        spec = await ctx.spec(conn, run["spec_id"], live=True)
        planned = run["params"].get("tables") or {}
        if run["params"].get("pinned"):  # a file side's pins, which its stat check reads
            planned = tables.unsealed(await ctx.sealer(conn, run["run_id"]), run["params"])
    relations = {}
    for side in "ab":
        if not (plan := planned.get(side)):
            continue
        try:
            target = ctx.targets.live(spec.sides[Side(side)].target)
            engine = tables.planned(side, plan, target)
            session = await engine.connect(target, timeout_s=TIMEOUT_S)
            try:
                relations[side] = await engine.describe(session, plan["schema"], plan["name"], side=plan)
            finally:
                await session.close()
        except Exception as exc:
            raise _Unread(side, exc) from exc
    return spec, relations


def _looked(
    run: Mapping[str, Any], spec: CompiledSpec, relations: Mapping[str, Any], where: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The guard after a look that saw ``relations``, and each side's part of its ``schema.drift`` row:
    None unless a side's shape is not the one last recorded and brings a change (once per state). A run
    planned before the guard existed is adopted: the shape seen now is held as its plan's, so only what is
    missing is judged."""
    cycle, planned = run["cycle"], run["params"]["tables"]
    guard = dict(run["guard"] or _blank(mode(spec)))
    moved = {}
    for side, relation in relations.items():
        now = seen(relation)
        held, sha256 = guard.get(side), precheck.sha256(now)
        if held is None:
            held = _held(spec, side, planned[side], now) | {"seen_sha256": None}
            guard["adopted"] = cycle
        found = held["changes"]
        if sha256 != held["seen_sha256"]:
            built = tables.named(planned[side]).built
            found = precheck.changes(held["planned"], now, held["roles"], built=built)
        added = [change for change in found if change not in held["changes"]]
        moved[side] = _counted(added, held["seen_sha256"], sha256)
        other = None if sha256 == held["sha256"] else now  # as planned: held once
        guard[side] = held | {"seen": other, "seen_sha256": sha256, "changes": found}
    guard["checked"] = _checked(cycle, where)
    standing = any(held["changes"] for held in _sides(guard).values())
    if any(part["from"] != part["to"] for part in moved.values()) or not standing:
        guard["since"] = {"cycle": cycle, "at": guard["checked"]["at"], "where": where} if standing else None
    return guard, moved if any(part["breaking"] + part["other"] for part in moved.values()) else None


def _stops(guard: Mapping[str, Any], breaking: bool = False) -> bool:
    """Whether the changes that stand pause the run; ``breaking``: only one that breaks its plan does."""
    found = [change for _, change in _unaccepted(guard)]
    stopping = not breaking or any(change["breaking"] for change in found)
    return stopping and precheck.verdict(guard["mode"], found) == "paused"


def _counts(guard: Mapping[str, Any]) -> str:
    said = []
    for side, held in _sides(guard).items():
        if held["changes"]:
            part = _counted(held["changes"], None, None)
            said.append(f"side {side}: {part['breaking']} breaking, {part['other']} other")
    return ", ".join(said)


async def check(
    ctx: Context,
    run: Mapping[str, Any],
    spec: CompiledSpec,
    relations: Mapping[str, Any],
    where: str,
    *,
    breaking: bool = False,
) -> bool:
    """Judge what a look saw (each table side's relation, None: gone) against the run's plan and record it;
    True when the changes that stand pause the run (``breaking``: only one that breaks its plan does, as
    after a failed read). A trip is one transaction: the guard, judged on what the run holds under its row
    lock and written only while the run is in the cycle that was looked at, the pause of a running run, then
    the audit rows ``paused`` and, last, ``schema.drift``. A fault of the guard's own code is recorded and
    the run goes on unchecked."""
    run_id = run["run_id"]
    async with repo.audited(ctx.pool) as (conn, audit):
        held = await repo.held_guard(conn, run_id)
        if held is None or held["cycle"] != run["cycle"]:
            return False  # the run moved on to another cycle: its start is looked at afresh
        try:
            guard, moved = _looked(run | {"guard": held["guard"]}, spec, relations, where)
            stops, fault = _stops(guard, breaking), None
        except Exception as exc:
            stops, fault = False, exc
        paused, seq = False, None
        if fault is None:
            await repo.set_guard(conn, run_id, guard)
            paused = stops and await repo.set_run_status(
                conn, run_id, "paused", when=("running",), stop_reason=SCHEMA_PAUSE
            )
            if paused:
                pause = {"reason": SCHEMA_PAUSE, "schema": _hashes(guard)}
                seq = (await audit(WORKER, "paused", run_id, pause))["chain"]["seq"]
            if moved is not None:
                detail = _detail(guard, where, moved, paused=paused, seq=seq)
                await audit(WORKER, "schema.drift", run_id, detail)
    if fault is not None:
        await unchecked(ctx, run, where, fault, spec=spec)
    if paused:
        log.warning("schema guard: run %s paused at cycle %s (%s)", run_id, run["cycle"], _counts(guard))
    return stops


async def unchecked(
    ctx: Context,
    run: Mapping[str, Any],
    where: str,
    exc: BaseException,
    *,
    side: str | None = None,
    spec: CompiledSpec | None = None,
) -> None:
    """Record a look that could not be made, with why as an exception's class, and one ``schema.drift``
    row (``checked: false``): the cycle goes on unchecked, never silently, and no lease waits for it."""
    error = "TargetUnreachable" if isinstance(exc, tables.UNREACHABLE) else type(exc).__name__
    async with repo.audited(ctx.pool) as (conn, audit):
        held = await repo.held_guard(conn, run["run_id"])
        if held is not None and held["cycle"] == run["cycle"]:
            guard = held["guard"] or _blank(mode(spec) if spec else precheck.MODES[0])
            guard = _unmade(guard, run["cycle"], where, error, side)
            await repo.set_guard(conn, run["run_id"], guard)
            await audit(WORKER, "schema.drift", run["run_id"], _detail(guard, where, {}))
    log.warning("schema guard: run %s not checked: %s", run["run_id"], error)


async def cycle_start(ctx: Context, run_id: str) -> None:
    """Look at the tables of a running key-list run whose cycle fell due, before its census and its
    draw. A side that cannot be reached is tried once more a lease period later; then, as after any other
    fault, the look is recorded as not made and the cycle goes on. A refused login is raised
    (``TargetAuthRefused``): the worker pauses the run for it."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
    if run is None or run["status"] != "running" or not due(run):
        return
    for last in (False, True):
        try:
            spec, relations = await observed(ctx, run)
        except _Unread as exc:
            if isinstance(exc.cause, tables.TargetAuthRefused):
                raise exc.cause from None
            if isinstance(exc.cause, tables.UNREACHABLE) and not last:
                await asyncio.sleep(ctx.settings.worker.lease_seconds)
                continue
            return await unchecked(ctx, run, "cycle", exc.cause, side=exc.side)
        except psycopg.OperationalError:
            raise  # the store is unreachable: the next sweep looks again
        except Exception as exc:  # a spec that no longer compiles against today's targets, say
            return await unchecked(ctx, run, "cycle", exc)
        await check(ctx, run, spec, relations, "cycle")
        return


async def failed(ctx: Context, run_id: str) -> bool:
    """After a read failed on a column or a table, look at the run's tables; True when a change that
    breaks its plan paused the run instead of failing it. Whatever keeps the look from being made leaves
    the failure to fail the run."""
    try:
        async with ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
        if run is None:
            return False
        spec, relations = await observed(ctx, run)
        return await check(ctx, run, spec, relations, "read", breaking=True)
    except Exception as exc:
        log.warning("schema guard: run %s not checked: %s", run_id, type(exc).__name__)
        return False


async def resuming(
    ctx: Context, run: Mapping[str, Any], who: Principal
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Look again at the tables of a run the guard paused, before it resumes. The guard to record, what
    the ``running`` row says was accepted (None: nothing was) and a ``schema.drift`` detail when the look
    saw a new state. Refused while a change that breaks the plan stands or a side cannot be looked
    at. A resume of a run created paused, or of one whose spec pauses on schema drift, accepts the
    changes that stand: the run goes on with its plan, and is a baseline. A fault of the guard's own code
    keeps no run paused: the resume goes on, its look recorded as not made."""
    run_id = run["run_id"]
    try:
        spec, relations = await observed(ctx, run)
    except _Unread as exc:
        raise Refused(
            409,
            f"run {run_id} cannot resume: side {exc.side}'s table could not be checked now"
            f" ({tables.code(exc.cause)}): try again once the target answers",
        ) from None
    try:
        narrowed = _narrowed(run["guard"])
        guard, moved = _looked(run, spec, relations, "resume")
        broken = [_broken(side, guard[side], c) for side, c in _unaccepted(guard) if c["breaking"]]
    except Exception as exc:
        log.warning("schema guard: run %s not checked: %s", run_id, type(exc).__name__)
        guard = _unmade(run["guard"], run["cycle"], "resume", type(exc).__name__)
        return guard, None, _detail(guard, "resume", {})
    if broken:
        raise Refused(409, f"run {run_id} cannot resume: {broken[0][0]}")
    accepted = None
    if narrowed or (guard["mode"] == "pause" and _unaccepted(guard)):
        observed_at = (guard["since"] or guard["checked"])["cycle"]
        guard["accepted"] = _hashes(guard) | {
            "by": who.actor, "name": who.name, "at": utc_now_iso(), "cycle": observed_at,
            "where": "creation" if narrowed else "resume",
        }  # fmt: skip
        guard["baseline"], accepted = True, {"accepted": _hashes(guard)}
    return guard, accepted, moved and _detail(guard, "resume", moved)


# -- texts -----------------------------------------------------------------------------------------


def _typed(held: Sequence[Any]) -> str:
    return f"{held[0]} ({held[1]})"


def _when(at: Mapping[str, Any]) -> str:
    """When a look was made, as a line says it."""
    if at["where"] == "creation":
        return "before any table row was read"
    return f"{'at the start of' if at['where'] in ('scan', 'cycle') else 'in'} cycle {at['cycle']}"


def _listed(clauses: Sequence[str]) -> str:
    """The first of several clauses, with how many more there are."""
    distinct = list(dict.fromkeys(clauses))
    return distinct[0] + (f" (and {len(distinct) - 1:,} more)" if len(distinct) > 1 else "")


def _broken(side: str, held: Mapping[str, Any], change: Mapping[str, Any]) -> tuple[str, str]:
    """A change that breaks the run's plan, as a clause, and what a person does about it."""
    kind, column, relation = change["kind"], change["column"], held["relation"]
    was, now = change["was"], change["now"]
    if kind == "relation_missing":
        said = f"table or view {relation} is gone or no longer visible"
    elif kind == "column_missing":
        said = f"column {column!r} of {relation} is gone, or the role may no longer read it"
    elif kind in ("class_changed", "render_changed"):  # another class, or one the engine renders otherwise
        said = (
            f"column {column!r} of {relation} was {_typed(was)} when this run was planned and is"
            f" {_typed(now)} now: its values would no longer render as planned"
        )
    else:
        said = (
            f"key column {column!r} of {relation} was {was[0]} when this run was planned and is {now[0]} now:"
            " its key text would render differently"
        )
    return f"side {side}: {said}", _ADVICE[kind]


def _narrowing(change: Mapping[str, Any], of: str) -> str:
    """A change that narrows what a new run compares against run ``of`` of its version."""
    column, was, now = change["column"], change["was"], change["now"]
    if change["kind"] == "compared_lost":
        return f"column {column!r} was compared by run {of} of this version and is not compared now"
    if change["kind"] == "class_changed":
        return f"column {column!r} was {_typed(was)} in run {of} of this version and is {_typed(now)} now"
    return f"key column {column!r} was {was[0]} in run {of} of this version and is {now[0]} now"


def _other(side: str, held: Mapping[str, Any], change: Mapping[str, Any], gained: str) -> str:
    """A change that breaks nothing, as a warning's line (W); ``gained`` ends a gained column's."""
    kind, column, was, now = change["kind"], change["column"], change["was"], change["now"]
    if kind == "column_added":
        return f"side {side} gained column {column!r} {gained}"
    if kind == "column_removed":
        return f"side {side}: column {column!r}, which this run does not read, is gone"
    if kind == "type_changed":
        still = f"still {now[1]}" if was[1] == now[1] else "this run does not read it"
        return f"side {side}: column {column!r} was {was[0]} and is {now[0]} now ({still})"
    if kind == "nullable_changed":
        said = ["nullable" if typed[2] else "NOT NULL" for typed in (was, now)]
        return f"side {side}: column {column!r} was {said[0]} and is {said[1]} now"
    if kind == "key_changed":
        return f"side {side}: the primary key of {held['relation']} changed"
    return f"side {side}: {held['relation']} was a {was} and is a {now} now"


def _standing(guard: Mapping[str, Any]) -> list[tuple[bool, str, str]]:
    """Each change that stands against the run's plan: whether it stops the run, its line and, for one
    that breaks the plan, what a person does about it."""
    sides, said = _sides(guard), []
    pending = {id(change) for _, change in _unaccepted(guard)}
    for side, held in sides.items():
        for change in held["changes"]:
            if change["breaking"]:
                said.append((True, *_broken(side, held, change)))
                continue
            gained = "since this run was planned: this run does not compare it"
            if held["common"] and _shared(change["column"], sides.get("b" if side == "a" else "a")):
                gained += "; a new run would"
            line = _other(side, held, change, gained)
            said.append((guard["mode"] == "pause" and id(change) in pending, line, ""))
    return said


def _shared(column: str | None, other: Mapping[str, Any] | None) -> bool:
    """Whether the other table side holds ``column`` too, by its lower-cased name, as a plan pairs the
    columns both sides have; a side that is no table takes whatever the table holds."""
    if other is None:
        return True
    held = other["planned"] if other["seen_sha256"] == other["sha256"] else other["seen"]
    return (column or "").lower() in {name.lower() for name in (held or {}).get("columns", {})}


def _since(guard: Mapping[str, Any]) -> list[tuple[bool, str]]:
    """Each change against the basis at the run's creation: whether it narrows the comparison, and its
    line."""
    basis, said = guard["basis"] or {}, []
    for side, found in (basis.get("changes") or {}).items():
        for change in found:
            if change["breaking"]:
                said.append((True, _narrowing(change, basis["run_id"])))
                continue
            held = guard.get(side) or {"roles": {}, "relation": ""}
            compares = held["roles"].get(change["column"]) == "compared"
            does = "it is compared from this run on" if compares else "this version does not compare it"
            said.append((False, _other(side, held, change, f"since run {basis['run_id']}: {does}")))
    return said


def _pause(guard: Mapping[str, Any]) -> tuple[str, list[str]] | None:
    """The line of a run the guard paused and the lines it leaves to be said after it."""
    at, standing, since = guard["since"] or guard["checked"], _standing(guard), _since(guard)
    lead = f"Paused by the schema guard {_when(at)}"
    rest = [line for _, line, _ in standing] + [line for _, line in since]
    if stopping := [(line, advice) for stops, line, advice in standing if stops]:
        line, advice = stopping[0]
        plan = "This spec pauses on schema drift: resume to go on with the run's plan, or cancel."
        return f"{lead}: {line}. {advice or plan}", [said for said in rest if said != line]
    if not (_narrowed(guard) and since):
        return None
    lead = "Paused by the schema guard before any table row was read"
    narrowing = [line for narrows, line in since if narrows]
    line = narrowing[0] if narrowing else since[0][1]
    spec = "" if narrowing else "This spec pauses on schema drift: "
    accept = f"{spec}resume to accept the tables as they are, or cancel."
    return f"{lead}: {line}. {accept[0].upper()}{accept[1:]}", [said for said in rest if said != line]


def lines(run: Mapping[str, Any]) -> list[str]:
    """Every line a surface prints of what the schema guard holds of the run, the pause's first:
    the only builder of them. None for a run it does not guard."""
    guard = run["guard"]
    if guard is None:
        return []
    paused = run["status"] == "paused" and run["stop_reason"] == SCHEMA_PAUSE
    told = _pause(guard) if paused else None
    said = [told[0], *told[1]] if told else [line for _, line, _ in _standing(guard)]
    said += [] if told else [line for _, line in _since(guard)]
    changed = bool(said)  # else nothing differs from its plan, or from the basis
    if accepted := guard["accepted"]:
        day = f"{datetime.fromisoformat(accepted['at']):%Y-%m-%d}"
        basis = (guard["basis"] or {}).get("changes") or {}
        for side, held in _sides(guard).items():
            first = accepted["where"] == "creation" and basis.get(side)
            if first or accepted.get(side) != held["sha256"]:
                by = f"{accepted['name'] or accepted['by']} on {day}"
                said.append(
                    f"the change of side {side} observed at cycle {accepted['cycle']} was accepted by {by};"
                    " the run goes on with its plan"
                )
    checked = guard["checked"]
    if checked["error_type"]:
        what = f"side {checked['side']}" if checked["side"] else "the run's tables"
        said.append(
            f"the schema guard could not check {what} {_when(checked)} ({checked['error_type']}): the cycle"
            " went on unchecked"
        )
    basis, unmade = guard["basis"], [_when(at) for at in guard.get("unchecked") or ()]
    if guard["adopted"] is not None:
        at = f"its tables' shapes were first recorded at cycle {guard['adopted']}"
        said.append(f"planned before the schema guard existed: {at}")
    elif not changed and _sides(guard) and unmade:  # a look it could not make is never claimed
        looked = "no change observed against the run's plan where the schema guard looked"
        said.append(f"{looked} {_READ}; it could not look {_listed(unmade)}")
    elif not changed and _sides(guard):
        said.append(_UNCHANGED)
    if guard["adopted"] is None and basis is None and _sides(guard):
        said.append("no earlier run of this version recorded its tables' shapes: this run records them")
    elif basis is not None and basis["predates"]:
        only = "only its compared columns were checked against this run"
        no = "recorded no shape of its tables at its creation"  # it predates the guard, say
        said.append(f"run {basis['run_id']} of this version {no}: {only}")
    return list(dict.fromkeys(said))


def told(run: Mapping[str, Any]) -> dict[str, Any]:
    """A run's ``schema`` block (``GET /v1/runs/{id}``): whether the guard holds its tables, the spec's
    mode, the basis run, the last look, whether the guard paused it, whether its resume accepts changes
    (as ``resuming`` does: the run was created paused, or its spec pauses on schema drift and a change that
    breaks nothing stands, whatever must be undone first), and its lines."""
    guard = run["guard"]
    if guard is None:
        blank = dict.fromkeys(("mode", "basis", "checked"))
        return {"guarded": False, **blank, "paused": False, "accepts": False, "lines": []}
    paused = run["status"] == "paused" and run["stop_reason"] == SCHEMA_PAUSE
    others = [change for _, change in _unaccepted(guard) if not change["breaking"]]
    accepts = paused and (_narrowed(guard) or (guard["mode"] == "pause" and bool(others)))
    return {
        "guarded": bool(_sides(guard)), "mode": guard["mode"],
        "basis": guard["basis"] and guard["basis"]["run_id"], "checked": guard["checked"], "paused": paused,
        "accepts": accepts, "lines": lines(run),
    }  # fmt: skip


def warnings(run: Mapping[str, Any]) -> list[str]:
    """What a run's creation warns of (``POST /v1/runs``, ``ds run``): the lines of its tables' changes
    since the basis, the pause's first when it was created paused."""
    guard = run["guard"]
    if guard is None or not any((guard["basis"] or {}).get("changes", {}).values()):
        return []
    told = _pause(guard) if run["stop_reason"] == SCHEMA_PAUSE else None
    return list(dict.fromkeys([told[0], *told[1]] if told else [line for _, line in _since(guard)]))


def sentence(rows: Sequence[Mapping[str, Any]], cycle: int) -> str | None:
    """What ``report.pdf`` says of the guard through ``cycle``: the run's ``schema.drift`` rows of that
    cycle and the earlier ones added up, so a cycle's report reads the same whenever it is made; None
    without one. A change paused the run when its row did and it pauses one: it breaks the plan, or the
    spec pauses on any change. A look not made at a creation, a scan pass or a cycle's start is a cycle
    start's, any other (at a resume, after a failed read) another look's."""
    details = [row["detail"] for row in rows if row["detail"]["cycle"] <= cycle]
    recorded = []
    for side in "ab":
        stopped = others = 0
        for detail in details:
            part = detail["sides"].get(side) or {"breaking": 0, "other": 0}
            pausing = part["breaking"] + part["other"] * (detail["mode"] == "pause")
            pausing *= detail["paused"]
            stopped, others = stopped + pausing, others + part["breaking"] + part["other"] - pausing
        said = [f"{stopped:,} change{'s' * (stopped != 1)} that paused the run"] * bool(stopped)
        if others:
            said.append(f"{others:,} {'other' if stopped else 'change'}{'s' * (others != 1)}")
        if said:
            recorded.append(f"{' and '.join(said)} on side {side}")
    unmade = [detail["where"] in _STARTS for detail in details if not detail["checked"]]
    starts, looks = sum(unmade), len(unmade) - sum(unmade)
    unchecked_ = [f"{starts:,} cycle start{'s' * (starts != 1)}"] * bool(starts)
    unchecked_ += [f"{looks:,} other look{'s' * (looks != 1)}"] * bool(looks)
    parts = [f"recorded {' and '.join(recorded)}"] * bool(recorded)
    if unchecked_:
        parts.append(f"could not check {' and '.join(unchecked_)}")
    if not parts:
        return None
    return f"Schema guard (observed): through cycle {cycle} it {', and '.join(parts)}."
