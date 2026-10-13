"""Every query of DeltaSleuth: plain SQL, one async function per use case.

Each function takes an open connection (``async with ctx.pool.connection() as conn``; the pool
commits when the block exits cleanly, and ``audited`` is that block for a transaction that writes
audit rows) and returns dict rows. Keys are named by ``key_masked``. Raw keys, headers and chat are
sealed under the run's data key: rows carry ``key_enc``, ``headers_enc``
or ``sealed``, never a column named ``key``, ``headers`` or ``content``, so a caller must open them
with the run's ``Sealer.open_row``. Callers mask what they store (see 001_init.sql). Read functions
take ``cycle=None`` for the run's current cycle; paged ones return ``(rows, cursor)`` with an opaque
cursor, None on the last page, and raise ValueError on a malformed one.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Container, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, LiteralString

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from deltasleuth.auth.principal import Principal
from deltasleuth.connectors.base import utc_iso
from deltasleuth.core import jsonio
from deltasleuth.core.types import CompiledSpec, KeyResult, Observation, RootCause, Side
from deltasleuth.security.crypto import Sealer, chat_context

Conn = AsyncConnection[Any]
Row = dict[str, Any]
Page = tuple[list[Row], str | None]
Audit = Callable[..., Awaitable[Row]]

TERMINAL = ("cancelled", "completed", "completed_with_issues", "failed")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_OBSERVATION_FIELDS = (
    "obs_id", "key", "side", "http_status", "content_hash", "latency_ms", "attempts", "pages", "pages_eof",
    "pagination_unstable", "error_class", "headers",
)  # fmt: skip
# An observation row with its key's key_enc.
_OBSERVATION_COLUMNS: LiteralString = """o.run_id, o.obs_id, o.cycle, o.key_masked, o.side, o.pass_no,
    o.http_status, o.content_hash, o.latency_ms, o.attempts, o.pages, o.pages_eof, o.pagination_unstable,
    o.error_class, o.headers_enc, o.observed_at, k.key_enc"""
_BLOB_LOCK = 0x4453424C  # pg advisory lock "DSBL": blob writers (shared) against a blob drain (exclusive)
_PAIRS_LOCK = 0x44534C43  # pg advisory lock "DSLC": new pairs of licensed runs, and licence installs
_FINDING_COLUMNS = (
    "taxonomy_code", "severity", "change_type", "direction", "path_pattern", "json_pointer",
    "value_class", "member_key_tok", "signature_hash", "rule_id", "rule_classification", "suppressed",
)  # fmt: skip
_AUDIT_COLUMNS: LiteralString = (
    "id, at, actor, actor_name, action, object, detail, request_id, session_id, client_addr"
)
# The chain entry of the audit row ``a``; null for a row the trigger never saw.
_CHAIN_ENTRY: LiteralString = """(SELECT jsonb_build_object('seq', c.seq, 'hash', c.hash)
    FROM ds.audit_chain c WHERE c.id = a.id) AS chain"""
# A chain row with its audit row's action and columns as ``audit_<column>`` (null without one), which
# chain.py compares with the stored text and object: ds.audit_text could be replaced to agree with them.
_CHAIN_COLUMNS: LiteralString = """c.seq, c.id, c.prev, c.obj_prev, c.hash, c.object, c.text, l.action,
    l.id AS audit_id, l.at AS audit_at, l.actor AS audit_actor, l.actor_name AS audit_actor_name,
    l.action AS audit_action, l.object AS audit_object, l.detail AS audit_detail,
    l.request_id AS audit_request_id, l.session_id AS audit_session_id, l.client_addr AS audit_client_addr"""
_TOKEN_STATE: LiteralString = """CASE WHEN revoked_at IS NOT NULL THEN 'revoked'
    WHEN expires_at <= now() THEN 'expired' ELSE 'active' END AS state"""  # noqa: S105 - SQL
_TOKEN_COLUMNS: LiteralString = """token_id, name, owner, owner_user_id, role, created_by, created_at,
    expires_at, last_used_at, revoked_at, revoked_by"""  # noqa: S105 - SQL
_SESSION_COLUMNS: LiteralString = """session_id, user_id, token_id, actor, name, roles, client_addr,
    auth_time, acr, amr, created_at, last_seen_at, expires_at, revoked_at, revoked_by"""
# A key-list run with a table side whose current cycle lacks a key census of each table side
# (runner.census.pending says the same in Python). A hash sample's frame is its
# census: a key census would count every unsampled key missing.
_CENSUS_PENDING: LiteralString = """(r.mode <> 'one_shot'
    AND coalesce(r.params->>'keys_source', '') NOT IN ('table_scan', 'hash_sample')
    AND EXISTS (SELECT 1 FROM unnest(ARRAY['a', 'b']) AS s (side)
                WHERE r.params->'tables'->s.side IS NOT NULL
                  AND (r.census->>'cycle' IS DISTINCT FROM r.cycle::text
                       OR jsonb_typeof(r.census->s.side) IS DISTINCT FROM 'object')))"""
_siem = logging.getLogger("deltasleuth.audit")


def storable(value: Any) -> Any:
    """``value`` with NUL, which PostgreSQL text and jsonb cannot hold, as the visible text ``\\u0000``
    (a legacy side's NUL padding is data; its raw bytes stay in the evidence)."""
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, Mapping):
        return {storable(k): storable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [storable(v) for v in value]
    return value


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(storable(value), dumps=jsonio.dumps)  # exact Decimals


async def _rows(conn: Conn, sql: LiteralString, params: Any = None) -> list[Row]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def _row(conn: Conn, sql: LiteralString, params: Any = None) -> Row | None:
    rows = await _rows(conn, sql, params)
    return rows[0] if rows else None


async def _value(conn: Conn, sql: LiteralString, params: Any) -> Any:
    """The first column of the first row (a RETURNING value or a scalar); None without rows."""
    row = await (await conn.execute(sql, params)).fetchone()
    return row[0] if row else None


async def _changed(conn: Conn, sql: LiteralString, params: Any) -> int:
    return (await conn.execute(sql, params)).rowcount


def lost(conn: Conn, exc: BaseException) -> bool:
    """Whether ``exc`` is a lost connection, which nothing on ``conn`` outlives. Any other error, a
    statement the server cancelled or refused among them, ends only the savepoint it was raised in."""
    if conn.broken:
        return True
    return isinstance(exc, errors.Error) and (exc.sqlstate is None or exc.sqlstate.startswith("08"))


async def _current(conn: Conn, run_id: str, cycle: int | None) -> int | None:
    if cycle is not None:
        return cycle
    return await _value(conn, "SELECT cycle FROM ds.runs WHERE run_id = %s", (run_id,))


def _after(cursor: str | None, parts: int) -> list[int]:
    """The keyset position a cursor encodes; before everything when None."""
    if cursor is None:
        return [-1] * parts
    values = [int(part) for part in cursor.split(".")]
    if len(values) != parts:
        raise ValueError("malformed cursor")
    return values


def _page(rows: list[Row], limit: int, *cursor_fields: str) -> Page:
    """``rows`` holds one row past the page when there is a next page."""
    if len(rows) <= limit:
        return rows, None
    return rows[:limit], ".".join(str(rows[limit - 1][f]) for f in cursor_fields)


async def pages(fetch: Callable[..., Awaitable[Page]], *args: Any, **kwargs: Any) -> list[Row]:
    """Every row of a paged query (``fetch(*args, **kwargs)``), a thousand at a time."""
    rows, cursor = await fetch(*args, limit=1000, **kwargs)
    while cursor is not None:
        more, cursor = await fetch(*args, limit=1000, cursor=cursor, **kwargs)
        rows += more
    return rows


# -- specs ------------------------------------------------------------------------------------


async def save_spec(conn: Conn, spec: CompiledSpec, *, created_by: str) -> int:
    """Store a compiled spec once per content hash; return its spec_id (the existing one if known).
    ``created_by`` is kept from the first upload."""
    return await _value(
        conn,
        """INSERT INTO ds.specs (name, version, content_hash, body, created_by) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (content_hash) DO UPDATE SET content_hash = EXCLUDED.content_hash
           RETURNING spec_id""",
        (spec.name, spec.version, spec.content_hash, _jsonb(spec.raw), created_by),
    )


async def get_spec(conn: Conn, spec_id: int) -> Row | None:
    return await _row(conn, "SELECT * FROM ds.specs WHERE spec_id = %s", (spec_id,))


async def spec_by_hash(conn: Conn, content_hash: str) -> Row | None:
    """The stored version of that content, if any: a stored version is not saved again."""
    return await _row(conn, "SELECT * FROM ds.specs WHERE content_hash = %s", (content_hash,))


async def list_specs(conn: Conn) -> list[Row]:
    """Newest first, without bodies, each with ``approval``: its newest spec approval request
    (``approval_id``, ``status``, ``scopes``), or None."""
    return await _rows(
        conn,
        """SELECT s.spec_id, s.name, s.version, s.content_hash, s.created_at,
                  (SELECT jsonb_build_object('approval_id', a.approval_id, 'status', a.status,
                                             'scopes', a.scopes)
                   FROM ds.approvals a WHERE a.spec_id = s.spec_id AND a.kind = 'spec'
                   ORDER BY a.approval_id DESC LIMIT 1) AS approval
           FROM ds.specs s ORDER BY s.spec_id DESC""",
    )


# -- runs -------------------------------------------------------------------------------------


def _sortable_id(prefix: str) -> str:
    """``prefix`` + 26 Crockford base32 characters (microseconds, then 76 random bits): ids sort by age."""
    n = (time.time_ns() // 1000) << 76 | secrets.randbits(76)
    return prefix + "".join(_CROCKFORD[(n >> shift) & 31] for shift in range(125, -1, -5))


def new_run_id() -> str:
    """An ``R-`` id. A run's id comes before its row, as its sealed values are bound to it."""
    return _sortable_id("R-")


async def _insert_keys(
    conn: Conn,
    run_id: str,
    cycle: int,
    keys: Mapping[str, bytes],
    available_at: datetime | None,
    first_seq: int = 0,
    labels: Sequence[tuple[str | None, str | None, bool]] | None = None,
) -> None:
    """``labels``: a sample's (stratum, signoff_stratum, signoff_member) of each key, in order."""
    strata, signoffs, members = zip(*labels, strict=True) if labels else (None, None, None)
    await conn.execute(
        """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, available_at, stratum,
               signoff_stratum, signoff_member)
           SELECT %s, %s, k.masked, k.enc, %s + k.n - 1, coalesce(%s::timestamptz, now()), k.stratum,
               k.signoff, coalesce(k.member, false)
           FROM unnest(%s::text[], %s::bytea[], %s::text[], %s::text[], %s::boolean[])
               WITH ORDINALITY AS k (masked, enc, stratum, signoff, member, n)""",
        (
            run_id, cycle, first_seq, available_at, list(keys), list(keys.values()),
            None if strata is None else list(strata), None if signoffs is None else list(signoffs),
            None if members is None else list(members),
        ),
    )  # fmt: skip


async def create_run(
    conn: Conn,
    spec_id: int,
    mode: str,
    keys: Mapping[str, bytes],
    params: Mapping[str, Any],
    scan: Mapping[str, Any] | None = None,
    *,
    run_id: str,
    created_by: str,
    retention: str,
    data_key: tuple[str, bytes],
    approval_id: str | None = None,
    keys_cut: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Insert the queued run ``run_id`` (from ``new_run_id``) of retention class ``retention``, its data
    key (the KEK id and the key it wraps) and its cycle-1 keys (masked key -> key_enc, in manifest
    order); a table scan's run starts with no keys and its ``scan`` state. ``approval_id`` is the spec
    approval it starts under; ``keys_cut`` ``{resolved, taken}`` when maxKeys cut its key set. Its
    ``params`` as stored (jsonb keeps 1e16 as 10000000000000000, say), which a pack's plan hashes."""
    params = await _value(
        conn,
        """INSERT INTO ds.runs (run_id, spec_id, mode, params, created_by, retention, approval_id, keys_cut)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING params""",
        (
            run_id, spec_id, mode, _jsonb(params), created_by, retention, approval_id,
            None if keys_cut is None else _jsonb(keys_cut),
        ),
    )  # fmt: skip
    await insert_data_key(conn, run_id, *data_key)
    if scan is not None:
        await conn.execute(
            "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 1, %s)", (run_id, _jsonb(scan))
        )
    await _insert_keys(conn, run_id, 1, keys, None)
    return params


async def save_scan(
    conn: Conn,
    run_id: str,
    cycle: int,
    scan: Mapping[str, Any],
    keys: Mapping[str, bytes],
    saved_checkpoint: int,
) -> bool:
    """A checkpoint of the cycle's scan on checkpoint ``saved_checkpoint``: its state (``after``
    sealed), written as the next checkpoint, whose ``candidates`` counts ``keys`` (masked ->
    key_enc), and those keys; False, saving nothing, when the run is no longer running or that
    checkpoint is no longer the latest (another scanner saved)."""
    if not await _changed(
        conn,
        """UPDATE ds.scans sc SET state = %s FROM ds.runs r
           WHERE sc.run_id = %s AND sc.cycle = %s AND r.run_id = sc.run_id AND r.status = 'running'
             AND (sc.state->>'checkpoint')::int = %s""",
        (_jsonb(dict(scan) | {"checkpoint": saved_checkpoint + 1}), run_id, cycle, saved_checkpoint),
    ):
        return False
    await _insert_keys(conn, run_id, cycle, keys, None, first_seq=scan["candidates"] - len(keys))
    return True


async def get_scan(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The cycle's scan state; None when the cycle did not scan."""
    return await _value(conn, "SELECT state FROM ds.scans WHERE run_id = %s AND cycle = %s", (run_id, cycle))


async def run_scans(conn: Conn, run_id: str) -> dict[int, Row]:
    """Each scanned cycle's scan state, by cycle."""
    rows = await _rows(conn, "SELECT cycle, state FROM ds.scans WHERE run_id = %s", (run_id,))
    return {row["cycle"]: row["state"] for row in rows}


async def replace_graph_diffs(conn: Conn, run_id: str, cycle: int, rows: Sequence[Mapping[str, Any]]) -> None:
    """The cycle's graph parity rows, masked by the caller, replacing any before."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("DELETE FROM ds.graph_diffs WHERE run_id = %s AND cycle = %s", (run_id, cycle))
        await cur.executemany(
            """INSERT INTO ds.graph_diffs (run_id, cycle, seq, code, label, count_a, count_b, only_a, only_b,
                   detail)
               VALUES (%(run_id)s, %(cycle)s, %(seq)s, %(code)s, %(label)s, %(count_a)s, %(count_b)s,
                   %(only_a)s, %(only_b)s, %(detail)s)""",
            [
                row | {"run_id": run_id, "cycle": cycle, "seq": seq, "detail": _jsonb(row["detail"])}
                for seq, row in enumerate(rows)
            ],
        )


async def list_graph_diffs(
    conn: Conn, run_id: str, cycle: int | None = None, *, limit: int = 1000, cursor: str | None = None
) -> Page:
    """The cycle's graph parity rows in their order."""
    after = _after(cursor, 1)
    rows = await _rows(
        conn,
        """SELECT seq, code, label, count_a, count_b, only_a, only_b, detail FROM ds.graph_diffs
           WHERE run_id = %s AND cycle = %s AND seq > %s ORDER BY seq LIMIT %s""",
        (run_id, await _current(conn, run_id, cycle), after[0], limit + 1),
    )
    return _page(rows, limit, "seq")


async def unmatched_keys(conn: Conn, run_id: str, cycle: int) -> list[Row]:
    """The keys (key_masked, key_enc) whose result in the cycle is not MATCH."""
    return await _rows(
        conn,
        """SELECT k.key_masked, k.key_enc
           FROM ds.results r JOIN ds.run_keys k USING (run_id, cycle, key_masked)
           WHERE r.run_id = %s AND r.cycle = %s AND r.outcome <> 'MATCH'""",
        (run_id, cycle),
    )


async def get_run(conn: Conn, run_id: str) -> Row | None:
    """The run with its spec's ``spec_name`` and ``spec_version``, its current cycle's ``scan``, and
    ``created_by_name``, the creator's name."""
    return await _row(
        conn,
        """SELECT r.*, s.name AS spec_name, s.version AS spec_version, sc.state AS scan,
                  ds.actor_name(r.created_by) AS created_by_name
           FROM ds.runs r JOIN ds.specs s USING (spec_id)
           LEFT JOIN ds.scans sc ON sc.run_id = r.run_id AND sc.cycle = r.cycle
           WHERE r.run_id = %s""",
        (run_id,),
    )


async def list_runs(
    conn: Conn,
    *,
    spec_id: int | None = None,
    status: str | None = None,
    schedule_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Newest first, as ``get_run`` shows them but a file side's sealed pins; ``schedule_id``: the runs that
    schedule started (runs_schedule_due_idx)."""
    rows = await _rows(
        conn,
        """SELECT r.*, s.name AS spec_name, s.version AS spec_version, sc.state AS scan,
                  ds.actor_name(r.created_by) AS created_by_name
           FROM ds.runs r JOIN ds.specs s USING (spec_id)
           LEFT JOIN ds.scans sc ON sc.run_id = r.run_id AND sc.cycle = r.cycle
           WHERE (%(spec_id)s::bigint IS NULL OR r.spec_id = %(spec_id)s)
             AND (%(status)s::text IS NULL OR r.status = %(status)s)
             AND (%(schedule_id)s::text IS NULL OR (
                     r.params ? 'schedule' AND r.params->'schedule'->>'schedule_id' = %(schedule_id)s))
             AND (%(after)s::text IS NULL OR r.run_id < %(after)s)
           ORDER BY r.run_id DESC LIMIT %(limit)s""",
        {"spec_id": spec_id, "status": status, "schedule_id": schedule_id, "after": cursor,
         "limit": limit + 1},
    )  # fmt: skip
    for row in rows:  # a file side's sealed pins, about 200 bytes a file
        row["params"].pop("pinned", None)
    return _page(rows, limit, "run_id")


async def previous_run(conn: Conn, run_id: str) -> Row | None:
    """The latest completed run of the same spec created before ``run_id`` (the trend baseline)."""
    return await _row(
        conn,
        """SELECT * FROM ds.runs
           WHERE spec_id = (SELECT spec_id FROM ds.runs WHERE run_id = %(run_id)s)
             AND run_id < %(run_id)s AND status IN ('completed', 'completed_with_issues')
           ORDER BY run_id DESC LIMIT 1""",
        {"run_id": run_id},
    )


async def runs_reading(conn: Conn, target_id: str) -> int:
    """How many unfinished runs read the target: those whose spec names it as a side's, or as a key
    source's (``key.source`` and the sources nested in it)."""
    return await _value(
        conn,
        f"""SELECT count(*) FROM ds.runs r JOIN ds.specs s USING (spec_id)
            WHERE {_LIVE}
              AND (jsonb_path_exists(s.body, '$.sides.*.target ? (@ == $t)', %(vars)s)
                   OR jsonb_path_exists(s.body, '$.key.source.**.target ? (@ == $t)', %(vars)s))""",  # noqa: S608
        {"vars": _jsonb({"t": target_id})},
    )


async def set_run_status(
    conn: Conn,
    run_id: str,
    status: str,
    *,
    when: Sequence[str] | None = None,
    stop_reason: str | None = None,
    error: str | None = None,
    cleared: Sequence[str] = (),
) -> bool:
    """Move the run to ``status``, only from one of ``when`` if given; False when it did not move.

    started_at is set on the first ``running``, finished_at on a terminal status; a stop_reason
    or error, once set, is kept, unless the stop_reason is one of ``cleared`` (a pause's, on resume).
    """
    moved = await _changed(
        conn,
        """UPDATE ds.runs SET status = %(status)s, updated_at = now(),
               stop_reason = coalesce(
                   %(stop_reason)s, CASE WHEN stop_reason = ANY(%(cleared)s) THEN NULL ELSE stop_reason END),
               error = coalesce(%(error)s, error),
               started_at = CASE WHEN %(status)s = 'running' THEN coalesce(started_at, now())
                                 ELSE started_at END,
               finished_at = CASE WHEN %(status)s = ANY(%(terminal)s) THEN now() END
           WHERE run_id = %(run_id)s AND (%(when)s::text[] IS NULL OR status = ANY(%(when)s))""",
        {
            "run_id": run_id,
            "status": status,
            "when": None if when is None else list(when),
            "stop_reason": stop_reason,
            "error": error,
            "cleared": list(cleared),
            "terminal": list(TERMINAL),
        },
    )
    return moved == 1


async def set_keys_cut(conn: Conn, run_id: str, keys_cut: Mapping[str, int] | None) -> None:
    """How maxKeys cut the run's key set (``{resolved, taken}``, or a scan's ``{taken}``); None: uncut."""
    await conn.execute(
        "UPDATE ds.runs SET keys_cut = %s, updated_at = now() WHERE run_id = %s",
        (None if keys_cut is None else _jsonb(keys_cut), run_id),
    )


async def set_counters(conn: Conn, run_id: str, counters: Mapping[str, Any]) -> None:
    await conn.execute(
        "UPDATE ds.runs SET counters = %s, updated_at = now() WHERE run_id = %s", (_jsonb(counters), run_id)
    )


async def set_census(conn: Conn, run_id: str, census: Mapping[str, Any]) -> bool:
    """Record the census of the run's current cycle (``census["cycle"]``), merged
    into what that cycle recorded before; False when the run is in another cycle now."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.runs SET census = CASE WHEN census->>'cycle' = cycle::text THEN census || %(census)s
                                               ELSE %(census)s END
               WHERE run_id = %(run_id)s AND cycle = %(cycle)s""",
            {"census": _jsonb(census), "run_id": run_id, "cycle": census["cycle"]},
        )
    )


async def census_due(conn: Conn) -> list[str]:
    """The running runs whose current cycle awaits its key census."""
    rows = await _rows(
        conn,
        f"SELECT run_id FROM ds.runs r WHERE r.status = 'running' AND {_CENSUS_PENDING} ORDER BY run_id",  # noqa: S608
    )
    return [row["run_id"] for row in rows]


async def set_guard(conn: Conn, run_id: str, guard: Mapping[str, Any], *, cycle: int | None = None) -> bool:
    """Record what the schema guard holds of the run's tables, replacing it
    whole; with ``cycle``, only while the run is in that cycle. False when it is in another now."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.runs SET guard = %(guard)s
               WHERE run_id = %(run_id)s AND (%(cycle)s::int IS NULL OR cycle = %(cycle)s)""",
            {"guard": _jsonb(guard), "run_id": run_id, "cycle": cycle},
        )
    )


async def held_guard(conn: Conn, run_id: str) -> Row | None:
    """The run's ``cycle`` and ``guard`` as they stand, its row locked until the caller's transaction
    ends: of two looks of the schema guard at once, the second is judged on what the first recorded, so a
    state is recorded once."""
    return await _row(conn, "SELECT cycle, guard FROM ds.runs WHERE run_id = %s FOR UPDATE", (run_id,))


async def guard_basis(conn: Conn, spec_id: int, before: str) -> Row | None:
    """What the schema guard judges a new run of the version against (runs_spec_idx): the newest batch or
    soak run of ``spec_id`` created before the run ``before`` whose ``guard`` is a baseline, as its
    ``run_id``, ``guard`` and ``tables``. When no earlier run of the version is a baseline, the newest one
    that stored a plan (``tables``) and that the guard did not create paused, unaccepted: a run from before
    016 (its ``guard`` None), one the guard adopted, or one whose look at its creation failed. None with
    neither."""
    return await _row(
        conn,
        """SELECT r.run_id, r.guard, r.params->'tables' AS tables FROM ds.runs r
           WHERE r.spec_id = %(spec_id)s AND r.run_id < %(before)s AND r.mode <> 'one_shot'
             AND CASE WHEN EXISTS (SELECT 1 FROM ds.runs g WHERE g.spec_id = %(spec_id)s
                                     AND g.run_id < %(before)s AND g.mode <> 'one_shot'
                                     AND g.guard->'baseline' = 'true')
                      THEN r.guard->'baseline' = 'true'
                      ELSE jsonb_typeof(r.params->'tables') = 'object'
                           AND (r.guard IS NULL OR r.guard->'basis' = 'null' OR r.guard->'adopted' <> 'null')
                 END
           ORDER BY r.run_id DESC LIMIT 1""",
        {"spec_id": spec_id, "before": before},
    )


async def start_next_cycle(
    conn: Conn,
    run_id: str,
    available_at: datetime,
    keys: Mapping[str, bytes] | None = None,
    scan: Mapping[str, Any] | None = None,
) -> int:
    """Open the run's next cycle, due at ``available_at``, with ``keys`` (masked -> key_enc) or else
    the previous cycle's key set (a key_enc is sealed for every cycle) and labels, its retries reset, and
    ``scan`` as its scan's state; return the new cycle number."""
    cycle: int = await _value(
        conn,
        "UPDATE ds.runs SET cycle = cycle + 1, updated_at = now() WHERE run_id = %s RETURNING cycle",
        (run_id,),
    )
    if scan is not None:
        await conn.execute(
            "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, %s, %s)", (run_id, cycle, _jsonb(scan))
        )
    if keys is not None:
        await _insert_keys(conn, run_id, cycle, keys, available_at)
    else:
        await conn.execute(
            """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, available_at, stratum,
                   signoff_stratum, signoff_member)
               SELECT run_id, cycle + 1, key_masked, key_enc, seq, %s, stratum, signoff_stratum,
                   signoff_member
               FROM ds.run_keys WHERE run_id = %s AND cycle = %s""",
            (available_at, run_id, cycle - 1),
        )
    return cycle


# -- run keys ---------------------------------------------------------------------------------


async def lease_due(
    conn: Conn,
    owner: str,
    limit: int,
    lease_seconds: float,
    *,
    skip: Sequence[str] = (),
    run_id: str | None = None,
    census: bool = False,
) -> list[Row]:
    """Lease up to ``limit`` due keys of running runs, except the ``skip`` runs, to ``owner``,
    expired leases included.

    Concurrent callers never get the same key (SKIP LOCKED). Keys are looked up run by run, so
    the keys of paused runs are never scanned. ``run_id``: that run's keys only (a batch). ``census``
    (packs or readiness configured): no key of a run whose cycle awaits its key census. Rows: run_id,
    cycle, key_masked, key_enc, pass_no (1 for the first reads, 2+ for a recheck), signoff_member and
    retries (a sample's) and spec_id.
    """
    return await _rows(
        conn,
        f"""WITH due AS (
                SELECT k.run_id, k.cycle, k.key_masked, r.spec_id
                FROM ds.runs r CROSS JOIN LATERAL (
                    SELECT k.* FROM ds.run_keys k
                    WHERE k.run_id = r.run_id AND k.state IN ('pending', 'leased', 'recheck')
                      AND k.available_at <= now()
                    ORDER BY k.available_at, k.seq LIMIT %(limit)s FOR UPDATE SKIP LOCKED) k
                WHERE r.status = 'running' AND r.run_id <> ALL(%(skip)s::text[])
                  AND (%(run_id)s::text IS NULL OR r.run_id = %(run_id)s)
                  AND NOT coalesce(%(census)s AND {_CENSUS_PENDING}, false)
                ORDER BY k.available_at, k.seq LIMIT %(limit)s)
            UPDATE ds.run_keys k SET state = 'leased', lease_owner = %(owner)s,
                lease_until = now() + %(seconds)s * interval '1 second',
                available_at = now() + %(seconds)s * interval '1 second'
            FROM due WHERE (k.run_id, k.cycle, k.key_masked) = (due.run_id, due.cycle, due.key_masked)
            RETURNING k.run_id, k.cycle, k.key_masked, k.key_enc, k.pass_no, k.signoff_member, k.retries,
                due.spec_id""",  # noqa: S608
        {
            "owner": owner, "limit": limit, "seconds": lease_seconds, "skip": list(skip), "run_id": run_id,
            "census": census,
        },
    )  # fmt: skip


async def renew_lease(conn: Conn, owner: str, lease_seconds: float) -> bool:
    """Extend ``owner``'s leases (a key, or a group of one run) while its run is running. False: every
    lease was lost, or the run was paused or stopped meanwhile, so the keys must not be read now; a
    group that lost some is rolled back when saved."""
    renewed = await _changed(
        conn,
        """UPDATE ds.run_keys k SET lease_until = now() + %(seconds)s * interval '1 second',
               available_at = now() + %(seconds)s * interval '1 second'
           FROM ds.runs r
           WHERE r.run_id = k.run_id AND r.status = 'running' AND k.state = 'leased'
             AND k.lease_owner = %(owner)s""",
        {"owner": owner, "seconds": lease_seconds},
    )
    return renewed > 0


async def complete_key(conn: Conn, run_id: str, cycle: int, key_masked: str, owner: str) -> bool:
    """Mark a key ``owner`` holds done. False: the lease was lost, so roll back what was saved."""
    return await complete_keys(conn, run_id, cycle, [key_masked], owner) == 1


async def complete_keys(conn: Conn, run_id: str, cycle: int, keys_masked: Sequence[str], owner: str) -> int:
    """``complete_key`` for each key; how many ``owner`` still held."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys SET state = 'done', lease_owner = NULL, lease_until = NULL
           WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s) AND state = 'leased'
             AND lease_owner = %s""",
        (run_id, cycle, list(keys_masked), owner),
    )


async def complete_inline(conn: Conn, run_id: str, cycle: int, key_masked: str) -> bool:
    """Mark done a key read inline, never leased: a one-shot run's. False: it was cancelled."""
    done = await _changed(
        conn,
        """UPDATE ds.run_keys SET state = 'done'
           WHERE run_id = %s AND cycle = %s AND key_masked = %s AND state = 'pending'""",
        (run_id, cycle, key_masked),
    )
    return done == 1


async def schedule_recheck(
    conn: Conn, run_id: str, cycle: int, key_masked: str, owner: str, available_at: datetime
) -> bool:
    """Give up ``owner``'s lease and make the key due at ``available_at`` for its next pass."""
    return await schedule_rechecks(conn, run_id, cycle, {key_masked: available_at}, owner) == 1


async def schedule_rechecks(
    conn: Conn, run_id: str, cycle: int, due: Mapping[str, datetime], owner: str
) -> int:
    """``schedule_recheck`` for each masked key and its due time; how many ``owner`` still held."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys k SET state = 'recheck', pass_no = pass_no + 1, available_at = d.at,
               lease_owner = NULL, lease_until = NULL
           FROM unnest(%s::text[], %s::timestamptz[]) AS d (key_masked, at)
           WHERE k.run_id = %s AND k.cycle = %s AND k.key_masked = d.key_masked AND k.state = 'leased'
             AND k.lease_owner = %s""",
        (list(due), list(due.values()), run_id, cycle, owner),
    )


async def schedule_retries(
    conn: Conn, run_id: str, cycle: int, due: Mapping[str, datetime], owner: str
) -> int:
    """Give up ``owner``'s lease of each masked key and read it again from its first pass at its due time,
    one more retry, its stored result's reason appended to ``attrs.retried``;
    how many ``owner`` still held."""
    moved = await _rows(
        conn,
        """UPDATE ds.run_keys k SET state = 'pending', pass_no = 1, retries = retries + 1,
               available_at = d.at, lease_owner = NULL, lease_until = NULL
           FROM unnest(%s::text[], %s::timestamptz[]) AS d (key_masked, at)
           WHERE k.run_id = %s AND k.cycle = %s AND k.key_masked = d.key_masked AND k.state = 'leased'
             AND k.lease_owner = %s
           RETURNING k.key_masked""",
        (list(due), list(due.values()), run_id, cycle, owner),
    )
    await conn.execute(
        """UPDATE ds.results SET attrs = jsonb_set(attrs, '{retried}',
               coalesce(attrs->'retried', '[]') || to_jsonb(inconclusive_reason))
           WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s)""",
        (run_id, cycle, [row["key_masked"] for row in moved]),
    )
    return len(moved)


async def release_leases(conn: Conn, owner: str) -> int:
    """Hand back every key ``owner`` holds (a clean shutdown); return how many."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys SET state = CASE WHEN pass_no > 1 THEN 'recheck' ELSE 'pending' END,
               lease_owner = NULL, lease_until = NULL, available_at = now()
           WHERE state = 'leased' AND lease_owner = %s""",
        (owner,),
    )


