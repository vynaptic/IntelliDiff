"""tools/release.py (docs/mvp/RELEASE.md §13): keygen writes an encrypted key and its procedure outside every
repository and synced folder; build turns a scratch checkout into a signed bundle, running stub docker, syft,
grype, uv, npm and pip-audit as §7 and §9 set, and refuses what §9 lists, leaving nothing; the gate and the
SBOM completeness check are pure; rescan applies today's triage; sign signs only advisories, key transitions
and OpenVEX; and the real syft and grype run as the tool runs them, deaf to any other configuration."""

from __future__ import annotations

import errno
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import entry_points, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from typer.testing import CliRunner, Result

from deltasleuth import licence
from deltasleuth.config import load_settings
from deltasleuth.core.spec.targets import load_targets
from deltasleuth.security import signing
from tests.conftest import PG_IMAGE
from tests.unit import releases
from tests.unit.releases import LIBCRYPTO, PASSPHRASE, PYTHON_CVE, PYTHON_PURL, Scratch, match
from tools import release

STEM = "deltasleuth-0.1.0-linux-amd64"
LISTED = [
    "NOTES.txt", "RELEASE.txt", "SECURITY.md", "VERIFY.txt", f"image/{STEM}.tar.gz", f"sbom/{STEM}.cdx.json",
    f"sbom/{STEM}.spdx.json", "sbom/vendored-libraries.txt", "scan/SUMMARY.txt", "scan/grype.json",
    "scan/pip-audit.json", "scan/vex.openvex.json", "source/deltasleuth-0.1.0-src.tar.gz",
]  # fmt: skip
UNLISTED = ["SHA256SUMS", "SHA256SUMS.sig", "release-key.pem"]
SCANNING = {"PATH", "HOME", "TZ", "GRYPE_DB_CACHE_DIR", "GRYPE_DB_AUTO_UPDATE", "GRYPE_CHECK_FOR_APP_UPDATE",
            "SYFT_CHECK_FOR_APP_UPDATE"}  # fmt: skip
PIN = "is not pinned by digest (name:tag@sha256:<64 hex>)"
PSYCOPG = "pkg:pypi/psycopg-binary@3.3.6"
TODAY = datetime.now(UTC).date()
YESTERDAY, TOMORROW = TODAY - timedelta(days=1), TODAY + timedelta(days=1)
LATER = TODAY + timedelta(days=2)  # past the day's grace for the maintainer's time zone
VENDOR_KEY = (
    "--key: key {fp}… is a licence vendor key: releases are signed with a release key (release keygen)"
)
NOTES = "# DeltaSleuth 0.1.0\n\nThe first release.\n"  # of a build that embeds vendor keys


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> Scratch:
    return releases.scratch(tmp_path, monkeypatch, repo_root)


def _release(bundle: Path) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in (bundle / "RELEASE.txt").read_text().splitlines())


def _members(archive: Path) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive.read_bytes())))


def _public(scratch: Scratch) -> Path:
    return scratch.key.with_name(scratch.key.name.replace(".key.", ".pub."))


def test_keygen_writes_an_encrypted_key_and_the_procedure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "passphrase").write_text(f"{PASSPHRASE}\n")
    done = releases.keygen(tmp_path / "keys", tmp_path / "passphrase")
    assert done.exit_code == 0, done.output
    out = (tmp_path / "keys").resolve()
    [key] = out.glob("release-*.key.pem")
    public = key.with_name(key.name.replace(".key.", ".pub.")).read_bytes()
    fingerprint = signing.fingerprint(public)
    assert key.name == f"release-{fingerprint[:16]}.key.pem" and stat.S_IMODE(key.stat().st_mode) == 0o600
    with pytest.raises(TypeError, match="encrypted"):
        serialization.load_pem_private_key(key.read_bytes(), password=None)
    unlocked = serialization.load_pem_private_key(key.read_bytes(), PASSPHRASE.encode())
    assert signing.PackSigner(unlocked).public_pem == public  # type: ignore[arg-type]
    assert (out / "procedure.txt").read_text() == release.PROCEDURE
    filed = hashlib.sha256((out / "procedure.txt").read_bytes()).hexdigest()  # the hash on the printed line
    stem = f"release-{fingerprint[:16]}"
    assert done.output.splitlines() == [
        f"fingerprint: {fingerprint}",
        f"wrote {stem}.key.pem (encrypted, mode 0600), {stem}.pub.pem and procedure.txt to {out}",
        "the line of tools/release-keys.txt:", f"{fingerprint} signing procedure={filed}",
        "the row of SECURITY.md's key table:", f"| {fingerprint} | signing | {filed} |",
        "keep it out of Time Machine:", f"tmutil addexclusion {out}",
    ]  # fmt: skip
    again = releases.keygen(tmp_path / "keys", tmp_path / "passphrase")
    assert (again.exit_code, again.output) == (
        2,
        f"error: {out}/procedure.txt exists: keygen never writes over a file\n",
    )
    assert len(list(out.iterdir())) == 3
    relative = CliRunner().invoke(
        release.app, ["keygen", "--out", str(tmp_path / "k2"), "--passphrase", "file:p"]
    )
    assert (relative.exit_code, relative.output) == (2, "error: --passphrase must be file:<absolute path>\n")


def test_keygen_refuses_a_git_working_tree_or_a_synced_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "passphrase").write_text(f"{PASSPHRASE}\n")
    (tmp_path / "checkout" / ".git").mkdir(parents=True)
    (home / "Documents").mkdir(parents=True)
    (tmp_path / "link").symlink_to(home / "Documents")  # resolved, it is in ~/Documents
    synced = "in a cloud-synced folder (~/{}): keep release keys on this machine only"
    git = "in a git working tree: keep release keys outside every repository"
    cases = [(tmp_path / "checkout" / "keys", git),
             *((home / folder / "keys", synced.format(folder)) for folder in release.SYNCED),
             (tmp_path / "link" / "keys", synced.format("Documents"))]  # fmt: skip
    if (home / "documents").exists():  # a case-insensitive volume, as APFS by default: the same folder
        cases.append((home / "documents" / "keys", synced.format("Documents")))
    for out, refusal in cases:
        done = releases.keygen(out, tmp_path / "passphrase")
        assert (done.exit_code, done.output) == (2, f"error: {out.resolve()} is {refusal}\n")
        assert not out.exists()


