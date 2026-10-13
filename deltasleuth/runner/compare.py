"""One pass over one key: read both sides, keep the evidence first, observe, compare.

Reads come from the live endpoints (one connector per side, under its target's gate of the
process: a service's REST connector, or a table's per-key reads by the run's column plan) or from
captured samples (file mode). Every read is stored as evidence before it is
judged, failed calls included. A key that cannot be read is INCONCLUSIVE, never a failure of the
run: no sample, or a key that cannot be sent, is ``skipped``; a malformed sample is
``unparseable``. Observing and comparing run off the event loop, so a heavy spec map never stalls
the API or the other keys. A run is observed with the capabilities its targets declared when it
was created (``Context.capabilities``). Stored observations are opened with the run's sealer, and
their headers sealed with it when saved (docs/mvp/RETENTION.md §3). A side that reads fixed files is read
once: a recheck keeps its first observation of that side, after one stat check of the files, and reads
the other side alone (docs/mvp/FILES.md §12).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Container, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from deltasleuth.connectors.base import Connector, KeyRejectedError, NotCapturedError
from deltasleuth.connectors.files import FileConnector, FileSampleSource, SampleError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.rest import RestConnector
from deltasleuth.connectors.tables import Plan, TableConnector
from deltasleuth.context import Capabilities, Context
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.spec import SecretError
from deltasleuth.core.spec.keys import fit_parts
from deltasleuth.core.types import (
    CompiledSpec,
    ExecutionConfig,
    KeyResult,
    Observation,
    Side,
    SideState,
    SideStateKind,
)
from deltasleuth.evidence.store import EvidenceNotFoundError
from deltasleuth.runner.recheck import tag_intermittent
from deltasleuth.security.crypto import Sealer
from deltasleuth.security.tokenise import mask_finding, mask_key
from deltasleuth.store import repo

Reads = dict[Side, Observation]
# What a recheck does not read again (docs/mvp/FILES.md §12): a side of fixed files' first observation, by
# its id, and the state it was observed as.
Held = dict[Side, tuple[str, SideState]]


class EvidenceMissing(Exception):
    """A stored observation's blob is gone, purged or lost: its reads answer 410
    (docs/mvp/RETENTION.md §4.4)."""


def body_of(ctx: Context, obs: Observation) -> bytes | None:
    """A stored observation's body from the evidence store; None when it had none."""
    if obs.content_hash is None:
        return None
    try:
        return ctx.evidence.get_body(obs.content_hash)
    except EvidenceNotFoundError:
        raise EvidenceMissing(f"evidence of {obs.obs_id} is missing (purged or lost)") from None


def connect(
    ctx: Context,
    target_id: str,
    execution: ExecutionConfig,
    table: Plan | None = None,
    *,
    max_body_bytes: int = jsonio.DEFAULT_MAX_BYTES,
) -> Connector:
    """A live connector to the target, under the process's one gate for it (``ctx.gates``: the rate
    budget and slots every run on the target shares) and that gate's breaker for the ``circuitBreaker``
    that ``execution`` declares; a database target reads the table side ``table`` of a run's column plan.
    ``max_body_bytes`` caps a service's answer, its pages together (a trial of an endpoint reads less than
    a run does)."""
    target = ctx.targets.live(target_id)
    gate = ctx.gates.setdefault(target_id, TargetGate.for_target(target))
    if target.engine:
        return TableConnector(target, execution, table or {}, gate=gate)
    try:
        return RestConnector(target, execution, gate=gate, max_body_bytes=max_body_bytes)
    except SecretError:
        if target.declared_in != "product":
            raise  # an operator's own mount: the refusal names the file, never its content
        # Stored by the product (``deltasleuth.targets``): no answer, log line or audit detail names its file.
        unread = f"the stored credential of target {target_id} cannot be read: enter it again"
        raise SecretError(unread) from None


