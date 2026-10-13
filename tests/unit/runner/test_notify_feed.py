"""The feed (docs/mvp/ALERTS.md §4): the chain is the outbox. Each fed row is turned into deliveries exactly
once beside concurrent writers and across a crash; the cursor starts at the head; a poison row is skipped
with its seq while later rows notify; a lost connection moves nothing; a stale backlog is skipped in one
step; suppression, several rules and the cycle's digest over key-list, rolling, table-scan and sampled
soaks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from deltasleuth import approvals, notifications
from deltasleuth.core.notify import COUNTERS, Event
from deltasleuth.runner import drift
from deltasleuth.runner import scan as scanning
from deltasleuth.runner.notify import Notifier
from deltasleuth.runner.runs import AUTH_PAUSE, KEY_PAUSE, create_run, resume_run
from deltasleuth.store import repo
from tests.conftest import TESTER, Loopback
from tests.unit.core.notified import CHANGED, DRIFT, GUARD_PAUSE, PAIR

from .conftest import OpenHarness, until
from .notifying import (
    EMPTY,
    HEALTH,
    audits,
    cursor,
    deliveries,
    ended,
    hook,
    notifying,
    queued,
    receiving,
    rule,
    stall_in_digest,
    write,
)
from .prechecking import table_spec
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("loopback")]
NOWHERE = "http://127.0.0.1:9"  # the targets: no run here reads them
PAUSED = {"reason": AUTH_PAUSE}  # an alert: never rate limited


async def test_the_cursor_starts_at_the_head(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        before = [await write(h, "paused", run_id, PAUSED) for _ in range(3)]
        await notifier.feed()  # the first configured pass: rows before it are never notified
        assert await cursor(h) == before[-1] and await deliveries(h) == []
        seq = await write(h, "paused", run_id, PAUSED)
        await notifier.feed()
        (found,) = await deliveries(h)
        assert (found["seq"], found["event"], found["event_id"], found["state"]) == (
            seq,
            "run.paused",
            f"seq:{seq}:run.paused",
            "pending",
        )
        assert await cursor(h) == seq


async def test_exactly_once_beside_dsau_writers(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        written: list[int] = []
        done = asyncio.Event()

        async def writer() -> None:
            for _ in range(25):
                written.append(await write(h, "paused", run_id, PAUSED))
                await write(h, "key.view", run_id, {"cycle": 1})  # not fed
                await asyncio.sleep(random.random() / 100)  # noqa: S311

        async def feeding() -> None:
            while not done.is_set():
                await notifier.feed()

        feeder = asyncio.create_task(feeding())
        await asyncio.gather(*(writer() for _ in range(4)))
        done.set()
        await feeder
        await notifier.feed()
        stored = await deliveries(h)
        assert sorted(d["seq"] for d in stored) == sorted(written)  # none missed, none twice
        assert len({d["event_id"] for d in stored}) == len(written) == 100


async def test_a_crash_writes_nothing_twice(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        start = await cursor(h)
        seqs = [await write(h, "paused", run_id, PAUSED) for _ in range(3)]
        move = repo.move_cursor

        async def crash(*args: Any) -> None:
            raise RuntimeError("killed before the commit")

        monkeypatch.setattr(repo, "move_cursor", crash)
        with pytest.raises(RuntimeError):
            await notifier.feed()
        assert await deliveries(h) == [] and await cursor(h) == start  # rolled back together
        monkeypatch.setattr(repo, "move_cursor", move)
        await notifier.feed()
        await notifier.feed()
        assert [d["seq"] for d in await deliveries(h)] == seqs


async def test_a_poison_row_is_skipped_with_its_seq(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        good = await write(h, "paused", run_id, PAUSED)
        error = await write(h, "paused", "R-" + "Z" * 26, PAUSED)  # no such run: the projection fails
        unsafe = await write(
            h, "paused", run_id, {"reason": "run_paused_4111111111111111"}
        )  # fails its model
        later = await write(h, "paused", run_id, {"reason": KEY_PAUSE})
        await notifier.feed()
        assert [d["seq"] for d in await deliveries(h)] == [good, later]
        skipped = [r["detail"] for r in await audits(h, "notify.skipped")]
        assert skipped == [
            {"reason": "error", "rows": 1, "seqs": [error]},
            {"reason": "unsafe", "rows": 1, "seqs": [unsafe]},
        ]
        assert await cursor(h) == later  # the head read first: the skips come after it
        await notifier.feed()  # the skips are fed in turn; a notify.* row is never counted in a skip
        assert [d["event"] for d in await deliveries(h)][-2:] == ["notify.skipped", "notify.skipped"]
        assert "4111111111111111" not in json.dumps([dict(d) for d in await deliveries(h)], default=str)


async def test_a_lost_connection_moves_nothing(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        start = await cursor(h)
        await write(h, "paused", run_id, PAUSED)

        async def lost(*args: Any, **kwargs: Any) -> None:
            raise psycopg.OperationalError("server closed the connection unexpectedly")  # no SQLSTATE

        monkeypatch.setattr(repo, "get_run", lost)
        with pytest.raises(psycopg.OperationalError):
            await notifier.feed()
        assert (await cursor(h), await deliveries(h), await audits(h, "notify.skipped")) == (start, [], [])


async def test_a_cancel_inside_a_digest_savepoint_ends_the_notifier(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = stall_in_digest(monkeypatch)
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness,
            NOWHERE,
            {"ops-hook": hook(base, ref)},
            [rule("gaps", ["cycle.ended"], ["ops-hook"])],
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        await ended(h, run_id, 1)
        await write(h, "run.cycle", run_id, {"cycle": 2})
        running = asyncio.create_task(notifier.run())
        await until(lambda: held)
        running.cancel()  # psycopg ends the row's savepoint with OutOfOrderTransactionNesting instead
        await asyncio.wait([running], timeout=5)
        assert running.cancelled()  # never logged as a failed feed and gone past


async def test_a_stale_backlog_is_skipped_in_one_step(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness,
            NOWHERE,
            {"ops-hook": hook(base, ref)},
            [rule("health", ["run.paused"], ["ops-hook"])],
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        async with h.ctx.pool.connection() as conn:  # an outage: 100k rows older than max_age_hours
            await conn.execute(
                """INSERT INTO ds.audit_log (at, actor, action, object, detail)
                   SELECT now() - interval '30 hours', 'system:worker', 'paused', %s, %s
                   FROM generate_series(1, 100000)""",
                (run_id, json.dumps(PAUSED)),
            )
            last_stale = await repo.chain_head(conn)
        fresh = await write(h, "paused", run_id, PAUSED)
        await notifier.feed()
        ((skip,),) = [[r["detail"] for r in await audits(h, "notify.skipped")]]
        assert skip == {"reason": "stale", "from_seq": last_stale - 99_999, "to_seq": last_stale}
        assert [d["seq"] for d in await deliveries(h)] == [fresh]


async def test_pause_resume_pause_gives_two_deliveries(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        first = await write(h, "paused", run_id, PAUSED)
        await write(h, "running", run_id, {"reason": None, "approval_id": None})
        second = await write(h, "paused", run_id, PAUSED)
        await write(h, "paused", run_id, {"reason": None})  # a person's own: never
        await notifier.feed()
        assert [d["seq"] for d in await deliveries(h)] == [first, second]


# docs/mvp/ALERTS.md §8's example rule, as installations wrote it before the schema guard existed.
RUN_HEALTH = rule("run-health", ["run.paused", "run.stopped", "run.failed", "notify.dead"], ["ops-hook"])


async def test_guard_pause_reaches_a_run_paused_rule(open_harness: OpenHarness, tmp_path: Path) -> None:
    """docs/mvp/PRECHECKS.md §9: a rule that routes ``run.paused`` carries the schema guard's pause
    unchanged, and its ``schema.drift`` row sends nothing to a rule that does not name the event."""
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [RUN_HEALTH]) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        paused = await write(h, "paused", run_id, GUARD_PAUSE)  # the guard's trip: paused, then schema.drift
        await write(h, "schema.drift", run_id, DRIFT | {"paused_seq": paused})
        await notifier.feed()
        (delivery,) = await deliveries(h)
        async with h.ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE ds.runs SET status = 'paused', stop_reason = %s WHERE run_id = %s",
                (GUARD_PAUSE["reason"], run_id),
            )
            counts = await repo.daily_counts(conn, ["paused"])
    body = json.loads(delivery["body"])
    assert (delivery["event"], delivery["seq"], body["severity"]) == ("run.paused", paused, "alert")
    assert body["facts"]["reason"] == "run_paused_schema_drift" and body["rules"] == ["run-health"]
    assert body["text"].startswith(
        f"[test] unit-runner: run {run_id} paused: the schema guard found that a table it reads changed."
    )
    assert counts["paused"] == {"run_paused_schema_drift": 1}  # the daily summary names the reason


@pytest.mark.parametrize(("hours", "second"), [(6, ("suppressed", "duplicate")), (0, ("pending", None))])
async def test_a_channel_routed_both_of_a_guard_pause_s_events_sends_one(
    open_harness: OpenHarness, tmp_path: Path, hours: int, second: tuple[str, str | None]
) -> None:
    """The pause and its ``schema.drift`` share a dedup key: the ``run.paused``, written first, is sent
    and the other held back as its duplicate; with ``dedup_hours: 0`` both are sent."""
    rules = [RUN_HEALTH, rule("tables", ["schema.drift"], ["ops-hook"])]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, rules, dedup_hours=hours) as (
            h,
            notifier,
        ),
    ):
        run_id = await queued(h)
        await notifier.feed()
        paused = await write(h, "paused", run_id, GUARD_PAUSE)
        await write(h, "schema.drift", run_id, DRIFT | {"paused_seq": paused})
        warned = DRIFT | {"cycle": 8, "paused": False, "paused_seq": None}
        warned["sides"] = {"a": DRIFT["sides"]["a"] | {"other": 1}}
        await write(h, "schema.drift", run_id, warned)  # another state: a warning of its own
        await notifier.feed()
        found = [(d["event"], d["state"], d["why"]) for d in await deliveries(h)]
    assert found == [
        ("run.paused", "pending", None),
        ("schema.drift", *second),
        ("schema.drift", "pending", None),
    ]


async def test_a_guard_pause_resume_pause_of_one_state_gives_two_deliveries(
    open_harness: OpenHarness,
    databases: Callable[[str], str],  # noqa: F811
    tmp_path: Path,
) -> None:
    """Each pause is its own event (docs/mvp/ALERTS.md §2.1): the schema guard pauses a soak for a dropped
    column, a person restores it and resumes the run, and the same drop pauses it again. Only the pause and
    the ``schema.drift`` of one trip share a key, so the second pause is told within ``dedup_hours`` too."""
    seed = "CREATE TABLE customers (id int PRIMARY KEY, amount numeric(12,2));"
    seed += " INSERT INTO customers VALUES (1, 1)"
    dsn: dict[str, str] = {}
    targets: dict[str, Any] = {}
    for side in "ab":
        dsn[side] = databases(seed)
        (tmp_path / f"dsn_{side}").write_text(dsn[side])
        targets[f"db-{side}"] = {"dsn_ref": f"file:{tmp_path / f'dsn_{side}'}", "rate_per_s": 1000}
    rules = [RUN_HEALTH, rule("tables", ["schema.drift"], ["ops-hook"])]
    async with receiving(tmp_path) as (_, base, ref):
        notify = {"label": "test", "channels": {"ops-hook": hook(base, ref)}, "rules": rules}
        async with open_harness(json.dumps({"targets": targets}), notify=notify, lease_seconds=1) as h:
            notifier = Notifier(h.ctx)
            try:
                execution = {"concurrency": 2, "cycleInterval": "0s", "stop": {"maxCycles": 9}}
                spec_id = await h.spec(table_spec("customers", execution=execution))
                run_id = await create_run(h.ctx, spec_id, "soak", who=TESTER)
                await notifier.feed()  # the cursor starts at the head
                for statement in ("DROP COLUMN amount", "ADD COLUMN amount numeric(12,2)") * 2:
                    with psycopg.connect(dsn["b"], autocommit=True) as conn:
                        conn.execute(f"ALTER TABLE customers {statement}")  # type: ignore[arg-type]
                    if statement.startswith("DROP"):
                        with pytest.raises(drift.Tripped):
                            await scanning.scan(h.ctx, run_id)  # V2 pauses the run
                    else:
                        assert await resume_run(h.ctx, run_id, who=TESTER) == "running"  # V4
                await notifier.feed()
                found = [(d["event"], d["seq"], d["state"], d["why"]) for d in await deliveries(h)]
            finally:
                await notifier.close()
    pauses = [seq for event, seq, _, _ in found if event == "run.paused"]
    assert len(pauses) == 2 and [(event, state, why) for event, _, state, why in found] == [
        ("run.paused", "pending", None), ("schema.drift", "suppressed", "duplicate"),
        ("run.paused", "pending", None), ("schema.drift", "suppressed", "duplicate"),
    ]  # fmt: skip


async def test_a_labelling_labelled_is_notified_as_its_run_s_event(
    open_harness: OpenHarness, tmp_path: Path
) -> None:
    """``accuracy.label`` names its labelling, and its run in its detail (docs/mvp/ACCURACY.md §10): the
    feed reads the run from there, and sends ids and counts only."""
    labelling_id, other = "V-" + "0" * 25 + "7", "V-" + "0" * 25 + "8"
    rules = [rule("labelled", ["accuracy.measured"], ["ops-hook"], specs=["unit-*"])]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, rules) as (h, notifier),
    ):
        run_id = await queued(h)
        await ended(h, run_id, 1)
        await notifier.feed()
        hashes = {name: "a" * 64 for name in ("sheet_sha256", "labels_sha256", "score_sha256")}
        detail = {"run_id": run_id, "cycle": 1} | hashes
        counts = {"labelled": 150, "undecidable": 4, "not_reviewed": 1}
        seq = await write(h, "accuracy.label", labelling_id, detail | counts)
        await write(h, "accuracy.draw", other, {"run_id": run_id, "cycle": 1})  # a draw is not fed
        unsafe = await write(h, "accuracy.label", other, detail | counts | {"undecidable": "K001"})
        await notifier.feed()
        (found,) = await deliveries(h)
        assert (found["seq"], found["event"], found["severity"], found["run_id"], found["dedup_key"]) == (
            seq, "accuracy.measured", "info", run_id, f"accuracy.measured|{labelling_id}",
        )  # fmt: skip
        body = json.loads(found["body"])
        assert body["facts"] == {"cycle": 1, "labelling_id": labelling_id} | counts
        assert (body["run"]["run_id"], body["run"]["spec_name"]) == (run_id, "unit-runner")
        assert body["link"] is None and body["text"].startswith(
            f"[test] unit-runner: labelling {labelling_id} of cycle 1 of run {run_id} was labelled: 150 items"
            " labelled, 4 undecidable, 1 of them not reviewed (observed). Observed by DeltaSleuth"
        )
        assert "a" * 64 not in found["body"]  # its hashes stay in the audit row
        skipped = [r["detail"] for r in await audits(h, "notify.skipped")]
        assert skipped == [{"reason": "unsafe", "rows": 1, "seqs": [unsafe]}]


async def test_one_delivery_for_several_rules(open_harness: OpenHarness, tmp_path: Path) -> None:
    rules = [
        rule("health", ["run.paused"], ["ops-hook"]),
        rule("pauses", ["run.paused"], ["ops-hook", "other"]),
        rule("auth", ["run.paused"], ["other"], reasons=[KEY_PAUSE]),
    ]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref), "other": hook(base, ref)}, rules) as (
            h,
            notifier,
        ),
    ):
        run_id = await queued(h)
        await notifier.feed()
        await write(h, "paused", run_id, PAUSED)
        await notifier.feed()
        found = {d["channel"]: d["rules"] for d in await deliveries(h)}
        assert found == {"ops-hook": ["health", "pauses"], "other": ["pauses"]}
        assert json.loads((await deliveries(h))[0]["body"])["rules"] == ["health", "pauses"]


async def test_alerts_bypass_the_rate_limit_only(open_harness: OpenHarness, tmp_path: Path) -> None:
    rules = [rule("health", HEALTH, ["ops-hook"])]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, rules, rate_per_hour=2) as (
            h,
            notifier,
        ),
    ):
        run_id = await queued(h)
        await notifier.feed()
        for reason in (
            approvals.APPROVAL_PAUSE,
            approvals.APPROVAL_PAUSE,
            approvals.APPROVAL_PAUSE,
            AUTH_PAUSE,
        ):
            await write(
                h, "paused", run_id, {"reason": reason, "approval_id": None}
            )  # warnings, then an alert
        await notifier.feed()
        found = [(d["severity"], d["state"], d["why"]) for d in await deliveries(h)]
        assert found == [
            ("warning", "pending", None),
            ("warning", "pending", None),
            ("warning", "suppressed", "rate_limited"),
            ("alert", "pending", None),
        ]
        alert = json.loads((await deliveries(h))[3]["body"])
        assert alert["suppressed_since_last"] == 1
        assert alert["text"].endswith(
            "(1 notification to this channel was held back since the last one: see Notifications)"
        )
        async with h.ctx.pool.connection() as conn:  # a mute holds alerts too; so does a duplicate
            await conn.execute(
                """INSERT INTO ds.notify_mutes (mute_id, scope, target, until, reason, created_by)
                   VALUES ('M-' || repeat('0', 26), 'run', %s, now() + interval '1 hour', 'maintenance',
                           'user:U-0123456789abcdef')""",
                (run_id,),
            )
        await write(h, "paused", run_id, PAUSED)
        await notifier.feed()
        assert (await deliveries(h))[-1]["why"] == "muted"


async def test_a_test_send_resets_no_held_back_count(open_harness: OpenHarness, tmp_path: Path) -> None:
    rules = [rule("health", ["run.paused"], ["ops-hook"])]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, rules, rate_per_hour=1) as (
            h,
            notifier,
        ),
    ):
        run_id = await queued(h)
        await notifier.feed()
        for _ in range(3):  # warnings: the last two are held back
            await write(h, "paused", run_id, {"reason": approvals.APPROVAL_PAUSE, "approval_id": None})
        await notifier.feed()
        assert (await notifications.test(h.ctx, "ops-hook", TESTER))["state"] == "sent"  # outside the queue
        await write(h, "paused", run_id, PAUSED)  # an alert: never rate limited
        await notifier.feed()
        alert = json.loads((await deliveries(h))[-1]["body"])
    assert (alert["severity"], alert["suppressed_since_last"]) == ("alert", 2)


async def test_a_duplicate_is_held_back(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        for _ in range(2):  # a stop condition met twice (two processes): one notification
            await write(h, "cancelling", run_id, {"reason": "stop_condition:maxErrorRate"})
        await notifier.feed()
        assert [(d["state"], d["why"]) for d in await deliveries(h)] == [
            ("pending", None),
            ("suppressed", "duplicate"),
        ]


async def test_a_renewed_fall_is_sent_once_its_recovery_was(
    open_harness: OpenHarness, tmp_path: Path
) -> None:
    """A pair's readiness delivery is a duplicate only of the newest one the channel holds of the pair: a
    fall that follows a delivered recovery is sent, though its key was sent within ``dedup_hours``; the same
    fall recorded twice in a row is one notification."""
    gates = CHANGED["gates"] | {"targets": "not_met", "convergence": "not_met"}
    fell = CHANGED | {
        "from": "parity_ready", "to": "not_ready", "score": {"passed": 3, "required": 5}, "gates": gates,
        "changed": [{"gate": gate, "from": "met", "to": "not_met"} for gate in ("targets", "convergence")],
    }  # fmt: skip
    recovered = fell | {
        "from": "not_ready", "score": {"passed": 4, "required": 5}, "gates": gates | {"targets": "met"},
        "changed": [{"gate": "targets", "from": "not_met", "to": "met"}],
    }  # fmt: skip
    again = fell | {"from": "not_ready", "changed": [{"gate": "targets", "from": "met", "to": "not_met"}]}
    ready = rule("cutover", ["readiness.changed"], ["ops-hook"], causes=["cycle"])
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [ready]) as (h, notifier),
    ):
        await notifier.feed()
        for detail in (fell, recovered, again, again):  # cycle 4 fails, cycle 5 is clean, cycle 6 fails
            await write(h, "readiness.changed", f"readiness:{PAIR}", detail)
        other = fell | {"pair": "customer-links"}  # another pair's delivery is none of this pair's
        await write(h, "readiness.changed", "readiness:customer-links", other)
        await write(h, "readiness.changed", f"readiness:{PAIR}", again)
        await notifier.feed()
        sent = [
            (d["dedup_key"].split("|", 1)[1], d["severity"], d["state"], d["why"])
            for d in await deliveries(h)
        ]
    assert sent == [
        (f"{PAIR}|not_ready|300", "alert", "pending", None),
        (f"{PAIR}|not_ready|100", "info", "pending", None),  # still not ready: targets met
        (f"{PAIR}|not_ready|300", "alert", "pending", None),  # the last word is the fall, not the recovery
        (f"{PAIR}|not_ready|300", "alert", "suppressed", "duplicate"),
        ("customer-links|not_ready|300", "alert", "pending", None),
        (f"{PAIR}|not_ready|300", "alert", "suppressed", "duplicate"),
    ]


# -- the cycle's digest (§2.2) ------------------------------------------------------------------------------


async def _results(h: Any, run_id: str, cycle: int, outcomes: dict[str, str], causes: list[str] = ()) -> None:
    """Store a cycle's results (masked key -> outcome) and root causes as the worker would have."""
    async with h.ctx.pool.connection() as conn:
        sealer = await h.ctx.sealer(conn, run_id)
        await repo._insert_keys(conn, run_id, cycle, sealer.seal_keys({k: k for k in outcomes}), None, 10)
        for masked, outcome in outcomes.items():
            await conn.execute(
                """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
                       confirmation, first_outcome)
                   VALUES (%s, %s, %s, %s, 'COMPLETE', 'COMPLETE', 'rechecked', %s)""",
                (run_id, cycle, masked, outcome, outcome),
            )
        for seq, cause in enumerate(causes):
            await conn.execute(
                """INSERT INTO ds.root_causes (run_id, cycle, root_cause_id, seq, kind, max_severity,
                       path_pattern,
                       change_type, direction, affected_keys, finding_count)
                   VALUES (%s, %s, %s, %s, 'signature', 'HIGH', '$.status', 'value_changed', 'both', 1, 1)""",
                (run_id, cycle, cause, seq),
            )
    counters = EMPTY | {
        "total": len(outcomes),
        "different": 1,
        "compared": len(outcomes),
        "mismatch_rate": "0.5",
    }
    await ended(h, run_id, cycle, counters)


