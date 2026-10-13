"""A soak run over a mock pair for two cycles: the key that drifted in the first cycle matches in the
second, the change is listed (API data and the PDF), and each cycle keeps its own counters. And the table
demo as a soak whose seeded differences are fixed between its cycles 2 and 3: its pair moves from not ready
to parity ready, and the API, the CLI and the reports agree (docs/mvp/READINESS.md §17); its brief shows the
stored rate fall at cycle 3 and the root-cause groups of the first two cycles closed."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from deltasleuth.api import create_app
from deltasleuth.cli.main import app as cli
from deltasleuth.config import load_settings
from deltasleuth.core.readiness import DEFINITION
from deltasleuth.core.spec import load_spec_file
from deltasleuth.reports.csv import parse_csv
from deltasleuth.reports.pdf import report_document
from deltasleuth.reports.render import Document, Section
from deltasleuth.runner import create_run, outcome_changes
from mocks.registry import pair_dir
from tests.conftest import TESTER

from .conftest import Stack, finish, pdf_text, store_spec

pytestmark = [pytest.mark.e2e, pytest.mark.db]
DRIFTING = "ORG-000029"  # answers stale once, then catches up
DEMO = Path(__file__).parents[2] / "examples" / "tables"
PAIR = "crm-customers-vs-platform-users"
# What the demo seeds wrong on the platform (examples/tables/seed.sql), put right: three rows as the CRM
# holds them, the missing customer migrated, and the two rows the CRM does not migrate removed.
FIXED = """
    UPDATE public.users SET phone = '+447700900005' WHERE user_id = 100005;
    UPDATE public.users SET status = 'active' WHERE user_id = 100007;
    UPDATE public.users SET balance = 11.25 WHERE user_id = 100009;
    INSERT INTO public.users (user_id, email, name, phone, status, country, balance, created_at)
    VALUES (100011, 'customer11@example.com', 'Customer 11', '+447700900011', 'active', 'GB', 13.75,
            timestamptz '2020-01-01 00:00:00+00' + 11 * interval '1 day 37 minutes');
    DELETE FROM public.users WHERE user_id IN (100013, 101001);
