"""Delivery: each channel's heads in order under one id and the stored bytes, a crash
after a 2xx sending them again under the same id, backoff with Retry-After a floor, a poisoned head dying
``retries_exhausted`` so the next arrives, expiry after an outage, dead letters audited hourly without a
loop, one channel's failure never holding another, mutes, housekeeping, and no secret anywhere."""

from __future__ import annotations

import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Any

import pytest

from deltasleuth import notifications
from deltasleuth.connectors.retry import retry_delay
from deltasleuth.core.types import RetryPolicy
from deltasleuth.runner import notify
from deltasleuth.runner.runs import AUTH_PAUSE
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import OpenHarness
from .notifying import HEALTH, audits, deliveries, hook, notifying, queued, receiving, rule, write

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("loopback")]
NOWHERE = "http://127.0.0.1:9"
PAUSED = {"reason": AUTH_PAUSE}


async def _drain(notifier: notify.Notifier) -> None:
    """Scan once and wait for the send tasks it started."""
    await notifier.scan()
    for task in list(notifier.sending.values()):
        await task


async def _paused(h: Any, notifier: notify.Notifier, n: int = 1) -> str:
    run_id = await queued(h)
    await notifier.feed()
    for _ in range(n):
        await write(h, "paused", run_id, PAUSED)
    await notifier.feed()
    return run_id


async def _backdate(h: Any, sql: str) -> None:
    async with h.ctx.pool.connection() as conn:
        await conn.execute(sql)  # type: ignore[arg-type]


async def test_heads_are_sent_in_order_under_one_id(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        await _paused(h, notifier, 3)
        await _drain(notifier)
        stored = await deliveries(h)
    assert [d["state"] for d in stored] == ["sent"] * 3 and [d["attempts"] for d in stored] == [1, 1, 1]
    assert [r.headers["webhook-id"] for r in receiver.received] == [d["delivery_id"] for d in stored]
    assert all(r.verified for r in receiver.received)
    for got, row in zip(receiver.received, stored, strict=True):
        assert got.body.decode() == row["body"] and hashlib.sha256(got.body).hexdigest() == row["body_sha256"]
        assert json.loads(got.body)["id"] == row["delivery_id"]


async def test_a_crash_after_a_2xx_sends_the_same_bytes_again(
    open_harness: OpenHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        await _paused(h, notifier)
        record = notifier._record

        async def killed(*args: Any) -> None:
            raise RuntimeError("kill -9 between the 2xx and its record")

        monkeypatch.setattr(notifier, "_record", killed)
        await _drain(notifier)
        (held,) = await deliveries(h)
        assert (held["state"], held["attempts"]) == (
            "pending",
            1,
        )  # leased: due again once the send must have ended
        await _drain(notifier)
        assert len(receiver.received) == 1  # the lease holds
        await _backdate(h, "UPDATE ds.notify_deliveries SET due_at = now()")
        monkeypatch.setattr(notifier, "_record", record)
        await _drain(notifier)
        (sent,) = await deliveries(h)
    first, again = receiver.received
    assert first.body == again.body and first.headers["webhook-id"] == again.headers["webhook-id"]
    assert (sent["state"], sent["attempts"]) == ("sent", 2)


async def test_a_retry_backs_off_holding_its_channel(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        await _paused(h, notifier, 2)
        receiver.failures.append((503, "900"))
        await _drain(notifier)
        head, second = await deliveries(h)
        async with h.ctx.pool.connection() as conn:
            [(wait,)] = await (
                await conn.execute(
                    """SELECT extract(epoch FROM due_at - now()) FROM ds.notify_deliveries
                       WHERE delivery_id = %s""",
                    (head["delivery_id"],),
                )
            ).fetchall()
    assert (head["state"], head["last_error"], head["last_status"], head["attempts"]) == (
        "pending",
        "http_5xx",
        503,
        1,
    )
    expected = retry_delay(1, notify.POLICY, random.Random(f"{head['delivery_id']}:1"), 900.0)  # noqa: S311
    assert expected == 900.0 and abs(float(wait) - expected) < 5  # Retry-After a floor, never capped
    assert (second["state"], second["attempts"]) == ("pending", 0) and len(
        receiver.received
    ) == 1  # order holds


@pytest.mark.parametrize("status", [500, 999])  # 999: not HTTP's, its status never stored
async def test_a_poisoned_head_dies_and_the_next_alert_arrives(
    open_harness: OpenHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    monkeypatch.setattr(notify, "POLICY", RetryPolicy(initial_s=0.0, multiplier=1, max_s=0.0))
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(
            open_harness,
            NOWHERE,
            {"ops-hook": hook(base, ref)},
            [rule("health", HEALTH, ["ops-hook"])],
            max_attempts=3,
        ) as (h, notifier),
    ):
        await _paused(h, notifier, 2)
        receiver.failures += [(status, None)] * 3
        await _drain(notifier)
        dead, sent = await deliveries(h)
        (audited,) = await audits(h, "notify.dead")
    stored = 500 if status == 500 else None
    assert (dead["state"], dead["last_error"], dead["last_status"], dead["attempts"]) == (
        "dead",
        "retries_exhausted",
        stored,
        3,
    )
    assert sent["state"] == "sent" and len(receiver.received) == 4
    assert audited["object"] == "notify:ops-hook" and audited["actor"] == "system:worker"
    assert audited["detail"] == {
        "delivery_id": dead["delivery_id"],
        "event": "run.paused",
        "attempts": 3,
        "error": "retries_exhausted",
        "status": stored,
        "body_sha256": dead["body_sha256"],
        "dead_since_last": 0,
    }


async def test_dead_letters_are_audited_hourly_without_a_loop(
    open_harness: OpenHarness, tmp_path: Path
) -> None:
    rules = [rule("health", HEALTH, ["ops-hook"]), rule("dead", ["notify.dead"], ["ops-hook", "backup"])]
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref), "backup": hook(base, ref)}, rules) as (
            h,
            notifier,
        ),
    ):
        await _paused(h, notifier, 2)
        receiver.failures += [(404, None)] * 2  # dead at once
        await _drain(notifier)
        assert [r["detail"]["dead_since_last"] for r in await audits(h, "notify.dead")] == [0]  # hourly
        await notifier.feed()  # the notify.dead goes to backup only, never to the channel it names
        (routed,) = [d for d in await deliveries(h) if d["event"] == "notify.dead"]
        assert routed["channel"] == "backup"
        receiver.failures.append((404, None))
        await _drain(notifier)  # the notify.dead delivery dies too: only logged
        assert len(await audits(h, "notify.dead")) == 1
        async with h.ctx.pool.connection() as conn:  # an hour on: the next death is audited, counting since
            await conn.execute(
                """INSERT INTO ds.audit_log (at, actor, action, object, detail)
                   VALUES (now() - interval '2 hours', 'system:worker', 'notify.dead', 'notify:ops-hook',
                           '{}')"""
            )
        await write(h, "paused", (await deliveries(h))[0]["run_id"], PAUSED)
        await notifier.feed()
        receiver.failures.append((404, None))
        await _drain(notifier)
        last = (await audits(h, "notify.dead"))[-1]
    assert last["detail"]["dead_since_last"] == 2 and last["detail"]["error"] == "http_4xx"


