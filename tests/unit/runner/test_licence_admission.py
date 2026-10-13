"""Licence admission in the runner: the pairs an evaluation or a licence seats,
counted when a run is made or a key compared (again under the pair lock, so two new names at the limit give
one run), a soak's boundary pausing for the licence as of when its next cycle falls due and its resume,
each refusal before any key is read, the SSO fence, a stored row that cannot be read, and what never asks:
work in flight, other resumes, pause, cancel, holds and purge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth import licence
from deltasleuth.agent.tools import Scope, ToolError, run_tool
from deltasleuth.auth.principal import SERVER
from deltasleuth.config import SettingsError, load_settings
from deltasleuth.licence import LICENCE_PAUSE, LicenceError, LicenceRefused
from deltasleuth.runner import cancel_run, compare_one, create_run, pause_run, purge, resume_run, run_worker
from deltasleuth.runner.runs import AUTH_PAUSE, resumable
from deltasleuth.store import repo
from tests.conftest import TESTER, licence_file, licensed

from .conftest import Harness, OpenHarness, ScriptedService, doc, spec_text
from .test_approvals import OIDC, TOKENS, approve, audited, listed, sql, targets
from .test_scan import SEED as USERS
from .test_scan import SPEC as SCANNED
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("vendor_keys")]  # a vendor build

SOAK = "concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 2}"
KEYS = ["K001", "K002", "K003"]  # the unit spec's
EVALUATION = {"licence_id": None, "tier": None, "state": "evaluation", "ends": None}  # a run's record
LOCAL = {"asserted": True, "os_account": "tester"}  # what a CLI principal's audit rows add
PACKS = {"installation": "test-1", "signing_key_ref": "file:/run/secrets/unused"}
M5 = (
    "under SSO this installation keeps serving sign-in, reads, reports, exports, pack downloads and"
    " administration, and new runs, compares and soak cycles need an Enterprise licence (ds licence install"
    " FILE)"
)
LINKS = """
    CREATE TABLE links (entity_code text, id_type text, id_value text,
                        PRIMARY KEY (entity_code, id_type, id_value));
    INSERT INTO links VALUES ('e1', 'phone', 'p1'), ('e2', 'phone', 'p1'), ('e3', 'email', 'x3');
"""
LINK = ["entity_code", "id_type", "id_value"]
LINKED = {"name": "links", "keyColumns": LINK, "columns": ["entity_code"]}
GRAPH = SCANNED | {  # graph parity over the links: its L1 scan and its L2
    "metadata": {"name": "links-graph", "version": "1.0.0", "owner": "tests"},
    "key": {"name": "link", "type": "composite", "parts": LINK, "class": "identifier"},
    "sides": {side: SCANNED["sides"][side] | {"table": LINKED} for side in "ab"},
    "graph": {"members": ["entity_code"], "groups": ["id_type", "id_value"], "maxComponent": 20},
}


def named(name: str, keys: list[str] | None = None, *, execution: str = "", version: str = "1.0.0") -> str:
    """The unit spec as version ``version`` of spec ``name``: a pair of its own."""
    text = spec_text(keys, execution=execution).replace("name: unit-runner", f"name: {name}")
    return text.replace('version: "1.0.0"', f'version: "{version}"')


async def facts(h: Harness) -> dict[str, Any]:
    async with h.ctx.pool.connection() as conn:
        return await repo.licence_facts(conn)


async def today(h: Harness) -> date:
    return (await facts(h))["now"].astimezone(UTC).date()


async def standing(h: Harness) -> licence.Standing:
    async with h.ctx.pool.connection() as conn:
        return await licence.standing(h.ctx, conn)


async def stored(h: Harness, **fields: Any) -> licence.Licence:
    """A licence written as ``repo.insert_licence`` writes one, past what an install takes."""
    found = licence.parse(licence_file((await facts(h))["installation_id"], **fields))
    async with h.ctx.pool.connection() as conn:
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=TESTER.actor)
    return found


async def expired(h: Harness) -> licence.Licence:
    """An Enterprise licence that ended 31 days ago: its grace is over."""
    day = await today(h)
    return await stored(h, starts=day - timedelta(days=100), ends=day - timedelta(days=31))


def under(found: licence.Licence) -> str:
    """How a refusal names a licence."""
    return f"licence {found.licence_id} ({found.tier}, {found.starts} to {found.ends})"


def more(seats: str, since: date) -> str:
    """The tail of a full licence's refusal."""
    return (
        f" its seats since {since} are {seats}, whose runs go ahead; a new pair needs a licence with more"
        " pairs"
    )


