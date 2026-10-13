"""What a file side reads, without DuckDB: where a files target's root
may lie, the spec path's grammar, the path expanded into files in path order, their arrival, their pins
(size, times, inode and SHA-256) taken when a run is created and compared at each later read, a Parquet
file's footer walked before DuckDB is handed the file, the spill directory a process holds and the sweep of
those whose process ended, and the threads all of it runs on.

Every file-system call runs on ``POOL``, daemon threads of the engine's own: never on the event loop,
which ``ds serve`` shares between the API and the worker, nor on asyncio's default executor, which
judging and evidence writes use. Each call has a deadline (``STAT_S``, within the ``files.plan_s`` of a
run's creation while it is planned), counted from when a thread takes it: a call no thread took within
``STAT_S`` is withdrawn, never run, and is ``Busy``, which judges no mount. A call past its deadline is
abandoned, never joined: it is counted for its target's root until it returns, and while ``STUCK_MAX`` of
one root wait, that target's file work is refused at once (every target's while ``STUCK_ALL`` wait). A
message, a log or a stored row names a file by its position in path order, never by its name: only ``ds
target check`` prints names.
"""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import fcntl
import hashlib
import logging
import os
import queue
import re
import secrets
import shutil
import stat
import threading
import time
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import zstandard

from deltasleuth.config import FilesSettings, Settings
from deltasleuth.connectors.tables.engine import FilesChanged, NotReady, TargetDenied, TargetUnreachable
from deltasleuth.core.spec.durations import parse_duration
from deltasleuth.core.spec.targets import TargetRegistry
from deltasleuth.core.types import Target

STAT_S = 30  # a listing, a statvfs, a stat check or one read of a hash; and a call's wait for a thread
STUCK_MAX = 4  # abandoned calls of one root waiting before its target's file work is refused
STUCK_ALL = 8  # and of all together before every target's is: half the pool stays for mounts that answer
CHUNK = 2**20  # a hash's read
STAMP_MAX = 4096  # bytes of a stamp file
DEPTH = 8  # directories below a directory read
PATH_MAX = 512
CACHE_MAX = 100_000  # hashes a process keeps
FOOTER_MAX = 16 * 2**20  # bytes of a Parquet footer
FOOTER_ELEMENTS = 10_000  # elements of its schema: its columns, and the groups that nest them
FOOTER_DEPTH = 32  # groups nested in it, the root among them
FOOTER_STRUCTS = 8  # structs nested in it (5 as writers nest them: a chunk's statistics, a column's type)
# What DuckDB's reader holds of a footer is outside ``files.memory`` and far more than the footer's bytes: it
# makes a list's elements before it reads them, 704 bytes a column chunk, 96 a row group, 64 a key value
# and 24 or fewer of any other, each of which one byte of a footer declares (observed on 1.5.5, macOS
# arm64). The largest file of the performance tests (10M rows of 4 columns, DuckDB's own writer: a row group
# each 122,880 rows) holds 82 row groups, 328 column chunks and 1,075 list elements. That writer spends 81
# bytes of footer and 3 list elements a column chunk: 16 MiB of it hold about 200,000 chunks.
FOOTER_GROUPS = 10_000  # row groups of it
FOOTER_CHUNKS = 200_000  # column chunks in all of them
FOOTER_ITEMS = 2_000_000  # elements of all its lists together, those among them: 10 a column chunk
# The lists a footer may hold (parquet.thrift's FileMetaData, as the locked DuckDB's reader knows it), by the
# id of the field that holds each: a list's elements are a Thrift compact type's number (5 an i32, 6 an i64,
# 8 bytes) or structs, given as their own lists; ``(lists,)`` is a struct on the way to one. A struct not
# named here holds no list. The schema, the row groups, a group's column chunks and a chunk's path are
# walked by name (``_schema``, ``_groups``, ``_chunk``).
_PLAIN: dict[int, Any] = {}  # a schema element, a key and its value, a sorting column, a column order
_COLUMN = {2: 5, 8: _PLAIN, 13: _PLAIN, 16: ({2: 6, 3: 6},), 17: ({2: 5},)}  # ColumnMetaData, but its path
_GROUP = {4: _PLAIN}  # RowGroup: its sorting columns
_FOOTER = {5: _PLAIN, 7: _PLAIN}  # FileMetaData: its key values and column orders
_NAME = re.compile(r"[A-Za-z0-9 ._=+@,()-]+")  # the alphabet of a name: no glob, quote or URL scheme
_SEGMENT = re.compile(r"[A-Za-z0-9 _=+@,()*-][A-Za-z0-9 ._=+@,()*-]*")  # a path's: none starts with a dot
_SPILL = re.compile(r"[0-9a-f]{26}")  # a process's spill directory
_SIZE = {"MB": 2**20, "GB": 2**30, "TB": 2**40}
ALPHABET = "letters, digits, space and . _ - = + @ , ( )"
PATH_REFUSED = (
    f"file.path is a relative path under the target's root: {ALPHABET} in each segment, none starting with"
    " a dot; * matches inside one segment; a trailing / reads the whole directory"
)
# What is never claimed of a file side, as patterns no text built of one matches: the
# messages of this module and of ``duck``, a fixed side's census wording and the certificate's line.
NOT = (
    r"snapshot",  # a file side is fixed files
    r"immutable|tamper",  # a change restored between two checks is not seen
    r"source system (matches|agrees)|export is complete",  # only these files' rows, as read
    r"exported (at|on) ",  # an export's time is declared by its stamp file, or not observed
    r"single keys? (was|were) hashed|recheck(ed)? read",  # checked by size, times and inode; read once
    r"observed whole|every line (was |is )?compared",  # while lines were not rows
    r"types? (is |are |were )?verified|keys are unique",  # a CSV declares its types; a file promises no key
    r"object stor|s3://",  # not built
    r"sandbox|encrypted spill|spill (is|was) encrypted",  # DuckDB is neither
    r"isolat|confin",  # nor is its reader's process: it runs as the same user
    r"tested on",  # Parquet of DuckDB's own writer and the tests' hand-built files are observed
)
log = logging.getLogger("deltasleuth.files")
Pin = tuple[int, int, int, int, str]  # bytes, mtime_ns, ctime_ns, inode, sha256: one file's, sealed
# A Parquet footer's rows and each top-level column's uncompressed bytes in all its row groups, as walked
Footed = tuple[int, dict[str, int]]
_HASHED: dict[tuple[Any, ...], str] = {}  # (device, bytes, mtime_ns, ctime_ns, inode, stream) -> sha256
_HELD: dict[Path, int] = {}  # this process's spill directories' descriptors, flocked while it holds them


class _Job(Future[Any]):
    """A call of the pool: ``began`` is when a thread took it (a ``time.monotonic``), None while it waits
    for one."""

    began: float | None = None


