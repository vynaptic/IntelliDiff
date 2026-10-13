"""Readiness's measures (docs/mvp/READINESS.md §4, §14): what the worker's pass stores of an ended cycle,
once, with its audit row in the same transaction; the bases; how its discrepant keys moved; late, failed
and excluded cycles; the backfill and its throttle; the purge it waits for, or that it finds started; what
the store's statements then give the evaluation; and what a pair's brief reads, on one snapshot."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from deltasleuth import approvals
from deltasleuth import readiness as judging
from deltasleuth.auth.principal import WORKER
from deltasleuth.connectors.base import utc_iso
from deltasleuth.core import jsonio, readiness
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import ChangeType, Direction, RootCause, Severity
from deltasleuth.reports import accuracy
from deltasleuth.runner import create_run, run_worker
from deltasleuth.runner import readiness as measuring
from deltasleuth.runner.stop import StopConditions
from deltasleuth.store import db, repo
from tests.conftest import TESTER
from tests.unit.core import ready
from tests.unit.reports.conftest import GRAPH_SPEC, graph_row
from tests.unit.store.four_eyes import (
    approved,
    drawn,
    drawn_labelling,
    label,
    person,
    sampled,
    upload,
)

from .conftest import Harness, ScriptedService, doc, spec_text, until
from .measuring import (
    AT,
    KEYS,
    MATCH,
    MISMATCH,
    PAIR,
    RULES,
    SCAN,
    SCANNED,
    SOAK,
    TABLES,
    audited,
    ended,
    measure,
    measures,
    packed,
    scanned,
    scored,
    signed,
    started,
    stored,
)

pytestmark = pytest.mark.db

READ = "read to its end in 1 snapshot at 2026-10-03 04:00 UTC"
LISTED = "is not a census: its keys are the spec's inline list and no side is a table to check them against"


async def test_a_cycle_is_measured_once_stored_and_audited_in_one_transaction(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = await started(harness)
    await ended(harness, run_id, MATCH | {"K001": MISMATCH})
    insert = repo.insert_audit

    async def refused(*args: Any) -> Any:
        raise RuntimeError("the audit row is refused")

    monkeypatch.setattr(repo, "insert_audit", refused)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.readiness"):
        await measure(harness)  # never raises
    assert caplog.messages == [
        "readiness pass failed: RuntimeError",
        "readiness evaluation failed: RuntimeError",  # it records the settings: refused too
    ]
    assert (
        await measures(harness, run_id) is None and await audited(harness) == []
    )  # neither without the other
    monkeypatch.setattr(repo, "insert_audit", insert)
    await measure(harness)
    record = await measures(harness, run_id)
    assert record is not None and record.pop("measured_at").startswith(f"{datetime.now(UTC):%Y-%m-%d}")
    async with harness.ctx.pool.connection() as conn:
        spec_id = (await repo.get_run(conn, run_id))["spec_id"]  # type: ignore[index]
    assert record == {
        "format": 1, "pair": PAIR, "spec_id": spec_id, "version": "1.0.0", "mode": "soak",
        "keys_source": "list", "needs_approval": ["clear", "known_diffs"], "complete": True,
        "evidence": True, "incomplete": [], "basis": "keys",
        "basis_text": f"cycle 1 of run {run_id} {LISTED}", "total": 8, "compared": 8, "different": 1,
        "indeterminate": 0, "cancelled": 0, "outstanding": 0, "absent_both": 0, "api_uncomparable": 0,
        "suppressed": 0, "unexplained_critical": 0, "results": 8, "draw_id": None, "sample_status": None,
        "sample_strata": None, "selection_ok": None, "changes": None, "graph": None,
        "fails": {"standard": ["population", "targets"], "high": ["population", "targets"]},
        "late": False,
    }  # fmt: skip
    [row] = await audited(harness)
    kept = await measures(harness, run_id)
    assert (row["actor"], row["object"], row["chain"]["seq"] > 0) == ("system:worker", "readiness", True)
    assert row["detail"] == {"cycles": [[run_id, 1, sha256_hex(jsonio.dumps(kept, sort_keys=True))]]}
    assert len(jsonio.dumps(kept)) < 1_000  # counts, ids, codes and a text: no key, no value
    assert not any(key in jsonio.dumps(kept) for key in KEYS)
    await measure(harness)  # once: a measured cycle is not measured again
    assert await measures(harness, run_id) == kept and len(await audited(harness)) == 1
    async with harness.ctx.pool.connection() as conn:
        assert not await repo.set_readiness(conn, run_id, 1, {"format": 1})
        assert await repo.unmeasured_cycles(conn) == await repo.unmeasured_cycles(conn, backfill=True) == []


async def test_bug_rule_critical_counted(harness: Harness) -> None:
    """A CRITICAL finding counts as unexplained unless a rule that is no bug rule suppresses it, as the
    sign-off counts them; on a partial cycle too."""
    run_id = await started(harness)
    results = MATCH | {
        "K001": ("MISMATCH", [("CRITICAL", None, None)]),
        "K002": ("KNOWN_DIFF", [("CRITICAL", "KD-BUG", "bug")]),
        "K003": ("KNOWN_DIFF", [("CRITICAL", "KD-ACCEPTED", "accepted")]),
        "K004": ("MISMATCH", [("HIGH", None, None), ("CRITICAL", None, None)]),
        "K005": MISMATCH,
        "K008": None,  # never read: the cycle is partial
    }
    await ended(harness, run_id, results)
    await measure(harness)
    record = await measures(harness, run_id)
    assert record is not None
    assert (record["unexplained_critical"], record["different"], record["suppressed"]) == (3, 4, 2)
    assert (record["incomplete"], record["evidence"], record["cancelled"]) == (["cancelled"], False, 1)
    # 4 discrepant keys of 8 are over what any target allows whatever the unread key shows.
    assert record["fails"] == {"standard": ["critical", "targets"], "high": ["critical", "targets"]}


async def test_change_identities(harness: Harness) -> None:
    """The DIFFERENT keys of a cycle are its persistent and new ones, ``different`` plus ``outstanding``;
    those of the cycle before are the persistent, fixed and other ones; a key without a result in the cycle
    counts ``other``."""
    run_id = await started(harness)
    first = MATCH | {
        "K001": MISMATCH, "K002": MISMATCH, "K003": ("NOT_FOUND_A", []),
        "K004": ("KNOWN_DIFF", [("HIGH", "KD-BUG", "bug")]),
        "K007": ("KNOWN_DIFF", [("HIGH", "KD-ACCEPTED", "accepted")]),  # accepted: never discrepant
    }  # fmt: skip
    second = MATCH | {
        "K001": MISMATCH,  # persistent
        "K002": "MATCH",  # fixed
        "K003": "INCONCLUSIVE",  # other: read, and neither discrepant nor identical
        "K005": ("NOT_FOUND_B", []),  # new
        "K006": "MISMATCH?",  # new, and still owed a recheck
    }
    await ended(harness, run_id, first, keys=[k for k in KEYS if k != "K004"])  # K004 is gone: other
    del second["K004"]
    await ended(harness, run_id, second)
    await ended(harness, run_id, dict.fromkeys(second, "MATCH"), keys=KEYS[:7])
    async with harness.ctx.pool.connection() as conn:  # cycle 3's purge started before it is measured
        assert await repo.start_purge(conn, run_id, 3, "retention standard") is not None
    await ended(harness, run_id, dict.fromkeys(KEYS[:7], "MATCH"))
    await measure(harness)
    one, two, three, four = [await measures(harness, run_id, cycle) for cycle in (1, 2, 3, 4)]
    assert one is not None and two is not None and three is not None and four is not None
    assert one["changes"] is None  # a first cycle
    assert two["changes"] == {"new": 2, "persistent": 1, "fixed": 1, "other": 2}
    changes = two["changes"]
    assert changes["persistent"] + changes["new"] == two["different"] + two["outstanding"] == 3
    assert changes["persistent"] + changes["fixed"] + changes["other"] == one["different"] == 4
    assert (two["outstanding"], two["incomplete"], two["evidence"]) == (1, ["outstanding"], False)
    assert (three["late"], three["changes"], three["unexplained_critical"]) == (True, None, None)
    assert four["changes"] is None  # the cycle before is purging: its keys are going
    async with harness.ctx.pool.connection() as conn:
        assert await repo.cycle_changes(conn, run_id, 2) == changes


async def test_fails_per_tier_and_the_bases_of_a_scan(harness: Harness) -> None:
    """A scan read whole is a census, with or without its graph parity (L2); one stopped before the end or
    cut is partial; counters older than the fields a measure reads are unknown."""
    whole = await scanned(harness)
    await ended(harness, whole, MATCH | {"K001": MISMATCH})  # 1 of 1,000: within 0.1%, over 0.01%
    await ended(harness, whole, MATCH | {"K001": MISMATCH, "K002": MISMATCH})
    stopped = await scanned(harness, done=False, rows_a=812_000, rows_b=811_950)
    await ended(harness, stopped, MATCH | {"K001": MISMATCH, "K002": MISMATCH})
    cut = await scanned(harness)
    async with harness.ctx.pool.connection() as conn:
        await repo.set_keys_cut(conn, cut, {"taken": 5_000})
    await ended(harness, cut, MATCH | {"K001": MISMATCH, "K002": MISMATCH})
    old = await scanned(harness)
    await ended(harness, old, MATCH, counters={"total": 2})
    graph_id = await stored(harness, GRAPH_SPEC)
    unrun = await scanned(harness, graph_id)
    await ended(harness, unrun, MATCH)
    summary = {"groups_a": 3, "groups_b": 3, "groups_equal": 2, "groups_different": 1, "split": 1}
    run = await scanned(harness, graph_id, graph=summary)
    rows = [
        graph_row("CLUSTER_SPLIT", "m1"),
        graph_row("GROUP_MEMBERS_DIFFER", "m2"),
        graph_row("GROUP_MEMBERS_DIFFER", "m3"),
    ]
    async with harness.ctx.pool.connection() as conn:
        await repo.replace_graph_diffs(conn, run, 1, rows)
    await ended(harness, run, MATCH)
    await measure(harness)

    async def read(run_id: str, cycle: int = 1, *names: str) -> tuple[Any, ...]:
        record = await measures(harness, run_id, cycle)
        assert record is not None
        return tuple(
            record[name] for name in names or ("basis", "basis_text", "incomplete", "evidence", "fails")
        )

    nothing = {"standard": [], "high": []}
    assert await read(whole) == ("census", READ, [], True, {"standard": [], "high": ["targets"]})
    assert await read(whole, 1, "total", "compared", "different", "results") == (1_000, 1_000, 1, 8)
    assert await read(whole, 2, "fails") == ({"standard": ["targets"], "high": ["targets"]},)
    assert await read(stopped) == (
        "partial",
        "scanned only part of its tables (rows_a 812,000, rows_b 811,950 read; the scan stopped before the"
        " end)",
        ["scan_partial"],
        False,
        nothing,
    )
    assert await read(cut) == ("partial", "was cut at 5,000 keys (maxKeys)", ["cut"], False, nothing)
    unknown = {"standard": ["unknown"], "high": ["unknown"]}
    assert await read(old) == ("unknown", READ, ["nothing_compared"], False, unknown)
    assert await read(old, 1, "total", "compared", "different") == (2, None, None)
    codes = dict.fromkeys(readiness.GRAPH_CODES, 0)
    assert await read(whole, 1, "graph") == (None,)  # its spec declares none
    assert await read(unrun, 1, "basis", "evidence", "graph") == ("census", True, {"computed": False} | codes)
    assert await read(run, 1, "graph") == (
        {"computed": True} | codes | {"CLUSTER_SPLIT": 1, "GROUP_MEMBERS_DIFFER": 2},
    )


async def test_the_bases_of_key_lists_and_samples(harness: Harness) -> None:
    """A key list is a census only when its census counts side a; a hash sample's frame makes it a sample."""
    table = {
        "basis": "table", "target": "svc-a", "relation": "public.accounts", "where": None, "rows": 8,
        "skipped": 0, "missing": 0, "extra": 0, "at": AT,
    }  # fmt: skip
    runs = {}
    for name, side_a in (("counted", table), ("lacking", table | {"missing": 2}), ("pending", None)):
        runs[name] = await started(harness, keys_source="explicit", tables=TABLES)
        if side_a is not None:
            async with harness.ctx.pool.connection() as conn:
                assert await repo.set_census(conn, runs[name], {"cycle": 1, "at": AT, "a": side_a})
        await ended(harness, runs[name], MATCH)
    runs["given"] = await started(harness, keys_source="explicit")
    await ended(harness, runs["given"], MATCH | {"K001": MISMATCH})
    version = sampled()
    async with harness.ctx.pool.connection() as conn:
        amy = await person(conn, "amy", "spec_author")
        sampled_id = await upload(conn, version, amy)
        passed, draw_id = await drawn(conn, sampled_id, version, amy)
    async with harness.ctx.pool.connection() as conn:
        failed, _ = await drawn(conn, sampled_id, version, amy, discrepant=["C001"])
    async with harness.ctx.pool.connection() as conn:
        relabelled, _ = await drawn(conn, sampled_id, version, amy)
        await conn.execute(
            "UPDATE ds.run_keys SET stratum = 'segment=other' WHERE run_id = %s", (relabelled,)
        )
    await measure(harness)
    found = {name: await measures(harness, run_id) for name, run_id in runs.items()}
    at = "cycle 1 of run {}".format
    assert [(found[name]["basis"], found[name]["evidence"]) for name in runs] == [  # type: ignore[index]
        ("census", True), ("keys", False), ("keys", False), ("keys", False),
    ]  # fmt: skip
    assert found["counted"]["basis_text"] == (  # type: ignore[index]
        "its key set checked against public.accounts in 1 snapshot at 2026-10-03 04:00 UTC: 0 key(s) missing,"
        " 0 outside it"
    )
    assert found["lacking"]["basis_text"] == (  # type: ignore[index]
        f"{at(runs['lacking'])} is not a census: its key set lacks 2 key(s) of side a's table public.accounts"
        " (counted 2026-10-03 04:00 UTC)"
    )
    assert (
        found["pending"]["basis_text"]
        == f"{at(runs['pending'])} is not a census: side a's key census is pending"
    )  # type: ignore[index]
    assert found["given"]["basis_text"] == (  # type: ignore[index]
        f"{at(runs['given'])} is not a census: its keys were given with the run and no side is a table to"
        " check them against"
    )
    assert found["given"]["fails"] == {"standard": [], "high": []}  # type: ignore[index]  # those keys only
    record = await measures(harness, passed)
    assert record is not None
    names = ("basis", "evidence", "draw_id", "sample_status", "selection_ok", "fails")
    assert tuple(record[name] for name in names) == (
        "sample", True, draw_id, "passed", True, {"standard": [], "high": ["targets"]},
    )  # fmt: skip
    assert record["sample_strata"] == [
        {"label": "segment=retail", "tier": "standard"},
        {"label": "segment=other", "tier": "standard"},
    ]
    assert (record["keys_source"], record["needs_approval"], record["changes"]) == (
        "hash_sample",
        ["sampling"],
        None,
    )
    spent = await measures(harness, failed)
    assert spent is not None and (spent["sample_status"], spent["fails"]["standard"]) == (
        "failed",
        ["targets"],
    )
    edited = await measures(harness, relabelled)
    assert edited is not None and (edited["selection_ok"], edited["sample_status"]) == (False, "passed")


