"""deploy/up.sh (docs/mvp/FIRSTRUN.md §3): from a fresh clone to a running trial, against a stand-in docker.

The script runs in a scratch checkout under the real ``sh``, and under ``dash`` where installed, with a PATH
of one folder: links to the POSIX tools it may call and to ``openssl``, and stand-ins for ``docker``,
``curl``, ``sleep`` and ``uname`` that follow a scenario and log each call's argv and environment. Any other
program is "not found" there, so that folder is also everything the script can run (I5), and no Docker
daemon is ever asked."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from getpass import getuser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth.auth import tokens
from deltasleuth.config import load_settings
from deltasleuth.core.secrets import read_secret

REPO = Path(__file__).resolve().parents[3]
CONTRACT = (REPO / "docs" / "mvp" / "FIRSTRUN.md").read_text()
ENV = (REPO / "deploy" / "env.source.example").read_text()
SETTINGS = (REPO / "deploy" / "config" / "deltasleuth.example.yaml").read_text()
SITE = (REPO / "deploy" / "docker-compose.site.example.yml").read_text()
VERSION = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())["x-release"]["image"]
SHELLS = [shell for shell in ("sh", "dash") if shutil.which(shell)]
# The POSIX tools the script calls, with openssl: all a run finds on its PATH but the stand-ins.
TOOLS = ("cat", "chmod", "cp", "cut", "grep", "id", "ls", "mkdir", "mv", "rm", "sed", "tail", "tr", "openssl")
TEN = (  # §3.3: what a run creates, in the order it says so
    "deploy/config/deltasleuth.yaml", "deploy/config/targets.yaml", "deploy/config/llm_profiles.yaml",
    "deploy/docker-compose.site.yml", ".env", "deploy/secrets/db_password", "deploy/secrets/db_dsn",
    "deploy/secrets/kek", "deploy/secrets/tokeniser", ".deltasleuth/tokens/first-run",
)  # fmt: skip
STEPS = (
    "Settings, site file and .env", "Secrets", "Build the image {project}:source", "Workspace and database",
    "Start", "First token",
)  # fmt: skip
STARTED_FROM = 'x{{.Label "com.docker.compose.project.working_dir"}}'  # U16 asks each container
UNNAMED = "a folder its container does not name"  # U16's folder, of a container without that label
PROBE = ["-q", "-g", "-s", "-o", "/dev/null", "--noproxy", "*", "--max-time", "2"]  # U18's curl, before a URL
LOOPBACK = ("127.0.0.1", "[::1]")  # the two addresses U18 asks, in its order
LABEL = "label=com.docker.compose."
# Step 5's filters after the project's: the app service's own container, none of ``docker compose run``.
OWN = ["--filter", LABEL + "service=app", "--filter", LABEL + "oneoff=False"]
I4 = {"pull", "down", "rm", "prune"}  # no argv of a Docker call holds one: "volume rm" and the like too
OPTIONS = "(--project NAME, --port N, --subnet A.B.C.0/24)"
NAME = "a project name is lower-case letters and digits, joined by -, _ or __ (for example ds2)"
IN_ENV = f".env's COMPOSE_PROJECT_NAME (read unquoted and once): {NAME}"
PORT = "--port: a number from 1024 to 65535"
NET = "--subnet: a.b.c.0/24, for example 172.31.7.0/24"
AGAIN = "remove it yourself, then run this script again"
UNCHANGED = "this script changes no existing file (edit it yourself, or give the option that matches it)"
TRIAL = (
    "this script runs an unsigned build of this checkout for a trial; a {release} is run as"
    " docs/mvp/DEPLOY.md says"
)
U12 = (
    ".env has no line COMPOSE_FILE=… that names deploy/docker-compose.source.yml (once, unquoted): "
    + TRIAL.format(release="loaded release")
)
U13 = (
    f"the release image {VERSION} is loaded on this machine and this folder has no .env: "
    + TRIAL.format(release="release")
    + " (for a trial all the same: cp deploy/env.source.example .env and, for another project than"
    " deltasleuth, add a line COMPOSE_PROJECT_NAME=<name> to it; then run this script again)"
)
U14 = (
    "deploy/config/deltasleuth.yaml is not a token sign-in trial at a loopback http address ({why}): this"
    " script sets up a trial only; docs/mvp/DEPLOY.md is the office deployment"
)


def _u14(why: str) -> str:
    return U14.format(why=why)


def _at(address: str) -> str:
    """The settings example with ``address``, written as given, as its ``public_url``."""
    return SETTINGS.replace("http://localhost:8000 ", f"{address} ")


U16 = (
    'Compose project "{project}" on this machine was started from {dir}: this script takes over no other'
    " installation. For a second one here: sh deploy/up.sh --project <name> --port <port> --subnet"
    " <a.b.c.0/24>"
)
U21 = (
    "deploy/secrets holds some of db_password, db_dsn, kek and tokeniser but not all: put the missing ones"
    " back; new ones cannot open what the old ones protected"
)
U6 = "deploy/docker-compose.yml is not in this folder: cd to the DeltaSleuth checkout, then sh deploy/up.sh"
U7 = "unset COMPOSE_FILE and COMPOSE_PROJECT_NAME in this shell: they override .env"
U8 = "{tool} is not installed (docs/guide/01-install-docker.md, section 1)"
U11 = (
    '.env makes this folder project "{a}", not "{b}": this script changes no existing file (leave --project'
    " out, or edit .env yourself)"
)
NO_LINE = 'it has no single line "  public_url: <address>"'
U15 = f"{{file}} exists and does not say {{what}}: {UNCHANGED}"
MOVE = "choose another with --port N (for a first installation on this machine that option alone is enough)"
U17 = f"port {{port}} of this machine is published by the container {{name}}: {MOVE}"
U18 = f"port {{port}} of this machine answers already: {MOVE}"
U19 = "the subnet {subnet} is the Docker network {name}'s: choose another with --subnet a.b.c.0/24"
U20 = (
    'Docker volumes of project "deltasleuth" exist, but deploy/secrets holds no db_password, kek and'
    " tokeniser: this folder does not hold the keys of that data, which DeltaSleuth cannot open without"
    " them. Use another project name, or put the secret files back. If the data is no longer wanted, docker"
    " volume ls --filter label=com.docker.compose.project=deltasleuth lists its volumes, for you to remove"
    " with docker volume rm; this script deletes nothing"
)
U24 = (
    "Docker Compose {version} is older than 2.24.4, which a site file for another port or subnet needs"
    " (!override): update Compose, or keep port 8000 and subnet 172.30.0.0/24"
)
U25 = "deploy/secrets is open to other accounts: chmod 700 it yourself, then run this script again"
U26 = (
    "deploy/secrets/db_password holds characters an address cannot carry as written: write"
    " deploy/secrets/db_dsn yourself (postgresql://deltasleuth:<the password, percent-encoded>@db:5432/"
    "deltasleuth), then run this script again"
)
U27 = (
    "{file} is a link to a file that is not there: put that file back, or remove the link yourself; this"
    " script changes no existing file"
)
V1 = "Compose does not accept .env and deploy/docker-compose.site.yml (the lines above say why)"
V2 = '.env does not make this folder project "deltasleuth"'
V3 = (
    "Compose would not run the image deltasleuth:source: this script runs a build of this checkout only (see"
    " COMPOSE_FILE in .env)"
)
V4 = (
    "deploy/docker-compose.site.yml does not publish port 8000, which deploy/config/deltasleuth.yaml names in"
    " public_url"
)
V5 = "{step} failed (the lines above say why): correct it and run this script again; it keeps what exists"
V6 = (
    "the app did not become healthy; the line above, if any, is its last error (all of it: docker compose"
    " logs --tail 40 app)"
)
V7 = "the token was not created (the lines above say why)"
W1 = (
    "warning: the token in .deltasleuth/tokens/first-run (T-{id}) is not an active token of this database"
    " (revoked, expired, or another database's) and will be refused: remove the file and run this script"
    " again for a new one\n"
)

# The stand-in for docker, curl, sleep and uname, by the name it is called under. ``compose config`` is a
# small model of Compose's: the project, the two images, the published port and the subnet of the files
# that .env or -f names (tests/unit/deploy/test_config_examples.py renders the same files in Compose).
STAND_IN = r"""
import json, os, re, signal, sys, time
from pathlib import Path

state = Path(__file__).resolve().parent.parent / "stub"
tool, argv = Path(sys.argv[0]).name, sys.argv[1:]
scenario = json.loads((state / "scenario.json").read_text())
log = state / "calls.jsonl"
before = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
with log.open("a") as calls:
    calls.write(json.dumps({"tool": tool, "argv": argv, "env": dict(os.environ)}) + "\n")


def end(code=0, out="", err=""):
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


def after(option):
    return argv[argv.index(option) + 1]


