"""Member names that are data: maps under an explicit tokenise rule never leak."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from deltasleuth.core.classify.enrich import enrich
from deltasleuth.core.classify.patterns import PatternError, segments
from deltasleuth.core.classify.rootcause import groups, root_causes
from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.jsonio import dumps
from deltasleuth.core.types import (
    ArrayRule,
    ChangeType,
    KeyResult,
    KnownDiffRule,
    Outcome,
    RuleClassification,
    SideState,
)
from deltasleuth.security.policy import canonical_policy
from deltasleuth.security.tokenise import (
    mask_document,
    mask_finding,
    mask_path_pattern,
    mask_pointer,
)

CHANGED = ChangeType.VALUE_CHANGED
JANE, BOB = "jane.doe.secret@example.org", "bob.hidden@example.org"
TODAY = date(2026, 9, 24)
MAP_RULES = (("$.contactVerified", "email"), ("$.name", "name"))


def _jane_flag(make_finding, key: str = "K1", email: str = JANE):
    return make_finding(
        f"$.contactVerified['{email}']",
        CHANGED,
        True,
        False,
        (("field", "contactVerified"), ("field", email)),
        key=key,
    )


def test_map_keyed_by_email_never_leaks_from_a_finding_row(make_spec, make_finding, tok) -> None:
    spec = make_spec(tokenize=MAP_RULES)
    [f] = enrich([_jane_flag(make_finding)], spec, today=TODAY)
    row = mask_finding(f, spec, tok)
    assert row["path_pattern"] == "$.contactVerified[*]"
    assert row["json_pointer"] == f"/contactVerified/{tok.token('email', JANE)}"
    assert (row["value_a_tok"], row["value_b_tok"]) == (True, False)  # booleans stay clear
    assert JANE not in dumps(row) and "jane" not in dumps(row)


def test_map_keyed_by_email_never_leaks_from_a_document_view(make_spec, tok) -> None:
    spec = make_spec(tokenize=MAP_RULES)
    doc = {"contactVerified": {JANE: True, BOB: True}, "name": "Jane Doe", "tier": "GOLD"}
    masked = mask_document(doc, spec, tok)
    assert masked["contactVerified"] == dict(
        sorted({tok.token("email", JANE): True, tok.token("email", BOB): True}.items())
    )
    text = dumps(masked)
    assert JANE not in text and BOB not in text and "Jane Doe" not in text
    assert set(masked) == {"contactVerified", "name", "tier"}  # field names elsewhere are kept


def test_member_names_below_the_covered_object_are_data_too(make_spec, make_finding, tok) -> None:
    spec = make_spec(tokenize=MAP_RULES)
    parts = (("field", "contactVerified"), ("field", JANE), ("field", "since"))
    assert mask_pointer(parts, spec, tok) == (
        f"/contactVerified/{tok.token('email', JANE)}/{tok.token('email', 'since')}"
    )
    assert mask_path_pattern(f"$.contactVerified['{JANE}'].since", spec) == "$.contactVerified[*][*]"


@pytest.mark.parametrize(
    ("tokenize", "clear", "unlisted", "names_are_data"),
    [
        (MAP_RULES, (), "tokenize", True),
        ((), (), "tokenize", False),  # the unlisted default never makes field names data
        ((), (), "clear", False),
        ((("$.contactVerified", "free_text"),), ("$.contactVerified",), "tokenize", True),  # a tie tokenises
        ((("$.profile", "free_text"),), ("$.profile.contactVerified",), "tokenize", False),  # deeper clear
        ((("$.profile", "email"),), ("$.profile.contactVerified",), "tokenize", True),  # the floor holds
    ],
)
def test_which_objects_hold_data_in_their_names(make_spec, tokenize, clear, unlisted, names_are_data) -> None:
    spec = make_spec(tokenize=tokenize, clear=clear, unlisted=unlisted)
    where = "$.profile.contactVerified" if tokenize and tokenize[0][0] == "$.profile" else "$.contactVerified"
    name_class = canonical_policy(spec.security).name_class(segments(where))
    assert (name_class is not None) is names_are_data
    collapsed = mask_path_pattern(f"{where}['{JANE}']", spec)
    assert collapsed.endswith("[*]") is names_are_data
    assert (JANE in collapsed) is not names_are_data


def test_ordinary_field_names_stay_readable(make_spec) -> None:
    spec = make_spec(
        tokenize=(("$.phones[*].number", "phone"), ("$.name", "name")),
        arrays=(ArrayRule("$.phones", "keyed", ("number",)),),
    )
    for pattern in ("$.phones[*].kind", "$.phones[*]", "$.name", "$['odd name'].x", "$"):
        assert mask_path_pattern(pattern, spec) == pattern
    with pytest.raises(PatternError, match="descendant"):
        mask_path_pattern("$..email", spec)


def test_signatures_hash_the_published_pattern(make_spec, make_finding) -> None:
    """An unsalted hash of a raw email in the pattern could be reversed by guessing."""
    spec = make_spec(tokenize=MAP_RULES)
    [jane] = enrich([_jane_flag(make_finding)], spec, today=TODAY)
    [bob] = enrich([_jane_flag(make_finding, "K2", BOB)], spec, today=TODAY)
    assert jane.signature_hash == bob.signature_hash == signature_hash(jane, spec)
    assert jane.signature_hash != signature_hash(jane)  # not the hash of the raw pattern


def test_root_causes_group_map_members_under_the_published_pattern(make_spec, make_finding, tok) -> None:
    spec = make_spec(tokenize=MAP_RULES)
    results = [
        KeyResult(
            key,
            Outcome.MISMATCH,
            SideState.absent(),
            SideState.absent(),
            findings=enrich([_jane_flag(make_finding, key, email)], spec, today=TODAY),
        )
        for key, email in (("K1", JANE), ("K2", BOB))
    ]
    [rc] = root_causes(results, spec=spec, tokeniser=tok)
    assert (rc.kind, rc.path_pattern, rc.affected_keys) == ("signature", "$.contactVerified[*]", 2)
    [(grouped, fs)] = groups(results, spec=spec, tokeniser=tok)
    assert (grouped, {f.key for f in fs}) == (rc, {"K1", "K2"})
    assert JANE not in repr(rc) and BOB not in repr(rc)


def test_known_diff_errors_carry_the_published_pattern(make_spec, make_finding, tok) -> None:
    rule = KnownDiffRule("KD-E-0001", "a.x == 1", RuleClassification.ACCEPTED, "reason text", "T-1")
    spec = make_spec(tokenize=MAP_RULES)
    errors: list = []
    enrich([_jane_flag(make_finding)], replace(spec, known_diffs=(rule,)), today=TODAY, rule_errors=errors)
    assert [e["path_pattern"] for e in errors] == ["$.contactVerified[*]"]
