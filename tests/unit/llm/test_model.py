"""Canonical model: structural rules, payload-free reprs and errors."""

from __future__ import annotations

import pytest

from deltasleuth.llm.model import (
    BadRequest,
    DataClass,
    LLMRequest,
    Message,
    ReasoningPart,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)

SCHEMA = {"type": "object", "properties": {"k": {"type": "string"}}}
TOOL = ToolSpec("lookup", "Look something up.", SCHEMA)


def _request(**kwargs: object) -> LLMRequest:
    return LLMRequest(messages=(Message.user("hi"),), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("role", "part"),
    [
        (Role.SYSTEM, ToolCallPart("c1", "lookup")),
        (Role.USER, ReasoningPart("anthropic_messages", {"type": "thinking"})),
        (Role.TOOL, TextPart("x")),
        (Role.ASSISTANT, ToolResultPart("c1", "x")),
    ],
)
def test_parts_must_fit_the_role(role: Role, part: object) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        Message(role, (part,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one part"):
        Message(role, ())


def test_message_accessors() -> None:
    reasoning = ReasoningPart("anthropic_messages", {"type": "thinking", "signature": "s"})
    call = ToolCallPart("c1", "lookup", {"k": "v"})
    msg = Message("assistant", (reasoning, TextPart("a"), TextPart("b"), call))  # type: ignore[arg-type]
    assert (msg.role, msg.text) == (Role.ASSISTANT, "a\n\nb")
    assert (msg.tool_calls, msg.reasoning) == ((call,), (reasoning,))
    assert Message.tool_result("c1", "out", is_error=True).parts == (ToolResultPart("c1", "out", True),)
    assert not ToolCallPart("c1", "t", {}, raw_arguments="{broken").arguments_valid


def test_payload_is_not_in_reprs() -> None:
    payload = "PAYLOAD-7Q2K"
    for obj in (
        Message.user(payload),
        ToolCallPart("c1", "lookup", {"k": payload}, raw_arguments=payload),
        ToolResultPart("c1", payload),
        ReasoningPart("anthropic_messages", {"thinking": payload}),
    ):
        assert payload not in repr(obj)


@pytest.mark.parametrize("name", ["", "has space", "x" * 65, "dot.name", "ünicode"])
def test_tool_names_are_identifiers(name: str) -> None:
    with pytest.raises(ValueError, match="tool name"):
        ToolSpec(name, "d", SCHEMA)


def test_tool_schema_must_be_an_object() -> None:
    with pytest.raises(ValueError, match="object"):
        ToolSpec("t", "d", {"type": "string"})


def test_request_coerces_data_classes() -> None:
    assert _request(data_classes={"synthetic", DataClass.GOLDEN}).data_classes == {"synthetic", "golden"}
    with pytest.raises(ValueError):
        _request(data_classes={"raw_production"})


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"messages": ()}, "at least one message"),
        ({"tool_choice": "required", "tools": (TOOL,)}, "tool_choice must be"),
        ({"tools": (TOOL, TOOL)}, "unique"),
        ({"max_output_tokens": 0}, "positive"),
    ],
)
def test_invalid_requests(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        LLMRequest(**({"messages": (Message.user("hi"),)} | kwargs))  # type: ignore[arg-type]


def test_usage_parts_of_input() -> None:
    usage = Usage(input=16_000, output=1_500, cache_read=10_000, cache_write_5m=100, cache_write_1h=50)
    assert (usage.cache_write, usage.uncached_input) == (150, 5_850)


def test_errors_render_without_free_text() -> None:
    err = BadRequest(
        "request rejected", status=400, provider_code="invalid_request_error", request_id="req_1"
    )
    assert (
        str(err) == "BadRequest: request rejected (status=400, code=invalid_request_error, request_id=req_1)"
    )
    echoed = BadRequest("x", provider_code="prompt: my secret customer text", request_id="id with spaces")
    assert (echoed.provider_code, echoed.request_id) == (None, None)
    assert "secret" not in str(echoed)
