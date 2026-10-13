"""A hash sample's draws, retries and judgements (docs/mvp/SAMPLING.md §3, §5, §7, §8).

A sampled run is created with no keys and a pending draw (``runs.draw``). The worker's draw task (``draw``)
reads the run's frame from the spec's declared targets only, its sources in order: a table side's
population in one snapshot, or a side's complete key listing, streamed a page at a time. Each key passes
``key.normalize``, ``key.pattern`` and the store's limits, or is unfit: counted per source, never shown,
left out. A frame holding a listing or two sources is deduplicated in a temporary table of the draw's own
session, keyed by the first 16 bytes of each key's selection digest (the nonce keys it, and it goes with
the session); the first source to give a key gives its strata row. ``core.sampling`` selects, a batch at a
time off the event loop. The chosen keys are masked and sealed, then stored in one audited transaction
before any key of the cycle exists: the draw (its nonce, strata and the selection's SHA-256), its keys with
their labels, the cycle's census and ``sample.draw``, whose row holds the salt's SHA-256 but never the
nonce. A draw its frame refuses (``DrawError``) pauses the run; one the run stopped meanwhile is discarded,
nothing disclosed, its frame's read stopping at the next check of the run.

A sign-off member no read compared is read again (``retriable``). Every end of a drawn cycle judges its
strata from stored rows (``judge``: counts, the exact decision, the bounds shown, verdicts), stored once in
``ds.cycles.sample``. Creation and resume refusals are ``SampleRefused``.

Every draw of a population, whatever spec drew it, is on its ledger in commit order (the chain seqs of
its ``sample.draw`` and ``sample.close`` rows, §9). A draw is signed off only when every earlier draw
passed or was closed before it (R-a), its own cycle passed (R-b), no later draw failed (R-c) and its keys
re-hash to its selection (R-d): ``problems``, from stored draws and judgements, never recomputed. A
person closes a name's ended draws that did not pass with a note of the fix (``close``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Context as Decimals
from decimal import Decimal
from fractions import Fraction
from functools import partial
from typing import Any

import psycopg

from deltasleuth.auth.principal import WORKER, Principal
from deltasleuth.connectors.base import UnsafeTargetError, utc_iso, utc_now_iso
from deltasleuth.connectors.rest import ListingIncomplete, RestConnector
from deltasleuth.connectors.tables import (
    TIMEOUT,
    KeyUnfit,
    SnapshotLost,
    TargetAuthRefused,
    TargetRefused,
    code,
    encode_key,
    planned,
    relation,
    unsealed,
)
from deltasleuth.context import Context
from deltasleuth.core import jsonio, sampling
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.canonical.paths import find
from deltasleuth.core.sampling import DrawError, Selection, passes, percent, share
from deltasleuth.core.spec import compile_spec, key_matches, listing_side, normalise_key
from deltasleuth.core.spec.errors import SpecError, UnknownTargetError
from deltasleuth.core.spec.expressions import compile_key_pattern
from deltasleuth.core.types import (
    ERROR_REASONS,
    CompiledSpec,
    FrameMember,
    KeyResult,
    Outcome,
    Sample,
    Target,
)
from deltasleuth.runner import census, counters
from deltasleuth.runner.compare import connect
from deltasleuth.runner.recheck import RecheckPolicy
from deltasleuth.security import rawvalues
from deltasleuth.security.crypto import Sealer
from deltasleuth.security.tokenise import mask_key
from deltasleuth.store import repo

DRAW_PAUSE = "run_paused_draw"  # its frame refused the draw (W1-W9); a resume draws again, a new nonce
SAMPLE_PAUSE = "run_paused_sample"  # a soak's boundary after a bottom_k draw that did not pass (D7)
DEDUPE = 10_000  # keys a batch of the frame's temporary table
WATCH_S = 5.0  # seconds between a frame read's checks that its run still waits for the draw
MAX_KEY_BYTES = 1024  # a run key
TRANSIENT = frozenset({*ERROR_REASONS, "unstable_pagination"})  # a read that may compare when read again
_SIX = Decimals(prec=6)
ETA_WINDOW = 900  # seconds of finished keys a live draw's ETA is paced by
NOTE_KEY_SHAPED = "the note holds a key-shaped value: write it without customer keys"
# G9's words for an earlier draw that did not pass (a live one cannot precede a draw of its population, D8).
_UNPASSED = {"failed": "failed", "incomplete": "ended incomplete", "unjudged": "ended without a judgement",
             "live": "had not ended"}  # fmt: skip
log = logging.getLogger(__name__)
Candidate = tuple[Any, Any]  # a source's raw key and strata row (a table's as JSON text)


class SampleRefused(Exception):
    """A sampled run refused at creation or resume (docs/mvp/SAMPLING.md §13 R1-R5); ``status`` is the
    API's answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Moved(Exception):
    """The run stopped, moved on to another cycle or lost its pending draw while its frame was read."""


# -- creation and resume (§5) -------------------------------------------------------------------------


def refuse(mode: str, keys: object, samples_dir: object, soak: Mapping[str, Any] | None) -> None:
    """R4, R2 and R3: a hash sample draws its keys from the frame, a new sample each soak cycle, live."""
    if keys is not None:
        raise SampleRefused(422, "a hash sample draws its keys from the frame: run it without --keys")
    if mode == "soak" and not (soak or {}).get("rolling"):
        raise SampleRefused(
            422,
            "a soak over a hash sample draws a new sample each cycle: start it rolling"
            " (ds run --soak --rolling)",
        )
    if samples_dir is not None:
        raise SampleRefused(
            422, "captured samples cannot be sampled: a hash sample reads its frame from the targets"
        )


