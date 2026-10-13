"""DuckDB's CSV reader on the locked instance, observed
on the locked version: what each gate holds, and what 1.5.5 does where it was expected to do otherwise."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
import zstandard

from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.tables import TableConnector
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.types import ExecutionConfig, Side
from tests.unit.connectors.ducked import csv_table, file_spec, locked, planned
from tests.unit.connectors.ducked import read as ducked_read
from tests.unit.connectors.ducked import scanned as ducked_scanned

# The CSV reader (strict_mode left to DuckDB's default, which is strict: see test_csv_newlines).
READER = (
    "read_csv($files, auto_detect=false, columns=$columns, header=$header, delim='|', new_line=$new_line,"
    " quote='\"', escape='\"', encoding=$encoding, compression=$compression, nullstr='',"
    " allow_quoted_nulls=false, null_padding=false, union_by_name=false, filename=false, max_line_size=$max,"
    " ignore_errors=true{more})"
)
COLUMNS = {"id": "VARCHAR", "v": "VARCHAR"}


def _read(
    con: Any, files: list[Path], *, more: str = "", project: str = "*", tail: str = "", **bound: Any
) -> list[tuple[Any, ...]]:
    values = {"columns": COLUMNS, "header": True, "new_line": "\\n", "encoding": "utf-8",
              "compression": "none", "max": 2097152} | bound  # fmt: skip
    statement = f"SELECT {project} FROM {READER.format(more=more)}{tail}"  # noqa: S608 (constants)
    return con.execute(statement, values | {"files": [str(f) for f in files]}).fetchall()


def _file(base: Path, name: str, data: bytes) -> Path:
    path = base / "files" / "root" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_csv_nulls_bom_encodings_and_compression(tmp_path: Path) -> None:
    """allow_quoted_nulls=false tells "" from an empty field; a UTF-8 BOM is no part of the first field;
    utf-16, latin-1, gzip and zstd read."""
    with locked(tmp_path) as made:
        bom = _file(tmp_path, "bom.csv", b'\xef\xbb\xbfid|v\n1|\n2|""\n3|x\n')
        assert _read(made.con, [bom]) == [("1", None), ("2", ""), ("3", "x")]
        assert _read(made.con, [bom], header=False, project="id")[0] == ("id",)  # the BOM is skipped
        latin = _file(tmp_path, "latin.csv", "id|v\n1|caf\xe9\n".encode("latin-1"))
        assert _read(made.con, [latin], encoding="latin-1") == [("1", "café")]
        wide = _file(tmp_path, "wide.csv", "id|v\n1|ü\n".encode("utf-16"))
        assert _read(made.con, [wide], encoding="utf-16") == [("1", "ü")]
        astral = _file(tmp_path, "astral.csv", "id|v\n1|ü\n2|😀\n".encode("utf-16"))  # a surrogate pair
        with pytest.raises(duckdb.InvalidInputException):  # the whole file, ignore_errors or not (observed)
            _read(made.con, [astral], encoding="utf-16")
        gz = _file(tmp_path, "g.csv.gz", gzip.compress(b"id|v\n1|g\n"))
        zs = _file(tmp_path, "z.csv.zst", zstandard.ZstdCompressor().compress(b"id|v\n1|z\n"))
        assert _read(made.con, [gz], compression="gzip") == [("1", "g")]
        assert _read(made.con, [zs], compression="zstd") == [("1", "z")]
        for spelling in ("latin1", "utf16", "windows-1252"):  # only the three spellings a spec takes
            with pytest.raises(duckdb.InvalidInputException):
                _read(made.con, [latin], encoding=spelling)


def test_rejects_do_not_depend_on_projection(tmp_path: Path) -> None:
    """As observed: a wrong field count and an over-long line are skipped whatever a statement projects,
    but invalid encoding is found only in a field that is read. A statement that reads every declared column
    (an unread column is named by ``"c" IS NULL OR "c" IS NOT NULL``) skips the counting
    pass's lines exactly."""
    with locked(tmp_path) as made:
        three = {"id": "VARCHAR", "v": "VARCHAR", "w": "VARCHAR"}
        data = b"id|v|w\n1|a|x\n2|b\xff|y\n3|c\n4|d|e|f\n9|" + b"z" * 3000 + b"|q\n5|e|z\n"
        found = _file(tmp_path, "rejects.csv", data)

        def ids(project: str, where: str = "") -> list[str]:
            statement = f"SELECT {project} FROM {READER.format(more='')}{where} ORDER BY 1"  # noqa: S608 (constants)
            values = {"columns": three, "header": True, "new_line": "\\n", "encoding": "utf-8",
                      "compression": "none", "max": 2048, "files": [str(found)]}  # fmt: skip
            return [row[0] for row in made.con.execute(statement, values).fetchall()]

        assert ids("id") == ["1", "2", "5"] and ids("w") == ["x", "y", "z"]  # line 3 kept: v is not read
        assert ids("v") == ["a", "e"] and ids("id || v || w") == ["1ax", "5ez"]  # line 3 skipped
        every = ' WHERE ("v" IS NULL OR "v" IS NOT NULL) AND ("w" IS NULL OR "w" IS NOT NULL)'
        assert ids("id", every) == ["1", "5"]
        cursor = made.con.cursor()
        counted = cursor.execute(
            f"SELECT count(*) FROM {READER.format(more=', store_rejects=true, rejects_limit=1001')}",  # noqa: S608 (constants)
            {"columns": three, "header": True, "new_line": "\\n", "encoding": "utf-8", "compression": "none",
             "max": 2048, "files": [str(found)]},
        ).fetchall()  # fmt: skip
        kinds = cursor.execute("SELECT line, error_type FROM reject_errors ORDER BY line").fetchall()
        assert counted == [(3,)] and kinds == [
            (4, "MISSING COLUMNS"),
            (5, "TOO MANY COLUMNS"),
            (6, "LINE SIZE OVER MAXIMUM"),
        ]


