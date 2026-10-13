"""Schedules over the API (docs/mvp/SCHEDULES.md §9, §10): who may read and write them in each auth mode,
every refusal with its status, the warnings, the list with its filters, banners and overdue count, a
preview that writes nothing, the zones, the firings as a projection of the worker's rows, a run naming the
schedule that started it, and what no answer ever tells (§2), keys among it. The firing itself is tested in
tests/unit/runner/test_schedule_fire.py; here the real ``fire`` runs on the server's loop."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from deltasleuth import schedules
from deltasleuth.auth.principal import expand
from deltasleuth.context import Context
from deltasleuth.core import cron
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.runner import schedule as firing
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import OIDC, RAW_KEY, SPEC_YAML, approved, audited, said

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
NEWER = SPEC_YAML.replace('"1.0.0"', '"1.1.0"')
ENDLESS = SPEC_YAML.replace('"1.0.0"', '"2.0.0"') + "execution: {cycleInterval: 1h}\n"
RUN, DRAW = "R-" + "0" * 25 + "1", "D-" + "0" * 25 + "2"
SC = "SC-" + "0" * 25 + "3"
ITEM = {
    "schedule_id", "name", "spec_name", "spec_id", "runs", "newer", "run", "cron", "time_zone",
    "start_within", "ends_at", "state", "auth_mode", "revision", "created_by", "created_by_name",
    "created_at", "set_by", "set_by_name", "set_at", "paused_by", "paused_by_name", "paused_at", "next_due",
    "overdue", "clock_behind", "last", "held",
}  # fmt: skip
FIRING = {
    "seq", "at", "outcome", "due", "to", "count", "late_s", "run_id", "reason", "code", "next", "text"
}  # fmt: skip
BANNED = re.compile(r"%|percent|uptime|success rate|forecast|completed|succeeded", re.I)


def sql(client: TestClient, query: str, *params: Any) -> list[dict[str, Any]]:
    async def go(ctx: Any) -> list[dict[str, Any]]:
        async with ctx.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query.encode(), params)
            return await cur.fetchall() if cur.description else []

    return client.portal.call(go, client.app.state.ctx)


async def _stored(ctx: Context, text: str) -> int:
    async with ctx.pool.connection() as conn:
        spec = compile_spec(parse_yaml_text(text), targets=ctx.targets)
        return await repo.save_spec(conn, spec, created_by=TESTER.actor)


def stored(client: TestClient, text: str = SPEC_YAML) -> int:
    """The spec stored, as an upload stores it; its spec_id."""
    return client.portal.call(_stored, client.app.state.ctx, text)


def minute(client: TestClient) -> datetime:
    """The database's now, at its minute."""
    return sql(client, "SELECT date_trunc('minute', now()) AS at")[0]["at"]


def declared(spec_id: int | None, at: datetime, zone: str = "UTC", **more: Any) -> dict[str, Any]:
    """A schedule's body: ``nightly``, batch runs of the spec daily at ``at``'s minute on the zone's clock."""
    local = at.astimezone(ZoneInfo(zone))
    body = {"name": "nightly", "run": {"mode": "batch"}, "cron": f"{local.minute} {local.hour} * * *"}
    return body | {"time_zone": zone} | ({} if spec_id is None else {"spec_id": spec_id}) | more


def due(client: TestClient, schedule_id: str, at: datetime) -> None:
    """The schedule moved by SQL to have ``at`` due: its cron's first time after its anchor."""
    sql(
        client,
        "UPDATE ds.schedules SET anchor_at = %s, next_due = %s WHERE schedule_id = %s",
        at - timedelta(minutes=1),
        at,
        schedule_id,
    )


def fire(client: TestClient, schedule_id: str, **options: Any) -> Any:
    """The worker's firing of the schedule, on the server's loop."""
    return client.portal.call(partial(firing.fire, client.app.state.ctx, schedule_id, **options))