async def overlapping(conn: repo.Conn, spec: CompiledSpec, *, lock: bool = False) -> None:
    """R1: a ``bottom_k`` spec draws no sample of a population a live ``bottom_k`` run of any spec samples
    (D8); ``lock`` judges it under the DSSM lock of each population id, until the transaction ends."""
    sample = spec.sample
    if sample is None or sample.method != "bottom_k":
        return
    for run in await repo.live_sampled_runs(conn, sample.population, lock=lock):
        shared = ", ".join(sorted(set(run["population"]) & set(sample.population)))
        raise SampleRefused(
            409,
            f"spec {spec.name} samples the population of live sampled run {run['run_id']} (spec"
            f" {run['spec_name']}, {shared}): stop it before drawing another sample"
            " (docs/mvp/SAMPLING.md §5)",
        )


async def refuse_resume(conn: repo.Conn, run: repo.Row) -> None:
    """R5: a sampled run whose cycle is still to be drawn resumes only once the ``bottom_k`` draw of the
    cycle before passed or was closed, so the fix and its close precede the next draw (D7)."""
    if run["params"].get("keys_source") != "hash_sample" or run["draw"] is None or run["cycle"] == 1:
        return
    before = await repo.get_draw(conn, run["run_id"], run["cycle"] - 1)
    if before is None or before["method"] != "bottom_k" or before["closed_at"] is not None:
        return
    ended = await repo.get_cycle(conn, run["run_id"], run["cycle"] - 1)
    if ((ended or {}).get("sample") or {}).get("status") != "passed":
        raise SampleRefused(
            409,
            f"run {run['run_id']} paused after draw {before['draw_id']} did not pass: close it with the fix"
            " (POST /v1/samples/close), then resume",
        )


# -- the draw (§3, §4, §5) ----------------------------------------------------------------------------


async def draw(ctx: Context, run_id: str) -> None:
    """Draw the running run's pending sample, or pause the run on the refusal its frame gives (DrawError).
    Raises what stops it otherwise: ``connectors.tables.UNREACHABLE`` while a database is unreachable (the
    worker draws again later), a KEK the keyring lacks, anything else a bug."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None or run["status"] != "running" or run["draw"] is None:
            return
        spec = await ctx.spec(conn, run["spec_id"])
        sealer = await ctx.sealer(conn, run_id)
    sample, cycle, nonce = spec.sample, run["cycle"], sampling.nonce()
    assert sample is not None
    salt = sampling.salt(sample.label, nonce)
    try:
        opened = run | {"params": run["params"] | {"tables": unsealed(sealer, run["params"])}}  # file pins
        frame = _Frame(ctx, spec, opened, salt)
        await frame.read()
        selection = await asyncio.to_thread(frame.selection().result)
    except DrawError as exc:
        await _refused(ctx, run_id, cycle, str(exc))
        return
    except _Moved:  # the run moved on meanwhile: nothing was drawn
        return
    sealed, labels, selected = await asyncio.to_thread(_sealed, ctx, spec, sealer, selection)
    row = {
        "draw_id": repo.new_draw_id(), "run_id": run_id, "spec_id": run["spec_id"], "cycle": cycle,
        "method": sample.method, "population": list(sample.population), "salt_label": sample.label,
        "nonce": nonce, "keys": len(sealed), "frame": frame.summary(selection), "strata": selection.strata,
        "selection_sha256": selected,
    }  # fmt: skip
    strata = [{name: s[name] for name in ("label", "frame_keys", "drawn")} for s in selection.strata]
    detail = {name: row[name] for name in ("draw_id", "cycle", "method", "population", "salt_label")}
    detail |= {"salt_sha256": sha256_hex(salt), "keys": row["keys"], "selection_sha256": selected}
    detail |= {"frame": row["frame"], "strata": strata}  # never the nonce: no SIEM reader can draw it again
    async with repo.audited(ctx.pool) as (conn, audit):
        # The run's row lock first: a stop meanwhile moved the run on, and the draw is discarded unseen.
        if not await repo.set_run_status(conn, run_id, "running", when=("running",)):
            return
        latest = await repo.get_run(conn, run_id)
        if latest is None or latest["cycle"] != cycle or latest["draw"] is None:
            return
        await repo.insert_draw(conn, row, sealed, labels)
        await repo.set_census(conn, run_id, {"cycle": cycle, "at": frame.read_at, **frame.records})
        await repo.set_draw(conn, run_id, None)
        await audit(WORKER, "sample.draw", run_id, detail)


def _sealed(
    ctx: Context, spec: CompiledSpec, sealer: Sealer, selection: Selection
) -> tuple[dict[str, bytes], list[tuple[str | None, str | None, bool]], str]:
    """The chosen keys masked and sealed in seq order, their labels and the selection's SHA-256."""
    taken = {d.key: mask_key(d.key, spec, ctx.tokeniser) for d in selection.keys}
    lines = ((d.seq, taken[d.key], d.stratum, d.signoff, d.member) for d in selection.keys)
    labels = [(d.stratum, d.signoff, d.member) for d in selection.keys]
    return sealer.seal_keys(taken), labels, sampling.selection_sha256(lines)


async def _refused(ctx: Context, run_id: str, cycle: int, error: str) -> None:
    """Pause the running run for its frame's refusal, recorded in ``draw.error`` (a safe text) and audited;
    ``resume_run`` clears it."""
    log.warning("draw of run %s refused: %s", run_id, error)
    async with repo.audited(ctx.pool) as (conn, audit):
        if not await repo.set_run_status(conn, run_id, "paused", when=("running",), stop_reason=DRAW_PAUSE):
            return
        run = await repo.get_run(conn, run_id)
        pending = (run or {}).get("draw") or {}
        await repo.set_draw(conn, run_id, pending | {"error": error, "at": utc_now_iso()})
        await audit(WORKER, "paused", run_id, {"reason": DRAW_PAUSE})
        await audit(WORKER, "sample.draw_failed", run_id, {"cycle": cycle, "error": error})


