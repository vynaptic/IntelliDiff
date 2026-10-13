"""Migration 010: the installation id, made once, audited as it is made and
kept by a copy of the database, one taken before 010 included; ds.licences, whose rows are only ever
withdrawn, once, as of now(); a store at 010 opened by a release that ships only 001-009."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth import licence
from deltasleuth.store import db, repo
from deltasleuth.store.db import MIGRATIONS, migrate
from tests.conftest import TESTER, licence_file

pytestmark = pytest.mark.db

APPEND_ONLY = "installation is append-only"
WITHDRAWN_ONCE = r"ds.licences: licence LI-\w+ is only ever withdrawn, once, as of now\(\)"


async def _all(conn: repo.Conn, sql: str, params: tuple = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


async def _installation(pool: AsyncConnectionPool) -> str:
    async with pool.connection() as conn:
        [(installation_id,)] = await _all(conn, "SELECT installation_id FROM ds.installation")
    return installation_id


async def _derived(pool: AsyncConnectionPool) -> str:
    """The id 010 derives from the first audit chain row when there is one."""
    async with pool.connection() as conn:
        [(first,)] = await _all(conn, "SELECT hash FROM ds.audit_chain ORDER BY seq LIMIT 1")
    return "I-" + hashlib.sha256(f"deltasleuth-installation\n{first}".encode()).hexdigest()[:16]


def _ctx(pool: AsyncConnectionPool) -> Any:
    """A context as ``licence.install`` reads it: the pool, tokens mode, no packs."""
    return SimpleNamespace(pool=pool, settings=SimpleNamespace(auth_mode="tokens", packs=None))


@asynccontextmanager
async def _copied(pg_server: str, pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool on a ``CREATE DATABASE … TEMPLATE`` copy of ``pg_dsn`` (as a dump and restore makes), which
    no session may hold; the copy is dropped after."""
    copy = f"t_{uuid.uuid4().hex[:12]}"
    source = pg_dsn.rsplit("/", 1)[1]
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{copy}" TEMPLATE "{source}"')  # type: ignore[arg-type]
    try:
        copied = AsyncConnectionPool(pg_server.rsplit("/", 1)[0] + f"/{copy}", open=False)
        await copied.open(wait=True)
        try:
            yield copied
        finally:
            await copied.close()
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{copy}" WITH (FORCE)')  # type: ignore[arg-type]


async def _store(pool: AsyncConnectionPool) -> licence.Licence:
    """A licence for the store's installation, stored as ``licence.install`` stores it."""
    found = licence.parse(licence_file(await _installation(pool)))
    async with pool.connection() as conn:
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=TESTER.actor)
    return found


@pytest.mark.parametrize("over", [9, None])  # a store at 009, and an empty one
async def test_010_over_009_and_empty(empty_pool: AsyncConnectionPool, over: int | None) -> None:
    before = [] if over is None else [("system.migrate", {"versions": list(range(1, 10))})]
    if over is not None:
        assert await migrate(empty_pool, upto=over) == list(range(1, 10))
    versions = list(range(1, 11)) if over is None else [10]
    assert await migrate(empty_pool, upto=10) == versions
    async with empty_pool.connection() as conn:
        audited = await _all(conn, "SELECT action, detail FROM ds.audit_log ORDER BY id")
        indexes = await _all(conn, "SELECT indexname FROM pg_indexes WHERE indexname = 'runs_window_idx'")
    installation_id = await _installation(empty_pool)
    assert re.fullmatch(r"I-[0-9a-f]{16}", installation_id) and indexes == [("runs_window_idx",)]
    # over 009 the id is the first audit row's (system.migrate); an empty store's is random
    assert (installation_id == await _derived(empty_pool)) is (over is not None)
    assert audited == [
        *before,
        ("installation.created", {"installation_id": installation_id}),
        ("system.migrate", {"versions": versions}),
    ]


async def test_installation_one_row_append_only(pool: AsyncConnectionPool) -> None:
    installation_id = await _installation(pool)
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation, match="installation_one"):
            await conn.execute("INSERT INTO ds.installation (installation_id) VALUES ('I-0123456789abcdef')")
    for statement in (
        "UPDATE ds.installation SET installation_id = 'I-0123456789abcdef'",
        "DELETE FROM ds.installation",
        "TRUNCATE ds.installation",
    ):
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.RaiseException, match=APPEND_ONLY):
                await conn.execute(statement)  # type: ignore[arg-type]
    assert await _installation(pool) == installation_id


