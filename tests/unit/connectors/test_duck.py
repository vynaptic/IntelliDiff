"""Statements on the locked instance (docs/mvp/FILES.md §3's U5 to U7, U10 and U11; §10): bound reader
arguments, streamed fetches, the key order, interrupting a sort, one budget for every cursor. What DuckDB
itself does is observed on a connection in this process (``locked``); the product's cursors are a reader's,
a process of its own (``started``; docs/mvp/READER.md)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deltasleuth.connectors import tables
from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.tables import (
    FilesChanged,
    Refusal,
    StatementTimeout,
    TableConnector,
    TargetRefused,
    duck,
    manifest,
)
from deltasleuth.core.canonical.observe import _fetch_failure
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.types import ExecutionConfig, Side, SideConfig
from tests.unit.connectors import ducked
from tests.unit.connectors.ducked import (
    csv_table,
    file_spec,
    locked,
    planned,
    read,
    registry,
    scanned,
    unrestricted,
)
from tests.unit.connectors.ducked import settings as reader_settings

CSV_OPTIONS = (
    "auto_detect=false, columns=$columns, header=$header, skip=$skip, delim=$delim, new_line=$new_line,"
    " quote=$quote, escape=$escape, encoding=$encoding, compression=$compression, nullstr=$null,"
    " allow_quoted_nulls=false, null_padding=false, union_by_name=false, filename=false,"
    " max_line_size=2097152, ignore_errors=true, parallel=$parallel"
)
BOUND = {
    "columns": {"id": "VARCHAR", "name": "VARCHAR"}, "header": True, "skip": 1, "delim": "|",
    "new_line": "\\r\\n", "quote": "'", "escape": "\\", "encoding": "latin-1", "compression": "gzip",
    "null": "-", "parallel": False,
}  # fmt: skip


def test_reader_arguments_are_bound(tmp_path: Path) -> None:
    """U5: the file list and every reader option bind as parameters, so no spec text is spliced into a
    statement."""
    import gzip

    with locked(tmp_path) as made:
        root = tmp_path / "files" / "root"
        (root / "a.csv.gz").write_bytes(
            gzip.compress("junk line\r\nid|name\r\n1|caf\xe9\r\n2|-\r\n3|'it\\'s'\r\n".encode("latin-1"))
        )
        (root / "b.csv.gz").write_bytes(gzip.compress(b"junk\r\nid|name\r\n4|'-'\r\n"))
        files = [str(root / "a.csv.gz"), str(root / "b.csv.gz")]
        rows = made.con.execute(f"SELECT * FROM read_csv($files, {CSV_OPTIONS})", BOUND | {"files": files})  # noqa: S608 (constants)
        assert rows.fetchall() == [("1", "café"), ("2", None), ("3", "it's"), ("4", "-")]
        unrestricted().execute(
            f"COPY (SELECT 1 AS id, 'x'::BLOB AS b) TO '{root}/c.parquet' (FORMAT parquet)"
        )
        parquet = made.con.execute(
            "SELECT id, b FROM read_parquet($files, union_by_name=false, filename=false,"
            " binary_as_string=$b)",
            {"files": [str(root / "c.parquet")], "b": True},
        )
        assert parquet.fetchall() == [(1, "x")]


def test_scan_memory_is_bounded(tmp_path: Path) -> None:
    """U6: execute then fetchmany streams: a process that reads 5,000,000 rows of 132 bytes (660 MB) a batch
    at a time stays far below what holding them would take."""
    script = (
        "import resource, sys\nfrom pathlib import Path\nfrom tests.unit.connectors.ducked import locked\n"  # noqa: S608 (constants)
        f"with locked(Path({str(tmp_path)!r}), memory_mib=64, spill_mib=1) as made:\n"
        "    cursor = made.con.cursor()\n"
        "    cursor.execute(\"SELECT md5(CAST(i AS VARCHAR)) || repeat('y', 100)\"\n"
        '                   " FROM range(5000000) t(i)")\n'
        "    rows = 0\n"
        "    while batch := cursor.fetchmany(10000):\n"
        "        rows += len(batch)\n"
        "peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "print(rows, peak if sys.platform == 'darwin' else peak * 1024)\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=Path(__file__).parents[3],
    )
    rows, peak = map(int, ran.stdout.split())
    assert rows == 5_000_000 and peak < 400 * 2**20, peak


@settings(max_examples=50, deadline=None)
@given(st.lists(st.text(st.characters(exclude_categories=["Cs"]), max_size=6), max_size=40))
def test_key_order_corpus(keys: list[str]) -> None:
    """U7: ORDER BY encode(k) is the UTF-8 bytes' order, which is code-point order, whatever a collation
    would say: case, accents, emoji, combining marks, characters beyond the BMP, NUL."""
    con = unrestricted()
    fixed = ["B", "Z", "a", "a ", "e", "é", "é", "😀", "\uff5e", "", "\x00", "\U0010fffd"]
    ordered = con.execute("SELECT k FROM (SELECT unnest($k) AS k) ORDER BY encode(k)", {"k": fixed + keys})
    assert [row[0] for row in ordered.fetchall()] == sorted(fixed + keys, key=lambda k: k.encode())


async def test_cancel_ends_a_sort(tmp_path: Path) -> None:
    """U10: interrupt() ends a sort within 5 s, through the cursor's deadline as through a caller's cancel."""
    with ducked.started(tmp_path) as made:
        cursor = made.cursor()
        sort = "SELECT md5(CAST(i AS VARCHAR)) AS k FROM range(300000000) t(i) ORDER BY encode(k)"
        started = time.monotonic()
        with pytest.raises(StatementTimeout):
            await cursor.execute(sort, timeout_s=1)
        assert time.monotonic() - started < 6
        await cursor.execute("SELECT 42")
        assert await cursor.fetch(1) == [(42,)]  # usable again
        task = asyncio.create_task(cursor.execute(sort))
        await asyncio.sleep(1)
        cancelled = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await cursor.execute("SELECT 1")  # the interrupted sort no longer holds the cursor
        assert time.monotonic() - cancelled < 5


def test_one_budget(tmp_path: Path) -> None:
    """U11: the cursors of one instance share its memory, threads and spill limit: each reads the
    instance's, and one cursor's sort spills where another sees it."""
    with locked(tmp_path, memory_mib=duck.SELFTEST_MIB, spill_mib=512, threads=2) as made:
        cursors = [made.con.cursor() for _ in range(3)]
        limits = {
            c.execute("SELECT current_setting('memory_limit'), current_setting('threads'),"
                      " current_setting('max_temp_directory_size')").fetchone() for c in cursors
        }  # fmt: skip
        assert limits == {("80.0 MiB", 2, "512.0 MiB")}
        seen, stop = [0], threading.Event()

        def watch() -> None:
            while not stop.wait(0.001):
                seen[0] = max(seen[0], cursors[1].execute(
                    "SELECT coalesce(sum(size), 0) FROM duckdb_temporary_files()").fetchone()[0])  # fmt: skip

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            sorted_ = cursors[0].execute(duck.SPILL_SORT)
            assert sum(len(b) for b in iter(lambda: sorted_.fetchmany(10_000), [])) == 200_000
        finally:
            stop.set()
            watcher.join()
        assert seen[0] > 0
        with pytest.raises(duckdb.InvalidInputException):  # no cursor sets a budget of its own
            cursors[2].execute("SET memory_limit = '1GiB'")
    assert os.path.isdir(tmp_path / "spill")


# --- a file side's per-key reads, sorts and errors (FILES.md §9, §10) ------------------------------------


def _root(base: Path, name: str, data: bytes) -> Path:
    path = base / "files" / "root" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


ROWS = b"id|v|s\n1|a|x\n2|b|x\n2|c|x\n3|keep|z\n3|drop|y\n"  # 2 is held twice; 3 twice, one selected


async def _connector(
    base: Path, data: bytes = ROWS, csv: dict[str, Any] | None = None, **table: Any
) -> tuple[TableConnector, SideConfig]:
    """A per-key connector of a CSV side, ``csv`` its dialect's other members, ``table`` its table's."""
    _root(base, "t.csv", data)
    plan, found = await planned(base, csv_table("t.csv", ["id", "v", "s"], **(csv or {})) | table)
    target, execution = found.get("exports"), ExecutionConfig(total_timeout_s=30)
    cfg = compile_spec(file_spec(csv_table("t.csv", ["id", "v", "s"])), targets=found).sides[Side.A]
    return TableConnector(target, execution, plan, gate=TargetGate.for_target(target)), cfg


async def test_per_key_rows_come_one_statement_a_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9: a row, 404, a key held twice (identity_ambiguous) and a key whose filter selects one of its two
    rows, which comes first even though a column is named s; a key not of its class's form and a lone
    surrogate are answered without a statement."""
    statements: list[str] = []
    real = duck.Cursor.execute

    async def execute(self: duck.Cursor, statement: str, *args: Any, **kwargs: Any) -> None:
        statements.append(statement)
        await real(self, statement, *args, **kwargs)

    table, cfg = await _connector(tmp_path, where="v <> 'drop'", keyColumns=["id"])
    monkeypatch.setattr(duck.Cursor, "execute", execute)
    await table.prefetch(["1", "2", "3", "4"], cfg)
    found = {key: await table.fetch(key, cfg) for key in ("1", "2", "3", "4")}
    assert len(statements) == 1 and "QUALIFY" in statements[0]
    assert [(o.http_status, o.error_class) for o in found.values()] == [
        (200, None), (200, "identity_ambiguous"), (200, None), (404, None),
    ]  # fmt: skip
    assert found["3"].body == b'{"v": "keep", "s": "z"}' and found["1"].headers == {}
    integer, cfg = await _connector(tmp_path / "keyed", csv={"keys": {"id": {"as": "integer"}}})
    statements.clear()
    assert (await integer.fetch("x", cfg)).http_status == 404 and statements == []  # no integer's text
    with pytest.raises(KeyRejectedError):
        await integer.fetch("\ud800", cfg)
    assert statements == []
    await table.aclose()
    await integer.aclose()


