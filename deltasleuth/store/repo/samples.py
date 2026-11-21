"""The queries of sample draws."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, LiteralString

from deltasleuth.store.repo.base import (
    _BUG,
    _LIVE,
    Conn,
    Page,
    Row,
    _after,
    _changed,
    _jsonb,
    _page,
    _row,
    _rows,
    _sortable_id,
    _value,
)
from deltasleuth.store.repo.runs import _insert_keys

_DRAW_LOCK = 0x4453534D  # pg advisory lock "DSSM", with each population id: a live bottom_k run's draws


async def create_frame_keys(conn: Conn, temp_buffers: str) -> None:
    """The session's temporary table of a draw's frame keys, by digest (``h``) with the source that gave
    each (``member``: 0 or 1, 2 for both), in ``temp_buffers`` of the session's memory."""
    await conn.execute("SELECT set_config('temp_buffers', %s, false)", (temp_buffers,))
    await conn.execute("CREATE TEMP TABLE frame_keys (h uuid PRIMARY KEY, member smallint NOT NULL)")


async def share_frame_keys(conn: Conn, digests: Sequence[Any]) -> set[Any]:
    """Mark the keys of ``digests`` the first source gave alone as given by both; the digests marked."""
    cur = await conn.execute(
        "UPDATE frame_keys SET member = 2 WHERE h = ANY(%s::uuid[]) AND member = 0 RETURNING h", (digests,)
    )
    return {row[0] for row in await cur.fetchall()}


async def insert_frame_keys(conn: Conn, digests: Sequence[Any], member: int) -> set[Any]:
    """Insert the keys of ``digests`` as given by source ``member``; the digests that were new."""
    cur = await conn.execute(
        "INSERT INTO frame_keys SELECT unnest(%s::uuid[]), %s ON CONFLICT DO NOTHING RETURNING h",
        (digests, member),
    )
    return {row[0] for row in await cur.fetchall()}


def new_draw_id() -> str:
    return _sortable_id("D-")


async def set_draw(conn: Conn, run_id: str, draw: Mapping[str, Any] | None) -> bool:
    """Set the run's pending draw (``{due[, error, at]}``), or clear it; False without the run."""
    return bool(
        await _changed(
            conn,
            "UPDATE ds.runs SET draw = %s, updated_at = now() WHERE run_id = %s",
            (None if draw is None else _jsonb(draw), run_id),
        )
    )


async def draws_due(conn: Conn) -> list[str]:
    """The running runs whose pending draw is due."""
    rows = await _rows(
        conn,
        """SELECT run_id FROM ds.runs WHERE status = 'running' AND draw ? 'due'
             AND (draw->>'due')::timestamptz <= now() ORDER BY run_id""",
    )
    return [row["run_id"] for row in rows]


async def insert_draw(
    conn: Conn,
    draw: Mapping[str, Any],
    keys: Mapping[str, bytes],
    labels: Sequence[tuple[str | None, str | None, bool]],
) -> Row:
    """Store a draw as of now() and then its keys (masked -> key_enc, in seq order) with their labels; the
    trigger refuses a draw not for a running run's current cycle with its draw pending, or after keys.
    ``draw``: draw_id, run_id, spec_id, cycle, method, population, salt_label, nonce, keys, frame, strata,
    selection_sha256. The stored row."""
    row = await _row(
        conn,
        """INSERT INTO ds.sample_draws (draw_id, run_id, spec_id, cycle, method, population, salt_label,
               nonce, drawn_at, keys, frame, strata, selection_sha256)
           VALUES (%(draw_id)s, %(run_id)s, %(spec_id)s, %(cycle)s, %(method)s, %(population)s,
               %(salt_label)s, %(nonce)s, now(), %(keys)s, %(frame)s, %(strata)s, %(selection_sha256)s)
           RETURNING *""",
        dict(draw) | {"population": list(draw["population"]), "frame": _jsonb(draw["frame"]),
                      "strata": _jsonb(draw["strata"])},
    )  # fmt: skip
    assert row is not None
    await _insert_keys(conn, row["run_id"], row["cycle"], keys, None, labels=labels)
    return row


