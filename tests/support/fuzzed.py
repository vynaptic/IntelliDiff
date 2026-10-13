"""A fuzz of Parquet files through the product's own path: one to five bytes changed in a footer or in the
pages before it, of files DuckDB's writer and ``parquet_tiny`` make here; each mutant the footer walk
accepts goes through ``ds target check`` with its count, a plan, a scan, a census with its rows without a
key and a per-key read, in the process that calls this. Its reader's process may end on one; this process
must not, and whatever a step meets is a closed code: a class's name, never DuckDB's text nor the file's
name (``UNSEEN``: what no outcome may hold).

Test-only.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import struct
from collections import Counter
from pathlib import Path
from typing import Any

from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import duck, manifest, reader
from tests.support import parquet_tiny as tiny
from tests.unit.connectors import ducked

# A seed by DuckDB's writer: its statement, its COPY's options, its key and the keys read from it.
ROWS = "FROM range(6) t(i)"
WRITTEN = {
    "flat": ("SELECT i AS id, 'v' || i AS v, i * 1.5 AS d, DATE '2020-01-01' + i::INT AS dt"
             " FROM range(5) t(i)", "", "id", "1 0 2"),
    "nested": ("SELECT i AS id, {'a': i, 'b': {'c': [i, i+1], 'd': MAP {'k': i}}} AS s, [1,2,3] AS l"
               " FROM range(3) t(i)", "", "id", "1 0 2"),
    "types": ("SELECT 1::TINYINT a, 2::SMALLINT b, 3::BIGINT c, 4::HUGEINT h, 1.5::DECIMAL(10,2) de,"
              " 1.25::DECIMAL(30,5) de2, TIMESTAMP '2020-01-01 00:00:00' ts,"
              " TIMESTAMPTZ '2020-01-01 00:00:00+00' tz, TIME '01:02:03' ti, 'x'::BLOB bl, true bo,"
              " '6ba7b810-9dad-11d1-80b4-00c04fd430c8'::UUID u, INTERVAL 1 DAY iv, 1.5::FLOAT f,"
              " TIMESTAMP_NS '2020-01-01 00:00:00' tn, 'a'::ENUM('a','b') AS e", "", "c", "3 0 2"),
    "groups": ("SELECT i AS id, i % 7 AS v FROM range(3000) t(i)", ", ROW_GROUP_SIZE 1024, COMPRESSION zstd",
               "id", "1 0 2"),
    "nulls": ("SELECT CASE WHEN i % 2 = 0 THEN i END AS id, NULL::VARCHAR AS v FROM range(6) t(i)",
              ", COMPRESSION gzip, KV_METADATA {a: 'b', c: 'd'}", "id", "1 0 2"),
    "v2": ("SELECT i id, 'x' v FROM range(4) t(i)", ", PARQUET_VERSION v2", "id", "1 0 2"),
    "fid": ("SELECT i id, 'x' v FROM range(4) t(i)", ", FIELD_IDS 'auto'", "id", "1 0 2"),
    "bloom": ("SELECT (i % 3)::VARCHAR id FROM range(2000) t(i)", ", DICTIONARY_SIZE_LIMIT 100", "id",
              "1 0 2"),
    "json": ("SELECT i id, '{\"a\":1}'::JSON j FROM range(2) t(i)", "", "id", "1 0 2"),
    "dates": (f"SELECT DATE '2020-01-01' + i::INT AS d, i AS v, TIME '01:02:03' AS ti,"
              f" TIMESTAMP '2020-01-01 00:00:00' + INTERVAL (i) DAY AS ts {ROWS}", "", "d",
              "2020-01-02 2020-01-09"),
    "decs": (f"SELECT (i * 1.5)::DECIMAL(12,2) AS de, i AS v, (i * 2.5)::DECIMAL(30,4) AS de2 {ROWS}", "",
             "de", "1.50 9.00"),
    "texts": (f"SELECT 'k' || i AS s, i AS v, md5(i::VARCHAR)::UUID AS u {ROWS}", "", "s", "k1 zz"),
    "blobs": (f"SELECT ('b' || i)::BLOB AS b, i AS v, true AS bo, i::FLOAT AS f {ROWS}", "", "b",
              "\\x6231 \\x00"),
}  # fmt: skip
COMPARED = {"nested": ["id"], "tiny": ["s", "d"]}  # a seed's compared columns, where not all
BYTES = [0x00, 0x01, 0x02, 0x05, 0x15, 0x16, 0x18, 0x19, 0x1C, 0x2C, 0x3C, 0x4C, 0x5C, 0x25, 0x35, 0x45, 0x55,
         0x7F, 0x80, 0xFF, 0x0C, 0x1B, 0x1A, 0x10, 0x20, 0x50]  # fmt: skip
ENDED = re.compile(r"the file reader's process ended \(([^)]*)\)")
MUTANT_S = 120  # a mutant's whole way: no statement of one took a second in 250,000


def seeds(folder: Path) -> dict[str, tuple[bytes, str, list[str]]]:
    """The files mutants are made of, by name: its bytes, its key and the keys read from it. Written under
    ``folder`` by a DuckDB that installs and loads nothing, and by ``parquet_tiny``."""
    import duckdb

    con = duckdb.connect(
        ":memory:", config={"autoinstall_known_extensions": False, "autoload_known_extensions": False}
    )
    folder.mkdir(parents=True, exist_ok=True)
    made = {}
    for name, (statement, options, key, keys) in WRITTEN.items():
        con.execute(f"COPY ({statement}) TO '{folder / name}.parquet' (FORMAT parquet{options})")
        made[name] = (folder / f"{name}.parquet").read_bytes(), key, keys.split()
    con.close()
    one = [
        tiny.Column("id", tiny.INT64, struct.pack("<q", 1)),
        tiny.byte_array("s", b"abc", logical={"utf8": True}),
        tiny.decimal("d", 12345, 9, 2),
        tiny.timestamp("t", 5, utc=True, unit="MICROS"),
        tiny.int96("o", 2459000, 5),
    ]
    made["tiny"] = tiny.write(one, created_by="x"), "id", ["1", "0", "2"]
    made["tiny_stats"] = tiny.write([tiny.byte_array("s", b"abc", statistics=(b"abc", b"abc"))]), "s", ["abc"]
    return made


def mutate(rng: random.Random, data: bytes, pages: bool) -> bytes:
    """``data`` with one to five changes to its footer's bytes, or with ``pages`` to the bytes before it: a
    bit flipped, a byte set (to one Thrift reads as a type, a count or a stop, or to any), added, taken
    away or moved by a little, a run of up to 64 copied elsewhere."""
    size = struct.unpack("<I", data[-8:-4])[0]
    body, foot = data[: len(data) - 8 - size], bytearray(data[len(data) - 8 - size : -8])
    if pages:
        tail, body, foot = bytes(foot), b"", bytearray(body[4:])
    for _ in range(rng.choice((1, 1, 1, 2, 2, 3, 5))):
        how, i = rng.randrange(7), rng.randrange(len(foot))
        if how == 0:
            foot[i] ^= 1 << rng.randrange(8)
        elif how == 1:
            foot[i] = rng.choice(BYTES)
        elif how == 2:
            foot[i] = rng.randrange(256)
        elif how == 3:
            foot[i:i] = bytes([rng.choice(BYTES)])
        elif how == 4 and len(foot) > 4:
            del foot[i]
        elif how == 5:
            j = rng.randrange(len(foot))
            a, b = min(i, j), min(max(i, j), min(i, j) + 64)
            k = rng.randrange(len(foot))
            foot[k:k] = foot[a:b]
        elif how == 6:
            foot[i] = (foot[i] + rng.choice((1, -1, 2, -2, 16, -16))) % 256
    if pages:
        return b"PAR1" + bytes(foot) + tail + struct.pack("<I", len(tail)) + b"PAR1"
    return body + bytes(foot) + struct.pack("<I", len(foot)) + b"PAR1"


CODES = ("Died", "Alone", "Unstarted", "Busy", "Dropped", "Deadline", "Stuck")  # an outage's
UNSEEN = ("NOT CLOSED", "LOST", "FEWER", "TIMED OUT")  # what no outcome may hold


def _met(step: str, exc: BaseException, hidden: tuple[str, ...]) -> str:
    """What a step met, as its closed code: a neutral error's class with the name it holds, a refusal with
    the class its text ends on, an end of the reader by what the text says of it. ``NOT CLOSED`` for an
    exception of another kind, and for a text that holds the file's name, its directory or DuckDB's own
    words."""
    text = str(exc)
    closed = isinstance(exc, tables.TableError | ValueError) and "Error: " not in text
    if not closed or any(part in text for part in hidden):
        return f"{step} NOT CLOSED {type(exc).__name__}"
    if isinstance(exc, tables.ReaderDied) or "the file reader's process ended" in text:
        return f"{step} ReaderDied" if "3 times" in text else f"{step} the reader ended"
    if isinstance(exc, tables.TableError):
        named = text if text in reader.NAMES or text in CODES else "a text of the product's"
        return f"{step} {type(exc).__name__} {named}"
    if (busy := re.search(r"did not take its files' work \(([A-Za-z]+)\)", text)) is not None:
        return f"{step} not ready ({busy[1]})"
    named = re.search(r"\(([A-Za-z]+)\)$", text)
    return f"{step} refused{f' ({named[1]})' if named and named[1] in reader.NAMES else ''}"


async def through(base: Path, name: str, key: str, columns: list[str] | None, keys: list[str]) -> str:
    """One file of the tests' root through the product's path: what each step met, joined. ``LOST`` for a
    key a scan gave that its own read lacks, ``FEWER`` for a census of fewer keys than the scan's."""
    hidden, out = (name, str(base)), []
    table: dict[str, Any] = {"name": "t", "keyColumns": [key], "file": {"path": name, "format": "parquet"}}
    if columns:
        table["columns"] = columns
    try:
        await duck.check(ducked.registry(base).get("exports"), name, "parquet", count=True)
        out.append("checked")
    except Exception as exc:
        out.append(_met("check", exc, hidden))
    try:
        plan, found = await ducked.planned(base, table)
    except Exception as exc:
        return "; ".join([*out, _met("plan", exc, hidden)])
    out.append("planned")
    rows: list[tuple[Any, ...]] = []
    try:
        rows = await ducked.scanned(found, plan)
        out.append("scanned")
    except Exception as exc:
        out.append(_met("scan", exc, hidden))
    engine = tables.ENGINES["files"]
    try:
        async with engine.snapshot(found.get("exports"), timeout_s=60) as session:
            counted = sum([len(batch) async for batch in engine.census(session, plan)])
            counted += await engine.null_keys(session, plan)
        out.append("census" if counted >= len(rows) else "census FEWER keys than the scan's")
    except Exception as exc:
        out.append(_met("census", exc, hidden))
    try:
        scanned = [row[0] for row in rows[:3] if isinstance(row[0], str)]
        read = await ducked.read(found, plan, *dict.fromkeys([*scanned, *keys]))
        out.append("read" if all(read[key] for key in scanned) else "read LOST a scanned key")
    except Exception as exc:
        out.append(_met("read", exc, hidden))
    return "; ".join(out)


