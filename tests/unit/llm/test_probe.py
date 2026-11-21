"""Capability probe end to end against both wire mocks: declared ∩ probed, and aborts."""

from __future__ import annotations

import json
from typing import Any

import pytest

from deltasleuth.llm.client import LLMClient
from deltasleuth.llm.model import AuthFailed, EgressDenied
from deltasleuth.llm.policy import EgressPolicy
from deltasleuth.llm.probe import PROBE_MAX_OUTPUT_TOKENS, PROBE_NONCE, ProbeAborted, probe_capabilities
from deltasleuth.llm.profiles import Capabilities

from .conftest import ALL_CAPABILITIES, completion

CHECKS = ("tool_roundtrip", "usage_cached_tokens")


def _status(report: Any, name: str) -> tuple[str, tuple[str, ...]]:
    result = next(r for r in report.results if r.name == name)
    return result.status, result.evidence


def _passed(report: Any) -> set[str]:
    return {r.name for r in report.results if r.status == "pass"}


async def test_fully_capable_endpoint(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("openai"))
    report = await probe_capabilities(client)
    assert [r.name for r in report.results] == list(CHECKS) and _passed(report) == set(CHECKS)
    assert report.probed == Capabilities(**ALL_CAPABILITIES)
    assert report.effective == client.profile.capabilities
    assert (report.model_configured, report.model_reported) == ("gpt-test", "gpt-test-2026-09-01")
    assert report.adapter_version.startswith("openai_chat/1+openai-")
    evidence = " ".join(e for r in report.results for e in r.evidence)
    assert PROBE_NONCE not in evidence and "The value is" not in evidence  # no reply text


@pytest.mark.parametrize(
    ("variant", "check", "status", "lost"),
    [
        ({"tools": False}, "tool_roundtrip", "unsupported", "tool_calling"),
        ({"cached_tokens": False}, "usage_cached_tokens", "fail", "prompt_caching"),
    ],
)
async def test_weaker_endpoints(start_mock: Any, make_client: Any, variant: Any, check: str, status: str,
                                lost: str) -> None:  # fmt: skip
    report = await probe_capabilities(make_client(await start_mock("openai", **variant)))
    assert _status(report, check)[0] == status
    assert getattr(report.declared, lost) and getattr(report.effective, lost) in (False, "none")


async def test_undeclared_tool_calling_is_not_probed(start_mock: Any, make_client: Any) -> None:
    report = await probe_capabilities(make_client(await start_mock("openai"), capabilities={}))
    assert _status(report, "tool_roundtrip")[0] == "unsupported" and not report.effective.tool_calling


async def test_report_is_serialisable_and_deterministic(start_mock: Any, make_client: Any) -> None:
    first = await probe_capabilities(make_client(await start_mock("openai")))
    second = await probe_capabilities(make_client(await start_mock("openai")))
    doc = json.loads(first.to_json())
    assert {
        "endpoint",
        "model_reported",
        "adapter_version",
        "results",
        "declared",
        "probed",
        "effective",
    } <= set(doc)
    # only the endpoint (a random local port) differs
    assert {k: v for k, v in doc.items() if k != "endpoint"} == {
        k: v for k, v in json.loads(second.to_json()).items() if k != "endpoint"
    }


async def test_claude_profile(start_mock: Any, make_client: Any) -> None:
    client = make_client(await start_mock("anthropic"))
    report = await probe_capabilities(client)
    assert _passed(report) == set(CHECKS)
    assert "reasoning parts carried back verbatim: 1" in _status(report, "tool_roundtrip")[1]
    assert (report.effective, report.model_reported) == (client.profile.capabilities, "claude-opus-5")


