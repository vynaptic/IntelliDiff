"""The engine seam of a table side: what the plan, the per-key connector and the
runner ask of a database, whichever engine it runs, and the errors they tell apart.

An engine's own exceptions never reach the runner with a driver's text: PostgreSQL's stay psycopg's, which
the callers show by type only, and any other engine's become a ``TableError`` holding only a code.
"""

from __future__ import annotations

import re
import socket
import ssl
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.connectors.tables.keys import Plan
from deltasleuth.core.secrets import SecretError, read_secret_bytes
from deltasleuth.core.types import DatabaseTarget, Target

Rows = list[tuple[Any, ...]]  # one fetched batch
Asked = dict[str, list[list[str]]]  # each key's side values per key column
# A per-key row: its key's position, its document's JSON text (or why it has none: a value that does not
# convert, a row past a size limit), and whether the filter selects it.
Row = tuple[int, "str | ValueError", bool]
# A figure a pre-check reads and the column it is of (None: of the rows): rows,
# null_keys; nulls, empty, shortest, longest, sum, min, max, true, distinct.
Figure = tuple[str, "str | None"]


class Session(Protocol):
    """An engine's read-only session on a target."""

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Column:
    name: str
    declared: str  # its type as the catalogue writes it
    cls: str | None  # its class; None: a type no plan reads
    readable: bool  # the role may read it
    likeable: bool  # LIKE can match it
    keyable: bool = True  # its key text is one every engine renders alike
    lob: bool = False  # it may hold a large value: its engine never hashes it
    nullable: bool | None = None  # it may hold NULL; None: its catalogue does not say (a view's column)
    size: tuple[Any, ...] | None = None  # the (n) or (p,s) it declares, after their unit; None: none


# The unit of the (n) or (p,s) a type of the class declares.
_UNITS = {
    "decimal": "digits",
    **dict.fromkeys(("text", "char"), "characters"),
    **dict.fromkeys(("timestamp", "instant", "time"), "fraction"),
}


def sized(cls: str | None, declared: str) -> tuple[Any, ...] | None:
    """The (n) or (p,s) a declared type writes, after their unit (``Column.size``): a decimal's digits and
    its scale (0 when it declares none beside them), a text's characters, a time's fractional digits."""
    found = re.search(r"\(([0-9]+)(?:,(-?[0-9]+))?\)", declared)
    if found is None or cls not in _UNITS:
        return None
    return (_UNITS[cls], int(found[1]), *([int(found[2] or 0)] if cls == "decimal" else []))


@dataclass(frozen=True)
class Relation:
    """A table or view as its catalogue spells it, its live columns in order, and what the session says of
    it and of its server (``facts``: kind, a table or a view; key, its primary key's columns, or None;
    server, consistency, tls, charset, and on Oracle national, its other character set). A file side's has
    no schema: its name is the spec's label."""

    schema: str | None
    name: str
    columns: tuple[Column, ...]
    facts: dict[str, Any]


