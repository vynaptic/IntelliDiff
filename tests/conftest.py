"""Shared pytest configuration. Other fixtures live in their own directory's conftest.py."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import oracledb
import psycopg
import pymysql
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pymysql.constants import CLIENT

from deltasleuth import licence
from deltasleuth.auth.principal import ROLES, Principal
from deltasleuth.evidence.store import EvidenceStore
from deltasleuth.security.crypto import DataKey, Keyring, Sealer, key_id
from deltasleuth.security.signing import PackSigner
from deltasleuth.store import repo
from mocks.notify import Receiver

if TYPE_CHECKING:
    from deltasleuth.context import Context

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = "postgres:16-alpine"  # the local image; tests never pull
MYSQL_IMAGE = "mysql:8.4"
ORACLE_IMAGE = "gvenzl/oracle-free:23-slim-faststart"
# The principal of every test call that takes ``who`` or ``created_by``: the CLI's, as a tester.
TESTER = Principal("local:tester@test", "tester", frozenset(ROLES), os_account="tester")
# Fixed keys for contexts and evidence stores built without a database.
TEST_KEYRING = Keyring([b"k" * 32])
TEST_ADDRESS_KEY = DataKey("address", TEST_KEYRING.current, b"a" * 32)
TEST_EVIDENCE_KEY = DataKey(key_id("E-", b"e" * 32), TEST_KEYRING.current, b"e" * 32)
# The data key of runs a test stores without a Context: (its KEK id, the key wrapped) and its sealer,
# bound to one run id, which only such tests' own reads open with.
TEST_DATA_KEY = TEST_KEYRING.wrap(b"r" * 32)
TEST_SEALER = Sealer(b"r" * 32, "R-" + "T" * 26)
# The vendor key of every licence a test signs, made in memory: no private key is committed, and a
# test's vendor build trusts it alone.
VENDOR_KEY = ec.generate_private_key(ec.SECP256R1())
TIER_PAIRS = {"community": 2, "pro": 5, "enterprise": "unlimited"}
BUILDS = ("vendor_keys", "open_build")  # ``build``'s parameters: a test of both asks for it first


@pytest.fixture(scope="session", autouse=True)
def open_build() -> Iterator[None]:
    """The build under test is open: ``licence.VENDOR_KEYS`` is empty for the
    session, whatever ``SHIPPED_KEYS`` holds, which stays."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(licence, "VENDOR_KEYS", ())
        yield


@pytest.fixture
def vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vendor build for the test, which enforces the licence: ``licence.VENDOR_KEYS`` is
    ``VENDOR_KEY``'s public key."""
    monkeypatch.setattr(licence, "VENDOR_KEYS", (PackSigner(VENDOR_KEY).public_pem,))


@pytest.fixture
def build(request: pytest.FixtureRequest) -> str:
    """The build of a test parametrised over ``BUILDS`` (``indirect``): its fixture's name."""
    request.getfixturevalue(request.param)
    return request.param


def licence_file(
    installation_id: str,
    *,
    tier: str = "enterprise",
    pairs: int | str | None = None,
    starts: date | None = None,
    ends: date | None = None,
    key: ec.EllipticCurvePrivateKey | None = None,
    **fields: Any,
) -> bytes:
    """A licence file for ``installation_id`` signed by ``key`` (``VENDOR_KEY``): the tier's pairs, from
    ``starts`` (today, UTC) to ``ends`` (364 days later); ``fields`` replace other values by line name."""
    signer = PackSigner(key or VENDOR_KEY)
    starts = starts or datetime.now(UTC).date()
    pairs = TIER_PAIRS.get(tier, 2) if pairs is None else pairs
    values = {
        "licence": licence.new_id(), "customer": "Example Bank plc", "order": "ORD-2026-014",
        "installation": installation_id, "tier": tier, "pairs": pairs,
        "starts": starts, "ends": ends or starts + timedelta(days=364),
        "issued": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}", "key": signer.fingerprint,
    } | fields  # fmt: skip
    body = licence.body(values)
    return f"{body}signature: {base64.b64encode(signer.sign(body.encode())).decode()}\n".encode()


