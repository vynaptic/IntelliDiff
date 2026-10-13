"""Cut-over readiness, the worker's part: each ended batch or soak cycle
is measured once onto its tombstone (``ds.cycles.readiness``), where ``core.readiness`` judges it, and each
change of a pair's state or of a required gate is chained as ``readiness.changed``, which the notifier
routes.

``watch`` is one pass, run by the worker's sweep as a task of its own, one at a time and outside
``finish_cycle``: whatever it meets it logs, so readiness never fails a run. It measures the cycles ended
since 014, newest first, then, at most every ``BACKFILL_EVERY_S``, a batch of those ended before it. A
batch is one audited transaction: each cycle is measured in a savepoint that holds its tombstone FOR SHARE
(a purge's start waits for the batch, or the measure finds the purge started and reads the tombstone
alone), stored once, and ``readiness.measured`` names each stored record's hash, last. A measure that fails
(a statement the server cancelled or refused too: only a lost connection ends the batch) stores its
error's type instead and is tried again 1, 2, 4 and 8 hours later; the fifth failure is final. Counts,
ids, codes and stratum labels only: no key and no value.

Once no cycle ended before 014 is left to measure, a pass evaluates every pair as the API does
(``deltasleuth.readiness.judged``) when it measured a cycle, when a row that may change a pair entered the
audit chain (followed by its seq, which is commit order, from a cursor held in memory: it only tells that
an evaluation is due, and a process's first pass evaluates whatever entered before it) at least
``READINESS_MIN_S`` after the last evaluation, or every ``READINESS_EVERY_S``; one that fails is tried
again ``READINESS_MIN_S`` later. A pair without a ``readiness.changed`` row gets its baseline; one whose
state, ``regressed`` or a required gate's status differs from its newest row gets a new one, naming why
from what differs (``core.readiness.causes``); a pair with a cycle still to measure gets none until the
pass that measures it; a closed pair is silent after its day. A process's first evaluation records the
settings as judged (``readiness.config``) when they differ from the newest such row, in the transaction of
the rows it writes, which alone name ``config``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from deltasleuth import approvals
from deltasleuth import readiness as judging
from deltasleuth.auth.principal import WORKER
from deltasleuth.config import ReadinessSettings
from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.context import Context
from deltasleuth.core import readiness
from deltasleuth.core.types import CompiledSpec
from deltasleuth.runner import census, sample
from deltasleuth.runner.cycle import failure
from deltasleuth.store import repo

BACKFILL_EVERY_S = 10  # a batch of the cycles ended before 014 at most this often
ATTEMPTS = 5  # measures of a cycle before its failure is final
READINESS_MIN_S = 30  # a chain row that may change a pair is judged this long after the last evaluation
READINESS_EVERY_S = 900  # and time alone (a cycle no longer fresh) this often; a failed one after MIN_S
# The audit actions that may change a pair: one entering the chain makes an evaluation due.
TRIGGERS = (
    "run.cycle", "run.finished", "approval.request", "approval.grant", "approval.reject", "approval.revoke",
    "pack.issue", "pack.revoke", "sample.draw", "sample.close", "adjudication.score", "accuracy.label",
    "licence.state", "licence.install", "licence.withdraw",
)  # fmt: skip
log = logging.getLogger(__name__)


@dataclass
class State:
    """What the worker's passes keep between them."""

    backfilled_at: float = -float("inf")  # monotonic: the last backfill batch
    configured: bool = False  # the settings this process judges with are recorded
    seq: int | None = None  # the chain row the evaluations have followed to: the head at the first pass
    evaluated_at: float = -float("inf")  # monotonic: the last evaluation
    failed_at: float = -float("inf")  # monotonic: the last evaluation that failed
    measured: bool = False  # a cycle was measured since


