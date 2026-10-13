"""Readiness's transitions: a pair's baseline, then a ``readiness.changed`` row
per change only, by ``system:worker`` outside every pack segment; nothing during an upgrade's backfill but
one baseline a pair after it, and nothing of a pair whose cycle is still to measure; each trigger, and the
causes a row names from what differs for its pair, whatever entered the chain, after a restart and beside a
late commit too; a chain row committed after a higher id; a cycle no longer fresh; a closed pair's silence;
a restart; the settings recorded, named by the rows of their first evaluation only; a failed evaluation
tried again later, not every pass; the notifier routing a row; the key census readiness turns on in tokens
mode; and what an evaluation never does: take a cycle that is no evidence as the candidate, keep a revoked
sign-off met, or stop at measures it cannot read."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from deltasleuth import approvals, licence
from deltasleuth import readiness as judging
from deltasleuth.auth.principal import WORKER
from deltasleuth.config import ReadinessSettings
from deltasleuth.core import readiness
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.reports import pack, pack_verify
from deltasleuth.runner import create_run, run_worker
from deltasleuth.runner import readiness as measuring
from deltasleuth.runner.notify import Notifier
from deltasleuth.store import repo
from tests.conftest import TESTER, licensed, licensed_once
from tests.unit.reports.conftest import with_packs
from tests.unit.reports.packs import sampled_cycle, signed_cycle
from tests.unit.store.four_eyes import approved, person, upload

from .conftest import TARGETS, Harness, OpenHarness
from .measuring import (
    KEYS,
    MATCH,
    MISMATCH,
    PAIR,
    SCAN,
    SCANNED,
    SOAK,
    audited,
    ended,
    measures,
    scanned,
    scored,
    signed,
    started,
    stored,
)
from .notifying import deliveries, hook, receiving, rule
from .test_census import Tables, accounts, on_tables  # noqa: F401 (a fixture)
from .test_census import spec as tables_spec
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
NOWHERE = TARGETS.format(base="http://127.0.0.1:9", rate=500)  # the targets: no run here reads them
FAILING = MATCH | {"K001": MISMATCH, "K002": MISMATCH}  # 2 of 1,000 keys: over the 1 that 0.1% allows
PARITY = {"population": "met", "critical": "met", "targets": "met", "convergence": "met", "fresh": "met"}
# Developer mode, eight results a cycle: no sign-off can be made, and no adjudication is needed.
REQUIRED = PARITY | {
    "spec_approval": "unavailable", "adjudication": "not_applicable", "sample_ledger": "not_applicable",
    "signoff": "unavailable", "pack": "unavailable",
}  # fmt: skip


async def changes(h: Harness, pair: str | None = None) -> list[dict[str, Any]]:
    """The details of the ``readiness.changed`` rows (of ``pair``), oldest first."""
    rows = await audited(h, "readiness.changed")
    return [row["detail"] for row in rows if pair is None or row["object"] == f"readiness:{pair}"]


async def watch(h: Harness, state: measuring.State, *, after: float = 0) -> None:
    """A worker's readiness pass, ``after`` seconds later than its last evaluation was (or failed)."""
    state.evaluated_at -= after
    state.failed_at -= after
    state.backfilled_at = -float("inf")
    await measuring.watch(h.ctx, state)


async def record(h: Harness, action: str, obj: str = "x", detail: dict[str, Any] | None = None) -> int:
    """An audit row as its writer writes it; its chain seq."""
    async with repo.audited(h.ctx.pool) as (_, audit):
        return (await audit(TESTER, action, obj, detail or {}))["chain"]["seq"]


async def unsettle(h: Harness, pair: str = PAIR, *, days_ago: int = 0, **detail: Any) -> None:
    """Chain a row saying the pair was last recorded otherwise (in another state, and as ``detail`` says;
    ``days_ago``: then), so that the next evaluation records it again: what a change of stored facts does,
    without one."""
    async with h.ctx.pool.connection() as conn:
        last = (await repo.last_readiness(conn, [pair]))[pair]["detail"]
        await conn.execute(
            """INSERT INTO ds.audit_log (at, actor, action, object, detail)
               VALUES (now() - make_interval(days => %s), %s, 'readiness.changed', %s, %s)""",
            (days_ago, WORKER.actor, f"readiness:{pair}", Jsonb(last | {"to": "unknown"} | detail)),
        )


async def converged(h: Harness, spec_id: int | None = None, cycles: int = 3) -> str:
    """A soak over two tables read whole whose first ``cycles`` cycles ended clean, now."""
    run_id = await scanned(h, spec_id)
    for _ in range(cycles):
        await ended(h, run_id, MATCH)
    return run_id


