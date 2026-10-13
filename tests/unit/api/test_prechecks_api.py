"""Table pre-checks over the API: who may make, read and cancel them
in each auth mode, every refusal with its status and binding message, what an unapproved version is read
of and shown as under SSO, the label of an unlicensed evaluation, the reports, what the list reads of the
store, and what no surface ever tells. The worker's reads are tested in
tests/unit/runner/test_precheck_run.py; here the app's own worker reads an engine held in memory."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from deltasleuth import prechecks
from deltasleuth.api.routes.prechecks import Verdict
from deltasleuth.auth.principal import expand
from deltasleuth.connectors.tables import ENGINES, StatementTimeout, TargetUnreachable
from deltasleuth.core import precheck
from deltasleuth.core.spec import compile_spec
from deltasleuth.reports import precheck as reports
from deltasleuth.store import repo
from tests.conftest import BUILDS, TESTER
from tests.unit.runner.prechecking import FakeEngine, column, relation, table_spec

from .conftest import OIDC, SPEC_YAML, TARGETS, approved, audited, said

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
DATABASES = TARGETS + "".join(
    f"  {name}:\n    dsn_ref: file:/run/secrets/unused\n    rate_per_s: 1000\n" for name in ("db-a", "db-b")
)
PC = "PC-" + "0" * 25 + "1"
NAMES = ("accounts", "cards", "loans", "users")
COLUMNS = (column("email"), column("balance", "numeric(12,2)", "decimal", size=("digits", 12, 2)))
ITEM = {
    "precheck_id", "source", "level", "pairs", "skipped", "evaluation", "created_by", "created_by_name",
    "created_at", "cancelled_by", "cancelled_by_name", "cancelled_at", "finished_at", "progress", "verdicts",
    "totals",
}  # fmt: skip
PAIR = {"n", "label", "spec_id", "status", "verdict", "differing", "rows", "text"}
N1 = re.compile(r"\b(match|matches|matched|identical|passed|verified|validated|reconciled)\b|[✓✔]", re.I)
N2 = re.compile(r"%|percent|\bscore\b|\b[0-9]+ of [0-9]+ (tables|pairs)\b", re.I)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    """Every database target answers as the engine in memory: a table of each of ``NAMES``."""
    engine = FakeEngine(*(relation(name, *COLUMNS) for name in NAMES))
    monkeypatch.setitem(ENGINES, "postgresql", engine)
    return engine


def stored(client: TestClient, text: str) -> int:
    """A spec stored as the tester's upload, unchecked against the targets."""

    async def save(ctx: Any) -> int:
        async with ctx.pool.connection() as conn:
            return await repo.save_spec(conn, compile_spec(json.loads(text)), created_by=TESTER.actor)

    return client.portal.call(save, client.app.state.ctx)


