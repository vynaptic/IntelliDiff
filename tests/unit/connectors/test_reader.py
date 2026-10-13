"""The file reader in a process of its own: its frames, what an
answer outside the protocol costs (one that would have this process build more than it may, or word a
reader's text, among them), its start with nothing of this process's, a start that fails, a reader
replaced or killed, that no process but the reader loads DuckDB; what it denies itself, and what its start
says of that; a statement given up and a process that gives no sign of life, killed; its end learnt from
its process; and its ends accounted: charged to the one file set in flight, which is read apart and after
three such ends no more, while sets in flight together are read apart once. A reader is ended here by the
signals of ``tests/support/faults.py``: none that is a crash report on macOS, unless ``DS_FAULTS=1``."""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import os
import platform
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import tracemalloc
from collections import Counter
from contextlib import suppress
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import StatementTimeout, TargetRefused, duck, manifest, reader
from deltasleuth.core.spec import TargetRegistry, compile_spec
from deltasleuth.runner.cycle import failure
from tests.support import crafted, faults, fuzzed
from tests.unit.connectors import ducked
from tests.unit.connectors.ducked import csv_table, file_spec, planned, read, registry, scanned, stand_in

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "tests" / "golden" / "engines"
TEXT = st.text(st.characters(exclude_categories=["Cs"]), max_size=8)
WHOLE = st.integers(-(10**39), 10**39)
VALUES = {"s": TEXT, "b": st.binary(max_size=8), "i": WHOLE, "o": st.booleans(), "n": st.none()}
BOUND = st.one_of(
    st.none(), st.booleans(), WHOLE, TEXT, st.binary(max_size=8), st.lists(TEXT, max_size=3),
    st.decimals(allow_nan=False, allow_infinity=False), st.dictionaries(TEXT, TEXT, max_size=3),
)  # fmt: skip
EDGES = [("\x00", b"\x00", 10**38, True), ("\U0010fffd😀", b"", -(10**38), None), ("", None, None, False)]


@st.composite
def _batches(draw: st.DrawFn) -> list[tuple[Any, ...]]:
    """A batch as DuckDB hands one over: each column of one type, NULLs among its values."""
    kinds, count = draw(st.lists(st.sampled_from("sbion"), min_size=1, max_size=5)), draw(st.integers(0, 12))
    held = [st.lists(st.one_of(st.none(), VALUES[kind]), min_size=count, max_size=count) for kind in kinds]
    return list(zip(*(draw(column) for column in held), strict=True))


def _typed(value: Any) -> Any:
    """A value with its type, and its members': True is not 1 here."""
    members = value.items() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return type(value), str(value), [_typed(member) for member in members]


@settings(max_examples=200, deadline=None)
@given(st.integers(0, 2**32 - 1), _batches(), st.dictionaries(TEXT, BOUND, max_size=4))
@example(7, EDGES, {"a": Decimal("1.50"), "b": b"\x00\xff", "c": ["x"], "d": {"k": "v"}, "e": 10**38})
def test_frames_carry_rows_as_fetched(
    number: int, rows: list[tuple[Any, ...]], binds: dict[str, Any]
) -> None:
    """A batch crosses as it was fetched, whatever its values: NUL, a character beyond the BMP, the
    empty text, NULLs, an integer of 39 digits; and a request's binds under every tag."""
    frame = reader.pack_rows(number, rows, reader.FRAME_MAX)
    kind, body = reader.read(io.BytesIO(frame), {b"R": len(frame)}) or (b"", b"")
    assert kind == b"R" and _typed(list(reader.unpack_rows(body, len(rows)))) == _typed([number, rows])
    request = {
        "id": number,
        "op": "execute",
        "binds": {name: reader.bind(value) for name, value in binds.items()},
    }
    kind, body = reader.read(io.BytesIO(reader.pack(request, reader.REQUEST_MAX)), {b"J": 2**20}) or (
        b"",
        b"",
    )
    carried = {name: reader.bound(*bind) for name, bind in reader.unpack(body)["binds"].items()}
    assert kind == b"J" and _typed(carried) == _typed(binds)


def test_a_frame_carries_no_other_value() -> None:
    """No pickle, no marshal: a value that is not None, a boolean, an integer, text or bytes is refused
    where it would be framed, as is a column of two types, a message past its limit and a frame past its
    kind's; and a frame cut short is its source's end."""
    for rows in ([(1.5,)], [(Decimal(1),)], [([1],)], [(1,), ("1",)], [(True,), (1,)]):
        with pytest.raises(reader.Unframed):
            reader.pack_rows(1, rows, reader.FRAME_MAX)
    for value in (1.5, object(), [1], {"k": 1}, {1: "v"}):
        with pytest.raises(reader.Unframed):
            reader.bind(value)
    frame = reader.pack_rows(1, [("abc",)], reader.FRAME_MAX)
    with pytest.raises(reader.Oversize):
        reader.pack_rows(1, [("abc",)], len(frame) - 6)
    with pytest.raises(reader.Oversize):
        reader.pack({"statement": "x" * 100}, 100)
    with pytest.raises(ValueError, match="frame"):
        reader.read(io.BytesIO(frame), {b"R": len(frame) - 6})
    with pytest.raises(ValueError, match="frame"):
        reader.read(io.BytesIO(frame), {b"J": 2**20})
    assert reader.read(io.BytesIO(frame[:-1]), {b"R": 2**20}) is None
    assert reader.read(io.BytesIO(frame[:3]), {b"R": 2**20}) is None
    with pytest.raises(ValueError, match="rows"):
        reader.unpack_rows(frame[5:], 0)


def test_the_error_names_are_duckdb_s() -> None:
    """A failure crosses as its class's name and a kind, never its text: the names are the locked
    DuckDB's exception classes and five more, any other class is ``Error``, and the kinds are the closed
    set."""
    import duckdb

    classes = {
        name: found
        for name, found in vars(duckdb).items()
        if isinstance(found, type) and issubclass(found, Exception)
    }
    own = {"MemoryError", "OSError", "PermissionError", "Unframed", "Oversize"}
    assert len(classes) == 32 and reader.NAMES == set(classes) | own
    kinds = {name: reader.refusal(found("/a/path and a value")) for name, found in classes.items()}
    assert all(found == name and kind in reader.KINDS for name, (found, kind) in kinds.items())
    others = {name: kind for name, (_, kind) in kinds.items() if kind != "other"}
    assert others == {
        "OutOfMemoryException": "memory", "InterruptException": "interrupt", "PermissionException": "denied",
        "IOException": "io", "HTTPException": "io", "FatalException": "fatal", "InternalException": "fatal",
    }  # fmt: skip
    python = (MemoryError(), PermissionError(), OSError(), FileNotFoundError(), KeyError("k"))
    assert [reader.refusal(exc) for exc in python] == [
        ("MemoryError", "memory"), ("PermissionError", "denied"), ("OSError", "io"), ("Error", "io"),
        ("Error", "other"),
    ]  # fmt: skip
    assert [reader.refusal(exc) for exc in (reader.NotSelect(), reader.Unframed(), reader.Oversize())] == [
        ("Error", "select"), ("Unframed", "other"), ("Oversize", "other"),
    ]  # fmt: skip


# --- what an answer outside the protocol costs ---------------------------------------------------------

# A process that speaks the reader's frames and nothing of DuckDB: its first answer, then one a request
# (``n``: how many it has answered).
FAKE = """
import itertools, os, struct
answered = itertools.count()
source, sink = os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb")
def send(frame):
    sink.write(frame)
    sink.flush()
def J(**message):
    return reader.pack(message, 2**30)
def raw(kind, body):
    return struct.pack("<Ic", len(body), kind) + body
def R(request, rows, body):
    return raw(b"R", struct.pack("<IIH", request["id"], rows, 1) + body)
def ok(request):
    return J(id=request["id"], ok=True)
READY = dict(ready=True, version="1.5.5", limit=None, sockets=False, inherited=0)
reader.read(source, {{b"J": reader.REQUEST_MAX}})
send({first})
while (frame := reader.read(source, {{b"J": reader.REQUEST_MAX}})) is not None:
    request = reader.unpack(frame[1])
    if "id" in request:
        n = next(answered)
        send({answer})
"""
READY = "J(**READY)"
STATEMENT = {  # what a statement is answered with
    "an over-long frame": 'struct.pack("<Ic", reader.ANSWER_MAX + 1, b"J")',
    "a frame of no kind": 'raw(b"X", b"SECRET")',
    "no JSON": 'raw(b"J", b"SECRET")',
    "no object": 'raw(b"J", b\'["SECRET"]\')',
    "a nesting too deep": 'raw(b"J", b"[" * 200000)',
    "an unknown id": 'J(id=request["id"] + 7, ok=True)',
    "an id of another type": 'J(id=float(request["id"]), ok=True)',
    "a name outside NAMES": 'J(id=request["id"], error="SECRET /a/path", kind="other")',
    "a kind outside KINDS": 'J(id=request["id"], error="IOException", kind="SECRET")',
    "a member of another type": 'J(id=request["id"], ok="SECRET")',
    "a member more": 'J(id=request["id"], ok=True, more="SECRET")',
    "another request's answer": 'J(id=request["id"], columns=[["SECRET", "VARCHAR"]])',
    "rows for a statement": 'R(request, 0, b"n")',
    "a fault of other characters": 'J(fault="SECRET class", line=1)',
    "a fault's line of another type": 'J(fault="KeyError", line="SECRET")',
    "a second ready": READY,
}
FETCH = {  # what a fetch of one row is answered with, its statement taken
    "too many rows": 'R(request, 3, b"n")',
    "lengths that do not sum": 'R(request, 1, b"s" + struct.pack("<Ii", 6, 5) + b"SECRET")',
    "a length under -1": 'R(request, 1, b"s" + struct.pack("<Ii", 0, -2))',
    "a length past its values": 'R(request, 1, b"s" + struct.pack("<Ii", 60, 6) + b"SECRET")',
    "invalid UTF-8": 'R(request, 1, b"s" + struct.pack("<Ii", 1, 1) + bytes([255]))',
    "an unknown tag": 'R(request, 1, b"x")',
    "a byte left over": 'R(request, 1, b"n" + b"S")',
    "an integer of other characters": 'R(request, 1, b"i" + struct.pack("<Ii", 2, 2) + b"1_")',
    "a boolean of another byte": 'R(request, 1, b"o" + bytes([3]))',
    "rows without a column": 'raw(b"R", struct.pack("<IIH", request["id"], 1, 0))',
    "an answer that is no rows": "ok(request)",
    "rows of another width than its statement's": (
        'R(request, 1, b"n") if n == 1 else raw(b"R", struct.pack("<IIH", request["id"], 1, 2) + b"nn")'
    ),
}
COLUMNS = {  # what a statement's columns are answered with, a row of one column and its end fetched
    "a type of other characters": 'J(id=request["id"], columns=[["id", "SECRET value\\nof an export"]])',
    "a type that is no text": 'J(id=request["id"], columns=[["id", 7]])',
    "a name that is no text": 'J(id=request["id"], columns=[[7, "BIGINT"]])',
    "more columns than its rows hold": 'J(id=request["id"], columns=[["id", "BIGINT"], ["v", "VARCHAR"]])',
}
ANSWERS = (
    STATEMENT
    | {name: f'ok(request) if request["op"] == "execute" else {bad}' for name, bad in FETCH.items()}
    | {
        name: f'ok(request) if n == 0 else R(request, 2 - n, b"n") if request["op"] == "fetch" else {bad}'
        for name, bad in COLUMNS.items()
    }
)
FIRST = {  # what the start is answered with
    "a version of other characters": 'J(**READY | {"version": "1.5.5 SECRET"})',
    "a version too long": 'J(**READY | {"version": "1" * 33})',
    "a limit of another type": 'J(**READY | {"limit": "SECRET"})',
    "a limit that was not asked for": 'J(**READY | {"limit": 5296})',
    "a filter that was not asked for": 'J(**READY | {"sockets": True})',
    "a count of another type": 'J(**READY | {"inherited": True})',
    "a member more": 'J(**READY, more="SECRET")',
    "names that are not this process's": 'J(unstarted="lacks", names=["SECRET"])',
    "a check that is none": 'J(unstarted="SECRET", names=[])',
    "an answer to no request": "J(id=1, ok=True)",
    "an over-long frame": 'struct.pack("<Ic", reader.ANSWER_MAX + 1, b"J")',
}