def render(files):
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    class Override(list):
        pass

    Loader.add_constructor("!override", lambda loader, node: Override(loader.construct_sequence(node, True)))
    lines = Path(".env").read_text().splitlines() if Path(".env").exists() else []
    env = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    name, images, ports, subnets = env.get("COMPOSE_PROJECT_NAME"), {}, [], []
    for file in files or env.get("COMPOSE_FILE", "compose.yaml").split(":"):
        if not Path(file).is_file():
            end(14, err=f"open {file}: no such file or directory\n")
        text = re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m[1]) or m[2], Path(file).read_text())
        try:
            doc = yaml.load(text, Loader=Loader)
        except yaml.YAMLError:
            end(15, err=f"yaml: {file} is not a compose file\n")
        name = name or doc.get("name")
        services = doc.get("services") or {}
        images |= {service: body["image"] for service, body in services.items() if "image" in (body or {})}
        published = (services.get("app") or {}).get("ports")
        if published is not None:
            ports = ([] if isinstance(published, Override) else ports) + published
        config = (((doc.get("networks") or {}).get("default") or {}).get("ipam") or {}).get("config")
        if config is not None:
            subnets = ([] if isinstance(config, Override) else subnets) + config
    out = [f"name: {name}", "services:", "  app:", f"    image: {images.get('app')}", "    ports:"]
    for port in ports:
        out += ["      - mode: ingress", f'        published: "{port.split(":")[-2]}"']
    out += ["  mocks:", f"    image: {images.get('mocks')}", "networks:", "  default:", "    ipam:"]
    out += ["      config:", *(f"        - subnet: {net['subnet']}" for net in subnets)]
    return "\n".join(out) + "\n"


if tool == "curl":
    end(scenario["curl6" if "[::1]" in argv[-1] else "curl"])
if tool != "docker":
    end(out=scenario["uname"] + "\n" if tool == "uname" else "")
if argv[0] == "compose":
    words = argv[1:]
    while words[0] in ("-f", "--profile"):
        words = words[2:]
    files = [argv[at + 1] for at, word in enumerate(argv) if word == "-f"]
    if words == ["version", "--short"]:
        end(scenario["compose"] is None, out=f"{scenario['compose']}\n")
    if words == ["config"]:
        said = scenario["site" if files else "config"]
        if said is None:
            end(out=render(files))
        end(said["code"], err=said["err"])
    if words == ["build"]:
        end(scenario["build"], err="stub: build\n")
    if words == ["up", "-d"]:
        end(scenario["up"], err="stub: up\n")
    if words[0] == "logs":
        end(out=scenario["logs"])
    if words[0] == "run":
        ds = words[words.index("ds") + 1 :]
        command = " ".join(ds[:2]) if ds[0] == "token" else ds[0]
        if command == scenario["block"]:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # Python ignores it; what docker starts does not
            (state / "blocked").touch()
            time.sleep(60)
        if command == "token create":
            end(scenario["ds"][command], out=scenario["token"], err="token T-… (first-run): shown once\n")
        code, listed = scenario["ds"][command], scenario["tokens"] if command == "token list" else ""
        end(code, out=listed, err=scenario["said"] if code else "")
elif argv[0] == "info":
    end(scenario["info"])
elif argv[:2] == ["image", "inspect"]:
    end(argv[2] not in scenario["images"])
elif argv[0] == "ps":
    asked = [argv[at + 1] for at, word in enumerate(argv) if word == "--filter"]
    if "label=com.docker.compose.service=app" in asked:  # step 5: the app service's containers, newest first
        own = "label=com.docker.compose.oneoff=False" in asked  # else those of ``docker compose run`` too
        end(out=("" if own else scenario["oneoff"]) + scenario["app"])
    for container in scenario["containers"] if scenario["ps"] == 0 else ():
        if not (container["running"] or "-a" in argv):
            continue
        if not asked:
            print(container["name"], container.get("ports", ""))
        elif asked == [f"label=com.docker.compose.project={container['project']}"]:
            label = '{{.Label "com.docker.compose.project.working_dir"}}'
            print(after("--format").replace(label, container["dir"]))
    end(scenario["ps"], err="stub: ps\n" if scenario["ps"] else "")
elif argv[:3] == ["volume", "ls", "-q"]:
    project = after("--filter").rpartition("=")[2]
    end(out="".join(f"{name}\n" for name in scenario["volumes"].get(project, ())))
elif argv[:3] == ["network", "ls", "-q"]:
    end(out="".join(f"{name}\n" for name in scenario["networks"]))
elif argv[:2] == ["network", "inspect"]:
    end(out=scenario["networks"][argv[-1]] + "\n")
elif argv[0] == "inspect":
    if argv[-1] not in scenario["app"].split():  # as Docker answers a name it does not hold: two ids are none
        end(1, err=f"error: no such object: {argv[-1]}\n")
    read = sum(call["tool"] == "docker" and call["argv"][:1] == ["inspect"] for call in before)
    end(out=scenario["health"][min(read, len(scenario["health"]) - 1)] + "\n")
end(99, err=f"stub: docker {' '.join(argv)} is not a call the script makes\n")
"""


@dataclass
class Ran:
    code: int
    out: str
    err: str
    calls: list[dict[str, Any]]  # each stand-in call: its tool, argv and environment
    words: str = ""  # in a watched trial: the words of every other program the script ran, a call a line

    def docker(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls if call["tool"] == "docker"]

    def ds(self) -> list[str]:
        """What a ``docker compose`` call ran or started: ``build``, ``up`` and each ``ds`` command."""
        calls = [argv for argv in self.docker() if argv[0] == "compose"]
        started = [word for argv in calls for word in argv[1:] if word in ("build", "up")]
        return started + [" ".join(argv[argv.index("ds") :]) for argv in calls if "ds" in argv]


@dataclass
class Trial:
    """A scratch checkout, the folder of programs a run finds, and the scenario its stand-ins follow."""

    tmp: Path
    shell: str
    watched: bool = False  # the real tools log their words too, so every argv of a run can be read
    token: str = field(default_factory=lambda: f"dst_{secrets.token_hex(8)}_{secrets.token_hex(32)}")

    def __post_init__(self) -> None:
        self.folder, self.bin, self.stub = self.tmp / "checkout", self.tmp / "bin", self.tmp / "stub"
        for made in (self.folder / "deploy" / "config", self.bin, self.stub):
            made.mkdir(parents=True)
        deploy = REPO / "deploy"
        for kit in (*deploy.glob("docker-compose*.yml"), deploy / "up.sh", deploy / "env.source.example"):
            if kit.name != "docker-compose.site.yml":  # a checkout's own, in no kit
                shutil.copy(kit, self.folder / "deploy" / kit.name)
        for example in (deploy / "config").glob("*.example.yaml"):
            shutil.copy(example, self.folder / "deploy" / "config" / example.name)
        for tool in TOOLS:
            if not self.watched:
                (self.bin / tool).symlink_to(str(shutil.which(tool)))
                continue
            log = f'printf \'%s\\n\' "{tool} $*" >> "{self.stub / "words.log"}"'
            (self.bin / tool).write_text(f'#!/bin/sh\n{log}\nexec "{shutil.which(tool)}" "$@"\n')
            (self.bin / tool).chmod(0o755)
        for stand_in in ("docker", "curl", "sleep", "uname"):
            (self.bin / stand_in).write_text(f"#!{sys.executable}\n{STAND_IN}")
            (self.bin / stand_in).chmod(0o755)

    @property
    def token_id(self) -> str:
        return self.token.split("_")[1]

    def scenario(self, **changes: Any) -> dict[str, Any]:
        """A machine that holds nothing of DeltaSleuth, where every step succeeds, but for ``changes``."""
        return {
            "compose": "2.40.3-desktop.1", "info": 0, "images": [], "containers": [], "volumes": {}, "ps": 0,
            "networks": {"n1": "bridge 172.17.0.0/16 ", "n2": "host ", "n3": "other_default 172.29.0.0/24 "},
            "curl": 7, "curl6": 7, "uname": "Darwin", "config": None, "site": None, "build": 0, "up": 0,
            "app": "c0ffee\n", "oneoff": "",
            "ds": {"init": 0, "migrate": 0, "token create": 0, "token list": 0}, "said": "",
            "health": ["starting 0", "healthy 0"], "logs": "", "token": self.token + "\n", "tokens": "",
            "block": None,
        } | changes  # fmt: skip

    def installed(self, project: str = "deltasleuth", port: int = 8000) -> dict[str, Any]:
        """What Docker holds once this folder runs as ``project``: scenario changes for a later run."""
        app = {"project": project, "dir": str(self.folder / "deploy"), "running": True}
        stopped = app | {"running": False, "name": f"{project}-mocks-1"}
        return {
            "containers": [
                app | {"name": f"{project}-app-1", "ports": f"127.0.0.1:{port}->8000/tcp"},
                stopped,
            ],
            "volumes": {project: [f"{project}_pgdata", f"{project}_evidence"]},
            "tokens": f"T-{self.token_id} (first-run)  spec_author  active  expires 2027-01-03 10:00Z\n",
        }

    def path(self, name: str) -> Path:
        """Where ``name`` goes, its folder made as DEPLOY makes it: deploy/secrets is its owner's alone."""
        (self.folder / name).parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("deploy/secrets/"):
            (self.folder / "deploy" / "secrets").chmod(0o700)
        return self.folder / name

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            self.path(name).write_text(text.replace("{folder}", str(self.folder)))

    def link(self, links: dict[str, str]) -> None:
        """Symbolic links, each to a path under the scratch folder, which need not exist."""
        for name, target in links.items():
            self.path(name).symlink_to(self.tmp / target)

    def start(
        self,
        *options: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        umask: int = -1,
        **changes: Any,
    ) -> subprocess.Popen[str]:
        for log in ("calls.jsonl", "words.log"):
            (self.stub / log).unlink(missing_ok=True)
        text = json.dumps(self.scenario(**changes)).replace("{tmp}", str(self.tmp))
        (self.stub / "scenario.json").write_text(text)
        return subprocess.Popen(
            [str(shutil.which(self.shell)), str(self.folder / "deploy" / "up.sh"), *options],
            cwd=cwd or self.folder, env={"PATH": str(self.bin)} | (env or {}), text=True, umask=umask,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )  # fmt: skip

    def ended(self, proc: subprocess.Popen[str]) -> Ran:
        out, err = proc.communicate(timeout=120)
        log = self.stub / "calls.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        words = self.stub / "words.log"
        return Ran(proc.returncode, out, err, calls, words.read_text() if words.exists() else "")

    def shown(self, ran: Ran) -> list[str]:
        """Everything a run let out of its files: what it printed, and every argv and environment value."""
        assert self.watched and ran.words
        return [
            ran.out,
            ran.err,
            ran.words,
            *(w for call in ran.calls for w in (*call["argv"], *call["env"].values())),
        ]

    def run(self, *options: str, **how: Any) -> Ran:
        return self.ended(self.start(*options, **how))

    def tree(self) -> dict[str, tuple[int, int, bytes | None]]:
        """Every path of the checkout: its mode, when it was last written and, of a file, what it holds."""
        found = {}
        for path in sorted(self.folder.rglob("*")):
            status = path.lstat()
            holds = path.read_bytes() if path.is_file() else None
            found[str(path.relative_to(self.folder))] = (status.st_mode, status.st_mtime_ns, holds)
        return found

    def created(self, before: dict[str, Any]) -> list[str]:
        """The files, never the folders, that the checkout holds beyond ``before``."""
        return sorted(
            name for name, (mode, *_) in self.tree().items() if name not in before and stat.S_ISREG(mode)
        )

    def mode(self, name: str) -> int:
        return stat.S_IMODE((self.folder / name).stat().st_mode)


