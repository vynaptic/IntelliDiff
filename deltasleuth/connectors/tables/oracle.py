"""Oracle behind the engine seam (docs/mvp/ENGINES.md §4 to §11): its session, its catalogue query, the
primitives of ``compose``'s statements and its error codes.

The driver is python-oracledb in thin mode only: no Oracle client library is loaded and no ``tnsnames.ora``,
``sqlnet.ora`` or wallet is read. Its ``host`` is always an address the guard vetted, never the declared
name, so a certificate's subject is matched, here and whole, against the target's ``tls_server_dn``. Every
value in a statement is a bind variable typed by its column's class. No session switch makes a session
read-only, so every read runs inside a read-only transaction. No driver exception leaves this module: each
becomes a ``TableError`` holding only its code, because the server's messages quote values.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import oracledb
from cryptography import x509

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
    tls_context,
    unreachable,
)
from deltasleuth.connectors.tables.keys import Plan
from deltasleuth.core import jsonio
from deltasleuth.core.secrets import read_secret
from deltasleuth.core.types import Target

BATCH = 10_000  # rows per fetch at most
BATCH_BYTES = 64 * 1024 * 1024  # over the declared width of a row: what a fetch may hold
CONNECT_S = 10.0  # the socket, the handshake, the logon and the session's settings
_OLDEST = 19
_ARMS = (1, 2, 4, 8, 16, 32, 50)  # the arm counts a per-key statement is written with: seven texts a plan
_HASHES = 60  # column hashes joined in one text: 64 hexadecimal digits each

# §11's classes by server code; any other server code is TargetRefused.
_CODES: dict[int, type[TableError]] = {
    **dict.fromkeys((1722, 1426, 1438, *range(1830, 1862), 12899), KeyUnfit),
    **dict.fromkeys(
        (
            *(18, 20, 28, 1033, 1034, 1035, 1089, 2391, 2396, 3113, 3114, 3135, 3136, 12170, 12514, 12516),
            *(12518, 12520, 12537, 12541, 12547, 12571),
        ),
        TargetUnreachable,
    ),
    1013: StatementTimeout,
    **dict.fromkeys((1555, 8176, 1466), SnapshotLost),
    **dict.fromkeys((1017, 28000, 28001), TargetAuthRefused),
    1652: TargetTempSpace,
    **dict.fromkeys((942, 904, 1031, 41900), TargetDenied),
}
_GLOSSES = {
    "ORA-01017": "the credential was refused",
    "ORA-28000": "the account is locked",
    "ORA-28001": "the password has expired",
    "ORA-12514": "the listener does not know the service",
    "DPY-6001": "the listener does not know the service",  # the driver's own code for the listener's 12514
    "ORA-12541": "no server answered",
    "DPY-6005": "no server answered",
    "DPY-3001": "the server requires Native Network Encryption: ask for a TCPS listener",
    "DPY-3015": "the account has only a 10G password verifier",
}
# How a listener that speaks no TLS ends a handshake's first bytes (a reset, observed).
_CUT = ("ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError")
# The class of each declared type (§7), its precision and length left out; NUMBER and FLOAT are classed
# by their scale. A type not listed (LONG, BFILE, XMLTYPE, an object or a collection) has none.
_CLASSES = {
    "BINARY_DOUBLE": "float",
    "BINARY_FLOAT": "float32",
    **dict.fromkeys(("VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB"), "text"),
    **dict.fromkeys(("CHAR", "NCHAR"), "char"),
    "BOOLEAN": "boolean",
    **dict.fromkeys(("DATE", "TIMESTAMP"), "timestamp"),
    **dict.fromkeys(("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"), "instant"),
    **dict.fromkeys(("RAW", "BLOB"), "bytes"),
    "JSON": "json",
    **dict.fromkeys(("INTERVAL DAY TO SECOND", "INTERVAL YEAR TO MONTH", "ROWID", "UROWID"), "other"),
}
_LOBS = ("CLOB", "NCLOB", "BLOB", "JSON")
_UNKEYED = ("FLOAT", "NCHAR", "NVARCHAR2", *_LOBS)  # of a class with a key text, yet no key (§8)
_HEX = re.compile(r"\\x((?:[0-9a-fA-F]{2})*)")
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_MOMENT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?)?")
_SCALED = re.compile(r"NUMBER\((\*|[0-9]+),([0-9]+)\)")
_LENGTH = re.compile(r"\(([0-9]+)")
_MOMENTS = 'SYYYY-MM-DD"T"HH24:MI:SS'  # the year signed: YYYY prints 44 BC and AD 44 alike
# The settings every statement relies on (§6), then what the session says of them and of itself.
_SESSION = (
    "ALTER SESSION SET NLS_SORT = BINARY NLS_COMP = BINARY NLS_CALENDAR = 'GREGORIAN' TIME_ZONE = 'UTC'"
    " NLS_NUMERIC_CHARACTERS = '.,'"
)
_SETTINGS = {
    "NLS_SORT": "BINARY",
    "NLS_COMP": "BINARY",
    "NLS_CALENDAR": "GREGORIAN",
    "NLS_NUMERIC_CHARACTERS": ".,",
    "TIME_ZONE": "UTC",
}
_READ_BACK = """
    SELECT (SELECT value FROM nls_session_parameters WHERE parameter = 'NLS_SORT'),
           (SELECT value FROM nls_session_parameters WHERE parameter = 'NLS_COMP'),
           (SELECT value FROM nls_session_parameters WHERE parameter = 'NLS_CALENDAR'),
           (SELECT value FROM nls_session_parameters WHERE parameter = 'NLS_NUMERIC_CHARACTERS'),
           SESSIONTIMEZONE, SYS_CONTEXT('USERENV', 'NETWORK_PROTOCOL'),
           SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'),
           (SELECT value FROM nls_database_parameters WHERE parameter = 'NLS_CHARACTERSET'),
           (SELECT value FROM nls_database_parameters WHERE parameter = 'NLS_NCHAR_CHARACTERSET')
    FROM DUAL"""
# The relation as spelled, else upper-cased; ALL_TAB_COLUMNS lists no column the server made for itself.
# Each column's place in the primary key, if it has one.
_RELATION = """
    SELECT c.owner, c.table_name, c.column_name, c.data_type, c.data_length, c.data_precision, c.data_scale,
           c.char_length, c.char_used, c.collation, c.nullable,
           (SELECT 'view' FROM all_views v WHERE v.owner = c.owner AND v.view_name = c.table_name),
           (SELECT k.position FROM all_cons_columns k JOIN all_constraints p
                ON p.owner = k.owner AND p.constraint_name = k.constraint_name
            WHERE p.constraint_type = 'P' AND p.owner = c.owner AND p.table_name = c.table_name
              AND k.column_name = c.column_name)
    FROM all_tab_columns c
    WHERE c.owner IN (:owner, :owner_folded) AND c.table_name IN (:name, :name_folded)
    ORDER BY c.owner, c.table_name, c.column_id NULLS LAST, c.column_name"""
# The tables and views of the schema of that name (as spelled, else upper-cased) the account may read: its
# own, those of a privilege to read any table, and those granted READ or SELECT to it, to PUBLIC or to a role
# it holds. ALL_TABLES also lists a table the account may only write.
_RELATIONS = """
    SELECT o.owner, o.name, o.kind FROM (
        SELECT owner, table_name AS name, 'table' AS kind FROM all_tables
        WHERE owner IN (:owner, :owner_folded) AND dropped = 'NO' AND nested = 'NO' AND secondary = 'N'
          AND (iot_type IS NULL OR iot_type = 'IOT')
        UNION ALL SELECT owner, view_name, 'view' FROM all_views WHERE owner IN (:owner, :owner_folded)) o
    WHERE o.owner = USER
       OR EXISTS (SELECT 1 FROM session_privs WHERE privilege IN ('SELECT ANY TABLE', 'READ ANY TABLE'))
       OR EXISTS (SELECT 1 FROM all_tab_privs p
                  WHERE p.table_schema = o.owner AND p.table_name = o.name
                    AND p.privilege IN ('SELECT', 'READ')
                    AND (p.grantee IN (USER, 'PUBLIC') OR p.grantee IN (SELECT role FROM session_roles)))"""
# The synonym of that name, the schema's own before a public one.
_SYNONYM = """
    SELECT s.table_owner, s.table_name, s.db_link
    FROM all_synonyms s
    WHERE s.owner IN (:owner, :owner_folded, :shared) AND s.synonym_name IN (:name, :name_folded)
    ORDER BY CASE WHEN s.owner = 'PUBLIC' THEN 1 ELSE 0 END,
             CASE WHEN s.owner = :owner AND s.synonym_name = :name THEN 0 ELSE 1 END"""
_ORDERS = "SELECT UTL_I18N.STRING_TO_RAW('a', 'AL32UTF8') FROM DUAL"


def _bind(binds: compose.Binds, value: str | bytes) -> str:
    """``value`` bound under the next name; its placeholder."""
    name = f"b{len(binds)}"
    binds[name] = value
    return f":{name}"


class _Primitives:
    """``compose.Dialect`` for Oracle: a value is a bind variable, typed by the class of its column."""

    def __init__(self, utf8: bool = True) -> None:
        self._utf8 = utf8  # the database is AL32UTF8: a key text's own bytes are its code-point order

    def ident(self, name: str) -> str:
        if '"' in name or "\0" in name:  # no catalogue name holds either
            raise Refusal("a name holding a double quote is not read")
        return f'"{name}"'

    def relation(self, side: Plan) -> str:
        return f"{self.ident(side['schema'])}.{self.ident(side['name'])} t"  # AS is refused there

    def literal(self, text: str, cls: str, declared: str, binds: compose.Binds) -> str | None:
        if render.family(cls) == "number":
            try:
                text = text if _NUMBER.fullmatch(text) else jsonio.number_text(render.number(text))
            except ValueError:  # not a number
                return None
            return f"TO_NUMBER({_bind(binds, text)})"
        if cls == "bytes":
            match = _HEX.fullmatch(text)
            return f"HEXTORAW({_bind(binds, match.group(1))})" if match else None
        if cls in ("timestamp", "instant"):
            match = _MOMENT.fullmatch(text)
            if match is None or (declared == "DATE" and match.group(1)):
                return None
            if declared == "DATE":  # compared as a DATE, so the column's index serves
                return f"TO_DATE({_bind(binds, text.replace('T', ' '))}, 'YYYY-MM-DD HH24:MI:SS')"
            return f"TO_TIMESTAMP({_bind(binds, text.replace('T', ' '))}, 'YYYY-MM-DD HH24:MI:SS.FF')"
        if cls == "boolean":
            return text.upper() if text.lower() in ("true", "false") else None
        if cls == "char":
            # A plain bind finds nothing and a cast to the column's length cuts a longer value; the
            # widest CHAR does neither and keeps the index.
            national = declared.startswith("NCHAR")
            if len(text) > 1000 if national else len(text.encode()) > 2000:
                return None
            return f"CAST({_bind(binds, text)} AS {'NCHAR(1000)' if national else 'CHAR(2000)'})"
        return _bind(binds, text)

    def key_text(self, column: str, cls: str, declared: str) -> str:
        name = f"t.{self.ident(column)}"
        if cls == "text":
            return name
        if cls == "char":
            return f"RTRIM({name}, ' ')"
        if cls == "bytes":  # \x and lower-case hex
            return f"NVL2({name}, '\\x' || LOWER(RAWTOHEX({name})), NULL)"
        if cls == "timestamp":  # a DATE or TIMESTAMP(0); a year before 1 would print as its AD twin
            return f"CASE WHEN {name} >= DATE '0001-01-01' THEN TO_CHAR({name}, 'YYYY-MM-DD HH24:MI:SS') END"
        if declared == "NUMBER":  # unconstrained: the text of a whole number, none for any other
            whole = f"{name} = TRUNC({name}) AND ABS({name}) < 1E38"
            return f"CASE WHEN {whole} THEN TO_CHAR({name}, 'TM9') END"
        scaled = _SCALED.fullmatch(declared)
        if cls == "decimal" and scaled:  # NUMBER(10,2) prints 1.50, as PostgreSQL; TM9 prints 1.5
            precision, scale = (38 if scaled[1] == "*" else int(scaled[1])), int(scaled[2])
            return f"TO_CHAR({name}, 'FM{'9' * (max(precision - scale, 1) - 1)}0.{'0' * scale}')"
        return f"TO_CHAR({name}, 'TM9')"

    def order(self, text: str) -> str:
        # In any other character set a text's own bytes are not its code points' order: its UTF-8 are.
        return text if self._utf8 else f"UTL_I18N.STRING_TO_RAW({text}, 'AL32UTF8')"

    def exact(self, order: str, part: str, binds: compose.Binds) -> str | None:
        if not part:  # '' is NULL there: the key text of no row
            return None
        return f"{order} = {_bind(binds, part if self._utf8 else part.encode())}"

    def after(self, orders: Sequence[str], bound: Sequence[bytes], binds: compose.Binds) -> str:
        """No row comparison exists there: (o1 > a) OR (o1 = a AND o2 > b). An empty part is NULL to the
        server, where > '' is never true: any text is after it and none equals it."""
        arms, equal = [], []
        for order, part in zip(orders, bound, strict=True):
            value = _bind(binds, part.decode() if self._utf8 else part) if part else None
            greater = f"{order} > {value}" if value else f"{order} IS NOT NULL"
            arms.append(f"({' AND '.join([*equal, greater])})")
            equal.append(f"{order} = {value}" if value else "1 = 0")
        return f"({' OR '.join(arms)})"

    def render(self, column: str, cls: str, declared: str) -> str:
        return self.rendered(f"t.{self.ident(column)}", cls, declared)

    def rendered(self, value: str, cls: str, declared: str) -> str:
        if cls in ("text", "char", "bytes"):
            return value
        if cls == "json":
            return f"JSON_SERIALIZE({value} RETURNING CLOB)"
        if cls == "timestamp":
            return f"TO_CHAR({value}, '{_MOMENTS}{'' if declared == 'DATE' else '.FF9'}')"
        if cls == "instant":  # the instant, whatever zone it was stored with
            return f"TO_CHAR(SYS_EXTRACT_UTC({value}), '{_MOMENTS}.FF9')"
        if cls in ("integer", "decimal"):
            return f"TO_CHAR({value}, 'TM9')"
        if declared in ("ROWID", "UROWID"):
            return f"ROWIDTOCHAR({value})"
        return f"TO_CHAR({value})"  # a float's 17 or 9 digits, TRUE or FALSE, an interval

    def length(self, column: str, cls: str, declared: str) -> str:
        name = f"t.{self.ident(column)}"
        if cls == "bytes":  # LENGTH of a RAW counts its hexadecimal digits
            return f"LENGTH(RAWTOHEX({name})) / 2"
        if cls == "char":  # a blank one trims to NULL, being '': its length is 0
            return f"NVL2({name}, NVL(LENGTH4(RTRIM({name}, ' ')), 0), NULL)"
        return f"LENGTH4({name})"  # code points: LENGTH counts an emoji of an NVARCHAR2 twice

    def distinct(self, column: str, cls: str, declared: str) -> str:
        # Binary under the session's NLS_COMP, which a column's own declared collation overrides (not
        # observed: the local image refuses to declare one, ORA-43929); a CHAR as stored, every value of
        # the column padded alike.
        return f"t.{self.ident(column)}" + (" COLLATE BINARY" if " COLLATE " in declared else "")

    def lob(self, declared: str) -> bool:
        return declared in _LOBS

    def oversize(self, side: Plan, columns: Sequence[str]) -> str:
        total = " + ".join(f"COALESCE(LENGTH(t.{self.ident(column)}), 0)" for column in columns)
        return f"({total} > {compose.MAX_LOB_BYTES})"

    def row_hash(self, side: Plan, columns: Sequence[str]) -> str:
        # 32 bytes a column whatever it holds, so a boundary cannot move; a NULL hashes as the empty
        # string, which the server does not tell from it. The hashes are joined as hexadecimal digits,
        # 60 at most in the 4000 bytes of a text, and such blocks hashed again.
        classes, declared = side["classes"], side["declared"]
        hashes = [self.render(column, classes[column], declared[column]) for column in columns]
        hashes = [f"STANDARD_HASH({rendered}, 'SHA256')" for rendered in hashes]
        while True:
            blocks = [" || ".join(hashes[i : i + _HASHES]) for i in range(0, len(hashes), _HASHES)]
            hashes = [f"STANDARD_HASH({block}, 'SHA256')" for block in blocks]
            if len(hashes) == 1:
                return hashes[0]

    def first(self, select: str, count: int, by: int | None = None) -> str:
        if by is None:  # the probe: parsed, reading no row
            return f"SELECT 1 FROM ({select}) WHERE 1 = 0"  # noqa: S608 (a statement compose wrote)
        return f"({select} ORDER BY {by} DESC FETCH FIRST {count} ROWS ONLY)"

    def selected(self, condition: str | None) -> str:
        return "1" if condition is None else f"CASE WHEN {condition} THEN 1 ELSE 0 END"

    def prefix_pad(self, text: str, prefix: str, width: int, binds: compose.Binds) -> str:
        # PostgreSQL's ~ '^[0-9]+$': nothing is left once the digits are trimmed, and it is not empty.
        digits = f"LTRIM({text}, '0123456789') IS NULL AND {text} IS NOT NULL"
        padded = f"LPAD({text}, GREATEST({int(width)}, LENGTH({text})), '0')"
        return f"CASE WHEN {digits} THEN {_bind(binds, prefix)} || {padded} END"

    def like(self, parts: list[str]) -> str:
        return " ".join(parts) + " ESCAPE '\\'"  # the server's own LIKE, a backslash escaping

    def arms(self, count: int) -> int:
        return next((arms for arms in _ARMS if arms >= count), count)


PRIMITIVES = _Primitives()


def _error(exc: BaseException) -> TableError:
    """The neutral error of a driver's or a socket's, classified by its origin first (§5). A server's code
    is shown as the server's, also where the driver puts one of its own in its place (ORA-03113 comes as
    DPY-4011)."""
    if not isinstance(exc, oracledb.Error):
        return unreachable(exc)
    found = exc.args[0] if exc.args else None
    number, driver = getattr(found, "code", 0), getattr(found, "full_code", "") or type(exc).__name__
    if number:
        shown = f"ORA-{number:05d}"
        return _CODES.get(number, TargetRefused)(shown, _GLOSSES.get(shown))
    if driver == "DPY-6005" and exc.__cause__ is not None:  # a connect that failed, for this reason
        return _error(exc.__cause__)
    return (StatementTimeout if driver == "DPY-4024" else TargetUnreachable)(driver, _GLOSSES.get(driver))


def _settled(target: Target, version: str, read: Sequence[Any], encrypted: bool) -> None:
    """Refuse a server older than 19 (§12 P3), a session whose settings read back differently (P4), and
    one that reports no TLS where the target declares it (P1)."""
    major, minor = [*version.split("."), "0"][:2]
    if not major.isdigit() or int(major) < _OLDEST:
        raise Refusal(f"target {target.target_id} is Oracle {major}.{minor}: 19c or later is supported")
    for (setting, wanted), found in zip(_SETTINGS.items(), read, strict=False):
        if found != wanted:
            why = "a logon trigger overrides it"
            raise Refusal(f"the session could not be set ({setting} is {found}): {why}")
    if encrypted and read[5] != "tcps":
        raise TargetUnreachable(str(read[5]), "TLS: the server offers none")


def _fetch(side: Plan, columns: Sequence[str]) -> int:
    """Rows a fetch: what ``BATCH_BYTES`` holds of rows as wide as the columns read are declared, a LOB
    column counting as the most a row may hold of them (§6)."""
    width = 0
    for declared in (side["declared"][column] for column in columns):
        length = _LENGTH.search(declared)
        width += compose.MAX_LOB_BYTES if declared in _LOBS else max(64, 4 * int(length[1]) if length else 0)
    return max(1, min(BATCH, BATCH_BYTES // max(width, 1)))


def _subject(expected: x509.Name) -> type[ssl.SSLObject]:
    """TLS that ends its handshake unless the certificate's subject is ``expected``, attribute for
    attribute in order. The driver's own match keeps one value an attribute type and no order, so
    ``CN=crm-db,CN=Computers`` would also pass another machine's ``CN=laptop,CN=Computers``."""

    class Checked(ssl.SSLObject):
        def do_handshake(self) -> None:
            super().do_handshake()
            certificate = self.getpeercert(True)
            if certificate is None or x509.load_der_x509_certificate(certificate).subject != expected:
                raise ssl.SSLCertVerificationError("the certificate's subject is not tls_server_dn")

    return Checked


