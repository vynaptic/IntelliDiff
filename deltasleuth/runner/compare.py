"""One pass over one key: read both sides, observe, compare, and keep the evidence before the rows.

Reads come from the live endpoints (one connector per side, under its target's gate of the
process: a service's REST connector, or a table's per-key reads by the run's column plan) or from
captured samples (file mode). Every read, a failed call included, is named as evidence when it is judged
and stored before its observation is saved: a group's blobs are durable before the transaction that names
them commits, each directory synced once a group. A key that cannot be read is INCONCLUSIVE, never a
failure of the run: no sample, or a key that cannot be sent, is ``skipped``; a malformed sample is
``unparseable``. Observing and comparing run off the event loop, in a thread or in the worker's judge
processes (``runner.judges``), so a heavy spec map never stalls the API or the other keys. A run is
observed with the capabilities its targets declared when it was created (``Context.capabilities``).
Stored observations are opened with the run's sealer, and their headers sealed with it when saved. A side
that reads fixed files is read once: a recheck keeps its first observation of that side, after one stat
check of the files, and reads the other side alone.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Container, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from deltasleuth import targets as product
from deltasleuth.connectors.base import BatchConnector, Connector, KeyRejectedError, NotCapturedError
from deltasleuth.connectors.files import FileConnector, FileSampleSource, SampleError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.rest import RestConnector
from deltasleuth.connectors.tables import Plan, TableConnector
from deltasleuth.context import Capabilities, Context
from deltasleuth.core import jsonio
from deltasleuth.core.judge import compared, observe_one, observed
from deltasleuth.core.spec import SecretError
from deltasleuth.core.spec.keys import fit_parts
from deltasleuth.core.types import (
    CompiledSpec,
    ExecutionConfig,
    KeyResult,
    Observation,
    Outcome,
    RuleClassification,
    Side,
    SideState,
    SideStateKind,
    Target,
    TargetCapabilities,
)
from deltasleuth.errors import Invalid
from deltasleuth.evidence.store import EvidenceNotFoundError
from deltasleuth.runner.judges import ENDED_DETAIL, Ended, Item, Judged, Judges
from deltasleuth.runner.recheck import tag_intermittent
from deltasleuth.security.crypto import Sealer
from deltasleuth.security.tokenise import KeyForm, mask_finding, mask_key, mask_pointer
from deltasleuth.store import repo

Reads = dict[Side, Observation]
# What a recheck does not read again: a side of fixed files' first observation, by
# its id, and the state it was observed as.
Held = dict[Side, tuple[str, SideState]]


class EvidenceMissing(Exception):
    """A stored observation's blob is gone, purged or lost: its reads answer 410."""


def body_of(ctx: Context, obs: Observation) -> bytes | None:
    """A stored observation's body from the evidence store; None when it had none."""
    if obs.content_hash is None:
        return None
    try:
        return ctx.evidence.get_body(obs.content_hash)
    except EvidenceNotFoundError:
        raise EvidenceMissing(f"evidence of {obs.obs_id} is missing (purged or lost)") from None


def _service(target: Target, execution: ExecutionConfig, gate: TargetGate, **limits: Any) -> Connector:
    try:
        return RestConnector(
            target,
            execution,
            gate=gate,
            max_body_bytes=limits["max_body_bytes"],
            max_values=limits["max_values"],
        )
    except SecretError:
        if target.declared_in != "product":
            raise  # an operator's own mount: the refusal names the file, never its content
        # Stored by the product (``deltasleuth.targets``): no answer, log line or audit detail names its file.
        unread = f"the stored credential of target {target.target_id} cannot be read: enter it again"
        raise SecretError(unread) from None


def _table(target: Target, execution: ExecutionConfig, gate: TargetGate, **limits: Any) -> Connector:
    return TableConnector(target, execution, limits["table"] or {}, gate=gate)


