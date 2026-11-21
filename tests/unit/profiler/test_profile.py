"""Per-path profiling (kinds, the map rule, fingerprints) and the annotated schemas, through bootstrap()."""

from __future__ import annotations

import itertools
import string
from decimal import Decimal
from typing import Any

import pytest

from deltasleuth.core.types import Side
from deltasleuth.profiler import DraftSpec, EvidenceRow

from .conftest import draft_of, rows_of


def _row(draft: DraftSpec, kind: str, path: str) -> EvidenceRow:
    (row,) = rows_of(draft, kind, path)
    return row


def _map_row(objects: list[dict[str, Any]]) -> EvidenceRow | None:
    """The map-rule row of ``$.m`` (None when fewer than two member names leave nothing to decide)."""
    docs = [{"k": f"K{i}", "m": obj} for i, obj in enumerate(objects)]
    rows = rows_of(draft_of(docs, docs), "map", "$.m")
    return rows[0] if rows else None


class TestMapRule:
    def test_id_like_names_make_a_map(self) -> None:
        for names in (
            ["10000001", "10000002"],
            ["3f2b8c1e-9d4a-4b6f-8e2a-1c3d5e7f9a0b", "ACC00012345"],
            ["E-0001234", "E-0001235"],
            ["ada@example.org", "+44 7700 900123"],  # stable across samples, still data
        ):
            row = _map_row([{name: {"daily": Decimal(i)} for name in names} for i in range(4)])
            assert row is not None and row.decision == "map" and row.scores["id_like_keys"] is True, names

    def test_many_distinct_names_make_a_map(self) -> None:
        names = ["".join(p) for p in itertools.product(string.ascii_lowercase[:8], repeat=2)][:60]
        row = _map_row([{n: True for n in names[i::3]} for i in range(3)])
        assert row is not None and row.decision == "map" and row.scores["distinct_keys"] == 60
        assert "more than 50" in row.note

    def test_sparse_appearance_makes_a_map(self) -> None:
        names = [f"attr{c}" for c in string.ascii_lowercase[:20]]
        row = _map_row([{names[i]: "x"} for i in range(20)] * 2)
        assert row is not None and row.decision == "map" and float(row.scores["mean_appearance"] or 0) < 0.1

    def test_address_record_is_not_a_map(self) -> None:
        address = {"city": "London", "postcode": "sw1a 1aa", "country": "gb"}
        row = _map_row([{"line1": f"{i} Example Street", **address} for i in range(40)])
        assert row is not None and row.decision == "record"
        assert row.scores["distinct_keys"] == 4 and row.scores["mean_appearance"] == 1.0

    def test_mixed_value_types_are_a_record_even_with_id_keys(self) -> None:
        row = _map_row([{"1001": "x", "1002": Decimal(1)}] * 3)
        assert row is not None and row.decision == "record" and "different types" in row.note

    def test_entropy_breaks_the_tie(self) -> None:
        names = [f"k{c}" for c in "abcdefghij"]
        uniform = _map_row([{names[(i + j) % 10]: "v" for j in range(3)} for i in range(20)])
        assert uniform is not None and uniform.decision == "map"
        assert uniform.scores["entropy_norm"] is not None
        skewed_objects: list[dict[str, Any]] = [{"main": "v"} for _ in range(20)]
        for i, name in enumerate(["opta", "optb", "optc", "optd"]):
            skewed_objects[2 * i][name] = skewed_objects[2 * i + 1][name] = "v"
        skewed = _map_row(skewed_objects)
        assert skewed is not None and skewed.decision == "record"
        assert float(skewed.scores["entropy_norm"] or 1) < 0.9

    def test_one_member_name_leaves_nothing_to_decide(self) -> None:
        assert _map_row([{"only": 1}, {"only": 2}]) is None


