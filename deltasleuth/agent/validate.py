"""The answer contract: at least one inline citation [F-…] [O-…] [RC-…] [K:<masked key>], each an
id that this session's tool results returned, and no number that no tool result holds.

A number is read in each of these forms and judged by its value:

- digits, with a decimal part (``12.5``) or without its zero (``.97``);
- digits in groups of three behind commas (``1,234``), or behind a space, a no-break, narrow no-break,
  figure or thin space or an apostrophe (``1 000 000``, ``1'000'000``): one number, never a number a group;
- digits with a unit or a multiplier's ``x`` attached (``9000ms``, ``30x``; the units are ``UNITS``);
- cardinal words and their compounds (``ninety``, ``twelve thousand``, ``one hundred and five``), and
  digits before a magnitude in words (``12 thousand``).

A count in a shorthand (2.4M, 1e6, 1.250.000) is refused as written unless a tool result shows it so.

No number: digits inside a token, an id, a masked key or a version; an ordinal (``first``, ``2nd``); a
number word joined to another word by a hyphen (``four-eyes``, ``one-shot``); a side's name; the word
``one`` on its own, which is mostly a pronoun; and ``two`` before the pair's own noun (``the two sides``)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

CITATION = re.compile(r"\[((?:F|O|RC)-[0-9A-Za-z]+|K:[^\]\n]+)\]")
# What a number's digits may carry attached: a unit of time or size, a share, or a multiplier's x.
UNITS = (
    "ns", "us", "\u00b5s", "\u03bcs", "ms", "s", "sec", "secs", "second", "seconds", "min", "mins", "minute",
    "minutes", "h", "hr", "hrs", "hour", "hours", "day", "days", "kb", "mb", "gb", "tb", "kib", "mib", "gib",
    "tib", "byte", "bytes", "pct", "percent", "x",
)  # fmt: skip
_GAP = " \u00a0\u2007\u2009\u202f'\u2019"  # what groups of three digits stand behind, a comma aside
_APART = re.compile(f"[{_GAP}]")
# 1,234 and 1 234 are one number, .97 is one, and 250ms is one with its unit; digits inside a token, an id
# or a version are not a number.
NUMBER = re.compile(
    rf"(?<![\w.])(?P<digits>(?:\d{{1,3}}(?:[{_GAP}]\d{{3}})+|\d(?:[\d,]*\d)?)(?:\.\d+)?|\.\d+)"
    rf"(?P<unit>(?i:{'|'.join(UNITS)}))?(?!\w|\.\d)"
)
# A count NUMBER does not read: with a magnitude (2.4M, 37k, 3bn) or an exponent (1e6), or grouped by dots
# or underscores (1.250.000, 900_000). One group after a dot is a decimal (0.125), which NUMBER reads.
SHORTHAND = re.compile(
    r"(?<![\w.:])(?:\d[\d,]*(?:\.\d+)?(?:[kKmMbB]|bn|[eE][+-]?\d+)"
    r"|\d{1,3}(?:(?:\.\d{3}){2,}|(?:_\d{3})+))(?!\w)"
)
# The cardinal words, each with its kind and its value, and the kinds that continue a number by the kind of
# its last word: a hundred multiplies a count under a hundred, and a scale the count before it.
_CARDINALS = {
    "digit": dict(zip("one two three four five six seven eight nine".split(), range(1, 10), strict=True)),
    "teen": dict(zip(
        "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
        range(10, 20), strict=True,
    )),
    "tens": dict(zip(
        "twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10), strict=True
    )),
    "hundred": {"hundred": 100, "dozen": 12},
    "scale": {"thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12},
    "zero": {"zero": 0},
}  # fmt: skip
_KIND = {word: kind for kind, words in _CARDINALS.items() for word in words}
_VALUE = {word: value for words in _CARDINALS.values() for word, value in words.items()}
_NEXT = {
    "digit": ("hundred", "scale"),
    "teen": ("hundred", "scale"),
    "tens": ("digit", "hundred", "scale"),
    "hundred": ("digit", "teen", "tens", "scale"),
    "scale": ("digit", "teen", "tens"),
}
_WORD = "|".join(sorted(_KIND, key=len, reverse=True))
# A run of cardinal words: no part of a hyphenated word (four-eyes, twenty-first), nor inside a word.
WORDS = re.compile(rf"(?<![\w-])(?:{_WORD})(?:(?:\s+and\s+|\s+|-)(?:{_WORD}))*(?![\w-])", re.IGNORECASE)
_PART = re.compile(rf"{_WORD}|and", re.IGNORECASE)
# What multiplies the digits before it (12 thousand), and what "two" names the pair before (the two sides).
_MAGNITUDE = re.compile(rf"[\s-]?({'|'.join(_CARDINALS['hundred'] | _CARDINALS['scale'])})(?![\w-])", re.I)
_PAIR = re.compile(r"\s+(?:\w+\s+)?(?:sides|services|sources|systems)\b", re.IGNORECASE)
_STORED = re.compile(r"[FO]-[0-9a-f]{16}|RC-[0-9a-f]{12}")  # an id as the store writes one
# Only these fields hold citable ids: an id-shaped string inside a value is data, not evidence.
_CITABLE = {
    "finding_id": "",
    "root_cause_id": "",
    "obs_id": "",
    "obs_a_id": "",
    "obs_b_id": "",
    "key_masked": "K:",
    "sample_keys": "K:",
}


@dataclass
class Evidence:
    """The citable ids and the numbers of a session's tool results, and the shorthand its texts show (a
    value such as ``6M``)."""

    ids: set[str] = field(default_factory=set)
    numbers: set[Decimal] = field(default_factory=set)
    shorthand: set[str] = field(default_factory=set)

    def add(self, data: Any, name: str | None = None) -> None:
        """Take in one tool result's data (``name``: the field holding it)."""
        if isinstance(data, dict):
            for k, v in data.items():
                self.add(v, k)
        elif isinstance(data, list):
            for v in data:
                self.add(v, name)
        elif isinstance(data, str):
            self.numbers.update(value for _, value in stated(data))
            # Each group of a spaced number is also a value of its own: "10 250" may be two numbers.
            self.numbers.update(
                _number(group) for found in NUMBER.finditer(data) for group in _APART.split(found["digits"])
            )
            self.shorthand.update(SHORTHAND.findall(data))
            if name in _CITABLE:
                self.ids.add(_CITABLE[name] + data)
        elif isinstance(data, int | float | Decimal) and not isinstance(data, bool):
            self.numbers.add(Decimal(str(data)))


