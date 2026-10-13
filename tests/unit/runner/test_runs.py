"""Runs through the worker against a scripted service: rechecks, stops, soak, one-shot, politeness."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import threading
import time
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException

from deltasleuth.api.routes.runs import _runner_errors
from deltasleuth.connectors import tables
from deltasleuth.connectors.files import SampleWriter
from deltasleuth.connectors.postgres import engine as postgresql
from deltasleuth.core.types import SideStateKind
from deltasleuth.reports.csv import write_csv
from deltasleuth.runner import (
    cancel_run,
    compare,
    compare_one,
    create_run,
    cycle,
    outcome_changes,
    resume_run,
    run_worker,
)
from deltasleuth.runner import keys as key_sets
from deltasleuth.runner import runs as running
from deltasleuth.runner.recheck import INTERMITTENT_B
from deltasleuth.runner.worker import LOCK_ID
from deltasleuth.security.crypto import Sealer
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import TARGETS, UNAVAILABLE, Harness, OpenHarness, ScriptedService, doc, spec_text, until
from .sampled import (
    Tables,
    audited,
    tables_fixture,  # noqa: F401 (the on_tables fixture)
)
from .test_keys_table import (
    ITEM_KEYS,
    ITEMS,
    Failing,
    compiled,
    engine,  # noqa: F401 (a fixture)
    keyed,
)
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
KEYS = [f"K{n:03d}" for n in range(1, 9)]


async def _run(h: Harness, spec: str, keys: list[str] | None = None, **kw: Any) -> tuple[str, repo.Row]:
    run_id = await create_run(h.ctx, await h.spec(spec), "batch", keys=keys, **kw, who=TESTER)
    return run_id, await h.finish(run_id)


# -- rechecks and evidence -------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["a", "b"])
async def test_a_transient_failure_then_agreement_is_match(
    harness: Harness, service: ScriptedService, failing: str
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    service.script(failing, "K001", UNAVAILABLE, (200, doc("K001")))
    run_id, run = await _run(harness, spec_text(), ["K001"])
    k1 = (await harness.results(run_id))["K001"]
    assert (k1["outcome"], k1["confirmation"], k1["first_outcome"]) == ("MATCH", "rechecked", "INCONCLUSIVE")
    assert (run["status"], run["counters"]["match"]) == ("completed", 1)
    # The failed call is evidence too, kept with its pass.
    failed = [
        o for o in await harness.observations(run_id, "K001") if o["pass_no"] == 1 and o["side"] == failing
    ]
    assert [(o["http_status"], o["error_class"]) for o in failed] == [(503, "http_5xx")]


async def test_a_transient_failure_then_difference_is_owed_a_recheck(
    harness: Harness, service: ScriptedService
) -> None:
    service.script("a", "K001", (200, doc("K001", status="OLD")))
    service.script("b", "K001", UNAVAILABLE, (200, doc("K001", status="NEW")))
    service.both("K002", doc("K002", status="OLD"), doc("K002", status="NEW"))  # never failed
    run_id, run = await _run(harness, spec_text(), ["K001", "K002"])
    results = await harness.results(run_id)
    k1, k2 = results["K001"], results["K002"]
    assert (k1["outcome"], k1["confirmation"], k1["first_outcome"]) == (
        "MISMATCH",
        "provisional",
        "INCONCLUSIVE",
    )
    assert (k2["outcome"], k2["confirmation"]) == ("MISMATCH", "rechecked")
    # B answered once: nothing intermittent, and the defect groups with the key that never failed.
    (f1,), (f2,) = k1["findings"], k2["findings"]
    assert (f1["value_class"], f1["signature_hash"], f1["root_cause_id"]) == (
        f2["value_class"],
        f2["signature_hash"],
        f2["root_cause_id"],
    )
    c = run["counters"]
    assert (c["different"], c["outstanding"], c["different_provisional"], run["status"]) == (
        1,
        1,
        0,
        "completed_with_issues",
    )


async def test_rechecks_read_b_first_and_judge_changes(harness: Harness, service: ScriptedService) -> None:
    service.both("K001", doc("K001", status="OLD"), doc("K001", status="NEW"))  # confirmed
    service.script("a", "K002", (200, doc("K002")))
    service.script("b", "K002", (200, doc("K002", status="STALE")), (200, doc("K002")))  # b converges
    # A's raw bytes change on the re-read, but a declared normaliser removes the change.
    service.script(
        "a", "K003", (200, doc("K003", name="Al", status="OLD")), (200, doc("K003", name="Al ", status="OLD"))
    )
    service.script("b", "K003", (200, doc("K003", name="Al", status="NEW")))
    run_id, _ = await _run(harness, spec_text(), ["K001", "K002", "K003"])
    results = await harness.results(run_id)
    judged = {k: (r["outcome"], r["drift_reason"], r["confirmation"]) for k, r in results.items()}
    assert judged == {
        "K001": ("MISMATCH", None, "rechecked"),
        "K002": ("DRIFT", "b_converged", "rechecked"),
        "K003": ("MISMATCH", None, "rechecked"),
    }
    assert [side for side, key, _ in service.log if key == "K001"] == ["a", "b", "b", "a"]
    assert results["K002"]["first_outcome"] == "MISMATCH" and results["K002"]["findings"] == []


@pytest.mark.parametrize("path", ["worker", "compare_one"])
async def test_rechecks_wait_their_delay_and_outlast_an_unusable_read(
    harness: Harness, service: ScriptedService, path: str
) -> None:
    service.script("a", "K001", (200, doc("K001", status="OLD")))
    service.script(
        "b", "K001", (200, doc("K001", status="NEW")), UNAVAILABLE, (200, doc("K001", status="NEWER"))
    )
    policy = "recheck: {delay: 500ms, times: 2, oneShot: {delay: 500ms, times: 2}}"
    spec_id = await harness.spec(
        spec_text().replace("recheck: {delay: 0s, times: 1, oneShot: {delay: 0s, times: 1}}", policy)
    )
    if path == "worker":
        run_id = await create_run(harness.ctx, spec_id, "batch", keys=["K001"], who=TESTER)
        await harness.finish(run_id)
    else:
        run_id = (await compare_one(harness.ctx, spec_id, "K001", who=TESTER)).attrs["run_id"]
    run, k1 = await harness.run(run_id), (await harness.results(run_id))["K001"]
    async with harness.ctx.pool.connection() as conn:
        (cause,) = await repo.list_root_causes(conn, run_id)
    b_reads = [t for side, _, t in service.log if side == "b"]
    assert len(b_reads) == 3 and all(later - earlier >= 0.5 for earlier, later in pairwise(b_reads))
    # The 503 left a recheck to go; the last one found B changed and still different.
    assert (k1["outcome"], k1["confirmation"], run["counters"]["different"]) == ("MISMATCH", "rechecked", 1)
    assert [f["value_class"] for f in k1["findings"]] == [INTERMITTENT_B] == [cause["value_class"]]


async def test_a_nul_padded_value_is_saved_not_read_again(harness: Harness, service: ScriptedService) -> None:
    service.both("K001", doc("K001", status="ACTIVE\u0000"), doc("K001", status="ACTIVE"))
    run_id, run = await _run(harness, spec_text(), ["K001"])
    (finding,) = (await harness.results(run_id))["K001"]["findings"]
    assert (run["status"], finding["value_a"], finding["value_b"]) == ("completed", "ACTIVE\\u0000", "ACTIVE")
    assert service.calls[("a", "K001")] == 2  # the first read and its recheck, and no more


# -- stop conditions, pause, resume, cancel -----------------------------------------------------------


async def test_consecutive_errors_stop_the_run(harness: Harness, service: ScriptedService) -> None:
    keys = [f"K{n:03d}" for n in range(1, 31)]
    for key in keys:
        service.script("a", key, UNAVAILABLE)
        service.script("b", key, UNAVAILABLE)
    stop = "concurrency: 1, stop: {maxConsecutiveErrors: 3, maxErrorRate: {rate: 0.2, window: 10}}"
    run_id, run = await _run(harness, spec_text(keys, execution=stop))
    # Key 1 (a, b) and key 2 (a) fail in a row; key 2 finishes its pair, then nothing starts.
    assert sum(service.calls.values()) == 4
    assert (run["status"], run["stop_reason"]) == (
        "completed_with_issues",
        "stop_condition:maxConsecutiveErrors",
    )
    c = run["counters"]
    assert (c["total"], c["indeterminate"], c["cancelled"]) == (30, 2, 28)
    retry = list(csv.DictReader(io.StringIO((await write_csv(harness.ctx, run_id, None, "retry")).decode())))
    cancelled = {(r["outcome"], r["reason"]) for r in retry[2:]}
    assert len(retry) == 30 and cancelled == {("CANCELLED", "stop_condition:maxConsecutiveErrors")}


async def test_max_keys_caps_the_key_set_but_not_the_rechecks(
    harness: Harness, service: ScriptedService
) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    service.both("K002", doc("K002", status="OLD"), doc("K002", status="NEW"))
    _, run = await _run(harness, spec_text(KEYS, execution="concurrency: 2, stop: {maxKeys: 3}"))
    assert {key for _, key in service.calls} == {"K001", "K002", "K003"}
    assert service.calls[("b", "K002")] == 2
    c = run["counters"]
    assert (c["total"], c["match"], c["different"], c["cancelled"]) == (3, 2, 1, 0)
    assert (run["status"], run["stop_reason"]) == ("completed", "stop_condition:maxKeys")


async def test_a_refused_credential_pauses_the_run_until_resumed(
    open_harness: OpenHarness, service: ScriptedService, tmp_path: Path
) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    service.token, secret = "new", tmp_path / "token"
    secret.write_text("old")  # the targets want a credential the file does not hold yet
    assert service.server is not None
    targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500).replace(
        "    capabilities:", f'    auth: {{kind: bearer, secret_ref: "file:{secret}"}}\n    capabilities:'
    )
    text = spec_text(KEYS, execution="concurrency: 1").replace(
        "oneShot: {delay: 0s, times: 1}", "outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]"
    )  # INCONCLUSIVE is not rechecked: the refused key is read again all the same
    async with open_harness(targets) as h:
        run_id = await create_run(h.ctx, await h.spec(text), "batch", who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))  # one process pauses and resumes
        try:
            run = await h.wait(run_id, ("paused",))
            assert run["stop_reason"] == "run_paused_auth_error"
            (refused,) = {key for _, key in service.calls}  # a refused credential is not hammered
            secret.write_text("new")  # the operator replaces the credential file, then resumes
            assert await resume_run(h.ctx, run_id, who=TESTER) == "running"
            run = await h.wait(run_id, ("paused", *repo.TERMINAL))
        finally:
            stop.set()
            await worker
    assert (run["status"], run["stop_reason"]) == ("completed", None)
    assert (run["counters"]["match"], service.calls[("a", refused)]) == (8, 2)


async def test_a_recorded_refusal_is_judged_not_paused_on(open_harness: OpenHarness, tmp_path: Path) -> None:
    samples = _samples(tmp_path / "samples", {k: ((200, doc(k)), (200, doc(k))) for k in KEYS[:3]})
    refusal = b'{"error": "UNAUTHORIZED"}'
    SampleWriter(samples).write("K002", "b", refusal, status=401, error_class="auth_error")
    async with open_harness(TARGETS.format(base="http://127.0.0.1:9", rate=500)) as h:
        run_id = await create_run(h.ctx, await h.spec(spec_text()), "batch", samples_dir=samples, who=TESTER)
        run = await h.finish(run_id, ("paused", *repo.TERMINAL))  # no credential to replace: no pause
        assert run["status"] == "completed"
        k2 = (await h.results(run_id))["K002"]
    assert (run["counters"]["match"], k2["outcome"]) == (2, "INCONCLUSIVE")


async def test_a_run_whose_kek_the_keyring_lacks_pauses_until_it_is_back(
    harness: Harness, service: ScriptedService
) -> None:
    for key in KEYS[:2]:
        service.both(key, doc(key), doc(key))
    run_id = await create_run(harness.ctx, await harness.spec(spec_text(KEYS[:2])), "batch", who=TESTER)
    async with harness.ctx.pool.connection() as conn:  # its data key as if wrapped by a KEK now lost
        kek = (await repo.data_key(conn, run_id))["kek"]  # type: ignore[index]
        await conn.execute("UPDATE ds.data_keys SET kek = %s WHERE scope = %s", ("K-" + "0" * 16, run_id))
    run = await harness.finish(run_id, ("paused", *repo.TERMINAL))
    assert (run["status"], run["stop_reason"], sum(service.calls.values())) == (
        "paused", "run_paused_key_missing", 0,  # its keys cannot even be opened, so nothing is read
    )  # fmt: skip
    async with harness.ctx.pool.connection() as conn:  # the line restored from the vault
        await conn.execute("UPDATE ds.data_keys SET kek = %s WHERE scope = %s", (kek, run_id))
    assert await resume_run(harness.ctx, run_id, who=TESTER) == "running"
    run = await harness.finish(run_id)
    assert (run["status"], run["stop_reason"], run["counters"]["match"]) == ("completed", None, 2)


async def test_cancel_ends_the_run_with_its_unstarted_keys_cancelled(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    keys = [f"K{n:03d}" for n in range(1, 41)]
    for key in keys:
        service.both(key, doc(key), doc(key))
    assert service.server is not None
    async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=10)) as h:
        run_id = await create_run(h.ctx, await h.spec(spec_text(keys)), "batch", who=TESTER)
        worker = asyncio.create_task(h.finish(run_id))
        await until(lambda: len(service.calls) >= 6 or worker.done())
        assert await cancel_run(h.ctx, run_id, who=TESTER) in ("cancelling", "cancelled")
        run = await worker
    assert (run["status"], run["stop_reason"]) == ("cancelled", "cancelled")
    assert 0 < run["counters"]["cancelled"] < 40 and run["counters"]["match"] > 0


async def test_until_in_the_past_stops_the_run(harness: Harness, service: ScriptedService) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    _, run = await _run(
        harness, spec_text(KEYS, execution='concurrency: 1, stop: {until: "2020-01-01T00:00:00Z"}')
    )
    assert (run["status"], run["stop_reason"]) == ("completed_with_issues", "stop_condition:until")
    assert run["counters"]["cancelled"] == 7


async def test_a_bug_fails_the_run_naming_only_the_error_type(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("K001 Alice Secretname")  # a message may hold keys and values

    monkeypatch.setattr("deltasleuth.runner.worker.evaluate_many", broken)
    run_id, run = await _run(harness, spec_text(KEYS))
    assert (run["status"], run["error"]) == ("failed", "RuntimeError")
    async with harness.ctx.pool.connection() as conn:
        assert set((await repo.cycle_counts(conn, run_id))["keys"]) == {"cancelled"}
        ended, _ = await repo.list_audit(conn, action="run.finished", obj=run_id)
    assert [(r["actor"], r["detail"]) for r in ended] == [  # once, however many slots failed
        ("system:worker", {"status": "failed", "reason": "RuntimeError"})
    ]


async def test_cancel_reads_no_key_that_waited_for_its_turn(
    harness: Harness, service: ScriptedService
) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    text = spec_text(KEYS, execution="concurrency: 1, delay: 1s")
    run_id = await create_run(harness.ctx, await harness.spec(text), "batch", who=TESTER)
    worker = asyncio.create_task(harness.finish(run_id))
    await until(lambda: len(service.log) == 2 or worker.done())  # one key read: its slot waits its delay
    assert await cancel_run(harness.ctx, run_id, who=TESTER) == "cancelling"
    read = len(service.log)
    run = await worker
    assert (run["status"], len(service.log), run["counters"]["cancelled"]) == ("cancelled", read, 7)


async def test_a_one_shot_run_cancelled_meanwhile_stays_cancelled(
    harness: Harness, service: ScriptedService
) -> None:
    service.both("K001", doc("K001", status="OLD"), doc("K001", status="NEW"))
    spec_id = await harness.spec(spec_text().replace("oneShot: {delay: 0s", "oneShot: {delay: 1s"))
    comparing = asyncio.create_task(compare_one(harness.ctx, spec_id, "K001", who=TESTER))
    await until(lambda: service.calls[("b", "K001")] or comparing.done())
    async with harness.ctx.pool.connection() as conn:
        (queued,), _ = await repo.list_runs(conn, status="queued")
    assert await cancel_run(harness.ctx, queued["run_id"], who=TESTER) == "cancelled"
    await comparing  # its recheck is read, but the run's end is not rewritten
    async with harness.ctx.pool.connection() as conn:
        keys = (await repo.cycle_counts(conn, queued["run_id"]))["keys"]
    assert ((await harness.run(queued["run_id"]))["status"], keys) == ("cancelled", {"cancelled": 1})


async def test_a_missing_target_secret_fails_the_run_naming_its_file(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    assert service.server is not None
    targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500).replace(
        "  svc-b:\n", "  svc-b:\n    auth: {kind: bearer, secret_ref: file:missing_token}\n"
    )
    async with open_harness(targets) as h:
        _, run = await _run(h, spec_text(), ["K001"])
    assert run["status"] == "failed" and run["error"].startswith("cannot read secret file")
    assert run["error"].endswith("missing_token: No such file or directory")


async def test_the_sweep_isolates_a_run_the_store_refuses(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    spec_id = await harness.spec(spec_text())
    other = await create_run(harness.ctx, spec_id, "batch", keys=["K001"], who=TESTER)
    refused = await create_run(
        harness.ctx, spec_id, "batch", keys=["K001"], who=TESTER
    )  # the newer: ended first
    summarise, finish, ended = cycle.summarise, cycle.finish_cycle, []

    async def refusing(*args: Any) -> dict[str, Any]:
        ended.append(args[3]["run_id"])
        if args[3]["run_id"] == refused:
            raise psycopg.errors.CheckViolation("a value the store refuses")
        return await summarise(*args)

    async def together(ctx: Any, run_id: str, **options: Any) -> None:
        """The first cycle-ending pass waits until both runs' keys are done, so that one sweep ends both."""
        done = False
        while not done:
            await asyncio.sleep(0.02)
            async with ctx.pool.connection() as conn:
                done = await repo.cycle_done(conn, other, 1) and await repo.cycle_done(conn, refused, 1)
        await finish(ctx, run_id, **options)

    monkeypatch.setattr(cycle, "summarise", refusing)
    monkeypatch.setattr("deltasleuth.runner.worker.finish_cycle", together)
    assert (await harness.finish(other, timeout=20))["status"] == "completed"
    failed = await harness.run(refused)  # by then: that worker's sweep ended it first, and went on
    assert (failed["status"], failed["error"]) == ("failed", "CheckViolation")
    assert ended == [refused, other] and failed["finished_at"] < (await harness.run(other))["finished_at"]


