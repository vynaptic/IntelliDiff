"""Chat: off without LLM profiles; with them, sessions scoped to a run or key, answered by the agent,
and the two fixed questions (explain a root cause, summarise the run), each in a session of its own."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth.agent import prompt
from deltasleuth.agent.tools import cause_facts, tools_for
from deltasleuth.store import repo

from .conftest import append_chat, audited

pytestmark = pytest.mark.db


def test_every_chat_route_answers_503_with_ai_off(client: TestClient, seeded: Any) -> None:
    calls = [
        client.post("/v1/chat/sessions", json={"run_id": seeded.run_id}),
        client.post("/v1/chat/sessions/explain", json={"run_id": seeded.run_id}),
        client.post("/v1/chat/sessions/S-1/messages", json={"content": "Why?"}),
        client.get("/v1/chat/sessions/S-1"),
    ]
    assert [(r.status_code, r.json()) for r in calls] == [(503, {"detail": "AI disabled"})] * 4


def test_an_unreadable_llm_key_is_a_503_naming_its_file(open_client: Any, seed: Any) -> None:
    with open_client(ai=True) as client:  # the profile's auth_ref file:llm.key was never written
        run = {"run_id": seed(client).run_id}
        session = client.post("/v1/chat/sessions", json=run).json()["session_id"]
        reply = client.post(f"/v1/chat/sessions/{session}/messages", json={"content": "Why?"})
        draft = client.post("/v1/chat/sessions/explain", json=run)
    for refused in (reply, draft):
        assert refused.status_code == 503 and "cannot read secret file llm.key" in refused.json()["detail"]


def test_a_key_scoped_session_is_answered_by_the_agent(
    open_client: Any, stub: Any, seed: Any, audit: Any
) -> None:
    async def answer(ctx: Any, session_id: str, question: str, who: Any, about: Any) -> None:
        await append_chat(ctx, session_id, "user", question)
        await append_chat(ctx, session_id, "assistant", "The price differs.")

    stub("deltasleuth.agent.loop", answer=answer)
    with open_client(ai=True) as client:
        seeded = seed(client)
        created = client.post("/v1/chat/sessions", json={"run_id": seeded.run_id, "key": seeded.key})
        assert created.status_code == 201
        session = f"/v1/chat/sessions/{created.json()['session_id']}"
        reply = client.post(session + "/messages", json={"content": "Why?"}).json()
        assert (reply["key_masked"], reply["run_id"]) == (seeded.key, seeded.run_id)
        assert [(m["role"], m["content"]) for m in reply["messages"]] == [
            ("user", "Why?"),
            ("assistant", "The price differs."),
        ]
        assert client.get(session).json() == reply
        unknown_key = {"run_id": seeded.run_id, "key": seeded.raw_key}  # sessions take masked keys
        assert client.post("/v1/chat/sessions", json=unknown_key).status_code == 404
        assert client.get("/v1/chat/sessions/S-0000").status_code == 404
        assert audit(client) == ["chat.create", "chat.ask"]


def test_a_question_the_model_never_received_is_not_audited(
    open_client: Any, seed: Any, audit_rows: Any
) -> None:
    with open_client(ai=True) as client:  # the profile's key file was never written: no client opens
        run = {"run_id": seed(client).run_id}
        session = client.post("/v1/chat/sessions", json=run).json()["session_id"]
        refused = client.post(f"/v1/chat/sessions/{session}/messages", json={"content": "Why?"})
        draft = client.post("/v1/chat/sessions/explain", json=run)  # its session was created first: it stays
        asked = [row for row in audit_rows(client) if row["action"] == "chat.ask"]
        created = [row["object"] for row in audit_rows(client) if row["action"] == "chat.create"]
        left = [client.get(f"/v1/chat/sessions/{sid}").json()["messages"] for sid in created]
    assert refused.status_code == 503 and asked == []
    assert draft.status_code == 503 and (created[0], left) == (session, [[], []])


def test_once_the_run_s_current_cycle_is_purging_no_chat_starts_or_asks(
    open_client: Any, stub: Any, seed: Any, audit: Any
) -> None:
    from deltasleuth.store import repo

    async def answer(*_: Any) -> None:
        raise AssertionError("the agent must not run")

    async def purging(ctx: Any, run_id: str) -> Any:  # the purge's first step: its reads answer 410
        async with ctx.pool.connection() as conn:
            return await repo.start_purge(conn, run_id, 1, "retention standard")

    stub("deltasleuth.agent.loop", answer=answer)
    with open_client(ai=True) as client:
        seeded = seed(client)
        session = client.post("/v1/chat/sessions", json={"run_id": seeded.run_id}).json()["session_id"]
        assert client.post(f"/v1/runs/{seeded.run_id}/cancel").json()["status"] == "cancelled"
        cycle = client.portal.call(purging, client.app.state.ctx, seeded.run_id)
        blocked = {
            "detail": f"the run's evidence was purged on {cycle['purging_at']:%Y-%m-%d}; AI answers need it"
        }
        asked = client.post(f"/v1/chat/sessions/{session}/messages", json={"content": "Why?"})
        started = client.post("/v1/chat/sessions", json={"run_id": seeded.run_id})
        drafted = client.post("/v1/chat/sessions/explain", json={"run_id": seeded.run_id})
        assert [(r.status_code, r.json()) for r in (asked, started, drafted)] == [(410, blocked)] * 3
        assert client.get(f"/v1/chat/sessions/{session}").status_code == 200  # what was said stays readable
        assert audit(client).count("chat.ask") == 0 and audit(client).count("chat.create") == 1


def test_explain_starts_a_session_asks_the_fixed_question_and_audits_it(
    open_client: Any, stub: Any, seed: Any, audit: Any
) -> None:
    turns: list[tuple[Any, ...]] = []

    async def answer(ctx: Any, session_id: str, question: str, who: Any, about: Any) -> None:
        turns.append((session_id, question, about, who))
        await append_chat(ctx, session_id, "user", question)
        await append_chat(ctx, session_id, "assistant", "The price differs.")

    stub("deltasleuth.agent.loop", answer=answer)
    with open_client(ai=True) as client:
        run_id = seed(client).run_id
        (cause,) = client.get(f"/v1/runs/{run_id}/root-causes").json()["items"]
        rc = cause["root_cause_id"]
        bodies = [{"root_cause_id": rc}, {}, {"root_cause_id": None}]
        drafts = [client.post("/v1/chat/sessions/explain", json={"run_id": run_id} | body) for body in bodies]
        ids = [draft.json()["session_id"] for draft in drafts]
        assert [draft.json() for draft in drafts] == [
            client.get(f"/v1/chat/sessions/{sid}").json() for sid in ids
        ]
        assert audit(client) == ["chat.create", "chat.ask"] * 3
        asks = audited(client, "chat.ask")
    assert [draft.status_code for draft in drafts] == [201] * 3 and len(set(ids)) == 3  # a session each
    first = drafts[0].json()
    assert (first["run_id"], first["key_masked"]) == (run_id, None)
    assert [(m["role"], m["content"]) for m in first["messages"]] == [
        ("user", prompt.EXPLAIN.format(id=rc)),
        ("assistant", "The price differs."),
    ]
    assert [turn[:3] for turn in turns] == [
        (ids[0], prompt.EXPLAIN.format(id=rc), cause_facts(cause)),
        (ids[1], prompt.SUMMARISE, None),
        (ids[2], prompt.SUMMARISE, None),
    ]
    # developer mode acts as admin: the agent of a fixed question is a viewer all the same
    viewer = ["get_run_summary", "get_findings", "get_observations"]
    assert [[tool.name for tool in tools_for(turn[3])] for turn in turns] == [viewer] * 3
    summarise = {"question": "summarise"}
    assert asks == [
        ("dev:loopback", ids[0], {"question": "explain", "root_cause_id": rc}),
        ("dev:loopback", ids[1], summarise),
        ("dev:loopback", ids[2], summarise),
    ]


def test_explain_refuses_an_unknown_root_cause_without_a_session(
    open_client: Any, stub: Any, seed: Any, audit: Any
) -> None:
    async def answer(*_: Any) -> None:
        raise AssertionError("the agent must not run")

    async def next_cycle(ctx: Any, run_id: str) -> int:  # a soak's second cycle: no root cause yet
        async with ctx.pool.connection() as conn:
            return await repo.start_next_cycle(conn, run_id, datetime.now(UTC))

    async def sessions(ctx: Any) -> int:
        async with ctx.pool.connection() as conn:
            return (await (await conn.execute("SELECT count(*) FROM ds.chat_sessions")).fetchone())[0]

    stub("deltasleuth.agent.loop", answer=answer)
    with open_client(ai=True) as client:
        run_id, ctx = seed(client).run_id, client.app.state.ctx
        older = client.get(f"/v1/runs/{run_id}/root-causes").json()["items"][0]["root_cause_id"]
        assert client.portal.call(next_cycle, ctx, run_id) == 2
        unknown = "RC-" + "0" * 12
        ask = lambda **body: client.post("/v1/chat/sessions/explain", json=body)  # noqa: E731
        refused = [ask(run_id=run_id, root_cause_id=rc) for rc in (unknown, older)]
        malformed = [ask(run_id=run_id, root_cause_id=rc) for rc in ("RC-1", "RC-" + "G" * 12, "", 7)]
        no_run = ask(run_id="R-" + "0" * 26, root_cause_id=older)
        assert audit(client) == [] and client.portal.call(sessions, ctx) == 0
    assert [(r.status_code, r.json()["detail"]) for r in refused] == [
        (404, f"root cause {unknown} not found"),
        (404, f"root cause {older} not found"),
    ]
    assert [r.status_code for r in malformed] == [422] * 4
    assert (no_run.status_code, no_run.json()["detail"]) == (404, f"run R-{'0' * 26} not found")


def test_explain_under_a_lost_kek_is_a_503_that_leaves_an_empty_session(
    open_client: Any, seed: Any, audit: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, loopback: Any
) -> None:
    lost = "K-" + "0" * 16

    async def lose(ctx: Any, run_id: str) -> None:  # the run's data key as if wrapped by a KEK now lost
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE ds.data_keys SET kek = %s WHERE scope = %s", (lost, run_id))

    (tmp_path / "llm.key").write_text("k3y\n")  # the model client opens; the turn stops before any call
    monkeypatch.chdir(tmp_path)  # the profile's auth_ref is file:llm.key
    with open_client(ai=True) as client:
        run_id = seed(client).run_id
        client.portal.call(lose, client.app.state.ctx, run_id)
        reply = client.post("/v1/chat/sessions/explain", json={"run_id": run_id})
        assert audit(client) == ["chat.create"]
    unreadable = f"evidence unreadable: the keyring lacks {lost}"
    assert (reply.status_code, reply.json()) == (503, {"detail": unreadable})
