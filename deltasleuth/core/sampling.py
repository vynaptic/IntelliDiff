"""Sign-off sampling (docs/mvp/SAMPLING.md §4, §6, §8.3): a draw's salt, each frame key's u, strata and
selection, and the exact one-sided 95% Clopper-Pearson decision with the bound it shows; and the exact
two-sided 95% interval a labelled sample's shares are shown with (docs/mvp/ACCURACY.md §6.1).

A key's u is the first 64 bits of SHA-256 of the salt (the spec's label and a nonce the installation
draws) and the key. Each ``bottom_k`` sign-off bucket takes its keys with the smallest (u, key bytes):
given a uniformly random salt and SHA-256 taken as a random function, a simple random sample without
replacement. ``Selector`` reads the frame a batch at a time and holds heaps and contrast candidates,
never the frame. ``passes`` decides in integers; ``upper`` is only shown. Pure: no I/O, no clock.
"""

from __future__ import annotations

import hashlib
import heapq
import math
import secrets
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal
from fractions import Fraction
from functools import cache
from typing import Any, NoReturn

from deltasleuth.core.canonical import predicates
from deltasleuth.core.canonical.errors import ExpressionError
from deltasleuth.core.spec.expressions import reads
from deltasleuth.core.types import Sample, StratumBucket, StratumDimension

TARGETS = {"standard": Fraction(1, 1000), "high": Fraction(1, 10000)}  # copied into every draw
CONFIDENCE = Fraction(19, 20)  # one-sided
MARGIN = Fraction(1, 200)  # drawn beyond the clean keys a pass needs, for keys left unresolved
LIMIT = 1_000_000  # keys a sample holds at most (W7)
_ALPHA = 1 - CONFIDENCE
_TAIL = Fraction(1, 40)  # each end of a two-sided 95% interval
_ALL = 2**64  # u is below it: rate 1.0, a census
_EXACT_CHOOSE = 1000  # up to this x, log C(n, x) is taken from the exact integer
_SHOWN = Context(prec=4, rounding=ROUND_CEILING)
_FLOOR = Context(prec=4, rounding=ROUND_FLOOR)
_INVERT = bytes(range(255, -1, -1))  # reverses the byte order a heap compares keys in
_MEMO = 4096  # bucket lookups a selector memoises at most


class DrawError(ValueError):
    """A draw failed on its frame (docs/mvp/SAMPLING.md §13 W1-W9): a safe text, never naming a key."""


class _Unlabelled(Exception):
    """A key's strata or sign-off stratum failed (counted by kind)."""


# -- salt and u ---------------------------------------------------------------------------------------


def nonce() -> str:
    """A draw's nonce: 128 random bits as 32 lowercase hex, redrawn until it holds a letter (D1), so the
    pack's gate reads it as generated, never as a key (docs/mvp/PACK.md §2.3)."""
    while (value := secrets.token_hex(16)).isdigit():
        pass
    return value


def salt(label: str, nonce: str) -> str:
    """The salt a draw hashes keys with; its SHA-256 goes to the audit trail, the nonce only to sign-off."""
    return f"{label}:{nonce}"


def digest(salt: str, key: str) -> bytes:
    """A key's selection digest (``adjudicate._hash``'s form): label and nonce hold no U+001F."""
    return hashlib.sha256(f"{salt}\x1f{key}".encode()).digest()


def u(digest: bytes) -> int:
    return int.from_bytes(digest[:8], "big")


# -- sizes and the decision ---------------------------------------------------------------------------


def passes(x: int, n: int, target: Fraction) -> bool:
    """Whether x discrepant keys of n cleanly compared ones pass ``target``: whether the one-sided 95%
    Clopper-Pearson upper bound is within it, which is BinomCDF(x; n, t) ≤ 0.05, decided in integers."""
    a, b = target.numerator, target.denominator
    c = b - a
    if n <= 0 or x * b >= n * a:
        return False
    total, choose, power = 0, 1, 1
    for k in range(x + 1):  # Horner's sum of C(n,k)·a^k·c^(x-k)
        total = total * c + choose * power
        choose = choose * (n - k) // (k + 1)
        power *= a
    return _ALPHA.denominator * c ** (n - x) * total <= _ALPHA.numerator * b**n


