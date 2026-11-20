"""The queries of evidence packs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, LiteralString

from deltasleuth.store.repo.base import (
    Conn,
    Page,
    Row,
    _jsonb,
    _one,
    _page,
    _row,
    _rows,
    _sortable_id,
    _value,
)

_PACK_COLUMNS: LiteralString = """p.pack_id, p.run_id, p.cycle, p.signoff_id, p.adjudication_id,
    p.sums_sha256, p.zip_sha256, p.key_fingerprint, p.audit_seq, p.issued_by,
    ds.actor_name(p.issued_by) AS issued_by_name, p.issued_at, p.status, p.revoked_by,
    ds.actor_name(p.revoked_by) AS revoked_by_name, p.revoked_at, p.revoke_reason, p.purged_at"""


def new_pack_id() -> str:
    """A ``P-`` id: a pack's id comes before its row, as its files name it."""
    return _sortable_id("P-")


async def signed_off(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The ended cycle's approved sign-off, if any."""
    return await _row(
        conn,
        """SELECT * FROM ds.approvals
           WHERE kind = 'signoff' AND run_id = %s AND cycle = %s AND status = 'approved'""",
        (run_id, cycle),
    )


async def signoff_for_pack(conn: Conn, approval_id: str) -> Row | None:
    """The sign-off's status and attests hash, read FOR SHARE: a revoke waits for this transaction, or
    this read waits for the revoke and finds it revoked."""
    return await _row(
        conn,
        "SELECT status, attests_sha256 FROM ds.approvals WHERE approval_id = %s FOR SHARE",
        (approval_id,),
    )


async def count_results(conn: Conn, run_id: str, cycle: int) -> int:
    """How many per-key results the cycle holds."""
    return await _value(
        conn, "SELECT count(*) FROM ds.results WHERE run_id = %s AND cycle = %s", (run_id, cycle)
    )


async def last_census(conn: Conn, run_id: str, cycle: int) -> Row | None:
    """The newest ended cycle before ``cycle`` with a census: its ``cycle`` and ``census``."""
    return await _row(
        conn,
        """SELECT cycle, census FROM ds.cycles WHERE run_id = %s AND cycle < %s AND census IS NOT NULL
           ORDER BY cycle DESC LIMIT 1""",
        (run_id, cycle),
    )


async def spec_lineage(conn: Conn, spec_id: int) -> list[int]:
    """The versions of the spec's name after its newest approved version before it, up to it (the lineage
    whose uploads ds.spec_accountable counts), oldest first."""
    rows = await _rows(
        conn,
        """WITH this AS (SELECT spec_id, name FROM ds.specs WHERE spec_id = %(spec_id)s)
           SELECT s.spec_id FROM ds.specs s JOIN this ON s.name = this.name
           WHERE s.spec_id <= this.spec_id AND s.spec_id > coalesce((
               SELECT max(v.spec_id) FROM ds.specs v
               WHERE v.name = this.name AND v.spec_id < this.spec_id AND EXISTS (
                   SELECT 1 FROM ds.approvals a WHERE a.spec_id = v.spec_id AND a.kind = 'spec'
                                                  AND a.status = 'approved')), 0)
           ORDER BY s.spec_id""",
        {"spec_id": spec_id},
    )
    return [row["spec_id"] for row in rows]


async def issuers(conn: Conn, actors: Sequence[str]) -> dict[str, str]:
    """The IdP that signed in each ``user:`` actor (ds.users.issuer)."""
    rows = await _rows(
        conn,
        "SELECT 'user:' || user_id AS actor, issuer FROM ds.users WHERE 'user:' || user_id = ANY(%s)",
        (list(actors),),
    )
    return {row["actor"]: row["issuer"] for row in rows}


async def insert_pack(conn: Conn, pack: Mapping[str, Any], zip_bytes: bytes) -> Row:
    """An issued pack (``pack``: its ``ds.packs`` columns but the status ones) and its zip; the trigger
    refuses it unless its sign-off is approved, which it reads FOR SHARE."""
    columns = ("pack_id", "run_id", "cycle", "signoff_id", "adjudication_id", "manifest", "sums_sha256",
               "zip_sha256", "key_fingerprint", "audit_seq", "issued_by", "issued_at")  # fmt: skip
    values = [_jsonb(pack[c]) if c == "manifest" else pack[c] for c in columns]
    row = await _one(
        conn,
        """INSERT INTO ds.packs (pack_id, run_id, cycle, signoff_id, adjudication_id, manifest, sums_sha256,
               zip_sha256, key_fingerprint, audit_seq, issued_by, issued_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        values,
    )
    await conn.execute(
        "INSERT INTO ds.pack_zips (pack_id, zip) VALUES (%s, %s)", (pack["pack_id"], zip_bytes)
    )
    return row


async def get_pack(conn: Conn, pack_id: str) -> Row | None:
    """The pack with its manifest and its actors' names."""
    return await _row(
        conn,
        f"SELECT {_PACK_COLUMNS}, p.manifest FROM ds.packs p WHERE p.pack_id = %s",  # noqa: S608
        (pack_id,),
    )


async def list_packs(
    conn: Conn,
    *,
    run_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """Packs newest first, without their manifests."""
    rows = await _rows(
        conn,
        f"""SELECT {_PACK_COLUMNS} FROM ds.packs p
            WHERE (%(run_id)s::text IS NULL OR p.run_id = %(run_id)s)
              AND (%(status)s::text IS NULL OR p.status = %(status)s)
              AND (%(after)s::text IS NULL OR p.pack_id < %(after)s)
            ORDER BY p.pack_id DESC LIMIT %(limit)s""",  # noqa: S608 - fixed SQL fragments only
        {"run_id": run_id, "status": status, "after": cursor, "limit": limit + 1},
    )
    return _page(rows, limit, "pack_id")


async def cycle_packs(conn: Conn, run_id: str, cycle: int) -> list[Row]:
    """The ended cycle's packs, oldest first, without their manifests."""
    return await _rows(
        conn,
        f"""SELECT {_PACK_COLUMNS} FROM ds.packs p WHERE p.run_id = %s AND p.cycle = %s
            ORDER BY p.pack_id""",  # noqa: S608 - fixed SQL fragments only
        (run_id, cycle),
    )


async def pack_zip(conn: Conn, pack_id: str) -> bytes | None:
    """The pack's stored zip; None once its cycle's purge deleted it."""
    return await _value(conn, "SELECT zip FROM ds.pack_zips WHERE pack_id = %s", (pack_id,))


async def issued_packs(conn: Conn, signoff_id: str) -> list[str]:
    """The issued (not revoked) packs citing the sign-off."""
    rows = await _rows(
        conn,
        "SELECT pack_id FROM ds.packs WHERE signoff_id = %s AND status = 'issued' ORDER BY pack_id",
        (signoff_id,),
    )
    return [row["pack_id"] for row in rows]


async def revoke_packs(conn: Conn, signoff_ids: Sequence[str]) -> list[Row]:
    """The packs of the sign-offs, revoked with them: ds.tg_signoff_packs revokes a sign-off's packs as it
    leaves approved, by any writer, copying who, when and why; each ``pack_id``, ``signoff_id`` and
    ``revoke_reason``, in order."""
    return await _rows(
        conn,
        """SELECT pack_id, signoff_id, revoke_reason FROM ds.packs
           WHERE signoff_id = ANY(%s) AND status = 'revoked' ORDER BY pack_id""",
        (list(signoff_ids),),
    )


async def revoked_packs(conn: Conn) -> list[Row]:
    """Every revoked pack of every key, in the order they were revoked: what the revocation list lists."""
    return await _rows(
        conn,
        """SELECT pack_id, sums_sha256, revoked_at, key_fingerprint FROM ds.packs
           WHERE status = 'revoked' ORDER BY revoked_at, pack_id""",
    )


async def applied_at(conn: Conn, version: int) -> datetime | None:
    """When migration ``version`` was applied; None before."""
    return await _value(conn, "SELECT applied_at FROM ds.schema_version WHERE version = %s", (version,))
