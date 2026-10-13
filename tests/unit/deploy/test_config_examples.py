"""The shipped deployment's own files: the config examples start a trial as copied and load as the image
loads them with their office lines switched on, serving the mock specs and the chat, and the image and the
Compose files keep secrets and state safe and run the release pinned and hardened."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from fnmatch import fnmatch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth.agent.loop import DATA_CLASSES
from deltasleuth.config import McpSettings, Settings, SettingsError, load_settings
from deltasleuth.context import init_workspace
from deltasleuth.core.spec import compile_spec, load_spec_file, load_targets
from deltasleuth.llm.model import EgressDenied
from deltasleuth.llm.policy import EgressPolicy, check_egress
from deltasleuth.llm.profiles import load_profiles
from mocks.registry import PAIRS, pair_dir
from tests.unit.deploy.test_up_script import OWN_SITE
from tools import release

P0_DIR = "/var/lib/deltasleuth/p0"
SPILL_DIR = "/var/lib/deltasleuth/spill"  # a spill volume's mount point
PINNED = re.compile(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}")  # name:tag@sha256:<64 hex>
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
BASE, SITE, SITE_EXAMPLE = "docker-compose.yml", "docker-compose.site.yml", "docker-compose.site.example.yml"
SOURCE_BUILD = f"deploy/{BASE}:deploy/{SITE}:deploy/docker-compose.source.yml"  # .env's, for a source build
MARK = "## ---- optional blocks"  # the settings example: its required head, then the optional blocks
TRIAL, OFFICE = (
    "http://localhost:8000",
    "https://deltasleuth.bank.example",
)  # the example's public_url; an office's
TABLES = ("examples/tables/README.md", "examples/tables/compose.yaml")  # the table demo's layering
HARDENED = {  # docker-compose.yml's x-release, less the image
    "pull_policy": "never",
    "read_only": True,
    "tmpfs": ["/tmp:size=64m,mode=1777,noexec,nosuid,nodev"],  # noqa: S108
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
}


@pytest.fixture
def config(repo_root: Path, tmp_path: Path) -> Path:
    """deploy/config with the three examples copied to their real names, as an installation holds them."""
    for name in ("deltasleuth", "targets", "llm_profiles"):
        shutil.copy(repo_root / "deploy" / "config" / f"{name}.example.yaml", tmp_path / f"{name}.yaml")
    return tmp_path


def on(text: str) -> str:
    """The example's one rule: a line that starts "# " is a setting switched off, and removing those two
    characters switches it on."""
    return re.sub(r"(?m)^# ", "", text)


def office(head: str) -> str:
    """The head of the settings example as an office deploys it: its lines on, and an https public_url."""
    assert head.count(f"  public_url: {TRIAL} ") == 1  # the text deploy/up.sh edits in a copy
    return on(head).replace(f"  public_url: {TRIAL} ", f"  public_url: {OFFICE} ")


def _serves_the_mock_specs(settings: Settings) -> None:
    targets = load_targets(settings.targets_file)
    for pair in PAIRS:
        assert compile_spec(load_spec_file(pair_dir(pair) / "spec.yaml"), targets=targets).name


def test_the_examples_start_a_trial_as_copied(repo_root: Path, config: Path) -> None:
    """As copied: token sign-in at http://localhost:8000, no proxy trusted, AI off, and no secret beyond the
    four the base compose file mounts."""
    settings = load_settings(config / "deltasleuth.yaml")
    assert settings.auth is not None and settings.auth_mode == "tokens"
    assert (settings.auth.public_url, settings.auth.trusted_proxies) == (TRIAL, ())
    assert (settings.llm_profiles_file, settings.packs, settings.notify, settings.mcp) == (None,) * 4
    _serves_the_mock_specs(settings)
    text = (config / "deltasleuth.yaml").read_text()
    named = set(re.findall(r"^[^#\n]*file:/run/secrets/(\w+)", text, re.M))
    mounted = yaml.safe_load((repo_root / "deploy" / BASE).read_text())["services"]["app"]["secrets"]
    assert named == set(mounted) == {"db_dsn", "kek", "tokeniser"}
    assert text.count(f"  public_url: {TRIAL} ") == 1


def test_one_rule_switches_every_setting_on(config: Path) -> None:
    """Every comment line of the settings example is a setting switched off ("# ") or prose ("##"), so one
    rule serves people and tests: with every "# " removed, the whole file loads, as an office's."""
    path = config / "deltasleuth.yaml"
    text = path.read_text()
    assert [
        line for line in text.splitlines() if line.startswith("#") and not line.startswith(("# ", "##"))
    ] == []
    path.write_text(office(text))
    settings = load_settings(path)
    assert settings.auth_mode == "oidc" and settings.llm_profiles_file is not None
    assert settings.llm_profile in load_profiles(settings.llm_profiles_file).profiles
    blocks = (settings.packs, settings.notify, settings.readiness, settings.mcp)
    assert None not in blocks and settings.files.memory == "2GB"
    assert settings.llm_external_data_classes == frozenset()  # prose, which no rule switches on


