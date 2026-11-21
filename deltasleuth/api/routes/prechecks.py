"""Table pre-checks: the pre-checks with their banner, one with its totals and
its pairs worst first, a pair's result, the reports, a pair's draft, and the acts, each audited as its
caller: making a pre-check of stored specs and cancelling one (operators; a token or developer mode too),
and making one of two schemas paired by name (spec authors, as drafting is authoring). A viewer reads
everything; a report download is audited once built, before it is sent, and a draft is not, as a profile's
is not. No route here calls the licence but to label an unlicensed evaluation, none reads a table (the
worker does; two schemas are listed, as a run's creation plans), and every answer but a download is
headed by what a pre-check is not."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import Field

from deltasleuth import prechecks
from deltasleuth.api.deps import Ctx, Limit, Operator, Payload, SpecAuthor, Viewer, ok
from deltasleuth.store import repo

router = APIRouter(prefix="/prechecks")
Verdict = Literal[
    "unpaired", "refused", "not_read", "rows_differ", "keys_null_or_repeated", "columns_not_compared",
    "values_differ", "incomplete", "schema_differs", "no_difference_observed",
]  # core.precheck.VERDICTS  # fmt: skip


class PrecheckIn(Payload):
    """Exactly one of the three forms; the level is judged by its own message."""

    level: str = "columns"
    spec_ids: list[int] | None = None  # stored versions
    spec_names: list[str] | None = None  # each name's newest version
    all_specs: bool = False  # the newest version of every name whose sides are both tables


class SchemaIn(Payload):
    target: str
    schema_: str | None = Field(None, alias="schema")  # the target's default when left out


class CatalogueIn(Payload):
    """Two declared database targets' schemas, their tables paired by name; the level is judged by its
    own message, the globs by theirs."""

    a: SchemaIn
    b: SchemaIn
    include: list[str] | None = None
    exclude: list[str] | None = None
    level: str = "counts"


@router.post("", status_code=202)
async def create(ctx: Ctx, who: Operator, body: PrecheckIn) -> Response:
    """Store the pre-check, its pairs pending for the worker."""
    return ok(await prechecks.create(ctx, body.model_dump(), who), 202)


@router.post("/catalogue", status_code=202)
async def catalogue(ctx: Ctx, who: SpecAuthor, body: CatalogueIn) -> Response:
    """List the two schemas, pair their tables by name and store the pre-check, its pairs pending."""
    return ok(await prechecks.catalogue(ctx, body.model_dump(by_alias=True), who), 202)


@router.get("")
async def overview(ctx: Ctx, who: Viewer, limit: Limit = 50, cursor: str | None = None) -> Response:
    """The pre-checks, newest first, each with its progress and its pairs counted by verdict."""
    return ok(await prechecks.overview(ctx, limit=limit, cursor=cursor))


@router.get("/{precheck_id}")
async def precheck(
    ctx: Ctx,
    who: Viewer,
    precheck_id: str,
    verdict: Verdict | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> Response:
    """The pre-check, its totals and its pairs worst first, each with its verdict's line."""
    return ok(await prechecks.described(ctx, precheck_id, verdict=verdict, limit=limit, cursor=cursor))


@router.get("/{precheck_id}/pairs/{n}")
async def pair(ctx: Ctx, who: Viewer, precheck_id: str, n: int) -> Response:
    """The pair's result: its lines, refusals, schema codes, keys and each column's figures."""
    return ok(await prechecks.pair(ctx, precheck_id, n))


@router.get("/{precheck_id}/pairs/{n}/draft.yaml")
async def draft(ctx: Ctx, who: SpecAuthor, precheck_id: str, n: int) -> Response:
    """A starter spec of a pair paired by name, its issues heading it as comments: never stored."""
    text, _ = await prechecks.draft(ctx, precheck_id, n)
    return Response(text, media_type="application/yaml")


@router.post("/{precheck_id}/cancel")
async def cancel(ctx: Ctx, who: Operator, precheck_id: str) -> Response:
    """Cancel its pending and running pairs; what was read stays."""
    return ok(await prechecks.cancel(ctx, precheck_id, who))


@router.get("/{precheck_id}/reports/{name}")
async def report(ctx: Ctx, who: Viewer, precheck_id: str, name: str) -> Response:
    """``precheck.csv``, ``precheck-columns.csv`` or ``precheck.pdf``, as a download."""
    from deltasleuth.reports import precheck as reports

    if name not in reports.NAMES:
        raise HTTPException(404, f"report {name!r} not found")
    media, content, named = await reports.download(ctx, precheck_id, name)
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(who, "report.download", precheck_id, {"name": name})
    headers = {"Content-Disposition": f'attachment; filename="{named}"'}
    return Response(content, media_type=media, headers=headers)