async def test_the_reader_is_handed_the_pinned_files_alone(tmp_path: Path) -> None:
    """D3, P18: an export holding a directory named ``*`` beside its partitions and a hidden one is refused
    when its run is planned (pinned, ``*/part.csv`` was a pattern to DuckDB, which then read the hidden
    directory's rows and each partition's twice); and the reader binds pinned files only, none of them a
    pattern, whoever asks."""
    for name, row in (("dt=1", "1|a|x"), ("*", "2|b|x"), (".staging", "777|NEVER-PINNED|x")):
        _root(tmp_path, f"{name}/part.csv", f"id|v|s\n{row}\n".encode())
    table = csv_table("*/part.csv", ["id", "v", "s"])
    with pytest.raises(ValueError, match=r"side a: file 1 of 2 \(in path order\) has a name outside "):
        await planned(tmp_path, table)
    expanded = unrestricted().execute(
        "SELECT id FROM read_csv($f, auto_detect=false, header=true, delim='|',"
        " columns={'id': 'VARCHAR', 'v': 'VARCHAR', 's': 'VARCHAR'}) ORDER BY id",
        {"f": [str(tmp_path / "files" / "root" / "*" / "part.csv")]},
    )
    assert expanded.fetchall() == [("1",), ("2",), ("777",)]  # U24, observed on 1.5.5: the path is a pattern
    shutil.rmtree(tmp_path / "files" / "root" / "*")
    plan, found = await planned(tmp_path, table)
    root = found.get("exports").root or ""
    assert duck._reader(plan, root)["r_files"] == [f"{root}/dt=1/part.csv"]
    found_ = await read(found, plan, "1", "777")
    assert (len(found_["1"]), len(found_["777"])) == (1, 0)
    for files in (["*/part.csv"], [".staging/part.csv"], ["dt=1/part.csv", "dt=?/part.csv"]):
        with pytest.raises(TargetRefused, match="Unpinned"):
            duck._reader(plan, root, files)
    with pytest.raises(TargetRefused, match="Unpinned"):
        duck._reader(plan | {"pinned": {"files": ["[d]t=1/part.csv"]}}, root)