# -- soak, one-shot, file mode ---------------------------------------------------------------------

LISTED = """\
  source:
    kind: paginated_endpoint
    target: svc-a
    request: {method: GET, path: /list}
    pagination: {kind: cursor, itemsPath: "$.items", cursorPath: "$.next", cursorParam: cursor}
    keyPath: "$.id"
"""


def listed(execution: str = "", paging: str = "") -> str:
    """The unit spec reading its keys from the service's ``/list`` (``paging``: more pagination)."""
    text = spec_text(execution=execution).replace(
        "  source: {kind: list, keys: [K001, K002, K003]}\n", LISTED
    )
    return text.replace("cursorParam: cursor}", f"cursorParam: cursor{paging}}}")


async def test_a_rolling_soak_records_outcome_changes(harness: Harness, service: ScriptedService) -> None:
    service.listings = [["K001", "K002", "K004"], ["K002", "K003", "K004"]]  # two pages each
    for key in ("K001", "K003", "K004"):
        service.both(key, doc(key), doc(key))
    service.script("a", "K002", (200, doc("K002", status="OLD")))
    service.script("b", "K002", (200, doc("K002", status="OLD")), (200, doc("K002", status="NEW")))
    text = listed("concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 2}")
    run_id = await create_run(
        harness.ctx, await harness.spec(text), "soak", soak={"rolling": True}, who=TESTER
    )
    run = await harness.finish(run_id)
    assert (run["status"], run["stop_reason"], run["cycle"]) == ("completed", "stop_condition:maxCycles", 2)
    assert set(await harness.results(run_id, 1)) == {"K001", "K002", "K004"}
    assert set(await harness.results(run_id, 2)) == {"K002", "K003", "K004"}
    assert await outcome_changes(harness.ctx, run_id) == [
        {"cycle": 2, "key_masked": "K002", "from_outcome": "MATCH", "to_outcome": "MISMATCH"}
    ]


