"""Ended cycles for readiness's runner tests (docs/mvp/READINESS.md §4), written as the worker's writes
leave them: a run's keys, each cycle's results and findings, its counters and its tombstone."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import CompiledSpec, Confirmation, KeyResult, Outcome, SideState, SideStateKind
from deltasleuth.runner import readiness
from deltasleuth.runner.counters import count_cycle
from deltasleuth.runner.recheck import RecheckPolicy
from deltasleuth.runner.scan import new_scan
from deltasleuth.store import repo
from tests.conftest import TESTER, create_run
from tests.unit.store.four_eyes import approve, finding

from .conftest import Harness, spec_text

KEYS = [f"K{n:03d}" for n in range(1, 9)]
COMPLETE, ABSENT = SideState(SideStateKind.COMPLETE), SideState(SideStateKind.ABSENT)
# The unit spec with a rule that accepts a difference and one that names a known bug.
RULES = """\
knownDiffs:
  - {id: KD-ACCEPTED, when: "path == '$.status' && a == 'OLD' && b == 'NEW'", classification: accepted,
     reason: status vocabulary renamed, ticket: UNIT-1}
  - {id: KD-BUG, when: "path == '$.status' && a == 'X' && b == 'Y'", classification: bug,
     reason: a known bug of the new side, ticket: UNIT-2}
"""
SOAK = spec_text(
    execution="cycleInterval: 1h, concurrency: 2", extra=RULES
)  # a soak's spec names its interval
TABLES = {side: {"schema": "public", "name": "accounts"} for side in "ab"}
PAIR = "unit-runner"
MATCH = dict.fromkeys(KEYS, "MATCH")
MISMATCH = ("MISMATCH", [("HIGH", None, None)])
AT = "2026-10-03T04:00:00.000000Z"
# A scan that read both tables to their end in one snapshot and found 992 rows identical.
SCANNED = new_scan() | {
    "done": True, "matched": 992, "rows_a": 1_000, "rows_b": 1_000, "candidates": 8, "snapshots_a": 1,
    "snapshots_b": 1, "first_at": AT, "last_at": AT,
}  # fmt: skip
SCAN = {"keys_source": "table_scan", "tables": TABLES}
# A result as a cycle's ``results`` name it: its outcome and its findings' (severity, rule, classification).
Read = str | tuple[str, Sequence[tuple[str, str | None, str | None]]] | None


async def stored(h: Harness, text: str | None = None) -> int:
    """The unit spec (with its known-diff rules), or ``text``, compiled without targets and stored."""
    spec = compile_spec(parse_yaml_text(text or SOAK))
    async with h.ctx.pool.connection() as conn:
        return await repo.save_spec(conn, spec, created_by=TESTER.actor)


async def started(
    h: Harness,
    spec_id: int | None = None,
    *,
    keys: Sequence[str] = KEYS,
    mode: str = "soak",
    scan: Mapping[str, Any] | None = None,
    **params: Any,
) -> str:
    """A running run of the spec over ``keys`` (masked as they are), its keys the spec's inline list unless
    ``params`` say."""
    spec_id = spec_id or await stored(h)
    params = {"keys_source": "list", "samples_dir": None} | params
    async with h.ctx.pool.connection() as conn:
        run_id, _ = await create_run(h.ctx, conn, spec_id, mode, {k: k for k in keys}, params, scan)
        await repo.set_run_status(conn, run_id, "running")
    return run_id


async def scanned(h: Harness, spec_id: int | None = None, **state: Any) -> str:
    """A soak over two tables whose scans read them whole (``state``: otherwise)."""
    return await started(h, spec_id, scan=SCANNED | state, **SCAN)


def _result(key: str, read: Read) -> tuple[KeyResult, list[dict[str, Any]]]:
    outcome, findings = (read, ()) if isinstance(read, str) else read  # type: ignore[misc]
    provisional = outcome.endswith("?")  # a discrepancy still owed a recheck
    outcome = Outcome(outcome.rstrip("?"))
    side, reason = COMPLETE, None
    if outcome is Outcome.INCONCLUSIVE:
        side, reason = ABSENT, "absent_both"
    confirmation = Confirmation.PROVISIONAL if provisional else Confirmation.RECHECKED
    result = KeyResult(key, outcome, side, side, inconclusive_reason=reason, confirmation=confirmation)
    return result, [finding(*found) for found in findings]


async def ended(
    h: Harness,
    run_id: str,
    results: Mapping[str, Read],
    *,
    at: datetime | None = None,
    keys: Sequence[str] | None = None,
    counters: Mapping[str, Any] | bool | None = True,
    next_cycle: bool = True,
) -> int:
    """End the run's current cycle: each key of ``results`` read as it says (None, or a key it leaves out:
    never read, cancelled), its counters summarised from the stored rows and the rows its scan found
    identical (``counters``: others, or None for a cycle without its own) and its tombstone written at
    ``at``. A soak's next cycle then starts with the same keys, or ``keys``. The cycle ended."""
    async with h.ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        cycle, spec = run["cycle"], await h.ctx.spec(conn, run["spec_id"])
        for key, read in results.items():
            if read is not None:
                result, findings = _result(key, read)
                await repo.save_key_result(conn, run_id, cycle, key, result, findings)
        done = [key for key, read in results.items() if read is not None]
        await conn.execute(
            """UPDATE ds.run_keys SET state = CASE WHEN key_masked = ANY(%s) THEN 'done' ELSE 'cancelled' END
               WHERE run_id = %s AND cycle = %s""",
            (done, run_id, cycle),
        )
        if counters is True:
            rows = await repo.pages(repo.list_results, conn, run_id, cycle)
            found: dict[str, list[repo.Row]] = defaultdict(list)
            for row in await repo.pages(repo.list_findings, conn, run_id, cycle):
                found[row["key_masked"]].append(row)
            states = await repo.cycle_counts(conn, run_id, cycle)
            matched = (run["scan"] or {}).get("matched", 0)
            counters = count_cycle(
                rows, found, total=sum(states["keys"].values()) + matched, live=True,
                recheck_outcomes=RecheckPolicy.from_spec(spec.raw).outcomes, bulk_match=matched,
            )  # fmt: skip
        if counters is not None:
            await repo.set_counters(conn, run_id, dict(counters) | {"cycle": cycle})  # type: ignore[arg-type]
        await repo.end_cycle(conn, run_id, at)
        if run["mode"] == "soak" and next_cycle:
            sealer = await h.ctx.sealer(conn, run_id)
            sealed = None if keys is None else sealer.seal_keys({k: k for k in keys})
            scan = None if run["scan"] is None else dict(run["scan"])
            await repo.start_next_cycle(conn, run_id, run["created_at"], sealed, scan)
    return cycle


