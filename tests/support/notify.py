"""Local notification receivers for the notifier's tests: nothing leaves 127.0.0.1.

* ``Receiver``: an aiohttp app. ``POST /webhook`` checks the Standard Webhooks signature against its keys,
  ``/slack`` answers ``ok``, ``/teams`` 202 as a Workflows webhook does; ``GET /__received`` lists what
  arrived; ``POST /__fail?status=&times=&retry_after=`` scripts the next answers, ``/__hang?times=`` holds
  them open.
* ``SmtpSink``: an asyncio SMTP server with STARTTLS (a certificate of an in-memory CA, ``Authority``), AUTH
  PLAIN, scripted 4yz and 5yz replies by command, a hang and a slow drip.
* ``Proxy``: a recording HTTP proxy, absolute-form requests and CONNECT tunnels.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import os
import ssl
import tempfile
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HOSTS = ("localhost", "127.0.0.1", "smtp.test", "hooks.test")  # the names the server certificate holds


class Authority:
    """An in-memory CA and a server certificate it signed for ``HOSTS``."""

    def __init__(self) -> None:
        now = dt.datetime.now(dt.UTC)
        self._key, self._server_key = (
            ec.generate_private_key(ec.SECP256R1()),
            ec.generate_private_key(ec.SECP256R1()),
        )
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "deltasleuth test CA")])
        issuer = self._key.public_key()

        def made(subject: x509.Name, key: Any, extensions: list[tuple[Any, bool]]) -> x509.Certificate:
            builder = (
                x509.CertificateBuilder().subject_name(subject).issuer_name(name).public_key(key)
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=1))
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer), critical=False)
            )  # fmt: skip
            for extension, critical in extensions:
                builder = builder.add_extension(extension, critical=critical)
            return builder.sign(self._key, hashes.SHA256())

        usage = dict.fromkeys(("content_commitment", "key_encipherment", "data_encipherment", "key_agreement",
                               "encipher_only", "decipher_only"), False)  # fmt: skip
        self.cert = made(name, issuer, [
            (x509.BasicConstraints(ca=True, path_length=None), True),
            (x509.KeyUsage(digital_signature=False, key_cert_sign=True, crl_sign=True, **usage), True),
        ])  # fmt: skip
        names = [
            x509.IPAddress(ipaddress.ip_address(h)) if h[0].isdigit() else x509.DNSName(h) for h in HOSTS
        ]
        self.server_cert = made(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]),
                                self._server_key.public_key(), [
            (x509.BasicConstraints(ca=False, path_length=None), True),
            (x509.SubjectAlternativeName(names), False),
            (x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False),
        ])  # fmt: skip

    @property
    def pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def client(self) -> ssl.SSLContext:
        """A client context trusting this CA only."""
        return ssl.create_default_context(cadata=self.pem)

    def server(self) -> ssl.SSLContext:
        """The server's context: ``ssl`` loads a chain from a file only, so the test chain is written to a
        temporary file while it loads."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        pem = self.server_cert.public_bytes(serialization.Encoding.PEM) + self._server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(pem)
        try:
            context.load_cert_chain(handle.name)
        finally:
            os.unlink(handle.name)
        return context


def verify(keys: list[bytes], headers: Any, body: bytes) -> bool:
    """Whether one ``v1`` signature of ``webhook-signature`` is HMAC-SHA256 of ``id.timestamp.body``."""
    signed = f"{headers.get('webhook-id')}.{headers.get('webhook-timestamp')}.".encode() + body
    wanted = {base64.b64encode(hmac.new(k, signed, hashlib.sha256).digest()).decode() for k in keys}
    given = [
        s.removeprefix("v1,") for s in headers.get("webhook-signature", "").split(" ") if s.startswith("v1,")
    ]
    return any(hmac.compare_digest(g, w) for g in given for w in wanted)


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes
    verified: bool | None = None


class Receiver:
    """The HTTP receiver; ``keys`` are the webhook signing keys it verifies against."""

    def __init__(self, keys: list[bytes] | None = None, ssl_context: ssl.SSLContext | None = None) -> None:
        self.keys, self.ssl_context = keys or [], ssl_context
        self.received: list[Received] = []
        self.failures: list[tuple[int, str | None]] = []  # the next answers
        self.hangs = 0
        self.server: TestServer | None = None

    async def _take(self, request: web.Request) -> web.Response:
        body = await request.read()
        verified = verify(self.keys, request.headers, body) if request.path == "/webhook" else None
        self.received.append(Received(request.path, dict(request.headers), body, verified))
        if self.hangs:
            self.hangs -= 1
            await asyncio.sleep(3600)
        if self.failures:
            status, retry_after = self.failures.pop(0)
            return web.Response(status=status, headers={"Retry-After": retry_after} if retry_after else {})
        if request.path == "/webhook" and not verified:
            return web.Response(status=401)
        return web.Response(status=202) if request.path == "/teams" else web.Response(status=200, text="ok")

    async def _fail(self, request: web.Request) -> web.Response:
        q = request.query
        self.failures += [(int(q["status"]), q.get("retry_after"))] * int(q.get("times", "1"))
        return web.Response(status=204)

    async def _hang(self, request: web.Request) -> web.Response:
        self.hangs += int(request.query.get("times", "1"))
        return web.Response(status=204)

    async def _list(self, request: web.Request) -> web.Response:
        return web.json_response([{"path": r.path, "verified": r.verified} for r in self.received])

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/__fail", self._fail)
        app.router.add_post("/__hang", self._hang)
        app.router.add_get("/__received", self._list)
        for path in ("/webhook", "/slack", "/teams"):
            app.router.add_post(path, self._take)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server(ssl=self.ssl_context, access_log=None)  # a query may hold a secret
        return f"{'https' if self.ssl_context else 'http'}://127.0.0.1:{self.server.port}"

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()


