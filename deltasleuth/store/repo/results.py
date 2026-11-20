"""The queries of observations, results and findings."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Container, Mapping, Sequence
from typing import Any, LiteralString

from deltasleuth.core.types import KeyResult, Observation, Side, utc_iso
from deltasleuth.store.repo.base import (
    Conn,
    Page,
    Row,
    _after,
    _current,
    _jsonb,
    _page,
    _row,
    _rows,
    _value,
    storable,
)

_OBSERVATION_FIELDS = (
    "obs_id", "key", "side", "http_status", "content_hash", "latency_ms", "attempts", "pages", "pages_eof",
    "pagination_unstable", "error_class", "headers",
)  # fmt: skip
# An observation row with its key's key_enc.
_OBSERVATION_COLUMNS: LiteralString = """o.run_id, o.obs_id, o.cycle, o.key_masked, o.side, o.pass_no,
    o.http_status, o.content_hash, o.latency_ms, o.attempts, o.pages, o.pages_eof, o.pagination_unstable,
    o.error_class, o.headers_enc, o.observed_at, k.key_enc"""
_BLOB_LOCK = 0x4453424C  # pg advisory lock "DSBL": blob writers (shared) against a blob drain (exclusive)

_FINDING_COLUMNS = (
    "taxonomy_code", "severity", "change_type", "direction", "path_pattern", "json_pointer",
    "value_class", "member_key_tok", "signature_hash", "rule_id", "rule_classification", "suppressed",
)  # fmt: skip


async def lock_blobs(conn: Conn, *, exclusive: bool = False) -> None:
    """Hold the blob lock until the transaction ends: shared for a writer of observations, exclusive for
    a drain deleting unreferenced blobs."""
    if exclusive:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BLOB_LOCK,))
    else:
        await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (_BLOB_LOCK,))


async def insert_observation(
    conn: Conn, run_id: str, cycle: int, pass_no: int, key_masked: str, obs: Observation, headers_enc: bytes
) -> None:
    """Record an observation the evidence store holds (it set obs_id and content_hash) with its sealed
    headers; idempotent."""
    await insert_observations(conn, run_id, cycle, [(pass_no, key_masked, obs, headers_enc)])


async def insert_observations(
    conn: Conn, run_id: str, cycle: int, observations: Sequence[tuple[int, str, Observation, bytes]]
) -> None:
    """``insert_observation`` for each (pass_no, key_masked, observation, headers_enc), in one round trip."""
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.observations (run_id, cycle, pass_no, obs_id, key_masked, side, http_status,
                   content_hash, latency_ms, attempts, pages, pages_eof, pagination_unstable, error_class,
                   headers_enc, observed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, obs_id) DO NOTHING""",
            [
                (
                    run_id, cycle, pass_no, obs.obs_id, key_masked, obs.side.value, obs.http_status,
                    obs.content_hash, obs.latency_ms, obs.attempts, obs.pages, obs.pages_eof,
                    obs.pagination_unstable, obs.error_class, headers_enc, obs.observed_at,
                )
                for pass_no, key_masked, obs, headers_enc in observations
            ],
        )  # fmt: skip


def as_observation(row: Row) -> Observation:
    """An observation row, opened by its run's sealer, as the engine's Observation, without its body
    (``ctx.evidence.get_body(obs.content_hash)`` holds it)."""
    exact = {"side": Side(row["side"]), "observed_at": utc_iso(row["observed_at"])}
    return Observation(body=None, **{name: row[name] for name in _OBSERVATION_FIELDS} | exact)


async def get_observation(conn: Conn, run_id: str, obs_id: str) -> Row | None:
    return (await get_observations(conn, run_id, [obs_id])).get(obs_id)


async def get_observations(conn: Conn, run_id: str, obs_ids: Sequence[str]) -> dict[str, Row]:
    """The run's observation rows of ``obs_ids``, by obs_id, in one query."""
    rows = await _rows(
        conn,
        f"""SELECT {_OBSERVATION_COLUMNS}
            FROM ds.observations o JOIN ds.run_keys k USING (run_id, cycle, key_masked)
            WHERE o.run_id = %s AND o.obs_id = ANY(%s)""",  # noqa: S608 - fixed SQL fragments only
        (run_id, list(obs_ids)),
    )
    return {row["obs_id"]: row for row in rows}