async def test_one_channel_s_failure_never_holds_another(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path, "a") as (failing, base_a, ref_a),
        receiving(tmp_path, "b") as (working, base_b, ref_b),
        notifying(
            open_harness,
            NOWHERE,
            {"a": hook(base_a, ref_a), "b": hook(base_b, ref_b)},
            [rule("all", HEALTH, ["a", "b"])],
        ) as (h, notifier),
    ):
        failing.failures += [(503, None)] * 5
        await _paused(h, notifier, 2)
        await _drain(notifier)
        states = {(d["channel"], d["attempts"], d["state"]) for d in await deliveries(h)}
    assert states == {("a", 1, "pending"), ("a", 0, "pending"), ("b", 1, "sent")}
    assert len(working.received) == 2 and len(failing.received) == 1


async def test_after_a_30h_outage(open_harness: OpenHarness, tmp_path: Path) -> None:
    """Heads older than max_age_hours die expired; audit rows as old are skipped; new ones arrive."""
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        run_id = await _paused(h, notifier, 2)
        await _backdate(
            h,
            """UPDATE ds.notify_deliveries
               SET queued_at = now() - interval '30 hours', due_at = now() + interval '1 hour'""",
        )
        await _drain(notifier)
        assert [(d["state"], d["last_error"]) for d in await deliveries(h)] == [("dead", "expired")] * 2
        assert [r["detail"]["error"] for r in await audits(h, "notify.dead")] == ["expired"]
        seq = await write(h, "paused", run_id, PAUSED)
        await notifier.feed()
        await _drain(notifier)
        assert (await deliveries(h))[-1]["seq"] == seq and len(receiver.received) == 1


async def test_an_undeclared_channel_and_a_mute(open_harness: OpenHarness, tmp_path: Path) -> None:
    async with (
        receiving(tmp_path) as (receiver, base, ref),
        notifying(
            open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, [rule("health", HEALTH, ["ops-hook"])]
        ) as (h, notifier),
    ):
        await _paused(h, notifier, 2)
        heads = await deliveries(h)
        await _backdate(
            h,
            f"UPDATE ds.notify_deliveries SET channel = 'gone' WHERE delivery_id = '{heads[0]['delivery_id']}'",  # noqa: S608, E501
        )
        await _backdate(
            h,
            """INSERT INTO ds.notify_mutes (mute_id, scope, target, until, reason, created_by)
               VALUES ('M-' || repeat('1', 26), 'channel', 'ops-hook', now() + interval '1 hour',
                       'maintenance',
                       'user:U-0123456789abcdef')""",
        )
        await _drain(notifier)
        found = {
            d["channel"]: (d["state"], d["why"], d["last_error"], d["mute_id"]) for d in await deliveries(h)
        }
    assert found == {
        "gone": ("dead", None, "channel_removed", None),
        "ops-hook": ("suppressed", "muted", None, "M-" + "1" * 26),
    }
    assert receiver.received == []


