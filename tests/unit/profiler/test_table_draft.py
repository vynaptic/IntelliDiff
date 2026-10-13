"""A starter spec for two relations a table pre-check paired by name (docs/mvp/PRECHECKS.md §3): its
tables in the catalogue's spelling, the common primary key or a placeholder, the columns both sides hold of
one family, a comment for each column left out, its issues; and that a catalogue's name, whatever it holds,
changes nothing a parser reads of it."""

from __future__ import annotations

from typing import Any

from deltasleuth.connectors.tables.engine import Column, Relation
from deltasleuth.core import precheck
from deltasleuth.core.precheck import Pair
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.profiler.tables import TODO_KEY, table_draft

FACTS = {"kind": "table", "server": "16.4", "consistency": "snapshot"}
AT = "2026-10-07T10:02:11.000000Z"
PC = "PC-" + "0" * 25 + "1"


def col(name: str, declared: str = "text", cls: str | None = None) -> Column:
    cls = declared if cls is None else cls
    return Column(name, declared, cls, True, cls == "text", nullable=True)


def drafted(a: Relation, b: Relation, label: str = "customers", **pair: Any) -> dict[str, Any]:
    """The stored pair of ``a`` and ``b`` paired by name, read at level schema."""
    plan = precheck.implied(a, b, ("oracle", "postgresql"))
    sides = {s: {"engine": "postgresql", "at": AT, "failed": {}} for s in "ab"}
    judged = precheck.compare("schema", "schema", Pair(None, label), plan, {"a": a, "b": b}, sides)
    return {
        "precheck_id": PC, "n": 7, "label": label, "spec_id": None,
        "target_a": "crm-legacy", "target_b": "platform", "result": judged.result,
    } | pair  # fmt: skip


def test_a_draft_with_a_common_key_loads_as_a_spec() -> None:
    a = Relation(
        "APP", "CUSTOMERS",
        (col("ID", "NUMBER(10)", "integer"), col("EMAIL", "VARCHAR2(120 CHAR)", "text"),
         col("OPENED", "DATE", "timestamp"), col("LEGACY_FLAG", "NUMBER(1)", "integer")),
        FACTS | {"key": ["ID"]},
    )  # fmt: skip
    b = Relation(
        "public",
        "customers",
        (col("id", "integer"), col("email"), col("opened", "date"), col("tier")),
        FACTS | {"key": ["id"]},
    )
    draft = table_draft(drafted(a, b))
    spec = draft.spec
    assert parse_yaml_text(draft.yaml_text) == spec
    # A catalogue gives no key pattern, and identifier keys without one get no evidence pack: said at once.
    assert [(i.severity, i.pointer, i.code) for i in draft.issues] == [
        ("warning", "/key/pattern", "key_pattern_empty")
    ]
    assert '#   - "warning: /key/pattern: key.pattern is missing: a raw key cannot be told' in draft.yaml_text
    compiled = compile_spec(parse_yaml_text(draft.yaml_text))  # it loads as a spec
    assert (compiled.name, compiled.key.name) == ("customers", "id")
    assert spec["sides"]["a"] == {
        "role": "baseline", "target": "crm-legacy",
        "table": {"schema": "APP", "name": "CUSTOMERS", "keyColumns": ["ID"], "columns": ["EMAIL"]},
    }  # fmt: skip
    assert spec["sides"]["b"]["table"] == {
        "schema": "public", "name": "customers", "keyColumns": ["id"], "columns": ["email"],
    }  # fmt: skip
    assert spec["security"] == {"unlisted": "tokenize"} and spec["key"]["class"] == "identifier"
    # Each column left out, as the pre-check said it, its bridge never applied: as comments.
    text = draft.yaml_text
    assert (
        '#   "opened is a timestamp (DATE) on side a and a date on side b: declare typeMap {OPENED: {as:'
        in text
    )
    assert '#   "legacy_flag is on side a only"' in text and '#   "tier is on side b only"' in text
    assert "TODO" not in text and text.startswith(
        f"# DeltaSleuth ComparisonSpec DRAFT, from table pre-check {PC} pair 7:\n"
        '#     "customers", paired by name.'
    )


