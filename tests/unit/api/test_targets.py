"""Targets declared in the product: an endpoint declared through
the route is called, with every credential header, without a restart; no credential is in an answer, a
log line or an audit row; a credential never follows a target to another address; a target unfinished runs
read is neither removed nor moved, and one moved while a run's keys are listed refuses that run; and each
refusal's binding message."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth import targets as product
from deltasleuth.context import Context
from deltasleuth.core.spec import TargetsError
from deltasleuth.runner import runs
from deltasleuth.store import repo
from tests.conftest import TESTER, create_run

from .conftest import RAW_KEY, SPEC_YAML, audited, said

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
Seen = list[tuple[str, dict[str, str]]]  # each request the endpoint received: its path and headers
OWN_SPEC = SPEC_YAML.replace("svc-a", "own-a").replace("svc-b", "own-b")
LISTED = """source: {kind: paginated_endpoint, target: own-b, request: {method: GET, path: /list},
               keyPath: "$.id", pagination: {kind: cursor, itemsPath: "$.items", cursorPath: "$.next",
                                             cursorParam: cursor}}"""
COOKIE = {"kind": "header", "header": "Cookie", "more": [{"header": "X-CSRF-Token"}, {"header": "X-Tenant"}]}
CREDENTIALS = ("sid=5e55ion-c00kie", "f0rgery-t0ken", "tenant-acme-7", "b3arer-t0ken")  # of own-a, then own-b
TRIAL = {"public_url": "http://127.0.0.1:8000"}  # tokens mode on a plain-http loopback address


@pytest.fixture
def endpoint() -> Iterator[tuple[str, Seen]]:
    """A local service: ``GET /<any>/items/<key>`` answers a record about ``key`` (``/gone/...``: 404) and
    echoes the anti-forgery header it was sent, and ``GET /<any>/list`` a listing of RAW_KEY; its base URL
    and what it received."""
    seen: Seen = []

    class Service(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
            record = {"id": self.path.rpartition("/")[2], "price": 2.5, "n": 1}
            body = json.dumps({"items": [{"id": RAW_KEY}]} if record["id"] == "list" else record).encode()
            self.send_response(404 if self.path.startswith("/gone/") else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-CSRF-Token", self.headers.get("X-CSRF-Token", "none"))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


def _put(
    client: TestClient, target_id: str, base_url: str, credential: str | None = None, **body: Any
) -> Any:
    body = {"base_url": base_url, "allow_private_network": True, "allow_insecure_http": True} | body
    return client.put(
        f"/v1/targets/{target_id}", json=body | ({} if credential is None else {"secret": credential})
    )


def _declared(client: TestClient, url: str) -> int:
    """own-a (a session cookie and two further headers) and own-b (a bearer) at the endpoint, and the id of
    a stored spec that reads them."""
    more = dict(zip(("X-CSRF-Token", "X-Tenant"), CREDENTIALS[1:3], strict=True))
    a = _put(client, "own-a", f"{url}/a", CREDENTIALS[0], auth=COOKIE, more_secrets=more)
    b = _put(client, "own-b", f"{url}/b", CREDENTIALS[3], auth={"kind": "bearer"})
    assert (a.status_code, b.status_code) == (201, 201), (a.text, b.text)
    return client.post("/v1/specs", content=OWN_SPEC).json()["spec_id"]


def _compared(client: TestClient, spec_id: int) -> Any:
    return client.post("/v1/compare", json={"spec_id": spec_id, "key": RAW_KEY})


def _listed(client: TestClient) -> dict[str, dict[str, Any]]:
    return {item["target_id"]: item for item in client.get("/v1/targets").json()["items"]}


def test_declare_change_and_remove_without_a_restart(client: TestClient, endpoint: tuple[str, Seen]) -> None:
    url, seen = endpoint
    made = [
        client.put(f"/v1/targets/own-{s}", json={"base_url": f"{url}/{s}/"} | {"allow_private_network": True})
        for s in "ab"
    ]
    assert [answer.status_code for answer in made] == [201, 201]
    absent = {"absence_shapes": {}, "max_depth": None, "pagination": "none", "stable_ordering": False}
    assert made[0].json() == {
        "target_id": "own-a",
        "capabilities": absent
        | {"authoritative_absence": None, "truncation_indicators": None, "watermark": "none"},
        "engine": None,
        "declared_in": "product",
        "base_url": f"{url}/a",
        "allow_private_network": True,
        "allow_insecure_http": False,
        "auth": {"kind": "none", "header": None, "more": []},
        "has_secret": False,
        "rate_per_s": 20.0,
        "max_concurrency": 4,
        "absent_on_404": False,
    }
    listed = client.get("/v1/targets").json()
    assert listed["declarer"] == "admin" and listed["items"][0] == made[0].json()
    assert [(item["target_id"], item["declared_in"]) for item in listed["items"]] == [
        ("own-a", "product"),
        ("own-b", "product"),
        ("svc-a", "file"),
        ("svc-b", "file"),
    ]
    spec_id = client.post("/v1/specs", content=OWN_SPEC).json()["spec_id"]
    assert _compared(client, spec_id).json()["result"]["outcome"] == "MATCH"
    assert {path for path, _ in seen} == {f"/a/items/{RAW_KEY}", f"/b/items/{RAW_KEY}"}
    seen.clear()
    changed = _put(client, "own-a", f"{url}/moved", allow_insecure_http=False, rate_per_s=50)
    assert (changed.status_code, changed.json()["base_url"], changed.json()["rate_per_s"]) == (
        200,
        f"{url}/moved",
        50,
    )
    assert _compared(client, spec_id).status_code == 200
    assert {path for path, _ in seen} == {f"/moved/items/{RAW_KEY}", f"/b/items/{RAW_KEY}"}
    removed = client.delete("/v1/targets/own-a")
    assert (removed.status_code, removed.content) == (204, b"") and list(_listed(client)) == [
        "own-b",
        "svc-a",
        "svc-b",
    ]
    refused = _compared(client, spec_id)  # the stored spec names a target that is gone
    assert (refused.status_code, [issue["code"] for issue in refused.json()["issues"]]) == (
        422,
        ["unknown_target"],
    )
    assert [(actor, obj) for actor, obj, _ in audited(client, "target.set")] == [
        ("dev:loopback", "own-a"),
        ("dev:loopback", "own-b"),
        ("dev:loopback", "own-a"),
    ]
    assert audited(client, "target.set")[-1][2] == {
        "created": False,
        "base_url": f"{url}/moved",
        "allow_private_network": True,
        "allow_insecure_http": False,
        "auth": {"kind": "none", "header": None, "more": []},
        "secret": "none",
        "rate_per_s": 50,
        "max_concurrency": 4,
        "absent_on_404": False,
    }
    assert audited(client, "target.delete") == [("dev:loopback", "own-a", {})]


def test_a_target_sends_every_credential_header(client: TestClient, endpoint: tuple[str, Seen]) -> None:
    """Declared through the route and called at once: the session cookie, both further headers and the other
    side's bearer; then one value replaced, the others kept."""
    url, seen = endpoint
    spec_id = _declared(client, url)
    assert _compared(client, spec_id).json()["result"]["outcome"] == "MATCH"
    sent = {path.split("/")[1]: headers for path, headers in seen}
    cookie, forgery, tenant, bearer = CREDENTIALS
    assert [sent["a"].get(name) for name in ("cookie", "x-csrf-token", "x-tenant", "authorization")] == [
        cookie,
        forgery,
        tenant,
        None,
    ]
    assert (sent["b"]["authorization"], sent["b"].get("cookie")) == (f"Bearer {bearer}", None)
    seen.clear()
    again = _put(client, "own-a", f"{url}/a", auth=COOKIE, more_secrets={"X-CSRF-Token": "r0tated"})
    assert (again.status_code, again.json()["has_secret"]) == (200, True)
    assert _compared(client, spec_id).status_code == 200
    sent = {path.split("/")[1]: headers for path, headers in seen}
    assert [sent["a"].get(name) for name in ("cookie", "x-csrf-token", "x-tenant")] == [
        cookie,
        "r0tated",
        tenant,
    ]
    details = [detail for _, obj, detail in audited(client, "target.set") if obj == "own-a"]
    assert [(d["auth"], d["secret"]) for d in details] == [  # header names only
        ({"kind": "header", "header": "Cookie", "more": ["X-CSRF-Token", "X-Tenant"]}, "replaced")
    ] * 2


