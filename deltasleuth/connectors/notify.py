"""One attempt of a notification: a signed HTTPS webhook, Slack, Teams or SMTP
mail, on threads of the notifier's own, never the loop's default executor, which key judging, sample
judgements, purge and target DNS share.

The sender owns two threads, each its own executor: one resolves names, one sends mail, so a hung lookup
never holds the mail thread. Every address a send reaches is vetted first (``_allowed``): link-local,
unspecified, multicast and metadata addresses are always refused, any other non-global one only with
``allow_private_network``. Names are vetted at each connection by ``_Resolver`` (so a rebinding is
refused), IP literals before the call (aiohttp never resolves them). A channel with ``proxy: true`` sends
through ``notify.proxy``, https only, resolved as any internal host may be: the proxy resolves the
destination, checked only as its declared host. Nothing is taken from the environment.
``timeout_seconds`` bounds the whole attempt, resolving and the wait for the mail thread included: a slow
relay's socket is shut down at the deadline, or at once when the send is cancelled, so the mail thread is
free by then. Secrets (a URL in ``url_ref``, the signing keys, the SMTP password) are read at every send
and never logged; a send answers closed codes only.
``check`` and ``unreadable`` read and vet the same way without sending, for ``ds notify check`` and the
warnings ``serve``, ``worker`` and ``run`` print at start.
"""

from __future__ import annotations

import asyncio
import base64
import email
import hashlib
import hmac
import ipaddress
import smtplib
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.policy import SMTP
from functools import partial
from importlib.metadata import version
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from deltasleuth.config import LOOPBACK
from deltasleuth.connectors.retry import parse_retry_after
from deltasleuth.core.notify import refusal
from deltasleuth.core.secrets import SecretError, read_secret

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from deltasleuth.config import ChannelSettings, NotifySettings

MAX_BODY = 64 * 1024
MAX_TEAMS = 28 * 1024  # a Teams message
READ_BYTES = 4096  # of an answer, discarded
RETRIED = frozenset({"http_5xx", "http_429", "http_408", "http_425", "timeout", "connect_error", "dns_error",
                     "tls_error", "smtp_temporary", "secret_unreadable"})  # fmt: skip
LOCAL_NAME = "deltasleuth.invalid"  # the EHLO name: smtplib never calls socket.getfqdn()


@dataclass(frozen=True)
class Outcome:
    """An attempt's result: ``sent``, ``retry`` or ``dead``, a closed ``error`` code, the HTTP or SMTP
    ``status``, a ``Retry-After`` in seconds and how long it took."""

    state: str
    error: str | None = None
    status: int | None = None
    retry_after: float | None = None
    latency_ms: int = 0


class _Unsafe(Exception):  # not an OSError: aiohttp passes it on as it is
    """An address the channel may not reach."""


class _NoStartTLS(Exception):
    """The relay does not offer STARTTLS."""


def _allowed(address: str, allow_private: bool) -> bool:
    return refusal(address, allow_private) is None


def _literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


def _vet(host: str, allow_private: bool) -> None:
    """_Unsafe when ``host`` is an IP literal the channel may not reach."""
    if (address := _literal(host)) is not None and not _allowed(address, allow_private):
        raise _Unsafe


def signing_key(ref: str) -> bytes:
    """A webhook's signing key: ``whsec_`` and base64 of 24 to 64 bytes, read now; SecretError else."""
    text = read_secret(ref)
    try:
        key = base64.b64decode(text.removeprefix("whsec_"), validate=True)
    except ValueError:  # binascii.Error, or a character that is not ASCII
        key = b""
    if not text.startswith("whsec_") or not 24 <= len(key) <= 64:
        raise SecretError(
            f"the signing key in {ref.removeprefix('file:')} is not whsec_ and base64 of 24 to 64 bytes"
        )
    return key


def signature(keys: list[bytes], delivery_id: str, timestamp: int, body: bytes) -> str:
    """The Standard Webhooks ``webhook-signature``: one ``v1,<base64 HMAC-SHA256>`` per key."""
    signed = f"{delivery_id}.{timestamp}.".encode() + body
    return " ".join(
        "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode() for key in keys
    )