# The connector of each kind of target (``Target.kind``), made of the target, the run's execution, the
# target's gate and ``limits``: ``table`` (the side's column plan, or None), ``max_body_bytes`` and
# ``max_values``. Another kind of source is one more entry, whose connector may be a ``BatchConnector``.
CONNECTORS: dict[str, Callable[..., Connector]] = {"service": _service, "database": _table, "files": _table}


def connect(
    ctx: Context,
    target_id: str,
    execution: ExecutionConfig,
    table: Plan | None = None,
    *,
    capabilities: TargetCapabilities | None = None,
    max_body_bytes: int = jsonio.DEFAULT_MAX_BYTES,
    max_values: int | None = None,
) -> Connector:
    """A live connector to the target, of its kind (``CONNECTORS``), under the process's one gate for it
    (``ctx.gates``: the rate budget and slots every run on the target shares) and that gate's breaker for
    the ``circuitBreaker`` that ``execution`` declares; a database or files target reads the table side
    ``table`` of a run's column plan. ``max_body_bytes`` caps a service's answer, its pages together (a
    trial of an endpoint reads less than a run does), and ``max_values`` the values it holds (a key listing
    holds more). A change another process made to the product's list of targets is taken in first."""
    product.follow(ctx)
    target = ctx.targets.live(target_id)
    if capabilities is not None:
        target = dataclasses.replace(target, capabilities=capabilities)
    gate = ctx.gates.setdefault(target_id, TargetGate.for_target(target))
    limits = {"table": table, "max_body_bytes": max_body_bytes, "max_values": max_values}
    return CONNECTORS[target.kind](target, execution, gate, **limits)


class Reader(Protocol):
    """A run's reads of its two sides, live (``LiveReader``) or captured (``FileReader``), as the worker
    asks for them. ``batches``: a slot reads a group of keys (``prefetch``); ``large``: a side's reads may
    hold large values; ``fixed``: the sides that read fixed files, whose first observation stands."""

    @property
    def fixed(self) -> frozenset[Side]: ...

    @property
    def batches(self) -> bool: ...

    @property
    def large(self) -> bool: ...

    def reconnect(self) -> None: ...

    def refused(self, reads: Mapping[Side, Observation]) -> float | None: ...

    async def standing(self, firsts: Mapping[str, KeyResult]) -> dict[str, Held]: ...

    async def ahead(self, keys: Sequence[str]) -> None: ...

    async def prefetch(
        self,
        batches: Sequence[tuple[Sequence[str], Sequence[Side]]],
        held: Mapping[str, Held] | None = None,
        ahead: Sequence[str] = (),
    ) -> None: ...

    async def fetch(self, key: str, side: Side, *, run_id: str, call_seq: int) -> Observation: ...

    async def aclose(self) -> None: ...


