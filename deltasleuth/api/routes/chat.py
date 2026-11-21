"""Chat about a run or one of its keys. Every chat route answers 503 while AI is off, to a viewer.

A session is its creator's: questions are stored as typed, so only the creator may read or continue
it, and anyone else is told it does not exist. Only a cited draft from a built-in question can be shared
in the report's AI annex.
Once the run's current cycle is purged no chat starts or asks (410): answers need its evidence.
``explain`` starts a session and asks it one of the two fixed questions. A typed question holds at most
``MAX_QUESTION`` characters (422), and a caller asks at most ``ASK_LIMIT`` questions a minute over both
asking routes (429), refused before anything is created, stored or asked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field

from deltasleuth.agent import prompt, tools
from deltasleuth.api.deps import Ctx, Payload, Viewer, need, ok, run_of, shown
from deltasleuth.auth.principal import Principal, expand
from deltasleuth.context import Context
from deltasleuth.core.secrets import SecretError
from deltasleuth.runner.purge import refuse_chat
from deltasleuth.security.crypto import KeyringError
from deltasleuth.store import repo


def require_ai(ctx: Ctx, _: Viewer) -> None:
    if ctx.llm is None:
        raise HTTPException(503, "AI disabled")


router = APIRouter(prefix="/chat/sessions", dependencies=[Depends(require_ai)])
MAX_QUESTION = 4000  # characters a typed question holds at most
ASK_LIMIT = 10  # questions a minute per caller, over both asking routes


class SessionIn(Payload):
    run_id: str
    key: str | None = None  # a masked key of the run, for a key-scoped chat


class MessageIn(Payload):
    content: str = Field(min_length=1, max_length=MAX_QUESTION)


class ExplainIn(Payload):
    run_id: str
    root_cause_id: str | None = Field(None, pattern=r"^RC-[0-9a-f]{12}$")  # none: summarise the run


async def _asking(ctx: Context, who: Principal) -> None:
    """Count a question of ``who`` against the chat questions' rate, kept in the store so that it holds across
    the app's processes and its restarts: 429 past it."""
    async with ctx.pool.connection() as conn:
        within = await repo.hit_rate(conn, "chat.ask", who.actor, ASK_LIMIT)
    if not within:
        raise HTTPException(
            429,
            f"rate limited: at most {ASK_LIMIT} chat questions a minute per caller",
            headers={"Retry-After": "60"},
        )


async def _own(conn: repo.Conn, session_id: str, who: Principal, *, recent: int | None = None) -> repo.Row:
    """The caller's session; 404 for another's (sessions before 005 are no one's)."""
    return need(await repo.get_chat_session(conn, session_id, recent=recent, actor=who.actor),
                f"chat session {session_id}")


@router.post("", status_code=201)
async def new_session(ctx: Ctx, who: Viewer, body: SessionIn) -> Response:
    async with repo.audited(ctx.pool) as (conn, audit):
        await refuse_chat(conn, await run_of(conn, body.run_id))
        if body.key is not None:
            need(await repo.get_key_detail(conn, body.run_id, body.key), f"key {body.key}")
        session_id = await repo.create_chat_session(conn, body.run_id, body.key, created_by=who.actor)
        await audit(who, "chat.create", session_id, {"run_id": body.run_id})
    return ok({"session_id": session_id}, 201)


@router.post("/explain", status_code=201)
async def explain(ctx: Ctx, who: Viewer, body: ExplainIn) -> Response:
    """Start a run-scoped session and ask it a fixed question: explain one root cause of the run's
    current cycle, or summarise the run. The agent acts as a viewer, whoever asks. Returns the session
    with every message, the last one the answer or (``citations: []``) the refusal."""
    await _asking(ctx, who)
    cause, about, detail = body.root_cause_id, None, {"question": "summarise"}
    async with repo.audited(ctx.pool) as (conn, audit):
        run = await run_of(conn, body.run_id)
        await refuse_chat(conn, run)
        if cause is not None:
            causes = await repo.list_root_causes(conn, body.run_id, run["cycle"])
            rows = {r["root_cause_id"]: r for r in causes}
            about = tools.cause_facts(need(rows.get(cause), f"root cause {cause}"))
            detail = {"question": "explain", "root_cause_id": cause}
        session_id = await repo.create_chat_session(conn, body.run_id, created_by=who.actor)
        await audit(who, "chat.create", session_id, {"run_id": body.run_id})
    question = prompt.SUMMARISE if cause is None else prompt.EXPLAIN.format(id=cause)
    await _answer(ctx, replace(who, roles=expand(["viewer"])), session_id, question, detail, about,
                  preset="summary" if cause is None else "root_cause", cycle=run["cycle"])
    drafted = await session(ctx, who, session_id)
    drafted.status_code = 201
    return drafted


@router.post("/{session_id}/messages")
async def ask(ctx: Ctx, who: Viewer, session_id: str, body: MessageIn) -> Response:
    """Ask within the session's scope; the agent, acting for the asker, stores the question, its tool
    calls and the answer. Returns the session with every message."""
    await _asking(ctx, who)
    async with ctx.pool.connection() as conn:
        own = await _own(conn, session_id, who, recent=0)
        await refuse_chat(conn, await run_of(conn, own["run_id"]))
    await _answer(ctx, who, session_id, body.content, cycle=own["current_cycle"])
    return await session(ctx, who, session_id)


async def _answer(
    ctx: Context,
    who: Principal,
    session_id: str,
    question: str,
    detail: dict[str, Any] | None = None,
    about: Mapping[str, Any] | None = None,
    *,
    preset: str | None = None,
    cycle: int | None = None,
) -> None:
    """Have the agent answer ``question`` in the session, acting for ``who``, and audit ``chat.ask``
    with ``detail``. ``about``: the root cause a fixed question explains."""
    from deltasleuth.agent.loop import answer

    asked = True
    try:
        await answer(ctx, session_id, question, who, about, preset=preset, cycle=cycle)
    except SecretError as exc:  # the profile's key file: the message names the file, never its content
        asked = False  # the model client never opened, so nothing was asked or stored
        raise HTTPException(503, str(exc)) from None
    except KeyringError:  # the run's data key cannot be opened: nothing was asked or stored (a 503)
        asked = False
        raise
    finally:
        if asked:  # the agent has stored the question, even when the model failed
            async with repo.audited(ctx.pool) as (_, audit):
                await audit(who, "chat.ask", session_id, detail)


@router.get("/{session_id}")
async def session(ctx: Ctx, who: Viewer, session_id: str) -> Response:
    async with ctx.pool.connection() as conn:
        row = await _own(conn, session_id, who)
        if messages := row["messages"]:
            sealer = await ctx.sealer(conn, row["run_id"])
            messages = [shown(sealer.open_row(m), "session_id") for m in messages]
    return ok(shown(row, "current_cycle") | {"messages": messages})
