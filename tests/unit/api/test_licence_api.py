"""The licence over the API: GET /v1/licence, for every viewer and unaudited,
in each state and when the standing cannot be read; each refusal answers 409, at a run's creation, a
compare, the resume of a run paused for the licence, a sign-off's request and approval step and a pack's
issue; a pack is refused by the licence at issue or for a run made in evaluation, and carries
the licence it was issued under; a sign-off after the grace holds its cycle for a pack after renewal; runs
and their report downloads are labelled; and nothing that needs no licence is ever locked out."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltasleuth import licence
from deltasleuth.auth.principal import expand
from deltasleuth.connectors.tables import ENGINES
from deltasleuth.context import Context
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.reports import pack
from deltasleuth.reports.render import Document
from deltasleuth.store import repo
from tests.conftest import TESTER, create_run, licence_file, licensed, licensed_once, unlicensed
from tests.unit.api.test_roles import ROUTES
from tests.unit.auth.fake_idp import FakeIdp, sign_in
from tests.unit.reports.packs import listed_spec
from tests.unit.runner.prechecking import FakeEngine, relation, table_spec
from tests.unit.store.four_eyes import person

from .conftest import OIDC, RAW_KEY, SPEC_YAML, TARGETS, Callers, approved, audited, said, session_as, signed

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("vendor_keys")]  # a vendor build
OpenClient = Callable[..., AbstractContextManager[TestClient]]
M5 = (
    "under SSO this installation keeps serving sign-in, reads, reports, exports, pack downloads and"
    " administration, and new runs, compares and soak cycles need an Enterprise licence (ds licence install"
    " FILE); keep SSO, which evidence packs need"
)


def _packs(pack_key: Path) -> dict[str, str]:
    return {"installation": "test-1", "signing_key_ref": f"file:{pack_key}"}


async def _facts(ctx: Context) -> repo.Row:
    async with ctx.pool.connection() as conn:
        return await repo.licence_facts(conn)


async def _today(ctx: Context) -> date:
    return (await _facts(ctx))["now"].astimezone(UTC).date()


async def _ended(ctx: Context) -> licence.Licence:
    """An Enterprise licence whose grace ended yesterday, stored as ``repo.insert_licence`` stores one."""
    day = await _today(ctx)
    data = licence_file(
        (await _facts(ctx))["installation_id"],
        starts=day - timedelta(days=100),
        ends=day - timedelta(days=31),
    )
    found = licence.parse(data)
    async with ctx.pool.connection() as conn:
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=TESTER.actor)
    return found


async def _spec(ctx: Context) -> int:
    """The API's spec, a soak's cycle due hourly, stored as the tester's upload."""
    spec = compile_spec(
        parse_yaml_text(SPEC_YAML) | {"execution": {"cycleInterval": "1h"}}, targets=ctx.targets
    )
    async with ctx.pool.connection() as conn:
        return await repo.save_spec(conn, spec, created_by=TESTER.actor)


async def _paused(ctx: Context, spec_id: int) -> str:
    """A soak of the spec, started, that its boundary paused for the licence."""
    async with ctx.pool.connection() as conn:
        run_id, _ = await create_run(ctx, conn, spec_id, "soak", {"K001": "K001"}, {})
        await repo.set_run_status(conn, run_id, "running")
        await repo.set_run_status(conn, run_id, "paused", stop_reason=licence.LICENCE_PAUSE)
    return run_id


async def _runs(ctx: Context) -> int:
    async with ctx.pool.connection() as conn:
        return (await (await conn.execute("SELECT count(*) FROM ds.runs")).fetchone())[0]  # type: ignore[index]


