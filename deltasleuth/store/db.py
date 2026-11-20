"""Migrations: numbered SQL files, applied once each and recorded in ``ds.schema_version`` with the checksum
of their statements, on a database that holds no version this build lacks and no applied file this build
changed; and what the first connection of a process says of the database: an address or a refusal no
wait mends, and the pool's lines while it waits, each once."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
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
APPROVALS = 8  # 008_approvals: stored spec bodies are re-hashed as it applies
LICENCES = 10  # 010_licences: the installation id it makes is audited as it applies
SAMPLING = 11  # 011_sampling: sample draws, strata on run_keys, each cycle's judgement
NOTIFY = 12  # 012_notify: the notifier's cursor, deliveries and mutes
ACCURACY = 13  # 013_accuracy: labelled samples and their labeller's exclusion
READINESS = 14  # 014_readiness: each ended cycle's readiness measures
CHECKSUMS = 19  # 019_checksums: each applied migration's checksum, which every start compares with its file
LEGACY = "legacy plaintext remains: stop the app and run 'ds keys migrate'"
NEEDS_MIGRATION = "the database needs migrations: run ds migrate"
NEWER = (
    "the database holds migrations this build does not have: {versions} (this build's newest is {newest}):"
    " run the build that applied them, or restore a backup made before them"
)
CHANGED = (
    "the applied migrations {names} differ from this build's files in more than comments and layout: run the"
    " build that applied them, or restore a backup made before them"
)
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


_WORD = re.compile(r"[\w$]")  # characters of a name, a keyword or a number: a space keeps two apart
_OPERATOR = re.compile(r"[-+*/<>=~!@#%^&|`?:]")  # characters an operator is made of: so does a space


def checksum(sql: bytes) -> str:
    """The SHA-256 of a migration's statements: its text without comments and with whitespace only where it
    parts two words or two operator characters, every quoted literal and name kept as written. A change to a
    comment or to the layout keeps it; any other change does not. A dollar-quoted body is read as the code
    it is in these files (a function's or a DO block's), so its comments and layout are free too."""
    text, out, at, spaced = sql.decode(), [], 0, False
    while at < len(text):
        char = text[at]
        if text.startswith("--", at):
            at = text.find("\n", at)
            at, spaced = len(text) if at < 0 else at, True
        elif text.startswith("/*", at):  # they nest
            depth, at = 1, at + 2
            while depth and at < len(text):
                depth += text.startswith("/*", at) - text.startswith("*/", at)
                at += 2 if text.startswith(("/*", "*/"), at) else 1
            spaced = True
        elif char.isspace():
            at, spaced = at + 1, True
        else:
            if spaced and out and any(p.match(out[-1][-1]) and p.match(char) for p in (_WORD, _OPERATOR)):
                out.append(" ")
            spaced = False
            if char in "'\"":
                prefix = text[max(at - 2, 0) : at]  # E'…', not a name ending in e, takes backslash escapes
                escapes = char == "'" and prefix[-1:] in ("e", "E") and not _WORD.match(prefix[:-1] or " ")
                end = at + 1
                while end < len(text):
                    if escapes and text[end] == "\\":
                        end += 2
                    elif text[end] == char:
                        if text.startswith(char * 2, end):  # a doubled quote is one in the text
                            end += 2
                        else:
                            break
                    else:
                        end += 1
                out.append(text[at : end + 1])
                at = end + 1
            else:
                out.append(char)
                at += 1
    return hashlib.sha256("".join(out).encode()).hexdigest()


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


def _files() -> dict[int, Path]:
    """This build's migrations, by version, in order."""
    return {int(path.name.split("_", 1)[0]): path for path in sorted(MIGRATIONS.glob("*.sql"))}


async def _applied(conn: repo.Conn, files: dict[int, Path], *, legacy: bool = True) -> set[int]:
    """The versions the database holds. MigrationError when it holds one this build lacks (``NEWER``,
    naming them), or when the statements of an applied file differ from those it was applied with
    (``CHANGED``): this build does not know that schema, and would fail later inside a query. A version
    recorded without a checksum (by hand) is not compared. With ``legacy``, then ``LEGACY`` where 007
    would refuse: a store before 006 holding legacy plaintext, which only ``ds keys migrate`` mends."""
    cur = await conn.execute(
        "SELECT version, to_jsonb(v)->>'checksum' FROM ds.schema_version v ORDER BY version"
    )  # the column exists from 019 on
    held: dict[int, str | None] = dict(await cur.fetchall())  # type: ignore[arg-type]
    if unknown := [version for version in held if version not in files]:
        raise MigrationError(NEWER.format(versions=", ".join(map(str, unknown)), newest=max(files)))
    changed = [
        files[version].name
        for version, stored in held.items()
        if stored is not None and stored != checksum(files[version].read_bytes())
    ]
    if changed:
        raise MigrationError(CHANGED.format(names=", ".join(changed)))
    if legacy and held and SEALED - 1 not in held:
        query = _PLAINTEXT + (_SCAN_POINTS if 4 in held else b"")  # 004 added ds.scans
        if (await (await conn.execute(query)).fetchone())[0]:  # type: ignore[index]
            raise MigrationError(LEGACY)
    return set(held)


