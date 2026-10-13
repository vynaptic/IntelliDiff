"""Connector fixtures: local aiohttp servers on 127.0.0.1 and connectors that opt in to them; and, after
each module, that no file reader's process is left but the current one."""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import pytest
from aiohttp import web

from deltasleuth.connectors.rest import RestConnector
from deltasleuth.core.types import (
    ExecutionConfig,
    Pagination,
    RequestTemplate,
    RetryPolicy,
    Side,
    SideConfig,
    Target,
    TargetAuth,
    TargetCapabilities,
)

Serve = Callable[[web.Application], Awaitable[str]]
MakeConnector = Callable[..., RestConnector]
Reply = Callable[[web.Request], Any]  # returns a response, or an awaitable of one
Start = Callable[..., Awaitable[RestConnector]]
Served = Callable[..., Awaitable[str]]


def fast_execution(**overrides: Any) -> ExecutionConfig:
    """Short timeouts and near-zero backoff so retry tests run in milliseconds."""
    fields: dict[str, Any] = {
        "connect_timeout_s": 2.0,
        "read_timeout_s": 2.0,
        "total_timeout_s": 10.0,
        "retries": RetryPolicy(max=2, initial_s=0.01, max_s=0.02),
    }
    return ExecutionConfig(**{**fields, **overrides})


def side_config(
    path: str = "/things/{{ key }}",
    *,
    method: str = "GET",
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: Any = None,
    pagination: Pagination | None = None,
    side: Side = Side.A,
) -> SideConfig:
    request = RequestTemplate(method=method, path=path, query=query or {}, headers=headers or {}, body=body)  # type: ignore[arg-type]
    return SideConfig(side=side, role="legacy", target="t", request=request, pagination=pagination)


class Recorder:
    """What the test server received."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def note(self, request: web.Request) -> None:
        self.requests.append(
            {
                "method": request.method,
                "raw_path": request.raw_path,
                "query": list(request.query.items()),
                "headers": dict(request.headers),
                "body": await request.read(),
            }
        )

    @property
    def count(self) -> int:
        return len(self.requests)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture(scope="module", autouse=True)
def no_stray_reader() -> Iterator[None]:
    """docs/mvp/READER.md §15: after a module's tests this process has one reader child at most, the current
    one. A reader that was replaced, retired or killed is gone: what the tests leave open ends with it."""
    yield
    from tests.unit.connectors import ducked

    ended = time.monotonic() + 10  # a killed reader is reaped by its own thread
    while (left := ducked.strays()) and time.monotonic() < ended:
        time.sleep(0.05)
    assert left == []


@pytest.fixture
async def serve() -> AsyncIterator[Serve]:
    """Start aiohttp apps on 127.0.0.1; returns their base URL."""
    runners: list[web.AppRunner] = []

    async def start(app: web.Application) -> str:
        runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=0.5)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture
async def make_connector() -> AsyncIterator[MakeConnector]:
    """RestConnectors for a base URL (private networks allowed by default); closed after the test."""
    made: list[RestConnector] = []

    def make(
        base_url: str,
        *,
        execution: ExecutionConfig | None = None,
        auth: TargetAuth | None = None,
        rate_per_s: float = 1000.0,
        max_concurrency: int = 8,
        capabilities: TargetCapabilities | None = None,
        allow_private_network: bool = True,
        **kwargs: Any,
    ) -> RestConnector:
        target = Target(
            target_id="t",
            base_url=base_url,
            auth=auth or TargetAuth(),
            rate_per_s=rate_per_s,
            max_concurrency=max_concurrency,
            capabilities=capabilities or TargetCapabilities(),
            allow_private_network=allow_private_network,
        )
        made.append(RestConnector(target, execution or fast_execution(), **kwargs))
        return made[-1]

    yield make
    for connector in made:
        await connector.aclose()


@pytest.fixture
def served(serve: Serve, recorder: Recorder) -> Served:
    """Serve ``reply`` at ``path``, recording every request; returns the server's base URL."""

    async def served_(reply: Reply, path: str = "/api/things/{key}") -> str:
        async def handler(request: web.Request) -> web.StreamResponse:
            await recorder.note(request)
            response = reply(request)
            return await response if inspect.isawaitable(response) else response

        app = web.Application()
        app.router.add_route("*", path, handler)
        return await serve(app)

    return served_


@pytest.fixture
def start(served: Served, make_connector: MakeConnector) -> Start:
    """``served(reply, path)``, and a connector for its ``/api`` base path."""

    async def start_(reply: Reply, path: str = "/api/things/{key}", **connector_kwargs: Any) -> RestConnector:
        return make_connector(await served(reply, path) + "/api", **connector_kwargs)

    return start_
