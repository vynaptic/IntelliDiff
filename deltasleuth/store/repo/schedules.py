"""The queries of schedules."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, LiteralString

from deltasleuth.store.repo.base import (
    Conn,
    Page,
    Row,
    _after,
    _changed,
    _jsonb,
    _one,
    _page,
    _row,
    _rows,
    _sortable_id,
    _value,
)

# A schedule with its ``state``, the database's ``now``, its actors' names, what a firing would run now
# (``runs_spec_id``, ``runs_version``: its pinned version, else the name's newest), the name's newest stored
# version (``newest_spec_id``, ``newest_version``) and the status of the newest run it started.
_SCHEDULE: LiteralString = """SELECT s.*, now() AS now,
        CASE WHEN s.deleted_at IS NOT NULL THEN 'deleted' WHEN s.paused_at IS NOT NULL THEN 'paused'
             WHEN s.next_due IS NULL THEN 'ended' ELSE 'active' END AS state,
        ds.actor_name(s.created_by) AS created_by_name, ds.actor_name(s.set_by) AS set_by_name,
        ds.actor_name(s.paused_by) AS paused_by_name, v.spec_id AS runs_spec_id, v.version AS runs_version,
        n.spec_id AS newest_spec_id, n.version AS newest_version, r.status AS last_run_status
    FROM ds.schedules s
    LEFT JOIN LATERAL (SELECT spec_id, version FROM ds.specs WHERE name = s.spec_name
                       ORDER BY spec_id DESC LIMIT 1) n ON true
    LEFT JOIN ds.specs v ON v.spec_id = coalesce(s.spec_id, n.spec_id)
    LEFT JOIN ds.runs r ON r.run_id = s.last_run_id"""
# What a person's write does to what the schedule held and last settled (``last``: None keeps it).
_SETTLED: LiteralString = """held = NULL, last = coalesce(%(last)s, last),
    last_at = CASE WHEN %(last)s::jsonb IS NULL THEN last_at ELSE now() END"""
FIRINGS = ("schedule.fire", "schedule.missed", "schedule.rezone")  # the audit rows a schedule's history is
_SCHEDULES_LOCK = 0x44535343  # pg advisory lock "DSSC": creates of schedules, counted against their limit


def new_schedule_id() -> str:
    return _sortable_id("SC-")


async def insert_schedule(conn: Conn, schedule: Mapping[str, Any]) -> Row:
    """Store an active schedule, set by its creator: schedule_id, name, spec_name, spec_id (None: the
    name is followed), run, cron, time_zone, start_within, ends_at, auth_mode, created_by, anchor_at and
    next_due. The stored row."""
    return await _one(
        conn,
        """INSERT INTO ds.schedules (schedule_id, name, spec_name, spec_id, run, cron, time_zone,
               start_within, ends_at, auth_mode, created_by, set_by, anchor_at, next_due)
           VALUES (%(schedule_id)s, %(name)s, %(spec_name)s, %(spec_id)s, %(run)s, %(cron)s, %(time_zone)s,
               %(start_within)s, %(ends_at)s, %(auth_mode)s, %(created_by)s, %(created_by)s, %(anchor_at)s,
               %(next_due)s)
           RETURNING *""",
        dict(schedule) | {"run": _jsonb(schedule["run"])},
    )


async def get_schedule(conn: Conn, ident: str, *, lock: bool = False) -> Row | None:
    """The schedule of that id, or the live one of that name (``_SCHEDULE``); ``lock`` takes its row lock
    first, until the transaction ends."""
    where: LiteralString = "schedule_id = %(ident)s OR (name = %(ident)s AND deleted_at IS NULL)"
    if lock:
        await conn.execute(f"SELECT 1 FROM ds.schedules WHERE {where} FOR UPDATE", {"ident": ident})  # noqa: S608
    return await _row(
        conn,
        f"{_SCHEDULE} WHERE s.schedule_id = %(ident)s OR (s.name = %(ident)s AND s.deleted_at IS NULL)",
        {"ident": ident},
    )


async def list_schedules(
    conn: Conn,
    *,
    spec_name: str | None = None,
    state: str | None = None,
    set_by: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """The schedules newest first (``_SCHEDULE``): one join, no spec compiled. Those of ``state`` (active,
    paused, ended or deleted; None: all but the deleted), of the spec name and last saved by ``set_by``,
    if given."""
    rows = await _rows(
        conn,
        f"""SELECT * FROM ({_SCHEDULE}) s
            WHERE (%(spec_name)s::text IS NULL OR spec_name = %(spec_name)s)
              AND (state = %(state)s OR (%(state)s::text IS NULL AND state <> 'deleted'))
              AND (%(set_by)s::text IS NULL OR set_by = %(set_by)s)
              AND (%(after)s::text IS NULL OR schedule_id < %(after)s)
            ORDER BY schedule_id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"spec_name": spec_name, "state": state, "set_by": set_by, "after": cursor, "limit": limit + 1},
    )
    return _page(rows, limit, "schedule_id")


