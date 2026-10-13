"""Table pre-checks as the worker reads them (docs/mvp/PRECHECKS.md §5, §6): one pair of a pre-check at a
time, each side inside one snapshot of its engine, judged by ``core.precheck`` and settled in one
transaction.

A pre-check is its own object, never a run: ``check`` claims a pending pair, reads its two catalogues,
lists what would refuse a run of its spec (``tables.issues`` and the zero-row probe), then the figures its
level asks for, a few aggregate statements a side. Two relations paired by name have no spec: they are
read by the plan their two catalogues imply (``core.precheck.implied``), and sent no probe. Two targets
are read at once; a pair whose sides are one target describes and probes both on side a's session, since
its plan needs both relations before either is read, then reads side a there and side b in a snapshot of
its own. No row crosses the wire, and a minimum, a maximum or a sum that is not shown stays in this
process's memory.

Under SSO a version that declares a filter and that the approval gate does not admit is read at level
``schema`` and sent no probe: no unapproved filter reaches a target (D15). A ``regex`` key map is no filter
and needs no approval of its own: its pattern reaches the target as a run's does (§6). A read's failure is
recorded by its closed code, never a driver's text, and what was read before it stays. An unreachable target
is read again once, a lease period later, then every pending pair of the pre-check naming it is settled
unread; a refused credential is never tried again: every pending pair of every unfinished pre-check
naming that target is settled, so a stale password costs at most the sessions in flight. Nothing here
touches a run: it writes ``ds.precheck_pairs``, ends its pre-check and audits ``precheck.finished``, and
its logs name ids and codes only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any

from deltasleuth import approvals
from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors import tables
from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.connectors.tables import Engine, Plan, Relation, Session, figures
from deltasleuth.connectors.tables.plan import described, probed
from deltasleuth.context import Context
from deltasleuth.core import precheck
from deltasleuth.core.precheck import Judged, Pair
from deltasleuth.core.spec import SpecError
from deltasleuth.core.types import CompiledSpec, FieldCompare, Side, Target
from deltasleuth.store import repo

SESSION = "session"  # the check a side whose session did not open, or whose read met a bug, is recorded by
_UNTRIED = ("unreachable", "auth_refused")  # the codes after which no other pair of the target is tried
TRIES = 3  # claims of a pair no read of which was settled: the next settles it unread, asking no target
Failed = tuple[str, str]  # a failed read's closed code and what a line may show beside it
log = logging.getLogger(__name__)


async def check(ctx: Context, precheck_id: str, n: int) -> None:
    """Read pair ``n`` of the pre-check, if it is pending, and settle it; its pre-check ends with its last
    pair. Only the store's own errors escape, and the pair is then read again: ``TRIES`` times at most,
    so that a read that can never be settled is not repeated against its targets for ever."""
    spec, refused = None, []
    async with ctx.pool.connection() as conn:
        claimed = await repo.claim_pair(conn, precheck_id, n)
        if claimed is None:
            return
        try:
            if claimed["spec_id"] is not None:
                spec = await ctx.spec(conn, claimed["spec_id"], live=True)
        except SpecError as exc:  # a target it names is no longer declared, say
            refused = [issue.message for issue in exc.issues]
        approved = spec is None or await _approved(ctx, conn, claimed["spec_id"], spec)
    named, level = Pair(claimed["spec_id"], claimed["label"], targets=_targets(claimed)), claimed["level"]
    if claimed["spec_id"] is None:  # paired by name: each target must still be a declared database
        found = [t for t in _targets(claimed) if t not in ctx.targets or ctx.targets.get(t).engine is None]
        refused = [f"target {target} is not declared as a database" for target in dict.fromkeys(found)]
    if claimed["tries"] > TRIES:
        await _settle(ctx, claimed, _unread(level, named, dict.fromkeys("ab", ("error", "NotSettled"))), {})
    elif refused:  # refused as a run of it would be, with no session opened
        await _settle(ctx, claimed, precheck.compare(level, level, named, None, {}, {}, refused), {})
    else:
        await _settle(ctx, claimed, *await _tried(ctx, claimed, spec, approved))


async def _tried(
    ctx: Context, claimed: Mapping[str, Any], spec: CompiledSpec | None, approved: bool
) -> tuple[Judged, dict[str, Failed]]:
    """The pair as read and judged, and each target of it after whose failure no other pair is tried. A
    side that could not be reached is read again once, after ``worker.lease_seconds``; a refused
    credential never is. Whatever the read itself raises is the pair's failure, by its class alone."""
    for last in (False, True):
        try:
            judged, sides = await _read(ctx, claimed, spec, approved)
        except Exception as exc:  # a bug: the pair says so, and is not read again
            failed = figures.failure(exc, ctx.settings.precheck.statement_timeout_s)
            return _unread(claimed["level"], _pair(claimed, spec, approved), dict.fromkeys("ab", failed)), {}
        untried = {
            target: failed
            for side, target in zip("ab", _targets(claimed), strict=True)
            for failed in sides[side]["failed"].values()
            if failed[0] in _UNTRIED
        }
        if last or {code for code, _ in untried.values()} != {"unreachable"}:
            break
        log.warning("table pre-check %s pair %s: a target was not reached: read again", *_named(claimed))
        await asyncio.sleep(ctx.settings.worker.lease_seconds)
    return judged, untried


