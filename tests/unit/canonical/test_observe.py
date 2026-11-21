"""observe(): side states beyond the golden corpus (details, variable binding, edge cases)."""

from __future__ import annotations

import dataclasses

import pytest

from deltasleuth.core.canonical import predicates
from deltasleuth.core.canonical.errors import ExpressionError
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.spec.expressions import CEL_VARIABLES
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    CompareConfig,
    FieldCompare,
    FieldRule,
    NotFoundRule,
    Pagination,
    SecurityPolicy,
    Side,
    SideConfig,
    SideStateKind,
    TargetCapabilities,
    TokenizeRule,
    TruncationIndicators,
)

INCOMPLETE, ABSENT, COMPLETE = SideStateKind.INCOMPLETE, SideStateKind.ABSENT, SideStateKind.COMPLETE
ABSENT_404 = TargetCapabilities(authoritative_absence=AuthoritativeAbsence(status=(404,)))
NF_404 = NotFoundRule(status=(404,))


def side(**fields) -> SideConfig:
    return SideConfig(side=Side.A, role="legacy", target="t", **fields)


@pytest.fixture
def state(make_spec, make_observation):
    def run(body, cfg=None, caps=None, *, spec=None, **obs_fields):
        cfg = cfg or side()
        return observe(
            make_observation(body, **obs_fields),
            cfg,
            spec or make_spec(side_a=cfg),
            caps or TargetCapabilities(),
        )

    return run


def test_fetch_error_detail_never_carries_the_transport_text(state) -> None:
    result = state(None, status=None, error_class="connect_error", error_detail="secret host x")
    assert (result.kind, result.reason) == (SideStateKind.ERROR, "error_a")
    assert "secret" not in result.detail


class TestNotFound:
    @pytest.mark.parametrize("status", [404, 410])
    def test_an_undeclared_not_found_status_is_unconfirmed(self, state, status) -> None:
        assert state(b"gone", caps=ABSENT_404, status=status).reason == "absence_unconfirmed"

    def test_declared_404_without_a_target_declaration_is_unconfirmed(self, state) -> None:
        result = state(b"", side(not_found=NF_404), status=404)
        assert (result.kind, result.reason) == (INCOMPLETE, "absence_unconfirmed")
        assert "authoritative" in result.detail

    def test_shape_that_errors_is_undeclared_or_meets_html_does_not_match(self, state) -> None:
        caps = TargetCapabilities(
            authoritative_absence=AuthoritativeAbsence(status=(404,), shapes=("unknown", "strict")),
            absence_shapes={"strict": "body.error == 'NOT_FOUND'"},
        )
        for body in ({"other": 1}, b"<html>404</html>"):
            assert state(body, side(not_found=NF_404), caps, status=404).reason == "absence_unconfirmed"

    def test_when_error_is_unparseable_and_is_skipped_for_non_json(self, state) -> None:
        cfg = side(not_found=NotFoundRule(status=(404,), when="body.error == 'nf'"))
        assert state({"a": 1}, cfg, ABSENT_404).reason == "unparseable"
        not_json = state(b"not json", cfg)
        assert not_json.reason == "unparseable" and "JSON" in not_json.detail


class TestNotFoundIn2xx:
    """A 2xx is ABSENT only when notFound.when matches and the target declares that status
    with an absence shape that matches; a 2xx status is never not-found by being listed alone."""

    GRAPHQL = side(
        not_found=NotFoundRule(status=(200,), when="has(body.data) && body.data.character == null"),
        select="$.data.character",
    )
    SHAPED = TargetCapabilities(
        authoritative_absence=AuthoritativeAbsence(status=(200,), shapes=("null_entity",)),
        absence_shapes={"null_entity": "!has(body.errors) && body.data.character == null"},
    )

    def test_declared_by_spec_and_target_is_absent(self, state) -> None:
        assert state({"data": {"character": None}}, self.GRAPHQL, self.SHAPED).kind is ABSENT

    @pytest.mark.parametrize(
        "caps",
        [
            TargetCapabilities(),
            TargetCapabilities(authoritative_absence=AuthoritativeAbsence(status=(200,))),  # no shape
            ABSENT_404,
        ],
    )
    def test_without_a_matching_target_shape_it_is_unconfirmed(self, state, caps) -> None:
        result = state({"data": {"character": None}}, self.GRAPHQL, caps)
        assert (result.kind, result.reason) == (INCOMPLETE, "absence_unconfirmed")

    def test_a_shape_that_does_not_match_is_unconfirmed(self, state) -> None:
        body = {"data": {"character": None}, "errors": [{"message": "timeout"}]}
        assert state(body, self.GRAPHQL, self.SHAPED).reason == "absence_unconfirmed"

    def test_a_listed_2xx_whose_when_is_false_is_a_document(self, state) -> None:
        result = state({"data": {"character": {"id": "1"}}}, self.GRAPHQL, self.SHAPED)
        assert result.kind is COMPLETE and result.canonical.doc == {"id": "1"}

    def test_a_listed_2xx_whose_body_is_not_json_is_unparseable(self, state) -> None:
        assert state(b"<html>", self.GRAPHQL, self.SHAPED).reason == "unparseable"