class _Ends(logging.Handler):
    """The causes of the readers' ends the product logged."""

    def __init__(self) -> None:
        super().__init__()
        self.causes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if (found := ENDED.match(record.getMessage())) is not None:
            self.causes.append(found[1])


async def run(
    base: Path, seed: int, count: int, *, pages: bool, kept: bytes | None = None
) -> tuple[Counter[str], Counter[tuple[str, str]], int]:
    """``count`` mutants the footer walk accepts, made from ``seed``, each through the product's path under
    ``base``, then the file ``kept`` (keyed by ``id``): what they met and how often, the readers' ends
    meanwhile by the mutant's seed file and the end's cause, and how many mutants the walk refused."""
    rng, made = random.Random(seed), sorted(seeds(base / "seeds").items())  # noqa: S311 - a fuzz's
    root = base / "files" / "root"
    root.mkdir(parents=True, exist_ok=True)
    met: Counter[str] = Counter()
    ends: Counter[tuple[str, str]] = Counter()
    logged, refused, done, n = _Ends(), 0, 0, 0
    manifest.log.addHandler(logged)
    try:
        while done < count:
            stem, (original, key, keys) = rng.choice(made)
            data, n = mutate(rng, original, pages), n + 1
            if data == original:
                continue
            path = root / f"m{n:06d}.parquet"
            path.write_bytes(data)
            try:
                manifest.footed(str(path))
            except manifest.BadFooter:
                refused += 1
                path.unlink()
                continue
            try:
                async with asyncio.timeout(MUTANT_S):
                    met[await through(base, path.name, key, COMPARED.get(stem), keys)] += 1
            except TimeoutError:
                met["TIMED OUT"] += 1
            ends.update((stem, cause) for cause in logged.causes)
            logged.causes.clear()
            path.unlink()
            done += 1
        if kept is not None:
            (root / "kept.parquet").write_bytes(kept)
            met[await through(base, "kept.parquet", "id", None, ["1", "0", "2"])] += 1
            ends.update(("kept", cause) for cause in logged.causes)
    finally:
        manifest.log.removeHandler(logged)
    return met, ends, refused
