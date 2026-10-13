"""Array modes and identity keys.

An object array's key is the best-scoring minimal member combination that passes the hard
filters on side a (coverage >= 0.99, unique within every array) and that side b also returns;
without one the array is a multiset. A scalar array is a multiset when the sides order the same
values differently.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import combinations, pairwise

from deltasleuth.core.canonical.encoding import member_key_text
from deltasleuth.core.types import JSON
from deltasleuth.profiler.align import Alignment, name_similarity, path_words
from deltasleuth.profiler.model import EvidenceRow, Path, node_at, render, rounded
from deltasleuth.profiler.profile import LEAF, OBJECT_ARRAY, SCALAR_ARRAY, ArrayInstance, SideProfile
from deltasleuth.profiler.values import decimal_places, light_token, name_tokens, numeric_value

W_COVERAGE = 1.0
W_UNIQUENESS = 1.0
W_OVERLAP = 4.0
W_STABILITY = 1.0
W_NAME = 0.5
W_TYPE = 0.5
MIN_COVERAGE = 0.99
MIN_OVERLAP = 0.5
MAX_WIDTH = 3
MAX_TRIPLE_FIELDS = 20  # C(20, 3) = 1140 candidates; wider elements stop at pairs
MAX_NESTED_MULTISETS = 2  # each multiset level multiplies the size of the enclosing members' identities

_NAME_BONUS = frozenset(
    {"id", "code", "key", "uuid", "guid", "number", "no", "num", "nr", "ref", "reference", "identifier"}
)
# Positions, sequence numbers and timestamps are unique by accident; measures also change between reads.
_NAME_PENALTY = frozenset(
    {
        "index",
        "idx",
        "seq",
        "sequence",
        "position",
        "pos",
        "order",
        "rank",
        "ts",
        "timestamp",
        "time",
        "date",
        "at",
        "created",
        "updated",
        "modified",
        "since",
        "amount",
        "balance",
        "total",
        "count",
        "price",
    }
)

KeyTokens = dict[int, set[tuple[str | None, ...]]]


@dataclass(frozen=True)
class KeyCandidate:
    fields: tuple[str, ...]
    fields_b: tuple[Path, ...]  # side-b member paths of the fields that have one
    coverage: float
    uniqueness: float
    overlap: float | None  # None when side b has no aligned array
    stability: float | None  # None without repeated reads
    name_prior: float
    type_prior: float
    b_coverage: float | None
    b_uniqueness: float | None
    tested_instances: int  # arrays with 2+ keyed members, where uniqueness was actually tested
    elements: int
    aligned: bool

    @property
    def passes_hard_filters(self) -> bool:
        # Side a only: keyed mode keeps side b's duplicates visible (duplicate_key), never hides them.
        return self.coverage >= MIN_COVERAGE and self.uniqueness >= 1.0

    @property
    def rejection(self) -> str | None:
        if self.coverage < MIN_COVERAGE:
            return f"coverage {self.coverage:.3f} < {MIN_COVERAGE}"
        if self.uniqueness < 1.0:
            return f"duplicate keys within one array (uniqueness {self.uniqueness:.3f})"
        if self.overlap is not None and self.overlap < MIN_OVERLAP:
            return f"cross-side overlap {self.overlap:.2f} < {MIN_OVERLAP}: values not shared by side b"
        if not self.aligned:
            return "a key field has no aligned counterpart on side b"
        return None

    @property
    def score(self) -> float:
        total = (
            W_COVERAGE * self.coverage
            + W_UNIQUENESS * self.uniqueness
            + W_NAME * self.name_prior
            + W_TYPE * self.type_prior
        )
        if self.overlap is not None:
            total += W_OVERLAP * self.overlap
        if self.stability is not None:
            total += W_STABILITY * self.stability
        return total


@dataclass(frozen=True)
class ArrayDecision:
    path_a: Path
    path_b: Path | None
    mode: str  # keyed | multiset | ordered
    key: tuple[str, ...] = ()
    key_b: tuple[Path, ...] = ()  # side-b member paths of the key fields, relative to the element
    reason: str = ""
    nesting_capped: bool = False  # a multiset left ordered by MAX_NESTED_MULTISETS


@dataclass(frozen=True)
class _ArraySide:
    instances: list[ArrayInstance]
    rels: tuple[Path, ...]  # key member paths, relative to the element

    def parts(self, element: JSON) -> tuple[JSON, ...] | None:
        values = tuple(node_at(element, rel) for rel in self.rels)
        return None if None in values else values

    def coverage(self) -> tuple[float, int]:
        total = sum(len(i.elements) for i in self.instances)
        covered = sum(1 for i in self.instances for e in i.elements if self.parts(e) is not None)
        return (covered / total if total else 0.0), total

    def uniqueness(self) -> tuple[float, int]:
        distinct = count = tested = 0
        for inst in self.instances:
            keys = [member_key_text(list(p)) for p in map(self.parts, inst.elements) if p is not None]
            distinct += len(set(keys))
            count += len(keys)
            tested += len(keys) >= 2
        return (distinct / count if count else 0.0), tested

    def tokens(self) -> KeyTokens:
        out: KeyTokens = {}
        for inst in self.instances:
            bucket = out.setdefault(inst.doc, set())
            bucket.update(tuple(map(light_token, p)) for p in map(self.parts, inst.elements) if p is not None)
        return out


def _jaccard(a: set[tuple[str | None, ...]], b: set[tuple[str | None, ...]]) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def _mean(scores: list[float]) -> float | None:
    return sum(scores) / len(scores) if scores else None


def _overlap(a: _ArraySide, b: _ArraySide, docs: Sequence[int]) -> float:
    ta, tb = a.tokens(), b.tokens()
    scores = [_jaccard(ta.get(d, set()), tb.get(d, set())) for d in docs if ta.get(d) or tb.get(d)]
    return _mean(scores) or 0.0


def _stability(sides: Sequence[_ArraySide], groups: Sequence[Sequence[int]]) -> float | None:
    """Mean Jaccard of the key values between consecutive reads of the same request key."""
    scores = []
    for side in sides:
        tokens, seen = side.tokens(), {i.doc for i in side.instances}
        for group in groups:
            reads = [d for d in group if d in seen]
            scores += [_jaccard(tokens.get(x, set()), tokens.get(y, set())) for x, y in pairwise(reads)]
    return _mean(scores)


def name_prior(name: str) -> float:
    words = set(name_tokens(name))
    return -1.0 if words & _NAME_PENALTY else 1.0 if words & _NAME_BONUS else 0.0


def type_prior(values: Sequence[JSON]) -> float:
    """Strings make good keys, integers fair ones; decimals and booleans are unique by accident."""
    if not values or any(isinstance(v, bool) for v in values):
        return -1.0
    numbers = [numeric_value(v) for v in values]
    if all(n is not None for n in numbers):
        fractional = any(
            (decimal_places(v) or 0) > 0 and n != n.to_integral_value()
            for v, n in zip(values, numbers, strict=True)
            if n is not None
        )
        return -1.0 if fractional else 0.75
    return 1.0 if all(isinstance(v, str) for v in values) else 0.25


class _KeySearch:
    """Key candidates of one side-a object array, measured against its aligned side-b array."""

    def __init__(
        self,
        pa: SideProfile,
        pb: SideProfile,
        alignment: Alignment,
        array_a: Path,
        groups: Sequence[Sequence[int]],
    ) -> None:
        self.array_a, self.groups, self.docs = array_a, groups, alignment.common_docs
        self.array_b = alignment.matches.get(array_a)
        self.elements_a = pa.arrays[array_a]
        # A key names member fields with minLength 1 (spec schema), so "" is never a candidate.
        self.fields = [p[-1] for p in pa.children((*array_a, None)) if p[-1] and _keyable(pa, p)]
        self.values = {
            f: [e[f] for i in self.elements_a for e in i.elements if e.get(f) is not None]
            for f in self.fields
        }
        self.elements_b: list[ArrayInstance] = []
        self.counterparts: dict[str, tuple[Path | None, bool]] = {}
        if self.array_b is not None:
            self.elements_b = pb.arrays[self.array_b]
            self.counterparts = {f: self._counterpart(pb, alignment, self.array_b, f) for f in self.fields}

    def _counterpart(
        self, pb: SideProfile, alignment: Alignment, array_b: Path, field: str
    ) -> tuple[Path | None, bool]:
        """Side-b member path for ``field`` and whether it is the aligned one.

        An unaligned field is measured against the side-b member sharing most of its values (the
        closest name breaks ties), so a rejection says "not shared" rather than just "unaligned".
        """
        element_b = (*array_b, None)
        aligned = alignment.matches.get((*self.array_a, None, field))
        if aligned is not None:
            return aligned[len(element_b) :], True
        words, best = " ".join(name_tokens(field)), None
        for path, st in pb.stats.items():
            if st.kind == LEAF and st.container == array_b:
                rel = path[len(element_b) :]
                candidate = (
                    _overlap(self._side_a((field,)), _ArraySide(self.elements_b, (rel,)), self.docs),
                    name_similarity(words, path_words(rel, ())),
                    rel,
                )
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        return (best[2] if best is not None and (best[0] > 0 or best[1] == 1.0) else None), False

    def _side_a(self, fields: tuple[str, ...]) -> _ArraySide:
        return _ArraySide(self.elements_a, tuple((f,) for f in fields))

    def evaluate(self, fields: tuple[str, ...]) -> KeyCandidate:
        a = self._side_a(fields)
        coverage, elements = a.coverage()
        uniqueness, tested = a.uniqueness()
        overlap = b_cov = b_uniq = None
        fields_b: tuple[Path, ...] = ()
        sides = [a]
        if self.array_b is not None:
            fields_b = tuple(rel for f in fields if (rel := self.counterparts[f][0]) is not None)
            overlap = 0.0
            if len(fields_b) == len(fields):
                b = _ArraySide(self.elements_b, fields_b)
                overlap = _overlap(a, b, self.docs)
                b_cov, b_uniq = b.coverage()[0], b.uniqueness()[0]
                sides.append(b)
        aligned = all(self.counterparts[f][1] for f in fields) if self.array_b is not None else True
        return KeyCandidate(
            fields,
            fields_b,
            coverage,
            uniqueness,
            overlap,
            _stability(sides, self.groups),
            sum(name_prior(f) for f in fields) / len(fields),
            sum(type_prior(self.values[f]) for f in fields) / len(fields),
            b_cov,
            b_uniq,
            tested,
            elements,
            aligned,
        )

    def row(self, c: KeyCandidate, decision: str) -> EvidenceRow:
        note = f"key [{', '.join(c.fields)}]: " + (
            c.rejection or ("highest score" if decision == "selected" else "passes all filters")
        )
        if c.tested_instances == 0:
            note += "; uniqueness untested (no array held two keyed members)"
        if c.passes_hard_filters and c.b_uniqueness is not None and c.b_uniqueness < 1.0:
            note += "; side b repeats members under this key (kept visible as duplicate_key)"
        scores = {
            "key": ", ".join(c.fields),
            "score": rounded(c.score),
            "coverage": rounded(c.coverage),
            "uniqueness": rounded(c.uniqueness),
            "overlap": rounded(c.overlap),
            "stability": rounded(c.stability),
            "name_prior": c.name_prior,
            "type_prior": c.type_prior,
            "b_coverage": rounded(c.b_coverage),
            "b_uniqueness": rounded(c.b_uniqueness),
            "tested_instances": c.tested_instances,
        }
        path_b = None if self.array_b is None else render(self.array_b)
        return EvidenceRow("key_candidate", render(self.array_a), path_b, decision, scores, c.elements, note)


def _keyable(prof: SideProfile, path: Path) -> bool:
    st = prof.stats[path]
    return st.kind == LEAF and not st.types.get("boolean") and st.types["string"] + st.types["number"] > 0


def suggest_key(
    pa: SideProfile, pb: SideProfile, alignment: Alignment, array_a: Path, groups: Sequence[Sequence[int]]
) -> tuple[ArrayDecision, list[EvidenceRow]]:
    """Mode and key for one side-a object array, with a row per candidate worth reviewing."""
    search = _KeySearch(pa, pb, alignment, array_a, groups)
    fields = search.fields
    evaluated: list[KeyCandidate] = []
    eligible: list[KeyCandidate] = []
    qualified: set[tuple[str, ...]] = set()
    for width in range(1, MAX_WIDTH + 1):
        if width == MAX_WIDTH and (eligible or len(fields) > MAX_TRIPLE_FIELDS):
            break  # triples only when no single or pair qualifies, and only while the search is small
        for combo in combinations(fields, width):
            if any(sub in qualified for w in range(1, width) for sub in combinations(combo, w)):
                continue  # not minimal: a smaller key already passes the hard filters
            cand = search.evaluate(combo)
            if width == 1 or cand.passes_hard_filters:
                evaluated.append(cand)
            if cand.passes_hard_filters:
                qualified.add(combo)
                if cand.rejection is None:
                    eligible.append(cand)
    best = min(eligible, key=lambda c: (-c.score, len(c.fields), c.fields), default=None)
    rows = [
        search.row(c, "selected" if c is best else "eligible" if c.rejection is None else "rejected")
        for c in evaluated
    ]
    if best is None:
        if not fields:
            reason = "no scalar members to key on"
        elif len(fields) > MAX_TRIPLE_FIELDS:
            reason = f"no single or pair qualifies; triples not searched over {len(fields)} members"
        else:
            reason = "no candidate passes the hard filters and the overlap check"
        return ArrayDecision(array_a, search.array_b, "multiset", reason=reason), rows
    return ArrayDecision(
        array_a,
        search.array_b,
        "keyed",
        best.fields,
        best.fields_b,
        f"key [{', '.join(best.fields)}] scores {best.score:.2f}",
    ), rows


def _scalar_mode(
    pa: SideProfile, pb: SideProfile, alignment: Alignment, array_a: Path
) -> tuple[ArrayDecision, dict[str, int], int]:
    """Multiset when instance pairs hold the same values in a different order, else ordered."""
    array_b = alignment.matches.get(array_a)
    b_seqs: dict[int, list[list[str | None]]] = {}
    for inst in pb.arrays.get(array_b, []):  # none when b's counterpart is a valueless leaf
        b_seqs.setdefault(inst.doc, []).append(list(map(light_token, inst.elements)))
    same = differs = 0
    for inst in pa.arrays[array_a]:
        seq = list(map(light_token, inst.elements))
        other = next((o for o in b_seqs.get(inst.doc, []) if Counter(o) == Counter(seq)), None)
        if len(seq) > 1 and other is not None:
            same += seq == other
            differs += seq != other
    reason = (
        f"{differs} matching instance pairs list the same values in a different order"
        if differs
        else "no order difference observed; positional comparison (the default) kept"
    )
    decision = ArrayDecision(array_a, array_b, "multiset" if differs else "ordered", reason=reason)
    return decision, {"same_order": same, "order_differs": differs}, same + differs


def suggest_arrays(
    pa: SideProfile, pb: SideProfile, alignment: Alignment, groups: Sequence[Sequence[int]]
) -> tuple[dict[Path, ArrayDecision], list[EvidenceRow]]:
    """A decision for every side-a array, in walk order (parents before children)."""
    decisions: dict[Path, ArrayDecision] = {}
    rows: list[EvidenceRow] = []
    for path, st in pa.stats.items():
        if st.kind == OBJECT_ARRAY:
            decision, key_rows = suggest_key(pa, pb, alignment, path, groups)
            rows += key_rows
            scores: dict[str, int] = {}
            count = sum(len(i.elements) for i in pa.arrays[path])
        elif st.kind == SCALAR_ARRAY:
            decision, scores, count = _scalar_mode(pa, pb, alignment, path)
        else:
            continue
        depth = sum(
            1
            for p, d in decisions.items()
            if d.mode == "multiset" and len(p) < len(path) and path[: len(p)] == p
        )
        if decision.mode == "multiset" and depth >= MAX_NESTED_MULTISETS:
            decision = replace(
                decision,
                mode="ordered",
                nesting_capped=True,
                reason=(
                    f"would be a multiset, but it is nested in {depth} multiset arrays: left ordered, "
                    "because every multiset level multiplies the size of the enclosing members' "
                    "identities; key it or choose its mode after review"
                ),
            )
        decisions[path] = decision
        path_b = None if decision.path_b is None else render(decision.path_b)
        rows.append(EvidenceRow("array", render(path), path_b, decision.mode, scores, count, decision.reason))
    return decisions, rows
