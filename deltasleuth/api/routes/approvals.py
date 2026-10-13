"""Four-eyes approvals and sign-off: list and review requests, request a
spec version's approval or an ended cycle's sign-off, decide, revoke.

People decide: ``require("rule_approver")`` refuses every non-person, an approval step needs a sign-in at
most 15 minutes old (``deps.fresh``; rejecting and revoking only restrict), the route refuses whoever is
in the request's exclusion set, and ds.tg_approvals checks every decision again. Requests need SSO:
tokens and developer modes have no approvers. In a vendor build a sign-off's request and approval steps
also need an Enterprise licence; an open build asks for none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response

from deltasleuth import approvals, licence
from deltasleuth.api.deps import (
    Ctx,
    Limit,
    Operator,
    Payload,
    RuleApprover,
    SpecAuthor,
    Viewer,
    fresh,
    masked_body,
    need,
    ok,
    run_of,
    shown,
)
from deltasleuth.approvals import ApprovalConflict
from deltasleuth.auth.principal import Principal, printable
from deltasleuth.context import Context
from deltasleuth.core.spec.errors import pointer
from deltasleuth.reports import accuracy
from deltasleuth.runner import sample
from deltasleuth.runner.purge import refuse_purged
from deltasleuth.security import rawvalues
from deltasleuth.store import repo

router = APIRouter()
NOTE_CHARS = 1000  # ds.note
REASON_CHARS = 200  # a revoke's reason, as a hold's


class NoteIn(Payload):
    note: str | None = None


class DecisionIn(Payload):
    decision: Literal["approve", "reject"]
    note: str | None = None
    content_hash: str | None = None  # a spec version's, echoed from the review
    scopes: list[str] | None = None
    attests_sha256: str | None = None  # a sign-off's


class RevokeIn(Payload):
    reason: str


def checked(text: str | None, what: str = "note", chars: int = NOTE_CHARS) -> str | None:
    if text is not None and not (1 <= len(text) <= chars and printable(text) and _utf8(text)):
        raise HTTPException(422, f"{what} must be 1 to {chars} characters without control characters")
    return text


def _utf8(text: str) -> bool:
    """False for text with no UTF-8 form (a lone surrogate), which PostgreSQL cannot store."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def _item(row: repo.Row) -> repo.Row:
    """An approval as lists show it: ``attests`` only in its review."""
    return shown(row, "attests")


# -- reading ---------------------------------------------------------------------------------------


