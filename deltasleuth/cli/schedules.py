"""``ds schedules``: cron schedules."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import ConfigOpt, _a, _at, _errors, _fail, _in_context, _json, _local, _named, _o

if TYPE_CHECKING:
    from deltasleuth.context import Context
    from deltasleuth.store import repo


schedules_app = typer.Typer(invoke_without_command=True)


ScheduleArg = Annotated[str, _a("The schedule: its name, or its id (SC-…).")]
CRON = "When its runs start: minute hour day-of-month month day-of-week."
ZONE = "The zone of those times: UTC, or an IANA region name such as Europe/London."
PIN = "Run this stored spec version."
FOLLOW = "Run this spec name's newest stored version at each firing."
WITHIN = "Minutes after its time a run may still start: 15 to 1440."
ENDS = (
    "No time is due after this ISO-8601 time with a UTC offset; a run for the last time before it may still"
    " start within its window."
)
# The options' defaults: deltasleuth.schedules' START_WITHIN and PREVIEW, which a test pins. Importing that
# module here would load the store's driver for every command.
START_WITHIN, PREVIEW = 60, 10


def _next_due(found: dict[str, Any]) -> str:
    """A schedule's next due time on its zone's clock and in UTC; its state when none is due."""
    due = found["next_due"]
    if due is None:
        return found["state"]
    return (
        f"next {due['local']} {found['time_zone']} ({due['utc']})" if due["local"] else f"next {due['utc']}"
    )


def _schedule_line(found: dict[str, Any]) -> str:
    """A schedule in one line: what it runs, when, its state or next due time, and its newest firing with
    its run's status."""
    version = f"{found['runs']['version']} (spec {found['runs']['spec_id']})"
    pinned = found["spec_id"] is not None
    spec = f"pinned {version}" if pinned else f"follows {found['spec_name']}: {version} now"
    line = f"{found['schedule_id']}  {found['name']}  {spec}  {found['run']['mode']}"
    line += f"  {found['cron']} {found['time_zone']}  {_next_due(found)}"
    if (last := found["last"]) is not None:
        line += f"  last: {last['text']}" + (f" ({last['run_status']})" if last["run_status"] else "")
    return line


def _run_options(soak: bool, rolling: bool) -> dict[str, Any]:
    return {"mode": "soak" if soak else "batch"} | ({"soak": {"rolling": True}} if rolling else {})


def _saved(saved: tuple[repo.Row, list[str]], said: str) -> None:
    """Print a write's warnings on stderr, then what it did and the time due next."""
    from deltasleuth import schedules

    row, warnings = saved
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    typer.echo(f"{said}: {_next_due(schedules.item(row))}")


