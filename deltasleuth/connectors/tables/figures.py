"""What a table pre-check reads of one side (docs/mvp/PRECHECKS.md §5), inside one snapshot of its engine:
the figures ``core.precheck`` asks for, a few columns a statement, then the keys several rows hold and
the distinct counts; and the closed code a failed read is recorded by.

Each statement aggregates: no value of a row is selected, and what comes back is counts, lengths and
rendered sums, minima and maxima, which stay in the worker's memory. A statement is bounded by the
timeout its session was opened with (``Engine.snapshot(target, timeout_s=...)``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from typing import Any

import psycopg

from deltasleuth.connectors.tables.engine import (
    TIMEOUT,
    UNREACHABLE,
    Engine,
    Figure,
    Relation,
    Session,
    SnapshotLost,
    TableError,
    TargetAuthRefused,
    TargetDenied,
    TargetTempSpace,
    code,
)
from deltasleuth.connectors.tables.keys import Plan

COLUMNS = 150  # columns whose figures one statement reads
DISTINCT = 4  # columns whose distinct values one statement counts: each of them sorts or hashes


def classed(side: Plan, relation: Relation) -> Plan:
    """The side with the class and the declared type of each column of its relation, which the plan of a
    pair of PostgreSQL tables does not store: a figure is written for its column's class."""
    if "classes" in side:
        return side
    columns = relation.columns
    return side | {
        "classes": {c.name: c.cls for c in columns},
        "declared": {c.name: c.declared for c in columns},
    }


def failure(exc: BaseException, timeout_s: float, elapsed_s: float = 0) -> tuple[str, str]:
    """A failed read as a pre-check records it (§5): its closed code, and what a line may show beside it,
    an engine's code or an error's class, never a driver's text; for a timeout, the seconds the statement
    was given. A MySQL statement the server interrupted (1317) once it had run ``elapsed_s`` past its
    timeout timed out too: the server's limit may end it so."""
    if isinstance(exc, TIMEOUT) or (code(exc) == "MySQL 1317" and 0 < timeout_s <= elapsed_s):
        return "timeout", f"{timeout_s:g}"
    if isinstance(exc, TargetAuthRefused):
        return "auth_refused", code(exc)
    if isinstance(exc, psycopg.errors.ConfigurationLimitExceeded):  # temp_file_limit
        return "temp_space", exc.sqlstate or code(exc)
    for cls, found in (
        (TargetTempSpace, "temp_space"),
        (SnapshotLost, "snapshot_lost"),
        (TargetDenied, "denied"),
    ):
        if isinstance(exc, cls):
            return found, code(exc)
    if isinstance(exc, UNREACHABLE):
        return "unreachable", code(exc)
    return "error", f"{type(exc).__name__}: {exc}" if isinstance(exc, TableError) else type(exc).__name__


async def read(
    engine: Engine,
    session: Session,
    side: Plan,
    wanted: Sequence[Figure],
    *,
    duplicates: bool,
    distinct_max_rows: int,
    timeout_s: float,
) -> dict[str, Any]:
    """What the session reads of the side, a plan side with its ``classes``: ``wanted``'s figures, each
    with the canonical name of its column (None: of the rows). ``rows`` and ``null_keys`` are numbers;
    a column's ``figures`` are its rendered texts by canonical name and figure; with ``duplicates``,
    ``duplicate_keys`` and ``rows_in_duplicates``. The figures come ``COLUMNS`` columns a statement, the
    distinct counts ``DISTINCT``; those and the duplicate keys are skipped (``skipped``) when the side
    holds over ``distinct_max_rows`` rows. A read that fails ends the side's: ``failed`` maps its check
    (figures, duplicates, distinct) to ``failure``, and what was read before it stays."""
    found: dict[str, Any] = {"figures": {}, "failed": {}, "skipped": []}
    counted = [w for w in wanted if w[0] == "distinct"]
    check, started = "figures", time.monotonic()
    try:
        for batch in _batches([w for w in wanted if w[0] != "distinct"], COLUMNS):
            started = time.monotonic()
            _keep(found, batch, await engine.figures(session, side, _columns(side, batch)))
        if (duplicates or counted) and found.get("rows", 0) > distinct_max_rows:
            found["skipped"].append("distinct_rows")
            return found
        if duplicates:
            check, started = "duplicates", time.monotonic()
            found["duplicate_keys"], found["rows_in_duplicates"] = await engine.duplicates(session, side)
        check = "distinct"
        for batch in _batches(counted, DISTINCT):
            started = time.monotonic()
            _keep(found, batch, await engine.figures(session, side, _columns(side, batch)))
    except Exception as exc:
        found["failed"][check] = failure(exc, timeout_s, time.monotonic() - started)
    return found


def _batches(wanted: Sequence[Figure], size: int) -> Iterator[list[Figure]]:
    """``wanted`` in statements of at most ``size`` columns; the figures of the rows go with the first."""
    columns = list(dict.fromkeys(column for _, column in wanted if column is not None))
    for start in range(0, max(len(columns), bool(wanted)), size):
        chunk = set(columns[start : start + size])
        yield [w for w in wanted if w[1] in chunk or (w[1] is None and start == 0)]


def _columns(side: Plan, batch: Sequence[Figure]) -> list[Figure]:
    """The batch with each canonical name as the side's column."""
    return [(figure, name if name is None else side["columns"][name]) for figure, name in batch]


def _keep(found: dict[str, Any], batch: Sequence[Figure], texts: Sequence[str | None]) -> None:
    for (figure, name), text in zip(batch, texts, strict=True):
        if name is None:
            found[figure] = int(text or 0)
        else:
            found["figures"].setdefault(name, {})[figure] = text