@pytest.mark.parametrize("answer", ANSWERS.values(), ids=ANSWERS.keys())
async def test_a_reply_not_of_the_protocol_ends_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, answer: str
) -> None:
    """Every answer is checked, by its length, its kind, the request it names and each member's type,
    its rows by their statement's width and its columns' types by a grammar of this process's; one that is
    not of the protocol, and any exception raised while it is decoded, is the reader's end (``reply``): it
    is killed, what waited on it and what is sent to it later is Busy('Died'), and nothing it sent is in a
    log line: the end is logged by its cause alone."""
    stand_in(tmp_path, monkeypatch, FAKE.format(first=READY, answer=answer))
    with ducked.started(tmp_path) as made:
        cursor = made.cursor()
        with pytest.raises(manifest.Busy) as died:
            await cursor.execute("SELECT 1")
            await cursor.fetch(1)
            await cursor.fetch(1)
            await cursor.columns()
        made._ending.join(5)
        assert str(died.value) == "Died" and made.cause == "reply" and made.dropped
        assert made.proc.returncode == -signal.SIGKILL and not made.spill.exists()
        with pytest.raises(manifest.Busy) as died:  # and what is sent to it later
            await cursor.execute("SELECT 1")
        assert str(died.value) == "Died"
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", "the file reader's process ended (reply) with no statement in flight")
    ]


@pytest.mark.parametrize("first", FIRST.values(), ids=FIRST.keys())
def test_a_start_not_of_the_protocol_is_p27(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, first: str
) -> None:
    """A first answer that is neither ``ready`` nor ``unstarted`` as the protocol has them (a
    version is digits and dots, a check's names are this process's own) is no start: the process is killed,
    its spill directory given up, and the log says so without a word of the answer."""
    stand_in(tmp_path, monkeypatch, FAKE.format(first=first, answer="ok(request)"))
    with pytest.raises(duck.Unstarted) as refused, ducked.started(tmp_path):
        pass
    assert refused.value.reason == "process" and list((tmp_path / "spill").iterdir()) == []
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("ERROR", "the file reader did not start: its process answered outside the protocol")
    ]


# The product's own reader, but that the rows of one statement (by how it begins), or every statement's
# columns' types, are of its choosing: free text where DuckDB answers a number or a type.
FORGING = """
taken, fetched = reader._execute, reader._batch
def execute(held, request):
    held.statement = request["statement"]
    return taken(held, request)
def batch(held, request):
    rows = fetched(held, request)
    return {rows} if rows and held.statement.startswith({statement!r}) else rows
def columns(held, request):
    found = [[name, {kind}] for name, kind, *_ in held.cursor.description]
    return reader.pack({{"id": request["id"], "columns": found}}, reader.ANSWER_MAX)
reader._OPS["execute"], reader._OPS["columns"], reader._batch = execute, columns, batch
reader.main()
"""
TEXT = "'SECRET value of another export' + chr(10) + '2026-10-04 12:00:00 ERROR a forged line'"
FORGED = {  # the statement, its rows and a column's type, as a reader of the attacker's answers them
    "a column's type": ("parquet", "", "rows", TEXT),
    "a count": ("csv", "SELECT count(*), ", f"[({TEXT}, '1', '1', '1')]", "str(kind)"),
    "a rejected line's number": (
        "csv",
        "SELECT s.file_path",
        f"[(rows[0][0], 1, {TEXT}, 'x')]",
        "str(kind)",
    ),
    "a rejected line's file": ("csv", "SELECT s.file_path", f"[({TEXT}, 1, 2, 'x')]", "str(kind)"),
}


@pytest.mark.parametrize("forged", FORGED.values(), ids=FORGED.keys())
async def test_a_reader_s_text_reaches_no_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, forged: tuple[str, ...]
) -> None:
    """What a plan keeps or words of a reader's answers is checked where it crosses, by what its
    statement must answer: a column's type by this process's grammar, a count and a rejected line's number
    as integers, a rejected line's file as one of the side's. Text in their place (a reader whose answers
    are a hostile file's) was worded in the plan's refusal, the 422 of a run's creation, and in the log.
    It is no answer of DuckDB's: the reader is ended (``reply``), the plan is not ready, and nothing
    of the text is in a message or a log line."""
    form, statement, rows, kind = forged
    base = tmp_path.resolve()
    root = base / "files" / "root"
    root.mkdir(parents=True)
    (root / "two.parquet").write_bytes(duck.PARQUET)
    (root / "t.csv").write_bytes(b'id|v\n1|a\n2|"b\n')  # its last line is no row
    stand_in(base, monkeypatch, FORGING.format(statement=statement, rows=rows, kind=kind))
    parquet = {"name": "t", "keyColumns": ["id"], "file": {"path": "two.parquet", "format": "parquet"}}
    with pytest.raises(tables.NotReady) as refused:
        await planned(base, parquet if form == "parquet" else csv_table("t.csv", ["id", "v"]))
    assert str(refused.value) == "side a: the file reader did not take its files' work (Died): try again"
    async with asyncio.timeout(5):
        while duck._readers:  # its end is made
            await asyncio.sleep(0.01)
    said = _logged(caplog)
    assert len(said) == 1 and said[0][1].startswith("the file reader's process ended (reply) ")
    assert "SECRET" not in caplog.text and "forged" not in caplog.text and duck._instance is None


async def test_a_frame_of_too_many_cells_is_refused_unbuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a frame may make this process build is bounded before it is built. A frame of rows is one
    byte a column that is all NULL: 400 rows of 65,535 such columns, in 65 KiB and within the rows its
    fetch allows, would be 26 million cells (408 MiB, as observed; 20 GiB at the 20,000 rows allowed). It
    passes ``reader.CELLS``: no row of it is built, and it is the reader's end (``reply``)."""
    bomb = 'raw(b"R", struct.pack("<IIH", request["id"], 400, 65535) + b"n" * 65535)'
    answer = f'ok(request) if request["op"] == "execute" else {bomb}'
    stand_in(tmp_path, monkeypatch, FAKE.format(first=READY, answer=answer))
    assert 65_535 <= reader.CELLS < 400 * 65_535 and 400 < 2 * duck.BATCH
    with ducked.started(tmp_path) as made:
        cursor = made.cursor()
        await cursor.execute("SELECT 1")
        tracemalloc.start()
        try:
            with pytest.raises(manifest.Busy) as died:
                await cursor.fetch(duck.BATCH)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        made._ending.join(5)
        assert str(died.value) == "Died" and made.cause == "reply" and peak < 8 * 2**20


async def test_a_wide_statement_crosses_in_shorter_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader closes a batch at ``reader.CELLS`` cells as it closes one at its rows or its bytes,
    so a statement of many columns is never refused for them: it crosses in batches of fewer rows, in
    their order, a step's rows past the cells waiting for the next fetch, and nothing is read ahead."""
    stand_in(tmp_path, monkeypatch, CELLS=40)  # ten rows of four columns
    select = "SELECT i, i + 1, i + 2, i + 3 FROM range(25) t(i)"
    rows = [(i, i + 1, i + 2, i + 3) for i in range(25)]
    with ducked.started(tmp_path) as made:
        cursor = made.cursor()
        for step in (duck.BATCH, 7, 3):
            await cursor.execute(select)
            batches = [await cursor.batch(step) for _ in range(4)]
            assert [len(batch) for batch in batches] == [10, 10, 5, 0] and not cursor._ahead
            assert [row for batch in batches for row in batch] == rows
        await cursor.execute(select)
        assert [len(await cursor.fetch(size)) for size in (4, 25, 25, 25)] == [4, 10, 10, 1]
        assert made.cause is None


async def test_a_batch_over_a_frame_crosses_in_several(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch is never refused for its size, only a row: one whose frame would pass FRAME_MAX crosses
    as its longest prefix of whole rows that fits, and the rest with the cursor's next fetch; a row that
    alone passes it is Oversize, and the reader lives."""
    monkeypatch.setattr(reader, "FRAME_MAX", 4096)  # the child's is its first frame's
    with ducked.started(tmp_path) as made:
        cursor = made.cursor()
        await cursor.execute("SELECT i, repeat('x', 1000 + i::INTEGER) FROM range(20) t(i) ORDER BY i")
        batches = [await cursor.batch(duck.STEP) for _ in range(7)]
        assert [len(batch) for batch in batches] == [4, 4, 4, 3, 3, 2, 0]
        assert [row for batch in batches for row in batch] == [(i, "x" * (1000 + i)) for i in range(20)]
        for batch, after in itertools.pairwise(batches[:6]):  # each the longest that fits
            with pytest.raises(reader.Oversize):
                reader.pack_rows(1, [*batch, after[0]], 4096)
        await cursor.execute("SELECT i, repeat('x', 1000) FROM range(20) t(i) ORDER BY i")
        assert len(await cursor.batch(duck.STEP)) == 4  # sixteen rows are kept: no answer holds more than
        assert await cursor.fetch(1) == [(4, "x" * 1000)]  # its fetch asked for, whatever was kept
        assert [len(await cursor.fetch(2)) for _ in range(9)] == [2, 2, 2, 2, 2, 2, 2, 1, 0]
        await cursor.execute("SELECT i, repeat('x', 5000 * (i = 2)::INTEGER) FROM range(4) t(i) ORDER BY i")
        assert await cursor.fetch(10) == [(0, ""), (1, "")]  # the rows before the one no frame holds
        with pytest.raises(TargetRefused) as refused:
            await cursor.fetch(10)
        assert str(refused.value) == "Oversize" and made.cause is None
        # A request past its limit and a bind of a type no frame carries are refused before they are sent.
        monkeypatch.setattr(reader, "REQUEST_MAX", 200)
        for statement, bind in (("SELECT '" + "x" * 200 + "'", None), ("SELECT $v", 1.5)):
            with pytest.raises(TargetRefused) as refused:
                await cursor.execute(statement, {"v": bind})
            assert str(refused.value) == ("Unframed" if bind else "Oversize")
        await cursor.execute("SELECT 42")
        assert await cursor.fetch(1) == [(42,)] and made.cause is None


async def test_an_answer_over_its_limit_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An answer that would pass ANSWER_MAX (the columns of a footer whose names are that long) is
    answered Oversize in its place: the plan is refused by that class, and the reader lives and reads on."""
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    wide = ", ".join(f"{n} AS {'c' * 200}{n}" for n in range(3))
    ducked.unrestricted().execute(f"COPY (SELECT 1 AS id, {wide}) TO '{root}/wide.parquet' (FORMAT parquet)")
    (root / "t.csv").write_bytes(b"id|v\n1|a\n")
    stand_in(tmp_path, monkeypatch, ANSWER_MAX=400)
    table = {"name": "t", "keyColumns": ["id"], "file": {"path": "wide.parquet", "format": "parquet"}}
    with pytest.raises(ValueError) as refused:
        await planned(tmp_path, table)
    assert str(refused.value) == "side a: its files could not be read while the run was planned (Oversize)"
    kept = duck._instance
    assert kept is not None and kept.cause is None
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v"]))
    assert len(await scanned(found, plan)) == 1 and duck._instance is kept


async def test_every_composed_statement_fetches_framed_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No statement DeltaSleuth composes selects a value a frame does not carry. Every statement a
    plan, a scan across engines and in DuckDB, a census, a frame, the count of rows without a key, a
    per-key read and ds target check send, of a Parquet side holding every type and a CSV side with lines
    that are not rows, is sent again on a connection in this process: each value DuckDB hands over there is
    None, a boolean, an integer, text or bytes, and the rows the reader's frames carried are those rows."""
    sent: list[tuple[int, str, Any, list[Any]]] = []
    select, asked = duck.Cursor._select, duck.Cursor._asked

    def selecting(self: duck.Cursor, statement: str, values: Any) -> None:
        sent.append((self._number, statement, values, []))
        select(self, statement, values)

    def asking(self: duck.Cursor, op: str, *tags: Any, **members: Any) -> Any:
        found = asked(self, op, *tags, **members)
        if op == "fetch":
            next(rows for number, *_, rows in reversed(sent) if number == self._number).extend(found)
        return found

    monkeypatch.setattr(duck.Cursor, "_select", selecting)
    monkeypatch.setattr(duck.Cursor, "_asked", asking)
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    con = ducked.unrestricted()
    con.execute((GOLDEN / "files.sql").read_text())
    con.execute(f"COPY corpus TO '{root}/corpus.parquet' (FORMAT parquet)")
    (root / "t.csv").write_bytes(b'id|v|d\n1|"a\nb"|2024-01-02\n2|c\n3||2024-01-03\n007|e|x\n')
    engine, found = duck.Files(), registry(tmp_path)
    exports = found.get("exports")
    keyed = csv_table("t.csv", ["id", "v", "d"], keys={"id": {"as": "integer"}}) | {"where": "v <> 'z'"}
    typed = {"typeMap": {"d": {"as": "date"}}}
    parquet = {"name": "t", "keyColumns": ["k"], "file": {"path": "corpus.parquet", "format": "parquet"}}
    pair = file_spec(keyed, {"role": "new", "target": "exports", "table": keyed})
    plans = [(await planned(tmp_path, table, **side))[0] for table, side in ((keyed, typed), (parquet, {}))]
    made = await tables.planning(found, compile_spec(pair, targets=found))
    assert made.plan is not None
    plans.append(tables.pins(made.plan, made.pinned)["a"])  # hashed in DuckDB
    for plan in plans:
        name, column = next(iter(plan["columns"].items()))
        framed = plan | {"strata": {name: column}}
        await scanned(found, plan)
        async with engine.snapshot(exports, timeout_s=30) as session:
            assert [row async for batch in engine.census(session, plan) for row in batch]
            assert [row async for batch in engine.frame(session, framed) for row in batch]
            await engine.null_keys(session, plan)
        key = (await scanned(found, plan))[0][0]
        assert (await read(found, plan, key))[key]
    for path, form in (("t.csv", "csv"), ("corpus.parquet", "parquet")):
        assert await duck.check(exports, path, form, count=True, listed=True, header=form == "csv")
    said = " ".join(statement for _, statement, _, _ in sent)
    for composed in "sniff_csv parquet_schema reject_errors QUALIFY sha256 unhex store_rejects".split():
        assert composed in said
    with ducked.locked(tmp_path) as here:
        cursors: dict[int, Any] = {}
        for number, statement, values, carried in sent:
            cursor = cursors.setdefault(number, here.con.cursor())
            rows = cursor.execute(statement, values).fetchall()
            kinds = {type(value) for row in rows for value in row}
            assert kinds <= {type(None), bool, int, str, bytes}, (statement, kinds)
            assert Counter(map(repr, carried)) <= Counter(map(repr, rows)), statement
    assert len(sent) > 40 and len({statement for _, statement, _, _ in sent}) > 25


# --- the reader's process ------------------------------------------------------------------------------


def _state(pid: int) -> str:
    """A process's state by the process table: ``S`` asleep, ``Z`` ended and not reaped, none when gone."""
    if sys.platform == "darwin":
        listed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        return listed.stdout.strip()[:1]
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return ""


def _threads(pid: int) -> int:
    """A process's threads, by the process table."""
    if sys.platform == "darwin":
        listed = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True, check=True)
        return len(listed.stdout.splitlines()) - 1
    return int(re.search(r"Threads:\s+([0-9]+)", Path(f"/proc/{pid}/status").read_text())[1])  # type: ignore[index]


