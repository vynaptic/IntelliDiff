"""What a file side reads, without DuckDB: the
roots' places, the path's grammar, the expansion, the arrival, the pins, the threads and the spill."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import errno
import fcntl
import functools
import gzip
import hashlib
import inspect
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deltasleuth.config import FilesSettings, Settings
from deltasleuth.connectors.tables import FilesChanged, NotReady, TargetDenied, TargetUnreachable, manifest
from deltasleuth.core.spec.targets import TargetRegistry
from deltasleuth.core.types import DatabaseTarget, Target
from deltasleuth.runner.worker import _Worker
from tests.support import parquet_tiny

FILES = FilesSettings()
UID = os.getuid()


def _target(root: Path, **declared: Any) -> Target:
    """A fixture target: settle_s 0 and allow_writable, as a test's directory is writable."""
    return Target("legacy", "", root=str(root), **({"settle_s": 0, "allow_writable": True} | declared))


def _write(root: Path, *names: str, data: bytes = b"id\n1\n") -> None:
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    made = tmp_path.resolve() / "exports"
    made.mkdir()
    return made


async def _paths(root: Path, path: str, **options: Any) -> list[str]:
    found = await manifest.expand(
        _target(root), path, "side a", max_files=options.pop("max_files", 1000), **options
    )
    return [f.path for f in found]


async def _refusal(root: Path, path: str, target: Target | None = None, **options: Any) -> str:
    with pytest.raises(ValueError) as refused:
        await manifest.pin(target or _target(root), path, "side a", FILES, **options)
    return str(refused.value)


# -- expansion ------------------------------------------------------------------------------------


async def test_one_file_a_glob_and_a_directory(root: Path) -> None:
    _write(root, "customers/current/c-2.csv", "customers/current/c-10.csv", "customers/current/C-1.csv",
           "customers/current/notes.txt", "customers/old/c-0.csv")  # fmt: skip
    assert await _paths(root, "customers/current/c-2.csv") == ["customers/current/c-2.csv"]
    # Path order is the relative paths' UTF-8 bytes: upper case before lower, "1" before "2".
    assert await _paths(root, "customers/current/*.csv") == [
        "customers/current/C-1.csv", "customers/current/c-10.csv", "customers/current/c-2.csv",
    ]  # fmt: skip
    assert await _paths(root, "customers/*/c-0.csv") == ["customers/old/c-0.csv"]
    assert await _paths(root, "customers/") == [
        "customers/current/C-1.csv", "customers/current/c-10.csv", "customers/current/c-2.csv",
        "customers/current/notes.txt", "customers/old/c-0.csv",
    ]  # fmt: skip


async def test_hidden_names_are_left_out_and_temporary_directories_not_entered(root: Path) -> None:
    _write(root, "sales/part-0.parquet", "sales/.part-0.parquet.crc", "sales/_SUCCESS",
           "sales/_temporary/0/part-1.parquet", "sales/.staging/part-2.parquet")  # fmt: skip
    assert await _paths(root, "sales/") == ["sales/part-0.parquet"]
    assert await _paths(root, "sales/*.parquet") == ["sales/part-0.parquet"]  # a dot name is never matched


async def test_a_directory_read_goes_eight_deep(root: Path) -> None:
    _write(root, "deep/" + "/".join(f"d{n}" for n in range(8)) + "/f.csv")
    assert len(await _paths(root, "deep/")) == 1
    _write(root, "deeper/" + "/".join(f"d{n}" for n in range(9)) + "/f.csv")
    assert await _refusal(root, "deeper/") == "side a: the path holds directories more than 8 deep"


async def test_no_file_is_not_ready(root: Path) -> None:
    _write(root, "customers/current/c.txt")
    for path in ("customers/current/*.csv", "customers/missing/*.csv", "customers/current/c.csv", "none/"):
        with pytest.raises(NotReady) as absent:
            await manifest.pin(_target(root), path, "side a", FILES)
        assert (absent.value.code, str(absent.value)) == (
            "export_absent", f"side a: no file matches {path} under target legacy"
        )  # fmt: skip


async def test_too_many_files_at_the_settings_limit(root: Path) -> None:
    _write(root, *(f"many/f{n}.csv" for n in range(3)))
    limited = FilesSettings(max_files=2)
    with pytest.raises(ValueError, match=r"matches 3 files: a side reads at most 2 \(files.max_files\)"):
        await manifest.pin(_target(root), "many/", "side a", limited)


LINK = "is a symbolic link or is reached through one: links are never followed"


