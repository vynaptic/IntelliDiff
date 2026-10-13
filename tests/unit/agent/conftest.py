"""Agent fixtures: a Context on a fresh database holding one run seeded through the engine, and
scripted LLM servers (Chat Completions and Messages wire) on 127.0.0.1."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from psycopg_pool import AsyncConnectionPool

from deltasleuth.config import Settings
from deltasleuth.connectors.base import utc_iso
from deltasleuth.context import Context
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.classify.rootcause import root_causes
from deltasleuth.core.spec import TargetRegistry, compile_spec
from deltasleuth.core.types import Observation, Side, Target, TargetCapabilities, TruncationIndicators
from deltasleuth.llm.profiles import parse_profiles
from deltasleuth.runner.compare import save_pass
from deltasleuth.security.tokenise import Tokeniser, mask_key
from deltasleuth.store import repo
from deltasleuth.store.db import migrate
from tests.conftest import TEST_KEYRING, TESTER, create_run, evidence_store

PROFILE = "agent-test"
RAW_KEY, MATCH_KEY = "C1001", "C1002"
RAW_EMAIL = "ann@example.org"
SPEC_DOC: dict[str, Any] = {
    "apiVersion": "deltasleuth/v1",
    "kind": "ComparisonSpec",
    "metadata": {"name": "agent-demo", "version": "1.0.0", "owner": "tests"},
    "normalizerVersion": "1.1.0",
    "key": {
        "name": "id",
        "type": "string",
        "pattern": "^C[0-9]{4}$",
        "class": "identifier",
        "source": {"kind": "list", "keys": [RAW_KEY]},
    },
    "sides": {
        side: {"role": side, "target": f"svc-{side}", "request": {"method": "GET", "path": "/x/{{ key }}"}}
        for side in "ab"
    },
    "compare": {},
    "recheck": {"delay": "1s", "times": 1},
    "security": {
        "tokenize": [{"path": "$.email", "class": "email"}, {"path": "$.id", "class": "identifier"}],
        "clear": ["$.total", "$.note"],
    },
}
_CAPS = TargetCapabilities(truncation_indicators=TruncationIndicators(cap=100, items_path="$.items"))
TARGETS = TargetRegistry(
    {t: Target(t, f"https://{t}.example", capabilities=_CAPS) for t in ("svc-a", "svc-b")}
)


Call = tuple[str, dict[str, Any]]


class ScriptedLLM:
    """An LLM endpoint in one wire dialect replaying scripted turns (text, tool calls and, when not
    the natural one, a stop reason); records every request body and the most requests in flight
    (each held ``delay_s``). An exhausted script answers 400, which the client does not retry."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.turns: list[tuple[str, tuple[Call, ...], str | None]] = []
        self.requests: list[dict[str, Any]] = []
        self.delay_s = 0.0
        self.in_flight = self.max_in_flight = 0
        self._server: TestServer | None = None

    def say(self, text: str = "", *calls: Call, stop: str | None = None) -> ScriptedLLM:
        self.turns.append((text, calls, stop))
        return self

    async def start(self) -> ScriptedLLM:
        app = web.Application()
        route = "/v1/chat/completions" if self.dialect == "openai_chat" else "/v1/messages"
        app.router.add_post(route, self._handle)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        return self

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        origin = f"http://127.0.0.1:{self._server.port}"
        return origin + "/v1" if self.dialect == "openai_chat" else origin

    def sent(self) -> str:
        return json.dumps(self.requests, ensure_ascii=False)

    def tool_results(self, n: int) -> list[str]:
        """The tool-result contents request ``n`` carried, in either wire."""
        messages = self.requests[n]["messages"]
        found = [m["content"] for m in messages if m["role"] == "tool"]
        blocks = [b for m in messages if isinstance(m["content"], list) for b in m["content"]]
        return found + [b["content"] for b in blocks if b.get("type") == "tool_result"]

    def tool_choice(self, n: int) -> str:
        choice = self.requests[n]["tool_choice"]
        return choice if isinstance(choice, str) else choice["type"]

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(self.delay_s)
        self.in_flight -= 1
        if not self.turns:
            return web.json_response({"error": {"message": "script exhausted", "type": "x"}}, status=400)
        text, calls, stop = self.turns.pop(0)
        n = len(self.requests)
        reply = _openai if self.dialect == "openai_chat" else _anthropic
        return web.json_response(
            reply(text, [(f"call_{n}_{i}", name, a) for i, (name, a) in enumerate(calls)], stop)
        )


