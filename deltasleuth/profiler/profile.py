"""Per-path statistics of one side's samples (§8.6).

Every object path is classified record or map as it is walked, so a map's member names (ids)
never become paths: its values are profiled pooled under ``[*]``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from deltasleuth.core.canonical.encoding import type_class
from deltasleuth.core.types import JSON, Side
from deltasleuth.profiler.model import ROOT, EvidenceRow, Path, Score, render
from deltasleuth.profiler.values import decimal_places, light_token, value_shape

# Leaf kinds are compared value by value; containers hold members aligned in their own scope.
LEAF = "leaf"
SCALAR_ARRAY = "scalar_array"
OPAQUE = "opaque"  # mixed, nested-array or always-empty content: compared whole
RECORD = "record"
MAP = "map"
OBJECT_ARRAY = "object_array"
LEAF_KINDS = (LEAF, SCALAR_ARRAY, OPAQUE)
CONTAINER_KINDS = (OBJECT_ARRAY, MAP)

# The map rule (after quicktype and DuckDB): member values share one type AND
# (every name is id-like OR more than MANY_KEYS names OR mean appearance below SPARSE); entropy
# breaks the tie below TIE_APPEARANCE. An address {line1, city, postcode} stays a record.
MANY_KEYS = 50
SPARSE_APPEARANCE = 0.1
TIE_APPEARANCE = 0.5
TIE_ENTROPY = 0.9
# All digits, UUIDs, long hex, a short alphabetic prefix before 3+ digits (ACC00012345, E-0001234),
# e-mail addresses and phone numbers: data, never a field name, however stable across samples.
ID_LIKE = re.compile(
    r"\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}"
    r"|[A-Za-z]{0,6}[-_:]?\d{3,}[A-Za-z]{0,2}|[^@\s]+@[^@\s]+\.[^@\s]+|\+?\d[\d ().-]{5,}\d",
    re.IGNORECASE,
)


class MapDecision(NamedTuple):
    is_map: bool
    reason: str
    instances: int
    scores: dict[str, Score]


def decide_map(objects: Sequence[Mapping[str, JSON]]) -> MapDecision | None:
    """The map rule over every instance of one object path; None (a record) below two names."""
    counts = Counter(name for obj in objects for name in obj)
    n, distinct = len(objects), len(counts)
    if distinct < 2:
        return None
    mean = sum(c / n for c in counts.values()) / distinct
    same_type = len({type_class(v) for obj in objects for v in obj.values() if v is not None}) == 1
    ids = all(ID_LIKE.fullmatch(name) for name in counts)
    entropy = None
    if not same_type:
        is_map, reason = False, "record: member values have different types"
    elif ids:
        is_map, reason = True, "map: every member name is id-like"
    elif distinct > MANY_KEYS:
        is_map, reason = True, f"map: more than {MANY_KEYS} distinct member names"
    elif mean < SPARSE_APPEARANCE:
        is_map, reason = True, f"map: mean member appearance below {SPARSE_APPEARANCE}"
    elif mean < TIE_APPEARANCE:
        total = sum(counts.values())
        entropy = -sum(c / total * math.log2(c / total) for c in counts.values()) / math.log2(distinct)
        is_map = entropy >= TIE_ENTROPY
        reason = (
            f"map: entropy tie-break ({entropy:.2f} >= {TIE_ENTROPY})"
            if is_map
            else f"record: entropy tie-break ({entropy:.2f} < {TIE_ENTROPY})"
        )
    else:
        is_map, reason = False, "record: stable member names"
    scores: dict[str, Score] = {
        "distinct_keys": distinct,
        "mean_appearance": round(mean, 4),
        "same_value_type": same_type,
        "id_like_keys": ids,
        "entropy_norm": None if entropy is None else round(entropy, 4),
    }
    return MapDecision(is_map, reason, n, scores)


class ArrayInstance(NamedTuple):
    """One occurrence of an array: object members for object arrays, every member otherwise."""

    doc: int
    elements: list[JSON]


@dataclass
class PathStats:
    kind: str
    container: Path  # nearest enclosing object array or map (the root otherwise)
    parent_instances: int  # instances of the parent that could hold this path
    instances: int
    docs: int
    nulls: int
    types: Counter[str]
    map_decision: MapDecision | None = None
    # doc -> non-null values (a scalar array's members); in memory only, never exported
    values: dict[int, list[JSON]] = field(default_factory=dict, repr=False)

    def all_values(self) -> list[JSON]:
        return [v for vs in self.values.values() for v in vs]

    def fingerprint(self) -> dict[str, Score]:
        """Example-free statistics: counts, lengths, number scale and string shapes."""
        values = self.all_values()
        strings = [v for v in values if isinstance(v, str)]
        scales = [s for s in map(decimal_places, values) if s is not None]
        shapes = Counter(map(value_shape, strings)).most_common(3)
        return {
            "presence": round(self.instances / self.parent_instances if self.parent_instances else 0.0, 4),
            "docs": self.docs,
            "instances": self.instances,
            "nulls": self.nulls,
            "distinct": len({light_token(v) for v in values if not isinstance(v, dict | list)} - {None}),
            "types": ",".join(f"{t}:{n}" for t, n in sorted(self.types.items())),
            "len_min": min(map(len, strings), default=None),
            "len_max": max(map(len, strings), default=None),
            "scale_max": max(scales, default=None),
            "shapes": ", ".join(f"{shape} x{n}" for shape, n in shapes) or None,
        }


@dataclass
class SideProfile:
    side: Side
    doc_ids: frozenset[int]
    stats: dict[Path, PathStats] = field(default_factory=dict)  # in walk order, parents first
    arrays: dict[Path, list[ArrayInstance]] = field(default_factory=dict)

    def kind(self, path: Path) -> str:
        return self.stats[path].kind

    def children(self, path: Path) -> list[Path]:
        return [p for p in self.stats if len(p) == len(path) + 1 and p[: len(path)] == path]

    def alignable(self, container: Path, kinds: Sequence[str]) -> list[Path]:
        """Paths of ``kinds`` whose nearest enclosing container is ``container``."""
        return [p for p, st in self.stats.items() if p and st.container == container and st.kind in kinds]

    def leaves_under(self, path: Path) -> list[Path]:
        n = len(path)
        return [p for p, st in self.stats.items() if len(p) > n and p[:n] == path and st.kind in LEAF_KINDS]

    def evidence(self) -> list[EvidenceRow]:
        rows = []
        for path, st in self.stats.items():
            paths = (render(path), None) if self.side is Side.A else (None, render(path))
            if path and st.kind in LEAF_KINDS:
                rows.append(EvidenceRow("profile", *paths, st.kind, st.fingerprint(), st.docs))
            elif path and (d := st.map_decision):
                rows.append(
                    EvidenceRow(
                        "map", *paths, "map" if d.is_map else "record", d.scores, d.instances, d.reason
                    )
                )
        return rows


def _kind(objs: list[Any], arrs: list[Any], scalars: bool) -> str:
    if objs and not arrs and not scalars:
        return RECORD
    if arrs and not objs and not scalars:
        elements = [e for arr in arrs for e in arr if e is not None]
        if elements and all(isinstance(e, dict) for e in elements):
            return OBJECT_ARRAY
        return OPAQUE if any(isinstance(e, dict | list) for e in elements) else SCALAR_ARRAY
    return OPAQUE if objs or arrs else LEAF


def _walk(
    prof: SideProfile, path: Path, container: Path, items: list[tuple[int, JSON]], parent_instances: int
) -> None:
    objs = [(d, v) for d, v in items if isinstance(v, dict)]
    arrs = [(d, v) for d, v in items if isinstance(v, list)]
    present = [(d, v) for d, v in items if v is not None]
    kind = _kind([o for _, o in objs], [a for _, a in arrs], len(present) > len(objs) + len(arrs))
    if kind == RECORD and path and not any(o for _, o in objs):
        kind = OPAQUE  # empty in every sample: compared whole, carried like a leaf into side b's map
    decision = decide_map([o for _, o in objs]) if kind == RECORD else None
    if decision and decision.is_map:
        kind = MAP
    stats = PathStats(
        kind,
        container,
        parent_instances,
        len(items),
        len({d for d, _ in items}),
        len(items) - len(present),
        Counter(type_class(v) for _, v in items),
        decision,
    )
    prof.stats[path] = stats

    if kind in (LEAF, OPAQUE, SCALAR_ARRAY):
        for d, v in present:
            members = [e for e in v if e is not None] if kind == SCALAR_ARRAY else [v]
            if members:
                stats.values.setdefault(d, []).extend(members)
    if kind == SCALAR_ARRAY:
        prof.arrays[path] = [ArrayInstance(d, list(arr)) for d, arr in arrs]
    elif kind == OBJECT_ARRAY:
        prof.arrays[path] = [ArrayInstance(d, [e for e in arr if isinstance(e, dict)]) for d, arr in arrs]
        elements = [(inst.doc, e) for inst in prof.arrays[path] for e in inst.elements]
        _walk(prof, (*path, None), path, elements, len(elements))
    elif kind == MAP:
        values = [(d, v) for d, obj in objs for v in obj.values()]
        _walk(prof, (*path, None), path, values, len(values))
    elif kind == RECORD:  # members in first-appearance order, so the draft reads like the samples
        for name in dict.fromkeys(k for _, obj in objs for k in obj):
            members = [(d, obj[name]) for d, obj in objs if name in obj]
            _walk(prof, (*path, name), container, members, len(objs))


def profile_side(side: Side, docs: Sequence[tuple[int, JSON]]) -> SideProfile:
    """Profile one side's selected documents, given as ``(pair index, document)``."""
    prof = SideProfile(side, frozenset(d for d, _ in docs))
    _walk(prof, ROOT, ROOT, list(docs), len(docs))
    return prof
