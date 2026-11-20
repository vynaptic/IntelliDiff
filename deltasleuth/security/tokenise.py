"""Keyed tokens and masked renditions of keys, findings, pointers and documents.

Tokens are deterministic, one-way HMACs separated by value class; the key sits in a local
``file:`` (0600), which this module reads. The tokeniser and the maskers are pure and live in
``core.masking``, which the engine's classification uses; this module names them for the callers outside
the engine. A class-only value (``date_of_birth``) shows as its class, never a linkable token; member
names that are data are tokens in pointers and views and ``[*]`` in patterns.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from deltasleuth.core.masking import (
    LABEL_CHARS,
    MIN_KEY_BYTES,
    TOKEN_CHARS,
    Tokeniser,
    TokeniserError,
    inline_stand_ins,
    is_stand_in,
    is_token,
    mask_document,
    mask_finding,
    mask_key,
    mask_member_key,
    mask_path_pattern,
    mask_pointer,
    mask_raw,
)
from deltasleuth.core.secrets import SecretError, read_secret

__all__ = [
    "LABEL_CHARS",
    "MIN_KEY_BYTES",
    "TOKEN_CHARS",
    "KeyFileError",
    "Tokeniser",
    "TokeniserError",
    "inline_stand_ins",
    "is_stand_in",
    "is_token",
    "load_or_create_key",
    "mask_document",
    "mask_finding",
    "mask_key",
    "mask_member_key",
    "mask_path_pattern",
    "mask_pointer",
    "mask_raw",
    "read_key",
]


class KeyFileError(TokeniserError):
    """The key file is missing, unreadable, malformed or accessible by group or others."""


def load_or_create_key(ref: str | os.PathLike[str]) -> bytes:
    """The hex key at ``file:<path>`` (or a path), created at random (mode 0600) if absent: developer
    mode's workspace key."""
    ref = ref if isinstance(ref, str) else f"file:{os.fspath(ref)}"
    if not ref.startswith("file:") or ref == "file:":
        raise KeyFileError("a tokeniser key is referenced as file:<path>")
    path = Path(ref.removeprefix("file:"))
    _create_key(path)
    if path.stat().st_mode & 0o077:
        raise KeyFileError(f"key file {path} must not be accessible by group or others (chmod 600)")
    return read_key(ref)


def read_key(ref: str) -> bytes:
    """The hex key at ``file:<path>``, never created: ``encryption.tokeniser_key_ref``'s."""
    path = Path(ref.removeprefix("file:"))
    try:
        key = bytes.fromhex(read_secret(ref))
    except SecretError as exc:
        raise KeyFileError(str(exc)) from None
    except ValueError:
        raise KeyFileError(f"key file {path} must hold the key as hexadecimal text") from None
    if len(key) < MIN_KEY_BYTES:
        raise KeyFileError(f"key file {path} holds fewer than {MIN_KEY_BYTES} bytes")
    return key


def _create_key(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:  # O_EXCL: two processes never write different keys; the loser reads the winner's
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write((secrets.token_bytes(MIN_KEY_BYTES).hex() + "\n").encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())  # a lost key orphans every token made with it
