"""bootstrap() end to end: both generators, output hygiene, arguments, drafts run through the engine."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from deltasleuth.core.canonical.encoding import canonical_json
from deltasleuth.core.canonical.normalisers import NORMALIZER_VERSION
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.jsonio import parse_json
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    CompiledSpec,
    Observation,
    Side,
    SideState,
    TargetCapabilities,
)
from deltasleuth.profiler import DraftSpec, ProfilerError, bootstrap
from deltasleuth.profiler.keys import MAX_NESTED_MULTISETS
from deltasleuth.profiler.model import render
from deltasleuth.profiler.values import light_token

from .conftest import (
    ARGS,
    CUSTOMER_ARGS,
    CUSTOMER_KEYS,
    CUSTOMER_NORMALISERS,
    CUSTOMER_RENAMES,
    CUSTOMER_SELECT,
    CUSTOMER_UNMATCHED,
    LINKER_KEYS,
    LINKER_RENAMES,
    LINKER_SELECT,
    LINKER_UNMATCHED,
    SECRET,
    arrays,
    candidates,
    draft_of,
    matches,
    normalisers,
    rows_of,
    run_map,
    strings_in,
    to_jsonata_input,
)


def _selects(draft: DraftSpec) -> dict[str, str]:
    return {side: draft.spec["sides"][side]["select"] for side in ("a", "b")}


def _selected_keys(draft: DraftSpec) -> dict[str | None, list[str]]:
    return {
        r.path_a: str(r.scores["key"]).split(", ")
        for r in rows_of(draft, "key_candidate")
        if r.decision == "selected"
    }


def _maps(draft: DraftSpec, side: Side) -> set[str]:
    rows = [r for r in rows_of(draft, "map") if r.decision == "map"]
    return {p for r in rows if (p := r.path_a if side is Side.A else r.path_b)}


def _flatten(doc: Any, maps: set[str]) -> dict[str, list[str]]:
    """Canonical-path pattern -> sorted light tokens of the leaves below it."""
    out: dict[str, list[str]] = defaultdict(list)
    stack: list[tuple[Any, tuple[str | None, ...]]] = [(doc, ())]
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            wildcard = render(path) in maps
            stack.extend((v, (*path, None if wildcard else k)) for k, v in value.items())
        elif isinstance(value, list):
            stack.extend((v, (*path, None)) for v in value)
        else:  # JSONata outputs floats; the profiler reads exact numbers
            exact = Decimal(repr(value)) if isinstance(value, float) else value
            out[render(path)].append(str(light_token(exact)))
    return {k: sorted(v) for k, v in out.items()}


def _aligned_leaves(draft: DraftSpec) -> set[str]:
    kinds = {r.path_a: r.note.partition(";")[0] for r in rows_of(draft, "align") if r.decision == "matched"}
    return {
        f"{p}[*]" if kind == "scalar_array" else str(p)
        for p, kind in kinds.items()
        if kind in ("leaf", "opaque", "scalar_array")
    }


def _map_agreement(draft: DraftSpec, pairs: list[Any], select: dict[str, str]) -> float:
    """Share of pairs whose aligned values agree once side b is mapped; its shape must be side a's."""
    maps, aligned = _maps(draft, Side.A), _aligned_leaves(draft)
    a_root, b_root = select["a"].removeprefix("$."), select["b"].removeprefix("$.")
    agree = 0
    for _, doc_a, doc_b in pairs:
        flat_b = _flatten(run_map(draft.spec["sides"]["b"]["map"], doc_b[b_root]), maps)
        flat_a = {k: v for k, v in _flatten(doc_a[a_root], maps).items() if k in aligned}
        assert set(flat_b) <= aligned and set(flat_b) == set(flat_a)
        agree += flat_a == flat_b
    return agree / len(pairs)


