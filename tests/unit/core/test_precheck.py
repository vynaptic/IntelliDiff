"""Table pre-checks, pure (docs/mvp/PRECHECKS.md §4, §6, §10): what a level reads, the schema codes and
their groups, each class's figures, when a sum is exact and when it is shown, the verdict ladder, and
every text of a pair."""

from __future__ import annotations

import ast
import inspect
import re
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deltasleuth.connectors.tables import ENGINES, render
from deltasleuth.connectors.tables.engine import Column, Relation
from deltasleuth.connectors.tables.plan import _A, _BRIDGES, _CASTS
from deltasleuth.core import jsonio, precheck
from deltasleuth.core.precheck import Pair
from deltasleuth.core.types import SecurityPolicy, TokenizeRule
from deltasleuth.runner import census

AT = "2026-10-07T10:02:11.000000Z"
# A stored spec that lists no columns (the plan takes the common ones) and clears every one of them.
SPEC = Pair(12, "customer-profile 1.4.0", False, SecurityPolicy(unlisted="clear"), True, False, ("a", "b"))
READ = (
    "side a: postgresql 16.4, public.customers, in 1 snapshot at 2026-10-07 10:02:11 UTC; side b: postgresql"
    " 16.4, public.customers, in 1 snapshot, 0 s later: not read at one moment"
)
BEST = "no difference observed in the checks made (level {}): not parity; run a comparison to compare rows"
N1 = re.compile(r"\b(match|matches|matched|identical|passed|verified|validated|reconciled)\b|[✓✔]|%", re.I)


def col(name: str, declared: str, cls: str | None = None, **facts: Any) -> Column:
    """A readable column; its class is its declared type's unless given, and it may hold NULL."""
    cls = declared if cls is None else cls
    return Column(name, declared, cls or None, True, cls in ("text", "char"), **{"nullable": True} | facts)


def relation(*columns: Column, name: str = "public.customers", **facts: Any) -> Relation:
    """A base table with the primary key (id), on a PostgreSQL 16.4 read in a snapshot."""
    schema, _, table = name.rpartition(".")
    held = {"kind": "table", "key": ["id"], "server": "16.4", "consistency": "snapshot"} | facts
    return Relation(schema, table, (col("id", "integer", nullable=False), *columns), held)


def side(columns: dict[str, str], **members: Any) -> dict[str, Any]:
    return {
        "schema": "public", "name": "customers", "keys": ["id"], "key_maps": [None], "columns": columns,
        "where": None, "types": {}, "digest": [],
    } | members  # fmt: skip


def paired(a: Relation, b: Relation, /, **members: Any) -> dict[str, Any]:
    """The plan of two relations over their common column names, lower-cased; ``members`` change a side
    (``a``, ``b``: its plan's members) or the pair (``only_a``, ``only_b``)."""
    names = [{c.name.lower(): c.name for c in found.columns if c.name.lower() != "id"} for found in (a, b)]
    common = [name for name in names[0] if name in names[1]]
    sides = [side({name: found[name] for name in common}) for found in names]
    only = {
        "only_a": [n for n in names[0] if n not in common],
        "only_b": [n for n in names[1] if n not in common],
    }
    return {"a": sides[0] | members.pop("a", {}), "b": sides[1] | members.pop("b", {})} | only | members


def read(rows: int | None = 10, at: str = AT, engine: str = "postgresql", **members: Any) -> dict[str, Any]:
    """What was read of a side: ``rows`` rows, none of them without a key, on an engine whose sums are
    exact; ``members`` holds its figures by canonical name and anything else it says."""
    said = {name: members.pop(name) for name in list(members) if not isinstance(members[name], dict)}
    said |= {name: members.pop(name) for name in ("failed",) if name in members}
    found = {
        "engine": engine, "at": at, "sum_digits": None, "empty_null": False, "rows": rows, "null_keys": 0,
        "figures": members, "failed": {}, "skipped": [],
    }  # fmt: skip
    return found | said


def judged(
    a: Relation, b: Relation, sides: tuple[dict[str, Any], dict[str, Any]], /, level: str = "columns",
    pair: Pair = SPEC, asked: str | None = None, **members: Any,
) -> precheck.Judged:  # fmt: skip
    found = {"a": a, "b": b}
    return precheck.compare(
        asked or level, level, pair, paired(a, b, **members), found, dict(zip("ab", sides, strict=True)),
        distinct_max_rows=20_000_000,
    )  # fmt: skip


def lines(found: precheck.Judged, column: str) -> list[str]:
    return precheck.column_lines(found.result["columns"][column])


def test_the_words_other_modules_hold() -> None:
    assert precheck.NOT.startswith("A table pre-check counts and adds up; it does not compare rows.")
    assert precheck.NOT.endswith("no evidence pack contains it: run a comparison to compare rows.")
    assert len(precheck.VERDICTS) == 10 and [precheck.rank(v) for v in precheck.VERDICTS] == list(
        range(1, 11)
    )
    assert precheck.LEVELS == ("schema", "counts", "columns", "distinct")
    assert (precheck.COLUMNS, precheck.DISTINCT, precheck.REFUSALS, precheck.NAME) == (200, 40, 10, 128)
    # The words of the plan's cross-engine check and of the census, which core cannot import.
    assert (precheck._BRIDGES, precheck._A, precheck._CASTS) == (_BRIDGES, _A, _CASTS)
    assert (precheck._FAMILIES, precheck._MAPPED) == (render._FAMILIES, render._MAPPED)
    unpinned = {name: f"{one}, {why}" for name, (one, _, why) in census._UNPINNED.items()}
    assert precheck._UNPINNED == unpinned


def test_module_names_no_engine() -> None:
    """One more engine is one more ``ENGINES`` entry: the pure module imports no connector and holds no
    engine's name, so nothing in it can depend on which engine a side runs on."""
    tree = ast.parse(inspect.getsource(precheck))
    imported = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imported += [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    ]
    assert not [name for name in imported if name and ("connectors" in name or "runner" in name)]
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not constants & set(ENGINES)


# -- the two catalogues (§4, §7) ---------------------------------------------------------------------------


def test_shape_ignores_column_order() -> None:
    email, amount = col("email", "text"), col("amount", "numeric(12,2)", "decimal", size=("digits", 12, 2))
    one, other = relation(email, amount), relation(amount, email)
    assert precheck.shape(one) == {
        "kind": "table",
        "key": ["id"],
        "columns": {
            "id": ["integer", "integer", False], "email": ["text", "text", True],
            "amount": ["numeric(12,2)", "decimal", True],
        },
    }  # fmt: skip
    assert precheck.sha256(precheck.shape(one)) == precheck.sha256(precheck.shape(other))
    assert re.fullmatch("[0-9a-f]{64}", precheck.sha256(precheck.shape(one)))
    changed = [
        relation(email, col("amount", "numeric(12,4)", "decimal")),  # a declared type
        relation(email, col("amount", "numeric(12,2)", "text")),  # a class
        relation(email, col("amount", "numeric(12,2)", "decimal", nullable=False)),
        relation(email, amount, key=["id", "email"]),
        relation(email, amount, kind="view", key=None),
        relation(email),
    ]
    hashes = {precheck.sha256(precheck.shape(found)) for found in [one, *changed]}
    assert len(hashes) == 7
    served = relation(email, amount, server="17.0", consistency="unverified")  # not the server's version
    assert precheck.sha256(precheck.shape(served)) == precheck.sha256(precheck.shape(one))


def codes(found: list[dict[str, Any]]) -> list[tuple[str, str | None, str]]:
    return [(entry["code"], entry["column"], entry["group"]) for entry in found]


def test_schema_diff_per_code_and_group() -> None:
    a = relation(
        col("amount", "NUMBER(10,2)", "decimal", size=("digits", 10, 2)),
        col("code", "CHAR(3)", "char", size=("characters", 3), nullable=False),
        col("opened", "DATE", "timestamp"),
        col("flag", "NUMBER(1)", "integer"),
        col("geom", "SDO_GEOMETRY", ""),
        col("span", "INTERVAL DAY(2) TO SECOND(6)", "other"),
        col("email", "VARCHAR2(120 CHAR)", "text", size=("characters", 120)),
        col("raw", "RAW(16)", "bytes", size=("bytes", 16)),
        col("legacy_flag", "CHAR(1)", "char"),
        name="APP.CUSTOMERS",
    )
    b = relation(
        col("amount", "numeric(12,4)", "decimal", size=("digits", 12, 4)),
        col("code", "text"),
        col("opened", "date"),
        col("flag", "boolean"),
        col("geom", "text"),
        col("span", "interval", "other"),
        col("email", "character varying(120)", "text", size=("characters", 120)),
        col("raw", "bytea", "bytes"),
        col("loyalty_tier", "text"),
    )
    across = {"a": {"engine": "oracle"}, "b": {"engine": "postgresql"}}
    found = precheck.schema_diff(paired(a, b, **across), {"a": a, "b": b}, listed=False)
    assert codes(found) == [
        ("size_differs", "amount", "schema"),  # digits against digits
        ("class_differs", "code", "schema"),  # char against text: one family
        ("nullable_differs", "code", "schema"),
        ("family_differs", "opened", "not_compared"),
        ("family_differs", "flag", "not_compared"),
        ("uncomparable", "geom", "not_compared"),  # a type no plan reads (P7)
        ("uncomparable", "span", "not_compared"),  # other never crosses engines (P8)
        ("only_a", "legacy_flag", "not_compared"),  # the plan takes the common columns
        ("only_b", "loyalty_tier", "not_compared"),
    ]  # the same declared size, a size in another unit and one side's alone are no difference
    assert found[0]["a"] == {"declared": "NUMBER(10,2)", "cls": "decimal", "nullable": True}
    assert found[0]["b"] == {"declared": "numeric(12,4)", "cls": "decimal", "nullable": True}
    assert (found[-2]["a"]["declared"], found[-2]["b"], found[-1]["a"]) == ("CHAR(1)", None, None)
    # In one engine two columns of class other are compared; a typeMap gives its side another family.
    same = paired(a, b, a={"types": {"opened": {"as": "date"}, "flag": {"as": "boolean"}}})
    assert codes(precheck.schema_diff(same, {"a": a, "b": b}, listed=False))[3:6] == [
        ("class_differs", "opened", "schema"),
        ("class_differs", "flag", "schema"),
        ("uncomparable", "geom", "not_compared"),
    ]
    # A spec that lists its columns leaves the others out on purpose: one on a side only is the schema's.
    listed = paired(a, b)
    for name in ("a", "b"):
        listed[name]["columns"] = {"email": "email"}
    assert codes(
        precheck.schema_diff(listed | {"only_a": [], "only_b": []}, {"a": a, "b": b}, listed=True)
    ) == [
        ("only_a", "legacy_flag", "schema"),
        ("only_b", "loyalty_tier", "schema"),
    ]
    # Whether a column may hold NULL is judged between two base tables alone: a view's does not say.
    view = relation(*(col(c.name, c.declared, c.cls) for c in b.columns[1:]), kind="view", key=None)
    assert "nullable_differs" not in [
        c for c, _, _ in codes(precheck.schema_diff(paired(a, view), {"a": a, "b": view}, False))
    ]


