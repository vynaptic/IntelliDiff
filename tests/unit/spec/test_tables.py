"""Table sides (docs/mvp/TABLES.md §2): they compile to ``SideConfig.table``, what the product does not run
yet is ``unsupported``, key columns fit the key, and a side is a table exactly when its target is a database.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from deltasleuth.core.spec import UNSUPPORTED, TargetRegistry, compile_spec, validate_spec
from deltasleuth.core.types import DatabaseTarget, NotFoundRule, Side, TableConfig, Target

_SPEC: dict[str, Any] = {
    "apiVersion": "deltasleuth/v1",
    "kind": "ComparisonSpec",
    "metadata": {"name": "users-tables", "version": "1.0.0", "owner": "tests"},
    "normalizerVersion": "1.1.0",
    "key": {"name": "userid", "type": "string"},
    "sides": {
        "a": {"role": "legacy", "target": "db-a", "table": {"name": "users", "keyColumns": ["userid"]}},
        "b": {
            "role": "new",
            "target": "db-b",
            "table": {
                "schema": "public",
                "name": "users_v",
                "keyColumns": ["user_id"],
                "columns": ["Email", {"name": "full_name", "as": "name"}],
            },
        },
    },
    "compare": {},
    "recheck": {"delay": "0s", "times": 1},
}
SERVICE = {
    "role": "new",
    "target": "svc",
    "request": {"method": "GET", "path": "/users/{{ key }}"},
    "truncation": {"itemsPath": "$.tags", "cap": 50},
}
LOOKUP = {"kind": "lookup", "target": "db-a", "template": {"id": "x", "version": 1}}
TARGETS = TargetRegistry(
    {
        "db-a": Target("db-a", "", dsn_ref="file:/run/secrets/a"),
        "db-b": Target("db-b", "", dsn_ref="file:/run/secrets/b"),
        "svc": Target("svc", "https://svc.example"),
    }
)
LEDGER = DatabaseTarget("mysql", "ledger.example", 3306, "ledger", "reader", "file:/run/secrets/ledger")
ENGINE_TARGETS = TargetRegistry(
    {
        "db-a": Target("db-a", "", database=LEDGER),
        "db-b": Target("db-b", "", dsn_ref="file:/run/secrets/b"),
        "svc": Target("svc", "https://svc.example"),
    }
)


@pytest.fixture
def spec() -> dict[str, Any]:
    return copy.deepcopy(_SPEC)


def _errors(doc: dict[str, Any], **kwargs: Any) -> set[tuple[str, str]]:
    return {(i.pointer, i.code) for i in validate_spec(doc, **kwargs) if i.severity == "error"}


def _mixed(doc: dict[str, Any]) -> dict[str, Any]:
    """Side b a service: its keys come from key.source."""
    doc["key"] |= {"pattern": "^[0-9]+$", "source": {"kind": "list", "keys": ["1"]}}
    doc["sides"]["b"] = copy.deepcopy(SERVICE)
    return doc


def test_keys_of_a_value_class_without_a_pattern_are_warned_of(spec: dict[str, Any]) -> None:
    """Two table sides need no key.pattern, and a pack's gate cannot tell a key of a value class without
    one (docs/mvp/PACK.md §2.3): no pack was issued for any run of such a spec, and nothing said so before
    its cycle was signed off. A warning: the spec stays valid."""
    spec["key"]["class"] = "identifier"
    [issue] = validate_spec(spec, targets=TARGETS)
    assert (issue.pointer, issue.code, issue.severity) == ("/key/pattern", "key_pattern_empty", "warning")
    assert issue.message == (
        "key.pattern is missing: a raw key cannot be told from other text, so no evidence pack is issued for"
        " a run of this spec (docs/mvp/PACK.md §2.3); declare a pattern that fits its keys only, or class"
        " keys that are not personal business_code"
    )
    assert compile_spec(spec, targets=TARGETS).key.pattern is None
    spec["key"]["pattern"] = "^[0-9]{6}$"
    assert validate_spec(spec, targets=TARGETS) == []


def test_table_sides_compile_to_table_configs(spec: dict[str, Any]) -> None:
    assert validate_spec(spec, targets=TARGETS) == []  # no session, and side a without columns
    sides = compile_spec(spec, targets=TARGETS).sides
    a, b = sides[Side.A], sides[Side.B]
    assert a.table == TableConfig(name="users", key_columns=("userid",), key_maps=(None,))
    assert b.table == TableConfig(
        name="users_v",
        key_columns=("user_id",),
        columns=(("Email", "email"), ("full_name", "name")),
        schema="public",
        key_maps=(None,),
    )
    assert (b.not_found, b.success_status) == (NotFoundRule((404,)), (200,))
    assert (b.request, b.select, b.map) == (None, None, None)
    spec["sides"]["a"]["table"]["columns"] = []
    assert _errors(spec) == {("/sides/a/table/columns", "schema.minItems")}


def _template(doc: dict[str, Any]) -> None:
    doc["sides"]["a"]["template"] = {"id": "users", "version": 1, "keyColumns": ["userid"]}
    del doc["sides"]["a"]["table"]


@pytest.mark.parametrize(
    ("mutate", "pointer"),
    [
        (_template, "/sides/a/template"),
        (lambda d: d["sides"]["a"].update(session={"timezone": "UTC"}), "/sides/a/session"),
        (lambda d: d["sides"]["a"].update(collation={"name": "C"}), "/sides/a/collation"),
        (
            lambda d: d["sides"]["a"]["table"].update(keyColumns=[{"name": "userid", "part": "userid"}]),
            "/sides/a/table/keyColumns/0/part",
        ),
        (
            lambda d: d["sides"]["a"]["table"].update(keyColumns=[{"name": "userid", "keyMap": LOOKUP}]),
            "/sides/a/table/keyColumns/0/keyMap",
        ),
        (lambda d: d["key"].update(normalize=["trim"]), "/key/normalize"),
        (lambda d: d["compare"].update(valueFilter="side == 'a' || value != ''"), "/compare/valueFilter"),
    ],
)
def test_what_table_sides_do_not_run_yet_is_unsupported(
    spec: dict[str, Any], mutate: Callable[[dict[str, Any]], None], pointer: str
) -> None:
    mutate(spec)
    issues = validate_spec(spec)
    assert [(i.pointer, i.code) for i in issues] == [(pointer, UNSUPPORTED)]
    assert issues[0].message.startswith("not supported: ")


def test_a_spec_with_a_table_side_may_pause_on_schema_drift(spec: dict[str, Any]) -> None:
    """docs/mvp/PRECHECKS.md §7: ``schemaDrift: {mode: pause}`` is accepted when a side is a table, with
    two table sides and with one, and compiles as the spec's content."""
    spec["schemaDrift"] = {"mode": "pause"}
    assert _errors(spec) == set() and _errors(_mixed(copy.deepcopy(spec))) == set()
    assert compile_spec(spec).raw["schemaDrift"] == {"mode": "pause"}
    without = {name: member for name, member in spec.items() if name != "schemaDrift"}
    assert compile_spec(spec).content_hash != compile_spec(without).content_hash  # in the diff reviewed


