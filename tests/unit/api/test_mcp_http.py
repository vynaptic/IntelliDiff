"""MCP over HTTP: ``POST /v1/mcp`` behind the API's own guard and its one
authentication path, for an API token and never a browser session; off unless the settings serve it; no
stream and no session; and, below the route, the MCP library's own client served in both protocol eras, each
request under its own caller. The tools themselves are tested in tests/unit/mcp_server."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from deltasleuth.auth import tokens
from deltasleuth.auth.principal import expand
from deltasleuth.mcp import tools

from . import conftest
from .conftest import OIDC, PUBLIC_URL, Seeded, approved, audited, said

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
# The conftest's spec declares $.price clear: its run is served only where the settings say so.
MCP = {
    "data_classes": [tools.AGGREGATES, tools.TOKENISED, tools.CLEAR],
    "work": ["compare_key"],
    "http": True,
}
ERAS = ("auto", "legacy")  # the library's client at 2026-07-28, and with the handshake of 2025-11-25
ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
OFF = "MCP over HTTP is off (mcp.http)"
MODERN = {"MCP-Protocol-Version": "2026-07-28"}
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
VIEWERS = tuple(tools.CATALOGUE)[:-1]  # what a viewer is offered: every tool but compare_key


def post(client: TestClient, headers: dict[str, str], **sent: Any) -> Any:
    return client.post("/v1/mcp", headers=ACCEPT | headers, **{"json": LIST} | sent)


@asynccontextmanager
async def agent(
    app: Any, headers: dict[str, str], era: str, seen: list[Any] | None = None
) -> AsyncIterator[Client]:
    """The MCP library's own client on the app's ``/v1/mcp``, as a caller of ``headers`` at 10.1.2.3 (in
    developer mode: on loopback); ``seen`` gets each HTTP response's status and headers."""
    developer = "X-CSRF-Token" in headers
    url = "http://127.0.0.1/v1/mcp" if developer else f"{PUBLIC_URL}/v1/mcp"
    transport = httpx2.ASGITransport(app=app, client=("127.0.0.1" if developer else "10.1.2.3", 50000))

    async def note(response: Any) -> None:
        if seen is not None:
            seen.append((response.status_code, response.headers))

    hooks = {"response": [note]}
    async with httpx2.AsyncClient(transport=transport, headers=headers, event_hooks=hooks) as http:
        async with Client(streamable_http_client(url, http_client=http), mode=era) as client:
            yield client


async def ask(client: Client, name: str, args: dict[str, Any]) -> tuple[Any, bool]:
    """The ``data`` of a call's frame, or its ``error`` text, and whether it is an error."""
    result = await client.call_tool(name, args)
    (block,) = result.content
    framed = json.loads(block.text)
    assert set(framed) == {"tool", "error" if result.is_error else "data"}
    return framed["error" if result.is_error else "data"], bool(result.is_error)


def test_who_may_call(open_client: OpenClient, callers: Any) -> None:
    """A bearer API token only. The dependency every route shares answers first, so a
    cookie session without its CSRF token is refused as on any route, and with it as a browser session."""
    with open_client(auth={"oidc": OIDC}, mcp=MCP) as client:
        as_ = callers(client)
        person, nobody, token = as_.session("viewer"), as_.session(username="bob"), as_.token("viewer")
        forged = {"Authorization": "Bearer dst_" + "0" * 16 + "_" + "0" * 64}
        refused = [
            post(client, {}),
            post(client, forged),
            post(client, nobody),
            post(client, person),
            post(client, {"Cookie": person["Cookie"]}),
        ]
        served = post(client, token)
    with open_client(auth={}, mcp=MCP) as client:  # tokens mode: a token's sign-in is a browser session too
        token = callers(client).token("operator")
        signed_in = callers(client).session("viewer", token=token["Authorization"].removeprefix("Bearer "))
        refused.append(post(client, signed_in))
    assert [said(answer) for answer in refused] == [
        (401, "not signed in"),
        (401, "invalid or expired API token"),
        (403, "needs the viewer role"),
        (403, "MCP takes an API token (Authorization: Bearer dst_…), not a browser session"),
        (403, "missing or invalid CSRF token"),
        (403, "MCP takes an API token (Authorization: Bearer dst_…), not a browser session"),
    ]
    assert [answer.headers.get("www-authenticate") for answer in refused[:2]] == ["Bearer"] * 2
    assert served.status_code == 200 and len(served.json()["result"]["tools"]) == len(VIEWERS)