async def test_links_are_refused_wherever_they_stand(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path.resolve() / "elsewhere"
    _write(elsewhere, "secret.csv")
    _write(root, "a/1.csv", "a/3.csv")
    (root / "a" / "2.csv").symlink_to(elsewhere / "secret.csv")  # a file
    assert await _refusal(root, "a/*.csv") == f"side a: file 2 of 3 (in path order) {LINK}"
    (root / "b").symlink_to(elsewhere)  # a directory of the path's fixed part
    assert await _refusal(root, "b/*.csv") == f"side a: file 1 of 1 (in path order) {LINK}"
    _write(root, "c/x/1.csv")
    (root / "c" / "y").symlink_to(elsewhere)  # a directory a directory read meets
    assert await _refusal(root, "c/") == f"side a: file 2 of 2 (in path order) {LINK}"
    linked = tmp_path.resolve() / "linked"
    linked.symlink_to(root)  # the root itself
    assert await _refusal(root, "a/1.csv", _target(linked)) == (
        "side a: the directory of target legacy is, or is reached through, a symbolic link: declare its real"
        " path as root"
    )


async def test_what_is_no_regular_file_or_has_a_name_outside_the_alphabet_is_refused(root: Path) -> None:
    _write(root, "f/1.csv", "n/1.csv", "n/2 'x'.csv", "u/1.csv", "u/2.csv")
    os.mkfifo(root / "f" / "2.csv")
    assert await _refusal(root, "f/*.csv") == "side a: file 2 of 2 (in path order) is not a regular file"
    assert await _refusal(root, "n/") == (
        "side a: file 2 of 2 (in path order) has a name outside letters, digits, space and . _ - = + @ , ( ):"
        " rename it"
    )
    (root / "u" / "2.csv").chmod(0)
    try:
        assert await _refusal(root, "u/*.csv") == (
            f"side a: file 2 of 2 (in path order) may not be read by DeltaSleuth (uid {UID})"
        )
    finally:
        (root / "u" / "2.csv").chmod(0o644)


@pytest.mark.parametrize("name", ["*", "**", "?", "[a]", "a'b"])
async def test_a_directory_named_as_a_pattern_is_refused_and_never_entered(root: Path, name: str) -> None:
    """Under a globbed directory segment a directory whose name is outside the alphabet refuses the
    plan, as below a directory read. Entered, its file was pinned under a path DuckDB expands, which then read
    files that were never listed, pinned or hashed: a hidden directory's, another target's through a link."""
    _write(root, "dt=1/part.csv", ".staging/part.csv")
    pinned = await manifest.pin(_target(root), "*/part.csv", "side a", FILES)
    assert pinned.files == ("dt=1/part.csv",)
    _write(root, f"{name}/part.csv")
    assert await _refusal(root, "*/part.csv") == (
        "side a: file 1 of 2 (in path order) has a name outside letters, digits, space and . _ - = + @ , ( ):"
        " rename it"
    )
    with pytest.raises(FilesChanged) as changed:  # made after the run was created, it fails the next check
        await manifest.verify(_target(root), "*/part.csv", "a", pinned.files, pinned.pins)
    assert str(changed.value) == "side a: 2 files match now, 1 when the run was created"


async def test_a_root_that_is_not_there_or_writable(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(root, "a.csv")
    assert await _refusal(root, "a.csv", _target(root / "gone")) == (
        f"side a: the directory of target legacy is not there, or DeltaSleuth (uid {UID}) may not list it: is"
        " it mounted into this container?"
    )
    writable = _target(root, allow_writable=False)
    assert await _refusal(root, "a.csv", writable) == (
        "side a: the directory of target legacy is mounted writable: mount it read-only (:ro), or declare"
        " allow_writable: true on the target"
    )
    statvfs = os.statvfs

    def read_only(path: Any) -> Any:
        found = statvfs(path)
        return SimpleNamespace(
            f_flag=found.f_flag | os.ST_RDONLY, f_bavail=found.f_bavail, f_frsize=found.f_frsize
        )

    monkeypatch.setattr(os, "statvfs", read_only)
    monkeypatch.setattr(os, "fstatvfs", read_only)
    pinned = await manifest.pin(writable, "a.csv", "side a", FILES)
    assert pinned.mount == "read-only"
    assert (await manifest.pin(_target(root), "a.csv", "target legacy", FILES)).mount == "read-only"


async def test_the_mount_of_each_file_is_judged(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A root on a read-only mount (the image's own file system, with the exports mounted below it)
    says nothing of the mounts its files lie on: each file's is judged by its descriptor. One on a mount
    DeltaSleuth could write to refuses the plan, or with allow_writable is what the plan records, so the
    certificate says it."""
    _write(root, "e/one/a.csv", "e/two/b.csv")
    statvfs, fstatvfs, writable = os.statvfs, os.fstatvfs, os.stat(root / "e" / "two" / "b.csv").st_ino

    def read_only(found: Any) -> Any:
        return SimpleNamespace(
            f_flag=found.f_flag | os.ST_RDONLY, f_bavail=found.f_bavail, f_frsize=found.f_frsize
        )

    def by_descriptor(fd: int) -> Any:  # every mount read-only but the one that holds two/b.csv
        return fstatvfs(fd) if os.fstat(fd).st_ino == writable else read_only(fstatvfs(fd))

    monkeypatch.setattr(os, "statvfs", lambda path: read_only(statvfs(path)))
    monkeypatch.setattr(os, "fstatvfs", by_descriptor)
    strict = _target(root, allow_writable=False)
    assert manifest._rooted(str(root)) == "read-only"  # all that was judged
    assert (await manifest.pin(strict, "e/one/", "side a", FILES)).mount == "read-only"
    assert await _refusal(root, "e/", strict) == (
        "side a: file 2 of 2 lies on a mount DeltaSleuth could write to: mount it read-only (:ro), or declare"
        " allow_writable: true on the target"
    )
    assert (await manifest.pin(_target(root), "e/", "side a", FILES)).mount == "writable"


async def test_a_mount_that_answers_with_an_error(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing or a stat the mount answers with an error (a stale handle, EIO) refuses the run while it
    is planned, and afterwards an outage (TargetUnreachable: paused, read again), never a failed run."""
    _write(root, "e/a.csv")
    pinned = await manifest.pin(_target(root), "e/", "side a", FILES)
    lstat = os.lstat

    def stale(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path).endswith("/e"):
            raise OSError(errno.ESTALE, "Stale file handle")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", stale)
    assert await _refusal(root, "e/") == (
        f"side a: the directory of target legacy is not there, or DeltaSleuth (uid {UID}) may not list it: is"
        " it mounted into this container?"
    )
    with pytest.raises(TargetUnreachable) as outage:
        await manifest.verify(_target(root), "e/", "a", pinned.files, pinned.pins)
    assert str(outage.value) == "OSError"
    monkeypatch.setattr(os, "lstat", lstat)
    await manifest.verify(_target(root), "e/", "a", pinned.files, pinned.pins)


async def test_partitions_are_the_directories_below_the_fixed_part(root: Path) -> None:
    _write(root, "sales/dt=2026-09-30/region=eu/p.parquet", "sales/dt=2026-10-01/region=us/p.parquet")
    paths = await _paths(root, "sales/", partitions=["dt", "region"])
    assert paths == ["sales/dt=2026-09-30/region=eu/p.parquet", "sales/dt=2026-10-01/region=us/p.parquet"]
    _write(root, "sales/p.parquet")
    with pytest.raises(ValueError) as refused:
        await _paths(root, "sales/", partitions=["dt", "region"])
    assert str(refused.value) == "side a: file 3 of 3 does not lie under the declared partitions (dt, region)"


ABOVE = (
    "side a: a directory above the declared partitions (dt) is named name=value, in the path or in the root"
    " of target legacy: it would be read as a column of every row; rename it"
)


async def test_no_other_name_value_directory_lies_above_the_partitions(root: Path) -> None:
    """With partitions declared DuckDB reads every name=value directory of a file's whole path as a
    column of each row, in place of the files' own column of that name. One in the path's fixed part or in
    the target's root refuses the plan; without partitions none is read so."""
    _write(root, "region=eu/dt=2024-01-01/part.parquet", "type=full/sales/dt=1/part.parquet")
    for path in ("region=eu/dt=*/part.parquet", "region=eu/"):
        with pytest.raises(ValueError) as refused:
            await _paths(root, path, partitions=["dt"])
        assert str(refused.value) == ABOVE
    rooted = _target(root / "type=full")
    with pytest.raises(ValueError) as refused:
        await manifest.expand(rooted, "sales/", "side a", max_files=9, partitions=["dt"])
    assert str(refused.value) == ABOVE
    assert await _paths(root, "region=eu/dt=*/part.parquet") == ["region=eu/dt=2024-01-01/part.parquet"]
    plain = await manifest.expand(rooted, "sales/", "side a", max_files=9)
    assert [f.path for f in plain] == ["sales/dt=1/part.parquet"]


# -- the path's grammar ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["customers.csv", "customers/current/*.csv", "sales/", "a b/c-d_e=f+g@h,(i).csv", "dt=*/part.parquet",
     "part-*-*.csv", "x" * 512],
)  # fmt: skip
def test_accepted_paths(path: str) -> None:
    assert manifest.path_error(path) is None


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../x.csv", "a/../b.csv", "./a.csv", "a/./b.csv", ".hidden/a.csv", "a/.b.csv",
     "a\\b.csv", "a//b.csv", "a/*/*.csv", "a/*/", "s3://bucket/a.csv", "http://h/a.csv", "a?.csv",
     "a[1].csv", "a'b.csv", 'a"b.csv', "a;b.csv", "~/a.csv", "a\nb.csv", "x" * 513, "/"],
)  # fmt: skip
def test_refused_paths(path: str) -> None:
    assert manifest.path_error(path) == manifest.PATH_REFUSED


