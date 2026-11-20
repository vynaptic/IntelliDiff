"""The table scan: the key source of a batch or soak run over two tables.

Each side streams (key texts, row hash) in key order from one read-only snapshot of its engine, and a side
whose keys come out of that order fails the run. Key order is the code-point order of the key texts, but a
key part the run's plan orders as an integer (``integers``: a whole-number column on both sides, read as
itself) orders by its value, so each side reads it through its index and resumes by range; the merge, its
guard on each side's order and the resume bound compare keys by that rank. A merge counts the identical
rows and queues every other key for the worker's per-key reads while it goes on. It runs synchronously
over each fetched batch and awaits only for the next: a task per row costs 16x. Each side fetches its next
batch ahead, while the merge reads the one before and while the other side fetches its own. Progress is
checkpointed with the keys found, so a restart, a pause or a lost connection resumes after the last saved
key and counts nothing twice. The keys found and the point the scan resumes after are sealed under the
run's data key; checkpoints are numbered, so a scanner saves only on the checkpoint it read. Each pass reads
both catalogues on its own sessions before a cursor opens (the schema guard's look): a table that is no
longer what the run's plan reads pauses the run there, and the pass ends without failing it
(``drift.Tripped``); a key column the plan orders as an integer that its engine no longer orders so fails
the run (``KeyOrderChanged``), whose saved bound is of that order. A pass with a file side takes the file
reader's one sort before either snapshot opens, so no database's snapshot sits idle while the pass waits for
another run's sort.

A candidate is due as soon as its checkpoint commits. Its per-key reads keep the documents and apply the
spec's recheck policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from datetime import UTC, datetime
from typing import Any

import psycopg

from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.connectors.tables import (
    Engine,
    KeyOrderChanged,
    KeysOutOfOrder,
    Plan,
    Relation,
    RowsMoved,
    Session,
    TableError,
    duck,
    encode_key,
    planned,
    unsealed,
)
from deltasleuth.connectors.tables.connector import scan_slots
from deltasleuth.context import Context
from deltasleuth.core.types import CompiledSpec, Side, Target
from deltasleuth.runner import drift, graph
from deltasleuth.runner.stop import STOP_PREFIX, StopConditions
from deltasleuth.security.crypto import MaskedAlike, Sealer, seal_after
from deltasleuth.security.crypto import opened_after as _opened_after
from deltasleuth.security.tokenise import key_form, mask_key
from deltasleuth.store import repo

BATCH = 10_000  # merged keys per checkpoint
SLICE = 1_000  # merged keys between two turns of the event loop: about 3 ms of the merge
MAX_KEY_BYTES = 1024  # a run key
Key = tuple[str, ...]
Rank = tuple[int | str, ...]  # what keys merge by: a key's texts, an integer part's value in its place
_ALIKE = "(they differ only in Unicode normal form): no result could tell them apart"


class KeysMaskAlike(TableError):
    """Keys the scan found share a masked key: one masked key holds one key's row and result, so the run
    fails, counting the keys and naming none, and the checkpoint that met them saves nothing."""


def new_scan() -> dict[str, Any]:
    """A scan's state before it starts (``ds.scans``): ``after`` is the last saved key's parts, sealed
    (None before the first checkpoint and once done), ``checkpoint`` the number of checkpoints saved,
    ``snapshots_a`` and ``_b`` how many snapshots of each side its saved rows were read in, between
    ``first_at`` and ``last_at`` (a resumed scan opens fresh ones). A side whose
    plan names its engine also gets ``server_a`` or ``_b``, the server version its last pass read."""
    counters = (
        "rows_a", "rows_b", "skipped_a", "skipped_b", "matched", "candidates", "checkpoint", "snapshots_a",
        "snapshots_b",
    )  # fmt: skip
    return {"done": False, "after": None, "first_at": None, "last_at": None} | dict.fromkeys(counters, 0)


def _whole(text: str) -> int:
    """The value of a key part ordered as an integer; ValueError for a text that is not a whole number
    written once, which would rank alike with another text."""
    value = int(text)
    if str(value) != text:
        raise ValueError(text)
    return value


def _ranking(parts: int, integers: Sequence[int]) -> Callable[[Key], Rank] | None:
    """What a side's keys merge by: each part's text, but the value of a part the plan orders as an
    integer; None where every part is a text, so the key is its own rank."""
    if not integers:
        return None
    if parts == 1:
        return lambda key: (_whole(key[0]),)
    wanted = set(integers)
    return lambda key: tuple(_whole(part) if i in wanted else part for i, part in enumerate(key))


class _Stream:
    """One side's fetched rows: the head key, a group's count and its sole digest when it has one row.

    A row's digest is its engine's (``connectors.tables.render.digest``): the database's hash of its plain
    columns, extended with the columns the worker converts. A row without
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
        self.rank: Rank = ()  # the head's rank (``_ranking``)
        self._ranked = _ranking(self._parts, plan.get("integers") or ())
        self._after = after  # the resume bound's texts
        # The rank of the key before the head: the resume bound's, then each row's.
        self._floor, self._bound = None if after is None else self._rank(after), True
        self.check: Callable[[], Awaitable[None]] | None = None
        self.group_rows = 0
        self.serial: asyncio.Lock | None = None  # both cursors share a single target slot
        self._ahead: asyncio.Task[list[tuple[Any, ...]] | None] | None = None  # the next batch, being fetched

    def _rank(self, key: Key) -> Rank:
        if self._ranked is None:
            return key
        try:
            return self._ranked(key)
        except ValueError:
            not_one = "a key part the plan orders as an integer is not one"
            raise KeysOutOfOrder(f"side {self._side}: {not_one}") from None

    def confirm(self) -> None:
        """Fail the pass where this pass's catalogue says a key column the plan orders as an integer is of a
        type its engine does not order so (``Engine.integral``): the scan's saved bound is of that order,
        and no pass reads in another. A column the catalogue no longer holds is the schema guard's, or its
        statement's, to refuse."""
        if self.relation is None:
            return
        columns = {column.name: column for column in self.relation.columns}
        for i in self._plan.get("integers") or ():
            name = self._plan["keys"][i]
            column = columns.get(name)
            if column is not None and not self._engine.integral(column.cls or "", column.declared):
                raise KeyOrderChanged(
                    f"side {self._side}: key column {name!r} is {column.declared} now, which this run's scan"
                    " cannot order as the integer it was planned as: start a new run, which plans the table"
                    " as it is now"
                )

    async def open(self) -> None:
        """Send the scan's statement, which pins the side, and begin fetching its first batch. The engine is
        told the server's character set as this pass's catalogue says it, by which a PostgreSQL side in
        UTF-8 orders and resumes by its key texts as they are (``queries.scan_sql``), and each column's
        character set and collation where that catalogue names them (``seen_collations``), by which a MySQL
        side whose key columns its plan recorded in a collation that orders them as their key texts orders
        and resumes by them (``mysql.scan_sql``); the run's plan is not changed."""
        bound = None if self._after is None else [part.encode() for part in self._after]
        said: dict[str, Any] = {}
        if self.relation is not None:
            seen = {c.name: list(c.collation) for c in self.relation.columns if c.collation}
            said = {"charset": self.relation.facts.get("charset"), "seen_collations": seen}
        async with self.serial or nullcontext():
            self._batches = await self._engine.scan(self.session, self._plan | said, bound)
        self._ahead = asyncio.create_task(self._fetch())
        await asyncio.sleep(0)  # sent before the other side's statement

    async def _fetch(self) -> list[tuple[Any, ...]] | None:
        async with self.serial or nullcontext():
            return await anext(self._batches, None)

    async def settle(self) -> None:
        """End the fetch begun ahead, if one is: a pass that stops before its end closes its session with no
        statement of the side in flight. What that fetch read, or failed with, the pass no longer needs."""
        ahead, self._ahead = self._ahead, None
        if ahead is not None:
            ahead.cancel()
            await asyncio.wait([ahead])
            if not ahead.cancelled():
                ahead.exception()  # retrieved: not logged as never retrieved

    async def head(self) -> Key | None:
        """The key of the next row, None at the end, its rank as ``rank``. A key ranked below the one before
        it, or a first key not ranked after the resume bound, raises ``KeysOutOfOrder``: whatever the engine's
        collation did, a mis-sorted side fails the run and never mis-pairs a key."""
        if self._head is None and not self._eof:
            while self._i == len(self._rows):
                if self.check is not None:
                    await self.check()
                fetched = await (self._ahead or asyncio.create_task(self._fetch()))
                if fetched is None:
                    self._ahead, self._eof = None, True
                    return None
                self._ahead = asyncio.create_task(self._fetch())  # while the merge reads this one
                await asyncio.sleep(0)  # its first step sends it: the merge yields nothing until the next
                self._rows, self._i = fetched, 0
            key = self._rows[self._i][: self._parts]
            rank = key if self._ranked is None else self._rank(key)
            if self._floor is not None and (rank < self._floor or (self._bound and rank == self._floor)):
                raise KeysOutOfOrder(f"side {self._side}: the population's keys came out of order")
            self._head, self._floor, self.rank = key, rank, rank
            self._bound = False
        return self._head

    async def take(self, key: Key, *, limit: int | None = None) -> tuple[int, bytes | None]:
        """Count ``key``'s rows and keep only a singleton's digest, in constant space across batches.
        ``limit`` is used only for the final maxKeys candidate: two rows prove a duplicate, so that
        terminal cut need not drain its group and never saves a partial group's resume cursor."""
        self.group_rows = 0
        single = None
        while await self.head() == key:
            digest = self._rows[self._i][self._parts]
            self.group_rows += 1
            single = (
                (digest if len(digest) == 32 else digest + self._marked) if self.group_rows == 1 else None
            )
            self._i, self._head = self._i + 1, None
            if self.group_rows == limit:
                break
        return self.group_rows, single

    async def null_keys(self) -> int:
        """Rows of the population whose key is NULL or unmatched by its key map, in the same snapshot."""
        return await self._engine.null_keys(self.session, self._plan)

    def server(self) -> str:
        """The major and minor version of the server this pass reads."""
        return self._engine.release(self.session)


