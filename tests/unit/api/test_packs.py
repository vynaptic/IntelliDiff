"""Evidence packs over the API (docs/mvp/PACK.md §6, §7.1, §7.2): a person issues a signed-off cycle's pack,
a viewer lists, reads and downloads it (audited first) and the signed revocation list, and the download
verifies away from the host; each route's 409 outside SSO before its 503, the 403 for a token and the
refusals joined; the run's cycles carrying their census, adjudications and packs; a purge keeping the
sign-off and the pack record but not the ZIP (410); a sign-off revoked in SQL, or by a cascade from its
spec approval, revoking its packs."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth.context import Context
from deltasleuth.reports import pack_verify
from deltasleuth.runner import purge
from deltasleuth.store import repo
from tests.conftest import TESTER, create_run
from tests.unit.reports.packs import listed_spec, samples

from .conftest import OIDC, audited, said, session_as, signed

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
NOT_CONFIGURED = "evidence packs are not configured: set packs.signing_key_ref (docs/mvp/PACK.md §3.1)"


def sso_only(mode: str) -> str:
    return f"evidence packs need SSO (auth.oidc); this deployment runs in {mode}"


@pytest.fixture
def client(packs_client: TestClient) -> TestClient:
    return packs_client


def test_a_person_issues_a_pack_that_a_viewer_reads_downloads_and_verifies_away_from_the_host(
    client: TestClient, tmp_path: Path
) -> None:
    cycle = signed(client, pairs=samples(52), adjudicate=True)
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    issued = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga)
    assert issued.status_code == 201, issued.text
    pack_id = issued.json()["pack_id"]
    signer = client.app.state.ctx.signer
    listed = client.get("/v1/packs", headers=vic).json()
    by_run = client.get("/v1/packs", params={"run_id": cycle.run_id, "status": "issued"}, headers=vic).json()
    revoked = client.get("/v1/packs", params={"status": "revoked"}, headers=vic).json()
    record = client.get(f"/v1/packs/{pack_id}", headers=vic).json()
    download = client.get(f"/v1/packs/{pack_id}/download", headers=vic)
    revocations = client.get("/v1/packs/revocations", headers=vic)
    run = client.get(f"/v1/runs/{cycle.run_id}", headers=vic).json()
    assert issued.json() == {
        "pack_id": pack_id,
        "sums_sha256": record["sums_sha256"],
        "key_fingerprint": signer.fingerprint,
    }
    assert [p["pack_id"] for p in listed["items"]] == [p["pack_id"] for p in by_run["items"]] == [pack_id]
    assert "manifest" not in listed["items"][0] and listed["next_cursor"] is None and revoked["items"] == []
    assert (record["status"], record["issued_by"], record["signoff_id"]) == (
        "issued",
        cycle.people["olga"].actor,
        cycle.signoff_id,
    )
    assert record["manifest"]["pack_id"] == pack_id and record["manifest"]["claim"] == "observed"
    assert (download.status_code, download.headers["content-type"], download.headers["cache-control"]) == (
        200,
        "application/zip",
        "no-store",
    )
    assert download.headers["content-disposition"] == f'attachment; filename="{pack_id}.zip"'
    assert hashlib.sha256(download.content).hexdigest() == record["zip_sha256"]
    vic_actor = client.get("/v1/auth/me", headers=vic).json()["actor"]
    assert audited(client, "pack.issue")[0][:2] == (cycle.people["olga"].actor, pack_id)
    assert audited(client, "pack.download") == [(vic_actor, pack_id, {"run_id": cycle.run_id, "cycle": 1})]
    # The download, checked away from the host with the register's fingerprint and the custodian's list.
    (tmp_path / f"{pack_id}.zip").write_bytes(download.content)
    with zipfile.ZipFile(io.BytesIO(revocations.content)) as zf:
        assert sorted(zf.namelist()) == ["revoked.txt", "revoked.txt.sig", "signing-key.pem"]
        zf.extractall(tmp_path / "custodian")
    listing = (tmp_path / "custodian" / "revoked.txt").read_text().splitlines()
    assert listing[0].startswith(f"# DeltaSleuth pack revocations, key {signer.fingerprint}, as of ")
    assert listing[1:] == ["# end"]
    steps = pack_verify.verify(tmp_path / f"{pack_id}.zip", signer.fingerprint, tmp_path / "custodian")
    assert [s.number for s in steps if s.ok is False] == [] and steps[-1].ok is True
    # The run's ended cycle carries its census, its adjudication (without its sheet) and its pack.
    [ended] = run["cycles"]
    assert ended["cycle"] == 1 and run["census_pending"] == []
    assert ended["census"]["a"]["basis"] == "listing" and ended["census"]["a"]["items"] == 52
    assert [(j["adjudication_id"], j["passed"]) for j in ended["adjudications"]] == [
        (cycle.adjudication_id, True)
    ]
    assert "sheet" not in ended["adjudications"][0]
    assert [(p["pack_id"], p["status"]) for p in ended["packs"]] == [(pack_id, "issued")]


async def _key_list_run(ctx: Context) -> str:
    """A running run of given keys whose side a is a table: its cycle awaits the worker's key census."""
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, listed_spec(), created_by=TESTER.actor)
        table = {"schema": "legacy", "name": "accounts", "keys": ["id"], "columns": {}}
        params = {"keys_source": "explicit", "tables": {"a": table}}
        run_id, _ = await create_run(ctx, conn, spec_id, "batch", {"K001": "K001"}, params)
        await repo.set_run_status(conn, run_id, "running")
    return run_id


