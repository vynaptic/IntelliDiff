"""The queries of runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from deltasleuth.store.repo.base import (
    _CENSUS_PENDING,
    _LIVE,
    TERMINAL,
    Conn,
    Page,
    Row,
    _after,
    _changed,
    _current,
    _jsonb,
    _page,
    _row,
    _rows,
    _sortable_id,
    _value,
)
from deltasleuth.store.repo.datakeys import insert_data_key


def new_run_id() -> str:
    """An ``R-`` id. A run's id comes before its row, as its sealed values are bound to it."""
    return _sortable_id("R-")


async def _insert_keys(
    conn: Conn,
    run_id: str,
    cycle: int,
    keys: Mapping[str, bytes],
    available_at: datetime | None,
    first_seq: int = 0,
    labels: Sequence[tuple[str | None, str | None, bool]] | None = None,
    delays: Sequence[float] | None = None,
) -> None:
    """``labels``: a sample's (stratum, signoff_stratum, signoff_member) of each key, in order.
    ``delays``: the seconds after ``available_at`` (None: the database's clock) each key falls due, in
    order."""
    strata, signoffs, members = zip(*labels, strict=True) if labels else (None, None, None)
    await conn.execute(
        """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, available_at, stratum,
               signoff_stratum, signoff_member)
           SELECT %s, %s, k.masked, k.enc, %s + k.n - 1,
               coalesce(%s::timestamptz, now()) + coalesce(k.delay, 0) * interval '1 second', k.stratum,
               k.signoff, coalesce(k.member, false)
           FROM unnest(%s::text[], %s::bytea[], %s::text[], %s::text[], %s::boolean[], %s::float8[])
               WITH ORDINALITY AS k (masked, enc, stratum, signoff, member, delay, n)""",
        (
            run_id, cycle, first_seq, available_at, list(keys), list(keys.values()),
            None if strata is None else list(strata), None if signoffs is None else list(signoffs),
            None if members is None else list(members), None if delays is None else list(delays),
        ),
    )  # fmt: skip