async def count_schedules(conn: Conn, *, lock: bool = False) -> Row:
    """How many schedules are not deleted (``count``), and the database's ``now``; ``lock`` first takes the
    schedules lock until the transaction ends, so that creates count one at a time."""
    if lock:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEDULES_LOCK,))
    return await _one(
        conn, "SELECT count(*) AS count, now() AS now FROM ds.schedules WHERE deleted_at IS NULL"
    )


async def overdue_schedules(conn: Conn, minutes: int) -> int:
    """How many schedules were due over ``minutes`` ago and are not settled (schedules_due_idx)."""
    return await _value(
        conn,
        "SELECT count(*) FROM ds.schedules WHERE next_due <= now() - %s * interval '1 minute'",
        (minutes,),
    )


async def replace_schedule(
    conn: Conn,
    schedule_id: str,
    revision: int,
    schedule: Mapping[str, Any],
    *,
    by: str,
    last: Mapping[str, Any] | None = None,
) -> Row | None:
    """Replace the definition of the schedule seen at ``revision`` (``insert_schedule``'s members but its
    id and creator), set by ``by``; ``last``: what the write settled. None when it is deleted or was
    written since."""
    return await _row(
        conn,
        f"""UPDATE ds.schedules SET name = %(name)s, spec_name = %(spec_name)s, spec_id = %(spec_id)s,
                run = %(run)s, cron = %(cron)s, time_zone = %(time_zone)s, start_within = %(start_within)s,
                ends_at = %(ends_at)s, auth_mode = %(auth_mode)s, revision = revision + 1, set_by = %(by)s,
                set_at = now(), anchor_at = %(anchor_at)s, next_due = %(next_due)s, {_SETTLED}
            WHERE schedule_id = %(schedule_id)s AND revision = %(revision)s AND deleted_at IS NULL
            RETURNING *""",  # noqa: S608 - fixed SQL fragments only
        dict(schedule)
        | {"run": _jsonb(schedule["run"]), "schedule_id": schedule_id, "revision": revision, "by": by}
        | {"last": None if last is None else _jsonb(last)},
    )


async def pause_schedule(
    conn: Conn,
    schedule_id: str,
    by: str,
    *,
    revision: int,
    anchor: datetime | None = None,
    last: Mapping[str, Any] | None = None,
) -> Row | None:
    """Pause the schedule as ``by``, at ``revision`` (a person's write is one more, the worker's the same):
    nothing is due while it is paused. ``anchor``, ``last``: the time and outcome the pause settled. None
    when it is paused or deleted."""
    return await _row(
        conn,
        f"""UPDATE ds.schedules SET paused_by = %(by)s, paused_at = now(), next_due = NULL,
                revision = %(revision)s, anchor_at = coalesce(%(anchor)s, anchor_at), {_SETTLED}
            WHERE schedule_id = %(schedule_id)s AND paused_at IS NULL AND deleted_at IS NULL
            RETURNING *""",  # noqa: S608 - fixed SQL fragments only
        {"schedule_id": schedule_id, "by": by, "revision": revision, "anchor": anchor,
         "last": None if last is None else _jsonb(last)},
    )  # fmt: skip


async def resume_schedule(
    conn: Conn, schedule_id: str, *, anchor_at: datetime, next_due: datetime | None
) -> Row | None:
    """Resume the paused schedule, anchored at ``anchor_at`` and next due at ``next_due`` (None: it has
    ended); None when it is not paused."""
    return await _row(
        conn,
        """UPDATE ds.schedules SET paused_by = NULL, paused_at = NULL, revision = revision + 1,
               anchor_at = %s, next_due = %s
           WHERE schedule_id = %s AND paused_at IS NOT NULL AND deleted_at IS NULL RETURNING *""",
        (anchor_at, next_due, schedule_id),
    )


async def delete_schedule(
    conn: Conn,
    schedule_id: str,
    by: str,
    *,
    anchor: datetime | None = None,
    last: Mapping[str, Any] | None = None,
) -> Row | None:
    """Leave the schedule's tombstone, by ``by``: its name is free and its runs still name it. ``anchor``,
    ``last``: the time and outcome the delete settled. None when it is deleted already."""
    return await _row(
        conn,
        f"""UPDATE ds.schedules SET deleted_by = %(by)s, deleted_at = now(), next_due = NULL,
                revision = revision + 1, anchor_at = coalesce(%(anchor)s, anchor_at), {_SETTLED}
            WHERE schedule_id = %(schedule_id)s AND deleted_at IS NULL RETURNING *""",  # noqa: S608
        {"schedule_id": schedule_id, "by": by, "anchor": anchor,
         "last": None if last is None else _jsonb(last)},
    )  # fmt: skip


async def schedules_due(conn: Conn) -> list[Row]:
    """The schedules the worker looks at now, oldest due first, each with its ``next_due`` and the
    database's ``now``: those due (schedules_due_idx), and those holding a standing refusal's range whose
    first time is 24 hours old (``next_due`` may then be later, or None)."""
    return await _rows(
        conn,
        """SELECT schedule_id, next_due, now() AS now FROM ds.schedules WHERE next_due <= now()
           UNION
           SELECT schedule_id, next_due, now() FROM ds.schedules
           WHERE deleted_at IS NULL AND (held->>'due')::timestamptz <= now() - interval '24 hours'
           ORDER BY next_due, schedule_id""",
    )