"""
SEEDED = ("100005", "100007", "100009", "100011", "100013", "101001")  # identifier keys: never shown
M9 = (
    "Mismatch rate 0: 0 different of 989 compared. Known differences and noise-only keys count as matching;"
    " a known difference under a bug rule counts as different."
)


async def test_a_two_cycle_soak_lists_the_outcome_changes(serve: Any, make_stack: Any) -> None:
    spec = load_spec_file(pair_dir("linker") / "spec.yaml")
    spec["execution"] |= {"cycleInterval": "0s", "stop": {"maxCycles": 2}}
    async with make_stack(serve("linker").targets()).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, spec), "soak", who=TESTER)
        run = await finish(ctx, run_id)
        changes = await outcome_changes(ctx, run_id)
        second, first = await report_document(ctx, run_id), await report_document(ctx, run_id, cycle=1)
    assert (run["status"], run["cycle"], run["stop_reason"]) == ("completed", 2, "stop_condition:maxCycles")
    assert changes == [{"cycle": 2, "key_masked": DRIFTING, "from_outcome": "DRIFT", "to_outcome": "MATCH"}]
    assert _section(second, "Outcome changes").rows == [(2, DRIFTING, "DRIFT", "MATCH")]
    was, now = (_counters(doc) for doc in (first, second))  # each cycle's own counters
    assert (now["match"] - was["match"], was["indeterminate"] - now["indeterminate"]) == (1, 1)


def _section(doc: Document, title: str) -> Section:
    return next(s for s in doc.sections if s.title.startswith(title))


def _counters(doc: Document) -> dict[str, Any]:
    cells = [cell for row in _section(doc, "Coverage counters").rows for cell in row]  # counter, value, ...
    return dict(zip(cells[::2], cells[1::2], strict=True))


def test_the_table_demo_soak_moves_from_not_ready_to_parity_ready(
    make_stack: Callable[..., Stack], databases: Callable[..., str], tmp_path: Path
) -> None:
    _, legacy, platform = (
        "\n".join(part.splitlines()[1:]) for part in (DEMO / "seed.sql").read_text().split("\\connect")
    )
    dsn = {"crm-legacy": databases("a", legacy), "platform-users": databases("b", platform)}
    for name, value in dsn.items():
        (tmp_path / name).write_text(value)
    targets = {name: {"dsn_ref": f"file:{name}", "rate_per_s": 1000} for name in dsn}
    stack = make_stack(yaml.safe_dump({"targets": targets}))
    spec = load_spec_file(DEMO / "spec.yaml")
    spec["execution"] |= {"cycleInterval": "3s", "stop": {"maxCycles": 5}}
    local = {
        "base_url": "http://127.0.0.1",
        "client": ("127.0.0.1", 50000),
        "headers": {"X-CSRF-Token": "dev"},
    }

    def waited(read: Callable[[], Any], timeout: float = 60) -> Any:
        deadline = time.monotonic() + timeout
        while not (found := read()):
            assert time.monotonic() < deadline, "timed out"
            time.sleep(0.05)
        return found

    with TestClient(create_app(load_settings(stack.settings), worker=True), **local) as client:
        spec_id = client.post("/v1/specs", content=yaml.safe_dump(spec)).json()["spec_id"]
        run_id = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "soak"}).json()["run_id"]

        def pair() -> dict[str, Any]:
            return client.get(f"/v1/readiness/pairs/{PAIR}").json()

        def run() -> dict[str, Any]:
            return client.get(f"/v1/runs/{run_id}").json()

        third = waited(lambda: (found := run())["cycle"] == 3 and found)
        with psycopg.connect(dsn["platform-users"], autocommit=True) as conn:
            conn.execute(FIXED)  # between cycles 2 and 3
        assert datetime.now(UTC) < datetime.fromisoformat(third["scan"]["due"]), "cycle 2 ended too late"
        before = waited(lambda: (found := pair())["evidence"] and found["evidence"]["cycle"] == 2 and found)
        waited(lambda: run()["status"] == "completed")
        after = waited(
            lambda: (found := pair())["since"] is not None and found["state"] != "not_ready" and found
        )
        listed = client.get("/v1/readiness").json()
        rows = client.get("/v1/readiness/transitions", params={"pair": PAIR}).json()["items"]
        table = parse_csv(client.get("/v1/readiness/reports/dashboard.csv").content)[1]
        points = parse_csv(client.get(f"/v1/runs/{run_id}/reports/readiness.csv").content)[1]
        ctx = client.app.state.ctx
        reports = {cycle: client.portal.call(report_document, ctx, run_id, cycle) for cycle in (2, 5)}
        pdf = pdf_text(client.get(f"/v1/runs/{run_id}/reports/report.pdf?cycle=2").content)
        brief = client.get(f"/v1/readiness/pairs/{PAIR}/brief").json()
    config = ("--config", str(stack.settings))
    printed = CliRunner().invoke(cli, ["readiness", *config]).stdout
    as_json = json.loads(CliRunner().invoke(cli, ["readiness", "--json", *config]).stdout)
    passed = CliRunner().invoke(cli, ["readiness", "--fail-unless", "parity_ready", *config])

    over = (
        f"cycle 2 of run {run_id} has 6 discrepant or unresolved key(s) of 991 observed whole; its pooled"
        " 0.1% target allows 0: 6 over; 0 key(s) suppressed (known differences or noise only)"
    )
    by = {gate["gate"]: gate for gate in before["gates"]}
    assert (before["state"], before["blocking"], by["targets"]["text"]) == (
        "not_ready", ["critical", "targets", "convergence"], over,
    )  # fmt: skip
    # Every surface agrees on where the pair is now: the API, the recorded rows, the CLI and the CSVs.
    version = f"1.0.0 (spec {spec_id})"
    converged = (
        f"the newest 3 evidence cycles of {version} meet every per-cycle gate: at least the 3 the gate needs"
    )
    by = {gate["gate"]: gate for gate in after["gates"]}
    assert (after["state"], after["score"], after["blocking"]) == (
        "parity_ready", {"passed": 5, "required": 5}, [],
    )  # fmt: skip
    assert (after["evidence"]["cycle"], after["evidence"]["basis"], by["convergence"]["text"]) == (
        5, "census", converged,
    )  # fmt: skip
    assert [(i["pair"], i["state"], i["score"]) for i in listed["items"]] == [
        (PAIR, "parity_ready", after["score"])
    ]
    moves = [(row["detail"]["from"], row["detail"]["to"]) for row in reversed(rows)]
    assert (moves[0], moves[-1]) == ((None, "not_ready"), ("not_ready", "parity_ready"))
    assert {to for _, to in moves[:-1]} == {"not_ready"} and rows[-1]["detail"]["causes"] == ["baseline"]
    assert "cycle" in rows[0]["detail"]["causes"] and rows[0]["at"] == after["since"]
    assert {(row["pair"], row["state"], row["gates_passed"], row["gates_required"]) for row in table} == {
        (PAIR, "parity_ready", "5", "5")
    }
    assert [(i["pair"], i["state"]) for i in as_json["items"]] == [(PAIR, "parity_ready")]
    lines = printed.splitlines()
    assert lines[0] == DEFINITION and passed.exit_code == 0
    assert any(
        line.startswith(f"{PAIR} {version}  -  parity ready  5 of 5 gates  cycle 5 of {run_id} (")
        for line in lines
    ), printed
    # The trend: six discrepant keys twice, then none; of the six, four read equal and two left both tables.
    assert [(p["cycle"], p["total"], p["different"], p["fails"]) for p in points] == [
        ("1", "991", "6", "critical targets"), ("2", "991", "6", "critical targets"), ("3", "989", "0", ""),
        ("4", "989", "0", ""), ("5", "989", "0", ""),
    ]  # fmt: skip
    assert [(p["new"], p["persistent"], p["fixed"], p["other"]) for p in points[1:3]] == [
        ("0", "6", "0", "0"), ("0", "0", "4", "2"),
    ]  # fmt: skip
    # Each cycle's report says what held as of it, whatever came after.
    was, now = ({row[0]: row[2:] for row in _section(reports[c], "Pair readiness").rows} for c in (2, 5))
    assert (was["targets"], now["convergence"]) == (("not met", over), ("met", converged))
    assert "Pair readiness" in pdf and "pooled 0.1% target allows 0: 6 over" in " ".join(pdf.split())
    # The brief restates it: the stored rate falls at cycle 3, and the groups the fix removed read closed.
    assert [(p["cycle"], p["mismatch_rate"], p["root_causes"], p["marks"]) for p in brief["points"]] == [
        (1, "0.006054", 6, []), (2, "0.006054", 6, []), (3, "0", 0, []), (4, "0", 0, []), (5, "0", 0, []),
    ]  # fmt: skip
    causes = brief["causes"]
    assert (brief["cycle"]["cycle"], brief["cycle"]["notes"]) == (5, [M9]) and causes["text"].endswith(
        "0 opened in the newest, 0 still open, 6 closed (not observed in it)"
    )
    assert [(g["state"], g["first"]["cycle"], g["last"]["cycle"], g["since"]) for g in causes["items"]] == [
        ("closed", 1, 2, [])
    ] * 6
    texts = [text for doc in reports.values() for s in doc.sections for text in s.lines] + [pdf]
    published = [json.dumps([before, after, listed, rows, table, points, as_json, brief]), printed, *texts]
    assert not any(key in text for text in published for key in SEEDED)