def test_refusals_409(open_client: OpenClient, pack_key: Path) -> None:
    with open_client(auth={"oidc": OIDC}, packs=_packs(pack_key), tier=None) as client:
        ctx = client.app.state.ctx
        yesterday = client.portal.call(_today, ctx) - timedelta(days=1)
        client.portal.call(partial(licensed, ctx, starts=yesterday))  # Enterprise
        cycle = signed(client, sign=False)
        olga = session_as(client, "olga", "operator")
        erin = Callers(client).session(
            *expand(["rule_approver"]), username="erin", auth_time=datetime.now(UTC)
        )
        request = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/signoff", headers=olga)
        assert request.status_code == 201, request.text
        spec_id = client.portal.call(_spec, ctx)
        approved(client, spec_id)  # the four-eyes gate comes first
        paused = client.portal.call(_paused, ctx, spec_id)
        pro = client.portal.call(partial(licensed, ctx, tier="pro"))  # a downgrade, from today
        step = {"decision": "approve", "attests_sha256": request.json()["attests_sha256"]}
        replies = [
            client.post("/v1/runs", json={"spec_id": spec_id, "mode": "batch"}, headers=olga),
            client.post("/v1/compare", json={"spec_id": spec_id, "key": RAW_KEY}, headers=olga),
            client.post(f"/v1/runs/{paused}/resume", headers=olga),
            client.post(f"/v1/runs/{cycle.run_id}/cycles/1/signoff", headers=olga),
            client.post(f"/v1/approvals/{request.json()['approval_id']}/decision", json=step, headers=erin),
            client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga),
        ]
        runs = client.portal.call(_runs, ctx)
    m5 = f"SSO (auth.oidc) is an Enterprise feature and licence {pro.licence_id} is pro: {M5}"
    m6 = f"four-eyes sign-off is an Enterprise feature and licence {pro.licence_id} is pro"
    m7 = f"evidence packs are an Enterprise feature and licence {pro.licence_id} is pro"
    assert [said(reply) for reply in replies] == [
        (409, m5),
        (409, m5),
        (409, f"run {paused} cannot resume: {m5}"),
        (409, m6),
        (409, m6),
        (409, f"{m7}; cycle 1 of run {cycle.run_id} has no approved sign-off"),
    ]
    assert runs == 2  # the signed cycle's and the paused soak: nothing was written


@pytest.mark.parametrize("standing", ["expired", "pro", "made_in_evaluation", "evaluation"])
def test_packs_M4_M7_M8(open_client: OpenClient, pack_key: Path, standing: str) -> None:
    with open_client(auth={"oidc": OIDC}, packs=_packs(pack_key), tier=None) as client:
        ctx = client.app.state.ctx
        found = None
        if standing == "expired":
            found = client.portal.call(_ended, ctx)
        elif standing == "pro":
            found = client.portal.call(partial(licensed, ctx, tier="pro"))
        cycle = signed(
            client, tier=None if "evaluation" in standing else "enterprise"
        )  # else: the licence named
        if standing == "made_in_evaluation":
            client.portal.call(licensed, ctx)  # Enterprise, installed since
        answer = client.post(
            f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=session_as(client, "olga", "operator")
        )
        installation = client.portal.call(_facts, ctx)["installation_id"]
    m8 = (
        f"run {cycle.run_id} was made in an unlicensed evaluation: no evidence pack is issued for it; start a"
        " new run under the licence"
    )
    if standing == "expired":
        assert found is not None
        expected = (
            f"licence {found.licence_id} ended on {found.ends} and its 30 days of grace on"
            f" {found.ends + timedelta(days=30)}: new runs, compares, soak cycles and evidence packs"
            " are refused until a renewed licence is installed (ds licence install FILE); reads, reports,"
            " exports, holds, erasure, purge, audit, pack downloads and sign-offs under an Enterprise"
            " licence keep working"
        )
    elif standing == "pro":
        assert found is not None
        expected = f"evidence packs are an Enterprise feature and licence {found.licence_id} is pro"
    elif standing == "made_in_evaluation":
        expected = m8  # whatever licence is installed since
    else:
        expected = (
            "no evidence pack is issued in an unlicensed evaluation: install an Enterprise licence for"
            f" installation {installation}; {m8}"
        )
    assert said(answer) == (409, expected)


