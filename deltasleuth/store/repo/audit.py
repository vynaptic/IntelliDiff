"""The queries of the audit log and its chain."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, LiteralString

from psycopg_pool import AsyncConnectionPool

from deltasleuth.auth.principal import Principal
from deltasleuth.core import jsonio
from deltasleuth.errors import Invalid
from deltasleuth.store.repo.base import Conn, Page, Row, _after, _jsonb, _one, _page, _row, _rows, _value

Audit = Callable[..., Awaitable[Row]]

_AUDIT_COLUMNS: LiteralString = (
    "id, at, actor, actor_name, action, object, detail, request_id, session_id, client_addr"
)
# The chain entry of the audit row ``a``; null for a row the trigger never saw.
_CHAIN_ENTRY: LiteralString = """(SELECT jsonb_build_object('seq', c.seq, 'hash', c.hash)
    FROM ds.audit_chain c WHERE c.id = a.id) AS chain"""
# A chain row with its audit row's action and columns as ``audit_<column>`` (null without one), which
# chain.py compares with the stored text and object: ds.audit_text could be replaced to agree with them.
_CHAIN_COLUMNS: LiteralString = """c.seq, c.id, c.prev, c.obj_prev, c.hash, c.object, c.text, l.action,
    l.id AS audit_id, l.at AS audit_at, l.actor AS audit_actor, l.actor_name AS audit_actor_name,
    l.action AS audit_action, l.object AS audit_object, l.detail AS audit_detail,
    l.request_id AS audit_request_id, l.session_id AS audit_session_id, l.client_addr AS audit_client_addr"""

_siem = logging.getLogger("deltasleuth.audit")


async def insert_audit(
    conn: Conn, who: Principal, action: str, obj: str, detail: Mapping[str, Any] | None = None
) -> Row:
    """Append a row for ``who`` (masked detail only) and return it with its ``chain`` entry (``{seq,
    hash}``; None before 009, as 'ds keys migrate' writes rows on 006). Callers
    write audit rows through ``audited``, as the last statement of the transaction that does what the
    row records."""
    extra: dict[str, Any] = {} if who.via is None else {"via": who.via}
    if who.actor.startswith("local:"):  # asserted, not verified
        extra |= {"asserted": True, "os_account": who.os_account}
    row = await _one(
        conn,
        f"""INSERT INTO ds.audit_log (actor, actor_name, action, object, detail, request_id, session_id,
                client_addr)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_AUDIT_COLUMNS}, to_regclass('ds.audit_chain') IS NOT NULL AS chained""",  # noqa: S608
        (
            who.actor, who.name, action, obj, _jsonb(dict(detail or {}) | extra), who.request_id,
            who.session_id, who.client_addr,
        ),
    )  # fmt: skip
    if row.pop("chained"):  # its AFTER INSERT trigger chained it, after RETURNING
        row["chain"] = await _row(conn, "SELECT seq, hash FROM ds.audit_chain WHERE id = %s", (row["id"],))
    else:
        row["chain"] = None
    return row


@asynccontextmanager
async def audited(pool: AsyncConnectionPool) -> AsyncIterator[tuple[Conn, Audit]]:
    """A pool connection and ``audit(who, action, obj, detail=None)``, which inserts an audit row.

    The rows' SIEM lines go to logger ``deltasleuth.audit`` once the transaction has committed; none
    when the block raises. The audit insert is the last statement before the commit.
    """
    rows: list[Row] = []
    async with pool.connection() as conn:

        async def audit(
            who: Principal, action: str, obj: str, detail: Mapping[str, Any] | None = None
        ) -> Row:
            rows.append(await insert_audit(conn, who, action, obj, detail))
            return rows[-1]

        yield conn, audit
    for row in rows:
        _siem.info(siem_line(row))


def siem_line(row: Row) -> str:
    """An audit row as its SIEM copy: one JSON line, with its ``chain`` entry."""
    addr = row["client_addr"]
    return jsonio.dumps(
        row | {"at": row["at"].isoformat(), "client_addr": None if addr is None else str(addr)}
    )


async def list_audit(
    conn: Conn,
    *,
    actor: str | None = None,
    action: str | None = None,
    obj: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> Page:
    """Audit rows newest first, each filter exact (``since`` inclusive, ``until`` exclusive), each with
    its ``chain`` entry (``{seq, hash}``)."""
    before = _after(cursor, 1)[0]
    rows = await _rows(
        conn,
        f"""SELECT {_AUDIT_COLUMNS}, {_CHAIN_ENTRY} FROM ds.audit_log a
            WHERE (%(before)s < 0 OR id < %(before)s)
              AND (%(actor)s::text IS NULL OR actor = %(actor)s)
              AND (%(action)s::text IS NULL OR action = %(action)s)
              AND (%(obj)s::text IS NULL OR object = %(obj)s)
              AND (%(session_id)s::text IS NULL OR session_id = %(session_id)s)
              AND (%(since)s::timestamptz IS NULL OR at >= %(since)s)
              AND (%(until)s::timestamptz IS NULL OR at < %(until)s)
            ORDER BY id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {
            "before": before,
            "actor": actor,
            "action": action,
            "obj": obj,
            "session_id": session_id,
            "since": since,
            "until": until,
            "limit": limit + 1,
        },
    )
    return _page(rows, limit, "id")


