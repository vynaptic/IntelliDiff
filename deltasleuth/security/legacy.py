"""What releases before 006 stored in clear, sealed offline by ``ds keys migrate`` (docs/mvp/RETENTION.md
§7.2).

The only reader of legacy data: the raw keys, headers, chat and scan points of those releases, and the
``blobs/sha256/`` blobs (zstd frames of plaintext bodies, named by their SHA-256). Holding the worker
lock on its own connection, so that no app runs meanwhile, it opens the store with the migrations up to
006 only, then works in batches of one transaction each that select legacy data only and clear the
plaintext as they seal it: interrupted anywhere, a rerun goes on where it stopped. A legacy blob is
rewritten at its ``hk1:`` address (a file written before is reused) before the observations naming it
are repointed, and goes once that commits; a reference to a blob already missing gets an address that
stays absent, and a rerun repoints it once the blob is back. References to legacy blobs where
``blobs/sha256/`` holds none (a wrong evidence directory) are refused before anything changes, unless
the operator accepts them all as missing. Last, what is left of ``blobs/sha256/`` is deleted and 007
applied. The module goes once no deployment predates 006.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import zstandard
from psycopg.types.json import Jsonb

from deltasleuth.auth.principal import Principal
from deltasleuth.config import Settings
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.secrets import read_secret
from deltasleuth.evidence.fsio import fsync_dir
from deltasleuth.evidence.store import EvidenceCorruptedError, EvidenceStore
from deltasleuth.runner.scan import seal_after
from deltasleuth.runner.worker import LOCK_ID
from deltasleuth.security.crypto import chat_context
from deltasleuth.store import db, repo

LEGACY = Path("blobs", "sha256")  # under the evidence directory
# Rows (or blobs) per transaction, by phase; each phase's count is audited under its name.
BATCHES = {
    "runs": 500,
    "keys": 5_000,
    "headers": 5_000,
    "chat_messages": 1_000,
    "scans": 100,
    "blobs": 500,
    "missing_blobs": 500,
}
INTERRUPTED = "interrupted: run 'ds keys migrate' again to continue"
_FILE = re.compile(r"([0-9a-f]{64})\.zst")
Rows = list[tuple[Any, ...]]
Phase = Callable[[Context], AsyncIterator[int]]


async def migrate(settings: Settings, who: Principal, *, accept_missing: bool = False) -> dict[str, int]:
    """Seal every legacy value and blob, then apply 007; the counts of this invocation, audited as
    ``keys.migrate`` at the end, or when Ctrl-C or an error stops it. ValueError, changing nothing,
    when observations name legacy blobs but ``blobs/sha256/`` holds none, unless ``accept_missing``."""
    counts = dict.fromkeys(BATCHES, 0)
    dsn = read_secret(settings.database_dsn_ref)
    await db.admitted(dsn)  # the lock's connection is the first: a refusal is said as by every command
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as lock:
        taken = (await (await lock.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))).fetchone())[0]
        if not taken:
            raise ValueError("stop the app first: docker compose stop app")
        if not accept_missing:  # before Context.open, which would already apply 006
            await _refuse_absent(lock, settings.evidence_dir)
        async with Context.open(settings, migrating=True) as ctx:
            try:
                await _seal(ctx, counts)
            except BaseException:
                with contextlib.suppress(Exception):  # the store may be what failed
                    await _audit(ctx, who, counts, complete=False)
                raise
            await _audit(ctx, who, counts, complete=True)
    return counts


async def _seal(ctx: Context, counts: dict[str, int]) -> None:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM ds.schema_version WHERE version = %s", (db.SEALED,))
        sealed = await cur.fetchone() is not None
    for name, phase in PHASES:
        if sealed and name not in ("blobs", "missing_blobs"):
            continue  # 007 dropped the plaintext columns: only legacy blobs can be left
        async for done in phase(ctx):
            counts[name] += done
    legacy = ctx.settings.evidence_dir / LEGACY
    if legacy.exists():
        await asyncio.to_thread(shutil.rmtree, legacy)
        fsync_dir(legacy.parent)
    await db.migrate(ctx.pool)


async def _refuse_absent(conn: psycopg.AsyncConnection, evidence_dir: Path) -> None:
    """Refuse while observations name legacy blobs but ``blobs/sha256/`` holds none and none was moved
    yet (no observation names an ``hk1:`` blob, as after a run interrupted once every file had moved):
    each would be recorded as missing, most likely because ``evidence_dir`` names the wrong directory.
    It runs on any schema from 001 on, before anything is migrated, so a refusal changes nothing."""
    legacy = evidence_dir / LEGACY
    [table] = await (await conn.execute("SELECT to_regclass('ds.observations')")).fetchone()  # type: ignore[misc]
    if table is None:  # an empty database: nothing to refuse
        return
    cur = await conn.execute(
        """SELECT EXISTS (SELECT 1 FROM ds.observations WHERE content_hash LIKE 'sha256:%')
                  AND NOT EXISTS (SELECT 1 FROM ds.observations WHERE content_hash LIKE 'hk1:%')"""
    )
    [named] = await cur.fetchone()  # type: ignore[misc]
    if named and not await asyncio.to_thread(_listed, legacy, 1):
        raise ValueError(
            f"observations name legacy blobs, but {legacy} holds none: check evidence_dir, or run 'ds keys"
            " migrate --accept-missing' to record every one as missing"
        )


async def _audit(ctx: Context, who: Principal, counts: dict[str, int], *, complete: bool) -> None:
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(who, "keys.migrate", "keys", counts | {"complete": complete})


def read_blob(evidence_dir: Path, digest: str, max_bytes: int = jsonio.DEFAULT_MAX_BYTES) -> bytes | None:
    """The body of legacy blob ``sha256:<digest>``, verified against the digest; None when its file is
    missing."""
    path = evidence_dir / LEGACY / digest[:2] / f"{digest}.zst"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    with contextlib.suppress(zstandard.ZstdError):
        # A declared size is required, so a tampered frame cannot make us allocate past the cap.
        if 0 <= zstandard.frame_content_size(data) <= max_bytes:
            body = zstandard.ZstdDecompressor().decompress(data)
            if hashlib.sha256(body).hexdigest() == digest:
                return body
    raise EvidenceCorruptedError(
        f"legacy blob {path} is corrupted: restore it from a backup, or delete it to record it as"
        " missing, then run 'ds keys migrate' again"
    )


def missing_address(evidence: EvidenceStore, content_hash: str) -> str:
    """Where a reference to a missing legacy blob points: an ``hk1:`` address no body has, so the blob
    stays absent."""
    return evidence.address(b"missing\x1f" + bytes.fromhex(content_hash.removeprefix("sha256:")))


# -- phases 1 to 5: rows ------------------------------------------------------------------------------


async def _batches(
    name: str,
    ctx: Context,
    *,
    first: tuple[Any, ...],
    select: LiteralString,
    seal: Callable[[Context, repo.Conn, Rows], Awaitable[None]],
) -> AsyncIterator[int]:
    """The rows ``select`` finds after a keyset position (its first columns; ``first`` to start), a
    batch at a time, each sealed by ``seal`` in its own transaction; yields the rows of each batch."""
    after = first
    while True:
        async with ctx.pool.connection() as conn:
            rows = await (await conn.execute(select, (*after, BATCHES[name]))).fetchall()
            if rows:
                await seal(ctx, conn, rows)
        if not rows:
            return
        yield len(rows)
        after = rows[-1][: len(first)]


def _column(rows: Rows, index: int) -> list[Any]:
    return [row[index] for row in rows]


async def _data_keys(ctx: Context, conn: repo.Conn, rows: Rows) -> None:
    for (run_id,) in rows:
        kek, wrapped, _ = ctx.new_sealer(run_id)
        await repo.insert_data_key(conn, run_id, kek, wrapped)


async def _keys(ctx: Context, conn: repo.Conn, rows: Rows) -> None:
    """As ``Sealer.seal_keys`` seals a run's keys."""
    sealed = [
        (await ctx.sealer(conn, run_id)).seal("run_key", 0, masked, key.encode())
        for run_id, _, _, masked, key in rows
    ]
    await conn.execute(
        """UPDATE ds.run_keys k SET key_enc = u.enc, key = NULL
           FROM unnest(%s::text[], %s::int[], %s::int[], %s::bytea[]) AS u (run_id, cycle, seq, enc)
           WHERE (k.run_id, k.cycle, k.seq) = (u.run_id, u.cycle, u.seq)""",
        (_column(rows, 0), _column(rows, 1), _column(rows, 2), sealed),
    )


