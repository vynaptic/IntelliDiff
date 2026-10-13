"""REST connector: fetch one key from one HTTP target as an ``Observation``.

Statuses are evidence, not verdicts. ``latency_ms`` sums, per page, the attempt that produced it
(it measures the target, not our queues); ``attempts`` is the most any page needed; ``pages``
counts responses received, a failing last one included and an empty end page excluded. A key listing
too large to merge is streamed a page at a time instead (``walk``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable, Mapping
from functools import partial
from typing import Any

from deltasleuth.connectors.base import (
    TRANSPORT_ERRORS,
    ConnectorConfigError,
    ConnectorError,
    circuit_open,
    guarded,
    utc_now_iso,
)
from deltasleuth.connectors.breaker import Ticket
from deltasleuth.connectors.http import HttpCaller, HttpResult, TargetGate
from deltasleuth.connectors.netguard import GuardedResolver, HostResolver, check_literal_host, system_resolve
from deltasleuth.connectors.paging import PageWalk, check_pagination, window_params
from deltasleuth.connectors.templating import (
    FRAMING_HEADERS,
    Endpoint,
    RenderedRequest,
    build_request,
    check_header_name,
    template_variables,
)
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import content_hash
from deltasleuth.core.secrets import read_secret
from deltasleuth.core.types import (
    ExecutionConfig,
    Observation,
    Pagination,
    RequestTemplate,
    SideConfig,
    Target,
    TargetAuth,
)

SecretReader = Callable[[str], str]
_OPEN = "circuit breaker is open for this target; no call was made"


class ListingIncomplete(ConnectorError):
    """A streamed key listing (``walk``) was not read completely: a page failed, or the walk ended before
    the last page or unstable. Its message names the target, a status and positions, never a key."""


def _auth_headers(auth: TargetAuth, read: SecretReader) -> dict[str, str]:
    """The headers carrying the target's credentials, each read once."""
    if auth.kind == "none":
        return {}
    if not auth.secret_ref:
        raise ConnectorConfigError(f"auth kind {auth.kind!r} needs a secret_ref")
    secret = read(auth.secret_ref)
    if not secret or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in secret):  # never echo it
        raise ConnectorConfigError("the target's secret is empty or contains control characters")
    if auth.kind == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if auth.kind != "header" or not auth.header:
        raise ConnectorConfigError(f"auth kind {auth.kind!r} is not usable (header auth needs a header name)")
    check_header_name(auth.header)
    if auth.header.lower() in FRAMING_HEADERS:
        raise ConnectorConfigError(f"header {auth.header!r} cannot carry credentials")
    headers = {auth.header: secret}
    for name, ref in auth.more:  # each further header as the first: its name and its secret checked
        headers |= _auth_headers(TargetAuth("header", name, ref), read)
    return headers


def _ended_on(
    observation: Callable[..., Observation], result: HttpResult, detail: str | None, attempts: int, pages: int
) -> Observation:
    """The observation of a read that ended on ``result``: its only response, or a failed page."""
    return observation(
        http_status=result.status,
        body=result.body,
        latency_ms=result.latency_ms,
        headers=result.headers,
        error_class=result.error_class,
        error_detail=detail,
        attempts=attempts,
        pages=pages,
        content_hash=content_hash(result.body),
    )