def _leaks(
    client: TestClient, answers: list[Any], caplog: pytest.LogCaptureFixture, *secrets: str
) -> list[str]:
    """Which of ``secrets``, the credential files' names and their folder are in an answer, a log line, an
    audit row or the audit route's answer."""
    folder = product.path(client.app.state.ctx.settings).parent
    names = [file.name for file in (folder / "secrets").iterdir()]
    assert names  # the credentials are stored
    answers = [*answers, client.get("/v1/targets"), client.get("/v1/audit")]
    said = [answer.text + str(answer.headers) for answer in answers]
    said += [record.getMessage() for record in caplog.records] + [caplog.text]
    said += [json.dumps(row) for row in audited(client, "target.set") + audited(client, "target.delete")]
    return [text for text in (*secrets, *names, str(folder)) if any(text in line for line in said)]


def test_the_credential_is_in_no_answer_log_or_audit_row(
    client: TestClient,
    endpoint: tuple[str, Seen],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    url, _ = endpoint
    bearer = {"auth": {"kind": "bearer"}}
    answers = [
        _put(client, "own", url, "t0p-s3cret", **bearer),
        _put(client, "own", url, **bearer),  # kept
        _put(client, "own", url, "n3w-s3cret", **bearer, rate_per_s=5),  # replaced
        _put(client, "own", f"{url}/elsewhere", **bearer),  # 422: it is needed again
        _put(client, "own", url, "wr0ng-s3cret", **bearer, rate_per_s="fast"),  # 422: the request
        _put(client, "own", "ftp://own.example", "0ther-s3cret", **bearer),  # 422: the target
        _put(client, "own", url, "padded-s3cret ", **bearer),  # 422: the credential
    ]

    def denied(path: Path, data: bytes) -> None:
        raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(path))

    with monkeypatch.context() as patched:
        patched.setattr(product, "atomic_write", denied)
        answers.append(_put(client, "own", url, "l0st-s3cret", **bearer))  # 500: nothing written
    assert [answer.status_code for answer in answers] == [201, 200, 200, 422, 422, 422, 422, 500]
    answers.append(client.delete("/v1/targets/missing"))
    secrets = ("t0p-s3cret", "n3w-s3cret", "wr0ng-s3cret", "0ther-s3cret", "padded-s3cret", "l0st-s3cret")
    assert _leaks(client, answers, caplog, *secrets) == []
    assert [detail["secret"] for _, _, detail in audited(client, "target.set")] == ["replaced", "kept"] + [
        "replaced"
    ] * 2


