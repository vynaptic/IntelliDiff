"""Four-eyes approvals and sign-off (docs/mvp/APPROVALS.md §1-§4): what a version needs, the gate's
refusals, a run's approval state, each reason a cycle is not signed off, what a sign-off attests and a
revoke's ordered cascade."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal
from deltasleuth.connectors.base import utc_iso
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.types import (
    ChangeType,
    CompareConfig,
    CompiledSpec,
    Confirmation,
    Direction,
    KeyResult,
    KnownDiffRule,
    Outcome,
    RootCause,
    RuleClassification,
    Sample,
    SecurityPolicy,
    Severity,
    Side,
    SideConfig,
    SideState,
    SideStateKind,
    TableConfig,
)
from deltasleuth.runner import sample
from deltasleuth.runner.purge import CyclePurged
from deltasleuth.store import repo
from deltasleuth.store.db import migrate
from tests.conftest import TESTER
from tests.unit.store.four_eyes import (
    OIDC,
    TRUTH,
    approve,
    approved,
    counters,
    drawn,
    end,
    finding,
    labelling,
    mode,
    person,
    request_signoff,
    sampled,
    save,
    spec,
    start,
    token,
    upload,
)

BUG = KnownDiffRule("R-BUG", "true", RuleClassification.BUG, "a known defect", "PAY-1")
ACCEPTED = KnownDiffRule("R-OK", "true", RuleClassification.ACCEPTED, "rounding", "PAY-2")
# Values in clear, and rules that suppress findings.
PROFILE = spec("1.4.0", security=SecurityPolicy(clear=("$.status",)), known_diffs=(BUG, ACCEPTED))
NEEDS = ["clear", "known_diffs"]
REVOKED = "wrong clear path"


def table(**options: Any) -> dict[str, Any]:
    """A table side reading ``accounts`` with ``options`` (where, columns)."""
    side = SideConfig(Side.A, "legacy", "db-a", table=TableConfig("accounts", ("id",), **options))
    return {"sides": {Side.A: side}}


@pytest.mark.parametrize(
    ("members", "scopes"),
    [
        ({}, []),
        ({"security": SecurityPolicy(clear=("$.status",))}, ["clear"]),
        ({"security": SecurityPolicy(unlisted="clear")}, ["unlisted_clear"]),
        ({"known_diffs": (ACCEPTED,)}, ["known_diffs"]),
        ({"compare": CompareConfig(include=("$.a",))}, ["population_filters"]),
        ({"compare": CompareConfig(exclude=("$.a",))}, ["population_filters"]),
        ({"compare": CompareConfig(value_filter="true")}, ["population_filters"]),
        (table(where="status = 'A'"), ["population_filters"]),
        (table(columns=(("name", "name"),)), ["population_filters"]),
        (table(), []),  # a whole table
        # docs/mvp/FILES.md §5: csv.skip leaves each file's first lines out of what is compared, as a filter
        # does (a headerless export's first rows went unread with no approval at all); a dialect that skips
        # nothing gates nothing.
        (table(file={"path": "a.csv", "format": "csv", "csv": {"skip": 2}}), ["population_filters"]),
        (table(file={"path": "a.csv", "format": "csv", "csv": {"skip": 0, "header": False}}), []),
        (table(file={"path": "a.parquet", "format": "parquet"}), []),
        ({"sample": Sample("bottom_k", "cutover-q4", (), ("table:db-a/accounts",))}, ["sampling"]),
        (
            {"security": SecurityPolicy(clear=("$.x",), unlisted="clear"), "known_diffs": (BUG,)}
            | table(where="true"),
            ["clear", "unlisted_clear", "known_diffs", "population_filters"],
        ),
    ],
)
def test_needs_names_what_exposes_values_suppresses_findings_or_filters_keys(
    members: dict[str, Any], scopes: list[str]
) -> None:
    assert approvals.needs(spec(**members)) == scopes


@pytest.mark.parametrize(
    ("record", "approval", "state"),
    [
        ({"needs": NEEDS, "mode": "oidc"}, {"status": "approved"}, "approved"),
        ({"needs": NEEDS, "mode": "oidc"}, {"status": "revoked"}, "revoked"),
        ({"needs": NEEDS, "mode": "tokens"}, None, "unapproved"),
        ({"needs": [], "mode": "tokens"}, None, "not_needed"),
        (None, None, "unapproved"),  # a run from before 008
    ],
)
def test_a_run_s_approval_state(record: dict | None, approval: dict | None, state: str) -> None:
    assert approvals.run_state(record, approval) == state


DAY_1, DAY_2 = datetime(2026, 9, 27, 23, 30, tzinfo=UTC), datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
AMY, BOB = (
    {"actor": "user:U-1", "name": "amy", "at": DAY_1},
    {"actor": "user:U-2", "name": "bob", "at": DAY_2},
)
UNREVOKED = {"revoked_by": None, "revoked_by_name": None, "revoked_at": None, "reason": None}


@pytest.mark.parametrize(
    ("approval", "said"),
    [
        (
            {"state": "unapproved", "needs": NEEDS, "mode": "tokens"},
            "Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode"
            " has no approvers). Not sign-off evidence.",
        ),
        (
            {"state": "unapproved", "needs": ["clear"], "mode": "none"},
            "Unapproved spec version: clear was not approved by a second person (developer mode has no"
            " approvers). Not sign-off evidence.",
        ),
        (
            {"state": "unapproved", "needs": None, "mode": None},
            "Unapproved spec version: the run started before approvals (008). Not sign-off evidence.",
        ),
        (
            {"state": "not_needed", "needs": [], "mode": "oidc"},
            "The spec version needs no four-eyes approval.",
        ),
        (
            {"state": "approved", "approval_id": "A-1", "needs": NEEDS, "deciders": [BOB]} | UNREVOKED,
            "Spec approval A-1 was granted by bob on 2026-09-28.",
        ),
        (
            {"state": "approved", "approval_id": "A-1", "needs": NEEDS, "deciders": [AMY, BOB]} | UNREVOKED,
            "Spec approval A-1 was granted by amy on 2026-09-27 and bob on 2026-09-28.",
        ),
        (
            {"state": "revoked", "approval_id": "A-1", "needs": NEEDS, "deciders": [BOB]}
            | {"revoked_by_name": "jdoe", "revoked_at": DAY_1, "reason": REVOKED},
            f"Spec approval A-1 was revoked on 2026-09-27 by jdoe ({REVOKED}). Not sign-off evidence.",
        ),
    ],
)
def test_what_a_run_s_label_says(approval: dict[str, Any], said: str) -> None:
    assert approvals.label(approval) == said


def test_a_cycle_s_sign_off_label_names_the_approved_one_else_the_newest_revoked() -> None:
    def signoff(approval_id: str, status: str, **fields: Any) -> dict[str, Any]:
        steps = {
            f"{step}_{name}": None
            for step in ("first", "decided", "revoked")
            for name in ("by", "by_name", "at")
        }
        return steps | {"approval_id": approval_id, "status": status, "revoke_reason": None} | fields

    decided = {"decided_by": "user:U-2", "decided_by_name": "bob", "decided_at": DAY_2}
    revoked = {"revoked_by": "system:worker", "revoked_by_name": "system:worker", "revoked_at": DAY_2}
    newest_revoked = signoff("A-3", "revoked", **decided, **revoked, revoke_reason="cycle 1 purged")
    older_revoked = signoff(
        "A-2", "revoked", **decided, **revoked, revoke_reason="spec approval A-1 was revoked"
    )
    approved = signoff("A-1", "approved", first_by="user:U-1", first_by_name="amy", first_at=DAY_1, **decided)
    listed = [signoff("A-4", "requested"), newest_revoked, older_revoked, approved]
    assert approvals.signoff_label(listed) == (
        "Sign-off A-1 was granted by amy on 2026-09-27 and bob on 2026-09-28."
    )
    assert approvals.signoff_label([newest_revoked, older_revoked]) == (
        "Sign-off A-3 was revoked on 2026-09-28 by system:worker (cycle 1 purged). Not sign-off evidence."
    )
    assert approvals.signoff_label([signoff("A-4", "rejected", **decided)]) is None


# -- the gate and sign-offs, on the store ----------------------------------------------------------


@dataclasses.dataclass
class World:
    bob: Principal  # the spec's author
    carol: Principal  # who approved it
    dave: Principal  # the operator who starts runs
    spec_id: int
    approval_id: str


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    await migrate(pool)
    yield pool
    await pool.close()


@pytest.fixture
async def world(pool: AsyncConnectionPool) -> World:
    """Customer-profile 1.4.0, uploaded by Bob and approved by Carol."""
    async with pool.connection() as conn:
        bob, carol, dave = [await person(conn, name) for name in ("bob", "carol", "dave")]
        spec_id = await upload(conn, PROFILE, bob)
        approval_id = await approved(conn, spec_id, bob, carol, scopes=NEEDS)
    return World(bob, carol, dave, spec_id, approval_id)


async def _gate(
    pool: AsyncConnectionPool, spec_id: int, version: CompiledSpec = PROFILE, ctx: Any = OIDC
) -> Any:
    async with pool.connection() as conn:
        return await approvals.gate(ctx, conn, spec_id, version)


async def _refused(pool: AsyncConnectionPool, spec_id: int, version: CompiledSpec = PROFILE) -> str:
    with pytest.raises(approvals.ApprovalRequired) as refused:
        await _gate(pool, spec_id, version)
    return str(refused.value)


async def _revoke(pool: AsyncConnectionPool, approval_id: str, who: Principal = TESTER) -> dict[str, Any]:
    async with repo.audited(pool) as (conn, audit):
        return await approvals.revoke(conn, audit, approval_id, REVOKED, who=who)


@pytest.mark.db
@pytest.mark.parametrize("auth_mode", ["tokens", "none"])
async def test_tokens_and_developer_modes_have_no_approvers(
    pool: AsyncConnectionPool, auth_mode: str
) -> None:
    async with pool.connection() as conn:  # not even a stored body that does not re-hash is refused
        spec_id = await upload(conn, dataclasses.replace(PROFILE, content_hash="0" * 64), TESTER)
    assert await _gate(pool, spec_id, ctx=mode(auth_mode)) == (None, {"needs": NEEDS, "mode": auth_mode})


@pytest.mark.db
async def test_the_gate_gives_the_active_approval(pool: AsyncConnectionPool, world: World) -> None:
    assert await _gate(pool, world.spec_id) == (world.approval_id, {"needs": NEEDS, "mode": "oidc"})


@pytest.mark.db
async def test_the_gate_refuses_a_version_that_needs_an_approval_it_lacks(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        spec_id = await upload(conn, PROFILE, TESTER)
    assert await _refused(pool, spec_id) == (
        f"spec {spec_id} (customer-profile 1.4.0) needs a four-eyes approval of clear, known_diffs before it"
        " runs"
    )


@pytest.mark.db
async def test_the_gate_names_a_revoked_approval(pool: AsyncConnectionPool, world: World) -> None:
    await _revoke(pool, world.approval_id)
    assert await _refused(pool, world.spec_id) == (
        f"spec {world.spec_id} (customer-profile 1.4.0) needs a new four-eyes approval: {world.approval_id}"
        " was revoked"
    )


@pytest.mark.db
async def test_the_gate_refuses_an_approval_lacking_a_scope_this_build_honours(
    pool: AsyncConnectionPool, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear = spec("1.5.0", security=SecurityPolicy(clear=("$.status",)))
    async with pool.connection() as conn:
        spec_id = await upload(conn, clear, world.bob)
        approval_id = await approved(conn, spec_id, world.bob, world.carol, scopes=["clear"])
    monkeypatch.setattr(approvals, "needs", lambda _: ["clear", "raw_clear"])  # a build that honours it
    assert await _refused(pool, spec_id, clear) == (
        f"spec {spec_id} (customer-profile 1.5.0) needs a new four-eyes approval: {approval_id} covers clear;"
        " this build also needs raw_clear"
    )


@pytest.mark.db
async def test_the_gate_refuses_a_stored_body_that_does_not_rehash(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        spec_id = await upload(conn, dataclasses.replace(PROFILE, content_hash="0" * 64), TESTER)
    assert await _refused(pool, spec_id) == (
        f"spec {spec_id}'s stored body does not re-hash to its content hash (edited, or not storable as"
        " uploaded): upload a changed version (a new version)"
    )


@pytest.mark.db
async def test_a_version_that_needs_nothing_runs_under_its_approval_if_it_has_one(
    pool: AsyncConnectionPool, world: World
) -> None:
    plain = spec("2.0.0")
    async with pool.connection() as conn:
        spec_id = await upload(conn, plain, world.bob)
    assert await _gate(pool, spec_id, plain) == (None, {"needs": [], "mode": "oidc"})
    async with pool.connection() as conn:
        approval_id = await approved(conn, spec_id, world.bob, world.carol)
    assert await _gate(pool, spec_id, plain) == (approval_id, {"needs": [], "mode": "oidc"})


async def _ended(
    pool: AsyncConnectionPool,
    world: World,
    cycle_counters: dict[str, Any] | None = None,
    *,
    ended: bool = True,
    **options: Any,
) -> str:
    """A completed run of the world's version by Dave, under its approval unless ``options`` say, whose
    cycle 1 ended with ``cycle_counters`` (default: complete; None in ``options``: none of its own)."""
    options = {"approval_id": world.approval_id, "status": "completed"} | options
    async with pool.connection() as conn:
        run_id = await start(conn, world.spec_id, world.dave, **options)
        if ended:
            await end(conn, run_id, counters(1) if cycle_counters is None else cycle_counters)
    return run_id


async def _problems(pool: AsyncConnectionPool, run_id: str, cycle: int = 1, **options: Any) -> list[str]:
    version = options.pop("version", PROFILE)
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        return await approvals.signoff_problems(conn, run, cycle, version, **options)


def _unreconciled() -> dict[str, Any]:
    broken = counters(1)
    broken["reconciliation"][0]["holds"] = False
    return broken


@pytest.mark.db
async def test_a_complete_approved_cycle_of_a_live_run_can_be_signed_off(
    pool: AsyncConnectionPool, world: World
) -> None:
    assert await _problems(pool, await _ended(pool, world)) == []


# case -> (the run's options, its cycle's counters, the refusal)
REFUSALS: dict[str, tuple[dict[str, Any], dict[str, Any] | None, str]] = {
    "one_shot": ({"mode": "one_shot"}, None, "a one-shot run is not signed off"),
    "samples": (
        {"params": {"samples_dir": "/p0/samples-1"}}, None,
        "run {run} read captured samples: only a live run is signed off",
    ),
    "partial": (
        {}, counters(1, cancelled=12, owed=3),
        "cycle 1 of run {run} is partial: 12 key(s) cancelled, 3 owed a recheck",
    ),
    "unreconciled": (
        {}, _unreconciled(),
        "cycle 1 of run {run} does not reconcile: total = match + different + indeterminate + cancelled"
        " + outstanding",
    ),
    "nothing_compared": ({}, counters(1, matched=0), "cycle 1 of run {run} compared no key"),
    "cut": (
        {"keys_cut": {"resolved": 12000, "taken": 10000}}, None,
        "cycle 1 of run {run} was cut at 10000 keys (maxKeys)",
    ),
    "unapproved": ({"approval_id": None}, None, "cycle 1 of run {run} ran without a spec approval"),
    "tokens": (
        {"params": {"approval": {"needs": NEEDS, "mode": "tokens"}}}, None,
        "run {run} started in tokens mode: only a run started under SSO is signed off",
    ),
    "developer": (
        {"params": {"approval": {"needs": NEEDS, "mode": "none"}}}, None,
        "run {run} started in developer mode: only a run started under SSO is signed off",
    ),
}  # fmt: skip


@pytest.mark.db
@pytest.mark.parametrize("case", list(REFUSALS))
async def test_each_refusal_of_a_signoff(pool: AsyncConnectionPool, world: World, case: str) -> None:
    options, cycle_counters, refusal = REFUSALS[case]
    run_id = await _ended(pool, world, cycle_counters, **options)
    assert await _problems(pool, run_id) == [refusal.format(run=run_id)]


@pytest.mark.db
async def test_a_cycle_without_counters_of_its_own_is_refused(
    pool: AsyncConnectionPool, world: World
) -> None:
    run_id = await _ended(pool, world, ended=False)
    async with pool.connection() as conn:
        await end(conn, run_id)  # as a failed, erased or abandoned cycle ends
    assert await _problems(pool, run_id) == [
        f"cycle 1 of run {run_id} has no counters of its own (failed, erased or abandoned)"
    ]


@pytest.mark.db
async def test_a_cycle_that_has_not_ended_is_refused(pool: AsyncConnectionPool, world: World) -> None:
    run_id = await _ended(pool, world, ended=False, status="running")
    assert await _problems(pool, run_id) == [f"cycle 1 of run {run_id} has not ended"]


@pytest.mark.db
async def test_a_run_started_before_008_is_never_signed_off(pool: AsyncConnectionPool, world: World) -> None:
    run_id = await _ended(pool, world)
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE ds.runs SET created_at = (SELECT applied_at FROM ds.schema_version WHERE version = 8)
                   - interval '1 second'
               WHERE run_id = %s""",
            (run_id,),
        )
    assert await _problems(pool, run_id) == [
        f"run {run_id} started before approvals (008): only later runs are signed off"
    ]