class Engine(Protocol):
    """One database engine's sessions and statements. ``side`` is a table side of a run's plan."""

    name: str  # postgresql, for one; request_url is <name>://<target_id>/<relation>
    arms: int  # keys per per-key statement
    unfit: tuple[type[BaseException], ...]  # a key the column cannot hold
    fold: Callable[[str], str] | None  # how its catalogue folds an unquoted name; None: any case matches
    hides: bool  # its catalogue lists only the columns the role holds a privilege on
    grant: str  # the privilege a role reads a relation by
    sum_digits: int | None  # the digits its sum of numbers holds; None: every digit
    empty_null: bool  # it stores an empty string as NULL
    retry_alone: bool  # a batch past its timeout is read again key by key: not a file side's

    def built(self, column: str, cls: str, declared: str, key: bool) -> str | None:
        """What its statements read a planned column of this class and declared type as: a key column's key
        text, any other's rendering; None where no statement is built of a declared type. The
        schema guard compares two declared types'."""

    async def connect(self, target: Target, *, timeout_s: float = 0) -> Session:
        """A read-only session; ``timeout_s`` bounds each statement (0: no bound)."""

    def snapshot(self, target: Target, *, timeout_s: float = 0) -> AbstractAsyncContextManager[Session]:
        """A session whose statements all see the same data."""

    async def server(self, session: Session) -> dict[str, Any]:
        """What the session says of its server, which ``ds target check`` alone prints: its full
        ``version``, its ``charset`` and whether the session is encrypted (``tls``)."""

    def release(self, session: Session) -> str:
        """The major and minor version of the session's server, as a plan's ``server``."""

    async def describe(
        self, session: Session, schema: str | None, name: str, *, side: Any = None
    ) -> Relation | None:
        """The relation as the catalogue holds it; None when it is missing or not visible. ``side`` is what a
        file side is described by: its ``SideConfig`` when a run is planned, its
        stored plan afterwards; every other engine ignores it."""

    async def probe(self, session: Session, side: Plan) -> None:
        """Plan and bind the side's key maps and filter, reading no row."""

    def large(self, side: Plan) -> bool:
        """Whether a per-key row of the side may hold a large value: its keys are read few at a time."""

    async def rows(self, session: Session, side: Plan, asked: Asked) -> list[Row]:
        """Each asked key's rows, at most two: its position, the row's document, whether the filter
        selects it."""

    async def scan(self, session: Session, side: Plan, after: Sequence[bytes] | None) -> AsyncIterator[Rows]:
        """The population's (key parts..., digest) in key order after ``after``, in fetched batches; its
        statement is sent before it returns, so both sides of a scan are pinned when they are opened."""

    def census(self, session: Session, side: Plan) -> AsyncIterator[Rows]:
        """The population's (key parts...) in key order, in fetched batches."""

    def frame(self, session: Session, side: Plan) -> AsyncIterator[Rows]:
        """The population's (key parts..., strata JSON) in key order, in fetched batches."""

    async def null_keys(self, session: Session, side: Plan) -> int:
        """Rows of the filtered population that have no key."""

    async def relations(self, session: Session, schema: str | None) -> list[tuple[str, str]]:
        """Each table and view of the schema the role may read, by name: its name and its kind."""

    async def figures(self, session: Session, side: Plan, wanted: Sequence[Figure]) -> list[str | None]:
        """Each figure of ``wanted`` over the side's filtered rows, those without a key among them, in one
        statement that reads no value: a count, a length, or a sum, a minimum or a maximum rendered as
        text."""

    async def duplicates(self, session: Session, side: Plan) -> tuple[int, int]:
        """The keys of the population that several rows hold, and the rows that hold them."""


class TableError(Exception):
    """A table side's failure, holding only a code (``ORA-01555``, ``MySQL 1142``, ``DPY-4024``, or an
    exception's class name): a driver's text may quote a key or a value, so none is kept. ``gloss`` is
    what the code means, where a refused connect says it."""

    def __init__(self, code: str, gloss: str | None = None) -> None:
        super().__init__(code)
        self.gloss = gloss


class KeyUnfit(TableError):
    """A key the column cannot hold."""


class TargetUnreachable(TableError):
    """No server answered, or it answered with an outage's code."""


class StatementTimeout(TargetUnreachable):
    """The statement ran past its timeout."""


class SnapshotLost(TableError):
    """The server can no longer serve the session's snapshot."""


class TargetAuthRefused(TableError):
    """The login was refused: never tried again in a loop."""


class TargetTempSpace(TableError):
    """The server ran out of temporary space."""


class TargetDenied(TableError):
    """The role may not read what the plan reads, or it is gone."""


class TargetRefused(TableError):
    """Any other server code. ``fixed``: a read error of fixed files that did not change: the same bytes
    fail the same way, whatever a pass saved before it met them."""

    fixed = False


class EngineChanged(TableError):
    """The target's engine is no longer the one the run was planned for."""


class RowsMoved(TableError):
    """A file side read to its end met another number of rows than its plan counted: a parser that
    disagrees with itself fails the run, never gives a wrong answer."""


class FilesChanged(TableError):
    """A file side's files are not the ones its run was created on: its code is the
    whole text, naming a file by position, never by name."""


class ReaderDied(TableError):
    """A file side's reader, a process of its own, ended as often as a file set may end it while only that
    side's files were being read: they are read no more until this process restarts.
    Its code is the whole text, naming the end's cause and no file."""


class NotReady(ValueError):
    """A file side's export is not ready to be planned: ``code`` is one of ``NOT_READY``,
    which a schedule's miss records."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


NOT_READY = ("export_absent", "unsettled", "stale", "unstamped", "planning")


class KeysOutOfOrder(ValueError):
    """A side's keys came below their predecessor, or not after the resume bound."""