async def create_run(
    conn: Conn,
    spec_id: int,
    mode: str,
    keys: Mapping[str, bytes],
    params: Mapping[str, Any],
    scan: Mapping[str, Any] | None = None,
    *,
    run_id: str,
    created_by: str,
    retention: str,
    data_key: tuple[str, bytes],
    approval_id: str | None = None,
    keys_cut: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Insert the queued run ``run_id`` (from ``new_run_id``) of retention class ``retention``, its data
    key (the KEK id and the key it wraps) and its cycle-1 keys (masked key -> key_enc, in manifest
    order); a table scan's run starts with no keys and its ``scan`` state. ``approval_id`` is the spec
    approval it starts under; ``keys_cut`` ``{resolved, taken}`` when maxKeys cut its key set. Its
    ``params`` as stored (jsonb keeps 1e16 as 10000000000000000, say), which a pack's plan hashes."""
    stored: dict[str, Any] = await _value(
        conn,
        """INSERT INTO ds.runs (run_id, spec_id, mode, params, created_by, retention, approval_id, keys_cut)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING params""",
        (
            run_id, spec_id, mode, _jsonb(params), created_by, retention, approval_id,
            None if keys_cut is None else _jsonb(keys_cut),
        ),
    )  # fmt: skip
    await insert_data_key(conn, run_id, *data_key)
    if scan is not None:
        await conn.execute(
            "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, 1, %s)", (run_id, _jsonb(scan))
        )
    await _insert_keys(conn, run_id, 1, keys, None)
    return stored


async def save_scan(
    conn: Conn,
    run_id: str,
    cycle: int,
    scan: Mapping[str, Any],
    keys: Mapping[str, bytes],
    saved_checkpoint: int,
    delays: Mapping[str, float] | None = None,
) -> bool:
    """A checkpoint of the cycle's scan on checkpoint ``saved_checkpoint``: its state (``after``
    sealed), written as the next checkpoint, whose ``candidates`` counts ``keys`` (masked ->
    key_enc), and those keys; False, saving nothing, when the run is no longer running or that
    checkpoint is no longer the latest (another scanner saved). ``delays``: the seconds after this save,
    on the database's clock, that each masked key's per-key read falls due (None: at once)."""
    if not await _changed(
        conn,
        """UPDATE ds.scans sc SET state = %s FROM ds.runs r
           WHERE sc.run_id = %s AND sc.cycle = %s AND r.run_id = sc.run_id AND r.status = 'running'
             AND (sc.state->>'checkpoint')::int = %s""",
        (_jsonb(dict(scan) | {"checkpoint": saved_checkpoint + 1}), run_id, cycle, saved_checkpoint),
    ):
        return False
    waits = None if delays is None else [delays[masked] for masked in keys]
    await _insert_keys(conn, run_id, cycle, keys, None, scan["candidates"] - len(keys), delays=waits)
    return True


async def get_scan(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The cycle's scan state; None when the cycle did not scan."""
    return await _value(conn, "SELECT state FROM ds.scans WHERE run_id = %s AND cycle = %s", (run_id, cycle))


async def run_scans(conn: Conn, run_id: str) -> dict[int, Row]:
    """Each scanned cycle's scan state, by cycle."""
    rows = await _rows(conn, "SELECT cycle, state FROM ds.scans WHERE run_id = %s", (run_id,))
    return {row["cycle"]: row["state"] for row in rows}


async def cycle_scans(conn: Conn, run_id: str, cycles: Sequence[int]) -> dict[int, Row]:
    """The scan state of each of ``cycles`` that scanned, by cycle."""
    rows = await _rows(
        conn,
        "SELECT cycle, state FROM ds.scans WHERE run_id = %s AND cycle = ANY(%s)",
        (run_id, list(cycles)),
    )
    return {row["cycle"]: row["state"] for row in rows}


async def replace_graph_diffs(conn: Conn, run_id: str, cycle: int, rows: Sequence[Mapping[str, Any]]) -> None:
    """The cycle's graph parity rows, masked by the caller, replacing any before."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("DELETE FROM ds.graph_diffs WHERE run_id = %s AND cycle = %s", (run_id, cycle))
        await cur.executemany(
            """INSERT INTO ds.graph_diffs (run_id, cycle, seq, code, label, count_a, count_b, only_a, only_b,
                   detail)
               VALUES (%(run_id)s, %(cycle)s, %(seq)s, %(code)s, %(label)s, %(count_a)s, %(count_b)s,
                   %(only_a)s, %(only_b)s, %(detail)s)""",
            [
                {**row, "run_id": run_id, "cycle": cycle, "seq": seq, "detail": _jsonb(row["detail"])}
                for seq, row in enumerate(rows)
            ],
        )


async def list_graph_diffs(
    conn: Conn, run_id: str, cycle: int | None = None, *, limit: int = 1000, cursor: str | None = None
) -> Page:
    """The cycle's graph parity rows in their order."""
    after = _after(cursor, 1)
    rows = await _rows(
        conn,
        """SELECT seq, code, label, count_a, count_b, only_a, only_b, detail FROM ds.graph_diffs
           WHERE run_id = %s AND cycle = %s AND seq > %s ORDER BY seq LIMIT %s""",
        (run_id, await _current(conn, run_id, cycle), after[0], limit + 1),
    )
    return _page(rows, limit, "seq")


async def unmatched_keys(conn: Conn, run_id: str, cycle: int) -> list[Row]:
    """The keys (key_masked, key_enc) whose result in the cycle is not MATCH."""
    return await _rows(
        conn,
        """SELECT k.key_masked, k.key_enc
           FROM ds.results r JOIN ds.run_keys k USING (run_id, cycle, key_masked)
           WHERE r.run_id = %s AND r.cycle = %s AND r.outcome <> 'MATCH'""",
        (run_id, cycle),
    )


async def get_run(conn: Conn, run_id: str) -> Row | None:
    """The run with its spec's ``spec_name`` and ``spec_version``, its current cycle's ``scan``, and
    ``created_by_name``, the creator's name."""
    return await _row(
        conn,
        """SELECT r.*, s.name AS spec_name, s.version AS spec_version, sc.state AS scan,
                  ds.actor_name(r.created_by) AS created_by_name
           FROM ds.runs r JOIN ds.specs s USING (spec_id)
           LEFT JOIN ds.scans sc ON sc.run_id = r.run_id AND sc.cycle = r.cycle
           WHERE r.run_id = %s""",
        (run_id,),
    )


async def list_runs(
    conn: Conn,
    *,
    spec_id: int | None = None,
    status: str | None = None,
    schedule_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Newest first, as ``get_run`` shows them but a file side's sealed pins; ``schedule_id``: the runs that
    schedule started (runs_schedule_due_idx)."""
    rows = await _rows(
        conn,
        """SELECT r.*, s.name AS spec_name, s.version AS spec_version, sc.state AS scan,
                  ds.actor_name(r.created_by) AS created_by_name
           FROM ds.runs r JOIN ds.specs s USING (spec_id)
           LEFT JOIN ds.scans sc ON sc.run_id = r.run_id AND sc.cycle = r.cycle
           WHERE (%(spec_id)s::bigint IS NULL OR r.spec_id = %(spec_id)s)
             AND (%(status)s::text IS NULL OR r.status = %(status)s)
             AND (%(schedule_id)s::text IS NULL OR (
                     r.params ? 'schedule' AND r.params->'schedule'->>'schedule_id' = %(schedule_id)s))
             AND (%(after)s::text IS NULL OR r.run_id < %(after)s)
           ORDER BY r.run_id DESC LIMIT %(limit)s""",
        {"spec_id": spec_id, "status": status, "schedule_id": schedule_id, "after": cursor,
         "limit": limit + 1},
    )  # fmt: skip
    for row in rows:  # a file side's sealed pins, about 200 bytes a file
        row["params"].pop("pinned", None)
    return _page(rows, limit, "run_id")


async def previous_run(conn: Conn, run_id: str) -> Row | None:
    """The latest completed run of the same spec created before ``run_id`` (the trend baseline)."""
    return await _row(
        conn,
        """SELECT * FROM ds.runs
           WHERE spec_id = (SELECT spec_id FROM ds.runs WHERE run_id = %(run_id)s)
             AND run_id < %(run_id)s AND status IN ('completed', 'completed_with_issues')
           ORDER BY run_id DESC LIMIT 1""",
        {"run_id": run_id},
    )


async def runs_reading(conn: Conn, target_id: str) -> int:
    """How many unfinished runs read the target: those whose spec names it as a side's, or as a key
    source's (``key.source`` and the sources nested in it)."""
    return await _value(
        conn,
        f"""SELECT count(*) FROM ds.runs r JOIN ds.specs s USING (spec_id)
            WHERE {_LIVE}
              AND (jsonb_path_exists(s.body, '$.sides.*.target ? (@ == $t)', %(vars)s)
                   OR jsonb_path_exists(s.body, '$.key.source.**.target ? (@ == $t)', %(vars)s))""",  # noqa: S608
        {"vars": _jsonb({"t": target_id})},
    )


async def set_run_status(
    conn: Conn,
    run_id: str,
    status: str,
    *,
    when: Sequence[str] | None = None,
    stop_reason: str | None = None,
    error: str | None = None,
    cleared: Sequence[str] = (),
) -> bool:
    """Move the run to ``status``, only from one of ``when`` if given; False when it did not move.

    started_at is set on the first ``running``, finished_at on a terminal status; a stop_reason
    or error, once set, is kept, unless the stop_reason is one of ``cleared`` (a pause's, on resume).
    """
    moved = await _changed(
        conn,
        """UPDATE ds.runs SET status = %(status)s, updated_at = now(),
               stop_reason = coalesce(
                   %(stop_reason)s, CASE WHEN stop_reason = ANY(%(cleared)s) THEN NULL ELSE stop_reason END),
               error = coalesce(%(error)s, error),
               started_at = CASE WHEN %(status)s = 'running' THEN coalesce(started_at, now())
                                 ELSE started_at END,
               finished_at = CASE WHEN %(status)s = ANY(%(terminal)s) THEN now() END
           WHERE run_id = %(run_id)s AND (%(when)s::text[] IS NULL OR status = ANY(%(when)s))""",
        {
            "run_id": run_id,
            "status": status,
            "when": None if when is None else list(when),
            "stop_reason": stop_reason,
            "error": error,
            "cleared": list(cleared),
            "terminal": list(TERMINAL),
        },
    )
    return moved == 1


async def set_keys_cut(conn: Conn, run_id: str, keys_cut: Mapping[str, int] | None) -> None:
    """How maxKeys cut the run's key set (``{resolved, taken}``, or a scan's ``{taken}``); None: uncut."""
    await conn.execute(
        "UPDATE ds.runs SET keys_cut = %s, updated_at = now() WHERE run_id = %s",
        (None if keys_cut is None else _jsonb(keys_cut), run_id),
    )


async def set_counters(conn: Conn, run_id: str, counters: Mapping[str, Any]) -> None:
    await conn.execute(
        "UPDATE ds.runs SET counters = %s, updated_at = now() WHERE run_id = %s", (_jsonb(counters), run_id)
    )


async def set_census(conn: Conn, run_id: str, census: Mapping[str, Any]) -> bool:
    """Record the census of the run's current cycle (``census["cycle"]``), merged
    into what that cycle recorded before; False when the run is in another cycle now."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.runs SET census = CASE WHEN census->>'cycle' = cycle::text THEN census || %(census)s
                                               ELSE %(census)s END
               WHERE run_id = %(run_id)s AND cycle = %(cycle)s""",
            {"census": _jsonb(census), "run_id": run_id, "cycle": census["cycle"]},
        )
    )


async def census_due(conn: Conn) -> list[str]:
    """The running runs whose current cycle awaits its key census."""
    rows = await _rows(
        conn,
        f"SELECT run_id FROM ds.runs r WHERE r.status = 'running' AND {_CENSUS_PENDING} ORDER BY run_id",  # noqa: S608
    )
    return [row["run_id"] for row in rows]


async def set_guard(conn: Conn, run_id: str, guard: Mapping[str, Any], *, cycle: int | None = None) -> bool:
    """Record what the schema guard holds of the run's tables, replacing it
    whole; with ``cycle``, only while the run is in that cycle. False when it is in another now."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.runs SET guard = %(guard)s
               WHERE run_id = %(run_id)s AND (%(cycle)s::int IS NULL OR cycle = %(cycle)s)""",
            {"guard": _jsonb(guard), "run_id": run_id, "cycle": cycle},
        )
    )


