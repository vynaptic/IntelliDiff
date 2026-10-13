"""The locked DuckDB instance the files tests read through (docs/mvp/FILES.md §3, §10, §18;
docs/mvp/READER.md §15): a root of their own under a temporary ``files.roots_under``, and a spill directory
beside it. ``started`` is a reader, the child process the product reads through; ``locked`` an in-process
connection locked as a reader's is, for the tests of what DuckDB itself does (U1 to U31)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import yaml

from deltasleuth.config import FilesSettings, Settings
from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import Plan, duck, manifest, reader
from deltasleuth.core.spec import TargetRegistry, compile_spec, load_targets

if TYPE_CHECKING:
    import pytest

READER = reader.__file__  # the reader's own path, whatever a test stands in for it


def settings(base: Path, **files: Any) -> Settings:
    """Settings whose spill lies under ``base`` and whose roots lie under ``base / 'files'``."""
    (base / "etc").mkdir(parents=True, exist_ok=True)
    (base / "etc" / "targets.yaml").write_text("targets: {}\n")
    budget = {"memory": "256MB", "spill": "1GB", "threads": 2} | files
    return Settings(
        database_dsn_ref=f"file:{base / 'etc' / 'db_dsn'}", evidence_dir=base / "evidence",
        workspace_dir=base / "workspace", targets_file=base / "etc" / "targets.yaml",
        files=FilesSettings(roots_under=base / "files", **budget),
    )  # fmt: skip


def _rooted(base: Path) -> Path:
    root = base / "files" / "root"
    root.mkdir(parents=True, exist_ok=True)
    (base / "outside.csv").write_text("id\n1\n")
    return root


@contextmanager
def locked(
    base: Path, memory_mib: int = 256, spill_mib: int = 1024, threads: int = 2
) -> Iterator[SimpleNamespace]:
    """A connection in this process (``con``), locked and proved as a reader's (``reader.connect``,
    ``reader.prove``), that reads ``base / 'files' / 'root'`` alone and spills in ``spill``, a directory
    held under ``base / 'spill'``."""
    root, spill = _rooted(base), manifest.held(base / "spill")
    con = reader.connect(str(spill), [str(root)], memory_mib, spill_mib, threads)
    try:
        reader.prove(con, [str(root)], str(base / "outside.csv"))
        yield SimpleNamespace(con=con, spill=spill)
    finally:
        con.close()


@contextmanager
def started(
    base: Path, memory_mib: int = 256, spill_mib: int = 1024, threads: int = 2, limit: int | None = None
) -> Iterator[duck.Instance]:
    """A reader that reads ``base / 'files' / 'root'`` alone and spills under ``base / 'spill'``, proved,
    with ``limit`` (MiB) under that data limit and with no socket, where its platform has them; killed,
    reaped and its spill directory given up when the block ends."""
    root, probe = _rooted(base), base / "outside.csv"
    made = duck._spawned(base / "spill", [str(root)], probe, memory_mib, spill_mib, threads, limit)
    try:
        yield made
    finally:
        duck._stopped(made)


def stand_in(
    base: Path, monkeypatch: pytest.MonkeyPatch, script: str = "reader.main()", **constants: Any
) -> None:
    """Start this process's readers from a script of the test's in place of ``reader.py``: ``script``, run
    with the module as ``reader`` (loaded by its path, as the child loads it: no ``deltasleuth`` package)
    and ``constants`` set in it, as they are set in this process's."""
    for name, value in constants.items():
        monkeypatch.setattr(reader, name, value)
    path = base / "stand_in.py"
    path.write_text(
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('reader', {READER!r})\n"
        "reader = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(reader)\n"
        + "".join(f"reader.{name} = {value!r}\n" for name, value in constants.items())
        + script
        + "\n"
    )
    monkeypatch.setattr(reader, "__file__", str(path))


def strays() -> list[int]:
    """This process's children that were started as a reader is, but the process's own reader: none, once
    a retired reader was killed and an ended one reaped."""
    listed = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    found = (line.split(None, 2) for line in listed.stdout.splitlines())
    kept = duck._instance.proc.pid if duck._instance is not None else None
    return [
        int(pid)
        for pid, parent, command in found
        if int(parent) == os.getpid() and " -I -X utf8 " in command and int(pid) != kept
    ]


