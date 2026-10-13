"""What a table pre-check reads of a side (docs/mvp/PRECHECKS.md §4, §5) without a database: each engine's
statements at each level, pinned by a golden file each; that they aggregate over the filtered rows, those
without a key among them, while the duplicate keys are counted over the population; that whatever a name
or a filter literal holds a statement holds only grammar (MySQL), or no value at all (Oracle); the
statements a read is sent in, a few columns each; that two engines' minima and maxima of a moment are read
in canonical rendering 1; and the closed code of each failure, after which what was read stays.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from deltasleuth.connectors.postgres import queries
from deltasleuth.connectors.postgres.engine import PostgreSQL
from deltasleuth.connectors.tables import (
    ENGINES,
    Plan,
    Relation,
    SnapshotLost,
    StatementTimeout,
    TargetAuthRefused,
    TargetDenied,
    TargetRefused,
    TargetTempSpace,
    TargetUnreachable,
    compose,
    figures,
    mysql,
    oracle,
)
from deltasleuth.connectors.tables.engine import Column
from deltasleuth.core import precheck

from .engines import FACTS, Recorder

ENGINES_DIR = Path(__file__).parents[2] / "golden" / "engines"
GOLDEN = json.loads((ENGINES_DIR / "postgres_figures.json").read_text())
PLAIN, MAPPED = GOLDEN["plain"], GOLDEN["mapped"]  # one key; a composite key, two key maps, a filter
CLASSES = {
    "users": {"userid": "integer", "email": "text", "full_name": "char", "legacy_flag": "boolean"},
    "Accounts": {
        "branch": "integer", "acct": "text", "Amount": "decimal", "opened": "date", "status": "text",
        "closed": "boolean", "raw": "bytes", "seen": "instant", "rate": "float", "doc": "json",
        "fee": "decimal",
    },
}  # fmt: skip
LEVELS = ("counts", "columns", "distinct")
hostile = st.text(
    st.characters(exclude_characters="\x00", exclude_categories=["Cs"]), min_size=1, max_size=30
)


def relation(side: Plan) -> Relation:
    """The side's relation: each column of ``CLASSES`` under a declared type named as its class."""
    columns = tuple(
        Column(name, cls, cls, True, cls == "text") for name, cls in CLASSES[side["name"]].items()
    )
    return Relation(side["schema"], side["name"], columns, FACTS)


def _sent(side: Plan) -> dict[str, list[Any]]:
    """What PostgreSQL sends for ``side`` paired with itself: the statement of level counts and of level
    columns, the statements of level distinct's distinct counts, and that of its duplicate keys; each
    one's text and parameters."""
    classed = figures.classed(side, relation(side))
    plan, relations = (
        {"a": side, "b": side, "only_a": [], "only_b": []},
        {"a": relation(side), "b": relation(side)},
    )

    def sent(wanted: Any, size: int) -> list[list[Any]]:
        composed = [
            queries.figures_sql(classed, figures._columns(classed, b)) for b in figures._batches(wanted, size)
        ]
        return [[statement.as_string(None), params] for statement, params in composed]

    counted = [w for w in precheck.wanted("distinct", plan, relations)["a"] if w[0] == "distinct"]
    statement, params = queries.duplicates_sql(classed)
    return {
        "counts": sent(precheck.wanted("counts", plan, relations)["a"], figures.COLUMNS)[0],
        "columns": sent(precheck.wanted("columns", plan, relations)["a"], figures.COLUMNS)[0],
        "distinct": sent(counted, figures.DISTINCT),
        "duplicates": [statement.as_string(None), params],
    }


def test_postgresql_statements_are_the_golden_ones() -> None:
    assert _sent(PLAIN) == GOLDEN["sent"]["plain"]
    assert _sent(MAPPED) == GOLDEN["sent"]["mapped"]


