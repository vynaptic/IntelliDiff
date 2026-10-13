"""API fixtures: a TestClient over a fresh database and two local sides, stub modules (for what a
test must not do for real: call an LLM), a run stored through the engine as a worker stores one, the
callers of each auth mode (tokens, and sessions inserted as a sign-in would insert them), a spec
version's four-eyes approval (``approved``), a fake identity provider to sign in through, and an oidc
client with evidence packs (``packs_client``) over a signed-off census cycle (``signed``). An oidc client
of a vendor build (``vendor_keys``) runs under an Enterprise licence; the open
build's, the tests' own, needs none."""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi.testclient import TestClient

from deltasleuth import approvals
from deltasleuth.api import create_app
from deltasleuth.auth import sessions, tokens
from deltasleuth.auth.principal import expand
from deltasleuth.config import Settings, load_settings
from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.context import Context, init_workspace
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.classify.rootcause import root_causes
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import KeyResult, Observation, Side
from deltasleuth.runner.compare import save_pass
from deltasleuth.security.tokenise import mask_key
from deltasleuth.store import repo
from tests.conftest import TESTER, create_run, encryption, licensed_once
from tests.unit.auth.fake_idp import FakeIdp
from tests.unit.reports.packs import Signed, signed_cycle
from tests.unit.store.four_eyes import approve, person

RAW_KEY, OTHER_KEY = "K12345678", "K87654321"  # 1, 8 and 9 never occur in a base32 token
SPEC_YAML = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: api-demo, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key: {name: item_id, type: string, pattern: "^K[0-9]{8}$", class: identifier,
      source: {kind: list, keys: [K12345678]}}
sides:
  a: &side
    role: old
    target: svc-a
    request: {method: GET, path: "/items/{{ key }}"}
    truncation: {countPath: $.n, cap: 9}
  b: {<<: *side, role: new, target: svc-b}
compare: {}
recheck: {delay: 1s, times: 1, oneShot: {delay: 0s, times: 1}}
security: {clear: [$.price]}
"""
TARGETS = """\
targets:
  svc-a:
    base_url: http://127.0.0.1:{port}/a
    allow_private_network: true
    allow_insecure_http: true
    auth:
      kind: bearer
      secret_ref: file:svc-a.token
  svc-b:
    base_url: http://127.0.0.1:{port}/b
    allow_private_network: true
"""
PUBLIC_URL = "https://ds.test"
OIDC = {  # settings only: the server's start finds no IdP at this closed port, and nothing signs in
    "issuer": "http://127.0.0.1:1",
    "client_id": "deltasleuth",
    "client_secret_ref": "file:oidc_secret",
    "roles": {"viewer": ["G-view"], "operator": ["G-op"], "admin": ["G-admin"]},
}
LLM_PROFILES = """\
hosts: {llm.example.com: internal}
profiles:
  local: {dialect: openai_chat, base_url: "https://llm.example.com/v1", model: m1, auth_ref: "file:llm.key"}
"""
BODIES = {  # side a's price has more digits than a float holds
    Side.A: b'{"id": "K12345678", "price": 1.00000000000000000001, "n": 1}',
    Side.B: b'{"id": "K12345678", "price": 2.5, "n": 1}',
}


@dataclass(frozen=True)
class Seeded:
    run_id: str
    spec_id: int
    key: str  # RAW_KEY masked: compared, a MISMATCH
    other_key: str  # OTHER_KEY masked: still pending
    raw_key: str = RAW_KEY


class _Sides(BaseHTTPRequestHandler):
    """``GET /<side>/items/<key>``: that side's body in BODIES, about ``key``."""

    def do_GET(self) -> None:
        side, _, key = self.path.removeprefix("/").partition("/items/")
        body = BODIES[Side(side)].replace(RAW_KEY.encode(), key.encode())
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture(scope="session")
def sides_port() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Sides)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def spec_yaml() -> str:
    return SPEC_YAML


