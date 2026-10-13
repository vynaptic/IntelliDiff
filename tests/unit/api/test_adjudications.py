"""Adjudications over the API: an operator draws a reviewer sheet of an
ended cycle, a viewer downloads it, another operator scores its reviewed upload once; the sign-off request
attests it and freezes the cycle; every refusal with its binding message."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth.reports.adjudicate import SHEET_COLUMNS
from deltasleuth.reports.csv import parse_csv, render_csv
from tests.unit.reports.packs import samples

from .conftest import audited, said, session_as, signed

pytestmark = pytest.mark.db


@pytest.fixture
def client(packs_client: TestClient) -> TestClient:
    return packs_client


def reviewed(sheet: str, **first: str) -> bytes:
    """The sheet with each engine verdict agreed and a note on every row; ``first``: typed into row 1."""
    header, rows = parse_csv(sheet.encode())
    for row in rows:
        row |= {"reviewer_verdict": row["engine_verdict"].lower(), "reviewer_notes": "called the branch"}
    rows[0] |= first
    return render_csv(header, rows).encode()


def csv_post(client: TestClient, adjudication_id: str, data: bytes, headers: dict[str, str]) -> Any:
    return client.post(
        f"/v1/adjudications/{adjudication_id}/score",
        content=data,
        headers=headers | {"Content-Type": "text/csv"},
    )


def test_a_sheet_is_drawn_downloaded_scored_once_and_attested_by_the_sign_off(client: TestClient) -> None:
    cycle = signed(client, pairs=samples(52), sign=False)
    run_id = cycle.run_id
    olga, frank = session_as(client, "olga", "operator"), session_as(client, "frank", "operator")
    vic = session_as(client, "vic", "viewer")
    drawn = client.post(f"/v1/runs/{run_id}/cycles/1/adjudications", json={}, headers=olga)
    assert drawn.status_code == 201, drawn.text
    adjudication_id = drawn.json()["adjudication_id"]
    listed = client.get(f"/v1/runs/{run_id}/cycles/1/adjudications", headers=vic).json()
    sheet = client.get(f"/v1/adjudications/{adjudication_id}/sheet.csv", headers=vic)
    header, rows = parse_csv(sheet.content)
    scored = csv_post(client, adjudication_id, reviewed(sheet.text), frank)
    again = csv_post(client, adjudication_id, reviewed(sheet.text), frank)
    kept = parse_csv(client.get(f"/v1/adjudications/{adjudication_id}/sheet.csv", headers=vic).content)[1]
    assert drawn.json() == {
        "adjudication_id": adjudication_id,
        "salt": listed["items"][0]["salt"],
        "keys": [row["key"] for row in rows],
    }
    assert len(rows) == 50 and header == list(SHEET_COLUMNS)
    assert [j["adjudication_id"] for j in listed["items"]] == [adjudication_id]
    assert "sheet" not in listed["items"][0] and listed["items"][0]["drawn_by_name"] == "olga"
    assert sheet.headers["content-type"] == "text/csv; charset=utf-8"
    assert sheet.headers["content-disposition"] == f'attachment; filename="{adjudication_id}.csv"'
    assert scored.status_code == 200, scored.text
    item = scored.json()
    assert (item["passed"], item["scored_by_name"], item["score"]["adjudicated"]) == (True, "frank", 50)
    assert "sheet" not in item and all(row["reviewer_notes"] == "" for row in kept)  # no note is stored
    assert said(again) == (409, f"adjudication {adjudication_id} is already scored: draw a new sheet")
    assert [(o, d["passed"]) for _, o, d in audited(client, "adjudication.score")] == [
        (adjudication_id, True)
    ]
    assert [(o, d["cycle"]) for _, o, d in audited(client, "adjudication.draw")] == [(adjudication_id, 1)]
    # The request attests the scored sheet and freezes the cycle; the scorer may not approve it.
    second = client.post(f"/v1/runs/{run_id}/cycles/1/adjudications", json={"limit": 3}, headers=olga)
    requested = client.post(f"/v1/runs/{run_id}/cycles/1/signoff", headers=olga).json()
    frozen = f"cycle 1 of run {run_id} has a sign-off ({requested['approval_id']}): revoke it before a new"
    frozen += " adjudication"
    later = second.json()["adjudication_id"]
    sheet_2 = client.get(f"/v1/adjudications/{later}/sheet.csv", headers=vic).text
    replies = [
        client.post(f"/v1/runs/{run_id}/cycles/1/adjudications", headers=olga),
        csv_post(client, later, reviewed(sheet_2), frank),
    ]
    review = client.get(f"/v1/approvals/{requested['approval_id']}", headers=vic).json()
    assert second.status_code == 201 and len(second.json()["keys"]) == 3
    assert [said(r) for r in replies] == [(409, frozen)] * 2
    assert review["attests"]["adjudication"]["adjudication_id"] == adjudication_id  # the newest scored


def test_draw_and_score_refusals(client: TestClient) -> None:
    cycle = signed(client, pairs=samples(52), sign=False)
    run_id = cycle.run_id
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    drawn = client.post(f"/v1/runs/{run_id}/cycles/1/adjudications", json={"limit": 5}, headers=olga).json()
    sheet = client.get(f"/v1/adjudications/{drawn['adjudication_id']}/sheet.csv", headers=vic).text
    header, rows = parse_csv(sheet.encode())
    foreign = render_csv(header, [rows[0] | {"key": "K999"}]).encode()
    replies = [
        client.post(f"/v1/runs/{run_id}/cycles/1/adjudications", json={"limit": 0}, headers=olga),
        client.post(f"/v1/runs/{run_id}/cycles/1/adjudications", json={"limit": 501}, headers=olga),
        client.post(f"/v1/runs/{run_id}/cycles/2/adjudications", headers=olga),
        client.post("/v1/runs/R-x/cycles/1/adjudications", headers=olga),
        csv_post(client, "J-x", reviewed(sheet), olga),
        csv_post(client, drawn["adjudication_id"], b"key\n" + b"K001\n" * 300_000, olga),
        csv_post(client, drawn["adjudication_id"], foreign, olga),
        csv_post(client, drawn["adjudication_id"], reviewed(sheet, reviewer_verdict="maybe"), olga),
        client.post(f"/v1/runs/{run_id}/cycles/1/adjudications", headers=vic),
    ]
    assert [said(r) for r in replies] == [
        (422, "the adjudication limit must be 1 to 500"),
        (422, "the adjudication limit must be 1 to 500"),
        (409, f"cycle 2 of run {run_id} has not ended"),
        (404, "run R-x not found"),
        (404, "adjudication J-x not found"),
        (413, "the reviewed sheet is over 1 MiB"),
        (422, "row(s) for keys the run did not select (lines 2)"),
        (422, "reviewer_verdict must be one of MATCH, DIFFERENT, INDETERMINATE (lines 2)"),
        (403, "needs the operator role"),
    ]
    assert audited(client, "adjudication.score") == []
