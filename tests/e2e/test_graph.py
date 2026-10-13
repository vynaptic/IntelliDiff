"""Bulk graph parity (docs/mvp/GRAPH.md): two link-fact tables, whose L1 scan finds the missing and
extra links and whose L2 names the groups that changed members and the components that split,
merge or rearrange, a hot group's component reported as oversized.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import pytest

from deltasleuth.runner.runs import create_run
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.e2e.conftest import Stack, both, finish, store_spec, table_spec

pytestmark = pytest.mark.e2e

TABLE = """
    CREATE TABLE links (entity_code text, id_type text, id_value text,
                        PRIMARY KEY (entity_code, id_type, id_value));
"""
HOT_A = ", ".join(f"('h{i}', 'phone', 'p0')" for i in range(1, 31))
HOT_B = ", ".join(f"('h{i}', 'phone', 'p0')" for i in range(1, 30))  # h30 leaves the hot group
# e3 joins p1 (a merge), e5 leaves p4 (a split), p6 and p8 swap e7 and e8 (a rearrange), e41 joins
# x40, whose e40 is also in the hot group p0 (an oversized component), and p50 stays as it is.
FACTS_A = f"""
    INSERT INTO links VALUES ('e1', 'phone', 'p1'), ('e2', 'phone', 'p1'), ('e1', 'email', 'x1'),
        ('e3', 'email', 'x3'), ('e4', 'phone', 'p4'), ('e5', 'phone', 'p4'), ('e5', 'email', 'x5'),
        ('e6', 'phone', 'p6'), ('e7', 'phone', 'p6'), ('e8', 'phone', 'p8'), ('e9', 'phone', 'p8'),
        {HOT_A}, ('e40', 'phone', 'p0'), ('e40', 'email', 'x40'),
        ('e50', 'phone', 'p50'), ('e51', 'phone', 'p50');
"""  # noqa: S608 - constants
FACTS_B = f"""
    INSERT INTO links VALUES ('e1', 'phone', 'p1'), ('e2', 'phone', 'p1'), ('e3', 'phone', 'p1'),
        ('e1', 'email', 'x1'), ('e3', 'email', 'x3'), ('e4', 'phone', 'p4'), ('e5', 'email', 'x5'),
        ('e6', 'phone', 'p6'), ('e8', 'phone', 'p6'), ('e7', 'phone', 'p8'), ('e9', 'phone', 'p8'),
        {HOT_B}, ('e40', 'phone', 'p0'), ('e40', 'email', 'x40'), ('e41', 'email', 'x40'),
        ('e50', 'phone', 'p50'), ('e51', 'phone', 'p50');
