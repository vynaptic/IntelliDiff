"""The queries of labelled accuracy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, LiteralString

from deltasleuth.store.repo.base import _DIFFERENT, _MATCH, Conn, Row, _jsonb, _one, _row, _rows, _sortable_id

# A labelling with its actors' names, its sides' roles, its cycle's labellings counted (drawn, labelled)
# and its labeller's independence: whether the labeller answers for the run's start, and the spec
# approval the cycle cites when the labeller wrote it.
_LABELLINGS: LiteralString = """SELECT v.*, ds.actor_name(v.drawn_by) AS drawn_by_name,
        ds.actor_name(v.labelled_by) AS labelled_by_name, s.body #>> '{sides,a,role}' AS role_a,
        s.body #>> '{sides,b,role}' AS role_b,
        (SELECT count(*) FROM ds.labellings o
         WHERE (o.run_id, o.cycle) = (v.run_id, v.cycle)) AS cycle_drawn,
        (SELECT count(o.labelled_at) FROM ds.labellings o
         WHERE (o.run_id, o.cycle) = (v.run_id, v.cycle)) AS cycle_labelled,
        ds.accountable(v.labelled_by) && ds.accountable(r.created_by) AS started_run,
        (SELECT p.approval_id FROM ds.approvals p
         WHERE p.approval_id = coalesce(c.approval_id, (
                   SELECT a.approval_id FROM ds.approvals a
                   WHERE a.spec_id = r.spec_id AND a.kind = 'spec' AND a.status = 'approved'))
           AND p.authors && ds.accountable(v.labelled_by)) AS spec_author
    FROM ds.labellings v JOIN ds.runs r ON r.run_id = v.run_id JOIN ds.specs s ON s.spec_id = r.spec_id
    JOIN ds.cycles c ON (c.run_id, c.cycle) = (v.run_id, v.cycle)"""
# A labelling as lists show it: no sheet and no selection, of which only the items drawn are counted,
# and of its score what its one-line summary reads, without each root cause's and rule's figures.
_LISTED: LiteralString = """SELECT v.labelling_id, v.run_id, v.cycle, v.salt, v.size, v.per_cause,
        v.accepted, v.agreed, v.truth_source, v.truth_as_of, v.selection_sha256, v.drawn_by, v.drawn_at,
        v.sheet_sha256, v.labels_sha256, v.score_sha256, v.labelled_by, v.labelled_at, v.purged_at,
        ds.actor_name(v.drawn_by) AS drawn_by_name, ds.actor_name(v.labelled_by) AS labelled_by_name,
        s.body #>> '{sides,a,role}' AS role_a, s.body #>> '{sides,b,role}' AS role_b,
        (v.selection #>> '{counts,discrepant,drawn}')::int + (v.selection #>> '{counts,accepted,drawn}')::int
            + (v.selection #>> '{counts,agreed,drawn}')::int AS item_count,
        v.accuracy #- '{discrepant,by_cause}' #- '{accepted,by_rule}' AS accuracy
    FROM ds.labellings v JOIN ds.runs r ON r.run_id = v.run_id JOIN ds.specs s ON s.spec_id = r.spec_id"""
# The population of the result ``k``: what ``types.derive_verdict`` calls DIFFERENT is discrepant; of what
# it calls MATCH, a key read identical is agreed and the rest (noise, or a difference a rule explains)
# accepted.
_POPULATION: LiteralString = f"""CASE WHEN {_DIFFERENT} THEN 'discrepant'
        WHEN k.outcome = 'MATCH' THEN 'agreed' WHEN {_MATCH} THEN 'accepted' ELSE 'indeterminate' END"""
_BY_DIGEST: LiteralString = 'digest, key_masked COLLATE "C", cause COLLATE "C"'
_DRAWN: LiteralString = "population, key_masked, cause, digest, NULL::int AS seq, NULL::bigint AS n"


async def labelling_draw(
    conn: Conn,
    run_id: str,
    cycle: int,
    *,
    salt: str,
    size: int,
    per_cause: int,
    accepted: int,
    agreed: int,
    timeout_s: int,
) -> list[Row]:
    """A labelled sample's draw from the ended cycle, in one statement stopped after ``timeout_s``
    (QueryCanceled): the cycle's results and discrepant findings each read once, every item (a discrepant
    key in a root cause, ``*`` without one; an accepted or agreed key, when asked for) hashed once with
    ``salt`` as ``sampling.digest`` hashes ``key_masked U+001F cause``. Its joins are hash or merge joins
    whatever rows the planner expects (``enable_nestloop`` off): a nested loop over the cycle's rows, which
    it takes when the statistics say few findings have a root cause, is quadratic. Both settings last until
    the caller's transaction ends, which holds this statement alone. Rows by ``kind``: ``uniform`` (each
    population's ``size``, ``accepted`` or ``agreed`` smallest digests), ``coverage`` (each cause's
    ``per_cause`` smallest), ``cause`` (its ``seq`` and items ``n``), ``keys`` (per population, the
    indeterminate ones too), ``not_compared``, ``matched_by_row_hash`` and ``rule`` (accepted keys per
    suppressing rule, ``mask`` for none)."""
    await conn.execute(
        "SELECT set_config('statement_timeout', %s, true), set_config('enable_nestloop', 'off', true)",
        (str(timeout_s * 1000),),
    )
    return await _rows(
        conn,
        f"""WITH res AS MATERIALIZED (
                SELECT k.key_masked, {_POPULATION} AS population
                FROM ds.results k WHERE k.run_id = %(r)s AND k.cycle = %(c)s),
            caused AS MATERIALIZED (
                SELECT DISTINCT f.key_masked, f.root_cause_id AS cause
                FROM ds.findings f JOIN res USING (key_masked)
                WHERE f.run_id = %(r)s AND f.cycle = %(c)s AND f.root_cause_id IS NOT NULL
                  AND res.population = 'discrepant'),
            items AS MATERIALIZED (
                SELECT i.*, sha256(convert_to(%(salt)s || chr(31) || i.key_masked || chr(31) || i.cause,
                                              'UTF8')) AS digest
                FROM (SELECT 'discrepant' AS population, key_masked, cause FROM caused
                      UNION ALL
                      SELECT population, key_masked, '*' FROM res
                      WHERE population = 'discrepant'
                        AND NOT EXISTS (SELECT 1 FROM caused WHERE caused.key_masked = res.key_masked)
                      UNION ALL
                      SELECT population, key_masked, '*' FROM res
                      WHERE (population = 'accepted' AND %(accepted)s > 0)
                         OR (population = 'agreed' AND %(agreed)s > 0)) AS i)
            (SELECT 'uniform' AS kind, {_DRAWN} FROM items WHERE population = 'discrepant'
             ORDER BY {_BY_DIGEST} LIMIT %(size)s)
            UNION ALL
            (SELECT 'uniform', {_DRAWN} FROM items WHERE population = 'accepted'
             ORDER BY {_BY_DIGEST} LIMIT %(accepted)s)
            UNION ALL
            (SELECT 'uniform', {_DRAWN} FROM items WHERE population = 'agreed'
             ORDER BY {_BY_DIGEST} LIMIT %(agreed)s)
            UNION ALL
            (SELECT 'coverage', {_DRAWN}
             FROM (SELECT i.*, row_number() OVER (PARTITION BY cause
                                                 ORDER BY digest, key_masked COLLATE "C") AS place
                   FROM items i WHERE population = 'discrepant' AND cause <> '*' AND %(per_cause)s > 0) AS i
             WHERE place <= %(per_cause)s)
            UNION ALL
            (SELECT 'cause', 'discrepant', NULL, i.cause, NULL, rc.seq, i.n
             FROM (SELECT cause, count(*) AS n FROM items WHERE population = 'discrepant' GROUP BY cause) AS i
             LEFT JOIN ds.root_causes rc ON (rc.run_id, rc.cycle, rc.root_cause_id) = (%(r)s, %(c)s, i.cause))
            UNION ALL
            (SELECT 'keys', population, NULL, NULL, NULL, NULL, count(*) FROM res GROUP BY population)
            UNION ALL
            (SELECT 'not_compared', NULL, NULL, NULL, NULL, NULL,
                    (SELECT count(*) FROM ds.run_keys WHERE run_id = %(r)s AND cycle = %(c)s)
                    - (SELECT count(*) FROM res))
            UNION ALL
            (SELECT 'matched_by_row_hash', NULL, NULL, NULL, NULL, NULL, (state->>'matched')::bigint
             FROM ds.scans WHERE run_id = %(r)s AND cycle = %(c)s)
            UNION ALL
            (SELECT 'rule', 'accepted', NULL, coalesce(f.rule_id, 'mask'), NULL, NULL,
                    count(DISTINCT res.key_masked)
             FROM res LEFT JOIN ds.findings f
                  ON (f.run_id, f.cycle, f.key_masked) = (%(r)s, %(c)s, res.key_masked)
                 AND f.suppressed AND f.rule_id IS NOT NULL
             WHERE res.population = 'accepted' AND %(accepted)s > 0
             GROUP BY coalesce(f.rule_id, 'mask'))""",  # noqa: S608 - fixed SQL fragments only
        {"r": run_id, "c": cycle, "salt": salt, "size": size, "per_cause": per_cause, "accepted": accepted,
         "agreed": agreed},
    )  # fmt: skip


async def labelling_items(
    conn: Conn, run_id: str, cycle: int, items: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], Row]:
    """What a labelling's sheet shows of each drawn item (masked key, cause): its key's outcome and
    observations, ``observed_at`` (the later observation's, else the cycle's end), its suppressing
    ``rules`` and its cause's masked pattern and change type."""
    rows = await _rows(
        conn,
        """SELECT i.key_masked, i.cause, k.outcome, k.obs_a_id, k.obs_b_id, rc.path_pattern, rc.change_type,
                  coalesce(greatest(a.observed_at, b.observed_at), c.ended_at) AS observed_at,
                  ARRAY(SELECT DISTINCT f.rule_id FROM ds.findings f
                        WHERE (f.run_id, f.cycle, f.key_masked) = (k.run_id, k.cycle, k.key_masked)
                          AND f.suppressed AND f.rule_id IS NOT NULL ORDER BY f.rule_id) AS rules
           FROM unnest(%(keys)s::text[], %(causes)s::text[]) AS i (key_masked, cause)
           JOIN ds.results k ON (k.run_id, k.cycle, k.key_masked) = (%(r)s, %(c)s, i.key_masked)
           JOIN ds.cycles c ON (c.run_id, c.cycle) = (k.run_id, k.cycle)
           LEFT JOIN ds.observations a ON (a.run_id, a.obs_id) = (k.run_id, k.obs_a_id)
           LEFT JOIN ds.observations b ON (b.run_id, b.obs_id) = (k.run_id, k.obs_b_id)
           LEFT JOIN ds.root_causes rc
                ON (rc.run_id, rc.cycle, rc.root_cause_id) = (k.run_id, k.cycle, i.cause)""",
        {"r": run_id, "c": cycle, "keys": [key for key, _ in items], "causes": [cause for _, cause in items]},
    )
    return {(row["key_masked"], row["cause"]): row for row in rows}