async def _dial(addresses: Sequence[str], deadline: float, **options: Any) -> oracledb.AsyncConnection:
    """A logged-on connection to the first vetted address that answers: the driver is handed the address,
    so it resolves nothing. Each address is given its share of the time left until ``deadline`` (the
    loop's time) for its connect and logon, so one that never answers leaves the next ones theirs."""
    failure: Exception = TimeoutError()
    for left, address in zip(range(len(addresses), 0, -1), addresses, strict=True):
        try:
            async with asyncio.timeout((deadline - asyncio.get_running_loop().time()) / left):
                return await oracledb.connect_async(host=address, **options)
        except TimeoutError as exc:  # a silent address: the next one
            failure = exc
        except oracledb.Error as exc:
            if not isinstance(exc.__cause__, OSError):  # a server answered: its refusal is the answer
                raise
            failure = exc  # an address without a route: the next one
    raise failure


def _socket(conn: oracledb.AsyncConnection) -> Any:
    """The connection's socket, None when it is not found. The thin driver shows none: it is asyncio's
    transport, held by the driver's transport, which its protocol holds, which the connection holds."""
    held = [conn._impl]
    for _ in range(3):
        held = [found for holder in held for found in gc.get_referents(holder) if not isinstance(found, type)]
        for found in held:
            if isinstance(found, asyncio.BaseTransport):
                return found.get_extra_info("socket")
    return None


