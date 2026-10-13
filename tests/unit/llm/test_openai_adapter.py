"""openai_chat: canonical <-> Chat Completions wire, and typed errors against the local mock."""

from __future__ import annotations

from typing import Any

import pytest
from openai._models import construct_type
from openai.types.chat import ChatCompletion

from deltasleuth.llm.adapters.openai_chat import from_wire, to_wire
from deltasleuth.llm.model import (
    AuthFailed,
    BadRequest,
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
from deltasleuth.llm.profiles import WireOptions

from .conftest import SECRET_VALUE, req

SCHEMA = {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
LOOKUP = ToolSpec("lookup", "Look up a value.", SCHEMA)


def _from(message: dict[str, Any], finish: str = "stop", usage: dict[str, Any] | None = None) -> Any:
    raw = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-test-2026",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
    }
    return from_wire(construct_type(type_=ChatCompletion, value=raw | ({"usage": usage} if usage else {})))


def _call(id_: str, arguments: str, **extra: Any) -> dict[str, Any]:
    return {"id": id_, "type": "function", "function": {"name": "lookup", "arguments": arguments}} | extra


def test_messages_to_wire() -> None:
    request = LLMRequest(
        messages=(
            Message.system("sys"),
            Message(Role.USER, (TextPart("a"), TextPart("b"))),
            Message(
                Role.ASSISTANT,
                (
                    ReasoningPart("anthropic_messages", {"type": "thinking"}),
                    ToolCallPart("c1", "lookup", {"key": "é"}),
                ),
            ),
            Message(Role.TOOL, (ToolResultPart("c1", "42"), ToolResultPart("c2", "boom", is_error=True))),
            Message(Role.ASSISTANT, (ToolCallPart("c3", "lookup", {}, raw_arguments='{"key": broken'),)),
            Message(Role.ASSISTANT, (ReasoningPart("anthropic_messages", {"type": "thinking"}),)),
        )
    )
    body = to_wire(request, model="gpt-test")
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"role": "assistant", "content": None, "tool_calls": [_call("c1", '{"key":"é"}')]},
        {"role": "tool", "tool_call_id": "c1", "content": "42"},
        {"role": "tool", "tool_call_id": "c2", "content": "ERROR: boom"},  # no error flag on this wire
        {"role": "assistant", "content": None, "tool_calls": [_call("c3", '{"key": broken')]},  # verbatim
        {"role": "assistant", "content": ""},  # another dialect's reasoning is not sent
    ]
    assert (body["model"], body["store"], body["max_completion_tokens"]) == ("gpt-test", False, 4096)
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(body)


@pytest.mark.parametrize("choice", ["auto", "none"])
def test_tools_to_wire(choice: Any) -> None:
    body = to_wire(LLMRequest(messages=(Message.user("x"),), tools=(LOOKUP,), tool_choice=choice), model="m")
    assert (body["tool_choice"], body["parallel_tool_calls"]) == (choice, False)
    function = {"name": "lookup", "description": "Look up a value.", "parameters": SCHEMA}
    assert body["tools"] == [{"type": "function", "function": function}]


def test_wire_options_for_a_strict_gateway() -> None:
    wire = WireOptions("max_tokens", send_store=False, send_parallel_tool_calls=False)
    request = LLMRequest(messages=(Message.user("x"),), tools=(LOOKUP,), max_output_tokens=99)
    body = to_wire(request, model="m", wire=wire)
    assert body["max_tokens"] == 99
    assert not {"max_completion_tokens", "store", "parallel_tool_calls"} & set(body)


def test_reply_from_wire() -> None:
    usage = {
        "prompt_tokens": 16_000,
        "completion_tokens": 1_500,
        "prompt_tokens_details": {"cached_tokens": 10_000},
        "completion_tokens_details": {"reasoning_tokens": 300},
    }
    response = _from({"content": "hello", "refusal": None}, usage=usage)
    assert (response.text, response.stop_reason, response.provider_stop) == ("hello", "end_turn", "stop")
    assert (response.model_reported, response.response_id) == ("gpt-test-2026", "chatcmpl-1")
    u = response.usage
    assert (u.input, u.output, u.cache_read, u.reasoning, u.uncached_input) == (
        16_000,
        1_500,
        10_000,
        300,
        6_000,
    )
    assert u.reported and u.cache_reported and u.consistent
    bare = _from({"content": ""}, usage={"prompt_tokens": 5, "completion_tokens": 1})
    assert (bare.text, bare.usage.cache_reported) == ("", False)
    assert not _from({"content": "x"}).usage.reported


