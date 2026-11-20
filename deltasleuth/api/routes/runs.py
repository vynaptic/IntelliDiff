"""Runs and what they found: create, list, control, results, findings, root causes, keys, soak changes,
graph parity and report downloads; plus the one-shot compare. Keys are shown masked only. A key view is
audited once built, before it is sent, and a report download before its first byte: a CSV export is sent a
page of rows at a time. A cycle's routes answer 410 once its purge has started. A run shows the licence it
was made under (none in an open build), and an unlicensed evaluation's reports download under names that say
so; one a schedule started shows that schedule and the due time it was started for. A run shows what the
schema guard holds of its tables as lines only (``schema``), and a run's creation and a one-shot compare
answer with its ``warnings``. A run shows its newest SHOWN ended cycles, and the cycles route pages through
every one; its progress is counted once for all its readers in COUNTED_FOR seconds. A statement of these
reads that runs past ``api.read_timeout_s`` is cancelled by the store and answered 503 (``SLOW``)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal
from weakref import WeakKeyDictionary

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from psycopg.errors import QueryCanceled
from psycopg_pool import AsyncConnectionPool
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
from deltasleuth.context import Context
from deltasleuth.core.secrets import SecretError
from deltasleuth.core.types import Outcome, Severity
from deltasleuth.reports import accuracy
from deltasleuth.runner import census, drift
from deltasleuth.runner.purge import days
from deltasleuth.store import repo

if TYPE_CHECKING:
    from deltasleuth.runner.runs import KeyView

router = APIRouter()
LIVE = ("queued", "running", "paused")  # a live run's current cycle may still await its key census
SHOWN = 20  # the ended cycles a run's answer holds, its newest: the cycles route pages through every one
COUNTED_FOR = 5.0  # seconds a run's counted progress answers its other readers
SLOW = "the store did not answer this read within {} s: try again"


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
    """A secret the runner cannot read as 422, its message naming the file, never its content. The runner's
    refusals are ``Refused``, which the app answers with their status; anything else is a fault."""
    try:
        yield
    except SecretError as exc:
        raise HTTPException(422, str(exc)) from None


@asynccontextmanager
async def _reading(ctx: Context) -> AsyncIterator[repo.Conn]:
    """A connection for a route's reads, in one transaction whose statements the store cancels past
    ``api.read_timeout_s``: the route answers 503 (``SLOW``) rather than piling reads up on the pool the
    worker shares."""
    limit = ctx.settings.api.read_timeout_s
    try:
        async with ctx.pool.connection() as conn:
            await repo.limit_statements(conn, limit)
            yield conn
    except QueryCanceled:
        raise HTTPException(503, SLOW.format(limit)) from None


@dataclass
class _Count:
    task: asyncio.Task[dict[str, dict[str, int]]]
    at: float | None = None  # when it was counted, on time.monotonic's clock; None while it is counted


# Each store's counts of runs' progress (pool -> (run id, cycle, status, updated_at) -> its count).
_counts: WeakKeyDictionary[AsyncConnectionPool, dict[tuple[Any, ...], _Count]] = WeakKeyDictionary()


async def progress_of(ctx: Context, run: repo.Row) -> dict[str, dict[str, int]]:
    """The key states and outcomes of the run's current cycle (``repo.cycle_counts``), counted once for
    every reader in this process: one that comes while it is counted waits for that count, and one that
    comes within COUNTED_FOR seconds of its end takes its answer. A run that moved on since (its cycle,
    status or ``updated_at``) is counted again, and a count that failed is not kept."""
    counts = _counts.setdefault(ctx.pool, {})
    now = time.monotonic()
    stale = [key for key, count in counts.items() if count.at is not None and now - count.at >= COUNTED_FOR]
    for key in stale:
        del counts[key]
    key = (run["run_id"], run["cycle"], run["status"], run["updated_at"])
    if (count := counts.get(key)) is None:
        count = counts[key] = _Count(asyncio.create_task(_count(ctx, run)))
        count.task.add_done_callback(partial(_counted, counts, key))
    return await asyncio.shield(count.task)  # a reader that leaves does not stop the others' count


async def _count(ctx: Context, run: repo.Row) -> dict[str, dict[str, int]]:
    async with _reading(ctx) as conn:
        return await repo.cycle_counts(conn, run["run_id"], run["cycle"])


def _counted(counts: dict[tuple[Any, ...], _Count], key: tuple[Any, ...], task: asyncio.Task[Any]) -> None:
    if (count := counts.get(key)) is None or count.task is not task:
        return
    if task.cancelled() or task.exception() is not None:
        del counts[key]  # its readers have its error; the next reader counts again
    else:
        count.at = time.monotonic()


@router.post("/runs", status_code=202)
async def new_run(ctx: Ctx, who: Operator, body: RunIn) -> Response:
    """Resolve the keys and hand the run to the workers (the runner audits it)."""
    from deltasleuth.runner.runs import create_run

    with _runner_errors():
        soak: dict[str, bool] | None = None if body.soak is None else dict(body.soak.items())
        run_id = await create_run(ctx, body.spec_id, body.mode, keys=body.keys, soak=soak, who=who)
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
    async with _reading(ctx) as conn:
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
    outcomes, ``progress_of``), its retention, its spec approval, its cycles' sign-offs, its newest SHOWN
    ended cycles (oldest first) as the cycles route shows each and how many have ended (``cycles_ended``),
    a sampled run's current draw, its licence, the schedule that started it, if one did, and what the schema
    guard holds of its tables, as lines (``schema``)."""
    async with _reading(ctx) as conn:
        found = await run_of(conn, run_id)
        scheduled = await schedules.of_run(conn, found)
        kept = await repo.cycle_retention(conn, run_id, **days(ctx))
        newest, _ = await repo.cycle_page(conn, run_id, limit=SHOWN, cursor=None, **days(ctx))
        approval = await run_approval(conn, found)
        signoffs = await repo.pages(repo.list_approvals, conn, kind="signoff", run_id=run_id)
        evidence = await _evidence(ctx, conn, found, newest[::-1], signoffs, who)
        drawing = await _drawing(conn, found)
    progress = await progress_of(ctx, found)  # on a connection of its own, the request's given back
    pending = census.pending(found) if ctx.settings.packs and found["status"] in LIVE else []
    return ok(
        shown(_licensed(found))
        | {"progress": progress, "retention": _retention(ctx, found, kept)}
        | {"approval": approval, "signoffs": [shown(s, "attests") for s in signoffs]}
        | {"census_pending": pending, "cycles": evidence, "cycles_ended": len(kept)}
        | {"sample": drawing, "schedule": scheduled, "schema": drift.told(found)}
    )


