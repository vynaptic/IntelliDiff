"""The worker's judge processes (``worker.judge_processes``): a group's judging away from the interpreter
lock of the worker's own process, where its slots read and save.

``Judges`` starts a judge (``_judge_process``) when a group finds none free, up to its number, and gives
each group to one free judge in the order the slots ask. A judge is this interpreter started anew on that
file's path (``subprocess``: fork then exec at once, so nothing of this process's memory, its keys
included, goes on in it), isolated (``-I``), with none of this process's variables (``env={}``), no
argument but the path, its error output discarded and in a session of its own. It loads no settings, no
keyring and no tokeniser or evidence key: this process opens the evidence and sends each key's reads,
bodies and headers unmasked on the judge's pipe (as the file reader's rows come back on its own), with
what the run's targets declare and the compiled spec, pickled once and sent once to each judge, which
keeps it by its content hash. It takes back each key's result, or the key's observed states when
comparing them asks the tokeniser (``core.judge``), which it then compares itself, and masks and saves
them as it does a thread's. A judge is not a sandbox: it runs the engine's code as a thread of the
worker would, and its answer is unpickled here with the engine's own types allowed and nothing else.

A judge whose process ends while it judges fails that group alone: its keys are judged again one at a
time, each on a judge that starts afresh, and a key whose judging ends a judge again is INCONCLUSIVE
(``resource_limit``, ``ENDED_DETAIL``). A judge that does not start, answers outside the protocol or meets
another fault is ended, and its group judged in a thread of this process, as without judges; after one did
not start, the groups of the next ``RETRY_S`` are too. ``close`` ends every judge, and the worker closes
its judges as it stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import pickle
import select
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from datetime import date
from enum import Enum
from typing import Any

from deltasleuth.core import types as engine_types
from deltasleuth.core.judge import Capabilities, States
from deltasleuth.core.types import CompiledSpec, KeyResult, Observation, Side, SideState
from deltasleuth.runner import _judge_process as process

START_S = 30.0  # for a judge to import the engine and say it is ready
RETRY_S = 60.0  # after a judge did not start: the groups judged in a thread meanwhile
REAP_S = 5.0  # for an ended judge's process to be reaped
CHUNK = 2**20  # bytes read from a judge's output at once
ENDED_DETAIL = "judging this key ended a judge process"
log = logging.getLogger("deltasleuth.runner.worker")  # the worker's lines, whichever of its parts writes them

Item = tuple[str, dict[Side, Observation], States]  # a key, its reads by side, the states of sides not read


class Ended(Enum):
    """What a key's judging gave when it ended a judge's process twice."""

    JUDGE = "ended"


Judged = KeyResult | States | Ended | None  # None: judged in a thread here (``core.judge``)


class JudgeEnded(Exception):
    """A judge's process ended before it answered."""


class JudgeUnstarted(Exception):
    """A judge's process did not start, or did not say it was ready within ``START_S``."""


class JudgeAnswered(Exception):
    """A judge answered outside the protocol."""


class _Answer(pickle.Unpickler):
    """A judge's answer: plain data and the engine's own types (``core.types``) and ``Decimal``, nothing
    else."""

    def find_class(self, module: str, name: str) -> Any:
        found = getattr(engine_types, name, None) if module == engine_types.__name__ else None
        if isinstance(found, type) and found.__module__ == module:
            return found
        if (module, name) == ("decimal", "Decimal"):
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"a judge answered with {module}.{name}")  # a type's name, no value


def loads(data: bytes) -> Any:
    """A judge's answer unpickled (``_Answer``)."""
    return _Answer(io.BytesIO(data)).load()