def test_the_key_is_judged_between_two_base_tables_with_a_primary_key() -> None:
    email = col("email", "text")
    a, wider = relation(email), relation(email, col("region", "text"), key=["id", "region"])

    def key(found: Relation, other: Relation, **members: Any) -> list[str]:
        told = judged(found, other, (read(), read()), level="schema", **members).result["lines"]
        return [text for text in told if text.startswith("key")]

    assert key(a, relation(email)) == []
    assert key(a, wider, only_b=[]) == ["key: keyColumns (id) is not side b's primary key (id, region)"]
    renamed = relation(email, name="public.CUSTOMERS", key=["ID"])  # the catalogue's spelling: another column
    assert key(renamed, a, a={"keys": ["id"]}) == ["key: keyColumns (id) is not side a's primary key (ID)"]
    # Two relations paired by name plan a key only where both show one over the same columns.
    unkeyed = {"a": {"keys": []}, "b": {"keys": []}, "only_b": []}
    assert key(a, wider, **unkeyed) == ["key: the primary key is (id) on side a and (id, region) on side b"]
    assert judged(a, wider, (read(), read()), level="schema", **unkeyed).verdict == "schema_differs"
    assert key(a, relation(email), **unkeyed) == []


def test_key_not_checked_over_a_view_keeps_the_best_verdict() -> None:
    email = col("email", "text")
    a = relation(email)
    for other, said in (
        (relation(email, kind="view", key=None), "side b is a view (no primary key)"),
        (
            relation(email, kind="materialized view", key=None),
            "side b is a materialized view (no primary key)",
        ),
        (relation(email, key=None), "side b has no primary key"),
    ):
        found = judged(a, other, (read(), read()), level="counts")
        assert found.verdict == "no_difference_observed" and found.rank == 10
        assert found.result["lines"] == [BEST.format("counts"), f"key: not checked: {said}", READ]
        assert found.result["keys"]["b"]["primary"] is None and found.result["schema"] == []
    both = judged(relation(email, key=None), a, (read(), read()), level="schema")
    assert both.result["lines"][1] == "key: not checked: side a has no primary key"


def keyed(key: Column, *columns: Column, name: str = "public.customers", **facts: Any) -> Relation:
    """A base table whose primary key is ``key``, on a PostgreSQL 16.4 read in a snapshot."""
    schema, _, table = name.rpartition(".")
    held = {"kind": "table", "key": [key.name], "server": "16.4", "consistency": "snapshot"} | facts
    return Relation(schema, table, (key, *columns), held)


def test_key_parts_are_judged_as_columns_and_one_whose_texts_never_meet_is_not_compared() -> None:
    """Each key part's two sides are judged by class, size and NULLs; two whose key texts are never equal
    (two scales of a number, two families neither of which is text) would find no key of one side on the
    other, so the pair is not ``no difference observed`` though every figure agrees (PostgreSQL's
    ``numeric(10,2)`` key reads 1.00 where an ``integer`` reads 1)."""
    v = col("v", "text")
    integer = col("id", "integer", nullable=False)
    cents = col("id", "numeric(10,2)", "decimal", size=("digits", 10, 2), nullable=False)
    figures = {"nulls": "0", "empty": "0", "shortest": "1", "longest": "1", "distinct": "2"}
    same = (read(2, v=figures), read(2, v=figures))
    never = (
        "key column id is numeric(10,2) on side a and integer on side b: a number key's text holds its scale,"
        " so no key of one side is found on the other: compare a view that casts it"
    )
    found = judged(keyed(cents, v), keyed(integer, v), same, level="distinct")
    assert (found.verdict, found.rank, found.differing) == ("columns_not_compared", 6, 0)
    assert found.result["lines"] == [never, READ]
    assert codes(found.result["schema"]) == [("key_text_differs", "id", "not_compared")]
    assert found.result["schema"][0]["a"] == {
        "declared": "numeric(10,2)",
        "cls": "decimal",
        "nullable": False,
    }

    def schema(a: Column, b: Column, /, **members: Any) -> list[tuple[str, str | None, str]]:
        return codes(
            precheck.schema_diff(paired(keyed(a), keyed(b), **members), {"a": keyed(a), "b": keyed(b)}, False)
        )

    whole = col("id", "numeric(10,0)", "decimal", size=("digits", 10, 0), nullable=False)
    wide = col("id", "numeric(12,2)", "decimal", size=("digits", 12, 2), nullable=False)
    assert schema(integer, whole) == [("key_class_differs", "id", "schema")]  # 1 and 1: they can meet
    assert schema(cents, wide) == [("key_size_differs", "id", "schema")]
    day, moment = col("id", "date", nullable=False), col("id", "timestamp", nullable=False)
    assert schema(day, moment) == [
        ("key_text_differs", "id", "not_compared")  # 2024-01-01 is never 2024-01-01 00:00:00
    ]
    assert judged(keyed(day, v), keyed(moment, v), same, level="distinct").line == (
        "key column id is date on side a and timestamp on side b: the key text of a date is never that of a"
        " timestamp, so no key of one side is found on the other: compare a view that casts it"
    )
    assert schema(integer, col("id", "integer", nullable=True)) == [("key_nullable_differs", "id", "schema")]
    assert schema(cents, col("id", "numeric", "decimal", nullable=False)) == []  # a scale on one side alone
    mapped = {"a": {"key_maps": [{"kind": "prefix_pad", "prefix": "A-", "width": 6}]}}
    assert schema(cents, integer, **mapped) == []  # a key map makes the key text
    # A class or a size of a key part is a schema difference, worded as a column's.
    told = judged(keyed(integer, v), keyed(whole, v), same, level="distinct")
    assert (told.verdict, told.line) == (
        "schema_differs",
        "schema: 1 difference: key column id is integer on side a and numeric(10,0) on side b",
    )
    # Across engines: two relations paired by name plan the key P6 would refuse in a spec, and say so.
    oracle = Relation(
        "APP", "T", (col("ID", "NUMBER(10,2)", "decimal", size=("digits", 10, 2), nullable=False), v),
        ORACLE_FACTS | {"key": ["ID"]},
    )  # fmt: skip
    mysql = Relation(
        "ledger", "t", (col("id", "varchar(10)", "text", nullable=False), v), POSTGRES_FACTS | {"key": ["id"]}
    )
    pg = keyed(integer, v)
    implied = precheck.implied(oracle, pg, ("oracle", "postgresql"))
    across = precheck.compare(
        "counts", "counts", Pair(None, "t"), implied, {"a": oracle, "b": pg},
        {"a": read(2, engine="oracle"), "b": read(2)},
    )  # fmt: skip
    assert across.verdict == "columns_not_compared" and across.line == (
        "key column ID is NUMBER(10,2) on side a and integer on side b: a number key's text holds its scale,"
        " so no key of one side is found on the other: compare a view that casts it"
    )
    texts = precheck.implied(pg, mysql, ("postgresql", "mysql"))  # '1' can be the text of 1: they may meet
    found = precheck.compare(
        "counts", "counts", Pair(None, "t"), texts, {"a": pg, "b": mysql},
        {"a": read(2), "b": read(2, engine="mysql")},
    )  # fmt: skip
    assert (found.verdict, found.line) == (
        "schema_differs",
        "schema: 1 difference: key column id is integer on side a and varchar(10) on side b",
    )


# -- what a level reads, and each class's figures (§4) -----------------------------------------------------

CLASSES = [
    col("name", "text"), col("code", "character(3)", "char"), col("qty", "bigint", "integer"),
    col("amount", "numeric(12,2)", "decimal", size=("digits", 12, 2)), col("opened", "date"),
    col("seen", "timestamp", "timestamp"), col("stamp", "timestamptz", "instant"), col("due", "time"),
    col("flag", "boolean"), col("raw", "bytea", "bytes"), col("score", "double precision", "float"),
    col("ratio", "real", "float32"), col("doc", "jsonb", "json"), col("span", "interval", "other"),
    col("note", "CLOB", "text", lob=True),
]  # fmt: skip


def test_each_level_reads_its_figures_by_class() -> None:
    a = relation(*CLASSES)
    plan_, relations = paired(a, a), {"a": a, "b": a}
    assert precheck.wanted("schema", plan_, relations) == {"a": [], "b": []}
    assert precheck.wanted("counts", plan_, relations)["a"] == [("rows", None), ("null_keys", None)]
    found = precheck.wanted("distinct", plan_, relations)
    assert found["a"] == found["b"] and found["a"][:2] == [("rows", None), ("null_keys", None)]
    by: dict[str, list[str]] = {}
    for figure, column in found["a"][2:]:
        by.setdefault(column, []).append(figure)  # type: ignore[arg-type]
    assert by == {
        "name": ["nulls", "empty", "shortest", "longest", "distinct"],
        "code": ["nulls", "empty", "shortest", "longest", "distinct"],
        "qty": ["nulls", "sum", "min", "max", "distinct"],
        "amount": ["nulls", "sum", "min", "max", "distinct"],
        "opened": ["nulls", "min", "max", "distinct"],
        "seen": ["nulls", "min", "max", "distinct"],
        "stamp": ["nulls", "min", "max", "distinct"],
        "due": ["nulls", "min", "max", "distinct"],
        "flag": ["nulls", "true", "distinct"],
        "raw": ["nulls", "shortest", "longest", "distinct"],
        "score": ["nulls"], "ratio": ["nulls"], "doc": ["nulls"], "span": ["nulls"], "note": ["nulls"],
    }  # fmt: skip
    columns = precheck.wanted("columns", plan_, relations)["a"]
    assert columns == [w for w in found["a"] if w[0] != "distinct"]
    # A catalogue pair without a common primary key plans no key: no row is counted as lacking one.
    unkeyed = paired(a, a, a={"keys": []}, b={"keys": []})
    assert precheck.wanted("counts", unkeyed, relations) == {"a": [("rows", None)], "b": [("rows", None)]}
    # Of two classes of one family, the figures both have; of two families, none.
    b = relation(
        col("name", "character(40)", "char"), col("qty", "numeric(12,2)", "decimal"), col("opened", "text")
    )
    mixed = precheck.wanted("columns", paired(a, b), {"a": a, "b": b})["b"][2:]
    assert mixed == [(f, "name") for f in ("nulls", "empty", "shortest", "longest")] + [
        (f, "qty") for f in ("nulls", "sum", "min", "max")
    ]


