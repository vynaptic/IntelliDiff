"""Runs: creation through the runner, pages, control, masked views, key detail, reports, compare; under SSO
each waits for its spec version's four-eyes approval."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient

from deltasleuth.auth.principal import expand
from deltasleuth.core.types import KeyResult, Outcome, SideState, SideStateKind
from deltasleuth.reports import FORMATS
from deltasleuth.store import repo
from tests.unit.runner.prechecking import table_spec

from .conftest import OIDC, RAW_KEY, TARGETS, approved, audited, said

pytestmark = pytest.mark.db
RUN_ID = "R-" + "0" * 26


def test_a_new_run_is_created_by_the_runner(client: TestClient, seeded: Any) -> None:
    body = {"spec_id": seeded.spec_id, "mode": "batch", "keys": ["K00000001"]}
    created = client.post("/v1/runs", json=body)
    assert created.status_code == 202
    run = client.get(f"/v1/runs/{created.json()['run_id']}").json()
    assert (run["status"], run["progress"]["keys"]) == ("running", {"pending": 1})
    refused = [
        client.post("/v1/runs", json=body | {"spec_id": 999}),  # no such spec
        client.post("/v1/runs", json=body | {"mode": "soak"}),  # the spec has no cycleInterval
        client.post("/v1/runs", json=body | {"mode": "one_shot"}),
    ]
    assert [r.status_code for r in refused] == [404, 422, 422]


def test_under_sso_runs_and_compares_wait_for_the_spec_version_s_approval(
    open_client: Callable[..., AbstractContextManager[TestClient]], callers: Any, spec_yaml: str
) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        alice = callers(client).session(*expand(["spec_author"]))
        spec_id = client.post("/v1/specs", content=spec_yaml, headers=alice).json()["spec_id"]
        olga = callers(client).session(*expand(["operator"]), username="olga")
        run, compare = {"spec_id": spec_id, "mode": "batch"}, {"spec_id": spec_id, "key": RAW_KEY}
        refused = [
            client.post("/v1/runs", json=run, headers=olga),
            client.post("/v1/compare", json=compare, headers=olga),
        ]
        approval_id = approved(client, spec_id)  # alice uploaded as herself: one approver
        created = client.post("/v1/runs", json=run, headers=olga)
        started = client.get(f"/v1/runs/{created.json()['run_id']}", headers=olga).json()
    needs = f"spec {spec_id} (api-demo 1.0.0) needs a four-eyes approval of clear before it runs"
    assert [(r.status_code, r.json()["detail"]) for r in refused] == [(409, needs)] * 2
    assert (created.status_code, started["approval_id"]) == (202, approval_id)
    assert started["params"]["approval"] == {"needs": ["clear"], "mode": "oidc"}


def test_a_soak_post_keeps_its_settings(client: TestClient, spec_yaml: str) -> None:
    spec_id = client.post("/v1/specs", content=spec_yaml + "execution: {cycleInterval: 1h}\n").json()[
        "spec_id"
    ]
    created = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "soak", "soak": {"rolling": True}})
    run = client.get(f"/v1/runs/{created.json()['run_id']}").json()
    assert (run["mode"], run["params"]["soak"]) == ("soak", {"rolling": True})
    for soak in ({"rolling": "false"}, {"roling": True}):  # never coerced, never ignored
        refused = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "soak", "soak": soak})
        assert refused.status_code == 422


SAMPLED = (
    "source: {kind: hash_sample, method: bottom_k, salt: api-q4,"
    " strata: [{name: all, cel: 'true', buckets: [{label: Y, values: [true], targetSize: 10}]}],"
    " source: {kind: paginated_endpoint, target: svc-a, request: {method: GET, path: /list}, keyPath: $.id,"
    " pagination: {kind: cursor, itemsPath: $.items, cursorPath: $.next, cursorParam: c}}}"
)


def test_a_sampled_run_is_answered_at_once_and_its_refusals_by_their_status(
    client: TestClient, spec_yaml: str
) -> None:
    sampled = spec_yaml.replace("source: {kind: list, keys: [K12345678]}", SAMPLED)
    spec_id = client.post("/v1/specs", content=sampled).json()["spec_id"]
    created = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"})
    run = client.get(f"/v1/runs/{created.json()['run_id']}").json()
    assert (created.status_code, run["status"], run["progress"]["keys"], list(run["draw"])) == (
        202,
        "running",
        {},
        ["due"],
    )  # its sample is drawn by a worker
    refused = [
        client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch", "keys": [RAW_KEY]}),
        client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}),
    ]
    assert [(r.status_code, r.json()["detail"]) for r in refused] == [
        (422, "a hash sample draws its keys from the frame: run it without --keys"),
        (
            409,
            f"spec api-demo samples the population of live sampled run {run['run_id']} (spec api-demo,"
            " listing:svc-a:GET /list): stop it before drawing another sample",
        ),
    ]


def test_runs_page_by_cursor(client: TestClient, seed: Any, seeded: Any) -> None:
    newer = seed(client)
    first = client.get("/v1/runs", params={"limit": 1}).json()
    rest = client.get("/v1/runs", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [r["run_id"] for r in first["items"] + rest["items"]] == [newer.run_id, seeded.run_id]
    assert rest["next_cursor"] is None
    malformed = client.get(f"/v1/runs/{seeded.run_id}/findings", params={"cursor": "x"})
    assert (malformed.status_code, malformed.json()) == (400, {"detail": "malformed cursor"})


def test_a_run_shows_its_progress(client: TestClient, seeded: Any) -> None:
    run = client.get(f"/v1/runs/{seeded.run_id}").json()
    assert (run["status"], run["spec_name"]) == ("running", "api-demo")
    assert run["progress"] == {"keys": {"done": 1, "pending": 1}, "outcomes": {"MISMATCH": 1}}
    assert client.get(f"/v1/runs/{RUN_ID}").status_code == 404


def test_run_views_name_keys_by_their_masked_form_only(client: TestClient, seeded: Any) -> None:
    base = f"/v1/runs/{seeded.run_id}"
    paths = ("", "/results", "/findings", "/root-causes", "/changes", f"/keys/{seeded.key}")
    views = [client.get(base + path) for path in paths]
    assert [v.status_code for v in views] == [200] * len(paths)
    assert not [v.request.url for v in views if seeded.raw_key in v.text]
    results, findings, causes = (views[i].json()["items"] for i in (1, 2, 3))
    assert [(r["key_masked"], r["outcome"]) for r in results] == [(seeded.key, "MISMATCH")]
    assert [(f["key_masked"], f["json_pointer"], f["finding_id"][:2]) for f in findings] == [
        (seeded.key, "/price", "F-")
    ]
    assert [c["root_cause_id"][:3] for c in causes] == ["RC-"]
    assert client.get(base + "/keys/" + seeded.raw_key).status_code == 404  # raw keys address nothing


def test_a_run_shows_its_scan_but_not_the_raw_key_it_resumes_after(client: TestClient, seeded: Any) -> None:
    counts = {"rows_a": 2, "rows_b": 2, "skipped_a": 0, "skipped_b": 0, "matched": 1, "candidates": 0}
    scan = {"done": False, "after": [seeded.raw_key], **counts}

    async def store(ctx: Any) -> None:
        async with ctx.pool.connection() as conn:
            insert = "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 1, %s::jsonb)"
            await conn.execute(insert, (seeded.run_id, json.dumps(scan)))

    client.portal.call(store, client.app.state.ctx)
    run, listed = client.get(f"/v1/runs/{seeded.run_id}"), client.get("/v1/runs")
    assert seeded.raw_key not in run.text + listed.text
    assert run.json()["scan"] == listed.json()["items"][0]["scan"] == {"done": False, **counts}


def test_run_lists_and_pages_show_no_guard(client: TestClient, seeded: Any) -> None:
    """What the schema guard holds of a run's tables (names and declared types)
    is in no run row an answer shows."""

    async def record(ctx: Any) -> None:
        shape = {"kind": "table", "key": ["id"], "columns": {"iban": ["character varying(34)", "text", True]}}
        side = {"relation": "public.customers", "roles": {"id": "key", "iban": "compared"}, "common": True}
        side |= {"planned": shape, "sha256": "1" * 64, "seen": None, "seen_sha256": "1" * 64, "changes": []}
        checked = {"cycle": 1, "at": "2026-10-07T10:02:11.000000Z", "where": "creation", "error_type": None}
        guard = {
            "format": 1,
            "mode": "finding",
            "baseline": True,
            "a": side,
            "checked": checked | {"side": None},
        }
        guard |= dict.fromkeys(("basis", "adopted", "since", "accepted"))
        async with ctx.pool.connection() as conn:
            assert await repo.set_guard(conn, seeded.run_id, guard)

    client.portal.call(record, client.app.state.ctx)
    page, listed = client.get(f"/v1/runs/{seeded.run_id}"), client.get("/v1/runs")
    assert (page.status_code, listed.status_code) == (200, 200)
    assert "guard" not in page.json() and "guard" not in listed.json()["items"][0]
    assert "character varying" not in page.text + listed.text and "iban" not in page.text + listed.text
    # The run's page prints the guard's lines, built before the row is shown; a list prints none.
    assert page.json()["schema"]["guarded"] and "schema" not in listed.json()["items"][0]
    assert "no change observed against the run's plan" in page.json()["schema"]["lines"][0]
    assert "unchanged" not in page.text


CUSTOMERS = "(id int PRIMARY KEY, email text NOT NULL, amount numeric(12,2), legacy_flag boolean)"
UNCHANGED = (
    "no change observed against the run's plan at its creation and each cycle's start (table sides only; a"
    " view's own tables, constraints and defaults are not read)"
)
FIRST = "no earlier run of this version recorded its tables' shapes: this run records them"


def test_a_run_s_schema_block_its_warnings_and_what_a_resume_accepts(
    open_client: Callable[..., AbstractContextManager[TestClient]], tmp_path: Any, pg_dsn: str
) -> None:
    """``GET /v1/runs/{id}`` prints the schema guard's lines and never its
    record; ``POST /v1/runs`` and ``POST /v1/compare`` answer with ``warnings``; a run created paused
    resumes by accepting its tables, is refused while a change that breaks its plan stands (409), and
    a spec that pauses on schema drift refuses a narrowed run (422)."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for name in ("customers", "customers_new"):
            conn.execute(f"CREATE TABLE {name} {CUSTOMERS}")  # type: ignore[arg-type]
            conn.execute(f"INSERT INTO {name} VALUES (1, 'a@x', 10.50, true)")  # type: ignore[arg-type] # noqa: S608
    entry = f"    dsn_ref: file:{tmp_path / 'dsn'}\n    rate_per_s: 1000\n"
    with open_client(targets=TARGETS + f"  db-a:\n{entry}  db-b:\n{entry}") as client:
        ids = [
            client.post("/v1/specs", content=table_spec("customers", b={"name": "customers_new"}, **members))
            for members in ({}, {"version": "1.1.0", "schemaDrift": {"mode": "pause"}})
        ]
        finding, pausing = (answer.json()["spec_id"] for answer in ids)
        created = [
            client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}) for spec_id in ids_of(ids)
        ]
        assert [(answer.status_code, answer.json()["warnings"]) for answer in created] == [(202, [])] * 2
        first, second = (client.get(f"/v1/runs/{answer.json()['run_id']}").json() for answer in created)
        assert first["schema"] == {
            "guarded": True, "mode": "finding", "basis": None, "checked": first["schema"]["checked"],
            "paused": False, "accepts": False,
            "lines": [UNCHANGED, FIRST],
        }  # fmt: skip
        assert first["schema"]["checked"]["where"] == "creation" and "guard" not in first
        assert "unchanged" not in json.dumps(first["schema"])  # it never says the schema is unchanged
        assert client.post("/v1/compare", json={"spec_id": finding, "key": "1"}).json()["warnings"] == []

        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute("ALTER TABLE customers_new DROP COLUMN amount")
        lost = (
            f"column 'amount' was compared by run {first['run_id']} of this version and is not compared now"
        )
        compared = client.post("/v1/compare", json={"spec_id": finding, "key": "1"})
        assert (compared.status_code, compared.json()["warnings"]) == (200, [lost])
        refused = client.post("/v1/runs", json={"spec_id": pausing, "mode": "batch"})
        assert said(refused) == (
            422,
            f"spec {pausing} (customers 1.1.0) pauses on schema drift and a run of it would not compare what"
            f" run {second['run_id']} compared: {lost.replace(first['run_id'], second['run_id'])}. Upload a"
            " new version to compare the tables as they are.",
        )
        paused = client.post("/v1/runs", json={"spec_id": finding, "mode": "batch"})
        line = (
            f"Paused by the schema guard before any table row was read: {lost}. Resume to accept the tables"
            " as they are, or cancel."
        )
        assert (paused.status_code, paused.json()["warnings"]) == (202, [line])
        run_id = paused.json()["run_id"]
        page = client.get(f"/v1/runs/{run_id}").json()
        assert (page["status"], page["stop_reason"]) == ("paused", "run_paused_schema_drift")
        schema = page["schema"]
        assert (schema["paused"], schema["accepts"], schema["basis"]) == (True, True, first["run_id"])
        assert schema["lines"] == [line] and "guard" not in page

        with psycopg.connect(pg_dsn, autocommit=True) as conn:  # a column its own plan reads goes too
            conn.execute("ALTER TABLE customers_new DROP COLUMN email")
        assert said(client.post(f"/v1/runs/{run_id}/resume")) == (
            409,
            f"run {run_id} cannot resume: side b: column 'email' of public.customers_new is gone, or the role"
            " may no longer read it",
        )
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute("ALTER TABLE customers_new ADD COLUMN email text NOT NULL DEFAULT 'a@x'")
        resumed = client.post(f"/v1/runs/{run_id}/resume")
        assert (resumed.status_code, resumed.json()["status"]) == (200, "running")
        done = client.get(f"/v1/runs/{run_id}").json()
        assert (done["status"], done["stop_reason"]) == ("running", None) and not done["schema"]["accepts"]
        assert done["schema"]["lines"][0] == lost and "was accepted by" in done["schema"]["lines"][1]
        rows = audited(client, "running")
        assert rows[-1][1] == run_id and set(rows[-1][2]["schema"]) == {"accepted"}


