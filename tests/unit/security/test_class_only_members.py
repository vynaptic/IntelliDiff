"""Class-only member keys and names (``date_of_birth``) are never linkable tokens."""

from __future__ import annotations

import re

import pytest

from deltasleuth.core.classify.enrich import classify
from deltasleuth.core.classify.rootcause import root_causes
from deltasleuth.core.jsonio import dumps
from deltasleuth.core.types import ArrayRule, ChangeType, KeyedMap, KeyResult, Outcome, SideState
from deltasleuth.security.tokenise import (
    Tokeniser,
    TokeniserError,
    mask_document,
    mask_finding,
    mask_member_key,
    mask_pointer,
)

MISSING = ChangeType.MISSING_IN_B
DOB = "2015-03-02"
LABEL = re.compile(r"class:date_of_birth#[A-Z2-7]{10}")


@pytest.fixture
def spec(make_spec):
    return make_spec(
        tokenize=(("$.dependants[*].dob", "date_of_birth"),),
        clear=("$.dependants[*].rel",),
        arrays=(ArrayRule("$.dependants", "keyed", ("dob",)),),
    )


def _lost_dependant(make_finding, key: str):
    return make_finding(
        "$.dependants[*]",
        MISSING,
        {"dob": DOB, "rel": "CHILD"},
        None,
        (("field", "dependants"), ("member", DOB)),
        key=key,
        member_key=DOB,
        array_path="$.dependants",
    )


def test_dob_member_key_never_tokenised_in_findings(spec, make_finding, tok) -> None:
    rows = [
        mask_finding(classify(_lost_dependant(make_finding, k), spec, None), spec, tok) for k in ("K1", "K2")
    ]
    for row in rows:
        assert LABEL.fullmatch(row["member_key_tok"])
        assert row["json_pointer"] == f"/dependants/{row['member_key_tok']}"
        assert row["value_a_tok"] == dumps({"dob": "class:date_of_birth", "rel": "CHILD"}, sort_keys=True)
        assert "tok:date_of_birth" not in dumps(row) and DOB not in dumps(row)
    # the same birth date under two customers gives unrelated labels: no record linking
    assert rows[0]["member_key_tok"] != rows[1]["member_key_tok"]


def test_labels_agree_within_one_key(spec, tok) -> None:
    """Findings, pointers and the side-by-side view of one key line up by string equality."""
    parts = (("field", "dependants"), ("member", DOB))
    label = mask_member_key(DOB, "$.dependants", spec, tok, key="K1")
    assert mask_pointer(parts, spec, tok, key="K1") == f"/dependants/{label}"
    doc = {"dependants": KeyedMap({DOB: [{"dob": DOB, "rel": "CHILD"}]})}
    assert mask_document(doc, spec, tok, key="K1") == {
        "dependants": {label: [{"dob": "class:date_of_birth", "rel": "CHILD"}]}
    }
    # without the key there is no safe scope: the class alone, never a token
    assert mask_member_key(DOB, "$.dependants", spec, tok) == "class:date_of_birth"
    assert mask_pointer(parts, spec, None) == "/dependants/tok:date_of_birth:unkeyed"


def test_dob_members_never_fan_out_across_keys(spec, make_finding, tok) -> None:
    results = [
        KeyResult(
            k,
            Outcome.MISMATCH,
            SideState.absent(),
            SideState.absent(),
            findings=[classify(_lost_dependant(make_finding, k), spec, None)],
        )
        for k in ("K1", "K2")
    ]
    [rc] = root_causes(results, spec=spec, tokeniser=tok)
    assert (rc.kind, rc.member_key_tok, rc.affected_keys) == ("signature", None, 2)


def test_map_keyed_by_birth_date_uses_scoped_labels(make_spec, tok) -> None:
    spec = make_spec(tokenize=(("$.birthdays", "date_of_birth"),))
    masked = mask_document({"birthdays": {DOB: "Ada"}}, spec, tok, key="K1")
    [(name, value)] = masked["birthdays"].items()
    assert LABEL.fullmatch(name) and value == "class:date_of_birth"


def test_scoped_labels_are_keyed_and_domain_separated() -> None:
    a, b = Tokeniser(bytes(range(32))), Tokeniser(bytes(range(1, 33)))
    label = a.scoped_label("date_of_birth", "K1", DOB)
    assert LABEL.fullmatch(label)
    assert label == a.scoped_label("date_of_birth", "K1", DOB)
    assert label != b.scoped_label("date_of_birth", "K1", DOB)  # needs the secret key
    assert label != a.scoped_label("date_of_birth", "K2", DOB)
    # the scope is length-prefixed: it cannot absorb part of the value
    assert a.scoped_label("date_of_birth", "K\x1f1", "x") != a.scoped_label("date_of_birth", "K", "1\x1fx")
    assert label.split("#")[1] not in a.token("date_of_birth", DOB)
    with pytest.raises(TokeniserError):
        a.scoped_label("Bad Class", "K1", DOB)