def test_developer_mode_calls_behind_its_csrf_header(open_client: OpenClient, seed: Any) -> None:
    """``dev:loopback``, whose POST needs ``X-CSRF-Token: dev``, acts as an operator and no more."""
    with open_client(mcp=MCP) as client:
        seeded = seed(client)
        refused = post(client, {"X-CSRF-Token": "x"})

        async def call() -> Any:
            async with agent(client.app, {"X-CSRF-Token": "dev"}, "auto") as dev:
                listed = [tool.name for tool in (await dev.list_tools()).tools]
                return listed, await ask(
                    dev, "get_observations", {"run_id": seeded.run_id, "key": seeded.key}
                )

        listed, (viewed, failed) = client.portal.call(call)
        rows = audited(client, "key.view")
    assert said(refused) == (403, "missing or invalid CSRF token")
    assert listed == list(tools.CATALOGUE) and (viewed["key_masked"], failed) == (seeded.key, False)
    assert rows == [
        ("dev:loopback", seeded.run_id, {"key_masked": seeded.key, "cycle": 1, "via": "mcp:http"})
    ]


@pytest.mark.parametrize("block", [None, {"data_classes": ["aggregates"]}], ids=["no block", "http off"])
def test_off_unless_the_settings_serve_it(
    open_client: OpenClient, callers: Any, block: dict[str, Any] | None
) -> None:
    """The 404, for each operation, once its caller is known: without it the answer is the route's own 401."""
    with open_client(auth={}, mcp=block) as client:
        viewer = callers(client).token("viewer")
        off = [post(client, viewer), client.get("/v1/mcp", headers=viewer)]
        off.append(client.delete("/v1/mcp", headers=viewer))
        unknown = [post(client, {}), client.get("/v1/mcp"), client.delete("/v1/mcp")]
        assert client.app.state.mcp is None
    assert [said(answer) for answer in off] == [(404, OFF)] * 3
    assert [said(answer) for answer in unknown] == [(401, "not signed in")] * 3


def test_only_post_is_served(open_client: OpenClient, callers: Any) -> None:
    """No stream to GET and no session to DELETE; the library, which would open a stream, is not asked."""
    with open_client(auth={}, mcp=MCP) as client:
        viewer = callers(client).token("viewer")
        answers = [client.get("/v1/mcp", headers=viewer | ACCEPT), client.delete("/v1/mcp", headers=viewer)]
        answers.append(client.get("/v1/mcp", headers=viewer | ACCEPT | MODERN))
    assert [(*said(answer), answer.headers["allow"]) for answer in answers] == [
        (405, "MCP is served by POST only: no stream and no session", "POST")
    ] * 3


def test_guard_refuses_not_the_library(open_client: OpenClient, callers: Any) -> None:
    """``_Guard`` is the one Host and Origin check, and the library's own is off: it would
    answer 421 to a Host it does not know, and it knows none."""
    with open_client(auth={"allowed_hosts": ["API.ds.test"]}, mcp=MCP) as client:
        viewer = callers(client).token("viewer")
        refused = [
            post(client, viewer | {"Host": "evil.example"}),
            post(client, viewer | {"Origin": "https://evil.example"}),
            post(client, viewer | {"Sec-Fetch-Site": "cross-site"}),
            client.get("/v1/mcp", headers=viewer | {"Sec-Fetch-Site": "same-site"}),
            client.delete("/v1/mcp", headers=viewer | {"Origin": "null"}),
        ]
        allowed = [
            post(client, viewer | {"Host": "api.ds.test"}),
            post(client, viewer | {"Origin": PUBLIC_URL, "Sec-Fetch-Site": "same-origin"}),
        ]
    with open_client(mcp=MCP, client=("10.0.0.9", 50000)) as client:
        refused.append(post(client, {}))
    with open_client(mcp=MCP) as client:
        refused.append(post(client, {"X-Forwarded-For": "127.0.0.1"}))
    assert [said(answer) for answer in refused] == [
        (400, "unexpected Host header"),
        (403, "cross-origin request refused"),
        (403, "cross-site request refused"),
        (403, "cross-site request refused"),
        (403, "cross-origin request refused"),
        (403, "developer mode serves loopback clients only"),
        (403, "developer mode refuses proxied requests"),
    ]
    assert [answer.status_code for answer in allowed] == [200, 200]
    assert "access-control-allow-origin" not in allowed[1].headers  # no CORS for another origin's page