@pytest.mark.db
async def test_a_purged_cycle_is_gone(pool: AsyncConnectionPool, world: World) -> None:
    run_id = await _ended(pool, world)
    async with pool.connection() as conn:
        await repo.start_purge(conn, run_id, 1, "retention standard")
    with pytest.raises(
        CyclePurged, match=rf"^cycle 1 of run {run_id} was purged on .* \(retention standard\)"
    ):
        await _problems(pool, run_id)


@pytest.mark.db
async def test_unexplained_critical_findings_count_bug_rules(pool: AsyncConnectionPool, world: World) -> None:
    run_id = await _ended(pool, world, ended=False)
    async with pool.connection() as conn:
        await save(conn, run_id, "k1", finding())
        await save(conn, run_id, "k2", finding(rule="R-BUG", classification="bug"))
        await save(conn, run_id, "k3", finding(rule="R-OK", classification="accepted"), finding("HIGH"))
        await end(conn, run_id, counters(1))
    assert await _problems(pool, run_id) == [
        f"cycle 1 of run {run_id} has 2 key(s) with unexplained CRITICAL findings (bug rules count as"
        " unexplained)"
    ]


@pytest.mark.db
async def test_a_cycle_run_under_a_revoked_approval_is_refused(
    pool: AsyncConnectionPool, world: World
) -> None:
    run_id = await _ended(pool, world)
    async with pool.connection() as conn:
        signoff = await request_signoff(conn, run_id, 1, world.dave, PROFILE)
    await _revoke(pool, world.approval_id)
    refusal = f"cycle 1 of run {run_id} ran under spec approval {world.approval_id}, since revoked"
    assert await _problems(pool, run_id) == [refusal]
    async with pool.connection() as conn:
        request = await repo.get_approval(conn, signoff)
    assert request is not None
    cited = request["attests"]["spec_approval_id"]
    assert await _problems(pool, run_id, cited=cited, lock=True) == [refusal]  # at its decision


