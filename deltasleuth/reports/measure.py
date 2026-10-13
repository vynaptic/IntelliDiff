"""Black-box contract measurements of a run's observations, and the capabilities they support.

Per side: statuses, not-found body shapes (member names, never values) and the declared shapes
each satisfies, body sizes, live latency percentiles (nearest rank), array fan-out (raw and
canonical), truncation candidates (a count member beside an array) and pagination hints; from
them, a proposed ``TargetCapabilities`` block per target with the evidence for each field.

A field is proposed only when the samples support it soundly, else a REVIEW flag says
why: an absence without declared shapes only from the one body shape every known-missing key
answered with, as a predicate accepting exactly its member names (never an empty-body 404); a
truncation cap only as the most items any document returned. Names are JSON-quoted unless plain
identifiers and YAML comments are printable ASCII; patterns are published masked.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from deltasleuth.context import Context
from deltasleuth.core.canonical import predicates
from deltasleuth.core.canonical.canonicalise import shape
from deltasleuth.core.canonical.errors import CanonicalError, PathError
from deltasleuth.core.canonical.paths import ROOT_PATTERN, child_pattern, member_pattern, normalise_pattern
from deltasleuth.core.jsonio import JSONParseError, ResourceLimitError, parse_json
from deltasleuth.core.types import JSON, CompiledSpec, Observation, Side, TargetCapabilities
from deltasleuth.reports.data import load_run, observations, write_json
from deltasleuth.security.tokenise import mask_path_pattern

MEASURE_NAME, PROPOSAL_NAME = "measure.json", "capabilities.proposed.yaml"
NOT_FOUND_STATUSES = (404, 410)
PERCENTILES = (50, 95, 99)
_COUNT_NAME = re.compile(r"count|total|length|num")  # on squashed (lower-case) names
_PAGE_CONTROL = re.compile(r"page|limit|size|offset|cursor")  # bounds or numbers pages, never counts
_CURSOR_NAMES = {"nextcursor", "cursor", "nextpagetoken", "pagetoken", "continuationtoken"}
_LINK_NAMES = {"next", "nextlink", "nextpage", "nexturl", "odatanextlink"}
_PAGING_NAMES = {
    *_CURSOR_NAMES,
    *_LINK_NAMES,
    *"hasmore hasnext nextoffset offset page pagesize pagenumber limit".split(),
}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PRINTABLE = re.compile(r"[\x20-\x7e]+")
_NOT_PRINTABLE = re.compile(r"[^\x20-\x7e]")
_NEXT_REL = re.compile(r"""rel\s*=\s*"?next"?""", re.IGNORECASE)
CONFIRM_ABSENCE = (
    "needs human confirmation: check with the service owner that this is its documented "
    "'no such key' answer before merging"
)
CONFIRM_CAP = "needs human confirmation: check the measured cap against the documented one before merging"
Pairs = defaultdict[tuple[str, str], list[tuple[int, int]]]  # (items path, count path) -> (items, count)
_UNSTABLE = "ordering stability cannot be proven from single reads; declare true only if documented"
_GATEWAY = (
    "not proposed: any 404 can come from a gateway; pass keys that cannot exist (--missing-keys) "
    "to measure the service's own 'no such key' answer"
)


def nearest_rank(values: Sequence[float], percentile: int) -> float:
    return sorted(values)[max(1, math.ceil(percentile / 100 * len(values))) - 1]


def distribution(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    percentiles = {f"p{p}": nearest_rank(values, p) for p in PERCENTILES}
    return {"count": len(values), "max": max(values), **percentiles}


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _counts(name: str, value: JSON) -> bool:
    integer = isinstance(value, Decimal) and value == value.to_integral_value()
    return integer and bool(_COUNT_NAME.search(_squash(name))) and not _PAGE_CONTROL.search(_squash(name))


def name_label(name: str) -> str:
    """Bare when a plain identifier, else JSON-quoted ASCII: no line break can reach the output."""
    return name if _IDENTIFIER.fullmatch(name) else json.dumps(name, ensure_ascii=True)


def _nodes(doc: JSON, pattern: str = ROOT_PATTERN) -> Iterator[tuple[str, JSON]]:
    """(pattern, node) of every node of a parsed document, in the spec's path grammar."""
    yield pattern, doc
    if isinstance(doc, dict):
        for name, value in doc.items():
            yield from _nodes(value, child_pattern(pattern, name))
    elif isinstance(doc, list):
        for value in doc:
            yield from _nodes(value, member_pattern(pattern))