async def _posted(app: Any, headers: dict[str, str], body: bytes) -> tuple[int, int]:
    """A POST of ``body`` handed to the app itself: its status, and how often its body was asked for."""
    asked, statuses = 0, []

    async def receive() -> dict[str, Any]:
        nonlocal asked
        asked += 1
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    sent = {"Host": "ds.test", "Content-Type": "application/json", "Content-Length": str(len(body))}
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "https", "path": "/v1/mcp", "raw_path": b"/v1/mcp", "root_path": "", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (sent | ACCEPT | headers).items()],
        "client": ("10.1.2.3", 50000), "server": ("ds.test", 443),
    }  # fmt: skip
    await app(scope, receive, send)
    return statuses[0], asked


def test_no_body_is_read_before_its_caller_is_known(open_client: OpenClient, callers: Any) -> None:
    """The route declares no body, so a refusal answers without asking for one; the library reads it."""
    body = json.dumps(LIST).encode() + b" " * 5000
    with open_client(auth={}, mcp=MCP) as client:
        as_ = callers(client)
        token = as_.token("viewer")
        session = as_.session("viewer", token=token["Authorization"].removeprefix("Bearer "))
        callers_ = ({}, {"Authorization": "Bearer nope"}, {"Cookie": session["Cookie"]}, session, token)
        got = [client.portal.call(_posted, client.app, headers, body) for headers in callers_]
    assert got == [(401, 0), (401, 0), (403, 0), (403, 0), (200, 1)]


