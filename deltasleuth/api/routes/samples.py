"""Sign-off sampling (docs/mvp/SAMPLING.md §9, §14): a population's ledger, a draw with its live progress or
its sign-off precheck, and closing a name's draws that did not pass with a note of the fix.

Only aggregates leave these routes: strata, counts and bounds, never a key or a key's label (§11.5). A
draw's nonce is withheld while its cycle is live, so nobody reading it can compute the sample being
compared, and from a caller below rule_approver with each stratum's ``u_max``: with keys shown in clear,
they tell which keys lie in a stratum compared whole (§19). A close is a person's statement under SSO,
checked as revoke reasons are.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response

from deltasleuth.api.deps import Ctx, Limit, Operator, Payload, Viewer, need, ok, shown
from deltasleuth.api.routes.approvals import checked
from deltasleuth.approvals import MODES
from deltasleuth.auth.principal import Principal
from deltasleuth.core.spec import compile_spec
from deltasleuth.runner import sample
from deltasleuth.store import repo

router = APIRouter()


class CloseIn(Payload):
    spec_name: str
    note: str


def withheld(draw: repo.Row, who: Principal) -> repo.Row:
    """A stored draw as ``who`` may read it: below rule_approver, without its nonce or its strata's
    ``u_max`` (attests and packs keep both)."""
    if "rule_approver" in who.roles:
        return draw
    strata = [{name: v for name, v in s.items() if name != "u_max"} for s in draw["strata"]]
    return draw | {"nonce": None, "strata": strata}


def _item(draw: repo.Row) -> repo.Row:
    """A ledger draw as the list shows it: whether it is closed, and whether it blocks later sign-offs."""
    return shown(draw, "strata", "frame") | {
        "closed": draw["closed_at"] is not None,
        "blocking": sample.blocking(draw),
    }


@router.get("/samples")
async def ledger(
    ctx: Ctx, who: Viewer, spec_name: str | None = None, limit: Limit = 50, cursor: str | None = None
) -> Response:
    """A name's ledger (its newest version's population: every spec's draws of it), or every draw, in
    commit order."""
    async with ctx.pool.connection() as conn:
        if spec_name is None:
            try:
                rows, next_cursor = await repo.draws(conn, limit=limit, cursor=cursor)
            except ValueError:
                raise HTTPException(400, "malformed cursor") from None
        else:
            stored = need(await repo.newest_spec(conn, spec_name), f"spec {spec_name}")
            found = compile_spec(stored["body"]).sample
            rows, next_cursor = await repo.ledger(conn, found.population) if found else [], None
    return ok({"items": [_item(row) for row in rows], "next_cursor": next_cursor})


@router.get("/samples/{draw_id}")
async def draw(ctx: Ctx, who: Viewer, draw_id: str) -> Response:
    """The draw (``withheld``) and its ledger; while its cycle is live, its ``progress`` (and no nonce),
    else its sign-off ``precheck`` (G1-G11, computed now; null once the cycle's purge started: it is never
    signed off)."""
    async with ctx.pool.connection() as conn:
        found = need(await repo.ledger_draw(conn, draw_id), f"draw {draw_id}")
        run = need(await repo.get_run(conn, found["run_id"]), f"run {found['run_id']}")
        spec = await ctx.spec(conn, found["spec_id"])
        rows = await repo.ledger(conn, found["population"])
        extra: dict[str, Any]
        if found["status"] == "live":
            extra = {"nonce": None, "progress": await sample.progress(conn, found, spec)}
        elif (await repo.get_cycle(conn, run["run_id"], found["cycle"]))["purging_at"]:  # type: ignore[index]
            extra = {"precheck": None}
        else:
            extra = {"precheck": await sample.problems(conn, run, found["cycle"], spec)}
    listed = {"blocking": sample.blocking(found), "ledger": [_item(d) for d in rows]}
    return ok(shown(withheld(found, who)) | listed | extra)


@router.post("/samples/close")
async def close(ctx: Ctx, who: Operator, body: CloseIn) -> Response:
    """Close every ended bottom_k draw of the name's ledger that did not pass and is not closed, with a note
    of what was fixed (C1-C5): the draws closed."""
    if (mode := ctx.settings.auth_mode) != "oidc":
        raise HTTPException(
            409, f"closing draws needs SSO (auth.oidc); this deployment runs in {MODES[mode]}"
        )
    if not who.person:
        raise HTTPException(403, "closing draws needs a signed-in person")
    note = checked(body.note)
    assert note is not None
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            closed = await sample.close(conn, audit, body.spec_name, note, who)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    return ok({"closed": closed})