def test_a_composite_key_beside_a_service_side_is_judged_by_its_parts(spec: dict[str, Any]) -> None:
    spec["key"] = {"name": "sku", "type": "composite", "parts": ["region", "code"]}
    for side in spec["sides"].values():
        side["table"]["keyColumns"] = ["region", "code"]
    assert validate_spec(spec, targets=TARGETS) == []
    # Beside a service side each part is judged on its own: the key 1 is one part of two.
    assert _errors(_mixed(spec)) == {("/key/source/keys/0", "invalid_key")}
    spec["key"]["source"]["keys"] = ["7|1"]
    assert validate_spec(spec, targets=TARGETS) == []


@pytest.mark.parametrize(
    ("key", "columns", "pointer"),
    [
        ({"type": "string"}, {"a": ["region", "code"]}, "/sides/a/table/keyColumns"),
        (
            {"type": "composite", "parts": ["region", "code"]},
            {"a": ["region", "code"], "b": ["region"]},
            "/sides/b/table/keyColumns",
        ),
    ],
)
def test_each_table_side_has_one_key_column_per_key_part(
    spec: dict[str, Any], key: dict[str, Any], columns: dict[str, list[str]], pointer: str
) -> None:
    spec["key"] |= key
    for name, names in columns.items():
        spec["sides"][name]["table"]["keyColumns"] = names
    assert _errors(spec) == {(pointer, "key_columns_mismatch")}