@pytest.fixture
def open_client(
    tmp_path: Path, pg_dsn: str, sides_port: int
) -> Callable[..., AbstractContextManager[TestClient]]:
    """``with open_client(auth=..., ai=..., targets=..., packs=..., notify=..., readiness=..., files=...,
    mcp=..., client=..., tier=..., **create_app_options) as client``.

    Without ``auth``, developer mode: a loopback client sending ``X-CSRF-Token: dev``. With ``auth``
    (the settings block; ``public_url`` defaults to PUBLIC_URL), a client of that URL at 10.1.2.3.
    ``packs``: the evidence pack settings; ``notify``: the notification settings; ``readiness``: the
    readiness settings; ``files``: the file reader's; ``mcp``: the MCP server's. In oidc mode of a vendor
    build (``vendor_keys``) a licence of ``tier`` (None: none, an evaluation) is installed once the server has
    started, unless the store names one, so that its runs, sign-offs and packs go ahead, as they do in the
    open build without one.
    """

    def settings(
        auth: dict[str, Any] | None,
        ai: bool,
        targets: str,
        packs: dict[str, Any] | None,
        notify: dict[str, Any] | None,
        readiness: dict[str, Any] | None,
        files: dict[str, Any] | None,
        mcp: dict[str, Any] | None,
    ) -> Settings:
        (tmp_path / "dsn").write_text(pg_dsn + "\n")
        (tmp_path / "targets.yaml").write_text(targets.format(port=sides_port))
        (tmp_path / "svc-a.token").write_text("t0ken\n")
        (tmp_path / "oidc_secret").write_text("client-s3cret\n")
        doc: dict[str, Any] = {
            "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
            "evidence_dir": "evidence",
            "workspace_dir": "workspace",
            "targets_file": "targets.yaml",
            "worker": {"poll_seconds": 0.1},
        }
        if auth is not None:
            doc["auth"] = {"public_url": PUBLIC_URL} | auth
            doc["encryption"] = encryption(tmp_path / "secrets")
        if packs is not None:
            doc["packs"] = packs
        if notify is not None:
            doc["notify"] = notify
        if readiness is not None:
            doc["readiness"] = readiness
        if files is not None:
            doc["files"] = files
        if mcp is not None:
            doc["mcp"] = mcp
        if ai:
            (tmp_path / "llm.yaml").write_text(LLM_PROFILES)
            doc |= {"llm_profiles_file": "llm.yaml", "llm_profile": "local"}
        (tmp_path / "deltasleuth.yaml").write_text(yaml.safe_dump(doc))
        loaded = load_settings(tmp_path / "deltasleuth.yaml")
        init_workspace(loaded)
        return loaded

    @contextmanager
    def open_(
        *,
        auth: dict[str, Any] | None = None,
        ai: bool = False,
        targets: str = TARGETS,
        packs: dict[str, Any] | None = None,
        notify: dict[str, Any] | None = None,
        readiness: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        mcp: dict[str, Any] | None = None,
        raise_server_exceptions: bool = True,
        client: tuple[str, int] | None = None,
        tier: str | None = "enterprise",
        **options: Any,
    ) -> Iterator[TestClient]:
        app = create_app(settings(auth, ai, targets, packs, notify, readiness, files, mcp), **options)
        if auth is None:
            where: dict[str, Any] = {"base_url": "http://127.0.0.1", "client": client or ("127.0.0.1", 50000)}
            where["headers"] = {"X-CSRF-Token": "dev"}
        else:
            where = {"base_url": auth.get("public_url", PUBLIC_URL), "client": client or ("10.1.2.3", 50000)}
        with TestClient(app, raise_server_exceptions=raise_server_exceptions, **where) as test_client:
            if auth is not None and "oidc" in auth:
                test_client.portal.call(licensed_once, test_client.app.state.ctx, tier)
            yield test_client

    return open_


@pytest.fixture
def idp() -> Iterator[FakeIdp]:
    """A fake IdP; ``open_client(auth={"oidc": idp.settings()})`` signs in through it."""
    with FakeIdp() as server:
        yield server


