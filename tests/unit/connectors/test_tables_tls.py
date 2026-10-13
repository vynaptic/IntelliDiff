"""TLS on a database target is enforced, not preferred: a MySQL server that
offers none is refused before any authentication packet, a certificate is verified with its name against
the declared CA, the stock certificate passes the chain-only mode alone, and the session runs on the
address the guard vetted, whatever name TLS verifies. An Oracle session verifies the chain and, attribute
for attribute, the declared subject before the driver sends a packet, and a listener that speaks no TLS is
said to be one.
"""

from __future__ import annotations

import asyncio
import socket
import socketserver
import ssl
import struct
import threading
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

import pymysql
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from deltasleuth.connectors import netguard
from deltasleuth.connectors.tables import ENGINES, TargetUnreachable, plan
from deltasleuth.connectors.tables import mysql as engine_module
from deltasleuth.core.secrets import SecretError
from deltasleuth.core.spec import TargetRegistry, load_targets
from tests.conftest import MYSQL_NAME, MySQLServer, make_ca, server_certificate

from .test_postgres import compiled

MYSQL = ENGINES["mysql"]
TABLE = {"name": "t", "keyColumns": ["id"]}


def test_the_installed_pymysql_refuses_a_server_without_tls() -> None:
    # Up to 1.1.x PyMySQL carried on in plain text when the server offered no TLS; 1.2.0 raises 2026, and
    # 1.2.1 fixes its escaping, which no statement of this product calls.
    installed = tuple(int(part) for part in metadata.version("pymysql").split(".")[:3])
    assert installed >= (1, 2, 1) and pymysql.VERSION[:3] == installed


class _NoTLS(socketserver.BaseRequestHandler):
    """A MySQL server's greeting without the TLS capability; whatever the client then sends is kept."""

    def handle(self) -> None:
        capabilities = 0x0001 | 0x0008 | 0x0200 | 0x8000 | 0x00080000  # not 0x0800, CLIENT_SSL
        greeting = (
            b"\x0a8.4.11\x00"
            + struct.pack("<I", 7)
            + b"abcdefgh\x00"
            + struct.pack("<HBHHB", capabilities & 0xFFFF, 255, 2, capabilities >> 16, 21)
            + bytes(10)
            + b"ijklmnopqrst\x00caching_sha2_password\x00"
        )
        self.request.sendall(struct.pack("<I", len(greeting))[:3] + b"\x00" + greeting)
        self.request.settimeout(1)
        try:
            self.server.received.append(self.request.recv(4096))  # type: ignore[attr-defined]
        except TimeoutError:
            self.server.received.append(b"")  # type: ignore[attr-defined]


@pytest.fixture
def stub() -> Iterator[Any]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _NoTLS)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def _targets(tmp_path: Path, **entry: Any) -> TargetRegistry:
    (tmp_path / "password").write_text("s3cret-password")
    entry = {
        "engine": "mysql", "host": "127.0.0.1", "database": "ledger", "user": "reader",
        "password_ref": "file:password", "allow_private_network": True,
    } | entry  # fmt: skip
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry}}))
    return load_targets(tmp_path / "targets.yaml")


@pytest.mark.parametrize("members", [{}, {"tls_verify_name": False, "tls_ca_ref": "file:ca.pem"}])
async def test_a_server_that_offers_no_tls_is_refused_before_the_password_leaves(
    stub: Any, tmp_path: Path, members: dict[str, Any]
) -> None:
    (tmp_path / "ca.pem").write_bytes(make_ca("some CA")[0].public_bytes(serialization.Encoding.PEM))
    port = stub.server_address[1]
    targets = _targets(tmp_path, port=port, **members)
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(TABLE))
    assert str(exc.value) == (
        f"side a: cannot connect to target db-a (mysql 127.0.0.1:{port}/ledger as reader):"
        " MySQL 2026: TLS: the server offers none"
    )
    plain = _targets(tmp_path, port=port, allow_unencrypted=True)
    with pytest.raises(TargetUnreachable):  # declared unencrypted, the login is sent (the stub ends it)
        await MYSQL.connect(plain.get("db-a"))
    stub.shutdown()  # every handler has ended
    # No authentication packet, right password or wrong: nothing was sent, until the target allowed it.
    assert stub.received[0] == b"" and b"reader" in stub.received[1] and len(stub.received) == 2


