"""Sign-off sampling's statistics and selection (docs/mvp/SAMPLING.md §4, §6, §8): the exact one-sided 95%
Clopper-Pearson decision and the bound it shows, pinned to §8.3's table and checked against scipy and an
independent Fraction sum; the sizes; u; bottom-k, membership, contrast, census and the pace of seq; and
the draw's label errors, never naming a key. And the exact two-sided 95% interval of a labelled sample
(docs/mvp/ACCURACY.md §6): pinned to §6.3, checked against scipy and exact integer sums, shown rounded
outward, and its coverage of a sample drawn without replacement."""

from __future__ import annotations

import hashlib
import math
import random
import tracemalloc
from datetime import UTC, datetime
from decimal import Context, Decimal
from fractions import Fraction
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.stats import beta, binom, chi2, hypergeom

from deltasleuth.core import sampling
from deltasleuth.core.canonical import predicates
from deltasleuth.core.sampling import (
    DrawError,
    Selection,
    Selector,
    attempted,
    interval,
    needed,
    passes,
    percent,
    share,
    shown,
    upper,
    x_max,
    zero_failure_n,
)
from deltasleuth.core.types import Sample, StratumBucket, StratumDimension

STANDARD, HIGH = sampling.TARGETS["standard"], sampling.TARGETS["high"]
NOW = datetime(2026, 10, 1, 9, 12, tzinfo=UTC)
SALT = sampling.salt("cutover-q4", "3f9a0c1e5b7d2468ace0135792468bdf")

# §8.3: x, n, beta.ppf(0.95, x+1, n-x), shown, passes 0.1%, passes 0.01%
TABLE = [
    (0, 2995, 0.000999744421, "0.09998%", True, False),
    (0, 2994, 0.001000078170, "0.1001%", False, False),
    (0, 2980, 0.001004774162, "0.1005%", False, False),
    (2, 2995, 0.002100594339, "0.2101%", False, False),
    (1, 4742, 0.000999998368, "0.1000%", True, False),
    (1, 4741, 0.001000209210, "0.1001%", False, False),
    (5, 10511, 0.000999931315, "0.1000%", True, False),
    (5, 10510, 0.001000026431, "0.1001%", False, False),
    (0, 29956, 0.000099999415, "0.01000%", True, True),
    (0, 29955, 0.000100002753, "0.01001%", True, False),
    (1, 47437, 0.000099999522, "0.01000%", True, True),
    (1, 47436, 0.000100001630, "0.01001%", True, False),
    (3, 77535, 0.000099998953, "0.01000%", True, True),
    (2, 2997, 0.002099193546, "0.2100%", False, False),  # the worst case of 0 + 2 unresolved
    (15, 2995, 0.007701469069, "0.7702%", False, False),
]


@pytest.mark.parametrize(("x", "n", "ppf", "display", "standard", "high"), TABLE)
def test_the_exact_decision_and_the_shown_bound(
    x: int, n: int, ppf: float, display: str, standard: bool, high: bool
) -> None:
    assert (passes(x, n, STANDARD), passes(x, n, HIGH)) == (standard, high)
    assert abs(upper(x, n) - ppf) <= 1e-12
    assert abs(upper(x, n) - beta.ppf(0.95, x + 1, n - x)) <= 3e-15
    assert percent(shown(x, n, STANDARD, standard)) == percent(shown(x, n, HIGH, high)) == display


def test_upper_matches_scipy_over_a_grid() -> None:
    grid = sorted({round(10 ** (k / 8)) for k in range(41)})  # n from 1 to 100,000
    worst = max(
        abs(upper(x, n) - beta.ppf(0.95, x + 1, n - x)) / beta.ppf(0.95, x + 1, n - x)
        for n in grid
        for x in range(min(21, n))
    )
    assert worst <= 1e-11
    assert (upper(0, 0), upper(3, 3), upper(4, 3)) == (1.0, 1.0, 1.0)


def _cdf(x: int, n: int, t: Fraction) -> Fraction:
    return sum((math.comb(n, k) * t**k * (1 - t) ** (n - k) for k in range(x + 1)), Fraction(0))


