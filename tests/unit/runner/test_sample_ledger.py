"""The population's ledger and its closes (docs/mvp/SAMPLING.md §9, §13): a draw that did not pass, under
any spec name, blocks the sign-off of every later draw of its population until a person closes it with a
note, and only a draw made after the close is signed off; a later failure spends an earlier pass; chain
order, never the clock, decides what came first. Draws are written as the worker commits them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth.auth.principal import Principal
from deltasleuth.core.types import CompiledSpec
from deltasleuth.runner import sample
from deltasleuth.runner.runs import create_run
from deltasleuth.store import repo
from deltasleuth.store.db import migrate
from tests.conftest import TESTER
from tests.unit.store.four_eyes import drawn, person, sampled, upload

from .conftest import Harness, ScriptedService, doc
from .sampled import KEYS, listed, listed_source

pytestmark = pytest.mark.db
PROFILE, PROFILE_2 = sampled("customer-profile"), sampled("customer-profile-2")
NOTE = "fixed the segment mapping in the new API (PAY-7)"


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(pg_dsn, open=False)
    await pool.open(wait=True)
    await migrate(pool)
    yield pool
    await pool.close()


async def _people(pool: AsyncConnectionPool) -> tuple[Principal, Principal, dict[str, int]]:
    """Dave, who starts runs, Amy, who closes draws, and both names' versions, uploaded by Bob."""
    async with pool.connection() as conn:
        bob, dave, amy = [await person(conn, name, "operator") for name in ("bob", "dave", "amy")]
        ids = {v.name: await upload(conn, v, bob) for v in (PROFILE, PROFILE_2)}
    return dave, amy, ids


async def _draw(pool: AsyncConnectionPool, ids: dict[str, int], version: CompiledSpec, who: Principal,
                **options: Any) -> tuple[str, str]:  # fmt: skip
    async with pool.connection() as conn:
        return await drawn(conn, ids[version.name], version, who, **options)


async def _problems(
    pool: AsyncConnectionPool, run_id: str, version: CompiledSpec, **options: Any
) -> list[str]:
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        return await sample.problems(conn, run, 1, version, **options)


async def _close(pool: AsyncConnectionPool, name: str, who: Principal, note: str = NOTE) -> list[str]:
    async with repo.audited(pool) as (conn, audit):
        return await sample.close(conn, audit, name, note, who)


def _day() -> str:
    return f"{datetime.now(UTC):%Y-%m-%d}"


def _unclosed(
    draw_id: str, run_id: str, later: str, name: str = "customer-profile-2", how: str = "failed"
) -> str:
    return (
        f"draw {draw_id} (spec {name}, run {run_id} cycle 1) {how} before draw {later} was made and was not"
        " closed before it: close it with the fix, then sign off a draw made after the close"
    )


async def test_cherry_picking_a_redraw_is_refused_until_a_person_closes_the_failure(
    pool: AsyncConnectionPool,
) -> None:
    dave, amy, ids = await _people(pool)
    failed_run, failed = await _draw(pool, ids, PROFILE_2, dave, discrepant=["C001"])
    redrawn_run, redrawn = await _draw(pool, ids, PROFILE, dave)  # another name, the same population
    refused = await _problems(pool, redrawn_run, PROFILE)
    closed = await _close(pool, "customer-profile", amy)  # the name's ledger covers the other name's draw
    still = await _problems(pool, redrawn_run, PROFILE)  # the close came after the draw
    fixed_run, _ = await _draw(pool, ids, PROFILE, dave)
    allowed = await _problems(pool, fixed_run, PROFILE)
    async with pool.connection() as conn:
        closes = await (
            await conn.execute("SELECT actor, object, detail FROM ds.audit_log WHERE action = 'sample.close'")
        ).fetchall()
        stored = await repo.get_draw(conn, failed_run, 1)
    assert refused == still == [_unclosed(failed, failed_run, redrawn)]
    assert closed == [failed] and allowed == []
    assert closes == [(amy.actor, failed, {"spec_name": "customer-profile", "note": NOTE})]
    assert stored is not None and (stored["closed_by"], stored["close_note"]) == (amy.actor, NOTE)
    with pytest.raises(sample.SampleRefused) as nothing:  # C4: every ended bottom_k draw passed or is closed
        await _close(pool, "customer-profile-2", amy)
    assert (nothing.value.status, str(nothing.value)) == (
        409,
        "the population of customer-profile-2 has no draw to close (every ended bottom_k draw passed or is"
        " closed)",
    )