def test_a_statement_s_parts() -> None:
    plain, mapped = GOLDEN["sent"]["plain"], GOLDEN["sent"]["mapped"]
    key = '(t."userid"::text COLLATE "C")'
    counts = f"(count(*))::text, (count(*) FILTER (WHERE {key} IS NULL))::text"
    assert plain["counts"] == [f'SELECT {counts} FROM "public"."users" AS t', {}]  # noqa: S608
    # Figures count the rows the filter selects, those without a key among them: no key is a condition.
    assert mapped["columns"][0].endswith(
        ' FROM "crm"."Accounts" AS t WHERE (t."status" IN ( %(w0)s , %(w1)s ) AND t."Amount" > %(w2)s'
        ' AND NOT t."closed")'
    )
    assert (
        "IS NOT NULL" not in mapped["columns"][0] and " WHERE " not in plain["columns"][0].split(" FROM ")[1]
    )
    # Duplicate keys do not: the population's rows, grouped by their canonical key texts in "C".
    grouped = f"WHERE {key} IS NOT NULL GROUP BY {key} HAVING count(*) > 1"
    counted = f'(SELECT count(*) AS n FROM "public"."users" AS t {grouped}) AS d'  # noqa: S608
    assert plain["duplicates"][0] == f"SELECT count(*), coalesce(sum(d.n), 0) FROM {counted}"  # noqa: S608
    assert (
        mapped["duplicates"][0].count(" IS NOT NULL") == 2
        and " GROUP BY CASE WHEN " in mapped["duplicates"][0]
    )
    assert mapped["duplicates"][1] == mapped["columns"][1]  # the same key-map constants and filter literals
    # A text is counted and measured in "C", a character(n) without its padding; a sum is a numeric's.
    text = '(t."email"::text COLLATE "C")'
    for form in (
        f"(count(*) FILTER (WHERE {text} = ''))::text", f"(min(char_length({text})))::text",
        '(count(*) FILTER (WHERE t."legacy_flag"))::text',
    ):  # fmt: skip
        assert form in plain["columns"][0]
    assert [len(found) for found in (plain["distinct"], mapped["distinct"])] == [1, 2]  # four columns each
    distinct = ", ".join(
        f"(count(DISTINCT {c}))::text" for c in (text, '(t."full_name"::text COLLATE "C")', 't."legacy_flag"')
    )
    assert plain["distinct"][0][0] == f'SELECT {distinct} FROM "public"."users" AS t'  # noqa: S608
    for form in (
        '(sum(t."fee"::numeric))::text', '(min(t."fee"))::text', '(max(octet_length(t."raw")))::text',
        """(to_jsonb(max(t."opened")) #>> '{}')::text""", """(to_jsonb(min(t."seen")) #>> '{}')::text""",
    ):  # fmt: skip
        assert form in mapped["columns"][0]
    assert mapped["distinct"][1][0].startswith(
        'SELECT (count(DISTINCT t."fee"))::text FROM "crm"."Accounts" AS t'
    )
    # A float, a JSON and a typeMap column give their NULLs alone; a filter column that is not compared, none.
    for column in ("rate", "doc", "Amount"):
        assert mapped["columns"][0].count(f't."{column}"') == 1 + (
            column == "Amount"
        )  # the filter reads it too
        assert f't."{column}" IS NULL' in mapped["columns"][0]
        assert all(f't."{column}"' not in statement.split(" FROM ")[0] for statement, _ in mapped["distinct"])
    assert 't."closed"' not in mapped["columns"][0].split(" FROM ")[0]
    statements = [plain[n] for n in ("counts", "columns", "duplicates")] + plain["distinct"]
    statements += [mapped[n] for n in ("counts", "columns", "duplicates")] + mapped["distinct"]
    for statement, _ in statements:
        assert statement.startswith("SELECT (count(") or statement.startswith("SELECT count(*), coalesce(")
        assert re.fullmatch(r"SELECT [^;]* FROM [^;]*", statement)  # one statement, every item an aggregate


@given(name=hostile, literal=hostile)
def test_statements_hold_only_grammar(name: str, literal: str) -> None:
    """Whatever a column's name or a filter's literal holds, the statement is the one a plain name and
    literal give: a name is a quoted identifier, a literal a parameter."""
    # The relation's own, and the collation a text is read in (COLLATE "C"): could not tell.
    assume('"' + name.replace('"', '""') + '"' not in '"s"."t" COLLATE "C"')

    def skeleton(column: str, value: str) -> list[str]:
        quoted, written = column.replace('"', '""'), value.replace("'", "''")
        where = f""""{quoted}" = '{written}' AND "{quoted}" <> '{written}'"""
        side = {
            "schema": "s", "name": "t", "keys": [column], "key_maps": [None], "columns": {"c": column},
            "where": where, "types": {}, "digest": [], "classes": {column: "text"},
        }  # fmt: skip
        wanted = [("rows", None), ("null_keys", None)]
        wanted += [
            (f, column) for f in ("nulls", "empty", "shortest", "longest", "sum", "min", "max", "distinct")
        ]
        texts = []
        for statement, params in (queries.figures_sql(side, wanted), queries.duplicates_sql(side)):
            assert list(params.values()) == [value, value]  # the literal is bound, twice
            identifier = '"' + quoted + '"'
            texts.append(statement.as_string(None).replace(identifier, '"x"'))
        return texts

    assert skeleton(name, literal) == skeleton("x", "plain")


