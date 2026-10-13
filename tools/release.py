"""``release``: the maintainer's release tool (docs/mvp/RELEASE.md), dsv's sibling, outside the shipped
product.

    uv run python tools/release.py keygen --out DIR [--passphrase file:/abs]
    uv run python tools/release.py build --key file:/abs/release-<fp16>.key.pem --notes FILE --out DIR
        --register FILE [--platform linux/amd64|linux/arm64] [--passphrase file:/abs]
    uv run python tools/release.py rescan BUNDLE...
    uv run python tools/release.py sign --key file:/abs/… [--passphrase file:/abs] FILE

``build`` builds the image from a git archive of HEAD, catalogues it with syft, scans it with grype and
pip-audit, gates the result on tools/triage.toml, signs SHA256SUMS with the release key and tags the commit.
It never runs docker pull, push or login, and no subprocess gets a secret in argv or its environment. The
release key stays on the maintainer's machine, outside every repository and synced folder; its passphrase
comes by prompt or ``file:``, as dsv's. This module holds the image's base pins, which deploy/Dockerfile
names, the approved scanners and the procedure a site verifies a release with: change them only on purpose.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import version as installed
from pathlib import Path
from typing import Annotated, Any, NoReturn
from urllib.parse import unquote

import typer
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from deltasleuth import licence
from deltasleuth.security import signing
from deltasleuth.security.signing import PackSigner
from tools import dsv
from tools.dsv import fail

ROOT = Path(__file__).resolve().parents[1]  # the checkout a release builds from
# The python:3.13-slim index that deploy/Dockerfile's PYTHON default pins, and its platform manifests, looked
# up once and never pulled by this tool: a release builds from its platform's manifest digest, so the local
# index reference, which source builds use, never changes (RELEASE.md §5).
INDEX = "sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b"
BASES = {
    "linux/amd64": "sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332",
    "linux/arm64": "sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f",
}
MACHINES = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}  # platform_machine, for uv.lock's markers
# The canonical verification procedure (RELEASE.md §3): VERIFY.txt repeats it from line 3, and each release
# key is filed with its sha256. A changed procedure is a new format, filed at every site like a new key.
PROCEDURE = Path(__file__).with_name("release-procedure.txt").read_text(encoding="utf-8")
FORMAT = "deltasleuth-release/1"
SYFT, GRYPE = "1.52.0", "0.119.0"  # the approved scanners (§7): build and rescan refuse any other
# What the source archive, and so the build context, holds (§2): never tests, tools or site files.
SHIPPED = ("README.md", "SECURITY.md", "pyproject.toml", "uv.lock", "deltasleuth", "web", "deploy",
           "docs/mvp", "docs/guide", "mocks", "examples")  # fmt: skip
TOOLS = ("docker", "git", "grype", "npm", "syft", "uv")
POSTGRES = "postgres:16-alpine"  # the smoke test's database, from the local store
GRYPE_DB = Path.home() / "Library" / "Caches" / "grype" / "db"  # the maintainer's: grype db update fills it
SYNCED = ("Desktop", "Documents", "Library/Mobile Documents", "Library/CloudStorage")  # under ~
# docker-compose.yml's x-release (§6), under which the smoke test runs the image
HARDENED = ("--read-only", "--tmpfs", "/tmp:size=64m,mode=1777,noexec,nosuid,nodev", "--cap-drop", "ALL",  # noqa: S108
            "--security-opt", "no-new-privileges:true")  # fmt: skip
SMOKE_SECONDS = 180
# The files reader's self-test (docs/mvp/FILES.md §3, READER.md §11): its guard, both readers, a sort that
# spills, a process replaced when it ends, and its limits.
DUCK = "from deltasleuth.connectors.tables import duck; duck.selftest()"
# The MCP server's self-test (docs/mvp/MCP.md §3): built without a store, it lists its tools to the library's
# own client in both protocol eras.
MCP = "from deltasleuth.mcp import server; server.selftest()"
ONE_DAY = timedelta(days=1)  # triage dates are the maintainer's local days; today is UTC's
HEALTH = (
    "import sys, urllib.request as u; sys.exit(u.build_opener(u.ProxyHandler({}))"
    ".open('http://127.0.0.1:8000/v1/health', timeout=4).status != 200)"
)
# Run in the built image: its python version, then each file a wheel vendors (``*.libs/*``, §7).
LIBS = """import importlib.metadata as m, platform
print(platform.python_version())
for d in m.distributions():
    for f in d.files or ():
        if f.parts[0].endswith(".libs"):
            print(d.metadata["Name"], d.version + "\\t" + f.as_posix())
"""
CHECKS = (  # after the gate (§9 step 8), in the checkout, with DS_REQUIRE_DB=1 DS_REQUIRE_TOOLS=1
    ("uv", "run", "--frozen", "pytest", "-q", "-p", "no:cacheprovider"),
    ("uv", "run", "--frozen", "ruff", "check", "deltasleuth", "mocks", "tests", "examples", "tools"),
    ("npm", "--prefix", "web", "ci"),
    ("npm", "--prefix", "web", "test"),
)
RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(rc\d+)?")  # X.Y.Z or X.Y.ZrcN
PINNED = re.compile(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}")  # name:tag@sha256:<64 hex>
KEY_LINE = re.compile(
    r"([0-9a-f]{64}) (signing|standby|(?:retired|withdrawn) \d{4}-\d\d-\d\d) procedure=([0-9a-f]{64})"
)
ADVISORY = re.compile(r"# DeltaSleuth security advisory DS-SA-\d{4}-\d{3}")
TRANSITION = "# DeltaSleuth release key transition"
# A line that sha256sum -c or shasum -c would check (§9, sign): blanks and a backslash, then a digest and a
# blank (shasum takes SHA-1's 40 hex digits and up, whatever -a says), or the tagged SHA256 (name) = digest.
CHECKSUM = re.compile(r"^[ \t]*\\?(?:[0-9A-Fa-f]{40,}[ \t]|SHA\S*[ \t]*\(.*\)[ \t]*=)", re.M)
CHECKLIST = "## Before the first release (the vendor's checklist)"  # SECURITY.md's maintainer section (§11)
# What the notes of a release without vendor keys say, on a line of its own, and those of a release with
# vendor keys never do (docs/mvp/LICENCE.md §19).
OPEN_BUILD = "This release is an open build: it embeds no licence vendor key and enforces no licence."
BLOCKING = ("Critical", "High", "Unknown")  # with a fix, or known exploited (§8)
JUSTIFICATIONS = ("component_not_present", "vulnerable_code_not_present",
                  "vulnerable_code_not_in_execute_path", "vulnerable_code_cannot_be_controlled_by_adversary",
                  "inline_mitigations_already_exist")  # OpenVEX's five  # fmt: skip
TRIAGE_FIELDS = {
    "finding": ("id", "component", "status", "justification", "statement", "decided", "expires", "by"),
    "vendored": ("distribution", "version", "files", "status", "justification", "statement", "decided",
                 "expires", "by"),
}  # fmt: skip
app = typer.Typer(no_args_is_help=True, help="The maintainer's release tool.", pretty_exceptions_enable=False)
KeyOpt = Annotated[str, typer.Option(help="file:PATH of the release key (release keygen).")]


def _run(argv: Iterable[str], *, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    argv = list(argv)
    done = subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)  # noqa: S603
    if check and done.returncode:
        last = (done.stderr.strip().splitlines() or [""])[-1]
        fail(f"{Path(argv[0]).name} {argv[1]} failed (exit {done.returncode}): {last}")
    return done


def _git(*args: str) -> str:
    return _run(["git", *args], cwd=ROOT).stdout.strip()


def _refuse(problems: Iterable[str], detail: Iterable[str] = ()) -> NoReturn:
    """One ``error:`` line per problem, then ``detail``, and exit 2."""
    for line in [f"error: {problem}" for problem in problems] + list(detail):
        typer.echo(line, err=True)
    raise typer.Exit(2)


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _short(ref: str) -> str:
    """A reference with its digest cut to 12 hex digits."""
    at = ref.find("sha256:")
    return ref if at < 0 else f"{ref[: at + 19]}…"


def _gzip(source: Path, target: Path) -> None:
    """``source`` gzipped as a release ships it: no name, mtime 0, level 9 (§2)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as packed:
            shutil.copyfileobj(src, packed, 1 << 20)