class _Frame:
    """A run's frame, read source by source into its selection (§3), with each side's census record."""

    def __init__(self, ctx: Context, spec: CompiledSpec, run: repo.Row, salt: str) -> None:
        assert spec.sample is not None
        self._ctx, self._spec, self._run, self._salt = ctx, spec, run, salt
        self._sample: Sample = spec.sample
        self._members = spec.sample.members
        self._chain = [n.name for n in spec.key.normalize]
        self._pattern = compile_key_pattern(spec.key.pattern or "")
        self._selector: sampling.Selector | None = None
        self._session: psycopg.AsyncConnection | None = None  # the temporary table's, when one is needed
        self._keys, self._unfit, self._repeats = Counter[int](), Counter[int](), Counter[int]()
        self._fed = Counter[int]()  # keys a source gave the selection, which positions count
        self._both = 0
        self._watched = asyncio.get_running_loop().time()  # the last check that the run waits for the draw
        self.records: dict[str, dict[str, Any]] = {}  # by side: the census record of the source listing it
        self.read_at: str | None = None

    async def read(self) -> None:
        """Read each source to its end; DrawError when one is incomplete or refused."""
        deduped = len(self._members) > 1 or self._members[0].kind != "table"  # a table's keys are distinct
        try:
            if deduped:
                self._session = await _session(self._ctx)
            for i, member in enumerate(self._members):
                if member.kind == "table":
                    await self._table(i, member)
                else:
                    await self._listing(i, member)
        finally:
            if self._session is not None:
                await self._session.close()
        self.read_at = utc_now_iso()

    def selection(self) -> sampling.Selector:
        if self._selector is None:
            raise DrawError("the frame is empty")
        return self._selector

    def summary(self, selection: Selection) -> dict[str, Any]:
        """The draw's ``frame``: its distinct keys, by the sides whose sources held them, and the unfit."""
        held = {m.side.value: self._keys[i] - self._both for i, m in enumerate(self._members)}
        origin = {"a": held.get("a", 0), "b": held.get("b", 0), "both": self._both}
        return {"keys": selection.frame_keys, "origin": origin, "unfit": sum(self._unfit.values())}

    def _started(self, moment: datetime) -> None:
        """The frame's read start (its first source's): ``now`` in its strata."""
        if self._selector is None:
            self._selector = sampling.Selector(self._sample, self._salt, moment)

    def _target(self, target_id: str) -> Target:
        try:
            return self._ctx.targets.live(target_id)
        except UnknownTargetError:
            raise DrawError(f"target {target_id} is not in the registry") from None

    async def _table(self, i: int, member: FrameMember) -> None:
        """A table side's population in one snapshot, in census order: several rows of a key count once,
        its first row giving its strata; keys out of order are refused."""
        side = member.side.value
        plan = self._run["params"]["tables"][side]
        target = self._target(self._spec.sides[member.side].target)
        order: list[tuple[bytes, ...] | None] = [None]  # the last key read
        engine, rows = planned(side, plan, target), 0
        try:
            async with engine.snapshot(target, timeout_s=census.TIMEOUT_S) as session:
                moment, server = datetime.now(UTC), engine.release(session)
                self._started(moment)
                async for fetched in engine.frame(session, plan):
                    rows += len(fetched)
                    await self._take(i, await asyncio.to_thread(_distinct, fetched, len(plan["keys"]), order))
                skipped = await engine.null_keys(session, plan)
        except TIMEOUT as exc:
            raise DrawError(
                f"the frame of side {side} timed out after {census.TIMEOUT_S:.0f} s ({code(exc)})"
            ) from None
        except SnapshotLost as exc:
            held = "could not be held in one snapshot"
            raise DrawError(f"the frame of side {side} {held} ({code(exc)})") from None
        except TargetAuthRefused as exc:
            refused = "could not be read: the target refused the credential"
            raise DrawError(f"the frame of side {side} {refused} ({code(exc)})") from None
        except (TargetRefused, KeyUnfit, UnsafeTargetError) as exc:  # a server's other code, or the guard
            raise DrawError(f"the frame of side {side} could not be read ({code(exc)})") from None
        found = {
            "basis": "frame", "target": target.target_id, "relation": relation(plan),
            "where": plan.get("where"), "rows": rows + skipped, "skipped": skipped, "keys": self._keys[i],
            "unfit": self._unfit[i], "at": utc_iso(moment),
        } | census.engined(plan, server)  # fmt: skip
        # A file side's record says what it was read from (docs/mvp/FILES.md §11); a frame counts no repeat.
        self.records[side] = census.fixed(found, plan, getattr(session, "verified_at", None), None)

    async def _listing(self, i: int, member: FrameMember) -> None:
        """A side's key listing, walked to its end a page at a time; W2 when it lists a key twice, W1 when
        it was not read completely."""
        side = listing_side(member.source)
        connector = connect(self._ctx, self._target(side.target).target_id, self._spec.execution)
        assert isinstance(connector, RestConnector)
        moment, items, incomplete = datetime.now(UTC), 0, None
        self._started(moment)
        try:
            async for page in connector.walk(side):
                items += len(page)
                await self._take(i, await asyncio.to_thread(_listed, page, member))
        except ListingIncomplete as exc:
            incomplete = str(exc)
        finally:
            await connector.aclose()
        if self._repeats[i]:
            raise DrawError(
                f"the key listing of target {side.target} lists {self._repeats[i]:,} key(s) twice: its"
                " pages shifted while they were read, so keys may be missing (declare stable_ordering, or"
                " use cursor pagination)"
            )
        if incomplete is not None:
            raise DrawError(incomplete)
        self.records[member.side.value] = {
            "basis": "listing", "target": side.target, "items": items, "keys": self._keys[i],
            "unfit": self._unfit[i], "first_at": utc_iso(moment), "last_at": utc_now_iso(),
        }  # fmt: skip

    async def _take(self, i: int, candidates: list[Candidate]) -> None:
        """Feed a batch of source ``i``'s candidates to the selection: unfit keys counted, keys another
        source (or this one) gave already left out."""
        await self._watch()
        fit, unfit = await asyncio.to_thread(self._fit, candidates)
        self._unfit[i] += unfit
        if self._session is None:
            new = fit
            self._keys[i] += len(new)
        else:
            new = []
            for start in range(0, len(fit), DEDUPE):
                new += await self._dedupe(i, fit[start : start + DEDUPE])
        if new:
            first, self._fed[i] = self._fed[i] + 1, self._fed[i] + len(new)
            await asyncio.to_thread(
                self.selection().add, [(k, _row(r)) for k, r in new], source=i, first=first
            )

    async def _watch(self) -> None:
        """_Moved once the run, read every ``WATCH_S`` seconds, is no longer running, has moved on to
        another cycle or has no draw pending: the read stops, its snapshot, walk and session closed."""
        now = asyncio.get_running_loop().time()
        if now - self._watched < WATCH_S:
            return
        self._watched = now
        async with self._ctx.pool.connection() as conn:
            run = await repo.get_run(conn, self._run["run_id"])
        if run is None or run["status"] != "running" or run["cycle"] != self._run["cycle"] or not run["draw"]:
            raise _Moved

    def _fit(self, candidates: list[Candidate]) -> tuple[list[Candidate], int]:
        """The candidates whose keys pass ``key.normalize``, ``key.pattern`` and the store's limits."""
        fit, unfit = [], 0
        for raw, row in candidates:
            if isinstance(raw, Decimal):
                raw = jsonio.number_text(raw)
            key = normalise_key(raw, self._chain) if isinstance(raw, str) else ""
            if key_matches(self._pattern, key) and "\x00" not in key and len(key.encode()) <= MAX_KEY_BYTES:
                fit.append((key, row))
            else:
                unfit += 1
        return fit, unfit

    async def _dedupe(self, i: int, fit: list[Candidate]) -> list[Candidate]:
        """The keys of a batch new to the frame. The first source only inserts, its keys not inserted being
        repeats; the second marks the first source's keys it gives (member 2: counted once, as both), then
        inserts the rest: a key it gave already, marked or its own, is a repeat."""
        assert self._session is not None
        digests: dict[uuid.UUID, Candidate] = {}
        for key, row in fit:
            h = uuid.UUID(bytes=sampling.digest(self._salt, key)[:16])
            if h in digests:
                self._repeats[i] += 1
            else:
                digests[h] = (key, row)
        shared: set[uuid.UUID] = set()
        if i:
            cursor = await self._session.execute(
                "UPDATE frame_keys SET member = 2 WHERE h = ANY(%s::uuid[]) AND member = 0 RETURNING h",
                (list(digests),),
            )
            shared = {row[0] for row in await cursor.fetchall()}
        cursor = await self._session.execute(
            "INSERT INTO frame_keys SELECT unnest(%s::uuid[]), %s ON CONFLICT DO NOTHING RETURNING h",
            ([h for h in digests if h not in shared], i),
        )
        inserted = {row[0] for row in await cursor.fetchall()}
        both = len(shared)
        self._both += both
        self._repeats[i] += len(digests) - len(inserted) - both
        self._keys[i] += len(inserted) + both
        return [digests[h] for h in digests if h in inserted]