async def _headers(ctx: Context, conn: repo.Conn, rows: Rows) -> None:
    """As ``compare.save_passes`` seals an observation's headers."""
    sealed = [
        (await ctx.sealer(conn, run_id)).seal(
            "headers", cycle, obs_id, jsonio.dumps(repo.storable(headers)).encode()
        )
        for run_id, obs_id, cycle, headers in rows
    ]
    await conn.execute(
        """UPDATE ds.observations o SET headers_enc = u.enc, headers = NULL
           FROM unnest(%s::text[], %s::text[], %s::bytea[]) AS u (run_id, obs_id, enc)
           WHERE (o.run_id, o.obs_id) = (u.run_id, u.obs_id)""",
        (_column(rows, 0), _column(rows, 1), sealed),
    )


async def _chat(ctx: Context, conn: repo.Conn, rows: Rows) -> None:
    """As ``repo.append_chat_message`` seals a message, in the cycle 006 gave it."""
    sealed = []
    for session_id, seq, role, cycle, content, tool_calls, run_id in rows:
        text = jsonio.dumps(repo.storable({"content": content, "tool_calls": tool_calls}))
        context = chat_context(session_id, seq, role)
        sealed.append((await ctx.sealer(conn, run_id)).seal("chat", cycle, context, text.encode()))
    await conn.execute(
        """UPDATE ds.chat_messages m SET sealed = u.enc, content = NULL, tool_calls = NULL
           FROM unnest(%s::text[], %s::int[], %s::bytea[]) AS u (session_id, seq, enc)
           WHERE (m.session_id, m.seq) = (u.session_id, u.seq)""",
        (_column(rows, 0), _column(rows, 1), sealed),
    )


