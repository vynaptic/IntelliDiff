"""Bulk graph parity, L2: which members each group links, and how the
components that the differing groups touch split or merge.

Both sides stream one row per group (its size and its member set's hash) in one order, so comparing
every group takes O(1) memory. Only the groups that differ are read in full, and only the components
their members belong to are explored, each up to ``maxComponent`` members. A larger group or
component is reported as oversized: counted, never classified, never dropped. Everything written
is masked with the key's class.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Iterable, Sequence
from contextlib import AsyncExitStack
from typing import Any

import psycopg

from deltasleuth.connectors import postgres as pg
from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.context import Context
from deltasleuth.core.types import CompiledSpec, Side
from deltasleuth.security.tokenise import mask_key

BATCH = 500  # groups or members a lookup reads at once
FETCH = 10_000  # group rows a stream fetches at once
SAMPLES = 5  # masked members a row shows
Row = dict[str, Any]
# A cell component's code by (several a-components, several b-components); one-to-one is no difference.
_CODES = {(True, True): "CLUSTER_REARRANGE", (False, True): "CLUSTER_SPLIT", (True, False): "CLUSTER_MERGE"}
Facts = Callable[[str, str, Iterable[str]], Awaitable[list[tuple[str, str]]]]  # (side, by, values) -> (g, m)


class _Forest:
    """A union-find; ``weight`` counts the member nodes of each tree."""

    def __init__(self) -> None:
        self._parent: dict[Hashable, Hashable] = {}
        self._weight: dict[Hashable, int] = {}

    def root(self, node: Hashable) -> Hashable:
        """The node's root (adding the node), halving the path: no recursion, however long the chain."""
        self._parent.setdefault(node, node)
        while (parent := self._parent[node]) != node:
            self._parent[node] = node = self._parent[parent]
        return node

    def join(self, x: Hashable, y: Hashable) -> None:
        rx, ry = self.root(x), self.root(y)
        if rx != ry:
            if self._weight.get(rx, 0) < self._weight.get(ry, 0):  # keep trees shallow
                rx, ry = ry, rx
            self._parent[ry] = rx
            self._weight[rx] = self._weight.get(rx, 0) + self._weight.pop(ry, 0)

    def weight(self, node: Hashable) -> int:
        """Members in the node's tree; 0 for a node not reached yet, which it does not add."""
        return self._weight.get(self.root(node), 0) if node in self._parent else 0

    def add_member(self, member: str) -> bool:
        """Count ``member`` in its tree once; False when it was already there."""
        if ("m", member) in self._parent:
            return False
        self._weight[self.root(("m", member))] = 1
        return True

    def members(self) -> list[str]:
        return [node[1] for node in self._parent if node[0] == "m"]  # type: ignore[index]


async def analyse(ctx: Context, spec: CompiledSpec, tables: pg.Plan, state: Row) -> tuple[Row, list[Row]]:
    """The cycle's L2 summary (``ds.scans.state.graph``) and its ``ds.graph_diffs`` rows; the scan's
    ``state["graph_at"]`` is when L2's own snapshots were taken."""
    graph, parts = spec.raw["graph"], list(spec.key.parts)
    members, groups = [parts.index(p) for p in graph["members"]], [parts.index(p) for p in graph["groups"]]
    cap = graph.get("maxComponent", 10_000)

    def mask(text: str) -> str:
        return mask_key(text, spec, ctx.tokeniser)

    async with AsyncExitStack() as stack:  # one read-only snapshot a side for the whole analysis
        conns = {
            s: await stack.enter_async_context(pg.snapshot(ctx.targets.live(spec.sides[Side(s)].target)))
            for s in "ab"
        }
        state["graph_at"] = utc_now_iso()

        async def lookup(side: str, by: str, batch: list[str]) -> list[tuple[str, str]]:
            statement, params = pg.facts_sql(tables[side], members, groups, by, batch)
            try:
                async with conns[side].transaction():  # a savepoint: a refused batch keeps the snapshot
                    return await (await conns[side].execute(statement, params)).fetchall()
            except pg.UNFIT:  # another side's text this column's type or the encoding cannot hold
                if len(batch) == 1:
                    return []  # so no row of this side holds it
                half = len(batch) // 2
                return await lookup(side, by, batch[:half]) + await lookup(side, by, batch[half:])

        async def facts(side: str, by: str, values: Iterable[str]) -> list[tuple[str, str]]:
            ordered = sorted(values)
            found: list[tuple[str, str]] = []
            for i in range(0, len(ordered), BATCH):
                found += await lookup(side, by, ordered[i : i + BATCH])
            return found

        summary, differing, hot = await _group_sets(conns, tables, members, groups, cap)
        rows, seeds = await _group_rows(differing, hot, facts, mask)
        too_big = hot["a"] | hot["b"]  # read by its sizes only on both sides, so expanded on neither
        explored = {s: await _explore(s, seeds, too_big, facts, cap) for s in "ab"}
    clusters, counts = _contingency(explored, mask)
    return summary | counts, rows + clusters