def test_csv_newlines(tmp_path: Path) -> None:
    """As observed: ``new_line`` is the escaped text (``\\n``, ``\\r\\n``), honoured with
    auto_detect=false: a ``\\r\\n`` file leaves no ``\\r`` in a field, under DuckDB's default strict mode.
    Explicit ``strict_mode=true`` with ``\\r\\n`` reads no row and records no reject. A file of other or
    mixed line ends makes the plan's first-record read raise, and the counting pass alone would read
    it as no rows and no rejects: the first-record read runs on every file before it."""
    with locked(tmp_path) as made:
        crlf = _file(tmp_path, "crlf.csv", b"id|v\r\n1|a\r\n2|b\r\n")
        assert _read(made.con, [crlf], new_line="\\r\\n") == [("1", "a"), ("2", "b")]
        assert _read(made.con, [crlf], new_line="\\r\\n", more=", strict_mode=true") == []
        for raw in ("\n", "\r\n"):  # a real line end is refused: only its escaped text is taken
            with pytest.raises(duckdb.InvalidInputException):
                _read(made.con, [crlf], new_line=raw)
        others = {
            "crlf.csv": b"id|v\r\n1|a\r\n2|b\r\n", "cr.csv": b"id|v\r1|a\r2|b\r",
            "mixed.csv": b"id|v\n1|a\n2|b\r\n3|c\n", "mixed2.csv": b"id|v\r\n1|a\r\n2|b\n3|c\r\n",
        }  # fmt: skip
        for name, data in others.items():
            found = _file(tmp_path, name, data)
            declared = "\\n" if name in ("crlf.csv", "cr.csv", "mixed2.csv") else "\\r\\n"
            with pytest.raises(duckdb.InvalidInputException):
                _read(made.con, [found], new_line=declared, header=False, tail=" LIMIT 1")
        assert _read(made.con, [_file(tmp_path, "crlf.csv", others["crlf.csv"])]) == []  # silently none


def test_rejects_belong_to_their_cursor(tmp_path: Path) -> None:
    """store_rejects works with auto_detect=false on the locked instance and gives each line's file,
    line and kind; its two tables are its cursor's and go when it closes."""
    with locked(tmp_path) as made:
        bad = _file(tmp_path, "bad.csv", b'id|v\n1|a\n2|b|c\n3|"x\n4|d\n')
        good = _file(tmp_path, "good.csv", b"id|v\n7|g\n")
        cursor = made.con.cursor()
        rows = _read(cursor, [good, bad], more=", store_rejects=true, rejects_limit=1001")
        assert rows == [("7", "g"), ("1", "a")]  # line 4 lies inside the unterminated quote of line 3
        rejected = cursor.execute(
            "SELECT s.file_path, e.line, e.error_type FROM reject_errors e"
            " JOIN reject_scans s USING (scan_id, file_id) ORDER BY e.line"
        ).fetchall()
        assert rejected == [(str(bad), 3, "TOO MANY COLUMNS"), (str(bad), 4, "UNQUOTED VALUE")]
        other = made.con.cursor()
        with pytest.raises(duckdb.CatalogException):
            other.execute("SELECT count(*) FROM reject_errors")
        cursor.close()
        with pytest.raises(duckdb.CatalogException):
            made.con.cursor().execute("SELECT count(*) FROM reject_scans")
        # The tables are made when the statement's rows are read to their end: a count fetched alone, then
        # another statement, stores none (observed), so the counting pass reads every batch.
        early = made.con.cursor()
        counted = early.execute(f"SELECT count(*) FROM {READER.format(more=', store_rejects=true')}", {  # noqa: S608 (constants)
            "files": [str(bad)], "columns": COLUMNS, "header": True, "new_line": "\\n", "encoding": "utf-8",
            "compression": "none", "max": 2097152}).fetchone()  # fmt: skip
        assert counted == (1,)
        with pytest.raises(duckdb.CatalogException):
            early.execute("SELECT count(*) FROM reject_errors")