class _Pool:
    """Daemon threads, started at the first call: none holds the process at its exit, and a call hung in
    the kernel keeps its thread and nothing else. A call cancelled while it waits for a thread is never
    run."""

    def __init__(self, size: int) -> None:
        self._size, self._jobs, self._lock = size, queue.SimpleQueue[Any](), threading.Lock()
        self._started = False

    def submit(self, call: Callable[..., Any], *args: Any) -> _Job:
        with self._lock:
            if not self._started:
                for n in range(self._size):
                    threading.Thread(target=self._work, name=f"ds-files-{n}", daemon=True).start()
                self._started = True
        future = _Job()
        self._jobs.put((future, call, args))
        return future

    def _work(self) -> None:
        while True:
            future, call, args = self._jobs.get()
            if future.set_running_or_notify_cancel():
                future.began = time.monotonic()
                try:
                    future.set_result(call(*args))
                except BaseException as exc:
                    future.set_exception(exc)


POOL = _Pool(16)
_stuck: Counter[str | None] = Counter()  # abandoned calls that have not returned, by their target's root
_stuck_lock = threading.Lock()


class Stuck(TargetUnreachable):
    """``STUCK_MAX`` calls of the target's root past their deadline still wait, or ``STUCK_ALL`` of all
    (a plan is refused for it)."""


class Busy(TargetUnreachable):
    """The reader did not take the work, and no mount is judged by it (at plan, not ready): no thread of
    ``POOL`` was free within ``STAT_S`` (``Busy``), so the call was withdrawn and is never run; or its
    reader was discarded for another read's failure (``Dropped``, ``duck``) as it asked for a cursor, or
    after an error that invalidated its database; or the reader's process ended (``Died``) or did not
    start (``Unstarted``), or another task's reader apart was not free (``Alone``). Tried again."""


class Late(Exception):
    """A run's planning passed ``files.plan_s``."""


def stuck(root: str | None = None) -> int:
    """Calls abandoned after their deadline that have not returned: of every root, or of ``root``."""
    return sum(_stuck.values()) if root is None else _stuck[root]


def _returned(root: str | None, _: Future[Any]) -> None:
    with _stuck_lock:
        _stuck[root] -= 1


def _abandoned(future: Future[Any], root: str | None = None) -> TargetUnreachable:
    """Count ``future``'s call as abandoned, for its target's ``root``, until it returns (a reader's
    statement: until it answers or its process ends)."""
    with _stuck_lock:
        _stuck[root] += 1
        waiting = stuck()
    future.add_done_callback(partial(_returned, root))
    log.warning("file read abandoned after its deadline (%d waiting)", waiting)
    return TargetUnreachable("Deadline")


def _awaited(pooled: Future[Any]) -> asyncio.Future[Any]:
    """``pooled`` as the running loop's future, whose error is read even when no caller waits for it any
    more (a caller past its deadline, or cancelled)."""
    future = asyncio.wrap_future(pooled)
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    return future


def _admitted(root: str | None = None) -> None:
    """Before anything is handed to the pool: a target whose root holds ``STUCK_MAX`` abandoned calls takes
    no file work, and none does while ``STUCK_ALL`` wait, so a dead mount costs no thread more."""
    if _stuck[root] >= STUCK_MAX or stuck() >= STUCK_ALL:
        raise Stuck("Stuck")


def unanswered(exc: TargetUnreachable, who: str, target: Target) -> ValueError:
    """What a plan's file work met in place of an answer, worded for ``who``: reads still waiting
    (``Stuck``), work not taken (``Busy``: not ready, tried again) or a directory that did not answer."""
    if isinstance(exc, Stuck) and stuck(target.root) >= STUCK_MAX:
        return ValueError(
            f"{who}: the file reader takes no work of target {target.target_id}: {stuck(target.root)} reads"
            " of its mount, which does not answer, are still waiting; it resumes when they return or"
            " DeltaSleuth restarts"
        )
    if isinstance(exc, Stuck):
        return ValueError(
            f"{who}: the file reader takes no work: {stuck()} reads of mounts that do not answer are still"
            " waiting; it resumes when they return or DeltaSleuth restarts"
        )
    if isinstance(exc, Busy):
        return NotReady("planning", f"{who}: the file reader did not take its files' work ({exc}): try again")
    return ValueError(
        f"{who}: the directory of target {target.target_id} did not answer within {STAT_S} s: is its"
        " mount alive?"
    )


async def call[T](
    fn: Callable[..., T],
    *args: Any,
    timeout_s: float | None = None,
    end: float | None = None,
    root: str | None = None,
    read: list[float] | None = None,
) -> T:
    """``fn(*args)`` on ``POOL``, for the target whose ``root`` it reads, within ``timeout_s`` (``STAT_S``)
    of when a thread took it (of its last read, whose time it keeps in ``read``) and, when ``end`` (a
    ``time.monotonic``) is given, before it: past ``end`` it is ``Late`` and left to finish; past
    ``timeout_s`` it is ``TargetUnreachable('Deadline')`` and abandoned, counted until it returns. A call no
    thread took within ``STAT_S`` is ``Busy``; it, and a queued call whose caller left, is never run."""
    _admitted(root)
    timeout_s = STAT_S if timeout_s is None else timeout_s
    if end is not None and time.monotonic() >= end:
        raise Late
    pooled = POOL.submit(fn, *args)
    future, sent = _awaited(pooled), time.monotonic()
    try:
        while True:
            began = pooled.began
            if began is None:  # queued: looked at again before a deadline of its own could pass
                due = min(sent + STAT_S, time.monotonic() + timeout_s)
            else:  # from its start, or its last read
                due = max([began, *(read or ())]) + timeout_s
            done, _ = await asyncio.wait({future}, timeout=max(min(due, end or due) - time.monotonic(), 0))
            if done:
                return future.result()
            now = time.monotonic()
            if end is not None and now >= end:
                raise Late
            if pooled.began is None:
                if now >= sent + STAT_S and pooled.cancel():
                    raise Busy("Busy")
            elif began is not None and now - max([began, *(read or ())]) >= timeout_s:
                raise _abandoned(pooled, root)
    finally:
        pooled.cancel()  # still queued, it never runs; running, it goes on


def size_bytes(size: str) -> int:
    """A ``files`` size (``2GB``) in bytes: binary units, as ``sampling.temp_buffers`` reads them."""
    return int(size[:-2]) * _SIZE[size[-2:]]


# -- where roots lie ------------------------------------------------------------------------------


def _refs(value: Any) -> Iterator[str]:
    """Every ``file:`` reference a settings or target value holds."""
    if isinstance(value, str) and value.startswith("file:"):
        yield value.removeprefix("file:")
    elif isinstance(value, dict):
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _refs(item)


def forbidden(
    settings: Settings, registry: TargetRegistry, *, resolve: bool = True, config: Path | None = None
) -> list[str]:
    """A refusal for each files target whose root, after ``realpath`` (``resolve``), does not lie under
    ``files.roots_under``, or is, holds or lies inside a directory DeltaSleuth uses: the evidence volume,
    the workspace, the spill directory, the targets file's (and ``config``'s, the settings file, where
    the caller knows it) and every ``file:`` reference's."""
    real = os.path.realpath if resolve else os.path.normpath
    targets = [registry.get(target_id) for target_id in registry]
    used = [settings.evidence_dir, settings.workspace_dir, settings.spill_dir, settings.targets_file.parent]
    used += [config.parent] if config is not None else []
    refs = [
        *_refs(dataclasses.asdict(settings)),
        *(ref for t in targets for ref in _refs(dataclasses.asdict(t))),
    ]
    places = [Path(real(place)) for place in [*used, *(Path(ref).parent for ref in refs)]]
    under = Path(real(settings.files.roots_under))
    refused = []
    for target in targets:
        if target.root is None:
            continue
        root = Path(real(target.root))
        if not root.is_relative_to(under) or any(
            root.is_relative_to(place) or place.is_relative_to(root) for place in places
        ):
            refused.append(
                f"root of target {target.target_id} must lie under {settings.files.roots_under}"
                " (files.roots_under) and outside the directories DeltaSleuth uses (evidence, workspace,"
                " spill, settings, targets, a secret): mount exports in a directory of their own"
            )
    return refused


