"""Keyring, key ids and sealed values (docs/mvp/RETENTION.md §1, §3): errors name files, lines and ids,
never key material; a sealed value opens only with its label, cycle, context, run key and run id."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from deltasleuth.security.crypto import Keyring, KeyringError, Sealer, create_keyring, fingerprint, key_id

KEK_1, KEK_2 = "11" * 32, "a2" * 32
REFUSED = "sealed value does not authenticate"
RUN, OTHER_RUN = "R-" + "0" * 26, "R-" + "1" * 26


def kek_id(hex_key: str) -> str:
    return "K-" + hmac.new(bytes.fromhex(hex_key), b"deltasleuth/kek_id/v1", hashlib.sha256).hexdigest()[:16]


def keyring_file(tmp_path: Path, text: str) -> str:
    (tmp_path / "kek").write_text(text)
    return f"file:{tmp_path / 'kek'}"


class TestKeyring:
    def test_every_line_unwraps_and_the_last_wraps(self, tmp_path: Path) -> None:
        old = Keyring.from_file(keyring_file(tmp_path, f"{KEK_1}\n"))
        kek, wrapped = old.wrap(b"d" * 32)
        ref = keyring_file(tmp_path, f"# rotated 2026-09-27\n{KEK_1}\n\n  {KEK_2.upper()}  \r\n# next\n")
        keyring = Keyring.from_file(ref)
        assert (keyring.ids, keyring.current) == ((kek_id(KEK_1), kek_id(KEK_2)), kek_id(KEK_2))
        assert (kek, len(wrapped)) == (kek_id(KEK_1), 40) and keyring.unwrap(kek, wrapped) == b"d" * 32
        current, rewrapped = keyring.wrap(b"d" * 32)
        assert current == kek_id(KEK_2) and keyring.unwrap(current, rewrapped) == b"d" * 32
        assert (fingerprint(tmp_path / "kek").ids, fingerprint(tmp_path / "kek").current) == (
            keyring.ids,
            keyring.current,
        )
        assert old.wrap(b"d" * 32, kek_id(KEK_1))[0] == keyring.wrap(b"d" * 32, kek_id(KEK_1))[0] == kek
        with pytest.raises(KeyringError, match=f"^the keyring lacks {kek_id(KEK_2)}$"):
            old.wrap(b"d" * 32, kek_id(KEK_2))

    def test_ids_are_keyed_hashes(self) -> None:
        assert key_id("K-", bytes.fromhex(KEK_1)) == kek_id(KEK_1)
        assert key_id("E-", bytes.fromhex(KEK_1)) == "E-" + kek_id(KEK_1)[2:]
        assert hashlib.sha256(bytes.fromhex(KEK_1)).hexdigest()[:16] not in kek_id(KEK_1)

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            (f"{KEK_1}\n{KEK_2[:-1]}\n", "line 2: expected 64 hex"),
            (f"# comment\n{KEK_2}0\n", "line 2: expected 64 hex"),
            (f"{KEK_1} # the old one\n", "line 1: expected 64 hex"),
            ("zz" * 32 + "\n", "line 1: expected 64 hex"),
            ("\n# only comments\n", "holds no key"),
            (f"{KEK_1}\n{KEK_2}\n# restored\n{KEK_1}\n", "line 4: repeats line 1"),
        ],
    )
    def test_errors_name_the_file_and_line_never_the_content(
        self, tmp_path: Path, text: str, error: str
    ) -> None:
        with pytest.raises(KeyringError) as refused:
            Keyring.from_file(keyring_file(tmp_path, text))
        assert str(refused.value) == f"keyring {tmp_path / 'kek'} {error}"
        assert all(line.strip()[:8] not in str(refused.value) for line in text.splitlines() if line.strip())

    def test_an_unreadable_keyring_names_its_path(self, tmp_path: Path) -> None:
        with pytest.raises(KeyringError, match=f"cannot read secret file {tmp_path / 'absent'}"):
            Keyring.from_file(f"file:{tmp_path / 'absent'}")

    def test_unwrap_names_the_kek_it_lacks_or_the_alteration(self, tmp_path: Path) -> None:
        keyring = Keyring.from_file(keyring_file(tmp_path, KEK_2))
        with pytest.raises(KeyringError, match=f"^the keyring lacks {kek_id(KEK_1)}$"):
            keyring.unwrap(kek_id(KEK_1), b"\x00" * 40)
        kek, wrapped = keyring.wrap(b"d" * 32)
        with pytest.raises(KeyringError, match=f"a key wrapped by {kek} does not unwrap"):
            keyring.unwrap(kek, wrapped[:-1] + bytes([wrapped[-1] ^ 1]))

    def test_no_key_material_in_reprs(self, tmp_path: Path) -> None:
        keyring = Keyring.from_file(keyring_file(tmp_path, KEK_1))
        shown = repr(keyring) + repr(Sealer(bytes.fromhex(KEK_1), RUN))
        assert KEK_1 not in shown and "11" * 4 not in shown and kek_id(KEK_1) in shown

    def test_a_created_keyring_is_private_and_never_replaced(self, tmp_path: Path) -> None:
        create_keyring(tmp_path / "kek")
        first = (tmp_path / "kek").read_text()
        create_keyring(tmp_path / "kek")
        assert (tmp_path / "kek").read_text() == first
        assert stat.S_IMODE((tmp_path / "kek").stat().st_mode) == 0o600
        assert len(Keyring.from_file(f"file:{tmp_path / 'kek'}").ids) == 1


class TestSealer:
    def test_the_sealed_format(self) -> None:
        key = os.urandom(32)
        sealed = Sealer(key, RUN).seal("headers", 3, "O-0123456789abcdef", b'{"etag": "e"}')
        info = b"headers\x1f3\x1f" + RUN.encode()
        subkey = HKDF(algorithm=SHA256(), length=32, salt=None, info=info).derive(key)
        aad = b"deltasleuth/headers/v1\x1fO-0123456789abcdef"
        assert sealed[:1] == b"\x01" and len(sealed) == 1 + 12 + len(b'{"etag": "e"}') + 16
        assert AESGCM(subkey).decrypt(sealed[1:13], sealed[13:], aad) == b'{"etag": "e"}'

    def test_a_value_opens_only_where_it_was_sealed(self) -> None:
        sealer = Sealer(b"r" * 32, RUN)
        context = "S-1\x1f4\x1fuser"
        sealed = sealer.seal("chat", 2, context, b"the question")
        assert sealer.open("chat", 2, context, sealed) == b"the question"
        assert sealer.seal("chat", 2, context, b"the question") != sealed  # a random nonce each time
        elsewhere = [
            ("scan_after", 2, context, sealed),  # another label
            ("chat", 3, context, sealed),  # another cycle
            ("chat", 2, "S-1\x1f5\x1fuser", sealed),  # another position
            ("chat", 2, "S-1\x1f4\x1fassistant", sealed),  # another role
            ("chat", 2, context, sealed[:-1] + bytes([sealed[-1] ^ 1])),  # a flipped tag byte
            ("chat", 2, context, sealed[:5] + bytes([sealed[5] ^ 1]) + sealed[6:]),  # a flipped nonce byte
            ("chat", 2, context, b"\x02" + sealed[1:]),  # another format
            ("chat", 2, context, sealed[:20]),  # cut short
        ]
        for label, cycle, where, value in elsewhere:
            with pytest.raises(ValueError, match=f"^{REFUSED}$"):
                sealer.open(label, cycle, where, value)
        with pytest.raises(ValueError, match=f"^{REFUSED}$"):
            Sealer(b"s" * 32, RUN).open("chat", 2, context, sealed)  # another run's key
        with pytest.raises(ValueError, match=f"^{REFUSED}$"):  # moved to another run with its data key
            Sealer(b"r" * 32, OTHER_RUN).open("chat", 2, context, sealed)

    def test_raw_keys_seal_under_their_masked_key_and_rows_open(self) -> None:
        sealer = Sealer(b"r" * 32, RUN)
        masked = "tok:customer_id:v1:AAAA"
        sealed = sealer.seal_keys({"C00012027": masked, "ORG-1": "ORG-1"})
        assert list(sealed) == [masked, "ORG-1"] and b"C00012027" not in sealed[masked]
        row = {"key_masked": masked, "key_enc": sealed[masked], "cycle": 1}
        assert sealer.open_row(row) == {"key_masked": masked, "cycle": 1, "key": "C00012027"}
        with pytest.raises(ValueError, match=REFUSED):  # moved to another key's row
            sealer.open_row(row | {"key_enc": sealed["ORG-1"]})
        with pytest.raises(ValueError, match=REFUSED):  # moved to another run, its wrapped data key too
            Sealer(b"r" * 32, OTHER_RUN).open_row(row)

    def test_rows_open_their_headers_and_chat(self) -> None:
        sealer = Sealer(b"r" * 32, RUN)
        headers = sealer.seal("headers", 2, "O-1", json.dumps({"etag": "e"}).encode())
        payload = json.dumps({"content": "why?", "tool_calls": None}).encode()
        chat = sealer.seal("chat", 2, "S-1\x1f0\x1fuser", payload)
        observation = {"obs_id": "O-1", "cycle": 2, "headers_enc": headers}
        message = {"session_id": "S-1", "seq": 0, "role": "user", "cycle": 2, "sealed": chat}
        assert sealer.open_row(observation) == {"obs_id": "O-1", "cycle": 2, "headers": {"etag": "e"}}
        assert sealer.open_row(message) == {
            "session_id": "S-1", "seq": 0, "role": "user", "cycle": 2, "content": "why?", "tool_calls": None,
        }  # fmt: skip
        with pytest.raises(ValueError, match=REFUSED):
            sealer.open_row(observation | {"obs_id": "O-2"})

    @pytest.mark.parametrize("bad", [b"", b"short", b"k" * 33])
    def test_a_data_key_is_32_bytes(self, bad: bytes) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            Sealer(bad, RUN)
