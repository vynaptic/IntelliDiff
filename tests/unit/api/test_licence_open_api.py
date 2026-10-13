"""The open build over the API (docs/mvp/LICENCE.md §19): GET /v1/licence says ``open``, allows everything
and warns of nothing; under SSO a run, a compare, the resume of a run an older build paused for the licence,
a sign-off's request and approval step and a pack's issue answer as under an Enterprise licence, the pack
naming no licence; a run and its report downloads carry no label, while a run made in an evaluation keeps
its own."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth import licence
from deltasleuth.auth.principal import expand
from deltasleuth.security.signing import PackSigner
from tests.conftest import VENDOR_KEY

from .conftest import OIDC, RAW_KEY, Callers, approved, audited, session_as, signed
from .test_licence_api import _audit_rows, _facts, _named, _packs, _paused, _spec

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
ALL = {"runs": True, "sso": True, "signoff": True, "packs": True}


def test_get_licence_says_open(open_client: OpenClient) -> None:
    with open_client(auth={"oidc": OIDC}) as client:  # oidc mode, and no licence in the store
        ctx = client.app.state.ctx
        installation = client.portal.call(_facts, ctx)["installation_id"]
        client.portal.call(_named, ctx, "accounts", "loans", "cards")  # a third name: none is counted
        callers = [Callers(client).token("viewer"), Callers(client).session("viewer", username="vera")]
        before = client.portal.call(_audit_rows, ctx)
        answers = [client.get("/v1/licence", headers=headers) for headers in callers]
        assert client.portal.call(_audit_rows, ctx) == before  # unaudited, as ever
        (start,) = audited(client, "system.start")
    assert [answer.status_code for answer in answers] == [200, 200]
    assert answers[0].json() == answers[1].json() == {
        "installation_id": installation, "state": "open", "warnings": [], "licence": None, "renewal": None,
        "allows": ALL, "pairs": {"limit": None, "since": None, "counted": []}, "clock_behind_seconds": None,
        "installed": [],
    }  # fmt: skip
    assert start[2]["licence"] == {"installation_id": installation, "state": "open", "licence_id": None,
                                   "tier": None, "pairs": None, "ends": None}  # fmt: skip


def test_work_under_sso_needs_no_licence(open_client: OpenClient, pack_key: Path) -> None:
    with open_client(auth={"oidc": OIDC}, packs=_packs(pack_key)) as client:
        ctx = client.app.state.ctx
        cycle = signed(client, sign=False)
        olga = session_as(client, "olga", "operator")
        erin = Callers(client).session(
            *expand(["rule_approver"]), username="erin", auth_time=datetime.now(UTC)
        )
        spec_id = client.portal.call(_spec, ctx)
        approved(client, spec_id)  # the four-eyes gate is not the licence's: it stays
        paused = client.portal.call(_paused, ctx, spec_id)  # as an older build's boundary left it
        request = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/signoff", headers=olga)
        assert request.status_code == 201, request.text
        step = {"decision": "approve", "attests_sha256": request.json()["attests_sha256"]}
        replies = [
            client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}, headers=olga),
            client.post("/v1/compare", json={"spec_id": spec_id, "key": RAW_KEY}, headers=olga),
            client.post(f"/v1/runs/{paused}/resume", headers=olga),
            client.post(f"/v1/approvals/{request.json()['approval_id']}/decision", json=step, headers=erin),
            client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga),
        ]
        assert [reply.status_code for reply in replies] == [202, 200, 200, 200, 201], [
            reply.text for reply in replies
        ]
        made, issued = replies[0].json()["run_id"], replies[-1].json()["pack_id"]
        manifest = client.get(f"/v1/packs/{issued}", headers=olga).json()["manifest"]
        listed = {
            run["run_id"]: run["licence"] for run in client.get("/v1/runs", headers=olga).json()["items"]
        }
        named = client.get(f"/v1/runs/{cycle.run_id}/reports/report.pdf", headers=olga)
        standing = client.get("/v1/licence", headers=olga).json()
        (issue,) = audited(client, "pack.issue")
    assert (standing["state"], standing["allows"], standing["warnings"]) == ("open", ALL, [])
    assert manifest["licence"] is None and issue[2]["licence_id"] is None  # the pack names no licence
    assert listed[made] is None and set(listed.values()) == {None}  # no record, so no label
    assert named.headers["content-disposition"] == f'attachment; filename="{cycle.run_id}-report.pdf"'


def test_a_run_made_in_an_evaluation_keeps_its_label(
    client: TestClient, seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def started() -> str:
        body = {"spec_id": seeded.spec_id, "mode": "batch", "keys": [RAW_KEY]}
        answer = client.post("/v1/runs", json=body, headers={"X-CSRF-Token": "dev"})
        assert answer.status_code == 202, answer.text
        return answer.json()["run_id"]

    monkeypatch.setattr(licence, "VENDOR_KEYS", (PackSigner(VENDOR_KEY).public_pem,))
    old = started()  # under an older build: an unlicensed evaluation
    monkeypatch.setattr(licence, "VENDOR_KEYS", ())
    new = started()
    label = (
        f"Unlicensed evaluation: run {old} was made without a licence. Not for production use; no evidence"
        " pack is issued for it."
    )
    record = {"licence_id": None, "tier": None, "state": "evaluation", "ends": None, "label": label}
    listed = {run["run_id"]: run["licence"] for run in client.get("/v1/runs").json()["items"]}
    assert listed == {old: record, new: None, seeded.run_id: None}  # history is not rewritten
    assert [client.get(f"/v1/runs/{run}").json()["licence"] for run in (old, new)] == [record, None]
    names = [
        client.get(f"/v1/runs/{run}/reports/report.pdf").headers["content-disposition"] for run in (old, new)
    ]
    assert names == [
        f'attachment; filename="{old}-unlicensed-evaluation-report.pdf"',
        f'attachment; filename="{new}-report.pdf"',
    ]
    assert client.get("/v1/licence").json()["warnings"] == []  # and no banner on any screen
