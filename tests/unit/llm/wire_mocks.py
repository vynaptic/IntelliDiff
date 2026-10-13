"""Local aiohttp emulations of the two wire APIs the LLM port speaks.

``OpenAIMock`` serves ``POST /v1/chat/completions`` and ``AnthropicMock`` ``POST /v1/messages``,
with their tool, usage and error shapes. The "model" is deterministic: it calls a tool the last
user text names ("twice" asks for two calls, where parallel calls are allowed) and answers a tool
result with "The value is <result>". Variants switch features off to emulate weaker endpoints.
Every request is recorded verbatim for leak scans; queued replies are served first.

Loaded by path from ``conftest.py`` (and the CLI's tests): the test tree is not a package.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer


@dataclass
class Recorded:
    method: str
    path: str
    query: str
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    def everything(self) -> str:
        """All request bytes as text, for leak scanning."""
        lines = [f"{self.method} {self.path}?{self.query}", *(f"{k}: {v}" for k, v in self.headers)]
        return "\n".join(lines) + "\n" + self.body.decode("utf-8", "replace")


@dataclass(frozen=True)
class _Text:
    text: str
    content_type: str


class MockServer:
    """Records requests; replays queued responses first, then emulates the API at ``route``."""

    route = "/"

    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self.queue: list[tuple[int, dict[str, str], Any]] = []
        self.counter = 0
        self.delay_s = 0.0  # sleep before answering (timeouts, concurrency)
        self.in_flight = self.max_in_flight = 0
        self._server: TestServer | None = None

    async def start(self) -> MockServer:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        return self

    @property
    def origin(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.port}"

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    def enqueue(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.queue.append((status, headers or {}, body))

    def enqueue_text(self, status: int, text: str, content_type: str = "text/html") -> None:
        """Replay a non-JSON body, e.g. an SSO proxy's login page."""
        self.queue.append((status, {}, _Text(text, content_type)))

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        headers = list(request.headers.items())
        self.requests.append(Recorded(request.method, request.path, request.query_string, headers, body))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay_s)
            if self.queue:
                status, reply_headers, payload = self.queue.pop(0)
                if isinstance(payload, _Text):
                    return web.Response(text=payload.text, content_type=payload.content_type, status=status)
                return web.json_response(payload, status=status, headers=reply_headers)
            if request.path != self.route or request.method != "POST":
                return web.json_response({"error": "not found"}, status=404)
            self.counter += 1
            status, payload = self.respond(json.loads(body), request, len(body))
            return web.json_response(payload, status=status)
        finally:
            self.in_flight -= 1

    def respond(self, body: dict[str, Any], request: web.Request, size: int) -> tuple[int, Any]:
        return 200, {}


def synth(schema: dict[str, Any]) -> Any:
    """A value valid against the simple schemas the tests use."""
    if schema.get("type") == "object":
        return {k: synth(v) for k, v in schema.get("properties", {}).items()}
    return {"string": "ok", "integer": 7, "number": 1.5, "boolean": True, "array": []}.get(
        schema.get("type"), "ok"
    )


def arguments(tool: dict[str, Any]) -> dict[str, Any]:
    return synth(tool.get("input_schema") or tool.get("parameters") or {})


# ---------------------------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------------------------


@dataclass
class OpenAIVariant:
    tools: bool = True
    parallel: bool = True
    cached_tokens: bool = True
    usage: bool = True  # False: replies carry no usage block
    unknown_fields: frozenset[str] = frozenset()  # body fields a strict gateway rejects with 400
    expected_key: str | None = None
    context_limit_bytes: int | None = None


def openai_error(status: int, code: str) -> tuple[int, Any]:
    return status, {"error": {"message": f"mock error {code}", "type": "invalid_request_error", "code": code}}