def test_no_credential_header_value_is_in_any_answer_log_or_audit_row(
    client: TestClient, endpoint: tuple[str, Seen], caplog: pytest.LogCaptureFixture
) -> None:
    """Several credential headers, sent and one of them echoed by the endpoint: none is in the answers of
    the changes, the compare and its key, a log line or an audit row."""
    caplog.set_level(logging.DEBUG)
    url, seen = endpoint
    spec_id = _declared(client, url)
    compared = _compared(client, spec_id)
    view = compared.json()
    assert view["result"]["outcome"] == "MATCH" and len(seen) == 2
    answers = [
        compared,
        client.get(f"/v1/runs/{view['run_id']}/keys/{view['key_masked']}"),
        _put(client, "own-a", f"{url}/a", auth=COOKIE),  # each kept
        _put(client, "own-a", f"{url}/a", auth=COOKIE | {"more": [{"header": "X-Other"}]}),  # 422: its value
        _put(client, "own-a", f"{url}/a", auth=COOKIE, more_secrets={"X-CSRF-Token": "bad\nvalue"}),  # 422
        client.delete("/v1/targets/own-b"),
    ]
    assert [answer.status_code for answer in answers] == [200, 200, 200, 422, 422, 204]
    assert _leaks(client, answers, caplog, *CREDENTIALS, "bad\nvalue", "bad\\nvalue") == []
    ctx = client.app.state.ctx

    async def recorded() -> dict[str, dict[str, str]]:
        async with ctx.pool.connection() as conn:
            sealer = await ctx.sealer(conn, view["run_id"])
            rows = await repo.list_observations(conn, view["run_id"])
        return {row["side"]: sealer.open_row(row)["headers"] for row in rows}

    headers = client.portal.call(recorded)  # what the evidence holds of the endpoint's echo
    assert "x-csrf-token" not in headers["a"] and headers["a"]["content-type"] == "application/json"
    assert headers["b"]["x-csrf-token"] == "none"  # no credential of own-b: an answer's header like another


def test_a_changed_origin_needs_the_credential_again(client: TestClient, endpoint: tuple[str, Seen]) -> None:
    url, _ = endpoint
    bearer = {"auth": {"kind": "bearer"}}
    assert _put(client, "own", f"{url}/api", "0ne", **bearer).status_code == 201
    needed = (422, product.REQUIRED)
    moved = [
        _put(client, "own", f"{url}/other", **bearer),  # the address
        _put(client, "own", f"{url.replace('127.0.0.1', 'localhost')}/api", **bearer),
        _put(client, "own", f"{url}/api", auth={"kind": "header", "header": "X-Api-Key"}),  # the kind
    ]
    assert [said(answer) for answer in moved] == [needed] * 3
    assert (
        _put(client, "own", f"{url}/api", "tw0", auth={"kind": "header", "header": "X-Api-Key"}).status_code
        == 200
    )
    renamed = _put(client, "own", f"{url}/api", auth={"kind": "header", "header": "X-Key"})  # the header
    assert said(renamed) == needed
    kept = _put(
        client,
        "own",
        f"{url.upper().replace('HTTP', 'http')}/api/",
        auth={"kind": "header", "header": "X-Api-Key"},
    )
    assert (kept.status_code, kept.json()["base_url"]) == (
        200,
        f"{url}/api",
    )  # the same address, as it is read
    assert _put(client, "own", f"{url}/other", "thr33", **bearer).status_code == 200
    assert [detail["secret"] for _, _, detail in audited(client, "target.set")] == [
        "replaced",
        "replaced",
        "kept",
        "replaced",
    ]
    assert [detail["base_url"] for _, _, detail in audited(client, "target.set")][-1] == f"{url}/other"
    folder = product.path(client.app.state.ctx.settings).parent / "secrets"
    assert [file.read_text() for file in folder.iterdir()] == ["thr33"]  # the old ones are unlinked


