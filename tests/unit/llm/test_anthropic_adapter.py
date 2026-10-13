"""anthropic_messages: canonical <-> Messages wire, thinking round-trip, usage and typed errors."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from anthropic._models import construct_type
from anthropic.types import Message as WireMessage

from deltasleuth.llm.adapters.anthropic_messages import from_wire, to_wire
from deltasleuth.llm.model import (
    AuthFailed,
    BadRequest,
    CapabilityUnavailable,
    ContextTooLong,
    LLMRequest,
    Message,
    Overloaded,
    ProviderError,
    RateLimited,
    ReasoningPart,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)

from .conftest import SECRET_VALUE, req

SCHEMA = {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
LOOKUP = ToolSpec("lookup", "Look up a value.", SCHEMA)
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
TEXT = {"type": "text", "text": "x"}


def _from(content: list[dict[str, Any]], stop: str = "end_turn", **fields: Any) -> Any:
    raw = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5"}
    return from_wire(
        construct_type(type_=WireMessage, value=raw | {"content": content, "stop_reason": stop} | fields)
    )


def _to(*messages: Message, caching: bool = False, **kwargs: Any) -> dict[str, Any]:
    return to_wire(LLMRequest(messages=messages, **kwargs), model="claude-opus-5", caching=caching)


def test_messages_to_wire() -> None:
    body = _to(
        Message.system("one"),
        Message.system("two"),
        Message.user("hi"),
        Message(
            Role.ASSISTANT,
            (
                ReasoningPart("anthropic_messages", THINKING),
                TextPart(""),  # the API rejects empty text blocks
                TextPart("calling"),
                ReasoningPart("openai_chat", {"type": "other"}),  # another dialect's state is not sent
                ToolCallPart("toolu_1", "lookup", {"key": "alpha"}),
            ),
        ),
        Message.user("and then?"),
        Message(
            Role.TOOL, (ToolResultPart("toolu_1", "42"), ToolResultPart("toolu_2", "bad", is_error=True))
        ),
    )
    assert body["system"] == [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                THINKING,
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"key": "alpha"}},
            ],
        },
        {  # merged, with the tool results opening the turn
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"},
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "bad", "is_error": True},
                {"type": "text", "text": "and then?"},
            ],
        },
    ]
    assert (body["model"], body["max_tokens"]) == ("claude-opus-5", 4096)
    assert not {"tools", "tool_choice", "cache_control"} & set(body)


def test_empty_assistant_reply_is_left_out_of_the_next_request() -> None:
    reply = _from([]).message  # Claude may end a turn with no blocks
    assert reply.parts == (TextPart(""),)
    assert _to(Message.user("hi"), reply, Message.user("please answer"))["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "please answer"}],
        }
    ]


def test_unexpressible_requests_are_refused() -> None:
    with pytest.raises(CapabilityUnavailable):
        _to(Message.user("a"), Message.system("late"))  # hoisting would change its meaning


@pytest.mark.parametrize(
    ("choice", "wire"),
    [("auto", {"type": "auto", "disable_parallel_tool_use": True}), ("none", {"type": "none"})],
)
def test_tools_to_wire(choice: Any, wire: dict[str, Any]) -> None:
    body = _to(Message.user("x"), tools=(LOOKUP,), tool_choice=choice)
    assert body["tool_choice"] == wire
    assert body["tools"] == [{"name": "lookup", "description": "Look up a value.", "input_schema": SCHEMA}]


def test_caching() -> None:
    assert _to(Message.user("x"), caching=True)["cache_control"] == {"type": "ephemeral"}


def test_blocks_from_wire() -> None:
    redacted = {"type": "redacted_thinking", "data": "opaque=="}
    future = {"type": "fancy_new_block", "payload": {"a": 1}}
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"key": "a"}}
    response = _from([THINKING, redacted, {"type": "text", "text": "hi"}, tool_use, future], stop="tool_use")
    assert [p.state for p in response.message.reasoning] == [THINKING, redacted, future]  # verbatim
    assert (response.text, response.stop_reason) == ("hi", "tool_use")
    assert response.tool_calls[0].arguments == {"key": "a"}


@pytest.mark.parametrize(("value", "raw"), [('{"key": "a"}', '{"key": "a"}'), (["a"], '["a"]'), (7, "7")])
def test_non_object_tool_input_is_kept_for_repair(value: Any, raw: str) -> None:
    (call,) = _from(
        [{"type": "tool_use", "id": "t1", "name": "lookup", "input": value}], stop="tool_use"
    ).tool_calls
    assert (dict(call.arguments), call.raw_arguments) == ({}, raw)
    # sent back as an (empty) object, as the API requires
    assert (
        _to(Message.user("x"), Message(Role.ASSISTANT, (call,)))["messages"][1]["content"][0]["input"] == {}
    )


def _usage(
    uncached: int, read: int, written: int, w5m: int | None, w1h: int | None, output: int, thinking: int
) -> Any:
    usage = {
        "input_tokens": uncached,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": written,
    }
    if w5m is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5m, "ephemeral_1h_input_tokens": w1h}
    return usage | {"output_tokens": output, "output_tokens_details": {"thinking_tokens": thinking}}


@pytest.mark.parametrize(
    ("usage", "expected", "consistent"),
    [
        # input_tokens excludes cache reads and writes
        (
            _usage(6_000, 10_000, 3_000, 1_000, 2_000, 1_500, 400),
            (19_000, 10_000, 1_000, 2_000, 1_500, 400),
            True,
        ),
        # no TTL breakdown: the default TTL is 5 minutes
        (_usage(5, 0, 7, None, None, 1, 0), (12, 0, 7, 0, 1, 0), True),
        # unattributed writes are billed at the higher 1-hour rate: never under-cost
        (_usage(500, 0, 10_000, 0, 0, 10, 0), (10_500, 0, 0, 10_000, 10, 0), False),
        # a breakdown above the total is trusted as the larger count
        (_usage(5, 0, 10, 30, 20, 3, 7), (55, 0, 30, 20, 7, 7), False),
    ],
)
def test_usage_from_wire(usage: dict[str, Any], expected: tuple[int, ...], consistent: bool) -> None:
    u = _from([TEXT], usage=usage).usage
    assert (u.input, u.cache_read, u.cache_write_5m, u.cache_write_1h, u.output, u.reasoning) == expected
    assert (u.consistent, u.cache_reported) == (consistent, True)
    bare = _from([TEXT], usage={"input_tokens": 10, "output_tokens": 5}).usage
    assert (bare.input, bare.cache_reported, bare.reported) == (10, False, True)
    assert not _from([TEXT]).usage.reported


@pytest.mark.parametrize(
    ("stop", "canonical"),
    [
        ("stop_sequence", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("model_context_window_exceeded", "max_tokens"),
        ("refusal", "refusal"),
        ("pause_turn", "other"),
        ("brand_new", "other"),
    ],
)
def test_stop_reasons(stop: str, canonical: str) -> None:
    response = _from([TEXT], stop=stop)
    assert (response.stop_reason, response.provider_stop) == (canonical, stop)


async def test_request_on_the_wire(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("anthropic", expected_key=SECRET_VALUE)
    assert (await make_client(mock).complete(req())).text == "ready"
    (seen,) = mock.requests
    assert (seen.path, seen.header("x-api-key")) == ("/v1/messages", SECRET_VALUE)
    assert seen.header("Authorization") is None
    assert seen.header("anthropic-version")
    assert seen.json["cache_control"] == {"type": "ephemeral"}  # declared explicit caching


async def test_bearer_gateway(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("anthropic")
    await make_client(mock, auth_header="Authorization", auth_scheme="Bearer").complete(req())
    seen = mock.requests[0]
    assert (seen.header("Authorization"), seen.header("x-api-key")) == (f"Bearer {SECRET_VALUE}", None)


async def test_conversation_continues_after_an_empty_reply(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("anthropic")
    empty = {
        "id": "e",
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": [],
        "stop_reason": "end_turn",
    }
    mock.enqueue(200, empty | {"usage": {"input_tokens": 3, "output_tokens": 0}})
    client = make_client(mock)
    first = await client.complete(req())
    second = await client.complete(
        replace(req(), messages=(*req().messages, first.message, Message.user("again")))
    )
    assert second.text == "ready"  # the mock applies the API's non-empty-content rule


async def test_thinking_round_trips_verbatim_across_a_tool_call(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("anthropic")
    client = make_client(mock)
    ask = req("Call lookup for alpha.", tools=(LOOKUP,))
    first = await client.complete(ask)
    (reasoning,) = first.message.reasoning
    results = Message.tool_result(first.tool_calls[0].id, "42")
    second = await client.complete(replace(ask, messages=(*ask.messages, first.message, results)))
    assert (second.text, first.usage.reasoning) == ("The value is 42", 5)
    assert mock.requests[1].json["messages"][1]["content"][0] == reasoning.state

    # the endpoint rejects a modified or missing signed thinking block
    tampered = tuple(
        ReasoningPart(p.provider, {**p.state, "thinking": "edited"}) if isinstance(p, ReasoningPart) else p
        for p in first.message.parts
    )
    dropped = tuple(p for p in first.message.parts if not isinstance(p, ReasoningPart))
    for parts in (tampered, dropped):
        with pytest.raises(BadRequest):
            await client.complete(
                replace(ask, messages=(*ask.messages, Message(Role.ASSISTANT, parts), results))
            )


async def test_no_parallel_tool_calls(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("anthropic"))
    assert len((await client.complete(req("Call lookup twice.", tools=(LOOKUP,)))).tool_calls) == 1


async def test_cache_usage_and_cost(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("anthropic", cache_ttl_1h_share=3)
    client = make_client(mock)
    first, second = await client.complete(req()), await client.complete(req())
    assert (first.usage.cache_write_5m, first.usage.cache_write_1h, first.usage.cache_read) == (0, 3, 0)
    assert (second.usage.cache_read, second.usage.cache_write) == (3, 0)
    row = client.call_log[-1]
    assert (row["usage_cache_read"], row["usage_input"]) == (3, second.usage.input)
    assert row["price_version"] == "test-2026-09"
    assert client.counters.cost_micros == sum(r["cost_micros"] for r in client.call_log) > 0


async def test_refusal_and_context_window_stops(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("anthropic"))
    assert (await client.complete(req("MOCK_REFUSE"))).stop_reason == "refusal"
    assert client.call_log[-1]["stop"] == "refusal"
    await client.complete(req("MOCK_CONTEXT_WINDOW"))
    row = client.call_log[-1]
    assert (row["stop"], row["provider_stop"]) == ("max_tokens", "model_context_window_exceeded")


@pytest.mark.parametrize(
    ("status", "kind", "error", "error_class", "attempts"),
    [
        (401, "authentication_error", AuthFailed, "Auth", 1),
        (403, "permission_error", AuthFailed, "PermissionDenied", 1),
        (413, "request_too_large", BadRequest, "RequestTooLarge", 1),
        (400, "invalid_request_error", BadRequest, "BadRequest", 1),
        (429, "rate_limit_error", RateLimited, "RateLimited", 3),
        (500, "api_error", ProviderError, "ProviderInternal", 3),
        (529, "overloaded_error", Overloaded, "Overloaded", 3),
    ],
)
async def test_error_envelopes(
    start_mock: Any, make_client: Any, status: int, kind: str, error: type, error_class: str, attempts: int
) -> None:
    mock = await start_mock("anthropic")
    for _ in range(3):
        mock.enqueue(
            status,
            {"type": "error", "error": {"type": kind, "message": "echoing the prompt"}},
            {"request-id": "req_9"},
        )
    with pytest.raises(error) as info:
        await make_client(mock).complete(req())
    err = info.value
    assert (err.error_class, err.provider_code, err.request_id) == (error_class, kind, "req_9")
    assert "echoing" not in str(err) and len(mock.requests) == attempts


async def test_529_then_success(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("anthropic")
    mock.enqueue(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    assert (await make_client(mock).complete(req())).text == "ready"
    assert len(mock.requests) == 2


async def test_prompt_too_long(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("anthropic", context_limit_bytes=200)
    with pytest.raises(ContextTooLong) as info:
        await make_client(mock).complete(req("x" * 500))
    assert info.value.provider_code == "invalid_request_error" and "prompt is too long" not in str(info.value)
