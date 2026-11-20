"""Cycle closing over a transaction-local table of masked grouping facts and manifest positions.

The worker supplies one result page at a time. PostgreSQL can spill the grouping and ordering to disk;
even a group covering the entire run collects at most five keys into an array. No request key, source
document or finding value is copied into the table.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from deltasleuth.core.classify.rootcause import MIN_FANOUT_KEYS, SAMPLE_KEYS, Member, cause_id
from deltasleuth.core.types import Severity
from deltasleuth.store.repo.base import Conn, storable
from deltasleuth.store.repo.causes import analyse_cause_links, replace_root_causes

_PageWriter = Callable[[Sequence[Member], Mapping[str, int]], Awaitable[None]]


@asynccontextmanager
async def cause_members(conn: Conn, run_id: str, cycle: int) -> AsyncIterator[_PageWriter]:
    """Write pages with their masked-key to manifest-position map, then atomically replace causes and links.
    A cycle with no discrepant members needs no staging table."""
    staged = False

    async def write(members: Sequence[Member], positions: Mapping[str, int]) -> None:
        nonlocal staged
        if not members:
            return
        if not staged:
            await conn.execute(
                """CREATE TEMP TABLE ds_cause_members (
                       pos bigint GENERATED ALWAYS AS IDENTITY, key_seq integer NOT NULL, ref text,
                       grp text COLLATE "C" NOT NULL, rc_id text NOT NULL, fan text COLLATE "C", fan_id text,
                       member_tok text, signature text COLLATE "C" NOT NULL, path_pattern text NOT NULL,
                       change_type text NOT NULL, direction text NOT NULL, severity smallint NOT NULL,
                       value_class text COLLATE "C", taxonomy text COLLATE "C") ON COMMIT DROP"""
            )
            staged = True
        async with conn.cursor() as cur, cur.copy(
            """COPY pg_temp.ds_cause_members (key_seq,ref,grp,rc_id,fan,fan_id,member_tok,signature,
                   path_pattern,change_type,direction,severity,value_class,taxonomy) FROM STDIN"""
        ) as copy:
            for m in members:
                await copy.write_row(_facts(m, positions[m.key_masked]))

    async with conn.transaction():
        yield write
        if staged:
            await _publish(conn, run_id, cycle)
            await conn.execute("DROP TABLE pg_temp.ds_cause_members")
        else:
            await replace_root_causes(conn, run_id, cycle, [], {})


def _facts(m: Member, key_seq: int) -> tuple[Any, ...]:
    # The full, unambiguous group identity is distinct from its shorter public id. JSON escapes even
    # NULs before grouping. The two identity arrays differ in length, as well as in their parts.
    sig = (m.signature,)
    fan = m.fanout
    return (
        key_seq, m.ref, json.dumps((*sig, m.bug_rule)), cause_id("signature", sig, m.bug_rule),
        json.dumps((*fan, m.bug_rule)) if fan else None,
        cause_id("member_key", fan, m.bug_rule) if fan else None,
        storable(fan[2]) if fan else None, m.signature, storable(m.path_pattern), m.change_type.value,
        m.direction.value, m.severity.rank, storable(m.value_class), storable(m.taxonomy_code),
    )


async def _publish(conn: Conn, run_id: str, cycle: int) -> None:
    # Claim a member-key group only if it spans enough distinct request keys. A run's manifest has exactly
    # one position per key; repeated findings of the same key cannot create a fan-out.
    await conn.execute("ANALYZE pg_temp.ds_cause_members")
    claimed = await conn.execute(
        """UPDATE pg_temp.ds_cause_members m SET grp = m.fan, rc_id = m.fan_id
           FROM (SELECT fan FROM pg_temp.ds_cause_members WHERE fan IS NOT NULL GROUP BY fan
                 HAVING count(DISTINCT key_seq) >= %s) claimed WHERE m.fan = claimed.fan""",
        (MIN_FANOUT_KEYS,),
    )
    if claimed.rowcount:
        await conn.execute("ANALYZE pg_temp.ds_cause_members")
    params = {"run": run_id, "cycle": cycle, "sample": SAMPLE_KEYS,
              "severities": [s.value for s in sorted(Severity, key=lambda s: s.rank)]}
    await conn.execute("DELETE FROM ds.root_causes WHERE run_id = %(run)s AND cycle = %(cycle)s", params)
    await conn.execute(
        """WITH facts AS (
               SELECT grp, min(pos) AS first, count(*) AS findings, max(severity) AS severity,
                      CASE WHEN min(direction) = max(direction) THEN min(direction) ELSE 'both' END AS dir,
                      CASE WHEN min(signature) = max(signature) THEN min(signature) END AS sig,
                      CASE WHEN count(value_class) = count(*) AND min(value_class) = max(value_class)
                           THEN min(value_class) END AS cls,
                      CASE WHEN count(taxonomy) = count(*) AND min(taxonomy) = max(taxonomy)
                           THEN min(taxonomy) END AS tax
               FROM pg_temp.ds_cause_members GROUP BY grp),
           keys AS (SELECT DISTINCT grp, key_seq FROM pg_temp.ds_cause_members),
           numbered AS (
               SELECT k.grp, m.key_masked,
                      row_number() OVER (PARTITION BY k.grp ORDER BY m.key_masked COLLATE "C") AS n
               FROM keys k JOIN ds.run_keys m
                 ON m.run_id = %(run)s AND m.cycle = %(cycle)s AND m.seq = k.key_seq),
           reach AS (
               SELECT grp, count(*) AS affected,
                      array_agg(key_masked ORDER BY n) FILTER (WHERE n <= %(sample)s) AS samples
               FROM numbered GROUP BY grp),
           causes AS (
               SELECT f.*, r.affected, r.samples, m.rc_id, m.path_pattern, m.change_type,
                      coalesce(m.fan = m.grp, false) AS fanout,
                      CASE WHEN m.fan = m.grp THEN m.member_tok END AS member
               FROM facts f JOIN reach r USING (grp) JOIN pg_temp.ds_cause_members m ON m.pos = f.first)
           INSERT INTO ds.root_causes (run_id,cycle,seq,root_cause_id,kind,taxonomy_code,max_severity,
               path_pattern,change_type,direction,affected_keys,finding_count,member_key_tok,value_class,
               signature_hash,sample_keys)
           SELECT %(run)s, %(cycle)s, row_number() OVER (ORDER BY fanout DESC, severity DESC,
                      affected DESC, findings DESC, rc_id COLLATE "C") - 1,
                  rc_id, CASE WHEN fanout THEN 'member_key' ELSE 'signature' END, tax,
                  (%(severities)s::text[])[severity + 1], path_pattern, change_type, dir, affected, findings,
                  member, cls, sig, samples FROM causes""",
        params,
    )
    await conn.execute(
        """UPDATE ds.findings SET root_cause_id = NULL WHERE run_id = %(run)s AND cycle = %(cycle)s
           AND root_cause_id IS NOT NULL""", params,
    )
    # A ref shared by more than one re-derived finding takes the last cause in published rank order.
    cur = await conn.execute(
        """WITH links AS (
               SELECT DISTINCT ON (m.ref) m.ref, m.rc_id FROM pg_temp.ds_cause_members m
               JOIN ds.root_causes c ON c.run_id = %(run)s AND c.cycle = %(cycle)s
                                    AND c.root_cause_id = m.rc_id
               WHERE m.ref IS NOT NULL ORDER BY m.ref, c.seq DESC)
           UPDATE ds.findings f SET root_cause_id = l.rc_id FROM links l
           WHERE f.run_id = %(run)s AND f.cycle = %(cycle)s AND f.finding_id = l.ref""",
        params,
    )
    await analyse_cause_links(conn, cur.rowcount)