def _transport(folder: Path, target: Path, mtime: int) -> None:
    """The bundle's transport tar: ustar, sorted, folders and regular files only, 0755 and 0644, owner 0 with
    no names, the commit's mtime (§2)."""
    with tarfile.open(target, "x", format=tarfile.USTAR_FORMAT) as tar:
        for path in [folder, *sorted(folder.rglob("*"))]:
            info = tar.gettarinfo(path, path.relative_to(folder.parent).as_posix())
            info.mode, info.mtime, info.uid, info.gid = (0o755 if path.is_dir() else 0o644), mtime, 0, 0
            info.uname = info.gname = ""
            with path.open("rb") if path.is_file() else nullcontext() as handle:
                tar.addfile(info, handle)


def _outside(path: Path, what: str) -> None:
    """Refuse a path, symlinks resolved, in a git working tree or a cloud-synced folder (§4), by name or by
    file identity: APFS ignores case, so ~/documents is ~/Documents."""
    path = path.resolve()
    if any((folder / ".git").exists() for folder in (path, *path.parents)):
        fail(f"{what} in a git working tree: keep release keys outside every repository")
    held = [folder for folder in (path, *path.parents) if folder.exists()]
    for synced in SYNCED:
        folder = Path.home() / synced
        if any(path.is_relative_to(p) for p in (folder, folder.resolve())) or (
            folder.is_dir() and any(os.path.samefile(p, folder) for p in held)
        ):
            fail(f"{what} in a cloud-synced folder (~/{synced}): keep release keys on this machine only")


def release_keys() -> dict[str, tuple[str, str]]:
    """tools/release-keys.txt: each key's status and the sha256 of the procedure filed with it (§4)."""
    keys = {}
    for number, line in enumerate((ROOT / "tools" / "release-keys.txt").read_text().splitlines(), 1):
        if line and not line.startswith("#"):
            if not (match := KEY_LINE.fullmatch(line)):
                fail(
                    f'tools/release-keys.txt line {number} is not "<fingerprint> <status> procedure=<sha256>"'
                )
            keys[match[1]] = (match[2], match[3])
    return keys


def _release_key(key: str, passphrase: str | None, *, building: bool) -> PackSigner:
    """The release key, unlocked, refused where it may not sign (§4, §9 step 1)."""
    signer = dsv.signer(key, passphrase)
    if building:
        _outside(Path(key.removeprefix("file:")), "--key: the key file is")
        if passphrase is not None:
            _outside(Path(passphrase.removeprefix("file:")), "--passphrase: the passphrase file is")
    fp = signer.fingerprint
    if fp in {signing.fingerprint(pem) for pem in licence.SHIPPED_KEYS} | set(licence.REMOVED_KEYS):
        fail(f"--key: key {fp[:12]}… is a licence vendor key: releases are signed with a release key"
             " (release keygen)")  # fmt: skip
    status, filed = release_keys().get(fp, ("", ""))
    if building:
        if status != "signing":
            fail(f"--key: key {fp[:12]}… is not the signing key tools/release-keys.txt lists")
        if filed != hashlib.sha256(PROCEDURE.encode()).hexdigest():
            fail(f"release.PROCEDURE differs from the procedure filed with key {fp[:12]}…: a changed"
                 " procedure is filed like a new key (RELEASE.md §4)")  # fmt: skip
    elif status.startswith(("retired", "withdrawn")):
        fail(f"--key: key {fp[:12]}… is {status} in tools/release-keys.txt: it signs nothing more")
    return signer


def triage(text: str, today: date | None = None) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """tools/triage.toml's decisions and a line for each malformed entry, and with ``today`` each expired or
    decided after it."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return {}, [f"tools/triage.toml: {exc}"]
    problems = [f"tools/triage.toml: unknown table {name}" for name in data if name not in TRIAGE_FIELDS]
    entries = {kind: data.get(kind, []) for kind in TRIAGE_FIELDS}
    for kind, fields in TRIAGE_FIELDS.items():
        if type(entries[kind]) is not list or any(type(entry) is not dict for entry in entries[kind]):
            problems.append(f"tools/triage.toml: {kind} must be [[{kind}]] entries")
            entries[kind] = []
        for entry in entries[kind]:
            name = (f"{entry.get('id')} for {entry.get('component')}" if kind == "finding"
                    else f"[[vendored]] {entry.get('distribution')} {entry.get('version')}")  # fmt: skip
            problems += [
                f"tools/triage.toml: {name} {problem}" for problem in _malformed(entry, fields, today)
            ]
    return entries, problems


def _malformed(entry: dict[str, Any], fields: tuple[str, ...], today: date | None) -> list[str]:
    problems = [f"has an unknown key {key}" for key in entry if key not in fields]
    problems += [f"lacks {key}" for key in fields if key != "justification" and key not in entry]
    problems += [f"needs {key} as a string" for key in fields if key in entry and type(entry[key]) is not str
                 and key not in ("status", "justification", "files", "decided", "expires")]  # fmt: skip
    files = entry.get("files", [])
    if type(files) is not list or any(type(file) is not str for file in files):
        problems.append("needs files as a list of strings")
    status, justification = entry.get("status"), entry.get("justification")
    if status not in ("not_affected", "affected"):
        problems.append(f"has status {status!r}: use not_affected or affected")
    elif (status == "not_affected") != (justification is not None):
        problems.append("needs a justification if not_affected, and only then")
    elif justification is not None and justification not in JUSTIFICATIONS:
        problems.append(f"has justification {justification!r}: use one of OpenVEX's five")
    decided, expires = entry.get("decided"), entry.get("expires")
    if type(decided) is not date or type(expires) is not date:
        problems.append("needs decided and expires as dates (YYYY-MM-DD)")
    elif not 0 <= (expires - decided).days <= (30 if status == "affected" else 90):
        problems.append(f"expires {(expires - decided).days} days after decided: at most 90 (affected: 30)")
    elif today is not None and expires < today:
        problems.append(f"expired on {expires}: decide again")
    elif today is not None and decided > today + ONE_DAY:  # a day ahead of UTC: the maintainer's local date
        problems.append(f"has decided {decided}, in the future: date a decision the day it is made")
    if "@" in str(entry.get("by", "")):
        problems.append("names an e-mail address in by: name a role")
    return problems


@dataclass(frozen=True)
class Finding:
    """A grype match or a pip-audit vulnerability, by id and purl without qualifiers."""

    id: str
    severity: str  # grype's; pip-audit gives none
    fix: str  # the fixed versions
    state: str  # grype's fix state; pip-audit's is fixed when it names a fix
    purl: str
    ids: frozenset[str]  # the id, related ids and aliases
    kev: bool
    source: str  # grype or pip-audit

    @property
    def blocks(self) -> bool:
        if self.source == "pip-audit":
            return bool(self.fix)
        return self.state == "fixed" and (self.severity in BLOCKING or self.kev)

    def line(self) -> str:
        rated = f"{self.severity}, known exploited" if self.kev else self.severity
        rated = rated if self.source == "grype" else "(pip-audit)"
        return f"  {self.id} {rated}, fixed in {self.fix}: {self.purl}"


def findings(matches: list[dict[str, Any]], audit: Mapping[str, Any]) -> list[Finding]:
    """grype's matches and pip-audit's vulnerabilities, once per source, id and purl: neither hides the
    other's."""
    found: dict[tuple[str, str, str], Finding] = {}
    for match in matches:
        vuln, fix = match["vulnerability"], match["vulnerability"].get("fix") or {}
        purl = match["artifact"].get("purl", "").split("?")[0]
        ids = frozenset({vuln["id"], *(related["id"] for related in match.get("relatedVulnerabilities", []))})
        versions, severity = ", ".join(fix.get("versions", [])), vuln.get("severity") or "Unknown"
        kev = bool(vuln.get("knownExploited"))
        finding = Finding(vuln["id"], severity, versions, fix.get("state", ""), purl, ids, kev, "grype")
        found.setdefault(("grype", vuln["id"], purl), finding)
    for dependency in audit.get("dependencies", []):
        purl = f"pkg:pypi/{canonicalize_name(dependency['name'])}@{dependency.get('version')}"
        for vuln in dependency.get("vulns", []):
            fix = ", ".join(vuln.get("fix_versions", []))
            ids = frozenset({vuln["id"], *vuln.get("aliases", [])})
            state = "fixed" if fix else "not-fixed"
            finding = Finding(vuln["id"], "", fix, state, purl, ids, False, "pip-audit")
            found.setdefault(("pip-audit", vuln["id"], purl), finding)
    return list(found.values())