@pytest.mark.db
async def test_a_version_needing_nothing_cites_its_active_approval(
    pool: AsyncConnectionPool, world: World
) -> None:
    plain = spec("2.0.0")
    async with pool.connection() as conn:
        spec_id = await upload(conn, plain, world.bob)
        run_id = await start(conn, spec_id, world.dave, status="completed")  # it ran under none
        await end(conn, run_id, counters(1))
    assert await _problems(pool, run_id, version=plain) == [
        f"cycle 1 of run {run_id} ran without a spec approval"
    ]
    async with pool.connection() as conn:
        approval_id = await approved(conn, spec_id, world.bob, world.carol)
        run = await repo.get_run(conn, run_id)
        facts = await approvals.attests(conn, run, 1, plain)  # type: ignore[arg-type]
    assert await _problems(pool, run_id, version=plain) == [] and facts["spec_approval_id"] == approval_id


@pytest.mark.db
async def test_a_run_started_outside_sso_is_refused_though_its_version_needs_nothing(
    pool: AsyncConnectionPool, world: World
) -> None:
    plain = spec("2.0.0")
    async with pool.connection() as conn:
        spec_id = await upload(conn, plain, world.bob)
        await approved(conn, spec_id, world.bob, world.carol)  # which a sign-off would cite
        params = {"approval": {"needs": [], "mode": "none"}}  # a CLI --config without auth.oidc
        run_id = await start(conn, spec_id, "local:ops@host", status="completed", params=params)
        await end(conn, run_id, counters(1))
    assert await _problems(pool, run_id, version=plain) == [
        f"run {run_id} started in developer mode: only a run started under SSO is signed off"
    ]


@pytest.mark.db
async def test_a_table_scan_that_stopped_before_the_end_is_refused_and_attested(
    pool: AsyncConnectionPool, world: World
) -> None:
    run_id = await _ended(pool, world, params={"keys_source": "table_scan"})
    scan = {"done": False, "after": None, "rows_a": 10, "rows_b": 9, "skipped_a": 1, "skipped_b": 0,
            "matched": 8, "candidates": 1, "checkpoint": 1}  # fmt: skip
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 1, %s)", (run_id, Jsonb(scan))
        )
        run = await repo.get_run(conn, run_id)
        facts = await approvals.attests(conn, run, 1, PROFILE)  # type: ignore[arg-type]
    assert await _problems(pool, run_id) == [
        f"cycle 1 of run {run_id} scanned only part of its tables (rows_a 10, rows_b 9 read; the scan stopped"
        " before the end)"
    ]
    assert facts["scan"] == {name: scan[name] for name in scan if name not in ("after", "checkpoint")}


