"""Tokeniser and key file."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import unicodedata
from decimal import Decimal as D
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deltasleuth.security.tokenise import (
    KeyFileError,
    Tokeniser,
    TokeniserError,
    is_token,
    load_or_create_key,
    mask_raw,
)

KEY = bytes(range(32))


def test_token_format_and_reference_value() -> None:
    digest = hmac.new(KEY, b"phone\x1f+447700900123", hashlib.sha256).digest()
    expected = "tok:phone:v1:" + base64.b32encode(digest).decode()[:26]
    assert Tokeniser(KEY).token("phone", "+447700900123") == expected
    assert Tokeniser(KEY, "v2").token("name", "Ada").startswith("tok:name:v2:")


def test_tokens_are_deterministic_keyed_and_domain_separated() -> None:
    tok = Tokeniser(KEY)
    assert tok.token("phone", "12345678") == Tokeniser(KEY).token("phone", "12345678")
    assert tok.token("phone", "12345678") != Tokeniser(b"\x02" * 32).token("phone", "12345678")
    assert tok.token("phone", "1").split(":")[3] != tok.token("account_number", "1").split(":")[3]


def test_tokens_use_the_canonical_scalar_text() -> None:
    tok = Tokeniser(KEY)
    assert tok.token("free_number", D("1.50")) == tok.token("free_number", D("1.5"))
    assert tok.token("free_text", "12") == tok.token("free_text", D(12))  # untyped, like member keys
    assert tok.token("name", "e\u0301") == tok.token("name", "\u00e9")  # NFC
    assert tok.token("free_text", {"b": 1, "a": 2}) == tok.token("free_text", {"a": 2, "b": 1})


@given(st.text(max_size=40), st.text(max_size=40))
def test_distinct_values_give_distinct_tokens(a: str, b: str) -> None:
    same = unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)
    assert (Tokeniser(KEY).token("name", a) == Tokeniser(KEY).token("name", b)) is same


def test_is_token() -> None:
    """A token's form, whichever key, version or class made it; nothing else, however close."""
    token = Tokeniser(KEY, "v2").token("free_text", "Ada")
    assert is_token(token) and is_token(Tokeniser(b"\x02" * 32).token("account_number", 1))
    for text in (
        "K12345678",
        "",
        token.lower(),
        token + " ",
        "x" + token,
        token[:-1],
        token + "A",
        token.replace("free_text", "Free"),
        "tok:name:unkeyed",  # the marker of a view without a key
        Tokeniser(KEY).scoped_label("date_of_birth", "K1", "1990-01-01"),
    ):
        assert not is_token(text), text


def test_mask_raw() -> None:
    """An answer no spec classifies: every string and number is a token of its kind, booleans and nulls
    stay, and a member name stays unless it reads as data: an id by the profiler's rule, or a name holding
    three digits in a row or four in all (an IBAN, a prefixed card number, a dashed account id). A name
    that is a word of data, or data with fewer digits, shows."""
    tok = Tokeniser(KEY)
    text, number = (lambda v: tok.token("free_text", v)), (lambda v: tok.token("free_number", v))
    data = ("12345", "ACC00012345", "ada@example.com", "+44 20 7946 0000", "abcdef0123456789", "a b", "", "é")
    data += ("1st", "n" * 65, "0d1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b")
    data += ("GB29NWBK60161331926819", "NL91ABNA0417164300", "FR1420041010050500013M02606")
    data += ("card-4111-1111-1111-1111", "ACC-2024-000123", "ACCOUNT00012345", "CUST-0001-A")
    data += ("sortcode-601613", "a1b2c3d4", "http_404_count")
    fields = ("address_line_1", "top10", "p99-latency", "acct_abcdef", "acct_1Nv0FGQ9RKHgCVdK")  # the limit
    doc = {
        "name": "Ada",
        "amount": D("1.50"),
        "open": True,
        "closed": None,
        "rows": ["Ada", D("1.5"), False, [None, "7", D(7)], {"_id": "x", "holder-1": {}}],
        "Smith": {name: D(1) for name in data},
        "fields": dict.fromkeys(fields),
        "n" * 64: [],
    }
    before = repr(doc)
    assert mask_raw(doc, tok) == {
        "name": text("Ada"),
        "amount": number(D("1.5")),
        "open": True,
        "closed": None,
        "rows": [
            text("Ada"),
            number(D("1.5")),
            False,
            [None, text("7"), number(D(7))],
            {"_id": text("x"), "holder-1": {}},
        ],
        "Smith": {text(name): number(D(1)) for name in data},
        "fields": dict.fromkeys(fields),
        "n" * 64: [],
    }
    assert repr(doc) == before and text("7") != number(D(7))
    assert (mask_raw("Ada", tok), mask_raw(D(2), tok), mask_raw(None, tok), mask_raw([], tok)) == (
        text("Ada"),
        number(D(2)),
        None,
        [],
    )
    assert mask_raw(doc, Tokeniser(b"\x02" * 32))["name"] != text("Ada")  # another key, other stand-ins


@pytest.mark.parametrize(
    ("key", "version", "cls"), [(b"short", "v1", "name"), (KEY, "v-1", "name"), (KEY, "v1", "Pho-ne")]
)
def test_invalid_key_version_or_class(key: bytes, version: str, cls: str) -> None:
    with pytest.raises(TokeniserError):
        Tokeniser(key, version).token(cls, "x")


def test_repr_hides_the_key() -> None:
    tok = Tokeniser(KEY, "v3")
    assert repr(tok) == "Tokeniser(version='v3')" and not hasattr(tok, "__dict__")


def test_creates_a_0600_key_and_reads_it_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("environment read")

    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(os.environ, "get", forbidden)
    path = tmp_path / "keys" / "tok.key"
    key = load_or_create_key(f"file:{path}")
    assert len(key) == 32 and path.read_text() == key.hex() + "\n"
    assert (stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(path.parent.stat().st_mode)) == (0o600, 0o700)
    assert load_or_create_key(f"file:{path}") == load_or_create_key(path) == key  # a path is a reference too


def test_an_existing_key_file_is_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "tok.key"
    path.write_text("ab" * 32)
    path.chmod(0o600)
    assert load_or_create_key(f"file:{path}") == bytes.fromhex("ab" * 32)


@pytest.mark.parametrize("ref", ["relative/x.key", "env:TOKEN_KEY", "vault://x", "file:"])
def test_only_file_references(ref: str) -> None:
    with pytest.raises(KeyFileError):
        load_or_create_key(ref)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_group_or_other_access_is_rejected(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "tok.key"
    path.write_text("ab" * 32)
    path.chmod(mode)
    with pytest.raises(KeyFileError, match="chmod 600"):
        load_or_create_key(f"file:{path}")


@pytest.mark.parametrize(("content", "message"), [("zz" * 32, "hexadecimal"), ("ab" * 8, "fewer than 32")])
def test_malformed_key_files(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "tok.key"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(KeyFileError, match=message) as exc:
        load_or_create_key(f"file:{path}")
    assert content not in str(exc.value)


def test_a_directory_is_not_a_key_file(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir(mode=0o700)
    with pytest.raises(KeyFileError, match="not a regular file"):
        load_or_create_key(f"file:{tmp_path / 'd'}")
