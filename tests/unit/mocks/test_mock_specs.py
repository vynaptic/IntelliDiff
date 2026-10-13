"""The mock specs and target registries: parseable, schema-valid, P0-only and mutually consistent.

These mirror the spec-loader rules (schema, expression compilation, notFound within the target's
authoritative statuses, truncation declared) without importing the loader.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jsonata
import pytest
import yaml
from cel_expr_python import cel
from jsonschema import Draft202012Validator

from deltasleuth.core.types import Target, TargetAuth, TargetCapabilities, TruncationIndicators
from mocks.registry import MOCKS_DIR, PAIRS, PORTS, pair_dir

TARGET_FIELDS = {f.name for f in dataclasses.fields(Target)} - {"target_id"} | {"allow_insecure_http"}
CAPABILITY_FIELDS = {f.name for f in dataclasses.fields(TargetCapabilities)}
AUTH_FIELDS = {f.name for f in dataclasses.fields(TargetAuth)}
TRUNCATION_FIELDS = {f.name for f in dataclasses.fields(TruncationIndicators)}
P0_UNSUPPORTED_TOP_LEVEL = ("domainPack", "noise", "identity", "inferredSchema", "schemaDrift", "sampling")


def _jsonpaths(spec: dict[str, Any]) -> list[str]:
    """Every RFC 9535 path the spec declares."""
    paths = [rule["path"] for rule in spec["compare"]["arrays"] + spec["compare"]["fields"]]
    for side in spec["sides"].values():
        paths.append(side["select"])
        paths += [side["truncation"][k] for k in ("itemsPath", "countPath") if k in side["truncation"]]
    security = spec.get("security", {})
    paths += [rule["path"] for rule in security.get("tokenize", [])] + security.get("clear", [])
    return paths


def test_strict_loader_rejects_duplicate_keys(tmp_path: Path, yaml_strict: Callable[[Path], Any]) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text("a: 1\nb: 2\na: 3\n")
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate"):
        yaml_strict(path)


@pytest.mark.parametrize("pair", PAIRS)
class TestSpec:
    def test_validates_against_the_schema(
        self, pair: str, specs: dict[str, Any], spec_schema: dict[str, Any]
    ) -> None:
        Draft202012Validator.check_schema(spec_schema)
        validator = Draft202012Validator(spec_schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
        errors = [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(specs[pair])]
        assert errors == []

    def test_uses_only_p0_features(self, pair: str, specs: dict[str, Any]) -> None:
        spec = specs[pair]
        assert [k for k in P0_UNSUPPORTED_TOP_LEVEL if k in spec] == []
        assert spec["canonical"] == {"mode": "tree"}
        assert spec["time"] == {"mode": "live"}
        assert spec["key"]["source"]["kind"] == "list"
        assert all("request" in side and "table" not in side for side in spec["sides"].values())

    def test_key_list_is_keys_txt(
        self, pair: str, specs: dict[str, Any], keys_files: dict[str, list[str]]
    ) -> None:
        keys = specs[pair]["key"]["source"]["keys"]
        assert keys == keys_files[pair]
        assert len(set(keys)) == len(keys)
        pattern = re.compile(specs[pair]["key"]["pattern"])
        assert all(pattern.fullmatch(k) for k in keys)

    def test_keys_txt_is_the_dataset_sample(
        self, pair: str, keys_files: dict[str, list[str]], datasets: dict[str, Any]
    ) -> None:
        assert tuple(keys_files[pair]) == datasets[pair].keys

    def test_every_jsonpath_compiles_strictly(
        self, pair: str, specs: dict[str, Any], strict_jsonpath: Any
    ) -> None:
        for path in _jsonpaths(specs[pair]):
            strict_jsonpath.compile(path)

    def test_every_map_compiles(self, pair: str, specs: dict[str, Any]) -> None:
        for side in specs[pair]["sides"].values():
            jsonata.Jsonata(side["map"])

    def test_known_diff_predicates_compile(
        self, pair: str, specs: dict[str, Any], known_diff_env: Any
    ) -> None:
        ids = [rule["id"] for rule in specs[pair].get("knownDiffs", [])]
        assert len(set(ids)) == len(ids)
        for rule in specs[pair].get("knownDiffs", []):
            known_diff_env.compile(rule["when"])
            assert "expires" not in rule, "an expiry would silently flip the ground truth one day"

    def test_request_paths_template_the_key(self, pair: str, specs: dict[str, Any]) -> None:
        for side in specs[pair]["sides"].values():
            assert side["request"]["path"].count("{{ key }}") == 1

    def test_array_and_field_paths_are_unique(self, pair: str, specs: dict[str, Any]) -> None:
        for kind in ("arrays", "fields"):
            paths = [rule["path"] for rule in specs[pair]["compare"][kind]]
            assert len(set(paths)) == len(paths), kind

    def test_sides_point_at_declared_targets(
        self, pair: str, specs: dict[str, Any], targets: dict[str, dict[str, Any]]
    ) -> None:
        assert [specs[pair]["sides"][s]["target"] for s in ("a", "b")] == [f"{pair}-a", f"{pair}-b"]
        for side in specs[pair]["sides"].values():
            caps = targets[pair][side["target"]]["capabilities"]
            assert set(side["notFound"]["status"]) <= set(caps["authoritative_absence"]["status"])

    def test_truncation_declared_per_side_and_agrees_with_targets(
        self, pair: str, specs: dict[str, Any], targets: dict[str, dict[str, Any]]
    ) -> None:
        for side in specs[pair]["sides"].values():
            trunc = side["truncation"]
            declared = targets[pair][side["target"]]["capabilities"].get("truncation_indicators")
            if declared is not None:
                assert declared == {
                    "cap": trunc["cap"],
                    "items_path": trunc["itemsPath"],
                    "count_path": trunc["countPath"],
                }


@pytest.mark.parametrize("pair", PAIRS)
class TestTargets:
    def test_parse_with_known_fields_only(self, pair: str, targets: dict[str, dict[str, Any]]) -> None:
        assert set(targets[pair]) == {f"{pair}-a", f"{pair}-b"}
        for target in targets[pair].values():
            assert set(target) <= TARGET_FIELDS
            assert set(target["capabilities"]) <= CAPABILITY_FIELDS
            assert set(target["auth"]) <= AUTH_FIELDS
            if "truncation_indicators" in target["capabilities"]:
                assert set(target["capabilities"]["truncation_indicators"]) <= TRUNCATION_FIELDS

    def test_local_plain_http_without_credentials(
        self, pair: str, targets: dict[str, dict[str, Any]]
    ) -> None:
        for target in targets[pair].values():
            url = urlsplit(target["base_url"])
            assert (url.scheme, url.hostname) == ("http", "127.0.0.1") and url.port is not None
            assert (url.path, url.query, url.fragment, url.username) == ("", "", "", None)
            assert target["auth"] == {"kind": "none"}
            assert target["allow_private_network"] is True and target["allow_insecure_http"] is True

    def test_absence_is_status_plus_declared_shape(
        self, pair: str, targets: dict[str, dict[str, Any]]
    ) -> None:
        env = cel.NewEnv(variables={"body": cel.Type.DYN, "status": cel.Type.DYN})
        for target in targets[pair].values():
            caps = target["capabilities"]
            assert caps["authoritative_absence"] == {"status": [404], "shapes": ["not_found_body"]}
            predicate = env.compile(caps["absence_shapes"]["not_found_body"])

            def accepts(body: Any, predicate: Any = predicate) -> bool:
                return predicate.eval(data={"body": body, "status": 404}).plain_value()

            assert accepts({"error": "NOT_FOUND"}) is True
            assert accepts({"message": "no route"}) is False
            assert accepts({"error": "GONE"}) is False


def test_all_ports_are_distinct(targets: dict[str, dict[str, Any]]) -> None:
    ports = [urlsplit(t["base_url"]).port for pair in PAIRS for t in targets[pair].values()]
    assert len(set(ports)) == len(ports) == 4


def test_the_combined_targets_are_the_pairs_own(
    targets: dict[str, dict[str, Any]], yaml_strict: Callable[[Path], Any]
) -> None:
    """mocks/targets.yaml holds the four entries of the pairs' files, unchanged, and ``PORTS`` names the
    port of each, as the Compose mocks service starts them."""
    combined = yaml_strict(MOCKS_DIR / "targets.yaml")["targets"]
    assert combined == targets["linker"] | targets["customer"] and len(combined) == 4
    declared = {
        pair: {name[-1]: urlsplit(target["base_url"]).port for name, target in targets[pair].items()}
        for pair in PAIRS
    }
    assert declared == PORTS
    compose = yaml.safe_load((MOCKS_DIR.parent / "deploy" / "docker-compose.yml").read_text())
    started = re.findall(
        r"mocks\.run (\w+) --variant (\w) --host \S+ --port (\d+)",
        compose["services"]["mocks"]["command"][-1],
    )
    assert {(pair, variant, int(port)) for pair, variant, port in started} == {
        (pair, variant, port) for pair, variants in PORTS.items() for variant, port in variants.items()
    } and len(started) == 4


def test_no_backend_names_anywhere_in_the_mocks() -> None:
    """Black-box rule: nothing in mocks/ refers to how a target is built."""
    forbidden = re.compile(r"janus|cassandra|gremlin|postgres|oracle", re.IGNORECASE)
    root = pair_dir("linker").parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".yaml", ".json", ".txt"}
        and forbidden.search(path.read_text())
    ]
    assert offenders == []
