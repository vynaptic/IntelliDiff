"""Judging one key from its reads: each side observed, then the two compared (``compare_pair``).

The worker judges in a thread of its own process, with the tokeniser, or in judge processes that hold no
key (``runner.judges``), which judge with ``judge_keyless``. There the tokeniser is stood in for: a
comparison that asks it (a known-difference rule reading a pointer through a protected member name or
member key) does not finish, and the observed states go back for the worker to compare with its own.
Both ways call the same functions, so a key is judged alike either way.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, cast

from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.masking import Tokeniser
from deltasleuth.core.types import (
    CompiledSpec,
    KeyResult,
    Observation,
    Side,
    SideState,
    TargetCapabilities,
)

Capabilities = Mapping[str, TargetCapabilities]  # by target id
States = dict[Side, SideState]


class KeyNeeded(Exception):
    """A comparison asked the tokeniser, which a judge without its key only stands in for."""


class _Keyless:
    """Stands in for the tokeniser where no key is held: any use of it raises ``KeyNeeded`` and is
    remembered (``asked``), should the code that asked catch what it raised."""

    __slots__ = ("asked",)

    def __init__(self) -> None:
        self.asked = False

    def __getattr__(self, name: str) -> Any:
        self.asked = True
        raise KeyNeeded(name)


def observe_one(spec: CompiledSpec, caps: Capabilities, obs: Observation) -> SideState:
    """``obs`` observed as its side of ``spec`` reads, with what its target declares."""
    side = spec.sides[obs.side]
    return observe(obs, side, spec, caps[side.target])


def observed(
    spec: CompiledSpec, caps: Capabilities, observations: Mapping[Side, Observation], states: States
) -> States:
    """``states`` (a side not read: its state) and each side of ``observations`` observed."""
    return states | {side: observe_one(spec, caps, obs) for side, obs in observations.items()}


def compared(
    spec: CompiledSpec,
    key: str,
    states: Mapping[Side, SideState],
    *,
    today: date,
    tokeniser: Tokeniser | None,
) -> KeyResult:
    """The key's two side states compared into its provisional result."""
    return compare_pair(key, states[Side.A], states[Side.B], spec, today=today, tokeniser=tokeniser)


def judge_keyless(
    spec: CompiledSpec,
    caps: Capabilities,
    key: str,
    observations: Mapping[Side, Observation],
    states: States,
    *,
    today: date,
) -> KeyResult | States | None:
    """``key`` judged without the tokeniser key: its result; its observed states when comparing them asked
    the tokeniser; None when judging raised (the worker judges such a key again itself, and so raises what
    its own judging raises)."""
    try:
        states = observed(spec, caps, observations, states)
    except Exception:
        return None
    keyless = _Keyless()
    try:
        result = compared(spec, key, states, today=today, tokeniser=cast(Tokeniser, keyless))
    except KeyNeeded:
        return states
    except Exception:
        return states if keyless.asked else None
    return states if keyless.asked else result
