"""compile_spec: dataclasses built faithfully, canonical path text, hashing and immutability."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from typing import Any

import pytest

from deltasleuth.core.spec import SpecError, TargetRegistry, compile_spec
from deltasleuth.core.types import (
    ArrayRule,
    ExecutionConfig,
    FieldCompare,
    FieldRule,
    Normalizer,
    NotFoundRule,
    Pagination,
    RequestTemplate,
    RetryPolicy,
    SecurityPolicy,
    Side,
    TokenizeRule,
    TruncationIndicators,
)


def test_minimal_spec(minimal_spec: dict[str, Any], things_targets: TargetRegistry) -> None:
    compiled = compile_spec(minimal_spec, targets=things_targets)
    a, b = compiled.sides[Side.A], compiled.sides[Side.B]
    assert a.request == RequestTemplate(
        method="GET", path="/v1/things/{{ key }}", headers={"Accept": "application/json"}
    )
    assert (a.not_found, b.not_found) == (NotFoundRule(status=(404,)), None)  # b: a 404 is side state ERROR
    assert (a.map, b.map) == (None, minimal_spec["sides"]["b"]["map"])
    assert b.truncation == TruncationIndicators(
        cap=50, items_path="$.data.components", count_path="$.meta.total"
    )
    assert compiled.compare.arrays == (ArrayRule(path="$.parts", mode="keyed", key=("sku",)),)
    assert compiled.compare.fields[1] == FieldRule(
        path="$.parts[*].qty",
        compare=FieldCompare(mode="tolerance", abs_tol=Decimal("0.5"), rel_tol=Decimal(0)),
    )
    assert (compiled.security, compiled.execution, compiled.known_diffs) == (
        SecurityPolicy(),
        ExecutionConfig(),
        (),
    )
    expected_hash = hashlib.sha256(
        json.dumps(minimal_spec, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    assert compiled.content_hash == expected_hash
    assert compiled.domain_pack is None


def test_rule_paths_are_stored_in_canonical_pattern_text(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["compare"].update(include=["$['parts']", "$.id"], exclude=["$.parts.*['internal note']"])
    minimal_spec["compare"]["arrays"][0]["path"] = "$['parts']"
    minimal_spec["compare"]["fields"][0]["path"] = '$["parts"][*]["sku"]'
    minimal_spec["security"] = {
        "tokenize": [{"path": "$['parts'][*].sku", "class": "identifier"}],
        "clear": ["$['id']"],
        "unlisted": "clear",
    }
    a = minimal_spec["sides"]["a"]
    a["select"] = "$['thing']"
    a["truncation"]["itemsPath"] = "$['thing'].parts"
    compiled = compile_spec(minimal_spec)
    assert (compiled.compare.arrays[0].path, compiled.compare.fields[0].path) == ("$.parts", "$.parts[*].sku")
    assert (compiled.compare.include, compiled.compare.exclude) == (
        ("$.parts", "$.id"),
        ("$.parts[*]['internal note']",),
    )
    assert compiled.security == SecurityPolicy(
        tokenize=(TokenizeRule("$.parts[*].sku", "identifier"),), clear=("$.id",), unlisted="clear"
    )
    side = compiled.sides[Side.A]
    assert (side.select, side.truncation.items_path) == ("$.thing", "$.thing.parts")
    # The raw document keeps what the author wrote.
    assert compiled.raw["compare"]["arrays"][0]["path"] == "$['parts']"


def test_pagination_success_and_field_rules(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["sides"]["b"].update(
        pagination={
            "kind": "cursor",
            "itemsPath": "$['data'].components",
            "cursorPath": "$.meta.next",
            "cursorParam": "after",
            "maxPages": 20,
        },
        success={"status": [200, 203]},
    )
    minimal_spec["compare"]["fields"] = [
        {
            "path": "$.price",
            "normalize": [{"decimal": {"scale": 2, "rounding": "half_up"}}],
            "severity": "HIGH",
        },
        {
            "path": "$.address",
            "normalize": ["nfkc", "address_std"],
            "compare": {"similarity": {"threshold": 0.9}},
            "taxonomyCode": "VALUE_MISMATCH",
        },
        {"path": "$.ratio", "compare": {"tolerance": {"rel": 0.001}}},
    ]
    compiled = compile_spec(minimal_spec)
    b = compiled.sides[Side.B]
    assert b.pagination == Pagination(
        kind="cursor",
        items_path="$.data.components",
        cursor_path="$.meta.next",
        cursor_param="after",
        max_pages=20,
    )
    assert b.success_status == (200, 203)
    price, address, ratio = compiled.compare.fields
    assert price.normalize == (Normalizer("decimal", {"scale": 2, "rounding": "half_up"}),)
    assert price.severity is not None and price.severity.value == "HIGH"
    assert (address.compare, address.taxonomy_code) == (
        FieldCompare(mode="similarity", threshold=0.9),
        "VALUE_MISMATCH",
    )
    assert ratio.compare == FieldCompare(mode="tolerance", abs_tol=Decimal(0), rel_tol=Decimal("0.001"))


def test_inactive_known_diffs_are_not_applied(minimal_spec: dict[str, Any]) -> None:
    base = {"classification": "accepted", "reason": "documented difference", "ticket": "THING-9"}
    minimal_spec["knownDiffs"] = [
        {"id": "KD-1", "when": "path == '$.id'", **base},
        {"id": "KD-2", "when": "true", "active": False, **base},
        {"id": "KD-3", "when": "false", "active": True, "expires": "2027-01-01", **base},
    ]
    compiled = compile_spec(minimal_spec)
    assert [(r.rule_id, r.expires) for r in compiled.known_diffs] == [("KD-1", None), ("KD-3", "2027-01-01")]


@pytest.mark.parametrize(
    ("retries", "expected"),
    [
        (5, RetryPolicy(max=5)),
        ({"max": 3}, RetryPolicy(max=3)),
        (
            {"max": 1, "backoff": {"initial": "100ms", "multiplier": 3}, "retryOn": ["timeout"]},
            RetryPolicy(max=1, initial_s=0.1, max_s=4.0, multiplier=3.0, retry_on=("timeout",)),
        ),
    ],
)
def test_retries(minimal_spec: dict[str, Any], retries: Any, expected: RetryPolicy) -> None:
    minimal_spec["execution"] = {"retries": retries}
    assert compile_spec(minimal_spec).execution.retries == expected


def test_execution_durations_and_breaker(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["execution"] = {
        "concurrency": 3,
        "delay": "1.5s",
        "timeouts": {"read": "2m"},
        "circuitBreaker": {"errorRate": {"rate": 0.5, "window": 20}, "cooldown": "90s"},
    }
    assert compile_spec(minimal_spec).execution == ExecutionConfig(
        concurrency=3,
        delay_s=1.5,
        read_timeout_s=120.0,
        breaker_error_rate=0.5,
        breaker_window=20,
        breaker_cooldown_s=90.0,
    )


def test_content_hash_ignores_key_order(minimal_spec: dict[str, Any]) -> None:
    first = compile_spec(minimal_spec).content_hash
    assert compile_spec(dict(reversed(list(minimal_spec.items())))).content_hash == first
    minimal_spec["metadata"]["version"] = "1.0.1"
    assert compile_spec(minimal_spec).content_hash != first


def test_compiled_spec_does_not_share_state_with_the_input(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["sides"]["b"]["request"] = {"method": "POST", "path": "/v2/q", "body": {"id": "{{ key }}"}}
    snapshot = copy.deepcopy(minimal_spec)
    compiled = compile_spec(minimal_spec)
    assert minimal_spec == snapshot  # compile never mutates its input
    minimal_spec["sides"]["b"]["request"]["body"]["id"] = "changed"
    minimal_spec["metadata"]["name"] = "changed"
    assert compiled.sides[Side.B].request.body == {"id": "{{ key }}"}
    assert compiled.raw["metadata"]["name"] == "things-v1-vs-v2"


def test_spec_error_lists_every_blocking_issue(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["sides"]["b"]["map"] = "{"
    minimal_spec["key"]["pattern"] = "("
    with pytest.raises(SpecError) as exc:
        compile_spec(minimal_spec)
    assert {(i.pointer, i.code) for i in exc.value.issues} == {
        ("/key/pattern", "invalid_key_pattern"),
        ("/sides/b/map", "invalid_jsonata"),
    }
    assert "/key/pattern [invalid_key_pattern]" in str(exc.value) and "/sides/b/map [invalid_jsonata]" in str(
        exc.value
    )
    with pytest.raises(SpecError) as exc:
        compile_spec("apiVersion: deltasleuth/v1")
    assert exc.value.issues[0].code == "document_not_mapping"
