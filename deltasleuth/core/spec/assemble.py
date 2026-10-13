"""Build the engine's ``CompiledSpec`` from a validated spec document.

Validation guarantees the shapes, so lookups are direct. Rule paths are stored in canonical pattern
text and query paths in canonical text where expressible; numbers that feed exact comparisons become
``Decimal`` (``Decimal(repr(float))`` is exact for every human-written literal).
"""

from __future__ import annotations

import copy
import json
import re
import string
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.canonical.paths import normalise_pattern, normalise_query
from deltasleuth.core.spec.durations import parse_duration
from deltasleuth.core.types import (
    ArrayRule,
    CompareConfig,
    CompiledSpec,
    ExecutionConfig,
    FieldCompare,
    FieldRule,
    FrameMember,
    KeyConfig,
    KnownDiffRule,
    Normalizer,
    NotFoundRule,
    Pagination,
    RequestTemplate,
    Resolve,
    RetryPolicy,
    RuleClassification,
    Sample,
    SecurityPolicy,
    Severity,
    Side,
    SideConfig,
    StratumBucket,
    StratumDimension,
    TableConfig,
    TokenizeRule,
    TruncationIndicators,
)

_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")  # a path's percent-escape
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")  # RFC 3986 §2.3


def content_hash(doc: Mapping[str, Any]) -> str:
    return sha256_hex(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str))


def build_compiled_spec(doc: dict[str, Any]) -> CompiledSpec:
    raw = copy.deepcopy(doc)
    key, compare = raw["key"], raw["compare"]
    # A multi-part key's parts (``CompiledSpec.key_parts``): a composite key's beside a service side.
    service = any("table" not in entry for entry in raw["sides"].values())
    parts = tuple(key["parts"]) if key["type"] == "composite" and service else ()
    return CompiledSpec(
        name=raw["metadata"]["name"],
        version=raw["metadata"]["version"],
        content_hash=content_hash(raw),
        normalizer_version=raw["normalizerVersion"],
        key=KeyConfig(
            name=key["name"],
            type=key["type"],
            pattern=key.get("pattern"),
            value_class=key.get("class", "business_code"),
            normalize=tuple(Normalizer(name) for name in key.get("normalize", [])),
            parts=tuple(key.get("parts", [])),
        ),
        sides={side: _side(side, raw["sides"][side.value], parts) for side in Side},
        compare=CompareConfig(
            arrays=tuple(_array_rule(rule) for rule in compare.get("arrays", [])),
            fields=tuple(_field_rule(rule) for rule in compare.get("fields", [])),
            include=_patterns(compare.get("include")),
            exclude=_patterns(compare.get("exclude")),
            value_filter=compare.get("valueFilter"),
            null_equals_absent=compare.get("nullEqualsAbsent", False),
        ),
        known_diffs=tuple(
            _known_diff(rule) for rule in raw.get("knownDiffs", []) if rule.get("active", True)
        ),
        security=_security(raw.get("security", {})),
        execution=_execution(raw.get("execution", {})),
        domain_pack=raw["domainPack"]["name"] if "domainPack" in raw else None,
        sample=_sample(raw),
        raw=raw,
    )


def _side(side: Side, entry: dict[str, Any], parts: tuple[str, ...]) -> SideConfig:
    if "table" in entry:  # a missing row is the connector's 404
        table = _table(entry["table"], entry.get("typeMap", {}))
        return SideConfig(side, entry["role"], entry["target"], not_found=NotFoundRule(), table=table)
    not_found, truncation = entry.get("notFound"), entry.get("truncation", {})
    return SideConfig(
        side=side,
        role=entry["role"],
        target=entry["target"],
        request=_request(entry["request"], parts) if "request" in entry else None,  # None: a table side
        pagination=_pagination(entry["pagination"]) if "pagination" in entry else None,
        select=_query(entry.get("select")),
        map=entry.get("map"),
        not_found=NotFoundRule(tuple(not_found["status"]), not_found.get("when")) if not_found else None,
        success_status=tuple(entry.get("success", {}).get("status", [200])),
        truncation=_truncation(truncation) if "cap" in truncation else None,  # no cap: the target's rule
        truncation_flags=tuple(normalise_query(path) for path in truncation.get("flagPaths", ())),
    )


def _table(entry: Mapping[str, Any], types: Mapping[str, Any]) -> TableConfig:
    columns, keys = entry.get("columns"), entry["keyColumns"]
    return TableConfig(
        name=entry["name"],
        key_columns=tuple(k if isinstance(k, str) else k["name"] for k in keys),
        key_maps=tuple(None if isinstance(k, str) else k.get("keyMap") for k in keys),
        types=dict(types),
        columns=None
        if columns is None
        else tuple((c, c.lower()) if isinstance(c, str) else (c["name"], c["as"]) for c in columns),
        schema=entry.get("schema"),
        where=entry.get("where"),
        file=copy.deepcopy(entry["file"]) if "file" in entry else None,
    )