@pytest.mark.db
async def test_a_cycle_is_labelled_by_the_approval_it_ran_under(
    pool: AsyncConnectionPool, world: World
) -> None:
    run_id = await _ended(pool, world, mode="soak", status="running")  # cycle 1 ended under the approval
    await _revoke(pool, world.approval_id, world.carol)
    async with pool.connection() as conn:
        again = await approved(conn, world.spec_id, world.bob, world.dave, scopes=NEEDS)
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        await repo.set_run_approval(conn, run_id, again)  # the resume
        run = await repo.get_run(conn, run_id)
        said = [
            approvals.label(await approvals.run_approval(conn, run, cycle))  # type: ignore[arg-type]
            for cycle in (1, 2, None)
        ]
    revoked = f"Spec approval {world.approval_id} was revoked on "
    granted = f"Spec approval {again} was granted by dave on "
    assert [said[0].startswith(revoked), said[1].startswith(granted), said[2] == said[1]] == [True] * 3


@pytest.mark.db
async def test_a_run_from_before_008_stays_unapproved_whatever_it_attaches(
    pool: AsyncConnectionPool, world: World
) -> None:
    run_id = await _ended(pool, world, mode="soak", status="running", params={"approval": None})
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE ds.runs SET created_at = (SELECT applied_at FROM ds.schema_version WHERE version = 8)
                   - interval '1 second'
               WHERE run_id = %s""",
            (run_id,),
        )
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        await repo.set_run_approval(conn, run_id, world.approval_id)  # a boundary attach
        run = await repo.get_run(conn, run_id)
        shown = [await approvals.run_approval(conn, run, cycle) for cycle in (1, 2, None)]  # type: ignore[arg-type]
    assert [(a["state"], a["needs"], a["mode"]) for a in shown] == [("unapproved", None, None)] * 3
    assert {approvals.label(a) for a in shown} == {
        f"Unapproved spec version: the run started before approvals (008). {approvals.NOT_EVIDENCE}"
    }


@pytest.mark.db
async def test_a_cancelled_soak_s_completed_cycle_passes_and_its_last_is_refused(
    pool: AsyncConnectionPool, world: World
) -> None:
    run_id = await _ended(pool, world, mode="soak", status="running")
    async with pool.connection() as conn:
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        await repo.set_run_status(conn, run_id, "cancelling", stop_reason="cancelled")
        await repo.cancel_keys(conn, run_id)
        await end(conn, run_id, counters(2, matched=1, cancelled=2))
        await repo.set_run_status(conn, run_id, "cancelled")
    assert await _problems(pool, run_id, 1) == []
    assert await _problems(pool, run_id, 2) == [
        f"cycle 2 of run {run_id} is partial: 2 key(s) cancelled, 0 owed a recheck"
    ]


@pytest.mark.db
async def test_a_capped_run_revoked_approved_again_and_resumed_is_still_refused(
    pool: AsyncConnectionPool, world: World
) -> None:
    async with pool.connection() as conn:
        run_id = await start(conn, world.spec_id, world.dave, approval_id=world.approval_id,
                             keys_cut={"resolved": 5, "taken": 3})  # fmt: skip
        await repo.set_run_status(conn, run_id, "running", stop_reason="stop_condition:maxKeys")
    assert (await _revoke(pool, world.approval_id, world.carol))["paused"] == [run_id]
    async with pool.connection() as conn:
        again = await approved(conn, world.spec_id, world.bob, world.carol, scopes=NEEDS)
    async with pool.connection() as conn:  # the resume
        gated, _ = await approvals.gate(OIDC, conn, world.spec_id, PROFILE, lock=True)
        cleared = (approvals.APPROVAL_PAUSE,)
        assert await repo.set_run_status(conn, run_id, "running", when=("paused",), cleared=cleared)
        await repo.set_run_approval(conn, run_id, gated)
    async with pool.connection() as conn:
        await end(conn, run_id, counters(1))
        run = await repo.get_run(conn, run_id)
    assert run is not None
    assert (run["approval_id"], run["stop_reason"]) == (
        again, None  # the pause replaced the cut's stop reason
    )  # fmt: skip
    assert await _problems(pool, run_id) == [f"cycle 1 of run {run_id} was cut at 3 keys (maxKeys)"]


@pytest.mark.db
async def test_a_rolling_soak_s_capped_cycle_is_refused(pool: AsyncConnectionPool, world: World) -> None:
    run_id = await _ended(pool, world, mode="soak", status="running", params={"soak": {"rolling": True}})
    async with pool.connection() as conn:
        await repo.set_keys_cut(conn, run_id, {"resolved": 5, "taken": 3})  # its next listing was cut
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        await end(conn, run_id, counters(2))
    assert await _problems(pool, run_id, 1) == []
    assert await _problems(pool, run_id, 2) == [f"cycle 2 of run {run_id} was cut at 3 keys (maxKeys)"]


@pytest.mark.db
async def test_attests_are_stable_and_list_each_rule_and_root_cause(
    pool: AsyncConnectionPool, world: World
) -> None:
    cause = RootCause(
        "RC-" + "a" * 12, "signature", "$.x", ChangeType.VALUE_CHANGED, Direction.BOTH, affected_keys=1,
        finding_count=1, max_severity=Severity.HIGH, taxonomy_code="VALUE_MISMATCH",
    )  # fmt: skip
    cycle_counters = counters(1) | {"suppressed_by_rule": {"R-BUG": 1}}
    run_id = await _ended(pool, world, ended=False)
    async with pool.connection() as conn:
        await save(conn, run_id, "k1", finding("HIGH", "R-BUG", "bug"))
        await repo.replace_root_causes(conn, run_id, 1, [cause], {})
        await end(conn, run_id, cycle_counters)
        run, ended = await repo.get_run(conn, run_id), await repo.get_cycle(conn, run_id, 1)
        facts = await approvals.attests(conn, run, 1, PROFILE)  # type: ignore[arg-type]
        again = await approvals.attests(conn, run, 1, PROFILE)  # type: ignore[arg-type]
        rebuilt = await approvals.rebuilt(conn, run, 1, PROFILE, facts)  # type: ignore[arg-type]
        before_packs = await approvals.rebuilt(conn, run, 1, PROFILE, {"run_id": run_id})  # type: ignore[arg-type]
        before_sampling = await approvals.rebuilt(conn, run, 1, PROFILE, {"format": 1})  # type: ignore[arg-type]
        before_accuracy = await approvals.rebuilt(conn, run, 1, PROFILE, {"format": 2})  # type: ignore[arg-type]
        unknown = await approvals.rebuilt(conn, run, 1, PROFILE, {"format": 4})  # type: ignore[arg-type]
        await repo.replace_root_causes(
            conn, run_id, 1, [cause, dataclasses.replace(cause, root_cause_id="RC-" + "b" * 12)], {}
        )
        changed = await approvals.attests(conn, run, 1, PROFILE)  # type: ignore[arg-type]
    assert ended is not None
    format_0 = {
        "run_id": run_id,
        "cycle": 1,
        "mode": "batch",
        "auth_mode": "oidc",
        "keys_source": "explicit",
        "scan": None,
        "spec_id": world.spec_id,
        "content_hash": PROFILE.content_hash,
        "spec_approval_id": world.approval_id,
        "created_by": world.dave.actor,
        "ended_at": utc_iso(ended["ended_at"]),
        "counters": cycle_counters,
        "unexplained_critical": 0,
        "rules": [
            {"rule_id": "R-BUG", "when": "true", "classification": "bug", "reason": "a known defect",
             "ticket": "PAY-1", "expires": None, "keys": 1},
            {"rule_id": "R-OK", "when": "true", "classification": "accepted", "reason": "rounding",
             "ticket": "PAY-2", "expires": None, "keys": 0},
        ],
        "root_causes": [
            {"root_cause_id": "RC-" + "a" * 12, "taxonomy_code": "VALUE_MISMATCH", "max_severity": "HIGH",
             "change_type": "value_changed", "path_pattern": "$.x", "affected_keys": 1},
        ],
        "claim": "observed",
    }  # fmt: skip
    # Format 1 (docs/mvp/PACK.md §1.2) names the same rule fields, and adds its census and adjudication;
    # format 2 (docs/mvp/SAMPLING.md §10) adds a sampled cycle's draw, null for a census cycle; format 3
    # (docs/mvp/ACCURACY.md §8) the cycle's labellings, counted, and the one it attests, null without one.
    census = {"cycle": 1, "at": None, "a": None, "b": None}  # explicit keys, no table: nothing counted
    format_1 = format_0 | {"format": 1, "census": census, "adjudication": None}
    format_2 = format_1 | {"format": 2, "sample": None}
    assert facts == format_2 | {"format": 3, "labellings": {"drawn": 0, "labelled": 0}, "accuracy": None}
    assert rebuilt == facts and before_packs == format_0 and before_sampling == format_1
    assert before_accuracy == format_2 and unknown is None
    digest = approvals.attests_sha256(facts)
    assert re.fullmatch("[0-9a-f]{64}", digest) and approvals.attests_sha256(again) == digest
    assert approvals.attests_sha256(changed) != digest


# -- labelled accuracy (docs/mvp/ACCURACY.md §8) ---------------------------------------------------


async def _attested(pool: AsyncConnectionPool, run_id: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        return await approvals.attests(conn, run, 1, PROFILE)  # type: ignore[arg-type]


@pytest.mark.db
async def test_a_sign_off_attests_the_newest_labelling_a_person_labelled(
    pool: AsyncConnectionPool, world: World
) -> None:
    """Format 3: a person's labelling over a token's, developer mode's and a newer unlabelled draw; its
    exact counts and hashes; rebuilt from the labelling it names, and stable across the cycle's purge."""
    run_id = await _ended(pool, world)
    async with pool.connection() as conn:
        frank = await person(conn, "frank", "operator")
        bot = await token(conn, "ci-bot", frank)
        first = await labelling(conn, run_id, world.dave, frank)
        by_token = await labelling(conn, run_id, world.dave, bot)
        developer = Principal("dev:loopback", "developer", frozenset())
        in_developer_mode = await labelling(conn, run_id, world.dave, developer)
        unlabelled = await labelling(conn, run_id, world.dave)
    facts = await _attested(pool, run_id)
    counted = dict.fromkeys(("A_RIGHT", "B_RIGHT", "BOTH_WRONG", "BOTH_RIGHT", "UNDECIDABLE"), 0)
    assert facts["labellings"] == {"drawn": 4, "labelled": 3}
    assert facts["format"] == 3 and facts["adjudication"] is None and facts["accuracy"] == {
        "labelling_id": first["labelling_id"], "salt": first["salt"], "truth": TRUTH, "unlocatable": False,
        "selection_sha256": first["selection_sha256"], "labels_sha256": first["labels_sha256"],
        "score_sha256": first["score_sha256"], "drawn_by": world.dave.actor, "labelled_by": frank.actor,
        "labelled_at": utc_iso(first["labelled_at"]),
        "labeller": {"started_run": False, "spec_author": False},
        "discrepant": {"items": 1, "uniform": 1, "census": True, "labels": counted | {"B_RIGHT": 1},
                       "reasons": {}},
        "accepted": None,
        "agreed": {"keys": 2, "drawn": 1, "labels": counted | {"BOTH_RIGHT": 1}},
    }  # fmt: skip
    others = {v["labelling_id"] for v in (by_token, in_developer_mode, unlabelled)}
    assert first["labelling_id"] < min(others)  # the newest a person labelled, though the oldest drawn
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        rebuilt = await approvals.rebuilt(conn, run, 1, PROFILE, facts)  # type: ignore[arg-type]
        # A labelling is never attested as an adjudication, nor an adjudication as a labelling.
        crossed = facts | {"adjudication": {"adjudication_id": first["labelling_id"]},
                           "accuracy": {"labelling_id": "J-" + "0" * 26}}  # fmt: skip
        neither = await approvals.rebuilt(conn, run, 1, PROFILE, crossed)  # type: ignore[arg-type]
        assert await repo.cycle_adjudication(conn, run_id, 1, first["labelling_id"]) is None
        assert await repo.cycle_labelling(conn, run_id, 2, first["labelling_id"]) is None
        # The cycle's purge keeps what the sign-off attests of its labelling.
        assert await repo.start_purge(conn, run_id, 1, "erasure: a request")
        await repo.purge_keys(conn, run_id, 1, 0, 10)
        await repo.finish_purge(conn, run_id, 1, TESTER.actor)
        purged = await approvals.rebuilt(conn, run, 1, PROFILE, facts)  # type: ignore[arg-type]
        tombstone = await repo.get_labelling(conn, first["labelling_id"])
    assert rebuilt == facts and approvals.attests_sha256(rebuilt) == approvals.attests_sha256(facts)
    assert neither is not None and (neither["adjudication"], neither["accuracy"]) == (None, None)
    assert tombstone is not None and tombstone["purged_at"] is not None and tombstone["sheet"] is None
    assert purged is not None and purged["accuracy"] == facts["accuracy"]