def _tls() -> ssl.SSLContext:
    """The TLS context of every send: the system's CA store, hosts verified."""
    return ssl.create_default_context()


def secure(url: str, host: str | None, proxied: bool = False) -> Any:
    """``url`` split when it is https (http to loopback, never through a proxy) without userinfo or
    fragment, to ``host`` when given (``url_ref``'s URL); None else."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return None
    loopback = parts.scheme == "http" and parts.hostname in LOOPBACK and not proxied
    scheme = parts.scheme == "https" or loopback
    if not scheme or parts.username is not None or parts.fragment or url.endswith("#"):
        return None
    return parts if host is None or parts.hostname == host.strip("[]") else None


def _http_outcome(status: int, retry_after: float | None) -> Outcome:
    if not 100 <= status <= 599:  # not HTTP's: kept out of last_status, retried as a server's fault
        return Outcome("retry", "http_5xx", None, retry_after)
    if 200 <= status <= 299:
        return Outcome("sent", status=status)
    if status in (408, 425, 429) or status >= 500:
        error = f"http_{status}" if status < 500 else "http_5xx"
        return Outcome("retry", error, status, retry_after)
    return Outcome("dead", "http_3xx" if status < 400 else "http_4xx", status)


class _Resolver(AbstractResolver):
    """aiohttp's resolver for a send: the names it connects to, resolved on the notifier's resolve thread,
    every answer vetted."""

    def __init__(self, resolve: Callable[[str, int], Awaitable[list[str]]], allow_private: bool) -> None:
        self._resolve, self._allow_private = resolve, allow_private

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        addresses = await self._resolve(host, port)
        if not all(_allowed(address, self._allow_private) for address in addresses):
            raise _Unsafe
        return [
            {
                "hostname": host, "host": address, "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET, "proto": 0,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            }
            for address in addresses
        ]  # fmt: skip

    async def close(self) -> None:
        return None


class _Guarded:
    """smtplib against one vetted address, under one deadline (``time.monotonic``): each command's socket
    timeout is the budget left; ``abort`` shuts the socket down, ending a slow drip, and ends the budget."""

    address: str
    deadline: float
    sock: socket.socket | None
    aborted = False

    def _budget(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0 or self.aborted:
            raise TimeoutError
        if self.sock is not None:
            self.sock.settimeout(left)
        return left

    def _connect(self, port: int) -> socket.socket:
        return socket.create_connection((self.address, port), timeout=self._budget())

    def send(self, s: Any) -> None:
        self._budget()
        super().send(s)  # type: ignore[misc]

    def getreply(self) -> tuple[int, bytes]:
        self._budget()
        return super().getreply()  # type: ignore[misc]

    def abort(self) -> None:
        self.aborted = True
        if (sock := self.sock) is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _GuardedSMTP(_Guarded, smtplib.SMTP):
    def __init__(self, host: str, address: str, deadline: float) -> None:
        self.address, self.deadline = address, deadline
        super().__init__(local_hostname=LOCAL_NAME)
        self._host = host  # what STARTTLS verifies

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return self._connect(port)


class _GuardedSMTPSSL(_Guarded, smtplib.SMTP_SSL):
    def __init__(self, host: str, address: str, deadline: float, context: ssl.SSLContext) -> None:
        self.address, self.deadline = address, deadline
        super().__init__(local_hostname=LOCAL_NAME, context=context)
        self._host = host

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return self.context.wrap_socket(self._connect(port), server_hostname=self._host)


def _begin(begun: Callable[[], object], *args: Any) -> dict[str, Any]:
    """``_deliver`` on the mail thread, first telling the loop it started."""
    begun()
    return _deliver(*args)


def _deliver(
    smtp: smtplib.SMTP, channel: ChannelSettings, password: str | None, message: Any, context: ssl.SSLContext
) -> dict[str, Any]:
    """One mail on the mail thread: connect, STARTTLS unless implicit or none, AUTH, the message; the
    recipients refused."""
    try:
        code, greeting = smtp.connect(channel.smtp_host, channel.smtp_port)  # type: ignore[arg-type]
        if code != 220:
            raise smtplib.SMTPConnectError(code, greeting)
        if channel.tls == "starttls":
            smtp.ehlo()
            if not smtp.has_extn("starttls"):
                raise _NoStartTLS
            smtp.starttls(context=context)
            smtp.ehlo()
        if channel.username is not None:
            smtp.login(channel.username, password or "")
        refused = smtp.send_message(message)
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            pass
        return refused
    finally:
        smtp.close()


def _smtp_outcome(exc: BaseException, late: bool) -> Outcome:
    if isinstance(exc, _Unsafe):
        return Outcome("dead", "unsafe_destination")
    if isinstance(exc, SecretError):
        return Outcome("retry", "secret_unreadable")
    if isinstance(exc, _NoStartTLS):
        return Outcome("dead", "smtp_no_starttls")
    if late:  # the socket was shut down at the deadline: a reply cut short is no answer
        return Outcome("retry", "timeout")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        temporary = any(400 <= code <= 499 for code in codes)
        return Outcome("retry" if temporary else "dead", "smtp_temporary" if temporary else "smtp_permanent",
                       codes[0] if codes and 100 <= codes[0] <= 599 else None)  # fmt: skip
    if isinstance(exc, smtplib.SMTPResponseException):
        status = exc.smtp_code if 100 <= exc.smtp_code <= 599 else None
        if 400 <= exc.smtp_code <= 499:
            return Outcome("retry", "smtp_temporary", status)
        return Outcome("dead", "smtp_permanent", status)
    if isinstance(exc, ssl.SSLError | ssl.CertificateError):
        return Outcome("retry", "tls_error")
    if isinstance(exc, TimeoutError):
        return Outcome("retry", "timeout")
    if isinstance(exc, socket.gaierror):
        return Outcome("retry", "dns_error")
    if isinstance(exc, smtplib.SMTPException) and not isinstance(exc, smtplib.SMTPServerDisconnected):
        return Outcome("dead", "smtp_permanent")  # smtplib's own refusals: no AUTH (in common), no SMTPUTF8
    if isinstance(exc, OSError):
        return Outcome("retry", "connect_error")
    return Outcome("dead", "smtp_permanent")


class Sender:
    """Sends deliveries on two threads of its own, a resolve thread and a mail thread (``close`` shuts them
    down, not waiting): a lookup or mail waits its turn, and one whose caller gave up never starts."""

    def __init__(self, notify: NotifySettings) -> None:
        self.notify = notify
        self._resolving = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ds-notify-dns")
        self._mailing = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ds-notify-mail")
        self._ssl = _tls()
        self._agent = f"deltasleuth-notify/{version('deltasleuth')}"

    def close(self) -> None:
        for pool in (self._resolving, self._mailing):
            pool.shutdown(wait=False, cancel_futures=True)

    async def _resolve(self, host: str, port: int) -> list[str]:
        """``host``'s addresses, from the resolve thread; OSError when it has none."""
        infos = await asyncio.get_running_loop().run_in_executor(
            self._resolving, partial(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        )
        if not infos:
            raise socket.gaierror(f"{host} does not resolve")
        return [str(info[4][0]) for info in infos]

    async def send(self, channel: ChannelSettings, delivery: Mapping[str, Any]) -> Outcome:
        """One attempt of ``delivery`` (``delivery_id``, ``body``) to ``channel``, ended by
        ``timeout_seconds``."""
        started = time.monotonic()
        body = delivery["body"].encode()
        if len(body) > (MAX_TEAMS if channel.kind == "teams" else MAX_BODY):
            outcome = Outcome("dead", "payload_too_large")
        elif channel.kind == "email":
            outcome = await self._mail(channel, delivery["body"], started + self.notify.timeout_seconds)
        else:
            outcome = await self._post(channel, delivery["delivery_id"], body)
        latency = round((time.monotonic() - started) * 1000)
        return Outcome(outcome.state, outcome.error, outcome.status, outcome.retry_after, latency)

    async def _post(self, channel: ChannelSettings, delivery_id: str, body: bytes) -> Outcome:
        notify = self.notify
        try:
            url = channel.url or read_secret(channel.url_ref)  # type: ignore[arg-type]
            if (parts := secure(url, channel.host if channel.url_ref else None, channel.proxy)) is None:
                return Outcome("dead", "url_mismatch")  # the URL is never shown
            headers = {"Content-Type": "application/json", "User-Agent": self._agent}
            if channel.kind == "webhook":
                keys = [signing_key(ref) for ref in channel.signing_key_refs]
                timestamp = int(time.time())
                headers |= {"webhook-id": delivery_id, "webhook-timestamp": str(timestamp),
                            "webhook-signature": signature(keys, delivery_id, timestamp, body)}  # fmt: skip
            proxy = notify.proxy if channel.proxy else None
            _vet(parts.hostname, channel.allow_private_network)
            if proxy is not None:
                _vet(urlsplit(proxy).hostname or "", True)
            resolver = _Resolver(self._resolve, channel.allow_private_network or proxy is not None)
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, ssl=self._ssl),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,  # HTTP(S)_PROXY and netrc are never read
                timeout=aiohttp.ClientTimeout(total=notify.timeout_seconds),
            )
            async with (
                session,
                session.post(url, data=body, headers=headers, allow_redirects=False, proxy=proxy) as response,
            ):
                await response.content.read(READ_BYTES)
                return _http_outcome(response.status, parse_retry_after(response.headers.get("Retry-After")))
        except _Unsafe:
            return Outcome("dead", "unsafe_destination")
        except SecretError:
            return Outcome("retry", "secret_unreadable")
        except TimeoutError:  # before ClientError: ServerTimeoutError is both
            return Outcome("retry", "timeout")
        except aiohttp.ClientConnectorDNSError:
            return Outcome("retry", "dns_error")
        except (aiohttp.ClientSSLError, ssl.SSLError):
            return Outcome("retry", "tls_error")
        except (aiohttp.ClientError, OSError, ValueError):  # the type only: a message may hold the URL
            return Outcome("retry", "connect_error")

    async def _mail(self, channel: ChannelSettings, stored: str, deadline: float) -> Outcome:
        loop = asyncio.get_running_loop()
        host = channel.smtp_host
        assert host is not None
        try:
            password = read_secret(channel.password_ref) if channel.password_ref else None
            message = email.message_from_string(stored, policy=SMTP)
            message["From"] = channel.sender
            message["To"] = ", ".join(channel.recipients)
            left = deadline - time.monotonic()
            literal = _literal(host)
            addresses = (
                [literal] if literal else await asyncio.wait_for(self._resolve(host, channel.smtp_port), left)
            )
            global_ = any(refusal(address, False) is None for address in addresses)
            if not all(_allowed(a, channel.allow_private_network) for a in addresses) or (
                channel.tls == "none" and global_
            ):
                raise _Unsafe
            if (left := deadline - time.monotonic()) <= 0:
                raise TimeoutError
            smtp: _Guarded = (
                _GuardedSMTPSSL(host, addresses[0], deadline, self._ssl) if channel.tls == "implicit"
                else _GuardedSMTP(host, addresses[0], deadline)
            )  # fmt: skip
            loop.call_later(left, smtp.abort)  # whether or not the wait below still runs then
            begun = asyncio.Event()
            started = partial(loop.call_soon_threadsafe, begun.set)
            job = loop.run_in_executor(
                self._mailing, partial(_begin, started, smtp, channel, password, message, self._ssl)
            )
            try:
                try:  # a job not started by the deadline never is; once started, the abort bounds it
                    await asyncio.wait_for(begun.wait(), left)
                except TimeoutError:
                    job.cancel()
                    raise
                refused = await job  # a mail the relay accepted is sent, even if QUIT is answered late
            except asyncio.CancelledError:
                smtp.abort()  # the loop may end before the deadline: the thread is freed now
                raise
        except Exception as exc:
            return _smtp_outcome(exc, time.monotonic() >= deadline)
        return Outcome("sent", "recipients_refused" if refused else None, 250)


