"""Stop conditions (spec ``execution.stop``): the run stops at the first one met and records it.

``maxKeys`` caps the key set when the run is created. ``maxCycles``, ``until`` and ``maxDuration``
end a soak at a cycle boundary; ``until`` and ``maxDuration`` also stop a cycle in progress, like
the call and key conditions a ``StopMonitor`` judges as keys finish. Rates are judged once their
window is full, so the first few calls cannot stop a run on their own. Each worker process judges
the calls and keys it made itself; the CRITICAL findings count every key's latest saved result.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from deltasleuth.core.spec import parse_duration
from deltasleuth.core.types import KeyResult, Outcome, Severity

STOP_PREFIX = "stop_condition:"


@dataclass(frozen=True)
class RateWindow:
    rate: float
    window: int


@dataclass(frozen=True)
class StopConditions:
    max_keys: int | None = None
    max_cycles: int | None = None
    max_duration_s: float | None = None
    until: datetime | None = None
    max_error_rate: RateWindow | None = None
    max_inconclusive_rate: RateWindow | None = None
    max_consecutive_errors: int | None = None
    max_unexplained_critical: int | None = None

    @classmethod
    def from_spec(cls, raw: Mapping[str, Any]) -> StopConditions:
        entry = raw.get("execution", {}).get("stop", {})
        until = datetime.fromisoformat(entry["until"]) if "until" in entry else None
        return cls(
            max_keys=entry.get("maxKeys"),
            max_cycles=entry.get("maxCycles"),
            max_duration_s=parse_duration(entry["maxDuration"]) if "maxDuration" in entry else None,
            until=until if until is None or until.tzinfo else until.replace(tzinfo=UTC),
            max_error_rate=_window(entry.get("maxErrorRate")),
            max_inconclusive_rate=_window(entry.get("maxInconclusiveRate")),
            max_consecutive_errors=entry.get("maxConsecutiveErrors"),
            max_unexplained_critical=entry.get("maxUnexplainedCritical"),
        )

    def expired(self, started_at: datetime, now: datetime) -> str | None:
        """``until`` or ``maxDuration`` (counted from the run's start) reached at ``now``."""
        if self.until is not None and now >= self.until:
            return STOP_PREFIX + "until"
        if self.max_duration_s is not None and (now - started_at).total_seconds() >= self.max_duration_s:
            return STOP_PREFIX + "maxDuration"
        return None

    def at_boundary(self, cycle: int, started_at: datetime, next_at: datetime) -> str | None:
        """Why a soak ends after ``cycle`` instead of starting its next cycle at ``next_at``."""
        if self.max_cycles is not None and cycle >= self.max_cycles:
            return STOP_PREFIX + "maxCycles"
        return self.expired(started_at, next_at)


def _window(entry: Mapping[str, Any] | None) -> RateWindow | None:
    return None if entry is None else RateWindow(float(entry["rate"]), int(entry["window"]))


def _exceeded(samples: deque[bool], limit: RateWindow | None) -> bool:
    return limit is not None and len(samples) >= limit.window and sum(samples) / len(samples) > limit.rate


class StopMonitor:
    """Feeds one run's calls and results into its stop conditions; ``reason`` says when to stop."""

    def __init__(
        self, conditions: StopConditions, started_at: datetime, critical: Mapping[str, int] | None = None
    ) -> None:
        """``critical``: unsuppressed CRITICAL findings per key, of results saved before it."""
        self.conditions, self.started_at = conditions, started_at
        c = conditions
        self._calls: deque[bool] = deque(maxlen=c.max_error_rate.window if c.max_error_rate else 0)
        self._keys: deque[bool] = deque(
            maxlen=c.max_inconclusive_rate.window if c.max_inconclusive_rate else 0
        )
        self._consecutive = 0
        self.critical = dict(critical or {})
        self._fired: str | None = None

    def record_call(self, *, failed: bool) -> None:
        """A read judged ERROR is a failed call; an incomplete read (truncated, unparseable) is not."""
        c = self.conditions
        self._calls.append(failed)
        self._consecutive = self._consecutive + 1 if failed else 0
        if c.max_consecutive_errors is not None and self._consecutive >= c.max_consecutive_errors:
            self._fire("maxConsecutiveErrors")
        if _exceeded(self._calls, c.max_error_rate):
            self._fire("maxErrorRate")

    def record_result(self, key: str, result: KeyResult, *, first_read: bool) -> None:
        """A first read feeds the INCONCLUSIVE window; every result replaces the key's CRITICAL count."""
        c = self.conditions
        if first_read:
            self._keys.append(result.outcome is Outcome.INCONCLUSIVE)
            if _exceeded(self._keys, c.max_inconclusive_rate):
                self._fire("maxInconclusiveRate")
        self.critical[key] = sum(
            1 for f in result.findings if not f.suppressed and f.severity is Severity.CRITICAL
        )
        if (
            c.max_unexplained_critical is not None
            and sum(self.critical.values()) >= c.max_unexplained_critical
        ):
            self._fire("maxUnexplainedCritical")

    def reason(self, now: datetime) -> str | None:
        """The first condition met so far (the time limits are judged at ``now``), else None."""
        return self._fired or self.conditions.expired(self.started_at, now)

    def _fire(self, name: str) -> None:
        self._fired = self._fired or STOP_PREFIX + name
