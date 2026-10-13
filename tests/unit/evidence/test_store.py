"""Evidence store: encrypted format 1 blobs at keyed addresses, evidence first, dedup, verification,
keyed ids, private modes, and the keys a read or a write needs (docs/mvp/RETENTION.md §2)."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
import zstandard
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap

from deltasleuth.core.canonical.encoding import content_hash, sha256_hex
from deltasleuth.core.types import Observation, Side
from deltasleuth.evidence import store as store_module
from deltasleuth.evidence.store import (
    MAGIC,
    EvidenceCorruptedError,
    EvidenceError,
    EvidenceIntegrityError,
    EvidenceKeyError,
    EvidenceNotFoundError,
    EvidenceStore,
    observation_id,
)
from deltasleuth.security.crypto import DataKey, key_id
from tests.conftest import TEST_ADDRESS_KEY, TEST_EVIDENCE_KEY, evidence_store

KEY_A, KEY_B = b"a" * 32, b"b" * 32
BODY = b'{"id": 1, "name": "Planted Marker 7f3e"}'
MARKER = b"Planted Marker 7f3e"
AT = "2026-09-24T08:00:00.000000Z"
NEWER = DataKey(key_id("E-", b"n" * 32), "K-00000000000000ff", b"n" * 32)


def make_obs(body: bytes | None = BODY, **overrides: object) -> Observation:
    fields: dict[str, object] = {
        "key": "K1",
        "side": Side.A,
        "http_status": 200 if body is not None else None,
        "body": body,
        "observed_at": AT,
        "latency_ms": 12.5,
        "headers": {"content-type": "application/json"},
        "request_url": "http://svc.example/things/K1",
    }
    return Observation(**{**fields, **overrides})  # type: ignore[arg-type]


def failed(key: str = "C00012027", body: bytes | None = None, **overrides: object) -> Observation:
    """A failed read, bodiless by default: the observation an attacker could brute-force best."""
    fields = {
        "key": key,
        "side": Side.B,
        "error_class": "timeout",
        "error_detail": "TimeoutError",
        "attempts": 3,
    }
    return make_obs(body, **{"http_status": None, **fields, **overrides})


def address(body: bytes = BODY, key: bytes = b"a" * 32) -> str:
    return "hk1:" + hmac.new(key, body, hashlib.sha256).hexdigest()


def keyed_id(material: str, key: bytes = KEY_A) -> str:
    return "O-" + hmac.new(key, material.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def blob_path(root: Path, body: bytes = BODY) -> Path:
    digest = address(body)[4:]
    return root / "blobs" / "hk1" / digest[:2] / f"{digest}.dse"


def sealed_frame(frame: bytes, body: bytes = BODY, key: DataKey = TEST_EVIDENCE_KEY) -> bytes:
    """A blob whose tag holds for ``frame``, as only a holder of the evidence key can write one."""
    assert key.key is not None
    blob_key, nonce = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(blob_key).encrypt(nonce, frame, MAGIC + bytes.fromhex(address(body)[4:]))
    return MAGIC + key.scope.encode() + aes_key_wrap(key.key, blob_key) + nonce + ciphertext


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return evidence_store(tmp_path, id_key=KEY_A)


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    yield
    os.umask(previous)


class TestPutAndGet:
    def test_put_blob_sets_identity_and_writes_the_blob(self, store: EvidenceStore, tmp_path: Path) -> None:
        stored = store.put_blob(make_obs())
        assert stored.content_hash == address()
        assert stored.obs_id == keyed_id(f"{address()}|K1|a|{AT}|1")
        assert blob_path(tmp_path).exists()
        assert evidence_store(tmp_path, id_key=KEY_A).get_body(address()) == BODY

    def test_the_blob_is_format_1(self, store: EvidenceStore, tmp_path: Path) -> None:
        store.put_blob(make_obs(b"x" * 10_000))
        data = blob_path(tmp_path, b"x" * 10_000).read_bytes()
        assert (data[:4], data[4:22]) == (MAGIC, TEST_EVIDENCE_KEY.scope.encode())
        assert TEST_EVIDENCE_KEY.key is not None
        blob_key = aes_key_unwrap(TEST_EVIDENCE_KEY.key, data[22:62])
        aad = MAGIC + bytes.fromhex(address(b"x" * 10_000)[4:])
        frame = AESGCM(blob_key).decrypt(data[62:74], data[74:], aad)
        assert len(data) == 74 + len(frame) + 16 < 1000  # the header, the zstd frame and the tag
        assert zstandard.ZstdDecompressor().decompress(frame) == b"x" * 10_000

    def test_neither_the_blob_nor_its_name_shows_the_body(self, store: EvidenceStore, tmp_path: Path) -> None:
        store.put_blob(make_obs(MARKER * 50))
        [blob] = (tmp_path / "blobs").rglob("*.dse")
        assert MARKER not in blob.read_bytes() and sha256_hex(MARKER * 50) not in str(blob)
        assert zstandard.ZstdCompressor(level=3).compress(MARKER * 50) not in blob.read_bytes()

    def test_bodiless_and_empty_bodies_are_evidence(self, store: EvidenceStore) -> None:
        stored = store.put_blob(make_obs(None, error_class="timeout", attempts=3))
        assert stored.content_hash is None and stored.obs_id == keyed_id(f"|K1|a|{AT}|3")
        assert store.get_body(store.put_blob(make_obs(b"")).content_hash or "") == b""

    def test_identity_is_unambiguous(self) -> None:
        a, b = make_obs(key="k|a|x", side=Side.A), make_obs(key="k", side=Side.A)
        assert observation_id(a, "c", KEY_A) != observation_id(b, "c", KEY_A)

    def test_same_content_one_blob(self, store: EvidenceStore, tmp_path: Path) -> None:
        first, second = store.put_blob(make_obs(key="K1")), store.put_blob(make_obs(key="K2", side=Side.B))
        assert first.content_hash == second.content_hash and first.obs_id != second.obs_id
        written = blob_path(tmp_path).read_bytes()
        assert store.put_blob(first) == first  # a stored observation re-puts with its hk1: hash
        assert blob_path(tmp_path).read_bytes() == written  # deduplicated, not rewritten
        assert len(list((tmp_path / "blobs").rglob("*.dse"))) == 1
        assert list(tmp_path.rglob("*.tmp")) == []

    @pytest.mark.parametrize("given", [None, content_hash(BODY), address()], ids=["none", "sha256", "hk1"])
    def test_put_accepts_no_hash_or_the_body_s_own(self, store: EvidenceStore, given: str | None) -> None:
        assert store.put_blob(make_obs(content_hash=given)).content_hash == address()

    def test_another_address_key_names_the_body_differently(self, tmp_path: Path) -> None:
        other = DataKey("address", TEST_ADDRESS_KEY.kek, b"z" * 32)
        stored = evidence_store(tmp_path / "other", address_key=other).put_blob(make_obs())
        assert stored.content_hash == address(key=b"z" * 32) != address()

    def test_open_ignores_a_stray_observations_log(self, tmp_path: Path) -> None:
        # A P0-era log, even a damaged one, is not the store's business any more.
        (tmp_path / "observations.jsonl").write_text('{"obs_id": "O-1", "key": "K"\n')
        store = evidence_store(tmp_path)
        assert store.get_body(store.put_blob(make_obs()).content_hash or "") == BODY


class TestIntegrity:
    @pytest.mark.parametrize(
        "wrong",
        [
            {"content_hash": content_hash(b"other")},
            {"content_hash": address(b"other")},
            {"content_hash": address(key=b"z" * 32)},  # the body's hk1: under another address key
            {"content_hash": "md5:abc"},
            {"obs_id": "O-0000000000000000"},
        ],
    )
    def test_self_contradicting_observation_refused(self, store: EvidenceStore, wrong: dict) -> None:
        with pytest.raises(EvidenceIntegrityError):
            store.put_blob(make_obs(**wrong))

    def test_a_bodiless_observation_carries_no_hash(self, store: EvidenceStore) -> None:
        for given in (content_hash(b""), address(b"")):
            with pytest.raises(EvidenceIntegrityError):
                store.put_blob(make_obs(None, content_hash=given))

    def test_oversized_body_refused_before_writing(self, tmp_path: Path) -> None:
        with pytest.raises(EvidenceIntegrityError):
            evidence_store(tmp_path, max_blob_bytes=10).put_blob(make_obs(b"x" * 11))
        assert list((tmp_path / "blobs").rglob("*.dse")) == []

    @pytest.mark.parametrize(
        ("offset", "region"),
        [(0, "magic"), (30, "wrapped key"), (65, "nonce"), (80, "ciphertext"), (-1, "tag")],
    )
    def test_a_flipped_byte_reads_corrupted(
        self, store: EvidenceStore, tmp_path: Path, offset: int, region: str
    ) -> None:
        store.put_blob(make_obs())
        data = bytearray(blob_path(tmp_path).read_bytes())
        data[offset] ^= 0x01
        blob_path(tmp_path).write_bytes(bytes(data))
        with pytest.raises(EvidenceCorruptedError):
            store.get_body(address())

    def test_a_moved_blob_fails_its_tag(self, store: EvidenceStore, tmp_path: Path) -> None:
        store.put_blob(make_obs())
        store.put_blob(make_obs(b'{"id": 2}'))
        blob_path(tmp_path, b'{"id": 2}').write_bytes(blob_path(tmp_path).read_bytes())
        with pytest.raises(EvidenceCorruptedError, match="fails its authentication tag"):
            store.get_body(address(b'{"id": 2}'))

    def test_corrupted_blobs_are_detected_on_read_and_repaired_by_put(
        self, store: EvidenceStore, tmp_path: Path
    ) -> None:
        stored = store.put_blob(make_obs())
        blobs = {  # garbage, a cut header, frames the tag holds for: garbage, one without a declared size
            "not a format 1 blob": b"garbage",
            "fails its authentication tag": blob_path(tmp_path).read_bytes()[:-20] + b"\x00" * 4,
            "not a valid zstd": sealed_frame(b"not a frame"),
            "size": sealed_frame(zstandard.ZstdCompressor(write_content_size=False).compress(BODY)),
        }
        for message, data in blobs.items():
            blob_path(tmp_path).write_bytes(data)
            with pytest.raises(EvidenceCorruptedError, match=message):
                store.get_body(stored.content_hash or "")
        store.put_blob(make_obs(key="K2"))
        assert store.get_body(stored.content_hash or "") == BODY

    def test_missing_or_malformed_address(self, store: EvidenceStore) -> None:
        with pytest.raises(EvidenceNotFoundError):
            store.get_body(address(b"never stored"))
        for bad in ("hk1:../../etc/passwd", content_hash(BODY), "md5:abc", "hk1:" + "A" * 64, ""):
            with pytest.raises(ValueError):
                store.get_body(bad or "")

    def test_verification_failure_after_write_stops_put(
        self, store: EvidenceStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def truncating_write(path: Path, data: bytes) -> None:
            path.write_bytes(data[:-4])  # storage that truncates what it is given

        monkeypatch.setattr(store_module, "atomic_write", truncating_write)
        with pytest.raises(EvidenceIntegrityError):
            store.put_blob(make_obs())


class TestKeys:
    """The newest evidence key writes and every loaded one reads; a lacking key fails what needs it."""

    def test_the_newest_evidence_key_writes_and_older_ones_read(self, tmp_path: Path) -> None:
        old = evidence_store(tmp_path).put_blob(make_obs(b'{"old": 1}'))
        rotated = evidence_store(tmp_path, evidence_keys=[TEST_EVIDENCE_KEY, NEWER])
        new = rotated.put_blob(make_obs(b'{"new": 1}'))
        assert rotated.get_body(old.content_hash or "") == b'{"old": 1}'
        assert {key for _, key, _ in rotated.walk()} == {TEST_EVIDENCE_KEY.scope, NEWER.scope}
        with pytest.raises(EvidenceKeyError, match=f"needs {NEWER.scope}, which ds.data_keys lacks"):
            evidence_store(tmp_path).get_body(new.content_hash or "")

    def test_a_blob_whose_kek_is_missing_names_both_ids(self, tmp_path: Path) -> None:
        stored = evidence_store(tmp_path).put_blob(make_obs())
        lacking = DataKey(TEST_EVIDENCE_KEY.scope, "K-00000000000000aa")
        store = evidence_store(tmp_path, evidence_keys=[lacking, NEWER])
        with pytest.raises(EvidenceKeyError) as refused:
            store.get_body(stored.content_hash or "")
        assert str(refused.value) == (
            f"blob {address()} needs {TEST_EVIDENCE_KEY.scope}, whose KEK K-00000000000000aa the keyring"
            " lacks"
        )
        assert store.put_blob(make_obs()) == stored  # a put rewrites it under the newest key
        assert store.get_body(address()) == BODY

    def test_writes_need_the_address_key_and_the_newest_evidence_key(self, tmp_path: Path) -> None:
        evidence_store(tmp_path).put_blob(make_obs())
        no_address = evidence_store(tmp_path, address_key=DataKey("address", "K-00000000000000aa"))
        with pytest.raises(EvidenceKeyError, match="address key needs K-00000000000000aa, which the keyring"):
            no_address.put_blob(make_obs(b"{}"))
        assert no_address.get_body(address()) == BODY  # reads need no address key
        lacking = DataKey(NEWER.scope, NEWER.kek)
        no_writer = evidence_store(tmp_path, evidence_keys=[TEST_EVIDENCE_KEY, lacking])
        with pytest.raises(EvidenceKeyError, match=f"evidence key {NEWER.scope} needs {NEWER.kek}"):
            no_writer.put_blob(make_obs(b"{}"))

    @pytest.mark.parametrize("keys", [[], [DataKey("E-short", "K-00000000000000aa", b"e" * 32)]])
    def test_the_store_needs_well_named_evidence_keys(self, tmp_path: Path, keys: list[DataKey]) -> None:
        with pytest.raises(ValueError, match="evidence keys"):
            evidence_store(tmp_path, evidence_keys=keys)


class TestEnsureDeleteWalk:
    def test_ensure_writes_back_the_missing_blobs_of_bodies_in_memory(
        self, store: EvidenceStore, tmp_path: Path
    ) -> None:
        reads = [store.put_blob(make_obs(body)) for body in (BODY, b'{"id": 2}', None)]
        assert store.ensure(reads) == 0
        assert store.delete(address()) > 0 and not blob_path(tmp_path).exists()
        assert store.ensure(reads + reads) == 1  # written once, then present
        assert store.get_body(address()) == BODY

    def test_ensure_refuses_a_body_that_is_not_its_address_s(self, store: EvidenceStore) -> None:
        stored = store.put_blob(make_obs())
        store.delete(address())
        forged = Observation(**{**stored.__dict__, "body": b"another body"})
        with pytest.raises(EvidenceIntegrityError):
            store.ensure([forged])

    def test_delete_returns_the_bytes_freed(self, store: EvidenceStore, tmp_path: Path) -> None:
        store.put_blob(make_obs())
        size = blob_path(tmp_path).stat().st_size
        assert (store.delete(address()), store.delete(address())) == (size, 0)
        with pytest.raises(EvidenceNotFoundError):
            store.get_body(address())

    def test_walk_lists_blobs_with_their_key_and_size(self, store: EvidenceStore, tmp_path: Path) -> None:
        store.put_blob(make_obs())
        store.put_blob(make_obs(b"{}"))
        (blob_path(tmp_path).parent / f".{blob_path(tmp_path).name}.x1.tmp").write_bytes(b"partial")
        (tmp_path / "blobs" / "hk1" / "stray.txt").write_text("not a blob directory")
        misfiled = tmp_path / "blobs" / "hk1" / "00" / f"{'f' * 64}.dse"
        misfiled.parent.mkdir(exist_ok=True)
        misfiled.write_bytes(b"garbage")
        broken = blob_path(tmp_path, b"broken")
        broken.parent.mkdir(exist_ok=True)
        broken.write_bytes(b"garbage")
        assert sorted(store.walk()) == sorted(
            [
                (address(), TEST_EVIDENCE_KEY.scope, blob_path(tmp_path).stat().st_size),
                (address(b"{}"), TEST_EVIDENCE_KEY.scope, blob_path(tmp_path, b"{}").stat().st_size),
                (address(b"broken"), None, 7),
            ]
        )


class TestLegacy:
    def test_a_leftover_sha256_directory_stops_the_start(self, tmp_path: Path) -> None:
        (tmp_path / "blobs" / "sha256").mkdir(parents=True)
        with pytest.raises(EvidenceError) as refused:
            evidence_store(tmp_path)
        assert str(refused.value) == (
            f"legacy plaintext remains in {tmp_path / 'blobs' / 'sha256'}: stop the app and run"
            " 'ds keys migrate' (docs/mvp/RETENTION.md §7.2)"
        )
        assert evidence_store(tmp_path, migrating=True).put_blob(make_obs()).content_hash == address()


class TestKeyedIdentity:
    """Observation ids are published next to side, observed_at and attempts."""

    def test_obs_id_not_derivable_from_published_fields(self, tmp_path: Path) -> None:
        # With an unkeyed hash the raw key behind a token was recoverable by trying the key pattern's space.
        obs = failed()
        stored = evidence_store(tmp_path / "one", id_key=KEY_A).put_blob(obs)
        unkeyed = "O-" + sha256_hex(f"|{obs.key}|b|{obs.observed_at}|{obs.attempts}")[:16]
        assert stored.obs_id is not None and stored.obs_id != unkeyed
        assert evidence_store(tmp_path / "two", id_key=KEY_B).put_blob(obs).obs_id != stored.obs_id
        assert evidence_store(tmp_path / "three", id_key=KEY_A).put_blob(obs).obs_id == stored.obs_id

    @pytest.mark.parametrize("bad", [b"", b"short", "a string key of 32 characters!!!", None])
    def test_weak_id_key_rejected(self, tmp_path: Path, bad: object) -> None:
        with pytest.raises(ValueError, match="id_key"):
            evidence_store(tmp_path, id_key=bad)


class TestPrivateModes:
    """The blobs hold raw payloads, encrypted."""

    def test_blobs_and_dirs_are_private_under_umask_022(self, tmp_path: Path, umask_022: None) -> None:
        root = tmp_path / "evidence"
        evidence_store(root).put_blob(failed(body=b'{"id": "C00012027"}', http_status=503))
        blob = next((root / "blobs").rglob("*.dse"))
        assert mode(blob) == 0o600
        for directory in (root, root / "blobs", root / "blobs" / "hk1", blob.parent):
            assert mode(directory) == 0o700, directory

    def test_existing_wide_store_is_tightened(self, tmp_path: Path, umask_022: None) -> None:
        root = tmp_path / "evidence"
        evidence_store(root)
        root.chmod(0o755)
        evidence_store(root)
        assert mode(root) == 0o700
