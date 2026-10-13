"""Profiler types: structured paths, the draft, its evidence rows and the profiler's error."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from deltasleuth.core.canonical.paths import pattern_of
from deltasleuth.core.types import JSON, Side

Score = float | int | str | bool | None
# A path below a side's select root: member names, None for every element or map value ([*]).
Path = tuple[str | None, ...]
ROOT: Path = ()


class ProfilerError(ValueError):
    """The arguments or samples cannot produce a draft. Messages name paths, never values."""


@dataclass(frozen=True)
class EvidenceRow:
    """One suggestion or rejected alternative with its numbers; never a sampled value."""

    kind: str  # select | key_pattern | profile | map | align | array | key_candidate | normalize
    path_a: str | None
    path_b: str | None
    decision: str
    scores: dict[str, Score] = field(default_factory=dict)
    sample_count: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DraftSpec:
    """A draft spec (``spec``, and as commented ``yaml_text``) plus its review material.

    ``annotated_schemas`` holds each side's inferred JSON Schema with ``x-ds-*`` annotations and
    ``unmatched`` the paths without a counterpart on the other side.
    """

    spec: dict[str, Any]
    yaml_text: str
    evidence: list[EvidenceRow]
    annotated_schemas: dict[Side, dict[str, Any]]
    unmatched: dict[Side, list[str]]
    notes: list[str]


def render(path: Path) -> str:
    """The engine's path pattern of ``path`` (``("a", None, "b")`` -> ``$.a[*].b``)."""
    return pattern_of(tuple(0 if seg is None else seg for seg in path))


def rounded(x: float | None) -> float | None:
    """A score as evidence shows it."""
    return None if x is None else round(x, 4)


def node_at(doc: JSON, names: Path) -> JSON:
    """The node at member ``names`` below ``doc``, None when a member is missing."""
    for name in names:
        if not isinstance(doc, dict) or name not in doc:
            return None
        doc = doc[name]
    return doc
