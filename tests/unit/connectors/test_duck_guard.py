"""The locked DuckDB instance (docs/mvp/FILES.md §3's U1 to U4, U12 and U17, §10, §14): the wheel, what is
compiled in, the guard, the spill and its limit, the self-test, and P27 and P28. Every behaviour is observed
on the locked version; no statement here may reach a network (an ``INSTALL`` names a closed local port).
What DuckDB itself does is observed on a connection in this process (``locked``); what the product does, on
a reader, the process it reads through (``started``, ``duck.instance``; docs/mvp/READER.md)."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from deltasleuth.connectors.tables import (
    StatementTimeout,
    TargetDenied,
    TargetRefused,
    TargetTempSpace,
    duck,
    manifest,
    reader,
)
from tests.support import faults
from tests.unit.connectors import ducked
from tests.unit.connectors.ducked import locked, settings, stand_in
from tools import release

ROOT = Path(__file__).resolve().parents[3]
VERSION = "1.5.5"


def test_duckdb_wheel() -> None:
    """U1, from uv.lock without a download: pinned exactly, no required dependency, a cp313 manylinux wheel
    for each of release.MACHINES. The installed macOS wheel vendors no ``*.libs`` file; the manylinux ones
    are not inspected (FILES.md §3 and RELEASE §8 record that check as open)."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert f"duckdb=={VERSION}" in pyproject["project"]["dependencies"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (locked_duckdb,) = [p for p in lock["package"] if p["name"] == "duckdb"]
    assert locked_duckdb["version"] == VERSION and "dependencies" not in locked_duckdb
    wheels = [Path(w["url"]).name for w in locked_duckdb["wheels"]]
    for machine in release.MACHINES.values():
        assert [w for w in wheels if "-cp313-cp313-manylinux" in w and w.endswith(f"_{machine}.whl")], machine
    import importlib.metadata as metadata

    installed = metadata.distribution("duckdb")
    assert installed.version == VERSION and not [
        f for f in installed.files or () if f.parts[0].endswith(".libs")
    ]
    assert [r for r in installed.requires or () if "extra ==" not in r] == []  # pyarrow, pandas: extras only


def test_needed_extensions_are_built_in(tmp_path: Path) -> None:
    """U2: parquet and core_functions are loaded and statically linked, read before the lock; after it the
    listing needs the extension directory, which lies outside every allowed one (observed)."""
    con = duckdb.connect(
        ":memory:", config={"autoload_known_extensions": False, "autoinstall_known_extensions": False}
    )
    found = {name: (loaded, mode) for name, loaded, mode in con.execute(
        "SELECT extension_name, loaded, install_mode FROM duckdb_extensions()").fetchall()}  # fmt: skip
    assert all(found[name] == (True, "STATICALLY_LINKED") for name in reader.NEEDED)
    assert not [
        name
        for name, (loaded, _) in found.items()
        if loaded and name not in {*reader.NEEDED, "icu", "json", "jemalloc"}
    ]
    with locked(tmp_path) as made, pytest.raises(duckdb.PermissionException):
        made.con.execute("SELECT * FROM duckdb_extensions()").fetchall()


GUARDED = {
    "outside": "SELECT * FROM read_csv('{base}/outside.csv')",
    "sibling sharing a prefix": "SELECT * FROM read_csv('{base}/files/rootx/a.csv')",
    "traversal": "SELECT * FROM read_csv('{base}/files/root/../../outside.csv')",
    "blob": "SELECT * FROM read_blob('{base}/outside.csv')",
    "glob outside": "SELECT * FROM glob('{base}/*')",
    "http": "SELECT * FROM read_csv('http://127.0.0.1:9/a.csv')",
    "s3": "SELECT * FROM read_csv('s3://bucket/a.csv')",
    "attach outside": "ATTACH '{base}/x.duckdb' AS x",
    "copy to outside": "COPY (SELECT 1) TO '{base}/out.csv'",
    "export outside": "EXPORT DATABASE '{base}/exported'",
    "install": "INSTALL httpfs FROM 'http://127.0.0.1:9'",
    "force install": "FORCE INSTALL httpfs FROM 'http://127.0.0.1:9'",
    "load": "LOAD httpfs",
    "load a path": "LOAD '{base}/files/root/x.duckdb_extension'",
    "set": "SET threads = 4",
    "set allowed": "SET allowed_directories = ['/']",
    "reset": "RESET enable_external_access",
    "pragma": "PRAGMA threads = 4",
}


