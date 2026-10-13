"""A sampled run's draws (docs/mvp/SAMPLING.md §5): created at once with its draw pending, drawn by the worker
from a table of the local postgres against the scripted service, committed before any key with its audit row
last, deterministic under its nonce, refused by the trigger out of turn, discarded when the run stops
meanwhile, paused on its frame's refusal until a resume draws again with a new nonce, and rolling in a soak
that pauses after a draw that did not pass until it is closed; the creation refusals R1-R4 and R5."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth.connectors.postgres import engine as postgresql
from deltasleuth.core import sampling
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.runner import drift, sample
from deltasleuth.runner.cycle import CANCELLED, finish_cycle
from deltasleuth.runner.runs import cancel_run, create_run, pause_run, resume_run, stop_run
from deltasleuth.store import repo
from tests.conftest import TESTER, MySQLServer, OracleServer

from .conftest import TARGETS, Harness, OpenHarness, ScriptedService, doc, until
from .sampled import (
    CUSTOMER_KEYS,
    KEYS,
    Tables,
    audited,
    close,
    customer,
    cycle_row,
    drawn,
    keys,
    listed,
    segments,
    served_source,
    table_source,
    tabled,
    tables_fixture,  # noqa: F401 (the on_tables fixture)
)
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
TRIGGER = "draw D-{} is made as of now(), for a running run's current cycle, before its keys"


async def _selected(h: Harness, run_id: str, cycle: int = 1) -> str:
    async with h.ctx.pool.connection() as conn:
        return sampling.selection_sha256([line async for line in repo.selection_lines(conn, run_id, cycle)])


async def test_a_table_frame_is_drawn_then_compared_end_to_end(
    on_tables: Tables, service: ScriptedService
) -> None:
    for key in CUSTOMER_KEYS:
        service.script("b", key, (200, customer(key)))
    _, harness = on_tables()
    async with harness as h:
        run_id = await create_run(h.ctx, await h.spec(tabled()), "batch", who=TESTER)
        created = await h.run(run_id)
        async with h.ctx.pool.connection() as conn:
            drawing = await repo.cycle_done(conn, run_id, 1)
        assert await keys(h, run_id) == [] and sum(service.calls.values()) == 0  # created at once
        run = await h.finish(run_id)
        stored = await drawn(h, run_id)
        taken = await keys(h, run_id)
        rows = await audited(h, run_id, "create_run", "sample.draw")
        selected = await _selected(h, run_id)
        ended = await cycle_row(h, run_id)
    assert (created["status"], created["draw"].keys(), drawing) == ("running", {"due"}, False)
    params = created["params"]
    assert (params["keys_source"], params["method"], params["population"]) == (
        "hash_sample",
        "bottom_k",
        ["table:db-a/customers"],
    )
    assert (run["status"], run["draw"]) == ("completed", None)
    assert stored is not None and (stored["keys"], stored["method"], stored["salt_label"]) == (
        20,
        "bottom_k",
        "unit-q4",
    )
    assert re.fullmatch("[0-9a-f]{32}", stored["nonce"]) and re.search("[a-f]", stored["nonce"])
    assert [s["label"] for s in stored["strata"]] == ["segment=retail", "segment=other", "flagged=Y"]
    assert [(s["frame_keys"], s["drawn"], s["basis"]) for s in stored["strata"]] == [
        (9, 9, "census"),
        (9, 9, "census"),
        (2, 2, "census"),
    ]
    assert sorted(k["key"] for k in taken) == CUSTOMER_KEYS and [k["seq"] for k in taken] == list(range(20))
    assert selected == stored["selection_sha256"]
    [(_, created_row), (_, detail)] = rows
    assert "nonce" not in detail and stored["nonce"] not in str(detail)
    assert detail["salt_sha256"] == sha256_hex(f"unit-q4:{stored['nonce']}")
    assert (detail["draw_id"], detail["selection_sha256"], detail["keys"]) == (
        stored["draw_id"],
        selected,
        20,
    )
    assert detail["strata"] == [{"label": s["label"], "frame_keys": s["frame_keys"], "drawn": s["drawn"]}
                                for s in stored["strata"]]  # fmt: skip
    assert created_row["keys"] == 0
    assert ended is not None and ended["census"]["a"]["basis"] == "frame"
    assert ended["sample"]["status"] == "passed" and ended["sample"]["draw_id"] == stored["draw_id"]


async def test_the_same_nonce_selects_the_same_sample_whatever_the_batches(
    on_tables: Tables, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.listings = [["C001", "C021", {"id": "C022", "segment": "B", "flagged": "Y"}]] * 2
    frame = {"kind": "union", "sources": [table_source(), served_source()]}
    _, harness = on_tables()
    async with harness as h:
        spec_id = await h.spec(tabled(frame, strata=segments(4, 4, 2)))  # sampled strata
        run_id = await create_run(h.ctx, spec_id, "batch", who=TESTER)
        await sample.draw(h.ctx, run_id)
        stored = await drawn(h, run_id)
        assert stored is not None
        opened = await keys(h, run_id)
        taken = [k["key_masked"] for k in opened]
        # One transaction: the draw, then its keys as of the same now(), the census, the audit row last.
        assert {k["available_at"] for k in opened} == {stored["drawn_at"]}
        # The frame read again with the draw's nonce, in batches of 3 rows and keys: the same selection.
        monkeypatch.setattr(postgresql, "BATCH", 3)
        monkeypatch.setattr(sample, "DEDUPE", 3)
        run = await h.run(run_id)
        async with h.ctx.pool.connection() as conn:
            spec = await h.ctx.spec(conn, spec_id)
            sealer = await h.ctx.sealer(conn, run_id)
        frame_again = sample._Frame(h.ctx, spec, run, sampling.salt("unit-q4", stored["nonce"]))
        await frame_again.read()
        selection = frame_again.selection().result()
        sealed, _, selected = sample._sealed(h.ctx, spec, sealer, selection)
    assert (list(sealed), selected, selection.strata) == (taken, stored["selection_sha256"], stored["strata"])
    assert [s["basis"] for s in stored["strata"]] == ["sample", "sample", "sample"]


async def test_the_trigger_refuses_a_draw_out_of_turn(harness: Harness, service: ScriptedService) -> None:
    service.listings = [KEYS]
    run_id = await create_run(harness.ctx, await harness.spec(listed()), "batch", who=TESTER)
    row = {
        "run_id": run_id, "spec_id": (await harness.run(run_id))["spec_id"], "method": "bottom_k",
        "population": ["listing:svc-a:GET /list"], "salt_label": "unit-q4", "keys": 1, "frame": {},
        "strata": [], "selection_sha256": "0" * 64,
    }  # fmt: skip

    async def refused(cycle: int = 1) -> str:
        draw_id = repo.new_draw_id()
        with pytest.raises(psycopg.errors.CheckViolation) as info:
            async with harness.ctx.pool.connection() as conn:
                made = row | {"draw_id": draw_id, "cycle": cycle, "nonce": sampling.nonce()}
                await repo.insert_draw(conn, made, {}, [])
        assert TRIGGER.format(draw_id.removeprefix("D-")) in str(info.value)
        return draw_id

    await refused(cycle=2)  # not the run's current cycle
    await pause_run(harness.ctx, run_id, who=TESTER)
    await refused()  # not running
    await resume_run(harness.ctx, run_id, who=TESTER)
    await sample.draw(harness.ctx, run_id)
    async with harness.ctx.pool.connection() as conn:
        await refused()  # its draw is done: none pending
        await repo.set_draw(conn, run_id, {"due": "2026-01-01T00:00:00+00:00"})
    await refused()  # after its keys


async def test_a_run_stopped_while_drawing_discards_the_draw(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.listings = [KEYS]
    run_id = await create_run(harness.ctx, await harness.spec(listed()), "batch", who=TESTER)
    read = sample._Frame.read

    async def stopped_meanwhile(self: Any) -> None:
        await read(self)
        await stop_run(harness.ctx, run_id, CANCELLED, who=TESTER)

    monkeypatch.setattr(sample._Frame, "read", stopped_meanwhile)
    await sample.draw(harness.ctx, run_id)
    async with harness.ctx.pool.connection() as conn:
        assert await repo.cycle_done(conn, run_id, 1)  # a run stopped while drawing ends its cycle
    await finish_cycle(harness.ctx, run_id, who=TESTER)
    run = await harness.run(run_id)
    assert (run["status"], await drawn(harness, run_id), await keys(harness, run_id)) == (
        "cancelled",
        None,
        [],
    )
    assert await audited(harness, run_id, "sample.draw") == []
    ended = await cycle_row(harness, run_id)
    assert ended is not None and ended["sample"] is None


async def test_a_frame_read_stops_once_its_run_is_cancelled(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow listing of 100 pages, cancelled at its tenth: the read checks its run as it goes and stops at
    the next page, nothing drawn, refused or disclosed."""
    listed_, pages = sample._listed, []

    def slow(page: list[Any], member: Any) -> Any:
        time.sleep(0.02)
        pages.append(page)
        return listed_(page, member)

    monkeypatch.setattr(sample, "_listed", slow)
    monkeypatch.setattr(sample, "WATCH_S", 0.0)  # each page checks
    service.listings = [[f"K{n:03d}" for n in range(1, 201)]]
    run_id = await create_run(harness.ctx, await harness.spec(listed()), "batch", who=TESTER)
    drawing = asyncio.create_task(sample.draw(harness.ctx, run_id))
    await until(lambda: len(pages) >= 10)
    assert await cancel_run(harness.ctx, run_id, who=TESTER) == "cancelled"
    cancelled_at = len(pages)
    await drawing
    assert len(pages) <= cancelled_at + 1 < 100
    assert (await drawn(harness, run_id), await keys(harness, run_id)) == (None, [])
    assert await audited(harness, run_id, "paused", "sample.draw", "sample.draw_failed") == []


