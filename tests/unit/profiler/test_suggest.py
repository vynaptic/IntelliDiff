"""Select detection and normaliser suggestions, through the drafted spec, notes and evidence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from deltasleuth.core.canonical.normalisers import apply_chain
from deltasleuth.core.types import Normalizer
from deltasleuth.profiler import DraftSpec

from .conftest import draft_of, matches, normalisers, rows_of

N = 30


class TestSelect:
    @pytest.mark.parametrize(
        ("doc", "select", "note"),
        [
            ({"customer": {"id": "x", "name": "n"}}, "$.customer", "single-member wrapper object"),
            ({"result": {"data": {"id": "x", "n": 1}}}, "$.result.data", "single-member wrapper object"),
            (
                {"data": {"id": "x", "a": 1, "b": 2}, "_links": {"self": "s"}, "request_id": "r"},
                "$.data",
                "payload object beside envelope metadata; not compared: $._links, $.request_id",
            ),
        ],
    )
    def test_wrappers_are_selected(self, doc: dict[str, Any], select: str, note: str) -> None:
        draft = draft_of([doc] * 5, [doc] * 5)
        assert draft.spec["sides"]["a"]["select"] == select
        (row, _) = rows_of(draft, "select")
        assert row.note == note

    def test_payload_leaf_share(self) -> None:
        docs = [{"data": {"id": f"x{i}", "a": 1, "b": 2}, "meta": {"requestId": "r"}} for i in range(5)]
        (row, _) = rows_of(draft_of(docs, docs), "select")
        assert row.decision == "$.data" and row.scores["leaf_share"] == pytest.approx(0.75)

    @pytest.mark.parametrize(
        "docs",
        [
            [{"id": 1, "profile": {"a": 1}}],  # a record field beside the object, not metadata
            [{"data": [1, 2]}],  # array payload
            [{"customer": {"a": 1}}, {"other": {"a": 1}}],  # no shared wrapper
            [{"data": {"a": 1}, "meta": {"x": 1, "y": 2, "z": 3}}],  # payload too small
            [{"data": {"a": 1}, "links": {"self": "x"}, "extra": {"b": 2}}],  # two payload candidates
        ],
    )
    def test_no_select(self, docs: list[Any]) -> None:
        assert draft_of(docs, docs).spec["sides"]["a"]["select"] == "$"


def _draft(values_a: list[Any], values_b: list[Any]) -> DraftSpec:
    draft = draft_of(
        [{"k": f"K{i}", "v": v} for i, v in enumerate(values_a)],
        [{"k": f"K{i}", "w": v} for i, v in enumerate(values_b)],
    )
    assert matches(draft)["$.v"] == "$.w"
    return draft


def _chain(values_a: list[Any], values_b: list[Any]) -> list[Any]:
    return normalisers(_draft(values_a, values_b)).get("$.v", [])


def _engine_chain(draft: DraftSpec) -> list[Normalizer]:
    """The drafted chain as the engine compiles it (every step here is parameterised)."""
    steps = normalisers(draft)["$.v"]
    return [Normalizer(name, dict(params)) for step in steps for name, params in step.items()]


class TestNormalisers:
    @pytest.mark.parametrize(
        ("pattern_a", "pattern_b", "chain"),
        [
            ("CODE{}X", "code{}x", ["upper"]),  # the case side a writes
            ("code{}x", "CODE{}X", ["lower"]),
            ("Code{}x", "CODE{}X", ["casefold"]),
            (" name{} ", "name{}", ["trim"]),
            ("first  name{}", "first name{}", ["collapse_ws"]),
            (" Ada  LOVELACE{}", "ada lovelace{}", ["trim", "collapse_ws", "casefold"]),
            ("40-47-{:02d}", "4047{:02d}", ["digits_only"]),
            ("07700 900{:03d}", "07700900{:03d}", ["digits_only"]),
            ("User{}@Example.org", "user{}@example.org", ["email_basic"]),
            (" user{}@example.org", "User{}@Example.org", ["trim", "email_basic"]),
            ("{}.10", "{}.1", [{"decimal": {"scale": 38}}]),
            ("07700 900{:03d}", "+447700900{:03d}", [{"e164": {"defaultRegion": "GB"}}]),
        ],
    )
    def test_presentation_differences_get_a_chain(
        self, pattern_a: str, pattern_b: str, chain: list[Any]
    ) -> None:
        values_a, values_b = [pattern_a.format(i) for i in range(N)], [pattern_b.format(i) for i in range(N)]
        assert _chain(values_a, values_b) == chain

    def test_evidence_counts(self) -> None:
        draft = _draft([f" CODE {i}" for i in range(N)], [f"code {i}" for i in range(N)])
        (row,) = rows_of(draft, "normalize", "$.v")
        assert row.scores["compared"] == row.scores["differing"] == row.scores["class_case"] == N
        assert row.decision == "trim, upper" and row.sample_count == N

    def test_identical_values_need_nothing(self) -> None:
        draft = _draft([f"v{i}" for i in range(N)], [f"v{i}" for i in range(N)])
        assert normalisers(draft) == {} and rows_of(draft, "normalize") == []

    def test_light_normalisation_without_a_normaliser_is_left_to_review(self) -> None:
        draft = _draft([f"en-GB{i}" for i in range(N)], [f"en_gb{i}" for i in range(N)])
        assert "$.v" not in normalisers(draft)
        assert any("$.v: values match only after light normalisation" in n for n in draft.notes)

    def test_phone_normaliser_needs_every_value_to_be_a_phone(self) -> None:
        # A field mixing identifier types gets no e164, which would read a digit run of
        # another type as a phone number; phones with extensions still count as phones.
        phones_a = [f"07700 900{i:03d}" for i in range(N)] + ["07700 900999 ext. 12"]
        phones_b = [f"+447700900{i:03d}" for i in range(N)] + ["+447700900999;ext=12"]
        assert _chain(phones_a, phones_b) == [{"e164": {"defaultRegion": "GB"}}]
        others = ["jane.doe@example.org", "DEV-7700-900555-A"]
        draft = _draft([*phones_a, *others], [*phones_b, *others])
        assert "$.v" not in normalisers(draft)
        assert any("4 value(s) at this path (both sides) are not phone numbers" in n for n in draft.notes)

    def test_phone_without_gb_majority_leaves_a_note(self) -> None:
        draft = _draft([f"(202) 555-{i:04d}" for i in range(N)], [f"+1202555{i:04d}" for i in range(N)])
        assert "$.v" not in normalisers(draft) and any("defaultRegion" in n for n in draft.notes)
        assert rows_of(draft, "normalize", "$.v")[0].decision == "none"

    def test_decimal_suggestion_does_not_round_away_fractional_differences(self) -> None:
        # Whole-number samples used to draft scale 0, which made "2" vs 2.5 and "7" vs 7.49 equal.
        draft = _draft([str(1 + i % 5) for i in range(N)], [Decimal(1 + i % 5) for i in range(N)])
        chain = _engine_chain(draft)
        assert apply_chain("2", chain) == apply_chain(Decimal(2), chain)  # the writing is reconciled
        assert apply_chain("2", chain) != apply_chain(Decimal("2.5"), chain)
        assert apply_chain("7", chain) != apply_chain(Decimal("7.49"), chain)
        assert apply_chain("10.504", chain) != apply_chain(Decimal("10.5"), chain)
        (row,) = rows_of(draft, "normalize", "$.v")
        assert row.decision == "decimal{scale: 38}"
        assert "keeps every digit" in row.note and "differences finer than it MATCH" in row.note

    def test_decimal_places_beyond_any_scale_draft_nothing(self) -> None:
        places = "1" * 40  # 40 significant decimals: every schema scale would round them
        draft = _draft([f"{i}.{places}" for i in range(N)], [Decimal(f"{i}.{places}") for i in range(N)])
        assert normalisers(draft) == {} and rows_of(draft, "normalize", "$.v")[0].decision == "none"
        assert any("more than decimal keeps" in n for n in draft.notes)

    def test_timestamp_precision_is_the_finest_that_agrees(self) -> None:
        # Side b writes microseconds side a does not have: ms is the finest precision that agrees.
        draft = _draft(
            [f"2024-01-01T00:00:{i:02d}.123Z" for i in range(N)],
            [f"2024-01-01T00:00:{i:02d}.123456Z" for i in range(N)],
        )
        assert normalisers(draft)["$.v"] == [{"timestamp_utc": {"precision": "ms", "mode": "truncate"}}]
        note = rows_of(draft, "normalize", "$.v")[0].note
        assert "lossy" in note and "differences finer than ms become MATCH" in note

    def test_timestamp_precision_keeps_digits_the_samples_do_not_lose(self) -> None:
        # Whole seconds vs ".000": truncating to seconds would hide a later ".500" on either side.
        draft = _draft(
            [f"2024-01-01T00:00:{i:02d}Z" for i in range(N)],
            [f"2024-01-01T00:00:{i:02d}.000Z" for i in range(N)],
        )
        assert normalisers(draft)["$.v"] == [{"timestamp_utc": {"precision": "us", "mode": "truncate"}}]
        chain = _engine_chain(draft)
        assert apply_chain("2024-01-01T00:00:01Z", chain) == apply_chain("2024-01-01T00:00:01.000Z", chain)
        assert apply_chain("2024-01-01T00:00:01Z", chain) != apply_chain("2024-01-01T00:00:01.500Z", chain)
        assert "keeps every digit" in rows_of(draft, "normalize", "$.v")[0].note