async def _queued(ctx: Context, spec_id: int) -> str:
    """An unfinished run of the spec, as the runner creates one."""
    async with ctx.pool.connection() as conn:
        return (await create_run(ctx, conn, spec_id, "batch", {RAW_KEY: "masked"}, {}))[0]


def test_a_target_in_use_is_neither_removed_nor_moved(client: TestClient, endpoint: tuple[str, Seen]) -> None:
    url, _ = endpoint
    spec_id = _declared(client, url)
    run_id = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}).json()["run_id"]  # queued
    rows = len(audited(client, "target.set"))
    in_use = (409, "target own-b is read by 1 unfinished run(s): stop them first")
    bearer = {"auth": {"kind": "bearer"}}
    moved = [
        client.delete("/v1/targets/own-b"),
        _put(client, "own-b", f"{url}/moved", CREDENTIALS[3], **bearer),  # the address
        _put(client, "own-b", f"{url}/b", **bearer, absent_on_404=True),  # what a 404 means
        _put(client, "own-b", f"{url}/b", **bearer, allow_insecure_http=False),  # a consent
        _put(client, "own-b", f"{url}/b"),  # the auth
        _put(client, "own-b", f"{url}/b", "as-a-header", auth={"kind": "header", "header": "X-Api-Key"}),
    ]
    assert [said(answer) for answer in moved[:3] + moved[4:]] == [in_use] * 5
    assert said(moved[3])[0] == 422  # the targets file's own rule comes first: a credential over http
    assert len(audited(client, "target.set")) == rows and audited(client, "target.delete") == []
    # its credential and its budget change under the run
    assert (
        _put(client, "own-b", f"{url}/b", "r0tated", **bearer, rate_per_s=2, max_concurrency=1).status_code
        == 200
    )
    # a key source's target is read too
    listing = SPEC_YAML.replace("source: {kind: list, keys: [K12345678]}", LISTED).replace(
        '"1.0.0"', '"1.1.0"'
    )
    listed = client.post("/v1/specs", content=listing)
    assert listed.status_code == 201, listed.text
    other = client.portal.call(_queued, client.app.state.ctx, listed.json()["spec_id"])
    assert said(client.delete("/v1/targets/own-b")) == (
        409,
        in_use[1].replace("1 unfinished", "2 unfinished"),
    )
    assert said(client.delete("/v1/targets/own-a")) == (409, in_use[1].replace("own-b", "own-a"))
    for unfinished in (run_id, other):
        assert client.post(f"/v1/runs/{unfinished}/cancel").status_code == 200
    assert [client.delete(f"/v1/targets/own-{side}").status_code for side in "ab"] == [204, 204]