async def opened(settings: Settings, registry: TargetRegistry, config: Path | None = None) -> list[str]:
    """What every ``ds`` command does as it opens a context, and ``ds target check`` (with ``config``, its
    settings file) before it looks at a files target: the spill directories of processes that ended are
    swept, and where each root lies is judged (``forbidden``); a root that does not answer within ``STAT_S``
    is judged as written, so one dead mount stops no command."""
    try:
        await call(sweep, settings.spill_dir)
    except (OSError, TargetUnreachable) as exc:
        log.warning("spill not swept: %s", type(exc).__name__)
    if not any(registry.get(target_id).root is not None for target_id in registry):
        return []
    try:
        return await call(partial(forbidden, settings, registry, config=config))
    except TargetUnreachable:
        return forbidden(settings, registry, resolve=False, config=config)


# -- the path -------------------------------------------------------------------------------------


def path_error(path: str) -> str | None:
    """The refusal's text when ``path`` is not a relative path of the closed grammar: segments of the
    alphabet, none starting with a dot; ``*`` inside one segment, or a trailing ``/``; else None."""
    segments = path.split("/")
    if path.endswith("/"):
        segments.pop()
    globbed = [segment for segment in segments if "*" in segment]
    if (
        not 0 < len(path) <= PATH_MAX
        or not segments
        or not all(_SEGMENT.fullmatch(segment) for segment in segments)
        or len(globbed) > 1
        or (globbed and path.endswith("/"))
    ):
        return PATH_REFUSED
    return None


def _globbed(pattern: str, name: str) -> bool:
    """Whether ``name`` is matched by ``pattern``, whose ``*`` matches any characters of one name."""
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), name, re.DOTALL) is not None


def stamp_error(path: str, name: str) -> bool:
    """Whether ``name`` is no stamp file of ``path``: a name of the alphabet, in the path's fixed
    directory, that the path does not match."""
    if not _NAME.fullmatch(name) or name.startswith("."):
        return True
    rest = _fixed(path)[1]
    if not rest:  # a directory read: every name but those starting with . or _
        return not name.startswith("_")
    return len(rest) == 1 and _globbed(rest[0], name)


# -- expansion -----------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """A file the path expands to, or what refuses the plan at its place in path order: ``kind`` is
    ``file``, ``link``, ``other`` (not a regular file), ``unreadable`` or ``name`` (outside the alphabet)."""

    path: str  # relative to the root
    kind: str
    stat: os.stat_result | None = None


_KIND_TEXT = {
    "link": "is a symbolic link or is reached through one: links are never followed",
    "other": "is not a regular file",
    "unreadable": "may not be read by DeltaSleuth (uid {uid})",
    "name": f"has a name outside {ALPHABET}: rename it",
}


def _rooted(root: str) -> str:
    """Step 1: ``read-only`` or ``writable``, or why the root is no directory to read: ``absent`` (not
    there, or not listable) or ``link``."""
    try:
        found = os.stat(root)
        if not stat.S_ISDIR(found.st_mode) or not os.access(root, os.R_OK | os.X_OK):
            return "absent"
        if os.path.realpath(root) != root:
            return "link"
        return "read-only" if os.statvfs(root).f_flag & os.ST_RDONLY else "writable"
    except OSError:
        return "absent"


def _entry(directory: str, name: str) -> os.stat_result | None:
    try:
        return os.lstat(os.path.join(directory, name))
    except FileNotFoundError:
        return None


def _listed(directory: str) -> list[tuple[str, os.stat_result]] | None:
    """A directory's entries with their ``lstat``, never following a link; None when it may not be read."""
    try:
        with os.scandir(directory) as entries:
            return [(entry.name, entry.stat(follow_symlinks=False)) for entry in entries]
    except PermissionError:
        return None


def _kind(directory: str, name: str, found: os.stat_result) -> str:
    if stat.S_ISLNK(found.st_mode):
        return "link"
    if not _NAME.fullmatch(name):
        return "name"
    if not stat.S_ISREG(found.st_mode):
        return "other"
    return (
        "file" if os.access(os.path.join(directory, name), os.R_OK, follow_symlinks=False) else "unreadable"
    )


def _fixed(path: str) -> tuple[list[str], list[str]]:
    """The path's fixed directories, and the segments after them: a file's name, a glob and what follows
    it, or none for a directory read."""
    segments = path.removesuffix("/").split("/")
    if path.endswith("/"):
        return segments, []
    at = next((i for i, segment in enumerate(segments) if "*" in segment), len(segments) - 1)
    return segments[:at], segments[at:]


async def _walk(root: str, path: str, end: float | None) -> list[Found]:
    """The path's candidates: every regular file it names and everything that refuses the plan."""
    fixed, rest = _fixed(path)
    here = root
    for i, segment in enumerate(fixed):
        found = await call(_entry, here, segment, end=end, root=root)
        if found is None or not (stat.S_ISDIR(found.st_mode) or stat.S_ISLNK(found.st_mode)):
            return []
        if stat.S_ISLNK(found.st_mode):
            return [Found("/".join(fixed[: i + 1]), "link")]
        here = os.path.join(here, segment)
    base = "/".join(fixed)
    if not rest:
        return await _below(root, base, 0, end)
    return await _matched(root, base, rest, end)


async def _matched(root: str, base: str, rest: list[str], end: float | None) -> list[Found]:
    """The candidates of ``rest`` (a name or a glob, then literal names) under ``base``. A matched directory
    whose name is outside the alphabet is a candidate that refuses the plan, never entered: a file
    below it would be pinned under a path DuckDB reads as a glob."""
    here, (segment, *after) = os.path.join(root, base), rest
    if "*" not in segment:
        found = await call(_entry, here, segment, end=end, root=root)
        names = [] if found is None else [(segment, found)]
    else:
        listed = await call(_listed, here, end=end, root=root)
        if listed is None:
            return [Found(base, "unreadable")]
        names = [
            (name, found) for name, found in listed if not name.startswith(".") and _globbed(segment, name)
        ]
    candidates = []
    for name, found in names:
        relative = f"{base}/{name}" if base else name
        if not after:
            candidates.append(Found(relative, _kind(here, name, found), found))
        elif stat.S_ISLNK(found.st_mode):
            candidates.append(Found(relative, "link"))
        elif stat.S_ISDIR(found.st_mode) and not _NAME.fullmatch(name):
            candidates.append(Found(relative, "name"))
        elif stat.S_ISDIR(found.st_mode):
            candidates += await _matched(root, relative, after, end)
    return candidates