async def _closed(conn: oracledb.AsyncConnection, busy: bool = False) -> None:
    """Close the connection; a statement still running on it is cancelled first."""
    with contextlib.suppress(oracledb.Error, OSError):
        if busy:
            conn.cancel()
    with contextlib.suppress(oracledb.Error, OSError):  # a TimeoutError is an OSError
        await asyncio.wait_for(conn.close(), CONNECT_S)


class _Session:
    """A session of an account that only reads: outside the one transaction of a pass (``pin``), every
    read runs in a read-only transaction of its own."""

    def __init__(self, conn: oracledb.AsyncConnection, target: Target, read: Sequence[Any]) -> None:
        self._conn, self._pinned, self._busy, self._open = conn, False, 0, True
        self.target, self.version = target, conn.version
        self.encrypted, self.schema, self.charset, self.national = read[5] == "tcps", *read[6:9]
        self.dialect = PRIMITIVES if self.charset == "AL32UTF8" else _Primitives(utf8=False)

    async def _call[T](self, call: Awaitable[T]) -> T:
        self._busy += 1
        try:
            return await call
        except (oracledb.Error, OSError, EOFError) as exc:
            raise _error(exc) from None
        finally:
            self._busy -= 1

    async def pin(self) -> None:
        """Begin the one read-only transaction of a pass: every statement after it sees its moment."""
        await self._call(self._conn.execute("SET TRANSACTION READ ONLY"))
        self._pinned = True

    async def query(
        self, statement: str, binds: compose.Binds | None = None, *, decimals: bool = False
    ) -> Rows:
        """The statement's rows, a LOB as text or bytes, a number as a ``Decimal`` with ``decimals``. Outside
        a pass the read is a transaction of its own, in which a write is refused (ORA-01456): two more round
        trips a statement."""
        try:
            if not self._pinned:
                await self._call(self._conn.execute("SET TRANSACTION READ ONLY"))
            fetched = self._conn.fetchall(statement, binds or {}, fetch_lobs=False, fetch_decimals=decimals)
            return await self._call(fetched)
        finally:
            if not self._pinned:
                with contextlib.suppress(TableError):  # the statement's own error is the one to show
                    await self._call(self._conn.commit())

    def stream(
        self, statement: str, binds: compose.Binds, size: int, shaped: Callable[[Rows], Rows] | None = None
    ) -> AsyncIterator[Rows]:
        """The statement's rows in fetches of ``size``, each through ``shaped`` off the event loop. The
        statement is sent now; what the server answers it with is raised at the first fetch."""
        cursor = self._conn.cursor()
        cursor.arraysize = cursor.prefetchrows = size
        started = asyncio.ensure_future(self._call(cursor.execute(statement, binds, fetch_lobs=False)))
        # Its error is read here too: a caller that closed the session meanwhile no longer waits for it.
        started.add_done_callback(lambda done: done.cancelled() or done.exception())

        async def batches() -> AsyncIterator[Rows]:
            await started
            while rows := await self._call(cursor.fetchmany(size)):
                yield await asyncio.to_thread(shaped, rows) if shaped else rows

        return batches()

    async def close(self) -> None:
        if self._open:
            self._open = False
            await _closed(self._conn, bool(self._busy))