FIGURES = {
    "name": {"nulls": "1", "empty": "2", "shortest": "0", "longest": "40", "distinct": "7"},
    "code": {"nulls": "0", "empty": "0", "shortest": "3", "longest": "3", "distinct": "4"},
    "qty": {"nulls": "0", "sum": "55", "min": "1", "max": "10", "distinct": "10"},
    "amount": {"nulls": "2", "sum": "4999950000.00", "min": "-0.50", "max": "99.00", "distinct": "8"},
    "opened": {"nulls": "0", "min": "2024-01-01", "max": "2024-05-01", "distinct": "3"},
    "seen": {"nulls": "0", "min": "2024-01-01T00:00:00", "max": "2024-05-01T10:00:00.5", "distinct": "9"},
    "stamp": {"nulls": "0", "min": "2024-01-01T00:00:00+00:00", "max": "2024-05-01T10:00:00+00:00"}
    | {"distinct": "9"},
    "due": {"nulls": "9", "min": "03:04:05.12", "max": "03:04:05.12", "distinct": "1"},
    "flag": {"nulls": "0", "true": "6", "distinct": "2"},
    "raw": {"nulls": "10", "shortest": None, "longest": None, "distinct": "0"},
    "score": {"nulls": "0"}, "ratio": {"nulls": "0"}, "doc": {"nulls": "3"}, "span": {"nulls": "0"},
    "note": {"nulls": "4"},
}  # fmt: skip


def test_figures_per_class() -> None:
    a = relation(*CLASSES)
    same = judged(a, a, (read(**FIGURES), read(**FIGURES)), level="distinct")
    assert (same.verdict, same.differing, same.result["skipped"]) == ("no_difference_observed", 0, [])
    assert same.result["lines"] == [BEST.format("distinct"), READ] and same.line == BEST.format("distinct")
    assert lines(same, "name") == [
        "nulls: side a 1, side b 1 (same)",
        "empty: side a 2, side b 2 (same)",
        "length: side a 0 to 40, side b 0 to 40 characters (same)",
        "distinct: side a 7, side b 7 (same)",
    ]
    assert lines(same, "amount") == [
        "nulls: side a 2, side b 2 (same)",
        "sum: side a 4999950000.00, side b 4999950000.00 (same)",
        "min: same; max: same (values are never shown)",
        "distinct: side a 8, side b 8 (same)",
    ]
    assert lines(same, "flag")[1] == "true: side a 6, side b 6 (same)"
    assert lines(same, "raw")[1] == "length: side a no value, side b no value octets (same)"
    for column, reason in {
        "score": "a float sum depends on the order rows are added in",
        "ratio": "a float sum depends on the order rows are added in",
        "note": "a large-object column",
    }.items():
        nulls = FIGURES[column]["nulls"]
        assert lines(same, column) == [
            f"nulls: side a {nulls}, side b {nulls} (same)",
            f"not compared: {reason}",
        ]
    assert lines(same, "doc") == ["nulls: side a 3, side b 3 (same)"]  # its class has no other figure
    kept = same.result["columns"]["amount"]
    assert kept == {
        "classes": ["decimal", "decimal"],
        "figures": {
            "nulls": {"a": 2, "b": 2, "verdict": "same", "reason": None},
            "sum": {"a": "4999950000.00", "b": "4999950000.00", "verdict": "same", "reason": None},
            "min": {"verdict": "same"},  # D6: its verdict alone
            "max": {"verdict": "same"},
            "distinct": {"a": 8, "b": 8, "verdict": "same"},
        },
        "reason": None,
    }
    # No minimum, maximum or text is in a result (N4); a sum only where the spec clears its column.
    stored = jsonio.dumps(same.result)
    for value in ("2024-01-01", "2024-05-01", "03:04:05", "-0.50", "99.00"):
        assert value not in stored.replace(AT, "")
    assert '"4999950000.00"' in stored
    other = FIGURES | {
        "name": FIGURES["name"] | {"nulls": "0", "longest": "38"},
        "amount": FIGURES["amount"] | {"sum": "4999949987.50", "distinct": "7"},
        "opened": FIGURES["opened"] | {"max": "2024-05-02"},
        "flag": FIGURES["flag"] | {"true": "5"},
        "raw": FIGURES["raw"] | {"nulls": "9", "shortest": "0", "longest": "16", "distinct": "1"},
        "note": {"nulls": "5"},
    }
    found = judged(a, a, (read(**FIGURES), read(**other)), level="distinct")
    assert (found.verdict, found.rank, found.differing) == ("values_differ", 7, 10)
    assert found.line == (
        "6 of 15 compared columns differ in a figure: name (nulls, length), amount (sum, distinct), opened"
        " (max), flag (true), raw (nulls, length, distinct), note (nulls)"
    )
    assert lines(found, "name")[:3] == [
        "nulls: side a 1, side b 0 (differs)",
        "empty: side a 2, side b 2 (same)",
        "length: side a 0 to 40, side b 0 to 38 characters (differs)",
    ]
    assert lines(found, "amount")[1:3] == [
        "sum: side a 4999950000.00, side b 4999949987.50 (side b is 12.50 lower)",
        "min: same; max: same (values are never shown)",
    ]
    assert lines(found, "opened")[1] == "min: same; max: differs (values are never shown)"
    assert lines(found, "raw")[1] == "length: side a no value, side b 0 to 16 octets (differs)"
    # At a lower level no figure of a column is read, and the column says so.
    counts = judged(a, a, (read(), read()), level="counts")
    assert counts.verdict == "no_difference_observed" and lines(counts, "amount") == [
        "not compared: level counts"
    ]
    assert lines(judged(a, a, (read(None), read(None)), level="schema"), "name") == [
        "not compared: level schema"
    ]


def test_numbers_compare_exactly_and_any_other_rendering_as_its_text() -> None:
    a = relation(
        col("amount", "NUMBER(10,2)", "decimal", size=("digits", 10, 2)), col("qty", "bigint", "integer")
    )
    b = relation(
        col("amount", "numeric(10,2)", "decimal", size=("digits", 10, 2)),
        col("qty", "numeric(20,0)", "decimal"),
    )
    # 3.50 is 3.5, .5 is 0.50 and 1E+3 is 1000: an engine's text of a number is no part of its value.
    one = {
        "amount": {"nulls": "0", "sum": "3.50", "min": ".5", "max": "1E+3"},
        "qty": {"nulls": "0", "sum": "7", "min": "1", "max": "4"},
    }
    two = {
        "amount": {"nulls": "0", "sum": "3.5", "min": "0.50", "max": "1000.00"},
        "qty": {"nulls": "0", "sum": "7.0", "min": "1", "max": "4"},
    }
    found = judged(a, b, (read(**one), read(**two)))
    assert (found.verdict, found.differing) == ("schema_differs", 0)  # NUMBER(10,2) and numeric(10,2): none
    assert found.line == "schema: 1 difference: qty is bigint on side a and numeric(20,0) on side b"
    assert lines(found, "amount")[1] == "sum: side a 3.50, side b 3.5 (same)"
    assert lines(found, "amount")[2] == "min: same; max: same (values are never shown)"
    # A side without a value has no sum: against a number it differs.
    none = {"amount": {"nulls": "10", "sum": None, "min": None, "max": None}, "qty": one["qty"]}
    found = judged(a, b, (read(**none), read(**two)))
    assert lines(found, "amount")[:3] == [
        "nulls: side a 10, side b 0 (differs)",
        "sum: side a no value, side b 3.5 (differs)",
        "min: differs; max: differs (values are never shown)",
    ]
    assert (
        lines(judged(a, b, (read(**none), read(**none))), "amount")[1]
        == "sum: side a no value, side b no value (same)"
    )


# -- sums: exact, or not compared; shown, or withheld (§4, §6) ---------------------------------------------


def test_sum_exact_bounds() -> None:
    assert precheck.exact(10**30, Decimal("1E30"), None, None)  # PostgreSQL: every digit
    # 38 digits: the whole digits of the largest value, the scale and the digits of the count.
    assert precheck.exact(999, Decimal("99999999.99"), 2, 38)  # 8 + 2 + 3
    assert precheck.exact(10**9 - 1, Decimal(10**27 - 1), 2, 38)  # 27 + 2 + 9 = 38
    assert not precheck.exact(10**9, Decimal(10**27 - 1), 2, 38)  # one digit more of rows
    assert not precheck.exact(10**9 - 1, Decimal(10**27), 2, 38)  # or of magnitude
    assert not precheck.exact(10**9 - 1, Decimal(10**27 - 1), 3, 38)  # or of scale
    assert precheck.exact(10**9 - 1, Decimal(10**27 - 1), 29, 65) and not precheck.exact(
        10**9, Decimal(10**27), 29, 65
    )
    assert precheck.exact(10**9 - 1, Decimal(10**29 - 1), -2, 38)  # a negative scale counts 0
    assert not precheck.exact(1, Decimal(1), None, 38)  # no scale: any digit may be lost
    assert precheck.exact(0, Decimal(0), 0, 38) and precheck.exact(1, Decimal("0.004"), 3, 38)
    # Oracle rounds silently: 1E30, 1E-20, 5 and 1.5 in a NUMBER print 32 digits without the 1E-20.
    lost = {
        "n": {
            "nulls": "0",
            "sum": "1000000000000000000000000000006.5",
            "min": ".00000000000000000001",
            "max": "1E+30",
        }
    }
    kept = {
        "n": {
            "nulls": "0",
            "sum": "1000000000000000000000000000006.50000000000000000001",
            "min": "0.00000000000000000001",
            "max": "1000000000000000000000000000000",
        }
    }
    oracle = relation(col("n", "NUMBER", "decimal"), name="APP.T")
    found = judged(
        oracle,
        relation(col("n", "numeric", "decimal")),
        (read(4, engine="oracle", sum_digits=38, **lost), read(4, **kept)),
    )
    assert (
        found.verdict == "no_difference_observed" and found.differing == 0
    )  # never a difference, never same
    assert lines(found, "n")[1] == "sum: not compared (side a's NUMBER declares no scale)"
    assert found.result["columns"]["n"]["figures"]["sum"] == {
        "verdict": "not compared",
        "reason": "side a's NUMBER declares no scale",
    }
    scaled = col("n", "NUMBER(38,2)", "decimal", size=("digits", 38, 2))
    big = {"n": {"nulls": "0", "sum": "1", "min": "1", "max": str(10**35)}}
    for engine, digits, said in (("oracle", 38, "the 38 digits an oracle sum holds"), ("mysql", 65, None)):
        found = judged(
            relation(scaled), relation(scaled), (read(**big), read(engine=engine, sum_digits=digits, **big))
        )
        expected = (
            "sum: side a 1, side b 1 (same)"
            if said is None
            else f"sum: not compared (side b's sum could pass {said})"
        )
        assert lines(found, "n")[1] == expected
    wide = {"n": {"nulls": "0", "sum": "1", "min": "1", "max": str(10**61)}}
    found = judged(
        relation(scaled), relation(scaled), (read(**wide), read(engine="mysql", sum_digits=65, **wide))
    )
    assert (
        lines(found, "n")[1] == "sum: not compared (side b's sum could pass the 65 digits a mysql sum holds)"
    )
    # An integer's scale is 0, whatever it declares.
    whole = col("n", "NUMBER(10)", "integer", size=("digits", 10, 0))
    small = {"n": {"nulls": "0", "sum": "55", "min": "1", "max": "10"}}
    found = judged(
        relation(whole),
        relation(col("n", "bigint", "integer")),
        (read(engine="oracle", sum_digits=38, **small), read(**small)),
    )
    assert lines(found, "n")[1] == "sum: side a 55, side b 55 (same)"


