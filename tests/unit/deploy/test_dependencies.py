"""Every runtime dependency ``pyproject.toml`` declares is imported by the package: none ships unused, as an
image's and a release's size and its audit surface grow with each. The UI's oldest Node is enforced, not only
stated: CI tests and builds the UI on it, and npm refuses an older one."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _name(requirement: str) -> str:
    """A requirement's distribution name, normalised (PEP 503)."""
    return re.sub(r"[-_.]+", "-", re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0]).lower()


def _imported() -> set[str]:
    """The top-level modules the package imports, absolute imports only."""
    found: set[str] = set()
    for path in (ROOT / "deltasleuth").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def test_every_runtime_dependency_is_imported_by_the_package() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = {_name(requirement) for requirement in project["dependencies"]}
    providers = packages_distributions()  # module -> the distributions that install it
    used = {_name(dist) for module in _imported() for dist in providers.get(module, [])}
    assert declared - used == set()


def test_ci_builds_the_ui_on_the_oldest_node_its_engines_admit() -> None:
    floor = json.loads((ROOT / "web" / "package.json").read_text())["engines"]["node"]
    assert re.fullmatch(r">=\d+\.\d+\.0", floor)
    version = floor.removeprefix(">=").removesuffix(".0")
    assert f'node-version: "{version}"' in (ROOT / ".github" / "workflows" / "checks.yml").read_text()
    assert all(f"Node {version} or later" in (ROOT / name).read_text() for name in ("AGENTS.md", "README.md"))
    assert "engine-strict=true" in (ROOT / "web" / ".npmrc").read_text().splitlines()
