"""Engine properties: source-swap symmetry, determinism, order invariance, encoding stability.

Documents follow one small canonical shape that exercises every array mode, normalisers on key
fields and nested keyed members. Alphabets are tiny on purpose, so independently drawn sides
often share members, keys and values (the interesting cases for a differ).
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from deltasleuth.core.canonical.canonicalise import canonicalise
from deltasleuth.core.canonical.encoding import canonical_json, exact_json
from deltasleuth.core.diff.differ import diff
from deltasleuth.core.jsonio import dumps, parse_json
from deltasleuth.core.types import (
    ArrayRule,
    ChangeType,
    CompareConfig,
    CompiledSpec,
    Direction,
    FieldCompare,
    FieldRule,
    Finding,
    KeyConfig,
    Normalizer,
    Side,
    SideConfig,
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
@given(documents(), documents())
def test_equal_hashes_mean_no_normalised_findings(doc_a: dict, doc_b: dict) -> None:
    spec = property_spec()
    a, b = sides(doc_a, doc_b, spec)
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