def test_nan_and_infinity_sums_not_compared() -> None:
    rate = relation(col("rate", "numeric", "decimal"))
    held = {"rate": {"nulls": "0", "sum": "NaN", "min": "1", "max": "NaN"}}
    found = judged(rate, rate, (read(**held), read(**held)))
    assert (found.verdict, found.differing) == ("no_difference_observed", 0)  # two equal sides do not differ
    assert lines(found, "rate")[1:] == [
        "sum: not compared (side a holds NaN or Infinity)",
        "min: same; max: same (values are never shown)",  # as their texts
    ]
    endless = {"rate": {"nulls": "0", "sum": "Infinity", "min": "-Infinity", "max": "Infinity"}}
    found = judged(rate, rate, (read(**{"rate": held["rate"] | {"sum": "7", "max": "6"}}), read(**endless)))
    assert lines(found, "rate")[1:] == [
        "sum: not compared (side b holds NaN or Infinity)",
        "min: differs; max: differs (values are never shown)",
    ]
    assert (found.verdict, found.differing) == ("values_differ", 2)


POLICY = SecurityPolicy(
    tokenize=(TokenizeRule("$.iban", "account_number"),), clear=("$.amount", "$.iban"), unlisted="tokenize"
)


def test_sum_shown_only_under_four_conditions() -> None:
    numbers = [
        col(name, "numeric(12,2)", "decimal", size=("digits", 12, 2)) for name in ("amount", "iban", "fee")
    ]
    a = relation(*numbers)
    one = {
        name: {"nulls": "0", "sum": "100.00", "min": "1", "max": "9"} for name in ("amount", "iban", "fee")
    }
    two = {name: figures | {"sum": "87.50"} for name, figures in one.items()}

    def sums(pair: Pair, **members: Any) -> dict[str, str]:
        found = judged(a, a, (read(**one), read(**two)), pair=pair, **members)
        assert found.verdict == "values_differ"
        return {name: lines(found, name)[1] for name in one}

    spec = Pair(12, "customer-profile 1.4.0", True, POLICY)
    shown = "sum: side a 100.00, side b 87.50 (side b is 12.50 lower)"
    assert sums(spec) == {
        "amount": shown,  # listed under clear
        # A floor class tokenises whatever clear says; an unlisted column under unlisted: tokenize.
        "iban": "sum: differs (figures withheld: $.iban is protected by the spec's security policy)",
        "fee": "sum: differs (figures withheld: $.fee is protected by the spec's security policy)",
    }
    clear = Pair(12, "customer-profile 1.4.0", True, SecurityPolicy(unlisted="clear"))
    assert sums(clear) == dict.fromkeys(one, shown)
    assert sums(Pair(12, "customer-profile 1.4.0", True, None))["amount"].endswith("security policy)")
    # A side with a filter: two filters one row apart would give that row's value.
    for name in ("a", "b"):
        withheld = f"sum: differs (figures withheld: side {name} declares a filter)"
        assert sums(clear, **{name: {"where": '"fee" > 1'}}) == dict.fromkeys(one, withheld)
    unapproved = Pair(12, "customer-profile 1.4.0", True, SecurityPolicy(unlisted="clear"), approved=False)
    assert sums(unapproved)["amount"] == "sum: differs (figures withheld: spec 12 is not approved)"
    named = "sum: differs (figures withheld: paired by name, no security policy)"
    assert sums(Pair(None, security=SecurityPolicy(unlisted="clear")))["amount"] == named
    kept = judged(a, a, (read(**one), read(**one)), pair=spec).result["columns"]
    assert kept["iban"]["figures"]["sum"] == {
        "verdict": "same",
        "reason": "figures withheld: $.iban is protected by the spec's security policy",
    }
    assert "100.00" not in jsonio.dumps(kept["iban"]) and "100.00" in jsonio.dumps(kept["amount"])


def test_under_a_filter_a_protected_column_s_counts_are_verdicts() -> None:
    """A filter that names only the key selects one row or two: a protected column's true, empty and
    distinct counts and its lengths would tell that row's value is true, empty or how long, and whether
    two rows hold one value. They are kept as verdicts, as its sum is; its NULLs and the rows stay numbers,
    as does every figure of a column the spec clears."""
    a = relation(col("vip", "boolean"), col("note", "text"), col("ssn", "text"), col("amount", "text"))
    one = {
        "vip": {"nulls": "0", "true": "1", "distinct": "1"},
        "note": {"nulls": "0", "empty": "1", "shortest": "0", "longest": "0", "distinct": "1"},
        "ssn": {"nulls": "0", "empty": "0", "shortest": "11", "longest": "11", "distinct": "1"},
        "amount": {"nulls": "0", "empty": "0", "shortest": "4", "longest": "4", "distinct": "1"},
    }
    two = one | {"vip": {"nulls": "0", "true": "0", "distinct": "1"}}
    tokens = Pair(
        12, "customer-profile 1.4.0", True, SecurityPolicy(clear=("$.amount",), unlisted="tokenize")
    )
    found = judged(
        a, a, (read(1, **one), read(1, **two)), level="distinct", pair=tokens, a={"where": '"id" = 7'}
    )
    withheld = (
        "figures withheld: side a declares a filter and $.{} is protected by the spec's security policy"
    )
    assert found.result["columns"]["vip"]["figures"] == {
        "nulls": {"a": 0, "b": 0, "verdict": "same", "reason": None},
        "true": {"verdict": "differs", "reason": withheld.format("vip")},
        "distinct": {"verdict": "same", "reason": withheld.format("vip")},
    }
    assert lines(found, "note") == [
        "nulls: side a 0, side b 0 (same)",
        f"empty: same ({withheld.format('note')})",
        f"length: same ({withheld.format('note')})",
        f"distinct: same ({withheld.format('note')})",
    ]
    assert lines(found, "amount")[1:3] == [
        "empty: side a 0, side b 0 (same)",
        "length: side a 4 to 4, side b 4 to 4 characters (same)",
    ]
    assert (found.verdict, found.differing) == ("values_differ", 1)
    for column in ("vip", "note", "ssn"):  # of a protected column, its NULLs alone are kept as numbers
        assert [f for f, kept in found.result["columns"][column]["figures"].items() if "a" in kept] == [
            "nulls"
        ]
    unfiltered = judged(a, a, (read(1, **one), read(1, **two)), level="distinct", pair=tokens)
    assert unfiltered.result["columns"]["vip"]["figures"]["true"] == {"a": 1, "b": 0, "verdict": "differs"}


# -- NULLs, empty strings and typeMaps (§4) ----------------------------------------------------------------

ORACLE = {"engine": "oracle", "sum_digits": 38, "empty_null": True}


def test_oracle_empty_stays_a_difference() -> None:
    a = relation(col("EMAIL", "VARCHAR2(120 CHAR)", "text"), name="APP.CUSTOMERS", server="19.21")
    b = relation(col("Email", "text"))
    stored = {"nulls": "120", "empty": "0", "shortest": "6", "longest": "40"}
    held = {"nulls": "0", "empty": "120", "shortest": "0", "longest": "40"}
    found = judged(a, b, (read(email=stored, **ORACLE), read(email=held)))
    assert found.verdict == "values_differ"
    assert found.line == "1 of 1 compared columns differ in a figure: email (nulls, empty, length)"
    assert lines(found, "email")[0] == (
        "nulls: side a 120, side b 0 (differs); side b holds 120 empty string(s), which Oracle (side a)"
        " stores as NULL: declare typeMap {Email: {as: string, emptyAsNull: true}} on side b to compare them"
        " as equal"
    )
    # The other way round, and a gap that is not the other side's empty strings: no bridge is offered.
    found = judged(b, a, (read(email=held), read(email=stored, **ORACLE)))
    assert lines(found, "email")[0].startswith(
        "nulls: side a 0, side b 120 (differs); side a holds 120 empty"
    )
    assert "which Oracle (side b) stores as NULL" in lines(found, "email")[0]
    found = judged(a, b, (read(email=stored | {"nulls": "121"}, **ORACLE), read(email=held)))
    assert lines(found, "email")[0] == "nulls: side a 121, side b 0 (differs)"
    found = judged(a, a, (read(email=stored, **ORACLE), read(email=stored | {"nulls": "0"}, **ORACLE)))
    assert lines(found, "email")[0] == "nulls: side a 120, side b 0 (differs)"  # both store '' as NULL
    # Folded under emptyAsNull: the side's empty strings count with its NULLs, as a comparison reads them.
    mapped = {"b": {"types": {"email": {"as": "string", "emptyAsNull": True}}}}
    assert precheck.wanted("columns", paired(a, b, **mapped), {"a": a, "b": b}) == {
        "a": [("rows", None), ("null_keys", None), ("nulls", "email")],
        "b": [("rows", None), ("null_keys", None), ("nulls", "email"), ("empty", "email")],
    }
    found = judged(
        a, b, (read(email={"nulls": "120"}, **ORACLE), read(email={"nulls": "0", "empty": "120"})), **mapped
    )
    assert (found.verdict, found.differing) == ("no_difference_observed", 0)
    assert lines(found, "email") == [
        "nulls: side a 120, side b 120 (same); side b's 120 empty string(s) count as NULL (emptyAsNull)",
        "not compared: typeMap converts this column per row",
    ]


