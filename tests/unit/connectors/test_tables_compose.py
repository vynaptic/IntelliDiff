"""The statements of a MySQL and of an Oracle table side: their text, pinned
by a golden file each, and that whatever a key, a filter literal or a key map's prefix holds, a MySQL
statement holds only grammar and an Oracle statement holds no value at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from deltasleuth.connectors.tables import Plan, Refusal, compose, duck, encode_key, oracle, side_values
from deltasleuth.connectors.tables.mysql import PRIMITIVES
from deltasleuth.core.spec import where
from tests.unit.connectors.ducked import locked, unrestricted

GOLDEN = json.loads((Path(__file__).parents[2] / "golden" / "engines" / "mysql_pair.json").read_text())
PLAIN, MAPPED = GOLDEN["plain"], GOLDEN["mapped"]  # one key; a composite key, a key map, a filter, strata
# In a pair of one engine the worker hashes only these of ``MAPPED``'s columns (a mapped one, the large
# ones) and none of ``PLAIN``'s: the engine hashes the others.
WORKER = ["doc", "note", "opened"]
# Every value a statement may hold: a number, text as the hexadecimal digits of its UTF-8, or bytes.
VALUE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?|_utf8mb4 X'(?:[0-9a-f]{2})*'|X'(?:[0-9a-fA-F]{2})*'")
HEX = re.compile(r"X'[0-9a-fA-F]*'")
hostile = st.text(max_size=40) | st.sampled_from(
    [
        "'",
        "\\",
        "`",
        "%s",
        "%(k)s",
        "';--",
        "' OR '1'='1",
        "\x00",
        "X'41'",
        "*/",
        "/*",
        "\n",
        "\U0001f600",
        "1e999999999999999999999999999999",  # an exponent Decimal cannot hold
    ]
)
# Texts of a number's form, among them exponents of any length.
numeric = st.from_regex(
    r"-?(?:[0-9]{1,3}(?:\.[0-9]{0,3})?|\.[0-9]{1,3})(?:[eE][+-]?[0-9]{1,40})?", fullmatch=True
)


def _sent(side: Plan, keys: list[str], after: list[bytes], worker: list[str]) -> dict[str, str]:
    """Each statement of ``side``, by its read's name; ``scan_hashed`` is the scan of a pair of one engine,
    whose worker hashes the columns ``worker``."""
    asked = {key: side_values(key, side) for key in keys}
    return {
        "rows": compose.rows(PRIMITIVES, side, asked)[0],  # type: ignore[arg-type]
        "scan": compose.scan(PRIMITIVES, side, None)[0],
        "scan_after": compose.scan(PRIMITIVES, side, after)[0],
        "scan_hashed": compose.scan(PRIMITIVES, side | {"digest": worker}, None)[0],
        "census": compose.census(PRIMITIVES, side)[0],
        "frame": compose.frame(PRIMITIVES, side)[0],
        "null_keys": compose.null_keys(PRIMITIVES, side)[0],
        "probe": compose.probe(PRIMITIVES, side)[0],
    }


def test_mysql_statements_are_the_golden_ones() -> None:
    assert _sent(PLAIN, ["7", "o'x", "1.5"], [b"41"], []) == GOLDEN["sent"]["plain"]
    keys = ["eu|A-000042", "e'u|A-000007"]
    assert _sent(MAPPED, keys, [b"eu", b"A-000041"], WORKER) == GOLDEN["sent"]["mapped"]


def test_a_statement_s_parts() -> None:
    plain, mapped = GOLDEN["sent"]["plain"], GOLDEN["sent"]["mapped"]
    assert PRIMITIVES.relation(MAPPED) == "`led``ger`.`Accounts` AS t"  # a backtick in a name is doubled
    # One arm a key: the index-friendly IN, then the key text's bytes exactly. Letters are on no integer row.
    # The selected row comes first, by the flag's select position: any name may be a column's.
    arm = "WHERE t.`id` IN (7) AND CAST(CAST(t.`id` AS CHAR) AS BINARY) = X'37' ORDER BY 5 DESC LIMIT 2)"
    assert arm in plain["rows"] and plain["rows"].count("UNION ALL") == 1 and "6f2778" not in plain["rows"]
    assert plain["rows"].startswith("(SELECT 0, ") and ") UNION ALL (SELECT 2, " in plain["rows"]
    assert " AS s" not in plain["rows"] + mapped["rows"] and "ORDER BY 15 DESC LIMIT 2)" in mapped["rows"]
    order = "CAST(CAST(t.`id` AS CHAR) AS BINARY)"
    assert plain["scan_after"].endswith(f" AND ({order}) > (X'3431') ORDER BY {order}")
    # A composite bound compares rows of order expressions; a ZEROFILL key reads through UNSIGNED.
    assert "END AS BINARY)) > (X'6575', X'412d303030303431') ORDER BY " in mapped["scan_after"]
    assert "t.`branch` IN (_utf8mb4 X'652775') AND t.`No` IN (7) AND " in mapped["rows"]
    digits = "CAST(CAST(t.`No` AS UNSIGNED) AS CHAR)"
    assert (
        f"CASE WHEN {digits} <> '' AND {digits} NOT REGEXP '[^0-9]' THEN CONCAT(_utf8mb4 X'412d', "
        in (mapped["census"])
    )  # PostgreSQL's ~ '^[0-9]+$', written without $
    assert "t.`rate` BETWEEN 1 AND 2.5 OR t.`note` NOT LIKE _utf8mb4 X'785c5f25'" in mapped["scan"]
    assert mapped["probe"].endswith(" LIMIT 0)") and plain["null_keys"].startswith("SELECT COUNT(*) FROM ")
    assert mapped["frame"].endswith(  # ties broken by the strata's rendered texts, in binary order
        ", CAST(CONVERT(t.`branch` USING utf8mb4) AS BINARY), CAST(CONVERT(t.`st` USING utf8mb4) AS BINARY)"
    )
    # A row whose LOB values pass 16 MiB comes with them NULL and a flag, in every statement that reads them.
    big = "(COALESCE(LENGTH(t.`note`), 0) + COALESCE(LENGTH(t.`doc`), 0) > 16777216)"
    assert f"CASE WHEN {big} THEN NULL ELSE CAST(t.`doc` AS CHAR) END" in mapped["rows"]
    assert f", CASE WHEN {big} THEN 1 ELSE 0 END, CONVERT(t.`branch` USING utf8mb4), " in mapped["rows"]
    # In one engine the server hashes the columns both sides declare alike, as one JSON array of their
    # rendered texts, bytes as hexadecimal digits; without a worker column it sends keys and 32 bytes.
    hashed = "UNHEX(SHA2(JSON_ARRAY(CAST(t.`amount` AS CHAR), CONVERT(t.`email` USING utf8mb4)), 256))"
    assert plain["scan_hashed"].split(", ", 1)[1].startswith(hashed + " FROM `ledger`.")
    assert (
        ", UNHEX(SHA2(JSON_ARRAY(CAST(t.`at` AS CHAR), HEX(t.`raw`), CAST(t.`day` AS CHAR), "
        "CAST(CAST(t.`flags` AS UNSIGNED) AS CHAR), CAST(CAST(t.`rate` AS JSON) AS CHAR), "
        "DATE_FORMAT(t.`seen`, '%Y-%m-%dT%H:%i:%s.%f'), CONVERT(t.`st` USING utf8mb4)), 256)), CASE WHEN "
    ) in mapped["scan_hashed"]  # then the worker's columns: the mapped one and the large ones
    # A float is rendered through JSON in every statement, the hash included: a cast to CHAR is 22
    # characters wide, which a UNION and a temporary table cut a longer text to.
    assert ", CAST(CAST(t.`rate` AS JSON) AS CHAR), " in mapped["rows"]
    assert "DOUBLE" not in str(GOLDEN["sent"])
    assert PRIMITIVES.render("d", "float", "double") == "CAST(CAST(t.`d` AS JSON) AS CHAR)"
    assert "SHA2" not in mapped["scan"] + mapped["rows"] + mapped["census"] + mapped["frame"]
    # The probe parses the hash too, so a hash its server refuses is refused when the run is created.
    assert hashed in compose.probe(PRIMITIVES, PLAIN | {"digest": []})[0]


def test_a_scanned_row_s_digest_is_the_engine_s_hash_extended_by_the_worker() -> None:
    import hashlib

    from deltasleuth.connectors.tables import render

    plain = hashlib.sha256(b"engine").digest()
    # Without a worker column the digest is the engine's hash; a hash the engine could not hold is a marker.
    hashed = compose.digested(PRIMITIVES, PLAIN | {"digest": []})
    assert hashed([("7", plain), ("8", None)]) == [("7", plain), ("8", render.RESOURCE_LIMIT)]
    # With worker columns it is the hash extended with their converted values, PostgreSQL's shape.
    mixed = compose.digested(PRIMITIVES, PLAIN | {"digest": ["amount"]})
    expected = hashlib.sha256(plain + b'{"amount": 1.5}').digest()
    assert mixed([("7", plain, "1.50"), ("8", plain, "x"), ("9", None, "1.50")]) == [
        ("7", expected),
        ("8", render.UNCONVERTIBLE),
        ("9", render.RESOURCE_LIMIT),
    ]
    # Across engines the worker hashes every column: no hash comes, and the empty row's stands in its place.
    worker = compose.digested(PRIMITIVES, PLAIN)
    both = hashlib.sha256(render.EMPTY + b'{"amount": 1.5, "email": "a@x"}').digest()
    assert worker([("7", "1.50", "a@x")]) == [("7", both)]


@pytest.mark.parametrize(
    ("cls", "text", "written"),
    [
        ("integer", "42", "42"),
        ("integer", "007", "007"),  # as written: the server compares numbers, the key text tells them apart
        ("decimal", "1.50", "1.50"),  # the optimiser reads the key's text from this literal
        ("decimal", "-0.50", "-0.50"),
        ("float", "1e3", "1000"),
        ("decimal", ".5", "0.5"),
        ("integer", "4 2", None),
        ("integer", "1e999999", None),
        ("integer", "1e999999999999999999999999999999", None),  # an exponent Decimal cannot hold
        ("decimal", "-0.5E-999999999999999999999999999999", None),
        ("integer", "0x10", None),
        ("float32", "NaN", None),
        ("text", "o'x", "_utf8mb4 X'6f2778'"),
        ("char", "", "_utf8mb4 X''"),
        ("json", "{}", "_utf8mb4 X'7b7d'"),
        ("bytes", "\\x00FF", "X'00FF'"),
        ("bytes", "00ff", None),
        ("bytes", "\\x0", None),
        ("date", "2024-02-29", "_utf8mb4 X'323032342d30322d3239'"),
        ("date", "today", None),
        ("timestamp", "2024-02-31 00:00:00", "_utf8mb4 X'323032342d30322d33312030303a30303a3030'"),
        ("instant", "2024-02-29T10:00:00.5", "_utf8mb4 X'323032342d30322d32395431303a30303a30302e35'"),
        ("timestamp", "2024-02-29 10:00", None),
        ("time", "-838:59:59", "_utf8mb4 X'2d3833383a35393a3539'"),
        ("time", "noon", None),
    ],
)
def test_a_literal_is_typed_by_its_column_s_class(cls: str, text: str, written: str | None) -> None:
    assert PRIMITIVES.literal(text, cls, "", {}) == written


def _filtered(literal: where.Value, column: str = "note") -> Plan:
    node = where.Node("cmp", column=column, test="=", values=(literal,))
    return MAPPED | {"where": where.text(node)}


@pytest.mark.parametrize(
    ("literal", "column", "message"),
    [
        (
            where.Value("str", "abc"),
            "rate",
            "the filter compares column 'rate' (float) with a literal that is not a number",
        ),
        (
            where.Value("str", "soon"),
            "day",
            "the filter compares column 'day' (date) with a literal that is not a date",
        ),
        (
            where.Value("num", "5"),
            "at",
            "the filter compares column 'at' (time(3)) with a literal that is not a time",
        ),
        (
            where.Value("str", "ff"),
            "raw",
            "the filter compares column 'raw' (varbinary(16)) with a literal that is not bytes (\\x and"
            " hexadecimal digits)",
        ),
        (
            where.Value("bool", "true"),
            "flags",
            "the filter tests column 'flags' (bit(8)) as a boolean: compare it with 1 or 0",
        ),
    ],
)
def test_a_filter_literal_not_of_its_column_s_class_is_refused(
    literal: where.Value, column: str, message: str
) -> None:
    with pytest.raises(Refusal) as exc:  # the server's zero-row probe would accept it
        compose.probe(PRIMITIVES, _filtered(literal, column))
    assert str(exc.value) == message


def test_a_filter_number_whose_exponent_no_decimal_holds_is_refused() -> None:
    # Refused on both engines, not an arithmetic error out of the probe at run creation.
    huge = where.Value("num", "1e999999999999999999999999999999")
    with pytest.raises(Refusal) as exc:
        compose.probe(PRIMITIVES, _filtered(huge, "rate"))
    assert str(exc.value) == "the filter compares column 'rate' (float) with a literal that is not a number"
    node = where.Node("cmp", column="AMT", test=">", values=(huge,))
    with pytest.raises(Refusal) as exc:
        compose.probe(BOUND, O_MAPPED | {"where": where.text(node)})
    assert str(exc.value) == (
        "the filter compares column 'AMT' (NUMBER(10,2)) with a literal that is not a number"
    )


def test_what_is_refused_when_the_spec_is_loaded_is_never_composed() -> None:
    regex = {"kind": "regex", "toCanonical": {"pattern": "a", "replacement": "b"}}
    with pytest.raises(Refusal, match="a regex key map is not read on this engine"):
        compose.census(PRIMITIVES, PLAIN | {"key_maps": [regex]})
    with pytest.raises(Refusal, match="ILIKE is not read on this engine: use LIKE"):
        compose.scan(PRIMITIVES, PLAIN | {"where": "\"email\" ILIKE 'a%'"}, None)
    lob = MAPPED | {"strata": {"note": "note"}}
    with pytest.raises(Refusal) as exc:
        compose.frame(PRIMITIVES, lob)
    assert str(exc.value) == "strata column 'note' (longtext) cannot stratify a frame"
    with pytest.raises(Refusal, match="strata column 'doc' \\(json\\) cannot stratify a frame"):
        compose.probe(PRIMITIVES, MAPPED | {"strata": {"d": "doc"}})


def _skeleton(statement: str) -> str:
    """A statement without what its hexadecimal literals hold."""
    return HEX.sub("X''", statement)


@given(key=hostile, literal=hostile, prefix=hostile, bound=st.binary(max_size=40))
def test_mysql_statements_hold_only_grammar(key: str, literal: str, prefix: str, bound: bytes) -> None:
    # A text key, a filter's text literal, a key map's prefix and a resume bound: whatever they hold, each
    # statement is the one a harmless value gives, with other hexadecimal digits.
    def side(value: str, literal: str) -> Plan:
        key_map = {"kind": "prefix_pad", "prefix": value, "width": 6, "sideType": "integer"}
        return _filtered(where.Value("str", literal)) | {"key_maps": [None, key_map]}

    try:
        asked = {encode_key([key, f"{prefix}000042"]): [[key], ["42"]]}
        found = [
            compose.rows(PRIMITIVES, side(prefix, literal), asked)[0],
            compose.scan(PRIMITIVES, side(prefix, literal), [bound, key.encode()])[0],
            compose.null_keys(PRIMITIVES, side(prefix, literal))[0],
        ]
    except UnicodeEncodeError:  # a lone surrogate has no UTF-8: the key is unfit, nothing is sent
        return
    harmless = [
        compose.rows(PRIMITIVES, side("A-", "ok"), {"eu|A-000042": [["eu"], ["42"]]})[0],
        compose.scan(PRIMITIVES, side("A-", "ok"), [b"eu", b"A-1"])[0],
        compose.null_keys(PRIMITIVES, side("A-", "ok"))[0],
    ]
    assert [_skeleton(statement) for statement in found] == [_skeleton(statement) for statement in harmless]
    for statement in found:
        assert statement.isascii() and statement.isprintable()


@given(
    text=hostile | numeric,
    cls=st.sampled_from(["integer", "decimal", "float", "text", "char", "bytes", "date", "time"]),
)
@example(text="1e999999999999999999999999999999", cls="integer")
@example(text="-.5E-999999999999999999999999999999", cls="float")
def test_a_mysql_value_is_a_number_or_hexadecimal_digits(text: str, cls: str) -> None:
    try:  # nothing else is raised: a number no column holds is no value, so its key is on no row
        written = PRIMITIVES.literal(text, cls, "", {})
    except UnicodeEncodeError:
        return
    assert written is None or VALUE.fullmatch(written)


def test_an_identifier_is_quoted_whatever_it_holds() -> None:
    names: list[Any] = ["a`b", "`; DROP TABLE x; --", "%s", "a b", "é"]
    assert [PRIMITIVES.ident(name) for name in names] == [
        "`a``b`", "```; DROP TABLE x; --`", "`%s`", "`a b`", "`é`",
    ]  # fmt: skip


# --- Oracle: the same builders over its primitives, every value a bind variable -------------------------

ORACLE = json.loads((Path(__file__).parents[2] / "golden" / "engines" / "oracle_pair.json").read_text())
O_PLAIN, O_MAPPED = ORACLE["plain"], ORACLE["mapped"]  # one key; three key parts, a key map, a filter, strata
BOUND = oracle.PRIMITIVES
# Every value a statement may hold: a bind variable, in a conversion of fixed text when its class has one.
BIND = re.compile(r"(?:[A-Z_]+\()?:b0(?:, '[A-Z0-9 :.-]+'\)| AS N?CHAR\([0-9]+\)\)|\))?|TRUE|FALSE")


def _bound(side: Plan, keys: list[str], after: list[bytes], worker: list[str]) -> dict[str, list[Any]]:
    """Each statement of ``side`` and the values it binds, by its read's name."""
    asked = {key: side_values(key, side) for key in keys}
    return {
        "rows": list(compose.rows(BOUND, side, asked)),  # type: ignore[arg-type]
        "scan": list(compose.scan(BOUND, side, None)),
        "scan_after": list(compose.scan(BOUND, side, after)),
        "scan_hashed": list(compose.scan(BOUND, side | {"digest": worker}, None)),
        "census": list(compose.census(BOUND, side)),
        "frame": list(compose.frame(BOUND, side)),
        "null_keys": list(compose.null_keys(BOUND, side)),
        "probe": list(compose.probe(BOUND, side)),
    }