@pytest.mark.db
async def test_a_labeller_who_started_the_run_or_wrote_its_spec_is_attested_as_such(
    pool: AsyncConnectionPool, world: World
) -> None:
    """Independence is attested, never refused (§4): the run's starter and the spec approval's author may
    label, and the sign-off says so."""
    by_starter, by_author, by_token = [await _ended(pool, world) for _ in range(3)]
    async with pool.connection() as conn:
        bot = await token(conn, "bob-bot", world.bob)  # bob's token: a labelling it labels is not attested
        await labelling(conn, by_starter, world.carol, world.dave)
        await labelling(conn, by_author, world.dave, world.bob)
        mine = await labelling(conn, by_token, world.dave, bot)
        shown = await repo.get_labelling(conn, mine["labelling_id"])
    attested = [await _attested(pool, run_id) for run_id in (by_starter, by_author, by_token)]
    labellers = [facts["accuracy"] for facts in attested]
    assert [found and found["labeller"] for found in labellers] == [
        {"started_run": True, "spec_author": False}, {"started_run": False, "spec_author": True}, None,
    ]  # fmt: skip
    # A labelling no person labelled is attested by none, and counted: the sign-off attests that it exists.
    assert [facts["labellings"] for facts in attested] == [{"drawn": 1, "labelled": 1}] * 3
    async with pool.connection() as conn:
        run = await repo.get_run(conn, by_token)
        assert await approvals.rebuilt(conn, run, 1, PROFILE, attested[2]) == attested[2]  # type: ignore[arg-type]
    assert shown is not None and (shown["started_run"], shown["spec_author"]) == (False, world.approval_id)
    assert (shown["labelled_by_name"], shown["drawn_by_name"]) == ("bob-bot", "dave")