async def test_a_rolling_soak_outlives_a_listing_outage_at_a_boundary(
    harness: Harness, service: ScriptedService
) -> None:
    service.listings = [["K001", "K002", "K003"], [None]]  # the second walk answers 503
    for key in ("K001", "K002", "K003"):
        service.both(key, doc(key), doc(key))
    text = listed("concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 2}")
    run_id = await create_run(
        harness.ctx, await harness.spec(text), "soak", soak={"rolling": True}, who=TESTER
    )
    run = await harness.finish(run_id)
    assert (run["status"], run["cycle"], run["counters"]["match"]) == ("completed", 2, 3)
    assert set(await harness.results(run_id, 2)) == {"K001", "K002", "K003"}  # the same keys again


async def test_a_soak_waits_its_cycle_interval(harness: Harness, service: ScriptedService) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    text = spec_text(["K001"], execution="concurrency: 2, cycleInterval: 1s, stop: {maxCycles: 2}")
    await harness.finish(await create_run(harness.ctx, await harness.spec(text), "soak", who=TESTER))
    first, second = (t for side, _, t in service.log if side == "a")
    assert second - first >= 0.8  # cycle 2 is due a cycleInterval after the start, not at once


@pytest.mark.parametrize(
    ("listing", "paging"),
    [(["K001", "K002", None], ""), (["K001", "K002", "K003", "K004", "K005"], ", maxPages: 1")],
    ids=["a later page refused", "maxPages reached"],
)
async def test_a_partial_key_listing_refuses_the_run(
    harness: Harness, service: ScriptedService, listing: list[str | None], paging: str
) -> None:
    service.listings = [listing]
    with pytest.raises(ValueError, match="not read completely"):
        await create_run(harness.ctx, await harness.spec(listed(paging=paging)), "batch", who=TESTER)


