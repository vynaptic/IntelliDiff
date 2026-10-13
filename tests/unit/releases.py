"""A scratch checkout for tools/release.py: a temporary git repository as
``release.ROOT``, a release key from ``release keygen``, and stub docker, syft, grype, uv, npm and pip-audit
(``python -m pip_audit``) on PATH that follow a scenario, keep the local image store and what runs, and log
each run's argv, cwd and environment. git and openssl are real."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from tools import release

PYTHON = sys.executable  # the stubs' interpreter: the tests put the pip-audit stub in sys.executable
PASSPHRASE = "correct horse battery staple"  # noqa: S105 - the scratch key's, in a file of its own
COMMITTED = "2026-09-28T12:00:00+00:00"  # the scratch commits' time: the release's created
DPKG = ["libc6", "libstdc++6", "openssl"]  # libstdc++6: a purl name that is percent-encoded
LIBCRYPTO = "psycopg_binary.libs/libcrypto-02bae65c.so.1.1.1k"
EXPORTED = (
    "psycopg-binary==3.3.6 ; implementation_name != 'pypy'\ncolorama==0.4.6 ; sys_platform == 'win32'\n"
)
LOCK = {"name": "deltasleuth-web", "lockfileVersion": 3, "packages": {
    "": {"name": "deltasleuth-web"}, "node_modules/react": {"version": "19.3.0"},
    "node_modules/vite": {"version": "7.1.0", "dev": True},
}}  # fmt: skip
PYTHON_CVE = "CVE-2026-82049"
PYTHON_PURL = "pkg:generic/python@3.13.15"
STUB = r'''#!__PYTHON__ -S
# A stub of docker, syft, grype, uv, npm or pip-audit (python -m pip_audit) for tools/release.py's tests: it
# follows the scenario, keeps the local image store and what runs in the state file, and logs each run.
import json, os, sys  # the others only where needed: each run starts a fresh interpreter
from pathlib import Path

CONFIG = __CONFIG__
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
scenario = json.loads(Path(CONFIG["scenario"]).read_text())
state = json.loads(Path(CONFIG["state"]).read_text())
# What macOS's CoreFoundation and Python's locale coercion add to this interpreter's own environment.
added = ("__CF_USER_TEXT_ENCODING", "LC_CTYPE")
env = {name: value for name, value in os.environ.items() if name not in added}
with open(CONFIG["log"], "a") as log:
    log.write(json.dumps({"tool": tool, "argv": args, "cwd": os.getcwd(), "env": env}) + "\n")
images = {**scenario["images"], **state["images"]}


def done(out="", code=0, err=""):
    Path(CONFIG["state"]).write_text(json.dumps(state))
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


def option(name):
    return args[args.index(name) + 1]


def sha(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()


def blobs(tag, found):
    """A saved image's layer, config and manifest, as docker save writes them."""
    layer = f"the layer of {tag}\n".encode()
    os_, arch = found["platform"].split("/")
    config = json.dumps({"architecture": arch, "os": os_, "config": {"User": found["user"]},
                         "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha(layer)]}}).encode()
    manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "config": {"digest": "sha256:" + sha(config), "size": len(config)},
                           "layers": [{"digest": "sha256:" + sha(layer), "size": len(layer)}]}).encode()
    return layer, config, manifest