@dataclass(frozen=True)
class Gate:
    blocking: list[Finding]
    triaged: list[tuple[Finding, dict[str, Any]]]
    reported: list[Finding]
    decided: list[tuple[tuple[str, str, str], dict[str, Any]]]  # each vendored file and its decision
    refused: list[str]  # a vendored file without a decision; one for another version, the image's undecided
    unused: list[str]  # entries that match nothing

    def line(self) -> str:
        return (f"{'failed' if self.blocking or self.refused else 'passed'}; {len(self.blocking)} blocking,"
                f" {len(self.triaged)} triaged, {len(self.reported)} reported, {len(self.decided)} vendored"
                " file(s) decided")  # fmt: skip


def gate(
    matches: list[dict[str, Any]],
    audit: Mapping[str, Any],
    entries: Mapping[str, list[dict[str, Any]]],
    today: date,
    vendored: Iterable[tuple[str, str, str]] = (),
) -> Gate:
    """The release gate (§8), pure: grype's matches and pip-audit's report against tools/triage.toml's
    decisions in force (decided, not expired), and each vendored file, ``(distribution, version, path)``,
    against its decision."""
    live = {kind: [e for e in entries.get(kind, []) if e["decided"] - ONE_DAY <= today <= e["expires"]]
            for kind in TRIAGE_FIELDS}  # fmt: skip
    blocking, triaged, reported, used = [], [], [], set()
    for finding in findings(matches, audit):
        named = (e for e in live["finding"] if e["component"] == finding.purl and e["id"] in finding.ids)
        entry = next(named, None)
        if entry is not None:
            triaged.append((finding, entry))
            used.add((entry["id"], entry["component"]))
        else:
            (blocking if finding.blocks else reported).append(finding)
    decided, refused, vendored = [], [], list(vendored)
    image = {distribution: version for distribution, version, _ in vendored}
    for distribution, version, path in vendored:
        decision = next((e for e in live["vendored"] if e["distribution"] == distribution
                         and e["version"] == version and path in e["files"]), None)  # fmt: skip
        if decision is None:
            refused.append(f"{distribution} {version} ships {path} with no [[vendored]] decision in"
                           " tools/triage.toml")  # fmt: skip
        else:
            decided.append(((distribution, version, path), decision))
    decisions = {(e["distribution"], e["version"]) for e in live["vendored"]}
    for e in live["vendored"]:  # another line's decision stands while this version has one (rescan)
        shipped = image.get(e["distribution"], e["version"])
        if shipped != e["version"] and (e["distribution"], shipped) not in decisions:
            refused.append(f"tools/triage.toml: [[vendored]] {e['distribution']} {e['version']} is not the"
                           f" image's {shipped}: decide again")  # fmt: skip
    unused = [f"{e['id']} for {e['component']} matches no finding"
              for e in live["finding"] if (e["id"], e["component"]) not in used]  # fmt: skip
    unused += [f"[[vendored]] {e['distribution']} {e['version']} matches no vendored file"
               for e in live["vendored"] if vendored and e["distribution"] not in image]  # fmt: skip
    return Gate(blocking, triaged, reported, decided, refused,
                [f"tools/triage.toml: {line}: remove it" for line in unused])  # fmt: skip


def _purl(purl: str) -> tuple[str, str, str]:
    """A purl's type, name (with any namespace) and version, decoded, without qualifiers."""
    kind, _, rest = purl.removeprefix("pkg:").split("?")[0].split("#")[0].partition("/")
    name, _, version = rest.rpartition("@") if "@" in rest else (rest, "", "")
    return kind, unquote(name), unquote(version)