class RestConnector:
    """``Connector`` over one HTTP target.

    Pass the same ``gate`` to every connector of a target so the budget and concurrency cap hold
    across them, and its breaker across those whose ``execution`` declares the same one
    (``TargetGate.breaker``). ``resolver`` replaces DNS for the SSRF guard (tests).
    """

    def __init__(
        self,
        target: Target,
        execution: ExecutionConfig,
        *,
        secret_reader: SecretReader = read_secret,
        gate: TargetGate | None = None,
        resolver: HostResolver = system_resolve,
        max_body_bytes: int = jsonio.DEFAULT_MAX_BYTES,
    ) -> None:
        if execution.total_timeout_s <= 0 or max_body_bytes < 0:
            raise ConnectorConfigError("total timeout must be positive and the body cap non-negative")
        self._target = target
        self._endpoint = Endpoint.parse(target.base_url)
        self._auth = _auth_headers(target.auth, secret_reader)
        gate = gate or TargetGate.for_target(target)
        self._breaker = gate.breaker(execution)
        guarded = GuardedResolver(resolver, allow_private=target.allow_private_network)
        self._http = HttpCaller(gate, execution, guarded, self._auth)
        self._max_body = max_body_bytes

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def credentials(self) -> tuple[str, ...]:
        """The target's credentials as they were read (a bearer's without its scheme): what a caller that
        shows an answer keeps out of it."""
        return tuple(value.removeprefix("Bearer ") for value in self._auth.values())

    async def fetch(
        self, key: str, side_cfg: SideConfig, *, run_id: str = "p0", call_seq: int = 0
    ) -> Observation:
        if side_cfg.request is None:
            raise ConnectorConfigError("this side has no request template (file-sample side?)")
        paging = check_pagination(side_cfg.pagination)
        variables = template_variables(key, run_id, call_seq, side_cfg.request.key_parts)
        render = partial(self._request, side_cfg.request, variables)
        first = render(window_params(paging) if paging else ())
        if not self._target.allow_private_network:
            check_literal_host(self._endpoint.host)
        observation = partial(
            Observation, key=key, side=side_cfg.side, observed_at=utc_now_iso(), request_url=str(first.url)
        )
        return await guarded(
            self._breaker,
            lambda: circuit_open(observation),
            lambda ticket: self._read(observation, side_cfg, paging, render, first, ticket),
            lambda obs: None if obs.attempts == 0 else obs.error_class in TRANSPORT_ERRORS,
        )

    async def walk(self, side_cfg: SideConfig) -> AsyncIterator[list[Any]]:
        """Each page's items of the key listing ``side_cfg`` pages through (docs/mvp/SAMPLING.md §3), one
        call at a time under the target's gate and breaker. The pages are judged as ``fetch`` judges them
        (loops, ``maxPages``, declared ``stable_ordering``) but never merged, each capped at the body limit,
        and a repeat is judged against the previous page only. ``ListingIncomplete`` once the walk proves
        incomplete, after the pages it took."""
        paging = check_pagination(side_cfg.pagination)
        if side_cfg.request is None or paging is None:
            raise ConnectorConfigError("a key listing needs a request template and pagination")
        render = partial(self._request, side_cfg.request, template_variables("keys", "p0", 0))
        first = request = render(window_params(paging))
        if not self._target.allow_private_network:
            check_literal_host(self._endpoint.host)
        walk = PageWalk(paging, stable_ordering=self._target.capabilities.stable_ordering, merge=False)
        status, page_no = None, 0
        while True:
            page = await self._page(request)
            page_no += 1
            status = page.status if status is None else status
            if page.error_class:
                where = "" if page_no == 1 else f"page {page_no}: "
                raise self._incomplete(page.status, where + (page.error_detail or page.error_class))
            if page_no == 1:
                if page.status not in side_cfg.success_status:
                    raise self._incomplete(page.status, None)
                walk.start(page.body or b"", page.headers, request.url)
            elif page.status not in side_cfg.success_status:
                walk.stop(f"page {page_no} returned HTTP {page.status}; paging stopped")
            else:
                walk.add(page.body or b"", page.headers, request.url)
            if walk.page:
                yield walk.page
            if (step := walk.next) is None:
                break
            if step.link is None:
                request = render(step.params)
            elif self._endpoint.admits(step.link):  # auth headers must never leave the origin
                request = dataclasses.replace(first, url=step.link)
            else:
                walk.stop("pagination next link is not same-origin with the target; paging stopped")
                break
        if not walk.eof or walk.unstable:
            raise self._incomplete(status, walk.detail)

    async def _page(self, request: RenderedRequest) -> HttpResult:
        """One page of a streamed listing, admitted by the breaker on its own."""
        return await guarded(
            self._breaker,
            lambda: HttpResult(None, {}, None, "circuit_open", _OPEN, attempts=0),
            lambda ticket: self._http.call(request, max_bytes=self._max_body, ticket=ticket),
            lambda result: None if result.attempts == 0 else result.error_class in TRANSPORT_ERRORS,
        )

    def _incomplete(self, status: int | None, problem: str | None) -> ListingIncomplete:
        return ListingIncomplete(
            f"the key listing of target {self._target.target_id} was not read completely (HTTP {status}:"
            f" {problem or 'an incomplete or unstable page walk'})"
        )

    def _request(
        self, template: RequestTemplate, variables: Mapping[str, str], params: tuple[tuple[str, str], ...]
    ) -> RenderedRequest:
        return build_request(self._endpoint, template, variables, auth_headers=self._auth, page_params=params)

    async def _read(
        self,
        observation: Callable[..., Observation],
        side_cfg: SideConfig,
        paging: Pagination | None,
        render: Callable[[tuple[tuple[str, str], ...]], RenderedRequest],
        first: RenderedRequest,
        ticket: Ticket,
    ) -> Observation:
        result = await self._http.call(first, max_bytes=self._max_body, ticket=ticket)
        if paging is None or result.error_class or result.status not in side_cfg.success_status:
            return _ended_on(
                observation, result, result.error_detail, result.attempts, min(result.attempts, 1)
            )
        walk = PageWalk(paging, stable_ordering=self._target.capabilities.stable_ordering)
        walk.start(result.body or b"", result.headers, first.url)
        pages, attempts, latency, size = 1, result.attempts, result.latency_ms or 0.0, len(result.body or b"")
        while (step := walk.next) is not None:
            if step.link is None:
                request = render(step.params)
            elif self._endpoint.admits(step.link):  # auth headers must never leave the origin
                request = dataclasses.replace(first, url=step.link)
            else:
                walk.stop("pagination next link is not same-origin with the target; paging stopped")
                break
            page_no = pages + 1
            page = await self._http.call(request, max_bytes=max(self._max_body - size, 0), ticket=ticket)
            if page.attempts:  # a page that was sent and failed still counts
                pages, attempts = page_no, max(attempts, page.attempts)
                latency += page.latency_ms or 0.0
                size += len(page.body or b"")
            if page.error_class:
                return _ended_on(observation, page, f"page {page_no}: {page.error_detail}", attempts, pages)
            if page.status not in side_cfg.success_status:
                walk.stop(f"page {page_no} returned HTTP {page.status}; paging stopped")
                break
            walk.add(page.body or b"", page.headers, request.url)
        if walk.ended_on_empty_page:
            pages -= 1
        body = walk.body()
        common = {
            "http_status": result.status,
            "latency_ms": round(latency, 3),
            "headers": result.headers,
            "attempts": attempts,
            "pages": pages,
        }
        if len(body) > self._max_body:
            # Exact re-serialisation can outgrow the pages (1E+99 merges as 100 digits); the
            # evidence store refuses such a body, so it is not evidence either.
            detail = f"merged body of {pages} pages exceeds {self._max_body} bytes"
            return observation(**common, body=None, error_class="resource_limit", error_detail=detail)
        return observation(
            **common,
            body=body,
            error_detail=walk.detail,
            pages_eof=walk.eof,
            pagination_unstable=walk.unstable,
            content_hash=content_hash(body),
        )