async def _session(ctx: Context) -> psycopg.AsyncConnection:
    """The draw's own autocommit control-plane session, with its temporary table of frame keys."""
    conn = await psycopg.AsyncConnection.connect(ctx.pool.conninfo, autocommit=True)
    try:
        await conn.execute(
            "SELECT set_config('temp_buffers', %s, false)", (ctx.settings.sampling.temp_buffers,)
        )
        await conn.execute("CREATE TEMP TABLE frame_keys (h uuid PRIMARY KEY, member smallint NOT NULL)")
    except BaseException:
        await conn.close()
        raise
    return conn


def _distinct(
    fetched: list[tuple[Any, ...]], parts: int, order: list[tuple[bytes, ...] | None]
) -> list[Candidate]:
    """A fetch of a frame's rows as candidates: a key's later rows left out, keys out of order refused,
    as ``census._count`` refuses them; ``order`` holds the last key across fetches."""
    candidates = []
    for row in fetched:
        key = tuple(part.encode() for part in row[:parts])
        if key == order[0]:
            continue
        if order[0] is not None and key < order[0]:
            raise DrawError("the population's keys came out of order")
        order[0] = key
        candidates.append((encode_key(row[:parts]), row[parts]))
    return candidates


def _listed(page: list[Any], member: FrameMember) -> list[Candidate]:
    """A listing page's items as candidates: the ``keyPath`` value ("" where an item has none) and the
    first match of each strata field (None where it has none)."""
    key_path = member.source["keyPath"]
    return [
        (
            next((key for key, _ in find(key_path, item)), ""),
            {name: next((v for v, _ in find(path, item)), None) for name, path in member.fields.items()},
        )
        for item in page
    ]


def _row(row: Any) -> Mapping[str, Any]:
    """A strata row: a table's JSON document parsed, numbers exact."""
    return jsonio.parse_json(row)[0] if isinstance(row, str) else row


# -- retries to resolution (§7) -----------------------------------------------------------------------


