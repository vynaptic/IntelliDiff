"""Four-eyes enforcement in the runner (docs/mvp/APPROVALS.md §4): the gate at each entry point in each
auth mode, a soak's boundary attaching the active approval or pausing, a run started outside SSO never
signed off whatever it resumes or attaches under, a one-shot compare stopped by a revoke, and how
maxKeys cut each cycle's keys (``runs.keys_cut``, copied into ``ds.cycles``)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from psycopg.rows import dict_row

from deltasleuth import approvals
from deltasleuth.approvals import ApprovalRequired
from deltasleuth.runner import compare_one, create_run, pause_run, resume_run, runs
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import approved, person

from .conftest import TARGETS, Harness, OpenHarness, ScriptedService, doc, spec_text

pytestmark = pytest.mark.db

OIDC = {
    "public_url": "https://ds.test",
    "oidc": {  # settings only: nothing signs in, and the runner never contacts the IdP
        "issuer": "http://127.0.0.1:1",
        "client_id": "deltasleuth",
        "client_secret_ref": "file:oidc_secret",
        "roles": {"rule_approver": ["G-approver"]},
    },
}
TOKENS = {"public_url": "https://ds.test"}
LISTED = """\
  source:
    kind: paginated_endpoint
    target: svc-a
    request: {method: GET, path: /list}
    pagination: {kind: cursor, itemsPath: "$.items", cursorPath: "$.next", cursorParam: cursor}
    keyPath: "$.id"
"""
SOAK = "concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 2}"
RECORD = {"needs": ["clear"], "mode": "oidc"}  # the unit spec's clear paths need an approval


def listed(execution: str = "") -> str:
    """The unit spec reading its keys from the service's ``/list``."""
    return spec_text(execution=execution).replace(
        "  source: {kind: list, keys: [K001, K002, K003]}\n", LISTED
    )


def targets(service: ScriptedService) -> str:
    assert service.server is not None
    return TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)


@pytest.fixture
async def sso(open_harness: OpenHarness, service: ScriptedService) -> AsyncIterator[Harness]:
    """A Context in oidc mode whose targets are the scripted service."""
    async with open_harness(targets(service), auth=OIDC) as h:
        yield h


def needs(spec_id: int) -> str:
    return re.escape(f"spec {spec_id} (unit-runner 1.0.0) needs a four-eyes approval of clear before it runs")


def revoked(spec_id: int, approval_id: str) -> str:
    return re.escape(
        f"spec {spec_id} (unit-runner 1.0.0) needs a new four-eyes approval: {approval_id} was revoked"
    )


async def approve(h: Harness, spec_id: int) -> str:
    """An approval of the version's needs; the tester's upload is a CLI one, so it takes two approvers."""
    async with h.ctx.pool.connection() as conn:
        needed = approvals.needs(await h.ctx.spec(conn, spec_id))
        rita = await person(conn, "rita", "spec_author")
        deciders = [await person(conn, name) for name in ("amir", "bea")]
        return await approved(conn, spec_id, rita, *deciders, scopes=needed)


async def revoke(h: Harness, approval_id: str) -> dict[str, Any]:
    async with repo.audited(h.ctx.pool) as (conn, audit):
        return await approvals.revoke(conn, audit, approval_id, "wrong clear path", who=TESTER)


async def sql(h: Harness, query: str, *params: Any) -> list[dict[str, Any]]:
    async with h.ctx.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(query.encode(), params)
        return await cur.fetchall()


async def audited(h: Harness, *actions: str) -> list[dict[str, Any]]:
    return await sql(
        h,
        "SELECT actor, action, object, detail FROM ds.audit_log WHERE action = ANY(%s) ORDER BY id",
        list(actions),
    )


# -- the gate at each entry point ------------------------------------------------------------------


async def test_under_sso_a_run_and_a_compare_wait_for_an_approval_and_start_under_it(
    sso: Harness, service: ScriptedService
) -> None:
    service.listings = [["K001"]]
    service.both("K001", doc("K001"), doc("K001"))
    spec_id = await sso.spec(listed())
    with pytest.raises(ApprovalRequired, match=f"^{needs(spec_id)}$"):
        await create_run(sso.ctx, spec_id, "batch", who=TESTER)
    with pytest.raises(ApprovalRequired, match=f"^{needs(spec_id)}$"):
        await compare_one(sso.ctx, spec_id, "K001", who=TESTER)
    assert (service.walks, service.log) == (0, [])  # refused before the key listing or a key was read
    assert await sql(sso, "SELECT count(*) AS runs FROM ds.runs") == [{"runs": 0}]
    approval_id = await approve(sso, spec_id)
    run = await sso.finish(await create_run(sso.ctx, spec_id, "batch", who=TESTER))
    one_shot = await sso.run((await compare_one(sso.ctx, spec_id, "K001", who=TESTER)).attrs["run_id"])
    assert [(r["status"], r["approval_id"], r["params"]["approval"]) for r in (run, one_shot)] == [
        ("completed", approval_id, RECORD)
    ] * 2
    assert await sql(sso, "SELECT approval_id FROM ds.cycles") == [{"approval_id": approval_id}] * 2
    rows = await audited(sso, "create_run", "compare_one")
    assert [(r["action"], r["detail"]["approval_id"], r["detail"]["approval"]) for r in rows] == [
        ("create_run", approval_id, RECORD),
        ("compare_one", approval_id, RECORD),
    ]