def test_oracle_statements_are_the_golden_ones() -> None:
    assert _bound(O_PLAIN, ["7", "o'x", "1.5"], [b"41"], []) == ORACLE["sent"]["plain"]
    keys = ["eu|A-000042|1.50", "e'u|A-000007|-0.50", "|A-000001|2.00", "us|A-000003|2.00"]
    assert _bound(O_MAPPED, keys, [b"eu", b"", b"1.50"], WORKER) == ORACLE["sent"]["mapped"]


def test_an_oracle_statement_s_parts() -> None:
    plain = {name: sent[0] for name, sent in ORACLE["sent"]["plain"].items()}
    mapped = {name: sent[0] for name, sent in ORACLE["sent"]["mapped"].items()}
    rows, binds = ORACLE["sent"]["mapped"]["rows"]
    assert BOUND.relation(O_MAPPED) == '"APP"."Accounts" t'  # AS is refused there (ORA-03048)
    # One arm a key: the index-friendly IN of a typed bind, then the key text exactly.
    arm = (
        'WHERE t."ID" IN (TO_NUMBER(:b0)) AND TO_CHAR(t."ID", \'TM9\') = :b1'
        " ORDER BY 5 DESC FETCH FIRST 2 ROWS ONLY)"
    )
    assert arm in plain["rows"] and plain["rows"].startswith("(SELECT 0, ")
    assert ") UNION ALL (SELECT 2, " in plain["rows"]  # letters are on no number's row: no arm
    # Three keys have an arm (an empty part is NULL there: on no row); the last is repeated under -1,
    # so a statement has 1, 2, 4, 8, 16, 32 or 50 arms.
    assert [rows.count(f"(SELECT {i}, ") for i in (0, 1, 2, 3, -1)] == [1, 1, 0, 1, 1]
    assert rows.count(":b25") == 2 and "b31" not in binds  # the repeated arm binds nothing more
    assert [BOUND.arms(n) for n in (1, 2, 3, 5, 9, 17, 33, 50)] == [1, 2, 4, 8, 16, 32, 50, 50]
    # A CHAR key and a CHAR filter are compared with the widest CHAR; a plain bind finds nothing.
    assert 't."BRANCH" IN (CAST(:b13 AS CHAR(2000))) AND t."NO" IN (TO_NUMBER(:b14))' in rows
    assert "RTRIM(t.\"BRANCH\", ' ') = :b16 AND " in rows and binds["b16"] == binds["b13"] == "eu"
    char = "CAST(:b{} AS CHAR(2000))"
    assert f't."ST" = {char.format(1)} AND ' in rows and f't."ST" < {char.format(12)}' in rows
    assert f't."ST" IN ( {char.format(8)} , {char.format(9)} )' in rows
    assert f't."ST" BETWEEN {char.format(10)} AND {char.format(11)}' in rows
    assert "t.\"NOTE\" NOT LIKE :b4 ESCAPE '\\'" in rows and binds["b4"] == "x\\_%"  # the pattern, plainly
    assert 't."OPENED" < TO_DATE(:b7, \'YYYY-MM-DD HH24:MI:SS\') AND t."RAW" IS NOT NULL AND t."FLAG"' in rows
    # A NUMBER(10,2) key prints its scale, as PostgreSQL's; the digits test is written without a pattern.
    assert "TO_CHAR(t.\"AMT\", 'FM99999990.00') = :b18" in rows and binds["b18"] == "1.50"
    digits = "TO_CHAR(t.\"NO\", 'TM9')"
    assert (
        f"CASE WHEN LTRIM({digits}, '0123456789') IS NULL AND {digits} IS NOT NULL THEN :b0 || LPAD({digits},"
        f" GREATEST(6, LENGTH({digits})), '0') END" in mapped["census"]
    )
    # No row comparison exists there; an empty part of the bound is NULL to the server.
    branch, amount = "RTRIM(t.\"BRANCH\", ' ')", "TO_CHAR(t.\"AMT\", 'FM99999990.00')"
    assert f" AND (({branch} > :b13) OR ({branch} = :b13 AND CASE WHEN " in mapped["scan_after"]
    after = (
        f" END IS NOT NULL) OR ({branch} = :b13 AND 1 = 0 AND {amount} > :b14)) ORDER BY {branch}, CASE WHEN "
    )
    assert after in mapped["scan_after"]
    assert plain["scan_after"].endswith(
        "AND ((TO_CHAR(t.\"ID\", 'TM9') > :b0)) ORDER BY TO_CHAR(t.\"ID\", 'TM9')"
    )
    assert mapped["probe"].startswith("SELECT 1 FROM (SELECT ") and mapped["probe"].endswith(") WHERE 1 = 0")
    assert mapped["frame"].endswith(', t."BRANCH", t."ST"') and plain["null_keys"].startswith(
        "SELECT COUNT(*) "
    )
    big = '(COALESCE(LENGTH(t."NOTE"), 0) + COALESCE(LENGTH(t."DOC"), 0) > 16777216)'
    assert f'CASE WHEN {big} THEN NULL ELSE JSON_SERIALIZE(t."DOC" RETURNING CLOB) END' in rows
    assert f", CASE WHEN {big} THEN 1 ELSE 0 END, RTRIM(t.\"BRANCH\", ' '), " in rows
    # In one engine the server hashes each column both sides declare alike, 32 bytes whatever it holds,
    # and their hashes again, one column's too.
    amount, email = (
        "STANDARD_HASH(TO_CHAR(t.\"AMOUNT\", 'TM9'), 'SHA256')",
        "STANDARD_HASH(t.\"EMAIL\", 'SHA256')",
    )
    hashed = f"STANDARD_HASH({amount} || {email}, 'SHA256')"
    assert plain["scan_hashed"].split("'TM9'), ", 1)[1].startswith(hashed + ' FROM "APP".')
    one = compose.scan(BOUND, O_PLAIN | {"digest": ["amount"]}, None)[0]
    alone = f"STANDARD_HASH({email}, 'SHA256')"
    assert one.split("'TM9'), ", 1)[1].startswith(alone + ", TO_CHAR(t.\"AMOUNT\", 'TM9') FROM ")
    assert (
        "STANDARD_HASH(STANDARD_HASH(TO_CHAR(t.\"AT\", 'SYYYY-MM-DD\"T\"HH24:MI:SS.FF9'), 'SHA256') || "
        "STANDARD_HASH(t.\"RAW\", 'SHA256') || STANDARD_HASH(TO_CHAR(t.\"FLAG\"), 'SHA256') || "
    ) in mapped["scan_hashed"]
    assert "STANDARD_HASH" not in mapped["scan"] + rows + mapped["census"] + mapped["frame"]
    assert hashed in compose.probe(BOUND, O_PLAIN | {"digest": []})[0]
    # Past 60 columns their hashes no longer fit one text: blocks of 60 are hashed, and their hashes again.
    wide = {f"c{i:02d}": f"C{i:02d}" for i in range(61)}
    typed = {
        "classes": dict.fromkeys(wide.values(), "text"),
        "declared": dict.fromkeys(wide.values(), "VARCHAR2(10)"),
    }
    block = BOUND.row_hash(typed, list(wide.values()))
    assert block.count("STANDARD_HASH(") == 61 + 2 + 1 and block.count(" || ") == 59 + 1
    assert block.endswith(" || STANDARD_HASH(STANDARD_HASH(t.\"C60\", 'SHA256'), 'SHA256'), 'SHA256')")


