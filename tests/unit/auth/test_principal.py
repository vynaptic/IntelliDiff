"""Principals and roles (docs/mvp/AUTH.md §1, §4.2), the token format (§5) and the denial throttle (§6)."""

from __future__ import annotations

import re
import socket

import pytest

from deltasleuth.auth import sessions, tokens
from deltasleuth.auth.principal import (
    MIGRATION,
    PERSON_REQUIRED,
    ROLES,
    SERVER,
    TOKEN_ROLES,
    WORKER,
    Principal,
    Throttle,
    dev,
    expand,
    local,
    printable,
)


def test_roles_are_ordered_and_a_role_brings_every_lower_one() -> None:
    assert ROLES == ("viewer", "operator", "spec_author", "rule_approver", "pii_reader", "admin")
    assert expand(["spec_author"]) == {"viewer", "operator", "spec_author"}
    assert expand(["viewer", "admin"]) == set(ROLES)
    assert expand([]) == frozenset()
    assert (TOKEN_ROLES, PERSON_REQUIRED) == (
        {"viewer", "operator", "spec_author"},
        {"rule_approver", "pii_reader"},
    )


@pytest.mark.parametrize(
    ("text", "ok"),
    [("jdoe@bank.example", True), ("Zoë Łukasz 名前", True), ("a\x1b[31m", False), ("a\x00", False),
     ("a\x7f", False), ("a\x85", False), ("a\x9f", False), ("tab\t", False), ("line\n", False)],
)  # fmt: skip
def test_printable_refuses_every_control_character(text: str, ok: bool) -> None:
    assert printable(text) is ok


def test_fixed_principals_hold_no_role_and_only_users_are_people() -> None:
    assert [(p.actor, p.name, p.roles) for p in (WORKER, SERVER, MIGRATION)] == [
        ("system:worker", "worker", frozenset()),
        ("system:server", "server", frozenset()),
        ("system:migration", "migration", frozenset()),
    ]
    person = Principal("user:U-0123456789abcdef", "jdoe", frozenset({"viewer"}))
    assert person.person and not dev().person and not WORKER.person


def test_developer_mode_acts_as_admin_named_after_the_account() -> None:
    principal = dev()
    assert (principal.actor, principal.roles) == ("dev:loopback", set(ROLES))
    assert principal.name.endswith("@" + socket.gethostname())


def test_local_names_the_asserted_operator_and_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DS_OPERATOR", "jdoe")
    monkeypatch.setattr(socket, "gethostname", lambda: "deltasleuth-app")
    principal = local()
    assert (principal.actor, principal.name, principal.roles) == (
        "local:jdoe@deltasleuth-app",
        "jdoe",
        set(ROLES),
    )
    assert re.fullmatch(r"C-[0-9a-f]{16}", principal.request_id or "") and principal.os_account
    assert local().request_id != principal.request_id  # one per invocation


@pytest.mark.parametrize(
    ("operator", "host", "actor"),
    [
        ("J. Doe (ops)", "app.bank.example", "local:J._Doe__ops_@app.bank.example"),
        ("jdoe", "host:with/odd chars", "local:jdoe@host_with_odd_chars"),
        ("o" * 70, "h" * 300, f"local:{'o' * 64}@{'h' * 253}"),
        ("jdoe", "", "local:jdoe@unknown"),
    ],
)
def test_local_reduces_each_part_to_what_the_actor_check_allows(
    monkeypatch: pytest.MonkeyPatch, operator: str, host: str, actor: str
) -> None:
    monkeypatch.setenv("DS_OPERATOR", operator)
    monkeypatch.setattr(socket, "gethostname", lambda: host)
    principal = local()
    assert (principal.actor, principal.name) == (actor, operator)


def test_local_defaults_to_the_os_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    principal = local()
    assert principal.name == principal.os_account and principal.actor.startswith("local:")


@pytest.mark.parametrize("operator", [None, ""])
def test_local_with_the_auth_settings_needs_ds_operator(
    monkeypatch: pytest.MonkeyPatch, operator: str | None
) -> None:
    if operator is None:
        monkeypatch.delenv("DS_OPERATOR", raising=False)
    else:
        monkeypatch.setenv("DS_OPERATOR", operator)
    with pytest.raises(ValueError, match=r"^set DS_OPERATOR to your user name \(docs/mvp/AUTH\.md §4\.2\)$"):
        local(required=True)
    monkeypatch.setenv("DS_OPERATOR", "jdoe")
    assert local(required=True).name == "jdoe"


@pytest.mark.parametrize("operator", ["a" * 257, "jdoe\x1b[2J"])
def test_local_refuses_an_unprintable_or_long_operator(
    monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    monkeypatch.setenv("DS_OPERATOR", operator)
    with pytest.raises(
        ValueError, match=r"^DS_OPERATOR must be 1 to 256 characters without control characters$"
    ):
        local()


def test_the_token_format() -> None:
    token_id, token = tokens.new_token()
    assert re.fullmatch(r"T-[0-9a-f]{16}", token_id) and re.fullmatch(r"dst_[0-9a-f]{16}_[0-9a-f]{64}", token)
    assert tokens.token_id(token) == token_id and len(tokens.token_hash(token)) == 32
    for bad in (token.upper(), token + "0", token[:-1], "Bearer " + token, token.replace("dst_", "dsx_")):
        assert tokens.token_id(bad) is None


def test_a_session_cookie_and_its_csrf_token() -> None:
    cookie = sessions.new_secret()
    assert len(cookie) == 43 and re.fullmatch(r"[A-Za-z0-9_-]+", cookie)
    assert sessions.csrf_of(cookie) == sessions.csrf_of(cookie) != sessions.csrf_of(sessions.new_secret())
    assert "=" not in sessions.csrf_of(cookie) and len(sessions.secret_hash(cookie)) == 32


def test_the_throttle_writes_one_row_per_key_and_window_with_the_count_since_the_last() -> None:
    now = [0.0]
    throttle = Throttle(60, size=2, clock=lambda: now[0])
    assert [throttle.hit("a") for _ in range(100)] == [1] + [None] * 99
    assert throttle.hit("b") == 1
    now[0] = 61
    assert (throttle.hit("a"), throttle.hit("a")) == (100, None)
    throttle.hit("c")  # past its size: the oldest key goes
    assert throttle.hit("b") == 1
    throttle.forget("b")  # its row could not be written: the next hit is due one
    throttle.forget("never-hit")
    assert (throttle.hit("b"), throttle.hit("b")) == (1, None)