def test_a_target_moved_while_a_runs_keys_are_listed_refuses_the_run(
    client: TestClient, endpoint: tuple[str, Seen], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 409 counts runs, and a run is written only once its keys were listed: until then the change is
    taken. The run is then refused at its creation, so none takes its keys from one address and compares
    another, or is written for a target that is gone. Its credential and its budget may change meanwhile."""
    url, seen = endpoint
    _declared(client, url)
    listing = OWN_SPEC.replace("source: {kind: list, keys: [K12345678]}", LISTED)
    spec_id = client.post("/v1/specs", content=listing).json()["spec_id"]
    ctx, resolve = client.app.state.ctx, runs.resolve
    own_b = {"allow_private_network": True, "allow_insecure_http": True, "auth": {"kind": "bearer"}}
    meanwhile: list[Any] = []

    async def listed(*args: Any, **kwargs: Any) -> Any:
        found = await resolve(*args, **kwargs)  # the listing is read; no run is written yet
        await meanwhile.pop()()
        return found

    def started() -> tuple[int, str]:
        answer = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"})
        return answer.status_code, answer.json().get("detail", "")

    async def moved() -> None:  # no 409: no run reads it yet
        body = own_b | {"base_url": f"{url}/moved", "secret": CREDENTIALS[3]}
        assert (await product.put(ctx, "own-b", body, TESTER))[0].base_url == f"{url}/moved"

    async def removed() -> None:
        await product.remove(ctx, "own-b", TESTER)

    async def rotated() -> None:  # what a change may touch under a run
        await product.put(ctx, "own-b", own_b | {"base_url": f"{url}/b", "secret": "r0tated"}, TESTER)

    monkeypatch.setattr(runs, "resolve", listed)
    changed = (422, "target own-b was changed or removed while the run was being created: start it again")
    meanwhile.append(moved)
    assert started() == changed
    assert [path for path, _ in seen] == ["/b/list"]  # its keys were listed; /moved is never compared
    meanwhile.append(removed)
    assert started() == changed and "own-b" not in ctx.targets
    assert client.get("/v1/runs").json()["items"] == []
    assert _put(client, "own-b", f"{url}/b", CREDENTIALS[3], auth={"kind": "bearer"}).status_code == 201
    meanwhile.append(rotated)
    assert started()[0] == 202 and len(client.get("/v1/runs").json()["items"]) == 1
    assert said(client.delete("/v1/targets/own-b"))[0] == 409  # written: the 409 sees it


def test_a_change_waits_for_a_run_being_written(
    client: TestClient, endpoint: tuple[str, Seen], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that is being written is in no count yet: a change of its target waits until it is, and is then
    refused by it, so the run's insert never passes between a change's count and its switch."""
    url, _ = endpoint
    spec_id = _declared(client, url)
    ctx, insert = client.app.state.ctx, repo.create_run
    moved = {"base_url": f"{url}/moved", "allow_private_network": True}

    async def written() -> tuple[bool, tuple[int, str]]:
        inserting, go = asyncio.Event(), asyncio.Event()

        async def held(*args: Any, **kwargs: Any) -> Any:
            inserting.set()
            await go.wait()
            return await insert(*args, **kwargs)

        monkeypatch.setattr(repo, "create_run", held)
        writing = asyncio.create_task(runs.create_run(ctx, spec_id, "batch", who=TESTER))
        await asyncio.wait_for(inserting.wait(), 10)
        moving = asyncio.create_task(product.put(ctx, "own-b", moved, TESTER))
        await asyncio.sleep(0.2)
        waits = not moving.done()
        go.set()
        await writing
        with pytest.raises(product.Refused) as in_use:
            await moving
        return waits, (in_use.value.status, str(in_use.value))

    assert client.portal.call(written) == (
        True,
        (409, "target own-b is read by 1 unfinished run(s): stop them first"),
    )
    assert _listed(client)["own-b"]["base_url"] == f"{url}/b"


def test_a_shadowed_entry_is_removed(
    open_client: OpenClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An id the targets file came to declare: the product's entry is ignored at the start, with a warning,
    cannot be changed, and is removed with its credential. The server's start sweeps what a killed change
    left beside that credential: a file the list does not name."""
    folder = tmp_path / "workspace" / "targets"
    (folder / "secrets").mkdir(parents=True)
    (folder / "secrets" / "svc-a.0123456789abcdef").write_text("sh4dowed")
    (folder / "secrets" / "svc-a.fedcba9876543210").write_text("n3ver-listed")
    auth = {"kind": "bearer", "secret_ref": "file:secrets/svc-a.0123456789abcdef"}
    (folder / "targets.json").write_text(
        json.dumps({"targets": {"svc-a": {"base_url": "https://old.example", "auth": auth}}})
    )
    in_file = (409, "target svc-a is declared in the targets file: change it there")
    with open_client() as client:
        assert [file.read_text() for file in (folder / "secrets").iterdir()] == ["sh4dowed"]
        assert (
            "warning: target svc-a of the product's list is ignored: the targets file declares it"
            in caplog.messages
        )
        assert [
            (item["target_id"], item["declared_in"]) for item in client.get("/v1/targets").json()["items"]
        ] == [
            ("svc-a", "file"),
            ("svc-b", "file"),
        ]
        assert said(client.put("/v1/targets/svc-a", json={"base_url": "https://new.example"})) == in_file
        assert client.delete("/v1/targets/svc-a").status_code == 204
        assert not (folder / "targets.json").exists() and list((folder / "secrets").iterdir()) == []
        assert list(_listed(client)) == ["svc-a", "svc-b"]  # the targets file's stays, with its gate
        assert said(client.delete("/v1/targets/svc-a")) == in_file  # no entry of the product's is left
        assert audited(client, "target.delete") == [("dev:loopback", "svc-a", {})]


def test_a_link_in_the_products_secrets_stops_the_start_and_every_change(
    open_client: OpenClient, tmp_path: Path, endpoint: tuple[str, Seen]
) -> None:
    """Whoever can change the list in the workspace can also put a symbolic link there. A credential of
    exactly the product's form that is a link to a file outside (here the settings' DSN file) would be sent
    to the entry's address as its bearer; a linked secrets folder would be swept, every file of the
    directory it points to removed at the start. Neither happens: the server does not start, a server
    already running answers every change 422 without a path, and nothing is sent or removed."""
    url, seen = endpoint
    folder, dsn = tmp_path / "workspace" / "targets", tmp_path / "dsn"
    name = "own-b.0123456789abcdef"
    address = {"allow_private_network": True, "allow_insecure_http": True}
    own_b = {"base_url": f"{url}/b", "auth": {"kind": "bearer", "secret_ref": f"file:secrets/{name}"}}
    entries = {"own-a": {"base_url": f"{url}/a"} | address, "own-b": own_b | address}
    elsewhere = tmp_path / "elsewhere"  # another directory the service writes: its keys, its evidence
    elsewhere.mkdir()
    for kept in ("kek", "tokeniser", "pack.zip"):
        (elsewhere / kept).write_text("the installation's own\n")
    with open_client() as client:  # a start without a list
        assert _put(client, "own-a", f"{url}/a").status_code == 201
        spec_id = client.post("/v1/specs", content=OWN_SPEC.replace("own-b", "own-a")).json()["spec_id"]
        (folder / "secrets" / name).symlink_to(dsn)
        (folder / "targets.json").write_text(json.dumps({"targets": entries}))
        refused = [
            _put(client, "own-b", f"{url}/b", "n3w", auth={"kind": "bearer"}),
            _put(client, "own-z", url),
            client.delete("/v1/targets/own-a"),
        ]
        assert [said(answer) for answer in refused] == [(422, "invalid target")] * 3
        issues = [[(i["pointer"], i["code"]) for i in answer.json()["issues"]] for answer in refused]
        assert issues == [[("/targets/own-b/auth", "link")]] * 3
        assert not any(str(tmp_path) in answer.text or name in answer.text for answer in refused)
        assert _compared(client, spec_id).status_code == 200  # what the server read at its start holds
    for at in ("/targets/own-b/auth", "/targets"):
        if at == "/targets":  # the secrets folder itself: no credential and no list is needed
            (folder / "secrets" / name).unlink()
            (folder / "secrets").rmdir()
            (folder / "targets.json").unlink()
            (folder / "secrets").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(TargetsError) as stopped, open_client():
            pass
        (issue,) = stopped.value.issues
        assert (issue.pointer, issue.code) == (at, "link")
        assert issue.message.endswith(f" (the product's list, {folder / 'targets.json'})")
    assert dsn.read_text().startswith("postgresql://") and len(list(elsewhere.iterdir())) == 3
    sent = [headers.get("authorization") for _, headers in seen]
    assert sent == [None, None]  # the one compare's two reads, of own-a: never the DSN as a bearer


def test_a_product_target_takes_no_capabilities(client: TestClient, endpoint: tuple[str, Seen]) -> None:
    """But the one checkbox: a 404 means no such record, stored in the targets file's own form."""
    url, _ = endpoint
    capabilities = {"authoritative_absence": {"status": [404]}}
    refused = _put(client, "own-a", f"{url}/gone", capabilities=capabilities)
    assert said(refused) == (422, "invalid request")
    assert [(issue["pointer"], issue["code"]) for issue in refused.json()["issues"]] == [
        ("/body/capabilities", "extra_forbidden")
    ]
    paths = {"a": "gone", "b": "b"}  # side a's record is not there
    assert [_put(client, f"own-{s}", f"{url}/{paths[s]}").status_code for s in "ab"] == [201, 201]
    spec_id = client.post("/v1/specs", content=OWN_SPEC).json()["spec_id"]
    assert _compared(client, spec_id).json()["result"]["outcome"] == "INCONCLUSIVE"  # a 404 proves nothing
    with_not_found = OWN_SPEC.replace(
        "    truncation:", "    notFound: {status: [404]}\n    truncation:"
    ).replace('"1.0.0"', '"1.1.0"')
    undeclared = client.post("/v1/specs/validate", content=with_not_found).json()["issues"]
    assert {issue["code"] for issue in undeclared if issue["severity"] == "error"} == {"not_found_undeclared"}
    checked = [_put(client, f"own-{s}", f"{url}/{paths[s]}", absent_on_404=True) for s in "ab"]
    assert [(answer.status_code, answer.json()["absent_on_404"]) for answer in checked] == [(200, True)] * 2
    assert checked[0].json()["capabilities"]["authoritative_absence"] == {"status": [404], "shapes": []}
    stored = json.loads(product.path(client.app.state.ctx.settings).read_text())["targets"]["own-a"]
    assert stored == {
        "base_url": f"{url}/gone",
        "allow_private_network": True,
        "allow_insecure_http": True,
    } | {"capabilities": capabilities}
    spec_id = client.post("/v1/specs", content=with_not_found).json()["spec_id"]
    assert _compared(client, spec_id).json()["result"]["outcome"] == "NOT_FOUND_A"
    assert audited(client, "target.set")[-1][2]["absent_on_404"] is True


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("secret_ref", "file:/etc/passwd"),
        ("dsn_ref", "file:/run/secrets/dsn"),
        ("engine", "mysql"),
        ("host", "db.internal"),
        ("password_ref", "file:/run/secrets/p"),
        ("root", "/var/lib/exports"),
        ("target_id", "other"),
        ("auth/secret_ref", "file:/etc/passwd"),
        ("auth/more/0/secret_ref", "file:/etc/passwd"),
    ],
)
def test_only_service_members(client: TestClient, member: str, value: str) -> None:
    body: Any = {
        "base_url": "https://own.example",
        "auth": {"kind": "header", "header": "X-A", "more": [{"header": "X-B"}]},
    }
    at = body
    *path, name = member.split("/")
    for part in path:
        at = at[int(part) if part.isdigit() else part]
    at[name] = value
    refused = client.put("/v1/targets/own", json=body | {"secret": "s", "more_secrets": {"X-B": "t"}})
    assert said(refused) == (422, "invalid request")
    assert [(issue["pointer"], issue["code"]) for issue in refused.json()["issues"]] == [
        (f"/body/{member}", "extra_forbidden")
    ]
    assert list(_listed(client)) == ["svc-a", "svc-b"] and audited(client, "target.set") == []