def retriable(result: KeyResult, before: KeyResult | None) -> bool:
    """Whether a sign-off member's final result is read again (§7, D3): INCONCLUSIVE for an error, an unstable
    listing or a failed recheck, while no read of this attempt compared: ``before``, the stored result its
    last pass judged (the result itself on a first read), is INCONCLUSIVE for an error or an unstable
    listing. A comparable read stays the stored result, so a key that read MATCH, or found a discrepancy, is
    never read again; nor is one whose rechecks failed on a read no retry changes (truncated, ambiguous)."""
    first = result if before is None else before
    return (
        result.outcome is Outcome.INCONCLUSIVE
        and result.inconclusive_reason in {*TRANSIENT, "recheck_failed"}
        and first.outcome is Outcome.INCONCLUSIVE
        and first.inconclusive_reason in TRANSIENT
    )


# -- judging each stratum (§8) ------------------------------------------------------------------------


async def judge(
    conn: repo.Conn, drawn: repo.Row, spec: CompiledSpec, *, ended: bool = False
) -> dict[str, Any]:
    """The judgement of the draw ``drawn``'s cycle (``judged``) from one grouped query of stored rows: it
    needs no key."""
    groups = await repo.stratum_counts(conn, drawn["run_id"], drawn["cycle"])
    return await asyncio.to_thread(judged, drawn, groups, spec, ended)


async def ending(
    ctx: Context, conn: repo.Conn, run: repo.Row, spec: CompiledSpec | None = None
) -> tuple[repo.Row | None, dict[str, Any] | None]:
    """The draw of the run's current cycle and the judgement its end stores (§5 Ends): (None, None) for a
    cycle without a draw. A judgement that raises is logged and left None, the cycle unjudged (§9); the
    caller's transaction goes on."""
    if run["params"].get("keys_source") != "hash_sample":
        return None, None
    found = await repo.get_draw(conn, run["run_id"], run["cycle"])
    if found is None:
        return None, None
    try:
        async with conn.transaction():  # a savepoint
            return found, await judge(conn, found, spec or await ctx.spec(conn, run["spec_id"]), ended=True)
    except Exception as exc:
        log.warning("cycle %s of run %s left unjudged: %s", run["cycle"], run["run_id"], type(exc).__name__)
        return found, None


async def judgement(ctx: Context, run_id: str) -> dict[str, Any] | None:
    """The judgement a live run's current cycle would store now, read on a connection of its own (a failing
    run's, before it ends); None when there is none or it cannot be made."""
    try:
        async with ctx.pool.connection() as conn:
            run = await repo.get_run(conn, run_id)
            if run is None or run["status"] in repo.TERMINAL:
                return None
            return (await ending(ctx, conn, run))[1]
    except Exception as exc:
        log.warning("run %s not judged: %s", run_id, type(exc).__name__)
        return None


def judged(
    drawn: Mapping[str, Any], groups: Sequence[Mapping[str, Any]], spec: CompiledSpec, ended: bool = False
) -> dict[str, Any]:
    """A drawn cycle's judgement (§8.1): each sign-off stratum's counters over its members, its exact
    verdict, the bounds shown and why it failed (``ended``: the judgement its end stores, why an unfinished
    one would not pass too); the contrast keys' counters; ``status`` (passed: every stratum, the cycle
    complete; failed: any; else incomplete) and ``weighted_rate``, never a pooled bound.
    ReconciliationError when members and contrast do not make the draw's keys."""
    assert spec.sample is not None
    count = partial(
        counters.count_groups, live=True, recheck_outcomes=RecheckPolicy.from_spec(spec.raw).outcomes
    )
    by_stratum: defaultdict[str | None, list[Mapping[str, Any]]] = defaultdict(list)
    contrast = []
    for g in groups:
        (by_stratum[g["stratum"]] if g["member"] else contrast).append(g)
    times = spec.sample.resolve.times
    strata = [
        _stratum(record, by_stratum.pop(record["label"], []), count, times, ended)
        for record in drawn["strata"]
    ]
    keys = sum(g["n"] for g in groups)
    held = sum(s["counters"]["total"] for s in strata) + sum(g["n"] for g in contrast)
    if (
        by_stratum
        or held != keys
        or keys != drawn["keys"]
        or any(s["counters"]["total"] != s["drawn"] for s in strata)
    ):
        raise counters.ReconciliationError(
            f"sample counts do not reconcile: {held} members and contrast keys of {keys} keys,"
            f" {drawn['keys']} drawn"
        )
    verdicts = {s["verdict"] for s in strata}
    complete = all(g["done"] for g in groups)
    status = "failed" if "failed" in verdicts else "passed" if complete else "incomplete"
    frame = sum(s["frame_keys"] for s in strata)
    weighted = None
    if strata and all(s["n"] for s in strata):
        rate = sum(Fraction(s["frame_keys"], frame) * Fraction(s["x"], s["n"]) for s in strata)
        weighted = format(_SIX.divide(Decimal(rate.numerator), Decimal(rate.denominator)), "f")
    return {
        "draw_id": drawn["draw_id"],
        "status": status,
        "weighted_rate": weighted,
        "strata": strata,
        "contrast": count(contrast),
    }


