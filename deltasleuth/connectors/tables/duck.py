"""DuckDB behind the files engine (docs/mvp/FILES.md §3, §10; docs/mvp/READER.md): the reader, a child process
that alone loads it (``reader``), its handle and its cursors here, and the self-test the release's smoke
step runs.

No ``ds`` process imports ``duckdb``. Its reader is started at the first file read, never before, by
``subprocess.Popen`` with none of this process's variables (``env={}``), and builds the locked instance: it
reads the declared roots alone and spills in a directory this process holds for it; nothing installs or
loads, external access is off and the configuration is locked before any statement of a run, and it proves
all of that before it answers. Before DuckDB loads it restricts itself (READER.md §7): no core, and on
Linux, unless ``files.os_limits`` is false, a data limit and no socket; ``restricted`` words what its start
saw hold, for its start's log line, ``ds target check`` and the self-test.
The two speak in frames (``reader``'s codec: no pickle, every message bounded and every answer checked,
its rows by their cells, their width and, where a plan keeps or words them, their types, before one is
built; one outside the protocol ends the reader). Its cursors share its memory, its threads and its spill
(U11). A cursor runs one ``SELECT`` a call, on ``manifest.POOL`` under a deadline counted from when a
thread took it: past it the statement is interrupted and given ``GRACE_S`` more, then given up with the
reader, which is retired; another is started, one start whoever asks meanwhile. A retired reader makes no
cursor more: a session that holds one finishes on it, and its process is killed, and its spill directory
given up, with its last cursor. A reader's end is its process's, learnt by reaping it. What waits on a
reader whose process ended is ``Busy('Died')``, and an end this process did not cause, or caused for a
deadline, is accounted (READER.md §9): charged to the file set that alone was in flight, which is read no
more after ``DEATHS`` charges (``ReaderDied``); sets in flight together are read apart next, in a reader of
their task's own. No DuckDB text crosses the pipe (it quotes paths and values): each error becomes a
``TableError`` holding its class's name, raised from nothing. An ``IOException`` is ``TargetRefused`` here;
the session that knows the side's pins stat-checks them before it judges one (§10's table).
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import itertools
import json
import logging
import math
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Future
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any

from deltasleuth.config import FilesSettings, Settings, load_settings
from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.connectors.tables import compose, manifest, reader, render
from deltasleuth.connectors.tables.engine import (
    Asked,
    Column,
    Figure,
    FilesChanged,
    NotReady,
    ReaderDied,
    Refusal,
    Relation,
    Row,
    Rows,
    StatementTimeout,
    TableError,
    TargetDenied,
    TargetRefused,
    TargetTempSpace,
    TargetUnreachable,
    code,
)
from deltasleuth.connectors.tables.keys import Plan, decode_key, key_maps
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.convert import convert
from deltasleuth.core.precheck import FILE_SIDE
from deltasleuth.core.spec import where
from deltasleuth.core.spec.targets import TargetRegistry
from deltasleuth.core.types import SideConfig, Target

SPILL_RESERVE = 2**30  # free beyond files.spill when a reader starts
LOW_SPACE = 64 * 2**20  # an IOException with less free in the spill is temporary space
START_S = 20  # a reader's start, to its first answer; with its file work, ``manifest.STAT_S`` more
GRACE_S = 10  # after an interrupt, before a statement is given up
REAP_S = 10  # after its kill, before a reader's process that has not ended is logged
DRAIN_S = 1  # after its reaping, for what a reader wrote before its end to be read
WATCH_S = 60  # a statement running longer has its files stat-checked, and its reader asked, this often (§10)
DEATHS = 3  # a reader's ends charged to a file set before it is read no more (READER.md §9)
DEADLINE = "killed after its deadline"  # the cause of a kill for a statement given up, or a ping unanswered
BATCH = 10_000  # rows a fetched batch holds at most
BATCH_BYTES = 64 * 2**20  # or this much of their texts and bytes
STEP = 64  # rows a fetch step while a text, bytes or JSON column is read
# U4's sort, which the self-test runs: 200,000 rows of 132 bytes spill, with 2 threads, from 72 MiB to 88
# MiB (observed on 1.5.5, macOS arm64; 70 MiB fails, from 90 MiB the sort fits in memory).
SPILL_SORT = (
    "SELECT md5(CAST(i AS VARCHAR)) || repeat('y', 100) AS k FROM range(200000) t(i) ORDER BY encode(k)"
)
SELFTEST_MIB = 80
# What the self-test sends besides: a statement that does not end in its time, for the fault its reader is
# sent; and one that holds about 1 GiB (1,011 MiB at its peak in 0.8 s, observed as above), for the data
# limit under which it must not complete.
ENDLESS = "SELECT count(*) FROM range(100000000000) t(i) WHERE md5(CAST(i AS VARCHAR)) = ''"
HOLDS = (
    "SELECT count(*) FROM (SELECT i, row_number() OVER (ORDER BY i DESC) AS n FROM range(40000000) t(i))"
    " WHERE n > 0"
)
CSV = b"id,name\n1,a\n2,b\n3,c\n"
PARQUET = base64.b64decode(
    "UEFSMRUAFRwVHCwVBBUAFQYVBgAAAgAAAAQBAQAAAAIAAAAVABUgFSAsFQQVABUGFQYAAAIAAAAEAQEAAABhAQAAAGIVAhk8NQAY"
    "DWR1Y2tkYl9zY2hlbWEVBAAVAiUCGAJpZCUiABUMJQIYBG5hbWUlAAAWBBkcGSwmABwVAhkVABkYAmlkFQAWBBY+Fj4mCDwYBAIA"
    "AAAYBAEAAAAWACgEAgAAABgEAQAAABERAAAAJgAcFQwZFQAZGARuYW1lFQAWBBZCFkImRjwYAWIYAWEWACgBYhgBYRERAAAAFoAB"
    "FgQmCBaAAQAoKER1Y2tEQiB2ZXJzaW9uIHYxLjUuNSAoYnVpbGQgZDhjZGFhMzNmZCkZLBwAABwAAADYAAAAUEFSMQ=="
)  # two rows (1, a), (2, b), as DuckDB 1.5.5 writes them uncompressed
# The reader kept, each reader's open cursors and whether it was dropped, and the ends accounted.
_build = threading.Lock()
_builds: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()  # one start a loop
_instance: Instance | None = None
_key: tuple[Any, ...] | None = None
_readers: set[Instance] = set()  # every reader whose process has not ended: killed as this one exits
# READER.md §9, by a file set's key, in memory only and ``manifest.CACHE_MAX`` keys each: the ends charged
# to a set, with the last one's cause, and the sets that were in flight together when a reader ended.
_charged: dict[str, tuple[int, str]] = {}
_suspects: dict[str, None] = {}
# The free text taken from a reader, each by a grammar of this process's (READER.md §6): its version, its
# fault's class, and the head of a column's type that is none this process reads (``_typed``).
_VERSION = re.compile(r"[0-9.]{1,32}")
_FAULT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_HEAD = re.compile(r'"?([A-Z][A-Z0-9_ ]{0,31})"?(?:[(\[].*)?', re.DOTALL)


class Unstarted(Exception):
    """The reader did not start: ``reason`` is ``guard`` or ``process`` (P27), ``spill_dir`` or ``space``
    (P28)."""

    def __init__(self, reason: str, free: int = 0) -> None:
        super().__init__(reason)
        self.reason, self.free = reason, free


def unstarted(exc: Unstarted, who: str, files: FilesSettings) -> str:
    """P27 or P28, worded for ``who`` (``side a``, or ``target <id>``)."""
    return f"{who}: the file reader did not start: {_unstarted(exc, files)}"


def _unstarted(exc: Unstarted, files: FilesSettings) -> str:
    if exc.reason == "guard":
        return "its guard did not hold, or this build's DuckDB lacks the Parquet reader (see the server log)"
    if exc.reason == "process":
        return "its process did not start or did not answer (see the server log)"
    if exc.reason == "spill_dir":
        return f"files.spill_dir is not a directory DeltaSleuth (uid {os.getuid()}) may write"
    spill = manifest.size_bytes(files.spill)
    return (
        f"its spill volume has {exc.free // 2**30} GiB free, and files.spill ({spill // 2**30} GiB) plus"
        " 1 GiB must fit: lower files.spill or grow the volume"
    )


def _unguarded(why: str, names: Sequence[str] = ()) -> Unstarted:
    """P27, and in the server log which check of the reader's did not hold (``reader.Unguarded``'s code),
    worded here by the names of this process's own constants: no text of DuckDB's."""
    said = {
        "lacks": f"this build's DuckDB lacks {', '.join(names)}, statically linked",
        "unset": f"{', '.join(names)} did not read back as set",
        "probe": "a read outside the declared roots was not refused",
    }
    manifest.log.error("the file reader did not start: %s", said[why])
    return Unstarted("guard")


@dataclass(eq=False)
class Instance:
    """A reader's handle (READER.md §5): its process, the spill directory this process holds for it, its
    DuckDB's version and the limits its start saw hold; under ``_build``, its open cursors and whether it
    was ``dropped`` (``discard``, or its process ended). Three daemon threads serve it: one drains a queue
    into its input, so nothing here blocks writing to a stopped process; one reads its answers and
    completes the slot each one's request waits on; one waits for its process's end, reaps it and ends it
    (``_ended``): by the process, not by the end of its output, which a process it started may hold open."""

    proc: subprocess.Popen[bytes]
    spill: Path
    version: str = ""  # none until it started
    limited: bool = False  # its start was to limit it (``files.os_limits``)
    limit: int | None = None  # the data limit that start saw hold, MiB
    sockets: bool = False  # and whether it saw sockets denied
    dropped: bool = False
    cause: str | None = None  # why its process ended: ``reply``, ``DEADLINE``, a signal's name, its exit code
    killed: bool = False  # by this process and for no cause: its end is not accounted
    ahead: bool = True  # a scan's next batch is read ahead in it (READER.md R9): never in a reader apart

    def __post_init__(self) -> None:
        self._lock = threading.Lock()  # its pending requests and its cause
        self._numbers = itertools.count(1)  # its requests' ids and its cursors' numbers
        self._cursors: dict[int, Cursor] = {}  # open, by number
        self._pending: dict[int, tuple[Future[Any], Cursor | None]] = {}  # a request's slot and its cursor
        self._given: set[int] = set()  # the requests given up past their grace
        self._first: Future[Any] = Future()  # its first answer: ``ready``, or ``unstarted``
        self._fault = ""
        self._out: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._reading = threading.Thread(target=self._read, name="ds-reader-answers", daemon=True)
        self._ending = threading.Thread(target=self._wait, name="ds-reader-end", daemon=True)
        threading.Thread(target=self._write, name="ds-reader-requests", daemon=True).start()
        self._reading.start()
        self._ending.start()

    @property
    def held(self) -> int:
        return len(self._cursors)

    def cursor(self, root: str | None = None, key: str | None = None, who: str = "") -> Cursor:
        """A cursor for statements that read the target whose root is ``root``: the file set ``key``, for
        ``who`` (``side a``); a discarded reader makes none (``Busy('Dropped')``)."""
        return Cursor(self, root, key, who)

    def tell(self, **message: Any) -> None:
        """Queue a frame that takes no answer (its first, an interrupt, a close); one for a process that
        ended is dropped."""
        frame = reader.pack(message, reader.REQUEST_MAX)
        if self.cause is None:
            self._out.put(frame)

    def ask(self, by: Cursor | None = None, /, **request: Any) -> tuple[dict[str, Any], Future[Any]]:
        """Send ``request``, of the cursor ``by``, under an id of its own: the request as sent, and the
        slot its answer completes; ``Busy('Died')`` once the process ended, ``reader.Oversize`` past a
        request's limit."""
        slot: Future[Any] = Future()
        request["id"] = next(self._numbers) & 0xFFFFFFFF  # a frame of rows carries it as a u32
        frame = reader.pack(request, reader.REQUEST_MAX)
        with self._lock:
            if self.cause is not None:
                raise manifest.Busy("Died")
            self._pending[request["id"]] = slot, by
        self._out.put(frame)
        return request, slot

    def forsake(self, number: int) -> Future[Any] | None:
        """Give up request ``number``, unanswered past its grace (READER.md §8): what waits on its slot is
        answered (``Deadline``), and the slot handed back stands for the statement until it answers or the
        process ends; None for a request that has answered."""
        with self._lock:
            if number not in self._pending:
                return None
            slot, by = self._pending[number]
            kept: Future[Any] = Future()
            self._pending[number] = kept, by
            self._given.add(number)
        slot.set_exception(TargetUnreachable("Deadline"))
        return kept

    def end(self, cause: str | None = None) -> None:
        """Kill the process, which takes no request more, and whatever it started (its group: it leads a
        session of its own): for ``cause`` (an answer outside the protocol, a deadline), an end that is
        accounted; without one, this process's own doing (a retired reader, a start that failed), which is
        not. One that was reaped is killed no more: its number is its own no longer."""
        with self._lock:
            self.cause, self.killed = self.cause or cause, self.killed or cause is None
        if self.proc.returncode is None:
            with suppress(OSError):  # none of its group is left but the dead (macOS: EPERM)
                os.killpg(self.proc.pid, signal.SIGKILL)
        self._out.put(None)

    def _write(self) -> None:
        assert self.proc.stdin is not None
        with suppress(OSError), self.proc.stdin as sink:  # a broken pipe: the process ended
            while (frame := self._out.get()) is not None:
                sink.write(frame)
                sink.flush()
        self._ending.join(REAP_S)  # ended, or killed: reaped by the thread that waits for it
        if self.proc.returncode is None:  # uninterruptible I/O on a dead mount
            manifest.log.warning(
                "the file reader's process %d did not end within %s s of being killed: its reads stay"
                " counted",
                self.proc.pid,
                REAP_S,
            )

    def _read(self) -> None:
        assert self.proc.stdout is not None
        limits = {b"J": reader.ANSWER_MAX, b"R": reader.FRAME_MAX}
        try:
            with self.proc.stdout as source:
                while (frame := reader.read(source, limits)) is not None:
                    self._took(*frame)
        except Exception:  # outside the protocol: an over-long frame, a nesting too deep, an unknown id
            self.end("reply")

    def _wait(self) -> None:
        """The process's end, on the one thread that reaps it (``ds serve`` is PID 1 in the image). What
        is left of its group is killed at once: a process it started would hold its output open, and
        outlive it. What it wrote before its end is read first (``DRAIN_S``; the end of an output that
        another process holds open is waited for no longer), then its end is made (``_ended``)."""
        self.proc.wait()
        with suppress(OSError):  # none is left
            os.killpg(self.proc.pid, signal.SIGKILL)
        self._reading.join(DRAIN_S)
        _ended(self)

    def _took(self, kind: bytes, body: bytes) -> None:
        """An answer, handed to the slot of the request it names (a frame of rows starts with its id): one
        to a request given up completes the slot that stands for it, and is dropped. The first that names
        none is the start's; a ``fault`` is the process's last. Any other raises."""
        found: Any = body if kind == b"R" else reader.unpack(body)
        number = int.from_bytes(body[:4], "little") if kind == b"R" else found.get("id")
        if number is None and "fault" in found:
            if set(found) != {"fault", "line"} or type(found["line"]) is not int:
                raise ValueError("fault")
            if not _FAULT.fullmatch(found["fault"]):
                raise ValueError("fault")
            self._fault = f": {found['fault']} at reader.py line {found['line']}"
        elif number is None and not self._first.done():
            self._first.set_result(found)
        else:
            with self._lock:
                slot, _ = self._pending.pop(number)
                self._given.discard(number)
            slot.set_result((kind, found))


