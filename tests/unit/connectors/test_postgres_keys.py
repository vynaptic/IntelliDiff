"""A table key's text: a composite key's parts joined by ``|``, escaped, and
back, and a side's key maps from the canonical key to the values the side stores."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.tables import decode_key, encode_key, side_values
from deltasleuth.connectors.tables.keys import to_side

PART = st.text(st.sampled_from("|\\") | st.characters(codec="utf-8"), max_size=6)  # empty parts too


def test_a_composite_key_is_its_parts_joined_by_a_pipe() -> None:
    assert encode_key(["us|x", "1\\", ""]) == "us\\|x|1\\\\|"


@given(st.lists(PART, min_size=1, max_size=4))
def test_a_key_decodes_to_the_parts_it_encodes(parts: list[str]) -> None:
    assert decode_key(encode_key(parts), len(parts)) == parts


@pytest.mark.parametrize("key", ["eu|1|x", "eu", "eu\\|1", "eu|1\\"])
def test_a_key_with_another_number_of_parts_is_rejected(key: str) -> None:
    with pytest.raises(KeyRejectedError):
        decode_key(key, 2)


PREFIX_PAD = {"kind": "prefix_pad", "prefix": "ORG-", "width": 6, "sideType": "integer"}


def regex(pattern: str, replacement: str, side_type: str = "string") -> dict[str, Any]:
    rewrite = {"pattern": pattern, "replacement": replacement}
    return {"kind": "regex", "toSide": rewrite, "toCanonical": rewrite, "sideType": side_type}


@pytest.mark.parametrize(
    ("part", "key_map", "value"),
    [
        ("ORG-1", None, ["ORG-1"]),
        ("ORG-1", {"kind": "identity"}, ["ORG-1"]),
        ("ORG-000123", PREFIX_PAD, ["123"]),
        # A text key padded to the width may be stored with any fewer zeros: each maps to this key.
        ("ORG-000123", PREFIX_PAD | {"sideType": "string"}, ["000123", "00123", "0123", "123"]),
        ("ORG-123", PREFIX_PAD, None),  # shorter than the width: the scan never makes it
        ("ORG-0001234", PREFIX_PAD, None),  # wider, with a zero no integer holds
        ("ORG-1234567", PREFIX_PAD, ["1234567"]),  # wider than width
        ("ORG-000000", PREFIX_PAD, ["0"]),
        ("XX-000123", PREFIX_PAD, None),  # another prefix
        ("ORG-12a", PREFIX_PAD, None),
        ("ORG-", PREFIX_PAD, None),
        ("ORG-\u0661\u0662", PREFIX_PAD, None),  # digits, but not ASCII ones
        ("C-12-ab", regex("C-([0-9]+)-([a-z]+)", "$2:$1"), ["ab:12"]),
        ("C-12-ab-x", regex("C-([0-9]+)-([a-z]+)", "$2:$1"), None),  # the whole part must match
        ("x-C-12-ab", regex("C-([0-9]+)-([a-z]+)", "$2:$1"), None),
        ("ID42", regex("ID([0-9a-z]+)", "$1", "integer"), ["42"]),
        ("ID042", regex("ID([0-9]+)", "$1", "integer"), None),  # an integer renders no leading zero
        ("IDab", regex("ID([0-9a-z]+)", "$1", "integer"), None),  # the side stores integers
    ],
)
def test_a_canonical_key_part_maps_to_the_values_its_side_may_store(
    part: str, key_map: dict[str, Any] | None, value: list[str] | None
) -> None:
    assert to_side(part, key_map) == value


def test_a_key_one_part_of_which_cannot_map_back_holds_no_row_of_the_side() -> None:
    side = {"keys": ["entity", "code"], "key_maps": [None, PREFIX_PAD]}
    assert side_values("e|ORG-000042", side) == [["e"], ["42"]]
    assert side_values("e|XX-42", side) is None
    assert side_values("e|ORG-1", {"keys": ["entity", "code"]}) == [["e"], ["ORG-1"]]  # no maps: identity
    with pytest.raises(KeyRejectedError):
        side_values("e|ORG-1|x", side)
