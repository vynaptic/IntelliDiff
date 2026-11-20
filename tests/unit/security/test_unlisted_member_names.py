"""Member names of objects no rule lists: a name that reads as data is a token in views and pointers and
``[*]`` in patterns; a name that reads as a field's stays, and so does a name a clear entry covers."""

from __future__ import annotations

from datetime import date

import pytest

from deltasleuth.core.canonical.canonicalise import shape
from deltasleuth.core.canonical.paths import child_pattern
from deltasleuth.core.classify.enrich import enrich
from deltasleuth.core.classify.rootcause import root_causes
from deltasleuth.core.jsonio import dumps
from deltasleuth.core.masking import run_masking
from deltasleuth.core.types import ChangeType, KeyResult, Outcome, Side, SideConfig, SideState
from deltasleuth.security.tokenise import (
    KEY_FORM,
    mask_document,
    mask_finding,
    mask_path_pattern,
    mask_pointer,
)

CHANGED = ChangeType.VALUE_CHANGED
ADA, BOB = "ada@example.org", "bob@example.org"
TODAY = date(2026, 9, 24)
DATA = (
    ADA,
    "+44 20 7946 0000",
    "ORG-000123",
    "MBR-0045123",
    "0d1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b",
    "Ada Lovelace",
    "12345",
    "",
    "n" * 65,
)
FIELDS = ("givenName", "address_line_1", "top10", "p99-latency", "_links", "Smith")


def test_declared_names_preserve_structure_and_mask_dynamic_words(make_spec, make_finding, tok) -> None:
    base = make_spec(tokenize=(("$.contacts[*].age", "free_number"),), clear=("$.available",))
    spec = run_masking(base, {"member_names": "declared"})
    [finding] = enrich([_age(make_finding, email="Smith")], spec, today=TODAY)
    row = mask_finding(finding, spec, tok, form=KEY_FORM)
    member = tok.token("free_text", "Smith")
    assert (row["path_pattern"], row["json_pointer"]) == ("$.contacts[*].age", f"/contacts/{member}/age")
    doc = {"contacts": {"Smith": {"age": 8, "flag": False}}, "available": True}
    unknown = tok.token("free_text", "flag")
    assert mask_document(doc, spec, tok) == {
        "contacts": {member: {"age": tok.token("free_number", 8), unknown: "class:boolean"}},
        "available": True,
    }
    assert "Smith" not in dumps(row)
    assert run_masking(base, {}) is base
    assert mask_path_pattern("$.contacts.Smith.age", base) == "$.contacts.Smith.age"


def test_declared_table_columns_are_scoped_to_their_position_and_run(make_spec, tok) -> None:
    base = make_spec()

    def policy(column: str):
        tables = {"a": {"columns": {column: "SRC"}}, "only_b": []}
        return run_masking(base, {"member_names": "declared", "tables": tables})

    amount, tier = policy("amount"), policy("tier")
    assert len({amount.judge_key, tier.judge_key, base.judge_key}) == 3
    assert mask_path_pattern("$.amount.amount", amount) == "$.amount[*]"
    assert mask_path_pattern("$.amount", tier) == "$[*]"
    doc = {"é": 7, "e\u0301": 8}
    shown = mask_document(doc, amount, tok)
    assert len(shown) == len(doc)
    for name, value in doc.items():
        pointer = mask_pointer((("field", name),), amount, tok)
        assert shown[pointer[1:]] == tok.token("free_number", value)


def test_unreviewed_spec_cannot_publish_names_outside_its_catalogue(make_spec) -> None:
    base = make_spec(tokenize=(("$.contacts[*].age", "free_number"),))
    tables = {"a": {"columns": {"contacts": "SRC"}}}
    spec = run_masking(base, {"member_names": "catalogue", "tables": tables})
    assert mask_path_pattern("$.contacts.Smith.age", spec) == "$.contacts[*][*]"


def _age(make_finding, key: str = "K1", email: str = ADA):
    parts = (("field", "contacts"), ("field", email), ("field", "age"))
    return make_finding(f"$.contacts['{email}'].age", CHANGED, "x", "y", parts, key=key)


def test_a_data_name_of_an_unlisted_object_is_a_token_in_a_document_view(make_spec, tok) -> None:
    doc = {"contacts": {ADA: {"age": "x"}}, "tier": "GOLD"}
    masked = mask_document(doc, make_spec(), tok, key="K1")
    assert masked == {
        "contacts": {tok.token("free_text", ADA): {"age": tok.token("free_text", "x")}},
        "tier": tok.token("free_text", "GOLD"),
    }
    assert "ada" not in dumps(masked)