def test_a_table_side_needs_a_database_target_and_a_service_side_a_service(spec: dict[str, Any]) -> None:
    _mixed(spec)
    assert _errors(spec, targets=TARGETS) == set()
    spec["sides"]["a"]["target"], spec["sides"]["b"]["target"] = "svc", "db-a"
    assert _errors(spec, targets=TARGETS) == {
        ("/sides/a/target", "target_kind"),
        ("/sides/b/target", "target_kind"),
    }
    assert _errors(spec) == set()  # the target rules need a registry


def test_a_value_filter_that_ignores_the_side_runs_between_tables(spec: dict[str, Any]) -> None:
    spec["compare"]["valueFilter"] = "path != '$.email' && value != 'side'"
    assert _errors(spec, targets=TARGETS) == set()


def test_a_key_listing_needs_a_service_target(spec: dict[str, Any]) -> None:
    spec["key"]["source"] = {
        "kind": "paginated_endpoint",
        "target": "db-a",
        "request": {"method": "GET", "path": "/keys"},
        "keyPath": "$.id",
        "pagination": {"kind": "none", "itemsPath": "$.items"},
    }
    assert ("/key/source/target", "target_kind") in _errors(spec, targets=TARGETS)


PREFIX_PAD = {"kind": "prefix_pad", "prefix": "ORG-", "width": 6, "sideType": "integer"}
REGEX = {
    "kind": "regex",
    "toCanonical": {"pattern": "acct-([0-9]+)", "replacement": "A$1"},
    "toSide": {"pattern": "A([0-9]+)", "replacement": "acct-$1"},
    "sideType": "string",
}


def test_filters_key_maps_and_type_maps_compile_into_the_table_config(spec: dict[str, Any]) -> None:
    spec["sides"]["a"]["table"] |= {
        "where": "status IN ('active') AND amount > 10",
        "keyColumns": [{"name": "userid", "keyMap": PREFIX_PAD}],
    }
    spec["sides"]["a"]["typeMap"] = {"seen": {"as": "timestamp", "naiveTimezone": "Europe/London"}}
    spec["sides"]["b"]["table"]["keyColumns"] = [{"name": "user_id", "keyMap": REGEX}]
    assert validate_spec(spec, targets=TARGETS) == []
    sides = compile_spec(spec, targets=TARGETS).sides
    a, b = sides[Side.A].table, sides[Side.B].table
    assert (a.where, a.key_columns, a.key_maps) == (
        "status IN ('active') AND amount > 10",
        ("userid",),
        (PREFIX_PAD,),
    )
    assert a.types == {"seen": {"as": "timestamp", "naiveTimezone": "Europe/London"}}
    assert (b.key_columns, b.key_maps, b.types) == (("user_id",), (REGEX,), {})


def _bad_regex(
    doc: dict[str, Any], rule: str = "toSide", pattern: str = "A([0-9]+", replacement: str = "x$1"
) -> None:
    bad = REGEX | {rule: {"pattern": pattern, "replacement": replacement}}
    doc["sides"]["b"]["table"]["keyColumns"] = [{"name": "user_id", "keyMap": bad}]