def check(
    answer: str, evidence: Evidence, must: str | None = None, *, shown: bool = False
) -> tuple[tuple[str, ...], list[str]]:
    """The answer's citations (first-use order) and its problems, none when it is valid. ``must``: the
    root cause a fixed question asked about, which the answer has to cite. ``shown``: the problems are for
    a reader (the abstention), not for the model's repair: an unknown citation's text is the model's own,
    so it is named by its kind unless it has a stored id's shape."""
    cited = tuple(dict.fromkeys(CITATION.findall(answer)))
    unknown = dict.fromkeys(_named(c) if shown else f"[{c}]" for c in cited if c not in evidence.ids)
    problems = [f"{c} is not an id of this session's tool results" for c in unknown]
    text = CITATION.sub(" ", answer)
    unheld = dict.fromkeys(written for written, value in stated(text) if value not in evidence.numbers)
    problems += [f"{written} appears in no tool result" for written in unheld]
    problems += [
        f"{n} is not written as a tool result shows it: write the full digits"
        for n in dict.fromkeys(SHORTHAND.findall(text))
        if n not in evidence.shorthand
    ]
    if not answer.strip():
        problems.append("the answer is empty")
    elif not cited:
        problems.append("the answer cites no tool result")
    if must is not None and must not in cited:
        problems.append(f"the answer does not cite [{must}], the root cause asked about")
    return cited, problems


def stated(text: str) -> list[tuple[str, Decimal]]:
    """Every number ``text`` states, in its order: as written, and its value."""
    found: dict[int, tuple[str, Decimal]] = {}
    words = text  # what the cardinal words are read from: a magnitude behind digits is no word of its own
    for digits in NUMBER.finditer(text):
        end, value = digits.end(), _number(_APART.sub("", digits["digits"]))
        if not digits["unit"] and (magnitude := _MAGNITUDE.match(text, end)):
            end, value = magnitude.end(), value * _VALUE[magnitude[1].lower()]
            words = words[: digits.end()] + "#" * (end - digits.end()) + words[end:]
        found[digits.start()] = text[digits.start() : end], value
    for run in WORDS.finditer(words):
        if run[0].lower() == "one":
            continue
        for start, end, value in _spelt(run):
            written = " ".join(text[start:end].split())
            if written.lower() != "two" or not _PAIR.match(text, end):
                found[start] = written, Decimal(value)
    return [found[start] for start in sorted(found)]


def _spelt(run: re.Match[str]) -> Iterator[tuple[int, int, int]]:
    """The numbers a run of cardinal words spells: where each starts and ends, and its value. A word that
    cannot continue the number before it starts the next (``three four`` is two numbers), and an ``and``
    joins only a hundred or a scale to the count behind it."""
    at = run.start()
    parts = [(at + part.start(), at + part.end(), part[0].lower()) for part in _PART.finditer(run[0])]
    span: list[int] = []  # where the number being read starts and ends
    total = group = floor = 0  # its thousands and above, its count under a thousand, its smallest scale
    last = ""  # the kind of its last word
    for n, (start, end, word) in enumerate(parts):
        kind = _KIND.get(word, "and")
        if kind == "and":
            ahead = _KIND.get(parts[n + 1][2]) if n + 1 < len(parts) else None
            if last in ("hundred", "scale") and ahead in ("digit", "teen", "tens"):
                continue
        value = _VALUE.get(word, 0)
        fits = (
            kind in _NEXT.get(last, ())
            and (kind != "hundred" or group < 100)
            and (kind != "scale" or value < floor)
        )
        if not fits:
            if span:
                yield span[0], span[1], total + group
            span, total, group, floor, last = [], 0, 0, 10**15, ""
            if kind == "and":
                continue
            span = [start, end]
        if kind == "hundred":
            group = (group or 1) * value
        elif kind == "scale":
            total, group, floor = total + (group or 1) * value, 0, value
        else:
            group += value
        span[1], last = end, kind
    if span:
        yield span[0], span[1], total + group


def _number(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _named(cited: str) -> str:
    """An unknown citation as a reader is told of it: an id of the stored shape as cited, any other by its
    kind alone (a masked key may be any text, so a ``K:`` citation always)."""
    if _STORED.fullmatch(cited):
        return f"[{cited}]"
    kind = "K:" if cited.startswith("K:") else cited.partition("-")[0] + "-"
    return f"a [{kind}...] citation"
