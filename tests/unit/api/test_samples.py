"""Sign-off sampling over the API: a population's ledger, a draw's live
progress (its nonce withheld, no key listed) or its precheck once ended (its nonce and strata's u_max shown
only from rule_approver up), closing draws, a run's current draw and each cycle's judgement, and a
sampled sign-off whose final step records the later draws it judged (at the request and at the
decision) and whose closers may not approve it. Draws are written as the worker commits them."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth.auth.principal import Principal, expand
from deltasleuth.context import Context
from deltasleuth.core.types import CompiledSpec
from deltasleuth.runner import sample
from deltasleuth.store import repo
from tests.unit.store.four_eyes import approved, drawn, person, sampled, upload

from .conftest import OIDC, Callers, audited, said

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
PROFILE, PROFILE_2 = sampled("customer-profile"), sampled("customer-profile-2")
IDS = sampled("customer-ids", key="identifier")
FIX = "fixed the segment mapping (PAY-7)"
NOTE = "note must be 1 to 1000 characters without control characters"


async def _world(ctx: Context) -> dict[str, Any]:
    """Bob uploads the three names, Carol approves customer-profile (its sampling), Dave starts runs."""
    async with ctx.pool.connection() as conn:
        bob, carol, dave = [await person(conn, name, "rule_approver") for name in ("bob", "carol", "dave")]
        ids = {v.name: await upload(conn, v, bob) for v in (PROFILE, PROFILE_2, IDS)}
        approval = await approved(conn, ids["customer-profile"], bob, carol, scopes=["sampling"])
    return {"ids": ids, "dave": dave, "approval": approval}


async def _draw(
    ctx: Context, world: dict[str, Any], version: CompiledSpec, **options: Any
) -> tuple[str, str]:
    if version is PROFILE:
        options.setdefault("approval_id", world["approval"])
    async with ctx.pool.connection() as conn:
        return await drawn(conn, world["ids"][version.name], version, world["dave"], **options)


def world_of(client: TestClient) -> dict[str, Any]:
    return client.portal.call(_world, client.app.state.ctx)


def draw(client: TestClient, world: dict[str, Any], version: CompiledSpec, **options: Any) -> tuple[str, str]:
    return client.portal.call(partial(_draw, client.app.state.ctx, world, version, **options))


def person_of(client: TestClient, name: str, role: str) -> dict[str, str]:
    """``name``'s session holding ``role``, signed in now (an approval step needs a fresh sign-in)."""
    return Callers(client).session(*expand([role]), username=name, auth_time=datetime.now(UTC))


def test_closing_draws_needs_a_person_under_sso_and_a_note_free_of_keys(open_client: OpenClient) -> None:
    body = {"spec_name": "customer-profile", "note": "bad\x07note"}
    with open_client(auth={"oidc": OIDC}) as client:
        world = world_of(client)
        amy = person_of(client, "amy", "operator")
        close = partial(client.post, "/v1/samples/close", headers=amy)
        by_token = client.post("/v1/samples/close", json=body, headers=Callers(client).token("operator"))
        bad_note = close(json=body)
        unknown = close(json={"spec_name": "customer-profile-9", "note": FIX})
        nothing = close(json={"spec_name": "customer-profile", "note": FIX})
        failed_run, failed = draw(client, world, PROFILE_2, discrepant=["C001"])
        _, identified = draw(client, world, IDS, discrepant=["C002"])
        shaped = close(json={"spec_name": "customer-ids", "note": "C002 was wrong"})
        closed = close(json={"spec_name": "customer-profile", "note": FIX})
        again = close(json={"spec_name": "customer-profile", "note": FIX})
        closes = audited(client, "sample.close")
        amy_actor = client.get("/v1/auth/me", headers=amy).json()["actor"]
    with open_client(auth={}) as tokens:  # the auth mode comes first: even a bad note
        in_tokens = tokens.post("/v1/samples/close", json=body, headers=Callers(tokens).token("operator"))
    with open_client() as developer:
        in_developer = developer.post("/v1/samples/close", json=body)
    assert said(in_tokens) == (
        409,
        "closing draws needs SSO (auth.oidc); this deployment runs in tokens mode",
    )
    assert said(in_developer) == (
        409,
        "closing draws needs SSO (auth.oidc); this deployment runs in developer mode",
    )
    assert said(by_token) == (403, "closing draws needs a signed-in person")
    assert said(bad_note) == (422, NOTE)
    assert said(unknown) == (404, "spec customer-profile-9 not found")
    no_draw = "has no draw to close (every ended bottom_k draw passed or is closed)"
    assert said(nothing) == said(again) == (409, f"the population of customer-profile {no_draw}")
    assert said(shaped) == (422, "the note holds a key-shaped value: write it without customer keys")
    assert (closed.status_code, closed.json()) == (200, {"closed": [failed, identified]})  # one population
    assert closes == [
        (amy_actor, d, {"spec_name": "customer-profile", "note": FIX}) for d in (failed, identified)
    ]
    assert failed_run