def _components(components: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for component in components:
        yield component
        yield from _components(component.get("components", []))


def lacks(
    sbom: Mapping[str, Any], *, dpkg: Iterable[str], python: str, exported: str, version: str,
    lock: Mapping[str, Any], machine: str,
) -> list[str]:  # fmt: skip
    """What the CycloneDX SBOM lacks of what the image holds (§7), one refusal each: the Debian distribution,
    each package dpkg lists (and no other), the python binary, uv.lock's packages for the target, deltasleuth
    and web/package-lock.json's non-dev packages."""
    components = list(_components(sbom.get("components", [])))
    purls = {_purl(c["purl"]) for c in components if c.get("purl")}
    pypi = {(canonicalize_name(name), found) for kind, name, found in purls if kind == "pypi"}
    problems = []
    if not any(c.get("type") == "operating-system" and c.get("name") == "debian" for c in components):
        problems.append("the SBOM lacks the Debian distribution")
    debs = {
        name.removeprefix("debian/")
        for kind, name, _ in purls
        if kind == "deb" and name.startswith("debian/")
    }
    problems += [f"the SBOM lacks {name} (dpkg)" for name in sorted(set(dpkg) - debs)]
    problems += [
        f"the SBOM lists Debian package {name}, which dpkg does not" for name in sorted(debs - set(dpkg))
    ]
    if ("generic", "python", python) not in purls:
        problems.append(f"the SBOM lacks python {python}")
    target = {
        "os_name": "posix", "sys_platform": "linux", "platform_system": "Linux", "platform_machine": machine,
        "platform_release": "", "platform_version": "", "implementation_name": "cpython",
        "implementation_version": python, "platform_python_implementation": "CPython",
        "python_version": ".".join(python.split(".")[:2]), "python_full_version": python,
    }  # fmt: skip
    for requirement in (Requirement(line) for line in exported.splitlines() if line.strip()):
        pinned = next(iter(requirement.specifier)).version
        wanted = requirement.marker is None or requirement.marker.evaluate(target)
        if wanted and (canonicalize_name(requirement.name), pinned) not in pypi:
            problems.append(f"the SBOM lacks {requirement.name} {pinned} (uv.lock)")
    if ("deltasleuth", version) not in pypi:
        problems.append(f"the SBOM lacks deltasleuth {version} (pyproject.toml)")
    for path, entry in lock.get("packages", {}).items():
        name, found = path.rpartition("node_modules/")[2], entry.get("version")
        if path and found and not entry.get("dev") and ("npm", name, found) not in purls:
            problems.append(f"the SBOM lacks {name} {found} (web/package-lock.json)")
    return problems


def digests(archive: Path, image: str) -> tuple[str, str]:
    """The config and manifest digests of a ``docker save`` archive that holds ``image`` alone (§2)."""
    with tarfile.open(archive) as tar:
        index, listed = (json.load(tar.extractfile(name)) for name in ("index.json", "manifest.json"))  # type: ignore[arg-type]
        count = max(len(index.get("manifests", [])), len(listed))
        if count != 1 or listed[0].get("RepoTags") != [image]:
            fail(f"the saved archive holds {count} images; expected {image} alone")
        manifest = index["manifests"][0]["digest"]
        blob = tar.extractfile(f"blobs/sha256/{manifest.removeprefix('sha256:')}")
        return json.load(blob)["config"]["digest"], manifest  # type: ignore[arg-type]


def _staging(folder: Path) -> Path:
    (folder / "home").mkdir(parents=True)
    (folder / "empty.yaml").write_text("")
    return folder


def _scanning(staging: Path, *, network: bool = False) -> dict[str, Any]:
    """Where and how a scanner runs (§7): in staging, from an environment built from scratch, with the proxy
    only for the two network steps."""
    folders = dict.fromkeys(
        str(Path(shutil.which(tool) or tool).resolve().parent) for tool in ("syft", "grype")
    )
    env = {
        "PATH": os.pathsep.join([*folders, "/usr/bin", "/bin"]), "HOME": str(staging / "home"), "TZ": "UTC",
        "GRYPE_DB_CACHE_DIR": str(GRYPE_DB), "GRYPE_DB_AUTO_UPDATE": "false",
        "GRYPE_CHECK_FOR_APP_UPDATE": "false", "SYFT_CHECK_FOR_APP_UPDATE": "false",
    }  # fmt: skip
    env |= {name: os.environ[name] for name in ("HTTPS_PROXY", "NO_PROXY") if network and name in os.environ}
    return {"cwd": staging, "env": env}


def _scan(staging: Path, argv: list[str], *, network: bool = False) -> str:
    """syft's or grype's output, with ``-c <staging>/empty.yaml``, which replaces every config file (§7)."""
    return _run([*argv, "-c", str(staging / "empty.yaml")], **_scanning(staging, network=network)).stdout


def _sbom(staging: Path, version: str, vendor: str, stem: str) -> None:
    """syft's CycloneDX 1.6 and SPDX 2.3 SBOMs of staging's image.tar, in staging's bundle/sbom (§7)."""
    (staging / "bundle" / "sbom").mkdir(parents=True, exist_ok=True)
    _scan(staging, ["syft", "scan", "docker-archive:image.tar", "-q", "--select-catalogers",
                    "+javascript-lock-cataloger", "--source-name", "deltasleuth", "--source-version", version,
                    "--source-supplier", vendor, "-o", f"cyclonedx-json@1.6=bundle/sbom/{stem}.cdx.json",
                    "-o", f"spdx-json@2.3=bundle/sbom/{stem}.spdx.json"])  # fmt: skip


def _grype(staging: Path, sbom: str, *extra: str) -> dict[str, Any]:
    """grype's report on an SBOM (§8)."""
    return json.loads(_scan(staging, ["grype", f"sbom:{sbom}", "-o", "json", *extra]))


def _approved(staging: Path) -> dict[str, str]:
    reported = {}
    for tool, approved in (("grype", GRYPE), ("syft", SYFT)):
        reported[tool] = json.loads(_scan(staging, [tool, "version", "-o", "json"]))["version"]
        if reported[tool] != approved:
            fail(f"{tool} {reported[tool]} is not the approved {approved}: install it, or change"
                 f" release.{tool.upper()} on purpose")  # fmt: skip
    return reported


def _pip_audit(staging: Path, sbom: Mapping[str, Any], output: str) -> dict[str, Any]:
    """pip-audit of each pkg:pypi component but deltasleuth (§8); a dependency it skipped is a refusal."""
    purls = [_purl(c["purl"]) for c in _components(sbom.get("components", [])) if c.get("purl")]
    pinned = dict(
        sorted((name, found) for kind, name, found in purls if kind == "pypi" and name != "deltasleuth")
    )
    (staging / "pip-audit.txt").write_text("".join(f"{name}=={found}\n" for name, found in pinned.items()))
    argv = [sys.executable, "-m", "pip_audit", "-r", "pip-audit.txt", "--no-deps", "--disable-pip",
            "--progress-spinner", "off", "-f", "json", "-o", output]  # fmt: skip
    _run(argv, check=False, **_scanning(staging, network=True))  # exit 1: it found a vulnerability
    if not (staging / output).is_file():
        fail("pip-audit wrote no report")
    audit = json.loads((staging / output).read_text())
    skipped = [f"{d['name']} {pinned.get(canonicalize_name(d['name']))} ({d['skip_reason']})"
               for d in audit.get("dependencies", []) if "skip_reason" in d]  # fmt: skip
    if skipped:
        many = "dependency" if len(skipped) == 1 else "dependencies"
        fail(f"pip-audit could not audit {len(skipped)} {many}: {', '.join(skipped)}")
    return audit


def _pins(dockerfile: str) -> tuple[str, list[str]]:
    """The PYTHON default (the python index) and the other bases; refuses a base not pinned by digest,
    directly or through its ARG default: each FROM, and each COPY --from or RUN --mount from= that names no
    earlier stage. Keywords and stage names are case-insensitive, as Docker's."""
    args: dict[str, tuple[int, str]] = {}
    stages, others, index = set(), [], 0
    for number, line in enumerate(dockerfile.splitlines(), 1):
        keyword, *words = line.split() or [""]
        keyword = keyword.upper()
        if keyword == "ARG":
            args |= {name: (number, default) for name, _, default in (w.partition("=") for w in words)}
            continue
        if keyword == "FROM":
            words = [word for word in words if not word.startswith("--")]
            images = words[:1]
        elif keyword == "COPY":
            images = [word.removeprefix("--from=") for word in words if word.startswith("--from=")]
        elif keyword == "RUN":  # --mount=type=bind,from=<stage or image>,…
            options = [part for word in words if word.startswith("--mount=") for part in word[8:].split(",")]
            images = [option.removeprefix("from=") for option in options if option.startswith("from=")]
        else:
            images = []
        for image in images:
            name = image.strip("${}")
            if image.startswith("$") and name in args:
                (at, ref), what = args[name], f"ARG {name}="
            else:
                what = {"FROM": "FROM ", "COPY": "COPY --from="}.get(keyword, "RUN --mount from=")
                at, ref = number, image
            if ref.lower() in stages:
                continue
            if not PINNED.fullmatch(ref):
                fail(f"deploy/Dockerfile line {at}: {what}{ref} is not pinned by digest"
                     " (name:tag@sha256:<64 hex>)")  # fmt: skip
            if name != "PYTHON" and ref not in others:  # a release builds from BASES, not the python index
                others.append(ref)
        if keyword == "FROM":  # later instructions name the stage, or its index
            stages.add(str(index))
            index += 1
            if len(words) == 3 and words[1].upper() == "AS":
                stages.add(words[2].lower())
    if "PYTHON" not in args:
        fail("deploy/Dockerfile has no ARG PYTHON: a release passes its platform's manifest (RELEASE.md §5)")
    return args["PYTHON"][1], others


def _porcelain() -> list[str]:
    return _git("status", "--porcelain", "--untracked-files=all").splitlines()


def _static(platform: str, out: Path, register: Path, staging: Path) -> dict[str, Any]:
    """§9 step 2: every check that needs no image, the tools first."""
    if missing := [tool for tool in TOOLS if shutil.which(tool) is None]:
        fail(f"missing: {', '.join(missing)} (needs docker, git, grype, npm, syft and uv)")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    if not (parts := RELEASE.fullmatch(version)):
        fail(f"version {version} is not a release: set X.Y.Z or X.Y.ZrcN in pyproject.toml")
    if changed := _porcelain():
        fail(f"the working tree is not clean: commit or remove {len(changed)} path(s) first")
    revision = _git("rev-parse", "HEAD")
    if tagged := bool(_git("tag", "--list", f"v{version}")):  # another platform's bundle of this revision
        if _git("rev-parse", f"v{version}^{{commit}}") != revision:
            fail(f"tag v{version} is on another revision: a released version is never rebuilt")
    for name, tag in (("docker-compose.yml", version), ("docker-compose.ca.yml", f"{version}-ca")):
        services = yaml.safe_load((ROOT / "deploy" / name).read_text())["services"]
        for image in (services[service].get("image") for service in ("app", "mocks")):
            if image != f"deltasleuth:{tag}":
                fail(f"deploy/{name} names {image}, not deltasleuth:{tag}")
    python_index, build_bases = _pins((ROOT / "deploy" / "Dockerfile").read_text())
    authors = project.get("authors") or [{}]
    if not (vendor := authors[0].get("name")):
        fail('pyproject.toml names no vendor: set [project] authors = [{name = "<legal name>"}]')
    if any("email" in author for author in authors):
        fail("pyproject.toml [project].authors has an email: it would ship in the image and the SBOM;"
             " name the vendor only")  # fmt: skip
    if not (ROOT / "SECURITY.md").is_file():
        fail("SECURITY.md is missing: write it first (RELEASE.md §11)")
    if "<SECURITY-CONTACT>" in (security := (ROOT / "SECURITY.md").read_text()):
        fail("SECURITY.md still holds <SECURITY-CONTACT>: name the security contact first")
    if CHECKLIST in security:
        fail(f'SECURITY.md still holds "{CHECKLIST[3:]}": delete that section first')
    reported, today = _approved(staging), _now().date()
    entries, problems = triage((ROOT / "tools" / "triage.toml").read_text(), today)
    if problems:
        _refuse(problems)
    major, minor, patch, _ = parts.groups()
    earlier = [tag for tag in _git("tag", "--list", f"v{major}.{minor}.*").split()
               if (m := RELEASE.fullmatch(tag[1:])) and not m[4] and int(m[3]) < int(patch)]  # fmt: skip
    if earlier:
        base = max(earlier, key=lambda tag: int(tag.rpartition(".")[2]))
        listing = ("ls-tree", "-r", "--name-only")
        head, before = (
            set(_git(*listing, rev, "--", "deltasleuth/store/migrations").split()) for rev in ("HEAD", base)
        )
        if added := sorted(head - before):
            fail(f"{version} adds {added[0]}: a patch release adds no migration")
    lines = register.read_text().splitlines() if register.exists() else []
    released = [json.loads(line) for line in lines if line]
    if any((e["version"], e["platform"]) == (version, platform) for e in released):
        fail(f"{version} for {platform} is in the register already")
    if tagged and not any((e["version"], e["revision"]) == (version, revision) for e in released):
        fail(f"tag v{version} is on HEAD but {register} lists no bundle of it: pass the register that does")
    stem = f"deltasleuth-{version}-{platform.replace('/', '-')}"
    for path in (out / stem, out / f"{stem}.tar"):
        if path.exists():
            fail(f"--out: {path} exists: a release is never overwritten")
    epoch = int(_git("show", "-s", "--format=%ct", revision))
    return {
        "version": version, "stem": stem, "vendor": vendor, "revision": revision, "epoch": epoch,
        "created": f"{datetime.fromtimestamp(epoch, UTC):%Y-%m-%dT%H:%M:%SZ}", "python_index": python_index,
        "build_bases": build_bases, "reported": reported, "entries": entries, "today": today,
        "tagged": tagged,
    }  # fmt: skip


def _inspect(ref: str, template: str) -> str | None:
    done = _run(["docker", "image", "inspect", "--format", template, ref], check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def _local(platform: str, python_index: str, build_bases: list[str]) -> dict[str, str | None]:
    """§9 step 3: the bases are local, for their platforms; their ids, and the index reference's."""
    daemon = _run(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]).stdout.strip()
    wanted = [(f"python@{BASES[platform]}", f"{python_index.partition('@')[0]} for {platform}", platform)]
    wanted += [(base, f"the build base for {daemon}", daemon) for base in build_bases]
    wanted.append((POSTGRES, f"the smoke test's database for {daemon}", daemon))
    ids: dict[str, str | None] = {}
    for ref, label, want in wanted:
        found, _, ids[ref] = (_inspect(ref, "{{.Os}}/{{.Architecture}} {{.Id}}") or "").partition(" ")
        if not found:
            fail(f"{_short(ref)} ({label}) is not in the local image store; this tool never pulls. To add"
                 f" it: docker pull {ref}")  # fmt: skip
        if found != want:
            fail(f"{_short(ref)} ({label}) is {found} in the local image store, not {want}")
    ids[python_index] = _inspect(python_index, "{{.Id}}")
    return ids


def _smoke(image: str, platform: str, staging: Path) -> None:
    """§9 step 5: ``ds init`` and ``ds serve`` under the compose hardening, on an internal network with a
    trusting PostgreSQL and a developer-mode config, no secret; then a cron's next time in Europe/London,
    which the image's own time zone data must give (docs/mvp/SCHEDULES.md §4), and the files reader's
    self-test, which spills in the evidence volume (docs/mvp/FILES.md §3), without its limits where the
    image's machine is not this host's (macOS names aarch64 ``arm64``): it then runs under emulation, where
    a data limit would count the emulator and the socket filter judge it (docs/mvp/READER.md §11); then the
    MCP server's self-test, which reads no store (docs/mvp/MCP.md §3); everything removed afterwards."""
    name = f"ds-release-{secrets.token_hex(4)}"
    config = staging / "smoke"
    config.mkdir(mode=0o755)
    for file, text in {
        "deltasleuth.yaml": "database_dsn_ref: file:/etc/deltasleuth/db_dsn\ntargets_file: targets.yaml\n"
        "evidence_dir: /var/lib/deltasleuth/evidence\nworkspace_dir: /var/lib/deltasleuth/workspace\n",
        "targets.yaml": "targets:\n  smoke: {base_url: 'http://127.0.0.1:9', allow_private_network: true}\n",
        "db_dsn": f"postgresql://postgres@{name}-db:5432/postgres\n",
    }.items():
        (config / file).write_text(text)
        (config / file).chmod(0o644)
    volumes = {f"{name}-{kind}": f"/var/lib/deltasleuth/{kind}" for kind in ("evidence", "workspace", "p0")}
    mounts = ["-v", f"{config}:/etc/deltasleuth:ro"]
    mounts += [option for volume, path in volumes.items() for option in ("-v", f"{volume}:{path}")]
    run = ["docker", "run", "--pull", "never", "--platform", platform, "--network", name, *HARDENED, *mounts]
    deadline = time.monotonic() + SMOKE_SECONDS
    try:
        _run(["docker", "network", "create", "--internal", name])
        _run(["docker", "run", "-d", "--pull", "never", "--name", f"{name}-db", "--network", name,
              "-e", "POSTGRES_HOST_AUTH_METHOD=trust", POSTGRES])  # fmt: skip
        served = (
            _poll(["docker", "exec", f"{name}-db", "pg_isready", "-q", "-h", "127.0.0.1", "-U", "postgres"],
                  deadline)
            and not _run([*run, "--rm", image, "ds", "init"], check=False).returncode
            and not _run([*run, "-d", "--name", f"{name}-app", image, "ds", "serve", "--host", "127.0.0.1"],
                         check=False).returncode
            and _poll(["docker", "exec", f"{name}-app", "python", "-c", HEALTH], deadline)
        )  # fmt: skip
        if not served:
            fail(f"the smoke test failed: /v1/health did not answer 200 within {SMOKE_SECONDS} s under the"
                 " compose hardening")  # fmt: skip
        zoned = ["ds", "schedules", "next", "0 2 * * *", "--tz", "Europe/London", "--count", "1"]
        if _run([*run, "--rm", image, *zoned], check=False).returncode:
            fail("the smoke test failed: the image's time zone data does not load Europe/London")
        host = os.uname().machine
        native = MACHINES[platform] == {"arm64": "aarch64"}.get(host, host)
        duck = DUCK if native else DUCK.replace("()", "(limits=False)")
        if _run([*run, "--rm", image, "python", "-c", duck], check=False).returncode:
            fail("the smoke test failed: DuckDB does not load, its guard does not hold, it cannot read and"
                 " spill, its process is not replaced when it ends, or its limits do not hold, under the"
                 " compose hardening")  # fmt: skip
        if _run([*run, "--rm", image, "python", "-c", MCP], check=False).returncode:
            fail("the smoke test failed: the MCP server does not list its tools in both protocol eras under"
                 " the compose hardening")  # fmt: skip
    finally:
        _run(["docker", "rm", "-f", "-v", f"{name}-app", f"{name}-db"], check=False)  # -v: postgres's VOLUME
        _run(["docker", "volume", "rm", "-f", *volumes], check=False)
        _run(["docker", "network", "rm", name], check=False)


def _poll(argv: list[str], deadline: float) -> bool:
    while _run(argv, check=False).returncode:
        if time.monotonic() > deadline:
            return False
        time.sleep(1)
    return True


def _vex(result: Gate, facts: Mapping[str, Any], platform: str, manifest: str, signed: str) -> dict[str, Any]:
    """OpenVEX 0.2.0 (§8): one statement per matched [[finding]], for its component and, holding it, the image
    (the purl spec's digest version)."""
    oci = f"pkg:oci/deltasleuth@sha256%3A{manifest.removeprefix('sha256:')}?tag={facts['version']}"
    matched: dict[tuple[str, str], tuple[list[Finding], dict[str, Any]]] = {}
    for finding, entry in result.triaged:
        matched.setdefault((entry["id"], entry["component"]), ([], entry))[0].append(finding)
    statements = []
    for found, entry in matched.values():
        name = next((f.id for f in found if f.source == "grype"), found[0].id)
        component = {"@id": entry["component"]}
        aliases = sorted(frozenset().union(*(f.ids for f in found)) - {name})
        statements.append({
            "vulnerability": {"name": name, "aliases": aliases},
            "products": [component, {"@id": oci, "subcomponents": [component]}],
            "status": entry["status"],
            **({"justification": entry["justification"], "impact_statement": entry["statement"]}
               if entry["status"] == "not_affected" else {"action_statement": entry["statement"]}),
        })  # fmt: skip
    return {"@context": "https://openvex.dev/ns/v0.2.0",
            "@id": f"urn:deltasleuth:vex:{facts['version']}:{platform.replace('/', '-')}",
            "author": facts["vendor"], "timestamp": signed, "version": 1,
            "statements": statements}  # fmt: skip


def _summary(result: Gate, matches: list[dict[str, Any]], status: Mapping[str, Any], stem: str) -> str:
    """SUMMARY.txt (§2): the policy, the result, counts by severity and fix state, the decisions, the database
    and how to rescan."""
    counts: dict[str, dict[str, int]] = {}
    for vuln in (match["vulnerability"] for match in matches):
        row = counts.setdefault(vuln.get("severity") or "Unknown", {})
        state = (vuln.get("fix") or {}).get("state") or "unknown"
        row[state] = row.get(state, 0) + 1
    every = [*result.blocking, *result.reported, *(finding for finding, _ in result.triaged)]
    pip = [finding for finding in every if finding.source == "pip-audit"]
    decision = "{0[status]}{1} until {0[expires]}, decided {0[decided]} by {0[by]}: {0[statement]}"
    lines = [
        f"{stem}: scan summary",
        "Policy (docs/mvp/RELEASE.md §8): a grype match with a fix that is Critical, High, of unknown",
        "severity or known exploited, and a pip-audit finding with a fix, block the release unless",
        "tools/triage.toml decides it, with an expiry; everything else is reported. Each vendored library",
        "has a dated decision.",
        f"Result: {result.line()}",
        f"Database: grype {status.get('schemaVersion')} built {status.get('built')}",
        f"  from {status.get('from')}",
        "grype matches by severity and fix state:",
        *(f"  {severity}: " + ", ".join(f"{state} {n}" for state, n in sorted(row.items()))
          for severity, row in sorted(counts.items())),
        f"pip-audit: {len(pip)} finding(s), {sum(bool(f.fix) for f in pip)} with a fix",
        "Triaged:",
        *(f"  {f.id} {f.purl}: "  # an affected finding has no justification
          + decision.format(e, f" ({e.get('justification')})" * ("justification" in e))
          for f, e in result.triaged),
        "Vendored libraries:",
        *(f"  {d} {v} {p}: " + decision.format(e, "") for (d, v, p), e in result.decided),
        "Rescan, air-gapped (SECURITY.md, Scanning our releases): grype db import the database above, or",
        "a newer one, then run in this folder:",
        "  GRYPE_DB_AUTO_UPDATE=false GRYPE_CHECK_FOR_APP_UPDATE=false GRYPE_DB_VALIDATE_AGE=false"
        f" grype sbom:sbom/{stem}.cdx.json --vex scan/vex.openvex.json",
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


def _transition(signer: PackSigner) -> dict[str, bytes]:
    """tools/transition.txt and its .sig, when they hand this key over (§4): its last bundle ships them."""
    path, sig = ROOT / "tools" / "transition.txt", ROOT / "tools" / "transition.txt.sig"
    if not path.is_file() or f"\nold: {signer.fingerprint}\n" not in path.read_text():
        return {}
    if not sig.is_file() or not signing.verifies(signer.public_pem, sig.read_bytes(), path.read_bytes()):
        fail(f"tools/transition.txt.sig is not key {signer.fingerprint[:12]}…'s signature of"
             " tools/transition.txt")  # fmt: skip
    return {"transition.txt": path.read_bytes(), "transition.txt.sig": sig.read_bytes()}


@app.command()
def keygen(out: dsv.OutOpt, passphrase: dsv.PassOpt = None) -> None:
    """Write a new release key pair (P-256, PKCS#8, encrypted, mode 0600) and procedure.txt to DIR, never over
    a file, and print its fingerprint, tools/release-keys.txt line and SECURITY.md row. Refuses a DIR in a git
    working tree or a cloud-synced folder."""
    out = out.resolve()
    _outside(out, f"{out} is")
    if (out / "procedure.txt").exists():
        fail(f"{out / 'procedure.txt'} exists: keygen never writes over a file")
    signer = dsv.keypair(out, "release", passphrase)
    stem, filed = f"release-{signer.fingerprint[:16]}", hashlib.sha256(PROCEDURE.encode()).hexdigest()
    dsv.create(out / "procedure.txt", PROCEDURE.encode())
    typer.echo(f"fingerprint: {signer.fingerprint}")
    typer.echo(f"wrote {stem}.key.pem (encrypted, mode 0600), {stem}.pub.pem and procedure.txt to {out}")
    typer.echo(f"the line of tools/release-keys.txt:\n{signer.fingerprint} signing procedure={filed}")
    typer.echo(f"the row of SECURITY.md's key table:\n| {signer.fingerprint} | signing | {filed} |")
    typer.echo(f"keep it out of Time Machine:\ntmutil addexclusion {out}")


@app.command()
def build(
    key: KeyOpt,
    notes: Annotated[Path, typer.Option(help="The release notes: NOTES.txt and the tag's message.")],
    out: Annotated[Path, typer.Option(help="Where the bundle's folder and transport tar go.")],
    register: Annotated[Path, typer.Option(help="The register of signed releases: one JSON line each.")],
    platform: Annotated[str, typer.Option(help="linux/amd64 or linux/arm64.")] = "linux/amd64",
    passphrase: dsv.PassOpt = None,
) -> None:
    """Build, catalogue, scan, gate, sign and tag the release of HEAD (docs/mvp/RELEASE.md §9). It stages in a
    hidden folder in DIR and removes the image of a build it refuses, so a refusal leaves nothing."""
    if platform not in BASES:
        fail("--platform must be linux/amd64 or linux/arm64")
    signer = _release_key(key, passphrase, building=True)
    out, notes, register = out.absolute(), notes.absolute(), register.absolute()
    if not (notes.is_file() and os.access(notes, os.R_OK)):
        fail(f"--notes: {notes} is not a readable file")
    said = OPEN_BUILD.encode() in notes.read_bytes().splitlines()
    if not licence.SHIPPED_KEYS and not said:
        fail(f'--notes: a release without vendor keys is an open build: {notes} lacks the line "{OPEN_BUILD}"'
             " (LICENCE.md §19)")  # fmt: skip
    if licence.SHIPPED_KEYS and said:
        fail(f'--notes: a release with vendor keys enforces a licence: {notes} holds the line "{OPEN_BUILD}"'
             " (LICENCE.md §19)")  # fmt: skip
    if not (register.parent.is_dir() and os.access(register.parent, os.W_OK)):
        fail(f"--register: {register.parent} is not a writable folder")
    if register.exists() and not (register.is_file() and os.access(register, os.R_OK | os.W_OK)):
        fail(f"--register: {register} is not a writable file")
    if not out.parent.is_dir():
        fail(f"--out: {out.parent} is not a folder: build makes DIR, never its parents")
    made = not out.exists()
    out.mkdir(exist_ok=True)
    staging = _staging(Path(tempfile.mkdtemp(prefix=".deltasleuth-release-", dir=out)))
    unreleased: list[tuple[str, str, str | None]] = []  # _build's image, until it is released
    try:
        _build(signer, notes, out, register, platform, staging, unreleased)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if made and not any(out.iterdir()):
            out.rmdir()
        for image in unreleased:  # refused, or failed, after docker build
            _discard(*image)


def _discard(image: str, built: str, held: str | None) -> None:
    """Take a refused build's image out of the local store (§9): ``docker image rm`` of its tag, while the tag
    still names the image this run built, never forced and by no other reference, so an image under another
    tag or in use stays. The image the tag named before the build, which this run did not build and never
    removes, then gets its tag back. What could not be done is a ``warning:`` line."""
    if _inspect(image, "{{.Id}}") != built:
        left = f"{image} no longer names the image this build made, {_short(built)}: nothing was removed"
    elif (removed := _run(["docker", "image", "rm", image], check=False)).returncode:
        last = (removed.stderr.strip().splitlines() or [""])[-1]
        left = f"{image} is still in the local image store (docker image rm: {last})"
    elif held is None or not _run(["docker", "tag", held, image], check=False).returncode:
        return
    else:
        left = f"the image {image} named before the build is untagged now: run docker tag {held} {image}"
    typer.echo(f"warning: {left}", err=True)


def _build(
    signer: PackSigner, notes: Path, out: Path, register: Path, platform: str, staging: Path,
    unreleased: list[tuple[str, str, str | None]],
) -> None:  # fmt: skip
    facts = _static(platform, out, register, staging)
    version, stem, revision, vendor = facts["version"], facts["stem"], facts["revision"], facts["vendor"]
    entries, today = facts["entries"], facts["today"]
    ids = _local(platform, facts["python_index"], facts["build_bases"])
    bundle, image = staging / "bundle", f"deltasleuth:{version}"
    # 4. The build, from the source archive, never the working tree.
    _git("archive", "--format=tar", f"--prefix=deltasleuth-{version}/", "-o", str(staging / "source.tar"),
         revision, "--", *SHIPPED)  # fmt: skip
    with tarfile.open(staging / "source.tar") as tar:
        tar.extractall(staging / "context", filter="data")
    context = staging / "context" / f"deltasleuth-{version}"
    _gzip(staging / "source.tar", bundle / "source" / f"deltasleuth-{version}-src.tar.gz")
    args = {"PYTHON": f"python@{BASES[platform]}", "VERSION": version, "REVISION": revision,
            "CREATED": facts["created"], "VENDOR": vendor,
            "SOURCE_DATE_EPOCH": str(facts["epoch"])}  # fmt: skip
    held = _inspect(image, "{{.Id}}")  # what the tag names before the build, which no refusal removes
    _run(["docker", "build", "--no-cache", "--provenance=false", "--platform", platform, "-f",
          "deploy/Dockerfile", "-t", image, *(f"--build-arg={k}={v}" for k, v in args.items()), "."],
         cwd=context)  # fmt: skip
    if (made := _inspect(image, "{{.Id}}")) not in (None, held):  # the same id: it was there before
        unreleased.append((image, made, held))
    built, _, user = (_inspect(image, "{{.Os}}/{{.Architecture}} {{.Config.User}}") or "").partition(" ")
    if built != platform:
        fail(f"the built image is {built}, not {platform}")
    if user != "10001:10001":
        fail(f"the built image runs as {user or 'root'}, not 10001:10001")
    for ref, before in ids.items():
        if _inspect(ref, "{{.Id}}") != before:
            fail(f"the build changed the local image of {_short(ref)}: something pulled, and this tool"
                 " never pulls")  # fmt: skip
    # 5. The smoke test. 6. The save, its digests, the SBOMs and their completeness, the vendored files.
    _smoke(image, platform, staging)
    _run(["docker", "save", "-o", str(staging / "image.tar"), image])
    config, manifest = digests(staging / "image.tar", image)
    _sbom(staging, version, vendor, stem)
    probe = ["docker", "run", "--rm", "--pull", "never", "--platform", platform, "--network", "none"]
    dpkg = _run([*probe, "--entrypoint", "dpkg-query", image, "-W", "-f", "${db:Status-Abbrev}${Package}\\n"])
    python, *libs = _run([*probe, "--entrypoint", "python", image, "-c", LIBS]).stdout.splitlines()
    exported = _run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                     "--no-header", "--no-annotate"], cwd=context).stdout  # fmt: skip
    sbom = json.loads((bundle / "sbom" / f"{stem}.cdx.json").read_text())
    lock = json.loads((context / "web" / "package-lock.json").read_text())
    listed = [line[3:] for line in dpkg.stdout.splitlines() if line.startswith("ii ")]
    if problems := lacks(sbom, dpkg=listed, python=python, exported=exported, version=version, lock=lock,
                         machine=MACHINES[platform]):  # fmt: skip
        _refuse(problems)
    vendored = [(canonicalize_name(head.split()[0]), head.split()[1], path)
                for head, path in (line.split("\t") for line in libs)]  # fmt: skip
    if (checked := gate([], {}, entries, today, vendored)).refused:
        _refuse(checked.refused)
    (bundle / "sbom" / "vendored-libraries.txt").write_text(
        "".join(f"{d} {v}\t{p}\t{e['status']} until {e['expires']}\n" for (d, v, p), e in checked.decided)
    )
    _gzip(staging / "image.tar", bundle / "image" / f"{stem}.tar.gz")
    # 7. The scans, the gate and the VEX, which grype must apply to both SBOMs.
    (bundle / "scan").mkdir()
    _scan(staging, ["grype", "db", "update"], network=True)
    status = json.loads(_scan(staging, ["grype", "db", "status", "-o", "json"]))
    scanned = _grype(staging, f"bundle/sbom/{stem}.cdx.json")
    matches, descriptor = scanned.get("matches", []), scanned.get("descriptor", {})
    cut = {"name": descriptor.get("name"), "version": descriptor.get("version"),
           "db": {"status": {k: v for k, v in status.items() if k != "path"}}}  # fmt: skip
    (bundle / "scan" / "grype.json").write_text(json.dumps(scanned | {"descriptor": cut}, indent=2) + "\n")
    audit = _pip_audit(staging, sbom, "bundle/scan/pip-audit.json")
    result = gate(matches, audit, entries, today, vendored)
    if result.blocking:
        _refuse([f"{len(result.blocking)} finding(s) block the release; upgrade, or triage each in"
                 " tools/triage.toml:"], [finding.line() for finding in result.blocking])  # fmt: skip
    for line in result.unused:
        typer.echo(f"warning: {line}", err=True)
    signed = f"{_now():%Y-%m-%dT%H:%M:%SZ}"
    vex = json.dumps(_vex(result, facts, platform, manifest, signed), indent=2) + "\n"
    (bundle / "scan" / "vex.openvex.json").write_text(vex)
    (bundle / "scan" / "SUMMARY.txt").write_text(_summary(result, matches, status, stem))
    for kind in ("cdx", "spdx"):
        vexed = _grype(staging, f"bundle/sbom/{stem}.{kind}.json", "--vex", "bundle/scan/vex.openvex.json")
        left = {(m["vulnerability"]["id"], m["artifact"].get("purl", "").split("?")[0])
                for m in vexed.get("matches", [])}  # fmt: skip
        for finding, entry in result.triaged:
            if entry["status"] == "not_affected" and (finding.id, finding.purl) in left:
                fail(f"the VEX did not suppress {finding.id} for {finding.purl}")
    # 8. The checks, then the tree as it was.
    for argv in CHECKS:
        required = os.environ | {"DS_REQUIRE_DB": "1", "DS_REQUIRE_TOOLS": "1"}
        done = _run(argv, check=False, cwd=ROOT, env=required)
        if done.returncode:
            typer.echo((done.stdout + done.stderr).rstrip("\n"), err=True)
            fail(f"checks failed: {' '.join(argv)} (exit {done.returncode})")
    head, changed = _git("rev-parse", "HEAD"), _porcelain()
    if head != revision or changed:
        fail(f"the tree changed during the build (HEAD {head[:12]}…, {len(changed)} path changed):"
             " nothing was released")  # fmt: skip
    # 9. Assemble, register, sign, self-check, the transport tar, into DIR, tag.
    reported = facts["reported"]
    release = {
        "format": FORMAT, "version": version, "revision": revision, "created": facts["created"],
        "platform": platform, "image": image, "config": config, "manifest": manifest,
        "base": f"docker.io/library/python@{BASES[platform]} ({platform} of {facts['python_index']})",
        "build-base": " ".join(b if "/" in b.partition("@")[0] else f"docker.io/library/{b}"
                               for b in facts["build_bases"]),
        "vendor": vendor, "key": signer.fingerprint, "signed": signed,
        "sbom": f"syft {reported['syft']}; CycloneDX 1.6, SPDX 2.3",
        "scan": f"grype {reported['grype']}, database {status.get('schemaVersion')} built"
                f" {status.get('built')}; pip-audit {installed('pip-audit')}",
        "gate": result.line(),
    }  # fmt: skip
    verify = (f"DeltaSleuth release {version} for {platform}. In its folder, run in sh the copy of the"
              " procedure below filed with the release key; its step 0 reads line 2:\n"
              f"VERSION={version} PLATFORM={platform.replace('/', '-')}\n{PROCEDURE}")  # fmt: skip
    files = {"RELEASE.txt": "".join(f"{k}: {v}\n" for k, v in release.items()).encode(),
             "VERIFY.txt": verify.encode(), "NOTES.txt": notes.read_bytes(),
             "SECURITY.md": (context / "SECURITY.md").read_bytes(), **_transition(signer)}  # fmt: skip
    for name, data in files.items():
        dsv.create(bundle / name, data)
    names = sorted(path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file())
    sums = "".join(f"{_sha256(bundle / name)}  {name}\n" for name in names).encode()
    dsv.create(bundle / "SHA256SUMS", sums)
    entry = {"version": version, "platform": platform, "revision": revision, "created": facts["created"],
             "signed": signed, "key": signer.fingerprint, "config": config, "manifest": manifest,
             "sums_sha256": hashlib.sha256(sums).hexdigest()}  # fmt: skip
    strike = f"strike any line of sums_sha256 {entry['sums_sha256']} from {register}"
    target, transport, staged = out / stem, out / f"{stem}.tar", staging / stem
    try:  # a failure from here leaves nothing but the register line it names
        with register.open("a", encoding="utf-8") as handle:  # first: no genuine release goes unregistered
            handle.write(json.dumps(entry) + "\n")
        signature = signer.sign(sums)
        dsv.create(bundle / "SHA256SUMS.sig", signature)
        dsv.create(bundle / "release-key.pem", signer.public_pem)
        rehashed = "".join(f"{_sha256(bundle / name)}  {name}\n" for name in names).encode()
        if rehashed != sums or not signing.verifies(signer.public_pem, signature, sums):
            fail(f"the bundle failed its self-check: nothing was released; {strike}")
        bundle.rename(staged)  # the transport tar is written in staging, then both move to DIR
        _transport(staged, staging / transport.name, facts["epoch"])
        (staging / transport.name).rename(transport)
        staged.rename(target)
    except OSError as exc:
        fail(f"the bundle could not be written ({exc}): nothing was released; {strike}")
    unreleased.clear()  # released: its image stays, whatever becomes of the git tag
    if not facts["tagged"]:  # the tag of another platform's bundle stands
        tag = ["git", "tag", "-a", "--cleanup=verbatim", f"v{version}", revision, "-F", str(notes)]
        if _run(tag, check=False, cwd=ROOT).returncode:
            fail(f"the release is at {target} but git tag failed: run {' '.join(tag)}")
    typer.echo(f"released DeltaSleuth {version} for {platform}, image {_short(config)}, signed by key"
               f" {signer.fingerprint[:12]}…, tagged v{version}")  # fmt: skip
    typer.echo(f"sums_sha256: {entry['sums_sha256']} (in the register; file it with the delivery)")
    typer.echo(f"transport: {transport} sha256 {_sha256(transport)} (for the transfer ticket)")
    typer.echo(f"gate: {result.line()}")


