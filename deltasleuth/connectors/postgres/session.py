"""Read-only sessions on a database target: one DSN, refused where libpq's errors could quote it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict

from deltasleuth.core.secrets import SecretError, read_secret
from deltasleuth.core.types import Target

# Both sides render a value as the same text: instants in UTC, ISO dates and intervals, shortest floats.
# They are the connection's own options, so they hold from its start: no statement of the session runs
# under what the role or the database set (a role's statement_timeout of 1 ms would fail its first one).
_SESSION = {
    "default_transaction_read_only": "on",
    "TimeZone": "UTC",
    "DateStyle": "ISO, YMD",
    "IntervalStyle": "iso_8601",
    "extra_float_digits": "1",
    "bytea_output": "hex",
}
# A key its column's type or the database's encoding cannot hold. The statements are composed of
# identifiers and placeholders, so a syntax error can only be a type's input (ltree's, for one).
UNFIT = (psycopg.DataError, psycopg.errors.SyntaxError, UnicodeEncodeError)


async def connect(target: Target, *, timeout_s: float = 0) -> psycopg.AsyncConnection:
    """A read-only session on the target; ``timeout_s`` bounds each statement (0: no bound). The session's
    settings are given at connection, after any ``options`` its DSN holds, so they win over those too."""
    dsn = _dsn(target)
    settings = _SESSION | {"statement_timeout": str(round(timeout_s * 1000))}
    ours = " ".join(f"-c {name}={_escaped(value)}" for name, value in settings.items())
    given = conninfo_to_dict(dsn).get("options")
    return await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        connect_timeout=10,
        application_name="deltasleuth",
        options=f"{given} {ours}" if given else ours,
    )


def _escaped(value: str) -> str:
    """A value as libpq's ``options`` takes it: a space or a backslash escaped by a backslash."""
    return value.replace("\\", "\\\\").replace(" ", "\\ ")


def _dsn(target: Target) -> str:
    """The target's DSN, refused (naming the target only) where libpq's messages could quote it."""
    dsn = read_secret(target.dsn_ref or "")
    try:
        conninfo_to_dict(dsn)
        valid = True
    except psycopg.ProgrammingError:  # its message quotes the DSN, password included
        valid = False
    # In a URI, an unencoded @ or / in the password turns its tail into a host, port or database that
    # connection errors name: a raw @ may appear only once, before the first /.
    authority, _, path = dsn.partition("://")[2].partition("/")
    if not valid or authority.count("@") > 1 or "@" in path:
        raise SecretError(f"the DSN of target {target.target_id} is not a valid connection string")
    return dsn


@asynccontextmanager
async def snapshot(target: Target, *, timeout_s: float = 0) -> AsyncIterator[psycopg.AsyncConnection]:
    """A session inside one REPEATABLE READ READ ONLY transaction: every statement sees the same data;
    ``timeout_s`` bounds each statement (0: no bound)."""
    async with await connect(target, timeout_s=timeout_s) as conn:
        await conn.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ)
        await conn.set_read_only(True)
        async with conn.transaction():
            yield conn