async def _below(root: str, base: str, depth: int, end: float | None) -> list[Found]:
    """A directory read: every file below ``base`` whose name starts with neither ``.`` nor ``_``, in no
    directory whose name does; a directory deeper than ``DEPTH`` is refused."""
    listed = await call(_listed, os.path.join(root, base), end=end, root=root)
    if listed is None:
        return [Found(base, "unreadable")]
    candidates = []
    for name, found in listed:
        if name.startswith((".", "_")):
            continue
        relative = f"{base}/{name}" if base else name
        if stat.S_ISDIR(found.st_mode) and _NAME.fullmatch(name):
            if depth == DEPTH:
                raise _Deep
            candidates += await _below(root, relative, depth + 1, end)
        else:
            candidates.append(Found(relative, _kind(os.path.join(root, base), name, found), found))
    return candidates


class _Deep(Exception):
    pass


async def rooted(target: Target, who: str, *, end: float | None = None) -> str:
    """Step 1: the target's mount, ``read-only`` or ``writable`` (refused unless ``allow_writable``), of a
    root that is its own real path and a directory DeltaSleuth may list."""
    target_id = target.target_id
    mount = await call(_rooted, target.root or "", end=end, root=target.root)
    if mount == "absent":
        raise ValueError(_absent(who, target))
    if mount == "link":
        raise ValueError(
            f"{who}: the directory of target {target_id} is, or is reached through, a symbolic link: declare"
            " its real path as root"
        )
    if mount == "writable" and not target.allow_writable:
        raise ValueError(
            f"{who}: the directory of target {target_id} is mounted writable: mount it read-only (:ro), or"
            " declare allow_writable: true on the target"
        )
    return mount


def _absent(who: str, target: Target) -> str:
    return (
        f"{who}: the directory of target {target.target_id} is not there, or DeltaSleuth (uid {os.getuid()})"
        " may not list it: is it mounted into this container?"
    )


async def expand(
    target: Target,
    path: str,
    who: str,
    *,
    max_files: int,
    partitions: Sequence[str] = (),
    end: float | None = None,
) -> list[Found]:
    """Step 2: the files the path names under the target's root, in the order of their relative paths'
    UTF-8 bytes. A refusal is worded for ``who`` (``side a``, or ``target <id>``): a
    link, what is not a regular file or a name outside the alphabet is never skipped."""
    target_id = target.target_id
    try:
        found = sorted(await _walk(target.root or "", path, end), key=lambda f: f.path.encode())
    except _Deep:
        raise ValueError(f"{who}: the path holds directories more than {DEPTH} deep") from None
    if not found:
        raise NotReady("export_absent", f"{who}: no file matches {path} under target {target_id}")
    if len(found) > max_files:
        raise ValueError(
            f"{who}: the path matches {len(found):,} files: a side reads at most {max_files:,}"
            " (files.max_files)"
        )
    for n, candidate in enumerate(found, 1):
        if candidate.kind != "file":
            why = _KIND_TEXT[candidate.kind].format(uid=os.getuid())
            raise ValueError(f"{who}: file {n} of {len(found)} (in path order) {why}")
    if partitions:
        above, named = _fixed(path)[0], ", ".join(partitions)
        # DuckDB reads every name=value directory of a file's whole path as a column of each row, in place
        # of a column of that name in the files: only the declared ones may be in it.
        if any("=" in segment for segment in [*(target.root or "").split("/"), *above]):
            raise ValueError(
                f"{who}: a directory above the declared partitions ({named}) is named name=value, in the"
                f" path or in the root of target {target_id}: it would be read as a column of every row;"
                " rename it"
            )
        for n, candidate in enumerate(found, 1):
            dirs = candidate.path.removeprefix("/".join(above)).strip("/").split("/")[:-1]
            if len(dirs) != len(partitions) or any(
                not d.startswith(f"{p}=") for d, p in zip(dirs, partitions, strict=False)
            ):
                raise ValueError(
                    f"{who}: file {n} of {len(found)} does not lie under the declared partitions ({named})"
                )
    return found


# -- arrival and pins --------------------------------------------------


def _stamped(path: str) -> str:
    """The stamp file's instant, or why not: ``absent``, ``unread`` (a link, not a regular file, not
    readable or over ``STAMP_MAX`` bytes) or ``malformed``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unread"
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_size > STAMP_MAX:
            return "unread"
        text = os.read(fd, STAMP_MAX + 1)
    finally:
        os.close(fd)
    try:
        when = datetime.fromisoformat(text.decode().splitlines()[0].strip())
    except (UnicodeDecodeError, IndexError, ValueError):
        return "malformed"
    return when.isoformat() if when.tzinfo is not None else "malformed"


def _age(seconds: float) -> str:
    """A length of time as a refusal says it: ``3 d 4 h``, ``5 h 12 min``, ``12 min``."""
    minutes = int(seconds // 60)
    days, hours = divmod(minutes // 60, 24)
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {minutes % 60} min" if hours else f"{minutes} min"


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def arrival(
    target: Target,
    found: Sequence[Found],
    who: str,
    *,
    max_age: str | None = None,
    now: float | None = None,
) -> str:
    """Step 3, from the listing: no file was modified within the target's ``settle_s`` of the host's clock
    (``now``) and, with ``max_age``, the newest is not older; the newest modification time, in UTC."""
    now = time.time() if now is None else now
    newest = max(f.stat.st_mtime for f in found if f.stat is not None)
    age = now - newest
    if age < target.settle_s:
        raise NotReady(
            "unsettled",
            f"{who}: a file changed {max(int(age), 0)} s ago, inside the {target.settle_s} s target"
            f" {target.target_id} waits (settle_s)",
        )
    if max_age is not None and age > parse_duration(max_age):
        raise NotReady(
            "stale",
            f"{who}: the newest file is {_age(age)} old, over the {max_age} the spec allows (file.maxAge):"
            " the export did not arrive",
        )
    return _utc(newest)


async def stamp(target: Target, path: str, name: str, who: str, *, end: float | None = None) -> str:
    """Step 3: the instant the stamp file ``name``, in the path's fixed directory, declares."""
    where = os.path.join(target.root or "", *_fixed(path)[0], name)
    found = await call(_stamped, where, end=end, root=target.root)
    if found == "absent":
        raise NotReady(
            "unstamped", f"{who}: the stamp file is not there: the export is not complete, or the spec names"
            " another file"
        )  # fmt: skip
    if found == "malformed":
        raise NotReady("unstamped", f"{who}: the stamp file must hold one ISO-8601 time with a UTC offset")
    if found == "unread":
        raise NotReady(
            "unstamped", f"{who}: the stamp file is a symbolic link, may not be read or is over 4,096 bytes"
        )
    return found


def _key(found: os.stat_result) -> tuple[int, int, int, int]:
    return found.st_size, found.st_mtime_ns, found.st_ctime_ns, found.st_ino


class _Moved(Exception):
    pass


class Damaged(Exception):
    """A compressed export's stream is cut short or damaged: its class is what the plan names."""

    def __init__(self, stream: str) -> None:
        super().__init__("BadGzipFile" if stream == "gzip" else "ZstdError")


