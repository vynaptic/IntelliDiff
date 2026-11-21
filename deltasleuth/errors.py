"""Refusals: what a request is refused with, in words the package wrote for whoever asked.

A ``Refused`` carries the HTTP status the API answers it with, and a message that names ids, names, counts,
positions and settings, never a key or a value, so every entry point says it as it is: the API with that
status, the CLI as one ``error:`` line and exit 2, and the MCP tools as the call's error. Any other
exception is a fault, a ValueError or a LookupError of Python's or a library's included: the API answers
500 and the CLI exits 3, naming its type and where it was raised, never its message.

``Invalid`` and ``NotFound`` are also a ValueError and a LookupError, so that code catching bad input or an
unknown id catches them as it did.
"""

from __future__ import annotations

_PACKAGE = __name__.partition(".")[0]


def ours(exc: BaseException) -> bool:
    """Whether ``exc``'s message is the package's own words: a ``Refused``, or a ValueError or LookupError of
    a class the package defines (bad settings, an unreadable secret or key file, a sheet or a document that
    cannot be read). A ValueError or LookupError of Python's or a library's (``int()``'s, ``Decimal``'s,
    ``fromisoformat``'s) is a fault, as is a KeyError or an IndexError: their messages may quote a value."""
    if isinstance(exc, Refused):
        return True
    if isinstance(exc, KeyError | IndexError) or not isinstance(exc, ValueError | LookupError):
        return False
    return type(exc).__module__.partition(".")[0] == _PACKAGE


def told(exc: BaseException) -> str:
    """What may be said of ``exc`` where anyone reads it (a log line, an audit row, an answer): its message
    when that is the package's own words (``ours``), else its type alone."""
    return str(exc) if ours(exc) else type(exc).__name__


class Refused(Exception):
    """A request refused: ``status`` is the HTTP answer, the message says why."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Invalid(Refused, ValueError):
    """Input that cannot be taken as it is (422)."""

    def __init__(self, message: str) -> None:
        super().__init__(422, message)


class NotFound(Refused, LookupError):
    """An id or a name that is not stored (404)."""

    def __init__(self, message: str) -> None:
        super().__init__(404, message)


class Conflict(Refused):
    """A request that what is stored refuses now (409)."""

    def __init__(self, message: str) -> None:
        super().__init__(409, message)


class Gone(Refused):
    """What was asked for was deleted (410)."""

    def __init__(self, message: str) -> None:
        super().__init__(410, message)
