"""Cross-side path alignment: which side-b path carries each side-a path?

Both sides answered the same keys, so corresponding paths hold the same values (DUMAS). A pair
scores 0.8 * value (IDF-weighted Jaccard of light tokens per sample pair, corrected for chance
agreement) + 0.2 * name, solved one-to-one with dummies at tau = 0.5. Containers are
aligned first, then their members inside each aligned pair.
"""

from __future__ import annotations

import json
import math
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import NamedTuple

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from deltasleuth.core.jsonio import to_serialisable
from deltasleuth.core.types import JSON
from deltasleuth.profiler.model import ROOT, EvidenceRow, Path, render, rounded
from deltasleuth.profiler.profile import CONTAINER_KINDS, LEAF_KINDS, OPAQUE, SideProfile
from deltasleuth.profiler.values import light_token, name_tokens

TAU = 0.5
VALUE_WEIGHT = 0.8
NAME_WEIGHT = 0.2
CHANCE_SHIFTS = 5
_MATCH_EPS = 1e-9  # a pair scoring exactly tau is matched ("below 0.5 stays unmatched")
_FORBIDDEN = -1e6
_EMPTY: frozenset[str] = frozenset()
_NAME_ONLY = "aligned by name only: no values observed on either side"

TokenSets = dict[int, frozenset[str]]


@dataclass
class Alignment:
    common_docs: tuple[int, ...]
    matches: dict[Path, Path] = field(default_factory=dict)  # side-a path -> side-b path
    unmatched_a: list[Path] = field(default_factory=list)
    unmatched_b: list[Path] = field(default_factory=list)
    rows: list[EvidenceRow] = field(default_factory=list)


def _token(value: JSON, kind: str) -> str | None:
    if kind != OPAQUE:
        return light_token(value)
    if value in ({}, []):  # an empty container is no evidence of correspondence, like null
        return None
    return "j:" + json.dumps(to_serialisable(value), sort_keys=True, ensure_ascii=False)


class _Tokens:
    """One side's light-token sets per path and paired document; a container pools its leaves."""

    def __init__(self, prof: SideProfile, docs: Sequence[int]) -> None:
        self.prof, self.docs = prof, frozenset(docs)
        self.sets = cache(self._sets)
        self.valueless = cache(self._valueless)

    def _sets(self, path: Path) -> TokenSets:
        st = self.prof.stats[path]
        if st.kind in LEAF_KINDS:
            sets = {
                doc: frozenset(t for v in values if (t := _token(v, st.kind)) is not None)
                for doc, values in st.values.items()
                if doc in self.docs
            }
            return {doc: tokens for doc, tokens in sets.items() if tokens}
        pooled: dict[int, set[str]] = {}
        for leaf in self.prof.leaves_under(path):
            for doc, tokens in self.sets(leaf).items():
                pooled.setdefault(doc, set()).update(tokens)
        return {doc: frozenset(tokens) for doc, tokens in pooled.items()}

    def _valueless(self, path: Path) -> bool:
        """Whether ``path`` is null or empty in every sample of this side, paired or not."""
        st = self.prof.stats[path]
        if st.kind in LEAF_KINDS:
            return all(_token(v, st.kind) is None for v in st.all_values())
        return all(self.valueless(leaf) for leaf in self.prof.leaves_under(path))

    def leaf_sets(self) -> list[TokenSets]:
        return [self.sets(p) for p, st in self.prof.stats.items() if st.kind in LEAF_KINDS]


def _idf(docs: Sequence[int], *sides: _Tokens) -> dict[str, float]:
    """Inverse document frequency of every token either side holds in a paired document."""
    per_doc: dict[int, set[str]] = {doc: set() for doc in docs}
    for side in sides:
        for sets in side.leaf_sets():
            for doc, tokens in sets.items():
                per_doc[doc] |= tokens
    df = Counter(t for tokens in per_doc.values() for t in tokens)
    return {t: math.log(1.0 + len(docs) / c) for t, c in df.items()}


def weighted_jaccard(a: frozenset[str], b: frozenset[str], idf: dict[str, float]) -> float:
    inter = a & b
    return sum(idf[t] for t in inter) / sum(idf[t] for t in a | b) if inter else 0.0


def path_words(path: Path, container: Path) -> str:
    """Words of ``path`` below ``container`` (``account.sortCode`` -> ``account sort code``)."""
    return " ".join(w for seg in path[len(container) :] if seg is not None for w in name_tokens(seg))


