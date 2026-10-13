"""Credential canary: no environment variable an SDK reads may reach the wire (Appendix E).

Every variable the ``openai`` and ``anthropic`` constructors consult (keys, tokens, base URLs,
organisation/project ids, webhook secrets, custom headers, profiles), plus the proxy variables
``httpx`` would honour, is set to a canary value. A client built from a profile must then send
exactly the profile's credential to the profile's endpoint, and nothing carrying a canary to
anyone. The canary server stands behind every environment base URL and proxy, so any use of
them shows up as a request there.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from deltasleuth.llm.adapters.base import PLACEHOLDER_CREDENTIAL
from deltasleuth.llm.model import LLMRequest, Message, ToolSpec

from .conftest import SECRET_VALUE, SYN

CANARY = "CANARY-9d41"
PAYLOAD = "PAYLOAD-7e2b"
LOOKUP = ToolSpec("lookup", "Look up a value.", {"type": "object", "properties": {"key": {"type": "string"}}})


def _canary_env(canary_origin: str) -> dict[str, str]:
    def c(name: str) -> str:
        return f"{CANARY}-{name}"

    proxy = f"{canary_origin}/{CANARY}-proxy"
    return {
        "OPENAI_API_KEY": c("openai-key"),
        "OPENAI_ADMIN_KEY": c("openai-admin-key"),
        "OPENAI_ORG_ID": c("org"),
        "OPENAI_PROJECT_ID": c("project"),
        "OPENAI_WEBHOOK_SECRET": c("openai-webhook"),
        "OPENAI_BASE_URL": f"{canary_origin}/{CANARY}-openai-base/v1",
        "OPENAI_CUSTOM_HEADERS": "\n".join(
            [f"X-Canary: {c('x')}", f"User-Agent: {c('ua')}", f"Authorization: Bearer {c('auth')}"]
        ),
        "OPENAI_LOG": "debug",
        "ANTHROPIC_API_KEY": c("anthropic-key"),
        "ANTHROPIC_AUTH_TOKEN": c("anthropic-token"),
        "ANTHROPIC_BASE_URL": f"{canary_origin}/{CANARY}-anthropic-base",
        "ANTHROPIC_CUSTOM_HEADERS": "\n".join(
            [
                f"X-Canary: {c('x')}",
                f"anthropic-version: {c('version')}",
                f"x-api-key: {c('x-api-key')}",
                f"Content-Type: text/{CANARY}",
            ]
        ),
        "ANTHROPIC_PROFILE": c("profile"),
        "ANTHROPIC_WEBHOOK_SIGNING_KEY": c("signing-key"),
        "ANTHROPIC_LOG": "debug",
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "ALL_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "all_proxy": proxy,
        "NO_PROXY": "",
        "no_proxy": "",
    }


@pytest.fixture
async def canary(start_mock: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    server = await start_mock("canary")
    for name, value in _canary_env(server.origin).items():
        monkeypatch.setenv(name, value)
    return server


async def _tool_round_trip(client: Any) -> None:
    ask = LLMRequest((Message.user(f"Call lookup for {PAYLOAD}."),), (LOOKUP,), data_classes=SYN)
    first = await client.complete(ask)
    results = Message.tool_result(first.tool_calls[0].id, "42")
    second = await client.complete(
        LLMRequest((*ask.messages, first.message, results), (LOOKUP,), data_classes=SYN)
    )
    assert second.text == "The value is 42"


@pytest.mark.parametrize(
    ("kind", "sdk_logger", "auth_header", "auth_value"),
    [
        ("openai", "openai", "Authorization", f"Bearer {SECRET_VALUE}"),
        ("anthropic", "anthropic", "x-api-key", SECRET_VALUE),
    ],
)
async def test_no_environment_value_reaches_the_wire(
    canary: Any,
    start_mock: Any,
    make_client: Any,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    sdk_logger: str,
    auth_header: str,
    auth_value: str,
) -> None:
    logging.getLogger(sdk_logger).setLevel(logging.DEBUG)  # what *_LOG=debug does at SDK import
    caplog.set_level(logging.DEBUG)
    mock = await start_mock(kind, expected_key=SECRET_VALUE)
    client = make_client(mock)  # built after the canaries are in the environment
    await _tool_round_trip(client)

    assert canary.requests == []  # no environment base URL or proxy was used
    assert len(mock.requests) == 2
    for request in mock.requests:
        wire = request.everything()
        assert CANARY not in wire
        assert PLACEHOLDER_CREDENTIAL not in wire
        assert request.header(auth_header) == auth_value
        assert request.header("X-Canary") is None
    assert logging.getLogger(sdk_logger).level == logging.WARNING
    assert PAYLOAD not in caplog.text
    assert SECRET_VALUE not in caplog.text


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_profile_without_credential_sends_none(
    canary: Any, start_mock: Any, make_client: Any, kind: str
) -> None:
    mock = await start_mock(kind)
    await _tool_round_trip(make_client(mock, auth=False))
    assert canary.requests == []
    for request in mock.requests:
        assert CANARY not in request.everything()
        assert request.header("Authorization") is None
        assert request.header("x-api-key") is None
        assert request.header("OpenAI-Organization") is None
