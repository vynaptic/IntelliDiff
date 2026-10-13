"""The cost formula, checked against the design's worked example."""

from __future__ import annotations

from decimal import Decimal

from deltasleuth.llm.model import Usage
from deltasleuth.llm.usage import PriceVersion, call_cost, cost_micros

OPUS = PriceVersion(
    "anthropic-2026-09-24", "USD", Decimal(5), Decimal(25), Decimal("0.1"), Decimal("1.25"), Decimal(2)
)


def test_worked_example_cached_call_is_seven_cents() -> None:
    # 10k cached prefix at 0.1x, 6k new input, 1.5k output: $0.005 + $0.030 + $0.0375
    usage = Usage(input=16_000, cache_read=10_000, output=1_500)
    assert call_cost(usage, OPUS) == Decimal("0.0725")
    assert cost_micros(usage, OPUS) == 72_500


def test_cache_writes_use_their_ttl_multiplier() -> None:
    assert call_cost(Usage(input=16_000, cache_write_5m=10_000, output=1_500), OPUS) == Decimal("0.13")
    # 6k new + 4k x 1.25 + 6k x 2 at $5/MTok, 1.5k out at $25/MTok
    usage = Usage(input=16_000, cache_write_5m=4_000, cache_write_1h=6_000, output=1_500)
    assert call_cost(usage, OPUS) == Decimal("0.1525")


def test_reasoning_is_billed_as_output() -> None:
    assert call_cost(Usage(output=1_000, reasoning=600), OPUS) == Decimal("0.025")


def test_unset_multipliers_price_cache_as_ordinary_input() -> None:
    price = PriceVersion("v", "EUR", Decimal(2), Decimal(8))
    usage = Usage(input=3_000, cache_read=1_000, cache_write_5m=1_000, cache_write_1h=1_000)
    assert call_cost(usage, price) == Decimal("0.006")


def test_micros_round_half_up() -> None:
    price = PriceVersion("v", "USD", Decimal("0.5"), Decimal(0))
    assert (cost_micros(Usage(input=1), price), cost_micros(Usage(input=3), price)) == (1, 2)
