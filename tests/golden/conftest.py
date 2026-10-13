"""Golden-case loader: YAML case files -> engine types.

Cases are written in the spec DSL's own shapes (camelCase), and the compare block and side
members are validated against spec.schema.json, so the corpus also checks that the engine reads
the DSL as the schema defines it. Layout::

    description: why this case exists
    key: K1                                  # optional
    compare: {arrays, fields, include, exclude, valueFilter, nullEqualsAbsent}
    sides: {a: {select, map, notFound, success, truncation, pagination}, b: {...}}   # optional
    targets: {a: {authoritativeAbsence, absenceShapes, truncationIndicators,
                  stableOrdering, maxDepth}, b: {...}}                               # optional
    a: {status: 200, body: "<JSON text>", error_class, pages, pages_eof, pagination_unstable, headers}
    b: {...}
    expect:
      a: COMPLETE | ABSENT | "INCOMPLETE <reason>" | "ERROR <reason>"
      b: ...
      findings:                              # when both sides are COMPLETE (default [])
        - [json_pointer, path_pattern, change_type, taxonomy or null]
        - {finding: [...], value_class: ..., member_key: ..., direction: ..., raw_only: ...}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from deltasleuth.core.types import (
    ArrayRule,
    AuthoritativeAbsence,
    CompareConfig,
    CompiledSpec,
    FieldCompare,
    FieldRule,
    KeyConfig,
    Normalizer,
    NotFoundRule,
    Observation,
    Pagination,
    Severity,
    Side,
    SideConfig,
    TargetCapabilities,
    TruncationIndicators,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFS = json.loads((REPO_ROOT / "docs/architecture/spec.schema.json").read_text())["$defs"]
_TOP_LEVEL = {"description", "key", "compare", "sides", "targets", "a", "b", "expect"}
_SIDE_MEMBERS = ("select", "map", "notFound", "success", "truncation", "pagination")


@dataclass
class GoldenCase:
    name: str
    description: str
    key: str
    spec: CompiledSpec
    capabilities: dict[Side, TargetCapabilities]
    observations: dict[Side, Observation]
    expect_states: dict[Side, tuple[str, str | None]]
    expect_findings: list[dict[str, Any]]


def load_case(path: Path) -> GoldenCase:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if unknown := set(raw) - _TOP_LEVEL:
        raise ValueError(f"{path.name}: unknown top-level members {sorted(unknown)}")
    _validate_against_schema(path.name, raw)
    key, sides, targets = raw.get("key", "K1"), raw.get("sides") or {}, raw.get("targets") or {}
    spec = CompiledSpec(
        name=path.stem,
        version="1.0.0",
        content_hash="0" * 64,
        normalizer_version="1.0.0",
        key=KeyConfig(name="code"),
        sides={s: _side(s, sides.get(s.value) or {}) for s in Side},
        compare=_compare(raw.get("compare") or {}),
    )
    expect = raw["expect"]
    findings = [
        item if isinstance(item, dict) else {"finding": item} for item in expect.get("findings") or []
    ]
    return GoldenCase(
        name=path.stem,
        description=raw["description"],
        key=key,
        spec=spec,
        capabilities={s: _capabilities(targets.get(s.value) or {}) for s in Side},
        observations={s: _observation(key, s, raw[s.value]) for s in Side},
        expect_states={s: _state(expect[s.value]) for s in Side},
        expect_findings=[{**item, "finding": tuple(item["finding"])} for item in findings],
    )


def _validate_against_schema(name: str, raw: dict[str, Any]) -> None:
    def check(instance: Any, ref: dict[str, Any], where: str) -> None:
        validator = jsonschema.Draft202012Validator({**ref, "$defs": _DEFS})
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        if errors:
            raise ValueError(f"{name}: {where} is not schema-valid: {errors[0].message}")

    if "compare" in raw:
        check(raw["compare"], {"$ref": "#/$defs/compare"}, "compare")
    for side_name, members in (raw.get("sides") or {}).items():
        for member, value in members.items():
            if member not in _SIDE_MEMBERS:
                raise ValueError(f"{name}: side member {member!r} is not used by the engine")
            check(value, _DEFS["serviceSide"]["properties"][member], f"sides.{side_name}.{member}")


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(repr(value) if isinstance(value, float) else value)


def _normalizer(item: Any) -> Normalizer:
    if isinstance(item, str):
        return Normalizer(item)
    ((name, params),) = item.items()
    return Normalizer(name, dict(params))


def _field_compare(value: Any) -> FieldCompare:
    if value in (None, "exact"):
        return FieldCompare()
    if "tolerance" in value:
        bounds = value["tolerance"]
        return FieldCompare(
            "tolerance", abs_tol=_number(bounds.get("abs")), rel_tol=_number(bounds.get("rel"))
        )
    sim = value["similarity"]
    return FieldCompare(
        "similarity", threshold=float(sim["threshold"]), method=sim.get("method", "token_set")
    )


def _compare(raw: dict[str, Any]) -> CompareConfig:
    arrays = tuple(
        ArrayRule(
            path=a["path"],
            mode=a["mode"],
            key=tuple(a.get("key") or ()),
            severity=Severity(a["severity"]) if "severity" in a else None,
            taxonomy_missing_in_b=(a.get("taxonomy") or {}).get("missing_in_b"),
            taxonomy_extra_in_b=(a.get("taxonomy") or {}).get("extra_in_b"),
        )
        for a in raw.get("arrays") or []
    )
    fields = tuple(
        FieldRule(
            path=f["path"],
            normalize=tuple(_normalizer(n) for n in f.get("normalize") or []),
            compare=_field_compare(f.get("compare")),
            severity=Severity(f["severity"]) if "severity" in f else None,
            taxonomy_code=f.get("taxonomyCode"),
        )
        for f in raw.get("fields") or []
    )
    return CompareConfig(
        arrays=arrays,
        fields=fields,
        include=tuple(raw.get("include") or ()),
        exclude=tuple(raw.get("exclude") or ()),
        value_filter=raw.get("valueFilter"),
        null_equals_absent=bool(raw.get("nullEqualsAbsent", False)),
    )


def _truncation(raw: dict[str, Any] | None) -> TruncationIndicators | None:
    if raw is None:
        return None
    return TruncationIndicators(
        cap=raw["cap"], items_path=raw.get("itemsPath"), count_path=raw.get("countPath")
    )


def _side(side: Side, raw: dict[str, Any]) -> SideConfig:
    not_found, paging = raw.get("notFound"), raw.get("pagination")
    return SideConfig(
        side=side,
        role="legacy" if side is Side.A else "new",
        target=f"target-{side.value}",
        select=raw.get("select"),
        map=raw.get("map"),
        not_found=NotFoundRule(tuple(not_found.get("status", [404])), not_found.get("when"))
        if not_found
        else None,
        success_status=tuple((raw.get("success") or {}).get("status", [200])),
        truncation=_truncation(raw.get("truncation")),
        pagination=Pagination(
            kind=paging["kind"],
            items_path=paging.get("itemsPath"),
            page_param=paging.get("pageParam"),
            size_param=paging.get("sizeParam"),
            size=paging.get("size"),
        )
        if paging
        else None,
    )


def _capabilities(raw: dict[str, Any]) -> TargetCapabilities:
    absence = raw.get("authoritativeAbsence")
    return TargetCapabilities(
        authoritative_absence=AuthoritativeAbsence(tuple(absence["status"]), tuple(absence.get("shapes", ())))
        if absence
        else None,
        absence_shapes=dict(raw.get("absenceShapes") or {}),
        truncation_indicators=_truncation(raw.get("truncationIndicators")),
        stable_ordering=bool(raw.get("stableOrdering", False)),
        max_depth=raw.get("maxDepth"),
    )


def _observation(key: str, side: Side, raw: dict[str, Any]) -> Observation:
    body = raw.get("body")
    return Observation(
        key=key,
        side=side,
        http_status=raw.get("status", 200),
        body=None if body is None else body.encode("utf-8"),
        observed_at="2026-09-24T00:00:00Z",
        headers=dict(raw.get("headers") or {}),
        error_class=raw.get("error_class"),
        pages=raw.get("pages", 1),
        pages_eof=raw.get("pages_eof", True),
        pagination_unstable=raw.get("pagination_unstable", False),
    )


def _state(text: str) -> tuple[str, str | None]:
    kind, _, reason = text.partition(" ")
    return kind, reason or None


@pytest.fixture
def load() -> Any:
    return load_case