class Engine:
    """An engine that answers every figure with ``1`` and records each statement's figures; ``failing``
    maps a call's number (from 1) to what it raises."""

    def __init__(self, pause: float = 0, **failing: BaseException) -> None:
        self.sent: list[Any] = []
        self.pause, self.failing = pause, {int(n[1:]): exc for n, exc in failing.items()}

    def _call(self, sent: Any) -> None:
        self.sent.append(sent)
        if len(self.sent) in self.failing:
            raise self.failing[len(self.sent)]

    async def figures(self, session: Any, side: Plan, wanted: list[Any]) -> list[str]:
        await asyncio.sleep(self.pause)
        self._call(wanted)
        return ["1"] * len(wanted)

    async def duplicates(self, session: Any, side: Plan) -> tuple[int, int]:
        self._call("duplicates")
        return (2, 5)


def _wide(columns: int) -> tuple[Plan, list[tuple[str, str | None]]]:
    """A side of ``columns`` compared columns c0, c1, ... (each read as C0, C1, ...) and what level
    ``distinct`` would read of them: two figures a column, and the distinct values of the first ten."""
    names = [f"c{n}" for n in range(columns)]
    side = {"keys": ["id"], "columns": {name: name.upper() for name in names}}
    wanted: list[tuple[str, str | None]] = [("rows", None), ("null_keys", None)]
    wanted += [(figure, name) for name in names for figure in ("nulls", "empty")]
    return side, wanted + [("distinct", name) for name in names[:10]]


async def _read(engine: Any, side: Plan, wanted: Any, **options: Any) -> dict[str, Any]:
    options = {"duplicates": True, "distinct_max_rows": 20_000_000, "timeout_s": 900} | options
    return await figures.read(engine, None, side, wanted, **options)  # type: ignore[arg-type]


async def test_chunks_of_150_and_4() -> None:
    engine = Engine()
    side, wanted = _wide(310)
    read = await _read(engine, side, wanted)
    columns = [
        sorted({c for _, c in sent if c}, key=lambda c: int(c[1:]))
        for sent in engine.sent
        if sent != "duplicates"
    ]
    assert [len(found) for found in columns] == [150, 150, 10, 4, 4, 2]
    assert columns[0][0] == "C0" and columns[1][0] == "C150" and columns[5] == ["C8", "C9"]  # as the side's
    assert engine.sent[0][:2] == [("rows", None), ("null_keys", None)]  # with the first statement alone
    assert all(None not in [c for _, c in sent] for sent in engine.sent[1:] if sent != "duplicates")
    assert engine.sent[3] == "duplicates" and {f for sent in engine.sent[4:] for f, _ in sent} == {"distinct"}
    assert (read["rows"], read["null_keys"], read["duplicate_keys"], read["rows_in_duplicates"]) == (
        1,
        1,
        2,
        5,
    )
    assert len(read["figures"]) == 310 and read["figures"]["c9"] == {
        "nulls": "1",
        "empty": "1",
        "distinct": "1",
    }
    assert read["figures"]["c10"] == {"nulls": "1", "empty": "1"} and (read["failed"], read["skipped"]) == (
        {},
        [],
    )
    # Level counts is one statement; a level that reads nothing sends none.
    engine = Engine()
    assert await _read(engine, side, wanted[:2], duplicates=False) == {
        "rows": 1, "null_keys": 1, "figures": {}, "failed": {}, "skipped": [],
    }  # fmt: skip
    assert engine.sent == [[("rows", None), ("null_keys", None)]]
    assert (await _read(engine, side, [], duplicates=False))["figures"] == {} and len(engine.sent) == 1