def test_the_office_lines_load_when_uncommented(config: Path) -> None:
    """The head's commented lines, switched on by the rule, are an office's: the proxy and the IdP."""
    path = config / "deltasleuth.yaml"
    head, mark, optional = path.read_text().partition(MARK)
    path.write_text(office(head) + mark + optional)
    settings = load_settings(path)
    assert settings.auth is not None and settings.auth.oidc is not None and settings.auth_mode == "oidc"
    assert (settings.auth.public_url, settings.auth.trusted_proxies) == (OFFICE, ("172.30.0.1/32",))
    assert settings.auth.oidc.client_secret_ref == "file:/run/secrets/oidc_client_secret"  # noqa: S105
    assert settings.auth.oidc.roles["admin"] == ("DS.Admin",) and len(settings.auth.oidc.roles) == 6
    assert (settings.llm_profiles_file, settings.packs) == (None, None)  # the optional blocks stay off
    _serves_the_mock_specs(settings)


def test_only_the_example_chat_profile_may_receive_what_the_chat_sends(config: Path) -> None:
    path = config / "deltasleuth.yaml"
    path.write_text(on(path.read_text()))  # the three llm_ lines among them
    settings = load_settings(path)
    assert settings.llm_profiles_file is not None
    profiles = load_profiles(settings.llm_profiles_file)
    check_egress(
        profiles.get(str(settings.llm_profile)), DATA_CLASSES, EgressPolicy(settings.llm_environment)
    )
    for external in ("claude", "openai"):  # they serve the probe only, in eval
        with pytest.raises(EgressDenied):
            check_egress(profiles.get(external), DATA_CLASSES, EgressPolicy("eval"))


def test_the_example_gateway_auth_lines_load_when_uncommented(config: Path) -> None:
    path = config / "llm_profiles.yaml"
    path.write_text(re.sub(r"# (auth_(?:header|scheme):)", r"\1", path.read_text()))
    assert load_profiles(path).get("gateway").auth == ("api-key", "Bearer")


def test_the_example_optional_blocks_load_when_uncommented(config: Path) -> None:
    """Every optional block of the settings example, and the files target, loads as it is written, under
    the office's head: on a trial's the packs block, which needs single sign-on, says so."""
    path = config / "deltasleuth.yaml"
    head, mark, optional = path.read_text().partition(MARK)
    commented = load_settings(path)
    assert mark and commented.notify is None and commented.mcp is None  # nothing of them is on
    path.write_text(head + mark + on(optional))
    with pytest.raises(SettingsError, match=r"packs: needs auth\.oidc \(packs certify sign-offs"):
        load_settings(path)
    path.write_text(office(head) + mark + on(optional))
    settings = load_settings(path)
    assert settings.llm_profiles_file is not None
    assert settings.llm_profile in load_profiles(settings.llm_profiles_file).profiles
    assert settings.packs is not None and settings.packs.installation == "prod-eu-1"
    assert settings.notify is not None and sorted(settings.notify.channels) == ["ops-hook", "owners-mail"]
    assert settings.readiness is not None and settings.readiness.programmes[0].name == "cutover-q4"
    assert (settings.precheck.distinct_max_rows, settings.files.memory) == (20_000_000, "2GB")
    assert settings.mcp == McpSettings(("aggregates", "production_tokenised"), http=True)
    targets = config / "targets.yaml"
    assert [load_targets(targets).get(name).engine for name in ("billing-mysql", "ledger-oracle")] == [
        "mysql", "oracle",
    ]  # fmt: skip
    targets.write_text(
        re.sub(r"(?m)^  # (  )?(legacy-exports:|engine: files|root: )", r"  \1\2", targets.read_text())
    )
    assert load_targets(targets).get("legacy-exports").engine == "files"