class _Stream:
    """A gzip or zstd export's stream, checked as the file is hashed: each member read to its end and its
    checksum, by Python's zlib or by zstandard. DuckDB reads a stream cut short, or a damaged gzip one, as
    fewer rows without an error (observed on 1.5.5), so its rows would be lost silently. What
    it decompresses is dropped a slice at a time, so a stream that expands far holds no memory."""

    SLICE = 256  # compressed bytes of a zstd step, which expands to some MiB at most (gzip's is bounded)

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._open = self._new()

    def _new(self) -> Any:
        return (
            zlib.decompressobj(31) if self._kind == "gzip" else zstandard.ZstdDecompressor().decompressobj()
        )

    def feed(self, chunk: bytes) -> None:
        try:
            step = len(chunk) if self._kind == "gzip" else self.SLICE
            for at in range(0, len(chunk), step):
                data = chunk[at : at + step]
                while data:
                    if self._open.eof:  # its member ended: another begins
                        self._open = self._new()
                    if self._kind == "gzip":
                        self._open.decompress(data, CHUNK)
                        left = self._open.unconsumed_tail
                    else:
                        self._open.decompress(data)
                        left = b""
                    # At a member's end what follows it, else what the output limit left unread: never both
                    # (zlib leaves the bytes after a member in both, so their sum fed the next member twice).
                    data = self._open.unused_data if self._open.eof else left
        except (zlib.error, zstandard.ZstdError):
            raise Damaged(self._kind) from None

    def close(self) -> None:
        if not self._open.eof:  # cut short
            raise Damaged(self._kind)


def _digest(
    path: str, cancel: threading.Event, read: list[float], cached: bool, stream: str | None = None
) -> tuple[os.stat_result, str, bool, Footed | None]:
    """The file's ``fstat`` and SHA-256, opened without following a link and read a ``CHUNK`` at a time,
    ``cancel`` read at each and the time of each read kept in ``read``, whether the mount it lies on is
    read-only (its descriptor's, which may be another than the root's), and what a ``parquet`` file's
    footer holds; ``_Moved`` when its ``fstat`` after the last read is not the one before the first. With
    ``cached``, the process's hash of a file whose change time differs from its modification time (one
    renamed into place) stands for reading it. A ``stream`` (``gzip``, ``zstd``) is checked as it is read:
    ``Damaged`` when it is cut short or broken; a ``parquet`` file's footer is walked once it is read, and
    again where its hash stands for reading it (``BadFooter``)."""
    fd, parquet = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), stream == "parquet"
    try:
        before = os.fstat(fd)
        read_only = bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        key = (before.st_dev, *_key(before), stream)
        if cached and before.st_ctime_ns != before.st_mtime_ns and key in _HASHED:
            return before, _HASHED[key], read_only, _footer(fd, before.st_size) if parquet else None
        digest, size = hashlib.sha256(), 0
        checked = _Stream(stream) if stream in ("gzip", "zstd") else None
        while chunk := os.read(fd, CHUNK):
            read[0] = time.monotonic()
            if cancel.is_set():
                raise _Moved
            digest.update(chunk)
            if checked is not None:
                checked.feed(chunk)
            size += len(chunk)
        if checked is not None:
            checked.close()
        footed = _footer(fd, size) if parquet else None
        if _key(os.fstat(fd)) != _key(before) or size != before.st_size:
            raise _Moved
    finally:
        os.close(fd)
    sha = digest.hexdigest()
    if before.st_ctime_ns != before.st_mtime_ns:
        if len(_HASHED) >= CACHE_MAX:
            _HASHED.pop(next(iter(_HASHED)))
        _HASHED[key] = sha
    return before, sha, read_only, footed


class BadFooter(Exception):
    """A Parquet file DuckDB is not handed: its footer is over ``FOOTER_MAX``, its
    schema over ``FOOTER_ELEMENTS`` or nested over ``FOOTER_DEPTH``, it holds over ``FOOTER_GROUPS`` row
    groups or ``FOOTER_CHUNKS`` column chunks, a row group's chunks are not the schema's leaves, or the
    footer is one this module does not walk to its end exactly as a Thrift reader reads it; ``encrypted``
    when its footer, or a column of it, is."""


def _varint(data: bytes, at: int, bits: int = 64) -> tuple[int, int]:
    """The number at ``at`` and where it ends. One of over ten bytes is refused, and one of over ``bits``
    bits: a Thrift reader cuts it to that width, and would take another number than this walk."""
    value = shift = 0
    while True:
        value, at, shift, more = value | (data[at] & 0x7F) << shift, at + 1, shift + 7, data[at] & 0x80
        if not more and not value >> bits:
            return value, at
        if not more or shift > 63:
            raise ValueError


def _field(data: bytes, at: int, last: int) -> tuple[int, int, int]:
    """The field whose header is at ``at``, after the field ``last``: its id, its Thrift compact type and
    where its value starts. The ids of a struct ascend, as every writer writes them: a field written twice
    (a reader takes the last), one out of order and an id a reader would cut to 16 bits are refused."""
    kind, field, at = data[at] & 15, last + (data[at] >> 4), at + 1
    if field == last:  # its id written out, a zigzag number
        moved, at = _varint(data, at, 16)
        field = moved >> 1 ^ -(moved & 1)
    if not last < field < 2**15:
        raise ValueError
    return field, kind, at


def _elements(data: bytes, at: int, of: Any, left: list[int]) -> tuple[int, int]:
    """The list whose header is at ``at``: how many elements it holds and where they start. Thrift's
    generated reader reads the elements as the type parquet.thrift declares for the list's field (``of``),
    whatever type the list itself names (observed on 1.5.5), and passes over a list it does not know by the
    type named: so a list where none is declared, and one that names another type, are refused. The reader
    makes the elements before it reads them: the footer's lists hold ``FOOTER_ITEMS`` in all, what is
    ``left`` of them counted down here."""
    size, kind, at = data[at] >> 4, data[at] & 15, at + 1
    if size == 15:
        size, at = _varint(data, at, 31)
    left[0] -= size
    if kind != (12 if isinstance(of, dict) else of) or left[0] < 0:
        raise ValueError
    return size, at


def _value(data: bytes, at: int, kind: int, of: Any, depth: int, left: list[int]) -> int:
    """Where the value of Thrift compact type ``kind`` at ``at`` ends; ``of`` is what parquet.thrift declares
    there when it is a list (its elements) or a struct on the way to one. A set, a map and a type Thrift
    does not know are refused, and a text's length a reader would cut."""
    if kind in (1, 2):  # a boolean is in its field's header
        return at
    if kind in (3, 7):
        return at + (1 if kind == 3 else 8)
    if kind in (4, 5, 6):
        return _varint(data, at)[1]
    if kind == 8:
        size, at = _varint(data, at, 31)
        return at + size
    if kind == 12:
        return _struct(data, at, of[0] if isinstance(of, tuple) else _PLAIN, depth + 1, left)
    if kind != 9:
        raise ValueError
    size, at = _elements(data, at, of, left)
    for _ in range(size):
        at = _value(data, at, 12 if isinstance(of, dict) else of, (of,), depth, left)
    return at


def _number(data: bytes, at: int, bits: int = 64) -> tuple[int, int]:
    """The signed number at ``at``, a zigzag one of ``bits`` bits at most, and where it ends."""
    number, at = _varint(data, at, bits)
    return number >> 1 ^ -(number & 1), at