@cache
def needed(x: int, target: Fraction) -> int:
    """The smallest n at which x discrepant keys still pass, estimated from ``upper`` and
    confirmed by exact ``passes`` calls. ValueError over ``LIMIT``, where a census is the answer."""
    lo, hi = x + 1, LIMIT
    if upper(x, hi) > target:
        raise ValueError(
            f"{x:,} discrepant key(s) pass a {percent(target)} target only among over {LIMIT:,} cleanly"
            " compared keys: compare the population as a census"
        )
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if upper(x, mid) <= target else (mid + 1, hi)
    while not passes(x, lo, target):
        lo += 1
    while passes(x, lo - 1, target):
        lo -= 1
    return lo


def zero_failure_n(target: Fraction) -> int:
    """The smallest n that passes with no discrepant key, 20·(b-a)^n ≤ b^n: 2,995 at 0.1%, 29,956 at 0.01%."""
    return needed(0, target)


def attempted(n: int) -> int:
    """The keys to draw so that n are cleanly compared with a 0.5% margin: ⌈200n/199⌉ (3,011; 30,107)."""
    return math.ceil(n / (1 - MARGIN))


def share(target: Fraction, n: int) -> int:
    """⌊t·n⌋: the keys a target allows among n (a census's discrepant ones, the unresolved gate)."""
    return target.numerator * n // target.denominator


def x_max(n: int, target: Fraction) -> int:
    """The largest x that passes at n, -1 if none (0 at 3,011 and at 30,107): found from ``upper`` and
    confirmed by two exact ``passes`` calls."""
    lo, hi = -1, (n * target.numerator - 1) // target.denominator  # x·b < n·a
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if upper(mid, n) <= target else (lo, mid - 1)
    while lo >= 0 and not passes(lo, n, target):
        lo -= 1
    while passes(lo + 1, n, target):
        lo += 1
    return lo


def upper(x: int, n: int, alpha: Fraction = _ALPHA) -> float:
    """The one-sided Clopper-Pearson upper bound at confidence 1 - ``alpha`` (95%: ``beta.ppf(0.95, x+1,
    n-x)``), for display only: 1 - alpha^(1/n) at x = 0, 1 at x ≥ n, else a bisection on a log-space
    binomial CDF."""
    if x >= n:
        return 1.0
    log_alpha = math.log(alpha)
    if x == 0:
        return -math.expm1(log_alpha / n)
    if x <= _EXACT_CHOOSE:
        choose = math.log(math.comb(n, x))
    else:
        choose = math.lgamma(n + 1) - math.lgamma(x + 1) - math.lgamma(n - x + 1)
    lo, hi = x / n, 1.0  # the CDF is at least 1/2 at p = x/n and falls as p rises
    while lo < (mid := (lo + hi) / 2) < hi:
        lo, hi = (mid, hi) if _log_cdf(x, n, mid, choose) > log_alpha else (lo, mid)
    return hi


def interval(x: int, n: int) -> tuple[str, str]:
    """The exact two-sided 95% Clopper-Pearson interval of x in n (docs/mvp/ACCURACY.md §6.1), as shown:
    each end a one-sided 97.5% bound rounded outward to 4 significant digits, so the shown interval
    contains the exact one; "0" at x = 0 and "1" at x = n."""
    low = "0" if x == 0 else format(_FLOOR.create_decimal_from_float(1 - upper(n - x, n, _TAIL)), "f")
    high = "1" if x == n else format(_SHOWN.create_decimal_from_float(upper(x, n, _TAIL)), "f")
    return low, high


def _log_cdf(x: int, n: int, p: float, choose: float) -> float:
    """log P(X ≤ x) for X ~ Bin(n, p), p ≥ x/n, ``choose`` being log C(n, x): summed from k = x down,
    where the terms fall, until they no longer count."""
    log_p, log_q = math.log(p), math.log1p(-p)
    odds = math.exp(log_q - log_p)
    total = term = 1.0
    for k in range(x, 0, -1):
        term *= k / (n - k + 1) * odds
        total += term
        if term < total * 1e-17:
            break
    return choose + x * log_p + (n - x) * log_q + math.log(total)