def _ended(instance: Instance) -> None:
    """At a reader's end, once, on the thread that reaped its process: its spill directory given up, its
    end accounted (``_accounted``) unless this process killed it for no cause with no statement given up
    in it, and what waited for an answer failed (``Busy('Died')``). It is the process's reader no more."""
    global _instance, _key
    status = instance.proc.wait()
    with _build:
        if _instance is instance:
            _instance = _key = None
        instance.dropped = True
    with instance._lock:
        pending, instance._pending = instance._pending, {}
        late = instance.killed and not instance._given.isdisjoint(pending)  # killed with one given up
        own = instance.killed and instance.cause is None and not late
        instance.cause = instance.cause or (DEADLINE if late else _cause(status))
    manifest.release(instance.spill)
    try:
        if instance.version and not own:  # one that never started is P27's, and logged as that
            _accounted(instance, [by for _, by in pending.values()])
    finally:
        for slot in (instance._first, *(slot for slot, _ in pending.values())):
            if not slot.done():
                slot.set_exception(manifest.Busy("Died"))
        instance._out.put(None)  # its writing thread ends
        with _build:
            _readers.discard(instance)


def _accounted(instance: Instance, flights: Sequence[Cursor | None]) -> None:
    """A reader's end, accounted for the file sets in flight in it (READER.md §9): those of its requests
    then unanswered (``flights``: their cursors) and of its cursors with a batch read ahead; if none, of its
    cursors whose statement was not read to its end. One set alone is charged the end, a kill for a
    deadline only where its root answers a stat (a dead mount stays an outage); several are each suspect,
    and read apart next. The log names the cause and counts, never a set."""
    with _build:
        held = list(instance._cursors.values())
    flying = dict.fromkeys([*flights, *(cursor for cursor in held if cursor._ahead)])
    reading = [by for by in flying if by and by._key] or [by for by in held if by._unread and by._key]
    keys = {by._key for by in reading}
    ended = f"the file reader's process ended ({instance.cause}{instance._fault})"
    if not keys:
        manifest.log.warning("%s with no statement in flight", ended)
    elif len(keys) > 1:
        with _build:
            for key in keys:
                _noted(_suspects, key, None)
        manifest.log.error(
            "%s with %d statements in flight: each one's files are read apart next", ended, len(reading)
        )
    elif instance.cause != DEADLINE or _answers(reading[0]._root):
        with _build:
            count = min(_charged.get(reading[0]._key, (0, ""))[0] + 1, DEATHS)
            _noted(_charged, reading[0]._key, (count, instance.cause))
        manifest.log.error(
            "%s while only one side's files were being read: %d of the %d after which they are read no more",
            ended, count, DEATHS,
        )  # fmt: skip


def _noted(held: dict[Any, Any], key: Any, value: Any) -> None:
    """``key`` kept in ``held``, which holds ``manifest.CACHE_MAX`` keys: its oldest goes for a new one."""
    if key not in held and len(held) >= manifest.CACHE_MAX:
        held.pop(next(iter(held)))
    held[key] = value


def _answers(root: str | None) -> bool:
    """Whether a stat of ``root`` answers within ``manifest.STAT_S``; one that does not is abandoned as
    any file work is, counted until it returns."""
    try:
        manifest._admitted(root)
    except manifest.Stuck:
        return False
    job = manifest.POOL.submit(manifest._rooted, root)
    try:
        job.result(manifest.STAT_S)
    except TimeoutError:
        if not job.cancel():
            manifest._abandoned(job, root)
        return False
    return True


def _doubted(key: str | None) -> bool:
    """Whether a reader ended on the file set ``key``, alone or beside others: it is read apart."""
    with _build:
        return key in _suspects or key in _charged


def _condemned(key: str | None, who: str) -> ReaderDied | None:
    """F12, and P35 at plan (READER.md §12), of a file set charged ``DEATHS`` ends, worded for ``who``
    (``side a``) with the last one's cause; None for any other set."""
    with _build:
        count, cause = _charged.get(key or "", (0, ""))
    if count < DEATHS:
        return None
    return ReaderDied(
        f"{who}: the file reader's process ended {DEATHS} times ({cause}) while only this side's files were"
        " being read: they are read no more until DeltaSleuth restarts"
    )


def _cause(status: int) -> str:
    """How a process ended: the signal's name (``signal 35`` for one without), or its exit code."""
    if status >= 0:
        return f"exit {status}"
    with suppress(ValueError):
        return signal.Signals(-status).name
    return f"signal {-status}"


def _stopped(made: Instance) -> None:
    """Kill a reader and wait for its end: its process reaped and its spill directory given up, by the
    thread that waits for it."""
    made.end()
    made._ending.join(GRACE_S)


@atexit.register
def _exited() -> None:
    """As this process exits, each reader it still has is killed and reaped. One left to end by itself, at
    the end of its input, would be left to whatever started this process, which may wait for none: under
    ``docker compose exec`` a command's reader would stay a zombie entry of the container."""
    with _build:
        left = list(_readers)
    for made in left:
        made.end()
    for made in left:
        made._ending.join(REAP_S)


def _failed(made: Instance, why: str) -> Unstarted:
    """P27's second text of a reader whose process did not start as it must, and that in the server log;
    what was started is killed, and its spill directory given up."""
    _stopped(made)
    manifest.log.error("the file reader did not start: its process %s", why)
    return Unstarted("process")


def _readied(first: Mapping[str, Any], limit: int | None) -> Unstarted | None:
    """A reader's first answer, checked: None for ``ready``, which reports no limit but ``limit``, the one
    asked for, and no socket filter where none was; P27 for ``unstarted``, whose names are of this
    process's own constants. Any other exception: the answer is not of the protocol."""
    if "unstarted" in first:
        known = {"lacks": reader.NEEDED, "unset": (*reader.GUARD, *reader.CACHES), "probe": ()}
        names = first["names"]
        if set(first) != {"unstarted", "names"} or not isinstance(names, list):
            raise ValueError("unstarted")
        if not set(names) <= set(known[first["unstarted"]]):
            raise ValueError("names")
        return _unguarded(first["unstarted"], names)
    if (
        set(first) != {"ready", "version", "limit", "sockets", "inherited"}
        or first["ready"] is not True
        or not _VERSION.fullmatch(first["version"])
        or (type(first["limit"]), first["limit"]) not in ((int, limit), (type(None), None))
        or type(first["sockets"]) is not bool
        or (first["sockets"] and limit is None)
        or type(first["inherited"]) is not int
    ):
        raise ValueError("ready")
    return None


def _spawned(
    spill_dir: Path, roots: Sequence[str], probe: Path, memory_mib: int, spill_mib: int, threads: int,
    limit: int | None = None,
) -> Instance:  # fmt: skip
    """A reader started and proved (READER.md §5) that reads ``roots`` and spills in a directory of its own
    under ``spill_dir``, made and held here; with ``limit`` (MiB) it sets itself that data limit and denies
    itself sockets, where its platform has them (READER.md §7; ``restricted`` says what its start saw).
    ``Unstarted`` when its spill directory cannot be made, its process does not start or answer (within
    ``START_S``), its guard does not hold (``probe``, a file outside the roots, must be refused) or the
    spill's file system lacks ``spill_mib`` plus ``SPILL_RESERVE`` free, what the live sorts of this
    installation (its other processes', a discarded reader's) spill there counted as free: they spill
    within the same budget. A failed start leaves no process and no directory. Run by its path, the child
    imports no ``deltasleuth`` package; its error output is discarded (DuckDB's text quotes paths and
    values), and a session of its own keeps a terminal's Ctrl-C from it. The sweep of ended processes'
    spill is best effort: only this reader's own directory decides its start."""
    with suppress(OSError):
        manifest.sweep(spill_dir)
    try:
        spill = manifest.held(spill_dir)
    except OSError:
        raise Unstarted("spill_dir") from None
    try:
        proc = subprocess.Popen(  # noqa: S603 - this interpreter and this package's own module, by path
            [sys.executable, "-I", "-X", "utf8", reader.__file__], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, close_fds=True,
            start_new_session=True, cwd=spill,
        )  # fmt: skip
    except OSError as exc:
        manifest.release(spill)
        manifest.log.error(
            "the file reader did not start: its process could not be started (%s)", type(exc).__name__
        )
        raise Unstarted("process") from None
    made = Instance(proc, spill, limited=limit is not None)
    with _build:
        _readers.add(made)
    made.tell(
        roots=list(roots), spill=str(spill), memory=memory_mib, spilled=spill_mib, threads=threads,
        limit=limit, filter=made.limited, frame_max=reader.FRAME_MAX, probe=str(probe),
    )  # fmt: skip
    try:
        first = made._first.result(START_S)
        unguarded = _readied(first, limit)
    except TimeoutError:
        raise _failed(made, f"did not answer within {START_S} s") from None
    except manifest.Busy:  # its output ended first
        if made.cause == "reply":
            raise _failed(made, "answered outside the protocol") from None
        raise _failed(made, f"ended as it started ({made.cause}{made._fault})") from None
    except Exception:
        made.end("reply")
        raise _failed(made, "answered outside the protocol") from None
    if unguarded is not None:
        _stopped(made)
        raise unguarded
    free = _free(spill) + manifest.spilled(spill_dir)
    if free < spill_mib * 2**20 + SPILL_RESERVE:
        _stopped(made)
        raise Unstarted("space", free)
    made.version, made.limit, made.sockets = first["version"], first["limit"], first["sockets"]
    if first["inherited"]:
        manifest.log.warning("the file reader inherited %d descriptor(s)", first["inherited"])
    return made


def _limit(memory_mib: int, threads: int) -> int:
    """A reader's data limit, MiB (READER.md R7): its memory, 2 GiB and 600 MiB a thread (U31's footers,
    DEPLOY's 1 GiB, the stacks of 24 requests, one batch)."""
    return memory_mib + 2048 + 600 * threads


def restricted(made: Instance) -> tuple[str, str]:
    """READER.md §11's two phrases of a reader, each as its start saw it: its data limit, and that sockets
    are denied it; or why not: ``files.os_limits`` is false, its platform has none (a data limit is
    Linux's, the socket filter of its machines of ``reader.FILTERED``), or it was tried and not seen to
    hold."""
    here = reader.LINUX, reader.LINUX and os.uname().machine in reader.FILTERED
    data, sockets = (
        "files.os_limits is false" if not made.limited
        else "not held on this host" if tried else "not on this platform"
        for tried in here
    )  # fmt: skip
    return (
        f"no data limit ({data})" if made.limit is None else f"data limit {made.limit:,} MiB",
        "sockets denied" if made.sockets else f"sockets not denied ({sockets})",
    )


def _started(settings: Settings, roots: Sequence[str], apart: bool = False) -> Instance:
    """A reader of ``settings``' budget, its start logged with what it saw hold: as a warning, the lowest
    level every ``ds`` process prints (none configures this log), so that the line is there whatever held.
    A reader ``apart`` (§9: one a read of a charged or suspect file set) says so only where a limit did
    not hold: a set's later groups would else write one line each."""
    files = settings.files
    memory = manifest.size_bytes(files.memory) // 2**20
    made = _spawned(
        settings.spill_dir, roots, settings.targets_file, memory, manifest.size_bytes(files.spill) // 2**20,
        files.threads, _limit(memory, files.threads) if files.os_limits else None,
    )  # fmt: skip
    held = ", ".join(restricted(made))
    level = logging.INFO if apart and made.limit is not None and made.sockets else logging.WARNING
    manifest.log.log(
        level, "the file reader started: duckdb %s in process %d, %s", made.version, made.proc.pid, held
    )
    return made


async def _begun(settings: Settings, roots: Sequence[str], apart: bool = False) -> Instance:
    """A reader started on ``manifest.POOL`` and proved, reading ``roots``; ``Unstarted`` when it does not
    start, within ``START_S`` and the ``manifest.STAT_S`` of its file work too (P27's second text, never
    P14's). One whose start ended after its caller had left is retired."""
    started: list[Instance] = []  # under ``_build``, with whether its caller has left
    left = threading.Event()

    def start() -> Instance:
        made = _started(settings, roots, apart)
        with _build:
            started.append(made)
            late = left.is_set()
        if late:
            discard(made)
        return made

    try:
        return await manifest.call(start, timeout_s=START_S + manifest.STAT_S)
    except BaseException as exc:  # its deadline, or a caller cancelled: what was started is retired
        with _build:
            left.set()
        for made in started:
            discard(made)
        if type(exc) is not TargetUnreachable:  # no deadline: ``Unstarted``, no thread, a cancel
            raise
        ready_s = START_S + manifest.STAT_S
        manifest.log.error("the file reader did not start: its process was not ready within %s s", ready_s)
        raise Unstarted("process") from None


async def instance(settings: Settings, roots: Sequence[str]) -> Instance:
    """The process's reader, started at its first use, reading ``roots`` (every files target's) and proved
    before it is handed out; ``Unstarted`` when it does not start (``_begun``). Those who ask while it
    starts wait for that one start (D9: one reader, one budget). A new budget or root list starts another
    (one process holds one: its settings and targets are read at its start), and the reader it replaces is
    retired."""
    global _instance, _key
    files = settings.files
    key = (tuple(roots), settings.spill_dir, files.memory, files.spill, files.threads, files.os_limits)
    async with _builds.setdefault(asyncio.get_running_loop(), asyncio.Lock()):
        with _build:
            if _instance is not None and _key == key:
                return _instance
        built = await _begun(settings, roots)
        with _build:
            replaced, _instance, _key = _instance, built, key
        if replaced is not None:
            discard(replaced)
        return built


def discard(dropped: Instance) -> None:
    """Retire ``dropped`` (a statement of it was given up, a fatal error, or it is the process's reader no
    more): the next use starts another. It makes no cursor more. A session that holds one of its cursors
    finishes on it (another target's scan pass in flight is not lost for one target's hung statement), so
    two budgets run side by side until those cursors are closed; with the last, at once when it has none,
    its process is killed, and its spill directory goes with it (``_ended``): what a sort of it still
    spills stays counted until then (``manifest.spilled``)."""
    global _instance, _key
    with _build:
        if _instance is dropped:
            _instance = _key = None
        dropped.dropped = True
        if not dropped.held:
            dropped.end()


def _taken(instance: Instance, cursor: Cursor) -> None:
    """A session takes a cursor of ``instance``, which a discarded one refuses."""
    with _build:
        if instance.dropped:
            raise manifest.Busy("Dropped")
        instance._cursors[cursor._number] = cursor


def _returned(instance: Instance, cursor: Cursor) -> None:
    """A cursor of ``instance`` was closed: with the last of a discarded one its process is killed."""
    with _build:
        del instance._cursors[cursor._number]
        if instance.dropped and not instance.held:
            instance.end()


def _error(kind: str, name: str, spill: Path) -> TableError:
    """A reader's refusal, by its kind, as a neutral error holding its class's name (§10)."""
    if kind == "memory":
        return TargetTempSpace(name)  # the memory limit, or the spill at its limit (observed)
    if kind == "interrupt":
        return StatementTimeout(name)
    if kind == "denied":
        return TargetDenied(name)
    if kind == "io" and _free(spill) < LOW_SPACE:
        return TargetTempSpace(name)
    return TargetRefused("not one SELECT" if kind == "select" else name)


