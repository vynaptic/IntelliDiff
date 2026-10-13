"""``create_app(settings)``: the public API v1 under /v1 and the built UI (web/dist) at /.

The app holds a Context for its lifetime; ``worker=True`` (``ds serve``) also runs the run worker
in the same event loop. Every /v1 route but the five public ones declares who may call it
(docs/mvp/AUTH.md §4.1). Every error is JSON: ``{"detail": ..., "issues": [{pointer, ...}]}``. No
interactive docs are served: they would load third-party scripts onto the UI's origin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from deltasleuth import licence, notifications, prechecks, readiness, schedules, targets
from deltasleuth.api.deps import UNSAFE, issues_of, ok, principal
from deltasleuth.api.routes import approvals, chat, labellings, meta, packs, runs, samples, specs
from deltasleuth.api.routes import auth as auth_routes
from deltasleuth.api.routes import mcp as mcp_routes
from deltasleuth.api.routes import notifications as notify_routes
from deltasleuth.api.routes import prechecks as precheck_routes
from deltasleuth.api.routes import readiness as readiness_routes
from deltasleuth.api.routes import schedules as schedule_routes
from deltasleuth.approvals import ApprovalConflict, ApprovalRequired
from deltasleuth.auth.principal import SERVER, Throttle
from deltasleuth.auth.sessions import roles_sha256
from deltasleuth.config import LOOPBACK, AuthSettings, Settings
from deltasleuth.connectors.base import ConnectorConfigError, UnsafeTargetError
from deltasleuth.context import Context
from deltasleuth.core.spec import SpecError, TargetsError
from deltasleuth.core.spec.errors import pointer
from deltasleuth.evidence.store import EvidenceKeyError
from deltasleuth.profiler import ProfilerError
from deltasleuth.reports.accuracy import DrawTimeout
from deltasleuth.reports.adjudicate import AdjudicationConflict, AdjudicationError, UploadTooLarge
from deltasleuth.reports.pack import PackForbidden, PackRefused, PackUnavailable
from deltasleuth.runner import drift
from deltasleuth.runner.compare import EvidenceMissing
from deltasleuth.runner.purge import CyclePurged
from deltasleuth.runner.sample import SampleRefused
from deltasleuth.security.crypto import KeyringError
from deltasleuth.store import repo

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
MAX_BODY = 10 * 2**20  # bytes: room for large key lists and sample pairs, not for exhausting memory
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
HSTS = ("strict-transport-security", "max-age=31536000")
PUBLIC = {  # the /v1 routes anyone may call
    ("GET", "/v1/health"),
    ("GET", "/v1/auth/login"),
    ("GET", "/v1/auth/callback"),
    ("POST", "/v1/auth/token-login"),
    ("POST", "/v1/auth/logout"),  # it resolves the caller itself: an ended session still signs out
}
# Reached from other sites by design; /v1/auth/login refuses cross-site requests itself, more strictly.
CROSS_SITE_GETS = frozenset({"/v1/health", "/v1/auth/callback", "/v1/auth/login"})
PROXY_HEADERS = (
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-proto",
    b"x-real-ip",
    b"via",
)
_HOST = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::([0-9]{1,5}))?")


def create_app(settings: Settings, *, worker: bool = False, web_dir: Path = WEB_DIST) -> FastAPI:
    auth = settings.auth
    mapping = roles_sha256(auth and auth.oidc)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The IdP client, in oidc mode only; the data keys move onto the current KEK.
        async with Context.open(settings, oidc=True, rewrap=True, packs=True) as ctx:
            app.state.ctx = ctx
            targets.sweep(settings)  # what a killed change left of the product's credentials
            current, _ = await licence.shown(ctx)  # never raises: each warning is logged
            started = {
                "version": version("deltasleuth"),
                "auth": settings.auth_mode,
                "roles_sha256": mapping,
                "retention": asdict(settings.retention),
                "keks": list(ctx.keyring.ids),
                "current_kek": ctx.keyring.current,
                "missing_keks": sorted(ctx.missing_keks),
                "pack_key": ctx.signer and ctx.signer.fingerprint,  # the key register's entry (PACK.md §3.1)
                "licence": current and current.detail(),  # docs/mvp/LICENCE.md §11
                "mcp": settings.mcp and asdict(settings.mcp),  # docs/mvp/MCP.md §4
            }
            async with repo.audited(ctx.pool) as (conn, audit):
                started["audit_head"] = await repo.audit_head(conn)  # an anchor (docs/mvp/PACK.md §4.1)
                await audit(SERVER, "system.start", "server", started)
            served = _mcp(app, ctx) if settings.mcp is not None and settings.mcp.http else nullcontext()
            async with served, _worker(ctx) if worker else nullcontext():
                yield

    app = FastAPI(
        title="DeltaSleuth", version="1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.roles_sha256 = mapping  # the role mapping every SSO session must be granted under
    app.state.denials = Throttle(60)  # authz.denied rows: one per caller and route a minute
    app.state.login_denials = Throttle(600)  # auth.login_denied rows: one per user and reason in 10 minutes
    app.state.notify_tests = Throttle(10)  # test sends: one per channel in 10 s (docs/mvp/ALERTS.md §10)
    app.state.mcp = None  # the MCP session manager while mcp.http serves it (docs/mvp/MCP.md §5.1)
    app.add_middleware(_Guard, auth=auth)
    if auth and auth.trusted_proxies:  # added last, so it runs first: every check sees the client
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(auth.trusted_proxies))
    for error, handler in _HANDLERS.items():
        app.add_exception_handler(error, handler)
    app.include_router(meta.public, prefix="/v1")
    app.include_router(auth_routes.public, prefix="/v1")
    routers = (
        meta, auth_routes, specs, approvals, packs, samples, notify_routes, labellings, readiness_routes,
        schedule_routes, precheck_routes, mcp_routes, runs, chat,
    )  # fmt: skip
    for routes in routers:  # before runs' {action}
        app.include_router(routes.router, prefix="/v1")
    app.state.templates = {}  # route -> its /v1 path template, which authz.denied rows name
    for route in iter_route_contexts(app.routes):
        if isinstance(route.original_route, APIRoute) and route.path.startswith("/v1/"):
            app.state.templates[id(route.original_route)] = route.path
            unlisted = {(method, route.path) for method in route.methods} - PUBLIC
            if unlisted and not _guarded(route.dependant):
                raise RuntimeError(f"route {sorted(unlisted)} declares no principal (docs/mvp/AUTH.md §1)")
    if web_dir.is_dir():
        app.mount("/", _SinglePageApp(directory=web_dir, html=True), name="web")
    else:  # ds serve has warned (docs/mvp/FIRSTRUN.md §8); every other path stays Not Found
        app.add_api_route("/", _unbuilt, methods=["GET"], include_in_schema=False)  # no /v1 operation
    return app


def _guarded(dependant: Dependant) -> bool:
    return any(d.call is principal or _guarded(d) for d in dependant.dependencies)


@asynccontextmanager
async def _mcp(app: FastAPI, ctx: Context) -> AsyncIterator[None]:
    """The MCP server behind ``POST /v1/mcp`` (docs/mvp/MCP.md §5.1), and the only place this app imports
    the MCP library: one stateless session manager with JSON answers, no stream and no session. ``_Guard``
    has checked Host and Origin, so the library's own check is off; a body is 65,536 bytes at most. A role
    refusal's audit row shares the routes' throttle."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    from deltasleuth.mcp import server

    assert ctx.settings.mcp is not None
    manager = StreamableHTTPSessionManager(
        app=server.build(ctx, ctx.settings.mcp, denials=app.state.denials),
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        max_request_body_size=65_536,
    )
    async with manager.run():
        app.state.mcp = manager
        try:
            yield
        finally:
            app.state.mcp = None