def shown(x: int, n: int, target: Fraction, passed: bool) -> str:
    """``upper`` as stored and shown, a decimal string: rounded up to 4 significant digits, clamped to the
    target when the stratum passed and kept above it when it did not, so it never contradicts the verdict."""
    bound = _SHOWN.create_decimal_from_float(upper(x, n))
    limit = Decimal(target.numerator) / Decimal(target.denominator)
    limit = limit.quantize(Decimal(1).scaleb(limit.adjusted() - 3))
    if passed:
        bound = min(bound, limit)
    elif bound <= limit:
        bound = _SHOWN.next_plus(limit)
    return format(bound, "f")


def percent(value: Fraction | Decimal | str) -> str:
    """A share as a percentage, its digits kept: 1/1000 is 0.1%, "0.0009998" is 0.09998%."""
    if isinstance(value, Fraction):
        value = Decimal(value.numerator) / Decimal(value.denominator)
    return format(Decimal(value).scaleb(2), "f") + "%"


def unpassed(judgement: Mapping[str, Any] | None, draw_id: str, at: str) -> list[str]:
    """R-b (docs/mvp/SAMPLING.md §10): why the stored ``judgement`` of draw ``draw_id``'s cycle (``at``, as
    texts name it) is no pass, empty for one that passed: G5-G8 and G11, as the judgement words them, and a
    cycle that ended before every key was final; None is a cycle that ended without a judgement."""
    if judgement is None:  # judging raised when the cycle ended
        return [f"the sample {draw_id} of {at} ended without a judgement: draw again"]
    found = [f"stratum {s['label']} of {at} {why}" for s in judgement["strata"] for why in s["why"]]
    if judgement["status"] not in ("passed", "failed"):  # a failed one's strata said why
        unfinished = [s["label"] for s in judgement["strata"] if s["verdict"] == "unfinished"]
        strata = f"stratum {unfinished[0]}" if len(unfinished) == 1 else f"strata {', '.join(unfinished)}"
        named = f" ({strata} unfinished)" if unfinished else ""
        found.append(
            f"the sample {draw_id} of {at} ended incomplete{named}: its cycle ended before every key was"
            " final; draw again"
        )
    return found


# -- selection ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Drawn:
    """A key of the sample as ``run_keys`` stores it: ``member`` when its own bucket chose it."""

    key: str
    seq: int
    stratum: str
    signoff: str
    member: bool


@dataclass(frozen=True)
class Selection:
    keys: list[Drawn]  # in seq order
    strata: list[dict[str, Any]]  # each sign-off stratum's {label, tier, target, size, basis, …} (§10)
    frame_keys: int


def line(seq: int, key_masked: str, stratum: str | None, signoff: str | None, member: bool) -> bytes:
    """A line ``seq⇥key_masked⇥stratum⇥signoff_stratum⇥member``, as ``selection_sha256`` hashes it."""
    return f"{seq}\t{key_masked}\t{stratum or ''}\t{signoff or ''}\t{str(member).lower()}\n".encode()


def selection_sha256(lines: Iterable[tuple[int, str, str | None, str | None, bool]]) -> str:
    """The SHA-256 of a sample's lines in seq order: R-d re-hashes the stored keys to it."""
    h = hashlib.sha256()
    for fields in lines:
        h.update(line(*fields))
    return h.hexdigest()