async def test_incomplete_and_unjudged_draws_need_a_close_too(pool: AsyncConnectionPool) -> None:
    dave, amy, ids = await _people(pool)
    unjudged_run, unjudged = await _draw(pool, ids, PROFILE, dave, judged=False)
    run_id, draw_id = await _draw(pool, ids, PROFILE, dave)
    async with pool.connection() as conn:  # an incomplete one: its cycle stopped with keys never read
        stopped, _ = await drawn(conn, ids["customer-profile"], PROFILE, dave, end=False)
        cur = await conn.execute(
            "UPDATE ds.run_keys SET state = 'cancelled' WHERE run_id = %s AND seq = 0 RETURNING key_masked",
            (stopped,),
        )
        never_read = (await cur.fetchone())[0]  # type: ignore[index]
        await conn.execute(
            "DELETE FROM ds.results WHERE run_id = %s AND key_masked = %s", (stopped, never_read)
        )
        found = await repo.get_draw(conn, stopped, 1)
        await repo.end_cycle(conn, stopped, sample=await sample.judge(conn, found, PROFILE))  # type: ignore[arg-type]
    last_run, last = await _draw(pool, ids, PROFILE, dave)
    async with pool.connection() as conn:
        ledger = {d["draw_id"]: d["status"] for d in await repo.ledger(conn, PROFILE.sample.population)}  # type: ignore[union-attr]
    assert list(ledger.values()) == ["unjudged", "passed", "incomplete", "passed"]
    assert await _problems(pool, run_id, PROFILE) == [
        _unclosed(unjudged, unjudged_run, draw_id, "customer-profile", "ended without a judgement")
    ]
    assert await _problems(pool, last_run, PROFILE, later=False) == [
        _unclosed(unjudged, unjudged_run, last, "customer-profile", "ended without a judgement"),
        _unclosed(found["draw_id"], stopped, last, "customer-profile", "ended incomplete"),  # type: ignore[index]
    ]
    assert sorted(await _close(pool, "customer-profile", amy)) == sorted([unjudged, found["draw_id"]])  # type: ignore[index]


async def test_a_later_failure_spends_an_earlier_pass_whatever_its_close(pool: AsyncConnectionPool) -> None:
    dave, amy, ids = await _people(pool)
    passed_run, passed = await _draw(pool, ids, PROFILE, dave)
    failed_run, failed = await _draw(pool, ids, PROFILE_2, dave, discrepant=["C002", "C005"])
    spent = await _problems(pool, passed_run, PROFILE)
    await _close(pool, "customer-profile-2", amy)
    after_close = await _problems(pool, passed_run, PROFILE)
    without_later = await _problems(pool, passed_run, PROFILE, later=False)  # a pack reads R-c from the grant
    async with pool.connection() as conn:
        later = await sample.later_draws(conn, passed_run, 1)
    refusal = (
        f"draw {failed} (spec customer-profile-2, run {failed_run} cycle 1), made after draw {passed}, failed"
        f" strata segment=retail, segment=other on {_day()}: fix it, close it and sign off a draw made after"
        " the close"
    )
    assert spent == after_close == [refusal]
    assert without_later == [] and later == [{"draw_id": failed, "status": "failed"}]