async def _named(ctx: Context, *names: str) -> None:
    """A run of each spec ``name``, in order, each created in its own transaction."""
    for name in names:
        spec = compile_spec(
            parse_yaml_text(SPEC_YAML.replace("name: api-demo", f"name: {name}")), targets=ctx.targets
        )
        async with ctx.pool.connection() as conn:
            spec_id = await repo.save_spec(conn, spec, created_by=TESTER.actor)
            await create_run(ctx, conn, spec_id, "batch", {"K001": "K001"}, {})


async def _audit_rows(ctx: Context) -> int:
    async with ctx.pool.connection() as conn:
        return (await (await conn.execute("SELECT count(*) FROM ds.audit_log")).fetchone())[0]  # type: ignore[index]


def _fields(found: licence.Licence) -> dict[str, Any]:
    return {name: value for name, value in found.fields().items() if name != "installation_id"}


def test_get_licence_states_roles(open_client: OpenClient) -> None:
    with open_client(auth={"oidc": OIDC}, tier=None) as client:
        ctx = client.app.state.ctx
        installation, day = (
            client.portal.call(_facts, ctx)["installation_id"],
            client.portal.call(_today, ctx),
        )
        client.portal.call(_named, ctx, "accounts", "loans", "cards")
        callers = [Callers(client).token("viewer"), Callers(client).session("viewer", username="vera")]
        before = client.portal.call(_audit_rows, ctx)
        evaluation = [client.get("/v1/licence", headers=headers) for headers in callers]
        running = client.portal.call(partial(licensed, ctx, starts=day - timedelta(days=340)))  # ending
        ending = client.get("/v1/licence", headers=callers[0]).json()
        follows = running.ends + timedelta(days=1)
        lesser = client.portal.call(partial(licensed, ctx, tier="pro", starts=follows))
        renewed = client.get("/v1/licence", headers=callers[1]).json()
        audit_rows = client.portal.call(_audit_rows, ctx) - before
    assert [answer.status_code for answer in evaluation] == [200, 200]
    body = evaluation[0].json()
    assert body == evaluation[1].json()
    assert (body["installation_id"], body["state"], body["licence"], body["renewal"]) == (
        installation, "evaluation", None, None,
    )  # fmt: skip
    assert body["allows"] == {"runs": False, "sso": False, "signoff": False, "packs": False}  # under SSO
    assert (body["pairs"]["limit"], body["pairs"]["since"]) == (2, None)
    assert [(p["name"], p["seated"], p["targets"]) for p in body["pairs"]["counted"]] == [
        ("accounts", True, ["svc-a vs svc-b"]), ("loans", True, ["svc-a vs svc-b"]),
        ("cards", False, ["svc-a vs svc-b"]),
    ]  # fmt: skip
    assert body["warnings"][0] == (
        f"Unlicensed evaluation of installation {installation}: at most 2 pairs (3 used), no runs under SSO,"
        " no sign-off or evidence packs; not licensed for production use (ds licence)"
    )
    assert body["warnings"][1].startswith(
        "SSO (auth.oidc) is an Enterprise feature and this installation is an unlicensed evaluation: "
    )
    assert (body["clock_behind_seconds"], body["installed"]) == (None, [])
    ends = running.ends
    assert (ending["state"], ending["licence"], ending["renewal"]) == ("ending", _fields(running), None)
    assert ending["allows"] == {"runs": True, "sso": True, "signoff": True, "packs": True}
    assert ending["pairs"]["limit"] is None and ending["pairs"]["since"] == running.starts.isoformat()
    assert [(p["name"], p["seated"]) for p in ending["pairs"]["counted"]] == [
        ("accounts", True), ("loans", True), ("cards", True),
    ]  # fmt: skip
    ending_line = (
        f"licence {running.licence_id} ends on {ends} (in 24 days): install its renewal (ds licence install"
        f" FILE); new runs and evidence packs stop on {ends + timedelta(days=31)}"
    )
    assert ending["warnings"] == [ending_line]
    [row] = ending["installed"]
    shown = ("licence_id", "tier", "starts", "ends", "installed_by", "in_force")
    assert {name: row[name] for name in shown} == {
        "licence_id": running.licence_id, "tier": "enterprise", "starts": running.starts.isoformat(),
        "ends": ends.isoformat(), "installed_by": TESTER.actor, "in_force": True,
    }  # fmt: skip
    assert (row["withdrawn_at"], row["problem"]) == (None, None)
    assert (renewed["state"], renewed["renewal"]) == ("ending", _fields(lesser))
    assert renewed["warnings"] == [
        f"{ending_line}; {lesser.licence_id} (pro, 5 pairs) follows from {follows}"
    ]
    assert [(i["licence_id"], i["in_force"]) for i in renewed["installed"]] == [
        (running.licence_id, True), (lesser.licence_id, False),
    ]  # fmt: skip
    assert audit_rows == 2  # the two installs; no read is audited