@pytest.fixture(params=SHELLS)
def trial(request: pytest.FixtureRequest, tmp_path: Path) -> Trial:
    return Trial(tmp_path, request.param)


@pytest.fixture(params=SHELLS)
def watched(request: pytest.FixtureRequest, tmp_path: Path) -> Trial:
    return Trial(tmp_path, request.param, watched=True)


def _fenced(after: str) -> str:
    """The contract's first fenced block after the text ``after``, unindented."""
    block = CONTRACT.partition(after)[2].partition("```")[2].partition("\n")[2].partition("```")[0]
    return textwrap.dedent(block[: block.rindex("\n") + 1])


def _rows(section: str) -> dict[str, re.Pattern[str]]:
    """Each message of a table of the contract's section, by its label: a ``<placeholder>`` is any text."""
    table = CONTRACT.partition(f"### {section}")[2].partition("\n\n- ")[0].partition("\n###")[0]
    return {
        label: re.compile(
            "".join(".+" if part[:1] == "<" else re.escape(part) for part in re.split("(<[^>]+>)", text))
        )
        for label, text in re.findall(r"^\| ([UVW]\d+) \| `(.+?)`", table, re.M)
    }


EARLY, LATER = _rows("3.2 Refusals before"), _rows("3.4 Refusals after")


def _closing(project: str = "deltasleuth", address: str = "http://localhost:8000") -> str:
    """§3.5's block, which a run ends with, for ``project`` at ``address``."""
    block = _fenced("### 3.5 The closing block")
    assert f"it expires at most {tokens.DEFAULT_DAYS} days after it was made" in block
    return block.replace('"deltasleuth"', f'"{project}"').replace("http://localhost:8000", address)


def _said(project: str = "deltasleuth", kept: tuple[str, ...] = ()) -> str:
    """A whole run's stdout before the closing block: each step, and each of the ten files under its own."""
    lines = []
    for number, title in enumerate(STEPS, 1):
        lines.append(f"== {number}/6 {title.format(project=project)}")
        files = {1: TEN[:5], 2: TEN[5:9], 6: TEN[9:]}.get(number, ())
        lines += [f"{'kept    ' if file in kept else 'created '} {file}" for file in files]
    return "".join(f"{line}\n" for line in lines)


def test_up_sh_passes_shellcheck(repo_root: Path) -> None:
    """Clean under ``shellcheck -s sh``, parsed by every shell here, and executable in the checkout."""
    script = repo_root / "deploy" / "up.sh"
    assert script.read_text().startswith("#!/bin/sh\n") and os.access(script, os.X_OK)
    for shell in SHELLS:
        assert subprocess.run([shell, "-n", str(script)], capture_output=True, check=False).returncode == 0
    if shutil.which("shellcheck") is None:
        (pytest.fail if os.environ.get("DS_REQUIRE_TOOLS") else pytest.skip)("missing shellcheck")
    checked = subprocess.run(
        ["shellcheck", "-s", "sh", str(script)], capture_output=True, text=True, check=False
    )
    assert (checked.returncode, checked.stdout) == (0, "")


def test_help_prints_the_header_and_calls_nothing(trial: Trial, repo_root: Path) -> None:
    """-h and --help print §3's header before any check; what it says a first run fetches is what the
    Dockerfile pins and the base file's db image."""
    header = _fenced("**Header.**")
    for options in (("--help",), ("-h",), ("--bogus", "--port", "--help")):
        helped = trial.run(*options)
        assert (helped.code, helped.out, helped.err, helped.calls) == (0, header, "", [])
    script = (repo_root / "deploy" / "up.sh").read_text().splitlines()
    assert script[1 : script.index("")] == [f"# {line}".rstrip() for line in header.splitlines()]
    dockerfile = (repo_root / "deploy" / "Dockerfile").read_text()
    pinned = set(re.findall(r"([\w.-]+:[\w.-]+)@sha256:[0-9a-f]{64}", dockerfile))
    db = yaml.safe_load((repo_root / "deploy" / "docker-compose.yml").read_text())["services"]["db"]["image"]
    assert len(pinned) == 2 and db == "postgres:16-alpine"
    named = set(re.findall(r"\b[a-z]+:[0-9][\w.-]*\b", header))
    assert named == pinned | {db}


