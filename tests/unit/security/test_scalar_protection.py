"""A scalar where the spec protects descendants is the protected data."""

from __future__ import annotations

import pytest

from deltasleuth.core.jsonio import dumps
from deltasleuth.core.types import ChangeType
from deltasleuth.security.tokenise import mask_document, mask_finding

TYPED = ChangeType.TYPE_CHANGED
ADDRESS = "Flat 10, 3 Mill Lane, York YO1 7JU"
PHONE = "+447700900123"
ADDRESS_RULES = (("$.address.line1", "address"), ("$.address.postcode", "address"))
PHONE_RULES = (("$.contact.phones[*].number", "phone"),)

# (tokenise rules, security.clear, unlisted) that leave the parent of the protected fields clear
CLEAR_PARENT = [
    pytest.param(ADDRESS_RULES, ("$.address",), "tokenize", id="clear-ancestor"),
    pytest.param(ADDRESS_RULES, (), "clear", id="unlisted-clear"),
]


@pytest.mark.parametrize(("tokenize", "clear", "unlisted"), CLEAR_PARENT)
def test_scalar_replacing_protected_subtree_is_tokenised(
    make_spec, make_finding, tok, tokenize, clear, unlisted
) -> None:
    """Object -> string: side b flattens the address into one string."""
    spec = make_spec(tokenize=tokenize, clear=clear, unlisted=unlisted)
    a = {"line1": "Flat 10, 3 Mill Lane", "postcode": "YO1 7JU", "country": "GB"}
    f = make_finding("$.address", TYPED, a, ADDRESS, (("field", "address"),))
    row = mask_finding(f, spec, tok)
    assert row["value_b_tok"] == tok.token("address", ADDRESS)
    assert "Mill Lane" not in dumps(row) and "YO1" not in dumps(row)
    assert '"country": "GB"' in row["value_a_tok"]  # what the spec clears stays clear
    assert mask_document({"address": ADDRESS}, spec, tok) == {"address": tok.token("address", ADDRESS)}


@pytest.mark.parametrize(
    ("clear", "unlisted"),
    [pytest.param(("$.contact",), "tokenize", id="clear-ancestor"), pytest.param((), "clear", id="unlisted")],
)
def test_scalar_replacing_a_protected_array_is_tokenised(
    make_spec, make_finding, tok, clear, unlisted
) -> None:
    """Array -> string: side b returns the phone list as the one number."""
    spec = make_spec(tokenize=PHONE_RULES, clear=clear, unlisted=unlisted)
    f = make_finding(
        "$.contact.phones",
        TYPED,
        [{"number": PHONE, "type": "MOBILE"}],
        PHONE,
        (("field", "contact"), ("field", "phones")),
    )
    row = mask_finding(f, spec, tok)
    assert row["value_b_tok"] == tok.token("phone", PHONE)
    assert PHONE not in dumps(row)  # neither side shows it, so no token-to-value mapping leaks


def test_class_only_descendant_shows_its_class(make_spec, make_finding, tok) -> None:
    spec = make_spec(tokenize=(("$.person.dob", "date_of_birth"),), clear=("$.person",))
    row = mask_finding(
        make_finding("$.person", TYPED, {"dob": "1990-05-17"}, "1990-05-17", (("field", "person"),)),
        spec,
        tok,
    )
    assert (row["value_a_tok"], row["value_b_tok"]) == (
        '{"dob": "class:date_of_birth"}',
        "class:date_of_birth",
    )


def test_clear_scalars_without_protected_descendants_stay_clear(make_spec, make_finding, tok) -> None:
    spec = make_spec(tokenize=ADDRESS_RULES, clear=("$.address", "$.status"))
    row = mask_finding(make_finding("$.status", TYPED, "OPEN", "1", (("field", "status"),)), spec, tok)
    assert (row["value_a_tok"], row["value_b_tok"]) == ("OPEN", "1")
    row = mask_finding(
        make_finding("$.address.country", TYPED, "GB", "1", (("field", "address"), ("field", "country"))),
        spec,
        tok,
    )
    assert (row["value_a_tok"], row["value_b_tok"]) == ("GB", "1")