@pytest.mark.db
async def test_scheduled_run_needs_two_approvers(pool: AsyncConnectionPool, world: World) -> None:
    """A schedule's run is created by ``system:worker`` (docs/mvp/SCHEDULES.md §6): nobody answers for its
    start, so its authorship is unknown (§3) and its sign-off takes two approvers, of whom who saved the
    schedule may be one; a run a person started is judged as before, with one."""
    by_hand = await _ended(pool, world)  # Dave's
    scheduled, again = [
        await _ended(pool, world, params={"schedule": {"schedule_id": "SC-" + "0" * 26, "due": due}})
        for due in ("2026-10-07T01:00Z", "2026-10-08T01:00Z")
    ]
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.runs SET created_by = 'system:worker' WHERE run_id = ANY(%s)", ([scheduled, again],)
        )
        erin = await person(conn, "erin")
        assert await _rows_of(
            conn, "SELECT ds.attributed('system:worker'), ds.accountable('system:worker')"
        ) == [(False, [])]
        assert await _problems(pool, scheduled) == await _problems(pool, by_hand) == []
        mine = await labelling(conn, scheduled, erin, world.dave)  # ACCURACY: Dave started no scheduled run
        assert (await repo.get_labelling(conn, mine["labelling_id"]))["started_run"] is False  # type: ignore[index]
        requests = [
            await request_signoff(conn, run_id, 1, world.dave, PROFILE) for run_id in (by_hand, scheduled)
        ]
        needed = [(await repo.get_approval(conn, a))["approvers_needed"] for a in requests]  # type: ignore[index]
    assert needed == [1, 2]
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="needs 2 approver"):
            await repo.decide_approval(conn, requests[1], world.carol.actor, approve=True)
    async with pool.connection() as conn:
        one = await approve(conn, requests[0], world.carol)
        two = await approve(conn, requests[1], world.carol, await person(conn, "frank"))
        assert (one["status"], one["unattributed"], one["first_by"]) == ("approved", False, None)
        assert (two["status"], two["unattributed"], two["authors"]) == (
            "approved",
            True,
            sorted([world.bob.actor, world.dave.actor]),  # the spec's author and who asked: no starter
        )
        # Who saved the schedule is excluded by nothing: asked by Erin, Dave is one of the two.
        other = await request_signoff(conn, again, 1, erin, PROFILE)
        saved = await approve(conn, other, world.dave, world.carol)
        assert (saved["status"], saved["first_by"], saved["decided_by"]) == (
            "approved",
            world.dave.actor,
            world.carol.actor,
        )


async def _rows_of(conn: repo.Conn, sql: str) -> list[tuple]:
    return await (await conn.execute(sql)).fetchall()  # type: ignore[arg-type]


# -- revokes ---------------------------------------------------------------------------------------


async def _signed(pool: AsyncConnectionPool, world: World, run_id: str, cycle: int = 1) -> str:
    """Dave's sign-off of the run's ended cycle, approved by Carol, holding the run as the decision does."""
    async with pool.connection() as conn:
        signoff = await request_signoff(conn, run_id, cycle, world.dave, PROFILE)
        await approve(conn, signoff, world.carol)
        await repo.set_hold(conn, run_id, f"{approvals.SIGNED_OFF}{signoff}", world.carol.actor)
    return signoff


async def _rows(pool: AsyncConnectionPool, sql: str, *params: Any) -> list[tuple]:
    async with pool.connection() as conn:
        return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


@pytest.mark.db
async def test_revoking_a_spec_approval_cascades_in_order(pool: AsyncConnectionPool, world: World) -> None:
    async with pool.connection() as conn:
        erin = await person(conn, "erin")
        running = await start(conn, world.spec_id, world.dave, approval_id=world.approval_id)
        one_shot = await start(
            conn, world.spec_id, world.dave, approval_id=world.approval_id, mode="one_shot", status="queued"
        )
    signed_run, open_run = await _ended(pool, world), await _ended(pool, world)
    signed = await _signed(pool, world, signed_run)
    async with pool.connection() as conn:
        open_signoff = await request_signoff(conn, open_run, 1, world.dave, PROFILE)
    done = await _revoke(pool, world.approval_id, erin)
    cascade = f"spec approval {world.approval_id} was revoked"
    assert done == {
        "approval_id": world.approval_id, "status": "revoked", "paused": [running], "stopped": [one_shot],
        "revoked": [signed],
    }  # fmt: skip
    assert await _rows(
        pool,
        "SELECT run_id, status, stop_reason, hold_reason FROM ds.runs WHERE run_id = ANY(%s) ORDER BY run_id",
        [running, one_shot, signed_run],
    ) == [
        (running, "paused", approvals.APPROVAL_PAUSE, None),
        (one_shot, "cancelling", approvals.APPROVAL_REVOKED, None),
        (signed_run, "completed", None, None),  # its sign-off hold released
    ]  # fmt: skip
    assert await _rows(pool, "SELECT DISTINCT state FROM ds.run_keys WHERE run_id = %s", one_shot) == [
        ("cancelled",)
    ]
    assert await _rows(
        pool,
        """SELECT approval_id, status, revoked_by, revoke_reason, decided_by, decision_note FROM ds.approvals
           ORDER BY approval_id""",
    ) == [
        (world.approval_id, "revoked", erin.actor, REVOKED, world.carol.actor, None),
        (signed, "revoked", erin.actor, cascade, world.carol.actor, None),
        (open_signoff, "rejected", None, None, erin.actor, cascade),  # a person revoker rejects it
    ]  # fmt: skip
    approval_id = world.approval_id
    audited = await _rows(
        pool,
        "SELECT actor, action, object, detail FROM ds.audit_log WHERE actor = %s ORDER BY id",
        erin.actor,
    )
    assert audited == [
        (erin.actor, "paused", running, {"reason": "run_paused_approval", "approval_id": approval_id}),
        (erin.actor, "cancelling", one_shot, {"reason": "approval_revoked", "approval_id": approval_id}),
        (erin.actor, "approval.revoke", approval_id,
         {"reason": REVOKED, "paused": [running], "stopped": [one_shot], "cascade_of": None}),
        (erin.actor, "approval.revoke", signed,
         {"reason": cascade, "paused": [], "stopped": [], "cascade_of": approval_id}),
        (erin.actor, "approval.reject", open_signoff, {"kind": "signoff", "cascade_of": approval_id}),
        (erin.actor, "retention.release", signed_run, {"reason": f"signed off: {signed}"}),
    ]  # fmt: skip


@pytest.mark.db
async def test_a_cli_revoke_leaves_an_open_signoff_to_be_refused_at_its_decision(
    pool: AsyncConnectionPool, world: World
) -> None:
    run_id = await _ended(pool, world)
    async with pool.connection() as conn:
        signoff = await request_signoff(conn, run_id, 1, world.dave, PROFILE)
    await _revoke(pool, world.approval_id)
    async with pool.connection() as conn:
        request = await repo.get_approval(conn, signoff)
    assert request is not None and request["status"] == "requested"
    async with pool.connection() as conn:
        with pytest.raises(repo.ApprovalRefused, match="cites is not approved"):
            await repo.decide_approval(conn, signoff, world.carol.actor, approve=True)