def _count_pairs(doc: JSON) -> Iterator[tuple[str, str, int, int]]:
    """(items path, count path, items returned, count reported): a count member beside an array,
    directly or one object level down (``meta.total``)."""
    for pattern, node in _nodes(doc):
        if not isinstance(node, dict):
            continue
        counts = [(child_pattern(pattern, n), int(v)) for n, v in node.items() if _counts(n, v)]
        counts += [
            (child_pattern(child_pattern(pattern, n), sub), int(v))
            for n, inner in node.items() if isinstance(inner, dict)
            for sub, v in inner.items() if _counts(sub, v)
        ]  # fmt: skip
        for name, items in node.items():
            if isinstance(items, list):
                yield from ((child_pattern(pattern, name), c, len(items), total) for c, total in counts)


def body_shape(body: bytes | None) -> tuple[str, JSON]:
    """(shape label, parsed value): an object body's sorted member names, never its values."""
    if not body:
        return "<empty>", None
    try:
        value, _ = parse_json(body)
    except (JSONParseError, ResourceLimitError):
        return "<unparseable>", None
    if isinstance(value, dict):
        return "{" + ", ".join(name_label(n) for n in sorted(value)) + "}", value
    return ("<array>" if isinstance(value, list) else "<scalar>"), value


@dataclass
class _NotFound:
    status: int
    shape: str
    names: tuple[str, ...]  # of an object body
    count: int = 0
    known_missing: int = 0  # answers to keys known not to exist
    matches: frozenset[str] = frozenset()  # declared shapes these bodies satisfy


def _answer(obs: Observation) -> str:
    return f"error:{obs.error_class}" if obs.error_class else str(obs.http_status)


def _is_not_found(obs: Observation) -> bool:
    return not obs.error_class and obs.http_status in NOT_FOUND_STATUSES


def _holds(predicate: str, variables: Mapping[str, Any]) -> bool:
    try:
        return predicates.test(predicate, variables)
    except predicates.PredicateError:
        return False  # the engine treats an evaluation error as "no match" too


def _not_found_shapes(
    found: list[Observation], declared: TargetCapabilities, missing: Collection[str]
) -> list[_NotFound]:
    shapes: dict[tuple[int, str, frozenset[str]], _NotFound] = {}
    for obs in filter(_is_not_found, found):
        label, value = body_shape(obs.body)
        names = tuple(sorted(value)) if isinstance(value, dict) else ()
        matches: frozenset[str] = frozenset()
        if label not in ("<empty>", "<unparseable>"):  # bound as observe() binds them
            variables = predicates.response_variables(obs.http_status, obs.headers, value)
            matches = frozenset(name for name, p in declared.absence_shapes.items() if _holds(p, variables))
        # Grouped by what the body matched too: a predicate may test values, so equal member names
        # do not make equal answers.
        entry = shapes.setdefault(
            (obs.http_status, label, matches), _NotFound(obs.http_status, label, names, matches=matches)
        )
        entry.count += 1
        entry.known_missing += obs.key in missing
    return sorted(shapes.values(), key=lambda s: (s.status, -s.count, s.shape))