def test_messages(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def put(
        target_id: str = "own",
        base_url: str = "https://own.example",
        credential: str | None = None,
        **body: Any,
    ) -> Any:
        body = {"base_url": base_url} | body | ({} if credential is None else {"secret": credential})
        return client.put(f"/v1/targets/{target_id}", json=body)

    def issues(answer: Any) -> tuple[int, str, list[tuple[str, str]]]:
        return (*said(answer), [(issue["pointer"], issue["code"]) for issue in answer.json()["issues"]])

    bearer = {"auth": {"kind": "bearer"}}
    in_file = (409, "target svc-a is declared in the targets file: change it there")
    assert said(put("svc-a")) == said(client.delete("/v1/targets/svc-a")) == in_file
    assert said(client.delete("/v1/targets/own")) == (404, "target own not found")
    required = "secret is required: none is stored for this auth, or the base URL or the auth changed"
    assert said(put(**bearer)) == (422, required)
    unstorable = (
        "secret is refused: it is empty, over 65536 bytes, or holds a BOM, surrounding whitespace, a line"
        " break or a control character"
    )
    for refused in ("", " padded", "two\nlines", "\ufeffbom", "bell\x07", "del\x7f", "x" * 65537):
        assert said(put(**bearer, credential=refused)) == (422, unstorable), repr(refused[:9])
    none = (422, "auth none takes no secret")
    assert said(put(credential="s")) == said(put(auth={"kind": "none"}, more_secrets={"X-A": "s"})) == none
    invalid = "invalid target"  # the targets file's own issues
    assert issues(put(auth={"kind": "header", "header": "Host"}, credential="s")) == (
        422,
        invalid,
        [("/targets/own/auth/header", "schema.not")],
    )
    assert issues(put(auth={"kind": "basic"}, credential="s")) == (
        422,
        invalid,
        [("/targets/own/auth/kind", "schema.enum")],
    )
    assert issues(put(base_url="http://own.example", **bearer, credential="s")) == (
        422,
        invalid,
        [("/targets/own/base_url", "insecure_http")],
    )
    assert issues(put(base_url="http://10.1.2.3:8080")) == (
        422,
        invalid,
        [("/targets/own/base_url", "private_network")],
    )
    assert issues(put("Own_1")) == (422, invalid, [("/targets/Own_1", "schema.propertyNames")])
    # an id or a header's name that ends in a line break: no second svc-a beside the targets file's
    assert issues(put("svc-a%0A")) == (422, invalid, [("/targets/svc-a\n", "schema.propertyNames")])
    assert issues(put("own%0A")) == (422, invalid, [("/targets/own\n", "schema.propertyNames")])
    assert issues(put(auth={"kind": "header", "header": "X-Key\n"}, credential="s")) == (
        422,
        invalid,
        [("/targets/own/auth/header", "schema.pattern")],
    )
    further = {"kind": "header", "header": "X-Key", "more": [{"header": "X-More\n"}]}
    assert issues(put(auth=further, credential="s", more_secrets={"X-More\n": "t"})) == (
        422,
        invalid,
        [("/targets/own/auth/more/0/header", "schema.pattern")],
    )
    assert issues(put(rate_per_s=0)) == (
        422,
        invalid,
        [("/targets/own/rate_per_s", "schema.exclusiveMinimum")],
    )
    (tmp_path / ".dockerenv").touch()  # in a container, localhost is not the machine
    with monkeypatch.context() as contained:
        contained.setattr(product, "_CONTAINER", (str(tmp_path / ".dockerenv"),))
        here = put(base_url="http://localhost:8080", allow_private_network=True)
    assert here.json() == {
        "detail": invalid,
        "issues": [
            {
                "pointer": "/targets/own/base_url",
                "message": "localhost is DeltaSleuth's own container here, not this machine: write the host"
                " as host.docker.internal, keep the port, and set allow_private_network",
                "code": "container_localhost",
                "severity": "error",
            }
        ],
    }
    assert audited(client, "target.set") == [] and list(_listed(client)) == ["svc-a", "svc-b"]

    def denied(path: Path, data: bytes) -> None:
        raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(path))

    monkeypatch.setattr(product, "atomic_write", denied)  # the row stays
    assert said(put(**bearer, credential="s")) == (
        500,
        f"the target could not be written: {os.strerror(errno.EACCES)}",
    )
    assert len(audited(client, "target.set")) == 1 and list(_listed(client)) == ["svc-a", "svc-b"]