async def test_a_failed_read_ends_the_side_s_and_what_was_read_stays() -> None:
    side, wanted = _wide(160)
    engine = Engine(n2=StatementTimeout("MySQL 3024"))  # the second statement of figures
    read = await _read(engine, side, wanted)
    assert read["failed"] == {"figures": ("timeout", "900")} and len(engine.sent) == 2  # nothing after it
    assert (read["rows"], len(read["figures"])) == (1, 150) and "duplicate_keys" not in read
    engine = Engine(n3=TargetTempSpace("ORA-01652"))
    read = await _read(engine, side, wanted)
    assert read["failed"] == {"duplicates": ("temp_space", "ORA-01652")} and len(read["figures"]) == 160
    engine = Engine(n5=psycopg.errors.ConfigurationLimitExceeded())  # the second batch of distinct counts
    read = await _read(engine, side, wanted)
    assert read["failed"] == {"distinct": ("temp_space", "53400")} and read["duplicate_keys"] == 2
    assert read["figures"]["c3"]["distinct"] == "1" and "distinct" not in read["figures"]["c4"]
    # Over distinct_max_rows nothing that sorts or hashes is sent; 0 never sends one.
    for most in (0,):
        engine = Engine()
        read = await _read(engine, side, wanted, distinct_max_rows=most)
        assert (read["skipped"], len(engine.sent), "duplicate_keys" in read) == (["distinct_rows"], 2, False)
    engine = Engine()
    assert (await _read(engine, side, wanted, distinct_max_rows=1))["skipped"] == [] and len(engine.sent) == 6
    # A MySQL statement the server interrupted once its timeout had passed timed out (1317); before, it
    # was cancelled for another reason, and is a session's failure.
    late = await _read(Engine(0.02, n1=TargetUnreachable("MySQL 1317")), side, wanted, timeout_s=0.01)
    early = await _read(Engine(n1=TargetUnreachable("MySQL 1317")), side, wanted, timeout_s=900)
    assert (late["failed"], early["failed"]) == (
        {"figures": ("timeout", "0.01")}, {"figures": ("unreachable", "MySQL 1317")},
    )  # fmt: skip


@pytest.mark.parametrize(
    ("exc", "failed"),
    [
        (psycopg.errors.QueryCanceled(), ("timeout", "900")),  # 57014
        (StatementTimeout("DPY-4024"), ("timeout", "900")),
        (TargetAuthRefused("ORA-01017"), ("auth_refused", "ORA-01017")),
        (psycopg.errors.ConfigurationLimitExceeded(), ("temp_space", "53400")),
        (TargetTempSpace("MySQL 3"), ("temp_space", "MySQL 3")),
        (psycopg.OperationalError("the password of crm-db is hunter2"), ("unreachable", "OperationalError")),
        (TargetUnreachable("ORA-12541"), ("unreachable", "ORA-12541")),
        (SnapshotLost("ORA-01555"), ("snapshot_lost", "ORA-01555")),
        (TargetDenied("ORA-00942"), ("denied", "ORA-00942")),
        (TargetRefused("MySQL 1064"), ("error", "TargetRefused: MySQL 1064")),
        (psycopg.errors.InsufficientPrivilege("table pay"), ("error", "InsufficientPrivilege")),
        (TypeError("int() argument must be a string, not 'Secret'"), ("error", "TypeError")),
    ],
)  # fmt: skip
def test_a_failure_is_a_closed_code_and_never_a_driver_s_text(
    exc: BaseException, failed: tuple[str, str]
) -> None:
    assert figures.failure(exc, 900) == failed


def test_a_side_is_read_by_its_columns_classes() -> None:
    found = relation(PLAIN)
    classed = figures.classed(PLAIN, found)  # a pair of PostgreSQL tables stores no class
    assert classed["classes"] == CLASSES["users"] and classed["declared"] == CLASSES["users"]
    assert "classes" not in PLAIN and {name: classed[name] for name in PLAIN} == PLAIN
    stored = PLAIN | {"classes": {"email": "char"}, "declared": {"email": "CHAR(3)"}}  # a pair across engines
    assert figures.classed(stored, found) is stored
    assert (figures.COLUMNS, figures.DISTINCT) == (150, 4)
    assert (PostgreSQL.sum_digits, PostgreSQL.empty_null) == (None, False)
    assert [(ENGINES[name].sum_digits, ENGINES[name].empty_null) for name in ("mysql", "oracle")] == [
        (65, False),
        (38, True),
    ]