def test_get_licence_unknown(open_client: OpenClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreadable(*_: Any) -> None:
        raise RuntimeError("the store is gone")

    with open_client() as client:
        monkeypatch.setattr(licence, "standing", unreadable)
        answer = client.get("/v1/licence")
        runs = client.get("/v1/runs")
    assert (answer.status_code, runs.status_code) == (200, 200)
    assert answer.json() == {
        "installation_id": None, "state": "unknown",
        "warnings": ["licence status unavailable (RuntimeError): see the server log"], "licence": None,
        "renewal": None, "allows": None, "pairs": None, "clock_behind_seconds": None, "installed": [],
    }  # fmt: skip


async def _graced(ctx: Context) -> licence.Licence:
    """The licences gone (an operator's DELETE) and an Enterprise licence that ended yesterday stored."""
    await unlicensed(ctx)
    facts, day = await _facts(ctx), await _today(ctx)
    found = licence.parse(
        licence_file(facts["installation_id"], starts=day - timedelta(days=100), ends=day - timedelta(days=1))
    )
    async with ctx.pool.connection() as conn:
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=TESTER.actor)
    return found


def test_enterprise_pack_carries_licence(packs_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client, documents = packs_client, []
    render = pack.render_pdf

    def kept(document: Document) -> bytes:
        documents.append(document)
        return render(document)

    monkeypatch.setattr(pack, "render_pdf", kept)
    ctx = client.app.state.ctx
    cycle = signed(client)
    olga = session_as(client, "olga", "operator")
    named = client.portal.call(licensed_once, ctx).licence  # the Enterprise licence the client installed
    first = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga)
    graced = client.portal.call(_graced, ctx)
    second = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga)  # issued in its grace
    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    manifests = [
        client.get(f"/v1/packs/{r.json()['pack_id']}", headers=olga).json()["manifest"]
        for r in (first, second)
    ]
    issued = audited(client, "pack.issue")
    assert named is not None
    assert manifests[0]["licence"] == {
        "licence_id": named.licence_id, "customer": "Example Bank plc", "order": "ORD-2026-014",
        "installation_id": named.installation_id, "tier": "enterprise", "pairs": None,
        "starts": named.starts.isoformat(), "ends": named.ends.isoformat(), "state": "valid",
        "key": named.key,
    }  # fmt: skip
    assert (manifests[1]["licence"]["licence_id"], manifests[1]["licence"]["state"]) == (
        graced.licence_id,
        "grace",
    )
    claims = [
        next(s for s in document.sections if s.title == "Claim (observed)").lines[-1]
        for document in documents
        if document.title.startswith("DeltaSleuth evidence pack")
    ]
    assert claims[0] == (
        f"Licence {named.licence_id} of Example Bank plc (order ORD-2026-014): enterprise, {named.starts} to"
        f" {named.ends}, installation {named.installation_id}"
    )
    assert claims[-1].startswith(f"Licence {graced.licence_id} of ") and claims[-1].endswith(
        "; issued in its grace period"
    )
    assert [detail["licence_id"] for _, _, detail in issued] == [named.licence_id, graced.licence_id]
    key = listed_spec(pii=True).raw["key"] | {"pattern": "^[a-z0-9]{4,30}$"}  # lowercase words, hex runs
    spec = listed_spec(pii=True, key=key, knownDiffs=[])
    member = {"licence": manifests[0]["licence"]}
    assert pack.raw_values(spec, {"manifest.json": json.dumps(member).encode()}) == []


