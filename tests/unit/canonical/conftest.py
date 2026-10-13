"""Fixtures for the canonical / observe / diff tests: specs built directly from core types."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from deltasleuth.core.canonical.canonicalise import canonicalise
from deltasleuth.core.jsonio import parse_json
from deltasleuth.core.types import (
    Canonical,
    CompareConfig,
    CompiledSpec,
    KeyConfig,
    Observation,
    Side,
    SideConfig,
)


def build_spec(
    compare: CompareConfig | None = None,
    *,
    side_a: SideConfig | None = None,
    side_b: SideConfig | None = None,
) -> CompiledSpec:
    return CompiledSpec(
        name="unit",
        version="1.0.0",
        content_hash="0" * 64,
        normalizer_version="1.0.0",
        key=KeyConfig(name="code"),
        sides={
            Side.A: side_a or SideConfig(side=Side.A, role="legacy", target="target-a"),
            Side.B: side_b or SideConfig(side=Side.B, role="new", target="target-b"),
        },
        compare=compare or CompareConfig(),
    )


def build_observation(
    body: Any = None, *, side: Side = Side.A, status: int | None = 200, key: str = "K1", **fields: Any
) -> Observation:
    raw = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
    return Observation(
        key=key, side=side, http_status=status, body=raw, observed_at="2026-09-24T00:00:00Z", **fields
    )


@pytest.fixture
def make_spec() -> Callable[..., CompiledSpec]:
    return build_spec


@pytest.fixture
def make_observation() -> Callable[..., Observation]:
    return build_observation


@pytest.fixture
def canon() -> Callable[..., Canonical]:
    """Canonicalise JSON text (exact numbers) for one side of ``spec``."""

    def run(text: str, spec: CompiledSpec, side: Side = Side.A, key: str = "K1") -> Canonical:
        return canonicalise(parse_json(text)[0], spec.sides[side], spec, key=key)

    return run