@pytest.mark.parametrize("era", ERAS)
def test_a_token_s_agent_is_served_its_own_tools(
    open_client: OpenClient,
    callers: Any,
    seed: Any,
    audit_rows: Any,
    monkeypatch: pytest.MonkeyPatch,
    era: str,
) -> None:
    """Both code paths of the route: a role refusal, a read, a key view and a compare, each as the request's
    own token; on the modern path the library's inner ``tools/list`` is the caller's as well."""
    offered, lists = tools.offered, []

    def listing(settings: Any, who: Any) -> Any:
        lists.append((who.actor, who.via, tuple(spec.name for spec in offered(settings, who))))
        return offered(settings, who)

    monkeypatch.setattr(tools, "offered", listing)
    with open_client(auth={}, mcp=MCP) as client:
        seeded: Seeded = seed(client)
        as_, seen = callers(client), []
        viewer, operator = as_.token("viewer", "reader-bot"), as_.token("operator", "compare-bot")
        key = {"run_id": seeded.run_id, "key": seeded.key}

        async def session() -> dict[str, Any]:
            async with agent(client.app, viewer, era, seen) as low, agent(client.app, operator, era) as high:
                return {
                    "low": [tool.name for tool in (await low.list_tools()).tools],
                    "high": [tool.name for tool in (await high.list_tools()).tools],
                    "refused": [await ask(low, "compare_key", key) for _ in range(2)],
                    "summary": await ask(low, "get_run_summary", {"run_id": seeded.run_id}),
                    "viewed": await ask(low, "get_observations", key),
                    "compared": await ask(high, "compare_key", key),
                    "era": (low.protocol_version, high.protocol_version, low.instructions),
                }

        got = client.portal.call(session)
        rows = [row for row in audit_rows(client) if row["actor"].startswith("token:")]
    low, high = (f"token:{tokens.token_id(who['Authorization'][7:])}" for who in (viewer, operator))
    assert (got["low"], got["high"]) == (list(VIEWERS), list(tools.CATALOGUE))
    assert got["era"][:2] == (("2026-07-28",) * 2 if era == "auto" else ("2025-11-25",) * 2)
    assert got["era"][2].startswith("These tools read DeltaSleuth comparison runs.")
    assert got["refused"] == [("compare_key needs the operator role", True)] * 2
    assert (got["summary"][0]["run_id"], got["summary"][1]) == (seeded.run_id, False)
    assert (got["viewed"][0]["key_masked"], got["viewed"][0]["cycle"], got["viewed"][1]) == (
        seeded.key,
        1,
        False,
    )
    assert (got["compared"][0]["key_masked"], got["compared"][0]["outcome"]) == (seeded.key, "MISMATCH")
    via = {"via": "mcp:http"}
    assert [(row["actor"], row["actor_name"], row["action"], row["object"]) for row in rows] == [
        (low, "reader-bot", "authz.denied", "MCP compare_key"),  # once a minute: the second is counted later
        (low, "reader-bot", "mcp.read", seeded.run_id),
        (low, "reader-bot", "key.view", seeded.run_id),
        (high, "compare-bot", "compare_one", rows[-1]["object"]),
        (high, "compare-bot", "run.finished", rows[-1]["object"]),
    ]
    assert [row["detail"] for row in rows[:3]] == [
        {"needed": "operator", "count": 1} | via,
        {"tool": "get_run_summary", "calls": 1} | via,
        {"key_masked": seeded.key, "cycle": 1} | via,
    ]
    assert all(row["detail"]["via"] == "mcp:http" for row in rows[3:])
    # Each row is of its own HTTP request, of the client's address, and of no session.
    assert (
        len({row["request_id"] for row in rows[:4]}) == 4 and rows[3]["request_id"] == rows[4]["request_id"]
    )
    assert {(row["request_id"][:2], str(row["client_addr"]), row["session_id"]) for row in rows} == {
        ("Q-", "10.1.2.3", None)
    }
    # The modern path lists the tools again inside each call that has arguments: as that call's caller.
    listed = [(low, "mcp:http", VIEWERS), (high, "mcp:http", tuple(tools.CATALOGUE))]
    assert lists == listed + ([listed[0]] * 4 + [listed[1]] if era == "auto" else [])
    # Stateless, with JSON answers under the guard's headers: no session id, no stream.
    assert len(seen) >= 6 and not [headers for _, headers in seen if "mcp-session-id" in headers]
    assert {headers["content-type"] for status, headers in seen if status == 200} == {"application/json"}
    assert {status for status, _ in seen} <= {200, 202}  # 202: the handshake's notification
    assert {(headers["cache-control"], headers["x-request-id"][:2]) for _, headers in seen} == {
        ("no-store", "Q-")
    }


@pytest.mark.parametrize("era", ERAS)
def test_interleaved_callers_keep_their_roles(
    open_client: OpenClient,
    callers: Any,
    seed: Any,
    audit_rows: Any,
    monkeypatch: pytest.MonkeyPatch,
    era: str,
) -> None:
    """One server serves every request, so nothing of a caller is kept on it: while an operator's compare
    waits for its target, a viewer's is refused, and each row names its own token."""
    reached, answer, get = threading.Event(), threading.Event(), conftest._Sides.do_GET

    def held(handler: Any) -> None:
        reached.set()
        assert answer.wait(30)
        get(handler)

    with open_client(auth={}, mcp=MCP) as client:
        seeded: Seeded = seed(client)
        as_ = callers(client)
        viewer, operator = as_.token("viewer", "reader-bot"), as_.token("operator", "compare-bot")
        key = {"run_id": seeded.run_id, "key": seeded.key}
        monkeypatch.setattr(conftest._Sides, "do_GET", held)

        async def session() -> list[Any]:
            async with agent(client.app, viewer, era) as low, agent(client.app, operator, era) as high:
                comparing = asyncio.ensure_future(ask(high, "compare_key", key))
                assert await asyncio.to_thread(reached.wait, 30)  # the operator's compare is in flight
                refused, viewed = await ask(low, "compare_key", key), await ask(low, "get_observations", key)
                answer.set()
                return [refused, viewed, await comparing]

        refused, viewed, compared = client.portal.call(session)
        rows = [row for row in audit_rows(client) if row["actor"].startswith("token:")]
    assert refused == ("compare_key needs the operator role", True)
    assert (viewed[0]["key_masked"], viewed[1], compared[0]["outcome"]) == (seeded.key, False, "MISMATCH")
    assert sorted((row["actor_name"], row["action"]) for row in rows) == [
        ("compare-bot", "compare_one"),
        ("compare-bot", "run.finished"),
        ("reader-bot", "authz.denied"),
        ("reader-bot", "key.view"),
    ]


