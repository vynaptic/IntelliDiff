"""The app shell: health, the request guard (developer mode, Host, Origin, headers), JSON errors,
service facts, the UI mount and the worker."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from deltasleuth.api.app import MAX_BODY, AuthQueryFilter
from deltasleuth.connectors.tables import ENGINES, TargetRefused, TargetUnreachable
from tests.unit.connectors.engines import FakeEngine
from tests.unit.connectors.test_postgres import compiled

from .conftest import OIDC, TARGETS

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]


@pytest.mark.parametrize(
    ("auth", "mode"),
    [(None, "none"), ({}, "tokens"), ({"oidc": OIDC}, "oidc")],
    ids=["developer", "tokens", "oidc"],
)
def test_health_needs_no_credential_and_says_what_is_on(
    open_client: OpenClient, auth: dict[str, Any] | None, mode: str
) -> None:
    with open_client(auth=auth, ai=True) as client:
        assert client.get("/v1/health").json() == {"status": "ok", "ai": True, "auth": mode}


def test_health_says_when_the_database_does_not_answer(
    open_client: OpenClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/mvp/FIRSTRUN.md §8 S2: three seconds for a connection, under the image health check's four;
    and one text, which names no wait as passed: a pooled connection the database has closed fails at
    once."""
    waited = []

    def busy(timeout: float | None = None) -> Any:
        waited.append(timeout)
        raise PoolTimeout("couldn't get a connection after 3.00 sec")

    class Closed:
        async def execute(self, _: str) -> None:
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    @asynccontextmanager
    async def closed(timeout: float | None = None) -> AsyncIterator[Closed]:
        waited.append(timeout)
        yield Closed()

    with open_client(auth={}) as client:
        replies = []
        for connection in (busy, closed):
            with monkeypatch.context() as patched:
                patched.setattr(client.app.state.ctx.pool, "connection", connection)
                replies.append(client.get("/v1/health"))
    assert [(down.status_code, down.json()) for down in replies] == [
        (503, {"detail": "the database is not answering, or the pool gave no connection in 3 s"})
    ] * 2
    assert waited == [3, 3]
    down = replies[0]
    assert (down.headers["cache-control"], down.headers["x-content-type-options"]) == ("no-store", "nosniff")
    assert down.headers["x-request-id"].startswith("Q-") and "strict-transport-security" in down.headers


def test_an_oversized_body_is_refused_before_it_is_read_or_authorised(open_client: OpenClient) -> None:
    def chunked() -> Iterator[bytes]:  # no Content-Length: counted as it streams
        yield b'{"spec_id": 1, "mode": "batch", "keys": ["'
        yield b"K" * MAX_BODY
        yield b'"]}'

    with open_client(auth={}) as client:
        declared = client.post("/v1/runs", content=b" " * (MAX_BODY + 1))
        streamed = client.post("/v1/runs", content=chunked())
    assert [(r.status_code, r.json()["detail"]) for r in (declared, streamed)] == [
        (413, f"a request body is at most {MAX_BODY} bytes")
    ] * 2


def test_responses_carry_security_headers_and_no_api_docs(open_client: OpenClient, tmp_path: Path) -> None:
    with open_client(web_dir=tmp_path / "no-ui") as client:
        health = client.get("/v1/health")
        docs = [client.get(path).status_code for path in ("/docs", "/redoc", "/openapi.json")]
    assert docs == [404, 404, 404]
    assert {name: health.headers[name] for name in ("x-content-type-options", "referrer-policy")} == {
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }
    csp = health.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self'" in csp


def test_every_response_has_a_new_request_id(client: TestClient) -> None:
    ids = [client.get("/v1/specs", headers={"X-Request-ID": "Q-0000000000000000"}) for _ in range(2)]
    first, second = (r.headers["x-request-id"] for r in ids)
    assert re.fullmatch(r"Q-[0-9a-f]{16}", first) and first != second != "Q-0000000000000000"