def listing_side(source: Mapping[str, Any]) -> SideConfig:
    """A ``paginated_endpoint`` key source as a side the connectors can walk (its items hold keys)."""
    return SideConfig(
        Side.A, "keys", source["target"], _request(source["request"]), _pagination(source["pagination"])
    )


def key_table(source: Mapping[str, Any]) -> SideConfig:
    """A key table (a ``table`` key source with a target and a table of its own) as a table side its plan
    reads: its key columns, key maps and filter, and no column to compare."""
    table = replace(_table(source["table"], {}), columns=())
    return SideConfig(Side.A, "keys", source["target"], not_found=NotFoundRule(), table=table)


def _sample(raw: Mapping[str, Any]) -> Sample | None:
    """A ``hash_sample`` key source; None for any other."""
    source = raw["key"].get("source")
    if source is None or source["kind"] != "hash_sample":
        return None
    frame = source["source"]
    frame = frame["sources"] if frame["kind"] == "union" else [frame]
    # The loader refuses another kind of frame member; a gated spec still builds for inspection.
    members = tuple(_member(raw, m) for m in frame if "side" in m or m["kind"] == "paginated_endpoint")
    resolve = source.get("resolve", {})
    return Sample(
        method=source.get("method", "rate"),
        label=source["salt"],
        members=members,
        population=tuple(sorted(m.member_id for m in members)),
        dimensions=tuple(
            StratumDimension(d["name"], tuple(map(_bucket, d["buckets"])), d.get("column"), d.get("cel"))
            for d in source.get("strata", [])
        ),
        rate=_threshold(source.get("rate")),
        signoff_stratum=source.get("signoffStratum"),
        resolve=Resolve(resolve.get("times", Resolve.times), _seconds(resolve.get("delay"), Resolve.delay_s)),
    )


def _member(raw: Mapping[str, Any], entry: Mapping[str, Any]) -> FrameMember:
    """A frame member and its population id: the table's name lower-cased, its schema, where and filters
    left out, or the listing's method and canonical path."""
    if entry["kind"] == "table":
        side = raw["sides"][entry["side"]]
        member_id = f"table:{side['target']}/{side['table']['name'].lower()}"
        columns = tuple(entry.get("strataColumns", []))
        return FrameMember(
            "table", Side(entry["side"]), member_id, columns, source=copy.deepcopy(dict(entry))
        )
    side = next(s for s in Side if raw["sides"][s.value]["target"] == entry["target"])
    canonical = _ESCAPE.sub(_unescaped, entry["request"]["path"])
    member_id = f"listing:{entry['target']}:{entry['request']['method']} {canonical}"
    fields = {name: normalise_query(path) for name, path in entry.get("strataFields", {}).items()}
    return FrameMember(
        "paginated_endpoint", side, member_id, fields=fields, source=copy.deepcopy(dict(entry))
    )


def _unescaped(escape: re.Match[str]) -> str:
    """A path's percent-escape as RFC 3986 §6.2.2 compares paths: an unreserved character's decoded,
    another's hex upper-cased, so every spelling of one endpoint names one population."""
    char = chr(int(escape[1], 16))
    return char if char in _UNRESERVED else escape[0].upper()


def _bucket(entry: Mapping[str, Any]) -> StratumBucket:
    return StratumBucket(
        label=entry["label"],
        values=tuple(entry["values"]) if "values" in entry else None,
        min=_decimal(entry["min"]) if "min" in entry else None,
        max=_decimal(entry["max"]) if "max" in entry else None,
        rate=_threshold(entry.get("rate")),
        target_size=entry.get("targetSize"),
        risk_tier=entry.get("riskTier", "standard"),
    )


def _threshold(rate: float | None) -> int | None:
    """A rate as the u a key's must be below: rate 1.0 is 2^64, a census."""
    return None if rate is None else int(Decimal(str(rate)) * 2**64)


def _request(entry: Mapping[str, Any], parts: tuple[str, ...] = ()) -> RequestTemplate:
    return RequestTemplate(
        method=entry["method"],
        path=entry["path"],
        query=copy.deepcopy(entry.get("query", {})),
        headers=dict(entry.get("headers", {})),
        body=copy.deepcopy(entry.get("body")),
        key_parts=parts,
    )


def _pagination(entry: Mapping[str, Any]) -> Pagination:
    return Pagination(
        kind=entry["kind"],
        items_path=_query(entry.get("itemsPath")),
        cursor_path=_query(entry.get("cursorPath")),
        cursor_param=entry.get("cursorParam"),
        page_param=entry.get("pageParam"),
        size_param=entry.get("sizeParam"),
        size=entry.get("size"),
        max_pages=entry.get("maxPages"),
    )