@schedules_app.callback()
def schedules_list(
    command: typer.Context,
    spec_name: Annotated[str | None, _o("Only this spec name's schedules.", "--spec")] = None,
    state: Annotated[str | None, _o("Only those active, paused, ended or deleted.")] = None,
    set_by: Annotated[str | None, _o("Only those this actor saved last (user:U-…).", "--set-by")] = None,
    as_json: Annotated[bool, _o("Print GET /v1/schedules's body, every schedule in it.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the schedules, each in one line with its next due time and its newest firing, under what a
    schedule never tells and the banners; or show one, preview a cron, add, set, pause, resume or delete
    one. A schedule starts runs of a stored spec at cron times in a time zone; only a worker fires (ds
    worker, or ds serve)."""
    if command.invoked_subcommand is not None:
        if as_json or any(option is not None for option in (spec_name, state, set_by, config)):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    from deltasleuth import schedules

    if state is not None and state not in ("active", "paused", "ended", "deleted"):
        _fail("--state is one of active, paused, ended, deleted")

    async def work(ctx: Context) -> dict[str, Any]:
        asked = partial(schedules.overview, ctx, spec_name=spec_name, state=state, set_by=set_by, limit=500)
        found = page = await asked()
        while page["next_cursor"] is not None:
            page = await asked(cursor=page["next_cursor"])
            found["items"] += page["items"]
            found["banners"] += page["banners"]
        return found | {"next_cursor": None}

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    for line in (*found["not"], *found["banners"]):
        typer.echo(line)
    if not found["items"]:
        typer.echo("no schedules")
    for item in found["items"]:
        typer.echo(_schedule_line(item))


@schedules_app.command("show")
def schedules_show(
    schedule: ScheduleArg,
    as_json: Annotated[bool, _o("Print GET /v1/schedules/SC-…'s body.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show one schedule: its definition, who saved it, what the four-eyes gate says of the version it would
    run, its next five times and its newest 20 firings."""
    from deltasleuth import schedules

    found = _in_context(config, lambda ctx: schedules.described(ctx, schedule))
    if as_json:
        typer.echo(_json(found))
        return
    typer.echo(_schedule_line(found))
    by = _named(found["set_by"], found["set_by_name"])
    ends = f"; ends {_at(found['ends_at'])}" if found["ends_at"] else ""
    typer.echo(
        f"revision {found['revision']}, saved by {by} {_at(found['set_at'])} ({found['auth_mode']}); starts"
        f" within {found['start_within']} min{ends}"
    )
    if found["paused_at"] is not None:
        typer.echo(
            f"paused by {_named(found['paused_by'], found['paused_by_name'])} {_at(found['paused_at'])}"
        )
    if (gate := found["runs"]["approval"]) is not None:
        typer.echo(f"approval: {gate['state']}" + (f": {gate['refusal']}" if gate["refusal"] else ""))
    for due in found["upcoming"]:
        typer.echo(f"due {due['local']} {found['time_zone']} ({due['utc']})")
    for fired in found["firings"]:
        typer.echo(f"{_at(fired['at'])}  {fired['text']}")


@schedules_app.command("next")
def schedules_next(
    cron: Annotated[str, _a("A cron expression: minute hour day-of-month month day-of-week.")],
    time_zone: Annotated[str, _o(ZONE, "--tz")],
    count: Annotated[int, typer.Option(min=1, max=100, help="How many times.")] = PREVIEW,
) -> None:
    """Print a cron's next times in a zone, as this build's tz data gives them, and warn of each time a
    clock change moves and of a zone whose name holds a number; without settings or a database."""
    from deltasleuth import schedules

    with _errors():
        found = schedules.preview(cron, time_zone, datetime.now(UTC), count)
    for due in found["upcoming"]:
        typer.echo(f"{due['local']} {time_zone} ({due['utc']})")
    for note in found["notes"]:
        typer.echo(f"warning: {note}", err=True)


@schedules_app.command("add")
def schedules_add(
    name: Annotated[str, _a("Its name: a slug, unique among the schedules.")],
    cron: Annotated[str, _o(CRON)],
    time_zone: Annotated[str, _o(ZONE, "--tz")],
    spec_id: Annotated[int | None, _o(PIN, "--spec-id")] = None,
    spec_name: Annotated[str | None, _o(FOLLOW, "--spec")] = None,
    soak: Annotated[bool, _o("Start soak runs, not batch runs.", "--soak")] = False,
    rolling: Annotated[bool, _o("A soak resolving its keys again each cycle.", "--rolling")] = False,
    start_within: Annotated[int, _o(WITHIN, "--start-within")] = START_WITHIN,
    ends: Annotated[str | None, _o(ENDS)] = None,
    config: ConfigOpt = None,
) -> None:
    """Declare a schedule of a stored spec, pinned to a version or following its name; its first run starts
    at its next time."""
    from deltasleuth import schedules

    who = _local(config)
    body = {"name": name, "spec_id": spec_id, "spec_name": spec_name, "run": _run_options(soak, rolling)}
    body |= {"cron": cron, "time_zone": time_zone, "start_within": start_within, "ends_at": ends}
    saved = _in_context(config, lambda ctx: schedules.create(ctx, body, who))
    _saved(saved, f"added {saved[0]['schedule_id']} ({saved[0]['name']})")


@schedules_app.command("set")
def schedules_set(
    schedule: ScheduleArg,
    cron: Annotated[str | None, _o(CRON)] = None,
    time_zone: Annotated[str | None, _o(ZONE, "--tz")] = None,
    spec_id: Annotated[int | None, _o(PIN, "--spec-id")] = None,
    spec_name: Annotated[str | None, _o(FOLLOW, "--spec")] = None,
    soak: Annotated[bool | None, _o("Start soak runs, or batch runs.", "--soak/--batch")] = None,
    rolling: Annotated[bool, _o("A soak resolving its keys again each cycle.", "--rolling")] = False,
    start_within: Annotated[int | None, _o(WITHIN, "--start-within")] = None,
    ends: Annotated[str | None, _o(ENDS)] = None,
    config: ConfigOpt = None,
) -> None:
    """Save a schedule again with the options given, the rest as stored: a time due and not started is
    recorded as missed first, and a paused schedule stays paused."""
    from deltasleuth import schedules
    from deltasleuth.store import repo

    who = _local(config)

    async def work(ctx: Context) -> tuple[repo.Row, list[str]]:
        async with ctx.pool.connection() as conn:
            row = await repo.get_schedule(conn, schedule)
        if row is None:
            raise schedules.Refused(404, f"schedule {schedule} not found")
        kept = ("name", "run", "cron", "time_zone", "start_within", "ends_at", "revision")
        body = {name: row[name] for name in kept}
        body |= {"spec_name": row["spec_name"]} if row["spec_id"] is None else {"spec_id": row["spec_id"]}
        if spec_id is not None or spec_name is not None:
            body |= {"spec_id": spec_id, "spec_name": spec_name}
        if soak is not None or rolling:
            body["run"] = _run_options(row["run"]["mode"] == "soak" if soak is None else soak, rolling)
        given = {"cron": cron, "time_zone": time_zone, "start_within": start_within, "ends_at": ends}
        body |= {name: value for name, value in given.items() if value is not None}
        return await schedules.replace(ctx, row["schedule_id"], body, who)

    saved = _in_context(config, work)
    _saved(saved, f"saved {saved[0]['schedule_id']}")


@schedules_app.command("pause")
def schedules_pause(
    schedule: Annotated[str | None, _a("The schedule: its name, or its id (SC-…).")] = None,
    every: Annotated[bool, _o("Pause every active schedule.", "--all")] = False,
    config: ConfigOpt = None,
) -> None:
    """Pause a schedule, or every active one (before a worker starts on a restored or copied database):
    nothing is due until it is resumed, and a time due now is recorded as missed."""
    from deltasleuth import schedules

    if (schedule is None) != every:
        _fail("name a schedule, or pass --all")
    who = _local(config)
    if schedule is None:
        paused = _in_context(config, lambda ctx: schedules.pause_all(ctx, who))
        typer.echo(
            f"paused {paused} schedule{'' if paused == 1 else 's'}" if paused else "no active schedules"
        )
        return
    row = _in_context(config, lambda ctx: schedules.pause(ctx, schedule, who))
    typer.echo(f"paused {row['schedule_id']}")


@schedules_app.command("resume")
def schedules_resume(schedule: ScheduleArg, config: ConfigOpt = None) -> None:
    """Resume a paused schedule from now: the times of its pause are neither due nor missed."""
    from deltasleuth import schedules

    who = _local(config)
    saved = _in_context(config, lambda ctx: schedules.resume(ctx, schedule, who))
    _saved(saved, f"resumed {saved[0]['schedule_id']}")


@schedules_app.command("delete")
def schedules_delete(schedule: ScheduleArg, config: ConfigOpt = None) -> None:
    """Delete a schedule: its name is free again, and its runs and firings still name it."""
    from deltasleuth import schedules

    who = _local(config)
    row = _in_context(config, lambda ctx: schedules.delete(ctx, schedule, who))
    typer.echo(f"deleted {row['schedule_id']}")
