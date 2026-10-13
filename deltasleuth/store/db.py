"""Migrations: numbered SQL files, applied once each and recorded in ``ds.schema_version``; and what the
first connection of a process says of the database (docs/mvp/FIRSTRUN.md §8): an address or a refusal no
wait mends, and the pool's lines while it waits, each once."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool

from deltasleuth.auth.principal import MIGRATION
from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.store import repo

MIGRATIONS = Path(__file__).with_name("migrations")
_LOCK_ID = 0x44534D47  # pg advisory lock "DSMG": processes starting together migrate one at a time
AUDITED = 5  # from 005_identity on, the transaction that applies migrations audits them
SEALED = 7  # 007_sealed drops the plaintext columns; it refuses while legacy data remains
APPROVALS = 8  # 008_approvals: stored spec bodies are re-hashed as it applies (APPROVALS.md §7)
LICENCES = 10  # 010_licences: the installation id it makes is audited as it applies (LICENCE.md §5)
SAMPLING = 11  # 011_sampling: sample draws, strata on run_keys, each cycle's judgement (SAMPLING.md §12)
NOTIFY = 12  # 012_notify: the notifier's cursor, deliveries and mutes (docs/mvp/ALERTS.md §13)
ACCURACY = 13  # 013_accuracy: labelled samples and their labeller's exclusion (docs/mvp/ACCURACY.md §13)
READINESS = 14  # 014_readiness: each ended cycle's readiness measures (docs/mvp/READINESS.md §13)
LEGACY = "legacy plaintext remains: stop the app and run 'ds keys migrate' (docs/mvp/RETENTION.md §7.2)"
# 007's guard on a store before 006, where every raw key, header and chat message is in clear: checked
# first, so a refused start does not rewrite the spine with 006 only to roll it back.
_PLAINTEXT = b"""SELECT EXISTS (SELECT 1 FROM ds.run_keys) OR EXISTS (SELECT 1 FROM ds.observations)
                        OR EXISTS (SELECT 1 FROM ds.chat_messages)"""
_SCAN_POINTS = b" OR EXISTS (SELECT 1 FROM ds.scans WHERE jsonb_typeof(state->'after') = 'array')"
WAIT = 30.0  # seconds a process waits for its first connection
TRIES, APART = 3, 2.0  # a refusal after a password is tried again: a connection limit may free in 4 s
REFUSED = (
    "the database answered and asked for a password, then refused the connection: check the user, the"
    " password and the database name in the file database_dsn_ref names; if they are right, the server, the"
    " role or the database is at its connection limit, or a pooler cannot reach its server"
)
UNREADABLE = (
    "the file database_dsn_ref names does not hold an address the driver can read: percent-encode special"
    " characters in the user and the password"
)
log = logging.getLogger(__name__)


class MigrationError(ValueError):
    """A migration refused the data it found; the message says what to do."""


class DatabaseRefused(ValueError):
    """The database asked for a password and kept refusing the connection, or its address cannot be read:
    waiting for the pool mends neither."""


async def admitted(dsn: str) -> None:
    """Connect before the pool opens, and close at once. DatabaseRefused, never with the driver's text or
    a piece of the address: ``UNREADABLE``, before any connection, for an address the driver cannot parse
    or would split inside the user or the password; ``REFUSED`` when the server asked for a password and
    refused, at once where none was given, else after ``TRIES`` tries ``APART`` seconds apart: a
    connection limit that is full for a moment refuses a right password in the same way. Anything else
    returns: a database that does not answer is the pool's wait to report."""
    try:
        read = conninfo_to_dict(dsn)
    except (psycopg.ProgrammingError, UnicodeError):  # the driver's message quotes the address
        read = None
    # In a URI, an unencoded @ or / in the password turns its tail into the host or the database name
    # the driver reads, which connection errors name: neither holds an @ (a query's value may).
    if read is None or any("@" in str(read.get(part) or "") for part in ("host", "dbname")):
        raise DatabaseRefused(UNREADABLE)
    for left in reversed(range(TRIES)):
        try:
            await (await psycopg.AsyncConnection.connect(dsn, connect_timeout=5)).close()
            return
        except psycopg.OperationalError as exc:
            if exc.pgconn is None or not (exc.pgconn.used_password or exc.pgconn.needs_password):
                return
            if exc.pgconn.needs_password or not left:
                raise DatabaseRefused(REFUSED) from None
        except psycopg.Error:
            return
        await asyncio.sleep(APART)


