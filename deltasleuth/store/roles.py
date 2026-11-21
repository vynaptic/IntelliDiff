"""The store's two database roles, made and kept apart for ``ds db setup``.

As a superuser, in one transaction under the migration lock (``prepare``): the schema's owner, the role of
``migrate_dsn_ref``, and the application's role, of ``database_dsn_ref``, are made where missing, each able
to sign in with the password its address carries; that password is set as a SCRAM verifier made here, so no
statement the server receives holds it. The owner may connect to the database and create in it, and what
another role owns in schema ``ds`` (an installation whose app ran as the superuser) is handed to the owner.
As the owner, after the migrations, the grants ``ds db grants`` prints (``grant``). Each step changes only
what differs, so a second run changes nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, LiteralString

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from deltasleuth.store import repo
from deltasleuth.store.db import lock_migrations

_SCRAM = re.compile(r"SCRAM-SHA-256\$(\d+):([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):[A-Za-z0-9+/=]+")
# A statement for each object of schema ds that a role other than %(owner)s owns, handing it to that role,
# each name as the catalog quotes it. A sequence a column owns, a table's row type and an array type move
# with what they belong to; a table's indexes and triggers have no owner of their own.
_FOREIGN: LiteralString = """
    SELECT format('ALTER %%s %%s OWNER TO %%I', CASE c.relkind WHEN 'v' THEN 'VIEW'
                      WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'S' THEN 'SEQUENCE'
                      ELSE 'TABLE' END, c.oid::regclass, %(owner)s::text)
    FROM pg_class c
    WHERE c.relnamespace = 'ds'::regnamespace AND pg_get_userbyid(c.relowner) <> %(owner)s
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
      AND NOT EXISTS (SELECT FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                        AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i'))
    UNION ALL
    SELECT format('ALTER %%s %%s OWNER TO %%I', CASE p.prokind WHEN 'p' THEN 'PROCEDURE'
                      WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END, p.oid::regprocedure, %(owner)s::text)
    FROM pg_proc p
    WHERE p.pronamespace = 'ds'::regnamespace AND pg_get_userbyid(p.proowner) <> %(owner)s
    UNION ALL
    SELECT format('ALTER %%s %%s OWNER TO %%I', CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
                  t.oid::regtype, %(owner)s::text)
    FROM pg_type t
    WHERE t.typnamespace = 'ds'::regnamespace AND pg_get_userbyid(t.typowner) <> %(owner)s
      AND (t.typtype IN ('d', 'e', 'r')
           OR t.typtype = 'c' AND (SELECT c.relkind FROM pg_class c WHERE c.oid = t.typrelid) = 'c')"""


class RolesError(ValueError):
    """A role or an address ``ds db setup`` cannot use, or a statement the database refused; the message
    names roles and settings, never a password."""


@dataclass(frozen=True)
class Login:
    """A role and the password its address carries (None: the address gives none)."""

    role: str
    password: str | None


def login(dsn: str, setting: str) -> Login:
    """The role and password of the address in the file ``setting`` names."""
    try:
        read = conninfo_to_dict(dsn)
    except (psycopg.ProgrammingError, UnicodeError):  # the driver's message quotes the address
        raise RolesError(f"the file {setting} names does not hold an address the driver can read") from None
    role, password = read.get("user"), read.get("password")
    if not isinstance(role, str) or not repo._ROLE.fullmatch(role):
        raise RolesError(
            f"the address in the file {setting} names no role, or one that is not 1 to 63 of a-z, 0-9 and _,"
            " starting with a letter or _"
        )
    return Login(role, str(password) if password else None)


def holds(verifier: str | None, password: str) -> bool:
    """Whether ``verifier``, a role's stored SCRAM-SHA-256 verifier, is one of ``password``. A password the
    server prepares (SASLprep) into other bytes is not recognised, and is set again: no harm."""
    found = _SCRAM.fullmatch(verifier or "")
    if found is None:
        return False
    salt, stored = base64.b64decode(found[2]), base64.b64decode(found[3])
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(found[1]))
    client = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    return hmac.compare_digest(hashlib.sha256(client).digest(), stored)


async def prepare(conn: psycopg.AsyncConnection[Any], owner: Login, app: Login) -> list[str]:
    """As the superuser ``conn`` holds, with no transaction open: the owner's and the application's roles
    made or mended, the owner's rights on the database, and schema ds handed to the owner, in one
    transaction. What changed, a phrase each; none when nothing did."""
    changed: list[str] = []
    async with _refused("the superuser's changes"), conn.transaction():
        await lock_migrations(conn)  # two runs at once: one makes the roles, the other finds them
        cur = await conn.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
        me, superuser = await cur.fetchone() or ("", False)
        if not superuser:
            raise RolesError(f"--superuser: role {me} is not a superuser of the database")
        if owner.role == app.role:
            raise RolesError(
                f"migrate_dsn_ref and database_dsn_ref name one role, {app.role}: the application needs a"
                " role of its own"
            )
        for held, setting in ((owner, "migrate_dsn_ref"), (app, "database_dsn_ref")):
            if held.role == me:
                raise RolesError(
                    f"{setting} names the superuser {me}: it needs a role of its own, which this command"
                    " makes"
                )
            changed += await _signs_in(conn, held, setting, owner=held is owner)
        changed += await _connects(conn, owner.role, sql.SQL("CREATE"), "create in")
        changed += await _connects(conn, app.role, sql.SQL("TEMPORARY"), "make temporary tables in")
        changed += await _handed(conn, owner.role)
    return changed


async def _connects(conn: psycopg.AsyncConnection[Any], role: str, right: sql.SQL, words: str) -> list[str]:
    """``role`` given CONNECT and ``right`` on the database where it lacks either: PUBLIC holds CONNECT and
    TEMPORARY unless a site took them away."""
    cur = await conn.execute(
        "SELECT current_database(), has_database_privilege(%(role)s, current_database(), 'CONNECT')"
        " AND has_database_privilege(%(role)s, current_database(), %(right)s)",
        {"role": role, "right": right.as_string()},
    )
    database, may = await cur.fetchone() or ("", True)
    if may:
        return []
    granted = sql.SQL("GRANT CONNECT, {} ON DATABASE {} TO {}")
    await conn.execute(granted.format(right, sql.Identifier(database), sql.Identifier(role)))
    return [f"role {role} may connect to and {words} database {database}"]


async def _signs_in(
    conn: psycopg.AsyncConnection[Any], held: Login, setting: str, *, owner: bool
) -> list[str]:
    """``held``'s role made, or given LOGIN and its address's password where it lacks them; the
    application's may be no superuser."""
    cur = await conn.execute(
        "SELECT rolsuper, rolcanlogin, rolpassword FROM pg_authid WHERE rolname = %s", (held.role,)
    )
    found = await cur.fetchone()
    name = sql.Identifier(held.role)
    verifier = None
    if held.password is not None:  # a verifier, never the password: a statement may reach a log
        made = conn.pgconn.encrypt_password(held.password.encode(), held.role.encode(), b"scram-sha-256")
        verifier = sql.Literal(made.decode())
    if found is None:
        made_role = sql.SQL("CREATE ROLE {} LOGIN").format(name)
        if verifier is not None:
            made_role = sql.SQL("{} PASSWORD {}").format(made_role, verifier)
        await conn.execute(made_role)
        return [f"role {held.role} made"]
    superuser, can_login, stored = found
    if superuser and not owner:
        raise RolesError(
            f"role {held.role} of {setting} is a superuser: name another role for the application, which this"
            " command makes"
        )
    changed = []
    if not can_login:
        await conn.execute(sql.SQL("ALTER ROLE {} LOGIN").format(name))
        changed.append(f"role {held.role} may sign in")
    if verifier is not None and held.password is not None and not holds(stored, held.password):
        await conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(name, verifier))
        changed.append(f"role {held.role} takes the password of {setting}")
    return changed