def _stratum(
    record: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    count: Callable[[list[Mapping[str, Any]]], dict[str, Any]],
    times: int,
    ended: bool,
) -> dict[str, Any]:
    """A sign-off stratum's judgement (§8.2) from the draw's record of it and its members' grouped rows:
    passed once complete within every gate, failed once complete outside one or outside one for certain
    (``why`` says which, as the sign-off's refusals end), else unfinished; ``ended``, the cycle's end, words
    every gate an unfinished stratum does not hold too."""
    target = Fraction(record["target"])
    t, drawn, frame_keys, x_max = percent(target), record["drawn"], record["frame_keys"], record["x_max"]
    c, done = count(rows), count([g for g in rows if g["done"]])
    ab, api = c["absent_both"], c["api_uncomparable"]
    ab_f, api_f = done["absent_both"], done["api_uncomparable"]  # final: a first read's recheck may change
    owed = done["outstanding"]  # done, its one comparable read discrepant: never left out (D3)
    n, x = c["compared"] + owed, c["different"] + owed
    unresolved = c["indeterminate"] - ab - api
    final = done["indeterminate"] - ab_f - api_f  # u_f
    census_ = record["basis"] == "census"
    gates: list[tuple[bool, bool, str]] = []  # (holds, fails for certain, why)
    bound = None
    if census_:  # never the binomial bound: the exact count
        allowed, has = share(target, frame_keys - ab), x + unresolved
        gates.append((
            has <= allowed, x + final > share(target, frame_keys - ab_f),
            f"is over its {t} target: compared whole, its {frame_keys:,} keys allow {allowed:,} discrepant or"
            f" unresolved key(s); it has {has:,}",
        ))  # fmt: skip
    else:
        clean = x <= x_max and passes(x, n, target)  # passes rises with n: over x_max, never
        bound, within = sampling.shown(x, n, target, clean), share(target, n)
        gates.append((
            clean, x > x_max,
            f"is over its {t} target: its one-sided 95% upper bound is {percent(bound)} ({x:,} discrepant of"
            f" {n:,} cleanly compared keys)",
        ))  # fmt: skip
        gates.append((
            unresolved <= within, final > share(target, drawn - final - ab_f),
            f"has {unresolved:,} unresolved key(s) after {times} retries, over the {within:,} its {t} target"
            f" allows (of {n:,} cleanly compared keys)",
        ))  # fmt: skip
    absent = share(target, drawn)
    gates.append((
        ab <= absent, ab_f > absent,
        f"has {ab:,} key(s) absent on both sides, over the {absent:,} its {t} target allows of {drawn:,}"
        " drawn: the frame was stale; draw again",
    ))  # fmt: skip
    sides = " and ".join(f"side {s}'s" for s in sorted({s for g in rows for s in g["truncated"] or ""}))
    gates.append((
        api == 0, api_f > 0,
        f"has {api:,} key(s) above the API cap that were not compared (truncated): declare pagination for"
        f" {sides or 'its'} fetch in a new version and draw again",
    ))  # fmt: skip
    complete = done["total"] == c["total"]
    failed = any(certain or (complete and not holds) for holds, certain, _ in gates)
    why = [text for holds, certain, text in gates if certain or (not holds and (complete or ended))]
    worst, suppressed = unresolved + ab + api, n - c["by_outcome"]["MATCH"]  # reported, never gated
    return {
        name: record[name] for name in ("label", "tier", "target", "basis", "frame_keys", "drawn", "contrast")
    } | {
        "x_max": x_max, "counters": c, "n": n, "x": x, "unresolved": unresolved, "absent_both": ab,
        "api_uncomparable": api, "retried": sum(g["n"] for g in rows if g["retried"]),
        "ub95_one_sided": bound,
        "ub95_one_sided_all_suppressed": None if census_ else _shown(suppressed, n, target, x_max),
        "ub95_one_sided_worst_case": None if census_ else _shown(x + worst, n + worst, target, x_max),
        "verdict": "failed" if failed else "passed" if complete else "unfinished",
        "why": why,
    }  # fmt: skip


def _shown(x: int, n: int, target: Fraction, x_max: int) -> str:
    """The bound shown for x of n, never contradicting ``passes``, which is called only when x ≤ x_max."""
    return sampling.shown(x, n, target, x <= x_max and passes(x, n, target))


# -- the ledger, closes and sign-off (§9, §10, §14) ---------------------------------------------------


def blocking(draw: Mapping[str, Any]) -> bool:
    """Whether a ledger draw needs a close before a later draw of its population is signed off (R-a): an
    ended ``bottom_k`` draw that did not pass and is not closed. A rate draw never blocks (monitoring)."""
    return draw["method"] == "bottom_k" and draw["status"] not in ("live", "passed") and not draw["closed_at"]


def _around(rows: Sequence[repo.Row], draw_id: str) -> tuple[int | None, list[repo.Row], list[repo.Row]]:
    """The chain seq of the draw's ``sample.draw`` row, and the other ``bottom_k`` draws of its ledger
    committed before it and after it: none without that row (P2), and never a draw no row names (which
    ``_unordered`` refuses)."""
    seq = next(d["draw_seq"] for d in rows if d["draw_id"] == draw_id)
    if seq is None:
        return None, [], []
    others = [
        d for d in rows if d["method"] == "bottom_k" and d["draw_id"] != draw_id and d["draw_seq"] is not None
    ]
    return seq, [d for d in others if d["draw_seq"] < seq], [d for d in others if d["draw_seq"] > seq]


def _unordered(rows: Sequence[repo.Row], draw_id: str) -> list[str]:
    """The draw, and the other ``bottom_k`` draws of its ledger, that no committed ``sample.draw`` row
    names (a row altered since): their order in the ledger is unknown, so the sign-off is refused."""
    return [
        f"the sample {d['draw_id']} of cycle {d['cycle']} of run {d['run_id']} does not match its sample.draw"
        " audit row: its order in the ledger is unknown"
        for d in rows
        if d["draw_seq"] is None and (d["draw_id"] == draw_id or d["method"] == "bottom_k")
    ]


def _closed_before(draw: Mapping[str, Any], seq: int) -> bool:
    return draw["close_seq"] is not None and draw["close_seq"] < seq


def _day(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d}"


def _strata(labels: Sequence[str]) -> str:
    return f"stratum {labels[0]}" if len(labels) == 1 else f"strata {', '.join(labels)}"