@given(st.text(alphabet="ab./\\*~:?% \x00\n'\"$", max_size=24))
def test_no_accepted_path_leaves_the_root(path: str) -> None:
    if manifest.path_error(path) is None:
        joined = os.path.normpath(os.path.join("/r", path))
        assert joined.startswith("/r/") and ".." not in path.split("/") and not path.startswith("/")
        assert not any(segment.startswith(".") for segment in path.split("/"))


# -- arrival --------------------------------------------------------------------------


async def test_settle_and_max_age_with_an_injected_clock(root: Path) -> None:
    _write(root, "e/1.csv", "e/2.csv")
    old, new = time.time() - 4 * 86400, time.time() - 3 * 86400 - 4 * 3600
    os.utime(root / "e" / "1.csv", (old, old))
    os.utime(root / "e" / "2.csv", (new, new))
    found = await manifest.expand(_target(root), "e/", "side a", max_files=10)
    now = new + 12
    waiting = _target(root, settle_s=60)
    with pytest.raises(NotReady) as unsettled:
        manifest.arrival(waiting, found, "side a", now=now)
    assert (unsettled.value.code, str(unsettled.value)) == (
        "unsettled", "side a: a file changed 12 s ago, inside the 60 s target legacy waits (settle_s)"
    )  # fmt: skip
    assert manifest.arrival(waiting, found, "side a", now=new + 60) == time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(new)
    )
    later = new + 3 * 86400 + 4 * 3600 + 59
    with pytest.raises(NotReady) as stale:
        manifest.arrival(waiting, found, "side a", max_age="26h", now=later)
    assert (stale.value.code, str(stale.value)) == (
        "stale",
        "side a: the newest file is 3 d 4 h old, over the 26h the spec allows (file.maxAge): the export did"
        " not arrive",
    )
    manifest.arrival(waiting, found, "side a", max_age="4d", now=later)


async def test_the_stamp_file(root: Path, tmp_path: Path) -> None:
    _write(root, "e/1.csv")
    (root / "e" / "_AS_OF").write_text("2026-09-30T23:59:00+00:00\nwritten by the export job\n")
    pinned = await manifest.pin(_target(root), "e/*.csv", "side a", FILES, as_of_file="_AS_OF")
    assert pinned.as_of == "2026-09-30T23:59:00+00:00"
    cases = {
        "missing": (None, "the stamp file is not there: the export is not complete, or the spec names another"
                    " file"),
        "naive": (b"2026-09-30T23:59:00\n", "the stamp file must hold one ISO-8601 time with a UTC offset"),
        "text": (b"yesterday\n", "the stamp file must hold one ISO-8601 time with a UTC offset"),
        "binary": (b"\xff\xfe\n", "the stamp file must hold one ISO-8601 time with a UTC offset"),
        "empty": (b"", "the stamp file must hold one ISO-8601 time with a UTC offset"),
        "large": (b"2026-09-30T23:59:00+00:00\n" + b"x" * 4096,
                  "the stamp file is a symbolic link, may not be read or is over 4,096 bytes"),
        "link": ("link", "the stamp file is a symbolic link, may not be read or is over 4,096 bytes"),
    }  # fmt: skip
    for name, (content, message) in cases.items():
        stamp = root / "e" / f"_{name}"
        if content == "link":
            (tmp_path / "real").write_text("2026-09-30T23:59:00+00:00\n")
            stamp.symlink_to(tmp_path / "real")
        elif content is not None:
            stamp.write_bytes(content)
        with pytest.raises(NotReady) as refused:
            await manifest.pin(_target(root), "e/*.csv", "side a", FILES, as_of_file=f"_{name}")
        assert (refused.value.code, str(refused.value)) == ("unstamped", f"side a: {message}"), name


# -- pins -----------------------------------------------------------------------------


def _sha256sum(root: Path, files: tuple[str, ...]) -> str:
    """``sha256sum`` in the root, then ``cut -c1-64 | sha256sum``."""
    lines = [f"{hashlib.sha256((root / f).read_bytes()).hexdigest()}  {f}" for f in files]
    return hashlib.sha256("".join(line[:64] + "\n" for line in lines).encode()).hexdigest()


