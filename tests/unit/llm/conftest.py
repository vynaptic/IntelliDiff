"""Fixtures for the LLM port tests: local wire mocks, profiles through the real parser, clients."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import stamina

from deltasleuth.llm.client import LLMClient
from deltasleuth.llm.model import DataClass, LLMRequest, Message
from deltasleuth.llm.policy import EgressPolicy
from deltasleuth.llm.profiles import LLMProfile, parse_profiles


def _load_wire_mocks() -> Any:
    """Loaded by path (the test tree is not a package), under the name the CLI tests use too."""
    name = "deltasleuth_llm_wire_mocks"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("wire_mocks.py"))
        assert spec is not None and spec.loader is not None
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


wire_mocks = _load_wire_mocks()

SECRET_VALUE = "sk-test-0123456789abcdef"  # noqa: S105 - fake key for local mocks
PROFILE_ID = "test-profile"
SYN = frozenset({DataClass.SYNTHETIC})
ALL_CAPABILITIES: dict[str, Any] = {"tool_calling": True, "prompt_caching": "automatic"}


def req(text: str = "hi", **kwargs: Any) -> LLMRequest:
    """A synthetic request with a system prompt."""
    return LLMRequest(
        messages=(Message.system("You are terse."), Message.user(text)), data_classes=SYN, **kwargs
    )


def completion(content: str | None = "ready", *, usage: dict[str, Any] | None = None, **message: Any) -> Any:
    """A Chat Completions reply to queue on the OpenAI mock."""
    body: dict[str, Any] = {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, **message},
            }
        ],
    }
    return body | ({"usage": usage} if usage is not None else {})


@pytest.fixture(autouse=True)
def _no_retry_waits() -> Iterator[None]:
    """Keep the client's attempt count but never sleep between attempts."""
    with stamina.set_testing(True, attempts=10, cap=True):
        yield


@pytest.fixture
async def start_mock() -> AsyncIterator[Callable[..., Awaitable[Any]]]:
    """``await start_mock("openai" | "anthropic" | "canary", **variant_fields)``."""
    servers: list[Any] = []

    async def start(kind: str, **variant: Any) -> Any:
        if kind == "openai":
            server = wire_mocks.OpenAIMock(wire_mocks.OpenAIVariant(**variant))
        elif kind == "anthropic":
            server = wire_mocks.AnthropicMock(wire_mocks.AnthropicVariant(**variant))
        else:
            server = wire_mocks.MockServer()
        servers.append(await server.start())
        return server

    yield start
    for server in servers:
        await server.close()


@pytest.fixture
def secret_ref(tmp_path: Path) -> str:
    path = tmp_path / "llm.key"
    path.write_text(SECRET_VALUE + "\n", encoding="utf-8")
    return f"file:{path}"


def profile_doc(dialect: str, base_url: str, **overrides: Any) -> dict[str, Any]:
    """A profiles document with one profile on a loopback mock (internal unless ``hosts`` says)."""
    hosts = overrides.pop("hosts", {"127.0.0.1": "internal"})
    capabilities = dict(ALL_CAPABILITIES)
    if dialect == "anthropic_messages":  # what claude-opus-5 supports
        capabilities |= {"prompt_caching": "explicit"}
    profile: dict[str, Any] = {
        "dialect": dialect,
        "base_url": base_url,
        "model": "claude-opus-5" if dialect == "anthropic_messages" else "gpt-test",
        "data_classes": [c.value for c in DataClass],
        "environments": ["dev", "test", "eval", "prod"],
        "capabilities": capabilities,
        "pricing": {
            "version": "test-2026-09",
            "currency": "USD",
            "input_per_mtok": 5,
            "output_per_mtok": 25,
            "cache_read_mult": 0.1,
            "cache_write_mult": 1.25,
            "cache_write_1h_mult": 2,
        },
    }
    return {"hosts": hosts, "profiles": {PROFILE_ID: profile | overrides}}


@pytest.fixture
def make_profile(secret_ref: str) -> Callable[..., LLMProfile]:
    """``make_profile(dialect, base_url, auth=True, **overrides)``."""

    def build(dialect: str, base_url: str, *, auth: bool = True, **overrides: Any) -> LLMProfile:
        if auth:
            overrides = {"auth_ref": secret_ref, "allow_insecure_localhost": True} | overrides
        return parse_profiles(profile_doc(dialect, base_url, **overrides)).get(PROFILE_ID)

    return build


@pytest.fixture
async def make_client(make_profile: Callable[..., LLMProfile]) -> AsyncIterator[Callable[..., LLMClient]]:
    """``make_client(mock, environment="dev", retry_attempts=3, **profile_overrides)``."""
    clients: list[LLMClient] = []

    def build(mock: Any, *, environment: str = "dev", retry_attempts: int = 3, **overrides: Any) -> LLMClient:
        dialect = "anthropic_messages" if isinstance(mock, wire_mocks.AnthropicMock) else "openai_chat"
        profile = make_profile(dialect, mock.base_url, **overrides)
        client = LLMClient.from_profile(
            profile, egress=EgressPolicy(environment), retry_attempts=retry_attempts
        )
        clients.append(client)
        return client

    yield build
    for client in clients:
        await client.aclose()