def test_a_credential_holding_this_installations_own_cookie_is_refused(
    open_client: OpenClient, callers: Any, tmp_path: Path, endpoint: tuple[str, Seen]
) -> None:
    """The applications a trial compares run on its own host, and a browser keeps cookies by host, not by
    port: a request to one of them, copied from the browser, carries the trial's own session cookie. Stored
    as the target's credential it would lie in clear in the workspace, sign in whoever reads it, and go to
    the service with every read. It is refused by its name, alone or beside the service's cookie, as any
    credential header's value; so is the sign-in cookie; nothing is written, audited or sent."""
    url, seen = endpoint
    refusal = (
        "secret is refused: it holds DeltaSleuth's own cookie {}, which signs in here and which a browser"
        " sends to every port of this host: leave that cookie out"
    )
    with open_client(auth=TRIAL) as client:
        author = callers(client).token("spec_author")
        token = author["Authorization"].removeprefix("Bearer ")
        assert client.post("/v1/auth/token-login", json={"token": token}).status_code == 200
        ((name, secret),) = client.cookies.items()
        client.cookies.clear()
        assert name == "ds_session_8000"
        own = f"{name}={secret}"
        assert client.get("/v1/auth/me", headers={"Cookie": own}).json()["roles"]  # it signs in

        def put(credential: str, **body: Any) -> Any:
            body = {"auth": {"kind": "header", "header": "Cookie"}, "secret": credential} | body
            address = {"base_url": url, "allow_private_network": True, "allow_insecure_http": True}
            return client.put("/v1/targets/own-app", json=address | body, headers=author)

        further = {"kind": "header", "header": "X-Auth-Token", "more": [{"header": "Cookie"}]}
        refused = [
            put(own),
            put(f"sid=abc; {own}; theme=dark"),
            put(f"sid=abc;{name} ={secret}"),
            put(own, auth={"kind": "bearer"}),
            put("t0k", auth=further, more_secrets={"Cookie": f"sid=abc; {own}"}),
        ]
        assert [said(answer) for answer in refused] == [(422, refusal.format(name))] * 5
        assert said(put("sid=abc; ds_login_8000=in-progress")) == (422, refusal.format("ds_login_8000"))
        assert not any(secret in answer.text for answer in refused)
        listed = client.get("/v1/targets", headers=author).json()["items"]
        assert audited(client, "target.set") == [] and len(listed) == 2
        assert not (tmp_path / "workspace" / "targets").exists()
        # the service's own cookie, and another installation's (another port: not this one's to judge)
        assert put("sid=abc; theme=dark; ds_session_18000=another").status_code == 201
        spec = OWN_SPEC.replace("own-a", "own-app").replace("own-b", "own-app")
        spec_id = client.post("/v1/specs", content=spec, headers=author).json()["spec_id"]
        compared = client.post("/v1/compare", json={"spec_id": spec_id, "key": RAW_KEY}, headers=author)
        assert compared.status_code == 200
    assert {headers.get("cookie") for _, headers in seen} == {"sid=abc; theme=dark; ds_session_18000=another"}


