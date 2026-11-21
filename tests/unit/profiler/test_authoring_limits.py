"""Authoring refuses quadratic work and never lets sample names become YAML policy."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from deltasleuth.core.spec import validate_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import Side
from deltasleuth.profiler import ProfilerError, bootstrap
from deltasleuth.profiler import align as alignment
from deltasleuth.profiler.draft import render_yaml
from deltasleuth.profiler.profile import LEAF_KINDS, profile_side

from .conftest import ARGS


def _must_not_run(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("expensive alignment work started before its budget was checked")


def test_wide_scope_is_refused_before_score_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mixed value types prevent the profiler treating this wide record as an id-keyed map.
    doc = {f"field_{i}": str(i) if i % 2 else i for i in range(600)}
    pa, pb = (profile_side(side, [(0, doc)]) for side in Side)
    monkeypatch.setattr(alignment._Aligner, "_score", _must_not_run)
    with pytest.raises(ProfilerError, match=r"alignment.*(limit|budget)"):
        alignment.align(pa, pb)


def test_asymmetric_assignment_is_refused_before_dummy_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    # A skinny input is small, but its private-dummy matrix is square in the sum of the sides.
    scores = np.zeros((1500, 1))
    monkeypatch.setattr(alignment.np, "full", _must_not_run)
    with pytest.raises(ProfilerError, match=r"alignment.*(limit|budget)"):
        alignment.assign_with_dummies(scores)


def test_member_product_is_refused_before_scoring_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {"items": [{f"field_{i}": i for i in range(5)}]}
    pa, pb = (profile_side(side, [(0, doc)]) for side in Side)
    aligner = alignment._Aligner(pa, pb)
    aligner.budget.remaining = 8
    aligner.leaf_value = _must_not_run
    with pytest.raises(ProfilerError, match=r"alignment.*(limit|budget)"):
        aligner._member_value(("items",), ("items",))


def test_member_product_bounds_retained_cache_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {"items": [{f"field_{i}": i for i in range(5)}]}
    pa, pb = (profile_side(side, [(0, doc)]) for side in Side)
    monkeypatch.setattr(alignment, "MAX_ALIGNMENT_LEAF_PAIRS", 8)
    aligner = alignment._Aligner(pa, pb)
    aligner.leaf_value = _must_not_run
    with pytest.raises(ProfilerError, match="alignment"):
        aligner._member_value(("items",), ("items",))


def test_leaf_cache_cap_is_shared_and_counts_only_new_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {"one": [{"x": 1}], "two": [{"x": 2}], "three": [{"x": 3}]}
    pa, pb = (profile_side(side, [(0, doc)]) for side in Side)
    monkeypatch.setattr(alignment, "MAX_ALIGNMENT_LEAF_PAIRS", 2)
    aligner = alignment._Aligner(pa, pb)
    for _ in range(3):
        assert aligner._member_value(("one",), ("one",)) == 1.0
    assert aligner._member_value(("two",), ("two",)) == 1.0
    assert aligner.leaf_value.cache_info().currsize == 2
    with pytest.raises(ProfilerError, match="alignment"):
        aligner._member_value(("three",), ("three",))
    assert aligner.leaf_value.cache_info().currsize == 2


def test_work_budget_accumulates_across_small_scopes() -> None:
    doc = {"one": [{"x": 1}], "two": [{"x": 2}], "three": [{"x": 3}]}
    pa, pb = (profile_side(side, [(0, doc)]) for side in Side)
    aligner = alignment._Aligner(pa, pb)
    aligner.budget.remaining = 22  # two one-token pairs, including their set operations
    assert aligner._member_value(("one",), ("one",)) == 1.0
    assert aligner._member_value(("two",), ("two",)) == 1.0
    with pytest.raises(ProfilerError, match="alignment"):
        aligner._member_value(("three",), ("three",))


def _array_fields(size: int) -> dict[str, Any]:
    return {**{f"field_{i}": list(range(i * size, (i + 1) * size)) for i in range(4)}, "kind": "record"}


def test_array_token_volume_is_bounded_with_few_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only 25 candidate pairs, but 32,000 token visits before chance correction in the old code.
    doc = _array_fields(1000)
    pa, pb = (profile_side(side, [(0, doc)]) for side in Side)
    monkeypatch.setattr(alignment, "MAX_ALIGNMENT_WORK", 10_000)
    with pytest.raises(ProfilerError, match="alignment"):
        alignment.align(pa, pb)


@pytest.mark.parametrize("operation", ["scope", "shifted", "members", "pooled"])
def test_token_work_is_charged_after_leaf_sets_are_cached(operation: str) -> None:
    doc = _array_fields(1000)
    if operation in ("members", "pooled"):
        doc = {"items": [doc]}
    pa, pb = (profile_side(side, [(0, doc), (1, doc)]) for side in Side)
    aligner = alignment._Aligner(pa, pb)
    # Construction has its own allowance. These are real comparisons/copies of cached token sets.
    aligner.budget.remaining = 10_000 if operation == "scope" else 1000
    with pytest.raises(ProfilerError, match="alignment"):
        if operation == "scope":
            aligner.align_scope((), (), LEAF_KINDS)
        elif operation == "shifted":
            aligner._mean_jaccard(aligner.ta.sets(("field_0",)), aligner.tb.sets(("field_0",)), shift=1)
        elif operation == "members":
            aligner._member_value(("items",), ("items",))
        else:
            aligner.ta.sets(("items",))
    assert aligner.budget.remaining >= 0


def test_small_array_token_alignment_keeps_its_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _array_fields(20)
    pa, pb = (profile_side(side, [(0, doc)]) for side in Side)
    monkeypatch.setattr(alignment, "MAX_ALIGNMENT_WORK", 10_000)
    result = alignment.align(pa, pb)
    assert result.matches == {(name,): (name,) for name in doc}


def test_draft_key_patterns_use_the_runtime_ascii_and_scoped_unicode_contract() -> None:
    pairs = [("é", {"id": "é", "v": 1}, {"id": "é", "v": 1})]
    with pytest.raises(ProfilerError, match="no sampled key matches"):
        bootstrap(pairs, **ARGS, key_pattern=r"^\w+$")
    draft = bootstrap(pairs, **ARGS, key_pattern=r"(?u:^\w+$)")
    assert draft.spec["key"]["source"]["keys"] == ["é"]
    assert not [issue for issue in validate_spec(draft.spec) if issue.severity == "error"]


@pytest.mark.parametrize("key", ["", ".", "..", "a\nb", "a\rb", "a\u2028b", "a\u2029b"])
def test_drafts_do_not_include_keys_their_spec_cannot_request(key: str) -> None:
    with pytest.raises(ProfilerError, match="no sampled key matches"):
        bootstrap([(key, {"v": 1}, {"v": 1})], **ARGS, key_pattern=".*")


@pytest.mark.parametrize("breaking", ["\n", "\r", "\r\n", "\u0085", "\u2028", "\u2029"])
def test_all_yaml_comment_breaks_stay_comments(breaking: str) -> None:
    hostile = f"source{breaking}security: {{unlisted: clear}}{breaking}#"
    spec = {"metadata": {"name": "draft"}, "sides": {"a": {"select": hostile}}}
    text = render_yaml(spec, [hostile], {"sides": [hostile]})
    assert parse_yaml_text(text) == spec
    comments = [line for line in text.split("\n") if line.startswith("#")]
    assert not any(ch in line for line in comments for ch in "\r\u0085\u2028\u2029")


@pytest.mark.parametrize("breaking", ["\u0085", "\u2028", "\u2029"])
def test_sample_wrapper_name_cannot_inject_draft_policy(breaking: str) -> None:
    wrapper = f"{breaking}security: {{unlisted: clear}}{breaking}#"
    pairs = [("K0001", {"id": "K0001", "v": 1}, {wrapper: {"id": "K0001", "v": 1}})]
    draft = bootstrap(pairs, **ARGS)
    assert parse_yaml_text(draft.yaml_text) == draft.spec
    assert "security" not in draft.spec
