"""The vendor tool: keygen writes an encrypted key outside any git working tree;
issue refuses what ``licence.parse`` would and a key no build trusts, and what it writes parses back as
registered."""

from __future__ import annotations

import ast
import json
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner, Result

from deltasleuth import licence
from deltasleuth.security import signing
from tools import dsv

INSTALLATION = "I-3f2a9c1b7d4e5a60"


def dsv_(*args: object) -> Result:
    return CliRunner().invoke(dsv.app, [str(a) for a in args])


@pytest.fixture
def vendor(tmp_path: Path) -> dict[str, Path]:
    """A key pair from ``dsv keygen`` in a fresh directory, its passphrase in a file of its own."""
    (tmp_path / "media").mkdir()
    passphrase = tmp_path / "media" / "passphrase"
    passphrase.write_text("correct horse battery staple\n")
    done = dsv_("keygen", "--out", tmp_path / "keys", "--passphrase", f"file:{passphrase}")
    assert done.exit_code == 0, done.output
    [key] = (tmp_path / "keys").glob("vendor-*.key.pem")
    return {"key": key, "public": key.with_name(key.name.replace(".key.", ".pub.")), "passphrase": passphrase}


def test_keygen_writes_an_encrypted_key(vendor: dict[str, Path]) -> None:
    public = vendor["public"].read_bytes()
    fingerprint = signing.fingerprint(public)
    assert vendor["key"].name == f"vendor-{fingerprint[:16]}.key.pem"
    assert stat.S_IMODE(vendor["key"].stat().st_mode) == 0o600
    with pytest.raises(TypeError, match="encrypted"):
        serialization.load_pem_private_key(vendor["key"].read_bytes(), password=None)
    key = serialization.load_pem_private_key(vendor["key"].read_bytes(), b"correct horse battery staple")
    assert signing.PackSigner(key).public_pem == public  # type: ignore[arg-type]


def test_keygen_output_names_the_key(tmp_path: Path) -> None:
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("a passphrase\n")
    done = dsv_("keygen", "--out", tmp_path / "keys", "--passphrase", f"file:{passphrase}")
    [public] = (tmp_path / "keys").glob("vendor-*.pub.pem")
    fingerprint = signing.fingerprint(public.read_bytes())
    lines = done.output.splitlines()
    assert lines[0] == f"fingerprint: {fingerprint}" and lines[-1] == f"{fingerprint} trusted"
    entry = "".join(line for line in lines if line.startswith('    b"'))  # an entry of the tuple
    assert ast.literal_eval(entry) == (public.read_bytes(),)


def test_keygen_refuses_a_git_working_tree(tmp_path: Path) -> None:
    (tmp_path / "checkout" / ".git").mkdir(parents=True)
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("a passphrase\n")
    done = dsv_("keygen", "--out", tmp_path / "checkout" / "keys", "--passphrase", f"file:{passphrase}")
    assert done.exit_code == 2
    assert done.output == (
        f"error: {tmp_path.resolve() / 'checkout' / 'keys'} is in a git working tree: keep vendor keys"
        " outside every repository\n"
    )
    assert not (tmp_path / "checkout" / "keys").exists()
    relative = dsv_("keygen", "--out", tmp_path / "keys", "--passphrase", "file:passphrase")
    assert (relative.exit_code, relative.output) == (2, "error: --passphrase must be file:<absolute path>\n")


def _issue(vendor: dict[str, Path], tmp_path: Path, *extra: object) -> Result:
    return dsv_(
        "issue", "--key", f"file:{vendor['key']}", "--passphrase", f"file:{vendor['passphrase']}",
        "--customer", "Example Bank plc", "--order", "ORD-2026-014", "--installation", INSTALLATION,
        "--tier", "pro", "--starts", "2026-10-01", "--ends", "2027-09-30", "--out", tmp_path / "issued",
        "--register", tmp_path / "register.jsonl", *extra,
    )  # fmt: skip


def test_issue_parse_round_trip(
    vendor: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(licence, "VENDOR_KEYS", (vendor["public"].read_bytes(),))
    done = _issue(vendor, tmp_path)
    assert done.exit_code == 0, done.output
    [written] = (tmp_path / "issued").glob("LI-*.txt")
    found = licence.parse(written.read_bytes())
    assert (found.customer, found.order, found.installation_id, found.tier, found.pairs) == (
        "Example Bank plc", "ORD-2026-014", INSTALLATION, "pro", 5
    )  # fmt: skip
    assert abs(found.issued - datetime.now(UTC)) < timedelta(minutes=1)
    assert done.output == (
        f"issued {found.licence_id} (pro, Example Bank plc, order ORD-2026-014, 2026-10-01 to 2027-09-30,"
        f" 5 pairs) for {INSTALLATION}: {written}\n"
    )
    [entry] = [json.loads(line) for line in (tmp_path / "register.jsonl").read_text().splitlines()]
    assert entry == found.fields()
    assert _issue(vendor, tmp_path, "--pairs", "unlimited").exit_code == 0
    listed = dsv_("register", "--register", tmp_path / "register.jsonl")
    assert re.findall(r"(\S+) pairs\)", listed.output) == ["5", "unlimited"]
    ending = dsv_("register", "--register", tmp_path / "register.jsonl", "--ending", 60)
    assert ending.output == ""  # 2027-09-30 is not within 60 days of today


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("--customer", "Example Bank plc "), "error: customer begins or ends with a blank\n"),
        (("--order", " ORD-2026-014"), "error: order begins or ends with a blank\n"),
        (("--ends", "2027-10-02"), "error: the licence is not a DeltaSleuth licence"
         " (deltasleuth-licence/1): ends is more than 365 days after starts\n"),
        (("--installation", "prod-eu-1"), "error: the licence is not a DeltaSleuth licence"
         " (deltasleuth-licence/1): installation is not I- and 16 of 0-9 and a-f\n"),
    ],
)  # fmt: skip
def test_issue_refuses_what_parse_would(
    vendor: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: tuple[str, str],
    message: str,
) -> None:
    monkeypatch.setattr(licence, "VENDOR_KEYS", (vendor["public"].read_bytes(),))
    done = _issue(vendor, tmp_path, *change)
    assert (done.exit_code, done.output) == (2, message)
    assert not (tmp_path / "register.jsonl").exists() and not (tmp_path / "issued").exists()


def test_issue_refuses_a_key_no_build_trusts(vendor: dict[str, Path], tmp_path: Path) -> None:
    fingerprint = signing.fingerprint(vendor["public"].read_bytes())
    done = _issue(vendor, tmp_path)  # the session's builds trust their test key only
    assert (done.exit_code, done.output) == (
        2, f"error: key {fingerprint[:12]}… is not in VENDOR_KEYS: no build trusts it yet\n"
    )  # fmt: skip
    assert not (tmp_path / "register.jsonl").exists()
    vendor["passphrase"].write_text("not its passphrase\n")
    wrong = _issue(vendor, tmp_path)
    assert (wrong.exit_code, wrong.output) == (
        2, "error: --key: not an encrypted PEM private key, or not its passphrase\n"
    )  # fmt: skip
    relative = _issue(vendor | {"key": Path("vendor.key.pem")}, tmp_path)
    assert (relative.exit_code, relative.output) == (2, "error: --key must be file:<absolute path>\n")
