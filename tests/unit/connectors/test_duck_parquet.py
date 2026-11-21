"""DuckDB's Parquet reader on the locked instance, on files DuckDB
writes and on ``parquet_tiny``'s hand-built ones for the shapes its writer does not produce (observed, not
stubbed)."""

from __future__ import annotations

import dataclasses
import math
import os
import random
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest

from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import duck, manifest, reader, render
from deltasleuth.core import jsonio
from deltasleuth.core.spec import compile_spec
from tests.support import parquet_tiny
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

INSTANT_NS = 1_704_067_200_123_456_789  # 2024-01-01T00:00:00.123456789Z
ROOT = Path(__file__).resolve().parents[3]


def test_golden_renderings(tmp_path: Path) -> None:
    """CAST(DOUBLE AS VARCHAR) reads back exactly; epoch_us of an instant is its UTC microseconds
    whatever the session's zone (ICU's), so no ICU function is needed; a DECIMAL over 38 digits shows in
    parquet_schema, and DuckDB hands it over as a DOUBLE."""
    rng = random.Random(21)  # noqa: S311 (test data)
    doubles = [struct.unpack("<d", rng.randbytes(8))[0] for _ in range(20_000)]
    doubles += [
        0.1,
        1 / 3,
        5e-324,
        1.7976931348623157e308,
        -1.1102230246251565e-16,
        2.0**54 + 2,
        46056592.0,
        -0.0,
    ]
    finite = [d for d in doubles if d == d and abs(d) != float("inf")]
    con = unrestricted()
    read_back = con.execute(
        "SELECT CAST(v AS VARCHAR) FROM (SELECT unnest($v::DOUBLE[]) AS v)", {"v": finite}
    )
    assert [float(row[0]) for row in read_back.fetchall()] == finite
    zones = set()
    for zone in ("UTC", "Asia/Kolkata", "America/St_Johns"):
        con.execute(f"SET TimeZone = '{zone}'")
        zones.add(con.execute("SELECT epoch_us(TIMESTAMPTZ '2024-01-01 05:30:00.123456+05:30')").fetchone())
    assert zones == {(1_704_067_200_123_456,)}
    with locked(tmp_path) as made:
        wide = tmp_path / "files" / "root" / "wide.parquet"
        wide.write_bytes(parquet_tiny.write([parquet_tiny.decimal("amount", 10**70 + 1, 76, 10)]))
        schema = made.con.execute(
            "SELECT type, precision, scale, duckdb_type FROM parquet_schema($f) WHERE name = 'amount'",
            {"f": str(wide)},
        )
        assert schema.fetchall() == [("FIXED_LEN_BYTE_ARRAY", 76, 10, "DOUBLE")]
        read = made.con.execute(
            "SELECT typeof(amount), CAST(amount AS VARCHAR) FROM read_parquet($f)", {"f": [str(wide)]}
        )
        assert read.fetchall() == [("DOUBLE", "1.0000000000000001e+60")]  # 10^60 + 10^-10, not exact