async def _handed(conn: psycopg.AsyncConnection[Any], owner: str) -> list[str]:
    """What another role owns in schema ds, the schema among it, handed to ``owner``."""
    cur = await conn.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'ds'")
    found = await cur.fetchone()
    if found is None:  # no schema yet: the owner's first migration makes it
        return []
    cur = await conn.execute(_FOREIGN, {"owner": owner})
    statements = [statement for (statement,) in await cur.fetchall()]
    if found[0] == owner and not statements:
        return []
    for statement in statements:
        await conn.execute(statement.encode())  # the catalog's own quoting of each name
    await conn.execute(sql.SQL("ALTER SCHEMA ds OWNER TO {}").format(sql.Identifier(owner)))
    return [f"schema ds handed from {found[0]} to {owner}"]


async def busy(conn: psycopg.AsyncConnection[Any], app: str) -> int:
    """The sessions the role ``app`` holds open on ``conn``'s database: a running app, or a command of it.
    Any role may read who is connected where."""
    cur = await conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND usename = %s"
        " AND pid <> pg_backend_pid()",
        (app,),
    )
    found = await cur.fetchone()
    return int(found[0]) if found else 0


async def grant(conn: psycopg.AsyncConnection[Any], app: str) -> None:
    """The grants ``ds db grants`` prints for ``app``, applied in one transaction as the schema's owner
    that ``conn`` holds."""
    try:
        async with _refused("the grants"), conn.transaction():
            await conn.execute(repo.grants(app).encode())  # no parameters: several statements as one script
    except RolesError as exc:
        if not isinstance(exc.__cause__, psycopg.errors.RaiseException):
            raise
        raise RolesError(exc.__cause__.diag.message_primary or str(exc)) from None  # the script's own words


@asynccontextmanager
async def _refused(what: str) -> AsyncIterator[None]:
    """A statement the database refuses, as RolesError naming ``what`` and the server's own words, which
    name roles and objects; a lost connection stays what it is."""
    try:
        yield
    except psycopg.OperationalError:
        raise
    except psycopg.Error as exc:
        why = exc.diag.message_primary or type(exc).__name__
        raise RolesError(f"{what} were refused: {why} (SQLSTATE {exc.sqlstate})") from exc