def test_the_ledger_a_live_draw_s_progress_and_an_ended_draw_s_precheck(open_client: OpenClient) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        world = world_of(client)
        viewer = Callers(client).token("viewer")
        failed_run, failed = draw(client, world, PROFILE_2, discrepant=["C003"])
        passed_run, passed = draw(client, world, PROFILE)
        live_run, live = draw(client, world, PROFILE, end=False, discrepant=["C006"])
        ledger = client.get("/v1/samples", params={"spec_name": "customer-profile"}, headers=viewer).json()
        every = client.get("/v1/samples", params={"limit": 2}, headers=viewer).json()
        rest = client.get("/v1/samples", params={"cursor": every["next_cursor"]}, headers=viewer).json()
        shown_live = client.get(f"/v1/samples/{live}", headers=viewer)
        shown_ended = client.get(f"/v1/samples/{passed}", headers=viewer).json()
        missing = client.get("/v1/samples/D-" + "0" * 26, headers=viewer)
        no_spec = client.get("/v1/samples", params={"spec_name": "nope"}, headers=viewer)
        run = client.get(f"/v1/runs/{passed_run}", headers=viewer).json()
        live_view = client.get(f"/v1/runs/{live_run}", headers=viewer).json()
        erin = person_of(client, "erin", "rule_approver")
        approver_ended = client.get(f"/v1/samples/{passed}", headers=erin).json()
        approver_run = client.get(f"/v1/runs/{passed_run}", headers=erin).json()
    items = ledger["items"]
    assert [(d["draw_id"], d["spec_name"], d["status"], d["closed"], d["blocking"]) for d in items] == [
        (failed, "customer-profile-2", "failed", False, True),
        (passed, "customer-profile", "passed", False, False),
        (live, "customer-profile", "live", False, False),
    ]
    assert all("nonce" not in d and "strata" not in d for d in items)
    assert [d["draw_id"] for d in every["items"] + rest["items"]] == [failed, passed, live]
    assert ledger["next_cursor"] is None and rest["next_cursor"] is None
    live_body = shown_live.json()
    assert live_body["nonce"] is None and "m-C0" not in shown_live.text  # no key while it is compared
    progress = live_body["progress"]
    assert progress["status"] == "failed" and progress["done"] == 6 and progress["finished_recently"] == 6
    other = next(s for s in progress["strata"] if s["label"] == "segment=other")
    assert (other["drawn"], other["done"], other["n"], other["x"], other["retrying"], other["verdict"]) == (
        3, 3, 3, 1, 0, "failed",
    )  # fmt: skip
    # Below rule_approver, an ended draw has no nonce and no stratum's u_max either: with keys shown in
    # clear (business_code), they would tell which keys lie in a stratum compared whole.
    [cycle] = run["cycles"]
    for draw_view in (shown_ended, cycle["sample"]["draw"]):
        assert draw_view["nonce"] is None and all("u_max" not in s for s in draw_view["strata"])
        assert [s["label"] for s in draw_view["strata"]] == ["segment=retail", "segment=other"]
    for draw_view in (approver_ended, approver_run["cycles"][0]["sample"]["draw"]):
        assert len(draw_view["nonce"]) == 32 and all(s["u_max"] for s in draw_view["strata"])
    assert shown_ended["ledger"] == items
    assert shown_ended["precheck"] == [
        f"draw {failed} (spec customer-profile-2, run {failed_run} cycle 1) failed before draw {passed} was"
        " made and was not closed before it: close it with the fix, then sign off a draw made after the"
        " close",
        f"draw {live} (spec customer-profile, run {live_run} cycle 1), made after draw {passed}, has failed"
        " stratum segment=other for certain: fix it, close it and sign off a draw made after the close",
    ]
    assert said(missing) == (404, f"draw D-{'0' * 26} not found")
    assert said(no_spec) == (404, "spec nope not found")
    assert run["sample"] == {
        "state": None,
        "error": None,
        "draw_id": passed,
        "drawn_at": run["sample"]["drawn_at"],
    }
    assert cycle["sample"]["draw"]["draw_id"] == passed and cycle["sample"]["judgement"]["status"] == "passed"
    assert (live_view["sample"]["state"], live_view["sample"]["draw_id"], live_view["cycles"]) == (
        "comparing",
        live,
        [],
    )


