"""Sign-in and administration: OIDC sign-in and its callback, token
sign-in (tokens mode), who is calling, sign-out, API tokens, sessions, users and the audit log.

Nothing is written to the database before a person or token is identified: a failed sign-in leaves a
WARNING on ``deltasleuth.auth`` only. Redirects go to ``auth.public_url``, never to ``Host``.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from deltasleuth.api.deps import (
    Admin,
    Anyone,
    Ctx,
    Limit,
    Payload,
    client_addr,
    need,
    ok,
    principal,
    require_fresh,
)
from deltasleuth.auth import sessions, tokens
from deltasleuth.auth.oidc import LOGIN_COOKIE, LOGIN_SECONDS, Identity, Login, Oidc, SignInError, identity
from deltasleuth.auth.principal import ROLES, Principal, expand
from deltasleuth.context import Context
from deltasleuth.store import repo

public = APIRouter(prefix="/auth")  # anyone may call these
router = APIRouter()
log = logging.getLogger("deltasleuth.auth")
# Set and read under sessions.named, with secure=not sessions.plain(auth): plain on loopback http.
_SESSION_COOKIE: dict[str, Any] = {"path": "/", "httponly": True, "samesite": "strict"}
_LOGIN_COOKIE: dict[str, Any] = {"path": "/", "httponly": True, "samesite": "lax"}


def _ordered(roles: Iterable[str]) -> list[str]:
    held = set(roles)
    return [role for role in ROLES if role in held]


def _oidc(ctx: Context) -> Oidc:
    if ctx.oidc is None:
        raise HTTPException(404, "SSO is not configured")
    return ctx.oidc


def _to_signin(oidc: Oidc, request: Request, code: str) -> Response:
    """The sign-in page with a fixed error code; IdP text is never reflected."""
    query = urlencode({"error": code, "request_id": request.state.request_id})
    return RedirectResponse(f"{oidc.public_url}/#/signin?{query}", 303)


def _failed(request: Request, exc: SignInError) -> None:
    log.warning(
        "auth.login_failed reason=%s ip=%s request_id=%s: %s",
        exc.code,
        client_addr(request),
        request.state.request_id,
        exc,
    )


def _denial(user: repo.Row, who: Identity) -> str | None:
    if user["disabled_at"] is not None:
        return "disabled"
    if who.roles is None:
        return "groups_overage"
    return None if who.roles else "no_role"


# -- sign-in ---------------------------------------------------------------------------------------


@public.get("/login")
async def login(
    request: Request,
    ctx: Ctx,
    next_: Annotated[str | None, Query(alias="next")] = None,
    reauth: bool = False,
) -> Response:
    """Start a sign-in: 303 to the IdP, with the signed login state in ``__Host-ds_login``
    (``ds_login_<port>`` on a plain-http loopback ``public_url``). Writes nothing to the database."""
    oidc, auth = _oidc(ctx), ctx.settings.auth
    assert auth is not None  # oidc mode
    site, mode = request.headers.get("sec-fetch-site"), request.headers.get("sec-fetch-mode")
    if site not in (None, "same-origin", "none") or mode not in (None, "navigate"):
        raise HTTPException(403, "cross-site sign-in refused")  # an <img> must not restart a sign-in
    replaces = None
    cookie = request.cookies.get(sessions.named(sessions.COOKIE, auth))
    if cookie is not None:  # Strict: sent on this navigation
        async with ctx.pool.connection() as conn:
            found = await repo.session_by_secret(
                conn,
                sessions.secret_hash(cookie),
                request.app.state.roles_sha256,
                idle_minutes=auth.session_idle_minutes,
            )
        replaces = found and found["session_id"]
    started = Login.new(next_, reauth=reauth, replaces=replaces)
    try:
        url = await oidc.authorization_url(started)
    except SignInError as exc:
        _failed(request, exc)
        return _to_signin(oidc, request, exc.code)
    response = RedirectResponse(url, 303)
    name, secure = sessions.named(LOGIN_COOKIE, auth), not sessions.plain(auth)
    sealed = started.seal(oidc.login_key)
    response.set_cookie(name, sealed, max_age=LOGIN_SECONDS, secure=secure, **_LOGIN_COOKIE)
    return response


@public.get("/callback")
async def callback(
    request: Request,
    ctx: Ctx,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    iss: str | None = None,
) -> Response:
    """Finish a sign-in: always 303, to the ``next`` route or to the sign-in page with a fixed code."""
    oidc, auth = _oidc(ctx), ctx.settings.auth
    name = sessions.named(LOGIN_COOKIE, auth)
    started = Login.unseal(request.cookies.get(name), oidc.login_key)
    try:
        if (
            started is None
            or state is None
            or not hmac.compare_digest(state.encode(), started.state.encode())
        ):
            raise SignInError(
                "login_expired", "the login cookie is missing, altered or expired, or state differs"
            )
        if error is not None:
            code_ = "access_denied" if error == "access_denied" else "idp_error"
            raise SignInError(code_, f"the identity provider answered error={error[:100]!r}")
        if iss is not None and iss != oidc.settings.issuer:  # RFC 9207
            raise SignInError("login_failed", f"iss {iss[:200]!r} is not auth.oidc.issuer")
        if not code:
            raise SignInError("login_failed", "the callback carries no code")
        who = identity(await oidc.sign_in(code, started), oidc.settings)
    except SignInError as exc:  # no identity yet: no database row, no audit row
        _failed(request, exc)
        response = _to_signin(oidc, request, exc.code)
        if exc.code != "login_expired":  # a stray callback must not end a sign-in in progress
            response.delete_cookie(name, secure=not sessions.plain(auth), **_LOGIN_COOKIE)
        return response
    return await _signed_in(request, ctx, oidc, started, who)


async def _signed_in(request: Request, ctx: Context, oidc: Oidc, started: Login, who: Identity) -> Response:
    """The identity is known: upsert the user, then refuse it or open its session, in one transaction
    whose audit rows come last."""
    auth, addr = ctx.settings.auth, client_addr(request)
    assert auth is not None  # oidc mode
    mapping, cookie = request.app.state.roles_sha256, sessions.new_secret()
    async with repo.audited(ctx.pool) as (conn, audit):
        user = await repo.upsert_user(
            conn,
            oidc.settings.issuer,
            who.subject,
            who.username,
            who.display_name,
            None if who.roles is None else sorted(who.roles),
            signed_in=bool(who.roles),
        )
        user_id = user["user_id"]
        request_id = request.state.request_id
        person = Principal(
            f"user:{user_id}",
            user["username"],
            frozenset(user["roles"]),
            request_id=request_id,
            client_addr=addr,
        )
        rows: list[tuple[Principal, str, str, dict[str, Any]]] = []
        if started.replaces is not None:  # the session presented at /login, in this browser, ends
            await repo.revoke_sessions(conn, person.actor, session_id=started.replaces)
        previous = user["previous_roles"]
        if previous is not None and previous != user["roles"]:
            change = {"from": _ordered(previous), "to": _ordered(user["roles"])}
            rows.append((person, "auth.roles_changed", user_id, change))
        if (reason := _denial(user, who)) is not None:
            count = request.app.state.login_denials.hit((user_id, reason))
            seen = "" if reason == "disabled" else f" claims={list(who.claim_names)!r} groups={who.groups}"
            log.warning(
                "auth.login_denied reason=%s user_id=%s request_id=%s%s%s",
                reason,
                user_id,
                request_id,
                seen,
                " (suppressed)" if count is None else "",
            )
            if count is not None:
                rows.append((person, "auth.login_denied", user_id, {"reason": reason, "count": count}))
        else:
            assert who.roles is not None
            await repo.delete_ended_sessions(conn)
            session = await repo.create_session(
                conn,
                secret_hash=sessions.secret_hash(cookie),  # a fresh id and secret: no fixation
                actor=person.actor,
                name=person.name,
                roles=sorted(who.roles),
                max_hours=auth.session_max_hours,
                user_id=user_id,
                roles_sha256=mapping,
                client_addr=addr,
                auth_time=who.auth_time,
                acr=who.acr,
                amr=who.amr,
            )
            person = replace(person, session_id=session["session_id"], auth_time=who.auth_time)
            detail = {"method": "oidc", "roles": _ordered(who.roles), "reauth": started.reauth}
            rows.append((person, "auth.login", session["session_id"], detail))
        for row in rows:
            await audit(*row)
    name, secure = sessions.named(sessions.COOKIE, auth), not sessions.plain(auth)
    if reason is not None:  # refused: the session this browser had ended above
        response = _to_signin(oidc, request, reason)
        response.delete_cookie(name, secure=secure, **_SESSION_COOKIE)
    else:
        response = RedirectResponse(f"{oidc.public_url}/{started.next}", 303)
        response.set_cookie(name, cookie, secure=secure, **_SESSION_COOKIE)
    response.delete_cookie(sessions.named(LOGIN_COOKIE, auth), secure=secure, **_LOGIN_COOKIE)
    return response


def _tokens_mode(ctx: Ctx) -> None:
    auth = ctx.settings.auth
    if auth is None or auth.oidc is not None:  # a machine token must not bypass SSO and MFA
        raise HTTPException(404, "token sign-in is not enabled")


class TokenLoginIn(Payload):
    token: str


@public.post("/token-login", dependencies=[Depends(_tokens_mode)])
async def token_login(request: Request, ctx: Ctx, body: TokenLoginIn) -> Response:
    """Tokens mode: a cookie session for the token, which acts as the token; returns the /me body."""
    auth, addr = ctx.settings.auth, client_addr(request)
    assert auth is not None
    cookie = sessions.new_secret()
    async with repo.audited(ctx.pool) as (conn, audit):
        token = await tokens.verify(conn, body.token)
        if token is None:  # not audited: the caller controls how many there are
            given = tokens.token_id(body.token) or "malformed"
            log.warning("invalid or expired API token %s request_id=%s", given, request.state.request_id)
            raise HTTPException(401, "invalid or expired API token", headers={"WWW-Authenticate": "Bearer"})
        actor, name, roles = f"token:{token['token_id']}", token["name"], expand([token["role"]])
        await repo.delete_ended_sessions(conn)
        session = await repo.create_session(
            conn,
            secret_hash=sessions.secret_hash(cookie),
            actor=actor,
            name=name,
            roles=sorted(roles),
            max_hours=auth.session_max_hours,
            token_id=token["token_id"],
            client_addr=addr,
        )
        seen = {"request_id": request.state.request_id, "client_addr": addr}
        who = Principal(actor, name, roles, session_id=session["session_id"], **seen)
        detail = {"method": "token", "roles": _ordered(roles), "reauth": False}
        await audit(who, "auth.login", session["session_id"], detail)
    response = ok(
        _me(ctx, who, display_name=None, csrf=sessions.csrf_of(cookie), expires_at=session["expires_at"])
    )
    secure = not sessions.plain(auth)
    response.set_cookie(sessions.named(sessions.COOKIE, auth), cookie, secure=secure, **_SESSION_COOKIE)
    return response


# -- the caller ------------------------------------------------------------------------------------


def _me(
    ctx: Context, who: Principal, *, display_name: str | None, csrf: str | None, expires_at: datetime | None
) -> dict[str, Any]:
    auth = ctx.settings.auth
    kind = "user" if who.person else "token" if who.actor.startswith("token:") else "dev"
    return {
        "actor": who.actor,
        "name": who.name,
        "display_name": display_name,
        "kind": kind,
        "roles": _ordered(who.roles),
        "csrf": csrf,
        "mode": ctx.settings.auth_mode,
        "idle_minutes": auth and auth.session_idle_minutes,
        "expires_at": expires_at,
    }


@router.get("/auth/me")
async def me(request: Request, ctx: Ctx, who: Anyone) -> Response:
    """Who calls, and the CSRF token its unsafe requests carry (a cookie session's; ``dev`` in
    developer mode; none for a bearer)."""
    async with ctx.pool.connection() as conn:
        user = await repo.get_user(conn, who.actor.removeprefix("user:")) if who.person else None
        if who.session_id is not None:
            ending = await repo.get_session(conn, who.session_id)
        elif who.actor.startswith("token:"):  # a bearer
            ending = await repo.token_by_id(conn, who.actor.removeprefix("token:"))
        else:
            ending = None
    cookie = request.cookies.get(sessions.named(sessions.COOKIE, ctx.settings.auth))
    csrf = "dev" if ctx.settings.auth is None else None
    if who.session_id is not None and cookie is not None:
        csrf = sessions.csrf_of(cookie)
    display_name, expires_at = user and user["display_name"], ending and ending["expires_at"]
    return ok(_me(ctx, who, display_name=display_name, csrf=csrf, expires_at=expires_at))


@router.post("/auth/logout")
async def logout(request: Request, ctx: Ctx) -> Response:
    """End the browser's session and return where the browser signs out of the IdP, if anywhere. A
    session that has already ended (idle) still signs out of the IdP; only a live one is revoked and
    audited, and it needs its CSRF token."""
    auth = ctx.settings.auth
    name = sessions.named(sessions.COOKIE, auth)
    if name not in request.cookies:
        raise HTTPException(400, "only a browser session signs out")
    try:
        who: Principal | None = await principal(request, ctx, None)
    except HTTPException as exc:
        if exc.status_code != 401:  # a CSRF refusal stays a refusal
            raise
        who = None
    async with repo.audited(ctx.pool) as (conn, audit):
        if who is None:  # ended for this server (idle, another mode or role mapping): never again
            await repo.revoke_session_secret(conn, sessions.secret_hash(request.cookies[name]))
        elif who.session_id is not None and await repo.revoke_sessions(
            conn, who.actor, session_id=who.session_id
        ):
            await audit(who, "auth.logout", who.session_id)  # only when this request ended it
    logout_url = await ctx.oidc.logout_url() if ctx.oidc is not None else None
    # Never "cookies": that would clear every cookie of the registrable domain.
    response = ok({"logout_url": logout_url}, headers={"Clear-Site-Data": '"storage"'})
    response.delete_cookie(name, secure=not sessions.plain(auth), **_SESSION_COOKIE)
    return response


# -- API tokens ------------------------------------------------------------------------------------


FreshAdmin = Annotated[Principal, Depends(require_fresh("admin"))]  # signed in within 15 minutes


class TokenIn(Payload):
    name: str
    owner: str
    owner_user_id: str | None = None  # the accountable person; required with SSO
    role: str
    days: int | None = None


@router.get("/auth/tokens")
async def list_tokens(ctx: Ctx, who: Admin) -> Response:
    async with ctx.pool.connection() as conn:
        return ok({"items": await repo.list_tokens(conn)})


@router.post("/auth/tokens", status_code=201)
async def create_token(ctx: Ctx, who: FreshAdmin, body: TokenIn) -> Response:
    """A new token, shown this once."""
    auth = ctx.settings.auth
    if auth is not None and auth.oidc is not None and body.owner_user_id is None:
        raise HTTPException(422, "owner_user_id is required")
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            row, token = await tokens.create(
                conn,
                who,
                name=body.name,
                owner=body.owner,
                role=body.role,
                max_days=auth.token_max_days if auth is not None else tokens.DEFAULT_DAYS,
                days=body.days,
                owner_user_id=body.owner_user_id,
            )
            detail = {name: row[name] for name in ("name", "owner", "owner_user_id", "role")}
            expires_at = row["expires_at"]
            await audit(who, "token.create", row["token_id"], detail | {"expires_at": expires_at.isoformat()})
    except ValueError as exc:  # the message names the field
        raise HTTPException(422, str(exc)) from None
    return ok({"token_id": row["token_id"], **detail, "expires_at": expires_at, "token": token}, 201)


@router.delete("/auth/tokens/{token_id}")
async def revoke_token(ctx: Ctx, who: Admin, token_id: str) -> Response:
    async with repo.audited(ctx.pool) as (conn, audit):
        need(await repo.token_by_id(conn, token_id), f"token {token_id}")
        row = await repo.revoke_token(conn, token_id, who.actor)
        if row is None:
            raise HTTPException(409, f"token {token_id} is already revoked")
        await audit(who, "token.revoke", token_id)
    return ok({"token_id": token_id, "revoked_at": row["revoked_at"]})


# -- sessions and users ----------------------------------------------------------------------------


@router.get("/auth/sessions")
async def list_sessions(ctx: Ctx, who: Admin) -> Response:
    async with ctx.pool.connection() as conn:
        return ok({"items": await repo.list_sessions(conn)})


@router.delete("/auth/sessions/{session_id}")
async def revoke_session(ctx: Ctx, who: Admin, session_id: str) -> Response:
    async with repo.audited(ctx.pool) as (conn, audit):
        count = await repo.revoke_sessions(conn, who.actor, session_id=session_id)
        if not count:  # unknown, or already ended
            raise HTTPException(404, f"session {session_id} not found")
        await audit(who, "session.revoke", session_id, {"count": count})
    return ok({"session_id": session_id, "count": count})


@router.get("/auth/users")
async def list_users(ctx: Ctx, who: Admin) -> Response:
    """Every user with the roles of its last sign-in, for access reviews."""
    async with ctx.pool.connection() as conn:
        return ok({"items": await repo.list_users(conn)})


class DisableIn(Payload):
    revoke_tokens: bool = False


@router.post("/auth/users/{user_id}/disable")
async def disable_user(ctx: Ctx, who: Admin, user_id: str, body: DisableIn | None = None) -> Response:
    """Disable the user and end its sessions; with ``revoke_tokens`` also revoke every live token it
    created or owns. Lists those tokens, so the admin sees what would outlive the person."""
    if who.actor == f"user:{user_id}":
        raise HTTPException(409, "you cannot disable yourself")
    revoke = body is not None and body.revoke_tokens
    async with repo.audited(ctx.pool) as (conn, audit):
        need(await repo.get_user(conn, user_id), f"user {user_id}")
        row = await repo.set_user_disabled(conn, user_id, who.actor)
        if row is None:
            raise HTTPException(409, f"user {user_id} is already disabled")
        revoked = await repo.revoke_user_tokens(conn, user_id, who.actor) if revoke else []
        listed = await repo.user_tokens(conn, user_id)
        detail = {"sessions_revoked": row["sessions_revoked"], "tokens_revoked": revoked}
        await audit(who, "user.disable", user_id, detail)
    return ok({"user_id": user_id, "disabled_at": row["disabled_at"], "tokens": listed} | detail)


@router.post("/auth/users/{user_id}/enable")
async def enable_user(ctx: Ctx, who: Admin, user_id: str) -> Response:
    """Let the user sign in again; revoked tokens stay revoked."""
    async with repo.audited(ctx.pool) as (conn, audit):
        need(await repo.get_user(conn, user_id), f"user {user_id}")
        if await repo.set_user_disabled(conn, user_id, None) is None:
            raise HTTPException(409, f"user {user_id} is not disabled")
        await audit(who, "user.enable", user_id)
    return ok({"user_id": user_id, "disabled_at": None})


# -- the audit log ---------------------------------------------------------------------------------


@router.get("/audit")
async def audit_log(
    ctx: Ctx,
    who: Admin,
    actor: str | None = None,
    action: str | None = None,
    obj: Annotated[str | None, Query(alias="object")] = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Limit = 100,
    cursor: str | None = None,
) -> Response:
    """Audit rows newest first, each filter exact; the read is audited with its filters."""
    given = {"actor": actor, "action": action, "object": obj, "session_id": session_id}
    given |= {"since": since and since.isoformat(), "until": until and until.isoformat()}
    filters = {name: value for name, value in given.items() if value is not None}
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            rows, next_cursor = await repo.list_audit(
                conn,
                actor=actor,
                action=action,
                obj=obj,
                session_id=session_id,
                since=since,
                until=until,
                limit=limit,
                cursor=cursor,
            )
            await audit(who, "audit.read", "audit", {"filters": filters})
    except ValueError:  # the repo's answer to a cursor it did not issue
        raise HTTPException(400, "malformed cursor") from None
    return ok({"items": rows, "next_cursor": next_cursor})