"""  # noqa: S608 - constants


def graph_spec(**overrides: Any) -> dict[str, Any]:
    table = {
        "name": "links",
        "keyColumns": ["entity_code", "id_type", "id_value"],
        "columns": ["entity_code"],
    }
    return table_spec(
        key={
            "name": "link",
            "type": "composite",
            "parts": ["entity_code", "id_type", "id_value"],
            "class": "identifier",
        },
        sides=both(table),
        graph={"members": ["entity_code"], "groups": ["id_type", "id_value"], "maxComponent": 20},
        security={"unlisted": "tokenize"},
        **overrides,
    )


async def test_graph_parity_names_the_groups_and_components_that_changed(
    tables: Callable[..., Stack],
) -> None:
    async with tables(TABLE + FACTS_A, TABLE + FACTS_B).context() as ctx:
        run = await finish(
            ctx, await create_run(ctx, await store_spec(ctx, graph_spec()), "batch", who=TESTER)
        )
        async with ctx.pool.connection() as conn:
            rows = await repo.pages(repo.list_graph_diffs, conn, run["run_id"], 1)
    assert run["status"] == "completed"
    outcomes = run["counters"]["by_outcome"]
    assert (outcomes["NOT_FOUND_A"], outcomes["NOT_FOUND_B"]) == (4, 4)  # L1: the links that moved
    assert run["scan"]["graph"] == {
        "groups_a": 10, "groups_b": 10, "groups_equal": 4, "groups_different": 6,
        "split": 1, "merge": 1, "rearrange": 1, "oversized": 2,
    }  # fmt: skip
    scan = run["scan"]  # L2 took its own snapshots, after L1's (docs/mvp/PACK.md §1.2)
    assert (scan["snapshots_a"], scan["snapshots_b"]) == (1, 1) and scan["first_at"] <= scan["graph_at"]
    codes = sorted(r["code"] for r in rows)
    assert codes == sorted(
        ["GROUP_MEMBERS_DIFFER"] * 6
        + ["CLUSTER_MERGE", "CLUSTER_SPLIT", "CLUSTER_REARRANGE"]
        + ["CLUSTER_OVERSIZED"] * 2
    )
    by_code = {r["code"]: r for r in rows}
    assert (by_code["CLUSTER_MERGE"]["count_a"], by_code["CLUSTER_MERGE"]["count_b"]) == (2, 1)
    assert (by_code["CLUSTER_SPLIT"]["count_a"], by_code["CLUSTER_SPLIT"]["count_b"]) == (1, 2)
    assert (by_code["CLUSTER_REARRANGE"]["count_a"], by_code["CLUSTER_REARRANGE"]["count_b"]) == (2, 2)
    hot = [r for r in rows if r["detail"].get("oversized")]
    assert [(r["count_a"], r["count_b"]) for r in hot] == [(31, 30)]  # sizes only: never read in full
    assert all(r["label"].startswith("tok:") for r in rows)  # identifier keys are personal data
    shown = str(rows)
    assert not any(f"'{v}'" in shown for v in ("e1", "e3", "p1", "x40", "h30"))


async def test_a_kill_during_l2_resumes_to_the_same_rows_and_counts(
    tables: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    from deltasleuth.runner import graph
    from deltasleuth.runner import scan as scanning

    monkeypatch.setattr(scanning, "BATCH", 5)  # the merge checkpoints before L2 starts
    real, calls = graph.analyse, []

    async def dying(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:  # the second run's scanner loses a database during L2
            raise psycopg.OperationalError("killed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(graph, "analyse", dying)
    async with tables(TABLE + FACTS_A, TABLE + FACTS_B, lease_seconds=3).context() as ctx:
        spec_id = await store_spec(ctx, graph_spec())
        runs = [await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER)) for _ in range(2)]
        async with ctx.pool.connection() as conn:
            rows = [await repo.pages(repo.list_graph_diffs, conn, run["run_id"], 1) for run in runs]
    clean, killed = runs
    assert (len(calls), killed["status"]) == (3, "completed")
    assert rows[1] == rows[0]
    when = ("snapshots_a", "snapshots_b", "first_at", "last_at", "graph_at")  # docs/mvp/PACK.md §1.2

    def counted(scan: dict[str, Any]) -> dict[str, Any]:
        return {name: value for name, value in scan.items() if name not in when}

    assert counted(killed["scan"]) == counted(clean["scan"])  # the graph counts too, no fact counted twice
    assert (killed["scan"]["snapshots_a"], clean["scan"]["snapshots_a"]) == (2, 1)  # resumed: read again
    assert killed["counters"]["by_outcome"] == clean["counters"]["by_outcome"]


# e2 exists on side b only and links to p1: its link is an L1 finding, p1's members differ, and no
# component splits or merges, since only members found on both sides form cells.
NEW_MEMBER_A = "INSERT INTO links VALUES ('e1', 'phone', 'p1'), ('e1', 'email', 'x1');"
NEW_MEMBER_B = "INSERT INTO links VALUES ('e1', 'phone', 'p1'), ('e1', 'email', 'x1'), ('e2', 'phone', 'p1');"
# One component of four members on both sides, whose links change: m4 moves from g3 to g2.
MOVED_A = """
    INSERT INTO links VALUES ('m1', 'id', 'g1'), ('m2', 'id', 'g1'), ('m2', 'id', 'g2'), ('m3', 'id', 'g2'),
                             ('m3', 'id', 'g3'), ('m4', 'id', 'g3');
"""
MOVED_B = """
    INSERT INTO links VALUES ('m1', 'id', 'g1'), ('m2', 'id', 'g1'), ('m2', 'id', 'g2'), ('m3', 'id', 'g2'),
                             ('m3', 'id', 'g3'), ('m4', 'id', 'g2');