async def _retrying(ctx: Context, run_id: str, *, drawing: bool = False) -> None:
    async with ctx.pool.connection() as conn:  # every key left to read is a retry; or its draw is due again
        if drawing:
            await repo.set_draw(
                conn, run_id, {"due": "2026-10-01T00:00:00+00:00", "error": "the frame is empty"}
            )
        else:
            await conn.execute(
                "UPDATE ds.run_keys SET state = 'pending', retries = 1 WHERE run_id = %s AND seq = 0",
                (run_id,),
            )


def test_a_run_s_draw_state(open_client: OpenClient) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        world = world_of(client)
        viewer = Callers(client).token("viewer")
        run_id, _ = draw(client, world, PROFILE, end=False)
        state = partial(lambda: client.get(f"/v1/runs/{run_id}", headers=viewer).json()["sample"])
        client.portal.call(_retrying, client.app.state.ctx, run_id)
        resolving = state()
        client.portal.call(partial(_retrying, client.app.state.ctx, run_id, drawing=True))
        drawing = state()
        client.post(f"/v1/runs/{run_id}/pause", headers=Callers(client).token("operator"))
        paused = state()
    assert (resolving["state"], drawing["state"], paused["state"]) == ("resolving", "drawing", "paused")
    assert (resolving["error"], drawing["error"]) == (None, "the frame is empty")


async def _ended(ctx: Context, run_id: str, version: CompiledSpec) -> None:
    """End a live draw's cycle as the worker does, storing its judgement."""
    async with ctx.pool.connection() as conn:
        found = await repo.get_draw(conn, run_id, 1)
        await repo.end_cycle(conn, run_id, sample=await sample.judge(conn, found, version))  # type: ignore[arg-type]
        await repo.set_run_status(conn, run_id, "completed")