def test_a_fresh_clone_gets_every_file_and_a_token_that_is_never_printed(watched: Trial) -> None:
    """One run of a fresh clone: the ten files and nothing else, each secret straight into its file and
    into no output, argv or environment of any program the run starts, the calls in order, and §3.5's
    block."""
    trial = watched
    before = trial.tree()
    ran = trial.run(env={"WAYLAND_DISPLAY": "wayland-0"})  # no matter on a Mac, which uname says this is
    assert (ran.code, ran.err) == (0, "stub: build\nstub: up\ntoken T-… (first-run): shown once\n")
    assert ran.out == _said() + _closing()
    assert trial.created(before) == sorted(TEN)
    assert {name: mode for name, (mode, *_) in trial.tree().items() if name in before} == {
        name: mode for name, (mode, *_) in before.items()
    }  # nothing the checkout held changed its mode
    for copy, example in (
        ("deploy/config/deltasleuth.yaml", "deploy/config/deltasleuth.example.yaml"),
        ("deploy/config/targets.yaml", "deploy/config/targets.example.yaml"),
        ("deploy/config/llm_profiles.yaml", "deploy/config/llm_profiles.example.yaml"),
        ("deploy/docker-compose.site.yml", "deploy/docker-compose.site.example.yml"),
        (".env", "deploy/env.source.example"),
    ):
        assert (trial.folder / copy).read_bytes() == (REPO / example).read_bytes()
    assert [trial.mode(name) for name in TEN[:3]] == [0o644] * 3  # the app reads them as another user
    held = {
        name: read_secret(f"file:{trial.folder / 'deploy' / 'secrets' / name}")
        for name in ("db_password", "db_dsn", "kek", "tokeniser")
    }
    assert [trial.mode(name) for name in TEN[5:9]] == [0o444] * 4 and trial.mode("deploy/secrets") == 0o700
    assert [len(held[name]) for name in ("db_password", "kek", "tokeniser")] == [48, 64, 64]
    assert len(set(held.values())) == 4 and all(
        re.fullmatch("[0-9a-f]+", held[name]) for name in ("db_password", "kek", "tokeniser")
    )
    assert held["db_dsn"] == f"postgresql://deltasleuth:{held['db_password']}@db:5432/deltasleuth"
    assert (trial.folder / TEN[9]).read_text() == trial.token + "\n"
    assert [trial.mode(name) for name in (TEN[9], ".deltasleuth/tokens", ".deltasleuth")] == [
        0o600,
        0o700,
        0o700,
    ]
    kept_secret = [
        held["db_password"],
        held["kek"],
        held["tokeniser"],
        trial.token,
        trial.token.split("_")[2],
    ]
    assert [secret for secret in kept_secret for text in trial.shown(ran) if secret in text] == []
    unused = {"cut", "ls", "rm", "tail"}  # a kept token's id, a secrets folder's mode, the trap's removal, V6
    assert {line.split()[0] for line in ran.words.splitlines()} == set(TOOLS) - unused
    ambient = {"PWD", "OLDPWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}  # a shell's, and macOS's
    exported = {name for call in ran.calls for name in call["env"]} - ambient
    assert exported == {"PATH", "WAYLAND_DISPLAY", "LC_ALL"}  # the script's one variable is the last
    assert [argv for argv in ran.docker() if I4 & set(argv) or "-f" in argv] == []  # no site file existed
    user, project = getuser(), "label=com.docker.compose.project=deltasleuth"
    networks = [
        ["network", "inspect", "--format", "{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}", name]
        for name in ("n1", "n2", "n3")
    ]
    health = ["inspect", "--format", "{{.State.Health.Status}} {{.RestartCount}}", "c0ffee"]
    assert [(call["tool"], call["argv"]) for call in ran.calls if call["tool"] != "uname"] == [
        ("docker", ["compose", "version", "--short"]),
        ("docker", ["info"]),
        ("docker", ["image", "inspect", VERSION]),
        ("docker", ["ps", "-a", "--filter", project, "--format", STARTED_FROM]),
        ("docker", ["volume", "ls", "-q", "--filter", project]),
        ("docker", ["ps", "--format", "{{.Names}} {{.Ports}}"]),
        *(("curl", [*PROBE, f"http://{host}:8000/"]) for host in LOOPBACK),
        ("docker", ["network", "ls", "-q"]),
        *(("docker", argv) for argv in networks),
        ("docker", ["compose", "--profile", "mocks", "config"]),
        ("docker", ["compose", "build"]),
        ("docker", ["compose", "run", "--rm", "app", "ds", "init"]),
        ("docker", ["compose", "run", "--rm", "app", "ds", "migrate"]),
        ("docker", ["compose", "--profile", "mocks", "up", "-d"]),
        ("docker", ["ps", "-aq", "--filter", project, *OWN]),
        ("docker", health),
        ("sleep", ["2"]),
        ("docker", health),
        ("docker", ["compose", "run", "--rm", "-T", "-e", f"DS_OPERATOR={user}", "app", "ds", "token",
                    "create", "--name", "first-run", "--owner", user, "--role", "spec_author"]),
    ]  # fmt: skip


def test_running_again_overwrites_nothing(trial: Trial) -> None:
    """A second run keeps all ten files to the timestamp: with no option, with the options the files already
    hold (I6), and with the base file's own port and subnet beside the plain copies, which U15 judges as
    Compose renders them. The folder may be spelt another way than Docker recorded it."""
    assert trial.run().code == 0
    first, held = trial.tree(), trial.installed()
    (trial.tmp / "link").symlink_to(trial.folder)
    held["containers"][1]["dir"] = str(trial.tmp / "link" / "deploy")
    for options in ((), ("--port", "8000", "--subnet", "172.30.0.0/24"), ("--project", "deltasleuth")):
        again = trial.run(*options, **held)
        assert (again.code, again.out) == (0, _said(kept=TEN) + _closing())
        assert again.err == "stub: build\nstub: up\n" and trial.tree() == first
        assert again.ds() == ["build", "up", "ds init", "ds migrate", "ds token list"]
        judged = [
            "compose",
            "-f",
            "deploy/docker-compose.yml",
            "-f",
            "deploy/docker-compose.site.yml",
            "config",
        ]
        assert [argv for argv in again.docker() if "-f" in argv] == [judged]  # the one command with -f
        assert [call for call in again.calls if call["tool"] == "curl" or I4 & set(call["argv"])] == []


def test_settings_and_secrets_that_exist_are_never_overwritten(watched: Trial) -> None:
    """Files the folder already holds, written by anyone, stay byte for byte, mode and time included, while
    the missing ones are made beside them: db_dsn alone may be missing, and is made from db_password, which
    no output, argv or environment carries on the way."""
    trial = watched
    ours = {
        TEN[0]: SETTINGS + "## ours\n", TEN[1]: "targets: {}  # ours\n", TEN[2]: "profiles: {}  # ours\n",
        TEN[3]: SITE + "# ours\n", TEN[4]: ENV + "# ours\n", TEN[5]: "our-password\n",
        TEN[7]: "ab" * 32 + "\n", TEN[8]: "cd" * 32 + "\n", TEN[9]: trial.token + "\n",
    }  # fmt: skip
    trial.write(ours)
    for name in (TEN[5], TEN[7], TEN[8]):
        (trial.folder / name).chmod(0o400)
    before = trial.tree()
    ran = trial.run(**trial.installed())
    assert (ran.code, ran.out) == (0, _said(kept=tuple(ours)) + _closing())
    after = trial.tree()  # a folder's time moves when a file is made in it
    assert [name for name, was in before.items() if was[2] is not None and after[name] != was] == []
    assert trial.created(before) == [TEN[6]]
    assert (trial.folder / TEN[6]).read_text() == "postgresql://deltasleuth:our-password@db:5432/deltasleuth"
    secret = ("our-password", "ab" * 32, "cd" * 32, trial.token.split("_")[2])
    assert [text for text in trial.shown(ran) if any(kept in text for kept in secret)] == []
    assert "cut -d_ -f2\n" in ran.words  # the token's id is read from the file's own second field


def test_secrets_and_the_token_are_private_whatever_the_callers_umask(trial: Trial) -> None:
    """The folders the script makes for its secrets and for the token are their owner's alone, and the
    token's file too, under any umask of the caller; a secrets folder that is there and private is used as
    it is (one open to other accounts is U25, among the refusals below)."""
    private = {
        "deploy/secrets": 0o700, **dict.fromkeys(TEN[5:9], 0o444),
        ".deltasleuth": 0o700, ".deltasleuth/tokens": 0o700, TEN[9]: 0o600,
    }  # fmt: skip
    for umask in (0, 0o027, 0o077):
        folder = Trial(trial.tmp / f"{umask:o}", trial.shell)
        if umask == 0o027:  # there already, as DEPLOY's command makes it
            (folder.folder / "deploy" / "secrets").mkdir(mode=0o700)
        assert folder.run(umask=umask).code == 0
        assert {name: folder.mode(name) for name in private} == private
    for special in (0o2700, 0o1700):  # setgid, as a group-shared checkout's folders inherit it, or sticky:
        folder = Trial(trial.tmp / f"{special:o}", trial.shell)  # nobody else has a permission
        (folder.folder / "deploy" / "secrets").mkdir(mode=0o700)
        (folder.folder / "deploy" / "secrets").chmod(special)
        assert folder.run().code == 0, oct(folder.mode("deploy/secrets"))


def test_a_kept_password_an_address_cannot_carry_is_refused_unprinted(watched: Trial) -> None:
    """U26, in U21's one passing case: db_dsn is not made of a kept password that is not safe in an address,
    which would not parse (``%``) or would read the password's tail as a port or a host (``/``, ``@``), and
    nothing the run printed or passed on carries it. One of the characters an address takes as written
    goes into db_dsn."""
    trial = watched
    for at, password in enumerate(("Tr1al%Pass-CANARY-9f3k", "Zm9v/YmFy-CANARY", "p@ssw0rd-CANARY")):
        folder = Trial(trial.tmp / str(at), trial.shell, watched=True)
        folder.write({TEN[5]: password + "\n"} | _KEPT)
        before = folder.tree()
        ran = folder.run()
        assert (ran.code, ran.out, ran.err) == (2, "", f"error: {U26}\n")
        assert folder.tree() == before and ran.ds() == []
        assert [text for text in folder.shown(ran) if "CANARY" in text] == []
    plain = "AZaz09._~-"
    trial.write({TEN[5]: plain + "\n"} | _KEPT)
    assert trial.run().code == 0
    assert (trial.folder / TEN[6]).read_text() == f"postgresql://deltasleuth:{plain}@db:5432/deltasleuth"


def test_a_link_to_a_file_that_is_not_there_is_never_replaced(trial: Trial) -> None:
    """I1 for links: secrets, settings and .env kept as links into a folder that is not mounted now are
    refused (U27), where new files and new keys would take their place; with their files there they are
    kept, and stay links."""
    held = {
        TEN[5]: "aa" * 24, TEN[6]: f"postgresql://deltasleuth:{'aa' * 24}@db:5432/deltasleuth",
        TEN[7]: "ab" * 32, TEN[8]: "cd" * 32, TEN[0]: SETTINGS, TEN[4]: ENV,
    }  # fmt: skip
    links = {name: f"vault/{Path(name).name}" for name in held}
    trial.link(links)
    before = trial.tree()
    ran = trial.run()
    assert (ran.code, ran.out, ran.err) == (2, "", f"error: {U27.format(file=TEN[4])}\n")  # the first asked
    assert trial.tree() == before and ran.ds() == [] and not (trial.tmp / "vault").exists()
    (trial.tmp / "vault").mkdir()
    for name, text in held.items():
        (trial.tmp / links[name]).write_text(text)
    ran = trial.run()
    assert (ran.code, ran.out) == (0, _said(kept=tuple(held)) + _closing())
    assert {name: os.readlink(trial.folder / name) for name in links} == {
        name: str(trial.tmp / target) for name, target in links.items()
    }
    assert {name: (trial.tmp / links[name]).read_text() for name in held} == held


def test_options_make_a_second_installation(trial: Trial) -> None:
    """--project, --port and --subnet shape a new folder's three files (§4), and pass when given again."""
    before = trial.tree()
    options = ("--project", "ds2", "--port", "18000", "--subnet", "172.31.10.0/24")
    ran = trial.run(*options)
    assert (ran.code, ran.out) == (0, _said("ds2") + _closing("ds2", "http://localhost:18000"))
    assert trial.created(before) == sorted(TEN)
    settings = (trial.folder / TEN[0]).read_text()
    assert settings == SETTINGS.replace(
        "  public_url: http://localhost:8000 ", "  public_url: http://localhost:18000 "
    )
    assert (
        settings != SETTINGS
        and load_settings(trial.folder / TEN[0]).auth.public_url == "http://localhost:18000"
    )
    written = _fenced("it is this file instead")
    site = (
        written.replace("<port>", "18000")
        .replace("<subnet>", "172.31.10.0/24")
        .replace("<gateway>", "172.31.10.1")
    )
    assert (trial.folder / TEN[3]).read_text() == site and "<" not in site
    assert (trial.folder / TEN[4]).read_text() == ENV + "COMPOSE_PROJECT_NAME=ds2\n"
    asked = [" ".join(call["argv"]) for call in ran.calls]
    assert [line for line in asked if "deltasleuth" in line.replace("-e DS_OPERATOR", "")] == [
        "image inspect " + VERSION
    ]
    assert "volume ls -q --filter label=com.docker.compose.project=ds2" in asked
    assert [line for line in asked if "max-time" in line] == [
        f"-q -g -s -o /dev/null --noproxy * --max-time 2 http://{host}:18000/" for host in LOOPBACK
    ]
    assert f"ps -aq --filter label=com.docker.compose.project=ds2 {' '.join(OWN)}" in asked
    first, held = trial.tree(), trial.installed("ds2", 18000)
    for again in (options, ()):
        kept = trial.run(*again, **held)
        assert (kept.code, kept.out) == (
            0,
            _said("ds2", kept=TEN) + _closing("ds2", "http://localhost:18000"),
        )
        assert trial.tree() == first
    port_only, subnet_only = (Trial(trial.tmp / name, trial.shell) for name in ("port", "subnet"))
    assert (
        port_only.run("--port", "18001").code == 0 and subnet_only.run("--subnet", "10.99.0.0/24").code == 0
    )
    lines = site.splitlines(keepends=True)
    assert (port_only.folder / TEN[3]).read_text() == "".join(lines[:-1]).replace("18000", "18001")
    assert (subnet_only.folder / TEN[3]).read_text() == "".join(lines[:-2] + lines[-1:]).replace(
        "172.31.10", "10.99.0"
    )
    assert (subnet_only.folder / TEN[0]).read_text() == SETTINGS and (
        port_only.folder / TEN[4]
    ).read_text() == ENV


def test_options_never_change_an_existing_file(trial: Trial) -> None:
    """An option that differs from what the folder's files say is refused, and no file is touched."""
    assert trial.run().code == 0
    first, held = trial.tree(), trial.installed()
    refused = {
        (
            "--project",
            "ds2",
        ): '.env makes this folder project "deltasleuth", not "ds2": this script changes no'
        " existing file (leave --project out, or edit .env yourself)",
        ("--port", "18000"): f"{TEN[0]} exists and does not say port 18000 in public_url: {UNCHANGED}",
        (
            "--subnet",
            "172.31.10.0/24",
        ): f"{TEN[3]} exists and does not say subnet 172.31.10.0/24: {UNCHANGED}",
    }
    for options, message in refused.items():
        ran = trial.run(*options, **held)
        assert (ran.code, ran.out, ran.err) == (2, "", f"error: {message}\n")
        assert trial.tree() == first and ran.ds() == []


@dataclass(frozen=True)
class Refused:
    label: str  # the row of §3.2
    message: str
    options: tuple[str, ...] = ()
    files: dict[str, str] = field(default_factory=dict)  # what the folder holds beyond a fresh clone
    links: dict[str, str] = field(default_factory=dict)  # links there, each to a path that is not there
    folders: dict[str, int] = field(default_factory=dict)  # folders there, each with its mode
    env: dict[str, str] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)  # of the scenario
    without: str | None = None  # a program that is not installed
    elsewhere: bool = False  # run from another folder than the checkout's
    above: str = ""  # what Compose said first


