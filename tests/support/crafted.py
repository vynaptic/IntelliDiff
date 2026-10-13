"""The kept mutant: a Parquet file of 320 bytes, four
rows of ``id BIGINT, v VARCHAR`` as DuckDB 1.5.5 writes them, one page of ``v`` changed by the fuzz. Its
footer is walked and its schema, its count and its ``id`` are read; a read of ``v`` ends DuckDB's process
(SIGBUS or SIGSEGV on macOS arm64 with 1.5.5). Test-only: it is read in a reader's process, never in the
one that runs the tests.
"""

from __future__ import annotations

import base64

PAGE = base64.b64decode(
    "UEFSMRUAFSgVLCwVCBUKFQYVBgAAFEwCAAAACAGAEAgEAAIAAAAAAAAAABUAFTAVNCwVCBUMFQYVBgAAGFwCAAAACAGAAAgEAgAA"
    "AAAAAAAAAHh4eHgVBBk8NQAYDWR1Y2tkYl9zY2hlbWEVBAAVBCUCGAJpZCUkABUMJQIYAXYlAAAWCBkcGSwmABwVBBkVChkYAmlk"
    "FQIWCBZKFk4mCDwYCAMAAAAAAAAAGAgAAAAAAAAAABYAKAgDAAAAAAAAABgIAAAAAAAAAAAREQAAACYAHBUMGRUMGRgBdhUCFggW"
    "UhZWJlY8GAF4GAF4FgAoAXgYAXgREQAAABacARYIJggWpAEAKChEdWNrREIgdmVyc2lvbiB2MS41LjUgKGJ1aWxkIGQ4Y2RhYTMz"
    "ZmQpGSwcAAAcAAAA4gAAAFBBUjE="
)
