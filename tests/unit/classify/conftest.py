"""Fixtures: a masked record policy built directly as a CompiledSpec, differ-shaped findings, a stub
differ and a fixed tokeniser."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from deltasleuth.core.canonical.encoding import canonical_json
from deltasleuth.core.types import (
    ArrayRule,
    Canonical,
    ChangeType,
    CompareConfig,
    CompiledSpec,
    Direction,
    FieldRule,
    Finding,
    KeyConfig,
    KnownDiffRule,
    RuleClassification,
    SecurityPolicy,
    Severity,
    Side,
    SideConfig,
    TokenizeRule,
)
from deltasleuth.security.tokenise import Tokeniser

EXPECTED_RULE = KnownDiffRule(
    "KD-TEST-1",
    "path == '$.group' && a == 'BLUE' && b == 'CYAN'",
    RuleClassification.EXPECTED_BY_DESIGN,
    "The candidate uses a new label for this group.",
    "TEST-1042",
)
BUG_RULE = KnownDiffRule(
    "KD-TEST-2",
    "path == '$.options.delivery.local' && change_type == 'missing_in_b'",
    RuleClassification.BUG,
    "The candidate omits this optional flag.",
    "TEST-1107",
    expires="2027-01-31",
)
_TOKENIZE = {
    "$.primaryLabel": "name",
    "$.otherLabel": "name",
    "$.privateDate": "date_of_birth",
    "$.email": "email",
    "$.channels[*].number": "phone",
    "$.locations[*].lines[*]": "address",
    "$.locations[*].city": "address",
    "$.locations[*].postcode": "address",
    "$.entries[*].slotNo": "account_number",
}
_CLEAR = """$.group $.status $.createdAt $.channels[*].kind $.locations[*].type $.locations[*].country
    $.entries[*].groupCode $.entries[*].kindCode $.entries[*].status $.entries[*].unit
    $.entries[*].ratio $.flags[*].mode $.flags[*].enabled $.flags[*].updatedAt
    $.options.language $.options.delivery.email $.options.delivery.local $.tags[*]""".split()


def build_spec(**overrides: Any) -> CompiledSpec:
    spec = CompiledSpec(
        name="item-parity-v1-vs-v2",
        version="1.0.0",
        content_hash="0" * 64,
        normalizer_version="1.0.0",
        key=KeyConfig(name="record_id", pattern="^C[0-9]{8}$", value_class="identifier"),
        sides={Side.A: SideConfig(Side.A, "legacy", "v1"), Side.B: SideConfig(Side.B, "new", "v2")},
        compare=CompareConfig(
            arrays=(
                ArrayRule("$.channels", "keyed", ("number",)),
                ArrayRule("$.locations", "keyed", ("type",)),
                ArrayRule("$.locations[*].lines", "ordered"),
                ArrayRule("$.entries", "keyed", ("groupCode", "slotNo"), severity=Severity.HIGH),
                ArrayRule("$.flags", "keyed", ("mode",), severity=Severity.HIGH),
                ArrayRule("$.tags", "set"),
            ),
            fields=(
                FieldRule("$.entries[*].amount", severity=Severity.HIGH),
                FieldRule("$.entries[*].ratio", severity=Severity.MEDIUM),
                FieldRule("$.flags[*].enabled", severity=Severity.CRITICAL),
            ),
        ),
        known_diffs=(EXPECTED_RULE, BUG_RULE),
        security=SecurityPolicy(tuple(TokenizeRule(p, c) for p, c in _TOKENIZE.items()), tuple(_CLEAR)),
    )
    return replace(spec, **overrides)


def build_finding(
    path: str = "$.group",
    change: ChangeType = ChangeType.VALUE_CHANGED,
    a: Any = None,
    b: Any = None,
    *,
    key: str = "C00000001",
    parts: tuple[tuple[str, str], ...] | None = None,
    **fields: Any,
) -> Finding:
    """A differ-shaped finding; its pointer parts default to the plain fields of ``path``."""
    if parts is None:
        parts = tuple(("field", n) for n in path.removeprefix("$").split(".") if n)
    direction = {ChangeType.MISSING_IN_B: Direction.A_ONLY, ChangeType.EXTRA_IN_B: Direction.B_ONLY}
    values: dict[str, Any] = {"a_raw": a, "b_raw": b, "direction": direction.get(change, Direction.BOTH)}
    return Finding(
        key=key,
        json_pointer="".join("/" + text for _, text in parts),
        path_pattern=path,
        change_type=change,
        a_value=a,
        b_value=b,
        pointer_parts=parts,
        **(values | fields),
    )


class StubDiffer:
    """Returns preset findings for every pair and records its calls."""

    def __init__(self, *findings: Finding) -> None:
        self.findings = findings
        self.calls: list[dict[str, Any]] = []

    def __call__(self, a: Canonical, b: Canonical, spec: CompiledSpec, *, key: str) -> list[Finding]:
        self.calls.append({"a": a, "b": b, "spec": spec, "key": key})
        return [replace(f, key=key) for f in self.findings]


@pytest.fixture
def spec() -> CompiledSpec:
    return build_spec()


@pytest.fixture
def make_spec() -> Callable[..., CompiledSpec]:
    return build_spec


@pytest.fixture
def make_finding() -> Callable[..., Finding]:
    return build_finding


@pytest.fixture
def make_canonical() -> Callable[[Any], Canonical]:
    return lambda doc: Canonical(doc=doc, hash=canonical_json(doc), raw_doc=doc, raw_hash=canonical_json(doc))


@pytest.fixture
def stub_differ() -> type[StubDiffer]:
    return StubDiffer


@pytest.fixture
def tok() -> Tokeniser:
    return Tokeniser(bytes(range(32)))


@pytest.fixture
def today() -> date:
    return date(2026, 9, 24)