@pytest.mark.parametrize("statement", GUARDED.values(), ids=GUARDED.keys())
def test_guard_refuses(tmp_path: Path, statement: str) -> None:
    """U3: after allowed_directories, enable_external_access=false and lock_configuration=true."""
    base = tmp_path.resolve()
    (base / "files" / "rootx").mkdir(parents=True)
    (base / "files" / "rootx" / "a.csv").write_text("id\n1\n")
    with locked(base) as made:
        with pytest.raises(duckdb.Error):
            made.con.execute(statement.format(base=base)).fetchall()
        assert not (base / "spill" / made.spill.name / "extensions").exists()  # nothing was installed


def test_guard_reads_inside_a_root(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    with locked(base) as made:
        (base / "files" / "root" / "a.csv").write_text("id,name\n1,x\n")
        assert made.con.execute(
            "SELECT * FROM read_csv($f)", {"f": [str(base / "files" / "root" / "a.csv")]}
        ).fetchall() == [(1, "x")]
        read = made.con.execute(
            "SELECT current_setting('enable_external_access'), current_setting('lock_configuration'),"
            " current_setting('python_enable_replacements'), current_setting('autoinstall_known_extensions'),"
            " current_setting('autoload_known_extensions'), current_setting('allow_community_extensions')"
        ).fetchone()
        assert read == (False, True, False, False, False, False)


def test_guard_does_not_refuse_writes_into_an_allowed_directory(tmp_path: Path) -> None:
    """U3 as observed: DuckDB itself runs ATTACH, COPY TO and EXPORT into an allowed directory (a root, or the
    spill it allows by itself), so the reader's cursor runs one SELECT a call and nothing else."""
    base = tmp_path.resolve()
    with locked(base) as made:
        temp = made.spill / "temp"
        temp.mkdir()
        made.con.execute(f"COPY (SELECT 1) TO '{temp}/out.csv'")
        assert (temp / "out.csv").exists()
    with ducked.started(base) as made:
        temp = made.spill / "temp"
        cursor = made.cursor()

        async def refused(statement: str) -> str:
            with pytest.raises(TargetRefused) as raised:
                await cursor.execute(statement)
            return str(raised.value)

        others = [
            f"COPY (SELECT 1) TO '{temp}/again.csv'",
            f"ATTACH '{temp}/x.duckdb' AS x",
            "SELECT 1; SELECT 2",
            "INSTALL httpfs FROM 'http://127.0.0.1:9'",
            "SET threads = 1",
        ]
        for statement in others:
            assert asyncio.run(refused(statement)) == "not one SELECT"
        assert not (temp / "again.csv").exists() and not (temp / "x.duckdb").exists()


def test_no_replacement_scan(tmp_path: Path) -> None:
    """U12: no SQL name resolves to a Python object."""
    customers = [("1", "x")]  # noqa: F841 - what a replacement scan would read
    with locked(tmp_path) as made, pytest.raises(duckdb.CatalogException):
        made.con.execute("SELECT * FROM customers").fetchall()


def _mib(text: str) -> int:
    number, unit = text.split()
    return round(float(number) * {"MiB": 1, "GiB": 1024}[unit]) * 2**20


def test_limits_are_binary(tmp_path: Path) -> None:
    """U17: memory_limit and max_temp_directory_size given as <n>MiB read back as n x 2^20 bytes."""
    with locked(tmp_path, memory_mib=2048, spill_mib=20 * 1024, threads=3) as made:
        memory, spill, threads = made.con.execute(
            "SELECT current_setting('memory_limit'), current_setting('max_temp_directory_size'),"
            " current_setting('threads')"
        ).fetchone()  # type: ignore[misc]
    assert (_mib(memory), _mib(spill), threads) == (2 * 2**30, 20 * 2**30, 3)
    assert manifest.size_bytes("2GB") // 2**20 == 2048  # files.memory's 2GB is handed over as 2048MiB


def _peak(con: Any, temp: Path, statement: str) -> tuple[int, int]:
    """The rows ``statement`` gives and the most bytes its spill held at once."""
    peak, stop = [0], threading.Event()

    def watch() -> None:
        while not stop.wait(0.001):
            try:
                peak[0] = max(peak[0], sum(e.stat().st_size for e in os.scandir(temp)))
            except OSError:
                pass

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        cursor = con.cursor().execute(statement)
        rows = sum(len(batch) for batch in iter(lambda: cursor.fetchmany(10_000), []))
    finally:
        stop.set()
        watcher.join()
    return rows, peak[0]


def test_spill_with_external_access_off(tmp_path: Path) -> None:
    """U4: the sort spills to temp_directory under the lock and stops at max_temp_directory_size. 200,000
    rows of 132 bytes spill from 72 MiB with 2 threads (recorded in RELEASE §5; 70 MiB fails, and from 90
    MiB they fit in memory); the self-test sorts them at 80 MiB."""
    with locked(tmp_path / "a", memory_mib=duck.SELFTEST_MIB) as made:
        assert _peak(made.con, made.spill / "temp", duck.SPILL_SORT)[0] == 200_000
        assert _peak(made.con, made.spill / "temp", duck.SPILL_SORT)[1] > 0
    with locked(tmp_path / "b", memory_mib=72) as made:
        rows, spilled = _peak(made.con, made.spill / "temp", duck.SPILL_SORT)
        assert rows == 200_000 and spilled > 0
    with locked(tmp_path / "c", memory_mib=48) as made, pytest.raises(duckdb.OutOfMemoryException):
        _peak(made.con, made.spill / "temp", duck.SPILL_SORT)
    with locked(tmp_path / "d", memory_mib=duck.SELFTEST_MIB, spill_mib=2) as made:
        with pytest.raises(duckdb.OutOfMemoryException, match="max_temp_directory_size"):
            _peak(made.con, made.spill / "temp", duck.SPILL_SORT)
    # Why the limit is set by a statement: given in connect's configuration it reads back, unenforced.
    temp = tmp_path / "e"
    given = {
        "threads": 2,
        "memory_limit": "80MiB",
        "temp_directory": str(temp),
        "max_temp_directory_size": "2MiB",
    }
    con = duckdb.connect(":memory:", config=given)
    assert con.execute("SELECT current_setting('max_temp_directory_size')").fetchone() == ("2.0 MiB",)
    assert _peak(con, temp, duck.SPILL_SORT)[1] > 2 * 2**20


def test_nothing_is_written_outside_the_spill(tmp_path: Path) -> None:
    """§10: DuckDB writes under its own spill directory alone: its temporary files, its home, extension and
    secret directories; and the spill directory of a live instance is kept by a sweep."""
    base = tmp_path.resolve()
    before = {p for p in base.rglob("*")}
    with locked(base, memory_mib=duck.SELFTEST_MIB) as made:
        _peak(made.con, made.spill / "temp", duck.SPILL_SORT)
        assert manifest.sweep(base / "spill") == 0 and made.spill.exists()
        written = {p for p in base.rglob("*")} - before
    outside = {p for p in written if not p.is_relative_to(base / "spill")}
    assert outside == {base / "files", base / "files" / "root", base / "outside.csv"}  # the fixture's own


def test_no_socket_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§14 point 1: building, proving and using the instance opens no Python socket (U3 covers DuckDB's)."""
    opened: list[Any] = []
    monkeypatch.setattr(socket.socket, "connect", lambda *a: opened.append(a))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: opened.append(a) or [])
    with locked(tmp_path, memory_mib=duck.SELFTEST_MIB) as made:
        _peak(made.con, made.spill / "temp", duck.SPILL_SORT)
    assert opened == []


@pytest.mark.parametrize("limits", [True, False])
def test_the_self_test_and_its_imports(tmp_path: Path, limits: bool) -> None:
    """§3's smoke step in a process of its own, as the release runs it (READER.md §11): the line it prints,
    with its limits (each as this platform has it) and without them; no pyarrow, pandas or numpy, and no
    DuckDB but in its readers, whose spill directories went with them. The reader it sent SIGSEGV is in its
    error output, by the warning of that end. On macOS without ``DS_FAULTS=1`` the self-test is handed the
    signal of ``faults`` that is no crash there."""
    config = tmp_path / "ds.yaml"
    config.write_text(
        "database_dsn_ref: file:dsn\nevidence_dir: ev\nworkspace_dir: ws\ntargets_file: t.yaml\n"
    )
    asked = ([] if limits else ["limits=False"]) + ([] if faults.REAL else [f"fault={faults.FAULT:d}"])
    call = release.DUCK.replace("()", f"({', '.join(asked)})")
    ran = subprocess.run(
        [sys.executable, "-c", f"{call}\nimport sys\nprint(sorted(m for m in sys.modules"
         " if m.split('.')[0] in ('duckdb', 'numpy', 'pandas', 'pyarrow', 'pytz')))"],
        env=os.environ | {"DS_CONFIG": str(config)}, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    none = "not on this platform"
    filtered = reader.LINUX and os.uname().machine in reader.FILTERED
    held = (
        "limits not tried" if not limits
        else f"{'data limit held' if reader.LINUX else f'no data limit ({none})'},"
        f" {'sockets denied' if filtered else f'sockets not denied ({none})'}"
    )  # fmt: skip
    said = f"file reader: duckdb {VERSION} in a process of its own, replaced when it ended; {held}"
    assert (ran.returncode, ran.stdout) == (0, f"{said}\n[]\n")
    ended = f"the file reader's process ended ({faults.FAULT.name}) with no statement in flight\n"
    assert ran.stderr.startswith(ended) and (ran.stderr == ended or reader.LINUX)
    assert list((tmp_path / "ev" / "spill").iterdir()) == []  # it spilled in the evidence volume


HELD = "lambda limit, filtered: (limit, filtered)"  # a reader whose start saw all it was asked for hold
REFUSED = "SELECT error('refused')"


@pytest.mark.parametrize(
    ("restrict", "holds", "failure"),
    [
        (
            "lambda limit, filtered: (None, filtered)",
            REFUSED,
            "the reader's limits did not hold: no data limit (not held on this host), sockets denied",
        ),
        (
            "lambda limit, filtered: (limit, False)",
            REFUSED,
            "the reader's limits did not hold: data limit 3,328 MiB, sockets not denied (not held on this"
            " host)",
        ),
        (HELD, "SELECT 42", "the data limit did not hold"),
        (HELD, REFUSED, None),
    ],
    ids=["no data limit", "sockets not denied", "the statement completes", "held"],
)
def test_the_self_test_judges_its_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], restrict: str,
    holds: str, failure: str | None,
) -> None:  # fmt: skip
    """READER.md §11, its step 3, by readers that take themselves for Linux ones on a machine of
    ``FILTERED`` and report what the case says: the self-test fails unless its first reader's start saw
    the data limit hold and sockets denied, naming which did not and why, and unless the statement sent
    under the small limit does not complete; else it prints that both held."""
    base = tmp_path.resolve()
    script = f"reader.restrict = {restrict}\nreader.main()"
    stand_in(base, monkeypatch, script, LINUX=True, FILTERED=(os.uname().machine,))
    monkeypatch.setattr(duck, "HOLDS", holds)
    if failure is None:
        duck.selftest(settings(base), fault=faults.FAULT)
        assert capsys.readouterr().out == (
            f"file reader: duckdb {VERSION} in a process of its own, replaced when it ended; data limit held,"
            " sockets denied\n"
        )
    else:
        with pytest.raises(RuntimeError) as failed:
            duck.selftest(settings(base), fault=faults.FAULT)
        assert str(failed.value) == failure
    assert ducked.strays() == [] and list(settings(base).spill_dir.iterdir()) == []


