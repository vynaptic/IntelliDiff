"""Alerts and notifications (docs/mvp/ALERTS.md §11): the channels' state and the banners every screen shows,
the deliveries, and the only runtime acts, each audited: a test send and sending dead deliveries again
(operators), and mutes (admins; a mute needs a sign-in at most 15 minutes old). Channels and rules live only
in the settings: no route creates a destination. Every POST answers 409 while notifications are off.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from deltasleuth import notifications
from deltasleuth.api.deps import Admin, Ctx, Limit, Operator, Payload, Viewer, ok, page, require_fresh
from deltasleuth.auth.principal import Principal
from deltasleuth.store import repo

router = APIRouter(prefix="/notifications")
FreshAdmin = Annotated[Principal, Depends(require_fresh("admin"))]  # signed in within 15 minutes


async def _on(ctx: Ctx) -> None:
    """409 while notifications are off, before a body is judged (docs/mvp/ALERTS.md §10's order)."""
    notifications.on(ctx.settings)


On = Annotated[None, Depends(_on)]


class RetryIn(Payload):
    since: str | None = None  # ISO-8601 with a zone; 72 hours ago by default


class MuteIn(Payload):
    scope: Literal["channel", "run"]
    target: str
    hours: int
    reason: str


@router.get("")
async def status(ctx: Ctx, who: Viewer) -> Response:
    """Whether notifications are on, the feed's cursor, each channel with its counts, the rules, the active
    mutes and the banners; never a URL, path, address or secret."""
    return ok(await notifications.status(ctx))


@router.get("/deliveries")
async def deliveries(
    ctx: Ctx,
    who: Viewer,
    channel: str | None = None,
    state: Literal["pending", "sent", "dead", "suppressed"] | None = None,
    event: str | None = None,
    run_id: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> Response:
    """Newest first, each with its stored body (an e-mail's without addresses) and its SHA-256."""
    async with ctx.pool.connection() as conn:
        return await page(
            repo.list_deliveries(
                conn, channel=channel, state=state, event=event, run_id=run_id, limit=limit, cursor=cursor
            )
        )


@router.post("/channels/{channel}/test")
async def test(request: Request, ctx: Ctx, who: Operator, channel: str) -> Response:
    """One attempt now, outside the queue, stored as sent or dead; one per channel in 10 seconds."""
    notifications.declared(notifications.on(ctx.settings), channel)
    if request.app.state.notify_tests.hit(channel) is None:
        raise HTTPException(429, f"a test of channel {channel} was sent less than 10 s ago")
    return ok(await notifications.test(ctx, channel, who))


@router.post("/channels/{channel}/retry")
async def retry(ctx: Ctx, who: Operator, _: On, channel: str, body: RetryIn | None = None) -> Response:
    """The channel's deliveries that died since ``since``, tests aside, pending again: how many."""
    count, _ = await notifications.retry(ctx, channel, body and body.since, who)
    return ok({"count": count})


@router.post("/mutes", status_code=201)
async def mute(ctx: Ctx, who: FreshAdmin, _: On, body: MuteIn) -> Response:
    row = await notifications.mute(ctx, body.scope, body.target, body.hours, body.reason, who)
    return ok(row, 201)


@router.post("/mutes/{mute_id}/end")
async def unmute(ctx: Ctx, who: Admin, mute_id: str) -> Response:
    await notifications.unmute(ctx, mute_id, who)
    return ok({"mute_id": mute_id})
