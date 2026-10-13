"""Safe YAML/JSON reading: YAML 1.2 scalars, no duplicates, capped aliases, JSON-only tags."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from deltasleuth.core.spec import SpecError, load_spec_file
from deltasleuth.core.spec.documents import MAX_DOCUMENT_BYTES, parse_json_text, parse_yaml_text

EXAMPLES = Path(__file__).resolve().parents[3] / "docs" / "architecture" / "examples"


def _issue(text: str, parse: object = parse_yaml_text) -> tuple[str, str]:
    with pytest.raises(SpecError) as exc:
        parse(text)  # type: ignore[operator]
    (issue,) = exc.value.issues
    return issue.pointer, issue.code


@pytest.mark.parametrize(
    "stem", ["generic-customer-services", "entity-linking-services", "pg-vs-oracle-tables"]
)
def test_examples_read_like_plain_json_data(stem: str) -> None:
    path = EXAMPLES / f"{stem}.yaml"
    assert load_spec_file(path) == yaml.safe_load(path.read_text("utf-8"))


def test_yaml_12_core_scalars() -> None:
    data = parse_yaml_text(
        "on_: on\nno_: no\noctal: 017\nhex: 0x1F\noct12: 0o17\nexp: 1e-06\ndate: 2027-01-31\n"
        "sexagesimal: 1:30\ninf: .inf\nnull_: ~\nempty:\nupper: TRUE\nunderscore: 1_000\nneg: -3\n"
    )
    assert data == {
        **{"on_": "on", "no_": "no", "octal": 17, "hex": 31, "oct12": 15, "exp": 1e-06, "date": "2027-01-31"},
        **{"sexagesimal": "1:30", "inf": ".inf", "null_": None, "empty": None, "upper": True},
        **{"underscore": "1_000", "neg": -3},
    }


def test_aliases_and_merge_keys_are_allowed() -> None:
    data = parse_yaml_text("base: &h {x: 1, y: 2}\na: *h\nm:\n  <<: *h\n  y: 3\n")
    assert (data["a"], data["m"]) == ({"x": 1, "y": 2}, {"x": 1, "y": 3})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sides:\n  a:\n    select: $.x\n    select: $.y\n", ("/sides/a/select", "yaml_duplicate_key")),
        ('a: 1\n"a": 2\n', ("/a", "yaml_duplicate_key")),
        ("a: &a [1, *a]\n", ("", "yaml_recursive_alias")),
        ("a: !!python/object/apply:os.system [echo]\n", ("/a", "yaml_unsupported_tag")),
        ("a: !!binary aGVsbG8=\n", ("/a", "yaml_unsupported_tag")),
        ("a: !!timestamp 2026-01-01\n", ("/a", "yaml_unsupported_tag")),
        ("a:\n  b: !custom 1\n", ("/a/b", "yaml_unsupported_tag")),
        ("a:\n  2: b\n", ("/a", "yaml_non_string_key")),
        ("true: a\n", ("", "yaml_non_string_key")),
        ("a: [1, 2\n", ("", "yaml_syntax")),
        ("a: 1\n---\nb: 2\n", ("", "yaml_syntax")),
        ("a: \x01\n", ("", "yaml_syntax")),
        ("a: !!int abc\n", ("", "yaml_bad_scalar")),
        ("a: !!bool yes\n", ("", "yaml_bad_scalar")),
        ("a: 1e999\n", ("", "number_out_of_range")),  # no float holds it: an infinity is no JSON data
        ("a: [1.5, -1E+400]\n", ("", "number_out_of_range")),
        ("a: !!float inf\n", ("", "number_out_of_range")),
        ("a: !!float nan\n", ("", "number_out_of_range")),
        ("a: " + "[" * 80 + "]" * 80 + "\n", ("", "document_too_deep")),
        ("a: " + "[" * 5000 + "]" * 5000 + "\n", ("", "document_too_deep")),
    ],
)
def test_unsafe_or_malformed_yaml(text: str, expected: tuple[str, str]) -> None:
    assert _issue(text) == expected


def test_duplicate_key_message_has_its_line() -> None:
    with pytest.raises(SpecError, match="line 4"):
        parse_yaml_text("sides:\n  a:\n    select: $.x\n    select: $.y\n")


def test_alias_bomb_is_refused_before_expansion() -> None:
    levels = ['a: &a ["x","x","x","x","x","x","x","x","x","x"]']
    for prev, name in zip("abcdefgh", "bcdefghi", strict=True):
        levels.append(f"{name}: &{name} [" + ",".join([f"*{prev}"] * 10) + "]")
    assert _issue("\n".join(levels) + "\n") == ("", "yaml_alias_limit")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ('{"a": 1, "a": 2}', "json_duplicate_key"),
        ('{"a": NaN}', "json_syntax"),
        ('{"a": 1e999}', "number_out_of_range"),
        ('{"a": [1.5, -1E+400]}', "number_out_of_range"),
        ('{"a": ', "json_syntax"),
        ('{"a": ' + "[" * 70 + "]" * 70 + "}", "document_too_deep"),
    ],
)
def test_malformed_json(text: str, code: str) -> None:
    assert _issue(text, parse_json_text)[1] == code


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        (
            "spec.json",
            b'{"apiVersion": "deltasleuth/v1", "n": 1.5}',
            {"apiVersion": "deltasleuth/v1", "n": 1.5},
        ),
        ("spec.yaml", b"\xef\xbb\xbfa: 1\n", {"a": 1}),  # a UTF-8 BOM is tolerated
        ("spec.yaml", b"- a\n- b\n", "document_not_mapping"),
        ("spec.yaml", b"", "document_not_mapping"),
        ("spec.yaml", b"a: \xff\n", "document_unreadable"),
        ("spec.yaml", b"a: " + b"x" * (MAX_DOCUMENT_BYTES + 1), "document_too_large"),
        ("absent.yaml", None, "document_unreadable"),
    ],
)
def test_load_spec_file(tmp_path: Path, name: str, content: bytes | None, expected: object) -> None:
    path = tmp_path / name
    if content is not None:
        path.write_bytes(content)
    if isinstance(expected, dict):
        assert load_spec_file(path) == expected
    else:
        with pytest.raises(SpecError) as exc:
            load_spec_file(path)
        assert exc.value.issues[0].code == expected


# -- inline key lists keep the text they were written with -------------------------------


def test_inline_key_list_items_keep_their_text() -> None:
    data = parse_yaml_text(
        "key:\n  source:\n    kind: list\n"
        "    keys: [00012001, 0o17, 0x1F, 1E3, 2.0, .5, -7, true, ~, 'q', !!int 12, !!str 13]\n"
        "other: [00012001, 1E3, true]\n"
    )
    assert data["key"]["source"]["keys"] == [
        *("00012001", "0o17", "0x1F", "1E3", "2.0", ".5", "-7", "true", "~", "q"),
        12,  # an explicit tag still types the item
        "13",
    ]
    assert data["other"] == [12001, 1000.0, True]  # everywhere else YAML 1.2 typing is unchanged


def test_nested_and_merged_key_lists_keep_their_text() -> None:
    data = parse_yaml_text(
        "defaults: &d {kind: list, keys: [1E3, 0x10]}\n"
        "key:\n  source:\n    kind: union\n    sources:\n"
        "      - {<<: *d}\n"
        "      - {kind: hash_sample, source: {kind: list, keys: [007]}}\n"
    )
    sources = data["key"]["source"]["sources"]
    assert (sources[0]["keys"], sources[1]["source"]["keys"]) == (["1E3", "0x10"], ["007"])


def test_an_aliased_key_keeps_its_type_elsewhere() -> None:
    data = parse_yaml_text("n: &n 0x10\nkey: {source: {kind: list, keys: [*n]}}\n")
    assert (data["n"], data["key"]["source"]["keys"]) == (16, ["0x10"])


@pytest.mark.parametrize("key", ["1E3", "0o17", "1e10", ".5", "00012001", "0x1F", "12", "-0", "1_000"])
def test_keys_written_by_a_yaml_11_dumper_read_back_unchanged(key: str) -> None:
    # The profiler writes drafts with PyYAML's dumper, which leaves 1E3 and 0o17 unquoted.
    text = yaml.safe_dump({"key": {"source": {"kind": "list", "keys": [key]}}})
    assert parse_yaml_text(text)["key"]["source"]["keys"] == [key]