async def settle_schedule(
    conn: Conn,
    schedule_id: str,
    *,
    due: datetime,
    revision: int,
    anchor: datetime,
    next_due: datetime | None,
    last: Mapping[str, Any],
    held: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    closes: datetime | None = None,
) -> int | None:
    """Settle the due time ``due`` of the schedule seen at ``revision``: ``anchor`` is the last time
    settled, ``next_due`` the one due next (None: it has ended), ``last`` the outcome's detail and
    ``held`` the standing refusal's range not yet chained. A firing gives the run it started (``run_id``)
    and its window's end (``closes``), judged by the clock as the statement runs. How many seconds after
    ``due`` it was settled (in ``last`` as ``late_s`` for a run); None, nothing moved, when that time is
    no longer due, the schedule was written, paused or deleted since, or the window has closed."""
    return await _value(
        conn,
        """UPDATE ds.schedules SET anchor_at = %(anchor)s, next_due = %(next_due)s, held = %(held)s,
               last_at = now(), last_run_id = coalesce(%(run_id)s, last_run_id),
               last = %(last)s::jsonb || CASE WHEN %(run_id)s::text IS NULL THEN '{}'::jsonb ELSE
                   jsonb_build_object('late_s', greatest(0, floor(extract(epoch FROM clock_timestamp()
                                                                          - %(due)s))::int)) END
           WHERE schedule_id = %(schedule_id)s AND next_due = %(due)s AND revision = %(revision)s
             AND paused_at IS NULL AND deleted_at IS NULL
             AND (%(closes)s::timestamptz IS NULL OR clock_timestamp() < %(closes)s)
           RETURNING greatest(0, floor(extract(epoch FROM clock_timestamp() - %(due)s))::int)""",
        {"schedule_id": schedule_id, "due": due, "revision": revision, "anchor": anchor,
         "next_due": next_due, "last": _jsonb(last), "held": None if held is None else _jsonb(held),
         "run_id": run_id, "closes": closes},
    )  # fmt: skip


async def release_held(conn: Conn, schedule_id: str, held: Mapping[str, Any]) -> bool:
    """Clear the range the schedule holds, to be chained in this transaction; False when it holds another
    by now."""
    return bool(
        await _changed(
            conn,
            "UPDATE ds.schedules SET held = NULL WHERE schedule_id = %s AND held = %s",
            (schedule_id, _jsonb(held)),
        )
    )


async def rezone_schedule(
    conn: Conn, schedule_id: str, revision: int, was: datetime, to: datetime | None
) -> bool:
    """Move the next due time of the schedule seen at ``revision`` from ``was`` to ``to``, as this build's
    tz data reads its cron; False when it was settled or written since."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.schedules SET next_due = %s
               WHERE schedule_id = %s AND revision = %s AND next_due = %s""",
            (to, schedule_id, revision, was),
        )
    )


async def setter_live(conn: Conn, set_by: str) -> bool:
    """Whether the actor who last saved a schedule still answers for it: a person is enabled and
    their stored roles hold operator; a token's accountable person, if it has one, is not disabled (its
    own expiry and revoke are not read: rotation stops nothing); any other actor is."""
    return await _value(
        conn,
        """SELECT CASE split_part(%(actor)s, ':', 1)
               WHEN 'user' THEN EXISTS (
                   SELECT 1 FROM ds.users WHERE user_id = split_part(%(actor)s, ':', 2)
                                            AND disabled_at IS NULL AND 'operator' = ANY (roles))
               WHEN 'token' THEN NOT EXISTS (
                   SELECT 1 FROM ds.api_tokens t JOIN ds.users u ON u.user_id = t.owner_user_id
                   WHERE t.token_id = split_part(%(actor)s, ':', 2) AND u.disabled_at IS NOT NULL)
               ELSE true END""",
        {"actor": set_by},
    )


async def schedule_firings(
    conn: Conn, schedule_id: str, *, limit: int = 20, cursor: str | None = None
) -> Page:
    """The schedule's ``schedule.fire``, ``schedule.missed`` and ``schedule.rezone`` rows, newest first
    (audit_log_object_idx): each row's ``id``, chain ``seq``, ``at``, ``action`` and ``detail`` (ids,
    times and closed codes: no actor, address or request data)."""
    rows = await _rows(
        conn,
        """SELECT l.id, (SELECT c.seq FROM ds.audit_chain c WHERE c.id = l.id) AS seq, l.at, l.action,
                  l.detail
           FROM ds.audit_log l
           WHERE l.object = %(object)s AND l.action = ANY(%(actions)s)
             AND (%(before)s < 0 OR l.id < %(before)s)
           ORDER BY l.id DESC LIMIT %(limit)s""",
        {"object": schedule_id, "actions": list(FIRINGS), "before": _after(cursor, 1)[0], "limit": limit + 1},
    )
    return _page(rows, limit, "id")
