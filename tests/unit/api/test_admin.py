"""Administration over the API: API tokens (shown once, owned by a person,
created after a fresh sign-in), revocation, disabling and enabling users, and sessions."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth.auth import tokens
from deltasleuth.auth.principal import Principal, expand
from tests.conftest import TESTER

from .conftest import OIDC

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
ADMIN = tuple(expand(["admin"]))
TOKEN = {"name": "ci-deploy", "owner": "payments team", "role": "operator"}


def _admin(callers: Any, username: str = "alice", ago: timedelta = timedelta()) -> dict[str, str]:
    """An admin's session, signed in ``ago``."""
    return callers.session(*ADMIN, username=username, auth_time=datetime.now(UTC) - ago)


def _user_id(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/v1/auth/me", headers=headers).json()["actor"].removeprefix("user:")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_token_is_shown_once_and_names_its_owner(
    open_client: OpenClient, callers: Any, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        admin = _admin(callers(client))
        owner = _user_id(client, admin)
        created = client.post(
            "/v1/auth/tokens", json=TOKEN | {"owner_user_id": owner, "days": 30}, headers=admin
        )
        body = created.json()
        listed = client.get("/v1/auth/tokens", headers=admin).json()["items"]
        used = client.get("/v1/runs", headers=_bearer(body["token"]))
        [row] = [row for row in audit_rows(client) if row["action"] == "token.create"]
    assert (created.status_code, created.headers["cache-control"]) == (201, "no-store")
    assert set(body) == {"token_id", "name", "owner", "owner_user_id", "role", "expires_at", "token"}
    assert re.fullmatch(r"dst_[0-9a-f]{16}_[0-9a-f]{64}", body["token"])
    assert body["token_id"] == tokens.token_id(body["token"]) and used.status_code == 200
    assert [(t["token_id"], t["state"], t["created_by"], t["owner_user_id"]) for t in listed] == [
        (body["token_id"], "active", f"user:{owner}", owner)
    ]
    assert body["token"] not in str(listed) and not {"token", "secret_hash"} & set(listed[0])
    assert (row["object"], row["detail"]) == (
        body["token_id"],
        {**TOKEN, "owner_user_id": owner, "expires_at": body["expires_at"]},
    )


def test_owner_user_id_is_required_under_sso_and_must_be_an_enabled_user(
    open_client: OpenClient, callers: Any
) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        as_ = callers(client)
        admin, bob = _admin(as_), as_.session("viewer", username="bob")
        bob_id = _user_id(client, bob)
        client.post(f"/v1/auth/users/{bob_id}/disable", headers=admin)
        replies = [
            client.post("/v1/auth/tokens", json=TOKEN | fields, headers=admin)
            for fields in ({}, {"owner_user_id": "U-0000000000000000"}, {"owner_user_id": bob_id})
        ]
    with open_client() as client:  # developer mode: no SSO, no person to name
        dev = client.post("/v1/auth/tokens", json=TOKEN)
    assert [(r.status_code, r.json()["detail"]) for r in replies] == [
        (422, "owner_user_id is required"),
        (422, "owner_user_id U-0000000000000000 is not an enabled user"),
        (422, f"owner_user_id {bob_id} is not an enabled user"),
    ]
    assert (dev.status_code, dev.json()["owner_user_id"]) == (201, None)


def test_token_creation_refusals(open_client: OpenClient, callers: Any) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        as_ = callers(client)
        admin = _admin(as_)
        body = TOKEN | {"owner_user_id": _user_id(client, admin)}
        callers_ = {
            "no credential": {},
            "an operator": as_.session(*expand(["operator"]), username="erin", auth_time=datetime.now(UTC)),
            "a sign-in 16 minutes old": _admin(as_, "carol", timedelta(minutes=16)),
            "a sign-in without auth_time": as_.session(*ADMIN, username="dave"),
        }
        refused = {name: client.post("/v1/auth/tokens", json=body, headers=h) for name, h in callers_.items()}
        invalid = [
            client.post("/v1/auth/tokens", json=body | change, headers=admin)
            for change in (
                {"role": "admin"},
                {"role": "rule_approver"},
                {"days": 0},
                {"days": 91},
                {"name": "CI Bot"},
                {"owner": "team\x1b[31m"},
                {"owner": ""},
            )
        ]
        missing = client.post("/v1/auth/tokens", json={"name": "ci-bot"}, headers=admin)
        listed = client.get("/v1/auth/tokens", headers=admin).json()["items"]
    assert {name: (r.status_code, r.json()["detail"]) for name, r in refused.items()} == {
        "no credential": (401, "not signed in"),
        "an operator": (403, "needs the admin role"),
        "a sign-in 16 minutes old": (403, "reauth_required"),
        "a sign-in without auth_time": (403, "reauth_required"),
    }
    role = "role must be one of operator, spec_author, viewer"
    owner = "owner must be 1 to 200 characters without control characters"
    assert [(r.status_code, r.json()["detail"]) for r in invalid] == [
        (422, role),
        (422, role),
        (422, "days must be from 1 to 90"),
        (422, "days must be from 1 to 90"),
        (422, "name must match ^[a-z0-9][a-z0-9._-]{2,63}$"),
        (422, owner),
        (422, owner),
    ]
    assert (missing.status_code, missing.json()["detail"], listed) == (422, "invalid request", [])


def test_a_token_is_revoked_once(open_client: OpenClient, callers: Any, audit_rows: Any) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        admin = _admin(callers(client))
        body = TOKEN | {"owner_user_id": _user_id(client, admin)}
        created = client.post("/v1/auth/tokens", json=body, headers=admin).json()
        token_id = created["token_id"]
        first = client.delete(f"/v1/auth/tokens/{token_id}", headers=admin)
        again = client.delete(f"/v1/auth/tokens/{token_id}", headers=admin)
        unknown = client.delete("/v1/auth/tokens/T-0000000000000000", headers=admin)
        used = client.get("/v1/runs", headers=_bearer(created["token"]))
        [row] = [row for row in audit_rows(client) if row["action"] == "token.revoke"]
    assert (first.status_code, set(first.json())) == (200, {"token_id", "revoked_at"})
    assert [(r.status_code, r.json()["detail"]) for r in (again, unknown, used)] == [
        (409, f"token {token_id} is already revoked"),
        (404, "token T-0000000000000000 not found"),
        (401, "invalid or expired API token"),
    ]
    assert (row["object"], row["detail"]) == (token_id, {})


async def _tokens_of(ctx: Any, user_id: str) -> list[str]:
    """Two live tokens of the user: one it created, one it owns."""
    maker = Principal(f"user:{user_id}", "bob", frozenset(ADMIN))
    async with ctx.pool.connection() as conn:
        made = await tokens.create(conn, maker, name="bob-made", owner="bob", role="viewer", max_days=90)
        owned = await tokens.create(
            conn, TESTER, name="bob-owned", owner="ops", role="viewer", max_days=90, owner_user_id=user_id
        )
    return [made[1], owned[1]]


def test_disable_ends_sessions_lists_the_tokens_and_revokes_them_on_request(
    open_client: OpenClient, callers: Any, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        as_ = callers(client)
        admin, bob = _admin(as_), as_.session(*expand(["operator"]), username="bob")
        admin_id, bob_id = _user_id(client, admin), _user_id(client, bob)
        made = client.portal.call(_tokens_of, client.app.state.ctx, bob_id)
        path = f"/v1/auth/users/{bob_id}"
        first = client.post(f"{path}/disable", headers=admin)
        bob_after = client.get("/v1/auth/me", headers=bob)
        kept = [client.get("/v1/runs", headers=_bearer(token)).status_code for token in made]
        again = client.post(f"{path}/disable", headers=admin)
        enabled = client.post(f"{path}/enable", headers=admin)
        enabled_again = client.post(f"{path}/enable", headers=admin)
        second = client.post(f"{path}/disable", json={"revoke_tokens": True}, headers=admin)
        client.post(f"{path}/enable", headers=admin)  # enabling never restores a token
        ended = [client.get("/v1/runs", headers=_bearer(token)).status_code for token in made]
        itself = client.post(f"/v1/auth/users/{admin_id}/disable", headers=admin)
        unknown = client.post("/v1/auth/users/U-0000000000000000/disable", headers=admin)
        users = client.get("/v1/auth/users", headers=admin).json()["items"]
        rows = [
            (r["action"], r["object"], r["detail"])
            for r in audit_rows(client)
            if r["action"].startswith("user.")
        ]
    ids = sorted(str(tokens.token_id(token)) for token in made)
    names = dict(zip([tokens.token_id(token) for token in made], ("bob-made", "bob-owned"), strict=True))
    assert (first.status_code, first.json()["sessions_revoked"], first.json()["tokens_revoked"]) == (
        200,
        1,
        [],
    )
    assert first.json()["user_id"] == bob_id and first.json()["disabled_at"] is not None
    assert sorted(
        (t["token_id"], t["name"], t["role"], t["state"]) for t in first.json()["tokens"]
    ) == sorted((token_id, name, "viewer", "active") for token_id, name in names.items())
    assert (bob_after.status_code, kept, ended) == (401, [200, 200], [401, 401])
    assert [(r.status_code, r.json()["detail"]) for r in (again, enabled_again, itself, unknown)] == [
        (409, f"user {bob_id} is already disabled"),
        (409, f"user {bob_id} is not disabled"),
        (409, "you cannot disable yourself"),
        (404, "user U-0000000000000000 not found"),
    ]
    assert enabled.json() == {"user_id": bob_id, "disabled_at": None}
    assert (second.json()["tokens_revoked"], {t["state"] for t in second.json()["tokens"]}) == (
        ids,
        {"revoked"},
    )
    assert rows == [
        ("user.disable", bob_id, {"sessions_revoked": 1, "tokens_revoked": []}),
        ("user.enable", bob_id, {}),
        ("user.disable", bob_id, {"sessions_revoked": 0, "tokens_revoked": ids}),
        ("user.enable", bob_id, {}),
    ]
    assert {(u["username"], u["disabled_at"] is None) for u in users} == {("alice", True), ("bob", True)}
    assert sorted(next(u for u in users if u["username"] == "bob")["roles"]) == ["operator", "viewer"]


def test_sessions_are_listed_and_revoked(open_client: OpenClient, callers: Any, audit_rows: Any) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        as_ = callers(client)
        admin, bob = _admin(as_), as_.session("viewer", username="bob")
        listed = client.get("/v1/auth/sessions", headers=admin).json()["items"]
        [session_id] = [s["session_id"] for s in listed if s["name"] == "bob"]
        revoked = client.delete(f"/v1/auth/sessions/{session_id}", headers=admin)
        again = client.delete(f"/v1/auth/sessions/{session_id}", headers=admin)
        bob_after = client.get("/v1/auth/me", headers=bob)
        remaining = client.get("/v1/auth/sessions", headers=admin).json()["items"]
        [row] = [row for row in audit_rows(client) if row["action"] == "session.revoke"]
    assert {s["name"] for s in listed} == {"alice", "bob"} and "secret_hash" not in listed[0]
    assert revoked.json() == {"session_id": session_id, "count": 1}
    assert (again.status_code, again.json()["detail"]) == (404, f"session {session_id} not found")
    assert (bob_after.status_code, [s["name"] for s in remaining]) == (401, ["alice"])
    assert (row["object"], row["detail"]) == (session_id, {"count": 1})
