"""The migration runner: a database newer than the build is refused before anything runs, every applied
file's statements are checked against the build's, each version is logged as it runs, and only a process
asked to apply migrations applies them."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.config import load_settings
from deltasleuth.context import Context, ContextError, init_workspace
from deltasleuth.store import db
from deltasleuth.store.db import MIGRATIONS, MigrationError, checksum, migrate
from tests.unit.store.test_context import settings_file

NEWEST = max(int(path.name[:3]) for path in MIGRATIONS.glob("*.sql"))
CHECKSUMS = 19  # the migration that adds the column
FILES = {int(path.name[:3]): path for path in MIGRATIONS.glob("*.sql")}


def _hash(text: str) -> str:
    return checksum(text.encode())


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("CREATE TABLE t (a int, b int);", "-- the table\nCREATE  TABLE t (\n  a int, -- one\n  b int\n);"),
        ("SELECT 1;", "/* a /* nested */ note */ SELECT 1 ;"),
        ("CHECK (x IN ('a', 'b'))", "CHECK(x IN('a','b'))"),
        (
            "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END $$",
            "CREATE FUNCTION f() RETURNS int AS $$\nBEGIN\n    -- one\n    RETURN 1;\nEND\n$$",
        ),
        ("x <= 1", "x\t<=\n1"),
    ],
)
def test_comments_and_layout_keep_the_checksum(one: str, other: str) -> None:
    assert _hash(one) == _hash(other)


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("SELECT '--a';", "SELECT '--b';"),  # a comment mark in a literal is text
        ("SELECT 'a  b';", "SELECT 'a b';"),  # so is the space in it
        ("SELECT E'it\\'s -- x';", "SELECT E'it\\'s -- y';"),  # an escaped quote does not end it
        ('SELECT "a  b";', 'SELECT "a b";'),  # a quoted name
        ("NOT NULL", "NOTNULL"),  # words a space keeps apart
        ("x < = 1", "x <= 1"),  # and operators
        ("CHECK (n >= 0)", "CHECK (n >= 1)"),
        ("SELECT 1; -- a\nSELECT 2;", "SELECT 1; -- a SELECT 2;"),
    ],
)
def test_a_statement_change_changes_the_checksum(one: str, other: str) -> None:
    assert _hash(one) != _hash(other)


def test_the_checksum_is_the_sha256_of_the_statements() -> None:
    assert checksum(b"-- note\nSELECT  1 ;\n") == hashlib.sha256(b"SELECT 1;").hexdigest()


async def _versions(pool: AsyncConnectionPool) -> dict[int, str | None]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT version, checksum FROM ds.schema_version ORDER BY version")
        return dict(await cur.fetchall())  # type: ignore[arg-type]


@pytest.mark.db
async def test_every_applied_migration_has_its_files_checksum(empty_pool: AsyncConnectionPool) -> None:
    """A store migrated before 019 gets the checksums of this build's files for the versions it holds, as
    019 applies; a version applied after it, its own."""
    assert await migrate(empty_pool, upto=CHECKSUMS - 1) == list(range(1, CHECKSUMS))
    assert await migrate(empty_pool) == list(range(CHECKSUMS, NEWEST + 1))
    expected = {version: checksum(path.read_bytes()) for version, path in FILES.items()}
    assert await _versions(empty_pool) == expected


@pytest.mark.db
async def test_a_newer_database_is_refused_before_anything_runs(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM ds.schema_version WHERE version = %s", (NEWEST,))  # pending again
        await conn.execute(
            "INSERT INTO ds.schema_version (version, checksum) VALUES (%s, %s), (%s, %s)",
            (NEWEST + 1, "a" * 64, NEWEST + 7, "b" * 64),
        )
    held = await _versions(pool)
    message = f"the database holds migrations this build does not have: {NEWEST + 1}, {NEWEST + 7}"
    with pytest.raises(MigrationError, match=re.escape(message)) as refused:
        await migrate(pool)
    assert f"this build's newest is {NEWEST}" in str(refused.value)
    assert await _versions(pool) == held  # the pending one was not applied
    with pytest.raises(MigrationError, match=re.escape(message)):
        await db.pending(pool)


@pytest.mark.db
async def test_a_changed_statement_in_an_applied_file_is_refused(
    pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, files)
    monkeypatch.setattr(db, "MIGRATIONS", files)
    edited = files / FILES[17].name
    edited.write_text("-- reworded\n" + re.sub(r"[ \t]+", "  ", edited.read_text()) + "\n\n")
    assert await migrate(pool) == [] and await db.pending(pool) == []  # comments and layout only
    edited.write_text(re.sub(r"CREATE\s+INDEX", "CREATE UNIQUE INDEX", edited.read_text(), count=1))
    message = f"the applied migrations {FILES[17].name} differ from this build's files"
    for check in (migrate, db.pending):
        with pytest.raises(MigrationError, match=re.escape(message)):
            await check(pool)


@pytest.mark.db
async def test_each_migration_is_logged_as_it_starts_and_ends(
    empty_pool: AsyncConnectionPool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="deltasleuth.store.db"):
        assert await migrate(empty_pool, upto=2) == [1, 2]
        assert await migrate(empty_pool, upto=2) == []  # nothing pending: nothing said
    lines = [r.getMessage() for r in caplog.records if r.name == "deltasleuth.store.db"]
    assert [re.sub(r"[0-9.]+ s$", "N s", line) for line in lines] == [
        f"migration {FILES[1].name}: started",
        f"migration {FILES[1].name}: done in N s",
        f"migration {FILES[2].name}: started",
        f"migration {FILES[2].name}: done in N s",
        "migrations committed: 1, 2",
    ]


@pytest.mark.db
async def test_a_failed_migration_is_logged_and_applies_nothing(
    empty_pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    files = tmp_path / "migrations"
    files.mkdir()
    for version in (1, 2):
        shutil.copy(FILES[version], files)
    (files / "003_broken.sql").write_text("SELECT * FROM ds.nothing_here;\n")
    monkeypatch.setattr(db, "MIGRATIONS", files)
    with caplog.at_level(logging.INFO, logger="deltasleuth.store.db"), pytest.raises(psycopg.Error):
        await migrate(empty_pool)
    assert caplog.messages[-1].startswith("migration 003_broken.sql: failed after ")
    async with empty_pool.connection() as conn:
        assert await (await conn.execute("SELECT to_regclass('ds.runs')")).fetchone() == (None,)


@pytest.mark.db
async def test_only_a_context_asked_to_apply_migrations_applies_them(tmp_path: Path, pg_dsn: str) -> None:
    settings = load_settings(settings_file(tmp_path, pg_dsn))
    init_workspace(settings)
    with pytest.raises(ContextError, match=r"^the database needs migrations: run ds migrate$"):
        async with Context.open(settings, apply_migrations=False):
            pass
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('ds')").fetchone() == (None,)  # nothing was created
    async with Context.open(settings) as ctx:  # the services' open
        assert await db.pending(ctx.pool) == []
    async with Context.open(settings, apply_migrations=False) as ctx:  # nothing pending: it opens
        assert not ctx.pool.closed
