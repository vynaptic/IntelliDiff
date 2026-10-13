"""Capability probe: do the declared capabilities the agent relies on work on this endpoint?

A plain baseline request must work first; otherwise the probe stops with ``ProbeAborted`` instead
of blaming every feature. Then a tool round trip (``tool_calling``), and from all replies whether
cached-token counts are reported (``prompt_caching``). Evidence is structural, never prompt or reply
text, and reports carry no timestamps.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from deltasleuth.llm.client import LLMClient
from deltasleuth.llm.model import (
    AuthFailed,
    BadRequest,
    BudgetExceeded,
    CapabilityUnavailable,
    DataClass,
    EgressDenied,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolResultPart,
    ToolSpec,
)
from deltasleuth.llm.profiles import Capabilities

CheckStatus = Literal["pass", "fail", "unsupported"]
PROBE_NONCE = "ds-probe-7Q2K"  # fixed, so reports are reproducible
PROBE_MAX_OUTPUT_TOKENS = 2048  # room for adaptive thinking before a tool call
READY = "Reply with the single word: ready."

_SYSTEM = Message.system(
    "You are a conformance probe for an API client. Follow the user's instructions exactly."
)
VALUE_TOOL = ToolSpec(
    "get_probe_value",
    "Look up a probe value by key.",
    {"type": "object", "additionalProperties": False, "properties": {"key": {"type": "string"}},
     "required": ["key"]},
)  # fmt: skip


class ProbeAborted(LLMError):
    """No verdict could mean anything: the baseline failed, or a check was refused locally."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityReport:
    profile_id: str
    dialect: str
    endpoint: str
    model_configured: str
    model_reported: str | None
    adapter_version: str
    results: tuple[CheckResult, ...]
    declared: Capabilities
    probed: Capabilities
    effective: Capabilities

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=indent, ensure_ascii=False)


async def probe_capabilities(client: LLMClient) -> CapabilityReport:
    """Run every check against ``client``'s endpoint. Egress, budget and authentication failures
    abort with their own error; nothing else could be learned."""
    responses: list[LLMResponse] = []

    async def call(request: LLMRequest) -> LLMResponse:
        # Sized to the declared limits, so a small model is probed, not refused.
        caps = client.effective
        room = [request.max_output_tokens, caps.max_output or request.max_output_tokens]
        if caps.max_context is not None:
            room.append(caps.max_context - client.estimate_input_tokens(request))
        response = await client.complete(replace(request, max_output_tokens=max(min(room), 1)))
        responses.append(response)
        return response

    try:
        await call(_request(READY))
    except (EgressDenied, BudgetExceeded, AuthFailed):
        raise
    except LLMError as exc:
        if isinstance(exc, BadRequest) and exc.status is None:
            raise _refused_locally("the baseline request", exc) from exc
        raise ProbeAborted(
            "the baseline request (plain text, no optional features) failed, so no feature was "
            "probed; check base_url and model, and for a strict OpenAI-compatible gateway the "
            "profile's wire options",
            error_class=exc.error_class,
            status=exc.status,
            provider_code=exc.provider_code,
            request_id=exc.request_id,
        ) from exc
    results = (await _tool_roundtrip(call), _usage_cached_tokens(responses))
    passed = {r.name for r in results if r.status == "pass"}
    profile, expressible = client.profile, client.adapter.expressible
    probed = Capabilities(
        tool_calling="tool_roundtrip" in passed,
        prompt_caching=expressible.prompt_caching if "usage_cached_tokens" in passed else "none",
    )
    return CapabilityReport(
        profile_id=profile.profile_id,
        dialect=profile.dialect,
        endpoint=profile.base_url,
        model_configured=profile.model,
        model_reported=next((r.model_reported for r in responses if r.model_reported), None),
        adapter_version=client.adapter.version,
        results=results,
        declared=profile.capabilities,
        probed=probed,
        effective=client.effective.intersect(probed),
    )