class TestCustomer:
    def test_selects(self, customer_draft: DraftSpec) -> None:
        assert _selects(customer_draft) == CUSTOMER_SELECT
        assert "map" not in customer_draft.spec["sides"]["a"]  # identity: canonical names are side a's

    def test_planted_keys_are_suggested_first(self, customer_draft: DraftSpec) -> None:
        assert _selected_keys(customer_draft) == CUSTOMER_KEYS
        rules = arrays(customer_draft)
        for path, key in CUSTOMER_KEYS.items():
            assert rules[path] == {"path": path, "mode": "keyed", "key": key}
        assert rules["$.tags"] == {"path": "$.tags", "mode": "multiset"}

    def test_selected_key_has_the_best_score(self, customer_draft: DraftSpec) -> None:
        for array in ("$.phones", "$.accounts", "$.consents"):
            passing = [r for r in candidates(customer_draft, array).values() if r.decision != "rejected"]
            assert max(passing, key=lambda r: float(r.scores["score"] or 0)).decision == "selected"

    def test_unique_by_accident_fields_lose(self, customer_draft: DraftSpec) -> None:
        accounts = candidates(customer_draft, "$.accounts")
        assert accounts["balance"].decision == "eligible"  # unique and shared, but a measure: it must not win
        assert accounts["accountNo"].decision == accounts["sortCode"].decision == "rejected"
        assert candidates(customer_draft, "$.consents")["updatedAt"].decision == "rejected"

    def test_map_detected_for_id_keys_not_for_address(self, customer_draft: DraftSpec) -> None:
        decisions = {r.path_a: r for r in rows_of(customer_draft, "map") if r.path_a}
        assert decisions["$.limitsByAccount"].decision == "map"
        assert decisions["$.limitsByAccount"].scores["id_like_keys"] is True
        assert decisions["$.correspondenceAddress"].decision == "record"
        assert "$.accountLimits" in _maps(customer_draft, Side.B)
        assert not any(re.search(r"\.\d{8}|\['\d{8}'\]", r.path_a or "") for r in customer_draft.evidence)

    def test_alignment_finds_planted_renames(self, customer_draft: DraftSpec) -> None:
        assert {a: matches(customer_draft).get(a) for a in CUSTOMER_RENAMES} == CUSTOMER_RENAMES

    def test_unrelated_fields_stay_unmatched(self, customer_draft: DraftSpec) -> None:
        assert customer_draft.unmatched == {Side.A: CUSTOMER_UNMATCHED["a"], Side.B: CUSTOMER_UNMATCHED["b"]}

    def test_planted_normalisers(self, customer_draft: DraftSpec) -> None:
        assert normalisers(customer_draft) == CUSTOMER_NORMALISERS

    def test_draft_validates(self, customer_draft: DraftSpec, spec_validator: Draft202012Validator) -> None:
        issues = spec_validator.iter_errors(customer_draft.spec)
        assert [f"{list(e.absolute_path)}: {e.message}" for e in issues] == []

    def test_map_runs_and_produces_side_a_shape(
        self, customer_draft: DraftSpec, customer_pairs: list[Any]
    ) -> None:
        assert _map_agreement(customer_draft, customer_pairs, CUSTOMER_SELECT) == 1.0

    def test_key_block_and_normalizer_version(
        self, customer_draft: DraftSpec, customer_pairs: list[Any]
    ) -> None:
        key = customer_draft.spec["key"]
        assert (key["pattern"], key["type"], key["class"]) == ("^[A-Z][0-9]{8}$", "string", "identifier")
        assert key["source"] == {"kind": "list", "keys": sorted(k for k, _, _ in customer_pairs)}
        # Drafted against the normalisers this code implements, not a stale copy.
        assert customer_draft.spec["normalizerVersion"] == NORMALIZER_VERSION == "1.1.0"