def put(item: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The item as a ``PUT`` sends it back, with ``changes``."""
    names = ("name", "run", "cron", "time_zone", "start_within", "revision")
    body = {name: item[name] for name in names}
    body |= {"spec_name": item["spec_name"]} if item["spec_id"] is None else {"spec_id": item["spec_id"]}
    return body | changes


def strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    return [value] if isinstance(value, str) else []


# -- who may do what (§6, §10) -------------------------------------------------------------------------


def test_viewers_read_and_operators_write_under_sso(open_client: OpenClient, callers: Any) -> None:
    with open_client(auth={"oidc": OIDC}) as client:
        spec_id, at = stored(client), minute(client)
        viewer = callers(client).session("viewer")
        olga = callers(client).session(*expand(["operator"]), username="olga")
        bot = callers(client).token("operator")
        body = declared(spec_id, at)
        refused = [
            client.post("/v1/schedules", json=body, headers=viewer),
            client.post(
                "/v1/schedules/preview", json={"cron": body["cron"], "time_zone": "UTC"}, headers=viewer
            ),
        ]
        made = client.post("/v1/schedules", json=body, headers=olga)
        item = made.json()
        schedule_id = item["schedule_id"]
        path = f"/v1/schedules/{schedule_id}"
        refused += [
            client.put(path, json=put(item), headers=viewer),
            *(client.post(f"{path}/{act}", headers=viewer) for act in ("pause", "resume")),
            client.delete(path, headers=viewer),
        ]
        reads = [
            client.get(read, headers=viewer)
            for read in ("/v1/schedules", "/v1/schedules/zones", path, f"{path}/firings")
        ]
        before = client.get(path, headers=viewer).json()["runs"]["approval"]
        due(client, schedule_id, at)
        fire(client, schedule_id)  # the gate judges each firing: the version is not approved
        unapproved = client.get(f"{path}/firings", headers=viewer).json()["items"]
        tomorrow = at + timedelta(days=1)
        fire(client, schedule_id, now=tomorrow + timedelta(seconds=1))  # refused again: held, not chained
        holding = client.get(path, headers=viewer).json()
        approval_id = approved(client, spec_id)
        after = client.get(path, headers=viewer).json()["runs"]["approval"]
        paused = client.post(f"{path}/pause", headers=bot)
        saved = client.put(path, json=put(paused.json(), start_within=30), headers=bot)
        resumed = client.post(f"{path}/resume", headers=olga)
        rows = sql(
            client, "SELECT actor, action FROM ds.audit_log WHERE object = %s ORDER BY id", schedule_id
        )
    assert [said(r) for r in refused] == [(403, "needs the operator role")] * 6
    assert [r.status_code for r in reads] == [200] * 4 and made.status_code == 201
    needs = f"spec {spec_id} (api-demo 1.0.0) needs a four-eyes approval of clear before it runs"
    assert item["warnings"] == [f"{needs}: its firings are not started until it is approved"]
    assert (item["auth_mode"], item["created_by"], item["created_by_name"]) == (
        "oidc",
        item["set_by"],
        "olga",
    )
    assert before == {
        "needs": ["clear"], "mode": "oidc", "state": "unapproved", "approval_id": None, "refusal": needs,
    }  # fmt: skip
    assert [(f["outcome"], f["reason"], f["text"]) for f in unapproved] == [
        (
            "missed",
            "approval",
            f"not started: spec {spec_id} lacked the four-eyes approval it needs (see Approvals)",
        )
    ]
    again = cron.utc(tomorrow)
    assert holding["held"] == {"due": again, "to": again, "count": 1} and len(holding["firings"]) == 1
    assert holding["last"]["text"] == (
        f"not started: 1 more time since {again} for the same reason, chained as one row within 24 h"
    )
    assert after == before | {"state": "approved", "approval_id": approval_id, "refusal": None}
    assert (paused.status_code, saved.status_code, resumed.status_code) == (200, 200, 200)
    assert saved.json()["warnings"] == [] and saved.json()["state"] == "paused"  # a paused one stays paused
    assert (saved.json()["set_by"][:6], saved.json()["set_by_name"]) == ("token:", "ci-bot")
    assert resumed.json()["state"] == "active" and resumed.json()["warnings"] == []
    assert [(actor.split(":")[0], action) for actor, action in ((r["actor"], r["action"]) for r in rows)] == [
        ("user", "schedule.create"),  # each write is its caller's own row
        ("system", "schedule.missed"),
        ("token", "schedule.missed"),  # the held range, chained by the write that follows it
        ("token", "schedule.pause"),
        ("token", "schedule.update"),
        ("user", "schedule.resume"),
    ]


@pytest.mark.parametrize(
    ("auth", "mode", "said_"), [({}, "tokens", "tokens mode"), (None, "none", "developer mode")]
)
def test_tokens_and_developer_mode_write_as_their_callers(
    open_client: OpenClient, callers: Any, auth: Any, mode: str, said_: str
) -> None:
    with open_client(auth=auth) as client:
        spec_id, at = stored(client), minute(client)
        operator = callers(client).token("operator") if auth is not None else {}
        viewer = callers(client).token("viewer") if auth is not None else {}
        made = client.post("/v1/schedules", json=declared(spec_id, at), headers=operator)
        item = made.json()
        path = f"/v1/schedules/{item['schedule_id']}"
        read = client.get(path, headers=viewer)
        denied = client.post(f"{path}/pause", headers=viewer) if auth is not None else None
        due(client, item["schedule_id"], at)
        fire(client, item["schedule_id"])  # no gate here: the run goes ahead, labelled unapproved
        fired = client.get(f"{path}/firings", headers=viewer).json()["items"]
        run = client.get(f"/v1/runs/{fired[0]['run_id']}", headers=viewer).json()
        gone = client.delete(path, headers=operator)
        rows = audited(client, "schedule.create") + audited(client, "schedule.delete")
    assert made.status_code == 201 and item["auth_mode"] == mode
    assert item["warnings"] == [f"{said_} has no approvers: its runs are labelled unapproved"]
    assert read.json()["runs"]["approval"] == {
        "needs": ["clear"], "mode": mode, "state": "unapproved", "approval_id": None, "refusal": None,
    }  # fmt: skip
    assert denied is None or said(denied) == (403, "needs the operator role")  # developer mode acts as admin
    assert [f["outcome"] for f in fired] == ["fire"] and run["created_by"] == "system:worker"
    assert run["approval"]["mode"] == mode and run["approval"]["state"] == "unapproved"
    assert gone.json().keys() == {"schedule_id", "deleted_at"} and gone.json()["deleted_at"]
    actor = "token:" if auth is not None else "dev:loopback"
    assert [row[0].startswith(actor) for row in rows] == [True, True]


# -- every refusal (§9) --------------------------------------------------------------------------------

LONG = f"0 {','.join(map(str, range(24)))} {','.join(map(str, range(1, 32)))} * *"
CRONS = [
    ("0 2 * * * *", "cron must have 5 fields (minute hour day-of-month month day-of-week), not 6"),
    ("61 2 * * *", "cron field minute: '61' is outside 0-59"),
    ("0 24 * * *", "cron field hour: '24' is outside 0-23"),
    ("0 2 0 * *", "cron field day-of-month: '0' is outside 1-31"),
    ("0 2 * 13 *", "cron field month: '13' is outside 1-12 or JAN-DEC"),
    ("0 2 * * 8", "cron field day-of-week: '8' is outside 0-7 or SUN-SAT"),
    ("0 2- * * *", "cron field hour: '2-' is not *, a number, a range a-b, a list a,b or a step */n or"
     " a-b/n"),
    ("0 2 * * 5-1", "cron field day-of-week: range '5-1' runs backwards"),
    ("*/0 2 * * *", "cron field minute: step '0' must be at least 1"),
    ("0 2 1 * 1", "cron sets both day-of-month and day-of-week: set one and leave the other * (cron dialects"
     " disagree on what both mean)"),
    ("0 2 30 2 *", "cron names no time in the next 8 years"),
    ("*/5 2 * * *", "cron fires at 02:00 and 02:05: a schedule fires at most every 15 minutes (a soak's"
     " execution.cycleInterval repeats faster)"),
    (LONG, "cron must be at most 100 characters"),
    ("0 2 * * \u017fun", "cron field day-of-week: '\u017fun' is not *, a number, a range a-b, a list a,b or a"
     " step */n or a-b/n"),  # U+017F upper-cases to S: a preview refuses what no row could store
]  # fmt: skip
ZONES = [
    ("Asia/Calcutta", "'Asia/Calcutta' is not a time zone this build uses (tzdata 2026c): use Asia/Kolkata"),
    ("Mars/Olympus", "'Mars/Olympus' is not a time zone this build knows (tzdata 2026c): use an IANA region"
     " name such as Europe/London, or UTC"),
    ("localtime", "time_zone must be UTC or an IANA region name such as Europe/London: 'localtime' is not"
     " one"),
]  # fmt: skip


def test_each_refusal_has_its_status_and_message(
    open_client: OpenClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cron, "tzdata", lambda: "2026c")
    with open_client() as client:
        spec_id, at = stored(client), minute(client)
        endless = stored(client, ENDLESS)
        good = declared(spec_id, at)
        soon = at + timedelta(days=1)

        def refused(**changes: Any) -> tuple[int, str]:
            body = {name: value for name, value in (good | changes).items() if value is not None}
            return said(client.post("/v1/schedules", json=body))

        slug = (
            "name must be a slug of 1 to 63 characters (lower-case letters, digits and single hyphens,"
            " starting with a letter)"
        )
        either = "give spec_id (a pinned version) or spec_name (the newest version at each firing), not both"
        ends = "ends_at must be an ISO-8601 time with a UTC offset, later than now"
        answers = {
            "S1": [refused(name=name) for name in ("", "Nightly", "a--b", "a" * 64, "zones", "preview")],
            "S3": [refused(spec_name="api-demo"), refused(spec_id=None)],
            "S4": [refused(spec_id=99), refused(spec_id=None, spec_name="customer-profile")],
            "S6": [
                refused(run={"mode": "batch", "soak": {"rolling": True}}),
                refused(run={"mode": "soak"}),
            ],
            "S7": [refused(spec_id=endless, run={"mode": "soak"})],
            "S8": [refused(start_within=n) for n in (14, 1441)],
            "S9": [
                refused(ends_at="tomorrow"),
                refused(ends_at="2026-10-01T00:00:00"),
                refused(ends_at="2020-01-01T00:00:00+00:00"),
                refused(ends_at="9999-12-31T23:59:59-12:00"),  # the year 10000 in UTC: no row holds it
                refused(ends_at="9999-12-31T23:59:59+00:00"),  # and in a store whose zone is ahead of UTC
                refused(cron="0 2 29 2 *", ends_at=soon.isoformat()),
            ],
            "C": [refused(cron=text) for text, _ in CRONS],
            "Z": [refused(time_zone=zone) for zone, _ in ZONES],
        }
        previews = [
            said(client.post("/v1/schedules/preview", json={"cron": text, "time_zone": zone}))
            for text, zone in [(text, "UTC") for text, _ in CRONS]
            + [("0 2 * * *", zone) for zone, _ in ZONES]
        ]
        keyed = client.post("/v1/schedules", json=good | {"run": {"mode": "batch", "keys": [RAW_KEY]}})
        padded = "0 2 * * *" + " " * 992  # over the 1,000 characters a body's cron may be, before any parse
        oversize = [
            client.post("/v1/schedules", json=good | {"cron": padded}),
            client.post("/v1/schedules", json=good | {"time_zone": "Europe/" + "L" * 58}),
            client.post("/v1/schedules/preview", json={"cron": padded, "time_zone": "UTC"}),
            client.post(
                "/v1/schedules/preview", json={"cron": "0 2 * * *", "time_zone": "Europe/" + "L" * 58}
            ),
        ]
        cron._parse.cache_clear()
        trimmed = [  # at most 1,000: read trimmed, and remembered as its trimmed text alone
            client.post("/v1/schedules/preview", json={"cron": padded[:-n], "time_zone": "UTC"})
            for n in (1, 2, 3)
        ]
        kept = cron._parse.cache_info().currsize
        assert sql(client, "SELECT count(*) AS n FROM ds.schedules")[0]["n"] == 0  # nothing was written
        compiled = Context.spec

        async def keyless(ctx: Context, conn: Any, found: int, **options: Any) -> Any:
            spec = await compiled(ctx, conn, found, **options)  # as only an earlier build stored one
            return dataclasses.replace(spec, raw=spec.raw | {"key": {"name": "item_id"}})

        with monkeypatch.context() as patched:
            patched.setattr(Context, "spec", keyless)
            answers["S5"] = [refused()]
        first = client.post("/v1/schedules", json=good).json()
        schedule_id = first["schedule_id"]
        path = f"/v1/schedules/{schedule_id}"
        answers["S2"] = [refused()]
        missing = "/v1/schedules/SC-0000000000000000000000000Z"
        answers["S11"] = [
            said(client.get(missing)),
            said(client.get(f"{missing}/firings")),
            said(client.put(missing, json=put(first))),
            *(said(client.post(f"{missing}/{act}")) for act in ("pause", "resume")),
            said(client.delete(missing)),
            said(client.put(path, json=put(first, revision=4))),
            said(client.post(f"{path}/resume")),
        ]
        assert client.post(f"{path}/pause").json()["state"] == "paused"
        answers["S11"].append(said(client.post(f"{path}/pause")))
        sql(client, "UPDATE ds.schedules SET ends_at = now() - interval '1 minute'")  # its end passes
        ended = client.post(f"{path}/resume").json()
        ended_at = sql(client, "SELECT ends_at FROM ds.schedules")[0]["ends_at"]
        answers["S11"].append(said(client.post(f"{path}/pause")))
        assert client.delete(path).status_code == 200
        answers["S11"] += [
            said(client.put(path, json=put(first, revision=2))),
            *(said(client.post(f"{path}/{act}")) for act in ("pause", "resume")),
            said(client.delete(path)),
        ]
        tombstone = client.get(path).json()
        sql(
            client,
            """INSERT INTO ds.schedules (schedule_id, name, spec_name, run, cron, time_zone, auth_mode,
                   created_by, set_by)
               SELECT 'SC-' || lpad(n::text, 26, '0'), 'filler-' || n, 'api-demo', '{"mode": "batch"}',
                      '0 2 * * *', 'UTC', 'none', 'dev:loopback', 'dev:loopback'
               FROM generate_series(1, 200) n""",
        )
        answers["S10"] = [refused()]
    named = f"spec {endless} (api-demo 2.0.0)"
    not_found = (404, "schedule SC-0000000000000000000000000Z not found")
    deleted = (409, f"schedule {schedule_id} is deleted")
    assert answers == {
        "S1": [
            *[(422, slug)] * 4,
            (422, "name must not be zones: /v1/schedules/zones is a route of the API's own"),
            (422, "name must not be preview: /v1/schedules/preview is a route of the API's own"),
        ],
        "S2": [(409, f"a schedule named nightly exists already ({schedule_id})")],
        "S3": [(422, either)] * 2,
        "S4": [(404, "spec 99 not found"), (404, "no spec is named customer-profile")],
        "S5": [
            (422, f"spec {spec_id} (api-demo 1.0.0) has no key source and is not a pair of tables: a"
             " schedule's runs need one of them")
        ],
        "S6": [
            (422, "mode must be batch or soak, and only a soak run takes soak settings"),
            (422, "a soak run needs the spec's execution.cycleInterval"),
        ],
        "S7": [
            (422, f"a scheduled soak must end by itself: {named} needs execution.stop.maxCycles, maxDuration"
             " or until, or every later firing is skipped while it runs")
        ],
        "S8": [(422, "start_within must be 15 to 1440 minutes")] * 2,
        "S9": [(422, ends)] * 5 + [(422, f"the schedule has no time before its ends_at ({cron.utc(soon)})")],
        "S10": [(409, "this installation holds 200 schedules, the most it takes: delete one first")],
        "S11": [
            *[not_found] * 6,
            (409, f"schedule {schedule_id} changed since you loaded it (revision 1): reload and edit again"),
            (409, f"schedule {schedule_id} is not paused"),
            (409, f"schedule {schedule_id} is paused already"),
            (409, f"schedule {schedule_id} has ended"),
            *[deleted] * 4,
        ],
        "C": [(422, message) for _, message in CRONS],
        "Z": [(422, message) for _, message in ZONES],
    }  # fmt: skip
    assert previews == [(422, message) for _, message in CRONS + ZONES]
    assert (ended["state"], ended["next_due"], ended["warnings"]) == (
        "ended",
        None,
        [f"the schedule has no time before its ends_at ({cron.utc(ended_at)})"],
    )
    assert said(keyed) == (422, "invalid request")  # a schedule holds no key list
    assert [issue["pointer"] for issue in keyed.json()["issues"]] == ["/body/run/keys"]
    assert [said(answer) for answer in oversize] == [(422, "invalid request")] * 4
    assert [answer.json()["issues"][0]["pointer"] for answer in oversize] == [
        "/body/cron", "/body/time_zone", "/body/cron", "/body/time_zone",
    ]  # fmt: skip
    assert [answer.status_code for answer in trimmed] == [200] * 3 and kept == 1
    assert RAW_KEY not in keyed.text and tombstone["state"] == "deleted" and tombstone["next_due"] is None


# -- preview and zones ---------------------------------------------------------------------------------


def test_a_preview_gives_ten_times_and_the_notes_and_writes_nothing(open_client: OpenClient) -> None:
    with open_client() as client:
        before = sql(client, "SELECT count(*) AS n FROM ds.audit_log")[0]["n"]
        found = client.post(
            "/v1/schedules/preview", json={"cron": " 30  1 * * * ", "time_zone": "Europe/London"}
        )
        now = sql(client, "SELECT now() AS at")[0]["at"]
        written = sql(
            client,
            "SELECT (SELECT count(*) FROM ds.schedules) AS schedules, count(*) AS rows FROM ds.audit_log",
        )
    answer = found.json()
    times = cron.upcoming("30 1 * * *", "Europe/London", now, 10)
    assert found.status_code == 200 and answer.keys() == {"upcoming", "notes", "tzdata"}
    assert answer["upcoming"] == [
        {"utc": cron.utc(at), "local": f"{at.astimezone(ZoneInfo('Europe/London')):%Y-%m-%d} 01:30"}
        for at in times
    ]
    assert answer["notes"] == cron.notes("30 1 * * *", "Europe/London", now) and len(answer["notes"]) == 2
    assert answer["notes"][0].endswith(
        ": 01:30 happens twice in Europe/London (clocks go back): the schedule fires at the first (00:30 UTC)"
    )
    assert answer["notes"][1].endswith(
        ": 01:30 does not exist in Europe/London (clocks go forward): that firing happens at 02:30 (01:30"
        " UTC)"
    )
    assert answer["tzdata"] == cron.tzdata() and written == [{"schedules": 0, "rows": before}]


def test_a_zone_named_by_its_offset_is_warned_of(open_client: OpenClient) -> None:
    said_ = (
        "the name of 'Etc/GMT+5' holds a number: under a key pattern that matches a short number, the runs of"
        " a schedule in it get no evidence pack: use a region's zone, or UTC"
    )
    with open_client() as client:
        spec_id, at = stored(client), minute(client)
        offered = client.get("/v1/schedules/zones").json()["zones"]
        noted = client.post("/v1/schedules/preview", json={"cron": "0 2 * * *", "time_zone": "Etc/GMT+5"})
        made = client.post("/v1/schedules", json=declared(spec_id, at, "Etc/GMT+5"))
        saved = client.put(f"/v1/schedules/{made.json()['schedule_id']}", json=put(made.json()))
        region = client.put(
            f"/v1/schedules/{made.json()['schedule_id']}", json=put(saved.json(), time_zone="Asia/Kolkata")
        )
    assert "Etc/GMT+5" in offered  # the list offers it, so the form and a save say what it costs
    assert noted.json()["notes"] == [said_] and made.status_code == 201
    assert made.json()["warnings"][1:] == [said_] == saved.json()["warnings"][1:]
    assert len(region.json()["warnings"]) == 1  # a region's name holds no number


def test_the_zones_are_those_a_schedule_takes(client: TestClient) -> None:
    found = client.get("/v1/schedules/zones").json()
    assert found.keys() == {"tzdata", "zones", "renamed"} and found["tzdata"] == cron.tzdata()
    zones = found["zones"]
    assert zones == sorted(zones) and {"UTC", "Europe/London", "Asia/Kolkata"} <= set(zones)
    assert all(zone == "UTC" or "/" in zone for zone in zones)  # never localtime or a bare abbreviation
    assert not set(zones) & set(cron.RENAMED) and found["renamed"] == cron.RENAMED
    assert found["renamed"]["Asia/Calcutta"] == "Asia/Kolkata"


# -- the list (§10) ------------------------------------------------------------------------------------


def test_the_list_filters_pages_counts_the_overdue_and_compiles_no_spec(
    open_client: OpenClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_client() as client:
        spec_id, at = stored(client), minute(client)
        made = {
            name: client.post("/v1/schedules", json=body | {"name": name}).json()
            for name, body in (
                ("pinned", declared(spec_id, at)),
                ("following", declared(None, at, spec_name="api-demo")),
                ("stalled", declared(spec_id, at)),
                ("zoned", declared(spec_id, at)),
                ("gone", declared(spec_id, at)),
            )
        }
        ids = {name: item["schedule_id"] for name, item in made.items()}
        newer = stored(client, NEWER)
        due(client, ids["pinned"], at - timedelta(minutes=11))  # due over ten minutes ago, not settled
        sql(
            client,
            "UPDATE ds.schedules SET anchor_at = now() + interval '1 hour', next_due = now() + interval"
            " '2 hours' WHERE schedule_id = %s",
            ids["stalled"],
        )
        sql(
            client,
            "UPDATE ds.schedules SET paused_by = 'system:worker', paused_at = now(), next_due = NULL"
            " WHERE schedule_id = %s",
            ids["zoned"],
        )
        client.delete(f"/v1/schedules/{ids['gone']}")

        def unread(*_: Any, **__: Any) -> Any:
            raise AssertionError("the list compiles no spec")

        with monkeypatch.context() as patched:
            patched.setattr(Context, "spec", unread)
            listed = client.get("/v1/schedules").json()
            pages = [client.get("/v1/schedules", params={"limit": 3}).json()]
            pages.append(
                client.get("/v1/schedules", params={"limit": 3, "cursor": pages[0]["next_cursor"]}).json()
            )
            filtered = {
                name: [item["name"] for item in client.get("/v1/schedules", params=params).json()["items"]]
                for name, params in {
                    "spec": {"spec_name": "api-demo"}, "other": {"spec_name": "customer-profile"},
                    "paused": {"state": "paused"}, "active": {"state": "active"},
                    "deleted": {"state": "deleted"}, "ended": {"state": "ended"},
                    "set_by": {"set_by": "dev:loopback"}, "leaver": {"set_by": "user:U-0123456789abcdef"},
                }.items()
            }  # fmt: skip
        bad = client.get("/v1/schedules", params={"state": "lost"})
        stalled = sql(client, "SELECT next_due FROM ds.schedules WHERE schedule_id = %s", ids["stalled"])[0]
    assert listed.keys() == {"not", "tzdata", "overdue", "banners", "items", "next_cursor"}
    assert listed["not"] == list(schedules.NOT) and listed["tzdata"] == cron.tzdata()
    items = {item["name"]: item for item in listed["items"]}
    assert [item["name"] for item in listed["items"]] == ["zoned", "stalled", "following", "pinned"]
    assert all(item.keys() == ITEM for item in listed["items"]) and listed["next_cursor"] is None
    pinned, following = items["pinned"], items["following"]
    assert (pinned["spec_id"], pinned["runs"], pinned["newer"]) == (
        spec_id,
        {"spec_id": spec_id, "version": "1.0.0"},
        {"spec_id": newer, "version": "1.1.0"},
    )
    assert (following["spec_id"], following["runs"], following["newer"]) == (
        None,
        {"spec_id": newer, "version": "1.1.0"},  # the name's newest version, at each firing
        None,
    )
    late = cron.utc(at - timedelta(minutes=11))
    assert (pinned["overdue"], pinned["next_due"]["utc"], listed["overdue"]) == (True, late, 1)
    assert [(item["overdue"], item["clock_behind"]) for item in listed["items"]] == [
        (False, False), (False, True), (False, False), (True, False),
    ]  # fmt: skip
    assert following["next_due"] == {
        "utc": cron.utc(at + timedelta(days=1)),
        "local": f"{at + timedelta(days=1):%Y-%m-%d %H:%M}",
    }
    assert (items["zoned"]["state"], items["zoned"]["next_due"], following["last"]) == ("paused", None, None)
    assert listed["banners"] == [
        "schedule zoned is paused by the worker: its time zone or cron did not load in the worker's build:"
        " save it with a zone and a cron the build takes, then resume it",
        f"schedule zoned is pinned to 1.0.0 (spec {spec_id}); 1.1.0 (spec {newer}) is stored",
        f"schedule stalled is pinned to 1.0.0 (spec {spec_id}); 1.1.0 (spec {newer}) is stored",
        "the database clock is behind schedule stalled's last firing: nothing fires until it passes"
        f" {cron.utc(stalled['next_due'])}",
        f"schedule pinned was due at {late} and is not settled yet: a worker may be retrying or starting it,"
        " or none is running (ds worker, or ds serve)",
        f"schedule pinned is pinned to 1.0.0 (spec {spec_id}); 1.1.0 (spec {newer}) is stored",
    ]
    assert [[item["name"] for item in page["items"]] for page in pages] == [
        ["zoned", "stalled", "following"],
        ["pinned"],
    ]
    assert pages[1]["next_cursor"] is None and pages[0]["overdue"] == 1  # every schedule's, whatever the page
    assert filtered == {
        "spec": ["zoned", "stalled", "following", "pinned"], "other": [], "paused": ["zoned"],
        "active": ["stalled", "following", "pinned"], "deleted": ["gone"], "ended": [],
        "set_by": ["zoned", "stalled", "following", "pinned"], "leaver": [],
    }  # fmt: skip
    assert said(bad) == (422, "invalid request")


def test_upcoming_counts_from_now_while_a_time_is_overdue(open_client: OpenClient) -> None:
    with open_client() as client:
        spec_id, at = stored(client), minute(client)
        quarters = declared(spec_id, at) | {"cron": "*/15 * * * *"}
        late = client.post("/v1/schedules", json=quarters).json()["schedule_id"]
        ahead = client.post("/v1/schedules", json=quarters | {"name": "ahead"}).json()["schedule_id"]
        due(client, late, at.replace(minute=0) - timedelta(hours=3))  # no worker for three hours
        sql(
            client,
            "UPDATE ds.schedules SET anchor_at = %s, next_due = %s WHERE schedule_id = %s",
            at.replace(minute=0) + timedelta(hours=2),  # the database's clock is behind what it settled
            at.replace(minute=15) + timedelta(hours=2),
            ahead,
        )
        shown, stalled = (client.get(f"/v1/schedules/{found}").json() for found in (late, ahead))
    times = [found["utc"] for found in shown["upcoming"]]
    # The time due and not settled is the item's next_due; the next times are after now: D6 starts no older.
    assert shown["overdue"] and shown["next_due"]["utc"] < cron.utc(at) < min(times) and len(times) == 5
    first = datetime.fromisoformat(times[0])
    assert times == [cron.utc(first + timedelta(minutes=15 * n)) for n in range(5)]
    assert (
        stalled["clock_behind"] and stalled["upcoming"][0] == stalled["next_due"]
    )  # never before its anchor


# -- firings, and a run's schedule (§10) ---------------------------------------------------------------


def test_firings_are_a_projection_of_the_worker_s_rows_without_actors(open_client: OpenClient) -> None:
    with open_client() as client:
        spec_id, at = stored(client), minute(client)
        item = client.post("/v1/schedules", json=declared(spec_id, at)).json()
        schedule_id = item["schedule_id"]
        path = f"/v1/schedules/{schedule_id}"
        due(client, schedule_id, at)
        fire(client, schedule_id)
        run_id = sql(client, "SELECT last_run_id FROM ds.schedules")[0]["last_run_id"]
        shown = client.get(path).json()
        sql(client, "UPDATE ds.runs SET status = 'completed', finished_at = now() WHERE run_id = %s", run_id)
        sql(
            client,
            "UPDATE ds.schedules SET anchor_at = anchor_at - interval '3 days', next_due = next_due -"
            " interval '3 days' WHERE schedule_id = %s",
            schedule_id,
        )
        saved = client.put(path, json=put(shown, start_within=30))  # three times were due: missed, as one row
        before = sql(client, "SELECT count(*) AS n FROM ds.audit_log")[0]["n"]
        firings = client.get(f"{path}/firings")
        after = client.get(path).json()
        first = client.get(f"{path}/firings", params={"limit": 1}).json()
        rest = client.get(f"{path}/firings", params={"limit": 1, "cursor": first["next_cursor"]}).json()
        malformed = client.get(f"{path}/firings", params={"cursor": "nope"})
        reads = sql(client, "SELECT count(*) AS n FROM ds.audit_log")[0]["n"] - before
        chain = sql(
            client,
            """SELECT c.seq, l.actor, l.action FROM ds.audit_log l JOIN ds.audit_chain c USING (id)
               WHERE l.object = %s AND l.action IN ('schedule.fire', 'schedule.missed') ORDER BY l.id DESC""",
            schedule_id,
        )
    fired, late = shown["last"], shown["firings"][0]["late_s"]
    on_time = f"started run {run_id} " + ("at its time" if late < 60 else "1 min after its time")
    assert (fired["run_id"], fired["run_status"], fired["text"]) == (
        run_id,
        "running",  # the run's status says how it ended, never the firing
        on_time,
    )
    assert shown["runs"]["approval"]["state"] == "unapproved" and shown["held"] is None
    assert [u["utc"] for u in shown["upcoming"]] == [cron.utc(at + timedelta(days=n)) for n in range(1, 6)]
    assert [f["text"] for f in shown["firings"]] == [fired["text"]] and saved.status_code == 200
    items = firings.json()["items"]
    assert all(found.keys() == FIRING for found in items) and firings.json()["next_cursor"] is None
    missed, started = items
    range_ = (cron.utc(at - timedelta(days=2)), cron.utc(at))
    assert missed == {
        "seq": chain[0]["seq"], "at": missed["at"], "outcome": "missed", "due": range_[0], "to": range_[1],
        "count": 3, "late_s": None, "run_id": None, "reason": "superseded", "code": "update",
        "next": saved.json()["next_due"]["utc"],
        "text": f"not started: 3 times, {range_[0]} to {range_[1]}, were due and not started when the"
        " schedule was saved again",
    }  # fmt: skip
    assert started == {
        "seq": chain[1]["seq"], "at": started["at"], "outcome": "fire", "due": cron.utc(at), "to": None,
        "count": None, "late_s": late, "run_id": run_id, "reason": None, "code": None,
        "next": cron.utc(at + timedelta(days=1)), "text": on_time,
    }  # fmt: skip
    assert [row["actor"] for row in chain] == ["dev:loopback", "system:worker"]  # in the audit log only
    assert not re.search(r"system:|dev:|local:|Q-[0-9a-f]{16}|127\.0\.0\.1", firings.text)
    assert reads == 0  # a projection: reading it is not reading the audit log
    assert (after["last"]["text"], after["last"]["run_id"], after["last"]["run_status"]) == (
        missed["text"],
        None,
        None,
    )
    assert [f["text"] for f in after["firings"]] == [missed["text"], started["text"]]
    assert first["items"] == [missed] and rest["items"] == [started] and rest["next_cursor"] is None
    assert said(malformed) == (400, "malformed cursor")


def test_a_run_names_its_schedule_in_the_zone_in_force_at_its_firing(
    open_client: OpenClient, seed: Any
) -> None:
    with open_client() as client:
        unscheduled = seed(client)
        at = minute(client)
        item = client.post("/v1/schedules", json=declared(unscheduled.spec_id, at, "Europe/London")).json()
        schedule_id = item["schedule_id"]
        due(client, schedule_id, at)
        fire(client, schedule_id)
        run_id = sql(client, "SELECT last_run_id FROM ds.schedules")[0]["last_run_id"]
        changed = client.put(
            f"/v1/schedules/{schedule_id}", json=put(item, name="weekly", time_zone="Asia/Kolkata")
        )
        run = client.get(f"/v1/runs/{run_id}").json()
        other = client.get(f"/v1/runs/{unscheduled.run_id}").json()
        mine = client.get("/v1/runs", params={"schedule_id": schedule_id}).json()["items"]
        none = client.get("/v1/runs", params={"schedule_id": "SC-0000000000000000000000000Z"}).json()["items"]
        every = client.get("/v1/runs").json()["items"]
        client.delete(f"/v1/schedules/{schedule_id}")
        kept = client.get(f"/v1/runs/{run_id}").json()["schedule"]
    assert changed.status_code == 200 and changed.json()["time_zone"] == "Asia/Kolkata"
    found, utc = run["schedule"], cron.utc(at)
    local = f"{at.astimezone(ZoneInfo('Europe/London')):%Y-%m-%d %H:%M}"
    assert found.keys() == {"schedule_id", "name", "due", "time_zone", "late_s", "text"}
    assert (found["schedule_id"], found["name"], found["due"], found["time_zone"]) == (
        schedule_id,
        "weekly",  # the schedule as it is named now
        utc,
        "Europe/London",  # the zone it fired in
    )
    late = found["late_s"]
    said_late = f"{late} s" if late < 60 else f"{late // 60} min"
    assert 0 <= late < 120 and found["text"] == (
        f"started by schedule weekly ({schedule_id}) for {local} Europe/London ({utc}), {said_late} after its"
        " time"
    )
    assert other["schedule"] is None and kept == found  # a deleted schedule's runs still name it
    assert [r["run_id"] for r in mine] == [run_id] and none == []
    assert {r["run_id"] for r in every} == {run_id, unscheduled.run_id}


# -- the texts (§9), each built by schedules.line ------------------------------------------------------


def detail(reason: str, code: str | None = None, **facts: Any) -> dict[str, Any]:
    """A ``schedule.missed`` row's detail, as ``schedules.missed`` builds one."""
    row = {
        "spec_name": "customer-profile", "run": {"mode": "batch"}, "runs_spec_id": 12, "revision": 4,
        "time_zone": facts.pop("time_zone", "Europe/London"), "start_within": facts.pop("start_within", 60),
        "auth_mode": facts.pop("auth_mode", "oidc"),
    }  # fmt: skip
    at = datetime(2026, 10, 7, 1, tzinfo=UTC)
    return schedules.missed(row, reason, code, due=facts.pop("due", at), next=at + timedelta(days=1), **facts)


def test_firing_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "tzdata", lambda: "2026c")
    first, last = datetime(2026, 10, 5, 1, tzinfo=UTC), datetime(2026, 10, 7, 1, tzinfo=UTC)
    seen = last + timedelta(hours=7, minutes=30)
    fired = {"run_id": RUN, "late_s": 4}
    run = {"schedule_id": SC, "name": "nightly-customers", "due": "2026-10-07T01:00Z", "late_s": 4}
    later = datetime(2026, 10, 8, 1, tzinfo=UTC)
    texts = [
        schedules.line("fire", fired),
        schedules.line("fire", fired | {"late_s": 12 * 60 + 40}),
        schedules.line("run", run | {"time_zone": "Europe/London"}),
        schedules.line("run", run | {"time_zone": "Mars/Olympus", "late_s": 3725}),
        schedules.line("missed", detail("late", "down", to=last, count=1), at=seen),
        schedules.line("missed", detail("late", "down", due=first, to=last, count=3), at=seen),
        schedules.line("missed", detail("late", "busy"), at=seen),
        schedules.line("missed", detail("late", "fired_already"), at=seen),
        schedules.line("missed", detail("superseded", "update", to=last, count=1), at=seen),
        schedules.line("missed", detail("superseded", "pause"), at=seen),  # times that could not be counted
        schedules.line("missed", detail("superseded", "delete", due=first, to=last, count=3), at=seen),
        schedules.line("missed", detail("zone", "zone", time_zone="Europe/Kyiv", tzdata="2025b"), at=seen),
        schedules.line("missed", detail("zone", "zone", time_zone="Europe/Kyiv"), at=seen),  # no version kept
        schedules.line("missed", detail("zone", "cron", tzdata="2025b"), at=seen),
        schedules.line("missed", detail("overlap", run_id=RUN), at=seen),
        schedules.line("missed", detail("mode", auth_mode="tokens", worker_mode="oidc"), at=seen),
        schedules.line("missed", detail("mode", auth_mode="oidc", worker_mode="none"), at=seen),
        schedules.line("missed", detail("setter"), at=seen),
        schedules.line("missed", detail("approval"), at=seen),
        schedules.line("missed", detail("licence", "expired"), at=seen),
        schedules.line("missed", detail("sample", "unclosed", draw_id=DRAW), at=seen),
        schedules.line("missed", detail("sample", "live"), at=seen),
        schedules.line("missed", detail("sample", "refused"), at=seen),
        schedules.line("missed", detail("not_started", error_type="ValueError"), at=seen),
        schedules.line("missed", detail("not_started", error_type="TimeoutError"), at=seen),
        schedules.line("missed", detail("error", error_type="KeyError"), at=seen),
        schedules.line("missed", detail("approval", due=later, to=later, count=14), at=seen),
        schedules.line("missed", detail("approval", due=later, to=later, count=1), at=seen, held=True),
        schedules.line(
            "rezone", {"from": "2026-10-08T00:00Z", "to": "2026-10-08T01:00Z", "tzdata": "2026c"}, at=seen
        ),
    ]
    assert texts == [
        f"started run {RUN} at its time",
        f"started run {RUN} 12 min after its time",
        f"started by schedule nightly-customers ({SC}) for 2026-10-07 02:00 Europe/London"
        " (2026-10-07T01:00Z), 4 s after its time",
        f"started by schedule nightly-customers ({SC}) for 2026-10-07T01:00Z, 1 h 2 min after its time",
        "not started: its time 2026-10-07T01:00Z passed 7 h 30 min before a worker could start it, over the"
        " 60 min the schedule allows (no worker that fires schedules ran, the store was down, or the clock"
        " moved)",
        "not started: 3 times, 2026-10-05T01:00Z to 2026-10-07T01:00Z, passed while no worker could start"
        " them: a schedule never runs a backlog",
        "not started: its time 2026-10-07T01:00Z passed its 60 min while the worker was still starting other"
        " schedules (two at a time)",
        "not started: a run for 2026-10-07T01:00Z exists already",
        "not started: its time 2026-10-07T01:00Z was due and not started when the schedule was saved again",
        "not started: its time 2026-10-07T01:00Z was due and not started when the schedule was paused",
        "not started: 3 times, 2026-10-05T01:00Z to 2026-10-07T01:00Z, were due and not started when the"
        " schedule was deleted",
        "not started, and paused by the worker: the worker's time zone data (2025b) did not hold"
        " 'Europe/Kyiv': save the schedule with a zone the build holds, then resume it",
        "not started, and paused by the worker: the worker's time zone data did not hold 'Europe/Kyiv': save"
        " the schedule with a zone the build holds, then resume it",
        "not started, and paused by the worker: the worker's build could not read its cron: save it again,"
        " then resume it",
        f"not started: run {RUN} of this schedule was still live when its window closed",
        "not started: the schedule was saved in tokens mode and this worker runs under SSO: save it again,"
        " signed in, to run it",
        "not started: the schedule was saved in SSO and this worker runs under developer mode: save it again"
        " to run it",
        "not started: who saved it is disabled or no longer an operator (for a token, its accountable person"
        " is disabled): another operator saves the schedule again to run it",
        "not started: spec 12 lacked the four-eyes approval it needs (see Approvals)",
        "not started: the licence refused the run (expired): see Licence",
        f"not started: draw {DRAW} of its population did not pass and is not closed: close it with the fix,"
        " then the next firing draws",
        "not started: a live sampled run samples its population",
        "not started: the sample's rules refused the run",
        "not started: the run could not be created (ValueError): starting one on the Runs screen shows the"
        " cause",
        "not started: the run was not created before its window closed (TimeoutError): starting one on the"
        " Runs screen shows the cause",
        "not started: a fault in DeltaSleuth (KeyError): see the server log",
        "not started: 14 more times since 2026-10-08T01:00Z for the same reason",
        "not started: 1 more time since 2026-10-08T01:00Z for the same reason, chained as one row within"
        " 24 h",
        "its times moved with the time zone data (2026c): next due 2026-10-08T01:00Z, was 2026-10-08T00:00Z",
    ]
    # A text is its row's own: it reads the same under whatever tz data its reader has.
    monkeypatch.setattr(cron, "tzdata", lambda: "2027a")
    zoned = detail("zone", "zone", time_zone="Europe/Kyiv", tzdata="2025b")
    assert schedules.line("missed", zoned, at=seen) == texts[11] and "2027a" not in texts[11]
    # A window closes at the next due time when that is sooner than start_within.
    hourly = detail("late", "busy", start_within=120) | {"next": "2026-10-07T01:30Z"}
    assert "passed its 30 min while" in schedules.line("missed", hourly, at=seen)


