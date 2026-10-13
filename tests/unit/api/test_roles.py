"""Roles (docs/mvp/AUTH.md §1, §4.1): every /v1 operation checks the role the contract gives it, for
bearer tokens, cookie sessions and developer mode; the person-required roles need a person; denials
are audited, a row per caller and route a minute; a chat session is its creator's."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from deltasleuth.api import create_app
from deltasleuth.api.deps import require
from deltasleuth.api.routes import meta
from deltasleuth.auth.principal import Principal, expand
from deltasleuth.config import Settings
from tests.conftest import BUILDS
from tests.unit.auth.fake_idp import FakeIdp, sign_in

from .conftest import OIDC, append_chat

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
ROUTES = {  # docs/mvp/AUTH.md §4.1; "anyone": any principal
    ("GET", "/v1/auth/me"): "anyone",
    ("GET", "/v1/targets"): "viewer",
    ("PUT", "/v1/targets/{target_id}"): "admin",  # who declares (test_who_declares): signed in within 15
    ("DELETE", "/v1/targets/{target_id}"): "admin",  # minutes; in a trial, spec_author
    ("GET", "/v1/llm/profiles"): "viewer",
    ("GET", "/v1/specs"): "viewer",
    ("GET", "/v1/specs/{spec_id}"): "viewer",
    ("POST", "/v1/specs/validate"): "spec_author",
    ("POST", "/v1/specs/try"): "spec_author",
    ("POST", "/v1/profile"): "spec_author",
    ("POST", "/v1/specs"): "spec_author",
    ("POST", "/v1/runs"): "operator",
    ("POST", "/v1/runs/{run_id}/{action}"): "operator",
    ("POST", "/v1/compare"): "operator",
    ("GET", "/v1/runs"): "viewer",
    ("GET", "/v1/runs/{run_id}"): "viewer",
    ("GET", "/v1/runs/{run_id}/results"): "viewer",
    ("GET", "/v1/runs/{run_id}/findings"): "viewer",
    ("GET", "/v1/runs/{run_id}/root-causes"): "viewer",
    ("GET", "/v1/runs/{run_id}/changes"): "viewer",
    ("GET", "/v1/runs/{run_id}/graph"): "viewer",
    ("GET", "/v1/runs/{run_id}/keys/{key}"): "viewer",
    ("GET", "/v1/runs/{run_id}/reports/{name}"): "viewer",
    ("POST", "/v1/chat/sessions"): "viewer",
    ("POST", "/v1/chat/sessions/explain"): "viewer",
    ("POST", "/v1/chat/sessions/{session_id}/messages"): "viewer",
    ("GET", "/v1/chat/sessions/{session_id}"): "viewer",
    ("GET", "/v1/auth/tokens"): "admin",
    ("POST", "/v1/auth/tokens"): "admin",
    ("DELETE", "/v1/auth/tokens/{token_id}"): "admin",
    ("GET", "/v1/auth/sessions"): "admin",
    ("DELETE", "/v1/auth/sessions/{session_id}"): "admin",
    ("GET", "/v1/auth/users"): "admin",
    ("POST", "/v1/auth/users/{user_id}/disable"): "admin",
    ("POST", "/v1/auth/users/{user_id}/enable"): "admin",
    ("GET", "/v1/audit"): "admin",
    ("GET", "/v1/approvals"): "viewer",  # docs/mvp/APPROVALS.md §6.1
    ("GET", "/v1/approvals/{approval_id}"): "viewer",
    ("POST", "/v1/specs/{spec_id}/approvals"): "spec_author",
    ("POST", "/v1/runs/{run_id}/cycles/{cycle}/signoff"): "operator",
    ("POST", "/v1/approvals/{approval_id}/decision"): "rule_approver",
    ("POST", "/v1/approvals/{approval_id}/revoke"): "rule_approver",
    ("POST", "/v1/runs/{run_id}/cycles/{cycle}/adjudications"): "operator",  # docs/mvp/PACK.md §7.1
    ("GET", "/v1/runs/{run_id}/cycles/{cycle}/adjudications"): "viewer",
    ("GET", "/v1/adjudications/{adjudication_id}/sheet.csv"): "viewer",
    ("POST", "/v1/adjudications/{adjudication_id}/score"): "operator",
    ("POST", "/v1/runs/{run_id}/cycles/{cycle}/labellings"): "operator",  # docs/mvp/ACCURACY.md §10
    ("GET", "/v1/runs/{run_id}/cycles/{cycle}/labellings"): "viewer",
    ("GET", "/v1/labellings/{labelling_id}"): "viewer",
    ("GET", "/v1/labellings/{labelling_id}/sheet.csv"): "viewer",
    ("POST", "/v1/labellings/{labelling_id}/labels"): "operator",
    ("POST", "/v1/runs/{run_id}/cycles/{cycle}/packs"): "operator",  # a person: 403 for a token
    ("GET", "/v1/packs"): "viewer",
    ("GET", "/v1/packs/revocations"): "viewer",
    ("GET", "/v1/packs/{pack_id}"): "viewer",
    ("GET", "/v1/packs/{pack_id}/download"): "viewer",
    ("GET", "/v1/licence"): "viewer",  # docs/mvp/LICENCE.md §12
    ("GET", "/v1/samples"): "viewer",  # docs/mvp/SAMPLING.md §14
    ("GET", "/v1/samples/{draw_id}"): "viewer",
    ("POST", "/v1/samples/close"): "operator",  # a person: 403 for a token
    ("GET", "/v1/notifications"): "viewer",  # docs/mvp/ALERTS.md §11
    ("GET", "/v1/notifications/deliveries"): "viewer",
    ("POST", "/v1/notifications/channels/{channel}/test"): "operator",
    ("POST", "/v1/notifications/channels/{channel}/retry"): "operator",
    ("POST", "/v1/notifications/mutes"): "admin",  # a person signed in within 15 minutes
    ("POST", "/v1/notifications/mutes/{mute_id}/end"): "admin",
    ("GET", "/v1/readiness"): "viewer",  # docs/mvp/READINESS.md §10: nothing writes readiness
    ("GET", "/v1/readiness/pairs/{name}"): "viewer",
    ("GET", "/v1/readiness/pairs/{name}/cycles"): "viewer",
    ("GET", "/v1/readiness/pairs/{name}/brief"): "viewer",
    ("GET", "/v1/readiness/transitions"): "viewer",
    ("GET", "/v1/readiness/reports/{name}"): "viewer",
    ("GET", "/v1/schedules"): "viewer",  # docs/mvp/SCHEDULES.md §10
    ("GET", "/v1/schedules/zones"): "viewer",
    ("GET", "/v1/schedules/{schedule_id}"): "viewer",
    ("GET", "/v1/schedules/{schedule_id}/firings"): "viewer",
    ("POST", "/v1/schedules/preview"): "operator",
    ("POST", "/v1/schedules"): "operator",
    ("PUT", "/v1/schedules/{schedule_id}"): "operator",
    ("POST", "/v1/schedules/{schedule_id}/pause"): "operator",
    ("POST", "/v1/schedules/{schedule_id}/resume"): "operator",
    ("DELETE", "/v1/schedules/{schedule_id}"): "operator",
    ("POST", "/v1/prechecks"): "operator",  # docs/mvp/PRECHECKS.md §11
    ("POST", "/v1/prechecks/catalogue"): "spec_author",
    ("GET", "/v1/prechecks"): "viewer",
    ("GET", "/v1/prechecks/{precheck_id}"): "viewer",
    ("GET", "/v1/prechecks/{precheck_id}/pairs/{n}"): "viewer",
    ("POST", "/v1/prechecks/{precheck_id}/cancel"): "operator",
    ("GET", "/v1/prechecks/{precheck_id}/reports/{name}"): "viewer",
    ("GET", "/v1/prechecks/{precheck_id}/pairs/{n}/draft.yaml"): "spec_author",
    ("POST", "/v1/mcp"): "viewer",  # docs/mvp/MCP.md §5.1: an API token's; 404 unless mcp.http serves it
    ("GET", "/v1/mcp"): "viewer",  # 405 where it is served: no stream
    ("DELETE", "/v1/mcp"): "viewer",  # 405: no session
}
PUBLIC = {
    ("GET", "/v1/health"),
    ("GET", "/v1/auth/login"),
    ("GET", "/v1/auth/callback"),
    ("POST", "/v1/auth/token-login"),
    ("POST", "/v1/auth/logout"),  # it resolves the caller itself (test_auth_flow.py)
}
LOWER = {
    "operator": "viewer",
    "spec_author": "operator",
    "rule_approver": "spec_author",
    "admin": "spec_author",
}
PERSONS_ONLY = ("rule_approver", "admin")  # no token holds these roles (docs/mvp/AUTH.md §5)


def _operations(client: TestClient) -> list[tuple[str, str]]:
    paths = client.app.openapi()["paths"]
    return [(method.upper(), path) for path, item in paths.items() for method in item]


def _call(client: TestClient, operation: tuple[str, str], headers: dict[str, str]) -> int:
    method, path = operation
    return client.request(method, re.sub(r"\{[^}]+\}", "x", path), headers=headers).status_code


def test_every_operation_is_listed(open_client: OpenClient, tmp_path: Path) -> None:
    """With the UI built and without it: a checkout need not hold ``web/dist``, and S1's ``GET /`` is no
    operation (docs/mvp/FIRSTRUN.md §8)."""
    for web_dir in (tmp_path, tmp_path / "no-ui"):
        with open_client(web_dir=web_dir) as client:
            assert set(_operations(client)) - PUBLIC == set(ROUTES), web_dir


def test_every_operation_checks_its_role(open_client: OpenClient, callers: Any) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        as_ = callers(client)
        problems = []
        for operation, role in ROUTES.items():
            anyone = role == "anyone"
            fresh = {"auth_time": datetime.now(UTC)}  # token creation needs a recent sign-in
            right = [as_.session(*([] if anyone else expand([role])), username="bob", **fresh)]
            right += [as_.token("viewer" if anyone else role)] if role not in PERSONS_ONLY else []
            refusals = [_call(client, operation, {})]
            if not anyone:  # No role sits below viewer: the too-low caller is a session holding none.
                low = as_.session() if role == "viewer" else as_.token(LOWER[role])
                refusals.append(_call(client, operation, low))
            got = [_call(client, operation, headers) for headers in right]
            if refusals != [401, 403][: len(refusals)] or {401, 403} & set(got):
                problems.append((operation, refusals, got))
    assert problems == []


def test_developer_mode_passes_every_operation(open_client: OpenClient) -> None:
    with open_client() as client:
        got = {operation: _call(client, operation, {}) for operation in ROUTES}
    refused = {operation: status for operation, status in got.items() if status in (401, 403)}
    assert refused == {  # developer mode acts as admin, never as a person
        operation: 403 for operation, role in ROUTES.items() if role == "rule_approver"
    }


def test_who_declares(open_client: OpenClient, callers: Any, tmp_path: Path) -> None:
    """docs/mvp/AUTH.md §4.1: a target is declared in the product by an admin signed in within 15 minutes;
    in a trial (tokens mode at a plain-http loopback address) by a spec_author's token; by no one in tokens
    mode behind https, which has no admin and keeps the targets file; and in developer mode, which acts as
    admin."""

    def declares(client: TestClient, headers: dict[str, str]) -> list[tuple[int, str | None]]:
        put = client.put("/v1/targets/own", json={"base_url": "https://own.example"}, headers=headers)
        answers = (put, client.delete("/v1/targets/own", headers=headers))
        return [
            (answer.status_code, answer.json().get("detail") if answer.content else None)
            for answer in answers
        ]

    done, now = [(201, None), (204, None)], datetime.now(UTC)
    # SSO first: developer mode then leaves a workspace tokeniser.key, which stops an encrypted start.
    with open_client(auth={"oidc": OIDC}, web_dir=tmp_path / "no-ui") as client:
        as_ = callers(client)
        stale = as_.session(*expand(["admin"]), username="bob", auth_time=now - timedelta(minutes=16))
        author = as_.session(*expand(["spec_author"]), username="carol", auth_time=now)
        assert declares(client, as_.session(*expand(["admin"]), auth_time=now)) == done
        assert declares(client, stale) == [(403, "reauth_required")] * 2
        assert declares(client, author) == [(403, "needs the admin role")] * 2
        assert declares(client, as_.token("spec_author", "sso-bot")) == [(403, "needs the admin role")] * 2
        assert client.get("/v1/targets", headers=author).json()["declarer"] == "admin"
    with open_client(auth={"public_url": "http://127.0.0.1:8000"}) as client:  # a trial, as deploy/up.sh's
        as_ = callers(client)
        author = as_.token("spec_author", "trial-author")
        assert declares(client, author) == done
        assert (
            declares(client, as_.token("operator", "trial-operator"))
            == [(403, "needs the spec_author role")] * 2
        )
        assert client.get("/v1/targets", headers=author).json()["declarer"] == "spec_author"
    with open_client(auth={}) as client:  # tokens mode behind https
        author = callers(client).token("spec_author", "office-author")
        assert declares(client, author) == [(403, "needs the admin role")] * 2
        assert client.get("/v1/targets", headers=author).json()["declarer"] == "admin"
    with open_client() as client:
        assert declares(client, {}) == done


def test_every_unauthenticated_call_is_challenged(open_client: OpenClient) -> None:
    with open_client(auth={}) as client:
        missing = client.get("/v1/specs")
        wrong = client.get("/v1/specs", headers={"Authorization": "Bearer dst_" + "0" * 16 + "_" + "0" * 64})
        garbage = client.get("/v1/specs", headers={"Authorization": "Bearer s3cret"})
        expired = client.get("/v1/specs", headers={"Cookie": "__Host-ds_session=nope"})
        forged = client.get("/v1/specs", headers={"X-Forwarded-User": "admin", "SM_USER": "admin"})
    assert [(r.status_code, r.json()["detail"], r.headers["www-authenticate"]) for r in (
        missing, wrong, garbage, expired, forged
    )] == [
        (401, "not signed in", "Bearer"),
        (401, "invalid or expired API token", "Bearer"),
        (401, "invalid or expired API token", "Bearer"),
        (401, "session expired", "Bearer"),
        (401, "not signed in", "Bearer"),
    ]  # fmt: skip


def test_a_cookie_session_needs_its_csrf_token_and_a_bearer_does_not(
    open_client: OpenClient, callers: Any, spec_yaml: str
) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        session = callers(client).session(*expand(["spec_author"]))
        token = callers(client).token("spec_author")
        refused = client.post("/v1/specs", content=spec_yaml, headers=session | {"X-CSRF-Token": "x"})
        missing = client.post("/v1/specs", content=spec_yaml, headers={"Cookie": session["Cookie"]})
        signed = client.post("/v1/specs", content=spec_yaml, headers=session)
        bearer = client.post("/v1/specs", content=spec_yaml, headers=token)
    assert [(r.status_code, r.json().get("detail")) for r in (refused, missing)] == [
        (403, "missing or invalid CSRF token")
    ] * 2
    assert (signed.status_code, bearer.status_code) == (201, 201)


def test_create_app_refuses_a_route_without_a_principal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unguarded = APIRouter()

    @unguarded.get("/secrets")
    async def secrets() -> None:
        pass

    monkeypatch.setattr(meta, "router", unguarded)
    settings = Settings("file:/unused", tmp_path, tmp_path, tmp_path / "targets.yaml")
    with pytest.raises(RuntimeError, match=r"GET', '/v1/secrets'.*declares no principal"):
        create_app(settings)


async def _approve(who: Annotated[Principal, Depends(require("rule_approver"))]) -> dict[str, str]:
    return {"actor": who.actor}


async def _reveal(who: Annotated[Principal, Depends(require("pii_reader"))]) -> dict[str, str]:
    return {"actor": who.actor}


def _person_required(client: TestClient, headers: dict[str, str]) -> list[tuple[int, str]]:
    client.app.add_api_route("/v1/approve", _approve, methods=["POST"])
    client.app.add_api_route("/v1/reveal", _reveal, methods=["POST"])
    replies = [client.post(path, headers=headers) for path in ("/v1/approve", "/v1/reveal")]
    return [(r.status_code, r.json().get("detail") or r.json()["actor"][:5]) for r in replies]


def test_person_required_roles_are_granted_to_people_only(
    open_client: OpenClient, callers: Any, tmp_path: Path
) -> None:
    # SSO first: developer mode then leaves a workspace tokeniser.key, which stops an encrypted start.
    with open_client(auth={"oidc": OIDC}, web_dir=tmp_path / "no-ui") as client:
        token = callers(client).token("spec_author")
        person = callers(client).session(*expand(["pii_reader"]))
        assert _person_required(client, token) == [
            (403, "needs the rule_approver role"),
            (403, "needs the pii_reader role"),
        ]
        assert _person_required(client, person) == [(200, "user:"), (200, "user:")]
    with open_client(web_dir=tmp_path / "no-ui") as client:
        assert _person_required(client, {}) == [
            (403, "the rule_approver role needs a signed-in person"),
            (403, "the pii_reader role needs a signed-in person"),
        ]  # developer mode acts as admin, never as a person


def test_denials_are_audited_once_a_minute_per_caller_and_route(
    open_client: OpenClient, callers: Any, audit_rows: Any
) -> None:
    with open_client(auth={}) as client:
        viewer = callers(client).token("viewer")
        denied = [client.post("/v1/runs", headers=viewer) for _ in range(100)]
        assert client.get("/v1/runs", headers=viewer).status_code == 200  # another route, allowed
        denials = client.app.state.denials
        later = denials.clock() + 61
        denials.clock = lambda: later
        last = client.post("/v1/runs", headers=viewer)
        rows = [r for r in audit_rows(client) if r["action"] == "authz.denied"]
    assert {(r.status_code, r.json()["detail"]) for r in [*denied, last]} == {
        (403, "needs the operator role")
    }
    assert [(r["actor"][:8], r["actor_name"], r["object"], r["detail"]) for r in rows] == [
        ("token:T-", "ci-bot", "POST /v1/runs", {"needed": "operator", "count": 1}),
        ("token:T-", "ci-bot", "POST /v1/runs", {"needed": "operator", "count": 100}),
    ]


@pytest.mark.parametrize("build", BUILDS, indirect=True)
def test_a_chat_session_is_its_creator_s(
    build: str, open_client: OpenClient, callers: Any, stub: Any, seed: Any, audit: Any
) -> None:
    async def answer(ctx: Any, session_id: str, question: str, who: Principal, about: Any) -> None:
        await append_chat(ctx, session_id, "user", question)

    stub("deltasleuth.agent.loop", answer=answer)
    # chat needs no licence (LICENCE.md §2): not a vendor build's evaluation under SSO, which refuses runs
    with open_client(auth={"oidc": OIDC}, ai=True, tier=None) as client:
        run_id = seed(client).run_id
        alice, bob = callers(client).session("viewer"), callers(client).session("viewer", username="bob")
        session_id = client.post("/v1/chat/sessions", json={"run_id": run_id}, headers=alice).json()[
            "session_id"
        ]
        path = f"/v1/chat/sessions/{session_id}"
        others = [
            client.get(path, headers=bob),
            client.post(path + "/messages", json={"content": "?"}, headers=bob),
        ]
        own = client.post(path + "/messages", json={"content": "Why?"}, headers=alice)
        draft = client.post("/v1/chat/sessions/explain", json={"run_id": run_id}, headers=alice)
        theirs = client.get(f"/v1/chat/sessions/{draft.json()['session_id']}", headers=bob)

        async def forget(ctx: Any) -> None:  # as a session stored before 005
            async with ctx.pool.connection() as conn:
                await conn.execute("UPDATE ds.chat_sessions SET created_by = NULL")

        client.portal.call(forget, client.app.state.ctx)
        legacy = client.get(path, headers=alice)
        assert audit(client) == ["chat.create", "chat.ask"] * 2
    missing = (404, f"chat session {session_id} not found")
    assert [(r.status_code, r.json()["detail"]) for r in [*others, legacy]] == [missing] * 3
    assert own.status_code == 200 and [m["content"] for m in own.json()["messages"]] == ["Why?"]
    drafted = draft.json()["session_id"]  # a fixed question's draft is its asker's too
    assert (draft.status_code, len(draft.json()["messages"])) == (201, 1)
    assert (theirs.status_code, theirs.json()["detail"]) == (404, f"chat session {drafted} not found")


def test_a_signed_in_person_holds_the_person_required_roles_the_idp_grants(
    open_client: OpenClient, idp: FakeIdp, tmp_path: Path
) -> None:
    with open_client(auth={"oidc": idp.settings()}, web_dir=tmp_path / "no-ui") as client:
        sign_in(client, idp, groups=["G-approver"])
        csrf = {"X-CSRF-Token": client.get("/v1/auth/me").json()["csrf"]}
        assert _person_required(client, csrf) == [(200, "user:"), (403, "needs the pii_reader role")]