def test_a_protected_column_s_nulls_under_a_filter_tell_no_empty_string() -> None:
    """Under a filter a protected column's empty count is a verdict (§6): the reason beside its NULLs names
    no count, and the bridge is offered whatever the other side holds, so it tells no value is empty."""
    a = relation(col("EMAIL", "VARCHAR2(120 CHAR)", "text"), name="APP.CUSTOMERS", server="19.21")
    b = relation(col("Email", "text"))
    tokens = Pair(12, "customer-profile 1.4.0", True, SecurityPolicy(unlisted="tokenize"))
    stored = {"nulls": "1", "empty": "0", "shortest": "6", "longest": "40"}
    held = {"nulls": "0", "empty": "1", "shortest": "0", "longest": "0"}
    filtered = {"where": '"ID" = 7'}
    bridge = (
        "Oracle (side a) stores as NULL any empty string side b holds: declare typeMap {Email: {as: string,"
        " emptyAsNull: true}} on side b to compare them as equal"
    )
    for b_holds in (held, held | {"empty": "0"}):
        found = judged(
            a, b, (read(1, email=stored, **ORACLE), read(1, email=b_holds)), pair=tokens, a=filtered
        )
        assert found.result["columns"]["email"]["figures"]["nulls"]["reason"] == bridge
    mapped = {"a": filtered, "b": {"types": {"email": {"as": "string", "emptyAsNull": True}}}}
    sides = (read(1, email={"nulls": "1"}, **ORACLE), read(1, email={"nulls": "0", "empty": "1"}))
    found = judged(a, b, sides, pair=tokens, **mapped)
    assert found.result["columns"]["email"]["figures"]["nulls"] == {
        "a": 1, "b": 1, "verdict": "same", "reason": "side b's empty string(s) count as NULL (emptyAsNull)",
    }  # fmt: skip


def test_typemap_gives_nulls_only() -> None:
    a = relation(col("opened", "DATE", "timestamp"), col("amount", "text"), name="APP.T")
    b = relation(col("opened", "date"), col("amount", "numeric(12,2)", "decimal"))
    types = {"a": {"types": {"opened": {"as": "date"}, "amount": {"as": "decimal", "scale": 2}}}}
    plan_ = paired(a, b, **types)
    assert precheck.wanted("distinct", plan_, {"a": a, "b": b})["a"] == [
        ("rows", None), ("null_keys", None), ("nulls", "opened"), ("nulls", "amount"),
    ]  # fmt: skip
    figures = {"opened": {"nulls": "1"}, "amount": {"nulls": "0"}}
    found = judged(a, b, (read(**figures), read(**figures)), level="distinct", **types)
    assert found.verdict == "schema_differs"  # a class within the family its typeMap gives
    assert lines(found, "opened") == [
        "nulls: side a 1, side b 1 (same)",
        "not compared: typeMap converts this column per row",
    ]
    # Without the typeMap the two sides are of two families: no figure at all, and the pair says why.
    found = judged(a, b, (read(), read()))
    assert found.verdict == "columns_not_compared" and found.line == (
        "2 columns would not be compared: opened is a timestamp (DATE) on side a and a date on side b:"
        " declare typeMap {opened: {as: date}} on side a, or {as: timestamp} on both; amount is text"
        " (text) on side a and a number on side b: declare a typeMap that gives both sides one type, or"
        " compare a view that casts it"
    )
    assert lines(found, "amount") == [f"not compared: {found.line.split('; ')[1]}"]
    assert found.result["columns"]["opened"]["figures"] == {}


# -- the verdict (§4) --------------------------------------------------------------------------------------


def test_missing_column_outranks_a_differing_figure() -> None:
    email = col("email", "text")
    a, b = relation(email, col("legacy_flag", "boolean")), relation(email)
    one = {"nulls": "0", "empty": "0", "shortest": "6", "longest": "40"}
    found = judged(a, b, (read(email=one), read(email=one | {"nulls": "3"})))
    assert (found.verdict, found.rank, found.differing) == ("columns_not_compared", 6, 1)
    assert found.result["lines"] == [
        "1 column would not be compared: legacy_flag is on side a only",
        "1 of 1 compared columns differ in a figure: email (nulls)",
        READ,
    ]


def test_verdict_ladder() -> None:
    """The verdict is the first that holds: a pair with every cause, then with one fewer each time."""
    counted = {"nulls": "0", "empty": "0", "shortest": "6", "longest": "40", "distinct": "9"}
    refusal = "side a: column 'x' is not in public.customers"

    def pair(*holds: str) -> precheck.Judged:
        a = relation(col("email", "text"), *[col("extra", "text")] * ("columns_not_compared" in holds))
        b = relation(col("email", "text", nullable="schema_differs" not in holds))
        figures = counted | ({"nulls": "3"} if "values_differ" in holds else {})
        side_a = read(
            9 if "rows_differ" in holds else 10,
            null_keys=int("keys_null_or_repeated" in holds),
            email=figures,
            skipped=["distinct_rows"] * ("incomplete" in holds),
            failed={"distinct": ("temp_space", "53400")} if "not_read" in holds else {},
        )
        sides = {"a": side_a, "b": read(email=counted)}
        refused = [refusal] * ("refused" in holds)
        return precheck.compare("distinct", "distinct", SPEC, paired(a, b), {"a": a, "b": b}, sides, refused)

    ladder = precheck.VERDICTS[1:-1]  # unpaired is a name without a pair: nothing of it is read
    for n, verdict in enumerate(ladder):
        found = pair(*ladder[n:])
        assert (found.verdict, found.rank) == (verdict, n + 2)
        assert len(found.result["lines"]) == len(ladder) - n + 1  # a line a cause, and how it was read
        assert found.line == found.result["lines"][0] and not N1.search("\n".join(found.result["lines"]))
    assert (pair().verdict, pair().rank, pair().result["lines"]) == (
        "no_difference_observed",
        10,
        [BEST.format("distinct"), READ],
    )
    assert pair("refused", "values_differ").line == f"a run of this spec would be refused: {refusal}"
    one = {"nulls": "0", "empty": "0", "shortest": "6", "longest": "40"}
    email = relation(col("email", "text"))
    repeated = read(duplicate_keys=3, rows_in_duplicates=7, email=one)
    assert (
        judged(email, email, (repeated, read(email=one)), level="distinct").verdict == "keys_null_or_repeated"
    )
    # Pairs sort by rank, then by the most differing figures: the ladder is the store's order.
    assert [precheck.rank(v) for v in ("unpaired", "values_differ", "no_difference_observed")] == [1, 7, 10]


sizes = st.sampled_from([None, ("digits", 12, 2), ("digits", 10, 0)])
counts = st.integers(0, 50).map(str)
numbers = st.sampled_from(["0", "1.50", "-3", "NaN", "100", "1E+3", None])


@st.composite
def reads(draw: st.DrawFn) -> tuple[Relation, dict[str, Any]]:
    """A relation of a text and a number column, and what a pre-check read of it."""
    nullable, scale = draw(st.booleans()), draw(sizes)
    found = relation(
        col("email", "text", nullable=nullable), col("amount", "numeric", "decimal", size=scale),
        **draw(st.sampled_from([{}, {"kind": "view", "key": None}, {"key": ["id", "email"]}])),
    )  # fmt: skip
    email = {"nulls": draw(counts), "empty": draw(counts), "shortest": draw(counts), "longest": draw(counts)}
    amount = {"nulls": draw(counts), "sum": draw(numbers), "min": draw(numbers), "max": draw(numbers)}
    engine = draw(st.sampled_from([{}, ORACLE]))
    return found, read(
        draw(st.integers(50, 60)), null_keys=draw(st.integers(0, 1)), email=email, amount=amount, **engine
    )


@given(a=reads(), b=reads(), level=st.sampled_from(precheck.LEVELS[:3]))
def test_compare_is_symmetric(
    a: tuple[Relation, dict[str, Any]], b: tuple[Relation, dict[str, Any]], level: str
) -> None:
    one, other = judged(a[0], b[0], (a[1], b[1]), level=level), judged(b[0], a[0], (b[1], a[1]), level=level)
    assert (one.verdict, one.differing) == (other.verdict, other.differing)
    for column, found in one.result["columns"].items():
        mirrored = other.result["columns"][column]["figures"]
        assert {f: judged_["verdict"] for f, judged_ in found["figures"].items()} == {
            f: judged_["verdict"] for f, judged_ in mirrored.items()
        }
        for figure, judged_ in found["figures"].items():
            assert (judged_.get("a"), judged_.get("b")) == (
                mirrored[figure].get("b"),
                mirrored[figure].get("a"),
            )
    assert not N1.search("\n".join(one.result["lines"]))
    assert jsonio.dumps(one.result)  # a result is JSON: counts, verdicts, names


# -- the schema guard (§7) ---------------------------------------------------------------------------------

EMAIL, AMOUNT = col("email", "varchar(20)", "text", nullable=False), col("amount", "numeric(12,2)", "decimal")
FLAG = col("legacy_flag", "boolean")  # a column no plan reads
PLANNED = {"id": "key", "email": "compared", "amount": "compared"}


def shaped(*columns: Column, **facts: Any) -> dict[str, Any]:
    return precheck.shape(relation(*columns, **facts))


def kinds(found: list[dict[str, Any]]) -> list[tuple[str, bool, str | None]]:
    return [(change["kind"], change["breaking"], change["column"]) for change in found]


def test_a_shape_holds_only_what_the_account_reads() -> None:
    """A column the role may not read is not in the shape, on any engine: one revoked reads as one
    dropped (N10: as the account reads it)."""
    hidden = Column("salary", "numeric(12,2)", "decimal", False, False)
    assert precheck.shape(relation(EMAIL, hidden)) == precheck.shape(relation(EMAIL))


def test_roles_name_what_a_plan_reads_each_column_as() -> None:
    plan = side({"email": "Email", "amount": "amount"}, where="opened >= '2020-01-01' AND amount > 0")
    plan |= {"strata": {"region": "REGION"}}
    assert precheck.roles(plan) == {
        "id": "key", "Email": "compared", "amount": "compared", "opened": "filter", "REGION": "strata",
    }  # fmt: skip
    assert precheck.roles(side({"id": "id"})) == {"id": "key"}  # of several roles, the key's


def test_changes_per_kind() -> None:
    before = shaped(EMAIL, AMOUNT, FLAG)
    assert precheck.changes(before, shaped(AMOUNT, FLAG, EMAIL), PLANNED) == []  # the order is not in it
    assert kinds(precheck.changes(before, None, PLANNED)) == [("relation_missing", True, None)]
    one = {
        "column_missing": shaped(EMAIL, FLAG),
        "class_changed": shaped(EMAIL, col("amount", "text"), FLAG),
        "column_added": shaped(EMAIL, AMOUNT, FLAG, col("loyalty_tier", "text")),
        "column_removed": shaped(EMAIL, AMOUNT),
        "type_changed": shaped(EMAIL, col("amount", "numeric(14,2)", "decimal"), FLAG),
        "nullable_changed": shaped(col("email", "varchar(20)", "text"), AMOUNT, FLAG),
        "key_changed": shaped(EMAIL, AMOUNT, FLAG, key=["id", "email"]),
        "kind_changed": shaped(EMAIL, AMOUNT, FLAG, kind="view"),
    }
    assert set(one) | {"relation_missing", "key_render_changed", "render_changed", "compared_lost"} == {
        *precheck.BREAKING, *precheck.OTHER,
    }  # fmt: skip
    for kind, after in one.items():
        [change] = precheck.changes(before, after, PLANNED)
        assert (change["kind"], change["breaking"]) == (kind, kind in precheck.BREAKING)
    [missing] = precheck.changes(before, one["column_missing"], PLANNED)
    assert missing == {
        "kind": "column_missing", "breaking": True, "column": "amount",
        "was": ["numeric(12,2)", "decimal", True], "now": None,
    }  # fmt: skip
    [kind] = precheck.changes(before, one["kind_changed"], PLANNED)
    assert (kind["column"], kind["was"], kind["now"]) == (None, "table", "view")
    # A column no plan reads never breaks it, whatever becomes of it; the breaking changes come first.
    several = shaped(col("email", "varchar(20)", "text"), col("legacy_flag", "text"))
    assert kinds(precheck.changes(before, several, PLANNED)) == [
        ("column_missing", True, "amount"), ("nullable_changed", False, "email"),
        ("type_changed", False, "legacy_flag"),
    ]  # fmt: skip
    # Without a shape held (a run planned before the guard existed) only what is missing is judged.
    assert kinds(precheck.changes(None, shaped(EMAIL, col("extra", "text")), PLANNED)) == [
        ("column_missing", True, "amount")
    ]
    assert kinds(precheck.changes(None, None, PLANNED)) == [("relation_missing", True, None)]