def test_an_arm_is_ordered_by_position_whatever_its_columns_are_named() -> None:
    # A compared column and a key named S, in both cases: ordered by an alias s, Oracle refuses the
    # statement (ORA-00960), so the selected row comes first by the flag's position.
    side = {
        "schema": "APP", "name": "T", "keys": ["S"], "key_maps": [None], "columns": {"s": "s"},
        "where": '"S" IS NOT NULL', "types": {}, "digest": ["s"],
        "classes": {"S": "text", "s": "bytes"}, "declared": {"S": "VARCHAR2(10)", "s": "RAW(4)"},
    }  # fmt: skip
    statement, binds = compose.rows(BOUND, side, {"x": [["x"]]})
    assert statement == (
        '(SELECT 0, t."s", t."S", CASE WHEN (t."S" IS NOT NULL) THEN 1 ELSE 0 END FROM "APP"."T" t'
        ' WHERE t."S" IN (:b0) AND t."S" = :b1 ORDER BY 4 DESC FETCH FIRST 2 ROWS ONLY)'
    )
    assert binds == {"b0": "x", "b1": "x"}
    assert " AS s" not in str(ORACLE["sent"]) and "ORDER BY s " not in str(ORACLE["sent"])


@pytest.mark.parametrize(
    ("cls", "declared", "text", "written", "bound"),
    [
        ("integer", "NUMBER(10)", "42", "TO_NUMBER(:b0)", "42"),
        ("decimal", "NUMBER(10,2)", "1.50", "TO_NUMBER(:b0)", "1.50"),
        ("float", "BINARY_DOUBLE", "1e3", "TO_NUMBER(:b0)", "1000"),
        ("integer", "NUMBER(10)", "4 2", None, None),
        ("integer", "NUMBER(10)", "1e999999999999999999999999999999", None, None),  # no Decimal holds it
        ("float32", "BINARY_FLOAT", "NaN", None, None),
        ("text", "VARCHAR2(10)", "o'x", ":b0", "o'x"),
        ("text", "VARCHAR2(10)", "", ":b0", ""),  # NULL to the server: its own comparison
        ("char", "CHAR(3)", "ab", "CAST(:b0 AS CHAR(2000))", "ab"),
        ("char", "NCHAR(3)", "ab", "CAST(:b0 AS NCHAR(1000))", "ab"),
        ("char", "CHAR(3)", "é" * 1001, None, None),  # over 2000 bytes: the cast would cut it
        ("char", "NCHAR(3)", "x" * 1001, None, None),
        ("bytes", "RAW(16)", "\\x00FF", "HEXTORAW(:b0)", "00FF"),
        ("bytes", "RAW(16)", "00ff", None, None),
        ("timestamp", "DATE", "2024-02-29", "TO_DATE(:b0, 'YYYY-MM-DD HH24:MI:SS')", "2024-02-29"),
        (
            "timestamp",
            "DATE",
            "2024-02-29T10:00:00",
            "TO_DATE(:b0, 'YYYY-MM-DD HH24:MI:SS')",
            "2024-02-29 10:00:00",
        ),
        ("timestamp", "DATE", "2024-02-29 10:00:00.5", None, None),  # a DATE holds no fraction
        (
            "timestamp",
            "TIMESTAMP(6)",
            "2024-02-29 10:00:00.5",
            "TO_TIMESTAMP(:b0, 'YYYY-MM-DD HH24:MI:SS.FF')",
            "2024-02-29 10:00:00.5",
        ),
        ("instant", "TIMESTAMP(6) WITH TIME ZONE", "soon", None, None),
        ("boolean", "BOOLEAN", "true", "TRUE", None),
        ("boolean", "BOOLEAN", "yes", None, None),
        ("json", "JSON", "{}", ":b0", "{}"),
    ],
)
def test_an_oracle_literal_is_a_bind_typed_by_its_column_s_class(
    cls: str, declared: str, text: str, written: str | None, bound: str | None
) -> None:
    binds: dict[str, Any] = {}
    assert BOUND.literal(text, cls, declared, binds) == written
    assert binds == ({} if bound is None else {"b0": bound})