class Oracle:
    """``connectors.tables.Engine`` over python-oracledb, thin mode."""

    name = "oracle"
    arms = 50
    unfit = (KeyUnfit, UnicodeEncodeError)  # the server's codes, and a lone surrogate, which no bind takes
    fold = staticmethod(str.upper)
    hides = False
    grant = "READ"
    sum_digits = 38  # a NUMBER's (docs/mvp/PRECHECKS.md §4): a longer sum is rounded silently
    empty_null = True
    retry_alone = True

    def built(self, column: str, cls: str, declared: str, key: bool) -> str | None:
        return (PRIMITIVES.key_text if key else PRIMITIVES.render)(column, cls, declared)

    async def connect(self, target: Target, *, timeout_s: float = 0) -> _Session:
        database = target.database
        assert database is not None
        password = read_secret(database.password_ref)  # at every connect, as the CA is
        context = tls_context(database)
        options: dict[str, Any] = {"protocol": "tcp"}
        if context is not None:
            # The driver dials an address, so no host name is matched: the certificate's subject is,
            # in the handshake, by DeltaSleuth (the driver's own match is not exact).
            context.check_hostname = False
            if database.tls_server_dn is not None:
                context.sslobject_class = _subject(x509.Name.from_rfc4514_string(database.tls_server_dn))
            options = {"protocol": "tcps", "ssl_context": context, "ssl_server_dn_match": False}
        options |= {
            "user": database.user,
            "password": password,  # an argument of its own, in no connect string
            "port": database.port,
            "service_name": database.name,
            "expire_time": 1,
            "tcp_connect_timeout": CONNECT_S,
            "retry_count": 0,
        }
        logged = False
        try:
            addresses = await netguard.vetted(database.host, database.port, target.allow_private_network)
            deadline = asyncio.get_running_loop().time() + CONNECT_S
            async with asyncio.timeout_at(deadline):  # every address, the logon and the settings
                conn = await _dial(addresses, deadline, **options)
                logged = True
                try:
                    if (sock := _socket(conn)) is not None:  # the driver's own idle time is the platform's
                        keep_alive(sock)
                    await conn.execute(_SESSION)
                    read = await conn.fetchone(_READ_BACK)
                    _settled(target, conn.version, read, context is not None)
                    conn.call_timeout = round(timeout_s * 1000)  # the client's own, each round trip
                except BaseException:
                    await _closed(conn)
                    raise
        except (oracledb.Error, OSError) as exc:  # a TimeoutError is an OSError
            failed = _error(exc)
            if context is not None and not logged and str(failed) in _CUT:
                failed.gloss = "TLS: the handshake failed"
            raise failed from None
        return _Session(conn, target, read)

    @asynccontextmanager
    async def snapshot(self, target: Target, *, timeout_s: float = 0) -> AsyncIterator[_Session]:
        """A session inside one read-only transaction: every statement of the pass sees its moment."""
        session = await self.connect(target, timeout_s=timeout_s)
        try:
            await session.pin()
            yield session
        finally:
            await session.close()

    async def server(self, session: _Session) -> dict[str, Any]:
        """Outside AL32UTF8 it also proves that the role may execute what orders text keys there (P5)."""
        if session.charset != "AL32UTF8":
            await _orders(session)
        return {"version": session.version, "charset": session.charset, "tls": session.encrypted}

    def release(self, session: _Session) -> str:
        return ".".join(session.version.split(".")[:2])

    async def describe(
        self, session: _Session, schema: str | None, name: str, *, side: Any = None
    ) -> Relation | None:
        asked = (schema or session.schema, name)
        rows = await _listed(session, *asked)
        if not rows:  # one synonym level: the real owner and name are stored
            shared = "PUBLIC" if schema is None else asked[0]
            synonyms = await session.query(_SYNONYM, _spelled(*asked) | {"shared": shared})
            if not synonyms:
                return None
            owner, table, link = synonyms[0]
            if link is not None:
                shown = f"{schema}.{name}" if schema else name
                not_read = "a synonym over a database link is not read"
                raise Refusal(f"table or view {shown} not found or not visible: {not_read}")
            rows = await _listed(session, owner, table, folded=False)
            if not rows:
                return None
        owner, table = rows[0][:2]
        relation = PRIMITIVES.relation({"schema": owner, "name": table})
        try:  # the catalogue lists a table the role may only write: a statement says whether it reads
            await session.query(f"SELECT 1 FROM {relation} WHERE 1 = 0")  # noqa: S608 (catalogue names, quoted)
            readable = True
        except TargetDenied:
            readable = False
        kind = rows[0][11] or "table"
        columns = tuple(_column(readable, *row[2:11], relation=kind) for row in rows)
        key = sorted((row[12], row[2]) for row in rows if row[12] is not None)
        facts = {
            "kind": kind,
            "key": [column for _, column in key] or None,
            "server": self.release(session),
            "consistency": "snapshot",
            "tls": session.encrypted,
            "charset": session.charset,
            "national": session.national,  # the server's hash reads a text in its own character set (§10)
        }
        return Relation(owner, table, columns, facts)

    async def probe(self, session: _Session, side: Plan) -> None:
        if session.charset != "AL32UTF8":
            _orderable(side, session.charset)
            await _orders(session)
        await session.query(*compose.probe(session.dialect, side))

    def large(self, side: Plan) -> bool:
        return compose.large(PRIMITIVES, side)

    async def rows(self, session: _Session, side: Plan, asked: Asked) -> list[Row]:
        statement, binds = compose.rows(session.dialect, side, asked)
        if not statement:
            return []
        return compose.found(session.dialect, side, asked, await session.query(statement, binds))

    async def scan(self, session: _Session, side: Plan, after: Sequence[bytes] | None) -> AsyncIterator[Rows]:
        """Each fetched batch is canonicalised and hashed off the event loop; the columns the server
        hashed itself come as 32 bytes a row (§10)."""
        size = _fetch(side, [*side["keys"], *compose.worker_columns(side)])
        digested = compose.digested(session.dialect, side)
        return session.stream(*compose.scan(session.dialect, side, after), size, digested)

    def census(self, session: _Session, side: Plan) -> AsyncIterator[Rows]:
        return session.stream(*compose.census(session.dialect, side), _fetch(side, side["keys"]))

    def frame(self, session: _Session, side: Plan) -> AsyncIterator[Rows]:
        size = _fetch(side, [*side["keys"], *side.get("strata", {}).values()])
        return session.stream(*compose.frame(session.dialect, side), size, compose.stratified(side))

    async def null_keys(self, session: _Session, side: Plan) -> int:
        return int((await session.query(*compose.null_keys(session.dialect, side)))[0][0])

    async def relations(self, session: _Session, schema: str | None) -> list[tuple[str, str]]:
        asked = schema or session.schema
        listed = await session.query(_RELATIONS, {"owner": asked, "owner_folded": asked.upper()})
        owner = asked if any(row[0] == asked for row in listed) else asked.upper()
        found = [(name, kind) for own, name, kind in listed if own == owner]
        return sorted(found, key=lambda relation: relation[0].encode())

    async def figures(self, session: _Session, side: Plan, wanted: Sequence[Figure]) -> list[str | None]:
        statement, binds = compose.figures(session.dialect, side, wanted)
        return compose.figured(side, wanted)(await session.query(statement, binds, decimals=True))

    async def duplicates(self, session: _Session, side: Plan) -> tuple[int, int]:
        keys, rows = (await session.query(*compose.duplicates(session.dialect, side)))[0]
        return int(keys), int(rows)


