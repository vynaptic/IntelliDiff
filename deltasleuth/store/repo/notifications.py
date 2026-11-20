"""The queries of notifications."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, LiteralString

from deltasleuth.core.types import Verdict
from deltasleuth.store.repo.base import (
    Conn,
    Page,
    Row,
    _changed,
    _one,
    _page,
    _row,
    _rows,
    _sortable_id,
    _value,
    _verdict_is,
)

# A level fact of a cycle ``c``, as text; null where the cycle does not hold it.
_LEVEL_FACTS: dict[str, LiteralString] = {
    "mismatch_rate": "nullif(c.counters->>'mismatch_rate', 'unavailable')",
    "coverage": "nullif(c.counters->>'coverage', 'unavailable')",
    "indeterminate_rate": """trim_scale(round((c.counters->>'indeterminate')::numeric
                             / nullif((c.counters->>'total')::numeric, 0), 6))::text""",
    "unexplained_critical": """CASE WHEN c.purging_at IS NULL THEN (
        SELECT count(DISTINCT f.key_masked) FROM ds.findings f
        WHERE f.run_id = c.run_id AND f.cycle = c.cycle AND f.severity = 'CRITICAL'
          AND (NOT f.suppressed OR f.rule_id = ANY(%(bug_rules)s)))::text END""",
}


def new_delivery_id() -> str:
    return _sortable_id("N-")


async def notify_cursor(conn: Conn) -> Row:
    """The feed's cursor ``{seq, now}``, locked until the transaction ends; the first pass inserts it at the
    chain's head (``now``: the transaction's time)."""
    lock = "SELECT seq, now() AS now FROM ds.notify_cursor FOR UPDATE"
    if (row := await _row(conn, lock)) is None:
        await conn.execute(
            """INSERT INTO ds.notify_cursor (seq) SELECT coalesce(max(seq), 0) FROM ds.audit_chain
               ON CONFLICT DO NOTHING"""
        )
        row = await _one(conn, lock)
    return row


async def move_cursor(conn: Conn, seq: int) -> None:
    await conn.execute("UPDATE ds.notify_cursor SET seq = %s, moved_at = now()", (seq,))


async def chain_head(conn: Conn) -> int:
    """The last chain row's seq; 0 while the chain is empty."""
    return await _value(conn, "SELECT coalesce(max(seq), 0) FROM ds.audit_chain", None)


async def chain_before(conn: Conn, low: int, high: int, before: datetime) -> int:
    """The last chain row from ``low`` (written before ``before``) to ``high`` written before ``before``: a
    bisection on seq, about 20 primary-key lookups, as ``at`` follows seq to within a transaction's length."""
    while low < high:
        mid = (low + high + 1) // 2
        at = await _value(
            conn,
            "SELECT l.at FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id WHERE c.seq = %s",
            (mid,),
        )
        low, high = (mid, high) if at < before else (low, mid - 1)
    return low


async def cycle_at(conn: Conn, run_id: str, seq: int) -> int:
    """The run's cycle as of chain row ``seq``: the one its latest ``run.cycle`` row before it started."""
    cycle = await _value(
        conn,
        """SELECT (l.detail->>'cycle')::int FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id
           WHERE c.object = %s AND c.seq < %s AND l.action = 'run.cycle' ORDER BY c.seq DESC LIMIT 1""",
        (run_id, seq),
    )
    return cycle or 1


async def installation_id(conn: Conn) -> str:
    return await _value(conn, "SELECT installation_id FROM ds.installation", None)


