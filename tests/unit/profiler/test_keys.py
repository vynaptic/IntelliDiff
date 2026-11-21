"""Array modes and identity keys, through the drafted compare.arrays and key_candidate evidence."""

from __future__ import annotations

import random
from typing import Any

import pytest

from deltasleuth.core.types import Side
from deltasleuth.profiler.keys import MIN_COVERAGE

from .conftest import arrays, candidates, draft_of, rows_of


def _linked(seed: int = 1, n: int = 60, *, b_ids_shared: bool = False) -> tuple[list[Any], list[Any]]:
    """Linked items keyed by code; ids are random per side unless shared; roles repeat."""
    rng = random.Random(seed)  # noqa: S311 - seeded test data
    a, b = [], []
    for i in range(n):
        members = [
            (f"E{i:03d}{k}", rng.randrange(10**9), rng.choice(["X", "Y"])) for k in range(rng.randint(2, 5))
        ]
        a.append({"items": [{"code": c, "id": vid, "role": r} for c, vid, r in members]})
        b.append(
            {
                "items": [
                    {"target": {"code": c}, "id": vid if b_ids_shared else rng.randrange(10**9), "role": r}
                    for c, vid, r in members
                ]
            }
        )
    return a, b


class TestKeySearch:
    def test_planted_key_wins_and_others_are_rejected_for_their_reason(self) -> None:
        draft = draft_of(*_linked())
        assert arrays(draft)["$.items"] == {"path": "$.items", "mode": "keyed", "key": ["code"]}
        assert draft.annotated_schemas[Side.B]["properties"]["items"]["x-ds-array-keys"] == ["target.code"]
        found = candidates(draft, "$.items")
        assert found["code"].decision == "selected"
        assert found["id"].decision == "rejected" and "cross-side overlap" in found["id"].note  # per-side ids
        assert found["id"].scores["overlap"] == 0.0 and found["id"].scores["uniqueness"] == 1.0
        assert found["role"].decision == "rejected" and "duplicate keys" in found["role"].note

    def test_shared_ids_are_eligible(self) -> None:
        found = candidates(draft_of(*_linked(b_ids_shared=True)), "$.items")
        assert found["id"].decision in ("eligible", "selected")

    def test_only_random_ids_leave_the_array_a_multiset(self) -> None:
        a, b = _linked()
        for doc_a, doc_b in zip(a, b, strict=True):
            for member in doc_a["items"]:
                del member["code"]
            for member in doc_b["items"]:
                del member["target"]
        assert arrays(draft_of(a, b))["$.items"] == {"path": "$.items", "mode": "multiset"}

    def test_coverage_filter(self) -> None:
        a, b = _linked()
        a[0]["items"][0]["code"] = None
        a[1]["items"][0].pop("code")
        total = sum(len(d["items"]) for d in a)
        draft = draft_of(a, b)
        coverage = candidates(draft, "$.items")["code"].scores["coverage"]
        assert coverage == pytest.approx((total - 2) / total)
        if coverage < MIN_COVERAGE:
            assert arrays(draft)["$.items"].get("key") != ["code"]

    def test_composite_key_when_no_single_is_unique(self) -> None:
        rng = random.Random(5)  # noqa: S311 - seeded test data
        a, b = [], []
        for _ in range(50):
            members = {(rng.choice("PQRS"), f"{rng.randrange(40):02d}") for _ in range(rng.randint(2, 6))}
            a.append(
                {"items": [{"type": t, "value": v, "note": rng.choice("xy")} for t, v in sorted(members)]}
            )
            b.append({"items": [{"idType": t, "idValue": v, "note": rng.choice("xy")} for t, v in members]})
        draft = draft_of(a, b)
        assert arrays(draft)["$.items"]["key"] == ["type", "value"]
        keys_b = draft.annotated_schemas[Side.B]["properties"]["items"]["x-ds-array-keys"]
        assert keys_b == ["idType", "idValue"]
        assert all(key.count(",") <= 1 for key in candidates(draft, "$.items"))  # no triple searched

    def test_triples_only_when_nothing_smaller_qualifies(self) -> None:
        docs = [
            {"items": [{"x": x, "y": y, "z": z, "tag": f"t{i}"} for x in "ab" for y in "cd" for z in "ef"]}
            for i in range(30)
        ]
        draft = draft_of(docs, docs)
        assert arrays(draft)["$.items"]["key"] == ["x", "y", "z"]
        assert "x, y, z" in candidates(draft, "$.items")

    def test_side_b_duplicates_do_not_disqualify(self) -> None:
        a, b = _linked(b_ids_shared=True)
        for doc in b:
            doc["items"].append(dict(doc["items"][0]))  # b repeats a member: a defect keyed mode shows
        draft = draft_of(a, b)
        assert arrays(draft)["$.items"]["key"] == ["code"]
        code = candidates(draft, "$.items")["code"]
        assert float(code.scores["b_uniqueness"] or 0) < 1.0 and "duplicate_key" in code.note

    def test_one_sided_array_is_keyed_without_overlap(self) -> None:
        a, _ = _linked()
        draft = draft_of(a, [{"unrelated": "x"}] * len(a))
        assert arrays(draft)["$.items"]["key"] == ["code"]
        code = candidates(draft, "$.items")["code"]
        assert code.path_b is None and code.scores["overlap"] is None

    def test_stability_needs_repeated_reads(self) -> None:
        a, b = _linked(b_ids_shared=True)
        assert candidates(draft_of(a, b), "$.items")["code"].scores["stability"] is None
        # Reads 0 and 1 of the same key: codes stable, ids re-issued on every read.
        a[1] = {"items": [dict(m, id=m["id"] + 1) for m in a[0]["items"]]}
        b[1] = {"items": [dict(m, id=m["id"] + 1) for m in b[0]["items"]]}
        keys = [f"K{i:04d}" for i in range(len(a))]
        keys[1] = keys[0]
        found = candidates(draft_of(a, b, keys=keys), "$.items")
        assert found["code"].scores["stability"] == 1.0 and found["id"].scores["stability"] == 0.0

    def test_uniqueness_untested_note(self) -> None:
        docs = [{"items": [{"code": f"C{i}"}]} for i in range(20)]
        assert "uniqueness untested" in candidates(draft_of(docs, docs), "$.items")["code"].note

    def test_repeating_values_beside_an_empty_member_name_leave_no_key(self) -> None:
        # The member "" is unique per array, but a key names fields with minLength 1 (schema).
        docs = [{"rows": [{"": f"r{i}-{j}", "n": j % 2} for j in range(3)]} for i in range(25)]
        draft = draft_of(docs, docs)
        assert arrays(draft)["$.rows"] == {"path": "$.rows", "mode": "multiset"}
        assert "" not in candidates(draft, "$.rows")


