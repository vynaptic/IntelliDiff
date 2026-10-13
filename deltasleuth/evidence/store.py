"""Encrypted, content-addressed evidence (docs/mvp/RETENTION.md §2): format 1 blobs at
``blobs/hk1/<2 hex>/<64 hex>.dse`` (directories 0700, files 0600). The observation metadata lives in
the database (``ds.observations``).

A blob is named by ``HMAC-SHA256(address_key, body)``, so a listing cannot confirm a guessed body. Its
file is a 74-byte header (magic, evidence key id, the blob's own data key wrapped with AES-KW by that
evidence key, nonce) and the AES-256-GCM encryption of the body's zstd frame, authenticated with the
magic and the address digest: a blob moved to another name fails its tag. The newest evidence key
writes; every loaded one reads. Reads need no address key.

Evidence first: a blob is written, fsynced and verified before the caller records its observation, so
a crash leaves at most an unreferenced blob. ``obs_id`` is an HMAC keyed by ``id_key``: it is published
next to side, observed_at and attempts, and an unkeyed hash of a raw key could be brute-forced.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import zstandard
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import content_hash as hash_body
from deltasleuth.core.types import Observation
from deltasleuth.evidence.fsio import atomic_write, ensure_private_dir, fsync_dir
from deltasleuth.security.crypto import KEY_BYTES, NONCE_BYTES, TAG_BYTES, WRAPPED_BYTES, DataKey

MAGIC = b"DSE\x01"
_ID_END = len(MAGIC) + 18  # "E-" + 16 hex
_WRAPPED_END = _ID_END + WRAPPED_BYTES
HEADER_BYTES = _WRAPPED_END + NONCE_BYTES  # 74
_MIN_ID_KEY_BYTES = 16
_ADDRESS = re.compile(r"hk1:([0-9a-f]{64})")
_FILE = re.compile(r"([0-9a-f]{64})\.dse")
_EVIDENCE_KEY = re.compile(r"E-[0-9a-f]{16}")


class EvidenceError(Exception):
    """Base class of evidence store failures."""


class EvidenceCorruptedError(EvidenceError):
    """A blob does not match what was recorded (tampering or disk fault)."""


class EvidenceNotFoundError(EvidenceError, KeyError):
    """No blob exists for the requested content hash."""


class EvidenceIntegrityError(EvidenceError):
    """A put was refused: the data contradicts itself or could not be verified."""


class EvidenceKeyError(EvidenceError):
    """A blob or write needs a key the keyring cannot unwrap (docs/mvp/RETENTION.md §6.1)."""


def observation_id(obs: Observation, content: str | None, id_key: bytes) -> str:
    # Every field after the key has a fixed format without "|", so the text parses uniquely.
    identity = f"{content or ''}|{obs.key}|{obs.side.value}|{obs.observed_at}|{obs.attempts}"
    return "O-" + hmac.new(id_key, identity.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


class EvidenceStore:
    """Evidence-first blob store under ``root``. Every observation is accepted: failures are evidence.

    ``id_key`` (at least 16 bytes) keys the observation ids and ``address_key`` the blob names; a store
    is always written with the same keys. ``evidence_keys`` are oldest first: the last writes. A leftover
    ``blobs/sha256/`` (plaintext) stops the start unless ``migrating`` (``ds keys migrate``).
    """

    def __init__(
        self,
        root: str | Path,
        *,
        id_key: bytes,
        address_key: DataKey,
        evidence_keys: Sequence[DataKey],
        max_blob_bytes: int = jsonio.DEFAULT_MAX_BYTES,
        migrating: bool = False,
    ) -> None:
        if not isinstance(id_key, bytes) or len(id_key) < _MIN_ID_KEY_BYTES:
            raise ValueError(f"id_key must be at least {_MIN_ID_KEY_BYTES} bytes")
        if not evidence_keys or not all(_EVIDENCE_KEY.fullmatch(k.scope) for k in evidence_keys):
            raise ValueError("evidence keys are named E-<16 hex>, and a store needs one")
        root = Path(root)
        if not migrating and (root / "blobs" / "sha256").exists():
            raise EvidenceError(
                f"legacy plaintext remains in {root / 'blobs' / 'sha256'}: stop the app and run"
                " 'ds keys migrate' (docs/mvp/RETENTION.md §7.2)"
            )
        self._blobs = root / "blobs" / "hk1"
        self._id_key = id_key
        self._address_key = address_key
        self._keys = {key.scope: key for key in evidence_keys}
        self._writer = evidence_keys[-1]
        self._max_blob_bytes = max_blob_bytes
        self._lock = threading.Lock()  # puts may come from worker threads
        for directory in (root, self._blobs.parent, self._blobs):
            ensure_private_dir(directory)

    def address(self, body: bytes) -> str:
        """``hk1:`` + the hex HMAC of ``body`` under the address key."""
        key = self._address_key
        if key.key is None:
            raise EvidenceKeyError(f"the evidence address key needs {key.kek}, which the keyring lacks")
        return "hk1:" + hmac.new(key.key, body, hashlib.sha256).hexdigest()

    def put_blob(self, obs: Observation) -> Observation:
        """Store the body (the caller keeps the metadata); return ``obs`` with obs_id and its ``hk1:``
        content_hash. ``obs.content_hash`` may be None, the body's ``sha256:`` (a connector's) or its
        ``hk1:`` (a stored observation's)."""
        if obs.body is not None and len(obs.body) > self._max_blob_bytes:
            raise EvidenceIntegrityError(f"body exceeds the store's {self._max_blob_bytes}-byte limit")
        address = None if obs.body is None else self.address(obs.body)
        if obs.content_hash not in (None, address) and obs.content_hash != hash_body(obs.body):
            raise EvidenceIntegrityError("observation content_hash does not match its body")
        obs_id = observation_id(obs, address, self._id_key)
        if obs.obs_id not in (None, obs_id):
            raise EvidenceIntegrityError("observation carries an obs_id that does not match its identity")
        if obs.body is not None and address is not None:
            with self._lock:
                self._store_blob(obs.body, address)
        return dataclasses.replace(obs, content_hash=address, obs_id=obs_id)

    def put_body(self, body: bytes) -> str:
        """Store ``body`` without an observation (``ds keys migrate`` moves a legacy blob); its ``hk1:``
        address. A valid blob already there is kept."""
        if len(body) > self._max_blob_bytes:
            raise EvidenceIntegrityError(f"body exceeds the store's {self._max_blob_bytes}-byte limit")
        address = self.address(body)
        with self._lock:
            self._store_blob(body, address)
        return address

    def get_body(self, content_hash: str) -> bytes:
        """The verified body for ``content_hash``; raises if it is missing, corrupted or its key lacking."""
        try:
            data = self._blob_path(content_hash).read_bytes()
        except FileNotFoundError:
            raise EvidenceNotFoundError(content_hash) from None
        return self._decode(data, content_hash)

    def ensure(self, observations: Iterable[Observation]) -> int:
        """Write again the missing blobs of ``observations``, put earlier with their bodies still in
        memory (a drain may have deleted one meanwhile, docs/mvp/RETENTION.md §5.4); the number written."""
        written = 0
        for obs in observations:
            if obs.body is None or obs.content_hash is None or self._blob_path(obs.content_hash).exists():
                continue
            if self.address(obs.body) != obs.content_hash:
                raise EvidenceIntegrityError("observation content_hash does not match its body")
            with self._lock:
                self._store_blob(obs.body, obs.content_hash)
            written += 1
        return written

    def delete(self, address: str) -> int:
        """Unlink the blob at ``address`` and fsync its directory; the bytes freed (0 if it was absent)."""
        path = self._blob_path(address)
        with self._lock:
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                return 0
        fsync_dir(path.parent)
        return size

    def walk(self) -> Iterator[tuple[str, str | None, int]]:
        """Every blob file: its address, the evidence key id its header names (None when the header is
        not format 1) and its size in bytes. Temporary files and strangers are skipped."""
        for directory in self._blobs.iterdir():
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                match = _FILE.fullmatch(path.name)
                if match is None or match[1][:2] != directory.name:
                    continue
                try:
                    size = path.stat().st_size
                    with path.open("rb") as handle:
                        header = handle.read(_ID_END)
                except FileNotFoundError:  # deleted meanwhile
                    continue
                scope = header[len(MAGIC) :].decode("ascii", "replace")
                ok = header.startswith(MAGIC) and _EVIDENCE_KEY.fullmatch(scope)
                yield f"hk1:{match[1]}", scope if ok else None, size

    def age(self, address: str) -> float | None:
        """Seconds since the blob at ``address`` was written; None when it is absent."""
        try:
            return time.time() - self._blob_path(address).stat().st_mtime
        except FileNotFoundError:
            return None

    def delete_stale_temps(self, older_than: float) -> int:
        """Delete the temporary files of writes that never finished (a crash), unchanged for
        ``older_than`` seconds; how many."""
        deleted = 0
        for path in self._blobs.glob("*/.*.tmp"):
            with contextlib.suppress(FileNotFoundError):
                if time.time() - path.stat().st_mtime > older_than:
                    path.unlink()
                    deleted += 1
        return deleted

    def _blob_path(self, content_hash: str) -> Path:
        match = _ADDRESS.fullmatch(content_hash) if isinstance(content_hash, str) else None
        if match is None:
            raise ValueError("content hash must look like hk1:<64 lowercase hex>")
        return self._blobs / match[1][:2] / f"{match[1]}.dse"

    def _store_blob(self, body: bytes, address: str) -> None:
        path = self._blob_path(address)
        if self._blob_is_valid(path, address):
            return  # deduplicated: the same content is stored and intact
        ensure_private_dir(path.parent)
        atomic_write(path, self._encode(body, address))
        if not self._blob_is_valid(path, address):  # storage that cannot hold it must stop the run
            raise EvidenceIntegrityError("blob failed verification after write")

    def _blob_is_valid(self, path: Path, address: str) -> bool:
        try:
            self._decode(path.read_bytes(), address)
        except (FileNotFoundError, EvidenceCorruptedError, EvidenceKeyError):
            return False  # rewritten under the newest evidence key: the body is the same
        return True

    def _encode(self, body: bytes, address: str) -> bytes:
        writer = self._writer
        if writer.key is None:
            raise EvidenceKeyError(f"evidence key {writer.scope} needs {writer.kek}, which the keyring lacks")
        blob_key, nonce = os.urandom(KEY_BYTES), os.urandom(NONCE_BYTES)
        frame = zstandard.ZstdCompressor(level=3).compress(body)
        sealed = AESGCM(blob_key).encrypt(nonce, frame, MAGIC + bytes.fromhex(address[4:]))
        return MAGIC + writer.scope.encode("ascii") + aes_key_wrap(writer.key, blob_key) + nonce + sealed

    def _decode(self, data: bytes, address: str) -> bytes:
        if len(data) < HEADER_BYTES + TAG_BYTES or not data.startswith(MAGIC):
            raise EvidenceCorruptedError(f"blob {address} is not a format 1 blob")
        scope = data[len(MAGIC) : _ID_END].decode("ascii", "replace")
        key = self._keys.get(scope)
        if key is None:
            raise EvidenceKeyError(f"blob {address} needs {scope}, which ds.data_keys lacks")
        if key.key is None:
            raise EvidenceKeyError(f"blob {address} needs {scope}, whose KEK {key.kek} the keyring lacks")
        try:
            blob_key = aes_key_unwrap(key.key, data[_ID_END:_WRAPPED_END])
            frame = AESGCM(blob_key).decrypt(
                data[_WRAPPED_END:HEADER_BYTES], data[HEADER_BYTES:], MAGIC + bytes.fromhex(address[4:])
            )
        except (InvalidUnwrap, InvalidTag):
            raise EvidenceCorruptedError(f"blob {address} fails its authentication tag") from None
        try:
            # A declared size is required, so a tampered frame cannot make us allocate past the cap.
            if not 0 <= zstandard.frame_content_size(frame) <= self._max_blob_bytes:
                raise EvidenceCorruptedError(f"blob {address} declares an invalid size")
            return zstandard.ZstdDecompressor().decompress(frame)
        except zstandard.ZstdError:
            raise EvidenceCorruptedError(f"blob {address} is not a valid zstd frame") from None