def ids_of(answers: list[Any]) -> list[int]:
    return [answer.json()["spec_id"] for answer in answers]


def test_results_and_findings_filter(client: TestClient, seeded: Any) -> None:
    def count(path: str, **params: str) -> int:
        return len(client.get(f"/v1/runs/{seeded.run_id}/{path}", params=params).json()["items"])

    assert [count("results", outcome=o) for o in ("MISMATCH", "MATCH")] == [1, 0]
    assert [count("findings", key=k) for k in (seeded.key, seeded.other_key)] == [1, 0]
    assert [count("findings", severity=s) for s in ("MEDIUM", "HIGH")] == [1, 0]
    assert client.get(f"/v1/runs/{seeded.run_id}/results", params={"outcome": "SIMILAR"}).status_code == 422


def _matched_in_cycle_2(client: TestClient, seeded: Any) -> None:
    """A second soak cycle in which the seeded MISMATCH key matches."""

    async def store(ctx: Any) -> None:
        complete = SideState(SideStateKind.COMPLETE)
        async with ctx.pool.connection() as conn:
            await repo.start_next_cycle(conn, seeded.run_id, datetime.now(UTC))
            result = KeyResult(seeded.raw_key, Outcome.MATCH, complete, complete)
            await repo.save_key_result(conn, seeded.run_id, 2, seeded.key, result, [])

    client.portal.call(store, client.app.state.ctx)