async def insert_labelling(conn: Conn, labelling: Mapping[str, Any]) -> Row:
    """A labelling (``V-`` id) drawn from the ended cycle: ``labelling``'s run_id, cycle, salt, size,
    per_cause, accepted, agreed, truth_source, truth_as_of, selection, selection_sha256, sheet and
    drawn_by. The stored row."""
    return await _one(
        conn,
        """INSERT INTO ds.labellings (labelling_id, run_id, cycle, salt, size, per_cause, accepted, agreed,
               truth_source, truth_as_of, selection, selection_sha256, sheet, drawn_by)
           VALUES (%(labelling_id)s, %(run_id)s, %(cycle)s, %(salt)s, %(size)s, %(per_cause)s, %(accepted)s,
               %(agreed)s, %(truth_source)s, %(truth_as_of)s, %(selection)s, %(selection_sha256)s, %(sheet)s,
               %(drawn_by)s)
           RETURNING *""",
        dict(labelling) | {"labelling_id": _sortable_id("V-"), "selection": _jsonb(labelling["selection"])},
    )


async def repeated_labelling(conn: Conn, labelling: Mapping[str, Any], seconds: int) -> Row | None:
    """The first unlabelled labelling of the cycle that ``labelling``'s drawer drew in the last ``seconds``
    with its sizes, truth and acknowledgement; None without one."""
    return await _row(
        conn,
        """SELECT * FROM ds.labellings
           WHERE run_id = %(run_id)s AND cycle = %(cycle)s AND drawn_by = %(drawn_by)s
             AND labelled_at IS NULL AND purged_at IS NULL
             AND drawn_at > now() - %(seconds)s * interval '1 second'
             AND (size, per_cause, accepted, agreed) = (%(size)s, %(per_cause)s, %(accepted)s, %(agreed)s)
             AND (truth_source, truth_as_of) = (%(truth_source)s, %(truth_as_of)s)
             AND (selection->>'unlocatable')::boolean = %(unlocatable)s
           ORDER BY labelling_id LIMIT 1""",
        dict(labelling) | {"seconds": seconds},
    )