async def test_keys_the_store_cannot_hold_are_refused_by_position(harness: Harness) -> None:
    spec_id = await harness.spec(spec_text().replace('pattern: "^K[0-9]{3}$"', 'pattern: "^K"'))
    with pytest.raises(ValueError, match=r"2 key\(s\) .* \(positions 2, 3\)"):
        await create_run(harness.ctx, spec_id, "batch", keys=["K001", "K" * 1025, "K\u0000"], who=TESTER)


async def test_compare_one_rechecks_inline(harness: Harness, service: ScriptedService) -> None:
    service.script("a", "K001", (200, doc("K001")))  # b has no such key: 404
    result = await compare_one(harness.ctx, await harness.spec(spec_text()), " k001 ", who=TESTER)
    assert (result.outcome.value, result.confirmation.value) == ("NOT_FOUND_B", "rechecked")
    assert service.calls[("b", "K001")] == 2
    run = await harness.run(result.attrs["run_id"])
    assert (run["mode"], run["status"], run["counters"]["different"]) == ("one_shot", "completed", 1)
    async with harness.ctx.pool.connection() as conn:
        assert (await repo.cycle_counts(conn, run["run_id"]))["keys"] == {"done": 1}


async def test_a_compare_that_fails_while_finishing_fails_its_run(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.runner import runs

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("summary")

    service.both("K001", doc("K001"), doc("K001"))
    spec_id = await harness.spec(spec_text())
    monkeypatch.setattr(runs, "summarise", broken)
    with pytest.raises(RuntimeError):
        await compare_one(harness.ctx, spec_id, "K001", who=TESTER)
    async with harness.ctx.pool.connection() as conn:
        [run] = [r for r in (await repo.list_runs(conn, limit=10))[0] if r["mode"] == "one_shot"]
    assert run["status"] == "failed"  # never left queued


async def test_file_mode_takes_one_malformed_sample_as_one_inconclusive_key(
    harness: Harness, tmp_path: Path
) -> None:
    writer = SampleWriter(tmp_path / "samples")
    for key in ("K001", "K002", "K003"):
        for side in "ab":
            writer.write(
                key, side, str(doc(key)).replace("'", '"').encode(), observed_at="2026-09-24T10:00:00Z"
            )
    (tmp_path / "samples" / "b" / "K002.meta.json").write_text('{"status": "404"}')
    run_id, run = await _run(harness, spec_text(), samples_dir=tmp_path / "samples")
    k2 = (await harness.results(run_id))["K002"]
    assert (k2["outcome"], k2["inconclusive_reason"], k2["confirmation"]) == (
        "INCONCLUSIVE",
        "unparseable",
        "provisional",
    )
    assert (run["status"], run["counters"]["match"]) == ("completed", 2)


def _samples(root: Path, docs: dict[str, tuple[Any, Any]]) -> Path:
    writer = SampleWriter(root)
    for key, pair in docs.items():
        for side, (status, body) in zip("ab", pair, strict=True):
            writer.write(
                key, side, json.dumps(body).encode(), status=status, observed_at="2026-09-24T10:00:00Z"
            )
    return root


async def test_file_mode_refuses_sample_keys_that_normalise_alike(harness: Harness, tmp_path: Path) -> None:
    pair = ((200, doc("K001")), (200, doc("K001")))
    samples = _samples(tmp_path / "samples", {"K001": pair, " K001": pair})
    with pytest.raises(ValueError, match="normalise to the same key"):
        await create_run(
            harness.ctx, await harness.spec(spec_text()), "batch", samples_dir=samples, who=TESTER
        )


async def test_file_mode_finds_a_multi_part_key_by_its_normalised_parts(
    harness: Harness, tmp_path: Path
) -> None:
    """A sample is its key's by its name's parts, each normalised; a name that is not the key's parts joined
    by ``|`` is no key's, and is left out rather than refused."""
    pair = ((200, doc("x")), (200, doc("x")))
    samples = _samples(tmp_path / "samples", {" eu | s1 |7": pair, "K001": pair, "eu|s1": pair})
    text = spec_text(['"EU|S1|7"', '"US|S2|8"']).replace('"^K[0-9]{3}$"', '"^[A-Z0-9]+$"')
    text = text.replace("type: string", "type: composite\n  parts: [region, shelf, code]")
    run_id, _ = await _run(harness, text, samples_dir=samples)
    found = await harness.results(run_id)
    assert {key: (r["outcome"], r["inconclusive_reason"]) for key, r in found.items()} == {
        "EU|S1|7": ("MATCH", None),
        "US|S2|8": ("INCONCLUSIVE", "skipped"),  # no sample is this key's
    }


async def test_stop_conditions_stop_a_file_mode_run(open_harness: OpenHarness, tmp_path: Path) -> None:
    ok = {k: ((200, doc(k)), (200, doc(k))) for k in ("K001", "K003")}
    samples = _samples(
        tmp_path / "samples", ok | {"K002": ((200, doc("K002")), (404, {"error": "NOT_FOUND"}))}
    )
    async with open_harness(TARGETS.format(base="http://127.0.0.1:9", rate=500), concurrency=1) as h:
        spec = spec_text(execution="concurrency: 1, stop: {maxUnexplainedCritical: 1}")
        _, run = await _run(h, spec, samples_dir=samples)
    # K002 is NOT_FOUND_B: its MISSING_DOCUMENT finding is CRITICAL and unsuppressed.
    assert (run["stop_reason"], run["counters"]["by_outcome"]["NOT_FOUND_B"]) == (
        "stop_condition:maxUnexplainedCritical",
        1,
    )
    assert run["counters"]["cancelled"] == 1


async def test_critical_findings_saved_before_a_worker_restart_still_count(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = {k: ((200, doc(k)), (404, {"error": "NOT_FOUND"})) for k in ("K001", "K003")}  # CRITICAL
    found = {k: ((200, doc(k)), (200, doc(k))) for k in ("K002", "K004")}
    samples = _samples(tmp_path / "samples", missing | found)
    evaluate, reads = compare.evaluate_many, 0

    async def stalling(*args: Any, **kw: Any) -> Any:
        nonlocal reads
        reads += 1
        if reads == 2:  # the first worker stops while it reads K002, which is handed back
            await asyncio.Event().wait()
        return await evaluate(*args, **kw)

    monkeypatch.setattr("deltasleuth.runner.worker.evaluate_many", stalling)
    async with open_harness(TARGETS.format(base="http://127.0.0.1:9", rate=500), concurrency=1) as h:
        spec = spec_text(KEYS[:4], execution="concurrency: 1, stop: {maxUnexplainedCritical: 2}")
        run_id = await create_run(h.ctx, await h.spec(spec), "batch", samples_dir=samples, who=TESTER)
        stop = asyncio.Event()
        first = asyncio.create_task(run_worker(h.ctx, stop=stop))
        await until(lambda: reads == 2)
        stop.set()
        await first
        run = await h.finish(run_id)  # a new worker process: it stops at K003, as K001's finding counts
    assert (run["stop_reason"], run["counters"]["cancelled"]) == ("stop_condition:maxUnexplainedCritical", 2)


async def test_file_mode_refuses_a_soak(harness: Harness, tmp_path: Path) -> None:
    samples = _samples(tmp_path / "samples", {"K001": ((200, doc("K001")), (200, doc("K001")))})
    spec_id = await harness.spec(spec_text(execution="concurrency: 1, cycleInterval: 0s"))
    with pytest.raises(ValueError, match="captured samples do not change"):
        await create_run(harness.ctx, spec_id, "soak", samples_dir=samples, who=TESTER)


# -- politeness -----------------------------------------------------------------------------------


async def test_one_target_serving_both_sides_keeps_one_budget(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    for key in KEYS[:6]:
        service.both(key, doc(key), doc(key))
    assert service.server is not None
    async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=4)) as h:
        await _run(h, spec_text(KEYS[:6]).replace("target: svc-b", "target: svc-a"))
    times = sorted(t for _, _, t in service.log)
    assert len(times) == 12
    assert max(sum(1 for u in times if t <= u < t + 1.0) for t in times) <= 5  # one budget, not one per side


async def test_execution_delay_spaces_consecutive_keys(harness: Harness, service: ScriptedService) -> None:
    for key in KEYS[:4]:
        service.both(key, doc(key), doc(key))
    await _run(harness, spec_text(KEYS[:4], execution="concurrency: 1, delay: 300ms, jitter: 100ms"))
    starts = [t for side, _, t in service.log if side == "a"]
    gaps = [later - earlier for earlier, later in pairwise(starts)]
    assert len(gaps) == 3 and all(0.28 <= gap <= 0.6 for gap in gaps), gaps


async def test_compares_share_the_target_budget(open_harness: OpenHarness, service: ScriptedService) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    assert service.server is not None
    async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=4)) as h:
        spec_id = await h.spec(spec_text())
        await asyncio.gather(*(compare_one(h.ctx, spec_id, "K001", who=TESTER) for _ in range(8)))
    times = sorted(t for side, _, t in service.log if side == "a")
    assert len(times) == 8 and max(sum(1 for u in times if t <= u < t + 1.0) for t in times) <= 5