async def list_observations(conn: Conn, run_id: str, cycle: int | None = None) -> list[Row]:
    """Every observation row of a cycle (all passes), in manifest order."""
    return await _rows(
        conn,
        f"""SELECT {_OBSERVATION_COLUMNS}
            FROM ds.observations o JOIN ds.run_keys k USING (run_id, cycle, key_masked)
            WHERE o.run_id = %s AND o.cycle = %s ORDER BY k.seq, o.pass_no, o.side""",  # noqa: S608
        (run_id, await _current(conn, run_id, cycle)),
    )


def _result_params(run_id: str, cycle: int, key_masked: str, r: KeyResult) -> dict[str, Any]:
    details = {f"side_{s}_detail": st.detail for s, st in (("a", r.side_a), ("b", r.side_b)) if st.detail}
    return {
        "run_id": run_id,
        "cycle": cycle,
        "key_masked": key_masked,
        "outcome": r.outcome.value,
        "side_a_state": r.side_a.kind.value,
        "side_a_reason": r.side_a.reason,
        "side_b_state": r.side_b.kind.value,
        "side_b_reason": r.side_b.reason,
        "inconclusive_reason": r.inconclusive_reason,
        "drift_reason": r.drift_reason.value if r.drift_reason else None,
        "confirmation": r.confirmation.value,
        "first_outcome": (r.first_outcome or r.outcome).value,
        "suppressed_count": r.suppressed_count,
        "obs_a_id": r.obs_a_id,
        "obs_b_id": r.obs_b_id,
        "attrs": _jsonb(r.attrs | details),
    }


def _finding_params(
    run_id: str, cycle: int, key_masked: str, seq: int, f: Mapping[str, Any],
    kept: dict[tuple[str, str, str], list[str]],
) -> dict[str, Any]:
    """Keep the prior id for this published pointer, pattern and change, in finding order."""
    a, b = f["value_a_tok"], f["value_b_tok"]
    columns = storable({name: f[name] for name in _FINDING_COLUMNS})
    ids = kept.get((columns["json_pointer"], columns["path_pattern"], columns["change_type"]))
    return columns | {
        "run_id": run_id,
        "cycle": cycle,
        "key_masked": key_masked,
        "seq": seq,
        "finding_id": ids.pop() if ids else "F-" + secrets.token_hex(8),
        "value_a": None if a is None else _jsonb(a),
        "value_b": None if b is None else _jsonb(b),
    }


async def save_key_result(
    conn: Conn,
    run_id: str,
    cycle: int,
    key_masked: str,
    result: KeyResult,
    findings: Sequence[Mapping[str, Any]],
) -> int:
    """Upsert a key's result, by its masked key, and replace its findings in one transaction; return
    the revision.

    ``findings`` are ``security.tokenise.mask_finding`` rows of ``result.findings``, in order. A
    finding at the pointer and of the change type of one it replaces (a recheck) keeps its ``F-`` id,
    so a citation of it stays valid; any other gets a new one. Side-state details go to ``attrs``
    (side_a_detail, side_b_detail).
    """
    return (await save_key_results(conn, run_id, cycle, [(key_masked, result, findings)]))[0]


