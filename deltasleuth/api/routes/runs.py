"""Runs and what they found: create, list, control, results, findings, root causes, keys, soak changes,
graph parity and report downloads; plus the one-shot compare. Keys are shown masked only. A key view and a
report download are audited once built, before they are sent. A cycle's routes answer 410 once its purge has
started. A run shows the licence it was made under (none in an open build), and an unlicensed evaluation's
reports download under names that say so; one a schedule started shows that schedule and the due time it was
started for. A run shows what the schema guard holds of its tables as lines only (``schema``), and a run's
creation and a one-shot compare answer with its ``warnings``."""

from __future__ import annotations

from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import Field, StrictBool

from deltasleuth import licence, schedules
from deltasleuth.api.deps import (
    Ctx,
    Limit,
    Operator,
    Payload,
    Viewer,
    live_cycle,
    need,
    ok,
    page,
    run_of,
    shown,
)
from deltasleuth.api.routes.packs import adjudication_item
from deltasleuth.api.routes.samples import withheld
from deltasleuth.approvals import run_approval
from deltasleuth.auth.principal import Principal
from deltasleuth.context import Capabilities, Context
from deltasleuth.core.types import CompiledSpec, Outcome, Severity
from deltasleuth.reports import accuracy
from deltasleuth.runner import census, drift
from deltasleuth.runner.purge import days
from deltasleuth.security.tokenise import mask_document, mask_key
from deltasleuth.store import repo

router = APIRouter()
LIVE = ("queued", "running", "paused")  # a live run's current cycle may still await its key census


class RunOptions(Payload):
    """What a run takes besides its spec and keys: a schedule holds it."""

    mode: Literal["batch", "soak"]
    soak: dict[Literal["rolling"], StrictBool] | None = None


class RunIn(RunOptions):
    spec_id: int
    keys: list[Annotated[str, Field(min_length=1)]] | None = None  # else the spec's key source


class CompareIn(Payload):
    spec_id: int
    key: str = Field(min_length=1)
    run_id: str | None = None  # then ``key`` is a masked key of that run, whose spec is ``spec_id``


@contextmanager
def _runner_errors() -> Iterator[None]:
    """The runner's refusals: LookupError for an unknown run or spec, ValueError for bad input."""
    try:
        yield
    except (KeyError, IndexError, drift.Refused):  # a bug, not a refusal; the schema guard's has its status
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/runs", status_code=202)
async def new_run(ctx: Ctx, who: Operator, body: RunIn) -> Response:
    """Resolve the keys and hand the run to the workers (the runner audits it)."""
    from deltasleuth.runner.runs import create_run

    with _runner_errors():
        run_id = await create_run(ctx, body.spec_id, body.mode, keys=body.keys, soak=body.soak, who=who)
    async with ctx.pool.connection() as conn:
        created = await run_of(conn, run_id)
    return ok({"run_id": run_id, "warnings": drift.warnings(created)}, 202)