async def previous_fact(
    conn: Conn, run_id: str, cycle: int, fact: str, bug_rules: Sequence[str]
) -> Row | None:
    """A level fact's baseline for ``cycle``: ``{cycle, value}`` of the latest
    earlier cycle that holds it (``unexplained_critical``: whose purge has not started), backwards on the
    ds.cycles key; None when none does."""
    value = _LEVEL_FACTS[fact]
    return await _row(
        conn,
        f"""SELECT c.cycle, {value} AS value FROM ds.cycles c
            WHERE c.run_id = %(run_id)s AND c.cycle < %(cycle)s AND {value} IS NOT NULL
            ORDER BY c.cycle DESC LIMIT 1""",  # noqa: S608 - fixed SQL fragments only
        {"run_id": run_id, "cycle": cycle, "bug_rules": list(bug_rules)},
    )


async def root_cause_changes(conn: Conn, run_id: str, cycle: int) -> Row:
    """The cycle's root causes held by no retained earlier cycle (``new``) and the previous cycle's absent
    from it (``gone``), with ``baseline_from``, the earliest retained earlier cycle, and whether the previous
    one is retained (``previous_kept``); a cycle is retained until its purge starts."""
    return await _one(
        conn,
        """WITH kept AS (SELECT cycle FROM ds.cycles
                         WHERE run_id = %(r)s AND cycle < %(c)s AND purging_at IS NULL),
                cur AS (SELECT root_cause_id FROM ds.root_causes WHERE run_id = %(r)s AND cycle = %(c)s)
           SELECT (SELECT min(cycle) FROM kept) AS baseline_from,
                  EXISTS (SELECT 1 FROM kept WHERE cycle = %(c)s - 1) AS previous_kept,
                  (SELECT count(*) FROM cur WHERE NOT EXISTS (
                       SELECT 1 FROM ds.root_causes p JOIN kept USING (cycle)
                       WHERE p.run_id = %(r)s AND p.root_cause_id = cur.root_cause_id)) AS new,
                  (SELECT count(*) FROM ds.root_causes p WHERE p.run_id = %(r)s AND p.cycle = %(c)s - 1
                     AND NOT EXISTS (SELECT 1 FROM cur WHERE cur.root_cause_id = p.root_cause_id)) AS gone""",
        {"r": run_id, "c": cycle},
    )


# The result ``cur`` of cycle ``%(c)s`` DIFFERENT or MATCH, and ``prev``, of the cycle before, DIFFERENT.
_CUR_DIFFERENT = _verdict_is(Verdict.DIFFERENT, "cur")
_CUR_MATCH = _verdict_is(Verdict.MATCH, "cur")
_PREV_DIFFERENT = _verdict_is(Verdict.DIFFERENT, "prev", "%(c)s - 1")


async def discrepant_changes(
    conn: Conn, run_id: str, cycle: int, *, read_all_before: bool, read_all: bool
) -> Row:
    """Over the keys read in both the cycle and the one before, ``newly`` discrepant in it and not before,
    and ``no_longer`` discrepant, read MATCH in it. Discrepant is DIFFERENT as ``types.derive_verdict`` and
    the counters read it: MISMATCH, NOT_FOUND_A or NOT_FOUND_B, or KNOWN_DIFF under a bug rule; MATCH is
    read identical, noise, or a difference a rule that is no bug rule explains. O(discrepant keys), from
    each cycle's results of the outcomes that can be discrepant (results_outcome_idx). In a cycle that read
    every key (``read_all_before`` for the one before, ``read_all`` for this one: a table scan's, run to its
    end and uncut) a key without a result was read identical; so is every key before cycle 1."""
    return await _one(
        conn,
        f"""SELECT
             (SELECT count(*) FROM ds.results cur
              LEFT JOIN ds.results prev ON (prev.run_id, prev.cycle, prev.key_masked)
                                           = (cur.run_id, cur.cycle - 1, cur.key_masked)
              WHERE cur.run_id = %(r)s AND cur.cycle = %(c)s AND {_CUR_DIFFERENT}
                AND (prev.outcome IS NULL AND (%(before)s OR %(c)s = 1) OR NOT {_PREV_DIFFERENT}))
               AS newly,
             (SELECT count(*) FROM ds.results prev
              LEFT JOIN ds.results cur ON (cur.run_id, cur.cycle, cur.key_masked)
                                          = (prev.run_id, prev.cycle + 1, prev.key_masked)
              WHERE prev.run_id = %(r)s AND prev.cycle = %(c)s - 1 AND {_PREV_DIFFERENT}
                AND ({_CUR_MATCH} OR cur.outcome IS NULL AND %(all)s)) AS no_longer""",  # noqa: S608
        {"r": run_id, "c": cycle, "before": read_all_before, "all": read_all},
    )