def _no(label: str, message: str, *options: str, **how: Any) -> Refused:
    fields = ("files", "links", "folders", "env", "without", "elsewhere", "above")
    named = {name: how.pop(name) for name in fields if name in how}
    return Refused(label, message, options, changes=how, **named)


_TWO_FILES = ENV.replace(":deploy/docker-compose.source.yml", "")  # a loaded release's .env
_QUOTED = ENV.replace("COMPOSE_FILE=", 'COMPOSE_FILE="').replace(".source.yml\n", '.source.yml"\n')
_THEIRS = {
    "project": "deltasleuth",
    "dir": "{tmp}/other/deploy",
    "running": False,
    "name": "deltasleuth-app-1",
}
# Another installation's app, as ``docker ps`` shows it: this machine's port 18000, the container's own 8000.
_GUIDE = {"project": "dsguide", "dir": "/srv/guide/deploy", "running": True, "name": "dsguide-app-1",
          "ports": "127.0.0.1:18000->8000/tcp"}  # fmt: skip
_OFFICE = _at("https://deltasleuth.bank.example")
_DEV = (REPO / "deploy" / "config" / "deltasleuth.dev.example.yaml").read_text()
_PUBLISHES_8001 = SITE.replace("    # ports: !override", "    ports: !override")
_NOT_YAML = "yaml: deploy/docker-compose.site.yml is not a compose file\n"
# With a db_password beside them, U21's one passing case: step 2 makes db_dsn.
_KEPT = {"deploy/secrets/kek": "ab" * 32 + "\n", "deploy/secrets/tokeniser": "cd" * 32 + "\n"}
# A container that carries the project's label and not its folder's: Compose itself made no such one.
_BARE = {"project": "deltasleuth", "dir": "", "running": False, "name": "deltasleuth-db-1"}
REFUSALS = [
    _no("U1", f"unknown option --bogus {OPTIONS}", "--bogus"),
    _no("U1", f"unknown option ds2 {OPTIONS}", "--port", "18000", "ds2"),
    _no("U1", f"unknown option --port=18000 {OPTIONS}", "--port=18000"),
    _no("U2", f"--port needs a value {OPTIONS}", "--project", "ds2", "--port"),
    _no("U2", f"--project needs a value {OPTIONS}", "--project", ""),
    *(_no("U3", f"--project: {NAME}", "--project", name)
      for name in ("ds-", "a__-b", "a___b", "DS2", "-ds", "d s", "ds\n2")),
    *(_no("U3", IN_ENV, files={".env": ENV + line})
      for line in (
          "COMPOSE_PROJECT_NAME=ds-\n", "COMPOSE_PROJECT_NAME=a__-b\n", 'COMPOSE_PROJECT_NAME="dsq"\n',
          "export COMPOSE_PROJECT_NAME=ds2\n", "COMPOSE_PROJECT_NAME=ds2\nCOMPOSE_PROJECT_NAME=ds2\n",
          " COMPOSE_PROJECT_NAME=ds2\n", "COMPOSE_PROJECT_NAME=ds2 \n", "COMPOSE_PROJECT_NAME=ds2\r\n",
          "COMPOSE_PROJECT_NAME=\n", "COMPOSE_PROJECT_NAME = ds2\n",
      )),
    *(_no("U4", PORT, "--port", port) for port in ("80", "1023", "65536", "08000", "8000x", "-1", "999999")),
    *(_no("U5", NET, "--subnet", net)
      for net in ("10.0.0.0/16", "300.1.1.0/24", "172.31.7.1/24", "172.31.7.0", "01.2.3.0/24")),
    _no("U6", U6, elsewhere=True),
    _no("U7", U7, env={"COMPOSE_FILE": "deploy/docker-compose.yml"}),
    _no("U7", U7, env={"COMPOSE_PROJECT_NAME": "ds2"}),
    _no("U7", U7, env={"COMPOSE_FILE": ""}),
    *(_no("U8", U8.format(tool=tool), without=tool) for tool in ("docker", "openssl")),
    _no("U9", "docker compose (version 2) is not installed", compose=None),
    _no("U10", "Docker is not running, or this account may not use it", info=1),
    *(_no("U23", f"{file}.new is left by an interrupted run: {AGAIN}", files={f"{file}.new": "x\n"})
      for file in TEN),
    *(_no("U23", f"{file}.new is left by an interrupted run: {AGAIN}", links={f"{file}.new": "elsewhere"})
      for file in (TEN[4], TEN[9])),
    *(_no("U27", U27.format(file=file), links={file: "vault/not-mounted"}) for file in TEN),
    *(_no("U22", f"{file} is empty: {AGAIN}", files={file: ""}) for file in TEN[5:]),
    *(_no("U12", U12, files={".env": text})
      for text in (
          _TWO_FILES,
          _QUOTED,
          ENV.replace("COMPOSE_FILE=", "export COMPOSE_FILE="),
          ENV + _TWO_FILES,  # a second line
          "COMPOSE_PROJECT_NAME=ds2\n",
          ENV.replace(".source.yml\n", ".source.yml \n"),
          ENV.replace(".source.yml\n", ".source.yml.bak\n"),
          ENV.replace("\n", "\r\n"),
      )),
    _no("U11", U11.format(a="deltasleuth", b="b"), "--project", "b", files={".env": ENV}),
    _no("U11", U11.format(a="a", b="b"), "--project", "b", files={".env": ENV + "COMPOSE_PROJECT_NAME=a\n"}),
    _no("U13", U13, images=[VERSION]),
    _no("U13", U13, "--project", "ds2", "--port", "18000", images=[VERSION]),
    *(_no("U14", _u14(why), files={TEN[0]: text})
      for why, text in (
          ("auth.oidc is on", re.sub(r"(?m)^# ", "", SETTINGS)),
          ("it has no auth block", _DEV),
          (NO_LINE, SETTINGS + "  public_url: http://localhost:8000\n"),
          (NO_LINE, "auth: {public_url: http://localhost:8000}\n"),
          ('public_url is "http://localhost:8000"', _at('"http://localhost:8000"')),
          ("public_url is https://deltasleuth.bank.example", _OFFICE),
          ("public_url is http://ds.bank.example:8000", _at("http://ds.bank.example:8000")),
          ("public_url is http://localhost", _at("http://localhost")),
          ("public_url is http://localhost:8000/", _at("http://localhost:8000/")),
      )),
    _no("U15", U15.format(file=TEN[0], what="port 18000 in public_url"), "--port", "18000",
        files={TEN[0]: SETTINGS}),
    _no("U15", U15.format(file=TEN[3], what="port 8000"), files={TEN[3]: _PUBLISHES_8001}),
    _no("U15", U15.format(file=TEN[3], what="port 18000"), "--port", "18000", files={TEN[3]: SITE}),
    _no("U15", U15.format(file=TEN[3], what="subnet 172.31.10.0/24"), "--subnet", "172.31.10.0/24",
        files={TEN[3]: SITE}),
    _no("V1", V1, files={TEN[3]: "services: [\n"}, above=_NOT_YAML),
    _no("U24", U24.format(version="2.20.0"), "--port", "18000", compose="2.20.0"),
    _no("U24", U24.format(version="v2.24.3"), "--subnet", "172.31.10.0/24", compose="v2.24.3"),
    _no("U24", U24.format(version="2.24"), "--port", "18000", compose="2.24"),
    _no("U24", U24.format(version="dev"), "--port", "18000", compose="dev"),
    *(_no("U25", U25, folders={"deploy/secrets": mode}) for mode in (0o755, 0o750, 0o711, 0o701)),
    _no("U25", U25, folders={"deploy/secrets": 0o755}, files={TEN[5]: "aa" * 24 + "\n"} | _KEPT),
    *(_no("U21", U21, files={f"deploy/secrets/{name}": "x\n" for name in names})
      for names in (("db_dsn",), ("db_password",), ("db_password", "db_dsn", "kek"), ("kek", "tokeniser"))),
    *(_no("U26", U26, files={TEN[5]: password + "\n"} | _KEPT)
      for password in ("Tr1al%Pass-9f3k", "Zm9v/YmFyQmF6", "p@ssw0rd-tail", "two words", "a:b", "pässword")),
    _no("U16", U16.format(project="deltasleuth", dir="{tmp}/other/deploy"),
        files={"../other/deploy/docker-compose.yml": ""}, containers=[_THEIRS]),
    _no("U16", U16.format(project="ds2", dir="{tmp}/gone/deploy"), "--project", "ds2",
        containers=[_THEIRS | {"project": "ds2", "dir": "{tmp}/gone/deploy"}]),
    _no("U16", U16.format(project="deltasleuth", dir=UNNAMED), containers=[_BARE | {"running": True}, _BARE]),
    _no("U20", U20, volumes={"deltasleuth": ["deltasleuth_pgdata"]}),
    _no("U17", U17.format(port=8000, name="other-app-1"),
        containers=[_GUIDE, _GUIDE | {"name": "other-app-1", "ports": "127.0.0.1:8000->8000/tcp"}]),
    _no("U17", U17.format(port=18000, name="dsguide-app-1"), "--port", "18000", containers=[_GUIDE]),
    _no("U17", U17.format(port=5432, name="db-1"), "--port", "5432",
        containers=[_GUIDE | {"name": "db-1", "ports": "0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp"}]),
    *(_no("U18", U18.format(port=18021), "--port", "18021", curl=code) for code in (0, 28, 52)),
    _no("U19", U19.format(subnet="172.30.0.0/24", name="other_default"),
        networks={"n1": "bridge 172.17.0.0/16 ", "n2": "other_default 172.30.0.0/24 "}),
    _no("U19", U19.format(subnet="172.31.10.0/24", name="bank"), "--subnet", "172.31.10.0/24",
        networks={"n1": "host ", "n2": "bank 10.1.0.0/16 172.31.10.0/24 "}),
]  # fmt: skip