async def test_a_pair_gets_its_baseline_then_a_row_per_change_only(harness: Harness) -> None:
    run_id = await converged(harness)
    state = measuring.State()
    await watch(harness, state)
    async with harness.ctx.pool.connection() as conn:
        installation = await repo.installation_id(conn)
    sha256 = readiness.config_sha256(None)
    [config] = await audited(harness, "readiness.config")
    assert (config["actor"], config["object"]) == ("system:worker", installation)
    assert config["detail"] == {
        "from_sha256": None,
        "sha256": sha256,
        "deviations": [],
    } | readiness.effective(None)
    [row] = await audited(harness, "readiness.changed")
    assert (row["actor"], row["object"], row["chain"]["seq"] > 0) == (
        "system:worker",
        f"readiness:{PAIR}",
        True,
    )
    at = {"run_id": run_id, "cycle": 3}
    baseline = {
        "pair": PAIR, "programme": None, "entity": None, "from": None, "to": "parity_ready",
        "was_regressed": False, "regressed": False, "score": {"passed": 5, "required": 5},
        "causes": ["baseline"], "gates": REQUIRED, "changed": [], "evidence": at, "candidate": at,
        "signoff_id": None, "pack_id": None, "config_sha256": sha256,
    }  # fmt: skip
    assert row["detail"] == baseline
    await watch(harness, state, after=measuring.READINESS_EVERY_S)  # judged again: nothing changed
    assert len(await changes(harness)) == 1
    await ended(harness, run_id, FAILING)
    await watch(harness, state)  # a measured cycle is judged at once
    [_, fell] = await changes(harness)
    assert fell == baseline | {
        "from": "parity_ready", "to": "not_ready", "score": {"passed": 3, "required": 5}, "causes": ["cycle"],
        "gates": REQUIRED | {"targets": "not_met", "convergence": "not_met"},
        "changed": [
            {"gate": "targets", "from": "met", "to": "not_met", "value": 2, "allowed": 1, "over": 1},
            {"gate": "convergence", "from": "met", "to": "not_met", "value": 0, "allowed": 3, "over": None},
        ],
        "evidence": at | {"cycle": 4}, "candidate": at | {"cycle": 4},
    }  # fmt: skip
    for cycle, expected in ((5, 3), (6, 3), (7, 4)):  # targets met again; 2 of 3; then converged
        await ended(harness, run_id, MATCH)
        await watch(harness, state)
        assert len(await changes(harness)) == expected, cycle
        if cycle < 7:  # a gate changed and the state stayed: the pair is not ready since it fell
            async with harness.ctx.pool.connection() as conn:
                since = (await judging.judged(harness.ctx, conn)).items[PAIR]["since"]
            rows = await audited(harness, "readiness.changed")
            assert since == rows[1]["at"] < rows[-1]["at"] and rows[-1]["detail"]["to"] == "not_ready"
    *_, still, rose = await changes(harness)
    assert (still["from"], still["to"], [c["gate"] for c in still["changed"]]) == (
        "not_ready", "not_ready", ["targets"],
    )  # fmt: skip
    assert (rose["from"], rose["to"], rose["causes"]) == ("not_ready", "parity_ready", ["cycle"])
    assert rose["changed"] == [
        {"gate": "convergence", "from": "not_met", "to": "met", "value": 3, "allowed": 3, "over": None}
    ]
    assert len(await audited(harness, "readiness.config")) == 1
    # No row holds a key, and what the dashboard shows is what the newest row recorded.
    written = json.dumps(await changes(harness))
    assert not any(key in written for key in KEYS)
    async with harness.ctx.pool.connection() as conn:
        found = await judging.judged(harness.ctx, conn)
    item = found.items[PAIR]
    assert (item["state"], item["score"], item["since"]) == (
        "parity_ready", {"passed": 5, "required": 5}, found.last[PAIR]["at"],
    )  # fmt: skip


async def test_upgrade_emits_only_baseline(harness: Harness) -> None:
    """Cycles that ended before 014 are measured in batches, and no transition is written until the last is:
    then one baseline a pair, and nothing else."""
    T0 = datetime.now(UTC) - timedelta(days=2)
    run_id = await scanned(harness)
    for hour, results in enumerate([MATCH, FAILING, *[MATCH] * 10]):
        await ended(harness, run_id, results, at=T0 + timedelta(hours=hour))
    other_id = await stored(harness, SOAK.replace("unit-runner", "unit-other"))
    other = await scanned(harness, other_id)
    await ended(harness, other, FAILING, at=T0)
    state = measuring.State()
    await measuring.watch(harness.ctx, state)  # the first batch of ten
    async with harness.ctx.pool.connection() as conn:
        owed, waiting = await repo.unmeasured_pairs(conn, [PAIR, "unit-other"])
        found = await judging.judged(harness.ctx, conn)
    assert (owed, {name: left["cycles"] for name, left in waiting.items()}) == (
        {"cycles": 3, "backfill": 3},
        {PAIR: 2, "unit-other": 1},
    )
    assert (found.unmeasured, found.backfill) == (3, 3) and await changes(harness) == []
    assert judging.notes(found, "", None) == [
        "3 ended cycle(s) are not measured yet; 3 ended before 014 are being measured, and no transition is"
        " written until they are"
    ]
    await measuring.watch(harness.ctx, state)  # within ten seconds: no backfill batch, and still nothing
    assert await changes(harness) == []
    await watch(harness, state)
    rows = await changes(harness)
    assert sorted((r["pair"], r["from"], r["to"], tuple(r["causes"])) for r in rows) == [
        ("unit-other", None, "not_ready", ("baseline",)),
        (PAIR, None, "parity_ready", ("baseline",)),
    ]
    assert all(row["changed"] == [] for row in rows)
    await watch(harness, state, after=measuring.READINESS_EVERY_S)
    assert len(await changes(harness)) == 2