class TestCelVariables:
    """Predicates bind exactly the names the spec loader declares (an accepted spec compiles)."""

    @pytest.fixture
    def seen(self, monkeypatch) -> list[set[str]]:
        seen: list[set[str]] = []
        real = predicates.test

        def record(source: str, bindings: dict) -> bool:
            seen.append(set(bindings))
            return real(source, bindings)

        monkeypatch.setattr(predicates, "test", record)
        return seen

    def test_not_found_when(self, state, seen) -> None:
        state({"a": 1}, side(not_found=NotFoundRule(status=(404,), when="false")), ABSENT_404)
        assert seen == [set(CEL_VARIABLES["not_found"])]

    def test_absence_shape(self, state, seen) -> None:
        caps = TargetCapabilities(
            authoritative_absence=AuthoritativeAbsence(status=(404,), shapes=("nf",)),
            absence_shapes={"nf": "true"},
        )
        assert state({}, side(not_found=NF_404), caps, status=404).kind is ABSENT
        assert seen == [set(CEL_VARIABLES["absence_shape"])]


class TestParse:
    @pytest.mark.parametrize("body", [b"{bad", b"", None, b"\xff\xfe", b"NaN"])
    def test_unparseable(self, state, body) -> None:
        result = state(body)
        assert (result.kind, result.reason) == (INCOMPLETE, "unparseable")

    def test_missing_status_is_error(self, state) -> None:
        assert state({"a": 1}, status=None).kind is SideStateKind.ERROR

    def test_number_beyond_the_magnitude_limit_is_a_resource_limit(self, state) -> None:
        assert state(b'{"n": 1e99999}').reason == "resource_limit"

    @pytest.mark.parametrize(
        "body", [b'{"name": "Ren\\ud83d"}', b'{"name\\ud800": 1}', b'[{"x": ["\\udfff"]}]']
    )
    def test_lone_surrogate_never_reaches_cel(self, state, body) -> None:
        # The CEL runtime crashed the interpreter on such a string.
        result = state(body, side(not_found=NotFoundRule(status=(404,), when="has(body.missing)")))
        assert result.reason == "unparseable" and "surrogate" in result.detail


