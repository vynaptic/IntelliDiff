"""Finding signatures (§8.2): where and how a difference occurs, never which key or values.

Root causes group by signature and runs compare by it. With the spec the pattern is the published
one, so a member name that is data (a map keyed by email) is never hashed unsalted, where a guess
of the name would reverse it.
"""

from __future__ import annotations

import hashlib

from deltasleuth.core.types import CompiledSpec, Finding
from deltasleuth.security.tokenise import mask_path_pattern


def signature_hash(f: Finding, spec: CompiledSpec | None = None) -> str:
    """16 hex chars of sha256 over path pattern, change type, value class and taxonomy code."""
    pattern = f.path_pattern if spec is None else mask_path_pattern(f.path_pattern, spec)
    parts = (pattern, f.change_type.value, f.value_class or "", f.taxonomy_code or "")
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]  # fits ds.hex_digest