async def problems(
    conn: repo.Conn,
    run: Mapping[str, Any],
    cycle: int,
    spec: CompiledSpec,
    *,
    later: bool = True,
    selection: bool = True,
) -> list[str]:
    """Why a sampled run's ended ``cycle`` cannot be signed off (§10, G1-G11), from its stored draw, judgement
    and ledger, never recomputed; without ``later``, not R-c (G10), which a pack reads from the final grant;
    without ``selection``, not R-d, which readiness checks once as it measures the cycle
    (docs/mvp/READINESS.md §5). Empty for a run without a hash sample."""
    if run["params"].get("keys_source") != "hash_sample":
        return []
    run_id = run["run_id"]
    at = f"cycle {cycle} of run {run_id}"
    drawn = await repo.get_draw(conn, run_id, cycle)
    if drawn is None:
        return [f"{at} has no draw of its own: its sample was never drawn"]
    draw_id, found = drawn["draw_id"], []
    if drawn["method"] != "bottom_k":
        found.append(
            f"the sample {draw_id} of {at} was drawn by rate: sign-off needs bottom_k sizes fixed in the spec"
        )
    if selection and not await reselected(conn, drawn):
        found.append(
            f"the keys of {at} are not draw {draw_id}'s selection: a sample is never extended, trimmed or"
            " relabelled"
        )
    ended = await repo.get_cycle(conn, run_id, cycle) or {}
    counted = ended.get("census") or {}
    assert spec.sample is not None
    for i, member in enumerate(spec.sample.members):
        if unfit := (counted.get(member.side.value) or {}).get("unfit"):
            found.append(
                f"the frame of {draw_id} holds {unfit:,} key(s) outside key.pattern or the store's limits"
                f" (source {i}): they could not be sampled; correct key.pattern in a new version and draw"
                " again"
            )
    found += sampling.unpassed(ended.get("sample"), draw_id, at)  # R-b
    rows = await repo.ledger(conn, drawn["population"])
    found += _unordered(rows, draw_id)
    seq, earlier, after = _around(rows, draw_id)
    found += [  # R-a (G9)
        f"draw {d['draw_id']} (spec {d['spec_name']}, run {d['run_id']} cycle {d['cycle']})"
        f" {_UNPASSED[d['status']]} before draw {draw_id} was made and was not closed before it: close it"
        " with the fix, then sign off a draw made after the close"
        for d in earlier
        if d["status"] != "passed" and not _closed_before(d, seq)
    ]
    if later:
        found += [why for _, why in await _later(conn, after, draw_id) if why is not None]
    return found


async def reselected(conn: repo.Conn, drawn: Mapping[str, Any]) -> bool:
    """R-d: whether the stored keys of the draw's cycle re-hash to its selection, streamed: a million lines
    are never held."""
    selected = hashlib.sha256()
    async for fields in repo.selection_lines(conn, drawn["run_id"], drawn["cycle"]):
        selected.update(sampling.line(*fields))
    return selected.hexdigest() == drawn["selection_sha256"]


async def _later(
    conn: repo.Conn, after: Sequence[repo.Row], draw_id: str
) -> list[tuple[dict[str, Any], str | None]]:
    """R-c over the ledger draws committed after ``draw_id``: each as judged (``{draw_id, status}``) and
    the G10 refusal it gives, or None. A failed or unjudged one refuses, and a live one with a stratum
    failed for certain (§8.2); later passes and incomplete or live draws never block."""
    judged = []
    for d in after:
        status, what = d["status"], None
        if status == "failed":
            what = f"failed {_strata(d['failed_strata'])} on {_day(d['ended_at'])}"
        elif status == "unjudged":
            what = f"ended on {_day(d['ended_at'])} without a judgement"
        elif status == "live":
            stored = await repo.get_spec(conn, d["spec_id"])
            now = await judge(conn, d, compile_spec(stored["body"]))  # type: ignore[index]
            if certain := [s["label"] for s in now["strata"] if s["verdict"] == "failed"]:
                what = f"has failed {_strata(certain)} for certain"
        why = what and (
            f"draw {d['draw_id']} (spec {d['spec_name']}, run {d['run_id']} cycle {d['cycle']}), made after"
            f" draw {draw_id}, {what}: fix it, close it and sign off a draw made after the close"
        )
        judged.append(({"draw_id": d["draw_id"], "status": status}, why))
    return judged


async def after(conn: repo.Conn, population: Sequence[str], draw_id: str) -> list[repo.Row]:
    """The other ``bottom_k`` draws of the population's ledger committed after ``draw_id``, as it lists
    them now: the later draws an evidence pack names at its issue (§11.2)."""
    return _around(await repo.ledger(conn, population), draw_id)[2]


async def later_draws(conn: repo.Conn, run_id: str, cycle: int) -> list[dict[str, Any]]:
    """The ledger draws committed after the cycle's draw, as R-c judges them now (``{draw_id, status}``):
    what a sampled sign-off's final approval step records in its ``approval.grant`` row (§9)."""
    drawn = await repo.get_draw(conn, run_id, cycle)
    assert drawn is not None
    _, _, after = _around(await repo.ledger(conn, drawn["population"]), drawn["draw_id"])
    return [entry for entry, _ in await _later(conn, after, drawn["draw_id"])]


def _decimal(value: Fraction) -> str:
    return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