async def test_postgresql_sends_no_parameter_it_does_not_bind() -> None:
    """A plan made of a catalogue alone has neither filter nor key map: its statements go without
    parameters, so a column named with a % is read (the driver reads no placeholder)."""
    side = {
        "schema": "s", "name": "t", "keys": ["id"], "key_maps": [None], "columns": {"rate%": "rate%"},
        "where": None, "types": {}, "digest": [], "classes": {"rate%": "integer"},
    }  # fmt: skip

    class Session(Recorder):
        async def execute(self, statement: Any, params: Any = None) -> Any:
            self.params = params
            return await super().execute(statement, params)

        async def fetchone(self) -> tuple[Any, ...]:
            return (3, 7)

    session = Session()
    assert await PostgreSQL().figures(session, side, [("rows", None), ("sum", "rate%")]) == [3, 7]  # type: ignore[arg-type]
    assert session.params is None and 't."rate%"' in session.sent[0][1]
    assert await PostgreSQL().duplicates(session, side) == (3, 7) and session.params is None  # type: ignore[arg-type]
    await PostgreSQL().figures(session, side | {"where": '"id" > 1'}, [("rows", None)])  # type: ignore[arg-type]
    assert session.params == {"w0": "1"}


# --- MySQL and Oracle (docs/mvp/PRECHECKS.md §4: their statement forms) ------------------------------

PAIRS = {name: json.loads((ENGINES_DIR / f"{name}_pair.json").read_text()) for name in ("mysql", "oracle")}
DIALECTS = {"mysql": mysql.PRIMITIVES, "oracle": oracle.PRIMITIVES}
FIGURED = {name: json.loads((ENGINES_DIR / f"{name}_figures.json").read_text()) for name in DIALECTS}


def _related(dialect: Any, side: Plan) -> Relation:
    """The relation a plan side of a pair across engines was made of: its columns of ``classes``."""
    columns = tuple(
        Column(
            name,
            side["declared"][name],
            cls,
            True,
            cls in ("text", "char"),
            lob=dialect.lob(side["declared"][name]),
        )
        for name, cls in side["classes"].items()
    )
    return Relation(side["schema"], side["name"], columns, FACTS)


def _composed(engine: str, side: Plan) -> dict[str, Any]:
    """What ``engine`` sends for ``side`` paired with itself, as ``_sent`` for PostgreSQL."""
    dialect, relation_ = DIALECTS[engine], _related(DIALECTS[engine], side)
    plan, relations = {"a": side, "b": side, "only_a": [], "only_b": []}, {"a": relation_, "b": relation_}

    def sent(wanted: Any, size: int) -> list[list[Any]]:
        batches = figures._batches(wanted, size)
        return [list(compose.figures(dialect, side, figures._columns(side, batch))) for batch in batches]

    counted = [w for w in precheck.wanted("distinct", plan, relations)["a"] if w[0] == "distinct"]
    return {
        "counts": sent(precheck.wanted("counts", plan, relations)["a"], figures.COLUMNS)[0],
        "columns": sent(precheck.wanted("columns", plan, relations)["a"], figures.COLUMNS)[0],
        "distinct": sent(counted, figures.DISTINCT),
        "duplicates": list(compose.duplicates(dialect, side)),
    }


@pytest.mark.parametrize("engine", ["mysql", "oracle"])
def test_mysql_and_oracle_statements_are_the_golden_ones(engine: str) -> None:
    for name in ("plain", "mapped"):
        assert _composed(engine, PAIRS[engine][name]) == FIGURED[engine]["sent"][name], name


