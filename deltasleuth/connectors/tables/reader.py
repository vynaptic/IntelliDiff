"""The file reader's process: the one place DuckDB is loaded, and the frames it
and its parent speak.

Run by its path (``python -I -X utf8 reader.py``, none of its parent's variables, its spill directory to
work in) it is the child of a ``ds`` process: it imports the standard library and ``duckdb`` alone, the
latter once its first frame has said what to read and it has restricted itself (``restrict``: no core;
on Linux a data limit, proved, and no socket); it builds the locked instance (``connect``), proves
it, then runs one ``SELECT`` a request, each on a thread started for it, and hands the rows back, one
batch read ahead where its parent asks for that. A fault or a memory blow-up of DuckDB's ends this process
and no other. Imported by ``duck``, its parent, it is the
codec and the constants both ends share, and loads no DuckDB.

A frame is a u32 length, a kind byte and a body of that length, integers little-endian. ``J`` is a JSON
object: a request (``REQUEST_MAX``) or an answer (``ANSWER_MAX``). ``R`` is a batch of rows by column
(``FRAME_MAX``, and ``CELLS`` rows times columns). No pickle and no marshal: a row's value crosses as None,
a boolean, an integer, text or bytes, and no other value does. No DuckDB text crosses either (it quotes
paths and values): a failure is answered by its class's name and its kind.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from decimal import Decimal
from typing import Any, BinaryIO, NoReturn

REQUEST_MAX = 64 * 2**20  # a request's body
ANSWER_MAX = 16 * 2**20  # an answer's
FRAME_MAX = 128 * 2**20  # a batch of rows'; the child's is its first frame's
# The cells (rows times columns) a batch of rows holds at most: what its frame makes the parent build is
# bounded by them, whatever its bytes say (a column that is all NULL is one byte). No fewer than the 65,535
# columns a frame can name, so that a row of any width crosses.
CELLS = 2**20
NEEDED = ("parquet", "core_functions")  # compiled into the wheel: nothing is ever installed
# What keeps a file's content between statements, off: the external file cache served a Parquet file's old
# footer for a file replaced at the same size within the same second; the others are off by default.
CACHES = ("enable_external_file_cache", "parquet_metadata_cache", "enable_http_metadata_cache")
GUARD = ("enable_external_access", "lock_configuration", "allowed_directories")  # read back before first use
KINDS = ("memory", "interrupt", "denied", "io", "fatal", "select", "other")  # what a failure is mapped by
# What a failure is named by: the locked DuckDB's exception classes, Python's and this module's own; the
# child answers ``Error`` for any other.
NAMES = frozenset(
    "BinderException CatalogException ConnectionException ConstraintException ConversionException DataError"
    " DatabaseError DependencyException Error FatalException HTTPException IOException IntegrityError"
    " InternalError InternalException InterruptException InvalidInputException InvalidTypeException"
    " NotImplementedException NotSupportedError OperationalError OutOfMemoryException OutOfRangeException"
    " ParserException PermissionException ProgrammingError SequenceException SerializationException"
    " SyntaxException TransactionException TypeMismatchException Warning"
    " MemoryError OSError PermissionError Unframed Oversize".split()
)
LINUX = sys.platform == "linux"  # where a process limits its data and filters its calls
PROBE = 64 * 2**20  # the data limit is proved at this, not at the limit itself
FILTERED = ("aarch64", "x86_64")  # the machines whose socket calls ``FILTER`` knows
# The socket filter, a seccomp program for both machines (so a translated process is judged by the
# architecture the kernel sees): EPERM for socket, socketpair and io_uring_setup, for an x32 call, and for
# any call of another architecture; every other call is allowed. An instruction is its code, the two
# jumps (over that many instructions) and its operand.
_LOAD, _EQUAL, _FROM, _RETURN = 0x20, 0x15, 0x35, 0x06  # ld [k]; jeq k; jge k; ret k
_DENY, _ALLOW = 0x00050001, 0x7FFF0000  # SECCOMP_RET_ERRNO with EPERM; SECCOMP_RET_ALLOW
FILTER = (
    (_LOAD, 0, 0, 4),  # the call's architecture
    (_EQUAL, 2, 0, 0xC00000B7),  # aarch64
    (_EQUAL, 5, 0, 0xC000003E),  # x86_64
    (_RETURN, 0, 0, _DENY),
    (_LOAD, 0, 0, 0),  # aarch64's call number
    (_EQUAL, 8, 0, 198),  # socket
    (_EQUAL, 7, 0, 199),  # socketpair
    (_EQUAL, 6, 5, 425),  # io_uring_setup
    (_LOAD, 0, 0, 0),  # x86_64's
    (_FROM, 4, 0, 0x40000000),  # x32
    (_EQUAL, 3, 0, 41),  # socket
    (_EQUAL, 2, 0, 53),  # socketpair
    (_EQUAL, 1, 0, 425),  # io_uring_setup
    (_RETURN, 0, 0, _ALLOW),
    (_RETURN, 0, 0, _DENY),
)
_HEAD = struct.Struct("<Ic")  # a frame's length and kind
_ROWS = struct.Struct("<IIH")  # a batch's request, rows and columns
_DIGITS = re.compile(rb"[0-9-]*")


class Unframed(Exception):
    """A value no frame carries: neither None, a boolean, an integer, text nor bytes."""


class Oversize(Exception):
    """A message past its limit: a row that alone passes ``FRAME_MAX``, an answer past ``ANSWER_MAX``."""


class NotSelect(Exception):
    """A statement that is not one ``SELECT``."""


class Unguarded(Exception):
    """The instance is not the locked one: ``code`` is ``lacks`` (extensions of ``NEEDED`` that are not
    statically linked), ``unset`` (settings that did not read back as set) or ``probe`` (a read outside the
    roots was not refused); ``names`` are which."""

    def __init__(self, code: str, names: Sequence[str] = ()) -> None:
        super().__init__(code)
        self.code, self.names = code, list(names)


# -- the frames ----------------------------------------------------------------------------


def pack(message: Mapping[str, Any], most: int) -> bytes:
    """A ``J`` frame of ``message``; ``Oversize`` when its body would pass ``most``."""
    body = json.dumps(message, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > most:
        raise Oversize
    return _HEAD.pack(len(body), b"J") + body


def unpack(body: bytes) -> dict[str, Any]:
    """A ``J`` frame's object; ``ValueError`` for a body that is none."""
    found = json.loads(body)
    if not isinstance(found, dict):
        raise ValueError("frame")
    return found


