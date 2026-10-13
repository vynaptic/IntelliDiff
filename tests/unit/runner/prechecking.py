"""Table pre-checks for the tests of the runner, the API, the CLI and the reports: a spec of two table sides,
an ``Engine`` over relations held in memory that records each session and statement and fails as it is told
to, and a worker run until a pre-check has ended."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from deltasleuth.connectors.tables import Plan, Relation
from deltasleuth.connectors.tables.engine import Column, Figure
from deltasleuth.context import Context
from deltasleuth.core.types import Target
from deltasleuth.runner import run_worker
from deltasleuth.store import repo

FACTS = {"kind": "table", "key": ["id"], "server": "16.4", "consistency": "snapshot", "tls": True}
# What a side answers for a figure unless a test says otherwise: three rows, none without a key.
FIGURES = {
    "rows": "3", "null_keys": "0", "nulls": "0", "empty": "0", "shortest": "1", "longest": "5", "sum": "60",
    "min": "10", "max": "30", "true": "1", "distinct": "3",
}  # fmt: skip


def column(name: str, declared: str = "text", cls: str | None = None, **facts: Any) -> Column:
    """A readable column that may hold NULL; its class is its declared type's unless given."""
    cls = declared if cls is None else cls
    return Column(name, declared, cls, True, cls in ("text", "char"), **{"nullable": True} | facts)


def relation(name: str, *columns: Column, **facts: Any) -> Relation:
    """A base table of schema public with the primary key (id)."""
    key = column("id", "integer", nullable=False)
    return Relation("public", name, (key, *columns), FACTS | facts)


def table_spec(
    name: str = "accounts",
    *,
    a: dict[str, Any] | None = None,
    b: dict[str, Any] | None = None,
    targets: tuple[str, str] = ("db-a", "db-b"),
    version: str = "1.0.0",
    **members: Any,
) -> str:
    """A spec named ``name`` of the table ``name`` on two database targets, keyed by ``id``; ``a`` and
    ``b`` change a side's ``table``, ``members`` replace or add top-level members."""
    table = {"name": name, "keyColumns": ["id"]}
    sides = {
        side: {"role": role, "target": target, "table": table | (own or {})}
        for side, role, target, own in (("a", "legacy", targets[0], a), ("b", "new", targets[1], b))
    }
    return json.dumps(
        {
            "apiVersion": "deltasleuth/v1",
            "kind": "ComparisonSpec",
            "metadata": {"name": name, "version": version, "owner": "tests"},
            "normalizerVersion": "1.1.0",
            "key": {"name": "id", "type": "string", "class": "business_code"},
            "sides": sides,
            "compare": {},
            "recheck": {"delay": "0s", "times": 1},
            "security": {"unlisted": "tokenize"},
        }
        | members
    )


def database_targets(dsn_file: Any, *names: str) -> str:
    """A targets file of PostgreSQL targets ``names`` (db-a and db-b by default), each reading
    ``dsn_file``."""
    entry = {"dsn_ref": f"file:{dsn_file}", "rate_per_s": 1000}
    return json.dumps({"targets": dict.fromkeys(names or ("db-a", "db-b"), entry)})


@dataclass(frozen=True)
class FakeSession:
    target: str
    n: int

    async def close(self) -> None:
        pass


