"""A release bundle checked as a site checks it, on the Docker host: its own
VERIFY.txt run step by step with ``sh``, under ``sha256sum``, under ``shasum -a 256`` and, where it is
installed, with macOS's LibreSSL, a docker stub loading the image for step 5. The untouched bundle passes;
each tamper fails the step that catches it."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from deltasleuth.security.signing import PackSigner
from tests.unit import releases
from tests.unit.releases import Scratch
from tests.unit.reports.packs import Ran, run_step, steps, tools

LIBRESSL = Path("/usr/bin/openssl")  # macOS's: the procedure must work with it too
STEM = "deltasleuth-0.1.0-linux-amd64"
IMAGE = f"image/{STEM}.tar.gz"


def _libressl() -> bool:
    if not LIBRESSL.exists():
        return False
    found = subprocess.run([LIBRESSL, "version"], capture_output=True, text=True, check=False)
    return found.stdout.startswith("LibreSSL")


VARIANTS = ["sha256sum", "shasum", *(["libressl"] if _libressl() else [])]


def run_with(folder: Path, number: int, variant: str) -> Ran:
    """A step under ``sha256sum``, ``shasum`` (the procedure's function) or ``libressl`` (both shims)."""
    if variant != "libressl":
        return run_step(folder, number, shasum=variant == "shasum")
    script = f'openssl() {{ {LIBRESSL} "$@"; }}\nsha256sum() {{ shasum -a 256 "$@"; }}\n'
    script += steps((folder / "VERIFY.txt").read_text())[number]
    done = subprocess.run(["sh", "-c", script], cwd=folder, capture_output=True, text=True, check=False)
    return Ran(done.returncode, done.stdout.splitlines(), done.stderr)


@pytest.fixture
def released(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> tuple[Scratch, Path]:
    """A signed bundle, its folder beside the procedure the site filed with the release key."""
    tools()
    scratch = releases.scratch(
        tmp_path, monkeypatch, repo_root, path=os.environ["PATH"]
    )  # openssl, sha256sum
    done = scratch.build()
    assert done.exit_code == 0, done.output
    shutil.copy(scratch.procedure, scratch.out / "FILED-RELEASE-PROCEDURE.txt")
    return scratch, scratch.bundle()


def _copy(bundle: Path, into: Path) -> Path:
    """A copy of the bundle's folder, with the filed procedure beside it."""
    shutil.copytree(bundle, into / bundle.name)
    shutil.copy(bundle.parent / "FILED-RELEASE-PROCEDURE.txt", into)
    return into / bundle.name


def _resign(folder: Path, signer: PackSigner) -> None:
    """SHA256SUMS over the folder's listed files, signed by ``signer``, as a holder of that key would."""
    unlisted = ("SHA256SUMS", "SHA256SUMS.sig", "release-key.pem")
    names = sorted(
        p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and p.name not in unlisted
    )
    sums = "".join(f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in names).encode()
    (folder / "SHA256SUMS").write_bytes(sums)
    (folder / "SHA256SUMS.sig").write_bytes(signer.sign(sums))
    (folder / "release-key.pem").write_bytes(signer.public_pem)


def _relist(folder: Path) -> None:
    """SHA256SUMS rewritten over the files as they are, unsigned."""
    lines = (folder / "SHA256SUMS").read_text().splitlines()
    names = [line[66:] for line in lines]
    (folder / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
    )


def test_the_untouched_bundle_passes_its_own_procedure(released: tuple[Scratch, Path]) -> None:
    scratch, bundle = released
    facts = dict(line.split(": ", 1) for line in (bundle / "RELEASE.txt").read_text().splitlines())
    listed = [line[66:] for line in (bundle / "SHA256SUMS").read_text().splitlines()]
    shown = ("version", "revision", "created", "platform", "image", "config", "manifest", "key", "signed")
    for variant in VARIANTS:
        ran = {number: run_with(bundle, number, variant) for number in range(6)}
        assert (ran[0].code, ran[0].out) == (0, []), variant
        assert ran[1].out == [f"{scratch.fingerprint}  -"], variant
        assert ran[2].out == ["Verified OK"], variant
        assert (ran[3].code, ran[3].out) == (0, [f"{name}: OK" for name in listed]), variant
        digest = hashlib.sha256((bundle / "SHA256SUMS").read_bytes()).hexdigest()
        assert ran[4].out == [*(f"{name}: {facts[name]}" for name in shown), digest], variant
        assert ran[5].out == ["Loaded image: deltasleuth:0.1.0", "1"], (
            variant
        )  # the classic store's config id
        assert all(r.err == "" for r in ran.values()), variant
    assert (scratch.out / "id.txt").read_text() == f"{facts['config']}\n"


def test_each_tamper_fails_its_step(released: tuple[Scratch, Path], tmp_path: Path) -> None:
    scratch, pristine = released
    other = PackSigner(ec.generate_private_key(ec.SECP256R1()))
    genuine, other_key = (pristine / "release-key.pem").read_bytes(), f"{other.fingerprint}  -"

    def flipped(folder: Path) -> None:
        data = bytearray((folder / IMAGE).read_bytes())
        data[len(data) // 2] ^= 0x01
        (folder / IMAGE).write_bytes(bytes(data))

    def edited_sums(folder: Path) -> None:
        text = (folder / "SHA256SUMS").read_text()
        (folder / "SHA256SUMS").write_text(
            text.replace("  NOTES.txt", "  NOTES.txt\n" + "0" * 64 + "  extra.txt")
        )

    def swapped(folder: Path) -> None:  # on the way to the host: another image, its RELEASE.txt and sums
        (folder / IMAGE).write_bytes(gzip.compress(b"another image", mtime=0))
        facts = (folder / "RELEASE.txt").read_text()
        (folder / "RELEASE.txt").write_text(
            re.sub(r"(?m)^(config|manifest): sha256:\w+", r"\1: sha256:" + "e" * 64, facts)
        )
        _relist(folder)

    def two_key_blocks(folder: Path) -> None:  # another key signs; the genuine block follows its own
        _resign(folder, other)
        begin, rest = other.public_pem.split(b"\n", 1)
        (folder / "release-key.pem").write_bytes(begin + b"\nComment: x\n\n" + rest + genuine)

    def commented_sums(folder: Path) -> None:  # an advisory's shape: sha256sum skips, macOS's only warns
        text = (folder / "SHA256SUMS").read_text()
        (folder / "SHA256SUMS").write_text("# DeltaSleuth security advisory DS-SA-2026-001\n#\n" + text)

    def line_before_the_header(folder: Path) -> None:
        lines = (folder / "VERIFY.txt").read_text().split("\n")
        (folder / "VERIFY.txt").write_text("\n".join([*lines[:2], "echo injected", *lines[2:]]))

    def shell_in_line_2(folder: Path) -> None:  # the site's shell must never run it
        lines = (folder / "VERIFY.txt").read_text().split("\n")
        lines[1] += "; openssl() { echo Verified OK; }; echo injected"
        (folder / "VERIFY.txt").write_text("\n".join(lines))

    cases: dict[str, tuple[Callable[[Path], None], int, Callable[[Ran], bool]]] = {
        # name: (tamper, the step that catches it, how it shows there)
        "a flipped image byte": (flipped, 3, lambda r: f"{IMAGE}: FAILED" in r.out),
        "a file added": (lambda f: (f / "extra.txt").write_text("x\n"), 3, lambda r: "> extra.txt" in r.out),
        "a file removed": (lambda f: (f / "sbom" / "vendored-libraries.txt").unlink(), 3,
                           lambda r: "< sbom/vendored-libraries.txt" in r.out),
        "an edited SHA256SUMS": (edited_sums, 2, lambda r: r.out != ["Verified OK"]),
        "a list line that no tool checks": (commented_sums, 3, lambda r: r.out[:2] == [
            "# DeltaSleuth security advisory DS-SA-2026-001", "#"]),
        "a consistent unsigned swap": (swapped, 2, lambda r: r.out != ["Verified OK"]),
        "another key's re-signature": (partial(_resign, signer=other), 1, lambda r: r.out == [other_key]),
        "two key blocks, the first with a header": (two_key_blocks, 2, lambda r: r.out != ["Verified OK"]),
        "a line before the header": (line_before_the_header, 0, lambda r: "< echo injected" in r.out),
        "shell in line 2": (shell_in_line_2, 0, lambda r: r.out == ["line 2 of VERIFY.txt is malformed"]),
    }  # fmt: skip
    for index, (name, (tamper, number, shows)) in enumerate(cases.items()):
        folder = _copy(pristine, tmp_path / f"case-{index}")
        if name == "a consistent unsigned swap":  # after a pass elsewhere, which then counts for nothing
            assert [run_step(folder, n).code for n in range(5)] == [0] * 5
        tamper(folder)
        for variant in VARIANTS:
            ran = run_with(folder, number, variant)
            assert shows(ran), (name, variant, ran)  # the procedure reads output, not exit codes
        if name == "a consistent unsigned swap":  # only the signature sees it: step 3 is consistent
            assert run_step(folder, 3).code == 0 and run_step(folder, 1).out == [f"{scratch.fingerprint}  -"]
        if name == "two key blocks, the first with a header":  # OpenSSL 3 shows the genuine key's fingerprint
            assert run_step(folder, 1).out == [f"{scratch.fingerprint}  -"] and run_step(folder, 3).code == 0
        if name in ("a line before the header", "shell in line 2"):  # read, never run
            assert all("injected" not in run_step(folder, n).out for n in range(6)), name
    scratch.scenario["loaded_id"] = "sha256:" + "9" * 64  # the tag names another image than the release lists
    (scratch.tmp / "scenario.json").write_text(json.dumps(scratch.scenario))
    folder = _copy(pristine, tmp_path / "another-id")
    assert [run_with(folder, 5, variant).out[-1] for variant in VARIANTS] == ["0"] * len(VARIANTS)