@asynccontextmanager
async def _snapshot(side: str, target: Target, plan: Plan, after: Key | None) -> AsyncIterator[_Stream]:
    """A side's snapshot, its catalogue read and its cursor not yet open."""
    engine = planned(side, plan, target)
    async with engine.snapshot(target) as session:
        relation = await engine.describe(session, plan["schema"], plan["name"], side=plan)
        yield _Stream(side, plan, engine, session, after, relation)


@asynccontextmanager
async def _snapshots(
    ctx: Context, spec: CompiledSpec, tables: Plan, after: Key | None
) -> AsyncIterator[tuple[_Stream, _Stream]]:
    """Admit both sides before opening a snapshot. A file sort is taken before opening a database
    snapshot, so none sits idle while this pass waits for another's sort."""
    targets = {s: ctx.targets.live(spec.sides[s].target) for s in Side}
    async with AsyncExitStack() as stack:
        serial = await stack.enter_async_context(scan_slots(ctx.gates, targets.values(), prefetch=True))
        if any("pinned" in tables[s.value] for s in Side):
            await stack.enter_async_context(duck.SORT.held())
        streams = {}
        for side, target in targets.items():
            await ctx.gates[target.target_id].limiter.acquire()
            stream = await stack.enter_async_context(_snapshot(side.value, target, tables[side.value], after))
            stream.serial = serial
            streams[side] = stream
        for stream in streams.values():
            stack.push_async_callback(stream.settle)  # before either session closes
        yield streams[Side.A], streams[Side.B]