def _free(path: Path) -> int:
    """Bytes free to this process on ``path``'s file system; none when it cannot be read."""
    try:
        found = os.statvfs(path)
    except OSError:
        return 0
    return found.f_bavail * found.f_frsize


class _Refused(Exception):
    """A reader's answer that refuses its request: the refusal's kind and its name."""


def _typed(kind: str) -> str | None:
    """A column's type as this process keeps and words it (READER.md §6), by a grammar of its own: one it
    reads (of ``_CLASSES``, a DECIMAL(p,s)) as it is; a list as LIST and an array as ARRAY; any other by
    its head, the word before its parameters (STRUCT, MAP, UNION, ENUM), which may hold a file's text: a
    struct's field names, an enum's values. None for a text that is no type's."""
    if kind in _CLASSES or _SCALED.fullmatch(kind):
        return kind
    if kind.endswith("]"):
        return "LIST" if kind.endswith("[]") else "ARRAY"
    found = _HEAD.fullmatch(kind)
    return found[1] if found else None


def _checked(
    request: Mapping[str, Any], kind: bytes, found: Any, width: int | None = None, tags: str | None = None
) -> Any:
    """What the reader's answer to ``request`` holds (READER.md §6): a fetch's rows, no more than it asked
    for nor than ``reader.CELLS`` cells, of its statement's ``width`` and under ``tags`` where those are
    known; a statement's columns, as many as that width, each type as ``_typed`` keeps it; else nothing.
    ``_Refused`` for a refusal. Any other exception: the answer is not of the protocol (another request's,
    a member of another type, a name outside ``reader.NAMES``, rows that do not decode, a type's text that
    is none)."""
    if kind == b"R":
        return reader.unpack_rows(found, request["rows"] + request["step"], width, tags)[1]
    if type(found["id"]) is not int or found["id"] != request["id"]:
        raise ValueError("id")
    if "error" in found:
        if set(found) != {"id", "error", "kind"} or found["error"] not in reader.NAMES:
            raise ValueError("error")
        if found["kind"] not in reader.KINDS:
            raise ValueError("kind")
        raise _Refused(found["kind"], found["error"])
    if request["op"] == "execute" and set(found) == {"id", "ok"} and found["ok"] is True:
        return None
    columns = found["columns"]
    if request["op"] != "columns" or set(found) != {"id", "columns"} or not isinstance(columns, list):
        raise ValueError("answer")
    if not all(isinstance(c, list) and len(c) == 2 and all(isinstance(t, str) for t in c) for c in columns):
        raise ValueError("columns")
    typed = [(name, _typed(declared)) for name, declared in columns]
    if len(typed) != (width or len(typed)) or not all(declared for _, declared in typed):
        raise ValueError("types")
    return typed