def _spelled(owner: str, name: str, folded: bool = True) -> dict[str, str]:
    """A relation's names as spelled and, when ``folded``, as the server folds an unquoted name."""
    fold = str.upper if folded else str
    return {"owner": owner, "owner_folded": fold(owner), "name": name, "name_folded": fold(name)}


async def _listed(session: _Session, owner: str, name: str, *, folded: bool = True) -> Rows:
    """The columns of the relation of that name: as spelled, else, when ``folded``, the one relation of
    its upper-cased names; none when no relation or several answer."""
    found: dict[tuple[str, str], Rows] = {}
    for row in await session.query(_RELATION, _spelled(owner, name, folded)):
        found.setdefault((row[0], row[1]), []).append(row)
    if (owner, name) not in found and len(found) != 1:
        return []
    return found.get((owner, name)) or next(iter(found.values()))


def _column(
    readable: bool, name: str, kind: str, length: int, precision: int | None, scale: int | None, chars: int,
    used: str | None, collation: str | None, nullable: str = "Y", relation: str = "table",
) -> Column:  # fmt: skip
    """A column as the catalogue declares it: its type written out, its class, whether it can be a key
    (§8), whether it may hold NULL (a view's is the server's guess: not said) and its size."""
    declared, cls, size = kind, _CLASSES.get(re.sub(r"\([0-9]+\)", "", kind)), None
    if kind == "NUMBER" and scale is not None:
        digits = "*" if precision is None else precision
        declared = f"NUMBER({digits})" if scale == 0 and precision else f"NUMBER({digits},{scale})"
        cls = "integer" if scale <= 0 else "decimal"
        size = ("digits", precision or 38, scale) if cls == "decimal" else None  # * is 38
    elif kind in ("NUMBER", "FLOAT"):
        declared, cls = kind if kind == "NUMBER" else f"FLOAT({precision})", "decimal"
    elif kind in ("VARCHAR2", "CHAR"):
        declared = f"{kind}({chars}{' CHAR' if used == 'C' else ''})"
        size = ("characters" if used == "C" else "octets", chars)
    elif kind in ("NVARCHAR2", "NCHAR"):
        declared, size = f"{kind}({chars})", ("characters", chars)
    elif kind == "RAW":
        declared = f"RAW({length})"
    elif kind.startswith("TIMESTAMP"):
        size = ("fraction", scale)
    fraction = kind.startswith("TIMESTAMP") and kind != "TIMESTAMP(0)"
    binary = collation in (None, "USING_NLS_COMP", "BINARY")
    keyable = cls in render.KEYS and kind not in _UNKEYED and not fraction and binary
    if not binary:  # declared with the type, as DDL writes it (MAX_STRING_SIZE = EXTENDED alone takes one)
        declared += f" COLLATE {collation}"
    says = None if relation == "view" else nullable == "Y"
    return Column(name, declared, cls, readable, cls in ("text", "char"), keyable, kind in _LOBS, says, size)


async def _orders(session: _Session) -> None:
    """§12 P5: outside AL32UTF8 the role must be able to execute the function that orders text keys."""
    try:
        await session.query(_ORDERS)
    except TargetDenied:
        orders = f"UTL_I18N, which orders text keys in a {session.charset} database"
        raise Refusal(f"the role may not execute {orders}: grant it EXECUTE") from None


def _orderable(side: Plan, charset: str) -> None:
    """§12 P5: outside AL32UTF8 a text key is ordered by its UTF-8 bytes, which the server's function
    returns up to 2000 of: a key column declared over 500 is refused."""
    for column in side["keys"]:
        declared = side["declared"][column]
        length = _LENGTH.search(declared)
        if render.family(side["classes"][column]) == "text" and length and int(length[1]) > 500:
            orders = "a scan orders text keys of up to 500 bytes there (compare a view that shortens it)"
            raise Refusal(f"key column {column!r} is {declared} in a {charset} database: {orders}")