@contextmanager
def opening() -> Iterator[None]:
    """While active, the ``psycopg.pool`` logger passes each distinct message once: the pool logs every
    failed try of its wait for the first connection, in the same words."""
    said: set[str] = set()

    def once(record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if message in said:
            return False
        said.add(message)
        return True

    pool = logging.getLogger("psycopg.pool")
    pool.addFilter(once)
    try:
        yield
    finally:
        pool.removeFilter(once)


async def migrate(pool: AsyncConnectionPool, *, upto: int | None = None) -> list[int]:
    """Apply the pending migrations in order (those up to ``upto`` only, if given), in one
    transaction; return their numbers. Once the schema has an audit log that names actors, the
    transaction ends with ``system.migrate``, after ``spec.rehash`` when 008 found stored spec bodies
    that do not re-hash to their content hash and ``installation.created`` when 010 made the
    installation id. MigrationError, applying nothing, when 007 would refuse:
    on a store before 006 holding legacy plaintext, before 006 runs."""
    async with repo.audited(pool) as (conn, audit):
        await lock_migrations(conn)
        await conn.execute(
            b"CREATE SCHEMA IF NOT EXISTS ds;"
            b" CREATE TABLE IF NOT EXISTS ds.schema_version"
            b" (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT version FROM ds.schema_version")
        done = {version for (version,) in await cur.fetchall()}
        if upto is None and done and SEALED - 1 not in done:
            query = _PLAINTEXT + (_SCAN_POINTS if 4 in done else b"")  # 004 added ds.scans
            if (await (await conn.execute(query)).fetchone())[0]:  # type: ignore[index]
                raise MigrationError(LEGACY)
        applied = []
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version not in done and (upto is None or version <= upto):
                try:  # no parameters: several statements run as one script
                    await conn.execute(path.read_bytes())
                except psycopg.errors.RaiseException as exc:  # its guard: the whole transaction rolls back
                    raise MigrationError(exc.diag.message_primary) from None
                await conn.execute("INSERT INTO ds.schema_version (version) VALUES (%s)", (version,))
                applied.append(version)
        rehashed = await _rehash(conn) if APPROVALS in applied else []
        if rehashed:
            await audit(MIGRATION, "spec.rehash", "specs", {"spec_ids": rehashed})
        if LICENCES in applied:
            cur = await conn.execute("SELECT installation_id FROM ds.installation")
            (made,) = await cur.fetchone()  # type: ignore[misc]
            await audit(MIGRATION, "installation.created", made, {"installation_id": made})
        if applied and applied[-1] >= AUDITED:
            await audit(MIGRATION, "system.migrate", "schema", {"versions": applied})
    return applied


async def _rehash(conn: repo.Conn) -> list[int]:
    """The specs whose stored body does not re-hash to their content hash (edited, or not storable as
    uploaded), each logged: oidc mode refuses to run them, and uploading the same file again maps to the
    same row, so a changed version (a new version) must be uploaded."""
    cur = await conn.execute(
        "SELECT spec_id, name, version, content_hash, body FROM ds.specs ORDER BY spec_id"
    )
    differ = []
    for spec_id, name, version, stored, body in await cur.fetchall():
        if content_hash(body) != stored:
            log.warning(
                "spec %s (%s %s): its stored body does not re-hash to its content hash; upload a changed"
                " version (a new version)",
                spec_id, name, version,
            )  # fmt: skip
            differ.append(spec_id)
    return differ


async def lock_migrations(conn: repo.Conn) -> None:
    """Hold the migration lock until ``conn``'s transaction ends: the start's key work takes it too."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