async def test_each_trigger_and_what_a_row_names(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain row that may change a pair is judged 30 s after the last evaluation at the earliest; time
    alone every 15 minutes. Such a row only makes the evaluation due: what a pair's row names is what
    differs for the pair, here a state recorded otherwise, which no cause explains."""
    run_id = await converged(harness)
    state = measuring.State()
    await watch(harness, state)
    assert [row["causes"] for row in await changes(harness)] == [["baseline"]]
    for action in measuring.TRIGGERS:
        await unsettle(harness)
        await record(harness, action)  # of another object: never this pair's cause
        await watch(harness, state, after=measuring.READINESS_MIN_S - 1)
        assert (await changes(harness))[-1]["to"] == "unknown", action  # not yet
        await watch(harness, state, after=1)
        last = (await changes(harness))[-1]
        assert (last["from"], last["to"], last["causes"]) == ("unknown", "parity_ready", ["time"]), action
    # A row that changes no pair starts no evaluation: time alone does, after 15 minutes.
    await unsettle(harness)
    await record(harness, "key.view")
    await watch(harness, state, after=measuring.READINESS_EVERY_S - 1)
    assert (await changes(harness))[-1]["to"] == "unknown"
    await watch(harness, state, after=1)
    assert (await changes(harness))[-1]["causes"] == ["time"]
    # Each changed gate names its cause: a parity gate the cycle (fresh: time, on the same evidence
    # cycle), the others the fact they read; sign-off and pack the licence, between unavailable and not met.
    for gate, was, causes in (
        ("targets", "not_met", ["cycle"]),
        ("convergence", "unknown", ["cycle"]),
        ("fresh", "not_met", ["time"]),
        ("spec_approval", "met", ["signoff"]),
        ("adjudication", "not_met", ["adjudication"]),
        ("sample_ledger", "unknown", ["ledger"]),
        ("signoff", "met", ["signoff"]),
        ("pack", "met", ["pack"]),
        ("signoff", "not_met", ["licence"]),
        ("pack", "not_met", ["licence"]),
    ):
        await unsettle(harness, gates=REQUIRED | {gate: was})
        await record(harness, "run.cycle", "R-" + "Z" * 26)  # another run's cycle names nothing here
        await watch(harness, state, after=measuring.READINESS_MIN_S)
        last = (await changes(harness))[-1]
        assert (last["causes"], [c["gate"] for c in last["changed"]]) == (causes, [gate]), gate
    # Another evidence cycle is the cycle's, whatever its gates then do; several causes are named in a
    # fixed order, and the licence that cannot be read is none.
    earlier = {"run_id": run_id, "cycle": 2}
    await unsettle(harness, evidence=earlier, gates=REQUIRED | {"fresh": "not_met"})
    await watch(harness, state, after=measuring.READINESS_EVERY_S)
    assert (await changes(harness))[-1]["causes"] == ["cycle"]
    await unsettle(harness, gates=REQUIRED | {"targets": "not_met", "pack": "met", "adjudication": "met"})

    async def unreadable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the licence table is unreadable")

    monkeypatch.setattr(licence, "standing", unreadable)
    await watch(harness, state, after=measuring.READINESS_EVERY_S)
    last = (await changes(harness))[-1]
    assert last["causes"] == ["cycle", "pack", "adjudication"] and last["gates"] == REQUIRED  # the last known


async def test_a_new_version_and_changed_settings_are_causes(open_harness: OpenHarness) -> None:
    async with open_harness(NOWHERE) as h:
        await converged(h)
        state = measuring.State()
        await watch(h, state)
        newer = await stored(h, SOAK.replace('version: "1.0.0"', 'version: "1.1.0"'))
        run_id = await converged(h, newer, cycles=1)
        await watch(h, state)
        [_, version] = await changes(h)
        assert (version["to"], version["causes"]) == ("not_ready", ["cycle", "version"])
        assert version["evidence"] == {"run_id": run_id, "cycle": 1}
        assert [c["gate"] for c in version["changed"]] == ["convergence"]
        await watch(h, measuring.State())  # a restart: the settings are recorded once, and nothing changed
        assert len(await changes(h)) == 2 and len(await audited(h, "readiness.config")) == 1
    relaxed = {"gates": {"convergence": {"cycles": 1}}}
    async with open_harness(NOWHERE, readiness=relaxed) as h:
        async with h.ctx.pool.connection() as conn:
            found = await judging.judged(h.ctx, conn)
            worker = await judging.worker_config(conn)
        config = readiness.config_sha256(h.ctx.settings.readiness)
        assert (
            judging.notes(found, config, worker)
            == [  # until the worker restarts too
                f"this server judges with readiness settings {config[:12]}, the worker with"
                f" {readiness.config_sha256(None)[:12]}: restart both after changing them"
            ]
        )
        await watch(h, measuring.State())
        [_, was, now] = await audited(h, "readiness.config"), *(await changes(h))[-2:]
        assert (now["from"], now["to"], now["causes"]) == ("not_ready", "parity_ready", ["config"])
        assert now["config_sha256"] == config != was["config_sha256"]
        [first, second] = await audited(h, "readiness.config")
        assert second["detail"]["from_sha256"] == first["detail"]["sha256"] == readiness.config_sha256(None)
        assert (second["detail"]["sha256"], second["detail"]["gates"]["cycles"]) == (config, 1)
        assert second["detail"]["deviations"] == [
            {"pair": None, "deviations": ["convergence 1 cycle (default 3; relaxed)"]}
        ]
        # The settings are named by the rows written with their record, and by no later one.
        rows = await audited(h, "readiness.changed")
        assert second["chain"]["seq"] == rows[-1]["chain"]["seq"] - 1
        state = measuring.State()
        await watch(h, state)  # another restart under the same settings
        await ended(h, run_id, FAILING)
        await watch(h, state)
        fell = (await changes(h))[-1]
        assert (fell["to"], fell["causes"], fell["config_sha256"]) == ("not_ready", ["cycle"], config)


async def test_a_chain_row_committed_after_a_higher_id_is_read(harness: Harness) -> None:
    """The chain's seq is commit order: a row that drew its id first and committed last is still followed,
    which a cursor on the audit log's ids would miss."""
    await converged(harness)
    state = measuring.State()
    await watch(harness, state)
    async with harness.ctx.pool.connection() as conn:
        held = (await (await conn.execute("SELECT nextval('ds.audit_log_id_seq')")).fetchone())[0]  # type: ignore[index]
    await unsettle(harness)
    later = await record(harness, "key.view")  # a higher id, committed first
    await watch(harness, state, after=measuring.READINESS_MIN_S)
    assert state.seq == later and (await changes(harness))[-1]["to"] == "unknown"  # followed to the head
    async with harness.ctx.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ds.audit_log (id, actor, action, object) VALUES (%s, %s, 'pack.revoke', 'x')",
            (held, TESTER.actor),
        )
        row = await (await conn.execute("SELECT seq FROM ds.audit_chain WHERE id = %s", (held,))).fetchone()
    assert row is not None and row[0] > later
    await watch(harness, state, after=measuring.READINESS_MIN_S)  # due for the row that committed late
    assert state.seq == row[0] and (await changes(harness))[-1]["to"] == "parity_ready"


async def test_a_pair_with_a_cycle_still_to_measure_gets_no_row(harness: Harness) -> None:
    """A pass measures ten cycles and then judges: a pair one of whose cycles was never measured is judged
    by the pass that measures it, so a wave of more than ten cycles chains no baseline of half a history
    and no fall of a pair that never left parity."""
    runs = []
    for n in range(11):
        run_id = await converged(harness, await stored(harness, SOAK.replace("unit-runner", f"unit-{n:02d}")))
        runs.append(run_id)
    state = measuring.State()
    await measuring.watch(harness.ctx, state)  # the newest 10 of 33 cycles: three pairs are whole
    first = await changes(harness)
    assert sorted(row["pair"] for row in first) == ["unit-08", "unit-09", "unit-10"]
    for _ in range(3):
        await measuring.watch(harness.ctx, state)
    rows = await changes(harness)
    assert len(rows) == 11 and {(row["from"], row["to"], *row["causes"]) for row in rows} == {
        (None, "parity_ready", "baseline")
    }
    for run_id in runs:  # an hour later every soak's fourth cycle ends clean, in one sweep
        await ended(harness, run_id, MATCH)
    await measuring.watch(harness.ctx, state)  # ten are measured: the eleventh pair waits, unjudged
    async with harness.ctx.pool.connection() as conn:
        found = await judging.judged(harness.ctx, conn)
    assert list(found.waiting) == ["unit-00"] and found.items["unit-00"]["state"] == "unknown"
    await measuring.watch(harness.ctx, state)
    await measuring.watch(harness.ctx, state)
    assert len(await changes(harness)) == 11  # nothing changed for any of them