async def get_draw(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The draw of the run's cycle; None when the cycle has none."""
    return await _row(conn, "SELECT * FROM ds.sample_draws WHERE run_id = %s AND cycle = %s", (run_id, cycle))


async def stratum_counts(conn: Conn, run_id: str, cycle: int) -> list[Row]:
    """The cycle's keys as a sample is judged, in one grouped query: ``n`` keys
    of each sign-off stratum, membership, whether done, whether retried, outcome, first outcome,
    confirmation, reasons (``truncated``: the sides an INCONCLUSIVE result's are truncated on, as ``a``,
    ``b`` or ``ab``, whatever its reason: a failed recheck of a truncated read too) and ``bug``, a finding
    under a bug rule; a key without a result has a NULL outcome. The only reader of the labels besides
    ``selection_lines``: no key leaves it."""
    return await _rows(
        conn,
        f"""SELECT stratum, member, done, retried, outcome, first_outcome, confirmation, inconclusive_reason,
                  drift_reason, truncated, bug, count(*) AS n
           FROM (SELECT k.signoff_stratum AS stratum, k.signoff_member AS member, k.state = 'done' AS done,
                        k.retries > 0 AS retried, r.outcome, r.first_outcome, r.confirmation,
                        r.inconclusive_reason, r.drift_reason,
                        CASE WHEN r.outcome = 'INCONCLUSIVE'
                                  AND 'truncated' IN (r.side_a_reason, r.side_b_reason) THEN
                            concat(CASE WHEN r.side_a_reason = 'truncated' THEN 'a' END,
                                   CASE WHEN r.side_b_reason = 'truncated' THEN 'b' END) END AS truncated,
                        {_BUG} AS bug
                 FROM ds.run_keys k LEFT JOIN ds.results r USING (run_id, cycle, key_masked)
                 WHERE k.run_id = %(r)s AND k.cycle = %(c)s) AS keys
           GROUP BY stratum, member, done, retried, outcome, first_outcome, confirmation, inconclusive_reason,
                    drift_reason, truncated, bug
           ORDER BY stratum, member, done, retried, outcome, first_outcome, confirmation, inconclusive_reason,
                    drift_reason, truncated, bug""",  # noqa: S608 - fixed SQL fragments only
        {"r": run_id, "c": cycle},
    )


async def live_sampled_runs(conn: Conn, population: Sequence[str], *, lock: bool = False) -> list[Row]:
    """The live runs (queued, running, paused or cancelling) of any spec drawing ``bottom_k`` samples of
    a population sharing an id with ``population``, with ``spec_name``. ``lock`` first takes the
    DSSM lock of each id, in sorted order, until the transaction ends: a run created under it is seen."""
    for member_id in sorted(population) if lock else ():
        await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_DRAW_LOCK, member_id))
    return await _rows(
        conn,
        f"""SELECT r.run_id, r.params->'population' AS population, s.name AS spec_name
            FROM ds.runs r JOIN ds.specs s USING (spec_id)
            WHERE {_LIVE} AND r.params->>'keys_source' = 'hash_sample'
              AND r.params->>'method' = 'bottom_k' AND r.params->'population' ?| %s::text[]
            ORDER BY r.run_id""",  # noqa: S608 - fixed SQL fragments only
        (list(population),),
    )


async def close_draws(conn: Conn, draw_ids: Sequence[str], by: str, note: str) -> list[Row]:
    """Close each of the draws not closed yet, as of now(), by ``by`` with ``note``; the closed
    rows. The trigger refuses a draw whose cycle has not ended."""
    return await _rows(
        conn,
        """UPDATE ds.sample_draws SET closed_by = %s, closed_at = now(), close_note = %s
           WHERE draw_id = ANY(%s) AND closed_at IS NULL RETURNING *""",
        (by, note, list(draw_ids)),
    )


async def selection_lines(
    conn: Conn, run_id: str, cycle: int
) -> AsyncIterator[tuple[int, str, str | None, str | None, bool]]:
    """The cycle's keys as ``sampling.line`` hashes them, in seq order, streamed (R-d re-hashes them)."""
    async with conn.cursor() as cur:
        async for row in cur.stream(
            """SELECT seq, key_masked, stratum, signoff_stratum, signoff_member FROM ds.run_keys
               WHERE run_id = %s AND cycle = %s ORDER BY seq""",
            (run_id, cycle),
        ):
            yield row