async def held_guard(conn: Conn, run_id: str) -> Row | None:
    """The run's ``cycle`` and ``guard`` as they stand, its row locked until the caller's transaction
    ends: of two looks of the schema guard at once, the second is judged on what the first recorded, so a
    state is recorded once."""
    return await _row(conn, "SELECT cycle, guard FROM ds.runs WHERE run_id = %s FOR UPDATE", (run_id,))


async def guard_basis(conn: Conn, spec_id: int, before: str) -> Row | None:
    """What the schema guard judges a new run of the version against (runs_spec_idx): the newest batch or
    soak run of ``spec_id`` created before the run ``before`` whose ``guard`` is a baseline, as its
    ``run_id``, ``guard`` and ``tables``. When no earlier run of the version is a baseline, the newest one
    that stored a plan (``tables``) and that the guard did not create paused, unaccepted: a run from before
    016 (its ``guard`` None), one the guard adopted, or one whose look at its creation failed. None with
    neither."""
    return await _row(
        conn,
        """SELECT r.run_id, r.guard, r.params->'tables' AS tables FROM ds.runs r
           WHERE r.spec_id = %(spec_id)s AND r.run_id < %(before)s AND r.mode <> 'one_shot'
             AND CASE WHEN EXISTS (SELECT 1 FROM ds.runs g WHERE g.spec_id = %(spec_id)s
                                     AND g.run_id < %(before)s AND g.mode <> 'one_shot'
                                     AND g.guard->'baseline' = 'true')
                      THEN r.guard->'baseline' = 'true'
                      ELSE jsonb_typeof(r.params->'tables') = 'object'
                           AND (r.guard IS NULL OR r.guard->'basis' = 'null' OR r.guard->'adopted' <> 'null')
                 END
           ORDER BY r.run_id DESC LIMIT 1""",
        {"spec_id": spec_id, "before": before},
    )