def test_widened_key_is_not_breaking() -> None:
    """A key column's declared type may change within its class: a widened text key, a wider whole
    number, render the same key texts."""
    planned = {"code": "key", "amount": "compared"}
    for was, now in (
        (col("code", "varchar(20)", "text"), col("code", "varchar(40)", "text")),
        (col("code", "NUMBER(10)", "integer"), col("code", "NUMBER(12)", "integer")),
        (col("code", "numeric(10,2)", "decimal"), col("code", "numeric(12,2)", "decimal")),
    ):
        found = precheck.changes(shaped(was, AMOUNT), shaped(now, AMOUNT), planned)
        assert kinds(found) == [("type_changed", False, "code")]


def test_respelled_integer_is_not_breaking() -> None:
    """A server upgrade that respells a type (MySQL's ``int(11)`` as ``int``) warns: the engine builds the
    same key text and rendering of both."""
    was, now = col("code", "int(11)", "integer"), col("code", "int", "integer")
    for planned in ({"code": "key"}, {"code": "compared"}):
        found = precheck.changes(shaped(was), shaped(now), planned, built=ENGINES["mysql"].built)
        assert kinds(found) == [("type_changed", False, "code")]


def test_key_scale_and_oracle_precision_are_breaking() -> None:
    """ENGINES §8 builds a number key's text from its scale, and on Oracle, whose key text is a mask of
    both, from its precision too: the key text the engine builds (``built``) is what is compared."""
    planned, oracle = {"ID": "key"}, ENGINES["oracle"].built

    def number(declared: str, cls: str = "decimal") -> dict[str, Any]:
        return shaped(col("ID", declared, cls))

    scale = precheck.changes(number("NUMBER(10,2)"), number("NUMBER(10,4)"), planned)
    assert kinds(scale) == [("key_render_changed", True, "ID")]
    assert (scale[0]["was"][0], scale[0]["now"][0]) == ("NUMBER(10,2)", "NUMBER(10,4)")
    precision = number("NUMBER(10,2)"), number("NUMBER(12,2)")
    assert kinds(precheck.changes(*precision, planned, built=oracle)) == [("key_render_changed", True, "ID")]
    assert kinds(precheck.changes(*precision, planned)) == [("type_changed", False, "ID")]
    assert kinds(precheck.changes(*precision, planned, built=ENGINES["postgresql"].built)) == [
        ("type_changed", False, "ID")
    ]  # a key's text is its ::text there
    # A scale that appears or goes: an unconstrained NUMBER's text is a whole number's.
    assert kinds(precheck.changes(number("NUMBER"), number("NUMBER(10,2)"), planned)) == [
        ("key_render_changed", True, "ID")
    ]
    # Oracle's NUMBER(*,2) is NUMBER(38,2): one mask, one key text, so the respelling breaks nothing.
    star = precheck.changes(number("NUMBER(38,2)"), number("NUMBER(*,2)"), planned, built=oracle)
    assert kinds(star) == [("type_changed", False, "ID")]
    # The same change of a compared column breaks nothing: its values render as planned.
    assert kinds(precheck.changes(*precision, {"ID": "compared"}, built=oracle)) == [
        ("type_changed", False, "ID")
    ]


def test_a_type_its_engine_reads_otherwise_is_breaking() -> None:
    """A change of a planned column's declared type within its class breaks the plan where the side's
    engine builds other statements of the new type than of the planned one: Oracle renders a DATE without
    the fraction a TIMESTAMP's rendering holds, MySQL casts a bit first. A respelling it reads alike warns,
    as does any such change against the basis at a creation: the new run's plan reads the type it is."""
    oracle, mysql, postgresql = (ENGINES[name].built for name in ("oracle", "mysql", "postgresql"))

    def changed(was: str, now: str, cls: str, role: str, **options: Any) -> list[Any]:
        before, after = shaped(col("c", was, cls)), shaped(col("c", now, cls))
        return kinds(precheck.changes(before, after, {"c": role}, **options))

    breaks = [("render_changed", True, "c")]
    for role in ("compared", "strata"):
        assert changed("DATE", "TIMESTAMP(6)", "timestamp", role, built=oracle) == breaks
        assert changed("TIMESTAMP(6)", "DATE", "timestamp", role, built=oracle) == breaks
        assert changed("tinyint", "bit(8)", "integer", role, built=mysql) == breaks
    assert changed("tinyint(1)", "bit(1)", "integer", "key", built=mysql) == [
        ("key_render_changed", True, "c")
    ]
    warns = [("type_changed", False, "c")]
    assert changed("TIMESTAMP(6)", "TIMESTAMP(9)", "timestamp", "compared", built=oracle) == warns
    assert changed("varchar(20)", "varchar(40)", "text", "compared", built=mysql) == warns
    assert changed("DATE", "TIMESTAMP(6)", "timestamp", "filter", built=oracle) == warns
    assert changed("DATE", "TIMESTAMP(6)", "timestamp", "compared") == warns
    assert changed("timestamp(0)", "timestamp(6)", "timestamp", "compared", built=postgresql) == warns
    basis = {"c": "compared"}
    assert changed("DATE", "TIMESTAMP(6)", "timestamp", "compared", built=oracle, basis=basis) == warns


def test_changes_against_the_basis_narrow() -> None:
    """At a run's creation: a column the basis compared that the new run does not, and a column both
    plan whose class or key rendering differs, narrow the comparison; any other change breaks nothing."""
    before, was = shaped(EMAIL, AMOUNT, FLAG), PLANNED | {"legacy_flag": "compared"}
    # Side a still holds the column that side b dropped: it is no longer compared on either.
    assert kinds(precheck.changes(before, before, PLANNED, basis=was)) == [
        ("compared_lost", True, "legacy_flag")
    ]
    gone = precheck.changes(before, shaped(EMAIL, AMOUNT), PLANNED, basis=was)
    assert kinds(gone) == [("compared_lost", True, "legacy_flag")]  # and not removed besides
    retyped = shaped(EMAIL, col("amount", "text"), FLAG, col("loyalty_tier", "text"))
    now = was | {"loyalty_tier": "compared"}
    assert kinds(precheck.changes(before, retyped, now, basis=was)) == [
        ("class_changed", True, "amount"), ("column_added", False, "loyalty_tier"),
    ]  # fmt: skip
    # A basis that held no shape (its run predates the guard): only its compared columns are judged.
    assert kinds(precheck.changes(None, retyped, PLANNED, basis=was)) == [
        ("compared_lost", True, "legacy_flag")
    ]


def test_verdict_per_mode() -> None:
    breaking, other = [{"breaking": True}], [{"breaking": False}]
    assert [precheck.verdict(mode, []) for mode in precheck.MODES] == [None, None]
    assert precheck.MODES == ("finding", "pause")
    assert [precheck.verdict(mode, [*other, *breaking]) for mode in precheck.MODES] == ["paused", "paused"]
    assert [precheck.verdict(mode, other) for mode in precheck.MODES] == ["warned", "paused"]
    created = [precheck.verdict(mode, breaking, creation=True) for mode in precheck.MODES]
    assert created == ["paused", "refused"]  # narrowing: created paused, or refused when the spec pauses
    assert [precheck.verdict(mode, other, creation=True) for mode in precheck.MODES] == ["warned", "paused"]


TYPES = st.sampled_from(
    [("text", "text"), ("varchar(20)", "text"), ("integer", "integer"), ("numeric(12,2)", "decimal"),
     ("numeric(12,4)", "decimal"), ("date", "date")]
)  # fmt: skip
NAMES = st.sampled_from(["email", "amount", "opened", "legacy_flag", "note"])


@st.composite
def shapes(draw: st.DrawFn) -> dict[str, Any]:
    columns = [
        col(name, *draw(TYPES), nullable=draw(st.booleans()))
        for name in draw(st.lists(NAMES, unique=True, max_size=5))
    ]
    facts = draw(st.sampled_from([{}, {"kind": "view", "key": None}, {"key": ["id", "email"]}]))
    return shaped(*columns, **facts)


BUILT = st.sampled_from([None, *(engine.built for engine in ENGINES.values())])  # an engine's, or none


@given(held=shapes(), built=BUILT)
def test_a_shape_does_not_differ_from_itself(held: dict[str, Any], built: Any) -> None:
    planned = dict.fromkeys(held["columns"], "compared") | {"id": "key"}
    assert precheck.changes(held, held, planned, built=built) == []
    assert precheck.changes(held, held, planned, basis=planned, built=built) == []


@given(before=shapes(), after=shapes(), built=BUILT)
def test_an_unplanned_column_is_never_breaking(
    before: dict[str, Any], after: dict[str, Any], built: Any
) -> None:
    """Whatever becomes of the columns a run does not read, only its key can break its plan."""
    found = precheck.changes(before, after, {"id": "key"}, built=built)
    assert not [change for change in found if change["breaking"]]
    assert {change["kind"] for change in found} <= set(precheck.OTHER)
    assert jsonio.dumps(found)  # what the guard holds is JSON


# -- every text (§10) --------------------------------------------------------------------------------------