if tool == "docker":
    if args[0] == "version":
        done(scenario["daemon"] + "\n")
    if args[:2] == ["image", "inspect"]:
        if args[4] not in images:
            done(code=1, err=f"Error: No such image: {args[4]}\n")
        found = images[args[4]]
        out = args[3].replace("{{.Os}}/{{.Architecture}}", found["platform"]).replace("{{.Id}}", found["id"])
        done(out.replace("{{.Config.User}}", found.get("user", "")) + "\n")
    if args[0] == "build":
        tag = option("-t")
        found = {"platform": scenario.get("built_platform", option("--platform")),
                 "user": scenario.get("user", "10001:10001")}
        built = found | {"id": "sha256:" + sha(blobs(tag, found)[1])}
        if images.get(tag, built)["id"] != built["id"]:  # the tag moves: its image stays, untagged, by its id
            state["images"][images[tag]["id"]] = images[tag]
        state["images"][tag] = built
        state["context"] = sorted(str(p) for p in Path().rglob("*") if p.is_file())
        if scenario.get("pull_during_build"):
            state["images"][scenario["index"]] = images[scenario["index"]] | {"id": "sha256:" + "f" * 64}
        done()
    if args[:2] == ["image", "rm"] and args[2:] in ([ref] for ref in state["images"]):  # one, never forced
        if scenario.get("in_use"):  # a container of the image: docker removes neither it nor its tag
            done(code=1, err="Error response from daemon: conflict: unable to remove repository reference"
                 f' "{args[2]}" (must force) - container 0f1e2d3c4b5a is using its referenced image\n')
        del state["images"][args[2]]
        done(f"Untagged: {args[2]}\n")
    if args[0] == "tag" and args[1] in state["images"] and args[1].startswith("sha256:"):  # an untagged one
        state["images"][args[2]] = state["images"].pop(args[1])
        done()
    if args[:2] == ["network", "create"]:
        state["networks"].append(args[-1])
        done()
    if args[:2] == ["network", "rm"]:
        state["networks"].remove(args[-1])
        done()
    if args[:2] == ["volume", "rm"]:
        state["volumes"] = [volume for volume in state["volumes"] if volume not in args]
        done()
    if args[0] == "rm":
        state["containers"] = [name for name in state["containers"] if name not in args]
        if "-v" in args:  # with the anonymous volumes of the removed containers
            state["volumes"] = [v for v in state["volumes"] if v.removesuffix("-anon") not in args]
        done()
    if args[0] == "exec":
        if args[1] not in state["containers"]:
            done(code=1, err=f"Error: No such container: {args[1]}\n")
        done(code=0 if "pg_isready" in args or scenario.get("health", True) else 1)
    if args[0] == "run":
        valued = {"--pull", "--name", "--network", "--platform", "--tmpfs", "--cap-drop", "--security-opt",
                  "-v", "-e", "--entrypoint"}
        at, given = 1, {}
        while args[at].startswith("-"):
            given.setdefault(args[at], []).append(args[at + 1] if args[at] in valued else True)
            at += 2 if args[at] in valued else 1
        ref, command = args[at], args[at + 1:]
        if ref not in images or given.get("--pull") != ["never"]:
            done(code=125, err=f"Unable to find image '{ref}' locally\n")
        state["volumes"] = sorted({*state["volumes"], *(v.split(":")[0] for v in given.get("-v", [])
                                                        if not v.startswith("/"))})
        entrypoint = given.get("--entrypoint", [None])[0]
        if entrypoint == "dpkg-query":
            done("".join(f"ii {name}\n" for name in scenario["dpkg"]) + "rc removed-long-ago\n")
        if entrypoint == "python":
            done("".join(f"{line}\n" for line in [scenario["python"], *scenario["libs"]]))
        if command[:2] == ["ds", "init"]:
            done(code=0 if scenario.get("init", True) else 1)
        if command[:3] == ["ds", "schedules", "next"]:  # the image's tz data loads the zone, or exit 2
            done(code=0 if scenario.get("zones", True) else 2)
        if command[:2] == ["python", "-c"] and "duck.selftest(" in command[2]:  # DuckDB loads and spills
            done(code=0 if scenario.get("duck", True) else 1)
        if command[:2] == ["python", "-c"] and "server.selftest(" in command[2]:  # MCP's tools are listed
            done(code=0 if scenario.get("mcp", True) else 1)
        if ref.startswith("postgres:"):  # its VOLUME /var/lib/postgresql/data: an anonymous volume
            state["volumes"].append(given["--name"][0] + "-anon")
        state["containers"].append(given["--name"][0])
        done(given["--name"][0] + "\n")
    if args[0] == "save":
        import io, tarfile

        tags = [args[-1], *scenario.get("also_saved", [])]
        files, index, listed = {"oci-layout": b'{"imageLayoutVersion": "1.0.0"}'}, [], []
        for tag in tags:
            layer, config, manifest = blobs(tag, images.get(tag, {"platform": "linux/amd64", "user": ""}))
            files |= {f"blobs/sha256/{sha(blob)}": blob for blob in (layer, config, manifest)}
            index.append({"digest": "sha256:" + sha(manifest), "size": len(manifest),
                          "annotations": {"io.containerd.image.name": f"docker.io/library/{tag}"}})
            listed.append({"Config": f"blobs/sha256/{sha(config)}", "RepoTags": [tag],
                           "Layers": [f"blobs/sha256/{sha(layer)}"]})
        files |= {"index.json": json.dumps({"schemaVersion": 2, "manifests": index}).encode(),
                  "manifest.json": json.dumps(listed).encode()}
        with tarfile.open(option("-o"), "w", format=tarfile.USTAR_FORMAT) as tar:
            for name in sorted(files):
                info = tarfile.TarInfo(name)
                info.size = len(files[name])
                tar.addfile(info, io.BytesIO(files[name]))
        done()
    if args[0] == "load":
        import gzip, tarfile

        with tarfile.open(fileobj=gzip.open(option("-i"))) as tar:
            [entry] = json.load(tar.extractfile("manifest.json"))
        found = {"platform": "linux/amd64", "id": "sha256:" + entry["Config"].rsplit("/", 1)[1]}
        state["images"][entry["RepoTags"][0]] = found | {"id": scenario.get("loaded_id") or found["id"]}
        done(f"Loaded image: {entry['RepoTags'][0]}\n")