# A spec that declares a breaker, rechecking no key it could not read (as the mock specs do).
GUARDED = spec_text(
    [f"K{n:03d}" for n in range(1, 13)],
    execution="concurrency: 1, circuitBreaker: {errorRate: {rate: 0.5, window: 10}, cooldown: 60s}",
).replace("recheck: {", "recheck: {outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B], ")


def _serve(service: ScriptedService, down: bool) -> None:
    """Every key answered alike on both sides, or by 503; and the calls counted from here."""
    for key in [f"K{n:03d}" for n in range(1, 13)]:
        service.script("a", key, UNAVAILABLE if down else (200, doc(key)))
        service.script("b", key, UNAVAILABLE if down else (200, doc(key)))
    service.calls.clear()


async def test_a_spec_s_own_breaker_governs_its_run_whoever_called_the_target_first(
    harness: Harness, service: ScriptedService
) -> None:
    """A target keeps a breaker for each ``execution.circuitBreaker`` declared (``TargetGate.breaker``). It
    kept one, with the settings of the first spec that called it: after a run of a spec that declares none,
    a later spec's breaker never opened, and its run tried every key of a service that was down."""
    _serve(service, down=False)
    _, run = await _run(harness, spec_text(KEYS))
    assert run["counters"]["match"] == len(KEYS)
    _serve(service, down=True)
    run_id, run = await _run(harness, GUARDED)
    # Ten calls fail on each side's target and open its breaker: the other two keys are not sent.
    assert (run["counters"]["indeterminate"], sum(service.calls.values())) == (12, 20)
    results = (await harness.results(run_id)).values()
    assert Counter((r["side_a_reason"], r["side_b_reason"]) for r in results) == {
        ("error_a", "error_b"): 10,
        ("circuit_open", "circuit_open"): 2,
    }


