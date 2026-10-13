"""``LLMClient``: the one way to call a configured model.

Before any network I/O a call passes the egress policy, is checked against the profile's effective
capabilities (declared ∩ what the dialect can express) and against the budgets. While in flight it
reserves its worst case (input estimate + ``max_output_tokens``), so concurrent calls cannot
overshoot, and a reply without usage is charged that worst case. Every attempt and every refusal
appends a payload-free row to ``call_log``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import stamina

from deltasleuth.core.secrets import read_secret
from deltasleuth.llm.adapters.base import ChatAdapter, Credential
from deltasleuth.llm.model import (
    BadRequest,
    BudgetExceeded,
    CapabilityUnavailable,
    ContextTooLong,
    EgressDenied,
    LLMError,
    LLMRequest,
    LLMResponse,
    Overloaded,
    ProviderError,
    RateLimited,
)
from deltasleuth.llm.policy import EgressPolicy, check_egress
from deltasleuth.llm.profiles import Budgets, Gate, LLMProfile
from deltasleuth.llm.usage import cost_micros

MAX_RETRY_AFTER_S = 30.0  # a longer Retry-After surfaces as RateLimited instead of stalling a turn
DEFAULT_BYTES_PER_TOKEN = 3.0  # leans high for prose and JSON until reported usage calibrates it
_CALIBRATION_BYTES = 8 * 1024  # below this, fixed per-request overhead distorts the ratio


@dataclass
class ClientCounters:
    calls: int = 0  # provider attempts, retries included
    errors: int = 0
    denied: int = 0  # refused before any network I/O
    input_tokens: int = 0  # as reported
    output_tokens: int = 0
    estimated_tokens: int = 0  # worst cases charged for replies without usage
    cache_read_tokens: int = 0
    tool_calls: int = 0
    cost_micros: int = 0


@dataclass
class TurnMeter:
    """Budget counters of one agent turn."""

    budgets: Budgets
    calls: int = 0
    input_tokens: int = 0  # reported, or the estimate when a reply had no usage
    reserved_input_tokens: int = 0  # estimates of calls still in flight
    tool_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def max_input_tokens(self) -> int:
        b = self.budgets
        return b.max_input_tokens_per_turn or (b.max_tool_calls + 1) * b.max_input_tokens_per_call

    @property
    def tool_calls_remaining(self) -> int:
        return max(self.budgets.max_tool_calls - self.tool_calls, 0)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started


class LLMClient:
    def __init__(
        self,
        profile: LLMProfile,
        adapter: ChatAdapter,
        *,
        egress: EgressPolicy,
        gate: Gate | None = None,
        retry_attempts: int = 3,
    ) -> None:
        self.profile = profile
        self.adapter = adapter
        self.egress = egress
        self.effective = profile.capabilities.intersect(adapter.expressible)
        self.counters = ClientCounters()
        self.call_log: list[dict[str, Any]] = []
        self.gate = gate or Gate(profile.limits)
        self._retry_attempts = retry_attempts
        self._reserved_tokens = 0  # worst cases of calls in flight
        self._densest: float | None = None  # fewest bytes per reported token seen

    @classmethod
    def from_profile(
        cls,
        profile: LLMProfile,
        *,
        egress: EgressPolicy,
        secret_reader: Callable[[str], str] = read_secret,
        gate: Gate | None = None,
        retry_attempts: int = 3,
    ) -> LLMClient:
        """The client for ``profile``, with the credential its ``auth_ref`` names; ``gate`` shares
        the profile's limits with other clients (by default this client has its own)."""
        # One adapter module per dialect, imported on first use so only the SDKs in use load.
        adapter = importlib.import_module(f"deltasleuth.llm.adapters.{profile.dialect}").Adapter
        secret = secret_reader(profile.auth_ref) if profile.auth_ref else None
        credential = Credential.for_profile(profile, secret) if secret is not None else None
        return cls(
            profile, adapter(profile, credential), egress=egress, gate=gate, retry_attempts=retry_attempts
        )

    def new_turn(self) -> TurnMeter:
        return TurnMeter(self.profile.budgets)

    async def aclose(self) -> None:
        await self.adapter.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def estimate_input_tokens(self, request: LLMRequest) -> int:
        """A pre-call estimate that leans high: 3 bytes per token until the reported usage of a
        large request shows denser text, then the densest ratio seen less a 10% margin."""
        return self._tokens(len(_canonical(request)))

    def _tokens(self, nbytes: int) -> int:
        per_token = DEFAULT_BYTES_PER_TOKEN if self._densest is None else max(self._densest / 1.1, 1.0)
        return math.ceil(nbytes / per_token)

    async def complete(self, request: LLMRequest, *, turn: TurnMeter | None = None) -> LLMResponse:
        row = self._row(request)
        try:
            check_egress(self.profile, request.data_classes, self.egress)
            if request.tools and not self.effective.tool_calling:
                raise CapabilityUnavailable("the profile has no effective tool calling")
            nbytes = len(_canonical(request))
            estimate = self._tokens(nbytes)
            row["estimated_input_tokens"] = estimate
            self._check_budgets(request, estimate, turn)
        except LLMError as exc:
            self._deny(row, exc)
            raise
        worst_case = estimate + request.max_output_tokens
        # Reserved before the first await, so a concurrent call checks against it.
        with self._reservation(worst_case, estimate, turn):
            response = await self._send(request, row)
            self._account(response, nbytes, worst_case, estimate, turn)
        return response

    def _check_budgets(self, request: LLMRequest, estimate: int, turn: TurnMeter | None) -> None:
        budgets, counters = self.profile.budgets, self.counters
        worst_case = estimate + request.max_output_tokens
        checks = [("max_input_tokens_per_call", budgets.max_input_tokens_per_call, estimate)]
        if budgets.per_run_tokens is not None:
            used = counters.input_tokens + counters.output_tokens + counters.estimated_tokens
            checks.append(
                ("per_run_tokens", budgets.per_run_tokens, used + self._reserved_tokens + worst_case)
            )
        if turn is not None:
            turn_input = turn.input_tokens + turn.reserved_input_tokens + estimate
            checks += [
                ("max_input_tokens_per_turn", turn.max_input_tokens, turn_input),
                ("max_tool_calls", budgets.max_tool_calls, turn.tool_calls),
                ("max_wall_s", budgets.max_wall_s, turn.elapsed_s),
            ]
        for name, limit, requested in checks:
            if requested > limit:
                raise BudgetExceeded(name, limit, requested)
        caps = self.effective
        if caps.max_output is not None and request.max_output_tokens > caps.max_output:
            raise BadRequest("max_output_tokens exceeds the profile's declared max_output")
        if caps.max_context is not None and worst_case > caps.max_context:
            raise ContextTooLong("estimated input plus max_output_tokens exceeds the declared max_context")

    @contextmanager
    def _reservation(self, run_tokens: int, turn_tokens: int, turn: TurnMeter | None) -> Iterator[None]:
        self._reserved_tokens += run_tokens
        if turn is not None:
            turn.reserved_input_tokens += turn_tokens
        try:
            yield
        finally:
            self._reserved_tokens -= run_tokens
            if turn is not None:
                turn.reserved_input_tokens -= turn_tokens

    async def _send(self, request: LLMRequest, row: dict[str, Any]) -> LLMResponse:
        caching = self.effective.prompt_caching != "none"
        async for attempt in stamina.retry_context(
            on=_retry_backoff,
            attempts=self._retry_attempts,
            timeout=self.profile.budgets.max_wall_s,
            wait_initial=0.5,
            wait_max=8.0,
        ):
            with attempt:
                return await self._attempt(request, row | {"attempt": attempt.num}, caching)
        raise AssertionError("unreachable: stamina re-raises the last error")  # pragma: no cover

    async def _attempt(self, request: LLMRequest, row: dict[str, Any], caching: bool) -> LLMResponse:
        async with self.gate.semaphore:
            if self.gate.limiter is not None:
                await self.gate.limiter.acquire()
            self.counters.calls += 1
            started = time.monotonic()
            try:
                response = await self.adapter.send(request, caching=caching)
            except Exception as exc:  # every attempt is logged, even an adapter bug
                self.counters.errors += 1
                err = exc if isinstance(exc, LLMError) else LLMError("unexpected adapter failure")
                failure = {
                    "error_class": err.error_class,
                    "http_status": err.status,
                    "provider_code": err.provider_code,
                }
                self.call_log.append(row | failure | {"latency_ms": _ms(started)})
                raise
        latency = _ms(started)
        self.call_log.append(row | self._outcome(response) | {"latency_ms": latency})
        return replace(response, latency_ms=float(latency))

    def _account(
        self, response: LLMResponse, nbytes: int, worst_case: int, estimate: int, turn: TurnMeter | None
    ) -> None:
        usage, counters, price = response.usage, self.counters, self.profile.pricing
        if usage.reported:
            counters.input_tokens += usage.input
            counters.output_tokens += usage.output
            counters.cache_read_tokens += usage.cache_read
            counters.cost_micros += cost_micros(usage, price) if price is not None else 0
            if nbytes >= _CALIBRATION_BYTES and usage.input > 0:
                self._densest = min(self._densest or math.inf, nbytes / usage.input)
        else:
            counters.estimated_tokens += worst_case  # fail-closed; estimates are never priced
        counters.tool_calls += len(response.tool_calls)
        if turn is not None:
            turn.calls += 1
            turn.input_tokens += usage.input if usage.reported else estimate
            turn.tool_calls += len(response.tool_calls)

    def _row(self, request: LLMRequest) -> dict[str, Any]:
        profile = self.profile
        return {
            "profile_id": profile.profile_id,
            "dialect": profile.dialect,
            "trust": profile.trust.value,
            "host": profile.host,
            "model_configured": profile.model,
            "data_classes": sorted(c.value for c in request.data_classes),
            "request_hash": hashlib.sha256(_canonical(request)).hexdigest(),
        }

    def _outcome(self, response: LLMResponse) -> dict[str, Any]:
        usage, price = response.usage, self.profile.pricing
        row = {
            "model_reported": response.model_reported,
            "response_id": response.response_id,
            "stop": response.stop_reason,
            "provider_stop": response.provider_stop,
            "ignored_tool_calls": response.ignored_tool_calls,
        } | {f"usage_{name}": value for name, value in asdict(usage).items()}
        if usage.reported and price is not None:
            row |= {
                "cost_micros": cost_micros(usage, price),
                "currency": price.currency,
                "price_version": price.version,
            }
        return row

    def _deny(self, row: dict[str, Any], exc: LLMError) -> None:
        self.counters.denied += 1
        if isinstance(exc, EgressDenied):
            reason = exc.reason
        elif isinstance(exc, BudgetExceeded):
            reason = f"budget:{exc.budget}"
        else:
            reason = f"local:{exc.error_class}"
        self.call_log.append(row | {"error_class": exc.error_class, "denial_reason": reason})


def _canonical(request: LLMRequest) -> bytes:
    """Deterministic bytes of everything a request asks (its data classes are never sent): the
    basis of the size estimate and the audit ``request_hash``."""
    doc = asdict(request)
    del doc["data_classes"]
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _retry_backoff(exc: Exception) -> bool | float:
    """Retry rate limits and overloads (honouring a bounded Retry-After, never exhausted quota)
    and transient provider or transport failures."""
    if isinstance(exc, RateLimited | Overloaded):
        if exc.provider_code == "insufficient_quota":
            return False
        if exc.retry_after is None:
            return True
        return exc.retry_after if exc.retry_after <= MAX_RETRY_AFTER_S else False
    return isinstance(exc, ProviderError)


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
