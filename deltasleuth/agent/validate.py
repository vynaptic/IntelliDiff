"""The answer contract: at least one inline citation [F-…] [O-…] [RC-…] [K:<masked key>], each an
id that this session's tool results returned, and no number that no tool result holds. Sentence by
sentence: one that states a number or names an outcome cites at least one such id, and each number it
states stands in the record of an id it cites. Counts of keys, findings and named outcomes must agree
with the cited engine field, and a named key outcome with the stored result. A sentence that does
neither may stand uncited.

A record is a mapping of a tool result that holds a citable id, with everything nested in it: a finding,
a root cause, a side's observation, a key's view. A number that stands in no record (a run's counters,
beside its root causes) is the record of every id its result holds. A sentence ends at a line break, or
at a full stop, question or exclamation mark before a space; citations that open the next sentence are
the ones before it too (``It differs. [F-…]``).

A sentence names an outcome when it holds an outcome's name or its plain form (``MISMATCH``, ``match``,
``differ``, ``missing``, ``not found``, ...), or a verdict on the run (``certified``, ``approved``,
``signed off``, ``cut-over``), unless ``not``, ``never`` or ``no`` stands right before the verdict.

A number is read in each of these forms and judged by its value:

- digits, with a decimal part (``12.5``) or without its zero (``.97``);
- digits in groups of three behind commas (``1,234``), or behind a space, a no-break, narrow no-break,
  figure or thin space or an apostrophe (``1 000 000``, ``1'000'000``): one number, never a number a group;
- digits with a unit or a multiplier's ``x`` attached (``9000ms``, ``30x``; the units are ``UNITS``);
- cardinal words and their compounds (``ninety``, ``twelve thousand``, ``one hundred and five``), and
  digits before a magnitude in words (``12 thousand``).

A count in a shorthand (2.4M, 1e6, 1.250.000) is refused as written unless a tool result shows it so.

No number: digits inside a token, an id, a masked key, a code (``KEY-000007``) or a version; an ordinal
(``first``, ``2nd``); a number word joined to another word by a hyphen (``four-eyes``, ``one-shot``); a
side's name; the word ``one`` on its own, which is mostly a pronoun; and ``two`` before the pair's own noun
(``the two sides``)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from typing import Any

from deltasleuth.core.types import Outcome, Verdict

CITATION = re.compile(r"\[((?:F|O|RC)-[0-9A-Za-z]+|K:[^\]\n]+)\]")
# What a number's digits may carry attached: a unit of time or size, a share, or a multiplier's x.
UNITS = (
    "ns", "us", "\u00b5s", "\u03bcs", "ms", "s", "sec", "secs", "second", "seconds", "min", "mins", "minute",
    "minutes", "h", "hr", "hrs", "hour", "hours", "day", "days", "kb", "mb", "gb", "tb", "kib", "mib", "gib",
    "tib", "byte", "bytes", "pct", "percent", "x",
)  # fmt: skip
_GAP = " \u00a0\u2007\u2009\u202f'\u2019"  # what groups of three digits stand behind, a comma aside
_APART = re.compile(f"[{_GAP}]")
# 1,234 and 1 234 are one number, .97 is one, and 250ms is one with its unit; digits inside a token, an id,
# a code behind a word and a hyphen (KEY-000007) or a version are not a number.
NUMBER = re.compile(
    r"(?<![\w.])(?<![A-Za-z_]-)(?P<sign>[-+\u2212]?)"
    rf"(?P<digits>(?:\d{{1,3}}(?:[{_GAP}]\d{{3}})+|\d(?:[\d,]*\d)?)(?:\.\d+)?|\.\d+)"
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
# The outcomes by their names (an underscore read as a space too) and their plain forms; and a verdict on
# the run, which no tool result holds, unless ``not``, ``never`` or ``no`` stands right before it.
_NAMES = sorted({value.lower().replace("_", "[ _]") for value in (*Outcome, *Verdict)}, key=len, reverse=True)
OUTCOME = re.compile(
    rf"\b(?:(?:{'|'.join(_NAMES)})s?|(?:mis)?match(?:es|ed|ing)?|differ\w*|missing|not found"
    r"|drift(?:ed|ing)?)\b",
    re.IGNORECASE,
)
VERDICT = re.compile(
    r"(?<!\bnot )(?<!\bnever )(?<!\bno )\b(?:certif\w*|approv\w*|attest\w*|sign(?:s|ed|ing)?[- ]?off"
    r"|cut[- ]?over)\b",
    re.IGNORECASE,
)
# Where a sentence ends: a line break, or a full stop, question or exclamation mark (and what closes a
# quote or bracket) before a space, but not after an abbreviation.
_END = re.compile(r"\n|[.!?]+[\"'\u2019\u201d)\]]*(?=\s)")
_ABBREVIATED = re.compile(r"(?:^|[\s(])(?:e\.g|i\.e|etc|vs|cf|approx|incl|no|nr|fig)$", re.IGNORECASE)
_LEADING = re.compile(rf"\s*(?:{CITATION.pattern}[\s,;:]*)+")  # citations that open a sentence
_EXCERPT = 80  # characters of a sentence the repair request quotes
_OUTCOME_NAME = "|".join(o.value for o in Outcome)
_COUNT_OF = re.compile(
    rf"\s+(?:(?:keys?\s+)?(?:(?:is|are|were|was)\s+)?(?:a\s+)?(?P<outcome>{_OUTCOME_NAME})"
    r"(?:es|ed|s)?\b|(?P<noun>keys?|findings?)\b)", re.I,
)
_TOTAL_BEFORE = re.compile(r"(?:^\s*of|\b(?:out of|compared|total(?: of)?))\s*$", re.I)
_TOTAL_AFTER = re.compile(r"\s+(?:(?:were|was)\s+)?(?:compared|in total)\b", re.I)
_RUN_BEFORE = re.compile(r"\b(?:(?:across|in|for) (?:the|this) run,?|the run (?:has|had|recorded))\s*$", re.I)
_DIFFER = re.compile(r"\s+differ(?:ed)?\b", re.I)
_LABEL_BEFORE = re.compile(rf"\b(?P<outcome>{_OUTCOME_NAME})\s*[:=]\s*$", re.I)
_COUNT_LABEL = re.compile(r"\b(?P<name>affected[ _]keys|finding[ _]count)\s*(?:[:=]|\bis)\s*$", re.I)
_ONE_COUNT = re.compile(r"\s+(?:keys?|findings?)\b", re.I)
_ENUM = re.compile(rf"\b(?:{_OUTCOME_NAME})\b")
_OUTCOME_CLAIM = re.compile(
    rf"\b(?:key(?:'s outcome)?|outcome|result)\s+(?:(?:is|was|of)\s+)?(?:a\s+)?(?P<outcome>{_OUTCOME_NAME})"
    r"(?:es|ed|s)?\b", re.I,
)
_UNIVERSAL = re.compile(rf"\b(?:every key|all keys)\s+(?P<outcome>{_OUTCOME_NAME})(?:es|ed|s)?\b", re.I)
_RELEASE_CLAIM = re.compile(
    r"\b(?:run|comparison|cut[- ]over|release)\s+(?:is|was|has been)\s+"
    r"(?:certified|approved|signed[- ]off|safe|ready for cut[- ]over)\b", re.I,
)


@dataclass
class Evidence:
    """The citable ids and the numbers of a session's tool results, the numbers of the records each id
    stands in (``held``), and the shorthand its texts show (a value such as ``6M``)."""

    ids: set[str] = field(default_factory=set)
    numbers: set[Decimal] = field(default_factory=set)
    shorthand: set[str] = field(default_factory=set)
    held: dict[str, set[Decimal]] = field(default_factory=dict)
    # Named engine fields; an equal number in a finding's displayed value cannot stand in for a count.
    facts: dict[str, dict[str, str | int]] = field(default_factory=dict)

    def add(self, data: Any) -> None:
        """Take in one tool result's data: each number under the ids of the records it stands in, and one
        that stands in no record under every id the result holds."""
        loose: set[Decimal] = set()
        found: set[str] = set()
        self._add(data, (), loose, found)
        for cited in found:
            self.held.setdefault(cited, set()).update(loose)
        if isinstance(data, dict):
            self._engine_facts(data, found)

    def _engine_facts(self, data: dict[str, Any], found: set[str]) -> None:
        counters = data.get("counters") or {}
        counts = {k: v for k, v in counters.get("by_outcome", counters).items() if k in Outcome}
        if "different" in counters:
            counts["different"] = counters["different"]
        total = next((counters[k] for k in ("total", "keys", "compared") if k in counters), None)
        if total is not None:
            counts["total"] = total
        for cited in found:
            self.facts.setdefault(cited, {}).update(counts)
        for cause in [data, *data.get("root_causes", [])]:
            if "root_cause_id" in cause:
                fields = {k: cause[k] for k in ("affected_keys", "finding_count") if k in cause}
                self.facts.setdefault(cause["root_cause_id"], {}).update(fields)
        result = data.get("result") or data
        if data.get("key_masked") and result.get("outcome") in Outcome:
            for cited in found:
                if not cited.startswith("RC-"):
                    self.facts.setdefault(cited, {})["outcome"] = result["outcome"]

    def _add(self, data: Any, owners: tuple[str, ...], loose: set[Decimal], found: set[str]) -> None:
        """Take in ``data``, which stands in the records of ``owners``."""
        if isinstance(data, dict):
            own = _ids(data)
            self.ids.update(own)
            found.update(own)
            for value in data.values():
                self._add(value, (*owners, *own), loose, found)
        elif isinstance(data, list):
            for value in data:
                self._add(value, owners, loose, found)
        else:
            values = _values(data)
            self.numbers.update(values)
            if isinstance(data, str):
                self.shorthand.update(SHORTHAND.findall(data))
            for owner in owners:
                self.held.setdefault(owner, set()).update(values)
            if not owners:
                loose.update(values)


def _ids(record: dict[str, Any]) -> set[str]:
    """The citable ids a mapping holds itself. Only the ``_CITABLE`` fields hold ids: an id-shaped string
    inside a value is data, not evidence."""
    ids = set()
    for name, prefix in _CITABLE.items():
        value = record.get(name)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                ids.add(prefix + item)
    return ids


def _values(data: Any) -> set[Decimal]:
    """The numbers a scalar holds."""
    if isinstance(data, str):
        values = {value for _, value in stated(data)}
        # Each group of a spaced number is also a value of its own: "10 250" may be two numbers.
        return values | {
            _number(group) for found in NUMBER.finditer(data) if not found["sign"]
            for group in _APART.split(found["digits"])
        }
    if isinstance(data, int | float | Decimal) and not isinstance(data, bool):
        return {Decimal(str(data))}
    return set()


def check(
    answer: str, evidence: Evidence, must: str | None = None, *, shown: bool = False
) -> tuple[tuple[str, ...], list[str]]:
    """The answer's citations (first-use order) and its problems, none when it is valid. ``must``: the
    root cause a fixed question asked about, which the answer has to cite. ``shown``: the problems are for
    a reader (the abstention), not for the model's repair: an unknown citation's text is the model's own,
    so it is named by its kind unless it has a stored id's shape, and a sentence by its place."""
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
    else:
        problems += dict.fromkeys(_unsupported(answer, evidence, shown))
    if must is not None and must not in cited:
        problems.append(f"the answer does not cite [{must}], the root cause asked about")
    return cited, problems