def name_similarity(words_a: str, words_b: str) -> float:
    if not (words_a and words_b):  # two map-value leaves have nothing to compare or hold against
        return float(words_a == words_b)
    return fuzz.token_set_ratio(words_a, words_b) / 100.0


def assign_with_dummies(scores: np.ndarray, tau: float = TAU) -> list[tuple[int, int]]:
    """One-to-one row/column pairs, leaving anything scoring below ``tau`` unmatched.

    Every real row and column gets a private dummy at benefit 0 while a real pair is worth
    ``score - tau``, so two real paths pair only when that beats leaving both unmatched.
    """
    n, m = scores.shape
    if n == 0 or m == 0:
        return []
    benefit = np.full((n + m, n + m), _FORBIDDEN)
    benefit[:n, :m] = np.where(scores >= tau, scores - tau + _MATCH_EPS, _FORBIDDEN)
    benefit[np.arange(n), m + np.arange(n)] = 0.0
    benefit[n + np.arange(m), np.arange(m)] = 0.0
    benefit[n:, m:] = 0.0
    rows, cols = linear_sum_assignment(benefit, maximize=True)
    pairs = zip(rows.tolist(), cols.tolist(), strict=True)
    return [(r, c) for r, c in pairs if r < n and c < m and scores[r, c] >= tau]


class _Pair(NamedTuple):
    total: float
    value: float | None  # None: neither path holds a value, so the name decides alone
    value_raw: float | None
    chance: float | None
    members: float | None  # containers: mean best member match, both directions
    name: float
    docs: int


def _unmatched_row(
    paths: tuple[str | None, str | None], scores: np.ndarray, candidates: list[Path], valueless: bool
) -> EvidenceRow:
    note, best = f"no counterpart scores >= {TAU}", None
    if candidates:
        k = int(np.argmax(scores))
        best = rounded(float(scores[k]))
        note += f"; best candidate {render(candidates[k])}"
    if valueless:
        note += "; no values observed, so only a counterpart with the same name aligns"
    return EvidenceRow("align", *paths, "unmatched", {"best_score": best}, 0, note)