class Refusal(ValueError):
    """What an engine does not plan, worded without its side: the plan names it."""


UNREACHABLE = (psycopg.OperationalError, TargetUnreachable)
TIMEOUT = (psycopg.errors.QueryCanceled, StatementTimeout)
ERRORS = (psycopg.Error, TableError)  # whatever a database answers a statement with
REFUSED = (psycopg.OperationalError, TableError, UnsafeTargetError)  # a connect, or the guard before it


def code(exc: BaseException) -> str:
    """What an error may show: a table error's code, any other's type."""
    return str(exc) if isinstance(exc, TableError) else type(exc).__name__


def unreachable(exc: BaseException) -> TargetUnreachable:
    """A failure that holds no server's code: a socket's, a resolver's or TLS's, by its class."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return TargetUnreachable("SSLCertVerificationError", "TLS: the server's certificate was not accepted")
    if isinstance(exc, ssl.SSLError):
        return TargetUnreachable("SSLError", "TLS: the handshake failed")
    if isinstance(exc, socket.gaierror):
        return TargetUnreachable("gaierror", "the host name does not resolve")
    answered = not isinstance(exc, ConnectionRefusedError | TimeoutError)
    return TargetUnreachable(type(exc).__name__, None if answered else "no server answered")


KEEPALIVE = (60, 10, 6)  # idle seconds, probe interval, probes: a silently dropped flow ends within 120 s


def keep_alive(sock: Any) -> None:
    """Keepalive probes on a session's socket, the idle time under the platform's name for it."""
    idle = getattr(socket, "TCP_KEEPIDLE", None) or socket.TCP_KEEPALIVE
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in zip((idle, socket.TCP_KEEPINTVL, socket.TCP_KEEPCNT), KEEPALIVE, strict=True):
        sock.setsockopt(socket.IPPROTO_TCP, option, value)


def tls_context(database: DatabaseTarget) -> ssl.SSLContext | None:
    """The TLS an engine target declares, for either driver: the chain verified against its CA, else
    the system's trust store, and the name too unless ``tls_verify_name`` is false; None when it allows no
    encryption. The CA is read now, as a password is: at every connect."""
    if database.allow_unencrypted:
        return None
    ca = None
    if database.tls_ca_ref is not None:
        ca = read_secret_bytes(database.tls_ca_ref).decode("ascii", "replace")
    try:
        if ca is not None and "BEGIN CERTIFICATE" not in ca:  # an empty one would mean the system's store
            raise ssl.SSLError
        context = ssl.create_default_context(cadata=ca)
    except ssl.SSLError:
        raise SecretError(f"secret file {(database.tls_ca_ref or '')[5:]} holds no PEM certificate") from None
    if not database.tls_verify_name:
        # The chain only. A stock MySQL certificate also lacks the key identifiers Python 3.13 insists on.
        context.check_hostname = False
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


# The server each engine was observed on: ``ds target check`` notes any other.
OBSERVED = {"mysql": "8.4", "oracle": "23.26"}


def of(target: Target) -> Engine:
    """The engine of a database target."""
    return ENGINES[target.engine or ""]


def named(side: Plan) -> Engine:
    """The engine a table side was planned for; a plan without ``engine`` is PostgreSQL."""
    return ENGINES[side.get("engine", "postgresql")]


def planned(side: str, plan: Plan, target: Target) -> Engine:
    """``named(plan)``, for a run's side ``side`` on ``target``; refused when the target's engine is no
    longer the one the run was planned for."""
    wanted = plan.get("engine", "postgresql")
    if target.engine != wanted:
        now = f"target {target.target_id} is {target.engine} now"
        raise EngineChanged(f"side {side}: {now}; this run was planned for {wanted}")
    return named(plan)


# Last: an adapter imports this module's types.
from deltasleuth.connectors.postgres import engine as _postgresql  # noqa: E402
from deltasleuth.connectors.tables import duck as _files  # noqa: E402
from deltasleuth.connectors.tables import mysql as _mysql  # noqa: E402
from deltasleuth.connectors.tables import oracle as _oracle  # noqa: E402

ENGINES: dict[str, Engine] = {
    "postgresql": _postgresql.PostgreSQL(),
    "mysql": _mysql.MySQL(),
    "oracle": _oracle.Oracle(),
    "files": _files.Files(),
}
