"""What every wire adapter provides, and the credential-safe transport the SDKs share.

The official SDKs read ``*_API_KEY``, ``*_BASE_URL``, ``*_CUSTOM_HEADERS`` and ``*_LOG`` from
the environment. So an adapter passes every constructor value explicitly with a placeholder key,
gives the SDK the HTTP client built here (no ambient proxies, no redirects, an allow-list of
headers plus the real credential, which the SDK never holds) and clears what the SDK took from
the environment anyway. The credential canary test proves it.
"""

from __future__ import annotations

import email.utils
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import httpx2

from deltasleuth.core.secrets import SecretError
from deltasleuth.llm.model import (
    AuthFailed,
    BadRequest,
    ContextTooLong,
    LLMError,
    LLMRequest,
    LLMResponse,
    Overloaded,
    ProviderError,
    RateLimited,
)
from deltasleuth.llm.profiles import Capabilities, LLMProfile

# Lets the SDK build requests without reading a key from the environment; never leaves the process.
PLACEHOLDER_CREDENTIAL = "ds-llm-credential-is-set-by-the-transport"
_ALLOWED_HEADERS = frozenset(
    {"host", "accept", "accept-encoding", "connection", "content-type", "content-length", "user-agent"}
    | {"anthropic-version"}
)
_SDK_TELEMETRY_PREFIX = "x-stainless-"  # SDK name, version, retry count; no payload
_HEADER_VALUE = re.compile(r"^(?:[\x21-\x7e]+ )?[\x21-\x7e]+$")  # "[scheme ]key", printable ASCII


@dataclass(frozen=True)
class Credential:
    """The header that authenticates a profile and its full value. Checked here, whatever read the
    secret, so a bad key fails as a typed error naming the header, not deep in the HTTP stack."""

    header: str
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _HEADER_VALUE.fullmatch(self.value):
            raise SecretError(
                f"the credential for header {self.header} must be printable ASCII without whitespace"
            )

    @classmethod
    def for_profile(cls, profile: LLMProfile, secret: str) -> Credential:
        header, scheme = profile.auth
        return cls(header, f"{scheme} {secret}" if scheme else secret)


class ChatAdapter(Protocol):
    """One wire dialect; ``expressible`` is the most it can carry at all."""

    dialect: ClassVar[str]
    version: ClassVar[str]
    expressible: ClassVar[Capabilities]

    def __init__(self, profile: LLMProfile, credential: Credential | None) -> None: ...

    async def send(self, request: LLMRequest, *, caching: bool) -> LLMResponse:
        """Send one request; raise typed ``LLMError`` subclasses on failure."""
        ...

    async def aclose(self) -> None: ...


def http_client(profile: LLMProfile, credential: Credential | None) -> httpx2.AsyncClient:
    """The only HTTP client an adapter may give its SDK."""
    allowed = _ALLOWED_HEADERS | ({credential.header.lower()} if credential else set())

    async def enforce_headers(request: httpx2.Request) -> None:
        for name in {n.lower() for n in request.headers}:
            if name not in allowed and not name.startswith(_SDK_TELEMETRY_PREFIX):
                del request.headers[name]
        if credential is not None:
            request.headers[credential.header] = credential.value

    connections = profile.limits.max_concurrency
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(profile.limits.timeout_s),
        limits=httpx2.Limits(max_connections=connections, max_keepalive_connections=connections),
        trust_env=False,
        follow_redirects=False,  # a redirect would carry the credential to another host
        event_hooks={"request": [enforce_headers]},
    )


def neutralise_sdk_environment(sdk: Any, *, logger_name: str) -> None:
    """Drop the headers the SDK merged from ``*_CUSTOM_HEADERS`` (failing closed if the internal
    attribute moves) and keep its logger above DEBUG, whose records include request bodies."""
    if not isinstance(getattr(sdk, "_custom_headers", None), dict):
        raise RuntimeError(
            f"{type(sdk).__name__} no longer exposes _custom_headers; review credential hygiene"
        )
    sdk._custom_headers = {}
    logging.getLogger(logger_name).setLevel(logging.WARNING)


def sdk_error(exc: Exception, sdk: Any, *, code: str | None, context_overflow: bool) -> LLMError:
    """An official SDK exception as a typed port error. Provider messages may echo the request,
    so only status, code and request id travel."""
    if isinstance(exc, sdk.APITimeoutError):
        return ProviderError("request timed out", error_class="Timeout")
    if isinstance(exc, sdk.APIConnectionError):
        return ProviderError("connection failed", error_class="Connection")
    if not isinstance(exc, sdk.APIStatusError):
        return ProviderError("response did not match the wire schema")
    status = exc.status_code
    ctx: dict[str, Any] = {"status": status, "provider_code": code, "request_id": exc.request_id}
    if status == 429:
        return RateLimited("rate limited", retry_after=parse_retry_after(exc.response.headers), **ctx)
    if status in (503, 529):
        return Overloaded("provider overloaded", retry_after=parse_retry_after(exc.response.headers), **ctx)
    if status == 401:
        return AuthFailed("authentication failed", **ctx)
    if status == 403:
        return AuthFailed("permission denied", error_class="PermissionDenied", **ctx)
    if status == 413:
        return BadRequest("request too large", error_class="RequestTooLarge", **ctx)
    if context_overflow:
        return ContextTooLong("input exceeds the model context window", **ctx)
    if status >= 500:
        return ProviderError("provider error", **ctx)
    return BadRequest("request rejected", **ctx)


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds to wait from ``retry-after-ms``, ``retry-after`` seconds or an HTTP date."""
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(name)
        if value:
            try:
                delay = float(value) / scale
            except ValueError:
                continue
            if math.isfinite(delay):  # "nan" and "inf" are garbage too
                return max(delay, 0.0)
    try:
        when = email.utils.parsedate_to_datetime(headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None
    return max(when.timestamp() - time.time(), 0.0)