async def cancel_keys(conn: Conn, run_id: str) -> int:
    """Cancel the run's unfinished keys, except those under a live lease; return how many."""
    return await _changed(
        conn,
        """UPDATE ds.run_keys SET state = 'cancelled', lease_owner = NULL, lease_until = NULL
           WHERE run_id = %s AND (state IN ('pending', 'recheck')
                                  OR (state = 'leased' AND lease_until <= now()))""",
        (run_id,),
    )


async def cycle_done(conn: Conn, run_id: str, cycle: int) -> bool:
    """True when every key of the cycle is done or cancelled, no running scan still adds keys and no
    running run's sample is still to be drawn (a stopped run ends its cycle undrawn)."""
    return await _value(
        conn,
        """SELECT NOT EXISTS (SELECT 1 FROM ds.run_keys WHERE run_id = %(run_id)s AND cycle = %(cycle)s
                                AND state IN ('pending', 'leased', 'recheck'))
              AND NOT EXISTS (SELECT 1 FROM ds.scans sc JOIN ds.runs r USING (run_id)
                              WHERE sc.run_id = %(run_id)s AND sc.cycle = %(cycle)s AND r.status = 'running'
                                AND NOT (sc.state->>'done')::boolean)
              AND NOT EXISTS (SELECT 1 FROM ds.runs WHERE run_id = %(run_id)s AND status = 'running'
                                AND draw IS NOT NULL)""",
        {"run_id": run_id, "cycle": cycle},
    )


async def analyse_new(conn: Conn) -> bool:
    """ANALYZE the tables a cycle end joins when one of them holds rows but no column statistics, as in a
    new store autovacuum has not analysed yet (its insert-driven VACUUM counts the rows, reltuples, but
    gathers no statistics): without them the planner takes a run's rows for one and joins each key to all
    of the run's findings and results, quadratic in differing keys. An
    analysed empty table has neither. Whether it analysed."""
    if not await _value(
        conn,
        """SELECT EXISTS (SELECT 1 FROM pg_class c
                  WHERE c.oid IN ('ds.run_keys'::regclass, 'ds.results'::regclass, 'ds.findings'::regclass,
                                  'ds.observations'::regclass)
                    AND c.reltuples <> 0
                    AND NOT EXISTS (SELECT 1 FROM pg_stats s WHERE s.schemaname = 'ds'
                                    AND s.tablename = c.relname))""",
        None,
    ):
        return False
    await conn.execute("ANALYZE ds.run_keys, ds.results, ds.findings, ds.observations")
    return True


async def cycle_counts(conn: Conn, run_id: str, cycle: int | None = None) -> dict[str, dict[str, int]]:
    """Progress of a cycle: ``{"keys": {state: n}, "outcomes": {outcome: n}}``."""
    rows = await _rows(
        conn,
        """SELECT 'keys' AS kind, state AS name, count(*) AS n FROM ds.run_keys
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s GROUP BY state
           UNION ALL
           SELECT 'outcomes', outcome, count(*) FROM ds.results
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s GROUP BY outcome""",
        {"run_id": run_id, "cycle": await _current(conn, run_id, cycle)},
    )
    counts: dict[str, dict[str, int]] = {"keys": {}, "outcomes": {}}
    for row in rows:
        counts[row["kind"]][row["name"]] = row["n"]
    return counts


async def list_keys(
    conn: Conn,
    run_id: str,
    cycle: int | None = None,
    *,
    state: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """A cycle's keys in manifest order: seq, key_masked, key_enc, state, pass_no."""
    rows = await _rows(
        conn,
        """SELECT seq, key_masked, key_enc, state, pass_no FROM ds.run_keys
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s AND seq > %(after)s
             AND (%(state)s::text IS NULL OR state = %(state)s)
           ORDER BY seq LIMIT %(limit)s""",
        {
            "run_id": run_id,
            "cycle": await _current(conn, run_id, cycle),
            "state": state,
            "after": _after(cursor, 1)[0],
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "seq")


# -- observations, results, findings ----------------------------------------------------------


async def lock_blobs(conn: Conn, *, exclusive: bool = False) -> None:
    """Hold the blob lock until the transaction ends: shared for a writer of observations, exclusive for
    a drain deleting unreferenced blobs."""
    if exclusive:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BLOB_LOCK,))
    else:
        await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (_BLOB_LOCK,))


async def insert_observation(
    conn: Conn, run_id: str, cycle: int, pass_no: int, key_masked: str, obs: Observation, headers_enc: bytes
) -> None:
    """Record an observation the evidence store holds (it set obs_id and content_hash) with its sealed
    headers; idempotent."""
    await insert_observations(conn, run_id, cycle, [(pass_no, key_masked, obs, headers_enc)])


async def insert_observations(
    conn: Conn, run_id: str, cycle: int, observations: Sequence[tuple[int, str, Observation, bytes]]
) -> None:
    """``insert_observation`` for each (pass_no, key_masked, observation, headers_enc), in one round trip."""
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.observations (run_id, cycle, pass_no, obs_id, key_masked, side, http_status,
                   content_hash, latency_ms, attempts, pages, pages_eof, pagination_unstable, error_class,
                   headers_enc, observed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, obs_id) DO NOTHING""",
            [
                (
                    run_id, cycle, pass_no, obs.obs_id, key_masked, obs.side.value, obs.http_status,
                    obs.content_hash, obs.latency_ms, obs.attempts, obs.pages, obs.pages_eof,
                    obs.pagination_unstable, obs.error_class, headers_enc, obs.observed_at,
                )
                for pass_no, key_masked, obs, headers_enc in observations
            ],
        )  # fmt: skip


def as_observation(row: Row) -> Observation:
    """An observation row, opened by its run's sealer, as the engine's Observation, without its body
    (``ctx.evidence.get_body(obs.content_hash)`` holds it)."""
    exact = {"side": Side(row["side"]), "observed_at": utc_iso(row["observed_at"])}
    return Observation(body=None, **{name: row[name] for name in _OBSERVATION_FIELDS} | exact)


async def get_observation(conn: Conn, run_id: str, obs_id: str) -> Row | None:
    return (await get_observations(conn, run_id, [obs_id])).get(obs_id)


async def get_observations(conn: Conn, run_id: str, obs_ids: Sequence[str]) -> dict[str, Row]:
    """The run's observation rows of ``obs_ids``, by obs_id, in one query."""
    rows = await _rows(
        conn,
        f"""SELECT {_OBSERVATION_COLUMNS}
            FROM ds.observations o JOIN ds.run_keys k USING (run_id, cycle, key_masked)
            WHERE o.run_id = %s AND o.obs_id = ANY(%s)""",  # noqa: S608 - fixed SQL fragments only
        (run_id, list(obs_ids)),
    )
    return {row["obs_id"]: row for row in rows}


async def list_observations(conn: Conn, run_id: str, cycle: int | None = None) -> list[Row]:
    """Every observation row of a cycle (all passes), in manifest order."""
    return await _rows(
        conn,
        f"""SELECT {_OBSERVATION_COLUMNS}
            FROM ds.observations o JOIN ds.run_keys k USING (run_id, cycle, key_masked)
            WHERE o.run_id = %s AND o.cycle = %s ORDER BY k.seq, o.pass_no, o.side""",  # noqa: S608
        (run_id, await _current(conn, run_id, cycle)),
    )


def _result_params(run_id: str, cycle: int, key_masked: str, r: KeyResult) -> dict[str, Any]:
    details = {f"side_{s}_detail": st.detail for s, st in (("a", r.side_a), ("b", r.side_b)) if st.detail}
    return {
        "run_id": run_id,
        "cycle": cycle,
        "key_masked": key_masked,
        "outcome": r.outcome.value,
        "side_a_state": r.side_a.kind.value,
        "side_a_reason": r.side_a.reason,
        "side_b_state": r.side_b.kind.value,
        "side_b_reason": r.side_b.reason,
        "inconclusive_reason": r.inconclusive_reason,
        "drift_reason": r.drift_reason.value if r.drift_reason else None,
        "confirmation": r.confirmation.value,
        "first_outcome": (r.first_outcome or r.outcome).value,
        "suppressed_count": r.suppressed_count,
        "obs_a_id": r.obs_a_id,
        "obs_b_id": r.obs_b_id,
        "attrs": _jsonb(r.attrs | details),
    }


def _finding_params(
    run_id: str, cycle: int, key_masked: str, seq: int, f: Mapping[str, Any], kept: dict[tuple[str, str], str]
) -> dict[str, Any]:
    """The finding's row; it takes the id ``kept`` holds for its masked pointer and change type."""
    a, b = f["value_a_tok"], f["value_b_tok"]
    columns = storable({name: f[name] for name in _FINDING_COLUMNS})
    return columns | {
        "run_id": run_id,
        "cycle": cycle,
        "key_masked": key_masked,
        "seq": seq,
        "finding_id": kept.pop((columns["json_pointer"], columns["change_type"]), None)
        or "F-" + secrets.token_hex(8),
        "value_a": None if a is None else _jsonb(a),
        "value_b": None if b is None else _jsonb(b),
    }


async def save_key_result(
    conn: Conn,
    run_id: str,
    cycle: int,
    key_masked: str,
    result: KeyResult,
    findings: Sequence[Mapping[str, Any]],
) -> int:
    """Upsert a key's result, by its masked key, and replace its findings in one transaction; return
    the revision.

    ``findings`` are ``security.tokenise.mask_finding`` rows of ``result.findings``, in order. A
    finding at the pointer and of the change type of one it replaces (a recheck) keeps its ``F-`` id,
    so a citation of it stays valid; any other gets a new one. Side-state details go to ``attrs``
    (side_a_detail, side_b_detail).
    """
    return (await save_key_results(conn, run_id, cycle, [(key_masked, result, findings)]))[0]


async def save_key_results(
    conn: Conn,
    run_id: str,
    cycle: int,
    items: Sequence[tuple[str, KeyResult, Sequence[Mapping[str, Any]]]],
    fresh: Container[str] = (),
) -> list[int]:
    """``save_key_result`` for each (key_masked, result, masked findings), with one statement per table.
    A key in ``fresh`` (masked) was read from its first pass again, a sample's retry: its result's first
    outcome is taken afresh. Every write keeps a result's ``attrs.retried``."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_a_reason,
                   side_b_state, side_b_reason, inconclusive_reason, drift_reason, confirmation,
                   first_outcome, suppressed_count, obs_a_id, obs_b_id, attrs)
               VALUES (%(run_id)s, %(cycle)s, %(key_masked)s, %(outcome)s, %(side_a_state)s,
                   %(side_a_reason)s, %(side_b_state)s, %(side_b_reason)s, %(inconclusive_reason)s,
                   %(drift_reason)s, %(confirmation)s, %(first_outcome)s, %(suppressed_count)s, %(obs_a_id)s,
                   %(obs_b_id)s, %(attrs)s)
               ON CONFLICT (run_id, cycle, key_masked) DO UPDATE SET
                   outcome = EXCLUDED.outcome,
                   side_a_state = EXCLUDED.side_a_state, side_a_reason = EXCLUDED.side_a_reason,
                   side_b_state = EXCLUDED.side_b_state, side_b_reason = EXCLUDED.side_b_reason,
                   inconclusive_reason = EXCLUDED.inconclusive_reason,
                   drift_reason = EXCLUDED.drift_reason, confirmation = EXCLUDED.confirmation,
                   first_outcome = CASE WHEN %(fresh)s THEN EXCLUDED.first_outcome
                                        ELSE ds.results.first_outcome END,
                   suppressed_count = EXCLUDED.suppressed_count,
                   obs_a_id = EXCLUDED.obs_a_id, obs_b_id = EXCLUDED.obs_b_id,
                   attrs = EXCLUDED.attrs || jsonb_strip_nulls(
                       jsonb_build_object('retried', ds.results.attrs->'retried')),
                   revision = ds.results.revision + 1, updated_at = now()
               RETURNING revision""",
            [
                _result_params(run_id, cycle, key_masked, result) | {"fresh": key_masked in fresh}
                for key_masked, result, _ in items
            ],
            returning=True,
        )
        revisions = []
        while True:
            revisions.append((await cur.fetchone())[0])  # type: ignore[index]
            if not cur.nextset():
                break
        await cur.execute(
            """DELETE FROM ds.findings WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s)
               RETURNING key_masked, json_pointer, change_type, finding_id""",
            (run_id, cycle, [key_masked for key_masked, _, _ in items]),
        )
        kept: dict[str, dict[tuple[str, str], str]] = {}
        for key_masked, pointer, change_type, finding_id in await cur.fetchall():
            kept.setdefault(key_masked, {})[(pointer, change_type)] = finding_id
        await cur.executemany(
            """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, taxonomy_code, severity,
                   change_type, direction, path_pattern, json_pointer, value_a, value_b, value_class,
                   member_key_tok, signature_hash, rule_id, rule_classification, suppressed)
               VALUES (%(run_id)s, %(cycle)s, %(key_masked)s, %(seq)s, %(finding_id)s, %(taxonomy_code)s,
                   %(severity)s, %(change_type)s, %(direction)s, %(path_pattern)s, %(json_pointer)s,
                   %(value_a)s, %(value_b)s, %(value_class)s, %(member_key_tok)s, %(signature_hash)s,
                   %(rule_id)s, %(rule_classification)s, %(suppressed)s)""",
            [
                _finding_params(run_id, cycle, key_masked, seq, f, kept.setdefault(key_masked, {}))
                for key_masked, _, findings in items
                for seq, f in enumerate(findings)
            ],
        )
    return revisions


async def unexplained_critical(conn: Conn, run_id: str) -> dict[str, int]:
    """Each key's unsuppressed CRITICAL findings in its latest result (any cycle), by masked key."""
    rows = await _rows(
        conn,
        """SELECT DISTINCT ON (r.key_masked) r.key_masked,
                  count(*) FILTER (WHERE f.severity = 'CRITICAL' AND NOT f.suppressed) AS critical
           FROM ds.results r LEFT JOIN ds.findings f USING (run_id, cycle, key_masked)
           WHERE r.run_id = %s GROUP BY r.key_masked, r.cycle ORDER BY r.key_masked, r.cycle DESC""",
        (run_id,),
    )
    return {r["key_masked"]: r["critical"] for r in rows}


async def unexplained_criticals(conn: Conn, run_id: str, cycle: int, bug_rules: Sequence[str]) -> int:
    """How many keys of the cycle have a CRITICAL finding that no rule explains: unsuppressed, or
    suppressed by one of ``bug_rules`` (the ids of the spec's ``classification: bug`` rules)."""
    return await _value(
        conn,
        """SELECT count(DISTINCT key_masked) FROM ds.findings
           WHERE run_id = %s AND cycle = %s AND severity = 'CRITICAL'
             AND (NOT suppressed OR rule_id = ANY(%s))""",
        (run_id, cycle, list(bug_rules)),
    )


async def get_key_detail(conn: Conn, run_id: str, key_masked: str, cycle: int | None = None) -> Row | None:
    """A key as its view shows it: the run_keys row (with key_enc) plus ``result`` (None until
    compared), ``findings`` and ``observations`` (every pass, with headers_enc)."""
    detail = await _row(
        conn,
        """SELECT key_masked, key_enc, cycle, seq, state, pass_no FROM ds.run_keys
           WHERE run_id = %s AND cycle = %s AND key_masked = %s""",
        (run_id, await _current(conn, run_id, cycle), key_masked),
    )
    if detail is None:
        return None
    where = (run_id, detail["cycle"], key_masked)
    detail["result"] = await _row(
        conn, "SELECT * FROM ds.results WHERE run_id = %s AND cycle = %s AND key_masked = %s", where
    )
    detail["findings"] = await _rows(
        conn,
        "SELECT * FROM ds.findings WHERE run_id = %s AND cycle = %s AND key_masked = %s ORDER BY seq",
        where,
    )
    detail["observations"] = await _rows(
        conn,
        f"""SELECT {_OBSERVATION_COLUMNS}
            FROM ds.observations o JOIN ds.run_keys k USING (run_id, cycle, key_masked)
            WHERE o.run_id = %s AND o.cycle = %s AND o.key_masked = %s
            ORDER BY o.pass_no, o.side""",  # noqa: S608 - fixed SQL fragments only
        where,
    )
    return detail


async def get_results(conn: Conn, run_id: str, cycle: int, keys_masked: Sequence[str]) -> dict[str, Row]:
    """The cycle's results of ``keys_masked``, by masked key, in one query."""
    rows = await _rows(
        conn,
        "SELECT * FROM ds.results WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s)",
        (run_id, cycle, list(keys_masked)),
    )
    return {row["key_masked"]: row for row in rows}