async def save_key_results(
    conn: Conn,
    run_id: str,
    cycle: int,
    items: Sequence[tuple[str, KeyResult, Sequence[Mapping[str, Any]]]],
    fresh: Container[str] = (),
) -> list[int]:
    """``save_key_result`` for each (key_masked, result, masked findings), with one statement per table.
    A key in ``fresh`` (masked) was read from its first pass again, a sample's retry: its result's first
    outcome is taken afresh. Every write keeps a result's ``attrs.retried``."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_a_reason,
                   side_b_state, side_b_reason, inconclusive_reason, drift_reason, confirmation,
                   first_outcome, suppressed_count, obs_a_id, obs_b_id, attrs)
               VALUES (%(run_id)s, %(cycle)s, %(key_masked)s, %(outcome)s, %(side_a_state)s,
                   %(side_a_reason)s, %(side_b_state)s, %(side_b_reason)s, %(inconclusive_reason)s,
                   %(drift_reason)s, %(confirmation)s, %(first_outcome)s, %(suppressed_count)s, %(obs_a_id)s,
                   %(obs_b_id)s, %(attrs)s)
               ON CONFLICT (run_id, cycle, key_masked) DO UPDATE SET
                   outcome = EXCLUDED.outcome,
                   side_a_state = EXCLUDED.side_a_state, side_a_reason = EXCLUDED.side_a_reason,
                   side_b_state = EXCLUDED.side_b_state, side_b_reason = EXCLUDED.side_b_reason,
                   inconclusive_reason = EXCLUDED.inconclusive_reason,
                   drift_reason = EXCLUDED.drift_reason, confirmation = EXCLUDED.confirmation,
                   first_outcome = CASE WHEN %(fresh)s THEN EXCLUDED.first_outcome
                                        ELSE ds.results.first_outcome END,
                   suppressed_count = EXCLUDED.suppressed_count,
                   obs_a_id = EXCLUDED.obs_a_id, obs_b_id = EXCLUDED.obs_b_id,
                   attrs = EXCLUDED.attrs || jsonb_strip_nulls(
                       jsonb_build_object('retried', ds.results.attrs->'retried')),
                   revision = ds.results.revision + 1, updated_at = now()
               RETURNING revision""",
            [
                _result_params(run_id, cycle, key_masked, result) | {"fresh": key_masked in fresh}
                for key_masked, result, _ in items
            ],
            returning=True,
        )
        revisions = []
        while True:
            returned = await cur.fetchone()
            assert returned is not None  # each statement returns its row
            revisions.append(returned[0])
            if not cur.nextset():
                break
        await cur.execute(
            """WITH prior AS (
                   DELETE FROM ds.findings WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s)
                   RETURNING key_masked, json_pointer, path_pattern, change_type, finding_id, seq)
               SELECT key_masked, json_pointer, path_pattern, change_type, finding_id FROM prior
               ORDER BY seq DESC""",
            (run_id, cycle, [key_masked for key_masked, _, _ in items]),
        )
        kept: dict[str, dict[tuple[str, str, str], list[str]]] = {}
        for key_masked, pointer, pattern, change_type, finding_id in await cur.fetchall():
            kept.setdefault(key_masked, {}).setdefault((pointer, pattern, change_type), []).append(finding_id)
        await cur.executemany(
            """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, taxonomy_code, severity,
                   change_type, direction, path_pattern, json_pointer, value_a, value_b, value_class,
                   member_key_tok, signature_hash, rule_id, rule_classification, suppressed)
               VALUES (%(run_id)s, %(cycle)s, %(key_masked)s, %(seq)s, %(finding_id)s, %(taxonomy_code)s,
                   %(severity)s, %(change_type)s, %(direction)s, %(path_pattern)s, %(json_pointer)s,
                   %(value_a)s, %(value_b)s, %(value_class)s, %(member_key_tok)s, %(signature_hash)s,
                   %(rule_id)s, %(rule_classification)s, %(suppressed)s)""",
            [
                _finding_params(run_id, cycle, key_masked, seq, f, kept.setdefault(key_masked, {}))
                for key_masked, _, findings in items
                for seq, f in enumerate(findings)
            ],
        )
    return revisions


async def unexplained_critical(conn: Conn, run_id: str) -> dict[str, int]:
    """Each key's unsuppressed CRITICAL findings in its latest result (any cycle), by masked key; a key whose
    latest result has none is left out."""
    rows = await _rows(
        conn,
        """SELECT key_masked, critical FROM (
               SELECT DISTINCT ON (r.key_masked) r.key_masked, count(f.seq) AS critical
               FROM ds.results r LEFT JOIN ds.findings f
                   ON (f.run_id, f.cycle, f.key_masked) = (r.run_id, r.cycle, r.key_masked)
                  AND f.severity = 'CRITICAL' AND NOT f.suppressed
               WHERE r.run_id = %s
               GROUP BY r.key_masked, r.cycle ORDER BY r.key_masked, r.cycle DESC) latest
           WHERE critical > 0""",
        (run_id,),
    )
    return {r["key_masked"]: r["critical"] for r in rows}


