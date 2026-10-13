"""What the routes share: the context, who calls with which roles, exact JSON
responses and key-safe rows."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from deltasleuth.auth import sessions, tokens
from deltasleuth.auth.principal import PERSON_REQUIRED, Principal, dev, expand
from deltasleuth.config import Settings
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.spec import SpecIssue
from deltasleuth.core.spec.keys import fit_parts
from deltasleuth.core.types import CompiledSpec
from deltasleuth.runner.purge import refuse_purged
from deltasleuth.security.tokenise import Tokeniser, mask_key
from deltasleuth.store import repo

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SEALED = ("key_enc", "headers_enc", "sealed")  # raw keys, headers and chat
FRESH = timedelta(minutes=15)  # how recent a person's sign-in must be to create a token or approve
_CHALLENGE = {"WWW-Authenticate": "Bearer"}
log = logging.getLogger("deltasleuth.auth")


def context(request: Request) -> Context:
    return request.app.state.ctx


Ctx = Annotated[Context, Depends(context)]
Limit = Annotated[int, Query(ge=1, le=500)]
_bearer = HTTPBearer(auto_error=False)


def client_addr(request: Request) -> str | None:
    """The client's address (resolved behind ``auth.trusted_proxies``); None when it is not an IP."""
    try:
        return str(ipaddress.ip_address(request.client.host)) if request.client else None
    except ValueError:
        return None


