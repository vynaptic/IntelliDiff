"""The queries of approvals."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, LiteralString

from psycopg import errors

from deltasleuth.store.repo.base import (
    Conn,
    Page,
    Row,
    _jsonb,
    _one,
    _page,
    _row,
    _rows,
    _sortable_id,
    _value,
)

# An approval with its spec's name, version and owner, its actors' names and how many people approve it:
# two when an author is unattributed (as stored at a step, else as the exclusion set stands now).
_APPROVAL_COLUMNS: LiteralString = """a.*, s.name AS spec_name, s.version AS spec_version,
    s.body->'metadata'->>'owner' AS spec_owner, ds.actor_name(a.requested_by) AS requested_by_name,
    ds.actor_name(a.first_by) AS first_by_name, ds.actor_name(a.decided_by) AS decided_by_name,
    ds.actor_name(a.revoked_by) AS revoked_by_name,
    CASE WHEN coalesce(a.unattributed, (ds.approval_authors(a)).unattributed) THEN 2 ELSE 1 END
        AS approvers_needed"""
# What each refusal of ds.tg_approvals says, and the check it names.
_TRIGGER_CHECKS = (
    ("is not an enabled rule_approver", "approver"),
    ("(four-eyes)", "four_eyes"),
    ("approver(s)", "approvers"),
    ("cites is not approved", "cited"),
)


class ApprovalRefused(Exception):
    """ds.tg_approvals refused a decision with its check_violation (23514, ``ds.approvals: …``), which
    the API answers 409, never 500. ``check`` names the check: ``approver`` (the decider is not an
    enabled rule_approver), ``four_eyes`` (the decider is in the exclusion set), ``approvers`` (the
    request needs another number of approvers) or ``cited`` (a sign-off's spec approval is not
    approved)."""

    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check


@asynccontextmanager
async def _trigger_checks() -> AsyncIterator[None]:
    """ApprovalRefused for the trigger's refusals; any other error passes."""
    try:
        yield
    except errors.CheckViolation as exc:
        message = exc.diag.message_primary or ""
        checks = [check for part, check in _TRIGGER_CHECKS if part in message]
        if not message.startswith("ds.approvals: ") or not checks:
            raise
        raise ApprovalRefused(checks[0], message) from None


async def create_approval(
    conn: Conn,
    kind: str,
    spec_id: int,
    requested_by: str,
    *,
    scopes: Sequence[str] = (),
    run_id: str | None = None,
    cycle: int | None = None,
    attests: Mapping[str, Any] | None = None,
    attests_sha256: str | None = None,
    note: str | None = None,
) -> Row | None:
    """A request (``A-`` id) to approve a spec version's content (its ``scopes``) or to sign off an ended
    cycle (its ``attests`` and their hash); None when the version or cycle has an open request already."""
    return await _row(
        conn,
        """INSERT INTO ds.approvals (approval_id, kind, spec_id, run_id, cycle, scopes, attests,
               attests_sha256, requested_by, request_note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT DO NOTHING RETURNING *""",
        (
            _sortable_id("A-"), kind, spec_id, run_id, cycle, list(scopes),
            None if attests is None else _jsonb(attests), attests_sha256, requested_by, note,
        ),
    )  # fmt: skip


async def get_approval(conn: Conn, approval_id: str, *, lock: bool = False) -> Row | None:
    """The approval with its spec's name, version and owner and its actors' names. ``lock`` reads it
    FOR SHARE: a revoke then waits for this transaction, or this read waits for the revoke and finds
    it revoked."""
    return await _row(
        conn,
        f"""SELECT {_APPROVAL_COLUMNS} FROM ds.approvals a JOIN ds.specs s USING (spec_id)
            WHERE a.approval_id = %s {"FOR SHARE OF a" if lock else ""}""",  # noqa: S608 - fixed SQL fragments only
        (approval_id,),
    )


async def active_spec_approval(conn: Conn, spec_id: int, *, lock: bool = False) -> Row | None:
    """The spec version's approved spec approval, if any; ``lock`` reads it FOR SHARE, as ``get_approval``."""
    return await _row(
        conn,
        f"""SELECT * FROM ds.approvals WHERE spec_id = %s AND kind = 'spec' AND status = 'approved'
            {"FOR SHARE" if lock else ""}""",  # noqa: S608 - fixed SQL fragments only
        (spec_id,),
    )