@given(
    text=hostile | numeric,
    typed=st.sampled_from(
        [
            *(("integer", "NUMBER(10)"), ("decimal", "NUMBER(10,2)"), ("float", "BINARY_DOUBLE")),
            *(("text", "VARCHAR2(10)"), ("char", "CHAR(3)"), ("bytes", "RAW(16)"), ("timestamp", "DATE")),
            *(("instant", "TIMESTAMP(6) WITH TIME ZONE"), ("boolean", "BOOLEAN")),
        ]
    ),
)
@example(text="1e999999999999999999999999999999", typed=("integer", "NUMBER(10)"))
@example(text="-.5E-999999999999999999999999999999", typed=("float", "BINARY_DOUBLE"))
def test_an_oracle_value_is_bound_or_none(text: str, typed: tuple[str, str]) -> None:
    binds: dict[str, Any] = {}
    try:  # nothing else is raised: a number no column holds is no value, so its key is on no row
        written = BOUND.literal(text, *typed, binds)
    except UnicodeEncodeError:
        return
    assert written is None or BIND.fullmatch(written)  # fixed text around one bind variable
    assert len(binds) == (0 if written is None or typed[0] == "boolean" else 1)


def test_an_oracle_key_text_is_postgresql_s_text() -> None:
    texts = {
        declared: BOUND.key_text("K", cls, declared)
        for cls, declared in (
            ("integer", "NUMBER(10)"), ("decimal", "NUMBER"), ("decimal", "NUMBER(4,4)"),
            ("decimal", "NUMBER(*,2)"), ("text", "VARCHAR2(10)"), ("char", "CHAR(4)"), ("bytes", "RAW(16)"),
            ("timestamp", "DATE"),
        )
    }  # fmt: skip
    k = 't."K"'
    assert texts == {
        "NUMBER(10)": f"TO_CHAR({k}, 'TM9')",
        # An unconstrained NUMBER: a whole number's text, none for any other (the row is skipped).
        "NUMBER": f"CASE WHEN {k} = TRUNC({k}) AND ABS({k}) < 1E38 THEN TO_CHAR({k}, 'TM9') END",
        "NUMBER(4,4)": f"TO_CHAR({k}, 'FM0.0000')",
        "NUMBER(*,2)": f"TO_CHAR({k}, 'FM{'9' * 35}0.00')",
        "VARCHAR2(10)": k,
        "CHAR(4)": f"RTRIM({k}, ' ')",
        "RAW(16)": f"NVL2({k}, '\\x' || LOWER(RAWTOHEX({k})), NULL)",
        # A year before 1 would print as its AD twin: it has no key text.
        "DATE": f"CASE WHEN {k} >= DATE '0001-01-01' THEN TO_CHAR({k}, 'YYYY-MM-DD HH24:MI:SS') END",
    }
    with pytest.raises(Refusal):
        BOUND.ident('a"b')  # no catalogue name holds a double quote


