"""Cut-over readiness over the API: the pairs worst first with their
programmes' and entity types' roll-ups, the filters and their refusals, what was recorded as of a time, a
pair's detail with the precheck on demand only, its trend and the transitions, the audited reports with
their volumes and the trends refusal; a pair climbing the ladder to ``evidenced`` under SSO; no 410 for a
purged cycle; a pair's brief: its newest complete cycle's stored figures, a sampled one's strata, its
marked points and its root-cause groups, opened, still open and closed; and what no answer, PDF or CSV
ever tells, keys among it."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth import readiness
from deltasleuth.auth.principal import expand
from deltasleuth.context import Context
from deltasleuth.core import readiness as gates
from deltasleuth.core.types import ChangeType, Direction, RootCause, Severity
from deltasleuth.reports import readiness as reports
from deltasleuth.reports.csv import parse_csv
from deltasleuth.reports.render import Document
from deltasleuth.runner import readiness as watching
from deltasleuth.store import repo
from tests.unit.reports.packs import listed_spec, samples
from tests.unit.runner.conftest import Harness
from tests.unit.runner.measuring import KEYS, MATCH, MISMATCH, PAIR, SOAK, ended, scanned, started, stored
from tests.unit.store.four_eyes import drawn, person, sampled, upload

from .conftest import OIDC, Callers, audited, said, session_as, signed

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
FAILING = MATCH | {"K001": MISMATCH, "K002": MISMATCH}  # 2 of 1,000 keys: over the 1 that 0.1% allows
ACCEPTED = MATCH | {
    "K007": ("KNOWN_DIFF", [("HIGH", "KD-ACCEPTED", "accepted")])
}  # suppressed, not different
PROGRAMMES = {
    "programmes": [
        {
            "name": "cutover-q4",
            "pairs": [{"spec": PAIR, "entity": "item"}, {"spec": "unit-none", "entity": "account"}],
        }
    ]
}
ITEM = {
    "pair", "programme", "entity", "version", "spec_id", "state", "regressed", "score", "gates", "blocking",
    "evidence", "trend", "since", "signoff", "pack", "closed", "deviations",
}  # fmt: skip
RECORDED = {
    "pair", "programme", "entity", "state", "regressed", "score", "gates", "evidence", "candidate",
    "signoff_id", "pack_id", "config_sha256", "since", "seq",
}  # fmt: skip
_ID = re.compile(r"\b[A-Z]{1,2}-[0-9A-Z]{26}\b")


def on(client: TestClient, work: Callable[..., Any], *args: Any) -> Any:
    """``work(ctx, *args)`` on the server's loop."""
    return client.portal.call(work, client.app.state.ctx, *args)


async def _watch(ctx: Context) -> None:
    """A worker's readiness pass, as its first: measure, record the settings, evaluate."""
    await watching.watch(ctx, watching.State())


async def _pairs(ctx: Context) -> dict[str, str]:
    """Two pairs with soaks over tables read whole: unit-runner, whose three cycles ended clean (a rule
    accepting one key's difference in the third), and unit-other, whose one cycle is over its target;
    measured and recorded by the worker."""
    h = Harness(ctx)
    good = await scanned(h)
    for results in (MATCH, MATCH, ACCEPTED):
        await ended(h, good, results)
    other = await scanned(h, await stored(h, SOAK.replace("unit-runner", "unit-other")))
    await ended(h, other, FAILING)
    await _watch(ctx)
    return {"good": good, "other": other}


async def _fail(ctx: Context, run_id: str) -> None:
    await ended(Harness(ctx), run_id, FAILING)
    await _watch(ctx)


@pytest.fixture
def client(open_client: OpenClient) -> Iterator[TestClient]:
    """Developer mode with a programme of two pairs, one of them a name no spec carries."""
    with open_client(readiness=PROGRAMMES) as found:
        yield found