def test_the_parent_never_loads_duckdb(tmp_path: Path) -> None:
    """The new guarantee: no ``ds`` process imports duckdb, only its reader does. In a process of its own, a
    CSV and a Parquet side planned, scanned, read by key and checked: duckdb is not among its modules."""
    script = (
        "import asyncio, sys\nfrom pathlib import Path\nfrom deltasleuth.connectors.tables import duck\n"
        "from tests.unit.connectors.ducked import csv_table, planned, read, scanned\n"
        "async def main(base):\n"
        "    root = base / 'files' / 'root'\n"
        "    root.mkdir(parents=True)\n"
        "    (root / 't.csv').write_bytes(b'id|v\\n1|a\\n2|b\\n')\n"
        "    (root / 'two.parquet').write_bytes(duck.PARQUET)\n"
        "    parquet = {'keyColumns': ['id'], 'file': {'path': 'two.parquet', 'format': 'parquet'}}\n"
        "    for made in (csv_table('t.csv', ['id', 'v']), parquet | {'name': 't'}):\n"
        "        plan, found = await planned(base, made)\n"
        "        rows, row = await scanned(found, plan), (await read(found, plan, '1'))['1'][0][0]\n"
        "        path = plan['file']['path']\n"
        "        checked = await duck.check(found.get('exports'), path, path.split('.')[1], count=True)\n"
        "        print(plan['rows'], len(rows), row, checked[-1][:17], 'duckdb' in sys.modules)\n"
        "asyncio.run(main(Path(sys.argv[1])))\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path.resolve())],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert ran.returncode == 0 and ran.stdout.splitlines() == [
        '2 2 {"v": "a"} count (as drafted False',
        '2 2 {"name": "a"} count: 2 rows by  False',
    ]
    starts = ran.stderr.splitlines()  # its readers' starts (the roots are set anew for each side), no more
    assert starts and all(line.startswith("the file reader started: duckdb ") for line in starts)


async def test_the_reader_is_started_with_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reader is this interpreter run on ``reader.py`` by its path, with ``-I``, an empty
    environment, its spill directory to work in and a session of its own; it imports no ``deltasleuth``
    package, inherits no descriptor and leaves no core. One it does inherit is counted, and logged as a
    warning."""
    base, seen, started, popen = tmp_path.resolve(), tmp_path / "seen.json", [], subprocess.Popen
    planted = os.open(os.devnull, os.O_RDONLY)

    def recording(args: Any, **kwargs: Any) -> Any:
        started.append((list(args), kwargs))
        return popen(args, **kwargs | ({"pass_fds": (planted,)} if len(started) > 1 else {}))

    monkeypatch.setattr(subprocess, "Popen", recording)
    with ducked.started(base) as made:
        ((args, kwargs),) = started
        assert (
            args == [sys.executable, "-I", "-X", "utf8", reader.__file__] and reader.__file__ == ducked.READER
        )
        assert (kwargs["env"], kwargs["cwd"], kwargs["close_fds"], kwargs["start_new_session"]) == (
            {}, made.spill, True, True,
        )  # fmt: skip
        assert (kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]) == (-1, -1, subprocess.DEVNULL)
        assert os.getsid(made.proc.pid) == made.proc.pid != os.getsid(0) and caplog.records == []
    # What the process itself sees, written as its input ends: the same reader, started by a script.
    leaving = (
        "import json, os, resource, sys\nleave = os._exit\n"
        "def seen(status):\n"
        f"    with open({str(seen)!r}, 'w') as out:\n"
        "        json.dump([sorted(sys.modules), sorted(os.environ), sys.flags.isolated, sys.flags.utf8_mode,"
        " os.getcwd(), sys.path, resource.getrlimit(resource.RLIMIT_CORE)], out)\n"
        "    leave(status)\n"
        "os._exit = seen\nreader.main()"
    )
    stand_in(base, monkeypatch, leaving)
    made = duck._spawned(base / "spill", [str(base / "files" / "root")], base / "outside.csv", 256, 1024, 2)
    cursor, spill = made.cursor(), str(made.spill)
    await cursor.execute("SELECT 42")
    assert await cursor.fetch(1) == [(42,)]
    made._out.put(None)  # its input ends
    made._ending.join(5)
    modules, environ, isolated, utf8, cwd, path, core = json.loads(seen.read_text())
    assert made.cause == "exit 0" and "duckdb" in modules and (isolated, utf8, cwd) == (1, 1, spill)
    assert core == [0, 0]  # it leaves no core, on every platform
    assert not [name for name in modules if name.split(".")[0] in ("deltasleuth", "tests", "pytest")]
    assert set(environ) <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}  # the interpreter's own, and macOS's
    assert "" not in path and str(Path(ducked.READER).parent) not in path  # its own directory shadows nothing
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", "the file reader inherited 1 descriptor(s)"),
        ("WARNING", "the file reader's process ended (exit 0) with no statement in flight"),
    ]
    os.close(planted)


async def test_a_process_that_does_not_start_is_p27(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A process that cannot be started, ends first, does not answer or misses its start's deadline is
    killed and is said as a reader that did not start, never as a missing directory; the log says which, and
    nothing is left of it."""
    base = tmp_path.resolve()
    found = registry(base)
    made, roots, exports = ducked.settings(base), [str(base / "files" / "root")], found.get("exports")
    p27 = (
        "side a: the file reader did not start: its process did not start or did not answer (see the server"
        " log)"
    )

    async def refused() -> str:
        with pytest.raises(ValueError) as refusal:
            async with duck._planning("side a", exports, 1):
                await duck.instance(made, roots)
        assert not isinstance(refusal.value, duck.NotReady)
        return str(refusal.value)

    scripts = ("raise SystemExit(3)", "reader.connect = None\nreader.main()", "import time\ntime.sleep(60)")
    with monkeypatch.context() as patched:
        patched.setattr(sys, "executable", str(base / "no-python"))
        assert await refused() == p27 and list(made.spill_dir.iterdir()) == []
    for script in scripts:
        with monkeypatch.context() as patched:
            patched.setattr(duck, "START_S", 0.5 if "sleep" in script else duck.START_S)
            stand_in(base, patched, script)
            assert await refused() == p27 and list(made.spill_dir.iterdir()) == []
    with monkeypatch.context() as patched:  # its file work past the deadline: what it started is killed
        free = duck._free
        patched.setattr(duck, "_free", lambda path: time.sleep(3) or free(path))
        patched.setattr(duck, "START_S", 1)
        patched.setattr(manifest, "STAT_S", 0.5)
        started = time.monotonic()
        assert await refused() == p27 and 1.5 <= time.monotonic() - started < 3
        async with asyncio.timeout(10):
            while manifest.stuck() or list(made.spill_dir.iterdir()):
                await asyncio.sleep(0.01)
    said = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    fault = r"exit 1: TypeError at reader.py line [0-9]+"  # the line is this build's
    assert re.fullmatch(
        rf"the file reader did not start: its process ended as it started \({fault}\)", said[2]
    )
    assert [*said[:2], *said[3:]] == [
        "the file reader did not start: its process could not be started (FileNotFoundError)",
        "the file reader did not start: its process ended as it started (exit 3)",
        "the file reader did not start: its process did not answer within 0.5 s",
        "the file reader did not start: its process was not ready within 1.5 s",
    ]
    assert ducked.strays() == []


async def test_a_failed_start_releases_its_spill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed start gives up the spill directory it made, whatever failed: the guard, the room on the
    spill's volume, the process (it stayed held, and counted, until the process ended)."""
    base, held = tmp_path.resolve(), set(manifest._HELD)
    inside = ducked.settings(base)
    with pytest.raises(duck.Unstarted) as unstarted:  # the probe lies inside the root
        duck._started(inside, [str(base / "etc")])
    assert unstarted.value.reason == "guard"
    with monkeypatch.context() as patched:
        patched.setattr(duck, "_free", lambda path: 0)
        with pytest.raises(duck.Unstarted) as unstarted:
            duck._started(inside, [str(base / "files")])
    assert unstarted.value.reason == "space"
    with monkeypatch.context() as patched:
        stand_in(base, patched, "raise SystemExit(3)")
        with pytest.raises(duck.Unstarted) as unstarted:
            duck._started(inside, [str(base / "files")])
    assert unstarted.value.reason == "process"
    assert list(inside.spill_dir.iterdir()) == [] and set(manifest._HELD) == held and ducked.strays() == []


async def test_a_replaced_reader_is_ended(tmp_path: Path) -> None:
    """A reader that is the process's no more (its roots or its budget changed) is retired: killed at
    once when it holds no cursor, else with its last one, the session that holds it finishing on it."""
    base = tmp_path.resolve()
    made, roots = ducked.settings(base), [str(base / "files" / "a"), str(base / "files" / "b")]
    first = await duck.instance(made, roots[:1])
    second = await duck.instance(made, roots)  # other roots
    first._ending.join(5)
    assert (first.cause, first.dropped, first.spill.exists()) == ("SIGKILL", True, False)
    cursor = second.cursor()
    await cursor.execute("SELECT i FROM range(3) t(i)")
    third = await duck.instance(ducked.settings(base, memory="128MB"), roots)  # another budget
    assert third is not second and second.dropped and second.cause is None
    assert await cursor.fetch(10) == [(0,), (1,), (2,)]  # its session finishes on it
    await cursor.close()
    second._ending.join(5)
    assert (second.cause, second.spill.exists()) == ("SIGKILL", False)
    assert (
        ducked.strays() == [] and await duck.instance(ducked.settings(base, memory="128MB"), roots) is third
    )


