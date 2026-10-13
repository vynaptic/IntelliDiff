"""Per-call cost from normalised usage and a versioned price table, exact in ``Decimal``.

``cost = p_in * (uncached + m_read * cache_read + m_5m * write_5m + m_1h * write_1h) + p_out * output``
Unset cache multipliers are 1, which over-states cost rather than under-stating it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import ConfigDict, Field, StrictStr, with_config

from deltasleuth.llm.model import Usage

MTOK = Decimal(1_000_000)
Rate = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


@with_config(ConfigDict(extra="forbid"))
@dataclass(frozen=True)
class PriceVersion:
    """A profile's ``pricing``: prices per million tokens and the version recorded per call."""

    version: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    input_per_mtok: Rate
    output_per_mtok: Rate
    cache_read_mult: Rate = Decimal(1)
    cache_write_mult: Rate = Decimal(1)  # 5-minute cache writes
    cache_write_1h_mult: Rate = Decimal(1)


def call_cost(usage: Usage, price: PriceVersion) -> Decimal:
    """Exact cost of one call in ``price.currency``."""
    weighted_input = (
        usage.uncached_input
        + price.cache_read_mult * usage.cache_read
        + price.cache_write_mult * usage.cache_write_5m
        + price.cache_write_1h_mult * usage.cache_write_1h
    )
    return (price.input_per_mtok * weighted_input + price.output_per_mtok * usage.output) / MTOK


def cost_micros(usage: Usage, price: PriceVersion) -> int:
    """``call_cost`` in millionths of the currency, rounded half-up."""
    return int((call_cost(usage, price) * MTOK).quantize(Decimal(1), rounding=ROUND_HALF_UP))
