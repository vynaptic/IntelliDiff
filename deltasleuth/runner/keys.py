"""A run's key set: the keys given with the run, else the spec's ``key.source``.

A ``list`` source is inline. A ``paginated_endpoint`` is walked through the connectors (the
target's rate budget, retries and paging) and must be read completely: a partial listing would
compare another population than the spec declares. A complete walk is the census of the side whose
target it reads (docs/mvp/PACK.md §1.2). A ``table`` source with a target and a table of its own (a key
table) is planned anew at each read and read to its end in one snapshot, under one slot of its target's
gate: a key list, not a census of a side. Every key goes through ``key.normalize`` and must
match ``key.pattern`` and fit the store (at most 1024 UTF-8 bytes, no NUL); duplicates are dropped,
the first kept. A multi-part key (a composite key beside a service side) is its parts joined by ``|``,
each part normalised and matched on its own. Problems are reported by position, never by value (keys
may be personal data). The rule runs off the event loop: a key set may hold a million keys.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from deltasleuth.connectors import tables
from deltasleuth.connectors.base import UnsafeTargetError, utc_now_iso
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.tables.engine import ERRORS
from deltasleuth.context import Context
from deltasleuth.core import jsonio, sampling
from deltasleuth.core.canonical.paths import find
from deltasleuth.core.spec import key_table, listing_side
from deltasleuth.core.spec.errors import UnknownTargetError
from deltasleuth.core.spec.keys import fit_parts, join_parts, parts_match
from deltasleuth.core.types import CompiledSpec
from deltasleuth.runner.census import TIMEOUT_S
from deltasleuth.runner.compare import connect
from deltasleuth.runner.stop import StopConditions
from deltasleuth.security.tokenise import Tokeniser, mask_key

KEYLESS = "the key source holds {:,} row(s) without a key: leave them out with table.where"
UNROWED = (
    "the key source holds {:,} line(s) that are not rows of its declared CSV dialect: fix the export or its"
    " csv declaration"
)
OVER = "the key source holds over {:,} keys: narrow it with table.where"


class KeyAuthRefused(ValueError):
    """A key table's target refused the credential: a roll pauses its run for it, as a scan does, and the
    read is not tried again in a loop."""


async def resolve_keys(ctx: Context, spec: CompiledSpec, explicit: Sequence[str] | None = None) -> list[str]:
    """The normalised keys of ``explicit``, else of the spec's key source, in source order."""
    return (await resolve(ctx, spec, explicit))[0]


async def resolve(
    ctx: Context, spec: CompiledSpec, explicit: Sequence[str] | None = None, *, bound_s: float = TIMEOUT_S
) -> tuple[list[str], dict[str, Any] | None]:
    """``resolve_keys``, and the read's record: the listing's census record when the keys were walked from
    one (``{basis, target, items, first_at, last_at}``), or what a key table's read met (``{basis:
    "key_table", target, relation, rows, keys, at}``), which counts no side. ``bound_s`` bounds a key
    table's read."""
    source = spec.raw["key"].get("source", {})
    record = None
    if explicit is None and source.get("kind") == "table":
        keys, record = await _tabled(ctx, spec, source, bound_s)
    else:
        if explicit is not None:
            raw: Iterable[Any] = explicit
        elif source.get("kind") == "list":
            raw = source["keys"]
        elif source.get("kind") == "paginated_endpoint":
            raw, record = await _listed(ctx, spec, source)
        else:
            raise ValueError("no keys: pass keys with the run or declare key.source")
        keys = await asyncio.to_thread(normalise_keys, raw, spec)
    if not keys:
        raise ValueError("the key set is empty")
    return keys, record


def normalise_keys(raw_keys: Iterable[Any], spec: CompiledSpec) -> list[str]:
    """Apply ``key.normalize`` and check ``key.pattern`` and the store's limits; the first of
    duplicates is kept."""
    keys: dict[str, None] = {}
    _refuse(_fit(raw_keys, spec, keys), spec)
    return list(keys)


