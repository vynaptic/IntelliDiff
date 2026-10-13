"""Hash-sample fixtures: specs whose frame is the scripted service's key listing, a
table of a fresh database (side a, against the service's side b), or both, and what a draw stored."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.store import repo

from .conftest import Harness, OpenHarness, ScriptedService, spec_text

KEYS = [f"K{n:03d}" for n in range(1, 9)]
PAGING = {"kind": "cursor", "itemsPath": "$.items", "cursorPath": "$.next", "cursorParam": "cursor"}
HALF = "key < 'K005'"  # K001-K004 are low, K005-K008 high
RESOLVE = {"times": 2, "delay": "0s"}
# 20 customers: C001-C010 in the retail segment, C007 and C014 flagged, and a NULL key.
CUSTOMERS = """
    CREATE TABLE customers (id text, name text, status text, segment text, flagged text,
                            changed_at timestamptz);
    INSERT INTO customers
        SELECT 'C' || lpad(g::text, 3, '0'), 'n' || g, 'A', CASE WHEN g <= 10 THEN 'R' ELSE 'B' END,
               CASE WHEN g % 7 = 0 THEN 'Y' ELSE 'N' END, now() - g * interval '1 day'
        FROM generate_series(1, 20) g;
    INSERT INTO customers VALUES (NULL, 'x', 'A', 'R', 'N', now());
"""
# A second row of C003, whose strata document sorts after its first row's: the first gives its strata.
DUPLICATED = CUSTOMERS + "INSERT INTO customers VALUES ('C003', 'n3', 'A', 'S', 'N', now());"  # noqa: S608
CUSTOMER_KEYS = [f"C{n:03d}" for n in range(1, 21)]
SIGNOFF = "riskTier == 'high' ? 'flagged=Y' : 'segment=' + strata.segment"
Tables = Callable[..., tuple[str, AbstractAsyncContextManager[Harness]]]


def bucket(label: str, size: int | None = None, **fields: Any) -> dict[str, Any]:
    return {"label": label, **({"targetSize": size} if size else {}), **fields}


def halves(low: int = 4, high: int = 4) -> list[dict[str, Any]]:
    """One dimension over the key: its low and high buckets' target sizes."""
    return [
        {"name": "half", "cel": HALF, "buckets": [bucket("low", low, values=[True]), bucket("high", high)]}
    ]


def segments(retail: int = 10, other: int = 10, flagged: int = 2) -> list[dict[str, Any]]:
    """The customers' segment and flag dimensions, with their buckets' target sizes."""
    return [
        {"name": "segment", "column": "segment",
         "buckets": [bucket("retail", retail, values=["R"]), bucket("other", other)]},
        {"name": "flagged", "column": "flagged",
         "buckets": [bucket("Y", flagged, values=["Y"], riskTier="high"), bucket("N")]},
    ]  # fmt: skip


def served_source(**fields: Any) -> dict[str, Any]:
    """Side b's listing of customers, their segment and flag as strata fields."""
    return listed_source("svc-b", strataFields={"segment": "$.segment", "flagged": "$.flagged"}) | fields


def listed_source(target: str = "svc-a", **fields: Any) -> dict[str, Any]:
    request = {"method": "GET", "path": "/list"}
    return (
        {"kind": "paginated_endpoint", "target": target, "request": request, "pagination": PAGING}
        | {"keyPath": "$.id"}
        | fields
    )


def table_source(**fields: Any) -> dict[str, Any]:
    return {"kind": "table", "side": "a", "strataColumns": ["segment", "flagged"]} | fields


