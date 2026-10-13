"""Tokenisation policy and masked renditions of findings, pointers, keys and documents."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from deltasleuth.core.classify.enrich import classify, enrich
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.classify.patterns import segments
from deltasleuth.core.jsonio import dumps
from deltasleuth.core.types import (
    ArrayRule,
    ChangeType,
    CompareConfig,
    Counted,
    KeyConfig,
    KeyedMap,
    SecurityPolicy,
    SideState,
    TokenizeRule,
)
from deltasleuth.security.policy import class_for, member_class
from deltasleuth.security.tokenise import mask_document, mask_finding, mask_key, mask_member_key, mask_pointer

CHANGED, MISSING = ChangeType.VALUE_CHANGED, ChangeType.MISSING_IN_B
PHONE = "+447700900123"


# --- policy --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$.givenName", "name"),
        ("$.birthDate", "date_of_birth"),
        ("$.phones[*].number", "phone"),
        ("$['phones'][0]['number']", "phone"),
        ("$.addresses[*].lines[*]", "address"),
        ("$.segment", None),
        ("$.tags[*]", None),
        ("$.preferences.marketing.sms", None),
        ("$.preferences.marketing.push", "free_text"),  # unlisted -> tokenised
        ("$.customerId", "free_text"),
    ],
)
def test_class_for_the_customer_policy(spec, path: str, expected: str | None) -> None:
    assert class_for(path, spec.security) == expected


def test_unlisted_clear() -> None:
    policy = SecurityPolicy(tokenize=(TokenizeRule("$.email", "email"),), unlisted="clear")
    assert (class_for("$.other", policy), class_for("$.email", policy)) == (None, "email")


def test_most_specific_rule_wins_and_a_tie_tokenises() -> None:
    policy = SecurityPolicy(
        tokenize=(TokenizeRule("$.notes", "free_text"), TokenizeRule("$.meta.code", "free_text")),
        clear=("$.notes.public", "$.meta", "$.meta.code"),
    )
    assert class_for("$.notes.private", policy) == "free_text"  # a rule covers its subtree
    assert class_for("$.notes.public", policy) is None  # a deeper clear entry wins
    assert class_for("$.meta.other", policy) is None
    assert class_for("$.meta.code", policy) == "free_text"  # same depth: tokenise


def test_the_pii_floor_cannot_be_cleared() -> None:
    policy = SecurityPolicy(
        tokenize=(TokenizeRule("$.contact", "email"), TokenizeRule("$.contact.note", "free_text")),
        clear=("$.contact.email", "$.contact.note.text"),
    )
    assert class_for("$.contact.email", policy) == "email"
    assert class_for("$.contact.note.text", policy) == "email"  # the floor class of an ancestor


def test_descendant_and_wildcard_rules() -> None:
    policy = SecurityPolicy(
        tokenize=(TokenizeRule("$..email", "email"), TokenizeRule("$.*.b", "name")), unlisted="clear"
    )
    assert class_for("$.a[*].email", policy) == class_for("$.email", policy) == "email"
    assert class_for("$.emails", policy) is None
    assert (class_for("$.x.b", policy), class_for("$.x[*]", policy)) == ("name", None)


def test_member_classes(spec, make_spec) -> None:
    assert member_class(segments("$.phones"), spec) == "phone"
    assert member_class(segments("$.accounts"), spec) == "account_number"  # sortCode clear, accountNo not
    assert member_class(segments("$.addresses"), spec) is None  # keyed by the clear type
    assert member_class(segments("$.tags"), spec) is None
    mixed = make_spec(
        compare=CompareConfig(
            arrays=(ArrayRule("$.people", "keyed", ("name", "phone")), ArrayRule("$.contacts", "set"))
        ),
        security=SecurityPolicy(
            tokenize=(
                TokenizeRule("$.people[*].name", "name"),
                TokenizeRule("$.people[*].phone", "phone"),
                TokenizeRule("$.contacts[*].phone", "phone"),
            ),
            clear=("$.contacts[*]",),
        ),
    )
    assert member_class(segments("$.people"), mixed) == "identifier"
    assert member_class(segments("$.contacts"), mixed) == "phone"  # a protected leaf inside the member
    unlisted = make_spec(compare=CompareConfig(arrays=(ArrayRule("$.codes", "keyed", ("code",)),)))
    assert (
        member_class(segments("$.codes"), unlisted) == member_class(segments("$.x"), unlisted) == "free_text"
    )


# --- pointers, keys, member keys -----------------------------------------------------------------


def test_mask_pointer(spec, tok) -> None:
    parts = (("field", "phones"), ("member", PHONE), ("dup", "1"), ("field", "kind"))
    assert mask_pointer(parts, spec, tok) == f"/phones/{tok.token('phone', PHONE)}/@1/kind"
    acct = (("field", "accounts"), ("member", "000000|12345678"), ("field", "balance"))
    assert (
        mask_pointer(acct, spec, tok) == f"/accounts/{tok.token('account_number', '000000|12345678')}/balance"
    )
    addr = (("field", "addresses"), ("member", "HOME"), ("field", "lines"), ("index", "0"))
    assert mask_pointer(addr, spec, tok) == "/addresses/HOME/lines/0"
    assert mask_pointer((), spec, tok) == ""


def test_pointer_escaping_and_the_reserved_at(spec, tok) -> None:
    assert mask_pointer((("field", "a/b~c"), ("field", "@type")), spec, tok) == "/a~1b~0c/@@type"
    assert mask_pointer((("field", "consents"), ("member", "@home/x")), spec, tok) == "/consents/@@home~1x"


def test_a_pointer_without_tokeniser_uses_a_non_token_marker(spec) -> None:
    assert mask_pointer((("field", "phones"), ("member", PHONE)), spec, None) == "/phones/tok:phone:unkeyed"


def test_mask_key_and_member_key(spec, make_spec, tok) -> None:
    assert mask_key("C00000001", spec, tok) == tok.token("identifier", "C00000001")
    assert mask_key("C00000001", make_spec(key=KeyConfig(name="k")), tok) == "C00000001"  # a business code
    assert mask_member_key(PHONE, "$.phones", spec, tok) == tok.token("phone", PHONE)
    assert mask_member_key("HOME", "$.addresses", spec, tok) == "HOME"


# --- findings ------------------------------------------------------------------------------------


@pytest.fixture
def row(spec, tok):
    """The report row of a classified finding."""
    return lambda f: mask_finding(classify(f, spec, None), spec, tok)


def cells(r: dict) -> tuple:
    return r["value_a_tok"], r["value_b_tok"]


def test_mask_finding_row(spec, tok, make_finding) -> None:
    value = {"number": PHONE, "kind": "MOBILE", "verified": True, "ext": None, "rank": D("2")}
    f = make_finding("$.phones[*]", MISSING, value, None, parts=(("field", "phones"), ("member", PHONE)))
    f.member_key, f.array_path = PHONE, "$.phones"
    f = classify(f, spec, None)
    r = mask_finding(f, spec, tok)
    ptok = tok.token("phone", PHONE)
    assert (r["key"], r["json_pointer"], r["member_key_tok"]) == (
        tok.token("identifier", "C00000001"),
        f"/phones/{ptok}",
        ptok,
    )
    masked = dict(value, number=ptok, rank=tok.token("free_number", D(2)))  # booleans and nulls stay clear
    assert cells(r) == (dumps(masked, sort_keys=True), None)
    assert (r["value_class"], r["taxonomy_code"], r["severity"], r["change_type"], r["direction"]) == (
        "other",
        "FIELD_MISSING",
        "MEDIUM",
        "missing_in_b",
        "a_only",
    )
    assert (r["signature_hash"], r["suppressed"]) == (f.signature_hash, False)
    assert PHONE not in repr(r) and "C00000001" not in repr(r)


@pytest.mark.parametrize(
    ("path", "a", "b", "expected_a", "expected_b"),
    [
        ("$.segment", "PRIVATE", "WEALTH", "PRIVATE", "WEALTH"),
        ("$.accounts[*].interestRate", D("0.0125"), D("0.013"), "0.0125", "0.013"),
        ("$.consents[*].granted", True, False, True, False),
        ("$.preferences.marketing.sms", None, False, None, False),
        ("$.birthDate", "1990-05-17", "1990-05-71", "class:date_of_birth", "class:date_of_birth"),
        ("$.birthDate", None, "1990-05-17", None, "class:date_of_birth"),
        ("$.tags", KeyedMap(mode="multiset"), KeyedMap({'"x"': [Counted("x", 2)]}, mode="multiset"), "[]",
         '["x", "x"]'),  # a whole set or multiset shows as an array
    ],
)  # fmt: skip
def test_clear_and_class_only_values(row, make_finding, path, a, b, expected_a, expected_b) -> None:
    assert cells(row(make_finding(path, CHANGED, a, b))) == (expected_a, expected_b)


def test_tokenised_scalar_values(row, tok, make_finding) -> None:
    email = row(make_finding("$.email", CHANGED, "a@x.org", "b@x.org"))
    assert cells(email) == (tok.token("email", "a@x.org"), tok.token("email", "b@x.org"))
    score = row(make_finding("$.score", CHANGED, D("7"), "7"))
    assert cells(score) == (tok.token("free_number", D(7)), tok.token("free_text", "7"))
    assert score["value_class"] == "type_only"  # the explanation survives tokenisation


def test_normalisation_only_rows_show_raw_values(row, tok, make_finding) -> None:
    def raw_only(path, a_raw, b_raw, value):
        preset = {"raw_only": True, "taxonomy_code": "NORMALIZATION_ONLY"}
        return row(make_finding(path, CHANGED, value, value, a_raw=a_raw, b_raw=b_raw, **preset))

    segment = raw_only("$.segment", "retail ", "RETAIL", "RETAIL")
    assert (*cells(segment), segment["raw_only"]) == ("retail ", "RETAIL", True)
    email = raw_only("$.email", "A@x", "a@x", "a@x")
    assert (*cells(email), email["value_class"]) == (
        tok.token("email", "A@x"),
        tok.token("email", "a@x"),
        "case_only",
    )


def test_document_level_and_rule_fields(spec, tok, make_canonical, make_finding, today) -> None:
    present = SideState.complete(make_canonical({"email": "a@x"}))
    r = mask_finding(
        compare_pair("C00000001", SideState.absent(), present, spec, today=today).findings[0], spec, tok
    )
    assert (r["json_pointer"], r["path_pattern"], r["taxonomy_code"], *cells(r)) == (
        "",
        "$",
        "EXTRA_DOCUMENT",
        None,
        None,
    )
    assert r["member_key_tok"] == r["key"] == tok.token("identifier", "C00000001")
    [f] = enrich([make_finding("$.segment", CHANGED, "PRIVATE", "WEALTH")], spec, today=today)
    r = mask_finding(f, spec, tok)
    assert (r["rule_id"], r["rule_classification"], r["suppressed"]) == (
        "KD-CP-0001",
        "expected_by_design",
        True,
    )


# --- documents -----------------------------------------------------------------------------------


def test_mask_canonical_document(spec, tok) -> None:
    doc = {
        "customerId": "C00000001",
        "givenName": "Ada",
        "birthDate": "1990-05-17",
        "segment": "RETAIL",
        "phones": KeyedMap({PHONE: [{"number": PHONE, "kind": "MOBILE"}]}, mode="keyed"),
        "tags": KeyedMap({'"vip"': ["vip"]}, mode="set"),
        "counts": KeyedMap({'"x"': [Counted("x", 2)]}, mode="multiset"),
        "addresses": KeyedMap(
            {"HOME": [{"type": "HOME", "lines": ["Flat 2", "1 Example St"], "country": "GB"}]}
        ),
        "preferences": {"marketing": {"sms": False, "email": True}, "language": "en-GB"},
        "balance": D("1250.10"),
    }
    got = mask_document(doc, spec, tok)
    ptok = tok.token("phone", PHONE)
    assert got["customerId"] == tok.token("free_text", "C00000001")
    assert (got["givenName"], got["birthDate"], got["segment"]) == (
        tok.token("name", "Ada"),
        "class:date_of_birth",
        "RETAIL",
    )
    assert got["phones"] == {ptok: [{"number": ptok, "kind": "MOBILE"}]}
    assert got["tags"] == {'"vip"': ["vip"]}
    assert got["counts"] == {
        tok.token("free_text", '"x"'): [{"value": tok.token("free_text", "x"), "count": 2}]
    }
    home = got["addresses"]["HOME"][0]
    assert (home["lines"], home["country"]) == (
        [tok.token("address", "Flat 2"), tok.token("address", "1 Example St")],
        "GB",
    )
    assert got["preferences"] == {"marketing": {"sms": False, "email": True}, "language": "en-GB"}
    assert got["balance"] == tok.token("free_number", D("1250.1"))
    assert doc["givenName"] == "Ada"  # the input is not modified


SECRET = st.text(alphabet=st.characters(min_codepoint=0x41, max_codepoint=0x5A), min_size=8, max_size=16)


# spec and tok are immutable, so sharing them across generated inputs is safe.
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=SECRET, email=SECRET, phone=st.from_regex(r"\+44[0-9]{10}", fullmatch=True))
def test_protected_values_never_appear_in_masked_output(spec, tok, name: str, email: str, phone: str) -> None:
    doc = {
        "givenName": name,
        "email": email,
        "phones": KeyedMap({phone: [{"number": phone, "kind": "MOBILE"}]}),
    }
    text = dumps(mask_document(doc, spec, tok))
    assert name not in text and email not in text and phone not in text