class TestTruncation:
    @pytest.mark.parametrize(
        ("indicators", "body", "truncated"),
        [
            (TruncationIndicators(2, "$.items", "$.total"), {"items": [1, 2], "total": 3}, True),  # above cap
            (TruncationIndicators(2, "$.items", "$.total"), {"items": [], "total": 0}, False),
            (TruncationIndicators(2, "$.items", "$.total"), {"items": [1, 2]}, True),  # total promised
            (TruncationIndicators(2, "$.items", "$.total"), {"items": [1, 2], "total": None}, True),
            (TruncationIndicators(2, "$.items", "$.total"), {"items": [1, 2], "total": "2"}, True),
            (TruncationIndicators(2, "$.items", "$.total"), {"items": [1, 2], "total": 1.5}, True),
            (TruncationIndicators(2, "$.items[*]"), {"items": [1, 2]}, True),  # the path selects items
            (TruncationIndicators(2, "$.items[*]"), {"items": [1]}, False),
            (TruncationIndicators(5, count_path="$.total"), {"total": 5}, False),
            (TruncationIndicators(5, count_path="$.total"), {"total": 6}, True),
        ],
    )
    def test_indicators(self, state, indicators, body, truncated) -> None:
        assert (state(body, side(truncation=indicators)).reason == "truncated") is truncated

    def test_detail_names_the_cap_not_the_response_numbers(self, state) -> None:
        result = state(
            {"items": [1], "total": 987654}, side(truncation=TruncationIndicators(2, "$.items", "$.total"))
        )
        assert result.reason == "truncated" and "987654" not in result.detail

    def test_side_indicators_replace_the_targets(self, state) -> None:
        caps = TargetCapabilities(truncation_indicators=TruncationIndicators(cap=1, items_path="$.items"))
        assert (
            state({"items": [1]}, side(truncation=TruncationIndicators(10, "$.items")), caps).kind is COMPLETE
        )

    PAGED = Pagination(kind="cursor", items_path="$.items", cursor_path="$.next", cursor_param="c")

    def test_a_merged_walk_is_judged_by_its_total_but_one_page_by_the_cap(self, state) -> None:
        cfg = side(pagination=self.PAGED, truncation=TruncationIndicators(50, "$.items", "$.total"))
        assert state({"items": list(range(120))}, cfg, pages=3).reason == "truncated"  # total missing
        assert state({"items": list(range(50)), "total": 120}, cfg, pages=1).reason == "truncated"
        items_only = side(pagination=self.PAGED, truncation=TruncationIndicators(50, "$.items"))
        assert state({"items": list(range(150))}, items_only, pages=3).kind is COMPLETE
        assert state({"items": list(range(150))}, items_only, pages=3, pages_eof=False).reason == "truncated"

    def test_offset_paging_is_stable_on_one_page_or_when_declared(self, state) -> None:
        cfg = side(pagination=Pagination(kind="offset", items_path="$.items", page_param="o", size_param="s"))
        assert state({"items": []}, cfg, pages=1).kind is COMPLETE
        assert state({"items": []}, cfg, TargetCapabilities(stable_ordering=True), pages=3).kind is COMPLETE

    FLAGGED = side(truncation_flags=("$.more", "$..cut"))

    def test_flag_true_is_truncated(self, state) -> None:
        result = state({"more": False, "links": [{"cut": False}, {"cut": True}]}, self.FLAGGED)
        assert (result.kind, result.reason) == (INCOMPLETE, "truncated")
        assert result.detail == "the truncation flag $..cut is true"
        # The paths are read in their order: the first that is not false names the flag.
        first = state({"more": True, "links": [{"cut": None}]}, self.FLAGGED, pages_eof=False)
        assert first.detail == "the truncation flag $.more is true"  # and before the paging rules

    @pytest.mark.parametrize(
        "body",
        [
            {"id": 1, "more": False, "links": [{"cut": False}, {"cut": False}]},
            {"id": 1, "links": []},  # no node: nothing is detected
            {"id": 1, "mores": True, "page": {"more": True}},  # another member's name, another place
        ],
    )
    def test_flag_false_or_absent_is_complete(self, state, body) -> None:
        assert state(body, self.FLAGGED).kind is COMPLETE

    @pytest.mark.parametrize("flag", [None, "true", "false", 1, 0, [], {}])
    def test_flag_not_boolean_is_truncated(self, state, flag) -> None:
        result = state({"more": False, "links": [{"cut": False}, {"cut": flag}]}, self.FLAGGED)
        assert (result.kind, result.reason) == (INCOMPLETE, "truncated")
        assert result.detail == "the truncation flag $..cut is not a boolean"

    def test_flags_keep_the_targets_count_rule(self, state) -> None:
        """Flags never turn the count rule off, the side's own or else its target's, and its text wins."""
        caps = TargetCapabilities(truncation_indicators=TruncationIndicators(cap=2, items_path="$.items"))
        by_count = "items returned reach the cap of 2 and no total is reported"
        for more in (True, False):  # the target's rule, whatever the flag says
            cut = state({"items": [1, 2], "more": more}, self.FLAGGED, caps)
            assert (cut.reason, cut.detail) == ("truncated", by_count)
        assert state({"items": [1], "more": False}, self.FLAGGED, caps).kind is COMPLETE
        assert state({"items": [1], "more": True}, self.FLAGGED, caps).reason == "truncated"
        # The side's own count members replace the target's, as they do without flags.
        own = dataclasses.replace(self.FLAGGED, truncation=TruncationIndicators(10, "$.items"))
        assert state({"items": [1, 2], "more": False}, own, caps).kind is COMPLETE
        assert state({"items": [1, 2], "more": True}, own, caps).reason == "truncated"


class TestCanonicalise:
    @pytest.mark.parametrize(
        ("compare", "expected"),
        [
            (
                CompareConfig(
                    fields=(FieldRule("$.balances.*", compare=FieldCompare("tolerance", abs_tol=1)),)
                ),
                "a value cannot be read as a decimal for tolerance compare (at $.balances[*])",
            ),
            (
                CompareConfig(value_filter="value.field == 1"),
                "valueFilter failed: CEL evaluation returned an error (at $.balances[*])",
            ),
        ],
    )
    def test_detail_writes_data_member_names_as_published(self, state, make_spec, compare, expected) -> None:
        # $.balances is tokenised, so its member names are data: the detail never holds them.
        spec = dataclasses.replace(
            make_spec(compare), security=SecurityPolicy(tokenize=(TokenizeRule("$.balances", "email"),))
        )
        result = state({"balances": {"jane.secret@example.org": "n/a"}}, spec.sides[Side.A], spec=spec)
        assert (result.reason, result.detail) == ("unparseable", expected)

    def test_mapping_detail_names_no_payload(self, state) -> None:
        cfg = side(map="{'id': customer.id, 'name': customer.name}")
        result = state({"error": {"code": "DB_DOWN"}}, cfg)
        assert (result.reason, result.detail) == ("unparseable", "map produced an empty document")

    def test_include_matches_are_recorded(self, state, make_spec) -> None:
        spec = make_spec(CompareConfig(include=("$.customer.accounts", "$.accounts")))
        result = state({"accounts": [1]}, spec.sides[Side.A], spec=spec)
        assert result.canonical.include_matched == (False, True)

    def test_spec_defects_propagate(self, state) -> None:
        with pytest.raises(ExpressionError):
            state({"a": 1}, side(map="{ 'a': "))