@pytest.mark.parametrize(
    ("mutate", "pointer", "code", "message"),
    [
        (
            lambda d: d["sides"]["a"]["table"].update(where="lower(status) = 'x'"),
            "/sides/a/table/where",
            "invalid_where",
            "unexpected '(' at position 5",  # the parser's own words
        ),
        (
            _bad_regex,
            "/sides/b/table/keyColumns/0/keyMap/toSide/pattern",
            "invalid_key_map",
            "invalid regular expression: ",
        ),
        (
            lambda d: _bad_regex(d, "toCanonical", "acct-([0-9]+)", "A$2"),
            "/sides/b/table/keyColumns/0/keyMap/toCanonical/replacement",
            "invalid_key_map",
            "the replacement names a group the pattern lacks",
        ),
        (
            lambda d: _bad_regex(d, "toCanonical", "acct-([0-9]+)", "A$0"),  # PostgreSQL has only \\1-\\9
            "/sides/b/table/keyColumns/0/keyMap/toCanonical/replacement",
            "invalid_key_map",
            "a replacement names groups $1 to $9 only",
        ),
        (
            lambda d: _bad_regex(d, "toCanonical", "acct-([0-9]+)", "A$01"),  # PostgreSQL copies $01 as is
            "/sides/b/table/keyColumns/0/keyMap/toCanonical/replacement",
            "invalid_key_map",
            "a replacement names groups $1 to $9 only",
        ),
        (
            lambda d: _bad_regex(d, "toSide", "(?u)A([0-9]+)", "acct-$1"),
            "/sides/b/table/keyColumns/0/keyMap/toSide/pattern",
            "invalid_key_map",
            "invalid regular expression: ",
        ),
        (
            lambda d: d["sides"]["a"].update(typeMap={"seen": {"as": "date", "naiveTimezone": "Mars/Base"}}),
            "/sides/a/typeMap/seen/naiveTimezone",
            "invalid_type_map",
            "'Mars/Base' is not an IANA time zone",
        ),
        (
            lambda d: d["sides"]["a"].update(typeMap={"note": {"as": "json", "emptyAsNull": True}}),
            "/sides/a/typeMap/note/emptyAsNull",
            "invalid_type_map",
            "emptyAsNull needs as: string",
        ),
    ],
    ids=[
        "filter",
        "key map pattern",
        "missing group",
        "group 0",
        "group 01",
        "inline flag",
        "type map zone",
        "emptyAsNull",
    ],
)
def test_a_filter_or_map_the_product_cannot_run_is_refused(
    spec: dict[str, Any], mutate: Callable[[dict[str, Any]], None], pointer: str, code: str, message: str
) -> None:
    mutate(spec)
    [issue] = validate_spec(spec)
    assert (issue.pointer, issue.code, issue.severity) == (pointer, code, "error")
    assert issue.message.startswith(message)


def _graph(doc: dict[str, Any], members: list[str], groups: list[str]) -> dict[str, Any]:
    doc["key"] = {"name": "link", "type": "composite", "parts": ["entity", "id_type", "id_value"]}
    for side in doc["sides"].values():
        side["table"] |= {"keyColumns": ["entity", "id_type", "id_value"], "columns": ["entity"]}
    doc["graph"] = {"members": members, "groups": groups}
    return doc


@pytest.mark.parametrize(
    ("members", "groups", "valid"),
    [
        (["entity"], ["id_type", "id_value"], True),
        (["entity"], ["id_type"], False),  # id_value is in neither
        (["entity", "id_type"], ["id_type", "id_value"], False),  # id_type is in both
        (["entity"], ["id_type", "nope"], False),  # not a key part
    ],
)
def test_graph_members_and_groups_split_the_key_parts(
    spec: dict[str, Any], members: list[str], groups: list[str], valid: bool
) -> None:
    errors = _errors(_graph(spec, members, groups), targets=TARGETS)
    assert errors == (set() if valid else {("/graph", "invalid_graph")})