async def list_results(
    conn: Conn,
    run_id: str,
    cycle: int | None = None,
    *,
    outcome: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Results in manifest order, each with its key's ``seq``."""
    rows = await _rows(
        conn,
        """SELECT k.seq, r.*
           FROM ds.results r JOIN ds.run_keys k USING (run_id, cycle, key_masked)
           WHERE r.run_id = %(run_id)s AND r.cycle = %(cycle)s AND k.seq > %(after)s
             AND (%(outcome)s::text IS NULL OR r.outcome = %(outcome)s)
           ORDER BY k.seq LIMIT %(limit)s""",
        {
            "run_id": run_id,
            "cycle": await _current(conn, run_id, cycle),
            "outcome": outcome,
            "after": _after(cursor, 1)[0],
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "seq")


async def list_findings(
    conn: Conn,
    run_id: str,
    cycle: int | None = None,
    *,
    key_masked: str | None = None,
    root_cause_id: str | None = None,
    taxonomy_code: str | None = None,
    path_pattern: str | None = None,
    severity: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Findings in manifest then finding order, with their key's ``key_seq`` and their result's
    outcome, confirmation and observation ids. ``k.seq >= key_after`` lets a page seek its first key by
    index: the row comparison alone would join every earlier key again on each page."""
    key_after, finding_after = _after(cursor, 2)
    rows = await _rows(
        conn,
        """SELECT k.seq AS key_seq, r.outcome, r.confirmation, r.obs_a_id, r.obs_b_id, f.*
           FROM ds.findings f
           JOIN ds.run_keys k USING (run_id, cycle, key_masked)
           JOIN ds.results r USING (run_id, cycle, key_masked)
           WHERE f.run_id = %(run_id)s AND f.cycle = %(cycle)s AND k.seq >= %(key_after)s
             AND (k.seq, f.seq) > (%(key_after)s, %(finding_after)s)
             AND (%(key_masked)s::text IS NULL OR f.key_masked = %(key_masked)s)
             AND (%(root_cause_id)s::text IS NULL OR f.root_cause_id = %(root_cause_id)s)
             AND (%(taxonomy_code)s::text IS NULL OR f.taxonomy_code = %(taxonomy_code)s)
             AND (%(path_pattern)s::text IS NULL OR f.path_pattern = %(path_pattern)s)
             AND (%(severity)s::text IS NULL OR f.severity = %(severity)s)
           ORDER BY k.seq, f.seq LIMIT %(limit)s""",
        {
            "run_id": run_id,
            "cycle": await _current(conn, run_id, cycle),
            "key_after": key_after,
            "finding_after": finding_after,
            "key_masked": key_masked,
            "root_cause_id": root_cause_id,
            "taxonomy_code": taxonomy_code,
            "path_pattern": path_pattern,
            "severity": severity,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "key_seq", "seq")


# What a cycle's end and its reports read of a cycle (the four reads below) probes by key, also where the
# planner's statistics do not hold the cycle yet (a later cycle of a soak, a run small beside the store:
# autovacuum analyses a table only once a tenth of it changed) and it takes the cycle for one row. Written
# as plain joins, each then joined on (run_id, cycle) alone and read the whole cycle for every page or
# finding. So each reads its keys by their order (``seq``, the one index holding it), and each key's rows
# in a LATERAL subquery that ``OFFSET 0`` keeps from being flattened and that takes run and cycle from the
# key's row, never as constants: the cycle's constants are what the planner believes match one row, for
# which any index does.


async def result_pages(
    conn: Conn, run_id: str, cycle: int, *, size: int = 1000
) -> AsyncIterator[tuple[list[Row], dict[str, list[Row]]]]:
    """A cycle's results in manifest order, ``size`` at a time, each page with its keys' findings by
    masked key, in finding order: a cycle's end and its reports hold a page of it, never every row. A page
    walks the cycle's keys by their order from where the page before ended, reading each key's result by
    key, then walks its keys again for their findings."""
    after, more = -1, True
    while more:
        rows = await _rows(
            conn,
            """SELECT k.seq, r.*
               FROM ds.run_keys k CROSS JOIN LATERAL (
                   SELECT r.* FROM ds.results r
                   WHERE (r.run_id, r.cycle, r.key_masked) = (k.run_id, k.cycle, k.key_masked) OFFSET 0) r
               WHERE k.run_id = %s AND k.cycle = %s AND k.seq > %s ORDER BY k.seq LIMIT %s""",
            (run_id, cycle, after, size + 1),
        )
        results, more = rows[:size], len(rows) > size
        last = results[-1]["seq"] if results else after
        found = await _rows(
            conn,
            """SELECT f.*
               FROM ds.run_keys k CROSS JOIN LATERAL (
                   SELECT f.* FROM ds.findings f
                   WHERE (f.run_id, f.cycle, f.key_masked) = (k.run_id, k.cycle, k.key_masked) OFFSET 0) f
               WHERE k.run_id = %s AND k.cycle = %s AND k.seq > %s AND k.seq <= %s
               ORDER BY k.seq, f.seq""",
            (run_id, cycle, after, last),
        )
        findings: dict[str, list[Row]] = {}
        for f in found:
            findings.setdefault(f["key_masked"], []).append(f)
        yield results, findings
        after = last


async def discrepant_findings(
    conn: Conn, run_id: str, cycle: int, severities: Sequence[str], limit: int
) -> tuple[list[Row], int]:
    """The cycle's first ``limit`` discrepant findings (of ``severities``, and unsuppressed or suppressed
    by a ``bug`` rule), in the order of ``severities`` then in manifest and finding order, as
    ``list_findings`` gives them, and how many it has. The cycle's findings are read once, and the key and
    the result of each discrepant one by key."""
    rows = await _rows(
        conn,
        """SELECT k.seq AS key_seq, r.outcome, r.confirmation, r.obs_a_id, r.obs_b_id, f.*,
                  count(*) OVER () AS discrepant
           FROM ds.findings f
           CROSS JOIN LATERAL (
               SELECT k.seq FROM ds.run_keys k
               WHERE (k.run_id, k.cycle, k.key_masked) = (f.run_id, f.cycle, f.key_masked) OFFSET 0) k
           CROSS JOIN LATERAL (
               SELECT r.outcome, r.confirmation, r.obs_a_id, r.obs_b_id FROM ds.results r
               WHERE (r.run_id, r.cycle, r.key_masked) = (f.run_id, f.cycle, f.key_masked) OFFSET 0) r
           WHERE f.run_id = %(run_id)s AND f.cycle = %(cycle)s AND f.severity::text = ANY(%(severities)s)
             AND (NOT f.suppressed OR f.rule_classification = 'bug')
           ORDER BY array_position(%(severities)s, f.severity::text), k.seq, f.seq LIMIT %(limit)s""",
        {"run_id": run_id, "cycle": cycle, "severities": list(severities), "limit": limit},
    )
    return rows, max((row.pop("discrepant") for row in rows), default=0)


async def rechecks(conn: Conn, run_id: str, cycle: int, seqs: Sequence[int]) -> dict[str, dict[str, str]]:
    """key_masked -> side -> the obs_id of the side's last read after the first pass, for the keys at
    ``seqs`` (a result's ``seq``; at least one) that were read again: of its highest pass, and there the
    latest read (a sample member sent back to its first pass leaves the abandoned attempt's reads at the
    same passes). The keys from the first to the last of ``seqs`` are walked, and each one's reads read by
    key."""
    rows = await _rows(
        conn,
        """SELECT DISTINCT ON (k.seq, o.side) o.key_masked, o.side, o.obs_id
           FROM ds.run_keys k CROSS JOIN LATERAL (
               SELECT o.key_masked, o.side, o.obs_id, o.pass_no, o.observed_at FROM ds.observations o
               WHERE (o.run_id, o.cycle, o.key_masked) = (k.run_id, k.cycle, k.key_masked) AND o.pass_no > 1
               OFFSET 0) o
           WHERE k.run_id = %s AND k.cycle = %s AND k.seq BETWEEN %s AND %s AND k.seq = ANY(%s)
           ORDER BY k.seq, o.side, o.pass_no DESC, o.observed_at DESC, o.obs_id DESC""",
        (run_id, cycle, min(seqs), max(seqs), list(seqs)),
    )
    found: dict[str, dict[str, str]] = {}
    for row in rows:
        found.setdefault(row["key_masked"], {})[row["side"]] = row["obs_id"]
    return found


async def bug_rules(conn: Conn, run_id: str, cycle: int) -> dict[str, str]:
    """root_cause_id -> the ``bug`` rule that suppressed a finding linked to it (the last one in manifest
    and finding order), for the cycle's root causes that have one. Each such finding's key is read by
    key."""
    rows = await _rows(
        conn,
        """SELECT DISTINCT ON (f.root_cause_id) f.root_cause_id, f.rule_id
           FROM ds.findings f CROSS JOIN LATERAL (
               SELECT k.seq FROM ds.run_keys k
               WHERE (k.run_id, k.cycle, k.key_masked) = (f.run_id, f.cycle, f.key_masked) OFFSET 0) k
           WHERE f.run_id = %s AND f.cycle = %s AND f.root_cause_id IS NOT NULL AND f.suppressed
             AND f.rule_classification = 'bug'
           ORDER BY f.root_cause_id, k.seq DESC, f.seq DESC""",
        (run_id, cycle),
    )
    return {row["root_cause_id"]: row["rule_id"] for row in rows}


# -- root causes and soak changes -------------------------------------------------------------


def _cause_params(rc: RootCause) -> dict[str, Any]:
    return storable(
        dataclasses.asdict(rc)
        | {
            "change_type": rc.change_type.value,
            "direction": rc.direction.value,
            "max_severity": rc.max_severity.value,
        }
    )


async def replace_root_causes(
    conn: Conn, run_id: str, cycle: int, causes: Sequence[RootCause], finding_causes: Mapping[str, str]
) -> None:
    """Replace a cycle's root causes (in rank order) and point each finding at its cause
    (``finding_causes``: finding_id -> root_cause_id), in one transaction, which analyses the findings'
    ``root_cause_id`` again when its statistics hold under half the findings linked now: a new store's
    first cycle end analysed them before any was linked (``analyse_new``), the links may be too few for
    autovacuum to analyse again, and the planner would take the cycle's linked findings for one row."""
    params = {"run_id": run_id, "cycle": cycle}
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM ds.root_causes WHERE run_id = %(run_id)s AND cycle = %(cycle)s", params
        )
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO ds.root_causes (run_id, cycle, seq, root_cause_id, kind, taxonomy_code,
                       max_severity, path_pattern, change_type, direction, affected_keys, finding_count,
                       member_key_tok, value_class, signature_hash, sample_keys)
                   VALUES (%(run_id)s, %(cycle)s, %(seq)s, %(root_cause_id)s, %(kind)s, %(taxonomy_code)s,
                       %(max_severity)s, %(path_pattern)s, %(change_type)s, %(direction)s, %(affected_keys)s,
                       %(finding_count)s, %(member_key_tok)s, %(value_class)s, %(signature_hash)s,
                       %(sample_keys)s)""",
                [_cause_params(rc) | params | {"seq": seq} for seq, rc in enumerate(causes)],
            )
        await conn.execute(
            """UPDATE ds.findings SET root_cause_id = NULL
               WHERE run_id = %(run_id)s AND cycle = %(cycle)s AND root_cause_id IS NOT NULL""",
            params,
        )
        await conn.execute(
            """UPDATE ds.findings f SET root_cause_id = m.rc
               FROM unnest(%(ids)s::text[], %(rcs)s::text[]) AS m (id, rc)
               WHERE f.finding_id = m.id AND f.run_id = %(run_id)s AND f.cycle = %(cycle)s""",
            params | {"ids": list(finding_causes), "rcs": list(finding_causes.values())},
        )
        if finding_causes and await _value(
            conn,
            """SELECT (1 - s.null_frac) * c.reltuples * 2 < %s
               FROM pg_class c JOIN pg_stats s ON s.schemaname = 'ds' AND s.tablename = c.relname
               WHERE c.oid = 'ds.findings'::regclass AND s.attname = 'root_cause_id'""",
            (len(finding_causes),),
        ):
            await conn.execute("ANALYZE ds.findings (root_cause_id)")


async def list_root_causes(conn: Conn, run_id: str, cycle: int | None = None) -> list[Row]:
    """Most probable cause first."""
    return await _rows(
        conn,
        "SELECT * FROM ds.root_causes WHERE run_id = %s AND cycle = %s ORDER BY seq",
        (run_id, await _current(conn, run_id, cycle)),
    )


async def outcome_changes(conn: Conn, run_id: str) -> list[Row]:
    """Keys whose outcome differs from the previous cycle's: cycle, key_masked, from/to outcome."""
    return await _rows(
        conn,
        """SELECT cur.cycle, cur.key_masked, prev.outcome AS from_outcome, cur.outcome AS to_outcome
           FROM ds.results cur
           JOIN ds.results prev
             ON prev.run_id = cur.run_id AND prev.cycle = cur.cycle - 1 AND prev.key_masked = cur.key_masked
           JOIN ds.run_keys k ON (k.run_id, k.cycle, k.key_masked) = (cur.run_id, cur.cycle, cur.key_masked)
           WHERE cur.run_id = %s AND cur.outcome <> prev.outcome
           ORDER BY cur.cycle, k.seq""",
        (run_id,),
    )


# -- chat, LLM calls, audit -------------------------------------------------------------------


async def create_chat_session(
    conn: Conn, run_id: str, key_masked: str | None = None, *, created_by: str
) -> str:
    """A chat about a run, or about one of its keys; return the ``S-`` id."""
    session_id = "S-" + secrets.token_hex(8)
    await conn.execute(
        "INSERT INTO ds.chat_sessions (session_id, run_id, key, created_by) VALUES (%s, %s, %s, %s)",
        (session_id, run_id, key_masked, created_by),
    )
    return session_id


async def append_chat_message(
    conn: Conn,
    session_id: str,
    role: str,
    payload: Mapping[str, Any],
    sealer: Sealer,
    *,
    citations: Sequence[str] = (),
) -> int:
    """Append a message (role user: the question as typed; assistant; tool: a masked result), its
    ``payload`` ``{content, tool_calls}`` sealed by the run's ``sealer`` in the run's current cycle;
    return its seq (1, 2, ...)."""
    async with conn.transaction():
        # The session's row lock orders concurrent appends, so each one sees the previous seq.
        cycle = await _value(
            conn,
            """SELECT r.cycle FROM ds.chat_sessions s JOIN ds.runs r USING (run_id)
               WHERE s.session_id = %s FOR UPDATE OF s""",
            (session_id,),
        )
        if cycle is None:
            raise LookupError(f"no chat session {session_id}")
        seq = await _value(
            conn,
            "SELECT coalesce(max(seq), 0) + 1 FROM ds.chat_messages WHERE session_id = %s",
            (session_id,),
        )
        text = jsonio.dumps(storable(dict(payload)))  # a question or answer may quote a NUL-padded value
        await conn.execute(
            """INSERT INTO ds.chat_messages (session_id, seq, role, cycle, sealed, citations)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                session_id, seq, role, cycle,
                sealer.seal("chat", cycle, chat_context(session_id, seq, role), text.encode()),
                _jsonb(list(citations)),
            ),
        )  # fmt: skip
    return seq


async def get_chat_session(conn: Conn, session_id: str) -> Row | None:
    """The session, its masked ``key_masked`` (None for a run's chat) and its ``messages`` in order,
    each ``sealed``."""
    session = await _row(
        conn,
        """SELECT session_id, run_id, key AS key_masked, created_at, created_by FROM ds.chat_sessions
           WHERE session_id = %s""",
        (session_id,),
    )
    if session is not None:
        session["messages"] = await _rows(
            conn,
            """SELECT session_id, seq, role, cycle, sealed, citations, created_at FROM ds.chat_messages
               WHERE session_id = %s ORDER BY seq""",
            (session_id,),
        )
    return session


async def chat_answers(conn: Conn, run_id: str) -> list[Row]:
    """Every assistant answer given about the run, oldest first, ``sealed`` (the report's AI annex)."""
    return await _rows(
        conn,
        """SELECT s.session_id, s.key AS key_masked, m.seq, m.role, m.cycle, m.sealed, m.citations,
                  m.created_at
           FROM ds.chat_messages m JOIN ds.chat_sessions s USING (session_id)
           WHERE s.run_id = %s AND m.role = 'assistant'
           ORDER BY m.created_at, s.session_id, m.seq""",
        (run_id,),
    )


async def insert_llm_call(conn: Conn, session_id: str | None, call: Mapping[str, Any]) -> str:
    """Record one ``LLMClient.call_log`` entry; ``usage`` keeps its ``usage_*`` fields."""
    call_id = "L-" + secrets.token_hex(8)
    usage = {name.removeprefix("usage_"): value for name, value in call.items() if name.startswith("usage_")}
    micros = call.get("cost_micros")
    await conn.execute(
        """INSERT INTO ds.llm_calls (call_id, session_id, profile_id, model_reported, usage, cost,
               latency_ms, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            call_id,
            session_id,
            call["profile_id"],
            call.get("model_reported"),
            _jsonb(usage),
            None if micros is None else Decimal(micros) / 1_000_000,
            call.get("latency_ms"),
            call.get("denial_reason") or call.get("error_class"),
        ),
    )
    return call_id


# -- users, sessions, API tokens ---------------------------------------------


async def upsert_user(
    conn: Conn,
    issuer: str,
    subject: str,
    username: str,
    display_name: str | None,
    roles: Sequence[str] | None,
    *,
    signed_in: bool,
) -> Row:
    """The user of ``(issuer, subject)``, created or brought up to date; it carries ``previous_roles``
    (None for a new user). A row is written only when a name or the roles differ, or ``signed_in``
    (a successful sign-in sets ``last_login_at``; a disabled user's never does). ``roles`` None keeps
    the stored roles, ``{}`` for a new user: a groups overage tells nothing about them."""
    params = {
        "user_id": "U-" + secrets.token_hex(8),
        "issuer": issuer,
        "subject": subject,
        "username": username,
        "display_name": display_name,
        "roles": None if roles is None else sorted(roles),
        "signed_in": signed_in,
    }
    previous = await _row(
        conn, "SELECT * FROM ds.users WHERE issuer = %(issuer)s AND subject = %(subject)s", params
    )
    row = await _row(
        conn,
        """INSERT INTO ds.users AS u (user_id, issuer, subject, username, display_name, roles, last_login_at)
           VALUES (%(user_id)s, %(issuer)s, %(subject)s, %(username)s, %(display_name)s,
                   coalesce(%(roles)s::text[], '{}'), CASE WHEN %(signed_in)s THEN now() END)
           ON CONFLICT (issuer, subject) DO UPDATE SET
               username = EXCLUDED.username, display_name = EXCLUDED.display_name,
               roles = coalesce(%(roles)s::text[], u.roles),
               last_login_at = CASE WHEN %(signed_in)s AND u.disabled_at IS NULL THEN now()
                                    ELSE u.last_login_at END
           WHERE (%(signed_in)s AND u.disabled_at IS NULL) OR u.username <> EXCLUDED.username
              OR u.display_name IS DISTINCT FROM EXCLUDED.display_name
              OR u.roles <> coalesce(%(roles)s::text[], u.roles)
           RETURNING *""",
        params,
    )
    return (row or previous) | {"previous_roles": previous and previous["roles"]}  # type: ignore[operator]


async def get_user(conn: Conn, user_id: str) -> Row | None:
    return await _row(conn, "SELECT * FROM ds.users WHERE user_id = %s", (user_id,))


async def list_users(conn: Conn) -> list[Row]:
    """Every user with the roles of the last sign-in, by username; ``disabled_by_name`` beside
    ``disabled_by``."""
    return await _rows(
        conn,
        "SELECT *, ds.actor_name(disabled_by) AS disabled_by_name FROM ds.users ORDER BY username, user_id",
    )


async def set_user_disabled(conn: Conn, user_id: str, disabled_by: str | None) -> Row | None:
    """Disable the user (``disabled_by`` an actor) and revoke its sessions, or enable it (None); the
    row with ``sessions_revoked``. None when the user is unknown or already in that state."""
    row = await _row(
        conn,
        """UPDATE ds.users SET disabled_at = CASE WHEN %(by)s::text IS NULL THEN NULL ELSE now() END,
               disabled_by = %(by)s
           WHERE user_id = %(user_id)s AND (disabled_at IS NULL) = (%(by)s::text IS NOT NULL)
           RETURNING *""",
        {"user_id": user_id, "by": disabled_by},
    )
    if row is None:
        return None
    revoked = 0 if disabled_by is None else await revoke_sessions(conn, disabled_by, user_id=user_id)
    return row | {"sessions_revoked": revoked}


async def user_tokens(conn: Conn, user_id: str) -> list[Row]:
    """Every token the user created or owns, newest first: token_id, name, role, state."""
    return await _rows(
        conn,
        f"""SELECT token_id, name, role, {_TOKEN_STATE} FROM ds.api_tokens
            WHERE created_by = 'user:' || %(user_id)s OR owner_user_id = %(user_id)s
            ORDER BY created_at DESC, token_id""",  # noqa: S608 - fixed SQL fragments only
        {"user_id": user_id},
    )


async def create_session(
    conn: Conn,
    *,
    secret_hash: bytes,
    actor: str,
    name: str,
    roles: Sequence[str],
    max_hours: int,
    user_id: str | None = None,
    token_id: str | None = None,
    roles_sha256: str | None = None,
    client_addr: str | None = None,
    auth_time: datetime | None = None,
    acr: str | None = None,
    amr: Sequence[str] | None = None,
) -> Row:
    """A session with a fresh ``W-`` id for a user or a token, ending ``max_hours`` from now."""
    return await _row(  # type: ignore[return-value]
        conn,
        f"""INSERT INTO ds.sessions (session_id, secret_hash, user_id, token_id, actor, name, roles,
                roles_sha256, client_addr, auth_time, acr, amr, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + %s * interval '1 hour')
            RETURNING {_SESSION_COLUMNS}""",  # noqa: S608 - fixed SQL fragments only
        (
            "W-" + secrets.token_hex(8), secret_hash, user_id, token_id, actor, name, sorted(roles),
            roles_sha256, client_addr, auth_time, acr, None if amr is None else list(amr), max_hours,
        ),
    )  # fmt: skip


async def session_by_secret(
    conn: Conn, secret_hash: bytes, roles_sha256: str | None, *, idle_minutes: int
) -> Row | None:
    """The valid session of a cookie: not revoked, before its end and used within ``idle_minutes``,
    its user enabled and granted under the running role mapping (``roles_sha256``, oidc mode), or its
    token live and the server in tokens mode (no mapping), so a token's browser session ends when the
    deployment moves to SSO."""
    return await _row(
        conn,
        f"""SELECT {_SESSION_COLUMNS} FROM ds.sessions s
            WHERE secret_hash = %(hash)s AND revoked_at IS NULL AND expires_at > now()
              AND last_seen_at > now() - %(idle)s * interval '1 minute'
              AND (user_id IS NULL OR (roles_sha256 = %(roles_sha256)s AND EXISTS (
                  SELECT 1 FROM ds.users u WHERE u.user_id = s.user_id AND u.disabled_at IS NULL)))
              AND (token_id IS NULL OR %(roles_sha256)s::text IS NULL AND EXISTS (
                  SELECT 1 FROM ds.api_tokens t
                  WHERE t.token_id = s.token_id AND t.revoked_at IS NULL AND t.expires_at > now()))""",  # noqa: S608
        {"hash": secret_hash, "roles_sha256": roles_sha256, "idle": idle_minutes},
    )


async def revoke_session_secret(conn: Conn, secret_hash: bytes) -> None:
    """Revoke the unrevoked session of a cookie that no longer resolves (idle, or granted under another
    mode or role mapping), by its own actor, so a later settings change cannot revive it."""
    await conn.execute(
        """UPDATE ds.sessions SET revoked_at = now(), revoked_by = actor
           WHERE secret_hash = %s AND revoked_at IS NULL""",
        (secret_hash,),
    )


async def get_session(conn: Conn, session_id: str) -> Row | None:
    return await _row(
        conn,
        f"SELECT {_SESSION_COLUMNS} FROM ds.sessions WHERE session_id = %s",  # noqa: S608
        (session_id,),
    )


async def touch_session(conn: Conn, session_id: str) -> None:
    """Record the session's use, at most once a minute."""
    await conn.execute(
        """UPDATE ds.sessions SET last_seen_at = now()
           WHERE session_id = %s AND last_seen_at < now() - interval '60 seconds'""",
        (session_id,),
    )


async def revoke_sessions(
    conn: Conn,
    revoked_by: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    everyone: bool = False,
) -> int:
    """Revoke one live session, a user's, or ``everyone``'s; return how many."""
    if sum((session_id is not None, user_id is not None, everyone)) != 1:
        raise ValueError("revoke a session, a user's sessions or every session")
    return await _changed(
        conn,
        """UPDATE ds.sessions SET revoked_at = now(), revoked_by = %(by)s
           WHERE revoked_at IS NULL AND expires_at > now()
             AND (%(session_id)s::text IS NULL OR session_id = %(session_id)s)
             AND (%(user_id)s::text IS NULL OR user_id = %(user_id)s)""",
        {"by": revoked_by, "session_id": session_id, "user_id": user_id},
    )  # fmt: skip


async def list_sessions(conn: Conn) -> list[Row]:
    """The live sessions (not revoked, before their end), newest first."""
    return await _rows(
        conn,
        f"""SELECT {_SESSION_COLUMNS} FROM ds.sessions
            WHERE revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC, session_id""",  # noqa: S608
    )


async def delete_ended_sessions(conn: Conn) -> int:
    """Delete the sessions that ended (revoked or past their end) over 30 days ago; return how many."""
    return await _changed(
        conn,
        "DELETE FROM ds.sessions WHERE coalesce(revoked_at, expires_at) < now() - interval '30 days'",
        (),
    )


async def create_token(
    conn: Conn,
    *,
    token_id: str,
    name: str,
    owner: str,
    owner_user_id: str | None,
    role: str,
    secret_hash: bytes,
    created_by: str,
    days: int,
) -> Row:
    """Store a token (``auth.tokens.create`` makes one); its row without the hash."""
    return await _row(  # type: ignore[return-value]
        conn,
        f"""INSERT INTO ds.api_tokens (token_id, name, owner, owner_user_id, role, secret_hash, created_by,
                expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s * interval '1 day')
            RETURNING {_TOKEN_COLUMNS}, {_TOKEN_STATE}""",  # noqa: S608 - fixed SQL fragments only
        (token_id, name, owner, owner_user_id, role, secret_hash, created_by, days),
    )


async def token_by_id(conn: Conn, token_id: str) -> Row | None:
    """The token with its ``secret_hash`` (for ``auth.tokens.verify``) and ``state``."""
    return await _row(
        conn,
        f"SELECT {_TOKEN_COLUMNS}, secret_hash, {_TOKEN_STATE} FROM ds.api_tokens WHERE token_id = %s",  # noqa: S608
        (token_id,),
    )


async def touch_token(conn: Conn, token_id: str) -> None:
    """Record the token's use, at most once a minute."""
    await conn.execute(
        """UPDATE ds.api_tokens SET last_used_at = now()
           WHERE token_id = %s AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')""",
        (token_id,),
    )


async def list_tokens(conn: Conn) -> list[Row]:
    """Every token, newest first, with its ``state`` and the names of ``created_by`` and
    ``owner_user_id``; never the hash."""
    return await _rows(
        conn,
        f"""SELECT {_TOKEN_COLUMNS}, {_TOKEN_STATE}, ds.actor_name(created_by) AS created_by_name,
                ds.actor_name('user:' || owner_user_id) AS owner_user_name
            FROM ds.api_tokens ORDER BY created_at DESC, token_id""",  # noqa: S608
    )


async def revoke_token(conn: Conn, token_id: str, revoked_by: str) -> Row | None:
    """Revoke the token (its sessions end with it); its row, or None when it is unknown or already
    revoked."""
    return await _row(
        conn,
        f"""UPDATE ds.api_tokens SET revoked_at = now(), revoked_by = %s
            WHERE token_id = %s AND revoked_at IS NULL
            RETURNING {_TOKEN_COLUMNS}, 'revoked' AS state""",  # noqa: S608 - fixed SQL fragments only
        (revoked_by, token_id),
    )


async def revoke_user_tokens(conn: Conn, user_id: str, revoked_by: str) -> list[str]:
    """Revoke every live token the user created or owns; their ids."""
    rows = await _rows(
        conn,
        """UPDATE ds.api_tokens SET revoked_at = now(), revoked_by = %(by)s
           WHERE revoked_at IS NULL AND expires_at > now()
             AND (created_by = 'user:' || %(user_id)s OR owner_user_id = %(user_id)s)
           RETURNING token_id""",
        {"by": revoked_by, "user_id": user_id},
    )
    return sorted(row["token_id"] for row in rows)


# -- retention and purge --------------------------------------------

# A run of retention class r keeps a cycle's raw data this long after the cycle ends (days read at purge
# time: the queries take standard_days and audit_days).
_DAYS: LiteralString = """CASE r.retention WHEN 'audit' THEN %(audit_days)s::int
                                             ELSE %(standard_days)s::int END"""
_KEPT: LiteralString = f"make_interval(days => {_DAYS})"
_LIVE: LiteralString = "r.status IN ('queued', 'running', 'paused', 'cancelling')"
# One chunk of a cycle's keys (k), and a row (t) of one of them.
_CHUNK: LiteralString = "k.run_id = %(r)s AND k.cycle = %(c)s AND k.seq >= %(lo)s AND k.seq < %(hi)s"
_CHUNK_ROW: LiteralString = "(t.run_id, t.cycle, t.key_masked) = (k.run_id, k.cycle, k.key_masked)"


async def end_cycle(
    conn: Conn, run_id: str, ended_at: datetime | None = None, sample: Mapping[str, Any] | None = None
) -> None:
    """Record the end of the run's current cycle (``ended_at``, default now), with its counters and its
    census if they are the cycle's, the spec approval it ran under (none before
    ``params["approval"]["from_cycle"]``, set by ``set_run_approval``), how maxKeys cut it and a drawn
    cycle's ``sample`` judgement; the tombstone the retention clock starts from and a sign-off judges. A
    one-shot or captured-sample run's cycle is marked excluded from readiness; any other is measured by the
    worker. Once per cycle."""
    await conn.execute(
        """INSERT INTO ds.cycles (run_id, cycle, ended_at, counters, approval_id, keys_cut, census, sample,
               readiness)
           SELECT run_id, cycle, coalesce(%s::timestamptz, now()),
                  CASE WHEN counters->>'cycle' = cycle::text THEN counters ELSE '{}' END,
                  CASE WHEN cycle < coalesce((params->'approval'->>'from_cycle')::int, 0) THEN NULL
                       ELSE approval_id END,
                  keys_cut,
                  CASE WHEN census->>'cycle' = cycle::text THEN census END,
                  %s,
                  CASE WHEN mode = 'one_shot' THEN '{"format": 1, "excluded": "one_shot"}'::jsonb
                       WHEN params->>'samples_dir' IS NOT NULL
                           THEN '{"format": 1, "excluded": "captured_samples"}'::jsonb END
           FROM ds.runs WHERE run_id = %s
           ON CONFLICT DO NOTHING""",
        (ended_at, None if sample is None else _jsonb(sample), run_id),
    )


async def get_cycle(conn: Conn, run_id: str, cycle: int, *, lock: bool = False) -> Row | None:
    """The cycle's ``ds.cycles`` row; None while it is live. ``lock`` reads it FOR SHARE: a purge's start
    (``start_purge``) then waits for this transaction, or this read waits for it and finds it purging."""
    return await _row(
        conn,
        f"SELECT * FROM ds.cycles WHERE run_id = %s AND cycle = %s {'FOR SHARE' if lock else ''}",  # noqa: S608 - fixed SQL fragments only
        (run_id, cycle),
    )


async def list_cycles(conn: Conn, run_id: str, *, standard_days: int, audit_days: int) -> list[Row]:
    """The run's ended cycles in order, each with ``expires_at``, when its retention ends."""
    return await _rows(
        conn,
        f"""SELECT c.*, c.ended_at + {_KEPT} AS expires_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.run_id = %(run_id)s ORDER BY c.cycle""",  # noqa: S608 - fixed SQL fragments only
        {"run_id": run_id, "standard_days": standard_days, "audit_days": audit_days},
    )


async def due_cycles(
    conn: Conn, *, standard_days: int, audit_days: int, limit: int | None = None
) -> list[Row]:
    """The cycles to purge, an interrupted purge first: those whose retention ended, except a held
    run's and the cycles a live run still reads (its current one and the last ended)."""
    return await _rows(
        conn,
        f"""SELECT c.run_id, c.cycle, c.ended_at, r.retention, r.params->>'samples_dir' AS samples_dir,
                   c.purging_at, c.ended_at + {_KEPT} AS expires_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.purged_at IS NULL
              AND (c.purging_at IS NOT NULL
                   OR (r.hold_reason IS NULL AND c.ended_at + {_KEPT} <= now()
                       AND NOT ({_LIVE} AND c.cycle >= r.cycle - 1)))
            ORDER BY c.purging_at NULLS LAST, c.ended_at
            LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"standard_days": standard_days, "audit_days": audit_days, "limit": limit},
    )


async def kept_cycles(conn: Conn, *, standard_days: int, audit_days: int) -> list[Row]:
    """The cycles whose retention ended but that are not due: a held run's (``hold_reason`` set) and a
    live run's last ended cycle, which its next cycle still reads."""
    return await _rows(
        conn,
        f"""SELECT c.run_id, c.cycle, c.ended_at, r.retention, r.hold_reason, r.hold_by, r.hold_at,
                   c.ended_at + {_KEPT} AS expires_at
            FROM ds.cycles c JOIN ds.runs r USING (run_id)
            WHERE c.purged_at IS NULL AND c.purging_at IS NULL AND c.ended_at + {_KEPT} <= now()
              AND (r.hold_reason IS NOT NULL OR ({_LIVE} AND c.cycle >= r.cycle - 1))
            ORDER BY c.ended_at""",  # noqa: S608 - fixed SQL fragments only
        {"standard_days": standard_days, "audit_days": audit_days},
    )


async def abandoned_runs(conn: Conn, *, standard_days: int, audit_days: int) -> list[Row]:
    """Runs past the purge's ceiling: a one-shot run queued for 24 h, and any run paused or queued
    (since ``updated_at``) longer than its retention ``days``."""
    return await _rows(
        conn,
        f"""SELECT r.run_id, r.mode, r.status, r.updated_at, r.retention, {_DAYS} AS days
            FROM ds.runs r
            WHERE (r.mode = 'one_shot' AND r.status = 'queued'
                   AND r.updated_at <= now() - interval '24 hours')
               OR (r.status IN ('paused', 'queued') AND r.updated_at + {_KEPT} <= now())
            ORDER BY r.updated_at, r.run_id""",  # noqa: S608 - fixed SQL fragments only
        {"standard_days": standard_days, "audit_days": audit_days},
    )


async def start_purge(conn: Conn, run_id: str, cycle: int, reason: str) -> Row | None:
    """Mark the ended cycle purging (its reads answer 410 from now on) for ``reason``; an interrupted
    purge keeps its start and reason. The run's row is share-locked first, so a hold committed before
    is seen and one placed meanwhile waits for this transaction. The row; None when the cycle is purged
    or has not ended, or its run is held and its purge has not started."""
    held = await _value(
        conn, "SELECT hold_reason IS NOT NULL FROM ds.runs WHERE run_id = %s FOR SHARE", (run_id,)
    )
    return await _row(
        conn,
        """UPDATE ds.cycles SET purging_at = coalesce(purging_at, now()),
               purge_reason = coalesce(purge_reason, %s)
           WHERE run_id = %s AND cycle = %s AND purged_at IS NULL AND (purging_at IS NOT NULL OR NOT %s)
           RETURNING *""",
        (reason, run_id, cycle, bool(held)),
    )


async def key_span(conn: Conn, run_id: str, cycle: int) -> tuple[int, int] | None:
    """The lowest and highest ``seq`` of the cycle's keys; None when it has none."""
    row = await (
        await conn.execute(
            "SELECT min(seq), max(seq) FROM ds.run_keys WHERE run_id = %s AND cycle = %s", (run_id, cycle)
        )
    ).fetchone()
    return None if row is None or row[0] is None else (row[0], row[1])


async def purge_keys(conn: Conn, run_id: str, cycle: int, lo: int, hi: int) -> dict[str, int]:
    """Delete the cycle's keys of ``seq`` in [lo, hi) with their findings, results and observations, in
    that order, never through the cascade, and queue the blobs of the deleted observations for the GC
    (one queued already waits 24 h again); the rows deleted and blobs queued, by kind."""
    params = {"r": run_id, "c": cycle, "lo": lo, "hi": hi}
    done = {
        "findings": await _changed(
            conn,
            f"DELETE FROM ds.findings t USING ds.run_keys k WHERE {_CHUNK} AND {_CHUNK_ROW}",  # noqa: S608
            params,
        ),
        "results": await _changed(
            conn,
            f"DELETE FROM ds.results t USING ds.run_keys k WHERE {_CHUNK} AND {_CHUNK_ROW}",  # noqa: S608
            params,
        ),
    }
    row = await (
        await conn.execute(
            f"""WITH gone AS (DELETE FROM ds.observations t USING ds.run_keys k
                              WHERE {_CHUNK} AND {_CHUNK_ROW} RETURNING t.content_hash),
                queued AS (INSERT INTO ds.blob_gc (content_hash)
                           SELECT DISTINCT content_hash FROM gone WHERE content_hash IS NOT NULL
                           ON CONFLICT (content_hash) DO UPDATE SET queued_at = now()
                           RETURNING 1)
                SELECT (SELECT count(*) FROM gone), (SELECT count(*) FROM queued)""",  # noqa: S608
            params,
        )
    ).fetchone()
    done["observations"], done["blobs_queued"] = row  # type: ignore[misc]
    done["keys"] = await _changed(conn, f"DELETE FROM ds.run_keys k WHERE {_CHUNK}", params)  # noqa: S608
    return done


async def finish_purge(conn: Conn, run_id: str, cycle: int, purged_by: str) -> dict[str, Any]:
    """End the cycle's purge under the run's row lock: delete its root causes, graph rows, scan, chat
    messages and stored pack zips (their pack records stay, marked purged), tombstone its adjudications
    (their sheets, selected keys and disagreements go; hashes and counts stay) and its labellings (their
    sheets and drawn items go; hashes, counts, truth and accuracy stay) and mark it purged by
    ``purged_by``. A terminal run's chat sessions and data key go with its last unpurged cycle. The chat
    messages deleted, whether the data key was destroyed and the packs whose zip was deleted."""
    status = await _value(conn, "SELECT status FROM ds.runs WHERE run_id = %s FOR UPDATE", (run_id,))
    where = (run_id, cycle)
    await conn.execute("DELETE FROM ds.root_causes WHERE run_id = %s AND cycle = %s", where)
    await conn.execute("DELETE FROM ds.graph_diffs WHERE run_id = %s AND cycle = %s", where)
    await conn.execute("DELETE FROM ds.scans WHERE run_id = %s AND cycle = %s", where)
    await conn.execute(
        """DELETE FROM ds.pack_zips z USING ds.packs p
           WHERE p.pack_id = z.pack_id AND p.run_id = %s AND p.cycle = %s""",
        where,
    )
    packs = await _rows(
        conn,
        """UPDATE ds.packs SET purged_at = now() WHERE run_id = %s AND cycle = %s AND purged_at IS NULL
           RETURNING pack_id""",
        where,
    )
    await conn.execute(
        """UPDATE ds.adjudications SET purged_at = now(), sheet = NULL,
               selection = jsonb_set(selection, '{keys}', 'null'),
               score = jsonb_set(score, '{disagreements}', 'null')
           WHERE run_id = %s AND cycle = %s AND purged_at IS NULL""",
        where,
    )
    await conn.execute(
        """UPDATE ds.labellings SET purged_at = now(), sheet = NULL,
               selection = jsonb_set(selection, '{items}', 'null')
           WHERE run_id = %s AND cycle = %s AND purged_at IS NULL""",
        where,
    )
    messages = await _changed(
        conn,
        """DELETE FROM ds.chat_messages m USING ds.chat_sessions s
           WHERE s.session_id = m.session_id AND s.run_id = %s AND m.cycle = %s""",
        where,
    )
    last = status in TERMINAL and not await _value(
        conn,
        "SELECT EXISTS (SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle <> %s AND purged_at IS NULL)",
        where,
    )
    if last:
        await conn.execute("DELETE FROM ds.chat_sessions WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM ds.data_keys WHERE scope = %s", (run_id,))
    await conn.execute(
        "UPDATE ds.cycles SET purged_at = now(), purged_by = %s WHERE run_id = %s AND cycle = %s",
        (purged_by, *where),
    )
    return {
        "chat_messages": messages,
        "data_key_destroyed": last,
        "packs": sorted(row["pack_id"] for row in packs),
    }


async def keys_in_flight(conn: Conn, run_id: str, cycle: int | None = None) -> bool:
    """Whether a worker holds a live lease on one of the run's keys (of ``cycle`` only, if given)."""
    return await _value(
        conn,
        """SELECT EXISTS (SELECT 1 FROM ds.run_keys
                          WHERE run_id = %s AND (%s::int IS NULL OR cycle = %s)
                            AND state = 'leased' AND lease_until > now())""",
        (run_id, cycle, cycle),
    )


async def set_hold(conn: Conn, run_id: str, reason: str | None, by: str | None) -> Row | None:
    """Hold the run's evidence past its retention (``reason`` and ``by``), or release it (None); the
    run's hold columns as they were, None when the run is unknown or already in that state."""
    return await _row(
        conn,
        """UPDATE ds.runs r SET hold_reason = %(reason)s, hold_by = %(by)s,
               hold_at = CASE WHEN %(reason)s::text IS NULL THEN NULL ELSE now() END
           FROM ds.runs old
           WHERE r.run_id = %(run_id)s AND old.run_id = r.run_id
             AND (old.hold_reason IS NULL) = (%(reason)s::text IS NOT NULL)
           RETURNING old.hold_reason, old.hold_by, old.hold_at""",
        {"run_id": run_id, "reason": reason, "by": by},
    )


async def grace_over(conn: Conn, days: int) -> bool:
    """Whether 006 (retention) was applied at least ``days`` days ago."""
    return await _value(
        conn,
        """SELECT coalesce(bool_or(applied_at + make_interval(days => %s) <= now()), false)
           FROM ds.schema_version WHERE version = 6""",
        (days,),
    )


async def gc_due(conn: Conn, limit: int) -> list[str]:
    """Up to ``limit`` blob addresses queued over 24 h ago, oldest first."""
    rows = await _rows(
        conn,
        """SELECT content_hash FROM ds.blob_gc WHERE queued_at <= now() - interval '24 hours'
           ORDER BY queued_at LIMIT %s""",
        (limit,),
    )
    return [row["content_hash"] for row in rows]


async def referenced(conn: Conn, addresses: Sequence[str]) -> set[str]:
    """Those of ``addresses`` an observation still names."""
    rows = await _rows(
        conn,
        "SELECT DISTINCT content_hash FROM ds.observations WHERE content_hash = ANY(%s)",
        (list(addresses),),
    )
    return {row["content_hash"] for row in rows}


async def queued_blobs(conn: Conn, addresses: Sequence[str]) -> set[str]:
    """Those of ``addresses`` the GC queue holds."""
    rows = await _rows(
        conn, "SELECT content_hash FROM ds.blob_gc WHERE content_hash = ANY(%s)", (list(addresses),)
    )
    return {row["content_hash"] for row in rows}


async def gc_done(conn: Conn, addresses: Sequence[str]) -> None:
    await conn.execute("DELETE FROM ds.blob_gc WHERE content_hash = ANY(%s)", (list(addresses),))


async def blob_addresses(conn: Conn, after: str = "", limit: int = 10_000) -> list[str]:
    """The distinct blob addresses observations name, in order, after ``after``: a page of them."""
    rows = await _rows(
        conn,
        """SELECT DISTINCT content_hash FROM ds.observations
           WHERE content_hash IS NOT NULL AND content_hash > %s ORDER BY content_hash LIMIT %s""",
        (after, limit),
    )
    return [row["content_hash"] for row in rows]


# -- data keys ----------------------------------------------------------


async def data_keys(conn: Conn) -> list[Row]:
    """Every data key, oldest first, as ``wrapped`` by the KEK ``kek``."""
    return await _rows(
        conn,
        "SELECT scope, kek, wrapped, created_at, rewrapped_at FROM ds.data_keys ORDER BY created_at, scope",
    )


async def data_key(conn: Conn, scope: str) -> Row | None:
    return await _row(conn, "SELECT scope, kek, wrapped FROM ds.data_keys WHERE scope = %s", (scope,))


async def insert_data_key(conn: Conn, scope: str, kek: str, wrapped: bytes) -> None:
    await conn.execute(
        "INSERT INTO ds.data_keys (scope, kek, wrapped) VALUES (%s, %s, %s)", (scope, kek, wrapped)
    )


async def rewrap_data_key(conn: Conn, scope: str, kek: str, wrapped: bytes) -> None:
    await conn.execute(
        "UPDATE ds.data_keys SET kek = %s, wrapped = %s, rewrapped_at = now() WHERE scope = %s",
        (kek, wrapped, scope),
    )


# -- approvals -------------------------------------------------------------

# An approval with its spec's name, version and owner, its actors' names and how many people approve it:
# two when an author is unattributed (as stored at a step, else as the exclusion set stands now).
_APPROVAL_COLUMNS: LiteralString = """a.*, s.name AS spec_name, s.version AS spec_version,
    s.body->'metadata'->>'owner' AS spec_owner, ds.actor_name(a.requested_by) AS requested_by_name,
    ds.actor_name(a.first_by) AS first_by_name, ds.actor_name(a.decided_by) AS decided_by_name,
    ds.actor_name(a.revoked_by) AS revoked_by_name,
    CASE WHEN coalesce(a.unattributed, (ds.approval_authors(a)).unattributed) THEN 2 ELSE 1 END
        AS approvers_needed"""
# What each refusal of ds.tg_approvals says, and the check it names.
_TRIGGER_CHECKS = (
    ("is not an enabled rule_approver", "approver"),
    ("(four-eyes)", "four_eyes"),
    ("approver(s)", "approvers"),
    ("cites is not approved", "cited"),
)


class ApprovalRefused(Exception):
    """ds.tg_approvals refused a decision with its check_violation (23514, ``ds.approvals: …``), which
    the API answers 409, never 500. ``check`` names the check: ``approver`` (the decider is not an
    enabled rule_approver), ``four_eyes`` (the decider is in the exclusion set), ``approvers`` (the
    request needs another number of approvers) or ``cited`` (a sign-off's spec approval is not
    approved)."""

    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check


@asynccontextmanager
async def _trigger_checks() -> AsyncIterator[None]:
    """ApprovalRefused for the trigger's refusals; any other error passes."""
    try:
        yield
    except errors.CheckViolation as exc:
        message = exc.diag.message_primary or ""
        checks = [check for part, check in _TRIGGER_CHECKS if part in message]
        if not message.startswith("ds.approvals: ") or not checks:
            raise
        raise ApprovalRefused(checks[0], message) from None


async def create_approval(
    conn: Conn,
    kind: str,
    spec_id: int,
    requested_by: str,
    *,
    scopes: Sequence[str] = (),
    run_id: str | None = None,
    cycle: int | None = None,
    attests: Mapping[str, Any] | None = None,
    attests_sha256: str | None = None,
    note: str | None = None,
) -> Row | None:
    """A request (``A-`` id) to approve a spec version's content (its ``scopes``) or to sign off an ended
    cycle (its ``attests`` and their hash); None when the version or cycle has an open request already."""
    return await _row(
        conn,
        """INSERT INTO ds.approvals (approval_id, kind, spec_id, run_id, cycle, scopes, attests,
               attests_sha256, requested_by, request_note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT DO NOTHING RETURNING *""",
        (
            _sortable_id("A-"), kind, spec_id, run_id, cycle, list(scopes),
            None if attests is None else _jsonb(attests), attests_sha256, requested_by, note,
        ),
    )  # fmt: skip


async def get_approval(conn: Conn, approval_id: str, *, lock: bool = False) -> Row | None:
    """The approval with its spec's name, version and owner and its actors' names. ``lock`` reads it
    FOR SHARE: a revoke then waits for this transaction, or this read waits for the revoke and finds
    it revoked."""
    return await _row(
        conn,
        f"""SELECT {_APPROVAL_COLUMNS} FROM ds.approvals a JOIN ds.specs s USING (spec_id)
            WHERE a.approval_id = %s {"FOR SHARE OF a" if lock else ""}""",  # noqa: S608 - fixed SQL fragments only
        (approval_id,),
    )


async def active_spec_approval(conn: Conn, spec_id: int, *, lock: bool = False) -> Row | None:
    """The spec version's approved spec approval, if any; ``lock`` reads it FOR SHARE, as ``get_approval``."""
    return await _row(
        conn,
        f"""SELECT * FROM ds.approvals WHERE spec_id = %s AND kind = 'spec' AND status = 'approved'
            {"FOR SHARE" if lock else ""}""",  # noqa: S608 - fixed SQL fragments only
        (spec_id,),
    )


async def approval_history(
    conn: Conn, spec_id: int | None = None, *, run_id: str | None = None, cycle: int | None = None
) -> list[Row]:
    """Every request of a spec version (``spec_id``), or of an ended cycle (``run_id`` and ``cycle``),
    newest first, as ``get_approval`` shows them."""
    return await _rows(
        conn,
        f"""SELECT {_APPROVAL_COLUMNS} FROM ds.approvals a JOIN ds.specs s USING (spec_id)
            WHERE (%(run_id)s::text IS NULL AND a.kind = 'spec' AND a.spec_id = %(spec_id)s)
               OR (a.kind = 'signoff' AND a.run_id = %(run_id)s AND a.cycle = %(cycle)s)
            ORDER BY a.approval_id DESC""",  # noqa: S608 - fixed SQL fragments only
        {"spec_id": spec_id, "run_id": run_id, "cycle": cycle},
    )


async def list_approvals(
    conn: Conn,
    *,
    status: str | None = None,
    kind: str | None = None,
    spec_id: int | None = None,
    run_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Newest first, as ``get_approval`` shows them."""
    rows = await _rows(
        conn,
        f"""SELECT {_APPROVAL_COLUMNS} FROM ds.approvals a JOIN ds.specs s USING (spec_id)
            WHERE (%(status)s::text IS NULL OR a.status = %(status)s)
              AND (%(kind)s::text IS NULL OR a.kind = %(kind)s)
              AND (%(spec_id)s::bigint IS NULL OR a.spec_id = %(spec_id)s)
              AND (%(run_id)s::text IS NULL OR a.run_id = %(run_id)s)
              AND (%(after)s::text IS NULL OR a.approval_id < %(after)s)
            ORDER BY a.approval_id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {
            "status": status,
            "kind": kind,
            "spec_id": spec_id,
            "run_id": run_id,
            "after": cursor,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "approval_id")


async def approval_authors(conn: Conn, approval_id: str) -> Row | None:
    """The exclusion set the trigger computes for the approval's next step, as it stands now:
    ``persons`` who may not approve it, and whether an author is ``unattributed`` (then two
    approvers outside the set must)."""
    return await _row(
        conn,
        """SELECT x.persons, x.unattributed FROM ds.approvals a, ds.approval_authors(a) x
           WHERE a.approval_id = %s""",
        (approval_id,),
    )


async def uploads_by(conn: Conn, spec_id: int, actor: str) -> list[Row]:
    """The uploads that put ``actor`` in the spec version's lineage (ds.spec_accountable: every upload of
    the name's versions after its newest approved version before this one, up to this one), each the
    uploading ``actor`` (a person, or a token it created or owns) with its ``actor_name``, and ``base``,
    that approved version (None when there is none)."""
    return await _rows(
        conn,
        """WITH this AS (SELECT spec_id, name FROM ds.specs WHERE spec_id = %(spec_id)s),
           base AS (
               SELECT max(s.spec_id) AS id FROM ds.specs s JOIN this ON s.name = this.name
               WHERE s.spec_id < this.spec_id AND EXISTS (
                   SELECT 1 FROM ds.approvals a WHERE a.spec_id = s.spec_id AND a.kind = 'spec'
                                                  AND a.status = 'approved')),
           versions AS (
               SELECT s.spec_id, s.created_by FROM ds.specs s JOIN this ON s.name = this.name, base
               WHERE s.spec_id > coalesce(base.id, 0) AND s.spec_id <= this.spec_id),
           uploads AS (
               SELECT v.created_by AS actor FROM versions v
               UNION ALL
               SELECT l.actor FROM versions v
               JOIN ds.audit_log l ON l.object = v.spec_id::text AND l.action = 'spec.create')
           SELECT DISTINCT u.actor, ds.actor_name(u.actor) AS actor_name, base.id AS base
           FROM uploads u, base WHERE %(actor)s = ANY (ds.accountable(u.actor))
           ORDER BY u.actor""",
        {"spec_id": spec_id, "actor": actor},
    )


async def previous_approved(conn: Conn, spec_id: int) -> Row | None:
    """The name's newest version before the spec version with an approved spec approval: its spec_id,
    version, body and approval_id; None when there is none."""
    return await _row(
        conn,
        """SELECT s.spec_id, s.version, s.body, a.approval_id
           FROM ds.specs s JOIN ds.approvals a ON a.spec_id = s.spec_id AND a.kind = 'spec'
                                              AND a.status = 'approved'
           WHERE s.name = (SELECT name FROM ds.specs WHERE spec_id = %(spec_id)s) AND s.spec_id < %(spec_id)s
           ORDER BY s.spec_id DESC LIMIT 1""",
        {"spec_id": spec_id},
    )


async def accountable(conn: Conn, actor: str | None) -> Row:
    """Whom ``actor`` makes accountable (ds.accountable: a person itself; a token the person who created
    it and its owner) as ``persons``, and its ``name``."""
    return await _row(  # type: ignore[return-value]
        conn,
        "SELECT ds.accountable(%(actor)s) AS persons, ds.actor_name(%(actor)s) AS name",
        {"actor": actor},
    )


async def actor_names(conn: Conn, actors: Sequence[str]) -> dict[str, str]:
    """Each actor's name (ds.actor_name: a user's username, a token's name, else the actor itself)."""
    rows = await _rows(
        conn, "SELECT a, ds.actor_name(a) AS name FROM unnest(%s::text[]) AS a", (list(actors),)
    )
    return {row["a"]: row["name"] for row in rows}


async def decide_approval(
    conn: Conn, approval_id: str, by: str, *, approve: bool, first: bool = False, note: str | None = None
) -> Row | None:
    """``by``'s decision on an open request: approve it (``first``: as the first of two approvers, whose
    note is not kept), or reject it; the row as the trigger completed it. None when the request is no
    longer open (a first approval: or has one already). ApprovalRefused when the trigger refuses."""
    if approve and first:
        sql: LiteralString = """UPDATE ds.approvals SET first_by = %(by)s, first_at = now()
            WHERE approval_id = %(approval_id)s AND status = 'requested' AND first_by IS NULL RETURNING *"""
    else:
        sql = """UPDATE ds.approvals SET status = %(status)s, decided_by = %(by)s, decided_at = now(),
                     decision_note = %(note)s
                 WHERE approval_id = %(approval_id)s AND status = 'requested' RETURNING *"""
    status = "approved" if approve else "rejected"
    async with _trigger_checks():
        return await _row(conn, sql, {"approval_id": approval_id, "by": by, "note": note, "status": status})


async def revoke_approval(conn: Conn, approval_id: str, by: str, reason: str) -> Row | None:
    """Revoke an approved approval, taking its row lock; None when it is not approved."""
    return await _row(
        conn,
        """UPDATE ds.approvals SET status = 'revoked', revoked_by = %s, revoked_at = now(), revoke_reason = %s
           WHERE approval_id = %s AND status = 'approved' RETURNING *""",
        (by, reason, approval_id),
    )


async def approval_runs(conn: Conn, approval_id: str) -> list[Row]:
    """The running and queued runs that started, resumed or attached under the approval, locked, in
    order: run_id, mode, status."""
    return await _rows(
        conn,
        """SELECT run_id, mode, status FROM ds.runs
           WHERE approval_id = %s AND status IN ('running', 'queued') ORDER BY run_id FOR UPDATE""",
        (approval_id,),
    )


async def signoffs_citing(conn: Conn, approval_id: str) -> list[Row]:
    """The open and approved sign-offs citing the spec approval, locked, in order."""
    return await _rows(
        conn,
        """SELECT * FROM ds.approvals
           WHERE kind = 'signoff' AND attests->>'spec_approval_id' = %s
             AND status IN ('requested', 'approved')
           ORDER BY approval_id FOR UPDATE""",
        (approval_id,),
    )


async def release_signoff_hold(conn: Conn, run_id: str) -> str | None:
    """Release the run's hold a sign-off placed (``signed off: A-…``) once no approved sign-off of the
    run is left; the released reason, None when nothing was released."""
    return await _value(
        conn,
        """UPDATE ds.runs r SET hold_reason = NULL, hold_by = NULL, hold_at = NULL
           FROM ds.runs old
           WHERE r.run_id = %(run_id)s AND old.run_id = r.run_id AND old.hold_reason LIKE 'signed off: %%'
             AND NOT EXISTS (SELECT 1 FROM ds.approvals a
                             WHERE a.run_id = r.run_id AND a.kind = 'signoff' AND a.status = 'approved')
           RETURNING old.hold_reason""",
        {"run_id": run_id},
    )


async def set_run_approval(
    conn: Conn, run_id: str, approval_id: str | None, *, from_cycle: int | None = None
) -> None:
    """The spec approval the run resumed or attached under (None: none, as outside SSO). ``from_cycle``,
    when given, is the first cycle it covers (``params["approval"]["from_cycle"]``): ``end_cycle`` records
    none for an earlier one. The rest of ``params["approval"]`` stays as the run's creation wrote it."""
    await conn.execute(
        """UPDATE ds.runs SET approval_id = %(approval_id)s, updated_at = now(),
               params = CASE WHEN %(from_cycle)s::int IS NULL THEN params
                        ELSE jsonb_set(params, '{approval,from_cycle}', to_jsonb(%(from_cycle)s::int)) END
           WHERE run_id = %(run_id)s""",
        {"approval_id": approval_id, "from_cycle": from_cycle, "run_id": run_id},
    )


# -- sample draws -----------------------------------------------------
_DRAW_LOCK = 0x4453534D  # pg advisory lock "DSSM", with each population id: a live bottom_k run's draws
# Whether a finding of the key of the row ``k`` is under a bug rule, which makes its KNOWN_DIFF DIFFERENT.
# ``k`` is a row of cycle ``%(c)s`` of run ``%(r)s``: named, they keep the subquery to that cycle's findings
# (hashed over ``k``'s columns it would read every run's).
_BUG: LiteralString = """EXISTS (SELECT 1 FROM ds.findings f
                                WHERE f.run_id = %(r)s AND f.cycle = %(c)s AND f.key_masked = k.key_masked
                                  AND f.rule_id IS NOT NULL AND f.rule_classification = 'bug')"""


def new_draw_id() -> str:
    return _sortable_id("D-")


async def set_draw(conn: Conn, run_id: str, draw: Mapping[str, Any] | None) -> bool:
    """Set the run's pending draw (``{due[, error, at]}``), or clear it; False without the run."""
    return bool(
        await _changed(
            conn,
            "UPDATE ds.runs SET draw = %s, updated_at = now() WHERE run_id = %s",
            (None if draw is None else _jsonb(draw), run_id),
        )
    )


async def draws_due(conn: Conn) -> list[str]:
    """The running runs whose pending draw is due."""
    rows = await _rows(
        conn,
        """SELECT run_id FROM ds.runs WHERE status = 'running' AND draw ? 'due'
             AND (draw->>'due')::timestamptz <= now() ORDER BY run_id""",
    )
    return [row["run_id"] for row in rows]


async def insert_draw(
    conn: Conn,
    draw: Mapping[str, Any],
    keys: Mapping[str, bytes],
    labels: Sequence[tuple[str | None, str | None, bool]],
) -> Row:
    """Store a draw as of now() and then its keys (masked -> key_enc, in seq order) with their labels; the
    trigger refuses a draw not for a running run's current cycle with its draw pending, or after keys.
    ``draw``: draw_id, run_id, spec_id, cycle, method, population, salt_label, nonce, keys, frame, strata,
    selection_sha256. The stored row."""
    row = await _row(
        conn,
        """INSERT INTO ds.sample_draws (draw_id, run_id, spec_id, cycle, method, population, salt_label,
               nonce, drawn_at, keys, frame, strata, selection_sha256)
           VALUES (%(draw_id)s, %(run_id)s, %(spec_id)s, %(cycle)s, %(method)s, %(population)s,
               %(salt_label)s, %(nonce)s, now(), %(keys)s, %(frame)s, %(strata)s, %(selection_sha256)s)
           RETURNING *""",
        dict(draw) | {"population": list(draw["population"]), "frame": _jsonb(draw["frame"]),
                      "strata": _jsonb(draw["strata"])},
    )  # fmt: skip
    assert row is not None
    await _insert_keys(conn, row["run_id"], row["cycle"], keys, None, labels=labels)
    return row


async def get_draw(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The draw of the run's cycle; None when the cycle has none."""
    return await _row(conn, "SELECT * FROM ds.sample_draws WHERE run_id = %s AND cycle = %s", (run_id, cycle))


async def stratum_counts(conn: Conn, run_id: str, cycle: int) -> list[Row]:
    """The cycle's keys as a sample is judged, in one grouped query: ``n`` keys
    of each sign-off stratum, membership, whether done, whether retried, outcome, first outcome,
    confirmation, reasons (``truncated``: the sides an INCONCLUSIVE result's are truncated on, as ``a``,
    ``b`` or ``ab``, whatever its reason: a failed recheck of a truncated read too) and ``bug``, a finding
    under a bug rule; a key without a result has a NULL outcome. The only reader of the labels besides
    ``selection_lines``: no key leaves it."""
    return await _rows(
        conn,
        f"""SELECT stratum, member, done, retried, outcome, first_outcome, confirmation, inconclusive_reason,
                  drift_reason, truncated, bug, count(*) AS n
           FROM (SELECT k.signoff_stratum AS stratum, k.signoff_member AS member, k.state = 'done' AS done,
                        k.retries > 0 AS retried, r.outcome, r.first_outcome, r.confirmation,
                        r.inconclusive_reason, r.drift_reason,
                        CASE WHEN r.outcome = 'INCONCLUSIVE'
                                  AND 'truncated' IN (r.side_a_reason, r.side_b_reason) THEN
                            concat(CASE WHEN r.side_a_reason = 'truncated' THEN 'a' END,
                                   CASE WHEN r.side_b_reason = 'truncated' THEN 'b' END) END AS truncated,
                        {_BUG} AS bug
                 FROM ds.run_keys k LEFT JOIN ds.results r USING (run_id, cycle, key_masked)
                 WHERE k.run_id = %(r)s AND k.cycle = %(c)s) AS keys
           GROUP BY stratum, member, done, retried, outcome, first_outcome, confirmation, inconclusive_reason,
                    drift_reason, truncated, bug
           ORDER BY stratum, member, done, retried, outcome, first_outcome, confirmation, inconclusive_reason,
                    drift_reason, truncated, bug""",  # noqa: S608 - fixed SQL fragments only
        {"r": run_id, "c": cycle},
    )


async def live_sampled_runs(conn: Conn, population: Sequence[str], *, lock: bool = False) -> list[Row]:
    """The live runs (queued, running, paused or cancelling) of any spec drawing ``bottom_k`` samples of
    a population sharing an id with ``population``, with ``spec_name``. ``lock`` first takes the
    DSSM lock of each id, in sorted order, until the transaction ends: a run created under it is seen."""
    for member_id in sorted(population) if lock else ():
        await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_DRAW_LOCK, member_id))
    return await _rows(
        conn,
        f"""SELECT r.run_id, r.params->'population' AS population, s.name AS spec_name
            FROM ds.runs r JOIN ds.specs s USING (spec_id)
            WHERE {_LIVE} AND r.params->>'keys_source' = 'hash_sample'
              AND r.params->>'method' = 'bottom_k' AND r.params->'population' ?| %s::text[]
            ORDER BY r.run_id""",  # noqa: S608 - fixed SQL fragments only
        (list(population),),
    )


async def close_draws(conn: Conn, draw_ids: Sequence[str], by: str, note: str) -> list[Row]:
    """Close each of the draws not closed yet, as of now(), by ``by`` with ``note``; the closed
    rows. The trigger refuses a draw whose cycle has not ended."""
    return await _rows(
        conn,
        """UPDATE ds.sample_draws SET closed_by = %s, closed_at = now(), close_note = %s
           WHERE draw_id = ANY(%s) AND closed_at IS NULL RETURNING *""",
        (by, note, list(draw_ids)),
    )


async def selection_lines(
    conn: Conn, run_id: str, cycle: int
) -> AsyncIterator[tuple[int, str, str | None, str | None, bool]]:
    """The cycle's keys as ``sampling.line`` hashes them, in seq order, streamed (R-d re-hashes them)."""
    async with conn.cursor() as cur:
        async for row in cur.stream(
            """SELECT seq, key_masked, stratum, signoff_stratum, signoff_member FROM ds.run_keys
               WHERE run_id = %s AND cycle = %s ORDER BY seq""",
            (run_id, cycle),
        ):
            yield row


# A draw as its ledger lists it, never its nonce: the chain seqs of its sample.draw and sample.close rows,
# its spec's name and its status: live (its cycle has not ended), unjudged (it ended without a judgement),
# else the stored judgement's.
_LEDGER: LiteralString = """SELECT d.draw_id, d.run_id, d.spec_id, s.name AS spec_name, d.cycle, d.method,
        d.population, d.salt_label, d.drawn_at, d.keys, d.frame, d.strata, d.selection_sha256, d.closed_by,
        ds.actor_name(d.closed_by) AS closed_by_name, d.closed_at, d.close_note, c.ended_at,
        CASE WHEN c.run_id IS NULL THEN 'live' WHEN c.sample IS NULL THEN 'unjudged'
             ELSE c.sample->>'status' END AS status,
        ARRAY(SELECT st->>'label' FROM jsonb_array_elements(coalesce(c.sample->'strata', '[]')) st
              WHERE st->>'verdict' = 'failed') AS failed_strata,
        (SELECT h.seq FROM ds.audit_log l JOIN ds.audit_chain h ON h.id = l.id
         WHERE l.object = d.run_id AND l.action = 'sample.draw'
           AND l.detail->>'draw_id' = d.draw_id) AS draw_seq,
        (SELECT h.seq FROM ds.audit_log l JOIN ds.audit_chain h ON h.id = l.id
         WHERE l.object = d.draw_id AND l.action = 'sample.close') AS close_seq
    FROM ds.sample_draws d JOIN ds.specs s USING (spec_id) LEFT JOIN ds.cycles c USING (run_id, cycle)"""


async def ledger(conn: Conn, population: Sequence[str]) -> list[Row]:
    """Every draw, of any spec, of a population sharing an id with ``population``, in commit order."""
    return await _rows(
        conn,
        f"{_LEDGER} WHERE d.population && %s::text[] ORDER BY draw_seq, d.draw_id",
        (list(population),),
    )


async def draws(conn: Conn, *, limit: int = 50, cursor: str | None = None) -> Page:
    """Every draw as its ledger lists it, in commit order."""
    return _page(
        await _rows(
            conn,
            f"""SELECT * FROM ({_LEDGER}) AS d WHERE draw_seq > %s
                ORDER BY draw_seq LIMIT %s""",  # noqa: S608 - fixed SQL fragments only
            (_after(cursor, 1)[0], limit + 1),
        ),
        limit,
        "draw_seq",
    )


async def ledger_draw(conn: Conn, draw_id: str) -> Row | None:
    """A draw as its ledger lists it, with its nonce; None when unknown."""
    row = await _row(conn, f"{_LEDGER} WHERE d.draw_id = %s", (draw_id,))
    if row is not None:
        row["nonce"] = await _value(conn, "SELECT nonce FROM ds.sample_draws WHERE draw_id = %s", (draw_id,))
    return row


async def run_draws(conn: Conn, run_id: str) -> list[Row]:
    """The run's draws, oldest cycle first."""
    return await _rows(conn, "SELECT * FROM ds.sample_draws WHERE run_id = %s ORDER BY cycle", (run_id,))


async def retrying(conn: Conn, run_id: str, cycle: int) -> bool:
    """Whether every key of the cycle still to be read is a retry."""
    first = await _value(
        conn,
        """SELECT bool_or(retries = 0) FROM ds.run_keys
           WHERE run_id = %s AND cycle = %s AND state IN ('pending', 'leased', 'recheck')""",
        (run_id, cycle),
    )
    return first is False


async def finished_since(conn: Conn, run_id: str, cycle: int, seconds: int) -> int:
    """How many of the cycle's keys are done with a result written in the last ``seconds``."""
    return await _value(
        conn,
        """SELECT count(*) FROM ds.run_keys k JOIN ds.results r USING (run_id, cycle, key_masked)
           WHERE k.run_id = %s AND k.cycle = %s AND k.state = 'done'
             AND r.updated_at > now() - %s * interval '1 second'""",
        (run_id, cycle, seconds),
    )


async def newest_spec(conn: Conn, name: str) -> Row | None:
    """The newest stored version of the spec ``name``."""
    return await _row(conn, "SELECT * FROM ds.specs WHERE name = %s ORDER BY spec_id DESC LIMIT 1", (name,))


# -- adjudications -----------------------------------------------------------

_ADJUDICATION_COLUMNS: LiteralString = """j.*, ds.actor_name(j.drawn_by) AS drawn_by_name,
    ds.actor_name(j.scored_by) AS scored_by_name"""


async def insert_adjudication(
    conn: Conn,
    run_id: str,
    cycle: int,
    *,
    salt: str,
    limit: int,
    selection: Mapping[str, Any],
    sheet: str,
    drawn_by: str,
) -> Row:
    """A reviewer sheet (``J-`` id) drawn from the ended cycle: its selection and blank sheet."""
    return await _row(  # type: ignore[return-value]
        conn,
        """INSERT INTO ds.adjudications (adjudication_id, run_id, cycle, salt, sample_limit, selection, sheet,
               drawn_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (_sortable_id("J-"), run_id, cycle, salt, limit, _jsonb(selection), sheet, drawn_by),
    )


async def get_adjudication(conn: Conn, adjudication_id: str) -> Row | None:
    """The adjudication with its actors' names."""
    return await _row(
        conn,
        f"SELECT {_ADJUDICATION_COLUMNS} FROM ds.adjudications j WHERE j.adjudication_id = %s",  # noqa: S608
        (adjudication_id,),
    )


async def list_adjudications(conn: Conn, run_id: str, cycle: int | None = None) -> list[Row]:
    """Every adjudication of the ended cycle (of every cycle of the run by default), newest first, with its
    actors' names."""
    return await _rows(
        conn,
        f"""SELECT {_ADJUDICATION_COLUMNS} FROM ds.adjudications j
            WHERE j.run_id = %(run_id)s AND (%(cycle)s::int IS NULL OR j.cycle = %(cycle)s)
            ORDER BY j.adjudication_id DESC""",  # noqa: S608
        {"run_id": run_id, "cycle": cycle},
    )


async def score_adjudication(
    conn: Conn,
    adjudication_id: str,
    *,
    sheet: str,
    upload_sha256: str,
    sheet_sha256: str,
    score: Mapping[str, Any],
    score_sha256: str,
    passed: bool,
    scored_by: str,
) -> Row | None:
    """Record the adjudication's one score and its reviewed sheet; None when it is scored already."""
    return await _row(
        conn,
        """UPDATE ds.adjudications SET sheet = %s, upload_sha256 = %s, sheet_sha256 = %s, score = %s,
               score_sha256 = %s, passed = %s, scored_by = %s, scored_at = now()
           WHERE adjudication_id = %s AND scored_at IS NULL RETURNING *""",
        (sheet, upload_sha256, sheet_sha256, _jsonb(score), score_sha256, passed, scored_by, adjudication_id),
    )


async def open_signoff(conn: Conn, run_id: str, cycle: int) -> str | None:
    """The ended cycle's requested or approved sign-off, if any, read under the cycle's row lock: a draw or
    score holding it cannot miss a sign-off request, which waits for it (and it for the request)."""
    await conn.execute("SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle = %s FOR UPDATE", (run_id, cycle))
    return await _value(
        conn,
        """SELECT approval_id FROM ds.approvals
           WHERE kind = 'signoff' AND run_id = %s AND cycle = %s AND status IN ('requested', 'approved')
           ORDER BY approval_id LIMIT 1""",
        (run_id, cycle),
    )


async def attested_adjudication(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The adjudication a sign-off request of the ended cycle attests: its newest scored by a person. The
    cycle's row is read FOR KEY SHARE first, so a draw or score in flight commits before this read."""
    await conn.execute(
        "SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle = %s FOR KEY SHARE", (run_id, cycle)
    )
    return await _row(
        conn,
        """SELECT * FROM ds.adjudications
           WHERE run_id = %s AND cycle = %s AND scored_at IS NOT NULL AND scored_by LIKE 'user:%%'
           ORDER BY adjudication_id DESC LIMIT 1""",
        (run_id, cycle),
    )


async def cycle_adjudication(conn: Conn, run_id: str, cycle: int, adjudication_id: str) -> Row | None:
    """The adjudication of that id, if it is the ended cycle's."""
    return await _row(
        conn,
        "SELECT * FROM ds.adjudications WHERE adjudication_id = %s AND run_id = %s AND cycle = %s",
        (adjudication_id, run_id, cycle),
    )


# -- labelled accuracy ----------------------------------------------

# A labelling with its actors' names, its sides' roles, its cycle's labellings counted (drawn, labelled)
# and its labeller's independence: whether the labeller answers for the run's start, and the spec
# approval the cycle cites when the labeller wrote it.
_LABELLINGS: LiteralString = """SELECT v.*, ds.actor_name(v.drawn_by) AS drawn_by_name,
        ds.actor_name(v.labelled_by) AS labelled_by_name, s.body #>> '{sides,a,role}' AS role_a,
        s.body #>> '{sides,b,role}' AS role_b,
        (SELECT count(*) FROM ds.labellings o
         WHERE (o.run_id, o.cycle) = (v.run_id, v.cycle)) AS cycle_drawn,
        (SELECT count(o.labelled_at) FROM ds.labellings o
         WHERE (o.run_id, o.cycle) = (v.run_id, v.cycle)) AS cycle_labelled,
        ds.accountable(v.labelled_by) && ds.accountable(r.created_by) AS started_run,
        (SELECT p.approval_id FROM ds.approvals p
         WHERE p.approval_id = coalesce(c.approval_id, (
                   SELECT a.approval_id FROM ds.approvals a
                   WHERE a.spec_id = r.spec_id AND a.kind = 'spec' AND a.status = 'approved'))
           AND p.authors && ds.accountable(v.labelled_by)) AS spec_author
    FROM ds.labellings v JOIN ds.runs r ON r.run_id = v.run_id JOIN ds.specs s ON s.spec_id = r.spec_id
    JOIN ds.cycles c ON (c.run_id, c.cycle) = (v.run_id, v.cycle)"""
# A labelling as lists show it: no sheet and no selection, of which only the items drawn are counted,
# and of its score what its one-line summary reads, without each root cause's and rule's figures.
_LISTED: LiteralString = """SELECT v.labelling_id, v.run_id, v.cycle, v.salt, v.size, v.per_cause,
        v.accepted, v.agreed, v.truth_source, v.truth_as_of, v.selection_sha256, v.drawn_by, v.drawn_at,
        v.sheet_sha256, v.labels_sha256, v.score_sha256, v.labelled_by, v.labelled_at, v.purged_at,
        ds.actor_name(v.drawn_by) AS drawn_by_name, ds.actor_name(v.labelled_by) AS labelled_by_name,
        s.body #>> '{sides,a,role}' AS role_a, s.body #>> '{sides,b,role}' AS role_b,
        (v.selection #>> '{counts,discrepant,drawn}')::int + (v.selection #>> '{counts,accepted,drawn}')::int
            + (v.selection #>> '{counts,agreed,drawn}')::int AS item_count,
        v.accuracy #- '{discrepant,by_cause}' #- '{accepted,by_rule}' AS accuracy
    FROM ds.labellings v JOIN ds.runs r ON r.run_id = v.run_id JOIN ds.specs s ON s.spec_id = r.spec_id"""
# The population of the result ``k``: what ``types.derive_verdict`` calls DIFFERENT is discrepant.
_POPULATION: LiteralString = f"""CASE
        WHEN k.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B') THEN 'discrepant'
        WHEN k.outcome = 'KNOWN_DIFF' THEN CASE WHEN {_BUG} THEN 'discrepant' ELSE 'accepted' END
        WHEN k.outcome = 'NOISE_ONLY' THEN 'accepted'
        WHEN k.outcome = 'MATCH' THEN 'agreed' ELSE 'indeterminate' END"""
_BY_DIGEST: LiteralString = 'digest, key_masked COLLATE "C", cause COLLATE "C"'
_DRAWN: LiteralString = "population, key_masked, cause, digest, NULL::int AS seq, NULL::bigint AS n"


async def labelling_draw(
    conn: Conn,
    run_id: str,
    cycle: int,
    *,
    salt: str,
    size: int,
    per_cause: int,
    accepted: int,
    agreed: int,
    timeout_s: int,
) -> list[Row]:
    """A labelled sample's draw from the ended cycle, in one statement stopped after ``timeout_s``
    (QueryCanceled): the cycle's results and discrepant findings each read once, every item (a discrepant
    key in a root cause, ``*`` without one; an accepted or agreed key, when asked for) hashed once with
    ``salt`` as ``sampling.digest`` hashes ``key_masked U+001F cause``. Its joins are hash or merge joins
    whatever rows the planner expects (``enable_nestloop`` off): a nested loop over the cycle's rows, which
    it takes when the statistics say few findings have a root cause, is quadratic. Both settings last until
    the caller's transaction ends, which holds this statement alone. Rows by ``kind``: ``uniform`` (each
    population's ``size``, ``accepted`` or ``agreed`` smallest digests), ``coverage`` (each cause's
    ``per_cause`` smallest), ``cause`` (its ``seq`` and items ``n``), ``keys`` (per population, the
    indeterminate ones too), ``not_compared``, ``matched_by_row_hash`` and ``rule`` (accepted keys per
    suppressing rule, ``mask`` for none)."""
    await conn.execute(
        "SELECT set_config('statement_timeout', %s, true), set_config('enable_nestloop', 'off', true)",
        (str(timeout_s * 1000),),
    )
    return await _rows(
        conn,
        f"""WITH res AS MATERIALIZED (
                SELECT k.key_masked, {_POPULATION} AS population
                FROM ds.results k WHERE k.run_id = %(r)s AND k.cycle = %(c)s),
            caused AS MATERIALIZED (
                SELECT DISTINCT f.key_masked, f.root_cause_id AS cause
                FROM ds.findings f JOIN res USING (key_masked)
                WHERE f.run_id = %(r)s AND f.cycle = %(c)s AND f.root_cause_id IS NOT NULL
                  AND res.population = 'discrepant'),
            items AS MATERIALIZED (
                SELECT i.*, sha256(convert_to(%(salt)s || chr(31) || i.key_masked || chr(31) || i.cause,
                                              'UTF8')) AS digest
                FROM (SELECT 'discrepant' AS population, key_masked, cause FROM caused
                      UNION ALL
                      SELECT population, key_masked, '*' FROM res
                      WHERE population = 'discrepant'
                        AND NOT EXISTS (SELECT 1 FROM caused WHERE caused.key_masked = res.key_masked)
                      UNION ALL
                      SELECT population, key_masked, '*' FROM res
                      WHERE (population = 'accepted' AND %(accepted)s > 0)
                         OR (population = 'agreed' AND %(agreed)s > 0)) AS i)
            (SELECT 'uniform' AS kind, {_DRAWN} FROM items WHERE population = 'discrepant'
             ORDER BY {_BY_DIGEST} LIMIT %(size)s)
            UNION ALL
            (SELECT 'uniform', {_DRAWN} FROM items WHERE population = 'accepted'
             ORDER BY {_BY_DIGEST} LIMIT %(accepted)s)
            UNION ALL
            (SELECT 'uniform', {_DRAWN} FROM items WHERE population = 'agreed'
             ORDER BY {_BY_DIGEST} LIMIT %(agreed)s)
            UNION ALL
            (SELECT 'coverage', {_DRAWN}
             FROM (SELECT i.*, row_number() OVER (PARTITION BY cause
                                                 ORDER BY digest, key_masked COLLATE "C") AS place
                   FROM items i WHERE population = 'discrepant' AND cause <> '*' AND %(per_cause)s > 0) AS i
             WHERE place <= %(per_cause)s)
            UNION ALL
            (SELECT 'cause', 'discrepant', NULL, i.cause, NULL, rc.seq, i.n
             FROM (SELECT cause, count(*) AS n FROM items WHERE population = 'discrepant' GROUP BY cause) AS i
             LEFT JOIN ds.root_causes rc ON (rc.run_id, rc.cycle, rc.root_cause_id) = (%(r)s, %(c)s, i.cause))
            UNION ALL
            (SELECT 'keys', population, NULL, NULL, NULL, NULL, count(*) FROM res GROUP BY population)
            UNION ALL
            (SELECT 'not_compared', NULL, NULL, NULL, NULL, NULL,
                    (SELECT count(*) FROM ds.run_keys WHERE run_id = %(r)s AND cycle = %(c)s)
                    - (SELECT count(*) FROM res))
            UNION ALL
            (SELECT 'matched_by_row_hash', NULL, NULL, NULL, NULL, NULL, (state->>'matched')::bigint
             FROM ds.scans WHERE run_id = %(r)s AND cycle = %(c)s)
            UNION ALL
            (SELECT 'rule', 'accepted', NULL, coalesce(f.rule_id, 'mask'), NULL, NULL,
                    count(DISTINCT res.key_masked)
             FROM res LEFT JOIN ds.findings f
                  ON (f.run_id, f.cycle, f.key_masked) = (%(r)s, %(c)s, res.key_masked)
                 AND f.suppressed AND f.rule_id IS NOT NULL
             WHERE res.population = 'accepted' AND %(accepted)s > 0
             GROUP BY coalesce(f.rule_id, 'mask'))""",  # noqa: S608 - fixed SQL fragments only
        {"r": run_id, "c": cycle, "salt": salt, "size": size, "per_cause": per_cause, "accepted": accepted,
         "agreed": agreed},
    )  # fmt: skip


async def labelling_items(
    conn: Conn, run_id: str, cycle: int, items: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], Row]:
    """What a labelling's sheet shows of each drawn item (masked key, cause): its key's outcome and
    observations, ``observed_at`` (the later observation's, else the cycle's end), its suppressing
    ``rules`` and its cause's masked pattern and change type."""
    rows = await _rows(
        conn,
        """SELECT i.key_masked, i.cause, k.outcome, k.obs_a_id, k.obs_b_id, rc.path_pattern, rc.change_type,
                  coalesce(greatest(a.observed_at, b.observed_at), c.ended_at) AS observed_at,
                  ARRAY(SELECT DISTINCT f.rule_id FROM ds.findings f
                        WHERE (f.run_id, f.cycle, f.key_masked) = (k.run_id, k.cycle, k.key_masked)
                          AND f.suppressed AND f.rule_id IS NOT NULL ORDER BY f.rule_id) AS rules
           FROM unnest(%(keys)s::text[], %(causes)s::text[]) AS i (key_masked, cause)
           JOIN ds.results k ON (k.run_id, k.cycle, k.key_masked) = (%(r)s, %(c)s, i.key_masked)
           JOIN ds.cycles c ON (c.run_id, c.cycle) = (k.run_id, k.cycle)
           LEFT JOIN ds.observations a ON (a.run_id, a.obs_id) = (k.run_id, k.obs_a_id)
           LEFT JOIN ds.observations b ON (b.run_id, b.obs_id) = (k.run_id, k.obs_b_id)
           LEFT JOIN ds.root_causes rc
                ON (rc.run_id, rc.cycle, rc.root_cause_id) = (k.run_id, k.cycle, i.cause)""",
        {"r": run_id, "c": cycle, "keys": [key for key, _ in items], "causes": [cause for _, cause in items]},
    )
    return {(row["key_masked"], row["cause"]): row for row in rows}


async def insert_labelling(conn: Conn, labelling: Mapping[str, Any]) -> Row:
    """A labelling (``V-`` id) drawn from the ended cycle: ``labelling``'s run_id, cycle, salt, size,
    per_cause, accepted, agreed, truth_source, truth_as_of, selection, selection_sha256, sheet and
    drawn_by. The stored row."""
    return await _row(  # type: ignore[return-value]
        conn,
        """INSERT INTO ds.labellings (labelling_id, run_id, cycle, salt, size, per_cause, accepted, agreed,
               truth_source, truth_as_of, selection, selection_sha256, sheet, drawn_by)
           VALUES (%(labelling_id)s, %(run_id)s, %(cycle)s, %(salt)s, %(size)s, %(per_cause)s, %(accepted)s,
               %(agreed)s, %(truth_source)s, %(truth_as_of)s, %(selection)s, %(selection_sha256)s, %(sheet)s,
               %(drawn_by)s)
           RETURNING *""",
        dict(labelling) | {"labelling_id": _sortable_id("V-"), "selection": _jsonb(labelling["selection"])},
    )


async def repeated_labelling(conn: Conn, labelling: Mapping[str, Any], seconds: int) -> Row | None:
    """The first unlabelled labelling of the cycle that ``labelling``'s drawer drew in the last ``seconds``
    with its sizes, truth and acknowledgement; None without one."""
    return await _row(
        conn,
        """SELECT * FROM ds.labellings
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s AND drawn_by = %(drawn_by)s
             AND labelled_at IS NULL AND purged_at IS NULL
             AND drawn_at > now() - %(seconds)s * interval '1 second'
             AND (size, per_cause, accepted, agreed) = (%(size)s, %(per_cause)s, %(accepted)s, %(agreed)s)
             AND (truth_source, truth_as_of) = (%(truth_source)s, %(truth_as_of)s)
             AND (selection->>'unlocatable')::boolean = %(unlocatable)s
           ORDER BY labelling_id LIMIT 1""",
        dict(labelling) | {"seconds": seconds},
    )


async def get_labelling(conn: Conn, labelling_id: str) -> Row | None:
    """The labelling with its actors' names, its sides' roles and its labeller's independence."""
    return await _row(conn, f"{_LABELLINGS} WHERE v.labelling_id = %s", (labelling_id,))


async def list_labellings(
    conn: Conn, run_id: str | None = None, cycle: int | None = None, *, full: bool = False
) -> list[Row]:
    """Every labelling of the ended cycle (of every cycle of the run, or of every run, by default), newest
    first, as lists show them: with its actors' names and sides' roles, ``item_count`` and, as
    ``accuracy``, its score without ``by_cause`` and ``by_rule``; no sheet, no selection. ``full``: as
    ``get_labelling`` shows them."""
    return await _rows(
        conn,
        f"""{_LABELLINGS if full else _LISTED} WHERE (%(run_id)s::text IS NULL OR v.run_id = %(run_id)s)
              AND (%(cycle)s::int IS NULL OR v.cycle = %(cycle)s)
            ORDER BY v.labelling_id DESC""",
        {"run_id": run_id, "cycle": cycle},
    )


async def label_labelling(
    conn: Conn,
    labelling_id: str,
    *,
    sheet: str,
    sheet_sha256: str,
    labels_sha256: str,
    accuracy: Mapping[str, Any],
    score_sha256: str,
    labelled_by: str,
) -> Row | None:
    """Record the labelling's one labelled sheet and its score, as of now(); None when it is labelled
    already."""
    return await _row(
        conn,
        """UPDATE ds.labellings SET sheet = %s, sheet_sha256 = %s, labels_sha256 = %s, accuracy = %s,
               score_sha256 = %s, labelled_by = %s, labelled_at = now()
           WHERE labelling_id = %s AND labelled_at IS NULL RETURNING *""",
        (sheet, sheet_sha256, labels_sha256, _jsonb(accuracy), score_sha256, labelled_by, labelling_id),
    )


async def attested_labelling(conn: Conn, run_id: str, cycle: int, *, lock: bool = True) -> Row | None:
    """The labelling a sign-off request of the ended cycle attests: its newest labelled by a person, as
    ``get_labelling`` shows it. ``lock`` reads the cycle's row FOR KEY SHARE first, so a draw or upload in
    flight commits before this read."""
    if lock:
        await conn.execute(
            "SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle = %s FOR KEY SHARE", (run_id, cycle)
        )
    return await _row(
        conn,
        f"""{_LABELLINGS} WHERE v.run_id = %s AND v.cycle = %s AND v.labelled_at IS NOT NULL
              AND v.labelled_by LIKE 'user:%%'
            ORDER BY v.labelling_id DESC LIMIT 1""",
        (run_id, cycle),
    )


async def cycle_labelling(conn: Conn, run_id: str, cycle: int, labelling_id: str) -> Row | None:
    """The labelling of that id, if it is the ended cycle's, as ``get_labelling`` shows it."""
    return await _row(
        conn,
        f"{_LABELLINGS} WHERE v.labelling_id = %s AND v.run_id = %s AND v.cycle = %s",
        (labelling_id, run_id, cycle),
    )


# -- evidence packs ------------------------------------------------------

_PACK_COLUMNS: LiteralString = """p.pack_id, p.run_id, p.cycle, p.signoff_id, p.adjudication_id,
    p.sums_sha256, p.zip_sha256, p.key_fingerprint, p.audit_seq, p.issued_by,
    ds.actor_name(p.issued_by) AS issued_by_name, p.issued_at, p.status, p.revoked_by,
    ds.actor_name(p.revoked_by) AS revoked_by_name, p.revoked_at, p.revoke_reason, p.purged_at"""


def new_pack_id() -> str:
    """A ``P-`` id: a pack's id comes before its row, as its files name it."""
    return _sortable_id("P-")


async def signed_off(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The ended cycle's approved sign-off, if any."""
    return await _row(
        conn,
        """SELECT * FROM ds.approvals
           WHERE kind = 'signoff' AND run_id = %s AND cycle = %s AND status = 'approved'""",
        (run_id, cycle),
    )


async def signoff_for_pack(conn: Conn, approval_id: str) -> Row | None:
    """The sign-off's status and attests hash, read FOR SHARE: a revoke waits for this transaction, or
    this read waits for the revoke and finds it revoked."""
    return await _row(
        conn,
        "SELECT status, attests_sha256 FROM ds.approvals WHERE approval_id = %s FOR SHARE",
        (approval_id,),
    )


async def count_results(conn: Conn, run_id: str, cycle: int) -> int:
    """How many per-key results the cycle holds."""
    return await _value(
        conn, "SELECT count(*) FROM ds.results WHERE run_id = %s AND cycle = %s", (run_id, cycle)
    )


async def last_census(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The newest ended cycle before ``cycle`` with a census: its ``cycle`` and ``census``."""
    return await _row(
        conn,
        """SELECT cycle, census FROM ds.cycles WHERE run_id = %s AND cycle < %s AND census IS NOT NULL
           ORDER BY cycle DESC LIMIT 1""",
        (run_id, cycle),
    )


async def spec_lineage(conn: Conn, spec_id: int) -> list[int]:
    """The versions of the spec's name after its newest approved version before it, up to it (the lineage
    whose uploads ds.spec_accountable counts), oldest first."""
    rows = await _rows(
        conn,
        """WITH this AS (SELECT spec_id, name FROM ds.specs WHERE spec_id = %(spec_id)s)
           SELECT s.spec_id FROM ds.specs s JOIN this ON s.name = this.name
           WHERE s.spec_id <= this.spec_id AND s.spec_id > coalesce((
               SELECT max(v.spec_id) FROM ds.specs v
               WHERE v.name = this.name AND v.spec_id < this.spec_id AND EXISTS (
                   SELECT 1 FROM ds.approvals a WHERE a.spec_id = v.spec_id AND a.kind = 'spec'
                                                  AND a.status = 'approved')), 0)
           ORDER BY s.spec_id""",
        {"spec_id": spec_id},
    )
    return [row["spec_id"] for row in rows]


async def issuers(conn: Conn, actors: Sequence[str]) -> dict[str, str]:
    """The IdP that signed in each ``user:`` actor (ds.users.issuer)."""
    rows = await _rows(
        conn,
        "SELECT 'user:' || user_id AS actor, issuer FROM ds.users WHERE 'user:' || user_id = ANY(%s)",
        (list(actors),),
    )
    return {row["actor"]: row["issuer"] for row in rows}


async def insert_pack(conn: Conn, pack: Mapping[str, Any], zip_bytes: bytes) -> Row:
    """An issued pack (``pack``: its ``ds.packs`` columns but the status ones) and its zip; the trigger
    refuses it unless its sign-off is approved, which it reads FOR SHARE."""
    columns = ("pack_id", "run_id", "cycle", "signoff_id", "adjudication_id", "manifest", "sums_sha256",
               "zip_sha256", "key_fingerprint", "audit_seq", "issued_by", "issued_at")  # fmt: skip
    values = [_jsonb(pack[c]) if c == "manifest" else pack[c] for c in columns]
    row: Row = await _row(  # type: ignore[assignment]
        conn,
        """INSERT INTO ds.packs (pack_id, run_id, cycle, signoff_id, adjudication_id, manifest, sums_sha256,
               zip_sha256, key_fingerprint, audit_seq, issued_by, issued_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        values,
    )
    await conn.execute(
        "INSERT INTO ds.pack_zips (pack_id, zip) VALUES (%s, %s)", (pack["pack_id"], zip_bytes)
    )
    return row


async def get_pack(conn: Conn, pack_id: str) -> Row | None:
    """The pack with its manifest and its actors' names."""
    return await _row(
        conn,
        f"SELECT {_PACK_COLUMNS}, p.manifest FROM ds.packs p WHERE p.pack_id = %s",  # noqa: S608
        (pack_id,),
    )


async def list_packs(
    conn: Conn,
    *,
    run_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Packs newest first, without their manifests."""
    rows = await _rows(
        conn,
        f"""SELECT {_PACK_COLUMNS} FROM ds.packs p
            WHERE (%(run_id)s::text IS NULL OR p.run_id = %(run_id)s)
              AND (%(status)s::text IS NULL OR p.status = %(status)s)
              AND (%(after)s::text IS NULL OR p.pack_id < %(after)s)
            ORDER BY p.pack_id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"run_id": run_id, "status": status, "after": cursor, "limit": limit + 1},
    )
    return _page(rows, limit, "pack_id")


async def cycle_packs(conn: Conn, run_id: str, cycle: int) -> list[Row]:
    """The ended cycle's packs, oldest first, without their manifests."""
    return await _rows(
        conn,
        f"""SELECT {_PACK_COLUMNS} FROM ds.packs p WHERE p.run_id = %s AND p.cycle = %s
            ORDER BY p.pack_id""",  # noqa: S608 - fixed SQL fragments only
        (run_id, cycle),
    )


async def pack_zip(conn: Conn, pack_id: str) -> bytes | None:
    """The pack's stored zip; None once its cycle's purge deleted it."""
    return await _value(conn, "SELECT zip FROM ds.pack_zips WHERE pack_id = %s", (pack_id,))


async def issued_packs(conn: Conn, signoff_id: str) -> list[str]:
    """The issued (not revoked) packs citing the sign-off."""
    rows = await _rows(
        conn,
        "SELECT pack_id FROM ds.packs WHERE signoff_id = %s AND status = 'issued' ORDER BY pack_id",
        (signoff_id,),
    )
    return [row["pack_id"] for row in rows]


async def revoke_packs(conn: Conn, signoff_ids: Sequence[str]) -> list[Row]:
    """The packs of the sign-offs, revoked with them: ds.tg_signoff_packs revokes a sign-off's packs as it
    leaves approved, by any writer, copying who, when and why; each ``pack_id``, ``signoff_id`` and
    ``revoke_reason``, in order."""
    return await _rows(
        conn,
        """SELECT pack_id, signoff_id, revoke_reason FROM ds.packs
           WHERE signoff_id = ANY(%s) AND status = 'revoked' ORDER BY pack_id""",
        (list(signoff_ids),),
    )


async def revoked_packs(conn: Conn) -> list[Row]:
    """Every revoked pack of every key, in the order they were revoked: what the revocation list lists."""
    return await _rows(
        conn,
        """SELECT pack_id, sums_sha256, revoked_at, key_fingerprint FROM ds.packs
           WHERE status = 'revoked' ORDER BY revoked_at, pack_id""",
    )


async def applied_at(conn: Conn, version: int) -> datetime | None:
    """When migration ``version`` was applied; None before."""
    return await _value(conn, "SELECT applied_at FROM ds.schema_version WHERE version = %s", (version,))


# -- licences --------------------------------------------------------------

_LICENCE_COLUMNS: LiteralString = """licence_id, text, sha256, installed_by,
    ds.actor_name(installed_by) AS installed_by_name, installed_at, withdrawn_by,
    ds.actor_name(withdrawn_by) AS withdrawn_by_name, withdrawn_at, withdraw_reason"""


async def licence_facts(conn: Conn) -> Row:
    """What ``licence.judge`` decides from: the ``installation_id`` (None without its row), the database's
    ``now``, the newest run's ``created_at`` (``newest_run_at``), the newest ``system.start``'s ``auth``
    and ``pack_key`` (``start``, None before one), each ``licence.install`` audit row's ``{id, object}``
    (``installs``) and the stored licences (``rows``), withdrawn ones included."""
    facts: Row = await _row(  # type: ignore[assignment]
        conn,
        """SELECT (SELECT installation_id FROM ds.installation) AS installation_id, now() AS now,
                  (SELECT max(created_at) FROM ds.runs) AS newest_run_at,
                  (SELECT coalesce(jsonb_agg(jsonb_build_object('id', id, 'object', object) ORDER BY id),
                                   '[]')
                   FROM ds.audit_log WHERE action = 'licence.install') AS installs""",
    )
    start = await last_audit(conn, "system.start")
    facts["start"] = start and {name: start["detail"].get(name) for name in ("auth", "pack_key")}
    facts["rows"] = await _rows(conn, f"SELECT {_LICENCE_COLUMNS} FROM ds.licences ORDER BY licence_id")  # noqa: S608
    return facts


async def get_licence(conn: Conn, licence_id: str) -> Row | None:
    return await _row(
        conn,
        f"SELECT {_LICENCE_COLUMNS} FROM ds.licences WHERE licence_id = %s",  # noqa: S608
        (licence_id,),
    )


async def insert_licence(conn: Conn, licence_id: str, text: str, sha256: str, *, installed_by: str) -> Row:
    """Store a licence's normalised text; ``licence.install`` checks it first."""
    return await _row(  # type: ignore[return-value]
        conn,
        """INSERT INTO ds.licences (licence_id, text, sha256, installed_by) VALUES (%s, %s, %s, %s)
           RETURNING *""",
        (licence_id, text, sha256, installed_by),
    )


async def withdraw_licence(conn: Conn, licence_id: str, by: str, reason: str) -> Row | None:
    """Withdraw the licence as of now() (ds.tg_licences refuses any other time); None when it is not
    installed or is withdrawn already."""
    return await _row(
        conn,
        """UPDATE ds.licences SET withdrawn_by = %s, withdrawn_at = now(), withdraw_reason = %s
           WHERE licence_id = %s AND withdrawn_at IS NULL RETURNING *""",
        (by, reason, licence_id),
    )


async def counted_pairs(conn: Conn, since: datetime | None) -> list[Row]:
    """Each spec name with a run in the window from ``since`` (every run when None), a run that
    finished before it being outside it, by its first such run: ``{name, first_at}``, oldest first."""
    return await _rows(
        conn,
        """SELECT s.name, min(w.first_at) AS first_at
           FROM (SELECT spec_id, min(created_at) AS first_at FROM ds.runs
                 WHERE coalesce(finished_at, 'infinity') >= coalesce(%(since)s::timestamptz, '-infinity')
                 GROUP BY spec_id) w
           JOIN ds.specs s USING (spec_id)
           GROUP BY s.name ORDER BY first_at, s.name""",
        {"since": since},
    )


async def pair_targets(conn: Conn, names: Sequence[str]) -> dict[str, list[str]]:
    """Each name's ``<side a target> vs <side b target>`` over its versions, for display only."""
    rows = await _rows(
        conn,
        """SELECT name,
                  array_agg(DISTINCT (body #>> '{sides,a,target}') || ' vs ' || (body #>> '{sides,b,target}'))
                  AS targets
           FROM ds.specs WHERE name = ANY(%s) GROUP BY name""",
        (list(names),),
    )
    return {row["name"]: [t for t in row["targets"] if t is not None] for row in rows}


async def lock_pairs(conn: Conn) -> None:
    """Hold the pair lock until the transaction ends: a run of a new pair recounts under it, and
    installs serialise on it."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PAIRS_LOCK,))


# -- notifications ----------------------------------------------------

_DISCREPANT = ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"]
_CLEAN = ["MATCH", "NOISE_ONLY", "KNOWN_DIFF"]  # read identical, or explained
# A level fact of a cycle ``c``, as text; null where the cycle does not hold it.
_LEVEL_FACTS: dict[str, LiteralString] = {
    "mismatch_rate": "nullif(c.counters->>'mismatch_rate', 'unavailable')",
    "coverage": "nullif(c.counters->>'coverage', 'unavailable')",
    "indeterminate_rate": """trim_scale(round((c.counters->>'indeterminate')::numeric
                             / nullif((c.counters->>'total')::numeric, 0), 6))::text""",
    "unexplained_critical": """CASE WHEN c.purging_at IS NULL THEN (
        SELECT count(DISTINCT f.key_masked) FROM ds.findings f
        WHERE f.run_id = c.run_id AND f.cycle = c.cycle AND f.severity = 'CRITICAL'
          AND (NOT f.suppressed OR f.rule_id = ANY(%(bug_rules)s)))::text END""",
}


def new_delivery_id() -> str:
    return _sortable_id("N-")


async def notify_cursor(conn: Conn) -> Row:
    """The feed's cursor ``{seq, now}``, locked until the transaction ends; the first pass inserts it at the
    chain's head (``now``: the transaction's time)."""
    lock = "SELECT seq, now() AS now FROM ds.notify_cursor FOR UPDATE"
    if (row := await _row(conn, lock)) is None:
        await conn.execute(
            """INSERT INTO ds.notify_cursor (seq) SELECT coalesce(max(seq), 0) FROM ds.audit_chain
               ON CONFLICT DO NOTHING"""
        )
        row = await _row(conn, lock)
    return row  # type: ignore[return-value]


async def move_cursor(conn: Conn, seq: int) -> None:
    await conn.execute("UPDATE ds.notify_cursor SET seq = %s, moved_at = now()", (seq,))


async def chain_head(conn: Conn) -> int:
    """The last chain row's seq; 0 while the chain is empty."""
    return await _value(conn, "SELECT coalesce(max(seq), 0) FROM ds.audit_chain", None)


async def chain_before(conn: Conn, low: int, high: int, before: datetime) -> int:
    """The last chain row from ``low`` (written before ``before``) to ``high`` written before ``before``: a
    bisection on seq, about 20 primary-key lookups, as ``at`` follows seq to within a transaction's length."""
    while low < high:
        mid = (low + high + 1) // 2
        at = await _value(
            conn,
            "SELECT l.at FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id WHERE c.seq = %s",
            (mid,),
        )
        low, high = (mid, high) if at < before else (low, mid - 1)
    return low


async def cycle_at(conn: Conn, run_id: str, seq: int) -> int:
    """The run's cycle as of chain row ``seq``: the one its latest ``run.cycle`` row before it started."""
    cycle = await _value(
        conn,
        """SELECT (l.detail->>'cycle')::int FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id
           WHERE c.object = %s AND c.seq < %s AND l.action = 'run.cycle' ORDER BY c.seq DESC LIMIT 1""",
        (run_id, seq),
    )
    return cycle or 1


async def installation_id(conn: Conn) -> str:
    return await _value(conn, "SELECT installation_id FROM ds.installation", None)


async def previous_fact(
    conn: Conn, run_id: str, cycle: int, fact: str, bug_rules: Sequence[str]
) -> Row | None:
    """A level fact's baseline for ``cycle``: ``{cycle, value}`` of the latest
    earlier cycle that holds it (``unexplained_critical``: whose purge has not started), backwards on the
    ds.cycles key; None when none does."""
    value = _LEVEL_FACTS[fact]
    return await _row(
        conn,
        f"""SELECT c.cycle, {value} AS value FROM ds.cycles c
            WHERE c.run_id = %(run_id)s AND c.cycle < %(cycle)s AND {value} IS NOT NULL
            ORDER BY c.cycle DESC LIMIT 1""",  # noqa: S608 - fixed SQL fragments only
        {"run_id": run_id, "cycle": cycle, "bug_rules": list(bug_rules)},
    )


async def root_cause_changes(conn: Conn, run_id: str, cycle: int) -> Row:
    """The cycle's root causes held by no retained earlier cycle (``new``) and the previous cycle's absent
    from it (``gone``), with ``baseline_from``, the earliest retained earlier cycle, and whether the previous
    one is retained (``previous_kept``); a cycle is retained until its purge starts."""
    return await _row(  # type: ignore[return-value]
        conn,
        """WITH kept AS (SELECT cycle FROM ds.cycles
                         WHERE run_id = %(r)s AND cycle < %(c)s AND purging_at IS NULL),
                cur AS (SELECT root_cause_id FROM ds.root_causes WHERE run_id = %(r)s AND cycle = %(c)s)
           SELECT (SELECT min(cycle) FROM kept) AS baseline_from,
                  EXISTS (SELECT 1 FROM kept WHERE cycle = %(c)s - 1) AS previous_kept,
                  (SELECT count(*) FROM cur WHERE NOT EXISTS (
                       SELECT 1 FROM ds.root_causes p JOIN kept USING (cycle)
                       WHERE p.run_id = %(r)s AND p.root_cause_id = cur.root_cause_id)) AS new,
                  (SELECT count(*) FROM ds.root_causes p WHERE p.run_id = %(r)s AND p.cycle = %(c)s - 1
                     AND NOT EXISTS (SELECT 1 FROM cur WHERE cur.root_cause_id = p.root_cause_id)) AS gone""",
        {"r": run_id, "c": cycle},
    )


async def discrepant_changes(
    conn: Conn, run_id: str, cycle: int, *, read_all_before: bool, read_all: bool
) -> Row:
    """Over the keys read in both the cycle and the one before, ``newly``
    discrepant (MISMATCH, NOT_FOUND_A or NOT_FOUND_B) in it and not before, and ``no_longer`` discrepant, read
    MATCH, NOISE_ONLY or KNOWN_DIFF in it; O(discrepant keys), from each cycle's discrepant subset
    (results_outcome_idx). In a cycle that read every key (``read_all_before`` for the one before,
    ``read_all`` for this one: a table scan's, run to its end and uncut) a key without a result was read
    identical; so is every key before cycle 1."""
    return await _row(  # type: ignore[return-value]
        conn,
        """SELECT
             (SELECT count(*) FROM ds.results cur
              LEFT JOIN ds.results prev ON (prev.run_id, prev.cycle, prev.key_masked)
                                           = (cur.run_id, cur.cycle - 1, cur.key_masked)
              WHERE cur.run_id = %(r)s AND cur.cycle = %(c)s AND cur.outcome = ANY(%(d)s)
                AND (prev.outcome <> ALL(%(d)s) OR prev.outcome IS NULL AND (%(before)s OR %(c)s = 1)))
               AS newly,
             (SELECT count(*) FROM ds.results prev
              LEFT JOIN ds.results cur ON (cur.run_id, cur.cycle, cur.key_masked)
                                          = (prev.run_id, prev.cycle + 1, prev.key_masked)
              WHERE prev.run_id = %(r)s AND prev.cycle = %(c)s - 1 AND prev.outcome = ANY(%(d)s)
                AND (cur.outcome = ANY(%(clean)s) OR cur.outcome IS NULL AND %(all)s)) AS no_longer""",
        {
            "r": run_id,
            "c": cycle,
            "d": _DISCREPANT,
            "clean": _CLEAN,
            "before": read_all_before,
            "all": read_all,
        },
    )


async def insert_delivery(conn: Conn, delivery: Mapping[str, Any]) -> bool:
    """Store a delivery once per event and channel, pending and due now unless ``why`` suppressed it;
    False when the event's delivery to the channel exists. ``delivery``: delivery_id, event_id, seq, event,
    severity, channel, rules, dedup_key, run_id, body, body_sha256, render, why, mute_id."""
    return bool(
        await _changed(
            conn,
            """INSERT INTO ds.notify_deliveries (delivery_id, event_id, seq, event, severity, channel, rules,
                   dedup_key, run_id, body, body_sha256, render, state, why, mute_id, due_at)
               VALUES (%(delivery_id)s, %(event_id)s, %(seq)s, %(event)s, %(severity)s, %(channel)s,
                   %(rules)s, %(dedup_key)s, %(run_id)s, %(body)s, %(body_sha256)s, %(render)s,
                   CASE WHEN %(why)s::text IS NULL THEN 'pending' ELSE 'suppressed' END, %(why)s, %(mute_id)s,
                   CASE WHEN %(why)s::text IS NULL THEN now() END)
               ON CONFLICT (event_id, channel) DO NOTHING""",
            dict(delivery) | {"rules": list(delivery["rules"])},
        )
    )


async def active_mute(conn: Conn, channel: str, run_id: str | None) -> str | None:
    """The first active mute of the channel or the run."""
    return await _value(
        conn,
        """SELECT mute_id FROM ds.notify_mutes WHERE ended_at IS NULL AND until > now()
             AND (scope, target) IN (('channel', %s), ('run', %s)) ORDER BY mute_id LIMIT 1""",
        (channel, run_id),
    )


async def held_back(
    conn: Conn, channel: str, dedup_key: str, dedup_hours: int, *, among: str | None = None
) -> Row:
    """What a new delivery to the channel is judged by: whether a pending or sent one
    of ``dedup_key`` is under ``dedup_hours`` old (``duplicate``; with ``among``, a key prefix, only while no
    pending or sent delivery of another key beginning with it followed: the newest of them alone counts),
    the pending and sent ones of the last hour (``last_hour``) and the suppressed ones since the last that
    was not, tests aside (``suppressed``)."""
    return await _row(  # type: ignore[return-value]
        conn,
        """SELECT EXISTS (SELECT 1 FROM ds.notify_deliveries d
                          WHERE d.channel = %(ch)s AND d.dedup_key = %(key)s
                            AND d.state IN ('pending', 'sent')
                            AND d.created_at > now() - make_interval(hours => %(hours)s)
                            AND NOT EXISTS (
                                SELECT 1 FROM ds.notify_deliveries n
                                WHERE n.channel = d.channel AND n.state IN ('pending', 'sent')
                                  AND n.created_at >= d.created_at AND n.delivery_id > d.delivery_id
                                  AND n.dedup_key <> d.dedup_key
                                  AND starts_with(n.dedup_key, %(among)s::text))) AS duplicate,
                  (SELECT count(*) FROM ds.notify_deliveries
                   WHERE channel = %(ch)s AND state IN ('pending', 'sent')
                     AND created_at > now() - interval '1 hour') AS last_hour,
                  (SELECT count(*) FROM ds.notify_deliveries
                   WHERE channel = %(ch)s AND state = 'suppressed'
                     AND delivery_id > coalesce((SELECT max(delivery_id) FROM ds.notify_deliveries
                                                 WHERE channel = %(ch)s AND state <> 'suppressed'
                                                   AND event_id NOT LIKE 'test:%%'), ''))
                   AS suppressed""",
        {"ch": channel, "key": dedup_key, "hours": dedup_hours, "among": among},
    )


async def due_channels(conn: Conn, max_age_hours: int) -> list[str]:
    """The channels whose head, the oldest pending delivery, is due: its ``due_at`` passed, or it was queued
    over ``max_age_hours`` ago (it expires). A head backing off holds its channel (notify_pending_idx)."""
    rows = await _rows(
        conn,
        """SELECT channel FROM (
               SELECT DISTINCT ON (channel) channel, due_at, queued_at FROM ds.notify_deliveries
               WHERE state = 'pending' ORDER BY channel, delivery_id) heads
           WHERE due_at <= now() OR queued_at < now() - make_interval(hours => %s)""",
        (max_age_hours,),
    )
    return [row["channel"] for row in rows]


async def notify_head(conn: Conn, channel: str, max_age_hours: int) -> Row | None:
    """The channel's head, locked until the transaction ends, with whether it is ``due`` and ``expired``."""
    return await _row(
        conn,
        """SELECT delivery_id, event, run_id, attempts, last_status, body_sha256, render,
                  queued_at < now() - make_interval(hours => %(age)s) AS expired,
                  due_at <= now() OR queued_at < now() - make_interval(hours => %(age)s) AS due
           FROM ds.notify_deliveries WHERE channel = %(ch)s AND state = 'pending'
           ORDER BY delivery_id LIMIT 1 FOR UPDATE""",
        {"ch": channel, "age": max_age_hours},
    )


async def claim_delivery(conn: Conn, delivery_id: str, lease_seconds: float) -> Row:
    """Take an attempt of the delivery: due again after ``lease_seconds``, unless recorded first."""
    return await _row(  # type: ignore[return-value]
        conn,
        """UPDATE ds.notify_deliveries SET attempts = attempts + 1, due_at = now() + make_interval(secs => %s)
           WHERE delivery_id = %s
           RETURNING delivery_id, channel, event, run_id, attempts, body, body_sha256, created_at""",
        (lease_seconds, delivery_id),
    )


async def end_delivery(
    conn: Conn,
    delivery_id: str,
    state: str,
    *,
    attempts: int | None = None,
    error: str | None = None,
    status: int | None = None,
    due_in: float = 0.0,
    mute_id: str | None = None,
) -> bool:
    """Record a pending delivery's attempt (of number ``attempts``, when given) or fate: ``sent``, ``dead``,
    ``pending`` again after ``due_in`` seconds, or ``suppressed`` by ``mute_id``; False when it moved on."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.notify_deliveries SET state = %(state)s, last_error = %(error)s,
                   last_status = coalesce(%(status)s, last_status),
                   due_at = CASE WHEN %(state)s = 'pending'
                                 THEN now() + make_interval(secs => %(due_in)s) END,
                   done_at = CASE WHEN %(state)s IN ('sent', 'dead') THEN now() END,
                   why = CASE WHEN %(state)s = 'suppressed' THEN 'muted' END, mute_id = %(mute_id)s
               WHERE delivery_id = %(id)s AND state = 'pending'
                 AND (%(attempts)s::int IS NULL OR attempts = %(attempts)s)""",
            {
                "id": delivery_id,
                "state": state,
                "attempts": attempts,
                "error": error,
                "status": status,
                "due_in": due_in,
                "mute_id": mute_id,
            },
        )
    )


async def deaths(conn: Conn, channel: str, delivery_id: str) -> Row:
    """Whether the channel's newest ``notify.dead`` row is under an hour old (``recent``) and how many of its
    other deliveries died since it (``since``; since the first, without one)."""
    return await _row(  # type: ignore[return-value]
        conn,
        """WITH last AS (SELECT at FROM ds.audit_log WHERE object = %(obj)s AND action = 'notify.dead'
                         ORDER BY id DESC LIMIT 1)
           SELECT coalesce((SELECT at > now() - interval '1 hour' FROM last), false) AS recent,
                  (SELECT count(*) FROM ds.notify_deliveries
                   WHERE channel = %(ch)s AND state = 'dead' AND delivery_id <> %(id)s
                     AND done_at > coalesce((SELECT at FROM last), '-infinity')) AS since""",
        {"obj": f"notify:{channel}", "ch": channel, "id": delivery_id},
    )


async def daily_counts(conn: Conn, actions: Sequence[str]) -> Row:
    """What ``notify.daily`` says, as of ``now``: live runs by status, paused
    ones by reason, cycles ended and runs finished in 24 h, approvals waiting, the newest recorded licence
    state, each channel's counts and the age of its pending head, the feed's lag (the age of the oldest
    unread row of ``actions``, 0 when none) and the active mutes."""
    counts: Row = await _row(  # type: ignore[assignment]
        conn,
        """SELECT now() AS now, (SELECT coalesce(jsonb_object_agg(status, n), '{}') FROM (
                       SELECT status, count(*) AS n FROM ds.runs
                       WHERE status IN ('queued', 'running', 'paused', 'cancelling') GROUP BY status) s)
                      AS runs,
                  (SELECT coalesce(jsonb_object_agg(reason, n), '{}') FROM (
                       SELECT coalesce(stop_reason, 'manual') AS reason, count(*) AS n FROM ds.runs
                       WHERE status = 'paused' GROUP BY 1) p) AS paused,
                  (SELECT count(*) FROM ds.cycles WHERE ended_at > now() - interval '24 hours')
                      AS cycles_ended_24h,
                  (SELECT count(*) FROM ds.runs WHERE finished_at > now() - interval '24 hours')
                      AS runs_finished_24h,
                  (SELECT count(*) FROM ds.approvals WHERE status = 'requested') AS approvals_waiting,
                  (SELECT detail->>'to' FROM ds.audit_log WHERE action = 'licence.state'
                   ORDER BY id DESC LIMIT 1)
                      AS licence,
                  coalesce((SELECT floor(extract(epoch FROM now() - l.at))::int
                            FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id
                            WHERE c.seq > (SELECT seq FROM ds.notify_cursor) AND l.action = ANY(%s)
                            ORDER BY c.seq LIMIT 1), 0) AS feed_lag_s,
                  (SELECT count(*) FROM ds.notify_mutes WHERE ended_at IS NULL AND until > now()) AS mutes""",
        (list(actions),),
    )
    counts["channels"] = {
        row.pop("channel"): row
        for row in await _rows(
            conn,
            """SELECT channel, count(*) FILTER (WHERE state = 'pending') AS pending,
                      count(*) FILTER (WHERE state = 'sent' AND done_at > now() - interval '1 day')
                          AS sent_24h,
                      count(*) FILTER (WHERE state = 'dead' AND done_at > now() - interval '1 day')
                          AS dead_24h,
                      count(*) FILTER (WHERE state = 'suppressed' AND created_at > now() - interval '1 day')
                          AS suppressed_24h,
                      floor(extract(epoch FROM now() - min(queued_at) FILTER (WHERE state = 'pending')))::int
                          AS head_age_s
               FROM ds.notify_deliveries WHERE state = 'pending' OR created_at > now() - interval '24 hours'
               GROUP BY channel""",
        )
    }
    return counts


async def prune_notify(conn: Conn, days: int, limit: int) -> int:
    """Delete up to ``limit`` deliveries made, and as many mutes ended or lapsed, over ``days`` days ago;
    the rows deleted."""
    deleted = await _changed(
        conn,
        """DELETE FROM ds.notify_deliveries WHERE delivery_id IN (
               SELECT delivery_id FROM ds.notify_deliveries
               WHERE created_at < now() - make_interval(days => %s) LIMIT %s)""",
        (days, limit),
    )
    return deleted + await _changed(
        conn,
        """DELETE FROM ds.notify_mutes WHERE mute_id IN (
               SELECT mute_id FROM ds.notify_mutes
               WHERE coalesce(ended_at, until) < now() - make_interval(days => %s) LIMIT %s)""",
        (days, limit),
    )


async def notify_state(conn: Conn, actions: Sequence[str]) -> Row:
    """The feed as the Notifications screen shows it: ``now``, the cursor's ``seq`` (None before the first
    pass), the chain's ``head`` and ``behind``, when the oldest unread row of ``actions`` was written (None
    when none is unread)."""
    return await _row(  # type: ignore[return-value]
        conn,
        """SELECT now() AS now, (SELECT seq FROM ds.notify_cursor) AS seq,
                  (SELECT coalesce(max(seq), 0) FROM ds.audit_chain) AS head,
                  (SELECT l.at FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id
                   WHERE c.seq > (SELECT seq FROM ds.notify_cursor) AND l.action = ANY(%s)
                   ORDER BY c.seq LIMIT 1) AS behind""",
        (list(actions),),
    )


async def channel_states(conn: Conn, channels: Sequence[str]) -> dict[str, Row]:
    """Each channel's deliveries as the Notifications screen counts them: pending; sent, dead and suppressed
    in 24 h; the newest attempt's ``last_error`` and ``last_status`` (its head's once tried, else the latest
    sent or dead one's); its head's ``head_error`` and when it fell due (``head_due``, None unless due); and
    the dead deliveries since its latest sent one, tests aside (``failing``, from ``failing_since``)."""
    rows = await _rows(
        conn,
        """SELECT c.channel, k.*, h.head_error, h.head_due,
                  coalesce(h.last_error, l.last_error) AS last_error,
                  coalesce(h.last_status, l.last_status) AS last_status, f.failing, f.failing_since
           FROM unnest(%s::text[]) AS c(channel)
           CROSS JOIN LATERAL (
               SELECT count(*) FILTER (WHERE state = 'pending') AS pending,
                      count(*) FILTER (WHERE state = 'sent' AND done_at > now() - interval '1 day')
                          AS sent_24h,
                      count(*) FILTER (WHERE state = 'dead' AND done_at > now() - interval '1 day')
                          AS dead_24h,
                      count(*) FILTER (WHERE state = 'suppressed' AND created_at > now() - interval '1 day')
                          AS suppressed_24h
               FROM ds.notify_deliveries WHERE channel = c.channel) k
           LEFT JOIN LATERAL (
               SELECT CASE WHEN attempts > 0 THEN last_error END AS head_error,
                      CASE WHEN attempts > 0 THEN last_error END AS last_error,
                      CASE WHEN attempts > 0 THEN last_status END AS last_status,
                      CASE WHEN due_at <= now() THEN due_at END AS head_due, attempts
               FROM ds.notify_deliveries WHERE channel = c.channel AND state = 'pending'
               ORDER BY delivery_id LIMIT 1) h ON true
           LEFT JOIN LATERAL (
               SELECT last_error, last_status FROM ds.notify_deliveries
               WHERE channel = c.channel AND state IN ('sent', 'dead') AND coalesce(h.attempts, 0) = 0
               ORDER BY done_at DESC LIMIT 1) l ON true
           CROSS JOIN LATERAL (
               SELECT count(*) AS failing, min(done_at) AS failing_since FROM ds.notify_deliveries d
               WHERE d.channel = c.channel AND d.state = 'dead' AND d.event_id NOT LIKE 'test:%%'
                 AND d.done_at > coalesce((SELECT max(done_at) FROM ds.notify_deliveries
                                           WHERE channel = c.channel AND state = 'sent'
                                             AND event_id NOT LIKE 'test:%%'), '-infinity')) f""",
        (list(channels),),
    )
    return {row.pop("channel"): row for row in rows}


async def list_deliveries(
    conn: Conn,
    *,
    channel: str | None = None,
    state: str | None = None,
    event: str | None = None,
    run_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Deliveries newest first, each with its stored body (an e-mail's without From and To)."""
    rows = await _rows(
        conn,
        """SELECT * FROM ds.notify_deliveries
           WHERE (%(channel)s::text IS NULL OR channel = %(channel)s)
             AND (%(state)s::text IS NULL OR state = %(state)s)
             AND (%(event)s::text IS NULL OR event = %(event)s)
             AND (%(run_id)s::text IS NULL OR run_id = %(run_id)s)
             AND (%(after)s::text IS NULL OR delivery_id < %(after)s)
           ORDER BY delivery_id DESC LIMIT %(limit)s""",
        {
            "channel": channel,
            "state": state,
            "event": event,
            "run_id": run_id,
            "after": cursor,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "delivery_id")


async def insert_tested(conn: Conn, delivery: Mapping[str, Any]) -> None:
    """Store a test send after its one attempt, ``sent`` or ``dead``, never pending, so no sender claims it.
    ``delivery``: delivery_id, event_id, event, severity, channel, dedup_key, body, body_sha256, render,
    state, error, status."""
    await conn.execute(
        """INSERT INTO ds.notify_deliveries (delivery_id, event_id, event, severity, channel, dedup_key, body,
               body_sha256, render, state, attempts, last_error, last_status, done_at)
           VALUES (%(delivery_id)s, %(event_id)s, %(event)s, %(severity)s, %(channel)s, %(dedup_key)s,
               %(body)s, %(body_sha256)s, %(render)s, %(state)s, 1, %(error)s, %(status)s, now())""",
        delivery,
    )


async def retry_dead(conn: Conn, channel: str, since: datetime, render: str) -> int:
    """Send again the channel's deliveries that died from ``since``, tests aside, rendered as it now renders
    (``render``): pending and due now, with no attempt, queued now (so they expire from now); how many."""
    return await _changed(
        conn,
        """UPDATE ds.notify_deliveries
           SET state = 'pending', due_at = now(), attempts = 0, queued_at = now(), done_at = NULL
           WHERE channel = %s AND state = 'dead' AND done_at >= %s AND event_id NOT LIKE 'test:%%'
             AND render = %s""",
        (channel, since, render),
    )


def new_mute_id() -> str:
    return _sortable_id("M-")


async def insert_mute(conn: Conn, scope: str, target: str, hours: int, reason: str, created_by: str) -> Row:
    """A mute of the channel or run ``target`` for ``hours`` from now: ``{mute_id, until}``."""
    return await _row(  # type: ignore[return-value]
        conn,
        """INSERT INTO ds.notify_mutes (mute_id, scope, target, until, reason, created_by)
           VALUES (%s, %s, %s, now() + make_interval(hours => %s), %s, %s) RETURNING mute_id, until""",
        (new_mute_id(), scope, target, hours, reason, created_by),
    )


async def get_mute(conn: Conn, mute_id: str) -> Row | None:
    """The mute, with whether it ``lapsed``; None when unknown."""
    return await _row(
        conn, "SELECT *, until <= now() AS lapsed FROM ds.notify_mutes WHERE mute_id = %s", (mute_id,)
    )


async def end_mute(conn: Conn, mute_id: str, ended_by: str) -> bool:
    """End an active mute now; False when it ended or lapsed meanwhile."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.notify_mutes SET ended_by = %s, ended_at = now()
               WHERE mute_id = %s AND ended_at IS NULL AND until > now()""",
            (ended_by, mute_id),
        )
    )


async def active_mutes(conn: Conn) -> list[Row]:
    """The mutes neither ended nor lapsed, oldest first (notify_mutes_active_idx)."""
    return await _rows(
        conn,
        """SELECT mute_id, scope, target, until, reason, created_by, created_at FROM ds.notify_mutes
           WHERE ended_at IS NULL AND until > now() ORDER BY mute_id""",
    )


# -- cut-over readiness -------------------------------------------

# An ended cycle readiness has still to measure, as cycles_unmeasured_idx holds them: never measured, or
# failed under five times; the second is due again 1, 2, 4 and 8 hours after its last try.
_UNMEASURED: LiteralString = "(c.readiness IS NULL OR (c.readiness ? 'error' AND NOT c.readiness ? 'final'))"
_RETRY_DUE: LiteralString = """(c.readiness->>'measured_at')::timestamptz
    + interval '1 hour' * (1 << ((c.readiness->>'attempts')::int - 1)) <= now()"""
_BEFORE_014: LiteralString = "c.ended_at < (SELECT applied_at FROM ds.schema_version WHERE version = 14)"
_TIERS = ("standard", "high")  # core.sampling.TARGETS: each has its breaks index
COUNTED = 100  # a pair's cycles are counted up to this many (core.readiness.COUNTED), and one more
OWED = 100_000  # the installation's cycles still to measure, likewise
# Measures as this build reads them (core.readiness.FORMAT): an evidence point, as the predicate of
# cycles_readiness_evidence_idx says it, and a record of another format, an unknown point that
# cycles_readiness_unread_idx holds.
_EVIDENCE: LiteralString = """c.readiness @> '{"evidence": true, "format": 1}'"""
_UNREAD: LiteralString = "c.readiness ? 'pair' AND c.readiness->'format' <> '1'"
# The newest points of the pair of the row ``a`` (pair, tier, until) failing at its tier, a probe of that
# tier's breaks index; ``{more}`` narrows it and ``{also}`` adds a column.
_FAILING: LiteralString = """(SELECT c.run_id, c.cycle, c.ended_at{also} FROM ds.cycles c
         WHERE a.tier = '{tier}' AND c.readiness->>'pair' = a.pair AND c.readiness->'fails'->'{tier}' <> '[]'
           AND c.ended_at <= coalesce(a.until, 'infinity'){more}
         ORDER BY c.ended_at DESC LIMIT {limit})"""
_FAILING_GATE: LiteralString = """ AND c.readiness->'fails'->'{tier}' ? g.gate AND c.readiness->'format' = '1'
           AND c.readiness->>'spec_id' = a.spec AND c.ended_at > a.evidence_at"""
# Whether the point fails a per-cycle gate, ``unknown`` aside: a record of another format never tells.
_FAILED: LiteralString = """, CASE WHEN c.readiness->'format' = '1'
                THEN (c.readiness->'fails'->'{tier}') - 'unknown' <> '[]' ELSE false END"""


def _failing(more: str = "", also: str = "", limit: str = "1") -> str:
    return " UNION ALL ".join(
        _FAILING.format(tier=tier, more=more.format(tier=tier), also=also.format(tier=tier), limit=limit)
        for tier in _TIERS
    )


def _timed(facts: Row | None, *names: str) -> Row | None:
    """Facts read from jsonb, the times among them as datetimes again."""
    if facts is None:
        return None
    return facts | {name: datetime.fromisoformat(facts[name]) for name in names if facts.get(name)}


async def unmeasured_cycles(conn: Conn, *, backfill: bool = False) -> list[Row]:
    """The next ended cycles readiness measures, newest first: those ended since 014, up to 10; or,
    ``backfill``, before it: up to 10, and with those whose purge started, which are read from the
    tombstone alone, up to 100. Each ``run_id``, ``cycle``, ``ended_at``, the run's ``pair`` (its spec's
    name) and ``spec_id``. A cycle whose measure failed comes again 1, 2, 4 and 8 hours later."""
    return await _rows(
        conn,
        f"""SELECT run_id, cycle, ended_at, pair, spec_id
            FROM (SELECT c.run_id, c.cycle, c.ended_at, s.name AS pair, r.spec_id,
                         count(*) FILTER (WHERE c.purging_at IS NULL)
                             OVER (ORDER BY c.ended_at DESC, c.run_id, c.cycle) AS whole
                  FROM ds.cycles c JOIN ds.runs r USING (run_id) JOIN ds.specs s USING (spec_id)
                  WHERE {_UNMEASURED} AND (c.readiness IS NULL OR {_RETRY_DUE})
                    AND ({_BEFORE_014}) = %(backfill)s
                  ORDER BY c.ended_at DESC, c.run_id, c.cycle LIMIT %(most)s) due
            WHERE whole <= 10 ORDER BY ended_at DESC, run_id, cycle""",  # noqa: S608 - fixed SQL fragments only
        {"backfill": backfill, "most": 100 if backfill else 10},
    )


async def backfill_owed(conn: Conn) -> bool:
    """Whether a cycle ended before 014 is still to measure (no transition is written until none is):
    one probe of cycles_unmeasured_idx."""
    return await _value(
        conn,
        f"SELECT EXISTS (SELECT 1 FROM ds.cycles c WHERE {_UNMEASURED} AND {_BEFORE_014})",  # noqa: S608
        None,
    )


async def unmeasured_pairs(conn: Conn, pairs: Sequence[str]) -> tuple[Row, dict[str, Row]]:
    """The ended cycles readiness has still to measure. Of the installation, how many were never measured
    (``cycles``) and how many ended before 014 (``backfill``: those and the ones to be tried again; while
    any is left, no transition is written), each counted up to ``OWED`` and one more. And each of
    ``pairs`` with cycles never measured: how many (``cycles``, up to ``COUNTED`` and one more) and the
    ``oldest`` (``{run_id, cycle, ended_at}``). The cycles ended since 014 that wait are few, and are read
    whole; while a backfill is owed, each run of the pairs is probed instead (cycles_unmeasured_run_idx),
    so that the read does not grow with what is owed."""
    waiting: LiteralString = "FROM ds.cycles c WHERE c.run_id = r.run_id AND c.readiness IS NULL"
    owed: Row = await _row(  # type: ignore[assignment]
        conn,
        f"""SELECT (SELECT count(*) FROM (SELECT 1 FROM ds.cycles c WHERE c.readiness IS NULL
                                           LIMIT %(most)s) n) AS cycles,
                   (SELECT count(*) FROM (SELECT 1 FROM ds.cycles c WHERE {_UNMEASURED} AND {_BEFORE_014}
                                           LIMIT %(most)s) b) AS backfill""",  # noqa: S608 - fixed fragments
        {"most": OWED + 1},
    )
    if owed["backfill"]:
        rows = await _rows(
            conn,
            f"""SELECT a.pair, least(sum(o.cycles), %(most)s)::int AS cycles, min(o.ended_at) AS ended_at,
                       (array_agg(ARRAY[o.run_id, o.cycle::text] ORDER BY o.ended_at, o.run_id, o.cycle))[1:1]
                           AS oldest
                FROM unnest(%(pairs)s::text[]) AS a (pair)
                JOIN ds.specs s ON s.name = a.pair JOIN ds.runs r ON r.spec_id = s.spec_id
                CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle, c.ended_at,
                           (SELECT count(*) FROM (SELECT 1 {waiting} LIMIT %(most)s) m) AS cycles
                    {waiting} ORDER BY c.cycle LIMIT 1) o
                GROUP BY a.pair""",  # noqa: S608 - fixed SQL fragments only
            {"pairs": list(pairs), "most": COUNTED + 1},
        )
    else:
        rows = await _rows(
            conn,
            """SELECT s.name AS pair, count(*) AS cycles, min(c.ended_at) AS ended_at,
                      (array_agg(ARRAY[c.run_id, c.cycle::text] ORDER BY c.ended_at, c.run_id, c.cycle))[1:1]
                          AS oldest
               FROM ds.cycles c JOIN ds.runs r USING (run_id) JOIN ds.specs s USING (spec_id)
               WHERE c.readiness IS NULL GROUP BY s.name""",
        )
    found = {}
    for row in rows:
        (run_id, cycle), ended_at = row["oldest"][0], row["ended_at"]
        oldest = {"run_id": run_id, "cycle": int(cycle), "ended_at": ended_at}
        found[row["pair"]] = {"cycles": row["cycles"], "oldest": oldest}
    return owed, found


async def set_readiness(conn: Conn, run_id: str, cycle: int, record: Mapping[str, Any]) -> bool:
    """Store an ended cycle's readiness measures, once: over none, or over a failed try's; False when the
    cycle holds measures already."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.cycles SET readiness = %s
               WHERE run_id = %s AND cycle = %s AND (readiness IS NULL OR readiness ? 'error')""",
            (_jsonb(record), run_id, cycle),
        )
    )


async def cycle_changes(conn: Conn, run_id: str, cycle: int) -> Row:
    """How the cycle's DIFFERENT keys (MISMATCH, NOT_FOUND_A or NOT_FOUND_B, or KNOWN_DIFF under a bug rule:
    those still owed a recheck too) moved since the cycle before: ``new`` in this one only,
    ``persistent`` in both, and of those in the one before only, ``fixed`` read MATCH in this one and
    ``other`` the rest (read otherwise, or not read: a table scan reads again every key that did not match
    before, so one without a result left both tables). Each cycle's discrepant keys come from
    results_outcome_idx; its joins are hash or merge joins whatever rows the planner expects
    (``enable_nestloop`` off for this statement, in the caller's transaction): the statistics do not know a
    cycle that has just ended, and a nested loop over its results is quadratic. So the cost is the
    discrepant keys of both cycles and, at worst, one pass over the cycle's results."""
    different: LiteralString = f"(k.outcome = ANY(%(d)s) OR (k.outcome = 'KNOWN_DIFF' AND {_BUG}))"
    planned = await _value(
        conn, "SELECT current_setting('enable_nestloop'), set_config('enable_nestloop', 'off', true)", ()
    )
    found = await _row(
        conn,
        f"""WITH cur AS (SELECT k.key_masked FROM ds.results k
                         WHERE k.run_id = %(r)s AND k.cycle = %(c)s AND {different}),
                 prev AS (SELECT k.key_masked FROM ds.results k
                          WHERE k.run_id = %(r)s AND k.cycle = %(p)s
                            AND {different.replace("%(c)s", "%(p)s")})
            SELECT count(*) FILTER (WHERE prev.key_masked IS NULL) AS new,
                   count(*) FILTER (WHERE cur.key_masked IS NOT NULL AND prev.key_masked IS NOT NULL)
                       AS persistent,
                   count(*) FILTER (WHERE cur.key_masked IS NULL AND read.outcome = 'MATCH') AS fixed,
                   count(*) FILTER (WHERE cur.key_masked IS NULL AND read.outcome IS DISTINCT FROM 'MATCH')
                       AS other
            FROM cur FULL JOIN prev USING (key_masked)
            LEFT JOIN ds.results read
                 ON cur.key_masked IS NULL
                AND (read.run_id, read.cycle, read.key_masked) = (%(r)s, %(c)s, prev.key_masked)""",  # noqa: S608
        {"r": run_id, "c": cycle, "p": cycle - 1, "d": _DISCREPANT},
    )
    await conn.execute("SELECT set_config('enable_nestloop', %s, true)", (planned,))
    return found  # type: ignore[return-value]


async def graph_counts(conn: Conn, run_id: str, cycle: int) -> dict[str, int]:
    """The cycle's graph parity rows, counted by code."""
    rows = await _rows(
        conn,
        "SELECT code, count(*) AS n FROM ds.graph_diffs WHERE run_id = %s AND cycle = %s GROUP BY code",
        (run_id, cycle),
    )
    return {row["code"]: row["n"] for row in rows}


async def readiness_series(
    conn: Conn, pairs: Sequence[Mapping[str, Any]], *, whole: bool = False
) -> dict[str, dict[str, Any]]:
    """What each of ``pairs`` (``{pair, tier, needed, window, until}``: judged at ``tier``, needing
    ``needed`` evidence cycles in a row and counting ``window`` at most, on cycles ended up to ``until``,
    None for now) is judged from, in one statement whatever the history, each cycle as ``{run_id,
    cycle, ended_at, readiness}`` (a point's ``readiness`` only when ``whole``: a streak counts its points,
    and only a pair's detail lists them):

    - ``versions``: the newest evidence point of each version, newest first (a skip scan of
      cycles_readiness_evidence_idx); the first is the evidence cycle, its version the current one;
    - ``breaks``: the newest points failing at the tier, up to ``needed``, newest first (a probe of its
      breaks index), those whose measures are of another format among them (an unknown point each);
    - ``points``: the current version's evidence points after both the newest of the breaks that fails a
      gate other than ``unknown`` and the newest evidence point of another version, newest first, up to
      the window: ``core.readiness.streak`` then applies the unknown breaks;
    - ``trend``: the pair's last 10 evidence points, newest first (the last 10 of each of its 10 newest
      versions, merged);
    - ``critical`` and ``targets``: the newest point of the current version after the evidence cycle failing
      that gate at the tier (a probe each);
    - ``newest`` and ``cycles``: for a pair without evidence, its newest point and how many it has, counted
      up to ``COUNTED`` and one more.

    The evidence cycle, ``critical`` and ``targets`` also carry ``sample``, their stored judgement. The
    statement probes indexes a pair at a time, which the planner prices high enough to compile (``jit``),
    at several times what it then takes to run: so ``jit`` is off for the rest of the caller's transaction,
    an evaluation's other reads among it. It expects far more cycles than it finds, too, so each is read
    by its key (``OFFSET 0`` keeps that read a probe: joined, the planner would scan the table)."""
    newer = _failing(_FAILING_GATE)
    measured: LiteralString = """c.readiness->>'pair' = a.pair AND c.readiness ? 'pair'
                      AND c.ended_at <= coalesce(a.until, 'infinity')"""
    await conn.execute("SELECT set_config('jit', 'off', true)")
    rows = await _rows(
        conn,
        f"""WITH RECURSIVE asked AS (
                SELECT * FROM unnest(%(pairs)s::text[], %(tiers)s::text[], %(needed)s::int[],
                                     %(windows)s::int[], %(untils)s::timestamptz[])
                    AS a (pair, tier, k, w, until)),
            versions AS (
                SELECT a.pair, a.until, v.* FROM asked a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle, c.ended_at, c.readiness->>'spec_id' AS spec FROM ds.cycles c
                    WHERE c.readiness->>'pair' = a.pair AND {_EVIDENCE}
                      AND c.ended_at <= coalesce(a.until, 'infinity')
                    ORDER BY c.readiness->>'spec_id', c.ended_at DESC LIMIT 1) v
                UNION ALL
                SELECT a.pair, a.until, v.* FROM versions a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle, c.ended_at, c.readiness->>'spec_id' AS spec FROM ds.cycles c
                    WHERE c.readiness->>'pair' = a.pair AND {_EVIDENCE}
                      AND c.readiness->>'spec_id' > a.spec AND c.ended_at <= coalesce(a.until, 'infinity')
                    ORDER BY c.readiness->>'spec_id', c.ended_at DESC LIMIT 1) v),
            ranked AS (
                SELECT v.*, row_number() OVER (PARTITION BY v.pair
                                               ORDER BY v.ended_at DESC, v.run_id DESC, v.cycle DESC) AS n
                FROM versions v),
            breaks AS (
                SELECT a.pair, b.* FROM asked a CROSS JOIN LATERAL (
                    SELECT * FROM (
                        {_failing(also=_FAILED, limit="a.k")}
                        UNION
                        (SELECT c.run_id, c.cycle, c.ended_at, false FROM ds.cycles c
                         WHERE c.readiness->>'pair' = a.pair AND {_UNREAD}
                           AND c.ended_at <= coalesce(a.until, 'infinity')
                         ORDER BY c.ended_at DESC LIMIT a.k)) f (run_id, cycle, ended_at, failed)
                    ORDER BY f.ended_at DESC, f.run_id DESC, f.cycle DESC LIMIT a.k) b),
            failures AS (SELECT pair, max(ended_at) AS ended_at FROM breaks WHERE failed GROUP BY pair),
            bounds AS (
                SELECT a.*, cur.spec, cur.ended_at AS evidence_at,
                       greatest(b.ended_at, other.ended_at) AS after
                FROM asked a JOIN ranked cur ON cur.pair = a.pair AND cur.n = 1
                LEFT JOIN ranked other ON other.pair = a.pair AND other.n = 2
                LEFT JOIN failures b ON b.pair = a.pair),
            found AS (
                SELECT 'version' AS kind, pair, run_id, cycle, n FROM ranked
                UNION ALL
                SELECT 'break', pair, run_id, cycle, 0 FROM breaks
                UNION ALL
                SELECT 'point', a.pair, p.run_id, p.cycle, 0 FROM bounds a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle FROM ds.cycles c
                    WHERE c.readiness->>'pair' = a.pair AND c.readiness->>'spec_id' = a.spec
                      AND {_EVIDENCE}
                      AND c.ended_at > coalesce(a.after, '-infinity')
                      AND c.ended_at <= coalesce(a.until, 'infinity')
                    ORDER BY c.ended_at DESC LIMIT a.w) p
                UNION ALL
                SELECT 'trend', t.pair, t.run_id, t.cycle, 0 FROM (
                    SELECT a.pair, p.run_id, p.cycle,
                           row_number() OVER (PARTITION BY a.pair
                                              ORDER BY p.ended_at DESC, p.run_id DESC, p.cycle DESC) AS n
                    FROM ranked a CROSS JOIN LATERAL (
                        SELECT c.run_id, c.cycle, c.ended_at FROM ds.cycles c
                        WHERE c.readiness->>'pair' = a.pair AND c.readiness->>'spec_id' = a.spec
                          AND {_EVIDENCE}
                          AND c.ended_at <= coalesce(a.until, 'infinity')
                        ORDER BY c.ended_at DESC LIMIT 10) p
                    WHERE a.n <= 10) t
                WHERE t.n <= 10
                UNION ALL
                SELECT g.gate, a.pair, f.run_id, f.cycle, 0
                FROM bounds a CROSS JOIN (VALUES ('critical'), ('targets')) AS g (gate)
                CROSS JOIN LATERAL ({newer}) f
                UNION ALL
                SELECT 'newest', a.pair, p.run_id, p.cycle, p.cycles FROM asked a CROSS JOIN LATERAL (
                    SELECT c.run_id, c.cycle,
                           (SELECT count(*) FROM (SELECT 1 FROM ds.cycles c WHERE {measured}
                                                  ORDER BY c.ended_at DESC LIMIT %(most)s) m) AS cycles
                    FROM ds.cycles c
                    WHERE {measured}
                      AND NOT EXISTS (SELECT 1 FROM ranked v WHERE v.pair = a.pair)
                    ORDER BY c.ended_at DESC LIMIT 1) p)
            SELECT f.kind, f.pair, f.n, c.*
            FROM found f CROSS JOIN LATERAL (
                SELECT c.run_id, c.cycle, c.ended_at,
                       CASE WHEN f.kind <> 'point' OR %(whole)s THEN c.readiness END AS readiness,
                       CASE WHEN f.kind IN ('critical', 'targets') OR (f.kind = 'version' AND f.n = 1)
                            THEN c.sample END AS sample
                FROM ds.cycles c WHERE (c.run_id, c.cycle) = (f.run_id, f.cycle) OFFSET 0) c
            ORDER BY f.pair, f.kind, f.n, c.ended_at DESC, c.run_id DESC, c.cycle DESC""",  # noqa: S608
        {
            "pairs": [p["pair"] for p in pairs],
            "tiers": [p["tier"] for p in pairs],
            "needed": [p["needed"] for p in pairs],
            "windows": [p["window"] for p in pairs],
            "untils": [p["until"] for p in pairs],
            "whole": whole,
            "most": COUNTED + 1,
        },
    )
    lists = {"version": "versions", "break": "breaks", "point": "points", "trend": "trend"}
    empty = {"critical": None, "targets": None, "newest": None, "cycles": 0}
    found: dict[str, dict[str, Any]] = {
        p["pair"]: {"versions": [], "breaks": [], "points": [], "trend": []} | empty for p in pairs
    }
    for row in rows:
        kind, pair, n = row.pop("kind"), row.pop("pair"), row.pop("n")
        if kind in lists:
            found[pair][lists[kind]].append(row)
        else:
            found[pair] |= {kind: row} | ({"cycles": n} if kind == "newest" else {})
    return found


async def readiness_governance(conn: Conn, pairs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """What each of ``pairs`` (``{pair, spec_id, after, until, run_id, cycle}``: its current version, when
    its streak began, None for ever, the end it is judged up to, None for now, and its evidence cycle) has
    stored of governance and evidence, in one statement:

    - ``signoffs``: of the pair's sign-offs requested, approved or revoked, the newest of each status in
      the streak, of the current version outside it and of other versions, of evidence cycles and of
      others apart, and every one of the candidate cycles: ``{approval_id, status, run_id, cycle, spec_id,
      version, ended_at, decided_at, revoked_at, evidence, readiness}`` (``evidence``: its cycle is an
      evidence point, which alone is in a streak; ``readiness``: the measures of one that is not);
    - ``cycles``: the cycles the candidate is chosen from, evidence points all: those of the streak's
      newest approved sign-off and open request, its newest cycle holding a passed adjudication a person
      scored or a labelling a person labelled, and the evidence cycle. Each ``{run_id, cycle, ended_at,
      spec_id, readiness, sample, started, worked, cited, adjudication, labelling, packs}``: the auth mode
      its run ``started`` in; whether it holds such work; the spec approval a sign-off would ``cite`` and
      its status; its newest ``adjudication`` a person scored and why it did not pass; its ``labelling``,
      the one its open or approved sign-off attests (``attested``: that sign-off), else the newest a
      person labelled, else the newest labelled, as ``accuracy.gate`` reads it, without its actors' names;
      and its ``packs``, newest first;
    - ``soaks``: the live soaks of the current version, with their spec's ``execution`` (no compile)."""
    rows = await _rows(
        conn,
        """WITH asked AS (
               SELECT * FROM unnest(%(pairs)s::text[], %(specs)s::bigint[], %(afters)s::timestamptz[],
                                    %(untils)s::timestamptz[], %(runs)s::text[], %(cycles)s::int[])
                   AS a (pair, spec_id, after, until, run_id, cycle)),
           signoffs AS (
               SELECT a.pair, g.approval_id, g.status, g.run_id, g.cycle, g.spec_id, s.version, c.ended_at,
                      g.decided_at, g.revoked_at, e.evidence, h.held,
                      row_number() OVER (PARTITION BY a.pair, g.status, g.spec_id = a.spec_id, e.evidence,
                                                      h.held
                                         ORDER BY c.ended_at DESC, g.approval_id DESC) AS n
               FROM asked a JOIN ds.specs s ON s.name = a.pair
               JOIN ds.approvals g ON g.spec_id = s.spec_id AND g.kind = 'signoff'
                                  AND g.status IN ('requested', 'approved', 'revoked')
               JOIN ds.cycles c ON (c.run_id, c.cycle) = (g.run_id, g.cycle),
               LATERAL (SELECT coalesce(c.readiness @> '{"evidence": true, "format": 1}', false))
                   e (evidence),
               LATERAL (SELECT e.evidence AND g.spec_id = a.spec_id
                               AND c.ended_at > coalesce(a.after, '-infinity')) h (held)
               WHERE c.ended_at <= coalesce(a.until, 'infinity')),
           work AS (
               SELECT j.run_id, j.cycle FROM ds.adjudications j WHERE j.passed AND j.scored_by LIKE 'user:%%'
               UNION
               SELECT v.run_id, v.cycle FROM ds.labellings v WHERE v.labelled_by LIKE 'user:%%'),
           worked AS (
               SELECT DISTINCT ON (a.pair) a.pair, c.run_id, c.cycle
               FROM asked a JOIN ds.runs r ON r.spec_id = a.spec_id JOIN work w ON w.run_id = r.run_id
               JOIN ds.cycles c ON (c.run_id, c.cycle) = (w.run_id, w.cycle)
               WHERE c.readiness @> '{"evidence": true, "format": 1}'
                 AND c.ended_at > coalesce(a.after, '-infinity')
                 AND c.ended_at <= coalesce(a.until, 'infinity')
               ORDER BY a.pair, c.ended_at DESC, c.run_id DESC, c.cycle DESC),
           candidates AS (
               SELECT pair, run_id, cycle FROM signoffs WHERE n = 1 AND held AND status <> 'revoked'
               UNION SELECT pair, run_id, cycle FROM worked
               UNION SELECT pair, run_id, cycle FROM asked WHERE run_id IS NOT NULL)
           SELECT 'signoffs' AS kind, s.pair, jsonb_build_object(
                      'approval_id', s.approval_id, 'status', s.status, 'run_id', s.run_id, 'cycle', s.cycle,
                      'spec_id', s.spec_id, 'version', s.version, 'ended_at', s.ended_at,
                      'decided_at', s.decided_at, 'revoked_at', s.revoked_at, 'evidence', s.evidence,
                      'readiness', CASE WHEN NOT s.evidence THEN (
                          SELECT c.readiness FROM ds.cycles c
                          WHERE (c.run_id, c.cycle) = (s.run_id, s.cycle)) END) AS facts
           FROM signoffs s
           WHERE s.n = 1 OR EXISTS (SELECT 1 FROM candidates k
                                    WHERE (k.pair, k.run_id, k.cycle) = (s.pair, s.run_id, s.cycle))
           UNION ALL
           SELECT 'cycles', k.pair, jsonb_build_object(
                      'run_id', c.run_id, 'cycle', c.cycle, 'ended_at', c.ended_at, 'spec_id', r.spec_id,
                      'readiness', c.readiness, 'sample', c.sample,
                      'started', r.params->'approval'->>'mode',
                      'worked', EXISTS (SELECT 1 FROM work w WHERE (w.run_id, w.cycle) = (c.run_id, c.cycle)),
                      'cited', (
                          SELECT jsonb_build_object('approval_id', p.approval_id, 'status', p.status)
                          FROM ds.approvals p
                          WHERE p.approval_id = coalesce(c.approval_id, CASE
                              WHEN c.readiness->'needs_approval' = '[]' THEN (
                                  SELECT a.approval_id FROM ds.approvals a
                                  WHERE a.spec_id = r.spec_id AND a.kind = 'spec' AND a.status = 'approved')
                              END)),
                      'adjudication', (
                          SELECT jsonb_build_object('adjudication_id', j.adjudication_id, 'passed', j.passed,
                                                    'reasons', j.score->'exit'->'reasons')
                          FROM ds.adjudications j
                          WHERE (j.run_id, j.cycle) = (c.run_id, c.cycle) AND j.scored_at IS NOT NULL
                            AND j.scored_by LIKE 'user:%%'
                          ORDER BY j.adjudication_id DESC LIMIT 1),
                      'labelling', (
                          SELECT jsonb_build_object(
                              'labelling_id', v.labelling_id, 'run_id', v.run_id, 'cycle', v.cycle,
                              'drawn_at', v.drawn_at, 'labelled_at', v.labelled_at,
                              'labelled_by', v.labelled_by,
                              'truth_source', v.truth_source, 'truth_as_of', v.truth_as_of,
                              'role_a', s.body #>> '{sides,a,role}', 'role_b', s.body #>> '{sides,b,role}',
                              'accuracy', v.accuracy #- '{discrepant,by_cause}' #- '{accepted,by_rule}',
                              'attested', g.approval_id)
                          FROM ds.labellings v JOIN ds.specs s ON s.spec_id = r.spec_id
                          LEFT JOIN ds.approvals g
                               ON g.kind = 'signoff' AND (g.run_id, g.cycle) = (v.run_id, v.cycle)
                              AND g.status IN ('requested', 'approved')
                              AND g.attests->'accuracy'->>'labelling_id' = v.labelling_id
                          WHERE (v.run_id, v.cycle) = (c.run_id, c.cycle) AND v.labelled_at IS NOT NULL
                          ORDER BY g.approval_id IS NULL, v.labelled_by NOT LIKE 'user:%%',
                                   v.labelling_id DESC
                          LIMIT 1),
                      'packs', coalesce((
                          SELECT jsonb_agg(jsonb_build_object(
                                     'pack_id', p.pack_id, 'signoff_id', p.signoff_id, 'status', p.status,
                                     'issued_at', p.issued_at, 'revoked_at', p.revoked_at)
                                     ORDER BY p.pack_id DESC)
                          FROM ds.packs p WHERE (p.run_id, p.cycle) = (c.run_id, c.cycle)), '[]'))
           FROM candidates k JOIN ds.cycles c USING (run_id, cycle) JOIN ds.runs r USING (run_id)
           UNION ALL
           SELECT 'soaks', a.pair, jsonb_build_object(
                      'run_id', r.run_id, 'cycle', r.cycle, 'status', r.status,
                      'stop_reason', r.stop_reason, 'started_at', r.started_at,
                      'execution', s.body->'execution')
           FROM asked a JOIN ds.runs r ON r.spec_id = a.spec_id AND r.mode = 'soak'
                                      AND r.status IN ('running', 'paused')
           JOIN ds.specs s ON s.spec_id = r.spec_id""",
        {
            "pairs": [p["pair"] for p in pairs],
            "specs": [p["spec_id"] for p in pairs],
            "afters": [p["after"] for p in pairs],
            "untils": [p["until"] for p in pairs],
            "runs": [p["run_id"] for p in pairs],
            "cycles": [p["cycle"] for p in pairs],
        },
    )
    found: dict[str, dict[str, Any]] = {p["pair"]: {"signoffs": [], "cycles": [], "soaks": []} for p in pairs}
    for row in rows:
        facts = _timed(row["facts"], "ended_at", "decided_at", "revoked_at", "started_at")
        assert facts is not None
        if row["kind"] == "cycles":
            facts["labelling"] = _timed(facts["labelling"], "drawn_at", "labelled_at")
            facts["packs"] = [_timed(pack, "issued_at", "revoked_at") for pack in facts["packs"]]
        found[row["pair"]][row["kind"]].append(facts)
    return found


async def last_readiness(conn: Conn, pairs: Sequence[str], *, at: datetime | None = None) -> dict[str, Row]:
    """Each pair's newest ``readiness.changed`` row (a LATERAL on audit_log_object_idx), or its newest
    written by ``at``: its ``id``, ``at``, ``detail``, chain ``seq``, the ``spec_id`` of its evidence
    cycle (None without one), which tells a changed version, and ``since``, when the pair entered the
    state it records: the ``at`` of its newest row by then that changed the state (the rows since changed
    a gate only); a pair without one is left out."""
    rows = await _rows(
        conn,
        """SELECT p.pair, l.id, l.at, l.detail, (SELECT c.seq FROM ds.audit_chain c WHERE c.id = l.id) AS seq,
                  (SELECT (c.readiness->>'spec_id')::bigint FROM ds.cycles c
                   WHERE c.run_id = l.detail #>> '{evidence,run_id}'
                     AND c.cycle = (l.detail #>> '{evidence,cycle}')::int) AS spec_id,
                  (SELECT e.at FROM ds.audit_log e
                   WHERE e.object = 'readiness:' || p.pair AND e.action = 'readiness.changed'
                     AND e.id <= l.id AND e.detail->>'from' IS DISTINCT FROM e.detail->>'to'
                   ORDER BY e.id DESC LIMIT 1) AS since
           FROM unnest(%(pairs)s::text[]) AS p (pair) CROSS JOIN LATERAL (
               SELECT id, at, detail FROM ds.audit_log
               WHERE object = 'readiness:' || p.pair AND action = 'readiness.changed'
                 AND (%(at)s::timestamptz IS NULL OR at <= %(at)s)
               ORDER BY id DESC LIMIT 1) l""",
        {"pairs": list(pairs), "at": at},
    )
    return {row.pop("pair"): row for row in rows}


async def readiness_pairs(conn: Conn, names: Sequence[str]) -> Row:
    """The database's ``now`` and the ``pairs`` readiness judges that are stored: the spec names with
    a batch or soak run that read no captured samples, and those of ``names`` (the settings', or one asked
    for) a stored spec carries."""
    return await _row(  # type: ignore[return-value]
        conn,
        """SELECT now() AS now, ARRAY(
               SELECT DISTINCT s.name FROM ds.specs s
               WHERE s.name = ANY(%s) OR EXISTS (
                   SELECT 1 FROM ds.runs r
                   WHERE r.spec_id = s.spec_id AND r.mode IN ('batch', 'soak')
                     AND r.params->>'samples_dir' IS NULL)
               ORDER BY s.name) AS pairs""",
        (list(names),),
    )


async def readiness_entered(conn: Conn, after: int, actions: Sequence[str]) -> Row:
    """What entered the audit chain after row ``after``: its ``head`` now (``after`` when nothing
    did) and the last row of ``actions`` (``seq``; None without one), which tells an evaluation is due.
    The chain's seq is commit order, so a row committed after one of a higher id is still found."""
    return await _row(  # type: ignore[return-value]
        conn,
        """SELECT coalesce(max(c.seq), %(after)s) AS head,
                  max(c.seq) FILTER (WHERE l.action = ANY(%(actions)s)) AS seq
           FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id WHERE c.seq > %(after)s""",
        {"after": after, "actions": list(actions)},
    )


async def readiness_transitions(
    conn: Conn, *, pair: str | None = None, programme: str | None = None, limit: int = 100,
    cursor: str | None = None,
) -> Page:  # fmt: skip
    """The ``readiness.changed`` rows, newest first, of one pair or one programme's pairs if given: each
    row's ``id``, chain ``seq``, ``at`` and ``detail`` (ids, states, counts and slugs: no actor, address or
    request data)."""
    before = _after(cursor, 1)[0]
    rows = await _rows(
        conn,
        """SELECT l.id, (SELECT c.seq FROM ds.audit_chain c WHERE c.id = l.id) AS seq, l.at, l.detail
           FROM ds.audit_log l
           WHERE l.action = 'readiness.changed' AND (%(before)s < 0 OR l.id < %(before)s)
             AND (%(pair)s::text IS NULL OR l.object = 'readiness:' || %(pair)s)
             AND (%(programme)s::text IS NULL OR l.detail->>'programme' = %(programme)s)
           ORDER BY l.id DESC LIMIT %(limit)s""",
        {"before": before, "pair": pair, "programme": programme, "limit": limit + 1},
    )
    return _page(rows, limit, "id")


# A pair's points as its trend reads them: every measured cycle, or by day or ISO week (UTC) each
# bucket's last evidence point.
_POINTS: LiteralString = """SELECT c.readiness->>'pair' AS pair, c.run_id, c.cycle, c.ended_at, c.readiness
    FROM ds.cycles c
    WHERE c.readiness->>'pair' = ANY(%(pairs)s) AND c.readiness ? 'pair'
      AND (%(at)s::timestamptz IS NULL OR (c.ended_at, c.run_id, c.cycle) < (%(at)s, %(run)s, %(cycle)s))
    ORDER BY 1, c.ended_at DESC, c.run_id DESC, c.cycle DESC"""
_BUCKETS: LiteralString = """SELECT DISTINCT ON (1, date_trunc(%(bucket)s, c.ended_at, 'UTC'))
           c.readiness->>'pair' AS pair, c.run_id, c.cycle, c.ended_at, c.readiness
    FROM ds.cycles c
    WHERE c.readiness->>'pair' = ANY(%(pairs)s) AND c.readiness ? 'pair'
      AND c.readiness @> '{"evidence": true, "format": 1}'
      AND (%(at)s::timestamptz IS NULL OR c.ended_at < date_trunc(%(bucket)s, %(at)s::timestamptz, 'UTC'))
    ORDER BY 1, date_trunc(%(bucket)s, c.ended_at, 'UTC') DESC, c.ended_at DESC, c.run_id DESC,
             c.cycle DESC"""


def _points(pairs: Sequence[str], bucket: str, before: tuple[datetime, str, int] | None) -> dict[str, Any]:
    at, run_id, cycle = before or (None, None, None)
    return {"pairs": list(pairs), "bucket": bucket, "at": at, "run": run_id, "cycle": cycle}


async def readiness_points(
    conn: Conn,
    pairs: Sequence[str],
    *,
    bucket: str = "cycle",
    before: tuple[datetime, str, int] | None = None,
    limit: int | None = None,
) -> list[Row]:
    """The points of ``pairs`` (``{pair, run_id, cycle, ended_at, readiness}``), by pair and newest first:
    every measured cycle, or by ``day`` or ``week`` each bucket's last evidence point; those before the
    point ``before`` (``(ended_at, run_id, cycle)``; by bucket, before its bucket), ``limit`` at most. One
    range of cycles_readiness_pair_idx a pair."""
    query = _POINTS if bucket == "cycle" else _BUCKETS
    return await _rows(conn, query + " LIMIT %(limit)s", _points(pairs, bucket, before) | {"limit": limit})


async def count_points(conn: Conn, pairs: Sequence[str], *, bucket: str = "cycle") -> int:
    """How many points ``readiness_points`` gives of ``pairs``, counted on the index."""
    query = _POINTS if bucket == "cycle" else _BUCKETS
    counted: LiteralString = "SELECT count(*) FROM (" + query + ") p"  # noqa: S608 - fixed SQL fragments only
    return await _value(conn, counted, _points(pairs, bucket, None))


async def brief_points(conn: Conn, pair: str, until: datetime | None, limit: int) -> list[Row]:
    """The pair's newest measured cycles ended by ``until`` (None: now), newest first, ``limit`` at most,
    as its brief reads them: each ``{run_id, cycle, ended_at, readiness, counters, sample, purging_at,
    root_causes}``, the last how many root-cause groups it holds, None once its purge started (they go
    with it). One range of cycles_readiness_pair_idx, then each cycle found counted by its key."""
    return await _rows(
        conn,
        """SELECT p.*, CASE WHEN p.purging_at IS NULL THEN (
                      SELECT count(*) FROM ds.root_causes g
                      WHERE (g.run_id, g.cycle) = (p.run_id, p.cycle)) END AS root_causes
           FROM (SELECT c.run_id, c.cycle, c.ended_at, c.readiness, c.counters, c.sample, c.purging_at
                 FROM ds.cycles c
                 WHERE c.readiness->>'pair' = %(pair)s AND c.readiness ? 'pair'
                   AND c.ended_at <= coalesce(%(until)s::timestamptz, 'infinity')
                 ORDER BY c.ended_at DESC, c.run_id DESC, c.cycle DESC LIMIT %(limit)s) p
           ORDER BY p.ended_at DESC, p.run_id DESC, p.cycle DESC""",
        {"pair": pair, "until": until, "limit": limit},
    )


async def brief_causes(conn: Conn, cycles: Sequence[Mapping[str, Any]], limit: int | None) -> Row:
    """The root-cause groups of ``cycles`` (``{run_id, cycle}``, oldest first: a brief's complete cycles
    whose groups are kept, the last of them its reported one), in one statement that groups their rows by
    ``root_cause_id``: how many groups there are (``total``), how many the last cycle ``opened`` (in it, in
    no earlier one) and how many are ``still_open`` (in it and in an earlier one), the rest being in
    earlier ones only; and ``items``, ``limit`` of them (None: every one): the last cycle's first, in its
    rank order, then the others by the last cycle holding them, newest first, then by id. Each
    ``{root_cause_id, first, last, seen, max_severity, taxonomy_code, change_type, affected_keys}``: the
    places in ``cycles`` (from 1) of the first and the last cycle holding it, how many hold it, and what
    its row of that last one says, read by its key. No path, key or member token is read. Each cycle's
    groups are read by its key (``OFFSET 0`` keeps that a probe: the cost is those cycles' rows, whatever
    the table holds)."""
    rows = await _rows(
        conn,
        """WITH asked AS (
               SELECT * FROM unnest(%(runs)s::text[], %(cycles)s::int[]) WITH ORDINALITY
                   AS a (run_id, cycle, n)),
           groups AS (
               SELECT g.root_cause_id, min(a.n) AS first, max(a.n) AS last, count(*) AS seen,
                      min(g.seq) FILTER (WHERE a.n = %(newest)s) AS seq
               FROM asked a CROSS JOIN LATERAL (
                   SELECT g.root_cause_id, g.seq FROM ds.root_causes g
                   WHERE (g.run_id, g.cycle) = (a.run_id, a.cycle) OFFSET 0) g
               GROUP BY g.root_cause_id),
           listed AS (
               SELECT g.*, count(*) OVER () AS total,
                      count(*) FILTER (WHERE g.first = %(newest)s) OVER () AS opened,
                      count(*) FILTER (WHERE g.last = %(newest)s AND g.first < %(newest)s) OVER ()
                          AS still_open
               FROM groups g
               ORDER BY g.seq NULLS LAST, g.last DESC, g.root_cause_id LIMIT %(limit)s)
           SELECT l.*, c.max_severity, c.taxonomy_code, c.change_type, c.affected_keys
           FROM listed l JOIN asked a ON a.n = l.last
           CROSS JOIN LATERAL (
               SELECT c.max_severity, c.taxonomy_code, c.change_type, c.affected_keys
               FROM ds.root_causes c
               WHERE (c.run_id, c.cycle, c.root_cause_id) = (a.run_id, a.cycle, l.root_cause_id)
               OFFSET 0) c
           ORDER BY l.seq NULLS LAST, l.last DESC, l.root_cause_id""",
        {
            "runs": [c["run_id"] for c in cycles],
            "cycles": [c["cycle"] for c in cycles],
            "newest": len(cycles),
            "limit": limit,
        },
    )
    counted = {name: rows[0][name] if rows else 0 for name in ("total", "opened", "still_open")}
    names = (
        "root_cause_id", "first", "last", "seen", "max_severity", "taxonomy_code", "change_type",
        "affected_keys",
    )  # fmt: skip
    return counted | {"items": [{name: row[name] for name in names} for row in rows]}


async def readiness_measured(conn: Conn) -> AsyncIterator[Row]:
    """Every cycle holding readiness measures or a marker, and every cycle a ``readiness.measured`` row
    names, streamed (``ds readiness verify``): its ``readiness`` (None: a named cycle that holds none, or
    is gone), its run's ``mode`` and ``samples_dir``, and of the ``readiness.measured`` rows naming it how
    many (``rows``; 0 without one) and the newest one's hash (``sha256``) and chain ``seq``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        async for row in cur.stream(
            """WITH named AS (
                   SELECT e->>0 AS run_id, (e->>1)::int AS cycle, count(*) AS rows, max(l.id) AS id,
                          (array_agg(e->>2 ORDER BY l.id DESC))[1] AS sha256
                   FROM ds.audit_log l CROSS JOIN LATERAL jsonb_array_elements(l.detail->'cycles') e
                   WHERE l.action = 'readiness.measured' GROUP BY 1, 2),
               held AS (
                   SELECT c.run_id, c.cycle, c.readiness, r.mode, r.params->>'samples_dir' AS samples_dir
                   FROM ds.cycles c JOIN ds.runs r ON r.run_id = c.run_id WHERE c.readiness IS NOT NULL)
               SELECT run_id, cycle, c.readiness, c.mode, c.samples_dir, coalesce(n.rows, 0) AS rows,
                      n.sha256, (SELECT h.seq FROM ds.audit_chain h WHERE h.id = n.id) AS seq
               FROM held c FULL JOIN named n USING (run_id, cycle) ORDER BY run_id, cycle"""
        ):
            yield row


# -- schedules -----------------------------------------------------

# A schedule with its ``state``, the database's ``now``, its actors' names, what a firing would run now
# (``runs_spec_id``, ``runs_version``: its pinned version, else the name's newest), the name's newest stored
# version (``newest_spec_id``, ``newest_version``) and the status of the newest run it started.
_SCHEDULE: LiteralString = """SELECT s.*, now() AS now,
        CASE WHEN s.deleted_at IS NOT NULL THEN 'deleted' WHEN s.paused_at IS NOT NULL THEN 'paused'
             WHEN s.next_due IS NULL THEN 'ended' ELSE 'active' END AS state,
        ds.actor_name(s.created_by) AS created_by_name, ds.actor_name(s.set_by) AS set_by_name,
        ds.actor_name(s.paused_by) AS paused_by_name, v.spec_id AS runs_spec_id, v.version AS runs_version,
        n.spec_id AS newest_spec_id, n.version AS newest_version, r.status AS last_run_status
    FROM ds.schedules s
    LEFT JOIN LATERAL (SELECT spec_id, version FROM ds.specs WHERE name = s.spec_name
                       ORDER BY spec_id DESC LIMIT 1) n ON true
    LEFT JOIN ds.specs v ON v.spec_id = coalesce(s.spec_id, n.spec_id)
    LEFT JOIN ds.runs r ON r.run_id = s.last_run_id"""
# What a person's write does to what the schedule held and last settled (``last``: None keeps it).
_SETTLED: LiteralString = """held = NULL, last = coalesce(%(last)s, last),
    last_at = CASE WHEN %(last)s::jsonb IS NULL THEN last_at ELSE now() END"""
FIRINGS = ("schedule.fire", "schedule.missed", "schedule.rezone")  # the audit rows a schedule's history is
_SCHEDULES_LOCK = 0x44535343  # pg advisory lock "DSSC": creates of schedules, counted against their limit


def new_schedule_id() -> str:
    return _sortable_id("SC-")


async def insert_schedule(conn: Conn, schedule: Mapping[str, Any]) -> Row:
    """Store an active schedule, set by its creator: schedule_id, name, spec_name, spec_id (None: the
    name is followed), run, cron, time_zone, start_within, ends_at, auth_mode, created_by, anchor_at and
    next_due. The stored row."""
    return await _row(  # type: ignore[return-value]
        conn,
        """INSERT INTO ds.schedules (schedule_id, name, spec_name, spec_id, run, cron, time_zone,
               start_within, ends_at, auth_mode, created_by, set_by, anchor_at, next_due)
           VALUES (%(schedule_id)s, %(name)s, %(spec_name)s, %(spec_id)s, %(run)s, %(cron)s, %(time_zone)s,
               %(start_within)s, %(ends_at)s, %(auth_mode)s, %(created_by)s, %(created_by)s, %(anchor_at)s,
               %(next_due)s)
           RETURNING *""",
        dict(schedule) | {"run": _jsonb(schedule["run"])},
    )


async def get_schedule(conn: Conn, ident: str, *, lock: bool = False) -> Row | None:
    """The schedule of that id, or the live one of that name (``_SCHEDULE``); ``lock`` takes its row lock
    first, until the transaction ends."""
    where: LiteralString = "schedule_id = %(ident)s OR (name = %(ident)s AND deleted_at IS NULL)"
    if lock:
        await conn.execute(f"SELECT 1 FROM ds.schedules WHERE {where} FOR UPDATE", {"ident": ident})  # noqa: S608
    return await _row(
        conn,
        f"{_SCHEDULE} WHERE s.schedule_id = %(ident)s OR (s.name = %(ident)s AND s.deleted_at IS NULL)",
        {"ident": ident},
    )


async def list_schedules(
    conn: Conn,
    *,
    spec_name: str | None = None,
    state: str | None = None,
    set_by: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """The schedules newest first (``_SCHEDULE``): one join, no spec compiled. Those of ``state`` (active,
    paused, ended or deleted; None: all but the deleted), of the spec name and last saved by ``set_by``,
    if given."""
    rows = await _rows(
        conn,
        f"""SELECT * FROM ({_SCHEDULE}) s
            WHERE (%(spec_name)s::text IS NULL OR spec_name = %(spec_name)s)
              AND (state = %(state)s OR (%(state)s::text IS NULL AND state <> 'deleted'))
              AND (%(set_by)s::text IS NULL OR set_by = %(set_by)s)
              AND (%(after)s::text IS NULL OR schedule_id < %(after)s)
            ORDER BY schedule_id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"spec_name": spec_name, "state": state, "set_by": set_by, "after": cursor, "limit": limit + 1},
    )
    return _page(rows, limit, "schedule_id")


async def count_schedules(conn: Conn, *, lock: bool = False) -> Row:
    """How many schedules are not deleted (``count``), and the database's ``now``; ``lock`` first takes the
    schedules lock until the transaction ends, so that creates count one at a time."""
    if lock:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEDULES_LOCK,))
    return await _row(  # type: ignore[return-value]
        conn, "SELECT count(*) AS count, now() AS now FROM ds.schedules WHERE deleted_at IS NULL"
    )


async def overdue_schedules(conn: Conn, minutes: int) -> int:
    """How many schedules were due over ``minutes`` ago and are not settled (schedules_due_idx)."""
    return await _value(
        conn,
        "SELECT count(*) FROM ds.schedules WHERE next_due <= now() - %s * interval '1 minute'",
        (minutes,),
    )


async def replace_schedule(
    conn: Conn,
    schedule_id: str,
    revision: int,
    schedule: Mapping[str, Any],
    *,
    by: str,
    last: Mapping[str, Any] | None = None,
) -> Row | None:
    """Replace the definition of the schedule seen at ``revision`` (``insert_schedule``'s members but its
    id and creator), set by ``by``; ``last``: what the write settled. None when it is deleted or was
    written since."""
    return await _row(
        conn,
        f"""UPDATE ds.schedules SET name = %(name)s, spec_name = %(spec_name)s, spec_id = %(spec_id)s,
                run = %(run)s, cron = %(cron)s, time_zone = %(time_zone)s, start_within = %(start_within)s,
                ends_at = %(ends_at)s, auth_mode = %(auth_mode)s, revision = revision + 1, set_by = %(by)s,
                set_at = now(), anchor_at = %(anchor_at)s, next_due = %(next_due)s, {_SETTLED}
            WHERE schedule_id = %(schedule_id)s AND revision = %(revision)s AND deleted_at IS NULL
            RETURNING *""",  # noqa: S608 - fixed SQL fragments only
        dict(schedule)
        | {"run": _jsonb(schedule["run"]), "schedule_id": schedule_id, "revision": revision, "by": by}
        | {"last": None if last is None else _jsonb(last)},
    )


async def pause_schedule(
    conn: Conn,
    schedule_id: str,
    by: str,
    *,
    revision: int,
    anchor: datetime | None = None,
    last: Mapping[str, Any] | None = None,
) -> Row | None:
    """Pause the schedule as ``by``, at ``revision`` (a person's write is one more, the worker's the same):
    nothing is due while it is paused. ``anchor``, ``last``: the time and outcome the pause settled. None
    when it is paused or deleted."""
    return await _row(
        conn,
        f"""UPDATE ds.schedules SET paused_by = %(by)s, paused_at = now(), next_due = NULL,
                revision = %(revision)s, anchor_at = coalesce(%(anchor)s, anchor_at), {_SETTLED}
            WHERE schedule_id = %(schedule_id)s AND paused_at IS NULL AND deleted_at IS NULL
            RETURNING *""",  # noqa: S608 - fixed SQL fragments only
        {"schedule_id": schedule_id, "by": by, "revision": revision, "anchor": anchor,
         "last": None if last is None else _jsonb(last)},
    )  # fmt: skip


async def resume_schedule(
    conn: Conn, schedule_id: str, *, anchor_at: datetime, next_due: datetime | None
) -> Row | None:
    """Resume the paused schedule, anchored at ``anchor_at`` and next due at ``next_due`` (None: it has
    ended); None when it is not paused."""
    return await _row(
        conn,
        """UPDATE ds.schedules SET paused_by = NULL, paused_at = NULL, revision = revision + 1,
               anchor_at = %s, next_due = %s
           WHERE schedule_id = %s AND paused_at IS NOT NULL AND deleted_at IS NULL RETURNING *""",
        (anchor_at, next_due, schedule_id),
    )


async def delete_schedule(
    conn: Conn,
    schedule_id: str,
    by: str,
    *,
    anchor: datetime | None = None,
    last: Mapping[str, Any] | None = None,
) -> Row | None:
    """Leave the schedule's tombstone, by ``by``: its name is free and its runs still name it. ``anchor``,
    ``last``: the time and outcome the delete settled. None when it is deleted already."""
    return await _row(
        conn,
        f"""UPDATE ds.schedules SET deleted_by = %(by)s, deleted_at = now(), next_due = NULL,
                revision = revision + 1, anchor_at = coalesce(%(anchor)s, anchor_at), {_SETTLED}
            WHERE schedule_id = %(schedule_id)s AND deleted_at IS NULL RETURNING *""",  # noqa: S608
        {"schedule_id": schedule_id, "by": by, "anchor": anchor,
         "last": None if last is None else _jsonb(last)},
    )  # fmt: skip


async def schedules_due(conn: Conn) -> list[Row]:
    """The schedules the worker looks at now, oldest due first, each with its ``next_due`` and the
    database's ``now``: those due (schedules_due_idx), and those holding a standing refusal's range whose
    first time is 24 hours old (``next_due`` may then be later, or None)."""
    return await _rows(
        conn,
        """SELECT schedule_id, next_due, now() AS now FROM ds.schedules WHERE next_due <= now()
           UNION
           SELECT schedule_id, next_due, now() FROM ds.schedules
           WHERE deleted_at IS NULL AND (held->>'due')::timestamptz <= now() - interval '24 hours'
           ORDER BY next_due, schedule_id""",
    )


async def settle_schedule(
    conn: Conn,
    schedule_id: str,
    *,
    due: datetime,
    revision: int,
    anchor: datetime,
    next_due: datetime | None,
    last: Mapping[str, Any],
    held: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    closes: datetime | None = None,
) -> int | None:
    """Settle the due time ``due`` of the schedule seen at ``revision``: ``anchor`` is the last time
    settled, ``next_due`` the one due next (None: it has ended), ``last`` the outcome's detail and
    ``held`` the standing refusal's range not yet chained. A firing gives the run it started (``run_id``)
    and its window's end (``closes``), judged by the clock as the statement runs. How many seconds after
    ``due`` it was settled (in ``last`` as ``late_s`` for a run); None, nothing moved, when that time is
    no longer due, the schedule was written, paused or deleted since, or the window has closed."""
    return await _value(
        conn,
        """UPDATE ds.schedules SET anchor_at = %(anchor)s, next_due = %(next_due)s, held = %(held)s,
               last_at = now(), last_run_id = coalesce(%(run_id)s, last_run_id),
               last = %(last)s::jsonb || CASE WHEN %(run_id)s::text IS NULL THEN '{}'::jsonb ELSE
                   jsonb_build_object('late_s', greatest(0, floor(extract(epoch FROM clock_timestamp()
                                                                          - %(due)s))::int)) END
           WHERE schedule_id = %(schedule_id)s AND next_due = %(due)s AND revision = %(revision)s
             AND paused_at IS NULL AND deleted_at IS NULL
             AND (%(closes)s::timestamptz IS NULL OR clock_timestamp() < %(closes)s)
           RETURNING greatest(0, floor(extract(epoch FROM clock_timestamp() - %(due)s))::int)""",
        {"schedule_id": schedule_id, "due": due, "revision": revision, "anchor": anchor,
         "next_due": next_due, "last": _jsonb(last), "held": None if held is None else _jsonb(held),
         "run_id": run_id, "closes": closes},
    )  # fmt: skip


async def release_held(conn: Conn, schedule_id: str, held: Mapping[str, Any]) -> bool:
    """Clear the range the schedule holds, to be chained in this transaction; False when it holds another
    by now."""
    return bool(
        await _changed(
            conn,
            "UPDATE ds.schedules SET held = NULL WHERE schedule_id = %s AND held = %s",
            (schedule_id, _jsonb(held)),
        )
    )


async def rezone_schedule(
    conn: Conn, schedule_id: str, revision: int, was: datetime, to: datetime | None
) -> bool:
    """Move the next due time of the schedule seen at ``revision`` from ``was`` to ``to``, as this build's
    tz data reads its cron; False when it was settled or written since."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.schedules SET next_due = %s
               WHERE schedule_id = %s AND revision = %s AND next_due = %s""",
            (to, schedule_id, revision, was),
        )
    )