def _unsupported(answer: str, evidence: Evidence, shown: bool) -> Iterator[str]:
    """What each sentence lacks: a citation of a returned id, when it states a number or names an outcome,
    or, of a number some tool result holds, a cited id whose record holds it."""
    for n, (sentence, cited) in enumerate(sentences(answer), 1):
        bare = CITATION.sub(" ", sentence)
        numbers = stated(bare)
        claims = bool(OUTCOME.search(bare) or VERDICT.search(bare))
        known = [c for c in cited if c in evidence.ids]
        where = f"sentence {n}" if shown else f'the sentence "{_excerpt(sentence)}"'
        if not known:
            written = ", ".join(dict.fromkeys(said for said, _ in numbers))
            said = ([f"states {written}"] if numbers else []) + (["names an outcome"] if claims else [])
            if said:
                yield f"{where} {' and '.join(said)} but cites no tool result"
            continue
        held = set().union(*(evidence.held.get(c, set()) for c in known))
        for written, value in numbers:
            if value in evidence.numbers and value not in held:  # else: no tool result holds it at all
                yield f"{written} in {where} appears in no record it cites"
        yield from _decision_claims(bare, evidence, known, where, held)


def _count_field(before: str, after: str) -> str | None:
    """The field a count names: a run outcome, the run total, or a cause's keys or findings."""
    if label := _LABEL_BEFORE.search(before):
        return label["outcome"].upper()
    if label := _COUNT_LABEL.search(before):
        return label["name"].lower().replace(" ", "_")
    if not (unit := _COUNT_OF.match(after)):
        return None
    if unit["outcome"]:
        return unit["outcome"].upper()
    if unit["noun"].lower().startswith("finding"):
        return "finding_count"
    if _RUN_BEFORE.search(before) and _DIFFER.match(after, unit.end()):
        return "different"
    if _TOTAL_BEFORE.search(before) or _TOTAL_AFTER.match(after, unit.end()):
        return "total"
    return "affected_keys"


