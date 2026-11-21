"""The queries of licences."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import LiteralString

from deltasleuth.store.repo.audit import last_audit
from deltasleuth.store.repo.base import Conn, Row, _one, _row, _rows

_PAIRS_LOCK = 0x44534C43  # pg advisory lock "DSLC": new pairs of licensed runs, and licence installs

_LICENCE_COLUMNS: LiteralString = """licence_id, text, sha256, installed_by,
    ds.actor_name(installed_by) AS installed_by_name, installed_at, withdrawn_by,
    ds.actor_name(withdrawn_by) AS withdrawn_by_name, withdrawn_at, withdraw_reason"""


async def licence_facts(conn: Conn) -> Row:
    """What ``licence.judge`` decides from: the ``installation_id`` (None without its row), the database's
    ``now``, the newest run's ``created_at`` (``newest_run_at``), the newest ``system.start``'s ``auth``
    and ``pack_key`` (``start``, None before one), each ``licence.install`` audit row's ``{id, object}``
    (``installs``) and the stored licences (``rows``), withdrawn ones included."""
    facts = await _one(
        conn,
        """SELECT (SELECT installation_id FROM ds.installation) AS installation_id, now() AS now,
                  (SELECT max(created_at) FROM ds.runs) AS newest_run_at,
                  (SELECT coalesce(jsonb_agg(jsonb_build_object('id', id, 'object', object) ORDER BY id),
                                   '[]')
                   FROM ds.audit_log WHERE action = 'licence.install') AS installs""",
    )
    start = await last_audit(conn, "system.start")
    facts["start"] = start and {name: start["detail"].get(name) for name in ("auth", "pack_key")}
    facts["rows"] = await _rows(conn, f"SELECT {_LICENCE_COLUMNS} FROM ds.licences ORDER BY licence_id")  # noqa: S608
    return facts


async def get_licence(conn: Conn, licence_id: str) -> Row | None:
    return await _row(
        conn,
        f"SELECT {_LICENCE_COLUMNS} FROM ds.licences WHERE licence_id = %s",  # noqa: S608
        (licence_id,),
    )


async def insert_licence(conn: Conn, licence_id: str, text: str, sha256: str, *, installed_by: str) -> Row:
    """Store a licence's normalised text; ``licence.install`` checks it first."""
    return await _one(
        conn,
        """INSERT INTO ds.licences (licence_id, text, sha256, installed_by) VALUES (%s, %s, %s, %s)
           RETURNING *""",
        (licence_id, text, sha256, installed_by),
    )


async def withdraw_licence(conn: Conn, licence_id: str, by: str, reason: str) -> Row | None:
    """Withdraw the licence as of now() (ds.tg_licences refuses any other time); None when it is not
    installed or is withdrawn already."""
    return await _row(
        conn,
        """UPDATE ds.licences SET withdrawn_by = %s, withdrawn_at = now(), withdraw_reason = %s
           WHERE licence_id = %s AND withdrawn_at IS NULL RETURNING *""",
        (by, reason, licence_id),
    )


async def counted_pairs(conn: Conn, since: datetime | None) -> list[Row]:
    """Each spec name with a run in the window from ``since`` (every run when None), a run that
    finished before it being outside it, by its first such run: ``{name, first_at}``, oldest first."""
    return await _rows(
        conn,
        """SELECT s.name, min(w.first_at) AS first_at
           FROM (SELECT spec_id, min(created_at) AS first_at FROM ds.runs
                 WHERE coalesce(finished_at, 'infinity') >= coalesce(%(since)s::timestamptz, '-infinity')
                 GROUP BY spec_id) w
           JOIN ds.specs s USING (spec_id)
           GROUP BY s.name ORDER BY first_at, s.name""",
        {"since": since},
    )


async def pair_targets(conn: Conn, names: Sequence[str]) -> dict[str, list[str]]:
    """Each name's ``<side a target> vs <side b target>`` over its versions, for display only."""
    rows = await _rows(
        conn,
        """SELECT name,
                  array_agg(DISTINCT (body #>> '{sides,a,target}') || ' vs ' || (body #>> '{sides,b,target}'))
                  AS targets
           FROM ds.specs WHERE name = ANY(%s) GROUP BY name""",
        (list(names),),
    )
    return {row["name"]: [t for t in row["targets"] if t is not None] for row in rows}


async def lock_pairs(conn: Conn) -> None:
    """Hold the pair lock until the transaction ends: a run of a new pair recounts under it, and
    installs serialise on it."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PAIRS_LOCK,))