@pytest.mark.usefixtures("vendor_keys")
async def test_withdrawn_once_as_of_now(pool: AsyncConnectionPool) -> None:
    found = await _store(pool)
    restrict, check = psycopg.errors.RestrictViolation, psycopg.errors.CheckViolation
    refused = [
        ("UPDATE ds.licences SET text = text || ' ' WHERE licence_id = %s", restrict),  # an edited text
        ("UPDATE ds.licences SET installed_by = installed_by WHERE licence_id = %s", restrict),
        ("UPDATE ds.licences SET withdrawn_by = 'x', withdrawn_at = now() - interval '1 day',"
         " withdraw_reason = 'backdated' WHERE licence_id = %s", restrict),
        ("UPDATE ds.licences SET withdrawn_by = 'x', withdrawn_at = now() WHERE licence_id = %s", check),
        ("DELETE FROM ds.licences WHERE licence_id = %s", restrict),
    ]  # fmt: skip
    for statement, error in refused:
        async with pool.connection() as conn:
            with pytest.raises(error):
                await conn.execute(statement, (found.licence_id,))  # type: ignore[arg-type]
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="licences is append-only"):
            await conn.execute("TRUNCATE ds.licences")
    async with pool.connection() as conn:
        row = await repo.withdraw_licence(conn, found.licence_id, TESTER.actor, "the wrong order")
        [(at,)] = await _all(conn, "SELECT now()")
    assert row is not None and (row["withdrawn_at"], row["withdraw_reason"]) == (at, "the wrong order")
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RestrictViolation, match=WITHDRAWN_ONCE):
            await conn.execute(
                "UPDATE ds.licences SET withdrawn_by = 'y', withdrawn_at = now(), withdraw_reason = 'again'"
                " WHERE licence_id = %s",
                (found.licence_id,),
            )
    async with pool.connection() as conn:
        assert await repo.withdraw_licence(conn, found.licence_id, TESTER.actor, "again") is None


async def test_installation_created_audited(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        row = await repo.last_audit(conn, "installation.created")
    installation_id = await _installation(pool)
    assert row is not None
    assert (row["actor"], row["actor_name"], row["object"], row["detail"]) == (
        "system:migration", "migration", installation_id, {"installation_id": installation_id}
    )  # fmt: skip
    assert await migrate(pool) == []  # made once
    async with pool.connection() as conn:
        assert len(await _all(conn, "SELECT 1 FROM ds.audit_log WHERE action = 'installation.created'")) == 1


@pytest.mark.usefixtures("vendor_keys")
async def test_template_copy_keeps_id_and_licence(pg_server: str, pg_dsn: str) -> None:
    """A copy of the database (as a dump and restore makes) is the same installation, licence included."""
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    await migrate(pool)
    installation_id, found = await _installation(pool), await _store(pool)
    await pool.close()  # a template has no other session
    async with _copied(pg_server, pg_dsn) as copied:
        assert await migrate(copied) == []
        async with copied.connection() as conn:
            facts = await repo.licence_facts(conn)
    assert facts["installation_id"] == installation_id
    assert [(row["licence_id"], row["text"]) for row in facts["rows"]] == [(found.licence_id, found.text)]


@pytest.mark.usefixtures("vendor_keys")
async def test_backup_before_010_keeps_id(pg_server: str, pg_dsn: str) -> None:
    """A backup taken at 009, restored after the upgrade, gets the same id when 010 applies to it again:
    the licence installed on the upgraded store installs on it."""
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    assert await migrate(pool, upto=9) == list(range(1, 10))  # audited: system.migrate
    await pool.close()
    async with _copied(pg_server, pg_dsn) as backup:
        live = AsyncConnectionPool(pg_dsn, open=False)
        await live.open(wait=True)
        assert await migrate(live) == [10, 11, 12, 13, 14, 15, 16, 17]
        installation_id = await _installation(live)
        data = licence_file(installation_id)
        assert (await licence.install(_ctx(live), data, who=TESTER))["installed"]
        await live.close()
        assert await migrate(backup) == [10, 11, 12, 13, 14, 15, 16, 17]  # the restore's first command
        assert await _installation(backup) == installation_id
        installed = await licence.install(_ctx(backup), data, who=TESTER)
    assert (installed["installed"], installed["applies"]) == (True, "now")


async def test_store_at_010_opens_with_001_to_009_files(
    pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release before 010 beside the new one: its migrate applies nothing."""
    for path in MIGRATIONS.glob("00[1-9]_*.sql"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(db, "MIGRATIONS", tmp_path)
    async with pool.connection() as conn:
        before = await _all(conn, "SELECT count(*) FROM ds.audit_log")
    assert await migrate(pool) == []
    async with pool.connection() as conn:
        assert await _all(conn, "SELECT count(*) FROM ds.audit_log") == before
        assert await _all(conn, "SELECT max(version) FROM ds.schema_version") == [(17,)]