def test_a_sampled_sign_off_judges_later_draws_at_request_and_decision(open_client: OpenClient) -> None:
    close = {"spec_name": "customer-profile", "note": FIX}
    with open_client(auth={"oidc": OIDC}) as client:
        world = world_of(client)
        dave = person_of(client, "dave", "operator")
        amy, erin = person_of(client, "amy", "rule_approver"), person_of(client, "erin", "rule_approver")
        _, earlier = draw(client, world, PROFILE_2, discrepant=["C001"])
        assert client.post("/v1/samples/close", json=close, headers=amy).status_code == 200
        signed_run, signed = draw(client, world, PROFILE)
        request = client.post(f"/v1/runs/{signed_run}/cycles/1/signoff", headers=dave).json()
        live_run, live = draw(client, world, PROFILE, end=False)  # later and live, clean so far: not blocking
        review = client.get(f"/v1/approvals/{request['approval_id']}", headers=amy).json()
        body = {"decision": "approve", "attests_sha256": request["attests_sha256"]}
        by_closer = client.post(f"/v1/approvals/{request['approval_id']}/decision", json=body, headers=amy)
        granted = client.post(f"/v1/approvals/{request['approval_id']}/decision", json=body, headers=erin)
        grant = audited(client, "approval.grant")[-1][2]
        client.portal.call(_ended, client.app.state.ctx, live_run, PROFILE)
        # The later draws at the request: a draw made after it failed
        blocked_run, blocked = draw(client, world, PROFILE)
        spoiler_run, spoiler = draw(client, world, PROFILE, discrepant=["C002"])
        at_request = client.post(f"/v1/runs/{blocked_run}/cycles/1/signoff", headers=dave)
        assert client.post("/v1/samples/close", json=close, headers=amy).status_code == 200
        # The later draws at the decision: a draw made after the request ended without a judgement
        decided_run, decided = draw(client, world, PROFILE)
        asked = client.post(f"/v1/runs/{decided_run}/cycles/1/signoff", headers=dave).json()
        late_run, late = draw(client, world, PROFILE, judged=False)
        body = {"decision": "approve", "attests_sha256": asked["attests_sha256"]}
        at_decision = client.post(f"/v1/approvals/{asked['approval_id']}/decision", json=body, headers=erin)
    attests = review["attests"]
    assert attests["format"] == 3 and attests["sample"]["draw_id"] == signed
    assert [d["draw_id"] for d in attests["sample"]["ledger"]] == [earlier]
    assert [(d["draw_id"], d["order"], d["status"]) for d in review["review"]["ledger"]] == [
        (earlier, "before", "failed"), (signed, "this", "passed"), (live, "after", "live"),
    ]  # fmt: skip
    closer = f"approval needs an approver other than you: you closed draw {earlier} of its ledger"
    assert review["you"] == {"can_decide": False, "reason": closer}
    assert said(by_closer) == (409, closer)
    assert granted.status_code == 200 and granted.json()["status"] == "approved"
    assert grant["later_draws"] == [{"draw_id": live, "status": "live"}]
    today, tail = f"{datetime.now(UTC):%Y-%m-%d}", "fix it, close it and sign off a draw made after the close"
    assert said(at_request) == (
        409,
        f"draw {spoiler} (spec customer-profile, run {spoiler_run} cycle 1), made after draw {blocked},"
        f" failed stratum segment=retail on {today}: {tail}",
    )
    assert said(at_decision) == (
        409,
        f"draw {late} (spec customer-profile, run {late_run} cycle 1), made after draw {decided}, ended on"
        f" {today} without a judgement: {tail}",
    )


def test_a_sampling_spec_approval_s_review_lists_its_population_s_ledger(open_client: OpenClient) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        world = world_of(client)
        _, failed = draw(client, world, PROFILE_2, discrepant=["C001"])
        bob = person_of(client, "bob", "spec_author")
        version = sampled("customer-profile", "1.1.0")
        spec_id = client.portal.call(_uploaded, client.app.state.ctx, version)
        request = client.post(f"/v1/specs/{spec_id}/approvals", json={}, headers=bob).json()
        review = client.get(f"/v1/approvals/{request['approval_id']}", headers=bob).json()["review"]
    assert request["scopes"] == ["sampling"]
    assert [n["pointer"] for n in review["needed"]] == ["/key/source"]
    assert [(d["draw_id"], d["status"], d["order"]) for d in review["ledger"]] == [(failed, "failed", None)]


async def _uploaded(ctx: Context, version: CompiledSpec) -> int:
    async with ctx.pool.connection() as conn:
        return await upload(conn, version, Principal("local:ops@host", "ops", frozenset()))


@pytest.mark.usefixtures("vendor_keys")
def test_draws_and_closes_work_in_an_unlicensed_evaluation(open_client: OpenClient) -> None:
    with open_client(auth={"oidc": OIDC}, tier=None) as client:  # no licence: an evaluation
        world = world_of(client)
        amy = person_of(client, "amy", "operator")
        _, failed = draw(client, world, PROFILE_2, discrepant=["C001"])
        draw(client, world, PROFILE)
        closed = client.post(
            "/v1/samples/close", json={"spec_name": "customer-profile", "note": FIX}, headers=amy
        )
        standing = client.get("/v1/licence", headers=amy).json()
    assert (closed.status_code, closed.json()) == (200, {"closed": [failed]})
    assert (
        standing["state"] == "evaluation"
    )  # a name is a pair, its frame's targets none: two names, one ledger
    assert [p["name"] for p in standing["pairs"]["counted"]] == ["customer-profile-2", "customer-profile"]
