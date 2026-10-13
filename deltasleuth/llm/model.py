"""Canonical messages, requests, responses, usage and errors of the ``ds_llm`` port.

Adapters map this model to and from one wire dialect. Payload fields (text, tool arguments and
results, reasoning state) are ``repr=False`` and errors never carry them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

# Identifiers both wire APIs accept as tool names.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "content_filter", "other"]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class DataClass(StrEnum):
    """What a request carries; the egress policy decides where each class may go."""

    SYNTHETIC = "synthetic"
    GOLDEN = "golden"
    SCHEMA_SUMMARY = "schema_summary"  # field names, types and shapes, never values
    AGGREGATES = "aggregates"  # counts and rates, no per-key values
    PRODUCTION_TOKENISED = "production_tokenised"


@dataclass(frozen=True)
class TextPart:
    text: str = field(repr=False)


@dataclass(frozen=True)
class ToolCallPart:
    """A tool call. Arguments that are not a JSON object are kept as ``raw_arguments`` (and
    ``arguments`` is empty) so the caller can send a repair result."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)
    raw_arguments: str | None = field(default=None, repr=False)

    @property
    def arguments_valid(self) -> bool:
        return self.raw_arguments is None


@dataclass(frozen=True)
class ToolResultPart:
    tool_call_id: str
    content: str = field(repr=False)
    is_error: bool = False


@dataclass(frozen=True)
class ReasoningPart:
    """Opaque provider state (e.g. signed thinking), sent back verbatim by the same dialect."""

    provider: str
    state: Mapping[str, Any] = field(repr=False)


Part = TextPart | ToolCallPart | ToolResultPart | ReasoningPart

_ALLOWED_PARTS: dict[Role, tuple[type, ...]] = {
    Role.SYSTEM: (TextPart,),
    Role.USER: (TextPart,),
    Role.ASSISTANT: (TextPart, ToolCallPart, ReasoningPart),
    Role.TOOL: (ToolResultPart,),
}


@dataclass(frozen=True)
class Message:
    role: Role
    parts: tuple[Part, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", Role(self.role))
        object.__setattr__(self, "parts", tuple(self.parts))
        if not self.parts:
            raise ValueError(f"a {self.role} message needs at least one part")
        for part in self.parts:
            if not isinstance(part, _ALLOWED_PARTS[self.role]):
                raise ValueError(f"{type(part).__name__} is not allowed in a {self.role} message")

    @classmethod
    def system(cls, text: str) -> Message:
        return cls(Role.SYSTEM, (TextPart(text),))

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(Role.USER, (TextPart(text),))

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(Role.ASSISTANT, (TextPart(text),))

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str, *, is_error: bool = False) -> Message:
        return cls(Role.TOOL, (ToolResultPart(tool_call_id, content, is_error),))

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> tuple[ToolCallPart, ...]:
        return tuple(p for p in self.parts if isinstance(p, ToolCallPart))

    @property
    def reasoning(self) -> tuple[ReasoningPart, ...]:
        return tuple(p for p in self.parts if isinstance(p, ReasoningPart))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"tool name must match {NAME_PATTERN.pattern}")
        if self.input_schema.get("type") != "object":
            raise ValueError("a tool input_schema must describe an object (type: object)")


ToolChoice = Literal["auto", "none"]


@dataclass(frozen=True)
class LLMRequest:
    """One model call; the model never makes parallel tool calls. ``data_classes`` is what the
    egress policy checks; an empty set is denied."""

    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice = "auto"
    max_output_tokens: int = 4096
    data_classes: frozenset[DataClass] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "data_classes", frozenset(DataClass(c) for c in self.data_classes))
        names = [t.name for t in self.tools]
        _require(bool(self.messages), "a request needs at least one message")
        _require(self.max_output_tokens >= 1, "max_output_tokens must be positive")
        _require(len(names) == len(set(names)), "tool names must be unique")
        _require(self.tool_choice in ("auto", "none"), "tool_choice must be auto or none")


def _require(condition: bool, problem: str) -> None:
    if not condition:
        raise ValueError(problem)


@dataclass(frozen=True)
class Usage:
    """Token usage with OpenTelemetry meaning: ``input`` includes cache reads and writes and
    ``output`` includes reasoning. ``consistent`` is False when the endpoint's counts disagreed
    and were resolved upward."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0
    reported: bool = True
    cache_reported: bool = False
    consistent: bool = True

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def uncached_input(self) -> int:
        return self.input - self.cache_read - self.cache_write


@dataclass(frozen=True)
class LLMResponse:
    message: Message
    stop_reason: StopReason
    usage: Usage
    model_reported: str | None = None
    response_id: str | None = None
    provider_stop: str | None = None
    ignored_tool_calls: int = 0  # calls of a tool kind this port never offers
    latency_ms: float | None = None

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def tool_calls(self) -> tuple[ToolCallPart, ...]:
        return self.message.tool_calls


def safe_code(value: object) -> str | None:
    """A provider code or id if it looks like an identifier; free text may echo the prompt."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


class LLMError(Exception):
    """Base of every port failure. ``error_class`` uses the call-log vocabulary; the text never
    contains payload."""

    default_error_class = "ProviderInternal"

    def __init__(
        self,
        detail: str,
        *,
        error_class: str | None = None,
        status: int | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.detail = detail
        self.error_class = error_class or self.default_error_class
        self.status = status
        self.provider_code = safe_code(provider_code)
        self.request_id = safe_code(request_id)
        self.retry_after = retry_after
        extras = [
            f"{k}={v}"
            for k, v in (("status", status), ("code", self.provider_code), ("request_id", self.request_id))
            if v is not None
        ]
        super().__init__(f"{self.error_class}: {detail}" + (f" ({', '.join(extras)})" if extras else ""))


class RateLimited(LLMError):
    default_error_class = "RateLimited"


class Overloaded(LLMError):
    default_error_class = "Overloaded"


class AuthFailed(LLMError):
    """401 (``Auth``) or 403 (``PermissionDenied``); never retried."""

    default_error_class = "Auth"


class BadRequest(LLMError):
    default_error_class = "BadRequest"


class ContextTooLong(BadRequest):
    default_error_class = "ContextTooLong"


class ProviderError(LLMError):
    """Provider or transport failure: ``ProviderInternal``, ``Timeout`` or ``Connection``."""


class CapabilityUnavailable(BadRequest):
    """The profile or the dialect cannot carry the request."""


class EgressDenied(LLMError):
    """The egress policy refused the call before any network I/O."""

    default_error_class = "PolicyDenied"

    def __init__(self, detail: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(detail)


class BudgetExceeded(LLMError):
    """A budget refused the call before any network I/O."""

    default_error_class = "PolicyDenied"

    def __init__(self, budget: str, limit: float, requested: float) -> None:
        self.budget, self.limit, self.requested = budget, limit, requested
        super().__init__(f"budget {budget} exceeded ({requested:g} > {limit:g})")
