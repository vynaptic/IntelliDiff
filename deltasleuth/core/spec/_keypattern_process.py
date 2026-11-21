"""Two reusable, isolated CPython regex workers for syntax outside the shared ASCII grammar."""

from __future__ import annotations

import atexit
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

SEARCH_SECONDS = 0.25  # includes a cold interpreter's startup
MAX_FRAME = 5 * 2**20


class WorkerError(ValueError):
    """No match result was obtained within the worker limits; never interpreted as no-match."""


class _Worker:
    def __init__(self) -> None:
        script = Path(__file__).with_name("_keypattern_worker.py")
        self.proc = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed package file
            [sys.executable, "-I", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env={},
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        os.set_blocking(self.proc.stdin.fileno(), False)
        os.set_blocking(self.proc.stdout.fileno(), False)

    def search(self, payload: bytes) -> bool:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        outgoing = memoryview(len(payload).to_bytes(4, "big") + payload)
        sink, source = self.proc.stdin.fileno(), self.proc.stdout.fileno()
        deadline = time.monotonic() + SEARCH_SECONDS
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise WorkerError("key.pattern exceeds its search time limit; simplify the pattern")
            readable, writable, _ = select.select([source], [sink] if outgoing else [], [], left)
            if writable:
                outgoing = outgoing[os.write(sink, outgoing) :]
            if readable:
                answer = os.read(source, 1)
                if answer not in (b"0", b"1") or outgoing:
                    raise WorkerError("key.pattern evaluation exceeded its worker resource limits")
                return answer == b"1"

    def close(self) -> str:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=1)
        diagnostic = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        return diagnostic

    def disown(self) -> None:
        # A fork inherits descriptors, not ownership of the parent's worker processes. These
        # unbuffered streams hold no buffered-I/O lock left behind by another parent thread.
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        self.proc.returncode = 0  # its destructor must not try to reap the parent's child


class _Pool:
    def __init__(self) -> None:
        self.slots = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.workers: set[_Worker] = set()
        self.idle: list[_Worker] = []

    def search(self, payload: bytes) -> bool:
        if not self.slots.acquire(timeout=SEARCH_SECONDS):
            raise WorkerError("key.pattern evaluation capacity is busy; retry later")
        worker = None
        try:
            with self.lock:
                worker = self.idle.pop() if self.idle else _Worker()
                self.workers.add(worker)
            try:
                answer = worker.search(payload)
            except (WorkerError, OSError) as exc:
                with self.lock:
                    self.workers.remove(worker)
                diagnostic = worker.close()
                if diagnostic:
                    exc.add_note(diagnostic)
                raise
            with self.lock:
                self.idle.append(worker)
            return answer
        except OSError as exc:
            raise WorkerError("key.pattern evaluation worker is unavailable; retry later") from exc
        finally:
            self.slots.release()

    def close(self) -> None:
        with self.lock:
            workers, self.workers, self.idle = self.workers, set(), []
        for worker in workers:
            worker.close()


_POOL = _Pool()


def _after_fork() -> None:
    global _POOL
    for worker in _POOL.workers:
        worker.disown()
    _POOL = _Pool()


os.register_at_fork(after_in_child=_after_fork)
atexit.register(lambda: _POOL.close())


def search(pattern: str, flags: int, text: str) -> bool:
    payload = json.dumps((pattern, flags, text)).encode()  # ASCII also preserves lone surrogate escapes
    if len(payload) > MAX_FRAME:
        raise WorkerError("key.pattern input exceeds its worker frame limit")
    return _POOL.search(payload)