def _decision_claims(
    text: str, evidence: Evidence, known: list[str], where: str, held: set[Decimal]
) -> Iterator[str]:
    """Check the quantities and outcomes the sentence attributes to the comparison engine."""
    records = [evidence.facts.get(c, {}) for c in known]
    quantified: set[int] = set()
    for start, (end, written, value) in _stated(text, counted_one=True).items():
        field = _count_field(text[:start], text[end:])
        if outcome_count := _COUNT_OF.match(text, end):
            if outcome_count["outcome"]:
                quantified.add(outcome_count.start("outcome"))
        if label := _LABEL_BEFORE.search(text, 0, start):
            quantified.add(label.start("outcome"))
        if field is None or (value not in held and written.lower() != "one"):
            continue  # an ungrounded number already has its own error
        values = {record[field] for record in records if field in record}
        if values != {value}:
            yield f"{written} in {where} needs a citation for {field}; use one subject per claim"
    for claim in _UNIVERSAL.finditer(text):
        quantified.add(claim.start("outcome"))
        if not any(r.get("total") and r.get("total") == r.get(claim["outcome"].upper()) for r in records):
            yield f"{where} gives an outcome for every key that its cited run counters do not establish"
    # An unquantified named outcome is a claim about the key's result.
    assertions = {m.start(): m[0] for m in _ENUM.finditer(text)}
    assertions.update({m.start("outcome"): m["outcome"] for m in _OUTCOME_CLAIM.finditer(text)})
    outcomes = {r["outcome"] for r in records if "outcome" in r}
    if any(outcomes != {value.upper()} for at, value in assertions.items() if at not in quantified):
        yield f"{where} names a key outcome its citations do not establish"
    if _RELEASE_CLAIM.search(text):
        yield f"{where} gives a release verdict that comparison evidence cannot establish"


