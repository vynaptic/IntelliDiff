"""The table scan (docs/mvp/TABLES.md §5): the key source of a batch or soak run (§11) over two tables.

Each side streams (key texts, row hash) in code-point order from one read-only snapshot of its engine
(docs/mvp/ENGINES.md §5), and a side whose keys come out of that order fails the run (§8). A merge
counts the identical rows and queues every other key for the worker's per-key reads while it goes
on. It runs synchronously over each fetched batch and awaits only to fetch the next: a task per row
costs 16x. Progress is checkpointed with the keys found, so a restart, a pause or a lost connection
resumes after the last saved key and counts nothing twice. The keys found and the point the scan
resumes after are sealed under the run's data key (docs/mvp/RETENTION.md §3.1); checkpoints are
numbered, so a scanner saves only on the checkpoint it read. Each pass reads both catalogues on its own
sessions before a cursor opens (the schema guard's V2, docs/mvp/PRECHECKS.md §7): a table that is no longer
what the run's plan reads pauses the run there, and the pass ends without failing it (``drift.Tripped``).
A pass with a file side takes the file reader's one sort before either snapshot opens (docs/mvp/FILES.md
§10), so no database's snapshot sits idle while the pass waits for another run's sort.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.connectors.tables import (
    Engine,
    KeysOutOfOrder,
    Plan,
    Relation,
    RowsMoved,
    Session,
    duck,
    encode_key,
    planned,
    unsealed,
)
from deltasleuth.context import Context
from deltasleuth.core.types import Side, Target
from deltasleuth.runner import drift, graph
from deltasleuth.runner.stop import STOP_PREFIX, StopConditions
from deltasleuth.security.crypto import Sealer
from deltasleuth.security.tokenise import mask_key
from deltasleuth.store import repo

BATCH = 10_000  # merged keys per checkpoint
MAX_KEY_BYTES = 1024  # a run key
Key = tuple[str, ...]


def new_scan() -> dict[str, Any]:
    """A scan's state before it starts (``ds.scans``): ``after`` is the last saved key's parts, sealed
    (None before the first checkpoint and once done), ``checkpoint`` the number of checkpoints saved,
    ``snapshots_a`` and ``_b`` how many snapshots of each side its saved rows were read in, between
    ``first_at`` and ``last_at`` (a resumed scan opens fresh ones: docs/mvp/PACK.md §1.2). A side whose
    plan names its engine also gets ``server_a`` or ``_b``, the server version its last pass read
    (docs/mvp/ENGINES.md §13)."""
    counters = (
        "rows_a", "rows_b", "skipped_a", "skipped_b", "matched", "candidates", "checkpoint", "snapshots_a",
        "snapshots_b",
    )  # fmt: skip
    return {"done": False, "after": None, "first_at": None, "last_at": None} | dict.fromkeys(counters, 0)


def seal_after(sealer: Sealer, cycle: int, parts: list[str]) -> dict[str, str]:
    """The point a scan resumes after, as ``state["after"]`` stores it: the key's parts, sealed."""
    sealed = sealer.seal("scan_after", cycle, "", json.dumps(parts).encode())
    return {"enc": base64.b64encode(sealed).decode("ascii")}


def _opened_after(sealer: Sealer, cycle: int, after: dict[str, str] | None) -> list[str] | None:
    if after is None:
        return None
    return json.loads(sealer.open("scan_after", cycle, "", base64.b64decode(after["enc"])))


