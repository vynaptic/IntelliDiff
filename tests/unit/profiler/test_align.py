"""Cross-side alignment: renames by value, chance correction, container scopes, valueless paths."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

import numpy as np

from deltasleuth.core.types import Side
from deltasleuth.profiler.align import TAU, assign_with_dummies

from .conftest import draft_of, matches, rows_of


def test_assignment_is_one_to_one_and_leaves_below_tau_unmatched() -> None:
    assert assign_with_dummies(np.array([[0.49]])) == []
    assert assign_with_dummies(np.array([[TAU]])) == [(0, 0)]  # exactly tau is matched
    # Greedy would take (0, 0) = 0.9 and strand row 1; the optimum pairs across.
    assert sorted(assign_with_dummies(np.array([[0.9, 0.8], [0.85, 0.1]]))) == [(0, 1), (1, 0)]
    assert assign_with_dummies(np.array([[0.2, 0.9, 0.7]])) == [(0, 1)]
    assert sorted(assign_with_dummies(np.array([[0.9], [0.95]]))) == [(1, 0)]
    assert assign_with_dummies(np.zeros((0, 3))) == []


def _people(n: int = 60, seed: int = 3) -> tuple[list[Any], list[Any]]:
    rng = random.Random(seed)  # noqa: S311 - seeded test data
    a, b = [], []
    for i in range(n):
        flag = rng.random() < 0.3
        quantities = [Decimal(rng.randint(1, 9)) for _ in range(rng.randint(1, 3))]
        a.append(
            {
                "id": f"P{i:05d}",
                "firstName": f"name{i}",
                "vip": flag,
                "onlyA": rng.random() < 0.5,
                "noise": f"{rng.randrange(10**9)}",
                "items": [{"sku": f"S{i}-{k}", "qty": q} for k, q in enumerate(quantities)],
            }
        )
        b.append(
            {
                "personId": f"P{i:05d}",
                "profile": {"given": f"NAME{i}"},
                "isVip": flag,
                "onlyB": rng.random() < 0.5,
                "other": f"{rng.randrange(10**9)}",
                "lines": [
                    {"item": {"sku": f"S{i}-{k}"}, "quantity": q, "lineId": rng.randrange(10**9)}
                    for k, q in enumerate(quantities)
                ],
                "skus": [f"S{i}-0"],
            }
        )
    return a, b


class TestAlign:
    def test_renames_found_by_values(self) -> None:
        found = matches(draft_of(*_people()))
        assert {p: found.get(p) for p in ("$.id", "$.firstName", "$.vip")} == {
            "$.id": "$.personId",
            "$.firstName": "$.profile.given",
            "$.vip": "$.isVip",
        }

    def test_unrelated_fields_and_chance_agreement_stay_unmatched(self) -> None:
        # onlyA and onlyB agree on about half the pairs by chance alone; the correction removes it.
        draft = draft_of(*_people())
        assert "$.noise" not in matches(draft) and "$.onlyA" not in matches(draft)
        assert {"$.noise", "$.onlyA"} <= set(draft.unmatched[Side.A])
        assert {"$.other", "$.onlyB"} <= set(draft.unmatched[Side.B])
        (row,) = rows_of(draft, "align", "$.onlyA")
        assert row.decision == "unmatched"

    def test_arrays_first_then_fields_inside(self) -> None:
        draft = draft_of(*_people())
        found = matches(draft)
        assert found["$.items"] == "$.lines"
        assert found["$.items[*].sku"] == "$.lines[*].item.sku"
        assert found["$.items[*].qty"] == "$.lines[*].quantity"
        # A top-level b field holding the same sku values is out of scope for fields inside items.
        assert {"$.skus", "$.lines[*].lineId"} <= set(draft.unmatched[Side.B])

    def test_evidence_rows(self) -> None:
        draft = draft_of(*_people())
        (row,) = rows_of(draft, "align", "$.id")
        assert row.decision == "matched" and row.path_b == "$.personId" and row.sample_count == 60
        assert set(row.scores) == {"score", "value", "value_raw", "chance", "members", "name", "runner_up"}
        assert all(isinstance(v, float | None) for v in row.scores.values())
        # A container whose members carry per-side ids still aligns on its shared members.
        (items,) = rows_of(draft, "align", "$.items")
        assert items.decision == "matched" and items.scores["members"] is not None


class TestValuelessPaths:
    def test_always_empty_fields_with_the_same_name_align(self) -> None:
        # No value ever pairs them, so without the name they stayed unmatched on both sides and
        # side b's map dropped them: identical documents then differed on every key.
        a = [
            {
                "id": f"C{i:04d}",
                "closedAt": None,
                "middleName": None,
                "alerts": [],
                "meta": {},
                "sub": {"note": None},
            }
            for i in range(30)
        ]
        b = [
            {
                "id": f"C{i:04d}",
                "closed_at": None,
                "middleName": None,
                "alerts": [],
                "meta": {},
                "sub": {"note": None},
            }
            for i in range(30)
        ]
        draft = draft_of(a, b)
        found = matches(draft)
        assert found["$.closedAt"] == "$.closed_at"  # case and separators aside
        for path in ("$.middleName", "$.alerts", "$.meta", "$.sub.note"):  # null, [], {} and nested
            assert found[path] == path
        assert all(not paths for paths in draft.unmatched.values())
        (row,) = rows_of(draft, "align", "$.closedAt")
        assert "aligned by name only" in row.note
        assert row.scores["value"] is None and row.scores["name"] == 1.0

    def test_valueless_fields_need_the_same_name(self) -> None:
        a = [{"id": f"C{i:04d}", "middleName": None, "alerts": []} for i in range(30)]
        b = [{"id": f"C{i:04d}", "maidenName": None, "alertList": None} for i in range(30)]
        draft = draft_of(a, b)
        assert "$.middleName" not in matches(draft) and "$.alerts" not in matches(draft)
        (row,) = rows_of(draft, "align", "$.middleName")
        assert row.decision == "unmatched" and "only a counterpart with the same name aligns" in row.note

    def test_valueless_leaves_align_whatever_the_emptiness_looks_like(self) -> None:
        a = [{"id": f"C{i:04d}", "alerts": []} for i in range(30)]
        b = [{"id": f"C{i:04d}", "alerts": None} for i in range(30)]
        assert matches(draft_of(a, b))["$.alerts"] == "$.alerts"  # [] vs null is then a finding, not a gap

    def test_a_valueless_field_never_takes_the_counterpart_values_found(self) -> None:
        a = [{"id": f"C{i:04d}", "closedAt": None, "closure": f"2024-01-{1 + i % 28:02d}"} for i in range(30)]
        b = [{"id": f"C{i:04d}", "closedAt": f"2024-01-{1 + i % 28:02d}"} for i in range(30)]
        found = matches(draft_of(a, b))
        assert found["$.closure"] == "$.closedAt" and "$.closedAt" not in found
