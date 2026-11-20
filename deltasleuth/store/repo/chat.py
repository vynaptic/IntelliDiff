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
    report_prompt: str | None = None,
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
            """INSERT INTO ds.chat_messages (session_id, seq, role, cycle, sealed, citations, report_prompt)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                session_id, seq, role, cycle,
                sealer.seal("chat", cycle, chat_context(session_id, seq, role), text.encode()),
                _jsonb(list(citations)), report_prompt,
            ),
        )  # fmt: skip
    return seq


async def get_chat_session(
    conn: Conn, session_id: str, *, recent: int | None = None, actor: str | None = None
) -> Row | None:
    """The session, its masked ``key_masked`` (None for a run's chat) and its ``messages`` in order,
    each ``sealed``. With ``recent``, read at most that many rows, restricted to the current cycle,
    dropping any partial oldest turn. Zero fetches session metadata only."""
    session = await _row(
        conn,
        """SELECT s.session_id, s.run_id, s.key AS key_masked, s.created_at, s.created_by,
                  r.cycle AS current_cycle FROM ds.chat_sessions s JOIN ds.runs r USING (run_id)
           WHERE s.session_id = %s AND (%s::text IS NULL OR s.created_by = %s)""",
        (session_id, actor, actor),
    )
    if session is not None and recent != 0:
        messages = await _rows(
            conn,
            """SELECT session_id, seq, role, cycle, sealed, citations, created_at FROM ds.chat_messages
               WHERE session_id = %s AND (%s::integer IS NULL OR cycle = %s)
               ORDER BY seq DESC LIMIT %s""",
            (session_id, recent, session["current_cycle"], recent),
        )
        if recent and len(messages) == recent:
            while messages and messages[-1]["role"] != "user":
                messages.pop()
        session["messages"] = messages[::-1]
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
    """The newest cited answers to built-in questions in the cycle, oldest first, and their total."""
    rows = await _rows(
        conn,
        """SELECT s.session_id, s.key AS key_masked, m.seq, m.role, m.cycle, m.sealed, m.citations,
                  m.created_at, m.report_prompt, count(*) OVER () AS total
           FROM ds.chat_messages m JOIN ds.chat_sessions s USING (session_id)
           WHERE s.run_id = %s AND m.cycle = %s AND m.report_prompt IS NOT NULL AND m.citations <> '[]'::jsonb
           ORDER BY m.created_at DESC, s.session_id DESC, m.seq DESC
           LIMIT %s""",
        (run_id, cycle, limit),
    )
    total = rows[0]["total"] if rows else 0
    return rows[::-1], total


# What a stored model call keeps in ``usage`` beside its ``usage_*`` fields: what its cost was priced at,
# what was asked (by hash), how it stopped, which attempt it was, what it carried, where it went and its
# input estimate.
_CALL_FACTS = (
    "price_version", "currency", "request_hash", "stop", "attempt", "data_classes", "host",
    "estimated_input_tokens",
)  # fmt: skip


def _call_values(call: Mapping[str, Any]) -> tuple[Any, ...]:
    """The stored values shared by a provisional attempt, its settlement and a refused request."""
    usage = {name.removeprefix("usage_"): value for name, value in call.items() if name.startswith("usage_")}
    usage |= {name: call[name] for name in _CALL_FACTS if call.get(name) is not None}
    micros = call.get("cost_micros")
    return (
        call.get("model_reported"), _jsonb(usage), None if micros is None else Decimal(micros) / 1_000_000,
        call.get("latency_ms"), call.get("denial_reason") or call.get("error_class"),
    )


async def insert_llm_call(conn: Conn, session_id: str | None, call: Mapping[str, Any]) -> str:
    """Record one ``LLMClient.call_log`` entry; ``usage`` keeps its ``usage_*`` fields and ``_CALL_FACTS``."""
    call_id = "L-" + secrets.token_hex(8)
    await conn.execute(
        """INSERT INTO ds.llm_calls (call_id, session_id, profile_id, model_reported, usage, cost,
               latency_ms, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (call_id, session_id, call["profile_id"], *_call_values(call)),
    )
    return call_id


async def settle_llm_call(conn: Conn, call_id: str, call: Mapping[str, Any]) -> None:
    """Replace the attempt's provisional charge with its outcome; a repeated settlement is idempotent."""
    await conn.execute(
        """UPDATE ds.llm_calls SET model_reported = %s, usage = %s, cost = %s, latency_ms = %s, error = %s
           WHERE call_id = %s""",
        (*_call_values(call), call_id),
    )


async def chat_tokens(conn: Conn, run_id: str, actor: str, *, lock: bool = False) -> dict[str, int]:
    """The tokens the stored model calls of chats were charged, by the budget that bounds them: those of
    every chat about the run (``per_run_tokens``), and those of the chats ``actor`` started, in the last 24
    hours (``per_actor_tokens_per_day``). A call is charged its ``usage.charged``, or where a row holds
    none its reported input and output; a refused call holds neither. Before an attempt, lock both
    budgets until its charge commits. Every admission locks the run before the actor."""
    if lock:
        await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (0x44534C52, run_id))
        await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (0x44534C41, actor))
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
           WHERE s.run_id = %(run)s
              OR (s.created_by = %(actor)s AND c.created_at > now() - interval '24 hours')""",
        {"run": run_id, "actor": actor},
    )
    assert row is not None  # an aggregate without GROUP BY returns one row
    return {name: int(value) for name, value in row.items()}
