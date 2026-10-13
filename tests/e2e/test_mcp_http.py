"""An agent on ``POST /v1/mcp`` of a served mock pair (docs/mvp/MCP.md §5.1): with an API token, the MCP
library's own client finds a run the API made, reads it as the Run screen shows it, masked, and compares
one of its keys again against the live pair; a viewer's token is offered no compare."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from deltasleuth.api import create_app
from deltasleuth.config import load_settings
from deltasleuth.mcp import tools
from mocks.registry import pair_dir
from tests.unit.api.test_mcp_http import agent, ask

from .conftest import raw_pii
from .test_api import _finished, _token

pytestmark = [pytest.mark.e2e, pytest.mark.db]
MCP = {
    "data_classes": [tools.AGGREGATES, tools.TOKENISED, tools.CLEAR],
    "work": ["compare_key"],
    "http": True,
}


@pytest.mark.parametrize("era", ["auto", "legacy"])
def test_an_agent_reads_a_run_and_compares_a_key_over_http(serve: Any, make_stack: Any, era: str) -> None:
    stack = make_stack(serve("customer").targets(), auth={"public_url": "https://ds.test"}, mcp=MCP)
    app = create_app(load_settings(stack.settings), worker=True)
    with TestClient(app, base_url="https://ds.test", client=("10.1.2.3", 50000)) as client:
        author, operator, viewer = (
            client.portal.call(_token, app.state.ctx, role) for role in ("spec_author", "operator", "viewer")
        )
        spec = client.post(
            "/v1/specs", content=(pair_dir("customer") / "spec.yaml").read_text(), headers=author
        )
        created = client.post(
            "/v1/runs", json={"spec_id": spec.json()["spec_id"], "mode": "batch"}, headers=operator
        )
        run_id = created.json()["run_id"]
        client.headers.update(viewer)
        run, said = _finished(client, run_id), []

        async def session() -> dict[str, Any]:
            async with agent(app, viewer, era) as reader, agent(app, operator, era) as comparer:

                async def asked(who: Any, name: str, **args: Any) -> Any:
                    data, failed = await ask(who, name, args)
                    said.append(json.dumps(data))
                    assert not failed, data
                    return data

                (listed,) = (await asked(reader, "list_runs"))["runs"]
                finding = (await asked(reader, "get_findings", run_id=run_id, limit=1))["findings"][0]
                key = {"run_id": run_id, "key": finding["key_masked"]}
                return {
                    "offered": [tool.name for tool in (await reader.list_tools()).tools],
                    "listed": listed,
                    "summary": await asked(reader, "get_run_summary", run_id=run_id),
                    "finding": finding,
                    "viewed": await asked(reader, "get_observations", **key),
                    "refused": await ask(reader, "compare_key", key),
                    "compared": await asked(comparer, "compare_key", **key),
                    "runs": (await asked(comparer, "list_runs"))["runs"],
                }

        got = client.portal.call(session)
        key = got["finding"]["key_masked"]
        screen = client.get(f"/v1/runs/{run_id}/keys/{quote(key, safe='')}").json()
        audit = client.portal.call(_compares, app.state.ctx)
    assert "compare_key" not in got["offered"] and got["refused"] == (
        "compare_key needs the operator role",
        True,
    )
    assert (got["listed"]["run_id"], got["listed"]["status"], run["status"]) == (run_id, *["completed"] * 2)
    assert got["summary"]["counters"] == run["counters"]
    # What the agent reads of a key is what the Run screen shows of it: the same masked values and ids.
    shown = {f["finding_id"]: (f["value_a"], f["value_b"]) for f in screen["findings"]}
    assert shown[got["finding"]["finding_id"]] == (got["finding"]["value_a"], got["finding"]["value_b"])
    assert got["viewed"]["result"]["outcome"] == screen["result"]["outcome"] == got["compared"]["outcome"]
    # The compare was a one-shot run of its own, made and ended as the operator's token.
    assert len(got["runs"]) == 2 and {row["mode"] for row in got["runs"]} == {"batch", "one_shot"}
    assert [(action, actor.split(":")[0], detail["via"]) for action, actor, detail in audit] == [
        ("compare_one", "token", "mcp:http"),
        ("run.finished", "token", "mcp:http"),
    ]
    assert [value[:3] for value in raw_pii("customer") if any(value in text for text in said)] == []


async def _compares(ctx: Any) -> list[tuple[str, str, Any]]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT action, actor, detail FROM ds.audit_log WHERE detail ->> 'via' = 'mcp:http' AND action IN"
            " ('compare_one', 'run.finished') ORDER BY id"
        )
        return await cur.fetchall()