@pytest.fixture
def client(open_client: Callable[..., AbstractContextManager[TestClient]]) -> Iterator[TestClient]:
    with open_client() as client:
        yield client


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Install module ``name`` with the given functions (the real one may not be built yet)."""

    def install(name: str, **functions: Any) -> None:
        module = types.ModuleType(name)
        vars(module).update(functions)
        monkeypatch.setitem(sys.modules, name, module)

    return install


def _spec(ctx: Context) -> Any:
    return compile_spec(parse_yaml_text(SPEC_YAML), targets=ctx.targets)


def _compare(ctx: Context, key: str) -> tuple[list[Observation], KeyResult]:
    """Store BODIES as the key's evidence and compare them, as a worker does."""
    spec, observations, states = _spec(ctx), [], {}
    for side, body in BODIES.items():
        obs = ctx.evidence.put_blob(Observation(key, side, 200, body, utc_now_iso()))
        cfg = spec.sides[side]
        states[side] = observe(obs, cfg, spec, ctx.targets.get(cfg.target).capabilities)
        observations.append(obs)
    result = compare_pair(
        key, states[Side.A], states[Side.B], spec, today=date.today(), tokeniser=ctx.tokeniser
    )
    result.obs_a_id, result.obs_b_id = (obs.obs_id for obs in observations)
    return observations, result


async def _seed_run(ctx: Context) -> Seeded:
    spec = _spec(ctx)
    observations, result = _compare(ctx, RAW_KEY)
    masked = {key: mask_key(key, spec, ctx.tokeniser) for key in (RAW_KEY, OTHER_KEY)}
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, spec, created_by=TESTER.actor)
        params = {"capabilities": ctx.declared(spec)}
        run_id, sealer = await create_run(ctx, conn, spec_id, "batch", masked, params)
        await repo.set_run_status(conn, run_id, "running")
        await save_pass(
            ctx, conn, spec, run_id, 1, 1, result, {obs.side: obs for obs in observations}, sealer
        )
        await repo.lease_due(conn, "seed", 100, 60)  # every due key, of any run
        assert await repo.complete_key(conn, run_id, 1, masked[RAW_KEY], "seed")
        await repo.release_leases(conn, "seed")
        causes = root_causes([result], spec=spec, tokeniser=ctx.tokeniser)
        await repo.replace_root_causes(conn, run_id, 1, causes, {})
    return Seeded(run_id, spec_id, masked[RAW_KEY], masked[OTHER_KEY])


async def append_chat(ctx: Context, session_id: str, role: str, content: str) -> None:
    """A chat message stored as the agent stores it: sealed under its run's data key."""
    async with ctx.pool.connection() as conn:
        run_id = (await repo.get_chat_session(conn, session_id))["run_id"]  # type: ignore[index]
        payload = {"content": content, "tool_calls": []}
        await repo.append_chat_message(conn, session_id, role, payload, await ctx.sealer(conn, run_id))


async def _approved(ctx: Context, spec_id: int) -> str:
    async with ctx.pool.connection() as conn:
        needed = approvals.needs(await ctx.spec(conn, spec_id))
        requester = await person(conn, "rita", "spec_author")
        row = await repo.create_approval(conn, "spec", spec_id, requester.actor, scopes=needed)
        assert row is not None
        authors = await repo.approval_authors(conn, row["approval_id"])
        deciders = [await person(conn, name) for name in ("amir", "bea")]
        await approve(conn, row["approval_id"], *deciders[: 2 if authors["unattributed"] else 1])
        return row["approval_id"]


def approved(client: TestClient, spec_id: int) -> str:
    """Approve the spec version's needs, as people other than its uploaders would:
    one approver, or two when an upload was not a person's; the approval's id. Under SSO a run of a version
    that needs one starts only then."""
    return client.portal.call(_approved, client.app.state.ctx, spec_id)


@pytest.fixture
def seed() -> Callable[[TestClient], Seeded]:
    """``seed(client)``: a running batch run of two keys, one done (MISMATCH), one pending."""
    return lambda client: client.portal.call(_seed_run, client.app.state.ctx)


@pytest.fixture
def seeded(client: TestClient, seed: Callable[[TestClient], Seeded]) -> Seeded:
    return seed(client)


async def _audit_actions(ctx: Context) -> list[str]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT action FROM ds.audit_log WHERE actor NOT LIKE 'system:%' ORDER BY id"
        )
        return [action for (action,) in await cur.fetchall()]


@pytest.fixture
def audit() -> Callable[[TestClient], list[str]]:
    """``audit(client)``: the audit log's actions, oldest first, without the system's own."""
    return lambda client: client.portal.call(_audit_actions, client.app.state.ctx)


async def _audit_rows(ctx: Context) -> list[repo.Row]:
    async with ctx.pool.connection() as conn:
        return list(reversed(await repo.pages(repo.list_audit, conn)))