@pytest.mark.db
async def test_revoking_a_signoff_releases_only_its_own_hold_once_none_is_left(
    pool: AsyncConnectionPool, world: World
) -> None:
    soak = await _ended(pool, world, mode="soak", status="running")
    async with pool.connection() as conn:
        await repo.start_next_cycle(conn, soak, datetime.now(UTC))
        await end(conn, soak, counters(2))
    first = await _signed(pool, world, soak)
    async with pool.connection() as conn:  # the run is held: the second sign-off keeps the first's hold
        second = await request_signoff(conn, soak, 2, world.dave, PROFILE)
        await approve(conn, second, world.carol)
    legal = await _ended(pool, world)
    legal_signoff = await _signed(pool, world, legal)
    async with pool.connection() as conn:
        await repo.set_hold(conn, legal, None, None)
        await repo.set_hold(conn, legal, "legal hold LH-7", TESTER.actor)
    hold = "SELECT hold_reason FROM ds.runs WHERE run_id = %s"
    assert (await _revoke(pool, first))["revoked"] == []
    assert await _rows(pool, hold, soak) == [(f"signed off: {first}",)]  # cycle 2 is still signed off
    await _revoke(pool, second)
    assert await _rows(pool, hold, soak) == [(None,)]
    await _revoke(pool, legal_signoff)
    assert await _rows(pool, hold, legal) == [("legal hold LH-7",)]
    released = await _rows(
        pool, "SELECT object, detail->>'reason' FROM ds.audit_log WHERE action = 'retention.release'"
    )
    assert released == [(soak, f"signed off: {first}")]


@pytest.mark.db
async def test_only_an_approved_approval_is_revoked(pool: AsyncConnectionPool, world: World) -> None:
    run_id = await _ended(pool, world)
    async with pool.connection() as conn:
        signoff = await request_signoff(conn, run_id, 1, world.dave, PROFILE)
    unknown = "A-" + "0" * 26
    with pytest.raises(LookupError, match=f"^approval {unknown} not found$"):
        await _revoke(pool, unknown)
    with pytest.raises(approvals.ApprovalConflict, match=f"^approval {signoff} is requested, not approved$"):
        await _revoke(pool, signoff)
    await _revoke(pool, world.approval_id)
    with pytest.raises(
        approvals.ApprovalConflict, match=f"^approval {world.approval_id} is revoked, not approved$"
    ):
        await _revoke(pool, world.approval_id)


# -- sampled cycles (docs/mvp/SAMPLING.md §9, §10) ----------------------------------------------------

SAMPLED = sampled("customer-profile", "2.0.0")
NOTE = "fixed the segment mapping (PAY-7)"


@dataclasses.dataclass
class Sampled:
    world: World  # Bob wrote customer-profile 2.0.0 and Carol approved it; Dave starts its runs
    amy: Principal  # who closes draws
    spec_id: int
    approval_id: str
    other_id: int  # customer-profile-2, over the same population, never approved


@pytest.fixture
async def sampling(pool: AsyncConnectionPool, world: World) -> Sampled:
    async with pool.connection() as conn:
        amy = await person(conn, "amy", "operator")
        spec_id = await upload(conn, SAMPLED, world.bob)
        approval_id = await approved(conn, spec_id, world.bob, world.carol, scopes=["sampling"])
        other_id = await upload(conn, sampled("customer-profile-2"), world.bob)
    return Sampled(world, amy, spec_id, approval_id, other_id)


async def _drawn(
    pool: AsyncConnectionPool, s: Sampled, *, other: bool = False, **options: Any
) -> tuple[str, str]:
    """A draw of customer-profile 2.0.0 under its approval, or of customer-profile-2 (``other``)."""
    version, spec_id = (sampled("customer-profile-2"), s.other_id) if other else (SAMPLED, s.spec_id)
    options.setdefault("approval_id", None if other else s.approval_id)
    async with pool.connection() as conn:
        return await drawn(conn, spec_id, version, s.world.dave, **options)


async def _close(pool: AsyncConnectionPool, s: Sampled) -> list[str]:
    async with repo.audited(pool) as (conn, audit):
        return await sample.close(conn, audit, "customer-profile", NOTE, s.amy)


async def _facts(pool: AsyncConnectionPool, run_id: str, recorded: dict[str, Any] | None = None) -> Any:
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        if recorded is not None:
            return await approvals.rebuilt(conn, run, 1, SAMPLED, recorded)
        return await approvals.attests(conn, run, 1, SAMPLED)


@pytest.mark.db
async def test_a_sampled_cycle_attests_format_2_stable_across_later_draws_closes_and_purges(
    pool: AsyncConnectionPool, sampling: Sampled
) -> None:
    s = sampling
    earlier_run, earlier = await _drawn(pool, s, other=True, discrepant=["C001"])
    await _close(pool, s)
    failed_run, failed = await _drawn(pool, s, other=True, discrepant=["C004"])  # closed after D: not shown
    run_id, draw_id = await _drawn(pool, s)
    await _close(pool, s)
    facts = await _facts(pool, run_id)
    later_run, _ = await _drawn(pool, s, discrepant=["C002"])  # a later failure, and its close
    await _close(pool, s)
    async with pool.connection() as conn:
        for run in (earlier_run, later_run):  # an earlier draw's cycle erased, a later one's purged
            assert await repo.start_purge(conn, run, 1, "erasure: a request")
            await repo.purge_keys(conn, run, 1, 0, 10)
            await repo.finish_purge(conn, run, 1, TESTER.actor)
        stored = await repo.get_draw(conn, run_id, 1)
        ended = await repo.get_cycle(conn, run_id, 1)
        ledger = await repo.ledger(conn, SAMPLED.sample.population)  # type: ignore[union-attr]
        chain = await (
            await conn.execute(
                """SELECT l.action, l.object, c.seq FROM ds.audit_log l JOIN ds.audit_chain c ON c.id = l.id
                   WHERE l.action IN ('sample.draw', 'sample.close') ORDER BY c.seq"""
            )
        ).fetchall()
        purged = await repo.get_draw(conn, earlier_run, 1), await repo.get_cycle(conn, earlier_run, 1)
    rebuilt = await _facts(pool, run_id, facts)
    assert stored is not None and ended is not None
    seq = {(action, obj): n for action, obj, n in chain}
    assert facts["format"] == 3 and facts["accuracy"] is None and rebuilt == facts
    assert facts["sample"] == {
        "draw_id": draw_id, "method": "bottom_k", "salt_label": "cutover-q4", "nonce": stored["nonce"],
        "salt_sha256": sha256_hex(f"cutover-q4:{stored['nonce']}"), "drawn_at": utc_iso(stored["drawn_at"]),
        "draw_seq": seq[("sample.draw", run_id)], "population": ["table:db-a/customers"],
        "targets": {"standard": "0.001", "high": "0.0001"}, "confidence": "0.95", "sided": "one-sided upper",
        "margin": "0.005", "resolve": {"times": 3, "delay_s": 600.0}, "frame": stored["frame"],
        "strata": stored["strata"], "selection_sha256": stored["selection_sha256"],
        "ledger": [
            {"draw_id": earlier, "spec_name": "customer-profile-2", "run_id": earlier_run, "cycle": 1,
             "draw_seq": seq[("sample.draw", earlier_run)], "status": "failed",
             "ended_at": utc_iso(purged[1]["ended_at"]),  # type: ignore[index]
             "failed_strata": ["segment=retail"], "closed_by": s.amy.actor,
             "close_seq": seq[("sample.close", earlier)], "close_note": NOTE},
            {"draw_id": failed, "spec_name": "customer-profile-2", "run_id": failed_run, "cycle": 1,
             "draw_seq": seq[("sample.draw", failed_run)], "status": "failed",
             "ended_at": facts["sample"]["ledger"][1]["ended_at"], "failed_strata": ["segment=other"],
             "closed_by": None, "close_seq": None, "close_note": None},
        ],
        "judgement": ended["sample"],
    }  # fmt: skip
    # the ledger outlives purges and erasures (§15): the draws, their closes and each cycle's judgement
    assert purged[0] is not None and purged[1]["sample"]["status"] == "failed"  # type: ignore[index]
    assert [d["status"] for d in ledger] == ["failed", "failed", "passed", "failed"]
    async with pool.connection() as conn:  # the earlier failure was closed after D: its sign-off is refused
        run = await repo.get_run(conn, run_id)
        refused = await approvals.signoff_problems(conn, run, 1, SAMPLED)  # type: ignore[arg-type]
    today, later = f"{datetime.now(UTC):%Y-%m-%d}", ledger[3]["draw_id"]
    assert refused == [
        f"draw {failed} (spec customer-profile-2, run {failed_run} cycle 1) failed before draw {draw_id} was"
        " made and was not closed before it: close it with the fix, then sign off a draw made after the"
        " close",
        f"draw {later} (spec customer-profile, run {later_run} cycle 1), made after draw {draw_id}, failed"
        f" stratum segment=retail on {today}: fix it, close it and sign off a draw made after the close",
    ]