async def insert_delivery(conn: Conn, delivery: Mapping[str, Any]) -> bool:
    """Store a delivery once per event and channel, pending and due now unless ``why`` suppressed it;
    False when the event's delivery to the channel exists. ``delivery``: delivery_id, event_id, seq, event,
    severity, channel, rules, dedup_key, run_id, body, body_sha256, render, why, mute_id."""
    return bool(
        await _changed(
            conn,
            """INSERT INTO ds.notify_deliveries (delivery_id, event_id, seq, event, severity, channel, rules,
                   dedup_key, run_id, body, body_sha256, render, state, why, mute_id, due_at)
               VALUES (%(delivery_id)s, %(event_id)s, %(seq)s, %(event)s, %(severity)s, %(channel)s,
                   %(rules)s, %(dedup_key)s, %(run_id)s, %(body)s, %(body_sha256)s, %(render)s,
                   CASE WHEN %(why)s::text IS NULL THEN 'pending' ELSE 'suppressed' END, %(why)s, %(mute_id)s,
                   CASE WHEN %(why)s::text IS NULL THEN now() END)
               ON CONFLICT (event_id, channel) DO NOTHING""",
            dict(delivery) | {"rules": list(delivery["rules"])},
        )
    )


async def active_mute(conn: Conn, channel: str, run_id: str | None) -> str | None:
    """The first active mute of the channel or the run."""
    return await _value(
        conn,
        """SELECT mute_id FROM ds.notify_mutes WHERE ended_at IS NULL AND until > now()
             AND (scope, target) IN (('channel', %s), ('run', %s)) ORDER BY mute_id LIMIT 1""",
        (channel, run_id),
    )


async def held_back(
    conn: Conn, channel: str, dedup_key: str, dedup_hours: int, *, among: str | None = None
) -> Row:
    """What a new delivery to the channel is judged by: whether a pending or sent one
    of ``dedup_key`` is under ``dedup_hours`` old (``duplicate``; with ``among``, a key prefix, only while no
    pending or sent delivery of another key beginning with it followed: the newest of them alone counts),
    the pending and sent ones of the last hour (``last_hour``) and the suppressed ones since the last that
    was not, tests aside (``suppressed``)."""
    return await _one(
        conn,
        """SELECT EXISTS (SELECT 1 FROM ds.notify_deliveries d
                          WHERE d.channel = %(ch)s AND d.dedup_key = %(key)s
                            AND d.state IN ('pending', 'sent')
                            AND d.created_at > now() - make_interval(hours => %(hours)s)
                            AND NOT EXISTS (
                                SELECT 1 FROM ds.notify_deliveries n
                                WHERE n.channel = d.channel AND n.state IN ('pending', 'sent')
                                  AND n.created_at >= d.created_at AND n.delivery_id > d.delivery_id
                                  AND n.dedup_key <> d.dedup_key
                                  AND starts_with(n.dedup_key, %(among)s::text))) AS duplicate,
                  (SELECT count(*) FROM ds.notify_deliveries
                   WHERE channel = %(ch)s AND state IN ('pending', 'sent')
                     AND created_at > now() - interval '1 hour') AS last_hour,
                  (SELECT count(*) FROM ds.notify_deliveries
                   WHERE channel = %(ch)s AND state = 'suppressed'
                     AND delivery_id > coalesce((SELECT max(delivery_id) FROM ds.notify_deliveries
                                                 WHERE channel = %(ch)s AND state <> 'suppressed'
                                                   AND event_id NOT LIKE 'test:%%'), ''))
                   AS suppressed""",
        {"ch": channel, "key": dedup_key, "hours": dedup_hours, "among": among},
    )