def test_soak_changes_name_keys_by_their_masked_form(client: TestClient, seeded: Any) -> None:
    _matched_in_cycle_2(client, seeded)
    changes = client.get(f"/v1/runs/{seeded.run_id}/changes")
    assert changes.json()["items"] == [
        {"cycle": 2, "key_masked": seeded.key, "from_outcome": "MISMATCH", "to_outcome": "MATCH"}
    ]


def test_cycle_selects_each_cycles_data(client: TestClient, seeded: Any) -> None:
    _matched_in_cycle_2(client, seeded)
    base = f"/v1/runs/{seeded.run_id}"

    def at(cycle: int) -> tuple[Any, ...]:
        get = lambda path: client.get(base + path, params={"cycle": cycle})  # noqa: E731
        return (
            [r["outcome"] for r in get("/results").json()["items"]],
            len(get("/findings").json()["items"]),
            len(get("/root-causes").json()["items"]),
            get(f"/keys/{seeded.key}").json()["result"]["outcome"],
            seeded.key in get("/reports/discrepancies.csv").text,
        )

    assert (at(1), at(2)) == ((["MISMATCH"], 1, 1, "MISMATCH", True), (["MATCH"], 0, 0, "MATCH", False))


def test_graph_rows_page_by_cycle_and_cursor(client: TestClient, seeded: Any) -> None:
    rows = [{"code": "GROUP_MEMBERS_DIFFER", "label": f"tok:g{i}", "count_a": 2, "count_b": 1, "only_a": 1,
             "only_b": 0, "detail": {"only_a": ["tok:m1"], "only_b": []}} for i in range(3)]  # fmt: skip

    async def store(ctx: Any) -> None:
        async with ctx.pool.connection() as conn:
            await repo.replace_graph_diffs(conn, seeded.run_id, 1, rows)

    client.portal.call(store, client.app.state.ctx)
    base = f"/v1/runs/{seeded.run_id}/graph"
    first = client.get(base, params={"limit": 2}).json()
    rest = client.get(base, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert first["items"] + rest["items"] == [{"seq": i} | row for i, row in enumerate(rows)]
    assert rest["next_cursor"] is None
    assert client.get(base, params={"cycle": 2}).json() == {"items": [], "next_cursor": None}
    assert client.get(f"/v1/runs/{RUN_ID}/graph").status_code == 404


def test_key_detail_shows_masked_canonical_documents_with_exact_numbers(
    client: TestClient, seeded: Any
) -> None:
    compared = client.get(f"/v1/runs/{seeded.run_id}/keys/{seeded.key}")
    detail = compared.json()
    assert (detail["key_masked"], detail["result"]["outcome"]) == (seeded.key, "MISMATCH")
    assert [f["json_pointer"] for f in detail["findings"]] == ["/price"]
    assert sorted(o["side"] for o in detail["observations"]) == ["a", "b"]
    a, b = detail["canonical"]["a"], detail["canonical"]["b"]
    assert (a["id"][:4], b["price"]) == ("tok:", 2.5)  # the id is tokenised, the price clear
    assert '"price": 1.00000000000000000001' in compared.text
    pending = client.get(f"/v1/runs/{seeded.run_id}/keys/{seeded.other_key}").json()
    assert (pending["result"], pending["canonical"]) == (None, {"a": None, "b": None})


def test_a_key_view_needing_a_kek_the_keyring_lacks_is_a_503(client: TestClient, seeded: Any) -> None:
    lost = "K-" + "0" * 16

    async def lose(ctx: Any) -> None:  # the run's data key as if wrapped by a KEK now lost
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE ds.data_keys SET kek = %s WHERE scope = %s", (lost, seeded.run_id))

    client.portal.call(lose, client.app.state.ctx)
    reply = client.get(f"/v1/runs/{seeded.run_id}/keys/{seeded.key}")
    assert (reply.status_code, reply.json()) == (
        503,
        {"detail": f"evidence unreadable: the keyring lacks {lost}"},
    )
    assert client.get(f"/v1/runs/{seeded.run_id}/results").status_code == 200  # masked data needs no key


def test_a_missing_blob_of_a_live_cycle_is_a_410(client: TestClient, seeded: Any) -> None:
    async def lose(ctx: Any) -> str:  # a blob lost, or deleted by hand
        async with ctx.pool.connection() as conn:
            [row] = [o for o in await repo.list_observations(conn, seeded.run_id) if o["side"] == "a"]
        digest = row["content_hash"].removeprefix("hk1:")
        (ctx.settings.evidence_dir / "blobs" / "hk1" / digest[:2] / f"{digest}.dse").unlink()
        return row["obs_id"]

    obs_id = client.portal.call(lose, client.app.state.ctx)
    reply = client.get(f"/v1/runs/{seeded.run_id}/keys/{seeded.key}")
    assert (reply.status_code, reply.json()) == (
        410,
        {"detail": f"evidence of {obs_id} is missing (purged or lost)"},
    )
    assert client.get(f"/v1/runs/{seeded.run_id}/results").status_code == 200


def _purged(client: TestClient, seeded: Any, *, hold: str | None = None) -> repo.Row:
    """End the seeded run (a cancel), date its cycle 91 days back and run a purge pass; its cycle row."""
    from deltasleuth.runner import purge
    from tests.conftest import TESTER

    assert client.post(f"/v1/runs/{seeded.run_id}/cancel").json()["status"] == "cancelled"

    async def purged(ctx: Any) -> repo.Row:
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE ds.cycles SET ended_at = now() - interval '91 days' WHERE run_id = %s",
                (seeded.run_id,),
            )
        if hold is not None:
            await purge.hold(ctx, TESTER, seeded.run_id, hold)
        await purge.purge(ctx, TESTER)
        async with ctx.pool.connection() as conn:
            return await repo.get_cycle(conn, seeded.run_id, 1)  # type: ignore[return-value]

    return client.portal.call(purged, client.app.state.ctx)