class TestArrayModes:
    def test_modes_for_every_array(self) -> None:
        a = [
            {"tags": ["A", "B"], "fixed": ["x", "y"], "items": [{"k": f"{i}-1"}, {"k": f"{i}-2"}]}
            for i in range(20)
        ]
        b = [
            {"labels": ["b", "a"], "fixed": ["x", "y"], "items": [{"k": f"{i}-2"}, {"k": f"{i}-1"}]}
            for i in range(20)
        ]
        draft = draft_of(a, b)
        assert {path: rule["mode"] for path, rule in arrays(draft).items()} == {
            "$.tags": "multiset",
            "$.items": "keyed",
        }  # $.fixed keeps the default (ordered)
        (tags,) = rows_of(draft, "array", "$.tags")
        assert tags.scores == {"same_order": 0, "order_differs": 20}

    def test_unaligned_scalar_array_stays_ordered(self) -> None:
        draft = draft_of([{"tags": ["A", "B"]}] * 5, [{"other": "x"}] * 5)
        (row,) = rows_of(draft, "array", "$.tags")
        assert row.decision == "ordered" and row.path_b is None and "$.tags" not in arrays(draft)

    def test_keyed_arrays_do_not_count_towards_the_multiset_chain(self) -> None:
        # groups is keyed; lines (no scalar member) and tags (repeated members) have no key.
        lines = [{"tags": [{"t": t}, {"t": t}]} for t in ("x", "y")]
        docs = [{"groups": [{"code": f"G{i}-{g}", "lines": lines} for g in range(2)]} for i in range(25)]
        assert {path: rule["mode"] for path, rule in arrays(draft_of(docs, docs)).items()} == {
            "$.groups": "keyed",
            "$.groups[*].lines": "multiset",
            "$.groups[*].lines[*].tags": "multiset",
        }