async def due_channels(conn: Conn, max_age_hours: int) -> list[str]:
    """The channels whose head, the oldest pending delivery, is due: its ``due_at`` passed, or it was queued
    over ``max_age_hours`` ago (it expires). A head backing off holds its channel (notify_pending_idx)."""
    rows = await _rows(
        conn,
        """SELECT channel FROM (
               SELECT DISTINCT ON (channel) channel, due_at, queued_at FROM ds.notify_deliveries
               WHERE state = 'pending' ORDER BY channel, delivery_id) heads
           WHERE due_at <= now() OR queued_at < now() - make_interval(hours => %s)""",
        (max_age_hours,),
    )
    return [row["channel"] for row in rows]


async def notify_head(conn: Conn, channel: str, max_age_hours: int) -> Row | None:
    """The channel's head, locked until the transaction ends, with whether it is ``due`` and ``expired``."""
    return await _row(
        conn,
        """SELECT delivery_id, event, run_id, attempts, last_status, body_sha256, render,
                  queued_at < now() - make_interval(hours => %(age)s) AS expired,
                  due_at <= now() OR queued_at < now() - make_interval(hours => %(age)s) AS due
           FROM ds.notify_deliveries WHERE channel = %(ch)s AND state = 'pending'
           ORDER BY delivery_id LIMIT 1 FOR UPDATE""",
        {"ch": channel, "age": max_age_hours},
    )


async def claim_delivery(conn: Conn, delivery_id: str, lease_seconds: float) -> Row:
    """Take an attempt of the delivery: due again after ``lease_seconds``, unless recorded first."""
    return await _one(
        conn,
        """UPDATE ds.notify_deliveries SET attempts = attempts + 1, due_at = now() + make_interval(secs => %s)
           WHERE delivery_id = %s
           RETURNING delivery_id, channel, event, run_id, attempts, body, body_sha256, created_at""",
        (lease_seconds, delivery_id),
    )


async def end_delivery(
    conn: Conn,
    delivery_id: str,
    state: str,
    *,
    attempts: int | None = None,
    error: str | None = None,
    status: int | None = None,
    due_in: float = 0.0,
    mute_id: str | None = None,
) -> bool:
    """Record a pending delivery's attempt (of number ``attempts``, when given) or fate: ``sent``, ``dead``,
    ``pending`` again after ``due_in`` seconds, or ``suppressed`` by ``mute_id``; False when it moved on."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.notify_deliveries SET state = %(state)s, last_error = %(error)s,
                   last_status = coalesce(%(status)s, last_status),
                   due_at = CASE WHEN %(state)s = 'pending'
                                 THEN now() + make_interval(secs => %(due_in)s) END,
                   done_at = CASE WHEN %(state)s IN ('sent', 'dead') THEN now() END,
                   why = CASE WHEN %(state)s = 'suppressed' THEN 'muted' END, mute_id = %(mute_id)s
               WHERE delivery_id = %(id)s AND state = 'pending'
                 AND (%(attempts)s::int IS NULL OR attempts = %(attempts)s)""",
            {
                "id": delivery_id,
                "state": state,
                "attempts": attempts,
                "error": error,
                "status": status,
                "due_in": due_in,
                "mute_id": mute_id,
            },
        )
    )


async def deaths(conn: Conn, channel: str, delivery_id: str) -> Row:
    """Whether the channel's newest ``notify.dead`` row is under an hour old (``recent``) and how many of its
    other deliveries died since it (``since``; since the first, without one)."""
    return await _one(
        conn,
        """WITH last AS (SELECT at FROM ds.audit_log WHERE object = %(obj)s AND action = 'notify.dead'
                         ORDER BY id DESC LIMIT 1)
           SELECT coalesce((SELECT at > now() - interval '1 hour' FROM last), false) AS recent,
                  (SELECT count(*) FROM ds.notify_deliveries
                   WHERE channel = %(ch)s AND state = 'dead' AND delivery_id <> %(id)s
                     AND done_at > coalesce((SELECT at FROM last), '-infinity')) AS since""",
        {"obj": f"notify:{channel}", "ch": channel, "id": delivery_id},
    )