class TestLinker:
    def test_selects_and_planted_keys(self, linker_draft: DraftSpec) -> None:
        assert _selects(linker_draft) == LINKER_SELECT
        assert _selected_keys(linker_draft) == LINKER_KEYS

    def test_internal_ids_rejected_by_cross_side_overlap(self, linker_draft: DraftSpec) -> None:
        row = candidates(linker_draft, "$.linkedEntities")["id"]
        assert row.scores["coverage"] == 1.0 and row.scores["uniqueness"] == 1.0  # passes the hard filters
        assert row.scores["overlap"] == 0.0
        assert row.decision == "rejected" and "cross-side overlap" in row.note

    def test_alignment(self, linker_draft: DraftSpec) -> None:
        assert {a: matches(linker_draft).get(a) for a in LINKER_RENAMES} == LINKER_RENAMES
        assert {side: sorted(paths) for side, paths in linker_draft.unmatched.items()} == {
            Side.A: sorted(LINKER_UNMATCHED["a"]),
            Side.B: sorted(LINKER_UNMATCHED["b"]),
        }

    def test_normalisers(self, linker_draft: DraftSpec) -> None:
        fields = normalisers(linker_draft)
        decimal = [{"decimal": {"scale": 38}}]
        assert fields["$.score"] == fields["$.linkedEntities[*].confidence"] == decimal
        since = [{"timestamp_utc": {"precision": "us", "mode": "truncate"}}]  # date vs midnight: lossless
        assert fields["$.linkedEntities[*].since"] == since
        # The identifier values mix types (phones, e-mails, tax ids, passport numbers): e164 would
        # read a digit run of another type as a phone number, so it is left to review.
        assert "$.identifiers[*].value" not in fields
        assert any("$.identifiers[*].value" in n and "not phone numbers" in n for n in linker_draft.notes)
        assert "$.flagged" not in fields  # identical booleans need nothing

    def test_draft_validates(self, linker_draft: DraftSpec, spec_validator: Draft202012Validator) -> None:
        assert list(spec_validator.iter_errors(linker_draft.spec)) == []

    def test_map_runs_and_produces_side_a_shape(
        self, linker_draft: DraftSpec, linker_pairs: list[Any]
    ) -> None:
        # About 5% of pairs have a link missing on side b (a real discrepancy): shapes still agree.
        assert _map_agreement(linker_draft, linker_pairs, LINKER_SELECT) >= 0.9

    def test_given_key_pattern_is_kept(self, linker_draft: DraftSpec) -> None:
        assert linker_draft.spec["key"]["pattern"] == "^E[0-9]{7}$"
        (row,) = rows_of(linker_draft, "key_pattern")
        assert row.scores["inferred"] is False and row.note == "given"


@pytest.fixture(params=["customer", "linker"])
def case(request: pytest.FixtureRequest) -> tuple[DraftSpec, list[Any]]:
    name = request.param
    return request.getfixturevalue(f"{name}_draft"), request.getfixturevalue(f"{name}_pairs")


class TestHygiene:
    def test_no_sampled_value_or_secret_is_exported(self, case: tuple[DraftSpec, list[Any]]) -> None:
        draft, pairs = case
        keys = {k for k, _, _ in pairs}
        values = {v for _, a, b in pairs for v in strings_in([a, b]) if len(v) >= 6 and v not in keys}
        evidence = json.dumps([r.as_dict() for r in draft.evidence], ensure_ascii=False)
        schemas = json.dumps({s.value: v for s, v in draft.annotated_schemas.items()}, ensure_ascii=False)
        for text in (evidence, draft.yaml_text, schemas, "\n".join(draft.notes)):
            assert [v for v in values if v in text] == []
        assert SECRET not in json.dumps(draft.spec)

    def test_yaml_round_trips_with_comments(self, case: tuple[DraftSpec, list[Any]]) -> None:
        draft, _ = case
        assert yaml.safe_load(draft.yaml_text) == draft.spec
        assert list(yaml.safe_load(draft.yaml_text)) == list(draft.spec)  # stable key order
        assert draft.yaml_text.startswith("# DeltaSleuth ComparisonSpec DRAFT")
        assert "    map: |" in draft.yaml_text  # multi-line text as a block
        assert "\n# Key drafted from the samples" in draft.yaml_text and "  status: [404]" in draft.yaml_text

    def test_annotated_schemas(self, case: tuple[DraftSpec, list[Any]]) -> None:
        draft, pairs = case
        bundle = json.dumps(
            {s.value: v for s, v in draft.annotated_schemas.items()}, sort_keys=True, separators=(",", ":")
        )
        assert f"sha256:{hashlib.sha256(bundle.encode()).hexdigest()}" in draft.yaml_text
        for side, index in ((Side.A, 1), (Side.B, 2)):
            Draft202012Validator.check_schema(draft.annotated_schemas[side])
            validator = Draft202012Validator(draft.annotated_schemas[side])
            for pair in pairs:  # every raw sample fits its side's inferred schema
                assert not list(validator.iter_errors(to_jsonata_input(pair[index])))

    def test_evidence_rows_are_plain(self, case: tuple[DraftSpec, list[Any]]) -> None:
        draft, _ = case
        kinds = {"select", "key_pattern", "profile", "map", "align", "key_candidate", "array", "normalize"}
        assert kinds <= {r.kind for r in draft.evidence}
        for row in draft.evidence:
            json.dumps(row.as_dict())  # plain JSON types only
            assert all(isinstance(v, int | float | str | bool | None) for v in row.scores.values())

    def test_deterministic(self, customer_pairs: list[Any], customer_draft: DraftSpec) -> None:
        again = bootstrap(customer_pairs, **CUSTOMER_ARGS)
        assert again.yaml_text == customer_draft.yaml_text and again.evidence == customer_draft.evidence


