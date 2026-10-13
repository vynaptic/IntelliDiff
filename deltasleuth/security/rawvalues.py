"""Raw key-shaped values in text a pack or the audit log carries forever (docs/mvp/PACK.md §2.3).

Outside tokens (``tok:…``, ``class:…``), a text's words are its runs of characters other than whitespace,
quotes, brackets and ``, ; = < > |``, without leading or trailing punctuation or symbols; a word's parts are
its pieces between ``- _ / . : @ + # ~``, and between any characters but ASCII letters and digits. A word
is a raw key when it, or one of its parts, normalised by ``key.normalize`` passes the spec's own key check
(``keys.key_matches`` with ``key.pattern``: its anchors, inline flags and search as when keys are read);
under ``digits_only``, so is a run of digit groups split by spaces, brackets, dots, slashes or dashes,
unless it holds a date. A word wholly in a shape DeltaSleuth writes itself (an ISO-8601 time or date, a
semantic version, an id, a hash, digest or salt, an IP address) is none. Keys of class ``business_code``
are stored in clear by design and never searched for. A missing pattern, or one matching the empty
string, cannot tell a key from other text.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Callable

from deltasleuth.core.spec.expressions import compile_key_pattern
from deltasleuth.core.spec.keys import key_matches, normalise_key
from deltasleuth.core.types import CompiledSpec
from deltasleuth.security.policy import BUSINESS_CODE

KEY_SHAPED = "the reason holds a key-shaped value: write it without customer keys"
_TOKENS = re.compile(
    r"tok:[a-z][a-z0-9_]*:[A-Za-z0-9]+:(?:[A-Z2-7]{26}|unkeyed)|class:[a-z][a-z0-9_]*(?:#[A-Z2-7]+)?"
)
_WORD = re.compile(r"[^\s\"'`,;()\[\]{}<>=|]+")
_PARTS = re.compile(r"[-_/.:@+#~]")
_PIECES = re.compile(r"[^0-9A-Za-z]+")
_SPACED = re.compile(r"\+?\(?[0-9]+(?:[ ()./-]+[0-9]+)+")  # digit groups
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_GENERATED = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"  # an ISO-8601 date, or time
    r"(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?)?"
    r"|(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}"  # a semantic version (spec.schema.json's semver)
    r"|(?:user:|token:)?(?:(?:[ADJMNPRV]|LI|SC)-[0-9A-Z]{26}|[CEFIKLOQSTUW]-[0-9a-f]{16}|RC-[0-9a-f]{12})"
    # a hash, digest or salt, holding a letter (16 digits may be a card number)
    r"|(?:sha256:)?(?=[0-9]*[a-f])(?:[0-9a-f]{16}|[0-9a-f]{32}|[0-9a-f]{64})"
)
_ADDRESS = re.compile(r"[0-9A-Fa-f]*[.:][0-9A-Fa-f.:]*")


class EmptyPattern(ValueError):
    """The spec has no key pattern, or it matches the empty string."""


def _generated(word: str) -> bool:
    """Whether ``word`` is wholly in a shape DeltaSleuth writes itself."""
    if _GENERATED.fullmatch(word):
        return True
    if _ADDRESS.fullmatch(word):
        try:
            ipaddress.ip_address(word)
        except ValueError:
            return False
        return True
    return False


def searcher(spec: CompiledSpec) -> Callable[[str], int] | None:
    """What counts the raw keys of the spec a text holds, a word at most once; None for ``business_code``
    keys. EmptyPattern when ``key.pattern`` is missing or search-matches the empty string."""
    if spec.key.value_class == BUSINESS_CODE:
        return None
    pattern = compile_key_pattern(spec.key.pattern or "")  # compiled when the spec loaded
    if spec.key.pattern is None or pattern.search("") is not None:
        raise EmptyPattern(spec.key.pattern)
    chain = [n.name for n in spec.key.normalize]
    judged: dict[str, bool] = {}

    def key(candidate: str) -> bool:
        return key_matches(pattern, normalise_key(candidate, chain))

    def raw(run: str) -> bool:
        if run not in judged:
            word = _stripped(run)
            generated = _generated(run) or _generated(word)
            candidates = {run, word, *_PARTS.split(word), *_PIECES.split(word)}
            judged[run] = not generated and any(map(key, candidates))
        return judged[run]

    def count(text: str) -> int:
        text = _TOKENS.sub(" ", text)
        found = sum(map(raw, _WORD.findall(text)))
        if "digits_only" in chain:  # a key its words split: 071 234 5601, (071) 234-5601
            spaced = (run for run in _SPACED.findall(text) if " " in run and not _DATE.search(run))
            found += sum(map(key, spaced))
        return found

    return count


def _stripped(run: str) -> str:
    """``run`` without leading or trailing punctuation or symbols (Unicode categories P and S)."""
    start, end = 0, len(run)
    while start < end and unicodedata.category(run[start])[0] in "PS":
        start += 1
    while end > start and unicodedata.category(run[end - 1])[0] in "PS":
        end -= 1
    return run[start:end]


def key_shaped(spec: CompiledSpec, text: str) -> bool:
    """Whether a reason that segments carry forever (a hold's, an erasure's, a revoke's) holds a raw
    key-shaped value of the spec, searched as the pack's texts are; a pattern that cannot tell leaves it
    to the pack's gate."""
    try:
        search = searcher(spec)
    except EmptyPattern:
        return False
    return search is not None and search(text) > 0


def no_pack(spec_id: int, spec: CompiledSpec) -> str | None:
    """Why no pack is issued for any run of the spec, as the pack and a sign-off precheck say it: its key
    pattern cannot tell a key (``searcher``'s EmptyPattern); None when it can."""
    try:
        searcher(spec)
    except EmptyPattern:
        return (
            f"spec {spec_id}'s key.pattern matches the empty string: a raw key cannot be told from other"
            " text, so no pack"
        )
    return None
