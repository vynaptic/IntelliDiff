"""The census of a cycle: over which population of each side parity was
observed.

A table scan counts both sides as it reads each table to its end (``ds.scans``). A complete key listing
counts the side whose target it reads. A key-list run with a table side has each table side counted
first in every cycle, with packs or readiness configured, by the worker (``record``): in one snapshot the
population's keys are merged with the cycle's sorted key set, so churn cannot hide an omitted key, nor an
out-of-filter key stand in for one. A hash sample's draw records its frame's sources, each read to its
end, as its cycle's census (``runner.sample``); no key census counts it. A
census is ``{cycle, at, a, b}``, a record, a failed count (``{failed: <error class>}``) or None per side;
it is stored on the run for its current cycle and copied to the cycle's tombstone when the cycle ends.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from deltasleuth.auth.principal import WORKER, Principal
from deltasleuth.connectors.base import KeyRejectedError, utc_now_iso
from deltasleuth.connectors.tables import Plan, RowsMoved, decode_key, planned, relation, unsealed
from deltasleuth.context import Context
from deltasleuth.core.types import CompiledSpec, Side
from deltasleuth.store import repo

TIMEOUT_S = 3600.0  # each statement of a key census; a timeout fails the count, not the cycle
Key = tuple[bytes, ...]  # a key's parts, as the census orders them
log = logging.getLogger(__name__)


def of_cycle(run: repo.Row, ended: repo.Row, scan: repo.Row | None, spec: CompiledSpec) -> dict[str, Any]:
    """The ended cycle's census (``ended``, its ``ds.cycles`` row; ``scan``, its scan state; ``spec``, the
    run's version, which names each side's target). A table scan counts both sides once it read each
    table to its end, graph parity (L2) included for a graph spec, uncut by maxKeys; any other run has
    what its cycle recorded."""
    cycle = ended["cycle"]
    if run["params"].get("keys_source") != "table_scan":
        stored = ended["census"] or {}
        return {"cycle": cycle, "at": stored.get("at"), "a": stored.get("a"), "b": stored.get("b")}
    if (
        scan is None
        or not scan["done"]
        or ended["keys_cut"] is not None
        or (spec.raw.get("graph") and scan.get("graph") is None)
    ):
        return {"cycle": cycle, "at": None, "a": None, "b": None}
    tables = run["params"]["tables"]
    sides = {s: scanned(s, tables[s], scan, spec) for s in "ab"}
    return {"cycle": cycle, "at": scan.get("last_at"), **sides}


def scanned(side: str, plan: Plan, scan: repo.Row, spec: CompiledSpec) -> dict[str, Any]:
    """The census record of a table scan's side, read to its end."""
    found = {
        "basis": "scan",
        "target": spec.sides[Side(side)].target,
        "relation": relation(plan),
        "rows": scan[f"rows_{side}"],
        "skipped": scan[f"skipped_{side}"],
        "snapshots": scan.get(f"snapshots_{side}"),
        "first_at": scan.get("first_at"),
        "last_at": scan.get("last_at"),
    } | engined(plan, scan.get(f"server_{side}"))
    return fixed(found, plan, scan.get(f"verified_{side}"), scan.get(f"repeated_{side}", 0))


def fixed(found: dict[str, Any], plan: Plan, verified: str | None, repeated: int | None) -> dict[str, Any]:
    """A census record of a file side holds no snapshots and no digest, but its files'
    count, bytes and times, whether its mount was read-only, its lines that were not rows, the lines its
    spec's csv.skip left unread and its keys held by several rows; any other side's record as it is."""
    if plan.get("engine") != "files":
        return found
    held = {
        name: plan[name] for name in ("bytes", "pinned_at", "as_of", "modified_last", "mount", "rejected")
    }
    held |= {"lines_skipped": plan.get("lines_skipped", 0)}  # by csv.skip: read by no statement
    kept = {name: value for name, value in found.items() if name != "snapshots"}
    return kept | {"files": plan["file_count"], "verified_at": verified, "repeated": repeated} | held


