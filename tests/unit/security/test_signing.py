"""The evidence packs' signing key (docs/mvp/PACK.md §3.1, §3.2, §9): EC P-256 only, refused unechoed
otherwise; deterministic signatures that ``openssl dgst`` verifies (OpenSSL 3, and LibreSSL where it is
installed); the fingerprint step 1 prints; and VERIFY.txt as ``ds packs key``'s procedure but for its two
filled lines."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from typer.testing import CliRunner

from deltasleuth.cli.main import app
from deltasleuth.core.secrets import SecretError
from deltasleuth.reports import pack
from deltasleuth.security.signing import (
    EXPECTED,
    PackSigner,
    SigningKeyError,
    fingerprint,
    load_public,
    verifies,
)
from deltasleuth.store.chain import FIELDS
from tests.conftest import REPO_ROOT, encryption
from tests.unit.reports.packs import tools

LIBRESSL = Path("/usr/bin/openssl")


def _pem(key: object, encrypted: bool = False) -> bytes:
    protection = BestAvailableEncryption(b"passphrase") if encrypted else NoEncryption()
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, protection)  # type: ignore[attr-defined]


def _key_file(tmp_path: Path, data: bytes, name: str = "pack-signing-key") -> Path:
    path = tmp_path / "secrets" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def _openssls() -> list[str]:
    """The OpenSSL on PATH, and macOS's LibreSSL where it is installed."""
    found = ["openssl"]
    if LIBRESSL.exists():
        version = subprocess.run([LIBRESSL, "version"], capture_output=True, text=True, check=False).stdout
        found += [str(LIBRESSL)] if version.startswith("LibreSSL") else []
    return found