DOCS: list[dict[str, Any]] = [
    {
        "id": "A1",
        "limits": {"10000001": {"daily": Decimal(5)}},
        "tags": ["x", "y"],
        "items": [{"code": "c1"}, {"code": "c2"}],
        "mixed": 1,
        "opt": None,
        "addr": {"city": "London", "line1": "1 Road"},
    },
    {
        "id": "A2",
        "limits": {"20000002": {"daily": Decimal(6)}, "20000003": {"daily": Decimal(7)}},
        "tags": [],
        "items": [],
        "mixed": {"deep": True},
        "addr": {"city": "Leeds", "line1": "2 Road"},
    },
]


class TestProfile:
    def test_kinds(self) -> None:
        draft = draft_of(DOCS, DOCS)
        kinds = {r.path_a: r.decision for r in rows_of(draft, "profile") if r.path_a}
        assert kinds == {
            "$.id": "leaf",
            "$.limits[*].daily": "leaf",
            "$.tags": "scalar_array",
            "$.items[*].code": "leaf",
            "$.mixed": "opaque",
            "$.opt": "leaf",
            "$.addr.city": "leaf",
            "$.addr.line1": "leaf",
        }
        assert _row(draft, "map", "$.limits").decision == "map"
        assert _row(draft, "map", "$.addr").decision == "record"
        assert _row(draft, "array", "$.items").sample_count == 2  # the object array's elements

    def test_always_empty_objects_are_compared_whole(self) -> None:
        # A record without members had nothing to align, so side b's map silently dropped it.
        docs = [
            {"meta": {}, "items": [{}], "sometimes": {}},
            {"meta": {}, "items": [{}, {}], "sometimes": {"x": "1"}},
        ]
        kinds = {r.path_a: r.decision for r in rows_of(draft_of(docs, docs), "profile") if r.path_a}
        assert kinds == {"$.meta": "opaque", "$.items[*]": "opaque", "$.sometimes.x": "leaf"}
        assert draft_of([{}], [{}]).spec["sides"]["b"]["map"] == "{}"  # an empty root is still a record

    def test_statistics_pool_map_members_and_hold_no_values(self) -> None:
        draft = draft_of(DOCS, DOCS)
        daily = _row(draft, "profile", "$.limits[*].daily").scores
        assert daily["instances"] == 3 and daily["docs"] == 2  # pooled across ids
        opt = _row(draft, "profile", "$.opt").scores
        assert opt["nulls"] == 1 and opt["presence"] == 0.5 and opt["distinct"] == 0
        assert _row(draft, "profile", "$.tags").scores["distinct"] == 2
        line1 = _row(draft, "profile", "$.addr.line1").scores
        assert line1["distinct"] == 2 and line1["len_min"] == line1["len_max"] == 6
        assert line1["shapes"] == "9 Aa{3} x2"  # "1 Road" and "2 Road" share one shape
        assert "Road" not in str(line1)


class TestAnnotatedSchema:
    def test_decimal_numbers_and_map_collapse(self) -> None:
        docs = [
            {
                "i": Decimal(3 + i),
                "n": Decimal("1.5") if i % 2 else Decimal(i),
                "flag": i % 2 == 0,
                "limits": {f"{id_}{i}": {"daily": Decimal(i)} for id_ in ("1000000", "2000000")},
            }
            for i in range(4)
        ]
        schema = draft_of(docs, docs).annotated_schemas[Side.A]
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        props = schema["properties"]
        assert [props[p]["type"] for p in ("i", "n", "flag")] == ["integer", "number", "boolean"]
        limits = props["limits"]
        assert limits["x-ds-map"] is True and "properties" not in limits and "required" not in limits
        assert limits["additionalProperties"]["properties"]["daily"]["x-ds-align"] == "$.limits[*].daily"

    @pytest.mark.parametrize("side", [Side.A, Side.B])
    def test_annotations_look_through_any_of(self, side: Side) -> None:
        docs = [{"k": f"K{i}", "a": {"x": f"v{i}"} if i % 2 else None} for i in range(10)]
        branches = draft_of(docs, docs).annotated_schemas[side]["properties"]["a"]["anyOf"]
        (record,) = [b for b in branches if b.get("type") == "object"]
        assert record["properties"]["x"]["x-ds-align"] == "$.a.x"
