"""Engine properties: source-swap symmetry, determinism, order invariance, encoding stability, what
the canonical hashes tell apart, and the per-key outcome under a swap of the sources.

Documents follow one small canonical shape that exercises every array mode, normalisers on key
fields and nested keyed members. Alphabets are tiny on purpose, so independently drawn sides
often share members, keys and values (the interesting cases for a differ).
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from deltasleuth.core.canonical.canonicalise import canonicalise
from deltasleuth.core.canonical.encoding import canonical_json, exact_json
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.diff.differ import diff
from deltasleuth.core.jsonio import dumps, parse_json
from deltasleuth.core.types import (
    ArrayRule,
    AuthoritativeAbsence,
    ChangeType,
    CompareConfig,
    CompiledSpec,
    Direction,
    FieldCompare,
    FieldRule,
    Finding,
    KeyConfig,
    KeyResult,
    KnownDiffRule,
    Normalizer,
    NotFoundRule,
    Observation,
    Outcome,
    RuleClassification,
    Side,
    SideConfig,
    TargetCapabilities,
)

TEXT = st.text(alphabet="aAbB ~/@\u00e9\u0301", max_size=4)  # incl. a combining accent (NFC)
KEY_TEXT = st.sampled_from(["k1", "K1", " k2", "k/3", "@k4"])
NUMBERS = st.one_of(
    st.integers(min_value=-3, max_value=3).map(Decimal),
    st.sampled_from([Decimal("1.0"), Decimal("1.50"), Decimal("0.1"), Decimal("12345678901234567890.1")]),
)
SCALARS = st.one_of(st.none(), st.booleans(), NUMBERS, TEXT)
JSON_VALUES = st.recursive(
    SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.sampled_from(["p", "q", "@r", "s/t"]), inner, max_size=3),
    ),
    max_leaves=6,
)

# Local settings (a global profile would leak into other Hypothesis tests).
PROPERTY = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def account(draw: Any) -> dict[str, Any]:
    member: dict[str, Any] = {"id": draw(KEY_TEXT)}
    if draw(st.booleans()):
        member["bal"] = draw(NUMBERS)
    if draw(st.booleans()):
        member["flags"] = draw(st.lists(st.booleans(), max_size=3))
    if draw(st.booleans()):
        member["subs"] = draw(
            st.lists(st.fixed_dictionaries({"no": st.sampled_from("12"), "v": SCALARS}), max_size=3)
        )
    return member


@st.composite
def documents(draw: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    optional = {
        "name": TEXT,
        "amount": NUMBERS,
        "tags": st.lists(TEXT, max_size=4),
        "codes": st.lists(st.sampled_from(["x", "X", "y"]), max_size=4),
        "accounts": st.lists(account(), max_size=4),
        "order": st.lists(st.sampled_from([Decimal(1), Decimal(2), "a"]), max_size=3),
        "extra": JSON_VALUES,
        "nullable": st.none(),
    }
    for name, strategy in optional.items():
        if draw(st.booleans()):
            doc[name] = draw(strategy)
    return doc


@st.composite
def pairs(draw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two documents: drawn apart, or the second a copy of the first in another raw form (padding and
    case the normalisers remove, ruled arrays reordered, a null member), with at most one real change.
    Independent draws seldom agree once normalised, where the hashes and the walks part ways."""
    doc_a = draw(documents())
    if draw(st.booleans()):
        return doc_a, draw(documents())
    doc_b = shuffle_ruled_arrays(doc_a, draw(st.randoms(use_true_random=False)))
    if isinstance(doc_b.get("name"), str) and draw(st.booleans()):
        doc_b["name"] = f" {doc_b['name'].swapcase()} "
    if isinstance(doc_b.get("codes"), list):
        doc_b["codes"] = [code.lower() if isinstance(code, str) else code for code in doc_b["codes"]]
    for member in doc_b.get("accounts") or []:
        if draw(st.booleans()):
            member["id"] = f" {member['id'].lower()}"
    if draw(st.booleans()):
        doc_b["nullable"] = None
    change = draw(st.sampled_from([None, "extra", "amount"]))
    if change is not None:
        doc_b[change] = draw(JSON_VALUES if change == "extra" else NUMBERS)
    return doc_a, doc_b