async def _tool_roundtrip(call: Callable[[LLMRequest], Awaitable[LLMResponse]]) -> CheckResult:
    ask = _request(
        'Call the get_probe_value tool with key "alpha". When you have the tool result, '
        "reply with exactly the value it returned and nothing else.",
        tools=(VALUE_TOOL,),
    )
    try:
        first = await call(ask)
        calls = [c for c in first.tool_calls if c.name == VALUE_TOOL.name]
        if not calls:
            evidence = (f"call 1: no get_probe_value call (stop={first.stop_reason})",)
            return CheckResult("tool_roundtrip", "fail", evidence)
        evidence = [
            f"call 1: stop={first.stop_reason}, get_probe_value called, "
            f"arguments_valid={calls[0].arguments_valid}"
        ]
        results = Message(Role.TOOL, tuple(ToolResultPart(c.id, PROBE_NONCE) for c in first.tool_calls))
        second = await call(replace(ask, messages=(*ask.messages, first.message, results)))
    except (EgressDenied, BudgetExceeded, AuthFailed):
        raise
    except CapabilityUnavailable as exc:  # not declared, or not expressible in the dialect
        return CheckResult("tool_roundtrip", "unsupported", (f"not available: {exc.detail}",))
    except LLMError as exc:
        if isinstance(exc, BadRequest) and exc.status is None:
            raise _refused_locally("check tool_roundtrip", exc) from exc
        status: CheckStatus = "unsupported" if _feature_rejected(exc) else "fail"
        return CheckResult("tool_roundtrip", status, (f"error: {_describe(exc)}",))
    if first.message.reasoning:
        evidence.append(f"reasoning parts carried back verbatim: {len(first.message.reasoning)}")
    echoed = PROBE_NONCE in second.text
    evidence.append(f"call 2: stop={second.stop_reason}, reply echoes the tool result: {echoed}")
    return CheckResult("tool_roundtrip", _verdict(echoed and not second.tool_calls), tuple(evidence))


def _usage_cached_tokens(responses: list[LLMResponse]) -> CheckResult:
    """A cache hit needs a long shared prefix, so this checks that counts are reported."""
    reported = [r.usage for r in responses if r.usage.reported]
    cached = [u for u in reported if u.cache_reported]
    evidence = [f"usage reported on {len(reported)}/{len(responses)} responses"]
    if cached:
        evidence.append(f"cached-token counts present (max cache_read={max(u.cache_read for u in cached)})")
    else:
        evidence.append("no cached-token counts in any response")
    if inconsistent := sum(not u.consistent for u in reported):
        evidence.append(f"usage counts inconsistent on {inconsistent}/{len(reported)} (resolved upward)")
    ok = bool(responses) and len(reported) == len(responses) and bool(cached)
    return CheckResult("usage_cached_tokens", _verdict(ok), tuple(evidence))


def _request(text: str, **fields: Any) -> LLMRequest:
    return LLMRequest(
        (_SYSTEM, Message.user(text)),
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        data_classes=frozenset({DataClass.SYNTHETIC}),
        **fields,
    )


def _verdict(ok: bool) -> CheckStatus:
    return "pass" if ok else "fail"


def _refused_locally(what: str, exc: LLMError) -> ProbeAborted:
    """A request refused before any I/O (the declared max_output or max_context) says nothing
    about the endpoint, so it is never a verdict."""
    return ProbeAborted(f"{what} was refused before sending ({exc.detail})", error_class=exc.error_class)


def _feature_rejected(exc: LLMError) -> bool:
    """With the baseline accepted, a 400 (or a validating gateway's 422) rejects the feature."""
    return exc.error_class == "BadRequest" and exc.status in (400, 422)


def _describe(exc: LLMError) -> str:
    parts = [exc.error_class]
    parts += [f"{k}={v}" for k, v in (("status", exc.status), ("code", exc.provider_code)) if v is not None]
    return " ".join(parts)