def test_mysql_statement_parts() -> None:
    plain, mapped = FIGURED["mysql"]["sent"]["plain"], FIGURED["mysql"]["sent"]["mapped"]
    key = "CAST(t.`id` AS CHAR)"
    counts = f"SELECT COUNT(*), COUNT(CASE WHEN {key} IS NULL THEN 1 END) FROM `ledger`.`customers` AS t"  # noqa: S608
    assert plain["counts"] == [counts, {}]  # nothing is bound: a MySQL driver binds nothing on the server
    # A text's empties are its zero lengths (under a PAD SPACE collation c = '' holds for spaces too), its
    # lengths code points, and its distinct values its bytes (its own collation counts A and a once).
    text = "CONVERT(t.`email` USING utf8mb4)"
    for form in (
        "COUNT(CASE WHEN CHAR_LENGTH(t.`email`) = 0 THEN 1 END)", "MIN(CHAR_LENGTH(t.`email`))",
        "CAST(SUM(t.`amount`) AS CHAR)", "CAST(MIN(t.`amount`) AS CHAR)",
    ):  # fmt: skip
        assert form in plain["columns"][0], form
    assert plain["distinct"][0][0].startswith(f"SELECT COUNT(DISTINCT CAST({text} AS BINARY))")
    # Bytes are measured in octets, a moment's bounds rendered as a row of the pair's rendering.
    for form in (
        "MIN(LENGTH(t.`raw`))", "DATE_FORMAT(MAX(t.`seen`), '%Y-%m-%dT%H:%i:%s.%f')",
        "CAST(MIN(t.`at`) AS CHAR)", "CAST(MAX(t.`day`) AS CHAR)",
    ):  # fmt: skip
        assert form in mapped["columns"][0], form
    # Figures count the rows the filter selects; duplicate keys are grouped by the key texts' bytes over the
    # population: whatever rows the filter selects, with a whole key.
    whole = "CONVERT(t.`branch` USING utf8mb4) IS NOT NULL"
    assert (
        " WHERE " in mapped["columns"][0]
        and whole not in mapped["columns"][0]
        and whole in mapped["duplicates"][0]
    )
    grouped = f"WHERE {key} IS NOT NULL GROUP BY CAST({key} AS BINARY) HAVING COUNT(*) > 1) d"
    counted = f"SELECT COUNT(*) AS n FROM `ledger`.`customers` AS t {grouped}"  # noqa: S608
    assert plain["duplicates"] == [f"SELECT COUNT(*), COALESCE(SUM(d.n), 0) FROM ({counted}", {}]  # noqa: S608
    # A float, a JSON, a large-object and a typeMap column give their NULLs alone; no sum is of a text.
    for column in ("rate", "doc", "note", "opened"):
        assert mapped["columns"][0].split(" FROM ")[0].count(f"t.`{column}`") == 1, column
    assert all("SUM(t.`st`)" not in statement for statement, _ in [mapped["columns"], *mapped["distinct"]])


def test_oracle_statement_parts() -> None:
    plain, mapped = FIGURED["oracle"]["sent"]["plain"], FIGURED["oracle"]["sent"]["mapped"]
    assert plain["columns"][0].endswith(' FROM "APP"."CUSTOMERS" t')  # AS is refused there
    # A NULL is counted by a CASE, which reads a CLOB too (COUNT of one raises ORA-22849); a text's length is
    # LENGTH4's, its code points; a CHAR is trimmed of its padding, a blank one being 0 long.
    for form in (
        'COUNT(CASE WHEN t."EMAIL" IS NULL THEN 1 END)', 'MAX(LENGTH4(t."EMAIL"))',
        """TO_CHAR(SUM(t."AMOUNT"), 'TM9')""", """TO_CHAR(MAX(t."AMOUNT"), 'TM9')""",
    ):  # fmt: skip
        assert form in plain["columns"][0], form
    char = """NVL2(t."ST", NVL(LENGTH4(RTRIM(t."ST", ' ')), 0), NULL)"""
    for form in (
        f"COUNT(CASE WHEN {char} = 0 THEN 1 END)", f"MIN({char})",
        'COUNT(CASE WHEN t."NOTE" IS NULL THEN 1 END)', 'MAX(LENGTH(RAWTOHEX(t."RAW")) / 2)',
        'COUNT(CASE WHEN t."FLAG" THEN 1 END)',
        """TO_CHAR(MIN(t."AT"), 'SYYYY-MM-DD"T"HH24:MI:SS.FF9')""",
        """TO_CHAR(SYS_EXTRACT_UTC(MAX(t."SEEN")), 'SYYYY-MM-DD"T"HH24:MI:SS.FF9')""",
    ):  # fmt: skip
        assert form in mapped["columns"][0], form
    # Binary under the session's NLS_COMP; a CHAR as stored, its values padded alike.
    assert 'COUNT(DISTINCT t."ST")' in mapped["distinct"][0][0]
    # Every value is bound: the filter's literals and the key map's prefix, in figures and duplicate keys.
    assert mapped["columns"][1] and set(mapped["columns"][1].values()) <= set(
        mapped["duplicates"][1].values()
    )
    assert mapped["duplicates"][0].endswith(" HAVING COUNT(*) > 1) d")
    assert re.search(r"'[^']*[0-9]'", mapped["columns"][0].split(" WHERE ")[1]) is None  # no literal