async def _stream(
    conn: psycopg.AsyncConnection, statement: Any, params: Row
) -> AsyncIterator[tuple[Any, ...]]:
    cursor = conn.cursor("ds_graph")
    await cursor.execute(statement, params)
    while batch := await cursor.fetchmany(FETCH):
        for row in batch:
            yield row
    await cursor.close()


async def _group_sets(
    conns: dict[str, psycopg.AsyncConnection],
    tables: pg.Plan,
    members: Sequence[int],
    groups: Sequence[int],
    cap: int,
) -> tuple[Row, list[tuple[str, int, int]], dict[str, set[str]]]:
    """Every group compared by its size and member set's hash: the counts, the groups that differ
    (with their sizes), and each side's groups of more than ``cap`` members."""
    streams = {s: _stream(conns[s], *pg.group_sets_sql(tables[s], members, groups)) for s in "ab"}
    heads = {s: await anext(streams[s], None) for s in "ab"}
    summary = {"groups_a": 0, "groups_b": 0, "groups_equal": 0, "groups_different": 0}
    differing, hot = [], {"a": set(), "b": set()}
    while any(heads.values()):
        group = min(h[0] for h in heads.values() if h)
        rows = {s: h[1:] if h and h[0] == group else None for s, h in heads.items()}
        for s, row in rows.items():
            if row is not None:
                summary[f"groups_{s}"] += 1
                heads[s] = await anext(streams[s], None)
                if row[0] > cap:
                    hot[s].add(group)
        if rows["a"] == rows["b"]:
            summary["groups_equal"] += 1
        else:  # another member set, or a group of one side only
            summary["groups_different"] += 1
            differing.append((group, *(row[0] if row else 0 for row in rows.values())))
    return summary, differing, hot


async def _group_rows(
    differing: list[tuple[str, int, int]], hot: dict[str, set[str]], facts: Facts, mask: Callable[[str], str]
) -> tuple[list[Row], set[str]]:
    """A row for each group that differs, and the members of those read in full (the seeds). A
    group of more than ``maxComponent`` members is shown by its sizes only."""
    too_big = hot["a"] | hot["b"]
    full = [g for g, _, _ in differing if g not in too_big]
    sets: dict[str, dict[str, set[str]]] = {s: defaultdict(set) for s in "ab"}
    for s in "ab":
        for group, member in await facts(s, "g", full):
            sets[s][group].add(member)
    rows, seeds = [], set()
    for group, count_a, count_b in differing:
        if group in too_big:
            rows.append(
                _row("GROUP_MEMBERS_DIFFER", mask(group), count_a, count_b, detail={"oversized": True})
            )
            continue
        a, b = sets["a"][group], sets["b"][group]
        seeds |= a | b
        detail = {
            f"only_{s}": [mask(m) for m in sorted(x)[:SAMPLES]] for s, x in (("a", a - b), ("b", b - a))
        }
        rows.append(_row("GROUP_MEMBERS_DIFFER", mask(group), len(a), len(b), len(a - b), len(b - a), detail))
    return rows, seeds


