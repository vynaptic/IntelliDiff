"""Retry timing: exponential backoff with full jitter; ``Retry-After`` is a floor, never capped."""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from deltasleuth.core.types import RetryPolicy

# RFC 9110 delta-seconds: ASCII digits only ("²".isdigit() is True, but int() rejects it).
_DELTA_SECONDS = re.compile(r"[0-9]{1,10}")


def retry_delay(retry_no: int, policy: RetryPolicy, rng: random.Random, retry_after_s: float | None) -> float:
    """Delay before retry ``retry_no`` (1-based): a full-jitter draw, at least ``Retry-After``."""
    ceiling = min(policy.max_s, policy.initial_s * policy.multiplier ** (retry_no - 1))
    delay = rng.uniform(0.0, max(ceiling, 0.0))
    return delay if retry_after_s is None else max(retry_after_s, delay)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds from a ``Retry-After`` value (delta-seconds or HTTP-date); None if unusable.

    The service under test controls this header, so it never raises.
    """
    if value is None:
        return None
    text = value.strip()
    if _DELTA_SECONDS.fullmatch(text):
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if moment.tzinfo is None:  # HTTP-dates are GMT
        moment = moment.replace(tzinfo=UTC)
    return max((moment - (now or datetime.now(UTC))).total_seconds(), 0.0)
