"""Fixtures: a minimal spec builder (built directly, without the loader), findings and a tokeniser."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from deltasleuth.core.types import (
    ArrayRule,
    ChangeType,
    CompareConfig,
    CompiledSpec,
    Direction,
    Finding,
    KeyConfig,
    SecurityPolicy,
    Side,
    SideConfig,
    TokenizeRule,
)
from deltasleuth.security.tokenise import Tokeniser


def build_spec(
    *,
    tokenize: tuple[tuple[str, str], ...] = (),
    clear: tuple[str, ...] = (),
    unlisted: str = "tokenize",
    arrays: tuple[ArrayRule, ...] = (),
) -> CompiledSpec:
    return CompiledSpec(
        name="security-test",
        version="1.0.0",
        content_hash="0" * 64,
        normalizer_version="1.0.0",
        key=KeyConfig(name="customer_id"),  # a business code: keys stay clear
        sides={Side.A: SideConfig(Side.A, "legacy", "svc-a"), Side.B: SideConfig(Side.B, "new", "svc-b")},
        compare=CompareConfig(arrays=arrays),
        security=SecurityPolicy(tuple(TokenizeRule(p, c) for p, c in tokenize), clear, unlisted),  # type: ignore[arg-type]
    )


def build_finding(
    path: str,
    change: ChangeType,
    a: Any,
    b: Any,
    parts: tuple[tuple[str, str], ...],
    *,
    key: str = "K1",
    **fields: Any,
) -> Finding:
    """A differ-shaped finding (json_pointer rendered from ``parts`` without masking)."""
    direction = {ChangeType.MISSING_IN_B: Direction.A_ONLY, ChangeType.EXTRA_IN_B: Direction.B_ONLY}
    return Finding(
        key=key,
        json_pointer="".join("/" + text for _, text in parts),
        path_pattern=path,
        change_type=change,
        a_value=a,
        b_value=b,
        a_raw=a,
        b_raw=b,
        pointer_parts=parts,
        direction=direction.get(change, Direction.BOTH),
        **fields,
    )


@pytest.fixture
def make_spec() -> Callable[..., CompiledSpec]:
    return build_spec


@pytest.fixture
def make_finding() -> Callable[..., Finding]:
    return build_finding


@pytest.fixture
def tok() -> Tokeniser:
    return Tokeniser(bytes(range(32)))