def test_file_targets_show_no_url(open_client: OpenClient, callers: Any, endpoint: tuple[str, Seen]) -> None:
    """Who declares reads what a target of the product's list was declared with; a targets-file item stays
    as it was, and another caller reads no more of either."""
    url, _ = endpoint
    public = {"target_id", "capabilities", "engine", "declared_in"}
    with open_client(auth=TRIAL) as client:
        author, viewer = (callers(client).token(role, role) for role in ("spec_author", "viewer"))
        made = client.put(
            "/v1/targets/own",
            json={
                "base_url": url,
                "allow_private_network": True,
                "allow_insecure_http": True,
                "auth": {"kind": "bearer"},
                "secret": "t0p-s3cret",
            },
            headers=author,
        )
        assert made.status_code == 201
        declarer, other = (client.get("/v1/targets", headers=headers).json() for headers in (author, viewer))
    assert declarer["declarer"] == other["declarer"] == "spec_author"
    assert [set(item) for item in other["items"]] == [public] * 3
    own, *filed = declarer["items"]
    assert [set(item) for item in filed] == [public] * 2 and "127.0.0.1" not in str(filed) + str(other)
    assert set(own) - public == {
        "base_url",
        "allow_private_network",
        "allow_insecure_http",
        "auth",
        "has_secret",
        "rate_per_s",
        "max_concurrency",
        "absent_on_404",
    }
    assert (own["base_url"], own["auth"], own["has_secret"]) == (
        url,
        {"kind": "bearer", "header": None, "more": []},
        True,
    )


def test_two_changes_do_not_interleave(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each change reads the list, then writes it: one at a time, or the slower would drop the other's."""
    order: list[str] = []
    switch = product._switch

    def slow(source: Path, entries: dict[str, Any], *rest: Any) -> None:
        order.append("in")
        time.sleep(0.3)
        switch(source, entries, *rest)
        order.append("out")

    monkeypatch.setattr(product, "_switch", slow)
    with ThreadPoolExecutor(2) as pool:
        made = list(
            pool.map(
                lambda name: client.put(f"/v1/targets/{name}", json={"base_url": "https://own.example"}),
                ("one", "two"),
            )
        )
    assert [answer.status_code for answer in made] == [201, 201] and order == ["in", "out"] * 2
    assert list(_listed(client)) == ["one", "svc-a", "svc-b", "two"]
    assert sorted(json.loads(product.path(client.app.state.ctx.settings).read_text())["targets"]) == [
        "one",
        "two",
    ]