def test_outside_al32utf8_an_oracle_key_is_ordered_and_compared_by_its_utf8_bytes() -> None:
    raw = oracle._Primitives(utf8=False)
    order = "UTL_I18N.STRING_TO_RAW(TO_CHAR(t.\"ID\", 'TM9'), 'AL32UTF8')"
    statement, binds = compose.scan(raw, O_PLAIN, [b"41"])
    assert statement.endswith(f" AND (({order} > :b0)) ORDER BY {order}") and binds == {"b0": b"41"}
    statement, binds = compose.rows(raw, O_PLAIN, {"7": [["7"]]})
    assert f" AND {order} = :b1 ORDER BY 5 DESC" in statement and binds == {"b0": "7", "b1": b"7"}


@given(key=hostile, literal=hostile, prefix=hostile, bound=st.binary(max_size=40))
def test_oracle_statements_hold_no_value(key: str, literal: str, prefix: str, bound: bytes) -> None:
    # A key, a filter's literal, a key map's prefix and a resume bound are bound, never written: each
    # statement is, to the character, the one a harmless value gives.
    def side(value: str, literal: str) -> Plan:
        key_map = {"kind": "prefix_pad", "prefix": value, "width": 6, "sideType": "integer"}
        node = where.Node("cmp", column="NOTE", test="=", values=(where.Value("str", literal),))
        return O_MAPPED | {"where": where.text(node), "key_maps": [None, key_map, None]}

    try:
        key_bytes = key.encode()
    except UnicodeEncodeError:  # a lone surrogate: the key is unfit, nothing is sent
        return
    if not key or len(key_bytes) > 2000 or not bound:
        return  # an empty part has no arm and writes another bound; a longer one fits no CHAR
    found = [
        compose.rows(BOUND, side(prefix, literal), {encode_key([key, "x", "1"]): [[key], ["42"], ["1"]]})[0],
        compose.scan(raw := oracle._Primitives(utf8=False), side(prefix, literal), [bound, key_bytes, b"1"])[
            0
        ],
        compose.null_keys(BOUND, side(prefix, literal))[0],
    ]
    harmless = [
        compose.rows(BOUND, side("A-", "ok"), {"eu|A-000042|1": [["eu"], ["42"], ["1"]]})[0],
        compose.scan(raw, side("A-", "ok"), [b"eu", b"A-1", b"1"])[0],
        compose.null_keys(BOUND, side("A-", "ok"))[0],
    ]
    assert found == harmless