def test_a_run_shows_its_retention(client: TestClient, seeded: Any) -> None:
    base = f"/v1/runs/{seeded.run_id}"
    live = client.get(base).json()["retention"]
    assert live == {"class": "standard", "days": 90, "hold": None, "purged": [], "last_purged_at": None,
                    "cycles": []}  # fmt: skip
    ended = _purged(client, seeded, hold="legal hold LH-7")
    held = client.get(base).json()["retention"]
    assert (held["hold"]["reason"], held["hold"]["by"], held["purged"]) == (
        "legal hold LH-7",
        "local:tester@test",
        [],
    )
    assert held["cycles"] == [
        {
            "cycle": 1,
            "ended_at": ended["ended_at"].isoformat(),
            "expires_at": (ended["ended_at"] + timedelta(days=90)).isoformat(),
        }
    ]


def test_a_purged_cycle_answers_410_on_its_routes(client: TestClient, seeded: Any) -> None:
    ended = _purged(client, seeded)
    base = f"/v1/runs/{seeded.run_id}"
    gone = (
        f"cycle 1 of run {seeded.run_id} was purged on {ended['purging_at']:%Y-%m-%d} (retention standard):"
        " its keys, results, findings and evidence are deleted"
    )
    for path in ("results", "findings", "root-causes", "graph", f"keys/{seeded.key}", "reports/report.pdf",
                 "reports/discrepancies.csv", "results?cycle=1"):  # fmt: skip
        reply = client.get(f"{base}/{path}")
        assert (reply.status_code, reply.json()) == (410, {"detail": gone}), path
    compared = client.post(
        "/v1/compare", json={"spec_id": seeded.spec_id, "key": seeded.key, "run_id": seeded.run_id}
    )
    assert (compared.status_code, compared.json()) == (410, {"detail": gone})
    run = client.get(base).json()
    assert (run["status"], run["retention"]["purged"], run["retention"]["cycles"]) == ("cancelled", [1], [])
    assert run["retention"]["last_purged_at"] == ended["purged_at"].isoformat()
    assert client.get(f"{base}/changes").json() == {"items": []}  # across cycles: what is left
    assert client.get(f"{base}/results?cycle=2").status_code == 200  # no such cycle: nothing purged