def pack_rows(number: int, rows: Sequence[tuple[Any, ...]], most: int) -> bytes:
    """An ``R`` frame, request ``number``'s answer: ``rows`` by column, each column under the tag of its
    values. ``n``: all NULL, nothing more. ``o``: booleans, a byte a row (2 for NULL). ``s`` text, ``b``
    bytes and ``i`` integers as their decimal text: the bytes of the values joined, an i32 length a row (-1
    for NULL; of text in characters), then the values joined (text as UTF-8). ``Unframed`` for a value of
    another type or a column of two; ``Oversize`` when the body would pass ``most``."""
    body = [_ROWS.pack(number, len(rows), len(rows[0]) if rows else 0)]
    for column in zip(*rows, strict=True):
        kinds = set(map(type, column)) - {type(None)}
        if not kinds:
            body.append(b"n")
            continue
        kind = kinds.pop()
        if kinds or kind not in (bool, int, str, bytes):
            raise Unframed
        if kind is bool:
            body.append(b"o" + bytes(2 if value is None else value for value in column))
            continue
        values = [None if value is None else str(value) for value in column] if kind is int else column
        held = [value for value in values if value is not None]
        joined = b"".join(held) if kind is bytes else "".join(held).encode()
        lengths = [-1 if value is None else len(value) for value in values]
        tag = b"b" if kind is bytes else b"i" if kind is int else b"s"
        body += [tag, struct.pack(f"<I{len(lengths)}i", len(joined), *lengths), joined]
    data = b"".join(body)
    if len(data) > most:
        raise Oversize
    return _HEAD.pack(len(data), b"R") + data