def test_revoked_token_stops_at_next_call(open_client: OpenClient, callers: Any) -> None:
    """A token's revocation applies at the next request; nothing of it is kept between two."""
    with open_client(auth={"oidc": OIDC}, mcp=MCP) as client:
        as_ = callers(client)
        token, admin = as_.token("viewer"), as_.session(*expand(["admin"]))
        before = post(client, token)
        token_id = tokens.token_id(token["Authorization"].removeprefix("Bearer "))
        assert client.delete(f"/v1/auth/tokens/{token_id}", headers=admin).status_code == 200
        after = [post(client, token), post(client, token | MODERN)]
    assert before.status_code == 200
    assert [said(answer) for answer in after] == [(401, "invalid or expired API token")] * 2


def test_a_machine_token_never_bypasses_sso(open_client: OpenClient, callers: Any, seed: Any) -> None:
    """Under SSO an agent's token is one a signed-in admin made, owned by a person. MCP gives it no
    session and no role but its own, and its compare still meets the four-eyes gate in the runner."""
    with open_client(auth={"oidc": OIDC}, mcp=MCP) as client:
        seeded: Seeded = seed(client)  # a run of a version that declares a clear path, stored unapproved
        admin = callers(client).session(*expand(["admin"]), auth_time=datetime.now(UTC))
        owner = client.get("/v1/auth/me", headers=admin).json()["actor"].removeprefix("user:")
        asked = {"name": "compare-bot", "owner": "payments team", "role": "operator", "owner_user_id": owner}
        made = client.post("/v1/auth/tokens", json=asked, headers=admin).json()
        bearer = {"Authorization": f"Bearer {made['token']}"}

        async def compare() -> Any:
            async with agent(client.app, bearer, "auto") as bot:
                return await ask(bot, "compare_key", {"run_id": seeded.run_id, "key": seeded.key})

        gated = client.portal.call(compare)
        approved(client, seeded.spec_id)
        compared, failed = client.portal.call(compare)
        signed_in = client.post("/v1/auth/token-login", json={"token": made["token"]})
        held = client.get("/v1/auth/sessions", headers=admin).json()["items"]
        rows = audited(client, "compare_one")
    assert gated == (
        f"spec {seeded.spec_id} (api-demo 1.0.0) needs a four-eyes approval of clear before it runs",
        True,
    )
    assert (compared["outcome"], failed) == ("MISMATCH", False)
    assert said(signed_in) == (404, "token sign-in is not enabled")
    assert [session["actor"] for session in held] == [f"user:{owner}"]  # the admin's own: none of the token's
    assert [(actor, detail["via"]) for actor, _, detail in rows] == [
        (f"token:{made['token_id']}", "mcp:http")
    ]