def test_signoff_after_grace_holds(open_client: OpenClient, pack_key: Path) -> None:
    with open_client(auth={"oidc": OIDC}, packs=_packs(pack_key), tier=None) as client:
        ctx = client.app.state.ctx
        ended = client.portal.call(_ended, ctx)  # Enterprise, its grace over: runs and packs stop
        cycle = signed(client, sign=False, tier=None)  # made while it ran
        olga = session_as(client, "olga", "operator")
        erin = Callers(client).session(
            *expand(["rule_approver"]), username="erin", auth_time=datetime.now(UTC)
        )
        request = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/signoff", headers=olga)
        step = {"decision": "approve", "attests_sha256": request.json()["attests_sha256"]}
        decision = client.post(
            f"/v1/approvals/{request.json()['approval_id']}/decision", json=step, headers=erin
        )
        refused = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga)
        held = audited(client, "retention.hold")
        client.portal.call(licensed, ctx)  # the renewal, from today
        issued = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga)
    assert (request.status_code, decision.status_code) == (201, 200), (request.text, decision.text)
    assert said(refused)[0] == 409 and said(refused)[1].startswith(f"licence {ended.licence_id} ended on ")
    assert [obj for _, obj, _ in held] == [cycle.run_id]  # the sign-off holds the cycle
    assert issued.status_code == 201, issued.text


def test_runs_list_and_file_names_labelled(client: TestClient, seeded: Any) -> None:
    started = client.post(
        "/v1/runs", json={"spec_id": seeded.spec_id, "mode": "batch", "keys": [RAW_KEY]},
        headers={"X-CSRF-Token": "dev"},
    )  # fmt: skip
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    label = (
        f"Unlicensed evaluation: run {run_id} was made without a licence. Not for production use; no evidence"
        " pack is issued for it."
    )
    record = {"licence_id": None, "tier": None, "state": "evaluation", "ends": None, "label": label}
    listed = {r["run_id"]: r["licence"] for r in client.get("/v1/runs").json()["items"]}
    assert listed == {run_id: record, seeded.run_id: None}  # a run without a record: none
    assert [client.get(f"/v1/runs/{r}").json()["licence"] for r in (run_id, seeded.run_id)] == [record, None]
    names = {
        (r, name): client.get(f"/v1/runs/{r}/reports/{name}").headers["content-disposition"]
        for r in (run_id, seeded.run_id)
        for name in ("report.pdf", "discrepancies.csv")
    }
    assert names == {
        (run_id, "report.pdf"): f'attachment; filename="{run_id}-unlicensed-evaluation-report.pdf"',
        (
            run_id,
            "discrepancies.csv",
        ): f'attachment; filename="{run_id}-unlicensed-evaluation-discrepancies.csv"',
        (seeded.run_id, "report.pdf"): f'attachment; filename="{seeded.run_id}-report.pdf"',
        (seeded.run_id, "discrepancies.csv"): f'attachment; filename="{seeded.run_id}-discrepancies.csv"',
    }


LOCKOUTS = ["evaluation", "pro_with_packs", "expired_enterprise", "expired_pro", "withdrawn", "edited",
            "deleted", "raising"]  # fmt: skip
STATES = {
    "pro_with_packs": "valid",
    "expired_enterprise": "expired",
    "expired_pro": "expired",
    "raising": "unknown",
}


async def _edited(ctx: Context) -> None:
    """Every stored licence's text changed with SQL, the triggers disabled: none verifies any more."""
    async with ctx.pool.connection() as conn:
        await conn.execute("ALTER TABLE ds.licences DISABLE TRIGGER licences_withdraw_only")
        await conn.execute(
            "UPDATE ds.licences SET text = replace(text, 'Example Bank plc', 'Example Bank plx')"
        )
        await conn.execute("ALTER TABLE ds.licences ENABLE TRIGGER licences_withdraw_only")