async def get_labelling(conn: Conn, labelling_id: str) -> Row | None:
    """The labelling with its actors' names, its sides' roles and its labeller's independence."""
    return await _row(conn, f"{_LABELLINGS} WHERE v.labelling_id = %s", (labelling_id,))


async def list_labellings(
    conn: Conn, run_id: str | None = None, cycle: int | None = None, *, full: bool = False
) -> list[Row]:
    """Every labelling of the ended cycle (of every cycle of the run, or of every run, by default), newest
    first, as lists show them: with its actors' names and sides' roles, ``item_count`` and, as
    ``accuracy``, its score without ``by_cause`` and ``by_rule``; no sheet, no selection. ``full``: as
    ``get_labelling`` shows them."""
    return await _rows(
        conn,
        f"""{_LABELLINGS if full else _LISTED} WHERE (%(run_id)s::text IS NULL OR v.run_id = %(run_id)s)
              AND (%(cycle)s::int IS NULL OR v.cycle = %(cycle)s)
            ORDER BY v.labelling_id DESC""",
        {"run_id": run_id, "cycle": cycle},
    )


async def label_labelling(
    conn: Conn,
    labelling_id: str,
    *,
    sheet: str,
    sheet_sha256: str,
    labels_sha256: str,
    accuracy: Mapping[str, Any],
    score_sha256: str,
    labelled_by: str,
) -> Row | None:
    """Record the labelling's one labelled sheet and its score, as of now(); None when it is labelled
    already."""
    return await _row(
        conn,
        """UPDATE ds.labellings SET sheet = %s, sheet_sha256 = %s, labels_sha256 = %s, accuracy = %s,
               score_sha256 = %s, labelled_by = %s, labelled_at = now()
           WHERE labelling_id = %s AND labelled_at IS NULL RETURNING *""",
        (sheet, sheet_sha256, labels_sha256, _jsonb(accuracy), score_sha256, labelled_by, labelling_id),
    )


async def attested_labelling(conn: Conn, run_id: str, cycle: int, *, lock: bool = True) -> Row | None:
    """The labelling a sign-off request of the ended cycle attests: its newest labelled by a person, as
    ``get_labelling`` shows it. ``lock`` reads the cycle's row FOR KEY SHARE first, so a draw or upload in
    flight commits before this read."""
    if lock:
        await conn.execute(
            "SELECT 1 FROM ds.cycles WHERE run_id = %s AND cycle = %s FOR KEY SHARE", (run_id, cycle)
        )
    return await _row(
        conn,
        f"""{_LABELLINGS} WHERE v.run_id = %s AND v.cycle = %s AND v.labelled_at IS NOT NULL
              AND v.labelled_by LIKE 'user:%%'
            ORDER BY v.labelling_id DESC LIMIT 1""",
        (run_id, cycle),
    )


async def cycle_labelling(conn: Conn, run_id: str, cycle: int, labelling_id: str) -> Row | None:
    """The labelling of that id, if it is the ended cycle's, as ``get_labelling`` shows it."""
    return await _row(
        conn,
        f"{_LABELLINGS} WHERE v.labelling_id = %s AND v.run_id = %s AND v.cycle = %s",
        (labelling_id, run_id, cycle),
    )
