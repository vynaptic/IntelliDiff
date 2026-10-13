"""Four-eyes approvals and sign-off over the API (docs/mvp/APPROVALS.md §5, §6), with people signed in
through the fake IdP: requests, reviews, decisions and their refusals (the trigger's among them), two
approvers for CLI and token uploads, the step-up, revokes and what they pause, stop, cascade to and
release, a signed cycle against the purge, and the scorer of the attested adjudication kept from
approving (docs/mvp/PACK.md §5)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth import approvals
from deltasleuth.auth import sessions
from deltasleuth.auth.principal import WORKER, Principal, expand
from deltasleuth.context import Context
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import ChangeType, Direction, RootCause, Severity
from deltasleuth.reports.adjudicate import SHEET_COLUMNS, store_draw, store_score
from deltasleuth.reports.csv import render_csv
from deltasleuth.reports.data import load_run
from deltasleuth.reports.pdf import report_document
from deltasleuth.reports.summary import summary_document
from deltasleuth.runner import purge
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.auth.fake_idp import FakeIdp, sign_in
from tests.unit.store.four_eyes import counters, end, start, upload

from .conftest import SPEC_YAML

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
Headers = dict[str, str]
NOTE = "note must be 1 to 1000 characters without control characters"


def _version(version: str) -> str:
    return SPEC_YAML.replace('version: "1.0.0"', f'version: "{version}"')


def signed_in(
    client: TestClient, idp: FakeIdp, name: str, *groups: str, ago: timedelta = timedelta(), **options: Any
) -> Headers:
    """``name`` signed in through ``idp`` holding ``groups`` (``ago``: its sign-in's age); the headers of
    its session, which the client does not keep, so that several people act side by side."""
    client.cookies.clear()
    auth_time = int(time.time() - ago.total_seconds())
    answer = sign_in(
        client, idp, groups=groups, sub=f"{name}-sub", preferred_username=name, auth_time=auth_time, **options
    )
    assert answer.status_code == 303 and "#/signin" not in answer.headers["location"], answer.headers
    return session_of(client)


def session_of(client: TestClient) -> Headers:
    """The headers of the session the client just signed in to, which it then forgets."""
    cookie = client.cookies[sessions.COOKIE]
    client.cookies.clear()
    return {"Cookie": f"{sessions.COOKIE}={cookie}", "X-CSRF-Token": sessions.csrf_of(cookie)}


def actor(client: TestClient, headers: Headers) -> str:
    return client.get("/v1/auth/me", headers=headers).json()["actor"]


def uploaded(client: TestClient, headers: Headers, text: str = SPEC_YAML) -> int:
    created = client.post("/v1/specs", content=text, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["spec_id"]


def requested(client: TestClient, spec_id: int, headers: Headers) -> str:
    answer = client.post(f"/v1/specs/{spec_id}/approvals", headers=headers)
    assert answer.status_code == 201, answer.text
    return answer.json()["approval_id"]


def approve(client: TestClient, approval_id: str, headers: Headers, **changes: Any) -> Any:
    """Approve as the review shows the request: its content hash and scopes, or its attests' hash."""
    shown = client.get(f"/v1/approvals/{approval_id}", headers=headers).json()
    if shown["kind"] == "spec":
        body = {"content_hash": shown["review"]["content_hash"], "scopes": shown["scopes"]}
    else:
        body = {"attests_sha256": shown["attests_sha256"]}
    return client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "approve"} | body | changes,
        headers=headers,
    )


def decide(client: TestClient, approval_id: str, headers: Headers, **body: Any) -> Any:
    return client.post(f"/v1/approvals/{approval_id}/decision", json=body, headers=headers)


def said(answer: Any) -> tuple[int, str]:
    return answer.status_code, answer.json().get("detail")


def started(client: TestClient, spec_id: int, headers: Headers) -> str:
    answer = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}, headers=headers)
    assert answer.status_code == 202, answer.text
    return answer.json()["run_id"]


async def _end(ctx: Context, run_id: str) -> None:
    async with ctx.pool.connection() as conn:
        await repo.set_run_status(conn, run_id, "completed")
        await end(conn, run_id, counters(1))


def ended(client: TestClient, run_id: str) -> str:
    """End the run's first cycle complete, as its worker would: two keys matched."""
    client.portal.call(_end, client.app.state.ctx, run_id)
    return run_id


def rows(
    audit_rows: Callable[[TestClient], list[repo.Row]], client: TestClient, *actions: str
) -> list[repo.Row]:
    return [row for row in audit_rows(client) if row["action"] in actions]


@pytest.fixture
def sso(open_client: OpenClient, idp: FakeIdp) -> Iterator[TestClient]:
    """A client of the app under SSO through ``idp``, whose usernames are ``preferred_username``."""
    with open_client(auth={"oidc": idp.settings(username_claim="preferred_username")}) as client:
        yield client


# -- spec versions ---------------------------------------------------------------------------------


