"""The connector contract: turn ``(key, side)`` into one ``Observation``, never judging it.

Black-box: a connector knows an endpoint and its declared behaviour, never how it is built.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from deltasleuth.connectors.breaker import CircuitBreaker, Ticket
from deltasleuth.core.types import Observation, SideConfig

# Failures that describe the target's health; only these feed the circuit breaker.
TRANSPORT_ERRORS = frozenset({"connect_error", "timeout", "http_429", "http_5xx"})

# Response headers that may carry credentials or session state: never recorded as evidence.
DROPPED_RESPONSE_HEADERS = frozenset(
    {"set-cookie", "set-cookie2", "cookie", "authorization", "proxy-authorization"}
    | {"www-authenticate", "proxy-authenticate"}
)


class ConnectorError(Exception):
    """Base class of typed connector failures (never raised for an ordinary HTTP status)."""


class ConnectorConfigError(ConnectorError):
    """The target, request template or pagination settings cannot be executed."""


class KeyRejectedError(ConnectorError, ValueError):
    """The key cannot be sent safely (empty, ``.`` or ``..``); no call was made."""


class UnsafeTargetError(ConnectorError):
    """A request would leave the registered origin or reach a forbidden network (SSRF guard)."""


class NotCapturedError(ConnectorError, LookupError):
    """A file source holds no sample for this key and side (the caller marks it ``skipped``)."""


@runtime_checkable
class Connector(Protocol):
    async def fetch(
        self, key: str, side_cfg: SideConfig, *, run_id: str = "p0", call_seq: int = 0
    ) -> Observation:
        """The evidence for ``key``: statuses as-is, ``error_class`` only per ``ERROR_CLASSES``."""
        ...

    async def aclose(self) -> None: ...


async def guarded[T](
    breaker: CircuitBreaker,
    refused: Callable[[], T],
    read: Callable[[Ticket], Awaitable[T]],
    failed: Callable[[T], bool | None],
) -> T:
    """``read`` under the target's breaker: ``refused()`` while it is open (no call is made);
    ``failed(result)`` is the read's health signal, None when nothing reached the target."""
    ticket = breaker.admit()
    if ticket is None:
        return refused()
    try:
        result = await read(ticket)
    except BaseException:
        breaker.release(ticket)
        raise
    if (signal := failed(result)) is None:
        breaker.release(ticket)
    else:
        breaker.record(ticket, signal)
    return result


def circuit_open(observation: Callable[..., Observation]) -> Observation:
    """The observation of a call the open breaker refused."""
    detail = "circuit breaker is open for this target; no call was made"
    return observation(
        http_status=None, body=None, error_class="circuit_open", error_detail=detail, attempts=0, pages=0
    )


def utc_iso(moment: datetime) -> str:
    # One fixed text form for live and file observations: observed_at is part of obs_id.
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_now_iso() -> str:
    return utc_iso(datetime.now(UTC))


def record_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Headers as evidence: lower-cased names, repeated fields joined, credentials dropped."""
    out: dict[str, str] = {}
    for name, value in headers:
        name = name.lower()
        if name not in DROPPED_RESPONSE_HEADERS:
            out[name] = f"{out[name]}, {value}" if name in out else value
    return out