async def start_next_cycle(
    conn: Conn,
    run_id: str,
    available_at: datetime,
    keys: Mapping[str, bytes] | None = None,
    scan: Mapping[str, Any] | None = None,
) -> int:
    """Open the run's next cycle, due at ``available_at`` (recorded as its ``cycle_due_at``), with ``keys``
    (masked -> key_enc) or else the previous cycle's key set (a key_enc is sealed for every cycle) and
    labels, its retries reset, and ``scan`` as its scan's state; return the new cycle number."""
    cycle: int = await _value(
        conn,
        """UPDATE ds.runs SET cycle = cycle + 1, cycle_due_at = %s, updated_at = now() WHERE run_id = %s
           RETURNING cycle""",
        (available_at, run_id),
    )
    if scan is not None:
        await conn.execute(
            "INSERT INTO ds.scans (run_id, cycle, state) VALUES (%s, %s, %s)", (run_id, cycle, _jsonb(scan))
        )
    if keys is not None:
        await _insert_keys(conn, run_id, cycle, keys, available_at)
    else:
        await conn.execute(
            """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, available_at, stratum,
                   signoff_stratum, signoff_member)
               SELECT run_id, cycle + 1, key_masked, key_enc, seq, %s, stratum, signoff_stratum,
                   signoff_member
               FROM ds.run_keys WHERE run_id = %s AND cycle = %s""",
            (available_at, run_id, cycle - 1),
        )
    return cycle
