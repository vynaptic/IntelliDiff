"""What a release relies on: the declared dependencies, the gate on the DB tests, the vendor keys a release
trusts and the release keys that sign it, none of whose private keys the repository holds, and the triage
its gate reads."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

from deltasleuth import licence
from deltasleuth.security import signing
from tools import release


def test_the_llm_sdks_are_declared_at_their_httpx2_majors(repo_root: Path) -> None:
    """The adapters hand the SDKs an httpx2 client; openai<3 and anthropic<1 use httpx and reject it."""
    project = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]
    declared = {r.name: r.specifier for r in map(Requirement, project["dependencies"])}
    assert "httpx2" in declared
    assert not declared["openai"].contains("2.99") and not declared["anthropic"].contains("0.99")


@pytest.mark.parametrize(("required", "outcome"), [("", "1 skipped"), ("1", "1 error")])
def test_db_tests_skip_without_docker_unless_required(
    repo_root: Path, tmp_path: Path, required: str, outcome: str
) -> None:
    (tmp_path / "conftest.py").write_text((repo_root / "tests" / "conftest.py").read_text())
    (tmp_path / "test_db.py").write_text("def test_db(pg_server):\n    pass\n")
    env = {**os.environ, "PATH": str(tmp_path), "DS_REQUIRE_DB": required}  # no docker on PATH
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rsE", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert outcome in run.stdout and "docker is not available" in run.stdout


def test_two_vendor_keys(repo_root: Path) -> None:
    """A release trusts no vendor key, an open build, or an issuing key and a standby, so that losing
    one reissues nothing: never one alone."""
    version = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["version"]
    assert ".dev" in version or len(licence.SHIPPED_KEYS) != 1


def test_the_version_is_one_in_pyproject_compose_and_the_ca_override(repo_root: Path) -> None:
    """Compose runs the image of pyproject's version, and the CA override its -ca layer, .dev included, so a
    release loads the tag the kit runs; uv.lock names it too, or ``uv run``
    rewrites the lock as it starts the release tool, which then refuses the changed tree."""
    version = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["version"]
    for name, tag in (("docker-compose.yml", version), ("docker-compose.ca.yml", f"{version}-ca")):
        services = yaml.safe_load((repo_root / "deploy" / name).read_text())["services"]
        assert {services[service]["image"] for service in ("app", "mocks")} == {f"deltasleuth:{tag}"}
    locked = tomllib.loads((repo_root / "uv.lock").read_text())["package"]
    assert [package["version"] for package in locked if package["name"] == "deltasleuth"] == [version]


def test_release_keys_match_security_md(repo_root: Path) -> None:
    """SECURITY.md's key table mirrors tools/release-keys.txt: fingerprint, status and procedure."""
    security = repo_root / "SECURITY.md"
    if not security.exists():
        pytest.skip("SECURITY.md is not written yet")
    row = re.compile(r"\| `?([0-9a-f]{64})`? \| ([^|]+?) \| `?(?:procedure=)?([0-9a-f]{64})`? \|")
    table = {match[1]: (match[2], match[3]) for match in row.finditer(security.read_text())}
    assert table == release.release_keys()


def test_the_maintainer_checklist_is_the_heading_build_refuses(repo_root: Path) -> None:
    """build refuses while SECURITY.md holds the maintainer's checklist, by its heading: a reworded
    heading would let the checklist ship."""
    security = repo_root / "SECURITY.md"
    if not security.exists():
        pytest.skip("SECURITY.md is not written yet")
    lines = security.read_text().splitlines()
    assert [line for line in lines if line.startswith("#") and "first release" in line] in (
        [],
        [release.CHECKLIST],
    )


def test_only_section_2_holds_the_security_contact_placeholder(repo_root: Path) -> None:
    """The checklist names the placeholder as section 2's, never itself, so filling it in touches that
    section alone."""
    security = repo_root / "SECURITY.md"
    if not security.exists():
        pytest.skip("SECURITY.md is not written yet")
    sections = re.split(r"\n(?=## )", security.read_text())
    holding = [section.partition("\n")[0] for section in sections if "<SECURITY-CONTACT>" in section]
    assert holding in ([], ["## 2. Reporting a vulnerability"])


def test_one_signing_release_key(repo_root: Path) -> None:
    """build signs only with the one signing key; a version without .dev needs it."""
    version = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["version"]
    signing_keys = [fp for fp, (status, _) in release.release_keys().items() if status == "signing"]
    assert len(signing_keys) <= 1 and (".dev" in version or len(signing_keys) == 1)


def test_the_signing_key_files_this_procedure() -> None:
    """A changed procedure is a new format, filed at every site like a new key."""
    procedure = hashlib.sha256(release.PROCEDURE.encode()).hexdigest()
    assert [filed for status, filed in release.release_keys().values() if status == "signing"] in (
        [],
        [procedure],
    )


def test_release_keys_are_not_vendor_keys(repo_root: Path) -> None:
    """Releases and licences are signed by different keys: none of the list is a vendor key."""
    listed = (repo_root / "tests" / "fixtures" / "vendor-keys.txt").read_text().splitlines()
    vendor = {line.split()[0] for line in listed if line and not line.startswith("#")}
    vendor |= {signing.fingerprint(pem) for pem in licence.SHIPPED_KEYS} | set(licence.REMOVED_KEYS)
    assert not vendor & set(release.release_keys())


def test_triage_is_well_formed(repo_root: Path) -> None:
    """tools/triage.toml parses and every entry has what the gate reads; expiry is build's refusal."""
    entries, problems = release.triage((repo_root / "tools" / "triage.toml").read_text())
    assert problems == [] and set(entries) == {"finding", "vendored"}


def test_no_private_key_tracked(repo_root: Path) -> None:
    if not (repo_root / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=repo_root, capture_output=True, check=True).stdout
    tracked = [repo_root / name for name in listed.decode().split("\0") if name]
    marker = b"PRIVATE" + b" KEY-----"  # split, so this file does not hold it
    held = [path for path in tracked if path.is_file() and marker in path.read_bytes()]
    assert held == [] and not [path for path in tracked if path.name.endswith(".key.pem")]