async def test_later_live_draws_block_only_once_they_fail_for_certain(pool: AsyncConnectionPool) -> None:
    dave, _, ids = await _people(pool)
    passed_run, passed = await _draw(pool, ids, PROFILE, dave)
    _, clean = await _draw(pool, ids, PROFILE, dave, end=False)
    shown = await _problems(pool, passed_run, PROFILE)
    async with pool.connection() as conn:
        clean_later = await sample.later_draws(conn, passed_run, 1)
    live_run, live = await _draw(pool, ids, PROFILE_2, dave, discrepant=["C006"], end=False)
    certain = await _problems(pool, passed_run, PROFILE)
    unjudged_run, unjudged = await _draw(pool, ids, PROFILE, dave, judged=False)
    both = await _problems(pool, passed_run, PROFILE)
    head = f"made after draw {passed},"
    tail = ": fix it, close it and sign off a draw made after the close"
    assert shown == [] and clean_later == [{"draw_id": clean, "status": "live"}]
    assert certain == [
        f"draw {live} (spec customer-profile-2, run {live_run} cycle 1), {head} has failed stratum"
        f" segment=other for certain{tail}"
    ]
    assert both == [
        *certain,
        f"draw {unjudged} (spec customer-profile, run {unjudged_run} cycle 1), {head} ended on {_day()}"
        f" without a judgement{tail}",
    ]


async def test_a_close_racing_a_draw_is_judged_by_chain_order(pool: AsyncConnectionPool) -> None:
    dave, amy, ids = await _people(pool)
    failed_run, failed = await _draw(pool, ids, PROFILE, dave, discrepant=["C001"])
    results: dict[str, Any] = {}
    async with pool.connection() as closing, pool.connection() as drawing:
        async with closing.transaction():  # the close takes the chain's lock first and holds it
            await sample.close(
                closing, lambda *a: repo.insert_audit(closing, *a), "customer-profile", NOTE, amy
            )

            async def draw() -> None:
                async with drawing.transaction():
                    results["later"] = await drawn(drawing, ids["customer-profile"], PROFILE, dave)

            racing = asyncio.create_task(draw())
            await _waiting(pool)  # the draw's sample.draw row waits for the close to commit
        await racing
    after_run, _ = results["later"]
    async with pool.connection() as conn, conn.transaction():  # now the draw commits first
        before_run, before = await drawn(conn, ids["customer-profile"], PROFILE, dave, discrepant=["C004"])
    async with pool.connection() as drawing:
        async with drawing.transaction():
            results["first"] = await drawn(drawing, ids["customer-profile"], PROFILE, dave)
            racing = asyncio.create_task(_close(pool, "customer-profile", amy))
            await _waiting(pool)  # the close's sample.close row waits for the draw to commit
        await racing
    first_run, first = results["first"]
    assert await _problems(pool, after_run, PROFILE, later=False) == []  # closed before it was drawn
    assert await _problems(pool, first_run, PROFILE, later=False) == [
        _unclosed(before, before_run, first, "customer-profile")  # drawn before the close committed
    ]
    assert failed_run and failed


async def _waiting(pool: AsyncConnectionPool) -> None:
    """Until another session waits on a lock (the chain's, DSAU)."""
    async with pool.connection() as conn:
        for _ in range(200):
            cur = await conn.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
            if (await cur.fetchone())[0]:  # type: ignore[index]
                return
            await asyncio.sleep(0.02)
    raise AssertionError("no session waited on a lock")


async def test_rate_draws_are_monitoring_never_closed_nor_blocking(pool: AsyncConnectionPool) -> None:
    dave, amy, ids = await _people(pool)
    rate = sampled("customer-profile-2", "1.1.0", method="rate")
    async with pool.connection() as conn:
        ids["customer-profile-2"] = await upload(conn, rate, dave)
    rate_run, rate_draw = await _draw(pool, ids, rate, dave, judged=False)  # unjudged, yet never blocking
    run_id, _ = await _draw(pool, ids, PROFILE, dave)
    rate_problems = await _problems(pool, rate_run, rate)
    with pytest.raises(sample.SampleRefused) as refused:
        await _close(pool, "customer-profile-2", amy)
    with pytest.raises(LookupError) as unknown:
        await _close(pool, "customer-profile-9", amy)
    assert await _problems(pool, run_id, PROFILE) == []
    assert (refused.value.status, str(refused.value)) == (
        409,
        "the population of customer-profile-2 has no draw to close (every ended bottom_k draw passed or is"
        " closed)",
    )
    assert str(unknown.value) == "spec customer-profile-9 not found"
    assert rate_problems == [
        f"the sample {rate_draw} of cycle 1 of run {rate_run} was drawn by rate: sign-off needs bottom_k"
        " sizes fixed in the spec",
        f"the sample {rate_draw} of cycle 1 of run {rate_run} ended without a judgement: draw again",
    ]