@pytest.mark.parametrize(
    ("x", "n", "target"),
    [(x, n + d, STANDARD) for x, n in enumerate([2995, 4742, 6294, 7752, 9151, 10511]) for d in (-1, 0)]
    + [(0, 29955, HIGH), (0, 29956, HIGH), (0, 1, STANDARD), (3, 3, STANDARD), (4, 3, HIGH)],
)
def test_passes_is_the_exact_binomial_cdf_test(x: int, n: int, target: Fraction) -> None:
    assert passes(x, n, target) is (x < n * target and _cdf(x, n, target) <= Fraction(1, 20))


@settings(max_examples=150, deadline=None)
@given(
    x=st.integers(0, 40),
    n=st.integers(0, 20_000),
    target=st.sampled_from([STANDARD, HIGH, Fraction(1, 100)]),
)
def test_shown_never_contradicts(x: int, n: int, target: Fraction) -> None:
    verdict = passes(x, n, target)
    bound = Decimal(shown(x, n, target, verdict))
    assert (bound <= Decimal(target.numerator) / target.denominator) is verdict
    assert len(bound.as_tuple().digits) <= 4  # rounded up to 4 significant digits


def test_sizes() -> None:
    assert [zero_failure_n(t) for t in (Fraction(1, 100), STANDARD, HIGH)] == [299, 2995, 29956]
    assert (attempted(2995), attempted(29956), attempted(47437)) == (3011, 30107, 47676)
    assert [needed(x, STANDARD) for x in range(6)] == [2995, 4742, 6294, 7752, 9151, 10511]
    assert [needed(x, HIGH) for x in range(4)] == [29956, 47437, 62956, 77535]
    assert 12 * attempted(needed(0, STANDARD)) + 3 * attempted(needed(0, HIGH)) == 126_453
    assert (x_max(3011, STANDARD), x_max(30107, HIGH)) == (0, 0)  # the drawn sizes allow no discrepant key
    with pytest.raises(ValueError, match="only among over 1,000,000 cleanly compared keys"):
        needed(1000, HIGH)


@pytest.mark.parametrize("target", [STANDARD, HIGH])
@pytest.mark.parametrize("n", [0, 1, 100, 2994, 2995, 3011, 4741, 4742, 6294, 9000, 20_000, 30_107, 47_437])
def test_x_max_is_the_largest_passing_x(n: int, target: Fraction) -> None:
    linear = -1
    while passes(linear + 1, n, target):
        linear += 1
    assert x_max(n, target) == linear


def test_a_census_takes_its_exact_share_never_the_binomial_bound() -> None:
    assert (share(HIGH, 5000), share(HIGH, 20_000), share(STANDARD, 2994)) == (0, 2, 2)
    assert percent(shown(0, 5000, HIGH, False)) == "0.05990%"  # the binomial bound would fail 0.01%


@pytest.mark.parametrize(("target", "sizes"), [(STANDARD, range(2995, 3101)), (HIGH, range(29956, 30001))])
def test_false_pass_at_most_5pct(target: Fraction, sizes: range) -> None:
    """At a true rate equal to the target, a stratum passes with at most 5% probability, and one more
    allowed discrepant key would exceed it."""
    for n in sizes:
        allowed = x_max(n, target)
        assert binom.cdf(allowed, n, float(target)) <= 0.05 < binom.cdf(allowed + 1, n, float(target))


@pytest.mark.parametrize(("target", "n1", "n2"), [(STANDARD, 2995, 4742), (HIGH, 29956, 47437)])
def test_dropped_two_stage_is_0_076(target: Fraction, n1: int, n2: int) -> None:
    """Passing at n1 on x = 0, else extending to n2 and passing on x ≤ 1 (what x_max allows
    there), passes 7.6% of samples at the target rate: why no sample is extended (D2)."""
    p = float(target)
    assert (x_max(n1, target), x_max(n2, target)) == (0, 1)
    twice = (1 - p) ** n1 + n1 * p * (1 - p) ** (n1 - 1) * (1 - p) ** (n2 - n1)
    assert round(twice, 3) == 0.076


