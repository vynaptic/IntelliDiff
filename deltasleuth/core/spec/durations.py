"""Compact durations (spec ``$defs/duration``): a number followed by ms, s, m, h or d."""

from __future__ import annotations

import re
from decimal import Decimal

_DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)")
_SECONDS = {
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "m": Decimal(60),
    "h": Decimal(3600),
    "d": Decimal(86400),
}


class DurationError(ValueError):
    """The text is not a compact duration."""


def parse_duration(text: str) -> float:
    """Seconds in ``text`` ("250ms" -> 0.25); scaled as a Decimal so "0.1s" == "100ms"."""
    match = _DURATION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise DurationError(f"not a duration (expected e.g. 250ms, 30s, 10m, 2h, 1d): {text!r}")
    return float(Decimal(match[1]) * _SECONDS[match[2]])