def test_nanosecond_instants_are_found(tmp_path: Path) -> None:
    """parquet_schema shows a timestamp's unit and whether it is adjusted to UTC, for DuckDB's own files
    and hand-built ones; DuckDB reads a UTC nanosecond timestamp to the microsecond, a naive one whole.
    A zoned value is never fetched as such (DuckDB would need pytz, which is not installed): it is
    rendered."""
    with locked(tmp_path) as made:
        root = tmp_path / "files" / "root"
        tiny = root / "ns.parquet"
        tiny.write_bytes(parquet_tiny.write([
            parquet_tiny.timestamp("seen", INSTANT_NS, utc=True, unit="NANOS"),
            parquet_tiny.timestamp("naive", INSTANT_NS, utc=False, unit="NANOS"),
            parquet_tiny.timestamp("millis", INSTANT_NS // 10**6, utc=True, unit="MILLIS"),
            parquet_tiny.int96("legacy", 2460311, 123_456_789),
            parquet_tiny.byte_array("raw", "café".encode()),
        ]))  # fmt: skip
        own = root / "own.parquet"
        unrestricted().execute(
            f"COPY (SELECT TIMESTAMPTZ '2024-01-01 00:00:00.123456+00' AS tz, TIMESTAMP_NS '2024-01-01"
            f" 00:00:00.123456789' AS ns) TO '{own}' (FORMAT parquet)"
        )
        logical = {
            (name, kind)
            for path in (tiny, own)
            for name, kind in made.con.execute(
                "SELECT name, logical_type FROM parquet_schema($f)", {"f": str(path)}
            ).fetchall()
            if kind
        }
        units = {"MILLIS": "MILLIS=MilliSeconds(), MICROS=<null>, NANOS=<null>",
                 "MICROS": "MILLIS=<null>, MICROS=MicroSeconds(), NANOS=<null>",
                 "NANOS": "MILLIS=<null>, MICROS=<null>, NANOS=NanoSeconds()"}  # fmt: skip
        declared = {"seen": (1, "NANOS"), "naive": (0, "NANOS"), "millis": (1, "MILLIS"), "tz": (1, "MICROS"),
                    "ns": (0, "NANOS")}  # fmt: skip
        assert logical >= {
            (name, f"TimestampType(isAdjustedToUTC={utc}, unit=TimeUnit({units[unit]}))")
            for name, (utc, unit) in declared.items()
        }
        typed = made.con.execute(
            "SELECT typeof(seen), CAST(epoch_us(seen) AS VARCHAR), typeof(naive), CAST(naive AS VARCHAR),"
            " typeof(legacy), CAST(legacy AS VARCHAR), typeof(raw) FROM read_parquet($f)",
            {"f": [str(tiny)]},
        )
        assert typed.fetchall() == [(
            "TIMESTAMP WITH TIME ZONE", "1704067200123456", "TIMESTAMP_NS", "2024-01-01 00:00:00.123456789",
            "TIMESTAMP", "2024-01-01 00:00:00.123456", "BLOB",
        )]  # fmt: skip
        as_text = made.con.execute(
            "SELECT raw FROM read_parquet($f, binary_as_string=true)", {"f": [str(tiny)]}
        )
        assert as_text.fetchall() == [("café",)]
        with pytest.raises(duckdb.InvalidInputException, match="pytz"):
            made.con.execute("SELECT seen FROM read_parquet($f)", {"f": [str(tiny)]}).fetchall()


def test_a_time_is_written_from_its_microseconds() -> None:
    """A TIME's text is built from its microseconds, never by DuckDB's own conversion, which is an
    internal error, or ends the process, for a TIME outside a day (a crafted file's page may hold one; no
    such value is made here). Of every time of day it is DuckDB's text: no digit after a whole second,
    else up to six without trailing zeros, 24:00:00 the last."""
    rng = random.Random(8)  # noqa: S311 (test data)
    micros = [0, 1, 5, 100_000, 999_999, 1_000_000, 3_723_120_000, 86_399_999_999, 86_400_000_000]
    micros += [rng.randrange(86_400_000_000) for _ in range(50_000)]
    micros += [rng.randrange(86_400_000) * 1000 for _ in range(1000)] + [None]
    text = duck.PRIMITIVES.rendered("t", "time", "TIME")
    assert "VARCHAR" not in text
    found = (
        unrestricted()
        .execute(
            f"SELECT CAST(t AS VARCHAR), {text} FROM (SELECT CASE WHEN u = 86400000000 THEN TIME '24:00:00'"  # noqa: S608
            " ELSE TIME '00:00:00' + to_microseconds(u) END AS t FROM (SELECT unnest($u::BIGINT[]) AS u))",
            {"u": micros},
        )
        .fetchall()
    )
    assert len(found) == len(micros) and all(own == ours for own, ours in found)
    assert [found[n][1] for n in (1, 2, 5, 8, -1)] == [
        "00:00:00.000001", "00:00:00.000005", "00:00:01", "24:00:00", None,
    ]  # fmt: skip


# --- the Parquet reader of a file side -------------------------------------------------

GOLDEN = Path(__file__).parents[2] / "golden" / "engines"
NESTED = "nested columns are not read; list the columns to compare without it"


def _parquet(base: Path, name: str, select: str) -> Path:
    """``select``'s rows written by DuckDB's own writer under the side's root."""
    path = base / "files" / "root" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    unrestricted().execute(f"COPY ({select}) TO '{path}' (FORMAT parquet)")
    return path


def _table(path: str, **file: Any) -> dict[str, Any]:
    return {"name": "t", "keyColumns": ["k"], "file": {"path": path, "format": "parquet"} | file}


def _loaded(text: str) -> Any:
    return jsonio.parse_json(text)[0]


async def test_golden_renderings_of_files(tmp_path: Path) -> None:
    """The Parquet types against tests/golden/engines/files.json: each type DuckDB reads a Parquet column as,
    its class and its canonical value, edge values among them; and the values every engine renders alike
    (portable.json), a padded CSV field against PostgreSQL's char(5) among them, which digest as the other
    engines' do."""
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    con = unrestricted()
    con.execute((GOLDEN / "files.sql").read_text())
    for name in ("corpus", "portable"):
        con.execute(f"COPY {name} TO '{root / name}.parquet' (FORMAT parquet)")
    expected = _loaded((GOLDEN / "files.json").read_text())
    found = registry(tmp_path)
    made = await tables.planning(found, compile_spec(file_spec(_table("corpus.parquet")), targets=found))
    assert made.plan is not None
    classes = {c.name: [c.declared, c.cls, c.keyable] for c in made.relations["a"].columns}
    assert classes == expected["classes"]
    corpus = await read(found, tables.pins(made.plan, made.pinned)["a"], *expected["files"])
    assert {key: _loaded(rows[0][0]) for key, rows in corpus.items()} == expected["files"]
    portable = _loaded((GOLDEN / "portable.json").read_text())["portable"]
    plan, found = await planned(tmp_path, _table("portable.parquet"))
    read_ = await read(found, plan, *portable)
    assert {key: _loaded(rows[0][0]) for key, rows in read_.items()} == {
        key: {name: value for name, value in document.items() if name != "c_char"}
        for key, document in portable.items()
    }
    padded = [
        (key, f"{document['c_char']:<5}" if document["c_char"] is not None else None)
        for key, document in portable.items()
    ]
    (root / "chars.csv").write_text("".join(f"{key}|{value or ''}\n" for key, value in padded))
    chars, found = await planned(tmp_path, csv_table("chars.csv", ["k", "c_char"], header=False, padded=True))
    by_char = await read(found, chars, *portable)
    assert {key: _loaded(rows[0][0])["c_char"] for key, rows in by_char.items()} == {
        key: document["c_char"] for key, document in portable.items()
    }
    digested = plan | {"digest": sorted(plan["columns"])}  # across engines the worker hashes every column
    scanned_ = {row[0]: row[1] for row in await scanned(found, digested)}
    shared = {name: value for name, value in portable["typical"].items() if name != "c_char"}
    assert scanned_["typical"] == render.digest(digested, render.EMPTY, shared)


@pytest.mark.parametrize(
    ("select", "message"),
    [
        (
            "SELECT 1 AS k, [1, 2] AS tags",
            f"column 'tags' (LIST) cannot be compared: {NESTED}",
        ),
        (
            "SELECT 1 AS k, {'a': 1} AS tags",
            f"column 'tags' (STRUCT) cannot be compared: {NESTED}",
        ),
        (
            "SELECT 1 AS k, MAP {'a': 1} AS tags",
            f"column 'tags' (MAP) cannot be compared: {NESTED}",
        ),
        (
            'SELECT 1 AS k, struct_pack("SECRET\n2026-10-04 12:00:00 ERROR a forged line" := 1) AS tags',
            f"column 'tags' (STRUCT) cannot be compared: {NESTED}",
        ),
        (
            "SELECT 1 AS k, [struct_pack(\"a ]\n) b\" := 'x'::ENUM('x', 'SECRET'))] AS tags",
            f"column 'tags' (LIST) cannot be compared: {NESTED}",
        ),
    ],
    ids=["a list", "a struct", "a map", "a field's name of two lines", "a list of such structs"],
)
async def test_nested_columns_are_refused_where_the_plan_reads_them(
    tmp_path: Path, select: str, message: str
) -> None:
    """Discovered columns included; listing the columns to compare leaves one out. A nested type is
    named by its head alone: what follows holds a file's text (a field's name, which may be lines of its
    own), and a plan's refusal words none of it."""
    _parquet(tmp_path, "t.parquet", select)
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("t.parquet"))
    assert str(error.value) == f"side a: {message}"
    _parquet(tmp_path, "u.parquet", select.replace(" AS tags", " AS tags, 'x' AS v"))
    plan, _ = await planned(tmp_path, _table("u.parquet") | {"columns": ["v"]})
    assert plan["classes"] == {"k": "integer", "v": "text"}