@pytest.fixture
def audit_rows() -> Callable[[TestClient], list[repo.Row]]:
    """``audit_rows(client)``: every audit row, oldest first."""
    return lambda client: client.portal.call(_audit_rows, client.app.state.ctx)


async def _token(ctx: Context, role: str, name: str) -> str:
    async with ctx.pool.connection() as conn:
        return (await tokens.create(conn, TESTER, name=name, owner="tests", role=role, max_days=90))[1]


async def _session(
    ctx: Context, roles: list[str], username: str, token: str | None, roles_sha256: str | None, **fields: Any
) -> str:
    cookie = sessions.new_secret()
    async with ctx.pool.connection() as conn:
        if token is not None:  # a token sign-in
            row = await repo.token_by_id(conn, str(tokens.token_id(token)))
            who = {"actor": f"token:{row['token_id']}", "name": row["name"], "token_id": row["token_id"]}
        else:
            user = await repo.upsert_user(
                conn, "https://idp.test", username, username, None, roles, signed_in=True
            )
            who = {"actor": f"user:{user['user_id']}", "name": username, "user_id": user["user_id"]}
            who["roles_sha256"] = roles_sha256
        await repo.create_session(
            conn, secret_hash=sessions.secret_hash(cookie), roles=roles, max_hours=8, **who | fields
        )
    return cookie


@dataclass
class Callers:
    """Request headers that make a client's calls as a token or a session holder."""

    client: TestClient

    def token(self, role: str, name: str = "ci-bot") -> dict[str, str]:
        token = self.client.portal.call(_token, self.client.app.state.ctx, role, name)
        return {"Authorization": f"Bearer {token}"}

    def session(
        self, *roles: str, username: str = "alice", token: str | None = None, **fields: Any
    ) -> dict[str, str]:
        """A user's session (oidc mode) holding ``roles`` (the IdP's, expanded by the caller), or with
        ``token`` a token sign-in's; ``fields`` go to the row (auth_time, ...)."""
        mapping = self.client.app.state.roles_sha256
        ctx = self.client.app.state.ctx
        cookie = self.client.portal.call(
            partial(_session, ctx, list(roles), username, token, mapping, **fields)
        )
        return {"Cookie": f"{sessions.COOKIE}={cookie}", "X-CSRF-Token": sessions.csrf_of(cookie)}


@pytest.fixture
def callers() -> Callable[[TestClient], Callers]:
    return Callers


# -- evidence packs -------------------------------------------------------------


@pytest.fixture
def pack_key(tmp_path: Path) -> Path:
    """A fresh EC P-256 signing key, PEM PKCS#8 unencrypted, outside the evidence and workspace dirs."""
    key = tmp_path / "secrets" / "pack-signing-key"
    key.parent.mkdir(exist_ok=True)
    key.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    return key


@pytest.fixture
def packs_client(
    open_client: Callable[..., AbstractContextManager[TestClient]], pack_key: Path
) -> Iterator[TestClient]:
    """Oidc mode with evidence packs of installation test-1, signed by ``pack_key``."""
    packs = {"installation": "test-1", "signing_key_ref": f"file:{pack_key}"}
    with open_client(auth={"oidc": OIDC}, packs=packs) as client:
        yield client


def signed(client: TestClient, **options: Any) -> Signed:
    """``reports.packs.signed_cycle`` on the client's store: cycle 1 of a run whose keys side a's target
    listed, signed off (``sign=False``: not yet), by the people it names."""
    return client.portal.call(partial(signed_cycle, client.app.state.ctx, **options))


def session_as(client: TestClient, name: str, role: str) -> dict[str, str]:
    """The headers of person ``name``'s session holding ``role`` and those below it; the people of
    ``signed`` keep their user ids."""
    return Callers(client).session(*expand([role]), username=name)


def said(answer: Any) -> tuple[int, str]:
    return answer.status_code, answer.json()["detail"]


async def _audited(ctx: Context, action: str) -> list[tuple[str, str, Any]]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT actor, object, detail FROM ds.audit_log WHERE action = %s ORDER BY id", (action,)
        )
        return await cur.fetchall()


def audited(client: TestClient, action: str) -> list[tuple[str, str, Any]]:
    """The ``action`` rows' actor, object and detail, oldest first."""
    return client.portal.call(_audited, client.app.state.ctx, action)
