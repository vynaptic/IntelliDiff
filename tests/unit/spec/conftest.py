"""Fixtures for the spec and target-registry tests."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth.core.spec import TargetRegistry, load_spec_file, load_targets
from deltasleuth.core.types import AuthoritativeAbsence, Target, TargetCapabilities, TruncationIndicators

EXAMPLES = Path(__file__).resolve().parents[2] / "fixtures" / "specs"

_MINIMAL_SPEC: dict[str, Any] = {
    "apiVersion": "deltasleuth/v1",
    "kind": "ComparisonSpec",
    "metadata": {"name": "things-v1-vs-v2", "version": "1.0.0", "owner": "platform-team"},
    "normalizerVersion": "1.1.0",
    "key": {
        "name": "thing_id",
        "type": "string",
        "pattern": "^T[0-9]{4}$",
        "normalize": ["trim", "upper"],
        "source": {"kind": "list", "keys": ["T0001", " t0002 "]},
    },
    "sides": {
        "a": {
            "role": "legacy",
            "target": "things-v1",
            "request": {
                "method": "GET",
                "path": "/v1/things/{{ key }}",
                "headers": {"Accept": "application/json"},
            },
            "select": "$.thing",
            "notFound": {"status": [404]},
            "truncation": {"itemsPath": "$.thing.parts", "cap": 100},
        },
        "b": {
            "role": "new",
            "target": "things-v2",
            "request": {"method": "GET", "path": "/v2/things/{{ key }}"},
            "select": "$.data",
            "map": '{"id": id, "parts": [components.{"sku": code, "qty": quantity}]}',
            "truncation": {"countPath": "$.meta.total", "itemsPath": "$.data.components", "cap": 50},
        },
    },
    "compare": {
        "arrays": [{"path": "$.parts", "mode": "keyed", "key": ["sku"]}],
        "fields": [
            {"path": "$.parts[*].sku", "normalize": ["trim", "upper"]},
            {"path": "$.parts[*].qty", "compare": {"tolerance": {"abs": 0.5}}},
        ],
    },
    "recheck": {"delay": "5m", "times": 1},
}


@pytest.fixture
def minimal_spec() -> dict[str, Any]:
    """A small spec the product runs end to end (tree mode, list keys, live time)."""
    return copy.deepcopy(_MINIMAL_SPEC)


@pytest.fixture
def example() -> Callable[[str], dict[str, Any]]:
    """A fresh copy of one shipped example spec, by file stem."""
    return lambda stem: load_spec_file(EXAMPLES / f"{stem}.yaml")


@pytest.fixture
def write_yaml(tmp_path: Path) -> Callable[[str, Any], Path]:
    def write(name: str, data: Any) -> Path:
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False), "utf-8")
        return path

    return write


@pytest.fixture
def customer_targets(write_yaml: Callable[[str, Any], Path]) -> TargetRegistry:
    """Targets of the generic example: 404 is authoritative, no truncation indicators declared."""
    absence = {"authoritative_absence": {"status": [404]}}
    targets = {
        "customer-profile-v1": {
            "base_url": "https://customers-v1.retailbank.example/",
            "auth": {"kind": "bearer", "secret_ref": "file:secrets/v1.token"},
            "capabilities": absence,
        },
        "customer-profile-v2": {"base_url": "https://customers.retailbank.example", "capabilities": absence},
    }
    return load_targets(write_yaml("targets.yaml", {"targets": targets}))


@pytest.fixture
def things_targets() -> TargetRegistry:
    """Targets of ``minimal_spec``: a declares 404 authoritative, b declares truncation."""
    absence = AuthoritativeAbsence(status=(404,))
    truncation = TruncationIndicators(cap=10, items_path="$.x")
    return TargetRegistry(
        {
            "things-v1": Target("things-v1", "https://v1.example", capabilities=TargetCapabilities(absence)),
            "things-v2": Target(
                "things-v2",
                "https://v2.example",
                capabilities=TargetCapabilities(truncation_indicators=truncation),
            ),
        }
    )