def _struct(data: bytes, at: int, lists: dict[int, Any], depth: int, left: list[int]) -> int:
    """Where the Thrift compact struct at ``at`` ends, ``lists`` what parquet.thrift declares in it. Structs
    nest ``FOOTER_STRUCTS`` deep at most."""
    if depth > FOOTER_STRUCTS:
        raise ValueError
    field = 0
    while data[at]:
        field, kind, at = _field(data, at, field)
        at = _value(data, at, kind, lists.get(field), depth, left)
    return at + 1


def _element(data: bytes, at: int, left: list[int]) -> tuple[bytes, int, int]:
    """The schema element at ``at``: its name, its count of children (its field 5 where that is an i32: a
    reader takes it from no other type; 0 without one) and where it ends. A negative number among its
    type, type length, repetition, children, converted type, scale and precision (its fields 1 to 8 that
    are an i32), and one a reader would cut to 32 bits, are refused: DuckDB binds a negative type length,
    scale or precision as an internal error, which invalidates its database (observed on 1.5.5)."""
    field, name, children = 0, b"", 0
    while data[at]:
        field, kind, start = _field(data, at, field)
        if kind == 5 and field < 9:
            number, at = _number(data, start, 32)
            if number < 0:
                raise ValueError
            children = number if field == 5 else children
        else:
            at = _value(data, start, kind, None, 1, left)
            if (field, kind) == (4, 8):
                name = data[_varint(data, start, 31)[1] : at]
    return name, children, at + 1


def _schema(data: bytes, at: int, left: list[int]) -> tuple[list[list[bytes]], int]:
    """The schema whose list is at ``at``: its leaves (the elements without children, but the root), each by
    its path below the root, in the schema's order, and where the list ends. Refused: over
    ``FOOTER_ELEMENTS`` elements, groups nested over ``FOOTER_DEPTH``, what ``_element`` refuses, and
    elements that are not exactly the root's tree."""
    elements, at = _elements(data, at, _PLAIN, left)
    if elements > FOOTER_ELEMENTS:
        raise ValueError
    open_: list[int] = []  # of each group still open, the children yet to come
    path: list[bytes] = []  # and its name
    leaves = []
    for n in range(elements):
        name, children, at = _element(data, at, left)
        if n and not open_:
            raise ValueError
        if open_:
            open_[-1] -= 1
        if children:
            open_.append(children)
            path.append(name)
        elif n:
            leaves.append([*path[1:], name])
        if len(open_) > FOOTER_DEPTH:
            raise ValueError
        while open_ and not open_[-1]:
            open_.pop()
            path.pop()
    if open_ or not elements:
        raise ValueError
    return leaves, at


def _chunk(data: bytes, at: int, leaf: list[bytes], left: list[int]) -> tuple[int, int]:
    """The column chunk at ``at``, which must be ``leaf``'s (its metadata's path is the leaf's): the
    uncompressed size its metadata gives, none negative, and where the chunk ends. ``BadFooter('encrypted')``
    for one that holds ``crypto_metadata``: DuckDB's read of such a column's rows is an internal error."""
    field, size, named = 0, 0, False
    while data[at]:
        field, kind, at = _field(data, at, field)
        if field == 8:
            raise BadFooter("encrypted")
        if (field, kind) != (3, 12):
            at = _value(data, at, kind, None, 2, left)
            continue
        inner = 0
        while data[at]:  # its ColumnMetaData
            inner, kind, at = _field(data, at, inner)
            if (inner, kind) == (6, 6):
                size, at = _number(data, at)
                if size < 0:
                    raise ValueError
            elif (inner, kind) != (3, 9):
                at = _value(data, at, kind, _COLUMN.get(inner), 3, left)
            else:
                names, at = _elements(data, at, 8, left)
                if names != len(leaf):
                    raise ValueError
                for name in leaf:
                    length, at = _varint(data, at, 31)
                    if data[at : at + length] != name:
                        raise ValueError
                    at += length
                named = True
        at += 1
    if not named:
        raise ValueError
    return size, at + 1


def _groups(data: bytes, at: int, leaves: list[list[bytes]], left: list[int]) -> tuple[list[int], int]:
    """The row groups whose list is at ``at``: each leaf's uncompressed bytes in all of them, and where the
    list ends. A group's column chunks are the schema's leaves, as many and each by its path, in the
    schema's order: DuckDB takes a leaf's chunk by its position. Refused: over ``FOOTER_GROUPS`` row groups,
    over ``FOOTER_CHUNKS`` column chunks in all, a group whose chunks are not the leaves, and one whose
    count of rows is negative (DuckDB's ``count(*)`` of such a file is an internal error: observed)."""
    groups, at = _elements(data, at, _PLAIN, left)
    if groups > FOOTER_GROUPS or groups * len(leaves) > FOOTER_CHUNKS:
        raise ValueError
    sizes = [0] * len(leaves)
    for _ in range(groups):
        field, chunks = 0, 0
        while data[at]:
            field, kind, at = _field(data, at, field)
            if (field, kind) != (1, 9):
                if (field, kind) == (3, 6) and _number(data, at)[0] < 0:
                    raise ValueError
                at = _value(data, at, kind, _GROUP.get(field), 1, left)
                continue
            chunks, at = _elements(data, at, _PLAIN, left)
            if chunks != len(leaves):
                raise ValueError
            for i, leaf in enumerate(leaves):
                size, at = _chunk(data, at, leaf, left)
                sizes[i] += size
        if chunks != len(leaves):  # it holds no chunks at all
            raise ValueError
        at += 1
    return sizes, at


def _footer(fd: int, size: int) -> Footed | None:
    """Walk a Parquet file's footer before DuckDB reads it: DuckDB follows a schema's nesting by recursion
    without a bound, so a file of some kilobytes whose groups nest a few thousand deep ended the process, and
    it makes a list's elements before it reads them, so a footer of one megabyte took 672 MiB outside
    ``files.memory`` (observed on 1.5.5). The whole of FileMetaData is walked, to the footer's declared
    length, as Thrift's generated reader reads it, so that the schema DuckDB takes is the one judged here:
    its one field 2, a list of structs; and its row groups, its one field 4. Returned: the rows it
    declares (its field 3) and each top-level column's uncompressed bytes, which the plan takes from here
    (no statement reads a footer's statistics as text: ``parquet_metadata`` ended the process on a TIME
    column's). ``BadFooter``: a footer over ``FOOTER_MAX`` or longer than its file, no schema, a negative
    count of rows, bytes after the footer's end, lists of over ``FOOTER_ITEMS`` elements in all, and whatever
    ``_schema``, ``_element``, ``_groups``, ``_chunk``, ``_field``, ``_elements``, ``_value`` and ``_struct``
    refuse; and an encrypted footer (the file ends ``PARE``). A file that ends in neither is left to DuckDB,
    which refuses it by its last bytes and reads no footer of it (observed): None."""
    tail = os.pread(fd, 8, size - 8) if size >= 12 else b""
    if tail[4:] == b"PARE":
        raise BadFooter("encrypted")
    if tail[4:] != b"PAR1":
        return None
    length = int.from_bytes(tail[:4], "little")
    if length > min(FOOTER_MAX, size - 12):
        raise BadFooter
    data = os.pread(fd, length, size - 8 - length)
    try:
        field = at = rows = 0
        leaves: list[list[bytes]] | None = None
        sizes: list[int] = []
        left = [FOOTER_ITEMS]
        while data[at]:
            field, kind, at = _field(data, at, field)
            if field == 2 and kind == 9:
                leaves, at = _schema(data, at, left)
            elif field == 4 and kind == 9 and leaves is not None:
                sizes, at = _groups(data, at, leaves, left)
            elif field in (2, 4):  # a reader passes over a schema or row groups that are no list: it has none
                raise ValueError
            elif (field, kind) == (3, 6):
                rows, at = _number(data, at)
            else:
                at = _value(data, at, kind, _FOOTER.get(field), 0, left)
        if leaves is None or rows < 0 or at + 1 != length:
            raise ValueError
    except (ValueError, IndexError):
        raise BadFooter from None
    tops = zip(leaves, sizes, strict=False)  # a footer without row groups holds no sizes: DuckDB's to refuse
    return rows, {str(leaf[0], "utf-8", "replace"): held for leaf, held in tops if len(leaf) == 1}