class LiveReader:
    """The spec's two sides, live; ``tables`` is the run's column plan (``params["tables"]``)."""

    def __init__(self, ctx: Context, spec: CompiledSpec, tables: Plan | None = None) -> None:
        self._ctx, self._sides, self._execution = ctx, spec.sides, spec.execution
        self._tables = tables or {}
        self._connectors: dict[Side, Connector] = {}
        self._replaced: list[Connector] = []
        self.reconnect()

    def reconnect(self) -> None:
        """Connect both sides, reading the targets' credentials (again, once a refused one was
        replaced); reads in flight finish on the connectors replaced, closed with the reader."""
        fresh = {
            side: connect(self._ctx, cfg.target, self._execution, self._tables.get(side.value))
            for side, cfg in self._sides.items()
        }
        self._replaced += self._connectors.values()
        self._connectors = fresh

    @property
    def batches(self) -> bool:
        """Whether a slot reads a group of keys (``prefetch``): every side is a table, or a table side
        reads fixed files, each read of which is a pass over them whatever its keys (docs/mvp/FILES.md §12);
        a service side of such a pair then reads the group's keys one by one."""
        tables = [c for c in self._connectors.values() if isinstance(c, TableConnector)]
        return len(tables) == len(self._connectors) or any(c.fixed for c in tables)

    @property
    def fixed(self) -> frozenset[Side]:
        """The sides that read fixed files: what a read of one observed stands (docs/mvp/FILES.md §12)."""
        return frozenset(
            side for side, c in self._connectors.items() if isinstance(c, TableConnector) and c.fixed
        )

    async def standing(self, firsts: Mapping[str, KeyResult]) -> dict[str, Held]:
        """What a recheck of each key (``firsts``: its stored first result) does not read again
        (docs/mvp/FILES.md §12): a side of fixed files keeps the key's first observation of it, after one
        stat check of its files for the group. A first read that failed observed no bytes, and files that
        do not answer the check keep nothing: those are read."""
        held: dict[str, Held] = {key: {} for key in firsts}
        for side, connector in self._connectors.items():
            if not (isinstance(connector, TableConnector) and connector.fixed):
                continue
            seen = {key: kept for key, first in firsts.items() if (kept := _observed(first, side))}
            if seen and await connector.unchanged(self._sides[side]):
                for key, kept in seen.items():
                    held[key][side] = kept
        return held

    @property
    def large(self) -> bool:
        """Whether a table side's rows may hold large values (ENGINES.md §6): its keys are read few at a
        time."""
        return any(isinstance(c, TableConnector) and c.large for c in self._connectors.values())

    async def ahead(self, keys: Sequence[str]) -> None:
        """Read ``keys`` on each side of fixed files, one pass over them a side (docs/mvp/FILES.md §12), for a
        group whose other side is then read few keys at a time: ``prefetch`` is told of them."""
        for side in self.fixed:
            connector = self._connectors[side]
            assert isinstance(connector, TableConnector)
            connector.forget(keys)
            await connector.prefetch(keys, self._sides[side])

    async def prefetch(
        self,
        batches: Sequence[tuple[Sequence[str], Sequence[Side]]],
        held: Mapping[str, Held] | None = None,
        ahead: Sequence[str] = (),
    ) -> None:
        """Read each batch of keys on each table side, in its sides' order, in one statement a side
        (TABLES.md §12), but the sides ``held`` keeps of a key (``standing``) and the sides of fixed files
        that ``ahead`` read of its keys; what an earlier group left unread of these keys is dropped first,
        never served."""
        tables = {side: c for side, c in self._connectors.items() if isinstance(c, TableConnector)}
        before = {(key, side) for side in (self.fixed if ahead else ()) for key in ahead}
        for keys, _ in batches:  # groups of one run share the connectors, and lease distinct keys
            for side, connector in tables.items():
                connector.forget([key for key in keys if (key, side) not in before])
        for keys, sides in batches:
            for side in [s for s in sides if s in tables]:
                kept = before | {(key, side) for key in keys if side in (held or {}).get(key, {})}
                if read := [key for key in keys if (key, side) not in kept]:
                    await tables[side].prefetch(read, self._sides[side])

    async def fetch(self, key: str, side: Side, *, run_id: str, call_seq: int) -> Observation:
        return await self._connectors[side].fetch(key, self._sides[side], run_id=run_id, call_seq=call_seq)

    async def aclose(self) -> None:
        for connector in [*self._replaced, *self._connectors.values()]:
            await connector.aclose()


class FileReader:
    """Captured samples (layouts 1 and 2), found by normalised key: files may use the raw spelling. A
    sample whose name is not a multi-part key's parts joined by ``|`` is no key's, and is left out."""

    def __init__(self, samples_dir: str | Path, spec: CompiledSpec) -> None:
        try:
            source = FileSampleSource(samples_dir)
        except SampleError as exc:
            raise ValueError(str(exc)) from None
        chain, n = [step.name for step in spec.key.normalize], len(spec.key_parts) or 1
        self._raw: dict[str, str] = {}
        for raw in source.keys():
            key = fit_parts(raw, chain, n)
            if key is not None and self._raw.setdefault(key, raw) != raw:
                raise ValueError("two captured sample keys normalise to the same key (key.normalize)")
        self._connector = FileConnector(source)
        self._sides = spec.sides
        self.fixed: frozenset[Side] = frozenset()  # captured samples: no side reads a table

    async def fetch(self, key: str, side: Side, *, run_id: str, call_seq: int) -> Observation:
        raw = self._raw.get(key)
        if raw is None:
            raise NotCapturedError("no sample captured for this key")
        obs = await self._connector.fetch(raw, self._sides[side])
        return obs if raw == key else dataclasses.replace(obs, key=key)

    async def aclose(self) -> None:
        return None