async def test_a_refused_draw_pauses_the_run_until_a_resume_draws_again_with_a_new_nonce(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.listings = [["K001", "K002", None], KEYS]  # the first walk's second page answers 503
    nonces: list[str] = []
    drawn_nonce = sampling.nonce
    monkeypatch.setattr(sampling, "nonce", lambda: nonces.append(drawn_nonce()) or nonces[-1])
    run_id = await create_run(harness.ctx, await harness.spec(listed()), "batch", who=TESTER)
    await sample.draw(harness.ctx, run_id)
    paused = await harness.run(run_id)
    error = "the key listing of target svc-a was not read completely (HTTP 503: page 2: HTTP 503)"
    assert (paused["status"], paused["stop_reason"], paused["draw"]["error"]) == (
        "paused",
        sample.DRAW_PAUSE,
        error,
    )
    assert set(paused["draw"]) == {"due", "error", "at"} and await drawn(harness, run_id) is None
    assert await audited(harness, run_id, "paused", "sample.draw_failed") == [
        ("paused", {"reason": sample.DRAW_PAUSE}),
        ("sample.draw_failed", {"cycle": 1, "error": error}),
    ]
    assert await resume_run(harness.ctx, run_id, who=TESTER) == "running"
    resumed = await harness.run(run_id)
    assert (resumed["stop_reason"], set(resumed["draw"])) == (None, {"due"})
    await sample.draw(harness.ctx, run_id)
    stored = await drawn(harness, run_id)
    assert stored is not None and stored["nonce"] == nonces[1] != nonces[0]


# Customers as MySQL and Oracle hold them (CUSTOMERS's first two), and how each puts back their segment.
FRAMED = {
    "mysql": (
        "CREATE TABLE customers (id VARCHAR(10) PRIMARY KEY, name VARCHAR(20), status VARCHAR(2),"
        " segment VARCHAR(2), flagged VARCHAR(2));"
        " INSERT INTO customers VALUES ('C001', 'n1', 'A', 'R', 'N'), ('C002', 'n2', 'A', 'B', 'Y')",
        "ALTER TABLE customers ADD COLUMN segment VARCHAR(2)",
    ),
    "oracle": (
        "CREATE TABLE customers (id VARCHAR2(10) PRIMARY KEY, name VARCHAR2(20), status VARCHAR2(2),"
        " segment VARCHAR2(2), flagged VARCHAR2(2));"
        " INSERT INTO customers VALUES ('C001', 'n1', 'A', 'R', 'N');"
        " INSERT INTO customers VALUES ('C002', 'n2', 'A', 'B', 'Y')",
        "ALTER TABLE customers ADD (segment VARCHAR2(2))",
    ),
}


@pytest.mark.parametrize(
    "engine",
    [
        "postgresql",
        pytest.param("mysql", marks=pytest.mark.mysql),
        pytest.param("oracle", marks=pytest.mark.oracle),
    ],
)
async def test_a_frame_that_meets_a_dropped_column_has_the_schema_guard_look_first(
    engine: str,
    on_tables: Tables,
    service: ScriptedService,
    open_harness: OpenHarness,
    request: pytest.FixtureRequest,
) -> None:
    """A draw whose frame meets a strata column dropped since the run was created (UndefinedColumn, MySQL
    1054, ORA-00904: docs/mvp/ENGINES.md §11's denied class) has the schema guard look before the run is
    failed, as a scan's or a key's read does (docs/mvp/PRECHECKS.md §7, V5): the column is gone, so the run
    is paused, not failed, its draw still due, and it resumes once the column is back."""
    if engine == "postgresql":
        dsn, harness = on_tables()
        relation, column, restored = (
            "public.customers",
            "segment",
            "ALTER TABLE customers ADD COLUMN segment text",
        )

        def alter(statement: str) -> None:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(statement)  # type: ignore[arg-type]

    else:
        seed, restored = FRAMED[engine]
        if engine == "mysql":
            server: MySQLServer = request.getfixturevalue("mysql_server")
            schema = server.database(seed)
            entry, alter = server.target(schema), lambda statement: server.execute(statement, schema)
            relation, column = f"{schema}.customers", "segment"
        else:
            oracle: OracleServer = request.getfixturevalue("oracle_server")
            schema = oracle.schema(seed)
            entry, alter = oracle.target(schema), lambda statement: oracle.execute(statement, schema)
            relation, column = f"{schema}.CUSTOMERS", "SEGMENT"
        assert service.server is not None
        served = {
            "base_url": f"http://127.0.0.1:{service.server.port}", "allow_private_network": True,
            "allow_insecure_http": True, "rate_per_s": 500,
            "capabilities": {"authoritative_absence": {"status": [404]}},
        }  # fmt: skip
        harness = open_harness(json.dumps({"targets": {"db-a": entry, "svc-a": served, "svc-b": served}}))
    async with harness as h:
        run_id = await create_run(h.ctx, await h.spec(tabled()), "batch", who=TESTER)
        alter("ALTER TABLE customers DROP COLUMN segment")
        run = await h.finish(run_id, (*repo.TERMINAL, "paused"))
        alter(restored)
        resumed = await resume_run(h.ctx, run_id, who=TESTER)
    assert (run["status"], run["stop_reason"], run["error"]) == ("paused", drift.SCHEMA_PAUSE, None)
    assert set(run["draw"]) == {"due"} and run["guard"]["checked"]["where"] == "read"
    assert drift.lines(run)[0].startswith(
        f"Paused by the schema guard in cycle 1: side a: column '{column}' of {relation} is gone"
    )
    assert resumed == "running"


async def test_an_unreachable_database_postpones_the_draw_a_lease_period(
    open_harness: OpenHarness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    service.listings = [KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    session, failures = sample._session, [psycopg.OperationalError("the server closed the connection")]

    async def once_unreachable(ctx: Any) -> Any:
        if failures:
            raise failures.pop()
        return await session(ctx)

    monkeypatch.setattr(sample, "_session", once_unreachable)
    assert service.server is not None
    async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{service.server.port}", rate=500),
                            lease_seconds=1) as h:  # fmt: skip
        run_id = await create_run(h.ctx, await h.spec(listed()), "batch", who=TESTER)
        with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.worker"):
            run = await h.finish(run_id)
        assert (run["status"], failures) == ("completed", []) and await drawn(h, run_id) is not None
    assert f"draw of run {run_id} postponed: OperationalError" in caplog.messages


async def test_a_sign_off_bucket_no_frame_key_falls_into_fails_the_draw(on_tables: Tables) -> None:
    strata = segments()
    strata[1]["buckets"][0]["values"] = ["Z"]
    _, harness = on_tables()
    async with harness as h:
        run_id = await create_run(h.ctx, await h.spec(tabled(strata=strata)), "batch", who=TESTER)
        await sample.draw(h.ctx, run_id)
        run = await h.run(run_id)
    assert (run["status"], run["draw"]["error"]) == (
        "paused",
        "no frame key falls into sign-off bucket flagged=Y: a stratum without keys certifies nothing (correct"
        " its values, or remove it in a new version)",
    )


async def test_a_rolling_soak_draws_each_cycle_and_pauses_after_one_that_did_not_pass(
    harness: Harness, service: ScriptedService
) -> None:
    service.listings = [KEYS, KEYS]
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    old = doc("K001", status="OLD")
    service.script("b", "K001", (200, old), (200, old), (200, doc("K001")))  # its first cycle's two reads
    text = listed(execution="concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 2}")
    run_id = await create_run(
        harness.ctx, await harness.spec(text), "soak", soak={"rolling": True}, who=TESTER
    )
    paused = await harness.finish(run_id, ("paused",))
    first = await drawn(harness, run_id)
    ended = await cycle_row(harness, run_id)
    assert first is not None and ended is not None and ended["sample"]["status"] == "failed"
    assert (paused["cycle"], paused["stop_reason"], await keys(harness, run_id, 2)) == (
        2,
        sample.SAMPLE_PAUSE,
        [],
    )
    assert set(paused["draw"]) == {"due"}  # its own draw, after the close
    [*_, (_, detail)] = await audited(harness, run_id, "paused")
    assert detail == {"reason": sample.SAMPLE_PAUSE, "approval_id": None, "draw_id": first["draw_id"]}
    with pytest.raises(sample.SampleRefused) as info:
        await resume_run(harness.ctx, run_id, who=TESTER)
    assert (info.value.status, str(info.value)) == (
        409,
        f"run {run_id} paused after draw {first['draw_id']} did not pass: close it with the fix"
        " (POST /v1/samples/close), then resume",
    )
    await close(harness, first["draw_id"])
    assert await resume_run(harness.ctx, run_id, who=TESTER) == "running"
    run = await harness.finish(run_id)
    second = await drawn(harness, run_id, 2)
    assert (run["status"], run["stop_reason"]) == ("completed", "stop_condition:maxCycles")
    assert second is not None and second["draw_id"] != first["draw_id"] and second["nonce"] != first["nonce"]
    assert await _selected(harness, run_id, 2) == second["selection_sha256"]
    assert ((await cycle_row(harness, run_id, 2)) or {})["sample"]["status"] == "passed"
    assert service.walks == 2  # each cycle read its own frame


async def test_a_hash_sample_refuses_keys_a_fixed_soak_captured_samples_and_a_live_population(
    harness: Harness, service: ScriptedService, tmp_path: Path
) -> None:
    spec_id = await harness.spec(listed(execution="cycleInterval: 1h"))

    async def refusal(**kwargs: Any) -> tuple[int, str]:
        with pytest.raises(sample.SampleRefused) as info:
            chosen, mode = kwargs.pop("spec_id", spec_id), kwargs.pop("mode", "batch")
            await create_run(harness.ctx, chosen, mode, **kwargs, who=TESTER)
        return info.value.status, str(info.value)

    assert await refusal(keys=["K001"]) == (
        422,
        "a hash sample draws its keys from the frame: run it without --keys",
    )
    assert await refusal(mode="soak") == (
        422,
        "a soak over a hash sample draws a new sample each cycle: start it rolling (ds run --soak --rolling)",
    )
    assert await refusal(samples_dir=tmp_path) == (
        422,
        "captured samples cannot be sampled: a hash sample reads its frame from the targets",
    )
    live = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
    other = await harness.spec(listed(name="unit-runner-2"))
    assert await refusal(spec_id=other) == (
        409,
        f"spec unit-runner-2 samples the population of live sampled run {live} (spec unit-runner,"
        " listing:svc-a:GET /list): stop it before drawing another sample (docs/mvp/SAMPLING.md §5)",
    )
    await stop_run(harness.ctx, live, CANCELLED, who=TESTER)
    assert await refusal(spec_id=other)  # cancelling: still live
    await finish_cycle(harness.ctx, live, who=TESTER)
    assert (await harness.run(await create_run(harness.ctx, other, "batch", who=TESTER)))[
        "status"
    ] == "running"