async def test_an_open_breaker_refuses_no_call_of_a_spec_that_declares_none(
    harness: Harness, service: ScriptedService
) -> None:
    """The breaker a spec declares is that declaration's. Open, it refused every other spec's runs on the
    same targets, those of a spec without one too: their keys came back ``circuit_open`` from a service that
    was up again, until the app was restarted."""
    _serve(service, down=True)
    await _run(harness, GUARDED)
    assert sum(service.calls.values()) == 20  # its breaker is open on both targets, for a minute
    _serve(service, down=False)
    _, run = await _run(harness, spec_text(KEYS))
    assert (run["counters"]["match"], sum(service.calls.values())) == (len(KEYS), 2 * len(KEYS))
    _, run = await _run(harness, GUARDED.replace("unit-runner", "guarded-too"))  # declares the same one
    assert (run["counters"]["indeterminate"], sum(service.calls.values())) == (12, 2 * len(KEYS))


async def test_a_second_worker_waits_for_the_first(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    assert service.server is not None
    targets = TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=4)
    async with open_harness(targets) as first, open_harness(targets) as second:  # two processes' worth
        stop = asyncio.Event()
        other = asyncio.create_task(run_worker(second.ctx, stop=stop))
        try:
            await first.finish(
                await create_run(first.ctx, await first.spec(spec_text(KEYS)), "batch", who=TESTER)
            )
        finally:
            stop.set()
            await other
    times = sorted(t for side, _, t in service.log if side == "a")
    assert len(times) == 8 and max(sum(1 for u in times if t <= u < t + 1.0) for t in times) <= 5


async def test_a_polite_run_does_not_hold_every_slot(harness: Harness, service: ScriptedService) -> None:
    for key in [*KEYS, "K009"]:
        service.both(key, doc(key), doc(key))
    polite = await harness.spec(spec_text(KEYS, execution="concurrency: 1, delay: 500ms"))
    await create_run(harness.ctx, polite, "batch", who=TESTER)
    brisk = await create_run(harness.ctx, await harness.spec(spec_text()), "batch", keys=["K009"], who=TESTER)
    await harness.finish(brisk)
    before = {key for _, key, _ in service.log[: [key for _, key, _ in service.log].index("K009")]}
    assert len(before) <= 2  # not every key of the polite run first


async def test_a_key_slower_than_its_lease_is_read_once(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    service.slow = 0.8  # each side: the pair takes longer than the lease
    assert service.server is not None
    async with open_harness(
        TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500), lease_seconds=1
    ) as h:
        _, run = await _run(h, spec_text(), ["K001"])
    assert (run["status"], dict(service.calls)) == ("completed", {("a", "K001"): 1, ("b", "K001"): 1})


async def test_a_slow_spec_map_does_not_stall_the_event_loop(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.both("K001", doc("K001"), doc("K001"))
    observe = compare.observe

    def heavy(*args: Any) -> Any:
        time.sleep(0.5)  # a CPU-bound JSONata map over a large response
        return observe(*args)

    monkeypatch.setattr(compare, "observe", heavy)
    ticks: list[float] = []

    async def tick() -> None:
        while True:
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(tick())
    await compare_one(harness.ctx, await harness.spec(spec_text()), "K001", who=TESTER)
    ticker.cancel()
    assert max(later - earlier for earlier, later in pairwise(ticks)) < 0.3  # the API stays served


# -- a stored run ----------------------------------------------------------------------------------


async def test_a_run_reads_the_same_once_its_targets_are_gone(
    open_harness: OpenHarness, service: ScriptedService
) -> None:
    service.script("a", "K001", (200, doc("K001")))  # b answers 404: svc-b declares it authoritative
    assert service.server is not None
    async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)) as h:
        run_id, _ = await _run(h, spec_text(), ["K001"])
    async with open_harness('targets:\n  other: {base_url: "https://other.example.com"}\n') as h:
        async with h.ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
            spec = await h.ctx.spec(conn, run["spec_id"])
            (result,), _ = await repo.list_results(conn, run_id)
            found = await compare.side_state(
                h.ctx, conn, spec, run_id, result["obs_b_id"], h.ctx.capabilities(run)
            )
    assert found is not None and (result["outcome"], found[1].kind) == ("NOT_FOUND_B", SideStateKind.ABSENT)