def hashed(frame: dict[str, Any], strata: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    """A ``bottom_k`` hash sample of ``frame``; ``fields`` replace or add members."""
    source = {"kind": "hash_sample", "method": "bottom_k", "salt": "unit-q4", "resolve": RESOLVE}
    return source | {"strata": strata, "source": frame} | fields


def listed(
    frame: dict[str, Any] | None = None,
    *,
    strata: list[dict[str, Any]] | None = None,
    name: str = "unit-runner",
    execution: str = "",
    **fields: Any,
) -> str:
    """The unit spec (``name``) drawing a sample of the service's listing (``frame``) by ``strata``
    (``halves()``)."""
    doc = parse_yaml_text(spec_text(execution=execution))
    doc["metadata"]["name"] = name
    doc["key"]["source"] = hashed(frame or listed_source(), halves() if strata is None else strata, **fields)
    return json.dumps(doc)


def tabled(
    frame: dict[str, Any] | None = None,
    *,
    strata: list[dict[str, Any]] | None = None,
    signoff: str | None = SIGNOFF,
    execution: dict[str, Any] | None = None,
    **fields: Any,
) -> str:
    """Side a's table of customers (db-a) against the service's side b, sampled from ``frame`` (the table
    with its segment and flag) by ``strata`` (``segments()``) and ``signoff``."""
    source = hashed(frame or table_source(), strata or segments(), **fields)
    source |= {"signoffStratum": signoff} if signoff else {}
    table = {"name": "customers", "keyColumns": ["id"], "columns": ["name", "status"]}
    served = {"method": "GET", "path": "/b/{{ key }}"}
    doc = {
        "apiVersion": "deltasleuth/v1",
        "kind": "ComparisonSpec",
        "metadata": {"name": "sampled-customers", "version": "1.0.0", "owner": "tests"},
        "normalizerVersion": "1.1.0",
        "key": {"name": "id", "type": "string", "pattern": "^C[0-9]{3}$", "class": "business_code"},
        "sides": {
            "a": {"role": "legacy", "target": "db-a", "table": table},
            "b": {
                "role": "new",
                "target": "svc-b",
                "request": served,
                "select": "$",
                "notFound": {"status": [404]},
                "truncation": {"itemsPath": "$.tags", "cap": 50},
            },
        },
        "compare": {},
        "recheck": {"delay": "0s", "times": 1},
        "security": {"unlisted": "tokenize"},
    }
    doc["key"]["source"] = source
    return json.dumps(doc | ({"execution": execution} if execution else {}))


def customer(key: str) -> dict[str, Any]:
    """Side b's document of a seeded customer: side a's row's compared columns."""
    return {"name": f"n{int(key[1:])}", "status": "A"}


@pytest.fixture(name="on_tables")
def tables_fixture(
    open_harness: OpenHarness, databases: Callable[[str], str], service: ScriptedService, tmp_path: Path
) -> Tables:
    """``dsn, harness = on_tables(seed, **settings)``: a fresh database seeded with ``seed``
    (``CUSTOMERS``) as target db-a, and the scripted service as svc-a and svc-b."""
    assert service.server is not None
    served = {
        "base_url": f"http://127.0.0.1:{service.server.port}",
        "allow_private_network": True,
        "allow_insecure_http": True,
        "rate_per_s": 500,
        "capabilities": {"authoritative_absence": {"status": [404]}},
    }

    def open_(seed: str = CUSTOMERS, **settings: Any) -> tuple[str, AbstractAsyncContextManager[Harness]]:
        dsn = databases(seed)
        (tmp_path / "dsn_tables").write_text(dsn)
        targets = {"db-a": {"dsn_ref": f"file:{tmp_path / 'dsn_tables'}", "rate_per_s": 1000}}
        targets |= {"svc-a": copy.deepcopy(served), "svc-b": copy.deepcopy(served)}
        return dsn, open_harness(json.dumps({"targets": targets}), **settings)

    return open_


async def drawn(h: Harness, run_id: str, cycle: int = 1) -> repo.Row | None:
    async with h.ctx.pool.connection() as conn:
        return await repo.get_draw(conn, run_id, cycle)


async def keys(h: Harness, run_id: str, cycle: int = 1) -> list[dict[str, Any]]:
    """The cycle's keys in seq order, opened, with their labels, retries and states."""
    async with h.ctx.pool.connection() as conn:
        sealer = await h.ctx.sealer(conn, run_id)
        cur = await conn.execute(
            """SELECT seq, key_masked, key_enc, stratum, signoff_stratum, signoff_member, retries, state,
                      pass_no, available_at
               FROM ds.run_keys WHERE run_id = %s AND cycle = %s ORDER BY seq""",
            (run_id, cycle),
        )
        columns = [c.name for c in cur.description or ()]
        return [sealer.open_row(dict(zip(columns, row, strict=True))) for row in await cur.fetchall()]


async def audited(h: Harness, run_id: str, *actions: str) -> list[tuple[str, dict[str, Any]]]:
    async with h.ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT action, detail FROM ds.audit_log WHERE object = %s AND action = ANY(%s) ORDER BY id",
            (run_id, list(actions)),
        )
        return [(action, detail) for action, detail in await cur.fetchall()]


async def cycle_row(h: Harness, run_id: str, cycle: int = 1) -> repo.Row | None:
    async with h.ctx.pool.connection() as conn:
        return await repo.get_cycle(conn, run_id, cycle)


async def close(h: Harness, draw_id: str, note: str = "fixed the mapping") -> None:
    """Close a draw as a signed-in person would."""
    async with h.ctx.pool.connection() as conn:
        assert await repo.close_draws(conn, [draw_id], "user:U-0123456789abcdef", note)