@pytest.mark.parametrize(
    "refused", [pytest.param(case, id=f"{case.label}-{at}") for at, case in enumerate(REFUSALS)]
)
def test_every_early_refusal_creates_nothing(trial: Trial, refused: Refused) -> None:
    """Every row of §3.2, worded as the contract's table words it: one error line, exit 2, the folder as it
    was, and nothing built, run or started."""
    trial.write(refused.files)
    trial.link(refused.links)
    for name, mode in refused.folders.items():
        (trial.folder / name).mkdir(exist_ok=True)
        (trial.folder / name).chmod(mode)
    if refused.without:
        (trial.bin / refused.without).unlink()
    before = trial.tree()
    ran = trial.run(
        *refused.options, env=refused.env, cwd=trial.tmp if refused.elsewhere else None, **refused.changes
    )
    message = refused.message.replace("{tmp}", str(trial.tmp))
    assert (ran.code, ran.out, ran.err) == (2, "", f"{refused.above}error: {message}\n")
    assert (EARLY | LATER)[refused.label].fullmatch(message)
    assert trial.tree() == before and ran.ds() == []
    assert not (trial.tmp / "elsewhere").exists()  # nothing was written through a link


def test_every_row_of_the_contracts_table_is_tried() -> None:
    assert {case.label for case in REFUSALS} == set(EARLY) | {"V1"}  # a site file Compose refuses is V1


def test_a_fresh_folder_passes_where_only_its_own_things_are_there(trial: Trial) -> None:
    """What does not refuse: an old Compose without options (nothing needs !override), the project's own
    network on the subnet, another project's container on another port, and a machine without curl."""
    (trial.bin / "curl").unlink()
    ours = {"n1": "deltasleuth_default 172.30.0.0/24 ", "n2": "other_default 172.31.0.0/24 "}
    theirs = [
        _GUIDE,
        _GUIDE | {"name": "db-1", "ports": "5432/tcp"},
        _GUIDE | {"name": "web-1", "ports": "0.0.0.0:80->8000/tcp"},
    ]
    ran = trial.run(compose="2.20.0", networks=ours, containers=theirs)
    assert (ran.code, ran.out) == (0, _said() + _closing())
    assert {call["tool"] for call in ran.calls} == {"docker", "sleep", "uname"}


def test_port_alone_moves_a_first_installation_whose_port_is_taken(trial: Trial) -> None:
    """Where only port 8000 is taken, by a container (U17) or by a program outside Docker (U18), the refusal
    says that --port N is enough, as the header does, and it is: the folder then runs under the default
    project and subnet, the port in its settings and its site file, and .env without a project line."""
    assert "port 8000 is taken, --port N alone is enough." in _fenced("**Header.**")
    taken = [_GUIDE | {"name": "web-1", "ports": "0.0.0.0:8000->80/tcp"}]
    before = trial.tree()
    refused = (
        ({"containers": taken}, U17.format(port=8000, name="web-1")),
        ({"curl": 0}, U18.format(port=8000)),
    )
    for changes, message in refused:
        ran = trial.run(**changes)
        assert (ran.code, ran.err) == (2, f"error: {message}\n") and message.endswith(MOVE)
        assert trial.tree() == before
    ran = trial.run("--port", "18001", containers=taken)
    assert (ran.code, ran.out) == (0, _said() + _closing(address="http://localhost:18001"))
    site = (trial.folder / TEN[3]).read_text()
    assert '    ports: !override ["127.0.0.1:18001:8000"]\n' in site and "networks:" not in site
    assert (trial.folder / TEN[4]).read_text() == ENV
    assert load_settings(trial.folder / TEN[0]).auth.public_url == "http://localhost:18001"