def property_spec(*, null_equals_absent: bool = False, value_filter: str | None = None) -> CompiledSpec:
    compare = CompareConfig(
        arrays=(
            ArrayRule("$.tags", "set"),
            ArrayRule("$.codes", "multiset"),
            ArrayRule("$.accounts", "keyed", ("id",)),
            ArrayRule("$.accounts[*].subs", "keyed", ("no",)),
        ),
        fields=(
            FieldRule("$.name", (Normalizer("trim"), Normalizer("casefold"))),
            FieldRule("$.accounts[*].id", (Normalizer("trim"), Normalizer("upper"))),
            FieldRule("$.codes[*]", (Normalizer("upper"),)),
            FieldRule("$.amount", compare=FieldCompare("tolerance", abs_tol=Decimal("0.5"))),
            FieldRule("$.accounts[*].bal", compare=FieldCompare("tolerance", rel_tol=Decimal("0.1"))),
        ),
        value_filter=value_filter,
        null_equals_absent=null_equals_absent,
    )
    return CompiledSpec(
        name="property",
        version="1.0.0",
        content_hash="0" * 64,
        normalizer_version="1.0.0",
        key=KeyConfig(name="code"),
        sides={
            Side.A: SideConfig(side=Side.A, role="legacy", target="a"),
            Side.B: SideConfig(side=Side.B, role="new", target="b"),
        },
        compare=compare,
    )


# -- differ properties -----------------------------------------------------------------------------

_MIRROR = {
    ChangeType.MISSING_IN_B: ChangeType.EXTRA_IN_B,
    ChangeType.EXTRA_IN_B: ChangeType.MISSING_IN_B,
}
_MIRROR_DIRECTION = {Direction.A_ONLY: Direction.B_ONLY, Direction.B_ONLY: Direction.A_ONLY}
FILTER = "!(path == '$.accounts[*]' && side == 'b' && has(value.bal) && value.bal < 0)"


def sides(doc_a: dict, doc_b: dict, spec: Any) -> tuple:
    a = canonicalise(copy.deepcopy(doc_a), spec.sides[Side.A], spec, key="K")
    b = canonicalise(copy.deepcopy(doc_b), spec.sides[Side.B], spec, key="K")
    return a, b


def row(f: Finding, *, mirrored: bool = False) -> tuple:
    change = _MIRROR.get(f.change_type, f.change_type) if mirrored else f.change_type
    direction = _MIRROR_DIRECTION.get(f.direction, f.direction) if mirrored else f.direction
    values = (
        (f.b_value, f.a_value, f.b_raw, f.a_raw) if mirrored else (f.a_value, f.b_value, f.a_raw, f.b_raw)
    )
    return (
        f.json_pointer,
        f.path_pattern,
        change,
        direction,
        f.taxonomy_code,
        f.value_class,
        f.member_key,
        f.array_path,
        f.raw_only,
        tuple(exact_json(v) for v in values),
    )


@PROPERTY
@given(documents(), documents(), st.booleans())
def test_source_swap_symmetry(doc_a: dict, doc_b: dict, null_equals_absent: bool) -> None:
    spec = property_spec(null_equals_absent=null_equals_absent)
    a, b = sides(doc_a, doc_b, spec)
    forward = Counter(row(f) for f in diff(a, b, spec, key="K"))
    # Swapping the sources: canonicalise each document as the other side (same rules both sides).
    b_as_a, a_as_b = sides(doc_b, doc_a, spec)
    backward = Counter(row(f, mirrored=True) for f in diff(b_as_a, a_as_b, spec, key="K"))
    assert forward == backward


@PROPERTY
@given(documents(), documents())
def test_filter_membership_is_symmetric(doc_a: dict, doc_b: dict) -> None:
    spec = property_spec(value_filter=FILTER.replace("side == 'b' && ", ""))
    a, b = sides(doc_a, doc_b, spec)
    forward = Counter(row(f) for f in diff(a, b, spec, key="K"))
    backward = Counter(row(f, mirrored=True) for f in diff(b, a, spec, key="K"))
    assert forward == backward


@PROPERTY
@given(documents())
def test_a_document_never_differs_from_itself(doc: dict) -> None:
    spec = property_spec()
    a, b = sides(doc, doc, spec)
    assert a.hash == b.hash and a.raw_hash == b.raw_hash
    assert diff(a, b, spec, key="K") == []


@PROPERTY
@given(documents(), documents())
def test_diff_is_deterministic(doc_a: dict, doc_b: dict) -> None:
    spec = property_spec(value_filter=FILTER)
    first = [row(f) for f in diff(*sides(doc_a, doc_b, spec), spec, key="K")]
    second = [row(f) for f in diff(*sides(doc_a, doc_b, spec), spec, key="K")]
    assert first == second


