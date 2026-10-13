"""Adjudications and evidence packs (docs/mvp/PACK.md §5, §7.1): draw a reviewer sheet of an ended cycle,
download it, score its reviewed upload; issue a signed-off cycle's pack, list packs, download one and the
signed revocation list.

Every route answers 409 outside oidc mode first: packs certify sign-offs, which need SSO, and so do the
adjudications a sign-off attests. Pack routes then answer 503 without the pack settings. Only a signed-in
person issues a pack (403 for a token). A download is audited ``pack.download`` before its bytes are sent.
A purged cycle answers 410 (``runner.purge.CyclePurged``); its pack records stay, their stored ZIPs do not.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import StrictInt

from deltasleuth.api.deps import Ctx, Operator, Payload, Viewer, need, ok, page, run_of, shown
from deltasleuth.reports import adjudicate, pack
from deltasleuth.reports.csv import parse_csv
from deltasleuth.runner.purge import refuse_purged
from deltasleuth.store import repo

router = APIRouter()


class DrawIn(Payload):
    limit: StrictInt = adjudicate.DEFAULT_LIMIT  # 1 to 500, checked by store_draw (its message is binding)


def adjudication_item(row: repo.Row) -> repo.Row:
    """An adjudication as lists show it: its sheet is downloaded on its own (``sheet.csv``)."""
    return shown(row, "sheet")


@contextmanager
def _not_found() -> Iterator[None]:
    """An unknown run, adjudication or pack (LookupError) as 404; a KeyError or IndexError is a bug."""
    try:
        yield
    except (KeyError, IndexError):
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


# -- adjudications (§5) ----------------------------------------------------------------------------------


@router.post("/runs/{run_id}/cycles/{cycle}/adjudications", status_code=201)
async def draw(ctx: Ctx, who: Operator, run_id: str, cycle: int, body: DrawIn | None = None) -> Response:
    """Draw a reviewer sheet of the ended cycle with a server salt (``adjudicate.store_draw``); 409 while
    the cycle has a requested or approved sign-off, or has not ended."""
    pack.sso(ctx)
    with _not_found():
        row = await adjudicate.store_draw(ctx, run_id, cycle, who=who, limit=(body or DrawIn()).limit)
    keys = [line["key"] for line in parse_csv(row["sheet"].encode())[1]]  # masked, in the sheet's order
    return ok({"adjudication_id": row["adjudication_id"], "salt": row["salt"], "keys": keys}, 201)


@router.get("/runs/{run_id}/cycles/{cycle}/adjudications")
async def adjudications(ctx: Ctx, who: Viewer, run_id: str, cycle: int) -> Response:
    """The cycle's adjudications, newest first."""
    pack.sso(ctx)
    async with ctx.pool.connection() as conn:
        await run_of(conn, run_id)
        await refuse_purged(conn, run_id, cycle)
        rows = await repo.list_adjudications(conn, run_id, cycle)
    return ok({"items": [adjudication_item(row) for row in rows]})


@router.get("/adjudications/{adjudication_id}/sheet.csv")
async def sheet(ctx: Ctx, who: Viewer, adjudication_id: str) -> Response:
    """The sheet as drawn, or once scored as stored: with the reviewer's verdicts and no notes. Masked keys
    only; CSV cells OWASP-escaped."""
    pack.sso(ctx)
    async with ctx.pool.connection() as conn:
        found = need(await repo.get_adjudication(conn, adjudication_id), f"adjudication {adjudication_id}")
        await refuse_purged(conn, found["run_id"], found["cycle"])
    headers = {"Content-Disposition": f'attachment; filename="{adjudication_id}.csv"'}
    return Response(found["sheet"], media_type="text/csv; charset=utf-8", headers=headers)


@router.post("/adjudications/{adjudication_id}/score")
async def score(ctx: Ctx, who: Operator, adjudication_id: str, request: Request) -> Response:
    """Score the reviewed sheet sent as the body (``text/csv``, at most 1 MiB), once
    (``adjudicate.store_score``): the adjudication as scored. 409 while the cycle has a requested or
    approved sign-off, or once it is scored; 413 over 1 MiB; 422 for a sheet that cannot be scored."""
    pack.sso(ctx)
    data = await request.body()
    with _not_found():
        await adjudicate.store_score(ctx, adjudication_id, data, who=who)
    async with ctx.pool.connection() as conn:
        row = await repo.get_adjudication(conn, adjudication_id)
    assert row is not None
    return ok(adjudication_item(row))


# -- evidence packs (§6) ---------------------------------------------------------------------------------


@router.post("/runs/{run_id}/cycles/{cycle}/packs", status_code=201)
async def issue(ctx: Ctx, who: Operator, run_id: str, cycle: int) -> Response:
    """Issue the signed-off cycle's evidence pack (``pack.issue``); 409 with every reason it gets none, 403
    for anyone but a signed-in person, 503 without the pack settings."""
    with _not_found():
        row = await pack.issue(ctx, run_id, cycle, who=who)
    fields = ("pack_id", "sums_sha256", "key_fingerprint")
    return ok({name: row[name] for name in fields}, 201)


@router.get("/packs")
async def packs(
    ctx: Ctx,
    who: Viewer,
    run_id: str | None = None,
    status: Literal["issued", "revoked"] | None = None,
    cursor: str | None = None,
) -> Response:
    """Packs newest first, without their manifests."""
    pack.available(ctx)
    async with ctx.pool.connection() as conn:
        return await page(repo.list_packs(conn, run_id=run_id, status=status, cursor=cursor))


@router.get("/packs/revocations")
async def revocations(ctx: Ctx, who: Viewer) -> Response:
    """A ZIP of ``revoked.txt`` (every revoked pack of every key used), its signature and the current
    signing key, which a pack's step 8 checks."""
    files = await pack.revocations(ctx, pack.available(ctx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    headers = {"Content-Disposition": 'attachment; filename="revocations.zip"'}
    return Response(out.getvalue(), media_type="application/zip", headers=headers)


@router.get("/packs/{pack_id}")
async def pack_detail(ctx: Ctx, who: Viewer, pack_id: str) -> Response:
    """The pack with its manifest; a purged cycle's too, whose record stays."""
    pack.available(ctx)
    async with ctx.pool.connection() as conn:
        return ok(need(await repo.get_pack(conn, pack_id), f"pack {pack_id}"))


@router.get("/packs/{pack_id}/download")
async def download(ctx: Ctx, who: Viewer, pack_id: str) -> Response:
    """The pack's stored ZIP, audited ``pack.download`` before it is sent; 410 once its cycle's purge
    has started, which deletes it (copies downloaded before still verify)."""
    pack.available(ctx)
    with _not_found():
        found, data = await pack.stored(ctx, pack_id)
    await pack.downloaded(ctx, who, found)
    headers = {"Content-Disposition": f'attachment; filename="{pack_id}.zip"'}
    return Response(data, media_type="application/zip", headers=headers)
