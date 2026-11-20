"""The audit chain's anchor outside the database, and the database roles.

The app writes the chain's head, keyed with a key the database never holds, to its workspace and its log
at start, at each sweep and at stop, and never moves it back. ``ds audit verify`` refuses a chain that ends
before its anchor or whose anchored row was rewritten, says so on a line of its own when nothing outside
the database was checked, and says whether the app's role can switch the audit log's triggers off. The role
whose grants ``ds db grants`` prints cannot, and with ``migrate_dsn_ref`` only ``ds migrate`` changes the
schema."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool
from typer.testing import CliRunner, Result

from deltasleuth.api import create_app
from deltasleuth.cli.main import app
from deltasleuth.config import Settings, load_settings
from deltasleuth.context import Context, ContextError, anchor_key, init_workspace
from deltasleuth.runner.worker import _Worker
from deltasleuth.security.tokenise import load_or_create_key
from deltasleuth.store import chain, repo
from deltasleuth.store.db import NEEDS_MIGRATION, migrate
from tests.conftest import TESTER

pytestmark = pytest.mark.db

ALTERED = "the audit log was altered after it was written"
NOTHING = "checked against nothing outside the database: a holder of its role can truncate it unseen"
SHARED = (
    "roles: shared: the role of database_dsn_ref is a superuser, so whoever holds it can switch the audit"
    " log's triggers off ('ds db grants' prints the grants of a role that cannot)"
)
SEPARATE = (
    "roles: separate: the role of database_dsn_ref owns nothing in schema ds and cannot switch the audit"
    " log's triggers off; migrate_dsn_ref's role applies the migrations"
)
TARGETS = """\
targets:
  svc: {base_url: "http://127.0.0.1:9/", allow_private_network: true, allow_insecure_http: true}
"""
TRIGGERS = ("ds.audit_log", "ds.audit_chain")
PASSWORD = "invented-app-password"  # noqa: S105 - a throwaway role's, on a throwaway server


@pytest.fixture
def audit_logger() -> Iterator[None]:
    """``deltasleuth.audit`` restored after a command attached its SIEM copy to it."""
    logger = logging.getLogger("deltasleuth.audit")
    handlers, propagate, level = logger.handlers, logger.propagate, logger.level
    logger.handlers, logger.propagate = [], True
    yield
    logger.handlers, logger.propagate = handlers, propagate
    logger.setLevel(level)


@pytest.fixture
def chain_lines() -> Iterator[list[str]]:
    """The lines logged on ``deltasleuth.store.chain``, from INFO up, wherever its records propagate."""
    lines: list[str] = []
    handler = logging.Handler(logging.INFO)
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    logger = logging.getLogger("deltasleuth.store.chain")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(level)


@pytest.fixture
def roles(pg_dsn: str) -> Iterator[tuple[str, str]]:
    """Two role names for this test, the schema owner's and the application's; the test makes them. Each is
    dropped with what it owns and was granted."""
    tag = uuid.uuid4().hex[:8]
    made = (f"ds_owner_{tag}", f"ds_app_{tag}")
    yield made
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for role in made:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                conn.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER".encode())
                conn.execute(f"DROP OWNED BY {role}".encode())
                conn.execute(f"DROP ROLE {role}".encode())


@pytest.fixture
def app_role(pg_dsn: str) -> Iterator[str]:
    """A role name for this test; the role, made by the test, is dropped with what it was granted."""
    role = f"ds_app_{uuid.uuid4().hex[:8]}"
    yield role
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute(f"DROP OWNED BY {role}".encode())
            conn.execute(f"DROP ROLE {role}".encode())


def _config(tmp_path: Path, dsn: str, **more: Any) -> Path:
    """Developer-mode settings on ``dsn``, in ``tmp_path``: workspace ``w``."""
    (tmp_path / "dsn").write_text(dsn + "\n")
    (tmp_path / "t.yaml").write_text(TARGETS)
    doc = {"database_dsn_ref": "file:dsn", "evidence_dir": "e", "workspace_dir": "w",
           "targets_file": "t.yaml"}  # fmt: skip
    (tmp_path / "ds.yaml").write_text(yaml.safe_dump(doc | more))
    return tmp_path / "ds.yaml"


def _as(dsn: str, role: str) -> str:
    """``dsn`` with ``role`` and the test's password."""
    return make_conninfo("", **conninfo_to_dict(dsn) | {"user": role, "password": PASSWORD})