def test_x_ds_keywords(customer_draft: DraftSpec) -> None:
    a = customer_draft.annotated_schemas[Side.A]["properties"]["customer"]["properties"]
    b = customer_draft.annotated_schemas[Side.B]["properties"]["data"]["properties"]
    assert a["limitsByAccount"]["x-ds-map"] is True and "additionalProperties" in a["limitsByAccount"]
    assert a["accounts"]["x-ds-array-mode"] == "keyed"
    assert a["accounts"]["x-ds-array-keys"] == ["sortCode", "accountNo"]
    assert b["products"]["x-ds-array-keys"] == ["account.sortCode", "account.number"]
    assert b["profile"]["properties"]["givenName"]["x-ds-align"] == "$.firstName"
    assert a["tags"]["items"]["x-ds-normalize"] == ["upper"]
    assert "x-ds-align" not in a["legacyBranchRef"]


def _small(n: int = 30) -> list[tuple[str, Any, Any]]:
    pairs = []
    for i in range(n):
        key = f"K{i:04d}"
        a = {
            "id": key,
            "scores": {"1001": Decimal(i), "1002": Decimal(i + 1)},
            "person": {"given": f"Given{i}", "family": f"Family{i}"},
            "tags": [f"t{i}"],
            "items": [{"code": f"C{i}-{k}"} for k in range(2)],
        }
        b: dict[str, Any] = {
            "key": key,
            "points": {"1001": Decimal(i), "1002": Decimal(i + 1)},
            "labels": [f"t{i}"],
            "lines": [{"code": f"C{i}-{k}"} for k in range(2)],
        }
        if i % 5:  # every fifth side-b document has no person fields at all
            b["first-name"] = f"Given{i}"
            b["names"] = {"last": f"Family{i}"}
        pairs.append((key, a, b))
    return pairs