def test_an_unproved_guard_is_p27(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """P27 sends its reader to the server log: each check that does not hold is logged there by its name
    (no text of DuckDB's) before the instance is refused: the reader's process answers which check and its
    names, and this one words them. A build that lacks an extension does not know its settings either, and
    parquet_metadata_cache is the Parquet reader's own: the extensions are judged before a cache is set, so
    such a build is P27 and not DuckDB's CatalogException out of a plan. Simulated, since the locked wheel
    links the reader: by httpfs, which it lacks, and a setting of that extension's, in a process whose
    constants hold them."""
    base = tmp_path.resolve()
    found = settings(base)
    (base / "files" / "root").mkdir(parents=True)
    p27 = (
        "side a: the file reader did not start: its guard did not hold, or this build's DuckDB lacks the"
        " Parquet reader (see the server log)"
    )
    nothing = {"autoinstall_known_extensions": False, "autoload_known_extensions": False}
    with pytest.raises(duckdb.CatalogException, match="s3_use_ssl"):  # as observed: the setting is not known
        duckdb.connect(":memory:", config=nothing).execute("SET s3_use_ssl = false")
    # A build without one: the reader's process, and this one, know httpfs as needed.
    stand_in(base, monkeypatch, NEEDED=(*reader.NEEDED, "httpfs"), CACHES=(*reader.CACHES, "s3_use_ssl"))
    with pytest.raises(duck.Unstarted) as unstarted:
        asyncio.run(duck.instance(found, [str(base / "files" / "root")]))
    assert duck.unstarted(unstarted.value, "side a", found.files) == p27
    monkeypatch.undo()
    # A probe the guard lets through: the root holds the targets file.
    inside = settings(base / "x")
    (base / "x" / "files").mkdir(parents=True)
    with pytest.raises(duck.Unstarted) as unstarted:
        duck._started(inside, [str(base / "x" / "etc")])
    assert unstarted.value.reason == "guard"
    # A setting that does not read back as the guard set it: the process proves other roots than it set.
    proved = "lambda con, roots, probe: prove(con, ['/elsewhere'], probe)"
    stand_in(base, monkeypatch, f"prove = reader.prove\nreader.prove = {proved}\nreader.main()")
    with pytest.raises(duck.Unstarted) as unstarted:
        duck._started(settings(base / "y"), [str(base / "files" / "root")])
    assert unstarted.value.reason == "guard"
    monkeypatch.undo()
    monkeypatch.setattr(reader, "CACHES", ())  # an instance whose caches stand as DuckDB starts them
    with locked(base / "z") as made:
        monkeypatch.undo()
        with pytest.raises(reader.Unguarded) as unguarded:
            reader.prove(made.con, [str(base / "z" / "files" / "root")], str(base / "z" / "outside.csv"))
    assert duck._unguarded(unguarded.value.code, unguarded.value.names).reason == "guard"
    logged = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "deltasleuth.files"]
    assert logged == [
        ("ERROR", "the file reader did not start: this build's DuckDB lacks httpfs, statically linked"),
        ("ERROR", "the file reader did not start: a read outside the declared roots was not refused"),
        ("ERROR", "the file reader did not start: allowed_directories did not read back as set"),
        ("ERROR", "the file reader did not start: enable_external_file_cache did not read back as set"),
    ]
    assert str(base) not in caplog.text