def test_example_targets_append_under_the_deployment_targets(repo_root: Path, config: Path) -> None:
    """The examples' READMEs: append the entries under the deployment's ``targets:``."""
    with (config / "targets.yaml").open("a") as deployed:
        for example in (repo_root / "examples" / "public").glob("*/targets.yaml"):
            deployed.write(example.read_text().split("\ntargets:\n", 1)[1])
    assert {"swapi-dev", "rickandmorty-graphql", "linker-a"} <= set(load_targets(config / "targets.yaml"))


def _ignored(repo_root: Path, name: str) -> bool:
    """Whether .gitignore covers ``name`` at the repository root (its plain glob lines)."""
    lines = (repo_root / ".gitignore").read_text().splitlines()
    return any(fnmatch(name, line.lstrip("/")) for line in lines if line and not line.endswith("/"))


def test_a_key_backup_or_dump_in_the_checkout_is_git_ignored(repo_root: Path) -> None:
    assert [n for n in ("tokeniser.key.backup", "ds-2026-09-24.sql") if not _ignored(repo_root, n)] == []


def test_compose_gives_the_cli_a_writable_p0_volume(repo_root: Path) -> None:
    compose = yaml.safe_load((repo_root / "deploy" / "docker-compose.yml").read_text())
    assert f"p0:{P0_DIR}" in compose["services"]["app"]["volumes"] and "p0" in compose["volumes"]
    dockerfile = (repo_root / "deploy" / "Dockerfile").read_text()
    assert P0_DIR in re.search(r"install -d -o deltasleuth(?:\\\n|[^\n])*", dockerfile)[0]  # type: ignore[index]