@app.command()
def rescan(bundles: Annotated[list[Path], typer.Argument(help="Archived bundle folders.")]) -> None:
    """Apply today's grype database, pip-audit's advisories and tools/triage.toml to archived bundles, in the
    build's environment; exit 2 lists what would block (§8)."""
    entries, problems = triage((ROOT / "tools" / "triage.toml").read_text())
    if problems:
        _refuse(problems)
    blocked = False
    with tempfile.TemporaryDirectory(prefix="deltasleuth-rescan-") as tmp:
        staging = _staging(Path(tmp))
        _approved(staging)
        _scan(staging, ["grype", "db", "update"], network=True)
        for folder in bundles:
            facts = dict(line.split(": ", 1) for line in (folder / "RELEASE.txt").read_text().splitlines())
            stem = f"deltasleuth-{facts['version']}-{facts['platform'].replace('/', '-')}"
            sbom = folder.resolve() / "sbom" / f"{stem}.cdx.json"
            matches = _grype(staging, str(sbom)).get("matches", [])
            audit = _pip_audit(staging, json.loads(sbom.read_text()), "pip-audit.json")
            (staging / "pip-audit.json").unlink()
            decided = (folder / "sbom" / "vendored-libraries.txt").read_text().splitlines()
            vendored = [
                (*head.split(" ", 1), path) for head, path, _ in (line.split("\t") for line in decided)
            ]
            result = gate(matches, audit, entries, _now().date(), vendored)  # type: ignore[arg-type]
            typer.echo(f"{stem}: {result.line()}")
            for line in result.unused:
                typer.echo(f"warning: {line}", err=True)
            if result.blocking or result.refused:
                blocked = True
                lines = [f.line() for f in result.blocking] + [f"  {problem}" for problem in result.refused]
                typer.echo(f"error: {stem}: {len(lines)} finding(s) would block the release; upgrade, or"
                           " triage each in tools/triage.toml:\n" + "\n".join(lines), err=True)  # fmt: skip
    if blocked:
        raise typer.Exit(2)