async def test_a_body_rendered_for_other_channel_settings_is_never_sent(
    open_harness: OpenHarness, tmp_path: Path
) -> None:
    """After a restart that turns a channel's labels off, or makes it Slack, a head rendered before dies
    ``channel_changed`` and a retry leaves it dead; with the channel as it was, a retry sends it."""
    rules = [rule("health", HEALTH, ["ops"])]
    async with receiving(tmp_path) as (receiver, base, ref):
        (tmp_path / "slack.url").write_text(f"{base}/slack\n")
        slack = {"kind": "slack", "url_ref": f"file:{tmp_path / 'slack.url'}", "host": "127.0.0.1",
                 "allow_private_network": True}  # fmt: skip
        async with notifying(open_harness, NOWHERE, {"ops": hook(base, ref)}, rules) as (h, notifier):
            await _paused(h, notifier)  # pending, rendered for a webhook with labels and links
        retried, found = [], []
        for ops in (hook(base, ref, labels=False), slack, hook(base, ref)):  # a restart each
            async with notifying(open_harness, NOWHERE, {"ops": ops}, rules) as (h, notifier):
                await _drain(notifier)
                retried.append((await notifications.retry(h.ctx, "ops", None, TESTER))[0])
                await _drain(notifier)
                found.append([(d["state"], d["last_error"]) for d in await deliveries(h)])
    assert retried == [0, 0, 1]
    assert found == [[("dead", "channel_changed")]] * 2 + [[("sent", None)]]
    assert [r.path for r in receiver.received] == ["/webhook"] and receiver.received[0].verified


async def test_housekeeping(open_harness: OpenHarness, tmp_path: Path) -> None:
    rules = [rule("health", HEALTH, ["ops-hook"]), rule("daily", ["notify.daily"], ["ops-hook"])]
    async with (
        receiving(tmp_path) as (_, base, ref),
        notifying(open_harness, NOWHERE, {"ops-hook": hook(base, ref)}, rules, daily_hour=0) as (h, notifier),
    ):
        await _paused(h, notifier)
        await _backdate(h, "UPDATE ds.notify_deliveries SET created_at = now() - interval '91 days'")
        await notifier.housekeep()
        await notifier.housekeep()  # hourly: nothing more
        notifier.housekept_at = -float("inf")
        await notifier.housekeep()  # the day's summary once
        (daily,) = await deliveries(h)
    body = json.loads(daily["body"])
    assert (daily["event"], daily["event_id"][:6], daily["seq"]) == ("notify.daily", "daily:", None)
    assert body["facts"]["channels"]["ops-hook"]["pending"] == 1  # counted before the prune deleted it
    assert body["facts"]["runs"] == {"queued": 1} and body["facts"]["feed_lag_s"] == 0
    assert body["audit"] is None and "Daily summary" in body["text"]


async def test_no_secret_reaches_a_delivery_an_audit_row_or_a_log(
    open_harness: OpenHarness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canary = "c4n4ry-s3cr3t"
    async with receiving(tmp_path) as (receiver, base, ref):
        (tmp_path / "teams.url").write_text(f"{base}/teams?sig={canary}\n")
        (tmp_path / "evil.url").write_text(f"https://evil.example/{canary}\n")
        channels = {
            "ops-hook": hook(base, ref),
            "owners-teams": {
                "kind": "teams",
                "url_ref": f"file:{tmp_path / 'teams.url'}",
                "host": "127.0.0.1",
                "allow_private_network": True,
            },
            "mismatch": {
                "kind": "slack",
                "url_ref": f"file:{tmp_path / 'evil.url'}",
                "host": "127.0.0.1",
                "allow_private_network": True,
            },
        }
        with caplog.at_level(logging.DEBUG):
            async with notifying(
                open_harness, NOWHERE, channels, [rule("health", HEALTH, list(channels))]
            ) as (h, notifier):
                await _paused(h, notifier)
                await _drain(notifier)
                await notifier.feed()
                await _drain(notifier)
                stored = json.dumps([dict(d) for d in await deliveries(h)], default=str)
                async with h.ctx.pool.connection() as conn:
                    audit, _ = await repo.list_audit(conn, limit=1000)
        secrets = [canary, (tmp_path / "hook.key").read_text().strip().removeprefix("whsec_"), str(tmp_path)]
        for text in (stored, json.dumps(audit, default=str), caplog.text):
            assert not [s for s in secrets if s in text]
        assert {d["channel"]: d["state"] for d in json.loads(stored) if d["event"] == "run.paused"} == {
            "ops-hook": "sent",
            "owners-teams": "sent",
            "mismatch": "dead",
        }
    assert [r.path for r in receiver.received][:2] == ["/webhook", "/teams"]
