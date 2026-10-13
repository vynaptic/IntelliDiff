"""Canonical rendering 1: each class's canonical value from an engine's
rendered text, the row digest, and the golden corpus under tests/golden/engines/, which pins what
PostgreSQL, MySQL and Oracle render one column of every supported type as, and that portable values render
and digest alike on all three.
"""

from __future__ import annotations

import hashlib
import json
import random
import struct
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from deltasleuth.connectors.tables import ENGINES, Engine, Plan, compose, mysql, render
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.convert import ConversionError, convert_row
from deltasleuth.core.spec import TargetRegistry, load_targets
from deltasleuth.core.types import Target
from tests.conftest import MySQLServer, OracleServer

GOLDEN = Path(__file__).parents[2] / "golden" / "engines"


@pytest.mark.parametrize(
    ("cls", "rendered", "expected"),
    [
        ("integer", "00007", Decimal(7)),  # a ZEROFILL column's text
        ("integer", "-9223372036854775808", Decimal(-9223372036854775808)),
        ("decimal", "000001.50", Decimal("1.5")),
        ("decimal", ".5", Decimal("0.5")),  # TM9's forms
        ("decimal", "-.5", Decimal("-0.5")),
        ("decimal", "1E+125", Decimal("1E+125")),
        ("float", "1e300", Decimal("1E+300")),
        ("float", "0.0000001", Decimal("1E-7")),
        ("float", "1.0000000000000001E+000", Decimal("1.0")),  # seventeen digits read back as 1
        ("float", "Nan", "NaN"),
        ("float", "Inf", "Infinity"),
        ("float", "-Inf", "-Infinity"),
        ("float32", "1.1234567165374756", Decimal("1.1234567")),  # the value widened to a double
        ("float32", "1.1234599351882935", Decimal("1.1234599")),
        ("float32", "1.12345672E+000", Decimal("1.1234567")),  # nine digits
        ("float32", "16777216", Decimal(16777216)),
        ("float32", "-Inf", "-Infinity"),
        ("text", "", ""),
        ("char", "ab   ", "ab"),
        ("char", " ab", " ab"),
        ("boolean", "TRUE", True),
        ("date", "2024-02-29", "2024-02-29"),
        ("date", "0000-00-00", "0000-00-00"),  # MySQL's zero date keeps its text
        ("timestamp", "2024-05-01T12:00:00.120000", "2024-05-01T12:00:00.12"),
        ("timestamp", "2024-05-01T12:00:00.000000", "2024-05-01T12:00:00"),
        ("timestamp", "2024-05-01T12:00:00.123456789", "2024-05-01T12:00:00.123456789"),
        ("timestamp", "2024-00-00T00:00:00.000000", "2024-00-00T00:00:00"),  # partly zero
        ("timestamp", "-0044-03-15T00:00:00", "0044-03-15T00:00:00 BC"),  # SYYYY's 44 BC
        ("timestamp", " 0044-03-15T00:00:00", "0044-03-15T00:00:00"),  # and its AD 44
        ("instant", "2024-05-01T12:00:00.500000", "2024-05-01T12:00:00.5+00:00"),
        ("instant", "-0044-03-15T00:00:00", "0044-03-15T00:00:00+00:00 BC"),
        ("time", "03:04:05.120", "03:04:05.12"),
        ("time", "03:04:05.000000", "03:04:05"),
        ("time", "-12:00:00.500000", "-12:00:00.5"),  # MySQL's TIME outside a day keeps its text
        ("time", "838:59:59", "838:59:59"),
        ("other", "P1DT2H", "P1DT2H"),
        ("bytes", b"\x00\xff", "\\x00ff"),
        ("bytes", b"", "\\x"),
        ("json", '{"a": 1.0, "b": [1e2]}', {"a": Decimal("1.0"), "b": [Decimal(100)]}),
        ("json", "null", None),
        ("text", None, None),
    ],
)
def test_a_class_s_rendered_text_becomes_its_canonical_value(cls: str, rendered: Any, expected: Any) -> None:
    found = render.value(cls, rendered, "c")
    assert found == expected and type(found) is type(expected)
    assert jsonio.dumps(found) == jsonio.dumps(expected)  # as it is recorded and hashed