@asynccontextmanager
async def _worker(ctx: Context) -> AsyncIterator[None]:
    from deltasleuth.runner.worker import run_worker

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(ctx, stop=stop))
    try:
        yield
    finally:
        stop.set()  # the worker hands back the leases of keys in flight
        await task


class _Guard:
    """Every request's checks, in order (docs/mvp/AUTH.md §3): a request id; in developer mode, loopback
    clients only, never through a proxy; the Host of /v1 calls (DNS rebinding); Origin and
    Sec-Fetch-Site on unsafe methods. Every response gets the security headers (/v1: no-store), and a
    request body over MAX_BODY is refused (413) while it is read, before it is parsed or its caller
    checked."""

    def __init__(self, app: ASGIApp, auth: AuthSettings | None) -> None:
        self.app, self.auth = app, auth
        headers = [(name.lower().encode(), value.encode()) for name, value in SECURITY_HEADERS.items()]
        self.hosts: set[tuple[str, int]] = set()
        self.origin: tuple[str, str, int] | None = None
        self.default_port = 80
        if auth is not None:
            url = urlsplit(auth.public_url)
            self.default_port = 443 if url.scheme == "https" else 80
            self.origin = (url.scheme, url.hostname or "", url.port or self.default_port)
            hosts = (_host_port(h, self.default_port) for h in (url.netloc, *auth.allowed_hosts))
            self.hosts = {h for h in hosts if h is not None}
            if url.scheme == "https":
                headers.append((HSTS[0].encode(), HSTS[1].encode()))
        self._headers = tuple(headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = "Q-" + secrets.token_hex(8)  # inbound ids are ignored
        scope.setdefault("state", {})["request_id"] = request_id
        path, api = scope["path"], scope["path"] == "/v1" or scope["path"].startswith("/v1/")
        headers = [
            *self._headers,
            (b"x-request-id", request_id.encode()),
            *([(b"cache-control", b"no-store")] if api else []),
        ]
        scope["state"]["headers"] = headers  # for _internal, whose 500 is sent from outside this guard
        size = 0

        async def capped() -> Message:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                raise HTTPException(413, f"a request body is at most {MAX_BODY} bytes")
            return message

        async def secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", ()), *headers]
            await send(message)

        if refusal := self._refusal(scope, path, api):
            return await _error(*refusal)(scope, receive, secured)
        await self.app(scope, capped, secured)

    def _refusal(self, scope: Scope, path: str, api: bool) -> tuple[int, str] | None:
        headers = {name: value.decode("latin-1") for name, value in scope["headers"]}
        if self.auth is None:
            if not _loopback_ip((scope.get("client") or ("",))[0]):
                return 403, "developer mode serves loopback clients only"
            if any(name in headers for name in PROXY_HEADERS):
                return 403, "developer mode refuses proxied requests"
        if api and (scope["method"], path) != ("GET", "/v1/health"):
            host = _host_port(headers.get(b"host", ""), self.default_port)
            if host is None or (host[0] not in LOOPBACK and host not in self.hosts):
                return 400, "unexpected Host header"
        site = headers.get(b"sec-fetch-site")
        if scope["method"] in UNSAFE:
            if (origin := headers.get(b"origin")) is not None and not self._same_origin(origin):
                return 403, "cross-origin request refused"
            if site not in (None, "same-origin"):
                return 403, "cross-site request refused"
        elif api and site not in (None, "same-origin", "none") and path not in CROSS_SITE_GETS:
            # A page elsewhere on the site (an <img>) must not make a read the audit records
            # (key.view, report.download) in its viewer's name; the IdP's redirect to the callback and
            # a load balancer's health probe are the exceptions.
            return 403, "cross-site request refused"
        return None

    def _same_origin(self, origin: str) -> bool:
        """``origin`` is public_url's origin (any loopback origin in developer mode)."""
        try:
            url = urlsplit(origin)
            port = url.port or (443 if url.scheme == "https" else 80)
        except ValueError:
            return False
        if url.scheme not in ("http", "https") or url.path or url.query or not url.hostname:
            return False
        if self.origin is None:
            return url.hostname in LOOPBACK
        return (url.scheme, url.hostname, port) == self.origin


def _host_port(value: str, default_port: int) -> tuple[str, int] | None:
    """A Host value as (host, port), case-folded, the port defaulted; None when malformed."""
    match = _HOST.fullmatch(value.lower())
    return (match[1].strip("[]"), int(match[2] or default_port)) if match else None


def _loopback_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class AuthQueryFilter(logging.Filter):
    """Drops the query of /v1/auth/ paths from uvicorn's access log lines (``ds serve`` adds it), so the
    callback's code, state and iss never reach the logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 2 and str(args[2]).startswith("/v1/auth/"):
            record.args = (*args[:2], str(args[2]).split("?", 1)[0], *args[3:])
        return True


class _SinglePageApp(StaticFiles):
    """The UI's files; any other path outside /v1 and /.well-known gets index.html, so client-side routes
    load."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or path.split(os.sep, 1)[0] in ("v1", ".well-known"):
                raise  # .well-known: a client probing for OAuth metadata reads JSON (docs/mvp/MCP.md §5.1)
            return await super().get_response("index.html", scope)


def _error(status: int, detail: str, issues: list[dict[str, Any]] | None = None, **kw: Any) -> Response:
    return ok({"detail": detail} | ({} if issues is None else {"issues": issues}), status, **kw)


async def _unbuilt() -> Response:
    return _error(404, "the web UI is not built on this server; its API is under /v1")


async def _http_error(_: Request, exc: HTTPException) -> Response:
    return _error(exc.status_code, str(exc.detail), headers=exc.headers)


async def _invalid_request(_: Request, exc: RequestValidationError) -> Response:
    issues = [{"pointer": pointer(e["loc"]), "message": e["msg"], "code": e["type"]} for e in exc.errors()]
    return _error(422, "invalid request", issues)  # never the input: it may hold keys


async def _invalid_spec(_: Request, exc: SpecError) -> Response:
    return _error(422, "invalid spec", issues_of(exc.issues))


async def _invalid_target(_: Request, exc: TargetsError) -> Response:
    return _error(422, "invalid target", issues_of(exc.issues))  # pointers into the targets document


async def _target_refused(_: Request, exc: targets.Refused) -> Response:
    # docs/mvp/DEPLOY.md §7, "In the product": ids, counts and an OS error's text, never a path
    return _error(exc.status, str(exc))


async def _unprocessable(_: Request, exc: Exception) -> Response:
    return _error(422, str(exc))  # profiler messages name paths, never values


async def _conflict(_: Request, exc: Exception) -> Response:
    return _error(409, str(exc))  # four-eyes approvals (docs/mvp/APPROVALS.md §6.2): ids and scopes only


async def _refused(_: Request, exc: repo.ApprovalRefused) -> Response:
    # ds.tg_approvals refused a decision (23514, docs/mvp/APPROVALS.md §6.1); the decision route words
    # the refusals it can explain, and passes on the others.
    if exc.check == "approver":
        return _error(409, "your stored roles lack rule_approver, or your account is disabled; sign in again")
    return _error(409, str(exc).removeprefix("ds.approvals: "))


async def _sample_refused(_: Request, exc: SampleRefused) -> Response:
    return _error(exc.status, str(exc))  # docs/mvp/SAMPLING.md §13 R1-R5: ids, names and population ids


async def _notify_refused(_: Request, exc: notifications.Refused) -> Response:
    return _error(exc.status, str(exc))  # docs/mvp/ALERTS.md §10: channel names, ids and times


async def _readiness_refused(_: Request, exc: readiness.Refused) -> Response:
    return _error(exc.status, str(exc))  # docs/mvp/READINESS.md §9: names, states and counts


async def _schedule_refused(_: Request, exc: schedules.Refused) -> Response:
    return _error(exc.status, str(exc))  # docs/mvp/SCHEDULES.md §9: ids, names, cron text, zones and times


async def _precheck_refused(_: Request, exc: prechecks.Refused) -> Response:
    return _error(exc.status, str(exc))  # docs/mvp/PRECHECKS.md §10: ids, spec names, versions and counts


async def _schema_refused(_: Request, exc: drift.Refused) -> Response:
    # docs/mvp/PRECHECKS.md §10 E6, U1, U2: ids, a version's name, and the names and declared types of the
    # columns a run's own page prints
    return _error(exc.status, str(exc))


async def _too_large(_: Request, exc: Exception) -> Response:
    return _error(413, str(exc))  # a reviewed sheet over 1 MiB (docs/mvp/PACK.md §5)


async def _timed_out(_: Request, exc: Exception) -> Response:
    return _error(504, str(exc))  # a labelling's draw was stopped (docs/mvp/ACCURACY.md §11): ids only


async def _forbidden(_: Request, exc: Exception) -> Response:
    return _error(403, str(exc))


async def _unavailable(_: Request, exc: Exception) -> Response:
    return _error(503, str(exc))  # evidence packs are not configured (docs/mvp/PACK.md §7.2)


async def _gone(_: Request, exc: Exception) -> Response:
    return _error(410, str(exc))  # a purged cycle or a missing blob: ids and dates only


async def _key_missing(_: Request, exc: Exception) -> Response:
    # A key the keyring cannot unwrap (docs/mvp/RETENTION.md §6.1): its message names ids only.
    return _error(503, f"evidence unreadable: {exc} (docs/mvp/RETENTION.md §6.1)")


async def _internal(request: Request, exc: Exception) -> Response:
    # Sent by the outermost middleware, past _Guard, so it adds the guard's headers (the request id,
    # no-store, HSTS) itself. The server log has the traceback; responses never do.
    guard = getattr(request.state, "headers", None)
    headers = {k.decode(): v.decode() for k, v in guard} if guard else SECURITY_HEADERS
    return _error(500, "internal error", headers=headers)


_HANDLERS = {
    HTTPException: _http_error,
    RequestValidationError: _invalid_request,
    SpecError: _invalid_spec,
    TargetsError: _invalid_target,
    targets.Refused: _target_refused,
    ProfilerError: _unprocessable,
    UnsafeTargetError: _unprocessable,  # the guard's refusal of a service's address: its kind and consent
    ConnectorConfigError: _unprocessable,  # a target or a template that cannot be called: names, no value
    ApprovalRequired: _conflict,
    ApprovalConflict: _conflict,
    repo.ApprovalRefused: _refused,
    PackRefused: _conflict,
    licence.LicenceRefused: _conflict,  # docs/mvp/LICENCE.md §9 M1-M8
    SampleRefused: _sample_refused,
    notifications.Refused: _notify_refused,
    readiness.Refused: _readiness_refused,
    schedules.Refused: _schedule_refused,
    prechecks.Refused: _precheck_refused,
    drift.Refused: _schema_refused,
    AdjudicationConflict: _conflict,
    UploadTooLarge: _too_large,
    AdjudicationError: _unprocessable,  # a sheet that cannot be scored: lines and ids, never values
    DrawTimeout: _timed_out,
    PackForbidden: _forbidden,
    PackUnavailable: _unavailable,
    KeyringError: _key_missing,
    EvidenceKeyError: _key_missing,
    CyclePurged: _gone,
    EvidenceMissing: _gone,
    Exception: _internal,
}