async def unexplained_criticals(conn: Conn, run_id: str, cycle: int, bug_rules: Sequence[str]) -> int:
    """How many keys of the cycle have a CRITICAL finding that no rule explains: unsuppressed, or
    suppressed by one of ``bug_rules`` (the ids of the spec's ``classification: bug`` rules)."""
    return await _value(
        conn,
        """SELECT count(DISTINCT key_masked) FROM ds.findings
           WHERE run_id = %s AND cycle = %s AND severity = 'CRITICAL'
             AND (NOT suppressed OR rule_id = ANY(%s))""",
        (run_id, cycle, list(bug_rules)),
    )


async def get_key_detail(conn: Conn, run_id: str, key_masked: str, cycle: int | None = None) -> Row | None:
    """A key as its view shows it: the run_keys row (with key_enc) plus ``result`` (None until
    compared), ``findings`` and ``observations`` (every pass, with headers_enc)."""
    detail = await _row(
        conn,
        """SELECT key_masked, key_enc, cycle, seq, state, pass_no FROM ds.run_keys
           WHERE run_id = %s AND cycle = %s AND key_masked = %s""",
        (run_id, await _current(conn, run_id, cycle), key_masked),
    )
    if detail is None:
        return None
    where = (run_id, detail["cycle"], key_masked)
    detail["result"] = await _row(
        conn, "SELECT * FROM ds.results WHERE run_id = %s AND cycle = %s AND key_masked = %s", where
    )
    detail["findings"] = await _rows(
        conn,
        "SELECT * FROM ds.findings WHERE run_id = %s AND cycle = %s AND key_masked = %s ORDER BY seq",
        where,
    )
    detail["observations"] = await _rows(
        conn,
        f"""SELECT {_OBSERVATION_COLUMNS}
            FROM ds.observations o JOIN ds.run_keys k USING (run_id, cycle, key_masked)
            WHERE o.run_id = %s AND o.cycle = %s AND o.key_masked = %s
            ORDER BY o.pass_no, o.side""",  # noqa: S608 - fixed SQL fragments only
        where,
    )
    return detail


async def get_results(conn: Conn, run_id: str, cycle: int, keys_masked: Sequence[str]) -> dict[str, Row]:
    """The cycle's results of ``keys_masked``, by masked key, in one query."""
    rows = await _rows(
        conn,
        "SELECT * FROM ds.results WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s)",
        (run_id, cycle, list(keys_masked)),
    )
    return {row["key_masked"]: row for row in rows}


async def get_findings(
    conn: Conn, run_id: str, cycle: int, keys_masked: Sequence[str]
) -> dict[str, list[Row]]:
    """The cycle's findings of ``keys_masked``, by masked key, each key's in finding order, in one query."""
    rows = await _rows(
        conn,
        """SELECT * FROM ds.findings WHERE run_id = %s AND cycle = %s AND key_masked = ANY(%s)
           ORDER BY key_masked, seq""",
        (run_id, cycle, list(keys_masked)),
    )
    found: dict[str, list[Row]] = {}
    for row in rows:
        found.setdefault(row["key_masked"], []).append(row)
    return found