@router.get("/approvals")
async def list_approvals(
    ctx: Ctx,
    who: Viewer,
    status: Literal["requested", "approved", "rejected", "revoked"] | None = None,
    kind: Literal["spec", "signoff"] | None = None,
    spec_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,  # a bigint
    run_id: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> Response:
    """Newest first."""
    async with ctx.pool.connection() as conn:
        rows, next_cursor = await repo.list_approvals(
            conn, status=status, kind=kind, spec_id=spec_id, run_id=run_id, limit=limit, cursor=cursor
        )
    return ok({"items": [_item(row) for row in rows], "next_cursor": next_cursor})


@router.get("/approvals/{approval_id}")
async def review(ctx: Ctx, who: Viewer, approval_id: str) -> Response:
    """The request, what it attests, its review (the needed items, the exclusion set, the earlier requests
    of its version or cycle and, for a spec version, its content hash and both bodies masked: its own and
    ``previous``'s, the name's newest approved version before it) and whether the caller can approve it
    now."""
    async with ctx.pool.connection() as conn:
        row = need(await repo.get_approval(conn, approval_id), f"approval {approval_id}")
        persons, unattributed = await _authors(conn, row)
        names = await repo.actor_names(conn, persons)
        found: dict[str, Any] = {
            "exclusion": {
                "persons": [{"actor": actor, "name": names[actor]} for actor in persons],
                "unattributed": unattributed,
            },
            "history": [_item(h) for h in await _requests(conn, row) if h["approval_id"] < approval_id],
        }
        if row["kind"] == "spec":
            stored = need(await repo.get_spec(conn, row["spec_id"]), f"spec {row['spec_id']}")
            body = await _masked(ctx, conn, stored)
            previous = await repo.previous_approved(conn, row["spec_id"])
            if previous is not None:
                previous |= {"body": await _masked(ctx, conn, previous)}
            found |= {"needed": _needed(body, row["scopes"]), "content_hash": stored["content_hash"]}
            found |= {"body": body, "previous": previous}
            if (sample := (await ctx.spec(conn, row["spec_id"])).sample) is not None:
                found["ledger"] = await _ledger(conn, sample.population)
        else:
            facts = row["attests"]
            found["needed"] = [{"rule": rule} for rule in facts["rules"]] + [
                {"root_cause": cause} for cause in facts["root_causes"]
            ]
            if drawn := facts.get("sample"):
                found["ledger"] = await _ledger(conn, drawn["population"], drawn["draw_id"])
            if "accuracy" in facts:  # attests format 3
                found |= await _labelled(conn, row)
        refusal = await _refusal(conn, row, who, persons)
    you = {"can_decide": refusal is None, "reason": refusal}
    return ok(_item(row) | {"attests": row["attests"], "review": found, "you": you})


async def _ledger(conn: repo.Conn, population: Sequence[str], draw_id: str | None = None) -> list[repo.Row]:
    """A review's ledger: every draw of the population under any name, in
    commit order, with its status and close, and whether each was made before or after ``draw_id``."""
    rows = await repo.ledger(conn, population)
    seq = next((d["draw_seq"] for d in rows if d["draw_id"] == draw_id), None)

    def order(at: int | None) -> str | None:  # None without a sample.draw row naming the draw
        if seq is None or at is None:
            return None
        return "this" if at == seq else "before" if at < seq else "after"

    return [shown(d, "strata", "frame", "selection_sha256") | {"order": order(d["draw_seq"])} for d in rows]


async def _labelled(conn: repo.Conn, row: repo.Row) -> dict[str, Any]:
    """A sign-off's review of labelled accuracy: what the labelling it attests
    says (``lines``, attested while the request is open or approved; none without one) and every labelling
    of its cycle, newest first."""
    run_id, cycle, named = row["run_id"], row["cycle"], (row["attests"]["accuracy"] or {}).get("labelling_id")
    rows = await repo.list_labellings(conn, run_id, cycle)
    attested = named and await repo.cycle_labelling(conn, run_id, cycle, named)
    active = row["approval_id"] if row["status"] in ("requested", "approved") else None
    names = ("labelling_id", "drawn_by", "drawn_at", "item_count", "labelled_by", "labelled_at")
    return {
        "lines": accuracy.lines(attested, signoff=active) if attested else [],
        "labellings": [
            {name: v[name] for name in names} | {"attested": v["labelling_id"] == named} for v in rows
        ],
    }


async def _requests(conn: repo.Conn, row: repo.Row) -> list[repo.Row]:
    """Every request of the approval's spec version or ended cycle, newest first."""
    if row["kind"] == "spec":
        return await repo.approval_history(conn, row["spec_id"])
    return await repo.approval_history(conn, run_id=row["run_id"], cycle=row["cycle"])


async def _masked(ctx: Context, conn: repo.Conn, stored: repo.Row) -> dict[str, Any]:
    """A stored spec version's body as the routes show it (``deps.masked_body``)."""
    return masked_body(stored["body"], await ctx.spec(conn, stored["spec_id"]), ctx.tokeniser)


def _needed(body: Mapping[str, Any], scopes: Sequence[str]) -> list[dict[str, Any]]:
    """What each scope covers in a spec body, by JSON pointer."""
    security, compare = body.get("security", {}), body.get("compare", {})
    filters = [
        (("sides", side, "table", name), entry["table"][name])
        for side, entry in body.get("sides", {}).items()
        for name in ("where", "columns")
        if entry.get("table", {}).get(name) is not None
    ]
    filters += [  # the first lines of each file a file side leaves unread
        (("sides", side, "table", "file", "csv", "skip"), skip)
        for side, entry in body.get("sides", {}).items()
        if (skip := ((entry.get("table", {}).get("file") or {}).get("csv") or {}).get("skip"))
    ]
    filters += [
        (("compare", name), compare[name])
        for name in ("include", "exclude", "valueFilter")
        if compare.get(name) not in (None, [])
    ]
    unlisted = [(("security", "unlisted"), "clear")] if security.get("unlisted") == "clear" else []
    source = body.get("key", {}).get("source", {})
    found: dict[str, list[tuple[tuple[str | int, ...], Any]]] = {
        "clear": [(("security", "clear", i), path) for i, path in enumerate(security.get("clear", []))],
        "unlisted_clear": unlisted,
        "known_diffs": [
            (("knownDiffs", i), rule)
            for i, rule in enumerate(body.get("knownDiffs", []))
            if rule.get("active", True)
        ],
        "population_filters": filters,
        "sampling": [(("key", "source"), source)] if source.get("kind") == "hash_sample" else [],
    }
    return [
        {"scope": scope, "pointer": pointer(at), "value": value}
        for scope in scopes
        for at, value in found.get(scope, [])
    ]


async def _authors(conn: repo.Conn, row: repo.Row) -> tuple[list[str], bool]:
    """The request's exclusion set as its next step meets it: the persons stored at earlier steps and
    those ds.approval_authors finds now, and whether an author is unattributed (two approvers)."""
    now = await repo.approval_authors(conn, row["approval_id"])
    assert now is not None
    persons = sorted({*(row["authors"] or []), *now["persons"]})
    return persons, bool(row["unattributed"]) or now["unattributed"]


async def _refusal(conn: repo.Conn, row: repo.Row, who: Principal, persons: Sequence[str]) -> str | None:
    """Why ``who`` cannot approve the request now; None when it can."""
    if "rule_approver" not in who.roles:
        return "needs the rule_approver role"
    if not who.person:
        return "the rule_approver role needs a signed-in person"
    if row["status"] != "requested":
        return f"approval {row['approval_id']} is already {row['status']}"
    if who.actor in persons:
        return f"approval needs an approver other than you: {await _why(conn, row, who)}"
    if row["first_by"] == who.actor:
        return f"you approved step 1 of {row['approval_id']}; another rule_approver must approve it"
    if row["first_by"] in persons:
        return _first_is_author(row)
    return None


def _first_is_author(row: repo.Row) -> str:
    """The request's first approver has joined its exclusion set since (a revoke moved the lineage's base
    back, or they uploaded the same content again): no second step can complete it."""
    return (
        f"step 1 of {row['approval_id']} was taken by {row['first_by_name']}, who is now an author of it;"
        " reject it and request it again"
    )


async def _why(conn: repo.Conn, row: repo.Row, who: Principal) -> str:
    """Why ``who`` is in the request's exclusion set."""
    requester = await repo.accountable(conn, row["requested_by"])
    if who.actor in requester["persons"]:
        return "you requested it" + _through(row["requested_by"], requester["name"])
    if row["kind"] == "signoff":
        creator = (await repo.get_run(conn, row["run_id"]) or {}).get("created_by")
        started = await repo.accountable(conn, creator)
        if who.actor in started["persons"]:
            return f"you started run {row['run_id']}" + _through(creator, started["name"])
        attested = row["attests"].get("adjudication")
        scored = attested and await repo.get_adjudication(conn, attested["adjudication_id"])
        if scored and scored["scored_by"] == who.actor:
            return f"you scored its adjudication {scored['adjudication_id']}"
        labelled = row["attests"].get("accuracy")
        if labelled and labelled["labelled_by"] == who.actor:
            return f"you labelled its labelling {labelled['labelling_id']}"
        ledger = (row["attests"].get("sample") or {}).get("ledger", [])
        if closed := next((d for d in ledger if d["closed_by"] == who.actor), None):
            return f"you closed draw {closed['draw_id']} of its ledger"
    uploads = await repo.uploads_by(conn, row["spec_id"], who.actor)
    if uploads:
        upload = min(uploads, key=lambda u: u["actor"] != who.actor)  # the person's own upload first
        since = "" if upload["base"] is None else f" since spec {upload['base']}"
        through = _through(upload["actor"], upload["actor_name"])
        return f"you uploaded a version of {row['spec_name']}{since}{through}"
    if row["kind"] == "signoff":  # the authors its spec approval stored: its requester, or a lineage's
        return f"you are an author of spec approval {row['attests']['spec_approval_id']}"
    return f"you were an author of it when step 1 of {row['approval_id']} was taken"  # the stored authors


def _through(actor: str | None, name: str | None) -> str:
    if actor is not None and actor.startswith("token:"):
        return f" through token {name} ({actor.removeprefix('token:')})"
    return ""


# -- requests --------------------------------------------------------------------------------------


def _sso(ctx: Context) -> None:
    if (mode := ctx.settings.auth_mode) != "oidc":
        raise ApprovalConflict(
            f"approvals need SSO (auth.oidc); this deployment runs in {approvals.MODES[mode]}"
        )


def _repeated(history: Sequence[repo.Row], what: str, request: str, done: str) -> None:
    """Refuse a second open request of a version or cycle, or one already approved."""
    for status, text in (
        ("requested", f"{what} already has an open {request} "),
        ("approved", f"{what} is already {done}: "),
    ):
        if found := next((a for a in history if a["status"] == status), None):
            raise ApprovalConflict(text + found["approval_id"])


@router.post("/specs/{spec_id}/approvals", status_code=201)
async def request_approval(ctx: Ctx, who: SpecAuthor, spec_id: int, body: NoteIn | None = None) -> Response:
    """Ask for a four-eyes approval of the spec version's whole content, recording the scopes that gate
    its runs (``approvals.needs``)."""
    note = checked(body.note if body else None)
    _sso(ctx)
    what = f"spec {spec_id}"
    async with repo.audited(ctx.pool) as (conn, audit):
        stored = need(await repo.get_spec(conn, spec_id), what)
        _repeated(await repo.approval_history(conn, spec_id), what, "request", "approved")
        scopes = approvals.needs(await ctx.spec(conn, spec_id))
        row = await repo.create_approval(conn, "spec", spec_id, who.actor, scopes=scopes, note=note)
        if row is None:  # a request committed meanwhile
            _repeated(await repo.approval_history(conn, spec_id), what, "request", "approved")
        assert row is not None
        row = await repo.get_approval(conn, row["approval_id"])
        assert row is not None
        detail = {
            "kind": "spec",
            "spec_id": spec_id,
            "content_hash": stored["content_hash"],
            "scopes": scopes,
        }
        await audit(who, "approval.request", row["approval_id"], detail)
    fields = ("approval_id", "status", "scopes", "approvers_needed")
    return ok({name: row[name] for name in fields}, 201)


@router.post("/runs/{run_id}/cycles/{cycle}/signoff", status_code=201)
async def request_signoff(
    ctx: Ctx, who: Operator, run_id: str, cycle: int, body: NoteIn | None = None
) -> Response:
    """Ask for the ended cycle to be accepted as parity evidence: refused
    with each problem it has; 410 once its purge has started."""
    note = checked(body.note if body else None)
    _sso(ctx)
    what = f"cycle {cycle} of run {run_id}"
    async with repo.audited(ctx.pool) as (conn, audit):
        await licence.need(ctx, conn, "signoff")
        run = await run_of(conn, run_id)
        history = await repo.approval_history(conn, run_id=run_id, cycle=cycle)
        _repeated(history, what, "sign-off request", "signed off")
        spec = await ctx.spec(conn, run["spec_id"])
        if problems := await approvals.signoff_problems(conn, run, cycle, spec):
            raise ApprovalConflict("; ".join(problems))
        facts = await approvals.attests(conn, run, cycle, spec)
        sha256 = approvals.attests_sha256(facts)
        row = await repo.create_approval(
            conn, "signoff", run["spec_id"], who.actor, run_id=run_id, cycle=cycle, attests=facts,
            attests_sha256=sha256, note=note,
        )  # fmt: skip
        if row is None:  # a request committed meanwhile
            _repeated(
                await repo.approval_history(conn, run_id=run_id, cycle=cycle),
                what,
                "sign-off request",
                "signed off",
            )
        assert row is not None
        row = await repo.get_approval(conn, row["approval_id"])
        assert row is not None
        detail = {
            "kind": "signoff",
            "spec_id": run["spec_id"],
            "run_id": run_id,
            "cycle": cycle,
            "attests_sha256": sha256,
        }
        await audit(who, "approval.request", row["approval_id"], detail)
    fields = ("approval_id", "status", "attests_sha256", "approvers_needed")
    return ok({name: row[name] for name in fields}, 201)


# -- decisions -------------------------------------------------------------------------------------


@router.post("/approvals/{approval_id}/decision")
async def decide(ctx: Ctx, who: RuleApprover, approval_id: str, body: DecisionIn) -> Response:
    """Approve the request (one step of two when an author is unattributed; a sign-off's last step holds
    its run) or reject it; the request as decided."""
    note, approving = checked(body.note), body.decision == "approve"
    if approving:
        fresh(who)  # an approval step needs a fresh sign-in; a rejection only restricts
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            row = need(await repo.get_approval(conn, approval_id), f"approval {approval_id}")
            if not approving and note is None:
                raise HTTPException(422, "note is required to reject")
            if approving and row["kind"] == "spec" and (body.content_hash is None or body.scopes is None):
                raise HTTPException(422, "content_hash and scopes are required to approve a spec version")
            if approving and row["kind"] == "signoff" and body.attests_sha256 is None:
                raise HTTPException(422, "attests_sha256 is required to approve a sign-off")
            if row["status"] != "requested":
                raise ApprovalConflict(f"approval {approval_id} is already {row['status']}")
            if approving:
                decided = await _approve(ctx, conn, audit, row, body, note, who)
            else:
                if await repo.decide_approval(conn, approval_id, who.actor, approve=False, note=note) is None:
                    raise await _moved(conn, approval_id)
                decided = await repo.get_approval(conn, approval_id)
                await audit(who, "approval.reject", approval_id, {"kind": row["kind"], "cascade_of": None})
    except repo.ApprovalRefused as exc:
        raise await _refused(ctx, exc, approval_id, who) from None
    return ok(_item(decided))  # type: ignore[arg-type]


async def _approve(
    ctx: Context, conn: repo.Conn, audit: repo.Audit, row: repo.Row, body: DecisionIn, note: str | None,
    who: Principal,
) -> repo.Row:  # fmt: skip
    """``who``'s approval step on the open request ``row``, audited; the request after it."""
    approval_id = row["approval_id"]
    persons, unattributed = await _authors(conn, row)
    if (refusal := await _refusal(conn, row, who, persons)) is not None:
        raise ApprovalConflict(refusal)
    if row["kind"] == "spec":
        stored = await repo.get_spec(conn, row["spec_id"])
        assert stored is not None
        await _reviewed_spec(ctx, conn, row, stored, body)
        requested = {"content_hash": stored["content_hash"], "scopes": row["scopes"]}
    else:
        await licence.need(ctx, conn, "signoff")
        run = await repo.get_run(conn, row["run_id"])
        assert run is not None
        await _reviewed_signoff(ctx, conn, row, run, body)
        requested = {"run_id": row["run_id"], "cycle": row["cycle"], "attests_sha256": row["attests_sha256"]}
    first = unattributed and row["first_by"] is None  # the first of two approvers
    decided = await repo.decide_approval(
        conn, approval_id, who.actor, approve=True, first=first, note=None if first else note
    )
    if decided is None:
        now = await repo.get_approval(conn, approval_id)
        if now is not None and now["status"] == "requested":  # another approver took step 1 meanwhile
            return await _approve(ctx, conn, audit, now, body, note, who)
        raise await _moved(conn, approval_id)
    hold, later = None, {}
    if decided["status"] == "approved" and row["kind"] == "signoff":
        hold = f"{approvals.SIGNED_OFF}{approval_id}"  # keeps what attests is rebuilt from
        if await repo.set_hold(conn, row["run_id"], hold, who.actor) is None:
            hold = None  # held already
        await refuse_purged(conn, row["run_id"], row["cycle"])  # a purge that started meanwhile: 410
        if row["attests"].get("sample"):  # R-c as this step judged it: a pack reads it
            later = {"later_draws": await sample.later_draws(conn, row["run_id"], row["cycle"])}
    step, of = (1, 2) if first else (2, 2) if row["first_by"] else (1, 1)
    shown_row = await repo.get_approval(conn, approval_id)
    assert shown_row is not None
    auth_time = who.auth_time and who.auth_time.isoformat()
    detail = {"kind": row["kind"], "spec_id": row["spec_id"], **requested}
    detail |= {"step": step, "of": of, "authors": decided["authors"], "auth_time": auth_time} | later
    await audit(who, "approval.grant", approval_id, detail)
    if hold is not None:
        await audit(who, "retention.hold", row["run_id"], {"reason": hold})
    return shown_row


async def _reviewed_spec(
    ctx: Context, conn: repo.Conn, row: repo.Row, stored: repo.Row, body: DecisionIn
) -> None:
    """Refuse an approval of other content than the version's, or of scopes its needs no longer are."""
    spec_id = row["spec_id"]
    if body.content_hash != stored["content_hash"] or sorted(body.scopes or []) != sorted(row["scopes"]):
        raise ApprovalConflict(
            f"the reviewed content hash or scopes are not spec {spec_id}'s; reload and review again"
        )
    if sorted(approvals.needs(await ctx.spec(conn, spec_id))) != sorted(row["scopes"]):
        raise ApprovalConflict(f"spec {spec_id}'s needs changed since the request; request it again")


async def _reviewed_signoff(
    ctx: Context, conn: repo.Conn, row: repo.Row, run: repo.Row, body: DecisionIn
) -> None:
    """Refuse a sign-off whose cycle no longer passes (the spec approval it cites read FOR SHARE, so a
    revoke waits or is seen) or whose facts, rebuilt by the builder of their attests format, differ from
    those requested and reviewed."""
    run_id, cycle, spec = row["run_id"], row["cycle"], await ctx.spec(conn, run["spec_id"])
    cited = row["attests"]["spec_approval_id"]
    if problems := await approvals.signoff_problems(conn, run, cycle, spec, cited=cited, lock=True):
        raise ApprovalConflict("; ".join(problems))
    rebuilt = await approvals.rebuilt(conn, run, cycle, spec, row["attests"])  # by the format it has
    if (
        body.attests_sha256 != row["attests_sha256"]
        or rebuilt is None
        or approvals.attests_sha256(rebuilt) != row["attests_sha256"]
    ):
        raise ApprovalConflict(
            f"cycle {cycle} of run {run_id} changed since the sign-off was requested; request it again"
        )


async def _moved(conn: repo.Conn, approval_id: str) -> ApprovalConflict:
    """The request was decided meanwhile."""
    now = await repo.get_approval(conn, approval_id)
    return ApprovalConflict(f"approval {approval_id} is already {now and now['status']}")


async def _refused(ctx: Context, exc: repo.ApprovalRefused, approval_id: str, who: Principal) -> Exception:
    """The 409 of a decision ds.tg_approvals refused, worded from the state after the rollback where the
    route can explain it (a change that raced its own checks); ``_HANDLERS`` words the rest."""
    async with ctx.pool.connection() as conn:
        row = await repo.get_approval(conn, approval_id)
        assert row is not None
        if exc.check == "four_eyes":
            persons, _ = await _authors(conn, row)
            if who.actor not in persons and row["first_by"] in persons:
                return ApprovalConflict(_first_is_author(row))
            return ApprovalConflict(
                f"approval needs an approver other than you: {await _why(conn, row, who)}"
            )
        if exc.check == "cited":
            return ApprovalConflict(
                f"cycle {row['cycle']} of run {row['run_id']} ran under spec approval"
                f" {row['attests']['spec_approval_id']}, since revoked"
            )
        if exc.check == "approvers" and row["first_by"] == who.actor:
            return ApprovalConflict(
                f"you approved step 1 of {approval_id}; another rule_approver must approve it"
            )
    return exc


@router.post("/approvals/{approval_id}/revoke")
async def revoke(ctx: Ctx, who: RuleApprover, approval_id: str, body: RevokeIn) -> Response:
    """Revoke an approved spec approval or sign-off, and what rests on it (``approvals.revoke``): the runs
    it paused and stopped and the sign-offs it revoked. Its reason, which audit rows and evidence packs
    carry forever, must hold no raw key-shaped value."""
    reason = checked(body.reason, "reason", REASON_CHARS)
    assert reason is not None
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            approval = need(await repo.get_approval(conn, approval_id), f"approval {approval_id}")
            if rawvalues.key_shaped(await ctx.spec(conn, approval["spec_id"]), reason):
                raise HTTPException(422, rawvalues.KEY_SHAPED)
            done = await approvals.revoke(conn, audit, approval_id, reason, who=who)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    return ok(done)