def _truncation(entry: dict[str, Any]) -> TruncationIndicators:
    return TruncationIndicators(
        cap=entry["cap"], items_path=_query(entry.get("itemsPath")), count_path=_query(entry.get("countPath"))
    )


def _array_rule(entry: dict[str, Any]) -> ArrayRule:
    taxonomy = entry.get("taxonomy", {})
    return ArrayRule(
        path=normalise_pattern(entry["path"]),
        mode=entry["mode"],
        key=tuple(entry.get("key", [])),
        severity=_severity(entry.get("severity")),
        taxonomy_missing_in_b=taxonomy.get("missing_in_b"),
        taxonomy_extra_in_b=taxonomy.get("extra_in_b"),
    )


def _field_rule(entry: dict[str, Any]) -> FieldRule:
    return FieldRule(
        path=normalise_pattern(entry["path"]),
        normalize=tuple(_normalizer(step) for step in entry.get("normalize", [])),
        compare=_field_compare(entry.get("compare", "exact")),
        severity=_severity(entry.get("severity")),
        taxonomy_code=entry.get("taxonomyCode"),
    )


def _normalizer(step: str | Mapping[str, Any]) -> Normalizer:
    """A bare name, or a single-key object ``{name: params}``."""
    if isinstance(step, str):
        return Normalizer(step)
    ((name, params),) = step.items()
    return Normalizer(name, dict(params))


def _field_compare(entry: Any) -> FieldCompare:
    """The schema's fieldCompare oneOf; an omitted tolerance bound counts as 0."""
    if entry == "exact":
        return FieldCompare()
    if "tolerance" in entry:
        bounds = entry["tolerance"]
        abs_tol, rel_tol = _decimal(bounds.get("abs", 0)), _decimal(bounds.get("rel", 0))
        return FieldCompare(mode="tolerance", abs_tol=abs_tol, rel_tol=rel_tol)
    similarity = entry["similarity"]
    return FieldCompare(
        mode="similarity",
        threshold=float(similarity["threshold"]),
        method=similarity.get("method", "token_set"),
    )


def _known_diff(entry: dict[str, Any]) -> KnownDiffRule:
    return KnownDiffRule(
        rule_id=entry["id"],
        when=entry["when"],
        classification=RuleClassification(entry["classification"]),
        reason=entry["reason"],
        ticket=entry["ticket"],
        expires=entry.get("expires"),
    )


def _security(entry: dict[str, Any]) -> SecurityPolicy:
    return SecurityPolicy(
        tokenize=tuple(
            TokenizeRule(normalise_pattern(rule["path"]), rule["class"]) for rule in entry.get("tokenize", [])
        ),
        clear=_patterns(entry.get("clear")),
        unlisted=entry.get("unlisted", "tokenize"),
    )


def _execution(entry: dict[str, Any]) -> ExecutionConfig:
    default = ExecutionConfig()
    timeouts = entry.get("timeouts", {})
    breaker = entry.get("circuitBreaker")
    return ExecutionConfig(
        concurrency=entry.get("concurrency", default.concurrency),
        delay_s=_seconds(entry.get("delay"), default.delay_s),
        jitter_s=_seconds(entry.get("jitter"), default.jitter_s),
        connect_timeout_s=_seconds(timeouts.get("connect"), default.connect_timeout_s),
        read_timeout_s=_seconds(timeouts.get("read"), default.read_timeout_s),
        total_timeout_s=_seconds(timeouts.get("total"), default.total_timeout_s),
        retries=_retries(entry.get("retries")),
        breaker_error_rate=float(breaker["errorRate"]["rate"]) if breaker else default.breaker_error_rate,
        breaker_window=breaker["errorRate"]["window"] if breaker else default.breaker_window,
        breaker_cooldown_s=_seconds(breaker["cooldown"] if breaker else None, default.breaker_cooldown_s),
    )


def _retries(entry: int | dict[str, Any] | None) -> RetryPolicy:
    """An integer (max retries with the default backoff) or a full policy object."""
    if entry is None:
        return RetryPolicy()
    if isinstance(entry, int):
        return RetryPolicy(max=entry)
    default, backoff = RetryPolicy(), entry.get("backoff", {})
    return RetryPolicy(
        max=entry["max"],
        initial_s=_seconds(backoff.get("initial"), default.initial_s),
        max_s=_seconds(backoff.get("max"), default.max_s),
        multiplier=float(backoff.get("multiplier", default.multiplier)),
        retry_on=tuple(entry.get("retryOn", default.retry_on)),
    )


def _seconds(duration: str | None, default: float) -> float:
    return default if duration is None else parse_duration(duration)


def _severity(value: str | None) -> Severity | None:
    return None if value is None else Severity(value)


def _query(path: str | None) -> str | None:
    return None if path is None else normalise_query(path)


def _patterns(paths: list[str] | None) -> tuple[str, ...]:
    return tuple(normalise_pattern(p) for p in paths or ())


def _decimal(value: int | float) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