async def daily_counts(conn: Conn, actions: Sequence[str]) -> Row:
    """What ``notify.daily`` says, as of ``now``: live runs by status, paused
    ones by reason, cycles ended and runs finished in 24 h, approvals waiting, the newest recorded licence
    state, each channel's counts and the age of its pending head, the feed's lag (the age of the oldest
    unread row of ``actions``, 0 when none) and the active mutes."""
    counts = await _one(
        conn,
        """SELECT now() AS now, (SELECT coalesce(jsonb_object_agg(status, n), '{}') FROM (
                       SELECT status, count(*) AS n FROM ds.runs
                       WHERE status IN ('queued', 'running', 'paused', 'cancelling') GROUP BY status) s)
                      AS runs,
                  (SELECT coalesce(jsonb_object_agg(reason, n), '{}') FROM (
                       SELECT coalesce(stop_reason, 'manual') AS reason, count(*) AS n FROM ds.runs
                       WHERE status = 'paused' GROUP BY 1) p) AS paused,
                  (SELECT count(*) FROM ds.cycles WHERE ended_at > now() - interval '24 hours')
                      AS cycles_ended_24h,
                  (SELECT count(*) FROM ds.runs WHERE finished_at > now() - interval '24 hours')
                      AS runs_finished_24h,
                  (SELECT count(*) FROM ds.approvals WHERE status = 'requested') AS approvals_waiting,
                  (SELECT detail->>'to' FROM ds.audit_log WHERE action = 'licence.state'
                   ORDER BY id DESC LIMIT 1)
                      AS licence,
                  coalesce((SELECT floor(extract(epoch FROM now() - l.at))::int
                            FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id
                            WHERE c.seq > (SELECT seq FROM ds.notify_cursor) AND l.action = ANY(%s)
                            ORDER BY c.seq LIMIT 1), 0) AS feed_lag_s,
                  (SELECT count(*) FROM ds.notify_mutes WHERE ended_at IS NULL AND until > now()) AS mutes""",
        (list(actions),),
    )
    counts["channels"] = {
        row.pop("channel"): row
        for row in await _rows(
            conn,
            """SELECT channel, count(*) FILTER (WHERE state = 'pending') AS pending,
                      count(*) FILTER (WHERE state = 'sent' AND done_at > now() - interval '1 day')
                          AS sent_24h,
                      count(*) FILTER (WHERE state = 'dead' AND done_at > now() - interval '1 day')
                          AS dead_24h,
                      count(*) FILTER (WHERE state = 'suppressed' AND created_at > now() - interval '1 day')
                          AS suppressed_24h,
                      floor(extract(epoch FROM now() - min(queued_at) FILTER (WHERE state = 'pending')))::int
                          AS head_age_s
               FROM ds.notify_deliveries WHERE state = 'pending' OR created_at > now() - interval '24 hours'
               GROUP BY channel""",
        )
    }
    return counts


async def prune_notify(conn: Conn, days: int, limit: int) -> int:
    """Delete up to ``limit`` deliveries made, and as many mutes ended or lapsed, over ``days`` days ago;
    the rows deleted."""
    deleted = await _changed(
        conn,
        """DELETE FROM ds.notify_deliveries WHERE delivery_id IN (
               SELECT delivery_id FROM ds.notify_deliveries
               WHERE created_at < now() - make_interval(days => %s) LIMIT %s)""",
        (days, limit),
    )
    return deleted + await _changed(
        conn,
        """DELETE FROM ds.notify_mutes WHERE mute_id IN (
               SELECT mute_id FROM ds.notify_mutes
               WHERE coalesce(ended_at, until) < now() - make_interval(days => %s) LIMIT %s)""",
        (days, limit),
    )