def test_an_unwritable_or_small_spill_is_p28(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path.resolve()
    found = settings(base / "a")
    locked_out = base / "ro"
    locked_out.mkdir(mode=0o500)
    try:
        unwritable = settings(base / "b", spill_dir=locked_out / "spill")
        with pytest.raises(duck.Unstarted) as unstarted:
            duck._started(unwritable, [str(base / "b" / "files" / "root")])
        assert duck.unstarted(unstarted.value, "side a", unwritable.files) == (
            f"side a: the file reader did not start: files.spill_dir is not a directory DeltaSleuth (uid"
            f" {os.getuid()}) may write"
        )
    finally:
        locked_out.chmod(0o700)
    statvfs = os.statvfs

    def free(size: int) -> None:
        def sized(path: Any) -> Any:
            real = statvfs(path)
            return SimpleNamespace(f_flag=real.f_flag, f_bavail=size // real.f_frsize, f_frsize=real.f_frsize)

        monkeypatch.setattr(os, "statvfs", sized)

    free(12 * 2**30)
    big = settings(base / "c", spill="20GB")
    with pytest.raises(duck.Unstarted) as unstarted:
        duck._started(big, [str(base / "c" / "files" / "root")])
    assert duck.unstarted(unstarted.value, "side a", big.files) == (
        "side a: the file reader did not start: its spill volume has 12 GiB free, and files.spill (20 GiB)"
        " plus 1 GiB must fit: lower files.spill or grow the volume"
    )
    assert found.spill_dir == base / "a" / "evidence" / "spill"
    # What this installation's live sorts spill there (another ds process's, here 1 MiB of it) counts as free:
    # they spill within the same budget, and a volume of files.spill plus 1 GiB starts a second process.
    live = manifest.held(big.spill_dir)
    (live / "temp").mkdir()
    (live / "temp" / "duckdb_temp_storage-0.tmp").write_bytes(b"x" * 2**20)
    free(21 * 2**30 - 2**19)
    assert manifest.spilled(big.spill_dir) >= 2**20
    duck._stopped(duck._started(big, [str(base / "c" / "files" / "root")]))
    manifest.release(live)
    with pytest.raises(duck.Unstarted) as unstarted:
        duck._started(big, [str(base / "c" / "files" / "root")])
    assert unstarted.value.reason == "space" and not live.exists()


async def test_one_instance_a_process_built_anew_when_dropped(tmp_path: Path) -> None:
    """§10: the reader is started at its first use and kept; a fatal error or an abandoned call drops it,
    and the next use starts another, in a spill directory of its own. The dropped one makes no cursor more;
    a session that holds one of its cursors goes on with it (it was Busy('Dropped') at its next statement,
    which cost every file side's pass in flight). A statement that meets the database an earlier fatal
    error invalidated is Busy('Dropped'), and a result that was open is still fetched. Its process is
    killed, and its spill directory given up, once its last cursor is closed. The fatal error is DuckDB's
    own: U16's aggregate over a line of invalid UTF-8."""
    base = tmp_path.resolve()
    found, roots = settings(base), [str(base / "files" / "root")]
    (base / "files" / "root").mkdir(parents=True)
    (base / "files" / "root" / "enc.csv").write_bytes(b"id|v\n1|a\n2|b\xff\n3|c\n")
    aggregated = (
        "SELECT count(v) FROM read_csv($f, auto_detect=false, columns={'id': 'VARCHAR', 'v': 'VARCHAR'},"
        " header=true, delim='|', ignore_errors=true, store_rejects=true)"
    )
    first = await duck.instance(found, roots)
    assert await duck.instance(found, roots) is first and first.version == VERSION
    cursor, other, third = first.cursor(), first.cursor(), first.cursor()
    await other.execute("SELECT 42")  # a result open before the error
    duck.discard(first)  # what an abandoned call does
    assert first.dropped and first.spill.exists() and first.spill in manifest._HELD
    with pytest.raises(manifest.Busy) as refused:  # no cursor more: a new session opens on the new reader
        first.cursor()
    assert str(refused.value) == "Dropped"
    second = await duck.instance(found, roots)
    assert second is not first and second.spill != first.spill and second.spill.exists()
    await third.execute("SELECT 43")  # a session that holds a cursor goes on with it
    assert await third.fetch(1) == [(43,)]
    with pytest.raises(manifest.Busy) as refused:  # the reader was dropped before DuckDB's error
        await cursor.execute(aggregated, {"f": [str(base / "files" / "root" / "enc.csv")]})
        await cursor.fetch(1)
    assert str(refused.value) == "Dropped" and await other.fetch(1) == [(42,)]
    with pytest.raises(manifest.Busy) as refused:  # and meets what invalidated the database as an outage
        await third.execute("SELECT 1")
    assert str(refused.value) == "Dropped"
    kept = second.cursor()
    with pytest.raises(TargetRefused) as raised:  # the error drops the reader that met it
        await kept.execute(aggregated, {"f": [str(base / "files" / "root" / "enc.csv")]})
        await kept.fetch(1)
    assert str(raised.value) == "InternalException" and second.dropped
    await kept.close()
    await cursor.close()
    await cursor.close()  # once
    await third.close()
    assert first.held == 1 and first.spill.exists() and first.cause is None  # the other session's cursor
    await other.close()
    async with asyncio.timeout(5):  # killed with its last cursor, and its directory given up
        while first.spill.exists() or first.spill in manifest._HELD or second.spill.exists():
            await asyncio.sleep(0.01)
    assert (first.cause, second.cause) == ("SIGKILL", "SIGKILL")


async def test_concurrent_first_uses_build_one_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9: one instance a process, one budget. Tasks that reach the reader before the first instance is
    stored (two schedules due together, a lane beside a resumed scan after a restart) wait for one build."""
    base = tmp_path.resolve()
    found, roots = settings(base), [str(base / "files" / "root")]
    (base / "files" / "root").mkdir(parents=True)
    built, started = [], duck._started

    def slow(*args: Any) -> duck.Instance:
        time.sleep(0.2)  # the build's length: every caller arrives within it
        built.append(started(*args))
        return built[-1]

    monkeypatch.setattr(duck, "_started", slow)
    held = set(manifest._HELD)
    made = await asyncio.gather(*(duck.instance(found, roots) for _ in range(3)))
    assert len(built) == 1 and all(instance is built[0] for instance in made)
    assert set(manifest._HELD) - held == {built[0].spill}  # one spill directory, one memory limit


def test_a_spill_directory_that_went_meanwhile_is_not_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§10: a spill directory that is gone between the sweep's listing and its look (its reader ended and
    gave it up on a thread of its own, or another process swept it) is no directory left unswept: nothing
    is logged of it, and the others are removed."""
    stale = [tmp_path / "spill" / (letter * 26) for letter in "ab"]
    for path in stale:
        (path / "temp").mkdir(parents=True)
    listed = manifest._spills(tmp_path / "spill")
    shutil.rmtree(stale[0])
    monkeypatch.setattr(manifest, "_spills", lambda spill_dir: listed)
    assert manifest.sweep(tmp_path / "spill") == 1 and not stale[1].exists() and caplog.records == []


def test_a_stale_spill_directory_that_cannot_be_swept_stops_no_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """§10: the sweep is best effort an entry. A leftover of another uid (one this process may not open, one
    whose entries it may not remove) is logged by its position and passed over; the others are removed, and
    the reader starts (it was P28's 'files.spill_dir is not a directory DeltaSleuth may write')."""
    base = tmp_path.resolve()
    found = settings(base)
    (base / "files" / "root").mkdir(parents=True)
    stale = [found.spill_dir / (letter * 26) for letter in "abc"]
    for path in stale:
        (path / "temp").mkdir(parents=True)
        (path / "temp" / "duckdb_temp_storage-0.tmp").write_bytes(b"raw values")
    stale[0].chmod(0)
    (stale[1] / "temp").chmod(0o500)
    try:
        made = duck._started(found, [str(base / "files" / "root")])
        lived = made.spill.exists()
        duck._stopped(made)
    finally:
        stale[0].chmod(0o700)
        (stale[1] / "temp").chmod(0o700)
    assert [path.exists() for path in stale] == [True, True, False] and lived
    held = ", ".join(duck.restricted(made))
    assert [r.getMessage() for r in caplog.records if r.name == "deltasleuth.files"] == [
        "spill directory 1 of 3 not swept: PermissionError",
        "spill directory 2 of 3 not swept: PermissionError",
        f"the file reader started: duckdb {VERSION} in process {made.proc.pid}, {held}",
    ]


async def test_a_cursor_maps_each_failure_to_its_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10's table, by the class DuckDB raises, never its text."""
    base = tmp_path.resolve()
    with ducked.started(base, memory_mib=48) as made:
        cursor = made.cursor()
        with pytest.raises(TargetTempSpace) as raised:
            await cursor.execute(duck.SPILL_SORT)
            await cursor.fetch(10)
        assert str(raised.value) == "OutOfMemoryException"
        with pytest.raises(TargetDenied) as raised:
            await cursor.execute("SELECT * FROM read_blob($f)", {"f": str(base / "outside.csv")})
        assert str(raised.value) == "PermissionException"
        with pytest.raises(TargetRefused) as raised:
            await cursor.execute(
                "SELECT * FROM read_csv($f)", {"f": [str(base / "files" / "root" / "none.csv")]}
            )
        assert str(raised.value) == "IOException" and str(base) not in str(raised.value)
        with pytest.raises(TargetRefused) as raised:
            await cursor.execute("SELECT CAST('x' AS INTEGER)")
        assert str(raised.value) == "ConversionException"
        statvfs = os.statvfs
        monkeypatch.setattr(
            os, "statvfs", lambda p: SimpleNamespace(f_bavail=1, f_frsize=statvfs(p).f_frsize)
        )
        with pytest.raises(TargetTempSpace) as raised:  # an IOException while the spill is nearly full
            await cursor.execute(
                "SELECT * FROM read_csv($f)", {"f": [str(base / "files" / "root" / "none.csv")]}
            )
        assert str(raised.value) == "IOException"
        monkeypatch.undo()
        started = time.monotonic()
        with pytest.raises(StatementTimeout) as raised:
            await cursor.execute(
                "SELECT count(*) FROM range(10000000000) a(i) WHERE md5(i::VARCHAR) = 'x'", timeout_s=0.5
            )
        assert str(raised.value) == "InterruptException" and time.monotonic() - started < 5
