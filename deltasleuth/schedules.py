"""Schedules as people declare, see and steer them: a stored spec,
pinned or followed by name, what a run of it takes but keys, a cron expression, a time zone and a start
window. The API and the CLI call this module; only the worker fires (``runner.schedule``).

Saving one checks what a firing would meet (``create_run``'s mode rules, a key source or two tables, a
sample's refusals, a soak that ends by itself) and answers the four-eyes gate as a warning, never a
refusal. Each write (create, replace, pause, resume, delete) is one transaction with the row locked, audited
as the caller: it first settles what is due (``settle_due``), so a time a person's write left unstarted is
recorded as missed, never dropped. A refusal carries its HTTP status and binding message (``Refused``); the
CLI prints the message. What every surface shows is built here: a schedule's item, the banners over a page
of them, and each firing's text (``line``, the only builder), from the audit rows the firings are (no
actor, address or request data). Nothing here starts a run, and no row, audit detail or text holds a key, a
value or a refusal's message.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import errors

from deltasleuth import approvals
from deltasleuth.auth.principal import WORKER, Principal
from deltasleuth.context import Context
from deltasleuth.core import cron
from deltasleuth.store import repo

LIMIT = 200  # schedules an installation holds, the deleted aside
START_WITHIN = 60  # minutes after its time a run may still start, unless the schedule says
OVERDUE_MIN = 10  # minutes past its time a schedule is not settled: is a worker that fires running?
UPCOMING, FIRINGS, PREVIEW = 5, 20, 10  # the times and firings a schedule's detail lists; a preview's times
NOT = (  # what a schedule is not
    "A firing starts a run and is not a completed run: the run's status says how it ended.",
    "Next due is a schedule, not a forecast.",
    "No person is named as a firing's actor: the worker starts a schedule's runs.",
    "A missed time is never shown as run: a late start says how late, a range of missed times is counted,"
    " and the times a save, pause or delete left unstarted are missed, never dropped.",
    "A schedule has no percentage, uptime or success rate: it is not evidence, readiness or a sign-off.",
    "It holds no key and no value.",
    "It is not a dead-man's switch: no alert is sent while no worker runs: a missed time is reported when a"
    " worker next sweeps.",
)
_NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_ROUTES = ("zones", "preview")  # no schedule's name: /v1/schedules/zones and /preview are the API's own
_SAVED = ("name", "spec_name", "spec_id", "run", "cron", "time_zone", "start_within", "ends_at", "auth_mode")
_ITEM = (
    "schedule_id", "name", "spec_name", "spec_id", "run", "cron", "time_zone", "start_within", "ends_at",
    "state", "auth_mode", "revision", "created_by", "created_by_name", "created_at", "set_by", "set_by_name",
    "set_at", "paused_by", "paused_by_name", "paused_at",
)  # fmt: skip
_MODES = {"oidc": "SSO"} | approvals.MODES  # an auth mode as a firing's text names it
_SUPERSEDED = {"update": "saved again", "pause": "paused", "delete": "deleted"}
_LAST = datetime.max.replace(tzinfo=UTC) - timedelta(days=1)  # an ends_at is before it: no zone reads 10000


class Refused(ValueError):
    """A schedule write refused: its HTTP status and binding message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def missed(
    row: Mapping[str, Any],
    reason: str,
    code: str | None = None,
    *,
    due: datetime,
    to: datetime | None = None,
    count: int | None = None,
    next: datetime | None = None,
    **facts: Any,
) -> dict[str, Any]:
    """A ``schedule.missed`` row's detail for the schedule ``row``: the closed reason and code, the
    time not started (``due``; a range's first, with its last, ``to``, and how many, ``count``), what rules
    match (the spec's name and the mode), the ids ``facts`` name (``run_id``, ``draw_id``) or an exception's
    type (``error_type``), the time due next, and what its text is built from (``line``): the zone, the
    start window and the auth mode the schedule was saved with, for reason ``mode`` the worker's
    (``worker_mode``) and for reason ``zone`` the worker's tz data version (``tzdata``). Never a message, a
    key or a value."""
    return {
        "reason": reason, "code": code, "due": cron.utc(due), "to": to and cron.utc(to), "count": count,
        "spec_name": row["spec_name"], "mode": row["run"]["mode"], "spec_id": row.get("runs_spec_id"),
        "run_id": facts.get("run_id"), "draw_id": facts.get("draw_id"),
        "error_type": facts.get("error_type"), "revision": row["revision"], "time_zone": row["time_zone"],
        "start_within": row["start_within"], "auth_mode": row["auth_mode"],
        "worker_mode": facts.get("worker_mode"), "tzdata": facts.get("tzdata"),
        "next": next and cron.utc(next),
    }  # fmt: skip