async def test_a_server_that_never_greets_is_given_up_within_the_connect_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "CONNECT_S", 0.5)
    with socket.create_server(("127.0.0.1", 0)) as silent:
        targets = _targets(tmp_path, port=silent.getsockname()[1], allow_unencrypted=True)
        with pytest.raises(TargetUnreachable) as exc:
            await MYSQL.connect(targets.get("db-a"))  # a scan's session: it sets no statement timeout
    assert str(exc.value) == "MySQL 2013"
    with pytest.raises(ValueError) as refused:  # the listener is gone
        await plan(targets, compiled(TABLE))
    assert str(refused.value).endswith("ConnectionRefusedError: no server answered")


async def test_a_name_that_does_not_resolve_and_a_ca_that_is_no_certificate_are_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unknown(host: str, port: int) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    with monkeypatch.context() as unresolved, pytest.raises(ValueError) as exc:
        unresolved.setattr(netguard, "system_resolve", unknown)
        await plan(_targets(tmp_path, host="no-such-host.invalid"), compiled(TABLE))
    assert str(exc.value) == (
        "side a: cannot connect to target db-a (mysql no-such-host.invalid:3306/ledger as reader):"
        " gaierror: the host name does not resolve"
    )
    for content in (
        "not a certificate",
        "",
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    ):
        (tmp_path / "ca.pem").write_text(content)  # an empty file never means the system's trust store
        with pytest.raises(SecretError) as secret:
            await plan(_targets(tmp_path, tls_ca_ref="file:ca.pem"), compiled(TABLE))
        assert str(secret.value) == f"secret file {tmp_path / 'ca.pem'} holds no PEM certificate"


# --- a real server's certificates ---------------------------------------------------------------------


async def _connected(mysql_server: MySQLServer, tmp_path: Path, **members: Any) -> Any:
    """A session on the server's own database as a target declaring ``members``; closed by the caller."""
    entry = mysql_server.target("mysql", **members)
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry}}))
    return await MYSQL.connect(load_targets(tmp_path / "targets.yaml").get("db-a"))