def test_a_p256_key_signs_deterministically_as_openssl_verifies_under_step_1_s_fingerprint(
    tmp_path: Path,
) -> None:
    tools()
    signer = PackSigner.load(f"file:{_key_file(tmp_path, _pem(ec.generate_private_key(ec.SECP256R1())))}")
    sums = b"0" * 64 + b"  VERIFY.txt\n"
    signature = signer.sign(sums)
    assert signature == signer.sign(sums)  # RFC 6979: no random nonce
    for name, data in (
        ("signing-key.pem", signer.public_pem),
        ("SHA256SUMS", sums),
        ("SHA256SUMS.sig", signature),
    ):
        (tmp_path / name).write_bytes(data)
    for openssl in _openssls():
        step_1 = subprocess.run(
            ["sh", "-c", f"{openssl} pkey -pubin -in signing-key.pem -outform DER | sha256sum"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        verify = [
            "dgst",
            "-sha256",
            "-verify",
            "signing-key.pem",
            "-signature",
            "SHA256SUMS.sig",
            "SHA256SUMS",
        ]
        step_2 = subprocess.run([openssl, *verify], cwd=tmp_path, capture_output=True, text=True, check=False)
        assert step_1.stdout == f"{signer.fingerprint}  -\n", openssl
        assert step_2.stdout == "Verified OK\n", openssl
    assert fingerprint(signer.public_pem) == signer.fingerprint and re.fullmatch(
        "[0-9a-f]{64}", signer.fingerprint
    )
    assert verifies(signer.public_pem, signature, sums) and not verifies(
        signer.public_pem, signature, sums + b"x"
    )
    assert repr(signer) == f"PackSigner(fingerprint={signer.fingerprint!r})"
    with pytest.raises(ValueError, match="not a PEM EC P-256 public key"):
        fingerprint(b"-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----\n")


def test_a_public_key_file_is_one_bare_p256_block_so_that_both_steps_read_one_key() -> None:
    private = ec.generate_private_key(ec.SECP256R1())
    signer, other = PackSigner(private), PackSigner(ec.generate_private_key(ec.SECP256R1()))
    key, found = load_public(b"\n " + signer.public_pem.replace(b"\n", b"\r\n") + b"\n\n")
    assert found == fingerprint(signer.public_pem) == signer.fingerprint
    assert verifies(key, signer.sign(b"x"), b"x") and not verifies(key, other.sign(b"x"), b"x")
    begin, rest = other.public_pem.split(b"\n", 1)
    point = private.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)
    compressed = bytes.fromhex("3039301306072a8648ce3d020106082a8648ce3d030107032200") + point  # its SPKI
    p384 = ec.generate_private_key(ec.SECP384R1()).public_key()
    refused = {
        # OpenSSL and cryptography read a block with a header line, which a strict reader skips.
        "a header, then the register's key": begin + b"\nComment: x\n\n" + rest + signer.public_pem,
        "two blocks": signer.public_pem + other.public_pem,
        "text around it": b"key:\n" + signer.public_pem,
        "a compressed point, re-encoded when read": (
            b"-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(compressed) + b"\n-----END PUBLIC KEY-----\n"
        ),
        "a P-384 key": p384.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        "a private key": _pem(private),
    }
    for case, data in refused.items():
        with pytest.raises(ValueError, match=r"^not a PEM EC P-256 public key$"):
            load_public(data)
        assert not verifies(data, signer.sign(b"x"), b"x"), case


@pytest.mark.parametrize("kind", ["rsa", "ed25519", "encrypted", "p384", "certificate-like", "empty"])
def test_other_keys_are_refused_without_echoing_them(tmp_path: Path, kind: str) -> None:
    data = {
        "rsa": lambda: _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        "ed25519": lambda: _pem(ed25519.Ed25519PrivateKey.generate()),
        "encrypted": lambda: _pem(ec.generate_private_key(ec.SECP256R1()), encrypted=True),
        "p384": lambda: _pem(ec.generate_private_key(ec.SECP384R1())),
        "certificate-like": lambda: (
            b"-----BEGIN CERTIFICATE-----\nMIIBsecretmaterial\n-----END CERTIFICATE-----\n"
        ),
        "empty": lambda: b"",
    }[kind]()
    with pytest.raises(SigningKeyError) as refused:
        PackSigner.load(f"file:{_key_file(tmp_path, data)}")
    assert str(refused.value) == EXPECTED
    error = refused.value  # nothing chained that could carry the library's view of the file
    assert error.__cause__ is None and (error.__suppress_context__ or error.__context__ is None)
    for line in data.splitlines():
        if line and not line.startswith(b"-----"):
            assert line.decode() not in str(refused.value)


def test_an_unreadable_key_names_its_file_only(tmp_path: Path) -> None:
    with pytest.raises(SecretError, match=re.escape(f"cannot read secret file {tmp_path / 'absent'}")):
        PackSigner.load(f"file:{tmp_path / 'absent'}")


def _settings(tmp_path: Path, key: Path | None) -> Path:
    (tmp_path / "dsn").write_text("postgresql://unused\n")
    (tmp_path / "t.yaml").write_text('targets:\n  svc: {base_url: "https://svc.example/"}\n')
    (tmp_path / "oidc_secret").write_text("s3cret\n")
    oidc = {"issuer": "https://idp.example", "client_id": "ds", "roles": {"rule_approver": ["G"]},
            "client_secret_ref": f"file:{tmp_path / 'oidc_secret'}"}  # fmt: skip
    doc = {
        "database_dsn_ref": f"file:{tmp_path / 'dsn'}", "evidence_dir": "e", "workspace_dir": "w",
        "targets_file": "t.yaml", "auth": {"public_url": "https://ds.example", "oidc": oidc},
        "encryption": encryption(tmp_path / "keys"),
    }  # fmt: skip
    if key is not None:
        doc["packs"] = {"installation": "prod-eu-1", "signing_key_ref": f"file:{key}"}
    (tmp_path / "ds.yaml").write_text(yaml.safe_dump(doc))
    return tmp_path / "ds.yaml"


def _procedure_in_the_contract() -> str:
    """PACK.md §3.3's block without its two pack-specific lines."""
    text = (REPO_ROOT / "docs/mvp/PACK.md").read_text()
    block = text.split("### 3.3", 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0]
    return block.split("\n", 2)[2] + "\n"


def test_verify_txt_is_ds_packs_key_s_procedure_but_its_two_filled_lines(tmp_path: Path) -> None:
    key = _key_file(tmp_path, _pem(ec.generate_private_key(ec.SECP256R1())))
    signer = PackSigner.load(f"file:{key}")
    filed = CliRunner().invoke(
        app, ["packs", "key", "--out", str(tmp_path / "register"), "--config", str(_settings(tmp_path, key))]
    )
    assert filed.exit_code == 0, filed.output
    assert filed.stdout == (
        f"wrote fingerprint.txt and procedure.txt to {tmp_path / 'register'}: file both in the key register,"
        f" dated (installation prod-eu-1, fingerprint {signer.fingerprint})\n"
    )
    procedure = (tmp_path / "register" / "procedure.txt").read_text()
    assert (tmp_path / "register" / "fingerprint.txt").read_text() == (
        f"installation: prod-eu-1\nfingerprint: {signer.fingerprint}\n"
    )
    verify = pack.verify_text(f"P-{'0' * 26}", "prod-eu-1", f"A-{'1' * 26}", f"A-{'2' * 26}", 81234, 5120)
    first, second, rest = verify.split("\n", 2)
    assert rest == procedure == _procedure_in_the_contract()
    assert ", ".join(FIELDS) in procedure  # step 7 compares every audited field of a row up to UPGRADE
    pack_id = f"P-{'0' * 26}"
    assert first == (
        f"DeltaSleuth evidence pack {pack_id} of installation prod-eu-1. Unzip it, cd {pack_id}/, then run in"
        " sh the copy of the procedure below filed with your key register; its step 0 reads line 2:"
    )
    assert second == f"PACK=P-{'0' * 26} SIGNOFF=A-{'1' * 26} SPECAPP=A-{'2' * 26} HEAD=81234 UPGRADE=5120"


def test_ds_packs_key_and_ds_serve_refuse_missing_pack_settings_and_bad_keys_in_one_line(
    tmp_path: Path,
) -> None:
    without = CliRunner().invoke(
        app, ["packs", "key", "--out", str(tmp_path / "r"), "--config", str(_settings(tmp_path, None))]
    )
    assert (without.exit_code, without.stderr) == (2, f"error: {pack.NOT_CONFIGURED}\n")
    config = _settings(
        tmp_path, _key_file(tmp_path, _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))
    )
    out = ["--out", str(tmp_path / "r")]
    for command in (["packs", "key", *out], ["packs", "revocations", *out], ["serve", "--host", "10.1.2.3"]):
        refused = CliRunner().invoke(app, [*command, "--config", str(config)])
        assert (refused.exit_code, refused.stderr.splitlines()[-1]) == (2, f"error: {EXPECTED}"), command
    assert not (tmp_path / "r").exists()