async def licensed(ctx: Context, **fields: Any) -> licence.Licence:
    """Install, as the tester, a licence of ``fields`` (``licence_file``'s) for ``ctx``'s installation,
    from the database's today unless ``starts`` is given."""
    async with ctx.pool.connection() as conn:
        facts = await repo.licence_facts(conn)
    fields.setdefault("starts", facts["now"].astimezone(UTC).date())
    installed = await licence.install(ctx, licence_file(facts["installation_id"], **fields), who=TESTER)
    return installed["licence"]


async def unlicensed(ctx: Context, *, ended: str | None = None) -> licence.Licence | None:
    """Every stored licence deleted, as only an operator who disables ds.licences' triggers can (their
    ``licence.install`` rows stay: each is warned as missing); with ``ended``, a licence of that tier whose
    grace ended yesterday then stored as ``repo.insert_licence`` stores one: the installation is expired."""
    async with ctx.pool.connection() as conn:
        await conn.execute("ALTER TABLE ds.licences DISABLE TRIGGER licences_withdraw_only")
        await conn.execute("DELETE FROM ds.licences")
        await conn.execute("ALTER TABLE ds.licences ENABLE TRIGGER licences_withdraw_only")
        if ended is None:
            return None
        facts = await repo.licence_facts(conn)
        day = facts["now"].astimezone(UTC).date()
        found = licence.parse(
            licence_file(facts["installation_id"], tier=ended, starts=day - timedelta(days=100),
                         ends=day - timedelta(days=31))
        )  # fmt: skip
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=TESTER.actor)
    return found


async def licensed_once(ctx: Context, tier: str | None = "enterprise") -> licence.Standing:
    """``licensed(ctx, tier=tier)`` unless the store names a licence already (a store opened again),
    ``tier`` is None or the build is open, which needs none; the standing then."""
    async with ctx.pool.connection() as conn:
        if tier is not None and licence.VENDOR_KEYS and (await licence.standing(ctx, conn)).licence is None:
            await licensed(ctx, tier=tier)
        return await licence.standing(ctx, conn)


def evidence_store(root: Path, **options: Any) -> EvidenceStore:
    """An evidence store under ``root`` with the fixed test keys; ``options`` override them."""
    keys = {"id_key": b"i" * 32, "address_key": TEST_ADDRESS_KEY, "evidence_keys": [TEST_EVIDENCE_KEY]}
    return EvidenceStore(root, **keys | options)


async def create_run(
    ctx: Context,
    conn: repo.Conn,
    spec_id: int,
    mode: str,
    keys: Mapping[str, str],
    params: Mapping[str, Any],
    scan: Mapping[str, Any] | None = None,
) -> tuple[str, Sealer]:
    """A queued run as the runner creates it, by the tester: its keys (raw -> masked) sealed under a
    new data key; the run id and its sealer."""
    run_id = repo.new_run_id()
    kek, wrapped, sealer = ctx.new_sealer(run_id)
    await repo.create_run(
        conn, spec_id, mode, sealer.seal_keys(keys), params, scan,
        run_id=run_id, created_by=TESTER.actor, retention="standard", data_key=(kek, wrapped),
    )  # fmt: skip
    return run_id, sealer