@pytest.mark.parametrize(
    ("population", "rate", "required"),
    [(1000, 0.01, 258), (5000, 0.001, 2253), (20_000, 0.0001, 15528), (50_000, 0.0001, 22536),
     (200_000, 0.0001, 27821)],
)  # fmt: skip
def test_hypergeometric_is_at_most_binomial(population: int, rate: float, required: int) -> None:
    """Without replacement fewer keys rule a rate out, so the binomial bound is conservative."""
    bad = math.ceil(rate * population - 1e-9)
    assert (
        hypergeom.pmf(0, population, bad, required) <= 0.05 < hypergeom.pmf(0, population, bad, required - 1)
    )
    n = zero_failure_n(Fraction(rate).limit_denominator(10_000))
    assert hypergeom.pmf(0, population, bad, min(n, population)) <= (1 - rate) ** n
    assert round(hypergeom.pmf(0, 5000, 5, 2995), 4) == 0.0103  # §8.4


# -- the two-sided interval (docs/mvp/ACCURACY.md §6) ------------------------------------------------------

TAIL = Fraction(1, 40)  # each end of a two-sided 95% interval
# §6.3: x, n, the exact interval, shown
PINNED = [
    (84, 120, 0.609603890578, 0.780243500759, "60.96% to 78.03%"),
    (12, 120, 0.052747786390, 0.168165212703, "5.274% to 16.82%"),
    (3, 120, 0.005185574958, 0.071323194157, "0.5185% to 7.133%"),
    (18, 120, 0.091389572528, 0.226667143413, "9.138% to 22.67%"),
    (15, 117, 0.073565244544, 0.202625999302, "7.356% to 20.27%"),
    (87, 117, 0.654616843019, 0.819837777393, "65.46% to 81.99%"),
    (102, 117, 0.797374000698, 0.926434755456, "79.73% to 92.65%"),
    (30, 117, 0.180162222607, 0.345383156981, "18.01% to 34.54%"),
    (84, 96, 0.791828111563, 0.933710904257, "79.18% to 93.38%"),
    (3, 22, 0.029055851129, 0.349122097257, "2.905% to 34.92%"),
    (0, 10, 0.0, 0.308497107819, "0% to 30.85%"),
    (5, 5, 0.478176249895, 1.0, "47.81% to 100%"),
    (1, 20, 0.001265089498, 0.248732762772, "0.1265% to 24.88%"),
]
SIZING = {50: ("35.52% to 64.48%", "7.113%"), 100: ("39.83% to 60.17%", "3.622%"),
          200: ("42.86% to 57.14%", "1.828%"), 400: ("44.99% to 55.01%", "0.9180%")}  # fmt: skip


def ends(x: int, n: int) -> tuple[float, float]:
    """The exact interval's ends, as ``interval`` computes them before it rounds."""
    return (1 - upper(n - x, n, TAIL) if x else 0.0), (upper(x, n, TAIL) if x < n else 1.0)


def _said(x: int, n: int) -> str:
    low, high = interval(x, n)
    return f"{percent(low)} to {percent(high)}"


@pytest.mark.parametrize(("x", "n", "low", "high", "display"), PINNED)
def test_interval_pinned(x: int, n: int, low: float, high: float, display: str) -> None:
    found = ends(x, n)
    assert abs(found[0] - low) <= 1e-12 and abs(found[1] - high) <= 1e-12
    assert _said(x, n) == display


