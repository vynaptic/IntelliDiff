"""Number expansion is bounded before rendering, at every source of exact numbers."""

from decimal import Decimal

import pytest

from deltasleuth.core import jsonio
from deltasleuth.core.canonical.convert import convert
from deltasleuth.core.canonical.encoding import canonical_json, exact_json
from deltasleuth.core.types import Counted, KeyedMap


@pytest.mark.parametrize("text", ["1e4001", "-1e4001", "1e-4001", "-1e-4001"])
def test_render_rejects_nonzero_magnitude_from_programmatic_values(text: str) -> None:
    with pytest.raises(jsonio.ResourceLimitError):
        jsonio.number_text(Decimal(text))


@pytest.mark.parametrize("kind", ["decimal", "integer"])
def test_type_map_does_not_bypass_the_number_limit(kind: str) -> None:
    with pytest.raises(jsonio.ResourceLimitError):
        convert("1e4001", {"as": kind})


@pytest.mark.parametrize("render", [jsonio.dumps, jsonio.to_serialisable, canonical_json, exact_json])
@pytest.mark.parametrize("wrapped", [False, True])
def test_render_charges_all_occurrences_before_expanding(render, wrapped: bool, monkeypatch) -> None:
    # Each leaf fits; their sum does not. Reusing an object must not deduplicate its charge.
    monkeypatch.setattr(jsonio, "MAX_NUMBER_BYTES", 5000, raising=False)
    repeated = [Decimal("1e3999")]
    value = [repeated, repeated]
    if wrapped:
        value = KeyedMap({"k": [Counted(value, 2)]}, mode="set")
    with pytest.raises(jsonio.ResourceLimitError):
        render(value)


def test_parse_checks_aggregate_expansion_before_downstream_item_hashes(monkeypatch) -> None:
    monkeypatch.setattr(jsonio, "MAX_NUMBER_BYTES", 5000, raising=False)
    with pytest.raises(jsonio.ResourceLimitError):
        jsonio.parse_json(b"[1e3999,1e3999]")


@pytest.mark.parametrize(
    ("text", "want"),
    [("-0e999999999", "0"), ("0e-999999999", "0"), ("1200.000", "1200"),
     ("0.0012300", "0.00123"), ("1234567890123456789012.1234567895", "1234567890123456789012.1234567895")],
)
def test_exact_number_controls(text: str, want: str) -> None:
    assert jsonio.number_text(Decimal(text)) == want
    assert jsonio.dumps([Decimal(text)]) == f"[{want}]"


def test_merged_pages_share_an_expansion_budget_before_item_hashing(monkeypatch) -> None:
    from yarl import URL

    from deltasleuth.connectors.paging import PageWalk
    from deltasleuth.core.types import Pagination

    monkeypatch.setattr(jsonio, "MAX_NUMBER_BYTES", 5000)
    walk = PageWalk(
        Pagination(kind="cursor", items_path="$.items", cursor_path="$.next", cursor_param="cursor"),
        stable_ordering=True,
    )
    walk.start(b'{"items":[1e3999],"next":"second"}', {}, URL("https://example.test/items"))
    walk.add(b'{"items":[2e3999],"next":null}', {}, URL("https://example.test/items"))
    assert walk.resource_limited
    assert walk.page == []
    with pytest.raises(jsonio.ResourceLimitError):
        walk.body()


def test_mapped_numbers_are_resource_limited_before_array_rewriting(make_spec, make_observation, monkeypatch):
    from deltasleuth.core.canonical.observe import observe
    from deltasleuth.core.types import (
        ArrayRule,
        CompareConfig,
        Side,
        SideConfig,
        SideStateKind,
        TargetCapabilities,
    )

    monkeypatch.setattr(jsonio, "MAX_NUMBER_BYTES", 180)
    spec = make_spec(
        CompareConfig(arrays=(ArrayRule("$", "set"),)),
        side_a=SideConfig(side=Side.A, role="a", target="a", map='[$number("1e-99"), $number("2e-99")]'),
    )
    result = observe(make_observation(b"{}"), spec.sides[Side.A], spec, TargetCapabilities())
    assert (result.kind, result.reason) == (SideStateKind.INCOMPLETE, "resource_limit")
