"""The queries of chat sessions and LLM calls."""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from deltasleuth.core import jsonio
from deltasleuth.errors import NotFound
from deltasleuth.security.crypto import Sealer, chat_context
from deltasleuth.store.repo.base import Conn, Row, _jsonb, _row, _rows, _value, storable


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
            raise NotFound(f"no chat session {session_id}")
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
    """Every assistant answer given about the run, oldest first, ``sealed`` (the evidence pack's hash)."""
    return await _rows(
        conn,
        """SELECT s.session_id, s.key AS key_masked, m.seq, m.role, m.cycle, m.sealed, m.citations,
                  m.created_at
           FROM ds.chat_messages m JOIN ds.chat_sessions s USING (session_id)
           WHERE s.run_id = %s AND m.role = 'assistant'
           ORDER BY m.created_at, s.session_id, m.seq""",
        (run_id,),
    )


async def annex_answers(conn: Conn, run_id: str, cycle: int, limit: int) -> tuple[list[Row], int]:
    """The newest ``limit`` answers given in the run's ``cycle`` that cite a tool result (an abstention and
    a failed turn cite none), oldest first, and how many such answers there are. Each is ``sealed``, with
    its ``asked_by`` (the session's creator, by name) and its ``question``: the user message before it,
    a row to open as the answer is, or None."""
    rows = await _rows(
        conn,
        """SELECT s.session_id, s.key AS key_masked, m.seq, m.role, m.cycle, m.sealed, m.citations,
                  m.created_at, ds.actor_name(s.created_by) AS asked_by, q.seq AS q_seq, q.cycle AS q_cycle,
                  q.sealed AS q_sealed, count(*) OVER () AS total
           FROM ds.chat_messages m JOIN ds.chat_sessions s USING (session_id)
           LEFT JOIN LATERAL (
               SELECT u.seq, u.cycle, u.sealed FROM ds.chat_messages u
               WHERE u.session_id = m.session_id AND u.role = 'user' AND u.seq < m.seq
               ORDER BY u.seq DESC LIMIT 1
           ) q ON true
           WHERE s.run_id = %s AND m.cycle = %s AND m.role = 'assistant' AND m.citations <> '[]'::jsonb
           ORDER BY m.created_at DESC, s.session_id DESC, m.seq DESC
           LIMIT %s""",
        (run_id, cycle, limit),
    )
    total = rows[0]["total"] if rows else 0
    for row in rows:
        seq, at, sealed = row.pop("q_seq"), row.pop("q_cycle"), row.pop("q_sealed")
        asked = {"session_id": row["session_id"], "seq": seq, "role": "user", "cycle": at, "sealed": sealed}
        row["question"] = None if seq is None else asked
        del row["total"]
    return rows[::-1], total


# What a stored model call keeps in ``usage`` beside its ``usage_*`` fields: what its cost was priced at,
# what was asked (by hash), how it stopped, which attempt it was, what it carried, where it went and its
# input estimate.
_CALL_FACTS = (
    "price_version", "currency", "request_hash", "stop", "attempt", "data_classes", "host",
    "estimated_input_tokens",
)  # fmt: skip


async def insert_llm_call(conn: Conn, session_id: str | None, call: Mapping[str, Any]) -> str:
    """Record one ``LLMClient.call_log`` entry; ``usage`` keeps its ``usage_*`` fields and ``_CALL_FACTS``."""
    call_id = "L-" + secrets.token_hex(8)
    usage = {name.removeprefix("usage_"): value for name, value in call.items() if name.startswith("usage_")}
    usage |= {name: call[name] for name in _CALL_FACTS if call.get(name) is not None}
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


async def chat_tokens(conn: Conn, run_id: str, actor: str) -> dict[str, int]:
    """The tokens the stored model calls of chats were charged, by the budget that bounds them: those of
    every chat about the run (``per_run_tokens``), and those of the chats ``actor`` started, in the last 24
    hours (``per_actor_tokens_per_day``). A call is charged its ``usage.charged``, or where a row holds
    none its reported input and output; a refused call holds neither."""
    row = await _row(
        conn,
        """SELECT coalesce(sum(t.tokens) FILTER (WHERE s.run_id = %(run)s), 0) AS per_run_tokens,
                  coalesce(sum(t.tokens) FILTER (
                      WHERE s.created_by = %(actor)s AND c.created_at > now() - interval '24 hours'
                  ), 0) AS per_actor_tokens_per_day
           FROM ds.llm_calls c JOIN ds.chat_sessions s USING (session_id)
           CROSS JOIN LATERAL (SELECT coalesce(
               (c.usage->>'charged')::bigint,
               coalesce((c.usage->>'input')::bigint, 0) + coalesce((c.usage->>'output')::bigint, 0)
           ) AS tokens) t
           WHERE s.run_id = %(run)s OR s.created_by = %(actor)s""",
        {"run": run_id, "actor": actor},
    )
    assert row is not None  # an aggregate without GROUP BY returns one row
    return {name: int(value) for name, value in row.items()}