async def test_stored_capabilities_unchanged(open_harness: OpenHarness, service: ScriptedService) -> None:
    """A truncation flag is the spec's alone: a run under flags stores what its targets declare, and hashes
    its plan, as the same run without them does. A key a flag cuts is above the API cap, as one the count
    rule cuts; a side with flags only keeps its target's count rule."""
    declared = "authoritative_absence: {{status: [404]}}"
    indicators = {"cap": 50, "items_path": "$.tags", "count_path": None}
    assert service.server is not None
    targets = TARGETS.replace(
        declared, declared + "\n      truncation_indicators: {{cap: 50, items_path: $.tags}}"
    )
    counted = spec_text().replace(  # a truncated key is not rechecked: its reason stays
        "oneShot: {delay: 0s, times: 1}", "outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]"
    )
    flagged = counted.replace('{itemsPath: "$.tags", cap: 50}', '{flagPaths: ["$.more"]}')
    flagged = flagged.replace("1.0.0", "1.1.0")
    whole = {"more": False}
    service.both("K001", doc("K001") | whole, doc("K001") | whole)
    service.both("K002", doc("K002") | {"more": True}, doc("K002") | {"more": True})  # a flag, where read
    service.both("K003", doc("K003") | whole, doc("K003") | whole | {"tags": ["t"] * 50})  # b's count rule
    cut = ("INCONCLUSIVE", "truncated")
    async with open_harness(targets.format(base=f"http://127.0.0.1:{service.server.port}", rate=500)) as h:
        stored = []
        for text in (counted, flagged):
            run_id, run = await _run(h, text)
            async with h.ctx.pool.connection() as conn:
                (created,), _ = await repo.list_audit(conn, action="create_run", obj=run_id)
            stored.append((run["params"]["capabilities"], created["detail"]["plan_sha256"]))
            found = await h.results(run_id)
            assert {key: (r["outcome"], r["inconclusive_reason"]) for key, r in found.items()} == {
                "K001": ("MATCH", None),
                "K002": cut if text is flagged else ("MATCH", None),
                "K003": cut,
            }
            assert run["counters"]["api_uncomparable"] == (2 if text is flagged else 1)
    assert stored[0] == stored[1]
    assert {caps["truncation_indicators"] == indicators for caps in stored[1][0].values()} == {True}
    assert "more" not in json.dumps(stored[1][0])


async def test_a_worker_that_loses_its_lock_stops_then_retakes_it(
    harness: Harness, service: ScriptedService
) -> None:
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(harness.ctx, stop=stop))

    async def holder(other_than: int | None = None) -> int:
        """The pid holding the worker lock (a new one when ``other_than`` is given), within 10 s."""
        sql = "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objid = %s AND granted"
        for _ in range(200):
            async with harness.ctx.pool.connection() as conn:
                row = await (await conn.execute(sql, (LOCK_ID,))).fetchone()
            if row and row[0] != other_than:
                return row[0]
            await asyncio.sleep(0.05)
        raise AssertionError("the worker lock was not (re)taken")

    try:
        first = await holder()
        async with harness.ctx.pool.connection() as conn:  # a database restart, as the worker sees it
            await conn.execute("SELECT pg_terminate_backend(%s)", (first,))
        await holder(other_than=first)
        spec_id = await harness.spec(spec_text(KEYS))
        run = await harness.finish(await create_run(harness.ctx, spec_id, "batch", who=TESTER))
    finally:
        stop.set()
        await worker
    assert run["status"] == "completed"  # the retaken lock keeps processing


# -- a key table: creation and rolls ---------------------------------------------------------------

ITEM = [f"K00{n}" for n in range(1, 6)]  # ITEMS' keys
ROLLING = "concurrency: 2, cycleInterval: {}, stop: {{maxCycles: 2}}"
MOVED = "DELETE FROM item_keys WHERE id = 'K001'; INSERT INTO item_keys VALUES ('K006'), ('K007')"


async def _rolling(h: Harness, service: ScriptedService, interval: str = "0s", **soak: Any) -> str:
    """A soak of two cycles between the two services, its keys those of ``ITEMS``' key table; rolling
    unless ``soak`` says."""
    for key in (*ITEM, "K006", "K007"):
        service.both(key, doc(key), doc(key))
    spec_id = await h.spec(keyed(ITEM_KEYS, execution=ROLLING.format(interval)))
    return await create_run(h.ctx, spec_id, "soak", soak={"rolling": True} | soak, who=TESTER)