@PROPERTY
@given(pairs())
def test_equal_hashes_mean_no_normalised_findings(pair: tuple[dict, dict]) -> None:
    spec = property_spec()
    a, b = sides(*pair, spec)
    findings = diff(a, b, spec, key="K")
    if a.hash == b.hash:
        assert all(f.taxonomy_code == "NORMALIZATION_ONLY" for f in findings)
    if a.raw_hash == b.raw_hash and a.hash == b.hash:
        assert findings == []


@PROPERTY
@given(documents(), documents())
def test_pointers_are_unique_per_key(doc_a: dict, doc_b: dict) -> None:
    spec = property_spec()
    findings = diff(*sides(doc_a, doc_b, spec), spec, key="K")
    pointers = [f.json_pointer for f in findings]
    assert len(pointers) == len(set(pointers))
    assert all(canonical_json(f.path_pattern) for f in findings)


@PROPERTY
@given(pairs())
def test_different_hashes_mean_a_finding(pair: tuple[dict, dict]) -> None:
    """The converse of the property above: documents either hash tells apart give at least one finding,
    normalised or raw, so the differ's equal-hash shortcut never hides a difference."""
    spec = property_spec()
    a, b = sides(*pair, spec)
    if (a.hash, a.raw_hash) != (b.hash, b.raw_hash):
        assert diff(a, b, spec, key="K")