def m4(found: licence.Licence) -> str:
    return (
        f"licence {found.licence_id} ended on {found.ends} and its 30 days of grace on"
        f" {found.ends + timedelta(days=30)}: new runs, compares, soak cycles and evidence packs are refused"
        " until a renewed licence is installed (ds licence install FILE); reads, reports, exports, holds,"
        " erasure, purge, audit, pack downloads and sign-offs under an Enterprise licence keep working"
    )


def m4_data(found: licence.Licence) -> dict[str, Any]:
    """What a ``paused`` row records of an expired licence: the record as of when the cycle falls due, the
    refusal."""
    return {"licence_id": found.licence_id, "tier": found.tier, "state": "expired", "ends": str(found.ends),
            "refusal": "expired"}  # fmt: skip


async def ran(h: Harness, name: str, *, ago: timedelta, finished: timedelta | None = None) -> str:
    """A run of spec ``name`` made ``ago`` (and finished ``finished`` ago), stored with SQL as a release
    before 010 could store it: nothing admitted it."""
    spec_id = await h.spec(named(name))
    [row] = await sql(
        h,
        """INSERT INTO ds.runs (run_id, spec_id, mode, status, created_by, created_at, finished_at)
           VALUES (%s, %s, 'batch', %s, %s, now() - %s::interval, now() - %s::interval) RETURNING run_id""",
        repo.new_run_id(), spec_id, "queued" if finished is None else "completed", TESTER.actor, ago,
        finished,
    )  # fmt: skip
    return row["run_id"]


async def refusal(work: Any) -> str:
    """The message of the LicenceRefused that awaiting ``work`` raises."""
    with pytest.raises(LicenceRefused) as refused:
        await work
    return str(refused.value)


# -- pairs and seats --------------------------------------------------------------------------


async def test_evaluation_two_then_M2(harness: Harness) -> None:
    ids = {name: await harness.spec(named(name)) for name in ("customer-profile", "linker", "cards")}
    for name in ("customer-profile", "linker"):
        await create_run(harness.ctx, ids[name], "batch", who=TESTER)
    m2 = (
        "spec cards would be a third pair: an unlicensed evaluation runs 2 pairs (customer-profile, linker);"
        f" install a licence for installation {(await facts(harness))['installation_id']}"
    )
    assert await refusal(create_run(harness.ctx, ids["cards"], "batch", who=TESTER)) == m2
    assert await refusal(compare_one(harness.ctx, ids["cards"], "K001", who=TESTER)) == m2
    assert (
        await sql(harness, "SELECT params->'licence' AS licence FROM ds.runs")
        == [{"licence": EVALUATION}] * 2
    )


async def _unseated_soak(h: Harness, service: ScriptedService) -> tuple[str, str]:
    """A soak of spec cards, the first pair, which two names whose runs an older release stored earlier
    push out of an evaluation's seats; ended at its first boundary. Its id and its refusal."""
    service.both("K001", doc("K001"), doc("K001"))
    run_id = await create_run(
        h.ctx, await h.spec(named("cards", ["K001"], execution=SOAK)), "soak", who=TESTER
    )
    for name in ("accounts", "loans"):
        await ran(h, name, ago=timedelta(hours=1))
    m3 = (
        "spec cards is not one of the 2 pairs an unlicensed evaluation runs (accounts, loans): its cycles"
        " under way end, and a new run or cycle needs a licence for installation"
        f" {(await facts(h))['installation_id']}"
    )
    return run_id, m3


async def test_evaluation_third_name_M3_soak_pauses_at_boundary(
    harness: Harness, service: ScriptedService
) -> None:
    run_id, m3 = await _unseated_soak(harness, service)
    run = await harness.finish(run_id, ("paused", *repo.TERMINAL))
    assert (run["status"], run["stop_reason"], run["cycle"]) == ("paused", LICENCE_PAUSE, 2)
    assert set(await harness.results(run_id, 1)) == {"K001"}  # its cycle under way ended
    assert await harness.results(run_id, 2) == {}  # its next one reads no key while paused
    [pause] = await audited(harness, "paused")
    unseated = EVALUATION | {"refusal": "unseated", "count": 3, "limit": 2, "since": None}  # as data
    assert (pause["actor"], pause["detail"]) == (
        "system:worker", {"reason": LICENCE_PAUSE, "approval_id": None, "licence": unseated},
    )  # fmt: skip
    assert await refusal(resume_run(harness.ctx, run_id, who=TESTER)) == f"run {run_id} cannot resume: {m3}"
    assert await refusal(create_run(harness.ctx, run["spec_id"], "soak", who=TESTER)) == m3
    assert (await harness.run(run_id))["status"] == "paused"