def settle_due(
    row: Mapping[str, Any], now: datetime, code: str, next: datetime | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, datetime | None]:
    """What a person's write settles of the locked schedule ``row``: the details it chains before its
    own audit row (a pending ``held`` range, then every due time at or before ``now`` as one ``superseded``
    row of ``code``, counted unless the stored cron or zone does not load), what the row's ``last`` becomes
    (None: nothing was due) and the last time so settled, its anchor."""
    chained = [row["held"]] if row["held"] is not None else []
    if row["next_due"] is None or row["next_due"] > now:
        return chained, None, None
    try:
        until = now if row["ends_at"] is None else min(now, row["ends_at"])
        times = cron.between(row["cron"], row["time_zone"], row["anchor_at"], until)
    except cron.CronError:
        times = []
    if times:
        detail = missed(row, "superseded", code, due=times[0], to=times[-1], count=len(times), next=next)
    else:
        detail = missed(row, "superseded", code, due=row["next_due"], next=next)
    return [*chained, detail], detail | {"outcome": "missed"}, times[-1] if times else None


# -- what every surface shows -----------------------------------------------------------------


def _local(at: datetime, time_zone: str) -> str | None:
    """``at`` on the zone's wall clock (``2026-10-07 02:00``); None when this build cannot load the zone."""
    try:
        return f"{at.astimezone(cron.zone(time_zone)):%Y-%m-%d %H:%M}"
    except cron.CronError:
        return None


def _when(at: datetime | None, time_zone: str) -> dict[str, Any] | None:
    return at and {"utc": cron.utc(at), "local": _local(at, time_zone)}