async def test_a_killed_reader_is_replaced(tmp_path: Path) -> None:
    """A reader whose process ends, idle or mid-statement, is reaped and its spill directory given up;
    what waited on it, and what is sent to it later, is Busy('Died'), and the next use starts another."""
    base = tmp_path.resolve()
    made, roots = ducked.settings(base), [str(base / "files" / "root")]
    first = await duck.instance(made, roots)
    os.kill(first.proc.pid, signal.SIGKILL)  # idle
    first._ending.join(5)
    assert (first.cause, first.dropped, first.spill.exists()) == ("SIGKILL", True, False)
    second = await duck.instance(made, roots)
    assert second is not first and second.spill.exists()
    cursor = second.cursor()
    sort = asyncio.create_task(cursor.execute(duck.SPILL_SORT.replace("200000", "300000000")))
    await asyncio.sleep(0.5)
    os.kill(second.proc.pid, signal.SIGTERM)  # mid-statement
    with pytest.raises(manifest.Busy) as died:
        await sort
    second._ending.join(5)
    assert str(died.value) == "Died" and (second.cause, second.spill.exists()) == ("SIGTERM", False)
    with pytest.raises(manifest.Busy) as died:
        await cursor.fetch(1)
    assert str(died.value) == "Died"
    await cursor.close()
    third = await duck.instance(made, roots)
    cursor = third.cursor()
    await cursor.execute("SELECT 42")
    assert await cursor.fetch(1) == [(42,)] and ducked.strays() == []
    await cursor.close()


async def test_a_closed_cursor_s_statement_does_not_go_on(tmp_path: Path) -> None:
    """A cursor's close interrupts its statement, and one the reader has not begun (its process stopped
    meanwhile, as a busy host stops it) is not run: nothing goes on sorting for nobody. No request is sent
    after the close."""
    with ducked.started(tmp_path) as made:
        cursor, other = made.cursor(), made.cursor()
        os.kill(made.proc.pid, signal.SIGSTOP)
        sort = asyncio.create_task(cursor.execute(duck.SPILL_SORT.replace("200000", "300000000")))
        await asyncio.sleep(0.2)  # its request is written
        await cursor.close()
        os.kill(made.proc.pid, signal.SIGCONT)
        closed = time.monotonic()
        with pytest.raises(StatementTimeout) as ended:
            await sort
        assert str(ended.value) == "InterruptException" and time.monotonic() - closed < 5
        with pytest.raises(TargetRefused) as refused:
            await cursor.execute("SELECT 1")
        assert str(refused.value) == "ConnectionException" and made.cause is None
        await other.execute("SELECT 42")
        assert await other.fetch(1) == [(42,)]


async def test_an_interrupt_before_a_statement_began_still_stops_it(tmp_path: Path) -> None:
    """DuckDB forgets an interrupt that reaches it while it still binds and prepares a statement
    (observed on 1.5.5), so the reader interrupts a request that runs until it has returned, and runs none
    that was interrupted before it began. A cursor closed as its statement is sent (a pass cancelled, here
    a statement as long to prepare as a group of 200 keys), and a deadline that falls before the statement
    began (a plan past ``files.plan_s`` sends its next one with a millisecond), end the statement at once:
    it does not run on for nobody until its reader is killed after ``GRACE_S`` and its files charged."""
    values = ", ".join(f"({i}, $v{i})" for i in range(200))
    long = (
        f"SELECT count(*) FROM range(100000000000) t(i) JOIN (VALUES {values}) AS a(i, k)"  # noqa: S608
        " ON encode(CAST(t.i AS VARCHAR)) = a.k WHERE md5(CAST(t.i AS VARCHAR)) = ''"
    )
    binds = {f"v{i}": f"key-{i}".encode() for i in range(200)}
    with ducked.started(tmp_path) as made:
        for delay in (0, 0.0005, 0.001, 0.002, 0.005) * 3:
            cursor = made.cursor()
            sent = asyncio.create_task(cursor.execute(long, binds))
            while cursor._flight is None:
                await asyncio.sleep(0)
            await asyncio.sleep(delay)
            await cursor.close()
            with pytest.raises(StatementTimeout) as ended:
                await asyncio.wait_for(sent, duck.GRACE_S / 2)
            assert str(ended.value) == "InterruptException"
        cursor, started = made.cursor(), time.monotonic()
        with pytest.raises(StatementTimeout) as ended:
            await cursor.execute(long, binds, timeout_s=0.001)
        assert str(ended.value) == "InterruptException" and time.monotonic() - started < duck.GRACE_S / 2
        await cursor.execute("SELECT 42")  # the reader was not retired for it, and the cursor reads on
        assert await cursor.fetch(1) == [(42,)]
        assert (made.dropped, made.cause, made._pending) == (False, None, {})


async def test_idle_cursors_hold_no_thread(tmp_path: Path) -> None:
    """A request runs on a thread started for it, so 200 open cursors, each with a result
    half read, add no thread to the reader's process: its count, by the process table, is what it was;
    the self-test's sort completes beside them, on Linux under a data limit of 1 GiB, which 200 threads'
    stacks alone would pass; and a ping is answered while a statement runs."""
    with ducked.started(tmp_path, memory_mib=duck.SELFTEST_MIB, limit=1024) as made:
        assert made.limit == (1024 if reader.LINUX else None)
        first = made.cursor()
        await first.execute("SELECT 1")
        before = _threads(made.proc.pid)
        cursors = [made.cursor() for _ in range(200)]
        for cursor in cursors:
            await cursor.execute("SELECT i FROM range(10) t(i)")
            assert await cursor.fetch(1) == [(0,)]
        async with asyncio.timeout(5):
            while _threads(made.proc.pid) != before:
                await asyncio.sleep(0.05)
        assert await duck._spilled(first, made.spill / "temp")
        sort = asyncio.create_task(first.execute(duck.SPILL_SORT.replace("200000", "300000000")))
        await asyncio.sleep(0.3)
        request, slot = made.ask(op="ping")
        assert slot.result(5) == (b"J", {"id": request["id"], "ok": True}) and not sort.done()
        assert await cursors[-1].fetch(2) == [(1,), (2,)]
        sort.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sort


def test_a_batch_is_read_ahead(tmp_path: Path) -> None:
    """The child's own functions on a locked connection of this process. A fetch that asks for it
    and is answered a full batch (its rows, or a step's more) has its answer sent, then the next batch
    fetched and packed on its thread;
    the next fetch of the same sizes is answered with that batch under its own id, and reads ahead again;
    a short batch, the statement's end, reads nothing ahead. A fetch of other sizes takes the rows read
    ahead first; an error met reading ahead answers the next fetch; an interrupt drops the batch, and the
    next fetch is the interrupted statement's (no row is passed over in silence); a new statement forgets
    it; and a fetch that does not ask reads nothing ahead."""
    numbers, frames = itertools.count(1), []
    with ducked.locked(tmp_path) as made:
        held = reader._Held()

        def ask(op: str, **members: Any) -> Any:
            request = {"id": next(numbers), "op": op, "cursor": 1, **members}
            reader._answer(frames.append, made.con, held, request, held.drops)
            kind, body = frames[-1][4:5], frames[-1][5:]
            if kind == b"R":
                number, rows = reader.unpack_rows(body, 10**6)
                assert number == request["id"]
                return [row[0] for row in rows]
            return reader.unpack(body)

        def fetch(rows: int, ahead: bool = True) -> Any:
            return ask("fetch", rows=rows, step=rows, bytes=2**20, **({"ahead": True} if ahead else {}))

        select = "SELECT i FROM range(25) t(i)"
        assert ask("execute", statement=select, binds=None)["ok"] is True and held.ahead is None
        assert fetch(10) == list(range(10))
        assert held.ahead is not None and held.ahead[2] == [(i,) for i in range(10, 20)]
        assert fetch(10) == list(range(10, 20)) and held.ahead is not None  # the last five, read ahead
        assert fetch(10) == list(range(20, 25)) and held.ahead is None  # a short batch: the end is next
        assert fetch(10) == [] and len(frames) == 5
        # Other sizes: the rows read ahead are the next ones, in their order.
        ask("execute", statement=select, binds=None)
        assert fetch(10) == list(range(10)) and held.ahead is not None
        assert fetch(3, ahead=False) == [10, 11, 12] and held.ahead is None
        assert fetch(10) + fetch(10) == list(range(13, 25))
        # An error met reading ahead answers the fetch that would have met it.
        late = (
            "SELECT CASE WHEN i < 300000 THEN i ELSE CAST(error('x') AS BIGINT) END FROM range(600000) t(i)"
        )
        ask("execute", statement=late, binds=None)
        seen: list[int] = []
        while isinstance(found := fetch(50_000), list):
            seen += found
            met = held.ahead is not None and isinstance(held.ahead[3], Exception)
        assert met and seen == list(range(len(seen))) and 0 < len(seen) <= 300_000
        assert found["kind"] == "other" and found["error"] in reader.NAMES and held.ahead is None
        # An interrupt drops the batch: the statement was interrupted, and its next fetch says so.
        ask("execute", statement=select, binds=None)
        assert fetch(10) == list(range(10)) and held.ahead is not None
        held.drops += 1  # as the child's loop does at an interrupt
        assert fetch(10) == {"id": next(numbers) - 1, "error": "InterruptException", "kind": "interrupt"}
        # A new statement forgets it; a fetch that does not ask reads nothing ahead.
        ask("execute", statement=select, binds=None)
        assert fetch(10) == list(range(10)) and held.ahead is not None
        ask("execute", statement="SELECT i FROM range(100, 125) t(i)", binds=None)
        assert held.ahead is None and fetch(10, ahead=False) == list(range(100, 110)) and held.ahead is None
        # A batch fetched in steps closes a step's rows past its rows: a full one all the same.
        ask("execute", statement=select, binds=None)
        stepped = {"rows": 10, "step": 4, "bytes": 2**20, "ahead": True}
        assert ask("fetch", **stepped) == list(range(12)) and held.ahead is not None
        assert held.ahead[2] == [(i,) for i in range(12, 24)]
        assert ask("fetch", **stepped) == list(range(12, 24)) and ask("fetch", **stepped) == [24]


async def test_a_batch_fetched_in_steps_is_read_ahead_too(tmp_path: Path) -> None:
    """A statement with a text, bytes or JSON column is fetched ``STEP`` rows a step (every scan
    across engines of such a column), so its batches close a step past ``BATCH`` rows: 10,048. Each is a
    full batch, after which the next is read ahead, as after one of ``BATCH`` rows exactly (it was read
    ahead only then, so never for these); the last, short one reads nothing ahead."""
    with ducked.started(tmp_path) as made:
        cursor, seen = made.cursor(), []
        await cursor.execute("SELECT i, 'text ' || i FROM range(35000) t(i)")
        while batch := await cursor.batch(duck.STEP):
            seen.append((batch[0][0], len(batch), cursor._ahead))
        assert seen == [(0, 10048, True), (10048, 10048, True), (20096, 10048, True), (30144, 4856, False)]


def test_the_reader_s_refusals_are_worded_by_class(tmp_path: Path) -> None:
    """What the failures of a run gain with the reader: a failed run's error is the class and the name the
    reader answered, of ``reader.NAMES``."""
    said = [
        failure(duck._error(kind, name, tmp_path))
        for kind, name in (("memory", "MemoryError"), ("other", "Oversize"), ("other", "Unframed"))
    ]
    assert said == ["TargetTempSpace: MemoryError", "TargetRefused: Oversize", "TargetRefused: Unframed"]
    assert {"MemoryError", "Oversize", "Unframed"} <= reader.NAMES


# --- deadlines and cancellation ------------------------------------------------------------------------

# --- what the reader denies itself ---------------------------------------------------------------------

AARCH64, X86_64, I386, RISCV64 = 0xC00000B7, 0xC000003E, 0x40000003, 0xC00000F3  # AUDIT_ARCH_* of a call
EPERM, ALLOWED = 0x00050001, 0x7FFF0000  # SECCOMP_RET_ERRNO with EPERM, SECCOMP_RET_ALLOW
X32 = 0x40000000  # what an x32 call's number carries
CALLS = {  # by architecture and number: what the filter answers
    (X86_64, 41): EPERM, (X86_64, 53): EPERM, (X86_64, 425): EPERM,  # socket, socketpair, io_uring_setup
    (X86_64, X32 + 41): EPERM, (X86_64, X32): EPERM, (X86_64, 0xFFFFFFFF): EPERM,  # x32: socket, read
    (X86_64, 0): ALLOWED, (X86_64, 9): ALLOWED, (X86_64, 435): ALLOWED,  # read, mmap, clone3
    (X86_64, 198): ALLOWED, (X86_64, 199): ALLOWED, (X86_64, 257): ALLOWED,  # aarch64's numbers; openat
    (AARCH64, 198): EPERM, (AARCH64, 199): EPERM, (AARCH64, 425): EPERM,
    (AARCH64, 63): ALLOWED, (AARCH64, 222): ALLOWED, (AARCH64, 435): ALLOWED,  # read, mmap, clone3
    (AARCH64, 41): ALLOWED, (AARCH64, 53): ALLOWED, (AARCH64, X32 + 41): ALLOWED,  # x86_64's numbers
    (I386, 3): EPERM, (I386, 102): EPERM, (RISCV64, 63): EPERM, (RISCV64, 198): EPERM,  # read among them
}  # fmt: skip


