"""The signals the tests end a file reader's process by (docs/mvp/READER.md §15). On macOS every process
that ends by SIGSEGV, SIGBUS, SIGABRT or SIGTRAP is a crash to the system, which writes a report and shows
a dialog for it: a real fault is made there only with ``DS_FAULTS=1``, and on Linux always. Test-only.

``QUIET`` ends a process and is no crash anywhere: what a test of the accounting, a charge, a message or a
restart sends, on every platform, and asserts by its name. ``FAULT`` is what a test of a fault itself
sends: SIGSEGV where real faults are made, ``QUIET`` elsewhere. ``real`` marks a test in which DuckDB
itself faults (the kept mutant): skipped where no real fault is made, and run in the image on Linux
(``tests/e2e/test_reader_image.py``) whatever the host.
"""

from __future__ import annotations

import os
import signal
import sys

import pytest

REAL = sys.platform != "darwin" or os.environ.get("DS_FAULTS") == "1"
QUIET = signal.SIGKILL
FAULT = signal.SIGSEGV if REAL else QUIET
real = pytest.mark.skipif(not REAL, reason="DS_FAULTS=1 runs it on macOS, where a fault is a crash report")
