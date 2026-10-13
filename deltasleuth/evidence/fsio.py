"""Durable, private file writes: owner-only modes, and atomic replace so no reader sees half a file."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

PRIVATE_DIR_MODE = 0o700


def ensure_private_dir(directory: Path) -> None:
    """Create ``directory`` 0700 (parents with default modes), or tighten it if it is wider."""
    directory.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        directory.chmod(PRIVATE_DIR_MODE)


def fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` so that after a crash ``path`` is either complete or absent (created 0600)."""
    # ".<name>.<random>.tmp": scanners looking for "*.json" or "*.dse" never see a partial file.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    fsync_dir(path.parent)  # the rename itself must survive a crash