class _Stream:
    """One side's rows in fetched batches: the head row's key, and the digests of one key's rows at once.

    A row's digest is its engine's (``connectors.tables.render.digest``): the database's hash of its plain
    columns, extended with the columns the worker converts (TABLES.md §10, ENGINES.md §10). A row without
    one (a value that does not convert, a row left in its database) comes with a marker instead, which
    its side completes, so the other side never has it.
    """

    def __init__(
        self,
        side: str,
        plan: Plan,
        engine: Engine,
        session: Session,
        after: Key | None,
        relation: Relation | None,
    ) -> None:
        self._side, self._plan, self._engine, self.session = side, plan, engine, session
        self.relation = relation  # as the session's catalogue holds it (None: gone)
        self._batches: AsyncIterator[list[tuple[Any, ...]]]
        self._parts, self._marked = len(plan["keys"]), side.encode()
        self._rows: list[tuple[Any, ...]] = []
        self._i, self._eof = 0, False
        self._head: Key | None = None  # the key of row ``_i``, once it passed the guard
        self._floor, self._bound = after, True  # the key before it: the resume bound, then each row's

    async def open(self) -> None:
        """Send the scan's statement, which pins the side: its batches are fetched from then on."""
        bound = None if self._floor is None else [part.encode() for part in self._floor]
        self._batches = await self._engine.scan(self.session, self._plan, bound)

    async def head(self) -> Key | None:
        """The key of the next row, None at the end. A key below the one before it, or a first key not
        after the resume bound, raises ``KeysOutOfOrder`` (ENGINES.md §8): whatever the engine's
        collation did, a mis-sorted side fails the run and never mis-pairs a key."""
        if self._head is None and not self._eof:
            while self._i == len(self._rows):
                fetched = await anext(self._batches, None)
                if fetched is None:
                    self._eof = True
                    return None
                self._rows, self._i = fetched, 0
            key = self._rows[self._i][: self._parts]
            if self._floor is not None and (key < self._floor or (self._bound and key == self._floor)):
                raise KeysOutOfOrder(f"side {self._side}: the population's keys came out of order")
            self._head = self._floor = key
            self._bound = False
        return self._head

    async def take(self, key: Key) -> list[bytes]:
        """The digests of the rows of ``key`` (none when the head is another key). Both sides sort as
        the merge compares, by the keys' UTF-8 bytes (``scan_sql``), so a key never comes back."""
        digests = []
        while await self.head() == key:
            digest = self._rows[self._i][self._parts]
            digests.append(digest if len(digest) == 32 else digest + self._marked)
            self._i, self._head = self._i + 1, None
        return digests

    async def null_keys(self) -> int:
        """Rows of the population whose key is NULL or unmatched by its key map, in the same snapshot."""
        return await self._engine.null_keys(self.session, self._plan)

    def server(self) -> str:
        """The major and minor version of the server this pass reads."""
        return self._engine.release(self.session)


@asynccontextmanager
async def _snapshot(side: str, target: Target, plan: Plan, after: Key | None) -> AsyncIterator[_Stream]:
    """A side's snapshot, its catalogue read (V2) and its cursor not yet open."""
    engine = planned(side, plan, target)
    async with engine.snapshot(target) as session:
        relation = await engine.describe(session, plan["schema"], plan["name"], side=plan)
        yield _Stream(side, plan, engine, session, after, relation)


