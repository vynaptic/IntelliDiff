"""Cut-over readiness: every pair's gates and its place on the ladder, a pair's
detail, trend and brief, the transitions the worker recorded, and the readiness reports. A viewer
reads everything and nothing here writes readiness (the gates are the host administrator's settings); only
a report download is audited. Transitions and ``as_of`` are projections of the worker's rows (ids, states,
counts and slugs), so reading them is not reading the audit log. No route answers 410: a purged cycle keeps
its measures."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from deltasleuth import readiness
from deltasleuth.api.deps import Ctx, Viewer, ok
from deltasleuth.core.readiness import config_sha256
from deltasleuth.store import repo

router = APIRouter(prefix="/readiness")


@router.get("")
async def overview(
    ctx: Ctx,
    who: Viewer,
    programme: str | None = None,
    entity: str | None = None,
    state: str | None = None,
    gate: str | None = None,
    closed: str | None = None,
    q: str | None = None,
    as_of: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
) -> Response:
    """The pairs, worst first, with each programme's and entity type's roll-up; ``as_of``: as recorded."""
    found = await readiness.overview(
        ctx, programme=programme, entity=entity, state=state, gate=gate, closed=closed, q=q, as_of=as_of,
        limit=limit, cursor=cursor,
    )  # fmt: skip
    return ok(found)


@router.get("/pairs/{name}")
async def pair(ctx: Ctx, who: Viewer, name: str, precheck: str | None = None) -> Response:
    """A pair's gates, convergence, points and versions; with ``precheck=1`` only, what a sign-off of its
    candidate would be refused for, computed now."""
    return ok(await readiness.pair(ctx, name, precheck=precheck == "1"))


@router.get("/pairs/{name}/cycles")
async def cycles(
    ctx: Ctx, who: Viewer, name: str, bucket: str | None = None, cursor: str | None = None
) -> Response:
    """The pair's trend: its points, or each day's or week's last evidence point."""
    return ok(await readiness.cycles(ctx, name, bucket=bucket, cursor=cursor))


@router.get("/pairs/{name}/brief")
async def brief(ctx: Ctx, who: Viewer, name: str) -> Response:
    """The pair's cut-over brief: its gates, its newest complete cycle's stored figures, its last measured
    cycles with their marks and its root-cause groups, opened, still open and closed."""
    return ok(await readiness.brief(ctx, name))


@router.get("/transitions")
async def transitions(
    ctx: Ctx,
    who: Viewer,
    pair: str | None = None,
    programme: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
) -> Response:
    """The ``readiness.changed`` rows, newest first."""
    return ok(await readiness.transitions(ctx, pair=pair, programme=programme, limit=limit, cursor=cursor))


@router.get("/reports/{name}")
async def report(
    ctx: Ctx,
    who: Viewer,
    name: str,
    programme: str | None = None,
    entity: str | None = None,
    pair: str | None = None,
    bucket: str | None = None,
    volume: str | None = None,
) -> Response:
    """``dashboard.pdf`` (a volume of at most 100 pairs), ``dashboard.csv`` or ``trends.csv``; or, of a
    ``pair``, its brief on one page (``brief.pdf``), the brief's points (``brief.csv``) or its root-cause
    groups (``causes.csv``): as a download, audited once built."""
    from deltasleuth.reports import readiness as reports

    if name not in (*reports.NAMES, *reports.BRIEFS):
        raise HTTPException(404, f"report {name!r} not found")
    media, content, named = await reports.download(
        ctx, name, programme=programme, entity=entity, pair=pair, bucket=bucket, volume=volume
    )
    detail = {"name": named, "programme": programme, "config_sha256": config_sha256(ctx.settings.readiness)}
    if name in reports.BRIEFS:  # the pair's, whatever programme was asked for
        detail |= {"programme": None, "pair": pair}
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(who, "report.download", "readiness", detail)
    headers = {"Content-Disposition": f'attachment; filename="readiness-{named}"'}
    return Response(content, media_type=media, headers=headers)