async def test_a_key_missing_from_a_side_with_lines_that_are_not_rows_is_inconclusive(tmp_path: Path) -> None:
    """D6, M1: no row read holds it, and it may be on such a line: never NOT_FOUND."""
    table, cfg = await _connector(tmp_path, ROWS + b"9|a\n")
    found = await table.fetch("9", cfg)
    await table.aclose()
    assert (found.http_status, found.error_class, found.error_detail) == (
        None, "not_rows", "no row read holds the key, and 1 line(s) of this side's files were not rows",
    )  # fmt: skip
    state = _fetch_failure(found.error_class, Side.A, found.error_detail)
    assert (state.kind.value, state.reason, state.detail) == ("INCOMPLETE", "unparseable", found.error_detail)


async def test_timed_out_group_is_not_reread_by_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§6: each key alone would be a pass over the files (retry_alone is false); a temporary space met by a
    group's sort is a connect error, read once more on a fresh session, and never fails the run."""
    table, cfg = await _connector(tmp_path)
    calls: list[int] = []

    async def rows(session: Any, side: Any, asked: Any) -> list[Any]:
        calls.append(len(asked))
        raise StatementTimeout("InterruptException")

    monkeypatch.setattr(duck.Files, "rows", lambda self, *args: rows(*args))
    await table.prefetch(["1", "2"], cfg)
    assert calls == [2] and (await table.fetch("1", cfg)).error_class == "timeout"
    calls.clear()

    async def full(session: Any, side: Any, asked: Any) -> list[Any]:
        calls.append(len(asked))
        raise duck.TargetTempSpace("OutOfMemoryException")

    monkeypatch.setattr(duck.Files, "rows", lambda self, *args: full(*args))
    await table.prefetch(["1", "2"], cfg)
    assert calls == [2, 2] and (await table.fetch("2", cfg)).error_detail == "OutOfMemoryException"
    await table.aclose()