def encryption(directory: Path) -> dict[str, str]:
    """The encryption settings: a keyring and a tokeniser key in ``directory``, written once."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("kek", "tokeniser"):
        if not (directory / name).exists():
            (directory / name).write_text(os.urandom(32).hex() + "\n")
    return {"kek_ref": f"file:{directory / 'kek'}", "tokeniser_key_ref": f"file:{directory / 'tokeniser'}"}


@dataclass
class Loopback:
    """The ``loopback`` guard: a test's own ``names``, each resolving to its addresses (a list, or a callable
    giving one per resolve), and what it refused."""

    names: dict[str, list[str] | Callable[[], list[str]]] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)


def _loopback(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return host == "localhost"
    return ip.is_loopback or (
        isinstance(ip, ipaddress.IPv6Address) and bool(ip.ipv4_mapped and ip.ipv4_mapped.is_loopback)
    )


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> Iterator[Loopback]:
    """Fail any resolve or connect that is not to loopback (the notifier's tests never reach a network): a
    refused one raises, and the test fails unless it asserts and clears ``refused``."""
    guard = Loopback()
    resolve, connect = socket.getaddrinfo, socket.socket.connect

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name in guard.names:
            found = guard.names[name]
            addresses = found() if callable(found) else found
            return [
                (
                    socket.AF_INET6 if ":" in a else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (a, int(port or 0)),
                )
                for a in addresses
            ]
        if _loopback(name):
            return resolve(host, port, *args, **kwargs)
        guard.refused.append(f"resolve {name}")
        raise socket.gaierror(socket.EAI_NONAME, "refused by the loopback guard")

    def guarded(sock: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and not _loopback(str(address[0])):
            guard.refused.append(f"connect {address[0]}")
            raise ConnectionRefusedError("refused by the loopback guard")
        return connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield guard
    assert not guard.refused, f"a notification test reached beyond loopback: {guard.refused}"


@contextmanager
def receiving_thread(tmp_path: Path) -> Iterator[tuple[Receiver, str, str]]:
    """A webhook receiver (``mocks.notify``) on a loop of its own thread, for callers whose loop is not theirs
    (a TestClient, a CLI command), verifying one signing key: it, its base URL and the key's reference."""
    raw = os.urandom(32)
    (tmp_path / "hook.key").write_text(f"whsec_{base64.b64encode(raw).decode()}\n")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    found = Receiver([raw])
    base = asyncio.run_coroutine_threadsafe(found.start(), loop).result(10)
    try:
        yield found, base, f"file:{tmp_path / 'hook.key'}"
    finally:
        asyncio.run_coroutine_threadsafe(found.close(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)


# A CI timeout's SIGTERM unwinds like Ctrl-C, so fixture teardown still removes the pg container.
signal.signal(signal.SIGTERM, signal.default_int_handler)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _unavailable(reason: str) -> NoReturn:
    """A skip locally; a failure where the DB tests must run (DS_REQUIRE_DB=1, e.g. CI)."""
    (pytest.fail if os.environ.get("DS_REQUIRE_DB") else pytest.skip)(reason)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str]:
    """A throwaway PostgreSQL server (Docker, local image) for the session; yields an admin DSN."""
    if not shutil.which("docker"):
        _unavailable("docker is not available")
    port, name = _free_port(), f"ds-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--pull", "never", "--name", name, "-e", "POSTGRES_PASSWORD=test",
         "-p", f"127.0.0.1:{port}:5432", PG_IMAGE],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if run.returncode != 0:
        _unavailable(f"cannot start {PG_IMAGE}: {run.stderr.strip()[:200]}")
    dsn = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                psycopg.connect(dsn, connect_timeout=1).close()
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)


@pytest.fixture
def pg_dsn(pg_server: str) -> Iterator[str]:
    """A fresh, empty database per test (isolation without restarting the server)."""
    db = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')
    yield pg_server.rsplit("/", 1)[0] + f"/{db}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


# --- a throwaway MySQL server for table sides -----------------------------