def engined(plan: Plan, server: str | None = None) -> dict[str, Any]:
    """What the census record of a side on MySQL or Oracle adds: its engine, and
    whether its rows were read in a snapshot. A side whose plan names its engine, PostgreSQL's in such a
    pair too, adds ``server``, the version the read met, which may be another than the plan's. A
    PostgreSQL pair's records are unchanged."""
    read = {"server": server} if server and "engine" in plan else {}
    if plan.get("engine", "postgresql") == "postgresql":
        return read
    return {"engine": plan["engine"], "consistency": plan["consistency"]} | read


def listed(spec: CompiledSpec, walk: dict[str, Any] | None, cycle: int) -> dict[str, Any] | None:
    """The census a complete key listing (``runner.keys.resolve``'s record) gives the cycle: each side
    whose target it reads; None when it reads no side's, and for a key table's read, which counts none."""
    if walk is None or walk["basis"] != "listing":
        return None
    sides = {cfg.side.value: walk for cfg in spec.sides.values() if cfg.target == walk["target"]}
    return {"cycle": cycle, "at": walk["last_at"], **sides} if sides else None


def pending(run: repo.Row) -> list[str]:
    """The table sides of a key-list run that its current cycle has not counted yet (what
    ``repo.census_due`` finds in SQL); none for a hash sample, whose frame is its census."""
    tables = run["params"].get("tables") or {}
    if run["mode"] == "one_shot" or run["params"].get("keys_source") in ("table_scan", "hash_sample"):
        return []
    census = run["census"] or {}
    counted = census if census.get("cycle") == run["cycle"] else {}
    return [s for s in "ab" if tables.get(s) and not isinstance(counted.get(s), dict)]