class LiveReader:
    """The spec's two sides, live; ``tables`` is the run's column plan (``params["tables"]``). A side whose
    connector is a ``BatchConnector`` reads groups of keys; any other reads its keys one by one."""

    def __init__(
        self, ctx: Context, spec: CompiledSpec, tables: Plan | None = None, *,
        caps: Capabilities | None = None,
    ) -> None:
        self._ctx, self._sides, self._execution = ctx, spec.sides, spec.execution
        self._tables = tables or {}
        self._caps = caps or {}
        self._connectors: dict[Side, Connector] = {}
        self._replaced: list[Connector] = []
        self.reconnect()

    def reconnect(self) -> None:
        """Connect both sides, reading the targets' credentials (again, once a refused one was
        replaced); reads in flight finish on the connectors replaced, closed with the reader."""
        fresh = {
            side: connect(
                self._ctx, cfg.target, self._execution, self._tables.get(side.value),
                capabilities=self._caps.get(cfg.target),
            )
            for side, cfg in self._sides.items()
        }
        self._replaced += self._connectors.values()
        self._connectors = fresh
        self._batched = {side: c for side, c in fresh.items() if isinstance(c, BatchConnector)}
        self._gates = {side: self._ctx.gates[cfg.target] for side, cfg in self._sides.items()}

    def refused(self, reads: Mapping[Side, Observation]) -> float | None:
        """Seconds until each side of ``reads`` whose read was refused unsent (``attempts`` 0: its target's
        breaker was open, or the target asked callers to wait past the call's timeout) may be read again:
        the longest of their breakers' cooldowns and their targets' pauses; None when no read was refused
        so. Such a read took no time and observed nothing of the key."""
        gates = [self._gates[side] for side, obs in reads.items() if obs.attempts == 0]
        if not gates:
            return None
        return max(max(g.paused_for(), g.breaker(self._execution).closed_for()) for g in gates)

    @property
    def batches(self) -> bool:
        """Whether a slot reads a group of keys (``prefetch``): every side reads groups, or a side reads
        fixed files, each read of which is a pass over them whatever its keys; a side that reads keys one by
        one (a service's) then reads the group's keys one by one."""
        batched = self._batched.values()
        return len(self._batched) == len(self._connectors) or any(c.fixed for c in batched)

    @property
    def fixed(self) -> frozenset[Side]:
        """The sides that read fixed files: what a read of one observed stands."""
        return frozenset(side for side, c in self._batched.items() if c.fixed)

    async def standing(self, firsts: Mapping[str, KeyResult]) -> dict[str, Held]:
        """What a recheck of each key (``firsts``: its stored first result) does not read again: a side of
        fixed files keeps the key's first observation of it, after one stat check of its files for the
        group. A first read that failed observed no bytes, and files that do not answer the check keep
        nothing: those are read."""
        held: dict[str, Held] = {key: {} for key in firsts}
        for side, connector in self._batched.items():
            if not connector.fixed:
                continue
            seen = {key: kept for key, first in firsts.items() if (kept := _observed(first, side))}
            if seen and await connector.unchanged(self._sides[side]):
                for key, kept in seen.items():
                    held[key][side] = kept
        return held

    @property
    def large(self) -> bool:
        """Whether a side's reads may hold large values: its keys are read few at a time."""
        return any(c.large for c in self._batched.values())

    async def ahead(self, keys: Sequence[str]) -> None:
        """Read ``keys`` on each side of fixed files, one pass over them a side, for a
        group whose other side is then read few keys at a time: ``prefetch`` is told of them."""
        for side in self.fixed:
            connector = self._batched[side]
            connector.forget(keys)
            await connector.prefetch(keys, self._sides[side])

    async def prefetch(
        self,
        batches: Sequence[tuple[Sequence[str], Sequence[Side]]],
        held: Mapping[str, Held] | None = None,
        ahead: Sequence[str] = (),
    ) -> None:
        """Read each batch of keys on each side that reads groups, in its sides' order, in one statement a
        side, but the sides ``held`` keeps of a key (``standing``) and the sides of fixed files that
        ``ahead`` read of its keys; what an earlier group left unread of these keys is dropped first, never
        served."""
        tables = self._batched
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
            raise Invalid(str(exc)) from None
        chain, n = [step.name for step in spec.key.normalize], len(spec.key_parts) or 1
        self._raw: dict[str, str] = {}
        for raw in source.keys():
            key = fit_parts(raw, chain, n)
            if key is not None and self._raw.setdefault(key, raw) != raw:
                raise Invalid("two captured sample keys normalise to the same key (key.normalize)")
        self._connector = FileConnector(source)
        self._sides = spec.sides
        self.fixed: frozenset[Side] = frozenset()  # captured samples: no side reads a table

    batches = False  # each key is read alone
    large = False

    def reconnect(self) -> None:
        return None  # captured samples hold no credential

    def refused(self, reads: Mapping[Side, Observation]) -> float | None:
        return None  # no breaker or pause stands before a sample

    async def standing(self, firsts: Mapping[str, KeyResult]) -> dict[str, Held]:
        return {key: {} for key in firsts}

    async def ahead(self, keys: Sequence[str]) -> None:
        return None

    async def prefetch(
        self,
        batches: Sequence[tuple[Sequence[str], Sequence[Side]]],
        held: Mapping[str, Held] | None = None,
        ahead: Sequence[str] = (),
    ) -> None:
        return None

    async def fetch(self, key: str, side: Side, *, run_id: str, call_seq: int) -> Observation:
        raw = self._raw.get(key)
        if raw is None:
            raise NotCapturedError("no sample captured for this key")
        obs = await self._connector.fetch(raw, self._sides[side])
        return obs if raw == key else dataclasses.replace(obs, key=key)

    async def aclose(self) -> None:
        return None



