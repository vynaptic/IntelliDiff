"""Service facts: health (open, for probes), targets and LLM profiles, never their secrets, and the
licence's standing; and the targets declared in the product, which who declares (``deps.Declarer``) adds,
changes and removes, each audited."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, HTTPException, Response

from deltasleuth import licence
from deltasleuth import targets as product
from deltasleuth.api.deps import Ctx, Declarer, Payload, Viewer, declarer, ok
from deltasleuth.runner import worker
from deltasleuth.store import repo

public = APIRouter()
router = APIRouter()


class HeaderIn(Payload):
    header: str


class AuthIn(Payload):
    kind: str
    header: str | None = None
    more: list[HeaderIn] | None = None  # kind header: further credential headers


class TargetIn(Payload):
    """A service as the product declares one: the targets file's members without references. The file's
    schema judges them; any other member (``secret_ref``, ``dsn_ref``, ``engine``, ``capabilities``) is a
    422 here."""

    base_url: str
    allow_private_network: bool | None = None
    allow_insecure_http: bool | None = None
    auth: AuthIn | None = None
    secret: str | None = None  # the credential of the bearer or of auth.header; absent: the stored one
    more_secrets: dict[str, str] | None = None  # those of auth.more, by header
    rate_per_s: float | None = None
    max_concurrency: int | None = None
    absent_on_404: bool = False  # a 404 means no such record: the one capability


@public.get("/health")
async def health(ctx: Ctx) -> Response:
    """Up when the database answers, else 503: at once when a pooled connection fails, or after the three
    seconds the pool is given to hand one out, under the image health check's four. The statement itself
    is not bounded. ``ai`` and ``auth`` tell the UI what to offer. ``worker`` is the worker's last sweep,
    whichever process holds the worker lock: when it ran and its age in seconds, null before any (which
    process holds it, a host name and a process id, is the worker's log line's, not an open answer's); a
    process that runs its own worker answers 503, ``status: stale``, once no sweep has run for longer than
    ``worker.stale_after`` (a ``--no-worker`` one only shows it)."""
    try:
        async with ctx.pool.connection(timeout=3) as conn:
            await repo.ping(conn)
            swept = await repo.last_sweep(conn)
    except psycopg.OperationalError:  # the pool's timeout among them
        raise HTTPException(
            503, "the database is not answering, or the pool gave no connection in 3 s"
        ) from None
    sweep = swept and {
        "last_sweep_at": swept["swept_at"],
        "age_s": round(max(swept["age_s"], 0.0), 3),
    }
    stale = worker.stalled(ctx, swept and swept["age_s"])
    body = {"status": "stale" if stale else "ok", "ai": ctx.llm is not None, "auth": ctx.settings.auth_mode}
    return ok(body | {"worker": sweep}, 503 if stale else 200)


@router.get("/targets")
async def targets(ctx: Ctx, who: Viewer) -> Response:
    """Ids, declared capabilities, a database target's engine (null for a service) and where each is
    declared (``file`` or ``product``); base URLs, hosts, users and credential references stay private.
    To who declares (``declarer``: the role) a target of the product's list also shows what it was
    declared with: its address, consents, auth (header names) and budget, never a credential."""
    role = declarer(ctx.settings)
    items = [product.item(ctx.targets.get(t), role in who.roles) for t in ctx.targets]
    return ok({"items": items, "declarer": role})


@router.put("/targets/{target_id}")
async def declare(ctx: Ctx, who: Declarer, target_id: str, body: TargetIn) -> Response:
    """Declare a service in the product, or change it: its item, 201 for a new one. It holds for every
    connector opened afterwards, without a restart."""
    target, created = await product.put(ctx, target_id, body.model_dump(exclude_none=True), who)
    return ok(product.item(target, True), 201 if created else 200)


@router.delete("/targets/{target_id}", status_code=204)
async def remove(ctx: Ctx, who: Declarer, target_id: str) -> Response:
    """Remove a target of the product's list, with its credentials."""
    await product.remove(ctx, target_id, who)
    return Response(status_code=204)


@router.get("/llm/profiles")
async def llm_profiles(ctx: Ctx, who: Viewer) -> Response:
    profiles = ctx.llm.profiles.values() if ctx.llm else ()
    fields = ("profile_id", "dialect", "model", "trust")
    items = [{name: getattr(p, name) for name in fields} for p in profiles]
    return ok({"default": ctx.settings.llm_profile if ctx.llm else None, "items": items})


@router.get("/licence")
async def licence_status(ctx: Ctx, who: Viewer) -> Response:
    """The installation's licence, what it allows and its pairs, unaudited; ``state: unknown`` when it
    cannot be read: no read depends on the licence."""
    return ok(await licence.status(ctx))