async def _digest(notifier: Any, h: Any, run_id: str, cycle: int) -> dict[str, Any]:
    async with h.ctx.pool.connection() as conn:
        block = {"run_id": run_id}
        return await notifier._digest(conn, Event("cycle.ended", {"cycle": cycle}, block))


RC = ["RC-" + c * 12 for c in "abcd"]


@pytest.mark.parametrize(
    ("source", "cycles", "expected"),
    [
        # key list: K2 newly discrepant, K1 no longer; K3 read in 1 only (a rolling listing) is not counted
        (
            "list",
            [{"K1": "MISMATCH", "K2": "MATCH", "K3": "MISMATCH"}, {"K1": "MATCH", "K2": "NOT_FOUND_B"}],
            (1, 1),
        ),
        # rolling: keys entering (K4) or leaving (K3) the listing are not counted
        ("list", [{"K2": "MATCH", "K3": "MISMATCH"}, {"K2": "MATCH", "K4": "MISMATCH"}], (0, 0)),
        # a table scan read every key: one without a result was read identical
        ("table_scan", [{"K1": "MISMATCH"}, {"K2": "MISMATCH"}], (1, 1)),
        # a hash sample's cycles are fresh samples: key changes are not counted between them
        ("hash_sample", [{"K1": "MISMATCH", "K2": "MATCH"}, {"K1": "MATCH", "K2": "MISMATCH"}], (None, None)),
    ],
)
async def test_the_digest(
    open_harness: OpenHarness, tmp_path: Path, source: str, cycles: list[dict[str, str]], expected: tuple
) -> None:
    await _digests(open_harness, tmp_path, source, cycles, expected)


