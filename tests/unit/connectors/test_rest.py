"""RestConnector against local aiohttp servers: statuses, retries, guards, auth, politeness."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
import socket
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
from aiohttp import web
from yarl import URL

from deltasleuth.connectors import http as http_module
from deltasleuth.connectors.base import Connector, ConnectorConfigError, KeyRejectedError, UnsafeTargetError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.rest import RestConnector
from deltasleuth.core.canonical.encoding import content_hash
from deltasleuth.core.secrets import SecretError
from deltasleuth.core.types import RetryPolicy, Side, SideConfig, Target, TargetAuth
from tests.conftest import evidence_store

from .conftest import MakeConnector, Recorder, Served, Start, fast_execution, side_config

ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
UNREAD_REF = "file:/nowhere"  # a secret reference; the readers under test return the value
NO_RETRY = fast_execution(retries=RetryPolicy(max=0))


def ok(request: web.Request) -> web.Response:
    return web.json_response({})


def status(code: int, body: bytes = b"{}", **headers: str):
    return lambda request: web.Response(status=code, body=body, headers=headers)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def resolver_for(*answers: list[str]):
    """A fake DNS: successive calls return successive answers (the last one repeats)."""
    queue = list(answers)

    async def resolve(host: str, port: int) -> list[str]:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return resolve


class TestSuccessAndPassthrough:
    async def test_success(self, start: Start, recorder: Recorder) -> None:
        def reply(request: web.Request) -> web.Response:
            response = web.json_response(
                {"id": request.match_info["key"], "n": 1.50},
                headers={"X-Trace": "t1", "Set-Cookie": "s=1", "WWW-Authenticate": "Bearer"},
            )
            response.headers.add("X-Multi", "one")
            response.headers.add("X-Multi", "two")
            return response

        conn = await start(reply)
        obs = await conn.fetch("K1", side_config(), run_id="r1", call_seq=4)
        assert isinstance(conn, Connector)
        assert (obs.key, obs.side, obs.http_status, obs.error_class, obs.source) == (
            "K1",
            Side.A,
            200,
            None,
            "live",
        )
        assert json.loads(obs.body or b"") == {"id": "K1", "n": 1.5}
        assert obs.content_hash == content_hash(obs.body)
        assert (obs.attempts, obs.pages, obs.pages_eof, obs.pagination_unstable) == (1, 1, True, False)
        assert obs.request_url is not None and obs.request_url.endswith("/api/things/K1")
        assert (obs.headers["x-trace"], obs.headers["x-multi"]) == ("t1", "one, two")
        assert "set-cookie" not in obs.headers and "www-authenticate" not in obs.headers
        assert all(name == name.lower() for name in obs.headers)
        assert obs.latency_ms is not None and obs.latency_ms > 0
        assert ISO_UTC.match(obs.observed_at)
        assert recorder.requests[0]["headers"]["User-Agent"] == "deltasleuth-p0"

    @pytest.mark.parametrize("code", [404, 410, 400, 204, 409])
    async def test_other_statuses_returned_as_is(self, code: int, start: Start, recorder: Recorder) -> None:
        conn = await start(status(code, b"" if code == 204 else b'{"error":"x"}'))
        obs = await conn.fetch("K1", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts, recorder.count) == (code, None, 1, 1)

    async def test_query_headers_and_side(self, start: Start, recorder: Recorder) -> None:
        conn = await start(ok)
        cfg = side_config(
            query={"ids": ["a", "{{ key }}"], "depth": 1},
            headers={"X-Correlation-Id": "ds-{{ run_id }}-{{ call_seq }}", "Accept": "application/json"},
            side=Side.B,
        )
        obs = await conn.fetch("K/1", cfg, run_id="run9", call_seq=12)
        seen = recorder.requests[0]
        assert seen["query"] == [("ids", "a"), ("ids", "K/1"), ("depth", "1")]
        assert seen["headers"]["X-Correlation-Id"] == "ds-run9-12"
        assert seen["raw_path"].startswith("/api/things/K%2F1?")
        assert obs.side is Side.B

    async def test_graphql_post_sends_the_key_as_json_data(self, start: Start, recorder: Recorder) -> None:
        conn = await start(ok, "/api/graphql")
        query = "query($id: ID!) { character(id: $id) { name } }"
        cfg = side_config("/graphql", method="POST", body={"query": query, "variables": {"id": "{{ key }}"}})
        key = '1" } } mutation { delete(id: "1'
        assert (await conn.fetch(key, cfg)).http_status == 200
        seen = recorder.requests[0]
        assert seen["method"] == "POST" and seen["headers"]["Content-Type"] == "application/json"
        assert json.loads(seen["body"]) == {"query": query, "variables": {"id": key}}

    async def test_cookies_are_not_carried_between_keys(self, start: Start, recorder: Recorder) -> None:
        def reply(request: web.Request) -> web.Response:
            response = web.json_response({})
            response.set_cookie("session", "abc")
            return response

        conn = await start(reply)
        await conn.fetch("K1", side_config())
        await conn.fetch("K2", side_config())
        assert "Cookie" not in recorder.requests[1]["headers"]

    async def test_close_and_file_sides(self, make_connector: MakeConnector) -> None:
        conn = make_connector("http://127.0.0.1:9/api")
        with pytest.raises(ConnectorConfigError):  # a side without a request template
            await conn.fetch("K1", SideConfig(side=Side.A, role="legacy", target="t"))
        await conn.aclose()
        await conn.aclose()


class TestRetries:
    async def test_5xx_then_success(self, start: Start, recorder: Recorder) -> None:
        async def reply(request: web.Request) -> web.Response:
            if recorder.count == 1:
                await asyncio.sleep(0.3)  # the failed attempt is slow; latency must exclude it
                return web.Response(status=503)
            return web.json_response({"ok": 1})

        obs = await (await start(reply)).fetch("K1", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts) == (200, None, 2)
        assert obs.latency_ms is not None and obs.latency_ms < 250

    @pytest.mark.parametrize(("code", "error_class"), [(500, "http_5xx"), (429, "http_429")])
    async def test_retries_exhausted(
        self, code: int, error_class: str, start: Start, recorder: Recorder
    ) -> None:
        obs = await (await start(status(code, b"boom"))).fetch("K1", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts, recorder.count) == (code, error_class, 3, 3)
        assert obs.body == b"boom" and obs.content_hash == content_hash(b"boom")

    async def test_retry_on_respected(self, start: Start, recorder: Recorder) -> None:
        execution = fast_execution(
            retries=RetryPolicy(max=3, initial_s=0.01, max_s=0.02, retry_on=("timeout",))
        )
        obs = await (await start(status(502), execution=execution)).fetch("K1", side_config())
        assert (obs.error_class, obs.attempts, recorder.count) == ("http_5xx", 1, 1)

    async def test_429_retry_after_is_waited_in_full(self, start: Start, recorder: Recorder) -> None:
        def reply(request: web.Request) -> web.Response:
            return (
                web.Response(status=429, headers={"Retry-After": "1"}) if recorder.count == 1 else ok(request)
            )

        execution = fast_execution(retries=RetryPolicy(max=2, initial_s=0.001, max_s=0.3))
        conn = await start(reply, execution=execution)
        started = time.monotonic()
        obs = await conn.fetch("K1", side_config())
        assert (obs.http_status, obs.attempts) == (200, 2)
        assert 1.0 <= time.monotonic() - started < 1.9  # the full second, although backoff max is 0.3 s

    async def test_timeout(self, start: Start, recorder: Recorder) -> None:
        async def reply(request: web.Request) -> web.Response:
            await asyncio.sleep(2)
            return ok(request)

        execution = fast_execution(read_timeout_s=0.2, retries=RetryPolicy(max=1, initial_s=0.01, max_s=0.02))
        obs = await (await start(reply, execution=execution)).fetch("K1", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts, obs.body, recorder.count) == (
            None,
            "timeout",
            2,
            None,
            2,
        )
        assert "K1" not in (obs.error_detail or "")

    async def test_total_timeout_caps_retries(self, start: Start) -> None:
        async def reply(request: web.Request) -> web.Response:
            await asyncio.sleep(2)
            return ok(request)

        execution = fast_execution(
            read_timeout_s=5.0, total_timeout_s=0.3, retries=RetryPolicy(max=5, initial_s=0.01, max_s=0.02)
        )
        conn = await start(reply, execution=execution)
        started = time.monotonic()
        assert (await conn.fetch("K1", side_config())).error_class == "timeout"
        assert time.monotonic() - started < 1.0

    async def test_connect_refused(self, make_connector: MakeConnector) -> None:
        obs = await make_connector(f"http://127.0.0.1:{free_port()}/api").fetch("SECRETKEY", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts) == (None, "connect_error", 3)
        assert "SECRETKEY" not in (obs.error_detail or "")

    @pytest.mark.parametrize("code", [401, 403])
    async def test_auth_error_not_retried_and_body_dropped(
        self, code: int, start: Start, recorder: Recorder
    ) -> None:
        obs = await (await start(status(code, b'{"echo": "Bearer leaked"}'))).fetch("K1", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts, recorder.count) == (code, "auth_error", 1, 1)
        assert obs.body is None and obs.content_hash is None


class TestRedirectsAndLimits:
    async def test_redirect_not_followed(self, start: Start, recorder: Recorder) -> None:
        def reply(request: web.Request) -> web.Response:
            target = "http://evil.example/steal" if request.match_info["key"] == "X" else "/api/things/X"
            return web.Response(status=302, headers={"Location": target})

        conn = await start(reply)
        cross, same = await conn.fetch("X", side_config()), await conn.fetch("Y", side_config())
        assert (cross.http_status, same.http_status, cross.error_class, same.error_class) == (
            302,
            302,
            None,
            None,
        )
        assert cross.headers["location"] == "http://evil.example/steal"
        assert recorder.count == 2  # neither redirect was followed

    @pytest.mark.parametrize("streamed", [False, True])
    async def test_body_cap(self, streamed: bool, start: Start) -> None:
        async def reply(request: web.Request) -> web.StreamResponse:
            if not streamed:
                return web.Response(body=b"x" * 5000)  # declared by Content-Length
            response = web.StreamResponse()
            response.enable_chunked_encoding()
            await response.prepare(request)
            for _ in range(5):
                await response.write(b"y" * 700)
            await response.write_eof()
            return response

        obs = await (await start(reply, max_body_bytes=1000)).fetch("K1", side_config())
        assert (obs.error_class, obs.body, obs.content_hash, obs.attempts, obs.http_status) == (
            "resource_limit",
            None,
            None,
            1,
            200,
        )

    async def test_body_at_cap_is_fine(self, start: Start) -> None:
        obs = await (await start(status(200, b"z" * 1000), max_body_bytes=1000)).fetch("K1", side_config())
        assert obs.error_class is None and len(obs.body or b"") == 1000

    async def test_oversized_5xx_stays_retryable(self, start: Start, recorder: Recorder) -> None:
        def reply(request: web.Request) -> web.Response:
            return web.Response(status=503, body=b"e" * 5000) if recorder.count == 1 else ok(request)

        obs = await (await start(reply, max_body_bytes=1000)).fetch("K1", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts) == (200, None, 2)


class TestAuth:
    def reply(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True}, headers={"Authorization": "echoed"})

    @pytest.mark.parametrize(
        ("auth", "header", "sent"),
        [
            (TargetAuth(kind="bearer"), "Authorization", "Bearer tok-9f8e7d"),
            (TargetAuth(kind="header", header="X-Service-Token"), "X-Service-Token", "tok-9f8e7d"),
        ],
    )
    async def test_credential_from_secret_file(
        self, auth: TargetAuth, header: str, sent: str, tmp_path: Path, start: Start, recorder: Recorder
    ) -> None:
        (tmp_path / "token").write_text("tok-9f8e7d\n")
        auth = dataclasses.replace(auth, secret_ref=f"file:{tmp_path / 'token'}")
        obs = await (await start(self.reply, auth=auth)).fetch("K1", side_config())
        assert recorder.requests[0]["headers"][header] == sent
        assert "tok-9f8e7d" not in repr(obs) and "authorization" not in obs.headers

    async def test_every_credential_header_is_sent_and_none_recorded(
        self, tmp_path: Path, start: Start, recorder: Recorder
    ) -> None:
        """A session cookie with further headers (``TargetAuth.more``): each from its own file, none for a
        template to set, and one the target echoes is no evidence."""
        sent = {"Cookie": "sid=9f8e7d", "X-CSRF-Token": "tok-1a2b3c", "X-Tenant": "acme-4d5e"}
        for name, value in sent.items():
            (tmp_path / name).write_text(value)
        first, *more = ((name, f"file:{tmp_path / name}") for name in sent)
        auth = TargetAuth("header", *first, more=tuple(more))

        def echo(request: web.Request) -> web.Response:
            return web.json_response({"ok": True}, headers={"x-csrf-token": request.headers["X-CSRF-Token"]})

        connector = await start(echo, auth=auth)
        obs = await connector.fetch("K1", side_config())
        assert {name: recorder.requests[0]["headers"][name] for name in sent} == sent
        assert obs.http_status == 200 and "x-csrf-token" not in obs.headers and "content-type" in obs.headers
        assert not [value for value in sent.values() if value in repr(obs)]
        with pytest.raises(ConnectorConfigError, match="may not be set by a template"):
            await connector.fetch("K1", side_config(headers={"x-tenant": "other"}))
        unusable = TargetAuth("header", *first, more=(("Host", more[0][1]),))
        with pytest.raises(ConnectorConfigError, match="cannot carry credentials"):
            await start(echo, auth=unusable)

    def test_environment_never_consulted(
        self, monkeypatch: pytest.MonkeyPatch, make_connector: MakeConnector
    ) -> None:
        monkeypatch.setenv("TOKEN", "from-env")
        for ref in ("env:TOKEN", "file:$TOKEN"):
            with pytest.raises(SecretError):
                make_connector("http://127.0.0.1:9/api", auth=TargetAuth(kind="bearer", secret_ref=ref))

    async def test_injected_secret_reader(self, start: Start, recorder: Recorder) -> None:
        refs: list[str] = []

        def reader(ref: str) -> str:
            refs.append(ref)
            return "injected"

        auth = TargetAuth(kind="bearer", secret_ref=UNREAD_REF)
        await (await start(self.reply, auth=auth, secret_reader=reader)).fetch("K1", side_config())
        assert refs == [UNREAD_REF]
        assert recorder.requests[0]["headers"]["Authorization"] == "Bearer injected"

    @pytest.mark.parametrize(
        ("auth", "secret"),
        [
            (TargetAuth(kind="bearer"), "x"),  # no secret_ref
            (TargetAuth(kind="header", secret_ref=UNREAD_REF), "x"),  # no header name
            (TargetAuth(kind="bearer", secret_ref=UNREAD_REF), ""),
            (TargetAuth(kind="bearer", secret_ref=UNREAD_REF), "a\nb"),
            (TargetAuth(kind="header", header="Host", secret_ref=UNREAD_REF), "x"),
            (TargetAuth(kind="header", header="Bad Name", secret_ref=UNREAD_REF), "x"),
        ],
    )
    def test_unusable_auth_rejected_without_echo(
        self, auth: TargetAuth, secret: str, make_connector: MakeConnector
    ) -> None:
        with pytest.raises(ConnectorConfigError) as info:
            make_connector("http://127.0.0.1:9/api", auth=auth, secret_reader=lambda ref: secret)
        assert "a\nb" not in str(info.value)


class TestKeysAndOrigin:
    @pytest.mark.parametrize(
        ("key", "segment"),
        [("a/b", "a%2Fb"), ("a b", "a%20b"), ("éß", "%C3%A9%C3%9F"), ("x%2F..", "x%252F..")],
    )
    async def test_key_encoding(self, key: str, segment: str, start: Start, recorder: Recorder) -> None:
        conn = await start(lambda request: web.json_response({"key": request.match_info["key"]}))
        obs = await conn.fetch(key, side_config())
        assert recorder.requests[0]["raw_path"] == f"/api/things/{segment}"
        assert json.loads(obs.body or b"")["key"] == key  # the server decodes the same key back

    @pytest.mark.parametrize(
        ("key", "path", "error"),
        [
            ("..", "/things/{{ key }}", KeyRejectedError),
            (".", "/things/{{ key }}", KeyRejectedError),
            ("", "/things/{{ key }}", KeyRejectedError),
            ("K1", "/../admin/{{ key }}", UnsafeTargetError),
            ("K1", "//evil.example/{{ key }}", UnsafeTargetError),
            ("K1", "/x/%2E%2E/{{ key }}", UnsafeTargetError),
        ],
    )
    async def test_refused_before_any_call(
        self, key: str, path: str, error: type[Exception], start: Start, recorder: Recorder
    ) -> None:
        conn = await start(ok, "/{tail:.*}")
        with pytest.raises(error):
            await conn.fetch(key, side_config(path))
        assert recorder.count == 0

    def test_file_only_target_rejected(self, make_connector: MakeConnector) -> None:
        with pytest.raises(ConnectorConfigError):
            make_connector("")


class TestSSRF:
    async def test_loopback_refused_without_opt_in(self, start: Start, recorder: Recorder) -> None:
        conn = await start(ok, allow_private_network=False)
        with pytest.raises(UnsafeTargetError):
            await conn.fetch("K1", side_config())
        assert recorder.count == 0

    async def test_private_resolution_refused(self, make_connector: MakeConnector) -> None:
        conn = make_connector(
            "http://internal.example:8080/api",
            allow_private_network=False,
            resolver=resolver_for(["10.20.30.40"]),
        )
        with pytest.raises(UnsafeTargetError):
            await conn.fetch("K1", side_config())

    async def test_every_connection_is_resolved_through_the_guard(
        self, served: Served, make_connector: MakeConnector
    ) -> None:
        # No DNS cache and no pre-flight lookup: the address connected to is the address checked,
        # on every new connection, so an answer that changes later (DNS rebinding) is checked too.
        def reply(request: web.Request) -> web.Response:
            response = web.json_response({"host": request.host})
            response.force_close()
            return response

        calls: list[str] = []

        async def resolve(host: str, port: int) -> list[str]:
            calls.append(host)
            return ["127.0.0.1"]

        port = URL(await served(reply)).port
        conn = make_connector(f"http://svc.test:{port}/api", resolver=resolve)  # opted in to private networks
        for key in ("K1", "K2"):
            obs = await conn.fetch(key, side_config())
            assert json.loads(obs.body or b"")["host"] == f"svc.test:{port}"
        assert calls == ["svc.test", "svc.test"]


class TestPoliteness:
    async def test_rate_budget(self, start: Start) -> None:
        arrivals: list[float] = []

        def reply(request: web.Request) -> web.Response:
            arrivals.append(time.monotonic())
            return ok(request)

        conn = await start(reply, rate_per_s=10, max_concurrency=10)
        started = time.monotonic()
        results = await asyncio.gather(*(conn.fetch(f"K{i}", side_config()) for i in range(25)))
        assert all(r.http_status == 200 for r in results)
        assert time.monotonic() - started >= 2.2  # 25 calls at 10/s: 24 gaps of 0.1 s
        arrivals.sort()
        for i, t in enumerate(arrivals):
            assert sum(1 for u in arrivals[i:] if u - t < 1.0) <= 11  # never more than rate + 1 per second

    async def test_gate_shared_between_connectors(
        self, served: Served, make_connector: MakeConnector
    ) -> None:
        base = await served(ok) + "/api"
        gate = TargetGate.for_target(Target(target_id="t", base_url=base, rate_per_s=20))
        one, two = make_connector(base, gate=gate), make_connector(base, gate=gate)
        started = time.monotonic()
        await asyncio.gather(
            *(one.fetch(f"A{i}", side_config()) for i in range(10)),
            *(two.fetch(f"B{i}", side_config()) for i in range(10)),
        )
        assert time.monotonic() - started >= 0.85  # 20 calls share one 20/s budget

    async def test_budget_queueing_is_not_a_timeout(self, start: Start) -> None:
        # At 4 req/s the fourth call queues ~0.75 s for the budget, longer than the 0.4 s total
        # timeout; the target answers at once, so no call may be reported as a timeout.
        conn = await start(ok, rate_per_s=4, execution=fast_execution(total_timeout_s=0.4))
        results = await asyncio.gather(*(conn.fetch(f"K{i}", side_config()) for i in range(4)))
        assert [r.error_class for r in results] == [None] * 4

    async def test_concurrency_cap(self, start: Start) -> None:
        in_flight = peak = 0

        async def reply(request: web.Request) -> web.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.1)
            in_flight -= 1
            return ok(request)

        conn = await start(reply, max_concurrency=2)
        await asyncio.gather(*(conn.fetch(f"K{i}", side_config()) for i in range(8)))
        assert peak == 2

    @pytest.mark.parametrize("change", [{"rate_per_s": 0}, {"execution": fast_execution(total_timeout_s=0)}])
    def test_invalid_budget_or_timeout(self, change: dict, make_connector: MakeConnector) -> None:
        with pytest.raises(ConnectorConfigError):
            make_connector("http://svc.example", **change)


BREAKER = fast_execution(
    breaker_error_rate=0.5, breaker_window=2, breaker_cooldown_s=0.3, retries=RetryPolicy(max=0)
)


class TestCircuitBreaker:
    async def breaker_server(self, start: Start, state: dict) -> RestConnector:
        async def reply(request: web.Request) -> web.Response:
            await asyncio.sleep(state.get("delay", 0))
            return web.Response(status=state["status"], body=b"{}")

        return await start(reply, execution=BREAKER)

    async def test_opens_then_probe_closes(self, start: Start, recorder: Recorder) -> None:
        state = {"status": 500}
        conn = await self.breaker_server(start, state)
        assert [(await conn.fetch(k, side_config())).error_class for k in ("K1", "K2")] == ["http_5xx"] * 2
        refused = await conn.fetch("K3", side_config())
        assert (refused.error_class, refused.attempts, refused.pages, refused.http_status) == (
            "circuit_open",
            0,
            0,
            None,
        )
        assert refused.request_url is not None and refused.request_url.endswith("/api/things/K3")
        assert recorder.count == 2  # the refused call never reached the target
        state["status"] = 200
        await asyncio.sleep(0.35)
        probe = await conn.fetch("K4", side_config())
        assert (probe.http_status, probe.error_class) == (200, None)
        assert (await conn.fetch("K5", side_config())).http_status == 200

    async def test_failed_probe_reopens(self, start: Start, recorder: Recorder) -> None:
        conn = await self.breaker_server(start, {"status": 503})
        for key in ("K1", "K2"):
            await conn.fetch(key, side_config())
        await asyncio.sleep(0.35)
        assert (await conn.fetch("K3", side_config())).error_class == "http_5xx"  # the probe
        assert (await conn.fetch("K4", side_config())).error_class == "circuit_open"
        assert recorder.count == 3

    async def test_one_probe_at_a_time(self, start: Start) -> None:
        state = {"status": 500}
        conn = await self.breaker_server(start, state)
        for key in ("K1", "K2"):
            await conn.fetch(key, side_config())
        state.update(status=200, delay=0.2)
        await asyncio.sleep(0.35)
        first, second = await asyncio.gather(conn.fetch("K3", side_config()), conn.fetch("K4", side_config()))
        assert sorted([first.error_class or "ok", second.error_class or "ok"]) == ["circuit_open", "ok"]

    async def test_not_found_is_not_an_error(self, start: Start, recorder: Recorder) -> None:
        conn = await self.breaker_server(start, {"status": 404})
        assert [(await conn.fetch(f"K{i}", side_config())).http_status for i in range(4)] == [404] * 4
        assert recorder.count == 4


# Regression tests


class TestRetryAfterIsHonoured:
    """Retry-After was capped at backoff max and ignored by every other worker."""

    @pytest.mark.parametrize("seconds", ["20", "date"])
    async def test_no_request_before_retry_after_even_if_the_call_gives_up(
        self, seconds: str, start: Start, recorder: Recorder
    ) -> None:
        def reply(request: web.Request) -> web.Response:
            later = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
            return web.Response(status=429, headers={"Retry-After": later if seconds == "date" else seconds})

        execution = fast_execution(total_timeout_s=3.0, retries=RetryPolicy(max=3, initial_s=0.01, max_s=1.0))
        conn = await start(reply, execution=execution)
        started = time.monotonic()
        obs = await conn.fetch("K1", side_config())
        assert (obs.http_status, obs.error_class, obs.attempts, recorder.count) == (429, "http_429", 1, 1)
        assert time.monotonic() - started < 0.9  # gave up at once instead of retrying early

    async def test_other_workers_pause_for_the_target(self, start: Start) -> None:
        arrivals: list[float] = []

        def reply(request: web.Request) -> web.Response:
            arrivals.append(time.monotonic())
            return (
                web.Response(status=429, headers={"Retry-After": "1"}) if len(arrivals) == 1 else ok(request)
            )

        conn = await start(reply, max_concurrency=4, execution=NO_RETRY)
        assert (await conn.fetch("K0", side_config())).error_class == "http_429"
        results = await asyncio.gather(*(conn.fetch(f"K{i}", side_config()) for i in range(1, 5)))
        assert [r.http_status for r in results] == [200] * 4
        assert all(t - arrivals[0] >= 1.0 for t in arrivals[1:])

    async def test_pause_beyond_the_total_timeout_sends_nothing(
        self, start: Start, recorder: Recorder
    ) -> None:
        execution = fast_execution(
            total_timeout_s=1.0,
            retries=RetryPolicy(max=0),
            breaker_error_rate=0.5,
            breaker_window=2,
            breaker_cooldown_s=60.0,
        )
        gate = TargetGate.for_target(Target(target_id="t", base_url="http://127.0.0.1"))
        conn = await start(status(429, **{"Retry-After": "30"}), execution=execution, gate=gate)
        assert (await conn.fetch("K1", side_config())).attempts == 1
        started = time.monotonic()
        for key in ("K2", "K3"):
            skipped = await conn.fetch(key, side_config())
            assert (skipped.error_class, skipped.attempts, skipped.pages, skipped.http_status) == (
                "http_429",
                0,
                0,
                None,
            )
            assert "Retry-After" in (skipped.error_detail or "")
        assert time.monotonic() - started < 0.5 and recorder.count == 1
        # calls that never reached the target say nothing of its health
        assert gate.breaker(execution).state == "closed"


class TestMalformedResponses:
    """A malformed header raised ValueError out of fetch() and cancelled the run."""

    async def test_unicode_digit_retry_after(self, start: Start) -> None:
        execution = fast_execution(retries=RetryPolicy(max=1, initial_s=0.01, max_s=0.02))
        obs = await (await start(status(429, **{"Retry-After": "²"}), execution=execution)).fetch(
            "K1", side_config()
        )
        assert (obs.error_class, obs.attempts) == ("http_429", 2)

    async def test_uninterpretable_response_is_a_failed_exchange(
        self, start: Start, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unreadable(headers: object) -> dict[str, str]:
            raise ValueError("the service sent something the client library cannot interpret")

        monkeypatch.setattr(http_module, "record_headers", unreadable)
        obs = await (await start(ok, execution=NO_RETRY)).fetch("K1", side_config())
        assert (obs.error_class, obs.error_detail) == (
            "connect_error",
            "response could not be interpreted (ValueError)",
        )


async def test_a_rebudgeted_gate_holds_for_an_open_connector(start: Start) -> None:
    """``TargetGate.rebudget`` (a target declared in the product was changed): a connector opened under
    the gate takes its new slots at once, more of them too, which its session was not opened for."""
    flying, most, slots, taken = 0, 0, 1, asyncio.Event()

    async def held(_: web.Request) -> web.Response:
        nonlocal flying, most
        flying += 1
        most = max(most, flying)
        if flying == slots:
            taken.set()
        with contextlib.suppress(TimeoutError):  # held until every slot is taken, which fewer never are
            await asyncio.wait_for(taken.wait(), 5)
        await asyncio.sleep(0.05)  # a call past the slots would arrive now
        flying -= 1
        return web.json_response({"ok": True})

    gate = TargetGate(1000, 1)
    connector = await start(held, gate=gate)
    for slots in (1, 4, 2):  # ``held`` reads it
        gate.rebudget(1000, slots)
        most, taken = 0, asyncio.Event()
        observations = await asyncio.gather(*(connector.fetch(f"K{n}", side_config()) for n in range(6)))
        assert (most, {obs.http_status for obs in observations}) == (slots, {200})
    with pytest.raises(ConnectorConfigError):
        gate.rebudget(0, 1)
    assert gate.max_concurrency == 2  # a refused budget changes nothing


async def test_breaker_is_checked_again_before_each_send(start: Start, recorder: Recorder) -> None:
    # Calls admitted while the circuit was closed but still queued at the gate used to
    # reach the target after the circuit opened.
    execution = fast_execution(
        breaker_error_rate=0.5, breaker_window=2, breaker_cooldown_s=60.0, retries=RetryPolicy(max=0)
    )
    conn = await start(status(503), execution=execution, max_concurrency=1)
    results = await asyncio.gather(*(conn.fetch(f"K{i}", side_config()) for i in range(8)))
    assert recorder.count == 2  # the two calls that filled the window; nothing after it opened
    refused = [r for r in results if r.error_class == "circuit_open"]
    assert len(refused) == 6 and all((r.attempts, r.pages, r.http_status) == (0, 0, None) for r in refused)


@pytest.mark.parametrize("failure", ["http_5xx", "auth_error", "connect_error", "timeout"])
async def test_failed_fetches_are_stored_as_evidence(
    failure: str, tmp_path: Path, start: Start, make_connector: MakeConnector
) -> None:
    # Failures are evidence too; the store must take every observation a connector returns.
    async def reply(request: web.Request) -> web.Response:
        if failure == "timeout":
            await asyncio.sleep(2)
        return web.Response(
            status={"http_5xx": 503, "auth_error": 401}.get(failure, 200), body=b'{"error": "busy"}'
        )

    execution = fast_execution(read_timeout_s=0.2, retries=RetryPolicy(max=0))
    if failure == "connect_error":
        conn = make_connector(f"http://127.0.0.1:{free_port()}/api", execution=execution)
    else:
        conn = await start(reply, execution=execution)
    obs = await conn.fetch("K1", side_config())
    assert obs.error_class == failure
    stored = evidence_store(tmp_path).put_blob(obs)
    fields = ("error_class", "http_status", "attempts", "body")
    assert stored.obs_id is not None
    assert [getattr(stored, f) for f in fields] == [getattr(obs, f) for f in fields]
