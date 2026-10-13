"""Envelope encryption at rest (docs/mvp/RETENTION.md §1, §3).

A keyring of key-encryption keys (KEKs) wraps data keys with AES-KW (RFC 3394); a run's data key seals
values with AES-256-GCM under an HKDF-SHA256 subkey per (label, cycle, run). Keys are named by ids, keyed
hashes that identify a key without revealing it; errors and reprs carry ids, never key material.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from deltasleuth.core.secrets import SecretError, read_secret_bytes

KEY_BYTES = 32
WRAPPED_BYTES = 40  # AES-KW of a 32-byte key
NONCE_BYTES = 12
TAG_BYTES = 16
_ID_LABEL = b"deltasleuth/kek_id/v1"
_SEALED = b"\x01"  # the sealed value format
_HEX_KEY = re.compile(rb"[0-9a-fA-F]{64}")
_REFUSED = "sealed value does not authenticate"


class KeyringError(ValueError):
    """The keyring is unreadable or lacks a KEK; the message names a file, line or KEK id only."""


def key_id(prefix: str, key: bytes) -> str:
    """``prefix`` (``K-`` for a KEK, ``E-`` for an evidence key) + 16 hex of HMAC-SHA256(key, label)."""
    return prefix + hmac.new(key, _ID_LABEL, hashlib.sha256).hexdigest()[:16]


@dataclass(frozen=True)
class DataKey:
    """A ``ds.data_keys`` row as loaded: its scope (``address``, ``R-…`` or ``E-…``), the KEK wrapping
    it, and the key, None while the keyring lacks that KEK."""

    scope: str
    kek: str
    key: bytes | None = field(default=None, repr=False)


class Keyring:
    """KEKs, oldest first: every one unwraps, the last (``current``) wraps."""

    __slots__ = ("_keks", "current", "ids")

    def __init__(self, keks: Sequence[bytes]) -> None:
        if not keks or any(len(kek) != KEY_BYTES for kek in keks):
            raise KeyringError(f"a keyring holds {KEY_BYTES}-byte keys")
        self._keks = {key_id("K-", kek): kek for kek in keks}
        self.ids = tuple(self._keks)
        self.current = key_id("K-", keks[-1])

    def __repr__(self) -> str:
        return f"Keyring(current={self.current!r}, ids={self.ids!r})"

    @classmethod
    def from_file(cls, ref: str) -> Keyring:
        """The keyring at ``file:<path>``: one KEK per line as 64 hex; blank lines and ``#`` comments
        are skipped, and the last KEK is current."""
        try:
            data = read_secret_bytes(ref)
        except SecretError as exc:
            raise KeyringError(str(exc)) from None
        path = ref.removeprefix("file:")
        lines: dict[bytes, int] = {}  # each KEK -> its line
        for number, line in enumerate(data.split(b"\n"), 1):
            line = line.strip()
            if not line or line.startswith(b"#"):
                continue
            if not _HEX_KEY.fullmatch(line):
                raise KeyringError(f"keyring {path} line {number}: expected 64 hex")
            kek = bytes.fromhex(line.decode("ascii"))
            if kek in lines:  # the last line would be current, but no longer the last listed
                raise KeyringError(f"keyring {path} line {number}: repeats line {lines[kek]}")
            lines[kek] = number
        if not lines:
            raise KeyringError(f"keyring {path} holds no key")
        return cls(list(lines))

    def wrap(self, key: bytes, kek_id: str | None = None) -> tuple[str, bytes]:
        """``key`` wrapped by the KEK ``kek_id`` (default the current one): (its id, 40 bytes)."""
        kek_id = kek_id or self.current
        kek = self._keks.get(kek_id)
        if kek is None:
            raise KeyringError(f"the keyring lacks {kek_id}")
        return kek_id, aes_key_wrap(kek, key)

    def unwrap(self, kek_id: str, wrapped: bytes) -> bytes:
        kek = self._keks.get(kek_id)
        if kek is None:
            raise KeyringError(f"the keyring lacks {kek_id}")
        try:
            return aes_key_unwrap(kek, bytes(wrapped))
        except InvalidUnwrap:
            raise KeyringError(f"a key wrapped by {kek_id} does not unwrap: it was altered") from None


def fingerprint(path: str | os.PathLike[str]) -> Keyring:
    """A keyring file's keyring, for its ids (oldest first) and current one (``ds keys fingerprint``: to
    check a vault copy)."""
    return Keyring.from_file(f"file:{os.fspath(path)}")


def chat_context(session_id: str, seq: int, role: str) -> str:
    """The context a chat message is sealed with: its session, position and role."""
    return f"{session_id}\x1f{seq}\x1f{role}"


def create_keyring(path: Path) -> None:
    """A keyring of one random KEK at ``path`` (0600), unless the file exists: developer mode's."""
    try:  # O_EXCL: two processes never write different keyrings; the loser keeps the winner's
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write((os.urandom(KEY_BYTES).hex() + "\n").encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())  # a lost KEK loses every key it wraps