def _observed(first: KeyResult, side: Side) -> tuple[str, SideState] | None:
    """A stored result's observation of ``side`` and its state; None without one, or when its read failed."""
    obs_id, state = (first.obs_a_id, first.side_a) if side is Side.A else (first.obs_b_id, first.side_b)
    return None if obs_id is None or state.kind is SideStateKind.ERROR else (obs_id, state)


def _observe(spec: CompiledSpec, caps: Capabilities, obs: Observation) -> SideState:
    return observe_one(spec, caps, obs)


def _compare(
    ctx: Context, spec: CompiledSpec, key: str, a: SideState, b: SideState, today: date | None = None
) -> KeyResult:
    today = today or datetime.now(UTC).date()
    return compared(spec, key, {Side.A: a, Side.B: b}, today=today, tokeniser=ctx.tokeniser)


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
    """Read both sides and compare them: A then B on the first pass, B then A on a recheck,
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
    judges: Judges | None = None,
) -> list[tuple[KeyResult, Reads]]:
    """``evaluate`` for each (key, pass_no): the reads first (a prefetched table side answers at
    once), then the evidence and the judgement of them all in one thread call, or with ``judges`` the
    evidence named in a thread and the reads judged by a judge process, which holds no key: its answers
    are finished here (``_finished``). A recheck's key keeps the sides ``held`` holds of it
    (``LiveReader.standing``): they are not read, and its result names the same observation. The reads end
    at the first refused credential, which is not tried on the group's other keys: the results are then of
    the keys read, and their caller hands the group back."""
    fetched = []
    for key, pass_no in keys:
        kept = (held or {}).get(key, {})
        fetched.append((*await _fetch(reader, key, run_id=run_id, pass_no=pass_no, held=kept), kept))
        if any(obs.error_class == "auth_error" for obs in fetched[-1][0].values()):
            break
    if judges is None:
        return await asyncio.to_thread(
            lambda: [_judge(ctx, spec, caps, key, *f) for (key, _), f in zip(keys, fetched, strict=False)]
        )
    today = datetime.now(UTC).date()  # one day for the group, wherever each key of it is judged
    named = await asyncio.to_thread(
        lambda: [{side: ctx.evidence.identify(obs) for side, obs in f[0].items()} for f in fetched]
    )
    items: list[Item] = [(key, reads, f[1]) for (key, _), reads, f in zip(keys, named, fetched, strict=False)]
    answers = await judges.judge(spec, caps, items, today)
    return await asyncio.to_thread(
        lambda: [
            _settled(ctx, spec, caps, item, f[2], answer, today)
            for item, f, answer in zip(items, fetched, answers, strict=True)
        ]
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
    """Name the evidence (``save_passes`` stores it before its rows), then observe and compare (off the
    event loop: a heavy map must not stall the API or the other keys); a side ``held`` keeps is named by its
    stored observation."""
    reads = {side: ctx.evidence.identify(obs) for side, obs in observations.items()}
    return _settled(ctx, spec, caps, (key, reads, states), held, None)


def _settled(
    ctx: Context,
    spec: CompiledSpec,
    caps: Capabilities,
    item: Item,
    held: Held,
    answer: Judged,
    today: date | None = None,
) -> tuple[KeyResult, Reads]:
    """``_finished``, its result naming the observations it judged: ``item``'s reads (named evidence) and
    those ``held`` keeps."""
    key, reads, states = item
    result = _finished(ctx, spec, caps, key, reads, states, answer, today)
    ids = {side: obs.obs_id for side, obs in reads.items()} | {side: kept[0] for side, kept in held.items()}
    result.obs_a_id, result.obs_b_id = (ids.get(s) for s in Side)
    return result, reads


def _finished(
    ctx: Context,
    spec: CompiledSpec,
    caps: Capabilities,
    key: str,
    reads: Mapping[Side, Observation],
    states: dict[Side, SideState],
    answer: Judged,
    today: date | None = None,
) -> KeyResult:
    """A key's result from a judge process's ``answer``: its result as given; its states compared here,
    with the tokeniser; for None, its reads observed and compared here, as without judges; and for a key
    whose judging ended a judge twice, INCONCLUSIVE, each side it read ``resource_limit``."""
    if isinstance(answer, KeyResult):
        return answer
    if answer is Ended.JUDGE:
        states = states | {side: SideState.incomplete("resource_limit", ENDED_DETAIL) for side in reads}
    elif answer is None:
        states = observed(spec, caps, reads, states)
    else:
        states = answer
    return _compare(ctx, spec, key, states[Side.A], states[Side.B], today)


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
    obs = _opened(ctx, obs)
    return obs, _observe(spec, caps, obs)


def _opened(ctx: Context, obs: Observation) -> Observation:
    """A stored observation with its body from the evidence."""
    return obs if obs.content_hash is None else dataclasses.replace(obs, body=body_of(ctx, obs))


async def rederive_all(
    ctx: Context,
    conn: repo.Conn,
    spec: CompiledSpec,
    run_id: str,
    rows: Sequence[Mapping],
    caps: Capabilities,
    intermittent: Container[str] = (),
    judges: Judges | None = None,
    saved_findings: Mapping[str, Sequence[repo.Row]] | None = None,
) -> list[KeyResult]:
    """One cycle's stored comparisons again, from their observations and evidence: the
    observations are read in one query and judged in one thread call, or with ``judges`` opened in a
    thread and judged by a judge process (``evaluate_many``). The keys (masked) in ``intermittent`` had
    their stored findings tagged by a recheck (``confirm``), so they are tagged again. An expiring
    rule keeps the decision recorded with the result; the cycle end already has its ``saved_findings``."""
    if not rows:
        return []
    ids = [row[f"obs_{s}_id"] for row in rows for s in "ab" if row[f"obs_{s}_id"]]
    sealer = await ctx.sealer(conn, run_id)
    found = await repo.get_observations(conn, run_id, ids)
    observations = {obs_id: repo.as_observation(sealer.open_row(row)) for obs_id, row in found.items()}
    rules = None
    if any(rule.expires for rule in spec.known_diffs):
        rules = saved_findings if saved_findings is not None else await repo.get_findings(
            conn, run_id, rows[0]["cycle"], [row["key_masked"] for row in rows]
        )

    def judged(row: Mapping, result: KeyResult | None = None) -> KeyResult:
        if result is None:
            stored = [_stored(ctx, spec, caps, observations[row[f"obs_{s}_id"]]) for s in "ab"]
            result = _compare(ctx, spec, stored[0][0].key, stored[0][1], stored[1][1])
        result.obs_a_id, result.obs_b_id = row["obs_a_id"], row["obs_b_id"]
        if rules and (saved := rules.get(row["key_masked"])):
            _recorded_rules(ctx, spec, row, result, saved)
        if row["key_masked"] in intermittent:
            result.findings = tag_intermittent(result.findings, spec)
        return result

    if judges is None:
        return await asyncio.to_thread(lambda: [judged(row) for row in rows])

    def opened(row: Mapping) -> Item:
        a, b = (_opened(ctx, observations[row[f"obs_{s}_id"]]) for s in "ab")
        return a.key, {Side.A: a, Side.B: b}, {}

    today = datetime.now(UTC).date()
    items = await asyncio.to_thread(lambda: [opened(row) for row in rows])
    answers = await judges.judge(spec, caps, items, today)
    return await asyncio.to_thread(
        lambda: [
            judged(row, _finished(ctx, spec, caps, *item, answer, today))
            for row, item, answer in zip(rows, items, answers, strict=True)
        ]
    )


def _recorded_rules(
    ctx: Context, spec: CompiledSpec, row: Mapping, result: KeyResult, saved: Sequence[repo.Row]
) -> None:
    """Restore the rule decisions made when these findings were judged, including on older stored runs."""
    # Both reads use finding order; check identity even when two masked pointers are alike.
    for f, recorded in zip(result.findings, saved, strict=True):
        pointer = repo.storable(mask_pointer(f.pointer_parts, spec, ctx.tokeniser, key=f.key))
        if (pointer, f.change_type.value) != (recorded["json_pointer"], recorded["change_type"]):
            raise ValueError("the stored finding does not match its evidence")
        f.rule_id = recorded["rule_id"]
        kind = recorded["rule_classification"]
        f.rule_classification = RuleClassification(kind) if kind is not None else None
    result.outcome = Outcome(row["outcome"])
    result.first_outcome = Outcome(row["first_outcome"])
    result.suppressed_count = row["suppressed_count"]
    result.attrs.pop("known_diff_errors", None)
    if errors := row["attrs"].get("known_diff_errors"):
        result.attrs["known_diff_errors"] = errors


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
    *,
    form: KeyForm,
) -> None:
    """The pass's observations, then the key's result and masked findings."""
    await save_passes(ctx, conn, spec, run_id, cycle, [(pass_no, result, reads)], sealer, form=form)


async def save_passes(
    ctx: Context,
    conn: repo.Conn,
    spec: CompiledSpec,
    run_id: str,
    cycle: int,
    passes: Sequence[tuple[int, KeyResult, Reads]],
    sealer: Sealer,
    *,
    form: KeyForm,
) -> None:
    """``save_pass`` for each (pass_no, result, reads), with one statement per table, in the caller's
    transaction. Each key is saved by its masked form in the run's ``form`` (``masking.key_form``; the
    foreign keys refuse a mismatch) and each observation's headers sealed by the run's ``sealer``; a first
    pass is a fresh read, a sample's retry replacing a stored result. The blob lock, shared, is taken and
    the group's blobs are stored first (in a thread: each file fsynced, then each directory that gained one
    once), so every blob these rows name is durable before they commit, and a drain cannot delete one
    meanwhile."""
    keys = [mask_key(result.key, spec, ctx.tokeniser, form=form) for _, result, _ in passes]
    observations = [
        (pass_no, masked, obs, _sealed_headers(sealer, cycle, obs))
        for (pass_no, _, reads), masked in zip(passes, keys, strict=True)
        for obs in reads.values()
    ]
    await repo.lock_blobs(conn)
    await asyncio.to_thread(ctx.evidence.put_all, [obs for _, _, obs, _ in observations])
    await repo.insert_observations(conn, run_id, cycle, observations)
    masked = [
        (key, result, [mask_finding(f, spec, ctx.tokeniser, form=form) for f in result.findings])
        for (_, result, _), key in zip(passes, keys, strict=True)
    ]
    fresh = {key for (pass_no, _, _), key in zip(passes, keys, strict=True) if pass_no == 1}
    await repo.save_key_results(conn, run_id, cycle, masked, fresh)


def _sealed_headers(sealer: Sealer, cycle: int, obs: Observation) -> bytes:
    headers = jsonio.dumps(repo.storable(obs.headers)).encode()
    assert obs.obs_id is not None  # identify set it
    return sealer.seal("headers", cycle, obs.obs_id, headers)
