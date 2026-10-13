"""MySQL behind the engine seam: its session, its catalogue query, the
primitives of ``compose``'s statements and its error codes.

A session runs on a socket DeltaSleuth opened to an address the guard vetted, handed to PyMySQL, which
still verifies the declared name. PyMySQL is synchronous, so a session owns one thread; it binds nothing
on the server, so every value in a statement is a number or a hexadecimal literal, and its own ``%``
interpolation and escaping are never used. No driver exception leaves this module: each becomes a
``TableError`` holding only its code, because the server's messages quote keys and values, and a server
that is no MySQL server may make the driver raise anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import pymysql
from pymysql import _auth
from pymysql.cursors import SSCursor

from deltasleuth.connectors import netguard
from deltasleuth.connectors.tables import compose, render
from deltasleuth.connectors.tables.engine import (
    Asked,
    Column,
    Figure,
    KeyUnfit,
    Refusal,
    Relation,
    Row,
    Rows,
    SnapshotLost,
    StatementTimeout,
    TableError,
    TargetAuthRefused,
    TargetDenied,
    TargetRefused,
    TargetTempSpace,
    TargetUnreachable,
    keep_alive,
    sized,
    tls_context,
    unreachable,
)
from deltasleuth.connectors.tables.keys import Plan
from deltasleuth.core import jsonio
from deltasleuth.core.secrets import read_secret
from deltasleuth.core.types import DatabaseTarget, Target

BATCH = 10_000  # rows per fetch
BATCH_BYTES = 64 * 1024 * 1024  # or this much fetched, whichever comes first
# What the driver joins of one packet's 16 MiB parts: more than any row a statement of this module asks for.
MAX_PACKET = BATCH_BYTES + compose.MAX_LOB_BYTES
CONNECT_S = 10.0  # the socket, the handshake and the session's settings
_OLDEST = (8, 0, 17)

# The error classes by server code; any other server code is TargetRefused.
_CODES: dict[int, type[TableError]] = {
    **dict.fromkeys((1267, 1270, 1271, 1292, 1366, 1525, 3854), KeyUnfit),
    **dict.fromkeys((1040, 1053, 1129, 1203, 1226, 1317, 4031), TargetUnreachable),
    3024: StatementTimeout,
    1412: SnapshotLost,
    **dict.fromkeys((1045, 1862, 3118), TargetAuthRefused),
    # 1 and 3 are the server's own for a file of its temporary directory it cannot create or write.
    **dict.fromkeys((1, 3, 1021, 1028, 1114), TargetTempSpace),
    # 1054: a column that is gone.
    **dict.fromkeys((1054, 1142, 1143, 1146), TargetDenied),
}
_GLOSSES = {
    1045: "the credential was refused",
    3118: "the account is locked",
    1862: "the password has expired",
    2026: "TLS: the server offers none",
}
_NOT_MYSQL = "the answer is not a MySQL server's"
_VERSION = r"([0-9]+)\.([0-9]+)\.([0-9]+)"
# The class of each declared type; a spatial type or VECTOR has none.
_CLASSES = {
    **dict.fromkeys(("tinyint", "smallint", "mediumint", "int", "bigint", "year", "bit"), "integer"),
    "decimal": "decimal",
    "double": "float",
    "float": "float32",
    **dict.fromkeys(("varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set"), "text"),
    "char": "char",
    "date": "date",
    "datetime": "timestamp",
    "timestamp": "instant",
    "time": "time",
    **dict.fromkeys(("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"), "bytes"),
    "json": "json",
}
_LOB = re.compile(r"(?:tiny|medium|long)?(?:text|blob)\b|json\b")
_HEX = re.compile(r"\\x((?:[0-9a-fA-F]{2})*)")
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
# The forms a literal compared with a column of the class may take; a number is ``render.number``'s.
_MOMENT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)?"
_FORMS = {
    "date": re.compile(_MOMENT),
    "timestamp": re.compile(_MOMENT),
    "instant": re.compile(_MOMENT),
    "time": re.compile(r"-?[0-9]{1,3}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"),
}
# The relation as spelled, else the one name equal ignoring case; only columns the account holds a
# privilege on are listed. Its primary key's columns, by their place in it, those the account may not
# read among them.
_RELATION = """
    SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE, t.ENGINE, c.COLUMN_NAME, c.COLUMN_TYPE, c.DATA_TYPE,
           c.PRIVILEGES, c.IS_NULLABLE, @@character_set_server,
           (SELECT JSON_OBJECTAGG(s.SEQ_IN_INDEX, s.COLUMN_NAME) FROM information_schema.STATISTICS s
            WHERE s.TABLE_SCHEMA = t.TABLE_SCHEMA AND s.TABLE_NAME = t.TABLE_NAME
              AND s.INDEX_NAME = 'PRIMARY')
    FROM information_schema.TABLES t JOIN information_schema.COLUMNS c
         ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
    WHERE (t.TABLE_SCHEMA = {schema} OR LOWER(t.TABLE_SCHEMA) = LOWER({schema}))
      AND (t.TABLE_NAME = {name} OR LOWER(t.TABLE_NAME) = LOWER({name}))
    ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION"""
# The tables and views of the schema of that name (as spelled, else the one equal ignoring case) of which
# the account may read a column.
_RELATIONS = """
    SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE FROM information_schema.TABLES t
    WHERE (t.TABLE_SCHEMA = {schema} OR LOWER(t.TABLE_SCHEMA) = LOWER({schema}))
      AND t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
      AND EXISTS (SELECT 1 FROM information_schema.COLUMNS c
                  WHERE c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
                    AND FIND_IN_SET('select', c.PRIVILEGES))"""
_SESSION = (
    "SET SESSION sql_mode = '', time_zone = '+00:00', wait_timeout = 86400, net_write_timeout = 86400,"
    " net_read_timeout = 86400, max_sort_length = 8388608, max_execution_time = {}"
)


def _text(text: str) -> str:
    """A text value: the hexadecimal digits of its UTF-8 bytes, which hold nothing a statement reads."""
    return f"_utf8mb4 X'{text.encode().hex()}'"


class _Primitives:
    """``compose.Dialect`` for MySQL. A value is exactly one of a number matching
    ``-?[0-9]+(\\.[0-9]+)?``, ``_utf8mb4 X'<hex of its UTF-8>'`` and ``X'<hex>'``: nothing is bound."""

    def ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def relation(self, side: Plan) -> str:
        return f"{self.ident(side['schema'])}.{self.ident(side['name'])} AS t"

    def literal(self, text: str, cls: str, declared: str, binds: compose.Binds) -> str | None:
        if render.family(cls) == "number":
            # As written when it can be: the optimiser puts a key's literal in the column's place, also
            # where the key text is compared, and 1.5 is not the text of 1.50.
            try:
                return text if _NUMBER.fullmatch(text) else jsonio.number_text(render.number(text))
            except ValueError:  # not a number, or one no column holds
                return None
        if cls == "bytes":
            match = _HEX.fullmatch(text)
            return f"X'{match.group(1)}'" if match else None
        if cls in _FORMS and not _FORMS[cls].fullmatch(text):
            return None
        return _text(text)

    def key_text(self, column: str, cls: str, declared: str) -> str:
        name = f"t.{self.ident(column)}"
        if cls in ("text", "char"):  # the server strips a CHAR's padding
            return f"CONVERT({name} USING utf8mb4)"
        if cls == "bytes":
            return f"CONCAT(_utf8mb4 X'5C78', LOWER(HEX({name})))"  # \x and lower-case hex
        if cls == "integer" and ("zerofill" in declared or declared.startswith("bit")):
            name = f"CAST({name} AS UNSIGNED)"  # 7, where the column prints 00007
        return f"CAST({name} AS CHAR)"

    def order(self, text: str) -> str:
        return f"CAST({text} AS BINARY)"

    def exact(self, order: str, part: str, binds: compose.Binds) -> str:
        return f"{order} = X'{part.encode().hex()}'"

    def after(self, orders: Sequence[str], bound: Sequence[bytes], binds: compose.Binds) -> str:
        return f"({', '.join(orders)}) > ({', '.join(f"X'{part.hex()}'" for part in bound)})"

    def render(self, column: str, cls: str, declared: str) -> str:
        return self.rendered(f"t.{self.ident(column)}", cls, declared)

    def rendered(self, value: str, cls: str, declared: str) -> str:
        if cls in ("text", "char"):
            return f"CONVERT({value} USING utf8mb4)"
        if cls == "bytes":
            return value
        if cls in ("timestamp", "instant"):  # six digits; an instant in the session's zone, UTC
            return f"DATE_FORMAT({value}, '%Y-%m-%dT%H:%i:%s.%f')"
        if cls in ("float", "float32"):
            # Through JSON, whose text has no declared width: a cast to CHAR is 22 characters wide, which
            # cuts a longer text in a UNION or a temporary table and rounds a view's expression to fit;
            # a FLOAT comes widened to a double (its own cast prints six digits).
            return f"CAST(CAST({value} AS JSON) AS CHAR)"
        if declared.startswith("bit"):
            return f"CAST(CAST({value} AS UNSIGNED) AS CHAR)"
        return f"CAST({value} AS CHAR)"

    def length(self, column: str, cls: str, declared: str) -> str:
        # The server strips a CHAR's padding; under a PAD SPACE collation c = '' also holds for spaces.
        return f"{'LENGTH' if cls == 'bytes' else 'CHAR_LENGTH'}(t.{self.ident(column)})"

    def distinct(self, column: str, cls: str, declared: str) -> str:
        # A text's bytes: its own collation counts A@example.com and a@example.com once.
        name = f"t.{self.ident(column)}"
        return self.order(self.key_text(column, cls, declared)) if cls in ("text", "char") else name

    def lob(self, declared: str) -> bool:
        return _LOB.match(declared) is not None

    def oversize(self, side: Plan, columns: Sequence[str]) -> str:
        total = " + ".join(f"COALESCE(LENGTH(t.{self.ident(column)}), 0)" for column in columns)
        return f"({total} > {compose.MAX_LOB_BYTES})"

    def row_hash(self, side: Plan, columns: Sequence[str]) -> str:
        # A JSON array keeps a NULL and the boundary between two texts apart; bytes go in as hexadecimal
        # digits. NULL where the array passes a server limit.
        texts = []
        for column in columns:
            cls, declared = side["classes"][column], side["declared"][column]
            hexed = f"HEX(t.{self.ident(column)})"
            texts.append(hexed if cls == "bytes" else self.render(column, cls, declared))
        return f"UNHEX(SHA2(JSON_ARRAY({', '.join(texts)}), 256))"

    def first(self, select: str, count: int, by: int | None = None) -> str:
        return f"({select}{f' ORDER BY {by} DESC' if by else ''} LIMIT {count})"

    def selected(self, condition: str | None) -> str:
        return "1" if condition is None else f"CASE WHEN {condition} THEN 1 ELSE 0 END"

    def prefix_pad(self, text: str, prefix: str, width: int, binds: compose.Binds) -> str:
        # PostgreSQL's ~ '^[0-9]+$', written without $: here it also matches before a trailing newline.
        digits = f"{text} <> '' AND {text} NOT REGEXP '[^0-9]'"
        padded = f"LPAD({text}, GREATEST({int(width)}, CHAR_LENGTH({text})), '0')"
        return f"CASE WHEN {digits} THEN CONCAT({_text(prefix)}, {padded}) END"

    def like(self, parts: list[str]) -> str:
        return " ".join(parts)  # a backslash escapes, as in PostgreSQL

    def arms(self, count: int) -> int:
        return count


PRIMITIVES = _Primitives()


def _error(exc: BaseException) -> TableError:
    """The neutral error of a driver's or a socket's, classified by its origin first."""
    if isinstance(exc, pymysql.MySQLError):
        origin = getattr(exc, "original_exception", None)  # the socket's or TLS's own, wrapped at a connect
        if origin is not None:
            return unreachable(origin)
        number = exc.args[0] if exc.args and type(exc.args[0]) is int else 0
        if not number:
            return TargetUnreachable(type(exc).__name__)
        client = 2000 <= number <= 2999  # the driver's own: no server said it
        return (TargetUnreachable if client else _CODES.get(number, TargetRefused))(
            f"MySQL {number}", _GLOSSES.get(number)
        )
    if not isinstance(exc, OSError | EOFError):  # the driver's own, parsing what no MySQL server sends
        return TargetUnreachable(type(exc).__name__, _NOT_MYSQL)
    return unreachable(exc)


def _dial(addresses: Sequence[str], port: int, deadline: float) -> socket.socket:
    """A socket to the first vetted address that answers before the deadline: PyMySQL is handed it, so
    it resolves nothing and sets neither option. Each address is given its share of the time left, so
    one that never answers leaves the next ones theirs."""
    failure: OSError = TimeoutError()
    for left, address in zip(range(len(addresses), 0, -1), addresses, strict=True):
        try:
            sock = socket.create_connection((address, port), max(deadline - time.monotonic(), 0.001) / left)
        except OSError as exc:  # an address without a route, or a silent one: the next one
            failure = exc
            continue
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        keep_alive(sock)
        return sock
    raise failure


def _shut(handle: socket.socket) -> None:
    """End whatever reads or writes the socket ``handle`` is a handle of."""
    with contextlib.suppress(OSError):  # closed meanwhile
        handle.shutdown(socket.SHUT_RDWR)


class _Connection(pymysql.connections.Connection):
    """PyMySQL's connection, refusing a packet whose 16 MiB parts join to more than ``MAX_PACKET``: the
    driver joins them without a limit, whatever a server sends."""

    _joined = 0

    def _read_packet(self, *args: Any) -> Any:
        self._joined = 0
        return super()._read_packet(*args)

    def _read_bytes(self, num_bytes: int) -> Any:
        self._joined += num_bytes
        if self._joined > MAX_PACKET:
            self._force_close()
            raise pymysql.err.OperationalError(2020, "a packet over the limit")  # CR_NET_PACKET_TOO_LARGE
        return super()._read_bytes(num_bytes)


class _Opened:
    """What a session is opened with, kept to open the short one that cancels its statement."""

    def __init__(self, target: Target, password: bytes, addresses: Sequence[str]) -> None:
        assert target.database is not None
        self.target, self.database, self.password = target, target.database, password
        self.context, self.addresses = tls_context(target.database), addresses

    def open(self, timeout_s: float = 0) -> tuple[pymysql.connections.Connection, socket.socket, bool]:
        """A read-only session whose every statement is bounded by ``timeout_s``, a second handle of its
        socket, and whether it is encrypted. The addresses, the handshake and the settings share the
        connect budget."""
        db: DatabaseTarget = self.database
        deadline = time.monotonic() + CONNECT_S
        sock = _dial(self.addresses, db.port, deadline)
        # PyMySQL wraps the socket for TLS and keeps the wrapper to itself; shutting this handle down
        # shuts the same socket down, whoever reads it.
        handle = sock.dup()
        conn = _Connection(
            host=db.host,  # the name TLS verifies; the socket is already at a vetted address
            port=db.port,
            user=db.user,
            password=self.password,
            database=db.name,
            charset="utf8mb4",
            conv={},  # no value converters: text and bytes only
            autocommit=True,
            local_infile=False,
            defer_connect=True,
            read_timeout=CONNECT_S,
            write_timeout=CONNECT_S,
            ssl=self.context,
            ssl_disabled=self.context is None,
            auth_plugin_map=None if self.context else {"caching_sha2_password": _FullAuthentication},
        )
        # A read's own timeout starts again at every byte a server sends: the budget ends the socket.
        expiry = threading.Timer(max(deadline - time.monotonic(), 0), _shut, (handle,))
        expiry.daemon = True
        expiry.start()
        try:
            # With a context PyMySQL requires TLS: a server whose greeting offers none is refused (2026)
            # before any authentication packet, so the password never meets a peer that stripped it.
            conn.connect(sock)
            _refuse_version(self.target, conn.server_version)
            cipher = _query(conn, "SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            encrypted = bool(cipher and cipher[0][1])
            if self.context is not None and not encrypted:  # the session's own report: a second check
                raise TargetUnreachable("MySQL 2026", _GLOSSES[2026])
            conn.query(_SESSION.format(round(timeout_s * 1000)))
            conn.query("SET SESSION TRANSACTION READ ONLY")
        except BaseException:
            expiry.cancel()
            handle.close()
            if conn.open:  # a failed connect closed it already
                conn.close()
            raise
        expiry.cancel()
        # A statement's own timeout comes first; a scan has none and relies on the socket's keepalive.
        # PyMySQL takes a read timeout only when it is built, and the handshake needed another.
        conn._read_timeout = timeout_s + 30 if timeout_s else None
        return conn, handle, encrypted


class _FullAuthentication:
    """``caching_sha2_password`` without TLS, for an account the server has not cached: the password goes
    under the RSA key the server sends. PyMySQL 1.2.3 reads the server's answer to it and then loses it
    (its function returns nothing), which fails the login it just completed; as a plugin's, a lost
    answer is taken for the end of the exchange."""

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def authenticate(self, packet: Any) -> Any:
        return _auth.caching_sha2_password_auth(self._conn, packet)


def _numbers(version: str) -> tuple[int, ...]:
    """The three numbers of a server's version string, zeros when it holds none; MariaDB's own, which its
    greeting may put 5.5.5- before."""
    found = re.search(_VERSION + ("(?=-MariaDB)" if "MariaDB" in version else ""), version)
    return tuple(int(n) for n in found.groups()) if found else (0, 0, 0)


def _refuse_version(target: Target, version: str) -> None:
    """MariaDB by its version string, and a MySQL older than 8.0.17."""
    maria, number = "MariaDB" in version, _numbers(version)
    if maria or number < _OLDEST:
        product = f"{'MariaDB' if maria else 'MySQL'} {number[0]}.{number[1]}"
        supported = "MySQL 8.0.17 or later is supported, MariaDB is not"
        raise Refusal(f"target {target.target_id} is {product}: {supported}")


def _query(conn: pymysql.connections.Connection, statement: str) -> Rows:
    cursor = conn.cursor()
    cursor.execute(statement)
    return list(cursor.fetchall())


class _Stream(SSCursor):
    """An unbuffered cursor read row by row. It is never drained: its session is closed instead."""

    def fetch(self) -> Rows:
        """The next rows: at most ``BATCH`` of them, or ``BATCH_BYTES``; none at the end."""
        rows: Rows = []
        size = 0
        while len(rows) < BATCH and size < BATCH_BYTES:
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
            size += sum(len(value) for value in row if value is not None)
        return rows

    def abandon(self) -> None:
        """Forget the rows not read, so nothing reads them when the cursor is collected."""
        if self._result is not None:
            self._result.unbuffered_active = False
        self.connection = None


class _Session:
    """A read-only session and the one thread every call of it runs in."""

    def __init__(self, opened: _Opened, thread: ThreadPoolExecutor, timeout_s: float) -> None:
        self._opened, self._thread, self._timeout_s = opened, thread, timeout_s
        self._conn: pymysql.connections.Connection | None = None
        self._sock: socket.socket | None = None
        self._busy = False  # the thread is inside a call
        self._stream: _Stream | None = None  # a statement whose rows are still coming
        self.target, self.encrypted, self.version = opened.target, False, ""

    def _run(
        self, call: Callable[..., Any], *args: Any, then: Callable[[Any], Any] | None = None
    ) -> asyncio.Future[Any]:
        """``call`` in the session's thread, sent to it now; ``then`` shapes its answer there. Whatever
        the driver raises becomes a neutral error: a server may make it raise anything."""

        def guarded() -> Any:
            self._busy = True
            try:
                try:
                    found = call(*args)
                except (TableError, Refusal):
                    raise
                except Exception as exc:
                    self._ended()
                    raise _error(exc) from None
                return then(found) if then else found
            finally:
                self._busy = False

        future = asyncio.get_running_loop().run_in_executor(self._thread, guarded)
        # Its error is read here too: a caller cancelled meanwhile no longer waits for it.
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        return future

    def _ended(self) -> None:
        """The session's statement ended, or is given up: no row of it is still coming."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.abandon()

    async def open(self) -> None:
        def opening() -> None:
            self._conn, self._sock, self.encrypted = self._opened.open(self._timeout_s)
            self.version = self._conn.server_version

        await self._run(opening)

    async def query(self, statement: str, then: Callable[[Rows], Any] | None = None) -> Any:
        """The statement's rows, through ``then`` in the session's thread when it is given."""
        return await self._run(_query, self._conn, statement, then=then)

    def stream(self, statement: str, shaped: Callable[[Rows], Rows] = list) -> AsyncIterator[Rows]:
        """The statement's rows in fetched batches, each through ``shaped`` in the session's thread. The
        statement is sent now; what the server answers it with is raised at the first fetch."""

        def start() -> _Stream:
            self._stream = self._conn.cursor(_Stream)  # type: ignore[union-attr]
            self._stream.execute(statement)
            return self._stream

        def fetch(cursor: _Stream) -> Rows:
            rows = cursor.fetch()
            if not rows:
                self._stream = None
            return rows

        started = self._run(start)

        async def batches() -> AsyncIterator[Rows]:
            cursor = await started
            while rows := await self._run(fetch, cursor, then=shaped):
                yield rows

        return batches()

    def _kill(self) -> None:
        conn, handle, _ = self._opened.open()
        try:
            conn.query(f"KILL QUERY {self._conn.thread_id():d}")  # type: ignore[union-attr]
        finally:
            handle.close()
            conn.close()

    async def close(self) -> None:
        """Close the session. A statement still running, or whose rows are still coming, is cancelled
        first, best effort, from a second short session; shutting the socket down then wakes the thread
        if it is blocked on it."""
        handle, self._sock = self._sock, None
        if handle is not None and self._conn is not None:
            if self._busy or self._stream is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.to_thread(self._kill), CONNECT_S)
            self._ended()
            _shut(handle)
            handle.close()
            self._thread.submit(self._conn.close)  # once the statement the shutdown woke has ended
        self._thread.shutdown(wait=False)