async def test_versions_reruns_cycles_and_one_shots_of_a_counted_name_free(
    harness: Harness, service: ScriptedService
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    first = await harness.spec(named("accounts", ["K001"]))
    await harness.finish(await create_run(harness.ctx, first, "batch", who=TESTER))
    await create_run(harness.ctx, await harness.spec(named("loans", ["K001"])), "batch", who=TESTER)
    newer = await harness.spec(named("accounts", ["K001"], execution=SOAK, version="2.0.0"))
    await create_run(harness.ctx, first, "batch", who=TESTER)  # a re-run
    await compare_one(harness.ctx, newer, "K001", who=TESTER)  # a one-shot of a new version
    soak = await harness.finish(await create_run(harness.ctx, newer, "soak", who=TESTER))  # and its cycles
    assert (soak["status"], soak["cycle"], soak["stop_reason"]) == (
        "completed",
        2,
        "stop_condition:maxCycles",
    )
    assert [row["detail"]["name"] for row in await audited(harness, "licence.pair")] == ["accounts", "loans"]
    assert (await standing(harness)).names == ["accounts", "loans"]


async def test_one_shot_counts_a_new_pair(harness: Harness, service: ScriptedService) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    ids = [await harness.spec(named(name, ["K001"])) for name in ("accounts", "loans", "cards")]
    one_shot = (await compare_one(harness.ctx, ids[0], "K001", who=TESTER)).attrs["run_id"]
    await compare_one(harness.ctx, ids[1], "K001", who=TESTER)
    refused = await refusal(compare_one(harness.ctx, ids[2], "K001", who=TESTER))
    assert refused.startswith("spec cards would be a third pair: an unlicensed evaluation runs 2 pairs")
    [pair, _] = await audited(harness, "licence.pair")
    detail = {"since": None, "name": "accounts", "run_id": one_shot, "count": 1, "limit": 2}
    detail |= {"targets": ["svc-a vs svc-b"]} | LOCAL
    installation = (await facts(harness))["installation_id"]
    assert (pair["actor"], pair["object"], pair["detail"]) == (TESTER.actor, installation, detail)
    [created] = await sql(
        harness, "SELECT params->'licence' AS licence FROM ds.runs WHERE run_id = %s", one_shot
    )
    assert created == {"licence": EVALUATION}
    found = await licensed(harness.ctx, tier="pro")  # each compare_one row carries its run's record
    await compare_one(harness.ctx, ids[2], "K001", who=TESTER)
    record = {"licence_id": found.licence_id, "tier": "pro", "state": "valid", "ends": str(found.ends)}
    compared = [row["detail"]["licence"] for row in await audited(harness, "compare_one")]
    assert compared == [EVALUATION, EVALUATION, record]


async def test_graph_and_table_count_once(
    open_harness: OpenHarness,
    databases: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    (tmp_path / "dsn_tables").write_text(databases(USERS + LINKS))
    entry = {"dsn_ref": f"file:{tmp_path / 'dsn_tables'}", "rate_per_s": 1000}
    async with open_harness(json.dumps({"targets": {"db-a": entry, "db-b": entry}})) as h:
        table, graph = await h.spec(json.dumps(SCANNED)), await h.spec(json.dumps(GRAPH))
        runs = [
            await h.finish(await create_run(h.ctx, spec_id, "batch", who=TESTER))
            for spec_id in (table, graph, table, graph)
        ]
        third = await h.spec(json.dumps(SCANNED | {"metadata": GRAPH["metadata"] | {"name": "users-copy"}}))
        refused = await refusal(create_run(h.ctx, third, "batch", who=TESTER))
        pairs = await audited(h, "licence.pair")
    assert [run["status"] for run in runs] == ["completed"] * 4
    assert runs[1]["scan"]["graph"] is not None  # its L2 ran within the run
    assert [(p["detail"]["name"], p["detail"]["count"]) for p in pairs] == [
        ("users-tables", 1),
        ("links-graph", 2),
    ]
    assert refused.startswith(
        "spec users-copy would be a third pair: an unlicensed evaluation runs 2 pairs (users-tables,"
        " links-graph)"
    )


async def test_pro_sixth_M1(harness: Harness) -> None:
    found = await licensed(harness.ctx, tier="pro")
    for name in ("accounts", "customers", "loans", "payments", "transfers"):
        await create_run(harness.ctx, await harness.spec(named(name)), "batch", who=TESTER)
    seats = "accounts, customers, loans, payments, transfers"
    assert await refusal(
        create_run(harness.ctx, await harness.spec(named("cards")), "batch", who=TESTER)
    ) == (f"spec cards would be pair 6 of 5 under {under(found)}:{more(seats, found.starts)}")
    record = {"licence_id": found.licence_id, "tier": "pro", "state": "valid", "ends": str(found.ends)}
    assert await sql(harness, "SELECT DISTINCT params->'licence' AS licence FROM ds.runs") == [
        {"licence": record}
    ]


async def test_trial_then_pro_seats_first_five(harness: Harness) -> None:
    day = await today(harness)
    await licensed(harness.ctx, starts=day - timedelta(days=5), ends=day + timedelta(days=20))  # a trial
    names = ["accounts", "cards", "customers", "loans", "payments", "savings", "transfers"]
    ids = {name: await harness.spec(named(name)) for name in names}
    for name in names:  # unlimited under the trial
        await create_run(harness.ctx, ids[name], "batch", who=TESTER)
    pro = await licensed(harness.ctx, tier="pro")  # starts later: it takes over, from today
    seats = "accounts, cards, customers, loans, payments"
    assert (await standing(harness)).seats == names[:5]
    assert await refusal(create_run(harness.ctx, ids["savings"], "batch", who=TESTER)) == (
        f"spec savings is not one of the 5 pairs {under(pro)} seats since {pro.starts} ({seats}): its cycles"
        " under way end, and a new run or cycle needs a licence with more pairs"
    )
    await create_run(harness.ctx, ids["accounts"], "batch", who=TESTER)  # seated
    assert await refusal(
        create_run(harness.ctx, await harness.spec(named("wires")), "batch", who=TESTER)
    ) == (f"spec wires would be pair 8 of 5 under {under(pro)}:{more(seats, pro.starts)}")


async def test_live_soaks_carried_into_a_smaller_window(harness: Harness, service: ScriptedService) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    live = await create_run(
        harness.ctx, await harness.spec(named("live", ["K001"], execution=SOAK)), "soak", who=TESTER
    )
    await sql(
        harness,
        "UPDATE ds.runs SET created_at = now() - interval '3 days' WHERE run_id = %s RETURNING 1",
        live,
    )  # live at the licence's starts
    await ran(harness, "old", ago=timedelta(days=5), finished=timedelta(days=4))  # finished before it
    found = await licensed(harness.ctx, tier="pro", pairs=1)
    assert ((await standing(harness)).names, (await standing(harness)).seats) == (["live"], ["live"])
    assert await refusal(create_run(harness.ctx, await harness.spec(named("old")), "batch", who=TESTER)) == (
        f"spec old would be pair 2 of 1 under {under(found)}:{more('live', found.starts)}"
    )
    run = await harness.finish(live)  # seated: its boundary goes on
    assert (run["status"], run["cycle"], run["stop_reason"]) == ("completed", 2, "stop_condition:maxCycles")


async def test_two_new_names_race(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    found = await licensed(harness.ctx, tier="pro", pairs=3)
    ids = {name: await harness.spec(named(name)) for name in ("accounts", "loans", "cards", "payments")}
    for name in ("accounts", "loans"):
        await create_run(harness.ctx, ids[name], "batch", who=TESTER)
    real, inserting, go = repo.create_run, asyncio.Event(), asyncio.Event()

    async def held(*args: Any, **kwargs: Any) -> Any:  # the first insert waits, holding the pair lock
        if not inserting.is_set():
            inserting.set()
            await go.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(repo, "create_run", held)
    first = asyncio.create_task(create_run(harness.ctx, ids["cards"], "batch", who=TESTER))
    await asyncio.wait_for(inserting.wait(), 10)
    second = asyncio.create_task(create_run(harness.ctx, ids["payments"], "batch", who=TESTER))
    waiting = """SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()
                 AND wait_event_type = 'Lock' AND wait_event = 'advisory'"""
    for _ in range(500):  # the second counted three names or fewer unlocked, and now waits for the lock
        if (await sql(harness, waiting))[0]["n"] == 1:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("the second creation never waited for the pair lock")
    assert not second.done()
    go.set()
    run_id = await first
    seats = more("accounts, loans, cards", found.starts)
    assert await refusal(second) == f"spec payments would be pair 4 of 3 under {under(found)}:{seats}"
    assert [p["detail"]["run_id"] for p in await audited(harness, "licence.pair")][-1] == run_id
    assert (await standing(harness)).names == ["accounts", "loans", "cards"]


async def test_refused_before_resolve(harness: Harness, service: ScriptedService) -> None:
    service.listings = [["K001"]]
    for name in ("accounts", "loans"):
        await ran(harness, name, ago=timedelta(minutes=1))
    cards = await harness.spec(listed().replace("name: unit-runner", "name: cards"))
    for work in (
        create_run(harness.ctx, cards, "batch", who=TESTER),
        compare_one(harness.ctx, cards, "K001", who=TESTER),
    ):
        assert (await refusal(work)).startswith("spec cards would be a third pair")
    assert (service.walks, service.log) == (0, [])  # refused before the key listing or a key was read
    assert await sql(harness, "SELECT count(*) AS runs FROM ds.runs") == [{"runs": 2}]


async def test_expired_M4(harness: Harness, service: ScriptedService) -> None:
    spec_id = await harness.spec(named("accounts"))
    found = await expired(harness)
    assert await refusal(create_run(harness.ctx, spec_id, "batch", who=TESTER)) == m4(found)
    assert await refusal(compare_one(harness.ctx, spec_id, "K001", who=TESTER)) == m4(found)
    assert (service.log, await sql(harness, "SELECT count(*) AS runs FROM ds.runs")) == ([], [{"runs": 0}])


# -- work in flight and a soak's boundary -----------------------------------------------------


async def test_batch_in_flight_completes_past_grace(harness: Harness, service: ScriptedService) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    run_id = await create_run(harness.ctx, await harness.spec(named("accounts")), "batch", who=TESTER)
    await expired(harness)  # the grace ends while it runs
    run = await harness.finish(run_id)
    assert (run["status"], run["counters"]["match"]) == ("completed", 3)


async def test_auth_paused_batch_resumes_and_completes_after_grace(
    open_harness: OpenHarness, service: ScriptedService, tmp_path: Path
) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    service.token, secret = "new", tmp_path / "token"
    secret.write_text("old")  # the targets want a credential the file does not hold yet
    assert service.server is not None
    auth = f'    auth: {{kind: bearer, secret_ref: "file:{secret}"}}\n    capabilities:'
    text = named("accounts", execution="concurrency: 1").replace(
        "oneShot: {delay: 0s, times: 1}", "outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]"
    )
    async with open_harness(targets(service).replace("    capabilities:", auth)) as h:
        run_id = await create_run(h.ctx, await h.spec(text), "batch", who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
        try:
            run = await h.wait(run_id, ("paused",))
            found = await expired(h)
            secret.write_text("new")
            assert await refusal(create_run(h.ctx, run["spec_id"], "batch", who=TESTER)) == m4(found)
            assert await resume_run(h.ctx, run_id, who=TESTER) == "running"  # not a licence pause
            run = await h.wait(run_id, ("paused", *repo.TERMINAL))
        finally:
            stop.set()
            await worker
    assert (run["status"], run["stop_reason"], run["counters"]["match"]) == ("completed", None, 3)


async def test_licence_paused_soak_resume_refused(harness: Harness, service: ScriptedService) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    text = named("accounts", ["K001"], execution=SOAK)
    run_id = await create_run(harness.ctx, await harness.spec(text), "soak", who=TESTER)
    found = await expired(harness)
    run = await harness.finish(run_id, ("paused", *repo.TERMINAL))
    assert (run["status"], run["stop_reason"], run["cycle"]) == ("paused", LICENCE_PAUSE, 2)
    [pause] = await audited(harness, "paused")
    assert pause["detail"] == {"reason": LICENCE_PAUSE, "approval_id": None, "licence": m4_data(found)}
    assert (
        await refusal(resume_run(harness.ctx, run_id, who=TESTER))
        == f"run {run_id} cannot resume: {m4(found)}"
    )
    assert (await harness.run(run_id))["stop_reason"] == LICENCE_PAUSE


async def test_boundary_pause_then_resume_after_install(harness: Harness, service: ScriptedService) -> None:
    run_id, _ = await _unseated_soak(harness, service)
    await harness.finish(run_id, ("paused", *repo.TERMINAL))
    await licensed(harness.ctx)  # Enterprise: unlimited pairs
    assert (await harness.run(run_id))["status"] == "paused"  # installing resumes nothing
    assert await resume_run(harness.ctx, run_id, who=TESTER) == "running"
    run = await harness.finish(run_id)
    assert (run["status"], run["cycle"], run["stop_reason"]) == ("completed", 2, "stop_condition:maxCycles")
    assert set(await harness.results(run_id, 2)) == {"K001"}
    moves = [
        (r["actor"], r["action"], r["detail"]["reason"]) for r in await audited(harness, "paused", "running")
    ]
    assert moves == [("system:worker", "paused", LICENCE_PAUSE), (TESTER.actor, "running", None)]


async def test_rolling_soak_paused_cycle_keeps_census(harness: Harness, service: ScriptedService) -> None:
    service.listings = [["K001", "K002"], ["K002", "K003"]]
    for key in ("K001", "K002", "K003"):
        service.both(key, doc(key), doc(key))
    text = listed(SOAK).replace("name: unit-runner", "name: accounts")
    run_id = await create_run(
        harness.ctx, await harness.spec(text), "soak", soak={"rolling": True}, who=TESTER
    )
    await expired(harness)
    run = await harness.finish(run_id, ("paused", *repo.TERMINAL))
    assert (run["status"], run["stop_reason"], run["cycle"], service.walks) == ("paused", LICENCE_PAUSE, 2, 2)
    census = run["census"]  # its paused cycle keeps the listing walked at the boundary
    assert (census["cycle"], census["a"]["items"], census["a"]["target"]) == (2, 2, "svc-a")
    async with harness.ctx.pool.connection() as conn:
        keys, _ = await repo.list_keys(conn, run_id, 2)
    assert [(key["key_masked"], key["state"]) for key in keys] == [("K002", "pending"), ("K003", "pending")]


async def test_boundary_error_never_fails_soak(
    harness: Harness,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    spec_id = await harness.spec(named("accounts", ["K001"], execution=SOAK))
    run_id = await create_run(harness.ctx, spec_id, "soak", who=TESTER)

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("a bug")

    monkeypatch.setattr(licence, "standing", broken)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.licence"):
        run = await harness.finish(run_id)
    assert (run["status"], run["cycle"], run["stop_reason"]) == ("completed", 2, "stop_condition:maxCycles")
    assert (
        f"licence check at the boundary of run {run_id} failed (RuntimeError): the soak goes on"
        in caplog.messages
    )

    async def unreachable(*_: Any, **__: Any) -> Any:
        raise psycopg.OperationalError("the store is down")

    monkeypatch.setattr(licence, "standing", unreachable)  # the sweep tries again: the soak is fine
    async with harness.ctx.pool.connection() as conn:
        spec = await harness.ctx.spec(conn, spec_id)
    with pytest.raises(psycopg.OperationalError):
        await licence.stopped(harness.ctx, run, spec, datetime.now(UTC))


async def test_cycle_due_after_the_grace_starts_paused(harness: Harness, service: ScriptedService) -> None:
    """The next cycle is judged as of when it falls due: in the grace now, but due after it, it starts
    paused, and resumes once a renewal covers it."""
    service.both("K001", doc("K001"), doc("K001"))
    day = await today(harness)
    found = await stored(harness, starts=day - timedelta(days=100), ends=day - timedelta(days=25))
    text = named("accounts", ["K001"], execution="cycleInterval: 30d, stop: {maxCycles: 2}")
    run_id = await create_run(harness.ctx, await harness.spec(text), "soak", who=TESTER)  # in its grace
    run = await harness.finish(run_id, ("paused", *repo.TERMINAL))
    assert (run["status"], run["stop_reason"], run["cycle"]) == ("paused", LICENCE_PAUSE, 2)
    [pause] = await audited(harness, "paused")
    assert pause["detail"]["licence"] == m4_data(found)
    assert (await standing(harness)).state == "grace"  # a resume is judged as of then too
    assert (
        await refusal(resume_run(harness.ctx, run_id, who=TESTER))
        == f"run {run_id} cannot resume: {falls_due(run, days=30)}{m4(found)}"
    )
    assert await resumable(harness.ctx) == []
    await licensed(harness.ctx)  # the renewal, from today
    assert await resumable(harness.ctx) == [run_id]
    assert await resume_run(harness.ctx, run_id, who=TESTER) == "running"


def falls_due(run: dict[str, Any], *, days: int) -> str:
    """How a refusal judged as of when the run's paused cycle falls due, later than now, begins."""
    at = run["started_at"] + timedelta(days=days) * (run["cycle"] - 1)
    return f"its cycle falls due on {at.astimezone(UTC):%Y-%m-%dT%H:%MZ}, when "


async def test_downgrade_before_the_cycle_falls_due_pauses_it(
    harness: Harness, service: ScriptedService
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    day = await today(harness)
    await licensed(harness.ctx, starts=day - timedelta(days=10))  # Enterprise: unlimited
    await ran(harness, "accounts", ago=timedelta(hours=1))  # live: the first seat
    text = named("cards", ["K001"], execution="cycleInterval: 10d, stop: {maxCycles: 2}")
    run_id = await create_run(harness.ctx, await harness.spec(text), "soak", who=TESTER)
    pro = await licensed(harness.ctx, tier="pro", pairs=1, starts=day + timedelta(days=5))  # a downgrade
    run = await harness.finish(run_id, ("paused", *repo.TERMINAL))
    assert (run["status"], run["stop_reason"], run["cycle"]) == ("paused", LICENCE_PAUSE, 2)
    [pause] = await audited(harness, "paused")
    record = {"licence_id": pro.licence_id, "tier": "pro", "state": "valid", "ends": str(pro.ends)}
    assert pause["detail"]["licence"] == record | {
        "refusal": "unseated", "count": 2, "limit": 1, "since": str(pro.starts)
    }  # fmt: skip
    assert await refusal(resume_run(harness.ctx, run_id, who=TESTER)) == (
        f"run {run_id} cannot resume: {falls_due(run, days=10)}spec cards is not one of the 1 pairs"
        f" {under(pro)} seats since"
        f" {pro.starts} (accounts): its cycles under way end, and a new run or cycle needs a licence with"
        " more pairs"
    )
    await licensed(harness.ctx, starts=day)  # an Enterprise correction from today: Pro still follows
    assert await resumable(harness.ctx) == []  # what ds licence install lists, judged as resume_run judges
    assert (await standing(harness)).seats == ["accounts", "cards"]  # seated now, under Enterprise


async def test_a_row_that_cannot_be_read_grants_nothing(harness: Harness, service: ScriptedService) -> None:
    """A row written with SQL whose signature line ends in a no-break space: uncounted, and
    nothing raises: the Enterprise licence beside it is valid, installs and runs work."""
    service.both("K001", doc("K001"), doc("K001"))
    found, day = await licensed(harness.ctx), await today(harness)
    text = licence_file((await facts(harness))["installation_id"]).decode()[:-1] + "\u00a0\n"
    licence_id = text.split("\n")[1].removeprefix("licence: ")
    async with harness.ctx.pool.connection() as conn:
        sha256 = hashlib.sha256(text.encode()).hexdigest()
        await repo.insert_licence(conn, licence_id, text, sha256, installed_by=TESTER.actor)
    current = await standing(harness)
    assert (current.state, current.licence) == ("valid", found)
    assert current.uncounted == ((licence_id, "its text is not a DeltaSleuth licence (deltasleuth-licence/1):"
                                              " signature is not base64"),)  # fmt: skip
    later = await licensed(harness.ctx, starts=day + timedelta(days=30))
    run = await harness.finish(await create_run(harness.ctx, await harness.spec(named("accounts", ["K001"])),
                                                "batch", who=TESTER))  # fmt: skip
    assert (run["status"], run["params"]["licence"]["licence_id"]) == ("completed", found.licence_id)
    with pytest.raises(LicenceError, match="signature is not base64"):
        await licence.withdraw(harness.ctx, licence_id, "written with SQL", who=TESTER)
    assert (await standing(harness)).renewal == later  # installed


# -- the SSO fence ---------------------------------------------------------------------------------


async def _start(h: Harness, auth: str) -> None:
    """The server's start in ``auth`` mode, as its audit row records it."""
    async with repo.audited(h.ctx.pool) as (_, audit):
        await audit(SERVER, "system.start", "server", {"auth": auth, "pack_key": None})


async def test_sso_fence_M5_suffix(open_harness: OpenHarness, service: ScriptedService) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    async with open_harness(targets(service), auth=OIDC, tier=None) as h:
        spec_id = await h.spec(named("accounts", ["K001"]))
        await approve(h, spec_id)  # the four-eyes gate comes first
        evaluation = await refusal(create_run(h.ctx, spec_id, "batch", who=TESTER))
        pro = await licensed(h.ctx, tier="pro")
        under_pro = await refusal(compare_one(h.ctx, spec_id, "K001", who=TESTER))
        await _start(h, "oidc")
    async with open_harness(targets(service), auth=TOKENS) as h:  # a --config without auth.oidc
        behind = await refusal(create_run(h.ctx, spec_id, "batch", who=TESTER))
    async with open_harness(targets(service)) as h:  # developer mode too
        assert await refusal(create_run(h.ctx, spec_id, "batch", who=TESTER)) == behind
        await _start(h, "tokens")  # the server started again in tokens mode, Pro's production mode
        await create_run(h.ctx, spec_id, "batch", who=TESTER)
    tail = f"{M5} or tokens mode"
    evaluated = "this installation is an unlicensed evaluation"
    assert evaluation == f"SSO (auth.oidc) is an Enterprise feature and {evaluated}: {tail}"
    assert (
        under_pro == f"SSO (auth.oidc) is an Enterprise feature and licence {pro.licence_id} is pro: {tail}"
    )
    assert behind == f"{under_pro} (this installation's server last started in oidc mode)"


@pytest.mark.parametrize("packs", [True, False], ids=["packs", "no_packs"])
async def test_M5_advice_never_a_settings_error(
    open_harness: OpenHarness, service: ScriptedService, tmp_path: Path, packs: bool
) -> None:
    async with open_harness(
        targets(service), auth=OIDC, tier=None, **({"packs": PACKS} if packs else {})
    ) as h:
        spec_id = await h.spec(named("accounts"))
        await approve(h, spec_id)
        refused = await refusal(create_run(h.ctx, spec_id, "batch", who=TESTER))
    advised = json.loads((tmp_path / "deltasleuth.yaml").read_text())
    del advised["auth"]["oidc"]  # tokens mode, as the advice would have it
    (tmp_path / "tokens.yaml").write_text(json.dumps(advised))
    if packs:  # packs need SSO: tokens mode would be a settings error, so it is never advised
        assert refused.endswith(f"{M5}; keep SSO, which evidence packs need")
        with pytest.raises(SettingsError, match=r"packs: needs auth\.oidc"):
            load_settings(tmp_path / "tokens.yaml")
    else:
        assert refused.endswith(f"{M5} or tokens mode")
        assert load_settings(tmp_path / "tokens.yaml").auth_mode == "tokens"


async def test_compare_key_tool_error(harness: Harness, service: ScriptedService) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    run_id = await create_run(
        harness.ctx, await harness.spec(named("accounts", ["K001"])), "batch", who=TESTER
    )
    await harness.finish(run_id)
    found = await expired(harness)
    with pytest.raises(ToolError) as refused:  # the model is told, as of a missing approval
        await run_tool(harness.ctx, Scope(run_id, TESTER), "compare_key", {"key": "K001"})
    assert str(refused.value) == m4(found)


async def test_licence_pair_once(harness: Harness) -> None:
    accounts = await harness.spec(named("accounts"))
    first = await create_run(harness.ctx, accounts, "batch", who=TESTER)
    await create_run(harness.ctx, accounts, "batch", who=TESTER)
    found = await licensed(harness.ctx, tier="pro")
    await create_run(harness.ctx, accounts, "batch", who=TESTER)  # counted in its window already
    loans = await create_run(harness.ctx, await harness.spec(named("loans")), "batch", who=TESTER)
    rows = await audited(harness, "create_run", "licence.pair")
    installation, targets_ = (await facts(harness))["installation_id"], ["svc-a vs svc-b"]
    record = {"licence_id": found.licence_id, "tier": "pro", "state": "valid", "ends": str(found.ends)}
    records = [r["detail"]["licence"] for r in rows if r["action"] == "create_run"]
    assert records == [EVALUATION, EVALUATION, record, record]
    pairs = [(r["actor"], r["object"], r["detail"]) for r in rows if r["action"] == "licence.pair"]
    accounts_pair = {"since": None, "name": "accounts", "run_id": first, "count": 1, "limit": 2}
    loans_pair = {"since": str(found.starts), "name": "loans", "run_id": loans, "count": 2, "limit": 5}
    assert pairs == [
        (TESTER.actor, installation, accounts_pair | {"targets": targets_} | LOCAL),
        (TESTER.actor, found.licence_id, loans_pair | {"targets": targets_} | LOCAL),
    ]
    assert [r["action"] for r in rows] == [  # each after its run's own row
        "create_run", "licence.pair", "create_run", "create_run", "create_run", "licence.pair",
    ]  # fmt: skip


# -- nothing locks out ------------------------------------------------------------------------


@pytest.mark.parametrize("fenced", ["expired", "sso_evaluation"])
async def test_nothing_locks_out(open_harness: OpenHarness, service: ScriptedService, fenced: str) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    async with open_harness(targets(service), auth=TOKENS) as h:  # made before the fence
        batch = await create_run(h.ctx, await h.spec(named("accounts")), "batch", who=TESTER)
        soak = await create_run(h.ctx, await h.spec(named("loans", execution=SOAK)), "soak", who=TESTER)
        await pause_run(h.ctx, batch, reason=AUTH_PAUSE, who=TESTER)  # as its worker pauses it
        if fenced == "expired":
            await expired(h)
    settings = {"auth": OIDC, "tier": None} if fenced == "sso_evaluation" else {"auth": TOKENS}
    async with open_harness(targets(service), **settings) as h:
        spec_id = (await h.run(batch))["spec_id"]
        if fenced == "sso_evaluation":  # spec approvals work: the four-eyes gate is not the licence's
            await approve(h, spec_id)
            await approve(h, (await h.run(soak))["spec_id"])
        refused = await refusal(create_run(h.ctx, spec_id, "batch", who=TESTER))  # new work is refused
        assert refused.startswith(
            "licence LI-" if fenced == "expired" else "SSO (auth.oidc) is an Enterprise"
        )
        assert await resume_run(h.ctx, batch, who=TESTER) == "running"
        assert await pause_run(h.ctx, batch, who=TESTER) == "paused"
        assert await resume_run(h.ctx, batch, who=TESTER) == "running"
        await purge.hold(h.ctx, TESTER, batch, "audit case 7")
        await purge.hold(h.ctx, TESTER, batch, None)
        assert await cancel_run(h.ctx, soak, who=TESTER) == "cancelled"
        run = await h.finish(batch)
        await purge.purge(h.ctx, TESTER)
    assert (run["status"], run["counters"]["match"]) == ("completed", 3)
