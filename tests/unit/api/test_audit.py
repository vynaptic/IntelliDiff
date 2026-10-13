"""The audit log (docs/mvp/AUTH.md §6): every row names its principal, request, session and client
address; the runner, the worker, the migrations and the server audit as themselves; each committed row
leaves a SIEM line, and a rolled-back one none; the log is append-only."""

from __future__ import annotations

import hashlib
import logging
import re
import time
import tomllib
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from deltasleuth import approvals
from deltasleuth.auth.principal import WORKER, expand, local
from deltasleuth.auth.sessions import roles_sha256
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.store import repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.conftest import BUILDS, TESTER

from .conftest import OIDC

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
SOAK = "execution: {concurrency: 1, cycleInterval: 0s, stop: {maxCycles: 2}}\n"


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    yield pool
    await pool.close()


@pytest.fixture
def siem() -> Iterator[list[str]]:
    """The SIEM lines logged on ``deltasleuth.audit``."""
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    logger = logging.getLogger("deltasleuth.audit")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(level)


def _rows(rows: list[repo.Row], action: str) -> list[repo.Row]:
    return [row for row in rows if row["action"] == action]


def test_a_bearer_row_names_the_token_the_request_and_the_client(
    open_client: OpenClient, callers: Any, audit_rows: Any, spec_yaml: str
) -> None:
    with open_client(auth={}) as client:
        created = client.post("/v1/specs", content=spec_yaml, headers=callers(client).token("spec_author"))
        [row] = _rows(audit_rows(client), "spec.create")
    assert (
        re.fullmatch(r"Q-[0-9a-f]{16}", row["request_id"])
        and row["request_id"] == created.headers["x-request-id"]
    )
    assert (row["actor"][:8], row["actor_name"], row["session_id"], str(row["client_addr"])) == (
        "token:T-",
        "ci-bot",
        None,
        "10.1.2.3",
    )


def test_a_cookie_session_row_names_its_session(
    open_client: OpenClient, callers: Any, audit_rows: Any, spec_yaml: str
) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        alice = callers(client).session(*("viewer", "operator", "spec_author"))
        client.post("/v1/specs", content=spec_yaml, headers=alice)
        client.post("/v1/specs", content=spec_yaml, headers=callers(client).token("spec_author"))
        uploads = _rows(audit_rows(client), "spec.create")

        async def by_session(ctx: Any, session_id: str) -> list[repo.Row]:
            async with ctx.pool.connection() as conn:
                return (await repo.list_audit(conn, session_id=session_id))[0]

        mine = client.portal.call(by_session, client.app.state.ctx, uploads[0]["session_id"])
    assert re.fullmatch(r"W-[0-9a-f]{16}", uploads[0]["session_id"]) and uploads[0]["actor"].startswith(
        "user:U-"
    )
    assert (uploads[0]["actor_name"], uploads[1]["session_id"]) == ("alice", None)
    assert [row["id"] for row in mine] == [uploads[0]["id"]]


def test_a_token_sign_in_session_acts_as_its_token(
    open_client: OpenClient, callers: Any, audit_rows: Any, spec_yaml: str
) -> None:
    with open_client(auth={}) as client:
        bearer = callers(client).token("spec_author")
        token = bearer["Authorization"].removeprefix("Bearer ")
        session = callers(client).session(*expand(["spec_author"]), token=token)
        assert client.post("/v1/specs", content=spec_yaml, headers=session).status_code == 201
        [row] = _rows(audit_rows(client), "spec.create")
    assert (row["actor"][:8], row["actor_name"], row["session_id"][:2]) == ("token:T-", "ci-bot", "W-")


@pytest.mark.parametrize(("peer", "recorded"), [("10.20.0.5", "10.1.2.3"), ("10.9.9.9", "10.9.9.9")])
def test_the_client_address_is_resolved_behind_a_trusted_proxy_only(
    open_client: OpenClient, callers: Any, audit_rows: Any, spec_yaml: str, peer: str, recorded: str
) -> None:
    with open_client(auth={"trusted_proxies": ["10.20.0.5"]}, client=(peer, 50000)) as client:
        headers = callers(client).token("spec_author") | {"X-Forwarded-For": "203.0.113.9, 10.1.2.3"}
        client.post("/v1/specs", content=spec_yaml, headers=headers)
        [row] = _rows(audit_rows(client), "spec.create")
    assert str(row["client_addr"]) == recorded