async def test_a_wide_decimal_and_a_utc_nanosecond_instant_are_found_in_the_footer(tmp_path: Path) -> None:
    """The refusal of parquet_tiny's 76-digit DECIMAL (which DuckDB hands over as a DOUBLE) and a UTC
    nanosecond timestamp (read to the microsecond), read once the spec declares {as: timestamp}; observed, not
    stubbed."""
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    key = parquet_tiny.byte_array("k", b"1")
    (root / "wide.parquet").write_bytes(
        parquet_tiny.write([key, parquet_tiny.decimal("amount", 10**70 + 1, 76, 10)])
    )
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("wide.parquet", binaryAsString=True))
    assert str(error.value) == (
        "side a: column 'amount' (DECIMAL(76,10)) cannot be compared: a decimal over 38 digits is not read"
        " exactly"
    )
    seen = parquet_tiny.timestamp("seen", INSTANT_NS, utc=True, unit="NANOS")
    (root / "ns.parquet").write_bytes(parquet_tiny.write([key, seen]))
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("ns.parquet", binaryAsString=True))
    assert str(error.value) == (
        "side a: column 'seen' (a UTC nanosecond timestamp) is read to the microsecond: declare typeMap"
        " {seen: {as: timestamp}} to compare microseconds, or leave it out"
    )
    plan, found = await planned(
        tmp_path, _table("ns.parquet", binaryAsString=True), typeMap={"seen": {"as": "timestamp"}}
    )
    assert (await read(found, plan, "1"))["1"][0][0] == '{"seen": "2024-01-01T00:00:00.123456+00:00"}'
    # A column's name is a file's text: one that is no word is quoted in the typeMap the refusal words too.
    lines = parquet_tiny.timestamp("seen\n2026-10-04 ERROR a forged line", INSTANT_NS, utc=True, unit="NANOS")
    (root / "lines.parquet").write_bytes(parquet_tiny.write([key, lines]))
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("lines.parquet", binaryAsString=True))
    assert str(error.value) == (
        "side a: column 'seen\\n2026-10-04 ERROR a forged line' (a UTC nanosecond timestamp) is read to the"
        ' microsecond: declare typeMap {"seen\\n2026-10-04 ERROR a forged line": {as: timestamp}} to compare'
        " microseconds, or leave it out"
    )


MICROSECOND = (
    "side a: column {name!r} ({what}) is read to the microsecond: declare typeMap {{{name}: {{as: {as_}}}}}"
    " to compare {compared}, or leave it out"
)


async def test_legacy_shapes_are_read(tmp_path: Path) -> None:
    """An un-annotated BYTE_ARRAY is bytes, or text by binaryAsString. INT96 (a timestamp with the nanoseconds
    of its day) and a nanosecond time are read by DuckDB to the microsecond, as the UTC nanosecond timestamp
    is: two values a nanosecond apart rendered alike without a word, so each is refused until the
    approved spec declares that reading. From parquet_tiny's files, observed."""
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    columns = [
        parquet_tiny.byte_array("k", b"1"),
        parquet_tiny.int96("legacy", 2460311, 123_456_789),
        parquet_tiny.time("clock", 3_723_000_000_999, utc=False, unit="NANOS"),
        parquet_tiny.byte_array("raw", "café".encode()),
    ]
    (root / "old.parquet").write_bytes(parquet_tiny.write(columns))
    table = _table("old.parquet", binaryAsString=True)
    refused = {
        "legacy": ("an INT96 timestamp", "timestamp", "microseconds"),
        "clock": ("a nanosecond time", "string", "its text"),
    }
    for name, (what, as_, compared) in refused.items():
        with pytest.raises(ValueError) as error:
            await planned(tmp_path, table | {"columns": [name]})
        assert str(error.value) == MICROSECOND.format(name=name, what=what, as_=as_, compared=compared)
        with pytest.raises(ValueError):  # another declaration is not that reading
            await planned(tmp_path, table | {"columns": [name]}, typeMap={name: {"as": "date"}})
    declared = {"legacy": {"as": "timestamp"}, "clock": {"as": "string"}}
    plan, found = await planned(tmp_path, table, typeMap=declared)
    assert plan["classes"] == {"k": "text", "legacy": "timestamp", "clock": "time", "raw": "text"}
    assert (await read(found, plan, "1"))["1"][0][0] == (
        '{"legacy": "2024-01-01T00:00:00.123456", "clock": "01:02:03", "raw": "café"}'
    )
    plan, _ = await planned(tmp_path, table | {"columns": ["raw"]})  # left out, they refuse nothing
    assert plan["classes"] == {"k": "text", "raw": "text"}


async def test_every_footer_holds_file_one_s_columns(tmp_path: Path) -> None:
    """Another type, a column missing, a file that is no Parquet, encrypted Parquet."""
    _parquet(tmp_path, "d/1.parquet", "SELECT 1 AS k, 1.5::DECIMAL(12,2) AS amount")
    _parquet(tmp_path, "d/2.parquet", "SELECT 2 AS k, 1.5::DOUBLE AS amount")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("d/"))
    assert (
        str(error.value)
        == "side a: file 2 of 2 has another type for column 'amount' than file 1 (DOUBLE, not DECIMAL(12,2))"
    )
    _parquet(tmp_path, "e/1.parquet", "SELECT 1 AS k, 1 AS amount")
    _parquet(tmp_path, "e/2.parquet", "SELECT 2 AS k")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("e/"))
    assert str(error.value) == "side a: file 2 of 2 lacks column 'amount'"
    _parquet(tmp_path, "f/1.parquet", "SELECT 1 AS k")
    (tmp_path / "files" / "root" / "f" / "2.parquet").write_bytes(b"k\n1\n")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("f/"))
    assert re.fullmatch(
        r"side a: file 2 of 2 is not a Parquet file this build reads \([A-Za-z]+\)", str(error.value)
    )
    (tmp_path / "files" / "root" / "f" / "2.parquet").write_bytes(b"PARE" + b"\x00" * 64 + b"PARE")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("f/"))
    assert str(error.value) == "side a: file 2 of 2 is encrypted Parquet, which is not read"


async def test_every_footer_holds_file_one_s_unit(tmp_path: Path) -> None:
    """DuckDB reads a timestamp of any unit, and INT96, as one type, so a later file's nanoseconds
    were cut to file 1's microseconds without the declared typeMap. The comparison of each footer with file
    1's includes the unit and whether the column is INT96; file 1's own is what the plan judges."""
    root = tmp_path / "files" / "root"
    key = parquet_tiny.byte_array("k", b"1")
    shapes = {
        "micros": parquet_tiny.timestamp("c", 1_700_000_000_000_001, utc=True, unit="MICROS"),
        "nanos": parquet_tiny.timestamp("c", 1_700_000_000_000_000_999, utc=True, unit="NANOS"),
        "naive": parquet_tiny.timestamp("c", 1_700_000_000_000_001, utc=False, unit="MICROS"),
        "int96": parquet_tiny.int96("c", 2460311, 123_456_789),
    }
    zoned = "TIMESTAMP WITH TIME ZONE"
    cases = [
        ("micros", "nanos", f"{zoned} in nanoseconds, not {zoned} in microseconds"),
        ("naive", "int96", "TIMESTAMP as INT96, not TIMESTAMP in microseconds"),
        ("nanos", "nanos", ""),
    ]
    for n, (first, second, said) in enumerate(cases):
        for m, shape in enumerate((first, second), 1):
            (root / f"u{n}").mkdir(parents=True, exist_ok=True)
            (root / f"u{n}" / f"{m}.parquet").write_bytes(parquet_tiny.write([key, shapes[shape]]))
        if not said:
            break
        with pytest.raises(ValueError) as error:
            await planned(tmp_path, _table(f"u{n}/", binaryAsString=True))
        assert str(error.value) == (
            f"side a: file 2 of 2 has another type for column 'c' than file 1 ({said})"
        )
    table = _table("u2/", binaryAsString=True)  # both in nanoseconds: file 1's is what the plan judges
    with pytest.raises(ValueError, match="a UTC nanosecond timestamp"):
        await planned(tmp_path, table)
    plan, _ = await planned(tmp_path, table, typeMap={"c": {"as": "timestamp"}})
    assert plan["declared"]["c"] == zoned