def _fit(raw_keys: Iterable[Any], spec: CompiledSpec, keys: dict[str, None], after: int = 0) -> list[int]:
    """The rule over ``raw_keys``, which stand after ``after`` others: those that pass join ``keys``; the
    positions of the others."""
    chain, pattern = [step.name for step in spec.key.normalize], spec.key.pattern or ""
    n = len(spec.key_parts) or 1
    bad = []
    for position, raw in enumerate(raw_keys, start=after + 1):
        key = fit_parts(jsonio.number_text(raw) if isinstance(raw, Decimal) else raw, chain, n)
        if key is None or not parts_match(pattern, key, n) or "\x00" in key or len(key.encode()) > 1024:
            bad.append(position)
        else:
            keys.setdefault(key)
    return bad


def _refuse(bad: Sequence[int], spec: CompiledSpec) -> None:
    """ValueError naming the positions of the keys the rule refused, if any."""
    if bad:
        n = len(spec.key_parts) or 1
        shown = ", ".join(map(str, bad[:10])) + (" ..." if len(bad) > 10 else "")
        failed = "do not match key.pattern"
        if n > 1:
            failed = f"are not {n} parts joined by |, or hold a part that fails key.pattern"
        raise ValueError(
            f"{len(bad)} key(s) {failed} after key.normalize,"
            f" or are over 1024 bytes or hold NUL (positions {shown})"
        )


def run_keys(keys: Sequence[str], spec: CompiledSpec, tokeniser: Tokeniser) -> dict[str, str]:
    """The keys a run takes (the first ``maxKeys``), each with its masked form."""
    return {
        key: mask_key(key, spec, tokeniser) for key in keys[: StopConditions.from_spec(spec.raw).max_keys]
    }


def keys_cut(resolved: Sequence[str], taken: Mapping[str, str]) -> dict[str, int] | None:
    """``runs.keys_cut`` when maxKeys cut the resolved key set (docs/mvp/APPROVALS.md §4), else None."""
    return {"resolved": len(resolved), "taken": len(taken)} if len(taken) < len(resolved) else None


async def _listed(
    ctx: Context, spec: CompiledSpec, source: dict[str, Any]
) -> tuple[list[Any], dict[str, Any]]:
    """The ``keyPath`` value of every item of a paginated listing ("" where an item has none), and the
    walk's census record. A multi-part key's ``keyPath`` selects its parts, each a string or a number: an
    item giving anything else is "" too."""
    side = listing_side(source)
    connector = connect(ctx, side.target, spec.execution)  # under the target's rate budget
    first_at = utc_now_iso()
    try:
        obs = await connector.fetch("keys", side)
    finally:
        await connector.aclose()
    last_at = utc_now_iso()
    if (
        obs.error_class
        or obs.http_status not in side.success_status
        or not obs.pages_eof
        or obs.pagination_unstable
    ):
        problem = obs.error_detail or obs.error_class or "an incomplete or unstable page walk"
        raise ValueError(f"the key listing was not read completely (HTTP {obs.http_status}: {problem})")
    listing, duplicate = jsonio.parse_json(obs.body or b"")
    if duplicate:  # RFC 8259 leaves a repeated name undefined: which keys were listed is unknown
        raise ValueError("the key listing repeats a member name, so its keys are ambiguous")
    found = [value for value, _ in find(side.pagination.items_path or "$", listing)]
    if len(found) != 1 or not isinstance(found[0], list):
        raise ValueError("the key listing's itemsPath does not select one array")
    walk = {"basis": "listing", "target": side.target, "items": len(found[0])}
    walk |= {"first_at": first_at, "last_at": last_at}
    path, n = source["keyPath"], len(spec.key_parts)
    if not n:
        return [next((key for key, _ in find(path, item)), "") for item in found[0]], walk
    keys = []
    for item in found[0]:
        parts = [jsonio.number_text(v) if isinstance(v, Decimal) else v for v, _ in find(path, item)]
        fit = len(parts) == n and all(isinstance(part, str) for part in parts)
        keys.append(join_parts(parts) if fit else "")
    return keys, walk