def test_an_invalid_encoding_rejected_under_an_aggregate_invalidates_the_database(tmp_path: Path) -> None:
    """As observed on 1.5.5: with store_rejects, a line of invalid UTF-8 in a column that an aggregate
    reads (count(v)) raises InternalException and invalidates the database; the same column selected is
    recorded as INVALID ENCODING. The counting pass selects the columns, and a cursor drops an instance an
    internal error invalidated."""
    found = tmp_path / "enc.csv"
    found.write_bytes(b"id|v\n1|a\n2|b\xff\n3|c\n")
    reader = (
        f"read_csv('{found}', auto_detect=false, columns={{'id': 'VARCHAR', 'v': 'VARCHAR'}}, header=true,"
        " delim='|', ignore_errors=true, store_rejects=true)"
    )
    selected = duckdb.connect(":memory:")
    assert len(selected.execute(f"SELECT * FROM {reader}").fetchall()) == 2  # noqa: S608 (constants)
    assert selected.execute("SELECT line, error_type FROM reject_errors").fetchall() == [
        (3, "INVALID ENCODING")
    ]
    aggregated = duckdb.connect(":memory:")
    with pytest.raises(duckdb.InternalException):
        aggregated.execute(f"SELECT count(v) FROM {reader}").fetchall()  # noqa: S608 (constants)
    with pytest.raises(duckdb.FatalException):
        aggregated.execute("SELECT 1").fetchall()


# --- the CSV reader of a file side -----------------------------------------------------

DIALECTS = [
    ("plain", b"id|v\n1|a\n2|\n", {}, {"1": "a", "2": None}),  # an unquoted empty field is NULL
    ("quoted null", b'id|v\n1|""\n2|x\n', {}, {"1": "", "2": "x"}),  # quoted, the empty string
    ("null text", b"id|v\n1|NULL\n2|\n", {"null": "NULL"}, {"1": None, "2": ""}),
    ("crlf", b"id|v\r\n1|a\r\n2|b\r\n", {"newline": "\r\n"}, {"1": "a", "2": "b"}),
    ("no quote", b'id|v\n1|a"b\n2|c\n', {"quote": "", "escape": ""}, {"1": 'a"b', "2": "c"}),
    ("escape", b'id|v\n1|"a\\"b"\n2|c\n', {"escape": "\\"}, {"1": 'a"b', "2": "c"}),
    ("skip", b"an export of 2\nid|v\n1|a\n2|b\n", {"skip": 1}, {"1": "a", "2": "b"}),
    ("headerless", b"1|a\n2|b\n", {"header": False}, {"1": "a", "2": "b"}),
    ("bom", b"\xef\xbb\xbfid|v\n1|a\n2|b\n", {}, {"1": "a", "2": "b"}),
    ("latin-1", "id|v\n1|café\n2|b\n".encode("latin-1"), {"encoding": "latin-1"}, {"1": "café", "2": "b"}),
    ("utf-16", "id|v\n1|ü\n2|b\n".encode("utf-16"), {"encoding": "utf-16"}, {"1": "ü", "2": "b"}),
    ("gzip", gzip.compress(b"id|v\n1|g\n2|h\n"), {"compression": "gzip"}, {"1": "g", "2": "h"}),
    (
        "zstd",
        zstandard.ZstdCompressor().compress(b"id|v\n1|z\n2|y\n"),
        {"compression": "zstd"},
        {"1": "z", "2": "y"},
    ),
    ("comma", b"id,v\n1,a\n2,b\n", {"delimiter": ","}, {"1": "a", "2": "b"}),
    ("padded", b"id|v\n1|ab   \n2| b\n", {"padded": True}, {"1": "ab", "2": " b"}),  # class char
    ("quoted line end", b'id|v\n1|"a\nb"\n2|c\n', {}, {"1": "a\nb", "2": "c"}),
]