async def test_a_cycle_ending_during_a_pass_is_judged_by_the_pass_that_measures_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep ends cycles while a pass measures: one that ends after the pass read its batch leaves its
    pair as recorded until the next pass, which measures it and chains what it changed, once."""
    run_id = await converged(harness)
    other = await converged(harness, await stored(harness, SOAK.replace("unit-runner", "unit-other")))
    state = measuring.State()
    await watch(harness, state)
    await ended(harness, run_id, MATCH)
    measured = measuring._measured

    async def meanwhile(ctx: Any, conn: Any, row: Any, specs: Any) -> Any:
        found = await measured(ctx, conn, row, specs)
        await ended(harness, other, FAILING)  # the other pair's cycle ends while this one is measured
        return found

    monkeypatch.setattr(measuring, "_measured", meanwhile)
    await watch(harness, state)
    monkeypatch.setattr(measuring, "_measured", measured)
    assert len(await changes(harness)) == 2  # the baselines: no row says unit-other went unknown
    await watch(harness, state)
    [fell] = (await changes(harness))[2:]
    assert (fell["pair"], fell["from"], fell["to"], fell["causes"]) == (
        "unit-other", "parity_ready", "not_ready", ["cycle"],
    )  # fmt: skip
    await watch(harness, state, after=measuring.READINESS_EVERY_S)
    assert len(await changes(harness)) == 3


async def test_a_failed_evaluation_is_tried_again_later_not_every_pass(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An evaluation the server cancels (a statement over its timeout) is logged and tried again 30 s later:
    the passes between only measure."""
    await converged(harness)
    state = measuring.State()
    await watch(harness, state)
    await unsettle(harness)
    series, calls = repo.readiness_series, []

    async def cancelled(conn: repo.Conn, pairs: Any, **options: Any) -> Any:
        calls.append(pairs)
        await conn.execute("SET LOCAL statement_timeout = '50ms'")
        await conn.execute("SELECT pg_sleep(1)")
        return await series(conn, pairs, **options)

    monkeypatch.setattr(repo, "readiness_series", cancelled)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.runner.readiness"):
        await watch(harness, state, after=measuring.READINESS_EVERY_S)
        for _ in range(4):  # the worker's passes, a poll apart
            await measuring.watch(harness.ctx, state)
    logged = [r.getMessage() for r in caplog.records if r.name == "deltasleuth.runner.readiness"]
    assert len(calls) == 1 and logged == ["readiness evaluation failed: QueryCanceled"]  # the audit log apart
    await watch(harness, state, after=measuring.READINESS_MIN_S)
    assert len(calls) == 2 and (await changes(harness))[-1]["to"] == "unknown"
    monkeypatch.setattr(repo, "readiness_series", series)
    await watch(harness, state, after=measuring.READINESS_MIN_S)
    assert (await changes(harness))[-1]["to"] == "parity_ready"


async def test_timed_fresh(open_harness: OpenHarness) -> None:
    """Time alone turns a pair: its evidence cycle is no longer fresh, which the 15-minute evaluation says."""
    async with open_harness(NOWHERE, readiness={"gates": {"fresh": {"max_age": "1h"}}}) as h:
        run_id = await converged(h)
        state = measuring.State()
        await watch(h, state)
        assert (await changes(h))[-1]["to"] == "parity_ready"
        async with h.ctx.pool.connection() as conn:  # an hour passes
            await conn.execute(
                "UPDATE ds.cycles SET ended_at = ended_at - interval '61 minutes' WHERE run_id = %s",
                (run_id,),
            )
        await watch(h, state, after=measuring.READINESS_EVERY_S - 1)
        assert len(await changes(h)) == 1
        await record(h, "run.cycle", "R-" + "Z" * 26)  # another run's cycle starts the evaluation: no cause
        await watch(h, state, after=1)
        stale = (await changes(h))[-1]
        assert (stale["to"], stale["causes"]) == ("not_ready", ["time"])
        [fresh] = stale["changed"]
        assert (fresh["gate"], fresh["to"], fresh["allowed"]) == ("fresh", "not_met", 3600)
        assert fresh["value"] > 3600 and fresh["over"] == fresh["value"] - 3600


async def test_a_closed_pair_is_judged_as_of_its_day_then_silent(open_harness: OpenHarness) -> None:
    today = datetime.now(UTC).date()
    listed = {"spec": PAIR, "entity": "item"}
    async with open_harness(
        NOWHERE, readiness={"programmes": [{"name": "cutover-q4", "pairs": [listed]}]}
    ) as h:
        run_id = await converged(h)
        async with h.ctx.pool.connection() as conn:  # the three cycles ended two days ago
            await conn.execute("UPDATE ds.cycles SET ended_at = ended_at - interval '2 days'")
        state = measuring.State()
        await watch(h, state)
        [baseline] = await changes(h)
        assert (baseline["programme"], baseline["entity"], baseline["to"]) == (
            "cutover-q4",
            "item",
            "parity_ready",
        )
        await unsettle(h, days_ago=3)  # what was last recorded before its day
    closed = listed | {"closed": (today - timedelta(days=1)).isoformat()}
    async with open_harness(
        NOWHERE, readiness={"programmes": [{"name": "cutover-q4", "pairs": [closed]}]}
    ) as h:
        state = measuring.State()
        await watch(h, state)
        last = (await changes(h))[-1]
        assert (last["to"], last["causes"]) == ("parity_ready", ["config", "closed"])
        await ended(h, run_id, FAILING)  # after its day: measured, never judged
        await unsettle(h)
        await watch(h, state, after=measuring.READINESS_EVERY_S)
        assert (await changes(h))[-1]["to"] == "unknown"  # silent, whatever is recorded or observed
        async with h.ctx.pool.connection() as conn:
            item = (await judging.judged(h.ctx, conn)).items[PAIR]
        assert (item["state"], item["open"], item["evidence"]["cycle"]) == ("parity_ready", False, 3)
        assert item["notes"] == [f"closed on {closed['closed']}: judged as of that day"]