Reader = LiveReader | FileReader


def _observed(first: KeyResult, side: Side) -> tuple[str, SideState] | None:
    """A stored result's observation of ``side`` and its state; None without one, or when its read failed."""
    obs_id, state = (first.obs_a_id, first.side_a) if side is Side.A else (first.obs_b_id, first.side_b)
    return None if obs_id is None or state.kind is SideStateKind.ERROR else (obs_id, state)


def _observe(spec: CompiledSpec, caps: Capabilities, obs: Observation) -> SideState:
    side = spec.sides[obs.side]
    return observe(obs, side, spec, caps[side.target])


def _compare(ctx: Context, spec: CompiledSpec, key: str, a: SideState, b: SideState) -> KeyResult:
    return compare_pair(key, a, b, spec, today=datetime.now(UTC).date(), tokeniser=ctx.tokeniser)


async def evaluate(
    ctx: Context,
    spec: CompiledSpec,
    caps: Capabilities,
    reader: Reader,
    key: str,
    *,
    run_id: str,
    pass_no: int,
    held: Held | None = None,
) -> tuple[KeyResult, Reads]:
    """Read both sides and compare them: A then B on the first pass, B then A on a recheck (§4.5),
    so a B that is catching up gets the most time; a recheck reads no side ``held`` keeps."""
    kept = {key: held} if held else None
    return (await evaluate_many(ctx, spec, caps, reader, [(key, pass_no)], run_id=run_id, held=kept))[0]


async def evaluate_many(
    ctx: Context,
    spec: CompiledSpec,
    caps: Capabilities,
    reader: Reader,
    keys: Sequence[tuple[str, int]],
    *,
    run_id: str,
    held: Mapping[str, Held] | None = None,
) -> list[tuple[KeyResult, Reads]]:
    """``evaluate`` for each (key, pass_no): the reads first (a prefetched table side answers at
    once), then the evidence and the judgement of them all in one thread call. A recheck's key keeps the
    sides ``held`` holds of it (``LiveReader.standing``): they are not read, and its result names the same
    observation. The reads end at the first refused credential, which is not tried on the group's other
    keys: the results are then of the keys read, and their caller hands the group back."""
    fetched = []
    for key, pass_no in keys:
        kept = (held or {}).get(key, {})
        fetched.append((*await _fetch(reader, key, run_id=run_id, pass_no=pass_no, held=kept), kept))
        if any(obs.error_class == "auth_error" for obs in fetched[-1][0].values()):
            break
    return await asyncio.to_thread(
        lambda: [_judge(ctx, spec, caps, key, *f) for (key, _), f in zip(keys, fetched, strict=False)]
    )


async def _fetch(
    reader: Reader, key: str, *, run_id: str, pass_no: int, held: Held
) -> tuple[dict[Side, Observation], dict[Side, SideState]]:
    """Each side's observation, the state of a side that could not be read, or the one ``held`` keeps."""
    observations: dict[Side, Observation] = {}
    states: dict[Side, SideState] = {side: state for side, (_, state) in held.items()}
    for i, side in enumerate((Side.A, Side.B) if pass_no == 1 else (Side.B, Side.A)):
        if side in held:
            continue
        try:
            observations[side] = await reader.fetch(key, side, run_id=run_id, call_seq=2 * (pass_no - 1) + i)
        except NotCapturedError:
            states[side] = SideState.incomplete("skipped", f"no sample captured on side {side.value}")
        except KeyRejectedError:
            states[side] = SideState.incomplete("skipped", f"the key cannot be sent to side {side.value}")
        except SampleError:  # its message names the sample file, i.e. the key
            states[side] = SideState.incomplete(
                "unparseable", f"the captured sample on side {side.value} is malformed"
            )
    return observations, states


def _judge(
    ctx: Context,
    spec: CompiledSpec,
    caps: Capabilities,
    key: str,
    observations: dict[Side, Observation],
    states: dict[Side, SideState],
    held: Held,
) -> tuple[KeyResult, Reads]:
    """Keep the evidence first, then observe and compare (off the event loop: a heavy map must not
    stall the API or the other keys); a side ``held`` keeps is named by its stored observation."""
    reads = {side: ctx.evidence.put_blob(obs) for side, obs in observations.items()}
    states = states | {side: _observe(spec, caps, obs) for side, obs in reads.items()}
    result = _compare(ctx, spec, key, states[Side.A], states[Side.B])
    ids = {side: obs.obs_id for side, obs in reads.items()} | {side: kept[0] for side, kept in held.items()}
    result.obs_a_id, result.obs_b_id = (ids.get(s) for s in Side)
    return result, reads