async def setter_live(conn: Conn, set_by: str) -> bool:
    """Whether the actor who last saved a schedule still answers for it: a person is enabled and
    their stored roles hold operator; a token's accountable person, if it has one, is not disabled (its
    own expiry and revoke are not read: rotation stops nothing); any other actor is."""
    return await _value(
        conn,
        """SELECT CASE split_part(%(actor)s, ':', 1)
               WHEN 'user' THEN EXISTS (
                   SELECT 1 FROM ds.users WHERE user_id = split_part(%(actor)s, ':', 2)
                                            AND disabled_at IS NULL AND 'operator' = ANY (roles))
               WHEN 'token' THEN NOT EXISTS (
                   SELECT 1 FROM ds.api_tokens t JOIN ds.users u ON u.user_id = t.owner_user_id
                   WHERE t.token_id = split_part(%(actor)s, ':', 2) AND u.disabled_at IS NOT NULL)
               ELSE true END""",
        {"actor": set_by},
    )


async def schedule_firings(
    conn: Conn, schedule_id: str, *, limit: int = 20, cursor: str | None = None
) -> Page:
    """The schedule's ``schedule.fire``, ``schedule.missed`` and ``schedule.rezone`` rows, newest first
    (audit_log_object_idx): each row's ``id``, chain ``seq``, ``at``, ``action`` and ``detail`` (ids,
    times and closed codes: no actor, address or request data)."""
    rows = await _rows(
        conn,
        """SELECT l.id, (SELECT c.seq FROM ds.audit_chain c WHERE c.id = l.id) AS seq, l.at, l.action,
                  l.detail
           FROM ds.audit_log l
           WHERE l.object = %(object)s AND l.action = ANY(%(actions)s)
             AND (%(before)s < 0 OR l.id < %(before)s)
           ORDER BY l.id DESC LIMIT %(limit)s""",
        {"object": schedule_id, "actions": list(FIRINGS), "before": _after(cursor, 1)[0], "limit": limit + 1},
    )
    return _page(rows, limit, "id")


