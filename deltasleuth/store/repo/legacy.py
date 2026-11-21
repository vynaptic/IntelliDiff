"""The queries of legacy data: what releases before 006 stored in clear, sealed by ds keys migrate."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Jsonb

from deltasleuth.store.repo.base import Conn, _value

# The selects read a batch after a keyset position (``after``: their first columns) and return tuples,
# their columns in the order they name them; each update clears the plaintext as it stores the sealed value.


async def schema_applied(conn: Conn, version: int) -> bool:
    """Whether the migration numbered ``version`` was applied."""
    cur = await conn.execute("SELECT 1 FROM ds.schema_version WHERE version = %s", (version,))
    return await cur.fetchone() is not None


async def legacy_blobs_only(conn: Conn) -> bool | None:
    """Whether observations name legacy blobs (``sha256:``) and none names a sealed one (``hk1:``); None on
    a database without observations. It reads any schema from 001 on."""
    table = await _value(conn, "SELECT to_regclass('ds.observations')", None)
    if table is None:
        return None
    named = await _value(
        conn,
        """SELECT EXISTS (SELECT 1 FROM ds.observations WHERE content_hash LIKE 'sha256:%')
                  AND NOT EXISTS (SELECT 1 FROM ds.observations WHERE content_hash LIKE 'hk1:%')""",
        None,
    )
    return bool(named)


async def legacy_runs(conn: Conn, after: tuple[Any, ...], limit: int) -> list[tuple[Any, ...]]:
    """(run_id) of the runs without a data key."""
    cur = await conn.execute(
        """SELECT run_id FROM ds.runs r
           WHERE run_id > %s AND NOT EXISTS (SELECT 1 FROM ds.data_keys k WHERE k.scope = r.run_id)
           ORDER BY run_id LIMIT %s""",
        (*after, limit),
    )
    return await cur.fetchall()


async def legacy_keys(conn: Conn, after: tuple[Any, ...], limit: int) -> list[tuple[Any, ...]]:
    """(run_id, cycle, seq, key_masked, key) of the run keys stored in clear."""
    cur = await conn.execute(
        """SELECT run_id, cycle, seq, key_masked, key FROM ds.run_keys
           WHERE (run_id, cycle, seq) > (%s, %s, %s) AND key_enc IS NULL
           ORDER BY run_id, cycle, seq LIMIT %s""",
        (*after, limit),
    )
    return await cur.fetchall()


async def legacy_headers(conn: Conn, after: tuple[Any, ...], limit: int) -> list[tuple[Any, ...]]:
    """(run_id, obs_id, cycle, headers) of the observations whose headers are stored in clear."""
    cur = await conn.execute(
        """SELECT run_id, obs_id, cycle, headers FROM ds.observations
           WHERE (run_id, obs_id) > (%s, %s) AND headers_enc IS NULL
           ORDER BY run_id, obs_id LIMIT %s""",
        (*after, limit),
    )
    return await cur.fetchall()


async def legacy_chat(conn: Conn, after: tuple[Any, ...], limit: int) -> list[tuple[Any, ...]]:
    """(session_id, seq, role, cycle, content, tool_calls, run_id) of the chat messages stored in clear."""
    cur = await conn.execute(
        """SELECT m.session_id, m.seq, m.role, m.cycle, m.content, m.tool_calls, s.run_id
           FROM ds.chat_messages m JOIN ds.chat_sessions s USING (session_id)
           WHERE (m.session_id, m.seq) > (%s, %s) AND m.sealed IS NULL
           ORDER BY m.session_id, m.seq LIMIT %s""",
        (*after, limit),
    )
    return await cur.fetchall()


async def legacy_scans(conn: Conn, after: tuple[Any, ...], limit: int) -> list[tuple[Any, ...]]:
    """(run_id, cycle, after) of the scans whose resume point is stored in clear."""
    cur = await conn.execute(
        """SELECT run_id, cycle, state->'after' FROM ds.scans
           WHERE (run_id, cycle) > (%s, %s) AND jsonb_typeof(state->'after') = 'array'
           ORDER BY run_id, cycle LIMIT %s""",
        (*after, limit),
    )
    return await cur.fetchall()


async def seal_legacy_keys(
    conn: Conn, run_ids: Sequence[str], cycles: Sequence[int], seqs: Sequence[int], sealed: Sequence[bytes]
) -> None:
    """Store each run key's sealed key, clearing its plaintext."""
    await conn.execute(
        """UPDATE ds.run_keys k SET key_enc = u.enc, key = NULL
           FROM unnest(%s::text[], %s::int[], %s::int[], %s::bytea[]) AS u (run_id, cycle, seq, enc)
           WHERE (k.run_id, k.cycle, k.seq) = (u.run_id, u.cycle, u.seq)""",
        (run_ids, cycles, seqs, sealed),
    )


async def seal_legacy_headers(
    conn: Conn, run_ids: Sequence[str], obs_ids: Sequence[str], sealed: Sequence[bytes]
) -> None:
    """Store each observation's sealed headers, clearing their plaintext."""
    await conn.execute(
        """UPDATE ds.observations o SET headers_enc = u.enc, headers = NULL
           FROM unnest(%s::text[], %s::text[], %s::bytea[]) AS u (run_id, obs_id, enc)
           WHERE (o.run_id, o.obs_id) = (u.run_id, u.obs_id)""",
        (run_ids, obs_ids, sealed),
    )


async def seal_legacy_chat(
    conn: Conn, session_ids: Sequence[str], seqs: Sequence[int], sealed: Sequence[bytes]
) -> None:
    """Store each chat message sealed, clearing its content and tool calls."""
    await conn.execute(
        """UPDATE ds.chat_messages m SET sealed = u.enc, content = NULL, tool_calls = NULL
           FROM unnest(%s::text[], %s::int[], %s::bytea[]) AS u (session_id, seq, enc)
           WHERE (m.session_id, m.seq) = (u.session_id, u.seq)""",
        (session_ids, seqs, sealed),
    )


async def seal_legacy_scans(conn: Conn, sealed: Sequence[tuple[dict[str, Any], str, int]]) -> None:
    """Store each scan's sealed resume point: (its sealed ``after``, run_id, cycle)."""
    params = [(Jsonb({"after": after}), run_id, cycle) for after, run_id, cycle in sealed]
    async with conn.cursor() as cur:
        await cur.executemany(
            "UPDATE ds.scans SET state = state || %s WHERE run_id = %s AND cycle = %s", params
        )


async def legacy_blob_names(conn: Conn, limit: int) -> list[str]:
    """Up to ``limit`` legacy blob addresses (``sha256:``) observations still name."""
    cur = await conn.execute(
        """SELECT DISTINCT content_hash FROM ds.observations
           WHERE content_hash LIKE 'sha256:%%' LIMIT %s""",
        (limit,),
    )
    return [content_hash for (content_hash,) in await cur.fetchall()]


async def repoint_observations(conn: Conn, moved: Sequence[tuple[str, str, str]]) -> None:
    """Point the observations naming each of two addresses at a new one: (new, old, old's other name)."""
    if moved:
        async with conn.cursor() as cur:
            await cur.executemany(
                "UPDATE ds.observations SET content_hash = %s WHERE content_hash IN (%s, %s)", moved
            )