# --- a file side's statements -----------------------------------------------------

FILES = json.loads((Path(__file__).parents[2] / "golden" / "engines" / "files_pair.json").read_text())
PINNED = {"files": ["customers/a.csv"], "pins": [[1, 2, 3, 4, "0" * 64]]}
CSV_SIDE = FILES["csv"] | {"pinned": PINNED | {"side": "a"}}
PARQUET_SIDE = FILES["parquet"] | {
    "pinned": {"files": ["dt=1/part.parquet"], "pins": PINNED["pins"], "side": "b"}
}


def _files_sent(side: Plan, keys: list[str], after: list[bytes]) -> dict[str, tuple[str, compose.Binds]]:
    """Each statement of a file side, and what it binds, by its read's name."""
    asked = {key: side_values(key, side) for key in keys}
    sent = {
        "rows": duck._keyed(side, asked),  # type: ignore[arg-type]
        "scan": compose.scan(duck.PRIMITIVES, side, None),
        "scan_after": compose.scan(duck.PRIMITIVES, side, after),
        "census": compose.census(duck.PRIMITIVES, side),
        "frame": compose.frame(duck.PRIMITIVES, side) if side.get("strata") else ("", {}),
        "null_keys": compose.null_keys(duck.PRIMITIVES, side),
        "probe": compose.probe(duck.PRIMITIVES, side),
    }
    return sent