async def list_results(
    conn: Conn,
    run_id: str,
    cycle: int | None = None,
    *,
    outcome: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Results in manifest order, each with its key's ``seq``."""
    rows = await _rows(
        conn,
        """SELECT k.seq, r.*
           FROM ds.results r JOIN ds.run_keys k USING (run_id, cycle, key_masked)
           WHERE r.run_id = %(run_id)s AND r.cycle = %(cycle)s AND k.seq > %(after)s
             AND (%(outcome)s::text IS NULL OR r.outcome = %(outcome)s)
           ORDER BY k.seq LIMIT %(limit)s""",
        {
            "run_id": run_id,
            "cycle": await _current(conn, run_id, cycle),
            "outcome": outcome,
            "after": _after(cursor, 1)[0],
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "seq")


async def list_findings(
    conn: Conn,
    run_id: str,
    cycle: int | None = None,
    *,
    key_masked: str | None = None,
    root_cause_id: str | None = None,
    taxonomy_code: str | None = None,
    path_pattern: str | None = None,
    severity: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Findings in manifest then finding order, with their key's ``key_seq`` and their result's
    outcome, confirmation and observation ids. ``k.seq >= key_after`` lets a page seek its first key by
    index: the row comparison alone would join every earlier key again on each page."""
    key_after, finding_after = _after(cursor, 2)
    rows = await _rows(
        conn,
        """SELECT k.seq AS key_seq, r.outcome, r.confirmation, r.obs_a_id, r.obs_b_id, f.*
           FROM ds.findings f
           JOIN ds.run_keys k USING (run_id, cycle, key_masked)
           JOIN ds.results r USING (run_id, cycle, key_masked)
           WHERE f.run_id = %(run_id)s AND f.cycle = %(cycle)s AND k.seq >= %(key_after)s
             AND (k.seq, f.seq) > (%(key_after)s, %(finding_after)s)
             AND (%(key_masked)s::text IS NULL OR f.key_masked = %(key_masked)s)
             AND (%(root_cause_id)s::text IS NULL OR f.root_cause_id = %(root_cause_id)s)
             AND (%(taxonomy_code)s::text IS NULL OR f.taxonomy_code = %(taxonomy_code)s)
             AND (%(path_pattern)s::text IS NULL OR f.path_pattern = %(path_pattern)s)
             AND (%(severity)s::text IS NULL OR f.severity = %(severity)s)
           ORDER BY k.seq, f.seq LIMIT %(limit)s""",
        {
            "run_id": run_id,
            "cycle": await _current(conn, run_id, cycle),
            "key_after": key_after,
            "finding_after": finding_after,
            "key_masked": key_masked,
            "root_cause_id": root_cause_id,
            "taxonomy_code": taxonomy_code,
            "path_pattern": path_pattern,
            "severity": severity,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "key_seq", "seq")


# What a cycle's end and its reports read of a cycle (the four reads below) probes by key, also where the
# planner's statistics do not hold the cycle yet (a later cycle of a soak, a run small beside the store:
# autovacuum analyses a table only once a tenth of it changed) and it takes the cycle for one row. Written
# as plain joins, each then joined on (run_id, cycle) alone and read the whole cycle for every page or
# finding. So each reads its keys by their order (``seq``, the one index holding it), and each key's rows
# in a LATERAL subquery that ``OFFSET 0`` keeps from being flattened and that takes run and cycle from the
# key's row, never as constants: the cycle's constants are what the planner believes match one row, for
# which any index does.


async def result_pages(
    conn: Conn, run_id: str, cycle: int, *, size: int = 1000
) -> AsyncIterator[tuple[list[Row], dict[str, list[Row]]]]:
    """A cycle's results in manifest order, ``size`` at a time, each page with its keys' findings by
    masked key, in finding order: a cycle's end and its reports hold a page of it, never every row. A page
    walks the cycle's keys by their order from where the page before ended, reading each key's result by
    key, then walks its keys again for their findings."""
    after, more = -1, True
    while more:
        rows = await _rows(
            conn,
            """SELECT k.seq, r.*
               FROM ds.run_keys k CROSS JOIN LATERAL (
                   SELECT r.* FROM ds.results r
                   WHERE (r.run_id, r.cycle, r.key_masked) = (k.run_id, k.cycle, k.key_masked) OFFSET 0) r
               WHERE k.run_id = %s AND k.cycle = %s AND k.seq > %s ORDER BY k.seq LIMIT %s""",
            (run_id, cycle, after, size + 1),
        )
        results, more = rows[:size], len(rows) > size
        last = results[-1]["seq"] if results else after
        found = await _rows(
            conn,
            """SELECT f.*
               FROM ds.run_keys k CROSS JOIN LATERAL (
                   SELECT f.* FROM ds.findings f
                   WHERE (f.run_id, f.cycle, f.key_masked) = (k.run_id, k.cycle, k.key_masked) OFFSET 0) f
               WHERE k.run_id = %s AND k.cycle = %s AND k.seq > %s AND k.seq <= %s
               ORDER BY k.seq, f.seq""",
            (run_id, cycle, after, last),
        )
        findings: dict[str, list[Row]] = {}
        for f in found:
            findings.setdefault(f["key_masked"], []).append(f)
        yield results, findings
        after = last


async def discrepant_findings(
    conn: Conn, run_id: str, cycle: int, severities: Sequence[str], limit: int
) -> tuple[list[Row], int]:
    """The cycle's first ``limit`` discrepant findings (of ``severities``, and unsuppressed or suppressed
    by a ``bug`` rule), in the order of ``severities`` then in manifest and finding order, as
    ``list_findings`` gives them, and how many it has. The cycle's findings are read once, and the key and
    the result of each discrepant one by key."""
    rows = await _rows(
        conn,
        """SELECT k.seq AS key_seq, r.outcome, r.confirmation, r.obs_a_id, r.obs_b_id, f.*,
                  count(*) OVER () AS discrepant
           FROM ds.findings f
           CROSS JOIN LATERAL (
               SELECT k.seq FROM ds.run_keys k
               WHERE (k.run_id, k.cycle, k.key_masked) = (f.run_id, f.cycle, f.key_masked) OFFSET 0) k
           CROSS JOIN LATERAL (
               SELECT r.outcome, r.confirmation, r.obs_a_id, r.obs_b_id FROM ds.results r
               WHERE (r.run_id, r.cycle, r.key_masked) = (f.run_id, f.cycle, f.key_masked) OFFSET 0) r
           WHERE f.run_id = %(run_id)s AND f.cycle = %(cycle)s AND f.severity::text = ANY(%(severities)s)
             AND (NOT f.suppressed OR f.rule_classification = 'bug')
           ORDER BY array_position(%(severities)s, f.severity::text), k.seq, f.seq LIMIT %(limit)s""",
        {"run_id": run_id, "cycle": cycle, "severities": list(severities), "limit": limit},
    )
    return rows, max((row.pop("discrepant") for row in rows), default=0)


async def rechecks(conn: Conn, run_id: str, cycle: int, seqs: Sequence[int]) -> dict[str, dict[str, str]]:
    """key_masked -> side -> the obs_id of the side's last read after the first pass, for the keys at
    ``seqs`` (a result's ``seq``; at least one) that were read again: of its highest pass, and there the
    latest read (a sample member sent back to its first pass leaves the abandoned attempt's reads at the
    same passes). The keys from the first to the last of ``seqs`` are walked, and each one's reads read by
    key."""
    rows = await _rows(
        conn,
        """SELECT DISTINCT ON (k.seq, o.side) o.key_masked, o.side, o.obs_id
           FROM ds.run_keys k CROSS JOIN LATERAL (
               SELECT o.key_masked, o.side, o.obs_id, o.pass_no, o.observed_at FROM ds.observations o
               WHERE (o.run_id, o.cycle, o.key_masked) = (k.run_id, k.cycle, k.key_masked) AND o.pass_no > 1
               OFFSET 0) o
           WHERE k.run_id = %s AND k.cycle = %s AND k.seq BETWEEN %s AND %s AND k.seq = ANY(%s)
           ORDER BY k.seq, o.side, o.pass_no DESC, o.observed_at DESC, o.obs_id DESC""",
        (run_id, cycle, min(seqs), max(seqs), list(seqs)),
    )
    found: dict[str, dict[str, str]] = {}
    for row in rows:
        found.setdefault(row["key_masked"], {})[row["side"]] = row["obs_id"]
    return found


async def bug_rules(conn: Conn, run_id: str, cycle: int, ids: Sequence[str] | None = None) -> dict[str, str]:
    """root_cause_id -> the ``bug`` rule that suppressed a finding linked to it (the last one in manifest
    and finding order), for the cycle's root causes that have one. Each such finding's key is read by
    key."""
    rows = await _rows(
        conn,
        """SELECT DISTINCT ON (f.root_cause_id) f.root_cause_id, f.rule_id
           FROM ds.findings f CROSS JOIN LATERAL (
               SELECT k.seq FROM ds.run_keys k
               WHERE (k.run_id, k.cycle, k.key_masked) = (f.run_id, f.cycle, f.key_masked) OFFSET 0) k
           WHERE f.run_id = %s AND f.cycle = %s AND f.root_cause_id IS NOT NULL AND f.suppressed
             AND f.rule_classification = 'bug' AND (%s::text[] IS NULL OR f.root_cause_id = ANY(%s))
           ORDER BY f.root_cause_id, k.seq DESC, f.seq DESC""",
        (run_id, cycle, ids, ids),
    )
    return {row["root_cause_id"]: row["rule_id"] for row in rows}
