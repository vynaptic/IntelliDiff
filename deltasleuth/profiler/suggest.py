"""Select detection and normaliser suggestions (§8.6).

A wrapper object is selected so envelope members stay out of the comparison. Aligned values
that agree by light token but differ as written get the normaliser chain that reconciles them,
never one that discards precision the samples do not force away (it would turn later
differences finer than it into a MATCH).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from deltasleuth.core.canonical.encoding import exact_json
from deltasleuth.core.canonical.normalisers import apply_chain
from deltasleuth.core.jsonio import number_text
from deltasleuth.core.types import JSON, Normalizer
from deltasleuth.profiler.align import Alignment
from deltasleuth.profiler.model import ROOT, EvidenceRow, Path, render
from deltasleuth.profiler.profile import LEAF, SCALAR_ARRAY, PathStats, SideProfile
from deltasleuth.profiler.values import (
    decimal_places,
    international_phone,
    is_email,
    is_phone_like,
    light_token,
    numeric_value,
    parse_timestamp,
)

WRAPPER_MIN_SHARE = 0.99
PAYLOAD_MIN_LEAF_SHARE = 0.5
MAX_SELECT_DEPTH = 3
CASE_MAJORITY = 0.9
GB_CALLING_CODE = 44
MAX_DECIMAL_SCALE = 38  # the schema's maximum for decimal.scale
TIMESTAMP_PRECISIONS = (("us", 6), ("ms", 3), ("s", 0))  # finest first

_METADATA_NAMES = frozenset(
    {
        "meta",
        "metadata",
        "asof",
        "links",
        "link",
        "paging",
        "pagination",
        "page",
        "pageinfo",
        "requestid",
        "traceid",
        "correlationid",
        "version",
        "apiversion",
        "timestamp",
        "servertime",
        "generatedat",
        "status",
        "statuscode",
        "message",
        "messages",
        "error",
        "errors",
        "warnings",
        "info",
        "debug",
        "etag",
        "self",
        "href",
        "cursor",
        "next",
        "prev",
        "previous",
        "total",
        "totalcount",
        "count",
        "took",
        "success",
        "ok",
    }
)
_EXCLUSIVE = ("phone", "timestamp", "decimal", "email")
# A trailing phone extension ("ext. 201", "x201", ";ext=201", "#201"): e164 keeps it.
_EXTENSION = re.compile(r"\s*(?:;\s*ext\s*=|ext\.?|extension|x|#)\s*\d+\s*$", re.IGNORECASE)

Normaliser = str | dict[str, dict[str, Any]]


@dataclass(frozen=True)
class SelectDecision:
    path: Path
    dropped: tuple[str, ...]  # envelope members left outside the select, as RFC 9535 paths
    leaf_share: float | None
    reason: str


def is_metadata_name(name: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", name.casefold()) in _METADATA_NAMES


def _leaves(value: JSON) -> int:
    if isinstance(value, dict | list):
        return sum(map(_leaves, value.values() if isinstance(value, dict) else value))
    return 1


def _wrapper_step(docs: Sequence[JSON]) -> tuple[str, list[str], float] | None:
    """(payload member, envelope siblings, payload leaf share) if ``docs`` share a wrapper."""
    objs = [d for d in docs if isinstance(d, dict)]
    if not objs or len(objs) < WRAPPER_MIN_SHARE * len(docs):
        return None
    names = list(dict.fromkeys(k for o in objs for k in o))
    holds_object = [
        n for n in names if sum(isinstance(o.get(n), dict) for o in objs) >= WRAPPER_MIN_SHARE * len(objs)
    ]
    if len(names) == 1:
        return (names[0], [], 1.0) if holds_object else None
    payload = [n for n in holds_object if not is_metadata_name(n)]
    siblings = [n for n in names if n not in payload]
    if len(payload) != 1 or not all(map(is_metadata_name, siblings)):
        return None
    name = payload[0]
    shares = [_leaves(o[name]) / max(1, _leaves(o)) for o in objs if isinstance(o.get(name), dict)]
    share = sum(shares) / len(shares)
    return (name, siblings, share) if share >= PAYLOAD_MIN_LEAF_SHARE else None


def detect_select(docs: Sequence[JSON]) -> SelectDecision:
    """The wrapper path to select on one side (the root when documents are not wrapped)."""
    path, dropped, share = ROOT, [], None
    for _ in range(MAX_SELECT_DEPTH):
        step = _wrapper_step(docs)
        if step is None:
            break
        name, siblings, step_share = step
        dropped += [render((*path, s)) for s in siblings]
        path = (*path, name)
        share = step_share if share is None else share * step_share
        docs = [d[name] for d in docs if isinstance(d, dict) and isinstance(d.get(name), dict)]
    reason = (
        "documents are not wrapped"
        if not path
        else "payload object beside envelope metadata"
        if dropped
        else "single-member wrapper object"
    )
    return SelectDecision(path, tuple(dropped), share, reason)


@dataclass(frozen=True)
class NormaliserSuggestion:
    path_a: Path  # canonical path of the compared values (a scalar array's members end in [*])
    path_b: Path
    chain: tuple[Normaliser, ...]
    compared: int
    differing: int
    note: str = ""


def _string_classes(x: str, y: str) -> set[str]:
    if x.strip() == y.strip():
        return {"trim"}
    cx, cy = " ".join(x.split()), " ".join(y.split())
    spaces = {"trim"} if x != x.strip() or y != y.strip() else set()
    if cx != x.strip() or cy != y.strip():
        spaces.add("collapse_ws")
    if cx == cy:
        return spaces
    if cx.casefold() == cy.casefold():
        return spaces | {"case"}
    dx, dy = re.sub(r"\D", "", x), re.sub(r"\D", "", y)
    if dx and dx == dy and not re.search(r"[^\W\d_]", x + y):
        return {"digits_only"}
    return {"other"}


def classify_difference(x: JSON, y: JSON) -> set[str]:
    """Value classes of two values that match by light token but differ as written."""
    if isinstance(x, str) and isinstance(y, str):
        if parse_timestamp(x) is not None and parse_timestamp(y) is not None:
            return {"timestamp"}
        if is_email(x) and is_email(y):
            return {"email"} | ({"trim"} if x != x.strip() or y != y.strip() else set())
        international = international_phone(x) is not None or international_phone(y) is not None
        if international and is_phone_like(x) and is_phone_like(y):
            return {"phone"}
        if numeric_value(x) is not None and numeric_value(y) is not None:
            return {"decimal"}
        return _string_classes(x, y)
    return {"decimal"} if numeric_value(x) is not None and numeric_value(y) is not None else {"other"}


def _matched_pairs(sa: PathStats, sb: PathStats, docs: Sequence[int]) -> list[tuple[JSON, JSON]]:
    """Value pairs of the same sample pair that agree by light token, matched in order."""
    pairs = []
    for doc in docs:
        by_token: dict[str | None, list[JSON]] = {}
        for v in sb.values.get(doc, []):
            by_token.setdefault(light_token(v), []).append(v)
        for v in sa.values.get(doc, []):
            token = light_token(v)
            if token is not None and by_token.get(token):
                pairs.append((v, by_token[token].pop(0)))
    return pairs


def _case_normaliser(values: Sequence[JSON]) -> str:
    """The case side a already writes in, so canonical values read like the baseline."""
    lettered = [v for v in values if isinstance(v, str) and v.upper() != v.lower()]
    for name, convert in (("upper", str.upper), ("lower", str.lower)):
        if lettered and sum(v == convert(v) for v in lettered) >= CASE_MAJORITY * len(lettered):
            return name
    return "casefold"


def _fraction_digits(st: PathStats) -> int:
    """Most fraction-of-second digits this side writes in a timestamp."""
    stamps = [parse_timestamp(v) for v in st.all_values() if isinstance(v, str)]
    return max((ts.fraction_digits for ts in stamps if ts is not None), default=0)


def _timestamp_chain(
    pairs: Sequence[tuple[JSON, JSON]], sa: PathStats, sb: PathStats
) -> tuple[tuple[Normaliser, ...], str]:
    """timestamp_utc at the finest precision that reconciles every pair, per the engine itself."""
    written = [_fraction_digits(sa), _fraction_digits(sb)]
    shown = f"fraction-of-second digits written: up to {written[0]} on side a, {written[1]} on side b"
    for precision, digits in TIMESTAMP_PRECISIONS:
        params = {"precision": precision, "mode": "truncate"}
        chain = [Normalizer("timestamp_utc", params)]
        if all(apply_chain(x, chain) == apply_chain(y, chain) for x, y in pairs):
            if max(written) > digits:  # the data forces the loss: say what becomes a MATCH
                return ({"timestamp_utc": params},), (
                    f"precision {precision} is the finest at which the samples agree and is lossy ({shown}): "
                    f"differences finer than {precision} become MATCH"
                )
            return ({"timestamp_utc": params},), (
                f"precision {precision} keeps every digit either side writes ({shown})"
            )
    return (), "timestamp_utc reconciles these timestamps at no precision; review"


def _decimal_chain(values: Sequence[JSON]) -> tuple[tuple[Normaliser, ...], str]:
    """decimal at the largest scale: samples cannot tell which decimals a field will ever carry."""
    numbers = [n for n in map(numeric_value, values) if n is not None]
    needed = max((len(number_text(n).partition(".")[2]) for n in numbers), default=0)
    if needed > MAX_DECIMAL_SCALE:
        return (), (
            f"numbers need up to {needed} decimal places, more than decimal keeps (scale "
            f"{MAX_DECIMAL_SCALE}), so no normaliser is drafted; review"
        )
    written = max((p for p in map(decimal_places, values) if p is not None), default=0)
    return ({"decimal": {"scale": MAX_DECIMAL_SCALE}},), (
        f"scale {MAX_DECIMAL_SCALE} (the schema maximum) keeps every digit, so only the writing is "
        f"reconciled (samples write up to {written} decimal places); a lower scale makes differences "
        "finer than it MATCH"
    )


def _phone_chain(values: Sequence[JSON]) -> tuple[tuple[Normaliser, ...], str]:
    # e164 parses leniently: a field mixing identifier types would read another type's digit run
    # as a phone number, so every value must be one.
    strings = [v for v in values if isinstance(v, str)]
    if others := sum(1 for v in strings if v.strip() and not is_phone_like(_EXTENSION.sub("", v))):
        return (), (
            f"phone formats differ, but {others} value(s) at this path (both sides) are not phone "
            "numbers; e164 applies to phone numbers only (split the values by type in the map)"
        )
    codes = [p.country_code for v in strings if (p := international_phone(v)) is not None]
    share = sum(c == GB_CALLING_CODE for c in codes) / len(codes) if codes else 0.0
    if share > 0.5:
        return ({"e164": {"defaultRegion": "GB"}},), f"+44 numbers dominate ({share:.0%} of {len(codes)})"
    return (), (
        f"phone formats differ but +44 numbers do not dominate ({share:.0%} of {len(codes)}); "
        "add e164 with the defaultRegion of the national-format numbers"
    )


def _chain(
    classes: Counter[str], differing: Sequence[tuple[JSON, JSON, set[str]]], sa: PathStats, sb: PathStats
) -> tuple[tuple[Normaliser, ...], str]:
    values = sa.all_values() + sb.all_values()
    exclusive = [c for c in _EXCLUSIVE if classes[c]]
    if exclusive:
        kind = max(exclusive, key=lambda c: classes[c])
        if kind == "phone":
            return _phone_chain(values)
        if kind == "timestamp":
            return _timestamp_chain([(x, y) for x, y, found in differing if kind in found], sa, sb)
        if kind == "decimal":
            return _decimal_chain(values)
        spaces: tuple[Normaliser, ...] = ("trim",) if classes["trim"] else ()
        return (*spaces, "email_basic"), "e-mail addresses differ in case or spacing"
    if classes["digits_only"]:
        return ("digits_only",), "values differ only in separators"
    chain: list[Normaliser] = [c for c in ("trim", "collapse_ws") if classes[c]]
    if classes["case"]:
        chain.append(_case_normaliser(sa.all_values()))
    note = "" if chain else "values match only after light normalisation no normaliser expresses; review"
    return tuple(chain), note


def _step_text(step: Normaliser) -> str:
    if isinstance(step, str):
        return step
    ((name, params),) = step.items()
    return f"{name}{{{', '.join(f'{k}: {v}' for k, v in params.items())}}}"


def chain_text(chain: Sequence[Normaliser]) -> str:
    """``trim, decimal{scale: 2}``."""
    return ", ".join(map(_step_text, chain))


def suggest_normalisers(
    pa: SideProfile, pb: SideProfile, alignment: Alignment
) -> tuple[list[NormaliserSuggestion], list[EvidenceRow], list[str]]:
    """Normaliser chains for aligned leaves whose values differ only in presentation."""
    suggestions, rows, notes = [], [], []
    for path_a, path_b in alignment.matches.items():
        kind = pa.kind(path_a)
        if kind not in (LEAF, SCALAR_ARRAY):
            continue
        sa, sb = pa.stats[path_a], pb.stats[path_b]
        pairs = _matched_pairs(sa, sb, alignment.common_docs)
        differing = [(x, y, classify_difference(x, y)) for x, y in pairs if exact_json(x) != exact_json(y)]
        if not differing:
            continue
        classes = Counter(c for _, _, found in differing for c in found)
        chain, why = _chain(classes, differing, sa, sb)
        if kind == SCALAR_ARRAY:
            path_a, path_b = (*path_a, None), (*path_b, None)
        note = f"{len(differing)} of {len(pairs)} matched value pairs differ as written; {why}".rstrip("; ")
        if chain:
            suggestions.append(NormaliserSuggestion(path_a, path_b, chain, len(pairs), len(differing), note))
        else:
            notes.append(f"{render(path_a)}: {why}")
        scores = {
            "compared": len(pairs),
            "differing": len(differing),
            **{f"class_{c}": n for c, n in sorted(classes.items())},
        }
        rows.append(
            EvidenceRow(
                "normalize",
                render(path_a),
                render(path_b),
                chain_text(chain) or "none",
                scores,
                len(pairs),
                note,
            )
        )
    return suggestions, rows, notes
