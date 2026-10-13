"""Fixtures for the mock-pair tests.

The tests check the mocks against their own ground truth without running the comparison engine:
they apply each spec's select + JSONata map with the same libraries the
engine uses, order keyed/set arrays the way the spec declares, and evaluate the declared CEL
shapes and known-diff rules directly.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import jsonata
import jsonpath
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from cel_expr_python import cel

from mocks.common import JSONBody, ScriptedDataset, Variant
from mocks.registry import PAIRS, load_pair, pair_dir

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "deltasleuth" / "core" / "spec" / "spec.schema.json"
STRICT_JSONPATH = jsonpath.JSONPathEnvironment(strict=True)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys, as the spec loader requires."""


def _no_duplicates(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise yaml.constructor.ConstructorError(
            None, None, f"duplicate keys {sorted(duplicates)}", node.start_mark
        )
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def load_yaml_strict(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_StrictLoader)  # noqa: S506 - _StrictLoader is a SafeLoader subclass


@pytest.fixture(scope="session")
def yaml_strict() -> Callable[[Path], Any]:
    """The duplicate-key-rejecting YAML loader (test modules cannot import conftest)."""
    return load_yaml_strict


@pytest.fixture(scope="session")
def strict_jsonpath() -> jsonpath.JSONPathEnvironment:
    """The strict RFC 9535 JSONPath environment."""
    return STRICT_JSONPATH


@pytest.fixture(scope="session")
def spec_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def specs() -> dict[str, dict[str, Any]]:
    return {name: load_yaml_strict(pair_dir(name) / "spec.yaml") for name in PAIRS}


@pytest.fixture(scope="session")
def targets() -> dict[str, dict[str, Any]]:
    return {name: load_yaml_strict(pair_dir(name) / "targets.yaml")["targets"] for name in PAIRS}


@pytest.fixture(scope="session")
def expected() -> dict[str, dict[str, Any]]:
    return {
        name: json.loads((pair_dir(name) / "expected.json").read_text(encoding="utf-8")) for name in PAIRS
    }


@pytest.fixture(scope="session")
def keys_files() -> dict[str, list[str]]:
    return {name: (pair_dir(name) / "keys.txt").read_text(encoding="utf-8").splitlines() for name in PAIRS}


@pytest.fixture(scope="session")
def datasets() -> dict[str, ScriptedDataset]:
    return {name: load_pair(name).build_dataset() for name in PAIRS}


def _unordered_paths(spec: dict[str, Any]) -> set[str]:
    return {rule["path"] for rule in spec["compare"]["arrays"] if rule["mode"] != "ordered"}


def _sort_unordered(node: Any, unordered: set[str], path: str = "$") -> Any:
    """Order the members of every keyed/set array canonically, so array order stops mattering."""
    if isinstance(node, dict):
        return {k: _sort_unordered(v, unordered, f"{path}.{k}") for k, v in node.items()}
    if isinstance(node, list):
        items = [_sort_unordered(v, unordered, f"{path}[*]") for v in node]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True)) if path in unordered else items
    return node


@pytest.fixture(scope="session")
def mapped(specs: dict[str, dict[str, Any]]) -> Callable[[str, Variant, JSONBody], Any]:
    """``mapped(pair, variant, body)`` -> the spec's canonical document for that body.

    Select + JSONata map exactly as the spec declares them, with keyed/set arrays ordered
    canonically. Bodies go through JSON text first, so the maps see what a client would parse.
    """
    compiled = {
        name: {
            side: (
                STRICT_JSONPATH.compile(spec["sides"][side]["select"]),
                jsonata.Jsonata(spec["sides"][side]["map"]),
            )
            for side in ("a", "b")
        }
        for name, spec in specs.items()
    }
    unordered = {name: _unordered_paths(spec) for name, spec in specs.items()}

    def apply(pair: str, variant: Variant, body: JSONBody) -> Any:
        select, mapping = compiled[pair][variant]
        (node,) = select.findall(json.loads(json.dumps(body)))
        return _sort_unordered(mapping.evaluate(node), unordered[pair])

    return apply


def _cel_env(names: tuple[str, ...]) -> Any:
    return cel.NewEnv(variables={name: cel.Type.DYN for name in names})


@pytest.fixture(scope="session")
def absence_shape(targets: dict[str, dict[str, Any]]) -> Callable[[str, str, JSONBody], bool]:
    """``absence_shape(pair, target_id, body)`` -> whether the target's declared shapes accept body."""
    env = _cel_env(("body", "status"))

    def check(pair: str, target_id: str, body: JSONBody) -> bool:
        caps = targets[pair][target_id]["capabilities"]
        predicates = [caps["absence_shapes"][name] for name in caps["authoritative_absence"]["shapes"]]
        return all(env.compile(p).eval(data={"body": body, "status": 404}).plain_value() for p in predicates)

    return check


@pytest.fixture(scope="session")
def known_diff_env() -> Any:
    """A CEL environment with the knownDiff variables the rules in the mock specs use."""
    return _cel_env(("key", "path", "pointer", "change_type", "direction", "a", "b", "value_class"))


@pytest.fixture
async def serve() -> AsyncIterator[Callable[..., Any]]:
    """``await serve(pair, variant, dataset=None)`` -> a TestClient on 127.0.0.1, closed afterwards."""
    clients: list[TestClient[Any, Any]] = []

    async def start(
        pair: str, variant: Variant, dataset: ScriptedDataset | None = None
    ) -> TestClient[Any, Any]:
        client: TestClient[Any, Any] = TestClient(TestServer(load_pair(pair).create_app(variant, dataset)))
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()
