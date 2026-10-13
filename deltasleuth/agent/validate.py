"""The answer contract: at least one inline citation [F-…] [O-…] [RC-…] [K:<masked key>], each an
id that this session's tool results returned, and no number that no tool result holds: in digits, or in
a shorthand (2.4M, 1e6, 1.250.000) that no tool result shows."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

CITATION = re.compile(r"\[((?:F|O|RC)-[0-9A-Za-z]+|K:[^\]\n]+)\]")
# 1,234 is one number; digits inside a token, id or version are not a number.
NUMBER = re.compile(r"(?<![\w.])\d(?:[\d,]*\d)?(?:\.\d+)?(?!\w|\.\d)")
# A count NUMBER does not read: with a magnitude (2.4M, 37k, 3bn) or an exponent (1e6), or grouped by dots
# or underscores (1.250.000, 900_000). One group after a dot is a decimal (0.125), which NUMBER reads.
SHORTHAND = re.compile(
    r"(?<![\w.:])(?:\d[\d,]*(?:\.\d+)?(?:[kKmMbB]|bn|[eE][+-]?\d+)"
    r"|\d{1,3}(?:(?:\.\d{3}){2,}|(?:_\d{3})+))(?!\w)"
)
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
            self.numbers.update(map(_number, NUMBER.findall(data)))
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
    numbers = dict.fromkeys(NUMBER.findall(text))
    problems += [f"{n} appears in no tool result" for n in numbers if _number(n) not in evidence.numbers]
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


def _number(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _named(cited: str) -> str:
    """An unknown citation as a reader is told of it: an id of the stored shape as cited, any other by its
    kind alone (a masked key may be any text, so a ``K:`` citation always)."""
    if _STORED.fullmatch(cited):
        return f"[{cited}]"
    kind = "K:" if cited.startswith("K:") else cited.partition("-")[0] + "-"
    return f"a [{kind}...] citation"
