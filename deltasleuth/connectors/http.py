"""One polite HTTP call: target budget, concurrency cap, timeouts, body cap and retries.

``HttpCaller.call`` classifies instead of raising: connect errors, timeouts, 429 and 5xx are
retried per policy; 401/403 are ``auth_error`` at once (body dropped: servers echo credentials);
a body over the cap is ``resource_limit``; other statuses go back untouched for ``observe()``.
``attempts == 0`` means nothing was sent.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace

import aiohttp
from aiohttp.abc import AbstractResolver
from aiolimiter import AsyncLimiter

from deltasleuth.connectors.base import ConnectorConfigError, record_headers
from deltasleuth.connectors.breaker import CircuitBreaker, Ticket
from deltasleuth.connectors.retry import parse_retry_after, retry_delay
from deltasleuth.connectors.templating import RenderedRequest
from deltasleuth.core.types import ExecutionConfig, Target

_CHUNK_BYTES = 64 * 1024
_USER_AGENT = "deltasleuth-p0"


@dataclass(frozen=True)
class HttpResult:
    """Outcome of one call after retries; headers already filtered for evidence."""

    status: int | None
    headers: dict[str, str]
    body: bytes | None
    error_class: str | None = None
    error_detail: str | None = None
    attempts: int = 1
    latency_ms: float | None = None  # the final attempt: send to last body byte
    retry_after_s: float | None = None


class TargetGate:
    """Politeness state shared by every connector of one target: budget, slots, pause, and a breaker for
    each ``execution.circuitBreaker`` its connectors declare.

    One request per ``1 / rate_per_s`` seconds with no burst, so no second ever carries more
    than ``rate_per_s + 1`` requests. ``not_before`` (a Retry-After pause) only moves later.
    """

    def __init__(self, rate_per_s: float, max_concurrency: int) -> None:
        self.rebudget(rate_per_s, max_concurrency)
        self.not_before = 0.0  # time.monotonic() before which nothing is sent
        self._breakers: dict[tuple[float | None, int, float], CircuitBreaker] = {}

    def rebudget(self, rate_per_s: float, max_concurrency: int) -> None:
        """Set the budget, at once for every connector under this gate (a target declared in the product
        was changed: ``deltasleuth.targets``); the pause and the breakers stay. Calls in flight finish in
        the slots they hold."""
        if rate_per_s <= 0 or max_concurrency < 1:
            raise ConnectorConfigError("rate_per_s must be > 0 and max_concurrency >= 1")
        self.limiter = AsyncLimiter(1, 1.0 / rate_per_s)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.max_concurrency = max_concurrency

    @classmethod
    def for_target(cls, target: Target) -> TargetGate:
        return cls(target.rate_per_s, target.max_concurrency)

    def breaker(self, execution: ExecutionConfig) -> CircuitBreaker:
        """The target's breaker for what ``execution`` declares (rate, window, cooldown), shared by every
        connector that declares the same: a spec's own settings govern its calls, and an open breaker
        refuses no call of a spec that declares another, or none (whose own never opens)."""
        declared = (execution.breaker_error_rate, execution.breaker_window, execution.breaker_cooldown_s)
        return self._breakers.setdefault(declared, CircuitBreaker(*declared))


class _Deadline:
    """A call's total-timeout clock, extended by time spent queueing for our own budget."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def extend(self, seconds: float) -> None:
        self._end += seconds


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _not_sent(error_class: str, detail: str) -> HttpResult:
    return HttpResult(None, {}, None, error_class, detail, attempts=0)


def _failed(error_class: str, detail: str, started: float) -> HttpResult:
    return HttpResult(None, {}, None, error_class, detail, latency_ms=_ms(started))


def _status_class(status: int) -> str | None:
    if status in (401, 403):
        return "auth_error"
    if status == 429:
        return "http_429"
    return "http_5xx" if 500 <= status <= 599 else None