class OpenAIMock(MockServer):
    route = "/v1/chat/completions"

    def __init__(self, variant: OpenAIVariant | None = None) -> None:
        super().__init__()
        self.variant = variant or OpenAIVariant()
        self._seen_system: set[str] = set()

    @property
    def base_url(self) -> str:
        return f"{self.origin}/v1"

    def respond(self, body: dict[str, Any], request: web.Request, size: int) -> tuple[int, Any]:
        v = self.variant
        if v.expected_key is not None and request.headers.get("Authorization") != f"Bearer {v.expected_key}":
            return openai_error(401, "invalid_api_key")
        code = self._rejection(body, size)
        if code is not None:
            return openai_error(400, code)
        content, calls = self._decide(body)
        return 200, self._completion(body, content, calls)

    def _rejection(self, body: dict[str, Any], size: int) -> str | None:
        v = self.variant
        if v.unknown_fields & set(body):  # Azure-style strict validation of the body
            return "unrecognized_request_argument"
        if v.context_limit_bytes is not None and size > v.context_limit_bytes:
            return "context_length_exceeded"
        if body.get("tools") and not v.tools:
            return "unsupported_parameter"
        return None

    def _decide(self, body: dict[str, Any]) -> tuple[str | None, list[Any]]:
        tools = {t["function"]["name"]: t["function"] for t in body.get("tools") or []}
        choice = body.get("tool_choice", "auto")
        last = body["messages"][-1]
        text = last["content"] if isinstance(last.get("content"), str) else ""
        if tools and choice != "none":
            if last["role"] == "tool":
                return f"The value is {last['content']}", []
            named = [spec for name, spec in tools.items() if name in text]
            if named:
                twice = "twice" in text and body.get("parallel_tool_calls", True) and self.variant.parallel
                return None, [self._call(named[0], i) for i in range(2 if twice else 1)]
        return "ready", []

    def _call(self, spec: dict[str, Any], index: int) -> dict[str, Any]:
        function = {"name": spec["name"], "arguments": json.dumps(arguments(spec))}
        return {"id": f"call_{self.counter}_{index}", "type": "function", "function": function}

    def _completion(self, body: dict[str, Any], content: str | None, calls: list[Any]) -> dict[str, Any]:
        v = self.variant
        message: dict[str, Any] = {"role": "assistant", "content": content}
        message |= {"tool_calls": calls} if calls else {}
        prompt = len(json.dumps(body["messages"])) // 4 + 1
        usage: dict[str, Any] = {
            "prompt_tokens": prompt,
            "completion_tokens": 12,
            "total_tokens": prompt + 12,
        }
        if v.cached_tokens:
            system = "".join(m["content"] for m in body["messages"] if m["role"] == "system")
            cached = min(len(system) // 4, prompt) if system in self._seen_system else 0
            self._seen_system.add(system)
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
            usage["completion_tokens_details"] = {"reasoning_tokens": 4}
        choice = {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
        completion = {
            "id": f"chatcmpl-mock-{self.counter}",
            "object": "chat.completion",
            "created": 1_758_700_000,
            "model": body["model"] + "-2026-09-01",
            "choices": [choice],
        }
        return completion | ({"usage": usage} if v.usage else {})


# ---------------------------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------------------------


@dataclass
class AnthropicVariant:
    cache_ttl_1h_share: int = 0  # tokens of each cache write reported as 1-hour writes
    expected_key: str | None = None
    context_limit_bytes: int | None = None


def anthropic_error(status: int, message: str) -> tuple[int, Any]:
    return status, {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


class AnthropicMock(MockServer):
    route = "/v1/messages"

    def __init__(self, variant: AnthropicVariant | None = None) -> None:
        super().__init__()
        self.variant = variant or AnthropicVariant()
        self._seen_system: set[str] = set()
        self._signed: dict[str, str] = {}  # tool_use id -> signature of the thinking issued with it

    @property
    def base_url(self) -> str:
        return self.origin

    def respond(self, body: dict[str, Any], request: web.Request, size: int) -> tuple[int, Any]:
        v = self.variant
        if not request.headers.get("anthropic-version"):
            return anthropic_error(400, "anthropic-version header is required")
        if v.expected_key is not None and request.headers.get("x-api-key") != v.expected_key:
            return 401, {
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            }
        rejection = self._rejection(body, size)
        if rejection is not None:
            return anthropic_error(400, rejection)
        last = body["messages"][-1]
        text = " ".join(b.get("text", "") for b in last["content"] if b["type"] == "text")
        results = [b for b in last["content"] if b["type"] == "tool_result"]
        content, stop = self._decide(body, text, results)
        if "MOCK_REFUSE" in text:
            content, stop = [], "refusal"
        elif "MOCK_CONTEXT_WINDOW" in text:
            stop = "model_context_window_exceeded"
        thinking = (body.get("thinking") or {}).get("type") != "disabled"
        if thinking:
            signature = f"sig-{self.counter}"
            self._signed |= {b["id"]: signature for b in content if b["type"] == "tool_use"}
            content = [{"type": "thinking", "thinking": "", "signature": signature}, *content]
        return 200, self._message(body, content, stop, thinking)

    def _rejection(self, body: dict[str, Any], size: int) -> str | None:
        messages = body["messages"]
        final = len(messages) - 1
        if any(
            not m["content"] and not (i == final and m["role"] == "assistant") for i, m in enumerate(messages)
        ):
            return "all messages must have non-empty content except for the optional final assistant message"
        if self.variant.context_limit_bytes is not None and size > self.variant.context_limit_bytes:
            return "prompt is too long: 250000 tokens > 200000 maximum"
        # Every tool_use issued after signed thinking must come back after that block, unmodified.
        for msg in (m for m in messages if m["role"] == "assistant"):
            seen: dict[str, str] = {}
            for block in msg["content"]:
                if block["type"] == "thinking":
                    seen[block["signature"]] = block["thinking"]
                elif block["type"] == "tool_use" and block["id"] in self._signed:
                    if seen.get(self._signed[block["id"]]) != "":  # the mock issues empty thinking
                        return "thinking block missing or modified"
        return None

    def _decide(self, body: dict[str, Any], text: str, results: list[Any]) -> tuple[list[Any], str]:
        tools = {t["name"]: t for t in body.get("tools") or []}
        choice = body.get("tool_choice") or {"type": "auto"}
        if tools and choice["type"] != "none":
            if results:
                return [{"type": "text", "text": f"The value is {results[0]['content']}"}], "end_turn"
            named = [spec for name, spec in tools.items() if name in text]
            if named:
                count = 2 if "twice" in text and not choice.get("disable_parallel_tool_use") else 1
                return [self._tool_use(named[0], i) for i in range(count)], "tool_use"
        return [{"type": "text", "text": "ready"}], "end_turn"

    def _tool_use(self, spec: dict[str, Any], index: int) -> dict[str, Any]:
        return {"type": "tool_use", "id": f"toolu_{self.counter}_{index}", "name": spec["name"],
                "input": arguments(spec)}  # fmt: skip

    def _message(self, body: dict[str, Any], content: list[Any], stop: str, thinking: bool) -> dict[str, Any]:
        system = "".join(b["text"] for b in body.get("system") or [])
        total = len(json.dumps(body["messages"])) // 4 + len(system) // 4 + 1
        usage: dict[str, Any] = {"input_tokens": total, "output_tokens": 20}
        usage |= {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        if body.get("cache_control") and system:
            prefix = len(system) // 4
            write = 0 if system in self._seen_system else prefix
            self._seen_system.add(system)
            w1h = min(self.variant.cache_ttl_1h_share, write)
            usage |= {
                "input_tokens": total - prefix,
                "cache_read_input_tokens": prefix - write,
                "cache_creation_input_tokens": write,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": write - w1h,
                    "ephemeral_1h_input_tokens": w1h,
                },
            }
        if thinking:
            usage["output_tokens_details"] = {"thinking_tokens": 5}
        return {
            "id": f"msg_mock_{self.counter}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": "cyber"} if stop == "refusal" else None,
            "usage": usage,
        }
