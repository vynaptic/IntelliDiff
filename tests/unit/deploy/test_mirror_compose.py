"""The launcher validates all mirrors before building or starting a local instance."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.release import _pins

VALUES = {
    "MIRROR_DOCKER": "registry.example.invalid:5443/dockerhub/library",
    "MIRROR_PYPI": "https://packages.example.invalid/pypi/simple",
    "MIRROR_NPM": "https://packages.example.invalid/npm",
    "MIRROR_DEBIAN": "https://packages.example.invalid/debian",
    "MIRROR_DEBIAN_SECURITY": "https://packages.example.invalid/security",
}


@pytest.fixture
def mirror(repo_root: Path, tmp_path: Path) -> Path:
    if not shutil.which("docker"):
        (pytest.fail if os.environ.get("DS_REQUIRE_TOOLS") else pytest.skip)("missing Docker Compose")
    source, deploy = repo_root / "deploy", tmp_path / "deploy"
    deploy.mkdir()
    for name in ("mirror.sh", "docker-compose.yml", "docker-compose.source.yml", "docker-compose.mirror.yml"):
        shutil.copy(source / name, deploy / name)
    shutil.copy(source / "docker-compose.site.example.yml", deploy / "docker-compose.site.yml")
    template = (source / "env.mirror.example").read_text()
    for key, value in VALUES.items():
        template = template.replace(f"\n{key}=\n", f"\n{key}={value}\n")
    (tmp_path / ".env").write_text(template)
    return tmp_path


def _compose(mirror: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "deploy/mirror.sh", *args], cwd=mirror, text=True, capture_output=True,
        env=os.environ | {"MIRROR_DOCKER": "ignored.invalid", "COMPOSE_FILE": "missing.yml"},
    )


def test_mirror_sources_and_secret_mounts(mirror: Path, repo_root: Path) -> None:
    done = _compose(mirror, "config", "--format", "json")
    assert done.returncode == 0, done.stderr
    rendered = json.loads(done.stdout)
    services = rendered["services"]
    build = services["app"]["build"]
    args = build["args"]
    python, (node,) = _pins((repo_root / "deploy/Dockerfile").read_text())
    assert args == {
        "MIRROR_BUILD": "1", **{k: v for k, v in VALUES.items() if k != "MIRROR_DOCKER"},
        "PYTHON": f"{VALUES['MIRROR_DOCKER']}/{python}", "NODE": f"{VALUES['MIRROR_DOCKER']}/{node}",
    }
    assert services["db"]["image"] == f"{VALUES['MIRROR_DOCKER']}/postgres:16-alpine"
    assert services["app"]["image"] == services["migrate"]["image"] == "deltasleuth:source"
    confidential = {"mirror_ca", "mirror_netrc", "mirror_npmrc", "mirror_apt_auth"}
    assert {s["source"] for s in build["secrets"]} == confidential
    assert not confidential.intersection(s["source"] for v in services.values() for s in v.get("secrets", []))


@pytest.mark.parametrize("key", VALUES)
def test_missing_mirror_cannot_start_the_trial(mirror: Path, key: str) -> None:
    file = mirror / ".env"
    file.write_text(file.read_text().replace(f"{key}={VALUES[key]}", f"{key}="))
    done = _compose(mirror, "trial")
    assert done.returncode and key in done.stderr
    assert not (mirror / "deploy/secrets").exists()


@pytest.mark.parametrize(
    "registry", ["dockerhub/library", "docker.io/library", "https://r.example/r", "u:p@r"],
)
def test_mirror_rejects_ambiguous_registry_before_compose(mirror: Path, registry: str) -> None:
    file = mirror / ".env"
    file.write_text(file.read_text().replace(VALUES["MIRROR_DOCKER"], registry))
    done = _compose(mirror, "build")
    assert done.returncode == 2 and "MIRROR_DOCKER" in done.stderr


def test_mirror_trial_rejects_public_source_env(mirror: Path) -> None:
    file = mirror / ".env"
    file.write_text(file.read_text().replace(":deploy/docker-compose.mirror.yml", ""))
    done = _compose(mirror, "trial")
    assert done.returncode == 2 and "env.mirror.example" in done.stderr


def test_mirror_refuses_a_credential_in_a_build_argument(mirror: Path) -> None:
    file = mirror / ".env"
    file.write_text(file.read_text().replace(VALUES["MIRROR_NPM"], "https://reader:example@r.example/npm"))
    done = _compose(mirror, "build")
    assert done.returncode == 2 and "MIRROR_NPM" in done.stderr and "reader:example" not in done.stderr


@pytest.mark.parametrize("override", [
    "export COMPOSE_FILE=other.yml", "  MIRROR_NPM=https://other.invalid",
    "MIRROR_NPM = https://other.invalid", "MIRROR_NPM: https://other.invalid",
    "export MIRROR_NPM:https://other.invalid", "MIRROR_PYPI=https://other.invalid",
    "COMPOSE_DISABLE_ENV_FILE=1",
])
def test_mirror_refuses_ambiguous_dotenv_before_startup(mirror: Path, override: str) -> None:
    with (mirror / ".env").open("a") as dotenv:
        dotenv.write(override + "\n")
    done = _compose(mirror, "trial")
    assert done.returncode == 2 and "one plain NAME=value" in done.stderr
    assert not (mirror / "deploy/secrets").exists()