# -- table pre-checks ---------------------------------------------------

# A pre-check with the database's ``now``, its actors' names, when a pair of it was last settled, when a
# worker last took or renewed a running pair of it, and its pairs counted by status (``progress``) and by
# verdict (``verdicts``): no pair's ``result`` is read.
_PRECHECK: LiteralString = """SELECT p.*, now() AS now, ds.actor_name(p.created_by) AS created_by_name,
        ds.actor_name(p.cancelled_by) AS cancelled_by_name,
        (SELECT max(finished_at) FROM ds.precheck_pairs WHERE precheck_id = p.precheck_id) AS settled_at,
        (SELECT max(claimed_at) FROM ds.precheck_pairs
         WHERE precheck_id = p.precheck_id AND status = 'running') AS claimed_at,
        (SELECT coalesce(jsonb_object_agg(s.status, s.n), '{}')
         FROM (SELECT status, count(*) AS n FROM ds.precheck_pairs
               WHERE precheck_id = p.precheck_id GROUP BY status) s) AS progress,
        (SELECT coalesce(jsonb_object_agg(v.verdict, v.n), '{}')
         FROM (SELECT verdict, count(*) AS n FROM ds.precheck_pairs
               WHERE precheck_id = p.precheck_id AND verdict IS NOT NULL GROUP BY verdict) v) AS verdicts
    FROM ds.prechecks p"""