async def test_rolling_reads_the_key_table_again(
    on_tables: Tables,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rolling soak reads its key table again at each cycle's end, bounded by its cycle interval, an hour
    at most (a creation's read by the hour): the next cycle compares the keys the table then holds, and its
    start audits what the read met. Without ``--rolling`` the first cycle's keys stay."""
    dsn, harness = on_tables(ITEMS)
    real, bounds = tables.ENGINES["postgresql"], []

    def bounded(target: Any, *, timeout_s: float = 0) -> Any:
        bounds.append(timeout_s)
        return type(real).snapshot(real, target, timeout_s=timeout_s)

    monkeypatch.setattr(real, "snapshot", bounded)
    async with harness as h:
        run_id = await _rolling(h, service, "2s")
        fixed = await _rolling(h, service, "2s", rolling=False)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(MOVED)
        run = await h.finish(run_id)
        await h.finish(fixed)
        assert (run["status"], run["cycle"], bounds) == ("completed", 2, [3600.0, 3600.0, 2.0])
        assert set(await h.results(run_id, 1)) == set(ITEM) == set(await h.results(fixed, 2))
        assert set(await h.results(run_id, 2)) == {*ITEM[1:], "K006", "K007"}
        ((_, started),) = await audited(h, run_id, "run.cycle")
        read = started.pop("key_source")
        assert (started, await audited(h, fixed, "run.cycle")) == (
            {"cycle": 2},
            [("run.cycle", {"cycle": 2})],
        )
        assert {name: read[name] for name in ("basis", "target", "relation", "rows", "keys")} == {
            "basis": "key_table", "target": "db-a", "relation": "public.item_keys", "rows": 6, "keys": 6,
        }  # fmt: skip
        # Cycles back to back bound nothing more than the hour.
        spec = compiled(h, keyed(ITEM_KEYS, execution=ROLLING.format("0s")))
        taken, _, _ = await cycle._rolled(h.ctx, spec, {"run_id": run_id, "params": run["params"]})
        assert (len(taken or ()), bounds[3:]) == (6, [3600.0])


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (psycopg.OperationalError("server closed the connection unexpectedly"), "OperationalError"),
        (tables.TargetRefused("MySQL 1105"), "MySQL 1105"),
    ],
    ids=["PostgreSQL mid-stream", "a TableError"],
)
async def test_roll_outage_keeps_keys(
    on_tables: Tables,
    service: ScriptedService,
    engine: Failing,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    code: str,
) -> None:
    """An outage of a key table's target at a roll never ends a soak, nor is it taken for the store's: the
    next cycle compares the previous keys, and the failed read is audited by its code alone."""
    monkeypatch.setattr(postgresql, "BATCH", 2)  # three fetches: the fault comes after the first
    dsn, harness = on_tables(ITEMS)
    async with harness as h:
        run_id = await _rolling(h, service)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(MOVED)
        engine("census", error, after=1)
        run = await h.finish(run_id)
        assert (run["status"], run["cycle"], run["counters"]["match"]) == ("completed", 2, 5)
        assert set(await h.results(run_id, 2)) == set(ITEM)
        assert await audited(h, run_id, "rolling_resolve_failed", "run.cycle") == [
            ("rolling_resolve_failed", {"error": f"the key source could not be read ({code})"}),
            ("run.cycle", {"cycle": 2}),
        ]


async def test_roll_refused_credential_pauses(
    on_tables: Tables,
    service: ScriptedService,
    engine: Failing,  # noqa: F811
) -> None:
    """A key table's target that refuses the credential at a roll pauses the soak, as a scan's does: it is
    not asked again until a resume, which reads the table again."""
    dsn, harness = on_tables(ITEMS)
    async with harness as h:
        run_id = await _rolling(h, service)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(MOVED)
        engine("snapshot", tables.TargetAuthRefused("ORA-01017"))
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
        try:
            run = await h.wait(run_id, ("paused", *repo.TERMINAL))
            await asyncio.sleep(0.3)  # sweeps go by: a paused run's roll is not tried again
            assert (run["status"], run["stop_reason"], run["cycle"]) == ("paused", "run_paused_auth_error", 1)
            assert await audited(h, run_id, "paused", "rolling_resolve_failed", "run.cycle") == [
                ("paused", {"reason": "run_paused_auth_error"})
            ]
            engine("snapshot")  # the credential is fixed
            assert await resume_run(h.ctx, run_id, who=TESTER) == "running"
            run = await h.wait(run_id)
        finally:
            stop.set()
            await worker
        assert (run["status"], run["cycle"]) == ("completed", 2)
        assert set(await h.results(run_id, 2)) == {*ITEM[1:], "K006", "K007"}


async def test_roll_audit_names_no_endpoint(
    on_tables: Tables,
    service: ScriptedService,
    tmp_path: Path,
) -> None:
    """A roll whose key table's target refuses the connect audits the target's id alone: the host and the
    port its message names for whoever creates a run reach no audit row."""
    _, harness = on_tables(ITEMS)
    async with harness as h:
        run_id = await _rolling(h, service)
        (tmp_path / "dsn_tables").write_text("postgresql://keyreader@127.0.0.1:1/keys")
        run = await h.finish(run_id)
        assert (run["status"], run["cycle"], run["counters"]["match"]) == ("completed", 2, 5)
        assert await audited(h, run_id, "rolling_resolve_failed") == [
            ("rolling_resolve_failed", {"error": "the key source: cannot connect to target db-a"})
        ]
        async with h.ctx.pool.connection() as conn:
            rows = await (await conn.execute("SELECT detail::text FROM ds.audit_log")).fetchall()
        assert rows and not any("port 1" in text or "127.0.0.1" in text for (text,) in rows)
        told = r'^the key source: cannot connect to target db-a: .* at "127\.0\.0\.1", port 1 failed'
        with pytest.raises(tables.Unconnected, match=told):
            await _rolling(h, service)  # whoever creates a run is told where


async def test_create_outage_is_422(
    on_tables: Tables,
    service: ScriptedService,
    engine: Failing,  # noqa: F811
) -> None:
    """A key table that cannot be read when a run is created refuses the run as bad input is refused (422;
    ``ds run`` exits 2), by its code alone, and no run is written."""
    _, harness = on_tables(ITEMS)
    async with harness as h:
        engine("census", psycopg.OperationalError("the server at keys.bank.example closed the connection"))
        with pytest.raises(HTTPException) as refused, _runner_errors():
            await _rolling(h, service)
        assert (refused.value.status_code, refused.value.detail) == (
            422,
            "the key source could not be read (OperationalError)",
        )
        async with h.ctx.pool.connection() as conn:
            assert (await repo.list_runs(conn))[0] == []


async def test_key_work_leaves_the_loop(
    on_tables: Tables,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key set may hold a million keys: the rule over it, its masking and its sealing run in a thread when
    a run is created and when a soak rolls, so neither stalls the API or the other runs' keys."""
    loop, work = threading.get_ident(), []

    def watched(name: str, real: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            work.append((name, threading.get_ident() == loop))
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(key_sets, "_fit", watched("rule", key_sets._fit))
    monkeypatch.setattr(running, "run_keys", watched("mask", key_sets.run_keys))
    monkeypatch.setattr(cycle, "run_keys", watched("mask", key_sets.run_keys))
    monkeypatch.setattr(Sealer, "seal_keys", watched("seal", Sealer.seal_keys))
    _, harness = on_tables(ITEMS)
    async with harness as h:
        run = await h.finish(await _rolling(h, service))
        listed = await create_run(h.ctx, await h.spec(spec_text()), "batch", keys=ITEM, who=TESTER)
        assert run["cycle"] == 2 and (await h.run(listed))["status"] == "running"
    # The creation, the roll and the run of given keys: none on the loop's thread.
    assert Counter(work) == {("rule", False): 3, ("mask", False): 3, ("seal", False): 3}