CACHED_ABOVE_PROMPT = {
    "prompt_tokens": 500,
    "completion_tokens": 10,
    "prompt_tokens_details": {"cached_tokens": 10_000},
}
REASONING_ABOVE_OUTPUT = {
    "prompt_tokens": 10,
    "completion_tokens": 2,
    "prompt_tokens_details": {"cached_tokens": 50, "cache_write_tokens": 3},
    "completion_tokens_details": {"reasoning_tokens": 9},
}


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        # a gateway in front of a Messages-style backend leaves cache reads out of prompt_tokens
        (CACHED_ABOVE_PROMPT, (10_000, 10_000, 0, 10, 0)),
        (REASONING_ABOVE_OUTPUT, (53, 50, 3, 9, 9)),
        ({"prompt_tokens": -5, "completion_tokens": 3}, (0, 0, 0, 3, 0)),
    ],
)
def test_inconsistent_usage_is_resolved_upward_and_flagged(
    usage: dict[str, Any], expected: tuple[int, ...]
) -> None:
    u = _from({"content": "x"}, usage=usage).usage
    assert (u.input, u.cache_read, u.cache_write_5m, u.output, u.reasoning) == expected
    assert not u.consistent


def test_tool_calls_from_wire() -> None:
    calls = [
        _call("c1", '{"key": "a"}'),
        _call("c2", '{"key": '),
        _call("c3", "[1, 2]"),
        _call("c4", ""),
        {"id": "c5", "function": {"name": "lookup", "arguments": "{}"}},  # a lenient gateway omits type
        {"id": "c6", "type": "custom", "custom": {"name": "free", "input": "text"}},  # never offered
        {"id": "c7"},
    ]
    response = _from({"content": None, "tool_calls": calls}, finish="tool_calls")
    got = [(c.id, dict(c.arguments), c.raw_arguments) for c in response.tool_calls]
    assert got == [
        ("c1", {"key": "a"}, None),
        ("c2", {}, '{"key": '),  # kept for the repair step
        ("c3", {}, "[1, 2]"),
        ("c4", {}, None),
        ("c5", {}, None),
    ]
    assert (response.stop_reason, response.ignored_tool_calls) == ("tool_use", 2)


@pytest.mark.parametrize(
    ("finish", "has_calls", "stop"),
    [
        ("length", False, "max_tokens"),
        ("content_filter", False, "content_filter"),
        ("function_call", True, "tool_use"),
        ("stop", True, "tool_use"),  # some compatible servers report stop for a forced call
        ("length", True, "max_tokens"),
        ("something_new", False, "other"),
    ],
)
def test_stop_reasons(finish: str, has_calls: bool, stop: str) -> None:
    message = {"content": "x"} | ({"tool_calls": [_call("c", "{}")]} if has_calls else {})
    assert _from(message, finish=finish).stop_reason == stop


def test_refusal() -> None:
    response = _from({"content": None, "refusal": "I can't help with that."})
    assert (response.stop_reason, response.text) == ("refusal", "I can't help with that.")


def _err(code: str | None, kind: str = "invalid_request_error") -> dict[str, Any]:
    return {
        "error": {"message": "upstream said: echo of your prompt", "type": kind, "param": None, "code": code}
    }


