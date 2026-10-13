"""Browser sessions: the ``__Host-ds_session`` cookie (``ds_session_<port>`` on a
plain-http loopback ``public_url``), its CSRF token, and the role-mapping digest a user's
session is granted under."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from deltasleuth.config import LOOPBACK

if TYPE_CHECKING:
    from deltasleuth.config import AuthSettings, OidcSettings

COOKIE = "__Host-ds_session"  # under https; named(COOKIE, auth) is the name a server sets and reads


def plain(auth: AuthSettings | None) -> bool:
    """Whether ``auth.public_url`` is plain http, which the settings admit
    for a loopback host only. The cookies then carry neither ``__Host-`` nor ``Secure``, which only some
    browsers keep over http. From the settings alone, never from a request."""
    url = urlsplit(auth.public_url) if auth is not None else None
    return url is not None and url.scheme == "http" and url.hostname in LOOPBACK


def named(name: str, auth: AuthSettings | None) -> str:
    """The cookie a server sets and reads for ``name`` (COOKIE or ``oidc.LOGIN_COOKIE``): the name itself,
    or on plain http the name without its prefix, ``_`` and the port of ``public_url`` (80 when it names
    none): a browser keeps cookies by host, not by port, and two installations on one machine share the
    host."""
    if not plain(auth):
        return name
    assert auth is not None
    return f"{name.removeprefix('__Host-')}_{urlsplit(auth.public_url).port or 80}"


def new_secret() -> str:
    """A cookie value: 32 random bytes, base64url."""
    return secrets.token_urlsafe(32)


def secret_hash(cookie: str) -> bytes:
    """What a session row stores of its cookie."""
    return hashlib.sha256(cookie.encode()).digest()


def csrf_of(cookie: str) -> str:
    """The ``X-CSRF-Token`` a cookie session's unsafe requests carry."""
    digest = hashlib.sha256(b"ds-csrf/v1\x00" + cookie.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def roles_sha256(oidc: OidcSettings | None) -> str | None:
    """The hex sha256 of the role mapping (``group_claims`` and ``roles``) as canonical JSON; None
    outside oidc mode. A session granted under another mapping is no longer valid."""
    if oidc is None:
        return None
    mapping = {"group_claims": oidc.group_claims, "roles": oidc.roles}
    return hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
