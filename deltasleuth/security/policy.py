"""Tokenisation policy: which value class protects a position (None = clear).

The policy is pure and lives in ``core.masking``, which the engine's classification uses; this module
names it for the callers outside the engine.
"""

from __future__ import annotations

from deltasleuth.core.masking import (
    BUSINESS_CODE,
    CLASS_ONLY,
    DEFAULT_PII_FLOOR,
    MIXED_MEMBER_CLASS,
    UNLISTED_NUMBER,
    UNLISTED_TEXT,
    Policy,
    Resolved,
    canonical_policy,
    class_for,
    member_class,
    reads_as_data,
)

__all__ = [
    "BUSINESS_CODE",
    "CLASS_ONLY",
    "DEFAULT_PII_FLOOR",
    "MIXED_MEMBER_CLASS",
    "UNLISTED_NUMBER",
    "UNLISTED_TEXT",
    "Policy",
    "Resolved",
    "canonical_policy",
    "class_for",
    "member_class",
    "reads_as_data",
]