# A draw as its ledger lists it, never its nonce: the chain seqs of its sample.draw and sample.close rows,
# its spec's name and its status: live (its cycle has not ended), unjudged (it ended without a judgement),
# else the stored judgement's.
_LEDGER: LiteralString = """SELECT d.draw_id, d.run_id, d.spec_id, s.name AS spec_name, d.cycle, d.method,
        d.population, d.salt_label, d.drawn_at, d.keys, d.frame, d.strata, d.selection_sha256, d.closed_by,
        ds.actor_name(d.closed_by) AS closed_by_name, d.closed_at, d.close_note, c.ended_at,
        CASE WHEN c.run_id IS NULL THEN 'live' WHEN c.sample IS NULL THEN 'unjudged'
             ELSE c.sample->>'status' END AS status,
        ARRAY(SELECT st->>'label' FROM jsonb_array_elements(coalesce(c.sample->'strata', '[]')) st
              WHERE st->>'verdict' = 'failed') AS failed_strata,
        (SELECT h.seq FROM ds.audit_log l JOIN ds.audit_chain h ON h.id = l.id
         WHERE l.object = d.run_id AND l.action = 'sample.draw'
           AND l.detail->>'draw_id' = d.draw_id) AS draw_seq,
        (SELECT h.seq FROM ds.audit_log l JOIN ds.audit_chain h ON h.id = l.id
         WHERE l.object = d.draw_id AND l.action = 'sample.close') AS close_seq
    FROM ds.sample_draws d JOIN ds.specs s USING (spec_id) LEFT JOIN ds.cycles c USING (run_id, cycle)"""


async def ledger(conn: Conn, population: Sequence[str]) -> list[Row]:
    """Every draw, of any spec, of a population sharing an id with ``population``, in commit order."""
    return await _rows(
        conn,
        f"{_LEDGER} WHERE d.population && %s::text[] ORDER BY draw_seq, d.draw_id",
        (list(population),),
    )


async def draws(conn: Conn, *, limit: int = 50, cursor: str | None = None) -> Page:
    """Every draw as its ledger lists it, in commit order."""
    return _page(
        await _rows(
            conn,
            f"""SELECT * FROM ({_LEDGER}) AS d WHERE draw_seq > %s
                ORDER BY draw_seq LIMIT %s""",  # noqa: S608 - fixed SQL fragments only
            (_after(cursor, 1)[0], limit + 1),
        ),
        limit,
        "draw_seq",
    )


async def ledger_draw(conn: Conn, draw_id: str) -> Row | None:
    """A draw as its ledger lists it, with its nonce; None when unknown."""
    row = await _row(conn, f"{_LEDGER} WHERE d.draw_id = %s", (draw_id,))
    if row is not None:
        row["nonce"] = await _value(conn, "SELECT nonce FROM ds.sample_draws WHERE draw_id = %s", (draw_id,))
    return row


async def run_draws(conn: Conn, run_id: str) -> list[Row]:
    """The run's draws, oldest cycle first."""
    return await _rows(conn, "SELECT * FROM ds.sample_draws WHERE run_id = %s ORDER BY cycle", (run_id,))


async def cycle_draws(conn: Conn, run_id: str, cycles: Sequence[int]) -> list[Row]:
    """The draws of the run's ``cycles``, oldest cycle first."""
    return await _rows(
        conn,
        "SELECT * FROM ds.sample_draws WHERE run_id = %s AND cycle = ANY(%s) ORDER BY cycle",
        (run_id, list(cycles)),
    )


async def retrying(conn: Conn, run_id: str, cycle: int) -> bool:
    """Whether every key of the cycle still to be read is a retry."""
    first = await _value(
        conn,
        """SELECT bool_or(retries = 0) FROM ds.run_keys
           WHERE run_id = %s AND cycle = %s AND state IN ('pending', 'leased', 'recheck')""",
        (run_id, cycle),
    )
    return first is False


async def finished_since(conn: Conn, run_id: str, cycle: int, seconds: int) -> int:
    """How many of the cycle's keys are done with a result written in the last ``seconds``."""
    return await _value(
        conn,
        """SELECT count(*) FROM ds.run_keys k JOIN ds.results r USING (run_id, cycle, key_masked)
           WHERE k.run_id = %s AND k.cycle = %s AND k.state = 'done'
             AND r.updated_at > now() - %s * interval '1 second'""",
        (run_id, cycle, seconds),
    )


async def newest_spec(conn: Conn, name: str) -> Row | None:
    """The newest stored version of the spec ``name``."""
    return await _row(conn, "SELECT * FROM ds.specs WHERE name = %s ORDER BY spec_id DESC LIMIT 1", (name,))