def test_what_mcp_refuses_a_token_v1_serves_it(open_client: OpenClient, callers: Any, seed: Any) -> None:
    """An agent's bearer is a general API token. The data classes, ``mcp.work``, the absence of run
    control, the rate limit and ``mcp.read`` are the route's: they hold for calls made through ``/v1/mcp``
    and for no other. On ``/v1`` the same token is served, with its full role, what MCP refuses it. A token
    that is ever confined to the route changes this test and the guides that repeat it."""
    block = {"data_classes": [tools.AGGREGATES, tools.TOKENISED], "http": True, "calls_per_minute": 1}
    with open_client(auth={}, mcp=block) as client:
        seeded: Seeded = seed(client)  # a run of a spec that declares $.price clear; no work tool is served
        as_ = callers(client)
        viewer, operator = as_.token("viewer", "reader-bot"), as_.token("operator", "compare-bot")
        key, run = {"run_id": seeded.run_id, "key": seeded.key}, f"/v1/runs/{seeded.run_id}"

        def told(token: dict[str, str], name: str, args: dict[str, Any]) -> str:
            call = {"name": name, "arguments": args}
            sent = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}
            result = post(client, token, json=sent).json()["result"]
            return str(json.loads(result["content"][0]["text"])["error"])

        refused = [
            told(viewer, "get_observations", key),
            told(viewer, "get_findings", {"run_id": seeded.run_id}),  # the caller's second call of the minute
            told(operator, "compare_key", key),
        ]
        viewed = client.get(f"{run}/keys/{seeded.key}", headers=viewer)
        read = [client.get(f"{run}/findings", headers=viewer).status_code for _ in range(3)]
        again = {"spec_id": seeded.spec_id, "key": seeded.key, "run_id": seeded.run_id}
        compared = client.post("/v1/compare", headers=operator, json=again)
        started = client.post("/v1/runs", headers=operator, json={"spec_id": seeded.spec_id, "mode": "batch"})
        moved = [client.post(f"{run}/{action}", headers=operator) for action in ("pause", "resume", "cancel")]
        marked, views = audited(client, "mcp.read"), audited(client, "key.view")
    assert refused == [
        f"run {seeded.run_id} is not served by this installation: its spec declares clear values or a"
        " business-code key (mcp.data_classes lacks production_clear_declared)",
        "rate limited: at most 1 MCP tool calls a minute per caller",
        "compare_key is not served by this installation (mcp.work)",
    ]
    assert viewed.status_code == 200 and "2.5" in [found["value_b"] for found in viewed.json()["findings"]]
    assert read == [200] * 3 and marked == []  # no limit of MCP's, and no mark of what the agent read
    assert [detail for _, _, detail in views] == [{"key_masked": seeded.key, "cycle": 1}]  # the route's row
    assert (compared.status_code, started.status_code) == (200, 202)
    assert [(answer.status_code, answer.json()["status"]) for answer in moved] == [
        (200, "paused"),
        (200, "running"),
        (200, "cancelled"),
    ]