@pytest.mark.parametrize(
    ("cls", "rendered"),
    [
        ("integer", "1.5"),
        ("integer", "s3cret"),
        ("decimal", "1,5"),
        ("decimal", "1e999999999999999999999999999999"),  # an exponent Decimal cannot hold
        ("float", "s3cret"),
        ("float32", "1e39"),  # beyond 32 bits
        ("boolean", "s3cret"),
        ("date", "2024-02-29 00:00:00"),
        ("timestamp", "2024-05-01 12:00:00"),
        ("time", "s3cret"),
        ("bytes", "s3cret"),
        ("text", b"s3cret"),
        ("json", "{s3cret"),
        ("vector", "s3cret"),
    ],
)
def test_a_text_not_of_its_class_does_not_convert_and_is_never_shown(cls: str, rendered: Any) -> None:
    with pytest.raises(ConversionError) as exc:
        render.value(cls, rendered, "amount")
    assert str(exc.value) == f"column 'amount' does not read as {cls}"


def test_a_number_written_out_past_the_limit_is_a_resource_limit() -> None:
    with pytest.raises(jsonio.ResourceLimitError):
        render.value("decimal", "1E+999999999", "amount")


def _single(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def _reads(text: Decimal | str) -> float:
    """The 32-bit float a decimal text reads as, by exact arithmetic: of the floats around it the nearest,
    the even one at a tie; infinity from halfway between the greatest float and 2**128."""
    exact = abs(Fraction(text))
    greatest = _bits(0x7F7FFFFF)
    bits = struct.unpack(">I", struct.pack(">f", min(float(exact), greatest)))[0]  # a step off at most
    around = [bits + step for step in (-1, 0, 1) if 0 <= bits + step <= 0x7F800000]
    near = min(around, key=lambda b: (abs(_exactly(b) - exact), b & 1))
    return _bits(near) * (-1 if Fraction(text) < 0 else 1)


def _bits(bits: int) -> float:
    return struct.unpack(">f", struct.pack(">I", bits))[0]


def _exactly(bits: int) -> Fraction:
    return Fraction(2) ** 128 if bits == 0x7F800000 else Fraction(_bits(bits))


@pytest.mark.parametrize("value", [0.1, 1.1234567, 16777217.0, 3.4028235e38, 1e-45, 2.0**-126, 2.0**30, -0.0])
def test_float32_narrowing_is_the_shortest_text_that_reads_back(value: float) -> None:
    single = _single(value)
    found = render.value("float32", repr(single), "c")
    assert _reads(found) == single
    digits = len(found.as_tuple().digits)
    shorter = [f"{single:.{n - 1}e}" for n in range(1, digits)]
    assert not any(_reads(text) == single for text in shorter)  # no shorter text reads back


def test_float32_narrowing_just_above_a_power_of_two_takes_the_nearest_neighbour() -> None:
    # 2**-126 as a 32-bit float: its interval is half as wide below it, so the text nearest above it reads
    # back at a length where the correctly rounded one does not.
    for exponent in range(-126, 127):
        single = _single(2.0**exponent)
        found = render.value("float32", repr(single), "c")
        assert _reads(found) == single and len(found.as_tuple().digits) <= 9


def test_float32_narrowing_rounds_once() -> None:
    # 7.038531e-26 lies just below the midpoint of the floats 15ae43fd and 15ae43fe, and the double
    # nearest to it is that midpoint: rounded to a double and then to 32 bits it would read as the second,
    # which would then share the first's canonical value.
    lower, upper = _bits(0x15AE43FD), _bits(0x15AE43FE)
    assert _reads("7.038531e-26") == lower and _single(float("7.038531e-26")) == upper
    # The two floats widened to doubles, as MySQL renders them, and PostgreSQL's text of the first.
    texts = ("7.038530691851209e-26", "7.038531308148791e-26", "7.038531e-26")
    canonical = [str(render.value("float32", text, "c")) for text in texts]
    assert canonical == ["7.038531E-26", "7.0385313E-26", "7.038531E-26"]


@given(bits=st.integers(0, 0x7F7FFFFF), negative=st.booleans(), digits=st.integers(6, 17))
def test_a_32_bit_float_has_one_canonical_value_whatever_text_renders_it(
    bits: int, negative: bool, digits: int
) -> None:
    single = -_bits(bits) if negative else _bits(bits)
    found = render.value("float32", repr(single), "c")  # the float widened to a double: MySQL's text
    assert _reads(found) == single  # read back by exact arithmetic, it is that float
    rounded = f"{single:.{digits - 1}e}"  # a text of so many digits: Oracle's has nine
    if _reads(rounded) == single:  # and any text that reads as the float has its canonical value
        assert render.value("float32", rounded, "c") == found


SIDE = {
    "keys": ["k"],
    "columns": {"amount": "AMOUNT", "code": "CODE", "opened": "OPENED", "at": "AT"},
    "classes": {"k": "text", "AMOUNT": "decimal", "CODE": "char", "OPENED": "timestamp", "AT": "time"},
    "types": {"opened": {"as": "timestamp"}},
}


def test_digest_equals_postgres_shape() -> None:
    # A row MySQL rendered and the same row as PostgreSQL's to_jsonb text digest alike: sha256 of the empty
    # row's hash and the converted row's canonical JSON, as a PostgreSQL side's digest columns always were.
    rendered = ["1.50", "ab", "2024-05-01T12:00:00.120000", "03:04:05.120"]
    document = render.document(SIDE, list(SIDE["columns"]), rendered)
    assert document == {
        "amount": Decimal("1.5"), "code": "ab", "opened": "2024-05-01T12:00:00.12", "at": "03:04:05.12",
    }  # fmt: skip
    text = '{"at": "03:04:05.12", "code": "ab   ", "amount": 1.5, "opened": "2024-05-01T12:00:00.12"}'
    parsed = render.parsed(SIDE, text)
    assert parsed == document  # its char value trimmed
    converted = jsonio.dumps(convert_row(document, SIDE["types"]), sort_keys=True)
    assert converted == (
        '{"amount": 1.5, "at": "03:04:05.12", "code": "ab", "opened": "2024-05-01T12:00:00.120000Z"}'
    )
    expected = hashlib.sha256(render.EMPTY + converted.encode()).digest()
    assert render.EMPTY == hashlib.sha256(b"{}").digest()
    assert (
        render.digest(SIDE, render.EMPTY, document) == render.digest(SIDE, render.EMPTY, parsed) == expected
    )
    plain = SIDE | {"classes": None}  # a PostgreSQL pair: the row as to_jsonb wrote it
    assert render.parsed(plain, text)["code"] == "ab   "
    with pytest.raises(ConversionError):
        render.digest(SIDE, render.EMPTY, document | {"opened": "soon"})  # the scan marks the row


def test_a_postgresql_float_takes_the_canonical_form_in_a_pair_across_engines() -> None:
    # PostgreSQL prints the shortest text that reads back alone, a digit longer than the shortest for some
    # integers from 2**25 (real) and 2**54 (double precision): the same floats would differ from MySQL's
    # and Oracle's. Its text of a float that is not finite is the canonical one already.
    side = {
        "columns": {"f": "f", "d": "d", "g": "g", "n": "n", "i": "i"},
        "classes": {"f": "float32", "d": "float", "g": "float32", "n": "float", "i": "float32"},
        "types": {},
    }
    text = (
        '{"f": 4.6056592e+07, "d": 5.2990648348713776e+16, "g": 7.038531e-26, "n": "NaN", "i": "-Infinity"}'
    )
    parsed = render.parsed(side, text)
    assert jsonio.dumps(parsed) == (
        '{"f": 46056590, "d": 52990648348713780, "g": 0.00000000000000000000000007038531, "n": "NaN",'
        ' "i": "-Infinity"}'
    )
    for rendered in (  # the same five floats as MySQL and as Oracle render them
        ["46056592.0", "5.299064834871378e16", "7.038530691851209e-26", None, None],
        ["4.60565920E+007", "5.2990648348713776E+016", "7.03853069E-026", "Nan", "-Inf"],
    ):
        there = render.document(side, list(side["columns"]), rendered)
        there |= {name: parsed[name] for name in there if there[name] is None}  # MySQL holds no such float
        assert jsonio.dumps(there) == jsonio.dumps(parsed)
        assert render.digest(side, render.EMPTY, there) == render.digest(side, render.EMPTY, parsed)
    # A PostgreSQL pair keeps its rows as to_jsonb wrote them.
    plain = jsonio.dumps(render.parsed(side | {"classes": None}, text))
    assert plain.startswith('{"f": 46056592, "d": 52990648348713776, "g": 0.0000000000000000000000000703853')


@pytest.mark.parametrize(
    ("family", "classes"),
    [("text", ["text", "char"]), ("number", ["integer", "decimal", "float", "float32"])]
    + [(cls, [cls]) for cls in ("boolean", "date", "timestamp", "instant", "time", "bytes", "json", "other")],
)
def test_families(family: str, classes: list[str]) -> None:
    assert {render.family(cls) for cls in classes} == {family}


def test_a_type_map_entry_sets_its_side_s_family() -> None:
    mapped = {name: render.family("text", {"as": name}) for name in ("string", "integer", "decimal")}
    assert mapped == {"string": "text", "integer": "number", "decimal": "number"}
    assert [render.family("integer", {"as": name}) for name in ("boolean", "date", "json", "bytes")] == [
        "boolean", "date", "json", "bytes",
    ]  # fmt: skip
    assert render.family("timestamp", {"as": "timestamp"}) != render.family("timestamp")  # its Z form


# --- the golden corpus, on real engines -------------------------------------------------------------


async def _side(
    engine: Engine, target: Target, name: str, compared: list[str] | None = None, key: str = "k", **types: Any
) -> tuple[Plan, dict[str, list[Any]]]:
    """A side of ``name`` keyed by ``key`` and comparing every other column (or ``compared``), as a pair
    across engines stores it, with ``types`` its typeMap; and each column's declared type, class and
    whether it can be a key. Canonical names are lower-case, whatever case the catalogue holds."""
    session = await engine.connect(target)
    try:
        relation = await engine.describe(session, None, name)
    finally:
        await session.close()
    assert relation is not None
    names = {c.name.lower(): c.name for c in relation.columns}
    columns = {canonical: names[canonical] for canonical in compared or names if canonical != key}
    side = {
        "schema": relation.schema, "name": relation.name, "keys": [names[key]], "key_maps": [None],
        "columns": columns, "where": None, "types": types, "digest": sorted(columns), "engine": engine.name,
        "classes": {c.name: c.cls for c in relation.columns},
        "declared": {c.name: c.declared for c in relation.columns},
    }  # fmt: skip
    return side, {c.name.lower(): [c.declared, c.cls, c.keyable] for c in relation.columns}


async def _documents(
    engine: Engine, target: Target, name: str, compared: list[str] | None = None, **types: Any
) -> dict[str, Any]:
    """Each row's canonical document, by key, and its scan digest."""
    side, _ = await _side(engine, target, name, compared, **types)
    async with engine.snapshot(target) as session:
        digests = [row async for batch in await engine.scan(session, side, None) for row in batch]
        keys = [row[0] for row in digests]
        rows = await engine.rows(session, side, {key: [[key]] for key in keys})
    assert sorted(i for i, _, _ in rows) == list(range(len(keys)))
    return {keys[i]: (_loaded(document), digests[i][1]) for i, document, _ in rows}  # type: ignore[arg-type]


def _loaded(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)  # 1.50 and 1.5 are one number; no digit is rounded away


@pytest.fixture
def corpus(
    pg_dsn: str, mysql_server: MySQLServer, oracle_server: OracleServer, tmp_path: Path
) -> TargetRegistry:
    """Targets ``postgresql``, ``mysql`` and ``oracle``, each a fresh database (on Oracle an account's
    schema) holding the golden corpus."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute((GOLDEN / "postgres.sql").read_text())
    (tmp_path / "dsn").write_text(pg_dsn)
    database = mysql_server.database((GOLDEN / "mysql.sql").read_text())
    schema = oracle_server.schema((GOLDEN / "oracle.sql").read_text())
    entries = {
        "postgresql": {"dsn_ref": "file:dsn"},
        "mysql": mysql_server.target(database),
        "oracle": oracle_server.target(schema),
    }
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": entries}))
    return load_targets(tmp_path / "targets.yaml")


async def _read(
    corpus: TargetRegistry, table: str, *compared: str, **types: Any
) -> dict[str, dict[str, Any]]:
    """Each engine's documents and digests of a table of the corpus."""
    return {
        name: await _documents(ENGINES[name], corpus.get(name), table, list(compared), **types)
        for name in corpus
    }


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.oracle
async def test_golden_renderings(corpus: TargetRegistry) -> None:
    expected = _loaded((GOLDEN / "expected.json").read_text())
    found = await _read(corpus, "corpus")
    for engine in ("postgresql", "mysql", "oracle"):
        _, classes = await _side(ENGINES[engine], corpus.get(engine), "corpus")
        assert classes == expected["classes"][engine]  # each declared type's class, and a key or not
        assert {key: document for key, (document, _) in found[engine].items()} == expected[engine], engine


# The corpus columns that may hold a large value: the worker hashes them, the engine every other.
LARGE = {"mysql": ["c_blob", "c_json", "c_text"], "oracle": ["c_blob", "c_clob", "c_json", "c_nclob"]}


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.oracle
@pytest.mark.parametrize("engine", ["mysql", "oracle"])
async def test_an_engine_hashes_every_type_but_the_large_ones(corpus: TargetRegistry, engine: str) -> None:
    # In a pair of one engine the engine hashes the columns both sides declare alike but those that may
    # hold a large value: its hash takes every other type of the corpus, edge values and NULLs too.
    adapter, target, large = ENGINES[engine], corpus.get(engine), LARGE[engine]
    side, _ = await _side(adapter, target, "corpus")
    session = await adapter.connect(target)
    try:
        relation = await adapter.describe(session, None, "corpus")
    finally:
        await session.close()
    assert relation is not None and sorted(c.name.lower() for c in relation.columns if c.lob) == large
    async with adapter.snapshot(target) as session:
        first = [
            row
            async for batch in await adapter.scan(session, side | {"digest": large}, None)
            for row in batch
        ]
        again = [
            row
            async for batch in await adapter.scan(session, side | {"digest": large}, None)
            for row in batch
        ]
    assert first == again and len({digest for _, digest in first}) == len(first) == 6
    assert {len(digest) for _, digest in first} == {32}


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.oracle
@pytest.mark.parametrize("engine", ["mysql", "oracle"])
async def test_a_row_renders_alike_in_every_statement(corpus: TargetRegistry, engine: str) -> None:
    # A rendering expression must give one text whatever statement holds it. MySQL declares a cast of a
    # DOUBLE to CHAR 22 characters wide: a UNION of per-key arms and a scan that sorts in a temporary table
    # cut a longer text there, and a view's expression is rounded to fit. So each row of the corpus, its
    # types' longest texts among them, is read alone, in one batch and by a scan, from the table and from
    # a view that joins it (on MySQL its scan sorts in a temporary table) and computes a double.
    adapter, target = ENGINES[engine], corpus.get(engine)
    table, _ = await _side(adapter, target, "corpus")
    names = sorted(table["columns"])
    joined, _ = await _side(adapter, target, "corpus_joined", ["k", *names, "c_computed"], key="j")
    read: dict[str, dict[str, Any]] = {}
    for name, side in (("corpus", table), ("corpus_joined", joined)):
        async with adapter.snapshot(target) as session:
            scanned = [row async for batch in await adapter.scan(session, side, None) for row in batch]
            keys = [key for key, _ in scanned]
            alone = [(await adapter.rows(session, side, {key: [[key]]}))[0][1] for key in keys]
            batch = sorted(await adapter.rows(session, side, {key: [[key]] for key in keys}))
            if engine == "mysql" and name == "corpus_joined":
                statement = compose.scan(mysql.PRIMITIVES, side, None)[0]
                plans = await session.query(f"EXPLAIN {statement}")
                assert "Using temporary" in plans[0][-1]
        assert len(keys) == 6 and [document for _, document, _ in batch] == alone  # each row's JSON text
        documents = [jsonio.parse_json(document)[0] for document in alone]
        assert [digest for _, digest in scanned] == [  # and the scan hashed that document
            render.digest(side, render.EMPTY, document) for document in documents
        ]
        read[name] = {document.pop("k", key): document for key, document in zip(keys, documents, strict=True)}
    for key, document in read["corpus_joined"].items():
        assert document.pop("c_computed") == document["c_double"], key  # the view's expression, whole
    assert read["corpus_joined"] == read["corpus"]
    # The engine's own hash, of a pair of one engine, hashes the same texts: a row's is one through the
    # table and through the view, and the computed double's is the column's.
    same, _ = await _side(adapter, target, "corpus_joined", names, key="j")
    double = {"c_double": table["columns"]["c_double"]}
    hashed = []
    async with adapter.snapshot(target) as session:
        for side in (
            table | {"digest": LARGE[engine]},
            same | {"digest": LARGE[engine]},
            table | {"columns": double, "digest": []},
            joined | {"columns": {"c_double": joined["columns"]["c_computed"]}, "digest": []},
        ):
            rows = [row async for batch in await adapter.scan(session, side, None) for row in batch]
            hashed.append(sorted(digest for _, digest in rows))
    assert hashed[0] == hashed[1] and hashed[2] == hashed[3]
    assert len(set(hashed[0])) == 6 and len(set(hashed[2])) == 6
    whole = [hashlib.sha256(text).digest() for text in (b'["-1.7976931348623157e308"]', b'["0.1"]')]
    assert engine != "mysql" or set(whole) < set(hashed[2])  # SHA2 of the JSON array of the whole text


# What Oracle holds of the portable table: it has no date and no time, and its DATE holds a time.
SHARED = [
    *("c_integer", "c_decimal", "c_double", "c_real", "c_text", "c_char", "c_date", "c_timestamp"),
    *("c_instant", "c_bytes", "c_json"),
]


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.oracle
async def test_portable_values_digest_alike(corpus: TargetRegistry) -> None:
    expected = _loaded((GOLDEN / "portable.json").read_text())
    registry = {name: corpus.get(name) for name in ("postgresql", "mysql")}
    found = await _read(registry, "portable")  # type: ignore[arg-type]
    for engine in ("postgresql", "mysql"):
        assert {key: document for key, (document, _) in found[engine].items()} == expected["portable"], engine
    assert found["postgresql"] == found["mysql"]  # and each row's digest
    assert len({digest for _, digest in found["mysql"].values()}) == len(expected["portable"])
    keys = await _read(corpus, "amounts")
    amounts = [list(keys[engine]) for engine in ("postgresql", "mysql", "oracle")]
    assert amounts == [expected["amounts"]] * 3  # each side's stream in code-point order
    # All three: Oracle's DATE declared a date (typeMap), the columns it holds digest alike on every engine.
    shared = await _read(corpus, "portable", *SHARED, c_date={"as": "date"})
    # Among them whole floats from 2**25 and 2**54, PostgreSQL's own text of which is a digit longer.
    for key in ("typical", "small", "nulls", "power", "beyond"):
        documents = [shared[engine][key][0] for engine in ("postgresql", "mysql", "oracle")]
        midnight = documents[0]["c_date"] and documents[0]["c_date"] + "T00:00:00"  # before its typeMap
        assert documents[0] == documents[1] == documents[2] | {"c_date": documents[0]["c_date"]}, key
        assert documents[2]["c_date"] == midnight and len({shared[e][key][1] for e in shared}) == 1, key
    # The empty string, the empty CHAR and no bytes are NULL there: a reported difference ...
    edges, there = shared["postgresql"]["edges"], shared["oracle"]["edges"]
    absent = {"c_text": None, "c_char": None, "c_bytes": None, "c_date": "1000-01-01T00:00:00"}
    assert there[0] == edges[0] | absent and there[1] != edges[1] == shared["mysql"]["edges"][1]
    # ... until the other side's typeMap declares its empty strings NULL (no declaration bridges bytes).
    empty = {"as": "string", "emptyAsNull": True}
    bridged = await _read(corpus, "portable", *SHARED[:-2], c_date={"as": "date"}, c_text=empty, c_char=empty)
    assert len({bridged[engine]["edges"][1] for engine in bridged}) == 1


@pytest.mark.db
@pytest.mark.mysql
@pytest.mark.oracle
async def test_the_same_floats_read_and_digest_alike_on_every_engine(
    corpus: TargetRegistry, pg_dsn: str, mysql_server: MySQLServer, oracle_server: OracleServer
) -> None:
    # The same floats, bit for bit, in PostgreSQL's double precision and real, MySQL's DOUBLE and FLOAT
    # and Oracle's BINARY_DOUBLE and BINARY_FLOAT. Each engine's own text of a float is another: whole
    # floats from 2**54 and 2**25, which PostgreSQL prints a digit longer; doubles whose text passes 22
    # characters, which MySQL cut; two 32-bit floats around a midpoint that a double rounds onto; and
    # floats of any bits. Their documents and digests must be one.
    chosen = random.Random(20261002)  # noqa: S311 (test values)
    doubles = [52990648348713776.0 + 8 * i for i in range(60)] + [2.0**54, 1e22, 1e23, 5e-324]
    doubles += [-1.1102230246251565e-16, 1.9114234241396806e-147, -0.000012345678901234568]
    doubles += [-1.7976931348623157e308, -2.2250738585072014e-308, 0.1, 1 / 3, 123456.789]
    singles = [33554436.0 + 4 * i for i in range(60)] + [2.0**25, 46056592.0, 16777216.0, _bits(1)]
    singles += [_bits(0x15AE43FD), _bits(0x15AE43FE), _bits(0x7F7FFFFF), -_bits(0x00800000)]
    singles += [_single(value) for value in (0.1, 1 / 3, 123456.789, 1.1234567)]
    for _ in range(72):  # any bits but the exponent of a NaN or an infinity
        doubles.append(struct.unpack(">d", struct.pack(">Q", chosen.getrandbits(64) & ~(1 << 62)))[0])
        singles.append(_bits(chosen.getrandbits(32) & ~(1 << 30)))
    assert len(doubles) == len(singles) == 144
    values = [(i, repr(d), repr(f)) for i, (d, f) in enumerate(zip(doubles, singles, strict=True))]
    rows = ", ".join(f"({i}, {d}, {f})" for i, d, f in values)
    typed = ", ".join(f"({i}, {d}d, {f}f)" for i, d, f in values)  # Oracle's literals of the two types
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE floats (k integer PRIMARY KEY, d double precision, f real)")
        conn.execute(f"INSERT INTO floats VALUES {rows}")  # noqa: S608 (constants)
    mysql_server.execute(
        f"CREATE TABLE floats (k INT PRIMARY KEY, d DOUBLE, f FLOAT); INSERT INTO floats VALUES {rows}",  # noqa: S608
        corpus.get("mysql").database.name,  # type: ignore[union-attr]
    )
    table = "CREATE TABLE floats (k NUMBER(5) PRIMARY KEY, d BINARY_DOUBLE, f BINARY_FLOAT)"
    oracle_server.execute(
        f"{table}; INSERT INTO floats VALUES {typed}",  # noqa: S608
        corpus.get("oracle").database.user,  # type: ignore[union-attr]
    )
    found = await _read(corpus, "floats")
    assert found["postgresql"] == found["mysql"] == found["oracle"] and len(found["mysql"]) == 144
    for i, (d, f) in enumerate(zip(doubles, singles, strict=True)):  # and each is the float that was stored
        document = found["postgresql"][str(i)][0]
        assert (float(document["d"]), _reads(str(document["f"]))) == (d, f), i