async def test_file_work_refused_while_reads_wait_is_an_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10, D14: with STUCK_MAX abandoned reads waiting, a per-key read is an outage (connect_error, its
    detail the class), never an error that fails its run: closing its cursor, which the pool does not take
    either, raises nothing; once the reads return, the side is read again."""
    table, cfg = await _connector(tmp_path)
    assert (await table.fetch("1", cfg)).http_status == 200  # its session's cursor is open
    root = str((tmp_path / "files" / "root").resolve())
    monkeypatch.setitem(manifest._stuck, root, manifest.STUCK_MAX)  # its own target's mount
    refused = await table.fetch("1", cfg)
    assert (refused.http_status, refused.error_class, refused.error_detail) == (
        None,
        "connect_error",
        "Stuck",
    )
    monkeypatch.setitem(manifest._stuck, root, 0)
    monkeypatch.setitem(manifest._stuck, "/another/target", manifest.STUCK_MAX)  # another's costs it nothing
    assert (await table.fetch("1", cfg)).http_status == 200
    await table.aclose()


async def test_a_statement_no_thread_takes_is_withdrawn_and_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10: a statement's timeout starts when a thread takes it. One that waited for a thread and then ran
    inside its timeout is answered; one no thread took within STAT_S is withdrawn: Busy, an outage that is
    read again. It is not abandoned, the instance is kept, and it is not run afterwards for nobody (it was
    counted as abandoned at its timeout, the instance discarded, and still executed later)."""
    monkeypatch.setattr(manifest, "STAT_S", 0.3)
    release, ran = threading.Event(), []
    with ducked.started(tmp_path) as made:
        cursor = made.cursor()
        held = [manifest.POOL.submit(release.wait) for _ in range(16)]  # every thread busy with work
        try:
            threading.Timer(0.2, release.set).start()
            assert await cursor._call(lambda: time.sleep(0.2) or "ran", timeout_s=0.3) == "ran"
            release.clear()
            held += [manifest.POOL.submit(release.wait) for _ in range(16)]
            started = time.monotonic()
            with pytest.raises(manifest.Busy) as busy:
                await cursor._call(ran.append, 1, timeout_s=0.1)
            assert str(busy.value) == "Busy" and 0.3 <= time.monotonic() - started < 3
            assert manifest.stuck() == 0 and not made.dropped
        finally:
            release.set()
        for job in held:
            job.result(5)
        await cursor.execute("SELECT 1", timeout_s=5)  # the instance and its cursor stand
        assert await cursor.fetch(1) == [(1,)] and ran == []
        await cursor.close()


async def test_a_short_spill_at_run_time_is_temporary_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10: an instance built anew while a run reads (after a discard, a restart) that finds its spill volume
    short of room is temporary space, which a per-key read waits for as it does before a sort: it was
    Unstarted, no table error, and failed the run. A guard that does not hold still does."""
    table, cfg = await _connector(tmp_path)

    async def short(*args: Any) -> Any:
        raise duck.Unstarted("space", 2**30)

    monkeypatch.setattr(duck, "instance", short)
    found = await table.fetch("1", cfg)
    assert (found.http_status, found.error_class, found.error_detail) == (None, "connect_error", "spill")

    async def unguarded(*args: Any) -> Any:
        raise duck.Unstarted("guard")

    monkeypatch.setattr(duck, "instance", unguarded)
    with pytest.raises(duck.Unstarted):
        await table.fetch("1", cfg)
    await table.aclose()


async def test_one_sort_at_a_time(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§10: SORT is held by one task, its pair's two file sides at once; another task waits for it, holding
    no cursor, and says so once (F1)."""
    order: list[str] = []

    async def sort(name: str, hold: float) -> None:
        async with duck.SORT.held():
            async with duck.SORT.held():  # its pair's other file side
                order.append(f"{name} in")
                await asyncio.sleep(hold)
                order.append(f"{name} out")

    await asyncio.gather(sort("first", 0.2), sort("second", 0))
    assert order == ["first in", "first out", "second in", "second out"]
    assert [record.getMessage() for record in caplog.records] == ["a sort of a file side waits for another"]