async def scored(conn: repo.Conn, run_id: str, cycle: int, by: Principal, reasons: Sequence[str] = ()) -> str:
    """An adjudication of the ended cycle drawn and scored by ``by``: passed, or not for ``reasons``."""
    drawn = await repo.insert_adjudication(
        conn, run_id, cycle, salt=f"{cycle:032d}", limit=1, selection={"candidates": 1, "keys": {}},
        sheet="key\r\n", drawn_by=by.actor,
    )  # fmt: skip
    exit_ = {"passed": not reasons, "minimum": 50, "reasons": list(reasons)}
    await repo.score_adjudication(
        conn, drawn["adjudication_id"], sheet="key\r\n", upload_sha256="sha256:" + "0" * 64,
        sheet_sha256="0" * 64, score={"adjudicated": 1, "agreed": 1, "disagreements": [], "exit": exit_},
        score_sha256="0" * 64, passed=not reasons, scored_by=by.actor,
    )  # fmt: skip
    return drawn["adjudication_id"]


async def signed(
    conn: repo.Conn, run_id: str, cycle: int, version: CompiledSpec, asked: Principal, *approvers: Principal
) -> str:
    """A sign-off of the ended cycle that ``asked`` requested and ``approvers`` approved (none: open)."""
    run = await repo.get_run(conn, run_id)
    assert run is not None
    facts = await approvals.attests(conn, run, cycle, version)
    row = await repo.create_approval(
        conn, "signoff", run["spec_id"], asked.actor, run_id=run_id, cycle=cycle, attests=facts,
        attests_sha256=approvals.attests_sha256(facts),
    )  # fmt: skip
    assert row is not None
    if approvers:
        await approve(conn, row["approval_id"], *approvers)
    return row["approval_id"]


async def packed(
    conn: repo.Conn, run_id: str, cycle: int, signoff_id: str, by: Principal, at: datetime
) -> str:
    """An evidence pack of the signed cycle, as its issue stores one: its id."""
    pack = {
        "pack_id": repo.new_pack_id(), "run_id": run_id, "cycle": cycle, "signoff_id": signoff_id,
        "adjudication_id": None, "manifest": {}, "sums_sha256": "a" * 64, "zip_sha256": "b" * 64,
        "key_fingerprint": "c" * 64, "audit_seq": 1, "issued_by": by.actor, "issued_at": at,
    }  # fmt: skip
    await repo.insert_pack(conn, pack, b"zip")
    return pack["pack_id"]


async def measure(h: Harness, *, backfill: bool = True) -> None:
    """A worker's readiness pass, its backfill batch due (or not)."""
    state = readiness.State() if backfill else readiness.State(backfilled_at=float("inf"))
    await readiness.watch(h.ctx, state)


async def measures(h: Harness, run_id: str, cycle: int = 1) -> dict[str, Any] | None:
    async with h.ctx.pool.connection() as conn:
        row = await repo.get_cycle(conn, run_id, cycle)
    assert row is not None
    return row["readiness"]


async def audited(h: Harness, action: str = "readiness.measured") -> list[repo.Row]:
    """The audit rows of ``action``, oldest first."""
    async with h.ctx.pool.connection() as conn:
        rows, _ = await repo.list_audit(conn, action=action, limit=1000)
    return rows[::-1]