async def test_egress_and_auth_failures_abort(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    with pytest.raises(EgressDenied):
        await probe_capabilities(make_client(mock, data_classes=["production_tokenised"]))
    assert mock.requests == []
    claude = await start_mock("anthropic", expected_key="a-different-key")
    with pytest.raises(AuthFailed):
        await probe_capabilities(make_client(claude))
    assert len(claude.requests) == 1


async def test_one_bad_reply_fails_one_check(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.enqueue(200, completion(usage={"prompt_tokens": 20, "completion_tokens": 1}))  # the baseline
    mock.enqueue(200, completion("no"))  # the model ignores the tool, and reports no usage
    report = await probe_capabilities(make_client(mock))
    assert _status(report, "tool_roundtrip") == ("fail", ("call 1: no get_probe_value call (stop=end_turn)",))
    assert not report.probed.tool_calling and _status(report, "usage_cached_tokens")[0] == "fail"


@pytest.mark.parametrize(
    ("enqueue", "evidence"),
    [
        (
            lambda m: m.enqueue(500, {"error": {"type": "server_error"}}),
            "error: ProviderInternal status=500 code=server_error",
        ),
        (lambda m: m.enqueue_text(200, "<html>Sign in</html>"), "error: ProviderInternal status=200"),
    ],
)
async def test_transient_errors_fail_one_check(
    start_mock: Any, make_client: Any, enqueue: Any, evidence: str
) -> None:
    mock = await start_mock("openai")
    mock.enqueue(200, completion(usage={"prompt_tokens": 20, "completion_tokens": 1}))  # the baseline
    enqueue(mock)  # the first tool_roundtrip call
    report = await probe_capabilities(make_client(mock, retry_attempts=1))
    assert _status(report, "tool_roundtrip") == ("fail", (evidence,))


async def test_inconsistent_usage_shows_in_the_evidence(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    usage = {"prompt_tokens": 20, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 500}}
    mock.enqueue(200, completion(usage=usage))  # the baseline
    evidence = _status(await probe_capabilities(make_client(mock)), "usage_cached_tokens")[1]
    assert any(e.startswith("usage counts inconsistent on 1/") for e in evidence)


# baseline
async def test_a_rejected_baseline_is_reported_once(start_mock: Any, make_client: Any) -> None:
    # A strict OpenAI-compatible gateway that does not know `store`: every request is a 400.
    mock = await start_mock("openai", unknown_fields=frozenset({"store"}))
    with pytest.raises(ProbeAborted, match="wire options") as info:
        await probe_capabilities(make_client(mock))
    assert (info.value.error_class, info.value.status, len(mock.requests)) == ("BadRequest", 400, 1)
    # declared in the profile (an internal host), the same gateway probes clean
    assert _passed(await probe_capabilities(make_client(mock, wire={"send_store": False}))) == set(CHECKS)


async def test_an_endpoint_wide_404_aborts(start_mock: Any, make_profile: Any) -> None:
    mock = await start_mock("anthropic")
    # an anthropic_messages base_url must not end in /v1: every request goes to a 404
    profile = make_profile("anthropic_messages", mock.base_url + "/v1")
    async with LLMClient.from_profile(profile, egress=EgressPolicy("dev")) as client:
        with pytest.raises(ProbeAborted) as info:
            await probe_capabilities(client)
    assert (info.value.error_class, info.value.status, len(mock.requests)) == ("BadRequest", 404, 1)


async def test_an_html_baseline_aborts_with_a_typed_error(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    mock.enqueue_text(200, "<html>Sign in</html>")
    with pytest.raises(ProbeAborted) as info:
        await probe_capabilities(make_client(mock, retry_attempts=1))
    assert (info.value.error_class, info.value.status) == ("ProviderInternal", 200)


# declared limits
async def test_requests_fit_the_declared_limits(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock, capabilities=ALL_CAPABILITIES | {"max_output": 1024})
    assert _passed(await probe_capabilities(client)) == set(CHECKS)
    assert {r.json["max_completion_tokens"] for r in mock.requests} == {1024}
    assert client.counters.denied == 0
    sent = len(mock.requests)
    client = make_client(mock, capabilities=ALL_CAPABILITIES | {"max_context": 2048})
    assert _passed(await probe_capabilities(client)) == set(CHECKS)
    tokens = [r.json["max_completion_tokens"] for r in mock.requests[sent:]]
    assert (
        max(row["estimated_input_tokens"] + n for row, n in zip(client.call_log, tokens, strict=True)) <= 2048
    )
    assert max(tokens) < PROBE_MAX_OUTPUT_TOKENS


async def test_a_local_refusal_aborts_instead_of_reporting_unsupported(
    start_mock: Any, make_client: Any
) -> None:
    mock = await start_mock("openai")
    sizing = make_client(mock)
    await probe_capabilities(sizing)
    baseline = sizing.call_log[0]["estimated_input_tokens"]
    sent = len(mock.requests)
    # room for the baseline's reply, none for the larger first tool_roundtrip request
    client = make_client(mock, capabilities=ALL_CAPABILITIES | {"max_context": baseline + 1})
    with pytest.raises(ProbeAborted, match="check tool_roundtrip was refused before sending") as info:
        await probe_capabilities(client)
    assert (info.value.error_class, len(mock.requests)) == ("ContextTooLong", sent + 1)
    tiny = make_client(mock, capabilities=ALL_CAPABILITIES | {"max_context": 10})
    with pytest.raises(ProbeAborted, match="baseline request was refused before sending"):
        await probe_capabilities(tiny)
    assert len(mock.requests) == sent + 1