def _span(seconds: float) -> str:
    """A length of time as a firing's text says it: ``4 s``, ``12 min``, ``7 h 30 min``."""
    hours, minutes = divmod(int(seconds) // 60, 60)
    if not hours:
        return f"{minutes} min" if minutes else f"{int(seconds)} s"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def line(outcome: str, detail: Mapping[str, Any], *, at: datetime | None = None, held: bool = False) -> str:
    """A firing's text, the only builder: what the ``detail`` of a ``schedule.fire``, ``.missed`` or
    ``.rezone`` row says (``outcome``: fire, missed or rezone; ``at``: when the row was written; ``held``: a
    standing refusal's range not yet chained), or what a run says of the schedule that started it
    (``outcome`` run, ``detail`` its ``of_run``). Ids, times, zone names and closed codes only."""
    d = detail
    if outcome == "run":
        named = f"{d['name']} ({d['schedule_id']})" if d["name"] else d["schedule_id"]
        local = _local(datetime.fromisoformat(d["due"]), d["time_zone"])
        when = f"{local} {d['time_zone']} ({d['due']})" if local else d["due"]
        return f"started by schedule {named} for {when}, {_span(d['late_s'])} after its time"
    if outcome == "fire":
        late = "at its time" if d["late_s"] < 60 else f"{_span(d['late_s'])} after its time"
        return f"started run {d['run_id']} {late}"
    if outcome == "rezone":
        return f"its times moved with the time zone data ({d['tzdata']}): next due {d['to']}, was {d['from']}"
    reason, code, due, count = d["reason"], d["code"], d["due"], d["count"]
    if count is not None and reason not in ("late", "superseded"):  # a standing refusal's later times
        chained = ", chained as one row within 24 h" if held else ""
        times = "time" if count == 1 else "times"
        return f"not started: {count:,} more {times} since {due} for the same reason{chained}"
    several = f"{count:,} times, {due} to {d['to']}," if (count or 1) > 1 else None
    within = d["start_within"]  # its window: it closes at the next due time, if that is sooner
    if d["next"] is not None:
        gap = datetime.fromisoformat(d["next"]) - datetime.fromisoformat(due)
        within = min(within, int(gap.total_seconds()) // 60)
    paused = "not started, and paused by the worker"
    match reason, code:
        case "late", "down" if several:
            why = f"{several} passed while no worker could start them: a schedule never runs a backlog"
        case "late", "down":
            passed = _span((at - datetime.fromisoformat(due)).total_seconds())  # type: ignore[operator]
            why = (
                f"its time {due} passed {passed} before a worker could start it, over the {within} min the"
                " schedule allows (no worker that fires schedules ran, the store was down, or the clock"
                " moved)"
            )
        case "late", "busy":
            why = (
                f"its time {due} passed its {within} min while the worker was still starting other"
                " schedules (two at a time)"
            )
        case "late", _:
            why = f"a run for {due} exists already"
        case "superseded", _:
            what = f"{several} were" if several else f"its time {due} was"
            why = f"{what} due and not started when the schedule was {_SUPERSEDED[code]}"
        case "zone", "zone":
            data = f" ({d['tzdata']})" if d.get("tzdata") else ""  # the worker's, as the row holds it
            return (
                f"{paused}: the worker's time zone data{data} did not hold {d['time_zone']!r}: save the"
                " schedule with a zone the build holds, then resume it"
            )
        case "zone", _:
            return f"{paused}: the worker's build could not read its cron: save it again, then resume it"
        case "overlap", _:
            why = f"run {d['run_id']} of this schedule was still live when its window closed"
        case "mode", _:
            signed_in = ", signed in," if d["worker_mode"] == "oidc" else ""
            why = (
                f"the schedule was saved in {_MODES[d['auth_mode']]} and this worker runs under"
                f" {_MODES[d['worker_mode']]}: save it again{signed_in} to run it"
            )
        case "setter", _:
            why = (
                "who saved it is disabled or no longer an operator (for a token, its accountable person is"
                " disabled): another operator saves the schedule again to run it"
            )
        case "approval", _:
            why = f"spec {d['spec_id']} lacked the four-eyes approval it needs (see Approvals)"
        case "licence", _:
            why = f"the licence refused the run ({code}): see Licence"
        case "sample", "unclosed":
            why = (
                f"draw {d['draw_id']} of its population did not pass and is not closed: close it with the"
                " fix, then the next firing draws"
            )
        case "sample", "live":
            why = "a live sampled run samples its population"
        case "sample", _:
            why = "the sample's rules refused the run"
        case "not_started", str():
            # ds target check lists the files and says whether they are settled; it reads no stamp file,
            # takes no maxAge and plans nothing, so for those codes a run's creation is what says the cause.
            shown = "starting a run on the Runs screen shows the cause"
            if code in ("export_absent", "unsettled"):
                shown = "ds target check shows its state"
            why = f"the export a file side reads was not ready before its window closed ({code}): {shown}"
        case "not_started", _:
            created = "could not be created"
            if d["error_type"] == "TimeoutError":
                created = "was not created before its window closed"
            why = f"the run {created} ({d['error_type']}): starting one on the Runs screen shows the cause"
        case _:
            why = f"a fault in DeltaSleuth ({d['error_type']}): see the server log"
    return f"not started: {why}"


def item(row: Mapping[str, Any]) -> dict[str, Any]:
    """A schedule as every surface shows it, from its ``repo.get_schedule`` row alone: what a firing
    would run now (``runs``), the stored version past its pin (``newer``), its next due time in UTC and on
    its zone's clock, whether that is over ``OVERDUE_MIN`` minutes ago and not settled (``overdue``),
    whether the database's clock is behind what it settled (``clock_behind``), its newest firing with its
    text, and the range it holds unchained."""
    now, due, last, held = row["now"], row["next_due"], row["last"], row["held"]
    behind = row["spec_id"] is not None and row["newest_spec_id"] != row["spec_id"]  # pinned, and not newest
    run_id = last and last.get("run_id")
    return {name: row[name] for name in _ITEM} | {
        "runs": {"spec_id": row["runs_spec_id"], "version": row["runs_version"]},
        "newer": {"spec_id": row["newest_spec_id"], "version": row["newest_version"]} if behind else None,
        "next_due": _when(due, row["time_zone"]),
        "overdue": due is not None and due <= now - timedelta(minutes=OVERDUE_MIN),
        "clock_behind": due is not None and now < row["anchor_at"],
        "last": last and {
            "at": row["last_at"], "run_id": run_id,
            "run_status": row["last_run_status"] if run_id == row["last_run_id"] else None,
            "text": line(last["outcome"], last, at=row["last_at"], held=held is not None),
        },
        "held": held and {name: held[name] for name in ("due", "to", "count")},
    }  # fmt: skip


def banners(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """What the Schedules screen says over a page's ``items``: each schedule due and not settled, each
    the worker paused, each pinned behind a stored version, and each the database's clock is behind."""
    lines = []
    for found in items:
        name, due = found["name"], found["next_due"]
        if found["state"] == "deleted":
            continue
        if found["overdue"]:
            lines.append(
                f"schedule {name} was due at {due['utc']} and is not settled yet: a worker may be retrying"
                " or starting it, or none is running (ds worker, or ds serve)"
            )
        if found["paused_by"] == WORKER.actor:
            lines.append(
                f"schedule {name} is paused by the worker: its time zone or cron did not load in the"
                " worker's build: save it with a zone and a cron the build takes, then resume it"
            )
        if (newer := found["newer"]) is not None:
            lines.append(
                f"schedule {name} is pinned to {found['runs']['version']} (spec {found['spec_id']});"
                f" {newer['version']} (spec {newer['spec_id']}) is stored"
            )
        if found["clock_behind"]:
            its = f"{name}'" if name.endswith("s") else f"{name}'s"
            lines.append(
                f"the database clock is behind schedule {its} last firing: nothing fires until it passes"
                f" {due['utc']}"
            )
    return lines


async def overview(
    ctx: Context,
    *,
    spec_name: str | None = None,
    state: str | None = None,
    set_by: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """``GET /v1/schedules`` and ``ds schedules``: what readers are never told (``NOT``), this build's tz
    data, how many schedules are overdue (every one, for monitors), and a page of the schedules, newest
    first, with its banners: one join, no spec compiled."""
    async with ctx.pool.connection() as conn:
        rows, following = await repo.list_schedules(
            conn, spec_name=spec_name, state=state, set_by=set_by, limit=limit, cursor=cursor
        )
        overdue = await repo.overdue_schedules(conn, OVERDUE_MIN)
    items = [item(row) for row in rows]
    return {
        "not": list(NOT), "tzdata": cron.tzdata(), "overdue": overdue, "banners": banners(items),
        "items": items, "next_cursor": following,
    }  # fmt: skip


def zones() -> dict[str, Any]:
    """``GET /v1/schedules/zones``: the zones a schedule takes in this build, and each renamed zone's
    primary."""
    taken = sorted(cron.zones() - set(cron.RENAMED))
    return {"tzdata": cron.tzdata(), "zones": taken, "renamed": cron.RENAMED}


def _noted(text: str, time_zone: str, now: datetime) -> list[str]:
    """What a save warns of and a preview notes besides the times: a zone whose name holds a digit, which
    costs the schedule's runs their evidence pack under some key patterns, and each clock change."""
    numbered = []
    if any(char.isdigit() for char in time_zone):
        numbered.append(
            f"the name of {time_zone!r} holds a number: under a key pattern that matches a short number, the"
            " runs of a schedule in it get no evidence pack: use a region's zone, or UTC"
        )
    return numbered + cron.notes(text, time_zone, now)


def preview(text: str, time_zone: str, now: datetime, count: int = PREVIEW) -> dict[str, Any]:
    """``POST /v1/schedules/preview`` and ``ds schedules next``: the cron's next ``count`` times after
    ``now`` in the zone and each note (``_noted``); refused as ``cron.CronError`` words it. Writes nothing."""
    try:
        text = cron.parse(text).text
        cron.zone(time_zone)
    except cron.CronError as exc:
        raise Refused(422, str(exc)) from None
    times = cron.upcoming(text, time_zone, now, count)
    return {
        "upcoming": [_when(at, time_zone) for at in times],
        "notes": _noted(text, time_zone, now),
        "tzdata": cron.tzdata(),
    }


async def _found(conn: repo.Conn, ident: str) -> repo.Row:
    """The schedule of that id or live name, a tombstone too; refused when there is none."""
    row = await repo.get_schedule(conn, ident)
    if row is None:
        raise Refused(404, f"schedule {ident} not found")
    return row


async def _gate(ctx: Context, conn: repo.Conn, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """What the four-eyes gate says now of the version a firing would run, read unlocked: the scopes
    it needs, the auth mode, its ``state`` (approved, not_needed or unapproved), the approval, and the
    ``refusal`` a firing would meet. None when no version is stored."""
    spec_id = row["runs_spec_id"]
    if spec_id is None:
        return None
    spec = await ctx.spec(conn, spec_id)
    record: dict[str, Any] = {"needs": approvals.needs(spec), "mode": ctx.settings.auth_mode}
    approval_id = refusal = None
    try:
        approval_id, _ = await approvals.gate(ctx, conn, spec_id, spec)
    except approvals.ApprovalRequired as exc:
        refusal = str(exc)
    state = "approved" if approval_id else "unapproved" if refusal or record["needs"] else "not_needed"
    return record | {"state": state, "approval_id": approval_id, "refusal": refusal}


def _firing(row: Mapping[str, Any]) -> dict[str, Any]:
    """A ``schedule.fire``, ``.missed`` or ``.rezone`` row as the firings list shows it: its chain ``seq``
    and time, what its detail holds of ids, times and closed codes, and its text. No actor, address or
    request data."""
    detail, outcome = row["detail"], row["action"].removeprefix("schedule.")
    moved = outcome == "rezone"  # its ``to`` is the next due time, not a range's last
    told = {name: detail.get(name) for name in ("count", "late_s", "run_id", "reason", "code")}
    return {"seq": row["seq"], "at": row["at"], "outcome": outcome} | told | {
        "due": detail.get("due"), "to": None if moved else detail.get("to"),
        "next": detail["to"] if moved else detail.get("next"),
        "text": line(outcome, detail, at=row["at"]),
    }  # fmt: skip


async def described(ctx: Context, ident: str) -> dict[str, Any]:
    """``GET /v1/schedules/{id}`` and ``ds schedules show``: the schedule's item with the gate's state for
    what it would run (``runs.approval``, read here only), its next ``UPCOMING`` times after now (a time
    due and not settled is the item's ``next_due``, never one of them) and its newest ``FIRINGS`` firings."""
    async with ctx.pool.connection() as conn:
        row = await _found(conn, ident)
        found = item(row)
        found["runs"]["approval"] = await _gate(ctx, conn, row)
        rows, _ = await repo.schedule_firings(conn, row["schedule_id"], limit=FIRINGS)
    times: list[datetime] = []
    if row["state"] == "active":
        try:
            after = max(row["now"], row["anchor_at"])
            times = cron.upcoming(row["cron"], row["time_zone"], after, UPCOMING)
        except cron.CronError:  # the worker pauses it
            times = []
    ends = row["ends_at"]
    upcoming = [_when(at, row["time_zone"]) for at in times if ends is None or at <= ends]
    return found | {"upcoming": upcoming, "firings": [_firing(fired) for fired in rows]}


async def firings(
    ctx: Context, ident: str, *, limit: int = FIRINGS, cursor: str | None = None
) -> dict[str, Any]:
    """``GET /v1/schedules/{id}/firings``: the schedule's firings, newest first: projections of its audit
    rows, so reading them is not reading the audit log."""
    async with ctx.pool.connection() as conn:
        row = await _found(conn, ident)
        try:
            rows, following = await repo.schedule_firings(
                conn, row["schedule_id"], limit=limit, cursor=cursor
            )
        except ValueError:
            raise Refused(400, "malformed cursor") from None
    return {"items": [_firing(fired) for fired in rows], "next_cursor": following}


async def of_run(conn: repo.Conn, run: Mapping[str, Any]) -> dict[str, Any] | None:
    """A run's ``schedule``: the schedule that started it, the due time it was started for, in the
    zone in force at the firing, how many seconds after it the run was created, and its text. None for a
    run no schedule started."""
    fired = run["params"].get("schedule")
    if fired is None:
        return None
    row = await repo.get_schedule(conn, fired["schedule_id"])
    late = run["created_at"] - datetime.fromisoformat(fired["due"])
    found = {
        "schedule_id": fired["schedule_id"], "name": row and row["name"], "due": fired["due"],
        "time_zone": fired["time_zone"], "late_s": max(0, int(late.total_seconds())),
    }  # fmt: skip
    return found | {"text": line("run", found)}


# -- the writes: each one audited transaction ------------------------------------------------------


def _ends(value: Any, now: datetime) -> datetime | None:
    """``ends_at``: an optional instant with an offset, later than ``now``, that the store holds: one whose
    time in UTC, or in the store's own zone, is past the year 9999 is none."""
    if value is None:
        return None
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else value
        taken = isinstance(at, datetime) and at.tzinfo is not None and now < at.astimezone(UTC) < _LAST
    except (ValueError, OverflowError):
        taken = False
    if not taken:
        raise Refused(422, "ends_at must be an ISO-8601 time with a UTC offset, later than now")
    return at


async def _checked(
    ctx: Context, conn: repo.Conn, body: Mapping[str, Any], now: datetime
) -> tuple[dict[str, Any], list[str]]:
    """The schedule ``body`` declares (name, spec_id or spec_name, run, cron, time_zone, start_within,
    ends_at) as it is stored, and the warnings of saving it: the gate's, read unlocked, and each note
    (``_noted``). A refusal is ``Refused``."""
    from deltasleuth.runner import runs, sample  # the runner's modules import this one
    from deltasleuth.runner.stop import StopConditions

    name, spec_id, spec_name = body.get("name"), body.get("spec_id"), body.get("spec_name")
    if not isinstance(name, str) or len(name) > 63 or not _NAME.fullmatch(name):
        raise Refused(
            422,
            "name must be a slug of 1 to 63 characters (lower-case letters, digits and single hyphens,"
            " starting with a letter)",
        )
    if name in _ROUTES:
        raise Refused(422, f"name must not be {name}: /v1/schedules/{name} is a route of the API's own")
    if (spec_id is None) == (spec_name is None):
        raise Refused(
            422,
            "give spec_id (a pinned version) or spec_name (the newest version at each firing), not both",
        )
    if spec_id is not None:
        stored = await repo.get_spec(conn, spec_id)
    else:
        stored = await repo.newest_spec(conn, spec_name)  # type: ignore[arg-type]
    if stored is None:
        missing = f"spec {spec_id} not found" if spec_id is not None else f"no spec is named {spec_name}"
        raise Refused(404, missing)
    spec = await ctx.spec(conn, stored["spec_id"])
    named = f"spec {stored['spec_id']} ({spec.name} {spec.version})"
    run = body.get("run") or {}
    mode, soak = run.get("mode"), run.get("soak")
    if mode not in ("batch", "soak") or (soak is not None and mode != "soak"):
        raise Refused(422, runs.MODE)
    if mode == "soak" and "cycleInterval" not in spec.raw.get("execution", {}):
        raise Refused(422, runs.INTERVAL)
    if "source" not in spec.raw["key"] and sum(s.table is not None for s in spec.sides.values()) != 2:
        raise Refused(
            422,
            f"{named} has no key source and is not a pair of tables: a schedule's runs need one of them",
        )
    if spec.sample is not None:
        try:
            sample.refuse(mode, None, None, soak)
        except sample.SampleRefused as exc:
            raise Refused(exc.status, str(exc)) from None
    stop = StopConditions.from_spec(spec.raw)
    if mode == "soak" and stop.max_cycles is None and stop.max_duration_s is None and stop.until is None:
        raise Refused(
            422,
            f"a scheduled soak must end by itself: {named} needs execution.stop.maxCycles, maxDuration or"
            " until, or every later firing is skipped while it runs",
        )
    within = body.get("start_within", START_WITHIN)
    if isinstance(within, bool) or not isinstance(within, int) or not 15 <= within <= 1440:
        raise Refused(422, "start_within must be 15 to 1440 minutes")
    time_zone = str(body.get("time_zone", ""))
    try:
        text = cron.parse(str(body.get("cron", ""))).text
        cron.zone(time_zone)
    except cron.CronError as exc:
        raise Refused(422, str(exc)) from None
    schedule = {
        "name": name, "spec_name": spec.name, "spec_id": spec_id,
        "run": {"mode": mode} | ({} if soak is None else {"soak": dict(soak)}), "cron": text,
        "time_zone": time_zone, "start_within": within, "ends_at": _ends(body.get("ends_at"), now),
        "auth_mode": ctx.settings.auth_mode,
    }  # fmt: skip
    warnings = []
    try:
        await approvals.gate(ctx, conn, stored["spec_id"], spec)
        if (said := approvals.MODES.get(ctx.settings.auth_mode)) and approvals.needs(spec):
            warnings.append(f"{said} has no approvers: its runs are labelled unapproved")
    except approvals.ApprovalRequired as exc:
        warnings.append(f"{exc}: its firings are not started until it is approved")
    return schedule, warnings + _noted(text, time_zone, now)


def _next(schedule: Mapping[str, Any], anchor: datetime) -> datetime:
    """The first time of the schedule after ``anchor``; refused when ``ends_at`` leaves none."""
    at = cron.after(schedule["cron"], schedule["time_zone"], anchor, until=schedule["ends_at"])
    if at is None:
        raise Refused(422, _ended(schedule))
    return at


def _ended(schedule: Mapping[str, Any]) -> str:
    return f"the schedule has no time before its ends_at ({cron.utc(schedule['ends_at'])})"


def _saved(row: Mapping[str, Any]) -> dict[str, Any]:
    """What ``schedule.create`` and ``schedule.update`` record of the stored row."""
    ends_at, next_due = row["ends_at"], row["next_due"]
    return {name: row[name] for name in _SAVED} | {
        "ends_at": ends_at and cron.utc(ends_at),
        "revision": row["revision"],
        "next_due": next_due and cron.utc(next_due),
    }


async def _taken(conn: repo.Conn, name: str) -> None:
    """Refuse a ``name`` that a schedule not deleted holds."""
    if (other := await repo.get_schedule(conn, name)) is not None:
        raise Refused(409, f"a schedule named {name} exists already ({other['schedule_id']})")


async def _locked(conn: repo.Conn, ident: str) -> repo.Row:
    """The schedule of that id or live name, its row locked until the transaction ends; refused when
    there is none, or it is deleted."""
    row = await repo.get_schedule(conn, ident, lock=True)
    if row is None:
        raise Refused(404, f"schedule {ident} not found")
    if row["state"] == "deleted":
        raise Refused(409, f"schedule {ident} is deleted")
    return row


async def create(ctx: Context, body: Mapping[str, Any], who: Principal) -> tuple[repo.Row, list[str]]:
    """Store the schedule ``body`` declares, active and anchored now, audited ``schedule.create``: the
    schedule and the warnings."""
    name = body.get("name")
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            counted = await repo.count_schedules(conn, lock=True)  # creates count one at a time
            now = counted["now"]
            schedule, warnings = await _checked(ctx, conn, body, now)
            await _taken(conn, schedule["name"])
            if counted["count"] >= LIMIT:
                raise Refused(
                    409, f"this installation holds {LIMIT} schedules, the most it takes: delete one first"
                )
            schedule |= {"schedule_id": repo.new_schedule_id(), "created_by": who.actor, "anchor_at": now}
            row = await repo.insert_schedule(conn, schedule | {"next_due": _next(schedule, now)})
            shown = await repo.get_schedule(conn, row["schedule_id"])
            await audit(who, "schedule.create", row["schedule_id"], _saved(row))
    except errors.UniqueViolation:  # another create took the name meanwhile
        async with ctx.pool.connection() as conn:
            await _taken(conn, str(name))
        raise
    return shown, warnings  # type: ignore[return-value]


async def replace(
    ctx: Context, ident: str, body: Mapping[str, Any], who: Principal
) -> tuple[repo.Row, list[str]]:
    """Replace the schedule's definition with ``body`` (a schedule's members and the ``revision`` it was
    loaded at), set by ``who`` in this auth mode and anchored now, never before a settled time; a paused
    one stays paused. What was due is settled first; audited ``schedule.update``."""
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            row = await _locked(conn, ident)
            schedule_id, now = row["schedule_id"], row["now"]
            if body.get("revision") != row["revision"]:
                raise Refused(
                    409,
                    f"schedule {schedule_id} changed since you loaded it (revision {row['revision']}): reload"
                    " and edit again",
                )
            schedule, warnings = await _checked(ctx, conn, body, now)
            if schedule["name"] != row["name"]:
                await _taken(conn, schedule["name"])
            anchor = max(now, row["anchor_at"])
            next_due = _next(schedule, anchor)  # refused when the body leaves no time
            next_due = None if row["state"] == "paused" else next_due
            chained, last, _ = settle_due(row, now, "update", next_due)
            schedule |= {"anchor_at": anchor, "next_due": next_due}
            saved = await repo.replace_schedule(
                conn, schedule_id, row["revision"], schedule, by=who.actor, last=last
            )
            assert saved is not None  # its row is locked
            shown = await repo.get_schedule(conn, schedule_id)
            for detail in chained:
                await audit(who, "schedule.missed", schedule_id, detail)
            changed = [name for name in _SAVED if saved[name] != row[name]]
            await audit(who, "schedule.update", schedule_id, _saved(saved) | {"changed": changed})
    except errors.UniqueViolation:
        async with ctx.pool.connection() as conn:
            await _taken(conn, str(body.get("name")))
        raise
    return shown, warnings  # type: ignore[return-value]


async def pause(ctx: Context, ident: str, who: Principal) -> repo.Row:
    """Pause the active schedule: what was due is settled, and nothing is due until it is resumed; audited
    ``schedule.pause``."""
    async with repo.audited(ctx.pool) as (conn, audit):
        row = await _locked(conn, ident)
        schedule_id = row["schedule_id"]
        if row["state"] != "active":
            said = "is paused already" if row["state"] == "paused" else "has ended"
            raise Refused(409, f"schedule {schedule_id} {said}")
        chained, last, anchor = settle_due(row, row["now"], "pause")
        saved = await repo.pause_schedule(
            conn, schedule_id, who.actor, revision=row["revision"] + 1, anchor=anchor, last=last
        )
        assert saved is not None  # its row is locked
        shown = await repo.get_schedule(conn, schedule_id)
        for detail in chained:
            await audit(who, "schedule.missed", schedule_id, detail)
        await audit(who, "schedule.pause", schedule_id, {"revision": saved["revision"], "next_due": None})
    return shown  # type: ignore[return-value]


async def pause_all(ctx: Context, who: Principal) -> int:
    """Pause every active schedule, each as ``pause`` pauses one (``ds schedules pause --all``, before a
    worker starts on a restored or copied database): how many."""
    async with ctx.pool.connection() as conn:
        rows = await repo.pages(repo.list_schedules, conn, state="active")
    paused = 0
    for row in rows:
        try:
            await pause(ctx, row["schedule_id"], who)
        except Refused:  # paused, ended or deleted meanwhile
            continue
        paused += 1
    return paused


async def resume(ctx: Context, ident: str, who: Principal) -> tuple[repo.Row, list[str]]:
    """Resume the paused schedule, its cron and zone validated again, anchored now: the times of its pause
    are neither due nor missed. With no time left before ``ends_at`` it has ended, which the warning says;
    audited ``schedule.resume``."""
    async with repo.audited(ctx.pool) as (conn, audit):
        row = await _locked(conn, ident)
        schedule_id = row["schedule_id"]
        if row["state"] != "paused":
            raise Refused(409, f"schedule {schedule_id} is not paused")
        try:
            cron.parse(row["cron"])
            cron.zone(row["time_zone"])
        except cron.CronError as exc:
            raise Refused(422, str(exc)) from None
        anchor = max(row["now"], row["anchor_at"])
        next_due = cron.after(row["cron"], row["time_zone"], anchor, until=row["ends_at"])
        saved = await repo.resume_schedule(conn, schedule_id, anchor_at=anchor, next_due=next_due)
        assert saved is not None  # its row is locked
        shown = await repo.get_schedule(conn, schedule_id)
        detail = {"revision": saved["revision"], "next_due": next_due and cron.utc(next_due)}
        await audit(who, "schedule.resume", schedule_id, detail)
    return shown, [] if next_due is not None else [_ended(row)]  # type: ignore[return-value]


async def delete(ctx: Context, ident: str, who: Principal) -> repo.Row:
    """Delete the schedule: what was due is settled, and its tombstone stays, so its runs still name it and
    its name is free; audited ``schedule.delete``."""
    async with repo.audited(ctx.pool) as (conn, audit):
        row = await _locked(conn, ident)
        schedule_id = row["schedule_id"]
        chained, last, anchor = settle_due(row, row["now"], "delete")
        saved = await repo.delete_schedule(conn, schedule_id, who.actor, anchor=anchor, last=last)
        assert saved is not None  # its row is locked
        for detail in chained:
            await audit(who, "schedule.missed", schedule_id, detail)
        await audit(who, "schedule.delete", schedule_id, {"revision": saved["revision"], "next_due": None})
    return saved