# Every column of a pair but ``result`` and ``claimed_at``.
_PAIR_COLUMNS: LiteralString = """precheck_id, n, spec_id, label, target_a, relation_a, target_b, relation_b,
    status, tries, verdict, rank, differing, rows_a, rows_b, line, finished_at"""
_PRECHECKS_LOCK = 0x44535043  # pg advisory lock "DSPC": creates of pre-checks, counted against their limit


def new_precheck_id() -> str:
    return _sortable_id("PC-")


async def precheck_specs(
    conn: Conn, *, spec_ids: Sequence[int] | None = None, names: Sequence[str] | None = None
) -> list[Row]:
    """What a pre-check pairs of stored specs, read from their bodies without compiling one: the versions
    ``spec_ids``, else the newest version of each of ``names``, else of every name. Each one's spec_id,
    name, version, each side's target (``target_a``, ``target_b``) and its ``table`` member (``table_a``,
    ``table_b``; None: a service side)."""
    return await _rows(
        conn,
        """SELECT s.spec_id, s.name, s.version, s.body->'sides'->'a'->>'target' AS target_a,
                  s.body->'sides'->'b'->>'target' AS target_b, s.body->'sides'->'a'->'table' AS table_a,
                  s.body->'sides'->'b'->'table' AS table_b
           FROM ds.specs s
           WHERE CASE WHEN %(spec_ids)s::bigint[] IS NOT NULL THEN s.spec_id = ANY(%(spec_ids)s)
                      ELSE s.spec_id IN (SELECT max(spec_id) FROM ds.specs
                                         WHERE %(names)s::text[] IS NULL OR name = ANY(%(names)s)
                                         GROUP BY name) END
           ORDER BY s.name, s.spec_id""",
        {"spec_ids": None if spec_ids is None else list(spec_ids),
         "names": None if names is None else list(names)},
    )  # fmt: skip