async def approval_history(
    conn: Conn, spec_id: int | None = None, *, run_id: str | None = None, cycle: int | None = None
) -> list[Row]:
    """Every request of a spec version (``spec_id``), or of an ended cycle (``run_id`` and ``cycle``),
    newest first, as ``get_approval`` shows them."""
    return await _rows(
        conn,
        f"""SELECT {_APPROVAL_COLUMNS} FROM ds.approvals a JOIN ds.specs s USING (spec_id)
            WHERE (%(run_id)s::text IS NULL AND a.kind = 'spec' AND a.spec_id = %(spec_id)s)
               OR (a.kind = 'signoff' AND a.run_id = %(run_id)s AND a.cycle = %(cycle)s)
            ORDER BY a.approval_id DESC""",  # noqa: S608 - fixed SQL fragments only
        {"spec_id": spec_id, "run_id": run_id, "cycle": cycle},
    )


async def list_approvals(
    conn: Conn,
    *,
    status: str | None = None,
    kind: str | None = None,
    spec_id: int | None = None,
    run_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Newest first, as ``get_approval`` shows them."""
    rows = await _rows(
        conn,
        f"""SELECT {_APPROVAL_COLUMNS} FROM ds.approvals a JOIN ds.specs s USING (spec_id)
            WHERE (%(status)s::text IS NULL OR a.status = %(status)s)
              AND (%(kind)s::text IS NULL OR a.kind = %(kind)s)
              AND (%(spec_id)s::bigint IS NULL OR a.spec_id = %(spec_id)s)
              AND (%(run_id)s::text IS NULL OR a.run_id = %(run_id)s)
              AND (%(after)s::text IS NULL OR a.approval_id < %(after)s)
            ORDER BY a.approval_id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {
            "status": status,
            "kind": kind,
            "spec_id": spec_id,
            "run_id": run_id,
            "after": cursor,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "approval_id")


async def approval_authors(conn: Conn, approval_id: str) -> Row | None:
    """The exclusion set the trigger computes for the approval's next step, as it stands now:
    ``persons`` who may not approve it, and whether an author is ``unattributed`` (then two
    approvers outside the set must)."""
    return await _row(
        conn,
        """SELECT x.persons, x.unattributed FROM ds.approvals a, ds.approval_authors(a) x
           WHERE a.approval_id = %s""",
        (approval_id,),
    )


async def uploads_by(conn: Conn, spec_id: int, actor: str) -> list[Row]:
    """The uploads that put ``actor`` in the spec version's lineage (ds.spec_accountable: every upload of
    the name's versions after its newest approved version before this one, up to this one), each the
    uploading ``actor`` (a person, or a token it created or owns) with its ``actor_name``, and ``base``,
    that approved version (None when there is none)."""
    return await _rows(
        conn,
        """WITH this AS (SELECT spec_id, name FROM ds.specs WHERE spec_id = %(spec_id)s),
           base AS (
               SELECT max(s.spec_id) AS id FROM ds.specs s JOIN this ON s.name = this.name
               WHERE s.spec_id < this.spec_id AND EXISTS (
                   SELECT 1 FROM ds.approvals a WHERE a.spec_id = s.spec_id AND a.kind = 'spec'
                                                  AND a.status = 'approved')),
           versions AS (
               SELECT s.spec_id, s.created_by FROM ds.specs s JOIN this ON s.name = this.name, base
               WHERE s.spec_id > coalesce(base.id, 0) AND s.spec_id <= this.spec_id),
           uploads AS (
               SELECT v.created_by AS actor FROM versions v
               UNION ALL
               SELECT l.actor FROM versions v
               JOIN ds.audit_log l ON l.object = v.spec_id::text AND l.action = 'spec.create')
           SELECT DISTINCT u.actor, ds.actor_name(u.actor) AS actor_name, base.id AS base
           FROM uploads u, base WHERE %(actor)s = ANY (ds.accountable(u.actor))
           ORDER BY u.actor""",
        {"spec_id": spec_id, "actor": actor},
    )


async def previous_approved(conn: Conn, spec_id: int) -> Row | None:
    """The name's newest version before the spec version with an approved spec approval: its spec_id,
    version, body and approval_id; None when there is none."""
    return await _row(
        conn,
        """SELECT s.spec_id, s.version, s.body, a.approval_id
           FROM ds.specs s JOIN ds.approvals a ON a.spec_id = s.spec_id AND a.kind = 'spec'
                                              AND a.status = 'approved'
           WHERE s.name = (SELECT name FROM ds.specs WHERE spec_id = %(spec_id)s) AND s.spec_id < %(spec_id)s
           ORDER BY s.spec_id DESC LIMIT 1""",
        {"spec_id": spec_id},
    )


async def accountable(conn: Conn, actor: str | None) -> Row:
    """Whom ``actor`` makes accountable (ds.accountable: a person itself; a token the person who created
    it and its owner) as ``persons``, and its ``name``."""
    return await _one(
        conn,
        "SELECT ds.accountable(%(actor)s) AS persons, ds.actor_name(%(actor)s) AS name",
        {"actor": actor},
    )


async def actor_names(conn: Conn, actors: Sequence[str]) -> dict[str, str]:
    """Each actor's name (ds.actor_name: a user's username, a token's name, else the actor itself)."""
    rows = await _rows(
        conn, "SELECT a, ds.actor_name(a) AS name FROM unnest(%s::text[]) AS a", (list(actors),)
    )
    return {row["a"]: row["name"] for row in rows}


async def decide_approval(
    conn: Conn, approval_id: str, by: str, *, approve: bool, first: bool = False, note: str | None = None
) -> Row | None:
    """``by``'s decision on an open request: approve it (``first``: as the first of two approvers, whose
    note is not kept), or reject it; the row as the trigger completed it. None when the request is no
    longer open (a first approval: or has one already). ApprovalRefused when the trigger refuses."""
    if approve and first:
        sql: LiteralString = """UPDATE ds.approvals SET first_by = %(by)s, first_at = now()
            WHERE approval_id = %(approval_id)s AND status = 'requested' AND first_by IS NULL RETURNING *"""
    else:
        sql = """UPDATE ds.approvals SET status = %(status)s, decided_by = %(by)s, decided_at = now(),
                     decision_note = %(note)s
                 WHERE approval_id = %(approval_id)s AND status = 'requested' RETURNING *"""
    status = "approved" if approve else "rejected"
    async with _trigger_checks():
        return await _row(conn, sql, {"approval_id": approval_id, "by": by, "note": note, "status": status})


async def revoke_approval(conn: Conn, approval_id: str, by: str, reason: str) -> Row | None:
    """Revoke an approved approval, taking its row lock; None when it is not approved."""
    return await _row(
        conn,
        """UPDATE ds.approvals SET status = 'revoked', revoked_by = %s, revoked_at = now(), revoke_reason = %s
           WHERE approval_id = %s AND status = 'approved' RETURNING *""",
        (by, reason, approval_id),
    )


async def approval_runs(conn: Conn, approval_id: str) -> list[Row]:
    """The running and queued runs that started, resumed or attached under the approval, locked, in
    order: run_id, mode, status."""
    return await _rows(
        conn,
        """SELECT run_id, mode, status FROM ds.runs
           WHERE approval_id = %s AND status IN ('running', 'queued') ORDER BY run_id FOR UPDATE""",
        (approval_id,),
    )


async def signoffs_citing(conn: Conn, approval_id: str) -> list[Row]:
    """The open and approved sign-offs citing the spec approval, locked, in order."""
    return await _rows(
        conn,
        """SELECT * FROM ds.approvals
           WHERE kind = 'signoff' AND attests->>'spec_approval_id' = %s
             AND status IN ('requested', 'approved')
           ORDER BY approval_id FOR UPDATE""",
        (approval_id,),
    )


async def release_signoff_hold(conn: Conn, run_id: str) -> str | None:
    """Release the run's hold a sign-off placed (``signed off: A-…``) once no approved sign-off of the
    run is left; the released reason, None when nothing was released."""
    return await _value(
        conn,
        """UPDATE ds.runs r SET hold_reason = NULL, hold_by = NULL, hold_at = NULL
           FROM ds.runs old
           WHERE r.run_id = %(run_id)s AND old.run_id = r.run_id AND old.hold_reason LIKE 'signed off: %%'
             AND NOT EXISTS (SELECT 1 FROM ds.approvals a
                             WHERE a.run_id = r.run_id AND a.kind = 'signoff' AND a.status = 'approved')
           RETURNING old.hold_reason""",
        {"run_id": run_id},
    )


async def set_run_approval(
    conn: Conn, run_id: str, approval_id: str | None, *, from_cycle: int | None = None
) -> None:
    """The spec approval the run resumed or attached under (None: none, as outside SSO). ``from_cycle``,
    when given, is the first cycle it covers (``params["approval"]["from_cycle"]``): ``end_cycle`` records
    none for an earlier one. The rest of ``params["approval"]`` stays as the run's creation wrote it."""
    await conn.execute(
        """UPDATE ds.runs SET approval_id = %(approval_id)s, updated_at = now(),
               params = CASE WHEN %(from_cycle)s::int IS NULL THEN params
                        ELSE jsonb_set(params, '{approval,from_cycle}', to_jsonb(%(from_cycle)s::int)) END
           WHERE run_id = %(run_id)s""",
        {"approval_id": approval_id, "from_cycle": from_cycle, "run_id": run_id},
    )