async def _census_failed(ctx: Context, run_id: str) -> None:
    async with ctx.pool.connection() as conn:
        census = {"cycle": 1, "at": "2026-10-01T08:00:00.000000Z", "a": {"failed": "QueryCanceled"}}
        assert await repo.set_census(conn, run_id, census)


def test_a_live_run_shows_its_census_pending_then_failed(client: TestClient) -> None:
    run_id = client.portal.call(_key_list_run, client.app.state.ctx)
    vic = session_as(client, "vic", "viewer")
    pending = client.get(f"/v1/runs/{run_id}", headers=vic).json()
    client.portal.call(_census_failed, client.app.state.ctx, run_id)
    failed = client.get(f"/v1/runs/{run_id}", headers=vic).json()
    assert (pending["census_pending"], pending["census"], pending["cycles"]) == (["a"], None, [])
    assert (failed["census_pending"], failed["census"]["a"]) == ([], {"failed": "QueryCanceled"})


def test_pack_and_adjudication_routes_answer_409_outside_sso_first_then_503_and_403(
    open_client: OpenClient, callers: Any, pack_key: Path
) -> None:
    routes = [
        ("POST", "/v1/runs/R-x/cycles/1/packs"),
        ("GET", "/v1/packs"),
        ("GET", "/v1/packs/revocations"),
        ("GET", "/v1/packs/P-x"),
        ("GET", "/v1/packs/P-x/download"),
        ("POST", "/v1/runs/R-x/cycles/1/adjudications"),
        ("GET", "/v1/runs/R-x/cycles/1/adjudications"),
        ("GET", "/v1/adjudications/J-x/sheet.csv"),
        ("POST", "/v1/adjudications/J-x/score"),
    ]
    got = {}
    with open_client(auth={}) as client:  # tokens mode
        operator = callers(client).token("operator")
        got["tokens"] = [said(client.request(m, path, headers=operator)) for m, path in routes]
    with open_client(auth={"oidc": OIDC}) as client:  # no pack settings
        person = session_as(client, "olga", "operator")
        got["unconfigured"] = [said(client.request(m, path, headers=person)) for m, path in routes]
    packs = {"installation": "test-1", "signing_key_ref": f"file:{pack_key}"}
    with open_client(auth={"oidc": OIDC}, packs=packs) as client:
        token = callers(client).token("operator")
        got["token"] = [said(client.post("/v1/runs/R-x/cycles/1/packs", headers=token))]
    with open_client() as client:  # developer mode (its workspace keys last)
        got["developer"] = [said(client.request(m, path)) for m, path in routes]
    assert got == {
        "tokens": [(409, sso_only("tokens mode"))] * len(routes),
        "developer": [(409, sso_only("developer mode"))] * len(routes),
        "unconfigured": [(503, NOT_CONFIGURED)] * 5  # adjudications need no signing key
        + [
            (404, "run R-x not found"),
            (404, "run R-x not found"),
            (404, "adjudication J-x not found"),
            (404, "adjudication J-x not found"),
        ],
        "token": [(403, "issuing a pack needs a signed-in person")],
    }


def test_an_issue_is_refused_with_every_reason_and_unknown_packs_are_not_found(client: TestClient) -> None:
    unsigned = signed(client, sign=False)
    other = listed_spec(metadata={"name": "unit-reports", "version": "1.0.1", "owner": "tests"})
    unadjudicated = signed(client, spec=other, pairs=samples(52))
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    replies = [
        client.post(f"/v1/runs/{unsigned.run_id}/cycles/1/packs", headers=olga),
        client.post(f"/v1/runs/{unadjudicated.run_id}/cycles/1/packs", headers=olga),
        client.post("/v1/runs/R-x/cycles/1/packs", headers=olga),
        client.get("/v1/packs/P-x", headers=vic),
        client.get("/v1/packs/P-x/download", headers=vic),
        client.get("/v1/packs", params={"status": "lost"}, headers=vic),
    ]
    assert [said(r) for r in replies[:5]] == [
        (409, f"cycle 1 of run {unsigned.run_id} has no approved sign-off"),
        (
            409,
            f"cycle 1 of run {unadjudicated.run_id} has no scored adjudication in its sign-off"
            f" {unadjudicated.signoff_id}: revoke it, draw and score a reviewer sheet, then request the"
            " sign-off again",
        ),
        (404, "run R-x not found"),
        (404, "pack P-x not found"),
        (404, "pack P-x not found"),
    ]
    assert replies[5].status_code == 422
    assert audited(client, "pack.issue") == audited(client, "pack.download") == []