def _documents(docs: list[JSON], spec: CompiledSpec, side: Side) -> tuple[dict, Pairs, Counter, int]:
    """(canonical fan-out, count/array pairs, paging members, unmappable) of a side's 2xx documents."""
    raw = _raw_publisher(spec, side)
    canonical: defaultdict[str, list[int]] = defaultdict(list)
    pairs: Pairs = defaultdict(list)
    paging: Counter[tuple[str, str]] = Counter()
    unmappable = 0
    for doc in docs:
        for path, node in _nodes(doc):
            if isinstance(node, dict):
                names = [n for n in node if _squash(n) in _PAGING_NAMES]
                paging.update((raw(child_pattern(path, n)), _squash(n)) for n in names)
        for items, count, returned, total in _count_pairs(doc):
            pairs[(raw(items), raw(count))].append((returned, total))
        try:
            shaped = shape(doc, spec.sides[side])
        except CanonicalError:
            unmappable += 1
            continue
        for path, node in _nodes(shaped):
            if isinstance(node, list):
                canonical[mask_path_pattern(path, spec)].append(len(node))
    return canonical, pairs, paging, unmappable


def _truncation(pairs: list[tuple[int, int]]) -> dict[str, int]:
    """How a count member compared with its array over the documents: (items returned, count) pairs."""
    exceeds, below = sum(t > r for r, t in pairs), sum(t < r for r, t in pairs)
    equal, most = len(pairs) - exceeds - below, max(r for r, _ in pairs)
    return {"docs": len(pairs), "equal": equal, "total_exceeds_items": exceeds, "total_below_items": below,
            "max_items": most}  # fmt: skip