async def _tabled(
    ctx: Context, spec: CompiledSpec, source: Mapping[str, Any], bound_s: float
) -> tuple[list[str], dict[str, Any]]:
    """A key table's keys in key order, a key several rows hold once, and its read's record: planned, then
    read to its end in one snapshot under one slot of its target's gate, a fetched batch at a time through
    the rule. ``bound_s`` bounds each statement, and the plan and the read together. Whatever keeps it from
    being read is a ValueError that holds no key: a plan's refusal in its words, a CSV file's line that is
    not a row (by their count: the reader's log line says where), a row without a key, over
    ``sampling.LIMIT`` keys (counted before maxKeys cuts), keys the rule refuses (their positions are ranks
    in key order), or a fault of its target by its code alone, a refused credential ``KeyAuthRefused``."""
    cfg = key_table(source)
    keys: dict[str, None] = {}
    bad: list[int] = []
    rows, seen, last = 0, 0, None
    try:
        async with asyncio.timeout(bound_s):
            plan = await tables.key_plan(ctx.targets, cfg)
            if plan.get("rejected"):  # a CSV file's lines its plan counted: its reader leaves them out
                raise ValueError(UNROWED.format(plan["rejected"]))
            target = ctx.targets.live(cfg.target)
            gate = ctx.gates.setdefault(cfg.target, TargetGate.for_target(target))
            async with gate.semaphore:  # one slot and one tick of the target's budget for the whole read
                await gate.limiter.acquire()
                engine = tables.of(target)
                async with engine.snapshot(target, timeout_s=bound_s) as session:
                    at = utc_now_iso()
                    async for batch in engine.census(session, plan):
                        ranked, unfit = await asyncio.to_thread(_ranked, batch, last, spec, keys, seen)
                        rows, seen, last, bad = rows + len(batch), seen + ranked, batch[-1], bad + unfit
                        if seen > sampling.LIMIT:
                            raise ValueError(OVER.format(sampling.LIMIT))
                    skipped = await engine.null_keys(session, plan)
        if "selected" in plan and rows + skipped != plan["selected"]:  # a file's rows guard (FILES.md §7)
            read = rows + skipped
            raise tables.RowsMoved(f"{read:,} rows were read where the plan counted {plan['selected']:,}")
    except tables.Unconnected as exc:  # worded for whoever creates the run, but a refused credential
        if not isinstance(exc.cause, tables.TargetAuthRefused):
            raise
        raise _unread(exc.cause, bound_s) from None
    except (*ERRORS, UnsafeTargetError, UnknownTargetError, TimeoutError) as exc:
        raise _unread(exc, bound_s) from None
    if skipped:
        raise ValueError(KEYLESS.format(skipped))
    _refuse(bad, spec)
    record = {"basis": "key_table", "target": cfg.target, "relation": tables.relation(plan)}
    return list(keys), record | {"rows": rows, "keys": len(keys), "at": at}


def _ranked(
    batch: Sequence[Sequence[str]], last: Sequence[str] | None, spec: CompiledSpec, keys: dict[str, None],
    seen: int,
) -> tuple[int, list[int]]:  # fmt: skip
    """A fetched batch of a key table's rows, each its key's parts, through the rule (``_fit``): how many
    keys it holds, the rows of one key counting once (``last``: the row before the batch), and the ranks,
    after the ``seen`` keys before it, of those the rule refuses."""
    raw = [join_parts(row) for before, row in zip([last, *batch], batch, strict=False) if row != before]
    return len(raw), _fit(raw, spec, keys, seen)


def _unread(exc: BaseException, bound_s: float) -> ValueError:
    """Why a key table was not planned or read, by its fault's code alone."""
    code = tables.code(exc)
    if isinstance(exc, tables.TargetAuthRefused):
        return KeyAuthRefused(f"the key source could not be read: the target refused the credential ({code})")
    if isinstance(exc, (*tables.TIMEOUT, TimeoutError)):
        return ValueError(f"the key source timed out after {bound_s:.0f} s ({code})")
    if isinstance(exc, tables.SnapshotLost):
        return ValueError(f"the key source could not be held in one snapshot ({code})")
    return ValueError(f"the key source could not be read ({code})")
