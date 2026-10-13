"""The API end to end, as the UI drives it: upload a spec, start a run that the in-process worker
processes, read its results and a key's masked view, download reports, and chat about the key (AI
on: a scripted OpenAI-compatible model answering with valid citations; AI off: 503, all else works),
and have a root cause explained and the run summarised (cited, or refused when the answer cites another
root cause). Nothing the API returns, the model receives or the reports hold carries raw personal data.
In tokens mode, which has no approvers, runs are labelled unapproved and approvals are refused. A trial
declares its two endpoints in the product, tries each and compares them, the targets file naming neither."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from deltasleuth.agent import prompt
from deltasleuth.api import create_app
from deltasleuth.auth import tokens
from deltasleuth.cli.main import app as cli
from deltasleuth.config import load_settings
from deltasleuth.context import Context
from mocks.registry import pair_dir
from tests.conftest import TESTER

from .conftest import pdf_text, raw_pii

pytestmark = [pytest.mark.e2e, pytest.mark.db]
SPEC = pair_dir("customer") / "spec.yaml"
TRIAL = {"public_url": "http://127.0.0.1:8000"}  # tokens mode on a plain-http loopback address
FILED = "targets: {filed: {base_url: 'https://filed.example'}}\n"  # a targets file must name one target
ACTIONS = ("target.set", "target.try")


def _finished(client: TestClient, run_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while (run := client.get(f"/v1/runs/{run_id}").json())["status"] not in (
        "completed",
        "completed_with_issues",
    ):
        assert time.monotonic() < deadline and run["status"] == "running", run["status"]
        time.sleep(0.1)
    return run


@pytest.mark.parametrize("ai", [True, False], ids=["ai-on", "ai-off"])
def test_run_results_key_reports_and_chat(serve: Any, make_stack: Any, llm: Any, ai: bool) -> None:
    url, sent = llm
    stack = make_stack(serve("customer").targets(), llm_url=url if ai else None)
    app = create_app(load_settings(stack.settings), worker=True)
    local = {
        "base_url": "http://127.0.0.1",
        "client": ("127.0.0.1", 50000),
        "headers": {"X-CSRF-Token": "dev"},
    }
    with TestClient(app, **local) as client:  # developer mode: its settings have no auth
        spec = client.post("/v1/specs", content=(pair_dir("customer") / "spec.yaml").read_text())
        run_id = client.post("/v1/runs", json={"spec_id": spec.json()["spec_id"], "mode": "batch"}).json()[
            "run_id"
        ]
        run = _finished(client, run_id)
        mismatches = client.get(f"/v1/runs/{run_id}/results", params={"outcome": "MISMATCH"}).json()["items"]
        key = mismatches[0]["key_masked"]
        view = client.get(f"/v1/runs/{run_id}/keys/{quote(key, safe='')}").json()
        causes = client.get(f"/v1/runs/{run_id}/root-causes").json()["items"]
        csv = client.get(f"/v1/runs/{run_id}/reports/discrepancies.csv").text
        chat = client.post("/v1/chat/sessions", json={"run_id": run_id, "key": key})
        if ai:
            path = f"/v1/chat/sessions/{chat.json()['session_id']}/messages"
            messages = client.post(path, json={"content": "Why does this key differ?"}).json()["messages"]
        pdf = pdf_text(client.get(f"/v1/runs/{run_id}/reports/report.pdf").content)
    assert (spec.status_code, run["status"], len(mismatches)) == (201, "completed", 2)
    assert view["findings"] and view["canonical"]["a"] and view["result"]["outcome"] == "MISMATCH"
    assert key in csv and causes[0]["root_cause_id"] in csv
    if ai:
        tool, answer = messages[-2:]
        assert (tool["role"], answer["role"], answer["citations"]) == (
            "tool",
            "assistant",
            [causes[0]["root_cause_id"]],
        )
        assert answer["content"] in pdf and "AI-generated, not evidence" in pdf
    else:
        assert (chat.status_code, chat.json()) == (503, {"detail": "AI disabled"}) and sent == []
    published = [json.dumps([run, mismatches, view, causes]), csv, pdf, json.dumps(sent)]
    assert [v[:3] for v in raw_pii("customer") if any(v in text for text in published)] == []


@pytest.mark.parametrize("ai", [True, False], ids=["ai-on", "ai-off"])
def test_explain_and_summarise_are_cited_or_refused(serve: Any, make_stack: Any, llm: Any, ai: bool) -> None:
    """The scripted model cites the run's first root cause whatever it is asked: explaining that one and
    summarising the run are verified answers, explaining another is refused after the one repair."""
    url, sent = llm
    stack = make_stack(serve("customer").targets(), llm_url=url if ai else None)
    app = create_app(load_settings(stack.settings), worker=True)
    local = {
        "base_url": "http://127.0.0.1",
        "client": ("127.0.0.1", 50000),
        "headers": {"X-CSRF-Token": "dev"},
    }
    with TestClient(app, **local) as client:
        spec = client.post("/v1/specs", content=(pair_dir("customer") / "spec.yaml").read_text())
        run = {"spec_id": spec.json()["spec_id"], "mode": "batch"}
        run_id = client.post("/v1/runs", json=run).json()["run_id"]
        _finished(client, run_id)
        causes = client.get(f"/v1/runs/{run_id}/root-causes").json()["items"]
        first, other = (cause["root_cause_id"] for cause in causes[:2])
        asked = [{"root_cause_id": first}, {}, {"root_cause_id": other}]
        drafts = [client.post("/v1/chat/sessions/explain", json={"run_id": run_id} | ask) for ask in asked]
        pdf = pdf_text(client.get(f"/v1/runs/{run_id}/reports/report.pdf").content)
    if not ai:
        assert [(d.status_code, d.json()) for d in drafts] == [(503, {"detail": "AI disabled"})] * 3
        assert sent == [] and "No chat answers were given about this run." in pdf
        return
    assert [d.status_code for d in drafts] == [201] * 3
    explained, summarised, refused = (d.json()["messages"][-1] for d in drafts)
    assert [m["citations"] for m in (explained, summarised, refused)] == [[first], [first], []]
    problem = f"the answer does not cite [{other}], the root cause asked about"
    assert refused["content"] == prompt.abstain([problem])
    # report.pdf's annex holds each draft as any chat answer, the refusal too
    assert pdf.count(explained["content"]) == 2 and "AI-generated, not evidence" in pdf
    assert all(f"{d.json()['session_id']}:" in pdf for d in drafts)
    stored = [d.json()["messages"][0]["content"] for d in drafts]
    assert stored[0].startswith(prompt.EXPLAIN.format(id=first) + "\n\n") and stored[1] == prompt.SUMMARISE
    offered = {tool["function"]["name"] for request in sent for tool in request["tools"]}
    assert "compare_key" not in offered  # developer mode is an operator; a fixed question's agent a viewer
    assert [v[:3] for v in raw_pii("customer") if v in json.dumps(sent)] == []


async def _token(ctx: Context, role: str) -> dict[str, str]:
    async with ctx.pool.connection() as conn:
        _, token = await tokens.create(conn, TESTER, name=f"ci-{role}", owner="tests", role=role, max_days=90)
    return {"Authorization": f"Bearer {token}"}


def test_tokens_mode_labels_runs_unapproved_and_refuses_approvals(serve: Any, make_stack: Any) -> None:
    stack = make_stack(serve("customer").targets(), auth={"public_url": "https://ds.test"})
    app = create_app(load_settings(stack.settings), worker=True)
    with TestClient(app, base_url="https://ds.test", client=("10.1.2.3", 50000)) as client:
        author, operator = (
            client.portal.call(_token, app.state.ctx, role) for role in ("spec_author", "operator")
        )
        spec = client.post(
            "/v1/specs", content=(pair_dir("customer") / "spec.yaml").read_text(), headers=author
        )
        spec_id = spec.json()["spec_id"]
        requested = client.post(f"/v1/specs/{spec_id}/approvals", headers=author)
        created = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}, headers=operator)
        run_id = created.json()["run_id"]
        client.headers.update(operator)
        run = _finished(client, run_id)
        signoff = client.post(f"/v1/runs/{run_id}/cycles/1/signoff")
        decided = client.post(
            f"/v1/approvals/A-{'0' * 26}/decision", json={"decision": "reject", "note": "no"}
        )
        pdf = pdf_text(client.get(f"/v1/runs/{run_id}/reports/report.pdf").content)
    needs = spec.json()["needs"]
    refusal = "approvals need SSO (auth.oidc); this deployment runs in tokens mode"
    assert {"clear", "known_diffs"} <= set(needs) and spec.json()["approval_id"] is None
    assert [(r.status_code, r.json()["detail"]) for r in (requested, signoff, decided)] == [
        (409, refusal), (409, refusal), (403, "needs the rule_approver role"),
    ]  # fmt: skip
    assert run["status"] == "completed" and run["signoffs"] == []
    assert {name: run["approval"][name] for name in ("state", "approval_id", "needs", "mode")} == {
        "state": "unapproved", "approval_id": None, "needs": needs, "mode": "tokens",
    }  # fmt: skip
    assert f"Unapproved spec version: {', '.join(needs)} were not approved by a second person" in pdf


def test_two_endpoints_declared_tried_and_compared_without_a_file(
    serve: Any, make_stack: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-machine trial whose targets file names neither service: its first token declares both in the
    product, pastes the pair's spec into the builder's form, tries each side for one key and reads the
    answer as it came, then saves the form and compares. ``ds compare``, a process of its own, reads the
    product's list at its start, the 404 capability with it."""
    served = serve("customer")
    stack = make_stack(FILED, auth=TRIAL)
    app = create_app(load_settings(stack.settings), worker=True)
    mismatch, absent, match = "C00012003", "C00012027", "C00012001"
    with TestClient(app, base_url=TRIAL["public_url"], client=("127.0.0.1", 50000)) as client:
        client.headers.update(client.portal.call(_token, app.state.ctx, "spec_author"))  # as deploy/up.sh's
        before = client.get("/v1/targets").json()
        local = {"allow_private_network": True, "absent_on_404": True}  # a 404 means no such record
        declared = [
            client.put(f"/v1/targets/customer-{v}", json={"base_url": f"http://127.0.0.1:{port}"} | local)
            for v, port in served.ports.items()
        ]
        pasted = client.post("/v1/specs/validate", json={"text": SPEC.read_text()}).json()
        form = pasted["form"]  # the text's sides, split out as the builder's rows show them
        asked = (("a", mismatch), ("b", mismatch), ("b", absent))
        tried = [
            client.post("/v1/specs/try", json={"form": form, "side": side, "key": key}).json()
            for side, key in asked
        ]
        stored = [client.get(path).json()["items"] for path in ("/v1/specs", "/v1/runs")]
        saved = client.post("/v1/specs", json=form)
        compared = {
            key: client.post("/v1/compare", json={"spec_id": saved.json()["spec_id"], "key": key}).json()
            for key in (mismatch, match)
        }
    assert (before["declarer"], [t["target_id"] for t in before["items"]]) == ("spec_author", ["filed"])
    assert [(d.status_code, d.json()["declared_in"]) for d in declared] == [(201, "product")] * 2
    assert (pasted["valid"], [form["sides"][side]["target"] for side in "ab"]) == (
        True,
        ["customer-a", "customer-b"],
    )
    assert [(t["status"], t["attempts"], t["raw"]) for t in tried] == [(s, 1, True) for s in (200, 200, 404)]
    assert (tried[0]["answer"]["customer"]["id"], tried[1]["answer"]["data"]["customerId"]) == (mismatch,) * 2
    assert tried[2]["answer"] == {"error": "NOT_FOUND"} and stored == [[], []]  # a try stores nothing
    assert saved.status_code == 201
    assert {key: view["result"]["outcome"] for key, view in compared.items()} == {
        mismatch: "MISMATCH",
        match: "MATCH",
    }
    monkeypatch.setenv("DS_OPERATOR", "tester")

    def ds(*args: Any) -> str:
        result = CliRunner().invoke(cli, [*map(str, args), "--config", str(stack.settings)])
        assert result.exit_code == 0, result.output
        return result.stdout

    assert ": NOT_FOUND_B (" in ds("compare", absent, "--spec", SPEC)
    audited = {action: json.loads(ds("audit", "--action", action, "--json")) for action in ACTIONS}
    assert {action: len(rows) for action, rows in audited.items()} == {"target.set": 2, "target.try": 3}
    assert {row["actor_name"] for rows in audited.values() for row in rows} == {"ci-spec_author"}
    # Neither the audit log nor a view holds a key: the pair's key class is no business code.
    published = json.dumps([audited, *compared.values()])
    assert not any(key in published for key in (mismatch, absent, match))