@asynccontextmanager
async def _pool(dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(dsn, open=False, min_size=1, max_size=2)
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()


def _ds(config: Path | None, *args: Any) -> Result:
    tail = [] if config is None else ["--config", config]
    return CliRunner().invoke(app, [str(a) for a in [*args, *tail]])


def _set_up(tmp_path: Path, dsn: str) -> Path:
    config = _config(tmp_path, dsn)
    assert _ds(config, "init").exit_code == _ds(config, "migrate").exit_code == 0
    return config


def _as_owner(dsn: str, *statements: str, params: tuple[Any, ...] = ()) -> None:
    """``statements`` in one transaction with the audit log's and chain's triggers off, as whoever owns
    them (or a superuser) can run them."""
    with psycopg.connect(dsn) as conn:
        for table in TRIGGERS:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER".encode())
        for statement in statements:
            conn.execute(statement.encode(), params if "%s" in statement else None)
        for table in TRIGGERS:
            conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER".encode())


def _truncated(dsn: str, keep: int) -> None:
    """The chain's rows after row ``keep`` deleted, with their audit rows."""
    _as_owner(
        dsn,
        "DELETE FROM ds.audit_chain WHERE seq > %s",
        "DELETE FROM ds.audit_log l WHERE NOT EXISTS (SELECT 1 FROM ds.audit_chain c WHERE c.id = l.id)",
        params=(keep,),
    )


def _rewritten(dsn: str, seq: int) -> None:
    """Row ``seq``'s detail edited, and the chain from it on recomputed: each text from its audit row and
    each link and hash again, so that the chain agrees with itself."""
    with psycopg.connect(dsn) as conn:
        for table in TRIGGERS:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER".encode())
        conn.execute(
            """UPDATE ds.audit_log SET detail = '{"edited": true}'
               WHERE id = (SELECT id FROM ds.audit_chain WHERE seq = %s)""",
            (seq,),
        )
        rows = conn.execute(
            "SELECT c.seq, c.object, ds.audit_text(l) FROM ds.audit_chain c JOIN ds.audit_log l USING (id)"
            " ORDER BY c.seq"
        ).fetchall()
        prev, last = chain.ZERO, {}
        for n, obj, text in rows:
            link = last.get(obj, chain.ZERO)
            digest = chain.chain_hash(prev, link, text)
            if n >= seq:
                conn.execute(
                    "UPDATE ds.audit_chain SET prev = %s, obj_prev = %s, text = %s, hash = %s WHERE seq = %s",
                    (prev, link, text, digest, n),
                )
            prev = last[obj] = digest
        for table in TRIGGERS:
            conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER".encode())


def _head(dsn: str) -> tuple[int, str, str]:
    """The chain's last row: its seq, its hash and its audit row's action."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT c.seq, c.hash, l.action FROM ds.audit_chain c JOIN ds.audit_log l USING (id)"
            " ORDER BY c.seq DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return row


async def _written(config: Path, rows: int) -> chain.Anchor:
    """``rows`` audit rows, then the anchor a sweep writes: the one now in the workspace."""
    async with Context.open(load_settings(config), apply_migrations=False) as ctx:
        for n in range(rows):
            async with repo.audited(ctx.pool) as (_, audit):
                await audit(TESTER, "spec.create", str(n))
        await chain.anchored(ctx)
        return chain.read_anchor(chain.anchor_path(ctx.settings), ctx.anchor_key)


async def _swept(settings: Settings, rows: int) -> None:
    """``rows`` audit rows, then one sweep of a worker."""
    async with Context.open(settings, apply_migrations=False) as ctx:
        for n in range(rows):
            async with repo.audited(ctx.pool) as (_, audit):
                await audit(TESTER, "spec.create", f"swept-{n}")
        worker = _Worker(ctx)
        try:
            await worker._sweep_once()
        finally:
            await worker.close()


# -- the anchor ------------------------------------------------------------------------------------


@pytest.mark.usefixtures("audit_logger")
def test_a_chain_whose_newest_rows_were_deleted_is_refused_against_its_anchor(
    pg_dsn: str, tmp_path: Path
) -> None:
    config = _set_up(tmp_path, pg_dsn)
    anchored = asyncio.run(_written(config, 3))
    path = tmp_path / "w" / chain.ANCHOR_FILE
    shutil.copy(path, tmp_path / "copy.json")  # a copy kept elsewhere
    intact = _ds(config, "audit", "verify")
    assert intact.exit_code == 0, intact.output
    said = f"anchor: row {anchored.seq} {anchored.hash}, written {anchored.at} ({path}): the chain holds it"
    assert said in intact.stdout.splitlines() and NOTHING not in intact.stdout
    _truncated(pg_dsn, keep=anchored.seq - 2)  # as the database's owner: triggers off, rows gone, on again
    path.rename(tmp_path / "aside.json")
    alone = _ds(config, "audit", "verify")  # the shortened chain agrees with itself
    assert alone.exit_code == 0, alone.output
    assert f"chain: {anchored.seq - 2} row(s) verified" in alone.stdout
    assert NOTHING in alone.stdout.splitlines()
    (tmp_path / "aside.json").rename(path)
    missing = (
        f"rows {anchored.seq - 1} to {anchored.seq} are missing, though the anchor written {anchored.at}"
        f" holds row {anchored.seq}: {ALTERED}"
    )
    refused = _ds(config, "audit", "verify")
    assert (refused.exit_code, refused.stdout, refused.stderr) == (2, "", f"error: {missing}\n")
    path.unlink()
    copied = _ds(config, "audit", "verify", "--anchor", tmp_path / "copy.json")
    assert (copied.exit_code, copied.stderr) == (2, f"error: {missing}\n")
    logged = tmp_path / "logged.txt"  # the anchor's log line, copied from the app's log
    logged.write_text(f"2026-11-20 09:00:00,000 INFO deltasleuth.store.chain audit anchor {anchored.text()}")
    from_log = _ds(config, "audit", "verify", "--anchor", logged)
    assert (from_log.exit_code, from_log.stderr) == (2, f"error: {missing}\n")


@pytest.mark.usefixtures("audit_logger")
def test_a_chain_rewritten_with_recomputed_hashes_is_refused_against_its_anchor(
    pg_dsn: str, tmp_path: Path
) -> None:
    config = _set_up(tmp_path, pg_dsn)
    anchored = asyncio.run(_written(config, 3))
    path = tmp_path / "w" / chain.ANCHOR_FILE
    _rewritten(pg_dsn, anchored.seq - 1)
    path.rename(tmp_path / "aside.json")
    alone = _ds(config, "audit", "verify")  # every link and hash agrees: only something outside can tell
    assert alone.exit_code == 0 and NOTHING in alone.stdout.splitlines(), alone.output
    (tmp_path / "aside.json").rename(path)
    refused = _ds(config, "audit", "verify")
    said = f"row {anchored.seq} (spec.create 2) is not the row the anchor written {anchored.at} holds"
    said += f": {ALTERED}"
    assert (refused.exit_code, refused.stdout, refused.stderr) == (2, "", f"error: {said}\n")


@pytest.mark.usefixtures("audit_logger")
def test_an_anchor_without_this_installation_s_key_is_refused(pg_dsn: str, tmp_path: Path) -> None:
    config = _set_up(tmp_path, pg_dsn)
    anchored = asyncio.run(_written(config, 1))
    path = tmp_path / "w" / chain.ANCHOR_FILE
    forged = json.loads(path.read_text()) | {"seq": anchored.seq - 1}  # moved back, its mac kept
    path.write_text(json.dumps(forged) + "\n")
    refused = _ds(config, "audit", "verify")
    said = f"the anchor in {path} was not written with this installation's key: it was altered or forged"
    assert (refused.exit_code, refused.stderr) == (2, f"error: {said}\n")
    path.write_text("not an anchor\n")
    assert _ds(config, "audit", "verify").stderr == f"error: {path} holds no audit anchor\n"


@pytest.mark.usefixtures("audit_logger")
def test_the_command_says_what_it_checked_outside_the_database(pg_dsn: str, tmp_path: Path) -> None:
    config = _set_up(tmp_path, pg_dsn)
    first = _ds(config, "audit", "verify")  # its start writes keys.created rows, before it reads the chain
    seq, digest, _ = _head(pg_dsn)
    path = tmp_path / "w" / chain.ANCHOR_FILE
    assert (first.exit_code, first.stdout.splitlines()) == (0, [
        f"chain: {seq} row(s) verified",
        f"anchor: none ({path} is absent)",
        NOTHING,
        SHARED,
        f"head: row {seq} {digest}",
    ])  # fmt: skip
    with psycopg.connect(pg_dsn) as conn:
        cur = conn.execute(f"SELECT {repo._AUDIT_COLUMNS} FROM ds.audit_log ORDER BY id")  # noqa: S608
        names = [column.name for column in cur.description or ()]
        rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    (tmp_path / "siem.jsonl").write_text("".join(repo.siem_line(row) + "\n" for row in rows))
    against = _ds(config, "audit", "verify", "--siem", tmp_path / "siem.jsonl")
    assert against.exit_code == 0 and NOTHING not in against.stdout, against.output
    assert f"anchor: none ({path} is absent)" in against.stdout.splitlines()


def test_the_app_anchors_the_chain_at_start_at_each_sweep_and_at_stop(
    pg_dsn: str, tmp_path: Path, chain_lines: list[str]
) -> None:
    settings = load_settings(_config(tmp_path, pg_dsn))
    init_workspace(settings)
    key = anchor_key(load_or_create_key(settings.workspace_dir / "tokeniser.key"))  # never the database's
    path = chain.anchor_path(settings)
    with TestClient(create_app(settings)):  # the start migrates and writes system.start
        started = chain.read_anchor(path, key)
        assert (started.seq, started.hash, "system.start") == _head(pg_dsn)
        with psycopg.connect(pg_dsn) as conn:  # a row the stop anchors
            conn.execute(
                "INSERT INTO ds.audit_log (actor, action, object) VALUES ('system:server', 'probe', 'server')"
            )
    stopped = chain.read_anchor(path, key)
    assert (stopped.seq, stopped.hash) == _head(pg_dsn)[:2] and stopped.seq == started.seq + 1
    asyncio.run(_swept(settings, 2))
    swept = chain.read_anchor(path, key)
    assert swept.seq >= stopped.seq + 2
    _truncated(pg_dsn, keep=stopped.seq)  # the swept rows deleted with the triggers off
    asyncio.run(_swept(settings, 0))
    assert chain.read_anchor(path, key) == swept  # never moved back
    assert f"audit anchor {started.text()}" in chain_lines and f"audit anchor {stopped.text()}" in chain_lines
    assert SHARED in chain_lines
    kept = f"audit anchor kept at row {swept.seq}: the audit chain no longer holds that row"
    kept += "; run ds audit verify"
    assert chain_lines.count(kept) == 1
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# -- the roles -------------------------------------------------------------------------------------


async def _attacks(dsn: str) -> list[str]:
    """What each way to switch the audit log's triggers off, or around them, answers this role: its
    SQLSTATE, or "ok"."""
    attacks = [
        "ALTER TABLE ds.audit_log DISABLE TRIGGER USER",
        "ALTER TABLE ds.audit_chain DISABLE TRIGGER audit_chain_append_only",
        "SET session_replication_role = replica",
        "DELETE FROM ds.audit_chain WHERE seq = (SELECT max(seq) FROM ds.audit_chain)",
        "UPDATE ds.audit_log SET detail = '{}'",
        "TRUNCATE ds.audit_log CASCADE",
        "CREATE TRIGGER quiet BEFORE INSERT ON ds.audit_log FOR EACH ROW"
        " EXECUTE FUNCTION ds.tg_audit_chain()",
        "CREATE OR REPLACE FUNCTION ds.append_only() RETURNS trigger LANGUAGE plpgsql"
        " AS 'BEGIN RETURN NULL; END'",
        "DROP TABLE ds.audit_chain_upgrade",
    ]
    answers = []
    for attack in attacks:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            try:
                await conn.execute(attack.encode())
                answers.append("ok")
            except psycopg.Error as exc:
                answers.append(exc.sqlstate or "?")
            await conn.rollback()
    return answers


async def test_the_role_ds_db_grants_makes_cannot_switch_the_audit_log_s_triggers_off(
    pg_dsn: str, roles: tuple[str, str]
) -> None:
    owner, application = roles
    database = conninfo_to_dict(pg_dsn)["dbname"]
    with psycopg.connect(pg_dsn, autocommit=True) as conn:  # as a managed server's: no superuser
        conn.execute(f"CREATE ROLE {owner} LOGIN CREATEROLE PASSWORD '{PASSWORD}'".encode())
        conn.execute(f'ALTER DATABASE "{database}" OWNER TO {owner}'.encode())
    owner_dsn, app_dsn = (_as(pg_dsn, role) for role in roles)
    async with _pool(owner_dsn) as pool:
        assert await migrate(pool)  # ds migrate, as the schema's owner
        printed = _ds(None, "db", "grants", "--role", application)
        assert printed.exit_code == 0, printed.output
        async with pool.connection() as conn:  # the role's password is set apart
            await conn.execute(printed.stdout.encode())
            await conn.execute(f"ALTER ROLE {application} PASSWORD '{PASSWORD}'".encode())
        async with pool.connection() as conn:
            guard = await repo.audit_guard(conn)
        assert guard == {"superuser": False, "owner": True, "trigger": True, "replica": False}
    # The owner switches the triggers off, or adds or replaces one, as it likes; only a superuser sets
    # session_replication_role, and the triggers refuse the three statements (raise_exception) while on.
    assert await _attacks(owner_dsn) == ["ok", "ok", "42501", "P0001", "P0001", "P0001", "ok", "ok", "ok"]
    assert await _attacks(app_dsn) == ["42501"] * 9  # insufficient_privilege, each one
    async with _pool(app_dsn) as restricted:
        async with repo.audited(restricted) as (_, audit):  # what the app writes, chained by the trigger
            await audit(TESTER, "spec.create", "1")
        async with restricted.connection() as conn:
            assert (await chain.verify(conn))["rows"] >= 2
        async with restricted.connection() as conn:
            guard = await repo.audit_guard(conn)
        assert guard == dict.fromkeys(("superuser", "owner", "trigger", "replica"), False)
    async with _pool(pg_dsn) as pool, pool.connection() as conn:
        assert (await repo.audit_guard(conn))["superuser"] is True


def test_ds_db_grants_names_a_role_by_a_plain_name_only() -> None:
    for role in ('app"; DROP SCHEMA ds; --', "App", "1app", "a" * 64, ""):
        refused = _ds(None, "db", "grants", "--role", role)
        assert refused.exit_code == 2, role
    assert "error: --role must be 1 to 63 of a-z, 0-9 and _, starting with a letter or _" in refused.stderr
    printed = _ds(None, "db", "grants").stdout
    assert 'GRANT USAGE ON SCHEMA ds TO "deltasleuth_app";' in printed


@pytest.mark.usefixtures("audit_logger")
def test_with_migrate_dsn_ref_only_ds_migrate_changes_the_schema(
    pg_dsn: str, tmp_path: Path, app_role: str
) -> None:
    (tmp_path / "owner_dsn").write_text(pg_dsn + "\n")
    config = _config(tmp_path, pg_dsn, migrate_dsn_ref="file:owner_dsn")
    assert _ds(config, "init").exit_code == 0
    settings = load_settings(config)

    async def opened() -> None:
        async with Context.open(settings):  # as ds serve and ds worker open it
            pass

    with pytest.raises(ContextError) as refused:  # its role could, here: it applies none all the same
        asyncio.run(opened())
    assert str(refused.value) == NEEDS_MIGRATION
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('ds')").fetchone() == (None,)
    absent = pg_dsn.rsplit("/", 1)[0] + "/no_such_database"  # never reached: ds migrate uses the owner's
    (tmp_path / "dsn").write_text(absent + "\n")
    migrated = _ds(config, "migrate")
    assert migrated.exit_code == 0 and migrated.stdout.startswith("migrations applied: 1, "), migrated.output
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(_ds(None, "db", "grants", "--role", app_role).stdout.encode())
        conn.execute(f"ALTER ROLE {app_role} PASSWORD '{PASSWORD}'".encode())
    (tmp_path / "dsn").write_text(_as(pg_dsn, app_role) + "\n")
    asyncio.run(opened())  # the app's role creates the data keys: rights on data only
    verified = _ds(config, "audit", "verify")
    assert verified.exit_code == 0 and SEPARATE in verified.stdout.splitlines(), verified.output
