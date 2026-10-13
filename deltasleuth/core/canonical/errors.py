"""Engine errors. A ``CanonicalError`` is about one observation's data (observe makes the side
INCOMPLETE unparseable); an ``ExpressionError`` is a spec defect and stops the caller. Messages
never carry payload values."""

from __future__ import annotations

from collections.abc import Callable


class CanonicalError(ValueError):
    """One observation cannot be canonicalised."""

    def __init__(self, message: str, *, pattern: str | None = None) -> None:
        super().__init__(message)
        # Kept out of the message: member names can be data, and only the caller knows how
        # patterns are published.
        self.pattern = pattern

    def describe(self, render_pattern: Callable[[str], str]) -> str:
        return str(self) if self.pattern is None else f"{self} (at {render_pattern(self.pattern)})"


class MappingError(CanonicalError):
    """``select`` found no data, or the ``map`` failed or produced none."""


class ExpressionError(ValueError):
    """An expression or rule in the spec is invalid."""


class PathError(ExpressionError):
    """A JSONPath does not compile, or has no path-pattern form."""