async def test_a_close_note_shaped_like_a_key_of_a_draw_s_spec_is_refused(pool: AsyncConnectionPool) -> None:
    identifier = sampled("customer-ids", key="identifier")
    async with pool.connection() as conn:
        dave, amy = await person(conn, "dave", "operator"), await person(conn, "amy", "operator")
        ids = {"customer-ids": await upload(conn, identifier, dave)}
    await _draw(pool, ids, identifier, dave, discrepant=["C003"])
    with pytest.raises(sample.SampleRefused) as refused:
        await _close(pool, "customer-ids", amy, "C003 was fixed")
    closed = await _close(pool, "customer-ids", amy, "fixed the mapping (PAY-7)")
    assert (refused.value.status, str(refused.value)) == (
        422,
        "the note holds a key-shaped value: write it without customer keys",
    )
    assert len(closed) == 1


async def test_a_close_note_is_checked_against_every_spec_that_could_attest_it(
    pool: AsyncConnectionPool,
) -> None:
    """A later sign-off of the population under any name attests the note: it is checked against the named
    spec's newest version, each ledger name's and each stored name sampling the population, drawn or not,
    not only against the spec of each draw it closes."""
    identifier = sampled("customer-ids", key="identifier")
    dave, amy, ids = await _people(pool)
    async with pool.connection() as conn:
        ids["customer-ids"] = await upload(conn, identifier, dave)
    shaped = "C004 had the wrong segment, fixed in 4.2"  # a key of customer-ids, not of customer-profile-2
    await _draw(pool, ids, PROFILE_2, dave, discrepant=["C004"])
    with pytest.raises(sample.SampleRefused) as named:
        await _close(pool, "customer-ids", amy, shaped)
    with pytest.raises(sample.SampleRefused) as stored:  # customer-ids, stored, has drawn nothing yet
        await _close(pool, "customer-profile-2", amy, shaped)
    await _draw(pool, ids, identifier, dave)  # a draw under customer-ids on the ledger
    with pytest.raises(sample.SampleRefused) as listed:
        await _close(pool, "customer-profile-2", amy, shaped)
    closed = await _close(pool, "customer-profile-2", amy)
    assert [(e.value.status, str(e.value)) for e in (named, stored, listed)] == [
        (422, sample.NOTE_KEY_SHAPED)
    ] * 3
    assert len(closed) == 1


async def test_a_ledger_draw_no_sample_draw_row_names_refuses_a_later_sign_off(
    pool: AsyncConnectionPool,
) -> None:
    """The earlier draw's row was altered to name another draw: nothing orders it, so the later draw's
    sign-off is refused, never judged by comparing a missing seq, and its attests leave it out."""
    dave, _, ids = await _people(pool)
    altered_run, altered = await _draw(
        pool, ids, PROFILE_2, dave, discrepant=["C001"], audited={"draw_id": repo.new_draw_id()}
    )
    run_id, _ = await _draw(pool, ids, PROFILE, dave)
    async with pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        facts = await sample.attested(conn, run, 1, PROFILE)  # type: ignore[arg-type]
        later = await sample.later_draws(conn, run_id, 1)
    assert await _problems(pool, run_id, PROFILE) == [
        f"the sample {altered} of cycle 1 of run {altered_run} does not match its sample.draw audit row: its"
        " order in the ledger is unknown"
    ]
    assert facts is not None and (facts["ledger"], later) == ([], [])