class Sealer:
    """One run's data key. A sealed value is ``b"\\x01" || nonce || AES-256-GCM(subkey, nonce,
    plaintext, aad)``, with ``subkey = HKDF-SHA256(key, info = label || 0x1f || cycle || 0x1f ||
    run_id)`` (the cycle in decimal) and ``aad = b"deltasleuth/" || label || b"/v1" || 0x1f ||
    context``: a value moved to another row, position or role fails its context, to another cycle its
    subkey, and to another run its subkey too, even with the run's wrapped data key copied along."""

    __slots__ = ("_key", "_run_id", "_subkeys")

    def __init__(self, key: bytes, run_id: str) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError(f"a data key is {KEY_BYTES} bytes")
        self._key = key
        self._run_id = run_id
        self._subkeys: dict[tuple[str, int], AESGCM] = {}

    def __repr__(self) -> str:
        return f"Sealer({self._run_id!r})"

    def _aead(self, label: str, cycle: int) -> AESGCM:
        aead = self._subkeys.get((label, cycle))
        if aead is None:
            info = label.encode() + b"\x1f" + str(cycle).encode() + b"\x1f" + self._run_id.encode()
            subkey = HKDF(algorithm=SHA256(), length=KEY_BYTES, salt=None, info=info).derive(self._key)
            aead = self._subkeys[label, cycle] = AESGCM(subkey)
        return aead

    @staticmethod
    def _aad(label: str, context: str) -> bytes:
        return b"deltasleuth/" + label.encode() + b"/v1\x1f" + context.encode()

    def seal(self, label: str, cycle: int, context: str, plaintext: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return _SEALED + nonce + self._aead(label, cycle).encrypt(nonce, plaintext, self._aad(label, context))

    def open(self, label: str, cycle: int, context: str, sealed: bytes) -> bytes:
        sealed = bytes(sealed)
        if sealed[:1] != _SEALED or len(sealed) < 1 + NONCE_BYTES + TAG_BYTES:
            raise ValueError(_REFUSED)
        try:
            return self._aead(label, cycle).decrypt(
                sealed[1 : 1 + NONCE_BYTES], sealed[1 + NONCE_BYTES :], self._aad(label, context)
            )
        except InvalidTag:
            raise ValueError(_REFUSED) from None

    def seal_keys(self, keys: Mapping[str, str]) -> dict[str, bytes]:
        """Raw key -> masked key, as a run's keys are given, into masked key -> ``key_enc``."""
        return {masked: self.seal("run_key", 0, masked, raw.encode()) for raw, masked in keys.items()}

    def open_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """``row`` with its sealed columns opened: ``key_enc`` into ``key``, ``headers_enc`` into
        ``headers`` (JSON) and a chat message's ``sealed`` into ``content`` and ``tool_calls``."""
        opened = {name: v for name, v in row.items() if name not in ("key_enc", "headers_enc", "sealed")}
        if "key_enc" in row:
            opened["key"] = self.open("run_key", 0, row["key_masked"], row["key_enc"]).decode()
        if "headers_enc" in row:
            headers = self.open("headers", row["cycle"], row["obs_id"], row["headers_enc"])
            opened["headers"] = json.loads(headers)
        if "sealed" in row:
            context = chat_context(row["session_id"], row["seq"], row["role"])
            opened |= json.loads(self.open("chat", row["cycle"], context, row["sealed"]))
        return opened
