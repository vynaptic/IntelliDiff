"""Bound file matching and enumeration before an export can retain unbounded work."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deltasleuth.connectors.tables import TargetUnreachable, manifest

from .test_manifest import FILES, _paths, _target, _write


@pytest.mark.parametrize("pattern", ["*a" * 30 + "b", "*a" * 30 + "b*", "*" * 100 + "b"])
def test_adversarial_star_patterns_finish(pattern: str) -> None:
    # A subprocess bounds the regression even if the matcher again holds the GIL indefinitely.
    result = subprocess.run(
        [sys.executable, "-c", "from deltasleuth.connectors.tables import manifest\n"
         "import sys\npattern = sys.argv[1]\n"
         "assert manifest.path_error(pattern) is None\n"
         "assert manifest.stamp_error(pattern, 'a' * 60) is False\n"
         "assert manifest._globbed(pattern, 'a' * 60) is False\n", pattern],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr


@given(st.text("ab*.[?\n", max_size=10), st.text("ab*.[?\n", max_size=16))
def test_stars_keep_literal_and_newline_semantics(pattern: str, name: str) -> None:
    expected = re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), name, re.DOTALL)
    assert manifest._globbed(pattern, name) is (expected is not None)


def _listing(
    monkeypatch: pytest.MonkeyPatch, directory: Path, names: Iterator[str], *, slow: bool = False
) -> SimpleNamespace:
    real, found = os.scandir, directory.stat()
    observed = SimpleNamespace(seen=0, stats=0, closed=threading.Event(), release=threading.Event())

    class Entries:
        def __enter__(self) -> Entries:
            return self

        def __exit__(self, *args: Any) -> None:
            observed.closed.set()

        def __iter__(self) -> Entries:
            return self

        def __next__(self) -> Any:
            if observed.release.is_set():
                raise StopIteration
            if slow:
                time.sleep(0.005)
            name = next(names)
            observed.seen += 1

            def stat(*, follow_symlinks: bool) -> os.stat_result:
                assert follow_symlinks is False
                observed.stats += 1
                return found

            return SimpleNamespace(name=name, stat=stat)

    monkeypatch.setattr(os, "scandir", lambda path: Entries() if str(path) == str(directory) else real(path))
    return observed


@pytest.mark.parametrize("name,path", [(".hidden", "e/"), ("_temporary", "e/"), ("other", "e/*.csv")])
async def test_unmatched_and_hidden_entries_share_a_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, path: str
) -> None:
    root = tmp_path.resolve()
    (root / "e").mkdir()
    monkeypatch.setattr(manifest, "WALK_ENTRIES", 16, raising=False)
    observed = _listing(monkeypatch, root / "e", (f"{name}{n}" for n in range(100)))
    with pytest.raises(ValueError, match="directory entries"):
        await _paths(root, path)
    assert observed.seen <= 17 and observed.stats == 0 and observed.closed.is_set()


async def test_matched_files_stop_at_the_first_over_limit_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    _write(root, "e/file.csv")
    observed = _listing(monkeypatch, root / "e", (f"file{n}.csv" for n in range(100)))
    with pytest.raises(ValueError, match=r"files\.max_files"):
        await _paths(root, "e/*.csv", max_files=3)
    assert observed.seen <= 4 and observed.stats <= 4 and observed.closed.is_set()


async def test_empty_directories_count_towards_the_whole_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    for n in range(20):
        (root / "e" / f"d{n}" / "empty").mkdir(parents=True)
    monkeypatch.setattr(manifest, "WALK_ENTRIES", 16, raising=False)
    with pytest.raises(ValueError, match="directory entries"):
        await _paths(root, "e/")


@pytest.mark.parametrize("stop", ["cancel", "deadline", "planning"])
async def test_abandoned_enumeration_stops_its_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    root = tmp_path.resolve()
    (root / "e").mkdir()
    observed = _listing(monkeypatch, root / "e", (f".hidden{n}" for n in range(2000)), slow=True)
    monkeypatch.setattr(manifest, "STAT_S", 0.04)
    options = {"end": time.monotonic() + 0.025} if stop == "planning" else {}
    task = asyncio.create_task(_paths(root, "e/", **options))
    try:
        if stop == "cancel":
            await asyncio.sleep(0.025)
            task.cancel()
            error = asyncio.CancelledError
        else:
            error = manifest.Late if stop == "planning" else TargetUnreachable
        with pytest.raises(error):
            await task
        for _ in range(30):
            if observed.closed.is_set():
                break
            await asyncio.sleep(0.01)
        assert observed.closed.is_set() and observed.seen < 30
    finally:
        observed.release.set()


async def test_verification_bounds_unmatched_directory_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    _write(root, "e/file.csv")
    pinned = await manifest.pin(_target(root), "e/*.csv", "side a", FILES)
    monkeypatch.setattr(manifest, "WALK_ENTRIES", 16, raising=False)
    observed = _listing(monkeypatch, root / "e", (f"unmatched{n}" for n in range(100)))
    with pytest.raises(TargetUnreachable, match="EnumerationLimit"):
        await manifest.verify(_target(root), "e/*.csv", "a", pinned.files, pinned.pins)
    assert observed.seen <= 17 and observed.stats == 0
