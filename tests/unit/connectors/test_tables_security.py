"""The security review points of table sides on MySQL and Oracle that a test can hold: the drivers load no
client library and read no client configuration, nothing a server says or a statement holds is logged, and
a plan carries a side's engine and its server's major and minor
version, never its patch level, host, port or user; a MySQL endpoint that is hostile or broken is
contained: what it makes the driver raise shows its class alone, its handshake ends with the connect
budget and a packet it sends in parts is bounded. The other points are held where their behaviour is
tested: the vetted address in ``test_guards``, TLS in ``test_tables_tls``, the bounds on what is fetched in
``test_mysql`` and ``test_oracle``, what a certificate says in ``runner/test_census``, and what a scan
across engines stores in ``e2e/test_engines``.
"""

from __future__ import annotations

import json
import logging
import socketserver
import struct
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.tables import ENGINES, TableConnector, TargetUnreachable, plan, preflight
from deltasleuth.connectors.tables import mysql as engine_module
from deltasleuth.core.spec import TargetRegistry, load_targets
from deltasleuth.core.types import ExecutionConfig
from tests.conftest import REPO_ROOT, MySQLServer

from .test_postgres import USERS_SIDE, compiled

ENGINES_SOURCE = REPO_ROOT / "deltasleuth" / "connectors" / "tables"


def test_no_engine_loads_a_client_library_or_reads_a_client_s_configuration() -> None:
    source = "".join(path.read_text() for path in sorted(ENGINES_SOURCE.glob("*.py")))
    # Oracle's thick mode, an option file of MySQL's, the environment, and the driver's own escaping.
    for name in ("init_oracle_client", "read_default", "environ", "getenv", "escape_string", "mogrify"):
        assert name not in source, name


@pytest.mark.db
@pytest.mark.mysql
async def test_a_plan_names_the_engine_and_never_the_endpoint_and_nothing_a_server_says_is_logged(
    mysql_server: MySQLServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = mysql_server.database(
        "CREATE TABLE moments (k DATETIME PRIMARY KEY, v INT); INSERT INTO moments VALUES ('2024-02-29', 1)"
    )
    entry = mysql_server.target(database)
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
    targets = load_targets(tmp_path / "targets.yaml")
    table = {"name": "moments", "keyColumns": ["k"]}
    stored = await plan(targets, compiled(table, table))
    assert stored is not None
    [(version,)] = mysql_server.execute("SELECT VERSION()")
    text = json.dumps(stored)
    assert stored["a"]["server"] == "8.4" and version.startswith("8.4.")
    assert [said for said in (version, entry["host"], str(entry["port"]), '"root"') if said in text] == []
    # The server's answer to an impossible date quotes it (1525): the key is rejected, and neither the
    # key, the statement nor the server's text reaches a log.
    target, execution = targets.get("db-a"), ExecutionConfig()
    reader = TableConnector(target, execution, stored["a"], gate=TargetGate.for_target(target))
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(KeyRejectedError) as exc:
            await reader.fetch("2024-02-31 00:00:00", USERS_SIDE)
    finally:
        await reader.aclose()
    logged = [record.getMessage() for record in caplog.records] + [str(exc.value)]
    assert [line for line in logged if "2024-02-31" in line or "SELECT" in line or "Incorrect" in line] == []


# --- a MySQL endpoint that is hostile or broken --------------------------------------------------------

PART = bytes(2**24 - 1)  # one part of a packet sent in parts: the most a part holds


class _Hostile(socketserver.BaseRequestHandler):
    """A MySQL server without TLS that greets with ``greeting`` (a version, or a whole packet body), a byte
    every ``drip`` seconds, takes any login and answers each statement ``OK``, or as ``answers`` says for
    a statement holding its key."""

    greeting: bytes = b"8.4.11"
    drip = 0.0
    answers: ClassVar[dict[str, Callable[[Any], None]]] = {}

    def packet(self, body: bytes) -> None:
        sent = struct.pack("<I", len(body))[:3] + bytes([self.seq & 255]) + body
        for byte in sent[: len(sent) if self.drip else 0]:
            self.request.sendall(bytes([byte]))
            time.sleep(self.drip)
        self.request.sendall(b"" if self.drip else sent)
        self.seq += 1

    def read(self) -> bytes:
        head = self.request.recv(4, 0x100)  # MSG_WAITALL
        self.seq = head[3] + 1
        return self.request.recv(int.from_bytes(head[:3], "little"), 0x100)

    def handle(self) -> None:
        capabilities = 0x0001 | 0x0008 | 0x0200 | 0x8000 | 0x00080000  # not 0x0800, CLIENT_SSL
        greeting = self.greeting
        if not greeting.startswith(b"\x0a"):
            greeting = (
                b"\x0a" + greeting + b"\x00" + struct.pack("<I", 7) + b"abcdefgh\x00"
                + struct.pack("<HBHHB", capabilities & 0xFFFF, 255, 2, capabilities >> 16, 21)
                + bytes(10) + b"ijklmnopqrst\x00mysql_native_password\x00"
            )  # fmt: skip
        self.seq = 0
        try:
            self.packet(greeting)
            self.read()  # the login
            self.packet(b"\x00\x00\x00\x02\x00\x00\x00")
            while (statement := self.read())[:1] == b"\x03":
                answer = [act for key, act in self.answers.items() if key in statement[1:].decode()]
                answer[0](self) if answer else self.packet(b"\x00\x00\x00\x02\x00\x00\x00")
        except (OSError, IndexError):  # the client ended the connection
            return


@pytest.fixture
def hostile(tmp_path: Path) -> Iterator[Callable[..., TargetRegistry]]:
    """``hostile(**members)``: the targets db-a and db-b on a ``_Hostile`` server of those members."""
    servers = []

    def serve(**members: Any) -> TargetRegistry:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), type("Served", (_Hostile,), members))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        servers.append(server)
        (tmp_path / "password").write_text("s3cret-password")
        entry = {
            "engine": "mysql", "host": "127.0.0.1", "port": server.server_address[1], "database": "ledger",
            "user": "reader", "password_ref": "file:password", "allow_private_network": True,
            "allow_unencrypted": True,
        }  # fmt: skip
        (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"db-a": entry, "db-b": entry}}))
        return load_targets(tmp_path / "targets.yaml")

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def _infile(server: Any) -> None:
    server.packet(b"\xfb/etc/passwd")  # a request for a file of the client's
    server.asked = server.request.recv(4096)