async def principal(
    request: Request, ctx: Ctx, bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Principal:
    """Who calls: developer mode's principal, else a bearer token's, else a cookie session's; 401 for
    none of them, 403 for an unsafe request without its CSRF token."""
    unsafe, auth = request.method in UNSAFE, ctx.settings.auth
    seen = {"request_id": request.state.request_id, "client_addr": client_addr(request)}
    if auth is None:  # _Guard lets in loopback clients only, and never through a proxy
        if unsafe and request.headers.get("x-csrf-token") != "dev":  # against browser pages only
            raise HTTPException(403, "missing or invalid CSRF token")
        return replace(dev(), **seen)
    if bearer is not None:
        async with ctx.pool.connection() as conn:
            token = await tokens.verify(conn, bearer.credentials)
        if token is None:  # not audited: the caller controls how many there are
            given = tokens.token_id(bearer.credentials) or "malformed"
            log.warning("invalid or expired API token %s request_id=%s", given, seen["request_id"])
            raise HTTPException(401, "invalid or expired API token", headers=_CHALLENGE)
        return Principal(f"token:{token['token_id']}", token["name"], expand([token["role"]]), **seen)
    cookie = request.cookies.get(sessions.named(sessions.COOKIE, auth))  # its own form's name only
    if cookie is None:
        raise HTTPException(401, "not signed in", headers=_CHALLENGE)
    async with ctx.pool.connection() as conn:
        found = await repo.session_by_secret(
            conn,
            sessions.secret_hash(cookie),
            request.app.state.roles_sha256,
            idle_minutes=auth.session_idle_minutes,
        )
        if found is None:
            raise HTTPException(401, "session expired", headers=_CHALLENGE)
        csrf = request.headers.get("x-csrf-token", "").encode()
        if unsafe and not hmac.compare_digest(csrf, sessions.csrf_of(cookie).encode()):
            raise HTTPException(403, "missing or invalid CSRF token")
        if request.headers.get("x-ds-background") != "1":  # a polling tab still idles out
            await repo.touch_session(conn, found["session_id"])
    roles, session_id = frozenset(found["roles"]), found["session_id"]
    return Principal(
        found["actor"], found["name"], roles, session_id=session_id, auth_time=found["auth_time"], **seen
    )


def require(role: str, *, fresh_sign_in: bool = False) -> Callable[..., Awaitable[Principal]]:
    """A dependency giving the caller when it holds ``role`` (a person-required role: and is a person);
    else 403, audited as ``authz.denied`` at most once per caller and route a minute. With
    ``fresh_sign_in``, also 403 ``reauth_required`` for a person signed in over FRESH ago (``fresh``)."""

    async def holder(request: Request, ctx: Ctx, who: Annotated[Principal, Depends(principal)]) -> Principal:
        if role not in who.roles:
            refusal = f"needs the {role} role"
        elif role in PERSON_REQUIRED and not who.person:
            refusal = f"the {role} role needs a signed-in person"
        else:
            return fresh(who) if fresh_sign_in else who
        matched = request.scope["route"]
        route = f"{request.method} {request.app.state.templates.get(id(matched), matched.path)}"
        count = request.app.state.denials.hit((who.actor, route))
        if count is None:
            log.warning("authz.denied actor=%s route=%s needed=%s (suppressed)", who.actor, route, role)
        else:
            async with repo.audited(ctx.pool) as (_, audit):
                await audit(who, "authz.denied", route, {"needed": role, "count": count})
        raise HTTPException(403, refusal)

    return holder


def fresh(who: Principal) -> Principal:
    """``who`` when its sign-in (a person's) is at most FRESH old, else 403 ``reauth_required``: a
    hijacked session can neither mint long-lived credentials nor take an approval step. Developer mode
    has no session and is exempt."""
    if who.person and (who.auth_time is None or datetime.now(UTC) - who.auth_time > FRESH):
        raise HTTPException(403, "reauth_required")
    return who


def require_fresh(role: str) -> Callable[..., Awaitable[Principal]]:
    """``require(role)``, and a sign-in at most FRESH old (``fresh``)."""
    return require(role, fresh_sign_in=True)


def trial(settings: Settings) -> bool:
    """Whether the installation is a trial: tokens mode on a plain-http loopback ``public_url``, as
    ``deploy/up.sh`` writes."""
    return settings.auth_mode == "tokens" and sessions.plain(settings.auth)


def declarer(settings: Settings) -> str:
    """The role that declares targets in the product: admin; in a trial
    (``trial``), spec_author. Tokens mode elsewhere has no admin, and keeps the targets file."""
    return "spec_author" if trial(settings) else "admin"


async def _declares(request: Request, ctx: Ctx, who: Annotated[Principal, Depends(principal)]) -> Principal:
    """The caller when it may declare a target: ``declarer``'s role, an admin signed in at most FRESH ago."""
    role = declarer(ctx.settings)
    return await require(role, fresh_sign_in=role == "admin")(request, ctx, who)


# What a route declares as ``who``: the caller, holding the role named.
Anyone = Annotated[Principal, Depends(principal)]
Viewer = Annotated[Principal, Depends(require("viewer"))]
Operator = Annotated[Principal, Depends(require("operator"))]
SpecAuthor = Annotated[Principal, Depends(require("spec_author"))]
RuleApprover = Annotated[Principal, Depends(require("rule_approver"))]
Admin = Annotated[Principal, Depends(require("admin"))]
Declarer = Annotated[Principal, Depends(_declares)]  # per request, from the settings


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a mistyped field is an error, never ignored


def ok(content: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    """JSON with exact numbers: a Decimal is written as its digits, never through a float."""
    return Response(jsonio.dumps(jsonio.plain(content)), status, headers, media_type="application/json")


def shown(row: repo.Row, *hidden: str) -> repo.Row:
    """``row`` without its sealed columns, an opened raw ``key`` (and ``hidden``): outputs name keys by
    ``key_masked`` only. A run's table scan resumes ``after`` a key, so that is left out too, as is what
    the schema guard holds of its tables (``guard``: names and declared types),
    and a file side's sealed pins and the digest of its files."""
    # approval_kind: a foreign key's constant
    left_out = ("key", "approval_kind", "guard", *SEALED, *hidden)
    view = {name: value for name, value in row.items() if name not in left_out}
    if row.get("scan"):
        view["scan"] = {name: value for name, value in row["scan"].items() if name != "after"}
    params = row.get("params")
    if isinstance(params, dict) and isinstance(params.get("tables"), dict):
        tables = {
            side: {k: v for k, v in plan.items() if k != "files_sha256"} if isinstance(plan, dict) else plan
            for side, plan in params["tables"].items()
        }  # whether or not the row still holds its pins: the list route's does not
        view["params"] = {k: v for k, v in params.items() if k != "pinned"} | {"tables": tables}
    return view


async def page(query: Awaitable[repo.Page]) -> Response:
    try:
        rows, cursor = await query
    except ValueError:  # the repo's answer to a cursor it did not issue
        raise HTTPException(400, "malformed cursor") from None
    return ok({"items": [shown(row) for row in rows], "next_cursor": cursor})


def masked_body(body: Mapping[str, Any], spec: CompiledSpec, tokeniser: Tokeniser) -> dict[str, Any]:
    """A stored spec body as the routes show it: its inline keys (``key.source.keys``) masked by
    ``key.class``, as runs show keys."""
    shown_body, chain = deepcopy(dict(body)), [step.name for step in spec.key.normalize]
    source, n = shown_body["key"].get("source", {}), len(spec.key_parts) or 1
    if "keys" in source:  # each fits: the loader refuses a spec with one that does not
        keys = [fit_parts(k, chain, n) or "" for k in source["keys"]]
        source["keys"] = [mask_key(key, spec, tokeniser) for key in keys]
    return shown_body


def issues_of(issues: Iterable[SpecIssue]) -> list[dict[str, Any]]:
    return [asdict(issue) for issue in issues]


def need[T](value: T | None, what: str) -> T:
    if value is None:
        raise HTTPException(404, f"{what} not found")
    return value


async def run_of(conn: repo.Conn, run_id: str) -> repo.Row:
    return need(await repo.get_run(conn, run_id), f"run {run_id}")


async def live_cycle(conn: repo.Conn, run: repo.Row, cycle: int | None = None) -> int:
    """The cycle a route reads, the run's current one by default; ``runner.purge.CyclePurged`` (410)
    once its purge has started."""
    cycle = run["cycle"] if cycle is None else cycle
    await refuse_purged(conn, run["run_id"], cycle)
    return cycle
