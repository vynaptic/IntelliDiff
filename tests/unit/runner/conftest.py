"""Runner fixtures: a Context on a fresh database, a scripted HTTP service and a worker to drive runs.

The unit spec compares ``{id, name, status, tags[]}`` documents (``name`` tokenised, ``tags`` a set)
read from ``/a/{key}`` and ``/b/{key}``; ``/list`` serves a cursor-paged key listing.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from deltasleuth.config import load_settings
from deltasleuth.context import Context, init_workspace
from deltasleuth.core.spec import compile_spec, load_spec_file
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.runner import run_worker
from deltasleuth.store import repo
from tests.conftest import TESTER, encryption, licensed_once

SPEC = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: unit-runner, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
canonical: {mode: tree}
time: {mode: live}
key:
  name: item_id
  type: string
  pattern: "^K[0-9]{3}$"
  class: business_code
  normalize: [trim, upper]
  source: {kind: list, keys: [K001, K002, K003]}
sides:
  a:
    role: legacy
    target: svc-a
    request: {method: GET, path: "/a/{{ key }}"}
    select: "$"
    notFound: {status: [404]}
    truncation: {itemsPath: "$.tags", cap: 50}
  b:
    role: new
    target: svc-b
    request: {method: GET, path: "/b/{{ key }}"}
    select: "$"
    notFound: {status: [404]}
    truncation: {itemsPath: "$.tags", cap: 50}
compare:
  arrays:
    - {path: "$.tags", mode: set}
  fields:
    - {path: "$.name", normalize: [trim]}
    - {path: "$.status", severity: HIGH}
security:
  unlisted: tokenize
  tokenize: [{path: "$.name", class: name}]
  clear: ["$.status", "$.tags[*]", "$.id"]
recheck: {delay: 0s, times: 1, oneShot: {delay: 0s, times: 1}}
execution: {concurrency: 2, retries: 0, timeouts: {connect: 2s, read: 2s, total: 5s}}
"""

TARGETS = """\
targets:
  svc-a:
    base_url: {base}
    allow_private_network: true
    allow_insecure_http: true
    rate_per_s: {rate}
    capabilities:
      authoritative_absence: {{status: [404]}}
  svc-b:
    base_url: {base}
    allow_private_network: true
    allow_insecure_http: true
    rate_per_s: {rate}
    capabilities:
      authoritative_absence: {{status: [404]}}
"""

UNAVAILABLE = (503, {"error": "UNAVAILABLE"})
Reply = tuple[int, Any]


def doc(key: str, *, name: str = "Alice Secretname", status: str = "ACTIVE") -> dict[str, Any]:
    return {"id": key, "name": name, "status": status, "tags": ["x", "y"]}


