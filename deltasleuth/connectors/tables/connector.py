"""Per-key reads of a table side (TABLES.md §4, §9, §10, §12): one statement for one key or a batch,
through the side's engine (ENGINES.md §5).

A key its side's key maps cannot map back holds no row there: 404 without a query. A row outside the
side's filter is still read and recorded as not selected. typeMap columns are converted before
the row is recorded, so evidence, the engine and the scan's digests see the same values.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from functools import partial

from deltasleuth.connectors.base import (
    TRANSPORT_ERRORS,
    KeyRejectedError,
    UnsafeTargetError,
    circuit_open,
    guarded,
    utc_now_iso,
)
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.tables.engine import (
    TIMEOUT,
    UNREACHABLE,
    Asked,
    Row,
    Session,
    SnapshotLost,
    TableError,
    TargetAuthRefused,
    TargetRefused,
    TargetTempSpace,
    code,
    named,
    planned,
)
from deltasleuth.connectors.tables.keys import Plan, side_values
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.convert import ConversionError, convert_text
from deltasleuth.core.canonical.encoding import content_hash
from deltasleuth.core.types import ExecutionConfig, Observation, SideConfig, Target

Observed = dict[str, Observation]  # by key


def relation(side: Plan) -> str:
    """The side's table or view as people read it; a file side's label (docs/mvp/FILES.md §6)."""
    return f"{side['schema']}.{side['name']}" if side.get("schema") else side["name"]


class TableConnector:
    """``Connector`` over one table side of a run's plan, through one session of its engine; ``prefetch``
    reads a batch of keys in one statement, and ``fetch`` then takes each without one."""

    def __init__(self, target: Target, execution: ExecutionConfig, side: Plan, *, gate: TargetGate) -> None:
        self._target, self._timeout_s, self._gate, self._side = target, execution.total_timeout_s, gate, side
        self._breaker = gate.breaker(execution)
        self._engine = named(side)
        self._url = f"{self._engine.name}://{target.target_id}/{relation(side)}"
        self._session: Session | None = None
        self._lock = asyncio.Lock()
        self._cached: Observed = {}

    @property
    def large(self) -> bool:
        """Whether a row of the side may hold large values (ENGINES.md §6): its keys are read few at a
        time, since a group's rows are held together."""
        return bool(self._side) and self._engine.large(self._side)

    @property
    def fixed(self) -> bool:
        """Whether the side reads fixed files (docs/mvp/FILES.md §11): each read of it is a pass over them,
        and what a read observed stands."""
        return self._side.get("consistency") == "fixed"

    async def unchanged(self, side_cfg: SideConfig) -> bool:
        """One stat check of a fixed side's files, for a group of rechecks that read none of them again
        (docs/mvp/FILES.md §12): a changed file fails the run; False when the files do not answer, so
        nothing of them stands for this pass and its keys are read as any others."""
        engine = planned(side_cfg.side.value, self._side, self._target)
        try:
            async with self._lock:
                self._session = self._session or await engine.connect(self._target, timeout_s=self._timeout_s)
                await engine.describe(self._session, None, self._side["name"], side=self._side)
        except UNREACHABLE:
            return False
        return True

    async def fetch(
        self, key: str, side_cfg: SideConfig, *, run_id: str = "p0", call_seq: int = 0
    ) -> Observation:
        if (cached := self._cached.pop(key, None)) is not None:
            return cached
        observed = await self._read({key: side_values(key, self._side)}, side_cfg)
        if key not in observed:
            raise KeyRejectedError("the key does not fit the key column's type or encoding")
        return observed[key]

    def forget(self, keys: Iterable[str]) -> None:
        """Drop the prefetched observations of ``keys`` that were never fetched (a group handed back),
        so they are not served later; other groups' reads stay."""
        for key in keys:
            self._cached.pop(key, None)

    async def prefetch(self, keys: Sequence[str], side_cfg: SideConfig) -> None:
        """Read ``keys`` in one statement; a malformed key, or one its column cannot hold, is left to
        ``fetch``, which reads it alone and rejects it."""
        values = {}
        for key in keys:
            try:
                values[key] = side_values(key, self._side)
            except KeyRejectedError:
                continue
        self._cached |= await self._read(values, side_cfg)

    async def _read(self, values: dict[str, list[list[str]] | None], side_cfg: SideConfig) -> Observed:
        """Each key's observation, except a key its column cannot hold."""
        planned(side_cfg.side.value, self._side, self._target)  # the target's engine is still the plan's
        observed_at = utc_now_iso()

        def observation(key: str, **fields: object) -> Observation:
            return Observation(
                key=key,
                side=side_cfg.side,
                observed_at=observed_at,
                request_url=self._url,
                **fields,  # type: ignore[arg-type]
            )

        found = {key: observation(key, http_status=404, body=None) for key, v in values.items() if v is None}
        asked = {key: v for key, v in values.items() if v is not None}  # the others cannot be on this side
        if asked:
            found |= await self._guarded(asked, observation)
        if len(asked) > 1:  # a batch slower than the per-key timeout: each of its keys alone, on its terms,
            # but on a file side, where each would be a pass over its files (docs/mvp/FILES.md §6)
            timeouts = [
                k for k, o in found.items() if o.error_class == "timeout" and self._engine.retry_alone
            ]
            for key in timeouts:  # unfit keys are absent
                found |= await self._guarded({key: asked[key]}, observation)
            # A session the server ended while idle (a soak's interval, a failover) fails the batch it
            # was handed; the batch is read once more on a fresh session before its keys count as lost.
            if lost := {k: asked[k] for k, o in found.items() if o.error_class == "connect_error"}:
                found |= await self._guarded(lost, observation)
        return found

    async def _guarded(self, asked: Asked, observation: Callable[..., Observation]) -> Observed:
        """``asked`` read in one statement the breaker admits: all open-circuit when it refuses."""
        return await guarded(
            self._breaker,
            lambda: {key: circuit_open(partial(observation, key)) for key in asked},
            lambda _: self._query(asked, observation),
            lambda result: any(o.error_class in TRANSPORT_ERRORS for o in result.values()),
        )

    async def _query(self, asked: Asked, observation: Callable[..., Observation]) -> Observed:
        keys, items = list(asked), list(asked.items())
        failure = detail = None
        async with self._lock, self._gate.semaphore:  # a read queued on this session holds no slot
            await self._gate.limiter.acquire()
            started = time.perf_counter()
            try:
                self._session = self._session or await self._engine.connect(
                    self._target, timeout_s=self._timeout_s
                )
                rows: list[Row] | None = await self._rows(self._session, items)
            except self._engine.unfit:  # their messages quote a key: never recorded
                rows, failure = None, "unfit"
            except (
                *UNREACHABLE,
                SnapshotLost,
                TargetRefused,
                TargetAuthRefused,
                UnsafeTargetError,
                TargetTempSpace,
            ) as exc:
                # An outage, a refused login, the guard, or a server's code a later read may not meet; a
                # full disk or a privilege gone propagates and fails the run (ENGINES.md §11), but a file
                # side's read met its sort's budget, which the spec's retries read again (FILES.md §10).
                if isinstance(exc, TargetTempSpace) and self._engine.name != "files":
                    raise
                await self.aclose()  # the next read opens a fresh session
                rows, failure = None, "timeout" if isinstance(exc, TIMEOUT) else "connect_error"
                if isinstance(exc, TargetAuthRefused):
                    failure = "auth_error"  # the run pauses: a stale password is not tried in a loop
                coded = failure == "timeout" or isinstance(exc, TableError | UnsafeTargetError)
                detail = code(exc) if coded else "OperationalError"  # psycopg's outages share a name
        if rows is None and failure == "unfit" and len(keys) > 1:
            # A key its column cannot hold: halve the batch until that key stands alone.
            first, second = dict(items[: len(items) // 2]), dict(items[len(items) // 2 :])
            return await self._query(first, observation) | await self._query(second, observation)
        if rows is None:
            if failure == "unfit":
                return {}  # fetch rejects it
            return {
                k: observation(k, http_status=None, body=None, error_class=failure, error_detail=detail)
                for k in keys
            }
        latency = round((time.perf_counter() - started) * 1000.0, 3)
        by_key: dict[int, list[tuple[str | ValueError, bool]]] = defaultdict(list)
        for i, body, selected in rows:
            by_key[i].append((body, selected))
        return {
            k: self._observed(partial(observation, k, latency_ms=latency), by_key[i])
            for i, k in enumerate(keys)
        }

    async def _rows(self, session: Session, items: list[tuple[str, list[list[str]]]]) -> list[Row]:
        """The rows of ``items``' keys, each by its key's position among them, read in statements of as
        many keys as one of the engine's holds: each statement after the first takes a token of the rate
        budget too."""
        arms, rows = self._engine.arms, []
        for start in range(0, len(items), arms):
            if start:
                await self._gate.limiter.acquire()
            read = await self._engine.rows(session, self._side, dict(items[start : start + arms]))
            rows += [(start + i, body, selected) for i, body, selected in read]
        return rows

    def _observed(
        self, observation: Callable[..., Observation], rows: list[tuple[str | ValueError, bool]]
    ) -> Observation:
        if not rows and self._side.get("rejected"):  # D6: the key may be on a line that was not a row
            lines = self._side["rejected"]
            why = f"no row read holds the key, and {lines} line(s) of this side's files were not rows"  # M1
            return observation(http_status=None, body=None, error_class="not_rows", error_detail=why)
        if not rows:
            return observation(http_status=404, body=None)
        failed = partial(observation, http_status=200, body=None)
        if len(rows) > 1 and rows[0][1] == rows[1][1]:  # a key unique inside the filter is not ambiguous
            return failed(error_class="identity_ambiguous", error_detail="the key selects more than one row")
        text, selected = rows[0]
        try:
            if isinstance(text, ValueError):  # its engine could not render the row (ENGINES.md §9)
                raise text
            if self._side.get("types"):
                text = jsonio.dumps(convert_text(text, self._side["types"]))
        except jsonio.ResourceLimitError:
            return failed(error_class="resource_limit", error_detail="the row passes a parse limit")
        except ConversionError as exc:  # it names the column and the type, never the value
            return failed(error_class="unconvertible", error_detail=str(exc))
        body = text.encode()
        if len(body) > jsonio.DEFAULT_MAX_BYTES:  # the evidence store refuses it, so it is no evidence
            return failed(
                error_class="resource_limit",
                error_detail=f"row of {len(body)} bytes exceeds {jsonio.DEFAULT_MAX_BYTES}",
            )
        headers = {} if selected else {"selected": "false"}  # outside the side's filter
        return observation(http_status=200, body=body, headers=headers, content_hash=content_hash(body))

    async def aclose(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()
