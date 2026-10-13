"""The MCP server over ``tools`` (docs/mvp/MCP.md §5, §8): the library's low-level server, serving tools
only. ``tools/list`` offers a caller what its role and the settings serve; ``tools/call`` answers through
``tools.call``, where every exception ends. One server serves every request of a process, so nothing of a
caller is kept on it: over HTTP each request names its own (``request.state.mcp_who``, which the route
sets), over stdio ``build`` is given the one. A request that names none is answered T17, never as a default
principal. No resource, prompt, output schema or structured content, and no span: the library's
OpenTelemetry middleware is dropped (§3). A cancelled call's statement is cancelled in the store too (§10).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from importlib.metadata import version
from typing import Any

import anyio
from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool

from deltasleuth.agent import prompt
from deltasleuth.auth.principal import Principal, Throttle, dev
from deltasleuth.config import McpSettings
from deltasleuth.context import Context
from deltasleuth.mcp import tools

INSTRUCTIONS = "\n\n".join(
    (
        "These tools read DeltaSleuth comparison runs. A run reads the same keys from two sources, side A and"
        " side B, compares them and records each difference as a finding; findings are grouped into root"
        " causes.",
        prompt.UNTRUSTED,
        prompt.MASKED,
        prompt.CITE,
        "Every figure is observed: quote numbers as tool results show them. DeltaSleuth does not check what"
        " you write.",
    )
)
log = logging.getLogger("deltasleuth.mcp")
CANCEL = 5.0  # seconds a cancelled call waits for its cancel request to reach the store


class _Pool:
    """The store's pool as the server's calls use it (§10). The library cancels a call through an anyio
    scope, which cancels every later await of the call as well, psycopg's own cancel request among them:
    the statement would run on in its backend, behind a connection the pool closes and replaces. So a
    statement still running when its connection is given back is cancelled in the store first, from a
    shielded scope, which waits ``CANCEL`` seconds at most."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    def __getattr__(self, name: str) -> Any:  # whatever else of the pool a service reads
        return getattr(self._pool, name)

    @asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncIterator[AsyncConnection[Any]]:
        async with self._pool.connection(timeout) as conn:
            try:
                yield conn
            except BaseException:
                if conn.pgconn.transaction_status == TransactionStatus.ACTIVE:
                    with anyio.CancelScope(shield=True), suppress(Exception):
                        await conn.cancel_safe(timeout=CANCEL)
                raise


def _listed(spec: tools.ToolSpec) -> types.Tool:
    """A tool as ``tools/list`` shows it: its name, description, closed schema and hints, each static."""
    return types.Tool(
        name=spec.name,
        description=spec.description,
        input_schema=dict(spec.input_schema),
        annotations=types.ToolAnnotations.model_validate(spec.hints),
    )


def build(
    ctx: Context, settings: McpSettings, *, who: Principal | None = None, denials: Throttle | None = None
) -> Server[Any]:
    """The server of one process over ``ctx``, under its ``mcp`` block. ``who`` is stdio's caller
    (``tools.caller``'s), each of whose calls gets a request id of its own (§5.2); without it each request
    names its own. ``denials`` is the throttle of ``authz.denied`` rows ``ds serve``'s routes share (§9).
    Its calls read the store through ``_Pool`` (§10)."""
    limits = tools.Limits(settings, denials=denials)
    ctx = ctx and replace(ctx, pool=_Pool(ctx.pool))  # type: ignore[arg-type]  # None: the self-test's

    def asking(asked: ServerRequestContext[Any, Any]) -> Principal | None:
        if who is not None:
            return replace(who, request_id="C-" + secrets.token_hex(8))
        return getattr(getattr(asked.request, "state", None), "mcp_who", None)

    def unnamed(asked: ServerRequestContext[Any, Any]) -> str:
        """T17 for a request that names no caller, which is a bug of its route: logged, never served."""
        request_id = getattr(getattr(asked.request, "state", None), "request_id", None)
        log.error("%s without a caller request_id=%s", asked.method, request_id)
        return tools.FAULT.format(request_id)

    async def on_list_tools(
        asked: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        if (caller := asking(asked)) is None:
            raise MCPError(types.INTERNAL_ERROR, unnamed(asked))
        return types.ListToolsResult(tools=[_listed(spec) for spec in tools.offered(settings, caller)])

    async def on_call_tool(
        asked: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if (caller := asking(asked)) is None:
            text, failed = tools.refused(params.name, unnamed(asked)), True
        else:
            text, failed = await tools.call(ctx, limits, caller, params.name, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=failed)

    server: Server[Any] = Server(
        "deltasleuth",
        version=version("deltasleuth"),
        title="DeltaSleuth",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    server.middleware.clear()  # the library's default: an OpenTelemetry span per message (§3)
    return server


async def stdio(
    ctx: Context,
    settings: McpSettings,
    who: Principal,
    stop: asyncio.Event,
    stopped: Callable[[], Awaitable[None]],
) -> None:
    """``ds mcp`` (§5.2): serve ``who`` (``tools.caller``'s) over the process's stdin and stdout until the
    client ends its input. The library's transport keeps the wire on duplicates of its own while it serves,
    with fd 0 at the null device and fd 1 at stderr, so nothing else of the process reaches the wire.
    ``stop`` (a SIGTERM) ends the serving as the end of input does: a compare in flight ends first, any
    other call is cancelled. The transport's reader may then still wait in a thread for a line, where no
    cancel reaches it and the transport cannot be left, so ``stopped`` is awaited in its place and is to
    end the process. Once the serving has ended any other way nothing waits for ``stop``, while the
    transport may still wait for its reader or for a client that does not read: SIGTERM is then left
    without a handler, so that it ends the process."""
    served = build(ctx, settings, who=who)
    async with stdio_server() as (read, write):
        try:
            async with anyio.create_task_group() as serving:

                async def until_stopped() -> None:
                    await stop.wait()
                    serving.cancel_scope.cancel()

                serving.start_soon(until_stopped)
                await served.run(read, write, served.create_initialization_options())
                serving.cancel_scope.cancel()
        finally:
            if not stop.is_set():
                with suppress(ValueError):  # not the main thread, which alone takes signals
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if stop.is_set():
            await stopped()


def selftest() -> None:
    """The release's smoke step (§3): the server built without a store, every data class and the work tool
    served, lists its tools and says its instructions to the library's own client, as an operator, in both
    protocol eras. Prints what it saw; raises on any failure."""
    settings = McpSettings((tools.AGGREGATES, tools.TOKENISED, tools.CLEAR), work=("compare_key",))
    server = build(None, settings, who=tools.caller(dev(), tools.STDIO))  # type: ignore[arg-type]
    print(asyncio.run(_selftested(server)))


async def _selftested(server: Server[Any]) -> str:
    from mcp import Client  # the library's client, which this smoke step alone needs

    for mode in ("auto", "legacy"):
        async with Client(server, mode=mode) as client:
            listed = [tool.name for tool in (await client.list_tools()).tools]
            if listed != list(tools.CATALOGUE) or client.instructions != INSTRUCTIONS:
                raise RuntimeError(f"the MCP server did not list its tools to a {mode} client")
    return f"mcp: {len(tools.CATALOGUE)} tools listed in both protocol eras; no store was read"