def test_pause_resume_and_cancel_go_through_the_runner(client: TestClient, seeded: Any) -> None:
    base = f"/v1/runs/{seeded.run_id}"
    moves = [client.post(f"{base}/{action}").json()["status"] for action in ("pause", "resume", "cancel")]
    assert moves == ["paused", "running", "cancelled"]  # no key was in flight
    assert client.get(base).json()["progress"]["keys"] == {"done": 1, "cancelled": 1}
    refused = client.post(base + "/resume")
    assert (refused.status_code, refused.json()) == (409, {"detail": "cannot resume a run that is cancelled"})
    assert client.post(f"/v1/runs/{RUN_ID}/pause").status_code == 404
    assert client.post(base + "/restart").status_code == 422


def test_every_report_format_downloads(client: TestClient, seeded: Any) -> None:
    base = f"/v1/runs/{seeded.run_id}/reports/"
    for name, (media, _) in FORMATS.items():
        got = client.get(base + name)
        assert (got.status_code, got.headers["content-type"].split(";")[0]) == (200, media), name
        assert got.headers["content-disposition"] == f'attachment; filename="{seeded.run_id}-{name}"'
        assert seeded.raw_key.encode() not in got.content
    assert seeded.key in client.get(base + "discrepancies.csv").text
    assert client.get(base + "report.pdf").content[:5] == b"%PDF-"
    assert [client.get(base + name).status_code for name in ("keys.csv", "report.txt")] == [404, 404]
    assert client.get(f"/v1/runs/{RUN_ID}/reports/report.pdf").status_code == 404


