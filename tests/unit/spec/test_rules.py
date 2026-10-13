"""What the loader shares with the builder in the browser (docs/mvp/CONTRACTS.md §7): which request headers
carry a credential, by one fixture that the curl import's tests read too (web/test/building.test.ts), and
that the builder's starter is a spec the loader takes once each side has its target and request."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from deltasleuth.core.spec import TargetRegistry, validate_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import Target

REFUSALS = {"schema.propertyNames", "credential_header", "inline_credential"}


def test_credential_headers_are_the_fixtures(minimal_spec: dict[str, Any], repo_root: Path) -> None:
    """A header of a request is refused, under its own pointer, exactly when the fixture calls it a
    credential: by the schema's names, by a fragment of its name or by its value."""
    cases = json.loads((repo_root / "web" / "test" / "credential-headers.json").read_text())["cases"]
    assert {case["credential"] for case in cases} == {True, False}
    for case in cases:
        header = case["header"]
        minimal_spec["sides"]["a"]["request"]["headers"] = {header: case["value"]}
        at = f"/sides/a/request/headers/{header}"
        refused = {i.code for i in validate_spec(minimal_spec) if i.pointer == at} & REFUSALS
        assert bool(refused) is case["credential"], case


def test_the_builders_starter_is_a_spec_once_its_sides_are_filled(repo_root: Path) -> None:
    """``STARTER`` in web/src/building.ts, joined with two forms as the server joins them, has no issue
    against targets that declare no capability, as the product's targets do."""
    source = (repo_root / "web" / "src" / "building.ts").read_text()
    starter = re.search(r"^export const STARTER: SpecForm = \{\n  text: `(.*?)`,\n", source, re.DOTALL | re.M)
    assert starter and "${" not in starter[1]
    doc = parse_yaml_text(starter[1].replace("\\\\", "\\"))  # a template literal's one escape
    assert doc["metadata"]["name"] == "scratch-comparison"
    assert doc["key"]["pattern"] == r"^[^\s/]{1,200}$"
    for side in ("a", "b"):
        assert not {"target", "request"} & set(doc["sides"][side])
        doc["sides"][side] |= {
            "target": f"service-{side}",
            "request": {"method": "GET", "path": "/records/{{ key }}"},
        }
    targets = TargetRegistry(
        {name: Target(name, f"https://{name}.example") for name in ("service-a", "service-b")}
    )
    assert validate_spec(doc, targets=targets) == []
