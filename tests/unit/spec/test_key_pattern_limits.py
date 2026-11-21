"""Every key-pattern entry point has bounded work and keeps its matching contract."""

from __future__ import annotations

import re
import subprocess
import sys
from itertools import product
from typing import Any

import pytest

from deltasleuth.core.spec import compile_spec, validate_spec
from deltasleuth.core.spec import expressions as patterns
from deltasleuth.core.spec.expressions import KeyPatternLimitError, compile_key_pattern
from deltasleuth.core.spec.keys import key_matches, parts_match
from deltasleuth.security import rawvalues


@pytest.mark.parametrize("pattern", ["^(a+)+$", "^(a|aa)+$"])
def test_backtracking_key_pattern_finishes_in_a_bounded_process(pattern: str) -> None:
    # A process deadline keeps the test bounded on an unfixed build, including CPython re's GIL hold.
    source = """
import sys
from deltasleuth.core.spec.keys import key_matches
try:
    result = key_matches(sys.argv[1], 'a' * 50000 + '!')
except ValueError:
    print('limited')
else:
    assert result is False
    print('no match')
"""
    run = subprocess.run([sys.executable, "-c", source, pattern], capture_output=True, text=True, timeout=3)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() in {"limited", "no match"}


@pytest.mark.parametrize(
    ("pattern", "texts"),
    [
        (r"^\d+\w$", ["123A", "\u0661\u0662\u0663A", "123é", "x123A"]),
        (r"(?i)^[a-z]{2}[0-9]{2}$", ["aB12", "AB12", "\u0130B12", "aB123"]),
        (r"(?x) ^ [A-Z] [0-9]{2} $", ["A12", "B23", "xA12", "A12\n"]),
        (r"(?i:ab)C", ["ABC", "AbC", "abc", "xABCy"]),
        (r"(?u:\w+)", ["é", "abc", "\u0085", "\u0301", "\u200c", "\u00b2", ""]),
        (r"(?iu:[a-z]+)", ["\u0130", "\u0131", "\u017f", "\u212a"]),
        (r"\B", ["", " ", "a", "aa"]),
        (r"a{e<=1}", ["a", "b", "a{e<=1}"]),
        (r"(?<!x)a(?=b)", ["ab", "xab", "cab", "a"]),
        (r"\A([A-Z]+)-\1\Z", ["AB-AB", "AB-CD", "xAB-ABy"]),
        (r"(a)?(?(1)b|c)", ["ab", "c", "ac", "d"]),
        (r"[0-9]{2}", ["x12y", "123", "1", ""]),
        (r"a*", ["", "a", "xyz"]),
    ],
)
def test_bounded_matcher_preserves_python_ascii_search_contract(pattern: str, texts: list[str]) -> None:
    bounded, original = compile_key_pattern(pattern), re.compile(pattern, re.ASCII)
    for text in texts:
        assert (bounded.search(text) is not None) == (original.search(text) is not None)


def test_native_grammar_agrees_with_python_over_small_adversarial_strings() -> None:
    atoms = ["a", "b", "é", "[ab]", "[^a]", r"\w", r"\W", r"\d", r"\s", ".", r"\b", r"\A", r"\Z"]
    texts = ["".join(chars) for n in range(4) for chars in product("a1é\n", repeat=n)]
    texts += ["\u0130", "\u0131", "\u017f", "\u212a", "\u0301", "\u00b2"]
    sources = atoms + [a + b for a, b in product(atoms, repeat=2)]
    sources += [f"^(?:{a}|{b})+$" for a, b in product(atoms[:10], repeat=2)]
    sources += [f"{a}{q}" for a in atoms[:10] for q in ("?", "*", "+", "{1,3}", "++")]
    for source, prefix in product(sources, ("", "(?i)", "(?s)", "(?m)")):
        bounded = compile_key_pattern(prefix + source)
        original = re.compile(prefix + source, re.ASCII)
        assert bounded._compiled is not None  # these are the deliberately narrow fast-path grammar
        for text in texts:
            assert (bounded.search(text) is not None) == (original.search(text) is not None)


@pytest.mark.parametrize("pattern", ["a" * 4001, "a{100000000}", "(?:a{1000}){1000}"])
def test_compilation_limits_precede_the_native_compiler(
    pattern: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the native compiler received an oversized program")

    monkeypatch.setattr(patterns.regex, "compile", forbidden)
    with pytest.raises(KeyPatternLimitError):
        compile_key_pattern(pattern)


def test_compiled_python_patterns_and_composite_parts_use_the_bound() -> None:
    assert key_matches(re.compile(r"^é$", re.IGNORECASE), "É")
    assert parts_match(r"(?i)^(GB|[0-9]{4})$", "gb|1234", 2)
    with pytest.raises(KeyPatternLimitError, match="time limit"):
        key_matches(re.compile(r"^(a|aa)+$", re.ASCII), "a" * 100 + "b")


def test_search_work_bounds_capture_history_before_native_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    pattern = compile_key_pattern("(" * 80 + "a" + ")" * 80 + "+")
    monkeypatch.setattr(patterns, "MAX_KEY_SEARCH_WORK", 1000)
    with pytest.raises(KeyPatternLimitError, match="search work limit"):
        pattern.search("a" * 100)


def test_inline_keys_report_a_nonpayload_pattern_issue(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["key"].update(
        pattern="^(a|aa)+$", normalize=[], source={"kind": "list", "keys": ["a" * 100 + "b"]}
    )
    issues = validate_spec(minimal_spec)
    assert [(i.pointer, i.code) for i in issues] == [("/key/pattern", "key_pattern_limit")]
    assert "a" * 100 not in issues[0].message


def test_authoring_budget_is_per_validation(
    minimal_spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as bounded:
        bounded.setattr(patterns, "KEY_AUTHORING_SECONDS", 0)
        assert "key_pattern_limit" in {issue.code for issue in validate_spec(minimal_spec)}
    assert not [issue for issue in validate_spec(minimal_spec) if issue.severity == "error"]


def test_raw_value_gate_raises_on_exhaustion_and_keeps_empty_pattern_rule(
    minimal_spec: dict[str, Any],
) -> None:
    minimal_spec["key"].update(
        pattern="^(a|aa)+$", normalize=[], source={"kind": "list", "keys": ["a"]},
        **{"class": "identifier"},
    )
    spec = compile_spec(minimal_spec)
    search = rawvalues.searcher(spec)
    assert search is not None and search("aaa") == 1
    with pytest.raises(KeyPatternLimitError, match="time limit"):
        search("a" * 100 + "b")
    minimal_spec["key"]["pattern"] = "a*"
    with pytest.raises(rawvalues.EmptyPattern):
        rawvalues.searcher(compile_spec(minimal_spec))