@router.get("/runs/{run_id}/cycles")
async def cycles(
    ctx: Ctx, who: Viewer, run_id: str, limit: Limit = SHOWN, cursor: str | None = None
) -> Response:
    """The run's ended cycles newest first, a page at a time, each as the run's ``cycles`` shows it: its
    census, adjudications, labellings, evidence packs and draw with its judgement. A page's cursor is the
    oldest cycle it holds; the run's own answer holds the first page."""
    async with _reading(ctx) as conn:
        found = await run_of(conn, run_id)
        try:
            rows, following = await repo.cycle_page(conn, run_id, limit=limit, cursor=cursor, **days(ctx))
        except ValueError:  # the repo's answer to a cursor it did not issue
            raise HTTPException(400, "malformed cursor") from None
        signoffs = await repo.pages(repo.list_approvals, conn, kind="signoff", run_id=run_id)
        items = await _evidence(ctx, conn, found, rows, signoffs, who)
    return ok({"items": items, "next_cursor": following})


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
    """Each of the ended ``cycles``' census (``census.of_cycle``; none once its purge started, which deletes
    the scan state a table scan's census is read from), adjudications, labellings (each with the sign-off of
    ``signoffs`` attesting it), evidence packs and draw (as ``who`` may read it) with its judgement, in the
    order of ``cycles``."""
    if not cycles:
        return []
    run_id, numbers = run["run_id"], [c["cycle"] for c in cycles]
    spec = await ctx.spec(conn, run["spec_id"])
    scans = await repo.cycle_scans(conn, run_id, numbers)
    drawn = await repo.list_adjudications(conn, run_id)
    labelled, attested = await repo.list_labellings(conn, run_id), accuracy.attesting(signoffs)
    issued = await repo.pages(repo.list_packs, conn, run_id=run_id)
    samples = {d["cycle"]: d for d in await repo.cycle_draws(conn, run_id, numbers)}  # draws outlive purges
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
    async with _reading(ctx) as conn:
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
    async with _reading(ctx) as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        query = repo.list_findings(
            conn, run_id, cycle, key_masked=key, root_cause_id=root_cause_id, taxonomy_code=taxonomy_code,
            path_pattern=path_pattern, severity=severity, limit=limit, cursor=cursor,
        )  # fmt: skip
        return await page(query)