def test_graph_parity_needs_two_table_sides_and_a_composite_key(spec: dict[str, Any]) -> None:
    doc = _graph(spec, ["entity"], ["id_type", "id_value"])
    doc["key"] = {"name": "link", "type": "string"}
    for side in doc["sides"].values():
        side["table"]["keyColumns"] = ["entity"]
    assert _errors(doc) == {("/graph", "invalid_graph")}


# --- a side on MySQL or Oracle (docs/mvp/ENGINES.md D9, §12 L1 to L4) ---------------------------------


def _issues(doc: dict[str, Any], targets: TargetRegistry) -> dict[str, tuple[str, str]]:
    return {
        i.pointer: (i.code, i.message) for i in validate_spec(doc, targets=targets) if i.severity == "error"
    }


def test_a_table_side_reads_a_target_that_declares_its_engine(spec: dict[str, Any]) -> None:
    spec["sides"]["a"]["typeMap"] = {"note": {"as": "string", "emptyAsNull": True}}
    spec["sides"]["a"]["table"] |= {
        "where": "name LIKE 'A%'",
        "keyColumns": [{"name": "userid", "keyMap": PREFIX_PAD}],
    }
    assert _issues(spec, ENGINE_TARGETS) == {}
    assert compile_spec(spec, targets=ENGINE_TARGETS).sides[Side.A].table.types == {
        "note": {"as": "string", "emptyAsNull": True}
    }
    spec["sides"]["a"]["target"], spec["sides"]["b"]["target"] = "svc", "db-a"
    assert _issues(spec, ENGINE_TARGETS)["/sides/a/target"] == (
        "target_kind",
        "this side needs a database target (dsn_ref or engine)",
    )
    listed = _mixed(copy.deepcopy(_SPEC))
    listed["sides"]["b"]["target"] = "db-a"  # a service side on the MySQL target
    assert _issues(listed, ENGINE_TARGETS)["/sides/b/target"] == (
        "target_kind",
        "this side needs a service target, not a database",
    )


def test_what_a_mysql_side_does_not_run_is_unsupported(spec: dict[str, Any]) -> None:
    spec["sides"]["a"]["table"] |= {
        "keyColumns": [{"name": "userid", "keyMap": REGEX}],
        "where": "name ILIKE 'a%' OR NOT (email NOT ILIKE '%x')",
    }
    spec["sides"]["b"]["table"] |= {
        "keyColumns": [{"name": "user_id", "keyMap": REGEX}],
        "where": "x ILIKE 'a'",
    }
    assert _issues(spec, ENGINE_TARGETS) == {  # side b is PostgreSQL's: it runs both
        "/sides/a/table/keyColumns/0/keyMap": (
            UNSUPPORTED,
            "not supported: a regex key map on a MySQL or Oracle side; compare a view that derives the key",
        ),
        "/sides/a/table/where": (UNSUPPORTED, "not supported: ILIKE on a MySQL or Oracle side; use LIKE"),
    }
    assert _issues(spec, TARGETS) == {}
    graph = _graph(copy.deepcopy(_SPEC), ["entity"], ["id_type", "id_value"])
    assert _issues(graph, TARGETS) == {}
    assert _issues(graph, ENGINE_TARGETS) == {
        "/graph": (
            UNSUPPORTED,
            "not supported: graph parity with a MySQL or Oracle side; export the link facts into PostgreSQL",
        )
    }