def test_the_port_probe_asks_no_proxy(trial: Trial) -> None:
    """U18's two curl calls, word for word, with the shell's proxy set: 7 from both (nothing listens) lets the
    run go on; 0 and 28 from either refuse, so a program on ``[::1]`` alone, which ``http://localhost``
    reaches before what Docker publishes on ``127.0.0.1``, is seen. The real curl, given those words, asks
    the port and never the proxy, and tells the two addresses apart."""
    proxy = {
        "http_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "all_proxy": "http://127.0.0.1:9",
    }
    for answered in ({"curl": 0}, {"curl": 28}, {"curl6": 0}, {"curl6": 28}):
        ran = trial.run(env=proxy, **answered)
        assert (ran.code, ran.err) == (2, f"error: {U18.format(port=8000)}\n")
        asked_of = [call["argv"][-1] for call in ran.calls if call["tool"] == "curl"]
        assert asked_of == [f"http://{host}:8000/" for host in LOOPBACK[: 2 if "curl6" in answered else 1]]
    ran = trial.run(env=proxy)
    probes = [call for call in ran.calls if call["tool"] == "curl"]
    assert ran.code == 0 and [probe["argv"] for probe in probes] == [
        [*PROBE, f"http://{host}:8000/"] for host in LOOPBACK
    ]
    assert (
        probes[0]["env"]["http_proxy"] == "http://127.0.0.1:9"
    )  # it reaches curl, which is told to ignore it
    if shutil.which("curl") is None:
        return
    asked: list[str] = []

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            asked.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_: Any) -> None:
            pass

    class OnV6(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    def probe(address: str) -> int:
        return subprocess.run(["curl", *PROBE, f"http://{address}/"], env=env, check=False).returncode

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        closed = free.getsockname()[1]
    env = {"PATH": os.environ["PATH"]} | dict.fromkeys(proxy, f"http://127.0.0.1:{server.server_address[1]}")
    try:
        codes = [probe(f"127.0.0.1:{port}") for port in (closed, server.server_address[1])]
    finally:
        server.shutdown()
        server.server_close()
    assert codes == [7, 0] and asked == ["/"]  # the open port answered for itself, once
    try:
        other = OnV6(("::1", 0), Proxy)  # a program on the other loopback address alone
    except OSError:
        return  # this machine has no IPv6 loopback
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        codes = [probe(f"{host}:{other.server_address[1]}") for host in LOOPBACK]
    finally:
        other.shutdown()
        other.server_close()
    assert codes == [7, 0] and asked == ["/", "/"]  # only the second call finds it


def test_it_refuses_an_office_or_a_release_installation(trial: Trial) -> None:
    """The script is a trial's (D2): an https or named address, single sign-on, a .env of a loaded release
    and a loaded release image each stop it before anything is created, built, run or started. The .env
    that U13's advice makes goes ahead under its own project."""
    cases = (
        ({TEN[0]: _OFFICE}, {}, _u14("public_url is https://deltasleuth.bank.example")),
        ({TEN[0]: _at("http://ds.bank.example:8000")}, {}, _u14("public_url is http://ds.bank.example:8000")),
        ({TEN[0]: re.sub(r"(?m)^# ", "", _OFFICE)}, {}, _u14("auth.oidc is on")),
        ({".env": _TWO_FILES}, {}, U12),
        ({}, {"images": [VERSION]}, U13),
    )  # fmt: skip
    for at, (files, changes, message) in enumerate(cases):
        folder = Trial(trial.tmp / str(at), trial.shell)
        folder.write(files)
        before = folder.tree()
        ran = folder.run(**changes)
        assert (ran.code, ran.out, ran.err) == (2, "", f"error: {message}\n")
        assert folder.tree() == before and ran.ds() == []
    advised = Trial(trial.tmp / "advised", trial.shell)
    advised.write({".env": ENV + "COMPOSE_PROJECT_NAME=trial2\n"})
    ran = advised.run(images=[VERSION])
    assert (ran.code, ran.out) == (0, _said("trial2", kept=(".env",)) + _closing("trial2"))
    assert ["image", "inspect", VERSION] not in ran.docker()  # a .env is there: the folder has chosen


def test_it_takes_over_no_other_folders_compose_project(trial: Trial) -> None:
    """A project that any container, running or stopped, says was started from another folder is never
    acted on, nor one of which a container names no folder, wherever Docker lists it: asking Docker for the
    containers' folders is the last thing the script does, in a fresh folder and in one that holds every
    file. Under a project name of its own the folder goes ahead."""
    (trial.tmp / "other" / "deploy").mkdir(parents=True)
    ours = trial.installed()["containers"]
    there, gone = str(trial.tmp / "other" / "deploy"), str(trial.tmp / "gone" / "deploy")
    cases = (
        ([_THEIRS | {"dir": there, "running": True}], there),
        ([_THEIRS | {"dir": there}], there),  # stopped: seen only with -a
        ([_THEIRS | {"dir": gone}], gone),
        ([*ours, _THEIRS | {"dir": there}], there),  # one container of ours does not make the project ours
        ([_BARE | {"running": True}, _BARE], UNNAMED),  # the project's label and no folder's: not ours either
        ([*ours, _BARE], UNNAMED),  # listed last, where an empty line is easily lost
        ([_BARE, *ours], UNNAMED),
    )
    for complete in (False, True):
        if complete:
            assert trial.run().code == 0
        before = trial.tree()
        for containers, named in cases:
            ran = trial.run(containers=containers)
            assert (ran.code, ran.out) == (2, "")
            assert ran.err == "error: " + U16.format(project="deltasleuth", dir=named) + "\n"
            assert ran.docker()[-1][:4] == [
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=deltasleuth",
            ]
            assert trial.tree() == before and ran.ds() == []
        unasked = trial.run(ps=1)  # Docker would not say: the script stops there too
        assert (unasked.code, unasked.out, unasked.err) == (1, "", "stub: ps\n")
        assert trial.tree() == before and unasked.ds() == []
    second = Trial(trial.tmp / "second", trial.shell)
    ran = second.run(
        "--project", "ds2", "--port", "18000", "--subnet", "172.31.10.0/24", containers=cases[0][0]
    )
    assert ran.code == 0 and ran.ds()[:2] == ["build", "up"]


@pytest.mark.parametrize("signum", ["SIGHUP", "SIGINT", "SIGQUIT", "SIGPIPE", "SIGTERM"])
@pytest.mark.parametrize("step", ["token create", "migrate"])
def test_an_interrupt_leaves_no_new_file(trial: Trial, step: str, signum: str) -> None:
    """HUP (a closed terminal, a dropped session), INT, QUIT, PIPE and TERM, sent as a terminal sends them, to
    the script and the command it waits for, end the run through the trap under every shell: the token's
    .new file, which exists while ``ds token create`` runs, is removed, and every file completed before
    stays."""
    proc = trial.start(block=step)
    waited = time.monotonic()
    while not (trial.stub / "blocked").exists():
        assert proc.poll() is None and time.monotonic() - waited < 60
        time.sleep(0.01)
    assert (trial.folder / f"{TEN[9]}.new").exists() == (step == "token create")
    os.killpg(proc.pid, signal.Signals[signum])
    ran = trial.ended(proc)
    assert ran.code == 128 + signal.Signals[signum] and not ran.out.endswith(_closing())
    assert [name for name in trial.tree() if name.endswith(".new")] == []
    assert [name for name in TEN if (trial.folder / name).exists()] == list(TEN[:9])


def test_a_restart_loop_ends_at_once_with_the_apps_last_error_line(trial: Trial) -> None:
    """V6: a restart count above the first reading ends the wait at that reading, after the last ``error:``
    line of the app's log; a count that was not 0 and does not rise is no failure; an app that is never
    healthy is given 90 more readings, 2 s apart."""
    logs = (
        '{"action": "system.start"}\nerror: invalid settings /etc/deltasleuth/deltasleuth.yaml: first\n'
        "INFO: retrying\nerror: invalid targets /etc/deltasleuth/targets.yaml: last\nTraceback: none\n"
    )
    ran = trial.run(health=["starting 0", "starting 0", "starting 2", "healthy 2"], logs=logs)
    assert ran.code == 2 and ran.out == _said().partition("== 6/6")[0]
    assert (
        ran.err
        == f"stub: build\nstub: up\nerror: invalid targets /etc/deltasleuth/targets.yaml: last\nerror: {V6}\n"
    )
    assert len([argv for argv in ran.docker() if argv[0] == "inspect"]) == 3
    assert ran.docker()[-1] == ["compose", "logs", "--no-log-prefix", "--tail", "40", "app"]
    assert LATER["V6"].fullmatch(V6) and not (trial.folder / TEN[9]).exists()
    steady = Trial(trial.tmp / "steady", trial.shell)
    assert steady.run(health=["starting 3", "unhealthy 3", "starting 3", "healthy 3"]).code == 0
    never = Trial(trial.tmp / "never", trial.shell)
    ran = never.run(health=["starting 0"])
    assert (ran.code, ran.err) == (2, f"stub: build\nstub: up\nerror: {V6}\n")  # it logged no error line
    assert len([argv for argv in ran.docker() if argv[0] == "inspect"]) == 91
    assert [call["argv"] for call in ran.calls if call["tool"] == "sleep"] == [["2"]] * 90
    none = Trial(trial.tmp / "none", trial.shell)
    ran = none.run(app="")  # Compose names no app container
    assert (ran.code, ran.err) == (2, f"stub: build\nstub: up\nerror: {V6}\n")
    assert [argv for argv in ran.docker() if argv[0] == "inspect"] == []


def test_the_health_wait_reads_the_services_own_container(trial: Trial) -> None:
    """Step 5 beside a container of ``docker compose run … app`` (an agent's ``ds mcp``, or one left without
    --rm), which ``docker compose ps -a`` lists with the service's: the script asks Docker for the service's
    own container, reads that one id's health and goes on to the token; of two ids it reads the first."""
    assert trial.run().code == 0
    held = trial.installed() | {"oneoff": "0ne0ff\n", "health": ["healthy 0"]}
    for app in ("c0ffee\n", "c0ffee\nbeef\n"):
        (trial.folder / TEN[9]).unlink()  # as W1 advises, for a new token
        ran = trial.run(**held | {"app": app})
        assert ran.code == 0 and ran.out.endswith(_closing())
        project = "label=com.docker.compose.project=deltasleuth"
        assert [argv for argv in ran.docker() if "-aq" in argv] == [["ps", "-aq", "--filter", project, *OWN]]
        assert [argv[-1] for argv in ran.docker() if argv[0] == "inspect"] == ["c0ffee"]
        assert [call for call in ran.calls if call["tool"] == "sleep"] == []
        assert (trial.folder / TEN[9]).read_text() == trial.token + "\n"


_ALSO = ENV.replace(".source.yml\n", ".source.yml:deploy/{file}.yml\n")  # a fourth file, which .env may name
_CA = _ALSO.format(file="docker-compose.ca")  # the release's CA layer, last: its image is not this build's
_MOVED = 'services: {app: {ports: !override ["127.0.0.1:8001:8000"]}}\n'
_DS = {"init": 0, "migrate": 0, "token create": 0, "token list": 0}
_CREATE = "ds token create --name first-run --owner {user} --role spec_author"
_UP = ["build", "up", "ds init", "ds migrate"]  # as ``Ran.ds`` lists a run that started the containers
STOPPED = [  # (the row of §3.4, its message, the folder's files, the scenario, the files made, what was run)
    ("V1", V1, {}, {"config": {"code": 15, "err": "yaml: no\n"}}, TEN[:5], []),
    ("V2", V2, {".env": "COMPOSE_FILE=deploy/docker-compose.source.yml\n"}, {}, TEN[:4], []),
    ("V3", V3, {".env": _CA}, {}, TEN[:4], []),
    ("V4", V4, {".env": _ALSO.format(file="moved"), "deploy/moved.yml": _MOVED}, {}, TEN[:4], []),
    ("V5", V5.format(step="the build"), {}, {"build": 17}, TEN[:9], ["build"]),
    ("V5", V5.format(step="ds init"), {}, {"ds": _DS | {"init": 2}}, TEN[:9], ["build", "ds init"]),
    ("V5", V5.format(step="ds migrate"), {}, {"ds": _DS | {"migrate": 2}}, TEN[:9], _UP[:1] + _UP[2:]),
    ("V5", V5.format(step="docker compose up"), {}, {"up": 1}, TEN[:9], _UP),
    ("V6", V6, {}, {"app": ""}, TEN[:9], _UP),
    ("V7", V7, {}, {"ds": _DS | {"token create": 2}}, TEN[:9], [*_UP, _CREATE]),
]  # fmt: skip


@pytest.mark.parametrize(
    ("label", "message", "files", "changes", "made", "run"),
    STOPPED,
    ids=[f"{row[0]}-{at}" for at, row in enumerate(STOPPED)],
)
def test_later_refusals_keep_what_exists(
    trial: Trial,
    label: str,
    message: str,
    files: dict[str, str],
    changes: dict[str, Any],
    made: tuple[str, ...],
    run: list[str],
) -> None:
    """V1 to V7: one error line after what Docker or ds said, exit 2, no .new file, no later step, and every
    file made so far kept; with the cause gone, a second run completes the rest and rewrites none of them."""
    trial.write(files)
    before = trial.tree()
    ran = trial.run(**changes)
    assert ran.code == 2 and ran.err.endswith(f"error: {message}\n") and ran.err.count("error: ") == 1
    assert LATER[label].fullmatch(message)
    assert trial.created(before) == sorted(made)
    assert ran.ds() == [line.format(user=getuser()) for line in run]
    stopped = trial.tree()
    if label in ("V2", "V3", "V4"):  # the cause is the folder's own .env, which the script leaves alone
        return
    again = trial.run(**trial.installed() | {"volumes": {}})
    assert again.code == 0 and trial.created(before) == sorted(TEN)
    assert {name: again_ for name, again_ in trial.tree().items() if name in stopped and name in TEN} == {
        name: was for name, was in stopped.items() if name in TEN
    }


def test_an_example_without_its_address_line_gets_no_other_port(trial: Trial) -> None:
    """V8: the port goes on the settings example's one ``public_url`` line or nowhere."""
    example = trial.folder / "deploy" / "config" / "deltasleuth.example.yaml"
    example.write_text(SETTINGS.replace("http://localhost:8000 ", "'http://localhost:8000' "))
    before = trial.tree()
    ran = trial.run("--port", "18000")
    said = (
        'deploy/config/deltasleuth.example.yaml does not hold the line "  public_url: http://localhost:8000 "'
        " once: this script writes the port nowhere else"
    )
    assert (ran.code, ran.err) == (2, f"error: {said}\n") and LATER["V8"].fullmatch(said)
    assert list(trial.tree()) == list(before) and ran.ds() == []  # the settings' .new file came and went


def test_a_failed_token_leaves_no_file(watched: Trial) -> None:
    """V7: unless ``ds token create`` succeeds and prints exactly one line, a token, nothing is kept of what
    it printed and none of it is shown or passed on."""
    trial = watched
    printed = (
        {"ds": _DS | {"token create": 2}, "token": ""},
        {"ds": _DS | {"token create": 2}},  # printed, then failed
        {"token": ""},
        {"token": f"warning: slow\n{trial.token}\n"},
        {"token": f"{trial.token}\n{trial.token}\n"},
        {"token": trial.token.removeprefix("dst_") + "\n"},
    )
    for at, changes in enumerate(printed):
        folder = Trial(trial.tmp / str(at), trial.shell, watched=True, token=trial.token)
        ran = folder.run(**changes)
        assert ran.code == 2 and ran.err.endswith(f"token T-… (first-run): shown once\nerror: {V7}\n")
        assert [name for name in folder.tree() if name.startswith(".deltasleuth/tokens/")] == []
        assert [text for text in folder.shown(ran) if trial.token.split("_")[2] in text] == []


def test_a_kept_token_the_database_does_not_know_is_warned_of(watched: Trial) -> None:
    """W1: a kept token file whose id ``ds token list`` does not show as active is warned of, by its id and
    never its secret, and the run still ends 0. Another active token of the same name does not silence it;
    the file's own id, active, does. A list that fails ends the run with V5 under what ``ds`` said, and no
    closing block: it opens what a start opens, so a mistake in the targets file that the running app has
    not read, which ``ds init``, ``ds migrate`` and ``up -d`` pass, is not reported as a running trial."""
    trial = watched
    assert trial.run().code == 0
    held = trial.installed()
    line = "T-{id} (first-run)  spec_author  {state}  expires 2027-01-03 10:00Z  owner 'me'  last used -\n"
    mine, other = (line.replace("{id}", ident) for ident in (trial.token_id, "0123456789abcdef"))
    warned = W1.format(id=trial.token_id)
    listed = {
        other.format(state="active"): warned,
        mine.format(state="revoked") + other.format(state="active"): warned,
        mine.format(state="expired"): warned,
        "": warned,
        other.format(state="active") + mine.format(state="active"): "",
    }
    for tokens_, warning in listed.items():
        ran = trial.run(**held | {"tokens": tokens_})
        assert (ran.code, ran.out) == (0, _said(kept=TEN) + _closing())
        assert ran.err == "stub: build\nstub: up\n" + warning
        assert [text for text in trial.shown(ran) if trial.token.split("_")[2] in text] == []
    assert LATER["W1"].fullmatch(warned.rstrip("\n"))
    said = "error: /targets/orders-v1/rate_per_sec [schema.additionalProperties] unexpected member\n"
    first = trial.tree()
    unread = trial.run(**held | {"ds": _DS | {"token list": 2}, "said": said})
    assert (unread.code, unread.out) == (2, _said(kept=TEN))  # every step, and no closing block
    stopped = V5.format(step="ds token list")
    assert unread.err == f"stub: build\nstub: up\n{said}error: {stopped}\n" and LATER["V5"].fullmatch(stopped)
    assert unread.ds() == [*_UP, "ds token list"] and trial.tree() == first


def test_the_closing_block_names_the_desktops_clipboard_tool_and_runs_none(trial: Trial) -> None:
    """§3.5 outside macOS: wl-copy under Wayland, else xclip, each with the install hint where it is not
    installed; no clipboard tool is ever run (I5)."""
    hint = " (install xclip or wl-clipboard to copy it; the file holds the token)"
    xclip = "xclip -selection clipboard"
    cases = (
        ({"WAYLAND_DISPLAY": "wayland-0"}, "wl-copy", "wl-copy", "wl-copy --clear"),
        ({}, "xclip", xclip, f"{xclip} < /dev/null"),
        ({"WAYLAND_DISPLAY": "wayland-0"}, None, "wl-copy" + hint, "wl-copy --clear" + hint),
        ({"WAYLAND_DISPLAY": ""}, None, f"{xclip}{hint}", f"{xclip} < /dev/null{hint}"),
    )  # fmt: skip
    for at, (env, installed, copy, empty) in enumerate(cases):
        folder = Trial(trial.tmp / str(at), trial.shell)
        if installed:
            shutil.copy(folder.bin / "docker", folder.bin / installed)  # it would log a call
        ran = folder.run(env=env, uname="Linux")
        mac = _closing()
        assert mac.count("| pbcopy\n") == 1 and mac.count(": pbcopy < /dev/null\n") == 1
        linux = mac.replace("| pbcopy\n", f"| {copy}\n").replace(": pbcopy < /dev/null\n", f": {empty}\n")
        assert (ran.code, ran.out) == (0, _said() + linux)
        assert {call["tool"] for call in ran.calls} == {"docker", "curl", "sleep", "uname"}