async def _refused(mysql_server: MySQLServer, tmp_path: Path, **members: Any) -> tuple[str, str | None]:
    with pytest.raises(TargetUnreachable) as exc:
        await _connected(mysql_server, tmp_path, **members)
    return str(exc.value), exc.value.gloss


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every name resolves to loopback; the names asked for."""
    asked: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        asked.append(host)
        return ["127.0.0.1"]

    monkeypatch.setattr(netguard, "system_resolve", resolve)
    return asked


NOT_ACCEPTED = ("SSLCertVerificationError", "TLS: the server's certificate was not accepted")


@pytest.mark.db
@pytest.mark.mysql
async def test_a_certificate_is_verified_with_its_name_on_the_vetted_address(
    mysql_server: MySQLServer, tmp_path: Path, resolved: list[str]
) -> None:
    session = await _connected(mysql_server, tmp_path, host=MYSQL_NAME)  # the name the certificate carries
    try:
        peer, cipher = (
            session._sock.getpeername(),
            await session.query("SHOW SESSION STATUS LIKE 'Ssl_cipher'"),
        )
        nodelay = session._sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        keepalive = session._sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    finally:
        await session.close()
    assert resolved == [MYSQL_NAME] and peer == ("127.0.0.1", mysql_server.port)  # dialled by DeltaSleuth
    assert session.encrypted and cipher[0][1] and nodelay and keepalive
    # Another name on the same address, and the same name under another CA, are refused.
    assert await _refused(mysql_server, tmp_path, host="other.ds-test.example") == NOT_ACCEPTED
    (tmp_path / "other-ca.pem").write_bytes(make_ca("another CA")[0].public_bytes(serialization.Encoding.PEM))
    wrong = f"file:{tmp_path / 'other-ca.pem'}"
    assert await _refused(mysql_server, tmp_path, host=MYSQL_NAME, tls_ca_ref=wrong) == NOT_ACCEPTED
    assert await _refused(mysql_server, tmp_path, tls_ca_ref=wrong, tls_verify_name=False) == NOT_ACCEPTED


@pytest.mark.db
@pytest.mark.mysql
async def test_the_stock_certificate_passes_only_the_chain_against_its_own_ca(
    mysql_server: MySQLServer, tmp_path: Path
) -> None:
    stock = f"file:{mysql_server.secrets / 'stock-ca.pem'}"
    with mysql_server.stock_certificate():  # CN=MySQL_Server_..._Auto_Generated_Server_Certificate: no name
        assert await _refused(mysql_server, tmp_path, tls_ca_ref=stock) == NOT_ACCEPTED
        assert await _refused(mysql_server, tmp_path, tls_verify_name=False) == NOT_ACCEPTED  # the test CA
        session = await _connected(mysql_server, tmp_path, tls_ca_ref=stock, tls_verify_name=False)
        await session.close()
    assert session.encrypted


# --- Oracle: the image has no TCPS listener, so the driver's TLS meets a Python TLS listener ------------

ORACLE = ENGINES["oracle"]
ORACLE_NAME = "oracle.ds-test.example"  # the listener certificate's subject; it resolves to loopback here


class _Listener(threading.Thread):
    """A TLS listener that is no database: of each connection it keeps whether the handshake ended and
    the first bytes the client then sent, and closes it."""

    def __init__(self, directory: Path, ca: Any = None, subject: x509.Name | None = None) -> None:
        super().__init__(daemon=True)
        ca = ca or make_ca("DeltaSleuth test CA")
        cert, key = server_certificate(ca, [ORACLE_NAME], ["127.0.0.1"], subject)
        for name, content in (("cert.pem", cert), ("key.pem", key)):
            (directory / name).write_bytes(content)
        (directory / "ca.pem").write_bytes(ca[0].public_bytes(serialization.Encoding.PEM))
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
        self._socket = socket.create_server(("127.0.0.1", 0))
        self.port, self.met = self._socket.getsockname()[1], []  # type: ignore[var-annotated]

    def run(self) -> None:
        while True:
            try:
                raw, _ = self._socket.accept()
            except OSError:  # closed
                return
            try:
                with self._context.wrap_socket(raw, server_side=True) as tls:
                    tls.settimeout(2)
                    self.met.append((True, tls.recv(4096)))
            except OSError:  # the client refused the certificate
                self.met.append((False, b""))
                raw.close()

    def close(self) -> None:
        self._socket.close()
        self.join(5)


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[_Listener]:
    found = _Listener(tmp_path)
    found.start()
    yield found
    found.close()


async def test_oracle_s_tls_verifies_the_chain_and_the_declared_subject_before_the_logon(
    listener: _Listener, tmp_path: Path, resolved: list[str]
) -> None:
    (tmp_path / "password").write_text("s3cret-password")
    (tmp_path / "other-ca.pem").write_bytes(make_ca("another CA")[0].public_bytes(serialization.Encoding.PEM))
    subject, named, chain = f"CN={ORACLE_NAME}", {"tls_ca_ref": "file:ca.pem"}, {"tls_verify_name": False}
    not_accepted = "TLS: the server's certificate was not accepted"
    modes: list[tuple[dict[str, Any], str, tuple[bool, bool]]] = [
        # The chain and the certificate's subject: the driver sends its first packet, the logon comes later.
        (named | {"tls_server_dn": subject}, "DPY-4011", (True, True)),
        (chain | named, "DPY-4011", (True, True)),  # the chain only
        (
            named | {"tls_server_dn": "CN=other.ds-test.example"},
            f"SSLCertVerificationError: {not_accepted}",
            (False, False),
        ),
        (
            {"tls_ca_ref": "file:other-ca.pem", "tls_server_dn": subject},
            f"SSLCertVerificationError: {not_accepted}",
            (False, False),
        ),
        (
            chain | {"tls_ca_ref": "file:other-ca.pem"},
            f"SSLCertVerificationError: {not_accepted}",
            (False, False),
        ),
    ]
    for members, why, met in modes:
        entry = {
            "engine": "oracle", "host": ORACLE_NAME, "port": listener.port, "service": "CRMPDB",
            "user": "reader", "password_ref": "file:password", "allow_private_network": True,
        } | members  # fmt: skip
        (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry}}))
        with pytest.raises(ValueError) as exc:
            await plan(load_targets(tmp_path / "targets.yaml"), compiled(TABLE))
        endpoint = f"db-a (oracle {ORACLE_NAME}:{listener.port}/CRMPDB as reader)"
        assert str(exc.value) == f"side a: cannot connect to target {endpoint}: {why}", members
        for _ in range(100):  # the listener's thread notes the connection once it ended
            if listener.met:
                break
            await asyncio.sleep(0.02)
        handshake, sent = listener.met.pop()
        # A connect packet (type 1) once the certificate is accepted, and nothing before that: a wrong CA
        # and a wrong subject both end the handshake.
        assert (handshake, sent[4:5] == b"\x01") == met and b"s3cret" not in sent and not listener.met, (
            members
        )
    assert resolved == [ORACLE_NAME] * 5  # resolved by the guard alone, and dialled on its answer


async def _dialled(directory: Path, port: int, **members: Any) -> str:
    """Why a run's creation is refused on an Oracle target of ``members`` at the local ``port``."""
    (directory / "password").write_text("s3cret-password")
    entry = {
        "engine": "oracle", "host": ORACLE_NAME, "port": port, "service": "CRMPDB", "user": "reader",
        "password_ref": "file:password", "allow_private_network": True,
    } | members  # fmt: skip
    (directory / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry}}))
    with pytest.raises(ValueError) as exc:
        await plan(load_targets(directory / "targets.yaml"), compiled(TABLE))
    endpoint = f"side a: cannot connect to target db-a (oracle {ORACLE_NAME}:{port}/CRMPDB as reader): "
    assert str(exc.value).startswith(endpoint)
    return str(exc.value).removeprefix(endpoint)