async def test_a_key_a_scan_emits_is_found_by_its_own_read(tmp_path: Path) -> None:
    """A DECIMAL(p,p) key's text holds its integer digit, as every engine's does, where DuckDB writes
    .5: the key a scan emits is one the per-key read finds (it answered 404 for each). A DATE key outside
    the years 0001 to 9999 has a text of another form, which no key holds: the plan is refused, where
    its row was scanned, then answered 404."""
    _parquet(
        tmp_path,
        "w.parquet",
        "SELECT k::DECIMAL(9,9) AS k, n FROM (VALUES (0.5, 3), (-0.1, 1), (0.123456789, 2)) AS t(k, n)",
    )
    plan, found = await planned(tmp_path, _table("w.parquet"))
    keys = [row[0] for row in await scanned(found, plan)]
    assert keys == ["-0.100000000", "0.123456789", "0.500000000"]
    read_ = await read(found, plan, *keys, ".500000000")
    assert {key: [_loaded(doc)["n"] for doc, _ in rows] for key, rows in read_.items()} == {
        "-0.100000000": [1], "0.123456789": [2], "0.500000000": [3], ".500000000": [],
    }  # fmt: skip
    _parquet(
        tmp_path,
        "d.parquet",
        "SELECT * FROM (VALUES (DATE '2024-01-01', 1), (DATE '10000-01-01', 7), (DATE '0044-03-15 (BC)', 8),"
        " ('infinity', 9)) AS t(k, n)",
    )
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("d.parquet"))
    assert str(error.value) == (
        "side a: key column 'k' holds 3 date(s) outside the years 0001 to 9999, which have no key text:"
        " filter those rows out, or export the key as text"
    )
    plan, found = await planned(tmp_path, _table("d.parquet") | {"where": "n < 5"})
    assert [row[0] for row in await scanned(found, plan)] == ["2024-01-01"]


async def test_a_text_key_is_not_pruned_by_statistics(tmp_path: Path) -> None:
    """The per-key read holds no predicate on a key column that reaches the reader. DuckDB prunes
    a string column's row groups by the deprecated min and max, which writers before parquet-mr 1.10 computed
    in signed-byte order (for a group holding a, z and é: min é, max z): with an IN on it, the key a, which
    the scan read, was answered 404."""
    key = parquet_tiny.byte_array("k", b"a", logical={"utf8": True}, statistics=("é".encode(), b"z"))
    written = "parquet-mr version 1.8.1 (build 4aba4dae7bb0d4edbcf7923ae1339f28fd3f7fcf)"
    legacy = parquet_tiny.write([key, parquet_tiny.byte_array("v", b"x")], created_by=written)
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    (root / "old.parquet").write_bytes(legacy)
    for predicate in ("t WHERE k IN ('a')", "t JOIN (VALUES ('a')) AS a(k0) ON t.k = a.k0"):
        pruned = unrestricted().execute(
            f"SELECT k FROM read_parquet($f) AS {predicate}",  # noqa: S608 (constants)
            {"f": [str(root / "old.parquet")]},
        )
        assert pruned.fetchall() == []  # Observed on 1.5.5: the row group that holds a is skipped
    plan, found = await planned(tmp_path, _table("old.parquet", binaryAsString=True))
    assert plan["declared"]["k"] == "VARCHAR" and [row[0] for row in await scanned(found, plan)] == ["a"]
    assert (await read(found, plan, "a"))["a"][0][0] == '{"v": "x"}'
    assert " IN (" not in duck._keyed(plan, {"a": [["a"]]})[0]


def _q(number: int) -> bytes:
    return struct.pack("<q", number)


def _i(number: int) -> bytes:
    return struct.pack("<i", number)


@pytest.mark.parametrize(
    ("key", "text"),
    [
        (parquet_tiny.Column("k", parquet_tiny.INT64, _q(5), statistics=(_q(100), _q(200))), "5"),
        (
            parquet_tiny.Column(
                "k", parquet_tiny.INT32, _i(5), {"date": True}, statistics=(_i(100), _i(200))
            ),
            "1970-01-06",
        ),
        (dataclasses.replace(parquet_tiny.decimal("k", 150, 12, 2), statistics=(b"", b"")), "1.50"),
    ],
    ids=["integer", "date", "decimal"],
)
async def test_a_typed_key_is_not_pruned_by_statistics(
    tmp_path: Path, key: parquet_tiny.Column, text: str
) -> None:
    """DuckDB prunes an integer, a date and a decimal column's row groups by their statistics too,
    whatever they hold: a minimum and a maximum that do not hold the group's values (100 to 200 for a group
    holding 5), and a decimal stored as bytes whose statistics are empty. With the typed IN the per-key
    read bound for such a key, the key the scan had read was answered 404 by its own read. No statement
    holds a predicate on a key column that reaches the reader: the key is found."""
    root = tmp_path / "files" / "root"
    root.mkdir(parents=True)
    (root / "t.parquet").write_bytes(
        parquet_tiny.write([key, parquet_tiny.Column("v", parquet_tiny.INT64, _q(7))])
    )
    con = unrestricted()
    read_ = "SELECT k FROM read_parquet($f)"
    ((held,),) = con.execute(read_, {"f": [str(root / "t.parquet")]}).fetchall()
    for predicate in ("t WHERE k IN ($k)", "t JOIN (VALUES ($k)) AS a(k0) ON t.k = a.k0"):
        pruned = con.execute(f"{read_} AS {predicate}", {"f": [str(root / "t.parquet")], "k": held})
        assert pruned.fetchall() == []  # observed on 1.5.5: the row group that holds the key is skipped
    plan, found = await planned(tmp_path, _table("t.parquet"))
    assert [row[0] for row in await scanned(found, plan)] == [text]
    assert (await read(found, plan, text))[text][0][0] == '{"v": 7}'
    statement = duck._keyed(plan, {text: [[text]]})[0]
    assert " IN (" not in statement and " WHERE " not in statement