"""


@pytest.mark.parametrize(
    ("seed_a", "seed_b", "max_component", "structure"),
    [
        (NEW_MEMBER_A, NEW_MEMBER_B, 20, {"split": 0, "merge": 0, "rearrange": 0, "oversized": 0}),
        (MOVED_A, MOVED_B, 3, {"split": 0, "merge": 0, "rearrange": 0, "oversized": 2}),  # 4 > 3 a side
    ],
    ids=["a member on one side only", "a component of more members than maxComponent"],
)
async def test_components_count_the_members_each_side_holds(
    tables: Callable[..., Stack], seed_a: str, seed_b: str, max_component: int, structure: dict[str, int]
) -> None:
    doc = graph_spec()
    doc["graph"]["maxComponent"] = max_component
    async with tables(TABLE + seed_a, TABLE + seed_b).context() as ctx:
        run = await finish(ctx, await create_run(ctx, await store_spec(ctx, doc), "batch", who=TESTER))
        async with ctx.pool.connection() as conn:
            rows = await repo.pages(repo.list_graph_diffs, conn, run["run_id"], 1)
    counts = run["scan"]["graph"]
    assert {name: counts[name] for name in structure} == structure
    assert (
        sorted(r["code"] for r in rows if r["code"].startswith("CLUSTER_"))
        == ["CLUSTER_OVERSIZED"] * structure["oversized"]
    )


# X9 is a member side b's integer column cannot hold.
TEXT_MEMBERS = "INSERT INTO links VALUES ('1', 'phone', 'p1'), ('X9', 'phone', 'p1'), ('2', 'phone', 'p2');"
INTEGER_MEMBERS = "INSERT INTO links VALUES (1, 'phone', 'p1'), (2, 'phone', 'p2');"


async def test_a_member_the_other_side_cannot_hold_is_on_one_side_only(tables: Callable[..., Stack]) -> None:
    text, integer = (
        TABLE + TEXT_MEMBERS,
        TABLE.replace("entity_code text", "entity_code integer") + INTEGER_MEMBERS,
    )
    async with tables(text, integer).context() as ctx:
        run = await finish(
            ctx, await create_run(ctx, await store_spec(ctx, graph_spec()), "batch", who=TESTER)
        )
    assert run["status"] == "completed"  # L2's lookup of X9 on side b holds no row, never fails the run
    assert run["scan"]["graph"] == {
        "groups_a": 2, "groups_b": 2, "groups_equal": 1, "groups_different": 1,
        "split": 0, "merge": 0, "rearrange": 0, "oversized": 0,
    }  # fmt: skip


@pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")
async def test_a_million_facts_a_side_finish_in_two_minutes(tables: Callable[..., Stack]) -> None:
    # 500,000 entities, each with its own phone and an email shared by pairs: 1M facts a side. Side b
    # moves 100 entities to the next pair's email, and each move rearranges two pairs.
    entities = 500_000
    seed = f"""
        {TABLE}
        INSERT INTO links SELECT 'e' || g, 'phone', 'p' || g FROM generate_series(1, {entities}) g;
        INSERT INTO links SELECT 'e' || g, 'email', 'x' || g / 2 FROM generate_series(1, {entities}) g;
    """  # noqa: S608 (entities is a constant)
    moves = """
        UPDATE links SET id_value = 'x' || substr(entity_code, 2)::int / 2 + 1
        WHERE id_type = 'email' AND substr(entity_code, 2)::int % 5000 = 2500;
    """
    async with tables(seed, seed + moves).context() as ctx:
        run_id = await create_run(ctx, await store_spec(ctx, graph_spec()), "batch", who=TESTER)
        started = time.monotonic()
        run = await finish(ctx, run_id, timeout=300)
        elapsed = time.monotonic() - started
    print(f"\n{2 * entities} facts a side: {elapsed:.1f} s")
    outcomes = run["counters"]["by_outcome"]
    assert (outcomes["NOT_FOUND_A"], outcomes["NOT_FOUND_B"]) == (100, 100)
    graph = run["scan"]["graph"]
    assert (graph["groups_different"], graph["rearrange"], graph["split"], graph["merge"]) == (200, 100, 0, 0)
    assert elapsed < 120