class _Aligner:
    def __init__(self, pa: SideProfile, pb: SideProfile) -> None:
        self.pa, self.pb = pa, pb
        self.docs = tuple(sorted(pa.doc_ids & pb.doc_ids))
        self.ta, self.tb = _Tokens(pa, self.docs), _Tokens(pb, self.docs)
        self.idf = _idf(self.docs, self.ta, self.tb)
        self.leaf_value = cache(self._leaf_value)
        self.result = Alignment(self.docs)

    def _mean_jaccard(self, ta: TokenSets, tb: TokenSets, shift: int = 0) -> tuple[float, int]:
        total, n = 0.0, 0
        for i, doc in enumerate(self.docs):
            a, b = ta.get(doc, _EMPTY), tb.get(self.docs[(i + shift) % len(self.docs)], _EMPTY)
            if a or b:
                total += weighted_jaccard(a, b, self.idf)
                n += 1
        return (total / n if n else 0.0), n

    def _corrected(self, ta: TokenSets, tb: TokenSets, raw: float) -> tuple[float, float]:
        """(chance-corrected score, chance): chance is the score on deliberately mismatched pairs."""
        shifts = range(1, min(CHANCE_SHIFTS, len(self.docs) - 1) + 1)
        baseline = [self._mean_jaccard(ta, tb, s)[0] for s in shifts]
        chance = sum(baseline) / len(baseline) if baseline else 0.0
        if chance >= 1.0 - 1e-9:  # constant on both sides: agreement is all the evidence there is
            return raw, chance
        return max(0.0, (raw - chance) / (1.0 - chance)), chance

    def _leaf_value(self, leaf_a: Path, leaf_b: Path) -> float:
        if self.pa.kind(leaf_a) != self.pb.kind(leaf_b):
            return 0.0
        ta, tb = self.ta.sets(leaf_a), self.tb.sets(leaf_b)
        raw = self._mean_jaccard(ta, tb)[0]
        return self._corrected(ta, tb, raw)[0] if raw > 0 else 0.0

    def _member_value(self, container_a: Path, container_b: Path) -> float:
        """Mean best counterpart of each container's leaves in the other, both ways.

        Unlike pooled sets, one unshared member field (an internal id) cannot outweigh four shared ones.
        """
        leaves_a, leaves_b = self.pa.leaves_under(container_a), self.pb.leaves_under(container_b)
        if not leaves_a or not leaves_b:
            return 0.0
        best_a = [max(self.leaf_value(la, lb) for lb in leaves_b) for la in leaves_a]
        best_b = [max(self.leaf_value(la, lb) for la in leaves_a) for lb in leaves_b]
        return (sum(best_a) / len(best_a) + sum(best_b) / len(best_b)) / 2

    def _score(self, path_a: Path, path_b: Path, ca: Path, cb: Path) -> _Pair:
        kind_a, kind_b = self.pa.kind(path_a), self.pb.kind(path_b)
        words_a, words_b = path_words(path_a, ca), path_words(path_b, cb)
        if self.ta.valueless(path_a) and self.tb.valueless(path_b):
            # Only the same name (case and separators aside) pairs paths without values. A leaf's
            # kind is then just how its emptiness is written; containers must agree, because side
            # b's map rebuilds them by kind.
            name = name_similarity(words_a, words_b)
            same = words_a == words_b and (kind_a == kind_b or {kind_a, kind_b} <= set(LEAF_KINDS))
            return _Pair(1.0 if same else NAME_WEIGHT * name, None, None, None, None, name, 0)
        if kind_a != kind_b:
            return _Pair(0.0, 0.0, 0.0, None, None, 0.0, 0)
        ta, tb = self.ta.sets(path_a), self.tb.sets(path_b)
        raw, docs = self._mean_jaccard(ta, tb)
        name = name_similarity(words_a, words_b)
        members = self._member_value(path_a, path_b) if kind_a in CONTAINER_KINDS else None
        ceiling = max(raw, members or 0.0)
        if VALUE_WEIGHT * ceiling + NAME_WEIGHT * name < TAU:  # correction only lowers: tau is out of reach
            return _Pair(VALUE_WEIGHT * ceiling + NAME_WEIGHT * name, ceiling, raw, None, members, name, docs)
        value, chance = self._corrected(ta, tb, raw)
        value = max(value, members or 0.0)
        return _Pair(VALUE_WEIGHT * value + NAME_WEIGHT * name, value, raw, chance, members, name, docs)

    def align_scope(self, ca: Path, cb: Path, kinds: Sequence[str]) -> list[tuple[Path, Path]]:
        items_a, items_b = self.pa.alignable(ca, kinds), self.pb.alignable(cb, kinds)
        grid = [[self._score(a, b, ca, cb) for b in items_b] for a in items_a]
        totals = np.array([[s.total for s in row] for row in grid]).reshape(len(items_a), len(items_b))
        pairs = assign_with_dummies(totals)
        result = self.result
        for i, j in pairs:
            s, kind = grid[i][j], self.pa.kind(items_a[i])
            result.matches[items_a[i]] = items_b[j]
            runner_up = max((float(totals[i, k]) for k in range(len(items_b)) if k != j), default=0.0)
            scores = {
                "score": s.total,
                "value": s.value,
                "value_raw": s.value_raw,
                "chance": s.chance,
                "members": s.members,
                "name": s.name,
                "runner_up": runner_up,
            }
            result.rows.append(
                EvidenceRow(
                    "align",
                    render(items_a[i]),
                    render(items_b[j]),
                    "matched",
                    {k: rounded(v) for k, v in scores.items()},
                    s.docs,
                    kind if s.value is not None else f"{kind}; {_NAME_ONLY}",
                )
            )
        matched_a, matched_b = {i for i, _ in pairs}, {j for _, j in pairs}
        for i, path in enumerate(items_a):
            if i not in matched_a:
                result.unmatched_a.append(path)
                result.rows.append(
                    _unmatched_row((render(path), None), totals[i, :], items_b, self.ta.valueless(path))
                )
        for j, path in enumerate(items_b):
            if j not in matched_b:
                result.unmatched_b.append(path)
                result.rows.append(
                    _unmatched_row((None, render(path)), totals[:, j], items_a, self.tb.valueless(path))
                )
        return [(items_a[i], items_b[j]) for i, j in pairs]


def align(pa: SideProfile, pb: SideProfile) -> Alignment:
    """Align side-a paths to side-b paths (both sides share documents): containers first, then
    the members of each aligned container pair in their own scope."""
    aligner = _Aligner(pa, pb)
    scopes: deque[tuple[Path, Path]] = deque([(ROOT, ROOT)])
    while scopes:
        ca, cb = scopes.popleft()
        scopes.extend(aligner.align_scope(ca, cb, CONTAINER_KINDS))
        aligner.align_scope(ca, cb, LEAF_KINDS)
    return aligner.result