def sentences(answer: str) -> list[tuple[str, tuple[str, ...]]]:
    """The answer's sentences, each with the citations it holds and those that open the sentence after it."""
    # Where a sentence ends is read with each citation blanked: a masked key may hold a full stop.
    blank = CITATION.sub(lambda found: "#" * len(found[0]), answer)
    cuts = [0]
    for end in _END.finditer(blank):
        if end[0] == "\n" or not _ABBREVIATED.search(blank, 0, end.start()):
            cuts.append(end.end())
    cuts.append(len(answer))
    parts = [answer[start:stop].strip() for start, stop in pairwise(cuts)]
    parts = [part for part in parts if part]
    found = [tuple(CITATION.findall(part)) for part in parts]
    opened = [
        tuple(CITATION.findall(lead[0])) if (lead := _LEADING.match(part)) else () for part in parts
    ]
    return [
        (part, tuple(dict.fromkeys(found[i] + (opened[i + 1] if i + 1 < len(parts) else ()))))
        for i, part in enumerate(parts)
    ]


def _excerpt(sentence: str) -> str:
    said = " ".join(sentence.split())
    return said if len(said) <= _EXCERPT else said[: _EXCERPT - 1] + "…"


def stated(text: str) -> list[tuple[str, Decimal]]:
    """Every number ``text`` states, in its order: as written, and its value."""
    return [(written, value) for _, written, value in _stated(text).values()]


def _stated(text: str, *, counted_one: bool = False) -> dict[int, tuple[int, str, Decimal]]:
    found: dict[int, tuple[int, str, Decimal]] = {}
    words = text  # what the cardinal words are read from: a magnitude behind digits is no word of its own
    for digits in NUMBER.finditer(text):
        sign = digits["sign"].replace("\u2212", "-")
        end, value = digits.end(), _number(sign + _APART.sub("", digits["digits"]))
        if not digits["unit"] and (magnitude := _MAGNITUDE.match(text, end)):
            end, value = magnitude.end(), value * _VALUE[magnitude[1].lower()]
            words = words[: digits.end()] + "#" * (end - digits.end()) + words[end:]
        found[digits.start()] = end, text[digits.start() : end], value
    for run in WORDS.finditer(words):
        if run[0].lower() == "one" and not (counted_one and _ONE_COUNT.match(text, run.end())
                                            and not text[:run.start()].lower().endswith("no ")):
            continue
        for start, end, spelt in _spelt(run):
            written = " ".join(text[start:end].split())
            if written.lower() != "two" or not _PAIR.match(text, end):
                found[start] = end, written, Decimal(spelt)
    return dict(sorted(found.items()))


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
    kind alone (a masked key may be any text, so a ``K:`` citation always). Never in brackets, which the UI
    would draw as a citation the server accepted."""
    if _STORED.fullmatch(cited):
        return cited
    if cited.startswith("K:"):
        return "a K:... citation"
    return f"an {cited.partition('-')[0]}-... citation"