def _openai(text: str, calls: list[tuple[str, str, dict[str, Any]]], stop: str | None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [
            {"id": i, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for i, name, args in calls
        ]
    choice = {"index": 0, "message": message, "finish_reason": stop or ("tool_calls" if calls else "stop")}
    usage = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [choice],
        "usage": usage,
    }


def _anthropic(text: str, calls: list[tuple[str, str, dict[str, Any]]], stop: str | None) -> dict[str, Any]:
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "id": i, "name": name, "input": args} for i, name, args in calls]
    return {
        "id": "msg", "type": "message", "role": "assistant", "model": "m", "content": content,
        "stop_reason": stop or ("tool_use" if calls else "end_turn"), "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }  # fmt: skip


@pytest.fixture
async def scripted() -> AsyncIterator[Callable[[str], Awaitable[ScriptedLLM]]]:
    """``await scripted("openai_chat" | "anthropic_messages")``."""
    servers: list[ScriptedLLM] = []

    async def start(dialect: str) -> ScriptedLLM:
        servers.append(await ScriptedLLM(dialect).start())
        return servers[-1]

    yield start
    for server in servers:
        await server.close()


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(pg_dsn, open=False, max_size=4)
    await pool.open(wait=True)
    await migrate(pool)
    yield pool
    await pool.close()


@pytest.fixture
def make_ctx(pool: AsyncConnectionPool, tmp_path: Path) -> Callable[..., Context]:
    """``make_ctx(llm_server | None, trusted=True, **profile_fields)``: AI off without a server; an
    untrusted host is external to the egress policy."""

    def make(server: ScriptedLLM | None = None, *, trusted: bool = True, **fields: Any) -> Context:
        settings = Settings("file:/unused", tmp_path / "evidence", tmp_path, tmp_path / "targets.yaml",
                            llm_profile=PROFILE, llm_environment="test")  # fmt: skip
        llm = None
        if server is not None:
            profile = {
                "dialect": server.dialect, "base_url": server.base_url, "model": "m",
                "data_classes": ["production_tokenised"], "environments": ["test"],
                "capabilities": {"tool_calling": True},
            } | fields  # fmt: skip
            hosts = {"127.0.0.1": "internal"} if trusted else {}
            llm = parse_profiles({"hosts": hosts, "profiles": {PROFILE: profile}})
        evidence = evidence_store(tmp_path / "evidence")
        return Context(settings, pool, TARGETS, Tokeniser(b"k" * 32), evidence, llm, None, TEST_KEYRING)

    return make


@dataclass(frozen=True)
class Seeded:
    run_id: str
    spec_id: int
    key: str  # masked key of the discrepant key
    findings: dict[str, str]  # path_pattern -> finding_id
    causes: dict[str, str]  # path_pattern -> root_cause_id


async def seed(
    ctx: Context, note_b: str = "same", wide: int = 0, scan: dict[str, Any] | None = None
) -> Seeded:
    """A batch run of two keys compared by the engine: C1001 differs in email, total and note, and
    has ``wide`` more fields on side B only; with ``scan``, the run's table scan state."""
    spec = compile_spec(SPEC_DOC, targets=ctx.targets)
    extra = {f"x{i}": i for i in range(wide)}  # after note and total: their findings come first
    docs = {
        RAW_KEY: ({"id": RAW_KEY, "email": RAW_EMAIL, "total": 10, "note": "same"},
                  {"id": RAW_KEY, "email": "ann@example.com", "total": 12, "note": note_b} | extra),
        MATCH_KEY: ({"id": MATCH_KEY, "total": 3}, {"id": MATCH_KEY, "total": 3}),
    }  # fmt: skip
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, spec, created_by=TESTER.actor)
        keys = {k: mask_key(k, spec, ctx.tokeniser) for k in docs}
        params = {"capabilities": ctx.declared(spec)}
        run_id, sealer = await create_run(ctx, conn, spec_id, "batch", keys, params, scan)
        await repo.set_run_status(conn, run_id, "running")
        results = []
        for key, bodies in docs.items():
            obs = [_observed(ctx, key, side, body) for side, body in zip(Side, bodies, strict=True)]
            states = [
                observe(o, spec.sides[o.side], spec, ctx.targets.get(f"svc-{o.side}").capabilities)
                for o in obs
            ]
            result = compare_pair(key, *states, spec, today=date.today(), tokeniser=ctx.tokeniser)
            result = replace(result, obs_a_id=obs[0].obs_id, obs_b_id=obs[1].obs_id)
            await save_pass(ctx, conn, spec, run_id, 1, 1, result, {o.side: o for o in obs}, sealer)
            await repo.complete_key(conn, run_id, 1, keys[key], "nobody")
            results.append(result)
        rows, _ = await repo.list_findings(conn, run_id)
        causes = {rc.path_pattern: rc for rc in root_causes(results, spec=spec, tokeniser=ctx.tokeniser)}
        links = {r["finding_id"]: causes[r["path_pattern"]].root_cause_id for r in rows}
        await repo.replace_root_causes(conn, run_id, 1, list(causes.values()), links)
        await repo.set_counters(conn, run_id, {"keys": 2, "MATCH": 1, "MISMATCH": 1})
        await repo.set_run_status(conn, run_id, "completed")
    return Seeded(
        run_id,
        spec_id,
        keys[RAW_KEY],
        {r["path_pattern"]: r["finding_id"] for r in rows},
        {p: rc.root_cause_id for p, rc in causes.items()},
    )


def _observed(ctx: Context, key: str, side: Side, body: dict[str, Any]) -> Observation:
    raw = Observation(key, side, 200, json.dumps(body).encode(), utc_iso(datetime.now(UTC)))
    return ctx.evidence.put_blob(raw)
