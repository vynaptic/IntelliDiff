"""Deterministic structural diff of two canonical sides (§8.1 ``diff`` / ``walk``).

The normalised walk compares ``Canonical.doc`` under the field compare modes; two values a
tolerance or similarity accepts are still reported, as NORMALIZATION_ONLY / INFO. The raw walk,
run only when raw hashes differ, compares ``raw_doc`` byte-exact, and its findings at pointers
the normalised walk did not report are NORMALIZATION_ONLY / INFO (``raw_only``): a difference a
normaliser removed is never silent either. Each walk carries the other tree's twin node to fill
the raw (or normalised) values.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Context, Decimal

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from deltasleuth.core.canonical.encoding import canonical_json, exact_json, type_class
from deltasleuth.core.canonical.normalisers import read_decimal
from deltasleuth.core.canonical.paths import (
    Parts,
    member_parts,
    normalise_pattern,
    parts_pattern,
    render_pointer,
)
from deltasleuth.core.jsonio import MAX_EXPONENT
from deltasleuth.core.types import (
    JSON,
    Canonical,
    ChangeType,
    CompiledSpec,
    Counted,
    Direction,
    FieldCompare,
    Finding,
    KeyedMap,
    Severity,
)

NORMALIZATION_ONLY = "NORMALIZATION_ONLY"
FILTER_MEMBERSHIP = "FILTER_MEMBERSHIP"

MISSING, EXTRA, CHANGED = ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B, ChangeType.VALUE_CHANGED
_DIRECTION = {MISSING: Direction.A_ONLY, EXTRA: Direction.B_ONLY}
# Exact arithmetic for tolerance: enough digits for any number jsonio accepts.
_EXACT = Context(prec=2 * MAX_EXPONENT + 100, Emax=MAX_EXPONENT * 4, Emin=-MAX_EXPONENT * 4)
# Scores in [0, 1] compared as computed (scaling to 100 and back can lose the last bit). token_set
# scores the sorted words: word order is ignored, a dropped or added word never is.
_SIMILARITY = {
    "token_set": lambda a, b: fuzz.token_sort_ratio(a, b) / 100,
    "jaro_winkler": JaroWinkler.normalized_similarity,
    "levenshtein_ratio": lambda a, b: fuzz.ratio(a, b) / 100,  # normalised Indel similarity
}
Filtered = dict[str, tuple[str, ...]]


def diff(a: Canonical, b: Canonical, spec: CompiledSpec, *, key: str) -> list[Finding]:
    """All findings for one key whose sides are both COMPLETE, in deterministic order."""
    modes = {normalise_pattern(r.path): r.compare for r in spec.compare.fields if r.compare.mode != "exact"}
    filtered = (a.filtered_members, b.filtered_members)

    def walk(raw: bool) -> _Walk:
        return _Walk(key, spec.compare.null_equals_absent, modes, filtered, raw)

    findings = walk(raw=False).run(a.doc, b.doc, a.raw_doc, b.raw_doc) if a.hash != b.hash else []
    if a.raw_hash != b.raw_hash:
        seen = {f.json_pointer for f in findings}
        findings += [
            replace(f, taxonomy_code=NORMALIZATION_ONLY, severity=Severity.INFO)
            for f in walk(raw=True).run(a.raw_doc, b.raw_doc, a.doc, b.doc)
            if f.json_pointer not in seen
        ]
    return findings


@dataclass
class _Walk:
    """One walk. ``ta`` / ``tb`` are each side's twin nodes, of the same shape except where
    ``null_if_empty`` turned an empty container (which has no children to visit) into null."""

    key: str
    null_equals_absent: bool
    modes: dict[str, FieldCompare]
    filtered: tuple[Filtered, Filtered]
    raw: bool
    findings: list[Finding] = field(default_factory=list)

    def run(self, da: JSON, db: JSON, ta: JSON, tb: JSON) -> list[Finding]:
        self.node(da, db, ta, tb, ())
        return self.findings

    def encode(self, value: JSON) -> str:
        return exact_json(value) if self.raw else canonical_json(value)

    def node(self, da: JSON, db: JSON, ta: JSON, tb: JSON, parts: Parts) -> None:
        kind = type_class(da)
        if kind != type_class(db):  # never coerced
            self.emit(parts, ChangeType.TYPE_CHANGED, (da, db), (ta, tb))
        elif kind == "keyed":
            self.keyed(da, db, ta, tb, parts)
        elif kind == "object":
            self.object(da, db, ta, tb, parts)
        elif kind == "array":
            self.ordered(da, db, ta, tb, parts)
        elif kind == "counted":
            self.counted(da, db, ta, tb, parts)
        elif self.encode(da) != self.encode(db):
            finding = self.emit(parts, CHANGED, (da, db), (ta, tb))
            if not self.raw and self.accepted(parts_pattern(parts), da, db):
                finding.taxonomy_code, finding.severity = NORMALIZATION_ONLY, Severity.INFO

    def keyed(self, da: KeyedMap, db: KeyedMap, ta: KeyedMap, tb: KeyedMap, parts: Parts) -> None:
        """Members by key; members under one key pairwise in canonical order, surplus ones
        missing or extra (``duplicate_key`` when the other side has the key)."""
        for member_key in sorted(da.keys() | db.keys()):
            ma, mb = da.get(member_key, []), db.get(member_key, [])
            twa, twb = ta.get(member_key, []), tb.get(member_key, [])
            for i in range(max(len(ma), len(mb))):
                at = member_parts(parts, member_key, i, len(ma) > 1 or len(mb) > 1)
                if i < len(ma) and i < len(mb):
                    self.node(ma[i], mb[i], twa[i], twb[i], at)
                elif i < len(ma):
                    self.surplus(parts, at, MISSING, (ma[i], None), (twa[i], None), bool(mb))
                else:
                    self.surplus(parts, at, EXTRA, (None, mb[i]), (None, twb[i]), bool(ma))

    def surplus(
        self, array: Parts, at: Parts, change: ChangeType, values: tuple, twins: tuple, dup: bool
    ) -> None:
        finding = self.emit(at, change, values, twins, value_class="duplicate_key" if dup else None)
        # A member the other side's valueFilter removed is FILTER_MEMBERSHIP, not missing or extra.
        other = self.filtered[1 if change is MISSING else 0]
        if finding.member_key in other.get(render_pointer(p for p in array if p[0] != "dup"), ()):
            finding.taxonomy_code, finding.severity = FILTER_MEMBERSHIP, Severity.MEDIUM

    def object(self, da: dict, db: dict, ta: JSON, tb: JSON, parts: Parts) -> None:
        for name in sorted(da.keys() | db.keys()):
            at = (*parts, ("field", name))
            if name in da and name in db:
                self.node(da[name], db[name], ta[name], tb[name], at)
            elif name in da and not (self.null_equals_absent and da[name] is None):
                self.emit(at, MISSING, (da[name], None), (ta[name], None))
            elif name in db and not (self.null_equals_absent and db[name] is None):
                self.emit(at, EXTRA, (None, db[name]), (None, tb[name]))

    def ordered(self, da: list, db: list, ta: JSON, tb: JSON, parts: Parts) -> None:
        """Equal as multisets but reordered: one order_changed. Else position-wise plus surplus."""
        enc_a, enc_b = [self.encode(x) for x in da], [self.encode(y) for y in db]
        if enc_a == enc_b:
            return
        if Counter(enc_a) == Counter(enc_b):
            self.emit(parts, ChangeType.ORDER_CHANGED, (da, db), (ta, tb))
            return
        for i in range(max(len(da), len(db))):
            at = (*parts, ("index", str(i)))
            if i < len(da) and i < len(db):
                self.node(da[i], db[i], ta[i], tb[i], at)
            elif i < len(da):
                self.emit(at, MISSING, (da[i], None), (ta[i], None))
            else:
                self.emit(at, EXTRA, (None, db[i]), (None, tb[i]))

    def counted(self, da: Counted, db: Counted, ta: Counted, tb: Counted, parts: Parts) -> None:
        if da.n != db.n:  # counts are numbers in findings, so Decimals like every number
            self.emit(parts, CHANGED, (Decimal(da.n), Decimal(db.n)), (Decimal(ta.n), Decimal(tb.n)))
        elif self.encode(da.value) != self.encode(db.value):  # raw walk: the representatives differ
            self.emit(parts, CHANGED, (da.value, db.value), (ta.value, tb.value))

    def accepted(self, pattern: str, a: JSON, b: JSON) -> bool:
        """Whether the field's tolerance or similarity mode declares two different values equal."""
        mode = self.modes.get(pattern)
        if mode is None:
            return False
        if mode.mode == "similarity":
            texts = isinstance(a, str) and isinstance(b, str)
            return texts and _SIMILARITY[mode.method](a, b) >= mode.threshold
        x, y = read_decimal(a), read_decimal(b)
        if x is None or y is None:
            return False
        # |a - b| <= max(abs, rel * max(|a|, |b|)); a missing bound counts as 0.
        relative = _EXACT.multiply(Decimal(mode.rel_tol or 0), max(_EXACT.abs(x), _EXACT.abs(y)))
        return _EXACT.abs(_EXACT.subtract(x, y)) <= max(Decimal(mode.abs_tol or 0), relative)

    def emit(
        self, parts: Parts, change: ChangeType, values: tuple, twins: tuple, *, value_class: str | None = None
    ) -> Finding:
        normalised, raw = (twins, values) if self.raw else (values, twins)
        member = max((i for i, (kind, _) in enumerate(parts) if kind == "member"), default=None)
        finding = Finding(
            key=self.key,
            json_pointer=render_pointer(parts),
            path_pattern=parts_pattern(parts),
            change_type=change,
            a_value=normalised[0],
            b_value=normalised[1],
            a_raw=raw[0],
            b_raw=raw[1],
            value_class=value_class,
            member_key=None if member is None else parts[member][1],  # innermost enclosing member
            pointer_parts=parts,
            array_path=None if member is None else parts_pattern(parts[:member]),
            direction=_DIRECTION.get(change, Direction.BOTH),
            raw_only=self.raw,
        )
        self.findings.append(finding)
        return finding
