"""Schedules (docs/mvp/SCHEDULES.md §10): the schedules with their banners, the zones a schedule takes, one
schedule with its next times and its firings, a preview of a cron's times, and the writes, each audited as
its caller: create, replace, pause, resume and delete (operators; a token or developer mode too). A viewer
reads everything. Firings are projections of the worker's audit rows (ids, times and closed codes), so
reading them is not reading the audit log. No route here calls the licence, and none starts a run: the
worker fires."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import Field

from deltasleuth import schedules
from deltasleuth.api.deps import Ctx, Limit, Operator, Payload, Viewer, ok
from deltasleuth.api.routes.runs import RunOptions
from deltasleuth.store import repo

router = APIRouter(prefix="/schedules")
CRON, ZONE = 1000, 64  # the longest texts a body's cron (C9 judges it once trimmed) and time_zone may be


class ScheduleIn(Payload):
    name: str
    spec_id: int | None = None  # the pinned version
    spec_name: str | None = None  # or the name followed: its newest version at each firing
    run: RunOptions
    cron: str = Field(max_length=CRON)
    time_zone: str = Field(max_length=ZONE)
    start_within: int = schedules.START_WITHIN
    ends_at: str | None = None  # ISO-8601 with a UTC offset


class ScheduleSet(ScheduleIn):
    revision: int  # as it was loaded


class PreviewIn(Payload):
    cron: str = Field(max_length=CRON)
    time_zone: str = Field(max_length=ZONE)


@router.get("")
async def overview(
    ctx: Ctx,
    who: Viewer,
    spec_name: str | None = None,
    state: Literal["active", "paused", "ended", "deleted"] | None = None,
    set_by: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> Response:
    """The schedules, newest first (the deleted only when asked for), under what readers are never told,
    with the banners and how many are overdue."""
    found = await schedules.overview(
        ctx, spec_name=spec_name, state=state, set_by=set_by, limit=limit, cursor=cursor
    )
    return ok(found)


@router.get("/zones")
async def zones(who: Viewer) -> Response:
    """The time zones a schedule takes in this build, and the renamed ones' primaries."""
    return ok(schedules.zones())


@router.post("/preview")
async def preview(ctx: Ctx, who: Operator, body: PreviewIn) -> Response:
    """A cron's next ten times in a zone and its notes (its zone's name, a clock change); writes nothing."""
    async with ctx.pool.connection() as conn:
        now = (await repo.count_schedules(conn))["now"]
    return ok(schedules.preview(body.cron, body.time_zone, now))


@router.post("", status_code=201)
async def create(ctx: Ctx, who: Operator, body: ScheduleIn) -> Response:
    row, warnings = await schedules.create(ctx, body.model_dump(), who)
    return ok(schedules.item(row) | {"warnings": warnings}, 201)


@router.get("/{schedule_id}")
async def schedule(ctx: Ctx, who: Viewer, schedule_id: str) -> Response:
    """The schedule with the gate's state for what it would run, its next five times and its newest 20
    firings."""
    return ok(await schedules.described(ctx, schedule_id))


@router.get("/{schedule_id}/firings")
async def firings(
    ctx: Ctx, who: Viewer, schedule_id: str, limit: Limit = schedules.FIRINGS, cursor: str | None = None
) -> Response:
    """Its started runs, missed times and moved times, newest first: no actor, address or request data."""
    return ok(await schedules.firings(ctx, schedule_id, limit=limit, cursor=cursor))


@router.put("/{schedule_id}")
async def replace(ctx: Ctx, who: Operator, schedule_id: str, body: ScheduleSet) -> Response:
    """Replace its definition, as loaded at ``revision``; what was due is recorded as missed first."""
    row, warnings = await schedules.replace(ctx, schedule_id, body.model_dump(), who)
    return ok(schedules.item(row) | {"warnings": warnings})


@router.post("/{schedule_id}/pause")
async def pause(ctx: Ctx, who: Operator, schedule_id: str) -> Response:
    return ok(schedules.item(await schedules.pause(ctx, schedule_id, who)))


@router.post("/{schedule_id}/resume")
async def resume(ctx: Ctx, who: Operator, schedule_id: str) -> Response:
    row, warnings = await schedules.resume(ctx, schedule_id, who)
    return ok(schedules.item(row) | {"warnings": warnings})


@router.delete("/{schedule_id}")
async def delete(ctx: Ctx, who: Operator, schedule_id: str) -> Response:
    """Leave its tombstone: its name is free, and its runs still name it."""
    row = await schedules.delete(ctx, schedule_id, who)
    return ok({"schedule_id": row["schedule_id"], "deleted_at": row["deleted_at"]})