async def _expired(ctx: Context, run_id: str) -> None:
    await purge.hold(ctx, TESTER, run_id, None)  # ds purge release: the sign-off's hold is lifted
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.cycles SET ended_at = ended_at - interval '91 days' WHERE run_id = %s", (run_id,)
        )
    await purge.purge(ctx, TESTER)


def test_a_purge_keeps_the_sign_off_and_the_pack_record_and_deletes_the_zip(client: TestClient) -> None:
    cycle = signed(client, pairs=samples(52), adjudicate=True)
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    pack_id = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga).json()["pack_id"]
    client.portal.call(_expired, client.app.state.ctx, cycle.run_id)
    record = client.get(f"/v1/packs/{pack_id}", headers=vic).json()
    signoff = client.get(f"/v1/approvals/{cycle.signoff_id}", headers=vic).json()
    run = client.get(f"/v1/runs/{cycle.run_id}", headers=vic).json()
    day = record["purged_at"][:10]
    gone = f"cycle 1 of run {cycle.run_id} was purged on {day} (retention standard): its keys, results,"
    gone += " findings and evidence are deleted"
    replies = [
        client.get(f"/v1/packs/{pack_id}/download", headers=vic),
        client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga),
        client.post(f"/v1/runs/{cycle.run_id}/cycles/1/adjudications", headers=olga),
        client.get(f"/v1/runs/{cycle.run_id}/cycles/1/adjudications", headers=vic),
        client.get(f"/v1/adjudications/{cycle.adjudication_id}/sheet.csv", headers=vic),
    ]
    assert (record["status"], signoff["status"]) == ("issued", "approved")  # the issued pack cites it
    assert [said(r) for r in replies] == [
        (
            410,
            f"pack {pack_id} was purged with cycle 1 on {day}: its stored copy is deleted; copies"
            " downloaded before still verify",
        ),
        *[(410, gone)] * 4,
    ]
    [ended] = run["cycles"]
    [tombstone] = ended["adjudications"]
    assert ended["census"] is None and ended["packs"][0]["purged_at"] == record["purged_at"]
    assert tombstone["selection"]["keys"] is None and tombstone["score"]["disagreements"] is None
    assert tombstone["purged_at"] is not None and tombstone["sheet_sha256"] is not None


async def _revoked_in_sql(ctx: Context, signoff_id: str) -> None:
    async with ctx.pool.connection() as conn:
        await conn.execute(
            """UPDATE ds.approvals SET status = 'revoked', revoked_by = 'local:dba@db', revoked_at = now(),
                   revoke_reason = 'revoked by hand' WHERE approval_id = %s""",
            (signoff_id,),
        )


def test_a_sign_off_revoked_in_sql_revokes_its_packs(client: TestClient) -> None:
    cycle = signed(client, pairs=samples(52), adjudicate=True)
    olga, vic = session_as(client, "olga", "operator"), session_as(client, "vic", "viewer")
    pack_id = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga).json()["pack_id"]
    client.portal.call(_revoked_in_sql, client.app.state.ctx, cycle.signoff_id)
    record = client.get(f"/v1/packs/{pack_id}", headers=vic).json()
    listed = client.get("/v1/packs", params={"status": "revoked"}, headers=vic).json()
    with zipfile.ZipFile(io.BytesIO(client.get("/v1/packs/revocations", headers=vic).content)) as zf:
        lines = zf.read("revoked.txt").decode().splitlines()
    again = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga)
    assert (record["status"], record["revoked_by"], record["revoke_reason"]) == (
        "revoked",
        "local:dba@db",
        "revoked by hand",
    )
    assert [p["pack_id"] for p in listed["items"]] == [pack_id]
    assert re.fullmatch(
        rf"{pack_id} {record['sums_sha256']} \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ {record['key_fingerprint']}",
        lines[1],
    )
    assert lines[2:] == ["# end"]
    assert said(again) == (409, f"cycle 1 of run {cycle.run_id} has no approved sign-off")


def test_revoking_the_spec_approval_revokes_the_sign_off_and_its_packs(client: TestClient) -> None:
    cycle = signed(client, pairs=samples(52), adjudicate=True)
    olga, carol = session_as(client, "olga", "operator"), session_as(client, "carol", "rule_approver")
    pack_id = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga).json()["pack_id"]
    done = client.post(
        f"/v1/approvals/{cycle.spec_approval_id}/revoke", json={"reason": "wrong clear path"}, headers=carol
    )
    record = client.get(f"/v1/packs/{pack_id}", headers=carol).json()
    why = f"spec approval {cycle.spec_approval_id} was revoked"
    assert done.status_code == 200 and done.json()["revoked"] == [cycle.signoff_id]
    assert (record["status"], record["revoked_by"], record["revoke_reason"]) == (
        "revoked",
        cycle.people["carol"].actor,
        why,
    )
    assert audited(client, "pack.revoke") == [
        (cycle.people["carol"].actor, pack_id, {"reason": why, "cascade_of": cycle.signoff_id})
    ]