async def test_late_failed_and_excluded_cycles(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A cycle whose purge started is measured from its tombstone alone; a measure that fails is tried again
    1, 2, 4 and 8 hours later, then never; one-shot and captured-sample cycles are marked, never measured."""
    late = await scanned(harness)
    await ended(harness, late, MATCH | {"K001": ("MISMATCH", [("CRITICAL", None, None)])})
    listed = await started(harness)
    await ended(harness, listed, MATCH | {"K001": ("MISMATCH", [("CRITICAL", None, None)])})
    async with harness.ctx.pool.connection() as conn:
        for run_id in (late, listed):
            assert await repo.start_purge(conn, run_id, 1, "retention standard") is not None
    one_shot = await started(harness, mode="one_shot")
    captured = await started(harness, mode="batch", samples_dir="/captured")
    for run_id in (one_shot, captured):
        async with harness.ctx.pool.connection() as conn:
            await repo.end_cycle(conn, run_id)
    assert await measures(harness, one_shot) == {"format": 1, "excluded": "one_shot"}
    assert await measures(harness, captured) == {"format": 1, "excluded": "captured_samples"}
    failing = await started(harness)
    await ended(harness, failing, MATCH)
    measured = measuring._measured

    async def broken(ctx: Any, conn: Any, row: Any, specs: Any) -> Any:
        if row["run_id"] == failing:
            await repo.count_results(conn, failing, 1)
            raise KeyError("a bug")
        return await measured(ctx, conn, row, specs)

    monkeypatch.setattr(measuring, "_measured", broken)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.readiness"):
        await measure(harness)
    assert caplog.messages == [f"readiness: cycle 1 of run {failing} not measured: KeyError"]
    purged, tombstone = await measures(harness, late), await measures(harness, listed)
    assert purged is not None and tombstone is not None
    names = ("late", "basis", "basis_text", "unexplained_critical", "results", "changes", "graph", "complete")
    assert tuple(purged[name] for name in names) == (True, "unknown", None, None, None, None, None, True)
    assert (purged["total"], purged["different"], purged["evidence"]) == (1_000, 1, True)
    assert purged["fails"] == {"standard": ["unknown"], "high": ["targets", "unknown"]}
    assert (tombstone["late"], tombstone["basis"]) == (True, "keys")  # its census needs no purged row
    assert tombstone["fails"]["standard"] == ["population", "targets", "unknown"]
    assert await measures(harness, one_shot) == {"format": 1, "excluded": "one_shot"}
    assert [row["detail"]["cycles"] for row in await audited(harness)] == [
        [[run_id, 1, sha256_hex(jsonio.dumps(record, sort_keys=True))] for run_id, record in
         sorted(((listed, tombstone), (late, purged)), key=lambda pair: pair[0], reverse=True)]
    ]  # fmt: skip
    failed = await measures(harness, failing)
    assert failed is not None and failed.pop("measured_at")
    async with harness.ctx.pool.connection() as conn:
        spec_id = (await repo.get_run(conn, failing))["spec_id"]  # type: ignore[index]
    unknown = {"standard": ["unknown"], "high": ["unknown"]}
    assert failed == {
        "format": 1,
        "pair": PAIR,
        "spec_id": spec_id,
        "error": "KeyError",
        "attempts": 1,
        "fails": unknown,
    }

    async def after(hours: float) -> dict[str, Any]:
        """The failed record once its last try was ``hours`` ago and a pass ran."""
        tried = utc_iso(datetime.now(UTC) - timedelta(hours=hours))
        async with harness.ctx.pool.connection() as conn:
            await conn.execute(
                """UPDATE ds.cycles SET readiness = jsonb_set(readiness, '{measured_at}', %s)
                   WHERE run_id = %s""",
                (Jsonb(tried), failing),
            )
        await measure(harness)
        record = await measures(harness, failing)
        assert record is not None
        return record

    assert (await after(0.9))["attempts"] == 1  # not yet
    assert (await after(1.1))["attempts"] == 2
    assert (await after(1.9))["attempts"] == 2
    assert (await after(2.1))["attempts"] == 3
    assert (await after(4.1))["attempts"] == 4
    assert (await after(7.9))["attempts"] == 4
    final = await after(8.1)
    assert (final["attempts"], final["final"], final["fails"]) == (5, True, unknown)
    assert (await after(1_000))["attempts"] == 5  # an unknown point for good
    assert len(await audited(harness)) == 1  # a failed measure is not audited
    # A failure that was not final is measured, and audited, once what broke it is mended.
    mended = await started(harness)
    await ended(harness, mended, MATCH)
    failing = mended
    await measure(harness)
    assert (await measures(harness, mended))["error"] == "KeyError"  # type: ignore[index]
    monkeypatch.setattr(measuring, "_measured", measured)
    record = await after(1.1)
    assert "error" not in record and record["complete"] and len(await audited(harness)) == 2


async def test_only_a_lost_connection_ends_a_batch(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A measure whose statement the server cancels (a statement_timeout: QueryCanceled, an
    OperationalError as a lost connection is) is a failed measure: its error record, the back-off, the
    rest of the batch stored and the pairs judged. A lost connection alone leaves the batch to the next
    pass."""
    big = await scanned(harness)
    for _ in range(2):
        await ended(harness, big, MATCH)
    other = await scanned(harness, await stored(harness, SOAK.replace("unit-runner", "unit-other")))
    await ended(harness, other, MATCH)
    count, met = repo.count_results, {"lost": True}

    async def slow(conn: repo.Conn, run_id: str, cycle: int) -> int:
        if (run_id, cycle) == (big, 2):
            if met["lost"]:
                raise psycopg.OperationalError("the connection is lost")
            await conn.execute("SET LOCAL statement_timeout = '50ms'")
            await conn.execute("SELECT pg_sleep(1)")  # the server cancels it, as it would a long statement
        return await count(conn, run_id, cycle)

    monkeypatch.setattr(repo, "count_results", slow)
    state = measuring.State()
    with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.readiness"):
        await measuring.watch(harness.ctx, state)
        assert caplog.messages == ["readiness pass failed: OperationalError"]
        assert [await measures(harness, *at) for at in ((big, 1), (big, 2), (other, 1))] == [None] * 3
        assert await audited(harness, "readiness.changed") == []  # each pair waits for its cycles
        met["lost"] = False
        await measuring.watch(harness.ctx, state)
    assert caplog.messages[1:] == [f"readiness: cycle 2 of run {big} not measured: QueryCanceled"]
    failed, first, third = [await measures(harness, *at) for at in ((big, 2), (big, 1), (other, 1))]
    assert failed is not None and (failed["error"], failed["attempts"]) == ("QueryCanceled", 1)
    assert first is not None and third is not None and first["complete"] and third["complete"]
    assert [len(row["detail"]["cycles"]) for row in await audited(harness)] == [2]
    rows = await audited(harness, "readiness.changed")  # judged at once, the failed measure an unknown point
    assert sorted((row["detail"]["pair"], row["detail"]["to"]) for row in rows) == [
        ("unit-other", "not_ready"), (PAIR, "not_ready"),
    ]  # fmt: skip
    await measuring.watch(harness.ctx, state)  # within the hour: not tried again
    assert (await measures(harness, big, 2))["attempts"] == 1  # type: ignore[index]


async def test_a_start_purge_race(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A purge's start waits for the batch measuring its cycle; a measure waits for a purge that is starting,
    then reads the tombstone alone."""
    first, second = await started(harness), await started(harness)
    for run_id in (first, second):
        await ended(harness, run_id, MATCH)
    entered, go, store = asyncio.Event(), asyncio.Event(), repo.set_readiness

    async def held(conn: repo.Conn, run_id: str, cycle: int, record: Any) -> bool:
        if run_id == first:
            entered.set()
            await go.wait()
        return await store(conn, run_id, cycle, record)

    async def purge(run_id: str) -> repo.Row | None:
        async with harness.ctx.pool.connection() as conn:
            return await repo.start_purge(conn, run_id, 1, "retention standard")

    monkeypatch.setattr(repo, "set_readiness", held)
    passing = asyncio.create_task(measure(harness))
    await entered.wait()
    purging = asyncio.create_task(purge(first))
    await asyncio.sleep(0.3)
    assert not purging.done()  # it waits for the batch that holds the tombstone
    go.set()
    await passing
    assert (await purging)["purging_at"] is not None  # type: ignore[index]
    measured = await measures(harness, first)
    assert measured is not None and (measured["late"], measured["results"]) == (False, 8)
    monkeypatch.setattr(repo, "set_readiness", store)
    third = await started(harness)
    await ended(harness, third, MATCH)
    async with harness.ctx.pool.connection() as conn:  # the purge's first transaction, not yet committed
        assert await repo.start_purge(conn, third, 1, "retention standard") is not None
        passing = asyncio.create_task(measure(harness))
        await asyncio.sleep(0.3)
        assert not passing.done()  # it waits for the purge's start
    await passing
    late = await measures(harness, third)
    assert late is not None and (late["late"], late["results"]) == (True, None)


async def test_the_backfill_and_its_throttle(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cycles ended since 014 are measured first, 10 a pass; those ended before it newest first, 10 a batch
    (and those whose purge started, up to 100), a batch every 10 s at most."""
    async with harness.ctx.pool.connection() as conn:
        applied = await repo.applied_at(conn, db.READINESS)
    assert applied is not None
    before = await started(harness)
    for cycle in range(1, 15):
        await ended(harness, before, MATCH, at=applied - timedelta(days=30) + timedelta(hours=cycle))
    since = await started(harness)
    for cycle in range(1, 13):
        await ended(harness, since, MATCH, at=applied + timedelta(seconds=cycle))
    async with harness.ctx.pool.connection() as conn:
        for cycle in (1, 12, 13):
            assert await repo.start_purge(conn, before, cycle, "retention standard") is not None
        oldest = {"run_id": before, "cycle": 1, "ended_at": applied - timedelta(days=30, hours=-1)}
        assert await repo.backfill_owed(conn)  # so each run of the pairs is probed, not the backlog read
        assert await repo.unmeasured_pairs(conn, [PAIR, "unit-none"]) == (
            {"cycles": 26, "backfill": 14},
            {PAIR: {"cycles": 26, "oldest": oldest}},
        )

    async def unmeasured(run_id: str) -> list[int]:
        async with harness.ctx.pool.connection() as conn:
            rows = await repo._rows(
                conn,
                "SELECT cycle FROM ds.cycles WHERE run_id = %s AND readiness IS NULL ORDER BY cycle",
                (run_id,),
            )
        return [row["cycle"] for row in rows]

    state = measuring.State()
    await measuring.watch(harness.ctx, state)
    assert await unmeasured(since) == [1, 2]  # the newest 10 first
    assert await unmeasured(before) == [1, 2]  # 10 read in full and the 2 purging among them
    assert [len(row["detail"]["cycles"]) for row in await audited(harness)] == [10, 12]
    await measuring.watch(harness.ctx, state)
    assert await unmeasured(since) == [] and await unmeasured(before) == [1, 2]  # no backfill within 10 s
    monkeypatch.setattr(measuring, "BACKFILL_EVERY_S", 0)
    await measuring.watch(harness.ctx, state)
    assert await unmeasured(before) == []
    assert [len(row["detail"]["cycles"]) for row in await audited(harness)] == [10, 12, 2, 2]
    async with harness.ctx.pool.connection() as conn:
        assert await repo.unmeasured_pairs(conn, [PAIR]) == ({"cycles": 0, "backfill": 0}, {})
        assert not await repo.backfill_owed(conn)
    lates = [(await measures(harness, before, cycle))["late"] for cycle in (1, 2, 12, 13, 14)]  # type: ignore[index]
    assert lates == [True, False, True, True, False]
    # With no backfill owed, the few cycles that wait are read whole, whichever pairs are asked for.
    later = [await ended(harness, since, MATCH) for _ in range(2)]
    async with harness.ctx.pool.connection() as conn:
        owed, waiting = await repo.unmeasured_pairs(conn, [])
        ended_at = (await repo.get_cycle(conn, since, later[0]))["ended_at"]  # type: ignore[index]
    assert (owed, list(waiting)) == ({"cycles": 2, "backfill": 0}, [PAIR])
    assert waiting[PAIR] == {
        "cycles": 2, "oldest": {"run_id": since, "cycle": later[0], "ended_at": ended_at},
    }  # fmt: skip


async def test_the_worker_measures_and_readiness_never_fails_a_run(
    harness: Harness,
    service: ScriptedService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for key in ("K001", "K002", "K003"):
        service.both(key, doc(key), doc(key))
    spec_id = await harness.spec(spec_text())
    unmeasured = repo.unmeasured_cycles

    async def failing(conn: repo.Conn, **options: Any) -> Any:
        raise RuntimeError("a bug in readiness")

    monkeypatch.setattr(repo, "unmeasured_cycles", failing)
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(harness.ctx, stop=stop))
    try:
        with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.readiness"):
            run_id = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
            assert (await harness.wait(run_id))["status"] == "completed"  # the run never notices
            await until(lambda: "readiness pass failed: RuntimeError" in caplog.messages)
        assert await measures(harness, run_id) is None
        monkeypatch.setattr(repo, "unmeasured_cycles", unmeasured)
        found: list[Any] = []

        async def measured() -> None:
            while not found:
                if (record := await measures(harness, run_id)) is not None:
                    found.append(record)
                await asyncio.sleep(0.02)

        await asyncio.wait_for(measured(), 30)
    finally:
        stop.set()
        await worker
    assert (found[0]["pair"], found[0]["total"], found[0]["complete"], found[0]["basis"]) == (
        PAIR,
        3,
        True,
        "keys",
    )
    assert len(await audited(harness)) == 1 and not worker.exception()


# -- what the store then gives the evaluation (§14) -------------------------------------------------------


async def points(h: Harness, pair: str = PAIR) -> list[dict[str, Any]]:
    """The pair's measured cycles, as the store holds them."""
    async with h.ctx.pool.connection() as conn:
        return await repo._rows(
            conn,
            """SELECT run_id, cycle, ended_at, readiness, sample FROM ds.cycles
               WHERE readiness->>'pair' = %s ORDER BY ended_at""",
            (pair,),
        )


def named(found: dict[str, Any]) -> dict[str, Any]:
    """A series by its cycles' ids."""

    def at(row: Any) -> Any:
        return row and (row["run_id"], row["cycle"])

    return {
        name: [at(row) for row in value]
        if isinstance(value, list)
        else value
        if name == "cycles"
        else at(value)
        for name, value in found.items()
    }


async def test_the_series_reads_a_pair_as_the_evaluation_takes_it(harness: Harness) -> None:
    """``repo.readiness_series`` gives, in one statement, what the pure reading of the same points gives: at
    either tier, up to a day, and for a pair without evidence."""
    T0 = datetime.now(UTC) - timedelta(days=3)
    old_id = await stored(harness)
    new_id = await stored(harness, SOAK.replace('version: "1.0.0"', 'version: "1.1.0"'))
    clean, one, two = MATCH, MATCH | {"K001": MISMATCH}, MATCH | {"K001": MISMATCH, "K002": MISMATCH}
    critical = MATCH | {"K008": None, "K001": ("MISMATCH", [("CRITICAL", None, None)])}
    a, b = await scanned(harness, old_id), await scanned(harness, new_id)
    c = await scanned(harness, old_id)
    history = [  # 1 of 1,000 fails 0.01% only; 2 fail both
        (a, clean), (a, one), (a, clean), (a, two), (a, clean), (b, clean), (b, clean), (c, clean),
        (c, clean),
        (a, clean), (c, one), (c, clean), (c, clean), (a, critical),
    ]  # fmt: skip
    for hour, (run_id, results) in enumerate(history):
        await ended(harness, run_id, results, at=T0 + timedelta(hours=hour))
    other = await started(harness, await stored(harness, SOAK.replace("unit-runner", "unit-other")))
    await ended(harness, other, MATCH | {"K001": None})
    await measure(harness)
    await measure(harness)  # 15 cycles ended before 014 was applied here: two backfill batches
    stored_points = await points(harness)
    assert len(stored_points) == 14 and {p["readiness"]["spec_id"] for p in stored_points} == {old_id, new_id}
    high = readiness.policy(None, PAIR).__class__(readiness.Gates(tier="high"), None, None, None, "", ())
    async with harness.ctx.pool.connection() as conn:
        for policy in (ready.POLICY, high):
            for limit in (
                None,
                T0 + timedelta(hours=4, minutes=30),
                T0 + timedelta(hours=9, minutes=30),
                T0 - timedelta(hours=1),
            ):
                asked = {
                    "pair": PAIR,
                    "tier": policy.gates.tier,
                    "needed": policy.gates.cycles,
                    "window": readiness.window(policy),
                    "until": limit,
                }
                found = (await repo.readiness_series(conn, [asked]))[PAIR]
                upto = [p for p in stored_points if limit is None or p["ended_at"] <= limit]
                assert named(found) == named(ready.series(upto, policy)), (policy.gates.tier, limit)
        both = await repo.readiness_series(
            conn,
            [
                {"pair": PAIR, "tier": "standard", "needed": 3, "window": 3, "until": None},
                {"pair": "unit-other", "tier": "high", "needed": 3, "window": 60, "until": None},
                {"pair": "unit-none", "tier": "standard", "needed": 3, "window": 60, "until": None},
            ],
        )
    current = named(both[PAIR])
    # Its newest breaks: the partial CRITICAL cycle and, before the other version, the one over its target.
    assert current["versions"] == [(c, 5), (b, 2)] and current["breaks"] == [(a, 7), (a, 4)]
    assert current["points"] == [] and current["critical"] == (a, 7) and current["targets"] is None
    assert both[PAIR]["versions"][0]["ended_at"] == T0 + timedelta(hours=12)
    assert len(current["trend"]) == 10 and current["newest"] is None
    assert named(both["unit-other"]) == {
        "versions": [], "breaks": [], "points": [], "trend": [], "critical": None, "targets": None,
        "newest": (other, 1), "cycles": 1,
    }  # fmt: skip
    assert named(both["unit-none"])["newest"] is None and both["unit-none"]["cycles"] == 0
    # The evaluation of the stored series: version 1.0.0 is current, and its streak is one cycle long.
    governance = {"signoffs": [], "cycles": [ready.cycle(both[PAIR]["versions"][0])], "soaks": []}
    item = readiness.evaluate(
        PAIR, ready.POLICY, both[PAIR], governance, ready.seen(), now=T0 + timedelta(hours=14)
    )
    assert (item["state"], item["version"], item["convergence"]["streak"]) == ("not_ready", "1.0.0", 0)
    assert ready.said(item)["critical"][1].startswith(
        f"cycle 7 of run {a}, which is not evidence (partial), has 1 key(s) with unexplained CRITICAL"
    )
    assert ready.said(item)["population"] == ("met", f"cycle 5 of run {c} observed side a whole ({READ})")


async def test_the_series_keeps_what_is_behind_its_unknown_points(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind an unknown point the series still gives the newest cycle that failed a gate and the evidence
    since, so that convergence is not met where no unknown point could complete it; a record of another
    format is an unknown point too; and the cycles of a pair without evidence are counted up to a cap."""
    T0 = datetime.now(UTC) - timedelta(days=1)
    run_id = await scanned(harness)
    for hour, results in enumerate([MATCH | {"K001": MISMATCH, "K002": MISMATCH}, MATCH, MATCH]):
        await ended(harness, run_id, results, at=T0 + timedelta(hours=hour))
    other = await started(harness, await stored(harness, SOAK.replace("unit-runner", "unit-other")))
    for hour in range(4):  # each lacks a key: no cycle of it is evidence
        await ended(harness, other, MATCH | {"K001": None}, at=T0 + timedelta(hours=hour))
    await measure(harness)
    spec_id = (await harness.run(run_id))["spec_id"]
    asked = {"pair": PAIR, "tier": "standard", "needed": 3, "window": 60, "until": None}
    at = f"cycle {{}} of run {run_id}"

    async def read(cycle: int, record: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The series, and the pair judged from it, once ``cycle``'s measures are ``record``."""
        async with harness.ctx.pool.connection() as conn:
            await conn.execute(
                f"UPDATE ds.cycles SET readiness = {record} WHERE run_id = %s AND cycle = %s",  # noqa: S608
                (run_id, cycle),
            )
            found = (await repo.readiness_series(conn, [asked]))[PAIR]
        assert named(found) == named(ready.series(await points(harness), ready.POLICY))
        governance = {"signoffs": [], "cycles": [ready.cycle(found["versions"][0])], "soaks": []}
        now = T0 + timedelta(hours=5)
        return named(found), readiness.evaluate(PAIR, ready.POLICY, found, governance, ready.seen(), now=now)

    failed = {
        "format": 1,
        "pair": PAIR,
        "spec_id": spec_id,
        "error": "KeyError",
        "attempts": 5,
        "final": True,
    }
    failed |= {"fails": {"standard": ["unknown"], "high": ["unknown"]}, "measured_at": AT}
    series, item = await read(3, f"'{jsonio.dumps(failed)}'::jsonb")  # its measure failed for good
    assert (series["breaks"], series["points"]) == ([(run_id, 3), (run_id, 1)], [(run_id, 2)])
    assert (item["state"], item["convergence"]["streak"]) == ("not_ready", 0)  # 2 of 3 at the most
    assert ready.said(item)["convergence"] == (
        "not_met",
        f"0 of the 3 evidence cycles in a row that 1.0.0 (spec {spec_id}) needs meet every per-cycle gate"
        f" ({at.format(1)} failed targets; the streak reaches {at.format(3)}, which could not be measured"
        f" (KeyError)); no live soak of 1.0.0 (spec {spec_id}) will add cycles",
    )
    await ended(harness, run_id, MATCH, at=T0 + timedelta(hours=3))
    await measure(harness)
    series, item = await read(4, """readiness || '{"format": 2}'""")  # evidence, as a later build wrote it
    assert series["breaks"] == [(run_id, 4), (run_id, 3), (run_id, 1)] and series["points"] == [(run_id, 2)]
    assert (series["versions"], series["trend"]) == ([(run_id, 2)], [(run_id, 2), (run_id, 1)])
    assert (item["state"], ready.said(item)["convergence"]) == (
        "unknown",
        (
            "unknown",
            f"the streak of 1.0.0 (spec {spec_id}) reaches {at.format(4)}, whose measures are of format 2,"
            " which this version does not read",
        ),
    )
    # A pair without evidence: its newest point, and its cycles counted up to the cap and one more.
    assert repo.COUNTED == readiness.COUNTED == 100
    monkeypatch.setattr(repo, "COUNTED", 2)
    async with harness.ctx.pool.connection() as conn:
        found = (await repo.readiness_series(conn, [asked | {"pair": "unit-other"}]))["unit-other"]
    assert (named(found)["newest"], found["cycles"], found["versions"]) == ((other, 4), 3, [])
    assert readiness.counted(found["cycles"], repo.COUNTED) == "over 2"


async def test_governance_reads_the_candidates_stored_facts(harness: Harness) -> None:
    """``repo.readiness_governance``: the sign-offs, the cycles the candidate is chosen from with their cited
    approval, adjudication, labelling and packs, and the live soaks, in one statement; and
    ``repo.last_readiness``, each pair's newest transition."""
    T0 = datetime.now(UTC) - timedelta(days=2)
    text = spec_text(execution="cycleInterval: 1h, stop: {maxCycles: 9}, concurrency: 2", extra=RULES)
    version = compile_spec(parse_yaml_text(text))
    async with harness.ctx.pool.connection() as conn:
        amy, bob = await person(conn, "amy", "spec_author"), await person(conn, "bob")
        olga, sue = await person(conn, "olga", "operator"), await person(conn, "sue", "operator")
        rita, ruth = await person(conn, "rita"), await person(conn, "ruth")
        spec_id = await upload(conn, version, amy)
        approval_id = await approved(conn, spec_id, amy, bob, scopes=approvals.needs(version))
    sso = {"needs": approvals.needs(version), "mode": "oidc"}
    run_id = await started(harness, spec_id, scan=SCANNED, **SCAN, approval=sso)
    async with harness.ctx.pool.connection() as conn:
        await repo.set_run_approval(conn, run_id, approval_id)
    for hour in range(5):
        await ended(harness, run_id, MATCH, at=T0 + timedelta(hours=hour))
    tokens = await started(harness, spec_id, scan=SCANNED, **SCAN, approval=sso | {"mode": "tokens"})
    await ended(harness, tokens, MATCH, at=T0 + timedelta(hours=5))
    await measure(harness)
    reasons = ["0 key(s) adjudicated; the exit needs at least 50"]
    async with harness.ctx.pool.connection() as conn:
        await scored(conn, run_id, 2, sue)
        failed = await scored(conn, run_id, 4, sue, reasons)
        labelling = await repo.insert_labelling(conn, drawn_labelling(run_id, sue, cycle=4))
        assert await label(conn, labelling["labelling_id"], sue) is not None
        signoffs = {1: await signed(conn, run_id, 1, version, olga, rita, ruth)}
        signoffs[3] = await signed(conn, run_id, 3, version, olga)
        pack_id = await packed(conn, run_id, 1, signoffs[1], olga, T0 + timedelta(days=1))
    asked = {"pair": PAIR, "spec_id": spec_id, "after": None, "until": None, "run_id": tokens, "cycle": 1}
    since = asked | {"after": T0 + timedelta(hours=2, minutes=30)}
    upto = asked | {"until": T0 + timedelta(minutes=30), "run_id": run_id}
    async with harness.ctx.pool.connection() as conn:
        whole = (await repo.readiness_governance(conn, [asked]))[PAIR]
        later = (await repo.readiness_governance(conn, [since]))[PAIR]
        early = (await repo.readiness_governance(conn, [upto]))[PAIR]
        baseline = await repo.last_readiness(conn, [PAIR, "unit-none"])  # the pass that measured wrote it
    assert list(baseline) == [PAIR] and baseline[PAIR]["detail"]["causes"] == ["baseline"]
    assert baseline[PAIR]["spec_id"] == spec_id  # its evidence cycle's version
    by = {(c["run_id"], c["cycle"]): c for c in whole["cycles"]}
    assert sorted(by) == sorted([(run_id, 1), (run_id, 3), (run_id, 4), (tokens, 1)])
    found = {
        s["approval_id"]: (s["status"], s["cycle"], s["version"], s["spec_id"]) for s in whole["signoffs"]
    }
    assert found == {
        signoffs[1]: ("approved", 1, "1.0.0", spec_id),
        signoffs[3]: ("requested", 3, "1.0.0", spec_id),
    }
    granted = next(s for s in whole["signoffs"] if s["status"] == "approved")
    assert granted["ended_at"] == T0 and granted["decided_at"] > T0 and granted["revoked_at"] is None
    first, worked, evidence = by[run_id, 1], by[run_id, 4], by[tokens, 1]
    assert (first["started"], first["worked"], first["spec_id"], first["ended_at"]) == (
        "oidc",
        False,
        spec_id,
        T0,
    )
    assert first["cited"] == {"approval_id": approval_id, "status": "approved"}
    assert first["readiness"]["evidence"] and first["adjudication"] is None and first["labelling"] is None
    assert first["packs"] == [
        {"pack_id": pack_id, "signoff_id": signoffs[1], "status": "issued",
         "issued_at": T0 + timedelta(days=1), "revoked_at": None}
    ]  # fmt: skip
    assert (worked["worked"], worked["packs"]) == (True, [])  # the newest cycle holding a person's work
    assert worked["adjudication"] == {"adjudication_id": failed, "passed": False, "reasons": reasons}
    labelled = worked["labelling"]
    names = ("labelling_id", "labelled_by", "role_a", "role_b", "cycle")
    assert tuple(labelled[name] for name in names) == (
        labelling["labelling_id"], sue.actor, "legacy", "new", 4,
    )  # fmt: skip
    assert not {"labelled_by_name", "drawn_by_name"} & set(labelled)  # readiness reads no person's name
    assert labelled["attested"] is None and isinstance(labelled["labelled_at"], datetime)
    assert labelled["accuracy"]["discrepant"]["uniform"] == 1
    assert accuracy.gate(labelled, at="cycle 4", min_decided=1, max_b_wrong=0)[0] == "met"
    assert (evidence["started"], evidence["cited"], evidence["worked"]) == ("tokens", None, False)
    live = [(run_id, 6, await harness.run(run_id)), (tokens, 2, await harness.run(tokens))]
    assert sorted(whole["soaks"], key=lambda soak: soak["run_id"]) == [
        {"run_id": name, "cycle": cycle, "status": "running", "stop_reason": None,
         "started_at": soak["started_at"], "execution": version.raw["execution"]}
        for name, cycle, soak in live
    ]  # fmt: skip
    assert version.raw["execution"]["stop"] == {"maxCycles": 9}
    # From a streak that began after cycle 3, only the worked cycle and the evidence cycle are candidates;
    # the older sign-offs are still given: they credit nothing, and the pair may have regressed.
    assert sorted((c["run_id"], c["cycle"]) for c in later["cycles"]) == sorted([(run_id, 4), (tokens, 1)])
    assert {s["approval_id"] for s in later["signoffs"]} == set(signoffs.values())
    assert [(c["run_id"], c["cycle"]) for c in early["cycles"]] == [(run_id, 1)]
    assert early["signoffs"] == [granted]
    # The evaluation of what the store gave: the streak holds the signed cycle, so the pair is evidenced.
    reading = {"pair": PAIR, "tier": "standard", "needed": 3, "window": 60, "until": None}
    async with harness.ctx.pool.connection() as conn:
        series = (await repo.readiness_series(conn, [reading]))[PAIR]
        now = (await repo.licence_facts(conn))["now"]
    streak = readiness.streak(series, ready.POLICY)
    assert (streak.k, streak.after, streak.evidence["run_id"]) == (6, None, tokens)  # type: ignore[index]
    seen = readiness.Installation("oidc", True, ready.ALLOWS, accuracy.gate, StopConditions.from_spec)
    item = readiness.evaluate(PAIR, ready.POLICY, series, whole, seen, now=now)
    assert (item["state"], item["candidate"], item["signoff_id"], item["pack_id"]) == (
        "evidenced", {"run_id": run_id, "cycle": 1}, signoffs[1], pack_id,
    )  # fmt: skip
    assert item["score"] == {"passed": 8, "required": 8} and item["blocking"] == []
    unsigned = readiness.evaluate(PAIR, ready.POLICY, series, later, seen, now=now)
    assert (unsigned["candidate"], unsigned["state"]) == ({"run_id": run_id, "cycle": 4}, "signoff_ready")
    assert ready.said(unsigned)["adjudication"] == (
        "not_applicable",
        f"cycle 4 of run {run_id} has 8 per-key results, under the 50 an adjudication needs",
    )
    # A revoked sign-off is still given, but its cycle is no candidate for it: the open request's is.
    async with repo.audited(harness.ctx.pool) as (conn, audit):
        await approvals.revoke(conn, audit, signoffs[1], "the cut-over was postponed", who=rita)
        for state in ("parity_ready", "signed_off"):
            await audit(WORKER, "readiness.changed", f"readiness:{PAIR}", {"pair": PAIR, "to": state})
    async with harness.ctx.pool.connection() as conn:
        revoked = (await repo.readiness_governance(conn, [asked]))[PAIR]
        last = await repo.last_readiness(conn, [PAIR, "unit-none"])
    statuses = {s["approval_id"]: (s["status"], s["revoked_at"] is not None) for s in revoked["signoffs"]}
    assert statuses == {signoffs[1]: ("revoked", True), signoffs[3]: ("requested", False)}
    assert sorted((c["run_id"], c["cycle"]) for c in revoked["cycles"]) == sorted(
        [(run_id, 3), (run_id, 4), (tokens, 1)]
    )
    assert list(last) == [PAIR] and last[PAIR]["detail"] == {"pair": PAIR, "to": "signed_off"}
    assert last[PAIR]["seq"] > 0 and last[PAIR]["at"] > T0
    undone = readiness.evaluate(PAIR, ready.POLICY, series, revoked, seen, now=now)
    assert (undone["candidate"]["cycle"], undone["state"]) == (3, "signoff_ready")
    assert ready.said(undone)["signoff"] == (
        "not_met",
        f"sign-off {signoffs[3]} requested for cycle 3 of run {run_id} awaits a rule_approver",
    )


async def test_brief_reads_are_bounded(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A brief reads the pair's newest cycles, one more than its window (which only tells what the oldest
    point is marked against), and the root-cause groups of the window's complete cycles, of which it lists
    so many. The evaluation and both reads share one snapshot: a purge that commits between the reads
    changes nothing of it."""
    monkeypatch.setattr(readiness, "BRIEF_CYCLES", 3)
    monkeypatch.setattr(judging, "BRIEF_CAUSES", 2)
    scan = await scanned(harness)
    for _ in range(2):
        await ended(harness, scan, MATCH)
    listed = await started(harness, keys_source="explicit")  # the window: its three cycles
    for cycle, groups in enumerate(("ab", "abc", "acde"), start=1):
        await ended(harness, listed, MATCH)
        causes = [
            RootCause(
                "RC-" + letter * 12, "signature", "$.status", ChangeType.VALUE_CHANGED, Direction.BOTH,
                affected_keys=1, finding_count=1, max_severity=Severity.HIGH,
            )
            for letter in groups
        ]  # fmt: skip
        async with harness.ctx.pool.connection() as conn:
            await repo.replace_root_causes(conn, listed, cycle, causes, {})
    await measure(harness)
    points, grouped, asked = repo.brief_points, repo.brief_causes, []

    async def read(conn: repo.Conn, pair: str, until: Any, limit: int) -> list[repo.Row]:
        rows = await points(conn, pair, until, limit)
        asked.append((limit, [(row["run_id"], row["cycle"]) for row in rows]))
        return rows

    async def purged(conn: repo.Conn, cycles: Any, limit: int) -> repo.Row:
        asked.append((limit, [(c["run_id"], c["cycle"]) for c in cycles]))
        async with harness.ctx.pool.connection() as other:  # the reported cycle's purge, whole
            assert await repo.start_purge(other, listed, 3, "retention standard") is not None
            await repo.finish_purge(other, listed, 3, WORKER.actor)
        return await grouped(conn, cycles, limit)

    monkeypatch.setattr(repo, "brief_points", read)
    before = await judging.brief(harness.ctx, PAIR)
    window = [(listed, 1), (listed, 2), (listed, 3)]
    assert asked == [(4, [*window[::-1], (scan, 2)])]  # of five measured cycles
    assert [(p["run_id"], p["cycle"], p["marks"]) for p in before["points"]] == [
        (listed, 1, ["keys"]), (listed, 2, []), (listed, 3, []),
    ]  # fmt: skip
    found = before["causes"]
    assert (found["opened"], found["still_open"], found["closed"], found["total"], found["cycles"]) == (
        2, 2, 1, 5, 3,
    )  # fmt: skip
    assert [item["root_cause_id"][3] for item in found["items"]] == ["a", "c"]
    assert found["text"].endswith(" 1 closed (not observed in it); the first 2 of 5 are listed")
    monkeypatch.setattr(repo, "brief_causes", purged)
    during = await judging.brief(harness.ctx, PAIR)
    assert asked[2] == (2, window)
    told = ("notes", "points", "cycle", "causes")
    assert {name: during[name] for name in told} == {name: before[name] for name in told}
    monkeypatch.setattr(repo, "brief_causes", grouped)
    after = await judging.brief(harness.ctx, PAIR)
    assert (after["causes"], after["cycle"]["root_causes"], after["cycle"]["cycle"]) == (None, None, 3)
    assert after["notes"] == [f"Cycle 3 of run {listed} is purged: its root-cause groups are not kept."]