async def watch(ctx: Context, state: State) -> None:
    """One pass: measure the next cycles ended since 014 and, when one is due, a backfill batch; then,
    whatever the measuring met, evaluate the pairs when something may have changed them. Never raises: a
    failure is logged, and the next pass does what this one left; an evaluation that failed is tried again
    ``READINESS_MIN_S`` later, not every pass."""
    try:
        state.measured |= bool(await _batch(ctx, backfill=False))
        if time.monotonic() - state.backfilled_at >= BACKFILL_EVERY_S:
            state.backfilled_at = time.monotonic()
            state.measured |= bool(await _batch(ctx, backfill=True))
    except Exception as exc:
        log.warning("readiness pass failed: %s", failure(exc))
    if time.monotonic() - state.failed_at < READINESS_MIN_S:
        return
    try:
        await _evaluate(ctx, state)
    except Exception as exc:
        state.failed_at = time.monotonic()
        log.warning("readiness evaluation failed: %s", failure(exc))


async def _configured(
    conn: repo.Conn, audit: repo.Audit, settings: ReadinessSettings | None, was: str | None
) -> None:
    """Audit ``readiness.config``, the settings as judged, which differ from the newest such row's (``was``,
    its hash; ``licence.watch``'s pattern): the gates, each listed pair's, and what deviates from the
    built-in defaults."""
    listed = [None, *(pair.spec for p in (settings.programmes if settings else ()) for pair in p.pairs)]
    deviations = [
        {"pair": name, "deviations": list(found)}
        for name in listed
        if (found := readiness.policy(settings, name or "").deviations)
    ]
    detail = {"from_sha256": was, "sha256": readiness.config_sha256(settings)} | readiness.effective(settings)
    installation = await repo.installation_id(conn)
    await audit(WORKER, "readiness.config", installation, detail | {"deviations": deviations})


async def _evaluate(ctx: Context, state: State) -> None:
    """Evaluate every pair when a cycle was measured, a row that may change one entered the chain at least
    ``READINESS_MIN_S`` after the last evaluation, or ``READINESS_EVERY_S`` have passed, and chain what
    changed: nothing while a cycle ended before 014 is still to measure, and nothing of a pair one of
    whose cycles was never measured, which the pass that measures it judges. A process's first evaluation
    records the settings it judges with when they are new, before the rows it writes: they name ``config``,
    and no later row does."""
    started, settings = time.monotonic(), ctx.settings.readiness
    waited = started - state.evaluated_at
    found: judging.Judged | None = None
    async with repo.audited(ctx.pool) as (conn, audit):
        if state.seq is None:
            state.seq = await repo.chain_head(conn)
        entered = await repo.readiness_entered(conn, state.seq, TRIGGERS)
        recorded = None if state.configured else await repo.last_audit(conn, "readiness.config")
        was = recorded and recorded["detail"]["sha256"]
        config = not state.configured and was != readiness.config_sha256(settings)
        due = state.measured or waited >= (READINESS_MIN_S if entered["seq"] else READINESS_EVERY_S)
        if due and not await repo.backfill_owed(conn):
            found = await judging.judged(ctx, conn)
        if config:
            await _configured(conn, audit, settings, was)
        for name, item in found.items.items() if found is not None else ():
            if name not in found.waiting:  # judged on the pass that measures its cycles
                judged = readiness.as_of(found.policies[name], found.now)
                await _changed(audit, item, found.last.get(name), judged, config=config)
    state.configured = True
    if found is not None:
        state.seq, state.evaluated_at, state.measured = entered["head"], started, False
    elif entered["seq"] is None:
        state.seq = entered["head"]  # nothing that may change a pair entered: followed