def test_the_pairs_worst_first_with_their_roll_ups_filters_and_pages(client: TestClient) -> None:
    runs = on(client, _pairs)
    found = client.get("/v1/readiness").json()
    assert set(found) == {
        "definition", "now", "as_of", "config_sha256", "worker_config_sha256", "notes", "programmes",
        "entities", "items", "next_cursor", "volumes",
    }  # fmt: skip
    config = gates.config_sha256(client.app.state.ctx.settings.readiness)
    assert (found["definition"], found["config_sha256"], found["worker_config_sha256"]) == (
        gates.DEFINITION, config, config,
    )  # fmt: skip
    assert (found["notes"], found["as_of"], found["next_cursor"]) == ([], None, None)
    none, other, good = found["items"]
    assert [(i["pair"], i["state"], i["programme"], i["entity"]) for i in found["items"]] == [
        ("unit-none", "no_evidence", "cutover-q4", "account"),
        ("unit-other", "not_ready", None, None),
        (PAIR, "parity_ready", "cutover-q4", "item"),
    ]
    assert all(set(item) == ITEM for item in found["items"])
    assert (good["score"], good["blocking"], good["version"]) == ({"passed": 5, "required": 5}, [], "1.0.0")
    assert good["evidence"] | {"ended_at": None} == {
        "run_id": runs["good"], "cycle": 3, "ended_at": None, "basis": "census", "claim": "observed",
    }  # fmt: skip
    assert datetime.fromisoformat(good["since"]) <= datetime.fromisoformat(found["now"])
    assert (good["signoff"], good["pack"], good["closed"], good["deviations"]) == (None, None, None, [])
    assert [(p["cycle"], p["value"], p["allowed"], p["failed"], p["sampled"]) for p in good["trend"]] == [
        (1, 0, 1, [], False), (2, 0, 1, [], False), (3, 0, 1, [], False),
    ]  # fmt: skip
    assert [p["suppressed"] for p in good["trend"]] == [0, 0, 1]  # beside each discrepant count
    assert good["gates"][2]["text"].endswith(
        "(per-stratum targets are the approver's); 1 key(s) suppressed (known differences or noise only)"
    )
    assert [g["gate"] for g in good["gates"]] == list(gates.GATES) and set(good["gates"][0]) == {
        "gate", "group", "required", "status", "value", "allowed", "over", "run_id", "cycle", "text",
    }  # fmt: skip
    assert (other["blocking"], other["score"]) == (["targets", "convergence"], {"passed": 3, "required": 5})
    assert other["gates"][2]["text"] == (
        f"cycle 1 of run {runs['other']} has 2 discrepant or unresolved key(s) of 1,000 observed whole; its"
        " pooled 0.1% target allows 1: 1 over; 0 key(s) suppressed (known differences or noise only)"
    )
    assert {g["text"] for g in none["gates"]} == {"no spec named unit-none is stored"}
    assert (none["score"], none["evidence"], none["since"] is not None) == (
        {"passed": 0, "required": 5}, None, True,
    )  # fmt: skip
    assert found["programmes"] == [
        {
            "programme": "cutover-q4", "pairs": 2, "ready": 1, "worst": "no_evidence",
            "entities": [
                {"entity": "account", "states": {"no_evidence": 1}},
                {"entity": "item", "states": {"parity_ready": 1}},
            ],
        },
        {
            "programme": None, "pairs": 1, "ready": 0, "worst": "not_ready",
            "entities": [{"entity": None, "states": {"not_ready": 1}}],
        },
    ]  # fmt: skip
    assert found["entities"] == [
        {"entity": "account", "states": {"no_evidence": 1}},
        {"entity": "item", "states": {"parity_ready": 1}},
        {"entity": None, "states": {"not_ready": 1}},
    ]

    def pairs(**params: Any) -> list[str]:
        return [item["pair"] for item in client.get("/v1/readiness", params=params).json()["items"]]

    assert pairs(programme="cutover-q4") == ["unit-none", PAIR] and pairs(entity="item") == [PAIR]
    assert pairs(state="not_ready") == ["unit-other"] and pairs(q="OTHER") == ["unit-other"]
    assert pairs(gate="targets") == ["unit-none", "unit-other"]  # unknown or not met
    assert pairs(gate="convergence", programme="cutover-q4") == ["unit-none"]
    assert pairs(closed="closed") == [] and len(pairs(closed="all")) == len(pairs(closed="open")) == 3
    narrowed = client.get("/v1/readiness", params={"state": "parity_ready"}).json()
    assert narrowed["programmes"] == found["programmes"]  # the roll-ups are of the selection, not the state
    first = client.get("/v1/readiness", params={"limit": 2}).json()
    rest = client.get("/v1/readiness", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [i["pair"] for i in first["items"]] == ["unit-none", "unit-other"]
    assert (first["next_cursor"], [i["pair"] for i in rest["items"]], rest["next_cursor"]) == (
        "1.unit-other", [PAIR], None,
    )  # fmt: skip


def test_each_refusal_says_what_the_contract_says(client: TestClient) -> None:
    on(client, _pairs)
    states, named = ", ".join(gates.STATES), ", ".join(gates.GATES)
    missing = "pair customer-x not found: no spec or readiness setting names it"
    assert [
        said(client.get(path))
        for path in (
            "/v1/readiness/pairs/customer-x",
            "/v1/readiness/pairs/customer-x/cycles",
            "/v1/readiness/transitions?pair=customer-x",
            "/v1/readiness/reports/trends.csv?pair=customer-x",
            "/v1/readiness?programme=cutover-q9",
            "/v1/readiness/transitions?programme=cutover-q9",
            "/v1/readiness/reports/dashboard.csv?programme=cutover-q9",
            "/v1/readiness?state=ready",
            "/v1/readiness?gate=parity",
            f"/v1/readiness/pairs/{PAIR}/cycles?bucket=month",
            "/v1/readiness/reports/trends.csv?bucket=hour",
            "/v1/readiness/reports/dashboard.csv?bucket=bogus",
            "/v1/readiness/reports/dashboard.pdf?bucket=bogus",
            "/v1/readiness?closed=yes",
            "/v1/readiness?limit=0",
            "/v1/readiness?limit=501",
            "/v1/readiness/transitions?limit=many",
            "/v1/readiness?as_of=yesterday",
            "/v1/readiness?as_of=2026-10-03T04:00:00",
            "/v1/readiness/reports/dashboard.pdf?volume=2",
            "/v1/readiness/reports/dashboard.pdf?volume=first",
            "/v1/readiness/reports/dashboard.csv?volume=2",
            "/v1/readiness/reports/trends.csv?volume=0",
            "/v1/readiness?cursor=x",
            f"/v1/readiness/pairs/{PAIR}/cycles?cursor=1.2",
            "/v1/readiness/transitions?cursor=x.y",
            "/v1/readiness/reports/x.pdf",
        )
    ] == [
        *[(404, missing)] * 4,
        *[(404, "programme cutover-q9 is not configured")] * 3,
        (422, f"state must be one of {states}"),
        (422, f"gate must be one of {named}"),
        *[(422, "bucket must be cycle, day or week")] * 4,
        (422, "closed must be open, closed or all"),
        *[(422, "limit must be 1 to 500")] * 3,
        *[(422, "as_of must be an ISO-8601 time with a UTC offset")] * 2,
        *[(422, "volume must be 1 to 1 for this selection (100 pairs a volume)")] * 4,
        *[(400, "malformed cursor")] * 3,
        (404, "report 'x.pdf' not found"),
    ]
    assert len(gates.STATES) == 7 and len(gates.GATES) == 12
    assert audited(client, "report.download") == []  # a refusal is no download
    for path in ("dashboard.csv?bucket=day&volume=1", "trends.csv?bucket=week&volume=1"):
        assert client.get(f"/v1/readiness/reports/{path}").status_code == 200


def test_as_of_answers_what_was_recorded_then(client: TestClient) -> None:
    runs = on(client, _pairs)
    then = client.get("/v1/readiness").json()["now"]
    on(client, _fail, runs["good"])
    now = client.get("/v1/readiness").json()
    assert [(i["pair"], i["state"]) for i in now["items"]][-1] == (PAIR, "not_ready")
    was = client.get("/v1/readiness", params={"as_of": then}).json()
    assert [(i["pair"], i["state"]) for i in was["items"]] == [
        ("unit-none", "no_evidence"), ("unit-other", "not_ready"), (PAIR, "parity_ready"),
    ]  # fmt: skip
    assert all(set(item) == RECORDED for item in was["items"]) and was["as_of"] == then
    good = was["items"][-1]
    assert (good["evidence"], good["score"], good["gates"]["targets"]) == (
        {"run_id": runs["good"], "cycle": 3}, {"passed": 5, "required": 5}, "met",
    )  # fmt: skip
    assert good["since"] <= then and good["seq"] > 0 and good["config_sha256"] == was["config_sha256"]
    assert was["programmes"][0] | {"entities": None} == {
        "programme": "cutover-q4", "pairs": 2, "ready": 1, "worst": "no_evidence", "entities": None,
    }  # fmt: skip
    by_state = client.get("/v1/readiness", params={"as_of": then, "state": "parity_ready"}).json()
    by_gate = client.get("/v1/readiness", params={"as_of": then, "gate": "targets"}).json()
    assert [i["pair"] for i in by_state["items"]] == [PAIR]
    assert [i["pair"] for i in by_gate["items"]] == ["unit-none", "unit-other"]
    before = client.get("/v1/readiness", params={"as_of": "2026-01-01T00:00:00+00:00"}).json()
    assert before["items"] == [] and before["programmes"] == []


def test_a_pair_its_trend_and_its_transitions(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = on(client, _pairs)
    on(client, _fail, runs["good"])
    pair = client.get(f"/v1/readiness/pairs/{PAIR}").json()
    assert set(pair) == ITEM | {
        "definition", "now", "config_sha256", "worker_config_sha256", "candidate", "convergence", "notes",
        "points", "versions", "runs", "strata", "labelling",
    }  # fmt: skip
    assert "precheck" not in pair  # on demand only
    assert (pair["state"], pair["candidate"], pair["convergence"]) == (
        "not_ready", {"run_id": runs["good"], "cycle": 4}, {"streak": 0, "needed": 3, "window": 60},
    )  # fmt: skip
    assert (pair["points"], pair["strata"], pair["labelling"], pair["notes"]) == ([], None, None, [])
    [version] = pair["versions"]
    assert (version["version"], version["run_id"], version["cycle"]) == ("1.0.0", runs["good"], 4)
    assert [(r["run_id"], r["cycle"], r["status"]) for r in pair["runs"]] == [(runs["good"], 5, "running")]
    by = {gate["gate"]: gate for gate in pair["gates"]}
    assert by["convergence"]["text"].startswith(
        f"0 of the 3 evidence cycles in a row that 1.0.0 (spec {pair['spec_id']}) needs meet every per-cycle"
        f" gate (cycle 4 of run {runs['good']} failed targets); cycle 7 of run {runs['good']} is the earliest"
        " that can complete them: it is scheduled to start about "
    ) and by["convergence"]["text"].endswith(
        "cycles starting every 1h, and completes them when it ends complete (a schedule, not a forecast)"
    )
    checked = client.get(f"/v1/readiness/pairs/{PAIR}", params={"precheck": "1"}).json()
    assert checked["precheck"] is None  # developer mode signs nothing off
    assert "precheck" not in client.get(f"/v1/readiness/pairs/{PAIR}", params={"precheck": "0"}).json()
    # The trend: every point, oldest first; by day, the day's last evidence point; pages going back.
    trend = client.get(f"/v1/readiness/pairs/{PAIR}/cycles").json()
    assert (trend["pair"], trend["bucket"], trend["next_cursor"]) == (PAIR, "cycle", None)
    assert [(p["cycle"], p["different"], p["total"], p["fails"], p["boundary"]) for p in trend["items"]] == [
        (1, 0, 1000, [], False),
        (2, 0, 1000, [], False),
        (3, 0, 1000, [], False),
        (4, 2, 1000, ["targets"], False),
    ]
    for bucket in ("day", "week"):
        daily = client.get(f"/v1/readiness/pairs/{PAIR}/cycles", params={"bucket": bucket}).json()
        assert [(p["cycle"], p["evidence"]) for p in daily["items"]] == [(4, True)]
    monkeypatch.setattr(readiness, "PAGE", 2)
    newest = client.get(f"/v1/readiness/pairs/{PAIR}/cycles").json()
    older = client.get(f"/v1/readiness/pairs/{PAIR}/cycles", params={"cursor": newest["next_cursor"]}).json()
    assert [p["cycle"] for p in newest["items"]] == [3, 4] and [p["cycle"] for p in older["items"]] == [1, 2]
    assert older["next_cursor"] is None and newest["next_cursor"].endswith(f".{runs['good']}.3")
    # The transitions: the worker's rows, newest first.
    rows = client.get("/v1/readiness/transitions").json()
    assert rows["next_cursor"] is None and all(set(row) == {"seq", "at", "detail"} for row in rows["items"])
    assert [(r["detail"]["pair"], r["detail"]["from"], r["detail"]["to"]) for r in rows["items"]] == [
        (PAIR, "parity_ready", "not_ready"),
        (PAIR, None, "parity_ready"),
        ("unit-other", None, "not_ready"),
        ("unit-none", None, "no_evidence"),
    ]
    assert [r["seq"] for r in rows["items"]] == sorted((r["seq"] for r in rows["items"]), reverse=True)
    mine = client.get("/v1/readiness/transitions", params={"pair": "unit-other"}).json()
    programme = client.get("/v1/readiness/transitions", params={"programme": "cutover-q4", "limit": 2}).json()
    rest = client.get(
        "/v1/readiness/transitions", params={"programme": "cutover-q4", "cursor": programme["next_cursor"]}
    ).json()
    assert [r["detail"]["pair"] for r in mine["items"]] == ["unit-other"]
    assert [r["detail"]["pair"] for r in programme["items"]] == [PAIR, PAIR]
    assert [r["detail"]["pair"] for r in rest["items"]] == ["unit-none"] and rest["next_cursor"] is None


def test_a_viewer_reads_everything_and_only_a_download_is_audited(
    open_client: OpenClient, callers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_client(auth={}, readiness=PROGRAMMES) as client:  # tokens mode
        runs = on(client, _pairs)
        viewer = callers(client).token("viewer")
        config = gates.config_sha256(client.app.state.ctx.settings.readiness)
        paths = [
            "/v1/readiness",
            f"/v1/readiness/pairs/{PAIR}?precheck=1",
            f"/v1/readiness/pairs/{PAIR}/cycles",
            "/v1/readiness/transitions",
        ]
        assert [client.get(path).status_code for path in paths] == [401] * 4
        assert [client.get(path, headers=viewer).status_code for path in paths] == [200] * 4
        assert audited(client, "report.download") == [] and audited(client, "audit.read") == []
        pdf = client.get("/v1/readiness/reports/dashboard.pdf", headers=viewer)
        table = client.get("/v1/readiness/reports/dashboard.csv?programme=cutover-q4", headers=viewer)
        trends = client.get("/v1/readiness/reports/trends.csv", headers=viewer)
        daily = client.get(f"/v1/readiness/reports/trends.csv?pair={PAIR}&bucket=day", headers=viewer)
        assert client.get("/v1/readiness/reports/dashboard.pdf").status_code == 401
        actor = client.get("/v1/auth/me", headers=viewer).json()["actor"]
        assert audited(client, "report.download") == [
            (actor, "readiness", {"name": name, "programme": programme, "config_sha256": config})
            for name, programme in (
                ("dashboard.pdf", None),
                ("dashboard.csv", "cutover-q4"),
                ("trends.csv", None),
                ("trends.csv", None),
            )
        ]
        assert (pdf.status_code, pdf.headers["content-type"], pdf.content[:5]) == (
            200,
            "application/pdf",
            b"%PDF-",
        )
        assert pdf.headers["content-disposition"] == 'attachment; filename="readiness-dashboard.pdf"'
        assert table.headers["content-type"].startswith("text/csv")
        header, rows = parse_csv(table.content)
        assert tuple(header) == reports.DASHBOARD and len(rows) == 2 * 12  # a row per pair and gate
        assert {row["pair"] for row in rows} == {PAIR, "unit-none"} and rows[0]["pair"] == "unit-none"
        assert (rows[0]["version"], rows[0]["ended_at"], rows[0]["basis"], rows[0]["run_id"]) == (
            "",
            "",
            "",
            "",
        )
        # The suppressed count beside the one discrepant count a row holds, the targets gate's value.
        assert [(row["gate"], row["value"], row["suppressed"]) for row in rows if row["suppressed"]] == [
            ("targets", "0", "1")
        ]
        last = rows[-1]
        assert (
            last["gate"],
            last["status"],
            last["state"],
            last["gates_passed"],
            last["gates_required"],
        ) == (
            "pack",
            "unavailable",
            "parity_ready",
            "5",
            "5",
        )
        assert last["text"] == "evidence packs are not configured (packs)" and last["config_sha256"] == config
        header, points = parse_csv(trends.content)
        assert tuple(header) == reports.TRENDS
        assert [(p["pair"], p["cycle"], p["different"], p["fails"]) for p in points] == [
            ("unit-other", "1", "2", "targets"),
            (PAIR, "1", "0", ""),
            (PAIR, "2", "0", ""),
            (PAIR, "3", "0", ""),
        ]
        assert (points[1]["programme"], points[1]["entity"], points[1]["run_id"], points[1]["basis"]) == (
            "cutover-q4", "item", runs["good"], "census",
        )  # fmt: skip
        assert [p["cycle"] for p in parse_csv(daily.content)[1]] == ["3"]
        # Over its rows trends.csv is refused, counted first; a larger selection is split into volumes.
        monkeypatch.setattr(reports, "TRENDS_ROWS", 3)
        refused = client.get("/v1/readiness/reports/trends.csv", headers=viewer)
        assert said(refused) == (
            422,
            "trends.csv would hold 4 rows, over its 3: narrow it with programme=, entity=, pair= or"
            " bucket=day",
        )
        narrowed = client.get("/v1/readiness/reports/trends.csv?entity=item", headers=viewer)
        assert narrowed.status_code == 200 and len(parse_csv(narrowed.content)[1]) == 3
        assert client.get("/v1/readiness", headers=viewer).json()["volumes"] == 1
        monkeypatch.setattr(reports, "VOLUME", 2)
        listed = [client.get(f"/v1/readiness{q}", headers=viewer).json() for q in ("", "?entity=item")]
        assert [found["volumes"] for found in listed] == [2, 1]  # the screen offers a download of each
        second = client.get("/v1/readiness/reports/dashboard.pdf?volume=2", headers=viewer)
        beyond = client.get("/v1/readiness/reports/dashboard.pdf?volume=3", headers=viewer)
        assert second.headers["content-disposition"] == 'attachment; filename="readiness-dashboard-2.pdf"'
        assert said(beyond) == (422, "volume must be 1 to 2 for this selection (2 pairs a volume)")
        assert audited(client, "report.download")[-1][2]["name"] == "dashboard-2.pdf"
        assert len(audited(client, "report.download")) == 6  # a refusal is no download


def approve(client: TestClient, approval_id: str, headers: dict[str, str]) -> Any:
    shown = client.get(f"/v1/approvals/{approval_id}", headers=headers).json()
    body = {"decision": "approve", "attests_sha256": shown["attests_sha256"]}
    return client.post(f"/v1/approvals/{approval_id}/decision", json=body, headers=headers)


def test_a_pair_climbs_the_ladder_to_evidenced_under_sso(open_client: OpenClient, pack_key: Path) -> None:
    packs = {"installation": "test-1", "signing_key_ref": f"file:{pack_key}"}
    relaxed = {"gates": {"convergence": {"cycles": 1}}}  # one clean cycle converges, said everywhere
    with open_client(auth={"oidc": OIDC}, packs=packs, readiness=relaxed) as client:
        cycle = signed(client, sign=False)
        state = watching.State()

        async def watch(ctx: Context) -> None:
            state.evaluated_at -= watching.READINESS_MIN_S
            await watching.watch(ctx, state)

        def pair() -> dict[str, Any]:
            on(client, watch)
            return client.get(
                "/v1/readiness/pairs/unit-reports", params={"precheck": "1"}, headers=vic
            ).json()

        vic, olga = session_as(client, "vic", "viewer"), session_as(client, "olga", "operator")
        erin = Callers(client).session(
            *expand(["rule_approver"]), username="erin", auth_time=datetime.now(UTC)
        )
        at = f"cycle 1 of run {cycle.run_id}"
        ready = pair()
        by = {gate["gate"]: gate for gate in ready["gates"]}
        assert (ready["state"], ready["score"], ready["blocking"]) == (
            "signoff_ready", {"passed": 6, "required": 8}, ["signoff", "pack"],
        )  # fmt: skip
        assert ready["deviations"] == ["convergence 1 cycle (default 3; relaxed)"]
        assert (
            by["spec_approval"]["text"] == f"{at} ran under spec approval {cycle.spec_approval_id}, approved"
        )
        assert by["adjudication"]["text"] == f"{at} has 6 per-key results, under the 50 an adjudication needs"
        assert by["signoff"]["text"] == (
            f"request sign-off of {at} on the Run screen, the newest cycle in the streak whose adjudication"
            " passed (or the newest evidence cycle): every other gate is met"
        )
        assert by["pack"]["text"] == "no sign-off to pack yet"
        assert ready["precheck"] == {
            "label": "computed now; the sign-off decides",
            "run_id": cycle.run_id,
            "cycle": 1,
            "purged": False,
            "problems": [],
        }
        asked = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/signoff", headers=olga).json()["approval_id"]
        waiting = pair()
        assert waiting["state"] == "signoff_ready" and waiting["gates"][10]["text"] == (
            f"sign-off {asked} requested for cycle 1 awaits a rule_approver"
        )
        assert approve(client, asked, erin).json()["status"] == "approved"
        signed_off = pair()
        assert (signed_off["state"], signed_off["signoff"], signed_off["precheck"]) == (
            "signed_off",
            asked,
            None,
        )
        assert signed_off["gates"][11]["text"] == (
            f"sign-off {asked} has no issued evidence pack: issue one on the Run screen"
        )
        issued = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga).json()["pack_id"]
        evidenced = pair()
        assert (evidenced["state"], evidenced["pack"], evidenced["score"], evidenced["blocking"]) == (
            "evidenced", issued, {"passed": 8, "required": 8}, [],
        )  # fmt: skip
        rows = client.get("/v1/readiness/transitions", params={"pair": "unit-reports"}, headers=vic).json()
        assert [(r["detail"]["from"], r["detail"]["to"], r["detail"]["causes"]) for r in rows["items"]] == [
            ("signed_off", "evidenced", ["pack"]),
            ("signoff_ready", "signed_off", ["signoff"]),
            (None, "signoff_ready", ["baseline"]),
        ]
        listed = client.get("/v1/readiness", headers=vic).json()["items"]
        assert [(i["pair"], i["state"], i["signoff"], i["pack"]) for i in listed] == [
            ("unit-reports", "evidenced", asked, issued)
        ]
        assert audited(client, "report.download") == []  # reading readiness is not audited


async def _purging(ctx: Context, run_id: str, cycle: int = 1) -> None:
    async with ctx.pool.connection() as conn:
        assert await repo.start_purge(conn, run_id, cycle, "expired") is not None


@pytest.mark.parametrize("configured", [True, False])
def test_the_precheck_lists_what_refuses_every_pack_of_the_spec(
    open_client: OpenClient, pack_key: Path, configured: bool
) -> None:
    """Identifier keys that no key.pattern tells get no pack: the precheck said
    nothing refused the cycle's sign-off, and the pack was refused once it was signed off. It lists the
    pack's own line where packs are configured; the sign-off itself is not refused for it."""
    packs = {"installation": "test-1", "signing_key_ref": f"file:{pack_key}"} if configured else None
    key = listed_spec(pii=True).raw["key"] | {"pattern": "^[0-9]*$"}
    with open_client(
        auth={"oidc": OIDC}, packs=packs, readiness={"gates": {"convergence": {"cycles": 1}}}
    ) as client:
        cycle = signed(client, sign=False, spec=listed_spec(pii=True, key=key), pairs=samples(pii=True))
        on(client, _watch)
        vic, olga = session_as(client, "vic", "viewer"), session_as(client, "olga", "operator")
        pair = client.get("/v1/readiness/pairs/unit-reports?precheck=1", headers=vic).json()
        assert (
            pair["precheck"]["problems"]
            == [
                f"spec {cycle.spec_id}'s key.pattern matches the empty string: a raw key cannot be told from"
                " other text, so no pack"
            ]
            * configured
        )
        asked = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/signoff", headers=olga)
        assert asked.status_code == 201


def test_no_answer_is_410_for_a_purged_cycle(open_client: OpenClient) -> None:
    relaxed = {"gates": {"convergence": {"cycles": 1}}}
    with open_client(auth={"oidc": OIDC}, readiness=relaxed) as client:
        cycle = signed(client, sign=False)
        on(client, _watch)
        on(client, _purging, cycle.run_id)
        vic = session_as(client, "vic", "viewer")
        assert client.get(f"/v1/runs/{cycle.run_id}/results", headers=vic).status_code == 410
        answers = [
            client.get(path, headers=vic)
            for path in (
                "/v1/readiness",
                "/v1/readiness/pairs/unit-reports?precheck=1",
                "/v1/readiness/pairs/unit-reports/cycles",
                "/v1/readiness/transitions",
                "/v1/readiness/reports/dashboard.pdf",
                "/v1/readiness/reports/dashboard.csv",
                "/v1/readiness/reports/trends.csv",
            )
        ]
        assert [answer.status_code for answer in answers] == [200] * 7
        pair = answers[1].json()
        assert pair["state"] == "signoff_ready"  # its measures outlive its evidence
        assert pair["precheck"] == {
            "label": "computed now; the sign-off decides", "run_id": cycle.run_id, "cycle": 1, "purged": True,
            "problems": [],
        }  # fmt: skip


BANNED = re.compile(
    r"%|\b(probab\w*|percent\w*|likel\w*|forecast\w*|predict\w*|ETA|estimat\w*|weighted\w*|average\w*|mean"
    r"|certif\w*|guarante\w*)\b",
    re.IGNORECASE,
)
SCHEDULE = "(a schedule, not a forecast)"


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield str(name)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _printed(doc: Document) -> list[str]:
    """Every string of a document, as its PDF prints them."""
    cells = [cell for s in doc.sections for row in s.rows for cell in row]
    return [
        doc.title,
        doc.meta,
        *(s.title for s in doc.sections),
        *(line for s in doc.sections for line in s.lines),
        *(str(cell) for cell in cells),
        *(h for s in doc.sections for h in s.header),
    ]


async def _dashboard(ctx: Context) -> list[str]:
    return _printed(await reports.dashboard_document(ctx))


async def _paged(ctx: Context, pair: str) -> Document:
    """The pair's brief as its PDF prints it."""
    return reports.brief_document(await readiness.brief(ctx, pair), "I-" + "0" * 26, "open")


# -- a pair's brief -----------------------------------------------------------------------------

POINT = {
    "run_id", "cycle", "ended_at", "version", "spec_id", "basis", "complete", "evidence", "total", "compared",
    "different", "indeterminate", "absent_both", "api_uncomparable", "suppressed", "unresolved",
    "unexplained_critical", "new", "persistent", "fixed", "other", "sample_status", "graph_rows", "fails",
    "skipped", "boundary", "mismatch_rate", "coverage", "by_outcome", "cancelled", "outstanding",
    "root_causes", "marks",
}  # fmt: skip
BRIEF = ITEM | {"definition", "brief", "now", "config_sha256", "notes", "legend", "points", "cycle", "causes"}
GROUP = {
    "root_cause_id", "state", "first", "last", "seen", "max_severity", "taxonomy_code", "change_type",
    "affected_keys", "since",
}  # fmt: skip
COMPARED = ("MATCH", "KNOWN_DIFF", "NOISE_ONLY", "MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B")
FIVE = dict.fromkeys(KEYS[:5], "MATCH")  # the keys of a run given five of them
LACKING = dict.fromkeys(KEYS[:-1], "MATCH")  # a key never read: the cycle is incomplete
M5 = "These counts are of the keys compared, not of a population."
M9 = (
    "Mismatch rate {}: {} different of {} compared. Known differences and noise-only keys count as matching;"
    " a known difference under a bug rule counts as different."
)


def _brief(client: TestClient, pair: str = PAIR) -> dict[str, Any]:
    return client.get(f"/v1/readiness/pairs/{pair}/brief").json()


async def _grouped(ctx: Context, run_id: str, cycle: int, groups: Mapping[str, int]) -> None:
    """The ended cycle's root-cause groups in rank order, ``RC-<its letter, twelve times>`` each, with how
    many keys it affects: its path and its sample keys are in the store, and in no answer."""
    causes = [
        RootCause(
            "RC-" + letter * 12, "signature", "$.status", ChangeType.VALUE_CHANGED, Direction.BOTH,
            affected_keys=keys, finding_count=keys, max_severity=Severity.HIGH,
            taxonomy_code="VALUE_MISMATCH", sample_keys=tuple(KEYS[:keys]),
        )
        for letter, keys in groups.items()
    ]  # fmt: skip
    async with ctx.pool.connection() as conn:
        await repo.replace_root_causes(conn, run_id, cycle, causes, {})


async def _aged(ctx: Context, at: datetime) -> str:
    """unit-runner's soak over tables read whole: two cycles ended clean at ``at`` and an hour later (a rule
    accepting one key's difference in the second) and a third now; measured and recorded by the worker."""
    h = Harness(ctx)
    run_id = await scanned(h)
    await ended(h, run_id, MATCH, at=at)
    await ended(h, run_id, ACCEPTED, at=at + timedelta(hours=1))
    await ended(h, run_id, MATCH)
    await _watch(ctx)
    return run_id


async def _given(ctx: Context, results: Mapping[str, Any], spec_id: int | None = None) -> str:
    """A batch run of unit-runner over five keys given with it (``ds run --keys``), ended."""
    h = Harness(ctx)
    run_id = await started(h, spec_id, keys=list(results), mode="batch", keys_source="explicit")
    await ended(h, run_id, results)
    return run_id


def test_brief(open_client: OpenClient) -> None:
    """A pair's brief restates its newest complete cycle's stored figures, whose six comparable outcomes
    add up to the keys compared; after a run of five keys given with it the figures are that run's, and a
    note says which cycle the gates judge; a closed pair's window ends with its day."""
    day = datetime.now(UTC).date() - timedelta(days=3)
    with open_client(readiness=PROGRAMMES) as client:
        soak = on(client, _aged, datetime.combine(day, time(1), UTC))
        listed, brief = client.get("/v1/readiness").json()["items"][-1], _brief(client)
        assert set(brief) == BRIEF and all(set(point) == POINT for point in brief["points"])
        assert (brief["definition"], brief["brief"], brief["legend"]) == (
            gates.DEFINITION, gates.BRIEF, gates.LEGEND,
        )  # fmt: skip
        told = ITEM - {"gates"}  # the pair as listed, its trend unchanged (the fresh gate tells its age)
        assert {name: brief[name] for name in told} == {name: listed[name] for name in told}
        assert [gate["status"] for gate in brief["gates"]] == [gate["status"] for gate in listed["gates"]]
        assert (brief["state"], brief["notes"]) == ("parity_ready", [])
        points = brief["points"]
        assert [(p["run_id"], p["cycle"], p["marks"], p["root_causes"]) for p in points] == [
            (soak, 1, [], 0), (soak, 2, [], 0), (soak, 3, [], 0),
        ]  # fmt: skip
        for point in points:  # the stored counters, not a new count
            assert sum(point["by_outcome"][outcome] for outcome in COMPARED) == point["compared"] == 1_000
        assert points[1]["by_outcome"] == {
            "MATCH": 999, "MISMATCH": 0, "KNOWN_DIFF": 1, "NOISE_ONLY": 0, "DRIFT": 0, "NOT_FOUND_A": 0,
            "NOT_FOUND_B": 0, "INCONCLUSIVE": 0,
        }  # fmt: skip
        assert [(p["mismatch_rate"], p["coverage"], p["cancelled"], p["outstanding"]) for p in points] == [
            ("0", "1", 0, 0)
        ] * 3
        cycle = brief["cycle"]
        assert set(cycle) == POINT | {"draw_id", "claim", "strata", "notes"}
        assert {name: cycle[name] for name in POINT} == points[-1]
        assert (cycle["draw_id"], cycle["claim"], cycle["strata"], cycle["notes"]) == (
            None, "observed", None, [M9.format("0", 0, "1,000")],
        )  # fmt: skip
        assert brief["causes"] == {
            "opened": 0, "still_open": 0, "closed": 0, "total": 0, "cycles": 3, "from": points[0]["ended_at"],
            "text": (
                f"Root-cause groups over 3 complete cycle(s) since {day}: 0 opened in the newest, 0 still"
                " open, 0 closed (not observed in it)"
            ),
            "items": [],
        }  # fmt: skip
        # Five keys given with a run are no evidence: the gates still judge the soak's cycle.
        given_ = on(client, _given, FIVE | {"K001": MISMATCH})
        on(client, _watch)
        after = _brief(client)
        assert (after["state"], after["evidence"]["run_id"], after["evidence"]["cycle"]) == (
            "parity_ready", soak, 3,
        )  # fmt: skip
        assert [(p["run_id"], p["cycle"], p["marks"]) for p in after["points"]][2:] == [
            (soak, 3, []), (given_, 1, ["keys"]),
        ]  # fmt: skip
        cycle = after["cycle"]
        assert (cycle["run_id"], cycle["basis"], cycle["evidence"], cycle["mismatch_rate"]) == (
            given_, "keys", False, "0.2",
        )  # fmt: skip
        assert sum(cycle["by_outcome"][outcome] for outcome in COMPARED) == cycle["compared"] == 5
        assert cycle["notes"] == [
            f"The gates judge cycle 3 of run {soak}; these figures are of cycle 1 of run {given_}, the newest"
            " complete cycle.",
            M5,
            M9.format("0.2", 1, 5),
        ]
    closed = {"programmes": [{"name": "cutover-q4", "pairs": [{"spec": PAIR, "closed": day.isoformat()}]}]}
    with open_client(readiness=closed) as client:
        on(client, _watch)  # the worker, started again under these settings
        brief = _brief(client)
        assert (brief["closed"], brief["notes"]) == (
            day.isoformat(), [f"closed on {day}: judged as of that day"],
        )  # fmt: skip
        assert [(p["run_id"], p["cycle"]) for p in brief["points"]] == [(soak, 1), (soak, 2)]
        assert (brief["cycle"]["cycle"], brief["cycle"]["notes"], brief["causes"]["cycles"]) == (
            2, [M9.format("0", 0, "1,000")], 2,
        )  # fmt: skip


async def _drawn(ctx: Context, judged: bool = True) -> tuple[str, str]:
    """A batch run of customer-profile's sample, six keys in two strata compared whole, ended with its
    judgement (or without one) and measured: the run's and the draw's ids."""
    version = sampled()
    async with ctx.pool.connection() as conn:
        amy = await person(conn, "amy", "spec_author")
        found = await drawn(conn, await upload(conn, version, amy), version, amy, judged=judged)
    await _watch(ctx)
    return found


def test_brief_sampled_has_no_rate(client: TestClient) -> None:
    """A sampled cycle's brief gives no rate and no coverage: its strata as ``ds run`` words them, each
    with its own target and bound, and no figure for the strata pooled; one that ended without a judgement
    says so, and has no strata."""
    run_id, draw_id = on(client, _drawn)
    brief = _brief(client, "customer-profile")
    cycle = brief["cycle"]
    assert (cycle["run_id"], cycle["draw_id"], cycle["sample_status"], cycle["basis"]) == (
        run_id, draw_id, "passed", "sample",
    )  # fmt: skip
    assert (cycle["mismatch_rate"], cycle["coverage"], cycle["total"]) == (None, None, 6)
    assert cycle["by_outcome"]["MATCH"] == 6  # the sample's own counts
    assert cycle["notes"] == [
        f"Sampled (draw {draw_id}): judged per stratum, with no rate or bound for the strata pooled."
    ]
    assert cycle["strata"] == [
        f"stratum segment={label} (standard, ≤ 0.1%): 3 drawn of 3, 3 compared, 0 discrepant, 0 unresolved,"
        " compared whole: passed"
        for label in ("retail", "other")
    ]
    texts = list(_strings(brief))
    # sampling's lines and the gates' texts
    allowed = {*cycle["strata"], *(gate["text"] for gate in brief["gates"])}
    assert {text for text in texts if "%" in text} <= allowed
    assert not any("weighted" in text or "ub95" in text for text in texts)  # nothing pooled
    other, unjudged = on(client, _drawn, False)
    brief = _brief(client, "customer-profile")
    cycle = brief["cycle"]
    assert [(p["run_id"], p["marks"]) for p in brief["points"]] == [(run_id, []), (other, [])]
    assert (cycle["run_id"], cycle["sample_status"], cycle["strata"]) == (other, "unjudged", None)
    assert (cycle["mismatch_rate"], cycle["coverage"]) == (None, None)
    assert cycle["notes"] == [f"the sample {unjudged} of cycle 1 of run {other} ended without a judgement"]


async def _burned(ctx: Context) -> dict[str, str]:
    """unit-runner's cycles, oldest first, each with its root-cause groups: a soak over tables whose first
    cycle is complete and whose second lacks a key; a soak of a new version; and two batch runs of five
    keys given with them. Measured by the worker."""
    h = Harness(ctx)
    runs = {"first": await scanned(h)}
    await ended(h, runs["first"], MATCH)
    await ended(h, runs["first"], LACKING)
    newer = await stored(h, SOAK.replace('version: "1.0.0"', 'version: "1.1.0"'))
    runs["newer"] = await scanned(h, newer)
    await ended(h, runs["newer"], MATCH)
    runs["given"], runs["newest"] = await _given(ctx, FIVE, newer), await _given(ctx, FIVE, newer)
    groups = {
        ("first", 1): {"a": 3, "b": 2, "c": 1},
        ("first", 2): {"a": 1, "d": 1},  # incomplete: it neither opens nor closes
        ("newer", 1): {"a": 2, "b": 5},
        ("given", 1): {"f": 1, "a": 1},
        ("newest", 1): {"e": 2, "a": 4},
    }
    for (name, cycle), found in groups.items():
        await _grouped(ctx, runs[name], cycle, found)
    await _watch(ctx)
    return runs


def test_brief_burn_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root-cause groups over the window's complete cycles: opened in the newest, still open, or closed
    (not observed in it, which is not fixed), each closed one with the marks since it was last seen; the
    newest cycle's first, in its rank order, then the others by when they were last seen."""
    runs = on(client, _burned)
    brief = _brief(client)
    points, causes = brief["points"], brief["causes"]
    assert [(p["run_id"], p["cycle"], p["complete"], p["marks"], p["root_causes"]) for p in points] == [
        (runs["first"], 1, True, [], 3),
        (runs["first"], 2, False, [], 2),
        (runs["newer"], 1, True, ["version"], 2),
        (runs["given"], 1, True, ["keys"], 2),
        (runs["newest"], 1, True, [], 2),
    ]
    first, _, newer, given_, newest = (
        {name: point[name] for name in ("run_id", "cycle", "ended_at")} for point in points
    )
    assert {name: causes[name] for name in ("opened", "still_open", "closed", "total", "cycles", "from")} == {
        "opened": 1, "still_open": 1, "closed": 3, "total": 5, "cycles": 4, "from": points[0]["ended_at"],
    }  # fmt: skip
    text = (
        f"Root-cause groups over 4 complete cycle(s) since {points[0]['ended_at'][:10]}: 1 opened in the"
        " newest, 1 still open, 3 closed (not observed in it)"
    )
    assert causes["text"] == text and all(set(item) == GROUP for item in causes["items"])
    names = ("root_cause_id", "state", "first", "last", "seen", "affected_keys", "since")
    assert [tuple(item[name] for name in names) for item in causes["items"]] == [
        ("RC-" + "e" * 12, "opened", newest, newest, 1, 2, []),
        ("RC-" + "a" * 12, "still_open", first, newest, 4, 4, []),  # its last row's keys
        ("RC-" + "f" * 12, "closed", given_, given_, 1, 1, []),
        ("RC-" + "b" * 12, "closed", first, newer, 2, 5, ["keys"]),
        ("RC-" + "c" * 12, "closed", first, first, 1, 1, ["version", "keys"]),
    ]
    assert {(i["max_severity"], i["taxonomy_code"], i["change_type"]) for i in causes["items"]} == {
        ("HIGH", "VALUE_MISMATCH", "value_changed")
    }
    assert [gates.reads(item) for item in causes["items"]] == [
        "opened", "still open", "closed", "closed, keys changed since",
        "closed, version and keys changed since",
    ]  # fmt: skip
    assert gates.reads({"state": "closed", "since": ["version"]}) == "closed, version changed since"
    told = "".join(_strings(causes))  # no path, and no key of a group
    assert "$.status" not in told and not any(key in _ID.sub("", told) for key in KEYS)
    monkeypatch.setattr(readiness, "BRIEF_CAUSES", 3)
    cut = _brief(client)["causes"]
    assert cut["text"] == f"{text}; the first 3 of 5 are listed" and cut["total"] == 5
    assert [item["root_cause_id"][3] for item in cut["items"]] == ["e", "a", "f"]


async def _unread(ctx: Context) -> None:
    """unit-runner's soak over its inline list of eight keys: its first cycle holds three different keys in
    two root-cause groups; in its second seven keys could not be read and one matched. Measured by the
    worker."""
    h = Harness(ctx)
    run_id = await started(h)
    first = await ended(h, run_id, MATCH | dict.fromkeys(KEYS[:3], MISMATCH))
    await _grouped(ctx, run_id, first, {"a": 2, "b": 1})
    await ended(h, run_id, dict.fromkeys(KEYS[:7], "INCONCLUSIVE") | {"K008": "MATCH"})
    await _watch(ctx)


def test_brief_closed_groups_under_keys_not_compared(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cycle whose keys were mostly not read is complete, so it is the reported one, and it holds no
    root-cause group: a root cause groups keys that were compared. The earlier groups read closed, and the
    groups' line says how many keys of the newest were not compared, since a closed group's keys may be among
    them."""
    on(client, _unread)
    brief = _brief(client)
    figures = ("cycle", "complete", "compared", "indeterminate", "mismatch_rate", "root_causes", "marks")
    assert [tuple(point[name] for name in figures) for point in brief["points"]] == [
        (1, True, 8, 0, "0.375", 2, []), (2, True, 1, 7, "0", 0, []),
    ]  # fmt: skip
    causes = brief["causes"]
    text = (
        f"Root-cause groups over 2 complete cycle(s) since {brief['points'][0]['ended_at'][:10]}: 0 opened in"
        " the newest, 0 still open, 2 closed (not observed in it); 7 key(s) of the newest were not compared,"
        " so a closed group's keys may be among them"
    )
    assert causes["text"] == text
    assert [(group["state"], group["since"]) for group in causes["items"]] == [("closed", [])] * 2
    assert text in on(client, _paged, PAIR).sections[-1].lines  # brief.pdf prints it
    monkeypatch.setattr(readiness, "BRIEF_CAUSES", 1)
    assert _brief(client)["causes"]["text"] == f"{text}; the first 1 of 2 are listed"


async def _tabled(ctx: Context) -> tuple[str, str]:
    """Two batch runs of unit-runner whose keys are its key table's: eight keys with a root-cause group,
    then, the table read again, three keys that match."""
    h = Harness(ctx)
    first = await started(h, mode="batch", keys_source="table")
    await ended(h, first, MATCH | {"K008": MISMATCH})
    await _grouped(ctx, first, 1, {"a": 1})
    second = await started(h, keys=KEYS[:3], mode="batch", keys_source="table")
    await ended(h, second, dict.fromkeys(KEYS[:3], "MATCH"))
    await _watch(ctx)
    return first, second


def test_brief_marks_a_key_table_of_another_size(client: TestClient) -> None:
    """A key table is read again at each run, with no new version: where it gives a key list of another
    size the point is marked ``keys``, and a group closed since reads so."""
    first, second = on(client, _tabled)
    brief = _brief(client)
    assert [(p["run_id"], p["total"], p["marks"]) for p in brief["points"]] == [
        (first, 8, []), (second, 3, ["keys"]),
    ]  # fmt: skip
    assert [(g["state"], g["since"], gates.reads(g)) for g in brief["causes"]["items"]] == [
        ("closed", ["keys"], "closed, keys changed since")
    ]
    trend = on(client, _paged, PAIR).sections[-2].lines  # brief.pdf: the second point's line, lettered
    assert not trend[0].endswith("; K") and trend[1].endswith("; K") and trend[2] == gates.LEGEND


async def _kept(ctx: Context) -> dict[str, str]:
    """unit-other's soak, whose two cycles each lack a key, and unit-runner's, whose three complete cycles
    hold root-cause groups; measured by the worker."""
    h = Harness(ctx)
    lacking = await scanned(h, await stored(h, SOAK.replace("unit-runner", "unit-other")))
    for _ in range(2):
        await ended(h, lacking, LACKING)
    good = await scanned(h)
    for groups in ({"a": 1, "b": 1}, {"a": 1}, {"a": 1, "c": 1}):
        await _grouped(ctx, good, await ended(h, good, MATCH), groups)
    await _watch(ctx)
    return {"lacking": lacking, "good": good}


def test_brief_purged_and_empty(client: TestClient) -> None:
    """Without a complete cycle a brief reports none and says why; a purged cycle keeps its figures and no
    root-cause group: an earlier one is left out of the groups' cycles, the reported one leaves none."""
    runs = on(client, _kept)
    none, lacking = _brief(client, "unit-none"), _brief(client, "unit-other")
    assert (none["notes"], none["points"], none["cycle"], none["causes"]) == (
        ["No batch or soak cycle of unit-none is measured yet."], [], None, None,
    )  # fmt: skip
    assert (lacking["notes"], lacking["cycle"], lacking["causes"]) == (
        [
            f"None of the newest 2 measured cycle(s) of unit-other is complete; the newest: cycle 2 of run"
            f" {runs['lacking']} is partial: 1 key(s) cancelled, 0 owed a recheck"
        ],
        None,
        None,
    )
    assert [(p["cycle"], p["complete"], p["cancelled"]) for p in lacking["points"]] == [
        (1, False, 1), (2, False, 1),
    ]  # fmt: skip
    assert said(client.get("/v1/readiness/pairs/customer-x/brief")) == (
        404, "pair customer-x not found: no spec or readiness setting names it",
    )  # fmt: skip
    whole = _brief(client)["causes"]
    assert (whole["cycles"], whole["opened"], whole["still_open"], whole["closed"]) == (3, 1, 1, 1)
    on(client, _purging, runs["good"], 1)
    brief = _brief(client)
    assert [p["root_causes"] for p in brief["points"]] == [None, 1, 2] and brief["notes"] == []
    kept = brief["causes"]
    assert (kept["cycles"], kept["from"]) == (2, brief["points"][1]["ended_at"])
    assert [(i["root_cause_id"][3], i["state"], i["first"]["cycle"], i["seen"]) for i in kept["items"]] == [
        ("a", "still_open", 2, 2), ("c", "opened", 3, 1),
    ]  # fmt: skip
    on(client, _purging, runs["good"], 3)
    answer = client.get(f"/v1/readiness/pairs/{PAIR}/brief")
    brief = answer.json()
    assert (answer.status_code, brief["causes"]) == (200, None)  # readiness never answers 410
    assert brief["notes"] == [f"Cycle 3 of run {runs['good']} is purged: its root-cause groups are not kept."]
    assert (brief["cycle"]["cycle"], brief["cycle"]["root_causes"], brief["cycle"]["compared"]) == (
        3, None, 1_000,
    )  # fmt: skip


def _cell(value: Any) -> str:
    """A value of a brief's answer as its CSVs hold it."""
    if isinstance(value, list):
        return " ".join(value)
    return "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


def test_brief_reports(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair's brief as its reports: one page, its newest complete cycle's figures between the notes that
    place them, with its gates, its points and its groups, each list cut where a line says so; a row a
    point; a row a group, never cut and refused over its rows. Each is refused without the pair, whatever
    programme and entity type are asked for, and audited with the pair."""
    runs = on(client, _burned)
    brief, report = _brief(client), "/v1/readiness/reports/"
    cycle, points, causes = brief["cycle"], brief["points"], brief["causes"]
    grouped = {"programme": "cutover-q4", "entity": "item"}
    for name in reports.BRIEFS:
        assert [said(client.get(report + name, params=asked)) for asked in ({}, grouped)] == [
            (422, f"{name} is a pair's report: give pair=")
        ] * 2
    asked = ({"pair": "customer-x"}, {"pair": PAIR, "programme": "cutover-q9"}, {"pair": PAIR, "volume": 2})
    assert [said(client.get(report + "brief.pdf", params=params)) for params in asked] == [
        (404, "pair customer-x not found: no spec or readiness setting names it"),
        (404, "programme cutover-q9 is not configured"),
        (422, "volume must be 1 to 1 for this selection (100 pairs a volume)"),
    ]
    assert audited(client, "report.download") == []  # a refusal is no download
    pdf = client.get(report + "brief.pdf", params={"pair": PAIR})
    assert (pdf.status_code, pdf.headers["content-type"], pdf.headers["content-disposition"]) == (
        200, "application/pdf", 'attachment; filename="readiness-brief.pdf"',
    )  # fmt: skip
    assert pdf.content.startswith(b"%PDF-") and len(re.findall(rb"/Type /Page\b", pdf.content)) == 1
    # The page: which cycle the figures are of above them, the stored rate under them, then that they are
    # of keys only.
    paged = on(client, _paged, PAIR)
    new, ended = cycle["spec_id"], cycle["ended_at"][:10]
    assert paged.compact and [section.title for section in paged.sections] == [
        f"{PAIR} 1.1.0 (spec {new}): not ready", "Newest complete cycle", "Gates, approval and sign-off",
        "Trend", "Root-cause groups",
    ]  # fmt: skip
    told, figures, gated, trend, groups = (list(section.lines) for section in paged.sections)
    assert told == [gates.DEFINITION, gates.BRIEF] and len(cycle["notes"]) == 3
    assert [figures[0], *figures[3:]] == [cycle["notes"][0], cycle["notes"][2], M5]
    assert figures[1].startswith(f"Cycle 1 of run {runs['newest']}, ended {ended} ")
    assert figures[1].endswith(f" UTC, 1.1.0 (spec {new}), basis keys, observed.")
    assert figures[2] == (
        "Keys attempted 5. Compared 5 (coverage 1): Match 5, Known difference 0, Noise only 0, Mismatch 0,"
        " Not found on a 0, Not found on b 0 (these six add up to Compared). Inconclusive 0, Drift 0,"
        " Unexplained CRITICAL 0, Root-cause groups 2."
    )
    assert gated[0].startswith("4 of 5 gates: population met; critical met; targets met; convergence not ")
    assert "; graph not applicable (not required); spec approval unavailable; " in gated[0]
    by = {gate["gate"]: gate["text"] for gate in brief["gates"]}
    assert len(by["convergence"]) > 300 and by["convergence"].endswith(SCHEDULE)
    assert gated[1:] == [
        # Cut where a clause ends: its soak's schedule is left out whole, never without its label.
        f"convergence not met: {by['convergence'].split('; ')[0]}…",
        f"spec approval unavailable: {by['spec_approval']}",
        f"sign-off unavailable: {by['signoff']}",
        f"pack unavailable: {by['pack']}",
    ]
    counts = "compared, 0 different, 0 known difference, 0 noise only; rate 0; 2 root-cause group(s)"
    at = [
        f"cycle {point['cycle']} of run {point['run_id']}, {point['ended_at'][:10]}, spec {point['spec_id']}"
        for point in points
    ]
    assert trend == [
        f"{at[0]}, census: 1,000 {counts.replace('2 root', '3 root')}",
        f"{at[1]}, census, incomplete: 999 {counts}",
        f"{at[2]}, census: 1,000 {counts}; V",
        f"{at[3]}, keys: 5 {counts}; K",
        f"{at[4]}, keys: 5 {counts}",
        gates.LEGEND,
    ]
    assert groups[0] == causes["text"] and [line.split(": ")[0] for line in groups[1:]] == [
        "RC-eeeeeeeeeeee opened",
        "RC-aaaaaaaaaaaa still open",
        "RC-ffffffffffff closed",
        "RC-bbbbbbbbbbbb closed, keys changed since",
        "RC-cccccccccccc closed, version and keys changed since",
    ]
    assert groups[2].split(": ")[1] == (
        f"HIGH VALUE_MISMATCH value_changed, 4 key(s), in 4 cycle(s), first cycle 1 of run {runs['first']}"
        f" ({points[0]['ended_at'][:10]}), last cycle 1 of run {runs['newest']} ({ended})"
    )
    for name in ("BRIEF_POINTS", "BRIEF_GROUPS"):
        monkeypatch.setattr(reports, name, 2)
    cut = on(client, _paged, PAIR).sections
    assert list(cut[3].lines) == ["… 3 earlier: brief.csv", *trend[3:]]
    assert list(cut[4].lines) == [*groups[:3], "… 3 more: causes.csv"]
    # brief.csv: a row a point, whatever programme and entity type are asked for.
    header, rows = parse_csv(client.get(report + "brief.csv", params=grouped | {"pair": PAIR}).content)
    plain = POINT - {"ended_at", "by_outcome"}
    assert tuple(header) == reports.BRIEF and len(header) == 1 + len(plain) + 1 + 8
    for row, point in zip(rows, points, strict=True):
        assert {name: row[name] for name in plain} == {name: _cell(point[name]) for name in plain}
        assert {name.upper(): int(row[name]) for name in header[29:37]} == point["by_outcome"]
        assert (row["pair"], datetime.fromisoformat(row["ended_at"])) == (
            PAIR, datetime.fromisoformat(point["ended_at"]),
        )  # fmt: skip
    assert [row["marks"] for row in rows] == ["", "", "version", "keys", ""]
    # causes.csv: a row a group, every one whatever a brief lists, refused over its rows.
    monkeypatch.setattr(readiness, "BRIEF_CAUSES", 2)
    assert len(_brief(client)["causes"]["items"]) == 2
    header, rows = parse_csv(client.get(report + "causes.csv", params={"pair": PAIR}).content)
    ends = [(end, name) for end in ("first", "last") for name in ("run_id", "cycle")]
    flat = [
        {"pair": PAIR}
        | {name: _cell(item[name]) for name in GROUP - {"first", "last"}}
        | {f"{end}_{name}": _cell(item[end][name]) for end, name in ends}
        for item in causes["items"]
    ]
    assert tuple(header) == reports.CAUSES and len(flat) == 5
    assert [{name: row[name] for name in flat[0]} for row in rows] == flat
    assert [row["since"] for row in rows] == ["", "", "", "keys", "version keys"]
    assert [datetime.fromisoformat(row["last_ended_at"]) for row in rows] == [
        datetime.fromisoformat(item["last"]["ended_at"]) for item in causes["items"]
    ]
    monkeypatch.setattr(reports, "CAUSES_ROWS", 4)
    assert said(client.get(report + "causes.csv", params={"pair": PAIR})) == (
        422, "causes.csv would hold 5 rows, over its 4",
    )  # fmt: skip
    detail = {"programme": None, "config_sha256": brief["config_sha256"], "pair": PAIR}
    assert [(found[1], found[2]) for found in audited(client, "report.download")] == [
        ("readiness", {"name": name} | detail) for name in reports.BRIEFS
    ]
    # A sampled cycle: how it is judged and its strata under its figures, which hold no coverage.
    on(client, _drawn)
    monkeypatch.setattr(reports, "BRIEF_STRATA", 1)
    sampled_ = _brief(client, "customer-profile")["cycle"]
    figures = list(on(client, _paged, "customer-profile").sections[1].lines)
    assert figures[1].startswith("Keys attempted 6. Compared 6: Match 6, ") and figures[2:] == [
        *sampled_["notes"], sampled_["strata"][0], "… 1 more: the run's report.pdf",
    ]  # fmt: skip
    # Without a reported cycle it says why and has no groups; a purged one keeps its figures.
    none = on(client, _paged, "unit-none")
    assert [(section.title, list(section.lines)) for section in none.sections[:2]] == [
        ("unit-none: no evidence", [gates.DEFINITION, gates.BRIEF]),
        ("Newest complete cycle", ["No batch or soak cycle of unit-none is measured yet."]),
    ]
    assert (len(none.sections), none.sections[3].title, list(none.sections[3].lines)) == (
        4, "Trend", [gates.LEGEND],
    )  # fmt: skip
    for name in ("brief.csv", "causes.csv"):
        assert parse_csv(client.get(report + name, params={"pair": "unit-none"}).content)[1] == []
    on(client, _purging, runs["newest"], 1)
    purged = on(client, _paged, PAIR).sections
    assert list(purged[0].lines) == told and purged[1].lines[2].endswith(", Root-cause groups not kept.")
    assert purged[3].lines[-2].endswith("; rate 0; root-cause groups not kept")
    assert list(purged[4].lines) == [
        f"Cycle 1 of run {runs['newest']} is purged: its root-cause groups are not kept."
    ]
    header, rows = parse_csv(client.get(report + "causes.csv", params={"pair": PAIR}).content)
    assert (tuple(header), rows) == (reports.CAUSES, [])


def test_never_told(client: TestClient) -> None:
    """Over every answer (a pair's brief among them), the dashboard's and a brief's PDF and every CSV:
    no percentage but in the texts of the targets gate; no future time but a labelled schedule;
    nothing forecast, weighted, averaged or certified beyond the definition and what the report says it is
    not; and no key, nor a root-cause group's path."""
    runs = on(client, _pairs)
    on(client, _fail, runs["good"])
    for cycle, groups in ((3, {"a": 1}), (4, {"a": 2, "b": 1})):
        on(client, _grouped, runs["good"], cycle, groups)
    then = client.get("/v1/readiness").json()["now"]
    answers = [
        client.get(path, params=params).json()
        for path, params in (
            ("/v1/readiness", {"closed": "all"}),
            ("/v1/readiness", {"as_of": then}),
            (f"/v1/readiness/pairs/{PAIR}", {"precheck": "1"}),
            ("/v1/readiness/pairs/unit-other", {}),
            ("/v1/readiness/pairs/unit-none", {}),
            (f"/v1/readiness/pairs/{PAIR}/cycles", {}),
            (f"/v1/readiness/pairs/{PAIR}/cycles", {"bucket": "week"}),
            ("/v1/readiness/transitions", {}),
            (f"/v1/readiness/pairs/{PAIR}/brief", {}),
            ("/v1/readiness/pairs/unit-other/brief", {}),
            ("/v1/readiness/pairs/unit-none/brief", {}),
        )
    ]
    texts = [text for answer in answers for text in _strings(answer)]
    pairs = (PAIR, "unit-other", "unit-none")
    briefed = [f"{name}?pair={pair}" for name in ("brief.csv", "causes.csv") for pair in pairs]
    for name in ("dashboard.csv", "trends.csv", *briefed):
        header, rows = parse_csv(client.get(f"/v1/readiness/reports/{name}").content)
        texts += [*header, *(cell for row in rows for cell in row.values())]
    texts += on(client, _dashboard)
    for pair in pairs:
        texts += _printed(on(client, _paged, pair))
    for name in ("dashboard.pdf", f"brief.pdf?pair={PAIR}"):
        assert client.get(f"/v1/readiness/reports/{name}").content.startswith(b"%PDF-")
    assert gates.DEFINITION in texts and reports.NOT[0] in texts and len(texts) > 500
    targets = [text for text in texts if "pooled 0.1% target" in text]
    assert len(targets) >= 6  # the allowed texts: the targets gate's, on each surface
    scheduled = [text for text in texts if "scheduled to start" in text]
    assert scheduled and all(text.endswith(SCHEDULE) for text in scheduled)
    said_not = {gates.DEFINITION, *reports.NOT, *targets}
    told = [
        (found.group(0), text)
        for text in texts
        if text not in said_not
        for found in BANNED.finditer(text.replace(SCHEDULE, ""))
    ]
    assert told == []
    # Wherever a discrepant count is told, the suppressed count stands beside it.
    counted = [text for text in texts if "discrepant or unresolved key(s)" in text]
    assert len(counted) >= 12 and all(" suppressed" in text for text in counted)
    assert any(text.endswith("1 allowed, 1 suppressed") for text in counted)  # the PDF's cycle 3
    points = [point for item in answers[0]["items"] for point in item["trend"]]
    assert len(points) == 5 and all(point["suppressed"] is not None for point in points)
    assert all("suppressed" in point for answer in answers[5:7] for point in answer["items"])
    assert not any("weighted_rate" in text for text in texts)
    without_ids = [_ID.sub("", text) for text in texts]
    assert not any(key in text for text in without_ids for key in KEYS)  # no key, masked or raw
    assert not any(re.search(r"\b(tok|key)_?(masked|enc)\b", text) for text in texts)
    # The brief: what it is, its stored rate and its groups, none with its path, its keys or a token.
    brief = answers[8]
    assert gates.BRIEF in texts and gates.LEGEND in texts and M9.format("0.002", 2, "1,000") in texts
    assert (brief["causes"]["total"], len(brief["causes"]["items"])) == (2, 2)
    assert not any("$." in text or text in ("path_pattern", "sample_keys") for text in texts)
