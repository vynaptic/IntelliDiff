"""Migration 018: the scopes an approval may hold take ``transforms``; approvals stored before it stay."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from deltasleuth import approvals
from deltasleuth.store import repo
from deltasleuth.store.db import migrate
from tests.unit.store.four_eyes import approved, person, spec, upload

pytestmark = pytest.mark.db

TRANSFORMS = 18
EARLIER = ["clear", "unlisted_clear", "known_diffs", "population_filters", "sampling"]


async def _all(conn: repo.Conn, sql: str, params: Any = ()) -> list[tuple]:
    return await (await conn.execute(sql, params)).fetchall()  # type: ignore[arg-type]


async def test_018_over_a_017_store(empty_pool: AsyncConnectionPool) -> None:
    assert await migrate(empty_pool, upto=TRANSFORMS - 1) == list(range(1, TRANSFORMS))
    async with empty_pool.connection() as conn:
        amy, bob = await person(conn, "amy", "spec_author"), await person(conn, "bob")
        spec_id = await upload(conn, spec(), amy)
        approval_id = await approved(conn, spec_id, amy, bob, scopes=EARLIER)
        mapped = await upload(conn, spec("1.1.0"), amy)
    async with empty_pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="approvals_scopes_check"):
            await repo.create_approval(conn, "spec", mapped, amy.actor, scopes=["transforms"])
    assert await migrate(empty_pool, upto=TRANSFORMS) == [TRANSFORMS]
    async with empty_pool.connection() as conn:
        [(kept,)] = await _all(conn, "SELECT scopes FROM ds.approvals WHERE approval_id = %s", (approval_id,))
        assert await repo.create_approval(conn, "spec", mapped, amy.actor, scopes=["transforms"])
    assert kept == EARLIER
    async with empty_pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="approvals_scopes_check"):
            await repo.create_approval(conn, "spec", spec_id, amy.actor, scopes=["bogus"])


async def test_every_scope_a_version_may_need_is_storable(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        amy, bob = await person(conn, "amy", "spec_author"), await person(conn, "bob")
        spec_id = await upload(conn, spec(), amy)
        approval_id = await approved(conn, spec_id, amy, bob, scopes=approvals.SCOPES)
        stored = await _all(conn, "SELECT scopes FROM ds.approvals WHERE approval_id = %s", (approval_id,))
    assert stored == [(list(approvals.SCOPES),)]