async def test_request_on_the_wire(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai", expected_key=SECRET_VALUE)
    response = await make_client(mock).complete(req())
    assert (response.text, response.model_reported) == ("ready", "gpt-test-2026-09-01")
    (seen,) = mock.requests
    assert seen.path == "/v1/chat/completions"
    assert seen.header("Authorization") == f"Bearer {SECRET_VALUE}"
    assert seen.header("OpenAI-Organization") is None and seen.header("OpenAI-Project") is None
    assert seen.json["store"] is False


async def test_tool_round_trip(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock)
    ask = req("Call lookup for alpha.", tools=(LOOKUP,))
    first = await client.complete(ask)
    (call,) = first.tool_calls
    assert (first.stop_reason, call.name, call.arguments) == ("tool_use", "lookup", {"key": "ok"})
    results = Message.tool_result(call.id, "42")
    second = await client.complete(
        LLMRequest((*ask.messages, first.message, results), (LOOKUP,), data_classes=ask.data_classes)
    )
    assert second.text == "The value is 42"
    assert mock.requests[1].json["messages"][3] == {"role": "tool", "tool_call_id": call.id, "content": "42"}


async def test_strict_gateway_works_with_declared_wire_options(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock(
        "openai", unknown_fields=frozenset({"store", "max_completion_tokens", "parallel_tool_calls"})
    )
    with pytest.raises(BadRequest):  # with the OpenAI defaults, every call is rejected
        await make_client(mock, retry_attempts=1).complete(req())
    wire = {"max_tokens_param": "max_tokens", "send_store": False, "send_parallel_tool_calls": False}
    client = make_client(mock, wire=wire)  # the mock host is internal
    await client.complete(req("Call lookup for alpha.", tools=(LOOKUP,)))
    assert mock.requests[-1].json["max_tokens"] == 4096


async def test_cached_tokens(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock)
    await client.complete(req())
    second = await client.complete(req())
    assert second.usage.cache_read > 0 and client.call_log[-1]["usage_cache_read"] == second.usage.cache_read


async def test_429_is_typed_and_retried_after_its_delay(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.enqueue(
        429, _err("rate_limit_exceeded", "requests"), {"retry-after": "7", "x-request-id": "req_abc"}
    )
    with pytest.raises(RateLimited) as info:
        await make_client(mock, retry_attempts=1).complete(req())
    err = info.value
    assert (err.retry_after, err.status, err.provider_code, err.request_id) == (
        7.0,
        429,
        "rate_limit_exceeded",
        "req_abc",
    )
    assert "echo of your prompt" not in str(err)

    mock.enqueue(429, _err("rate_limit_exceeded"), {"retry-after-ms": "10"})
    client = make_client(mock)
    assert (await client.complete(req())).text == "ready"
    assert [(r["attempt"], r.get("error_class")) for r in client.call_log] == [(1, "RateLimited"), (2, None)]
    assert (client.counters.calls, client.counters.errors) == (2, 1)


@pytest.mark.parametrize(
    ("headers", "code", "retried"),
    [
        ({"retry-after": "0"}, "rate_limit_exceeded", True),  # retry now, not "do not retry"
        ({"retry-after": "120"}, "rate_limit_exceeded", False),  # surfaces instead of stalling a turn
        ({}, "insufficient_quota", False),
    ],
)
async def test_429_retry_decisions(
    start_mock: Any, make_client: Any, headers: dict[str, str], code: str, retried: bool
) -> None:
    mock = await start_mock("openai")
    mock.enqueue(429, _err(code), headers)
    if retried:
        assert (await make_client(mock).complete(req())).text == "ready"
    else:
        with pytest.raises(RateLimited):
            await make_client(mock).complete(req())
    assert len(mock.requests) == 1 + retried


@pytest.mark.parametrize(
    ("status", "code", "error", "error_class", "attempts"),
    [
        (400, "context_length_exceeded", ContextTooLong, "ContextTooLong", 1),
        (401, None, AuthFailed, "Auth", 1),
        (403, None, AuthFailed, "PermissionDenied", 1),
        (404, None, BadRequest, "BadRequest", 1),
        (413, None, BadRequest, "RequestTooLarge", 1),
        (422, None, BadRequest, "BadRequest", 1),
        (500, None, ProviderError, "ProviderInternal", 3),
        (503, None, Overloaded, "Overloaded", 3),
    ],
)
async def test_status_mapping_and_retries(
    start_mock: Any,
    make_client: Any,
    status: int,
    code: str | None,
    error: type,
    error_class: str,
    attempts: int,
) -> None:
    mock = await start_mock("openai")
    for _ in range(3):
        mock.enqueue(status, _err(code))
    with pytest.raises(error) as info:
        await make_client(mock).complete(req())
    assert (info.value.error_class, info.value.status, len(mock.requests)) == (error_class, status, attempts)


async def test_context_overflow_from_the_mock(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai", context_limit_bytes=200)
    with pytest.raises(ContextTooLong) as info:
        await make_client(mock).complete(req("x" * 500))
    assert info.value.provider_code == "context_length_exceeded"


async def test_transport_failures(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.delay_s = 1.0
    with pytest.raises(ProviderError) as info:
        await make_client(mock, retry_attempts=1, limits={"timeout_s": 0.1}).complete(req())
    assert info.value.error_class == "Timeout"
    client = make_client(mock, retry_attempts=2)
    await mock.close()
    with pytest.raises(ProviderError) as info:
        await client.complete(req())
    assert [r["error_class"] for r in client.call_log] == ["Connection", "Connection"]


async def test_redirects_are_not_followed(start_mock: Any, make_client: Any) -> None:
    mock, other = await start_mock("openai"), await start_mock("canary")
    mock.enqueue(307, _err(None), {"location": f"{other.origin}/v1/chat/completions"})
    with pytest.raises(BadRequest):
        await make_client(mock, retry_attempts=1).complete(req())
    assert other.requests == []