@pytest.mark.parametrize(
    ("members", "code"),
    [
        ({"greeting": b"\x0a8.4.11\x00"}, "error"),  # cut after the version: the driver's struct.error
        ({"greeting": b"ACCT 4929-1234 owner=alice.8.4.11"}, "ValueError"),  # no number: its int()
        ({"greeting": b"\x0a\xff\xfe.8.4.11\x00" + bytes(60)}, "ValueError"),
        ({"answers": {"character_set_server": _infile}}, "RuntimeError"),  # the driver's own refusal
    ],
    ids=["cut", "text", "bytes", "infile"],
)
async def test_what_a_server_makes_the_driver_raise_shows_its_class_alone(
    hostile: Callable[..., TargetRegistry], members: dict[str, Any], code: str
) -> None:
    # Each was a raw exception of the driver's: a traceback from ``ds target check``, or a run creation's
    # 422 holding the server's text.
    targets = hostile(**members)
    port = targets.get("db-a").database.port  # type: ignore[union-attr]
    not_mysql = f"{code}: the answer is not a MySQL server's"
    if "answers" in members:  # a statement of the pre-flight is answered with the request
        with pytest.raises(ValueError) as refused:  # once the neutral error itself, a traceback
            await preflight(targets.get("db-a"), "t")
        said = "target db-a: the session's account of its server could not be read"
        assert str(refused.value) == f"{said}: {not_mysql}" and refused.value.__cause__ is None
        return
    table = {"name": "t", "keyColumns": ["id"]}
    with pytest.raises(ValueError) as exc:
        await plan(targets, compiled(table, table))
    assert str(exc.value) == (
        f"side a: cannot connect to target db-a (mysql 127.0.0.1:{port}/ledger as reader): {not_mysql}"
    )
    with pytest.raises(TargetUnreachable) as raised:  # what a scan waits out and a per-key read records
        await ENGINES["mysql"].connect(targets.get("db-a"))
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


async def test_a_server_that_answers_its_own_variable_with_no_row_is_refused_by_the_pre_flight(
    hostile: Callable[..., TargetRegistry],
) -> None:
    # The stub answers every statement `OK`, the character set's too: reading its first row was an
    # IndexError of DeltaSleuth's own, a traceback and exit 3 from `ds target check`.
    with pytest.raises(ValueError) as refused:
        await preflight(hostile().get("db-a"))
    assert str(refused.value) == (
        "target db-a: the session's account of its server could not be read:"
        " IndexError: the answer is not a MySQL server's"
    )


async def test_a_greeting_dripped_a_byte_at_a_time_ends_with_the_connect_budget(
    hostile: Callable[..., TargetRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A read's timeout starts again at every byte: the greeting's 80 bytes once held a connect for 16 s,
    # and a longer one for as long as its server liked.
    monkeypatch.setattr(engine_module, "CONNECT_S", 1.0)
    targets = hostile(drip=0.2)
    started = time.monotonic()
    with pytest.raises(TargetUnreachable) as exc:
        await ENGINES["mysql"].connect(targets.get("db-a"))
    assert str(exc.value) == "MySQL 2013" and 0.9 < time.monotonic() - started < 3


async def test_a_packet_sent_in_parts_is_refused_past_its_limit(
    hostile: Callable[..., TargetRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert engine_module.MAX_PACKET == 80 * 1024 * 1024  # a fetched batch's 64 MiB and a row's 16 MiB of LOBs

    def parts(server: Any) -> None:  # one row of ten parts, 160 MiB, in answer to the connect's statement
        for _ in range(10):
            server.packet(PART)

    monkeypatch.setattr(engine_module, "MAX_PACKET", 20 * 1024 * 1024)
    targets = hostile(answers={"Ssl_cipher": parts})
    tracemalloc.start()
    with pytest.raises(TargetUnreachable) as exc:
        await ENGINES["mysql"].connect(targets.get("db-a"))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    # The driver joins the parts of a packet without limit: the second part is not read here.
    assert str(exc.value) == "MySQL 2020" and peak < 3 * len(PART)
