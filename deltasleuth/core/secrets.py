"""The one way DeltaSleuth reads a credential: a ``file:<path>`` reference.

Environment variables are never consulted (no ``$VAR`` or ``~`` expansion either), so a
credential can only come from a file an operator mounted on purpose. Errors name the problem,
never the content.
"""

from __future__ import annotations

import stat
from pathlib import Path

MAX_SECRET_BYTES = 64 * 1024


class SecretError(ValueError):
    """A secret reference is invalid or unreadable."""


def read_secret_bytes(ref: str) -> bytes:
    """Return the content of the secret file at ``ref`` as stored: a PEM key, which spans lines."""
    if not isinstance(ref, str) or not ref.startswith("file:") or ref == "file:" or "\x00" in ref:
        raise SecretError("secret references must be file:<path>; environment variables are never read")
    path = Path(ref.removeprefix("file:"))
    try:
        if not stat.S_ISREG(path.stat().st_mode):  # a FIFO or device could block forever
            raise SecretError(f"secret file {path} is not a regular file")
        with path.open("rb") as handle:
            data = handle.read(MAX_SECRET_BYTES + 1)
    except OSError as exc:
        raise SecretError(f"cannot read secret file {path}: {exc.strerror}") from None
    if len(data) > MAX_SECRET_BYTES:
        raise SecretError(f"secret file {path} is larger than {MAX_SECRET_BYTES} bytes")
    return data


def read_secret(ref: str) -> str:
    """Return the one-line secret stored at ``ref`` (one trailing newline is stripped)."""
    data = read_secret_bytes(ref)
    path = Path(ref.removeprefix("file:"))
    try:
        text = data.decode("utf-8").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError:
        raise SecretError(f"secret file {path} is not UTF-8 text") from None
    if not text or text.startswith("﻿") or text != text.strip():
        raise SecretError(f"secret file {path} is empty or has a BOM or surrounding whitespace")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise SecretError(f"secret file {path} contains line breaks or control characters")
    return text