def test_a_spec_version_is_requested_reviewed_and_approved(
    sso: TestClient, idp: FakeIdp, audit_rows: Any
) -> None:
    client = sso
    alice, bob = signed_in(client, idp, "alice", "G-author"), signed_in(client, idp, "bob", "G-approver")
    olga = signed_in(client, idp, "olga", "G-op")
    first = client.post("/v1/specs", content=SPEC_YAML, headers=alice).json()
    spec_id = first["spec_id"]
    asked = client.post(
        f"/v1/specs/{spec_id}/approvals", json={"note": "the report shows prices"}, headers=alice
    )
    approval_id = asked.json()["approval_id"]
    listed = client.get("/v1/approvals", params={"status": "requested"}, headers=olga).json()
    as_alice = client.get(f"/v1/approvals/{approval_id}", headers=alice).json()
    shown = client.get(f"/v1/approvals/{approval_id}", headers=bob).json()
    refused_run = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}, headers=olga)
    decided = approve(client, approval_id, bob, note="prices are public")
    again = client.post("/v1/specs", content=SPEC_YAML, headers=alice).json()
    specs = client.get("/v1/specs", headers=olga).json()["items"]
    run_id = started(client, spec_id, olga)
    run = client.get(f"/v1/runs/{run_id}", headers=olga).json()
    ids = {name: actor(client, h) for name, h in (("alice", alice), ("bob", bob))}
    grants = rows(audit_rows, client, "approval.request", "approval.grant")
    content_hash = first["content_hash"]
    assert (first["needs"], first["approval_id"]) == (["clear"], None)
    assert (asked.status_code, asked.json()) == (
        201,
        {"approval_id": approval_id, "status": "requested", "scopes": ["clear"], "approvers_needed": 1},
    )
    assert re.fullmatch(r"A-[0-9A-Z]{26}", approval_id)
    [item] = listed["items"]
    assert (listed["next_cursor"], "attests" in item) == (None, False)
    assert {
        name: item[name] for name in ("spec_name", "spec_version", "spec_owner", "requested_by_name")
    } == {
        "spec_name": "api-demo",
        "spec_version": "1.0.0",
        "spec_owner": "tests",
        "requested_by_name": "alice",
    }
    assert as_alice["you"] == {"can_decide": False, "reason": "needs the rule_approver role"}
    assert shown["you"] == {"can_decide": True, "reason": None}
    assert (shown["request_note"], shown["attests"], shown["review"]["content_hash"]) == (
        "the report shows prices",
        None,
        content_hash,
    )
    review = shown["review"]
    assert review["needed"] == [{"scope": "clear", "pointer": "/security/clear/0", "value": "$.price"}]
    assert review["exclusion"] == {
        "persons": [{"actor": ids["alice"], "name": "alice"}],
        "unattributed": False,
    }
    assert (review["history"], review["previous"]) == ([], None)
    assert review["body"]["key"]["source"]["keys"][0].startswith("tok:") and "K12345678" not in str(shown)
    assert said(refused_run) == (
        409,
        f"spec {spec_id} (api-demo 1.0.0) needs a four-eyes approval of clear before it runs",
    )
    assert decided.status_code == 200
    got = decided.json()
    assert (got["status"], got["decided_by"], got["decided_by_name"], got["decision_note"]) == (
        "approved",
        ids["bob"],
        "bob",
        "prices are public",
    )
    assert (again["needs"], again["approval_id"]) == (["clear"], approval_id)
    assert specs[0]["approval"] == {"approval_id": approval_id, "status": "approved", "scopes": ["clear"]}
    assert run["approval"] | {"deciders": None} == {
        "state": "approved",
        "approval_id": approval_id,
        "needs": ["clear"],
        "mode": "oidc",
        "deciders": None,
        "revoked_by": None,
        "revoked_by_name": None,
        "revoked_at": None,
        "reason": None,
    }
    assert [(d["actor"], d["name"]) for d in run["approval"]["deciders"]] == [(ids["bob"], "bob")]
    request, grant = grants
    assert (request["object"], request["actor"], request["detail"]) == (
        approval_id,
        ids["alice"],
        {"kind": "spec", "spec_id": spec_id, "content_hash": content_hash, "scopes": ["clear"]},
    )
    assert (grant["actor"], grant["session_id"] is not None) == (ids["bob"], True)
    assert grant["detail"] | {"auth_time": None} == {
        "kind": "spec",
        "spec_id": spec_id,
        "content_hash": content_hash,
        "scopes": ["clear"],
        "step": 1,
        "of": 1,
        "authors": [ids["alice"]],
        "auth_time": None,
    }
    assert datetime.fromisoformat(grant["detail"]["auth_time"]) > datetime.now(UTC) - timedelta(minutes=1)
    assert "prices" not in str([row["detail"] for row in audit_rows(client)])  # notes never enter details


def test_a_review_lists_what_each_scope_covers(sso: TestClient, idp: FakeIdp) -> None:
    client = sso
    alice, bob = signed_in(client, idp, "alice", "G-author"), signed_in(client, idp, "bob", "G-approver")
    rule = "{id: %s, when: 'true', classification: accepted, reason: a long reason, ticket: THING-1%s}"
    text = SPEC_YAML.replace("compare: {}", "compare: {include: [$.price]}").replace(
        "security: {clear: [$.price]}", "security: {clear: [$.price], unlisted: clear}"
    )
    text += f"knownDiffs: [{rule % ('KD-1', '')}, {rule % ('KD-2', ', active: false')}]\n"
    asked = client.post(f"/v1/specs/{uploaded(client, alice, text)}/approvals", headers=alice).json()
    needed = client.get(f"/v1/approvals/{asked['approval_id']}", headers=bob).json()["review"]["needed"]
    assert asked["scopes"] == ["clear", "unlisted_clear", "known_diffs", "population_filters"]
    assert [(n["scope"], n["pointer"]) for n in needed] == [
        ("clear", "/security/clear/0"),
        ("unlisted_clear", "/security/unlisted"),
        ("known_diffs", "/knownDiffs/0"),  # an inactive rule suppresses nothing
        ("population_filters", "/compare/include"),
    ]
    assert [n["value"] for n in needed][::3] == ["$.price", ["$.price"]]
    assert needed[2]["value"]["id"] == "KD-1"


def test_a_review_lists_the_lines_a_file_side_skips() -> None:
    """docs/mvp/FILES.md §5: csv.skip above 0 is gated as a filter is, so an approver is shown it."""
    from deltasleuth.api.routes.approvals import _needed

    csv = {"columns": ["id"], "delimiter": "|", "header": False}
    file = {"path": "a.csv", "format": "csv"}
    sides = {
        "a": {"table": {"name": "t", "keyColumns": ["id"], "file": file | {"csv": csv | {"skip": 2}}}},
        "b": {"table": {"name": "t", "keyColumns": ["id"], "file": file | {"csv": csv | {"skip": 0}}}},
    }
    assert _needed({"sides": sides}, ["population_filters"]) == [
        {"scope": "population_filters", "pointer": "/sides/a/table/file/csv/skip", "value": 2}
    ]


def test_an_approval_step_needs_a_sign_in_under_15_minutes_old(sso: TestClient, idp: FakeIdp) -> None:
    client = sso
    alice = signed_in(client, idp, "alice", "G-author")
    stale = signed_in(client, idp, "bob", "G-approver", ago=timedelta(minutes=16))
    recent = signed_in(client, idp, "carol", "G-approver", ago=timedelta(minutes=14))
    spec_id = uploaded(client, alice)
    first = requested(client, spec_id, alice)
    refused = approve(client, first, stale)
    approved = approve(client, first, recent)
    revoked = client.post(f"/v1/approvals/{first}/revoke", json={"reason": "wrong clear path"}, headers=stale)
    second = requested(client, spec_id, alice)
    rejected = decide(client, second, stale, decision="reject", note="clear the currency too")
    third = requested(client, spec_id, alice)
    back = f"#/approvals/{third}"  # where the UI sends the approver back to after a fresh sign-in
    client.cookies.clear()
    again = sign_in(client, idp, groups=["G-approver"], reauth=True, next_=back, sub="bob-sub")
    after = approve(client, third, session_of(client))
    assert said(refused) == (403, "reauth_required")
    assert [r.status_code for r in (approved, revoked, rejected, after)] == [200] * 4
    assert (revoked.json()["status"], rejected.json()["status"], after.json()["status"]) == (
        "revoked",
        "rejected",
        "approved",
    )
    assert again.headers["location"] == f"https://ds.test/{back}"
    assert {name: idp.authorized[-1][name] for name in ("prompt", "max_age")} == {
        "prompt": "login",
        "max_age": "0",
    }