def test_api_responses_are_not_stored_and_https_gets_hsts(open_client: OpenClient, tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>ui</html>")
    with open_client(auth={}, web_dir=web) as client:
        api, ui = client.get("/v1/health"), client.get("/")
    assert (api.headers["cache-control"], "cache-control" in ui.headers) == ("no-store", False)
    hsts = "strict-transport-security"
    assert api.headers[hsts] == ui.headers[hsts] == "max-age=31536000"
    with open_client(auth={"public_url": "http://127.0.0.1:8000"}) as client:
        assert hsts not in client.get("/v1/health").headers


def test_developer_mode_serves_loopback_clients_only(open_client: OpenClient) -> None:
    with open_client(client=("10.0.0.9", 50000)) as client:
        refused = [client.get(path) for path in ("/v1/health", "/v1/specs", "/")]
    assert [(r.status_code, r.json()["detail"]) for r in refused] == [
        (403, "developer mode serves loopback clients only")
    ] * 3


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Via"]
)
def test_developer_mode_refuses_proxied_requests(open_client: OpenClient, header: str) -> None:
    with open_client() as client:
        refused = client.get("/v1/specs", headers={header: "127.0.0.1"})
    assert (refused.status_code, refused.json()["detail"]) == (403, "developer mode refuses proxied requests")


def test_developer_mode_acts_as_admin_behind_a_csrf_header(client: TestClient, spec_yaml: str) -> None:
    assert client.get("/v1/specs").json() == {"items": []}
    refused = client.post("/v1/specs", content=spec_yaml, headers={"X-CSRF-Token": "other"})
    assert (refused.status_code, refused.json()["detail"]) == (403, "missing or invalid CSRF token")
    assert client.post("/v1/specs", content=spec_yaml).status_code == 201
    assert client.get("/v1/specs", headers={"Host": "localhost:5173"}).status_code == 200  # the dev server
    rebound = client.get("/v1/specs", headers={"Host": "evil.example"})
    assert (rebound.status_code, rebound.json()["detail"]) == (400, "unexpected Host header")


@pytest.mark.parametrize(
    ("public_url", "host", "status"),
    [
        ("https://ds.test", "ds.test", 200),
        ("https://ds.test", "ds.test:443", 200),
        ("https://ds.test", "DS.TEST", 200),
        ("https://ds.test", "ds.test:8443", 400),
        ("https://ds.test", "api.ds.test", 200),  # allowed_hosts
        ("https://ds.test", "127.0.0.1:8000", 200),  # loopback names, any port
        ("https://ds.test", "[::1]", 200),
        ("https://ds.test", "10.0.0.1", 400),
        ("https://ds.test", "ds.test@evil.example", 400),
        ("https://ds.test:8443", "ds.test:8443", 200),
        ("https://ds.test:8443", "ds.test", 400),
    ],
)
def test_the_host_header_must_name_this_service(
    open_client: OpenClient, callers: Any, public_url: str, host: str, status: int
) -> None:
    with open_client(auth={"public_url": public_url, "allowed_hosts": ["API.ds.test"]}) as client:
        headers = callers(client).token("viewer") | {"Host": host}
        got = client.get("/v1/specs", headers=headers)
        assert client.get("/v1/health", headers={"Host": "10.0.0.1:8000"}).status_code == 200  # probes
    assert got.status_code == status
    if status == 400:
        assert got.json()["detail"] == "unexpected Host header"


@pytest.mark.parametrize(
    ("headers", "refusal"),
    [
        ({"Origin": "https://evil.example"}, "cross-origin request refused"),
        ({"Origin": "http://ds.test"}, "cross-origin request refused"),
        ({"Origin": "null"}, "cross-origin request refused"),
        ({"Sec-Fetch-Site": "cross-site"}, "cross-site request refused"),
        ({"Sec-Fetch-Site": "same-site"}, "cross-site request refused"),
        ({"Origin": "https://ds.test", "Sec-Fetch-Site": "same-origin"}, None),
        ({"Origin": "https://DS.test:443"}, None),
    ],
)
def test_unsafe_methods_must_come_from_this_origin(
    open_client: OpenClient, callers: Any, spec_yaml: str, headers: dict[str, str], refusal: str | None
) -> None:
    with open_client(auth={}) as client:
        sent = client.post(
            "/v1/specs", content=spec_yaml, headers=callers(client).token("spec_author") | headers
        )
        read = client.get("/v1/health", headers=headers)  # safe methods are not checked
    assert read.status_code == 200
    assert (sent.status_code, sent.json().get("detail")) == ((403, refusal) if refusal else (201, None))


def test_developer_mode_takes_loopback_origins_only(client: TestClient, spec_yaml: str) -> None:
    ok = client.post("/v1/specs", content=spec_yaml, headers={"Origin": "http://localhost:5173"})
    refused = client.post("/v1/specs", content=spec_yaml, headers={"Origin": "http://intranet.example"})
    assert (ok.status_code, refused.status_code, refused.json()["detail"]) == (
        201,
        403,
        "cross-origin request refused",
    )