def unpack_rows(
    body: bytes, most: int, width: int | None = None, tags: str | None = None
) -> tuple[int, list[tuple[Any, ...]]]:
    """An ``R`` frame's request and rows, ``most`` rows and ``CELLS`` cells at most, judged before a row is
    built; with ``width``, rows of that many columns; with ``tags``, of as many columns as it has tags,
    each under its tag there or all NULL. ``ValueError`` (``struct``'s error among them) else, and for a
    body that is not one whole: an unknown tag, a length under -1, lengths that do not sum to their values,
    text that is not UTF-8, an integer of other characters, a byte left over."""
    number, count, wide = _ROWS.unpack_from(body)
    if count > most or count * wide > CELLS:
        raise ValueError("rows")
    if count and wide != ((len(tags) if tags else width) or wide or -1):  # or rows without a column
        raise ValueError("width")
    at, columns = _ROWS.size, []
    for n in range(wide):
        tag, at = body[at : at + 1], at + 1
        if tags and count and tag not in (b"n", tags[n].encode()):
            raise ValueError("tags")
        if tag == b"n":
            columns.append([None] * count)
            continue
        if tag == b"o":
            flags, at = body[at : at + count], at + count
            if len(flags) != count or max(flags, default=0) > 2:
                raise ValueError("booleans")
            columns.append([None if flag == 2 else flag == 1 for flag in flags])
            continue
        if tag not in (b"s", b"b", b"i"):
            raise ValueError("tag")
        size, *lengths = struct.unpack_from(f"<I{count}i", body, at)
        at += 4 + 4 * count
        joined, at = body[at : at + size], at + size
        if len(joined) != size or min(lengths, default=0) < -1:
            raise ValueError("values")
        if tag == b"i" and not _DIGITS.fullmatch(joined):
            raise ValueError("integers")
        text: Any = joined if tag == b"b" else joined.decode()
        values, start = [], 0
        for length in lengths:
            values.append(None if length < 0 else text[start : start + length])
            start += max(length, 0)
        if start != len(text):
            raise ValueError("lengths")
        columns.append([None if value is None else int(value) for value in values] if tag == b"i" else values)
    if at != len(body):
        raise ValueError("left over")
    return number, list(zip(*columns, strict=True))


def read(source: BinaryIO, limits: Mapping[bytes, int]) -> tuple[bytes, bytes] | None:
    """``source``'s next frame, its kind and body; None at its end, a frame cut short too. ``ValueError``
    for a kind that is not in ``limits`` or a body past its kind's limit, which is not read."""
    head = source.read(_HEAD.size)
    if len(head) < _HEAD.size:
        return None
    size, kind = _HEAD.unpack(head)
    if size > limits.get(kind, -1):
        raise ValueError("frame")
    body = source.read(size)
    return (kind, body) if len(body) == size else None


def bind(value: Any) -> list[Any]:
    """A statement's bound value as a request carries it, under its tag: ``n`` null, ``o`` a boolean, ``i``
    an integer, ``s`` text, ``b`` bytes in hexadecimal, ``d`` a Decimal's text, ``l`` a list of texts,
    ``m`` a map of text to text; ``Unframed`` for any other."""
    if value is None:
        return ["n", None]
    if isinstance(value, bool | int | str):  # a boolean is an integer too: told apart first
        return ["o" if isinstance(value, bool) else "i" if isinstance(value, int) else "s", value]
    if isinstance(value, bytes):
        return ["b", value.hex()]
    if isinstance(value, Decimal):
        return ["d", str(value)]
    if isinstance(value, list | tuple) and all(isinstance(text, str) for text in value):
        return ["l", list(value)]
    if isinstance(value, dict) and all(isinstance(text, str) for item in value.items() for text in item):
        return ["m", value]
    raise Unframed


def bound(tag: str, value: Any) -> Any:
    """The value a request's bind carries (``bind``'s inverse)."""
    return bytes.fromhex(value) if tag == "b" else Decimal(value) if tag == "d" else value


# -- what the child denies itself ------------------------------------------------------------


