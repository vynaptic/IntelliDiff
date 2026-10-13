"""Canonical encoding of member names that share an NFC form."""

from __future__ import annotations

import json
from decimal import Decimal

from deltasleuth.core.canonical.encoding import canonical_json, exact_json, member_key_text
from deltasleuth.core.types import KeyedMap

NFC, NFD = "café", "café"  # the same name to a reader; different code units


def test_nfc_equivalent_names_on_two_sides_encode_alike() -> None:
    assert canonical_json({NFC: 1}) == canonical_json({NFD: 1}) == '{"café":1}'
    assert exact_json({NFC: 1}) != exact_json({NFD: 1})


def test_names_of_one_object_sharing_an_nfc_form_are_written_exactly() -> None:
    encoded = canonical_json({NFC: "on", NFD: "off"})
    assert json.loads(encoded, object_pairs_hook=list) == [(NFD, "off"), (NFC, "on")]
    # A value swap between them is a difference; insertion order is not; nor is one name alone.
    assert encoded != canonical_json({NFD: "on", NFC: "off"})
    assert encoded == canonical_json({NFD: "off", NFC: "on"})
    assert canonical_json({NFC: 1, NFD: 1}) != canonical_json({NFC: 1})


def test_keyed_map_member_keys_follow_the_same_rule() -> None:
    one, swapped = (
        KeyedMap({NFC: ["on"], NFD: ["off"]}, mode="keyed"),
        KeyedMap({NFC: ["off"], NFD: ["on"]}, mode="keyed"),
    )
    assert canonical_json(one) != canonical_json(swapped)
    assert canonical_json(KeyedMap({NFD: [Decimal(1)]}, mode="keyed")) == canonical_json(
        KeyedMap({NFC: [Decimal(1)]}, mode="keyed")
    )
    assert canonical_json(KeyedMap({}, mode="set")) not in (canonical_json({}), canonical_json([]))


def test_member_keys_are_untyped_and_escaped() -> None:
    assert member_key_text([Decimal(12)]) == member_key_text(["12"]) == "12"
    assert member_key_text(["p|q", Decimal("2.0"), None]) == "p\\|q|2|null"