async def _changed(
    audit: repo.Audit,
    item: Mapping[str, Any],
    last: repo.Row | None,
    judged: datetime,
    *,
    config: bool,
) -> None:
    """Chain the pair's ``readiness.changed`` row when it has none (its baseline), or when its state,
    ``regressed`` or a required gate's status differs from its newest one; a closed pair (``judged``: the
    end of its day) is silent once a row was written after that. ``config``: the settings it is judged
    with were recorded with this evaluation."""
    was = last["detail"] if last is not None else {"to": None, "regressed": False, "gates": {}}
    if last is not None and not item["open"] and last["at"] > judged:
        return
    required = {gate["gate"]: gate for gate in item["gates"] if gate["required"]}
    statuses = {name: gate["status"] for name, gate in required.items()}
    if last is not None and (was["to"], was["regressed"], was["gates"]) == (
        item["state"], item["regressed"], statuses,
    ):  # fmt: skip
        return
    why = ["baseline"]
    if last is not None:  # what differs for the pair itself
        before, current = last["spec_id"], item["spec_id"]  # None: without an evidence cycle, then or now
        version = None not in (before, current) and before != current
        why = readiness.causes(was, item, version=version, config=config)
    changed = [
        {"gate": name, "from": was["gates"].get(name), "to": statuses.get(name)}
        | {key: required[name][key] if name in required else None for key in ("value", "allowed", "over")}
        for name in readiness.GATES
        if last is not None and was["gates"].get(name) != statuses.get(name)
    ]
    evidence = item["evidence"] and {"run_id": item["evidence"]["run_id"], "cycle": item["evidence"]["cycle"]}
    detail = {
        "pair": item["pair"], "programme": item["programme"], "entity": item["entity"], "from": was["to"],
        "to": item["state"], "was_regressed": was["regressed"], "regressed": item["regressed"],
        "score": item["score"], "causes": why,
        "gates": statuses, "changed": changed, "evidence": evidence, "candidate": item["candidate"],
        "signoff_id": item["signoff_id"], "pack_id": item["pack_id"],
        "config_sha256": item["config_sha256"],
    }  # fmt: skip
    await audit(WORKER, "readiness.changed", f"readiness:{item['pair']}", detail)


async def _batch(ctx: Context, *, backfill: bool) -> int:
    """Measure the next unmeasured cycles and store their records, in one audited transaction: how many
    were stored, failed ones too."""
    specs: dict[int, CompiledSpec] = {}
    measured = 0
    async with repo.audited(ctx.pool) as (conn, audit):
        stored = []
        for row in await repo.unmeasured_cycles(conn, backfill=backfill):
            run_id, cycle = row["run_id"], row["cycle"]
            try:
                async with conn.transaction():  # a savepoint: a failed measure leaves the batch whole
                    record = await _measured(ctx, conn, row, specs)
            except Exception as exc:
                if repo.lost(conn, exc):
                    raise  # the store is unreachable: the next pass measures the batch
                record = await _failed(conn, row, type(exc).__name__)
            if await repo.set_readiness(conn, run_id, cycle, record):
                measured += 1
                if "error" not in record:
                    stored.append([run_id, cycle, readiness.measured_sha256(record)])
        if stored:
            await audit(WORKER, "readiness.measured", "readiness", {"cycles": stored})
    return measured


async def _failed(conn: repo.Conn, row: repo.Row, error: str) -> dict[str, Any]:
    """The record of a cycle whose measure raised ``error``: an ``unknown`` point, for good once it failed
    ``ATTEMPTS`` times."""
    run_id, cycle = row["run_id"], row["cycle"]
    log.warning("readiness: cycle %s of run %s not measured: %s", cycle, run_id, error)
    before = (await repo.get_cycle(conn, run_id, cycle) or {}).get("readiness") or {}
    attempts = before.get("attempts", 0) + 1
    return {
        "format": readiness.FORMAT, "pair": row["pair"], "spec_id": row["spec_id"], "error": error,
        "attempts": attempts, "fails": {"standard": ["unknown"], "high": ["unknown"]},
        "measured_at": utc_now_iso(),
    } | ({"final": True} if attempts >= ATTEMPTS else {})  # fmt: skip