async def test_the_notifier_routes_a_change_and_withholds_a_key_shaped_name(
    open_harness: OpenHarness, tmp_path: Path, loopback: Any
) -> None:
    """The chained row is an event. A rule takes it by cause, programme, state and gate; a
    baseline goes only to a rule that lists it; a pair whose name its key pattern matches is never named."""
    shaped = SOAK.replace("unit-runner", "k001").replace("class: business_code", "class: identifier")
    programmes = [{"name": "cutover-q4", "pairs": [{"spec": PAIR, "entity": "item"}]}]
    async with receiving(tmp_path) as (_, base, ref):
        notify = {
            "label": "test",
            "channels": {"ops-hook": hook(base, ref)},
            "rules": [
                rule("baselines", ["readiness.changed"], ["ops-hook"], causes=["baseline"]),
                rule(
                    "falls", ["readiness.changed"], ["ops-hook"], causes=["cycle"], programmes=["cutover-*"],
                    states=["not_ready"], gates=["targets"],
                ),
                rule("others", ["readiness.changed"], ["ops-hook"], causes=["cycle"], specs=["k*"]),
            ],
        }  # fmt: skip
        async with open_harness(NOWHERE, notify=notify, readiness={"programmes": programmes}) as h:
            notifier = Notifier(h.ctx)
            try:
                await notifier.feed()  # the cursor starts at the head
                run_id, other = await converged(h), await converged(h, await stored(h, shaped))
                state = measuring.State()
                await watch(h, state)
                await ended(h, run_id, FAILING)
                await ended(h, other, FAILING)
                await watch(h, state)
                await notifier.feed()
                sent = [
                    json.loads(d["body"]) | {"rules": d["rules"], "key": d["dedup_key"]}
                    for d in await deliveries(h)
                ]
            finally:
                await notifier.close()
    by = {(doc["facts"]["pair"], doc["facts"]["to"]): doc for doc in sent}
    assert sorted(by, key=str) == sorted(
        [(None, "parity_ready"), (None, "not_ready"), (PAIR, "parity_ready"), (PAIR, "not_ready")], key=str
    )
    fell = by[PAIR, "not_ready"]
    assert (fell["rules"], fell["severity"], fell["event"]) == (["falls"], "alert", "readiness.changed")
    assert fell["key"] == f"readiness.changed|{PAIR}|not_ready|300"  # targets and convergence, of 12 bits
    assert fell["link"] is None and fell["withheld"] == {}  # developer mode has no public URL
    assert fell["text"].startswith(
        f"[test] cutover-q4 / {PAIR}: readiness parity ready -> not ready (3 of 5 gates; targets not met,"
        f" convergence not met; cycle 4 of run {run_id}). Observed; readiness is not a sign-off."
    )
    assert (
        by[PAIR, "parity_ready"]["rules"] == ["baselines"] and by[PAIR, "parity_ready"]["severity"] == "info"
    )
    hidden = by[None, "not_ready"]
    assert (hidden["rules"], hidden["withheld"]) == (["others"], {"pair": "key_shaped"})
    assert hidden["title"] == "a pair: readiness parity ready -> not ready" and "k001" not in json.dumps(
        {name: value for name, value in hidden.items() if name != "key"}
    )


async def test_packs_still_verify(open_harness: OpenHarness, tmp_path: Path) -> None:
    """Readiness's rows are chained on objects of their own (``readiness:<pair>``, ``readiness`` and the
    installation), so no pack's audit segment holds one, and a pack issued among them verifies; its issue is
    the cause of the pair's last rung."""
    key = tmp_path / "secrets" / "pack-signing-key"
    key.parent.mkdir()
    private = ec.generate_private_key(ec.SECP256R1())
    key.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    async with open_harness(NOWHERE, readiness={"gates": {"convergence": {"cycles": 1}}}) as plain:
        h = Harness(with_packs(plain.ctx, key))  # oidc mode, with evidence packs
        state = measuring.State()
        signed = await signed_cycle(h.ctx)
        await watch(h, state)
        issued = await pack.issue(h.ctx, signed.run_id, 1, who=signed.people["olga"])
        await watch(h, state, after=measuring.READINESS_MIN_S)
        async with h.ctx.pool.connection() as conn:
            data = await repo.pack_zip(conn, issued["pack_id"])
            item = (await judging.judged(h.ctx, conn)).items["unit-reports"]
            segment = await repo.audit_segment(conn, issued["manifest"]["audit"]["objects"], 10**9)
        rows = await changes(h, "unit-reports")
    assert data is not None and not any(row["action"].startswith("readiness.") for row in segment)
    (tmp_path / "pack.zip").write_bytes(data)
    steps = pack_verify.verify(tmp_path / "pack.zip", h.ctx.signer.fingerprint)  # type: ignore[union-attr]
    assert [step.number for step in steps if step.ok is False] == []
    assert [(row["from"], row["to"], row["causes"]) for row in rows] == [
        (None, "signed_off", ["baseline"]), ("signed_off", "evidenced", ["pack"]),
    ]  # fmt: skip
    assert rows[-1]["pack_id"] == issued["pack_id"] == item["pack_id"]
    assert rows[-1]["changed"] == [
        {"gate": "pack", "from": "not_met", "to": "met", "value": None, "allowed": None, "over": None}
    ]
    assert (item["state"], item["score"]) == ("evidenced", {"passed": 8, "required": 8})