def footed(path: str) -> Footed | None:
    """``_footer`` of the file at ``path``, for ``ds target check``, which reads files it has not pinned."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _footer(fd, os.fstat(fd).st_size)
    finally:
        os.close(fd)


def summed(footers: Iterable[Footed | None]) -> Footed:
    """A side's rows and columns' uncompressed bytes: its files' footers', added up."""
    rows, sizes = 0, Counter[str]()
    for rows_, sizes_ in filter(None, footers):
        rows += rows_
        sizes.update(sizes_)
    return rows, dict(sizes)


def footer_refused(who: str, n: int, of: int, why: BadFooter) -> str:
    """The refusal of a Parquet file DuckDB is not handed: an encrypted one, or one whose footer is not
    walked to its end."""
    if str(why) == "encrypted":
        return f"{who}: file {n} of {of} is encrypted Parquet, which is not read"
    return (
        f"{who}: file {n} of {of} has a Parquet footer that is not read: it is over {FOOTER_MAX // 2**20}"
        f" MiB, its schema holds over {FOOTER_ELEMENTS:,} elements or nests groups over {FOOTER_DEPTH} deep,"
        f" it holds over {FOOTER_GROUPS:,} row groups or {FOOTER_CHUNKS:,} column chunks, or it is not of"
        " the form this build reads"
    )


async def _hashed(
    path: str, *, end: float | None, cached: bool, stream: str | None = None, root: str | None = None
) -> tuple[os.stat_result, str, bool, Footed | None]:
    """``_digest`` on ``POOL``: no read may take ``STAT_S`` (then it is abandoned) and the whole must end by
    ``end`` (then ``Late``); a caller cancelled meanwhile stops it at its next read."""
    cancel, read = threading.Event(), [0.0]
    try:
        return await call(_digest, path, cancel, read, cached, stream, end=end, root=root, read=read)
    finally:
        cancel.set()


def files_sha256(pins: Sequence[Pin], files: Sequence[str] | None = None) -> str:
    """The SHA-256 of each file's hex digest and a newline, in path order: ``cut -c1-64 | sha256sum``
    of ``sha256sum``'s lines gives it. Of a side that declares partitions (``files``: its relative paths),
    whose directories' names are compared values, the SHA-256 of ``sha256sum``'s lines whole (``<hex>
    <path>`` and a newline each), so the digest binds them too."""
    names = [""] * len(pins) if files is None else [f"  {name}" for name in files]
    lines = (f"{pin[4]}{name}\n" for pin, name in zip(pins, names, strict=True))
    return hashlib.sha256("".join(lines).encode()).hexdigest()


# What ``files_sha256`` is the SHA-256 of, with the command that prints it from the files: said wherever a
# pack shows the digest, since it is no file's own, which the holder of one file would compare.
SHA256_OF = (
    "the list of its files' SHA-256 values, a line each, in the byte order of their paths under the target's"
    " root, not a file's own (sha256sum FILE... | cut -c1-64 | sha256sum)"
)
SHA256_OF_PATHS = (  # of a side that declares partitions
    "the list of its files' sha256sum lines, each with its path under the target's root, in the byte order"
    " of those paths, not a file's own (sha256sum PATH... | sha256sum, run in that root)"
)


@dataclass(frozen=True)
class Pinned:
    """A file side's files as its run was created on them: the relative paths and their pins, sealed
    together; and what the stored plan holds of them."""

    files: tuple[str, ...]
    pins: tuple[Pin, ...]
    pinned_at: str
    as_of: str | None
    modified_last: str
    mount: str
    partitioned: bool = False  # its side declares partitions: its digest binds its paths
    footed: Footed = (0, {})  # a Parquet side's rows and columns' bytes, of all its footers: the plan's

    def facts(self) -> dict[str, Any]:
        """The stored plan's members: counts, times and the one aggregate digest, no file's."""
        digest = files_sha256(self.pins, self.files if self.partitioned else None)
        return {
            "file_count": len(self.files), "bytes": sum(pin[0] for pin in self.pins),
            "files_sha256": digest, "pinned_at": self.pinned_at, "as_of": self.as_of,
            "modified_last": self.modified_last, "mount": self.mount,
        }  # fmt: skip


async def pin(
    target: Target,
    path: str,
    who: str,
    files: FilesSettings,
    *,
    partitions: Sequence[str] = (),
    as_of_file: str | None = None,
    max_age: str | None = None,
    now: float | None = None,
    stream: str | None = None,
    end: float | None = None,
) -> Pinned:
    """Steps 1 to 4 of a side's plan, before ``end`` (its run's creation's deadline; ``files.plan_s`` from
    now without one): the root, the expansion, the arrival, and each file opened without following a
    link, hashed and its ``fstat`` compared with the listing's and before and after its reads, its own mount
    read-only as the root's is; a compressed ``stream`` (``gzip``, ``zstd``) checked whole as it is
    hashed, a ``parquet`` file's footer walked. Steps 5 to 8 are DuckDB's (``duck``); step 9 is
    ``verify(..., planning=True)``. A refusal is worded for ``who``."""
    end = end or time.monotonic() + files.plan_s
    pins: list[Pin] = []
    found: list[Found] = []
    footers: list[Footed | None] = []
    try:
        mount = await rooted(target, who, end=end)
        found = await expand(target, path, who, max_files=files.max_files, partitions=partitions, end=end)
        modified = arrival(target, found, who, max_age=max_age, now=now)
        as_of = await stamp(target, path, as_of_file, who, end=end) if as_of_file else None
        pinned_at = _utc(time.time())
        for n, candidate in enumerate(found, 1):
            where = os.path.join(target.root or "", candidate.path)
            try:
                opened, sha, read_only, footed = await _hashed(
                    where, end=end, cached=True, stream=stream, root=target.root
                )
            except (_Moved, FileNotFoundError):
                raise _changed(who, n, len(found)) from None
            except Damaged as exc:
                raise ValueError(
                    f"{who}: its files could not be read while the run was planned ({exc})"
                ) from None
            except BadFooter as exc:
                raise ValueError(footer_refused(who, n, len(found), exc)) from None
            except PermissionError:
                raise ValueError(_refused(who, n, len(found), "unreadable")) from None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValueError(_refused(who, n, len(found), "link")) from None
                raise ValueError(
                    f"{who}: its files could not be read while the run was planned ({type(exc).__name__})"
                ) from None
            if _key(opened) != _key(candidate.stat):  # type: ignore[arg-type]
                raise _changed(who, n, len(found))
            if not (read_only or target.allow_writable):  # a mount below the root's, which step 1 judged
                raise ValueError(
                    f"{who}: file {n} of {len(found)} lies on a mount DeltaSleuth could write to: mount it"
                    " read-only (:ro), or declare allow_writable: true on the target"
                )
            mount = mount if read_only else "writable"
            pins.append((*_key(opened), sha))
            footers.append(footed)
    except Late:
        raise NotReady(
            "planning",
            f"{who}: planning its files passed the {files.plan_s} s files.plan_s allows ({len(pins)} of"
            f" {len(found)} files hashed): try again (a file renamed into place is not hashed twice), or"
            " raise files.plan_s and the proxy's read timeout",
        ) from None
    except TargetUnreachable as exc:
        raise unanswered(exc, who, target) from None
    except OSError:  # a listing or a stat the mount answered with an error
        raise ValueError(_absent(who, target)) from None
    files_ = tuple(f.path for f in found)
    return Pinned(files_, tuple(pins), pinned_at, as_of, modified, mount, bool(partitions), summed(footers))


def _refused(who: str, n: int, of: int, kind: str) -> str:
    return f"{who}: file {n} of {of} (in path order) {_KIND_TEXT[kind].format(uid=os.getuid())}"


def _changed(who: str, n: int, of: int) -> NotReady:
    return NotReady(
        "unsettled",
        f"{who}: file {n} of {of} changed while the run was planned: the export is still being written",
    )


async def verify(
    target: Target,
    path: str,
    side: str,
    files: Sequence[str],
    pins: Sequence[Pin],
    *,
    full: bool = False,
    partitions: Sequence[str] = (),
    planning: bool = False,
) -> None:
    """The path expanded again, and each position's name, kind, size, times and inode compared with its pin;
    ``full`` also hashes each file, never from the cache. ``FilesChanged`` names the first difference; at
    plan (``planning``, step 9) it is ``_changed``'s ``NotReady``. A root that cannot be listed, a call past
    its deadline and a read of the closing hash that the mount answers with an error are
    ``TargetUnreachable`` (a refused permission ``TargetDenied``): no claim that the export changed. So is,
    after the plan, a root that holds no entry at all (``Empty``): a mount point whose mount is not
    there."""
    who, of, root = f"side {side}", len(pins), target.root or ""

    def changed(why: str, n: int) -> Exception:
        if planning:
            return _changed(who, n, of)
        return FilesChanged(f"{who}: file {n} of {of} is not the file the run was created on ({why})")

    try:
        if await call(_rooted, root, root=root) in ("absent", "link"):
            raise TargetUnreachable("OSError")
        found = sorted(await _walk(root, path, None), key=lambda f: f.path.encode())
        if not found and not planning and not await call(_listed, root, root=root):
            raise TargetUnreachable("Empty")
    except _Deep:
        found = []
    except OSError:  # a listing or a stat the mount answered with an error
        raise TargetUnreachable("OSError") from None
    if len(found) != of:
        if planning:  # the first position whose file is not its pin's (the text names one of the plan's)
            moved = [n for n, (f, name) in enumerate(zip(found, files, strict=False), 1) if f.path != name]
            raise _changed(who, min([*moved, of]), of)
        raise FilesChanged(f"{who}: {len(found):,} files match now, {of:,} when the run was created")
    for n, (candidate, name, pinned) in enumerate(zip(found, files, pins, strict=True), 1):
        if candidate.kind != "file":
            raise changed("link" if candidate.kind == "link" else "replaced", n)
        now = _key(candidate.stat)  # type: ignore[arg-type]
        if candidate.path != name or now[3] != pinned[3]:
            raise changed("replaced", n)
        for why, at in (("size", 0), ("modified", 1), ("changed", 2)):
            if now[at] != pinned[at]:
                raise changed(why, n)
    for n, (name, pinned) in enumerate(zip(files, pins, strict=True), 1):
        if full:
            try:
                _, sha, _, _ = await _hashed(os.path.join(root, name), end=None, cached=False, root=root)
            except (_Moved, FileNotFoundError):
                raise changed("changed", n) from None
            except PermissionError:
                raise TargetDenied("PermissionError") from None
            except OSError as exc:  # EIO, a stale handle: the bytes are not known to differ
                if exc.errno == errno.ELOOP:
                    raise changed("link", n) from None
                raise TargetUnreachable("OSError") from None
            if sha != pinned[4]:
                raise changed("content", n)


# -- the spill ------------------------------------------------------------------------------


def held(spill_dir: Path) -> Path:
    """A spill directory of this process's own under ``spill_dir`` (0700), flocked until it ends or gives
    it up (``release``): made under a name the sweep passes over, locked, then renamed, so no sweep removes
    it in between."""
    spill_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = secrets.token_hex(13)
    making = spill_dir / f".{name}"
    making.mkdir(mode=0o700)
    fd = os.open(making, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.rename(making, spill_dir / name)
    _HELD[spill_dir / name] = fd
    return spill_dir / name


def release(spill: Path) -> None:
    """Give up a spill directory this process holds (its instance was discarded, and no cursor or call of
    it is left): removed, and its lock freed."""
    shutil.rmtree(spill, ignore_errors=True)
    if (fd := _HELD.pop(spill, None)) is not None:
        os.close(fd)


def _spills(spill_dir: Path) -> list[os.DirEntry[str]]:
    """The spill directories under ``spill_dir``, every process's, by name."""
    try:
        with os.scandir(spill_dir) as entries:
            return sorted((entry for entry in entries if _SPILL.fullmatch(entry.name)), key=lambda e: e.name)
    except FileNotFoundError:
        return []


def spilled(spill_dir: Path) -> int:
    """The bytes the spill directories under ``spill_dir`` hold on disk: after a sweep, what this
    installation's live sorts spill."""
    blocks = 0
    for entry in _spills(spill_dir):
        for directory, _, names in os.walk(entry.path):
            for name in names:
                with suppress(OSError):  # a sort's file that went meanwhile
                    blocks += os.lstat(os.path.join(directory, name)).st_blocks
    return blocks * 512


def sweep(spill_dir: Path) -> int:
    """Remove the spill directories under ``spill_dir`` whose process ended (their lock is free); how many.
    One that cannot be opened or removed (another uid's) is logged by its position and stops no other; one
    that went meanwhile (its reader ended, another process swept it) is neither. No DuckDB:
    ``Context.open``, the worker's hourly purge pass and a new instance call it."""
    listed, removed = _spills(spill_dir), 0
    for n, entry in enumerate(listed, 1):
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            fd = os.open(entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                # Another descriptor's lock refuses it, this process's own too.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                shutil.rmtree(entry.path)
                removed += 1
            except BlockingIOError:
                pass  # a live process's
            finally:
                os.close(fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("spill directory %d of %d not swept: %s", n, len(listed), type(exc).__name__)
    return removed