def make_ca(name: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """A certificate authority made for one test session."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    usage = x509.KeyUsage(
        digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
        key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
    )  # fmt: skip
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def server_certificate(
    ca: tuple[x509.Certificate, ec.EllipticCurvePrivateKey],
    names: list[str],
    addresses: list[str],
    subject: x509.Name | None = None,
) -> tuple[bytes, bytes]:
    """A server certificate ``ca`` issued, with a subjectAltName of ``names`` and ``addresses``: its PEM
    and its key's. Its subject is ``subject``, else the first name as a common name."""
    issuer, issuer_key = ca
    key = ec.generate_private_key(ec.SECP256R1())
    alt = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]
    usage = x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
        key_agreement=True, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
    )  # fmt: skip
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject or x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(issuer_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    private = key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return cert.public_bytes(pem), private


MYSQL_NAME = "mysql.ds-test.example"  # in the server certificate; a test resolves it to loopback itself
MYSQL_PASSWORD = "test"  # noqa: S105 (root's, of a throwaway server on loopback)


@dataclass(frozen=True)
class MySQLServer:
    """The session's MySQL server, on loopback: its port, root's password in a file, and its two server
    certificates' CAs in files: the test CA's (a certificate with a subjectAltName of ``MYSQL_NAME``,
    ``localhost`` and 127.0.0.1, which the server presents), and the stock one's (``stock``)."""

    name: str
    port: int
    secrets: Path

    def target(self, database: str, **members: Any) -> dict[str, Any]:
        """A targets-file entry for ``database`` as root, its certificate verified with its name."""
        return {
            "engine": "mysql", "host": "127.0.0.1", "port": self.port, "database": database, "user": "root",
            "password_ref": f"file:{self.secrets}/password", "tls_ca_ref": f"file:{self.secrets}/ca.pem",
            "allow_private_network": True, "rate_per_s": 1000,
        } | members  # fmt: skip

    def execute(self, script: str, database: str | None = None) -> list[tuple[Any, ...]]:
        """Run ``script`` (one statement or several) as root; the last statement's rows."""
        conn = pymysql.connect(
            host="127.0.0.1", port=self.port, user="root", password=MYSQL_PASSWORD, database=database,
            charset="utf8mb4", autocommit=True, client_flag=CLIENT.MULTI_STATEMENTS,
        )  # fmt: skip
        try:
            with conn.cursor() as cursor:
                cursor.execute(script)
                rows = list(cursor.fetchall())
                while cursor.nextset():
                    rows = list(cursor.fetchall())
            return rows
        finally:
            conn.close()

    def database(self, seed: str, options: str = "") -> str:
        """A fresh database created with ``options`` and seeded with ``seed``; its name."""
        name = f"t_{uuid.uuid4().hex[:12]}"
        self.execute(f"CREATE DATABASE `{name}` {options}")
        self.execute(seed, name)
        return name

    @contextmanager
    def stock_certificate(self) -> Iterator[None]:
        """The server presenting the certificate it generated for itself, which carries no name."""
        self._tls("ca.pem", "server-cert.pem", "server-key.pem")
        try:
            yield
        finally:
            self._tls("test-ca.pem", "test-cert.pem", "test-key.pem")

    def _tls(self, ca: str, cert: str, key: str) -> None:
        self.execute(
            f"SET GLOBAL ssl_ca = '{ca}', ssl_cert = '{cert}', ssl_key = '{key}'; ALTER INSTANCE RELOAD TLS"
        )


@pytest.fixture(scope="session")
def mysql_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MySQLServer]:
    """A throwaway MySQL server (Docker, local image) for the session, presenting a certificate of a test
    CA with a subjectAltName."""
    if not shutil.which("docker"):
        _unavailable("docker is not available")
    port, name = _free_port(), f"ds-test-mysql-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--pull", "never", "--name", name,
         "-e", f"MYSQL_ROOT_PASSWORD={MYSQL_PASSWORD}", "-p", f"127.0.0.1:{port}:3306", MYSQL_IMAGE],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if run.returncode != 0:
        _unavailable(f"cannot start {MYSQL_IMAGE}: {run.stderr.strip()[:200]}")
    secrets = tmp_path_factory.mktemp("mysql")
    server = MySQLServer(name, port, secrets)
    try:
        deadline = time.monotonic() + 120
        while True:  # the image first runs a server without a network to initialise itself
            try:
                server.execute("SELECT 1")
                break
            except pymysql.MySQLError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        ca = make_ca("DeltaSleuth test CA")
        cert, key = server_certificate(ca, [MYSQL_NAME, "localhost"], ["127.0.0.1"])
        pem = ca[0].public_bytes(serialization.Encoding.PEM)
        files = {"password": MYSQL_PASSWORD.encode(), "ca.pem": pem}
        files |= {"test-ca.pem": pem, "test-cert.pem": cert, "test-key.pem": key}
        for file, content in files.items():
            (secrets / file).write_bytes(content)
        for file in ("test-ca.pem", "test-cert.pem", "test-key.pem"):  # readable by the server's account
            (secrets / file).chmod(0o644)
            copy = ["docker", "cp", str(secrets / file), f"{name}:/var/lib/mysql/{file}"]
            subprocess.run(copy, capture_output=True, check=True)
        stock = subprocess.run(
            ["docker", "exec", name, "cat", "/var/lib/mysql/ca.pem"], capture_output=True, check=True
        )
        (secrets / "stock-ca.pem").write_bytes(stock.stdout)
        server._tls("test-ca.pem", "test-cert.pem", "test-key.pem")
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)