class MySQL:
    """``connectors.tables.Engine`` over PyMySQL."""

    name = "mysql"
    arms = 200
    unfit = (KeyUnfit, UnicodeEncodeError)  # the server's codes, and a lone surrogate, which has no UTF-8
    fold = None  # a column's name matches in any case
    hides = True
    grant = "SELECT"
    sum_digits = 65  # a DECIMAL's
    empty_null = False
    retry_alone = True

    def built(self, column: str, cls: str, declared: str, key: bool) -> str | None:
        return (PRIMITIVES.key_text if key else PRIMITIVES.render)(column, cls, declared)

    async def connect(self, target: Target, *, timeout_s: float = 0) -> _Session:
        database = target.database
        assert database is not None
        password = read_secret(database.password_ref).encode()  # at every connect, as the CA is
        try:
            addresses = await netguard.vetted(database.host, database.port, target.allow_private_network)
        except OSError as exc:
            raise unreachable(exc) from None
        opened = _Opened(target, password, addresses)
        thread = ThreadPoolExecutor(1, thread_name_prefix="ds-mysql")
        session = _Session(opened, thread, timeout_s)
        try:
            await session.open()
        except BaseException:
            thread.shutdown(wait=False)
            raise
        return session

    @asynccontextmanager
    async def snapshot(self, target: Target, *, timeout_s: float = 0) -> AsyncIterator[_Session]:
        """A session inside one consistent snapshot: of an InnoDB table. A view's cannot be observed and
        another storage engine's table has none (the plan's ``consistency``)."""
        session = await self.connect(target, timeout_s=timeout_s)
        try:
            await session.query("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await session.query("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            yield session
        finally:
            await session.close()

    async def server(self, session: _Session) -> dict[str, Any]:
        try:
            charset = (await session.query("SELECT @@character_set_server"))[0][0]
        except IndexError:  # no row: a MySQL server answers its own variable with one
            raise TargetUnreachable("IndexError", _NOT_MYSQL) from None
        return {"version": session.version, "charset": charset, "tls": session.encrypted}

    def release(self, session: _Session) -> str:
        return "{}.{}".format(*_numbers(session.version))

    async def describe(
        self, session: _Session, schema: str | None, name: str, *, side: Any = None
    ) -> Relation | None:
        database = session.target.database
        assert database is not None
        asked = (schema or database.name, name)
        found: dict[tuple[str, str], Rows] = {}
        try:
            listed = await session.query(_RELATION.format(schema=_text(asked[0]), name=_text(asked[1])))
        except KeyUnfit:  # a name the catalogue's character set cannot hold names no relation
            return None
        for row in listed:
            found.setdefault((row[0], row[1]), []).append(row)
        if asked not in found and len(found) != 1:
            return None
        rows = found.get(asked) or next(iter(found.values()))
        relation, storage = rows[0][2], rows[0][3]  # a view's storage engine is NULL
        columns = []
        for _, _, _, _, column, declared, kind, privileges, nullable, _, _ in rows:
            cls = _CLASSES.get(kind)
            declared = kind if kind in ("enum", "set") else declared  # not their members
            fraction = cls == "timestamp" and declared != "datetime"
            lob, zerofill = PRIMITIVES.lob(declared), cls == "decimal" and "zerofill" in declared
            keyable = cls in render.KEYS and not (lob or fraction or zerofill)
            readable = "select" in privileges.split(",")
            says = None if relation == "VIEW" else nullable == "YES"  # a view's is the server's guess
            columns.append(
                Column(column, declared, cls, readable, cls in ("text", "char"), keyable, lob, says,
                       sized(cls, declared))
            )  # fmt: skip
        consistency = "snapshot" if storage == "InnoDB" else "unverified" if relation == "VIEW" else "none"
        key = json.loads(rows[0][10]) if rows[0][10] else None
        facts = {
            "kind": "table" if relation == "BASE TABLE" else "view",
            "key": key and [key[place] for place in sorted(key, key=int)],
            "server": self.release(session),
            "consistency": consistency,
            "tls": session.encrypted,
            "charset": rows[0][9],
        }
        return Relation(rows[0][0], rows[0][1], tuple(columns), facts)

    async def probe(self, session: _Session, side: Plan) -> None:
        await session.query(compose.probe(PRIMITIVES, side)[0])

    def large(self, side: Plan) -> bool:
        return compose.large(PRIMITIVES, side)

    async def rows(self, session: _Session, side: Plan, asked: Asked) -> list[Row]:
        """The documents are built off the event loop, in the session's thread."""
        statement, _ = compose.rows(PRIMITIVES, side, asked)
        if not statement:
            return []
        found: list[Row] = await session.query(statement, partial(compose.found, PRIMITIVES, side, asked))
        return found

    async def scan(self, session: _Session, side: Plan, after: Sequence[bytes] | None) -> AsyncIterator[Rows]:
        """Each fetched batch is canonicalised and hashed off the event loop, in the session's thread; the
        columns the server hashed itself come as 32 bytes a row."""
        return session.stream(compose.scan(PRIMITIVES, side, after)[0], compose.digested(PRIMITIVES, side))

    def census(self, session: _Session, side: Plan) -> AsyncIterator[Rows]:
        return session.stream(compose.census(PRIMITIVES, side)[0])

    def frame(self, session: _Session, side: Plan) -> AsyncIterator[Rows]:
        return session.stream(compose.frame(PRIMITIVES, side)[0], compose.stratified(side))

    async def null_keys(self, session: _Session, side: Plan) -> int:
        return int((await session.query(compose.null_keys(PRIMITIVES, side)[0]))[0][0])

    async def relations(self, session: _Session, schema: str | None) -> list[tuple[str, str]]:
        database = session.target.database
        assert database is not None
        asked = schema or database.name
        try:
            listed = await session.query(_RELATIONS.format(schema=_text(asked)))
        except KeyUnfit:  # a name the catalogue's character set cannot hold names no schema
            return []
        schemas = {row[0] for row in listed}
        if asked not in schemas and len(schemas) != 1:
            return []
        shown = asked if asked in schemas else next(iter(schemas), asked)
        kinds = {"BASE TABLE": "table", "VIEW": "view"}
        found = [(name, kinds[kind]) for own, name, kind in listed if own == shown]
        return sorted(found, key=lambda relation: relation[0].encode())

    async def figures(self, session: _Session, side: Plan, wanted: Sequence[Figure]) -> list[str | None]:
        """The texts are read off the event loop, in the session's thread."""
        found: list[str | None] = await session.query(
            compose.figures(PRIMITIVES, side, wanted)[0], compose.figured(side, wanted)
        )
        return found

    async def duplicates(self, session: _Session, side: Plan) -> tuple[int, int]:
        keys, rows = (await session.query(compose.duplicates(PRIMITIVES, side)[0]))[0]
        return int(keys), int(rows)
