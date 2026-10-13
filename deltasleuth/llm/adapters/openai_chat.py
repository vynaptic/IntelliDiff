"""``openai_chat``: Chat Completions through the official ``openai`` SDK (OpenAI, vLLM and any
OpenAI-compatible gateway).

``store`` and ``parallel_tool_calls`` are sent as false unless the profile's ``wire`` options say
the (internal) endpoint rejects them. Tool results have no error flag here, so an error result is
prefixed ``ERROR: ``.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import openai
from openai.types.chat import ChatCompletion

from deltasleuth.llm.adapters.base import (
    PLACEHOLDER_CREDENTIAL,
    Credential,
    http_client,
    neutralise_sdk_environment,
    sdk_error,
)
from deltasleuth.llm.model import (
    LLMRequest,
    LLMResponse,
    Message,
    Part,
    ProviderError,
    Role,
    StopReason,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
    safe_code,
)
from deltasleuth.llm.profiles import Capabilities, LLMProfile, WireOptions

_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}
_CONTEXT_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})
OPENAI_WIRE = WireOptions()  # what the OpenAI API itself accepts


class Adapter:
    dialect: ClassVar[str] = "openai_chat"
    version: ClassVar[str] = f"openai_chat/1+openai-{openai.__version__}"
    expressible: ClassVar[Capabilities] = Capabilities(tool_calling=True, prompt_caching="automatic")

    def __init__(self, profile: LLMProfile, credential: Credential | None) -> None:
        self._model = profile.model
        self._wire = profile.wire
        # Every value the constructor would otherwise read from OPENAI_* is given here.
        self._sdk = openai.AsyncOpenAI(
            api_key=PLACEHOLDER_CREDENTIAL,
            admin_api_key="",
            organization="",
            project="",
            webhook_secret="",
            base_url=profile.base_url,
            timeout=profile.limits.timeout_s,
            max_retries=0,
            http_client=http_client(profile, credential),
        )
        neutralise_sdk_environment(self._sdk, logger_name="openai")

    async def send(self, request: LLMRequest, *, caching: bool) -> LLMResponse:  # caching is automatic
        body = to_wire(request, model=self._model, wire=self._wire)
        try:
            completion = await self._sdk.chat.completions.create(**body)
        except openai.APIError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "type", None)
            raise sdk_error(exc, openai, code=code, context_overflow=code in _CONTEXT_CODES) from None
        try:  # the SDK does not validate a 200 body; a proxy's HTML page comes back as a str
            return from_wire(completion)
        except (AttributeError, TypeError, ValueError):  # not chained: the cause may quote the body
            raise ProviderError("response did not match the Chat Completions schema", status=200) from None

    async def aclose(self) -> None:
        await self._sdk.close()


def to_wire(request: LLMRequest, *, model: str, wire: WireOptions = OPENAI_WIRE) -> dict[str, Any]:
    """Chat Completions arguments for ``request``."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [item for m in request.messages for item in _messages(m)],
        wire.max_tokens_param: request.max_output_tokens,
    }
    if wire.send_store:
        body["store"] = False
    if request.tools:
        body["tools"] = [_tool(t) for t in request.tools]
        body["tool_choice"] = request.tool_choice
        if wire.send_parallel_tool_calls:
            body["parallel_tool_calls"] = False
    return body


def _messages(message: Message) -> list[dict[str, Any]]:
    if message.role is Role.TOOL:
        return [
            {
                "role": "tool",
                "tool_call_id": p.tool_call_id,
                "content": f"ERROR: {p.content}" if p.is_error else p.content,
            }
            for p in message.parts
            if isinstance(p, ToolResultPart)
        ]
    if message.role is Role.ASSISTANT:
        calls = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.raw_arguments or _dumps(c.arguments)},
            }
            for c in message.tool_calls
        ]
        # Reasoning from another dialect is ignored; a turn made only of it becomes empty text.
        wire: dict[str, Any] = {"role": "assistant", "content": message.text or (None if calls else "")}
        return [wire | {"tool_calls": calls}] if calls else [wire]
    texts = [p.text for p in message.parts if isinstance(p, TextPart)]
    content = texts[0] if len(texts) == 1 else [{"type": "text", "text": t} for t in texts]
    return [{"role": message.role.value, "content": content}]


def _tool(spec: ToolSpec) -> dict[str, Any]:
    function = {"name": spec.name, "description": spec.description, "parameters": dict(spec.input_schema)}
    return {"type": "function", "function": function}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_wire(completion: Any) -> LLMResponse:
    if not isinstance(completion, ChatCompletion) or not completion.choices:
        raise ValueError("not a chat completion")
    choice = completion.choices[0]
    msg = choice.message
    parts: list[Part] = [TextPart(t) for t in (msg.content, msg.refusal) if t]
    # Lenient gateways omit a call's type; this port offers function tools only.
    calls = [c for c in msg.tool_calls or () if c.type == "function" or (c.type is None and _has_function(c))]
    parts += [_tool_call(c) for c in calls]
    finish = safe_code(choice.finish_reason)
    return LLMResponse(
        message=Message(Role.ASSISTANT, tuple(parts) or (TextPart(""),)),
        stop_reason=_stop(finish, refused=bool(msg.refusal), has_calls=bool(calls)),
        usage=_usage(completion.usage),
        model_reported=completion.model,
        response_id=completion.id,
        provider_stop=finish,
        ignored_tool_calls=len(msg.tool_calls or ()) - len(calls),
    )


def _has_function(call: Any) -> bool:
    function = getattr(call, "function", None)
    return function is not None and bool(function.name)


def _stop(finish: str | None, *, refused: bool, has_calls: bool) -> StopReason:
    """Some compatible servers report ``stop`` with tool calls: calls win unless the reply was cut
    off or filtered."""
    if refused:
        return "refusal"
    stop = _STOP.get(finish or "", "other")
    return "tool_use" if has_calls and stop in ("end_turn", "other") else stop


def _tool_call(call: Any) -> ToolCallPart:
    text = call.function.arguments or ""
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return ToolCallPart(call.id, call.function.name, parsed)
    return ToolCallPart(call.id, call.function.name, {}, raw_arguments=text)


def _usage(raw: Any) -> Usage:
    """``prompt_tokens`` includes cached tokens. Counts that disagree (a gateway in front of a
    Messages-style backend leaves cache reads out) are resolved upward, so cost and budgets are
    never under-counted, and flagged."""
    if raw is None:
        return Usage(reported=False)
    details, out_details = raw.prompt_tokens_details, raw.completion_tokens_details
    cached = getattr(details, "cached_tokens", None)
    counts = (
        raw.prompt_tokens or 0,
        raw.completion_tokens or 0,
        cached or 0,
        getattr(details, "cache_write_tokens", None) or 0,
        getattr(out_details, "reasoning_tokens", None) or 0,
    )
    prompt, output, cache_read, cache_write, reasoning = (max(c, 0) for c in counts)
    return Usage(
        input=max(prompt, cache_read + cache_write),
        output=max(output, reasoning),
        cache_read=cache_read,
        cache_write_5m=cache_write,
        reasoning=reasoning,
        cache_reported=cached is not None,
        consistent=min(counts) >= 0 and cache_read + cache_write <= prompt and reasoning <= output,
    )