def test_a_value_mapped_into_a_member_name_is_masked(make_spec, tok) -> None:
    side = SideConfig(Side.A, "legacy", "svc-a", map='{"as_name": { name: true }}')
    doc = shape({"name": "Ada Lovelace"}, side)
    assert doc == {"as_name": {"Ada Lovelace": True}}
    assert mask_document(doc, make_spec(), tok) == {"as_name": {tok.token("free_text", "Ada Lovelace"): True}}


def test_the_pointer_the_pattern_and_the_view_agree(make_spec, make_finding, tok) -> None:
    spec = make_spec()
    [f] = enrich([_age(make_finding)], spec, today=TODAY)
    row = mask_finding(f, spec, tok, form=KEY_FORM)
    shown = tok.token("free_text", ADA)
    assert (row["path_pattern"], row["json_pointer"]) == ("$.contacts[*].age", f"/contacts/{shown}/age")
    assert list(mask_document({"contacts": {ADA: {"age": "x"}}}, spec, tok, key="K1")["contacts"]) == [shown]
    assert "ada" not in dumps(row)
    assert mask_pointer(f.pointer_parts, spec, None) == "/contacts/tok:free_text:unkeyed/age"


@pytest.mark.parametrize("data", DATA)
def test_a_name_that_reads_as_data_is_masked(make_spec, tok, data: str) -> None:
    spec, shown = make_spec(), tok.token("free_text", data)
    assert mask_path_pattern(child_pattern("$.profile", data) + ".since", spec) == "$.profile[*].since"
    assert mask_pointer((("field", "profile"), ("field", data)), spec, tok) == f"/profile/{shown}"
    assert mask_document({"profile": {data: None}}, spec, tok) == {"profile": {shown: None}}


@pytest.mark.parametrize("field", FIELDS)
def test_a_name_that_reads_as_a_field_s_stays(make_spec, tok, field: str) -> None:
    spec, pattern = make_spec(), child_pattern("$.profile", field)
    assert mask_path_pattern(pattern, spec) == pattern
    assert mask_pointer((("field", "profile"), ("field", field)), spec, tok) == f"/profile/{field}"
    assert mask_document({"profile": {field: None}}, spec, tok) == {"profile": {field: None}}


@pytest.mark.parametrize(
    ("clear", "unlisted"),
    [
        (("$.contacts",), "tokenize"),  # the object
        (("$.contacts[*]",), "tokenize"),  # each of its members
        ((f"$.contacts['{ADA}']",), "tokenize"),  # the member itself
        ((), "clear"),
    ],
)
def test_a_cleared_name_stays_as_it_is(make_spec, tok, clear: tuple[str, ...], unlisted: str) -> None:
    spec = make_spec(clear=clear, unlisted=unlisted)
    pattern = f"$.contacts['{ADA}']"
    assert mask_path_pattern(pattern, spec) == pattern
    assert mask_pointer((("field", "contacts"), ("field", ADA)), spec, tok) == f"/contacts/{ADA}"
    assert mask_document({"contacts": {ADA: True}}, spec, tok) == {"contacts": {ADA: True}}


def test_a_clear_entry_below_a_data_name_leaves_the_name_masked(make_spec, tok) -> None:
    spec = make_spec(clear=(f"$.contacts['{ADA}'].age",))
    assert mask_path_pattern(f"$.contacts['{ADA}'].age", spec) == "$.contacts[*].age"
    assert mask_document({"contacts": {ADA: {"age": "x"}}}, spec, tok) == {
        "contacts": {tok.token("free_text", ADA): {"age": "x"}}
    }


def test_both_sides_line_up_and_the_raw_order_is_hidden(make_spec, tok) -> None:
    spec = make_spec()
    a = mask_document({"members": {ADA: True, "zeta": True, BOB: True}}, spec, tok)
    b = mask_document({"members": {BOB: True, "zeta": True, ADA: True}}, spec, tok)
    assert list(a["members"]) == list(b["members"]) == sorted(a["members"])
    fields = {"zeta": True, "alpha": True}  # no name is data: the document's own order
    assert list(mask_document({"members": fields}, spec, tok)["members"]) == ["zeta", "alpha"]


def test_one_field_under_data_names_is_one_signature_and_one_root_cause(make_spec, make_finding, tok) -> None:
    spec = make_spec()
    results = [
        KeyResult(
            key,
            Outcome.MISMATCH,
            SideState.absent(),
            SideState.absent(),
            findings=enrich([_age(make_finding, key, email)], spec, today=TODAY),
        )
        for key, email in (("K1", ADA), ("K2", BOB))
    ]
    assert len({f.signature_hash for r in results for f in r.findings}) == 1
    [rc] = root_causes(results, spec=spec, tokeniser=tok, form=KEY_FORM)
    assert (rc.kind, rc.path_pattern, rc.affected_keys) == ("signature", "$.contacts[*].age", 2)
    assert "ada" not in repr(rc) and "bob" not in repr(rc)