async def _lowest(a: _Stream, b: _Stream) -> Key | None:
    """The lower ranked of the two sides' next keys, None at both ends: of equal ranks they are one key, a
    part's rank being its text or its value."""
    head_a, head_b = await a.head(), await b.head()
    if head_a is None:
        return head_b
    return head_a if head_b is None or a.rank <= b.rank else head_b


class _Stopped(Exception):
    def __init__(self, reason: str | None) -> None:
        self.reason = reason


async def scan(ctx: Context, run_id: str) -> str | None:
    """Scan the run's tables from its last checkpoint to the end, or until the run stops running; the stop
    condition (``until``, ``maxDuration``) that ends the run, judged at each fetched batch. A file side counts
    the keys several of its rows hold (``repeated_x``), must have read exactly the rows its plan counted
    once read to its end (``RowsMoved``), and records when its files' closing hash agreed (``verified_x``).

    Raises what stops it: ``connectors.tables.UNREACHABLE`` when a database is unreachable (the worker
    resumes the scan later, as it does after some of an engine's codes: ``runner.worker``),
    ``drift.Tripped`` when the schema guard paused the run before a cursor opened, anything else fails the
    run.
    """
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None  # a run is never deleted while it is scanned
        if run["scan"] is None or run["scan"]["done"]:
            return None  # finished by the scanner the sweep saw running when it read the run
        spec = await ctx.spec(conn, run["spec_id"], live=True)
        sealer = await ctx.sealer(conn, run_id)
    tables, state, cycle = unsealed(sealer, run["params"]), dict(run["scan"]), run["cycle"]
    assert tables is not None  # a scanned run is of two table sides
    conditions = StopConditions.from_spec(spec.raw)
    async with ctx.pool.connection() as conn:  # read again even when identical: they did not match
        unmatched = await repo.unmatched_keys(conn, run_id, cycle - 1) if cycle > 1 else []
    before = {sealer.open_row(row)["key"] for row in unmatched}
    saved = state["checkpoint"]  # the checkpoint this scanner builds on
    state["after"] = _opened_after(sealer, cycle, state["after"])
    after = None if state["after"] is None else tuple(state["after"])
    found: dict[str, str] = {}  # keys since the last checkpoint: raw -> masked (in the run's form)
    form = key_form(run["params"])
    async with _snapshots(ctx, spec, tables, after) as (a, b):
        # When the snapshots opened, not when a file side's sort returned; counted with the rows next saved:
        # a lost pass read none of them.
        opened = utc_now_iso()
        seen = {"a": a.relation, "b": b.relation}  # before either cursor opens, every cycle and resume
        if await drift.check(ctx, run, spec, seen, "scan" if after is None else "resume"):
            raise drift.Tripped(run_id)
        a.confirm()
        b.confirm()
        await a.open()
        await b.open()
        state["first_at"], state["last_at"] = state.get("first_at") or opened, opened
        for s, stream in zip(Side, (a, b), strict=True):
            state[f"snapshots_{s.value}"] = state.get(f"snapshots_{s.value}", 0) + 1
            if "pinned" in tables[s.value]:  # a file side's count: 0 says it scanned and met none
                state.setdefault(f"repeated_{s.value}", 0)
            if "engine" in tables[s.value]:  # a PostgreSQL pair's state is what it was
                state[f"server_{s.value}"] = stream.server()
        merged = checkpointed = 0

        async def check() -> None:
            nonlocal saved, checkpointed
            await asyncio.sleep(0)  # even an engine whose next batch is already buffered yields control
            reason = conditions.expired(run["started_at"], datetime.now(UTC))
            if merged != checkpointed and (reason or max(a.group_rows, b.group_rows) >= BATCH):
                # A long group's counters and key are local to take(), never part of this checkpoint.
                if not await _save(ctx, sealer, run_id, cycle, state, found, saved):
                    raise _Stopped(None)
                saved, checkpointed = saved + 1, merged
            if reason:
                raise _Stopped(reason)

        a.check = b.check = check
        try:
            while True:
                key = await _lowest(a, b)
                if key is None:
                    break
                text = encode_key(key)
                usable = bool(text) and len(text.encode()) <= MAX_KEY_BYTES
                final = usable and conditions.max_keys == state["candidates"] + len(found) + 1
                rows = {
                    "a": await a.take(key, limit=2 if final else None),
                    "b": await b.take(key, limit=2 if final else None),
                }
                for side, (count, _) in rows.items():
                    state[f"rows_{side}"] += count
                    if count > 1 and "pinned" in tables[side]:  # a file guarantees no uniqueness
                        state[f"repeated_{side}"] += 1
                if not usable:
                    for side, (count, _) in rows.items():
                        state[f"skipped_{side}"] += count
                elif rows["a"][0] == rows["b"][0] == 1 and rows["a"] == rows["b"] and text not in before:
                    state["matched"] += 1
                else:  # changed, one-sided, or repeated on a side
                    found[text] = mask_key(text, spec, ctx.tokeniser, form=form)
                state["after"], merged = list(key), merged + 1
                if (
                    conditions.max_keys is not None
                    and state["candidates"] + len(found) >= conditions.max_keys
                ):
                    stop = STOP_PREFIX + "maxKeys"
                    await _save(
                        ctx, sealer, run_id, cycle, state, found, saved, done=True, stop_reason=stop
                    )
                    return None
                if merged % SLICE == 0:  # the event loop serves the rest of the process between slices
                    await asyncio.sleep(0)
                if merged % BATCH == 0:
                    if not await _save(ctx, sealer, run_id, cycle, state, found, saved):
                        return None
                    saved, checkpointed = saved + 1, merged
                    if reason := conditions.expired(run["started_at"], datetime.now(UTC)):
                        return reason
        except _Stopped as stopped:
            return stopped.reason
        finally:
            a.check = b.check = None
        for side, stream in (("a", a), ("b", b)):  # rows_x counts every row read, skipped_x among them
            nulls = await stream.null_keys()
            state[f"rows_{side}"] += nulls
            state[f"skipped_{side}"] += nulls
            if "selected" in tables[side] and state[f"rows_{side}"] != tables[side]["selected"]:
                read, counted = state[f"rows_{side}"], tables[side]["selected"]
                raise RowsMoved(f"side {side}: {read:,} rows were read where the plan counted {counted:,}")
            if verified := getattr(stream.session, "verified_at", None):  # a fixed side's closing hash
                state[f"verified_{side}"] = verified
    if spec.raw.get("graph"):  # L2: the structure the facts make, before the scan is done
        state["graph"], diffs = await graph.analyse(ctx, spec, tables, state)
        async with ctx.pool.connection() as conn:
            await repo.replace_graph_diffs(conn, run_id, cycle, diffs)
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
    each key's per-key read is due once the checkpoint commits.
    False: the run stopped running, or another scanner saved since (a worker lock handover), so this one
    stops. Keys that share a masked key, among those found or with one saved before, raise
    ``KeysMaskAlike`` and save nothing."""
    try:
        sealed = sealer.seal_keys(found)
    except MaskedAlike as exc:
        found_alike = f"the scan found {exc.count} keys that are one key once masked"
        raise KeysMaskAlike(f"{found_alike} {_ALIKE}") from None
    state["candidates"] += len(found)
    state["done"] = done
    stored = state | {"after": None if done else seal_after(sealer, cycle, state["after"])}
    async with ctx.pool.connection() as conn:
        try:
            ok = await repo.save_scan(conn, run_id, cycle, stored, sealed, saved)
        except psycopg.errors.UniqueViolation as exc:  # rolled back with the checkpoint
            if exc.diag.constraint_name != "run_keys_pkey":
                raise
            saved_alike = "the scan found a key that is one key once masked with a key it saved before"
            raise KeysMaskAlike(f"{saved_alike} {_ALIKE}") from None
        if ok and stop_reason:  # maxKeys: the run goes on, its keys found so far are still read
            await repo.set_run_status(conn, run_id, "running", when=("running",), stop_reason=stop_reason)
            await repo.set_keys_cut(conn, run_id, {"taken": state["candidates"]})
    found.clear()
    return ok
