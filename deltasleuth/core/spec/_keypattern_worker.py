"""Isolated stdlib regex worker. Only bounded JSON frames arrive on stdin; one boolean leaves."""

from __future__ import annotations

import functools
import json
import math
import re
import resource
import sys
import warnings

MAX_FRAME = 5 * 2**20  # encoded frame, independently of the caller's character bounds
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
if sys.platform == "linux":
    resource.setrlimit(resource.RLIMIT_DATA, (128 * 2**20, 128 * 2**20))


@functools.lru_cache(maxsize=128)
def compiled(pattern: str, flags: int) -> re.Pattern[str]:
    # The parent already compiled this exact source and surfaced these warnings. Repeating them
    # into an idle stderr pipe could eventually block a reused worker; runtime errors still report.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        result = re.compile(pattern, flags)
    re.purge()  # the bounded cache above owns compiled patterns
    return result


def read(size: int) -> bytes:
    result = sys.stdin.buffer.read(size)
    if len(result) != size:
        raise EOFError("incomplete key-pattern frame")
    return result


def main() -> None:
    while header := sys.stdin.buffer.read(4):
        if len(header) != 4:
            raise EOFError("incomplete key-pattern frame header")
        size = int.from_bytes(header, "big")
        if size > MAX_FRAME:
            raise ValueError("key-pattern frame exceeds its size limit")
        # The parent kills a timed-out operation. This advancing CPU limit also ends a busy
        # worker after parent death, while allowing a reused worker to do legitimate work.
        used = resource.getrusage(resource.RUSAGE_SELF)
        _, hard = resource.getrlimit(resource.RLIMIT_CPU)
        soft = math.ceil(used.ru_utime + used.ru_stime) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (soft if hard < 0 else min(soft, hard), hard))
        pattern, flags, text = json.loads(read(size))
        matched = compiled(pattern, flags).search(text) is not None
        sys.stdout.buffer.write(b"1" if matched else b"0")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
