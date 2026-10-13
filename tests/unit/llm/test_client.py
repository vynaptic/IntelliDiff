"""LLMClient: adapter choice, budgets, capabilities, call-log hygiene, limits."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import pytest

from deltasleuth.llm.client import LLMClient
from deltasleuth.llm.model import (
    BadRequest,
    BudgetExceeded,
    CapabilityUnavailable,
    ContextTooLong,
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    ToolSpec,
)
from deltasleuth.llm.policy import EgressPolicy

from .conftest import completion, req

LOOKUP = ToolSpec("lookup", "Look up a value.", {"type": "object", "properties": {"key": {"type": "string"}}})
PAYLOAD = "PAYLOAD-CANARY-51c9"


def _then(ask: LLMRequest, first: LLMResponse, result: str = "1") -> LLMRequest:
    """The follow-up request carrying ``first``'s tool results."""
    results = [Message.tool_result(c.id, result) for c in first.tool_calls]
    return LLMRequest((*ask.messages, first.message, *results), ask.tools, data_classes=ask.data_classes)


def _one_call_budget(client: LLMClient, request: LLMRequest) -> int:
    """A run budget that admits exactly one ``request``."""
    return client.estimate_input_tokens(request) + request.max_output_tokens + 1


async def test_adapter_per_dialect(start_mock: Any, make_client: Any) -> None:
    for kind in ("openai", "anthropic"):
        client = make_client(await start_mock(kind))
        assert client.adapter.dialect == client.profile.dialect


async def test_credential_comes_from_the_secret_reader(start_mock: Any, make_profile: Any) -> None:
    mock = await start_mock("openai")
    seen: list[str] = []
    profile = make_profile("openai_chat", mock.base_url, auth_ref="file:/vault/llm.key")
    reader = lambda ref: seen.append(ref) or "sk-from-vault"  # noqa: E731
    async with LLMClient.from_profile(profile, secret_reader=reader, egress=EgressPolicy("dev")) as client:
        await client.complete(req())
    assert seen == ["file:/vault/llm.key"]
    assert mock.requests[0].header("Authorization") == "Bearer sk-from-vault"


async def test_effective_capabilities(start_mock: Any, make_client: Any) -> None:
    claude = make_client(
        await start_mock("anthropic"), capabilities={"tool_calling": True, "prompt_caching": "automatic"}
    )
    assert (claude.effective.tool_calling, claude.effective.prompt_caching) == (True, "automatic")
    openai = make_client(await start_mock("openai"), capabilities={"prompt_caching": "automatic"})
    assert (openai.effective.tool_calling, openai.effective.prompt_caching) == (False, "automatic")


# budgets
async def test_per_call_input_budget_is_checked_before_sending(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock, budgets={"max_input_tokens_per_call": 50})
    with pytest.raises(BudgetExceeded) as info:
        await client.complete(req("x" * 400))
    assert (info.value.budget, info.value.limit) == ("max_input_tokens_per_call", 50)
    assert mock.requests == []
    row = client.call_log[-1]
    assert row["denial_reason"] == "budget:max_input_tokens_per_call"
    assert row["estimated_input_tokens"] == info.value.requested > 50


async def test_per_run_tokens_count_the_worst_case_output(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock, budgets={"per_run_tokens": 2000})
    with pytest.raises(BudgetExceeded, match="per_run_tokens") as info:
        await client.complete(req(max_output_tokens=8000))
    assert info.value.requested == client.estimate_input_tokens(req(max_output_tokens=8000)) + 8000
    assert mock.requests == []
    request = req(max_output_tokens=16)
    tight = make_client(mock, budgets={"per_run_tokens": _one_call_budget(client, request)})
    await tight.complete(request)
    with pytest.raises(BudgetExceeded, match="per_run_tokens"):
        await tight.complete(request)
    assert len(mock.requests) == 1