async def notify_state(conn: Conn, actions: Sequence[str]) -> Row:
    """The feed as the Notifications screen shows it: ``now``, the cursor's ``seq`` (None before the first
    pass), the chain's ``head`` and ``behind``, when the oldest unread row of ``actions`` was written (None
    when none is unread)."""
    return await _one(
        conn,
        """SELECT now() AS now, (SELECT seq FROM ds.notify_cursor) AS seq,
                  (SELECT coalesce(max(seq), 0) FROM ds.audit_chain) AS head,
                  (SELECT l.at FROM ds.audit_chain c JOIN ds.audit_log l ON l.id = c.id
                   WHERE c.seq > (SELECT seq FROM ds.notify_cursor) AND l.action = ANY(%s)
                   ORDER BY c.seq LIMIT 1) AS behind""",
        (list(actions),),
    )


async def channel_states(conn: Conn, channels: Sequence[str]) -> dict[str, Row]:
    """Each channel's deliveries as the Notifications screen counts them: pending; sent, dead and suppressed
    in 24 h; the newest attempt's ``last_error`` and ``last_status`` (its head's once tried, else the latest
    sent or dead one's); its head's ``head_error`` and when it fell due (``head_due``, None unless due); and
    the dead deliveries since its latest sent one, tests aside (``failing``, from ``failing_since``)."""
    rows = await _rows(
        conn,
        """SELECT c.channel, k.*, h.head_error, h.head_due,
                  coalesce(h.last_error, l.last_error) AS last_error,
                  coalesce(h.last_status, l.last_status) AS last_status, f.failing, f.failing_since
           FROM unnest(%s::text[]) AS c(channel)
           CROSS JOIN LATERAL (
               SELECT count(*) FILTER (WHERE state = 'pending') AS pending,
                      count(*) FILTER (WHERE state = 'sent' AND done_at > now() - interval '1 day')
                          AS sent_24h,
                      count(*) FILTER (WHERE state = 'dead' AND done_at > now() - interval '1 day')
                          AS dead_24h,
                      count(*) FILTER (WHERE state = 'suppressed' AND created_at > now() - interval '1 day')
                          AS suppressed_24h
               FROM ds.notify_deliveries WHERE channel = c.channel) k
           LEFT JOIN LATERAL (
               SELECT CASE WHEN attempts > 0 THEN last_error END AS head_error,
                      CASE WHEN attempts > 0 THEN last_error END AS last_error,
                      CASE WHEN attempts > 0 THEN last_status END AS last_status,
                      CASE WHEN due_at <= now() THEN due_at END AS head_due, attempts
               FROM ds.notify_deliveries WHERE channel = c.channel AND state = 'pending'
               ORDER BY delivery_id LIMIT 1) h ON true
           LEFT JOIN LATERAL (
               SELECT last_error, last_status FROM ds.notify_deliveries
               WHERE channel = c.channel AND state IN ('sent', 'dead') AND coalesce(h.attempts, 0) = 0
               ORDER BY done_at DESC LIMIT 1) l ON true
           CROSS JOIN LATERAL (
               SELECT count(*) AS failing, min(done_at) AS failing_since FROM ds.notify_deliveries d
               WHERE d.channel = c.channel AND d.state = 'dead' AND d.event_id NOT LIKE 'test:%%'
                 AND d.done_at > coalesce((SELECT max(done_at) FROM ds.notify_deliveries
                                           WHERE channel = c.channel AND state = 'sent'
                                             AND event_id NOT LIKE 'test:%%'), '-infinity')) f""",
        (list(channels),),
    )
    return {row.pop("channel"): row for row in rows}