async def _measured(
    ctx: Context, conn: repo.Conn, row: repo.Row, specs: dict[int, CompiledSpec]
) -> dict[str, Any]:
    """The ended cycle's measures, from its tombstone and, unless its purge has started (``late``), its
    stored rows: O(discrepant keys plus its findings)."""
    from deltasleuth.reports.pack import census_problem  # the reports import the runner's modules

    run_id, cycle = row["run_id"], row["cycle"]
    ended = await repo.get_cycle(conn, run_id, cycle, lock=True)  # a purge's start waits for the batch
    run = await repo.get_run(conn, run_id)
    assert ended is not None and run is not None
    if run["spec_id"] not in specs:
        specs[run["spec_id"]] = await ctx.spec(conn, run["spec_id"])
    spec, params, counters = specs[run["spec_id"]], run["params"], ended["counters"]
    late, own, cut = ended["purging_at"] is not None, counters.get("cycle") == cycle, ended["keys_cut"]
    source = params.get("keys_source")
    scan = None if late else await repo.get_scan(conn, run_id, cycle)
    drawn = await repo.get_draw(conn, run_id, cycle) if source == "hash_sample" else None
    whole = problem = said = None
    if source != "table_scan":
        counted = census.of_cycle(run, ended, scan, spec)
        last = await repo.last_census(conn, run_id, cycle)
        problem = census_problem(run, cycle, spec, counted, scan, ended, last)
        said = problem or census.described(counted["a"])
    elif not late:  # a scan read to its end counts both sides, with or without its graph parity (L2)
        whole = scan is not None and scan["done"] and cut is None
        if whole:  # with or without its graph parity; but a file side a with lines that were not rows
            side_a = census.scanned("a", params["tables"]["a"], scan, spec)  # type: ignore[arg-type]
            if side_a.get("rejected"):  # is no census
                counted = {"cycle": cycle, "a": side_a, "b": None}
                problem = census_problem(run, cycle, spec, counted, scan, ended, None)
            said = problem or census.described(side_a)
        elif cut is not None:
            said = f"was cut at {cut['taken']:,} keys (maxKeys)"
        else:
            rows = [(scan or {}).get(f"rows_{side}", 0) for side in "ab"]
            said = (
                f"scanned only part of its tables (rows_a {rows[0]:,}, rows_b {rows[1]:,} read; the scan"
                " stopped before the end)"
            )
    changes = graph = None
    if not late and own and cycle > 1 and source != "hash_sample":
        before = await repo.get_cycle(conn, run_id, cycle - 1, lock=True)
        if before is not None and before["purging_at"] is None:
            changes = await repo.cycle_changes(conn, run_id, cycle)
    if not late and spec.raw.get("graph"):
        counts = await repo.graph_counts(conn, run_id, cycle)
        graph = {"computed": scan is not None and scan.get("graph") is not None}
        graph |= {code: counts.get(code, 0) for code in readiness.GRAPH_CODES}
    critical = results = None
    if not late:
        bug_rules = approvals.bug_rules(spec)
        critical = await repo.unexplained_criticals(conn, run_id, cycle, bug_rules)
        results = await repo.count_results(conn, run_id, cycle)
    judged = (ended["sample"] or {}).get("status", "unjudged") if source == "hash_sample" else None
    record = {
        "pair": run["spec_name"], "spec_id": run["spec_id"], "version": run["spec_version"],
        "mode": run["mode"], "keys_source": source, "needs_approval": approvals.needs(spec),
        "basis_text": said, **{name: counters.get(name) if own else None for name in readiness.COUNTS},
        "unexplained_critical": critical, "results": results, "draw_id": drawn and drawn["draw_id"],
        "sample_status": judged,
        "sample_strata": drawn and [{"label": s["label"], "tier": s["tier"]} for s in drawn["strata"]],
        "selection_ok": None if late or drawn is None else await sample.reselected(conn, drawn),
        "changes": changes, "graph": graph, "late": late, "measured_at": utc_now_iso(),
    }  # fmt: skip
    method = drawn["method"] if drawn else params.get("method")
    return readiness.measured(
        record, own=own, problem=problem, method=method, whole=whole, cut=cut is not None
    )