@pytest.mark.db
async def test_a_passed_sampled_cycle_is_signed_off_and_its_closers_are_excluded(
    pool: AsyncConnectionPool, sampling: Sampled
) -> None:
    s = sampling
    await _drawn(pool, s, other=True, discrepant=["C003"])
    await _close(pool, s)
    run_id, _ = await _drawn(pool, s)
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        problems = await approvals.signoff_problems(conn, run, 1, SAMPLED)  # type: ignore[arg-type]
        signoff = await request_signoff(conn, run_id, 1, s.world.dave, SAMPLED)
        authors = await repo.approval_authors(conn, signoff)
    assert problems == []
    assert authors is not None and s.amy.actor in authors["persons"]  # she closed a draw of its ledger


@pytest.mark.db
async def test_each_refusal_of_a_sampled_cycle_names_its_draw(
    pool: AsyncConnectionPool, sampling: Sampled
) -> None:
    s = sampling
    async with pool.connection() as conn:  # stopped while drawing: its cycle ended undrawn
        undrawn = await start(conn, s.spec_id, s.world.dave, approval_id=s.approval_id,
                              params={"keys_source": "hash_sample"})  # fmt: skip
        await end(conn, undrawn, counters(1))
    census_run, census_draw = await _drawn(pool, s, discrepant=["C005"])
    relabelled, relabelled_draw = await _drawn(pool, s)
    unjudged, unjudged_draw = await _drawn(pool, s, judged=False)
    sized = sampled("customer-profile", "2.1.0", size=2)  # two of each segment's three: not a census
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.run_keys SET signoff_member = NOT signoff_member WHERE run_id = %s AND seq = 0",
            (relabelled,),
        )
        await conn.execute(
            """UPDATE ds.cycles SET census = jsonb_set(census, '{a,unfit}', '12') WHERE run_id = %s""",
            (relabelled,),
        )
        sized_id = await upload(conn, sized, s.world.bob)
        sized_run, _ = await drawn(conn, sized_id, sized, s.world.dave)
    at = "of cycle 1 of run"
    assert await _problems(pool, undrawn, version=SAMPLED) == [
        f"cycle 1 of run {undrawn} has no draw of its own: its sample was never drawn"
    ]
    assert await _problems(pool, census_run, version=SAMPLED, later=False) == [
        f"stratum segment=other {at} {census_run} is over its 0.1% target: compared whole, its 3 keys allow 0"
        " discrepant or unresolved key(s); it has 1"
    ]
    assert await _problems(pool, relabelled, version=SAMPLED, later=False) == [
        f"the keys {at} {relabelled} are not draw {relabelled_draw}'s selection: a sample is never"
        " extended, trimmed or relabelled",
        f"the frame of {relabelled_draw} holds 12 key(s) outside key.pattern or the store's limits (source"
        " 0): they could not be sampled; correct key.pattern in a new version and draw again",
        f"draw {census_draw} (spec customer-profile, run {census_run} cycle 1) failed before draw"
        f" {relabelled_draw} was made and was not closed before it: close it with the fix, then sign off a"
        " draw made after the close",
    ]
    assert (await _problems(pool, unjudged, version=SAMPLED, later=False))[0] == (
        f"the sample {unjudged_draw} {at} {unjudged} ended without a judgement: draw again"
    )
    bound = "its one-sided 95% upper bound is 77.64% (0 discrepant of 2 cleanly compared keys)"
    judged = [
        p for p in await _problems(pool, sized_run, version=sized, later=False) if p.startswith("stratum")
    ]
    assert judged == [
        f"stratum segment={segment} {at} {sized_run} is over its 0.1% target: {bound}"
        for segment in ("retail", "other")
    ]


@pytest.mark.db
async def test_unresolved_absent_and_truncated_keys_refuse_a_sampled_sign_off(
    pool: AsyncConnectionPool, sampling: Sampled
) -> None:
    sized = sampled("customer-profile", "2.1.0", size=2)  # two of each segment's three
    complete = SideState(SideStateKind.COMPLETE)
    reads = {  # a key's read as its reason
        "timeout": (complete, SideState(SideStateKind.ERROR, "timeout")),
        "absent_both": (SideState(SideStateKind.ABSENT), SideState(SideStateKind.ABSENT)),
        "truncated": (complete, SideState(SideStateKind.INCOMPLETE, "truncated")),
    }
    async with pool.connection() as conn:
        spec_id = await upload(conn, sized, sampling.world.bob)
        run_id, _ = await drawn(conn, spec_id, sized, sampling.world.dave, end=False)
        cur = await conn.execute(
            "SELECT signoff_stratum, key_masked FROM ds.run_keys WHERE run_id = %s ORDER BY seq", (run_id,)
        )
        by_stratum: dict[str, list[str]] = {}
        for label, masked in await cur.fetchall():
            by_stratum.setdefault(label, []).append(masked)
        chosen = [
            (by_stratum["segment=retail"][0], "timeout"),
            (by_stratum["segment=other"][0], "absent_both"),
            (by_stratum["segment=other"][1], "truncated"),
        ]
        for masked, reason in chosen:
            a, b = reads[reason]
            result = KeyResult(
                masked[2:], Outcome.INCONCLUSIVE, a, b, inconclusive_reason=reason,
                confirmation=Confirmation.RECHECKED,
            )  # fmt: skip
            await repo.save_key_result(conn, run_id, 1, masked, result, [])
        await repo.set_counters(conn, run_id, counters(1))
        found = await repo.get_draw(conn, run_id, 1)
        await repo.end_cycle(conn, run_id, sample=await sample.judge(conn, found, sized))  # type: ignore[arg-type]
        run = await repo.get_run(conn, run_id)
        problems = await approvals.signoff_problems(conn, run, 1, sized, later=False)  # type: ignore[arg-type]
    retail, other = (
        f"stratum segment={label} of cycle 1 of run {run_id} has" for label in ("retail", "other")
    )
    assert [p for p in problems if "is over its" not in p and p.startswith("stratum")] == [
        f"{retail} 1 unresolved key(s) after 3 retries, over the 0 its 0.1% target allows (of 1 cleanly"
        " compared keys)",
        f"{other} 1 key(s) absent on both sides, over the 0 its 0.1% target allows of 2 drawn: the frame was"
        " stale; draw again",
        f"{other} 1 key(s) above the API cap that were not compared (truncated): declare pagination for side"
        " b's fetch in a new version and draw again",
    ]
