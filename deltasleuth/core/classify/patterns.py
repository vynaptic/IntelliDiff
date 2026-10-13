"""Structural RFC 9535 position patterns, so rule paths and finding patterns match by position.

``$['phones'][0].number`` and ``$.phones[*].number`` parse to the same segments: an index is a
wildcard, since no rule distinguishes array positions. Filters, slices and unions select by value
or range, which no policy or severity rule may depend on, so they raise ``PatternError``.
"""

from __future__ import annotations

import re
from functools import lru_cache

Segment = tuple[str, ...]  # WILD, DESC or ("name", <member name>)
Segments = tuple[Segment, ...]
WILD: Segment = ("*",)  # any array element or object member
DESC: Segment = ("..",)  # the descendant segment: zero or more levels

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\*")
_BRACKET = re.compile(r"""\[(?:\*|-?[0-9]+|('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"))\]""")
_ESCAPE = re.compile(r"""\\(?:u([0-9A-Fa-f]{4})|(['"\\/bfnrt]))|\\""")  # the bare \ is a bad escape
_SHORT_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class PatternError(ValueError):
    """A path is not a supported RFC 9535 position pattern."""


def name(member: str) -> Segment:
    return ("name", member)


@lru_cache(maxsize=4096)
def segments(path: str) -> Segments:
    """Parse ``path`` into segments; raises ``PatternError`` for unsupported syntax."""
    text = path.strip()
    if not text.startswith("$"):
        raise PatternError(f"a path must start with $: {path!r}")
    out: list[Segment] = []
    pos = 1
    while pos < len(text):
        if text.startswith("..", pos):
            out.append(DESC)
            pos += 2
            dotted = not text.startswith("[", pos)
        elif text.startswith(".", pos):
            pos += 1
            dotted = True
        else:
            dotted = False
        m = (_NAME if dotted else _BRACKET).match(text, pos)
        if m is None:
            raise PatternError(f"unsupported selector at offset {pos} of path {path!r}")
        pos = m.end()
        if dotted:
            out.append(WILD if m[0] == "*" else name(m[0]))
        else:  # an index is a wildcard too
            out.append(WILD if m[1] is None else name(_unquote(m[1], path)))
    return tuple(out)


def _unquote(quoted: str, path: str) -> str:
    def unescape(m: re.Match[str]) -> str:
        if m[1]:
            return chr(int(m[1], 16))
        if m[2]:
            return _SHORT_ESCAPES.get(m[2], m[2])
        raise PatternError(f"bad escape in path {path!r}")

    return _ESCAPE.sub(unescape, quoted[1:-1])


def covers(rule: Segments, path: Segments) -> bool:
    """True when ``rule`` selects ``path`` or one of its ancestors (a rule covers its subtree)."""
    return _walk(rule, path, below=False)


def reaches_below(rule: Segments, path: Segments) -> bool:
    """True when ``rule`` can select a strict descendant of ``path`` without covering ``path``."""
    return _walk(rule, path, below=True)


def _walk(rule: Segments, path: Segments, *, below: bool) -> bool:
    if not rule:
        return not below  # the rule ends at or above path
    if rule[0] == DESC:
        return any(_walk(rule[1:], path[i:], below=below) for i in range(len(path) + 1))
    if not path:
        return below  # the rule goes on below path
    return rule[0] in (WILD, path[0]) and _walk(rule[1:], path[1:], below=below)


def specificity(rule: Segments) -> tuple[int, int]:
    """Order of "most specific rule wins": positional depth, then concrete names."""
    return sum(s != DESC for s in rule), sum(s not in (WILD, DESC) for s in rule)