async def until(check: Callable[[], object], timeout: float = 30) -> None:
    """Wait for ``check()``; fail after ``timeout`` seconds instead of hanging the suite."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def spec_text(keys: list[str] | None = None, *, execution: str = "", extra: str = "") -> str:
    """The unit spec with other inline ``keys``, extra ``execution`` members and extra top-level YAML."""
    text = SPEC
    if keys is not None:
        text = text.replace("keys: [K001, K002, K003]", "keys: [" + ", ".join(keys) + "]")
    if execution:
        text = text.replace("execution: {concurrency: 2,", f"execution: {{{execution},")
    return text + extra


class ScriptedService:
    """``/{a,b}/{key}`` answer the n-th call for a key with its n-th reply (the last repeats; no
    script: 404), each after ``slow`` seconds; with a ``token``, a call without ``Bearer <token>``
    answers 401. ``/list`` walks ``listings[n]`` on the n-th walk, two items per page (a key, as
    ``{"id": key}``, or an item as it is); a page holding None answers 503."""

    def __init__(self) -> None:
        self.scripts: dict[str, dict[str, list[Reply]]] = {"a": {}, "b": {}}
        self.calls: Counter[tuple[str, str]] = Counter()
        self.log: list[tuple[str, str, float]] = []  # (side, key, loop time) in arrival order
        self.listings: list[list[Any]] = []
        self.walks = 0
        self.slow = 0.0
        self.token: str | None = None
        self.server: TestServer | None = None

    def script(self, side: str, key: str, *replies: Reply) -> None:
        self.scripts[side][key] = list(replies)

    def both(self, key: str, a: Any, b: Any) -> None:
        self.script("a", key, (200, a))
        self.script("b", key, (200, b))

    async def _key(self, request: web.Request) -> web.Response:
        side, key = request.match_info["side"], request.match_info["key"]
        self.log.append((side, key, asyncio.get_running_loop().time()))
        n = self.calls[(side, key)]
        self.calls[(side, key)] += 1
        if self.token is not None and request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.json_response({"error": "UNAUTHORIZED"}, status=401)
        replies = self.scripts[side].get(key) or [(404, {"error": "NOT_FOUND"})]
        status, body = replies[min(n, len(replies) - 1)]
        await asyncio.sleep(self.slow)
        return web.json_response(body, status=status)

    async def _list(self, request: web.Request) -> web.Response:
        position = int(request.query.get("cursor", "0"))
        self.walks += position == 0
        keys = self.listings[min(self.walks, len(self.listings)) - 1]
        page = keys[position : position + 2]
        if None in page:
            return web.json_response(UNAVAILABLE[1], status=UNAVAILABLE[0])
        more = position + 2 < len(keys)
        return web.json_response(
            {
                "items": [{"id": k} if isinstance(k, str) else k for k in page],
                "next": str(position + 2) if more else None,
            }
        )

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/list", self._list)
        app.router.add_get("/{side:[ab]}/{key}", self._key)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        return f"http://127.0.0.1:{self.server.port}"


@dataclass
class Harness:
    ctx: Context

    async def spec(self, text_or_path: str | Path) -> int:
        """Compile and store a spec (YAML text or a file); return its spec_id."""
        body = (
            load_spec_file(text_or_path) if isinstance(text_or_path, Path) else parse_yaml_text(text_or_path)
        )
        async with self.ctx.pool.connection() as conn:
            return await repo.save_spec(
                conn, compile_spec(body, targets=self.ctx.targets), created_by=TESTER.actor
            )

    async def run(self, run_id: str) -> repo.Row:
        async with self.ctx.pool.connection() as conn:
            row = await repo.get_run(conn, run_id)
        assert row is not None
        return row

    async def finish(
        self, run_id: str, statuses: tuple[str, ...] = repo.TERMINAL, timeout: float = 60
    ) -> repo.Row:
        """Run a worker until the run reaches one of ``statuses``."""
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(self.ctx, stop=stop))
        try:
            return await self.wait(run_id, statuses, timeout)
        finally:
            stop.set()
            await worker

    async def wait(
        self, run_id: str, statuses: tuple[str, ...] = repo.TERMINAL, timeout: float = 60
    ) -> repo.Row:
        """Wait until the run, processed by a worker already running, reaches one of ``statuses``."""
        deadline = asyncio.get_running_loop().time() + timeout
        while (run := await self.run(run_id))["status"] not in statuses:
            assert asyncio.get_running_loop().time() < deadline, f"run still {run['status']}"
            await asyncio.sleep(0.05)
        return run

    async def results(self, run_id: str, cycle: int | None = None) -> dict[str, repo.Row]:
        """Results by masked key (the unit spec's keys are business codes, shown as they are), each with
        its ``findings`` rows."""
        async with self.ctx.pool.connection() as conn:
            rows, _ = await repo.list_results(conn, run_id, cycle, limit=1000)
            findings, _ = await repo.list_findings(conn, run_id, cycle, limit=5000)
        by_key = {r["key_masked"]: [] for r in rows}
        for f in findings:
            by_key[f["key_masked"]].append(f)
        return {r["key_masked"]: r | {"findings": by_key[r["key_masked"]]} for r in rows}

    async def observations(self, run_id: str, key_masked: str) -> list[repo.Row]:
        async with self.ctx.pool.connection() as conn:
            detail = await repo.get_key_detail(conn, run_id, key_masked)
        assert detail is not None
        return detail["observations"]


OpenHarness = Callable[..., AbstractAsyncContextManager[Harness]]


@pytest.fixture
def open_harness(tmp_path: Path, pg_dsn: str) -> OpenHarness:
    """``async with open_harness(targets_yaml, auth=None, packs=None, notify=None, readiness=None,
    precheck=None, files=None, tier=..., **worker) as h``: a Context on the test database; ``auth``: the
    auth settings (then the encryption settings too, the same each time); ``packs``: the evidence pack
    settings; ``notify``: the notification settings; ``readiness``: the readiness settings; ``precheck``:
    the table pre-check settings; ``files``: the file reader's. In oidc mode of a
    vendor build (``vendor_keys``) a licence of ``tier`` (None: none, an evaluation) is installed unless the
    store names one, so that its runs and sign-offs go ahead, as they do in the open build without one."""

    @asynccontextmanager
    async def open_(
        targets: str,
        *,
        auth: dict[str, Any] | None = None,
        packs: dict[str, Any] | None = None,
        notify: dict[str, Any] | None = None,
        readiness: dict[str, Any] | None = None,
        precheck: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        tier: str | None = "enterprise",
        **worker: Any,
    ) -> AsyncIterator[Harness]:
        (tmp_path / "dsn").write_text(pg_dsn)
        (tmp_path / "targets.yaml").write_text(targets)
        path = tmp_path / "deltasleuth.yaml"
        settings = {
            "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
            "evidence_dir": "evidence",
            "workspace_dir": "workspace",
            "targets_file": "targets.yaml",
            "worker": {"concurrency": 4, "lease_seconds": 30, "poll_seconds": 0.05} | worker,
        }
        if auth is not None:
            settings |= {"auth": auth, "encryption": encryption(tmp_path / "secrets")}
        if packs is not None:
            settings |= {"packs": packs}
        if notify is not None:
            settings |= {"notify": notify}
        if readiness is not None:
            settings |= {"readiness": readiness}
        if precheck is not None:
            settings |= {"precheck": precheck}
        if files is not None:
            settings |= {"files": files}
        path.write_text(json.dumps(settings))
        init_workspace(load_settings(path))
        async with Context.open(load_settings(path)) as ctx:
            if auth is not None and "oidc" in auth:
                await licensed_once(ctx, tier)
            yield Harness(ctx)

    return open_


@pytest.fixture
async def service() -> AsyncIterator[ScriptedService]:
    svc = ScriptedService()
    await svc.start()
    yield svc
    assert svc.server is not None
    await svc.server.close()


@pytest.fixture
async def harness(open_harness: OpenHarness, service: ScriptedService) -> AsyncIterator[Harness]:
    """A Context whose targets svc-a and svc-b are the scripted service."""
    assert service.server is not None
    async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)) as h:
        yield h