async def test_null_keys_on_a_mount_that_stops_answering_releases_the_sort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10, D14: null_keys is one more pass over the files while its scan (census, frame) holds SORT. It is
    watched as the sort is: a mount that stops answering under it is an outage within WATCH_S and the stat
    check's STAT_S, and SORT is free for the next sort (it had no timeout and no watch, and kept SORT until
    its run was cancelled)."""
    monkeypatch.setattr(duck, "WATCH_S", 0.2)
    monkeypatch.setattr(manifest, "STAT_S", 0.2)
    _root(tmp_path, "t.csv", ROWS)
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v", "s"]))
    engine, hung, dead = duck.Files(), threading.Event(), threading.Event()
    select, rooted = duck.Cursor._select, manifest._rooted

    def selecting(self: duck.Cursor, statement: str, values: Any) -> None:
        if statement.startswith("SELECT COUNT(*)"):  # null_keys' pass: the mount stops answering under it
            dead.set()
            hung.wait(20)
        select(self, statement, values)

    def rooting(root: str) -> str:
        if dead.is_set():
            hung.wait(20)
        return rooted(root)

    monkeypatch.setattr(duck.Cursor, "_select", selecting)
    monkeypatch.setattr(manifest, "_rooted", rooting)
    try:
        started = time.monotonic()
        with pytest.raises(duck.TargetUnreachable) as outage:
            async with asyncio.timeout(10), engine.snapshot(found.get("exports")) as session:
                assert sum([len(batch) async for batch in await engine.scan(session, plan, None)]) == 5
                await engine.null_keys(session, plan)
        assert str(outage.value) == "Deadline" and dead.is_set()
        async with asyncio.timeout(1), duck.SORT.held():  # the next sort does not wait
            assert time.monotonic() - started < 0.2 + 0.2 + duck.GRACE_S
    finally:
        hung.set()
    while manifest.stuck():  # the abandoned check returns
        await asyncio.sleep(0.01)


async def test_a_scan_in_flight_survives_another_target_s_hung_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10, D14: a scan of one files target has fetched its first batch when a
    statement of another hangs (its file is now a FIFO nobody writes, as a mount that dies under a read),
    passes its timeout and its grace and is given up with the reader, which is retired. The scan goes on
    with its cursor of the retired reader to its last row, its closing hash and its count of rows without a
    key (its next fetch was Busy('Dropped'): the pass lost, the side sorted again). A new session opens on a
    new reader, and the retired one's process is killed, its spill directory given up and the hung
    statement's count freed when the scan's cursor closes: no writer need end the read, and no restart
    (READER.md §8)."""
    monkeypatch.setattr(duck, "GRACE_S", 0.3)
    monkeypatch.setattr(duck, "BATCH", 2)
    base = tmp_path.resolve()
    dead, healthy = _root(base, "t.csv", ROWS), base / "files" / "b"
    healthy.mkdir()
    (healthy / "t.csv").write_bytes(ROWS)
    other = {"engine": "files", "root": str(healthy), "settle_s": 0, "allow_writable": True}
    found = registry(base, {"other": other})
    spec = file_spec(csv_table("t.csv", ["id", "v", "s"]))
    spec["sides"]["a"]["target"] = "other"
    made = await tables.planning(found, compile_spec(spec, targets=found))
    assert made.plan is not None
    plan, engine = tables.pins(made.plan, made.pinned)["a"], duck.Files()
    hung = await engine.connect(found.get("exports"), timeout_s=0.5)
    read = "SELECT count(*) FROM read_csv($f, auto_detect=false, columns={'id': 'VARCHAR'})"
    try:
        async with engine.snapshot(found.get("other")) as session:
            batches = await engine.scan(session, plan, None)
            rows = len(await anext(batches))
            assert session.cursor is not None
            first = session.cursor._instance
            cursor = await hung._opened()
            dead.unlink()
            os.mkfifo(dead)
            with pytest.raises(duck.TargetUnreachable) as outage:
                await cursor.execute(read, {"f": [str(dead)]}, timeout_s=0.5)
            assert str(outage.value) == "Deadline" and first.dropped
            assert (manifest.stuck(str(dead.parent)), manifest.stuck(str(healthy))) == (1, 0)
            rows += sum([len(batch) async for batch in batches])
            assert (rows, await engine.null_keys(session, plan)) == (5, 0) and session.verified_at
            assert session.cursor._instance is first
            assert await duck.configured() is not first  # a new session opens on a new instance
            assert first.spill.exists() and first.cause is None and first.held == 1  # the scan's cursor
        first._ending.join(5)  # killed with its last cursor
        assert first.cause == duck.DEADLINE and not first.spill.exists() and manifest.stuck() == 0
    finally:
        await hung.close()