def test_request_refusals(sso: TestClient, idp: FakeIdp) -> None:
    client = sso
    alice, bob = signed_in(client, idp, "alice", "G-author"), signed_in(client, idp, "bob", "G-approver")
    spec_id = uploaded(client, alice)
    path = f"/v1/specs/{spec_id}/approvals"
    unknown = client.post("/v1/specs/999/approvals", headers=alice)
    notes = [client.post(path, json={"note": note}, headers=alice) for note in ("", "x" * 1001, "ok\x1b[31m")]
    approval_id = requested(client, spec_id, alice)
    open_ = client.post(path, headers=alice)
    approve(client, approval_id, bob)
    done = client.post(path, headers=alice)
    assert said(unknown) == (404, "spec 999 not found")
    assert [said(r) for r in notes] == [(422, NOTE)] * 3
    assert said(open_) == (409, f"spec {spec_id} already has an open request {approval_id}")
    assert said(done) == (409, f"spec {spec_id} is already approved: {approval_id}")


def test_text_without_a_utf8_form_and_an_out_of_range_spec_id_are_refused(
    sso: TestClient, idp: FakeIdp
) -> None:
    client = sso
    alice, bob = signed_in(client, idp, "alice", "G-author"), signed_in(client, idp, "bob", "G-approver")
    spec_id = uploaded(client, alice)
    approval_id = requested(client, spec_id, alice)

    def raw(path: str, headers: Headers, text: str) -> Any:  # a lone surrogate, escaped in the JSON
        return client.post(path, content=text, headers=headers | {"Content-Type": "application/json"})

    lone = [
        raw(f"/v1/specs/{spec_id}/approvals", alice, '{"note": "a\\ud800b"}'),
        raw(f"/v1/approvals/{approval_id}/decision", bob, '{"decision": "reject", "note": "x\\udfffy"}'),
        raw(f"/v1/approvals/{approval_id}/revoke", bob, '{"reason": "x\\udfff"}'),
    ]
    listed = [
        client.get("/v1/approvals", params={"spec_id": n}, headers=bob).status_code
        for n in (2**70, 2**63, 0, 2**63 - 1)
    ]
    assert [said(r) for r in lone] == [
        (422, NOTE),
        (422, NOTE),
        (422, "reason must be 1 to 200 characters without control characters"),
    ]
    assert listed == [422, 422, 422, 200]


