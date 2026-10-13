"""Comparison keys: ``key.normalize``, ``key.pattern`` and a composite key's parts.

The seven key normalisers are defined here (the schema's enum) so the loader and the pipeline
normalise keys identically; the canonical library's normalisers of the same names agree. Beside a
service side a composite key is normalised and matched part by part (``fit_parts``, ``parts_match``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence

from deltasleuth.core.spec.expressions import compile_key_pattern

_LINE_TERMINATOR = re.compile(r"[\n\r\u2028\u2029]")
_ESCAPED = re.compile(r"(?:[^\\]|\\[\\|])*")  # a multi-part key as given: every \ is of a \\ or a \|

KEY_NORMALISERS: dict[str, Callable[[str], str]] = {
    "trim": str.strip,
    "collapse_ws": lambda s: re.sub(r"\s+", " ", s),
    "nfkc": lambda s: unicodedata.normalize("NFKC", s),
    "casefold": str.casefold,
    "upper": str.upper,
    "lower": str.lower,
    "digits_only": lambda s: re.sub(r"[^0-9]", "", s),
}


def normalise_key(raw: str | int, chain: Sequence[str]) -> str:
    """Apply ``key.normalize`` to one raw key (an integer is keyed by its decimal text)."""
    key = str(raw)
    for name in chain:
        key = KEY_NORMALISERS[name](key)
    return key


def key_matches(pattern: str | re.Pattern[str], key: str) -> bool:
    """True when a normalised key may be requested: not empty/``.``/``..``, one line, and matches.

    A key is templated into request paths; Python's ``$`` would also accept a trailing newline.
    """
    if key in ("", ".", "..") or _LINE_TERMINATOR.search(key):
        return False
    compiled = compile_key_pattern(pattern) if isinstance(pattern, str) else pattern
    return compiled.search(key) is not None


def join_parts(parts: Sequence[str]) -> str:
    """A key's text: the value, or a composite key's parts joined by ``|`` (``\\`` and ``|`` escaped)."""
    if len(parts) == 1:
        return parts[0]
    return "|".join(p.replace("\\", "\\\\").replace("|", "\\|") for p in parts)


def split_parts(key: str, n: int) -> list[str] | None:
    """The ``n`` parts of a key's text; None for another number of parts."""
    if n == 1:
        return [key]
    parts, part, escaped = [], "", False
    for ch in key:
        if escaped or ch not in "\\|":
            part, escaped = part + ch, False
        elif ch == "\\":
            escaped = True
        else:
            parts.append(part)
            part = ""
    return None if escaped or len(parts) + 1 != n else [*parts, part]


def fit_parts(raw: str | int, chain: Sequence[str], n: int) -> str | None:
    """One raw key as its text: ``normalise_key`` of a key of one part; of ``n`` parts, each normalised
    and the parts joined again, None for another number of parts, or for a ``\\`` that escapes neither a
    ``\\`` nor a ``|``: ``split_parts`` would drop it, and another key than the one given be compared."""
    if n == 1:
        return normalise_key(raw, chain)
    parts = split_parts(str(raw), n) if _ESCAPED.fullmatch(str(raw)) else None
    return None if parts is None else join_parts([normalise_key(part, chain) for part in parts])


def parts_match(pattern: str | re.Pattern[str], key: str, n: int) -> bool:
    """``key_matches`` of a key of one part, or of every one of its ``n`` parts."""
    if n == 1:
        return key_matches(pattern, key)
    parts = split_parts(key, n)
    return parts is not None and all(key_matches(pattern, part) for part in parts)
