"""``anthropic_messages``: the native Messages API through the official ``anthropic`` SDK.

Thinking and any block this port does not interpret become a ``ReasoningPart`` holding the block
verbatim, sent back in place next turn (signed thinking must round-trip unmodified). An empty
assistant turn is left out of the next request, which the API would reject.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import anthropic
from anthropic.types import Message as WireMessage

from deltasleuth.llm.adapters.base import (
    PLACEHOLDER_CREDENTIAL,
    Credential,
    http_client,
    neutralise_sdk_environment,
    sdk_error,
)
from deltasleuth.llm.model import (
    CapabilityUnavailable,
    LLMRequest,
    LLMResponse,
    Message,
    Part,
    ProviderError,
    ReasoningPart,
    Role,
    StopReason,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    safe_code,
)
from deltasleuth.llm.profiles import Capabilities, LLMProfile

DIALECT = "anthropic_messages"
_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
}


class Adapter:
    dialect: ClassVar[str] = DIALECT
    version: ClassVar[str] = f"anthropic_messages/1+anthropic-{anthropic.__version__}"
    expressible: ClassVar[Capabilities] = Capabilities(tool_calling=True, prompt_caching="explicit")

    def __init__(self, profile: LLMProfile, credential: Credential | None) -> None:
        self._model = profile.model
        # An explicit key also stops the SDK's profile and federation discovery chain.
        self._sdk = anthropic.AsyncAnthropic(
            api_key=PLACEHOLDER_CREDENTIAL,
            webhook_key="",
            base_url=profile.base_url,
            timeout=profile.limits.timeout_s,
            max_retries=0,
            http_client=http_client(profile, credential),
        )
        neutralise_sdk_environment(self._sdk, logger_name="anthropic")

    async def send(self, request: LLMRequest, *, caching: bool) -> LLMResponse:
        body = to_wire(request, model=self._model, caching=caching)
        try:
            message = await self._sdk.messages.create(**body)
        except anthropic.APIError as exc:
            overflow = _context_overflow(exc)
            raise sdk_error(
                exc, anthropic, code=getattr(exc, "type", None), context_overflow=overflow
            ) from None
        try:  # the SDK does not validate a 200 body (an HTML page, an error envelope)
            return from_wire(message)
        except (AttributeError, TypeError, ValueError):  # not chained: the cause may quote the body
            raise ProviderError("response did not match the Messages API schema", status=200) from None

    async def aclose(self) -> None:
        await self._sdk.close()


def _context_overflow(exc: anthropic.APIError) -> bool:
    """Reads the provider message only to recognise an overflow; it is never copied."""
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    too_long = isinstance(message, str) and message.lower().startswith("prompt is too long")
    return too_long and getattr(exc, "status_code", None) == 400


def to_wire(request: LLMRequest, *, model: str, caching: bool) -> dict[str, Any]:
    messages = request.messages
    lead = next((i for i, m in enumerate(messages) if m.role is not Role.SYSTEM), len(messages))
    if any(m.role is Role.SYSTEM for m in messages[lead:]):
        # Hoisting a later system message into ``system`` would change its meaning.
        raise CapabilityUnavailable("anthropic_messages supports system messages only at the start")
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_output_tokens,
        "messages": _merge_turns([_turn(m) for m in messages[lead:]]),
    }
    if lead:
        body["system"] = [{"type": "text", "text": p.text} for m in messages[:lead] for p in m.parts]
    if request.tools:
        body["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": dict(t.input_schema)}
            for t in request.tools
        ]
        auto = {"type": "auto", "disable_parallel_tool_use": True}
        body["tool_choice"] = auto if request.tool_choice == "auto" else {"type": "none"}
    if caching:
        body["cache_control"] = {"type": "ephemeral"}  # caches the last cacheable block
    return body


def _turn(message: Message) -> dict[str, Any]:
    role = "assistant" if message.role is Role.ASSISTANT else "user"  # tool results travel as user
    return {"role": role, "content": [b for p in message.parts if (b := _block(p)) is not None]}


def _block(part: Part) -> dict[str, Any] | None:
    if isinstance(part, ToolResultPart):
        block = {"type": "tool_result", "tool_use_id": part.tool_call_id, "content": part.content}
        return block | ({"is_error": True} if part.is_error else {})
    if isinstance(part, ToolCallPart):
        return {"type": "tool_use", "id": part.id, "name": part.name, "input": dict(part.arguments)}
    if isinstance(part, ReasoningPart):
        return dict(part.state) if part.provider == DIALECT else None
    return {"type": "text", "text": part.text} if part.text else None  # empty text blocks are rejected


def _merge_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty turns, merge consecutive same-role ones, and put tool results first in a user
    turn, as the API requires right after a ``tool_use``."""
    merged: list[dict[str, Any]] = []
    for turn in (t for t in turns if t["content"]):
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] += turn["content"]
        else:
            merged.append(turn)
    for turn in merged:
        if turn["role"] == "user":
            turn["content"].sort(key=lambda b: b["type"] != "tool_result")
    return merged


def from_wire(message: Any) -> LLMResponse:
    if not isinstance(message, WireMessage):
        raise ValueError("not a Messages API message")
    parts: list[Part] = []
    for block in message.content:
        if block.type == "text":
            parts.append(TextPart(block.text or ""))
        elif block.type == "tool_use":
            parts.append(_tool_call(block))
        else:
            parts.append(ReasoningPart(DIALECT, block.to_dict()))
    stop = safe_code(message.stop_reason)
    return LLMResponse(
        message=Message(Role.ASSISTANT, tuple(parts) or (TextPart(""),)),
        stop_reason=_STOP.get(stop or "", "other"),
        usage=_usage(message.usage),
        model_reported=message.model,
        response_id=message.id,
        provider_stop=stop,
    )


def _tool_call(block: Any) -> ToolCallPart:
    """A non-strict gateway may send ``input`` as a string or another JSON value: kept as
    ``raw_arguments`` for the repair step."""
    value = block.input
    if value is None or isinstance(value, dict):
        return ToolCallPart(block.id, block.name, dict(value or {}))
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ToolCallPart(block.id, block.name, {}, raw_arguments=raw)


def _usage(raw: Any) -> Usage:
    """Anthropic's ``input_tokens`` excludes cache reads and writes; ``input`` adds them. Counts
    that disagree are resolved upward and flagged: writes the TTL breakdown leaves unattributed
    are billed as 1-hour writes (the higher rate)."""
    if raw is None:
        return Usage(reported=False)
    breakdown = raw.cache_creation
    split = (
        (breakdown.ephemeral_5m_input_tokens or 0, breakdown.ephemeral_1h_input_tokens or 0)
        if breakdown is not None
        else (raw.cache_creation_input_tokens or 0, 0)  # the default TTL is 5 minutes
    )
    counts = (
        raw.input_tokens or 0,
        raw.cache_read_input_tokens or 0,
        raw.cache_creation_input_tokens or 0,
        raw.output_tokens or 0,
        getattr(raw.output_tokens_details, "thinking_tokens", None) or 0,
        *split,
    )
    uncached, read, written, output, reasoning, w5m, w1h = (max(c, 0) for c in counts)
    consistent = min(counts) >= 0 and w5m + w1h == written and reasoning <= output
    w1h += max(written - w5m - w1h, 0)
    return Usage(
        input=uncached + read + w5m + w1h,
        output=max(output, reasoning),
        cache_read=read,
        cache_write_5m=w5m,
        cache_write_1h=w1h,
        reasoning=reasoning,
        cache_reported=raw.cache_read_input_tokens is not None or raw.cache_creation_input_tokens is not None,
        consistent=consistent,
    )