# -- without sending: ds notify check and the warnings at start -----------------------------------


def _secrets(channel: ChannelSettings) -> tuple[str | None, list[str]]:
    """The channel's secrets read now, as a send reads them (SecretError when one cannot be): the URL its
    url_ref holds, and what was read."""
    url = read_secret(channel.url_ref) if channel.url_ref is not None else None
    keys = [signing_key(ref) for ref in channel.signing_key_refs]
    read = [f"{len(keys)} signing key{'s' if len(keys) > 1 else ''} readable"] if keys else []
    if channel.password_ref is not None:
        read_secret(channel.password_ref)
        read.append("the password readable")
    return url, read


def unreadable(notify: NotifySettings) -> list[tuple[str, str]]:
    """Each channel with a secret a send cannot read now, and its SecretError text: ``serve``, ``worker`` and
    ``run`` warn of them at start and go on, as the deliveries retry until it is readable."""
    found = []
    for name, channel in notify.channels.items():
        try:
            _secrets(channel)
        except SecretError as exc:
            found.append((name, str(exc)))
    return found


def _addresses(host: str, port: int) -> list[str]:
    """``host``'s addresses, resolved now on the caller's thread (the CLI's); [] when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _unfit(host: str, addresses: list[str], allow_private: bool, *, relay_none: bool = False) -> str | None:
    """Why ``host`` at ``addresses`` is no destination, as ``check`` words it (``_allowed``'s rule; ``tls:
    none`` refuses a global relay); None when it is one."""
    for address in addresses:
        if refusal(address, True) is not None:
            return f"{host} resolves to {address}: never a notification destination"
        if refusal(address, allow_private) is not None:
            return f"{host} resolves to {address}, a private address: set allow_private_network to allow it"
        if relay_none and refusal(address, False) is None:
            return f"{host} resolves to {address}, a global address: tls none sends to a private relay only"
    return None


def check(notify: NotifySettings) -> list[str]:
    """``ds notify check``, sending nothing: each channel's secrets read and its destination resolved and
    vetted as a send does it (a proxied channel's through ``notify.proxy``, resolved once, as any internal
    host may be); an ``ok:`` or ``error:`` line each, the proxy's first. The URL of a url_ref is never
    printed."""
    lines, via = [], None
    if notify.proxy is not None and any(channel.proxy for channel in notify.channels.values()):
        proxy = urlsplit(notify.proxy)
        host, port = proxy.hostname or "", proxy.port or (443 if proxy.scheme == "https" else 80)
        addresses = _addresses(host, port)
        if not addresses:
            lines.append(f"error: notify.proxy {host} does not resolve")
        elif why := _unfit(host, addresses, True):
            lines.append(f"error: notify.proxy {why}")
        else:
            via = f"via proxy {host}:{port} at {addresses[0]}"
    for name, channel in notify.channels.items():
        try:
            url, read = _secrets(channel)
        except SecretError as exc:
            lines.append(f"error: channel {name}: {exc}")
            continue
        host = channel.destination or ""
        if url is not None:
            if secure(url, channel.host, channel.proxy) is None:
                path = (channel.url_ref or "").removeprefix("file:")
                lines.append(f"error: channel {name}: the URL in {path} is not an https URL for host {host}")
                continue
            read.insert(0, "the URL file names the host")
        if channel.proxy:
            if via is None:  # the proxy's error says why
                continue
            where = via
        else:
            if channel.kind == "email":
                port = channel.smtp_port or 0
            else:
                target = urlsplit(url or channel.url or "")
                port = target.port or (443 if target.scheme == "https" else 80)
            addresses = _addresses(host, port)
            if not addresses:
                lines.append(f"error: channel {name}: host {host} does not resolve")
                continue
            relay_none = channel.kind == "email" and channel.tls == "none"
            if why := _unfit(host, addresses, channel.allow_private_network, relay_none=relay_none):
                lines.append(f"error: channel {name}: {why}")
                continue
            private = any(refusal(address, False) is not None for address in addresses)
            where = f"resolves to {', '.join(addresses)}" + (" (private: allowed)" if private else "")
        lines.append(f"ok: channel {name} ({channel.kind}, {host}): {'; '.join([where, *read])}")
    return lines