def test_messages_golden() -> None:
    # A name without a pair.
    assert precheck.unpaired("a", ["APP.CUSTOMERS"], "customers") == (
        "no table on side b is named customers (side a: APP.CUSTOMERS; paired by name)"
    )
    assert precheck.unpaired("a", ["Customers", "CUSTOMERS"], "customers") == (
        "two tables of side a are named alike ignoring case (Customers, CUSTOMERS): compare them in a spec"
    )
    assert precheck.unpaired("b", ["t", "T", "\uff54"], "t").startswith("3 tables of side b are named alike")
    assert precheck.CANCELLED == "not checked: the pre-check was cancelled"
    assert precheck.EVALUATION == "Unlicensed evaluation"
    assert (
        precheck.stalled(10) == "no worker has taken this pre-check for 10 min: start ds worker, or ds serve"
    )
    counted = dict(zip(precheck.VERDICTS, (1, 1, 1, 2, 1, 1, 1, 1, 1, 2), strict=True))
    assert precheck.totals(counted) == (
        "12 pairs: 1 unpaired, 1 a run would refuse, 1 not read, 2 with differing row counts, 1 with null or"
        " repeated keys, 1 with columns not compared, 1 with differing figures, 1 not fully checked, 1 with"
        " schema differences only, 2 with no difference observed (not parity)"
    )
    assert precheck.totals({"refused": 1, "no_difference_observed": 6}, pending=3, cancelled=2) == (
        "12 pairs: 1 a run would refuse, 6 with no difference observed (not parity); 3 pending, 2 cancelled"
    )
    assert precheck.totals({}, pending=1) == "1 pair: 1 pending"
    # A run of the spec would be refused: the plan's own message, and how many more.
    oracle = relation(col("EMAIL", "VARCHAR2(120 CHAR)", "text"), name="APP.CUSTOMERS", server="19.21")
    mysql = relation(
        col("email", "varchar(120)", "text"), name="ledger.customers", server="8.4", consistency="unverified"
    )
    sides = {"a": read(None, engine="oracle"), "b": read(None, "2026-10-07T10:02:14.000000Z", "mysql")}
    relations = {"a": oracle, "b": mysql}
    said = "side a: column 'x' is not in APP.CUSTOMERS"
    one = precheck.compare("columns", "schema", SPEC, None, relations, sides, [said])
    every_pair = (
        "side a: oracle 19.21, APP.CUSTOMERS, in 1 snapshot at 2026-10-07 10:02:11 UTC; side b: mysql 8.4,"
        " ledger.customers, in 1 transaction, a snapshot not observable (a MySQL view), 3 s later: not"
        " read at one moment"
    )
    assert one.result["lines"] == [f"a run of this spec would be refused: {said}", every_pair]
    assert (one.verdict, one.result["apart_s"], one.result["refused"]) == ("refused", 3, [said])
    many = precheck.compare("columns", "schema", SPEC, None, relations, sides, [said] * 12)
    assert many.line == f"a run of this spec would be refused: {said} (and 11 more)"
    assert len(many.result["refused"]) == 10
    earlier = {"a": sides["b"] | {"engine": "oracle"}, "b": sides["a"] | {"engine": "mysql"}}
    outside = relation(name="ledger.customers", server="8.4", consistency="none")
    assert precheck.compare(
        "schema", "schema", SPEC, None, {"a": oracle, "b": outside}, earlier, [said]
    ).result["lines"][1] == (
        "side a: oracle 19.21, APP.CUSTOMERS, in 1 snapshot at 2026-10-07 10:02:14 UTC; side b: mysql 8.4,"
        " ledger.customers, in 1 pass, not a snapshot (a MySQL table outside InnoDB), 3 s earlier: not"
        " read at one moment"
    )
    # Not read, one per code.
    pair = Pair(12, "customer-profile 1.4.0", targets=("crm-legacy", "ledger"))
    codes_ = {
        ("figures", "timeout", "900"): "not checked: side a's figures timed out after 900 s (raise"
        " precheck.statement_timeout_s, or pre-check a filtered view)",
        ("catalogue", "unreachable", "ORA-12541"): "not checked: side a could not be reached (ORA-12541)",
        ("catalogue", "auth_refused", "ORA-01017"): "not checked: side a refused the credential (ORA-01017);"
        " no other pair of target crm-legacy was tried",
        ("figures", "denied", "ORA-00942"): "not checked: side a's account may not read APP.CUSTOMERS or a"
        " column of it (ORA-00942)",
        ("duplicates", "snapshot_lost", "ORA-01555"): "not checked: side a's snapshot was lost (ORA-01555)",
        ("distinct", "temp_space", "53400"): "distinct values not counted: side a ran out of temporary space"
        " (53400)",
        ("duplicates", "temp_space", "ORA-01652"): "duplicate keys not counted: side a ran out of temporary"
        " space (ORA-01652)",
        ("figures", "temp_space", "MySQL 3"): "not checked: side a ran out of temporary space (MySQL 3)",
        ("figures", "error", "TargetRefused: MySQL 1064"): "not checked: side a's read failed (TargetRefused:"
        " MySQL 1064)",
        ("figures", "error", "TypeError"): "not checked: side a's read failed (TypeError)",
        ("distinct", "timeout", "900"): "not checked: side a's distinct values timed out after 900 s (raise"
        " precheck.statement_timeout_s, or pre-check a filtered view)",
    }
    for (check, code, shown), text in codes_.items():
        unread = sides | {"a": read(None, engine="oracle", failed={check: (code, shown)})}
        found = precheck.compare("distinct", "distinct", pair, None, relations, unread)
        assert (found.verdict, found.line, found.result["sides"]["a"]["failed"]) == (
            "not_read",
            text,
            {check: code},
        )
    unreached = {"a": {"failed": {"catalogue": ("unreachable", "OperationalError")}}, "b": sides["b"]}
    found = precheck.compare("counts", "counts", pair, None, {"a": None, "b": mysql}, unreached)
    assert found.result["lines"] == [
        "not checked: side a could not be reached (OperationalError)",
        "side a: not read; side b: mysql 8.4, ledger.customers, in 1 transaction, a snapshot not"
        " observable (a MySQL view) at 2026-10-07 10:02:14 UTC: not read at one moment",
    ]
    assert found.result["sides"]["a"] == {
        "engine": None, "at": None, "server": None, "consistency": None, "relation": None, "kind": None,
        "sha256": None, "failed": {"catalogue": "unreachable"},
    }  # fmt: skip
    # Rows and keys.
    email = col("email", "text")
    a = relation(email)
    counts_ = (
        read(1_204_331, null_keys=0, duplicate_keys=3, rows_in_duplicates=7),
        read(1_204_298, null_keys=12, duplicate_keys=0, rows_in_duplicates=0),
    )
    found = judged(a, a, counts_, level="counts")
    assert found.result["lines"][:3] == [
        "rows differ: side a 1,204,331, side b 1,204,298 (33 fewer on side b)",
        "keys: 3 key(s) of side a are held by more than one row (7 rows): per key they read"
        " identity_ambiguous",
        "keys: 12 row(s) of side b have no key (NULL, or its key map gives none): a scan does not compare"
        " them",
    ]
    assert (found.verdict, found.differing, found.result["keys"]["b"]) == (
        "rows_differ",
        1,
        {"rows": 1_204_298, "null_keys": 12, "duplicate_keys": 0, "rows_in_duplicates": 0, "primary": ["id"]},
    )
    unkeyed = judged(a, a, (read(), read()), level="counts", a={"keys": []}, b={"keys": []})
    assert unkeyed.result["lines"] == [
        BEST.format("counts"),
        "keys: not checked: no common primary key",
        READ,
    ]
    # Columns a comparison would leave out.
    left = relation(
        col("legacy_flag", "CHAR(1)", "char"),
        col("OPENED", "DATE", "timestamp"),
        col("GEOM", "SDO_GEOMETRY", ""),
        name="APP.CUSTOMERS",
    )
    right = relation(col("opened", "date"), col("geom", "text"))
    found = judged(left, right, (read(), read()), level="schema")
    assert found.line == (
        "3 columns would not be compared: opened is a timestamp (DATE) on side a and a date on side b:"
        " declare typeMap {OPENED: {as: date}} on side a, or {as: timestamp} on both; geom (SDO_GEOMETRY)"
        " cannot be compared: compare a view that casts it; legacy_flag is on side a only"
    )
    # A sum that neither engine can have rounded, a withheld one, and the others of test_sum_*.
    # Skipped by a limit, or by an unapproved filter.
    wide = relation(*(col(f"c{n}", "text") for n in range(214)))
    figures = {f"c{n}": {"nulls": "0", "empty": "0", "shortest": "1", "longest": "2"} for n in range(200)}
    found = judged(wide, wide, (read(**figures), read(**figures)))
    assert (found.verdict, found.result["skipped"], len(found.result["columns"])) == (
        "incomplete",
        ["columns"],
        200,
    )
    assert found.line == "not compared: 214 columns are compared and the first 200 were read"
    assert len(precheck.wanted("columns", paired(wide, wide), {"a": wide, "b": wide})["a"]) == 2 + 4 * 200
    some = relation(*(col(f"c{n}", "text") for n in range(52)), col("score", "real", "float32"))
    figures = {f"c{n}": {"nulls": "0", "empty": "0", "shortest": "1", "longest": "2"} for n in range(52)} | {
        "score": {"nulls": "0"}
    }
    counted_ = {name: found_ | {"distinct": "2"} for name, found_ in list(figures.items())[:40]}
    found = judged(some, some, (read(**figures | counted_), read(**figures | counted_)), level="distinct")
    assert found.line == "distinct values not counted for 12 of 52 compared columns: the first 40 were read"
    assert (
        found.result["skipped"] == ["distinct_columns"]
        and "distinct" not in found.result["columns"]["c40"]["figures"]
    )
    wanted = precheck.wanted("distinct", paired(some, some), {"a": some, "b": some})["a"]
    assert [c for f, c in wanted if f == "distinct"] == [f"c{n}" for n in range(40)]
    over = read(
        61_204_118,
        email={"nulls": "0", "empty": "0", "shortest": "1", "longest": "2"},
        skipped=["distinct_rows"],
    )
    under = read(
        61_204_118,
        email={"nulls": "0", "empty": "0", "shortest": "1", "longest": "2", "distinct": "9"},
        duplicate_keys=0,
        rows_in_duplicates=0,
    )
    found = judged(a, a, (over, under), level="distinct")
    over_ = "61,204,118 rows on side a is over precheck.distinct_max_rows (20,000,000)"
    assert found.line == f"duplicate keys and distinct values not counted: {over_}"  # both sort or hash
    assert (found.verdict, found.result["skipped"]) == ("incomplete", ["distinct_rows"])
    assert found.result["keys"]["a"]["duplicate_keys"] is None  # not counted: never read as none
    assert (
        lines(found, "email")[-1]
        == "distinct: not checked (side a holds over precheck.distinct_max_rows rows)"
    )
    unkeyed_ = judged(a, a, (over, under), level="distinct", a={"keys": []}, b={"keys": []})
    assert unkeyed_.line == f"distinct values not counted: {over_}"
    floats = relation(col("score", "real", "float32"))  # a key, and no column whose distinct values count
    score = {"nulls": "0"}
    over, under = read(61_204_118, score=score, skipped=["distinct_rows"]), read(61_204_118, score=score)
    assert judged(floats, floats, (over, under), level="distinct").line == (
        f"duplicate keys not counted: {over_}"
    )
    found = judged(
        a, a, (read(None), read(None)), level="schema", asked="columns", a={"where": "\"email\" LIKE 'a%'"}
    )
    assert found.line == (
        "not checked: spec 12 (customer-profile 1.4.0) declares a filter and is not approved: only its schema"
        " was read"
    )
    assert (found.verdict, found.result["level"], found.result["skipped"]) == (
        "incomplete",
        "schema",
        ["unapproved_filter"],
    )
    # The schema alone.
    amount = col("amount", "NUMBER(10,2)", "decimal", size=("digits", 10, 2))
    schema_a = relation(amount, col("opened", "date", nullable=False), name="APP.CUSTOMERS")
    schema_b = relation(
        col("amount", "numeric(12,4)", "decimal", size=("digits", 12, 4)), col("opened", "date")
    )
    found = judged(schema_a, schema_b, (read(None), read(None)), level="schema")
    assert found.verdict == "schema_differs" and found.line == (
        "schema: 2 differences: amount is NUMBER(10,2) on side a and numeric(12,4) on side b; opened is"
        " NOT NULL on side a and nullable on side b"
    )
    # What a pre-check does not apply, when the spec holds any.
    ruled = Pair(12, "customer-profile 1.4.0", True, unapplied=True)
    assert judged(a, a, (read(), read()), level="counts", pair=ruled).result["lines"] == [
        BEST.format("counts"),
        "not applied: this spec's normalisers, tolerances, known-difference rules and compare.include or"
        " exclude; a comparison applies them",
        READ,
    ]
    # A name is cut at 128 characters, and a list after ten names.
    long = relation(*(col(f"{n:02}" + "x" * 140, "text") for n in range(12)))
    found = judged(long, relation(), (read(None), read(None)), level="schema")
    clauses = found.line.split(": ", 1)[1].split("; ")
    assert found.line.startswith("12 columns would not be compared: 00xxx") and clauses[-1] == "and 2 more"
    assert len(clauses) == 11 and clauses[0] == "00" + "x" * 125 + "… is on side a only"


