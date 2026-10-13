"""Principals: who acts, under which roles.

Every route and agent tool checks one of six ordered roles; a principal holding one holds every lower
one. The two person-required roles are granted only to a person signed in through the IdP, whatever
roles another principal carries.
"""

from __future__ import annotations

import os
import pwd
import re
import secrets
import socket
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

Role = Literal["viewer", "operator", "spec_author", "rule_approver", "pii_reader", "admin"]
ROLES: tuple[Role, ...] = get_args(Role)  # lowest first
TOKEN_ROLES = frozenset({"viewer", "operator", "spec_author"})  # the only roles an API token can hold
PERSON_REQUIRED = frozenset({"rule_approver", "pii_reader"})
_UNSAFE_PART = re.compile(r"[^A-Za-z0-9._-]")


def expand(granted: Iterable[str]) -> frozenset[str]:
    """``granted`` with every lower role added."""
    top = max((ROLES.index(role) for role in granted), default=-1)  # type: ignore[arg-type]
    return frozenset(ROLES[: top + 1])


def printable(text: str) -> bool:
    """False when ``text`` holds a control character (Unicode category Cc: C0, DEL and C1)."""
    return not any(unicodedata.category(ch) == "Cc" for ch in text)


@dataclass(frozen=True)
class Principal:
    actor: str  # user:U-…, token:T-…, local:<operator>@<host>, system:…, dev:loopback
    name: str  # audited as actor_name
    roles: frozenset[str]
    request_id: str | None = None
    session_id: str | None = None  # a cookie session's W- id
    auth_time: datetime | None = None
    client_addr: str | None = None
    via: str | None = None  # chat:S-… when the agent acts for the asker; mcp:http or mcp:stdio for MCP's
    os_account: str | None = None  # the CLI's OS account

    @property
    def person(self) -> bool:
        return self.actor.startswith("user:")


WORKER = Principal("system:worker", "worker", frozenset())
SERVER = Principal("system:server", "server", frozenset())
MIGRATION = Principal("system:migration", "migration", frozenset())


def _account() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # a uid without a passwd entry, as a container may run
        return str(os.getuid())


def _part(text: str, limit: int) -> str:
    return _UNSAFE_PART.sub("_", text)[:limit] or "unknown"


def local(*, required: bool = False) -> Principal:
    """The CLI's principal: ``DS_OPERATOR`` as asserted, with every role. Unless ``required``
    (the settings have ``auth``), an unset or empty ``DS_OPERATOR`` means the OS account.

    One per invocation: it carries the invocation's ``C-`` request id.
    """
    account, operator = _account(), os.environ.get("DS_OPERATOR")
    if required and not operator:
        raise ValueError("set DS_OPERATOR to your user name")
    name = operator or account
    if not (1 <= len(name) <= 256 and printable(name)):
        raise ValueError("DS_OPERATOR must be 1 to 256 characters without control characters")
    actor = f"local:{_part(name, 64)}@{_part(socket.gethostname(), 253)}"
    request_id = "C-" + secrets.token_hex(8)
    return Principal(actor, name, frozenset(ROLES), request_id=request_id, os_account=account)


def dev() -> Principal:
    """Developer mode's principal: acts as admin, but never as a person."""
    return Principal("dev:loopback", f"{_account()}@{socket.gethostname()}", frozenset(ROLES))


class Throttle:
    """At most one audit row per key per ``window`` seconds, for denials a caller can repeat at will.

    ``hit`` counts a denial and returns the count to audit (the denials since the key's previous
    row, this one included), or None while the window is open. The oldest keys go past ``size``.
    """

    def __init__(
        self, window: float, size: int = 10_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.window, self.size, self.clock = window, size, clock
        self._keys: OrderedDict[Hashable, tuple[float, int]] = OrderedDict()  # key -> (row at, suppressed)

    def hit(self, key: Hashable) -> int | None:
        now = self.clock()
        last, suppressed = self._keys.pop(key, (None, 0))
        if last is not None and now - last < self.window:
            self._keys[key] = (last, suppressed + 1)
            return None
        self._keys[key] = (now, 0)
        while len(self._keys) > self.size:
            self._keys.popitem(last=False)
        return suppressed + 1

    def forget(self, key: Hashable) -> None:
        """Drop ``key``, whose row could not be written: the next hit is due one."""
        self._keys.pop(key, None)
