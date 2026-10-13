"""End-to-end harness: the mock pairs served on 127.0.0.1, a DeltaSleuth stack around them (settings,
a fresh database, a workspace) and a scripted OpenAI-compatible LLM.

Everything runs through the product's own entry points: ``create_run`` with the worker, the API
and the CLI. The mocks know nothing about DeltaSleuth and DeltaSleuth knows nothing about how they
are built: the only contract between them is HTTP plus the capabilities declared in targets.yaml.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import uuid
import zlib
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from aiohttp import web

from deltasleuth.config import load_settings
from deltasleuth.context import Context, init_workspace
from deltasleuth.core.spec import compile_spec, load_spec_file
from deltasleuth.runner import run_worker
from deltasleuth.store import repo
from mocks.registry import load_pair, pair_dir
from tests.conftest import TESTER, MySQLServer, OracleServer, encryption

# Response members whose values belong to a tokenised class in the pair's spec.
SENSITIVE = frozenset(
    "name entityName value firstName lastName dob email number lines line city postcode postalCode accountNo"
    " givenName familyName birthDate".split()
)
LLM_PROFILE = "mock-llm"


class ServedPair:
    """Variants a and b of one mock pair on free loopback ports, on a background event loop (so
    async tests, the API's TestClient and CLI subprocesses can all call them)."""

    def __init__(self, pair: str) -> None:
        self.pair, self.ports = pair, {}
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self._runners = self._run(self._start())

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)

    async def _start(self) -> list[web.AppRunner]:
        runners = []
        for variant in "ab":
            runner = web.AppRunner(load_pair(self.pair).create_app(variant), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            self.ports[variant] = runner.addresses[0][1]
            runners.append(runner)
        return runners

    def targets(self, rate: float | None = None) -> str:
        """The pair's targets.yaml with each target at its variant's port (and ``rate`` if given)."""
        doc = yaml.safe_load((pair_dir(self.pair) / "targets.yaml").read_text(encoding="utf-8"))
        for target_id, entry in doc["targets"].items():
            entry["base_url"] = f"http://127.0.0.1:{self.ports[target_id.rsplit('-', 1)[1]]}"
            entry.update({"rate_per_s": rate} if rate else {})
        return yaml.safe_dump(doc, sort_keys=False)

    def stop(self) -> None:
        async def cleanup() -> None:
            for runner in self._runners:
                await runner.cleanup()

        self._run(cleanup())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)


@pytest.fixture
def serve() -> Iterator[Callable[[str], ServedPair]]:
    """``serve(pair)``: the pair freshly started (its per-key call counters at zero)."""
    started: list[ServedPair] = []

    def start(pair: str) -> ServedPair:
        started.append(ServedPair(pair))
        return started[-1]

    yield start
    for served in started:
        served.stop()