def _decided(architecture: int, number: int) -> int:
    """What ``reader.FILTER`` returns for a call, read as the kernel reads a seccomp program: the
    accumulator loaded from the call's data (its number at 0, its architecture at 4), a jump over ``jt``
    or ``jf`` instructions as it equals, or is at or over, the operand, and a return of the operand."""
    data = struct.pack("<II", number, architecture) + bytes(56)  # then its address and its six arguments
    at, held = 0, 0
    for _ in reader.FILTER:  # a program only jumps forwards
        code, jt, jf, operand = reader.FILTER[at]
        if code == 0x06:
            return operand
        if code == 0x20:
            held, jt, jf = int.from_bytes(data[operand : operand + 4], "little"), 0, 0
        else:
            assert code in (0x15, 0x35)
        at += 1 + (jt if code == 0x20 or (held == operand if code == 0x15 else held >= operand) else jf)
    raise AssertionError("no return")


def test_the_socket_filter_decides_by_architecture() -> None:
    """On every platform and with no kernel: the one program of 15 instructions the reader loads
    on aarch64 and on x86_64, interpreted over calls of both and of two other architectures. EPERM for
    socket, socketpair and io_uring_setup by each one's own numbers, for an x32 call, and for any call of
    another architecture; every other call is allowed, the other architecture's numbers among them."""
    assert len(reader.FILTER) == 15 and reader.FILTERED == ("aarch64", "x86_64")
    assert all(at + 1 + max(jt, jf) < 15 for at, (code, jt, jf, _) in enumerate(reader.FILTER) if code != 6)
    assert {call: _decided(*call) for call in CALLS} == CALLS and len(CALLS) == 25


STARTED = "the file reader started: duckdb 1.5.5 in process %d, %s, %s"  # the locked version's


def _starts(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in caplog.records if "started" in r.getMessage()]


@pytest.mark.parametrize(
    ("limit", "sockets", "apart", "level"),
    [
        (5296, True, False, "WARNING"),  # the process's reader: its start is always in the log
        (5296, True, True, "INFO"),  # one a per-key group of a charged set: silent where both limits held
        (None, True, True, "WARNING"),
        (5296, False, True, "WARNING"),
    ],
)
def test_a_reader_apart_logs_its_start_only_where_a_limit_did_not_hold(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    limit: int | None, sockets: bool, apart: bool, level: str,
) -> None:  # fmt: skip
    class Made:
        version, proc, limited = "1.5.5", subprocess.CompletedProcess((), 0), True

    Made.proc.pid, Made.limit, Made.sockets = 4242, limit, sockets  # type: ignore[attr-defined]
    monkeypatch.setattr(duck, "_spawned", lambda *_: Made)

    class Configured:
        spill_dir, targets_file = Path("/nowhere"), Path("/nowhere/targets.yaml")
        files = duck.FilesSettings()

    with caplog.at_level("INFO", logger="deltasleuth.files"):
        assert duck._started(Configured, [], apart) is Made  # type: ignore[arg-type]
    [(said, line)] = _starts(caplog)
    assert said == level and line.startswith("the file reader started: duckdb 1.5.5 in process 4242, ")


def _first_frames(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """The data limit and the filter each reader's first frame asks for, from here on."""
    sent: list[tuple[Any, Any]] = []
    tell = duck.Instance.tell

    def told(self: duck.Instance, **message: Any) -> None:
        if "roots" in message:
            sent.append((message["limit"], message["filter"]))
        tell(self, **message)

    monkeypatch.setattr(duck.Instance, "tell", told)
    return sent


async def test_os_limits_off_sets_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With ``files.os_limits`` false a reader is asked for no data limit and no socket filter, and
    its start line says so (``ds target check`` too: ``test_cli``). With it on, the default, it is asked for
    ``files.memory`` + 2 GiB + 600 MiB a thread (5,296 MiB by default) and the filter, and the line says
    what that start saw hold: on macOS neither, the platform having none. The line is a warning either
    way: the level every ``ds`` process prints."""
    base, sent = tmp_path.resolve(), _first_frames(monkeypatch)
    roots, off = [str(base / "files" / "root")], "files.os_limits is false"
    first = await duck.instance(ducked.settings(base, os_limits=False), roots)
    assert sent == [(None, False)] and (first.limit, first.sockets) == (None, False)
    assert duck.restricted(first) == (f"no data limit ({off})", f"sockets not denied ({off})")
    assert _starts(caplog) == [("WARNING", STARTED % (first.proc.pid, *duck.restricted(first)))]
    caplog.clear()
    second = await duck.instance(ducked.settings(base), roots)  # the setting is of a reader's budget
    assert second is not first and sent[1:] == [(256 + 2048 + 2 * 600, True)] and duck._limit(2048, 2) == 5296
    if not reader.LINUX:
        none = "not on this platform"
        assert duck.restricted(second) == (f"no data limit ({none})", f"sockets not denied ({none})")
        assert _starts(caplog) == [("WARNING", STARTED % (second.proc.pid, *duck.restricted(second)))]
    first._ending.join(5)
    assert first.cause == "SIGKILL" and _logged(caplog) == []  # replaced: an end of this process's own


async def test_the_reader_s_memory_is_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """On Linux a reader's start sets its data limit and proves it, on a machine of ``FILTERED``
    denies it sockets, and the start line says both. A start whose probe is not refused (a kernel that does
    not count such a mapping), or whose filter cannot be loaded, reports what it did not see hold, in the
    start line too: on every platform, the reader taking itself for a Linux one. Nothing is reported that
    was not asked for and seen."""
    base = tmp_path.resolve()
    found, roots = ducked.settings(base), [str(base / "files" / "root")]

    def start() -> tuple[duck.Instance, list[tuple[str, str]]]:
        caplog.clear()
        made = duck._started(found, roots)
        duck._stopped(made)
        return made, _starts(caplog)

    if reader.LINUX:
        made, said = start()
        filtered = os.uname().machine in reader.FILTERED
        sockets = "sockets denied" if filtered else "sockets not denied (not on this platform)"
        assert (made.limit, made.sockets) == (3504, filtered)
        assert said == [("WARNING", STARTED % (made.proc.pid, "data limit 3,504 MiB", sockets))]
    unheld = "import resource\nresource.setrlimit = lambda *limits: None\nreader._prctl = None\nreader.main()"
    with monkeypatch.context() as patched:
        stand_in(base, patched, unheld, LINUX=True, FILTERED=(*reader.FILTERED, os.uname().machine))
        made, said = start()
        held = ("no data limit (not held on this host)", "sockets not denied (not held on this host)")
        assert (made.limit, made.sockets) == (None, False) and duck.restricted(made) == held
        assert said == [("WARNING", STARTED % (made.proc.pid, *held))]
    with monkeypatch.context() as patched:  # what a start that saw both hold says, by a process that says so
        held = 'J(**READY | {"limit": 3504, "sockets": True})'
        stand_in(base, patched, FAKE.format(first=held, answer="ok(request)"), LINUX=True)
        made, said = start()
        assert said == [("WARNING", STARTED % (made.proc.pid, "data limit 3,504 MiB", "sockets denied"))]
    assert ducked.strays() == []


SLOW = duck.SPILL_SORT.replace("200000", "300000000")  # a sort no test waits for
HUNG = "SELECT count(*) FROM read_csv($f, auto_detect=false, columns={'id': 'VARCHAR'})"  # of a FIFO
ABANDONED = "file read abandoned after its deadline (%d waiting)"
CHARGED = (
    "the file reader's process ended (%s) while only one side's files were being read: %d of the 3 after"
    " which they are read no more"
)
UNHELD = "the file reader's process ended (%s) with no statement in flight"


def _f12(cause: str, side: str = "a") -> str:
    return (
        f"side {side}: the file reader's process ended 3 times ({cause}) while only this side's files were"
        " being read: they are read no more until DeltaSleuth restarts"
    )


def _logged(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """What the file reader logged, but its starts (``_starts``)."""
    said = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "deltasleuth.files"]
    return [line for line in said if line not in _starts(caplog)]


def _fifo(base: Path) -> Path:
    """A file under the tests' root that nobody writes: a read of it does not end, as on a mount that
    died."""
    path = base.resolve() / "files" / "root" / "hung.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    return path


async def _sides(base: Path, *names: str) -> tuple[list[tables.Plan], TargetRegistry]:
    """A CSV side planned for each name: two rows each, and a file set of its own."""
    root = base / "files" / "root"
    root.mkdir(parents=True, exist_ok=True)
    plans = []
    for name in names:
        (root / f"{name}.csv").write_bytes(f"id|v\n1|{name}\n2|b\n".encode())
        plan, found = await planned(base, csv_table(f"{name}.csv", ["id", "v"]))
        plans.append(plan)
    return plans, found


async def _sent(session: Any) -> duck.Instance:
    """The reader a session's statement was sent to, once its request waits for an answer."""
    async with asyncio.timeout(20):
        while session.cursor is None or session.cursor._flight not in session.cursor._instance._pending:
            await asyncio.sleep(0.01)
    return session.cursor._instance


async def _ended(session: Any, plan: tables.Plan, sent: signal.Signals = faults.QUIET) -> BaseException:
    """What a statement of the side meets when its reader's process is sent a signal under it: one that
    is no crash to the system, unless the test is of a fault."""
    statement = asyncio.create_task(session.send(plan, SLOW, {}))
    made = await _sent(session)
    os.kill(made.proc.pid, sent)
    with pytest.raises(tables.TableError) as met:
        await statement
    made._ending.join(5)
    return met.value


async def test_a_hung_statement_s_process_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A statement that does not end (its file a FIFO nobody writes) is interrupted at its timeout and
    given up GRACE_S later: its caller is answered Deadline, and so is the pool's thread that waited for
    it, which is free; its reader is retired and, the statement having been alone in it, killed at once.
    The read is counted until the process is reaped: nothing is left of it."""
    monkeypatch.setattr(duck, "GRACE_S", 0.3)
    with ducked.started(tmp_path) as made:
        hung = _fifo(tmp_path)
        cursor, started = made.cursor(str(hung.parent)), time.monotonic()
        with pytest.raises(duck.TargetUnreachable) as outage:
            await cursor.execute(HUNG, {"f": [str(hung)]}, timeout_s=0.5)
        assert str(outage.value) == "Deadline" and time.monotonic() - started < 5 and made.dropped
        made._ending.join(5)
        assert (made.cause, made.proc.returncode) == (duck.DEADLINE, -signal.SIGKILL)
        assert manifest.stuck() == 0 and not made.spill.exists()
        free = threading.Barrier(16)  # every thread of the pool takes work again
        for job in [manifest.POOL.submit(free.wait, 10) for _ in range(16)]:
            job.result(15)
    assert _logged(caplog) == [("WARNING", ABANDONED % 1), ("WARNING", UNHELD % duck.DEADLINE)]


async def test_a_statement_killed_after_its_deadline_is_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A side's statement that does not end within its grace, under a root that answers a stat, has
    its reader killed, and the kill is charged to the side's files as any other end: the second and the
    third time in a reader of the task's own, and then they are read no more, ReaderDied naming the kill."""
    monkeypatch.setattr(duck, "GRACE_S", 0.2)
    base = tmp_path.resolve()
    (slow,), found = await _sides(base, "slow")
    hung, engine, readers = _fifo(base), duck.Files(), []
    for _ in range(duck.DEATHS):
        session = await engine.connect(found.get("exports"), timeout_s=0.3)
        with pytest.raises(duck.TargetUnreachable) as outage:
            await session.send(slow, HUNG, {"f": [str(hung)]}, timeout_s=0.3)
        readers.append(session.cursor._instance)  # type: ignore[union-attr]
        await session.close()
        readers[-1]._ending.join(5)
        assert str(outage.value) == "Deadline" and manifest.stuck() == 0
    assert [made.cause for made in readers] == [duck.DEADLINE] * 3 and len(set(readers)) == 3
    with pytest.raises(tables.ReaderDied) as refused:
        await read(found, slow, "1")
    assert str(refused.value) == _f12("killed after its deadline")
    assert _logged(caplog) == [
        line
        for n in (1, 2, 3)
        for line in (("WARNING", ABANDONED % 1), ("ERROR", CHARGED % (duck.DEADLINE, n)))
    ]


async def test_a_dead_mount_s_kill_is_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A kill for a deadline is charged only when a stat of the side's root answers within STAT_S. On a
    mount that does not, the statement that did not end is the outage it was: nothing is charged, the side
    is read in the shared reader again, and the stat is abandoned as any file work is."""
    monkeypatch.setattr(duck, "GRACE_S", 0.2)
    base = tmp_path.resolve()
    (slow,), found = await _sides(base, "slow")
    hung, root, dead, rooted = _fifo(base), str(base / "files" / "root"), threading.Event(), manifest._rooted
    session = await duck.Files().connect(found.get("exports"), timeout_s=0.3)
    monkeypatch.setattr(manifest, "_rooted", lambda found: dead.wait(20) and rooted(found))
    monkeypatch.setattr(manifest, "STAT_S", 0.3)
    try:
        with pytest.raises(duck.TargetUnreachable) as outage:
            await session.send(slow, HUNG, {"f": [str(hung)]}, timeout_s=0.3)
        made = session.cursor._instance  # type: ignore[union-attr]
        await session.close()
        made._ending.join(5)
        assert str(outage.value) == "Deadline" and made.cause == duck.DEADLINE
        assert manifest.stuck(root) == 1 and not duck._doubted(duck._set(slow))  # the stat alone waits
    finally:
        dead.set()
    async with asyncio.timeout(5):
        while manifest.stuck():
            await asyncio.sleep(0.01)
    assert _logged(caplog) == [("WARNING", ABANDONED % 1), ("WARNING", ABANDONED % 2)]
    monkeypatch.undo()
    assert len(await scanned(found, slow)) == 2 and duck._instance is not None and duck._aparts == {}


async def test_an_abandoned_statement_that_answers_frees_its_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement given up on a retired reader that another session's cursor keeps is counted for its
    root until it answers: the late answer, which nobody reads, frees its count with the process alive,
    and the other session finishes on its cursor."""
    monkeypatch.setattr(duck, "GRACE_S", 0.2)
    with ducked.started(tmp_path) as made:
        hung = _fifo(tmp_path)
        root = str(hung.parent)
        kept, cursor = made.cursor(), made.cursor(root)
        await kept.execute("SELECT i FROM range(3) t(i)")
        with pytest.raises(duck.TargetUnreachable) as outage:
            await cursor.execute(HUNG, {"f": [str(hung)]}, timeout_s=0.3)
        assert str(outage.value) == "Deadline" and (made.dropped, made.cause, made.held) == (True, None, 1)
        assert manifest.stuck(root) == 1
        async with asyncio.timeout(10):
            while manifest.stuck(root):  # a writer ends the read
                with suppress(OSError):
                    os.close(os.open(hung, os.O_WRONLY | os.O_NONBLOCK))
                await asyncio.sleep(0.01)
        assert made.cause is None and made._pending == {} and await kept.fetch(10) == [(0,), (1,), (2,)]
        await kept.close()  # its last cursor
        made._ending.join(5)
        assert made.cause == "SIGKILL"


async def test_a_child_that_stops_answering_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A watched statement's reader is asked for a sign of life at every watch. It answers while its
    sort runs; one that answers nobody within STAT_S (stopped here, as a frozen process is) is killed at
    once and its statement is Busy('Died'): a scan, which has no timeout, does not keep SORT for it."""
    monkeypatch.setattr(duck, "WATCH_S", 0.1)
    monkeypatch.setattr(manifest, "STAT_S", 0.5)

    async def unchanged() -> None:
        return None

    with ducked.started(tmp_path) as made:
        sort = asyncio.create_task(made.cursor().execute(SLOW, watch=unchanged))
        await asyncio.sleep(1.5)  # a dozen pings, each answered beside the sort
        assert not sort.done() and made.cause is None
        os.kill(made.proc.pid, signal.SIGSTOP)
        stopped = time.monotonic()
        with pytest.raises(manifest.Busy) as died:
            await sort
        made._ending.join(5)
        assert str(died.value) == "Died" and 0.5 <= time.monotonic() - stopped < 5
        assert (made.cause, made.proc.returncode) == (duck.DEADLINE, -signal.SIGKILL)
    assert _logged(caplog) == [("WARNING", UNHELD % duck.DEADLINE)]


async def test_a_killed_process_that_does_not_end_stays_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A killed process that is not reaped within REAP_S (uninterruptible I/O on a dead mount: here the
    kill is made a stop) is logged by its number, and the statement given up in it stays counted for its
    root until the process does end."""
    monkeypatch.setattr(duck, "GRACE_S", 0.2)
    monkeypatch.setattr(duck, "REAP_S", 0.3)
    with ducked.started(tmp_path) as made:
        hung, pid = _fifo(tmp_path), made.proc.pid
        root = str(hung.parent)
        cursor = made.cursor(root)
        with monkeypatch.context() as patched:
            patched.setattr(os, "killpg", lambda group, sent: os.kill(group, signal.SIGSTOP))
            with pytest.raises(duck.TargetUnreachable):
                await cursor.execute(HUNG, {"f": [str(hung)]}, timeout_s=0.3)
            await asyncio.sleep(1)
        assert (made.dropped, made.killed, made.cause) == (True, True, None) and manifest.stuck(root) == 1
        assert _logged(caplog) == [
            ("WARNING", ABANDONED % 1),
            ("WARNING", f"the file reader's process {pid} did not end within 0.3 s of being killed: its reads"
             " stay counted"),
        ]  # fmt: skip
        os.kill(pid, signal.SIGKILL)
        made._ending.join(5)
        assert made.cause == duck.DEADLINE and manifest.stuck() == 0


# A stand-in reader that starts two processes at its first statement, each holding its pipes, one of them
# in a session of its own; it answers nothing more, and then ends or stays (``then``).
FORKED = """
import os, time
source, sink = os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb")
reader.read(source, {{b"J": reader.REQUEST_MAX}})
sink.write(reader.pack(dict(ready=True, version="1.5.5", limit=None, sockets=False, inherited=0), 2**20))
sink.flush()
reader.read(source, {{b"J": reader.REQUEST_MAX}})
started = []
for leaves in (False, True):
    if (pid := os.fork()) == 0:
        if leaves:
            os.setsid()
        time.sleep(60)
        os._exit(0)
    started.append(pid)
with open({pids!r} + ".new", "w") as out:
    out.write(" ".join(map(str, started)))
os.rename({pids!r} + ".new", {pids!r})
{then}
"""


@pytest.mark.parametrize(
    ("then", "cause", "status"),
    [("os._exit(0)", "exit 0", 0), ("time.sleep(60)", duck.DEADLINE, -signal.SIGKILL)],
    ids=["it ends by itself", "it is killed for its deadline"],
)
async def test_a_reader_s_end_is_learnt_from_its_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, then: str, cause: str,
    status: int,
) -> None:  # fmt: skip
    """A reader's end is its process's, learnt by reaping it, and not the end of its output, which a
    process it started may hold open. A reader that started two such processes, under a pass's statement
    with no timeout, and then ended by itself or answered nobody and was killed: the statement is Died,
    ``SORT`` is free, the spill directory gone, nothing stays counted and the end is charged, although
    its output never ended. The process of its group went with it; one that left its group lives on, and
    holds no statement of this process's."""
    base, pids = tmp_path.resolve(), tmp_path.resolve() / "pids"
    root, key = str(base / "files" / "root"), str(base)
    monkeypatch.setattr(duck, "WATCH_S", 0.1)
    monkeypatch.setattr(duck, "DRAIN_S", 0.2)
    monkeypatch.setattr(manifest, "STAT_S", 0.5)
    stand_in(base, monkeypatch, FORKED.format(pids=str(pids), then=then))

    async def unchanged() -> None:
        return None

    async def passed(made: duck.Instance) -> None:
        async with duck.SORT.held():
            await made.cursor(root, key, "side a").execute("SELECT 1", watch=unchanged)

    with ducked.started(base) as made:
        started = time.monotonic()
        with pytest.raises(manifest.Busy) as died:
            await asyncio.wait_for(passed(made), 10)
        made._ending.join(5)
        inside, outside = map(int, pids.read_text().split())
        try:
            assert str(died.value) == "Died" and time.monotonic() - started < 5
            assert (made.cause, made.proc.returncode) == (cause, status)
            assert duck.SORT._owner is None and not made.spill.exists() and manifest.stuck() == 0
            assert duck._charged[key] == (1, cause)
            assert _state(outside) == "S" and made._reading.is_alive()  # its output is held open still
            async with asyncio.timeout(5):
                while _state(inside) not in ("", "Z"):  # the one of its group was killed with it
                    await asyncio.sleep(0.01)
        finally:
            os.kill(outside, signal.SIGKILL)
        made._reading.join(5)
        assert not made._reading.is_alive()
    assert _logged(caplog) == [("ERROR", CHARGED % (cause, 1))]