if tool == "syft":
    if args[0] == "version":
        done(json.dumps({"application": "syft", "version": scenario["syft_version"]}))
    if not Path("image.tar").is_file():
        done(code=1, err="no image.tar in the working directory\n")
    for output in (args[at + 1] for at, arg in enumerate(args) if arg == "-o"):
        kind, path = output.split("=", 1)
        Path(path).write_text(json.dumps(scenario["spdx" if kind.startswith("spdx") else "sbom"]))
    done()
if tool == "grype":
    if args[0] == "version":
        done(json.dumps({"application": "grype", "version": scenario["grype_version"]}))
    if args[:2] == ["db", "update"]:
        done("Vulnerability database updated to latest version!\n")
    if args[:2] == ["db", "status"]:
        done(json.dumps(scenario["db_status"]))
    matches, ignored = scenario["matches"], []
    if "--vex" in args and scenario.get("vex_suppresses", True):
        vex = json.loads(Path(option("--vex")).read_text())
        out = {(s["vulnerability"]["name"], product["@id"]) for s in vex["statements"]
               if s["status"] == "not_affected" for product in s["products"]}
        ignored = [m for m in matches
                   if (m["vulnerability"]["id"], m["artifact"]["purl"].split("?")[0]) in out]
        matches = [m for m in matches if m not in ignored]
    descriptor = {"name": "grype", "version": scenario["grype_version"],
                  "db": {"status": scenario["db_status"]},
                  "configuration": {"db": {"cache-dir": scenario["db_status"]["path"]}}}
    done(json.dumps({"matches": matches, "ignoredMatches": ignored, "descriptor": descriptor,
                     "source": {"type": "image", "target": {"userInput": "deltasleuth"}}}))
if tool == "uv":
    if args[0] == "export":
        done(scenario["exported"])
    check = "pytest" if "pytest" in args else "ruff"
    if check == "pytest" and scenario.get("commit_during_checks"):
        import subprocess

        subprocess.run(["git", "commit", "--allow-empty", "-qm", "during the build"], check=True)
    done(f"{check} said this\n", scenario["checks"].get(check, 0))
if tool == "npm":
    done(f"npm {args[2]} said this\n", scenario["checks"].get(f"npm {args[2]}", 0))