async def _scans(ctx: Context, conn: repo.Conn, rows: Rows) -> None:
    """As ``scan.scan`` checkpoints the point a running scan resumes after."""
    params = [
        (Jsonb({"after": seal_after(await ctx.sealer(conn, run_id), cycle, after)}), run_id, cycle)
        for run_id, cycle, after in rows
    ]
    async with conn.cursor() as cur:
        await cur.executemany(
            "UPDATE ds.scans SET state = state || %s WHERE run_id = %s AND cycle = %s", params
        )


# -- phase 6: blobs -----------------------------------------------------------------------------------


async def _blobs(ctx: Context) -> AsyncIterator[int]:
    """Each legacy blob file, read, verified and written at its ``hk1:`` address, then the observations
    naming it repointed; the file goes once that commits."""
    legacy = ctx.settings.evidence_dir / LEGACY
    while digests := await asyncio.to_thread(_listed, legacy, BATCHES["blobs"]):
        async with ctx.pool.connection() as conn:
            await repo.lock_blobs(conn)  # a drain cannot delete a written blob before its references commit
            moved = await asyncio.to_thread(_written, ctx.evidence, ctx.settings.evidence_dir, digests)
            await _repoint(conn, ctx.evidence, moved)
        await asyncio.to_thread(_unlink, [legacy / digest[:2] / f"{digest}.zst" for digest in digests])
        yield len(digests)


