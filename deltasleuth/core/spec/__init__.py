"""Comparison specs and the target registry.

``load_spec_file`` reads a spec, ``validate_spec`` lists its issues and ``compile_spec`` builds the
engine's ``CompiledSpec``. Targets (an endpoint plus its declared behaviour) come from
``load_targets``; credentials only through ``read_secret`` on ``file:`` references.
"""

from __future__ import annotations

from deltasleuth.core.canonical.errors import PathError
from deltasleuth.core.canonical.paths import normalise_pattern, normalise_query
from deltasleuth.core.secrets import SecretError, read_secret
from deltasleuth.core.spec.assemble import key_table, listing_side
from deltasleuth.core.spec.durations import DurationError, parse_duration
from deltasleuth.core.spec.errors import (
    IGNORED,
    UNSUPPORTED,
    SpecError,
    SpecIssue,
    TargetsError,
    UnknownTargetError,
)
from deltasleuth.core.spec.expressions import CEL_VARIABLES
from deltasleuth.core.spec.keys import KEY_NORMALISERS, key_matches, normalise_key
from deltasleuth.core.spec.loader import compile_spec, load_spec_file, validate_spec
from deltasleuth.core.spec.rules import RENDERED_PLACEHOLDERS
from deltasleuth.core.spec.targets import TargetRegistry, load_targets

__all__ = [
    "CEL_VARIABLES",
    "IGNORED",
    "KEY_NORMALISERS",
    "RENDERED_PLACEHOLDERS",
    "UNSUPPORTED",
    "DurationError",
    "PathError",
    "SecretError",
    "SpecError",
    "SpecIssue",
    "TargetRegistry",
    "TargetsError",
    "UnknownTargetError",
    "compile_spec",
    "key_matches",
    "key_table",
    "listing_side",
    "load_spec_file",
    "load_targets",
    "normalise_key",
    "normalise_pattern",
    "normalise_query",
    "parse_duration",
    "read_secret",
    "validate_spec",
]