def test_decision_refusals(sso: TestClient, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = sso
    alice = signed_in(client, idp, "alice", "G-author")
    bob, carol = signed_in(client, idp, "bob", "G-approver"), signed_in(client, idp, "carol", "G-approver")
    spec_id = uploaded(client, bob)  # bob wrote it, and asks for it
    approval_id = requested(client, spec_id, bob)
    unknown = decide(client, "A-" + "0" * 26, carol, decision="reject", note="no")
    missing = [
        decide(client, approval_id, carol, decision="approve"),
        decide(client, approval_id, carol, decision="approve", content_hash="x"),
        decide(client, approval_id, carol, decision="reject"),
        decide(client, approval_id, carol, decision="reject", note="x" * 1001),
    ]
    own = approve(client, approval_id, bob)
    other = [
        approve(client, approval_id, carol, content_hash="0" * 64),
        approve(client, approval_id, carol, scopes=["clear", "known_diffs"]),
    ]
    with monkeypatch.context() as patched:  # a later build's needs
        patched.setattr(approvals, "needs", lambda spec: ["clear", "raw_clear"])
        changed = approve(client, approval_id, carol)

    async def demote(ctx: Context) -> None:  # the IdP took carol's role; her session still holds it
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE ds.users SET roles = '{viewer}' WHERE username = 'carol'")

    async def unauthored(conn: repo.Conn, approval_id: str) -> repo.Row:  # a change the route cannot see
        return {"persons": [], "unattributed": False}

    refused: list[str] = []  # the checks of ds.tg_approvals that refused
    decide_approval = repo.decide_approval

    async def watched(*args: Any, **kwargs: Any) -> repo.Row | None:
        try:
            return await decide_approval(*args, **kwargs)
        except repo.ApprovalRefused as exc:
            refused.append(exc.check)
            raise

    monkeypatch.setattr(repo, "decide_approval", watched)
    with monkeypatch.context() as patched:
        patched.setattr(repo, "approval_authors", unauthored)
        four_eyes = approve(client, approval_id, bob)
    client.portal.call(demote, client.app.state.ctx)
    stored_roles = approve(client, approval_id, carol)
    dave = signed_in(client, idp, "dave", "G-approver")
    decided = approve(client, approval_id, dave)
    late = [
        approve(client, approval_id, carol),
        decide(client, approval_id, carol, decision="reject", note="no"),
    ]
    other_id = requested(client, uploaded(client, bob, _version("1.1.0")), alice)
    uploader = approve(client, other_id, bob)
    assert said(unknown) == (404, "approval A-00000000000000000000000000 not found")
    assert [said(r) for r in missing] == [
        (422, "content_hash and scopes are required to approve a spec version"),
        (422, "content_hash and scopes are required to approve a spec version"),
        (422, "note is required to reject"),
        (422, NOTE),
    ]
    assert said(own) == (409, "approval needs an approver other than you: you requested it")
    assert [said(r) for r in other] == [
        (409, f"the reviewed content hash or scopes are not spec {spec_id}'s; reload and review again")
    ] * 2
    assert said(changed) == (409, f"spec {spec_id}'s needs changed since the request; request it again")
    # ds.tg_approvals' check_violations (23514) are 409s, never 500s:
    assert refused == ["four_eyes", "approver"]
    assert said(four_eyes) == (409, "approval needs an approver other than you: you requested it")
    assert said(stored_roles) == (
        409,
        "your stored roles lack rule_approver, or your account is disabled; sign in again",
    )
    assert decided.json()["status"] == "approved"
    assert [said(r) for r in late] == [(409, f"approval {approval_id} is already approved")] * 2
    assert said(uploader) == (
        409,
        f"approval needs an approver other than you: you uploaded a version of api-demo since spec {spec_id}",
    )


async def _cli_upload(ctx: Context, text: str) -> int:
    """The version stored by ``ds run`` before SSO: its uploader, the CLI, attributes no person."""
    async with ctx.pool.connection() as conn:
        return await upload(conn, compile_spec(parse_yaml_text(text), targets=ctx.targets), TESTER)


def test_cli_and_token_uploads_need_two_approvers(sso: TestClient, idp: FakeIdp, audit_rows: Any) -> None:
    client = sso
    alice = signed_in(client, idp, "alice", "G-author")
    bob, carol = signed_in(client, idp, "bob", "G-approver"), signed_in(client, idp, "carol", "G-approver")
    dave, ada = signed_in(client, idp, "dave", "G-approver"), signed_in(client, idp, "ada", "G-admin")
    ids = {name: actor(client, h) for name, h in (("alice", alice), ("carol", carol), ("ada", ada))}
    # A CLI upload: bob takes the first step, carol the second.
    cli_spec = client.portal.call(_cli_upload, client.app.state.ctx, SPEC_YAML)
    cli = client.post(f"/v1/specs/{cli_spec}/approvals", headers=alice).json()
    shown = client.get(f"/v1/approvals/{cli['approval_id']}", headers=bob).json()
    step1 = approve(client, cli["approval_id"], bob)
    twice = approve(client, cli["approval_id"], bob)
    step2 = approve(client, cli["approval_id"], carol)
    # A token ada created for carol: its uploads name them both, and it attributes neither.
    token = client.post(
        "/v1/auth/tokens",
        json={
            "name": "ci-bot",
            "owner": "payments",
            "owner_user_id": ids["carol"][5:],
            "role": "spec_author",
        },
        headers=ada,
    ).json()
    bearer = {"Authorization": f"Bearer {token['token']}"}
    base = uploaded(client, alice, _version("0.9.0"))
    approve(client, requested(client, base, alice), bob)
    by_token = uploaded(client, bearer, _version("1.1.0"))
    token_request = requested(client, by_token, alice)
    reviewed = client.get(f"/v1/approvals/{token_request}", headers=dave).json()
    owner = approve(client, token_request, carol)
    creator = approve(client, token_request, ada)
    token_steps = [approve(client, token_request, bob), approve(client, token_request, dave)]
    asked_by_token = requested(client, uploaded(client, bearer, _version("1.2.0")), bearer)
    asker = approve(client, asked_by_token, carol)
    grants = rows(audit_rows, client, "approval.grant")
    through = f"through token ci-bot ({token['token_id']})"
    assert (cli["approvers_needed"], shown["review"]["exclusion"]) == (
        2,
        {"persons": [{"actor": ids["alice"], "name": "alice"}], "unattributed": True},
    )
    assert (step1.status_code, step1.json()["status"], step1.json()["first_by_name"]) == (
        200,
        "requested",
        "bob",
    )
    assert said(twice) == (
        409,
        f"you approved step 1 of {cli['approval_id']}; another rule_approver must approve it",
    )
    assert (step2.json()["status"], step2.json()["decided_by_name"]) == ("approved", "carol")
    assert [(g["detail"]["step"], g["detail"]["of"]) for g in grants[:2]] == [(1, 2), (2, 2)]
    assert reviewed["approvers_needed"] == 2
    assert reviewed["review"]["exclusion"]["persons"] == [
        {"actor": a, "name": n} for a, n in sorted((ids[n], n) for n in ("ada", "alice", "carol"))
    ]
    previous = reviewed["review"]["previous"]
    assert (previous["spec_id"], previous["version"], previous["body"]["metadata"]["version"]) == (
        base,
        "0.9.0",
        "0.9.0",
    )
    since = f"you uploaded a version of api-demo since spec {base} {through}"
    assert [said(r) for r in (owner, creator)] == [
        (409, f"approval needs an approver other than you: {since}")
    ] * 2
    assert [r.json()["status"] for r in token_steps] == ["requested", "approved"]
    assert said(asker) == (409, f"approval needs an approver other than you: you requested it {through}")


def test_a_first_approver_who_became_an_author_since_leaves_the_request_to_be_rejected(
    sso: TestClient, idp: FakeIdp
) -> None:
    client = sso
    alice = signed_in(client, idp, "alice", "G-author")
    amy, bob = signed_in(client, idp, "amy", "G-approver"), signed_in(client, idp, "bob", "G-approver")
    spec_id = client.portal.call(_cli_upload, client.app.state.ctx, SPEC_YAML)  # it needs two approvers
    approval_id = requested(client, spec_id, alice)
    step1 = approve(client, approval_id, amy)
    assert uploaded(client, amy) == spec_id  # amy uploads the same content: an author of it now
    shown = client.get(f"/v1/approvals/{approval_id}", headers=bob).json()
    step2 = approve(client, approval_id, bob)
    rejected = decide(client, approval_id, bob, decision="reject", note="amy uploaded it since")
    blocked = (
        f"step 1 of {approval_id} was taken by amy, who is now an author of it; reject it and request it"
        " again"
    )
    assert step1.json()["status"] == "requested"
    assert shown["you"] == {"can_decide": False, "reason": blocked}
    assert said(step2) == (409, blocked)
    assert rejected.json()["status"] == "rejected"


def test_an_author_stored_at_step_1_whom_the_lineage_no_longer_holds_is_told_so(
    sso: TestClient, idp: FakeIdp
) -> None:
    client = sso
    alice = signed_in(client, idp, "alice", "G-author")
    bob, carol = signed_in(client, idp, "bob", "G-approver"), signed_in(client, idp, "carol", "G-approver")
    dan = signed_in(client, idp, "dan", "G-approver")
    base = uploaded(client, dan, _version("0.9.0"))
    spec_id = client.portal.call(_cli_upload, client.app.state.ctx, SPEC_YAML)  # dan's 0.9 is in its lineage
    approval_id = requested(client, spec_id, alice)
    approve(client, approval_id, carol)  # step 1 stores dan as an author
    approve(client, requested(client, base, alice), bob)  # which moves the lineage's base past dan's upload
    assert said(approve(client, approval_id, dan)) == (
        409,
        f"approval needs an approver other than you: you were an author of it when step 1 of {approval_id}"
        " was taken",
    )


# -- sign-off --------------------------------------------------------------------------------------


def _approved_spec(
    client: TestClient, idp: FakeIdp, text: str = SPEC_YAML
) -> tuple[dict[str, Headers], int, str]:
    """alice's upload of ``text``, which bob approved; the people (alice, bob, carol, dave, olga) by name."""
    people = {name: signed_in(client, idp, name, "G-approver") for name in ("bob", "carol", "dave")}
    people |= {
        "alice": signed_in(client, idp, "alice", "G-author"),
        "olga": signed_in(client, idp, "olga", "G-op"),
    }
    spec_id = uploaded(client, people["alice"], text)
    approval_id = requested(client, spec_id, people["alice"])
    assert approve(client, approval_id, people["bob"]).status_code == 200
    return people, spec_id, approval_id


async def _cause(ctx: Context, run_id: str) -> None:
    cause = RootCause(
        "RC-00000000000a", "signature", "$.price", ChangeType.VALUE_CHANGED, Direction.BOTH,
        affected_keys=1, finding_count=1, max_severity=Severity.HIGH, taxonomy_code="VALUE_MISMATCH",
    )  # fmt: skip
    async with ctx.pool.connection() as conn:
        await repo.replace_root_causes(conn, run_id, 1, [cause], {})


def test_an_ended_cycle_is_signed_off_by_someone_who_neither_wrote_started_nor_requested_it(
    sso: TestClient, idp: FakeIdp, audit_rows: Any
) -> None:
    client = sso
    people, spec_id, spec_approval = _approved_spec(client, idp)
    bob, dave, olga = people["bob"], people["dave"], people["olga"]
    run_id = started(client, spec_id, dave)
    path = f"/v1/runs/{run_id}/cycles/1/signoff"
    live = client.post(path, headers=olga)
    unknown = client.post("/v1/runs/R-00000000000000000000000000/cycles/1/signoff", headers=olga)
    ended(client, run_id)
    first = client.post(path, json={"note": "cut-over rehearsal"}, headers=olga).json()
    open_ = client.post(path, headers=olga)
    shown = client.get(f"/v1/approvals/{first['approval_id']}", headers=bob).json()
    starter = approve(client, first["approval_id"], dave)
    missing = decide(client, first["approval_id"], bob, decision="approve")
    client.portal.call(_cause, client.app.state.ctx, run_id)  # a root cause found since the request
    changed = approve(client, first["approval_id"], bob)
    decide(client, first["approval_id"], bob, decision="reject", note="a root cause appeared")
    second = client.post(path, headers=olga).json()
    reviewed = client.get(f"/v1/approvals/{second['approval_id']}", headers=bob).json()
    signed = approve(client, second["approval_id"], bob)
    signed_again = client.post(path, headers=olga)
    run = client.get(f"/v1/runs/{run_id}", headers=olga).json()
    by_run = client.get("/v1/approvals", params={"kind": "signoff", "run_id": run_id}, headers=olga).json()
    page = client.get("/v1/approvals", params={"limit": 1}, headers=olga).json()
    rest = client.get(
        "/v1/approvals", params={"limit": 2, "cursor": page["next_cursor"]}, headers=olga
    ).json()
    by_spec = client.get("/v1/approvals", params={"kind": "spec", "spec_id": spec_id}, headers=olga).json()
    ids = {name: actor(client, h) for name, h in people.items()}
    grant, hold = rows(audit_rows, client, "approval.grant", "retention.hold")[-2:]
    revoke = f"/v1/approvals/{second['approval_id']}/revoke"
    revoked = client.post(revoke, json={"reason": "rehearsal redone"}, headers=people["carol"])
    unheld = client.get(f"/v1/runs/{run_id}", headers=olga).json()["retention"]["hold"]
    release = rows(audit_rows, client, "retention.release")
    what = f"cycle 1 of run {run_id}"
    assert said(live) == (409, f"{what} has not ended")
    assert said(unknown) == (404, "run R-00000000000000000000000000 not found")
    assert (first["status"], first["approvers_needed"]) == ("requested", 1)
    assert re.fullmatch(r"[0-9a-f]{64}", first["attests_sha256"])
    assert said(open_) == (409, f"{what} already has an open sign-off request {first['approval_id']}")
    facts = shown["attests"]
    assert {
        name: facts[name] for name in ("run_id", "cycle", "mode", "spec_approval_id", "created_by", "claim")
    } == {
        "run_id": run_id,
        "cycle": 1,
        "mode": "batch",
        "spec_approval_id": spec_approval,
        "created_by": ids["dave"],
        "claim": "observed",
    }
    assert approvals.attests_sha256(facts) == first["attests_sha256"]
    assert shown["review"]["needed"] == [] and "previous" not in shown["review"]
    assert shown["review"]["exclusion"]["persons"] == [
        {"actor": a, "name": n} for a, n in sorted((ids[n], n) for n in ("alice", "dave", "olga"))
    ]
    assert said(starter) == (409, f"approval needs an approver other than you: you started run {run_id}")
    assert said(missing) == (422, "attests_sha256 is required to approve a sign-off")
    assert said(changed) == (409, f"{what} changed since the sign-off was requested; request it again")
    assert [n["root_cause"]["root_cause_id"] for n in reviewed["review"]["needed"]] == ["RC-00000000000a"]
    assert [(h["approval_id"], h["status"], h["decision_note"]) for h in reviewed["review"]["history"]] == [
        (first["approval_id"], "rejected", "a root cause appeared")
    ]
    assert (signed.status_code, signed.json()["status"]) == (200, "approved")
    assert said(signed_again) == (409, f"{what} is already signed off: {second['approval_id']}")
    assert run["retention"]["hold"]["reason"] == f"signed off: {second['approval_id']}"
    assert run["retention"]["hold"]["by"] == ids["bob"]
    assert [(s["approval_id"], s["cycle"], s["status"], "attests" in s) for s in run["signoffs"]] == [
        (second["approval_id"], 1, "approved", False),
        (first["approval_id"], 1, "rejected", False),
    ]
    assert [a["approval_id"] for a in by_run["items"]] == [second["approval_id"], first["approval_id"]]
    assert [a["approval_id"] for a in page["items"] + rest["items"]] == [
        second["approval_id"],
        first["approval_id"],
        spec_approval,
    ]
    assert [a["approval_id"] for a in by_spec["items"]] == [spec_approval]
    assert grant["detail"] | {"auth_time": None} == {
        "kind": "signoff",
        "spec_id": spec_id,
        "run_id": run_id,
        "cycle": 1,
        "attests_sha256": second["attests_sha256"],
        "step": 1,
        "of": 1,
        "authors": sorted([ids["alice"], ids["dave"], ids["olga"]]),
        "auth_time": None,
    }
    assert (hold["actor"], hold["object"], hold["detail"]) == (
        ids["bob"],
        run_id,
        {"reason": f"signed off: {second['approval_id']}"},
    )
    # Revoking the sign-off releases the hold it placed.
    assert revoked.json() == {
        "approval_id": second["approval_id"],
        "status": "revoked",
        "paused": [],
        "stopped": [],
        "revoked": [],
    }
    assert unheld is None
    assert [(r["actor"], r["object"], r["detail"]) for r in release] == [
        (ids["carol"], run_id, {"reason": f"signed off: {second['approval_id']}"})
    ]


async def _reported(ctx: Context, run_id: str, out: Path, cycle: int | None = None) -> list[str]:
    """What report.pdf's title block and the P0 summary's four-eyes section say of the approvals of the
    run's ``cycle`` (default: its current one)."""
    meta = (await report_document(ctx, run_id, cycle)).meta.splitlines()
    four_eyes = summary_document(await load_run(ctx, run_id, cycle), out).sections[1]
    assert four_eyes.title == "Four-eyes approval" and set(four_eyes.lines) <= set(meta)
    return four_eyes.lines


def _day(at: str) -> str:
    return f"{datetime.fromisoformat(at).astimezone(UTC):%Y-%m-%d}"


async def _queued_one_shot(ctx: Context, spec_id: int, approval_id: str) -> str:
    async with ctx.pool.connection() as conn:
        return await start(conn, spec_id, TESTER, approval_id=approval_id, mode="one_shot", status="queued")


def test_a_revoke_pauses_stops_cascades_and_releases_the_hold(
    sso: TestClient, idp: FakeIdp, audit_rows: Any, tmp_path: Path
) -> None:
    client = sso
    people, spec_id, spec_approval = _approved_spec(client, idp)
    bob, dave, olga = people["bob"], people["dave"], people["olga"]
    running = started(client, spec_id, dave)
    signed_run, open_run = (
        ended(client, started(client, spec_id, dave)),
        ended(client, started(client, spec_id, dave)),
    )
    signoff = client.post(f"/v1/runs/{signed_run}/cycles/1/signoff", headers=olga).json()["approval_id"]
    approve(client, signoff, bob)
    pending = client.post(f"/v1/runs/{open_run}/cycles/1/signoff", headers=olga).json()["approval_id"]
    queued = client.portal.call(_queued_one_shot, client.app.state.ctx, spec_id, spec_approval)
    held = client.get(f"/v1/runs/{signed_run}", headers=olga).json()["retention"]["hold"]
    granted = client.portal.call(_reported, client.app.state.ctx, signed_run, tmp_path)
    carol = signed_in(client, idp, "carol", "G-approver", ago=timedelta(minutes=20))  # no step-up needed
    reasons = [
        client.post(f"/v1/approvals/{spec_approval}/revoke", json={"reason": r}, headers=carol)
        for r in ("", "x" * 201, "wrong\x07")
    ]
    revoked = client.post(
        f"/v1/approvals/{spec_approval}/revoke", json={"reason": "wrong clear path"}, headers=carol
    )
    again = client.post(f"/v1/approvals/{spec_approval}/revoke", json={"reason": "again"}, headers=carol)
    unknown = client.post(f"/v1/approvals/A-{'0' * 26}/revoke", json={"reason": "no"}, headers=carol)
    paused = client.get(f"/v1/runs/{running}", headers=olga).json()
    resumed = client.post(f"/v1/runs/{running}/resume", headers=olga)
    signed = client.get(f"/v1/runs/{signed_run}", headers=olga).json()
    report = client.get(f"/v1/runs/{signed_run}/reports/report.pdf", headers=olga)
    reported = client.portal.call(_reported, client.app.state.ctx, signed_run, tmp_path)
    cascade = {a["approval_id"]: a for a in client.get("/v1/approvals", headers=olga).json()["items"]}
    stopped = client.get(f"/v1/runs/{queued}", headers=olga).json()
    ids = {name: actor(client, h) for name, h in (("bob", bob), ("carol", carol))}
    written = rows(
        audit_rows, client, "paused", "cancelling", "approval.revoke", "approval.reject", "retention.release"
    )
    cause = f"spec approval {spec_approval} was revoked"
    assert held["reason"] == f"signed off: {signoff}"
    assert [said(r) for r in reasons] == [
        (422, "reason must be 1 to 200 characters without control characters")
    ] * 3
    assert (revoked.status_code, revoked.json()) == (
        200,
        {
            "approval_id": spec_approval,
            "status": "revoked",
            "paused": [running],
            "stopped": [queued],
            "revoked": [signoff],
        },
    )
    assert said(again) == (409, f"approval {spec_approval} is revoked, not approved")
    assert said(unknown) == (404, f"approval A-{'0' * 26} not found")
    assert (paused["status"], paused["stop_reason"]) == ("paused", approvals.APPROVAL_PAUSE)
    assert paused["approval"] | {"deciders": None, "revoked_at": None} == {
        "state": "revoked",
        "approval_id": spec_approval,
        "needs": ["clear"],
        "mode": "oidc",
        "deciders": None,
        "revoked_by": ids["carol"],
        "revoked_by_name": "carol",
        "revoked_at": None,
        "reason": "wrong clear path",
    }
    assert said(resumed) == (
        409,
        f"spec {spec_id} (api-demo 1.0.0) needs a new four-eyes approval: {spec_approval} was revoked",
    )
    assert (stopped["status"], stopped["stop_reason"]) == ("cancelling", approvals.APPROVAL_REVOKED)
    assert signed["retention"]["hold"] is None
    assert [(s["approval_id"], s["status"], s["revoke_reason"]) for s in signed["signoffs"]] == [
        (signoff, "revoked", cause)
    ]
    assert (report.status_code, report.headers["content-type"]) == (200, "application/pdf")
    spec_step, signoff_step = cascade[spec_approval], cascade[signoff]
    assert granted == [
        f"Spec approval {spec_approval} was granted by bob on {_day(spec_step['decided_at'])}.",
        f"Sign-off {signoff} was granted by bob on {_day(signoff_step['decided_at'])}.",
    ]
    assert reported == [
        f"Spec approval {spec_approval} was revoked on {_day(spec_step['revoked_at'])} by carol (wrong clear"
        f" path). {approvals.NOT_EVIDENCE}",
        f"Sign-off {signoff} was revoked on {_day(signoff_step['revoked_at'])} by carol ({cause})."
        f" {approvals.NOT_EVIDENCE}",
    ]
    assert (
        cascade[pending]["status"],
        cascade[pending]["decision_note"],
        cascade[pending]["decided_by"],
    ) == (
        "rejected",
        cause,
        ids["carol"],
    )
    assert [(r["action"], r["object"], r["actor"], r["detail"]) for r in written[-6:]] == [
        ("paused", running, ids["carol"], {"reason": approvals.APPROVAL_PAUSE, "approval_id": spec_approval}),
        (
            "cancelling",
            queued,
            ids["carol"],
            {"reason": approvals.APPROVAL_REVOKED, "approval_id": spec_approval},
        ),
        (
            "approval.revoke",
            spec_approval,
            ids["carol"],
            {"reason": "wrong clear path", "paused": [running], "stopped": [queued], "cascade_of": None},
        ),
        (
            "approval.revoke",
            signoff,
            ids["carol"],
            {"reason": cause, "paused": [], "stopped": [], "cascade_of": spec_approval},
        ),
        ("approval.reject", pending, ids["carol"], {"kind": "signoff", "cascade_of": spec_approval}),
        ("retention.release", signed_run, ids["carol"], {"reason": f"signed off: {signoff}"}),
    ]


async def _resumed_under(ctx: Context, run_id: str, approval_id: str) -> None:
    """The run's next cycle, live, under ``approval_id``, as a resume leaves it."""
    async with ctx.pool.connection() as conn:
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        await repo.set_run_approval(conn, run_id, approval_id)


async def _before_008(ctx: Context, run_id: str) -> None:
    async with ctx.pool.connection() as conn:
        await conn.execute(
            """UPDATE ds.runs SET created_at = (SELECT applied_at FROM ds.schema_version WHERE version = 8)
                   - interval '1 second'
               WHERE run_id = %s""",
            (run_id,),
        )


@pytest.mark.parametrize(
    ("pattern", "raw"), [("^K[0-9]{8}$", "K12345678"), ("(?i)^k[0-9]{8}$", "k12345678")]
)  # a leading inline flag is read as the key check reads it: a 422, never a 500
def test_a_revoke_reason_holding_a_key_shaped_value_is_refused(
    sso: TestClient, idp: FakeIdp, pattern: str, raw: str
) -> None:
    client = sso
    people, _, spec_approval = _approved_spec(client, idp, SPEC_YAML.replace("^K[0-9]{8}$", pattern))
    carol = signed_in(client, idp, "carol", "G-approver")
    keyed = client.post(
        f"/v1/approvals/{spec_approval}/revoke", json={"reason": f"customer {raw} asked"}, headers=carol
    )
    shown = client.get(f"/v1/approvals/{spec_approval}", headers=people["olga"]).json()
    assert said(keyed) == (422, "the reason holds a key-shaped value: write it without customer keys")
    assert shown["status"] == "approved"  # nothing revoked, nothing audited
    revoked = client.post(
        f"/v1/approvals/{spec_approval}/revoke", json={"reason": "wrong clear path"}, headers=carol
    )
    assert revoked.status_code == 200


def test_reports_label_the_reported_cycle_by_the_approval_it_ran_under(
    sso: TestClient, idp: FakeIdp, tmp_path: Path
) -> None:
    client = sso
    ctx = client.app.state.ctx
    people, spec_id, first = _approved_spec(client, idp)
    run_id = ended(client, started(client, spec_id, people["dave"]))
    reason = {"reason": "wrong clear path"}
    assert (
        client.post(f"/v1/approvals/{first}/revoke", json=reason, headers=people["carol"]).status_code == 200
    )
    second = requested(client, spec_id, people["alice"])
    granted = approve(client, second, people["carol"]).json()
    client.portal.call(_resumed_under, ctx, run_id, second)
    cycles = [client.portal.call(_reported, ctx, run_id, tmp_path, cycle) for cycle in (1, 2)]
    client.portal.call(_before_008, ctx, run_id)
    before = [client.portal.call(_reported, ctx, run_id, tmp_path, cycle) for cycle in (1, 2)]
    shown = client.get(f"/v1/runs/{run_id}", headers=people["olga"]).json()["approval"]
    [revoked] = cycles[0]
    assert revoked.startswith(f"Spec approval {first} was revoked on ") and approvals.NOT_EVIDENCE in revoked
    assert cycles[1] == [f"Spec approval {second} was granted by carol on {_day(granted['decided_at'])}."]
    unapproved = f"Unapproved spec version: the run started before approvals (008). {approvals.NOT_EVIDENCE}"
    assert before == [[unapproved]] * 2
    assert (shown["state"], shown["approval_id"], shown["needs"], shown["mode"]) == (
        "unapproved", second, None, None,
    )  # fmt: skip


def test_a_signed_cycle_survives_a_purge_pass_and_is_revoked_by_one_after_its_release(
    sso: TestClient, idp: FakeIdp, audit_rows: Any
) -> None:
    client = sso
    ctx = client.app.state.ctx
    people, spec_id, _ = _approved_spec(client, idp)
    run_id = ended(client, started(client, spec_id, people["dave"]))
    signoff = client.post(f"/v1/runs/{run_id}/cycles/1/signoff", headers=people["olga"]).json()["approval_id"]
    approve(client, signoff, people["bob"])

    async def expire() -> None:  # the cycle ended past its retention
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE ds.cycles SET ended_at = ended_at - interval '100 days' WHERE run_id = %s", (run_id,)
            )

    client.portal.call(expire)
    kept = client.portal.call(purge.purge, ctx, WORKER)
    still = client.get(f"/v1/approvals/{signoff}", headers=people["olga"]).json()
    client.portal.call(purge.hold, ctx, TESTER, run_id, None)  # ds purge release
    gone = client.portal.call(purge.purge, ctx, WORKER)
    revoked = client.get(f"/v1/approvals/{signoff}", headers=people["olga"]).json()
    purged = client.post(f"/v1/runs/{run_id}/cycles/1/signoff", headers=people["olga"])
    trail = [(r["action"], r["object"], r["actor"]) for r in audit_rows(client) if r["actor"] == WORKER.actor]
    revoke_row = rows(audit_rows, client, "approval.revoke")[-1]
    assert (kept.cycles, kept.held, still["status"]) == (0, 1, "approved")
    assert (gone.cycles, revoked["status"], revoked["revoked_by"], revoked["revoke_reason"]) == (
        1,
        "revoked",
        WORKER.actor,
        "cycle 1 purged",
    )
    assert trail[-4:-1] == [
        ("approval.revoke", signoff, WORKER.actor),  # in the transaction that starts the purge
        ("retention.expire", run_id, WORKER.actor),
        ("retention.purged", run_id, WORKER.actor),
    ]
    assert revoke_row["detail"] == {
        "reason": "cycle 1 purged",
        "paused": [],
        "stopped": [],
        "cascade_of": None,
    }
    assert said(purged)[0] == 410
    assert said(purged)[1].startswith(f"cycle 1 of run {run_id} was purged on ")