async def pending(pool: AsyncConnectionPool) -> list[int]:
    """This build's migrations the database has not applied, read without applying or creating anything;
    first ``_applied``'s MigrationError for a database this build cannot run on."""
    files = _files()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT to_regclass('ds.schema_version')")
        if (await cur.fetchone())[0] is None:  # type: ignore[index]
            return list(files)
        done = await _applied(conn, files)
    return [version for version in files if version not in done]


async def migrate(pool: AsyncConnectionPool, *, upto: int | None = None) -> list[int]:
    """Apply the pending migrations in order (those up to ``upto`` only, if given), in one
    transaction; return their numbers. First ``_applied``'s MigrationError, applying nothing, for a
    database this build cannot run on, or (without ``upto``) one 007 would refuse, before 006 runs. Each
    migration is logged as it starts and ends, and the commit once. Once the schema has an audit log that
    names actors, the transaction ends with ``system.migrate``, after ``spec.rehash`` when 008 found
    stored spec bodies that do not re-hash to their content hash and ``installation.created`` when 010
    made the installation id."""
    files = _files()
    async with repo.audited(pool) as (conn, audit):
        await lock_migrations(conn)
        await conn.execute(
            b"CREATE SCHEMA IF NOT EXISTS ds;"
            b" CREATE TABLE IF NOT EXISTS ds.schema_version"
            b" (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = await _applied(conn, files, legacy=upto is None)
        applied: list[int] = []
        for version, path in files.items():
            if version not in done and (upto is None or version <= upto):
                await _apply(conn, version, path, checksummed=CHECKSUMS in done or version >= CHECKSUMS)
                applied.append(version)
        if CHECKSUMS in applied:  # 019 fills in the versions applied before it, from this build's files
            await conn.execute(
                """UPDATE ds.schema_version v SET checksum = f.checksum
                   FROM unnest(%s::int[], %s::text[]) AS f (version, checksum)
                   WHERE v.version = f.version AND v.checksum IS NULL""",
                (list(files), [checksum(path.read_bytes()) for path in files.values()]),
            )
        rehashed = await _rehash(conn) if APPROVALS in applied else []
        if rehashed:
            await audit(MIGRATION, "spec.rehash", "specs", {"spec_ids": rehashed})
        if LICENCES in applied:
            cur = await conn.execute("SELECT installation_id FROM ds.installation")
            (made,) = await cur.fetchone()  # type: ignore[misc]
            await audit(MIGRATION, "installation.created", made, {"installation_id": made})
        if applied and applied[-1] >= AUDITED:
            await audit(MIGRATION, "system.migrate", "schema", {"versions": applied})
    if applied:
        log.info("migrations committed: %s", ", ".join(map(str, applied)))
    return applied


async def _apply(conn: repo.Conn, version: int, path: Path, *, checksummed: bool) -> None:
    """Run one migration in the caller's transaction and record it, with its checksum once 019 made the
    column; logged as it starts and as it ends or fails."""
    body, began = path.read_bytes(), time.monotonic()
    log.info("migration %s: started", path.name)
    try:
        try:  # no parameters: several statements run as one script
            await conn.execute(body)
        except psycopg.errors.RaiseException as exc:  # its guard: the whole transaction rolls back
            raise MigrationError(exc.diag.message_primary) from None
        if checksummed:
            await conn.execute(
                "INSERT INTO ds.schema_version (version, checksum) VALUES (%s, %s)", (version, checksum(body))
            )
        else:
            await conn.execute("INSERT INTO ds.schema_version (version) VALUES (%s)", (version,))
    except BaseException:
        log.error(
            "migration %s: failed after %.2f s; this run's migrations are rolled back",
            path.name, time.monotonic() - began,
        )  # fmt: skip
        raise
    log.info("migration %s: done in %.2f s", path.name, time.monotonic() - began)


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