async def list_deliveries(
    conn: Conn,
    *,
    channel: str | None = None,
    state: str | None = None,
    event: str | None = None,
    run_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Deliveries newest first, each with its stored body (an e-mail's without From and To)."""
    rows = await _rows(
        conn,
        """SELECT * FROM ds.notify_deliveries
           WHERE (%(channel)s::text IS NULL OR channel = %(channel)s)
             AND (%(state)s::text IS NULL OR state = %(state)s)
             AND (%(event)s::text IS NULL OR event = %(event)s)
             AND (%(run_id)s::text IS NULL OR run_id = %(run_id)s)
             AND (%(after)s::text IS NULL OR delivery_id < %(after)s)
           ORDER BY delivery_id DESC LIMIT %(limit)s""",
        {
            "channel": channel,
            "state": state,
            "event": event,
            "run_id": run_id,
            "after": cursor,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "delivery_id")


async def insert_tested(conn: Conn, delivery: Mapping[str, Any]) -> None:
    """Store a test send after its one attempt, ``sent`` or ``dead``, never pending, so no sender claims it.
    ``delivery``: delivery_id, event_id, event, severity, channel, dedup_key, body, body_sha256, render,
    state, error, status."""
    await conn.execute(
        """INSERT INTO ds.notify_deliveries (delivery_id, event_id, event, severity, channel, dedup_key, body,
               body_sha256, render, state, attempts, last_error, last_status, done_at)
           VALUES (%(delivery_id)s, %(event_id)s, %(event)s, %(severity)s, %(channel)s, %(dedup_key)s,
               %(body)s, %(body_sha256)s, %(render)s, %(state)s, 1, %(error)s, %(status)s, now())""",
        delivery,
    )


async def retry_dead(conn: Conn, channel: str, since: datetime, render: str) -> int:
    """Send again the channel's deliveries that died from ``since``, tests aside, rendered as it now renders
    (``render``): pending and due now, with no attempt, queued now (so they expire from now); how many."""
    return await _changed(
        conn,
        """UPDATE ds.notify_deliveries
           SET state = 'pending', due_at = now(), attempts = 0, queued_at = now(), done_at = NULL
           WHERE channel = %s AND state = 'dead' AND done_at >= %s AND event_id NOT LIKE 'test:%%'
             AND render = %s""",
        (channel, since, render),
    )


def new_mute_id() -> str:
    return _sortable_id("M-")


async def insert_mute(conn: Conn, scope: str, target: str, hours: int, reason: str, created_by: str) -> Row:
    """A mute of the channel or run ``target`` for ``hours`` from now: ``{mute_id, until}``."""
    return await _one(
        conn,
        """INSERT INTO ds.notify_mutes (mute_id, scope, target, until, reason, created_by)
           VALUES (%s, %s, %s, now() + make_interval(hours => %s), %s, %s) RETURNING mute_id, until""",
        (new_mute_id(), scope, target, hours, reason, created_by),
    )


async def get_mute(conn: Conn, mute_id: str) -> Row | None:
    """The mute, with whether it ``lapsed``; None when unknown."""
    return await _row(
        conn, "SELECT *, until <= now() AS lapsed FROM ds.notify_mutes WHERE mute_id = %s", (mute_id,)
    )


async def end_mute(conn: Conn, mute_id: str, ended_by: str) -> bool:
    """End an active mute now; False when it ended or lapsed meanwhile."""
    return bool(
        await _changed(
            conn,
            """UPDATE ds.notify_mutes SET ended_by = %s, ended_at = now()
               WHERE mute_id = %s AND ended_at IS NULL AND until > now()""",
            (ended_by, mute_id),
        )
    )


async def active_mutes(conn: Conn) -> list[Row]:
    """The mutes neither ended nor lapsed, oldest first (notify_mutes_active_idx)."""
    return await _rows(
        conn,
        """SELECT mute_id, scope, target, until, reason, created_by, created_at FROM ds.notify_mutes
           WHERE ended_at IS NULL AND until > now() ORDER BY mute_id""",
    )