class Selector:
    """A draw's selection over its frame's distinct keys (§4), fed a batch at a time by ``add``.

    Each key's strata come from its row, key and ``now``, never from fetched documents: a dimension's
    column or CEL value takes the first bucket it matches, and ``signoffStratum`` (or, without it, the
    first matched bucket with a targetSize, high-risk first; under ``rate`` the whole stratum) names its
    sign-off stratum. Label errors are counted, never naming a key, and fail ``result`` together.
    """

    def __init__(self, sample: Sample, salt: str, now: datetime) -> None:
        self.sample, self.salt, self.now = sample, salt, now
        self.frame_keys = 0
        self._buckets = {f"{d.name}={b.label}": b for d in sample.dimensions for b in d.buckets}
        self._matches: dict[tuple[int, type, Any], StratumBucket | None] = {}  # by id of the dimension
        self._strings: dict[str, str] = {}  # one copy of each label
        signoff = sample.signoff_stratum
        memo = signoff is not None and not any(reads(signoff, "signoff_stratum", v) for v in ("key", "row"))
        self._signoffs: dict[tuple[str, str], Any] | None = {} if memo else None
        self._counts: Counter[str] = Counter()  # frame keys per sign-off stratum (N_h)
        self._tiers: dict[str, str] = {}
        self._own: dict[str, int] = {}  # a rate stratum's own threshold
        self._heaps: dict[str, list[tuple[int, bytes, str, str, int]]] = {
            label: [] for label, b in self._buckets.items() if b.target_size and sample.method == "bottom_k"
        }
        # (u, key, stratum, sign-off stratum, the bound that chose it, member): chosen by a rate
        self._chosen: list[tuple[int, str, str, str, int, bool]] | None = []
        self._chosen_count = 0
        self._failures: dict[tuple[str, ...], list[int]] = {}  # [count, source, first position]
        self._labels_named: dict[str, set[str]] = {"unsized": set(), "standard": set(), "foreign": set()}

    def add(self, keys: Sequence[tuple[str, Mapping[str, Any]]], *, source: int = 0, first: int = 1) -> None:
        """Label and rank a batch of distinct frame keys with their strata rows: source ``source``'s
        keys from position ``first`` on."""
        rate = self.sample.rate or 0
        for position, (key, row) in enumerate(keys, start=first):
            self.frame_keys += 1
            try:
                stratum, labels, matched, tier = self._stratum(key, row, source, position)
                signoff = self._signoff(key, row, stratum, labels, matched, tier, source, position)
            except _Unlabelled:
                continue
            value = u(digest(self.salt, key))
            threshold = max([rate, *(b.rate or 0 for b in matched)])  # the effective rate
            if signoff is None:  # a rate sample without strata: every key it chooses is contrast
                if value < threshold:
                    self._choose((value, key, stratum, signoff, threshold, False))
                continue
            self._counts[signoff] += 1
            self._tiers.setdefault(signoff, tier)
            if self.sample.method == "bottom_k":
                self._rank(key, value, stratum, signoff, threshold)
                if value < threshold:
                    self._choose((value, key, stratum, signoff, threshold, False))
                continue
            own = self._own.setdefault(signoff, self._rate_of(signoff, threshold))
            if value < threshold:
                member = value < own
                self._choose((value, key, stratum, signoff, own if member else threshold, member))

    def result(self) -> Selection:
        """The sample in seq order and each sign-off stratum's record; DrawError for label errors (W3-W6),
        a sign-off bucket without keys (W9), over ``LIMIT`` keys (W7) or nothing to draw (W8)."""
        if not self.frame_keys:
            raise DrawError("the frame is empty")
        problems = self._problems()
        for label, heap in self._heaps.items():
            if not heap:
                problems.append(
                    f"no frame key falls into sign-off bucket {label}: a stratum without keys certifies"
                    " nothing (correct its values, or remove it in a new version)"
                )
        in_heaps = sum(1 for heap in self._heaps.values() for entry in heap if -entry[0] >= entry[4])
        total = self._chosen_count + in_heaps  # a chosen heap key is counted once
        if total > LIMIT:
            problems.append(
                f"the sample would hold {total:,} keys, over {LIMIT:,}: lower the rates, or compare the"
                " population as a census"
            )
        if problems:
            raise DrawError("; ".join(problems))
        if not total:
            raise DrawError("the sample is empty: raise the rates")
        return self._selection()

    # -- labels

    def _stratum(
        self, key: str, row: Mapping[str, Any], source: int, position: int
    ) -> tuple[str | None, list[str], list[StratumBucket], str]:
        labels, matched = [], []
        for dimension in self.sample.dimensions:
            try:
                if dimension.cel is None:
                    value = row.get(dimension.column or "")
                else:
                    value = predicates.label(dimension.cel, {"key": key, "row": row, "now": self.now})
                bucket = self._bucket(dimension, value)
            except (predicates.PredicateError, ExpressionError):
                bucket = None
            if bucket is None:
                self._failed(("dimension", dimension.name), source, position)
            labels.append(self._string(f"{dimension.name}={bucket.label}"))
            matched.append(bucket)
        tier = "high" if any(b.risk_tier == "high" for b in matched) else "standard"
        return self._string("|".join(labels)) if labels else None, labels, matched, tier

    def _bucket(self, dimension: StratumDimension, value: Any) -> StratumBucket | None:
        """The first bucket ``value`` falls into: ``values`` as JSON values, ``min``/``max`` inclusive in
        Decimal, null only into a catch-all."""
        memo = (id(dimension), type(value), value) if _scalar(value) else None
        if memo is not None and memo in self._matches:
            return self._matches[memo]
        number = value if isinstance(value, Decimal | int) and not isinstance(value, bool) else None
        found = None
        for bucket in dimension.buckets:
            if bucket.values is not None:
                hit = value is not None and any(_same(value, v) for v in bucket.values)
            elif bucket.min is not None or bucket.max is not None:
                hit = number is not None and (bucket.min is None or bucket.min <= number)
                hit = hit and (bucket.max is None or number <= bucket.max)
            else:
                hit = True  # a catch-all
            if hit:
                found = bucket
                break
        if memo is not None:
            if len(self._matches) >= _MEMO:  # a frame's distinct values are never held
                self._matches.clear()
            self._matches[memo] = found
        return found

    def _signoff(
        self,
        key: str,
        row: Mapping[str, Any],
        stratum: str | None,
        labels: list[str],
        matched: list[StratumBucket],
        tier: str,
        source: int,
        position: int,
    ) -> str | None:
        sample, expression = self.sample, self.sample.signoff_stratum
        if expression is None:
            if sample.method == "rate":
                return stratum
            sized = [(label, b) for label, b in zip(labels, matched, strict=True) if b.target_size]
            sized.sort(key=lambda pair: pair[1].risk_tier != "high")  # stable: high first
            if not sized:
                self._named("unsized", stratum)
            if tier == "high" and sized[0][1].risk_tier != "high":  # its high-risk bucket has no targetSize
                self._named("standard", sized[0][0])
            return sized[0][0]
        memo = (stratum, tier)
        if self._signoffs is not None and memo in self._signoffs:
            value = self._signoffs[memo]
        else:
            strata = dict(label.split("=", 1) for label in labels)
            bindings = {"key": key, "row": row, "strata": strata, "riskTier": tier, "now": self.now}
            try:
                value = predicates.label(expression, bindings)
            except (predicates.PredicateError, ExpressionError):
                value = None
            if self._signoffs is not None:
                self._signoffs[memo] = value
        if not isinstance(value, str):
            self._failed(("signoff",), source, position)
        if value not in labels:
            self._named("foreign", value if value in self._buckets else None)
        bucket = self._buckets[value]
        if tier == "high" and bucket.risk_tier != "high":
            self._named("standard", value)
        if sample.method == "bottom_k" and not bucket.target_size:
            self._named("unsized", value)
        return self._string(value)

    def _rate_of(self, signoff: str, threshold: int) -> int:
        """A rate stratum's own threshold: its bucket's rate, else the base rate; a whole stratum's, the
        effective rate its keys share."""
        if self.sample.signoff_stratum is None:
            return threshold
        return self._buckets[signoff].rate or self.sample.rate or 0

    def _string(self, text: str) -> str:
        return self._strings.setdefault(text, text)

    def _failed(self, kind: tuple[str, ...], source: int, position: int) -> NoReturn:
        found = self._failures.setdefault(kind, [0, source, position])
        found[0] += 1
        raise _Unlabelled

    def _named(self, kind: str, label: str | None) -> NoReturn:
        if label is not None:
            self._labels_named[kind].add(label)
        self._failed((kind,), 0, 0)

    def _problems(self) -> list[str]:
        def count(kind: str) -> int:
            return self._failures.get((kind,), [0])[0]

        def named(kind: str) -> str:
            labels = sorted(self._labels_named[kind])
            return f" ({', '.join(labels)})" if labels else ""

        problems = []
        if n := count("unsized"):
            problems.append(
                f"the frame gave {n:,} key(s) a sign-off label naming no bucket with targetSize"
                f"{named('unsized')}: under bottom_k every key needs a sign-off stratum"
            )
        if n := count("standard"):
            problems.append(
                f"the frame gave {n:,} high-risk key(s) a standard sign-off label{named('standard')}: test"
                " the high-risk conditions first in signoffStratum"
            )
        if n := count("foreign"):
            problems.append(
                f"the frame gave {n:,} key(s) a sign-off label naming none of their buckets{named('foreign')}"
            )
        failed = [(f"stratum dimension {d.name}", ("dimension", d.name)) for d in self.sample.dimensions]
        for what, kind in [*failed, ("signoffStratum", ("signoff",))]:
            if kind in self._failures:
                n, source, position = self._failures[kind]
                problems.append(
                    f"{what} failed for {n:,} key(s) of the frame (source {source}, first at position"
                    f" {position:,})"
                )
        return problems

    # -- ranks

    def _rank(self, key: str, value: int, stratum: str, signoff: str, threshold: int) -> None:
        """Keep the key if it is among its sign-off bucket's targetSize smallest (u, key bytes): a max-heap
        of (-u, the key bytes inverted), keys holding no NUL."""
        heap, size = self._heaps[signoff], self._buckets[signoff].target_size or 0
        if len(heap) < size or value <= -heap[0][0]:
            entry = (-value, key.encode().translate(_INVERT) + b"\xff", key, stratum, threshold)
            if len(heap) < size:
                heapq.heappush(heap, entry)
            elif entry > heap[0]:
                heapq.heapreplace(heap, entry)

    def _choose(self, entry: tuple[int, str, str, str, int, bool]) -> None:
        """A key a rate chose: kept until they are over ``LIMIT``, then only counted (W7)."""
        self._chosen_count += 1
        if self._chosen is not None:
            self._chosen.append(entry)
            if len(self._chosen) > LIMIT:
                self._chosen = None

    def _selection(self) -> Selection:
        """The sample ordered by (⌊u·2^64/m⌋, key), m the bound that chose each key, so every stratum
        advances at the same pace; and the strata records."""
        assert self._chosen is not None  # at most LIMIT
        rows, members, contrast, u_max = [], Counter[str](), Counter[str](), {}
        for label, heap in self._heaps.items():
            census = self._counts[label] <= (self._buckets[label].target_size or 0)
            u_max[label] = top = max(-entry[0] for entry in heap)
            bound = _ALL if census else top + 1
            rows += [(-e[0] * _ALL // bound, e[2], e[3], label, True) for e in heap]
            members[label] = len(heap)
        ranked = {entry[2] for heap in self._heaps.values() for entry in heap}
        for value, key, stratum, signoff, bound, member in self._chosen:
            if key in ranked:
                continue
            rows.append((value * _ALL // bound, key, stratum, signoff, member))
            (members if member else contrast)[signoff] += 1
            if member:
                u_max[signoff] = max(value, u_max.get(signoff, 0))
        rows.sort(key=lambda r: (r[0], r[1].encode()))
        keys = [
            Drawn(key, seq, stratum, signoff, member)
            for seq, (_, key, stratum, signoff, member) in enumerate(rows)
        ]
        labels = list(self._heaps) if self._heaps else sorted(self._counts)
        return Selection(
            keys, [self._record(label, members, contrast, u_max) for label in labels], self.frame_keys
        )

    def _record(
        self, label: str, members: Counter[str], contrast: Counter[str], u_max: dict[str, int]
    ) -> dict[str, Any]:
        # A bottom_k stratum's tier is its sign-off bucket's; a rate stratum's keys share their buckets' tier.
        tier = self._buckets[label].risk_tier if self._heaps else self._tiers[label]
        frame_keys, drawn = self._counts[label], members[label]
        target = TARGETS[tier]
        size = self._buckets[label].target_size if self._heaps else None
        census = frame_keys <= size if size is not None else self._own[label] == _ALL
        return {
            "label": label,
            "tier": tier,
            "target": str(Decimal(target.numerator) / Decimal(target.denominator)),
            "size": size,
            "basis": "census" if census else "sample",
            "frame_keys": frame_keys,
            "drawn": drawn,
            "contrast": contrast[label],
            "u_max": None if label not in u_max else str(u_max[label]),
            "x_max": share(target, frame_keys) if census else x_max(drawn, target),
        }


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | bool | int | Decimal)


def _same(value: Any, wanted: str | int | bool) -> bool:
    """JSON equality: a boolean is not a number, and numbers compare by value."""
    if isinstance(wanted, bool) or isinstance(value, bool):
        return isinstance(wanted, bool) and isinstance(value, bool) and value is wanted
    if isinstance(wanted, int):
        return isinstance(value, Decimal | int) and value == wanted
    return value == wanted