def test_key_views_and_report_downloads_are_audited(
    open_client: OpenClient, callers: Any, seed: Any, audit_rows: Any
) -> None:
    with open_client(auth={}) as client:
        seeded, viewer = seed(client), callers(client).token("viewer")
        assert client.get(
            f"/v1/runs/{seeded.run_id}/keys/{quote(seeded.key, safe='')}", headers=viewer
        ).is_success
        report = client.get(f"/v1/runs/{seeded.run_id}/reports/discrepancies.csv", headers=viewer)
        missing = client.get(f"/v1/runs/{seeded.run_id}/keys/nope", headers=viewer)
        rows = audit_rows(client)
    assert (report.status_code, missing.status_code) == (200, 404)  # a key not shown is not audited
    assert [
        (r["action"], r["object"], r["detail"])
        for r in rows
        if r["action"] in ("key.view", "report.download")
    ] == [
        ("key.view", seeded.run_id, {"key_masked": seeded.key, "cycle": 1}),
        ("report.download", seeded.run_id, {"name": "discrepancies.csv", "cycle": 1}),
    ]
    assert seeded.raw_key not in str(rows)


async def _end(ctx: Any, run_id: str) -> None:
    async with ctx.pool.connection() as conn:
        await repo.set_run_status(conn, run_id, "completed")
        await repo.end_cycle(conn, run_id)


def test_accuracy_audit_holds_no_key(
    open_client: OpenClient, callers: Any, seed: Any, audit_rows: Any
) -> None:
    """``accuracy.draw`` and ``accuracy.label`` hold ids, hashes and counts (docs/mvp/ACCURACY.md §12):
    never a drawn key, masked or raw, nor a note."""
    truth = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01"}
    with open_client(auth={}) as client:
        seeded, operator = seed(client), callers(client).token("operator")
        client.portal.call(_end, client.app.state.ctx, seeded.run_id)
        body = {"size": 1, "per_cause": 1, "truth": truth}
        drawn = client.post(f"/v1/runs/{seeded.run_id}/cycles/1/labellings", json=body, headers=operator)
        labelling_id = drawn.json()["labelling_id"]
        sheet = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=operator).text
        header, line = sheet.split("\r\n")[:2]
        cells = dict(zip(header.split(","), line.split(","), strict=True))
        cells |= {"label": "UNDECIDABLE", "reason": "NOT_LOCATABLE", "notes": f"asked about {seeded.raw_key}"}
        upload = f"{header}\r\n{','.join(cells.values())}\r\n".encode()
        labelled = client.post(
            f"/v1/labellings/{labelling_id}/labels", content=upload,
            headers=operator | {"Content-Type": "text/csv"},
        )  # fmt: skip
        again = client.post(f"/v1/runs/{seeded.run_id}/cycles/1/labellings", json=body, headers=operator)
        sheet_again = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=operator)
        rows = audit_rows(client)
    assert (drawn.status_code, labelled.status_code, sheet_again.status_code) == (201, 200, 200)
    assert drawn.json()["items"] == [
        {"item": 1, "key": seeded.key, "population": "discrepant", "root_cause_id": cells["root_cause_id"]}
    ]
    found = [r for r in rows if r["action"].startswith("accuracy.")]
    assert [(r["action"], r["object"], sorted(r["detail"])) for r in found[:2]] == [
        ("accuracy.draw", labelling_id, sorted(
            ["run_id", "cycle", "salt", "truth", "size", "per_cause", "accepted", "agreed", "unlocatable",
             "item_count", "counts", "selection_sha256"])),
        ("accuracy.label", labelling_id, sorted(
            ["run_id", "cycle", "sheet_sha256", "labels_sha256", "score_sha256", "labelled", "undecidable",
             "not_reviewed"])),
    ]  # fmt: skip  # a sheet download is not audited
    assert found[0]["detail"]["counts"] == {"discrepant": 1, "accepted": 0, "agreed": 0}
    assert (found[1]["detail"]["labelled"], found[1]["detail"]["undecidable"]) == (1, 1)
    assert again.status_code == 201  # the first is labelled: a new draw, audited
    assert [(r["action"], r["object"]) for r in found[2:]] == [
        ("accuracy.draw", again.json()["labelling_id"])
    ]
    text = str(found)
    assert seeded.key not in text and seeded.raw_key not in text and "asked about" not in text
    assert hashlib.sha256(upload).hexdigest() not in text  # nor a hash of the upload, which hashes the note