@pytest.mark.parametrize(("name", "data", "csv", "values"), DIALECTS, ids=[d[0] for d in DIALECTS])
async def test_a_csv_side_reads_its_declared_dialect(
    tmp_path: Path, name: str, data: bytes, csv: dict[str, Any], values: dict[str, Any]
) -> None:
    """Each dialect member, declared, never sniffed: the per-key rows and the counts of the plan's pass."""
    _file(tmp_path, "t.csv", data)
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v"], **csv))
    assert (plan["rows"], plan["selected"], plan["rejected"], plan["kinds"]) == (2, 2, 0, {})
    assert plan["lines_skipped"] == csv.get("skip", 0)  # by the spec: said in the census, never rows
    assert plan["classes"] == {
        "id": "char" if csv.get("padded") else "text",
        "v": "char" if csv.get("padded") else "text",
    }
    read = await ducked_read(found, plan, "1", "2")
    assert {key: json.loads(rows[0][0])["v"] for key, rows in read.items()} == values
    assert [row[0] for row in await ducked_scanned(found, plan)] == ["1", "2"]


def _refused(error: pytest.ExceptionInfo[BaseException]) -> str:
    return str(error.value)


@pytest.mark.parametrize(
    ("data", "csv", "message"),
    [
        (
            b"id|v|w\n1|a|b\n",
            {},
            "side a: the first record of file 1 of 1 has 3 fields, not the 2 of csv.columns",
        ),
        (b"id\n1|a\n", {}, "side a: the first record of file 1 of 1 has 1 fields, not the 2 of csv.columns"),
        (b"", {}, "side a: the first record of file 1 of 1 has 0 fields, not the 2 of csv.columns"),
        (  # a compressed file, undeclared: its first bytes are no header
            zstandard.ZstdCompressor().compress(b"id|v\n1|a\n"),
            {},
            "side a: the header of file 1 of 1 does not name field 1 as csv.columns does",
        ),
        (
            b"id|value\n1|a\n",
            {},
            "side a: the header of file 1 of 1 does not name field 2 as csv.columns does",
        ),
        (b"Id|V\n1|a\n", {}, None),  # names fold: lower case
        (
            b'id|"v\r"\n1|a\n',
            {},
            "side a: file 1 of 1 ends its lines with \\r\\n where csv.newline declares \\n",
        ),
        (b"s3cret|canary\n1|a\n", {"header": False}, None),  # a headerless file's first record is a row
    ],
)
async def test_each_file_s_first_record_is_checked(
    tmp_path: Path, data: bytes, csv: dict[str, Any], message: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Compared with csv.columns, never kept: no value of it reaches a message or a log."""
    _file(tmp_path, "t.csv", data)
    if message is None:
        await planned(tmp_path, csv_table("t.csv", ["id", "v"], **csv))
        return
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, csv_table("t.csv", ["id", "v"], **csv))
    assert _refused(error) == message and "s3cret" not in caplog.text


async def test_a_file_of_other_line_ends_is_refused_by_the_reader(tmp_path: Path) -> None:
    """A \\r\\n file declared \\n raises at its first record, never reads as no rows."""
    _file(tmp_path, "t.csv", b"id|v\r\n1|a\r\n")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, csv_table("t.csv", ["id", "v"]))
    assert (
        _refused(error)
        == "side a: its files could not be read while the run was planned (InvalidInputException)"
    )


async def test_lines_that_are_not_rows_are_counted_by_their_kind(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Each line once, by file and line, of its kind; never dropped silently, never quoted.
    A file cut inside a row counts one line."""
    _file(
        tmp_path,
        "a.csv",
        b"id|v|w\n1|a|x\n2|b\xff|y\n3|c\n4|d|e|s3cret\n9|" + b"z" * 2_100_000 + b"|q\n5|e|z\n",
    )
    _file(tmp_path, "b.csv", b'id|v|w\n6|f|g\n7|"cut')
    plan, found = await planned(tmp_path, csv_table("*.csv", ["id", "v", "w"]))
    assert (plan["file_count"], plan["rows"], plan["selected"]) == (2, 3, 3)
    assert (plan["rejected"], plan["kinds"]) == (5, {"columns": 2, "encoding": 1, "line_size": 1, "quote": 1})
    assert "s3cret" not in caplog.text and "s3cret" not in json.dumps(plan)
    assert (  # logged by position and kind
        "side a: 5 line(s) of its files are not rows: file 1 line 3: encoding; file 1 line 4: columns; file 1"
        " line 5: columns; file 1 line 6: line_size; file 2 line 3: quote"
    ) in caplog.text
    assert {
        key: rows for key, rows in (await ducked_read(found, plan, "1", "6", "7")).items() if not rows
    } == {"7": []}


SCAN = 8 * 2**20  # where a second scan of DuckDB's parallel reader starts (observed on 1.5.5)
P33 = (
    "side a: its files hold line ends inside quoted fields, and whether a file ends inside one could not be"
    " checked: export them without line ends inside fields, or as Parquet"
)


def _spanning(at: int, second: bytes, then: bytes = b"") -> tuple[bytes, int]:
    """A CSV, and its rows of two fields, whose byte ``at`` lies in the first line of a quoted field holding a
    line end, ``second`` its second line and ``then`` what follows its record."""
    row = b"0|a row of the export\n"
    head = b"id|v\n" + row * ((at - 64) // len(row))
    spans = b'1|"' + b"x" * (at - len(head)) + b"\n" + second + b'"\n'
    return head + spans + then + row * 1000, head.count(b"\n") + 1000


def test_the_parallel_reader_takes_a_line_of_a_quoted_field_for_a_row(tmp_path: Path) -> None:
    """Observed on 1.5.5: where a scan of the parallel reader starts inside a quoted field that holds a
    line end, the field's next line is one more row when it has the declared fields, and no reject; the
    reader that is not parallel reads the rows there are, also across its 32 MiB buffer's end."""
    count = f"SELECT count(*) FROM {READER.format(more=', parallel=$parallel')}"  # noqa: S608 (constants)
    with locked(tmp_path) as made:
        for at in (SCAN, 4 * SCAN):
            data, rows = _spanning(at, b"its second line|reads as a row")
            found = _file(tmp_path, "spans.csv", data)
            values = {"files": [str(found)], "columns": COLUMNS, "header": True, "new_line": "\\n",
                      "encoding": "utf-8", "compression": "none", "max": 2097152}  # fmt: skip
            read = {p: made.con.execute(count, values | {"parallel": p}).fetchall() for p in (True, False)}
            assert read == {True: [(rows + 1,)], False: [(rows,)]}, at
        # A line of the field that is no row, before a record that is none either: it is a line that is not
        # a row to the parallel reader, which numbers the record after it one on.
        data, rows = _spanning(SCAN, b"its second line", b"short\n")
        _file(tmp_path, "spans.csv", data)
        stored, short = count.replace(", parallel=", ", store_rejects=true, parallel="), rows - 998
        for parallel, lines in ((True, [(short,), (short + 1,)]), (False, [(short,)])):
            cursor = made.con.cursor()
            assert cursor.execute(stored, values | {"parallel": parallel}).fetchall() == [(rows,)]
            assert cursor.execute("SELECT DISTINCT line FROM reject_errors ORDER BY 1").fetchall() == lines


async def test_a_quoted_field_s_line_ends_are_read_across_a_scan_s_start(tmp_path: Path) -> None:
    """The reader is not parallel, so a quoted field holding a line end is one field wherever it lies. The
    plan's end check reads each file with the parallel reader too, for a last record cut inside a quoted
    field: where that one's rows or its lines that are not rows are not the reader's (a line of the field
    read as a row; or, a line that is no row following the field, as a line that is not), no file's end is
    known and the plan is refused."""
    data, rows = _spanning(SCAN, b"its second line")  # no row of two fields: every reader agrees
    _file(tmp_path, "t.csv", data + b'short\n2|"cut')
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v"]))
    assert (plan["rows"], plan["selected"]) == (rows, rows)
    assert (plan["rejected"], plan["kinds"]) == (2, {"columns": 1, "quote": 1})
    ((document, _),) = (await ducked_read(found, plan, "1"))["1"]
    assert json.loads(document)["v"].endswith("x\nits second line")
    for second, then in ((b"its second line|reads as a row", b""), (b"its second line", b"short\n")):
        _file(tmp_path, "t.csv", _spanning(SCAN, second, then)[0])
        with pytest.raises(ValueError) as error:
            await planned(tmp_path, csv_table("t.csv", ["id", "v"]))
        assert _refused(error) == P33


def test_the_reader_drops_a_record_cut_inside_a_quoted_field(tmp_path: Path) -> None:
    """Observed on 1.5.5: a file's last record cut inside a quoted field is dropped without a reject by
    the reader that is not parallel, and by the parallel one too once it reads many files a statement (12
    with 2 threads: one thread a file); the parallel reader of one file records it, also with one thread,
    and in an encoded or a compressed file."""
    more, cut = ", store_rejects=true, parallel=$parallel", 'id|v\n3|a\n4|"cut'
    with locked(tmp_path) as made:
        files = [_file(tmp_path, f"{n:02d}.csv", b"id|v\n%d|a\n" % n) for n in range(12)]
        files[3].write_bytes(cut.encode())
        for parallel, read, rejected in (
            (False, files[3:4], []),
            (True, files, []),
            (True, files[3:4], [(3, "UNQUOTED VALUE")]),
        ):
            cursor = made.con.cursor()
            assert len(_read(cursor, read, more=more, parallel=parallel)) == len(read)
            assert cursor.execute("SELECT line, error_type FROM reject_errors").fetchall() == rejected
    forms = {
        "latin-1": (cut.encode("latin-1"), {"encoding": "latin-1"}),
        "utf-16": (cut.encode("utf-16"), {"encoding": "utf-16"}),
        "gzip": (gzip.compress(cut.encode()), {"compression": "gzip"}),
        "zstd": (zstandard.ZstdCompressor().compress(cut.encode()), {"compression": "zstd"}),
    }
    with locked(tmp_path / "one", threads=1) as made:
        for name, (data, bound) in forms.items():
            cursor = made.con.cursor()
            found = _file(tmp_path / "one", name, data)
            assert _read(cursor, [found], more=more, parallel=True, **bound) == [("3", "a")], name
            rejected = cursor.execute("SELECT line, error_type FROM reject_errors").fetchall()
            assert rejected == [(3, "UNQUOTED VALUE")], name


async def test_a_file_cut_inside_a_quoted_field_counts_one_line_whatever_the_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The end check finds it in a side of many files, in a first field and beside lines of a
    quoted field, each line that is not a row counted once; a side that declares no quote has no such end."""
    for n in range(12):
        _file(tmp_path, f"many/{n:02d}.csv", b'id|v\n%d|"two\nlines"\n%d|short|line|s3cret\n' % (n, n))
    _file(tmp_path, "many/03.csv", b'id|v\n3|a\n4|"cut\n')
    _file(tmp_path, "many/08.csv", b'id|v\n8|a\n"cut')
    plan, _ = await planned(tmp_path, csv_table("many/", ["id", "v"]))
    assert (plan["file_count"], plan["rows"]) == (12, 12)
    assert (plan["rejected"], plan["kinds"]) == (12, {"columns": 10, "quote": 2})
    assert "file 4 line 3: quote" in caplog.text and "file 9 line 3: quote" in caplog.text
    assert "s3cret" not in caplog.text
    _file(tmp_path, "bare.csv", b'id|v\n1|"a\n2|"cut')
    plan, found = await planned(tmp_path, csv_table("bare.csv", ["id", "v"], quote="", escape=""))
    assert (plan["rows"], plan["rejected"]) == (2, 0)
    assert json.loads((await ducked_read(found, plan, "2"))["2"][0][0]) == {"v": '"cut'}


async def test_a_csv_side_reads_its_partitions(tmp_path: Path) -> None:
    """``key=value`` directories read as text columns, beside a header and a quoted line end."""
    _file(tmp_path, "dt=2024-01-01/a.csv", b'id|v\n1|"a\nb"\n')
    _file(tmp_path, "dt=2024-01-02/a.csv", b"id|v\n2|c\n")
    table = csv_table("dt=*/a.csv", ["id", "v"])
    table["file"]["partitions"] = ["dt"]
    plan, found = await planned(tmp_path, table)
    assert plan["declared"] == {"id": "a CSV field", "v": "a CSV field", "dt": "a partition directory"}
    read = await ducked_read(found, plan, "1", "2")
    assert {key: json.loads(rows[0][0]) for key, rows in read.items()} == {
        "1": {"v": "a\nb", "dt": "2024-01-01"},
        "2": {"v": "c", "dt": "2024-01-02"},
    }
    # A name=value directory above them would be read as a column too, in place of the field of its name.
    _file(tmp_path, "v=eu/dt=2024-01-01/a.csv", b"id|v\n1|us\n")
    table = csv_table("v=eu/dt=*/a.csv", ["id", "v"])
    table["file"]["partitions"] = ["dt"]
    with pytest.raises(
        ValueError, match=r"side a: a directory above the declared partitions \(dt\) is named"
    ):
        await planned(tmp_path, table)


async def test_a_partitioned_side_is_counted_by_its_paths(tmp_path: Path) -> None:
    """A side's counts are kept a process by its digest and its declaration, and a partitioned side's
    digest binds its paths, whose directories' names are compared values. The same bytes moved under another
    directory were planned with the first day's counts (none selected of 2), and the scan that then read 2
    rows failed its run (RowsMoved), until the process restarted."""
    _file(tmp_path, "t/dt=2026-10-01/part.csv", b"id|v\n1|a\n2|b\n")
    table = csv_table("t/", ["id", "v"]) | {"where": "dt = '2026-10-02'"}
    table["file"]["partitions"] = ["dt"]
    first, _ = await planned(tmp_path, table)
    (tmp_path / "files" / "root" / "t" / "dt=2026-10-01").rename(
        tmp_path / "files" / "root" / "t" / "dt=2026-10-02"
    )
    moved, found = await planned(tmp_path, table)
    assert (first["rows"], first["selected"], moved["rows"], moved["selected"]) == (2, 0, 2, 2)
    assert first["files_sha256"] != moved["files_sha256"]
    assert [row[0] for row in await ducked_scanned(found, moved)] == ["1", "2"]


@pytest.mark.parametrize(
    ("where", "what"),
    [("v = 5", "a number: quote the literal"), ("v = TRUE", "TRUE or FALSE: quote it")],
)
async def test_a_csv_filter_compares_text(tmp_path: Path, where: str, what: str) -> None:
    """A CSV column is text, compared in binary order; a number or boolean literal is refused."""
    _file(tmp_path, "t.csv", b"id|v\n1|5\n2|10\n")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, csv_table("t.csv", ["id", "v"]) | {"where": where})
    assert _refused(error) == (
        f"side a: the filter compares column 'v' (a CSV field, which is text) with {what} to compare text"
    )
    plan, _ = await planned(tmp_path, csv_table("t.csv", ["id", "v"]) | {"where": "v > '10'"})
    assert (plan["rows"], plan["selected"]) == (2, 1)  # '5' > '10' as text


async def test_over_a_thousand_lines_that_are_not_rows_refuse_the_run(tmp_path: Path) -> None:
    """Over ``REJECT_MAX`` such lines: the dialect does not fit."""
    _file(tmp_path, "t.csv", b"id|v\n" + b"".join(b"%d|a|b\n" % i for i in range(1001)) + b"1|a\n")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, csv_table("t.csv", ["id", "v"]))
    assert _refused(error) == (
        "side a: over 1,000 lines of its files are not rows of the declared CSV dialect: the dialect does not"
        " fit (ds target check drafts one)"
    )


async def test_a_column_most_of_whose_values_do_not_convert_is_refused(tmp_path: Path) -> None:
    """The first rows of file 1, converted by typeMap and csv.keys."""
    _file(
        tmp_path,
        "t.csv",
        b"id|opened\n" + b"".join(b"%d|2024-01-%02d\n" % (i, i % 28 + 1) for i in range(1, 11)),
    )
    table = csv_table("t.csv", ["id", "opened"])
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, table, typeMap={"opened": {"as": "date", "format": "%d/%m/%Y"}})
    assert _refused(error) == (
        "side a: column 'opened' is declared date and 10 of the first 10 rows do not read as one: declare the"
        " format the export holds (typeMap format), or string"
    )
    _file(tmp_path, "u.csv", b"id|v\n" + b"".join(b"x%d|a\n" % i for i in range(10)))
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, csv_table("u.csv", ["id", "v"], keys={"id": {"as": "integer"}}))
    assert "column 'id' is declared integer and 10 of the first 10 rows" in _refused(error)


PACKED = b"id|v\n" + b"1|a\n" * 1000


@pytest.mark.parametrize(
    ("data", "csv", "cls"),
    [
        (b"id|v\n1|a\n", {"compression": "gzip"}, "BadGzipFile"),  # not gzip
        (gzip.compress(PACKED)[:-30], {"compression": "gzip"}, "BadGzipFile"),
        (gzip.compress(PACKED)[:-3] + b"xyz", {"compression": "gzip"}, "BadGzipFile"),  # its checksum
        (zstandard.ZstdCompressor().compress(PACKED)[:-4], {"compression": "zstd"}, "ZstdError"),
    ],
    ids=["wrong compression", "gzip cut short", "gzip checksum", "zstd cut short"],
)
async def test_files_the_reader_cannot_read_are_refused_by_its_class(
    tmp_path: Path, data: bytes, csv: dict[str, Any], cls: str
) -> None:
    """Refused naming the class, never DuckDB's text. DuckDB reads a gzip stream cut short or damaged, and a
    zstd one cut short, as fewer rows without an error (observed): the plan checks each stream as it hashes
    its file."""
    _file(tmp_path, "t.csv", data)
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, csv_table("t.csv", ["id", "v"], **csv))
    assert _refused(error) == f"side a: its files could not be read while the run was planned ({cls})"


def test_duckdb_reads_a_damaged_stream_as_fewer_rows(tmp_path: Path) -> None:
    """Observed on 1.5.5: what the plan's stream check stands in for. Half a stream reads as some of its rows,
    at most one line reported."""
    data = b"id|v\n" + b"".join(b"%d|a\n" % i for i in range(100_000))
    reader = (
        "SELECT count(*) FROM read_csv($f, auto_detect=false, columns={'id': 'VARCHAR', 'v': 'VARCHAR'},"
        " header=true, delim='|', compression=$c, ignore_errors=true, store_rejects=true)"
    )
    with locked(tmp_path) as made:
        for kind, packed in (
            ("gzip", gzip.compress(data)),
            ("zstd", zstandard.ZstdCompressor().compress(data)),
        ):
            cut = _file(tmp_path, f"cut.{kind}", packed[: len(packed) // 2])
            cursor = made.con.cursor()
            ((counted,),) = cursor.execute(reader, {"f": [str(cut)], "c": kind}).fetchall()
            ((rejected,),) = cursor.execute("SELECT count(*) FROM reject_errors").fetchall()
            assert 0 < counted and 100_000 - counted > 1_000 and rejected <= 1, kind


async def test_csv_keys_are_read_as_declared(tmp_path: Path) -> None:
    """An integer key's leading zeros go (00123 is 123), a decimal keeps its scale and is never rounded, a
    date is its ISO text; a field of another form has no key text and is skipped."""
    _file(tmp_path, "i.csv", b"id|v\n00123|a\n-0|b\n1.5|c\n" + b"9" * 39 + b"|d\n")
    plan, found = await planned(tmp_path, csv_table("i.csv", ["id", "v"], keys={"id": {"as": "integer"}}))
    assert [row[0] for row in await ducked_scanned(found, plan)] == ["0", "123"]
    assert {k: len(v) for k, v in (await ducked_read(found, plan, "123", "00123", "0")).items()} == {
        "123": 1,
        "00123": 0,
        "0": 1,
    }
    _file(tmp_path, "d.csv", b"id|v\n1.5|a\n2|b\n1.234|c\n-0.50|d\n")
    plan, found = await planned(
        tmp_path, csv_table("d.csv", ["id", "v"], keys={"id": {"as": "decimal", "scale": 2}})
    )
    assert [row[0] for row in await ducked_scanned(found, plan)] == ["-0.50", "1.50", "2.00"]
    _file(tmp_path, "t.csv", b"id|v\n2024-02-29|a\n2024-2-1|b\n2024-02-31|c\n2024-03-01|d\n")
    plan, found = await planned(tmp_path, csv_table("t.csv", ["id", "v"], keys={"id": {"as": "date"}}))
    assert [row[0] for row in await ducked_scanned(found, plan)] == ["2024-02-29", "2024-03-01"]
    # A year 0000 is a date to DuckDB, 0001-01-01 (BC), which no key text holds: refused, never scanned.
    _file(tmp_path, "y.csv", b"id|v\n2024-02-29|a\n0000-01-01|b\n")
    with pytest.raises(ValueError) as error:
        await planned(tmp_path, csv_table("y.csv", ["id", "v"], keys={"id": {"as": "date"}}))
    assert _refused(error) == (
        "side a: key column 'id' holds 1 date(s) outside the years 0001 to 9999, which have no key text:"
        " filter those rows out, or export the key as text"
    )


async def test_no_value_reaches_a_message_or_a_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A value that does not convert is named by its column and type, a line that is no row
    by its position and kind; neither the value, the line's text nor DuckDB's own text is read, logged or
    kept in the plan."""
    _file(tmp_path, "t.csv", b"id|opened\n1|2024-01-01\n2|s3cret-canary\n3|2024-01-03\n4|s3cret|canary\n")
    table = csv_table("t.csv", ["id", "opened"])
    plan, found = await planned(tmp_path, table, typeMap={"opened": {"as": "date"}})
    rows = await ducked_read(found, plan, "1", "2")
    assert rows["1"][0][0] == '{"opened": "2024-01-01"}'
    connector = TableConnector(
        found.get("exports"),
        ExecutionConfig(),
        plan,
        gate=TargetGate.for_target(found.get("exports")),
    )
    cfg = compile_spec(file_spec(table, typeMap={"opened": {"as": "date"}}), targets=found).sides[Side.A]
    observed = await connector.fetch("2", cfg)
    await connector.aclose()
    assert (observed.error_class, observed.body) == ("unconvertible", None)
    assert observed.error_detail is not None and "opened" in observed.error_detail
    stored = {name: value for name, value in plan.items() if name != "pinned"}  # what its run stores of it
    said = json.dumps(stored) + caplog.text + observed.error_detail + observed.request_url
    assert "s3cret" not in said and "canary" not in said
    assert "side a: 1 line(s) of its files are not rows: file 1 line 5: columns" in caplog.text