async def _digests(
    open_harness: OpenHarness,
    tmp_path: Path,
    source: str,
    cycles: list[dict[str, str]],
    expected: tuple,
    scans: tuple[str, str] = ("done", "done"),
) -> None:
    """Cycles 1 and 2 of a run of ``source`` and their digests; a table scan's ``scans``: each cycle's scan
    ``done``, ``stopped`` before the end, or ``cut`` by maxKeys."""
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness,
            NOWHERE,
            {"ops-hook": hook(base, ref)},
            [rule("gaps", ["cycle.ended"], ["ops-hook"])],
        ) as (h, notifier),
    ):
        run_id = await queued(h, keys_source=source)
        await _results(h, run_id, 1, cycles[0], RC[:2])
        await _results(h, run_id, 2, cycles[1], RC[1:3])
        if source == "table_scan":
            async with h.ctx.pool.connection() as conn:
                for cycle, scan in enumerate(scans, 1):
                    await conn.execute(
                        "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, %s, %s)",
                        (run_id, cycle, json.dumps({"done": scan != "stopped"})),
                    )
                    cut = json.dumps({"taken": 1}) if scan == "cut" else None
                    await conn.execute(
                        "UPDATE ds.cycles SET keys_cut = %s WHERE run_id = %s AND cycle = %s",
                        (cut, run_id, cycle),
                    )
        first, second = await _digest(notifier, h, run_id, 1), await _digest(notifier, h, run_id, 2)
    discrepant = sum(o != "MATCH" for o in cycles[0].values())
    assert (first["new_root_causes"], first["gone_root_causes"], first["newly_discrepant"]) == (
        2,
        0,
        discrepant,
    )
    assert (second["new_root_causes"], second["gone_root_causes"], second["baseline_from"]) == (1, 1, 1)
    assert (second["newly_discrepant"], second["no_longer_discrepant"]) == expected
    assert second["between_samples"] is (source == "hash_sample") and second["unavailable"] == []
    assert second["previous"]["mismatch_rate"] == {"cycle": 1, "value": "0.5"}