@given(name=hostile, literal=hostile)
def test_mysql_statements_hold_only_grammar(name: str, literal: str) -> None:
    """Whatever a column's name or a filter's literal holds, a MySQL statement is the one a plain name and
    literal give: a name is a quoted identifier, a literal the hexadecimal digits of its UTF-8."""
    assume(mysql.PRIMITIVES.ident(name) not in "`s`.`t`")  # the relation's own: the text could not tell

    def skeleton(column: str, value: str) -> list[str]:
        quoted, written = column.replace('"', '""'), value.replace("'", "''")
        side = {
            "schema": "s", "name": "t", "keys": [column], "key_maps": [None], "columns": {"c": column},
            "where": f""""{quoted}" = '{written}'""", "types": {}, "digest": [],
            "classes": {column: "text"}, "declared": {column: "varchar(20)"},
        }  # fmt: skip
        wanted = [("rows", None), ("null_keys", None)]
        wanted += [(f, column) for f in ("nulls", "empty", "shortest", "longest", "distinct")]
        texts = []
        for statement, binds in (
            compose.figures(mysql.PRIMITIVES, side, wanted),
            compose.duplicates(mysql.PRIMITIVES, side),
        ):
            assert binds == {}
            texts.append(
                statement.replace(mysql.PRIMITIVES.ident(column), "`x`").replace(
                    f"X'{value.encode().hex()}'", "X''"
                )
            )
        return texts

    assert skeleton(name, literal) == skeleton("x", "")


@given(literal=hostile)
def test_oracle_statements_hold_no_value(literal: str) -> None:
    """Whatever a filter's literal holds, an Oracle statement is the one any literal gives: it is bound."""

    def sent(value: str) -> list[tuple[str, compose.Binds]]:
        side = {
            "schema": "S", "name": "T", "keys": ["K"], "key_maps": [None], "columns": {"c": "C"},
            "where": f""""C" = '{value.replace("'", "''")}'""", "types": {}, "digest": [],
            "classes": {"K": "integer", "C": "text"}, "declared": {"K": "NUMBER(10)", "C": "VARCHAR2(20)"},
        }  # fmt: skip
        wanted = [("rows", None), ("null_keys", None), ("nulls", "C"), ("longest", "C"), ("distinct", "C")]
        return [compose.figures(oracle.PRIMITIVES, side, wanted), compose.duplicates(oracle.PRIMITIVES, side)]

    for (statement, binds), (plain, bound) in zip(sent(literal), sent("x"), strict=True):
        assert statement == plain and binds == dict.fromkeys(bound, literal) and len(bound) == 1


def test_two_engines_bounds_of_a_moment_read_as_postgresql_writes_them() -> None:
    """A minimum and a maximum of a date or a time is compared across engines in canonical rendering 1:
    MySQL's six digits and Oracle's signed year read as PostgreSQL's ``to_jsonb`` writes them."""
    side = {"classes": {"a": "timestamp", "b": "instant", "c": "time", "d": "date", "e": "decimal"}}
    wanted = [
        ("rows", None),
        ("min", "a"),
        ("max", "b"),
        ("min", "c"),
        ("max", "d"),
        ("max", "e"),
        ("min", "a"),
    ]
    rendered = [
        "3", "-0044-03-15T00:00:00", "2024-05-01T10:00:00.500000", "03:04:05.120", "2024-05-01", "-.5", None,
    ]  # fmt: skip
    assert compose.figured(side, wanted)([rendered]) == [
        "3", "0044-03-15T00:00:00 BC", "2024-05-01T10:00:00.5+00:00", "03:04:05.12", "2024-05-01", "-.5",
        None,
    ]  # fmt: skip
    assert compose.figured(side, [("rows", None)])([(Decimal(3),)]) == ["3"]  # Oracle's, read as a Decimal
