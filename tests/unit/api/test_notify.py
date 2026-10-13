"""Alerts and notifications over the API (docs/mvp/ALERTS.md §10, §11): the channels' state and banners, the
deliveries, and the audited acts: a test send (one attempt, stored sent or dead, throttled), sending dead
deliveries again, and mutes (an admin signed in within 15 minutes). Every answer holds ids, counts and closed
codes, never a URL, path, address or secret; every POST is refused while notifications are off."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from deltasleuth.auth.principal import expand
from deltasleuth.notifications import OFF
from deltasleuth.security.rawvalues import KEY_SHAPED
from deltasleuth.store import repo
from tests.conftest import receiving_thread

from .conftest import OIDC, audited, said

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
HOOK = "/v1/notifications/channels/ops-hook"
MUTES = "/v1/notifications/mutes"
POSTS = [
    (f"{HOOK}/test", None),
    (f"{HOOK}/retry", {}),
    (MUTES, {"scope": "channel", "target": "ops-hook", "hours": 1, "reason": "planned"}),
    (f"{MUTES}/M-00000000000000000000000000/end", None),
]


def minute(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def settings(base: str, ref: str) -> dict[str, Any]:
    hook = {"kind": "webhook", "url": f"{base}/webhook", "signing_key_refs": [ref]}
    return {
        "label": "test",
        "channels": {"ops-hook": hook | {"allow_private_network": True}},
        "rules": [{"name": "run-health", "events": ["run.failed", "notify.dead"], "channels": ["ops-hook"]}],
    }


@pytest.fixture
def notify(tmp_path: Path) -> dict[str, Any]:
    """Settings of one webhook channel, ops-hook, which these tests never send to."""
    (tmp_path / "hook.key").write_text("whsec_" + base64.b64encode(os.urandom(32)).decode() + "\n")
    return settings("https://events.bank.example", f"file:{tmp_path / 'hook.key'}")


def sql(client: TestClient, query: str, *params: Any) -> list[dict[str, Any]]:
    async def go(ctx: Any) -> list[dict[str, Any]]:
        async with ctx.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query.encode(), params)
            return await cur.fetchall() if cur.description else []

    return client.portal.call(go, client.app.state.ctx)


def dead(client: TestClient, *, event_id: str, done: str = "now()") -> str:
    """A dead delivery of ops-hook that died at ``done`` (``test``: a test send's)."""
    delivery_id = repo.new_delivery_id()
    sql(
        client,
        f"""INSERT INTO ds.notify_deliveries (delivery_id, event_id, event, severity, channel, dedup_key,
                body, body_sha256, render, state, attempts, last_error, last_status, done_at)
            VALUES (%s, %s, 'run.failed', 'alert', 'ops-hook', 'run.end|R-1', '{{}}', repeat('0', 64),
                    'webhook:1:1', 'dead', 8, 'http_5xx', 503, {done})""",  # noqa: S608 - a test's fixed SQL
        delivery_id,
        f"test:{delivery_id}" if event_id == "test" else event_id,
    )
    return delivery_id


def test_every_post_is_refused_while_notifications_are_off(open_client: OpenClient) -> None:
    malformed = [
        (MUTES, {"scope": "bogus"}),
        (MUTES, {**POSTS[2][1], "hours": "many"}),
        (f"{HOOK}/retry", {"since": 5}),
    ]
    with open_client() as client:
        status = client.get("/v1/notifications").json()
        refused = [said(client.post(path, json=body)) for path, body in POSTS + malformed]  # 409 first
    off = {
        "enabled": False,
        "label": None,
        "cursor": None,
        "channels": [],
        "rules": [],
        "mutes": [],
        "banners": [],
    }
    assert status == off and refused == [(409, OFF)] * (len(POSTS) + len(malformed))


def test_the_state_names_channels_by_kind_and_host_never_a_url_path_or_secret(
    open_client: OpenClient, tmp_path: Path, notify: dict[str, Any]
) -> None:
    host = "prod-00.eastus.logic.azure.com"
    (tmp_path / "teams.url").write_text(f"https://{host}/workflows/s3cr3t-sig\n")
    notify["channels"]["owners-teams"] = {
        "kind": "teams",
        "url_ref": f"file:{tmp_path / 'teams.url'}",
        "host": host,
    }
    with open_client(notify=notify) as client:
        body = client.get("/v1/notifications")
    found = body.json()
    assert found["enabled"] and found["label"] == "test" and found["banners"] == []
    assert found["cursor"]["seq"] is None and found["cursor"]["behind"] is None  # no pass yet
    counts = {"recipients": 0, "pending": 0, "sent_24h": 0, "dead_24h": 0, "suppressed_24h": 0,
              "last_error": None, "last_status": None}  # fmt: skip
    assert found["channels"] == [
        {"name": "ops-hook", "kind": "webhook", "host": "events.bank.example", "proxy": False,
         "allow_private_network": True, "labels": True, "links": True} | counts,
        {"name": "owners-teams", "kind": "teams", "host": host, "proxy": False,
         "allow_private_network": False, "labels": False, "links": False} | counts,
    ]  # fmt: skip
    assert [r["name"] for r in found["rules"]] == ["run-health"]
    secret = ("s3cr3t", "/workflows", "/webhook", str(tmp_path), "whsec_")
    assert [part for part in secret if part in body.text] == []


@pytest.mark.usefixtures("loopback")
def test_a_test_send_is_one_attempt_stored_sent_or_dead_audited_and_throttled(
    open_client: OpenClient, tmp_path: Path
) -> None:
    with receiving_thread(tmp_path) as (hook, base, ref), open_client(notify=settings(base, ref)) as client:
        sent = client.post(f"{HOOK}/test")
        again = client.post(f"{HOOK}/test")
        undeclared = client.post("/v1/notifications/channels/ops-slak/test")
        tests = client.app.state.notify_tests
        later = tests.clock() + 11
        tests.clock = lambda: later
        hook.failures.append((401, None))
        refused = client.post(f"{HOOK}/test")
        rows = sql(client, "SELECT * FROM ds.notify_deliveries ORDER BY delivery_id")
        rows_audited = audited(client, "notify.test")
    first, second = sent.json(), refused.json()
    assert (first["state"], first["status"], first["error"]) == ("sent", 200, None)
    assert sent.status_code == 200 and isinstance(first["latency_ms"], int)
    assert said(again) == (429, "a test of channel ops-hook was sent less than 10 s ago")
    assert said(undeclared) == (404, "channel ops-slak is not declared")
    assert (second["state"], second["status"], second["error"]) == ("dead", 401, "http_4xx")
    stored = [(r["delivery_id"], r["state"], r["attempts"], r["last_error"], r["last_status"]) for r in rows]
    assert stored == [
        (first["delivery_id"], "sent", 1, None, 200),
        (second["delivery_id"], "dead", 1, "http_4xx", 401),
    ]
    row, delivery_id = rows[0], first["delivery_id"]
    assert (row["event_id"], row["dedup_key"]) == (f"test:{delivery_id}", f"notify.test|{delivery_id}")
    assert row["done_at"] is not None and row["due_at"] is None and row["seq"] is None
    [delivered, _] = hook.received
    assert delivered.verified and delivered.headers["webhook-id"] == first["delivery_id"]
    doc = json.loads(delivered.body)
    assert (doc["event"], doc["facts"], doc["rules"]) == ("notify.test", {"channel": "ops-hook"}, [])
    assert doc["text"].startswith("[test] This is a test of channel ops-hook: the channel delivers.")
    fields = ("delivery_id", "state", "status", "error")
    assert [(actor[:4], obj, detail) for actor, obj, detail in rows_audited] == [
        ("dev:", "notify:ops-hook", {k: first[k] for k in fields}),
        ("dev:", "notify:ops-hook", {k: second[k] for k in fields}),
    ]


def test_retry_sends_dead_deliveries_again_from_since_tests_aside(
    open_client: OpenClient, notify: dict[str, Any]
) -> None:
    with open_client(notify=notify) as client:
        recent = dead(client, event_id="seq:7:run.failed")
        dead(client, event_id="test")  # a test is never sent again
        old = dead(client, event_id="seq:5:run.failed", done="now() - interval '4 days'")
        retried = client.post(f"{HOOK}/retry")
        since = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        older = client.post(f"{HOOK}/retry", json={"since": since})
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        refusals = [
            said(client.post(f"{HOOK}/retry", json={"since": value}))
            for value in ("2026-10-01T09:00:00", "yesterday", future)
        ]
        undeclared = client.post("/v1/notifications/channels/ops-slak/retry", json={})
        rows = {r["delivery_id"]: r for r in sql(client, "SELECT * FROM ds.notify_deliveries")}
        rows_audited = audited(client, "notify.retry")
    assert (retried.json(), older.json()) == ({"count": 1}, {"count": 1})
    assert refusals == [(422, "since must be an ISO-8601 time with a zone, not in the future")] * 3
    assert said(undeclared) == (404, "channel ops-slak is not declared")
    for delivery_id in (recent, old):
        row = rows[delivery_id]
        assert (row["state"], row["attempts"], row["done_at"]) == ("pending", 0, None)
        assert row["due_at"] is not None and row["queued_at"] > datetime.now(UTC) - timedelta(minutes=1)
    assert [d["count"] for _, _, d in rows_audited] == [1, 1] and rows_audited[1][2]["since"] == since


def test_mutes_are_checked_audited_bannered_and_ended_once(
    open_client: OpenClient, notify: dict[str, Any], seed: Any
) -> None:
    with open_client(notify=notify) as client:
        run_id = seed(client).run_id
        body = {"scope": "channel", "target": "ops-hook", "hours": 24, "reason": "a planned outage"}
        made = client.post(MUTES, json=body)
        of_run = client.post(MUTES, json=body | {"scope": "run", "target": run_id})
        refusals = [
            said(client.post(MUTES, json=body | change))
            for change in (
                {"hours": 0}, {"hours": 169}, {"target": "ops-slak"}, {"scope": "run", "target": "ops-hook"},
                {"reason": ""}, {"reason": "line\nbreak"}, {"reason": "x" * 1001},
                {"scope": "run", "target": run_id, "reason": "customer K12345678 complained"},
                {"scope": "run", "target": "R-0000000000000000000000000Z"},
                {"scope": "run", "target": "R-0000000000000000000000000Z", "hours": 0},  # 404 before 422
                {"scope": "run", "target": "R-0000000000000000000000000Z", "reason": "line\nbreak"},
            )
        ]  # fmt: skip
        status = client.get("/v1/notifications").json()
        mute_id = made.json()["mute_id"]
        ended = client.post(f"{MUTES}/{mute_id}/end")
        twice = client.post(f"{MUTES}/{mute_id}/end")
        unknown = client.post(f"{MUTES}/M-0000000000000000000000000Z/end")
        lapsed = repo.new_mute_id()
        sql(client, """INSERT INTO ds.notify_mutes (mute_id, scope, target, until, reason, created_by,
                           created_at)
                       VALUES (%s, 'channel', 'ops-hook', now() - interval '1 hour', 'r', 'dev:loopback',
                               now() - interval '2 hours')""", lapsed)  # fmt: skip
        until = sql(client, "SELECT until FROM ds.notify_mutes WHERE mute_id = %s", lapsed)[0]["until"]
        too_late = client.post(f"{MUTES}/{lapsed}/end")
        mutes, unmutes = audited(client, "notify.mute"), audited(client, "notify.unmute")
        after = client.get("/v1/notifications").json()
    assert made.status_code == 201 and of_run.status_code == 201
    target = "target must be a declared channel for scope channel, or R-… for scope run"
    assert refusals == [
        (422, "hours must be 1 to 168"), (422, "hours must be 1 to 168"), (422, target), (422, target),
        *[(422, "reason must be 1 to 1000 characters without control characters")] * 3, (422, KEY_SHAPED),
        *[(404, "run R-0000000000000000000000000Z not found")] * 3,
    ]  # fmt: skip
    until_made, until_run = (minute(datetime.fromisoformat(m.json()["until"])) for m in (made, of_run))
    run_mute = of_run.json()["mute_id"]
    assert status["banners"] == [
        f"Notifications to ops-hook are muted until {until_made} by dev:loopback ({mute_id})",
        f"Notifications for run {run_id} are muted until {until_run} by dev:loopback ({run_mute})",
    ]
    assert [m["mute_id"] for m in status["mutes"]] == [mute_id, of_run.json()["mute_id"]]
    assert ended.json() == {"mute_id": mute_id}
    assert said(twice) == (409, f"mute {mute_id} ended already")
    assert said(unknown) == (404, "mute M-0000000000000000000000000Z not found")
    assert said(too_late) == (409, f"mute {lapsed} lapsed at {minute(until)}")
    assert [(obj, d["scope"], d["target"], d["reason"]) for _, obj, d in mutes] == [
        (mute_id, "channel", "ops-hook", "a planned outage"),
        (of_run.json()["mute_id"], "run", run_id, "a planned outage"),
    ]
    assert unmutes == [("dev:loopback", mute_id, {})]
    assert [m["mute_id"] for m in after["mutes"]] == [of_run.json()["mute_id"]]


def test_a_mute_needs_an_admin_signed_in_within_15_minutes(
    open_client: OpenClient, callers: Any, notify: dict[str, Any]
) -> None:
    body = {"scope": "channel", "target": "ops-hook", "hours": 1, "reason": "planned"}
    now = datetime.now(UTC)
    with open_client(auth={"oidc": OIDC}, notify=notify) as client:
        stale = callers(client).session(*expand(["admin"]), auth_time=now - timedelta(minutes=16))
        fresh = callers(client).session(*expand(["admin"]), username="bob", auth_time=now)
        operator = callers(client).token("operator")
        answers = [client.post(MUTES, json=body, headers=headers) for headers in (stale, operator, fresh)]
        mute_id = answers[2].json()["mute_id"]
        ended = client.post(f"{MUTES}/{mute_id}/end", headers=stale)  # ending only restricts
    assert [said(a) for a in answers[:2]] == [(403, "reauth_required"), (403, "needs the admin role")]
    assert answers[2].status_code == 201 and ended.status_code == 200


def test_deliveries_are_listed_newest_first_with_their_stored_bodies(
    open_client: OpenClient, notify: dict[str, Any]
) -> None:
    listed = "/v1/notifications/deliveries"
    with open_client(notify=notify) as client:
        ids = [dead(client, event_id=f"seq:{n}:run.failed") for n in (1, 2, 3)]
        first = client.get(listed, params={"limit": 2}).json()
        rest = client.get(listed, params={"limit": 2, "cursor": first["next_cursor"]}).json()
        none = client.get(listed, params={"state": "sent"}).json()
        other = client.get(listed, params={"channel": "ops-slak"}).json()
        bad = client.get(listed, params={"state": "lost"})
    assert [d["delivery_id"] for d in first["items"] + rest["items"]] == ids[::-1]
    row = first["items"][0]
    assert (row["body"], row["body_sha256"], row["attempts"]) == ("{}", "0" * 64, 8)
    assert (row["last_error"], row["last_status"]) == ("http_5xx", 503)
    assert rest["next_cursor"] is None and none["items"] == other["items"] == [] and bad.status_code == 422


def test_banners_say_a_channel_fails_cannot_read_its_secret_or_the_notifier_is_behind(
    open_client: OpenClient, notify: dict[str, Any]
) -> None:
    with open_client(notify=notify) as client:
        dead(client, event_id="seq:1:run.failed", done="now() - interval '2 hours'")
        dead(client, event_id="seq:2:run.failed")
        sql(client, """INSERT INTO ds.notify_deliveries (delivery_id, event_id, event, severity, channel,
                           dedup_key, body, body_sha256, render, state, attempts, last_error, due_at)
                       VALUES (%s, 'seq:3:run.failed', 'run.failed', 'alert', 'ops-hook', 'run.end|R-2',
                               '{}', repeat('0', 64), 'webhook:1:1', 'pending', 1, 'secret_unreadable',
                               now() - interval '11 minutes')""", repo.new_delivery_id())  # fmt: skip
        since = sql(client, "SELECT min(done_at) AS at FROM ds.notify_deliveries WHERE state = 'dead'")
        found = client.get("/v1/notifications").json()
    [channel] = found["channels"]
    assert (channel["pending"], channel["dead_24h"], channel["last_error"]) == (1, 2, "secret_unreadable")
    assert found["banners"][:2] == [
        f"Notifications to ops-hook are failing: 2 not delivered since {minute(since[0]['at'])}",
        "Notifications to ops-hook cannot be sent: a secret file cannot be read",
    ]
    assert found["banners"][2].startswith("The notifier is behind: audit rows since ")
    assert found["banners"][2].endswith(" are not yet notified; is the worker running?")


@pytest.mark.usefixtures("loopback")
def test_no_secret_reaches_an_api_answer(open_client: OpenClient, tmp_path: Path) -> None:
    canary = "c4n4ry-s3cr3t"
    with receiving_thread(tmp_path) as (_, base, ref):
        (tmp_path / "teams.url").write_text(f"{base}/teams?sig={canary}\n")
        (tmp_path / "smtp.pw").write_text(f"{canary}-pw\n")
        notify = settings(base, ref)
        notify["channels"] |= {
            "owners-teams": {"kind": "teams", "url_ref": f"file:{tmp_path / 'teams.url'}",
                             "host": "127.0.0.1", "allow_private_network": True},
            "owners-mail": {"kind": "email", "smtp_host": "127.0.0.1", "smtp_port": 9, "username": "ds",
                            "password_ref": f"file:{tmp_path / 'smtp.pw'}", "sender": "ds@bank.example",
                            "recipients": ["owners@bank.example"], "allow_private_network": True},
        }  # fmt: skip
        with open_client(notify=notify) as client:
            tests = [
                client.post(f"/v1/notifications/channels/{name}/test")
                for name in ("owners-teams", "owners-mail")
            ]
            reads = [
                client.get(path)
                for path in ("/v1/notifications", "/v1/notifications/deliveries", "/v1/audit")
            ]
    assert [t.json()["state"] for t in tests] == ["sent", "dead"]
    secrets = [canary, (tmp_path / "hook.key").read_text().strip().removeprefix("whsec_"), str(tmp_path)]
    assert [s for s in secrets for answer in (*tests, *reads) if s in answer.text] == []