async def test_a_parquet_plan_s_figures_are_the_footer_walk_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Parquet side's rows and its worker columns' uncompressed bytes come from the
    plan's own walk of each footer, the figures DuckDB's parquet_file_metadata and parquet_metadata give of a
    file of its own writer (five row groups in two files here; against a CSV side, so that its compared
    column is the worker's). No statement of a plan, a scan, a per-key read or ds target check calls either:
    parquet_metadata reads a footer's statistics as text, and a crafted statistic ended the process there."""
    sent: list[str] = []
    select = duck.Cursor._select
    monkeypatch.setattr(
        duck.Cursor,
        "_select",
        lambda self, statement, values: (sent.append(statement), select(self, statement, values))[1],
    )
    paths = [
        _parquet(
            tmp_path,
            f"d/{n}.parquet",
            f"SELECT 'k' || i AS k, 'name ' || i AS name FROM range({n * 70_000}) t(i)",  # noqa: S608
        )
        for n in (1, 2)
    ]
    con, files = unrestricted(), {"f": [str(path) for path in paths]}
    ((rows, groups),) = con.execute(
        "SELECT sum(num_rows), sum(num_row_groups) FROM parquet_file_metadata($f)", files
    ).fetchall()
    sizes = dict(
        con.execute(
            "SELECT path_in_schema, sum(total_uncompressed_size) FROM parquet_metadata($f) GROUP BY ALL",
            files,
        ).fetchall()
    )
    assert (rows, groups) == (210_000, 3)
    assert manifest.summed(manifest.footed(str(path)) for path in paths) == (rows, sizes)
    (tmp_path / "files" / "root" / "b.csv").write_text("k1|name 1\n")
    side_b = {"role": "new", "target": "exports", "table": csv_table("b.csv", ["k", "name"], header=False)}
    found = registry(tmp_path)
    made = await tables.planning(found, compile_spec(file_spec(_table("d/"), side_b), targets=found))
    assert made.plan is not None and made.plan["a"]["digest"] == ["name"]
    plan = tables.pins(made.plan, made.pinned)["a"]
    keys = sum(len(f"k{i}") for n in (1, 2) for i in range(n * 70_000))
    row = duck.SORT_VALUE * (duck.SORT_KEYS + 1) + duck.SORT_ROW
    assert plan["rows"] == rows and plan["sort_bytes"] == math.ceil(
        duck.SORT_MARGIN * (duck.SORT_KEYS * keys + sizes["name"] + row * rows)
    )
    assert "footed" not in plan["pinned"]  # the walk's figures are the plan's, no part of the sealed pins
    assert len(await scanned(found, plan)) == rows and len((await read(found, plan, "k7"))["k7"]) == 2
    lines = await duck.check(found.get("exports"), "d/", "parquet", count=True)
    assert lines[-1].startswith("count: 210,000 rows by the footers, a pass over the first column ")
    assert sent and not [statement for statement in sent if "_metadata(" in statement]


FOOTER = (
    "has a Parquet footer that is not read: it is over 16 MiB, its schema holds over 10,000 elements or"
    " nests groups over 32 deep, it holds over 10,000 row groups or 200,000 column chunks, or it is not of"
    " the form this build reads"
)


async def test_a_footer_duckdb_is_not_handed_refuses_the_plan_and_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan, and ds target check, walk each Parquet footer before DuckDB reads the file:
    groups nested past the limit (DuckDB's own read of 2,000 ended the process; 200 here, which it survives),
    a negative count of children (an IndexError out of planning), the footers whose schema Thrift's reader
    takes deeper than a walk that stops at the first one (40 deep here), a row group that holds more column
    chunks than the schema has leaves, a negative type length (DuckDB's bind of it is an internal error) and
    an encrypted column are refused in fixed words, and no statement is sent."""
    sent: list[str] = []
    monkeypatch.setattr(duck.Cursor, "_select", lambda self, statement, values: sent.append(statement))
    one = parquet_tiny.Column("k", parquet_tiny.INT64, (1).to_bytes(8, "little"))
    negative = parquet_tiny.write(
        [one, parquet_tiny.Column("v", parquet_tiny.INT64, b"\x01" * 8, children=-3)]
    )
    encrypted = parquet_tiny.write(
        [one, parquet_tiny.Column("v", parquet_tiny.INT64, b"\x01" * 8, encrypted=True)]
    )
    hostile = {"n": parquet_tiny.write([one], groups=200), "c": negative, "e": encrypted}
    hostile |= dict(zip("xyz", parquet_tiny.crafted(40).values(), strict=True))
    hostile["s"] = parquet_tiny.swollen(40)
    fixed = parquet_tiny.Column("v", parquet_tiny.FIXED_LEN_BYTE_ARRAY, b"", type_length=-1)
    hostile["t"] = parquet_tiny.write([one, fixed])
    root = tmp_path / "files" / "root"
    for name, data in hostile.items():
        said = "is encrypted Parquet, which is not read" if name == "e" else FOOTER
        (root / name).mkdir(parents=True)
        (root / name / "1.parquet").write_bytes(parquet_tiny.write([one]))
        (root / name / "2.parquet").write_bytes(data)
        with pytest.raises(ValueError) as error:
            await planned(tmp_path, _table(f"{name}/"))
        assert str(error.value) == f"side a: file 2 of 2 {said}"
        with pytest.raises(ValueError) as error:
            await duck.check(registry(tmp_path).get("exports"), f"{name}/2.parquet", "parquet")
        assert str(error.value) == f"target exports: file 1 of 1 {said}"
    assert sent == []
    rows = [("f", "schema", 2), ("f", "k", None), ("f", "v", -3)]
    assert list(duck._tops(rows)) == ["k", "v"]  # and the columns' walk itself never leaves the rows


def _statistics() -> dict[str, bytes]:
    """Files of an INT64 key 1 and one more column whose chunk holds statistics DuckDB cannot convert: a TIME
    whose maximum is 2**60 microseconds (the verifier's time_big: parquet_metadata ended the process there,
    SIGSEGV), one whose maximum is negative and a NULL-typed column with a minimum and a maximum
    (InternalException from the same statement, which invalidated the database). And files whose page
    holds such a TIME under statistics that say it lies inside a day: DuckDB's own text of the value
    (CAST AS VARCHAR) ended the process, or was an InternalException, and with those statistics a guard
    before the cast was folded away."""
    key = parquet_tiny.Column("id", parquet_tiny.INT64, _q(1))
    clock = parquet_tiny.time("t", 5, utc=False, unit="MICROS")
    null = parquet_tiny.Column("t", parquet_tiny.INT32, _i(5), {"null": True}, statistics=(_i(1), _i(9)))
    day = (_q(0), _q(5))
    return {
        "a time's maximum past a day": parquet_tiny.write(
            [key, dataclasses.replace(clock, statistics=(_q(0), _q(2**60)))]
        ),
        "a time's maximum before midnight": parquet_tiny.write(
            [key, dataclasses.replace(clock, statistics=(_q(0), _q(-(2**60))))]
        ),
        "a null column's minimum and maximum": parquet_tiny.write([key, null]),
        "a time past a day": parquet_tiny.write(
            [key, dataclasses.replace(clock, value=_q(2**60), statistics=day)]
        ),
        "a time before midnight": parquet_tiny.write(
            [key, dataclasses.replace(clock, value=_q(-(2**60)), statistics=day)]
        ),
    }


async def _handed(base: str, depth: int, chunks: int) -> None:
    """In a process of its own: each crafted footer, ``depth`` deep, and one that holds a second row group
    of ``chunks`` column chunks, planned and checked, and one of them once more under last bytes that are not
    Parquet's, which the walk leaves to DuckDB; what refused each and how many statements DuckDB was sent
    meanwhile, printed. Then each file of ``_statistics``, which the walk accepts: planned, scanned, read
    by its key and checked with its count; what each gave, printed."""
    sent, select = [], duck.Cursor._select
    duck.Cursor._select = lambda self, *args: (sent.append(args[0]), select(self, *args))[1]  # type: ignore[method-assign]
    crafted = parquet_tiny.crafted(depth)
    crafted["a row group of many chunks"] = parquet_tiny.swollen(chunks)
    crafted["no Parquet by its last bytes"] = crafted["the schema twice"][:-4] + b"PARX"
    for n, (name, data) in enumerate(crafted.items()):
        path = Path(base) / "files" / "root" / str(n) / "1.parquet"
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        for check in (False, True):
            try:
                if check:
                    await duck.check(registry(Path(base)).get("exports"), f"{n}/1.parquet", "parquet")
                else:
                    await planned(Path(base), _table(f"{n}/1.parquet"))
            except ValueError as exc:
                print(f"{name}: {exc}; {len(sent)} statements")
    for n, (name, data) in enumerate(_statistics().items()):
        path = Path(base) / "files" / "root" / "s" / f"{n}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        plan, found = await planned(Path(base), _table(f"s/{n}.parquet") | {"keyColumns": ["id"]})
        rows, row = await scanned(found, plan), (await read(found, plan, "1"))["1"]
        counted = (await duck.check(found.get("exports"), f"s/{n}.parquet", "parquet", count=True))[-1]
        print(f"{name}: {plan['rows']} row planned, {len(rows)} scanned, read {row[0][0]}; {counted[:28]}")
    print(len([statement for statement in sent if "_metadata(" in statement]), "statements read statistics")


def test_a_crafted_footer_reaches_duckdb_in_no_process(tmp_path: Path) -> None:
    """The three footers the walk once took for flat while Thrift's reader took them
    9,000 groups deep ended the process that planned or checked them (SIGBUS in DuckDB's recursion), and a
    footer of a megabyte that held a second row group of a million column chunks took 672 MiB outside
    files.memory (100,000 here). In a process of its own, so that a walk that let one through would end that
    process and not this one: each is refused by the plan and by ds target check, and DuckDB is sent no
    statement. A file that does not end as Parquet does is handed to DuckDB, which refuses it by its last
    bytes and reads no footer of it: the same footer under such an end costs no process either. And the
    statistics that ended the process in parquet_metadata, or invalidated its database, in files the walk
    accepts: no statement calls that function, and each file is planned, scanned, read and checked. So is
    a TIME outside a day in a file's page: it is written from its microseconds, never by DuckDB's own
    conversion, and is a value that does not read as a time."""
    code = (
        "import asyncio, sys\nfrom tests.unit.connectors.test_duck_parquet import _handed\n"
        "asyncio.run(_handed(sys.argv[1], 9000, 100_000))"
    )
    ran = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path.resolve())],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert ran.returncode == 0, (ran.returncode, ran.stderr[-500:])  # a signal's is negative
    walked = [
        f"{name}: {who}: file 1 of 1 {FOOTER}; 0 statements"
        for name in (*parquet_tiny.crafted(1), "a row group of many chunks")
        for who in ("side a", "target exports")
    ]
    counted = "1 row planned, 1 scanned, read {}; count: 1 rows by the footers"
    assert ran.stdout.splitlines() == [
        *walked,
        "no Parquet by its last bytes: side a: file 1 of 1 is not a Parquet file this build reads"
        " (InvalidInputException); 2 statements",
        "no Parquet by its last bytes: target exports: its files could not be read while the run was planned"
        " (InvalidInputException); 3 statements",
        "a time's maximum past a day: " + counted.format('{"t": "00:00:00.000005"}'),
        "a time's maximum before midnight: " + counted.format('{"t": "00:00:00.000005"}'),
        "a null column's minimum and maximum: " + counted.format('{"t": null}'),
        "a time past a day: " + counted.format("column 't' does not read as time"),
        "a time before midnight: " + counted.format("column 't' does not read as time"),
        "0 statements read statistics",
    ]


LISTS = (
    "schema", "row groups", "chunks", "encodings", "path", "column key values", "encoding stats",
    "repetition histogram", "definition histogram", "geospatial types", "sorting columns", "key values",
    "column orders",
)  # fmt: skip


def _listing(doubles: str | None = None) -> bytes:
    """A file of one INT64 column, 7, whose footer holds every list parquet.thrift declares in FileMetaData
    (but an encrypted column's path), a field after each; the list ``doubles`` names doubles, eight bytes
    each, as its elements."""
    made, number, wide, text, struct_ = (
        getattr(parquet_tiny, name) for name in ("_Struct", "_I32", "_I64", "_BINARY", "_STRUCT")
    )
    body, schema, _ = parquet_tiny.laid([parquet_tiny.Column("id", parquet_tiny.INT64, struct.pack("<q", 7))])
    size = len(body) - 4

    def items(start: Any, field: int, name: str, kind: int, held: list[Any]) -> Any:
        return start.items(field, kind, held, 7 if name == doubles else None)

    column = items(made().i32(1, parquet_tiny.INT64), 2, "encodings", number, [0])
    column = items(column, 3, "path", text, [b"id"]).i32(4, 0).i64(5, 1).i64(6, size).i64(7, size)
    column = items(column, 8, "column key values", struct_, [made().binary(1, b"k").binary(2, b"v")])
    column = items(column.i64(9, 4), 13, "encoding stats", struct_, [made().i32(1, 0).i32(2, 0).i32(3, 1)])
    sizes = items(made().i64(1, 0), 2, "repetition histogram", wide, [1])
    column.struct(16, items(sizes, 3, "definition histogram", wide, [1]))
    column.struct(17, items(made(), 2, "geospatial types", number, [1]))
    chunk = made().i64(2, 4).struct(3, column).i64(4, 0)
    group = items(made(), 1, "chunks", struct_, [chunk]).i64(2, size).i64(3, 1)
    group = items(
        group, 4, "sorting columns", struct_, [made().i32(1, 0).boolean(2, False).boolean(3, False)]
    )
    footer = items(made().i32(1, 1), 2, "schema", struct_, schema).i64(3, 1)
    footer = items(footer, 4, "row groups", struct_, [group])
    footer = items(footer, 5, "key values", struct_, [made().binary(1, b"fk").binary(2, b"fv")])
    footer = items(footer.binary(6, b"made by hand"), 7, "column orders", struct_, [made().struct(1, made())])
    return parquet_tiny.footed(body, footer.end())


def test_the_reader_takes_a_list_s_elements_as_parquet_thrift_declares_them(tmp_path: Path) -> None:
    """What the footer walk rests on: Thrift's generated reader, which DuckDB reads a
    footer with, reads a list's elements as the type parquet.thrift declares for the list's field, whatever
    element type the list itself names; a list it does not know it passes over by the type named. A footer
    holding each list the walk knows, one of them in turn naming doubles, is read by DuckDB as the footer
    that names them rightly: each of the walk's lists is the reader's, of the walk's element type. The walk
    refuses each of those footers (there a reader that went by the name would pass over other bytes) and
    reads the right one. The reader takes the last of a schema written twice, also under an id it cuts to 16
    bits, and a count of children from an i32 alone: the footers a walk once took for flat are read 40
    groups deep. And the rows of a column whose chunk says it is encrypted are an InternalException, which
    invalidates the database: the walk refuses such a file as encrypted."""
    path = tmp_path / "lists.parquet"
    asked = (
        "SELECT * FROM read_parquet($f)",
        "SELECT created_by, num_rows FROM parquet_file_metadata($f)",
        "SELECT decode(key), decode(value) FROM parquet_kv_metadata($f)",
        "SELECT path_in_schema, encodings, key_value_metadata, data_page_offset FROM parquet_metadata($f)",
    )
    read = []
    for doubles in (None, *LISTS):
        path.write_bytes(_listing(doubles))
        con = unrestricted()  # one a file: DuckDB keeps a footer between statements
        read.append([con.execute(statement, {"f": str(path)}).fetchall() for statement in asked])
        if doubles is None:
            manifest.footed(str(path))
        else:
            with pytest.raises(manifest.BadFooter):
                manifest.footed(str(path))
    expected = [[(7,)], [("made by hand", 1)], [("fk", "fv")], [("id", "PLAIN", {b"k": b"v"}, 4)]]
    assert read == [expected] * (1 + len(LISTS))
    for name, data in parquet_tiny.crafted(40).items():
        path.write_bytes(data)
        schema = unrestricted().execute(
            "SELECT max(num_children), count(*) FROM parquet_schema($f)", {"f": str(path)}
        )
        assert schema.fetchall() == [(1, 42)], name  # the root, 40 groups of one child each, the column
    one = parquet_tiny.Column("k", parquet_tiny.INT64, (1).to_bytes(8, "little"), encrypted=True)
    path.write_bytes(parquet_tiny.write([one]))
    con = unrestricted()
    with pytest.raises(duckdb.InternalException):
        con.execute("SELECT * FROM read_parquet($f)", {"f": str(path)}).fetchall()
    with pytest.raises(duckdb.FatalException):
        con.execute("SELECT 1").fetchall()


def test_a_parquet_file_replaced_in_place_is_read_by_its_own_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DuckDB keeps a Parquet file's footer between statements and takes a file of the same size
    whose modification time is the same to the second for the same file: a file replaced so was read with
    the first one's footer and no error, its nanoseconds as microseconds, though a run created on it hashes
    the new bytes. The instance switches the external file cache off, and the Parquet metadata cache, which
    is off by default and does the same when on: the file is read by its own footer."""
    micros = parquet_tiny.write(
        [parquet_tiny.timestamp("c", 1_700_000_000_000_001, utc=False, unit="MICROS")]
    )
    nanos = parquet_tiny.write(
        [parquet_tiny.timestamp("c", 1_700_000_000_000_000_999, utc=False, unit="NANOS")]
    )
    assert len(micros) == len(nanos) and micros != nanos

    def replaced(con: Any, path: Path) -> list[Any]:
        """The file read, replaced by one of its size with its modification time put back, and read again."""
        path.write_bytes(micros)
        was, read = path.stat(), "SELECT typeof(c), CAST(c AS VARCHAR) FROM read_parquet($f)"
        assert con.execute(read, {"f": [str(path)]}).fetchall() == [
            ("TIMESTAMP", "2023-11-14 22:13:20.000001")
        ]
        path.write_bytes(nanos)
        os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
        return con.execute(read, {"f": [str(path)]}).fetchall()

    with locked(tmp_path / "off") as made:
        found = replaced(made.con, tmp_path / "off" / "files" / "root" / "c.parquet")
        kept = (
            "SELECT name, value FROM duckdb_settings() WHERE name LIKE '%cache%' AND input_type = 'BOOLEAN'"
        )
        assert dict(made.con.execute(kept).fetchall()) == {
            **dict.fromkeys(reader.CACHES, "false"),
            "enable_object_cache": "false",  # a placeholder of DuckDB's that does nothing, off as it starts
        }  # every cache this build's DuckDB can switch
    assert found == [("TIMESTAMP_NS", "2023-11-14 22:13:20.000000999")]
    stale = [("TIMESTAMP", "55840-11-08 22:13:20.000999")]  # the new bytes by the old footer (observed)
    monkeypatch.setattr(reader, "CACHES", ())
    with locked(tmp_path / "on") as made:  # DuckDB's defaults
        assert replaced(made.con, tmp_path / "on" / "files" / "root" / "c.parquet") == stale
    con = unrestricted()
    con.execute("SET enable_external_file_cache = false")
    con.execute("SET parquet_metadata_cache = true")
    assert replaced(con, tmp_path / "c.parquet") == stale


async def test_two_instants_a_nanosecond_apart_digest_alike_only_once_declared(tmp_path: Path) -> None:
    """An unzoned nanosecond timestamp keeps its nine digits; declared {as: timestamp} on both sides of a
    pair, six."""
    digests = {}
    for name, last in (("a", 9), ("b", 8)):
        _parquet(
            tmp_path / name,
            "t.parquet",
            f"SELECT 1 AS k, TIMESTAMP_NS '2024-01-01 00:00:00.12345678{last}' AS seen",
        )
        for typed in (None, {"seen": {"as": "timestamp"}}):
            plan, found = await planned(
                tmp_path / name, _table("t.parquet"), **({"typeMap": typed} if typed else {})
            )
            digests[name, bool(typed)] = (await scanned(found, plan | {"digest": ["seen"]}))[0][1]
    assert digests["a", False] != digests["b", False] and digests["a", True] == digests["b", True]


async def test_typed_keys_a_column_cannot_hold_are_on_no_row(tmp_path: Path) -> None:
    """A key part not of its class's form, or out of its Parquet type's range, is on no row, answered
    without a value of its column bound: an impossible date, an integer past a TINYINT, letters."""
    _parquet(
        tmp_path,
        "d.parquet",
        "SELECT DATE '2024-02-29' AS k, 1::TINYINT AS n UNION ALL SELECT DATE '2024-03-01', 2",
    )
    plan, found = await planned(tmp_path, _table("d.parquet"))
    dated = await read(found, plan, "2024-02-29", "2024-02-31", "x")
    assert {key: len(rows) for key, rows in dated.items()} == {"2024-02-29": 1, "2024-02-31": 0, "x": 0}
    plan, found = await planned(
        tmp_path, {"name": "t", "keyColumns": ["n"], "file": {"path": "d.parquet", "format": "parquet"}}
    )
    assert {key: len(rows) for key, rows in (await read(found, plan, "1", "300", "-0", "01")).items()} == {
        "1": 1, "300": 0, "-0": 0, "01": 0,
    }  # fmt: skip


async def test_a_partition_and_a_type_no_plan_reads_are_refused(tmp_path: Path) -> None:
    """A partition's name that is also a column of the files, and a type no plan reads (one that is
    neither nested nor read: DuckDB's own writer produces none, so the text is asserted where it is made)."""
    _parquet(tmp_path, "dt=1/p.parquet", "SELECT 1 AS k, 'x' AS dt")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table("dt=*/p.parquet", partitions=["dt"]))
    assert str(error.value) == "side a: partition 'dt' is also a column of the files"
    assert duck._parquet_type("flags", "BIT", None, {}) == (
        None,
        "column 'flags' (BIT) cannot be compared: its type is not read; list the columns to compare without"
        " it",
    )
    # A type is kept and worded by this process's grammar: one it reads as it is, any other by its head.
    typed = {
        "BIGINT": "BIGINT", "DECIMAL(12,2)": "DECIMAL(12,2)", "INTEGER[]": "LIST", "INTEGER[3]": "ARRAY",
        'STRUCT("a b" INTEGER)': "STRUCT", "ENUM('x', 'y')": "ENUM", '"NULL"': "NULL", "BIT": "BIT",
        "a type": None, "X" * 33: None, "": None,
    }  # fmt: skip
    assert {kind: duck._typed(kind) for kind in typed} == typed
    # A decimal whose scale is over its precision, which only a crafted footer declares: DuckDB reads it as
    # DECIMAL(12,30) and writes its text without an integer digit, so a key the scan read was no key text.
    scaled = [parquet_tiny.decimal("k", 150, 12, 30), parquet_tiny.Column("v", parquet_tiny.INT64, _q(7))]
    (tmp_path / "files" / "root" / "s.parquet").write_bytes(parquet_tiny.write(scaled))
    for table in (_table("s.parquet"), _table("s.parquet") | {"keyColumns": ["v"]}):
        with pytest.raises(ValueError) as error:
            await planned(tmp_path, table)
        assert str(error.value) == (
            "side a: column 'k' (DECIMAL(12,30)) cannot be compared: its type is not read; list the columns"
            " to compare without it"
        )
    assert duck._parquet_type("k", "DECIMAL(12,12)", None, {}) == ("decimal", "")


@pytest.mark.parametrize(
    ("path", "column", "value"),
    [("region=eu/dt=*/part.parquet", "region", "eu"), ("k=zzz/dt=*/part.parquet", "k", "zzz")],
)
async def test_a_name_value_directory_above_the_partitions_is_refused(
    tmp_path: Path, path: str, column: str, value: str
) -> None:
    """A name=value directory above the declared partitions is one more partition to DuckDB, whose
    value stands for the files' own column of that name in every row (a compared column read 'eu' whatever
    the rows held; a key column gave every row one key): the plan is refused. Undeclared, no directory is
    read as a column."""
    rows = "SELECT * FROM (VALUES ('k1', 'us', 1), ('k2', 'apac', 2), ('k3', 'eu', 3)) AS t(k, region, n)"
    written = _parquet(tmp_path, path.replace("*", "2024-01-01"), rows)
    seen = unrestricted().execute(
        f'SELECT DISTINCT "{column}" FROM read_parquet($f, hive_partitioning=true)',  # noqa: S608 (constants)
        {"f": [str(written)]},
    )
    assert seen.fetchall() == [(value,)]  # observed on 1.5.5: the directory's, for each of the three rows
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, _table(path, partitions=["dt"]))
    assert str(error.value) == (
        "side a: a directory above the declared partitions (dt) is named name=value, in the path or in the"
        " root of target exports: it would be read as a column of every row; rename it"
    )
    plan, found = await planned(tmp_path, _table(path))
    read_ = await read(found, plan, "k1", "k2", "k3")
    assert [_loaded(rows[0][0]) for rows in read_.values()] == [
        {"region": "us", "n": 1}, {"region": "apac", "n": 2}, {"region": "eu", "n": 3},
    ]  # fmt: skip


async def test_a_row_over_sixteen_mebibytes_stays_in_its_file(tmp_path: Path) -> None:
    """A row whose bytes and JSON values pass 16 MiB is not fetched: its per-key read is a
    resource limit and its scan digest the marker no row has, while the rows beside it are read."""
    _parquet(
        tmp_path,
        "big.parquet",
        "SELECT 1 AS k, repeat('x', 17 * 1024 * 1024)::BLOB AS raw UNION ALL SELECT 2, 'y'::BLOB",
    )
    plan, found = await planned(tmp_path, _table("big.parquet"))
    rows = await read(found, plan, "1", "2")
    assert isinstance(rows["1"][0][0], jsonio.ResourceLimitError) and "x" * 64 not in str(rows["1"][0][0])
    assert _loaded(rows["2"][0][0]) == {"raw": "\\x79"}
    digests = dict(await scanned(found, plan | {"digest": ["raw"]}))  # across engines: the worker's column
    assert digests["1"] == render.RESOURCE_LIMIT and len(digests["2"]) == 32
    hashed = dict(await scanned(found, plan))  # hashed where it lies, in a pair of two file sides
    assert len(hashed["1"]) == len(hashed["2"]) == 32
