"""Per-key labels stay in the store: a sampled key's stratum, sign-off stratum
and membership reach no per-key output (results, findings, a key's view, discrepancies.csv, retry.csv, the
report's findings, an agent tool's result) and every other output carries them only in aggregate, as a
sign-off stratum's counts and bounds. A sampled cycle's labelling reads none."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth.agent.tools import Scope, run_tool
from deltasleuth.context import Context
from deltasleuth.core.types import Confirmation, KeyResult, Outcome, SideState, SideStateKind
from deltasleuth.reports.csv import SAMPLE_COLUMNS
from deltasleuth.reports.data import load_run
from deltasleuth.reports.pdf import report_document
from deltasleuth.reports.summary import summary_document
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import approved, drawn, finding, person, sampled, upload

from .conftest import OIDC, Callers

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
PROFILE = sampled("customer-profile")
LABELS = ("segment=retail", "segment=other")
PER_KEY = ("stratum", "signoff_stratum", "signoff_member", "segment=")


async def _sampled(ctx: Context) -> tuple[str, str]:
    """A signed-off-able sampled run: C001 discrepant with a finding, C004 unresolved (in retry.csv)."""
    async with ctx.pool.connection() as conn:
        bob, carol = await person(conn, "bob"), await person(conn, "carol")
        spec_id = await upload(conn, PROFILE, bob)
        approval = await approved(conn, spec_id, bob, carol, scopes=["sampling"])
        run_id, draw_id = await drawn(conn, spec_id, PROFILE, bob, discrepant=["C001"], approval_id=approval)
        complete, failed = SideState(SideStateKind.COMPLETE), SideState(SideStateKind.ERROR, "timeout")
        discrepant = KeyResult(
            "C001", Outcome.MISMATCH, complete, complete, confirmation=Confirmation.RECHECKED
        )
        await repo.save_key_result(conn, run_id, 1, "m-C001", discrepant, [finding("HIGH")])
        unresolved = KeyResult(
            "C004", Outcome.INCONCLUSIVE, complete, failed, inconclusive_reason="timeout",
            confirmation=Confirmation.RECHECKED,
        )  # fmt: skip
        await repo.save_key_result(conn, run_id, 1, "m-C004", unresolved, [])
    return run_id, draw_id


def _keyed(value: Any) -> list[str]:
    """Where ``value`` (a response or a tool's result) names a per-key label or a label's text."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return [word for word in PER_KEY if word in text]


def test_no_per_key_output_names_a_key_s_labels(open_client: OpenClient, tmp_path: Path) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        ctx = client.app.state.ctx
        run_id, draw_id = client.portal.call(_sampled, ctx)
        viewer = Callers(client).token("viewer")
        base = f"/v1/runs/{run_id}"
        answers = {
            "results": client.get(f"{base}/results", headers=viewer),
            "findings": client.get(f"{base}/findings", headers=viewer),
            "key view": client.get(f"{base}/keys/m-C001", headers=viewer),
            "discrepancies.csv": client.get(f"{base}/reports/discrepancies.csv", headers=viewer),
            "retry.csv": client.get(f"{base}/reports/retry.csv", headers=viewer),
        }
        coverage = client.get(f"{base}/reports/coverage.csv", headers=viewer).text
        pdf = client.get(f"{base}/reports/report.pdf", headers=viewer)
        run = client.get(base, headers=viewer).json()
        shown = client.get(f"/v1/samples/{draw_id}", headers=viewer).json()
        scope = Scope(run_id, TESTER)
        tools = {
            name: client.portal.call(run_tool, ctx, scope, name, {})
            for name in ("get_findings", "get_run_summary")
        }
        document = client.portal.call(report_document, ctx, run_id)
        summary = summary_document(client.portal.call(load_run, ctx, run_id), tmp_path)
    assert {name: a.status_code for name, a in answers.items()} == dict.fromkeys(answers, 200)
    assert "m-C001" in answers["discrepancies.csv"].text and "m-C004" in answers["retry.csv"].text
    assert {name: _keyed(a.text) for name, a in answers.items() if _keyed(a.text)} == {}
    assert _keyed(tools["get_findings"]) == [] and tools["get_findings"]["findings"]
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # Aggregates: each sign-off stratum's row, never beside a key.
    rows = list(csv.DictReader(io.StringIO(coverage)))
    assert [r["stratum"] for r in rows] == ["*", *LABELS, "(contrast)"]
    assert next(csv.reader(io.StringIO(coverage)))[-len(SAMPLE_COLUMNS) :] == list(SAMPLE_COLUMNS)
    shown_cells = ("attempted", "compared", "mismatched", "absent_both", *SAMPLE_COLUMNS)
    assert [[r[name] for name in shown_cells] for r in rows] == [
        ["5", "5", "0", "0", *[""] * len(SAMPLE_COLUMNS)],  # the counters the fixture saved, unchanged
        ["3", "3", "1", "0", "standard", "0.001", "census", "3", "3", "0", "0", "", "", "", "failed"],
        ["3", "3", "0", "0", "standard", "0.001", "census", "3", "3", "0", "0", "", "", "", "passed"],
        ["0", "0", "0", "0", *[""] * len(SAMPLE_COLUMNS)],
    ]
    assert "m-C0" not in coverage
    sampled_section = next(s for s in document.sections if s.title == "Sample and bounds (observed)")
    assert [row[0] for row in sampled_section.rows] == list(LABELS)
    assert list(sampled_section.rows[0]) == [
        "segment=retail",
        "standard",
        "0.1%",
        "census",
        3,
        3,
        3,
        1,
        0,
        0,
        0,
        0,
        None,
        None,
        None,
        "failed",
    ]
    assert sampled_section.lines[2:] == [
        "Judgement: failed.",
        "No bound is given for the strata pooled.",
        "Contrast keys (compared, never bounded): 0, 0 compared, 0 discrepant.",
    ]
    assert "Sample and bounds (observed)" in [s.title for s in summary.sections]
    others = [s for s in document.sections if s is not sampled_section]
    assert not any(label in json.dumps([s.lines, s.rows], default=str) for s in others for label in LABELS)
    for aggregate in (run["cycles"][0]["sample"], shown, tools["get_run_summary"]["sample"]):
        text = json.dumps(aggregate, default=str)
        assert "m-C0" not in text and "signoff_member" not in text
    assert [s["label"] for s in tools["get_run_summary"]["sample"]["strata"]] == list(LABELS)


def test_no_labelling_output_of_a_sampled_cycle_names_a_stratum(open_client: OpenClient) -> None:
    """A labelling reads no stratum label: a sampled cycle's populations are its
    compared keys, members and contrast alike, and neither the sheet, the score nor a text names a key's
    labels."""
    truth = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01"}
    with open_client(auth={"oidc": OIDC}) as client:
        run_id, _ = client.portal.call(_sampled, client.app.state.ctx)
        operator, viewer = Callers(client).token("operator"), Callers(client).token("viewer")
        body = {"size": 10, "per_cause": 2, "agreed": 10, "truth": truth}
        drawn = client.post(f"/v1/runs/{run_id}/cycles/1/labellings", json=body, headers=operator)
        labelling_id = drawn.json()["labelling_id"]
        sheet = client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=viewer)
        rows = list(csv.DictReader(io.StringIO(sheet.text)))
        for row in rows:
            row |= {"label": "BOTH_RIGHT", "basis": "BUSINESS_RULE"}
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        labelled = client.post(
            f"/v1/labellings/{labelling_id}/labels", content=out.getvalue().encode(),
            headers=operator | {"Content-Type": "text/csv"},
        )  # fmt: skip
        answers = {
            "draw": drawn,
            "sheet": client.get(f"/v1/labellings/{labelling_id}/sheet.csv", headers=viewer),
            "labels": labelled,
            "item": client.get(f"/v1/labellings/{labelling_id}", headers=viewer),
            "list": client.get(f"/v1/runs/{run_id}/cycles/1/labellings", headers=viewer),
        }
        run = client.get(f"/v1/runs/{run_id}", headers=viewer).json()
    assert {name: a.status_code for name, a in answers.items()} == dict.fromkeys(answers, 200) | {"draw": 201}
    # One discrepant key (C001) and the agreed ones, contrast or member alike; C004 was left unresolved.
    assert [(r["key"], r["population"]) for r in rows][:1] == [("m-C001", "discrepant")]
    assert sorted(r["key"] for r in rows[1:]) == ["m-C002", "m-C003", "m-C005", "m-C006"]
    assert answers["item"].json()["accuracy"]["indeterminate"] == 1
    assert {name: _keyed(a.text) for name, a in answers.items() if _keyed(a.text)} == {}
    assert _keyed(run["cycles"][0]["labellings"]) == []