class _LLM(BaseHTTPRequestHandler):
    """A Chat Completions endpoint playing a careful analyst: it asks for the run summary, then
    answers citing the top root cause and a counter the summary returned."""

    requests: list[dict[str, Any]]

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        last = body["messages"][-1]
        message: dict[str, Any] = {"role": "assistant", "content": None}
        if last["role"] == "tool":
            data = json.loads(last["content"])["data"]
            cause, different = data["root_causes"][0]["root_cause_id"], data["counters"]["different"]
            message["content"] = f"{different} keys differ; the most probable root cause is [{cause}]."
        else:
            call = {"name": "get_run_summary", "arguments": "{}"}
            message["tool_calls"] = [
                {"id": f"call_{len(self.requests)}", "type": "function", "function": call}
            ]
        reply = json.dumps({
            "id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }).encode()  # fmt: skip
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def llm() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A scripted LLM: its base URL and the request bodies it received."""
    handler = type("Handler", (_LLM,), {"requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", handler.requests
    server.shutdown()


@dataclass(frozen=True)
class Stack:
    """A configured DeltaSleuth: its settings file (for the API and the CLI) and a Context opener."""

    settings: Path

    @asynccontextmanager
    async def context(self) -> AsyncIterator[Context]:
        async with Context.open(load_settings(self.settings)) as ctx:
            yield ctx


@pytest.fixture
def make_stack(tmp_path: Path, pg_dsn: str) -> Callable[..., Stack]:
    """``make_stack(targets_yaml, llm_url=None, auth=None, notify=None, files=None, mcp=None, **worker)``:
    settings on the test database, initialised; with ``auth`` (the settings block), its encryption secrets
    too; ``notify``: the notification settings; ``files``: the file reader's (docs/mvp/FILES.md §4);
    ``mcp``: the MCP server's (docs/mvp/MCP.md §4)."""

    def make(
        targets: str,
        llm_url: str | None = None,
        auth: dict[str, Any] | None = None,
        notify: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        mcp: dict[str, Any] | None = None,
        **worker: Any,
    ) -> Stack:
        (tmp_path / "dsn").write_text(pg_dsn)
        (tmp_path / "targets.yaml").write_text(targets)
        settings: dict[str, Any] = {
            "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
            "evidence_dir": "evidence",
            "workspace_dir": "workspace",
            "targets_file": "targets.yaml",
            "worker": {"concurrency": 4, "lease_seconds": 30, "poll_seconds": 0.05} | worker,
        }
        if auth is not None:
            settings |= {"auth": auth, "encryption": encryption(tmp_path / "secrets")}
        if notify is not None:
            settings["notify"] = notify
        if files is not None:
            settings["files"] = files
        if mcp is not None:
            settings["mcp"] = mcp
        if llm_url:
            profile = {
                "dialect": "openai_chat", "base_url": llm_url, "model": "m", "environments": ["test"],
                "data_classes": ["production_tokenised"], "capabilities": {"tool_calling": True},
            }  # fmt: skip
            profiles = {"hosts": {"127.0.0.1": "internal"}, "profiles": {LLM_PROFILE: profile}}
            (tmp_path / "llm.yaml").write_text(yaml.safe_dump(profiles))
            settings |= {
                "llm_profiles_file": "llm.yaml",
                "llm_profile": LLM_PROFILE,
                "llm_environment": "test",
            }
        (tmp_path / "deltasleuth.yaml").write_text(yaml.safe_dump(settings))
        init_workspace(load_settings(tmp_path / "deltasleuth.yaml"))
        return Stack(tmp_path / "deltasleuth.yaml")

    return make


async def store_spec(ctx: Context, spec: Path | dict[str, Any]) -> int:
    """Compile and store a spec (a file, or a spec document); its spec_id."""
    body = load_spec_file(spec) if isinstance(spec, Path) else spec
    async with ctx.pool.connection() as conn:
        return await repo.save_spec(conn, compile_spec(body, targets=ctx.targets), created_by=TESTER.actor)


async def finish(ctx: Context, run_id: str, timeout: float = 120) -> repo.Row:
    """Run a worker until the run ends; the run row."""
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(ctx, stop=stop))
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            async with ctx.pool.connection() as conn:
                run = await repo.get_run(conn, run_id)
            if run["status"] in repo.TERMINAL:
                return run
            assert asyncio.get_running_loop().time() < deadline, f"run still {run['status']}"
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await worker


async def raw_keys(ctx: Context, run_id: str) -> dict[str, str]:
    """The current cycle's masked keys -> raw keys, opened with the run's sealer (outputs never show
    raw keys; a test compares them with its own)."""
    async with ctx.pool.connection() as conn:
        rows = await repo.pages(repo.list_keys, conn, run_id)
        sealer = await ctx.sealer(conn, run_id)
    return {row["key_masked"]: sealer.open_row(row)["key"] for row in rows}


def raw_pii(pair: str) -> set[str]:
    """Every value the pair serves at a tokenised member, plus the customer numbers (identifier keys)."""
    dataset = load_pair(pair).build_dataset()
    found: set[str] = set(dataset.keys) if pair == "customer" else set()

    def walk(node: Any, sensitive: bool = False) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                walk(value, name in SENSITIVE)
        elif isinstance(node, list):
            for value in node:
                walk(value, sensitive)
        elif sensitive and isinstance(node, str) and len(node) >= 4:
            found.add(node)

    for variant in "ab":
        for key in dataset.keys:
            for reply in dataset.script(variant, key):
                walk(reply.body)
    return found


def pdf_text(pdf: bytes) -> str:
    """The text a ReportLab PDF shows: its ASCII85 + Flate page streams, decoded, strings joined."""
    streams = re.findall(rb"/ASCII85Decode /FlateDecode \].*?stream\n(.*?)~>endstream", pdf, re.S)
    pages = "".join(zlib.decompress(base64.a85decode(s)).decode("latin-1") for s in streams)
    return " ".join(re.findall(r"\(((?:[^()\\]|\\.)*)\) Tj", pages))


# --- two PostgreSQL databases for table pairs (docs/mvp/TABLES.md) ---------------------------------


@pytest.fixture
def databases(pg_server: str) -> Iterator[Callable[..., str]]:
    """``create(name, seed, options="")``: a fresh database created with ``options`` and seeded with
    ``seed``; its DSN."""
    made: list[str] = []

    def create(name: str, seed: str, options: str = "") -> str:
        db = f"t_{name}_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(pg_server, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{db}" {options}')
        made.append(db)
        dsn = pg_server.rsplit("/", 1)[0] + f"/{db}"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(seed)
        return dsn

    yield create
    with psycopg.connect(pg_server, autocommit=True) as conn:
        for db in made:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


def both(table: dict[str, Any]) -> dict[str, Any]:
    """Sides a and b, each reading ``table`` in its own database."""
    return {
        s: {"role": role, "target": f"db-{s}", "table": table} for s, role in (("a", "legacy"), ("b", "new"))
    }


def table_spec(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "deltasleuth/v1",
        "kind": "ComparisonSpec",
        "metadata": {"name": "users-tables", "version": "1.0.0", "owner": "tests"},
        "normalizerVersion": "1.1.0",
        "key": {"name": "userid", "type": "string", "class": "business_code"},
        "sides": {
            "a": {"role": "legacy", "target": "db-a", "table": {"name": "users", "keyColumns": ["userid"]}},
            "b": {
                "role": "new",
                "target": "db-b",
                "table": {
                    "schema": "public",
                    "name": "users_v",
                    "keyColumns": ["user_id"],
                    "columns": ["email", {"name": "full_name", "as": "name"}, "amount"],
                },
            },
        },
        "compare": {},
        "recheck": {"delay": "0s", "times": 1, "outcomes": ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"]},
        "security": {"unlisted": "tokenize", "clear": ["$.amount"]},
    }
    return doc | overrides


@pytest.fixture
def tables(
    make_stack: Callable[..., Stack], databases: Callable[..., str], tmp_path: Any
) -> Callable[..., Stack]:
    """``tables(seed_a, seed_b, options_a="", **worker)``: a stack whose targets db-a and db-b are fresh
    databases seeded with ``seed_a`` (created with ``options_a``) and ``seed_b``."""

    def make(seed_a: str, seed_b: str, options_a: str = "", **worker: Any) -> Stack:
        targets = {}
        for side, seed, options in (("a", seed_a, options_a), ("b", seed_b, "")):
            (tmp_path / f"dsn_{side}").write_text(databases(side, seed, options))
            targets[f"db-{side}"] = {"dsn_ref": f"file:dsn_{side}", "rate_per_s": 1000}
        return make_stack(yaml.safe_dump({"targets": targets}), **worker)

    return make


# --- a database of each engine for table pairs (docs/mvp/ENGINES.md) ---------------------------------

Seeded = tuple[str, ...]  # an engine, the seed of a fresh database on it, and its creation options


@pytest.fixture
def engines(
    make_stack: Callable[..., Stack],
    databases: Callable[..., str],
    mysql_server: MySQLServer,
    request: pytest.FixtureRequest,
    tmp_path: Any,
) -> Callable[..., Stack]:
    """``engines(a, b, **worker)``: a stack whose targets db-a and db-b are fresh databases (on Oracle,
    accounts with a schema of their own), each on its side's engine and seeded with its seed."""

    def make(a: Seeded, b: Seeded, **worker: Any) -> Stack:
        targets = {}
        for side, (engine, seed, *options) in (("a", a), ("b", b)):
            if engine == "mysql":
                targets[f"db-{side}"] = mysql_server.target(mysql_server.database(seed, *options))
            elif engine == "oracle":  # started only for a test that reads it
                server: OracleServer = request.getfixturevalue("oracle_server")
                targets[f"db-{side}"] = server.target(server.schema(seed))
            else:
                (tmp_path / f"dsn_{side}").write_text(databases(side, seed, *options))
                targets[f"db-{side}"] = {"dsn_ref": f"file:dsn_{side}", "rate_per_s": 1000}
        return make_stack(yaml.safe_dump({"targets": targets}), **worker)

    return make