async def _rejectable(ctx: Context) -> str:
    """An open request to approve a second version of the API's spec."""
    spec = compile_spec(parse_yaml_text(SPEC_YAML.replace('"1.0.0"', '"2.0.0"')), targets=ctx.targets)
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, spec, created_by=TESTER.actor)
        rita = await person(conn, "rita", "spec_author")
        row = await repo.create_approval(conn, "spec", spec_id, rita.actor, scopes=["clear"])
    assert row is not None
    return row["approval_id"]


async def _tables(ctx: Context) -> int:
    """A spec of two table sides, stored unchecked against the targets: no target is read here."""
    async with ctx.pool.connection() as conn:
        return await repo.save_spec(conn, compile_spec(json.loads(table_spec())), created_by=TESTER.actor)


async def _running(ctx: Context, spec_id: int) -> str:
    async with ctx.pool.connection() as conn:
        run_id, _ = await create_run(ctx, conn, spec_id, "batch", {"K001": "K001"}, {})
        await repo.set_run_status(conn, run_id, "running")
    return run_id


@pytest.mark.parametrize("standing", LOCKOUTS)
def test_nothing_locks_out(
    open_client: OpenClient, idp: FakeIdp, pack_key: Path, monkeypatch: pytest.MonkeyPatch, standing: str
) -> None:
    oidc = idp.settings(username_claim="preferred_username")
    monkeypatch.setitem(ENGINES, "postgresql", FakeEngine(relation("accounts")))  # two schemas listed
    databases = "".join(f"  db-{side}:\n    dsn_ref: file:/run/secrets/unused\n" for side in "ab")
    with open_client(
        auth={"oidc": oidc}, packs=_packs(pack_key), tier=None, targets=TARGETS + databases
    ) as client:
        ctx = client.app.state.ctx
        day = client.portal.call(_today, ctx)
        client.portal.call(partial(licensed, ctx, starts=day - timedelta(days=1)))  # Enterprise, for a pack
        cycle = signed(client)
        olga = session_as(client, "olga", "operator")
        pack_id = client.post(f"/v1/runs/{cycle.run_id}/cycles/1/packs", headers=olga).json()["pack_id"]
        spec_id = client.portal.call(_spec, ctx)
        approval_id = approved(client, spec_id)
        live = client.portal.call(_running, ctx, spec_id)
        rejectable = client.portal.call(_rejectable, ctx)
        if standing in ("evaluation", "deleted", "withdrawn"):
            client.portal.call(unlicensed, ctx)
        if standing == "withdrawn":  # a licence not yet started, withdrawn
            later = client.portal.call(partial(licensed, ctx, starts=day + timedelta(days=10)))
            client.portal.call(partial(licence.withdraw, ctx, later.licence_id, "wrong order", who=TESTER))
        elif standing == "pro_with_packs":
            client.portal.call(partial(licensed, ctx, tier="pro"))  # from today: it takes over
        elif standing.startswith("expired_"):
            client.portal.call(partial(unlicensed, ctx, ended=standing.removeprefix("expired_")))
        elif standing == "edited":
            client.portal.call(_edited, ctx)
        elif standing == "raising":

            async def unreadable(*_: Any) -> None:
                raise RuntimeError("the store is gone")

            monkeypatch.setattr(licence, "standing", unreadable)
        signed_in = sign_in(client, idp, groups=["G-admin"], preferred_username="root", sub="root-sub")
        client.cookies.clear()
        admin = Callers(client).session(*expand(["admin"]), username="admin", auth_time=datetime.now(UTC))
        tabled = client.portal.call(_tables, ctx)  # a table pre-check reads the licence only to label it
        prechecked = client.post("/v1/prechecks", json={"spec_ids": [tabled]}, headers=admin)
        precheck_id = prechecked.json().get("precheck_id")
        schemas = {"a": {"target": "db-a"}, "b": {"target": "db-b"}}
        catalogued = client.post("/v1/prechecks/catalogue", json=schemas, headers=admin)
        ids = {"run_id": cycle.run_id, "spec_id": spec_id, "approval_id": approval_id, "pack_id": pack_id,
               "cycle": 1, "name": "report.pdf", "precheck_id": precheck_id, "n": 1}  # fmt: skip
        reads = {
            path: client.get(re.sub(r"\{([^}]+)\}", lambda m: str(ids.get(m[1], "x")), path), headers=admin)
            for (method, path), _ in ROUTES.items()
            if method == "GET"
        }
        shown = client.get("/v1/licence", headers=admin).json()
        control = f"/v1/runs/{live}"
        writes = {
            "reject": client.post(
                f"/v1/approvals/{rejectable}/decision",
                json={"decision": "reject", "note": "no"},
                headers=admin,
            ),
            "pause": client.post(f"{control}/pause", headers=admin),
            "resume": client.post(f"{control}/resume", headers=admin),  # an operator's pause
            "cancel": client.post(f"{control}/cancel", headers=admin),
            "revoke": client.post(
                f"/v1/approvals/{approval_id}/revoke", json={"reason": "wrong clear path"}, headers=admin
            ),
        }
        users = {
            u["username"]: u["user_id"] for u in client.get("/v1/auth/users", headers=admin).json()["items"]
        }
        writes["disable"] = client.post(f"/v1/auth/users/{users['olga']}/disable", headers=admin)
        nightly = {"name": "nightly", "spec_id": spec_id, "run": {"mode": "batch"}, "cron": "0 2 * * *"}
        scheduled = client.post("/v1/schedules", json=nightly | {"time_zone": "UTC"}, headers=admin)
        schedule = (
            f"/v1/schedules/{scheduled.json().get('schedule_id')}"  # no schedule route reads the licence
        )
        writes |= {
            f"schedule {act}": client.post(f"{schedule}/{act}", headers=admin) for act in ("pause", "resume")
        }
        writes["schedule delete"] = client.delete(schedule, headers=admin)
        exported = client.get(f"/v1/prechecks/{precheck_id}/reports/precheck.pdf", headers=admin)
        writes["precheck cancel"] = client.post(f"/v1/prechecks/{precheck_id}/cancel", headers=admin)
        downloads = [client.get(f"/v1/packs/{pack_id}/download", headers=admin), client.get(
            "/v1/packs/revocations", headers=admin)]  # fmt: skip
    assert signed_in.status_code == 303 and "#/signin" not in signed_in.headers["location"]
    assert shown["state"] == STATES.get(standing, "evaluation") and shown["warnings"]
    refused = {
        path: r.status_code for path, r in reads.items() if r.status_code == 409 or r.status_code >= 500
    }
    drafted = "/v1/prechecks/{precheck_id}/pairs/{n}/draft.yaml"  # of a stored spec: no licence's refusal
    assert said(reads[drafted]) == (409, "pair 1 is a stored spec (accounts 1.0.0): it needs no draft")
    assert refused == {"/v1/chat/sessions/{session_id}": 503, drafted: 409}  # AI is off, whatever the licence
    assert reads["/v1/runs/{run_id}/reports/{name}"].status_code == 200
    assert scheduled.status_code == 201 and reads["/v1/schedules"].json()["items"] == []
    assert (prechecked.status_code, exported.status_code, catalogued.status_code) == (202, 200, 202)
    labelled = shown["state"] == "evaluation"  # read to label it, never to refuse it
    assert prechecked.json()["evaluation"] is catalogued.json()["evaluation"] is labelled
    assert reads["/v1/prechecks/{precheck_id}/pairs/{n}"].json()["evaluation"] is labelled
    assert {name: r.status_code for name, r in writes.items()} == dict.fromkeys(writes, 200), {
        name: r.text for name, r in writes.items()
    }
    assert [r.status_code for r in downloads] == [200, 200]
