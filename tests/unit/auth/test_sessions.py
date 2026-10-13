"""The session and login cookies' names, and when they are plain."""

from __future__ import annotations

import pytest

from deltasleuth.auth import sessions
from deltasleuth.auth.oidc import LOGIN_COOKIE
from deltasleuth.config import AuthSettings


@pytest.mark.parametrize(
    ("public_url", "session", "login"),
    [
        ("http://localhost:8000", "ds_session_8000", "ds_login_8000"),
        ("http://127.0.0.1:18000", "ds_session_18000", "ds_login_18000"),
        ("http://[::1]:8000", "ds_session_8000", "ds_login_8000"),
        ("http://localhost", "ds_session_80", "ds_login_80"),  # the scheme's port
        ("https://ds.test", sessions.COOKIE, LOGIN_COOKIE),
        ("https://ds.test:8443", sessions.COOKIE, LOGIN_COOKIE),
        ("https://localhost:8443", sessions.COOKIE, LOGIN_COOKIE),  # loopback, yet https
        ("https://127.0.0.1", sessions.COOKIE, LOGIN_COOKIE),
        # the settings refuse http beyond loopback (test_config); built by hand, it is never plain either
        ("http://ds.bank.example", sessions.COOKIE, LOGIN_COOKIE),
        ("http://localhost.bank.example:8000", sessions.COOKIE, LOGIN_COOKIE),
    ],
)
def test_only_an_http_public_url_gives_plain_cookies_named_by_port(
    public_url: str, session: str, login: str
) -> None:
    auth = AuthSettings(public_url=public_url)
    assert (sessions.COOKIE, LOGIN_COOKIE) == ("__Host-ds_session", "__Host-ds_login")
    assert (sessions.named(sessions.COOKIE, auth), sessions.named(LOGIN_COOKIE, auth)) == (session, login)
    assert sessions.plain(auth) == (session != sessions.COOKIE)


def test_developer_mode_has_no_plain_cookie() -> None:
    assert not sessions.plain(None) and sessions.named(sessions.COOKIE, None) == sessions.COOKIE
