"""Per-target circuit breaker (spec ``execution.circuitBreaker``): a target keeps one for each such
declaration (``TargetGate.breaker``).

Closed: outcomes fill a sliding window; once it is full and its error share reaches
``error_rate`` the circuit opens and refuses calls for ``cooldown_s``. Then one probe is let
through (half-open): success closes the circuit with a fresh window, failure re-opens it. Where more than
one worker processes keys, an opening reaches the others' breakers of the same declaration (``untold``,
``hold``), each of which then probes on its own.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Literal

Ticket = Literal["call", "probe"]


class CircuitBreaker:
    def __init__(
        self,
        error_rate: float | None,
        window: int,
        cooldown_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._error_rate = error_rate
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._outcomes: deque[bool] = deque(maxlen=window)
        self.state: Literal["closed", "open", "half_open"] = "closed"
        self._opened_at = 0.0
        self._probing = False
        self.told = 0.0  # the opening the other workers know of (``_opened_at``)

    def admit(self) -> Ticket | None:
        """A ticket for an admitted call, or None when the circuit refuses it."""
        if self.state == "closed":
            return "call"
        if self.state == "open" and self._clock() - self._opened_at >= self._cooldown_s:
            self.state = "half_open"
        if self.state == "half_open" and not self._probing:
            self._probing = True
            return "probe"
        return None

    def record(self, ticket: Ticket, error: bool) -> None:
        """Record an admitted call's outcome; only the probe resolves a half-open circuit."""
        if ticket == "probe":
            self._probing = False
            if error:
                self._open()
            else:
                self.state = "closed"
                self._outcomes.clear()
        elif self.state == "closed":
            self._outcomes.append(error)
            # The window must be full first, so a run's first failure (1/1) cannot trip it.
            if self._error_rate is not None and len(self._outcomes) == self._outcomes.maxlen:
                if sum(self._outcomes) / len(self._outcomes) >= self._error_rate:
                    self._open()

    def closed_for(self) -> float:
        """Seconds until the circuit admits a call again: what is left of the cooldown while it is open, a
        whole cooldown while its probe is out (a failed probe opens it again), else 0."""
        if self.state == "open":
            return max(self._opened_at + self._cooldown_s - self._clock(), 0.0)
        return self._cooldown_s if self.state == "half_open" and self._probing else 0.0

    def untold(self) -> bool:
        """Whether the circuit is open on an opening the other workers do not know of yet, which counts as
        told from now on."""
        if self.state != "open" or self._opened_at <= self.told or not self.closed_for():
            return False
        self.told = self._opened_at
        return True

    def hold(self, seconds: float) -> None:
        """Keep the circuit open ``seconds`` from now at least: another worker's breaker of the same
        declaration opened. A probe in flight settles it on its own."""
        if self.state == "half_open" and self._probing:
            return
        opened = self._clock() + seconds - self._cooldown_s
        if self.state == "open" and opened <= self._opened_at:
            return
        self.state, self._opened_at, self.told = "open", opened, opened
        self._outcomes.clear()

    def release(self, ticket: Ticket) -> None:
        """Forget an admitted call that ended without an outcome (cancelled, or never sent)."""
        if ticket == "probe":
            self._probing = False

    def _open(self) -> None:
        self.state = "open"
        self._opened_at = self._clock()
        self._outcomes.clear()