async def attested(
    conn: repo.Conn, run: Mapping[str, Any], cycle: int, spec: CompiledSpec
) -> dict[str, Any] | None:
    """What attests format 2 adds for a sampled cycle (§10): its draw (salt label, nonce, the salt's SHA-256,
    its ``sample.draw`` chain row, population, frame, strata and selection), the build's tiers, the ledger
    draws before it as R-a judged them (a close shown only if it preceded the draw), and its stored
    judgement; None for a cycle without a draw. Chain order freezes each fact: later draws, closes and
    purges change none."""
    drawn = None
    if run["params"].get("keys_source") == "hash_sample":
        drawn = await repo.get_draw(conn, run["run_id"], cycle)
    if drawn is None or spec.sample is None:
        return None
    seq, earlier, _ = _around(await repo.ledger(conn, drawn["population"]), drawn["draw_id"])
    ended = await repo.get_cycle(conn, run["run_id"], cycle) or {}
    resolve = spec.sample.resolve
    ledger = []
    for d in earlier:
        closed = _closed_before(d, seq)
        ledger.append(
            {name: d[name] for name in ("draw_id", "spec_name", "run_id", "cycle", "draw_seq", "status")}
            | {"ended_at": d["ended_at"] and utc_iso(d["ended_at"]), "failed_strata": d["failed_strata"]}
            | {name: d[name] if closed else None for name in ("closed_by", "close_seq", "close_note")}
        )
    return {
        name: drawn[name] for name in ("draw_id", "method", "salt_label", "nonce", "population")
    } | {
        "salt_sha256": sha256_hex(sampling.salt(drawn["salt_label"], drawn["nonce"])),
        "drawn_at": utc_iso(drawn["drawn_at"]), "draw_seq": seq,
        "targets": {tier: _decimal(t) for tier, t in sampling.TARGETS.items()},
        "confidence": _decimal(sampling.CONFIDENCE), "sided": "one-sided upper",
        "margin": _decimal(sampling.MARGIN), "resolve": {"times": resolve.times, "delay_s": resolve.delay_s},
        "frame": drawn["frame"], "strata": drawn["strata"], "selection_sha256": drawn["selection_sha256"],
        "ledger": ledger, "judgement": ended.get("sample"),
    }  # fmt: skip


async def progress(conn: repo.Conn, drawn: Mapping[str, Any], spec: CompiledSpec) -> dict[str, Any]:
    """A live draw's progress (§14): the judgement its cycle would store now (one grouped query, through
    ``judged``'s verdict rules: a stratum is failed once certain, its bound interim), each stratum's members
    done and retrying, and an ETA paced by the keys finished in the last ``ETA_WINDOW`` seconds."""
    run_id, cycle = drawn["run_id"], drawn["cycle"]
    groups = await repo.stratum_counts(conn, run_id, cycle)
    now = await asyncio.to_thread(judged, drawn, groups, spec)
    for s in now["strata"]:
        rows = [g for g in groups if g["member"] and g["stratum"] == s["label"]]
        s["done"] = sum(g["n"] for g in rows if g["done"])
        s["retrying"] = sum(g["n"] for g in rows if g["retried"] and not g["done"])
    done = sum(g["n"] for g in groups if g["done"])
    recent = await repo.finished_since(conn, run_id, cycle, ETA_WINDOW)
    left = drawn["keys"] - done
    eta = round(left * ETA_WINDOW / recent) if recent and left else None
    return now | {"done": done, "finished_recently": recent, "eta_seconds": eta}


async def close(conn: repo.Conn, audit: repo.Audit, spec_name: str, note: str, who: Principal) -> list[str]:
    """Close, as the person ``who`` with ``note`` (what was fixed), every ended ``bottom_k`` draw of the
    name's ledger (its newest version's population) that did not pass and is not closed, whatever spec
    drew it; each audited ``sample.close``. The draws closed. LookupError for an unknown name (C3);
    SampleRefused for nothing to close (C4) or a note shaped like a key of a spec whose later sign-offs
    could attest it (C5): the name's newest version, each ledger name's and each closed draw's."""
    stored = await repo.newest_spec(conn, spec_name)
    if stored is None:
        raise LookupError(f"spec {spec_name} not found")
    found = compile_spec(stored["body"]).sample
    rows = await repo.ledger(conn, found.population) if found else []
    due = [d for d in rows if blocking(d)]
    if due:  # checked as revoke reasons are, against each spec whose later sign-offs could attest it
        sharing = {d["spec_name"] for d in rows} | await _sharing(conn, found.population)  # type: ignore[union-attr]
        names = sorted(sharing - {spec_name})
        versions = [stored, *[await repo.newest_spec(conn, name) for name in names]]
        versions += [await repo.get_spec(conn, spec_id) for spec_id in sorted({d["spec_id"] for d in due})]
        if any(rawvalues.key_shaped(compile_spec(v["body"]), note) for v in versions):  # type: ignore[index]
            raise SampleRefused(422, NOTE_KEY_SHAPED)
    closed = await repo.close_draws(conn, [d["draw_id"] for d in due], who.actor, note) if due else []
    if not closed:
        raise SampleRefused(
            409,
            f"the population of {spec_name} has no draw to close (every ended bottom_k draw passed or is"
            " closed)",
        )
    ids = sorted(row["draw_id"] for row in closed)  # draw ids sort by age
    for draw_id in ids:
        await audit(who, "sample.close", draw_id, {"spec_name": spec_name, "note": note})
    return ids


async def _sharing(conn: repo.Conn, population: Sequence[str]) -> set[str]:
    """The stored spec names whose newest version samples a population overlapping ``population``, drawn
    or not: each could later sign off a draw whose ledger attests a close note (§9)."""
    names, shared = {row["name"] for row in await repo.list_specs(conn)}, set()
    for name in names:
        stored = await repo.newest_spec(conn, name)
        try:
            sample = compile_spec(stored["body"]).sample  # type: ignore[index]
        except SpecError:  # a version that no longer compiles signs nothing off
            continue
        if sample is not None and set(sample.population) & set(population):
            shared.add(name)
    return shared


def lines(judgement: Mapping[str, Any]) -> list[str]:
    """A judgement's strata as ``ds run`` prints them (§14)."""
    return [
        f"stratum {s['label']} ({s['tier']}, ≤ {percent(s['target'])}): {s['drawn']:,} drawn of"
        f" {s['frame_keys']:,}, {s['n']:,} compared, {s['x']:,} discrepant, {s['unresolved']:,} unresolved,"
        f" {'compared whole' if s['ub95_one_sided'] is None else 'bound ' + percent(s['ub95_one_sided'])}:"
        f" {s['verdict']}"
        for s in judgement["strata"]
    ]