# --- a throwaway Oracle server for table sides ----------------------------

ORACLE_PASSWORD = "test"  # noqa: S105 (every account's, of a throwaway server on loopback)
ORACLE_SERVICE = "FREEPDB1"
_DDL = re.compile(r"\b(?:CREATE|ALTER|DROP|TRUNCATE)\b", re.IGNORECASE)


@dataclass(frozen=True)
class OracleServer:
    """The session's Oracle server, on loopback: its port and every account's password in a file. It has
    no TCPS listener, so its targets declare ``allow_unencrypted``."""

    name: str
    port: int
    secrets: Path

    def target(self, user: str, **members: Any) -> dict[str, Any]:
        """A targets-file entry for the account ``user``, whose schema is its own."""
        return {
            "engine": "oracle", "host": "127.0.0.1", "port": self.port, "service": ORACLE_SERVICE,
            "user": user, "password_ref": f"file:{self.secrets}/password", "allow_unencrypted": True,
            "allow_private_network": True, "rate_per_s": 1000,
        } | members  # fmt: skip

    def execute(self, script: str, user: str = "system", **binds: Any) -> list[tuple[Any, ...]]:
        """Run ``script`` (statements, a ``;`` and white space between them) as ``user`` and commit; the
        last query's rows. After DDL it waits until a read-only transaction reads the account's tables:
        for seconds after a table's DDL such a read is refused (ORA-01466)."""
        rows: list[tuple[Any, ...]] = []
        with oracledb.connect(
            user=user, password=ORACLE_PASSWORD, host="127.0.0.1", port=self.port, service_name=ORACLE_SERVICE
        ) as conn, conn.cursor() as cursor:  # fmt: skip
            for statement in (part.strip() for part in re.split(r";(?:\s|$)", script)):
                if statement:
                    cursor.execute(statement, binds if ":" in statement else {})
                    rows = cursor.fetchall() if cursor.description else rows
            conn.commit()
            if user != "system" and _DDL.search(script):
                tables = [name for (name,) in cursor.execute("SELECT table_name FROM user_tables")]
                deadline = time.monotonic() + 30
                while tables:
                    try:
                        cursor.execute("SET TRANSACTION READ ONLY")
                        for table in tables:
                            cursor.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608 (its own name)
                        tables = []
                    except oracledb.DatabaseError as exc:
                        if exc.args[0].code != 1466 or time.monotonic() > deadline:
                            raise
                        time.sleep(0.25)
                    finally:
                        conn.commit()
        return rows

    def schema(self, seed: str = "") -> str:
        """A fresh account, which may create tables, views and synonyms, its schema seeded with ``seed``;
        its name."""
        name = f"T_{uuid.uuid4().hex[:12].upper()}"
        self.execute(
            f'CREATE USER {name} IDENTIFIED BY "{ORACLE_PASSWORD}" QUOTA UNLIMITED ON users;'
            f" GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SYNONYM TO {name}"
        )
        self.execute(seed, name)
        return name


@pytest.fixture(scope="session")
def oracle_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OracleServer]:
    """A throwaway Oracle server (Docker, local image) for the session: about a minute to start and 2 GB
    of memory, so it is started once."""
    if not shutil.which("docker"):
        _unavailable("docker is not available")
    secrets = tmp_path_factory.mktemp("oracle")
    (secrets / "password").write_text(ORACLE_PASSWORD)
    port, name = _free_port(), f"ds-test-oracle-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--pull", "never", "--name", name,
         "-e", f"ORACLE_PASSWORD={ORACLE_PASSWORD}", "-p", f"127.0.0.1:{port}:1521", ORACLE_IMAGE],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if run.returncode != 0:
        _unavailable(f"cannot start {ORACLE_IMAGE}: {run.stderr.strip()[:200]}")
    server = OracleServer(name, port, secrets)
    try:
        deadline = time.monotonic() + 300
        while True:  # the listener answers before the pluggable database is open
            try:
                server.execute("SELECT 1 FROM DUAL")
                break
            except (oracledb.Error, OSError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)