def test_a_draft_without_a_common_key_names_none() -> None:
    a = Relation("APP", "ORDERS", (col("REGION"), col("ID", "integer"), col("NOTE")), FACTS | {"key": ["ID"]})
    b = Relation(
        "public", "orders", (col("region"), col("id", "integer"), col("note")), FACTS | {"key": None}
    )
    draft = table_draft(drafted(a, b, "orders"))
    assert [draft.spec["sides"][s]["table"]["keyColumns"] for s in "ab"] == [[TODO_KEY], [TODO_KEY]]
    assert draft.spec["sides"]["a"]["table"]["columns"] == ["ID", "NOTE", "REGION"]  # by name
    assert f"# No common primary key: name a key column on each side for {TODO_KEY}." in draft.yaml_text
    assert parse_yaml_text(draft.yaml_text) == draft.spec


def test_a_key_whose_texts_never_meet_is_said_at_the_key_not_among_the_columns() -> None:
    cents = Column("ID", "NUMBER(10,2)", "decimal", True, False, nullable=False, size=("digits", 10, 2))
    a = Relation("APP", "T", (cents, col("Email"), col("EMAIL"), col("NOTE")), FACTS | {"key": ["ID"]})
    b = Relation("public", "t", (col("id", "integer"), col("email"), col("note")), FACTS | {"key": ["id"]})
    draft = table_draft(drafted(a, b, "t"))
    text = draft.yaml_text
    said = text.index('# "key column ID is NUMBER(10,2) on side a and integer on side b: a number key')
    assert said < text.index("\nkey:") < text.index("# Columns left out")  # said at the key
    assert draft.spec["sides"]["a"]["table"]["keyColumns"] == ["ID"]
    assert draft.spec["sides"]["a"]["table"]["columns"] == ["NOTE"]  # neither of two columns named alike
    assert '#   "Email and another column of side a both compare as email: list each in a spec' in text
    assert '#   "EMAIL and another column of side a both compare as email: list each in a spec' in text


def test_a_hostile_column_name_changes_nothing_a_parser_reads() -> None:
    """YAML reads U+0085, U+2028 and U+2029 as line breaks: a name holding them, or a newline, then spec
    content, reaches the body as a scalar the dumper writes and a comment as ``json.dumps`` (ASCII)."""
    lines, paragraphs = chr(0x2028), chr(0x2029)  # written so, as a source file holds no such break
    hostile = f"x{lines}security: {{unlisted: clear}}\u0085y\nsecurity: {{unlisted: clear}}{paragraphs}"
    a = Relation(
        "APP", "T", (col("ID", "integer"), col(hostile), col("only" + hostile)), FACTS | {"key": ["ID"]}
    )
    b = Relation("public", "t", (col("id", "integer"), col(hostile)), FACTS | {"key": ["id"]})
    draft = table_draft(drafted(a, b, f"t{lines}security: {{unlisted: clear}}"))
    parsed = parse_yaml_text(draft.yaml_text)
    assert parsed == draft.spec and parsed["security"] == {"unlisted": "tokenize"}
    assert (
        parsed["sides"]["a"]["table"]["columns"] == [hostile]
        and parsed["metadata"]["name"] == "t-security-unlisted-clear"
    )
    comments = [line for line in draft.yaml_text.split("\n") if line.startswith("#")]
    assert all(line.isascii() for line in comments) and "\\u2028" in "".join(comments)
    for breaking in (lines, "\u0085", paragraphs):
        assert all(breaking not in line for line in comments)
    # The name is no column name a spec takes: its issues head the draft, as comments too.
    assert "error" in {issue.severity for issue in draft.issues}
    assert "# Issues (validate_spec):" in draft.yaml_text