@router.get("/runs")
async def runs(
    ctx: Ctx,
    who: Viewer,
    spec_id: int | None = None,
    status: str | None = None,
    schedule_id: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> Response:
    """Newest first; ``schedule_id``: the runs that schedule started."""
    async with ctx.pool.connection() as conn:
        query = repo.list_runs(
            conn, spec_id=spec_id, status=status, schedule_id=schedule_id, limit=limit, cursor=cursor
        )
        return await page(_labelled(query))


def _licensed(run: repo.Row) -> repo.Row:
    """The run with ``licence``: the record it was made under and its label, or None without one."""
    record = run["params"].get("licence")
    return run | {"licence": record and record | {"label": licence.run_label(run)}}


async def _labelled(query: Awaitable[repo.Page]) -> repo.Page:
    rows, cursor = await query
    return [_licensed(row) for row in rows], cursor


@router.get("/runs/{run_id}")
async def run(ctx: Ctx, who: Viewer, run_id: str) -> Response:
    """The run (with its column plan and table scan, if any), its current cycle's progress (key states and
    outcomes), its retention, its spec approval, its cycles' sign-offs, each ended cycle's census,
    adjudications and evidence packs and labellings, a sampled run's current draw and each ended cycle's
    draw and judgement, its licence, the schedule that started it, if one did, and what the schema guard
    holds of its tables, as lines (``schema``)."""
    async with ctx.pool.connection() as conn:
        found = await run_of(conn, run_id)
        scheduled = await schedules.of_run(conn, found)
        progress = await repo.cycle_counts(conn, run_id)
        cycles = await repo.list_cycles(conn, run_id, **days(ctx))
        approval = await run_approval(conn, found)
        signoffs = await repo.pages(repo.list_approvals, conn, kind="signoff", run_id=run_id)
        evidence = await _evidence(ctx, conn, found, cycles, signoffs, who)
        drawing = await _drawing(conn, found)
    pending = census.pending(found) if ctx.settings.packs and found["status"] in LIVE else []
    return ok(
        shown(_licensed(found))
        | {"progress": progress, "retention": _retention(ctx, found, cycles)}
        | {"approval": approval, "signoffs": [shown(s, "attests") for s in signoffs]}
        | {"census_pending": pending, "cycles": evidence, "sample": drawing, "schedule": scheduled}
        | {"schema": drift.told(found)}
    )


async def _drawing(conn: repo.Conn, run: repo.Row) -> dict[str, Any] | None:
    """A sampled run's current draw: ``state`` (drawing, comparing, resolving or paused; None once the run
    ended), the error its frame gave, and the draw's id and time once drawn; None for other runs."""
    if run["params"].get("keys_source") != "hash_sample":
        return None
    run_id, cycle = run["run_id"], run["cycle"]
    drawn = await repo.get_draw(conn, run_id, cycle)
    state = None
    if run["status"] == "paused":
        state = "paused"
    elif run["status"] in LIVE:
        drawing = run["draw"] is not None
        state = (
            "drawing" if drawing else "resolving" if await repo.retrying(conn, run_id, cycle) else "comparing"
        )
    return {
        "state": state,
        "error": (run["draw"] or {}).get("error"),
        "draw_id": drawn and drawn["draw_id"],
        "drawn_at": drawn and drawn["drawn_at"],
    }


async def _evidence(
    ctx: Context,
    conn: repo.Conn,
    run: repo.Row,
    cycles: list[repo.Row],
    signoffs: list[repo.Row],
    who: Principal,
) -> list[dict[str, Any]]:
    """Each ended cycle's census (``census.of_cycle``; none once its purge started, which deletes the scan
    state a table scan's census is read from), adjudications, labellings (each with the sign-off of
    ``signoffs`` attesting it), evidence packs and draw (as ``who`` may read it) with its judgement,
    oldest cycle first."""
    if not cycles:
        return []
    run_id = run["run_id"]
    spec = await ctx.spec(conn, run["spec_id"])
    scans = await repo.run_scans(conn, run_id)
    drawn = await repo.list_adjudications(conn, run_id)
    labelled, attested = await repo.list_labellings(conn, run_id), accuracy.attesting(signoffs)
    issued = await repo.pages(repo.list_packs, conn, run_id=run_id)
    samples = {d["cycle"]: d for d in await repo.run_draws(conn, run_id)}  # draws outlive purges
    return [
        {
            "cycle": c["cycle"],
            "census": None if c["purging_at"] else census.of_cycle(run, c, scans.get(c["cycle"]), spec),
            "adjudications": [adjudication_item(j) for j in drawn if j["cycle"] == c["cycle"]],
            "labellings": [
                accuracy.listed(v, attested.get(v["labelling_id"]))
                for v in labelled
                if v["cycle"] == c["cycle"]
            ],
            "packs": [shown(p) for p in issued if p["cycle"] == c["cycle"]],
            "sample": (d := samples.get(c["cycle"])) and {"draw": withheld(d, who), "judgement": c["sample"]},
        }
        for c in cycles
    ]


def _retention(ctx: Context, run: repo.Row, cycles: list[repo.Row]) -> dict[str, Any]:
    """The run's retention class and its days, its hold, the cycles purged (their purge started) and the
    ended cycles still kept, with the day each expires."""
    purged = [c for c in cycles if c["purging_at"] is not None]
    hold = run["hold_reason"] and {"reason": run["hold_reason"], "by": run["hold_by"], "at": run["hold_at"]}
    return {
        "class": run["retention"],
        "days": days(ctx)[f"{run['retention']}_days"],
        "hold": hold,
        "purged": [c["cycle"] for c in purged],
        "last_purged_at": max((c["purged_at"] for c in purged if c["purged_at"]), default=None),
        "cycles": [
            {"cycle": c["cycle"], "ended_at": c["ended_at"], "expires_at": c["expires_at"]}
            for c in cycles
            if c["purging_at"] is None
        ],
    }


@router.post("/runs/{run_id}/{action}")
async def control(
    ctx: Ctx, who: Operator, run_id: str, action: Literal["cancel", "pause", "resume"]
) -> Response:
    """Move the run through the runner (which audits it); 409 when it cannot move."""
    from deltasleuth.runner.runs import cancel_run, pause_run, resume_run

    move, reached = {
        "pause": (pause_run, ("paused",)),
        "resume": (resume_run, ("running",)),
        "cancel": (cancel_run, ("cancelling", "cancelled")),  # cancelling while keys are in flight
    }[action]
    with _runner_errors():
        status = await move(ctx, run_id, who=who)
    if status not in reached:
        raise HTTPException(409, f"cannot {action} a run that is {status}")
    return ok({"run_id": run_id, "status": status})


@router.get("/runs/{run_id}/results")
async def results(
    ctx: Ctx,
    who: Viewer,
    run_id: str,
    cycle: int | None = None,
    outcome: Outcome | None = None,
    limit: Limit = 100,
    cursor: str | None = None,
) -> Response:
    async with ctx.pool.connection() as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        return await page(repo.list_results(conn, run_id, cycle, outcome=outcome, limit=limit, cursor=cursor))


@router.get("/runs/{run_id}/findings")
async def findings(
    ctx: Ctx,
    who: Viewer,
    run_id: str,
    cycle: int | None = None,
    key: str | None = None,  # masked
    root_cause_id: str | None = None,
    taxonomy_code: str | None = None,
    path_pattern: str | None = None,
    severity: Severity | None = None,
    limit: Limit = 100,
    cursor: str | None = None,
) -> Response:
    async with ctx.pool.connection() as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        query = repo.list_findings(
            conn, run_id, cycle, key_masked=key, root_cause_id=root_cause_id, taxonomy_code=taxonomy_code,
            path_pattern=path_pattern, severity=severity, limit=limit, cursor=cursor,
        )  # fmt: skip
        return await page(query)


@router.get("/runs/{run_id}/root-causes")
async def root_causes(ctx: Ctx, who: Viewer, run_id: str, cycle: int | None = None) -> Response:
    async with ctx.pool.connection() as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        return ok({"items": await repo.list_root_causes(conn, run_id, cycle)})


@router.get("/runs/{run_id}/changes")
async def changes(ctx: Ctx, who: Viewer, run_id: str) -> Response:
    """Keys whose outcome changed from one soak cycle to the next."""
    async with ctx.pool.connection() as conn:
        await run_of(conn, run_id)
        return ok({"items": [shown(row) for row in await repo.outcome_changes(conn, run_id)]})


@router.get("/runs/{run_id}/graph")
async def graph(
    ctx: Ctx,
    who: Viewer,
    run_id: str,
    cycle: int | None = None,
    limit: Limit = 100,
    cursor: str | None = None,
) -> Response:
    """Graph parity rows, masked when written; none for a run without them."""
    async with ctx.pool.connection() as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        return await page(repo.list_graph_diffs(conn, run_id, cycle, limit=limit, cursor=cursor))


@router.get("/runs/{run_id}/keys/{key:path}")
async def key_detail(ctx: Ctx, who: Viewer, run_id: str, key: str, cycle: int | None = None) -> Response:
    """A key by its masked form: result, findings, observations and the masked canonical A and B."""
    view = await _key_view(ctx, run_id, key, cycle)
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(who, "key.view", run_id, {"key_masked": key, "cycle": view["cycle"]})
    return ok(view)


async def _key_view(ctx: Context, run_id: str, key: str, cycle: int | None = None) -> dict[str, Any]:
    async with ctx.pool.connection() as conn:
        run = await run_of(conn, run_id)
        cycle = await live_cycle(conn, run, cycle)
        detail = need(await repo.get_key_detail(conn, run_id, key, cycle), f"key {key}")
        spec, caps = await ctx.spec(conn, run["spec_id"]), ctx.capabilities(run)
        result = detail["result"] or {}  # empty until the key is compared
        canonical = {
            s: await _canonical(ctx, conn, run_id, spec, caps, result.get(f"obs_{s}_id")) for s in "ab"
        }
    view = shown(detail, "result", "findings", "observations")
    return view | {
        "run_id": run_id,
        "result": shown(result) if result else None,
        "findings": [shown(f) for f in detail["findings"]],
        "observations": [shown(o, "content_hash") for o in detail["observations"]],
        "canonical": canonical,
    }


async def _canonical(
    ctx: Context, conn: repo.Conn, run_id: str, spec: CompiledSpec, caps: Capabilities, obs_id: str | None
) -> Any:
    """The masked canonical document of an observation, re-derived from its evidence with the run's
    capabilities; None when there is no observation or its side was not COMPLETE."""
    from deltasleuth.runner.compare import side_state

    found = await side_state(ctx, conn, spec, run_id, obs_id, caps)
    if found is None or found[1].canonical is None:
        return None
    obs, state = found
    return mask_document(state.canonical.doc, spec, ctx.tokeniser, key=obs.key)


@router.get("/runs/{run_id}/reports/{name}")
async def report(ctx: Ctx, who: Viewer, run_id: str, name: str, cycle: int | None = None) -> Response:
    """A report of ``reports.FORMATS`` (masked; CSV cells OWASP-escaped), as a download."""
    from deltasleuth.reports import FORMATS

    async with ctx.pool.connection() as conn:
        run = await run_of(conn, run_id)
    media, render = need(FORMATS.get(name), f"report {name!r}")
    content = await render(ctx, run_id, cycle)  # 410 once the cycle's purge has started
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(who, "report.download", run_id, {"name": name, "cycle": cycle or run["cycle"]})
    headers = {"Content-Disposition": f'attachment; filename="{licence.file_name(run, name)}"'}
    return Response(content, media_type=media, headers=headers)


@router.post("/compare")
async def compare(ctx: Ctx, who: Operator, body: CompareIn) -> Response:
    """Compare one key now (a one-shot run, rechecked when discrepant); answer with its key view.

    A key of a run is compared under that run's spec only: another spec would mask it by other
    rules, and show in clear what the run's own spec tokenises.
    """
    from deltasleuth.runner.runs import compare_one

    key = body.key
    with _runner_errors():
        async with ctx.pool.connection() as conn:
            spec = await ctx.spec(conn, body.spec_id)
            if body.run_id is not None:
                of = await run_of(conn, body.run_id)
                if of["spec_id"] != body.spec_id:
                    raise HTTPException(422, "a key of a run is compared under that run's spec")
                await live_cycle(conn, of)
                detail = need(await repo.get_key_detail(conn, body.run_id, key), f"key {key}")
                key = (await ctx.sealer(conn, body.run_id)).open_row(detail)["key"]
        result = await compare_one(ctx, body.spec_id, key, who=who)
    view = await _key_view(ctx, result.attrs["run_id"], mask_key(result.key, spec, ctx.tokeniser))
    return ok(view | {"warnings": result.attrs["warnings"]})