async def record(ctx: Context, run_id: str, *, who: Principal = WORKER) -> dict[str, Any] | None:
    """Count each table side of the key-list run's current cycle not counted yet, store the census and
    audit ``run.census`` per side. A failed count, a timeout included, is recorded as its error class
    and warned; the cycle goes on. None when there was nothing to count, or the cycle ended meanwhile.
    ``RowsMoved`` alone is raised: a file side's reader that disagrees with its plan fails the run, and no
    later read of the cycle would meet it."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        sides = [] if run is None else pending(run)
        if not sides:
            return None
        # Not checked against today's targets: a target removed or changed since fails its side's count
        # below, which is recorded.
        spec = await ctx.spec(conn, run["spec_id"])  # type: ignore[index]
    assert run is not None
    cycle, tables = run["cycle"], run["params"]["tables"]
    try:
        async with ctx.pool.connection() as conn:
            tables = unsealed(await ctx.sealer(conn, run_id), run["params"])  # a file side's pins
        keys: list[str] | Exception = await _keys(ctx, run_id, cycle)
    except Exception as exc:  # the keyring lacks the run's KEK, say: no side can be counted
        keys = exc
    found: dict[str, dict[str, Any]] = {}
    for side in sides:
        try:
            if isinstance(keys, Exception):
                raise keys
            found[side] = await _count(ctx, spec, side, tables[side], keys)
        except RowsMoved:
            raise
        except Exception as exc:
            log.warning("census of side %s of run %s failed: %s", side, run_id, type(exc).__name__)
            found[side] = {"failed": type(exc).__name__}
    census = {"cycle": cycle, "at": utc_now_iso(), **found}
    async with repo.audited(ctx.pool) as (conn, audit):
        if not await repo.set_census(conn, run_id, census):
            return None
        for side, counted in found.items():
            await audit(who, "run.census", run_id, _detail(cycle, side, counted))
    return census


async def _keys(ctx: Context, run_id: str, cycle: int) -> list[str]:
    async with ctx.pool.connection() as conn:
        sealer = await ctx.sealer(conn, run_id)
        rows = await repo.pages(repo.list_keys, conn, run_id, cycle)
    return [sealer.open_row(row)["key"] for row in rows]


def _parts(key: str, n: int) -> Key | None:
    try:
        return tuple(part.encode() for part in decode_key(key, n))
    except KeyRejectedError:  # not a key of this table: it counts as outside the population
        return None


async def _count(ctx: Context, spec: CompiledSpec, side: str, plan: Plan, keys: list[str]) -> dict[str, Any]:
    """One table side's census: its population's keys, in one snapshot, merged with the sorted key set."""
    n = len(plan["keys"])
    wanted = sorted({parts for key in keys if (parts := _parts(key, n)) is not None})
    target = spec.sides[Side(side)].target
    rows = missing = matched = i = repeated = 0
    previous: Key | None = None
    live = ctx.targets.live(target)
    engine = planned(side, plan, live)
    async with engine.snapshot(live, timeout_s=TIMEOUT_S) as session:
        at, server, twice = utc_now_iso(), engine.release(session), None  # twice: the key last repeated
        async for batch in engine.census(session, plan):
            for row in batch:
                rows += 1
                key = tuple(part.encode() for part in row)
                if key == previous:  # another row of the same key
                    repeated, twice = repeated + (key != twice), key
                    continue
                if previous is not None and key < previous:
                    raise ValueError("the population's keys came out of order")
                previous = key
                while i < len(wanted) and wanted[i] < key:
                    i += 1
                if i < len(wanted) and wanted[i] == key:
                    matched, i = matched + 1, i + 1
                else:
                    missing += 1
        skipped = await engine.null_keys(session, plan)
    if "selected" in plan and rows + skipped != plan["selected"]:  # a file side's rows guard
        read = rows + skipped
        raise RowsMoved(f"side {side}: {read:,} rows were read where the plan counted {plan['selected']:,}")
    found = {
        "basis": "table",
        "target": target,
        "relation": relation(plan),
        "where": plan.get("where"),
        "rows": rows + skipped,  # every row of the filtered table, those without a key among them
        "skipped": skipped,
        "missing": missing,
        "extra": len(keys) - matched,
        "at": at,
    } | engined(plan, server)
    return fixed(found, plan, getattr(session, "verified_at", None), repeated)


def _detail(cycle: int, side: str, counted: dict[str, Any]) -> dict[str, Any]:
    """The ``run.census`` audit detail of one side's count; a MySQL or Oracle side's names its engine and
    how it was read."""
    detail = {"cycle": cycle, "side": side, "basis": "table"}
    if "failed" in counted:
        return detail | {"failed": counted["failed"]}
    detail |= {name: counted[name] for name in ("rows", "missing", "extra")}
    if counted.get("consistency") == "fixed":  # a file side's, in place of its snapshot
        return detail | {
            name: counted[name] for name in ("consistency", "files", "bytes", "rejected", "repeated")
        }
    detail |= {"snapshots": 1}
    return detail | {name: counted[name] for name in ("engine", "consistency") if name in counted}


# -- what the certificate says ------------------------------------------------------------------


# How a side that its engine could not pin was read, by its plan's consistency:
# what stands where a snapshot stands, one or several, and why.
_PINNED = ("snapshot", "snapshots", "")
_UNPINNED = {
    "unverified": ("transaction", "transactions", "a snapshot not observable (a MySQL view)"),
    "none": ("pass", "passes", "not a snapshot (a MySQL table outside InnoDB)"),
}
_AS_READ = "rows and values as the account reads them (redaction, masking and row policies apply)"


def _moment(at: str | None) -> str:
    return "-" if at is None else f"{datetime.fromisoformat(at):%Y-%m-%d %H:%M} UTC"


def _read(found: dict[str, Any], count: int = 1) -> str:
    """``in 1 snapshot``, or what a side its engine could not pin was read in instead, or the files a file
    side was read from."""
    if found.get("consistency") == "fixed":
        return _fixed(found)
    one, several, why = _UNPINNED.get(found.get("consistency", ""), _PINNED)
    return f"in {count} {one if count == 1 else several}" + (f", {why}" if why else "")


def _fixed(found: dict[str, Any]) -> str:
    """Where a file side's record says what it was read from: its files as DeltaSleuth read them between
    their pin and their closing hash, never the system they were exported from; no digest."""
    hashed = f" and hashed the same when the read ended at {_utc(found['verified_at'])}"
    stamp = f"declared as of {_utc(found['as_of'])} by its stamp file, not observed" if found["as_of"] else ""
    skipped = found.get("lines_skipped") or 0
    unread = f" ({skipped:,} line(s) at the files' starts skipped as the spec declares, not read)"
    return (
        f"from {found['files']:,} fixed file(s), {found['bytes']:,} bytes{unread if skipped else ''}, pinned"
        f" {_utc(found['pinned_at'])}"
        f"{hashed if found.get('verified_at') else ''}; an export, not the system it came from:"
        f" {stamp or 'its own time is not observed'}; newest file modified {_utc(found['modified_last'])} on"
        " the mount"
    )


def _utc(at: str) -> str:
    return f"{datetime.fromisoformat(at).astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def described(found: dict[str, Any]) -> str:
    """How a side's census record observed it (a scan state saved before 009 has no snapshots or times);
    a MySQL or Oracle side's also says whose view of the rows it is."""
    pinned = found.get("consistency") == "fixed"  # a file side's says what it was read from instead
    return _described(found) + (f"; {_AS_READ}" if "engine" in found and not pinned else "")