def without_null_members(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_null_members(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [without_null_members(v) for v in value]
    return value


@PROPERTY
@given(pairs())
def test_null_equals_absent_hides_only_null_members(pair: tuple[dict, dict]) -> None:
    """With nullEqualsAbsent a null member equals an absent one: documents that still differ once their
    null members are dropped give a finding. The plain hashes do not apply the option."""
    doc_a, doc_b = pair
    spec = property_spec(null_equals_absent=True)
    findings = diff(*sides(doc_a, doc_b, spec), spec, key="K")
    a, b = sides(without_null_members(doc_a), without_null_members(doc_b), property_spec())
    if (a.hash, a.raw_hash) != (b.hash, b.raw_hash):
        assert findings


# -- outcome properties -------------------------------------------------------------------------------

# What a side may answer instead of its document, as (HTTP status, body, error class): an authoritative
# not-found, a status outside success, a fetch that timed out, a body that is not JSON.
UNREAD = [
    (404, '{"error": "not found"}', None),
    (503, "{}", None),
    (None, None, "timeout"),
    (200, '{"p": ', None),
]


@st.composite
def responses(draw: Any) -> tuple[tuple, tuple]:
    """The two sides' responses, each its document (most often) or one of ``UNREAD``."""
    doc_a, doc_b = draw(pairs())

    def answer(doc: dict[str, Any]) -> tuple:
        return draw(st.sampled_from([(200, dumps(doc), None)] * 3 + UNREAD))

    return answer(doc_a), answer(doc_b)


ABSENCE = TargetCapabilities(authoritative_absence=AuthoritativeAbsence((404,)))
# Symmetric in the sides, so it holds whichever source is A.
AMOUNT_RULE = KnownDiffRule(
    "KD-0001", "path == '$.amount'", RuleClassification.ACCEPTED, "Amounts are rounded apart.", "ORG-1001"
)
_MIRROR_OUTCOME = {Outcome.NOT_FOUND_A: Outcome.NOT_FOUND_B, Outcome.NOT_FOUND_B: Outcome.NOT_FOUND_A}
_MIRROR_REASON = {"error_a": "error_b", "error_b": "error_a"}
_MIRROR_CODE = {
    "FIELD_MISSING": "FIELD_EXTRA",
    "FIELD_EXTRA": "FIELD_MISSING",
    "MISSING_DOCUMENT": "EXTRA_DOCUMENT",
    "EXTRA_DOCUMENT": "MISSING_DOCUMENT",
}


def outcome_spec(null_equals_absent: bool) -> CompiledSpec:
    spec = property_spec(null_equals_absent=null_equals_absent)
    return replace(
        spec,
        sides={side: replace(cfg, not_found=NotFoundRule((404,))) for side, cfg in spec.sides.items()},
        known_diffs=(AMOUNT_RULE,),
    )


def judge(response_a: tuple, response_b: tuple, spec: CompiledSpec) -> KeyResult:
    """The key's result when side a gives ``response_a`` and side b ``response_b``."""
    states = []
    for side, (status, body, error) in ((Side.A, response_a), (Side.B, response_b)):
        obs = Observation(
            key="K",
            side=side,
            http_status=status,
            body=None if body is None else body.encode(),
            observed_at="2026-09-24T00:00:00Z",
            error_class=error,
        )
        states.append(observe(obs, spec.sides[side], spec, ABSENCE))
    return compare_pair("K", *states, spec, today=date(2026, 9, 24))


def classified_row(f: Finding, *, mirrored: bool = False) -> tuple:
    change = _MIRROR.get(f.change_type, f.change_type) if mirrored else f.change_type
    direction = _MIRROR_DIRECTION.get(f.direction, f.direction) if mirrored else f.direction
    code = _MIRROR_CODE.get(f.taxonomy_code or "", f.taxonomy_code) if mirrored else f.taxonomy_code
    return (f.json_pointer, f.path_pattern, change, direction, code, f.severity, f.rule_id)


@PROPERTY
@given(responses(), st.booleans())
def test_swapping_the_sources_mirrors_the_outcome(pair: tuple[tuple, tuple], nulls: bool) -> None:
    """MATCH, MISMATCH, KNOWN_DIFF and INCONCLUSIVE stay; NOT_FOUND_A and NOT_FOUND_B, a side's error
    reason, missing and extra members and documents trade places."""
    response_a, response_b = pair
    spec = outcome_spec(nulls)
    forward, backward = judge(response_a, response_b, spec), judge(response_b, response_a, spec)
    assert backward.outcome is _MIRROR_OUTCOME.get(forward.outcome, forward.outcome)
    reason = forward.inconclusive_reason
    assert backward.inconclusive_reason == _MIRROR_REASON.get(reason or "", reason)
    assert Counter(classified_row(f, mirrored=True) for f in backward.findings) == Counter(
        classified_row(f) for f in forward.findings
    )
    assert backward.suppressed_count == forward.suppressed_count


# -- canonicalisation properties ------------------------------------------------------------------


def canon(doc: dict) -> Any:
    spec = property_spec()
    return canonicalise(copy.deepcopy(doc), spec.sides[Side.A], spec, key="K")


def shuffle_ruled_arrays(doc: dict, rnd: Any) -> dict:
    out = copy.deepcopy(doc)
    for name in ("tags", "codes", "accounts"):
        if isinstance(out.get(name), list):
            rnd.shuffle(out[name])
    for member in out.get("accounts") or []:
        if isinstance(member.get("subs"), list):
            rnd.shuffle(member["subs"])
    return out


@PROPERTY
@given(documents(), st.randoms(use_true_random=False))
def test_set_multiset_and_keyed_arrays_are_order_invariant(doc: dict, rnd: Any) -> None:
    original, shuffled = canon(doc), canon(shuffle_ruled_arrays(doc, rnd))
    assert (original.hash, original.raw_hash) == (shuffled.hash, shuffled.raw_hash)


@PROPERTY
@given(documents())
def test_unruled_arrays_stay_order_sensitive(doc: dict) -> None:
    order = doc.get("order")
    if not isinstance(order, list):
        return
    encoded = [canonical_json(x) for x in order]
    if encoded == encoded[::-1]:  # a palindrome reverses to itself
        return
    reversed_doc = {**copy.deepcopy(doc), "order": list(reversed(order))}
    assert canon(doc).hash != canon(reversed_doc).hash


@PROPERTY
@given(documents())
def test_canonicalisation_is_deterministic(doc: dict) -> None:
    first, second = canon(doc), canon(doc)
    assert (first.hash, first.raw_hash, first.filtered_members) == (
        second.hash,
        second.raw_hash,
        second.filtered_members,
    )
    assert exact_json(first.doc) == exact_json(second.doc)


@PROPERTY
@given(documents())
def test_input_is_never_mutated(doc: dict) -> None:
    before = exact_json(doc)
    canon(doc)
    assert exact_json(doc) == before


@PROPERTY
@given(JSON_VALUES)
def test_canonical_json_is_stable_under_copies_and_key_order(value: Any) -> None:
    def reorder(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: reorder(v[k]) for k in reversed(list(v))}
        if isinstance(v, list):
            return [reorder(x) for x in v]
        return v

    assert canonical_json(value) == canonical_json(copy.deepcopy(value)) == canonical_json(reorder(value))


@PROPERTY
@given(JSON_VALUES)
def test_canonical_json_survives_a_serialisation_round_trip(value: Any) -> None:
    reparsed, duplicate = parse_json(dumps(value))
    assert not duplicate
    assert exact_json(reparsed) == exact_json(value)
    assert canonical_json(reparsed) == canonical_json(value)


@PROPERTY
@given(JSON_VALUES)
def test_canonical_json_is_valid_json_text(value: Any) -> None:
    json.loads(canonical_json(value))