def _named(claimed: Mapping[str, Any]) -> tuple[str, int]:
    return claimed["precheck_id"], claimed["n"]


def _targets(claimed: Mapping[str, Any]) -> tuple[str, str]:
    return claimed["target_a"], claimed["target_b"]


async def _approved(ctx: Context, conn: repo.Conn, spec_id: int, spec: CompiledSpec) -> bool:
    """Whether the approval gate, read unlocked, admits the version: true outside SSO, which has none."""
    try:
        await approvals.gate(ctx, conn, spec_id, spec)
    except approvals.ApprovalRequired:
        return False
    return True


def _pair(claimed: Mapping[str, Any], spec: CompiledSpec | None, approved: bool) -> Pair:
    """What the pair of a stored spec, or of two relations paired by name (``spec`` None: no security
    policy, nothing a pre-check does not apply), is judged by beside its plan (``core.precheck.Pair``)."""
    if spec is None:
        return Pair(None, claimed["label"], targets=_targets(claimed))
    compare = spec.compare
    unapplied = (
        spec.known_diffs
        or compare.include
        or compare.exclude
        or any(rule.normalize or rule.compare != FieldCompare() for rule in compare.fields)
    )
    listed = any(cfg.table is not None and cfg.table.columns is not None for cfg in spec.sides.values())
    named = f"{spec.name} {spec.version}"
    return Pair(
        claimed["spec_id"], named, listed, spec.security, approved, bool(unapplied), _targets(claimed)
    )


def _unread(level: str, pair: Pair, failed: Mapping[str, Failed]) -> Judged:
    """A pair none of whose sides in ``failed`` was read: each with its failure's code and what a line
    shows of it."""
    sides = {side: {"failed": {SESSION: found}} for side, found in failed.items()}
    return precheck.compare(level, level, pair, None, {}, sides)


class _Side:
    """One side of a pair while it is read: its target's engine, its session once open, its relation
    once described, and what was read of it, as ``core.precheck.compare`` takes it."""

    def __init__(self, name: str, target: Target, timeout_s: float) -> None:
        self.name, self.target, self.timeout_s = name, target, timeout_s
        self.engine: Engine = tables.of(target)
        self.session: Session | None = None
        self.relation: Relation | None = None
        self.read: dict[str, Any] = {
            "engine": self.engine.name, "at": None, "sum_digits": self.engine.sum_digits,
            "empty_null": self.engine.empty_null, "failed": {},
        }  # fmt: skip

    async def open(self, stack: AsyncExitStack) -> None:
        """A snapshot of the side's target, each of its statements bounded; a refused one is the side's
        failure."""
        try:
            self.session = await stack.enter_async_context(
                self.engine.snapshot(self.target, timeout_s=self.timeout_s)
            )
            self.read["at"] = utc_now_iso()
        except Exception as exc:
            self.read["failed"][SESSION] = figures.failure(exc, self.timeout_s)

    async def fetch(self, plan: Plan, wanted: list[precheck.Wanted], *, duplicates: bool, most: int) -> None:
        """Read what the level asks of the side on its session, if it has one."""
        if self.session is not None and self.relation is not None:
            self.read |= await figures.read(
                self.engine, self.session, figures.classed(plan, self.relation), wanted,
                duplicates=duplicates, distinct_max_rows=most, timeout_s=self.timeout_s,
            )  # fmt: skip