def test_a_run_stays_readable_after_its_targets_are_removed(open_client: Any, seed: Any) -> None:
    with open_client() as client:
        seeded = seed(client)
    with open_client(targets="targets:\n  svc-z:\n    base_url: https://z.example.com\n") as client:
        base = f"/v1/runs/{seeded.run_id}"
        paths = (f"/keys/{seeded.key}", "/reports/report.pdf", "/reports/discrepancies.csv")
        assert [client.get(base + path).status_code for path in paths] == [200, 200, 200]
        assert client.get(base + f"/keys/{seeded.key}").json()["canonical"]["b"]["price"] == 2.5


def test_compare_runs_one_key_now_and_answers_with_its_key_view(client: TestClient, seeded: Any) -> None:
    now = client.post("/v1/compare", json={"spec_id": seeded.spec_id, "key": seeded.raw_key})
    view = now.json()
    assert (view["key_masked"], view["result"]["outcome"], view["result"]["confirmation"]) == (
        seeded.key,
        "MISMATCH",
        "rechecked",
    )
    assert view["run_id"] != seeded.run_id and view["canonical"]["b"]["price"] == 2.5
    assert seeded.raw_key not in now.text
    # The key screen knows a key by its masked form only: its run resolves it.
    by_run = {"spec_id": seeded.spec_id, "key": seeded.key, "run_id": seeded.run_id}
    assert client.post("/v1/compare", json=by_run).json()["key_masked"] == seeded.key
    refused = [
        client.post("/v1/compare", json={"spec_id": 999, "key": seeded.raw_key}),
        client.post("/v1/compare", json={"spec_id": seeded.spec_id, "key": "not-a-key"}),
    ]
    assert [r.status_code for r in refused] == [404, 422]