class TestSmallCases:
    def test_side_b_map_edge_cases(self) -> None:
        pairs = _small()
        expr = bootstrap(pairs, **ARGS).spec["sides"]["b"]["map"]
        assert '"scores": points' in expr  # a scalar-valued map needs no rebuilding
        assert "`first-name`" in expr  # a name JSONata cannot spell bare
        assert "$exists($keys(" in expr  # person's fields come from two side-b parents
        with_person = run_map(expr, pairs[1][2])
        assert with_person["person"] == {"given": "Given1", "family": "Family1"}
        assert with_person["tags"] == ["t1"]  # a one-member array stays an array
        assert "person" not in run_map(expr, pairs[0][2])  # nothing on b: no empty record
        assert run_map(expr, dict(pairs[1][2], lines=None))["items"] is None  # null is not turned into [{}]
        assert "items" not in run_map(expr, {k: v for k, v in pairs[1][2].items() if k != "lines"})

    def test_names_jsonata_cannot_spell_bare(self) -> None:
        docs_a = [{"id": f"I{i}", "true": f"t{i}", "we`ird": f"w{i}"} for i in range(25)]
        docs_b = [{"key": d["id"], "x": {"true": d["true"]}, "we`ird": d["we`ird"]} for d in docs_a]
        expr = draft_of(docs_a, docs_b).spec["sides"]["b"]["map"]
        assert "x.`true`" in expr and '$lookup($, "we`ird")' in expr  # operators, and not even backticks
        assert run_map(expr, docs_b[1]) == docs_a[1]

    def test_path_patterns_are_the_engines(self) -> None:
        docs = [
            {"id": f"I{i}", "tab\there": f"v{i}", "it's": f"w{i}", "a\\b": f"x{i}", "1abc": f"y{i}"}
            for i in range(25)
        ]
        assert set(matches(draft_of(docs, docs))) == {
            "$.id",
            "$['tab\\there']",
            "$['it\\'s']",
            "$['a\\\\b']",
            "$['1abc']",
        }

    def test_notes_on_few_samples_and_placeholders(self) -> None:
        draft = bootstrap(_small(8), **ARGS)
        assert any("Only 8 pairs" in n for n in draft.notes)
        assert draft.spec["sides"]["a"]["request"] == {"method": "GET", "path": "/{{ key }}"}
        assert any("placeholders" in n for n in draft.notes)

    def test_requests_and_owner(self, spec_validator: Draft202012Validator) -> None:
        requests = {
            Side.A: {"method": "GET", "path": "/v1/things/{{ key }}"},
            Side.B: {"method": "GET", "path": "/v2/things/{{ key }}", "query": {"include": ["x"]}},
        }
        draft = bootstrap(_small(), **ARGS, requests=requests, owner="team-x")
        assert draft.spec["sides"]["a"]["request"] == requests[Side.A]
        assert draft.spec["sides"]["b"]["request"] == requests[Side.B]
        assert draft.spec["metadata"]["owner"] == "team-x"
        assert not any("placeholders" in n for n in draft.notes)
        assert list(spec_validator.iter_errors(draft.spec)) == []

    def test_one_sided_pairs(self) -> None:
        pairs = [*_small(), ("K9998", {"id": "K9998"}, None), ("K9999", None, {"key": "K9999"})]
        draft = bootstrap(pairs, **ARGS)
        assert "K9998" in draft.spec["key"]["source"]["keys"]
        assert draft.yaml_text.count("Samples: 32 pairs; side a 31 documents, side b 31, both 30.") == 1

    def test_repeated_reads_score_stability(self) -> None:
        pairs = _small()
        draft = bootstrap(pairs + pairs[:10], **ARGS)
        assert candidates(draft, "$.items")["code"].scores["stability"] == 1.0
        assert not any("stability (w4) was not scored" in n for n in draft.notes)
        assert len(draft.spec["key"]["source"]["keys"]) == 30

    def test_float_inputs_are_made_exact(self, customer_pairs: list[Any], customer_draft: DraftSpec) -> None:
        floats = [(k, to_jsonata_input(a), to_jsonata_input(b)) for k, a, b in customer_pairs]
        assert bootstrap(floats, **CUSTOMER_ARGS).spec == customer_draft.spec

    def test_non_matching_keys_are_left_out(self) -> None:
        pairs = [*_small(), ("odd key", {"id": "x"}, {"key": "x"})]
        draft = bootstrap(pairs, **ARGS, key_pattern="^K[0-9]{4}$")
        assert "odd key" not in draft.spec["key"]["source"]["keys"]
        assert any("do not match key.pattern" in n for n in draft.notes)

    def test_no_aligned_paths(self) -> None:
        draft = bootstrap([(f"K{i}", {"x": f"a{i}"}, {"y": f"b{i}"}) for i in range(25)], **ARGS)
        assert draft.spec["sides"]["b"]["map"] == "{}"
        assert any("No side-b path aligned" in n for n in draft.notes)

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"spec_name": "Bad Name"}, "spec_name"),
            ({"target_a": "UPPER"}, "target_a"),
            ({"target_b": "-x"}, "target_b"),
            ({"key_name": "CustomerId"}, "key_name"),
            ({"key_pattern": "("}, "does not compile"),
            ({"owner": " "}, "owner"),
            ({"key_pattern": "^Z$"}, "no sampled key matches"),
        ],
    )
    def test_bad_arguments(self, override: dict[str, str], message: str) -> None:
        with pytest.raises(ProfilerError, match=message):
            bootstrap(_small(), **{**ARGS, **override})

    @pytest.mark.parametrize(
        ("pairs", "message"),
        [
            ([], "no samples"),
            ([("k1", {"a": 1}, None)], "side b has no samples"),
            ([("k1", None, {"a": 1})], "side a has no samples"),
            ([("k1", {"a": 1}, None), ("k2", None, {"a": 1})], "both sides"),
            ([("k1", "text", "text")], "unsupported: side a documents are not JSON objects"),
            (
                [(f"k{i}", {f"{i}001": {"v": i}, f"{i}002": {"v": i}}, {"v": i}) for i in range(5)],
                "unsupported: side a documents are maps",
            ),
        ],
    )
    def test_unusable_samples(self, pairs: list[Any], message: str) -> None:
        with pytest.raises(ProfilerError, match=message):
            bootstrap(pairs, **ARGS)

    @pytest.mark.parametrize(
        ("keys", "pattern"),
        [
            (["C00012345", "C10000000"], "^[A-Z][0-9]{8}$"),
            (["E-0001", "X-12345"], "^[A-Z]-[0-9]{4,5}$"),
            (["a.1", "b.22"], "^[a-z]\\.[0-9]{1,2}$"),
            (["abc", "de"], "^[a-z]{2,3}$"),
            (["A1", "1A"], "^[A-Z0-9]{2}$"),
            (["a.b", "C_1"], "^[A-Za-z0-9._]{3}$"),
            (["a-b", "c]d1"], "^[a-z0-9\\-\\]]{3,4}$"),
            (["x", "yy"], "^[a-z]{1,2}$"),
        ],
    )
    def test_key_pattern_from_character_classes(self, keys: list[str], pattern: str) -> None:
        docs = [{"v": f"v{i}"} for i in range(len(keys))]
        assert draft_of(docs, docs, keys=keys).spec["key"]["pattern"] == pattern
        assert all(re.search(pattern, k) for k in keys)

    @pytest.mark.parametrize(
        ("keys", "key_type"),
        [
            (["3f2b8c1e-9d4a-4b6f-8e2a-1c3d5e7f9a0b"], "uuid"),
            (["12", "0"], "integer"),
            (["012"], "string"),
            (["C1"], "string"),
        ],
    )
    def test_key_type(self, keys: list[str], key_type: str) -> None:
        docs = [{"v": f"v{i}"} for i in range(len(keys))]
        assert draft_of(docs, docs, keys=keys).spec["key"]["type"] == key_type