def _named(sent: dict[str, tuple[str, compose.Binds]]) -> dict[str, list[Any]]:
    return {name: [statement, sorted(binds)] for name, (statement, binds) in sent.items()}


def test_files_statements_are_the_golden_ones() -> None:
    assert _named(_files_sent(CSV_SIDE, ["7", "x", "007"], [b"41"])) == FILES["sent"]["csv"]
    keys = ["eu|A-000042", "e'u|A-000007"]
    assert _named(_files_sent(PARQUET_SIDE, keys, [b"eu", b"A-000041"])) == FILES["sent"]["parquet"]


def test_a_files_statement_s_parts() -> None:
    csv, parquet = FILES["sent"]["csv"], FILES["sent"]["parquet"]
    rows = csv["rows"][0]
    # One statement a group: the keys join the rows by their key texts' bytes (an expression, of which DuckDB
    # derives no filter for the files' reader), each keeping two rows at most, the one its filter selects
    # first, ordered by the filter's expression (an alias s would be the column s) and, outside, by its
    # position. Letters and 007 are no integer's key text: they are not joined at all.
    flag = '((t."status" = $v0 AND t."email" LIKE $v1 ESCAPE \'\\\')) IS TRUE'
    assert rows.endswith(
        f" QUALIFY row_number() OVER (PARTITION BY a.i ORDER BY {flag} DESC) <= 2 ORDER BY a.i, 6 DESC"
    )
    assert (
        " JOIN (VALUES (0, $v2)) AS a(i, k0) ON encode(CASE WHEN regexp_full_match(t.\"id\", '-?[0-9]{1,38}')"
        in rows
    )
    # Every CSV statement reads every declared column; a csv.keys integer reads through DECIMAL(38,0).
    every = 'WHERE ("id" IS NULL OR "id" IS NOT NULL) AND ("email" IS NULL OR "email" IS NOT NULL) AND'
    assert all(every in statement for statement, _ in csv.values() if statement)
    assert "read_csv($r_files, auto_detect=false, columns=$r_columns, header=$r_header" in csv["scan"][0]
    # The order is each key text's UTF-8 bytes; a resume bound compares them, bound as bytes.
    integer = 'CAST(TRY_CAST(t."id" AS DECIMAL(38,0)) AS VARCHAR)'
    order = f"encode(CASE WHEN regexp_full_match(t.\"id\", '-?[0-9]{{1,38}}') THEN {integer} END)"
    assert csv["scan_after"][0].endswith(f" AND (({order} > $v2)) ORDER BY {order}")
    bound = '((encode(t."branch") > $v4) OR (encode(t."branch") = $v4 AND encode(CASE WHEN'
    assert bound in parquet["scan_after"][0]
    # A Parquet side's keys are joined as a CSV side's, and no typed IN is bound: of a predicate on a key
    # column the files' reader prunes row groups by statistics, which need not hold the group's values. An
    # instant renders through its UTC microseconds, a literal compared with it carries an offset; bytes as \x
    # and hex.
    assert ' ON encode(t."branch") = a.k0 AND encode(CASE WHEN ' in parquet["rows"][0]
    assert " END) = a.k1 QUALIFY " in parquet["rows"][0] and " IN (" not in parquet["rows"][0]
    assert "replace(CAST(make_timestamp(epoch_us(t.\"seen\")) AS VARCHAR), ' ', 'T')" in parquet["scan"][0]
    assert 't."seen" > CAST($v3 AS TIMESTAMPTZ)' in parquet["rows"][0]
    assert "hive_partitioning=$r_hive, hive_types=$r_types) AS t" in parquet["census"][0]
    assert parquet["frame"][0].endswith(', encode(t."branch")')  # ties broken by the strata, binary
    assert csv["probe"][0].endswith(" LIMIT 0") and csv["null_keys"][0].startswith("SELECT COUNT(*) FROM ")


