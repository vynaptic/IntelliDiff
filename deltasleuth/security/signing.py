"""The evidence packs' signing key (docs/mvp/PACK.md §3): one EC P-256 key per installation, a ``file:``
secret in PEM (PKCS#8, unencrypted), that signs a pack's ``SHA256SUMS`` and the revocation list with
deterministic ECDSA over SHA-256 (RFC 6979), DER-encoded, as ``openssl dgst -sha256 -verify`` checks it.

A key is named by its fingerprint: the SHA-256 of its public key's DER SubjectPublicKeyInfo, as step 1
of the verification prints it (``openssl pkey -pubin -outform DER | sha256sum``) and the site's key
register lists it. Errors name the setting, never the file's content.
"""

from __future__ import annotations

import base64
import hashlib
import re

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from deltasleuth.core.secrets import read_secret_bytes

EXPECTED = "packs.signing_key_ref: expected an unencrypted PEM EC P-256 private key (docs/mvp/PACK.md §3.1)"
NOT_PUBLIC = "not a PEM EC P-256 public key"
_ECDSA = ec.ECDSA(hashes.SHA256(), deterministic_signing=True)
_PEM = re.compile(rb"\s*-----BEGIN PUBLIC KEY-----([A-Za-z0-9+/=\s]*)-----END PUBLIC KEY-----\s*")


class SigningKeyError(ValueError):
    """The signing key is unreadable or not an unencrypted PEM EC P-256 private key."""


class PackSigner:
    """The installation's signing key; its repr and errors carry the fingerprint only."""

    __slots__ = ("_key", "fingerprint", "public_pem")

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise SigningKeyError(EXPECTED)
        self._key = key
        self.public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.fingerprint = fingerprint(self.public_pem)

    def __repr__(self) -> str:
        return f"PackSigner(fingerprint={self.fingerprint!r})"

    @classmethod
    def load(cls, ref: str) -> PackSigner:
        """The key at ``file:<path>`` (``packs.signing_key_ref``)."""
        data = read_secret_bytes(ref)
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):  # not PEM, encrypted, or unsupported
            raise SigningKeyError(EXPECTED) from None
        return cls(key)  # type: ignore[arg-type]

    def sign(self, data: bytes) -> bytes:
        """The DER signature of ``data`` (ECDSA P-256 over its SHA-256, RFC 6979)."""
        return self._key.sign(data, _ECDSA)


def load_public(public_pem: bytes) -> tuple[ec.EllipticCurvePublicKey, str]:
    """The EC P-256 public key a PEM file holds, and its fingerprint (the SHA-256, hex, of its DER
    SubjectPublicKeyInfo); ValueError unless the file is one ``PUBLIC KEY`` block without header lines,
    with nothing but whitespace around it, whose DER is the key's own: the key step 1's ``openssl pkey``
    fingerprints is then the key step 2 verifies with."""
    match = _PEM.fullmatch(public_pem)
    try:
        der = base64.b64decode(b"".join(match[1].split()), validate=True) if match else b""
        key = serialization.load_der_public_key(der)
    except (ValueError, UnsupportedAlgorithm):
        raise ValueError(NOT_PUBLIC) from None
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if (
        not isinstance(key, ec.EllipticCurvePublicKey)
        or not isinstance(key.curve, ec.SECP256R1)
        or key.public_bytes(*spki) != der  # a key re-encoded, not as the file holds it
    ):
        raise ValueError(NOT_PUBLIC)
    return key, hashlib.sha256(der).hexdigest()


def fingerprint(public_pem: bytes) -> str:
    """The fingerprint of a PEM public key (``load_public``); ValueError when it is not one."""
    return load_public(public_pem)[1]


def verifies(public_key: bytes | ec.EllipticCurvePublicKey, signature: bytes, data: bytes) -> bool:
    """Whether ``signature`` is an ECDSA P-256/SHA-256 signature of ``data`` by the public key, given as
    ``load_public`` returns it or as the PEM it reads."""
    try:
        key = load_public(public_key)[0] if isinstance(public_key, bytes) else public_key
        key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature):
        return False
    return True