async def test_every_spelling_of_a_listing_s_path_shares_its_population(
    harness: Harness, service: ScriptedService
) -> None:
    """/list, /%6Cist and /%6cist name one endpoint (RFC 3986 §6.2.2), so one population: R1 refuses a run
    over another spelling while one is live, and G9 names the earlier draw that failed."""
    service.listings = [KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    service.script("b", "K001", (200, doc("K001", status="B")))  # a discrepancy every read finds
    escaped = [
        await harness.spec(listed(listed_source(request={"method": "GET", "path": path}), name=f"unit-{n}"))
        for n, path in enumerate(["/%6Cist", "/%6cist"])
    ]
    first = await create_run(harness.ctx, await harness.spec(listed()), "batch", who=TESTER)
    with pytest.raises(sample.SampleRefused) as live:
        await create_run(harness.ctx, escaped[0], "batch", who=TESTER)
    await harness.finish(first)
    later = await create_run(harness.ctx, escaped[1], "batch", who=TESTER)
    run = await harness.finish(later)
    async with harness.ctx.pool.connection() as conn:
        failed, redrawn = await repo.get_draw(conn, first, 1), await repo.get_draw(conn, later, 1)
        assert failed is not None and redrawn is not None
        found = await sample.problems(conn, run, 1, await harness.ctx.spec(conn, escaped[1]))
    assert (live.value.status, str(live.value)) == (
        409,
        f"spec unit-0 samples the population of live sampled run {first} (spec unit-runner, listing:svc-a:GET"
        " /list): stop it before drawing another sample (docs/mvp/SAMPLING.md §5)",
    )
    assert redrawn["population"] == failed["population"] == ["listing:svc-a:GET /list"]
    assert _unclosed(failed["draw_id"], first, redrawn["draw_id"], "unit-runner") in found


async def test_progress_is_the_stored_judgement_once_the_cycle_ends(pool: AsyncConnectionPool) -> None:
    dave, _, ids = await _people(pool)
    async with pool.connection() as conn:
        run_id, _ = await drawn(conn, ids["customer-profile"], PROFILE, dave, discrepant=["C002"], end=False)
        await conn.execute(  # a retry's fresh read replaced its first
            "UPDATE ds.run_keys SET retries = 1 WHERE run_id = %s AND key_masked = 'm-C005'", (run_id,)
        )
        found = await repo.get_draw(conn, run_id, 1)
        assert found is not None
        live = await sample.progress(conn, found, PROFILE)
        await repo.end_cycle(conn, run_id, sample=await sample.judge(conn, found, PROFILE))
        stored = (await repo.get_cycle(conn, run_id, 1))["sample"]  # type: ignore[index]
        ended = await sample.progress(conn, found, PROFILE)
    extras = ("done", "retrying")
    judged = {
        name: v for name, v in ended.items() if name not in ("done", "finished_recently", "eta_seconds")
    }
    judged["strata"] = [{n: v for n, v in s.items() if n not in extras} for s in judged["strata"]]
    assert judged == stored
    assert [(s["label"], s["done"], s["retrying"], s["retried"], s["verdict"]) for s in live["strata"]] == [
        ("segment=retail", 3, 0, 0, "failed"),  # a discrepancy in a census stratum: failed for certain
        ("segment=other", 3, 0, 1, "passed"),
    ]
    assert (live["done"], live["finished_recently"], live["eta_seconds"]) == (6, 6, None)


def test_ds_run_prints_each_stratum_of_the_judgement() -> None:
    judgement = {
        "strata": [
            {"label": "degree=d0", "tier": "standard", "target": "0.001", "drawn": 3011,
             "frame_keys": 1204331, "n": 3009, "x": 0, "unresolved": 2, "ub95_one_sided": "0.0009951",
             "verdict": "passed"},
            {"label": "flagged=Y", "tier": "high", "target": "0.0001", "drawn": 4210, "frame_keys": 4210,
             "n": 4210, "x": 1, "unresolved": 0, "ub95_one_sided": None, "verdict": "failed"},
        ]
    }  # fmt: skip
    assert sample.lines(judgement) == [
        "stratum degree=d0 (standard, ≤ 0.1%): 3,011 drawn of 1,204,331, 3,009 compared, 0 discrepant, 2"
        " unresolved, bound 0.09951%: passed",
        "stratum flagged=Y (high, ≤ 0.01%): 4,210 drawn of 4,210, 4,210 compared, 1 discrepant, 0 unresolved,"
        " compared whole: failed",
    ]