def test_no_pii_reader_ever(open_client: OpenClient, callers: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside MCP a principal acts as a viewer or an operator and never as more, whatever it holds:
    developer mode's, which holds every role, and the highest token there is."""
    call, callers_seen = tools.call, []

    async def calling(ctx: Any, limits: Any, who: Any, name: str, args: Any) -> Any:
        callers_seen.append((who.actor.split(":")[0], who.roles, who.via))
        return await call(ctx, limits, who, name, args)

    monkeypatch.setattr(tools, "call", calling)
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_runs"}}
    with open_client(auth={}, mcp=MCP) as client:
        answers = [
            post(client, callers(client).token(role), json=listing) for role in ("spec_author", "viewer")
        ]
    with open_client(mcp=MCP) as client:
        assert {"admin", "pii_reader"} <= set(client.get("/v1/auth/me").json()["roles"])
        answers.append(post(client, {}, json=listing))
    assert [answer.json()["result"]["isError"] for answer in answers] == [False] * 3
    assert callers_seen == [
        ("token", {"viewer", "operator"}, "mcp:http"),
        ("token", {"viewer"}, "mcp:http"),
        ("dev", {"viewer", "operator"}, "mcp:http"),
    ]


def test_a_probe_for_oauth_metadata_reads_a_json_404(open_client: OpenClient, tmp_path: Path) -> None:
    """After a 401 a client looks under ``/.well-known/``: the UI's page there would not parse."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>ui</html>")
    probes = (
        "/.well-known/oauth-protected-resource/v1/mcp",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    )
    with open_client(auth={}, mcp=MCP, web_dir=web) as client:
        unknown = post(client, {})
        probed = [client.get(path) for path in probes]
        assert client.get("/runs/R-1").text == "<html>ui</html>"  # a client-side route still loads
    assert (unknown.status_code, unknown.headers["www-authenticate"]) == (401, "Bearer")
    assert [(answer.status_code, answer.json()) for answer in probed] == [(404, {"detail": "Not Found"})] * 4


def test_the_server_start_records_the_block(open_client: OpenClient) -> None:
    """``system.start`` names the block as applied; without one its ``mcp`` is null (test_audit.py)."""
    with open_client(auth={}, mcp=MCP | {"calls_per_minute": 30}) as client:
        [(actor, _, started)] = audited(client, "system.start")
    assert actor == "system:server" and started["mcp"] == {
        "data_classes": MCP["data_classes"], "http": True, "work": ["compare_key"], "calls_per_minute": 30,
        "concurrency": 4,
    }  # fmt: skip


STARTED = """\
import sys
from fastapi.testclient import TestClient
from deltasleuth.api import create_app
from deltasleuth.config import load_settings
import deltasleuth.cli.main
with TestClient(create_app(load_settings(sys.argv[1]))) as client:
    print(client.app.state.mcp is not None, "mcp" in sys.modules, "deltasleuth.mcp.server" in sys.modules)
"""


@pytest.mark.parametrize(
    ("block", "served"),
    [
        (None, False),
        ({"data_classes": ["aggregates"]}, False),
        ({"data_classes": ["aggregates"], "http": True}, True),
    ],
    ids=["no block", "http off", "http on"],
)
def test_the_library_is_imported_only_to_serve_http(
    open_client: OpenClient, tmp_path: Path, repo_root: Path, block: dict[str, Any] | None, served: bool
) -> None:
    """A server that does not serve MCP over HTTP never imports the MCP library, here in an
    interpreter of its own that has imported the API and the CLI and started the app."""
    with open_client(mcp=block):  # the settings file, the workspace and the store
        pass
    argv = [sys.executable, "-c", STARTED, str(tmp_path / "deltasleuth.yaml")]
    done = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True, check=False)
    assert (done.returncode, done.stdout) == (0, f"{served} {served} {served}\n"), done.stderr


def test_the_library_answers_below_the_route(open_client: OpenClient, callers: Any) -> None:
    """Below the route the answers are the library's own, some of them plain text: each status is
    pinned, since a client reads them."""
    with open_client(auth={}, mcp=MCP) as client:
        viewer = callers(client).token("viewer")

        def sent(headers: dict[str, str] | None = None, **content: Any) -> Any:
            return client.post(
                "/v1/mcp", headers=viewer | (ACCEPT if headers is None else headers), **content
            )

        plain = {"Content-Type": "application/json"}
        modern = ACCEPT | MODERN
        unknown = {"jsonrpc": "2.0", "id": 1, "method": "nope/nope"}
        notified = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        answers = {
            "content type": sent(ACCEPT | {"Content-Type": "text/plain"}, content=b"{}"),
            "too large": sent(ACCEPT | plain, content=b" " * 65_537),
            "accept": sent({"Accept": "text/html"}, json=LIST),
            "accept, modern": sent({"Accept": "text/html"} | MODERN, json=LIST),
            "unparsable": sent(ACCEPT | plain, content=b"{nope"),
            "unparsable, modern": sent(modern | plain, content=b"{nope"),
            "notification": sent(json=notified),
            "notification, modern": sent(modern, json=notified),
            "unknown method": sent(json=unknown),
            "unknown method, modern": sent(
                modern | {"Mcp-Method": "nope/nope"}, json=unknown | {"params": {"_meta": META}}
            ),
            "no method header": sent(modern, json=LIST | {"params": {"_meta": META}}),
            "another version": sent(ACCEPT | {"MCP-Protocol-Version": "1999-01-01"}, json=LIST),
            "the fullest body": sent(ACCEPT | plain, content=json.dumps(LIST).encode().ljust(65_536)),
        }
    assert {name: answer.status_code for name, answer in answers.items()} == {
        "content type": 400, "too large": 413, "accept": 406, "accept, modern": 406, "unparsable": 400,
        "unparsable, modern": 400, "notification": 202, "notification, modern": 202, "unknown method": 200,
        "unknown method, modern": 404, "no method header": 400, "another version": 400,
        "the fullest body": 200,
    }  # fmt: skip
    assert (answers["content type"].text, answers["too large"].text) == (
        "Invalid Content-Type header",
        "Request body too large",
    )
    assert [
        answers[name].json()["error"]["code"] for name in ("unknown method", "unknown method, modern")
    ] == [-32601] * 2
    assert answers["no method header"].json()["error"]["code"] == -32020
    assert not [name for name, answer in answers.items() if "x-request-id" not in answer.headers]