async def test_a_row_names_its_pair_s_cause_after_a_restart_and_beside_a_late_commit(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row's causes are what differs for its pair, not what the worker saw enter the chain: a pack issued
    after an evaluation read the chain, and a sign-off revoked while no worker ran, are named all the same."""
    async with open_harness(NOWHERE, readiness={"gates": {"convergence": {"cycles": 1}}}) as plain:
        h = Harness(with_packs(plain.ctx, signing_key(tmp_path)))
        state = measuring.State()
        cycle = await signed_cycle(h.ctx)
        await watch(h, state)
        assert [row["to"] for row in await changes(h, "unit-reports")] == ["signed_off"]
        judged = judging.judged

        async def late(ctx: Any, conn: Any, **options: Any) -> Any:
            # committed by another transaction after this evaluation read the chain, before it reads the pairs
            await pack.issue(h.ctx, cycle.run_id, 1, who=cycle.people["olga"])
            return await judged(ctx, conn, **options)

        monkeypatch.setattr(judging, "judged", late)
        await watch(h, state, after=measuring.READINESS_EVERY_S)  # the 15-minute evaluation
        monkeypatch.setattr(judging, "judged", judged)
        issued = (await changes(h, "unit-reports"))[-1]
        assert (issued["from"], issued["to"], issued["causes"]) == ("signed_off", "evidenced", ["pack"])
        await watch(h, state, after=measuring.READINESS_MIN_S)  # the pack's row is followed: nothing is left
        assert len(await changes(h, "unit-reports")) == 2
        async with repo.audited(h.ctx.pool) as (conn, audit):  # while the worker is down
            await approvals.revoke(
                conn, audit, cycle.signoff_id, "a defect was found", who=cycle.people["erin"]
            )
        await watch(h, measuring.State())  # a new worker: its cursor starts at the head, after the revoke
        fell = (await changes(h, "unit-reports"))[-1]
        assert (fell["from"], fell["regressed"], fell["causes"]) == ("evidenced", False, ["signoff", "pack"])
        assert [(c["gate"], c["from"], c["to"]) for c in fell["changed"]] == [
            ("signoff", "met", "not_met"), ("pack", "met", "not_met"),
        ]  # fmt: skip


async def test_a_fall_after_an_unrelated_settings_edit_names_no_config_and_reaches_its_rule(
    open_harness: OpenHarness, tmp_path: Path, loopback: Any
) -> None:
    """Editing the settings for another pair leaves this one's next row its own causes: a rule of
    every cause but baseline and config delivers its fall."""
    listed = {"name": "cutover-q4", "pairs": [{"spec": PAIR}]}
    edited = [listed, {"name": "decom-q1", "pairs": [{"spec": "some-other-pair"}]}]
    causes = [cause for cause in readiness.CAUSES if cause not in ("baseline", "config")]
    async with receiving(tmp_path) as (_, base, ref):
        notify = {
            "label": "test",
            "channels": {"owners": hook(base, ref)},
            "rules": [
                rule("cutover", ["readiness.changed"], ["owners"], programmes=["cutover-q4"], causes=causes)
            ],
        }
        async with open_harness(NOWHERE, notify=notify, readiness={"programmes": [listed]}) as h:
            run_id = await converged(h)
            await watch(h, measuring.State())
        async with open_harness(NOWHERE, notify=notify, readiness={"programmes": edited}) as h:
            notifier = Notifier(h.ctx)
            try:
                await notifier.feed()  # the cursor starts at the head
                state = measuring.State()
                await watch(h, state)  # the restart records the settings: nothing changed for the pair
                assert len(await audited(h, "readiness.config")) == 2 and len(await changes(h, PAIR)) == 1
                await ended(h, run_id, FAILING)
                await watch(h, state)
                fell = (await changes(h, PAIR))[-1]
                assert (fell["from"], fell["to"], fell["causes"]) == ("parity_ready", "not_ready", ["cycle"])
                await notifier.feed()
                sent = [d for d in await deliveries(h) if f"|{PAIR}|" in d["dedup_key"]]
            finally:
                await notifier.close()
    assert [(d["severity"], d["state"], d["rules"]) for d in sent] == [("alert", "pending", ["cutover"])]


async def test_a_sampled_pair_s_draw_discipline_is_read_once_its_parity_gates_are_met(
    open_harness: OpenHarness, tmp_path: Path
) -> None:
    """A sampled candidate's ledger is read only when every parity gate is met; its pair then shows
    the evidence cycle's strata as counts, never a pooled rate."""
    key = tmp_path / "secrets" / "pack-signing-key"
    key.parent.mkdir()
    private = ec.generate_private_key(ec.SECP256R1())
    key.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    async with open_harness(NOWHERE) as plain:
        h = Harness(with_packs(plain.ctx, key))
        drawn = await sampled_cycle(h.ctx, sign=False)
        await watch(h, measuring.State())
        at = f"cycle 1 of run {drawn.run_id}"
        async with h.ctx.pool.connection() as conn:
            item = (await judging.judged(h.ctx, conn)).items["customer-profile"]
        by = {gate["gate"]: gate for gate in item["gates"]}
        assert (item["state"], by["convergence"]["status"]) == ("not_ready", "not_met")  # 1 of 3 cycles
        assert by["population"]["text"] == (
            f"{at} compared bottom_k sample {drawn.draw_id}, its frame reading side a whole"
        )
        assert by["targets"]["text"] == f"sample {drawn.draw_id} of {at} passed every stratum at its tier"
        assert (by["sample_ledger"]["status"], by["sample_ledger"]["text"]) == (
            "unknown",
            f"the draw discipline of draw {drawn.draw_id} of {at} is read once every parity gate is met",
        )
        relaxed = TypeAdapter(ReadinessSettings).validate_python({"gates": {"convergence": {"cycles": 1}}})
        ctx = replace(h.ctx, settings=replace(h.ctx.settings, readiness=relaxed))
        found = await judging.pair(ctx, "customer-profile", precheck=True)
    by = {gate["gate"]: gate for gate in found["gates"]}
    assert (by["sample_ledger"]["status"], by["sample_ledger"]["text"]) == (
        "met",
        f"draw {drawn.draw_id} of {at} meets the draw discipline",
    )
    assert (found["state"], found["blocking"], found["precheck"]["problems"]) == (
        "signoff_ready", ["signoff", "pack"], [],
    )  # fmt: skip
    assert [(s["label"], s["basis"], s["drawn"], s["n"], s["x"], s["verdict"]) for s in found["strata"]] == [
        ("segment=retail", "census", 3, 3, 0, "passed"),
        ("segment=other", "census", 3, 3, 0, "passed"),
    ]
    assert not any("rate" in name or "ub95" in name for stratum in found["strata"] for name in stratum)


@pytest.mark.usefixtures("vendor_keys")
async def test_the_key_census_is_counted_for_readiness_in_tokens_mode(
    on_tables: Tables,  # noqa: F811
) -> None:
    """With ``readiness`` configured (``{}`` suffices) the worker counts a key-list run's table side before
    its keys are leased, in tokens mode and under a Community licence too: its cycles are evidence, and the
    pair reaches parity-ready, where tokens mode stops."""
    seed = "CREATE TABLE accounts (id text, email text); INSERT INTO accounts VALUES ('1', 'a'), ('2', 'b');"
    execution = {"concurrency": 2, "cycleInterval": "0s", "stop": {"maxCycles": 3}}
    _, harness = on_tables(seed, auth={"public_url": "https://ds.test"}, readiness={})
    async with harness as h:
        await licensed(h.ctx, tier="community")
        spec_id = await h.spec(tables_spec(accounts(), accounts(), execution=execution))
        run_id = await create_run(h.ctx, spec_id, "soak", ["1", "2"], soak={"rolling": False}, who=TESTER)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(h.ctx, stop=stop))
        try:
            await h.wait(run_id)
            found: list[dict[str, Any]] = []

            async def recorded() -> None:
                found[:] = await changes(h, "accounts")

            deadline = asyncio.get_running_loop().time() + 30
            while not (found and found[-1]["to"] == "parity_ready"):
                assert asyncio.get_running_loop().time() < deadline, found
                await asyncio.sleep(0.1)
                await recorded()
        finally:
            stop.set()
            await worker
        async with h.ctx.pool.connection() as conn:
            item = (await judging.judged(h.ctx, conn)).items["accounts"]
    by = {gate["gate"]: gate for gate in item["gates"]}
    assert (item["state"], item["score"]) == ("parity_ready", {"passed": 5, "required": 5})
    assert by["population"]["status"] == "met" and by["population"]["text"].startswith(
        f"cycle 3 of run {run_id} observed side a whole (its key set checked against public.accounts in 1"
        " snapshot at "
    )
    assert by["signoff"]["text"] == "sign-off needs SSO (auth.oidc); this deployment runs in tokens mode"
    assert found[-1]["gates"]["signoff"] == "unavailable"