def test_a_files_target_needs_table_file(spec: dict[str, Any]) -> None:
    """docs/mvp/FILES.md §15 L5: a table side reads a files target through table.file (path, format)."""
    exports = Target("exports", "", root="/var/lib/deltasleuth/files/x")
    files = TargetRegistry(
        {target_id: TARGETS.get(target_id) for target_id in TARGETS} | {"exports": exports}
    )
    spec["sides"]["a"]["target"] = "exports"
    assert _issues(spec, files)["/sides/a/target"] == (
        "target_kind",
        "a files target needs table.file (path, format)",
    )
    listed = _mixed(copy.deepcopy(_SPEC))
    listed["sides"]["b"]["target"] = "exports"  # a service side on the files target
    assert _issues(listed, files)["/sides/b/target"] == (
        "target_kind",
        "this side needs a service target, not a database",
    )


# --- a file side (docs/mvp/FILES.md §5) -------------------------------------------------------------------

EXPORTS = Target("exports", "", root="/var/lib/deltasleuth/files/x")
FILE_TARGETS = TargetRegistry(
    {target_id: TARGETS.get(target_id) for target_id in TARGETS} | {"exports": EXPORTS}
)
CSV_FILE = {
    "path": "customers/current/*.csv",
    "format": "csv",
    "asOfFile": "_AS_OF",
    "maxAge": "26h",
    "csv": {
        "columns": ["userid", "email", "balance", "opened", "status"], "keys": {"userid": {"as": "integer"}},
        "delimiter": "|", "newline": "\r\n", "quote": "\"", "escape": "\"", "header": True, "skip": 1,
        "encoding": "latin-1", "null": "", "compression": "gzip", "padded": False,
    },
}  # fmt: skip


def _filed(spec: dict[str, Any], file: dict[str, Any] | None = None, **table: Any) -> dict[str, Any]:
    """Side a reading ``file`` on the files target."""
    spec["sides"]["a"]["target"] = "exports"
    spec["sides"]["a"]["table"] |= {"file": copy.deepcopy(file or CSV_FILE)} | table
    return spec


def test_a_file_side_compiles_to_its_file(spec: dict[str, Any]) -> None:
    doc = _filed(spec)
    doc["sides"]["a"]["typeMap"] = {
        "balance": {"as": "decimal", "decimalSeparator": ","}, "opened": {"as": "date", "format": "%d/%m/%Y"},
    }  # fmt: skip
    assert validate_spec(doc, targets=FILE_TARGETS) == []
    table = compile_spec(doc, targets=FILE_TARGETS).sides[Side.A].table
    assert table is not None and table.file == CSV_FILE and table.schema is None
    parquet = _filed(
        copy.deepcopy(_SPEC), {"path": "dt=*/part.parquet", "format": "parquet", "partitions": ["dt"]}
    )
    assert validate_spec(parquet, targets=FILE_TARGETS) == []


def test_table_file_goes_exactly_with_a_files_target(spec: dict[str, Any]) -> None:
    """L5, both ways."""
    filed = _filed(spec)
    filed["sides"]["a"]["target"] = "db-a"
    assert _issues(filed, FILE_TARGETS)["/sides/a/target"] == (
        "target_kind",
        "table.file is for a files target (engine: files)",
    )
    assert "/sides/a/target" not in _issues(_filed(copy.deepcopy(_SPEC)), FILE_TARGETS)