@pytest.mark.parametrize(
    ("declared", "presented", "accepted"),
    [
        # A directory's names repeat an attribute type. The driver's own match kept one value a type, so
        # any machine of the container passed for the database.
        *[
            ("CN=crm-db,CN=Computers,DC=bank,DC=example", presented, accepted)
            for presented, accepted in (
                ("CN=crm-db,CN=Computers,DC=bank,DC=example", True),
                ("CN=laptop-17,CN=Computers,DC=bank,DC=example", False),
                ("CN=laptop-17,CN=Computers,DC=partner,DC=example", False),
                ("CN=crm-db,CN=Users,DC=bank,DC=example", False),
            )
        ],
        *[
            ("CN=crm-db.bank.example,O=Bank", presented, accepted)
            for presented, accepted in (
                ("CN=crm-db.bank.example,O=Bank", True),
                ("CN=evil,CN=crm-db.bank.example,O=Bank", False),  # one attribute more
                ("CN=jdoe\\,CN=crm-db.bank.example,O=Bank", False),  # one name that holds a comma
                ("CN=crm-db.bank.example,OU=Payments,O=Bank", False),
                ("O=Bank,CN=crm-db.bank.example", False),  # in another order
                ("CN=CRM-DB.bank.example,O=Bank", False),  # in another case
            )
        ],
    ],
)
async def test_oracle_s_declared_subject_is_matched_whole(
    tmp_path: Path, resolved: list[str], declared: str, presented: str, accepted: bool
) -> None:
    ca = make_ca("Bank internal CA")
    found = _Listener(tmp_path, ca, x509.Name.from_rfc4514_string(presented))
    found.start()
    try:
        why = await _dialled(tmp_path, found.port, tls_ca_ref="file:ca.pem", tls_server_dn=declared)
        for _ in range(100):  # the listener's thread notes the connection once it ended
            if found.met:
                break
            await asyncio.sleep(0.02)
    finally:
        found.close()
    handshake, sent = found.met.pop()
    if accepted:  # the driver sent its connect packet: the listener, no database, then closed
        assert (why, handshake, sent[4:5]) == ("DPY-4011", True, b"\x01")
    else:  # refused in the handshake: no packet of the driver's left, and so no password
        assert why == "SSLCertVerificationError: TLS: the server's certificate was not accepted"
        assert (handshake, sent) == (False, b"")


@pytest.mark.parametrize("ending", ["reset", "closed"])
@pytest.mark.parametrize("members", [{"tls_server_dn": f"CN={ORACLE_NAME}"}, {"tls_verify_name": False}])
async def test_tls_declared_against_a_listener_that_speaks_none_is_said_to_be_tls(
    tmp_path: Path, resolved: list[str], ending: str, members: dict[str, Any]
) -> None:
    # A plain listener ends a TLS handshake's first bytes, Oracle's with a reset (observed): the likeliest
    # mis-declaration, TLS being the default, once read as a bare ConnectionResetError.
    (tmp_path / "ca.pem").write_bytes(make_ca("some CA")[0].public_bytes(serialization.Encoding.PEM))

    def plain() -> None:
        peer, _ = server.accept()
        peer.recv(4096)
        if ending == "reset":
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        peer.close()

    with socket.create_server(("127.0.0.1", 0)) as server:
        found = []
        for declared in ({"tls_ca_ref": "file:ca.pem"} | members, {"allow_unencrypted": True}):
            thread = threading.Thread(target=plain, daemon=True)
            thread.start()
            found.append(await _dialled(tmp_path, server.getsockname()[1], **declared))
            thread.join(5)
    # Declared unencrypted, the same ending is the network's, not TLS's.
    assert found[0] == "ConnectionResetError: TLS: the handshake failed" and "TLS" not in found[1]