async def scan(ctx: Context, run_id: str) -> str | None:
    """Scan the run's tables from its last checkpoint to the end, or until the run stops running;
    the stop condition (``until``, ``maxDuration``) that ends the run, judged at each checkpoint. A file side
    (docs/mvp/FILES.md §7, §9, §11) counts the keys several of its rows hold (``repeated_x``), must have read
    exactly the rows its plan counted once read to its end (``RowsMoved``), and records when its files'
    closing hash agreed (``verified_x``).

    Raises what stops it: ``connectors.tables.UNREACHABLE`` when a database is unreachable (the worker
    resumes the scan later, as it does after some of an engine's codes: ``runner.worker``),
    ``drift.Tripped`` when the schema guard paused the run before a cursor opened, anything else fails the
    run.
    """
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run["scan"] is None or run["scan"]["done"]:
            return None  # finished by the scanner the sweep saw running when it read the run
        spec = await ctx.spec(conn, run["spec_id"], live=True)
        sealer = await ctx.sealer(conn, run_id)
    tables, state, cycle = unsealed(sealer, run["params"]), dict(run["scan"]), run["cycle"]
    conditions = StopConditions.from_spec(spec.raw)
    async with ctx.pool.connection() as conn:  # read again even when identical: they did not match
        unmatched = await repo.unmatched_keys(conn, run_id, cycle - 1) if cycle > 1 else []
    before = {sealer.open_row(row)["key"] for row in unmatched}
    saved = state["checkpoint"]  # the checkpoint this scanner builds on
    state["after"] = _opened_after(sealer, cycle, state["after"])
    after = None if state["after"] is None else tuple(state["after"])
    found: dict[str, str] = {}  # keys since the last checkpoint: raw -> masked
    async with AsyncExitStack() as stack:
        if any("pinned" in tables[s.value] for s in Side):
            # The one sort of the file reader (FILES.md §10), before a database side's snapshot opens: none
            # sits idle while this pass waits for another's sort.
            await stack.enter_async_context(duck.SORT.held())
        a, b = [
            await stack.enter_async_context(
                _snapshot(s.value, ctx.targets.live(spec.sides[s].target), tables[s.value], after)
            )
            for s in Side
        ]
        # When the snapshots opened, not when a file side's sort returned; counted with the rows next saved:
        # a lost pass read none of them.
        opened = utc_now_iso()
        seen = {"a": a.relation, "b": b.relation}  # V2: before either cursor opens, every cycle and resume
        if await drift.check(ctx, run, spec, seen, "scan" if after is None else "resume"):
            raise drift.Tripped(run_id)
        await a.open()
        await b.open()
        state["first_at"], state["last_at"] = state.get("first_at") or opened, opened
        for s, stream in zip(Side, (a, b), strict=True):
            state[f"snapshots_{s.value}"] = state.get(f"snapshots_{s.value}", 0) + 1
            if "pinned" in tables[s.value]:  # a file side's count: 0 says it scanned and met none
                state.setdefault(f"repeated_{s.value}", 0)
            if "engine" in tables[s.value]:  # a PostgreSQL pair's state is what it was
                state[f"server_{s.value}"] = stream.server()
        merged = 0
        while True:
            heads = [h for h in (await a.head(), await b.head()) if h is not None]
            if not heads:
                break
            key = min(heads)
            rows = {"a": await a.take(key), "b": await b.take(key)}
            text = encode_key(key)
            for side, hashes in rows.items():
                state[f"rows_{side}"] += len(hashes)
                if len(hashes) > 1 and "pinned" in tables[side]:  # a file guarantees no uniqueness (FILES §9)
                    state[f"repeated_{side}"] += 1
            if not text or len(text.encode()) > MAX_KEY_BYTES:  # no run key can hold it
                for side, hashes in rows.items():
                    state[f"skipped_{side}"] += len(hashes)
            elif len(rows["a"]) == len(rows["b"]) == 1 and rows["a"] == rows["b"] and text not in before:
                state["matched"] += 1
            else:  # changed, one-sided, or repeated on a side
                found[text] = mask_key(text, spec, ctx.tokeniser)
            state["after"], merged = list(key), merged + 1
            if conditions.max_keys is not None and state["candidates"] + len(found) >= conditions.max_keys:
                stop = STOP_PREFIX + "maxKeys"
                await _save(ctx, sealer, run_id, cycle, state, found, saved, done=True, stop_reason=stop)
                return None
            if merged % BATCH == 0:
                if not await _save(ctx, sealer, run_id, cycle, state, found, saved):
                    return None
                saved += 1
                if reason := conditions.expired(run["started_at"], datetime.now(UTC)):
                    return reason
        for side, stream in (("a", a), ("b", b)):  # rows_x counts every row read, skipped_x among them
            nulls = await stream.null_keys()
            state[f"rows_{side}"] += nulls
            state[f"skipped_{side}"] += nulls
            if "selected" in tables[side] and state[f"rows_{side}"] != tables[side]["selected"]:
                read, counted = state[f"rows_{side}"], tables[side]["selected"]
                raise RowsMoved(f"side {side}: {read:,} rows were read where the plan counted {counted:,}")
            if verified := getattr(stream.session, "verified_at", None):  # a fixed side's closing hash
                state[f"verified_{side}"] = verified
    if spec.raw.get("graph"):  # L2: the structure the facts make (GRAPH.md §3), before the scan is done
        state["graph"], rows = await graph.analyse(ctx, spec, tables, state)
        async with ctx.pool.connection() as conn:
            await repo.replace_graph_diffs(conn, run_id, cycle, rows)
    await _save(ctx, sealer, run_id, cycle, state, found, saved, done=True)
    return None


async def _save(
    ctx: Context,
    sealer: Sealer,
    run_id: str,
    cycle: int,
    state: dict[str, Any],
    found: dict[str, str],
    saved: int,
    *,
    done: bool = False,
    stop_reason: str | None = None,
) -> bool:
    """Checkpoint the state with the keys found since checkpoint ``saved``, both sealed (a done scan
    resumes nowhere: its ``after`` is None), and a maxKeys ``stop_reason`` with the run's ``keys_cut``;
    False: the run stopped running, or another scanner saved since (a worker lock handover), so this one
    stops."""
    state["candidates"] += len(found)
    state["done"] = done
    stored = state | {"after": None if done else seal_after(sealer, cycle, state["after"])}
    async with ctx.pool.connection() as conn:
        ok = await repo.save_scan(conn, run_id, cycle, stored, sealer.seal_keys(found), saved)
        if ok and stop_reason:  # maxKeys: the run goes on, its keys found so far are still read
            await repo.set_run_status(conn, run_id, "running", when=("running",), stop_reason=stop_reason)
            await repo.set_keys_cut(conn, run_id, {"taken": state["candidates"]})
    found.clear()
    return ok
