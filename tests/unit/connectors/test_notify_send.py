"""One attempt of a notification against local receivers only: every outcome
code, the address guard (private literals with the flag, never link-local, unspecified, multicast or
metadata, as a destination, a proxied host or the proxy's answer; a rebinding refused), no proxy from the
environment, a URL secret never shown, signing keys read at every send, SMTP's modes and one deadline, and
nothing run on the loop's default executor or through socket.getfqdn."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import copy
import logging
import os
import socket
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from deltasleuth.config import NotifySettings
from deltasleuth.connectors import notify
from deltasleuth.connectors.notify import Outcome, Sender
from deltasleuth.core.notify import refusal
from mocks.notify import Authority, Proxy, Receiver, SmtpSink
from tests.conftest import Loopback

CANARY = "c4n4ry-p4th-0f-the-url"
MESSAGE = (
    "Subject: [DeltaSleuth test] Info: a test\r\nMessage-ID: <N-1@I-0.deltasleuth.invalid>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n\r\nThis is a test.\r\n"
)


def _key(path: Path, raw: bytes | None = None) -> tuple[str, bytes]:
    raw = raw or os.urandom(32)
    path.write_text(f"whsec_{base64.b64encode(raw).decode()}\n")
    return f"file:{path}", raw


def _settings(channels: dict[str, Any], **options: Any) -> NotifySettings:
    return TypeAdapter(NotifySettings).validate_python({"label": "test", "channels": channels} | options)


async def _send(
    settings: NotifySettings, name: str, body: str = '{"format": "deltasleuth-notify/1"}'
) -> Outcome:
    sender = Sender(settings)
    try:
        return await sender.send(settings.channels[name], {"delivery_id": "N-" + "0" * 26, "body": body})
    finally:
        sender.close()


@pytest.fixture
async def receiver(tmp_path: Path, loopback: Loopback) -> AsyncIterator[tuple[Receiver, str, str]]:
    """A receiver with one signing key, its base URL and the key's reference."""
    ref, raw = _key(tmp_path / "hook.key")
    found = Receiver([raw])
    base = await found.start()
    yield found, base, ref
    await found.close()


@pytest.fixture
def authority(monkeypatch: pytest.MonkeyPatch) -> Authority:
    """A test CA the sender trusts, as a deployment's store would trust its receiver's."""
    found = Authority()
    monkeypatch.setattr(notify, "_tls", found.client)
    return found


@pytest.fixture
async def secure(
    tmp_path: Path, loopback: Loopback, authority: Authority
) -> AsyncIterator[tuple[Receiver, str, str]]:
    """An https receiver named hooks.test (127.0.0.1) under the test CA, its base URL and a key reference."""
    ref, raw = _key(tmp_path / "hook.key")
    found = Receiver([raw], ssl_context=authority.server())
    base = (await found.start()).replace("127.0.0.1", "hooks.test")
    loopback.names["hooks.test"] = ["127.0.0.1"]
    yield found, base, ref
    await found.close()


def _hook(base: str, ref: str) -> dict[str, Any]:
    return {
        "kind": "webhook",
        "url": f"{base}/webhook",
        "signing_key_refs": [ref],
        "allow_private_network": True,
    }


# -- HTTP outcomes -----------------------------------------------------------------------------------------


async def test_a_signed_webhook_is_sent_and_verified(
    receiver: tuple[Receiver, str, str], tmp_path: Path
) -> None:
    found, base, ref = receiver
    second, raw = _key(tmp_path / "second.key")
    found.keys.append(raw)
    outcome = await _send(
        _settings({"ops-hook": _hook(base, ref) | {"signing_key_refs": [ref, second]}}), "ops-hook"
    )
    assert (outcome.state, outcome.status, outcome.error) == ("sent", 200, None)
    (got,) = found.received
    assert got.verified and got.headers["webhook-id"] == "N-" + "0" * 26
    assert len(got.headers["webhook-signature"].split(" ")) == 2  # one per key
    assert got.headers["User-Agent"].startswith("deltasleuth-notify/")
    assert abs(int(got.headers["webhook-timestamp"]) - time.time()) < 5