def test_build_writes_the_bundle(scratch: Scratch, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    revision = scratch.git("rev-parse", "HEAD")
    seen: dict[str, bool] = {}
    sign, run = signing.PackSigner.sign, release._run

    def signs(signer: signing.PackSigner, data: bytes) -> bytes:  # the register holds the list first
        seen["registered"] = hashlib.sha256(data).hexdigest() in scratch.register.read_text()
        return sign(signer, data)

    def runs(argv: Any, **kwargs: Any) -> Any:  # the tag comes after the rename and the transport tar
        if list(argv)[:2] == ["git", "tag"]:
            seen["tagged"] = scratch.bundle().is_dir() and (scratch.out / f"{STEM}.tar").is_file()
        return run(argv, **kwargs)

    monkeypatch.setattr(signing.PackSigner, "sign", signs)
    monkeypatch.setattr(release, "_run", runs)
    done = scratch.build()
    assert done.exit_code == 0, done.output
    assert seen == {"registered": True, "tagged": True}
    bundle = scratch.bundle()
    assert sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()) == sorted(
        LISTED + UNLISTED
    )
    assert (bundle / "SECURITY.md").read_bytes() == (scratch.root / "SECURITY.md").read_bytes()
    assert (bundle / "NOTES.txt").read_bytes() == scratch.notes.read_bytes()
    sums = (bundle / "SHA256SUMS").read_bytes()
    assert sums.decode() == "".join(
        f"{hashlib.sha256((bundle / n).read_bytes()).hexdigest()}  {n}\n" for n in LISTED
    )
    public = (bundle / "release-key.pem").read_bytes()
    assert public == _public(scratch).read_bytes()
    assert signing.verifies(public, (bundle / "SHA256SUMS.sig").read_bytes(), sums)
    verify = ["openssl", "dgst", "-sha256", "-verify", "release-key.pem", "-signature", "SHA256SUMS.sig"]
    verified = subprocess.run([*verify, "SHA256SUMS"], cwd=bundle, capture_output=True, text=True)
    assert verified.stdout == "Verified OK\n"
    facts = _release(bundle)
    with _members(bundle / "image" / f"{STEM}.tar.gz") as tar:  # the digests are the archive's
        manifest = json.load(tar.extractfile("index.json"))["manifests"][0]["digest"]  # type: ignore[arg-type]
        config = json.load(tar.extractfile(f"blobs/sha256/{manifest[7:]}"))["config"]["digest"]  # type: ignore[arg-type]
    node = next(ref for ref in scratch.scenario["images"] if ref.startswith("node:"))
    assert facts == {
        "format": "deltasleuth-release/1", "version": "0.1.0", "revision": revision,
        "created": "2026-09-28T12:00:00Z", "platform": "linux/amd64", "image": "deltasleuth:0.1.0",
        "config": config, "manifest": manifest,
        "base": f"docker.io/library/python@{release.BASES['linux/amd64']} (linux/amd64 of"
                f" python:3.13-slim@{release.INDEX})",
        "build-base": f"docker.io/library/{node}", "vendor": "Example Vendor Ltd", "key": scratch.fingerprint,
        "signed": facts["signed"], "sbom": "syft 1.52.0; CycloneDX 1.6, SPDX 2.3",
        "scan": "grype 0.119.0, database v6.1.9 built 2026-09-29T06:32:31Z; pip-audit"
                f" {version('pip-audit')}",
        "gate": "passed; 0 blocking, 1 triaged, 2 reported, 1 vendored file(s) decided",
    }  # fmt: skip
    assert config == scratch.state()["images"]["deltasleuth:0.1.0"]["id"]
    for packed in (bundle / "image" / f"{STEM}.tar.gz", bundle / "source" / "deltasleuth-0.1.0-src.tar.gz"):
        head = packed.read_bytes()[:10]
        assert head[3] & 0x08 == 0 and head[4:8] == b"\0\0\0\0"  # no name, mtime 0
    with _members(bundle / "source" / "deltasleuth-0.1.0-src.tar.gz") as tar:
        shipped = sorted(m.name.removeprefix("deltasleuth-0.1.0/") for m in tar.getmembers() if m.isfile())
    assert {name.split("/")[0] for name in shipped} == {entry.split("/")[0] for entry in release.SHIPPED}
    unshipped = {"docs/drafts/notes.md", "tests/test_nothing.py", "tools/triage.toml", ".env",
                 "deploy/secrets/kek", "deploy/docker-compose.site.yml"}  # fmt: skip
    assert {"docs/mvp/DEPLOY.md", "docs/guide/01-install-docker.md"} <= set(shipped)
    assert not unshipped & set(shipped)
    assert scratch.state()["context"] == shipped  # docker built the archive, not the working tree
    assert (bundle / "VERIFY.txt").read_text().split("\n", 2) == [
        "DeltaSleuth release 0.1.0 for linux/amd64. In its folder, run in sh the copy of the procedure below"
        " filed with the release key; its step 0 reads line 2:",
        "VERSION=0.1.0 PLATFORM=linux-amd64", release.PROCEDURE,
    ]  # fmt: skip
    image = f"pkg:oci/deltasleuth@sha256%3A{manifest.removeprefix('sha256:')}?tag=0.1.0"
    assert json.loads((bundle / "scan" / "vex.openvex.json").read_text()) == {
        "@context": "https://openvex.dev/ns/v0.2.0", "@id": "urn:deltasleuth:vex:0.1.0:linux-amd64",
        "author": "Example Vendor Ltd", "timestamp": facts["signed"], "version": 1,
        "statements": [{
            "vulnerability": {"name": PYTHON_CVE, "aliases": []},
            "products": [{"@id": PYTHON_PURL}, {"@id": image, "subcomponents": [{"@id": PYTHON_PURL}]}],
            "status": "not_affected", "justification": "vulnerable_code_not_in_execute_path",
            "impact_statement": "DeltaSleuth never calls the affected function.",
        }],
    }  # fmt: skip
    soon = TODAY + timedelta(days=20)
    assert (bundle / "sbom" / "vendored-libraries.txt").read_text() == (
        f"psycopg-binary 3.3.6\t{LIBCRYPTO}\taffected until {soon}\n"
    )
    summary = (bundle / "scan" / "SUMMARY.txt").read_text()
    for part in (
        f"Result: {facts['gate']}\n",
        f"  {PYTHON_CVE} {PYTHON_PURL}: not_affected",
        "  High: fixed 1, wont-fix 1\n  Medium: fixed 1\n",
        f"  psycopg-binary 3.3.6 {LIBCRYPTO}: affected until {soon}",
    ):
        assert part in summary  # fmt: skip
    status = {name: value for name, value in scratch.scenario["db_status"].items() if name != "path"}
    descriptor = json.loads((bundle / "scan" / "grype.json").read_text())["descriptor"]
    assert descriptor == {"name": "grype", "version": "0.119.0", "db": {"status": status}}
    assert scratch.state()["audited"] == "psycopg-binary==3.3.6\n"  # each pkg:pypi component but deltasleuth
    [entry] = [json.loads(line) for line in scratch.register.read_text().splitlines()]
    assert entry == {"version": "0.1.0", "platform": "linux/amd64", "revision": revision,
                     "created": "2026-09-28T12:00:00Z", "signed": facts["signed"], "key": scratch.fingerprint,
                     "config": config, "manifest": manifest,
                     "sums_sha256": hashlib.sha256(sums).hexdigest()}  # fmt: skip
    assert scratch.git("rev-parse", "v0.1.0^{commit}") == revision
    assert scratch.git("tag", "-l", "--format=%(contents)", "v0.1.0") == scratch.notes.read_text().strip()
    transport = scratch.out / f"{STEM}.tar"
    with tarfile.open(transport) as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == [
        STEM,
        *(f"{STEM}/{p.relative_to(bundle)}" for p in sorted(bundle.rglob("*"))),
    ]
    epoch = int(datetime.fromisoformat(releases.COMMITTED).timestamp())
    kinds = {(m.type, m.mode, m.uid, m.gid, m.uname, m.gname, m.mtime) for m in members}
    assert kinds == {
        (tarfile.DIRTYPE, 0o755, 0, 0, "", "", epoch),
        (tarfile.REGTYPE, 0o644, 0, 0, "", "", epoch),
    }
    assert transport.read_bytes()[257:262] == b"ustar"
    assert done.output.splitlines() == [
        f"released DeltaSleuth 0.1.0 for linux/amd64, image {config[:19]}…, signed by key"
        f" {scratch.fingerprint[:12]}…, tagged v0.1.0",
        f"sums_sha256: {entry['sums_sha256']} (in the register; file it with the delivery)",
        f"transport: {transport} sha256 {hashlib.sha256(transport.read_bytes()).hexdigest()} (for the"
        " transfer ticket)",
        f"gate: {facts['gate']}",
    ]  # fmt: skip
    state = scratch.state()
    assert (state["containers"], state["networks"], state["volumes"]) == ([], [], [])
    assert sorted(path.name for path in scratch.out.iterdir()) == [STEM, f"{STEM}.tar"]
    docker = [run["argv"] for run in scratch.runs("docker")]
    [build] = [argv for argv in docker if argv[0] == "build"]
    args = {"PYTHON": f"python@{release.BASES['linux/amd64']}", "VERSION": "0.1.0", "REVISION": revision,
            "CREATED": "2026-09-28T12:00:00Z", "VENDOR": "Example Vendor Ltd",
            "SOURCE_DATE_EPOCH": str(epoch)}  # fmt: skip
    assert build == ["build", "--no-cache", "--provenance=false", "--platform", "linux/amd64", "-f",
                     "deploy/Dockerfile", "-t", "deltasleuth:0.1.0",
                     *(f"--build-arg={k}={v}" for k, v in args.items()), "."]  # fmt: skip
    app = yaml.safe_load((repo_root / "deploy" / "docker-compose.yml").read_text())["x-release"]
    hardened = ("--read-only", "--tmpfs", *app["tmpfs"], "--cap-drop", *app["cap_drop"], "--security-opt",
                *app["security_opt"])  # fmt: skip
    assert app["read_only"] and release.HARDENED == hardened
    for command in (["ds", "init"], ["ds", "serve", "--host", "127.0.0.1"]):  # under the compose hardening
        [argv] = [argv for argv in docker if argv[-len(command) :] == command]
        at = argv.index("--read-only")
        assert argv[at : at + len(release.HARDENED)] == list(release.HARDENED) and "--internal" not in argv
    probes = [argv[:9] for argv in docker if "--entrypoint" in argv]
    assert probes == [["run", "--rm", "--pull", "never", "--platform", "linux/amd64", "--network", "none",
                       "--entrypoint"]] * 2  # fmt: skip
    [syft] = [run for run in scratch.runs("syft") if run["argv"][0] == "scan"]
    assert syft["argv"] == [
        "scan", "docker-archive:image.tar", "-q", "--select-catalogers", "+javascript-lock-cataloger",
        "--source-name", "deltasleuth", "--source-version", "0.1.0", "--source-supplier",
        "Example Vendor Ltd", "-o", f"cyclonedx-json@1.6=bundle/sbom/{STEM}.cdx.json",
        "-o", f"spdx-json@2.3=bundle/sbom/{STEM}.spdx.json",
        "-c", f"{syft['cwd']}/empty.yaml",
    ]  # fmt: skip
    [audit] = scratch.runs("python")
    assert audit["argv"] == ["-m", "pip_audit", "-r", "pip-audit.txt", "--no-deps", "--disable-pip",
                             "--progress-spinner", "off", "-f", "json", "-o",
                             "bundle/scan/pip-audit.json"]  # fmt: skip


Setup = Callable[[Scratch, pytest.MonkeyPatch], dict[str, str] | None]