def test_a_file_pair_s_scan_hashes_in_duckdb(tmp_path: Path) -> None:
    """The columns a file-to-file pair leaves out of ``digest`` are hashed by DuckDB: each rendered
    text's SHA-256, ``-`` for a NULL, all of them hashed again to 32 bytes (bytes as their hexadecimal
    digits), which keeps a NULL and the boundary between two values apart; the worker's columns follow."""
    csv, parquet = CSV_SIDE | {"digest": ["balance"]}, PARQUET_SIDE | {"digest": []}
    sent = {
        "csv": compose.scan(duck.PRIMITIVES, csv, None),
        "parquet": compose.scan(duck.PRIMITIVES, parquet, None),
    }
    assert {name: [statement, sorted(binds)] for name, (statement, binds) in sent.items()} == FILES["sent"][
        "hashed"
    ]
    assert (
        " END, unhex(sha256(concat(coalesce(sha256(t.\"email\"), '-'), coalesce(sha256(t.\"s\"), '-')))),"
        ' t."balance" FROM '
    ) in sent["csv"][0]
    assert "coalesce(sha256('\\x' || lower(hex(t.\"raw\"))), '-')" in sent["parquet"][0]
    with locked(tmp_path) as made:
        found = []
        for a, b, cls, kind in ROWS:
            side = {"classes": {"a": "text", "b": cls}, "declared": {"a": "VARCHAR", "b": kind}}
            hashed = f"SELECT {duck.PRIMITIVES.row_hash(side, 'ab')} FROM (SELECT {a} AS a, {b} AS b) t"  # noqa: S608
            found += made.con.execute(hashed).fetchone()
    assert all(isinstance(digest, bytes) and len(digest) == 32 for digest in found)
    assert len(set(found)) == len(ROWS) - 1 and found[0] == found[1]  # all apart but the two padded alike


# (a, b, b's class, b's type): a NULL against an empty text, a boundary moved, bytes, padding.
ROWS = [
    ("'x'", "'ab  '", "char", duck.FIELD),
    ("'x'", "'ab'", "char", duck.FIELD),
    ("'x'", "'ab  '", "text", duck.FIELD),
    ("'x'", "NULL", "text", "VARCHAR"),
    ("'x'", "''", "text", "VARCHAR"),
    ("'xa'", "'b'", "text", "VARCHAR"),
    ("'x'", "'ab'::BLOB", "bytes", "BLOB"),
    ("NULL", "'x'", "text", "VARCHAR"),
]


def test_files_statements_are_one_select_and_run(tmp_path: Path) -> None:
    """Every statement is one SELECT (the cursor runs nothing else), and runs on the locked instance with
    its reader's values bound."""
    with locked(tmp_path) as made:
        root = tmp_path / "files" / "root"
        (root / "customers").mkdir()
        (root / "customers" / "a.csv").write_text("id|email|balance|status|s\n7|a@x|1,5|A|x\n")
        (root / "dt=1").mkdir()
        unrestricted().execute(
            "COPY (SELECT 'eu' AS branch, 42::BIGINT AS no, 1.5::DECIMAL(10,2) AS amount,"
            " '\\x01'::BLOB AS raw, TIMESTAMPTZ '2024-01-02 00:00:00+00' AS seen)"
            f" TO '{root}/dt=1/part.parquet' (FORMAT parquet)"
        )
        found = {}
        for side, keys, after in (
            (CSV_SIDE, ["7"], [b"41"]),
            (PARQUET_SIDE, ["eu|A-000042"], [b"eu", b"A-000041"]),
        ):
            for name, (statement, binds) in _files_sent(side, keys, after).items():
                if not statement:
                    continue
                assert [s.type.name for s in made.con.extract_statements(statement)] == ["SELECT"]
                bound = {k: v for k, v in duck._reader(side, str(root)).items() if f"${k}" in statement}
                found[side["name"], name] = made.con.execute(statement, binds | bound).fetchall()
    assert found["customers", "rows"] == [(0, "a@x", "1,5", "x", "7", True)]
    assert found["accounts", "rows"][0][-3:] == ("eu", "A-000042", True)
    assert found["accounts", "census"] == [("eu", "A-000042")] and found["customers", "null_keys"] == [(0,)]


@given(
    key=hostile,
    literal=hostile,
    prefix=hostile.filter(bool),
    delimiter=st.sampled_from(["|", ",", ";", "'", "\\"]),
)
@example(key="1'); DROP TABLE x; --", literal="' OR 1=1 --", prefix="$v0", delimiter="'")
def test_duck_statements_hold_only_grammar(key: str, literal: str, prefix: str, delimiter: str) -> None:
    """Whatever a key, a filter literal, a key map's prefix or the dialect holds, a file side's statement is
    the same text, every value bound: no spec or key text is spliced in."""
    where_ = f"status = '{literal.replace(chr(39), chr(39) * 2)}'"
    try:
        where.parse(where_)
    except where.WhereError:
        return
    side = CSV_SIDE | {
        "where": where_,
        "key_maps": [{"kind": "prefix_pad", "prefix": prefix, "width": 3, "sideType": "integer"}],
    }
    side["file"] = side["file"] | {"csv": side["file"]["csv"] | {"delimiter": delimiter}}
    benign = CSV_SIDE | {"where": "status = 'A'", "key_maps": side["key_maps"]}
    for found, plain in zip(_files_sent(side, [prefix + "007"], [key.encode("utf-8", "replace")]).values(),
                            _files_sent(benign, [prefix + "007"], [b"x"]).values(), strict=True):  # fmt: skip
        assert found[0] == plain[0]