def test_the_access_log_drops_the_query_of_sign_in_paths() -> None:
    def logged(path: str) -> Any:
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', None, None
        )
        record.args = ("10.0.0.1:5000", "GET", path, "1.1", 303)
        assert AuthQueryFilter().filter(record)
        return record.args[2]

    assert logged("/v1/auth/callback?code=c0de&state=s&iss=https://idp") == "/v1/auth/callback"
    assert logged("/v1/runs?cursor=R-1") == "/v1/runs?cursor=R-1"


def test_errors_are_json_and_point_at_the_problem(client: TestClient) -> None:
    missing = client.get("/v1/nothing-here")
    assert (missing.status_code, missing.json()) == (404, {"detail": "Not Found"})
    invalid = client.post("/v1/compare", json={"spec_id": "one", "extra": True})
    assert (invalid.status_code, invalid.json()["detail"]) == (422, "invalid request")
    assert {i["pointer"] for i in invalid.json()["issues"]} == {"/body/spec_id", "/body/key", "/body/extra"}


@pytest.mark.parametrize("error", [RuntimeError, KeyError])  # a KeyError is a bug, not a 404
def test_an_unexpected_error_is_a_500_without_its_details(
    open_client: OpenClient, stub: Any, spec_yaml: str, error: type[Exception]
) -> None:
    async def compare_one(*_: Any, **__: Any) -> None:
        raise error("password=hunter2")

    stub("deltasleuth.runner.runs", compare_one=compare_one)
    with open_client(raise_server_exceptions=False) as client:
        spec_id = client.post("/v1/specs", content=spec_yaml).json()["spec_id"]
        failed = client.post("/v1/compare", json={"spec_id": spec_id, "key": "K12345678"})
    assert (failed.status_code, failed.json()) == (500, {"detail": "internal error"})
    assert failed.headers["x-content-type-options"] == "nosniff"
    assert failed.headers["cache-control"] == "no-store"  # the guard's headers, though sent past it
    assert failed.headers["x-request-id"].startswith("Q-")


@pytest.mark.parametrize(
    ("path", "site", "status"),
    [
        ("/v1/runs", "same-site", 403),  # an <img> on another page of the site
        ("/v1/runs", "cross-site", 403),
        ("/v1/runs", "none", 200),  # typed into the address bar
        ("/v1/runs", "same-origin", 200),
        ("/v1/health", "cross-site", 200),  # load balancers and probes
    ],
)
def test_a_read_from_elsewhere_on_the_site_is_refused(
    client: TestClient, path: str, site: str, status: int
) -> None:
    answer = client.get(path, headers={"Sec-Fetch-Site": site})
    assert answer.status_code == status
    if status == 403:
        assert answer.json()["detail"] == "cross-site request refused"


def test_targets_show_capabilities_but_no_endpoint_or_credential(client: TestClient) -> None:
    body = client.get("/v1/targets").json()
    assert [t["target_id"] for t in body["items"]] == ["svc-a", "svc-b"]
    assert body["items"][0]["capabilities"]["pagination"] == "none"
    assert "127.0.0.1" not in str(body) and "token" not in str(body)


def test_targets_name_a_database_target_s_engine_and_never_its_host_or_user(open_client: OpenClient) -> None:
    declared = (
        TARGETS
        + """\
  ledger:
    engine: mysql
    host: ledger-db.bank.example
    port: 3307
    database: ledger
    user: ds_reader
    password_ref: file:ledger_password
  crm:
    dsn_ref: file:crm_dsn
"""
    )
    with open_client(targets=declared) as client:
        body = client.get("/v1/targets").json()
    assert {t["target_id"]: t["engine"] for t in body["items"]} == {
        "crm": "postgresql", "ledger": "mysql", "svc-a": None, "svc-b": None,
    }  # fmt: skip
    assert [
        text for text in ("ledger-db", "3307", "ds_reader", "password", "crm_dsn") if text in str(body)
    ] == []