class FakeEngine:
    """An ``Engine`` that answers as PostgreSQL's (a pair of its sides plans as a PostgreSQL pair): the
    relations it was made of (``held``) by name, the same on every target; ``values`` maps (target, figure,
    column) to the text a side answers, ``FIGURES`` otherwise; ``failing`` maps (method, target) to the
    errors its next calls raise in order (None: that call succeeds; the last one repeats when ``always``),
    the methods being snapshot, connect, relations, describe, probe, figures, duplicates and distinct (a
    figures statement that counts distinct values). ``listed`` maps a target to the (name, kind) a schema of
    it lists, every relation as a table otherwise. ``events`` lists (event, target, detail) in order;
    ``most`` is the most sessions a target had open at once. With ``hold`` set, a figures statement waits
    until it is released or cancelled."""

    name, arms, unfit = "postgresql", 200, ()
    fold, hides, grant = staticmethod(str.lower), False, "SELECT"
    sum_digits, empty_null = None, False

    def __init__(self, *relations: Relation, pause: float = 0.0) -> None:
        self.held = {found.name: found for found in relations}
        self.values: dict[tuple[str, str, str | None], str | None] = {}
        self.failing: dict[tuple[str, str], list[BaseException | None]] = {}
        self.always: set[tuple[str, str]] = set()
        self.events: list[tuple[str, str, Any]] = []
        self.open: Counter[str] = Counter()
        self.most: Counter[str] = Counter()
        self.hold: asyncio.Event | None = None
        self.pause = pause
        self.listed: dict[str, list[tuple[str, str]]] = {}
        self._sessions = itertools.count(1)

    def fail(self, method: str, target: str, *errors: BaseException | None, always: bool = False) -> None:
        self.failing[method, target] = list(errors)
        self.always |= {(method, target)} if always else set()

    def count(self, event: str, target: str | None = None) -> int:
        return sum(found == event and target in (None, where) for found, where, _ in self.events)

    def built(self, column: str, cls: str, declared: str, key: bool) -> str | None:
        return None  # as PostgreSQL's

    def _met(self, method: str, target: str, detail: Any = None) -> None:
        self.events.append((method, target, detail))
        queued = self.failing.get((method, target))
        if queued:
            error = queued[0] if (method, target) in self.always else queued.pop(0)
            if error is not None:
                raise error

    @asynccontextmanager
    async def snapshot(self, target: Target, *, timeout_s: float = 0) -> AsyncIterator[FakeSession]:
        self._met("snapshot", target.target_id, timeout_s)
        session = FakeSession(target.target_id, next(self._sessions))
        self.open[session.target] += 1
        self.most[session.target] = max(self.most[session.target], self.open[session.target])
        try:
            yield session
        finally:
            self.open[session.target] -= 1
            self.events.append(("close", session.target, session.n))

    async def connect(self, target: Target, *, timeout_s: float = 0) -> FakeSession:
        self._met("connect", target.target_id, timeout_s)
        return FakeSession(target.target_id, next(self._sessions))

    async def relations(self, session: FakeSession, schema: str | None) -> list[tuple[str, str]]:
        self._met("relations", session.target, schema)
        return self.listed.get(session.target, [(name, "table") for name in sorted(self.held)])

    def release(self, session: FakeSession) -> str:
        return "16.4"

    async def describe(
        self, session: FakeSession, schema: str | None, name: str, *, side: Any = None
    ) -> Relation | None:
        self._met("describe", session.target, (session.n, name))
        return self.held.get(name)

    async def probe(self, session: FakeSession, side: Plan) -> None:
        self._met("probe", session.target, (session.n, side["name"]))

    async def figures(self, session: FakeSession, side: Plan, wanted: Sequence[Figure]) -> list[str | None]:
        counted = any(figure == "distinct" for figure, _ in wanted)
        self._met("distinct" if counted else "figures", session.target, (session.n, tuple(wanted)))
        await asyncio.sleep(self.pause)
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:  # where a driver sends its server the statement's cancel
                self.events.append(("cancelled", session.target, session.n))
                raise
        canonical = {own: name for name, own in side["columns"].items()}
        return [
            self.values.get((session.target, figure, canonical.get(own)), FIGURES[figure])  # type: ignore[arg-type]
            for figure, own in wanted
        ]

    async def duplicates(self, session: FakeSession, side: Plan) -> tuple[int, int]:
        self._met("duplicates", session.target, session.n)
        return 0, 0


async def made(ctx: Context, precheck_id: str) -> repo.Row:
    async with ctx.pool.connection() as conn:
        row = await repo.get_precheck(conn, precheck_id)
    assert row is not None
    return row


async def pairs(ctx: Context, precheck_id: str) -> list[repo.Row]:
    """The pre-check's pairs worst first, each with its ``result``."""
    async with ctx.pool.connection() as conn:
        return await repo.pages(repo.precheck_pairs, conn, precheck_id, results=True)


async def ended(ctx: Context, *precheck_ids: str, timeout: float = 60) -> list[repo.Row]:
    """Run a worker until each pre-check has ended; their rows."""
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(ctx, stop=stop, schedules=False))
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not all([(await made(ctx, found))["finished_at"] for found in precheck_ids]):
            assert asyncio.get_running_loop().time() < deadline, "the pre-check has not ended"
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await worker
    return [await made(ctx, found) for found in precheck_ids]
