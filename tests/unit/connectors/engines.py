"""Stand-ins for tests of the engine seam (docs/mvp/ENGINES.md §5) that need no database: an engine over
relations held in memory, and a connection that records what PostgreSQL's adapter sends on it."""

from __future__ import annotations

import json
from typing import Any

from psycopg import sql

from deltasleuth.connectors.tables import KeyUnfit, Plan, Relation
from deltasleuth.connectors.tables.engine import Asked, Column
from deltasleuth.core.types import Target

FACTS = {"kind": "table", "server": "8.4", "consistency": "snapshot", "tls": True, "charset": "utf8mb4"}


class FakeEngine:
    """An ``Engine`` named ``fake``: ``relations`` maps a relation's name to its columns in schema public,
    each a name (readable text) or a ``Column``; ``facts`` maps one to what its server says otherwise than
    ``FACTS``; ``served`` is what a session says of its server; ``failing`` maps a method's name to the
    errors its next calls raise, in order (None: that call succeeds). A key holding ``unfit`` is one its
    column cannot hold; every other key has one selected row."""

    name, arms, unfit, retry_alone = "fake", 200, (KeyUnfit,), True
    fold, hides, grant = staticmethod(str.lower), False, "SELECT"

    def __init__(
        self, relations: dict[str, list[str | Column]] | None = None, **failing: list[BaseException | None]
    ) -> None:
        self.relations, self.failing = relations or {}, failing
        self.facts: dict[str, dict[str, Any]] = {}
        self.served = {"version": "8.4.11", "charset": "utf8mb4", "tls": True}
        self.opened = self.closed = 0  # sessions
        self.asked: list[list[str]] = []  # the keys of each per-key statement
        self.probed: list[Plan] = []

    def _fail(self, method: str) -> None:
        if self.failing.get(method) and (error := self.failing[method].pop(0)) is not None:
            raise error

    async def connect(self, target: Target, *, timeout_s: float = 0) -> FakeEngine:
        self._fail("connect")
        self.opened += 1
        return self

    async def close(self) -> None:  # the engine is its own session
        self.closed += 1

    async def server(self, session: Any) -> dict[str, Any]:
        self._fail("server")
        return self.served

    async def describe(
        self, session: Any, schema: str | None, name: str, *, side: Any = None
    ) -> Relation | None:
        self._fail("describe")
        if name not in self.relations:
            return None
        columns = tuple(
            Column(column, "text", "text", True, True) if isinstance(column, str) else column
            for column in self.relations[name]
        )
        return Relation("public", name, columns, FACTS | self.facts.get(name, {}))

    async def probe(self, session: Any, side: Plan) -> None:
        self._fail("probe")
        self.probed.append(side)

    async def rows(self, session: Any, side: Plan, asked: Asked) -> list[tuple[int, str, bool]]:
        self.asked.append(list(asked))
        self._fail("rows")
        if any("unfit" in key for key in asked):
            raise KeyUnfit("MySQL 1366")
        return [(i, json.dumps({"key": key}), True) for i, key in enumerate(asked)]


class Recorder:
    """A connection that records each statement sent on it as [cursor name, text, parameters] and answers
    with no row."""

    def __init__(self, cursor: str | None = None, sent: list[list[Any]] | None = None) -> None:
        self._cursor, self.sent = cursor, [] if sent is None else sent

    def cursor(self, name: str) -> Recorder:
        return Recorder(name, self.sent)

    async def execute(self, statement: Any, params: Any = None) -> Recorder:
        text = statement.as_string(None) if isinstance(statement, sql.Composable) else statement
        self.sent.append([self._cursor, text, json.loads(json.dumps(params, default=bytes.decode))])
        return self

    async def fetchmany(self, size: int) -> list[Any]:
        return []

    async def fetchall(self) -> list[Any]:
        return []

    async def fetchone(self) -> tuple[int]:
        return (0,)