async def test_pins_and_the_aggregate_digest(root: Path) -> None:
    _write(root, "e/b.csv", data=b"id\n2\n")
    _write(root, "e/a.csv", data=b"id\n1\n" * 1000)
    pinned = await manifest.pin(_target(root), "e/*.csv", "side a", FILES)
    assert pinned.files == ("e/a.csv", "e/b.csv")
    stats = [os.stat(root / f) for f in pinned.files]
    assert pinned.pins == tuple(
        (
            s.st_size,
            s.st_mtime_ns,
            s.st_ctime_ns,
            s.st_ino,
            hashlib.sha256((root / f).read_bytes()).hexdigest(),
        )
        for s, f in zip(stats, pinned.files, strict=True)
    )
    facts = pinned.facts()
    assert facts == {
        "file_count": 2, "bytes": 5005, "files_sha256": _sha256sum(root, pinned.files),
        "pinned_at": pinned.pinned_at, "as_of": None, "modified_last": pinned.modified_last,
        "mount": "writable",
    }  # fmt: skip
    if shutil.which("shasum"):  # a site's own tool gives the same digest
        listed = subprocess.run(
            ["shasum", "-a", "256", *pinned.files], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        assert (
            hashlib.sha256("".join(line[:64] + "\n" for line in listed.splitlines()).encode()).hexdigest()
            == (facts["files_sha256"])
        )
    assert not [f for f in pinned.files if f in str(facts)]  # no name, size, time or digest of one file


async def test_a_partitioned_side_s_digest_binds_its_paths(root: Path) -> None:
    """A side that declares partitions compares its directories' names, so its digest is the SHA-256 of
    sha256sum's lines whole, paths and all: the same bytes under another directory are another export.
    Without partitions it is the files' digests alone, wherever they lie."""
    _write(root, "t/dt=2026-10-01/part.csv", data=b"id\n1\n")
    first = await manifest.pin(_target(root), "t/", "side a", FILES, partitions=["dt"])
    plain = await manifest.pin(_target(root), "t/", "side a", FILES)
    (root / "t" / "dt=2026-10-01").rename(root / "t" / "dt=2026-10-02")
    moved = await manifest.pin(_target(root), "t/", "side a", FILES, partitions=["dt"])
    sha = hashlib.sha256(b"id\n1\n").hexdigest()
    assert [pin[4] for pin in (*first.pins, *moved.pins)] == [sha, sha]
    lines = f"{sha}  t/dt=2026-10-01/part.csv\n"  # what sha256sum prints in the root
    assert first.facts()["files_sha256"] == hashlib.sha256(lines.encode()).hexdigest()
    assert moved.facts()["files_sha256"] != first.facts()["files_sha256"]
    assert plain.facts()["files_sha256"] == _sha256sum(root, moved.files)
    assert (await manifest.pin(_target(root), "t/", "side a", FILES)).facts()["files_sha256"] == (
        plain.facts()["files_sha256"]
    )


class _Reads(types.ModuleType):
    """``os`` as ``manifest`` sees it, whose ``read`` of a file under ``root`` waits ``delay`` and records
    when, and calls ``during`` before its first read."""

    def __init__(self, root: Path, delay: float = 0.0, during: Any = None) -> None:
        super().__init__("os")
        self.root, self.delay, self.during, self.reads = root, delay, during, []

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def read(self, fd: int, size: int) -> bytes:
        if self.during is not None:
            self.during()
            self.during = None
        time.sleep(self.delay)
        self.reads.append(time.monotonic())
        return os.read(fd, size)


async def test_a_file_growing_while_hashed_is_unsettled(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(root, "e/a.csv", "e/b.csv", data=b"x" * 4096)

    def grow() -> None:
        with (root / "e" / "a.csv").open("ab") as f:
            f.write(b"more rows\n")

    monkeypatch.setattr(manifest, "os", _Reads(root, during=grow))
    with pytest.raises(NotReady) as moved:
        await manifest.pin(_target(root), "e/", "side a", FILES)
    assert (moved.value.code, str(moved.value)) == (
        "unsettled",
        "side a: file 1 of 2 changed while the run was planned: the export is still being written",
    )


async def test_file_changed_during_plan(root: Path) -> None:
    """Step 9: the closing stat after the plan's statements sees a file that changed since its pin."""
    _write(root, "e/a.csv", "e/b.csv")
    pinned = await manifest.pin(_target(root), "e/", "side a", FILES)
    await manifest.verify(_target(root), "e/", "a", pinned.files, pinned.pins, planning=True)
    earlier = time.time() - 5
    os.utime(root / "e" / "b.csv", (earlier, earlier))
    with pytest.raises(NotReady) as changed:
        await manifest.verify(_target(root), "e/", "a", pinned.files, pinned.pins, planning=True)
    assert str(changed.value) == (
        "side a: file 2 of 2 changed while the run was planned: the export is still being written"
    )


def _renamed(root: Path, name: str, data: bytes) -> None:
    """A file written, then given an older modification time: its change time differs, as one renamed
    into place after it was written has."""
    _write(root, name, data=data)
    os.utime(root / name, (time.time() - 3600, time.time() - 3600))


async def test_cache_only_when_change_time_differs(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _renamed(root, "e/a.csv", b"a" * 3000)
    _write(root, "e/b.csv", data=b"b" * 3000)  # written in place: its change time is its modification time
    assert os.stat(root / "e" / "b.csv").st_ctime_ns == os.stat(root / "e" / "b.csv").st_mtime_ns
    monkeypatch.setattr(manifest, "CHUNK", 1024)
    first = await manifest.pin(_target(root), "e/", "side a", FILES)
    reads = _Reads(root)
    monkeypatch.setattr(manifest, "os", reads)
    again = await manifest.pin(_target(root), "e/", "side a", FILES)
    assert again.pins == first.pins and len(reads.reads) == 4  # b's three chunks and its end; a not read
    reads.reads.clear()
    await manifest.verify(_target(root), "e/", "a", first.files, first.pins, full=True)
    assert len(reads.reads) == 8  # a closing hash never uses the cache


async def test_planning_past_plan_s_stops_within_a_chunk_and_goes_on(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _renamed(root, "e/a.csv", b"a" * 2048)
    _renamed(root, "e/b.csv", b"b" * 64 * 1024)
    monkeypatch.setattr(manifest, "CHUNK", 1024)
    slow = _Reads(root, delay=0.05)
    monkeypatch.setattr(manifest, "os", slow)
    short = FilesSettings()
    object.__setattr__(short, "plan_s", 0.6)  # below the settings' 10 s, to keep the test short
    with pytest.raises(NotReady) as late:
        await manifest.pin(_target(root), "e/", "side a", short)
    stopped = time.monotonic()
    assert (late.value.code, str(late.value)) == (
        "planning",
        "side a: planning its files passed the 0.6 s files.plan_s allows (1 of 2 files hashed): try again (a"
        " file renamed into place is not hashed twice), or raise files.plan_s and the proxy's read timeout",
    )
    await asyncio.sleep(0.2)
    assert len([at for at in slow.reads if at > stopped]) <= 1  # the read in flight, then the flag
    slow.reads.clear()
    slow.delay = 0
    pinned = await manifest.pin(_target(root), "e/", "side a", FILES)
    assert len(pinned.pins) == 2 and len(slow.reads) == 65  # b only: a's hash was kept


async def test_a_gzip_stream_of_several_members_is_read_member_by_member(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each member of a gzip export (parts concatenated, as log shippers write them) is read
    to its end and none is fed twice, whether the file comes in one read or its members span reads; cut short,
    or ending in what is no member, it is still refused. Once a member's text passed the 1 MiB a step
    decompresses, zlib leaves the bytes after its end in unused_data and in unconsumed_tail: fed both, the
    check of a valid export never ended, or refused it."""
    monkeypatch.setattr(manifest, "STAT_S", 5)  # a check that does not end fails this test, not the session
    small = gzip.compress(b"".join(b"%07d,a\n" % n for n in range(200_000)))  # 2 MB of text in one read
    large = gzip.compress(os.urandom(1_200_000).hex().encode(), 1)  # 2.4 MB of text over one read
    assert len(small) * 2 < manifest.CHUNK < len(large)
    exports = ([small, small], [small, small, small], [large, large], [large, small, large], [small])
    for n, members in enumerate(exports):
        _write(root, f"g/{n}.csv.gz", data=b"".join(members))
    pinned = await manifest.pin(_target(root), "g/", "side a", FILES, stream="gzip")
    assert [pin[4] for pin in pinned.pins] == [
        hashlib.sha256(b"".join(members)).hexdigest() for members in exports
    ]
    for cut in ((small + small)[:-5], small + b"garbage", (large + large)[:-5], large + large + b"\x1f\x8b"):
        _write(root, "h/cut.csv.gz", data=cut)
        assert await _refusal(root, "h/", stream="gzip") == (
            "side a: its files could not be read while the run was planned (BadGzipFile)"
        )


FOOTER = (
    "has a Parquet footer that is not read: it is over 16 MiB, its schema holds over 10,000 elements or"
    " nests groups over 32 deep, it holds over 10,000 row groups or 200,000 column chunks, or it is not of"
    " the form this build reads"
)
ENCRYPTED = "is encrypted Parquet, which is not read"


async def test_a_parquet_footer_is_walked_before_duckdb_reads_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DuckDB follows a schema's nesting by recursion without a bound (a file of 20 KB whose
    groups nest 2,000 deep ended the process that planned it), so a plan walks each footer as it hashes its
    file, before any statement names the file (``pin`` runs no DuckDB), and refuses one over
    a size, an element count or a nesting depth, and one that is not of the form the walk reads: a leaf that
    claims children, or a negative count of them, which once left planning as an IndexError; and whatever
    lets the walk and Thrift's generated reader, which DuckDB reads a footer with, take different schemas.
    Three such footers passed the walk as flat and ended the process (``crafted``, 5,000 deep here: the
    walk alone reads them); the others are refused so that the two cannot differ."""
    tiny = parquet_tiny
    one = tiny.Column("id", tiny.INT64, (1).to_bytes(8, "little"))
    nested = dataclasses.replace(one, children=-3)
    body, flat, group = tiny.laid([one])
    made, struct_, number = tiny._Struct, tiny._STRUCT, tiny._I32

    def meta(schema: Any = None) -> Any:  # FileMetaData: its version, schema, rows and row group
        start = made().i32(1, 1)
        start = start.items(2, struct_, flat) if schema is None else schema(start)
        return start.i64(3, 1).items(4, struct_, [group])

    def raw(start: Any, field: int, kind: int, data: bytes) -> bytes:  # a field as written here, then the end
        start._field(field, kind)
        return bytes(start.out) + data + b"\x00"

    cut = made().binary(4, b"schema")  # 1 child to a reader, which cuts the number to 32 bits
    cut._field(5, number)
    cut.out += tiny._varint(1 << 32 | 2)
    whole = meta().end()
    cases = {
        "nested 2,000 deep": tiny.write([one], groups=2000),
        "nested 32 deep": tiny.write([one], groups=32),
        "a negative count": tiny.write([one, nested]),
        "a leaf of 9": tiny.write([dataclasses.replace(one, children=9)]),
        "10,001 elements": tiny.write([one] * 10_001),
        "a footer past its file": tiny.write([one])[:-8] + (2**31).to_bytes(4, "little") + b"PAR1",
        "no schema": b"PAR1" + b"\x00" * 16 + (16).to_bytes(4, "little") + b"PAR1",
        **tiny.crafted(5000),
        "a count of children a reader cuts": tiny.footed(
            body, meta(lambda s: s.items(2, struct_, [cut, flat[1]])).end()
        ),
        "its schema a struct": tiny.footed(body, meta(lambda s: s.struct(2, made())).end()),
        "its schema a list of numbers": tiny.footed(body, meta(lambda s: s.items(2, number, [1, 2])).end()),
        "its schema after its rows": tiny.footed(
            body, made().i32(1, 1).i64(3, 1).items(2, struct_, flat).items(4, struct_, [group]).end()
        ),
        "row groups that name numbers": tiny.footed(
            body, made().i32(1, 1).items(2, struct_, flat).i64(3, 1).items(4, struct_, [group], 6).end()
        ),
        "a list parquet.thrift has not": tiny.footed(body, meta().items(20, number, [1, 2]).end()),
        "a set": tiny.footed(body, raw(meta(), 20, 10, b"\x15\x02")),
        "a map": tiny.footed(body, raw(meta(), 20, 11, b"\x00")),
        "a type Thrift has not": tiny.footed(body, raw(meta(), 20, 13, b"\x01")),
        "a struct nine deep": tiny.footed(
            body,
            meta()
            .struct(20, functools.reduce(lambda inner, _: made().struct(1, inner), range(8), made()))
            .end(),
        ),
        "bytes after its end": tiny.footed(body, whole + b"\x00"),
        "a length that starts inside its footer": tiny.footed(body, whole)[:-8]
        + (len(whole) - 2).to_bytes(4, "little")
        + b"PAR1",
        "a length that starts before its footer": tiny.footed(body, whole)[:-8]
        + (len(whole) + 3).to_bytes(4, "little")
        + b"PAR1",
    }
    encrypted = {
        "an encrypted footer": tiny.footed(body, whole, b"PARE"),
        "an encrypted column": tiny.write([one, dataclasses.replace(one, name="v", encrypted=True)]),
    }
    eight = functools.reduce(lambda inner, _: made().struct(1, inner), range(7), made())
    held = tiny.Column("s", tiny.BYTE_ARRAY, b"\x01\x00\x00\x00a", statistics=(b"a", b"a"))
    _write(root, "ok/1.parquet", data=tiny.write([one]))
    _write(root, "ok/2.parquet", data=tiny.write([one], groups=31))  # the root and 31 groups
    _write(root, "ok/3.parquet", data=tiny.write([one] * 9_999))
    _write(root, "ok/4.parquet", data=tiny.footed(body, whole, b"PARX"))  # no Parquet: DuckDB's to word
    _write(root, "ok/5.parquet", data=tiny.write([one, held], created_by="tests"))
    _write(root, "ok/6.parquet", data=tiny.footed(body, meta().struct(20, eight).binary(21, b"").end()))
    assert len((await manifest.pin(_target(root), "ok/", "side a", FILES, stream="parquet")).pins) == 6
    for said, files in ((FOOTER, cases), (ENCRYPTED, encrypted)):
        for name, data in files.items():
            _write(root, "bad/2.parquet", data=data)
            _write(root, "bad/1.parquet", data=tiny.write([one]))
            assert await _refusal(root, "bad/", stream="parquet") == f"side a: file 2 of 2 {said}", name
            with pytest.raises(manifest.BadFooter) as refused:
                manifest.footed(str(root / "bad" / "2.parquet"))
            assert str(refused.value) == ("encrypted" if said == ENCRYPTED else ""), name
    assert len(cases["nested 2,000 deep"]) < 21_000
    monkeypatch.setattr(manifest, "FOOTER_MAX", 1024)  # the size alone
    manifest.footed(str(root / "ok" / "1.parquet"))
    with pytest.raises(manifest.BadFooter):
        manifest.footed(str(root / "ok" / "3.parquet"))


async def test_a_footer_holds_the_schema_s_chunks_and_few_lists(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DuckDB's reader makes a list's elements before it reads them, 704 bytes
    for a column chunk that one byte of a footer declares, outside ``files.memory``: the verifier's footer
    of a megabyte, a second row group of a million column chunks, took 672 MiB (``swollen``; 100,000 here,
    which the walk alone reads). So the walk refuses a row group whose column chunks are not the schema's
    leaves, as many and each by its path, and a footer of over 10,000 row groups, 200,000 column chunks or
    2,000,000 list elements in all, before it reads them. It gives the plan each footer's rows and its
    top-level columns' uncompressed bytes, also of a file whose hash stands for reading it: no statement of
    DuckDB's reads them. And it refuses the negative numbers DuckDB reads as an internal error, which
    invalidates its database: a schema element's type length, scale or precision (its bind of the file),
    a row group's count of rows (its count of the file's rows); and a negative size or count of the file's
    rows, which the plan's figures would hold."""
    tiny = parquet_tiny
    made, struct_ = tiny._Struct, tiny._STRUCT
    one = tiny.Column("id", tiny.INT64, (1).to_bytes(8, "little"))
    other = dataclasses.replace(one, name="v")
    body, flat, group = tiny.laid([one])
    _, both, pair = tiny.laid([one, other])

    def meta(groups: list[Any], schema: list[Any] = flat) -> bytes:
        return made().i32(1, 1).items(2, struct_, schema).i64(3, 7).items(4, struct_, groups).end()

    def keyed(values: int) -> bytes:  # FileMetaData with that many key values, a byte each
        start = made().i32(1, 1).items(2, struct_, flat).i64(3, 7).items(4, struct_, [group])
        start._field(5, tiny._LIST)
        return bytes(start.out) + bytes([0xF0 | struct_]) + tiny._varint(values) + b"\x00" * values + b"\x00"

    twenty = tiny.laid([one] * 21)
    size = len(body) - 4
    empty = made()  # the row group again, of -1 rows
    empty.out, empty.last = group.out[:-2], 2
    sized = b"\x16\x02\x16" + tiny._zigzag(size)  # a chunk's one value, then its uncompressed size
    fixed = tiny.Column("v", tiny.FIXED_LEN_BYTE_ARRAY, b"", type_length=-1)
    scaled = dataclasses.replace(tiny.decimal("v", 150, 12, 2), logical={"decimal": (12, -1)})
    wide = dataclasses.replace(tiny.decimal("v", 150, 12, 2), logical={"decimal": (-1, 2)})
    cases = {
        "a second row group of 100,000 chunks": tiny.swollen(100_000),
        "a chunk more than the leaves": tiny.footed(body, meta([pair])),
        "a chunk fewer than the leaves": tiny.footed(body, meta([group], both)),
        "a chunk of another path": tiny.footed(body, meta([tiny.laid([other])[2]])),
        "a chunk without its metadata": tiny.footed(
            body, meta([made().items(1, struct_, [made().i64(2, 4)])])
        ),
        "a row group without chunks": tiny.footed(body, meta([made().i64(2, 0).i64(3, 1)])),
        "10,001 row groups": tiny.footed(body, meta([group] * 10_001)),
        "200,004 column chunks": tiny.footed(twenty[0], meta([twenty[2]] * 9_524, twenty[1])),
        "2,000,001 list elements": tiny.footed(body, keyed(1_999_995)),  # and 6 of its other lists
        "a row group of -1 rows": tiny.footed(body, meta([empty.i64(3, -1)])),
        "a file of -7 rows": tiny.footed(body, meta([group]).replace(b"\x16\x0e\x19", b"\x16\x0d\x19", 1)),
        "a chunk of -1 bytes": tiny.footed(body, meta([group]).replace(sized, b"\x16\x02\x16\x01", 1)),
        "a type length of -1": tiny.write([one, fixed]),
        "a scale of -1": tiny.write([one, scaled]),
        "a precision of -1": tiny.write([one, wide]),
    }
    assert sized in meta([group]) and b"\x16\x0e\x19" in meta([group])
    for name, data in cases.items():
        _write(root, "bad/1.parquet", data=data)
        assert await _refusal(root, "bad/", stream="parquet") == f"side a: file 1 of 1 {FOOTER}", name
    _write(root, "ok/1.parquet", data=tiny.footed(body, meta([group] * 10_000)))
    _write(root, "ok/2.parquet", data=tiny.footed(body, keyed(1_999_994)))
    _write(root, "ok/3.parquet", data=tiny.footed(body, meta([pair, pair], both)))
    _write(root, "ok/4.parquet", data=tiny.write([one], groups=2))  # a nested leaf: no top-level column
    _write(root, "ok/5.parquet", data=tiny.footed(body, meta([group]), b"PARX"))
    assert manifest.footed(str(root / "ok" / "1.parquet")) == (7, {"id": 10_000 * size})
    assert manifest.footed(str(root / "ok" / "3.parquet")) == (7, {"id": 2 * size, "v": 2 * size})
    assert manifest.footed(str(root / "ok" / "4.parquet")) == (1, {})
    assert manifest.footed(str(root / "ok" / "5.parquet")) is None  # no Parquet by its end: DuckDB's to word
    summed = (7 + 7 + 7 + 1, {"id": 10_003 * size, "v": 2 * size})
    assert (await manifest.pin(_target(root), "ok/", "side a", FILES, stream="parquet")).footed == summed
    for name in ("1", "2", "3", "4", "5"):  # renamed into place: hashed once a process, walked at every plan
        os.utime(root / "ok" / f"{name}.parquet", (time.time() - 3600, time.time() - 3600))
    await manifest.pin(_target(root), "ok/", "side a", FILES, stream="parquet")
    reads = _Reads(root)
    monkeypatch.setattr(manifest, "os", reads)
    again = await manifest.pin(_target(root), "ok/", "side a", FILES, stream="parquet")
    assert again.footed == summed and reads.reads == []
    assert (await manifest.pin(_target(root), "ok/5.parquet", "side a", FILES)).footed == (0, {})  # a CSV's


# -- verify ---------------------------------------------------------------------------------


async def test_verify_names_each_change(root: Path, tmp_path: Path) -> None:
    target, files = _target(root), [f"e/{n}.csv" for n in range(1, 4)]
    _write(root, *files)
    pinned = await manifest.pin(target, "e/", "side a", FILES)

    async def changed(full: bool = False, pins: Any = None) -> str:
        with pytest.raises(FilesChanged) as raised:
            await manifest.verify(target, "e/", "a", pinned.files, pins or pinned.pins, full=full)
        return str(raised.value)

    await manifest.verify(target, "e/", "a", pinned.files, pinned.pins, full=True)
    forged = (*pinned.pins[:1], (*pinned.pins[1][:4], "0" * 64), *pinned.pins[2:])
    assert await changed(full=True, pins=forged) == (
        "side a: file 2 of 3 is not the file the run was created on (content)"
    )
    os.chmod(root / "e" / "1.csv", 0o600)  # the change time alone
    assert await changed() == "side a: file 1 of 3 is not the file the run was created on (changed)"
    os.chmod(root / "e" / "1.csv", 0o644)
    pinned = await manifest.pin(target, "e/", "side a", FILES)
    earlier = time.time() - 30
    os.utime(root / "e" / "2.csv", (earlier, earlier))
    assert await changed() == "side a: file 2 of 3 is not the file the run was created on (modified)"
    pinned = await manifest.pin(_target(root), "e/", "side a", FILES)
    with (root / "e" / "3.csv").open("ab") as f:
        f.write(b"2\n")
    assert await changed() == "side a: file 3 of 3 is not the file the run was created on (size)"
    pinned = await manifest.pin(_target(root), "e/", "side a", FILES)
    _write(root, "e/new.tmp", data=b"id\n1\n")
    os.replace(root / "e" / "new.tmp", root / "e" / "2.csv")
    assert await changed() == "side a: file 2 of 3 is not the file the run was created on (replaced)"
    pinned = await manifest.pin(_target(root), "e/", "side a", FILES)
    (root / "e" / "1.csv").unlink()
    (root / "e" / "1.csv").symlink_to(tmp_path / "x.csv")
    assert await changed() == "side a: file 1 of 3 is not the file the run was created on (link)"
    (root / "e" / "1.csv").unlink()
    _write(root, "e/1.csv", "e/4.csv")
    assert await changed() == "side a: 4 files match now, 3 when the run was created"
    shutil.rmtree(root)
    with pytest.raises(TargetUnreachable):
        await manifest.verify(target, "e/", "a", pinned.files, pinned.pins)


async def test_a_read_error_of_the_closing_hash_is_no_change(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closing hash of files that did not change, answered with an error by the mount (EIO
    of a bad block or a soft mount, a stale handle after a failover), is an outage as a listing's is, and a
    refused permission is denied: FilesChanged says that the export changed, and only a file that is gone,
    replaced or of other content has."""
    target = _target(root)
    _write(root, "e/1.csv")
    pinned = await manifest.pin(target, "e/", "side a", FILES)
    digest = manifest._digest

    def failing(error: OSError) -> None:
        def raising(path: str, *args: Any) -> Any:
            raise error

        monkeypatch.setattr(manifest, "_digest", raising)

    for code in (errno.EIO, errno.ESTALE):
        failing(OSError(code, os.strerror(code)))
        with pytest.raises(TargetUnreachable) as outage:
            await manifest.verify(target, "e/", "a", pinned.files, pinned.pins, full=True)
        assert str(outage.value) == "OSError"
    failing(PermissionError(errno.EACCES, "denied"))
    with pytest.raises(TargetDenied) as denied:
        await manifest.verify(target, "e/", "a", pinned.files, pinned.pins, full=True)
    assert str(denied.value) == "PermissionError"
    for gone, why in (
        (FileNotFoundError(errno.ENOENT, "gone"), "changed"),
        (OSError(errno.ELOOP, "a link"), "link"),
    ):
        failing(gone)
        with pytest.raises(FilesChanged) as changed:
            await manifest.verify(target, "e/", "a", pinned.files, pinned.pins, full=True)
        assert str(changed.value) == f"side a: file 1 of 1 is not the file the run was created on ({why})"
    monkeypatch.setattr(manifest, "_digest", digest)
    await manifest.verify(target, "e/", "a", pinned.files, pinned.pins, full=True)  # it is its pin still


async def test_an_emptied_root_is_an_outage_after_the_plan(root: Path, tmp_path: Path) -> None:
    """A mount point whose mount is not there is an empty directory (the service started before
    the export was mounted). At run time that is an outage, paused and resumed, never a claim that the export
    changed; a root that still holds an entry has changed. While a run is planned it stays a missing
    export (export_absent), which a schedule waits for."""
    target = _target(root)
    _write(root, "e/1.csv", "e/2.csv", "e/3.csv")
    pinned = await manifest.pin(target, "e/", "side a", FILES)
    (root / "e").rename(tmp_path / "unmounted")
    with pytest.raises(TargetUnreachable) as outage:
        await manifest.verify(target, "e/", "a", pinned.files, pinned.pins)
    assert str(outage.value) == "Empty" and not isinstance(outage.value, FilesChanged)
    with pytest.raises(NotReady) as unsettled:  # the last step of a plan
        await manifest.verify(target, "e/", "a", pinned.files, pinned.pins, planning=True)
    assert "changed while the run was planned" in str(unsettled.value)
    with pytest.raises(NotReady) as absent:
        await manifest.pin(target, "e/", "side a", FILES)
    assert absent.value.code == "export_absent"
    _write(root, "other/x.csv")  # the mount answers and holds something else: the export is gone
    with pytest.raises(FilesChanged) as changed:
        await manifest.verify(target, "e/", "a", pinned.files, pinned.pins)
    assert str(changed.value) == "side a: 0 files match now, 3 when the run was created"
    (tmp_path / "unmounted").rename(root / "e")
    await manifest.verify(target, "e/", "a", pinned.files, pinned.pins)  # mounted again: the run goes on


# -- threads and deadlines ------------------------------------------------------------------


async def test_stuck_mount_stalls_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A mount whose listing never answers: the event loop goes on serving, the deadline refuses the plan, the
    fifth call of its target meets four abandoned ones and spends no thread, each abandoned read is
    logged, and the threads hold no exit. Another files target is planned meanwhile (a dead mount
    costs its own target's runs), until abandoned reads of all targets together reach STUCK_ALL."""
    _write(root, "e/a.csv")
    other = root.parent / "healthy"
    _write(other, "e/a.csv")
    healthy = Target("healthy", "", root=str(other), settle_s=0, allow_writable=True)
    hung, listed = threading.Event(), manifest._listed

    def blocked(directory: str) -> Any:
        if directory.startswith(str(root)):
            hung.wait()
        return listed(directory)

    monkeypatch.setattr(manifest, "_listed", blocked)
    monkeypatch.setattr(manifest, "STAT_S", 0.2)
    ticks = 0

    async def served() -> None:  # what else the loop serves meanwhile: the API, a run without a file side
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beside = asyncio.create_task(served())
    try:
        for n in range(4):
            assert await _refusal(root, "e/") == (
                "side a: the directory of target legacy did not answer within 0.2 s: is its mount alive?"
            )
            assert manifest.stuck() == manifest.stuck(str(root)) == n + 1
        assert await _refusal(root, "e/") == (
            "side a: the file reader takes no work of target legacy: 4 reads of its mount, which does not"
            " answer, are still waiting; it resumes when they return or DeltaSleuth restarts"
        )
        assert manifest.stuck() == 4  # refused before anything was handed to a thread
        assert ticks > 40
        assert [record.getMessage() for record in caplog.records if record.name == "deltasleuth.files"] == [
            f"file read abandoned after its deadline ({n} waiting)" for n in (1, 2, 3, 4)
        ]
        pinned = await manifest.pin(healthy, "e/", "side a", FILES)  # the other target's mount answers
        assert pinned.files == ("e/a.csv",) and manifest.stuck(str(other)) == 0
        monkeypatch.setattr(manifest, "STUCK_ALL", 4)  # the pool's own protection: every target is refused
        assert await _refusal(other, "e/", healthy) == (
            "side a: the file reader takes no work: 4 reads of mounts that do not answer are still waiting;"
            " it resumes when they return or DeltaSleuth restarts"
        )
    finally:
        beside.cancel()
        hung.set()
    for _ in range(100):
        if manifest.stuck() == 0:
            break
        await asyncio.sleep(0.01)
    assert manifest.stuck() == 0  # they returned: file work is taken again
    assert [f.path for f in await manifest.expand(_target(root), "e/", "side a", max_files=9)] == ["e/a.csv"]
    exits = subprocess.run(
        [sys.executable, "-c", "import threading, time\nfrom deltasleuth.connectors.tables import manifest\n"
         "manifest.POOL.submit(threading.Event().wait)\ntime.sleep(0.1)\nprint('exited')"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert exits.stdout == "exited\n" and exits.returncode == 0


async def test_a_busy_pool_is_no_dead_mount(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call's deadline starts when a thread takes it. With every thread of the pool held by work that
    answers (sixteen long hashes, say), a call that waited longer than STAT_S and then ran is answered; one no
    thread took within STAT_S is withdrawn and never run: Busy, which a plan words as not ready, no
    abandoned read and no word of a dead mount."""
    _write(root, "e/a.csv")
    monkeypatch.setattr(manifest, "STAT_S", 0.3)
    release, ran = threading.Event(), []
    held = [manifest.POOL.submit(release.wait) for _ in range(16)]
    try:
        threading.Timer(0.2, release.set).start()  # the threads come free before STAT_S has passed
        started = time.monotonic()
        assert await manifest.call(lambda: time.sleep(0.25) or "answered") == "answered"
        assert time.monotonic() - started > 0.4  # queued 0.2 s, then its own 0.25 s: over STAT_S in all
        release.clear()
        held = [manifest.POOL.submit(release.wait) for _ in range(16)]
        with pytest.raises(manifest.Busy) as busy:
            await manifest.call(ran.append, 1)
        assert str(busy.value) == "Busy" and isinstance(busy.value, TargetUnreachable)
        with pytest.raises(NotReady) as refused:
            await manifest.pin(_target(root), "e/", "side a", FILES)
        assert refused.value.code == "planning" and str(refused.value) == (
            "side a: the file reader did not take its files' work (Busy): try again"
        )
        assert manifest.stuck() == 0
    finally:
        release.set()
    for job in held:
        job.result(5)
    await manifest.call(time.sleep, 0.05)  # the pool works again
    assert ran == []  # the withdrawn call was never run


# -- where roots lie and the spill ----------------------------------------------------------


def _settings(base: Path, **files: Any) -> Settings:
    return Settings(
        database_dsn_ref=f"file:{base / 'etc' / 'db_dsn'}", evidence_dir=base / "evidence",
        workspace_dir=base / "workspace", targets_file=base / "etc" / "targets.yaml",
        files=FilesSettings(roots_under=base / "files", **files),
    )  # fmt: skip


def _registry(*roots: Path | str, **declared: Any) -> TargetRegistry:
    targets = [Target(f"t{n}", "", root=str(root)) for n, root in enumerate(roots)]
    targets += [Target(name, "", **found) for name, found in declared.items()]
    return TargetRegistry({target.target_id: target for target in targets})


def _t10(target_id: str, under: Path) -> str:
    return (
        f"root of target {target_id} must lie under {under} (files.roots_under) and outside the directories"
        " DeltaSleuth uses (evidence, workspace, spill, settings, targets, a secret): mount exports in a"
        " directory of their own"
    )


def test_roots_lie_under_files_roots_under_and_outside_what_deltasleuth_uses(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "files" / "legacy").mkdir(parents=True)
    settings = _settings(base)
    assert manifest.forbidden(settings, _registry(base / "files" / "legacy")) == []
    assert manifest.forbidden(settings, _registry("/etc")) == [_t10("t0", base / "files")]
    escape = base / "files" / "escape"
    escape.symlink_to(base / "workspace")
    assert manifest.forbidden(settings, _registry(escape)) == [_t10("t0", base / "files")]  # after realpath
    assert manifest.forbidden(settings, _registry(escape), resolve=False) == []  # as written
    # Each place DeltaSleuth uses, set under roots_under so that only the second check refuses it.
    places = {
        "evidence_dir": base / "files" / "ev", "workspace_dir": base / "files" / "ws",
        "targets_file": base / "files" / "etc" / "targets.yaml",
    }  # fmt: skip
    for name, place in places.items():
        moved = dataclasses.replace(settings, **{name: place})
        holding = place.parent if name == "targets_file" else place
        assert manifest.forbidden(moved, _registry(holding)) == [_t10("t0", base / "files")], name
        assert manifest.forbidden(moved, _registry(base / "files")) == [_t10("t0", base / "files")], name
    spilled = _settings(base, spill_dir=base / "files" / "legacy" / "spill")
    assert manifest.forbidden(spilled, _registry(base / "files" / "legacy")) == [_t10("t0", base / "files")]
    secret = dataclasses.replace(settings, database_dsn_ref=f"file:{base / 'files' / 'legacy' / 'dsn'}")
    assert manifest.forbidden(secret, _registry(base / "files" / "legacy")) == [_t10("t0", base / "files")]
    password = DatabaseTarget("mysql", "db", 3306, "x", "u", f"file:{base / 'files' / 'legacy' / 'pw'}")
    with_db = _registry(base / "files" / "legacy", db={"database": password})
    assert manifest.forbidden(settings, with_db) == [_t10("t0", base / "files")]
    config = base / "files" / "legacy" / "deltasleuth.yaml"
    assert manifest.forbidden(settings, _registry(base / "files" / "legacy"), config=config) == [
        _t10("t0", base / "files")
    ]


def _stale(spill: Path, name: str) -> Path:
    (spill / name / "temp").mkdir(parents=True)
    (spill / name / "temp" / "duckdb_temp_storage-0.tmp").write_bytes(b"raw values")
    return spill / name


async def test_stale_spill_swept_at_open_and_hourly(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    settings = _settings(base)
    spill = settings.spill_dir
    mine = manifest.held(spill)  # this process's: flocked, so kept
    live = _stale(spill, "b" * 26)
    holder = subprocess.Popen(
        [sys.executable, "-c", f"import fcntl, os, sys\nfd = os.open({str(live)!r}, os.O_RDONLY)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert holder.stdout is not None and holder.stdout.readline() == "held\n"
        crashed, other = _stale(spill, "a" * 26), spill / "not-a-spill"
        other.mkdir()
        # Context.open's call: the sweep, and where roots lie
        assert await manifest.opened(settings, _registry()) == []
        assert sorted(p.name for p in spill.iterdir()) == sorted([mine.name, live.name, "not-a-spill"])
        assert not crashed.exists()
        _stale(spill, "c" * 26)

        class Pool:
            def connection(self) -> Any:
                raise OSError("the store is down")

        await _Worker(SimpleNamespace(settings=settings, pool=Pool()))._purge()  # type: ignore[arg-type]
        assert sorted(p.name for p in spill.iterdir()) == sorted([mine.name, live.name, "not-a-spill"])
    finally:
        holder.communicate("")
    assert manifest.sweep(spill) == 1  # its process ended: the next sweep removes it
    fd = os.open(mine, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


async def test_opened_refuses_a_forbidden_root(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    assert await manifest.opened(_settings(base), _registry("/etc")) == [_t10("t0", base / "files")]


FIXED = {
    "basis": "scan", "target": "exports", "relation": "customers", "rows": 6, "skipped": 0, "first_at": None,
    "last_at": None, "engine": "files", "consistency": "fixed", "server": "duckdb 1.5", "files": 12,
    "bytes": 3650722201, "pinned_at": "2026-10-01T02:14:07Z", "verified_at": "2026-10-01T02:20:00Z",
    "as_of": "2026-09-30T23:59:00+00:00", "modified_last": "2026-10-01T01:58:11Z", "mount": "writable",
    "rejected": 3, "repeated": 5,
}  # fmt: skip


def _texts(module: Any) -> list[str]:
    """Every text a module builds: its string constants but its docstrings."""
    tree = ast.parse(inspect.getsource(module))
    documented = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module
    docstrings = {
        ast.get_docstring(node, clean=False) for node in ast.walk(tree) if isinstance(node, documented)
    }
    found = (node.value for node in ast.walk(tree) if isinstance(node, ast.Constant))
    return [text for text in found if isinstance(text, str) and text not in docstrings]


@pytest.mark.parametrize("never", manifest.NOT)
def test_never_told(never: str) -> None:
    """No message of the file reader, no census wording of a fixed side and no line the certificate adds
    for one says what is never claimed of files."""
    from deltasleuth.connectors.tables import duck, reader
    from deltasleuth.reports import pack
    from deltasleuth.runner import census

    table = FIXED | {"basis": "table", "missing": 0, "extra": 0, "at": "2026-10-01T02:20:00Z", "where": None}
    frame = table | {"basis": "frame", "keys": 6, "unfit": 0, "as_of": None, "repeated": None}
    said = [text for text in _texts(manifest) if text not in manifest.NOT] + _texts(duck) + _texts(reader)
    said += [census.described(found) for found in (FIXED, table, frame)]
    said += [census.warning(side, FIXED) or "" for side in "ab"] + [pack.SINGLE_KEYS]
    assert len(said) > 300 and "from 12 fixed file(s)" in said[-6]
    assert not [text for text in said if re.search(never, text, re.I)]
