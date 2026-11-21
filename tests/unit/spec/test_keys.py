from __future__ import annotations

import pytest

from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.tables import decode_key, encode_key
from deltasleuth.core.spec import KEY_NORMALISERS, key_matches, normalise_key
from deltasleuth.core.spec.keys import fit_parts, join_parts, parts_match, split_parts
from deltasleuth.core.spec.schemas import core_schema


def test_normalisers_match_the_schema_enum() -> None:
    schema = core_schema()["$defs"]["key"]["properties"]["normalize"]
    assert set(KEY_NORMALISERS) == set(schema["items"]["enum"])


@pytest.mark.parametrize(
    ("raw", "chain", "expected"),
    [
        ("  c00012345 ", ["trim", "upper"], "C00012345"),
        ("a  b\tc", ["collapse_ws"], "a b c"),
        ("\uff21\uff22\uff23\uff11", ["nfkc"], "ABC1"),  # full-width ABC1
        ("Straße", ["casefold"], "strasse"),
        ("ORG-000123", ["lower"], "org-000123"),
        ("ORG-000123", ["digits_only"], "000123"),
        (12345, ["trim"], "12345"),
    ],
)
def test_normalise_key(raw: str | int, chain: list[str], expected: str) -> None:
    assert normalise_key(raw, chain) == expected


@pytest.mark.parametrize(
    ("pattern", "key", "ok"),
    [
        ("^C[0-9]{8}$", "C00012345", True),
        ("^C[0-9]{8}$", "C0001234", False),
        ("^C[0-9]{8}$", "C00012345\n", False),  # Python's $ would accept this; ECMA-262 does not
        ("^C[0-9]{8}$", "C00012345\r", False),
        (".*", "", False),
        (".*", ".", False),
        (".*", "..", False),  # never a dot segment, whatever the pattern
        (".*", "a.b", True),
        (r"^\d+$", "123", True),
        (r"^\d+$", "١٢٣", False),  # Arabic-Indic digits are not ECMA-262 \d
    ],
)
def test_key_matches(pattern: str, key: str, ok: bool) -> None:
    assert key_matches(pattern, key) is ok


def test_single_part_is_unchanged() -> None:
    """A key of one part, two tables' joined composite text too, is normalised and matched whole."""
    chain = ["trim", "upper"]
    assert fit_parts(" eu|1 ", chain, 1) == normalise_key(" eu|1 ", chain) == "EU|1"
    assert [fit_parts(raw, [], 1) for raw in (12345, "|1", "a\\")] == ["12345", "|1", "a\\"]
    assert parts_match(r"^[A-Z]+\|[0-9]$", "EU|1", 1) and not parts_match("^[A-Z]+$", "EU|1", 1)
    assert parts_match(".*", "|1", 1) and not parts_match(".*", "..", 1)
    assert (split_parts("eu|1", 1), join_parts(["eu|1"])) == (["eu|1"], "eu|1")
    assert encode_key is join_parts and decode_key("eu|1", 2) == split_parts("eu|1", 2) == ["eu", "1"]


def test_each_part_is_judged() -> None:
    """Of several parts each is normalised and matched on its own: one pattern, an alternation where the
    parts' shapes differ. A ``|`` or ``\\`` inside a part is written ``\\|`` or ``\\\\``."""
    key = fit_parts(" eu | a\\|b |  7 ", ["trim", "upper"], 3)
    assert key == "EU|A\\|B|7" and split_parts(key, 3) == ["EU", "A|B", "7"]
    assert parts_match(r"^([A-Z]{2}|A\|B|[0-9])$", key, 3)
    assert not parts_match("^[A-Z]{2}$", key, 3) and not parts_match(r"^EU\|", key, 3)  # nor the joined text
    parts = ["a|b", "c\\d", "|", "\\"]
    assert split_parts(join_parts(parts), 4) == parts and join_parts(parts) == "a\\|b|c\\\\d|\\||\\\\"
    assert fit_parts(join_parts(parts), [], 4) == join_parts(parts)


@pytest.mark.parametrize("raw", ["EU\\west|c1|r2", "eu|1\\x|2", "a\\b\\c|c1|r2"])
def test_a_backslash_escapes_only_itself_and_the_bar(raw: str) -> None:
    """A backslash before another character is no escape. Read leniently it is dropped, and another key
    than the one given compared (``EUwest``, which may be another record's): a key given so is refused,
    as one that ends in a backslash is. A stored two-table key reads as before."""
    assert fit_parts(raw, ["trim"], 3) is None
    assert split_parts(raw, 3) == decode_key(raw, 3) == [part.replace("\\", "") for part in raw.split("|")]
    doubled = raw.replace("\\", "\\\\")
    assert fit_parts(doubled, ["trim"], 3) == doubled and split_parts(doubled, 3) == raw.split("|")
    assert fit_parts(raw, ["trim"], 1) == raw  # a key of one part is its text


@pytest.mark.parametrize("raw", ["eu|1", "eu|1|x|y", "eu", 7, "eu|1|x\\", ""])
def test_part_count_and_dot_parts_refused(raw: str | int) -> None:
    assert fit_parts(raw, ["trim"], 3) is None and split_parts(str(raw), 3) is None
    assert not parts_match(".*", str(raw), 3)
    for part in ("", ".", "..", "x\ny"):  # a part is templated into a request path, as a key is
        assert not parts_match(".*", join_parts(["eu", part, "1"]), 3)
    assert fit_parts("eu| . |1", ["trim"], 3) == "eu|.|1" and parts_match(".*", "eu|...|1", 3)
    with pytest.raises(KeyRejectedError, match="a composite key needs 3 parts joined by \\|"):
        decode_key(str(raw), 3)