async def test_a_per_key_reader_leaves_a_discarded_instance(tmp_path: Path) -> None:
    """§10, D9: a per-key read needs nothing of an earlier statement, so a reader whose session holds a cursor
    of a discarded instance closes it and reads on the new one: the discarded instance's budget ends with the
    passes and plans in flight, not with the runs that read by key, and its spill directory is given up with
    its last cursor."""
    table, cfg = await _connector(tmp_path)
    assert (await table.fetch("1", cfg)).http_status == 200
    assert table._session is not None
    first = table._session.cursor._instance  # type: ignore[attr-defined]
    duck.discard(first)
    assert first.spill.exists() and first.held == 1  # the reader's cursor
    assert (await table.fetch("1", cfg)).http_status == 200
    assert table._session.cursor._instance is not first  # type: ignore[attr-defined]
    async with asyncio.timeout(5):
        while first.spill.exists():
            await asyncio.sleep(0.01)
    await table.aclose()


async def test_a_run_s_file_sides_share_one_plan_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§6: a run's creation has one files.plan_s for all its file sides (each had its own, so a file-to-file
    pair planned for twice files.plan_s, past the proxy's read timeout DEPLOY prescribes): the side that is
    planned once it has passed is P30."""
    for name in ("a", "b"):
        _root(tmp_path, f"{name}/t.csv", ROWS)
    root = (tmp_path / "files" / "root").resolve()
    other = {"engine": "files", "root": str(root / "b"), "settle_s": 0, "allow_writable": True}
    found = registry(tmp_path, {"other": other}, root=str(root / "a"))
    table = csv_table("t.csv", ["id", "v", "s"])
    spec = compile_spec(file_spec(table, {"role": "new", "target": "other", "table": table}), targets=found)
    assert (await tables.planning(found, spec)).plan is not None
    short = reader_settings(tmp_path)
    object.__setattr__(short.files, "plan_s", 1)  # below the settings' 10 s, to keep the test short
    duck.configure(short, found)
    digest = manifest._digest

    def slow(*args: Any) -> Any:
        time.sleep(0.7)  # each side's hash: inside one plan_s alone, past it together
        return digest(*args)

    monkeypatch.setattr(manifest, "_digest", slow)
    monkeypatch.setattr(manifest, "_HASHED", {})
    started = time.monotonic()
    with pytest.raises(duck.NotReady) as late:
        await tables.planning(found, spec)
    assert late.value.code == "planning" and str(late.value) == (
        "side b: planning its files passed the 1 s files.plan_s allows (0 of 1 files hashed): try again (a"
        " file renamed into place is not hashed twice), or raise files.plan_s and the proxy's read timeout"
    )
    assert time.monotonic() - started < 1.3


async def test_few_runs_plan_their_files_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§10: at most PLANS creations plan file sides at once in a process, so that plans alone never fill the
    pool's threads; one that waited its whole files.plan_s for its turn is not ready (P34)."""
    _root(tmp_path, "t.csv", ROWS)
    found = registry(tmp_path)
    spec = compile_spec(file_spec(csv_table("t.csv", ["id", "v", "s"])), targets=found)
    monkeypatch.setattr(duck, "PLANS", 2)
    planning, most, pin = 0, 0, manifest.pin

    async def pinning(*args: Any, **kwargs: Any) -> Any:
        nonlocal planning, most
        planning += 1
        most = max(most, planning)
        await asyncio.sleep(0.05)
        try:
            return await pin(*args, **kwargs)
        finally:
            planning -= 1

    monkeypatch.setattr(manifest, "pin", pinning)
    made = await asyncio.gather(*(tables.planning(found, spec) for _ in range(6)))
    assert most == 2 and all(found_.plan is not None for found_ in made)
    short = reader_settings(tmp_path)
    object.__setattr__(short.files, "plan_s", 0.2)
    duck.configure(short, found)
    monkeypatch.setattr(duck, "PLANS", 1)
    monkeypatch.setattr(duck, "_plans", type(duck._plans)())

    async def waiting() -> str:
        with pytest.raises(duck.NotReady) as refused:
            await tables.planning(found, spec)
        assert refused.value.code == "planning"
        return str(refused.value)

    async with duck.creating():  # another run's creation holds the one turn
        assert await waiting() == (
            "the file reader plans 1 runs at once, and this one waited the 0.2 s files.plan_s allows for its"
            " turn: try again"
        )


