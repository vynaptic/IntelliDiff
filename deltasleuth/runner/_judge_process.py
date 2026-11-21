"""A judge process (``runner.judges``), run by its path: it judges the groups of keys its input carries and
writes each group's judgements on its output, a frame each, until its input ends.

Its parent starts it isolated (``-I``), with none of its own variables and no argument but this path, and
it loads the package beside this file (``_package``). It loads no settings and holds no key: it imports
the engine alone, and judges with the tokeniser stood in for (``core.judge``). What it is sent, read bodies
and their headers among them, comes on a pipe from the parent that started it, which is trusted as such;
what it answers is unpickled by its parent with the engine's own types allowed and nothing else. It makes
no core file, and its error output is discarded by its parent.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pickle
import resource
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO

# The protocol, which ``runner.judges`` reads from here.
HEADER = 8  # bytes of a frame's length, big-endian
SPECS = 8  # the compiled specs a judge keeps, the least recently used given up first
READY = ("ready", 1)  # the first frame, once the engine is imported: the protocol's version


def _exactly(source: BinaryIO, size: int) -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise EOFError("a frame ended early")
    return data


def _frame(source: BinaryIO) -> bytes | None:
    """The next frame; None when the input ended between two."""
    header = source.read(HEADER)
    if not header:
        return None
    if len(header) != HEADER:
        raise EOFError("a frame ended early")
    return _exactly(source, int.from_bytes(header, "big"))


def _send(sink: BinaryIO, data: bytes) -> None:
    sink.write(len(data).to_bytes(HEADER, "big"))
    sink.write(data)
    sink.flush()


def _package() -> None:
    """Load the package this file is part of, whatever else ``sys.path`` holds, so a judge runs the very
    engine its parent runs; ``sys.path`` itself is left as it is."""
    root = str(Path(__file__).resolve().parents[2])
    found = importlib.machinery.PathFinder.find_spec("deltasleuth", [root])
    if found is None or found.loader is None:
        raise ImportError("the package beside this judge was not found")
    package = sys.modules["deltasleuth"] = importlib.util.module_from_spec(found)
    found.loader.exec_module(package)


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core of read values
    _package()
    from deltasleuth.core.judge import judge_keyless  # once the package is the one beside this file

    source, sink = sys.stdin.buffer, os.fdopen(os.dup(1), "wb")
    quiet = os.open(os.devnull, os.O_WRONLY)
    os.dup2(quiet, 1)  # whatever else writes on the output goes nowhere, never into a frame
    os.close(quiet)
    specs: OrderedDict[str, Any] = OrderedDict()  # by content hash
    _send(sink, pickle.dumps(READY))
    while (frame := _frame(source)) is not None:
        # The parent that started this process wrote it.
        content_hash, pinned, caps, today, items = pickle.loads(frame)  # noqa: S301
        if pinned is not None:
            specs[content_hash] = pickle.loads(pinned)  # noqa: S301 - the parent's, as above
        spec = specs.get(content_hash)
        if spec is None:  # given up: the parent sends it again
            _send(sink, pickle.dumps(None))
            continue
        specs.move_to_end(content_hash)
        while len(specs) > SPECS:
            specs.popitem(last=False)
        judged = [
            judge_keyless(spec, caps, key, observations, states, today=today)
            for key, observations, states in items
        ]
        try:
            answer = pickle.dumps(judged, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:  # too deep to pickle, say: the parent judges the group itself
            answer = pickle.dumps([None] * len(items))
        _send(sink, answer)


if __name__ == "__main__":
    main()