async def test_the_signing_key_is_read_at_every_send(
    receiver: tuple[Receiver, str, str], tmp_path: Path
) -> None:
    found, base, ref = receiver
    settings = _settings({"ops-hook": _hook(base, ref)})
    assert (await _send(settings, "ops-hook")).state == "sent"
    (tmp_path / "hook.key").write_text("whsec_c2hvcnQ=\n")  # 5 bytes: malformed
    assert (await _send(settings, "ops-hook")).error == "secret_unreadable"
    (tmp_path / "hook.key").write_text(f"whsec_\u00e9{base64.b64encode(os.urandom(32)).decode()}\n")
    assert (await _send(settings, "ops-hook")).error == "secret_unreadable"  # not ASCII: never a crash
    (tmp_path / "hook.key").unlink()
    assert (await _send(settings, "ops-hook")).error == "secret_unreadable"
    _, raw = _key(tmp_path / "hook.key")  # rotated: a new key is used at once
    found.keys = [raw]
    assert (await _send(settings, "ops-hook")).state == "sent"


@pytest.mark.parametrize(
    ("status", "retry_after", "expected"),
    [
        (503, None, Outcome("retry", "http_5xx", 503)),
        (500, None, Outcome("retry", "http_5xx", 500)),
        (429, "30", Outcome("retry", "http_429", 429, 30.0)),
        (408, None, Outcome("retry", "http_408", 408)),
        (425, None, Outcome("retry", "http_425", 425)),
        (301, None, Outcome("dead", "http_3xx", 301)),
        (404, None, Outcome("dead", "http_4xx", 404)),
        (403, None, Outcome("dead", "http_4xx", 403)),  # Slack's invalid_token, no_service
        (999, None, Outcome("retry", "http_5xx", None)),  # not HTTP's: retried, its status never stored
        (600, None, Outcome("retry", "http_5xx", None)),
    ],
)
async def test_http_answers_are_closed_codes(
    receiver: tuple[Receiver, str, str], status: int, retry_after: str | None, expected: Outcome
) -> None:
    found, base, ref = receiver
    found.failures.append((status, retry_after))
    outcome = await _send(_settings({"ops-hook": _hook(base, ref)}), "ops-hook")
    assert (outcome.state, outcome.error, outcome.status, outcome.retry_after) == (
        expected.state,
        expected.error,
        expected.status,
        expected.retry_after,
    )


async def test_teams_and_slack(receiver: tuple[Receiver, str, str], tmp_path: Path) -> None:
    found, base, _ = receiver
    for path in ("teams", "slack"):
        (tmp_path / f"{path}.url").write_text(f"{base}/{path}?sig=secret\n")
    channels = {
        "owners-teams": {
            "kind": "teams",
            "url_ref": f"file:{tmp_path / 'teams.url'}",
            "host": "127.0.0.1",
            "allow_private_network": True,
        },
        "ops-slack": {
            "kind": "slack",
            "url_ref": f"file:{tmp_path / 'slack.url'}",
            "host": "127.0.0.1",
            "allow_private_network": True,
        },
    }
    settings = _settings(channels)
    assert (await _send(settings, "owners-teams")).status == 202
    assert (await _send(settings, "ops-slack")).status == 200
    assert [r.path for r in found.received] == ["/teams", "/slack"]
    assert "webhook-signature" not in found.received[0].headers


async def test_a_hanging_receiver_ends_timeout(receiver: tuple[Receiver, str, str]) -> None:
    found, base, ref = receiver
    found.hangs = 1
    started = time.monotonic()
    outcome = await _send(_settings({"ops-hook": _hook(base, ref)}, timeout_seconds=1), "ops-hook")
    assert outcome.error == "timeout" and time.monotonic() - started < 2