def unrestricted() -> Any:
    """A DuckDB connection with nothing locked, which writes the tests' Parquet files (§18)."""
    import duckdb

    return duckdb.connect(":memory:")


SERVICE = {
    "role": "new", "target": "svc", "request": {"method": "GET", "path": "/x/{{ key }}"},
    "truncation": {"itemsPath": "$.t", "cap": 50},
}  # fmt: skip


def registry(base: Path, others: dict[str, Any] | None = None, **target: Any) -> TargetRegistry:
    """A targets file of one files target, ``exports``, on ``base / 'files' / 'root'`` (``settle_s`` 0, a
    writable mount allowed, and ``target``'s members), a service, ``svc``, and ``others``; the process's file
    reader configured with ``settings(base)``."""
    root = (base / "files" / "root").resolve()
    root.mkdir(parents=True, exist_ok=True)
    made = settings(base)
    exports = {"engine": "files", "root": str(root), "settle_s": 0, "allow_writable": True} | target
    service = {
        "base_url": "https://svc.example",
        "capabilities": {"authoritative_absence": {"status": [404]}},
    }
    listed = {"exports": exports, "svc": service} | (others or {})
    made.targets_file.write_text(yaml.safe_dump({"targets": listed}))
    found = load_targets(made.targets_file)
    duck.configure(made, found)
    return found


def file_spec(table: dict[str, Any], side_b: dict[str, Any] | None = None, **side: Any) -> dict[str, Any]:
    """A spec whose side a reads ``table`` on ``exports`` (with ``side``'s members: a typeMap) against side b,
    a service unless ``side_b`` is given."""
    return {
        "apiVersion": "deltasleuth/v1", "kind": "ComparisonSpec",
        "metadata": {"name": "files", "version": "1.0.0", "owner": "tests"}, "normalizerVersion": "1.1.0",
        "key": {"name": "id", "type": "string", "class": "business_code", "pattern": "^.+$",
                "source": {"kind": "list", "keys": ["1"]}},
        "sides": {
            "a": {"role": "legacy", "target": "exports", "table": table} | side, "b": side_b or SERVICE,
        },
        "compare": {}, "recheck": {"delay": "0s", "times": 1}, "security": {"unlisted": "clear"},
    }  # fmt: skip


async def planned(base: Path, table: dict[str, Any], **side: Any) -> tuple[Plan, TargetRegistry]:
    """Side a's plan as a run is created with it, its pins held as its readers take them."""
    found = registry(base)
    made = await tables.planning(found, compile_spec(file_spec(table, **side), targets=found))
    assert made.plan is not None
    return tables.pins(made.plan, made.pinned)["a"], found


def csv_table(path: str, columns: list[str], **csv: Any) -> dict[str, Any]:
    """A CSV side's table: keyed by its first column, comparing the others, ``|``-delimited with a header."""
    dialect = {"columns": columns, "delimiter": "|", "header": True} | csv
    return {"name": "t", "keyColumns": [columns[0]], "file": {"path": path, "format": "csv", "csv": dialect}}


async def read(found: TargetRegistry, plan: Plan, *keys: str) -> dict[str, Any]:
    """Each key's rows by one per-key statement: its documents, or why none."""
    engine = tables.ENGINES["files"]
    session = await engine.connect(found.get("exports"), timeout_s=30)
    try:
        rows = await engine.rows(session, plan, {key: [[key]] for key in keys})
    finally:
        await session.close()
    return {keys[i]: [(doc, selected) for j, doc, selected in rows if j == i] for i in range(len(keys))}


async def scanned(found: TargetRegistry, plan: Plan) -> list[tuple[Any, ...]]:
    """The side's scan rows, (key parts..., digest), in its order."""
    engine = tables.ENGINES["files"]
    async with engine.snapshot(found.get("exports")) as session:
        return [row async for batch in await engine.scan(session, plan, None) for row in batch]