async def test_a_sort_is_refused_without_free_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§10: the spill's free space for the smaller of the side's sort_bytes and files.spill, before a byte is
    read (F3: TargetTempSpace: spill)."""
    _root(tmp_path, "t.csv", ROWS)
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v", "s"]))
    monkeypatch.setattr(duck, "_free", lambda path: 0)
    with pytest.raises(duck.TargetTempSpace) as refused:
        await scanned(found, plan)
    assert str(refused.value) == "spill"


async def test_an_error_of_a_changed_file_is_files_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10's table: an IOException from a statement has the files stat-checked at once: changed, FilesChanged;
    not listable, TargetUnreachable; unchanged, the statement's TargetRefused."""
    _root(tmp_path, "t.csv", ROWS)
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v", "s"]))
    real = duck.Cursor.fetch

    async def failing(self: duck.Cursor, *args: Any, **kwargs: Any) -> Any:
        raise TargetRefused("IOException")

    monkeypatch.setattr(duck.Cursor, "fetch", failing)
    with pytest.raises(TargetRefused):
        await read(found, plan, "1")
    time.sleep(0.01)
    _root(tmp_path, "t.csv", ROWS + b"4|d|x\n")
    with pytest.raises(FilesChanged) as changed:
        await read(found, plan, "1")
    assert str(changed.value) == "side a: file 1 of 1 is not the file the run was created on (size)"
    monkeypatch.setattr(duck.Cursor, "fetch", real)
    with pytest.raises(FilesChanged):  # the stat check before a group's statement sees it anyway
        await read(found, plan, "1")


async def test_a_file_side_scans_in_code_point_order(tmp_path: Path) -> None:
    """§9: a scan's order is each key text's UTF-8 bytes (U7), whatever a collation would say, a key several
    rows hold once a row; a row without a key is in no scan, and counted by null_keys."""
    keys = ["B", "Z", "a", "a ", "e", "é", "e\u0301", "😀", "\uff5e", "\U0010fffd", "10", "9", "a", "~"]
    _root(tmp_path, "t.csv", "".join(f"{key}|v|s\n" for key in [*keys, ""]).encode())
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v", "s"], header=False))
    assert [row[0] for row in await scanned(found, plan)] == sorted(keys, key=str.encode)
    session = await duck.Files().connect(found.get("exports"))
    assert await duck.Files().null_keys(session, plan) == 1 and plan["selected"] == len(keys) + 1
    await session.close()


async def test_a_file_changed_under_a_scan_fails_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§11: a scan's files are stat-checked after each fetched batch, and hashed again when its rows end."""
    monkeypatch.setattr(duck, "BATCH", 2)
    path = _root(tmp_path, "t.csv", ROWS)
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v", "s"]))
    engine = duck.Files()
    async with engine.snapshot(found.get("exports")) as session:
        batches = await engine.scan(session, plan, None)
        assert len(await anext(batches)) == 2 and session.verified_at is None
        assert [len(batch) async for batch in batches] == [2, 1] and session.verified_at is not None
    async with engine.snapshot(found.get("exports")) as session:
        batches = await engine.scan(session, plan, None)
        await anext(batches)
        time.sleep(0.01)
        path.write_bytes(ROWS.replace(b"keep", b"kept"))  # the same size, written again
        with pytest.raises(FilesChanged) as changed:
            await anext(batches)
    assert str(changed.value) == "side a: file 1 of 1 is not the file the run was created on (modified)"


async def test_a_fixed_side_is_described_from_its_plan(tmp_path: Path) -> None:
    """§12: after its run is created a file side's relation is its plan's, after a stat check (the schema
    guard's looks see one shape; a changed file is F9); a table pre-check, which names no side, is refused."""
    path = _root(tmp_path, "t.csv", ROWS)
    plan, found = await planned(
        tmp_path, csv_table("t.csv", ["id", "v", "s"], keys={"id": {"as": "integer"}})
    )
    engine = duck.Files()
    session = await engine.connect(found.get("exports"))
    relation = await engine.describe(session, None, "t", side=plan)
    assert relation is not None and (relation.schema, relation.name, relation.facts["kind"]) == (
        None, "t", "files",
    )  # fmt: skip
    assert [(c.name, c.declared, c.cls) for c in relation.columns] == [
        ("id", "a CSV field as integer", "integer"),
        ("v", "a CSV field", "text"),
        ("s", "a CSV field", "text"),
    ]
    with pytest.raises(Refusal) as refused:
        await engine.describe(session, None, "t")
    assert str(refused.value) == duck.FILE_SIDE
    time.sleep(0.01)
    path.write_bytes(ROWS + b"4|d|x\n")
    with pytest.raises(FilesChanged):
        await engine.describe(session, None, "t", side=plan)
    await session.close()