async def _read(
    ctx: Context, claimed: Mapping[str, Any], spec: CompiledSpec | None, approved: bool
) -> tuple[Judged, dict[str, dict[str, Any]]]:
    """The pair as read and judged, and what was read of each side. A refusal a run of the spec would
    meet ends the read: after one, a session is asked nothing more (its transaction may have ended with
    the refused statement). Two relations paired by name (``spec`` None) are each the one of that name in
    its side's schema."""
    settings, asked = ctx.settings.precheck, claimed["level"]
    pair = _pair(claimed, spec, approved)
    if spec is None:
        targets, gated = dict(zip("ab", _targets(claimed), strict=True)), False
        named = {s: (claimed["request"][s]["schema"], claimed[f"relation_{s}"]) for s in "ab"}
    else:
        table = {cfg.side.value: cfg.table for cfg in spec.sides.values() if cfg.table is not None}
        targets = {s: spec.sides[Side(s)].target for s in "ab"}
        named = {s: (found.schema, found.name) for s, found in table.items()}
        gated = not approved and any(found.where is not None for found in table.values())
    level = precheck.LEVELS[0] if gated else asked
    sides = {s: _Side(s, ctx.targets.live(targets[s]), settings.statement_timeout_s) for s in "ab"}
    a, b = sides["a"], sides["b"]
    one = a.target.target_id == b.target.target_id
    refused: list[str] = []
    plan: Plan | None = None
    wanted: dict[str, list[precheck.Wanted]] = {"a": [], "b": []}

    async def describe(side: _Side, session: Session | None) -> str | None:
        schema, name = named[side.name]
        try:
            if session is not None:
                side.relation = await described(side.engine, session, f"side {side.name}", schema, name)
        except ValueError as exc:
            return str(exc)
        return None

    async def probe(side: _Side, session: Session | None) -> str | None:
        try:
            if plan is not None and not gated and session is not None and spec is not None:
                await probed(side.engine, session, side.name, plan[side.name])
        except ValueError as exc:
            return str(exc)
        return None

    async def both(ask: Callable[[_Side, Session | None], Awaitable[str | None]]) -> None:
        """Ask each side, at once on two targets and in turn on one, whose side b is asked on side a's
        session unless side a was refused; what they refuse is listed in the order of the sides."""
        if one:
            found = [await ask(a, a.session)]
            found += [await ask(b, a.session)] if found == [None] else []
        else:
            found = await asyncio.gather(ask(a, a.session), ask(b, b.session))
        refused.extend(text for text in found if text is not None)

    async def fetch(side: _Side) -> None:
        if plan is not None and not refused and wanted[side.name]:
            duplicates = level == "distinct" and bool(plan[side.name]["keys"])
            await side.fetch(
                plan[side.name], wanted[side.name], duplicates=duplicates, most=settings.distinct_max_rows
            )

    async with AsyncExitStack() as stack:
        await asyncio.gather(*(side.open(stack) for side in ((a,) if one else (a, b))))
        await both(describe)
        if one:
            b.read["at"] = a.read["at"]  # its catalogue was read then; its own snapshot says when anew
        if a.relation is not None and b.relation is not None and not refused:
            relations = {"a": a.relation, "b": b.relation}
            if spec is None:
                plan = precheck.implied(a.relation, b.relation, (a.engine.name, b.engine.name))
            else:
                refused += tables.issues(ctx.targets, spec, relations)
                plan = None if refused else tables.resolved(ctx.targets, spec, relations)
            if plan is not None:
                wanted = precheck.wanted(level, plan, relations)
        await both(probe)
        await (fetch(a) if one else asyncio.gather(fetch(a), fetch(b)))
    if one and SESSION in a.read["failed"]:  # no second session is asked of a target that refused one
        b.read["failed"] = dict(a.read["failed"])
    elif one and plan is not None and not refused and wanted["b"]:
        async with AsyncExitStack() as stack:
            await b.open(stack)
            await fetch(b)
    read = {s: side.read for s, side in sides.items()}
    described_ = {s: side.relation for s, side in sides.items() if side.relation is not None}
    judged = precheck.compare(
        asked, level, pair, plan, described_, read, refused, distinct_max_rows=settings.distinct_max_rows
    )
    return judged, read


async def _settle(
    ctx: Context, claimed: Mapping[str, Any], judged: Judged, untried: Mapping[str, Failed]
) -> None:
    """Settle the pair with what was read of it, in one transaction: first, unread, the pending pairs
    naming a target of ``untried`` (of every unfinished pre-check for one that refused the credential; of
    this pre-check for one not reached), then the pair itself; each pre-check so left without an open
    pair ends, audited ``precheck.finished``. The statement that settles every pre-check's pairs locks
    their rows in the order of their ids and comes first, so two tasks settling at once never wait for
    each other in a circle."""
    precheck_id, n = _named(claimed)
    keys = judged.result["keys"]
    rows = {side: keys.get(side, {}).get("rows") for side in "ab"}
    async with repo.audited(ctx.pool) as (conn, audit):
        ended = {precheck_id}
        for target, failed in sorted(untried.items(), key=lambda found: (found[1][0], found[0])):
            unread = [
                _unread(precheck.LEVELS[0], Pair(targets=(target, target)), {side: failed}) for side in "ab"
            ]
            settled = await repo.settle_target(
                conn, target, unread[0].verdict, unread[0].rank,
                (unread[0].line, unread[0].result), (unread[1].line, unread[1].result),
                precheck_id=precheck_id if failed[0] == "unreachable" else None,
            )  # fmt: skip
            ended |= {row["precheck_id"] for row in settled}
            log.warning(
                "table pre-check %s pair %s: %s: %s other pair(s) of its target settled unread",
                precheck_id, n, failed[0], len(settled),
            )  # fmt: skip
        await repo.save_pair(
            conn, precheck_id, n, verdict=judged.verdict, rank=judged.rank, differing=judged.differing,
            line=judged.line, result=judged.result, rows_a=rows["a"], rows_b=rows["b"],
        )  # fmt: skip
        for found in sorted(ended):
            if (done := await repo.finish_precheck(conn, found)) is not None:
                await audit(WORKER, "precheck.finished", found, done)