# -- what no answer tells (§2) -------------------------------------------------------------------------


def test_never_told(open_client: OpenClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """§2 over every answer: a firing is a started run, never a completed one (N1); next due is a schedule
    (N2); no firing names an actor (N3); a missed time reads not started, late starts and ranges counted
    (N4); no percentage, uptime or success rate (N5); no key and no value, a refusal's message included
    (N6); and the list says a schedule is no dead-man's switch (N7)."""
    canary = f"customer {RAW_KEY} has no account"

    async def refusing(*_: Any, **__: Any) -> str:
        raise ValueError(canary)

    with open_client() as client:
        spec_id, late = stored(client), minute(client) - timedelta(minutes=20)
        schedule_id = client.post("/v1/schedules", json=declared(spec_id, late)).json()["schedule_id"]
        path = f"/v1/schedules/{schedule_id}"
        due(client, schedule_id, late)
        with monkeypatch.context() as patched:
            patched.setattr(firing, "create_run", refusing)
            fire(client, schedule_id, tried=firing.TRIES - 1)  # its last try: the refusal's type alone
        due(client, schedule_id, late - timedelta(days=3))
        fire(client, schedule_id)  # three older times as one counted range, then the newest, 20 minutes late
        run_id = sql(client, "SELECT last_run_id FROM ds.schedules")[0]["last_run_id"]
        sql(client, "UPDATE ds.runs SET status = 'failed', finished_at = now() WHERE run_id = %s", run_id)
        paths = ("/v1/schedules", path, f"{path}/firings", "/v1/schedules/zones", f"/v1/runs/{run_id}")
        answers = [client.get(read) for read in (*paths, "/v1/runs", "/v1/audit")]
        answers.append(client.post("/v1/schedules/preview", json={"cron": "0 2 * * *", "time_zone": "UTC"}))
    listed, shown, firings, zones, run = (answer.json() for answer in answers[:5])
    assert [answer.url.path for answer in answers if answer.status_code != 200] == []
    texts = [text for answer in (listed, shown, firings) for text in strings(answer)]
    started, backlog, refused = firings["items"]
    minutes = started["late_s"] // 60  # N4: a late start says how late
    assert minutes in (20, 21) and started["text"] == f"started run {run_id} {minutes} min after its time"
    assert (backlog["count"], backlog["text"]) == (
        3,
        f"not started: 3 times, {backlog['due']} to {backlog['to']}, passed while no worker could start them:"
        " a schedule never runs a backlog",
    )
    assert refused["text"] == (
        "not started: the run could not be created (ValueError): starting one on the Runs screen shows the"
        " cause"
    )
    # N1: a firing's text is the started run's id; how it ended is the run's status, beside it.
    assert run["status"] == "failed" and run["schedule"]["text"].startswith("started by schedule nightly (")
    assert [t for t in texts if "started run" in t and not t.startswith("started run R-")] == []
    last = listed["items"][0]["last"]
    assert (last["text"], last["run_id"], last["run_status"]) == (started["text"], run_id, "failed")
    # N2 and N7: the list says so itself.
    assert "Next due is a schedule, not a forecast." in listed["not"]
    assert any(
        "no alert is sent while no worker runs: a missed time is reported when a worker next sweeps" in line
        for line in listed["not"]
    )
    # N3: no firing names an actor.
    assert all(set(found) == FIRING for found in firings["items"] + shown["firings"])
    assert [t for t in strings(firings) + strings(shown["firings"]) if "system:" in t or "dev:" in t] == []
    # N4: every miss reads not started, and N5: nothing is a rate, an uptime or a forecast.
    assert all(found["text"].startswith("not started") for found in (backlog, refused))
    assert [t for t in texts if t not in schedules.NOT and BANNED.search(t)] == []
    assert [t for t in strings(zones) if BANNED.search(t)] == []
    # N6: no key and no value: the refusal's message is in no answer, the audit log's included.
    assert [
        answer.url.path for answer in answers if RAW_KEY in answer.text or "has no account" in answer.text
    ] == []
