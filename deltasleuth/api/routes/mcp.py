"""MCP over HTTP (docs/mvp/MCP.md §5.1): ``POST /v1/mcp`` hands a viewer's request to the MCP library's
session manager, which the app's lifespan builds while ``mcp.http`` says so (``app.state.mcp``; None: off).
Stateless, with JSON answers: no stream and no session, so GET and DELETE are served only to say so. The
caller is the request's own, cut as ``mcp.tools.caller`` cuts it, and never a cookie session's: an agent
names an API token. No body is read here: the library reads it, once its caller is known. Nothing of the
library is imported here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.types import Receive, Scope, Send

from deltasleuth.api.deps import Viewer
from deltasleuth.mcp import tools

OFF = "MCP over HTTP is off (mcp.http, docs/mvp/MCP.md §4)"
SESSION = "MCP takes an API token (Authorization: Bearer dst_…), not a browser session"
POST_ONLY = "MCP is served by POST only: no stream and no session"
router = APIRouter()


class _Passed(Response):
    """A response that is the session manager's: the request, its body unread, is handed to it."""

    def __init__(self, manager: Any) -> None:
        super().__init__()
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


@router.post("/mcp")
async def call(request: Request, who: Viewer) -> Response:
    """One JSON-RPC message of an agent, answered by the library below this route in its own bodies."""
    if (manager := request.app.state.mcp) is None:
        raise HTTPException(404, OFF)
    if who.session_id is not None:  # a browser's, a token sign-in's included
        raise HTTPException(403, SESSION)
    request.state.mcp_who = tools.caller(who, tools.HTTP)
    return _Passed(manager)


@router.get("/mcp")
@router.delete("/mcp")
async def post_only(request: Request, who: Viewer) -> None:
    """A stream's GET and a session's DELETE: this server has neither, and the library is never asked."""
    if request.app.state.mcp is None:
        raise HTTPException(404, OFF)
    raise HTTPException(405, POST_ONLY, headers={"Allow": "POST"})