# Drafts run through the engine: what an approved draft would report.

_CAPS = TargetCapabilities(authoritative_absence=AuthoritativeAbsence(status=(404,)))


def _draft(docs_a: list[Any], docs_b: list[Any]) -> DraftSpec:
    parsed = [parse_json(json.dumps(doc).encode())[0] for doc in docs_a + docs_b]
    return draft_of(parsed[: len(docs_a)], parsed[len(docs_a) :], spec_name="engine-check")


def _observe(spec: CompiledSpec, side: Side, doc: Any, key: str) -> SideState:
    obs = Observation(
        key=key,
        side=side,
        http_status=200,
        body=json.dumps(doc).encode(),
        observed_at="2026-09-24T00:00:00Z",
        source="file",
    )
    return observe(obs, spec.sides[side], spec, _CAPS)


def _compare(
    spec: CompiledSpec, doc_a: Any, doc_b: Any, key: str = "K9999"
) -> tuple[str, list[tuple[str, str]]]:
    result = compare_pair(
        key,
        _observe(spec, Side.A, doc_a, key),
        _observe(spec, Side.B, doc_b, key),
        spec,
        today=date(2026, 9, 24),
    )
    return result.outcome.value, sorted((f.path_pattern, f.change_type.value) for f in result.findings)


class TestDraftsThroughTheEngine:
    def test_identical_documents_with_always_empty_fields_all_match(
        self, spec_validator: Draft202012Validator
    ) -> None:
        # Always-null, always-[] and always-{} fields were left out of side b's map only, so every
        # key of identical data was MISMATCH with FIELD_MISSING on each of them.
        docs = [
            {
                "customerId": f"C{i:08d}",
                "name": f"Name {i}",
                "closedAt": None,
                "middleName": None,
                "alerts": [],
                "meta": {},
                "address": {"line2": None},
                "items": [{"code": f"I{i}-{j}", "note": None} for j in range(2)],
            }
            for i in range(30)
        ]
        draft = _draft(docs, docs)
        assert list(spec_validator.iter_errors(draft.spec)) == []
        assert draft.unmatched == {Side.A: [], Side.B: []}
        spec = compile_spec(draft.spec)
        assert {_compare(spec, d, d)[0] for d in docs} == {"MATCH"}
        # The carried-over fields are still compared: a value appearing on one side is reported.
        outcome, findings = _compare(spec, docs[0], dict(docs[0], closedAt="2026-01-01"))
        assert outcome == "MISMATCH" and [p for p, _ in findings] == ["$.closedAt"]

    def test_whole_number_samples_do_not_round_away_fractions(self) -> None:
        # Text vs number quantities, all whole in the samples: the draft once said scale 0.
        draft = _draft(
            [{"orderId": f"O{i}", "qty": str(1 + i % 5)} for i in range(40)],
            [{"orderId": f"O{i}", "qty": 1 + i % 5} for i in range(40)],
        )
        spec = compile_spec(draft.spec)
        assert _compare(spec, {"orderId": "O1", "qty": "2"}, {"orderId": "O1", "qty": 2})[0] == "MATCH"
        for text, number in (("2", 2.5), ("7", 7.49), ("10.504", 10.5)):
            outcome, findings = _compare(
                spec, {"orderId": "O1", "qty": text}, {"orderId": "O1", "qty": number}
            )
            assert outcome == "MISMATCH" and ("$.qty", "value_changed") in findings, (text, number)

    def test_nested_keyless_arrays_stay_cheap_to_canonicalise(
        self, spec_validator: Draft202012Validator
    ) -> None:
        # 18 nested key-less arrays once drafted 18 multiset rules: observing one 256-byte body
        # took 6 GB, because each multiset level multiplies the size of member identities.
        def nested(leaf: Any) -> dict[str, Any]:
            doc: dict[str, Any] = {"v": leaf}
            for _ in range(18):
                doc = {"items": [doc]}
            return doc

        docs = [{"id": f"K{i:03d}", **nested(i % 2 == 0)} for i in range(25)]  # booleans never key
        draft = _draft(docs, docs)
        assert list(spec_validator.iter_errors(draft.spec)) == []
        modes = [rule["mode"] for rule in draft.spec["compare"]["arrays"]]
        assert modes.count("multiset") == MAX_NESTED_MULTISETS and "set" not in modes
        assert any("left ordered" in n for n in draft.notes)
        capped = [r for r in rows_of(draft, "array") if "multiset arrays: left ordered" in r.note]
        assert len(capped) == 18 - MAX_NESTED_MULTISETS and {r.decision for r in capped} == {"ordered"}
        assert capped[0].path_a == "$" + ".items[*]" * MAX_NESTED_MULTISETS + ".items"
        assert f"nested in {MAX_NESTED_MULTISETS} multiset arrays" in capped[0].note
        state = _observe(compile_spec(draft.spec), Side.A, docs[1], "K001")
        assert state.canonical is not None and len(canonical_json(state.canonical.doc)) < 10_000

    def test_empty_member_name_is_never_a_key(self, spec_validator: Draft202012Validator) -> None:
        # The member "" is unique per array, but a key names fields with minLength 1 (schema).
        docs = [{"id": f"K{i}", "rows": [{"": f"r{i}-{j}", "n": j} for j in range(3)]} for i in range(25)]
        draft = _draft(docs, docs)
        assert list(spec_validator.iter_errors(draft.spec)) == []
        assert draft.spec["compare"]["arrays"] == [{"path": "$.rows", "mode": "keyed", "key": ["n"]}]
        assert "" not in candidates(draft, "$.rows")
        assert {_compare(compile_spec(draft.spec), d, d)[0] for d in docs} == {"MATCH"}