class _Judge:
    """One judge: its process, and the specs it keeps (by content hash, least recently used first, as it
    keeps them). Its pipes are used by one thread at a time, and closed by whoever is last to use them."""

    def __init__(self) -> None:
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - this interpreter and this package's own file, by path
                [sys.executable, "-I", "-X", "utf8", process.__file__], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, close_fds=True,
                start_new_session=True, bufsize=0, cwd=os.sep,
            )  # fmt: skip
        except OSError as exc:
            raise JudgeUnstarted(f"its process could not be started ({type(exc).__name__})") from None
        self.pinned: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._using = self._ended = False

    @property
    def pid(self) -> int:
        return self.proc.pid

    @contextlib.contextmanager
    def _use(self) -> Iterator[None]:
        with self._lock:
            if self._ended:
                raise JudgeEnded("ended")
            self._using = True
        try:
            yield
        finally:
            with self._lock:
                self._using = False
                ended = self._ended
            if ended:
                self._close_pipes()

    def ready(self) -> None:
        """Wait for its first answer, that it is ready; ``JudgeUnstarted`` if none comes as it must."""
        with self._use():
            assert self.proc.stdout is not None
            readable, _, _ = select.select([self.proc.stdout], [], [], START_S)
            if not readable:
                raise JudgeUnstarted(f"it did not answer within {START_S:g} s")
            try:
                first = loads(self._frame())
            except JudgeEnded:
                raise JudgeUnstarted("it ended as it started") from None
            except Exception:
                raise JudgeUnstarted("it answered outside the protocol") from None
            if first != process.READY:
                raise JudgeUnstarted("it answered outside the protocol")

    def ask(
        self, content_hash: str, spec: bytes, caps: Capabilities, today: date, items: Sequence[Item]
    ) -> list[Judged]:
        """``items`` judged: the spec (``spec``, pickled) is sent only to a judge that does not keep it."""
        with self._use():
            kept = content_hash in self.pinned
            answer = self._exchange(content_hash, None if kept else spec, caps, today, items)
            if answer is None:  # it had given the spec up
                answer = self._exchange(content_hash, spec, caps, today, items)
            self.pinned[content_hash] = None
            self.pinned.move_to_end(content_hash)
            while len(self.pinned) > process.SPECS:
                self.pinned.popitem(last=False)
        return _checked(answer, items)

    def end(self) -> None:
        """Kill its process and reap it; its pipes are closed now, or by the thread using them."""
        with self._lock:
            self._ended = True
            using = self._using
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
        try:
            self.proc.wait(REAP_S)
        except subprocess.TimeoutExpired:
            log.warning("a judge process %d did not end within %g s of being killed", self.proc.pid, REAP_S)
        if not using:
            self._close_pipes()

    def _close_pipes(self) -> None:
        for pipe in (self.proc.stdin, self.proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()

    def _exchange(
        self, content_hash: str, spec: bytes | None, caps: Capabilities, today: date, items: Sequence[Item]
    ) -> Any:
        request = (content_hash, spec, caps, today, list(items))
        self._send(pickle.dumps(request, protocol=pickle.HIGHEST_PROTOCOL))
        try:
            return loads(self._frame())
        except JudgeEnded:
            raise
        except Exception as exc:
            raise JudgeAnswered(type(exc).__name__) from None

    def _send(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        sink = self.proc.stdin.fileno()
        view = memoryview(len(data).to_bytes(process.HEADER, "big") + data)
        try:
            while view:
                view = view[os.write(sink, view) :]
        except OSError:  # its input closed: it ended
            raise JudgeEnded(self._how()) from None

    def _frame(self) -> bytes:
        size = int.from_bytes(self._read(process.HEADER), "big")
        return self._read(size)

    def _read(self, size: int) -> bytes:
        assert self.proc.stdout is not None
        source, chunks, left = self.proc.stdout.fileno(), [], size
        while left:
            try:
                chunk = os.read(source, min(left, CHUNK))
            except OSError:
                chunk = b""
            if not chunk:  # its output ended: it ended
                raise JudgeEnded(self._how())
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def _how(self) -> str:
        """How its process ended, as far as is known yet: its exit status or the signal that ended it."""
        try:
            code = self.proc.wait(REAP_S)
        except subprocess.TimeoutExpired:
            return "its output ended"
        return f"signal {-code}" if code < 0 else f"exit status {code}"


def _checked(answer: Any, items: Sequence[Item]) -> list[Judged]:
    """A judge's answer to ``items``, each a result of its own key, its two states or None."""
    if not isinstance(answer, list) or len(answer) != len(items):
        raise JudgeAnswered("not one answer a key")
    for one, (key, _, _) in zip(answer, items, strict=True):
        if one is None or (isinstance(one, KeyResult) and one.key == key):
            continue
        if not isinstance(one, dict) or set(one) != set(Side):
            raise JudgeAnswered("not a key's result or its states")
        if not all(isinstance(state, SideState) for state in one.values()):
            raise JudgeAnswered("not a key's states")
    return answer


class Judges:
    """At most ``processes`` judges, started as groups need them; ``judge`` gives a group to a free one."""

    def __init__(self, processes: int) -> None:
        self.processes = processes
        self._free = asyncio.Semaphore(processes)  # waiters are served in the order they came
        self._idle: list[_Judge] = []
        self._judges: set[_Judge] = set()  # started and not ended
        self._specs: OrderedDict[str, bytes] = OrderedDict()  # pickled, by content hash
        self._unstarted_until = 0.0
        self._closed = False

    @property
    def pids(self) -> list[int]:
        """The processes of the judges not ended."""
        return sorted(judge.pid for judge in self._judges)

    async def judge(
        self, spec: CompiledSpec, caps: Capabilities, items: Sequence[Item], today: date
    ) -> list[Judged]:
        """Each item judged, in their order: a ``KeyResult``; the key's observed states, which the caller
        compares with the tokeniser; None, which it judges itself; or ``Ended.JUDGE``: judging the key ended
        a judge's process alone, after its group's had ended one."""
        if not items:
            return []
        try:
            return await self._ask(spec, caps, items, today)
        except JudgeEnded as exc:
            log.warning(
                "a judge process ended (%s) while judging %d key(s): they are judged again one at a time",
                exc, len(items),
            )  # fmt: skip
        judged: list[Judged] = []
        for item in items:
            try:
                judged += await self._ask(spec, caps, [item], today)
            except JudgeEnded as exc:
                log.warning("a judge process ended (%s) while judging one key: it is INCONCLUSIVE", exc)
                judged.append(Ended.JUDGE)
        return judged

    async def _ask(
        self, spec: CompiledSpec, caps: Capabilities, items: Sequence[Item], today: date
    ) -> list[Judged]:
        """``items`` judged on one judge; None for each where this process judges them instead."""
        here: list[Judged] = [None] * len(items)
        if self._closed or time.monotonic() < self._unstarted_until:
            return here
        cache_key = spec.judge_key or spec.content_hash
        pickled = self._pickled(spec, cache_key)
        async with self._free:
            judge, answered = (self._idle.pop() if self._idle else None), False
            try:
                if judge is None:
                    judge = _Judge()
                    self._judges.add(judge)
                    await asyncio.to_thread(judge.ready)
                judged = await asyncio.to_thread(judge.ask, cache_key, pickled, caps, today, items)
                answered = True
                return judged
            except JudgeUnstarted as exc:
                self._unstarted_until = time.monotonic() + RETRY_S
                log.error("a judge process did not start: %s; groups are judged here for %g s", exc, RETRY_S)
                return here
            except JudgeAnswered as exc:
                log.warning(
                    "a judge process answered outside the protocol (%s): its group is judged here", exc
                )
                return here
            except JudgeEnded:
                raise
            except Exception as exc:  # a request that did not pickle, say: logged by type, it may hold values
                kind = type(exc).__name__
                log.warning("a group was not judged by a judge process (%s): it is judged here", kind)
                return here
            finally:
                if judge is not None:
                    if answered and not self._closed:
                        self._idle.append(judge)
                    else:
                        self._judges.discard(judge)
                        judge.end()

    def _pickled(self, spec: CompiledSpec, cache_key: str) -> bytes:
        """``spec`` pickled, once while it is among the most recent of its judges' specs."""
        pickled = self._specs.get(cache_key)
        if pickled is None:
            pickled = self._specs[cache_key] = pickle.dumps(spec, protocol=pickle.HIGHEST_PROTOCOL)
        self._specs.move_to_end(cache_key)
        while len(self._specs) > 2 * process.SPECS:
            self._specs.popitem(last=False)
        return pickled

    def close(self) -> None:
        """End every judge, each killed then reaped (a matter of milliseconds), so none is left running;
        a group judged meanwhile is judged in a thread. It neither awaits nor can be cancelled midway."""
        self._closed = True
        judges, self._judges, self._idle = list(self._judges), set(), []
        for judge in judges:
            judge.end()
