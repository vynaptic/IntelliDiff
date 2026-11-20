"""The queries of users, their sessions and API tokens."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import LiteralString

from deltasleuth.store.repo.base import Conn, Row, _changed, _one, _row, _rows

_TOKEN_STATE: LiteralString = """CASE WHEN revoked_at IS NOT NULL THEN 'revoked'
    WHEN expires_at <= now() THEN 'expired' ELSE 'active' END AS state"""  # noqa: S105 - SQL
_TOKEN_COLUMNS: LiteralString = """token_id, name, owner, owner_user_id, role, created_by, created_at,
    expires_at, last_used_at, revoked_at, revoked_by"""  # noqa: S105 - SQL
_SESSION_COLUMNS: LiteralString = """session_id, user_id, token_id, actor, name, roles, client_addr,
    auth_time, acr, amr, created_at, last_seen_at, expires_at, revoked_at, revoked_by"""


async def upsert_user(
    conn: Conn,
    issuer: str,
    subject: str,
    username: str,
    display_name: str | None,
    roles: Sequence[str] | None,
    *,
    signed_in: bool,
) -> Row:
    """The user of ``(issuer, subject)``, created or brought up to date; it carries ``previous_roles``
    (None for a new user). A row is written only when a name or the roles differ, or ``signed_in``
    (a successful sign-in sets ``last_login_at``; a disabled user's never does). ``roles`` None keeps
    the stored roles, ``{}`` for a new user: a groups overage tells nothing about them."""
    params = {
        "user_id": "U-" + secrets.token_hex(8),
        "issuer": issuer,
        "subject": subject,
        "username": username,
        "display_name": display_name,
        "roles": None if roles is None else sorted(roles),
        "signed_in": signed_in,
    }
    previous = await _row(
        conn, "SELECT * FROM ds.users WHERE issuer = %(issuer)s AND subject = %(subject)s", params
    )
    row = await _row(
        conn,
        """INSERT INTO ds.users AS u (user_id, issuer, subject, username, display_name, roles, last_login_at)
           VALUES (%(user_id)s, %(issuer)s, %(subject)s, %(username)s, %(display_name)s,
                   coalesce(%(roles)s::text[], '{}'), CASE WHEN %(signed_in)s THEN now() END)
           ON CONFLICT (issuer, subject) DO UPDATE SET
               username = EXCLUDED.username, display_name = EXCLUDED.display_name,
               roles = coalesce(%(roles)s::text[], u.roles),
               last_login_at = CASE WHEN %(signed_in)s AND u.disabled_at IS NULL THEN now()
                                    ELSE u.last_login_at END
           WHERE (%(signed_in)s AND u.disabled_at IS NULL) OR u.username <> EXCLUDED.username
              OR u.display_name IS DISTINCT FROM EXCLUDED.display_name
              OR u.roles <> coalesce(%(roles)s::text[], u.roles)
           RETURNING *""",
        params,
    )
    found = row or previous
    assert found is not None  # none returned: the conflict left ``previous`` as it was
    return found | {"previous_roles": None if previous is None else previous["roles"]}


async def get_user(conn: Conn, user_id: str) -> Row | None:
    return await _row(conn, "SELECT * FROM ds.users WHERE user_id = %s", (user_id,))


async def list_users(conn: Conn) -> list[Row]:
    """Every user with the roles of the last sign-in, by username; ``disabled_by_name`` beside
    ``disabled_by``."""
    return await _rows(
        conn,
        "SELECT *, ds.actor_name(disabled_by) AS disabled_by_name FROM ds.users ORDER BY username, user_id",
    )


async def set_user_disabled(conn: Conn, user_id: str, disabled_by: str | None) -> Row | None:
    """Disable the user (``disabled_by`` an actor) and revoke its sessions, or enable it (None); the
    row with ``sessions_revoked``. None when the user is unknown or already in that state."""
    row = await _row(
        conn,
        """UPDATE ds.users SET disabled_at = CASE WHEN %(by)s::text IS NULL THEN NULL ELSE now() END,
               disabled_by = %(by)s
           WHERE user_id = %(user_id)s AND (disabled_at IS NULL) = (%(by)s::text IS NOT NULL)
           RETURNING *""",
        {"user_id": user_id, "by": disabled_by},
    )
    if row is None:
        return None
    revoked = 0 if disabled_by is None else await revoke_sessions(conn, disabled_by, user_id=user_id)
    return row | {"sessions_revoked": revoked}


async def user_tokens(conn: Conn, user_id: str) -> list[Row]:
    """Every token the user created or owns, newest first: token_id, name, role, state."""
    return await _rows(
        conn,
        f"""SELECT token_id, name, role, {_TOKEN_STATE} FROM ds.api_tokens
            WHERE created_by = 'user:' || %(user_id)s OR owner_user_id = %(user_id)s
            ORDER BY created_at DESC, token_id""",  # noqa: S608 - fixed SQL fragments only
        {"user_id": user_id},
    )


async def create_session(
    conn: Conn,
    *,
    secret_hash: bytes,
    actor: str,
    name: str,
    roles: Sequence[str],
    max_hours: int,
    user_id: str | None = None,
    token_id: str | None = None,
    roles_sha256: str | None = None,
    client_addr: str | None = None,
    auth_time: datetime | None = None,
    acr: str | None = None,
    amr: Sequence[str] | None = None,
) -> Row:
    """A session with a fresh ``W-`` id for a user or a token, ending ``max_hours`` from now."""
    return await _one(
        conn,
        f"""INSERT INTO ds.sessions (session_id, secret_hash, user_id, token_id, actor, name, roles,
                roles_sha256, client_addr, auth_time, acr, amr, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + %s * interval '1 hour')
            RETURNING {_SESSION_COLUMNS}""",  # noqa: S608 - fixed SQL fragments only
        (
            "W-" + secrets.token_hex(8), secret_hash, user_id, token_id, actor, name, sorted(roles),
            roles_sha256, client_addr, auth_time, acr, None if amr is None else list(amr), max_hours,
        ),
    )  # fmt: skip


async def session_by_secret(
    conn: Conn, secret_hash: bytes, roles_sha256: str | None, *, idle_minutes: int
) -> Row | None:
    """The valid session of a cookie: not revoked, before its end and used within ``idle_minutes``,
    its user enabled and granted under the running role mapping (``roles_sha256``, oidc mode), or its
    token live and the server in tokens mode (no mapping), so a token's browser session ends when the
    deployment moves to SSO."""
    return await _row(
        conn,
        f"""SELECT {_SESSION_COLUMNS} FROM ds.sessions s
            WHERE secret_hash = %(hash)s AND revoked_at IS NULL AND expires_at > now()
              AND last_seen_at > now() - %(idle)s * interval '1 minute'
              AND (user_id IS NULL OR (roles_sha256 = %(roles_sha256)s AND EXISTS (
                  SELECT 1 FROM ds.users u WHERE u.user_id = s.user_id AND u.disabled_at IS NULL)))
              AND (token_id IS NULL OR %(roles_sha256)s::text IS NULL AND EXISTS (
                  SELECT 1 FROM ds.api_tokens t
                  WHERE t.token_id = s.token_id AND t.revoked_at IS NULL AND t.expires_at > now()))""",  # noqa: S608
        {"hash": secret_hash, "roles_sha256": roles_sha256, "idle": idle_minutes},
    )


async def revoke_session_secret(conn: Conn, secret_hash: bytes) -> None:
    """Revoke the unrevoked session of a cookie that no longer resolves (idle, or granted under another
    mode or role mapping), by its own actor, so a later settings change cannot revive it."""
    await conn.execute(
        """UPDATE ds.sessions SET revoked_at = now(), revoked_by = actor
           WHERE secret_hash = %s AND revoked_at IS NULL""",
        (secret_hash,),
    )


async def get_session(conn: Conn, session_id: str) -> Row | None:
    return await _row(
        conn,
        f"SELECT {_SESSION_COLUMNS} FROM ds.sessions WHERE session_id = %s",  # noqa: S608
        (session_id,),
    )


async def touch_session(conn: Conn, session_id: str) -> None:
    """Record the session's use, at most once a minute."""
    await conn.execute(
        """UPDATE ds.sessions SET last_seen_at = now()
           WHERE session_id = %s AND last_seen_at < now() - interval '60 seconds'""",
        (session_id,),
    )


async def revoke_sessions(
    conn: Conn,
    revoked_by: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    everyone: bool = False,
) -> int:
    """Revoke one live session, a user's, or ``everyone``'s; return how many."""
    if sum((session_id is not None, user_id is not None, everyone)) != 1:
        raise ValueError("revoke a session, a user's sessions or every session")
    return await _changed(
        conn,
        """UPDATE ds.sessions SET revoked_at = now(), revoked_by = %(by)s
           WHERE revoked_at IS NULL AND expires_at > now()
             AND (%(session_id)s::text IS NULL OR session_id = %(session_id)s)
             AND (%(user_id)s::text IS NULL OR user_id = %(user_id)s)""",
        {"by": revoked_by, "session_id": session_id, "user_id": user_id},
    )  # fmt: skip


async def list_sessions(conn: Conn) -> list[Row]:
    """The live sessions (not revoked, before their end), newest first."""
    return await _rows(
        conn,
        f"""SELECT {_SESSION_COLUMNS} FROM ds.sessions
            WHERE revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC, session_id""",  # noqa: S608
    )


async def delete_ended_sessions(conn: Conn) -> int:
    """Delete the sessions that ended (revoked or past their end) over 30 days ago; return how many."""
    return await _changed(
        conn,
        "DELETE FROM ds.sessions WHERE coalesce(revoked_at, expires_at) < now() - interval '30 days'",
        (),
    )


async def create_token(
    conn: Conn,
    *,
    token_id: str,
    name: str,
    owner: str,
    owner_user_id: str | None,
    role: str,
    secret_hash: bytes,
    created_by: str,
    days: int,
) -> Row:
    """Store a token (``auth.tokens.create`` makes one); its row without the hash."""
    return await _one(
        conn,
        f"""INSERT INTO ds.api_tokens (token_id, name, owner, owner_user_id, role, secret_hash, created_by,
                expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s * interval '1 day')
            RETURNING {_TOKEN_COLUMNS}, {_TOKEN_STATE}""",  # noqa: S608 - fixed SQL fragments only
        (token_id, name, owner, owner_user_id, role, secret_hash, created_by, days),
    )


async def token_by_id(conn: Conn, token_id: str) -> Row | None:
    """The token with its ``secret_hash`` (for ``auth.tokens.verify``) and ``state``."""
    return await _row(
        conn,
        f"SELECT {_TOKEN_COLUMNS}, secret_hash, {_TOKEN_STATE} FROM ds.api_tokens WHERE token_id = %s",  # noqa: S608
        (token_id,),
    )


async def touch_token(conn: Conn, token_id: str) -> None:
    """Record the token's use, at most once a minute."""
    await conn.execute(
        """UPDATE ds.api_tokens SET last_used_at = now()
           WHERE token_id = %s AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')""",
        (token_id,),
    )


async def list_tokens(conn: Conn) -> list[Row]:
    """Every token, newest first, with its ``state`` and the names of ``created_by`` and
    ``owner_user_id``; never the hash."""
    return await _rows(
        conn,
        f"""SELECT {_TOKEN_COLUMNS}, {_TOKEN_STATE}, ds.actor_name(created_by) AS created_by_name,
                ds.actor_name('user:' || owner_user_id) AS owner_user_name
            FROM ds.api_tokens ORDER BY created_at DESC, token_id""",  # noqa: S608
    )


async def revoke_token(conn: Conn, token_id: str, revoked_by: str) -> Row | None:
    """Revoke the token (its sessions end with it); its row, or None when it is unknown or already
    revoked."""
    return await _row(
        conn,
        f"""UPDATE ds.api_tokens SET revoked_at = now(), revoked_by = %s
            WHERE token_id = %s AND revoked_at IS NULL
            RETURNING {_TOKEN_COLUMNS}, 'revoked' AS state""",  # noqa: S608 - fixed SQL fragments only
        (revoked_by, token_id),
    )


async def revoke_user_tokens(conn: Conn, user_id: str, revoked_by: str) -> list[str]:
    """Revoke every live token the user created or owns; their ids."""
    rows = await _rows(
        conn,
        """UPDATE ds.api_tokens SET revoked_at = now(), revoked_by = %(by)s
           WHERE revoked_at IS NULL AND expires_at > now()
             AND (created_by = 'user:' || %(user_id)s OR owner_user_id = %(user_id)s)
           RETURNING token_id""",
        {"by": revoked_by, "user_id": user_id},
    )
    return sorted(row["token_id"] for row in rows)