@dataclass
class Mail:
    sender: str
    recipients: list[str]
    data: bytes
    login: str | None


@dataclass
class SmtpSink:
    """An SMTP server on 127.0.0.1: ``replies`` scripts a reply by command (``GREETING``, ``MAIL``, ``RCPT``,
    ``AUTH``, ``STARTTLS``, ``END`` of data; ``RCPT`` a list, one per recipient), ``starttls`` whether it is
    offered, ``hang`` that the greeting never comes, ``drip`` seconds between the bytes of every reply."""

    authority: Authority | None = None
    implicit: bool = False
    starttls: bool = True
    replies: dict[str, Any] = field(default_factory=dict)
    hang: bool = False
    drip: float = 0.0
    mails: list[Mail] = field(default_factory=list)
    port: int = 0
    _server: asyncio.Server | None = None

    async def start(self) -> int:
        tls = self.authority.server() if self.implicit and self.authority else None
        self._server = await asyncio.start_server(self._session, "127.0.0.1", 0, ssl=tls)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def reply(line: str) -> None:
            data = f"{line}\r\n".encode()
            if self.drip:
                for i in range(len(data)):
                    writer.write(data[i : i + 1])
                    await writer.drain()
                    await asyncio.sleep(self.drip)
            else:
                writer.write(data)
                await writer.drain()

        mail: Mail | None = None
        login = None
        try:
            if self.hang:
                await asyncio.sleep(3600)
            await reply(self.replies.get("GREETING", "220 sink ready"))
            while line := await reader.readline():
                verb, _, arg = line.decode().rstrip("\r\n").partition(" ")
                verb = verb.upper()
                scripted = self.replies.get(verb)
                if verb == "RCPT" and isinstance(scripted, list):
                    scripted = scripted.pop(0) if scripted else None
                if scripted:
                    await reply(scripted)
                elif verb in ("EHLO", "HELO"):
                    offers = ["250-sink", "250-AUTH PLAIN"] + (["250-STARTTLS"] if self.starttls else [])
                    await reply("\r\n".join([*offers, "250 8BITMIME"]))
                elif verb == "STARTTLS" and self.authority:
                    await reply("220 go ahead")
                    await writer.start_tls(self.authority.server())
                elif verb == "AUTH":
                    login = base64.b64decode(arg.split(" ", 1)[1]).split(b"\0")[1].decode()
                    await reply("235 accepted")
                elif verb == "MAIL":
                    mail = Mail(arg.split(":", 1)[1].strip("<> "), [], b"", login)
                    await reply("250 ok")
                elif verb == "RCPT" and mail is not None:
                    mail.recipients.append(arg.split(":", 1)[1].strip("<> "))
                    await reply("250 ok")
                elif verb == "DATA" and mail is not None:
                    await reply("354 go on")
                    while (chunk := await reader.readline()) != b".\r\n":
                        mail.data += chunk
                    self.mails.append(mail)
                    await reply(self.replies.get("END", "250 queued"))
                elif verb == "QUIT":
                    await reply("221 bye")
                    break
                else:
                    await reply("250 ok")
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            writer.close()


@dataclass
class Proxy:
    """A recording HTTP proxy on 127.0.0.1: what it was asked for (``requests``: method and target)."""

    requests: list[tuple[str, str]] = field(default_factory=list)
    port: int = 0
    _server: asyncio.Server | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._session, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ")
            self.requests.append((method, target))
            if method == "CONNECT":
                host, port = target.rsplit(":", 1)
                up_reader, up_writer = await asyncio.open_connection(host, int(port))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
                return
            url = target.removeprefix("http://")
            hostport, _, path = url.partition("/")
            host, _, port = hostport.rpartition(":")
            up_reader, up_writer = await asyncio.open_connection(host, int(port))
            fields = [h.partition(b":") for h in head.split(b"\r\n")[1:] if h]
            length = next((int(v) for k, _, v in fields if k.lower() == b"content-length"), 0)
            kept = [k + b":" + v for k, _, v in fields if k.lower() != b"connection"]
            request = [f"{method} /{path} HTTP/1.1".encode(), *kept, b"Connection: close", b"", b""]
            up_writer.write(b"\r\n".join(request) + await reader.readexactly(length))
            await _pipe(up_reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