async def last_audit(conn: Conn, action: str, *, obj: str | None = None) -> Row | None:
    """The newest audit row of ``action`` (of object ``obj`` only, if given)."""
    if obj is not None:  # audit_log_object_idx
        return await _row(
            conn,
            f"""SELECT {_AUDIT_COLUMNS} FROM ds.audit_log WHERE object = %s AND action = %s
                ORDER BY id DESC LIMIT 1""",  # noqa: S608 - fixed SQL fragments only
            (obj, action),
        )
    return await _row(
        conn,
        f"SELECT {_AUDIT_COLUMNS} FROM ds.audit_log WHERE action = %s ORDER BY id DESC LIMIT 1",  # noqa: S608
        (action,),
    )


async def audit_head(conn: Conn) -> Row | None:
    """The last chain row's ``{seq, hash}``; None while the chain is empty."""
    return await _row(conn, "SELECT seq, hash FROM ds.audit_chain ORDER BY seq DESC LIMIT 1")


async def audit_upgrade_seq(conn: Conn) -> int:
    """The last row 009 chained as it applied: rows up to it were written, and
    their SIEM lines logged, before the chain existed."""
    return await _value(conn, "SELECT min(upgrade_seq) FROM ds.audit_chain_upgrade", None)


async def audit_chain(
    conn: Conn,
    *,
    after: int = 0,
    limit: int = 5000,
    upto: int | None = None,
    actions: Sequence[str] | None = None,
) -> list[Row]:
    """The chain rows after row ``after`` (up to row ``upto``, of ``actions`` only, if given), in seq order
    (``_CHAIN_COLUMNS``)."""
    return await _rows(
        conn,
        f"""SELECT {_CHAIN_COLUMNS} FROM ds.audit_chain c LEFT JOIN ds.audit_log l ON l.id = c.id
            WHERE c.seq > %(after)s AND (%(upto)s::bigint IS NULL OR c.seq <= %(upto)s)
              AND (%(actions)s::text[] IS NULL OR l.action = ANY(%(actions)s))
            ORDER BY c.seq LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"after": after, "upto": upto, "actions": None if actions is None else list(actions), "limit": limit},
    )


async def audit_segment(conn: Conn, objects: Sequence[str], head: int) -> list[Row]:
    """A pack's audit segment: the chain rows of ``objects``, each object's from
    its first, up to row ``head``, in seq order (``_CHAIN_COLUMNS``)."""
    return await _rows(
        conn,
        f"""SELECT {_CHAIN_COLUMNS} FROM ds.audit_chain c LEFT JOIN ds.audit_log l ON l.id = c.id
            WHERE c.object = ANY(%s) AND c.seq <= %s ORDER BY c.seq""",  # noqa: S608 - fixed SQL fragments only
        (list(objects), head),
    )


async def audit_rows(conn: Conn, ids: Sequence[int]) -> list[Row]:
    """The audit rows with these ids, each with its ``chain`` entry."""
    return await _rows(
        conn,
        f"SELECT {_AUDIT_COLUMNS}, {_CHAIN_ENTRY} FROM ds.audit_log a WHERE id = ANY(%s) ORDER BY id",  # noqa: S608
        (list(ids),),
    )


async def unchained_audit_row(conn: Conn) -> Row | None:
    """The first audit row without a chain entry: one written with the chain trigger disabled, or whose
    entry was deleted."""
    return await _row(
        conn,
        """SELECT id, action, object FROM ds.audit_log l
           WHERE NOT EXISTS (SELECT 1 FROM ds.audit_chain c WHERE c.id = l.id) ORDER BY id LIMIT 1""",
    )


async def audit_anchor(conn: Conn, seq: int) -> Row:
    """The chain's last row's ``seq`` and ``hash`` (None while the chain is empty) and the hash of row
    ``seq`` as ``anchored`` (None without that row)."""
    return await _one(
        conn,
        """SELECT h.seq, h.hash, (SELECT c.hash FROM ds.audit_chain c WHERE c.seq = %s) AS anchored
           FROM (SELECT 1) AS one
           LEFT JOIN LATERAL (SELECT seq, hash FROM ds.audit_chain ORDER BY seq DESC LIMIT 1) AS h ON true""",
        (seq,),
    )


async def audit_guard(conn: Conn) -> Row:
    """What lets the connected role switch the audit log's triggers off, each a boolean: ``superuser``;
    ``owner``, of schema ds or of a table or function in it (or a member of its owner), which may disable
    a trigger or replace its function; ``trigger``, the privilege to add a trigger to the audit log or the
    chain; ``replica``, the right to set session_replication_role, under which triggers do not fire (a
    superuser's alone before PostgreSQL 15, which made it a grant)."""
    granted = await _value(conn, "SELECT current_setting('server_version_num')::int >= 150000", None)
    replica: LiteralString = (
        "has_parameter_privilege('session_replication_role', 'SET')" if granted else "r.rolsuper"
    )
    return await _one(
        conn,
        f"""SELECT r.rolsuper AS superuser,
                  coalesce(pg_has_role(n.nspowner, 'MEMBER')
                           OR EXISTS (SELECT 1 FROM pg_class c
                                      WHERE c.relnamespace = n.oid AND pg_has_role(c.relowner, 'MEMBER'))
                           OR EXISTS (SELECT 1 FROM pg_proc p
                                      WHERE p.pronamespace = n.oid AND pg_has_role(p.proowner, 'MEMBER')),
                           false) AS owner,
                  has_table_privilege('ds.audit_log', 'TRIGGER')
                      OR has_table_privilege('ds.audit_chain', 'TRIGGER') AS trigger,
                  {replica} AS replica
           FROM pg_roles r LEFT JOIN pg_namespace n ON n.nspname = 'ds'
           WHERE r.rolname = current_user""",  # noqa: S608 - fixed SQL fragments only
    )


_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# The application role's grants (``ds db grants``), for psql: ``{role}`` is a name ``_ROLE`` matches.
_GRANTS = """\
-- DeltaSleuth's application role {role}: rights on the data of schema ds and nothing more. It owns
-- nothing, may add no trigger and may not set session_replication_role, so it cannot switch the audit
-- log's triggers off. Run this as a superuser or the owner of schema ds, after 'ds migrate', in one
-- transaction (psql -v ON_ERROR_STOP=1 -1); the default privileges cover the tables later migrations add.
-- A new role is made without a password: set one apart, with \\password {role} in psql.
DO $$
DECLARE v_owner text := (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'ds');
BEGIN
    IF v_owner IS NULL THEN
        RAISE EXCEPTION 'schema ds does not exist: run ds migrate first';
    ELSIF v_owner = '{role}' THEN
        RAISE EXCEPTION 'role {role} owns schema ds: name another role for the application';
    ELSIF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}' AND rolsuper) THEN
        RAISE EXCEPTION 'role {role} is a superuser: name another role for the application';
    ELSIF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE "{role}" LOGIN;
    END IF;
    IF NOT (has_database_privilege('{role}', current_database(), 'CONNECT')
            AND has_database_privilege('{role}', current_database(), 'TEMPORARY')) THEN
        EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO "{role}"', current_database());
    END IF;
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA ds'
                   ' GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}"', v_owner);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA ds'
                   ' GRANT USAGE, SELECT ON SEQUENCES TO "{role}"', v_owner);
END $$;
GRANT USAGE ON SCHEMA ds TO "{role}";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ds TO "{role}";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ds TO "{role}";
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ds.schema_version FROM "{role}";
REVOKE UPDATE, DELETE, TRUNCATE ON ds.audit_log, ds.audit_chain, ds.audit_chain_upgrade FROM "{role}";
"""


def grants(role: str) -> str:
    """The SQL that gives the application's role ``role`` data rights on schema ds and nothing more, for
    the schema's owner to run; Invalid for a name that is not a plain lower-case one."""
    if not _ROLE.fullmatch(role):
        raise Invalid("--role must be 1 to 63 of a-z, 0-9 and _, starting with a letter or _")
    return _GRANTS.replace("{role}", role)