async def _stored_run(pool: AsyncConnectionPool, run_id: str) -> repo.Row:
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
    assert run is not None
    return run


def test_the_worker_audits_each_cycle_and_the_end_of_a_run(
    open_client: OpenClient, spec_yaml: str, audit_rows: Any
) -> None:
    with open_client(worker=True) as client:
        spec_id = client.post("/v1/specs", content=spec_yaml + SOAK).json()["spec_id"]
        run_id = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "soak"}).json()["run_id"]
        deadline = time.monotonic() + 30
        while client.get(f"/v1/runs/{run_id}").json()["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        rows = [r for r in audit_rows(client) if r["object"] == run_id]
        run = client.portal.call(_stored_run, client.app.state.ctx.pool, run_id)
    plan = rows[0]["detail"].pop("plan_sha256")  # the stored params' hash (docs/mvp/PACK.md §7.4)
    assert plan == sha256_hex(approvals.plan(run["params"]))
    assert [(r["actor"], r["action"], r["detail"]) for r in rows] == [
        (
            "dev:loopback",
            "create_run",
            {
                "spec_id": spec_id,
                "mode": "soak",
                "keys": 1,
                "file_mode": False,
                "approval_id": None,
                "approval": {"needs": ["clear"], "mode": "none"},
                "licence": None,  # an open build records none (docs/mvp/LICENCE.md §19)
            },
        ),
        ("system:worker", "run.cycle", {"cycle": 2}),
        ("system:worker", "run.finished", {"status": "completed", "reason": "stop_condition:maxCycles"}),
    ]
    assert [(r["actor_name"], r["request_id"], r["client_addr"]) for r in rows[1:]] == [
        ("worker", None, None)
    ] * 2
    assert str(rows[0]["client_addr"]) == "127.0.0.1" and rows[0]["request_id"].startswith("Q-")


def test_a_cancel_is_finished_by_its_caller(
    open_client: OpenClient, callers: Any, seed: Any, audit_rows: Any
) -> None:
    with open_client(auth={}) as client:
        run_id, operator = seed(client).run_id, callers(client).token("operator", name="ops-bot")
        assert client.post(f"/v1/runs/{run_id}/cancel", headers=operator).json()["status"] == "cancelled"
        rows = [r for r in audit_rows(client) if r["object"] == run_id]
    assert [(r["actor_name"], r["action"], r["detail"]) for r in rows] == [
        ("ops-bot", "cancelling", {"reason": "cancelled"}),
        ("ops-bot", "run.finished", {"status": "cancelled", "reason": "cancelled"}),
    ]


@pytest.mark.parametrize("build", BUILDS, indirect=True)
@pytest.mark.parametrize(("auth", "mode"), [({}, "tokens"), ({"oidc": OIDC}, "oidc")])
def test_the_server_start_records_the_role_mapping(
    build: str, open_client: OpenClient, audit_rows: Any, repo_root: Path, auth: dict[str, Any], mode: str
) -> None:
    with open_client(auth=auth) as client:
        rows = audit_rows(client)
        [start] = _rows(rows, "system.start")
        [created] = _rows(rows, "installation.created")
        mapping = roles_sha256(client.app.state.ctx.settings.auth.oidc)
        kek = client.app.state.ctx.keyring.current
    # docs/mvp/LICENCE.md §11: a vendor build's evaluation and its two pairs, or an open build's (§19)
    state, pairs = ("evaluation", 2) if build == "vendor_keys" else ("open", None)
    started = {"installation_id": created["object"], "state": state, "licence_id": None}
    started |= {"tier": None, "pairs": pairs, "ends": None}
    assert (start["actor"], start["actor_name"], start["object"]) == ("system:server", "server", "server")
    [head] = [row["chain"] for row in rows if row["chain"]["seq"] == start["chain"]["seq"] - 1]
    assert start["detail"] == {
        # pyproject's, .dev or a release's: a release's checks run this test (docs/mvp/RELEASE.md §10)
        "version": tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["version"],
        "auth": mode,
        "roles_sha256": mapping,
        "retention": {"standard_days": 90, "audit_days": 365},
        "keks": [kek],
        "current_kek": kek,
        "missing_keks": [],
        "pack_key": None,  # no evidence packs configured
        "audit_head": head,  # the chain's head as it started (docs/mvp/PACK.md §4.1)
        "licence": started,  # as it started, before an oidc client's licence was installed
        "mcp": None,  # no mcp block: no MCP server (docs/mvp/MCP.md §4; with one: test_mcp_http.py)
    }
    assert (mapping is None) == (mode == "tokens") and (
        mapping is None or re.fullmatch(r"[0-9a-f]{64}", mapping)
    )


def test_the_server_start_records_the_pack_signing_key(
    open_client: OpenClient, audit_rows: Any, tmp_path: Path
) -> None:
    key = tmp_path / "secrets" / "pack-signing-key"
    key.parent.mkdir(exist_ok=True)
    key.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    packs = {"installation": "test-1", "signing_key_ref": f"file:{key}"}
    with open_client(auth={"oidc": OIDC}, packs=packs) as client:
        [start] = _rows(audit_rows(client), "system.start")
        signer = client.app.state.ctx.signer
    assert (
        signer is not None and start["detail"]["pack_key"] == signer.fingerprint
    )  # the key register's entry


async def test_migrations_are_audited_once_the_log_names_actors(
    pool: AsyncConnectionPool, siem: list[str]
) -> None:
    async with pool.connection() as conn:  # the store as release 004 left it
        await conn.execute("CREATE SCHEMA ds; CREATE TABLE ds.schema_version (version integer PRIMARY KEY)")
        for path in sorted(MIGRATIONS.glob("00[1234]_*.sql")):
            await conn.execute(path.read_bytes())
            await conn.execute("INSERT INTO ds.schema_version VALUES (%s)", (int(path.name[:3]),))
    assert (await migrate(pool), await migrate(pool)) == ([5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17], [])
    async with pool.connection() as conn:
        rows, _ = await repo.list_audit(conn)
    installation_id = rows[1]["object"]
    assert [(r["actor"], r["actor_name"], r["action"], r["object"], r["detail"]) for r in rows] == [
        ("system:migration", "migration", "system.migrate", "schema",
         {"versions": [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]}),
        ("system:migration", "migration", "installation.created", installation_id,
         {"installation_id": installation_id}),
    ]  # fmt: skip
    assert len(siem) == 2 and '"action": "system.migrate"' in siem[1]


async def test_an_empty_database_records_every_migration(pool: AsyncConnectionPool) -> None:
    await migrate(pool)
    async with pool.connection() as conn:
        [row], _ = await repo.list_audit(conn, action="system.migrate")
    assert row["detail"] == {"versions": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]}


