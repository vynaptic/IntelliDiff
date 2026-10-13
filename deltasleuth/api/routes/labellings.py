"""Labelled accuracy (docs/mvp/ACCURACY.md §10): draw a labelling of an ended cycle against a named truth
source, list a cycle's labellings, read one with what it claims, download its sheet and upload the filled
sheet once.

Every auth mode serves them (D8): a token's or developer mode's labelling is shown, and never attested. A
draw or an upload answers 409 while the cycle has a requested or approved sign-off, 410 once its purge has
started (``runner.purge.CyclePurged``), as the sheet does; the list and the item then show the tombstone,
which keeps its score. Every text shown comes from ``accuracy.lines``, ``summary`` and ``LIMITS``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from pydantic import StrictBool, StrictInt

from deltasleuth.api.deps import Ctx, Operator, Payload, Viewer, need, ok, run_of
from deltasleuth.api.routes.packs import _not_found
from deltasleuth.reports import accuracy
from deltasleuth.runner.purge import refuse_purged
from deltasleuth.store import repo

router = APIRouter()
Cycle = Annotated[int, Path(ge=1, le=2**31 - 1)]  # a cycle the store can hold: any other answers 422


class TruthIn(Payload):
    source: str | None = None  # a system identifier and a date, checked by store_draw (its messages are
    as_of: str | None = None  # binding)


class DrawIn(Payload):
    size: StrictInt = 100
    per_cause: StrictInt = 2
    accepted: StrictInt = 0
    agreed: StrictInt = 0
    truth: TruthIn
    acknowledge_unlocatable: StrictBool = False


async def _attesting(conn: repo.Conn, run_id: str, cycle: int) -> dict[str, str]:
    return accuracy.attesting(await repo.approval_history(conn, run_id=run_id, cycle=cycle))


@router.post("/runs/{run_id}/cycles/{cycle}/labellings", status_code=201)
async def draw(ctx: Ctx, who: Operator, run_id: str, cycle: Cycle, body: DrawIn) -> Response:
    """Draw a labelling of the ended cycle with a server salt (``accuracy.store_draw``): 201 with its items
    (masked keys) and warnings; 200 with the first labelling when the caller repeats a draw within ten
    minutes. 409 while the cycle has a requested or approved sign-off, has not ended or lacks a population
    asked for; 504 when the draw was stopped."""
    asked = body.model_dump()
    with _not_found():
        row, again = await accuracy.store_draw(ctx, run_id, cycle, who=who, **asked)
    names = ("item", "key", "population", "root_cause_id")
    items = [{name: item[name] for name in names} for item in row["selection"]["items"]]
    warnings = [*row["selection"]["warnings"], *([again] if again else [])]
    found = {"labelling_id": row["labelling_id"], "salt": row["salt"], "items": items, "warnings": warnings}
    return ok(found, 200 if again else 201)


@router.get("/runs/{run_id}/cycles/{cycle}/labellings")
async def labellings(ctx: Ctx, who: Viewer, run_id: str, cycle: Cycle) -> Response:
    """The cycle's labellings, newest first, each with its ``summary`` and neither its selection nor its
    score (the item holds them); a purged cycle's tombstones too."""
    async with ctx.pool.connection() as conn:
        await run_of(conn, run_id)
        rows = await repo.list_labellings(conn, run_id, cycle)
        attested = await _attesting(conn, run_id, cycle)
    return ok({"items": [accuracy.listed(row, attested.get(row["labelling_id"])) for row in rows]})


@router.get("/labellings/{labelling_id}")
async def labelling(ctx: Ctx, who: Viewer, labelling_id: str) -> Response:
    """The labelling with its selection's counts, its score, what it says (``lines``, empty until it is
    labelled) and may not be read as (``limits``), and the sign-off attesting it."""
    async with ctx.pool.connection() as conn:
        row = need(await repo.get_labelling(conn, labelling_id), f"labelling {labelling_id}")
        attested = (await _attesting(conn, row["run_id"], row["cycle"])).get(labelling_id)
    return ok(accuracy.detailed(row, attested))


@router.get("/labellings/{labelling_id}/sheet.csv")
async def sheet(ctx: Ctx, who: Viewer, labelling_id: str) -> Response:
    """The sheet as drawn, or once labelled as stored: with its labels and no notes. Masked keys only; CSV
    cells OWASP-escaped."""
    async with ctx.pool.connection() as conn:
        found = need(await repo.get_labelling(conn, labelling_id), f"labelling {labelling_id}")
        await refuse_purged(conn, found["run_id"], found["cycle"])
    headers = {"Content-Disposition": f'attachment; filename="{labelling_id}.csv"'}
    return Response(found["sheet"], media_type="text/csv; charset=utf-8", headers=headers)


@router.post("/labellings/{labelling_id}/labels")
async def labels(ctx: Ctx, who: Operator, labelling_id: str, request: Request) -> Response:
    """Label the labelling with the filled sheet sent as the body (``text/csv``, at most 1 MiB), once
    (``accuracy.store_labels``): the labelling as labelled. 409 while the cycle has a requested or approved
    sign-off, or once it is labelled; 413 over 1 MiB; 422 for a sheet that fails a check."""
    data = await request.body()
    with _not_found():
        await accuracy.store_labels(ctx, labelling_id, data, who=who)
    async with ctx.pool.connection() as conn:
        row = await repo.get_labelling(conn, labelling_id)
    assert row is not None
    return ok(accuracy.detailed(row, None))