class Cursor:
    """A cursor of a reader, for the target whose root is ``root``, known to its process by a number: one
    statement at a time, each a single ``SELECT`` (U3: DuckDB itself runs ``ATTACH``, ``COPY TO`` and
    ``EXPORT`` into an allowed directory, so the reader runs no other statement)."""

    def __init__(
        self, instance: Instance, root: str | None = None, key: str | None = None, who: str = ""
    ) -> None:
        self._instance, self._number, self._root = instance, next(instance._numbers), root
        self._key, self._who = key, who  # the file set it reads (READER.md §9), and for whom
        self.lock = asyncio.Lock()
        self._open, self._sending = True, threading.Lock()  # no request is sent after its close
        self._flight: int | None = None  # the request it sent last
        self._unread = False  # its statement is not read to its end
        self._ahead = False  # its reader reads the statement's next batch ahead (READER.md R9)
        self._width: int | None = None  # its statement's columns, once an answer has said how many
        _taken(instance, self)

    def _asked(self, op: str, tags: str | None = None, /, **members: Any) -> Any:
        """A request of this cursor and what its answer holds, on the pool's thread, which waits for it;
        ``tags``: what each column of a fetch's rows must cross as (``reader.pack_rows``). A refusal is
        §10's error by its kind; a fatal one invalidated the database and discards the reader: the
        statements its other cursors send afterwards meet it too, and are ``Busy('Dropped')`` (their
        sessions open on the new reader). An answer outside the protocol ends the reader's process, and one
        whose process ended is ``Busy('Died')``: ``ReaderDied`` once its file set was charged ``DEATHS``
        ends."""
        instance = self._instance
        try:
            with self._sending:
                if not self._open:
                    raise TargetRefused("ConnectionException")  # as DuckDB names a closed cursor's
                request, slot = instance.ask(self, op=op, cursor=self._number, **members)
                self._flight = request["id"]
            answer = slot.result()
        except manifest.Busy:
            raise _condemned(self._key, self._who) or manifest.Busy("Died") from None
        try:
            found = _checked(request, *answer, None if op == "execute" else self._width, tags)
            self._unread = op != "fetch" or bool(found)
            self._ahead = bool(members.get("ahead")) and len(found) >= members["rows"]
            if op == "execute":
                self._width = None
            elif found:
                self._width = len(found) if op == "columns" else len(found[0])
            return found
        except _Refused as exc:
            (kind, name), self._unread, self._ahead = exc.args, False, False
        except Exception:
            instance.end("reply")
            raise manifest.Busy("Died") from None
        if kind == "fatal":
            earlier = instance.dropped  # the database was invalidated before this call
            discard(instance)
            if earlier:
                raise manifest.Busy("Dropped")
        raise _error(kind, name, instance.spill)

    def _select(self, statement: str, values: Mapping[str, Any] | None) -> None:
        binds = None if values is None else {name: reader.bind(value) for name, value in values.items()}
        self._asked("execute", statement=statement, binds=binds)

    async def _call(
        self, fn: Any, *args: Any, timeout_s: float | None, watch: Callable[[], Awaitable[None]] | None = None
    ) -> Any:
        """``fn(*args)`` on ``manifest.POOL`` (it sends a request, waits for its answer and shapes it); past
        ``timeout_s``, counted from when a thread took it, interrupted, ``GRACE_S`` later given up with the
        reader (``_forsaken``). A caller cancelled meanwhile interrupts it on its way out. With ``watch``
        (the files' stat check) a call is looked at every ``WATCH_S``: a changed file interrupts it, a
        check past its deadline abandons it, and a reader that gives no sign of life within
        ``manifest.STAT_S`` is killed, so neither a dead mount nor a frozen process keeps ``SORT``
        (FILES.md §10, READER.md §8). A call no thread took within ``manifest.STAT_S`` is
        ``manifest.Busy``: it is never run, nothing is abandoned and no reader discarded."""
        manifest._admitted(self._root)
        instance = self._instance

        def guarded() -> Any:
            try:
                return fn(*args)
            except TableError:
                raise
            except Exception as exc:  # a request no frame carries, the shaping's own: its class's name
                raise TargetRefused(type(exc).__name__) from None

        pooled = manifest.POOL.submit(guarded)
        future, sent, looked = manifest._awaited(pooled), time.monotonic(), 0.0
        try:
            while True:
                began, now = pooled.began, time.monotonic()
                wait: float | None
                if began is None:  # queued: looked at again before its timeout or its watch could be due
                    wait = min(sent + manifest.STAT_S - now, WATCH_S, timeout_s or WATCH_S)
                else:
                    left = None if timeout_s is None else began + timeout_s - now
                    wait = None if watch is None else max(began, looked) + WATCH_S - now
                    wait = left if wait is None else wait if left is None else min(wait, left)
                done, _ = await asyncio.wait({future}, timeout=None if wait is None else max(wait, 0))
                if done:
                    return future.result()
                if began is None:  # no thread took it within STAT_S: withdrawn; else its timeout starts
                    late = time.monotonic() >= sent + manifest.STAT_S
                    if late and pooled.began is None and pooled.cancel():
                        raise manifest.Busy("Busy")
                    continue
                if watch is None or (timeout_s is not None and time.monotonic() - began >= timeout_s):
                    break  # its timeout
                looked, sign = time.monotonic(), None
                with suppress(manifest.Busy):  # its process ended: the statement's answer says so
                    sign = manifest._awaited(instance.ask(op="ping")[1])
                try:
                    await watch()
                except manifest.Busy:  # no thread for the check: the statement is looked at again
                    pass
                except TargetUnreachable:
                    discard(instance)
                    raise
                left = max(looked + manifest.STAT_S - time.monotonic(), 0)
                if sign is not None and not (await asyncio.wait({future, sign}, timeout=left))[0]:
                    instance.end(DEADLINE)  # it answers nobody: killed at once
            self.interrupt()
            done, _ = await asyncio.wait({future}, timeout=GRACE_S)
            if not done:
                self._forsaken(pooled)
                raise TargetUnreachable("Deadline")
            return future.result()
        finally:
            if not future.done() and not pooled.cancel():  # queued, it never runs
                self.interrupt()

    def _forsaken(self, pooled: Future[Any] | None = None) -> None:
        """Give up the cursor's statement, unanswered ``GRACE_S`` after its interrupt (READER.md §8): what
        waits for its answer is answered, the cursor is closed and its reader retired, killed with its last
        cursor: at once where the statement was alone. The read is counted for its target's root until it
        answers or its process ends; ``pooled``, the pool's call, until that returns where no request of
        it is unanswered. Nothing is done when neither is there to give up."""
        kept = None if self._flight is None else self._instance.forsake(self._flight)
        if (counted := kept or pooled) is not None:
            if self._open:
                self._shut()
            discard(self._instance)
            manifest._abandoned(counted, self._root)

    async def execute(
        self,
        statement: str,
        values: Any = None,
        *,
        timeout_s: float | None = None,
        watch: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Send one ``SELECT`` with its bound ``values``; its rows are fetched with ``fetch``."""
        await self._call(self._select, statement, values, timeout_s=timeout_s, watch=watch)

    async def fetch(
        self,
        size: int,
        *,
        timeout_s: float | None = None,
        then: Callable[[Rows], Any] | None = None,
        watch: Callable[[], Awaitable[None]] | None = None,
        tags: str | None = None,
    ) -> Any:
        """Up to ``size`` more rows of the statement, streamed (U6), through ``then`` in the pool's thread;
        none when it has ended. ``tags``: a tag a column (``i`` an integer, ``s`` text, ``o`` a boolean),
        for rows whose values this process keeps or words: rows of another form are no answer."""

        def fetched() -> Any:
            rows = self._asked("fetch", tags, rows=size, step=size, bytes=BATCH_BYTES)
            return then(rows) if then and rows else rows

        return await self._call(fetched, timeout_s=timeout_s, watch=watch)

    async def batch(
        self,
        step: int,
        then: Callable[[Rows], Any] | None = None,
        *,
        timeout_s: float | None = None,
        watch: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """The next batch: ``BATCH`` rows or ``BATCH_BYTES`` of their texts and bytes, whichever comes
        first, fetched ``step`` rows at a time in the reader (fewer where a frame, or ``reader.CELLS``, has
        no room for them all) and shaped by ``then`` in the pool's thread; none at the end (§10). After a
        batch of ``BATCH`` rows or more the reader reads the next one ahead meanwhile (READER.md R9), unless
        it is a task's own."""

        def fetched() -> Any:
            ahead = self._instance.ahead
            rows = self._asked("fetch", rows=BATCH, step=step, bytes=BATCH_BYTES, ahead=ahead)
            return then(rows) if then and rows else rows

        return await self._call(fetched, timeout_s=timeout_s, watch=watch)

    async def columns(self, *, timeout_s: float | None = None) -> list[tuple[str, str]]:
        """The sent statement's columns: each one's name and the type DuckDB reads it as."""
        return await self._call(self._asked, "columns", timeout_s=timeout_s)

    def interrupt(self) -> None:
        self._instance.tell(op="interrupt", cursor=self._number)

    def _shut(self) -> None:
        with self._sending:
            self._open = False
            self.interrupt()
            self._instance.tell(op="close", cursor=self._number)
        _returned(self._instance, self)

    async def close(self) -> None:
        """Close the cursor, once: its statement in flight is interrupted (one its reader has not begun is
        not run), its rejects tables go with it (U16), and it holds its reader no longer. Two frames and no
        wait: no read fails for it. A statement still unanswered ``GRACE_S`` later is given up."""
        if not self._open:
            return
        self._shut()
        if self._flight in self._instance._pending:
            asyncio.get_running_loop().call_later(GRACE_S, self._forsaken)


def selftest(settings: Settings | None = None, *, limits: bool = True, fault: int = signal.SIGSEGV) -> None:
    """The release's smoke step (FILES.md §3, READER.md §11), under the compose hardening: a three-row CSV
    and the two-row Parquet constant written under the temporary directory; a reader that reads them alone
    and spills in the evidence volume (``files.spill_dir`` of ``DS_CONFIG``'s settings), proved (U2, a
    refused read outside its root), both files read, and U4's sort of 200,000 rows under ``SELFTEST_MIB``,
    which must spill; that reader sent ``fault`` (SIGSEGV) mid-statement, whose statement is ``Died`` and
    whose process and directory are gone, and another that reads again. With ``limits``, on Linux: the
    first reader's start saw its data limit hold and, on a machine of ``reader.FILTERED``, sockets denied,
    and under a data limit of 512 MiB ``HOLDS`` does not complete; without, its readers start as
    ``files.os_limits: false`` starts them. Prints what it saw; raises on any failure."""
    settings = settings or load_settings()
    with tempfile.TemporaryDirectory() as scratch:
        root, probe = Path(scratch, "files").resolve(), Path(scratch, "outside.csv")
        root.mkdir()
        (root / "three.csv").write_bytes(CSV)
        (root / "two.parquet").write_bytes(PARQUET)
        probe.write_bytes(CSV)
        print(asyncio.run(_selftested(settings.spill_dir, root, probe, limits, fault)))


async def _selftested(spill_dir: Path, root: Path, probe: Path, limits: bool, fault: int) -> str:
    started: list[Instance] = []

    def start(memory_mib: int = SELFTEST_MIB, limit: int | None = None) -> Cursor:
        limit = (limit or _limit(memory_mib, 2)) if limits else None
        started.append(_spawned(spill_dir, [str(root)], probe, memory_mib, 256, 2, limit))
        return started[-1].cursor()

    async def read(cursor: Cursor) -> None:
        await cursor.execute(
            "SELECT count(*) FROM read_csv($f, auto_detect=false, header=true, delim=',',"
            " columns={'id': 'VARCHAR', 'name': 'VARCHAR'})",
            {"f": [str(root / "three.csv")]},
        )
        csv = await cursor.fetch(1)
        await cursor.execute("SELECT count(*) FROM read_parquet($f)", {"f": [str(root / "two.parquet")]})
        if csv != [(3,)] or await cursor.fetch(1) != [(2,)]:
            raise RuntimeError("the files were not read")

    try:
        cursor = start()
        first = started[0]
        await read(cursor)
        if not await _spilled(cursor, first.spill / "temp"):
            raise RuntimeError("the sort did not spill")
        sent = asyncio.ensure_future(cursor.execute(ENDLESS))
        await asyncio.wait({sent}, timeout=0.5)  # mid-statement
        os.kill(first.proc.pid, fault)
        with suppress(manifest.Busy):  # Died
            await sent
        first._ending.join(GRACE_S)
        if first.cause != _cause(-fault) or first.spill.exists():
            raise RuntimeError("the reader's process or its directory outlived its end")
        await read(start())
        data, sockets = restricted(first)
        held = f"{'data limit held' if first.limit else data}, {sockets}" if limits else "limits not tried"
        if limits and reader.LINUX:
            if first.limit is None or not (first.sockets or os.uname().machine not in reader.FILTERED):
                raise RuntimeError(f"the reader's limits did not hold: {data}, {sockets}")
            cursor = start(2048, 512)
            try:
                await cursor.execute(HOLDS)
                await cursor.fetch(1)
            except TableError:  # refused, or its process ended
                pass
            else:
                raise RuntimeError("the data limit did not hold")
        return f"file reader: duckdb {first.version} in a process of its own, replaced when it ended; {held}"
    finally:
        for made in started:
            _stopped(made)


async def _spilled(cursor: Cursor, temp: Path) -> bool:
    """Whether ``SPILL_SORT`` read whole and its reader wrote to ``temp`` while it ran (watched by a
    thread of this process)."""
    peak, stop = [0], threading.Event()

    def watch() -> None:
        while not stop.wait(0.001):
            try:
                peak[0] = max(peak[0], sum(entry.stat().st_size for entry in os.scandir(temp)))
            except OSError:
                pass

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    started = time.monotonic()
    try:
        await cursor.execute(SPILL_SORT)
        rows = 0
        while batch := await cursor.fetch(10_000):
            rows += len(batch)
    finally:
        stop.set()
        watcher.join(max(1.0, time.monotonic() - started))
    return rows == 200_000 and peak[0] > 0


# -- the files engine (FILES.md §6 to §11) ---------------------------------------------------------------

FIELD = "a CSV field"  # a CSV column's declared type: every field is text, converted only as declared (D5)
PARTITION = "a partition directory"  # a key=value directory's, read as text
REJECT_MAX = 1000  # lines of a side's files that are not rows: more refuse its run (P24)
FLOOD = 1000  # rows of file 1 the flood guard converts (§6 step 7)
PADDED = 64  # fields beyond csv.columns a first record's count reads (§6 step 5)
# What a scan's sort holds of a row, as DuckDB 1.5.5 was measured to spill it (§6 step 8, §10): the key's text
# three times (the order key, the text selected, and the expression ordered by, which is carried too), 24
# bytes for each value carried beside its text, 16 a row; and what the projection adds to that sum, whose
# peak spill was 0.36 to 0.98 of it in the 37 sorts measured.
SORT_KEYS, SORT_VALUE, SORT_ROW, SORT_MARGIN = 3, 24, 16, 1.15
_settings: Settings | None = None
_roots: tuple[str, ...] = ()
_counted: dict[tuple[str, str], dict[str, Any]] = {}  # (files_sha256, its declaration's digest) -> counts
# Integer types by their bits, an unsigned one's negative.
_BITS = {"TINYINT": 8, "SMALLINT": 16, "INTEGER": 32, "BIGINT": 64, "HUGEINT": 128}
_BITS |= {f"U{name}": -bits for name, bits in _BITS.items()}
# The class of each type DuckDB reads a Parquet column as (§8); a DECIMAL(p,s) is decimal, a type not listed
# refused (P23).
_CLASSES = dict.fromkeys(_BITS, "integer") | {
    "DOUBLE": "float", "FLOAT": "float32", "VARCHAR": "text", "UUID": "text", "BOOLEAN": "boolean",
    "DATE": "date", "TIME": "time", "TIMESTAMP WITH TIME ZONE": "instant", "BLOB": "bytes", "JSON": "json",
    "INTERVAL": "other", "TIME WITH TIME ZONE": "other",
    **dict.fromkeys(("TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP", "TIMESTAMP_NS"), "timestamp"),
}  # fmt: skip
_KEYS = ("text", "char", "integer", "decimal", "date", "bytes")  # the classes a file's key may be (§9)
# What a filter's literal compared with a column of the class must be, and what it is cast to.
_MOMENT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_CLOCK = r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?"
_FORMS = {
    "date": re.compile(_MOMENT),
    "time": re.compile(_CLOCK),
    "timestamp": re.compile(f"{_MOMENT}[ T]{_CLOCK}"),
    "instant": re.compile(f"{_MOMENT}[ T]{_CLOCK}(Z|[+-][0-9]{{2}}(?::?[0-9]{{2}})?)?"),
}
# A key part a column's key text can be (§9: anything else is on no row, 404 without a statement).
_KEY_TEXT = {"integer": re.compile(r"0|-?[1-9][0-9]{0,37}"), "date": re.compile(_MOMENT)}
_HEX = re.compile(r"\\x((?:[0-9a-f]{2})*)")
_WHOLE = re.compile(r"DECIMAL\(([0-9]+),\1\)")  # a decimal without an integer digit: DECIMAL(9,9)
_SCALED = re.compile(r"DECIMAL\(([0-9]+),([0-9]+)\)")  # a decimal's precision and scale
_REJECTS = {
    "MISSING COLUMNS": "columns", "TOO MANY COLUMNS": "columns", "UNQUOTED VALUE": "quote",
    "LINE SIZE OVER MAXIMUM": "line_size", "INVALID ENCODING": "encoding",
}  # fmt: skip
_NEWLINES = {"\n": "\\n", "\r\n": "\\r\\n"}  # new_line takes the escaped text (U14)
_GLOB = re.compile(r"[*?\[]")  # what makes a path a pattern to DuckDB
# §7's reader, every option bound (U5): strict_mode is DuckDB's strict default (U14). ``ignore_errors`` and
# ``null_padding`` are bound too, so a first record is read by the same reader (§6 step 5). ``parallel`` is
# false in every statement that reads rows (U20: the parallel reader takes a line of a quoted field for a
# row); the plan's end check alone reads a file with it (U21).
_CSV = (
    "read_csv($r_files, auto_detect=false, columns=$r_columns, header=$r_header, skip=$r_skip,"
    " delim=$r_delim, new_line=$r_new_line, quote=$r_quote, escape=$r_escape, encoding=$r_encoding,"
    " compression=$r_compression, nullstr=$r_null, allow_quoted_nulls=false, null_padding=$r_padding,"
    " union_by_name=false, filename=false, max_line_size=2097152, ignore_errors=$r_errors,"
    " parallel=$r_parallel, hive_partitioning=$r_hive{more})"
)
_PARQUET = (
    "read_parquet($r_files, union_by_name=false, filename=false, binary_as_string=$r_binary,"
    " hive_partitioning=$r_hive{more})"
)


def configure(settings: Settings, registry: TargetRegistry) -> None:
    """What the process's reader is started with: its settings and every files target's root, which
    ``Context.open`` hands over before any file side is read (one reader a process, §10)."""
    global _settings, _roots
    found = (registry.get(target_id).root for target_id in registry)
    _settings, _roots = settings, tuple(sorted(root for root in found if root is not None))


def _configured() -> Settings:
    if _settings is None:
        raise TargetRefused("Unconfigured")  # no context opened in this process
    return _settings


async def configured() -> Instance:
    """The process's reader as ``configure`` set it up; ``Unstarted`` when it does not start."""
    return await instance(_configured(), _roots)


class _Sorting:
    """``SORT``: one sort at a time a process (§10), held by a scan pass (both file sides of its pair, which
    one task opens), a census and a frame for their length. A task waiting for it holds no cursor yet.
    ``apart``: its holder reads in a reader of its own, a side's catalogue having said so before a cursor
    opened (READER.md §9)."""

    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
        self._owner: asyncio.Task[Any] | None = None
        self.apart = False

    @asynccontextmanager
    async def held(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is not None and task is self._owner:  # its pair's other file side
            yield
            return
        lock = self._locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
        if lock.locked():
            manifest.log.warning("a sort of a file side waits for another")
        async with lock:
            self._owner = task
            try:
                yield
            finally:
                self._owner, self.apart = None, False


SORT = _Sorting()
# What a task that holds no ``SORT`` (a per-key read, a plan, ``ds target check``) reads apart under, one
# such reader a loop.
ALONE: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_aparts: dict[Any, _Apart] = {}  # by task, while it holds a cursor apart


@dataclass(eq=False)
class _Apart:
    """A task's reader of its own (READER.md §9), where a file set a reader ended on is read apart, and
    whatever else the task reads meanwhile: started for its first cursor there and killed with its last,
    which clears those sets of suspicion when it lived. ``alone`` is ``ALONE`` as the task holds it; a
    task that holds ``SORT`` waits for nothing more."""

    task: Any
    alone: asyncio.Lock | None
    reader: Instance | None = None
    keys: tuple[Any, ...] = ()


async def _parted(sorting: bool) -> _Apart:
    """The running task reads apart from here on; ``Busy('Alone')`` when it holds no ``SORT`` and another
    task's reader apart did not end within ``manifest.STAT_S``."""
    alone = None if sorting else ALONE.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    if alone is not None:
        try:
            async with asyncio.timeout(manifest.STAT_S):
                await alone.acquire()
        except TimeoutError:
            raise manifest.Busy("Alone") from None
    task = asyncio.current_task()
    _aparts[task] = _Apart(task, alone)
    return _aparts[task]


def _rejoined(apart: _Apart) -> None:
    """A cursor apart was closed, or none was opened: with the task's last its reader is killed, the sets
    read in it are suspect no more when it lived, and ``ALONE`` is free. A charged set is read apart until
    this process restarts."""
    if apart.reader is not None:
        if apart.reader.held:
            return
        if not apart.reader.dropped:
            with _build:
                for key in apart.keys:
                    _suspects.pop(key, None)
        discard(apart.reader)
    if apart.alone is not None:
        apart.alone.release()
    _aparts.pop(apart.task, None)


def _set(side: Plan) -> str:
    """A file side's key (READER.md §9): the SHA-256 of its pinned files and pins, held in memory only."""
    pinned = side["pinned"]
    return hashlib.sha256(jsonio.dumps([pinned["files"], pinned["pins"]]).encode()).hexdigest()


def _bind(binds: compose.Binds, value: Any) -> str:
    """``value`` bound under the next name; its placeholder."""
    name = f"v{len(binds)}"
    binds[name] = value
    return f"${name}"


def _csv(declared: str) -> bool:
    """Whether a column of this declared type is a CSV field (or a partition's), which compares as text."""
    return declared.startswith(FIELD) or declared == PARTITION


def _scale(declared: str) -> int:
    found = re.search(r",([0-9]+)\)", declared)
    return int(found[1]) if found else 0


class _Primitives:
    """``compose.Dialect`` for DuckDB reading a file side: a value is a bound parameter; the reader's own (the
    file list and the dialect) are bound by the session (U5)."""

    def ident(self, name: str) -> str:
        if not name.isprintable():
            raise Refusal("a column's name holds a control character: it is not read")
        return '"' + name.replace('"', '""') + '"'

    def relation(self, side: Plan) -> str:
        return _relation(side)

    def literal(self, text: str, cls: str, declared: str, binds: compose.Binds) -> str | None:
        if _csv(declared) or cls in ("text", "char", "json", "other"):  # a CSV column compares as text (§7)
            return _bind(binds, text)
        if render.family(cls) == "number":
            try:
                number = render.number(text)
            except ValueError:  # not a number, or one no column holds
                return None
            return _bind(binds, number) if len(number.as_tuple().digits) <= 38 else None
        if cls == "bytes":
            match = _HEX.fullmatch(text.lower())
            return _bind(binds, bytes.fromhex(match[1])) if match else None
        if cls == "boolean":
            return _bind(binds, text.lower() == "true") if text.lower() in ("true", "false") else None
        if cls not in _FORMS or not (found := _FORMS[cls].fullmatch(text)):
            return None
        if cls == "instant":  # an instant's text without an offset is UTC, as on every engine's session
            return f"CAST({_bind(binds, text if found[1] else text + '+00')} AS TIMESTAMPTZ)"
        typed = declared if cls == "timestamp" else cls.upper()  # TIMESTAMP_NS keeps its nanoseconds
        return f"CAST({_bind(binds, text)} AS {typed})"

    def key_text(self, column: str, cls: str, declared: str) -> str:
        name = f"t.{self.ident(column)}"
        if cls == "char":
            return f"rtrim({name}, ' ')"
        if _csv(declared):  # by csv.keys (§9): a field that is no value of its class has no key text
            if cls == "integer":
                matched, typed = "-?[0-9]{1,38}", "DECIMAL(38,0)"
            elif cls == "decimal":
                scale = _scale(declared)
                fraction = f"(\\.[0-9]{{1,{scale}}})?" if scale else ""
                matched, typed = f"-?[0-9]+{fraction}", f"DECIMAL(38,{scale})"  # never rounded
            elif cls == "date":
                matched, typed = _MOMENT, "DATE"
            else:
                return name
            typed = f"CAST(TRY_CAST({name} AS {typed}) AS VARCHAR)"
            return f"CASE WHEN regexp_full_match({name}, '{matched}') THEN {typed} END"
        if cls == "bytes":
            return f"'\\x' || lower(hex({name}))"
        if cls == "decimal" and _WHOLE.fullmatch(declared):  # DuckDB writes .5: every key text holds 0.5
            return f"regexp_replace(CAST({name} AS VARCHAR), '^(-?)\\.', '\\10.')"
        return name if declared == "VARCHAR" else f"CAST({name} AS VARCHAR)"

    def order(self, text: str) -> str:
        return f"encode({text})"

    def exact(self, order: str, part: str, binds: compose.Binds) -> str:
        return f"{order} = {_bind(binds, part.encode())}"

    def after(self, orders: Sequence[str], bound: Sequence[bytes], binds: compose.Binds) -> str:
        """(o1 > a) OR (o1 = a AND o2 > b), each part bound as bytes."""
        arms, equal = [], []
        for order, part in zip(orders, bound, strict=True):
            value = _bind(binds, part)
            arms.append(f"({' AND '.join([*equal, f'{order} > {value}'])})")
            equal.append(f"{order} = {value}")
        return f"({' OR '.join(arms)})"

    def render(self, column: str, cls: str, declared: str) -> str:
        if _csv(declared) and cls not in ("text", "char"):  # a csv.keys column compared: its key text
            return self.key_text(column, cls, declared)
        return self.rendered(f"t.{self.ident(column)}", cls, declared)

    def rendered(self, value: str, cls: str, declared: str) -> str:
        if cls in ("char", "bytes") or (cls == "text" and (_csv(declared) or declared == "VARCHAR")):
            return value
        if cls == "boolean":
            return f"CASE WHEN {value} THEN 'TRUE' WHEN NOT {value} THEN 'FALSE' END"
        if cls in ("float", "float32"):  # the shortest text that reads back (U9), narrowed in render
            return f"CAST(CAST({value} AS DOUBLE) AS VARCHAR)"
        if cls == "timestamp":  # up to 9 digits
            return f"replace(CAST({value} AS VARCHAR), ' ', 'T')"
        if cls == "instant":  # its UTC microseconds, read without ICU (U9); render adds +00:00
            return f"replace(CAST(make_timestamp(epoch_us({value})) AS VARCHAR), ' ', 'T')"
        if cls == "time":
            # Written from its microseconds. DuckDB's own text of a TIME outside a day, which a crafted file's
            # pages may hold, is an internal error or ends the process, and a guard before it is folded away
            # where the file's statistics say no value lies outside (U30). Of a time of day, the same text.
            us = f"epoch_us({value})"
            parts = f"{us} // 3600000000, {us} // 60000000 % 60, {us} // 1000000 % 60"
            micros = f"rtrim(printf('.%06d', {us} % 1000000), '0')"
            clock = f"printf('%02d:%02d:%02d', {parts})"
            return f"{clock} || CASE WHEN {us} % 1000000 = 0 THEN '' ELSE {micros} END"
        return f"CAST({value} AS VARCHAR)"

    def length(self, column: str, cls: str, declared: str) -> str:
        return f"length(t.{self.ident(column)})"

    def distinct(self, column: str, cls: str, declared: str) -> str:
        return f"t.{self.ident(column)}"

    def lob(self, declared: str) -> bool:
        """Bytes and JSON: a row whose values of them pass ``MAX_LOB_BYTES`` is not fetched (§10). A text
        column is not flagged: a frame's strata are text, and a CSV line holds 2 MiB at most."""
        return declared in ("BLOB", "JSON")

    def oversize(self, side: Plan, columns: Sequence[str]) -> str:
        sizes = [
            f"coalesce({'octet_length' if side['declared'][column] == 'BLOB' else 'strlen'}"
            f"(t.{self.ident(column)}{'' if side['declared'][column] == 'BLOB' else '::VARCHAR'}), 0)"
            for column in columns
        ]
        return f"({' + '.join(sizes)} > {compose.MAX_LOB_BYTES})"

    def row_hash(self, side: Plan, columns: Sequence[str]) -> str:
        """§9's: each rendered text's SHA-256 (``-`` for a NULL), all of them hashed again, so a NULL and the
        boundary between two values stay apart; bytes and a padded field as their key texts (the hexadecimal
        digits, the text without its padding), so equal hashes are equal canonical values."""
        texts = []
        for column in columns:
            cls, declared = side["classes"][column], side["declared"][column]
            text = (self.key_text if cls in ("bytes", "char") else self.render)(column, cls, declared)
            texts.append(f"coalesce(sha256({text}), '-')")
        return f"unhex(sha256(concat({', '.join(texts)})))"

    def first(self, select: str, count: int, by: int | None = None) -> str:
        return f"{select}{f' ORDER BY {by} DESC' if by else ''} LIMIT {count}"

    def selected(self, condition: str | None) -> str:
        return "1" if condition is None else f"CASE WHEN {condition} THEN 1 ELSE 0 END"

    def prefix_pad(self, text: str, prefix: str, width: int, binds: compose.Binds) -> str:
        padded = f"lpad({text}, CAST(greatest({int(width)}, length({text})) AS INTEGER), '0')"
        return f"CASE WHEN regexp_full_match({text}, '[0-9]+') THEN {_bind(binds, prefix)} || {padded} END"

    def like(self, parts: list[str]) -> str:
        return " ".join(parts) + " ESCAPE '\\'"  # DuckDB's LIKE has no escape of its own

    def arms(self, count: int) -> int:
        return count


PRIMITIVES = _Primitives()


def _relation(side: Plan, more: str = "") -> str:
    """The side's reader under the alias ``t``. A CSV side's reads every declared column, also in a
    statement that reads only some (U13: invalid encoding is found only in a field that is read)."""
    file = side["file"]
    more = (", hive_types=$r_types" if file.get("partitions") else "") + more
    if file["format"] == "parquet":
        return f"{_PARQUET.format(more=more)} AS t"
    read = (PRIMITIVES.ident(column) for column in file["csv"]["columns"])
    every = " AND ".join(f"({column} IS NULL OR {column} IS NOT NULL)" for column in read)
    return f"(SELECT * FROM {_CSV.format(more=more)} WHERE {every}) AS t"  # noqa: S608 (constants, names)


def _reader(side: Plan, root: str, files: Sequence[str] | None = None) -> dict[str, Any]:
    """The values the side's reader binds: its files (``files``: of them, by relative path), then its
    format's options. The files are the pinned ones and no path is a pattern (DuckDB expands ``*``, ``?``
    and ``[``), so it reads no file that was not listed, pinned and hashed (D3)."""
    file, pinned = side["file"], side["pinned"]["files"]
    paths = [os.path.join(root, name) for name in (pinned if files is None else files)]
    if not set(pinned if files is None else files) <= set(pinned) or any(map(_GLOB.search, paths)):
        raise TargetRefused("Unpinned")
    binds: dict[str, Any] = {"r_files": paths, "r_hive": bool(file.get("partitions"))}
    if file.get("partitions"):
        binds["r_types"] = dict.fromkeys(file["partitions"], "VARCHAR")  # hive_types: all text
    if file["format"] == "parquet":
        return binds | {"r_binary": bool(file.get("binaryAsString"))}
    csv = file["csv"]
    quote = csv.get("quote", '"')
    return binds | {
        "r_columns": dict.fromkeys(csv["columns"], "VARCHAR"), "r_header": bool(csv.get("header")),
        "r_skip": csv.get("skip", 0), "r_delim": csv["delimiter"],
        "r_new_line": _NEWLINES[csv.get("newline", "\n")], "r_quote": quote,
        "r_escape": csv.get("escape", quote), "r_encoding": csv.get("encoding", "utf-8"),
        "r_compression": csv.get("compression", "none"), "r_null": csv.get("null", ""), "r_padding": False,
        "r_errors": True, "r_parallel": False,
    }  # fmt: skip


async def _verify(target: Target, side: Plan, *, full: bool = False, planning: bool = False) -> None:
    """§11's check of the side's files against its pins: stat, or ``full`` (hashed again, uncached)."""
    file, pinned = side["file"], side["pinned"]
    pins = [tuple(pin) for pin in pinned["pins"]]
    await manifest.verify(
        target, file["path"], pinned["side"], pinned["files"], pins, full=full,  # type: ignore[arg-type]
        partitions=file.get("partitions", ()), planning=planning,
    )  # fmt: skip


def described(side: Plan) -> Relation:
    """A file side's relation as its plan holds it (§12): the columns it reads, their declared types and
    classes. Only other files change them, and a changed file fails the run (F9)."""
    columns = tuple(
        Column(column, side["declared"][column], cls, True, cls in ("text", "char"), cls in _KEYS)
        for column, cls in side["classes"].items()
    )
    facts = {"kind": "files", "key": None, "server": side["server"], "consistency": "fixed", "charset": None}
    return Relation(None, side["name"], columns, facts)


class _Session:
    """A file side's session: a cursor of the process's reader, opened at its first statement, on its
    target; ``timeout_s`` bounds each statement (none: a scan's)."""

    def __init__(self, target: Target, timeout_s: float) -> None:
        self.target, self.timeout_s = target, timeout_s or None
        self.cursor: Cursor | None = None
        self.apart: _Apart | None = None  # where its cursor was opened apart
        self.verified_at: str | None = None  # when its pass's closing hash agreed (§11)

    async def _opened(self, side: Plan | None = None) -> Cursor:
        """Its cursor, opened at its first statement: of the process's reader, or, for a side whose files
        a reader ended on and in a task that reads apart, of that task's own (READER.md §9). A side
        charged ``DEATHS`` ends is ``ReaderDied`` before anything is sent."""
        if self.cursor is None:
            key, who = (None, "") if side is None else (_set(side), f"side {side['pinned']['side']}")
            if (refused := _condemned(key, who)) is not None:
                raise refused
            task = asyncio.current_task()
            sorting = SORT._owner is task
            apart = _aparts.get(task)
            if apart is None and (_doubted(key) or (sorting and SORT.apart)):
                apart = await _parted(sorting)
            try:
                if apart is None:
                    made = await configured()
                else:
                    made = apart.reader = apart.reader or await _begun(_configured(), _roots, apart=True)
                    made.ahead = False  # one request at a time there: an end has one set in flight
                    apart.keys += (key,)
                self.cursor, self.apart = made.cursor(self.target.root, key, who), apart
            except Unstarted as exc:
                # A plan started the reader before its first statement (P27, P28). At run time a spill
                # volume short of room is temporary space, as before a sort: a read waits for it (§10);
                # a process that does not start is an outage with a code of its own (READER.md §9); a
                # guard that does not hold fails the run.
                if exc.reason == "process":
                    raise manifest.Busy("Unstarted") from None
                if exc.reason != "space":
                    raise
                raise TargetTempSpace("spill") from None
            finally:
                if apart is not None and self.cursor is None:
                    _rejoined(apart)
        return self.cursor

    @property
    def spill(self) -> Path:
        assert self.cursor is not None
        return self.cursor._instance.spill

    async def _judged(self, side: Plan, call: Awaitable[Any]) -> Any:
        """``call``'s answer; an error of unchanged files is the statement's, of changed ones ``FilesChanged``
        and of files that cannot be listed ``TargetUnreachable`` (§10's table)."""
        try:
            return await call
        except TargetRefused as exc:
            await _verify(self.target, side, planning="end" in side["pinned"])
            exc.fixed = True  # unchanged: every read of these bytes meets it
            raise

    async def send(
        self, side: Plan, statement: str, binds: compose.Binds, *, files: Sequence[str] | None = None,
        reader: Mapping[str, Any] | None = None, timeout_s: float | None = None, watch: bool = False,
    ) -> None:  # fmt: skip
        """Send one statement with the side's reader bound (``files``: of them; ``reader``: options of its
        own); a sort's is watched (§10)."""
        cursor = await self._opened(side)
        watched = partial(_verify, self.target, side) if watch else None
        bound = _reader(side, self.target.root or "", files) | dict(reader or {})
        values = binds | {name: value for name, value in bound.items() if f"${name}" in statement}
        await self._judged(side, cursor.execute(statement, values, timeout_s=timeout_s, watch=watched))

    async def rows(
        self, side: Plan, statement: str, binds: compose.Binds, *, files: Sequence[str] | None = None,
        reader: Mapping[str, Any] | None = None, then: Callable[[Rows], Any] | None = None,
        timeout_s: float | None = None, watch: bool = False, tags: str | None = None,
    ) -> list[Any]:  # fmt: skip
        """A statement's every row (a per-key group's, a count's), each fetch through ``then``; ``watch``:
        watched as a sort's is, its fetches too (a pass that holds ``SORT``); ``tags``: what its columns
        cross as (``Cursor.fetch``), of a statement whose values a plan keeps or words."""
        timeout_s = timeout_s or self.timeout_s
        await self.send(side, statement, binds, files=files, reader=reader, timeout_s=timeout_s, watch=watch)
        assert self.cursor is not None
        watched = partial(_verify, self.target, side) if watch else None
        found: list[Any] = []
        while fetched := await self._judged(
            side, self.cursor.fetch(BATCH, timeout_s=timeout_s, then=then, watch=watched, tags=tags)
        ):
            found += fetched
        return found

    def outside(self) -> manifest.Busy:
        """What rows that are no answer of DuckDB's to their statement cost (READER.md §6), where only the
        plan that reads them can tell: the reader's process is ended (``reply``), and the read is
        ``Died``."""
        assert self.cursor is not None
        self.cursor._instance.end("reply")
        return manifest.Busy("Died")

    async def batches(self, side: Plan, shaped: Callable[[Rows], Rows], wide: bool) -> AsyncIterator[Rows]:
        """The sent statement's rows in batches through ``shaped``, ``STEP`` rows a fetch while ``wide`` (a
        text, bytes or JSON column is read); the files stat-checked after each and hashed again at the end
        (§11)."""
        assert self.cursor is not None
        watched = partial(_verify, self.target, side)
        step = STEP if wide else BATCH
        while rows := await self._judged(side, self.cursor.batch(step, shaped, watch=watched)):
            await _verify(self.target, side)
            yield rows
        await _verify(self.target, side, full=True)
        self.verified_at = utc_now_iso()

    async def sort(self, side: Plan) -> None:
        """Before a sort: the spill's free space for the smaller of its ``sort_bytes`` and ``files.spill``
        (§10), before a byte is read. A reader whose process ended meanwhile, and its spill directory with
        it, is ``Died``, not a spill without room: its cause is set before its directory is given up."""
        cursor = await self._opened(side)
        need = min(side.get("sort_bytes") or 0, manifest.size_bytes(_configured().files.spill))
        if await manifest.call(_free, self.spill) < need:
            if cursor._instance.cause is not None:
                raise _condemned(cursor._key, cursor._who) or manifest.Busy("Died")
            raise TargetTempSpace("spill")

    async def renewed(self) -> None:
        """Before a per-key read, which needs nothing of an earlier statement: a cursor of a discarded
        reader is closed, so the read opens one of the new reader and the old one's budget ends (D9); and
        so is one of the shared reader whose files a reader has ended on since: the read opens apart."""
        cursor = self.cursor
        if cursor and (cursor._instance.dropped or (self.apart is None and _doubted(cursor._key))):
            await self.close()

    async def close(self) -> None:
        if self.cursor is not None:
            cursor, self.cursor, apart, self.apart = self.cursor, None, self.apart, None
            await cursor.close()
            if apart is not None:
                _rejoined(apart)


def _wide(side: Plan, columns: Sequence[str]) -> bool:
    """Whether a statement reading ``columns`` reads a text, bytes or JSON column (§10)."""
    return any(side["classes"][column] in ("text", "char", "bytes", "json") for column in columns)


def _fits(side: Plan, column: str, part: str) -> bool:
    """Whether a key part can be the key text of a value of the column (§9): of its class's form, and a
    Parquet integer in its type's range."""
    cls, declared = side["classes"][column], side["declared"][column]
    if cls == "decimal":
        scale = _scale(declared)
        return (
            re.fullmatch(r"-?(?:0|[1-9][0-9]*)" + (f"\\.[0-9]{{{scale}}}" if scale else ""), part) is not None
        )
    if cls == "bytes":
        return _HEX.fullmatch(part) is not None
    if cls in _KEY_TEXT and not _KEY_TEXT[cls].fullmatch(part):
        return False
    if cls == "date":
        try:
            date.fromisoformat(part)
        except ValueError:  # 2024-02-31
            return False
    bits = _BITS.get(declared)
    if bits is None:
        return True
    low, high = (0, 2**-bits - 1) if bits < 0 else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
    return low <= int(part) <= high


def _keyed(side: Plan, asked: Asked) -> tuple[str, compose.Binds]:
    """§9's per-key read: one statement for a group, one pass over the files. The keys join the rows by
    their key texts' bytes; a key one of whose parts no key text can be is on no row and is not joined. No
    predicate on a key column reaches the files' reader, which prunes row groups by their statistics: by
    those old writers computed in signed-byte order for a text or bytes column, and by those of any column
    that do not hold its group's values (a statistic of a decimal stored as bytes that is empty; a crafted
    one), a row group that holds the key was skipped, and a key the scan had read was on no row (observed
    on 1.5.5). Each key keeps two rows at most, the one its filter selects first. The selection is
    written out where a window orders by it: an alias there would be a column of the same name (observed)."""
    binds: compose.Binds = {}
    keys = compose._keys(PRIMITIVES, side, binds)
    condition = compose._filter(PRIMITIVES, side, binds)
    flag = f"({condition}) IS TRUE" if condition else "TRUE"
    select = [*compose._rendered(PRIMITIVES, side, list(side["columns"].values())), *keys, flag]
    mapped = [bool(key_map) for key_map in key_maps(side)]
    rows = []
    for i, key in enumerate(asked):
        parts = decode_key(key, len(keys))
        if not all(m or _fits(side, c, p) for c, p, m in zip(side["keys"], parts, mapped, strict=True)):
            continue
        rows.append(f"({i}, {', '.join(_bind(binds, part.encode()) for part in parts)})")
    if not rows:
        return "", {}
    # By the key texts' bytes, an expression: of a join on a bare column DuckDB derives a filter for the
    # files' reader, which prunes row groups by statistics as an IN on the column does (observed on 1.5.5).
    joined = " AND ".join(f"{PRIMITIVES.order(text)} = a.k{j}" for j, text in enumerate(keys))
    names = ", ".join(["i", *(f"k{j}" for j in range(len(keys)))])
    statement = (
        f"SELECT a.i, {', '.join(select)} FROM {_relation(side)}"  # noqa: S608 (primitives' texts)
        f" JOIN (VALUES {', '.join(rows)}) AS a({names}) ON {joined}"
        f" QUALIFY row_number() OVER (PARTITION BY a.i ORDER BY {flag} DESC) <= 2"
        f" ORDER BY a.i, {len(select) + 1} DESC"
    )
    return statement, binds


# -- a file side's plan (§6) ------------------------------------------------------------------------------


@asynccontextmanager
async def _planning(who: str, target: Target, files: int) -> AsyncIterator[None]:
    """A plan's file work, its failures worded for ``who`` (P27 to P30, P34, P35, P14's deadline)."""
    try:
        yield
    except Unstarted as exc:
        raise ValueError(unstarted(exc, who, _configured().files)) from None
    except ReaderDied as exc:  # P35: its code is the text, worded for its side
        raise ValueError(str(exc)) from None
    except StatementTimeout:
        raise NotReady(
            "planning",
            f"{who}: planning its files passed the {_configured().files.plan_s} s files.plan_s allows"
            f" ({files} of {files} files hashed): try again (a file renamed into place is not hashed twice),"
            " or raise files.plan_s and the proxy's read timeout",
        ) from None
    except TargetUnreachable as exc:
        raise manifest.unanswered(exc, who, target) from None
    except (FilesChanged, TargetTempSpace, TargetDenied, TargetRefused) as exc:  # P29: any of DuckDB's
        raise ValueError(
            f"{who}: its files could not be read while the run was planned ({code(exc)})"
        ) from None


PLANS = 8  # runs whose file sides are planned at once in a process: half the pool's threads
_plans: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()  # a loop's
_ending: ContextVar[float | None] = ContextVar("ending", default=None)  # when the creation under way must end


@asynccontextmanager
async def creating() -> AsyncIterator[None]:
    """A run's creation while its file sides are planned (§6, §10): one ``files.plan_s`` from here for all
    of them, and ``PLANS`` creations at once in a process, so that plans alone never fill the pool; one that
    waited its whole ``files.plan_s`` for its turn is not ready (P34)."""
    plan_s = _configured().files.plan_s
    end = time.monotonic() + plan_s
    slots = _plans.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(PLANS))
    try:
        async with asyncio.timeout(plan_s):
            await slots.acquire()
    except TimeoutError:
        raise NotReady(
            "planning",
            f"the file reader plans {PLANS} runs at once, and this one waited the {plan_s} s files.plan_s"
            " allows for its turn: try again",
        ) from None
    token = _ending.set(end)
    try:
        yield
    finally:
        _ending.reset(token)
        slots.release()


def _left(side: Plan) -> float:
    """What is left of its run's ``files.plan_s`` (§6); a statement past it is P30."""
    return max(side["pinned"]["end"] - time.monotonic(), 0.001)


def _fold(name: str) -> str:
    """A header's name and its declared one as step 5 compares them: lower case, each run of characters
    outside ``a-z 0-9 _ $ #`` one ``_``, trimmed, and ``_`` before a leading digit."""
    folded = re.sub(r"[^a-z0-9_$#]+", "_", name.lower()).strip("_")
    return f"_{folded}" if folded[:1].isdigit() else folded


async def _records(session: _Session, side: Plan, who: str) -> None:
    """Step 5 of a CSV side: each file's first record after ``skip``, read by the same reader as one record
    (``header=false``) with ``PADDED`` more columns, NULL past its last (the declared reader would skip a
    record of another count and hand over the next), has as many fields as csv.columns, no ``\\r`` ending
    its last with ``newline: "\\n"``, and with ``header`` each field folds to its declared name (P21). A file
    the reader refuses is P29, and the record is compared, never kept."""
    csv, files, declared = side["file"]["csv"], side["pinned"]["files"], side["file"]["csv"]["columns"]
    typed = ", hive_types=$r_types" if side["file"].get("partitions") else ""
    first = f"SELECT * FROM {_CSV.format(more=typed)} LIMIT 1"  # noqa: S608 (constants)
    wider = {f"f{i}": "VARCHAR" for i in range(len(declared) + PADDED)}
    padded = {"r_header": False, "r_padding": True, "r_null": "\x00", "r_columns": wider}
    for n, name in enumerate(files, 1):
        at, want = f"file {n} of {len(files)}", len(declared)
        found = await session.rows(side, first, {}, files=[name], reader=padded, timeout_s=_left(side))
        record = found[0][: len(wider)] if found else ()  # its partitions' columns follow
        fields = next((i for i, value in enumerate(record) if value is None), len(record))
        if fields != want:
            raise ValueError(
                f"{who}: the first record of {at} has {fields} fields, not the {want} of csv.columns"
            )
        if csv.get("newline", "\n") == "\n" and record[want - 1].endswith("\r"):
            raise ValueError(f"{who}: {at} ends its lines with \\r\\n where csv.newline declares \\n")
        if csv.get("header"):
            for i, (text, column) in enumerate(zip(record, declared, strict=False), 1):
                if _fold(text) != _fold(column):
                    raise ValueError(f"{who}: the header of {at} does not name field {i} as csv.columns does")


_SCHEMA = (
    "SELECT file_name, name, num_children, duckdb_type, logical_type, precision, scale, type"
    " FROM parquet_schema($r_files)"
)
_SCHEMA_TAGS = "ssissiis"
_UNITS = {"MilliSeconds": "milliseconds", "MicroSeconds": "microseconds", "NanoSeconds": "nanoseconds"}
_NESTED = ("LIST", "ARRAY", "STRUCT", "MAP", "UNION")  # as ``_typed`` keeps a nested column's type
_LIST = "list the columns to compare without it"


def _tops(rows: Rows) -> dict[str, tuple[Any, ...]]:
    """A footer's top-level columns, by name, from ``parquet_schema``'s rows of one file in order (its root
    first; a nested column's children follow it)."""
    found, i = {}, 1
    while i < len(rows):
        found[rows[i][1]] = rows[i]
        left, i = max(rows[i][2] or 0, 0), i + 1
        while left and i < len(rows):  # its subtree (the plan's walk of the footer saw that it closes)
            left, i = left - 1 + max(rows[i][2] or 0, 0), i + 1
    return found


def _footed(row: tuple[Any, ...]) -> str:
    """A footer column's type as a message names it, and as every file's must be file 1's (P22): a time's
    or a timestamp's with its unit, an INT96 as such, since DuckDB reads them all as one type."""
    if (row[4] or "").startswith("DecimalType"):
        return f"DECIMAL({row[5]},{row[6]})"
    kind = _typed(row[3] or "") or ("nested" if row[2] else "?")
    if row[7] == "INT96":
        return f"{kind} as INT96"
    unit = next((unit for name, unit in _UNITS.items() if name in (row[4] or "")), None)
    return f"{kind} in {unit}" if unit else kind


def _parquet_type(
    name: str, kind: str, row: tuple[Any, ...] | None, typed: Mapping[str, Any]
) -> tuple[str | None, str]:
    """A Parquet column's class (None: refused) and why it is refused (P23), from the type DuckDB reads it
    as and its footer: a DECIMAL over 38 digits comes as a DOUBLE; a UTC nanosecond timestamp, an INT96
    timestamp (nanoseconds of a day) and a nanosecond time come to the microsecond, read only once the spec
    declares that reading: ``{as: timestamp}``, and for a time ``{as: string}``, its text."""
    logical, declared = (row[4] or "") if row else "", (typed.get(name) or {}).get("as")
    if logical.startswith("DecimalType") and row and row[5] > 38:
        why = "a decimal over 38 digits is not read exactly"
        return None, f"column {name!r} ({_footed(row)}) cannot be compared: {why}"
    nanos = "NanoSeconds" in logical
    cut = (
        ("a nanosecond time", nanos and logical.startswith("TimeType"), "string", "its text"),
        ("a UTC nanosecond timestamp", nanos and "isAdjustedToUTC=1" in logical, "timestamp", "microseconds"),
        ("an INT96 timestamp", bool(row) and row[7] == "INT96", "timestamp", "microseconds"),
    )  # fmt: skip
    what, _, as_, compared = next((found for found in cut if found[1]), ("", False, declared, ""))
    if declared != as_:
        named = name if name.isidentifier() else json.dumps(name)  # a file's text: quoted where it is no word
        return None, (
            f"column {name!r} ({what}) is read to the microsecond: declare typeMap"
            f" {{{named}: {{as: {as_}}}}} to compare {compared}, or leave it out"
        )
    scaled = _SCALED.fullmatch(kind)
    if scaled and int(scaled[2]) <= int(scaled[1]):  # a scale over its precision is a crafted footer's
        return "decimal", ""
    if (cls := _CLASSES.get(kind)) is not None:
        return cls, ""
    if kind in _NESTED:
        return None, f"column {name!r} ({kind}) cannot be compared: nested columns are not read; {_LIST}"
    return None, f"column {name!r} ({kind}) cannot be compared: its type is not read; {_LIST}"


async def _footers(session: _Session, side: Plan, cfg: SideConfig, who: str) -> list[Column]:
    """Step 5 of a Parquet side: file 1's columns as DuckDB reads them, each column the plan reads of a class
    (P23), and every footer holding each with file 1's name and type (P22). Each file's footer was walked
    when it was pinned (``manifest._footer``); encrypted Parquet was refused there."""
    table, files, root = cfg.table, side["pinned"]["files"], session.target.root or ""
    assert table is not None
    timeout = _left(side)
    try:
        columns = f"SELECT * FROM {_relation(side)} LIMIT 0"  # noqa: S608 (constants)
        await session.send(side, columns, {}, files=files[:1], timeout_s=timeout)
        assert session.cursor is not None
        read = await session.cursor.columns(timeout_s=timeout)
        rows = await session.rows(side, _SCHEMA, {}, timeout_s=timeout, tags=_SCHEMA_TAGS)
    except TargetRefused as exc:
        for n, name in enumerate(files, 1):
            try:
                await session.rows(side, _SCHEMA, {}, files=[name], timeout_s=_left(side))
            except TargetRefused:
                raise ValueError(
                    f"{who}: file {n} of {len(files)} is not a Parquet file this build reads ({code(exc)})"
                ) from None
        raise
    footers: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        footers.setdefault(row[0], []).append(row)
    tops = [_tops(footers.get(os.path.join(root, name), [])) for name in files]
    partitions = side["file"].get("partitions") or []
    for partition in partitions:
        if partition in tops[0]:
            raise ValueError(f"{who}: partition {partition!r} is also a column of the files")
    keys = list(table.key_columns)
    listed = [column for column, _ in table.columns] if table.columns is not None else None
    filtered = list(where.columns(where.parse(table.where))) if table.where else []
    planned = set(keys + filtered + (listed if listed is not None else [name for name, _ in read]))
    columns = []
    for name, kind in read:
        if name in partitions:
            columns.append(Column(name, PARTITION, "text", True, True, True))
            continue
        cls, why = _parquet_type(name, kind, tops[0].get(name), table.types)
        if cls is None and name in planned:
            raise ValueError(f"{who}: {why}")
        columns.append(Column(name, kind, cls, True, cls == "text", cls in _KEYS and cls != "char"))
        if name not in planned:
            continue
        for n, top in enumerate(tops[1:], 2):
            at = f"file {n} of {len(files)}"
            if name not in top:
                raise ValueError(f"{who}: {at} lacks column {name!r}")
            if _footed(top[name]) != _footed(tops[0][name]):
                was, now = _footed(tops[0][name]), _footed(top[name])
                raise ValueError(
                    f"{who}: {at} has another type for column {name!r} than file 1 ({now}, not {was})"
                )
    return columns


def _csv_columns(file: Mapping[str, Any]) -> list[Column]:
    """A CSV side's columns (§7): every field text, or char with ``padded``; a csv.keys column of its key's
    class; the partitions' text."""
    csv, keys = file["csv"], file["csv"].get("keys") or {}
    columns = []
    for name in csv["columns"]:
        spec = keys.get(name)
        cls = spec["as"] if spec else "char" if csv.get("padded") else "text"
        declared = FIELD if spec is None else f"{FIELD} as {spec['as']}"
        declared += f"(38,{spec['scale']})" if spec and spec["as"] == "decimal" else ""
        columns.append(Column(name, declared, cls, True, cls in ("text", "char"), True))
    return columns + [
        Column(name, PARTITION, "text", True, True, True) for name in file.get("partitions") or []
    ]


# The lines a cursor's statements did not take as rows, by file, statement and line, each one's errors in
# column order (U16: they store them in one pair of tables, under a scan id each): never a line's text or
# DuckDB's message.
_REJECTED = (
    "SELECT s.file_path, e.scan_id, e.line, e.error_type FROM reject_errors e"
    " JOIN reject_scans s USING (scan_id, file_id) ORDER BY s.file_path, e.scan_id, e.line, e.column_idx"
)


def _p11(side: Plan, who: str) -> None:
    """P11 of a CSV side: its filter compares a column, which is text, with a number or TRUE or FALSE."""
    if not side.get("where"):
        return
    pending = [where.parse(side["where"])]
    while pending:
        node = pending.pop()
        pending += node.children
        for value in node.values if node.op != "like" else ():
            if value.kind != "str" and _csv(side["declared"].get(node.column or "", FIELD)):
                what = "a number: quote the literal" if value.kind == "num" else "TRUE or FALSE: quote it"
                raise ValueError(
                    f"{who}: the filter compares column {node.column!r} ({FIELD}, which is text) with {what}"
                    " to compare text"
                )


async def _count(session: _Session, side: Plan, who: str) -> dict[str, Any]:
    """Steps 6 and 7 (§6), on the session's own cursor: one pass counting the rows, those the filter selects
    and the texts a scan of the side would sort, from which ``sort_bytes`` projects its spill (step 8), every
    declared column read so no later statement rejects a line it took (U13); a CSV side's lines that are not
    rows, each (file, line) once by its kind, from the rejects of the pass and of its end check (U16, U21;
    over ``REJECT_MAX`` refuses, P24). A Parquet side's rows and its worker columns' bytes are its footers',
    as the plan's walk read them (``manifest.Pinned.footed``): no statement reads a footer's figures or its
    statistics as text (U30).
    Then the first ``FLOOD`` rows of file 1 converted by csv.keys and typeMap (P25)."""
    file, binds = side["file"], {}
    keys = compose._keys(PRIMITIVES, side, binds)
    condition = compose._filter(PRIMITIVES, side, binds)
    worker = compose.worker_columns(side) if file["format"] == "csv" else []
    texts = [(SORT_KEYS, key) for key in keys]
    texts += [(1, PRIMITIVES.render(c, side["classes"][c], side["declared"][c])) for c in worker]
    sized = " + ".join(f"{copies} * coalesce(strlen({text}), 0)" for copies, text in texts)
    keyed = " AND ".join(f"{key} IS NOT NULL" for key in keys)
    more = ", store_rejects=true, rejects_limit=1001" if file["format"] == "csv" else ""
    # A date key outside the years 0001 to 9999 has a text of another form (10000-01-01, 0044-03-15 (BC),
    # infinity), which no key holds (``_fits``): its row would be scanned and never found by its key.
    dated = [
        (column, f"regexp_full_match({text}, '{_MOMENT}')")
        for column, text, mapped in zip(side["keys"], keys, key_maps(side), strict=True)
        if side["classes"][column] == "date" and not mapped
    ]
    counted = (
        "count(*), count(*) FILTER (WHERE s), count(*) FILTER (WHERE s AND k), sum(b) FILTER (WHERE s AND k)"
    ) + "".join(f", count(*) FILTER (WHERE s AND NOT d{n})" for n in range(len(dated)))
    read = "".join(f", {form} AS d{n}" for n, (_, form) in enumerate(dated))
    counting = (
        f"SELECT {counted} FROM (SELECT ({condition or 'TRUE'}) IS TRUE AS s, {keyed} AS k,"  # noqa: S608
        f" {sized} AS b{read} FROM {_relation(side, more)})"
    )
    started = time.monotonic()
    ((rows, selected, sorted_, texts_bytes, *outside),) = await session.rows(
        side, counting, binds, timeout_s=_left(side), tags="i" * (4 + len(dated))
    )
    pass_s = round(time.monotonic() - started, 1)
    for (column, _), held in zip(dated, outside, strict=True):
        if held:
            raise ValueError(
                f"{who}: key column {column!r} holds {held:,} date(s) outside the years 0001 to 9999, which"
                " have no key text: filter those rows out, or export the key as text"
            )
    skipped = (file.get("csv") or {}).get("skip", 0) * len(side["pinned"]["files"])  # by the spec, each file
    counted = {"rows": rows, "selected": selected, "rejected": 0, "kinds": {}, "pass_s": pass_s}
    counted |= {"lines_skipped": skipped}
    if file["format"] == "csv":
        lines = await _lines(session, side, who, rows, more)
        counted |= {"rejected": len(lines), "kinds": dict(sorted(Counter(lines.values()).items()))}
        if lines:  # by position and kind, never a name or a line's text (D12)
            paths = [os.path.join(session.target.root or "", name) for name in side["pinned"]["files"]]
            shown = [
                f"file {paths.index(path) + 1} line {line}: {kind}" for (path, line), kind in lines.items()
            ]
            manifest.log.warning(
                "%s: %d line(s) of its files are not rows: %s", who, len(lines), "; ".join(shown[:20])
            )
        if len(lines) > REJECT_MAX:
            raise ValueError(
                f"{who}: over {REJECT_MAX:,} lines of its files are not rows of the declared CSV dialect: the"
                " dialect does not fit (ds target check drafts one)"
            )
    else:  # a Parquet side's rows and compared columns' sizes are its footers', as the plan's walk read them
        counted["rows"], footed = side["pinned"]["footed"]
        texts_bytes = (texts_bytes or 0) + sum(footed.get(c, 0) for c in compose.worker_columns(side))
    hashed = 32 if compose.engine_columns(side) else 0
    values = SORT_KEYS * len(keys) + len(compose.worker_columns(side)) + bool(hashed)
    row = SORT_VALUE * values + hashed + SORT_ROW
    counted["sort_bytes"] = math.ceil(SORT_MARGIN * (int(texts_bytes or 0) + row * sorted_))
    await _flood(session, side, who)
    return counted


Lines = dict[tuple[str, int], str]  # (file, line) to the kind of a line that is not a row


async def _rejected(session: _Session, side: Plan, before: set[int]) -> tuple[Lines, set[int]]:
    """The lines the cursor's statements did not take as rows, but those of the scans ``before``, each by the
    kind of its first error by column; and every scan's id. Each is of a file of the side's, by numbers:
    rows of anything else are no answer of DuckDB's, and nothing of them is kept or worded."""
    found = await session.rows(side, _REJECTED, {}, timeout_s=_left(side), tags="siis")
    paths = {os.path.join(session.target.root or "", name) for name in side["pinned"]["files"]}
    if not all(row[0] in paths and None not in row for row in found):
        raise session.outside()
    lines: Lines = {}
    for path, scan, line, kind in found:
        if scan not in before:
            lines.setdefault((path, line), _REJECTS.get(kind, "other"))
    return lines, {scan for _, scan, _, _ in found}


async def _lines(session: _Session, side: Plan, who: str, rows: int, more: str) -> Lines:
    """A CSV side's lines that are not rows: the counting pass's and, of a side that declares a quote, its
    files' last records cut inside a quoted field, which the reader drops without a reject (U21). DuckDB's
    parallel reader records one, so each file is read once more with that one, alone (several files in one
    statement are read as the reader reads them). A scan of it that starts inside a quoted field takes the
    field's lines for rows, or for lines that are not (U20), so it must have read the pass's ``rows`` and its
    lines, and beyond them nothing but a file's last line, cut in a quote: else no file's end is known, and
    the plan is refused (P33)."""
    counted, before = await _rejected(session, side, set())
    if not side["file"]["csv"].get("quote", '"') or len(counted) > REJECT_MAX:
        return counted
    ended = f"SELECT count(*) FROM {_relation(side, more)}"  # noqa: S608 (constants, names)
    parallel, read = {"r_parallel": True}, 0
    for name in side["pinned"]["files"]:
        ((found,),) = await session.rows(
            side, ended, {}, files=[name], reader=parallel, timeout_s=_left(side), tags="i"
        )
        read += found
    lines, _ = await _rejected(session, side, before)
    last: dict[str, int] = {}
    for path, line in lines:
        last[path] = max(line, last.get(path, 0))
    cut = {at: kind for at, kind in lines.items() if at not in counted}
    alike = all(lines.get(at) == kind for at, kind in counted.items())
    if read != rows or not alike or any(kind != "quote" or at[1] != last[at[0]] for at, kind in cut.items()):
        raise ValueError(
            f"{who}: its files hold line ends inside quoted fields, and whether a file ends inside one could"
            " not be checked: export them without line ends inside fields, or as Parquet"
        )
    return counted | cut


async def _flood(session: _Session, side: Plan, who: str) -> None:
    """Step 7: the first ``FLOOD`` rows of file 1, each csv.keys column's key text and each typeMap column's
    value converted; a column failing in over half refuses (P25)."""
    file, by = side["file"], {column: canonical for canonical, column in side["columns"].items()}
    keyed = [c for c in side["keys"] if c in ((file.get("csv") or {}).get("keys") or {})]
    mapped = [(side["columns"][canonical], spec) for canonical, spec in side["types"].items()]
    if not keyed and not mapped:
        return
    texts = [PRIMITIVES.key_text(c, side["classes"][c], side["declared"][c]) for c in keyed]
    texts += [f"t.{PRIMITIVES.ident(c)}" for c in keyed]
    texts += [PRIMITIVES.render(c, side["classes"][c], side["declared"][c]) for c, _ in mapped]
    statement = f"SELECT {', '.join(texts)} FROM {_relation(side)} LIMIT {FLOOD}"  # noqa: S608
    rows = await session.rows(side, statement, {}, files=side["pinned"]["files"][:1], timeout_s=_left(side))
    failed = Counter[str]()
    for row in rows:
        for i, column in enumerate(keyed):
            failed[column] += row[i] is None and row[len(keyed) + i] not in (None, "")
        for (column, spec), value in zip(mapped, row[2 * len(keyed) :], strict=True):
            try:
                convert(render.value(side["classes"][column], value, by.get(column, column)), spec)
            except ValueError:
                failed[column] += 1
    specs = {c: file["csv"]["keys"][c]["as"] for c in keyed} | {c: spec["as"] for c, spec in mapped}
    for column, count in failed.items():
        if 2 * count > len(rows):
            raise ValueError(
                f"{who}: column {column!r} is declared {specs[column]} and {count:,} of the first"
                f" {len(rows):,} rows do not read as one: declare the format the export holds (typeMap"
                " format), or string"
            )


def _declaration(side: Plan) -> str:
    """The digest of what a side's counts depend on beside its bytes: its table member as planned."""
    members = ("file", "keys", "key_maps", "columns", "where", "types", "digest", "classes", "declared")
    return hashlib.sha256(
        jsonio.dumps({m: side.get(m) for m in members}, sort_keys=True).encode()
    ).hexdigest()


class Files:
    """``connectors.tables.Engine`` over files a target's root holds, read by the process's reader
    (docs/mvp/FILES.md). A side's plan holds its pins as ``pinned`` once its run opened them (§6)."""

    name = "files"
    arms = 200
    unfit = ()  # no key is unfit here: the join compares texts (§9)
    fold = staticmethod(str)  # a column is named as spelled
    hides = False
    grant = "SELECT"
    sum_digits = None
    empty_null = False
    retry_alone = False  # each key alone would be a pass over the files

    def built(self, column: str, cls: str, declared: str, key: bool) -> str | None:
        return (PRIMITIVES.key_text if key else PRIMITIVES.render)(column, cls, declared)

    async def connect(self, target: Target, *, timeout_s: float = 0) -> _Session:
        return _Session(target, timeout_s)

    @asynccontextmanager
    async def snapshot(self, target: Target, *, timeout_s: float = 0) -> AsyncIterator[_Session]:
        """A session of files that cannot change under it (a change fails the run, §11), holding ``SORT``."""
        async with SORT.held():
            session = await self.connect(target, timeout_s=timeout_s)
            try:
                yield session
            finally:
                await session.close()

    async def server(self, session: _Session) -> dict[str, Any]:
        return {"version": version("duckdb"), "charset": None, "tls": None}

    def release(self, session: _Session) -> str:
        return "duckdb " + ".".join(version("duckdb").split(".")[:2])

    async def describe(
        self, session: _Session, schema: str | None, name: str, *, side: Any = None
    ) -> Relation | None:
        """When a run is planned (``side`` its ``SideConfig``), §6's steps 1 to 5: the files pinned and their
        columns read, the relation's facts holding what the plan stores of them and the pins; afterwards
        (``side`` the stored plan) the relation as planned, after a stat check. A table pre-check, which
        names no side, is refused."""
        if side is None:  # a table pre-check's (FILES.md §12: K1, the item a run would refuse)
            raise Refusal(FILE_SIDE)
        if not isinstance(side, SideConfig):
            await _verify(session.target, side)
            if SORT._owner is asyncio.current_task() and _doubted(_set(side)):
                SORT.apart = True  # a pass reads both its file sides apart: one reader, one budget
            return described(side)
        table, letter = side.table, side.side.value
        assert table is not None and table.file is not None
        file, who, files = table.file, f"side {letter}", _configured().files
        compression = (file.get("csv") or {}).get("compression", "none")
        stream = "parquet" if file["format"] == "parquet" else None if compression == "none" else compression
        # Its run's creation's (``creating``): one files.plan_s for every file side, steps 1 to 9 of each.
        end = _ending.get() or time.monotonic() + files.plan_s
        pinned = await manifest.pin(
            session.target, file["path"], who, files, partitions=file.get("partitions", ()),
            as_of_file=file.get("asOfFile"), max_age=file.get("maxAge"), stream=stream, end=end,
        )  # fmt: skip
        held = {
            "side": letter,
            "files": list(pinned.files),
            "pins": [list(p) for p in pinned.pins],
            "end": end,
            "footed": pinned.footed,
        }
        plan = {"file": file, "pinned": held}
        async with _planning(who, session.target, len(pinned.files)):
            await configured()  # P27, P28: before the side's first statement, which words neither
            if file["format"] == "parquet":
                columns = await _footers(session, plan, side, who)
            else:
                await _records(session, plan, who)
                columns = _csv_columns(file)
        facts = {
            "kind": "files", "key": None, "server": self.release(session), "consistency": "fixed",
            "charset": None, "stored": {"file": file} | pinned.facts(), "pinned": held,
        }  # fmt: skip
        return Relation(None, table.name, tuple(columns), facts)

    async def probe(self, session: _Session, side: Plan) -> None:
        """At a run's creation (the side's pins held with their deadline): its filter's literals (P11), its
        statements parsed reading no row, then steps 6 to 9, whose counts the side's plan gains (§6); the
        same bytes under the same declaration are counted once a process. A key table's are worded for it
        (``pinned["who"]``, ``plan.key_plan``'s): the log line of its lines that are not rows names no
        side."""
        pinned = side["pinned"]
        who = pinned.get("who") or f"side {pinned['side']}"
        if side["file"]["format"] == "csv":
            _p11(side, who)
        statement, binds = compose.probe(PRIMITIVES, side)
        try:
            await session.rows(side, statement, binds, timeout_s=_left(side))
        except (Unstarted, TargetUnreachable, ReaderDied):  # no refusal of the filter or the key map
            async with _planning(who, session.target, len(pinned["files"])):
                raise
        cached = (side["files_sha256"], _declaration(side))  # its paths too, where it declares partitions
        async with _planning(who, session.target, len(pinned["files"])):
            counted = _counted.get(cached) or await _count(session, side, who)
            await _verify(session.target, side, planning=True)
        if len(_counted) >= manifest.CACHE_MAX:
            _counted.pop(next(iter(_counted)))
        _counted[cached] = counted
        side |= counted

    def large(self, side: Plan) -> bool:
        # A group is one pass over the files: 200 keys whatever its rows hold, and beside a side whose rows
        # may be large too, which the worker then reads few at a time inside the group (FILES.md §12).
        return False

    async def rows(self, session: _Session, side: Plan, asked: Asked) -> list[Row]:
        """One statement for the group (§9), the files stat-checked before and after it; the documents are
        built in the pool's thread."""
        statement, binds = _keyed(side, asked)
        if not statement:
            return []
        await session.renewed()
        await _verify(session.target, side)
        try:
            found = await session.rows(
                side, statement, binds, then=partial(compose.found, PRIMITIVES, side, asked)
            )
        finally:
            if session.apart is not None:  # a reader apart is left after each read (READER.md §9)
                await session.close()
        await _verify(session.target, side)
        return found

    async def scan(self, session: _Session, side: Plan, after: Sequence[bytes] | None) -> AsyncIterator[Rows]:
        """The files stat-checked, the spill's room for the sort, then the sort, watched: when this returns,
        its rows are sorted."""
        statement, binds = compose.scan(PRIMITIVES, side, after)
        await _verify(session.target, side)
        await session.sort(side)
        await session.send(side, statement, binds, watch=True)
        wide = _wide(side, compose.worker_columns(side))
        return session.batches(side, compose.digested(PRIMITIVES, side), wide)

    async def census(self, session: _Session, side: Plan) -> AsyncIterator[Rows]:
        statement, binds = compose.census(PRIMITIVES, side)
        await _verify(session.target, side)
        await session.sort(side)
        await session.send(side, statement, binds, timeout_s=session.timeout_s, watch=True)
        async for rows in session.batches(side, list, _wide(side, side["keys"])):
            yield rows

    async def frame(self, session: _Session, side: Plan) -> AsyncIterator[Rows]:
        statement, binds = compose.frame(PRIMITIVES, side)
        await _verify(session.target, side)
        await session.sort(side)
        await session.send(side, statement, binds, timeout_s=session.timeout_s, watch=True)
        strata = list(side.get("strata", {}).values())
        async for rows in session.batches(
            side, compose.stratified(side), _wide(side, [*side["keys"], *strata])
        ):
            yield rows

    async def null_keys(self, session: _Session, side: Plan) -> int:
        """One more pass over the files, by a scan, a census or a frame that holds ``SORT``: stat-checked
        before and after as every read, and watched as their sort is, so a mount that stops answering under
        it keeps ``SORT`` no longer than under them (§10)."""
        statement, binds = compose.null_keys(PRIMITIVES, side)
        await _verify(session.target, side)
        ((count,),) = await session.rows(side, statement, binds, watch=True)
        await _verify(session.target, side)
        return int(count)

    async def relations(self, session: _Session, schema: str | None) -> list[tuple[str, str]]:
        raise Refusal(FILE_SIDE)

    async def figures(self, session: _Session, side: Plan, wanted: Sequence[Figure]) -> list[str | None]:
        raise Refusal(FILE_SIDE)

    async def duplicates(self, session: _Session, side: Plan) -> tuple[int, int]:
        raise Refusal(FILE_SIDE)


# -- ``ds target check --table`` (FILES.md §16) -------------------------------------------------------------

SNIFFED = 20_480  # rows of file 1 a draft is sniffed from
# A row a sniffed column, its name and type after the header's flag: a frame carries no list of structs.
_SNIFF = (
    "SELECT Delimiter, Quote, Escape, NewLineDelimiter, HasHeader, unnest(Columns, recursive := true),"
    " DateFormat FROM sniff_csv($file, sample_size=$rows{more})"
)
_DRAFTED = {
    "BIGINT": "integer", "DOUBLE": "decimal", "DATE": "date", "TIMESTAMP": "timestamp", "BOOLEAN": "boolean",
}  # fmt: skip


def _when(seconds: float) -> str:
    return f"{datetime.fromtimestamp(seconds, UTC):%Y-%m-%d %H:%M} UTC"


async def check(
    target: Target, path: str, form: str, *, delimiter: str | None = None, header: bool = False,
    count: bool = False, listed: bool = False,
) -> list[str]:  # fmt: skip
    """``ds target check ID --table PATH --format FORM``'s lines (§16): the files the path names, as a plan's
    steps 1 and 2 find them; a CSV's draft, sniffed from file 1 (the one place a dialect is sniffed: a run
    reads only what its approved spec declares, D5), or a Parquet side's columns with their classes; with
    ``count`` the rows, a pass's seconds and the lines that are not rows, by position and kind; with
    ``listed`` each file's SHA-256 and ``files_sha256``. For ``listed``, and for ``count`` of a compressed
    CSV export, the files are pinned as a plan pins them: each compressed stream read whole (U18: DuckDB
    reads one cut short as fewer rows), a Parquet footer walked. File names are printed here and nowhere
    else (D12). A refusal is a plan's, worded for ``target <id>``."""
    files, who = _configured().files, f"target {target.target_id}"
    if manifest.path_error(path):
        raise ValueError(manifest.PATH_REFUSED)
    async with _planning(who, target, 0):
        await manifest.rooted(target, who)
        found = await manifest.expand(target, path, who, max_files=files.max_files)
    stats = [f.stat for f in found if f.stat is not None]
    names = [f.path for f in found]
    try:
        manifest.arrival(target, found, who)
        settled = "settled"
    except NotReady:
        settled = f"not settled (settle_s {target.settle_s})"
    lines = [
        f"{path} ({form}): {len(found):,} file(s), {sum(stat.st_size for stat in stats):,} bytes, newest"
        f" modified {_when(max(stat.st_mtime for stat in stats))}, {settled}"
    ]
    lines += [
        f"  {n}  {name}  {stat.st_size:,}  {_when(stat.st_mtime)}"
        for n, (name, stat) in enumerate(zip(names, stats, strict=True), 1)
    ]
    root, packed = target.root or "", _packed(names[0]) if form == "csv" else None
    pinned = None
    if listed or (count and packed):  # as a plan pins them: a compressed stream is read whole (U18)
        stream = "parquet" if form == "parquet" else packed
        pinned = await manifest.pin(target, path, who, files, stream=stream)
    held = {"side": "a", "files": names, "pins": [[*manifest._key(stat), ""] for stat in stats]}
    side: Plan = {"file": {"path": path, "format": form}, "pinned": held}
    held["end"] = time.monotonic() + files.plan_s
    session, footers = _Session(target, files.plan_s), []
    try:
        async with _planning(who, target, len(found)):
            for n, name in enumerate(names if form == "parquet" else (), 1):  # as a plan's pin walks them
                try:
                    footers.append(await manifest.call(manifest.footed, os.path.join(root, name), root=root))
                except manifest.BadFooter as exc:
                    raise ValueError(manifest.footer_refused(who, n, len(names), exc)) from None
                except OSError:
                    raise TargetRefused("OSError") from None
            held["footed"] = manifest.summed(footers)
            read = _drafted if form == "csv" else _classed
            try:
                lines += await read(session, side, who, delimiter=delimiter, header=header, count=count)
            except manifest.Busy as exc:
                if str(exc) != "Died" or session.cursor is None:
                    raise
                cause = session.cursor._instance.cause
                raise ValueError(
                    f"{who}: the file reader's process ended ({cause}) while it read these files"
                ) from None
    except ValueError as exc:  # a file that changed under a statement is worded for a side
        said = str(exc).replace("side a:", f"{who}:", 1)
        raise (NotReady(exc.code, said) if isinstance(exc, NotReady) else type(exc)(said)) from None
    finally:
        await session.close()
    if listed and pinned is not None:
        lines += [f"{pin[4]}  {name}" for name, pin in zip(pinned.files, pinned.pins, strict=True)]
        lines.append(f"files_sha256 {manifest.files_sha256(pinned.pins)}")
        bound = manifest.files_sha256(pinned.pins, pinned.files)
        lines.append(f"files_sha256 {bound} where the spec declares partitions (the lines above, whole)")
    return lines


async def mount(target: Target) -> str:
    """A files target's mount for ``ds target check``, ``read-only`` or ``writable``: refused as a plan's
    step 1 refuses it, a directory that does not answer among them (P14, P15, P28)."""
    who = f"target {target.target_id}"
    async with _planning(who, target, 0):
        return await manifest.rooted(target, who)


def _packed(name: str) -> str | None:
    """The compression a CSV file's name drafts (``.gz``, ``.zst``)."""
    return {".gz": "gzip", ".zst": "zstd"}.get(os.path.splitext(name)[1])


def _member(spec: Mapping[str, Any]) -> str:
    """A draft's mapping as YAML's flow style writes it: a text quoted, a word or a list as it is."""
    shown = (f"{k}: {v if k in ('as', 'columns') else json.dumps(v)}" for k, v in spec.items())
    return "{" + ", ".join(shown) + "}"


async def _sniffed(cursor: Cursor, side: Plan, asked: Mapping[str, Any], more: str) -> Rows:
    """File 1 as DuckDB's sniffer reads it under the options ``more``, a row a column; none where it finds
    no dialect (its ``InvalidInputException``: any other failure is a read's, P29)."""
    statement = _SNIFF.format(more=more)
    values = {name: value for name, value in asked.items() if f"${name}" in statement}
    try:
        await cursor.execute(statement, values, timeout_s=_left(side))
    except TargetRefused as exc:
        if code(exc) != "InvalidInputException":
            raise
        return []
    sniffed: Rows = []
    while fetched := await cursor.fetch(BATCH, timeout_s=_left(side), tags="ssssosss"):
        sniffed += fetched
    return sniffed


async def _draft(
    session: _Session, side: Plan, who: str, sniffed: Rows, *, header: bool, rows_only: bool
) -> tuple[int, dict[str, Any], list[str]]:
    """A sniffed dialect as a draft: the fields of file 1's first ``SNIFFED`` rows that it reads, the
    side's ``csv`` member (left on the side) and the draft's lines, its fields named from the first record
    only with ``header`` (the sniffer may take a row for a header, so without it they are ``column 1 ...``
    and no value is printed, whatever it took). A column of digits whose sampled values start with 0 is
    noted: the sniffer leaves it text, and as text 00123 is not 123. A ``.gz`` or ``.zst`` name drafts its
    compression. ``rows_only``: it was sniffed from the lines that are rows, which is noted."""
    first = side["pinned"]["files"][:1]
    delim, quote, escape, newline, headed, _, _, dated = sniffed[0]
    columns = [{"name": name, "type": kind} for *_, name, kind, _ in sniffed]
    named = [str(c["name"]) if header else f"column {i}" for i, c in enumerate(columns, 1)]
    ends = {escaped: end for end, escaped in _NEWLINES.items()}
    if newline not in ends:  # a lone \r: no csv.newline declares it
        raise ValueError(f"{who}: file 1 ends its lines with neither \\n nor \\r\\n: convert the export")
    dialect: dict[str, Any] = {"delimiter": delim, "newline": ends[newline]}
    dialect |= {"header": True} if headed else {}
    dialect |= {"compression": packed} if (packed := _packed(first[0])) else {}
    if quote not in ("(empty)", '"'):
        dialect |= {"quote": quote, "escape": quote if escape == "(empty)" else escape}
    elif escape not in ("(empty)", '"'):  # its own beside the default quote (a backslash): else no row reads
        dialect |= {"escape": escape}
    side["file"]["csv"] = dialect | {"columns": [f"c{i}" for i in range(len(columns))]}
    typed = {
        name: {"as": _DRAFTED[c["type"]]}
        | ({"format": dated} if c["type"] == "DATE" and dated not in (None, "%Y-%m-%d") else {})
        for name, c in zip(named, columns, strict=True)
        if c["type"] in _DRAFTED
    }
    shown = dialect | {"columns": f"[{', '.join(named)}]"}
    lines = [
        f"draft (sniffed from the first {SNIFFED:,} rows of file 1): a run reads only what the approved spec"
        " declares",
        f"  csv: {_member(shown)}",
    ]
    if typed:
        drafted = ", ".join(f"{name}: {_member(spec)}" for name, spec in typed.items())
        lines.append(f"  typeMap: {{{drafted}}}")
    if headed and not header:
        lines.append("note: the first record of file 1 reads as a header: --header names its fields from it")
    if rows_only:
        lines.append("note: drafted from the lines of file 1 that read as rows: --count lists the others")
    zeros = ", ".join(
        f"count(*) FILTER (WHERE regexp_full_match(c{i}, '0[0-9]+')),"
        f" bool_and(regexp_full_match(c{i}, '[0-9]+'))"
        for i in range(len(columns))
    )
    sampled = f"SELECT count(*), {zeros} FROM (SELECT * FROM {_relation(side)} LIMIT {SNIFFED})"  # noqa: S608
    ((rows, *padded),) = await session.rows(
        side, sampled, {}, files=first, timeout_s=_left(side), tags="i" + "io" * len(columns)
    )
    lines += [
        f"note: {name} holds only digits and {zeroed:,} sampled values start with 0: left as text"
        for name, zeroed, digits in zip(named, padded[::2], padded[1::2], strict=True)
        if zeroed and digits
    ]
    return rows * len(columns), side["file"]["csv"], lines


async def _drafted(
    session: _Session, side: Plan, who: str, *, delimiter: str | None, header: bool, count: bool
) -> list[str]:
    """A CSV side's draft: file 1's dialect and types as DuckDB sniffs them from its first ``SNIFFED`` rows,
    under ``delimiter`` when one is given. The sniffer wants every sampled line a row: one line of another
    shape (an export's trailer record) and it splits no field, drafts the odd line's shape or, held to a
    delimiter, finds no dialect. So a file it reads as text at all is sniffed a second time from the lines
    that are rows, as a run reads a file (``ignore_errors``; never a file it refuses whatever the options,
    one of another encoding, whose lines that reading would leave out too), and where the two differ the
    draft is the one whose dialect reads more of the sample's fields (``_draft``; the sniffer's own of
    equals). With ``count``, the files read whole by the drafted dialect, as a plan's counting pass reads
    them."""
    cursor, first = await session._opened(side), side["pinned"]["files"][:1]
    asked = {"file": os.path.join(session.target.root or "", first[0]), "rows": SNIFFED, "delim": delimiter}
    more = (", delim=$delim" if delimiter else "") + (", header=true" if header else "")
    found = [await _sniffed(cursor, side, asked, more)]
    if found[0] or (more and await _sniffed(cursor, side, asked, "")):
        found.append(await _sniffed(cursor, side, asked, more + ", ignore_errors=true"))
    drafts = [
        await _draft(session, side, who, sniffed, header=header, rows_only=bool(n))
        for n, sniffed in enumerate(found)
        if sniffed and (n == 0 or sniffed != found[0])
    ]
    if not drafts:
        raise ValueError(
            f"{who}: no CSV dialect could be drafted from file 1: write its csv block by hand (delimiter,"
            " header, quote, escape, newline, encoding)"
        )
    _, side["file"]["csv"], lines = max(drafts, key=lambda draft: draft[0])
    if count:
        more = ", store_rejects=true, rejects_limit=1001"
        counting = f"SELECT count(*) FROM {_relation(side, more)}"  # noqa: S608 (constants)
        started = time.monotonic()
        ((rows,),) = await session.rows(side, counting, {}, timeout_s=_left(side), tags="i")
        pass_s = time.monotonic() - started
        rejected = await _lines(session, side, who, rows, more)
        paths = [os.path.join(session.target.root or "", name) for name in side["pinned"]["files"]]
        lines.append(
            f"count (as drafted): {rows:,} rows, a pass {pass_s:.1f} s, {len(rejected):,} line(s) not rows"
        )
        for (at, line), kind in sorted(rejected.items()):
            lines.append(f"  file {paths.index(at) + 1} line {line}: {kind}")
    return lines


async def _classed(session: _Session, side: Plan, who: str, *, count: bool, **_: Any) -> list[str]:
    """A Parquet side's columns as DuckDB reads file 1's, each with its class, or that a plan reading it is
    refused (P23), or the typeMap a plan needs of a column read to the microsecond; with ``count``, the rows
    the footers hold and the seconds of a pass over the first column."""
    first, relation = side["pinned"]["files"][:1], _relation(side)
    await session.send(side, f"SELECT * FROM {relation} LIMIT 0", {}, files=first, timeout_s=_left(side))  # noqa: S608
    assert session.cursor is not None
    read = await session.cursor.columns(timeout_s=_left(side))
    tops = _tops(await session.rows(side, _SCHEMA, {}, files=first, timeout_s=_left(side), tags=_SCHEMA_TAGS))

    def classed(name: str, kind: str) -> str:
        cls, why = _parquet_type(name, kind, tops.get(name), {})
        needs = why.partition("declare ")[2].partition(" to compare")[0]
        return cls or (f"read to the microsecond: a plan needs {needs}" if needs else "not comparable")

    rows = [(name, kind, classed(name, kind)) for name, kind in read]
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    lines = [f"  {name.ljust(widths[0])}  {kind.ljust(widths[1])}  {cls}" for name, kind, cls in rows]
    if count:
        total = side["pinned"]["footed"][0]
        column = f"SELECT count({PRIMITIVES.ident(read[0][0])}) FROM {relation}"  # noqa: S608 (a footer's name)
        started = time.monotonic()
        await session.rows(side, column, {}, timeout_s=_left(side))
        pass_s = time.monotonic() - started
        lines.append(f"count: {total:,} rows by the footers, a pass over the first column {pass_s:.1f} s")
    return lines
