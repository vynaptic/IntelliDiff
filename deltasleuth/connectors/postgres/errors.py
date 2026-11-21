"""PostgreSQL's errors behind the engine seam: whatever psycopg raises on a session of a compared database
leaves this package as a ``TableError`` of the same class name, whose code is that name, in the seam's
class for what it means. No caller then tells a compared database's failure from the store's by the
driver's type, and each shows what it showed of psycopg's error: its class name, never its text, but for
the first line of a refused connect or a refused filter (``said``), which a plan's refusal shows.
"""

from __future__ import annotations

from functools import cache

import psycopg

from deltasleuth.connectors.tables.engine import (
    LimitPassed,
    StatementTimeout,
    TableError,
    TargetDenied,
    TargetTempSpace,
    TargetUnreachable,
)

# A column or a table the statement names is gone, or the role may not read it.
_GONE = (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege)
OUTAGE = "OperationalError"  # what an observation records of any outage: libpq names them all alike


class Unfit(TableError):
    """A key the column's type or the database's encoding cannot hold (``PostgreSQL.unfit``)."""


class Failed(TableError):
    """Any other code the server answered with."""


@cache
def _class(name: str, base: type[TableError]) -> type[TableError]:
    """The seam's error class named as psycopg's class ``name``, of the seam's class ``base``."""
    return type(name, (base,), {"__module__": __name__, "__doc__": f"psycopg's {name}, of {base.__name__}."})


def _base(exc: psycopg.Error, *, figure: bool) -> type[TableError]:
    """The seam's class of ``exc``. A figure's statement (a pre-check's) tells a full temporary space from
    an outage, and a missing or unreadable relation is a figure's error like any other."""
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return StatementTimeout
    if figure and isinstance(exc, psycopg.errors.ConfigurationLimitExceeded):  # temp_file_limit
        return TargetTempSpace
    if isinstance(exc, psycopg.errors.ProgramLimitExceeded):
        return LimitPassed
    if isinstance(exc, psycopg.OperationalError):
        return TargetUnreachable
    if isinstance(exc, _GONE) and not figure:
        return TargetDenied
    if isinstance(exc, psycopg.DataError | psycopg.errors.SyntaxError):  # a type's input: a key's
        return Unfit
    return Failed


def wrapped(exc: psycopg.Error, *, said: bool = False, figure: bool = False) -> TableError:
    """``exc`` as the seam's error: of its class name and code, ``said`` keeping its text's first line."""
    base = _base(exc, figure=figure)
    if base is TargetTempSpace:  # a pre-check records the server's code of it
        return TargetTempSpace(exc.sqlstate or type(exc).__name__)
    # A class is hashable, but typeshed's cache asks for Hashable, which mypy finds no class to be.
    error = _class(type(exc).__name__, base)(type(exc).__name__)  # type: ignore[arg-type]
    if said:
        error.said = (str(exc).splitlines() or [type(exc).__name__])[0][:300]
    if isinstance(error, TargetUnreachable) and not isinstance(error, StatementTimeout):
        error.detail = OUTAGE
    return error