def test_the_app_can_name_its_machine(repo_root: Path) -> None:
    """An endpoint on the machine DeltaSleuth runs on is declared as host.docker.internal: Docker Desktop
    resolves the name by itself, Linux through this line; the mocks call no one."""
    services = yaml.safe_load((repo_root / "deploy" / BASE).read_text())["services"]
    assert services["app"]["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert not [name for name in ("db", "mocks") if "extra_hosts" in services[name]]


def test_the_image_holds_a_mount_point_for_a_spill_volume(repo_root: Path) -> None:
    """The file reader's spill may have a volume of its own. Docker makes a new volume mounted where the
    image holds no directory root's, which the service (uid 10001, every capability dropped) cannot write:
    the reader then does not start. So the image creates the mount point for the service's user, whose owner
    a new named volume takes; a site file mounts the volume there and the settings name that path as
    files.spill_dir."""
    dockerfile = (repo_root / "deploy" / "Dockerfile").read_text()
    made = re.search(r"install -d -o deltasleuth -m 0700(?:\\\n|[^\n])*", dockerfile)[0]  # type: ignore[index]
    assert SPILL_DIR in made.split()


def _stages(dockerfile: str) -> list[str]:
    """The Dockerfile's build stages, in order, each from its FROM line."""
    return ["FROM " + stage for stage in re.split(r"^FROM ", dockerfile, flags=re.M)[1:]]


def _sent(dockerignore: str, path: str) -> bool:
    """Whether a build sends ``path`` under these .dockerignore lines: the last line matching the path or a
    folder above it decides, and a ``!`` line sends."""
    sent, parts = True, path.split("/")
    for line in dockerignore.splitlines():
        if line and not line.startswith("#"):
            if any(fnmatch("/".join(parts[:n]), line.removeprefix("!")) for n in range(1, len(parts) + 1)):
                sent = line.startswith("!")
    return sent


def _version(repo_root: Path) -> str:
    return tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["version"]


def test_dockerfile_pins_every_base_by_digest(repo_root: Path) -> None:
    """Every FROM, in any case, names its base by digest, directly or through a pinned ARG default, and every
    COPY --from or RUN --mount from= names a stage; the release tool knows the python index and one manifest
    per platform, which a release builds from instead."""
    dockerfile = (repo_root / "deploy" / "Dockerfile").read_text()
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", dockerfile.split("\nFROM ", 1)[0], re.M))
    froms = re.findall(r"^FROM (?:--platform=\S+ )?(\S+)", dockerfile, re.M | re.I)
    bases = [args.get(base.strip("${}"), base) for base in froms]
    assert len(bases) == 3 and [base for base in bases if not PINNED.fullmatch(base)] == []
    stages = {name.lower() for name in re.findall(r"^FROM .* AS (\S+)$", dockerfile, re.M | re.I)}
    named = re.findall(r"^(?:COPY .*--from=|RUN .*[ ,=]from=)([^\s,]+)", dockerfile, re.M | re.I)
    assert named and {name.lower() for name in named} <= stages
    assert release._pins(dockerfile) == (args["PYTHON"], [bases[0]])  # the tool reads it so: node is local
    assert args["PYTHON"].partition("@")[2] == release.INDEX
    assert set(release.BASES) == {"linux/amd64", "linux/arm64"}
    digests = {release.INDEX, *release.BASES.values()}
    assert len(digests) == 3 and all(re.fullmatch(r"sha256:[0-9a-f]{64}", digest) for digest in digests)


def test_runtime_stage_holds_no_installer(repo_root: Path) -> None:
    """uv installs the environment in a build stage; the runtime stage takes only /app from it, uninstalls
    pip and runs as a numeric user."""
    stages = _stages((repo_root / "deploy" / "Dockerfile").read_text())
    venv, runtime = next(stage for stage in stages if " AS venv\n" in stage), stages[-1]
    assert re.findall(r"pip install [^\n]*", venv) == [
        "pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/uv-requirements.txt"
    ]
    assert not re.search(r"\buvx?\b|pip install", runtime) and "python -m pip uninstall --yes" in runtime
    assert re.findall(r"^COPY --from=venv (\S+)", runtime, re.M) == ["/app"]
    assert re.findall(r"^USER (.*)", runtime, re.M) == ["10001:10001"]


def test_uv_requirements_reach_the_build(repo_root: Path) -> None:
    """The build installs uv from a pin with a hash per manylinux wheel, x86_64 and aarch64, in a file the
    build context sends, like every other file the Dockerfile copies; no secret or site file is sent."""
    deploy = repo_root / "deploy"
    lines = (deploy / "uv-requirements.txt").read_text().splitlines()
    requirement = " ".join(line.removesuffix("\\") for line in lines if not line.startswith("#")).split()
    assert re.fullmatch(r"uv==\d+\.\d+\.\d+", requirement[0]) and len(requirement) == len(set(requirement))
    assert len(requirement) >= 3 and all(HASH.fullmatch(pin) for pin in requirement[1:])
    dockerfile, ignore = (deploy / "Dockerfile").read_text(), (deploy / "Dockerfile.dockerignore").read_text()
    assert "COPY deploy/uv-requirements.txt /tmp/\n" in dockerfile
    copied = [s for c in re.findall(r"^COPY (?!--from)(.+) \S+$", dockerfile, re.M) for s in c.split()]
    assert "deploy/uv-requirements.txt" in copied and "web/package-lock.json" in copied
    assert [source for source in copied if not _sent(ignore, source.rstrip("/"))] == []
    site = [".env", "deploy/secrets/kek", "deploy/config/deltasleuth.yaml", "deploy/docker-compose.site.yml"]
    assert [path for path in site if _sent(ignore, path)] == []


def test_the_image_carries_the_licence_text(repo_root: Path) -> None:
    """Apache-2.0 section 4(a): the venv stage copies LICENSE before it installs the project, so the
    package's dist-info holds the text (hatchling leaves it out, silently, when the file is absent), and a
    release builds from an archive that holds it."""
    stages = _stages((repo_root / "deploy" / "Dockerfile").read_text())
    venv = next(stage for stage in stages if " AS venv\n" in stage)
    before, installs, _ = venv.partition("RUN uv sync --frozen --no-dev --no-cache\n")
    assert installs and "COPY pyproject.toml uv.lock README.md LICENSE ./\n" in before
    assert "LICENSE" in release.SHIPPED and (repo_root / "LICENSE").is_file()


def test_compose_runs_the_release_hardened(repo_root: Path) -> None:
    """app and mocks run the loaded release under the hardening, never building or pulling it; db is as it
    was, and the optional secrets live in the site file."""
    text = (repo_root / "deploy" / BASE).read_text()
    compose = yaml.safe_load(text)
    for name in ("app", "mocks"):
        service = compose["services"][name]
        assert {key: service.get(key) for key in HARDENED} == HARDENED and "build" not in service
        assert service["image"] == f"deltasleuth:{_version(repo_root)}"
    db = compose["services"]["db"]
    assert db["image"] == "postgres:16-alpine" and not set(HARDENED) & set(db)
    assert not re.search(r"--build|\bbuild:|x-build", text)
    assert compose["services"]["app"]["secrets"] == ["db_dsn", "kek", "tokeniser"]
    assert set(compose["secrets"]) == {"db_password", "db_dsn", "kek", "tokeniser"}


def test_site_example_adds_no_secret_until_its_lines_are_on(repo_root: Path) -> None:
    """As copied the site example changes nothing: token sign-in with AI off needs no secret beyond the base
    file's four. After the edit its comments document (the two names in app.secrets, and the "# " of three
    lines removed), base and site mount what the base file alone mounted before the split: Compose appends
    an override's lists to the base file's and merges its maps."""
    text = (repo_root / "deploy" / SITE_EXAMPLE).read_text()
    assert yaml.safe_load(text) == {"services": {"app": {"secrets": []}}}
    edited = re.sub(
        r"(?m)^# (secrets:|  \w+: \{file: )",
        r"\1",
        text.replace("secrets: []", "secrets: [oidc_client_secret, llm_key]"),
    )
    base, site = yaml.safe_load((repo_root / "deploy" / BASE).read_text()), yaml.safe_load(edited)
    assert set(site) == {"services", "secrets"} and set(site["services"]) == {"app"}  # the rest is commented
    secrets = base["services"]["app"]["secrets"] + site["services"]["app"]["secrets"]
    assert secrets == ["db_dsn", "kek", "tokeniser", "oidc_client_secret", "llm_key"]
    files = {name: secret["file"] for name, secret in (base["secrets"] | site["secrets"]).items()}
    assert files == {name: f"./secrets/{name}" for name in ("db_password", *secrets)}


def test_the_source_override_builds_once_and_tags_by_project(repo_root: Path) -> None:
    """One build, the app's, which mocks runs too, under a tag that carries the Compose project's name, so
    two checkouts on one machine share no image. The default names the base
    file's project, written out: Compose is not relied on to hand a file's ``name:`` to interpolation."""
    base, source = (
        yaml.safe_load((repo_root / "deploy" / name).read_text())
        for name in (BASE, "docker-compose.source.yml")
    )
    services = source["services"]
    assert set(services) == {"app", "mocks"} and "build" not in services["mocks"]
    assert services["app"]["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile"}
    tag = "${COMPOSE_PROJECT_NAME:-deltasleuth}:source"
    assert [services[name]["image"] for name in ("app", "mocks")] == [tag, tag] and base[
        "name"
    ] == "deltasleuth"


def test_the_source_override_shows_ds_the_checkouts_mocks_and_examples(repo_root: Path) -> None:
    """A source build's app sees the checkout's mocks/ and examples/ read-only where the image's working
    directory resolves their repository paths, so ``ds run mocks/customer/spec.yaml`` under the user guide's
    alias reads the checkout's file. Only the override says so: the base file, which a release runs, gives
    the app no such mount."""
    base, source = (
        yaml.safe_load((repo_root / "deploy" / name).read_text())
        for name in (BASE, "docker-compose.source.yml")
    )
    assert source["services"]["app"]["volumes"] == ["../mocks:/app/mocks:ro", "../examples:/app/examples:ro"]
    assert "volumes" not in source["services"]["mocks"]  # the base file's own mount of mocks/
    assert [volume.split(":")[1] for volume in base["services"]["app"]["volumes"]] == [
        "/etc/deltasleuth",
        "/var/lib/deltasleuth/evidence",
        "/var/lib/deltasleuth/workspace",
        P0_DIR,
    ]
    runtime = _stages((repo_root / "deploy" / "Dockerfile").read_text())[-1]
    assert "\nWORKDIR /app\n" in runtime and "mocks" not in runtime and "examples" not in runtime
    # Relative to deploy/, the folder of the first compose file: the checkout's own, with the bundled specs.
    for bundled in ("mocks/customer/spec.yaml", "mocks/linker/spec.yaml", "examples/tables/spec.yaml"):
        assert (repo_root / "deploy" / ".." / bundled).is_file()


def test_the_env_example_is_the_source_builds(repo_root: Path) -> None:
    """deploy/env.source.example is .env for a source build as copied: its one active line names the base
    file, the site file and the source override, and it is that line's one source, since deploy/up.sh holds
    no compose file list."""
    lines = (repo_root / "deploy" / "env.source.example").read_text().splitlines()
    assert [line for line in lines if not line.startswith("#")] == [f"COMPOSE_FILE={SOURCE_BUILD}"]
    assert "# COMPOSE_PROJECT_NAME=deltasleuth2" in lines  # a second installation's, switched off
    assert "docker-compose.yml:" not in (repo_root / "deploy" / "up.sh").read_text()
    assert _ignored(repo_root, ".env") and not _ignored(repo_root, "deploy/env.source.example")


def _rendered(folder: Path) -> tuple[dict[str, Any], set[str]]:
    """``docker compose --profile mocks config`` in ``folder``, through its .env: the document, and its lines
    without their indentation, as deploy/up.sh reads them. Compose renders without a daemon, and is given
    the address of none."""
    env = {name: value for name, value in os.environ.items() if not name.startswith(("COMPOSE_", "DOCKER_"))}
    env["DOCKER_HOST"] = "tcp://127.0.0.1:9"
    render = ["docker", "compose", "--profile", "mocks", "config"]
    done = subprocess.run(render, cwd=folder, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return yaml.safe_load(done.stdout), {line.strip() for line in done.stdout.splitlines()}


def _binds(rendered: dict[str, Any], folder: Path) -> dict[str, tuple[str, bool]]:
    """The host folders the rendered app sees: each mount point with its folder below ``folder`` and whether
    it is read-only."""
    return {
        volume["target"]: (str(Path(volume["source"]).relative_to(folder.resolve())), volume["read_only"])
        for volume in rendered["services"]["app"]["volumes"]
        if volume["type"] == "bind"
    }


def _owned(rendered: dict[str, Any]) -> set[str]:
    """What an installation must not share with another on its machine: its image, port, network, subnet
    and volumes."""
    app, network = rendered["services"]["app"], rendered["networks"]["default"]
    volumes = {volume["name"] for volume in rendered["volumes"].values()}
    subnets = {config["subnet"] for config in network["ipam"]["config"]}
    return {app["image"], network["name"], *volumes, *subnets, *(port["published"] for port in app["ports"])}


def test_the_examples_render_in_compose_as_copied(repo_root: Path, tmp_path: Path) -> None:
    """The compose files with the site and .env examples copied render, in Compose itself, as project
    deltasleuth on port 8000 running one build, deltasleuth:source; with a project line in .env and the
    site file deploy/up.sh writes for another port and subnet, as a second installation that shares
    nothing with the first. Both hold the lines deploy/up.sh reads, and their
    app sees the checkout's mocks/ and examples/ read-only in /app; a release's .env, which names no
    source override, renders the release image with the settings as the app's one host folder."""
    if shutil.which("docker") is None:
        (pytest.fail if os.environ.get("DS_REQUIRE_TOOLS") else pytest.skip)("missing docker")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in (BASE, SITE_EXAMPLE, "docker-compose.source.yml", "env.source.example"):
        shutil.copy(repo_root / "deploy" / name, deploy / name)
    shutil.copy(deploy / SITE_EXAMPLE, deploy / SITE)
    shutil.copy(deploy / "env.source.example", tmp_path / ".env")
    first, lines = _rendered(tmp_path)
    services = first["services"]
    assert first["name"] == "deltasleuth" and [name for name in services if "build" in services[name]] == [
        "app"
    ]
    assert [services[name]["image"] for name in ("app", "mocks")] == ["deltasleuth:source"] * 2
    assert [secret["source"] for secret in services["app"]["secrets"]] == ["db_dsn", "kek", "tokeniser"]
    read = {"name: deltasleuth", "image: deltasleuth:source", 'published: "8000"', "- subnet: 172.30.0.0/24"}
    assert read <= lines
    settings = {"/etc/deltasleuth": ("deploy/config", True)}
    checkout = {"/app/mocks": ("mocks", True), "/app/examples": ("examples", True)}
    assert _binds(first, tmp_path) == settings | checkout
    with (tmp_path / ".env").open("a") as env:
        env.write("COMPOSE_PROJECT_NAME=dsprobe55\n")
    assert OWN_SITE.count("18000") == 1 and OWN_SITE.count("172.31.10.") == 2  # the port; subnet and gateway
    (deploy / SITE).write_text(OWN_SITE.replace("18000", "18044").replace("172.31.10.", "172.31.7."))
    second, lines = _rendered(tmp_path)
    read = {"name: dsprobe55", "image: dsprobe55:source", 'published: "18044"', "- subnet: 172.31.7.0/24"}
    assert read <= lines and second["services"]["mocks"]["image"] == "dsprobe55:source"
    assert len(_owned(first)) == len(_owned(second)) == 8 and not _owned(first) & _owned(second)
    assert _binds(second, tmp_path) == settings | checkout
    shutil.copy(deploy / SITE_EXAMPLE, deploy / SITE)
    (tmp_path / ".env").write_text(f"COMPOSE_FILE=deploy/{BASE}:deploy/{SITE}\n")  # a release's
    loaded, _ = _rendered(tmp_path)
    services, image = loaded["services"], f"deltasleuth:{_version(repo_root)}"
    assert [services[name]["image"] for name in ("app", "mocks")] == [image, image]
    assert not any("build" in service for service in services.values())
    assert _binds(loaded, tmp_path) == settings


def test_the_developer_example_runs_from_a_checkout(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deploy/config/deltasleuth.dev.example.yaml, copied to <checkout>/.deltasleuth/ as its header says and
    loaded from another working directory: developer mode, every path and the DSN reference its own
    folder's, the workspace's keys made by ``ds init``, and both mock specs served
    by mocks/targets.yaml."""
    home = tmp_path / ".deltasleuth"
    home.mkdir()
    (tmp_path / "mocks").symlink_to(repo_root / "mocks")  # ../mocks/targets.yaml, as in a checkout
    shutil.copy(repo_root / "deploy" / "config" / "deltasleuth.dev.example.yaml", home / "deltasleuth.yaml")
    monkeypatch.chdir(repo_root / "docs")
    settings = load_settings(home / "deltasleuth.yaml")
    assert (settings.auth_mode, settings.encryption, settings.llm_profiles_file) == ("none", None, None)
    assert settings.database_dsn_ref == f"file:{home / 'db_dsn'}"
    assert (settings.evidence_dir, settings.workspace_dir) == (home / "evidence", home / "workspace")
    init_workspace(settings)
    assert sorted(path.name for path in settings.workspace_dir.iterdir()) == ["kek", "tokeniser.key"]
    assert settings.targets_file.resolve() == repo_root / "mocks" / "targets.yaml"
    _serves_the_mock_specs(settings)


def test_the_site_file_and_env_are_git_ignored(repo_root: Path) -> None:
    """Every compose change goes to the site file, which is git-ignored like .env, so no upgrade overwrites
    either; the examples they are copied from are tracked."""
    assert _ignored(repo_root, f"deploy/{SITE}") and _ignored(repo_root, ".env")
    assert not _ignored(repo_root, f"deploy/{SITE_EXAMPLE}")


def test_every_compose_file_value_keeps_the_site_file(repo_root: Path) -> None:
    """A command without the site file loses its secrets and, with a moved subnet, recreates the network and
    cuts the app off from db: every COMPOSE_FILE the kit gives names the base file, then the site file, and
    goes in .env, never in an exported variable, which overrides .env. The documents are held to it too."""
    texts = [(repo_root / p).read_text() for p in ("deploy/env.source.example", *TABLES)]
    assert all("COMPOSE_FILE=" in text for text in texts)
    texts += [page.read_text() for page in (repo_root / "README.md", *(repo_root / "docs").glob("*.md"))]
    values = [value for text in texts for value in re.findall(r"COMPOSE_FILE=([^\s'`]+)", text)]
    # Not a value: a sed pattern that adds a file to the line .env holds.
    values = [value for value in values if not value.startswith(".*")]
    assert [value for value in values if value.split(":")[:2] != [f"deploy/{BASE}", f"deploy/{SITE}"]] == []
    assert [line for text in texts for line in text.splitlines() if "export COMPOSE_FILE=" in line] == []


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        pass


def test_the_image_health_check_ignores_proxy_settings(repo_root: Path) -> None:
    """Compose passes a host's Docker proxy settings to the app; a loopback check must not use them."""
    dockerfile = (repo_root / "deploy" / "Dockerfile").read_text()
    command = json.loads(re.search(r"HEALTHCHECK [^\n]*\\\n\s*CMD (\[.*\])", dockerfile)[1])  # type: ignore[index]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("no_proxy", "http_proxy", "https_proxy")}
    try:
        check = [sys.executable, *command[1:-1], command[-1].replace("8000", str(server.server_address[1]))]
        done = subprocess.run(
            check, env=env | {"HTTP_PROXY": "http://127.0.0.1:9"}, capture_output=True, check=False
        )
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stderr.decode()