async def test_the_siem_line_follows_the_commit_and_a_rollback_leaves_none(
    pool: AsyncConnectionPool, siem: list[str]
) -> None:
    await migrate(pool)
    siem.clear()
    async with repo.audited(pool) as (_, audit):
        row = await audit(TESTER, "spec.create", "1", {"content_hash": "sha256:0"})
        assert siem == []  # not before the commit
    with pytest.raises(RuntimeError):
        async with repo.audited(pool) as (_, audit):
            await audit(TESTER, "spec.create", "2")
            raise RuntimeError("the action failed")
    async with pool.connection() as conn:
        rows, _ = await repo.list_audit(conn, action="spec.create")
    assert [r["object"] for r in rows] == ["1"]
    assert siem == [repo.siem_line(row)]
    assert siem[0].startswith('{"id": ') and '"actor_name": "tester"' in siem[0] and '"at": "' in siem[0]


async def test_local_rows_are_asserted_and_system_rows_carry_no_session(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await migrate(pool)
    monkeypatch.setenv("DS_OPERATOR", "jdoe")
    cli = local()
    async with repo.audited(pool) as (_, audit):
        mine = await audit(cli, "create_run", "R-1", {"keys": 1})
        worker = await audit(WORKER, "run.finished", "R-1", {"status": "completed", "reason": None})
    assert mine["detail"] == {"keys": 1, "asserted": True, "os_account": cli.os_account}
    assert (mine["request_id"], mine["session_id"], mine["actor_name"]) == (cli.request_id, None, "jdoe")
    assert (worker["detail"], worker["request_id"], worker["session_id"]) == (
        {"status": "completed", "reason": None},
        None,
        None,
    )


async def test_the_audit_log_pages_newest_first_and_filters_exactly(pool: AsyncConnectionPool) -> None:
    await migrate(pool)
    async with repo.audited(pool) as (_, audit):
        for i in range(5):
            await audit(TESTER, "key.view" if i % 2 else "report.download", f"R-{i}")
    async with pool.connection() as conn:
        page, cursor = await repo.list_audit(conn, limit=2, obj=None, action=None)
        rest, end = await repo.list_audit(conn, limit=10, cursor=cursor)
        views, _ = await repo.list_audit(conn, action="key.view", actor=TESTER.actor)
        with pytest.raises(ValueError):
            await repo.list_audit(conn, cursor="x.y")
    objects = ["R-4", "R-3", "R-2", "R-1", "R-0", "schema", rest[-1]["object"]]  # then installation.created
    assert [r["object"] for r in page + rest] == objects and objects[-1].startswith("I-") and end is None
    assert [r["object"] for r in views] == ["R-3", "R-1"]


async def test_the_audit_log_refuses_updates_and_deletes(pool: AsyncConnectionPool) -> None:
    await migrate(pool)
    for statement in (
        "UPDATE ds.audit_log SET actor = 'api'",
        "DELETE FROM ds.audit_log",
        "TRUNCATE ds.audit_log CASCADE",  # the chain's rows reference it (docs/mvp/PACK.md §8)
    ):
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                await conn.execute(statement)


def test_the_audit_log_reads_page_by_page_and_every_read_is_audited(
    open_client: OpenClient, callers: Any, audit_rows: Any
) -> None:
    with open_client(auth={"oidc": OIDC}, tier=None) as client:  # reads need no licence (LICENCE.md §2)
        as_ = callers(client)
        admin = as_.session(*expand(["admin"]), auth_time=datetime.now(UTC))
        viewer = as_.session("viewer", username="bob")

        def read(**query: Any) -> Any:
            return client.get("/v1/audit", params=query, headers=admin)

        first = read(limit=5).json()  # system.start, keys.created (evidence, address), the migration's
        second = read(limit=2).json()  # first's audit.read, system.start
        rest = read(limit=4, cursor=second["next_cursor"]).json()
        me = client.get("/v1/auth/me", headers=admin).json()["actor"]
        reads = read(action="audit.read", actor=me, since="2020-01-01T00:00:00Z").json()["items"]
        filtered = audit_rows(client)[-1]
        refused = [read(cursor="x"), read(limit=0), read(limit=501), client.get("/v1/audit", headers=viewer)]
    started = ["keys.created", "keys.created", "system.migrate", "installation.created"]
    assert ([r["action"] for r in first["items"]], first["next_cursor"]) == (["system.start", *started], None)
    assert [r["action"] for r in second["items"]] == ["audit.read", "system.start"] and second["next_cursor"]
    assert ([r["action"] for r in rest["items"]], rest["next_cursor"]) == (started, None)
    assert [r["detail"] for r in reads] == [{"filters": {}}] * 3  # limit and cursor are not filters
    assert filtered["detail"] == {
        "filters": {"action": "audit.read", "actor": me, "since": "2020-01-01T00:00:00+00:00"}
    }
    item = reads[0]
    assert (item["actor"], item["actor_name"], item["object"], item["client_addr"]) == (
        me,
        "alice",
        "audit",
        "10.1.2.3",
    )
    assert item["session_id"].startswith("W-") and item["request_id"].startswith("Q-")
    assert [(r.status_code, r.json()["detail"]) for r in refused] == [
        (400, "malformed cursor"),
        (422, "invalid request"),
        (422, "invalid request"),
        (403, "needs the admin role"),
    ]


def test_a_one_shot_compare_finishes_its_run_as_its_caller(
    client: TestClient, seeded: Any, audit_rows: Any
) -> None:
    now = client.post("/v1/compare", json={"spec_id": seeded.spec_id, "key": seeded.raw_key}).json()
    trail = [
        (row["actor"], row["action"], row["detail"])
        for row in audit_rows(client)
        if row["object"] == now["run_id"]
    ]
    assert trail == [
        (
            "dev:loopback",
            "compare_one",
            {
                "spec_id": seeded.spec_id,
                "approval_id": None,
                "approval": {"needs": ["clear"], "mode": "none"},
                "licence": None,
            },
        ),
        ("dev:loopback", "run.finished", {"status": "completed", "reason": None}),
    ]