MARKS = "csv.delimiter, quote and escape are one character each, and the delimiter is not the quote"
KEYS = "csv.keys names key columns: {as: integer}, {as: decimal, scale: n} or {as: date}"
FORMAT = (
    "format goes with as: date or as: timestamp and holds only %Y %m %d %H %M %S %f %z and the characters - /"
    " . : T and space"
)
SEPARATOR = 'decimalSeparator goes with as: decimal or as: integer and is "." or ","'
PATH = (
    "file.path is a relative path under the target's root: letters, digits, space and . _ - = + @ , ( ) in"
    " each segment, none starting with a dot; * matches inside one segment; a trailing / reads the whole"
    " directory"
)
L10 = [
    ({"path": "../x.csv"}, "path", PATH),
    ({"csv": None}, "format", "format csv needs csv (columns, delimiter); format parquet takes none"),
    ({"format": "parquet"}, "csv", "format csv needs csv (columns, delimiter); format parquet takes none"),
    ({"csv": {"columns": ["a", "a"]}}, "csv", "csv.columns names every field once, in file order"),
    ({"csv": {"delimiter": "||"}}, "csv", MARKS),
    ({"csv": {"delimiter": '"'}}, "csv", MARKS),
    ({"csv": {"newline": "\r"}}, "csv", 'csv.newline must be "\\n" or "\\r\\n"'),
    (
        {"csv": {"encoding": "windows-1252"}}, "csv",
        "csv.encoding must be utf-8, utf-16 or latin-1: convert the export for any other",
    ),
    ({"csv": {"keys": {"email": {"as": "integer"}}}}, "csv", KEYS),
    ({"csv": {"keys": {"userid": {"as": "decimal"}}}}, "csv", KEYS),
    (
        {"asOfFile": "x.csv"}, "asOfFile",
        "file.asOfFile is a file name in the path's directory that the path does not match",
    ),
    ({"maxAge": "a day"}, "maxAge", "file.maxAge is a duration such as 26h or 2d"),
    (
        {"partitions": ["status"]}, "partitions",
        "file.partitions names the directories' keys: column names, none of them in csv.columns",
    ),
]  # fmt: skip


@pytest.mark.parametrize(
    ("change", "member", "message"), L10, ids=[f"{i}-{m}" for i, (_, m, _) in enumerate(L10)]
)
def test_each_problem_of_table_file_is_worded(
    spec: dict[str, Any], change: dict[str, Any], member: str, message: str
) -> None:
    """L10, each text, at the member it judges."""
    file = copy.deepcopy(CSV_FILE)
    for name, value in change.items():
        if name == "csv" and value is not None:
            file["csv"] |= value
        elif value is None:
            del file[name]
        else:
            file[name] = value
    assert _issues(_filed(spec, file), FILE_TARGETS)[f"/sides/a/table/file/{member}"] == (
        "invalid_file",
        message,
    )


def test_what_a_file_side_does_not_run_is_unsupported(spec: dict[str, Any]) -> None:
    """L6 to L9 (D10): graph parity, ILIKE, a regex key map and table.schema."""
    regex = [{"name": "userid", "keyMap": REGEX}]
    doc = _filed(spec, schema="public", where="email ILIKE 'a%'", keyColumns=regex)
    doc["graph"] = {"members": ["a"], "groups": ["b"]}
    issues = _issues(doc, FILE_TARGETS)
    expected = {
        "/graph": "not supported: graph parity with a file side; load the link facts into PostgreSQL",
        "/sides/a/table/where": "not supported: ILIKE on a file side; use LIKE",
        "/sides/a/table/schema": "not supported: table.schema on a file side",
        "/sides/a/table/keyColumns/0/keyMap": (
            "not supported: a regex key map on a file side; declare csv.keys or a prefix_pad map"
        ),
    }
    assert {pointer: issues[pointer] for pointer in expected} == {
        pointer: (UNSUPPORTED, message) for pointer, message in expected.items()
    }


@pytest.mark.parametrize(
    ("type_spec", "member", "message"),
    [
        ({"as": "string", "format": "%Y"}, "format", FORMAT),
        ({"as": "date", "format": "%d-%b-%y"}, "format", FORMAT),
        ({"as": "date", "decimalSeparator": ","}, "decimalSeparator", SEPARATOR),
        ({"as": "decimal", "decimalSeparator": ";"}, "decimalSeparator", SEPARATOR),
    ],
)
def test_a_type_spec_reads_a_text_by_its_format_or_separator(
    spec: dict[str, Any], type_spec: dict[str, Any], member: str, message: str
) -> None:
    """L11, on any table side."""
    spec["sides"]["b"]["typeMap"] = {"balance": type_spec}
    assert _issues(spec, TARGETS)[f"/sides/b/typeMap/balance/{member}"] == ("invalid_type_map", message)