@pytest.mark.parametrize(
    ("scans", "expected"),
    [
        (("done", "cut"), (1, 0)),  # maxKeys cut cycle 2 before K1: never counted as no longer discrepant
        (("done", "stopped"), (1, 0)),  # maxDuration stopped cycle 2's scan at a checkpoint
        (("stopped", "done"), (0, 1)),  # cycle 1 never read K2: not newly discrepant
    ],
)
async def test_a_cut_or_stopped_scan_counts_only_keys_read_in_both(
    open_harness: OpenHarness, tmp_path: Path, scans: tuple[str, str], expected: tuple
) -> None:
    cycles = [{"K1": "MISMATCH"}, {"K2": "MISMATCH"}]
    await _digests(open_harness, tmp_path, "table_scan", cycles, expected, scans)


async def test_a_purged_or_timed_out_fact_is_unavailable(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(
            open_harness,
            NOWHERE,
            {"ops-hook": hook(base, ref)},
            [rule("gaps", ["cycle.ended"], ["ops-hook"])],
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await _results(h, run_id, 1, {"K1": "MISMATCH"}, RC[:1])
        await _results(h, run_id, 2, {"K1": "MISMATCH"}, RC[:1])
        await _results(h, run_id, 3, {"K1": "MATCH"})
        async with h.ctx.pool.connection() as conn:
            await repo.start_purge(conn, run_id, 2, "retention")
        purged = await _digest(notifier, h, run_id, 3)  # cycle 2 purging: its changes are unknown
        assert (purged["gone_root_causes"], purged["newly_discrepant"], purged["baseline_from"]) == (
            None,
            None,
            1,
        )
        assert purged["unavailable"] == ["gone_root_causes", "newly_discrepant", "no_longer_discrepant"]

        async def canceled(*args: Any, **kwargs: Any) -> None:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

        monkeypatch.setattr(repo, "root_cause_changes", canceled)
        monkeypatch.setattr(repo, "unexplained_criticals", canceled)
        await notifier.feed()
        await write(h, "run.finished", run_id, {"status": "completed", "reason": None})
        await notifier.feed()
        (found,) = await deliveries(h)
        facts = json.loads(found["body"])["facts"]
        assert facts["unavailable"] == ["new_root_causes", "gone_root_causes", "unexplained_critical"]
        assert facts["new_root_causes"] is None and facts["levels"]["unexplained_critical"] is None
        assert "root causes (not counted) and 1 discrepant key observed" in json.loads(found["body"])["text"]
    assert receiver.received == []


async def test_cycle_counts_equal_the_tombstone(
    open_harness: OpenHarness, tmp_path: Path, loopback: Loopback
) -> None:
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(
            open_harness,
            NOWHERE,
            {"ops-hook": hook(base, ref)},
            [rule("gaps", ["cycle.ended"], ["ops-hook"])],
        ) as (h, notifier),
    ):
        run_id = await queued(h)
        await notifier.feed()
        await _results(h, run_id, 1, {"K1": "MISMATCH", "K2": "MATCH"}, RC[:1])
        await write(h, "run.cycle", run_id, {"cycle": 2})
        await notifier.feed()
        (found,) = await deliveries(h)
        async with h.ctx.pool.connection() as conn:
            tombstone = (await repo.get_cycle(conn, run_id, 1))["counters"]  # type: ignore[index]
        body = json.loads(found["body"])
        assert body["facts"]["counters"] == {k: tombstone[k] for k in COUNTERS}
        assert body["facts"]["cycle"] == 1 and body["run"]["now"]["cycle"] == 1
        assert found["body_sha256"] == hashlib.sha256(found["body"].encode()).hexdigest()