if tool == "python" and args[:2] == ["-m", "pip_audit"]:
    state["audited"] = Path(option("-r")).read_text()
    Path(option("-o")).write_text(json.dumps(scenario["audit"]))
    done(code=1 if any(dependency.get("vulns") for dependency in scenario["audit"]["dependencies"]) else 0)
done(code=99, err=f"the {tool} stub has no answer for {args}\n")
'''


def cyclonedx(version: str) -> dict[str, Any]:
    """What syft finds in the scenario's image: the distribution, dpkg's packages, python, the locked PyPI
    packages for linux, deltasleuth and the web lock's non-dev package."""
    deb = [f"pkg:deb/debian/{name.replace('+', '%2B')}@1.0-1?arch=amd64&distro=debian-13.7" for name in DPKG]
    purls = [*deb, PYTHON_PURL, "pkg:pypi/psycopg-binary@3.3.6", f"pkg:pypi/deltasleuth@{version}",
             "pkg:npm/react@19.3.0", "pkg:npm/deltasleuth-web"]  # fmt: skip
    components = [
        {"type": "library", "name": purl.split("/")[-1].split("@")[0], "purl": purl} for purl in purls
    ]
    debian = {"type": "operating-system", "name": "debian", "version": "13.7"}
    metadata = {"supplier": {"name": "Example Vendor Ltd"}}
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": metadata,
        "components": [debian, *components],
    }


def match(vid: str, severity: str, state: str, purl: str, *fixed: str, related: tuple[str, ...] = (),
          kev: bool = False) -> dict[str, Any]:  # fmt: skip
    """A grype match as grype's JSON writes it."""
    vulnerability = {"id": vid, "severity": severity, "fix": {"versions": list(fixed), "state": state}}
    return {"vulnerability": vulnerability | ({"knownExploited": [{"cve": vid}]} if kev else {}),
            "relatedVulnerabilities": [{"id": other} for other in related],
            "artifact": {"name": purl.split("/")[-1].split("@")[0], "purl": purl}}  # fmt: skip


def scenario(root: Path, db: Path, version: str = "0.1.0") -> dict[str, Any]:
    """The scenario of a release that passes: every base local, a built image that serves, an SBOM that is
    complete, one blocking finding that the triage decides and one vendored library that it decides."""
    dockerfile = (root / "deploy" / "Dockerfile").read_text()
    index = re.search(r"^ARG PYTHON=(\S+)", dockerfile, re.M)[1]  # type: ignore[index]
    node = re.search(r"^FROM --platform=\S+ (\S+)", dockerfile, re.M)[1]  # type: ignore[index]
    local = {f"python@{digest}": platform for platform, digest in release.BASES.items()}
    local |= {node: "linux/arm64", release.POSTGRES: "linux/arm64", index: "linux/arm64"}
    return {
        "daemon": "linux/arm64", "index": index,
        "images": {ref: {"platform": at, "id": f"sha256:{n:064d}"}
                   for n, (ref, at) in enumerate(local.items())},
        "dpkg": DPKG, "python": "3.13.15", "libs": [f"psycopg-binary 3.3.6\t{LIBCRYPTO}"],
        "syft_version": release.SYFT, "grype_version": release.GRYPE,
        "sbom": cyclonedx(version), "spdx": {"spdxVersion": "SPDX-2.3", "name": "deltasleuth"},
        "db_status": {"schemaVersion": "v6.1.9", "built": "2026-09-29T06:32:31Z", "valid": True,
                      "from": "https://grype.anchore.io/databases/v6/vulnerability-db_v6.1.9.tar.zst",
                      "path": str(db / "6" / "vulnerability.db")},
        "matches": [
            match(PYTHON_CVE, "High", "fixed", PYTHON_PURL, "3.14.0b1"),
            match("CVE-2026-82560", "High", "wont-fix", "pkg:deb/debian/openssl@1.0-1?arch=amd64"),
            match("CVE-2026-10001", "Medium", "fixed", "pkg:deb/debian/libc6@1.0-1?arch=amd64", "1.0-2"),
        ],
        "exported": EXPORTED, "checks": {},
        "audit": {"dependencies": [{"name": "psycopg-binary", "version": "3.3.6", "vulns": []}], "fixes": []},
    }  # fmt: skip


def triage(today: datetime) -> str:
    """The scratch triage: the python finding not affected, the vendored OpenSSL affected."""
    decided, expires, soon = (
        today.date(),
        (today + timedelta(days=80)).date(),
        (today + timedelta(days=20)).date(),
    )
    return f"""# What the release gate accepts, each decision with an expiry.
[[finding]]
id = "{PYTHON_CVE}"
component = "{PYTHON_PURL}"
status = "not_affected"
justification = "vulnerable_code_not_in_execute_path"
statement = "DeltaSleuth never calls the affected function."
decided = {decided}
expires = {expires}
by = "maintainer"

[[vendored]]
distribution = "psycopg-binary"
version = "3.3.6"
files = ["{LIBCRYPTO}"]
status = "affected"
statement = "OpenSSL 1.1.1k is end of life; moving to psycopg[c] by {soon}."
decided = {decided}
expires = {soon}
by = "maintainer"
"""


def keygen(out: Path, passphrase: Path) -> Result:
    return CliRunner().invoke(
        release.app, ["keygen", "--out", str(out), "--passphrase", f"file:{passphrase}"]
    )


@dataclass
class Scratch:
    tmp: Path
    root: Path  # the scratch checkout: release.ROOT
    bin: Path  # the stubs
    key: Path
    passphrase: Path
    fingerprint: str
    procedure: Path  # keygen's procedure.txt, as the site files it
    scenario: dict[str, Any]
    out: Path = field(init=False)
    register: Path = field(init=False)
    notes: Path = field(init=False)

    def __post_init__(self) -> None:
        self.out, self.register, self.notes = (
            self.tmp / "out",
            self.tmp / "register.jsonl",
            self.tmp / "notes.txt",
        )
        # git's default cleanup drops #; an open build's notes say so
        self.notes.write_text(f"# DeltaSleuth 0.1.0\n\nThe first release.\n{release.OPEN_BUILD}\n")

    def git(self, *args: str) -> str:
        env = os.environ | {"GIT_AUTHOR_DATE": COMMITTED, "GIT_COMMITTER_DATE": COMMITTED}
        return subprocess.run(["git", *args], cwd=self.root, env=env, capture_output=True, text=True,
                              check=True).stdout.strip()  # fmt: skip

    def commit(self, files: dict[str, str | None]) -> str:
        """Write (None: delete) and commit these files; the new revision."""
        for name, text in files.items():
            path = self.root / name
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", f"change {', '.join(files)}")
        return self.git("rev-parse", "HEAD")

    def build(self, *args: str, **options: str) -> Result:
        """``release build`` with the scratch key, notes, out and register; ``options`` replace any."""
        (self.tmp / "scenario.json").write_text(json.dumps(self.scenario))
        given = {"key": f"file:{self.key}", "passphrase": f"file:{self.passphrase}", "notes": str(self.notes),
                 "out": str(self.out), "register": str(self.register)} | options  # fmt: skip
        argv = ["build", *(part for name, value in given.items() for part in (f"--{name}", value)), *args]
        return CliRunner().invoke(release.app, argv)

    def runs(self, tool: str | None = None) -> list[dict[str, Any]]:
        lines = (self.tmp / "log.jsonl").read_text().splitlines() if (self.tmp / "log.jsonl").exists() else []
        return [run for run in map(json.loads, lines) if tool is None or run["tool"] == tool]

    def state(self) -> dict[str, Any]:
        return json.loads((self.tmp / "state.json").read_text())

    def bundle(self, version: str = "0.1.0") -> Path:
        return self.out / f"deltasleuth-{version}-linux-amd64"


def scratch(
    tmp: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path, *, path: str | None = None
) -> Scratch:
    """A scratch checkout at version 0.1.0 whose release would pass, as ``release.ROOT``, with its signing key
    listed, HOME in ``tmp`` and the stubs first on PATH (then ``path``: /usr/bin and /bin, so git but no real
    docker, syft or grype)."""
    home, bin_ = tmp / "home", tmp / "bin"
    for folder in (home, bin_, tmp / "media"):
        folder.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{path or '/usr/bin:/bin'}")
    for variable in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{variable}_NAME", "Release Maintainer")
        monkeypatch.setenv(f"GIT_{variable}_EMAIL", "maintainer@vendor.test")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    passphrase = tmp / "media" / "passphrase"
    passphrase.write_text(f"{PASSPHRASE}\n")
    made = keygen(tmp / "keys", passphrase)
    assert made.exit_code == 0, made.output
    [key] = (tmp / "keys").glob("release-*.key.pem")
    listed = re.search(r"^[0-9a-f]{64} signing procedure=[0-9a-f]{64}$", made.output, re.M)[0]  # type: ignore[index]
    root = tmp / "repo"
    dockerfile = (repo_root / "deploy" / "Dockerfile").read_text()
    compose = "services:\n  app: {image: 'deltasleuth:%s'}\n  mocks: {image: 'deltasleuth:%s'}\n"
    files = {
        "LICENSE": "Apache License, Version 2.0\n",
        "README.md": "# DeltaSleuth\n", "SECURITY.md": "# Security\n\nReport to security@vendor.test.\n",
        "pyproject.toml": '[project]\nname = "deltasleuth"\nversion = "0.1.0"\n'
                          'authors = [{name = "Example Vendor Ltd"}]\n',
        "uv.lock": "version = 1\n", "deltasleuth/__init__.py": "",
        "deltasleuth/store/migrations/001_init.sql": "--\n",
        "web/package.json": '{"name": "deltasleuth-web", "private": true}\n',
        "web/package-lock.json": json.dumps(LOCK), "deploy/Dockerfile": dockerfile,
        "deploy/docker-compose.yml": compose % ("0.1.0", "0.1.0"),
        "deploy/docker-compose.ca.yml": compose % ("0.1.0-ca", "0.1.0-ca"),
        "docs/deployment.md": "# Deployment\n", "docs/user-guide.md": "# User guide\n",
        "mocks/__init__.py": "", "examples/README.md": "# Examples\n", "tests/test_nothing.py": "",
        "tools/release-keys.txt": f"# the scratch release key\n{listed}\n",
        "tools/triage.toml": triage(datetime.now(UTC)),
        ".gitignore": "/.env\ndeploy/docker-compose.site.yml\ndeploy/secrets/\n",
    }  # fmt: skip
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    made = Scratch(tmp, root, bin_, key, passphrase, listed[:64], tmp / "keys" / "procedure.txt",
                   scenario(root, home / "Library" / "Caches" / "grype" / "db"))  # fmt: skip
    made.git("init", "-q", "-b", "main")
    made.git("add", "-A")
    made.git("commit", "-qm", "the scratch checkout")
    for name in (".env", "deploy/docker-compose.site.yml", "deploy/secrets/kek"):  # site files, git-ignored
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("site only\n")
    config = json.dumps({name: str(tmp / f"{name}.json") for name in ("scenario", "state")}
                        | {"log": str(tmp / "log.jsonl")})  # fmt: skip
    for tool in ("docker", "syft", "grype", "uv", "npm", "python"):
        (bin_ / tool).write_text(STUB.replace("__PYTHON__", PYTHON).replace("__CONFIG__", config))
        (bin_ / tool).chmod(0o755)
    (tmp / "state.json").write_text(
        json.dumps({"images": {}, "containers": [], "networks": [], "volumes": []})
    )
    (tmp / "scenario.json").write_text(json.dumps(made.scenario))
    monkeypatch.setattr(release, "ROOT", root)
    monkeypatch.setattr(
        sys, "executable", str(bin_ / "python")
    )  # pip-audit runs as sys.executable -m pip_audit
    return made