def restrict(limit: int | None, filtered: bool) -> tuple[int | None, bool]:
    """Before DuckDB loads, each step best effort: no core, which would hold rows in clear; on Linux the
    kernel's memory killer takes this process first, and it is not dumpable (a piped ``core_pattern``
    ignores the core limit), in that order, the adjustment being refused afterwards; with ``limit`` (MiB)
    a data limit; with ``filtered``, on a machine of ``FILTERED``, no socket. What this start saw hold:
    the data limit, else None, and whether sockets are denied."""
    import resource

    with suppress(OSError, ValueError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if not LINUX:
        return None, False
    with suppress(OSError), open("/proc/self/oom_score_adj", "w") as adjusted:
        adjusted.write("1000")
    with suppress(Exception):
        _prctl(4, 0)  # PR_SET_DUMPABLE
    held, denied = limit is not None and _limited(limit * 2**20), False
    with suppress(Exception):  # no prctl to call: nothing was loaded
        denied = filtered and os.uname().machine in FILTERED and _filtered()
    return limit if held else None, denied


def _prctl(option: int, *values: int) -> None:
    """``prctl(2)``, which the standard library reaches through ``ctypes`` alone."""
    import ctypes

    call = ctypes.CDLL(None, use_errno=True).prctl
    call.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    call(option, *values, *[0] * (4 - len(values)))


def _limited(limit: int) -> bool:
    """Set the data limit, ``limit`` bytes, which cannot be raised again, and prove it: with the soft limit
    at ``PROBE`` a private anonymous mapping of twice that, never touched, must be refused (Python's
    ``mmap`` is shared by default, which the limit does not count), and with the soft limit raised to the
    hard one the same mapping must be made. A kernel before 4.7 counts ``brk`` alone and one booted with
    ``ignore_rlimit_data`` only warns: not held. Proved at ``PROBE``: a mapping of the limit itself may be
    refused for the host's own memory, with nothing limited."""
    import mmap
    import resource

    def mapped() -> bool:
        try:
            mmap.mmap(-1, 2 * PROBE, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS).close()
        except OSError:  # ENOMEM
            return False
        return True

    try:
        resource.setrlimit(resource.RLIMIT_DATA, (PROBE, limit))
    except (OSError, ValueError):  # the host's own hard limit lies under this one: not this start's
        return False
    refused = not mapped()
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    return refused and mapped()


def _filtered() -> bool:
    """Load ``FILTER`` for this thread and whatever it starts, DuckDB's threads among them; held when a
    socket is refused."""
    import ctypes
    import socket

    steps = ctypes.create_string_buffer(b"".join(struct.pack("=HBBI", *step) for step in FILTER))
    program = ctypes.create_string_buffer(struct.pack("@HP", len(FILTER), ctypes.addressof(steps)))
    _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS, without which a process with no capability loads no filter
    _prctl(22, 2, ctypes.addressof(program))  # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
    try:
        socket.socket().close()
    except PermissionError:
        return True
    return False


# -- the locked instance, in the child ------------------------------------------------------


def connect(spill: str, roots: Sequence[str], memory_mib: int, spill_mib: int, threads: int) -> Any:
    """A locked connection that reads ``roots`` and spills under ``spill``, the directory its
    parent holds for it; ``Unguarded`` when the parquet reader is not compiled in.
    ``max_temp_directory_size`` is set by a statement: given in ``connect``'s configuration it reads back
    and is not enforced (observed on 1.5.5). The extensions are read before the lock: after it
    ``duckdb_extensions()`` needs the extension directory, which lies outside every allowed one, so that
    ``INSTALL`` is refused; and before the caches are set, one of which is the Parquet reader's own."""
    import duckdb

    config = {
        "threads": threads, "memory_limit": f"{memory_mib}MiB", "temp_directory": os.path.join(spill, "temp"),
        "home_directory": os.path.join(spill, "home"),
        "extension_directory": os.path.join(spill, "extensions"),
        "secret_directory": os.path.join(spill, "secrets"), "autoinstall_known_extensions": False,
        "autoload_known_extensions": False, "allow_community_extensions": False,
        "allow_unsigned_extensions": False, "allow_persistent_secrets": False,
        "python_enable_replacements": False, "default_collation": "",
    }  # fmt: skip
    con = duckdb.connect(":memory:", config=config)
    con.execute("SET max_temp_directory_size = $size", {"size": f"{spill_mib}MiB"})
    found = con.execute("SELECT extension_name, loaded, install_mode FROM duckdb_extensions()").fetchall()
    linked = {name for name, loaded, mode in found if loaded and mode == "STATICALLY_LINKED"}
    if lacking := sorted(set(NEEDED) - linked):
        con.close()
        raise Unguarded("lacks", lacking)
    # By statements, the Parquet reader's being no option of ``connect``'s configuration; and after the
    # extensions were read, since a build that lacks that reader does not know its setting (above).
    for cache in CACHES:
        con.execute(f"SET {cache} = false")
    con.execute("SET allowed_directories = $roots", {"roots": [root.rstrip("/") + "/" for root in roots]})
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def prove(con: Any, roots: Sequence[str], probe: str) -> None:
    """Before first use: the three settings of the guard and the caches' read back, and a
    file outside the roots (``probe``) is refused; ``Unguarded`` else. The spill's free space is the
    parent's to check."""
    import duckdb

    names = (*GUARD, *CACHES)
    read = con.execute(
        "SELECT current_setting('enable_external_access'), current_setting('lock_configuration'),"
        " current_setting('allowed_directories'), "
        + ", ".join(f"current_setting('{name}')" for name in CACHES)
    ).fetchone()
    allowed = {root.rstrip("/") + "/" for root in roots}
    held = (
        (False,) * len(names)
        if read is None
        else (read[0] is False, read[1] is True, allowed <= set(read[2]), *(off is False for off in read[3:]))
    )
    if not all(held):
        raise Unguarded("unset", [name for name, ok in zip(names, held, strict=True) if not ok])
    try:
        con.execute("SELECT size FROM read_blob($probe)", {"probe": probe}).fetchall()
    except duckdb.PermissionException:
        pass
    else:
        raise Unguarded("probe")


def select(cursor: Any, statement: str, values: Any) -> None:
    """Send one ``SELECT`` with its bound ``values`` (DuckDB itself runs ``ATTACH``, ``COPY TO`` and
    ``EXPORT`` into an allowed directory, so a cursor runs no other statement); ``NotSelect`` else."""
    import duckdb

    found = cursor.extract_statements(statement)
    if len(found) != 1 or found[0].type != duckdb.StatementType.SELECT:
        raise NotSelect
    cursor.execute(statement, values)


def refusal(exc: BaseException) -> tuple[str, str]:
    """A failure's name (of ``NAMES``; ``Error`` for any other) and its kind, by the
    class that was raised and never its text."""
    import duckdb

    name = type(exc).__name__
    if isinstance(exc, NotSelect):
        kind = "select"
    elif isinstance(exc, duckdb.OutOfMemoryException | MemoryError):
        kind = "memory"  # the memory limit, or the spill at its limit (observed)
    elif isinstance(exc, duckdb.InterruptException):
        kind = "interrupt"
    elif isinstance(exc, duckdb.PermissionException | PermissionError):
        kind = "denied"
    elif isinstance(exc, duckdb.IOException | OSError):
        kind = "io"
    elif isinstance(exc, duckdb.FatalException | duckdb.InternalException):
        kind = "fatal"  # the database is invalidated
    else:
        kind = "other"
    return name if name in NAMES else "Error", kind


# -- the child's loop ------------------------------------------------------------------------


class _Held:
    """A cursor the child holds under its parent's number: opened by its first request, one request of it
    at a time (``lock``), the rows a frame had no room for (``rest``), and whether to close it (``closed``)
    once no request of it is in flight. ``ahead``: the batch read ahead (``_ahead``), with the count of its
    parent's interrupts (``drops``) it was read under: one more since drops it. ``done``: set once the
    request that runs, or ran last, has returned; ``stopped``: the one being interrupted until it has,
    each interrupt under ``halting``, as ``done`` is set: none reaches the request after it."""

    def __init__(self) -> None:
        self.cursor: Any = None
        self.lock, self.rest, self.closed = threading.Lock(), [], False
        self.ahead: tuple[Any, ...] | None = None
        self.drops = 0
        self.done, self.halting = threading.Event(), threading.Lock()
        self.done.set()
        self.stopped: threading.Event | None = None


def _execute(held: _Held, request: Mapping[str, Any]) -> bytes:
    binds, held.rest, held.ahead = request["binds"], [], None
    values = None if binds is None else {name: bound(*value) for name, value in binds.items()}
    select(held.cursor, request["statement"], values)
    return pack({"id": request["id"], "ok": True}, ANSWER_MAX)


def _batch(held: _Held, request: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    """The next ``rows`` rows or ``bytes`` of their texts and bytes, whichever comes first, fetched ``step``
    rows at a time, those an earlier frame had no room for first (``rows`` of them at most: no answer holds
    more than was asked). No batch holds over ``CELLS`` cells: of a statement so wide that ``rows`` rows
    would, it holds the rows that do not (one at least), and a step's rows past them wait for the next
    fetch."""
    cells = max(CELLS // len(held.cursor.description or "."), 1)
    most = min(request["rows"], cells)
    rows, held.rest = held.rest[:most], held.rest[most:]
    size = sum(len(value) for row in rows for value in row if isinstance(value, str | bytes))
    while len(rows) < most and size < request["bytes"]:
        found = held.cursor.fetchmany(request["step"])
        if not found:
            break
        rows += found
        size += sum(len(value) for row in found for value in row if isinstance(value, str | bytes))
    if len(rows) > cells:
        rows, held.rest = rows[:cells], rows[cells:] + held.rest
    return rows


def _fetch(held: _Held, request: Mapping[str, Any]) -> bytes:
    """The next batch (``_batch``): the one read ahead where it was read for a fetch of these sizes and no
    interrupt came since (its error, had it met one; after an interrupt ``InterruptException``: the
    statement was interrupted, and no row is passed over in silence). A batch over a frame crosses as its
    longest prefix of whole rows that fits, the rest kept for the cursor's next fetch, before whatever is
    kept already; ``Oversize`` for a row that alone passes a frame. No row: the statement's end."""
    ahead, held.ahead = held.ahead, None
    if ahead is not None:
        import duckdb

        sizes, drops, rows, frame = ahead
        if drops != held.drops:
            raise duckdb.InterruptException
        if isinstance(frame, BaseException):
            raise frame
        if sizes == (request["rows"], request["step"], request["bytes"]):
            return frame[: _HEAD.size] + struct.pack("<I", request["id"]) + frame[_HEAD.size + 4 :]
        held.rest = rows + held.rest  # a fetch of other sizes takes them first
    rows = _batch(held, request)
    try:
        return pack_rows(request["id"], rows, FRAME_MAX)
    except Oversize:
        fits, over = 0, len(rows)  # a prefix of ``fits`` rows fits a frame, one of ``over`` does not
    while over - fits > 1:
        half = (fits + over) // 2
        try:
            pack_rows(request["id"], rows[:half], FRAME_MAX)
        except Oversize:
            over = half
        else:
            fits = half
    if not fits:
        raise Oversize
    held.rest = rows[fits:] + held.rest
    return pack_rows(request["id"], rows[:fits], FRAME_MAX)


def _columns(held: _Held, request: Mapping[str, Any]) -> bytes:
    """The sent statement's columns: each one's name and the type DuckDB reads it as."""
    found = [[name, str(kind)] for name, kind, *_ in held.cursor.description or ()]
    return pack({"id": request["id"], "columns": found}, ANSWER_MAX)


def _ahead(held: _Held, request: Mapping[str, Any]) -> None:
    """After a full batch, the next one fetched and packed on the same thread while the parent works on
    the one just sent, and held for the cursor's next fetch; an error met here answers that fetch. One a
    frame has no room for is kept as rows, and crosses as any such batch does."""
    drops, rows = held.drops, []
    try:
        rows = _batch(held, request)
        frame: Any = pack_rows(0, rows, FRAME_MAX)
    except Oversize:
        held.rest = rows + held.rest
        return
    except Exception as exc:
        frame = exc
    held.ahead = (request["rows"], request["step"], request["bytes"]), drops, rows, frame


_OPS = {"execute": _execute, "fetch": _fetch, "columns": _columns}


def _answer(
    send: Callable[[bytes], None], con: Any, held: _Held, request: Mapping[str, Any], drops: int
) -> None:
    """One request of a cursor, on the thread started for it: its answer, or its failure's name and kind.
    One whose cursor was closed or interrupted before it began (``drops``: the cursor's interrupts when
    the request was read) is not run: nothing goes on for nobody. A fetch that asks for it and is answered
    a full batch, ``rows`` rows or a step's more, reads the next one ahead (``_ahead``), its answer sent
    first."""
    import duckdb

    op = _OPS[request["op"]]
    with held.lock:
        held.done = done = threading.Event()  # before the interrupts are read: a later one finds it
        try:
            if held.cursor is None:
                held.cursor = con.cursor()
            if held.closed or held.drops != drops:
                raise duckdb.InterruptException
            frame = op(held, request)
        except Exception as exc:
            name, kind = refusal(exc)
            frame = pack({"id": request["id"], "error": name, "kind": kind}, ANSWER_MAX)
        full = frame[4:5] == b"R" and _ROWS.unpack_from(frame, _HEAD.size)[1] >= request["rows"]
        if full and request.get("ahead") and not held.closed:
            send(frame)
            frame = b""
            _ahead(held, request)
        with held.halting:
            done.set()
    _closed(held)
    if frame:
        send(frame)


def _interrupted(held: _Held, done: threading.Event) -> None:
    """Interrupt a cursor's request that runs, again and again until it has returned (``done``): DuckDB
    forgets an interrupt that reaches it before it began the statement, while it binds and prepares it
    (observed on 1.5.5), and the statement would run to its end for nobody."""
    while not done.is_set():
        with held.halting, suppress(Exception):  # no cursor yet
            if not done.is_set():
                held.cursor.interrupt()
        done.wait(0.01)


def _closed(held: _Held) -> None:
    """Close a cursor its parent closed once no request of it is in flight: at once, or by the request that
    was, as it returns. Its rejects tables go with it."""
    if held.closed and held.lock.acquire(blocking=False):
        try:
            cursor, held.cursor = held.cursor, None
            if cursor is not None:
                cursor.close()
        except Exception:  # noqa: S110 - an invalidated database's: nothing waits for a close's answer
            pass
        finally:
            held.lock.release()


def _fault(send: Callable[[bytes], None], exc: BaseException) -> NoReturn:
    """End the process (exit 1) after a last frame naming ``exc``'s class and the line of this module it
    passed last."""
    line, trace = 0, exc.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == __file__:
            line = trace.tb_lineno
        trace = trace.tb_next
    try:
        send(pack({"fault": type(exc).__name__, "line": line}, ANSWER_MAX))
    finally:
        os._exit(1)


def _guarded(call: Callable[..., None], send: Callable[[bytes], None], *args: Any) -> None:
    try:
        call(send, *args)
    except BaseException as exc:
        _fault(send, exc)


def _serve(send: Callable[[bytes], None], source: BinaryIO, inherited: int) -> None:
    """The first frame's restrictions, then its instance, connected and proved (``ready`` with what this
    start saw hold, or ``unstarted`` with what did not), then the requests until the input ends. This
    thread only reads: an interrupt and a close take no answer (each drops what its cursor read ahead; an
    interrupt stops the cursor's request that runs, on a thread of its own until it has returned, and
    those read before it that have not begun are not run), a ping is answered at once, and every other
    request runs on a thread started for it, so an idle cursor holds none."""
    global FRAME_MAX
    first = read(source, {b"J": REQUEST_MAX})
    if first is None:
        return
    asked = unpack(first[1])
    FRAME_MAX = asked["frame_max"]
    limit, sockets = restrict(asked["limit"], asked["filter"])
    import duckdb

    try:
        con = connect(asked["spill"], asked["roots"], asked["memory"], asked["spilled"], asked["threads"])
        prove(con, asked["roots"], asked["probe"])
    except Unguarded as exc:
        send(pack({"unstarted": exc.code, "names": exc.names}, ANSWER_MAX))
        return
    ready = {"ready": True, "version": duckdb.__version__, "limit": limit, "sockets": sockets}
    send(pack(ready | {"inherited": inherited}, ANSWER_MAX))
    cursors: dict[int, _Held] = {}
    while (frame := read(source, {b"J": REQUEST_MAX})) is not None:
        request = unpack(frame[1])
        if request["op"] == "ping":
            send(pack({"id": request["id"], "ok": True}, ANSWER_MAX))
        elif request["op"] == "interrupt":
            held = cursors.get(request["cursor"])
            if held is not None:
                held.drops += 1  # what was read ahead is dropped, and a request not begun is not run
                if not (done := held.done).is_set() and held.stopped is not done:
                    held.stopped = done
                    threading.Thread(target=_interrupted, args=(held, done), daemon=True).start()
        elif request["op"] == "close":
            if (held := cursors.pop(request["cursor"], None)) is not None:
                held.closed = True
                _closed(held)
        else:
            held = cursors.get(request["cursor"]) or cursors.setdefault(request["cursor"], _Held())
            answering = (_answer, send, con, held, request, held.drops)
            threading.Thread(target=_guarded, args=answering, daemon=True).start()


def main() -> None:
    """The child: the descriptors it holds beyond its three and its listing's own counted; its frames on
    duplicates of its input and output, which then are ``/dev/null`` and its error output, so a stray write
    of native code forges no frame. Its input's end is its own (exit 0), mid-statement too; an exception of
    this module's that nothing caught ends it with exit 1 after a last frame (``fault``)."""
    inherited = len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd")) - 4
    source, sink = os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb")
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    sending = threading.Lock()

    def send(frame: bytes) -> None:
        with sending:
            sink.write(frame)
            sink.flush()

    _guarded(_serve, send, source, inherited)
    os._exit(0)


if __name__ == "__main__":
    main()