async def side_state(
    ctx: Context, conn: repo.Conn, spec: CompiledSpec, run_id: str, obs_id: str | None, caps: Capabilities
) -> tuple[Observation, SideState] | None:
    """A stored observation, with its body from the evidence, observed again with the run's
    ``caps`` (``ctx.capabilities(run)``); None without one."""
    row = await repo.get_observation(conn, run_id, obs_id) if obs_id else None
    if row is None:
        return None
    obs = repo.as_observation((await ctx.sealer(conn, run_id)).open_row(row))
    return await asyncio.to_thread(_stored, ctx, spec, caps, obs)


def _stored(
    ctx: Context, spec: CompiledSpec, caps: Capabilities, obs: Observation
) -> tuple[Observation, SideState]:
    if obs.content_hash is not None:
        obs = dataclasses.replace(obs, body=body_of(ctx, obs))
    return obs, _observe(spec, caps, obs)


async def rederive_all(
    ctx: Context,
    conn: repo.Conn,
    spec: CompiledSpec,
    run_id: str,
    rows: Sequence[Mapping],
    caps: Capabilities,
    intermittent: Container[str] = (),
) -> list[KeyResult]:
    """Each stored result's comparison again, from its observations and their evidence: the
    observations are read in one query and judged in one thread call. The keys (masked) in
    ``intermittent`` had their stored findings tagged by a recheck (``confirm``), so they are tagged
    again."""
    if not rows:
        return []
    ids = [row[f"obs_{s}_id"] for row in rows for s in "ab" if row[f"obs_{s}_id"]]
    sealer = await ctx.sealer(conn, run_id)
    found = await repo.get_observations(conn, run_id, ids)
    observations = {obs_id: repo.as_observation(sealer.open_row(row)) for obs_id, row in found.items()}

    def judged(row: Mapping) -> KeyResult:
        stored = [_stored(ctx, spec, caps, observations[row[f"obs_{s}_id"]]) for s in "ab"]
        result = _compare(ctx, spec, stored[0][0].key, stored[0][1], stored[1][1])
        result.obs_a_id, result.obs_b_id = row["obs_a_id"], row["obs_b_id"]
        if row["key_masked"] in intermittent:
            result.findings = tag_intermittent(result.findings, spec)
        return result

    return await asyncio.to_thread(lambda: [judged(row) for row in rows])


async def save_pass(
    ctx: Context,
    conn: repo.Conn,
    spec: CompiledSpec,
    run_id: str,
    cycle: int,
    pass_no: int,
    result: KeyResult,
    reads: Reads,
    sealer: Sealer,
) -> None:
    """The pass's observations, then the key's result and masked findings."""
    await save_passes(ctx, conn, spec, run_id, cycle, [(pass_no, result, reads)], sealer)


async def save_passes(
    ctx: Context,
    conn: repo.Conn,
    spec: CompiledSpec,
    run_id: str,
    cycle: int,
    passes: Sequence[tuple[int, KeyResult, Reads]],
    sealer: Sealer,
) -> None:
    """``save_pass`` for each (pass_no, result, reads), with one statement per table, in the caller's
    transaction. Each key is saved by its masked form (the foreign keys refuse a mismatch) and each
    observation's headers sealed by the run's ``sealer``; a first pass is a fresh read, a sample's retry
    replacing a stored result (docs/mvp/SAMPLING.md §7). The blob lock, shared, is taken and every blob
    ensured first, so a drain cannot delete a blob these rows reference (docs/mvp/RETENTION.md §5.4)."""
    keys = [mask_key(result.key, spec, ctx.tokeniser) for _, result, _ in passes]
    observations = [
        (pass_no, masked, obs, _sealed_headers(sealer, cycle, obs))
        for (pass_no, _, reads), masked in zip(passes, keys, strict=True)
        for obs in reads.values()
    ]
    await repo.lock_blobs(conn)
    await asyncio.to_thread(ctx.evidence.ensure, [obs for _, _, obs, _ in observations])
    await repo.insert_observations(conn, run_id, cycle, observations)
    masked = [
        (key, result, [mask_finding(f, spec, ctx.tokeniser) for f in result.findings])
        for (_, result, _), key in zip(passes, keys, strict=True)
    ]
    fresh = {key for (pass_no, _, _), key in zip(passes, keys, strict=True) if pass_no == 1}
    await repo.save_key_results(conn, run_id, cycle, masked, fresh)


def _sealed_headers(sealer: Sealer, cycle: int, obs: Observation) -> bytes:
    headers = jsonio.dumps(repo.storable(obs.headers)).encode()
    return sealer.seal("headers", cycle, obs.obs_id, headers)  # type: ignore[arg-type]  # put_blob set it
