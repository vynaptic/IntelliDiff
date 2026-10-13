"""Labelled accuracy over the API (docs/mvp/ACCURACY.md §10, §11): an operator draws a labelling of an
ended cycle against a named truth source, a viewer reads it and downloads its sheet, an operator uploads the
filled sheet once; the sign-off request attests it, freezes the cycle and keeps its labeller from
approving; every auth mode serves the routes, and only a person's labelling is attested; a purged cycle
answers 410 and shows its tombstones; every refusal with its binding message."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from deltasleuth.auth.principal import expand
from deltasleuth.context import Context
from deltasleuth.reports import accuracy
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.runner import purge
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.reports.conftest import doc
from tests.unit.reports.packs import listed_spec, samples
from tests.unit.store.four_eyes import request_signoff

from .conftest import Callers, Seeded, audited, said, session_as, signed

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
TRUTH = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01"}
TODAY = f"{datetime.now(UTC):%Y-%m-%d}"
NOTE = "called the branch about 0712345678"


@pytest.fixture
def client(packs_client: TestClient) -> TestClient:
    return packs_client


def pairs(different: int = 8, **options: Any) -> dict[str, Any]:
    """Twenty agreeing keys and ``different`` whose status differs (one root cause)."""
    found = samples(20, **options)
    for key in list(found)[:different]:
        found[key] = (doc(key, status="A"), doc(key, status="B"))
    return found


def filled(sheet: str, *, label: str = "b_right", notes: str = NOTE, **first: str) -> bytes:
    """The sheet with every item ``label`` on the system of record and a note; ``first``: typed into row 1."""
    header, rows = parse_csv(sheet.encode())
    for row in rows:
        row |= {"label": label, "basis": "system_of_record", "notes": notes}
    rows[0] |= first
    return render_csv(header, rows).encode()


def upload(client: TestClient, labelling_id: str, data: bytes, headers: dict[str, str]) -> Any:
    return client.post(
        f"/v1/labellings/{labelling_id}/labels", content=data, headers=headers | {"Content-Type": "text/csv"}
    )


def draw(client: TestClient, run_id: str, headers: dict[str, str], cycle: int = 1, **body: Any) -> Any:
    return client.post(
        f"/v1/runs/{run_id}/cycles/{cycle}/labellings", json={"truth": TRUTH} | body, headers=headers
    )


def test_a_labelling_is_drawn_read_labelled_once_and_attested_by_the_sign_off(client: TestClient) -> None:
    cycle = signed(client, pairs=pairs(), sign=False)
    run_id = cycle.run_id
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    fresh = {"username": "erin", "auth_time": datetime.now(UTC)}  # an approval step needs a recent sign-in
    erin = Callers(client).session(*expand(["rule_approver"]), **fresh)
    drawn = draw(client, run_id, olga, size=5, per_cause=0, agreed=3)
    assert drawn.status_code == 201, drawn.text
    labelling_id, items = drawn.json()["labelling_id"], drawn.json()["items"]
    again = draw(client, run_id, olga, size=5, per_cause=0, agreed=3)  # a retry: the first labelling
    listed = client.get(f"/v1/runs/{run_id}/cycles/1/labellings", headers=vic).json()["items"]
    blank = client.get(f"/v1/labellings/{labelling_id}", headers=vic).json()
    sheet = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=vic)
    header, rows = parse_csv(sheet.content)
    assert drawn.json() == {
        "labelling_id": labelling_id, "salt": listed[0]["salt"], "warnings": blank["selection"]["warnings"],
        "items": [{"item": n, "key": row["key"], "population": row["population"],
                   "root_cause_id": row["root_cause_id"]} for n, row in enumerate(rows, start=1)],
    }  # fmt: skip
    assert [i["population"] for i in items] == ["discrepant"] * 5 + ["agreed"] * 3
    assert again.status_code == 200 and again.json() == drawn.json() | {"warnings": [
        *drawn.json()["warnings"],
        f"labelling {labelling_id} with these parameters was drawn by you at"
        f" {datetime.fromisoformat(listed[0]['drawn_at']).astimezone(UTC):%H:%M:%S} UTC and is returned, not"
        " drawn again",
    ]}  # fmt: skip
    assert header == list(accuracy.SHEET_COLUMNS) and [j["labelling_id"] for j in listed] == [labelling_id]
    assert sheet.headers["content-type"] == "text/csv; charset=utf-8"
    assert sheet.headers["content-disposition"] == f'attachment; filename="{labelling_id}.csv"'
    # A list item holds ids, actors, dates, sizes, truth and hashes; the item its selection's counts too.
    assert set(listed[0]) == {*accuracy.LISTED, "summary", "attested"} and listed[0]["item_count"] == 8
    assert "sheet" not in blank and "items" not in blank["selection"] and blank["accuracy"] is None
    assert listed[0]["summary"] == (
        f"{labelling_id} run {run_id} cycle 1: drawn {TODAY} by olga against CBS-PROD.account_master as of"
        " 2026-10-01; not labelled"
    )
    assert (blank["lines"], blank["limits"], blank["attested"]) == ([], accuracy.LIMITS, None)
    assert blank["selection"]["counts"]["discrepant"] == {"items": 8, "keys": 8, "causes": 1, "drawn": 5}
    labelled = upload(client, labelling_id, filled(sheet.text), erin)
    once = upload(client, labelling_id, filled(sheet.text), erin)
    assert labelled.status_code == 200, labelled.text
    item = labelled.json()
    kept = parse_csv(client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=vic).content)[1]
    assert (item["labelled_by_name"], item["accuracy"]["discrepant"]["labels"]["B_RIGHT"]["x"]) == ("erin", 5)
    assert "sheet" not in item and all(row["notes"] == "" and row["label"] == "B_RIGHT" for row in kept)
    assert said(once) == (409, f"labelling {labelling_id} is already labelled: draw a new sheet")
    unattested = f"labelled {TODAY} by erin against CBS-PROD.account_master as of 2026-10-01"
    assert item["lines"][0] == (
        f"Labelling {labelling_id} drawn {TODAY} by olga, {unattested}; no sign-off attests it."
    )
    assert item["summary"].startswith(
        f"{labelling_id} run {run_id} cycle 1: 5 of 8 discrepancies {unattested} (no sign-off attests it): b"
        " (new) alone right 5, a (legacy) alone right 0, neither 0, did not matter 0, undecidable 0; b (new)"
        " wrong on 0 of 5 decided (0% to 52.19%)"
    )
    labels = audited(client, "accuracy.label")
    assert [(o, d["labelled"], d["undecidable"], d["not_reviewed"]) for _, o, d in labels] == [
        (labelling_id, 8, 0, 0)
    ]
    assert [(o, d["item_count"]) for _, o, d in audited(client, "accuracy.draw")] == [(labelling_id, 8)]
    # The run shows its cycle's labellings; the request attests the labelling a person labelled and freezes
    # the cycle; its labeller may not approve it.
    second = draw(client, run_id, olga, size=2, per_cause=0)
    shown = client.get(f"/v1/runs/{run_id}", headers=vic).json()["cycles"][0]["labellings"]
    requested = client.post(f"/v1/runs/{run_id}/cycles/1/signoff", headers=olga).json()
    signoff_id = requested["approval_id"]
    frozen = f"cycle 1 of run {run_id} has a sign-off ({signoff_id}): revoke it before a new labelling"
    later = second.json()["labelling_id"]
    sheet_2 = client.get(f"/v1/labellings/{later}/sheet.csv", headers=vic).text
    replies = [draw(client, run_id, olga, size=1), upload(client, later, filled(sheet_2), olga)]
    review = client.get(f"/v1/approvals/{signoff_id}", headers=vic).json()
    attested = client.get(f"/v1/labellings/{labelling_id}", headers=vic).json()
    decision = {"decision": "approve", "attests_sha256": review["attests_sha256"]}
    refused = client.post(f"/v1/approvals/{signoff_id}/decision", json=decision, headers=erin)
    assert second.status_code == 201 and [v["labelling_id"] for v in shown] == [later, labelling_id]
    assert shown[1]["summary"] == item["summary"] and shown[0]["attested"] is None
    # The run, which its screen polls, lists each labelling as the list does: without its score.
    assert all(set(v) == set(listed[0]) for v in shown) and shown[1]["score_sha256"] == item["score_sha256"]
    assert [said(r) for r in replies] == [(409, frozen)] * 2
    assert review["attests"]["format"] == 3
    assert review["attests"]["accuracy"]["labelling_id"] == labelling_id  # the newest a person labelled
    assert review["attests"]["labellings"] == {"drawn": 2, "labelled": 1}
    assert review["review"]["lines"][:2] == [
        f"Labelling {labelling_id} drawn {TODAY} by olga, {unattested}, attested by sign-off {signoff_id}.",
        f"Labelling {labelling_id} is one of 2 drawn for cycle 1 (1 labelled).",
    ]
    assert attested["attested"] == signoff_id and attested["lines"] == review["review"]["lines"]
    assert [(v["labelling_id"], v["item_count"], v["attested"]) for v in review["review"]["labellings"]] == [
        (later, 2, False), (labelling_id, 8, True),
    ]  # fmt: skip
    assert said(refused) == (
        409, f"approval needs an approver other than you: you labelled its labelling {labelling_id}",
    )  # fmt: skip
    assert any(p["name"] == "erin" for p in review["review"]["exclusion"]["persons"])


def test_draw_and_upload_refusals(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cycle = signed(client, pairs=pairs(), sign=False)
    run_id = cycle.run_id
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    drawn = draw(client, run_id, olga, size=4, per_cause=0).json()
    labelling_id = drawn["labelling_id"]
    sheet = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=vic).text
    header, rows = parse_csv(sheet.encode())
    foreign = render_csv(header, [rows[0] | {"item": "9", "label": "A_RIGHT", "basis": "BUSINESS_RULE"}])
    missing = "V-" + "0" * 26
    # A second column named label, holding other labels than the one the uploader reviewed.
    twice = [
        line + (b",label" if n == 0 else b",A_RIGHT") for n, line in enumerate(filled(sheet).splitlines())
    ]
    # A quote left open in a note: it swallows the rows after it, over the CSV reader's own cell limit.
    opened = filled(sheet, notes="").split(b"\r\n")
    opened[1] += b'"' + b"x" * 140_000
    replies = [
        draw(client, run_id, olga, size=1001),
        draw(client, run_id, olga, size=0, per_cause=0),
        draw(client, run_id, olga, truth={"source": "the core system", "as_of": "2026-10-01"}),
        draw(client, run_id, olga, truth={"source": "CBS", "as_of": "1 October"}),
        draw(client, run_id, olga, truth={"source": "CBS"}),
        draw(client, run_id, olga, accepted=1),
        draw(client, run_id, olga, cycle=2),
        draw(client, "R-x", olga),
        upload(client, missing, filled(sheet), olga),
        client.get(f"/v1/labellings/{missing}", headers=vic),
        client.get(f"/v1/labellings/{missing}/sheet.csv", headers=vic),
        client.get("/v1/runs/R-x/cycles/1/labellings", headers=vic),
        upload(client, labelling_id, b"item\n" + b"1\n" * 600_000, olga),
        upload(client, labelling_id, foreign.encode(), olga),
        upload(client, labelling_id, b"\r\n".join(twice), olga),
        upload(client, labelling_id, b"\r\n".join(opened), olga),
        upload(client, labelling_id, filled(sheet, label="maybe"), olga),
        upload(client, labelling_id, filled(sheet, basis=""), olga),
        draw(client, run_id, vic),
        upload(client, labelling_id, filled(sheet), vic),
    ]
    assert [said(r) for r in replies] == [
        (422, "size must be 0 to 1,000, per_cause 0 to 20, accepted and agreed 0 to 500"),
        (422, "a labelling draws at least one item: set size, per_cause, accepted or agreed above 0"),
        (422, "truth.source must be a system identifier: 1 to 64 letters, digits and _ . : / -, starting with"
              " a letter"),
        (422, "truth.as_of must be a date (YYYY-MM-DD) no later than today"),
        (422, "truth.as_of must be a date (YYYY-MM-DD) no later than today"),
        (409, f"cycle 1 of run {run_id} has no accepted key to label: set accepted to 0"),
        (409, f"cycle 2 of run {run_id} has not ended"),
        (404, "run R-x not found"),
        (404, f"labelling {missing} not found"),
        (404, f"labelling {missing} not found"),
        (404, f"labelling {missing} not found"),
        (404, "run R-x not found"),
        (413, "the labelled sheet is over 1 MiB: empty the notes column and upload it again"),
        (422, "row(s) for items the labelling did not draw (lines 2)"),
        (422, "the labelled sheet names column(s) label more than once"),
        (422, "every drawn item needs a label: mark UNDECIDABLE with a reason where the truth cannot be"
              " established, with reason NOT_REVIEWED where nobody reviewed it (items 2, 3, 4)"),
        (422, "label must be one of A_RIGHT, B_RIGHT, BOTH_WRONG, BOTH_RIGHT, UNDECIDABLE (lines 2, 3, 4,"
              " 5)"),
        (422, "basis must be one of SYSTEM_OF_RECORD, SOURCE_DOCUMENT, BUSINESS_RULE, OWNER_CONFIRMED for a"
              " decided label, and empty for UNDECIDABLE (lines 2)"),
        (403, "needs the operator role"),
        (403, "needs the operator role"),
    ]  # fmt: skip
    assert said(client.post(f"/v1/runs/{run_id}/cycles/1/labellings", json={}, headers=olga))[0] == 422
    # A cycle the store cannot hold is refused, drawn or listed; the largest it can hold has no labelling.
    unheld = [client.get(f"/v1/runs/{run_id}/cycles/{n}/labellings", headers=vic) for n in (0, 2**31, 10**11)]
    unheld.append(draw(client, run_id, olga, cycle=10**11))
    assert [r.status_code for r in unheld] == [422] * 4
    assert client.get(f"/v1/runs/{run_id}/cycles/{2**31 - 1}/labellings", headers=vic).json() == {"items": []}
    assert audited(client, "accuracy.label") == [] and len(audited(client, "accuracy.draw")) == 1
    # A draw the store stops answers 504 and stores nothing.
    monkeypatch.setattr(accuracy, "DRAW_TIMEOUT_S", 1)
    with psycopg.connect(client.app.state.ctx.pool.conninfo) as held:
        held.execute("LOCK TABLE ds.findings IN ACCESS EXCLUSIVE MODE")
        stopped = draw(client, run_id, olga, size=3)
    assert said(stopped) == (
        504,
        f"the draw of cycle 1 of run {run_id} was stopped after 1 s (QueryCanceled): nothing was drawn; draw"
        " again with accepted and agreed at 0, or when the store is less busy",
    )
    assert len(client.get(f"/v1/runs/{run_id}/cycles/1/labellings", headers=vic).json()["items"]) == 1


def test_a_tokenised_key_needs_its_acknowledgement(client: TestClient) -> None:
    security = {"unlisted": "tokenize", "tokenize": [{"path": "$.name", "class": "name"}]}
    spec = listed_spec(pii=True, security=security)
    cycle = signed(client, spec=spec, pairs=pairs(pii=True), sign=False)
    olga = session_as(client, "olga", "operator")
    refused = draw(client, cycle.run_id, olga, size=3)
    drawn = draw(client, cycle.run_id, olga, size=3, acknowledge_unlocatable=True)
    assert said(refused) == (
        422,
        "the key is tokenised (class identifier) and the spec shows no path in clear: labellers cannot find"
        " records in the truth source until the per-key reveal (AUTH §11) exists; set"
        " acknowledge_unlocatable to draw for labels a business rule decides without the record"
        " (docs/mvp/ACCURACY.md §4)",
    )
    assert drawn.status_code == 201 and drawn.json()["warnings"][0] == (
        "the key is tokenised (class identifier) and the spec shows no path in clear: expect UNDECIDABLE"
        " (TOKENISED) rows (acknowledged)"
    )
    assert all(item["key"].startswith("tok:identifier:") for item in drawn.json()["items"])
    key_shaped = draw(client, cycle.run_id, olga, truth=TRUTH | {"source": "CBS.0712345601"})
    assert said(key_shaped) == (
        422, "truth.source holds a key-shaped value: name the system without customer keys",
    )  # fmt: skip


async def _ended(ctx: Context, run_id: str) -> None:
    async with ctx.pool.connection() as conn:
        await repo.set_run_status(conn, run_id, "completed")
        await repo.end_cycle(conn, run_id)


@pytest.mark.parametrize("mode", ["tokens", "developer"])
def test_tokens_and_developer_modes_label_and_are_never_attested(
    open_client: OpenClient, seed: Callable[[TestClient], Seeded], callers: Any, mode: str
) -> None:
    with open_client(**({"auth": {}} if mode == "tokens" else {})) as client:
        run_id = seed(client).run_id
        client.portal.call(_ended, client.app.state.ctx, run_id)
        operator = callers(client).token("operator") if mode == "tokens" else {}
        viewer = callers(client).token("viewer", "reader") if mode == "tokens" else {}
        drawn = draw(client, run_id, operator, size=1, per_cause=0, truth=TRUTH | {"source": "svc-a"})
        assert drawn.status_code == 201, drawn.text
        labelling_id = drawn.json()["labelling_id"]
        sheet = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=viewer).text
        labelled = upload(client, labelling_id, filled(sheet), operator)
        assert labelled.status_code == 200, labelled.text
        shown = client.get(f"/v1/labellings/{labelling_id}", headers=viewer).json()
        if mode == "tokens":
            assert said(draw(client, run_id, viewer)) == (403, "needs the operator role")
    actor = shown["labelled_by"]
    by = {
        "tokens": f"through token ci-bot ({actor.removeprefix('token:')})",
        "developer": "in developer mode (dev:loopback)",
    }[mode]
    assert actor.startswith("token:T-" if mode == "tokens" else "dev:loopback")
    assert shown["lines"][0].split(", labelled ")[1] == (
        f"{TODAY} {by} against svc-a as of 2026-10-01, not by a signed-in person: no sign-off attests it."
    )
    assert shown["attested"] is None and "(no sign-off attests it)" in shown["summary"]
    assert drawn.json()["warnings"][0].startswith("truth.source svc-a names side a (old)")
    assert shown["lines"][1].startswith("Warning: the truth source names side a (old)")
    assert shown["summary"].endswith("; CIRCULAR: the truth source names side a")


def test_a_draw_or_upload_racing_a_sign_off_request_gets_409(client: TestClient) -> None:
    """A request holding the cycle's row (FOR KEY SHARE, then its insert) makes a draw and an upload wait
    for it (they take the row FOR UPDATE): once it commits, both find its sign-off."""
    cycle = signed(client, pairs=pairs(), sign=False)
    run_id, ctx = cycle.run_id, client.app.state.ctx
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    labelling_id = draw(client, run_id, olga, size=2, per_cause=0).json()["labelling_id"]
    sheet = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=vic).text
    requested, found = threading.Event(), {}

    async def request() -> None:
        async with ctx.pool.connection() as conn:
            found["signoff"] = await request_signoff(conn, run_id, 1, cycle.people["olga"], cycle.spec)
            requested.set()
            await asyncio.sleep(1.5)  # the request's transaction is open while both calls arrive

    def call(name: str, send: Callable[[], Any]) -> None:
        found[name] = send()

    racing = client.portal.start_task_soon(request)
    assert requested.wait(10)
    callers = [
        threading.Thread(target=call, args=("draw", lambda: draw(client, run_id, olga, size=1))),
        threading.Thread(
            target=call, args=("upload", lambda: upload(client, labelling_id, filled(sheet), olga))
        ),
    ]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(30)
    racing.result(30)
    frozen = f"cycle 1 of run {run_id} has a sign-off ({found['signoff']}): revoke it before a new labelling"
    assert [said(found[name]) for name in ("draw", "upload")] == [(409, frozen)] * 2
    listed = client.get(f"/v1/runs/{run_id}/cycles/1/labellings", headers=vic).json()["items"]
    assert [(v["labelling_id"], v["labelled_at"]) for v in listed] == [(labelling_id, None)]


def test_a_purged_cycle_answers_410_and_shows_its_tombstones(client: TestClient) -> None:
    cycle = signed(client, pairs=pairs(), sign=False)
    run_id, ctx = cycle.run_id, client.app.state.ctx
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    labelling_id = draw(client, run_id, olga, size=3, per_cause=0).json()["labelling_id"]
    blank = draw(client, run_id, olga, size=2, per_cause=0).json()["labelling_id"]
    sheet = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=vic).text
    labelled = upload(client, labelling_id, filled(sheet), olga).json()
    client.portal.call(purge.erase, ctx, TESTER, run_id, "test erasure", 1)
    gone = (
        410,
        f"cycle 1 of run {run_id} was purged on {TODAY} (erasure): its keys, results, findings and evidence"
        " are deleted",
    )
    replies = [
        draw(client, run_id, olga),
        client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=vic),
        upload(client, labelling_id, filled(sheet), olga),
        upload(client, blank, filled(sheet), olga),
    ]
    assert [said(r) for r in replies] == [gone] * 4
    listed = client.get(f"/v1/runs/{run_id}/cycles/1/labellings", headers=vic).json()["items"]
    item = client.get(f"/v1/labellings/{labelling_id}", headers=vic).json()
    shown = client.get(f"/v1/runs/{run_id}", headers=vic).json()["cycles"][0]["labellings"]
    assert [v["labelling_id"] for v in listed] == [blank, labelling_id] == [v["labelling_id"] for v in shown]
    assert all(v["purged_at"] is not None and "sheet" not in v for v in (*listed, item, *shown))
    kept = ("accuracy", "summary", "labels_sha256", "score_sha256", "labelled_by_name")
    assert {name: item[name] for name in kept} == {name: labelled[name] for name in kept}
    assert item["lines"] == labelled["lines"] and listed[1]["summary"] == labelled["summary"]
    assert listed[0]["summary"].endswith("; not labelled") and listed == shown
    assert not any("accuracy" in v or "selection" in v for v in listed)


def test_a_purge_between_the_draws_two_statements_answers_410(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draw holds no lock on the cycle: a purge that deletes its results once the draw's statement took
    its rows, before the drawn items' sheet columns are read, is answered 410, and nothing is stored."""
    cycle = signed(client, pairs=pairs(), sign=False)
    run_id, ctx = cycle.run_id, client.app.state.ctx
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    drawing = repo.labelling_draw

    async def then_purged(conn: repo.Conn, run_id: str, cycle: int, **sizes: Any) -> list[repo.Row]:
        rows = await drawing(conn, run_id, cycle, **sizes)
        await purge.erase(ctx, TESTER, run_id, "test erasure", 1)
        return rows

    monkeypatch.setattr(repo, "labelling_draw", then_purged)
    assert said(draw(client, run_id, olga, size=3, per_cause=0)) == (
        410,
        f"cycle 1 of run {run_id} was purged on {TODAY} (erasure): its keys, results, findings and evidence"
        " are deleted",
    )
    assert client.get(f"/v1/runs/{run_id}/cycles/1/labellings", headers=vic).json()["items"] == []