async def test_concurrent_calls_cannot_overshoot_the_run(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.delay_s = 0.05  # every call is still in flight when the others check the budget
    request = req(max_output_tokens=100)
    probe = make_client(mock)
    worst_case = probe.estimate_input_tokens(request) + 100
    client = make_client(mock, budgets={"per_run_tokens": 2 * worst_case}, limits={"max_concurrency": 16})
    results = await asyncio.gather(*(client.complete(request) for _ in range(12)), return_exceptions=True)
    refused = [r for r in results if isinstance(r, BudgetExceeded)]
    assert (len(mock.requests), len(refused), {r.budget for r in refused}) == (2, 10, {"per_run_tokens"})
    assert client.counters.input_tokens + client.counters.output_tokens <= 2 * worst_case


async def test_concurrent_calls_cannot_overshoot_the_turn(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.delay_s = 0.05
    estimate = make_client(mock).estimate_input_tokens(req())
    client = make_client(
        mock, budgets={"max_input_tokens_per_turn": 3 * estimate}, limits={"max_concurrency": 16}
    )
    turn = client.new_turn()
    results = await asyncio.gather(
        *(client.complete(req(), turn=turn) for _ in range(8)), return_exceptions=True
    )
    assert len(mock.requests) == 3
    assert all(isinstance(r, BudgetExceeded) for r in results if not isinstance(r, LLMResponse))
    assert (turn.calls, turn.reserved_input_tokens) == (3, 0)


async def test_a_failed_call_releases_its_reservation(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.enqueue(500, {"error": {"message": "x", "type": "server_error", "code": None}})
    request = req(max_output_tokens=16)
    budget = make_client(mock).estimate_input_tokens(request) + 16
    client = make_client(mock, budgets={"per_run_tokens": budget}, retry_attempts=1)
    with pytest.raises(ProviderError):
        await client.complete(request)
    await client.complete(request)  # the failed call charged nothing
    assert len(mock.requests) == 2


async def test_replies_without_usage_are_charged_their_worst_case(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai", usage=False)
    request = req("x" * 3000, max_output_tokens=10)
    estimate = make_client(mock).estimate_input_tokens(request)
    client = make_client(mock, budgets={"per_run_tokens": 4 * (estimate + 10)})
    for _ in range(4):
        assert not (await client.complete(request)).usage.reported
    with pytest.raises(BudgetExceeded, match="per_run_tokens"):
        await client.complete(request)
    counters = client.counters
    assert (counters.input_tokens, counters.cost_micros) == (0, 0)
    assert counters.estimated_tokens == 4 * (estimate + 10)
    assert [r["usage_reported"] for r in client.call_log[:4]] == [False] * 4
    assert "cost_micros" not in client.call_log[0]  # estimates are never priced
    per_turn = make_client(mock, budgets={"max_input_tokens_per_turn": 2 * estimate})
    turn = per_turn.new_turn()
    await per_turn.complete(request, turn=turn)
    await per_turn.complete(request, turn=turn)
    with pytest.raises(BudgetExceeded, match="max_input_tokens_per_turn"):
        await per_turn.complete(request, turn=turn)
    assert (turn.input_tokens, len(mock.requests)) == (2 * estimate, 6)


async def test_turn_budgets(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock, budgets={"max_tool_calls": 1, "max_wall_s": 5})
    turn = client.new_turn()
    ask = req("Call lookup.", tools=(LOOKUP,))
    calls = [
        {"id": f"c{i}", "type": "function", "function": {"name": "lookup", "arguments": "{}"}} for i in (1, 2)
    ]
    mock.enqueue(200, completion(None, tool_calls=calls))  # parallel calls although none were allowed
    first = await client.complete(ask, turn=turn)
    assert (turn.tool_calls, turn.tool_calls_remaining, client.counters.tool_calls) == (2, 0, 2)
    with pytest.raises(BudgetExceeded, match="max_tool_calls"):
        await client.complete(_then(ask, first), turn=turn)
    late = client.new_turn()
    late.started -= 6
    with pytest.raises(BudgetExceeded, match="max_wall_s"):
        await client.complete(req(), turn=late)
    assert client.new_turn().max_input_tokens == 2 * 20_000  # default: (max_tool_calls + 1) per call


async def test_turn_input_tokens(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(
        mock, budgets={"max_input_tokens_per_turn": make_client(mock).estimate_input_tokens(req()) + 1}
    )
    turn = client.new_turn()
    await client.complete(req(), turn=turn)
    assert turn.calls == 1 and turn.input_tokens > 0
    with pytest.raises(BudgetExceeded, match="max_input_tokens_per_turn"):
        await client.complete(req(), turn=turn)


async def test_declared_limits_are_checked_locally(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock, capabilities={"max_output": 1000, "max_context": 2000})
    with pytest.raises(BadRequest, match="max_output"):
        await client.complete(req(max_output_tokens=4096))
    with pytest.raises(ContextTooLong):
        await client.complete(req("y" * 4000, max_output_tokens=1000))
    assert mock.requests == []
    assert [r["denial_reason"] for r in client.call_log] == ["local:BadRequest", "local:ContextTooLong"]


# capabilities on the wire
async def test_tool_requests_on_the_wire(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    response = await make_client(mock).complete(req("Call lookup.", tools=(LOOKUP,)))
    body = mock.requests[0].json
    assert (body["tool_choice"], body["parallel_tool_calls"], body["store"]) == ("auto", False, False)
    assert [c.name for c in response.tool_calls] == ["lookup"] and response.latency_ms is not None


async def test_tools_without_tool_calling(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock, capabilities={})
    with pytest.raises(CapabilityUnavailable):
        await client.complete(req(tools=(LOOKUP,)))
    assert (mock.requests, client.call_log[-1]["denial_reason"]) == ([], "local:BadRequest")


# call log
async def test_rows_hold_no_payload(
    start_mock: Any, make_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mock = await start_mock("openai")
    client = make_client(mock)
    ask = req(f"Call lookup for {PAYLOAD}.", tools=(LOOKUP,))
    await client.complete(_then(ask, await client.complete(ask), PAYLOAD))
    assert PAYLOAD in mock.requests[1].everything()  # it did travel to the model ...
    assert PAYLOAD not in json.dumps(client.call_log) + caplog.text  # ... but never into rows or logs


async def test_row_fields(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("openai"))
    response = await client.complete(req())
    (row,) = client.call_log
    assert re.fullmatch(r"[0-9a-f]{64}", row["request_hash"])
    expected = {
        "profile_id": "test-profile",
        "dialect": "openai_chat",
        "trust": "internal",
        "host": "127.0.0.1",
        "model_configured": "gpt-test",
        "data_classes": ["synthetic"],
        "attempt": 1,
        "model_reported": response.model_reported,
        "response_id": response.response_id,
        "stop": "end_turn",
        "provider_stop": "stop",
        "usage_reported": True,
        "usage_consistent": True,
        "usage_input": response.usage.input,
        "usage_output": 12,
        "usage_reasoning": 4,
        "currency": "USD",
        "price_version": "test-2026-09",
    }
    assert row | expected == row and "error_class" not in row
    assert row["cost_micros"] == client.counters.cost_micros > 0 and isinstance(row["latency_ms"], int)


async def test_request_hash_covers_everything_sent(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("openai"))
    variants: list[dict[str, Any]] = [
        {},
        {},  # the same request hashes the same
        {"tool_choice": "none"},
        {"max_output_tokens": 1},
    ]
    for fields in variants:
        await client.complete(req(tools=(LOOKUP,), **fields))
    await client.complete(req("other", tools=(LOOKUP,)))
    hashes = [row["request_hash"] for row in client.call_log]
    assert hashes[0] == hashes[1] and len(set(hashes)) == len(variants)


async def test_usage_details_in_rows(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.enqueue(
        200,
        completion(
            usage={
                "prompt_tokens": 500,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 10_000},
            }
        ),
    )
    custom = {"id": "c1", "type": "custom", "custom": {"name": "free", "input": "x"}}
    mock.enqueue(200, completion(tool_calls=[custom]))
    client = make_client(mock)
    for _ in range(3):
        await client.complete(req())
    inconsistent, unreported, consistent = client.call_log
    # resolved upward: the cached tokens are part of the input and priced as cache reads
    assert (
        inconsistent["usage_consistent"],
        inconsistent["usage_input"],
        inconsistent["usage_cache_read"],
    ) == (False, 10_000, 10_000)
    assert inconsistent["cost_micros"] == 5_250  # 10k reads at 0.1 x $5/MTok + 10 out at $25/MTok
    assert (unreported["usage_reported"], unreported["ignored_tool_calls"]) == (False, 1)
    assert (consistent["usage_consistent"], consistent["ignored_tool_calls"]) == (True, 0)


# malformed replies
@pytest.mark.parametrize(
    "case", ["openai_html", "openai_no_choices", "anthropic_html", "anthropic_error_envelope"]
)
async def test_malformed_200_is_typed_logged_and_retried(
    start_mock: Any, make_client: Any, case: str
) -> None:
    mock = await start_mock(case.split("_")[0])

    def enqueue() -> None:
        if case.endswith("html"):
            mock.enqueue_text(200, "<html><body>Session expired. Sign in again.</body></html>")
        elif case == "anthropic_error_envelope":
            mock.enqueue(200, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        else:
            mock.enqueue(200, {"id": "x", "object": "chat.completion", "created": 1, "model": "m"})

    enqueue()
    client = make_client(mock)
    assert (await client.complete(req())).text == "ready"  # attempt 2 gets a well-formed reply
    first = client.call_log[0]
    assert (first["error_class"], first["http_status"], len(client.call_log)) == ("ProviderInternal", 200, 2)
    enqueue()
    with pytest.raises(ProviderError) as info:
        await make_client(mock, retry_attempts=1).complete(req())
    assert (info.value.error_class, info.value.status) == ("ProviderInternal", 200)
    assert "Sign in" not in str(info.value) + json.dumps(client.call_log)


async def test_an_unforeseen_adapter_failure_still_gets_its_row(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("openai"))

    async def broken(request: LLMRequest, *, caching: bool) -> Any:
        raise RuntimeError("adapter bug")

    client.adapter.send = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="adapter bug"):
        await client.complete(req())
    assert (client.counters.calls, client.counters.errors) == (1, 1)
    assert client.call_log[0]["error_class"] == "ProviderInternal"


# estimate and limits
async def test_estimate_calibrates_from_large_reported_usage(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("openai"))
    small, large = req("z" * 100), req("z" * 20_000)
    before = client.estimate_input_tokens(large)
    await client.complete(small)  # too small to calibrate
    assert client.estimate_input_tokens(large) == before
    await client.complete(large)  # the mock reports about 4 bytes per token
    assert client.estimate_input_tokens(large) < before


async def test_max_concurrency(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.delay_s = 0.05
    client = make_client(mock, limits={"max_concurrency": 1, "rps": 1000})
    await asyncio.gather(*(client.complete(req()) for _ in range(4)))
    assert (mock.max_in_flight, len(mock.requests)) == (1, 4)


async def test_fractional_rps_limits_rate_without_error(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("openai"), limits={"rps": 0.5})
    assert (await client.complete(req())).text == "ready"  # used to raise ValueError in acquire()
    limiter = client.gate.limiter
    assert (limiter.max_rate, limiter.time_period) == (1.0, 2.0)  # one call every 2 s
    assert not limiter.has_capacity()  # the next call waits for the bucket to refill