async def insert_precheck(conn: Conn, precheck: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]]) -> Row:
    """Store a pre-check (precheck_id, source, level, request, skipped, evaluation, created_by) and its
    pairs, each pending: n, spec_id (None: paired by name), label, target_a, relation_a, target_b and
    relation_b. A pair that comes with a ``verdict`` is settled as it is made (a name without a pair),
    with its rank, differing, line and result. The stored row."""
    stored: Row = await _row(  # type: ignore[assignment]
        conn,
        """INSERT INTO ds.prechecks (precheck_id, source, level, request, pairs, skipped, evaluation,
               created_by)
           VALUES (%(precheck_id)s, %(source)s, %(level)s, %(request)s, %(pairs)s, %(skipped)s,
               %(evaluation)s, %(created_by)s)
           RETURNING *""",
        dict(precheck) | {"request": _jsonb(precheck["request"]), "pairs": len(pairs)},
    )
    settled = dict.fromkeys(("verdict", "rank", "differing", "line", "result"))
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.precheck_pairs (precheck_id, n, spec_id, label, target_a, relation_a, target_b,
                   relation_b, status, verdict, rank, differing, line, result, finished_at)
               VALUES (%(precheck_id)s, %(n)s, %(spec_id)s, %(label)s, %(target_a)s, %(relation_a)s,
                   %(target_b)s, %(relation_b)s,
                   CASE WHEN %(verdict)s::text IS NULL THEN 'pending' ELSE 'done' END, %(verdict)s, %(rank)s,
                   %(differing)s, %(line)s, %(result)s,
                   CASE WHEN %(verdict)s::text IS NULL THEN NULL ELSE now() END)""",
            [
                settled
                | dict(pair)
                | {"precheck_id": precheck["precheck_id"]}
                | {"result": _jsonb(pair["result"]) if pair.get("result") is not None else None}
                for pair in pairs
            ],
        )
    return stored


async def get_precheck(conn: Conn, precheck_id: str) -> Row | None:
    """The pre-check with its pairs counted (``_PRECHECK``)."""
    return await _row(conn, f"{_PRECHECK} WHERE p.precheck_id = %s", (precheck_id,))


async def list_prechecks(conn: Conn, *, limit: int = 50, cursor: str | None = None) -> Page:
    """The pre-checks newest first, each with its pairs counted (``_PRECHECK``)."""
    rows = await _rows(
        conn,
        f"""{_PRECHECK} WHERE (%(after)s::text IS NULL OR p.precheck_id < %(after)s)
            ORDER BY p.precheck_id DESC LIMIT %(limit)s""",
        {"after": cursor, "limit": limit + 1},
    )
    return _page(rows, limit, "precheck_id")


async def count_open_prechecks(conn: Conn, *, lock: bool = False) -> int:
    """How many pre-checks are unfinished (prechecks_open_idx); ``lock`` first takes the pre-checks lock
    until the transaction ends, so that creates count one at a time."""
    if lock:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PRECHECKS_LOCK,))
    return await _value(conn, "SELECT count(*) FROM ds.prechecks WHERE finished_at IS NULL", None)


async def precheck_pairs(
    conn: Conn,
    precheck_id: str,
    *,
    verdict: str | None = None,
    results: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """The pre-check's pairs worst first (precheck_pairs_worst_idx: by rank, then the most differing
    figures, then label; a pair not settled comes last), without their ``result`` unless ``results`` (a
    report's); those of ``verdict``, if given. The cursor is a position in that order, which a pair
    settled between two pages moves."""
    start = max(_after(cursor, 1)[0], 0)
    columns: LiteralString = _PAIR_COLUMNS + (", result" if results else "")
    rows = await _rows(
        conn,
        f"""SELECT {columns} FROM ds.precheck_pairs
            WHERE precheck_id = %(precheck_id)s AND (%(verdict)s::text IS NULL OR verdict = %(verdict)s)
            ORDER BY rank, differing DESC, label, n LIMIT %(limit)s OFFSET %(start)s""",  # noqa: S608
        {"precheck_id": precheck_id, "verdict": verdict, "limit": limit + 1, "start": start},
    )
    return rows[:limit], str(start + limit) if len(rows) > limit else None


async def precheck_pair(conn: Conn, precheck_id: str, n: int) -> Row | None:
    """One pair, with its ``result``."""
    return await _row(
        conn, "SELECT * FROM ds.precheck_pairs WHERE precheck_id = %s AND n = %s", (precheck_id, n)
    )


async def prechecks_due(conn: Conn) -> list[Row]:
    """The pairs the worker has to read or is reading (precheck_pairs_due_idx), the oldest pre-check's
    first: each one's precheck_id, n, status, tries and two targets. A pair of a cancelled pre-check is
    not among them."""
    return await _rows(
        conn,
        """SELECT precheck_id, n, status, tries, target_a, target_b FROM ds.precheck_pairs
           WHERE status IN ('pending', 'running') ORDER BY precheck_id, n""",
    )


async def claim_pair(conn: Conn, precheck_id: str, n: int) -> Row | None:
    """Take the pending pair of an unfinished pre-check to read it (one more of its ``tries``, claimed now):
    the pair without its ``result``, with its pre-check's ``source``, ``level`` and ``request`` (the schemas
    of two paired by name). None when it is not pending."""
    return await _row(
        conn,
        """UPDATE ds.precheck_pairs q SET status = 'running', tries = q.tries + 1, claimed_at = now()
           FROM ds.prechecks p
           WHERE q.precheck_id = %s AND q.n = %s AND q.status = 'pending'
             AND p.precheck_id = q.precheck_id AND p.finished_at IS NULL
           RETURNING q.precheck_id, q.n, q.spec_id, q.label, q.target_a, q.relation_a, q.target_b,
                     q.relation_b, q.tries, p.source, p.level, p.request""",
        (precheck_id, n),
    )


async def hold_pair(conn: Conn, precheck_id: str, n: int) -> None:
    """Renew the claim of the running pair: a worker reads it still."""
    await conn.execute(
        """UPDATE ds.precheck_pairs SET claimed_at = now()
           WHERE precheck_id = %s AND n = %s AND status = 'running'""",
        (precheck_id, n),
    )


async def save_pair(
    conn: Conn,
    precheck_id: str,
    n: int,
    *,
    verdict: str,
    rank: int,
    differing: int,
    line: str,
    result: Mapping[str, Any],
    rows_a: int | None = None,
    rows_b: int | None = None,
) -> bool:
    """Settle the running pair with what was read of it, replacing its result whole, in one statement.
    It takes the pre-check's row lock first, until the transaction ends, so that ``finish_precheck`` in
    the same transaction sees every pair another worker task settled. False, nothing written, when the
    pair is no longer running (its pre-check was cancelled, or its items were requeued)."""
    settled = await _changed(
        conn,
        """WITH p AS (SELECT precheck_id FROM ds.prechecks WHERE precheck_id = %(precheck_id)s FOR UPDATE)
           UPDATE ds.precheck_pairs q SET status = 'done', verdict = %(verdict)s, rank = %(rank)s,
               differing = %(differing)s, rows_a = %(rows_a)s, rows_b = %(rows_b)s, line = %(line)s,
               result = %(result)s, finished_at = now()
           FROM p WHERE q.precheck_id = p.precheck_id AND q.n = %(n)s AND q.status = 'running'""",
        {"precheck_id": precheck_id, "n": n, "verdict": verdict, "rank": rank, "differing": differing,
         "rows_a": rows_a, "rows_b": rows_b, "line": line, "result": _jsonb(result)},
    )  # fmt: skip
    return bool(settled)


async def settle_target(
    conn: Conn,
    target: str,
    verdict: str,
    rank: int,
    a: tuple[str, Mapping[str, Any]],
    b: tuple[str, Mapping[str, Any]],
    *,
    precheck_id: str | None = None,
) -> list[Row]:
    """Settle, in one statement, every pending pair that names ``target`` of every unfinished pre-check
    (of ``precheck_id`` alone, if given), unread: ``a`` is the line and result of a pair whose side a is
    on the target, ``b`` those of any other. It takes those pre-checks' row locks first, as ``save_pair``
    does. Each settled pair's precheck_id and n."""
    return await _rows(
        conn,
        """WITH p AS (SELECT precheck_id FROM ds.prechecks
                      WHERE finished_at IS NULL
                        AND (%(precheck_id)s::text IS NULL OR precheck_id = %(precheck_id)s)
                      ORDER BY precheck_id FOR UPDATE)
           UPDATE ds.precheck_pairs q SET status = 'done', verdict = %(verdict)s, rank = %(rank)s,
               differing = 0, finished_at = now(),
               line = CASE WHEN q.target_a = %(target)s THEN %(line_a)s ELSE %(line_b)s END,
               result = CASE WHEN q.target_a = %(target)s THEN %(result_a)s::jsonb
                             ELSE %(result_b)s::jsonb END
           FROM p
           WHERE q.precheck_id = p.precheck_id AND q.status = 'pending'
             AND %(target)s IN (q.target_a, q.target_b)
           RETURNING q.precheck_id, q.n""",
        {"precheck_id": precheck_id, "target": target, "verdict": verdict, "rank": rank,
         "line_a": a[0], "result_a": _jsonb(a[1]), "line_b": b[0], "result_b": _jsonb(b[1])},
    )  # fmt: skip


