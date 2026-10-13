"""Named API tokens (docs/mvp/AUTH.md §5): ``dst_<16 hex id>_<64 hex secret>``, id ``T-<16 hex>``.

A row stores ``sha256(token)``, found by the id the token carries and compared in constant time. The
token itself is shown once, when it is created.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from deltasleuth.auth.principal import TOKEN_ROLES, Principal, printable
from deltasleuth.store import repo

_TOKEN = re.compile(r"dst_([0-9a-f]{16})_[0-9a-f]{64}")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
DEFAULT_DAYS = 90


def new_token() -> tuple[str, str]:
    """A fresh ``(token_id, token)``."""
    hex_id = secrets.token_hex(8)
    return "T-" + hex_id, f"dst_{hex_id}_{secrets.token_hex(32)}"


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def token_id(token: str) -> str | None:
    """The ``T-`` id a well-formed token carries; None for anything else."""
    match = _TOKEN.fullmatch(token)
    return "T-" + match[1] if match else None


async def create(
    conn: repo.Conn,
    who: Principal,
    *,
    name: str,
    owner: str,
    role: str,
    max_days: int,
    days: int | None = None,
    owner_user_id: str | None = None,
) -> tuple[repo.Row, str]:
    """Store a new token created by ``who``; its row (without the hash) and the token. Bad input
    raises ValueError with a message for the caller to show."""
    if not NAME.fullmatch(name):
        raise ValueError(f"name must match ^{NAME.pattern}$")
    if not (1 <= len(owner) <= 200 and printable(owner)):
        raise ValueError("owner must be 1 to 200 characters without control characters")
    if role not in TOKEN_ROLES:
        raise ValueError(f"role must be one of {', '.join(sorted(TOKEN_ROLES))}")
    days = min(DEFAULT_DAYS, max_days) if days is None else days
    if not 1 <= days <= max_days:
        raise ValueError(f"days must be from 1 to {max_days}")
    if owner_user_id is not None:
        user = await repo.get_user(conn, owner_user_id)
        if user is None or user["disabled_at"] is not None:
            raise ValueError(f"owner_user_id {owner_user_id} is not an enabled user")
    new_id, token = new_token()
    row = await repo.create_token(
        conn,
        token_id=new_id,
        name=name,
        owner=owner,
        owner_user_id=owner_user_id,
        role=role,
        secret_hash=token_hash(token),
        created_by=who.actor,
        days=days,
    )
    return row, token


async def verify(conn: repo.Conn, token: str) -> repo.Row | None:
    """The live token's row, its use recorded (at most once a minute); None for anything else."""
    found = token_id(token)
    row = None if found is None else await repo.token_by_id(conn, found)
    if row is None or row["state"] != "active":
        return None
    if not hmac.compare_digest(bytes(row["secret_hash"]), token_hash(token)):
        return None
    await repo.touch_token(conn, row["token_id"])
    return row