def test_compare_by_masked_key_refuses_another_spec(client: TestClient, seeded: Any, spec_yaml: str) -> None:
    clear = spec_yaml.replace("class: identifier", "class: business_code").replace('"1.0.0"', '"1.0.1"')
    other = client.post("/v1/specs", content=clear).json()["spec_id"]
    by_run = {"spec_id": other, "key": seeded.key, "run_id": seeded.run_id}
    refused = client.post("/v1/compare", json=by_run)
    assert (refused.status_code, refused.json()) == (
        422,
        {"detail": "a key of a run is compared under that run's spec"},
    )


def test_a_file_side_s_root_pins_and_digest_are_in_no_answer(
    open_client: Callable[..., AbstractContextManager[TestClient]], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """GET /v1/targets names a files target's engine, never its root; a run's
    answers carry its file side's plan (counts, bytes, times) but not the digest of its files nor its sealed
    pins, on the list route too; a refused plan is a 422 with the plan's text."""
    exports = tmp_path_factory.mktemp("exports").resolve()
    for side in "ab":
        (exports / side).mkdir()
        (exports / side / "part-canary.csv").write_text("id|v\n1|a\n2|b\n")
    declared = TARGETS + "".join(
        f"  f{side}:\n    engine: files\n    root: {exports / side}\n    settle_s: 0\n"
        "    allow_writable: true\n"
        for side in "ab"
    )
    dialect = {"columns": ["id", "v"], "delimiter": "|", "header": True}
    table = {"name": "t", "keyColumns": ["id"], "file": {"path": "*.csv", "format": "csv", "csv": dialect}}
    doc = {
        "apiVersion": "deltasleuth/v1", "kind": "ComparisonSpec",
        "metadata": {"name": "exports", "version": "1.0.0", "owner": "tests"}, "normalizerVersion": "1.1.0",
        "key": {"name": "id", "type": "string", "class": "business_code"},
        "sides": {
            "a": {"role": "legacy", "target": "fa", "table": table},
            "b": {"role": "new", "target": "fb", "table": table},
        },
        "compare": {}, "recheck": {"delay": "0s", "times": 1}, "security": {"unlisted": "clear"},
    }  # fmt: skip
    absent = json.loads(json.dumps(doc))
    absent["metadata"]["version"] = "1.1.0"
    absent["sides"]["a"]["table"]["file"]["path"] = "none/*.csv"
    with open_client(targets=declared, files={"roots_under": str(exports)}) as client:
        targets = client.get("/v1/targets").json()
        spec_id = client.post("/v1/specs", content=yaml.safe_dump(doc)).json()["spec_id"]
        created = client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"})
        run = client.get(f"/v1/runs/{created.json()['run_id']}").json()
        listed = client.get("/v1/runs").json()["items"][0]
        other = client.post("/v1/specs", content=yaml.safe_dump(absent)).json()["spec_id"]
        refused = client.post("/v1/runs", json={"spec_id": other, "mode": "batch"})
    assert {t["target_id"]: t["engine"] for t in targets["items"]} == {
        "fa": "files", "fb": "files", "svc-a": None, "svc-b": None,
    }  # fmt: skip
    assert str(exports) not in str(targets) and "root" not in str(targets)
    assert created.status_code == 202
    for answer in (run, listed):
        side = answer["params"]["tables"]["a"]
        assert "pinned" not in answer["params"] and "files_sha256" not in side
        assert (side["engine"], side["file_count"], side["bytes"], side["rows"]) == ("files", 1, 13, 2)
        assert not [
            text for text in ("canary", str(exports), "sha256", '"enc"') if text in json.dumps(answer)
        ]
    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        "side a: no file matches none/*.csv under target fa",
    )