def signing_key(tmp_path: Path) -> Path:
    key = tmp_path / "secrets" / "pack-signing-key"
    key.parent.mkdir()
    private = ec.generate_private_key(ec.SECP256R1())
    key.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return key


async def test_a_cycle_that_is_no_evidence_is_never_the_candidate(
    open_harness: OpenHarness, tmp_path: Path
) -> None:
    """Under SSO: a four-eyes sign-off of five keys given at creation (no census: no evidence) leaves
    the pair unsigned, and an adjudication a person scored on a captured-sample replay (excluded) does
    not make its cycle the candidate, whose sign-off would be refused."""
    T0 = datetime.now(UTC) - timedelta(hours=10)
    version = compile_spec(parse_yaml_text(SOAK))
    sso = {"needs": approvals.needs(version), "mode": "oidc"}
    async with open_harness(NOWHERE) as plain:
        h = Harness(with_packs(plain.ctx, signing_key(tmp_path)))
        await licensed_once(h.ctx)
        async with h.ctx.pool.connection() as conn:
            amy, bob = await person(conn, "amy", "spec_author"), await person(conn, "bob")
            olga, sue = await person(conn, "olga", "operator"), await person(conn, "sue", "operator")
            rita, ruth = await person(conn, "rita"), await person(conn, "ruth")
            spec_id = await upload(conn, version, amy)
            approval_id = await approved(conn, spec_id, amy, bob, scopes=approvals.needs(version))

        async def run(at: timedelta, results: Any = MATCH, **params: Any) -> str:
            """A run of the approved version, started under SSO, its next cycle ended ``at`` after T0."""
            run_id = await started(h, spec_id, approval=sso, **params)
            async with h.ctx.pool.connection() as conn:
                await repo.set_run_approval(conn, run_id, approval_id)
            await ended(h, run_id, results, at=T0 + at)
            return run_id

        async def pair() -> dict[str, Any]:
            async with h.ctx.pool.connection() as conn:
                return (await judging.judged(h.ctx, conn)).items[PAIR]

        census = await run(timedelta(hours=0), scan=SCANNED, **SCAN)
        for hour in (1, 2):
            await ended(h, census, MATCH, at=T0 + timedelta(hours=hour))
        state = measuring.State()
        await watch(h, state)
        at = {"run_id": census, "cycle": 3}
        ready = (
            f"request sign-off of cycle 3 of run {census} on the Run screen, the newest cycle in the streak"
        )
        before = await pair()
        assert (before["state"], before["candidate"]) == ("signoff_ready", at)
        # (a) Five keys given at creation: its cycle may be signed off, and the pair is not.
        five = dict.fromkeys(KEYS[:5], "MATCH")
        listed = await run(timedelta(hours=3), five, mode="batch", keys=KEYS[:5], keys_source="explicit")
        await watch(h, state)
        measured = await measures(h, listed)
        assert measured is not None and (measured["evidence"], measured["basis"]) == (False, "keys")
        async with h.ctx.pool.connection() as conn:
            run_row = await repo.get_run(conn, listed)
            assert run_row is not None and await approvals.signoff_problems(conn, run_row, 1, version) == []
            signoff = await signed(conn, listed, 1, version, olga, rita, ruth)
        after = await pair()
        assert (after["state"], after["candidate"], after["signoff_id"], after["regressed"]) == (
            "signoff_ready", at, None, False,
        )  # fmt: skip
        assert (after["score"], after["evidence"]["run_id"]) == (before["score"], census)
        by = {gate["gate"]: gate for gate in after["gates"]}
        assert by["signoff"]["status"] == "not_met" and by["signoff"]["text"].startswith(
            f"cycle 1 of run {listed} was signed off ({signoff}), but is not evidence (a key list whose"
            f" census does not count side a): only a sign-off of an evidence cycle counts; {ready}"
        )
        # (b) A captured-sample replay, adjudicated by a person: its cycle is no one's candidate.
        replay = await run(timedelta(hours=4), mode="batch", samples_dir="/captured")
        async with h.ctx.pool.connection() as conn:
            await scored(conn, replay, 1, sue)
        await watch(h, state, after=measuring.READINESS_EVERY_S)
        assert await measures(h, replay) == {"format": 1, "excluded": "captured_samples"}
        replayed = await pair()
        assert (replayed["state"], replayed["candidate"]) == ("signoff_ready", at)
        assert ready in {gate["gate"]: gate["text"] for gate in replayed["gates"]}["signoff"]
        found = await judging.pair(h.ctx, PAIR, precheck=True)
        assert found["precheck"]["run_id"] == census and found["precheck"]["problems"] == []
        [baseline] = await changes(h, PAIR)  # one row: neither cycle changed what was recorded
        assert (baseline["to"], baseline["candidate"], baseline["signoff_id"]) == ("signoff_ready", at, None)