@pytest.mark.parametrize("mode", ["tokens", "none"])
async def test_tokens_and_developer_modes_run_compare_and_resume_unapproved_and_record_why(
    open_harness: OpenHarness, service: ScriptedService, mode: str
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    async with open_harness(targets(service), auth=TOKENS if mode == "tokens" else None) as h:
        spec_id = await h.spec(spec_text(["K001"]))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        await pause_run(h.ctx, run_id, who=TESTER)
        assert await resume_run(h.ctx, run_id, who=TESTER) == "running"
        run = await h.run(run_id)
        one_shot = await h.run((await compare_one(h.ctx, spec_id, "K001", who=TESTER)).attrs["run_id"])
    record = {"needs": ["clear"], "mode": mode}
    assert [(r["approval_id"], r["params"]["approval"]) for r in (run, one_shot)] == [(None, record)] * 2
    assert approvals.run_state(record, None) == "unapproved"


async def test_a_revoke_pauses_a_run_whose_resume_waits_for_a_new_approval(
    sso: Harness, service: ScriptedService
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    spec_id = await sso.spec(spec_text(["K001"]))
    first = await approve(sso, spec_id)
    run_id = await create_run(sso.ctx, spec_id, "batch", who=TESTER)
    assert (await revoke(sso, first))["paused"] == [run_id]
    with pytest.raises(ApprovalRequired, match=f"^{revoked(spec_id, first)}$"):
        await resume_run(sso.ctx, run_id, who=TESTER)
    paused = await sso.run(run_id)
    second = await approve(sso, spec_id)
    assert await resume_run(sso.ctx, run_id, who=TESTER) == "running"
    resumed = await sso.run(run_id)
    run = await sso.finish(run_id)
    assert (paused["status"], paused["stop_reason"], paused["approval_id"]) == (
        "paused", approvals.APPROVAL_PAUSE, first,
    )  # fmt: skip
    assert (resumed["stop_reason"], resumed["approval_id"], resumed["params"]["approval"]) == (
        None,
        second,
        RECORD,
    )
    assert run["status"] == "completed"
    assert await sql(sso, "SELECT approval_id FROM ds.cycles") == [{"approval_id": second}]
    moves = await audited(sso, "paused", "running")
    assert [(r["actor"], r["action"], r["detail"]["reason"], r["detail"]["approval_id"]) for r in moves] == [
        (TESTER.actor, "paused", approvals.APPROVAL_PAUSE, first),
        (TESTER.actor, "running", None, second),
    ]


@pytest.mark.parametrize("approved_first", [True, False], ids=["attaches", "pauses"])
async def test_a_soak_without_its_approval_attaches_one_at_its_boundary_or_waits_there(
    open_harness: OpenHarness, service: ScriptedService, approved_first: bool
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    async with open_harness(targets(service), auth=TOKENS) as h:  # started unapproved, in tokens mode
        spec_id = await h.spec(spec_text(["K001"], execution=SOAK))
        run_id = await create_run(h.ctx, spec_id, "soak", who=TESTER)
    async with open_harness(targets(service), auth=OIDC) as h:  # then the deployment moved to SSO
        approval_id = await approve(h, spec_id) if approved_first else None
        run = await h.finish(run_id, ("paused", *repo.TERMINAL))
        if not approved_first:
            assert (run["status"], run["stop_reason"], run["cycle"], run["approval_id"]) == (
                "paused", approvals.APPROVAL_PAUSE, 2, None,
            )  # fmt: skip
            [pause] = await audited(h, "paused")
            assert (pause["actor"], pause["detail"]) == (
                "system:worker", {"reason": approvals.APPROVAL_PAUSE, "approval_id": None},
            )  # fmt: skip
            with pytest.raises(ApprovalRequired, match=f"^{needs(spec_id)}$"):
                await resume_run(h.ctx, run_id, who=TESTER)
            approval_id = await approve(h, spec_id)
            assert await resume_run(h.ctx, run_id, who=TESTER) == "running"
            run = await h.finish(run_id)
        cycles = await sql(
            h, "SELECT cycle, approval_id FROM ds.cycles WHERE run_id = %s ORDER BY cycle", run_id
        )
        async with h.ctx.pool.connection() as conn:
            problems = await approvals.signoff_problems(conn, run, 2, await h.ctx.spec(conn, spec_id))
    # The record is its creation's: it started in tokens mode.
    assert (run["status"], run["approval_id"], run["params"]["approval"]) == (
        "completed",
        approval_id,
        {"needs": ["clear"], "mode": "tokens"},
    )
    # Its first cycle ran unapproved, and stays so; its second, from a paused start too, ran under the
    # approval. Neither is signed off: the run started outside SSO.
    assert cycles == [{"cycle": 1, "approval_id": None}, {"cycle": 2, "approval_id": approval_id}]
    assert problems == [f"run {run_id} started in tokens mode: only a run started under SSO is signed off"]


async def test_a_run_started_in_tokens_mode_and_resumed_under_sso_is_never_signed_off(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    async with open_harness(targets(service), auth=TOKENS) as h:  # started unapproved, in tokens mode
        spec_id = await h.spec(spec_text(["K001"]))
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        await pause_run(h.ctx, run_id, who=TESTER)
    async with open_harness(targets(service), auth=OIDC) as h:  # then resumed under SSO
        approval_id = await approve(h, spec_id)
        assert await resume_run(h.ctx, run_id, who=TESTER) == "running"
        run = await h.finish(run_id)
        async with h.ctx.pool.connection() as conn:
            ended = await repo.get_cycle(conn, run_id, 1)
            problems = await approvals.signoff_problems(conn, run, 1, await h.ctx.spec(conn, spec_id))
            said = approvals.label(await approvals.run_approval(conn, run, 1))
            current = approvals.label(await approvals.run_approval(conn, run))  # the Run screen's
    assert current.startswith(f"Spec approval {approval_id} was granted by ")
    assert current.endswith(" Not sign-off evidence: the run started in tokens mode.")
    assert ended is not None
    assert (run["status"], run["approval_id"], run["params"]["approval"]) == (
        "completed", approval_id, {"needs": ["clear"], "mode": "tokens", "from_cycle": 2},
    )  # fmt: skip
    assert ended["approval_id"] is None  # the cycle it resumed in started unapproved, and stays so
    assert problems == [
        f"run {run_id} started in tokens mode: only a run started under SSO is signed off",
        f"cycle 1 of run {run_id} ran without a spec approval",
    ]
    assert said == (
        "Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers)."
        " Not sign-off evidence."
    )


async def test_a_one_shot_compare_stopped_by_a_revoke_raises_instead_of_answering(
    sso: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    spec_id = await sso.spec(spec_text(["K001"]))
    approval_id = await approve(sso, spec_id)
    read_now = runs._read_now

    async def revoked_meanwhile(ctx: Any, *args: Any) -> Any:
        assert len((await revoke(sso, approval_id))["stopped"]) == 1
        return await read_now(ctx, *args)

    monkeypatch.setattr(runs, "_read_now", revoked_meanwhile)
    with pytest.raises(ApprovalRequired, match=f"^{revoked(spec_id, approval_id)}$"):
        await compare_one(sso.ctx, spec_id, "K001", who=TESTER)
    assert await sql(sso, "SELECT status, stop_reason, approval_id FROM ds.runs") == [
        {"status": "cancelling", "stop_reason": approvals.APPROVAL_REVOKED, "approval_id": approval_id}
    ]


# -- keys_cut ---------------------------------------------------------------------------------------


async def test_each_cycle_records_how_max_keys_cut_its_keys(
    harness: Harness, service: ScriptedService
) -> None:
    service.listings = [["K001", "K002", "K003"], ["K002"]]  # the second cycle's listing fits
    for key in ("K001", "K002", "K003"):
        service.both(key, doc(key), doc(key))
    text = listed("concurrency: 2, cycleInterval: 0s, stop: {maxKeys: 2, maxCycles: 2}")
    run_id = await create_run(
        harness.ctx, await harness.spec(text), "soak", soak={"rolling": True}, who=TESTER
    )
    created = await harness.run(run_id)
    run = await harness.finish(run_id)
    cycles = await sql(
        harness, "SELECT cycle, keys_cut FROM ds.cycles WHERE run_id = %s ORDER BY cycle", run_id
    )
    assert created["keys_cut"] == {"resolved": 3, "taken": 2}
    # A rolling cycle replaces the cut, whatever the stop reason still says.
    assert (run["keys_cut"], run["stop_reason"]) == (None, "stop_condition:maxCycles")
    assert cycles == [{"cycle": 1, "keys_cut": {"resolved": 3, "taken": 2}}, {"cycle": 2, "keys_cut": None}]