def ended(client: TestClient, precheck_id: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """The pre-check once the app's worker has ended it."""
    deadline = time.monotonic() + 60
    while (found := client.get(f"/v1/prechecks/{precheck_id}", headers=headers).json())[
        "finished_at"
    ] is None:
        assert time.monotonic() < deadline, "the pre-check has not ended"
        time.sleep(0.05)
    return found


def checked(client: TestClient, *specs: str, headers: dict[str, str] | None = None, **body: Any) -> str:
    """A pre-check of the stored ``specs``, made over the API and read by the app's worker."""
    spec_ids = [stored(client, text) for text in specs]
    made = client.post("/v1/prechecks", json={"spec_ids": spec_ids} | body, headers=headers)
    assert made.status_code == 202, made.text
    ended(client, made.json()["precheck_id"], headers)
    return made.json()["precheck_id"]


def test_viewers_read_and_operators_make_and_cancel_under_sso(open_client: OpenClient, callers: Any) -> None:
    with open_client(auth={"oidc": OIDC}, targets=DATABASES) as client:
        viewer = callers(client).session("viewer", username="vera")
        operator = callers(client).session(*expand(["operator"]), username="olga")
        spec_id = stored(client, table_spec())
        body = {"spec_ids": [spec_id], "level": "counts"}
        denied = [
            client.post("/v1/prechecks", json=body, headers=viewer),
            client.post(f"/v1/prechecks/{PC}/cancel", headers=viewer),
        ]
        made = client.post("/v1/prechecks", json=body, headers=operator)
        precheck_id = made.json()["precheck_id"]
        reads = [
            client.get(path, headers=viewer)
            for path in (
                "/v1/prechecks", f"/v1/prechecks/{precheck_id}", f"/v1/prechecks/{precheck_id}/pairs/1",
                f"/v1/prechecks/{precheck_id}/reports/precheck.csv",
            )
        ]  # fmt: skip
        cancelled = client.post(f"/v1/prechecks/{precheck_id}/cancel", headers=operator)
        created, downloads, cancels = (
            audited(client, action) for action in ("precheck.create", "report.download", "precheck.cancel")
        )
    assert [said(r) for r in denied] == [(403, "needs the operator role")] * 2
    assert made.status_code == 202 and made.json() == {
        "not": precheck.NOT, "precheck_id": precheck_id, "pairs": 1, "skipped": 0, "evaluation": False,
    }  # fmt: skip
    assert re.fullmatch("PC-[0-9A-Z]{26}", precheck_id) and [r.status_code for r in reads] == [200] * 4
    listed, shown, pair = (r.json() for r in reads[:3])
    assert set(listed) == {"not", "banners", "items", "next_cursor"} and listed["not"] == precheck.NOT
    assert set(listed["items"][0]) == ITEM and listed["items"][0]["created_by_name"] == "olga"
    assert set(shown) == ITEM | {"not", "banners", "worded", "items", "next_cursor"}
    assert (shown["level"], shown["source"], shown["progress"]) == ("counts", "specs", {"pending": 1})
    assert shown["totals"] == "1 pair: 1 pending" and shown["worded"] == {
        verdict: precheck.worded(verdict) for verdict in precheck.VERDICTS
    }
    assert shown["items"] == [
        {"n": 1, "label": "accounts", "spec_id": spec_id, "status": "pending", "verdict": None,
         "differing": None, "rows": [None, None], "text": None}
    ]  # fmt: skip
    assert pair["targets"] == ["db-a", "db-b"] and pair["relations"] == ["accounts", "accounts"]
    assert pair["result"] is None and pair["columns"] == [] and pair["not"] == precheck.NOT
    assert cancelled.status_code == 200 and set(cancelled.json()) == {"not", "precheck_id", "cancelled_at"}
    assert cancelled.json()["not"] == precheck.NOT  # every answer but a download carries it
    [(actor, obj, detail)] = created
    assert actor.startswith("user:U-") and (obj, detail) == (
        precheck_id, {"source": "specs", "level": "counts", "pairs": 1, "skipped": 0, "spec_ids": [spec_id]},
    )  # fmt: skip
    assert [(obj, detail) for _, obj, detail in downloads] == [(precheck_id, {"name": "precheck.csv"})]
    assert [(obj, detail) for _, obj, detail in cancels] == [(precheck_id, {"pending": 1, "running": 0})]


def test_tokens_and_developer_mode_make_them_as_their_callers(open_client: OpenClient, callers: Any) -> None:
    with open_client(auth={}, targets=DATABASES) as client:
        viewer, operator = callers(client).token("viewer"), callers(client).token("operator", "ops-bot")
        body = {"spec_ids": [stored(client, table_spec())]}
        refused = client.post("/v1/prechecks", json=body, headers=viewer)
        made = client.post("/v1/prechecks", json=body, headers=operator)
        read = client.get(f"/v1/prechecks/{made.json()['precheck_id']}", headers=viewer)
        cancelled = client.post(f"/v1/prechecks/{made.json()['precheck_id']}/cancel", headers=operator)
        actors = [actor for actor, _, _ in audited(client, "precheck.create")]
    assert said(refused) == (403, "needs the operator role")
    assert (made.status_code, read.status_code, cancelled.status_code) == (202, 200, 200)
    assert read.json()["level"] == "columns" and made.json()["evaluation"] is False  # an open build's
    assert len(actors) == 1 and actors[0].startswith("token:T-")
    with open_client(targets=DATABASES) as client:
        made = client.post("/v1/prechecks", json={"spec_names": ["accounts"]})
        cancelled = client.post(f"/v1/prechecks/{made.json()['precheck_id']}/cancel")
        actors = [actor for actor, _, _ in audited(client, "precheck.create")]
    assert (made.status_code, cancelled.status_code, actors[-1]) == (202, 200, "dev:loopback")


def test_each_refusal_has_its_status_and_message(
    open_client: OpenClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_client(targets=DATABASES) as client:
        post = lambda **body: said(client.post("/v1/prechecks", json=body))  # noqa: E731
        assert post(all_specs=True) == (422, "no stored spec is a pair of tables")
        service = client.post("/v1/specs", content=SPEC_YAML).json()["spec_id"]
        assert post(all_specs=True) == (422, "no stored spec is a pair of tables")
        old, new = stored(client, table_spec()), stored(client, table_spec(version="1.1.0"))
        cards = stored(client, table_spec("cards"))
        one = "give spec_ids, spec_names or all_specs, and only one of them"
        assert post() == post(spec_ids=[old], all_specs=True) == (422, one)
        assert post(spec_ids=[old], spec_names=["accounts"]) == post(spec_names=["cards"], all_specs=True)
        assert post(spec_names=["cards"], all_specs=True) == (422, one)
        assert post(spec_ids=[old, 999]) == (404, "spec 999 not found")
        assert post(spec_names=["accounts", "customer-profile"]) == (404, "no spec is named customer-profile")
        assert post(spec_ids=[old, cards, old]) == (422, f"spec {old} is listed twice")
        assert post(spec_names=["accounts", "accounts"]) == (422, f"spec {new} is listed twice")
        assert post(spec_ids=[cards, service]) == (
            422,
            f"spec {service} (api-demo 1.0.0) is not a pair of tables: a table pre-check reads two table"
            " sides",
        )
        level = "level must be schema, counts, columns or distinct"
        assert post(spec_ids=[old], level="rows") == post(all_specs=True, level="") == (422, level)
        assert post(spec_ids=[]) == (422, "spec_ids takes 1 to 500 ids and 0 were given")
        assert post(spec_ids=list(range(1, 813))) == (422, "spec_ids takes 1 to 500 ids and 812 were given")
        names = [f"n{i}" for i in range(812)]
        assert post(spec_names=names) == (422, "spec_names takes 1 to 500 names and 812 were given")
        monkeypatch.setattr(prechecks, "PAIRS", 1)
        assert post(all_specs=True) == (
            422,
            "2 stored specs are pairs of tables and a table pre-check takes up to 1: name them with"
            " spec_names or spec_ids",
        )
        monkeypatch.undo()
        assert audited(client, "precheck.create") == []  # a refusal writes nothing
        # The newest version of every name whose sides are both tables; the others are counted.
        every = client.post("/v1/prechecks", json={"all_specs": True, "level": "schema"})
        assert every.status_code == 202 and (every.json()["pairs"], every.json()["skipped"]) == (2, 1)
        precheck_id = every.json()["precheck_id"]
        pairs = client.get(f"/v1/prechecks/{precheck_id}").json()["items"]
        assert sorted((pair["label"], pair["spec_id"]) for pair in pairs) == [
            ("accounts", new),
            ("cards", cards),
        ]
        for _ in range(19):
            assert client.post("/v1/prechecks", json={"spec_names": ["accounts"]}).status_code == 202
        assert post(spec_ids=[old]) == (
            409,
            "this installation holds 20 unfinished table pre-checks, the most it takes: cancel one or wait",
        )
        gone = f"table pre-check {PC} not found (pre-checks are deleted 90 days after they are made)"
        reads = (
            f"/v1/prechecks/{PC}",
            f"/v1/prechecks/{PC}/pairs/1",
            f"/v1/prechecks/{PC}/reports/precheck.pdf",
        )
        assert {said(client.get(path)) for path in reads} == {(404, gone)}
        assert said(client.post(f"/v1/prechecks/{PC}/cancel")) == (404, gone)
        assert said(client.get(f"/v1/prechecks/{precheck_id}/pairs/7")) == (
            404, f"pair 7 of table pre-check {precheck_id} not found",
        )  # fmt: skip
        assert client.post(f"/v1/prechecks/{precheck_id}/cancel").status_code == 200
        assert said(client.post(f"/v1/prechecks/{precheck_id}/cancel")) == (
            409, f"table pre-check {precheck_id} has ended",
        )  # fmt: skip
        assert client.post("/v1/prechecks", json={"spec_ids": [old]}).status_code == 202  # one has ended
        assert said(client.get(f"/v1/prechecks/{precheck_id}/reports/findings.csv")) == (
            404, "report 'findings.csv' not found",
        )  # fmt: skip
        assert said(client.get(f"/v1/prechecks/{precheck_id}?cursor=x")) == (400, "malformed cursor")
        unknown = client.get(f"/v1/prechecks/{precheck_id}?verdict=fine")
        assert (unknown.status_code, unknown.json()["detail"]) == (422, "invalid request")
        mistyped = client.post("/v1/prechecks", json={"spec_ids": [old], "levels": "counts"})
        assert (mistyped.status_code, mistyped.json()["detail"]) == (422, "invalid request")
    assert get_args(Verdict) == precheck.VERDICTS


def test_the_create_row_lists_fifty_spec_ids_and_counts_more(open_client: OpenClient) -> None:
    with open_client(targets=DATABASES) as client:
        spec_ids = [stored(client, table_spec("accounts", version=f"1.0.{n}")) for n in range(51)]
        fifty = client.post("/v1/prechecks", json={"spec_ids": spec_ids[:50], "level": "schema"})
        more = client.post("/v1/prechecks", json={"spec_ids": spec_ids, "level": "schema"})
        rows = audited(client, "precheck.create")
    assert (fifty.status_code, more.status_code) == (202, 202)
    assert [detail["spec_ids"] for _, _, detail in rows] == [spec_ids[:50], 51]
    assert [detail["pairs"] for _, _, detail in rows] == [50, 51]


FILTERED = table_spec("cards", a={"where": "id >= 2"}, b={"where": "id >= 2"})
CLEAR = table_spec("loans", security={"unlisted": "clear"})


def test_unapproved_filter_reads_schema_only(open_client: OpenClient, callers: Any, fake: FakeEngine) -> None:
    """Under SSO no unapproved filter reaches a target, so such a version is read at level schema
    with no probe; once approved, or outside SSO, it reads the level asked."""
    with open_client(auth={"oidc": OIDC}, targets=DATABASES, worker=True) as client:
        operator = callers(client).session(*expand(["operator"]), username="olga")
        spec_id = stored(client, FILTERED)
        precheck_id = checked(client, FILTERED, headers=operator)
        unapproved = client.get(f"/v1/prechecks/{precheck_id}/pairs/1", headers=operator).json()
        exports = [
            client.get(f"/v1/prechecks/{precheck_id}/reports/{name}", headers=operator).text
            for name in ("precheck.csv", "precheck-columns.csv")
        ]
        sent = [event for event, _, _ in fake.events]
        approved(client, spec_id)
        allowed = checked(client, FILTERED, headers=operator)
        admitted = client.get(f"/v1/prechecks/{allowed}/pairs/1", headers=operator).json()
        probed = fake.count("probe")
    why = (
        f"not checked: spec {spec_id} (cards 1.0.0) declares a filter and is not approved: only its schema"
        " was read"
    )
    assert (unapproved["verdict"], unapproved["text"], unapproved["rows"]) == (
        "incomplete",
        why,
        [None, None],
    )
    assert set(sent) == {"snapshot", "describe", "close"}  # no probe, no figure: the filter reached no target
    result = unapproved["result"]
    assert (result["level"], result["skipped"]) == ("schema", ["unapproved_filter"])
    assert result["keys"]["a"] == {
        "rows": None, "null_keys": None, "duplicate_keys": None, "rows_in_duplicates": None,
        "primary": ["id"],
    }  # fmt: skip
    assert {name: found["figures"] for name, found in result["columns"].items()} == {
        "email": {},
        "balance": {},
    }
    assert unapproved["columns"][0]["lines"] == ["not compared: level schema"]
    assert f",incomplete,0,,,{why}," in exports[0] and ",not compared,level schema" in exports[1]
    assert admitted["verdict"] == "no_difference_observed" and admitted["rows"] == [3, 3] and probed == 2
    assert (
        admitted["result"]["level"] == "columns"
        and "nulls" in admitted["result"]["columns"]["email"]["figures"]
    )
    fake.events.clear()
    with open_client(auth={}, targets=DATABASES, worker=True) as client:  # tokens mode has no gate
        operator = callers(client).token("operator")
        read = checked(client, FILTERED, headers=operator)
        pair = client.get(f"/v1/prechecks/{read}/pairs/1", headers=operator).json()
    assert pair["verdict"] == "no_difference_observed" and fake.count("probe") == 2 and pair["rows"] == [3, 3]


def test_sums_are_withheld_under_sso_without_the_approval(
    open_client: OpenClient, callers: Any, fake: FakeEngine
) -> None:
    fake.values["db-b", "sum", "balance"] = "47.50"
    with open_client(auth={"oidc": OIDC}, targets=DATABASES, worker=True) as client:
        operator = callers(client).session(*expand(["operator"]), username="olga")
        spec_id = stored(client, CLEAR)

        def balance() -> tuple[dict[str, Any], list[str], str]:
            precheck_id = checked(client, CLEAR, headers=operator)
            pair = client.get(f"/v1/prechecks/{precheck_id}/pairs/1", headers=operator).json()
            lines = next(found["lines"] for found in pair["columns"] if found["column"] == "balance")
            export = client.get(f"/v1/prechecks/{precheck_id}/reports/precheck-columns.csv", headers=operator)
            return pair["result"]["columns"]["balance"]["figures"]["sum"], lines, export.text

        withheld, said_, exported = balance()
        approved(client, spec_id)
        shown, lines, cleared = balance()
    reason = f"figures withheld: spec {spec_id} is not approved"
    assert withheld == {"verdict": "differs", "reason": reason} and said_[1] == f"sum: differs ({reason})"
    assert f"balance,decimal,decimal,sum,,,differs,{reason}\r\n" in exported
    assert "60" not in exported.split("sum,")[1].split("\r\n")[0]
    assert shown == {"a": "60", "b": "47.50", "verdict": "differs", "reason": "side b is 12.50 lower"}
    assert lines[1] == "sum: side a 60, side b 47.50 (side b is 12.50 lower)"
    assert "balance,decimal,decimal,sum,60,47.50,differs,side b is 12.50 lower\r\n" in cleared
    # A minimum and a maximum are never numbers, whatever is approved.
    assert "balance,decimal,decimal,min,,,same,\r\n" in cleared and lines[2] == (
        "min: same; max: same (values are never shown)"
    )


@pytest.mark.usefixtures("vendor_keys")
def test_the_evaluation_label_is_on_the_item_the_list_and_the_exports(
    open_client: OpenClient, fake: FakeEngine
) -> None:
    with open_client(targets=DATABASES, worker=True) as client:  # no licence: an unlicensed evaluation
        precheck_id = checked(client, table_spec())
        path = f"/v1/prechecks/{precheck_id}"
        item, pair = client.get(path).json(), client.get(f"{path}/pairs/1").json()
        listed = client.get("/v1/prechecks").json()["items"]
        exports = {name: client.get(f"{path}/reports/{name}") for name in reports.NAMES}
        row, pairs = client.portal.call(reports.loaded, client.app.state.ctx, precheck_id)
    assert item["evaluation"] is pair["evaluation"] is listed[0]["evaluation"] is True
    assert {name: r.headers["content-disposition"] for name, r in exports.items()} == {
        name: f'attachment; filename="{precheck_id}-unlicensed-evaluation-{name}"' for name in reports.NAMES
    }
    head = f'"{precheck.NOT}"\r\nUnlicensed evaluation\r\nprecheck_id,n,label,'
    assert all(exports[name].text.startswith(head) for name in reports.NAMES[:2])
    assert exports["precheck.pdf"].content.startswith(b"%PDF") and row["evaluation"]
    document = reports.document(row, pairs)
    assert (document.meta, document.warning) == (precheck.NOT, "Unlicensed evaluation")


@pytest.mark.parametrize("build", BUILDS, indirect=True)
def test_a_licensed_pre_check_carries_no_label(build: str, open_client: OpenClient) -> None:
    # under the fixture's licence, or in an open build, which needs none
    with open_client(auth={"oidc": OIDC}, targets=DATABASES) as client:
        ctx = client.app.state.ctx
        stored(client, table_spec())
        made = client.portal.call(prechecks.create, ctx, {"spec_names": ["accounts"]}, TESTER)
        row, pairs = client.portal.call(reports.loaded, ctx, made["precheck_id"])
    assert made["evaluation"] is False and row["evaluation"] is False
    assert prechecks.file_name(row, "precheck.csv") == f"{made['precheck_id']}-precheck.csv"
    assert reports.pairs_csv(row, pairs).startswith(f'"{precheck.NOT}"\r\nprecheck_id,n,label,')
    assert reports.document(row, pairs).warning is None


def test_the_list_and_a_pre_check_read_no_result(
    open_client: OpenClient, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the list prints of a pair is kept beside its ``result`` (its rows and its line), so a
    pre-check of 500 pairs is listed without reading one."""
    with open_client(targets=DATABASES, worker=True) as client:
        precheck_id = checked(client, *(table_spec(name) for name in NAMES))
        sent: list[str] = []
        rows = repo._rows

        async def recorded(conn: Any, sql: str, params: Any = None) -> Any:
            sent.append(sql)
            return await rows(conn, sql, params)

        monkeypatch.setattr(repo, "_rows", recorded)
        listed = client.get("/v1/prechecks?limit=1").json()
        first = client.get(f"/v1/prechecks/{precheck_id}?limit=3").json()
        rest = client.get(f"/v1/prechecks/{precheck_id}?limit=3&cursor={first['next_cursor']}").json()
        only = client.get(f"/v1/prechecks/{precheck_id}?verdict=rows_differ").json()
    asked = [sql for sql in sent if "precheck" in sql]  # the app's worker sweeps meanwhile
    assert len(asked) >= 7 and not [sql for sql in asked if "result" in sql]
    assert [len(found["items"]) for found in (listed, first, rest, only)] == [1, 3, 1, 0]
    assert all(set(pair) == PAIR for pair in first["items"]) and rest["next_cursor"] is None
    assert first["totals"] == "4 pairs: 4 with no difference observed (not parity)"
    assert first["progress"] == {"done": 4} and first["verdicts"] == {"no_difference_observed": 4}


def test_a_pre_check_no_worker_takes_says_so(open_client: OpenClient) -> None:
    with open_client(targets=DATABASES) as client:  # no worker runs
        spec_id = stored(client, table_spec())
        precheck_id = client.post("/v1/prechecks", json={"spec_ids": [spec_id]}).json()["precheck_id"]
        fresh = client.get(f"/v1/prechecks/{precheck_id}").json()["banners"]

        async def aged(ctx: Any) -> None:
            async with ctx.pool.connection() as conn:
                await conn.execute("UPDATE ds.prechecks SET created_at = now() - interval '12 minutes'")

        client.portal.call(aged, client.app.state.ctx)
        banner = ["no worker has taken this pre-check for 12 min: start ds worker, or ds serve"]
        paths = (f"/v1/prechecks/{precheck_id}", "/v1/prechecks")
        waiting = [client.get(path).json()["banners"] for path in paths]

        def claimed(minutes: int) -> list[list[str]]:
            """The banners once the pair is running, its claim last renewed ``minutes`` ago."""

            async def held(ctx: Any) -> None:
                async with ctx.pool.connection() as conn:
                    await conn.execute(
                        """UPDATE ds.precheck_pairs
                           SET status = 'running', claimed_at = now() - %s * interval '1 minute'""",
                        (minutes,),
                    )

            client.portal.call(held, client.app.state.ctx)
            return [client.get(path).json()["banners"] for path in paths]

        # A worker reads it and renews its claim; a worker killed mid-pair renews none: after three lease
        # periods (2 min each here) the pair is read by no worker.
        read, killed = claimed(5), claimed(7)
        client.post(f"/v1/prechecks/{precheck_id}/cancel")
        ended_ = client.get("/v1/prechecks").json()["banners"]
    assert (fresh, waiting, ended_) == ([], [banner, banner], [])
    assert (read, killed) == ([[], []], [banner, banner])


def _texts(value: Any) -> list[str]:
    """Every string a JSON answer holds, its members' names among them."""
    if isinstance(value, dict):
        return [text for name, item in value.items() for text in (name, *_texts(item))]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return [value] if isinstance(value, str) else []


def test_never_told(open_client: OpenClient, fake: FakeEngine) -> None:
    """What no surface of a spec pre-check tells: the API's answers, both CSVs and the PDF's
    document (the CLI prints these texts: tests/unit/cli; the screens: web/test/prechecks.test.ts)."""
    planted = {"min": "7770001", "max": "8880003", "sum": "9990004"}
    for target in ("db-a", "db-b"):
        fake.values |= {(target, figure, "balance"): text for figure, text in planted.items()}
    fake.values |= {("db-b", "rows", None): "5", ("db-b", "min", "balance"): "7770002"}
    fake.fail("figures", "db-b", None, StatementTimeout("MySQL 3024"))  # the second pair read of it
    refused = table_spec("cards", a={"columns": ["nope"]}, b={"columns": ["nope"]})
    with open_client(targets=DATABASES, worker=True) as client:
        precheck_id = checked(client, table_spec(), refused, table_spec("loans"), level="distinct")
        path = f"/v1/prechecks/{precheck_id}"
        shown = client.get(path).json()
        pairs = [client.get(f"{path}/pairs/{pair['n']}").json() for pair in shown["items"]]
        answers = [client.get("/v1/prechecks").json(), shown, *pairs]
        exports = [client.get(f"{path}/reports/{name}").text for name in reports.NAMES[:2]]
        row, read = client.portal.call(reports.loaded, client.app.state.ctx, precheck_id)
    document = reports.document(row, read)
    printed = [document.title, document.meta, document.warning or ""]
    for section in document.sections:
        printed += [section.title, *section.lines, *section.header]
        printed += [str(cell) for cells in section.rows for cell in cells]
    surfaces = {"api": _texts(answers), "csv": exports, "pdf": printed}
    for surface, texts in surfaces.items():
        assert not [text for text in texts if N1.search(text)], surface  # nothing reads as agreeing
        assert not [text for text in texts if N2.search(text)], surface  # no share, no score
        assert precheck.NOT in "\n".join(texts), surface  # it heads each of them, verbatim
        # No minimum, maximum or protected sum reaches a surface.
        assert not [text for text in texts if any(value in text for value in (*planted.values(), "7770002"))]
    assert all(answer["not"] == precheck.NOT for answer in answers)
    assert [pair["verdict"] for pair in shown["items"]] == ["refused", "not_read", "rows_differ"]
    assert shown["totals"] == "3 pairs: 1 a run would refuse, 1 not read, 1 with differing row counts"
    by = {pair["verdict"]: pair for pair in pairs}
    # It quotes the plan when a run would be refused, and never says that one would succeed.
    assert by["refused"]["text"].startswith("a run of this spec would be refused: side a: column 'nope'")
    assert not [text for text in surfaces["api"] + printed if re.search(r"would (succeed|run)\b", text)]
    # A check not made says so with its reason, and is never counted as agreeing.
    hint = "raise precheck.statement_timeout_s, or pre-check a filtered view"
    assert by["not_read"]["text"] == f"not checked: side b's figures timed out after 900 s ({hint})"
    unread = {"verdict": "not checked", "reason": f"side b's figures timed out after 900 s ({hint})"}
    assert by["not_read"]["differing"] == 0 and by["not_read"]["result"]["columns"]["email"]["figures"] == (
        dict.fromkeys(("nulls", "empty", "length", "distinct"), unread)
    )
    # A minimum or a maximum is a verdict, never a value; a withheld sum has none either.
    figures = by["rows_differ"]["result"]["columns"]["balance"]["figures"]
    assert figures["min"] == {"verdict": "differs"} and figures["max"] == {"verdict": "same"}
    assert figures["sum"] == {
        "verdict": "same",
        "reason": "figures withheld: $.balance is protected by the spec's security policy",
    }
    # The two sides are never said to have been read at one moment.
    for pair in pairs:
        assert pair["result"]["lines"][-1].endswith(": not read at one moment")
    assert re.fullmatch(
        r"side a: postgresql 16\.4, public\.\w+, in 1 snapshot at \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC; side b:"
        r" postgresql 16\.4, public\.\w+, in 1 snapshot, \d+ s (later|earlier): not read at one moment",
        by["rows_differ"]["result"]["lines"][-1],
    )
    # The best verdict never reads as agreeing; the PDF says whose view the rows are.
    assert (
        shown["worded"]["no_difference_observed"] == "no difference observed in the checks made (not parity)"
    )
    assert "Rows and values are as the account reads them." in printed


# -- two schemas paired by name ---------------------------------------------------------------------

SCHEMAS = {"a": {"target": "db-a", "schema": "app"}, "b": {"target": "db-b"}}


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    """Side a's schema holds ACCOUNTS, two tables named alike ignoring case and LEDGER; side b's accounts and
    loans: one pair to read, a name of two tables, and one name on each side alone."""
    engine = FakeEngine(*(relation(name, *COLUMNS) for name in ("ACCOUNTS", "accounts")))
    engine.listed = {
        "db-a": [("ACCOUNTS", "table"), ("CARDS", "view"), ("Cards", "table"), ("LEDGER", "table")],
        "db-b": [("accounts", "table"), ("loans", "view")],
    }
    monkeypatch.setitem(ENGINES, "postgresql", engine)
    return engine


def test_spec_authors_pair_two_schemas_by_name_and_draft_a_spec(
    open_client: OpenClient, callers: Any, listed: FakeEngine
) -> None:
    with open_client(auth={"oidc": OIDC}, targets=DATABASES, worker=True) as client:
        caller = callers(client)
        viewer, operator = caller.session("viewer"), caller.session(*expand(["operator"]))
        author = caller.session(*expand(["spec_author"]), username="sam")
        denied = [
            client.post("/v1/prechecks/catalogue", json=SCHEMAS, headers=who) for who in (viewer, operator)
        ]
        made = client.post("/v1/prechecks/catalogue", json=SCHEMAS | {"exclude": ["zz*"]}, headers=author)
        precheck_id = made.json()["precheck_id"]
        shown = ended(client, precheck_id, author)
        pairs = {pair["label"]: pair for pair in shown["items"]}
        read = client.get(
            f"/v1/prechecks/{precheck_id}/pairs/{pairs['accounts']['n']}", headers=author
        ).json()
        path = f"/v1/prechecks/{precheck_id}/pairs/{{}}/draft.yaml"
        draft = client.get(path.format(pairs["accounts"]["n"]), headers=author)
        refusals = [
            said(client.get(path.format(pairs["ledger"]["n"]), headers=author)),
            said(client.get(path.format(pairs["loans"]["n"]), headers=author)),
            said(client.get(path.format(9), headers=author)),
            said(client.get(path.format(pairs["accounts"]["n"]), headers=operator)),
        ]
        spec_id = stored(client, table_spec())
        of_spec = client.post(
            "/v1/prechecks", json={"spec_ids": [spec_id], "level": "schema"}, headers=operator
        )
        stored_pair = said(
            client.get(
                path.replace("{}", "1").replace(precheck_id, of_spec.json()["precheck_id"]), headers=author
            )
        )
        created = audited(client, "precheck.create")
        sessions = [
            (event, target) for event, target, _ in listed.events if event in ("connect", "relations")
        ]
    assert [said(r) for r in denied] == [(403, "needs the spec_author role")] * 2
    assert made.status_code == 202 and made.json() == {
        "not": precheck.NOT, "precheck_id": precheck_id, "pairs": 4, "skipped": 0, "evaluation": False,
    }  # fmt: skip
    assert (shown["source"], shown["level"]) == ("catalogue", "counts")  # counts unless asked
    assert shown["totals"] == "4 pairs: 3 unpaired, 1 with no difference observed (not parity)"
    # Paired by lower-cased name: a name of two tables, or of one side alone, is settled as it is made.
    assert [(pair["label"], pair["verdict"], pair["text"]) for pair in shown["items"][:3]] == [
        ("cards", "unpaired",
         "two tables of side a are named alike ignoring case (CARDS, Cards): compare them in a spec"),
        ("ledger", "unpaired", "no table on side b is named ledger (side a: app.LEDGER; paired by name)"),
        ("loans", "unpaired", "no table on side a is named loans (side b: loans; paired by name)"),
    ]  # fmt: skip
    assert read["relations"] == ["ACCOUNTS", "accounts"] and read["spec_id"] is None
    assert read["result"]["names"]["a"]["name"] == "ACCOUNTS" and read["verdict"] == "no_difference_observed"
    # One short session a side listed the schemas, as a run's creation plans; the worker read the pair.
    assert sessions[:4] == [
        ("connect", "db-a"),
        ("relations", "db-a"),
        ("connect", "db-b"),
        ("relations", "db-b"),
    ]
    [(_, obj, detail)] = created[:1]
    assert (obj, detail) == (
        precheck_id,
        {"source": "catalogue", "level": "counts", "pairs": 4, "skipped": 0,
         "a": {"target": "db-a", "schema": "app"}, "b": {"target": "db-b", "schema": None}},
    )  # fmt: skip
    assert draft.status_code == 200 and draft.headers["content-type"].startswith("application/yaml")
    assert '"accounts", paired by name' in draft.text and "keyColumns: [id]" in draft.text
    assert refusals == [
        (409, f"pair {pairs['ledger']['n']} has no table on side b: there is nothing to draft"),
        (409, f"pair {pairs['loans']['n']} has no table on side a: there is nothing to draft"),
        (404, f"pair 9 of table pre-check {precheck_id} not found"),
        (403, "needs the spec_author role"),
    ]
    assert stored_pair == (409, "pair 1 is a stored spec (accounts 1.0.0): it needs no draft")
    for answer in (shown, read):  # nothing reads as agreeing
        assert not [text for text in _texts(answer) if N1.search(text)]


def test_two_schemas_with_a_files_target_are_refused(open_client: OpenClient, listed: FakeEngine) -> None:
    """A files target has no schema to pair; its run counts rows, lines that are
    not rows and repeated keys."""
    exports = "  exports:\n    engine: files\n    root: /var/lib/deltasleuth/files/legacy\n"
    with open_client(targets=DATABASES + exports) as client:
        refused = said(client.post("/v1/prechecks/catalogue", json=SCHEMAS | {"b": {"target": "exports"}}))
    assert refused == (
        422,
        "not supported: a table pre-check with a file side; its run counts rows, lines that are not rows and"
        " repeated keys",
    )


def test_each_refusal_of_two_schemas_has_its_status_and_message(
    open_client: OpenClient, listed: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_client(targets=DATABASES) as client:
        post = lambda **body: said(client.post("/v1/prechecks/catalogue", json=SCHEMAS | body))  # noqa: E731
        assert post(level="rows") == (422, "level must be schema, counts, columns or distinct")
        assert post(level="distinct") == (
            422,
            "two schemas paired by name take level schema, counts or columns: distinct counts need a stored"
            " spec",
        )
        assert post(a={"target": "crm-old"}) == (404, "target crm-old is not declared")
        assert post(b={"target": "svc-a"}) == (
            422, "target svc-a is a service: a table pre-check reads database targets",
        )  # fmt: skip
        globs = "include and exclude take up to 20 patterns of at most 128 characters"
        assert post(include=["a*"] * 21) == post(exclude=["x" * 129]) == (422, globs)
        listed.fail("connect", "db-a", TargetUnreachable("ORA-12541"))
        assert post() == (422, "side a: cannot list schema app of target db-a (ORA-12541)")
        listed.listed["db-b"] = []
        assert post() == (
            422, "side b: the default schema of target db-b holds no table or view the account can read",
        )  # fmt: skip
        listed.listed["db-b"] = [("accounts", "table"), ("loans", "view")]
        with monkeypatch.context() as patch:
            patch.setattr(prechecks, "RELATIONS", 3)
            assert post() == (
                422,
                "side a: schema app of target db-a holds over 3 relations: name fewer with include",
            )
            narrowed = client.post("/v1/prechecks/catalogue", json=SCHEMAS | {"include": ["l*", "a*"]})
            assert narrowed.status_code == 202  # the globs narrow what is counted
            # No worker runs here: pair 1, accounts on both sides, is pending.
            draft = f"/v1/prechecks/{narrowed.json()['precheck_id']}/pairs/1/draft.yaml"
            assert said(client.get(draft)) == (409, "pair 1 was not read: there is nothing to draft yet")
            patch.setattr(prechecks, "PAIRS", 1)
            assert post(include=["l*", "a*"]) == (
                422,
                "the two schemas give 3 pairs and a table pre-check takes up to 1: narrow them with include"
                " or exclude",
            )
        assert post(include=["nothing*"]) == (
            422,
            "include and exclude leave no table or view of either schema",
        )
        # Every name without a pair: the pre-check ends as it is made, its end audited as the worker's.
        alone = client.post("/v1/prechecks/catalogue", json=SCHEMAS | {"include": ["ledger", "loans"]}).json()
        ended_ = client.get(f"/v1/prechecks/{alone['precheck_id']}").json()
        finished = audited(client, "precheck.finished")
        mistyped = client.post("/v1/prechecks/catalogue", json=SCHEMAS | {"levels": "counts"})
    assert ended_["finished_at"] is not None and ended_["totals"] == "2 pairs: 2 unpaired"
    assert [(actor, obj) for actor, obj, _ in finished] == [("system:worker", alone["precheck_id"])]
    assert (mistyped.status_code, mistyped.json()["detail"]) == (422, "invalid request")