async def _read_capped(response: aiohttp.ClientResponse, max_bytes: int) -> bytes | None:
    """The body, or None above ``max_bytes``. Streamed with a running total: Content-Length
    may be absent or compressed, and a decompression bomb must not be held in memory."""
    if response.content_length is not None and response.content_length > max_bytes:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class HttpCaller:
    """Sends rendered requests for one target through its gate."""

    def __init__(
        self,
        gate: TargetGate,
        execution: ExecutionConfig,
        resolver: AbstractResolver,
        credentials: Iterable[str] = (),
    ) -> None:
        self._gate = gate
        self._breaker = gate.breaker(execution)
        self._execution = execution
        self._resolver = resolver
        self._credentials = frozenset(name.lower() for name in credentials)  # the target's own headers
        self._rng = random.Random()  # noqa: S311 - jitter only spreads retries in time
        self._session: aiohttp.ClientSession | None = None
        self._outgrown: list[aiohttp.ClientSession] = []  # opened under fewer slots: closed with the caller

    def _client(self) -> aiohttp.ClientSession:
        if (
            self._session is not None
            and self._session.connector is not None
            and self._session.connector.limit < self._gate.max_concurrency
        ):  # the gate was rebudgeted: calls in flight finish on the session they began on
            self._outgrown.append(self._session)
            self._session = None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=self._gate.max_concurrency,
                    resolver=self._resolver,
                    use_dns_cache=False,  # every new connection is re-checked by the SSRF guard
                ),
                cookie_jar=aiohttp.DummyCookieJar(),  # no state carried from key to key
                trust_env=False,  # no proxy or netrc from the environment
                headers={"User-Agent": _USER_AGENT},
            )
        return self._session

    async def aclose(self) -> None:
        for session in (*self._outgrown, self._session):
            if session is not None:
                await session.close()
        self._session, self._outgrown = None, []

    async def call(self, request: RenderedRequest, *, max_bytes: int, ticket: Ticket = "call") -> HttpResult:
        """Send ``request`` with retries; ``ticket`` is the breaker admission of its fetch."""
        policy = self._execution.retries
        deadline = _Deadline(self._execution.total_timeout_s)
        attempt, last = 0, None
        while True:
            result = await self._attempt(request, deadline, max_bytes, ticket)
            if result.attempts == 0:
                return last or result  # not sent this time: the last real answer is the evidence
            attempt += 1
            result = replace(result, attempts=attempt)
            if result.error_class not in policy.retry_on or attempt > policy.max:
                return result
            delay = retry_delay(attempt, policy, self._rng, result.retry_after_s)
            if delay >= deadline.remaining():
                return result  # another attempt cannot finish inside the total timeout
            await asyncio.sleep(delay)
            last = result

    async def _attempt(
        self, request: RenderedRequest, deadline: _Deadline, max_bytes: int, ticket: Ticket
    ) -> HttpResult:
        queued = time.monotonic()
        async with self._gate.semaphore:
            # A call must never time out before it was sent, or the evidence blames the target.
            deadline.extend(time.monotonic() - queued)
            if not await self._admitted(deadline):
                return _not_sent(
                    "http_429",
                    "the target asked callers to wait (Retry-After) past this call's total timeout;"
                    " it was not sent",
                )
            if ticket == "call" and self._breaker.state != "closed":
                return _not_sent(
                    "circuit_open", "circuit breaker opened while this call was queued; it was not sent"
                )
            timeout = aiohttp.ClientTimeout(
                total=max(deadline.remaining(), 0.001),  # 0 would mean no timeout at all
                sock_connect=self._execution.connect_timeout_s,
                sock_read=self._execution.read_timeout_s,
            )
            started = time.perf_counter()
            try:
                result = await self._send(request, timeout, max_bytes, started)
            except TimeoutError as exc:  # before ClientError: ServerTimeoutError is both
                return _failed("timeout", f"{type(exc).__name__} during the call", started)
            except aiohttp.ClientError as exc:  # the type only: a message can embed the URL (the key)
                return _failed("connect_error", type(exc).__name__, started)
            except ValueError as exc:  # a response the client library cannot interpret
                detail = f"response could not be interpreted ({type(exc).__name__})"
                return _failed("connect_error", detail, started)
            if result.retry_after_s:
                self._gate.not_before = max(self._gate.not_before, time.monotonic() + result.retry_after_s)
            return result

    async def _admitted(self, deadline: _Deadline) -> bool:
        """Wait out the target's Retry-After pause (it counts against the total timeout), then take
        a rate token (it does not). False: the pause outlasts the call, which is not sent."""
        while True:
            pause = self._gate.not_before - time.monotonic()
            if pause > 0:
                if pause >= deadline.remaining():
                    return False
                await asyncio.sleep(pause)
                continue
            queued = time.monotonic()
            await self._gate.limiter.acquire()
            deadline.extend(time.monotonic() - queued)
            if self._gate.not_before <= time.monotonic():
                return True
            # A Retry-After arrived while we queued: that token is spent; wait like everyone else.

    async def _send(
        self, request: RenderedRequest, timeout: aiohttp.ClientTimeout, max_bytes: int, started: float
    ) -> HttpResult:
        async with self._client().request(
            request.method,
            request.url,
            headers=request.headers,
            data=request.body,
            allow_redirects=False,  # a 3xx is evidence; following it could leave the origin
            timeout=timeout,
        ) as response:
            status = response.status
            error_class = _status_class(status)
            detail = f"HTTP {status}" if error_class else None
            headers = record_headers(response.headers.items())
            for name in self._credentials & headers.keys():  # echoed by the target: never evidence
                del headers[name]
            if error_class == "auth_error":
                return HttpResult(status, headers, None, error_class, detail, latency_ms=_ms(started))
            body = await _read_capped(response, max_bytes)
            if body is None and error_class is None:  # an oversized 429/5xx stays retryable
                detail = f"response body exceeds {max_bytes} bytes"
                return HttpResult(status, headers, None, "resource_limit", detail, latency_ms=_ms(started))
            retry_after = parse_retry_after(response.headers.get("Retry-After")) if error_class else None
            return HttpResult(
                status, headers, body, error_class, detail, latency_ms=_ms(started), retry_after_s=retry_after
            )