async def requeue_prechecks(conn: Conn, pairs: Sequence[tuple[str, int]] | None = None) -> int:
    """Set every running pair back to pending, to be read again: the worker that read them is gone (this
    one has just taken the worker lock). With ``pairs`` (each a precheck_id and n), those of them that
    are running: no task of this worker reads them, or this worker stops reading them. How many."""
    ids, ns = [p for p, _ in pairs or ()], [n for _, n in pairs or ()]
    return await _changed(
        conn,
        """UPDATE ds.precheck_pairs SET status = 'pending'
           WHERE status = 'running' AND (%(all)s OR (precheck_id, n) IN (
               SELECT * FROM unnest(%(ids)s::text[], %(ns)s::int[])))""",
        {"all": pairs is None, "ids": ids, "ns": ns},
    )


async def cancel_precheck(conn: Conn, precheck_id: str, by: str, line: str) -> Row | None:
    """Cancel the unfinished pre-check as ``by``: it ends now, and its pending and running pairs are
    cancelled, each saying ``line``. Its ``cancelled_at`` and how many pairs were ``pending`` and
    ``running``; None when it has ended, or does not exist."""
    cancelled = await _row(
        conn,
        """UPDATE ds.prechecks SET cancelled_by = %s, cancelled_at = now(), finished_at = now()
           WHERE precheck_id = %s AND finished_at IS NULL RETURNING precheck_id, cancelled_at""",
        (by, precheck_id),
    )
    if cancelled is None:
        return None
    counts = await _row(
        conn,
        """WITH open AS (SELECT n, status FROM ds.precheck_pairs
                         WHERE precheck_id = %(precheck_id)s AND status IN ('pending', 'running') FOR UPDATE),
                done AS (UPDATE ds.precheck_pairs q SET status = 'cancelled', line = %(line)s,
                             finished_at = now()
                         FROM open WHERE q.precheck_id = %(precheck_id)s AND q.n = open.n
                         RETURNING open.status)
           SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                  count(*) FILTER (WHERE status = 'running') AS running FROM done""",
        {"precheck_id": precheck_id, "line": line},
    )
    return cancelled | (counts or {})


async def finish_precheck(conn: Conn, precheck_id: str) -> Row | None:
    """End the pre-check once none of its pairs is pending or running: what ``precheck.finished`` records
    (its ``pairs``, its ``verdicts`` counted, the pairs each failure code of a side's read was met in,
    ``failed``, and the ``seconds`` since it was made). None while a pair is open, or when it has ended."""
    return await _row(
        conn,
        """UPDATE ds.prechecks p SET finished_at = now()
           WHERE p.precheck_id = %s AND p.finished_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM ds.precheck_pairs
                             WHERE precheck_id = p.precheck_id AND status IN ('pending', 'running'))
           RETURNING p.pairs, floor(extract(epoch FROM now() - p.created_at))::int AS seconds,
               (SELECT coalesce(jsonb_object_agg(v.verdict, v.n), '{}')
                FROM (SELECT verdict, count(*) AS n FROM ds.precheck_pairs
                      WHERE precheck_id = p.precheck_id AND verdict IS NOT NULL GROUP BY verdict) v
               ) AS verdicts,
               (SELECT coalesce(jsonb_object_agg(f.code, f.n), '{}')
                FROM (SELECT c.value AS code, count(DISTINCT q.n) AS n
                      FROM ds.precheck_pairs q, jsonb_each(q.result->'sides') s,
                           jsonb_each_text(s.value->'failed') c
                      WHERE q.precheck_id = p.precheck_id GROUP BY c.value) f) AS failed""",
        (precheck_id,),
    )


async def prune_prechecks(conn: Conn, days: int, limit: int = 1000) -> int:
    """Delete the pre-checks made over ``days`` days ago with their pairs (prechecks_created_idx), the
    oldest ``limit`` of them; how many. Their audit rows stay."""
    return await _changed(
        conn,
        """DELETE FROM ds.prechecks WHERE precheck_id IN (
               SELECT precheck_id FROM ds.prechecks WHERE created_at < now() - %s * interval '1 day'
               ORDER BY created_at LIMIT %s)""",
        (days, limit),
    )


# -- audit -------------------------------------------------------------------


async def insert_audit(
    conn: Conn, who: Principal, action: str, obj: str, detail: Mapping[str, Any] | None = None
) -> Row:
    """Append a row for ``who`` (masked detail only) and return it with its ``chain`` entry (``{seq,
    hash}``; None before 009, as 'ds keys migrate' writes rows on 006). Callers
    write audit rows through ``audited``, as the last statement of the transaction that does what the
    row records."""
    extra: dict[str, Any] = {} if who.via is None else {"via": who.via}
    if who.actor.startswith("local:"):  # asserted, not verified
        extra |= {"asserted": True, "os_account": who.os_account}
    row: Row = await _row(  # type: ignore[assignment]
        conn,
        f"""INSERT INTO ds.audit_log (actor, actor_name, action, object, detail, request_id, session_id,
                client_addr)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_AUDIT_COLUMNS}, to_regclass('ds.audit_chain') IS NOT NULL AS chained""",  # noqa: S608
        (
            who.actor, who.name, action, obj, _jsonb(dict(detail or {}) | extra), who.request_id,
            who.session_id, who.client_addr,
        ),
    )  # fmt: skip
    if row.pop("chained"):  # its AFTER INSERT trigger chained it, after RETURNING
        row["chain"] = await _row(conn, "SELECT seq, hash FROM ds.audit_chain WHERE id = %s", (row["id"],))
    else:
        row["chain"] = None
    return row


@asynccontextmanager
async def audited(pool: AsyncConnectionPool) -> AsyncIterator[tuple[Conn, Audit]]:
    """A pool connection and ``audit(who, action, obj, detail=None)``, which inserts an audit row.

    The rows' SIEM lines go to logger ``deltasleuth.audit`` once the transaction has committed; none
    when the block raises. The audit insert is the last statement before the commit.
    """
    rows: list[Row] = []
    async with pool.connection() as conn:

        async def audit(
            who: Principal, action: str, obj: str, detail: Mapping[str, Any] | None = None
        ) -> Row:
            rows.append(await insert_audit(conn, who, action, obj, detail))
            return rows[-1]

        yield conn, audit
    for row in rows:
        _siem.info(siem_line(row))


def siem_line(row: Row) -> str:
    """An audit row as its SIEM copy: one JSON line, with its ``chain`` entry."""
    addr = row["client_addr"]
    return jsonio.dumps(
        row | {"at": row["at"].isoformat(), "client_addr": None if addr is None else str(addr)}
    )


async def list_audit(
    conn: Conn,
    *,
    actor: str | None = None,
    action: str | None = None,
    obj: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Audit rows newest first, each filter exact (``since`` inclusive, ``until`` exclusive), each with
    its ``chain`` entry (``{seq, hash}``)."""
    before = _after(cursor, 1)[0]
    rows = await _rows(
        conn,
        f"""SELECT {_AUDIT_COLUMNS}, {_CHAIN_ENTRY} FROM ds.audit_log a
            WHERE (%(before)s < 0 OR id < %(before)s)
              AND (%(actor)s::text IS NULL OR actor = %(actor)s)
              AND (%(action)s::text IS NULL OR action = %(action)s)
              AND (%(obj)s::text IS NULL OR object = %(obj)s)
              AND (%(session_id)s::text IS NULL OR session_id = %(session_id)s)
              AND (%(since)s::timestamptz IS NULL OR at >= %(since)s)
              AND (%(until)s::timestamptz IS NULL OR at < %(until)s)
            ORDER BY id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {
            "before": before,
            "actor": actor,
            "action": action,
            "obj": obj,
            "session_id": session_id,
            "since": since,
            "until": until,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "id")


async def last_audit(conn: Conn, action: str, *, obj: str | None = None) -> Row | None:
    """The newest audit row of ``action`` (of object ``obj`` only, if given)."""
    if obj is not None:  # audit_log_object_idx
        return await _row(
            conn,
            f"""SELECT {_AUDIT_COLUMNS} FROM ds.audit_log WHERE object = %s AND action = %s
                ORDER BY id DESC LIMIT 1""",  # noqa: S608 - fixed SQL fragments only
            (obj, action),
        )
    return await _row(
        conn,
        f"SELECT {_AUDIT_COLUMNS} FROM ds.audit_log WHERE action = %s ORDER BY id DESC LIMIT 1",  # noqa: S608
        (action,),
    )


async def audit_head(conn: Conn) -> Row | None:
    """The last chain row's ``{seq, hash}``; None while the chain is empty."""
    return await _row(conn, "SELECT seq, hash FROM ds.audit_chain ORDER BY seq DESC LIMIT 1")


async def audit_upgrade_seq(conn: Conn) -> int:
    """The last row 009 chained as it applied: rows up to it were written, and
    their SIEM lines logged, before the chain existed."""
    return await _value(conn, "SELECT min(upgrade_seq) FROM ds.audit_chain_upgrade", None)


async def audit_chain(
    conn: Conn,
    *,
    after: int = 0,
    limit: int = 5000,
    upto: int | None = None,
    actions: Sequence[str] | None = None,
) -> list[Row]:
    """The chain rows after row ``after`` (up to row ``upto``, of ``actions`` only, if given), in seq order
    (``_CHAIN_COLUMNS``)."""
    return await _rows(
        conn,
        f"""SELECT {_CHAIN_COLUMNS} FROM ds.audit_chain c LEFT JOIN ds.audit_log l ON l.id = c.id
            WHERE c.seq > %(after)s AND (%(upto)s::bigint IS NULL OR c.seq <= %(upto)s)
              AND (%(actions)s::text[] IS NULL OR l.action = ANY(%(actions)s))
            ORDER BY c.seq LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"after": after, "upto": upto, "actions": None if actions is None else list(actions), "limit": limit},
    )


async def audit_segment(conn: Conn, objects: Sequence[str], head: int) -> list[Row]:
    """A pack's audit segment: the chain rows of ``objects``, each object's from
    its first, up to row ``head``, in seq order (``_CHAIN_COLUMNS``)."""
    return await _rows(
        conn,
        f"""SELECT {_CHAIN_COLUMNS} FROM ds.audit_chain c LEFT JOIN ds.audit_log l ON l.id = c.id
            WHERE c.object = ANY(%s) AND c.seq <= %s ORDER BY c.seq""",  # noqa: S608 - fixed SQL fragments only
        (list(objects), head),
    )


async def audit_rows(conn: Conn, ids: Sequence[int]) -> list[Row]:
    """The audit rows with these ids, each with its ``chain`` entry."""
    return await _rows(
        conn,
        f"SELECT {_AUDIT_COLUMNS}, {_CHAIN_ENTRY} FROM ds.audit_log a WHERE id = ANY(%s) ORDER BY id",  # noqa: S608
        (list(ids),),
    )


async def unchained_audit_row(conn: Conn) -> Row | None:
    """The first audit row without a chain entry: one written with the chain trigger disabled, or whose
    entry was deleted."""
    return await _row(
        conn,
        """SELECT id, action, object FROM ds.audit_log l
           WHERE NOT EXISTS (SELECT 1 FROM ds.audit_chain c WHERE c.id = l.id) ORDER BY id LIMIT 1""",
    )