async def _missing(ctx: Context) -> AsyncIterator[int]:
    """Once no legacy blob file is left, each reference still to one, pointed at ``missing_address``:
    its reads answer as for any missing blob."""
    while True:
        async with ctx.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT DISTINCT content_hash FROM ds.observations
                   WHERE content_hash LIKE 'sha256:%%' LIMIT %s""",
                (BATCHES["missing_blobs"],),
            )
            gone = [content_hash for (content_hash,) in await cur.fetchall()]
            await _repoint(conn, ctx.evidence, [(old, missing_address(ctx.evidence, old)) for old in gone])
        if not gone:
            return
        yield len(gone)


def _listed(legacy: Path, limit: int) -> list[str]:
    """Up to ``limit`` digests of the legacy blob files left, in order; temporary files and strangers
    are left to the final deletion."""
    digests: list[str] = []
    if not legacy.is_dir():
        return digests
    for directory in sorted(p for p in legacy.iterdir() if p.is_dir()):
        for name in sorted(p.name for p in directory.iterdir()):
            match = _FILE.fullmatch(name)
            if match and match[1][:2] == directory.name:
                digests.append(match[1])
                if len(digests) == limit:
                    return digests
    return digests


def _written(evidence: EvidenceStore, evidence_dir: Path, digests: Sequence[str]) -> list[tuple[str, str]]:
    """(the legacy address, the new one) of each blob, written again under the newest evidence key."""
    moved = []
    for digest in digests:
        body, old = read_blob(evidence_dir, digest), f"sha256:{digest}"
        moved.append((old, missing_address(evidence, old) if body is None else evidence.put_body(body)))
    return moved


async def _repoint(conn: repo.Conn, evidence: EvidenceStore, moved: Sequence[tuple[str, str]]) -> None:
    """Point the observations naming each legacy address, or its missing placeholder (recorded by an
    earlier run while the blob was absent), at the new address."""
    if moved:
        async with conn.cursor() as cur:
            await cur.executemany(
                "UPDATE ds.observations SET content_hash = %s WHERE content_hash IN (%s, %s)",
                [(new, old, missing_address(evidence, old)) for old, new in moved],
            )


def _unlink(paths: Sequence[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
    for directory in {path.parent for path in paths}:
        fsync_dir(directory)


PHASES: tuple[tuple[str, Phase], ...] = (
    (
        "runs",
        partial(
            _batches,
            "runs",
            first=("",),
            select="""SELECT run_id FROM ds.runs r
                      WHERE run_id > %s AND NOT EXISTS (SELECT 1 FROM ds.data_keys k WHERE k.scope = r.run_id)
                      ORDER BY run_id LIMIT %s""",
            seal=_data_keys,
        ),
    ),
    (
        "keys",
        partial(
            _batches,
            "keys",
            first=("", 0, -1),
            select="""SELECT run_id, cycle, seq, key_masked, key FROM ds.run_keys
                      WHERE (run_id, cycle, seq) > (%s, %s, %s) AND key_enc IS NULL
                      ORDER BY run_id, cycle, seq LIMIT %s""",
            seal=_keys,
        ),
    ),
    (
        "headers",
        partial(
            _batches,
            "headers",
            first=("", ""),
            select="""SELECT run_id, obs_id, cycle, headers FROM ds.observations
                      WHERE (run_id, obs_id) > (%s, %s) AND headers_enc IS NULL
                      ORDER BY run_id, obs_id LIMIT %s""",
            seal=_headers,
        ),
    ),
    (
        "chat_messages",
        partial(
            _batches,
            "chat_messages",
            first=("", 0),
            select="""SELECT m.session_id, m.seq, m.role, m.cycle, m.content, m.tool_calls, s.run_id
                      FROM ds.chat_messages m JOIN ds.chat_sessions s USING (session_id)
                      WHERE (m.session_id, m.seq) > (%s, %s) AND m.sealed IS NULL
                      ORDER BY m.session_id, m.seq LIMIT %s""",
            seal=_chat,
        ),
    ),
    (
        "scans",
        partial(
            _batches,
            "scans",
            first=("", 0),
            select="""SELECT run_id, cycle, state->'after' FROM ds.scans
                      WHERE (run_id, cycle) > (%s, %s) AND jsonb_typeof(state->'after') = 'array'
                      ORDER BY run_id, cycle LIMIT %s""",
            seal=_scans,
        ),
    ),
    ("blobs", _blobs),
    ("missing_blobs", _missing),
)