async def test_a_pass_whose_reader_ended_before_its_sort_is_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass whose reader's process ended between its cursor's opening and its check of the spill's
    room (the kernel, an operator, another run's file) is Died, an outage the worker pauses on, and not a
    spill without room, which fails the run: the reader's directory went with its process."""
    (one,), found = await _sides(tmp_path.resolve(), "one")
    engine, exports, call = duck.Files(), found.get("exports"), manifest.call

    async def racing(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn is duck._free and (made := duck._instance) is not None:
            os.kill(made.proc.pid, signal.SIGKILL)
            made._ending.join(5)
        return await call(fn, *args, **kwargs)

    monkeypatch.setattr(manifest, "call", racing)
    with pytest.raises(tables.TableError) as paused:
        async with engine.snapshot(exports) as session:
            await engine.describe(session, None, "t", side=one)
            await engine.scan(session, one, None)
    assert one["sort_bytes"] > 0 and (type(paused.value), str(paused.value)) == (manifest.Busy, "Died")
    monkeypatch.undo()
    assert len(await scanned(found, one)) == 2 and not duck._doubted(duck._set(one))


def test_an_end_s_cause_is_its_signal_s_name_or_its_exit(tmp_path: Path) -> None:
    """How an end is named in a failed run, a refused plan, the check's text and the log: a signal by its
    name, one that has none by its number (35, on Linux and on macOS), an exit by its code."""
    assert [duck._cause(status) for status in (-35, -signal.SIGKILL, -signal.SIGSEGV, 0, 1)] == [
        "signal 35", "SIGKILL", "SIGSEGV", "exit 0", "exit 1",
    ]  # fmt: skip


# --- ends and their accounting -------------------------------------------------------------------------


async def test_a_fault_ends_the_reader_s_process_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A fault under a statement (SIGSEGV, sent mid-statement; on macOS without ``DS_FAULTS=1``
    the signal of ``faults`` that is no crash there) ends the reader's process and no other. The statement
    is Busy('Died') and the end is charged to its side's files, which alone were in flight; they are read
    apart next, in a reader of the task's own, and the third such end is ReaderDied, as is
    every later read of them, for which nothing is sent. This process lives, and a new reader reads
    another side's files."""
    (bad, good), found = await _sides(tmp_path.resolve(), "bad", "good")
    engine, shared, sent = duck.Files(), await duck.configured(), faults.FAULT.name
    met, readers = [], []
    for _ in range(duck.DEATHS):
        session = await engine.connect(found.get("exports"), timeout_s=30)
        met.append(await _ended(session, bad, faults.FAULT))
        readers.append(session.cursor._instance)  # type: ignore[union-attr]
        await session.close()
    assert [(type(exc), str(exc)) for exc in met] == [
        (manifest.Busy, "Died"), (manifest.Busy, "Died"), (tables.ReaderDied, _f12(sent)),
    ]  # fmt: skip
    assert failure(met[2]) == f"ReaderDied: {_f12(sent)}"
    assert readers[0] is shared and shared not in readers[1:] and readers[1] is not readers[2]  # apart
    assert [made.cause for made in readers] == [sent] * 3 and not any(m.spill.exists() for m in readers)
    assert _logged(caplog) == [("ERROR", CHARGED % (sent, n)) for n in (1, 2, 3)]
    assert len(await scanned(found, good)) == 2
    last = duck._instance
    assert last is not None and last not in readers and ducked.strays() == []
    with pytest.raises(tables.ReaderDied) as refused:
        await scanned(found, bad)
    assert str(refused.value) == _f12(sent) and duck._instance is last and last.cause is None
    assert duck._aparts == {} and len(_logged(caplog)) == 3


@faults.real
async def test_the_crafted_page_is_contained(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The kept mutant, one page of which a fuzz changed. Its plan passes: its
    footer is walked, its rows counted and its key read. A scan reads the changed page, and ends as a read,
    as a closed refusal or with the reader's process, three times and then as ReaderDied: this process lives
    through each, no text of DuckDB's and no file's name is said, and a new reader reads another file. On
    macOS arm64 with DuckDB 1.5.5 each scan ends the reader with SIGBUS or SIGSEGV, as observed: a crash
    to that system, so the test runs there with ``DS_FAULTS=1`` alone."""
    base = tmp_path.resolve()
    (clean,), found = await _sides(base, "clean")
    (base / "files" / "root" / "crafted.parquet").write_bytes(crafted.PAGE)
    table = {"name": "t", "keyColumns": ["id"], "file": {"path": "crafted.parquet", "format": "parquet"}}
    plan, found = await planned(base, table)
    assert plan["rows"] == 4 and list(plan["columns"].values()) == ["v"]
    ends: list[Any] = []
    for _ in range(duck.DEATHS):
        try:
            ends.append(await scanned(found, plan))
        except tables.TableError as exc:
            ends.append(exc)
    f12 = re.escape(_f12("CAUSE")).replace("CAUSE", "(SIGBUS|SIGSEGV)")
    for end in ends:
        closed = isinstance(end, tables.TargetRefused) and str(end) in reader.NAMES
        assert isinstance(end, list) or closed or str(end) == "Died" or re.fullmatch(f12, str(end))
    if (sys.platform, platform.machine(), version("duckdb")) == ("darwin", "arm64", "1.5.5"):
        assert [(type(end), str(end)) for end in ends[:2]] == [(manifest.Busy, "Died")] * 2
        assert isinstance(ends[2], tables.ReaderDied) and re.fullmatch(f12, str(ends[2]))
        with pytest.raises(tables.ReaderDied) as refused:
            await read(found, plan, "1")
        assert str(refused.value) == str(ends[2])
        assert [level for level, _ in _logged(caplog)] == ["ERROR"] * 3
    assert "crafted" not in caplog.text and str(base) not in caplog.text
    assert len(await scanned(found, clean)) == 2 and (await read(found, clean, "1"))["1"]
    assert duck._instance is not None and duck._instance.cause is None and ducked.strays() == []


FUZZ = int(os.environ.get("DS_FUZZ", "100"))  # the mutants of each kind a run of the fuzz reads


@pytest.mark.parametrize("pages", [False, True], ids=["footers", "pages"])
async def test_fuzzed_files_end_no_process_but_a_reader(tmp_path: Path, pages: bool) -> None:
    """Mutants of the tests' own Parquet files that the footer walk accepts, 100
    whose footer and 100 whose pages hold one to five changed bytes (``DS_FUZZ=<n>``: n of each), and,
    where real faults are made (``faults.REAL``), the kept mutant, each through ``ds target check`` with
    its count, a plan, a scan, a census and a per-key read in this process. Whatever a step meets is a
    closed code, no scanned key is missing from its own read, a reader's process may end and this one does
    not: it reads a clean file afterwards. What was met is printed."""
    base = tmp_path.resolve()
    kept = crafted.PAGE if pages and faults.REAL else None
    met, ends, refused = await fuzzed.run(base, 7 + pages, FUZZ, pages=pages, kept=kept)
    print(f"\n{sum(met.values())} mutants of {'pages' if pages else 'footers'} ({refused} more refused by")
    print(f"the walk); readers' ends by seed file and cause: {dict(ends)}; this process lives")
    for outcome, count in met.most_common():
        print(f"{count:7d}  {outcome}")
    assert not [outcome for outcome in met if any(unseen in outcome for unseen in fuzzed.UNSEEN)]
    (clean,), found = await _sides(base, "clean")
    assert len(await scanned(found, clean)) == 2 and (await read(found, clean, "1"))["1"]
    assert duck._instance is not None and duck._instance.cause is None and ducked.strays() == []


async def test_ends_are_charged_when_one_set_was_in_flight(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Who an end is accounted to. The sets with a request unanswered in the reader then; if none, those
    of its cursors whose statement was not read to its end; if none, nobody. One set, whatever the number
    of its statements, is charged; several are each suspect, and none is charged. The log names the cause,
    a fault of the reader's own by its class and line, and never a set."""
    plans, found = await _sides(tmp_path.resolve(), "one", "two", "three", "four", "five", "six")
    one, two, three, four, five, six = plans
    engine, exports = duck.Files(), found.get("exports")
    keys = [duck._set(plan) for plan in plans]

    async def opened() -> Any:
        return await engine.connect(exports, timeout_s=30)

    first, second = await opened(), await opened()  # two sets in flight together
    beside = asyncio.create_task(second.send(two, SLOW, {}))
    await _sent(second)
    assert str(await _ended(first, one, signal.SIGKILL)) == "Died"
    with pytest.raises(manifest.Busy):
        await beside
    assert set(duck._suspects) >= set(keys[:2]) and not set(duck._charged) & set(keys)
    half = await opened()  # a result half read, no request unanswered
    await half.send(three, "SELECT i FROM range(10) t(i)", {})
    assert await half.cursor.fetch(1) == [(0,)]
    made = half.cursor._instance
    os.kill(made.proc.pid, signal.SIGKILL)
    made._ending.join(5)
    assert duck._charged[keys[2]] == (1, "SIGKILL")
    whole = await opened()  # a statement read to its end
    assert await whole.rows(four, "SELECT 1", {}) == [(1,)]
    made = whole.cursor._instance
    os.kill(made.proc.pid, signal.SIGKILL)
    made._ending.join(5)
    assert not duck._doubted(keys[3])
    third, fourth = await opened(), await opened()  # two statements of one set
    beside = asyncio.create_task(fourth.send(five, SLOW, {}))
    await _sent(fourth)
    assert str(await _ended(third, five, signal.SIGTERM)) == "Died"
    with pytest.raises(manifest.Busy):
        await beside
    assert duck._charged[keys[4]] == (1, "SIGTERM") and keys[4] not in duck._suspects
    own = await (await opened())._opened(six)  # a request the reader's own Python faults on
    _, slot = own._instance.ask(own, op="no such", cursor=own._number)
    with pytest.raises(manifest.Busy):
        slot.result(5)
    own._instance._ending.join(5)
    assert duck._charged[keys[5]] == (1, "exit 1")
    for session in (first, second, half, whole, third, fourth):
        await session.close()
    await own.close()
    said = _logged(caplog)
    fault = r"exit 1: KeyError at reader\.py line [0-9]+"
    assert re.fullmatch(re.escape(CHARGED % ("FAULT", 1)).replace("FAULT", fault), said.pop()[1])
    assert said == [
        ("ERROR", "the file reader's process ended (SIGKILL) with 2 statements in flight: each one's files"
         " are read apart next"),
        ("ERROR", CHARGED % ("SIGKILL", 1)),
        ("WARNING", UNHELD % "SIGKILL"),
        ("ERROR", CHARGED % ("SIGTERM", 1)),
    ]  # fmt: skip
    assert ".csv" not in caplog.text and str(tmp_path) not in caplog.text


async def test_a_batch_read_ahead_is_in_flight(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A scan's cursor that was answered a full batch has its next one read ahead, so its file set is
    in flight beside whatever request waits: an end then makes both suspect and charges neither. A reader
    apart reads nothing ahead, so that an end during a request there has one set in flight."""
    (one, two), found = await _sides(tmp_path.resolve(), "one", "two")
    engine, shared = duck.Files(), await duck.configured()
    first, second = [await engine.connect(found.get("exports"), timeout_s=30) for _ in range(2)]
    long = "SELECT i FROM range(30000) t(i)"  # three batches
    await first.send(one, long, {})
    assert first.cursor is not None and len(await first.cursor.batch(duck.BATCH)) == duck.BATCH
    assert shared.ahead and first.cursor._ahead and first.cursor._flight not in shared._pending
    assert isinstance(await _ended(second, two), manifest.Busy)  # a statement of the other set, in flight
    assert _logged(caplog) == [
        ("ERROR", f"the file reader's process ended ({faults.QUIET.name}) with 2 statements in flight: each"
                  " one's files are read apart next")
    ]  # fmt: skip
    keys = {duck._set(one), duck._set(two)}
    assert keys <= set(duck._suspects) and not keys & set(duck._charged)
    await first.close()
    await second.close()
    apart = await engine.connect(found.get("exports"), timeout_s=30)
    await apart.send(one, long, {})
    assert apart.cursor is not None and len(await apart.cursor.batch(duck.BATCH)) == duck.BATCH
    assert apart.cursor._instance is not duck._instance and not apart.cursor._instance.ahead
    assert not apart.cursor._ahead
    await apart.close()
    assert ducked.strays() == []


async def test_suspects_are_read_apart_and_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A suspect set's cursor opens in a reader of its task's own, under ALONE, and every cursor the
    task opens meanwhile opens there too; another task waits for ALONE, STAT_S at most (Busy('Alone')). The
    reader is killed when the task's cursors in it are closed, and with it alive the sets read there are
    suspect no more: they are read in the shared reader again. A charged set is read apart however clean
    its reads."""
    (one, two, three), found = await _sides(tmp_path.resolve(), "one", "two", "three")
    engine, exports = duck.Files(), found.get("exports")
    keys = [duck._set(plan) for plan in (one, two, three)]
    for key in keys[:2]:
        monkeypatch.setitem(duck._suspects, key, None)
    monkeypatch.setattr(manifest, "STAT_S", 0.3)
    shared = await duck.configured()

    async def elsewhere(plan: tables.Plan) -> tuple[list[Any], duck.Instance]:
        session = await engine.connect(exports, timeout_s=30)
        try:
            return await session.rows(plan, "SELECT 1", {}), session.cursor._instance
        finally:
            await session.close()

    first, beside = [await engine.connect(exports, timeout_s=30) for _ in range(2)]
    assert await first.rows(one, "SELECT 1", {}) == [(1,)]
    apart = first.cursor._instance
    assert apart is not shared and first.apart is not None and first.apart.alone.locked()
    assert await beside.rows(three, "SELECT 1", {}) == [(1,)] and beside.cursor._instance is apart
    with pytest.raises(manifest.Busy) as busy:
        await asyncio.create_task(elsewhere(two))
    assert str(busy.value) == "Alone"
    await beside.close()
    assert (apart.held, apart.dropped) == (1, False)
    await first.close()
    apart._ending.join(5)
    assert apart.cause == "SIGKILL" and not ducked.strays() and duck._aparts == {}
    assert not duck._doubted(keys[0]) and duck._doubted(keys[1]) and _logged(caplog) == []
    assert (await elsewhere(one))[1] is shared  # suspect no more
    rows, other = await asyncio.create_task(elsewhere(two))  # ALONE is free
    assert rows == [(1,)] and other not in (shared, apart) and not duck._doubted(keys[1])
    kept = await engine.connect(exports, timeout_s=30)  # a per-key reader's, its cursor in the shared one
    assert len(await engine.rows(kept, three, {"1": [["1"]]})) == 1 and kept.cursor._instance is shared
    monkeypatch.setitem(duck._charged, keys[2], (1, "SIGBUS"))
    assert len(await engine.rows(kept, three, {"1": [["1"]]})) == 1 and kept.cursor is None  # apart, and left
    readers = [(await elsewhere(three))[1] for _ in range(2)]
    assert shared not in readers and readers[0] is not readers[1] and duck._charged[keys[2]] == (1, "SIGBUS")
    assert (await elsewhere(one))[1] is shared and shared.cause is None


async def _passed(found: TargetRegistry, a: tables.Plan, b: tables.Plan) -> tuple[list[int], list[Any]]:
    """A scan pass of a pair as the runner opens one: SORT held by its task, both sides' catalogues read
    before either cursor opens, then each side sorted and read; each side's rows, and its reader."""
    engine, exports = duck.Files(), found.get("exports")
    async with duck.SORT.held(), engine.snapshot(exports) as first, engine.snapshot(exports) as second:
        sides = ((first, a), (second, b))
        for session, plan in sides:
            await engine.describe(session, None, "t", side=plan)
        batches = [await engine.scan(session, plan, None) for session, plan in sides]
        readers = [session.cursor._instance for session, _ in sides]  # type: ignore[union-attr]
        return [sum([len(batch) async for batch in found_]) for found_ in batches], readers


async def test_a_pair_of_suspects_shares_one_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A scan pass learns of a suspect or charged side when its sides' catalogues are read, before a
    cursor opens: both its sides are then read in the one reader of its own, under SORT, as are a pair that
    reads the same files and the two sides of a plan, one after the other. Each completes when its reads
    are clean, none waits for itself (Alone), and a pass whose reader ends a third time under one side's
    statement fails as ReaderDied."""
    base = tmp_path.resolve()
    (one, two), found = await _sides(base, "one", "two")
    two = two | {"pinned": two["pinned"] | {"side": "b"}}
    keys = [duck._set(plan) for plan in (one, two)]
    shared = await duck.configured()
    monkeypatch.setattr(manifest, "STAT_S", 0.5)  # a wait for ALONE would be Busy('Alone')
    for key in keys:  # both suspect
        monkeypatch.setitem(duck._suspects, key, None)
    rows, readers = await _passed(found, one, two)
    assert rows == [2, 2] and readers[0] is readers[1] is not shared and readers[0].killed
    assert not any(map(duck._doubted, keys)) and (await _passed(found, one, two))[1] == [shared, shared]
    monkeypatch.setitem(duck._charged, keys[1], (1, "SIGBUS"))  # side b alone
    rows, readers = await _passed(found, one, two)
    assert rows == [2, 2] and readers[0] is readers[1] is not shared and not duck.SORT.apart
    monkeypatch.setitem(duck._suspects, keys[0], None)  # a pair that reads the same files
    rows, readers = await _passed(found, one, one)
    assert rows == [2, 2] and readers[0] is readers[1] is not shared and not duck._doubted(keys[0])
    for key in keys:  # a plan whose two sides are suspect
        monkeypatch.setitem(duck._suspects, key, None)
    table = csv_table("one.csv", ["id", "v"])
    other = {"role": "new", "target": "exports", "table": csv_table("two.csv", ["id", "v"])}
    made = await tables.planning(found, compile_spec(file_spec(table, other), targets=found))
    assert made.plan is not None and duck._set(tables.pins(made.plan, made.pinned)["b"]) == keys[1]
    assert not duck._doubted(keys[0]) and duck._aparts == {} and _logged(caplog) == []
    # The pass's reader ends under side b's sort, a third time: ReaderDied, for side b.
    monkeypatch.setitem(duck._charged, keys[1], (2, "SIGBUS"))
    ask = duck.Instance.ask

    def faulting(self: duck.Instance, by: Any = None, /, **request: Any) -> Any:
        answer = ask(self, by, **request)
        if by is not None and by._key == keys[1] and " ORDER BY " in request.get("statement", ""):
            os.kill(self.proc.pid, faults.QUIET)
        return answer

    monkeypatch.setattr(duck.Instance, "ask", faulting)
    with pytest.raises(tables.ReaderDied) as died:
        await _passed(found, one, two)
    sent = faults.QUIET.name
    assert str(died.value) == _f12(sent, "b") and duck._charged[keys[1]] == (3, sent)
    assert not duck._doubted(keys[0]) and duck._aparts == {} and not duck.SORT.apart
    assert _logged(caplog) == [("ERROR", CHARGED % (sent, 3))]
    async with asyncio.timeout(5):
        while ducked.strays():
            await asyncio.sleep(0.01)


async def test_a_per_key_read_does_not_wait_for_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass reads apart under SORT, a per-key read under ALONE: neither waits for the other. A per-key
    reader closes the cursor it opened apart after each read, so ALONE is free between its reads."""
    (one, two), found = await _sides(tmp_path.resolve(), "one", "two")
    engine, exports = duck.Files(), found.get("exports")
    for plan in (one, two):
        monkeypatch.setitem(duck._charged, duck._set(plan), (1, "SIGBUS"))
    monkeypatch.setattr(manifest, "STAT_S", 0.5)
    opened, done = asyncio.Event(), asyncio.Event()

    async def passing() -> tuple[int, duck.Instance]:
        async with engine.snapshot(exports) as session:
            await engine.describe(session, None, "t", side=one)
            batches = await engine.scan(session, one, None)
            opened.set()
            await done.wait()
            return sum([len(batch) async for batch in batches]), session.cursor._instance

    scan = asyncio.create_task(passing())
    await opened.wait()
    session = await engine.connect(exports, timeout_s=30)
    for _ in range(2):  # beside the pass's reader, each read in a reader of its own
        assert len(await engine.rows(session, two, {"1": [["1"]]})) == 1
        assert session.cursor is None and duck._aparts.keys() == {scan}
    done.set()
    rows, apart = await scan
    assert rows == 2 and apart is not duck._instance and duck._aparts == {}


async def test_an_innocent_run_is_not_failed_by_another_s_files(tmp_path: Path) -> None:
    """A side whose statement was in flight beside another's when the shared reader ended loses that
    read and is read apart once: its read there is clean, it is suspect no more and it is never charged,
    while the other side's files end their own reader three times and are read no more."""
    (bad, innocent), found = await _sides(tmp_path.resolve(), "bad", "innocent")
    engine, exports = duck.Files(), found.get("exports")
    keys = [duck._set(plan) for plan in (bad, innocent)]
    first, second = [await engine.connect(exports, timeout_s=30) for _ in range(2)]
    beside = asyncio.create_task(second.send(innocent, SLOW, {}))
    await _sent(second)
    assert str(await _ended(first, bad)) == "Died"
    with pytest.raises(manifest.Busy) as lost:
        await beside
    assert str(lost.value) == "Died" and all(map(duck._doubted, keys))
    await first.close()
    await second.close()
    assert (await read(found, innocent, "1"))["1"] and not duck._doubted(keys[1])  # apart, and clean
    met = []
    for _ in range(duck.DEATHS):
        session = await engine.connect(exports, timeout_s=30)
        met.append(await _ended(session, bad))
        await session.close()
    assert [type(exc) for exc in met] == [manifest.Busy, manifest.Busy, tables.ReaderDied]
    assert keys[1] not in duck._charged and not duck._doubted(keys[1])
    assert len(await scanned(found, innocent)) == 2 and (await read(found, innocent, "2"))["2"]
    with pytest.raises(tables.ReaderDied):
        await read(found, bad, "1")


async def test_a_condemned_set_is_refused_with_no_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A set charged three ends is ReaderDied before anything is sent, until this process restarts:
    a scan and a per-key read fail their run, a new plan of the same files is refused, at its first statement
    or at its probe's (which was "the database refuses the filter or key map"), each with the last end's
    cause, and no reader is started or ended for them."""
    base = tmp_path.resolve()
    (one,), found = await _sides(base, "one")
    monkeypatch.setitem(duck._charged, duck._set(one), (duck.DEATHS, "SIGBUS"))
    shared = await duck.configured()
    sent, ask = [], duck.Instance.ask
    monkeypatch.setattr(
        duck.Instance, "ask", lambda self, *args, **request: sent.append(1) or ask(self, *args)
    )
    for reading in (scanned(found, one), read(found, one, "1")):
        with pytest.raises(tables.ReaderDied) as refused:
            await reading
        assert (
            str(refused.value) == _f12("SIGBUS") and failure(refused.value) == f"ReaderDied: {_f12('SIGBUS')}"
        )
    monkeypatch.undo()
    monkeypatch.setitem(duck._charged, duck._set(one), (duck.DEATHS, "SIGBUS"))
    with pytest.raises(ValueError) as planning:
        await planned(base, csv_table("one.csv", ["id", "v"]))
    assert str(planning.value) == _f12("SIGBUS") and not isinstance(planning.value, tables.NotReady)
    assert sent == [] and duck._instance is shared and shared.cause is None and ducked.strays() == []
    monkeypatch.undo()
    described = duck.Files.describe

    async def describing(self: duck.Files, *args: Any, **kwargs: Any) -> Any:
        found_ = await described(self, *args, **kwargs)
        monkeypatch.setitem(duck._charged, duck._set(one), (duck.DEATHS, "SIGSEGV"))  # before its probe
        return found_

    monkeypatch.setattr(duck.Files, "describe", describing)
    with pytest.raises(ValueError) as planning:
        await planned(base, csv_table("one.csv", ["id", "v"]))
    assert str(planning.value) == _f12("SIGSEGV") and not isinstance(planning.value, tables.NotReady)
    assert duck._instance is shared and duck._aparts == {} and _logged(caplog) == []


async def test_a_check_is_told_its_reader_s_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ds target check``, a process of its own, ends at its reader's first end with a text of its own
    that names the cause; a reader apart it does not get is not ready, worded for the target."""
    base = tmp_path.resolve()
    _, found = await _sides(base, "one")
    exports, ask = found.get("exports"), duck.Instance.ask

    def faulting(self: duck.Instance, by: Any = None, /, **request: Any) -> Any:
        answer = ask(self, by, **request)
        if request["op"] == "execute":
            os.kill(self.proc.pid, faults.QUIET)
        return answer

    with monkeypatch.context() as patched:
        patched.setattr(duck.Instance, "ask", faulting)
        with pytest.raises(ValueError) as ended:
            await duck.check(exports, "one.csv", "csv")
    assert str(ended.value) == (
        f"target exports: the file reader's process ended ({faults.QUIET.name}) while it read these files"
    ) and not isinstance(ended.value, tables.NotReady)
    monkeypatch.setattr(manifest, "STAT_S", 0.3)
    alone = duck.ALONE.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with alone:  # another task's reader apart: its files, charged once, are read apart
        with pytest.raises(tables.NotReady) as busy:
            await duck.check(exports, "one.csv", "csv")
    assert (busy.value.code, str(busy.value)) == (
        "planning", "target exports: the file reader did not take its files' work (Alone): try again",
    )  # fmt: skip
    monkeypatch.undo()
    assert (await duck.check(exports, "one.csv", "csv"))[0].startswith("one.csv (csv): 1 file(s)")
    assert duck._aparts == {} and not alone.locked()


PARENT = """
import asyncio, sys
from pathlib import Path
from deltasleuth.connectors.tables import duck
base, how = Path(sys.argv[1]), sys.argv[2]
(base / "files" / "root").mkdir(parents=True, exist_ok=True)
(base / "outside.csv").write_text("id")
made = duck._spawned(base / "spill", [str(base / "files" / "root")], base / "outside.csv", 256, 1024, 2)
async def read():
    cursor = made.cursor()
    await cursor.execute("SELECT 1")
    print(made.proc.pid, flush=True)
    if how == "sort":
        await cursor.execute(duck.SPILL_SORT.replace("200000", "300000000"))
    elif how != "exit":
        await asyncio.sleep(60)
asyncio.run(read())
"""


LEAVING = """
import asyncio, atexit, sys
from pathlib import Path
held = []
atexit.register(lambda: print("at its exit:", held[0].proc.returncode, held[0].spill.exists(), flush=True))
from deltasleuth.connectors.tables import duck  # what it registers after runs before
base = Path(sys.argv[1])
(base / "files" / "root").mkdir(parents=True, exist_ok=True)
(base / "outside.csv").write_text("id")
held.append(duck._spawned(base / "spill", [str(base / "files" / "root")], base / "outside.csv", 256, 1024, 2))
async def read():
    cursor = held[0].cursor()
    await cursor.execute("SELECT 42")
    print(await cursor.fetch(1), held[0].proc.returncode, flush=True)
asyncio.run(read())
"""


def test_a_process_reaps_its_reader_as_it_exits(tmp_path: Path) -> None:
    """A ``ds`` process that exits kills and reaps the readers it still has, and their spill
    directories go: one left to end by itself at the end of its input would be left to whatever started
    that process, which may wait for none (``docker compose exec``: a zombie entry a command)."""
    ran = subprocess.run(
        [sys.executable, "-c", LEAVING, str(tmp_path.resolve())],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert (ran.returncode, ran.stderr) == (0, "")
    assert ran.stdout.splitlines() == ["[(42,)] None", f"at its exit: {-signal.SIGKILL} False"]
    assert list((tmp_path / "spill").iterdir()) == []


def test_no_reader_outlives_its_parent(tmp_path: Path) -> None:
    """The end of its input is the reader's own, whatever ended its parent and whatever it
    was doing: an exit, SIGTERM, SIGKILL while it is idle and in the middle of a sort. Its spill directory,
    whose lock the kernel freed, goes with the next sweep."""
    base = tmp_path.resolve()
    for how, sent in (
        ("exit", None),
        ("idle", signal.SIGTERM),
        ("idle", signal.SIGKILL),
        ("sort", signal.SIGKILL),
    ):
        parent = subprocess.Popen(
            [sys.executable, "-c", PARENT, str(base), how], cwd=ROOT, stdout=subprocess.PIPE, text=True
        )
        assert parent.stdout is not None
        pid = int(parent.stdout.readline())
        if sent is not None:
            time.sleep(0.5)  # the sort is under way
            assert os.kill(pid, 0) is None
            parent.send_signal(sent)
        assert parent.wait(30) == (-sent if sent else 0)
        ended = time.monotonic() + 10
        while time.monotonic() < ended:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"the reader outlived its parent ({how}, {sent})")
    # Each start swept what the parent before it left; the last one's goes with this sweep.
    assert len(list((base / "spill").iterdir())) == 1 and manifest.sweep(base / "spill") == 1