async def test_connect_dns_and_tls_errors(
    receiver: tuple[Receiver, str, str], loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, ref = receiver
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        closed = spare.getsockname()[1]  # nothing listens once it closes
    loopback.names["nowhere.test"] = []
    settings = _settings(
        {
            "gone": _hook(f"http://127.0.0.1:{closed}", ref),
            "unnamed": _hook("https://nowhere.test", ref),
        }
    )
    assert (await _send(settings, "gone")).error == "connect_error"
    assert (await _send(settings, "unnamed")).error == "dns_error"
    tls = Receiver(ssl_context=Authority().server())
    loopback.names["hooks.test"] = ["127.0.0.1"]
    port = int((await tls.start()).rsplit(":", 1)[1])
    try:  # the system's CA store does not know the test CA
        settings = _settings({"hook": _hook(f"https://hooks.test:{port}", ref)})
        assert (await _send(settings, "hook")).error == "tls_error"
        monkeypatch.setattr(notify, "_tls", Authority().client)  # another CA
        assert (await _send(settings, "hook")).error == "tls_error"
    finally:
        await tls.close()


async def test_a_body_over_its_cap_is_payload_too_large(
    receiver: tuple[Receiver, str, str], tmp_path: Path
) -> None:
    found, base, ref = receiver
    (tmp_path / "teams.url").write_text(f"{base}/teams\n")
    teams = {
        "kind": "teams",
        "url_ref": f"file:{tmp_path / 'teams.url'}",
        "host": "127.0.0.1",
        "allow_private_network": True,
    }
    settings = _settings({"ops-hook": _hook(base, ref), "owners-teams": teams})
    assert (await _send(settings, "ops-hook", "x" * (64 * 1024 + 1))).error == "payload_too_large"
    assert (await _send(settings, "owners-teams", "x" * (28 * 1024 + 1))).error == "payload_too_large"
    assert (await _send(settings, "owners-teams", "x" * (28 * 1024))).state == "sent"
    assert len(found.received) == 1


# -- the address guard --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "fd00:ec2::254",
        "fe80::1",
        "0.0.0.0",  # noqa: S104 - an address judged, never bound
        "::",
        "224.0.0.1",
        "ff02::1",
        "::ffff:169.254.169.254",
        # The metadata address as a NAT64 translator, an IPv4-translated and an IPv4-compatible form hold it.
        "64:ff9b::a9fe:a9fe",
        "::ffff:0:a9fe:a9fe",
        "::a9fe:a9fe",
    ],
)
def test_some_addresses_are_never_destinations(address: str) -> None:
    for allow_private in (False, True):
        assert refusal(address, allow_private) == (
            f"{address} is never a notification destination (link-local, unspecified, multicast or metadata)"
        )


@pytest.mark.parametrize(
    "address",
    [
        *("10.1.4.2", "127.0.0.1", "::1", "192.168.1.1", "::ffff:10.0.0.1", "fd12::1"),
        *("64:ff9b::a01:402", "64:ff9b::7f00:1", "::ffff:0:a01:402"),  # 10.1.4.2 and 127.0.0.1, translated
    ],
)
def test_a_private_address_needs_the_flag(address: str) -> None:
    assert refusal(address, False) == f"{address} is a private address: set allow_private_network to allow it"
    assert refusal(address, True) is None
    assert refusal("93.184.216.34", False) is None
    assert refusal("64:ff9b::5db8:d822", False) is None  # the same public address through a translator


async def test_a_private_literal_is_sent_with_the_flag_only(receiver: tuple[Receiver, str, str]) -> None:
    found, base, ref = receiver
    with pytest.raises(ValueError, match=r"127\.0\.0\.1 is a private address"):  # judged at load
        _settings({"ops-hook": _hook(base, ref) | {"allow_private_network": False}})
    settings = _settings({"ops-hook": _hook(base, ref)})
    unflagged = copy.copy(settings.channels["ops-hook"])
    object.__setattr__(unflagged, "allow_private_network", False)  # as if the load had not judged it
    sender = Sender(settings)
    try:
        refused = await sender.send(unflagged, {"delivery_id": "N-" + "0" * 26, "body": "{}"})
    finally:
        sender.close()
    assert (refused.state, refused.error) == ("dead", "unsafe_destination")
    assert (await _send(settings, "ops-hook")).state == "sent"
    assert len(found.received) == 1