def test_interval_sizing() -> None:
    """RUNBOOK's sizing: the interval at p = 1/2, and the upper end at x = 0 (a 97.5% one-sided bound)."""
    assert {n: (_said(n // 2, n), percent(interval(0, n)[1])) for n in SIZING} == SIZING


def test_interval_matches_scipy_over_a_grid() -> None:
    sizes = [*range(1, 21), 25, 30, 40, 50, 64, 80, 100, 120, 150, 200, 250, 300, 400, 500]
    for n in sizes:
        for x in range(min(60, n) + 1):
            low, high = ends(x, n)
            want_low = beta.ppf(0.025, x, n - x + 1) if x else 0.0
            want_high = beta.ppf(0.975, x + 1, n - x) if x < n else 1.0
            assert abs(low - want_low) <= 1e-12 and abs(high - want_high) <= 1e-12, (x, n)


def _at_most(x: int, n: int, p: Fraction) -> bool:
    """Whether P(X ≤ x) ≤ 1/40 for X ~ Bin(n, p), in integers."""
    a, b = p.numerator, p.denominator
    return 40 * sum(math.comb(n, k) * a**k * (b - a) ** (n - k) for k in range(x + 1)) <= b**n


@pytest.mark.parametrize("n", [1, 2, 7, 20, 96, 117, 120, 500])
def test_interval_ends_are_the_exact_binomial_tails_on_both_sides(n: int) -> None:
    """An independent oracle, and the symmetry the lower end is computed by: the upper end is where
    P(X ≤ x) falls to 2.5%, the lower end where P(X ≥ x) does (P(n - X ≤ n - x) at 1 - p)."""
    nudge = Fraction(1, 10**9)
    for x in sorted({0, 1, n // 3, n // 2, n - 1, n}):
        low, high = (Fraction(end) for end in ends(x, n))
        if x < n:
            assert _at_most(x, n, high * (1 + nudge)) and not _at_most(x, n, high * (1 - nudge))
        if x:
            assert _at_most(n - x, n, 1 - low * (1 - nudge)) and not _at_most(n - x, n, 1 - low * (1 + nudge))


@settings(max_examples=200, deadline=None)
@given(n=st.integers(1, 400), data=st.data())
def test_shown_contains_exact(n: int, data: st.DataObject) -> None:
    """The shown interval contains the exact one and is within one unit in its fourth digit of it."""
    x = data.draw(st.integers(0, n))
    low, high = (Decimal(end) for end in interval(x, n))
    four = Context(prec=4)
    assert all(len(end.as_tuple().digits) <= 4 for end in (low, high))
    if x == 0:
        assert low == 0
    else:  # at or below the exact lower end, the next 4-digit value above it
        assert _at_most(n - x, n, 1 - Fraction(low)) and not _at_most(
            n - x, n, 1 - Fraction(four.next_plus(low))
        )
    if x == n:
        assert high == 1
    else:
        assert _at_most(x, n, Fraction(high)) and not _at_most(x, n, Fraction(four.next_minus(high)))


def test_upper_default_unchanged() -> None:
    """``upper`` gained ``alpha``; its default is the one-sided 95% bound §8.3 pins."""
    for x, n, ppf, _, _, _ in TABLE:
        assert upper(x, n) == upper(x, n, Fraction(1, 20)) and abs(upper(x, n) - ppf) <= 1e-12
    assert upper(0, 100, TAIL) == -math.expm1(math.log(1 / 40) / 100) > upper(0, 100)
    assert abs(upper(3, 50, TAIL) - beta.ppf(0.975, 4, 47)) <= 1e-12


def _coverage(population: int, n: int) -> tuple[float, int]:
    """The lowest chance, over every count D of the population's items with a property, that the interval
    of a sample of n drawn without replacement contains D/N; and that D."""
    x, marked = np.arange(n + 1), np.arange(population + 1)
    found = [ends(k, n) for k in range(n + 1)]
    low, high = (np.array([end[side] for end in found]) for side in (0, 1))
    share = marked[:, None] / population
    inside = (low[None, :] <= share) & (share <= high[None, :])
    covered = (hypergeom.pmf(x[None, :], population, marked[:, None], n) * inside).sum(axis=1)
    return float(covered.min()), int(covered.argmin())


def test_conservative_without_replacement() -> None:
    """§6.3: exhaustively for N ≤ 40, the binomial interval covers a share of a population sampled without
    replacement at least 97.134% of the time (N 39, n 7, D 16)."""
    lowest = min((*_coverage(size, n), size, n) for size in range(1, 41) for n in range(1, size + 1))
    assert (round(lowest[0], 5), lowest[2], lowest[3], lowest[1]) == (0.97134, 39, 7, 16)


@pytest.mark.parametrize(("population", "n"), [(1000, 50), (1000, 120), (5000, 50), (5000, 120)])
def test_without_replacement_spot_checks(population: int, n: int) -> None:
    lowest, _ = _coverage(population, n)
    assert lowest >= 0.95
    assert (population, n) != (5000, 50) or round(lowest, 4) == 0.9519  # the lowest of §6.3's grid


# -- u --------------------------------------------------------------------------------------------------


def test_golden_u_and_the_salt() -> None:
    assert SALT == "cutover-q4:3f9a0c1e5b7d2468ace0135792468bdf"
    assert hashlib.sha256(SALT.encode()).hexdigest() == (
        "931f7ee6955f6fe428d668fdc1bbb2fb76b1e223c7714df4c0ec8a2b7b8637fb"
    )
    golden = {"C00012345": 9521941347542013777, "1": 17691553104207503435, "ünïcode": 14602823002930777203}
    assert {key: sampling.u(sampling.digest(SALT, key)) for key in golden} == golden
    digest = sampling.digest(SALT, "T0001")
    assert digest == hashlib.sha256(f"{SALT}\x1fT0001".encode()).digest()
    assert sampling.u(digest) == int.from_bytes(digest[:8], "big") == 6083181073131691134


def test_a_nonce_holds_128_random_bits_and_a_letter(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn = iter(["0123456789" * 3 + "01", "0123456789abcdef" * 2])
    monkeypatch.setattr(sampling.secrets, "token_hex", lambda n: next(drawn))
    assert sampling.nonce() == "0123456789abcdef" * 2  # all digits, redrawn
    monkeypatch.undo()
    assert all(len(v) == 32 and set(v) <= set("0123456789abcdef") and not v.isdigit() for v in
               (sampling.nonce() for _ in range(100)))  # fmt: skip


# -- selection ------------------------------------------------------------------------------------------


def bucket(label: str, **fields: Any) -> StratumBucket:
    for name in ("min", "max"):
        if name in fields:
            fields[name] = Decimal(fields[name])
    if "rate" in fields:
        fields["rate"] = threshold(fields["rate"])
    return StratumBucket(label, **fields)


def dimension(name: str, *buckets: StratumBucket, cel: str | None = None) -> StratumDimension:
    return StratumDimension(name, buckets, None if cel else name, cel)


def threshold(rate: str) -> int:
    return int(Decimal(rate) * 2**64)


def sample(*dimensions: StratumDimension, method: str = "bottom_k", rate: str | None = None,
           signoff: str | None = None) -> Sample:  # fmt: skip
    base = None if rate is None else threshold(rate)
    return Sample(method, "cutover-q4", (), ("table:legacy-db/customers",), dimensions, base, signoff)  # type: ignore[arg-type]


def draw(chosen: Sample, frame: list[tuple[str, dict]], batch: int = 1000, salt: str = SALT) -> Selection:
    selector = Selector(chosen, salt, NOW)
    for start in range(0, len(frame), batch):
        selector.add(frame[start : start + batch], first=start + 1)
    return selector.result()


def rank(key: str, salt: str = SALT) -> tuple[int, bytes]:
    return sampling.u(sampling.digest(salt, key)), key.encode()


def frame(n: int, **columns: Any) -> list[tuple[str, dict]]:
    """``n`` keys K000000…, each column a function of the key's index."""
    return [(f"K{i:06d}", {name: f(i) for name, f in columns.items()}) for i in range(n)]


ALL = sample(dimension("all", bucket("any", target_size=50)))


def test_bottom_k_is_the_k_smallest_whatever_member_order_or_batch_size() -> None:
    keys = frame(2000)
    expected = sorted(keys, key=lambda item: rank(item[0]))[:50]
    first = draw(ALL, keys)
    assert [d.key for d in first.keys] == [key for key, _ in expected]  # a census-free heap: seq is u order
    shuffled = keys[:]
    random.Random(7).shuffle(shuffled)  # noqa: S311 - an order, not a secret
    for order, batch in [(keys, 1), (shuffled, 7), (shuffled[::-1], 2000)]:
        assert draw(ALL, order, batch).keys == first.keys
    assert first.strata == [
        {"label": "all=any", "tier": "standard", "target": "0.001", "size": 50, "basis": "sample",
         "frame_keys": 2000, "drawn": 50, "contrast": 0, "u_max": str(rank(expected[-1][0])[0]),
         "x_max": -1},
    ]  # fmt: skip


def test_membership_contrast_and_census() -> None:
    """A key is a sign-off member only when its own bucket chose it; a rate-only bucket's keys are
    contrast, and a bucket holding fewer keys than its size is a census."""
    chosen = sample(
        dimension(
            "kind", bucket("A", values=("A",), target_size=10), bucket("B", values=("B",), target_size=1000)
        ),
        dimension("flag", bucket("Y", values=("Y",), rate="0.5"), bucket("N")),
    )
    keys = frame(330, kind=lambda i: "A" if i < 300 else "B", flag=lambda i: "Y" if i % 3 == 0 else "N")
    result = draw(chosen, keys, batch=64)
    rows = dict(keys)
    heap = {key for key, _ in sorted(keys[:300], key=lambda item: rank(item[0]))[:10]}
    half = 2**63
    contrast = {k for k, row in keys[:300] if row["flag"] == "Y" and rank(k)[0] < half} - heap
    census = {key for key, _ in keys[300:]}
    assert {d.key for d in result.keys if d.member} == heap | census
    assert {d.key for d in result.keys if not d.member} == contrast
    assert all(d.signoff == f"kind={rows[d.key]['kind']}" for d in result.keys)
    assert all(d.stratum == f"kind={rows[d.key]['kind']}|flag={rows[d.key]['flag']}" for d in result.keys)
    fields = ("basis", "frame_keys", "drawn", "contrast", "x_max")
    assert [tuple(s[f] for f in fields) for s in result.strata] == [
        ("sample", 300, 10, len(contrast), -1), ("census", 30, 30, 0, 0)
    ]  # fmt: skip
    b = result.strata[1]
    assert b["u_max"] == str(max(rank(k)[0] for k in census)) and result.frame_keys == 330


def test_rate_membership_follows_the_own_bucket() -> None:
    chosen = sample(
        dimension("kind", bucket("A", values=("A",), rate="0.1"), bucket("B", values=("B",), rate="1.0")),
        dimension("flag", bucket("Y", values=("Y",), rate="0.9"), bucket("N")),
        method="rate", rate="0.05", signoff="'kind=' + strata.kind",
    )  # fmt: skip
    keys = frame(3000, kind=lambda i: "A" if i % 10 else "B", flag=lambda i: "Y" if i % 2 else "N")
    result = draw(chosen, keys)
    drawn = {d.key: d for d in result.keys}
    for key, row in keys:
        u = rank(key)[0]
        own = threshold("1" if row["kind"] == "B" else "0.1")
        effective = max(threshold("0.05"), own, threshold("0.9") if row["flag"] == "Y" else 0)
        assert (key in drawn) is (u < effective)
        assert key not in drawn or drawn[key].member is (u < own)
    a, b = result.strata
    assert (a["label"], a["basis"], a["size"], b["label"], b["basis"]) == (
        "kind=A",
        "sample",
        None,
        "kind=B",
        "census",
    )
    assert b["drawn"] == b["frame_keys"] == 300 and b["x_max"] == 0


def test_seq_advances_every_stratum_at_the_same_pace() -> None:
    """A stratum sampling half its keys and one sampling a ten-thousandth advance together along seq, so a
    stopped cycle has compared a uniform sub-sample of each; raw u would compare the sparse one first."""
    chosen = sample(
        dimension("s", bucket("dense", values=("dense",), target_size=200), bucket("sparse", target_size=20))
    )
    keys = frame(200_400, s=lambda i: "dense" if i < 400 else "sparse")
    result = draw(chosen, keys, batch=10_000)
    assert [s["frame_keys"] for s in result.strata] == [400, 200_000] and len(result.keys) == 220
    first_half = result.keys[:110]
    assert 4 <= sum(d.signoff == "s=sparse" for d in first_half) <= 16
    by_u = sorted(result.keys, key=lambda d: rank(d.key))
    assert all(d.signoff == "s=sparse" for d in by_u[:20])  # every sparse key has a smaller u


def test_selection_is_uniform_over_salts() -> None:
    keys = frame(100)
    ten = sample(dimension("all", bucket("any", target_size=10)))
    chosen = [0] * 100
    for n in range(1000):
        for d in draw(ten, keys, salt=sampling.salt("uniform", f"{n:032x}")).keys:
            chosen[int(d.key[1:])] += 1
    statistic = sum((c - 100) ** 2 / 100 for c in chosen)
    assert statistic < chi2.ppf(0.999, 99)


def test_strata_come_from_key_row_and_now() -> None:
    """A recency bucket counts from the frame's read (now), and a dimension may read the key alone."""
    recent = "row.changed_at != null && timestamp(row.changed_at) >= now - duration('168h')"
    chosen = sample(
        dimension("recent", bucket("Y", values=(True,), target_size=100, risk_tier="high"), bucket("N"),
                  cel=recent),
        dimension("club", bucket("zero", values=("zero",), target_size=100), bucket("other", target_size=100),
                  cel="key.endsWith('0') ? 'zero' : 'other'"),
        signoff="riskTier == 'high' ? 'recent=Y' : 'club=' + strata.club",
    )  # fmt: skip
    changed = {0: "2026-09-30T00:00:00Z", 1: "2026-09-01T00:00:00Z", 2: None}  # NOW is 2026-10-01
    result = draw(chosen, frame(30, changed_at=lambda i: changed[i % 3]))
    labels = {d.key: (d.stratum, d.signoff) for d in result.keys}
    assert labels["K000000"] == ("recent=Y|club=zero", "recent=Y")
    assert labels["K000010"] == ("recent=N|club=zero", "club=zero")
    assert (labels["K000001"], labels["K000002"]) == (("recent=N|club=other", "club=other"),) * 2
    assert [(s["label"], s["tier"], s["frame_keys"]) for s in result.strata] == [
        ("recent=Y", "high", 10), ("club=zero", "standard", 2), ("club=other", "standard", 18)
    ]  # fmt: skip


def test_a_signoff_stratum_reading_neither_key_nor_row_is_memoised(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    label = predicates.label
    monkeypatch.setattr(predicates, "label", lambda *args: calls.append(args) or label(*args))
    kinds = sample(dimension("kind", bucket("A", values=("A",), target_size=5), bucket("B", target_size=5)),
                   signoff="'kind=' + strata.kind")  # fmt: skip
    keys = frame(1000, kind=lambda i: "A" if i % 2 else "B")
    draw(kinds, keys)
    assert len(calls) == 2
    calls.clear()
    draw(sample(*kinds.dimensions, signoff="key == '' ? 'kind=A' : 'kind=' + strata.kind"), keys)
    assert len(calls) == 1000


def test_label_errors_fail_the_draw_together_never_naming_a_key() -> None:
    kind = dimension("kind", bucket("PERSON", values=("P",), target_size=5), bucket("ORG", values=("O",)),
                     bucket("HIGH", values=("H",), target_size=5, risk_tier="high"))  # fmt: skip
    size = dimension("size", bucket("small", max=9), bucket("big", min=10, max=99))  # no catch-all
    signoff = "row.pick == 'x' ? 1 : row.pick"
    keys = [
        ("C1", {"kind": "P", "size": Decimal(1), "pick": "kind=PERSON"}),
        ("C2", {"kind": "O", "size": Decimal(1), "pick": "kind=ORG"}),  # W3: no targetSize
        ("C3", {"kind": "H", "size": Decimal(1), "pick": "size=small"}),  # W4: high-risk, a standard label
        ("C4", {"kind": "P", "size": Decimal(1), "pick": "kind=HIGH"}),  # W5: not its bucket
        ("C5", {"kind": "P", "size": Decimal(500), "pick": "kind=PERSON"}),  # W6: no size bucket
        ("C6", {"kind": "P", "size": None, "pick": "kind=PERSON"}),  # W6: null only to a catch-all
        ("C7", {"kind": "P", "size": Decimal(1), "pick": "x"}),  # W6: not a string
        ("C8", {"kind": "P", "size": Decimal(1), "pick": "anything-C8"}),  # W5, never shown
    ]
    selector = Selector(sample(kind, size, signoff=signoff), SALT, NOW)
    selector.add(keys[:4], source=0)
    selector.add(keys[4:], source=1, first=1)
    with pytest.raises(DrawError) as failed:
        selector.result()
    assert str(failed.value) == "; ".join([
        "the frame gave 1 key(s) a sign-off label naming no bucket with targetSize (kind=ORG): under bottom_k"
        " every key needs a sign-off stratum",
        "the frame gave 1 high-risk key(s) a standard sign-off label (size=small): test the high-risk"
        " conditions first in signoffStratum",
        "the frame gave 2 key(s) a sign-off label naming none of their buckets (kind=HIGH)",
        "stratum dimension size failed for 2 key(s) of the frame (source 1, first at position 1)",
        "signoffStratum failed for 1 key(s) of the frame (source 1, first at position 3)",
        "no frame key falls into sign-off bucket kind=HIGH: a stratum without keys certifies nothing (correct"
        " its values, or remove it in a new version)",
    ])  # fmt: skip
    assert not any(key in str(failed.value) for key, _ in keys)


@pytest.mark.parametrize("order", ["as read", "high-risk first"])
def test_a_high_risk_key_left_to_a_standard_bucket_fails_the_draw_whatever_the_order(order: str) -> None:
    """Without signoffStratum, a high-risk key whose high-risk bucket has no targetSize falls to a standard
    sign-off bucket: W4, whichever key the frame gives first, never the first key's tier for the stratum."""
    chosen = sample(
        dimension("degree", bucket("d0", target_size=3011)),
        dimension("flagged", bucket("Y", values=("Y",), risk_tier="high"), bucket("N")),
    )
    keys = frame(5000, degree=lambda i: 0, flagged=lambda i: "Y" if i % 10 == 0 else "N")
    if order == "high-risk first":
        keys.sort(key=lambda item: item[1]["flagged"] != "Y")
    with pytest.raises(DrawError) as failed:
        draw(chosen, keys)
    assert str(failed.value) == (
        "the frame gave 500 high-risk key(s) a standard sign-off label (degree=d0): test the high-risk"
        " conditions first in signoffStratum"
    )


def test_the_bucket_memo_is_bounded() -> None:
    """A min/max dimension over 200,000 distinct values: the selector holds heaps, never a memo per value."""
    bands = bucket("low", max="1000", target_size=3011), bucket("high", target_size=3011)
    selector = Selector(sample(dimension("bal", *bands)), SALT, NOW)
    tracemalloc.start()
    try:
        for start in range(0, 200_000, 10_000):
            batch = [(f"K{i:07d}", {"bal": Decimal(i) / 100}) for i in range(start, start + 10_000)]
            selector.add(batch, first=start + 1)
        del batch
        held = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    assert [s["frame_keys"] for s in selector.result().strata] == [100_001, 99_999]
    assert held < 16_000_000  # a memo entry per value held 55 MB


def test_a_draw_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DrawError, match=r"^the frame is empty$"):
        Selector(ALL, SALT, NOW).result()
    with pytest.raises(DrawError, match=r"^the sample is empty: raise the rates$"):
        draw(sample(method="rate", rate="0.0000001"), frame(10))
    monkeypatch.setattr(sampling, "LIMIT", 5)
    with pytest.raises(
        DrawError, match=r"^the sample would hold 10 keys, over 5: lower the rates, or compare"
    ):
        draw(sample(method="rate", rate="1"), frame(10), batch=3)
    contrast = sample(dimension("all", bucket("any", target_size=3)), rate="1")
    with pytest.raises(DrawError, match=r"^the sample would hold 8 keys, over 5"):
        draw(contrast, frame(8))  # the heap's keys counted once


def test_a_rate_sample_without_strata_is_contrast_only() -> None:
    result = draw(sample(method="rate", rate="0.5"), frame(200))
    assert result.strata == [] and result.keys and not any(d.member for d in result.keys)
    assert {d.stratum for d in result.keys} == {None}


def test_selection_sha256_hashes_the_lines_in_seq_order() -> None:
    lines = [(0, "m-k1", "degree=d0", "degree=d0", True), (1, "m-k2", "degree=d0", "degree=d0", False)]
    text = "0\tm-k1\tdegree=d0\tdegree=d0\ttrue\n1\tm-k2\tdegree=d0\tdegree=d0\tfalse\n"
    assert sampling.selection_sha256(lines) == hashlib.sha256(text.encode()).hexdigest()