async def test_a_long_statement_is_watched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§10: a statement running past WATCH_S has the files stat-checked: a changed file interrupts it, and a
    check past its deadline abandons it with the instance, so a dead mount never keeps SORT."""
    monkeypatch.setattr(duck, "WATCH_S", 0.05)
    with ducked.started(tmp_path, memory_mib=duck.SELFTEST_MIB) as made:
        cursor = made.cursor()
        looked: list[int] = []

        async def changed() -> None:
            looked.append(1)
            raise FilesChanged("side a: file 1 of 1 is not the file the run was created on (content)")

        started = time.monotonic()
        with pytest.raises(FilesChanged):
            await cursor.execute(duck.SPILL_SORT.replace("200000", "20000000"), watch=changed)
        assert looked == [1] and time.monotonic() - started < 5
        await cursor.close()


async def test_a_cancelled_reader_interrupts_its_statement(tmp_path: Path) -> None:
    """§10: a caller that is cancelled (a stop, a closed request, a firing's window) interrupts its statement
    on its way out, so nothing goes on sorting for nobody (U10: an interrupt ends a sort): the reader is
    sent the interrupt, and its cursor takes the next statement at once."""
    with ducked.started(tmp_path, memory_mib=256) as made:
        cursor = made.cursor()
        interrupted, interrupt = threading.Event(), cursor.interrupt
        cursor.interrupt = lambda: (interrupted.set(), interrupt())[1]  # type: ignore[method-assign]
        task = asyncio.create_task(cursor.execute(duck.SPILL_SORT.replace("200000", "50000000")))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert interrupted.is_set()
        cancelled = time.monotonic()
        await cursor.execute("SELECT 1")
        assert await cursor.fetch(1) == [(1,)] and time.monotonic() - cancelled < 5


@pytest.mark.parametrize(
    ("raised", "said"),
    [
        (
            duck.TargetUnreachable("Deadline"),
            "side a: the directory of target exports did not answer within 30 s: is its mount alive?",
        ),
        (
            duck.manifest.Stuck("Stuck"),
            "side a: the file reader takes no work: 0 reads of mounts that do not answer are still waiting;"
            " it resumes when they return or DeltaSleuth restarts",
        ),
        (
            duck.manifest.Busy("Dropped"),
            "side a: the file reader did not take its files' work (Dropped): try again",
        ),
        (
            StatementTimeout("InterruptException"),
            "side a: planning its files passed the 300 s files.plan_s allows (12 of 12 files hashed): try"
            " again (a file renamed into place is not hashed twice), or raise files.plan_s and the proxy's"
            " read timeout",
        ),
        (
            TargetRefused("IOException"),
            "side a: its files could not be read while the run was planned (IOException)",
        ),
        (
            duck.TargetTempSpace("OutOfMemoryException"),
            "side a: its files could not be read while the run was planned (OutOfMemoryException)",
        ),
        (
            duck.Unstarted("guard"),
            "side a: the file reader did not start: its guard did not hold, or this build's DuckDB lacks the"
            " Parquet reader (see the server log)",
        ),
        (
            duck.Unstarted("space", 12 * 2**30),
            "side a: the file reader did not start: its spill volume has 12 GiB free, and files.spill (1 GiB)"
            " plus 1 GiB must fit: lower files.spill or grow the volume",
        ),
        (
            duck.Unstarted("process"),
            "side a: the file reader did not start: its process did not start or did not answer (see the"
            " server log)",
        ),
        (
            duck.manifest.Busy("Died"),
            "side a: the file reader did not take its files' work (Died): try again",
        ),
        (
            duck.manifest.Busy("Alone"),
            "side a: the file reader did not take its files' work (Alone): try again",
        ),
        (
            duck.manifest.Busy("Unstarted"),
            "side a: the file reader did not take its files' work (Unstarted): try again",
        ),
        (
            duck.ReaderDied(
                "side a: the file reader's process ended 3 times (SIGBUS) while only this side's files were"
                " being read: they are read no more until DeltaSleuth restarts"
            ),
            "side a: the file reader's process ended 3 times (SIGBUS) while only this side's files were being"
            " read: they are read no more until DeltaSleuth restarts",
        ),
    ],
    ids=[
        "P14",
        "P28 stuck",
        "P34",
        "P30",
        "P29",
        "P29 memory",
        "P27",
        "P28 space",
        "P27 process",
        "P34 died",
        "P34 alone",
        "P34 unstarted",
        "P35",
    ],
)
async def test_a_plan_s_file_work_is_worded_for_its_side(
    tmp_path: Path, raised: Exception, said: str
) -> None:
    """§15: what steps 5 to 9 of a side's plan meet is a refusal of the run's creation in the contract's
    words, holding the error's class and never its text; a plan past files.plan_s is not ready (planning)."""
    found = registry(tmp_path)
    with pytest.raises(ValueError) as refused:
        async with duck._planning("side a", found.get("exports"), 12):
            raise raised
    assert str(refused.value) == said
    assert isinstance(refused.value, duck.NotReady) == isinstance(raised, StatementTimeout | manifest.Busy)