async def test_a_revoked_sign_off_is_never_kept_met_by_an_unreadable_licence(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the licence standing unreadable, a sign-off (and its pack) revoked since the pair was recorded
    reads unknown, never the met it had: the pair leaves signed off, and the change is chained."""
    async with open_harness(NOWHERE, readiness={"gates": {"convergence": {"cycles": 1}}}) as plain:
        h = Harness(with_packs(plain.ctx, signing_key(tmp_path)))
        state = measuring.State()
        cycle = await signed_cycle(h.ctx)
        await watch(h, state)
        assert [row["to"] for row in await changes(h, "unit-reports")] == ["signed_off"]
        async with repo.audited(h.ctx.pool) as (conn, audit):
            await approvals.revoke(
                conn, audit, cycle.signoff_id, "a defect was found", who=cycle.people["erin"]
            )

        async def unreadable(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the licence table is unreadable")

        monkeypatch.setattr(licence, "standing", unreadable)
        async with h.ctx.pool.connection() as conn:
            item = (await judging.judged(h.ctx, conn)).items["unit-reports"]
        by = {gate["gate"]: (gate["status"], gate["text"]) for gate in item["gates"]}
        assert (item["state"], item["signoff_id"], item["blocking"]) == (
            "parity_ready",
            None,
            ["signoff", "pack"],
        )
        assert by["signoff"] == ("unknown", readiness.LICENCE_UNREAD)
        assert by["pack"] == ("not_met", f"{readiness.LICENCE_UNREAD}: last known")  # as the licence left it
        await watch(h, state, after=measuring.READINESS_MIN_S)
        fell = (await changes(h, "unit-reports"))[-1]
        assert (fell["from"], fell["to"], fell["causes"]) == ("signed_off", "parity_ready", ["signoff"])
        assert [(c["gate"], c["from"], c["to"]) for c in fell["changed"]] == [("signoff", "met", "unknown")]


async def test_measures_that_cannot_be_read_never_stop_the_other_pairs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A record of another format is an unknown point, whatever it says; and a record this build cannot
    read at all leaves its own pair unknown, told and logged by name, while the others are judged and their
    changes chained."""
    run_id = await converged(harness)
    other = await converged(harness, await stored(harness, SOAK.replace("unit-runner", "unit-other")))
    state = measuring.State()
    await watch(harness, state)
    assert [(row["pair"], row["to"]) for row in await changes(harness)] == [
        ("unit-other", "parity_ready"),
        (PAIR, "parity_ready"),
    ]

    async def judged() -> dict[str, dict[str, Any]]:
        async with harness.ctx.pool.connection() as conn:
            return (await judging.judged(harness.ctx, conn)).items

    async def rewrite(record: str) -> None:
        async with harness.ctx.pool.connection() as conn:
            await conn.execute(
                f"UPDATE ds.cycles SET readiness = {record} WHERE run_id = %s AND cycle = 3",  # noqa: S608
                (other,),
            )

    await rewrite("""readiness || '{"format": 2}'""")  # as a later build would leave it: evidence, no fails
    item = (await judged())["unit-other"]
    by = {gate["gate"]: (gate["status"], gate["text"]) for gate in item["gates"]}
    assert (item["state"], item["evidence"]["cycle"], item["convergence"]["streak"]) == ("unknown", 2, 0)
    assert by["convergence"] == (
        "unknown",
        f"the streak of 1.0.0 (spec {item['spec_id']}) reaches cycle 3 of run {other}, whose measures are of"
        " format 2, which this version does not read",
    )
    assert [p["cycle"] for p in item["trend"]] == [1, 2] and by["targets"][0] == "met"
    rows = (await judging.cycles(harness.ctx, "unit-other"))["items"]
    daily = (await judging.cycles(harness.ctx, "unit-other", bucket="day"))["items"]
    assert [(row["cycle"], row["fails"], row["skipped"], row["total"]) for row in rows] == [
        (1, [], False, 1000), (2, [], False, 1000), (3, ["unknown"], True, None),
    ]  # fmt: skip
    assert [row["cycle"] for row in daily] == [2]
    # A record of this format without its fields: its pair cannot be evaluated; the other one's fall is.
    await rewrite("jsonb_build_object('format', 1, 'pair', 'unit-other', 'evidence', true, 'spec_id', 1)")
    await ended(harness, run_id, FAILING)
    with caplog.at_level(logging.WARNING, logger="deltasleuth.readiness"):
        await watch(harness, state)
    assert "readiness: pair unit-other could not be evaluated: KeyError" in caplog.messages
    assert not [message for message in caplog.messages if message.startswith("readiness pass failed")]
    items = await judged()
    why = "unit-other could not be evaluated (KeyError): see the server log"
    assert (items["unit-other"]["state"], items["unit-other"]["notes"]) == ("unknown", [why])
    assert {gate["status"] for gate in items["unit-other"]["gates"]} == {"unknown"}
    assert items[PAIR]["state"] == "not_ready"
    assert [(row["pair"], row["from"], row["to"]) for row in (await changes(harness))[2:]] == [
        ("unit-other", "parity_ready", "unknown"),
        (PAIR, "parity_ready", "not_ready"),
    ]
    listed = (await judging.overview(harness.ctx))["items"]
    assert [(i["pair"], i["state"]) for i in listed] == [(PAIR, "not_ready"), ("unit-other", "unknown")]