def test_the_scorer_of_the_attested_adjudication_does_not_approve_the_sign_off(
    sso: TestClient, idp: FakeIdp
) -> None:
    client = sso
    ctx = client.app.state.ctx
    people, spec_id, _ = _approved_spec(client, idp)
    run_id = ended(client, started(client, spec_id, people["dave"]))
    olga = Principal(actor(client, people["olga"]), "olga", expand(["operator"]))
    carol = Principal(actor(client, people["carol"]), "carol", expand(["rule_approver"]))
    drawn = client.portal.call(partial(store_draw, ctx, run_id, 1, who=olga))
    upload = render_csv(SHEET_COLUMNS, []).encode()  # the cycle compared no key here: nothing to review
    client.portal.call(partial(store_score, ctx, drawn["adjudication_id"], upload, who=carol))
    signoff = client.post(f"/v1/runs/{run_id}/cycles/1/signoff", headers=people["olga"]).json()["approval_id"]
    shown = client.get(f"/v1/approvals/{signoff}", headers=people["carol"]).json()
    refused = approve(client, signoff, people["carol"])
    signed = approve(client, signoff, people["bob"])
    why = f"approval needs an approver other than you: you scored its adjudication {drawn['adjudication_id']}"
    assert shown["attests"]["adjudication"]["adjudication_id"] == drawn["adjudication_id"]
    assert shown["you"] == {"can_decide": False, "reason": why}
    assert carol.actor in [p["actor"] for p in shown["review"]["exclusion"]["persons"]]
    assert said(refused) == (409, why)
    assert (signed.status_code, signed.json()["status"]) == (200, "approved")