async def _explore(
    side: str, seeds: set[str], hot: set[str], facts: Facts, cap: int
) -> tuple[_Forest, set[Hashable]]:
    """The side's components around ``seeds``: members lead to their groups, groups to their members,
    until each component is closed or holds more than ``cap`` members (or a hot group). Returns the
    forest, holding every member reached, and the roots of the oversized components."""
    forest, seen_groups, reached = _Forest(), set(), set()
    frontier = set(seeds)
    while frontier:
        reached |= frontier
        groups = set()
        for group, member in await facts(side, "m", [m for m in frontier if forest.weight(("m", m)) <= cap]):
            forest.add_member(member)
            forest.join(("m", member), ("g", group))
            groups.add(group)
        new_groups = groups - seen_groups
        seen_groups |= new_groups
        expand = [g for g in new_groups if g not in hot and forest.weight(("g", g)) <= cap]
        frontier = set()
        for group, member in await facts(side, "g", expand):
            if forest.add_member(member):
                frontier.add(member)
            forest.join(("m", member), ("g", group))
        frontier -= reached
    oversized = {forest.root(("g", g)) for g in seen_groups & hot}
    oversized |= {forest.root(("m", m)) for m in reached if forest.weight(("m", m)) > cap}
    return forest, oversized


def _contingency(
    explored: dict[str, tuple[_Forest, set[Hashable]]], mask: Callable[[str], str]
) -> tuple[list[Row], Row]:
    """Rows for each oversized component and each cell component that splits, merges or both; the
    counts of each."""
    rows: list[Row] = []
    for s, (forest, oversized) in explored.items():
        by_root: dict[Hashable, list[str]] = defaultdict(list)
        for member in forest.members():
            by_root[forest.root(("m", member))].append(member)
        for root in sorted(oversized, key=lambda r: min(by_root[r])):
            size = forest.weight(root)
            label = mask(min(by_root[root]))
            rows.append(
                _row("CLUSTER_OVERSIZED", label, *((size, 0) if s == "a" else (0, size)), detail={"side": s})
            )
    clusters, counts = _clusters(explored, mask)
    return rows + clusters, counts | {"oversized": sum(len(big) for _, big in explored.values())}


def _clusters(
    explored: dict[str, tuple[_Forest, set[Hashable]]], mask: Callable[[str], str]
) -> tuple[list[Row], Row]:
    """The cell components: a-components and b-components joined by the members
    they share, oversized ones left out, each coded by how many of each it holds."""
    (a, big_a), (b, big_b) = explored["a"], explored["b"]
    cells, pairs = _Forest(), []
    for member in sorted(set(a.members()) & set(b.members())):
        old, new = ("a", a.root(("m", member))), ("b", b.root(("m", member)))
        if old[1] not in big_a and new[1] not in big_b:
            cells.join(old, new)
            pairs.append((old, new, member))
    components: dict[Hashable, tuple[set[Hashable], set[Hashable], list[str]]] = defaultdict(
        lambda: (set(), set(), [])
    )
    for old, new, member in pairs:
        olds, news, found = components[cells.root(old)]
        olds.add(old)
        news.add(new)
        found.append(member)
    counts = {"split": 0, "merge": 0, "rearrange": 0}
    rows = []
    for olds, news, found in components.values():
        # Connected, several a-components mean some b-component fans in (a merge), and several
        # b-components that some a-component fans out (a split).
        if (chosen := _CODES.get((len(olds) > 1, len(news) > 1))) is not None:
            counts[chosen.removeprefix("CLUSTER_").lower()] += 1
            detail = {"members": len(found), "sample": [mask(m) for m in found[:SAMPLES]]}
            rows.append(_row(chosen, mask(found[0]), len(olds), len(news), detail=detail))
    return rows, counts


def _row(
    code: str,
    label: str,
    count_a: int,
    count_b: int,
    only_a: int = 0,
    only_b: int = 0,
    detail: Row | None = None,
) -> Row:
    return {
        "code": code,
        "label": label,
        "count_a": count_a,
        "count_b": count_b,
        "only_a": only_a,
        "only_b": only_b,
        "detail": detail or {},
    }
