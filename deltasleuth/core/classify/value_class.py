"""Value class of a difference (§8.2): what kind of difference, never the values themselves.

It keeps the explanation once values are tokenised: ``tok:phone:…`` on both sides, but a
``digit_transposition``. It is computed on the normalised values when they differ, else on the
raw ones (NORMALIZATION_ONLY). ``duplicate_key`` and ``intermittent_b`` are set by the differ and
the recheck, never inferred here.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, localcontext

from deltasleuth.core.canonical.encoding import canonical_json, scalar_text, type_class
from deltasleuth.core.jsonio import number_text
from deltasleuth.core.types import JSON

_NUMERIC_TEXT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_DIGITS = frozenset("0123456789")
_SCALARS = frozenset({"null", "boolean", "number", "string"})


def value_class(a_raw: JSON, b_raw: JSON, a_norm: JSON, b_norm: JSON) -> str:
    """One of ``VALUE_CLASSES`` for a pair of values."""
    return _classify(a_raw, b_raw) if _same(a_norm, b_norm) else _classify(a_norm, b_norm)


def _same(a: JSON, b: JSON) -> bool:
    return canonical_json(a) == canonical_json(b)


def _classify(a: JSON, b: JSON) -> str:
    if a is None and b is None:  # a finding holds None for an absent side; equal nulls never differ
        return "null_vs_absent"
    if _same(a, b):  # e.g. an order change: the difference is not in these values
        return "other"
    ta, tb = type_class(a), type_class(b)
    if ta != tb:
        return "type_only" if _same_text(a, b, ta, tb) else "other"
    if ta == "string":
        return _string_class(a, b)
    if ta == "number":
        return _number_class(Decimal(a), Decimal(b))
    return "other"


def _same_text(a: JSON, b: JSON, ta: str, tb: str) -> bool:
    """Scalars of different JSON types with the same text: ``"12"`` vs 12, ``"true"`` vs true."""
    if not {ta, tb} <= _SCALARS:
        return False
    if scalar_text(a) == scalar_text(b):
        return True
    text, number = (b, a) if ta == "number" else (a, b)
    return "number" in (ta, tb) and _as_number(text) == Decimal(number)


def _as_number(value: JSON) -> Decimal | None:
    return Decimal(value) if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value) else None


def _string_class(a: str, b: str) -> str:
    if a.casefold() == b.casefold():
        return "case_only"
    if "".join(a.split()) == "".join(b.split()):
        return "whitespace"
    if [ch for ch in a if ch.isalnum()] == [ch for ch in b if ch.isalnum()]:
        return "format_only"
    if _digit_swap(a, b):
        return "digit_transposition"
    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None and _equal_at_lower_precision(na, nb):
        return "numeric_precision"
    return "other"


def _number_class(a: Decimal, b: Decimal) -> str:
    if _equal_at_lower_precision(a, b):
        return "numeric_precision"
    return "digit_transposition" if _digit_swap(number_text(a), number_text(b)) else "other"


def _digit_swap(a: str, b: str) -> bool:
    """Equal but for two adjacent, different digits swapped (``12345`` vs ``12435``)."""
    if len(a) != len(b):
        return False
    diffs = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    if len(diffs) != 2 or diffs[1] != diffs[0] + 1:
        return False
    i, j = diffs
    return a[i] == b[j] and a[j] == b[i] and {a[i], a[j]} <= _DIGITS


def _places(d: Decimal) -> int:
    return max(0, -int(d.as_tuple().exponent))


def _equal_at_lower_precision(a: Decimal, b: Decimal) -> bool:
    """The finer number, rounded or truncated to the coarser one's decimal places, equals it."""
    a, b = a.normalize(), b.normalize()
    if a == b or _places(a) == _places(b):
        return False
    coarse, fine = sorted((a, b), key=_places)
    quantum = Decimal(1).scaleb(-_places(coarse))
    # Enough precision for fine's digits plus a rounding carry, so quantize never raises.
    with localcontext(Context(prec=len(fine.as_tuple().digits) + 2)):
        roundings = (ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN)
        return any(fine.quantize(quantum, rounding=r) == coarse for r in roundings)