async def test_names_are_vetted_at_every_connection(
    secure: tuple[Receiver, str, str], loopback: Loopback
) -> None:
    found, base, ref = secure
    answers = iter([["127.0.0.1"], ["169.254.169.254"], ["fd00:ec2::254"]])
    loopback.names["hooks.test"] = lambda: next(answers)  # a rebinding name
    settings = _settings({"hook": _hook(base, ref)})
    assert (await _send(settings, "hook")).state == "sent"
    assert (await _send(settings, "hook")).error == "unsafe_destination"
    assert (await _send(settings, "hook")).error == "unsafe_destination"
    loopback.names["private.test"] = ["127.0.0.1"]
    unflagged = _settings({"hook": _hook("https://private.test", ref) | {"allow_private_network": False}})
    assert (await _send(unflagged, "hook")).error == "unsafe_destination"
    assert len(found.received) == 1


async def test_a_proxied_channel_goes_through_the_declared_proxy_only(
    secure: tuple[Receiver, str, str], tmp_path: Path, loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    found, base, ref = secure
    proxy = Proxy()
    proxy_url = await proxy.start()
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(variable, proxy_url)  # never read
    try:
        assert (await _send(_settings({"ops-hook": _hook(base, ref)}), "ops-hook")).state == "sent"
        assert proxy.requests == []
        (tmp_path / "teams.url").write_text(f"{base}/teams\n")
        teams = {
            "kind": "teams",
            "url_ref": f"file:{tmp_path / 'teams.url'}",
            "host": "127.0.0.1",
            "proxy": True,
        }
        with pytest.raises(
            ValueError, match=r"127\.0\.0\.1 is a private address"
        ):  # a proxied host is judged
            _settings({"owners-teams": teams}, proxy=proxy_url)
        proxied = teams | {"host": "hooks.test"}
        loopback.names["proxy.test"] = ["127.0.0.1"]
        settings = _settings({"owners-teams": proxied}, proxy=proxy_url.replace("127.0.0.1", "proxy.test"))
        assert (await _send(settings, "owners-teams")).status == 202
        assert proxy.requests == [("CONNECT", base.removeprefix("https://"))]
        (tmp_path / "teams.url").write_text(
            f"{base.replace('https://hooks.test', 'http://localhost')}/teams\n"
        )
        plain = _settings({"owners-teams": proxied | {"host": "localhost"}}, proxy=proxy_url)
        assert (await _send(plain, "owners-teams")).error == "url_mismatch"  # http never through a proxy
        with pytest.raises(ValueError, match="proxy: true needs an https url"):
            _settings({"ops-hook": _hook(base.replace("https://hooks.test", "http://localhost"), ref)
                       | {"allow_private_network": False, "proxy": True}}, proxy=proxy_url)  # fmt: skip
        assert len(proxy.requests) == 1
        (tmp_path / "teams.url").write_text(f"{base}/teams\n")
        loopback.names["proxy.test"] = ["169.254.10.1"]  # the proxy's answer is vetted too
        assert (await _send(settings, "owners-teams")).error == "unsafe_destination"
        with pytest.raises(ValueError, match=r"169\.254\.169\.254 is never a notification destination"):
            _settings({"owners-teams": proxied}, proxy="http://169.254.169.254:8080")
        with pytest.raises(ValueError, match="fd00:ec2::254 is never a notification destination"):
            _settings({"owners-teams": proxied | {"host": "fd00:ec2::254"}}, proxy=proxy_url)
    finally:
        await proxy.close()
    assert len(found.received) == 2


async def test_a_url_secret_for_another_host_is_url_mismatch_never_shown(
    receiver: tuple[Receiver, str, str], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    found, base, _ = receiver
    teams = {
        "kind": "teams",
        "url_ref": f"file:{tmp_path / 'teams.url'}",
        "host": "127.0.0.1",
        "allow_private_network": True,
    }
    for url in (
        f"https://evil.example/{CANARY}",
        f"http://user:pw@127.0.0.1/{CANARY}",
        f"{base}/{CANARY}#x",
        f"ftp://127.0.0.1/{CANARY}",
        f"http://10.0.0.1/{CANARY}",
    ):
        (tmp_path / "teams.url").write_text(url + "\n")
        with caplog.at_level(logging.DEBUG):
            outcome = await _send(_settings({"owners-teams": teams}), "owners-teams")
        assert (outcome.state, outcome.error) == ("dead", "url_mismatch")
        assert CANARY not in repr(outcome) and CANARY not in caplog.text
    assert found.received == []


# -- SMTP ----------------------------------------------------------------------------------------------------


def _mail(port: int, tmp_path: Path, **options: Any) -> dict[str, Any]:
    (tmp_path / "smtp.pw").write_text("s3cret-pw\n")
    return {
        "kind": "email",
        "smtp_host": "smtp.test",
        "smtp_port": port,
        "username": "ds-alerts",
        "password_ref": f"file:{tmp_path / 'smtp.pw'}",
        "sender": "deltasleuth@bank.example",
        "recipients": ["owners@bank.example", "ops@bank.example"],
        "allow_private_network": True,
    } | options


async def test_smtp_starttls_auth_and_the_stored_message(
    authority: Authority, loopback: Loopback, tmp_path: Path
) -> None:
    sink = SmtpSink(authority)
    port = await sink.start()
    loopback.names["smtp.test"] = ["127.0.0.1"]
    try:
        outcome = await _send(_settings({"owners-mail": _mail(port, tmp_path)}), "owners-mail", MESSAGE)
    finally:
        await sink.close()
    assert (outcome.state, outcome.status, outcome.error) == ("sent", 250, None)
    (mail,) = sink.mails
    assert (mail.sender, mail.recipients, mail.login) == (
        "deltasleuth@bank.example",
        ["owners@bank.example", "ops@bank.example"],
        "ds-alerts",
    )
    assert b"From: deltasleuth@bank.example" in mail.data and b"Message-ID: <N-1@I-0" in mail.data


async def test_smtp_implicit_tls_and_none(authority: Authority, loopback: Loopback, tmp_path: Path) -> None:
    implicit, plain = SmtpSink(authority, implicit=True), SmtpSink(starttls=False)
    loopback.names["smtp.test"] = ["127.0.0.1"]
    try:
        settings = _settings(
            {
                "smtps": _mail(await implicit.start(), tmp_path, tls="implicit"),
                "relay": _mail(await plain.start(), tmp_path, tls="none", username=None, password_ref=None),
            }
        )
        assert (await _send(settings, "smtps", MESSAGE)).state == "sent"
        assert (await _send(settings, "relay", MESSAGE)).state == "sent"
    finally:
        await implicit.close()
        await plain.close()
    assert implicit.mails[0].login == "ds-alerts" and plain.mails[0].login is None
    loopback.names["smtp.test"] = ["93.184.216.34"]  # tls none refuses a global address, before connecting
    assert (await _send(settings, "relay", MESSAGE)).error == "unsafe_destination"


@pytest.mark.parametrize(
    ("sink", "expected"),
    [
        ({"starttls": False}, Outcome("dead", "smtp_no_starttls")),
        ({"replies": {"GREETING": "421 busy"}}, Outcome("retry", "smtp_temporary", 421)),
        ({"replies": {"MAIL": "451 try later"}}, Outcome("retry", "smtp_temporary", 451)),
        ({"replies": {"MAIL": "550 no"}}, Outcome("dead", "smtp_permanent", 550)),
        ({"replies": {"AUTH": "535 bad credentials"}}, Outcome("dead", "smtp_permanent", 535)),
        ({"replies": {"RCPT": ["550 unknown", "550 unknown"]}}, Outcome("dead", "smtp_permanent", 550)),
        ({"replies": {"RCPT": ["450 busy", "550 unknown"]}}, Outcome("retry", "smtp_temporary", 450)),
        ({"replies": {"RCPT": ["550 unknown"]}}, Outcome("sent", "recipients_refused", 250)),
        ({"replies": {"END": "452 full"}}, Outcome("retry", "smtp_temporary", 452)),
        # smtplib's own refusals are permanent, never smtp_no_starttls: STARTTLS without AUTH, SMTPS
        # without AUTH, no AUTH mechanism in common
        (
            {"replies": {"EHLO": "250-sink\r\n250-STARTTLS\r\n250 8BITMIME"}},
            Outcome("dead", "smtp_permanent"),
        ),
        (
            {"implicit": True, "replies": {"EHLO": "250-sink\r\n250 8BITMIME"}},
            Outcome("dead", "smtp_permanent"),
        ),
        (
            {"replies": {"EHLO": "250-sink\r\n250-AUTH GSSAPI\r\n250-STARTTLS\r\n250 8BITMIME"}},
            Outcome("dead", "smtp_permanent"),
        ),
    ],
)
async def test_smtp_answers_are_closed_codes(
    authority: Authority, loopback: Loopback, tmp_path: Path, sink: dict[str, Any], expected: Outcome
) -> None:
    server = SmtpSink(authority, **sink)
    loopback.names["smtp.test"] = ["127.0.0.1"]
    tls = {"tls": "implicit"} if server.implicit else {}
    try:
        outcome = await _send(
            _settings({"mail": _mail(await server.start(), tmp_path, **tls)}), "mail", MESSAGE
        )
    finally:
        await server.close()
    assert (outcome.state, outcome.error, outcome.status) == (expected.state, expected.error, expected.status)


async def test_smtp_secrets_tls_and_a_slow_relay(
    authority: Authority, loopback: Loopback, tmp_path: Path
) -> None:
    loopback.names["smtp.test"] = ["127.0.0.1"]
    hung, drip, tarpit, untrusted = (
        SmtpSink(authority, hang=True),
        SmtpSink(authority, drip=0.3),
        SmtpSink(authority, drip=1.5),  # "22" by the deadline: a cut reply, never a code
        SmtpSink(Authority()),
    )
    try:
        settings = _settings(
            {
                "hung": _mail(await hung.start(), tmp_path),
                "drip": _mail(await drip.start(), tmp_path),
                "tarpit": _mail(await tarpit.start(), tmp_path),
                "untrusted": _mail(await untrusted.start(), tmp_path),
            },
            timeout_seconds=2,
        )
        for name in ("hung", "drip", "tarpit"):
            started = time.monotonic()
            outcome = await _send(settings, name, MESSAGE)
            assert (outcome.state, outcome.error) == ("retry", "timeout")
            assert time.monotonic() - started < 3  # timeout_seconds + 1 s: the socket is shut down
        assert (await _send(settings, "untrusted", MESSAGE)).error == "tls_error"
        (tmp_path / "smtp.pw").unlink()
        assert (await _send(settings, "untrusted", MESSAGE)).error == "secret_unreadable"
    finally:
        for sink in (hung, drip, tarpit, untrusted):
            await sink.close()


def _relay(port: int) -> dict[str, Any]:
    """A relay at a literal address: its mail needs no lookup."""
    return {
        "kind": "email", "smtp_host": "127.0.0.1", "smtp_port": port, "tls": "none",
        "allow_private_network": True, "sender": "ds@bank.example", "recipients": ["o@bank.example"],
    }  # fmt: skip


async def test_hung_lookups_never_hold_the_mail_thread(loopback: Loopback, tmp_path: Path) -> None:
    ref, _ = _key(tmp_path / "hook.key")
    answered = threading.Event()
    loopback.names |= dict.fromkeys(("a.hung.test", "b.hung.test"), lambda: answered.wait(30) and [])
    sink = SmtpSink(starttls=False)
    settings = _settings(
        {"a": _hook("https://a.hung.test", ref), "b": _hook("https://b.hung.test", ref),
         "relay": _relay(await sink.start())},
        timeout_seconds=1,
    )  # fmt: skip
    sender = Sender(settings)
    try:
        for name in "ab":  # one after the other: each lookup still blocks when its send ends
            outcome = await sender.send(
                settings.channels[name], {"delivery_id": "N-" + "0" * 26, "body": "{}"}
            )
            assert outcome.error == "timeout"
        started = time.monotonic()
        mailed = await sender.send(
            settings.channels["relay"], {"delivery_id": "N-" + "0" * 26, "body": MESSAGE}
        )
        assert mailed.state == "sent" and time.monotonic() - started < 2  # timeout_seconds + 1 s
    finally:
        answered.set()
        sender.close()
        await sink.close()
    assert len(sink.mails) == 1


async def test_a_cancelled_mail_frees_its_thread_at_once(loopback: Loopback) -> None:
    drip = SmtpSink(starttls=False, drip=0.2, replies={"GREETING": "220 " + "x" * 76})  # 16 s of greeting
    settings = _settings({"relay": _relay(await drip.start())}, timeout_seconds=2)
    sender = Sender(settings)
    try:
        sending = asyncio.create_task(
            sender.send(settings.channels["relay"], {"delivery_id": "N-" + "0" * 26, "body": MESSAGE})
        )
        await asyncio.sleep(0.5)  # the thread reads the drip
        sending.cancel()  # Notifier.close: the loop may end before the deadline
        await asyncio.gather(sending, return_exceptions=True)
    finally:
        sender.close()
    threads, cancelled = list(sender._mailing._threads), time.monotonic()
    while any(thread.is_alive() for thread in threads):
        assert time.monotonic() - cancelled < 1, "the mail thread outlived its cancelled send"
        await asyncio.sleep(0.02)
    await drip.close()


async def test_a_mail_the_relay_accepted_is_sent_when_quit_is_answered_late(loopback: Loopback) -> None:
    """The deadline bounds when the mail job starts, not its QUIT: a relay that queued the mail (250 to the
    end of DATA) and answers QUIT after the deadline has the mail, so the send is ``sent``, never a
    ``timeout`` that would send it again."""
    queued: list[bytes] = []

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write(f"{line}\r\n".encode())
            await writer.drain()

        await say("220 relay")
        try:
            while line := await reader.readline():
                verb = line.decode().split(" ", 1)[0].strip().upper()
                if verb in ("EHLO", "HELO"):
                    await say("250 relay")
                elif verb == "DATA":
                    await say("354 go on")
                    while (data := await reader.readline()) != b".\r\n":
                        queued.append(data)
                    await say("250 queued")
                elif verb == "QUIT":
                    await asyncio.sleep(3)  # past timeout_seconds
                    await say("221 bye")
                    break
                else:
                    await say("250 ok")
        except OSError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(relay, "127.0.0.1", 0)
    settings = _settings({"relay": _relay(server.sockets[0].getsockname()[1])}, timeout_seconds=1)
    sender = Sender(settings)
    try:
        outcome = await sender.send(
            settings.channels["relay"], {"delivery_id": "N-" + "0" * 26, "body": MESSAGE}
        )
    finally:
        sender.close()
        server.close()
    assert (outcome.state, outcome.status) == ("sent", 250) and queued


class _Refusing(concurrent.futures.ThreadPoolExecutor):
    def submit(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the notifier used the loop's default executor")


async def test_the_default_executor_and_getfqdn_are_never_reached(
    secure: tuple[Receiver, str, str],
    authority: Authority,
    loopback: Loopback,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, base, ref = secure

    def getfqdn(*args: Any) -> str:
        raise AssertionError("socket.getfqdn was called")

    monkeypatch.setattr(socket, "getfqdn", getfqdn)
    asyncio.get_running_loop().set_default_executor(_Refusing())
    loopback.names["smtp.test"] = ["127.0.0.1"]
    sink = SmtpSink(authority)
    try:
        settings = _settings(
            {
                "hook": _hook(base, ref),
                "mail": _mail(await sink.start(), tmp_path),
            }
        )
        assert (await _send(settings, "hook")).state == "sent"
        assert (await _send(settings, "mail", MESSAGE)).state == "sent"
    finally:
        await sink.close()