def _measure_side(
    found: list[Observation],
    spec: CompiledSpec,
    side: Side,
    declared: TargetCapabilities,
    missing: Collection[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ok = [o for o in found if not o.error_class]
    docs = [d for o in ok if 200 <= (o.http_status or 0) < 300 and (d := body_shape(o.body)[1]) is not None]
    canonical, pairs, paging, unmappable = _documents(docs, spec, side)
    shapes = _not_found_shapes(found, declared, missing)
    link_next = sum(bool(_NEXT_REL.search(o.headers.get("link", ""))) for o in found)
    live = [o.latency_ms for o in ok if o.source == "live" and o.latency_ms is not None]
    measured = {
        "target": spec.sides[side].target, "observations": len(found), "keys": len({o.key for o in found}),
        "statuses": dict(sorted(Counter(map(_answer, found)).items())),
        "attempts": {str(k): v for k, v in sorted(Counter(o.attempts for o in found).items())},
        "body_bytes": distribution([float(len(o.body)) for o in found if o.body is not None]),
        "latency_ms": distribution(live),
        "not_found_shapes": [{"status": s.status, "body_keys": s.shape, "count": s.count,
                              "declared_shapes": sorted(s.matches)} for s in shapes],
        "fanout_canonical": _fanout(canonical), "unmappable_documents": unmappable,
        "truncation_candidates": [{"items_path": i, "count_path": c, **_truncation(v)}
                                  for (i, c), v in sorted(pairs.items())],
        "pagination_hints": [{"path": path, "docs": n} for (path, _), n in sorted(paging.items())],
        "link_next_headers": link_next,
    }  # fmt: skip
    proposals = [
        *propose_absence(shapes, declared.absence_shapes, found, missing),
        propose_truncation(pairs, declared),
        propose_pagination(paging, link_next, len(found)),
        _proposal("stable_ordering", False, [_UNSTABLE]),
    ]
    return measured, proposals


def _raw_publisher(spec: CompiledSpec, side: Side) -> Callable[[str], str]:
    """How a raw-body pattern is published: below a mapless side's single ``select`` node it is a
    canonical pattern, masked like one; elsewhere raw and canonical paths do not correspond."""
    cfg = spec.sides[side]
    try:
        prefix = None if cfg.map else normalise_pattern(cfg.select or ROOT_PATTERN)
    except PathError:  # a filter or union select: no single node
        prefix = None
    if prefix is None or "[*]" in prefix:
        return lambda pattern: pattern

    def publish(pattern: str) -> str:
        rest = pattern[len(prefix) :]
        if not pattern.startswith(prefix) or rest[:1] not in ("", ".", "["):
            return pattern  # outside the selected node
        return prefix + mask_path_pattern(ROOT_PATTERN + rest, spec)[len(ROOT_PATTERN) :]

    return publish


def _proposal(name: str, value: Any, evidence: Iterable[str], flags: Iterable[str] = ()) -> dict[str, Any]:
    return {"field": name, "value": value, "evidence": list(evidence), "flags": list(flags)}


def _shape_counts(shapes: Iterable[_NotFound]) -> str:
    ordered = sorted(shapes, key=lambda s: (-s.count, s.status, s.shape))
    return "; ".join(f"HTTP {s.status} {s.shape} x{s.count}" for s in ordered)


def propose_absence(
    shapes: list[_NotFound], declared: Mapping[str, str], found: list[Observation], missing: Collection[str]
) -> list[dict[str, Any]]:
    if not shapes and (declared or not missing):
        cannot = "no 404/410 response observed: absence cannot be measured; declare it from documentation"
        return [_proposal("authoritative_absence", None, [cannot])]
    if declared:
        return _absence_against_declared(shapes, declared)
    return _absence_from_observed(shapes, found, missing)


def _absence_against_declared(shapes: list[_NotFound], declared: Mapping[str, str]) -> list[dict[str, Any]]:
    matched = [s for s in shapes if s.matches]
    names = sorted({name for s in matched for name in s.matches})
    evidence = [
        f"{s.count} HTTP {s.status} response(s) with body keys {s.shape} match declared shape(s) "
        f"{', '.join(sorted(s.matches))}"
        for s in matched
    ]
    flags = [
        f"UNANNOUNCED not-found shape: {s.count} HTTP {s.status} response(s) with body keys {s.shape} match "
        "no declared absence shape; they are not authoritative (INCOMPLETE absence_unconfirmed)"
        for s in shapes
        if not s.matches
    ]
    flags += [
        f"declared shape {name} accepts {len(accepted)} different bodies ({_shape_counts(accepted)}); "
        "a shape that loose may also accept gateway or infrastructure answers"
        for name in names
        if len(accepted := [s for s in matched if name in s.matches]) > 1
    ]
    if not matched:
        return [_proposal("authoritative_absence", None, evidence, flags)]
    value = {"status": sorted({s.status for s in matched}), "shapes": names}
    return [
        _proposal("authoritative_absence", value, evidence, flags),
        _proposal("absence_shapes", {n: declared[n] for n in names}, ["declared predicates that matched"]),
    ]


def _absence_from_observed(
    shapes: list[_NotFound], found: list[Observation], missing: Collection[str]
) -> list[dict[str, Any]]:
    """Propose only the one shape every observed answer of a known-missing key shares."""
    seen = {o.key for o in found if o.key in missing}
    other = Counter(
        ("" if o.error_class else "HTTP ") + _answer(o)
        for o in found
        if o.key in missing and not _is_not_found(o)
    )
    evidence = [
        f"{s.count} HTTP {s.status} response(s) with body keys {s.shape}"
        + (f", {s.known_missing} of them for known-missing keys" if s.known_missing else "")
        for s in shapes
    ]
    if missing:
        evidence.append(f"{len(seen)} of {len(missing)} known-missing key(s) observed")
    several = (
        f"several distinct not-found shapes ({_shape_counts(shapes)}): the service documents one 'no such "
        "key' answer; the others are likely gateway or infrastructure answers and must stay unconfirmed"
    )
    flags = [several] if len(shapes) > 1 else []
    known = [s for s in shapes if s.known_missing]
    answers = "; ".join([f"HTTP {s.status} {s.shape} x{s.known_missing}" for s in known]
                        + [f"{answer} x{n}" for answer, n in sorted(other.items())])  # fmt: skip
    if not missing:
        reason = _GATEWAY
    elif not seen:
        reason = "not proposed: no known-missing key was observed on this side"
    elif other or len(known) != 1:
        reason = f"not proposed: known-missing keys did not all answer with one not-found shape ({answers})"
    elif not known[0].names or not all(_PRINTABLE.fullmatch(n) for n in known[0].names):
        s = known[0]
        reason = (
            f"not proposed: known-missing keys answer HTTP {s.status} with body {s.shape}, which no shape "
            f"predicate can describe; a status-only declaration would accept every {s.status}, gateway "
            "answers included"
        )
    else:
        s = known[0]
        name = "shape_" + "_".join(re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_") for n in s.names)
        # Exact, not "has these members": a gateway answer with extra or other members cannot satisfy it.
        members = " && ".join(f"{json.dumps(n)} in body" for n in s.names)
        predicate = f"type(body) == map && size(body) == {len(s.names)} && {members}"
        exact = "predicate accepts exactly the member names every known-missing key answered with"
        return [
            _proposal("authoritative_absence", {"status": [s.status], "shapes": [name]}, evidence,
                      [*flags, CONFIRM_ABSENCE]),
            _proposal("absence_shapes", {name: predicate}, [exact], [CONFIRM_ABSENCE]),
        ]  # fmt: skip
    return [_proposal("authoritative_absence", None, evidence, [*flags, reason])]


def propose_truncation(pairs: Pairs, declared: TargetCapabilities) -> dict[str, Any]:
    stats = {k: _truncation(v) for k, v in sorted(pairs.items())}
    # A count can total its array only if it matched it at least once and never fell below it.
    plausible = {k: t for k, t in stats.items() if t["total_below_items"] == 0 and t["equal"] > 0}
    evidence = [
        f"{count} counts {items}: {t['docs']} docs, equal {t['equal']}, "
        f"total > items {t['total_exceeds_items']}, max items {t['max_items']}"
        for (items, count), t in plausible.items()
    ]
    if discarded := len(stats) - len(plausible):
        evidence.append(
            f"{discarded} count/array pairing(s) discarded: the count was below the items returned"
        )
    if ti := declared.truncation_indicators:
        evidence.append(f"declared: cap {ti.cap}, items {ti.items_path}, count {ti.count_path}")
    exceeding = sorted((k for k, t in plausible.items() if t["total_exceeds_items"]),
                       key=lambda k: (-plausible[k]["total_exceeds_items"], k))  # fmt: skip
    if not exceeding:
        tracking = (
            "a count member tracks its array but never exceeded it: declare truncation_indicators with the "
            "documented cap (the cap cannot be measured from these samples)"
        )
        return _proposal("truncation_indicators", None, evidence or ["no count member counts an array"],
                         [tracking] if plausible else [])  # fmt: skip
    flags = []
    for items, count in exceeding:
        when = sorted({returned for returned, total in pairs[(items, count)] if total > returned})
        if len(when) > 1:
            problem = f"items returned varied when the total exceeded them ({when})"
        elif when[0] < (most := plausible[(items, count)]["max_items"]):
            problem = f"the total exceeded the items at {when[0]}, but documents returned up to {most} items"
        else:
            value = {"cap": when[0], "items_path": items, "count_path": count}
            return _proposal("truncation_indicators", value, evidence, [*flags, CONFIRM_CAP])
        flags.append(f"{count} is not a cap of {items}: {problem}")
    return _proposal("truncation_indicators", None, evidence, flags)


def propose_pagination(paging: Counter[tuple[str, str]], link_next: int, observations: int) -> dict[str, Any]:
    names = {name for _, name in paging}
    evidence = [f"{path}: {n} doc(s)" for (path, _), n in sorted(paging.items())]
    if link_next:
        return _proposal(
            "pagination", "link_header", [*evidence, f'Link rel="next" on {link_next} responses']
        )
    for found, value, flag in (
        (_CURSOR_NAMES, "cursor", "confirm cursor_path and cursor_param in the spec"),
        (_LINK_NAMES, "cursor", "a next-link member: model it as a cursor or link"),
        ({"page", "pagenumber"}, "page", "page paging needs declared stable ordering"),
        ({"offset"}, "offset", "offset paging needs declared stable ordering"),
    ):
        if names & found:
            return _proposal("pagination", value, evidence, [flag])
    return _proposal(
        "pagination", "none", [f"no pagination member or Link header in {observations} responses"]
    )


def _fanout(arrays: Mapping[str, list[int]]) -> dict[str, dict[str, float]]:
    return {
        path: {"docs": len(v), "max": max(v), "p95": nearest_rank(v, 95), "mean": round(sum(v) / len(v), 3)}
        for path, v in sorted(arrays.items())
    }


def measure(
    found: Iterable[Observation],
    *,
    spec: CompiledSpec,
    capabilities: Mapping[str, TargetCapabilities],
    missing_keys: Collection[str] = (),
) -> dict[str, Any]:
    """Measurements per side and proposals per target (``measure.json``) against the targets'
    declared ``capabilities`` (by target id); ``missing_keys`` are raw keys known not to exist, the
    only evidence an absence proposal without declared shapes rests on."""
    found, missing = list(found), frozenset(missing_keys)
    sides, proposals = {}, defaultdict(list)
    for side, cfg in spec.sides.items():
        own = [o for o in found if o.side is side]
        sides[side.value], proposed = _measure_side(own, spec, side, capabilities[cfg.target], missing)
        if own:
            proposals[cfg.target] += proposed
    return {"sides": sides, "proposed_capabilities": dict(sorted(proposals.items()))}


def capabilities_yaml(measurement: Mapping[str, Any]) -> str:
    """The proposal as a commented targets-file fragment, every comment printable ASCII."""
    lines = [
        "# Proposed TargetCapabilities, measured black-box by DeltaSleuth.",
        "# Evidence, not a declaration: review each field and merge it into targets.yaml by hand.",
        "targets:",
    ]
    for target, proposals in measurement["proposed_capabilities"].items():
        lines += [f"  {_flow(target)}:", "    capabilities:"]
        for p in proposals:
            lines += [f"      # evidence: {_comment(line)}" for line in p["evidence"]]
            lines += [f"      # REVIEW: {_comment(flag)}" for flag in p["flags"]]
            live = p["value"] is not None
            lines.append(
                f"      {p['field']}: {_flow(p['value'])}" if live else f"      # {p['field']}: not proposed"
            )
    return "\n".join(lines) + "\n"


def _comment(text: str) -> str:
    return _NOT_PRINTABLE.sub(lambda m: m.group().encode("unicode_escape").decode("ascii"), text)


def _flow(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, sort_keys=True, width=10**6)
    return text.strip().removesuffix("...").strip()


async def measure_run(
    ctx: Context, run_id: str, out_dir: Path, *, cycle: int | None = None, missing_keys: Collection[str] = ()
) -> dict[str, Any]:
    """Measure a run cycle's observations; write measure.json and the proposal into ``out_dir``."""
    data = await load_run(ctx, run_id, cycle)
    found = await observations(ctx, data)
    capabilities = ctx.capabilities(data.run)  # as declared when the run ran
    measurement = measure(found, spec=data.spec, capabilities=capabilities, missing_keys=missing_keys)
    write_json(out_dir / MEASURE_NAME, {"run_id": run_id, "cycle": data.cycle, **measurement})
    (out_dir / PROPOSAL_NAME).write_text(capabilities_yaml(measurement), encoding="utf-8")
    return measurement