def _described(found: dict[str, Any]) -> str:
    at = "" if found.get("consistency") == "fixed" else f" at {_moment(found.get('at'))}"
    if found["basis"] == "table":
        return (
            f"its key set checked against {found['relation']} {_read(found)}{at}:"
            f" {found['missing']} key(s) missing, {found['extra']} outside it"
        )
    if found["basis"] == "frame":  # a hash sample's
        return f"its population read to its end {_read(found)}{at} as the sampling frame"
    first, last = _moment(found["first_at"]), _moment(found["last_at"])
    if found["basis"] == "listing":
        return (
            f"a paginated walk between {first} and {last} (ordering declared stable by the target), not a"
            " snapshot"
        )
    if found.get("consistency") == "fixed":
        return f"read to its end {_read(found)}"
    if found["snapshots"] is None:
        return "read to its end; snapshots not recorded (before 009)"
    if found["snapshots"] == 1:
        return f"read to its end {_read(found)} at {first}"
    return f"read to its end {_read(found, found['snapshots'])} between {first} and {last}"


def warning(side: str, found: dict[str, Any] | None) -> str | None:
    """The certificate's warnings for a side: a table scan read in several snapshots, a side its engine
    could not pin, and of a file side side b's lines that were not rows (side a's are no census), keys
    held by several rows and a mount it could write to, each on a line of its own; None without any."""
    if found is None:
        return None
    warnings = []
    _, several, why = _UNPINNED.get(found.get("consistency", ""), _PINNED)
    if side == "b" and found.get("rejected"):
        warnings.append(
            f"SIDE B: {found['rejected']} LINE(S) OF ITS FILES WERE NOT ROWS and were not compared: a key on"
            " such a line is not compared, and a key not found on this side is inconclusive"
        )
    if found.get("repeated"):
        held = f"{found['repeated']} KEY(S) ARE HELD BY MORE THAN ONE ROW"
        warnings.append(f"SIDE {side.upper()}: {held}: each is inconclusive, never paired")
    if found.get("mount") == "writable":
        warnings.append(f"SIDE {side.upper()} WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO")
    if found.get("basis") == "scan" and (found.get("snapshots") or 0) > 1:
        warnings.append(
            f"SIDE {side.upper()} READ IN {found['snapshots']} {several.upper()} between"
            f" {_moment(found['first_at'])} and {_moment(found['last_at'])}: rows added behind a resume point"
            " meanwhile were not read"
        )
    if why:
        warnings.append(
            f"SIDE {side.upper()} NOT READ IN A SNAPSHOT ({why}): rows written during the read may be missing"
            " or read twice"
        )
    return "\n".join(warnings) or None