def test_a_verdict_as_words_never_reads_as_agreeing() -> None:
    assert [precheck.worded(verdict) for verdict in precheck.VERDICTS] == [
        "unpaired", "refused", "not read", "rows differ", "keys null or repeated", "columns not compared",
        "values differ", "incomplete", "schema differs",
        "no difference observed in the checks made (not parity)",
    ]  # fmt: skip


def test_a_stored_result_s_figures_are_listed_in_one_order() -> None:
    """The store keeps a result's members in an order of its own (jsonb: by length, then by name): a
    column's lines come in ``ORDER`` whatever order its figures are held in."""
    a = relation(*CLASSES)
    same = judged(a, a, (read(**FIGURES), read(**FIGURES)), level="distinct")
    figures = {figure for found in same.result["columns"].values() for figure in found["figures"]}
    assert figures == set(precheck.ORDER)
    moved = 0
    for column, found in same.result["columns"].items():
        as_stored = dict(sorted(found["figures"].items(), key=lambda held: (len(held[0]), held[0])))
        moved += list(as_stored) != list(found["figures"])
        assert precheck.column_lines(found | {"figures": as_stored}) == lines(same, column)
    assert moved >= 3  # a text's, a number's and a date's figures are stored in another order


@pytest.mark.parametrize("word", ["match", "identical", "passed", "verified", "validated", "reconciled", "✓"])
def test_no_text_of_the_module_says_what_is_never_told(word: str) -> None:
    """N1: none of the words a pre-check may not say is in a text the module builds."""
    tree = ast.parse(inspect.getsource(precheck))
    texts = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.ClassDef | ast.Module)
    }
    said = [text for text in texts if text not in docstrings]
    assert not [text for text in said if re.search(rf"\b{word}", text, re.I)]


# -- two relations paired by name (§3) ---------------------------------------------------------------------

ORACLE_FACTS = {"kind": "table", "server": "23.26", "consistency": "snapshot"}
POSTGRES_FACTS = {"kind": "table", "server": "16.4", "consistency": "snapshot"}


def test_implied_with_and_without_a_common_primary_key() -> None:
    """The plan of two relations paired by name: the primary key when both catalogues show one over the same
    lower-cased columns (side b's in side a's order), every other name both sides read under its lower-cased
    name, no filter and no typeMap; a name one side alone reads is its ``only``."""
    a_columns = (
        col("REGION", "VARCHAR2(4)", "text"), col("ID", "NUMBER(10)", "integer"),
        col("AMOUNT", "NUMBER(12,2)", "decimal"), col("OPENED", "DATE", "timestamp"),
        col("SHAPE", "SDO_GEOMETRY", ""), col("LEGACY", "CHAR(1)", "char"),
        Column("SECRET", "VARCHAR2(9)", "text", False, True),
    )  # fmt: skip
    b_columns = (
        col("id", "integer"), col("region", "text"), col("amount", "numeric(12,2)", "decimal"),
        col("opened", "date"), col("shape", "geometry", "other"), col("secret", "text"), col("note", "text"),
    )  # fmt: skip
    a = Relation("APP", "ORDERS", a_columns, ORACLE_FACTS | {"key": ["REGION", "ID"]})
    b = Relation("public", "orders", b_columns, POSTGRES_FACTS | {"key": ["id", "region"]})
    plan = precheck.implied(a, b, ("oracle", "postgresql"))
    assert plan["a"] | {"classes": None, "declared": None} == {
        "schema": "APP", "name": "ORDERS", "keys": ["REGION", "ID"], "key_maps": [None, None],
        "columns": {"amount": "AMOUNT", "opened": "OPENED", "shape": "SHAPE"}, "where": None, "types": {},
        "digest": [], "engine": "oracle", "classes": None, "declared": None,
    }  # fmt: skip
    assert (plan["b"]["keys"], plan["b"]["columns"]["opened"]) == (["region", "id"], "opened")
    assert plan["a"]["classes"] == {
        "REGION": "text", "ID": "integer", "AMOUNT": "decimal", "OPENED": "timestamp", "SHAPE": None,
    }  # fmt: skip
    # A column the account may not read is on the other side only; two families or a type no plan reads
    # are compared by no figure, and say so.
    assert (plan["only_a"], plan["only_b"]) == (["legacy"], ["secret", "note"])
    found = precheck.compare(
        "columns", "columns", Pair(None, "orders", targets=("crm", "pg")), plan, {"a": a, "b": b},
        {"a": read(engine="oracle", AMOUNT={}), "b": read()},
    )  # fmt: skip
    assert found.verdict == "columns_not_compared" and found.line == (
        "5 columns would not be compared: opened is a timestamp (DATE) on side a and a date on side b:"
        " declare typeMap {OPENED: {as: date}} on side a, or {as: timestamp} on both; shape (SDO_GEOMETRY)"
        " cannot be compared: compare a view that casts it; legacy is on side a only; secret is on side b"
        " only; note is on side b only"
    )
    assert found.result["names"]["a"] == {
        "schema": "APP", "name": "ORDERS", "keys": ["REGION", "ID"],
        "columns": {"amount": "AMOUNT", "opened": "OPENED", "shape": "SHAPE"},
    }  # fmt: skip
    assert precheck.wanted("columns", plan, {"a": a, "b": b})["a"] == [
        ("rows", None), ("null_keys", None), ("nulls", "amount"), ("sum", "amount"), ("min", "amount"),
        ("max", "amount"),
    ]  # fmt: skip
    # Without a common primary key there is none: every name both sides read is a column.
    b_keyed = Relation("public", "orders", b_columns, POSTGRES_FACTS | {"key": ["id"]})
    keyless = precheck.implied(a, b_keyed, ("oracle", "postgresql"))
    assert (keyless["a"]["keys"], keyless["b"]["key_maps"]) == ([], [])
    assert list(keyless["a"]["columns"]) == ["region", "id", "amount", "opened", "shape"]
    found = precheck.compare(
        "counts", "counts", Pair(None, "orders"), keyless, {"a": a, "b": b_keyed},
        {"a": read(engine="oracle"), "b": read()},
    )  # fmt: skip
    assert "keys: not checked: no common primary key" in found.result["lines"]
    assert "key: the primary key is (REGION, ID) on side a and (id) on side b" in found.result["lines"]
    view = Relation("public", "orders", b_columns, POSTGRES_FACTS | {"kind": "view", "key": None})
    assert precheck.implied(a, view, ("oracle", "postgresql"))["a"]["keys"] == []


def test_columns_named_alike_ignoring_case_on_a_side_are_reported_never_dropped() -> None:
    """Two columns of one side that compare as one lower-cased name: neither is compared, both are reported
    as a comparison would leave them out, and the other side's column of that name is not called its
    side's alone."""
    amount = col("amount", "numeric(10,2)", "decimal", size=("digits", 10, 2))
    a = Relation(
        "sa",
        "u",
        (col("id", "integer"), col("Email", "text"), col("email", "text"), amount),
        POSTGRES_FACTS | {"key": ["id"]},
    )
    b = Relation(
        "sb", "u", (col("id", "integer"), col("email", "text"), amount), POSTGRES_FACTS | {"key": ["id"]}
    )
    plan = precheck.implied(a, b, ("postgresql", "postgresql"))
    assert (plan["a"]["columns"], plan["only_a"], plan["only_b"]) == ({"amount": "amount"}, [], [])
    assert (plan["alike_a"], plan["alike_b"]) == (["Email", "email"], [])
    figures = {"nulls": "0", "sum": "3.00", "min": "1.00", "max": "2.00"}
    found = precheck.compare(
        "columns", "columns", Pair(None, "u"), plan, {"a": a, "b": b},
        {"a": read(2, amount=figures), "b": read(2, amount=figures)},
    )  # fmt: skip
    alike = "and another column of side a both compare as email: list each in a spec under a name of its own"
    assert (found.verdict, found.line) == (
        "columns_not_compared",
        f"2 columns would not be compared: Email {alike}; email {alike}",
    )
    assert codes(found.result["schema"]) == [
        ("alike_a", "Email", "not_compared"),
        ("alike_a", "email", "not_compared"),
    ]
    assert found.result["schema"][0]["a"] == {"declared": "text", "cls": "text", "nullable": True}
    assert found.result["schema"][0]["b"] is None


def test_a_name_without_a_pair_is_judged_as_it_is_made() -> None:
    line = precheck.unpaired("a", ["APP.CUSTOMERS"], "customers")
    found = precheck.alone("counts", line)
    assert (found.verdict, found.rank, found.differing, found.line) == ("unpaired", 1, 0, line)
    assert found.result["lines"] == [line] and found.result["sides"]["a"]["relation"] is None
    # A pair of two tables a run would be refused for reads as these tables', not a spec's.
    refused = precheck.compare(
        "counts", "counts", Pair(None, "orders"), None, {}, {}, ["side a: table or view orders not found"]
    )
    assert refused.line == "a run of these tables would be refused: side a: table or view orders not found"