def test_a_database_s_error_while_a_run_is_created_is_a_422_naming_its_code(
    open_client: OpenClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ENGINES §12 P14: a view left invalid answers its description with ORA-04063, which left the plan as
    # the engine's error and so the API as a 500, where every other refusal of a plan is a 422.
    refused = [TargetRefused("ORA-04063"), TargetUnreachable("ORA-03113")]
    monkeypatch.setitem(ENGINES, "postgresql", FakeEngine({"users": ["userid", "email"]}, describe=refused))
    declared = TARGETS + "  db-a:\n    dsn_ref: file:dsn\n  db-b:\n    dsn_ref: file:dsn\n"
    users = {"name": "users", "keyColumns": ["userid"]}
    spec = yaml.safe_dump(compiled(users, users).raw)
    with open_client(targets=declared) as client:
        spec_id = client.post("/v1/specs", content=spec).json()["spec_id"]
        created = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"})
        compared = client.post("/v1/compare", json={"spec_id": spec_id, "key": "1"})
    assert (created.status_code, created.json()["detail"]) == (
        422,
        "side a: table or view users cannot be described: ORA-04063",
    )
    assert (compared.status_code, compared.json()["detail"]) == (
        422,
        "side a: table or view users cannot be described: ORA-03113",
    )


def test_a_guard_refusal_is_422(open_client: OpenClient, spec_yaml: str) -> None:
    """A service named by a host that resolves to a private address without ``allow_private_network`` (an
    app on this machine as host.docker.internal; here a host every resolver reads as 127.0.0.1 without a
    lookup), and a template setting the header its target sends as a credential: the connector's refusals
    are 422s saying what to change, where a compare answered 500."""
    declared = """\
targets:
  svc-a:
    base_url: http://127.0.0.1:{port}/a
    allow_private_network: true
    allow_insecure_http: true
    auth: {{kind: header, header: X-Tenant, secret_ref: "file:svc-a.token"}}
  svc-b:
    base_url: http://0x7f.1:{port}/b
"""
    headed = spec_yaml.replace('{{ key }}"}', '{{ key }}", headers: {X-Tenant: other}}')
    assert headed != spec_yaml
    with open_client(targets=declared) as client:
        ids = [client.post("/v1/specs", content=text).json()["spec_id"] for text in (spec_yaml, headed)]
        refused = [client.post("/v1/compare", json={"spec_id": i, "key": "K12345678"}) for i in ids]
        runs = client.get("/v1/runs").json()["items"]
    assert [(answer.status_code, answer.json()) for answer in refused] == [
        (
            422,
            {
                "detail": "target host resolves to a loopback, private, link-local or reserved address; set"
                " allow_private_network on the target to permit it"
            },
        ),
        (422, {"detail": "request header 'X-Tenant' may not be set by a template"}),
    ]
    assert [run["status"] for run in runs] == ["failed", "failed"]  # the one-shot runs, as before


def test_llm_profiles_show_ids_without_endpoints_or_credentials(open_client: OpenClient) -> None:
    with open_client(ai=True) as client:
        assert client.get("/v1/llm/profiles").json() == {
            "default": "local",
            "items": [{"profile_id": "local", "dialect": "openai_chat", "model": "m1", "trust": "internal"}],
        }
    with open_client() as client:  # AI off
        assert client.get("/v1/llm/profiles").json() == {"default": None, "items": []}


def test_the_ui_is_served_with_a_single_page_fallback(open_client: OpenClient, tmp_path: Path) -> None:
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>ui</html>")
    (web / "assets" / "app.js").write_text("js")
    with open_client(web_dir=web) as client:
        assert [client.get(path).text for path in ("/", "/runs/R-1", "/assets/app.js")] == [
            "<html>ui</html>",
            "<html>ui</html>",  # a client-side route
            "js",
        ]
        assert client.get("/v1/nothing-here").json() == {"detail": "Not Found"}
    with open_client(web_dir=tmp_path / "not-built") as client:
        unbuilt, elsewhere = client.get("/"), client.get("/runs/R-1")
    assert (unbuilt.status_code, unbuilt.json()) == (
        404,
        {"detail": "the web UI is not built on this server; its API is under /v1"},
    )
    assert (elsewhere.status_code, elsewhere.json()) == (404, {"detail": "Not Found"})
    assert unbuilt.headers["x-content-type-options"] == "nosniff" and "cache-control" not in unbuilt.headers


def test_with_its_worker_the_app_processes_runs(open_client: OpenClient, spec_yaml: str) -> None:
    with open_client(worker=True) as client:
        spec_id = client.post("/v1/specs", content=spec_yaml).json()["spec_id"]
        run = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}).json()  # the spec's keys
        deadline = time.monotonic() + 30
        while (run := client.get(f"/v1/runs/{run['run_id']}").json())["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.1)
    assert (run["status"], run["progress"]) == (
        "completed",
        {"keys": {"done": 1}, "outcomes": {"MISMATCH": 1}},
    )