def _key_in(folder: Callable[[Scratch], Path], option: str = "key") -> Setup:
    def moved(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
        (target := folder(scratch)).mkdir(parents=True, exist_ok=True)
        return {option: f"file:{shutil.copy(getattr(scratch, option), target)}"}

    return moved


def _commit(path: str, change: Callable[[str], str]) -> Setup:
    def committed(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
        scratch.commit({path: change((scratch.root / path).read_text())})

    return committed


def _scenario(**changes: Any) -> Setup:
    def changed(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, change in changes.items():
            scratch.scenario[name] = change(scratch.scenario[name]) if callable(change) else change

    return changed


def _expire(days: int) -> Callable[[str], str]:
    """The triage with the decision that ran ``days`` from today expired yesterday."""
    running = f"decided = {TODAY}\nexpires = {TODAY + timedelta(days=days)}"
    return lambda text: text.replace(
        running, f"decided = {TODAY - timedelta(days=10)}\nexpires = {YESTERDAY}"
    )


def _patch(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.1.1 after the tag v0.1.0, with a migration 0.1.0 lacks."""
    compose = "services:\n  app: {image: 'deltasleuth:%s'}\n  mocks: {image: 'deltasleuth:%s'}\n"
    scratch.git("tag", "v0.1.0")
    scratch.commit({
        "pyproject.toml": (scratch.root / "pyproject.toml").read_text().replace('"0.1.0"', '"0.1.1"'),
        "deploy/docker-compose.yml": compose % ("0.1.1", "0.1.1"),
        "deploy/docker-compose.ca.yml": compose % ("0.1.1-ca", "0.1.1-ca"),
        "deltasleuth/store/migrations/002_more.sql": "--\n",
    })  # fmt: skip


def _malformed(text: str) -> str:
    """An e-mail address as by, a justification OpenVEX lacks, an unknown key and an unknown table."""
    text = text.replace('by = "maintainer"', 'by = "o@vendor.test"', 1).replace(
        "vulnerable_code_not_in_execute_path", "x"
    )
    return text.replace("files = [", 'reason = "x"\nfiles = [') + "[[waiver]]\nid = 1\n"


def _just(action: Callable[[Scratch], object]) -> Setup:
    def done(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
        action(scratch)

    return done


def _open_build(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    """Notes that say nothing of an open build, in a checkout that embeds no vendor key."""
    scratch.notes.write_text(NOTES)
    monkeypatch.setattr(licence, "SHIPPED_KEYS", ())


def _vendor_keys(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scratch's notes, copied from an open build's, in a checkout that embeds two vendor keys."""
    vendors = (ec.generate_private_key(ec.SECP256R1()) for _ in range(2))
    monkeypatch.setattr(licence, "SHIPPED_KEYS", tuple(signing.PackSigner(key).public_pem for key in vendors))


def _vendor_build(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Notes that say nothing of an open build, in a checkout that embeds vendor keys; then the next
    refusal's setup, a register that is a folder."""
    _vendor_keys(scratch, monkeypatch)
    scratch.notes.write_text(NOTES)
    return {"register": str(scratch.out)}


# Each refusal of RELEASE.md §9, in its order: how to provoke it, and its output, formatted with the key's
# {fp} (12 digits), {out}, {notes}, the {head} after the build (12 digits) and the Dockerfile lines {from}
# and {arg}.
KEYS, TRIAGE, SYNCED = "tools/release-keys.txt", "tools/triage.toml", "keep release keys on this machine only"
REFUSALS: dict[str, tuple[Setup, str]] = {
    "relative key": (lambda s, m: {"key": "file:release.key.pem"}, "--key must be file:<absolute path>"),
    "wrong passphrase": (
        _just(lambda s: s.passphrase.write_text("not it\n")),
        "--key: not an encrypted PEM private key, or not its passphrase",
    ),
    "key in a git tree": (
        _key_in(lambda s: s.root / "keys"),
        "--key: the key file is in a git working tree: keep release keys outside every repository",
    ),
    "key in ~/Documents": (
        _key_in(lambda s: s.tmp / "home" / "Documents" / "keys"),
        f"--key: the key file is in a cloud-synced folder (~/Documents): {SYNCED}",
    ),
    "passphrase in iCloud": (
        _key_in(lambda s: s.tmp / "home" / "Library/Mobile Documents/iCloud", "passphrase"),
        "--passphrase: the passphrase file is in a cloud-synced folder (~/Library/Mobile Documents):"
        f" {SYNCED}",
    ),
    "vendor key": (lambda s, m: m.setattr(licence, "SHIPPED_KEYS", (_public(s).read_bytes(),)), VENDOR_KEY),
    "removed vendor key": (
        lambda s, m: m.setattr(licence, "REMOVED_KEYS", {s.fingerprint: "2026-01-05"}),
        VENDOR_KEY,
    ),
    "standby key": (
        _commit(KEYS, lambda t: t.replace(" signing ", " standby ")),
        "--key: key {fp}… is not the signing key tools/release-keys.txt lists",
    ),
    "unlisted key": (
        _commit(KEYS, lambda t: t.split("\n")[0] + "\n"),
        "--key: key {fp}… is not the signing key tools/release-keys.txt lists",
    ),
    "changed procedure": (
        _commit(KEYS, lambda t: t[: t.index("procedure=") + 10] + "0" * 64 + "\n"),
        "release.PROCEDURE differs from the procedure filed with key {fp}…: a changed procedure is filed like"
        " a new key (RELEASE.md §4)",
    ),
    "missing notes": (
        lambda s, m: {"notes": str(s.out / "notes.txt")},
        "--notes: {out}/notes.txt is not a readable file",
    ),
    "notes that do not say the build is open": (
        _open_build,
        "--notes: a release without vendor keys is an open build: {notes} lacks the line"
        f' "{release.OPEN_BUILD}" (LICENCE.md §19)',
    ),
    "a vendor build's notes that say it is open": (
        _vendor_keys,
        "--notes: a release with vendor keys enforces a licence: {notes} holds the line"
        f' "{release.OPEN_BUILD}" (LICENCE.md §19)',
    ),
    "a vendor build's notes say nothing of it": (_vendor_build, "--register: {out} is not a writable file"),
    "register in a missing folder": (
        lambda s, m: {"register": str(s.out / "new" / "register.jsonl")},
        "--register: {out}/new is not a writable folder",
    ),
    "register a folder": (lambda s, m: {"register": str(s.out)}, "--register: {out} is not a writable file"),
    "out in a missing folder": (
        lambda s, m: {"out": str(s.out / "new" / "out")},
        "--out: {out}/new is not a folder: build makes DIR, never its parents",
    ),
    "dev version": (
        _commit("pyproject.toml", lambda t: t.replace('"0.1.0"', '"0.2.0.dev0"')),
        "version 0.2.0.dev0 is not a release: set X.Y.Z or X.Y.ZrcN in pyproject.toml",
    ),
    "untracked file": (
        _just(lambda s: (s.root / "scratch.txt").write_text("x")),
        "the working tree is not clean: commit or remove 1 path(s) first",
    ),
    "released version": (  # on another revision: a tag on HEAD is another platform's (test_one_version_…)
        _just(lambda s: (s.git("tag", "v0.1.0"), s.commit({"README.md": "# DeltaSleuth, later\n"}))),
        "tag v0.1.0 is on another revision: a released version is never rebuilt",
    ),
    "compose version": (
        _commit("deploy/docker-compose.yml", lambda t: t.replace("0.1.0", "0.0.9", 1)),
        "deploy/docker-compose.yml names deltasleuth:0.0.9, not deltasleuth:0.1.0",
    ),
    "CA override version": (
        _commit("deploy/docker-compose.ca.yml", lambda t: t.replace("0.1.0", "0.0.9")),
        "deploy/docker-compose.ca.yml names deltasleuth:0.0.9-ca, not deltasleuth:0.1.0-ca",
    ),
    "unpinned FROM": (
        _commit("deploy/Dockerfile", lambda t: t.replace("node:24-alpine@", "node:24-alpine #")),
        "{from}: FROM node:24-alpine " + PIN,
    ),
    "unpinned ARG": (
        _commit("deploy/Dockerfile", lambda t: t.replace("python:3.13-slim@", "python:3.13-slim #")),
        "{arg}: ARG PYTHON=python:3.13-slim " + PIN,
    ),
    "no vendor": (
        _commit("pyproject.toml", lambda t: t.replace('authors = [{name = "Example Vendor Ltd"}]\n', "")),
        'pyproject.toml names no vendor: set [project] authors = [{{name = "<legal name>"}}]',
    ),
    "vendor e-mail": (
        _commit("pyproject.toml", lambda t: t.replace('Ltd"}', 'Ltd", email = "sales@vendor.test"}')),
        "pyproject.toml [project].authors has an email: it would ship in the image and the SBOM; name the"
        " vendor only",
    ),
    "contact placeholder": (
        _commit("SECURITY.md", lambda t: "Report to <SECURITY-CONTACT>.\n"),
        "SECURITY.md still holds <SECURITY-CONTACT>: name the security contact first",
    ),
    "maintainer's checklist": (
        _commit("SECURITY.md", lambda t: f"{t}\n{release.CHECKLIST}\n\n1. Pull the amd64 base.\n"),
        'SECURITY.md still holds "Before the first release (the vendor\'s checklist)": delete that section'
        " first",
    ),
    "another grype": (
        _scenario(grype_version="0.120.0"),
        "grype 0.120.0 is not the approved 0.119.0: install it, or change release.GRYPE on purpose",
    ),
    "another syft": (
        _scenario(syft_version="1.53.0"),
        "syft 1.53.0 is not the approved 1.52.0: install it, or change release.SYFT on purpose",
    ),
    "expired finding": (
        _commit(TRIAGE, _expire(80)),
        f"tools/triage.toml: {PYTHON_CVE} for {PYTHON_PURL} expired on {YESTERDAY}: decide again",
    ),
    "expired vendored": (
        _commit(TRIAGE, _expire(20)),
        f"tools/triage.toml: [[vendored]] psycopg-binary 3.3.6 expired on {YESTERDAY}: decide again",
    ),
    "decided in the future": (
        _commit(TRIAGE, lambda t: t.replace(f"decided = {TODAY}", f"decided = {LATER}", 1)),
        f"tools/triage.toml: {PYTHON_CVE} for {PYTHON_PURL} has decided {LATER}, in the future: date a"
        " decision the day it is made",
    ),
    "malformed entries": (
        _commit(TRIAGE, _malformed),
        f"tools/triage.toml: unknown table waiver\nerror: tools/triage.toml: {PYTHON_CVE} for {PYTHON_PURL}"
        " has justification 'x': use one of OpenVEX's five\nerror: tools/triage.toml:"
        f" {PYTHON_CVE} for {PYTHON_PURL} names an e-mail address in by: name a role\nerror:"
        " tools/triage.toml: [[vendored]] psycopg-binary 3.3.6 has an unknown key reason",
    ),
    "patch migration": (
        _patch,
        "0.1.1 adds deltasleuth/store/migrations/002_more.sql: a patch release adds no migration",
    ),
    "registered": (
        _just(lambda s: s.register.write_text('{"version": "0.1.0", "platform": "linux/amd64"}\n')),
        "0.1.0 for linux/amd64 is in the register already",
    ),
    "existing bundle": (
        _just(lambda s: (s.out / STEM).mkdir()),
        "--out: {out}/" + STEM + " exists: a release is never overwritten",
    ),
    "missing syft": (
        _just(lambda s: (s.bin / "syft").unlink()),
        "missing: syft (needs docker, git, grype, npm, syft and uv)",
    ),
    "base not local": (
        _scenario(images=lambda i: {ref: v for ref, v in i.items() if "37134a49" not in ref}),
        "python@sha256:37134a49d21d… (python:3.13-slim for linux/amd64) is not in the local image store; this"
        f" tool never pulls. To add it: docker pull python@{release.BASES['linux/amd64']}",
    ),
    "base of another platform": (
        _scenario(images=lambda i: i | {release.POSTGRES: i[release.POSTGRES] | {"platform": "linux/amd64"}}),
        "postgres:16-alpine (the smoke test's database for linux/arm64) is linux/amd64 in the local image"
        " store, not linux/arm64",
    ),
    "built for arm64": (
        _scenario(built_platform="linux/arm64"),
        "the built image is linux/arm64, not linux/amd64",
    ),
    "named user": (_scenario(user="deltasleuth"), "the built image runs as deltasleuth, not 10001:10001"),
    "pull during build": (
        _scenario(pull_during_build=True),
        f"the build changed the local image of python:3.13-slim@{release.INDEX[:19]}…: something pulled, and"
        " this tool never pulls",
    ),
    "smoke test": (
        _scenario(init=False),
        "the smoke test failed: /v1/health did not answer 200 within 180 s under the compose hardening",
    ),
    "smoke test: time zones": (
        _scenario(zones=False),
        "the smoke test failed: the image's time zone data does not load Europe/London",
    ),
    "smoke test: duckdb": (
        _scenario(duck=False),
        "the smoke test failed: DuckDB does not load, its guard does not hold, it cannot read and spill, its"
        " process is not replaced when it ends, or its limits do not hold, under the compose hardening",
    ),
    "smoke test: mcp": (
        _scenario(mcp=False),
        "the smoke test failed: the MCP server does not list its tools in both protocol eras under the"
        " compose hardening",
    ),
    "two saved images": (
        _scenario(also_saved=["deltasleuth:other"]),
        "the saved archive holds 2 images; expected deltasleuth:0.1.0 alone",
    ),
    "SBOM lacks a locked package": (
        _scenario(sbom=lambda b: b | {"components": [c for c in b["components"] if "psycopg" not in str(c)]}),
        "the SBOM lacks psycopg-binary 3.3.6 (uv.lock)",
    ),
    "SBOM lacks Debian": (
        _scenario(sbom=lambda b: b | {"components": b["components"][1:]}),
        "the SBOM lacks the Debian distribution",
    ),
    "SBOM lacks a dpkg package": (
        _scenario(dpkg=lambda d: [*d, "libzstd1"]),
        "the SBOM lacks libzstd1 (dpkg)",
    ),
    "SBOM lacks python": (_scenario(python="3.13.16"), "the SBOM lacks python 3.13.16"),
    "vendored without a decision": (
        _scenario(libs=lambda v: [*v, "psycopg-binary 3.3.6\tpsycopg_binary.libs/libpq.so"]),
        "psycopg-binary 3.3.6 ships psycopg_binary.libs/libpq.so with no [[vendored]] decision in"
        " tools/triage.toml",
    ),
    "decision of another version": (
        _commit(TRIAGE, lambda t: t.replace('"3.3.6"', '"3.3.5"')),
        f"psycopg-binary 3.3.6 ships {LIBCRYPTO} with no [[vendored]] decision in tools/triage.toml\nerror:"
        " tools/triage.toml: [[vendored]] psycopg-binary 3.3.5 is not the image's 3.3.6: decide again",
    ),
    "blocking finding": (
        _scenario(matches=lambda m: [*m, match("CVE-2026-20001", "High", "fixed", PSYCOPG, "3.3.7")]),
        "1 finding(s) block the release; upgrade, or triage each in tools/triage.toml:\n"
        f"  CVE-2026-20001 High, fixed in 3.3.7: {PSYCOPG}",
    ),
    "pip-audit skip": (
        _scenario(audit={"dependencies": [{"name": "psycopg-binary", "skip_reason": "not on PyPI"}]}),
        "pip-audit could not audit 1 dependency: psycopg-binary 3.3.6 (not on PyPI)",
    ),
    "VEX ignored": (
        _scenario(vex_suppresses=False),
        f"the VEX did not suppress {PYTHON_CVE} for {PYTHON_PURL}",
    ),
    "failing ruff": (
        _scenario(checks={"ruff": 1}),
        "ruff said this\nerror: checks failed: uv run --frozen ruff check deltasleuth mocks tests examples"
        " tools (exit 1)",
    ),
    "failing web tests": (
        _scenario(checks={"npm test": 3}),
        "npm test said this\nerror: checks failed: npm --prefix web test (exit 3)",
    ),
    "commit during build": (
        _scenario(commit_during_checks=True),
        "the tree changed during the build (HEAD {head}…, 0 path changed): nothing was released",
    ),
}


@pytest.mark.parametrize("refusal", REFUSALS)
def test_every_refusal_leaves_nothing(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    setup, expected = REFUSALS[refusal]
    scratch.out.mkdir()
    options = setup(scratch, monkeypatch) or {}
    before = (
        sorted(scratch.out.iterdir()),
        scratch.register.exists() and scratch.register.read_text(),
        scratch.git("tag"),
    )
    done = scratch.build(**options)
    dockerfile = (scratch.root / "deploy" / "Dockerfile").read_text().splitlines()
    lines = {start: next(n for n, line in enumerate(dockerfile, 1) if line.startswith(start))
             for start in ("FROM --platform", "ARG PYTHON")}  # fmt: skip
    expected = expected.format(
        fp=scratch.fingerprint[:12], out=scratch.out, head=scratch.git("rev-parse", "HEAD")[:12],
        notes=scratch.notes,
        **{"from": f"deploy/Dockerfile line {lines['FROM --platform']}", "arg": f"deploy/Dockerfile line"
           f" {lines['ARG PYTHON']}"},
    )  # fmt: skip
    message = expected if "said this\n" in expected else f"error: {expected}"  # a failed check's output first
    assert (done.exit_code, done.output) == (2, f"{message}\n")
    after = (
        sorted(scratch.out.iterdir()),
        scratch.register.exists() and scratch.register.read_text(),
        scratch.git("tag"),
    )
    assert after == before
    state = scratch.state()
    assert (state["containers"], state["networks"], state["volumes"]) == ([], [], [])
    assert [ref for ref in state["images"] if ref != scratch.scenario["index"]] == []  # nor the built image


IMAGE = "deltasleuth:0.1.0"  # the release tag, which the scratch build gives its image
ZONES = "error: the smoke test failed: the image's time zone data does not load Europe/London\n"


def _images(scratch: Scratch) -> dict[str, str]:
    """What the docker stub's store gained: each tag, or an untagged image's id, and the id it names."""
    return {ref: image["id"] for ref, image in scratch.state()["images"].items()}


def _hold(scratch: Scratch, ref: str, digit: str) -> str:
    """Put an image this build did not make in the stub's store under ``ref``; its id."""
    image = {"platform": "linux/arm64", "user": "10001:10001", "id": "sha256:" + digit * 64}
    state = scratch.state()
    (scratch.tmp / "state.json").write_text(json.dumps(state | {"images": state["images"] | {ref: image}}))
    return image["id"]


def _retagged(scratch: Scratch) -> list[list[str]]:
    """Each docker run that removes, prunes or tags an image."""
    docker, image = [run["argv"] for run in scratch.runs("docker")], (["image", "rm"], ["image", "prune"])
    return [argv for argv in docker if argv[0] in ("rmi", "tag") or argv[:2] in image]


def test_a_build_refused_at_the_gate_leaves_no_image(scratch: Scratch) -> None:
    """A refusal after docker build removes the image that build made (§9), by its tag alone and unforced, so
    nothing stays under the release tag and the next attempt has no image to leave untagged."""
    scratch.scenario["matches"].append(match("CVE-2026-20001", "High", "fixed", PSYCOPG, "3.3.7"))
    for attempt in (1, 2):
        done = scratch.build()
        assert done.exit_code == 2 and done.output.startswith("error: 1 finding(s) block the release;")
        assert _images(scratch) == {}
        assert _retagged(scratch) == [["image", "rm", IMAGE]] * attempt


def test_a_released_build_keeps_its_image(scratch: Scratch) -> None:
    done = scratch.build()
    assert done.exit_code == 0, done.output
    assert _images(scratch) == {IMAGE: _release(scratch.bundle())["config"]}
    assert _retagged(scratch) == []


def test_a_refused_build_gives_the_tag_back(scratch: Scratch) -> None:
    """An image that held the release tag before the build is not this build's: a refusal removes what the
    build made and tags that image again, so the store is as it was."""
    held = _hold(scratch, IMAGE, "a")
    scratch.scenario["zones"] = False
    done = scratch.build()
    assert (done.exit_code, done.output) == (2, ZONES)
    assert _images(scratch) == {IMAGE: held}
    assert _retagged(scratch) == [["image", "rm", IMAGE], ["tag", held, IMAGE]]


def test_a_refused_build_removes_only_the_image_it_built(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tag is removed only while it names the image this run built: one that took the tag meanwhile is not
    this build's, so it stays and the refusal says that nothing was removed."""
    made: list[str] = []

    def smoke(image: str, platform: str, staging: Path) -> None:  # another image takes the tag, then it fails
        made.append(_images(scratch)[image])
        _hold(scratch, image, "b")
        release.fail("the smoke test failed")

    monkeypatch.setattr(release, "_smoke", smoke)
    done = scratch.build()
    assert (done.exit_code, done.output) == (
        2,
        f"error: the smoke test failed\nwarning: {IMAGE} no longer names the image this build made,"
        f" {made[0][:19]}…: nothing was removed\n",
    )
    assert _images(scratch) == {IMAGE: "sha256:" + "b" * 64} and _retagged(scratch) == []


def test_a_refused_build_never_forces_its_image_out(scratch: Scratch) -> None:
    """Docker keeps an image a container uses: the refusal says that it is left, and forces nothing. The next
    attempt finds the tag on the very image it builds, cannot tell it from one loaded before and leaves it."""
    scratch.scenario |= {"zones": False, "in_use": True}
    done = scratch.build()
    assert (done.exit_code, done.output) == (
        2,
        f"{ZONES}warning: {IMAGE} is still in the local image store (docker image rm: Error response from"
        f' daemon: conflict: unable to remove repository reference "{IMAGE}" (must force) - container'
        " 0f1e2d3c4b5a is using its referenced image)\n",
    )
    left = _images(scratch)
    assert list(left) == [IMAGE]
    scratch.scenario["in_use"] = False
    again = scratch.build()
    assert (again.exit_code, again.output, _images(scratch)) == (2, ZONES, left)
    assert _retagged(scratch) == [["image", "rm", IMAGE]]


def test_an_affected_finding_ships_with_its_action(scratch: Scratch) -> None:
    """A finding decided as affected has no justification (§8): the release ships it with its dated action,
    in the summary and the VEX, and suppresses nothing."""
    soon, action = TODAY + timedelta(days=20), "Reachable through the health check; fixed by 0.1.1."
    decided = (scratch.root / TRIAGE).read_text()
    for old, new in (
        ('"not_affected"\njustification = "vulnerable_code_not_in_execute_path"', '"affected"'),
        ("DeltaSleuth never calls the affected function.", action),
        (f"expires = {TODAY + timedelta(days=80)}", f"expires = {soon}"),
    ):
        assert old in decided
        decided = decided.replace(old, new)
    scratch.commit({TRIAGE: decided})
    done = scratch.build()
    assert done.exit_code == 0, done.output
    bundle = scratch.bundle()
    assert _release(bundle)["gate"].startswith("passed; 0 blocking, 1 triaged, 2 reported,")
    line = f"  {PYTHON_CVE} {PYTHON_PURL}: affected until {soon}, decided {TODAY} by maintainer: {action}\n"
    assert line in (bundle / "scan" / "SUMMARY.txt").read_text()
    [statement] = json.loads((bundle / "scan" / "vex.openvex.json").read_text())["statements"]
    assert (statement["status"], statement["action_statement"]) == ("affected", action)
    assert not {"justification", "impact_statement"} & set(statement)


def test_a_failed_tag_keeps_the_signed_release(scratch: Scratch) -> None:
    revision = scratch.git("rev-parse", "HEAD")
    (scratch.root / ".git" / "refs" / "tags" / "v0.1.0.lock").write_text("")  # another git holds the tag
    done = scratch.build()
    assert (done.exit_code, done.output) == (2, (
        f"error: the release is at {scratch.bundle()} but git tag failed: run git tag -a --cleanup=verbatim"
        f" v0.1.0 {revision} -F {scratch.notes}\n"
    ))  # fmt: skip
    assert (scratch.bundle() / "SHA256SUMS.sig").is_file() and (scratch.out / f"{STEM}.tar").is_file()
    assert len(scratch.register.read_text().splitlines()) == 1
    assert list(_images(scratch)) == [IMAGE]  # the release stands, and so does its image


@pytest.mark.parametrize("failure", ["self-check", "full disk"])
def test_a_failure_after_the_register_names_its_line(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A refusal after the register append (§9 step 9) leaves nothing in DIR, no partial transport tar
    included, and names the register line to strike."""
    if failure == "self-check":
        monkeypatch.setattr(signing, "verifies", lambda *args: False)
        what = "the bundle failed its self-check"
    else:

        def full(folder: Path, target: Path, mtime: int) -> None:
            target.write_bytes(b"the first blocks")
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        monkeypatch.setattr(release, "_transport", full)
        what = f"the bundle could not be written ([Errno {errno.ENOSPC}] {os.strerror(errno.ENOSPC)})"
    scratch.out.mkdir()
    done = scratch.build()
    [entry] = [json.loads(line) for line in scratch.register.read_text().splitlines()]
    assert (done.exit_code, done.output) == (2, (
        f"error: {what}: nothing was released; strike any line of sums_sha256 {entry['sums_sha256']} from"
        f" {scratch.register}\n"
    ))  # fmt: skip
    assert (list(scratch.out.iterdir()), scratch.git("tag"), _images(scratch)) == ([], "", {})


def test_one_version_for_each_platform(scratch: Scratch) -> None:
    """One bundle per platform (D2): the second platform's build of the tagged revision keeps the tag, and the
    register refuses a platform twice."""
    assert scratch.build().exit_code == 0
    tag = scratch.git("rev-parse", "v0.1.0")
    arm64 = scratch.build(platform="linux/arm64")
    assert arm64.exit_code == 0, arm64.output
    assert _release(scratch.out / "deltasleuth-0.1.0-linux-arm64")["platform"] == "linux/arm64"
    assert scratch.git("rev-parse", "v0.1.0") == tag  # not made again
    registered = [json.loads(line)["platform"] for line in scratch.register.read_text().splitlines()]
    assert registered == ["linux/amd64", "linux/arm64"]
    again = scratch.build(platform="linux/arm64")
    assert (again.exit_code, again.output) == (2, "error: 0.1.0 for linux/arm64 is in the register already\n")
    elsewhere = scratch.tmp / "elsewhere.jsonl"  # the tag on HEAD, but a register that lists no bundle of it
    lost = scratch.build(out=str(scratch.tmp / "lost"), register=str(elsewhere))
    assert (lost.exit_code, lost.output) == (
        2, f"error: tag v0.1.0 is on HEAD but {elsewhere} lists no bundle of it: pass the register that"
        " does\n",
    )  # fmt: skip


def test_the_smoke_config_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ds serve loads its settings and targets as it starts (§9 step 5): the real loaders take the smoke
    test's config, loopback target included."""
    staging, loaded = release._staging(tmp_path / "staging"), []

    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not loaded:  # the config is written before the first docker run
            settings = load_settings(staging / "smoke" / "deltasleuth.yaml")
            loaded.append(load_targets(settings.targets_file))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(release, "_run", run)
    release._smoke("deltasleuth:0.1.0", "linux/amd64", staging)
    assert list(loaded[0]) == ["smoke"]


@pytest.mark.parametrize(
    ("host", "platform", "limits"),
    [
        ("arm64", "linux/arm64", True),  # macOS names aarch64 so: one machine
        ("aarch64", "linux/arm64", True),
        ("x86_64", "linux/amd64", True),
        ("arm64", "linux/amd64", False),
        ("aarch64", "linux/amd64", False),
        ("x86_64", "linux/arm64", False),
    ],
)
def test_the_self_test_tries_its_limits_natively_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, platform: str, limits: bool
) -> None:
    """docs/mvp/READER.md §11: an image of another machine than the build host's is smoke-tested under
    emulation, where a data limit would count the emulator and the socket filter judge it: its self-test
    runs without its limits."""
    ran: list[list[str]] = []

    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(list(argv))
        return subprocess.CompletedProcess(ran[-1], 0, "", "")

    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(machine=host))
    monkeypatch.setattr(release, "_run", run)
    release._smoke("deltasleuth:0.1.0", platform, release._staging(tmp_path / "staging"))
    duck = "from deltasleuth.connectors.tables import duck; duck.selftest({})"
    assert [argv[-1] for argv in ran if "duck" in argv[-1]] == [duck.format("" if limits else "limits=False")]
    assert release.DUCK == duck.format("")


def test_no_pull_push_login_or_secret_in_argv(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DS_UNRELATED", "kept")  # the maintainer's environment reaches docker, never a scanner
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(machine="x86_64"))  # the built platform's
    assert scratch.build().exit_code == 0
    runs = scratch.runs()
    docker = [run["argv"] for run in runs if run["tool"] == "docker"]
    used = {"version", "image", "build", "network", "run", "exec", "rm", "volume", "save"}
    assert {argv[0] for argv in docker} == used
    assert all(argv[argv.index("--pull") + 1] == "never" for argv in docker if argv[0] == "run")
    # The smoke test asks the built image for a cron's next time in a zone its own tz data must hold.
    zoned = ["ds", "schedules", "next", "0 2 * * *", "--tz", "Europe/London", "--count", "1"]
    assert [argv[argv.index("deltasleuth:0.1.0") :] for argv in docker if "schedules" in argv] == [
        ["deltasleuth:0.1.0", *zoned]
    ]
    assert ["--rm" in argv and "--read-only" in argv for argv in docker if "schedules" in argv] == [True]
    # The files reader's self-test, under the hardening, with the evidence volume it spills in (FILES.md §3).
    selftest = [argv for argv in docker if release.DUCK in argv]
    duck = "from deltasleuth.connectors.tables import duck; duck.selftest()"
    assert [argv[argv.index("deltasleuth:0.1.0") :] for argv in selftest] == [
        ["deltasleuth:0.1.0", "python", "-c", duck]
    ]
    spilled = "/var/lib/deltasleuth/evidence"
    assert ["--read-only" in argv and spilled in str(argv) for argv in selftest] == [True]
    # The MCP server's self-test (docs/mvp/MCP.md §3), under the hardening too: it reads no store.
    listed = [argv for argv in docker if release.MCP in argv]
    assert [argv[argv.index("deltasleuth:0.1.0") :] for argv in listed] == [
        ["deltasleuth:0.1.0", "python", "-c", "from deltasleuth.mcp import server; server.selftest()"]
    ]
    assert ["--rm" in argv and "--read-only" in argv for argv in listed] == [True]
    assert [
        argv for argv in docker if argv[0] == "build" and any(part.startswith("--pull") for part in argv)
    ] == []
    assert all("DS_UNRELATED" in run["env"] for run in runs if run["tool"] == "docker")
    assert not [
        run for run in runs if run["tool"] in ("syft", "grype", "python") and "DS_UNRELATED" in run["env"]
    ]
    secrets = [PASSPHRASE, scratch.key.read_text().splitlines()[1]]
    for run in runs:
        assert not [s for s in secrets for part in [*run["argv"], *run["env"].values()] if s in part], run[
            "argv"
        ]


def test_scanners_run_isolated(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    hostile = "only-notfixed: true\nexclude: ['**']\nignore: [{vulnerability: 'CVE-2026-82049'}]\n"
    for path in (scratch.tmp / "home" / ".grype.yaml", scratch.tmp / "home" / ".syft.yaml",
                 scratch.tmp / "cwd" / ".grype.yaml"):  # fmt: skip
        path.parent.mkdir(exist_ok=True)
        path.write_text(hostile)
    monkeypatch.chdir(scratch.tmp / "cwd")
    monkeypatch.setenv("GRYPE_ONLY_NOTFIXED", "true")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.bank.test:3128")
    monkeypatch.setenv("NO_PROXY", "localhost")
    assert scratch.build().exit_code == 0
    scanners = [run for run in scratch.runs() if run["tool"] in ("syft", "grype", "python")]
    assert [(run["tool"], run["argv"][0]) for run in scanners] == [
        ("grype", "version"), ("syft", "version"), ("syft", "scan"), ("grype", "db"), ("grype", "db"),
        ("grype", f"sbom:bundle/sbom/{STEM}.cdx.json"), ("python", "-m"),
        ("grype", f"sbom:bundle/sbom/{STEM}.cdx.json"), ("grype", f"sbom:bundle/sbom/{STEM}.spdx.json"),
    ]  # fmt: skip
    for run in scanners:
        staging = Path(run["cwd"])
        assert staging.parent == scratch.out.resolve() and staging.name.startswith(".deltasleuth-release-")
        network = run["argv"][:2] == ["db", "update"] or run["tool"] == "python"
        assert set(run["env"]) == SCANNING | ({"HTTPS_PROXY", "NO_PROXY"} if network else set()), run["argv"]
        assert {name: value for name, value in run["env"].items() if name in SCANNING} == {
            "PATH": f"{scratch.bin.resolve()}:/usr/bin:/bin", "HOME": f"{staging}/home", "TZ": "UTC",
            "GRYPE_DB_CACHE_DIR": str(release.GRYPE_DB), "GRYPE_DB_AUTO_UPDATE": "false",
            "GRYPE_CHECK_FOR_APP_UPDATE": "false", "SYFT_CHECK_FOR_APP_UPDATE": "false",
        }  # fmt: skip
        if network:
            assert (run["env"]["HTTPS_PROXY"], run["env"]["NO_PROXY"]) == (
                "http://proxy.bank.test:3128",
                "localhost",
            )
        else:
            assert run["argv"][-2:] == ["-c", f"{staging}/empty.yaml"]


def test_no_host_path_in_the_bundle(scratch: Scratch) -> None:
    assert scratch.build().exit_code == 0
    texts = []
    for path in (path for path in scratch.bundle().rglob("*") if path.is_file()):
        if path.suffix == ".gz":
            with _members(path) as tar:
                texts += [tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()]  # type: ignore[union-attr]
        else:
            texts.append(path.read_bytes())
    needles = [
        str(scratch.tmp),
        str(scratch.tmp.resolve()),
        "/Users/",
        "/private/",
        "/var/folders/",
        "/home/",
    ]
    assert [needle for needle in needles for text in texts if needle.encode() in text] == []


def test_packaging_is_deterministic(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    """For one image, the gzip, git archive, the sums, the signature and the transport tar are the same."""
    now = release._now().replace(microsecond=0)
    monkeypatch.setattr(release, "_now", lambda: now)
    assert scratch.build().exit_code == 0
    scratch.git("tag", "-d", "v0.1.0")  # a released version is never rebuilt: undone here, to compare
    again = scratch.build(out=str(scratch.tmp / "again"), register=str(scratch.tmp / "again.jsonl"))
    assert again.exit_code == 0, again.output

    def files(folder: Path) -> dict[str, bytes]:
        return {
            path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob("*")
            if path.is_file()
        }

    assert files(scratch.bundle()) == files(scratch.tmp / "again" / STEM)
    assert (scratch.out / f"{STEM}.tar").read_bytes() == (scratch.tmp / "again" / f"{STEM}.tar").read_bytes()


def _entry(vid: str, purl: str, *, status: str = "not_affected", days: int = 80) -> dict[str, Any]:
    entry = {"id": vid, "component": purl, "status": status, "statement": "Checked.", "decided": TODAY,
             "expires": TODAY + timedelta(days=days), "by": "maintainer"}  # fmt: skip
    return entry | ({"justification": "component_not_present"} if status == "not_affected" else {})


def test_gate() -> None:
    purl = "pkg:pypi/x@1.2.3"
    matches = [
        match("CVE-1", "Critical", "fixed", purl, "1.2.4"), match("CVE-2", "High", "fixed", purl, "1.3"),
        match("CVE-3", "Unknown", "fixed", purl, "1.4"),
        match("CVE-4", "Medium", "fixed", purl, "1.5", kev=True),
        match("CVE-5", "Medium", "fixed", purl, "1.6"), match("CVE-6", "High", "not-fixed", purl),
        match("CVE-7", "High", "wont-fix", "pkg:deb/debian/perl@5.40?arch=amd64"),
        match("CVE-8", "High", "", purl),
        match("GHSA-9", "High", "fixed", f"{purl}?package-id=1", "1.7", related=("CVE-9",)),
        match("CVE-10", "Critical", "fixed", "pkg:pypi/x@1.2.2", "1.2.4"),
    ]  # fmt: skip
    audit = {"dependencies": [{"name": "Y_Lib", "version": "2.0", "vulns": [
        {"id": "PYSEC-1", "fix_versions": ["2.1"], "aliases": ["GHSA-y"]},
        {"id": "PYSEC-2", "fix_versions": [], "aliases": []},
    ]}]}  # fmt: skip
    none = release.gate(matches, audit, {}, TODAY)
    assert [f.id for f in none.blocking] == [
        "CVE-1",
        "CVE-2",
        "CVE-3",
        "CVE-4",
        "GHSA-9",
        "CVE-10",
        "PYSEC-1",
    ]
    assert [f.id for f in none.reported] == ["CVE-5", "CVE-6", "CVE-7", "CVE-8", "PYSEC-2"]
    assert none.blocking[3].line() == "  CVE-4 Medium, known exploited, fixed in 1.5: pkg:pypi/x@1.2.3"
    assert none.blocking[-1].line() == "  PYSEC-1 (pip-audit), fixed in 2.1: pkg:pypi/y-lib@2.0"
    entries = {"finding": [
        _entry("CVE-1", purl),  # by its id
        _entry("CVE-9", purl, status="affected", days=30),  # by a related id, the purl without qualifiers
        _entry("GHSA-y", "pkg:pypi/y-lib@2.0"),  # by a pip-audit alias
        _entry("CVE-10", purl),  # another version: not this finding
        _entry("CVE-2", purl, days=-1) | {"decided": TODAY - timedelta(days=10)},  # expired: not applied
        _entry("CVE-3", purl) | {"decided": LATER},  # decided in the future: not applied either
        _entry("CVE-4", purl) | {"decided": TOMORROW},  # tomorrow in UTC may be today where the maintainer is
        _entry("CVE-404", purl),  # matches nothing
    ]}  # fmt: skip
    decided = release.gate(matches, audit, entries, TODAY)
    assert [(f.id, e["id"]) for f, e in decided.triaged] == [
        ("CVE-1", "CVE-1"), ("CVE-4", "CVE-4"), ("GHSA-9", "CVE-9"), ("PYSEC-1", "GHSA-y"),
    ]  # fmt: skip
    assert [f.id for f in decided.blocking] == ["CVE-2", "CVE-3", "CVE-10"]
    unused = "tools/triage.toml: {} for pkg:pypi/x@1.2.3 matches no finding: remove it"
    assert decided.unused == [unused.format("CVE-10"), unused.format("CVE-404")]
    assert decided.line() == "failed; 3 blocking, 4 triaged, 5 reported, 0 vendored file(s) decided"
    libpq = "psycopg_binary.libs/libpq.so"
    vendored = [("psycopg-binary", "3.3.6", LIBCRYPTO), ("psycopg-binary", "3.3.6", libpq),
                ("pillow", "12.3.0", "pillow.libs/libjpeg.so")]  # fmt: skip
    decision = {"distribution": "psycopg-binary", "version": "3.3.6", "files": [LIBCRYPTO],
                "status": "affected", "statement": "Moving.", "decided": TODAY,
                "expires": TODAY + timedelta(days=30), "by": "maintainer"}  # fmt: skip
    libraries = {"vendored": [
        decision, decision | {"files": ["psycopg_binary.libs/libpq.so"], "expires": YESTERDAY},  # expired
        decision | {"distribution": "pillow", "version": "12.2.0", "files": ["pillow.libs/libjpeg.so"]},
        decision | {"distribution": "numpy", "version": "2.5.3"},  # not in the image
        decision | {"version": "3.3.5"},  # another line's, while the image's 3.3.6 has one: it stands
    ]}  # fmt: skip
    checked = release.gate([], {}, libraries, TODAY, vendored)
    assert checked.decided == [(vendored[0], decision)]
    assert checked.refused == [
        f"psycopg-binary 3.3.6 ships {libpq} with no [[vendored]] decision in tools/triage.toml",
        "pillow 12.3.0 ships pillow.libs/libjpeg.so with no [[vendored]] decision in tools/triage.toml",
        "tools/triage.toml: [[vendored]] pillow 12.2.0 is not the image's 12.3.0: decide again",
    ]  # fmt: skip
    assert checked.unused == [
        "tools/triage.toml: [[vendored]] numpy 2.5.3 matches no vendored file: remove it"
    ]
    assert checked.line() == "failed; 0 blocking, 0 triaged, 0 reported, 1 vendored file(s) decided"
    ghsa, crypto = "GHSA-h4gh-qq45-vh27", "pkg:pypi/cryptography@43.0.0"  # PyPI gives the GHSA id, as grype
    vulns = [{"id": ghsa, "fix_versions": ["43.0.1"], "aliases": []}]
    audited = {"dependencies": [{"name": "cryptography", "version": "43.0.0", "vulns": vulns}]}
    both = release.gate([match(ghsa, "Medium", "fixed", crypto, "43.0.1")], audited, {}, TODAY)
    assert [(f.id, f.source) for f in both.blocking] == [(ghsa, "pip-audit")]  # not hidden by grype's Medium
    assert [(f.id, f.source) for f in both.reported] == [(ghsa, "grype")]
    together = release.gate([match(ghsa, "Medium", "fixed", crypto, "43.0.1")], audited,
                            {"finding": [_entry(ghsa, crypto)]}, TODAY)  # fmt: skip
    assert (together.blocking, [f.source for f, _ in together.triaged]) == ([], ["grype", "pip-audit"])


def test_triage_refuses_malformed_entries() -> None:
    assert release.triage(releases.triage(datetime.now(UTC)), TODAY)[1] == []
    long = f"""[[finding]]
id = "CVE-1"
component = "pkg:pypi/x@1.2.3"
status = "not_affected"
justification = "component_not_present"
statement = "Checked."
decided = {TODAY}
expires = {TODAY + timedelta(days=91)}
by = "maintainer"

[[vendored]]
distribution = "pillow"
version = "12.3.0"
files = ["pillow.libs/libjpeg.so"]
status = "fixed"
statement = "Fixed."
decided = {TODAY}
expires = {TODAY}
by = "maintainer"
"""
    assert release.triage(long)[1] == [
        "tools/triage.toml: CVE-1 for pkg:pypi/x@1.2.3 expires 91 days after decided: at most 90"
        " (affected: 30)",
        "tools/triage.toml: [[vendored]] pillow 12.3.0 has status 'fixed': use not_affected or affected",
    ]  # fmt: skip
    lacking = "[[finding]]\nid = 'CVE-1'\nstatus = 'affected'\njustification = 'x'\ndecided = 1\n"
    assert release.triage(lacking)[1] == [f"tools/triage.toml: CVE-1 for None {problem}" for problem in (
        "lacks component", "lacks statement", "lacks expires", "lacks by",
        "needs a justification if not_affected, and only then",
        "needs decided and expires as dates (YYYY-MM-DD)",
    )]  # fmt: skip
    assert release.triage("[finding\n")[1][0].startswith("tools/triage.toml: ")
    future = f"has decided {LATER}, in the future: date a decision the day it is made"
    assert release.triage(releases.triage(datetime.now(UTC) + timedelta(days=1)), TODAY)[1] == []  # local day
    assert release.triage(releases.triage(datetime.now(UTC) + timedelta(days=2)), TODAY)[1] == [
        f"tools/triage.toml: {PYTHON_CVE} for {PYTHON_PURL} {future}",
        f"tools/triage.toml: [[vendored]] psycopg-binary 3.3.6 {future}",
    ]
    assert release.triage('[finding]\nid = "CVE-1"\n')[1] == [
        "tools/triage.toml: finding must be [[finding]] entries"
    ]
    typed = long[long.index("[[vendored]]") :].replace('"fixed"', '"affected"').replace('"12.3.0"', "12.3")
    typed = typed.replace('["pillow.libs/libjpeg.so"]', '"pillow.libs/libjpeg.so"')  # a string, not a list
    assert release.triage(typed)[1] == [
        "tools/triage.toml: [[vendored]] pillow 12.3 needs version as a string",
        "tools/triage.toml: [[vendored]] pillow 12.3 needs files as a list of strings",
    ]


def test_pins_reads_every_base(capsys: pytest.CaptureFixture[str]) -> None:
    """Every base must be pinned, local and id-checked: each FROM, in any case, and each COPY --from and RUN
    --mount from= that names no earlier stage, by name or index (§9 step 2)."""
    index = f"python:3.13-slim@{release.INDEX}"
    node, uv = f"node:24-alpine@sha256:{'2' * 64}", f"ghcr.io/astral-sh/uv:0.11.21@sha256:{'1' * 64}"
    dockerfile = (f"ARG PYTHON={index}\nfrom --platform=$BUILDPLATFORM {node} as Web\nFROM ${{PYTHON}}\n"
                  f"COPY --from=web /src/web/dist web/dist/\nCOPY --from=0 /x /y\n"
                  f"copy --from={uv} /uv /bin/\n"
                  f"RUN --mount=type=bind,from={uv},target=/u true\n")  # fmt: skip
    assert release._pins(dockerfile) == (index, [node, uv])
    unpinned = {
        "from node:24-alpine AS web": "FROM node:24-alpine",
        "COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/": "COPY --from=ghcr.io/astral-sh/uv:latest",
        "run --mount=type=cache,target=/c,from=alpine:3 true": "RUN --mount from=alpine:3",
    }
    for line, what in unpinned.items():
        with pytest.raises(typer.Exit):
            release._pins(f"ARG PYTHON={index}\nFROM ${{PYTHON}} AS base\n{line}\n")
        assert capsys.readouterr().err == f"error: deploy/Dockerfile line 3: {what} {PIN}\n"


def test_sbom_completeness() -> None:
    sbom = releases.cyclonedx("0.1.0")
    given: dict[str, Any] = {"dpkg": releases.DPKG, "python": "3.13.15", "exported": releases.EXPORTED,
                             "version": "0.1.0", "lock": releases.LOCK, "machine": "x86_64"}  # fmt: skip
    assert (
        release.lacks(sbom, **given) == []
    )  # colorama is win32's; vite is a dev package; libstdc++6 decoded
    amd64 = releases.EXPORTED + "x==1.0 ; platform_machine == 'x86_64'\n"
    assert release.lacks(sbom | {"components": sbom["components"][1:]}, **given) == [
        "the SBOM lacks the Debian distribution"
    ]
    assert release.lacks(sbom, **given | {"dpkg": ["libc6", "libzstd1"]}) == [
        "the SBOM lacks libzstd1 (dpkg)", "the SBOM lists Debian package libstdc++6, which dpkg does not",
        "the SBOM lists Debian package openssl, which dpkg does not",
    ]  # fmt: skip
    assert release.lacks(sbom, **given | {"python": "3.13.16"}) == ["the SBOM lacks python 3.13.16"]
    assert release.lacks(sbom, **given | {"exported": amd64}) == ["the SBOM lacks x 1.0 (uv.lock)"]
    assert release.lacks(sbom, **given | {"exported": amd64, "machine": "aarch64"}) == []
    assert release.lacks(sbom, **given | {"version": "0.1.1"}) == [
        "the SBOM lacks deltasleuth 0.1.1 (pyproject.toml)"
    ]
    without = [
        component for component in sbom["components"] if component.get("purl") != "pkg:npm/react@19.3.0"
    ]
    assert release.lacks(sbom | {"components": without}, **given) == [
        "the SBOM lacks react 19.3.0 (web/package-lock.json)"
    ]


def test_the_mcp_library_is_pinned_and_brings_no_telemetry_backend(repo_root: Path) -> None:
    """docs/mvp/MCP.md §3: the exact pin; the four packages it adds to the image, each one pure-Python
    wheel, which the SBOM must hold, and ``pywin32`` for Windows alone, which it need not; no OpenTelemetry
    SDK, exporter or instrumentation in the lock; and the entry points that the API's ``OTEL_PYTHON_CONTEXT``,
    ``OTEL_PYTHON_TRACER_PROVIDER`` and ``OTEL_PROPAGATORS`` select are the API's own."""
    added = {"mcp": "2.2.0", "mcp-types": "2.2.0", "opentelemetry-api": "1.44.0", "sse-starlette": "3.4.11"}
    assert (
        "mcp==2.2.0" in tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["dependencies"]
    )
    packages = {
        package["name"]: package for package in tomllib.loads((repo_root / "uv.lock").read_text())["package"]
    }
    assert {name: packages[name]["version"] for name in added} == added
    assert all(
        [wheel["url"].rsplit("-", 3)[1:] for wheel in packages[name]["wheels"]]
        == [["py3", "none", "any.whl"]]
        for name in added
    )
    assert {"name": "pywin32", "marker": "sys_platform == 'win32'"} in packages["mcp"]["dependencies"]
    assert [name for name in packages if "telemetry" in name or "otel" in name] == ["opentelemetry-api"]
    exported = "".join(f"{name}=={pinned}\n" for name, pinned in added.items())
    exported += f"pywin32=={packages['pywin32']['version']} ; sys_platform == 'win32'\n"
    given: dict[str, Any] = {"dpkg": releases.DPKG, "python": "3.13.15", "version": "0.1.0",
                             "lock": releases.LOCK, "machine": "x86_64"}  # fmt: skip
    assert release.lacks(releases.cyclonedx("0.1.0"), exported=releases.EXPORTED + exported, **given) == [
        f"the SBOM lacks {name} {pinned} (uv.lock)" for name, pinned in added.items()
    ]
    for group in ("opentelemetry_context", "opentelemetry_tracer_provider", "opentelemetry_propagator"):
        assert {found.dist.name for found in entry_points(group=group)} == {"opentelemetry-api"}, group  # type: ignore[union-attr]


def test_rescan_applies_todays_triage(scratch: Scratch) -> None:
    assert scratch.build().exit_code == 0
    bundle = scratch.bundle().resolve()
    later = match("CVE-2026-20001", "High", "fixed", "pkg:pypi/psycopg-binary@3.3.6", "3.3.7")
    scratch.scenario["matches"].append(later)  # today's database knows more
    (scratch.tmp / "scenario.json").write_text(json.dumps(scratch.scenario))

    def rescan() -> Result:
        return CliRunner().invoke(release.app, ["rescan", str(bundle)])

    blocked = rescan()
    assert (blocked.exit_code, blocked.output) == (2, (
        f"{STEM}: failed; 1 blocking, 1 triaged, 2 reported, 1 vendored file(s) decided\n"
        f"error: {STEM}: 1 finding(s) would block the release; upgrade, or triage each in"
        " tools/triage.toml:\n"
        "  CVE-2026-20001 High, fixed in 3.3.7: pkg:pypi/psycopg-binary@3.3.6\n"
    ))  # fmt: skip
    triage = scratch.root / "tools" / "triage.toml"
    triage.write_text(
        triage.read_text()
        + f"""
[[finding]]
id = "CVE-2026-20001"
component = "pkg:pypi/psycopg-binary@3.3.6"
status = "affected"
statement = "psycopg-binary 3.3.7 ships in 0.1.1 by {TODAY + timedelta(days=14)}."
decided = {TODAY}
expires = {TODAY + timedelta(days=30)}
by = "maintainer"
"""
    )
    decided = rescan()
    assert (decided.exit_code, decided.output) == (
        0, f"{STEM}: passed; 0 blocking, 2 triaged, 2 reported, 1 vendored file(s) decided\n"
    )  # fmt: skip
    triage.write_text(_expire(80)(triage.read_text()))
    expired = rescan()  # an expired decision no longer applies: its finding blocks again
    assert (
        expired.exit_code == 2
        and f"\n  {PYTHON_CVE} High, fixed in 3.14.0b1: {PYTHON_PURL}\n" in expired.output
    )
    grype = [run for run in scratch.runs() if run["tool"] == "grype"][-3:]
    assert [run["argv"][:2] for run in grype] == [["version", "-o"], ["db", "update"],
                                                  [f"sbom:{bundle}/sbom/{STEM}.cdx.json", "-o"]]  # fmt: skip
    assert [set(run["env"]) for run in grype] == [SCANNING, SCANNING, SCANNING]


def test_rescan_passes_two_lines_that_vendor_other_versions(scratch: Scratch) -> None:
    """Two supported lines (SECURITY.md §1) whose wheel vendors another version: with a decision for each, one
    rescan passes both, and the newer build keeps the older line's decision (§8)."""
    assert scratch.build().exit_code == 0
    triage = (scratch.root / "tools" / "triage.toml").read_text()
    newer = triage[triage.index("[[vendored]]") :].replace('"3.3.6"', '"3.3.7"')
    compose = "services:\n  app: {image: 'deltasleuth:%s'}\n  mocks: {image: 'deltasleuth:%s'}\n"
    scratch.commit({
        "pyproject.toml": (scratch.root / "pyproject.toml").read_text().replace('"0.1.0"', '"0.1.1"'),
        "deploy/docker-compose.yml": compose % ("0.1.1", "0.1.1"),
        "deploy/docker-compose.ca.yml": compose % ("0.1.1-ca", "0.1.1-ca"),
        "tools/triage.toml": f"{triage}\n{newer}",
    })  # fmt: skip
    sbom = json.dumps(releases.cyclonedx("0.1.1")).replace(f"{PSYCOPG}", "pkg:pypi/psycopg-binary@3.3.7")
    scratch.scenario |= {"libs": [f"psycopg-binary 3.3.7\t{LIBCRYPTO}"], "sbom": json.loads(sbom),
                         "exported": releases.EXPORTED.replace("3.3.6", "3.3.7")}  # fmt: skip
    built = scratch.build()
    assert built.exit_code == 0, built.output
    bundles = [str(scratch.bundle(version).resolve()) for version in ("0.1.0", "0.1.1")]
    done = CliRunner().invoke(release.app, ["rescan", *bundles])
    passed = "passed; 0 blocking, 1 triaged, 2 reported, 1 vendored file(s) decided"
    assert (done.exit_code, done.output) == (0, "".join(
        f"deltasleuth-{version}-linux-amd64: {passed}\n" for version in ("0.1.0", "0.1.1")
    ))  # fmt: skip


def _transition(scratch: Scratch) -> str:
    new = "1" * 64
    return f"# DeltaSleuth release key transition\nold: {scratch.fingerprint}\nnew: {new}\nfrom: 2026-10-01\n"


def _sign(scratch: Scratch, path: Path) -> Result:
    return CliRunner().invoke(release.app, ["sign", "--key", f"file:{scratch.key}", "--passphrase",
                                            f"file:{scratch.passphrase}", str(path)])  # fmt: skip


def test_sign_accepts_advisories_and_refuses_sums(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "0" * 64
    signable = {
        "DS-SA-2026-001.txt": "# DeltaSleuth security advisory DS-SA-2026-001\nSummary: fixed in 0.1.1.\n"
                              f"sums_sha256: {digest}\n",
        "transition.txt": _transition(scratch),
        "interim.openvex.json": json.dumps({"@context": "https://openvex.dev/ns/v0.2.0", "statements": []}),
    }  # fmt: skip
    for name, text in signable.items():
        (path := scratch.tmp / name).write_text(text)
        done = _sign(scratch, path)
        assert (done.exit_code, done.output) == (
            0,
            f"signed {path} with key {scratch.fingerprint[:12]}…: {path}.sig\n",
        )
        verify = ["openssl", "dgst", "-sha256", "-verify", str(_public(scratch)), "-signature", f"{path}.sig"]
        verified = subprocess.run([*verify, str(path)], capture_output=True, text=True)
        assert verified.stdout == "Verified OK\n"
    again = _sign(scratch, scratch.tmp / "transition.txt")
    exists = f"error: {scratch.tmp}/transition.txt.sig exists: sign never writes over a file\n"
    assert (again.exit_code, again.output) == (2, exists)
    unsignable = {"SHA256SUMS": f"{digest}  RELEASE.txt\n", "other.json": '{"@context": "https://example.test"}',
                  "DS-SA-26-1.txt": "# DeltaSleuth security advisory DS-SA-26-1\n"}  # fmt: skip
    for name, text in unsignable.items():
        (path := scratch.tmp / name).write_text(text)
        done = _sign(scratch, path)
        refused = (
            f"error: {path} is not an advisory, a key transition or an OpenVEX document: the release key"
        )
        assert (done.exit_code, done.output) == (2, f"{refused} signs nothing else\n")
        assert not path.with_name(f"{name}.sig").exists()
    header = "# DeltaSleuth security advisory DS-SA-2026-003\n"
    listing = "".join(f"{digest}  {name}\n" for name in ("RELEASE.txt", "VERIFY.txt", f"image/{STEM}.tar.gz"))
    checked = {  # an advisory's header, then lines step 3's sha256sum -c or shasum -c would check
        "prefixed-sums.txt": f"{header}#\n{listing}",  # SHA256SUMS behind a header: '#' lines are skipped
        "indented.txt": f"{header}Summary.\n \t\\{digest} *VERIFY.txt\n",
        "tagged.txt": f"{header}Summary.\n  SHA256 (RELEASE.txt) = {digest}\n",
        "sha1.txt": f"{header}Summary.\n{'0' * 40}  RELEASE.txt\n",  # shasum takes it whatever -a says
    }
    for name, text in checked.items():
        (path := scratch.tmp / name).write_text(text)
        done = _sign(scratch, path)
        assert (done.exit_code, done.output) == (2, (
            f"error: {path} has a line that sha256sum or shasum would check: the release key signs no"
            ' checksum list, so begin no line with a digest or "SHA… ("\n'
        ))  # fmt: skip
        assert not path.with_name(f"{name}.sig").exists()
    keys = scratch.root / "tools" / "release-keys.txt"  # a withdrawn key signs nothing more (§4, Compromise)
    keys.write_text(keys.read_text().replace(" signing ", f" withdrawn {TODAY} "))
    (path := scratch.tmp / "DS-SA-2026-004.txt").write_text(header.replace("003", "004"))
    withdrawn = _sign(scratch, path)
    assert (withdrawn.exit_code, withdrawn.output) == (2, (
        f"error: --key: key {scratch.fingerprint[:12]}… is withdrawn {TODAY} in tools/release-keys.txt: it"
        " signs nothing more\n"
    ))  # fmt: skip
    monkeypatch.setattr(licence, "SHIPPED_KEYS", (_public(scratch).read_bytes(),))
    (path := scratch.tmp / "DS-SA-2026-002.txt").write_text(
        "# DeltaSleuth security advisory DS-SA-2026-002\n"
    )
    vendor = _sign(scratch, path)
    assert (vendor.exit_code, vendor.output) == (
        2,
        f"error: {VENDOR_KEY.format(fp=scratch.fingerprint[:12])}\n",
    )


def test_the_old_keys_last_bundle_ships_its_transition(scratch: Scratch) -> None:
    """Rotation (RELEASE.md §4): with the new key listed standby, the old key signs the transition and its
    last bundle ships it, listed; once the new key is signing and the old one retired, it signs nothing."""
    transition, keys = scratch.root / "tools" / "transition.txt", scratch.root / "tools" / "release-keys.txt"
    transition.write_text(_transition(scratch))
    signed = _sign(scratch, transition)
    assert signed.exit_code == 0, signed.output
    filed = hashlib.sha256(release.PROCEDURE.encode()).hexdigest()
    scratch.commit({  # with its .sig, and the new key standby
        "tools/transition.txt": transition.read_text(),
        "tools/release-keys.txt": f"{keys.read_text()}{'1' * 64} standby procedure={filed}\n",
    })  # fmt: skip
    assert scratch.build().exit_code == 0
    listed = (scratch.bundle() / "SHA256SUMS").read_text()
    assert "  transition.txt\n" in listed and "  transition.txt.sig\n" in listed
    assert (scratch.bundle() / "transition.txt.sig").read_bytes() == transition.with_name(
        "transition.txt.sig"
    ).read_bytes()
    swapped = keys.read_text().replace(" signing ", f" retired {TODAY} ").replace(" standby ", " signing ")
    keys.write_text(swapped)
    (advisory := scratch.tmp / "DS-SA-2026-001.txt").write_text(
        "# DeltaSleuth security advisory DS-SA-2026-001\n"
    )
    retired = _sign(scratch, advisory)
    assert (retired.exit_code, retired.output) == (2, (
        f"error: --key: key {scratch.fingerprint[:12]}… is retired {TODAY} in tools/release-keys.txt: it"
        " signs nothing more\n"
    ))  # fmt: skip


def _need(ready: bool, reason: str) -> None:
    """Skip without the tools or a database; fail instead where they must be (DS_REQUIRE_TOOLS=1)."""
    if not ready:
        (pytest.fail if os.environ.get("DS_REQUIRE_TOOLS") else pytest.skip)(reason)


def test_real_syft_and_grype(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool's own syft and grype runs on a real saved image (the local postgres:16-alpine), with a hostile
    ~/.grype.yaml and ~/.syft.yaml, a .grype.yaml in the working directory and GRYPE_ONLY_NOTFIXED and
    SYFT_EXCLUDE set: none applies; and grype applies the tool's VEX to both SBOMs."""
    missing = [tool for tool in ("docker", "syft", "grype") if shutil.which(tool) is None]
    _need(not missing, f"missing {', '.join(missing)}")
    staging = release._staging(tmp_path / "staging")
    scanning = release._scanning(staging)
    versions = {tool: subprocess.run([tool, "version", "-o", "json"], capture_output=True, text=True,
                                     **scanning) for tool in ("syft", "grype")}  # fmt: skip
    approved = {tool: json.loads(done.stdout or "{}").get("version") for tool, done in versions.items()}
    _need(
        approved == {"syft": release.SYFT, "grype": release.GRYPE}, f"not the approved scanners: {approved}"
    )
    status = subprocess.run(["grype", "db", "status", "-o", "json"], capture_output=True, text=True,
                            **release._scanning(staging))  # fmt: skip
    built = datetime.fromisoformat(
        json.loads(status.stdout or "{}").get("built", "2000-01-01T00:00:00+00:00")
    )
    _need(
        status.returncode == 0 and datetime.now(UTC) - built < timedelta(hours=120),
        "no grype database of 120 h",
    )
    saved = subprocess.run(
        ["docker", "save", "-o", str(staging / "image.tar"), PG_IMAGE], capture_output=True
    )
    _need(saved.returncode == 0, f"{PG_IMAGE} is not in the local image store")
    home = tmp_path / "home"
    for path in (home / ".grype.yaml", home / ".syft.yaml", tmp_path / "cwd" / ".grype.yaml"):
        path.parent.mkdir(exist_ok=True)
        path.write_text("only-notfixed: true\nexclude: ['**']\nignore: [{package: {type: apk}}]\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("GRYPE_ONLY_NOTFIXED", "true")
    monkeypatch.setenv("SYFT_EXCLUDE", "**")
    assert release._approved(staging) == approved
    release._sbom(staging, "0.0.0", "Example Vendor Ltd", "pg")
    cdx = json.loads((staging / "bundle" / "sbom" / "pg.cdx.json").read_text())
    spdx = json.loads((staging / "bundle" / "sbom" / "pg.spdx.json").read_text())
    assert (cdx["specVersion"], spdx["spdxVersion"]) == ("1.6", "SPDX-2.3")
    assert cdx["metadata"]["supplier"] == {"name": "Example Vendor Ltd"}
    components = list(release._components(cdx["components"]))
    assert ("operating-system", "alpine") in {(c["type"], c["name"]) for c in components}
    assert sum(c.get("purl", "").startswith("pkg:apk/alpine/") for c in components) > 10  # nothing excluded
    for text in (
        (staging / "bundle" / "sbom" / name).read_text() for name in ("pg.cdx.json", "pg.spdx.json")
    ):
        assert str(tmp_path) not in text and str(tmp_path.resolve()) not in text and "/Users/" not in text
    scanned = {kind: release._grype(staging, f"bundle/sbom/pg.{kind}.json") for kind in ("cdx", "spdx")}
    configuration = scanned["cdx"]["descriptor"]["configuration"]
    assert configuration["only-notfixed"] is False
    assert "apk" not in [rule["package"]["type"] for rule in configuration["ignore"]]
    matches = scanned["cdx"]["matches"]
    assert len(matches) == len(scanned["spdx"]["matches"])
    if matches:  # the tool's VEX, as build writes it, suppresses on both SBOMs
        first = matches[0]["vulnerability"]["id"], matches[0]["artifact"]["purl"].split("?")[0]
        result = release.gate(matches, {}, {"finding": [_entry(*first)]}, TODAY)
        vex = release._vex(result, {"version": "0.0.0", "vendor": "Example Vendor Ltd"}, "linux/arm64",
                           "sha256:" + "0" * 64, "2026-09-29T00:00:00Z")  # fmt: skip
        (staging / "vex.openvex.json").write_text(json.dumps(vex))
        for kind in ("cdx", "spdx"):
            vexed = release._grype(staging, f"bundle/sbom/pg.{kind}.json", "--vex", "vex.openvex.json")
            left = {(m["vulnerability"]["id"], m["artifact"]["purl"].split("?")[0]) for m in vexed["matches"]}
            assert first not in left and vexed["ignoredMatches"]