def test_a_sign_off_requested_before_packs_is_decided_by_its_format_0_facts(
    sso: TestClient, idp: FakeIdp
) -> None:
    client = sso
    ctx = client.app.state.ctx
    people, spec_id, _ = _approved_spec(client, idp)
    run_id = ended(client, started(client, spec_id, people["dave"]))
    olga = actor(client, people["olga"])

    async def request() -> str:  # as 008 requested it: attests format 0
        async with ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
            facts = await approvals.rebuilt(conn, run, 1, await ctx.spec(conn, spec_id), {})  # type: ignore[arg-type]
            assert facts is not None
            row = await repo.create_approval(
                conn, "signoff", spec_id, olga, run_id=run_id, cycle=1, attests=facts,
                attests_sha256=approvals.attests_sha256(facts),
            )  # fmt: skip
            assert row is not None
            return row["approval_id"]

    signoff = client.portal.call(request)
    shown = client.get(f"/v1/approvals/{signoff}", headers=people["bob"]).json()
    signed = approve(client, signoff, people["bob"])
    assert not {"format", "census", "adjudication"} & shown["attests"].keys()
    assert (signed.status_code, signed.json()["status"]) == (200, "approved")


# -- modes without approvers -----------------------------------------------------------------------


def test_tokens_and_developer_modes_label_runs_and_refuse_approvals(
    open_client: OpenClient, callers: Any
) -> None:
    replies = {}
    with open_client(auth={}) as client:  # tokens mode: every principal is a machine
        author, operator = (
            callers(client).token("spec_author", "ci-author"),
            callers(client).token("operator"),
        )
        spec_id = uploaded(client, author)
        run_id = started(client, spec_id, operator)
        replies["tokens"] = [
            client.post(f"/v1/specs/{spec_id}/approvals", headers=author),
            client.post(f"/v1/runs/{run_id}/cycles/1/signoff", headers=operator),
            client.post(
                "/v1/approvals/A-x/decision", json={"decision": "reject", "note": "x"}, headers=author
            ),
            client.post("/v1/approvals/A-x/revoke", json={"reason": "x"}, headers=author),
        ]
        tokens_run = client.get(f"/v1/runs/{run_id}", headers=operator).json()["approval"]
    with open_client() as client:  # developer mode: nobody signed in
        spec_id = uploaded(client, {})
        run_id = started(client, spec_id, {})
        replies["developer"] = [
            client.post(f"/v1/specs/{spec_id}/approvals"),
            client.post(f"/v1/runs/{run_id}/cycles/1/signoff"),
            client.post("/v1/approvals/A-x/decision", json={"decision": "reject", "note": "x"}),
            client.post("/v1/approvals/A-x/revoke", json={"reason": "x"}),
        ]
        developer_run = client.get(f"/v1/runs/{run_id}").json()["approval"]
        listed = client.get("/v1/approvals").json()
    assert {mode: [said(r) for r in answers] for mode, answers in replies.items()} == {
        "tokens": [(409, "approvals need SSO (auth.oidc); this deployment runs in tokens mode")] * 2
        + [(403, "needs the rule_approver role")] * 2,
        "developer": [(409, "approvals need SSO (auth.oidc); this deployment runs in developer mode")] * 2
        + [(403, "the rule_approver role needs a signed-in person")] * 2,
    }
    assert [(a["state"], a["needs"], a["mode"], a["approval_id"]) for a in (tokens_run, developer_run)] == [
        ("unapproved", ["clear"], "tokens", None),
        ("unapproved", ["clear"], "none", None),
    ]
    assert listed == {"items": [], "next_cursor": None}
