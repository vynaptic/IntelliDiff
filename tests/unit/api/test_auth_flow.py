"""Sign-in (docs/mvp/AUTH.md §2, §3, §5): the OIDC code flow through the fake IdP and its cookies, the
refusals before and after the identity is known, sessions and how they end, CSRF, sign-out, token
sign-in in tokens mode, no secret in the tables or the log, and the plain cookies of a plain-http
loopback ``public_url`` (§3, exception E1), which the settings alone give."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import psycopg
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from deltasleuth.api import create_app
from deltasleuth.auth import sessions
from deltasleuth.auth.oidc import LOGIN_COOKIE, Login
from deltasleuth.auth.principal import ROLES
from deltasleuth.config import load_settings
from deltasleuth.context import init_workspace
from tests.conftest import encryption
from tests.unit.auth.fake_idp import CLIENT_SECRET, FakeIdp, sign_in

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
COOKIE = sessions.COOKIE
HOME = "https://ds.test/#/runs"


def _set_cookies(response: httpx.Response) -> dict[str, str]:
    return {header.split("=", 1)[0]: header for header in response.headers.get_list("set-cookie")}


def _flags(header: str) -> set[str]:
    return {part.strip().lower() for part in header.split(";")[1:] if not part.strip().startswith("expires=")}


async def _query(ctx: Any, sql: str) -> list[tuple[Any, ...]]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(sql.encode())
        return await cur.fetchall() if cur.description else []


def sql(client: TestClient, statement: str) -> list[tuple[Any, ...]]:
    return client.portal.call(_query, client.app.state.ctx, statement)


def _written(client: TestClient) -> tuple[Any, ...]:
    """How many users, sessions and audit rows (the system's own aside) the database holds."""
    [counts] = sql(
        client,
        "SELECT (SELECT count(*) FROM ds.users), (SELECT count(*) FROM ds.sessions),"
        " (SELECT count(*) FROM ds.audit_log WHERE actor NOT LIKE 'system:%')",
    )
    return counts


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/v1/auth/me").json()["csrf"]}


def _as(value: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={value}"}


def _use(client: TestClient, value: str) -> None:
    """Put a session cookie in the client's jar, as the browser that holds it."""
    client.cookies.delete(COOKIE)
    client.cookies.set(COOKIE, value, domain="ds.test")


def _start(client: TestClient, **params: Any) -> tuple[str, dict[str, str]]:
    started = client.get("/v1/auth/login", params=params, follow_redirects=False)
    assert started.status_code == 303, started.text
    location = started.headers["location"]
    return location, dict(parse_qsl(urlsplit(location).query))


def _denied(response: httpx.Response, code: str) -> bool:
    return response.headers["location"] == (
        f"https://ds.test/#/signin?error={code}&request_id={response.headers['x-request-id']}"
    )


def test_sign_in_goes_to_the_idp_and_back_with_a_strict_session_cookie(
    open_client: OpenClient, idp: FakeIdp, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": idp.settings(username_claim="preferred_username")}) as client:
        started = client.get("/v1/auth/login", params={"next": "#/runs/R-1"}, follow_redirects=False)
        location = started.headers["location"]
        asked = dict(parse_qsl(urlsplit(location).query))
        code = idp.authorize(location, groups=["G-op"])
        query = {"code": code, "state": asked["state"], "iss": idp.issuer}
        back = client.get("/v1/auth/callback", params=query, follow_redirects=False)
        me = client.get("/v1/auth/me").json()
        [login] = [row for row in audit_rows(client) if row["action"] == "auth.login"]
        cookie = client.cookies[COOKIE]
        by_host = client.get("/v1/auth/login", headers={"Host": "localhost:8000"}, follow_redirects=False)
    assert (started.status_code, location.split("?")[0]) == (303, f"{idp.issuer}/authorize")
    assert set(asked) == {
        "response_type", "client_id", "redirect_uri", "scope", "state", "nonce", "code_challenge",
        "code_challenge_method",
    }  # fmt: skip
    assert (asked["redirect_uri"], asked["scope"]) == ("https://ds.test/v1/auth/callback", "openid profile")
    login_cookie = _set_cookies(started)[LOGIN_COOKIE]
    assert login_cookie.startswith(f"{LOGIN_COOKIE}=v1.")
    assert _flags(login_cookie) == {"httponly", "max-age=600", "path=/", "samesite=lax", "secure"}
    assert (back.status_code, back.headers["location"]) == (303, "https://ds.test/#/runs/R-1")
    assert _flags(_set_cookies(back)[COOKIE]) == {"httponly", "path=/", "samesite=strict", "secure"}
    assert "max-age=0" in _flags(_set_cookies(back)[LOGIN_COOKIE])
    assert me == {
        "actor": login["actor"],
        "name": "alice",
        "display_name": "Alice Example",
        "kind": "user",
        "roles": ["viewer", "operator"],
        "csrf": sessions.csrf_of(cookie),
        "mode": "oidc",
        "idle_minutes": 30,
        "expires_at": me["expires_at"],
    }
    assert (login["actor"][:7], login["actor_name"], login["object"], login["detail"]) == (
        "user:U-",
        "alice",
        login["session_id"],
        {"method": "oidc", "roles": ["viewer", "operator"], "reauth": False},
    )
    assert (str(login["client_addr"]), login["request_id"]) == ("10.1.2.3", back.headers["x-request-id"])
    assert dict(parse_qsl(urlsplit(by_host.headers["location"]).query))["redirect_uri"] == (
        "https://ds.test/v1/auth/callback"  # never Host
    )


@pytest.mark.parametrize(
    "headers",
    [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Mode": "no-cors"}, {"Sec-Fetch-Site": "same-site"}],
)
def test_a_cross_site_sign_in_sets_no_cookie_and_writes_nothing(
    open_client: OpenClient, idp: FakeIdp, headers: dict[str, str]
) -> None:
    with open_client(auth={"oidc": idp.settings()}, tier=None) as client:  # sign-in needs no licence
        before = _written(client)
        refused = client.get("/v1/auth/login", headers=headers, follow_redirects=False)
        after = _written(client)
        navigation = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}
        typed = client.get("/v1/auth/login", headers=navigation, follow_redirects=False)
    assert (refused.status_code, refused.json()["detail"]) == (403, "cross-site sign-in refused")
    assert "set-cookie" not in refused.headers and before == after == (0, 0, 0)
    assert typed.status_code == 303


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post", "private_key_jwt"])
def test_each_client_authentication_method_signs_in(
    open_client: OpenClient, idp: FakeIdp, tmp_path: Any, method: str
) -> None:
    fields: dict[str, Any] = {"client_auth": method}
    if method == "private_key_jwt":
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        (tmp_path / "oidc_client.key").write_bytes(pem)
        fields |= {"client_secret_ref": None, "client_key_ref": "file:oidc_client.key"}
        idp.client_public_key = key.public_key()
    idp.client_auth = method
    with open_client(auth={"oidc": idp.settings(**fields)}) as client:
        back = sign_in(client, idp)
    [token] = [request for request in idp.requests if request.path == "/token"]
    sent = {
        "client_secret_basic": token.headers.get("Authorization", "").startswith("Basic "),
        "client_secret_post": token.form.get("client_secret") == CLIENT_SECRET,
        "private_key_jwt": "client_assertion" in token.form and len(idp.assertions) == 1,
    }
    assert back.headers["location"] == HOME and sent[method]


def test_signing_in_again_keeps_the_user_and_replaces_only_the_session_shown_at_login(
    open_client: OpenClient, idp: FakeIdp
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        first = client.cookies[COOKIE]
        client.cookies.clear()
        sign_in(client, idp)  # another browser: nothing to replace
        second = client.cookies[COOKIE]
        _use(client, first)
        location, asked = _start(client)  # presents the first session
        _use(client, second)  # the callback presents the second one only
        query = {"code": idp.authorize(location, groups=["G-admin"]), "state": asked["state"]}
        back = client.get("/v1/auth/callback", params=query, follow_redirects=False)
        third = client.cookies[COOKIE]
        client.cookies.clear()
        replies = [client.get("/v1/auth/me", headers=_as(cookie)) for cookie in (first, second, third)]
        users = sql(client, "SELECT count(*) FROM ds.users")
    assert back.headers["location"] == HOME and users == [(1,)]
    assert [reply.status_code for reply in replies] == [401, 200, 200]
    assert replies[1].json()["actor"] == replies[2].json()["actor"]


def test_reauth_makes_the_idp_authenticate_again(
    open_client: OpenClient, idp: FakeIdp, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        again = sign_in(client, idp, reauth=True)
        stale = sign_in(client, idp, reauth=True, auth_time=int(time.time()) - 3600)
        logins = [row["detail"]["reauth"] for row in audit_rows(client) if row["action"] == "auth.login"]
    assert [(asked.get("prompt"), asked.get("max_age")) for asked in idp.authorized] == [
        (None, None),
        ("login", "0"),
        ("login", "0"),
    ]
    assert again.headers["location"] == HOME and logins == [False, True] and _denied(stale, "login_failed")


def test_the_identity_can_be_the_oid_claim(open_client: OpenClient, idp: FakeIdp) -> None:
    settings = idp.settings(subject_claim="oid", username_claim="preferred_username")
    with open_client(auth={"oidc": settings}) as client:
        sign_in(client, idp, oid="3f2a-oid", sub="pairwise-1")
        sign_in(client, idp, oid="3f2a-oid", sub="pairwise-2")  # a re-registered app sees another sub
        users = sql(client, "SELECT subject, username FROM ds.users")
    assert users == [("3f2a-oid", "alice")]


def _no_cookie(client: TestClient, idp: FakeIdp, location: str, query: dict[str, str]) -> dict[str, str]:
    client.cookies.delete(LOGIN_COOKIE)
    return query


def _expired(client: TestClient, idp: FakeIdp, location: str, query: dict[str, str]) -> dict[str, str]:
    key = client.app.state.ctx.oidc.login_key
    login = Login.unseal(client.cookies[LOGIN_COOKIE], key)
    assert login is not None
    client.cookies.delete(LOGIN_COOKIE)
    client.cookies.set(LOGIN_COOKIE, replace(login, exp=int(time.time()) - 1).seal(key), domain="ds.test")
    return query


def _down(client: TestClient, idp: FakeIdp, location: str, query: dict[str, str]) -> dict[str, str]:
    idp.down = True
    return query


def _refused_exchange(
    client: TestClient, idp: FakeIdp, location: str, query: dict[str, str]
) -> dict[str, str]:
    idp.client_secret = "another"  # noqa: S105 - the IdP expects another secret
    return query


FAILURES: dict[str, tuple[str, Callable[..., dict[str, str]]]] = {
    "no login cookie": ("login_expired", _no_cookie),
    "an expired login cookie": ("login_expired", _expired),
    "another state": ("login_expired", lambda c, i, at, q: q | {"state": "forged"}),
    "no state": ("login_expired", lambda c, i, at, q: {"code": q["code"]}),
    "an IdP refusal": ("access_denied", lambda c, i, at, q: {"state": q["state"], "error": "access_denied"}),
    "an IdP error": (
        "idp_error",
        lambda c, i, at, q: {
            "state": q["state"],
            "error": "server_error",
            "error_description": "<b>IdP text</b>",
        },
    ),
    "another iss": ("login_failed", lambda c, i, at, q: q | {"iss": "https://evil.example"}),
    "no code": ("login_failed", lambda c, i, at, q: {"state": q["state"]}),
    "a token for another client": (
        "login_failed",
        lambda c, i, at, q: q | {"code": i.authorize(at, aud="other")},
    ),
    "a replayed code": ("login_failed", lambda c, i, at, q: q | {"code": "used-or-unknown"}),
    "a refused exchange": ("login_failed", _refused_exchange),
    "an unreachable IdP": ("idp_unavailable", _down),
}


@pytest.mark.parametrize("failure", FAILURES)
def test_a_failure_before_the_identity_is_known_writes_nothing_and_names_the_request(
    open_client: OpenClient, idp: FakeIdp, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    code, arrange = FAILURES[failure]
    with open_client(auth={"oidc": idp.settings()}, tier=None) as client:  # sign-in needs no licence
        location, asked = _start(client)
        query = {"code": idp.authorize(location), "state": asked["state"], "iss": idp.issuer}
        query = arrange(client, idp, location, query)
        back = client.get("/v1/auth/callback", params=query, follow_redirects=False)
        written = _written(client)
    request_id = back.headers["x-request-id"]
    assert back.status_code == 303 and _denied(back, code) and written == (0, 0, 0)
    line = f"auth.login_failed reason={code} ip=10.1.2.3 request_id={request_id}: "
    assert [m for m in caplog.messages if m.startswith(line)] and "IdP text" not in caplog.text


def test_each_denial_is_audited_by_its_user(
    open_client: OpenClient, idp: FakeIdp, audit_rows: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        no_role = sign_in(client, idp, groups=["G-unknown"], sub="nobody")
        overage = sign_in(client, idp, groups=(), sub="many", _claim_names={"groups": "src1"})
        assert sign_in(client, idp, sub="leaver").headers["location"] == HOME
        sql(client, "UPDATE ds.users SET disabled_at = now(), disabled_by = 'local:tester@test'")
        disabled = sign_in(client, idp, sub="leaver")
        rows = [row for row in audit_rows(client) if row["action"] == "auth.login_denied"]
        users = dict(sql(client, "SELECT subject, user_id FROM ds.users"))
    assert (
        _denied(no_role, "no_role") and _denied(overage, "groups_overage") and _denied(disabled, "disabled")
    )
    assert [(row["actor"], row["actor_name"], row["object"], row["detail"]) for row in rows] == [
        (f"user:{users[sub]}", sub, users[sub], {"reason": reason, "count": 1})
        for sub, reason in (("nobody", "no_role"), ("many", "groups_overage"), ("leaver", "disabled"))
    ]
    [refused, many] = [m for m in caplog.messages if m.startswith("auth.login_denied reason=")][:2]
    assert "'groups'" in refused and "groups=1" in refused and "G-unknown" not in refused
    assert "'_claim_names'" in many and "groups=0" in many and "src1" not in many


def test_a_roles_change_is_audited_at_the_next_sign_in(
    open_client: OpenClient, idp: FakeIdp, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp, groups=["G-view"])
        sign_in(client, idp, groups=["G-op"])
        overage = sign_in(client, idp, groups=(), _claim_names={"groups": "src1"})
        rows = [
            (row["action"], row["detail"]) for row in audit_rows(client) if row["action"].startswith("auth.")
        ]
        roles = sql(client, "SELECT roles FROM ds.users")
    assert rows == [
        ("auth.login", {"method": "oidc", "roles": ["viewer"], "reauth": False}),
        ("auth.roles_changed", {"from": ["viewer"], "to": ["viewer", "operator"]}),
        ("auth.login", {"method": "oidc", "roles": ["viewer", "operator"], "reauth": False}),
        ("auth.login_denied", {"reason": "groups_overage", "count": 1}),
    ]
    assert _denied(overage, "groups_overage") and roles == [(["operator", "viewer"],)]  # kept


@pytest.mark.parametrize("reason", ["no_role", "disabled"])
def test_a_refused_user_s_sign_ins_write_no_new_version_and_one_denial_per_10_minutes(
    open_client: OpenClient, idp: FakeIdp, audit_rows: Any, reason: str
) -> None:
    groups = ["G-unknown"] if reason == "no_role" else ["G-view"]
    with open_client(auth={"oidc": idp.settings()}) as client:
        if reason == "disabled":
            sign_in(client, idp, groups=groups)
            sql(client, "UPDATE ds.users SET disabled_at = now(), disabled_by = 'local:tester@test'")
        sign_in(client, idp, groups=groups)
        version = sql(client, "SELECT xmin::text, last_login_at FROM ds.users")
        for _ in range(2):
            assert _denied(sign_in(client, idp, groups=groups), reason)
        unchanged = sql(client, "SELECT xmin::text, last_login_at FROM ds.users") == version
        denials = client.app.state.login_denials
        later = denials.clock() + 601
        denials.clock = lambda: later
        sign_in(client, idp, groups=groups)
        rows = [row["detail"] for row in audit_rows(client) if row["action"] == "auth.login_denied"]
    assert unchanged and rows == [{"reason": reason, "count": 1}, {"reason": reason, "count": 3}]


def test_sessions_and_tokens_keep_working_while_the_idp_is_down(
    open_client: OpenClient, idp: FakeIdp, callers: Any
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        bearer = callers(client).token("viewer")
        idp.down = True
        session = client.get("/v1/specs")
        client_token = client.get("/v1/specs", headers=bearer)
        again = sign_in(client, idp)  # discovery is cached; the code exchange finds no IdP
    with open_client(auth={"oidc": idp.settings()}) as client:  # a start while the IdP is down
        health = client.get("/v1/health")
        started = client.get("/v1/auth/login", follow_redirects=False)
    assert (session.status_code, client_token.status_code, health.status_code) == (200, 200, 200)
    assert _denied(again, "idp_unavailable") and _denied(started, "idp_unavailable")


@pytest.mark.parametrize(
    ("next_", "landed"),
    [
        ("https://evil.example", "#/runs"),
        ("//evil.example/#/runs", "#/runs"),
        ("#/runs\r\nSet-Cookie: x=1", "#/runs"),
        ("#/runs/R-1/keys/K%2B1?tab=diff&q=a+b*c'(d)!~", "#/runs/R-1/keys/K%2B1?tab=diff&q=a+b*c'(d)!~"),
    ],
)
def test_next_lands_on_a_hash_route_of_the_public_url(
    open_client: OpenClient, idp: FakeIdp, next_: str, landed: str
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        back = sign_in(client, idp, next_=next_)
    assert back.headers["location"] == f"https://ds.test/{landed}"


ENDINGS = {
    "revoked": "UPDATE ds.sessions SET revoked_at = now(), revoked_by = 'local:tester@test'",
    "past session_max_hours": "UPDATE ds.sessions SET created_at = now() - interval '9 hours',"
    " expires_at = now() - interval '1 hour'",
    "idle": "UPDATE ds.sessions SET last_seen_at = now() - interval '31 minutes'",
    "its user disabled": "UPDATE ds.users SET disabled_at = now(), disabled_by = 'local:tester@test'",
}


@pytest.mark.parametrize("ending", [*ENDINGS, "signed out"])
def test_every_session_ending_gives_session_expired(
    open_client: OpenClient, idp: FakeIdp, ending: str
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        cookie = client.cookies[COOKIE]
        if ending == "signed out":
            assert client.post("/v1/auth/logout", headers=_csrf(client)).status_code == 200
        else:
            sql(client, ENDINGS[ending])
        client.cookies.clear()
        after = client.get("/v1/auth/me", headers=_as(cookie))
    assert (after.status_code, after.json()["detail"]) == (401, "session expired")


def test_background_polls_do_not_keep_a_session_alive(open_client: OpenClient, idp: FakeIdp) -> None:
    idle = "SELECT last_seen_at < now() - interval '90 seconds' FROM ds.sessions"
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        sql(client, "UPDATE ds.sessions SET last_seen_at = now() - interval '2 minutes'")
        polled = client.get("/v1/runs", headers={"X-DS-Background": "1"})
        after_poll = sql(client, idle)
        client.get("/v1/runs")
        after_use = sql(client, idle)
        sql(client, "UPDATE ds.sessions SET last_seen_at = now() - interval '31 minutes'")
        ended = client.get("/v1/runs", headers={"X-DS-Background": "1"})
    assert polled.status_code == 200 and (after_poll, after_use) == ([(True,)], [(False,)])
    assert (ended.status_code, ended.json()["detail"]) == (401, "session expired")


def test_an_edited_role_mapping_ends_every_sso_session(open_client: OpenClient, idp: FakeIdp) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        cookie = client.cookies[COOKIE]
        assert client.get("/v1/auth/me").status_code == 200
    edited = idp.settings(roles={"viewer": ["G-view"], "admin": ["G-admin-2"]})  # an over-broad group removed
    with open_client(auth={"oidc": edited}) as client:
        after = client.get("/v1/auth/me", headers=_as(cookie))
    assert (after.status_code, after.json()["detail"]) == (401, "session expired")


def test_each_csrf_layer_and_a_bearer_skips_only_the_token(
    open_client: OpenClient, idp: FakeIdp, callers: Any, spec_yaml: str
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp, groups=["G-author"])
        csrf, bearer = _csrf(client), callers(client).token("spec_author")

        def upload(headers: dict[str, str]) -> tuple[int, str | None]:
            reply = client.post("/v1/specs", content=spec_yaml, headers=headers)
            return reply.status_code, reply.json().get("detail")

        cookie = [
            upload({}),
            upload({"X-CSRF-Token": "forged"}),
            upload(csrf | {"Origin": "https://evil.example"}),
            upload(csrf | {"Sec-Fetch-Site": "same-site"}),
            upload(csrf | {"Origin": "https://ds.test", "Sec-Fetch-Site": "same-origin"}),
        ]
        client.cookies.clear()
        by_bearer = [
            upload(bearer),
            upload(bearer | {"Origin": "https://evil.example"}),
            upload(bearer | {"Sec-Fetch-Site": "cross-site"}),
        ]
    csrf_refused = (403, "missing or invalid CSRF token")
    assert cookie[:4] == [
        csrf_refused,
        csrf_refused,
        (403, "cross-origin request refused"),
        (403, "cross-site request refused"),
    ]
    assert cookie[4][0] == 201 and by_bearer[0][0] == 201
    assert by_bearer[1:] == [(403, "cross-origin request refused"), (403, "cross-site request refused")]


def test_sign_out_ends_the_session_and_points_at_the_idp(
    open_client: OpenClient, idp: FakeIdp, callers: Any, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        cookie, csrf, bearer = client.cookies[COOKIE], _csrf(client), callers(client).token("viewer")
        unsigned = client.post("/v1/auth/logout", headers=_as(cookie))
        out = client.post("/v1/auth/logout", headers=csrf)
        after = client.get("/v1/auth/me", headers=_as(cookie))
        by_bearer = client.post("/v1/auth/logout", headers=bearer)
        [row] = [row for row in audit_rows(client) if row["action"] == "auth.logout"]
    assert (unsigned.status_code, unsigned.json()["detail"]) == (403, "missing or invalid CSRF token")
    assert out.json() == {
        "logout_url": f"{idp.issuer}/logout?client_id=deltasleuth"
        "&post_logout_redirect_uri=https%3A%2F%2Fds.test%2F"
    }
    assert out.headers["clear-site-data"] == '"storage"' and "max-age=0" in _flags(_set_cookies(out)[COOKIE])
    assert (after.status_code, by_bearer.status_code) == (401, 400)
    assert by_bearer.json()["detail"] == "only a browser session signs out"
    assert row["actor"].startswith("user:U-") and row["object"] == row["session_id"] is not None


def test_sign_out_without_an_idp_link(open_client: OpenClient, idp: FakeIdp) -> None:
    with open_client(auth={"oidc": idp.settings(end_session=False)}) as client:
        sign_in(client, idp)
        out = client.post("/v1/auth/logout", headers=_csrf(client))
    with open_client() as client:  # developer mode has no session to end
        dev = client.post("/v1/auth/logout")
    assert out.json() == {"logout_url": None}
    assert (dev.status_code, dev.json()["detail"]) == (400, "only a browser session signs out")


def test_tokens_mode_signs_a_browser_in_as_the_token(
    open_client: OpenClient, callers: Any, audit_rows: Any, spec_yaml: str
) -> None:
    with open_client(auth={}) as client:
        token = callers(client).token("spec_author")["Authorization"].removeprefix("Bearer ")
        wrong = client.post("/v1/auth/token-login", json={"token": "dst_" + "0" * 16 + "_" + "0" * 64})
        signed = client.post("/v1/auth/token-login", json={"token": token})
        me = client.get("/v1/auth/me").json()
        upload = client.post("/v1/specs", content=spec_yaml, headers={"X-CSRF-Token": me["csrf"]})
        [login] = [row for row in audit_rows(client) if row["action"] == "auth.login"]
        cookie = client.cookies[COOKIE]
        sql(client, "UPDATE ds.api_tokens SET revoked_at = now(), revoked_by = 'local:tester@test'")
        client.cookies.clear()
        revoked = client.get("/v1/auth/me", headers=_as(cookie))
        sso = client.get("/v1/auth/login", follow_redirects=False)
    assert (wrong.status_code, wrong.json()["detail"], wrong.headers["www-authenticate"]) == (
        401,
        "invalid or expired API token",
        "Bearer",
    )
    assert signed.json() == me and _flags(_set_cookies(signed)[COOKIE]) == {
        "httponly", "path=/", "samesite=strict", "secure"
    }  # fmt: skip
    assert (me["actor"][:8], me["name"], me["kind"], me["display_name"], me["mode"]) == (
        "token:T-",
        "ci-bot",
        "token",
        None,
        "tokens",
    )
    assert me["roles"] == ["viewer", "operator", "spec_author"] and upload.status_code == 201
    assert (login["actor"], login["object"], login["detail"]) == (
        me["actor"],
        login["session_id"],
        {"method": "token", "roles": ["viewer", "operator", "spec_author"], "reauth": False},
    )
    assert (revoked.status_code, revoked.json()["detail"]) == (401, "session expired")
    assert (sso.status_code, sso.json()["detail"]) == (404, "SSO is not configured")


@pytest.mark.parametrize("mode", ["none", "oidc"])
def test_token_sign_in_is_refused_under_sso_and_in_developer_mode(
    open_client: OpenClient, idp: FakeIdp, mode: str
) -> None:
    with open_client(auth={"oidc": idp.settings()} if mode == "oidc" else None) as client:
        refused = client.post("/v1/auth/token-login", json={"token": "anything"})
        sso = client.get("/v1/auth/callback", follow_redirects=False)
    assert (refused.status_code, refused.json()["detail"]) == (404, "token sign-in is not enabled")
    assert sso.status_code == (303 if mode == "oidc" else 404)


def test_me_names_every_kind_of_caller(open_client: OpenClient, callers: Any) -> None:
    with open_client(auth={}) as client:
        bearer = client.get("/v1/auth/me", headers=callers(client).token("operator")).json()
    with open_client() as client:
        dev = client.get("/v1/auth/me").json()
    assert (bearer["kind"], bearer["csrf"], bearer["roles"], bearer["mode"], bearer["idle_minutes"]) == (
        "token",
        None,
        ["viewer", "operator"],
        "tokens",
        30,
    )
    assert bearer["expires_at"] is not None
    assert dev == {
        "actor": "dev:loopback",
        "name": dev["name"],
        "display_name": None,
        "kind": "dev",
        "roles": list(ROLES),
        "csrf": "dev",
        "mode": "none",
        "idle_minutes": None,
        "expires_at": None,
    }


def test_identity_headers_are_not_credentials(open_client: OpenClient, idp: FakeIdp) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        replies = [
            client.get("/v1/auth/me", headers={name: "admin"})
            for name in ("X-Forwarded-User", "SM_USER", "Remote-User")
        ]
    assert {(reply.status_code, reply.json()["detail"]) for reply in replies} == {(401, "not signed in")}


async def _dump(ctx: Any) -> str:
    """Every row of every ds table, as text."""
    async with ctx.pool.connection() as conn:
        cur = await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'ds'")
        text = []
        for (table,) in await cur.fetchall():
            rows = await conn.execute(f'SELECT row_to_json(t)::text FROM ds."{table}" t'.encode())  # noqa: S608
            text += [row for (row,) in await rows.fetchall()]
    return "\n".join(text)


def test_no_secret_cookie_or_token_reaches_the_tables_or_the_log(
    open_client: OpenClient, idp: FakeIdp, callers: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, "deltasleuth")  # the server's loggers; the test client logs its URLs
    with open_client(auth={"oidc": idp.settings()}) as client:
        started = client.get("/v1/auth/login", follow_redirects=False)
        location = started.headers["location"]
        asked = dict(parse_qsl(urlsplit(location).query))
        login = Login.unseal(client.cookies[LOGIN_COOKIE], client.app.state.ctx.oidc.login_key)
        code = idp.authorize(location, groups=["G-view"])
        client.get(
            "/v1/auth/callback", params={"code": code, "state": asked["state"]}, follow_redirects=False
        )
        bearer = callers(client).token("viewer")
        client.get("/v1/specs", headers=bearer)
        forged = "dst_" + "1" * 16 + "_" + "2" * 64
        client.get("/v1/specs", headers={"Authorization": f"Bearer {forged}"})
        idp.client_secret = "another"  # noqa: S105 - a failed exchange's log line too
        sign_in(client, idp)
        dumped = client.portal.call(_dump, client.app.state.ctx)
    assert login is not None
    issued = idp.issued[0]
    secret = [
        CLIENT_SECRET, code, asked["state"], asked["nonce"], login.verifier, client.cookies.get(COOKIE) or "",
        _set_cookies(started)[LOGIN_COOKIE].split(";")[0].split("=", 1)[1], issued["access_token"],
        issued["refresh_token"], issued["id_token"], bearer["Authorization"].removeprefix("Bearer "), forged,
    ]  # fmt: skip
    assert all(secret)
    assert [value for value in secret if value in dumped or value in caplog.text] == []
    assert "IdP text" not in caplog.text


def test_a_refused_sign_in_ends_the_session_this_browser_had(open_client: OpenClient, idp: FakeIdp) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp, groups=["G-admin"])
        cookie = client.cookies[COOKIE]
        refused = sign_in(client, idp, groups=["G-unknown"])  # the same person, every role removed
        client.cookies.clear()
        after = client.get("/v1/auth/me", headers=_as(cookie))
    assert _denied(refused, "no_role") and "max-age=0" in _flags(_set_cookies(refused)[COOKIE])
    assert (after.status_code, after.json()["detail"]) == (401, "session expired")


def test_an_idle_browser_still_signs_out_of_the_idp(
    open_client: OpenClient, idp: FakeIdp, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        csrf = _csrf(client)
        sql(client, ENDINGS["idle"])
        out = client.post("/v1/auth/logout", headers=csrf)
        rows = [row for row in audit_rows(client) if row["action"] == "auth.logout"]
    assert out.status_code == 200 and out.json()["logout_url"].startswith(f"{idp.issuer}/logout?")
    assert "max-age=0" in _flags(_set_cookies(out)[COOKIE]) and rows == []  # nothing live to revoke


def test_sign_out_uses_the_last_checked_document_when_discovery_is_down(
    open_client: OpenClient, idp: FakeIdp
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        client.app.state.ctx.oidc._discovery = (0.0, client.app.state.ctx.oidc._discovery[1])  # stale
        idp.down = True
        out = client.post("/v1/auth/logout", headers=_csrf(client))
    assert out.json()["logout_url"].startswith(f"{idp.issuer}/logout?")


def test_a_malformed_discovery_list_is_an_unavailable_idp_not_an_error(
    open_client: OpenClient, idp: FakeIdp
) -> None:
    idp.discovery["code_challenge_methods_supported"] = [["S256"]]  # not strings
    with open_client(auth={"oidc": idp.settings()}) as client:  # the server still starts
        started = client.get("/v1/auth/login", follow_redirects=False)
    assert _denied(started, "idp_unavailable")


def test_signing_out_twice_writes_one_row_and_an_ended_session_cannot_come_back(
    open_client: OpenClient, idp: FakeIdp, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": idp.settings()}) as client:
        sign_in(client, idp)
        cookie, csrf = client.cookies[COOKIE], _csrf(client)
        first = client.post("/v1/auth/logout", headers=csrf | _as(cookie))
        second = client.post("/v1/auth/logout", headers=csrf | _as(cookie))  # a double click
        rows = [row for row in audit_rows(client) if row["action"] == "auth.logout"]
        sign_in(client, idp)
        other, csrf = client.cookies[COOKIE], _csrf(client)
        hexes = "'0123456789abcdef', 'fedcba9876543210'"
        flip = f"UPDATE ds.sessions SET roles_sha256 = translate(roles_sha256, {hexes})"  # noqa: S608
        sql(client, flip + " WHERE revoked_at IS NULL")  # granted under another role mapping
        ended = client.post("/v1/auth/logout", headers=csrf | _as(other))
        sql(client, flip)  # the mapping restored
        client.cookies.clear()
        revived = client.get("/v1/auth/me", headers=_as(other))
    assert (first.status_code, second.status_code, ended.status_code) == (200, 200, 200) and len(rows) == 1
    assert revived.status_code == 401


# -- a plain-http loopback public_url (docs/mvp/AUTH.md §3, exception E1) -----------------------------

TRIAL = "http://localhost:8000"
PLAIN = "ds_session_8000"


def _token_sign_in(client: TestClient, callers: Any, name: str = "first-run") -> httpx.Response:
    token = callers(client).token("spec_author", name)["Authorization"].removeprefix("Bearer ")
    return client.post("/v1/auth/token-login", json={"token": token})


def _named(name: str, value: str) -> dict[str, str]:
    return {"Cookie": f"{name}={value}"}


def test_an_http_loopback_public_url_sets_cookies_a_strict_jar_returns(
    open_client: OpenClient, callers: Any, spec_yaml: str
) -> None:
    """The client's own jar returns no ``Secure`` cookie over http, as a browser that does not take
    loopback for secure: with the https cookies nobody would be signed in here."""
    with open_client(auth={"public_url": TRIAL}) as client:
        signed = _token_sign_in(client, callers)
        secret = client.cookies[PLAIN]
        me = client.get("/v1/auth/me")
        upload = client.post("/v1/specs", content=spec_yaml, headers={"X-CSRF-Token": me.json()["csrf"]})
        others = [
            client.get("/v1/auth/me", headers=_named(name, secret))
            for name in (COOKIE, "ds_session", "ds_session_18000", "ds_session_80", "__Host-ds_session_8000")
        ]
        out = client.post("/v1/auth/logout", headers={"X-CSRF-Token": me.json()["csrf"]})
        after = client.get("/v1/auth/me")
    assert signed.headers.get_list("set-cookie") == [f"{PLAIN}={secret}; HttpOnly; Path=/; SameSite=strict"]
    assert (signed.status_code, me.status_code, upload.status_code) == (200, 200, 201)
    assert me.json() == signed.json() and me.json()["csrf"] == sessions.csrf_of(secret)
    assert {(r.status_code, r.json()["detail"]) for r in others} == {(401, "not signed in")}
    assert _flags(_set_cookies(out)[PLAIN]) == {"httponly", "max-age=0", "path=/", "samesite=strict"}
    assert (after.status_code, after.json()["detail"]) == (401, "not signed in")
    assert "strict-transport-security" not in signed.headers


@pytest.fixture
def second_dsn(pg_server: str) -> Iterator[str]:
    """Another empty database: a second installation's."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield pg_server.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _installation(folder: Path, dsn: str, public_url: str) -> TestClient:
    """A tokens-mode installation of its own folder, keys and database, at ``public_url``."""
    folder.mkdir()
    (folder / "dsn").write_text(dsn + "\n")
    (folder / "targets.yaml").write_text('targets:\n  svc-a: {base_url: "https://a.example.com"}\n')
    doc = {
        "database_dsn_ref": f"file:{folder / 'dsn'}",
        "evidence_dir": "evidence",
        "workspace_dir": "workspace",
        "targets_file": "targets.yaml",
        "auth": {"public_url": public_url},
        "encryption": encryption(folder / "secrets"),
    }
    (folder / "deltasleuth.yaml").write_text(yaml.safe_dump(doc))
    settings = load_settings(folder / "deltasleuth.yaml")
    init_workspace(settings)
    return TestClient(create_app(settings), base_url=public_url, client=("10.1.2.3", 50000))


def test_two_loopback_installations_share_a_jar_and_keep_their_sessions(
    open_client: OpenClient, callers: Any, tmp_path: Path, second_dsn: str
) -> None:
    """A browser keeps cookies by host, not by port: under one name the second sign-in would replace the
    first installation's session, silently. Each server is sent both cookies and reads its own."""
    other = "http://localhost:18000"
    with (
        open_client(auth={"public_url": TRIAL}) as first,
        _installation(tmp_path / "second", second_dsn, other) as second,
    ):
        second.cookies.jar = first.cookies.jar  # one browser
        signed = [_token_sign_in(first, callers, "first-run"), _token_sign_in(second, callers, "second-run")]
        held = sorted(cookie.name for cookie in first.cookies.jar)
        both = [client.get("/v1/auth/me") for client in (first, second)]
        sent = both[1].request.headers["cookie"]
        out = second.post("/v1/auth/logout", headers={"X-CSRF-Token": both[1].json()["csrf"]})
        after = [client.get("/v1/auth/me") for client in (first, second)]
    assert [r.status_code for r in signed] == [200, 200] and held == ["ds_session_18000", PLAIN]
    assert [(r.status_code, r.json()["name"]) for r in both] == [(200, "first-run"), (200, "second-run")]
    assert "ds_session_18000=" in sent and f"{PLAIN}=" in sent  # the other installation's secret, unread
    assert (out.status_code, [r.status_code for r in after]) == (200, [200, 401])
    assert after[0].json()["name"] == "first-run"


@pytest.mark.parametrize("public_url", ["https://ds.test", "https://localhost:8443"])
def test_an_https_public_url_keeps_the_prefixed_secure_cookies(
    open_client: OpenClient, callers: Any, public_url: str
) -> None:
    """Under https, a loopback address included, the cookie is the one it was before E1, byte for byte, and
    a valid secret under a plain name is no session."""
    port = urlsplit(public_url).port or 443
    with open_client(auth={"public_url": public_url}) as client:
        signed = _token_sign_in(client, callers)
        secret = client.cookies[COOKIE]
        plain = [
            client.get("/v1/auth/me", headers=_named(name, secret))
            for name in ("ds_session", f"ds_session_{port}", PLAIN, "ds_session_80")
        ]
        unsigned = client.post("/v1/auth/logout", headers=_named(f"ds_session_{port}", secret))
        me = client.get("/v1/auth/me")
        out = client.post("/v1/auth/logout", headers={"X-CSRF-Token": me.json()["csrf"]})
    assert signed.headers.get_list("set-cookie") == [
        f"__Host-ds_session={secret}; HttpOnly; Path=/; SameSite=strict; Secure"
    ]
    assert signed.headers["strict-transport-security"] == "max-age=31536000"
    assert {(r.status_code, r.json()["detail"]) for r in plain} == {(401, "not signed in")}
    assert (unsigned.status_code, unsigned.json()["detail"]) == (400, "only a browser session signs out")
    assert me.status_code == 200
    assert _flags(_set_cookies(out)[COOKIE]) == {
        "httponly", "max-age=0", "path=/", "samesite=strict", "secure"
    }  # fmt: skip


def test_a_request_cannot_choose_the_cookie_form(open_client: OpenClient, callers: Any) -> None:
    """E1 follows ``auth.public_url`` alone. A loopback ``Host`` passes the Host check under any
    ``public_url`` (§3), and names neither the cookie nor its port; nor do ``Origin`` and the headers a
    proxy would add."""
    loopback = {"Host": "localhost:8000", "X-Forwarded-Proto": "http", "Forwarded": "proto=http"}
    with open_client(auth={}) as client:  # https://ds.test
        signed = client.post(
            "/v1/auth/token-login",
            json={"token": callers(client).token("viewer")["Authorization"].removeprefix("Bearer ")},
            headers=loopback,
        )
        secret = signed.cookies[COOKIE]
        client.cookies.clear()
        asked = loopback | {"Origin": TRIAL, "Referer": TRIAL + "/"}
        chosen = client.get("/v1/auth/me", headers=asked | _named(PLAIN, secret))
        kept = client.get("/v1/auth/me", headers=asked | _named(COOKIE, secret))
    assert signed.headers.get_list("set-cookie") == [
        f"__Host-ds_session={secret}; HttpOnly; Path=/; SameSite=strict; Secure"
    ]
    assert (chosen.status_code, chosen.json()["detail"], kept.status_code) == (401, "not signed in", 200)
    secure = {"Host": "127.0.0.1:8443", "X-Forwarded-Proto": "https", "Forwarded": "proto=https"}
    with open_client(auth={"public_url": TRIAL}) as client:
        signed = client.post(
            "/v1/auth/token-login",
            json={"token": callers(client).token("viewer")["Authorization"].removeprefix("Bearer ")},
            headers=secure,
        )
        secret = signed.cookies[PLAIN]
        client.cookies.clear()
        asked = secure | {"Origin": "https://ds.test"}
        chosen = [
            client.get("/v1/auth/me", headers=asked | _named(name, secret))
            for name in (COOKIE, "ds_session_8443", "ds_session_443")
        ]
        kept = client.get("/v1/auth/me", headers=asked | _named(PLAIN, secret))
    assert signed.headers.get_list("set-cookie") == [f"{PLAIN}={secret}; HttpOnly; Path=/; SameSite=strict"]
    assert {(r.status_code, r.json()["detail"]) for r in chosen} == {(401, "not signed in")}
    assert kept.status_code == 200


def test_the_login_cookie_follows_the_same_rule(open_client: OpenClient, idp: FakeIdp) -> None:
    with open_client(auth={"public_url": TRIAL, "oidc": idp.settings()}) as client:
        started = client.get("/v1/auth/login", follow_redirects=False)
        location = started.headers["location"]
        state = dict(parse_qsl(urlsplit(location).query))["state"]
        sealed = client.cookies["ds_login_8000"]
        query = {"code": idp.authorize(location, groups=["G-view"]), "state": state, "iss": idp.issuer}
        prefixed = client.get(
            "/v1/auth/callback", params=query, headers=_named(LOGIN_COOKIE, sealed), follow_redirects=False
        )
        back = client.get("/v1/auth/callback", params=query, follow_redirects=False)  # the jar's, over http
        me = client.get("/v1/auth/me")
    assert _flags(_set_cookies(started)["ds_login_8000"]) == {
        "httponly", "max-age=600", "path=/", "samesite=lax"
    }  # fmt: skip
    assert prefixed.headers["location"] == (
        f"{TRIAL}/#/signin?error=login_expired&request_id={prefixed.headers['x-request-id']}"
    )
    assert "set-cookie" not in prefixed.headers  # a stray callback ends no sign-in in progress
    assert (back.status_code, back.headers["location"]) == (303, f"{TRIAL}/#/runs")
    assert _flags(_set_cookies(back)[PLAIN]) == {"httponly", "path=/", "samesite=strict"}
    assert _flags(_set_cookies(back)["ds_login_8000"]) == {"httponly", "max-age=0", "path=/", "samesite=lax"}
    assert (me.status_code, me.json()["roles"]) == (200, ["viewer"])
    with open_client(auth={"oidc": idp.settings()}) as client:  # https: as before
        started = client.get("/v1/auth/login", follow_redirects=False)
    [header] = started.headers.get_list("set-cookie")
    assert header == (
        f"__Host-ds_login={client.cookies[LOGIN_COOKIE]}; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure"
    )