@router.get("/runs/{run_id}/root-causes")
async def root_causes(ctx: Ctx, who: Viewer, run_id: str, cycle: int | None = None) -> Response:
    async with _reading(ctx) as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        return ok({"items": await repo.list_root_causes(conn, run_id, cycle)})


@router.get("/runs/{run_id}/changes")
async def changes(
    ctx: Ctx,
    who: Viewer,
    run_id: str,
    cycle: int | None = None,
    limit: Limit = 100,
    cursor: str | None = None,
) -> Response:
    """Keys whose outcome changed from the soak cycle before to ``cycle`` (the run's current one by
    default), a page at a time; none when the cycle before is purged."""
    async with _reading(ctx) as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        return await page(repo.list_outcome_changes(conn, run_id, cycle, limit=limit, cursor=cursor))


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
    async with _reading(ctx) as conn:
        cycle = await live_cycle(conn, await run_of(conn, run_id), cycle)
        return await page(repo.list_graph_diffs(conn, run_id, cycle, limit=limit, cursor=cursor))


@router.get("/runs/{run_id}/keys/{key:path}")
async def key_detail(ctx: Ctx, who: Viewer, run_id: str, key: str, cycle: int | None = None) -> Response:
    """A key by its masked form: result, findings, observations and the masked canonical A and B
    (``runner.runs.key_view``, which audits the view once it is built)."""
    from deltasleuth.runner.runs import key_view

    return ok(_key_shown(need(await key_view(ctx, run_id, key, cycle, who=who), f"key {key}")))


def _key_shown(view: KeyView) -> dict[str, Any]:
    detail = view.detail
    result = detail["result"] or {}  # empty until the key is compared
    return shown(detail, "result", "findings", "observations") | {
        "run_id": view.run["run_id"],
        "result": shown(result) if result else None,
        "findings": [shown(f) for f in detail["findings"]],
        "observations": [shown(o, "content_hash") for o in detail["observations"]],
        "canonical": dict(view.canonical),
    }


@router.get("/runs/{run_id}/reports/{name}")
async def report(ctx: Ctx, who: Viewer, run_id: str, name: str, cycle: int | None = None) -> Response:
    """A report of ``reports.FORMATS`` (masked; CSV cells OWASP-escaped), as a download, audited before its
    first byte (``reports.download``): a CSV export is sent a page of rows at a time."""
    from deltasleuth.reports import download

    got = await download(ctx, who, run_id, name, cycle)  # 410 once the cycle's purge has started
    headers = {"Content-Disposition": f'attachment; filename="{got.file_name}"'}
    if isinstance(got.content, bytes):
        return Response(got.content, media_type=got.media, headers=headers)
    return StreamingResponse(got.content, media_type=got.media, headers=headers)


@router.post("/compare")
async def compare(ctx: Ctx, who: Operator, body: CompareIn) -> Response:
    """Compare one key now (a one-shot run, rechecked when discrepant); answer with its key view.

    A key of a run is compared under that run's spec only (``runner.runs.compare_again``): another spec
    would mask it by other rules, and show in clear what the run's own spec tokenises.
    """
    from deltasleuth.runner.runs import compare_one

    with _runner_errors():
        async with ctx.pool.connection() as conn:
            await ctx.spec(conn, body.spec_id)  # an unknown spec is refused before any key is read
        if body.run_id is None:
            result = await compare_one(ctx, body.spec_id, body.key, who=who)
        else:
            from deltasleuth.runner.runs import compare_again

            again = await compare_again(ctx, body.run_id, body.key, who=who, spec_id=body.spec_id)
            result = need(again, f"key {body.key}").result
    from deltasleuth.runner.runs import key_view

    # The one-shot run's own masked key: a key token, whatever form a run the key was given by holds it in.
    view = await key_view(ctx, result.attrs["run_id"], result.attrs["key_masked"])
    return ok(_key_shown(need(view, f"key {body.key}")) | {"warnings": result.attrs["warnings"]})
