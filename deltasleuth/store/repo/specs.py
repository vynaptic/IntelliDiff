"""The queries of specs."""

from __future__ import annotations

from deltasleuth.core.types import CompiledSpec
from deltasleuth.store.repo.base import Conn, Row, _jsonb, _row, _rows, _value


async def save_spec(conn: Conn, spec: CompiledSpec, *, created_by: str) -> int:
    """Store a compiled spec once per content hash; return its spec_id (the existing one if known).
    ``created_by`` is kept from the first upload."""
    return await _value(
        conn,
        """INSERT INTO ds.specs (name, version, content_hash, body, created_by) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (content_hash) DO UPDATE SET content_hash = EXCLUDED.content_hash
           RETURNING spec_id""",
        (spec.name, spec.version, spec.content_hash, _jsonb(spec.raw), created_by),
    )


async def get_spec(conn: Conn, spec_id: int) -> Row | None:
    return await _row(conn, "SELECT * FROM ds.specs WHERE spec_id = %s", (spec_id,))


async def spec_by_hash(conn: Conn, content_hash: str) -> Row | None:
    """The stored version of that content, if any: a stored version is not saved again."""
    return await _row(conn, "SELECT * FROM ds.specs WHERE content_hash = %s", (content_hash,))


async def list_specs(conn: Conn) -> list[Row]:
    """Newest first, without bodies, each with ``approval``: its newest spec approval request
    (``approval_id``, ``status``, ``scopes``), or None."""
    return await _rows(
        conn,
        """SELECT s.spec_id, s.name, s.version, s.content_hash, s.created_at,
                  (SELECT jsonb_build_object('approval_id', a.approval_id, 'status', a.status,
                                             'scopes', a.scopes)
                   FROM ds.approvals a WHERE a.spec_id = s.spec_id AND a.kind = 'spec'
                   ORDER BY a.approval_id DESC LIMIT 1) AS approval
           FROM ds.specs s ORDER BY s.spec_id DESC""",
    )
