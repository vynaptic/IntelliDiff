"""Typed errors of the spec loader and the target registry.

An issue points (RFC 6901) into the document it came from and never quotes document values,
which may be credentials or personal data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

UNSUPPORTED = "unsupported"  # a feature the product does not run yet: always an error
IGNORED = "ignored"  # accepted but not applied; never turns a difference into a MATCH


@dataclass(frozen=True)
class SpecIssue:
    pointer: str  # "" = the whole document
    message: str
    code: str  # "schema.<keyword>" for JSON Schema failures, a loader-rule code otherwise
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        return f"{self.pointer or '/'} [{self.code}] {self.message}"


class SpecError(Exception):
    """A document failed to load, validate or compile; ``issues`` lists every blocking problem."""

    def __init__(self, issues: Iterable[SpecIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n  ".join([f"{len(self.issues)} issue(s):", *map(str, self.issues)]))


class TargetsError(SpecError):
    """The target registry file is invalid (pointers address the targets document)."""


class UnknownTargetError(LookupError):
    """A target id is not in the registry."""


def pointer(parts: Sequence[str | int]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def sort_issues(issues: Iterable[SpecIssue]) -> list[SpecIssue]:
    """Deduplicated, ordered by location (array indexes numerically), then code and message."""

    def key(issue: SpecIssue) -> tuple[list[tuple[int, int, str]], str, str]:
        segments = issue.pointer.split("/")[1:]
        return ([(0, int(s), "") if s.isdigit() else (1, 0, s) for s in segments], issue.code, issue.message)

    return sorted(set(issues), key=key)