@app.command()
def sign(
    file: Annotated[Path, typer.Argument(help="An advisory, a key transition or an OpenVEX document.")],
    key: KeyOpt,
    passphrase: dsv.PassOpt = None,
) -> None:
    """Sign FILE with the release key into FILE.sig (DER ECDSA P-256/SHA-256, as openssl dgst verifies it)."""
    data = file.read_bytes()
    if not _signable(data):
        fail(f"{file} is not an advisory, a key transition or an OpenVEX document: the release key signs"
             " nothing else")  # fmt: skip
    if CHECKSUM.search(data.decode()):  # step 3 would take it for SHA256SUMS
        fail(f"{file} has a line that sha256sum or shasum would check: the release key signs no checksum"
             ' list, so begin no line with a digest or "SHA… ("')  # fmt: skip
    if (target := file.with_name(f"{file.name}.sig")).exists():
        fail(f"{target} exists: sign never writes over a file")
    signer = _release_key(key, passphrase, building=False)
    dsv.create(target, signer.sign(data))
    typer.echo(f"signed {file} with key {signer.fingerprint[:12]}…: {target}")


def _signable(data: bytes) -> bool:
    try:
        text = data.decode()
        if ADVISORY.fullmatch(first := text.split("\n", 1)[0]) or first == TRANSITION:
            return True
        document = json.loads(text)
    except ValueError:  # not UTF-8, or not JSON
        return False
    return isinstance(document, dict) and str(document.get("@context", "")).startswith("https://openvex.dev/")


if __name__ == "__main__":
    app()
