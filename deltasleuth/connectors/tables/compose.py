"""The statements of a table side on an engine other than PostgreSQL (docs/mvp/ENGINES.md §8, §10) and those
a table pre-check aggregates by (docs/mvp/PRECHECKS.md §4), written once over the engine's primitives, and
what their rows are read as.

A statement is fixed text, identifiers and values. Identifiers are the catalogue's spelling, quoted by the
engine. A value is whatever the engine's ``literal`` writes for it: a bind variable, or on MySQL, whose
driver binds nothing on the server, a literal of a closed grammar. Nothing is escaped and no text of a
spec or a key is spliced in. Each builder returns its statement and the values it binds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from deltasleuth.connectors.tables import render
from deltasleuth.connectors.tables.engine import Refusal, Row, Rows
from deltasleuth.connectors.tables.keys import Plan, decode_key, key_maps
from deltasleuth.core import jsonio
from deltasleuth.core.spec import where

Binds = dict[str, Any]
MAX_LOB_BYTES = 16 * 1024 * 1024  # a row whose large values total more is not fetched (§6)


class Dialect(Protocol):
    """An engine's primitives. A column comes with its class and its declared type (the plan's ``classes``
    and ``declared``); ``binds`` takes the values a primitive binds."""

    def ident(self, name: str) -> str:
        """A name of the catalogue, quoted."""

    def relation(self, side: Plan) -> str:
        """The side's table or view under the alias ``t``."""

    def literal(self, text: str, cls: str, declared: str, binds: Binds) -> str | None:
        """``text`` as a value a column of class ``cls`` is compared with; None when it is not one."""

    def key_text(self, column: str, cls: str, declared: str) -> str:
        """The key column's text, PostgreSQL's ``::text`` of the same value (§8)."""

    def order(self, text: str) -> str:
        """What sorts a key text by its code points."""

    def exact(self, order: str, part: str, binds: Binds) -> str | None:
        """An order expression equal to a key's part; None when no key text can be the part."""

    def after(self, orders: Sequence[str], bound: Sequence[bytes], binds: Binds) -> str:
        """The keys after the resume bound, by their order expressions."""

    def render(self, column: str, cls: str, declared: str) -> str:
        """The column's value as the text or bytes ``render`` canonicalises (§9)."""

    def rendered(self, value: str, cls: str, declared: str) -> str:
        """``render`` of any expression of the column's type: an aggregate of it, say."""

    def length(self, column: str, cls: str, declared: str) -> str:
        """A text's length in code points (a ``char``'s without its padding, 0 when blank), bytes' in
        octets: a table pre-check's (docs/mvp/PRECHECKS.md §4)."""

    def distinct(self, column: str, cls: str, declared: str) -> str:
        """What a column's distinct values are counted by: a text's own, binary."""

    def lob(self, declared: str) -> bool:
        """Whether a column of this type may hold a large value (§6)."""

    def oversize(self, side: Plan, columns: Sequence[str]) -> str:
        """Whether the row's values in these LOB columns total more than ``MAX_LOB_BYTES``."""

    def row_hash(self, side: Plan, columns: Sequence[str]) -> str:
        """32 bytes that are equal on two sides only when these columns' rendered values are: a NULL and
        the boundary between two values stay apart (§10)."""

    def first(self, select: str, count: int, by: int | None = None) -> str:
        """The first ``count`` rows of ``select``, those with the greatest select item ``by`` first: its
        position, since any name may be a column's."""

    def selected(self, condition: str | None) -> str:
        """1 where the row filter selects the row, else 0."""

    def prefix_pad(self, text: str, prefix: str, width: int, binds: Binds) -> str:
        """A ``prefix_pad`` key map's canonical text of a key text of digits; NULL for any other."""

    def like(self, parts: list[str]) -> str:
        """A LIKE comparison, from its column, keywords and pattern."""

    def arms(self, count: int) -> int:
        """The arms a per-key statement of ``count`` keys is written with: more, where the engine keeps
        few statement texts."""


def _typed(side: Plan, column: str) -> tuple[str, str]:
    return side["classes"][column], side["declared"][column]


def _keys(d: Dialect, side: Plan, binds: Binds) -> list[str]:
    """Each key part's canonical text: the column's key text through its key map; NULL when unmatched."""
    texts = []
    for column, key_map in zip(side["keys"], key_maps(side), strict=True):
        text, kind = d.key_text(column, *_typed(side, column)), key_map["kind"] if key_map else "identity"
        if kind == "prefix_pad":
            text = d.prefix_pad(text, key_map["prefix"], key_map["width"], binds)  # type: ignore[index]
        elif kind != "identity":  # refused when the spec is loaded
            raise Refusal(f"a {kind} key map is not read on this engine")
        texts.append(text)
    return texts


def _tests(node: where.Node) -> set[str]:
    """The columns a filter tests as booleans: bare, or against TRUE or FALSE."""
    own = node.op == "column" or any(value.kind == "bool" for value in node.values)
    found = {node.column} if own and node.column else set()
    return found.union(*(_tests(child) for child in node.children))


def _patterns(node: where.Node) -> set[int]:
    """The literals of a filter that are LIKE patterns, each by its identity."""
    own = {id(value) for value in node.values} if node.op == "like" else set()
    return own.union(*(_patterns(child) for child in node.children))


def _filter(d: Dialect, side: Plan, binds: Binds) -> str | None:
    """The side's row filter, each literal typed by the class of the column it is compared with: neither
    engine's zero-row probe refuses a literal of the wrong type, so it is refused here (§12 P11). A LIKE
    pattern is no value of its column: it is written as text."""
    if not side.get("where"):
        return None
    node = where.parse(side["where"])
    for column in sorted(_tests(node)):
        cls, declared = _typed(side, column)
        if cls != "boolean":
            why = "as a boolean: compare it with 1 or 0"
            raise Refusal(f"the filter tests column {column!r} ({declared}) {why}")

    patterns = _patterns(node)

    def literal(found: where.Value, column: str) -> str:
        cls, declared = _typed(side, column)
        written = d.literal(found.text, "text" if id(found) in patterns else cls, declared, binds)
        if written is None:  # of another type, or longer than a CHAR, which would cut it
            what = f"that is not {render.LITERALS[cls]}" if cls in render.LITERALS else "over 2000 bytes"
            raise Refusal(f"the filter compares column {column!r} ({declared}) with a literal {what}")
        return written

    def join(parts: list[str]) -> str:  # keywords and operators come from the grammar
        if "ILIKE" in parts:  # refused when the spec is loaded
            raise Refusal("ILIKE is not read on this engine: use LIKE")
        return d.like(parts) if "LIKE" in parts else " ".join(parts)

    return "(" + where.render(node, lambda name: f"t.{d.ident(name)}", literal, join) + ")"


def _population(d: Dialect, side: Plan, keys: Sequence[str], binds: Binds) -> str:
    """The conditions of the compared population: every canonical key part, then the filter."""
    conditions = [f"{key} IS NOT NULL" for key in keys]
    return " AND ".join(conditions + [f for f in [_filter(d, side, binds)] if f is not None])


def _rendered(d: Dialect, side: Plan, columns: Sequence[str]) -> list[str]:
    """Each column's rendered value. With LOB columns among them, one more expression flags a row whose
    LOB values pass ``MAX_LOB_BYTES``: its LOB columns are then NULL, so no such value crosses the wire."""
    lobs = [column for column in columns if d.lob(side["declared"][column])]
    big = d.oversize(side, lobs) if lobs else None
    texts = []
    for column in columns:
        text = d.render(column, *_typed(side, column))
        texts.append(f"CASE WHEN {big} THEN NULL ELSE {text} END" if column in lobs else text)
    return [*texts, *([d.selected(big)] if big else [])]


def _strata(d: Dialect, side: Plan) -> list[str]:
    """The rendered values of a frame's strata columns (``strata``: name -> column)."""
    columns = list(side.get("strata", {}).values())
    for column in columns:
        cls, declared = _typed(side, column)
        if d.lob(declared) or cls in ("json", "other"):
            raise Refusal(f"strata column {column!r} ({declared}) cannot stratify a frame")
    return [d.render(column, *_typed(side, column)) for column in columns]


def worker_columns(side: Plan) -> list[str]:
    """The compared columns a scan renders for the worker to hash (the plan's ``digest``), as columns."""
    return [side["columns"][name] for name in side["digest"]]


def engine_columns(side: Plan) -> list[str]:
    """The compared columns the engine hashes itself, those the plan leaves out of ``digest``: none across
    engines (§10). In the order of their canonical names, which both sides share."""
    return [side["columns"][name] for name in sorted(side["columns"]) if name not in side["digest"]]


def _hashed(d: Dialect, side: Plan) -> list[str]:
    """The row hash of the side's engine columns, when it has any."""
    columns = engine_columns(side)
    return [d.row_hash(side, columns)] if columns else []


def _select(d: Dialect, side: Plan, select: Sequence[str], where_: str, order: Sequence[str] = ()) -> str:
    """One statement over the side's relation, of primitives' texts only (the module's first lines)."""
    statement = f"SELECT {', '.join(select)} FROM {d.relation(side)} WHERE {where_}"  # noqa: S608
    return statement + (f" ORDER BY {', '.join(order)}" if order else "")


def rows(d: Dialect, side: Plan, asked: Mapping[str, Sequence[Sequence[str]]]) -> tuple[str, Binds]:
    """One arm a key, as TABLES.md §4: its position in ``asked``, the rendered columns, the key texts and
    whether the filter selects the row, at most two rows, the selected first. An index-friendly ``IN`` of
    the values that may hold the key finds the rows, and each key text must also be the key's part
    exactly: a database's own comparison may find more (another case, an accent, ``007`` for ``7``). A
    key one of whose parts is no value of its column's class is on no row and has no arm; the statement
    is empty when no key has one. Where the engine keeps few statement texts, the last arm is repeated
    under position -1, which ``found`` leaves out."""
    binds: Binds = {}
    keys = _keys(d, side, binds)
    select = [*_rendered(d, side, list(side["columns"].values())), *keys, d.selected(_filter(d, side, binds))]
    flag = len(select) + 1  # the filter flag's select position, an arm's first item being its key's
    arms, last = [], ""
    for i, (key, values) in enumerate(asked.items()):
        conditions: list[str | None] = []
        own = dict(binds)  # the arm's values are bound only when it is written
        for column, alternatives in zip(side["keys"], values, strict=True):
            typed = _typed(side, column)
            written = [w for w in (d.literal(v, *typed, own) for v in alternatives) if w is not None]
            if written:
                conditions.append(f"t.{d.ident(column)} IN ({', '.join(written)})")
        if len(conditions) < len(keys):
            continue
        parts = decode_key(key, len(keys))
        conditions += [d.exact(d.order(text), part, own) for text, part in zip(keys, parts, strict=True)]
        if None in conditions:
            continue
        binds |= own
        last = " AND ".join(conditions)  # type: ignore[arg-type]
        arms.append(d.first(_select(d, side, [str(i), *select], last), 2, flag))
    if arms:
        arms += [d.first(_select(d, side, ["-1", *select], last), 2, flag)] * (d.arms(len(arms)) - len(arms))
    return " UNION ALL ".join(arms), binds


def scan(d: Dialect, side: Plan, after: Sequence[bytes] | None) -> tuple[str, Binds]:
    """The population's key texts, the hash of its engine columns when it has any and its worker columns'
    rendered values, in the code-point order of the key texts, after the key whose parts are ``after``."""
    binds: Binds = {}
    keys = _keys(d, side, binds)
    orders = [d.order(key) for key in keys]
    population = _population(d, side, keys, binds)
    if after is not None:
        population += f" AND {d.after(orders, after, binds)}"
    select = [*keys, *_hashed(d, side), *_rendered(d, side, worker_columns(side))]
    return _select(d, side, select, population, orders), binds


def census(d: Dialect, side: Plan) -> tuple[str, Binds]:
    """The population's key texts in ``scan``'s order: a key held by several rows comes once a row."""
    binds: Binds = {}
    keys = _keys(d, side, binds)
    return _select(d, side, keys, _population(d, side, keys, binds), [d.order(key) for key in keys]), binds


def frame(d: Dialect, side: Plan) -> tuple[str, Binds]:
    """``census``'s key texts and order, then each row's strata columns rendered, which break ties in
    binary order, so a key's first row is always the same one."""
    binds: Binds = {}
    select = [*_keys(d, side, binds), *_strata(d, side)]
    population = _population(d, side, select[: len(side["keys"])], binds)
    return _select(d, side, select, population, [d.order(text) for text in select]), binds


def null_keys(d: Dialect, side: Plan) -> tuple[str, Binds]:
    """Rows of the filtered population a scan cannot compare: a key part is NULL, or its key map does
    not match it."""
    binds: Binds = {}
    missing = " OR ".join(f"{key} IS NULL" for key in _keys(d, side, binds))
    conditions = [f"({missing})"] + [f for f in [_filter(d, side, binds)] if f is not None]
    return _select(d, side, ["COUNT(*)"], " AND ".join(conditions)), binds


def probe(d: Dialect, side: Plan) -> tuple[str, Binds]:
    """Everything a run reads of the side, its key maps, its filter and its row hash, parsed but reading no
    row: a name, a privilege or an operator the database refuses is refused when the run is created."""
    binds: Binds = {}
    keys = _keys(d, side, binds)
    rendered = _rendered(d, side, list(side["columns"].values()))
    select = [*keys, *rendered, *_hashed(d, side), *_strata(d, side)]
    return d.first(_select(d, side, select, _population(d, side, keys, binds)), 0), binds


# A table pre-check's figure of a column (docs/mvp/PRECHECKS.md §4), from the column ``c``, its ``length``
# and what its distinct values are counted by; a minimum and a maximum are ``rendered`` as the column is, a
# sum as a decimal.
_FIGURES = {
    "nulls": "COUNT(CASE WHEN {c} IS NULL THEN 1 END)",
    "empty": "COUNT(CASE WHEN {length} = 0 THEN 1 END)",
    "shortest": "MIN({length})",
    "longest": "MAX({length})",
    "true": "COUNT(CASE WHEN {c} THEN 1 END)",
    "distinct": "COUNT(DISTINCT {distinct})",
}
_MOMENTS = ("date", "timestamp", "instant", "time")  # whose minimum and maximum are compared canonical


def figures(d: Dialect, side: Plan, wanted: Sequence[tuple[str, str | None]]) -> tuple[str, Binds]:
    """One row of the figures ``wanted`` (each with its column; None: of the rows) over the rows the filter
    selects, those without a key among them, as a scan's ``rows`` and the census count them. Every item
    aggregates: no value of a row is selected."""
    binds: Binds = {}
    select = []
    for figure, column in wanted:
        if figure == "rows":
            select.append("COUNT(*)")
        elif column is None:  # those ``null_keys`` counts
            missing = " OR ".join(f"{key} IS NULL" for key in _keys(d, side, binds))
            select.append(f"COUNT(CASE WHEN {missing} THEN 1 END)")
        elif figure == "sum":  # an exact decimal on every engine: MySQL's of a bit too, which UNSIGNED clips
            select.append(d.rendered(f"SUM(t.{d.ident(column)})", "decimal", "decimal"))
        elif figure in ("min", "max"):
            select.append(d.rendered(f"{figure.upper()}(t.{d.ident(column)})", *_typed(side, column)))
        else:
            typed = (column, *_typed(side, column))
            forms = {"c": f"t.{d.ident(column)}", "length": d.length(*typed), "distinct": d.distinct(*typed)}
            select.append(_FIGURES[figure].format(**forms))
    selected = _filter(d, side, binds)
    statement = f"SELECT {', '.join(select)} FROM {d.relation(side)}"  # noqa: S608 (primitives' texts)
    return statement + (f" WHERE {selected}" if selected else ""), binds


def figured(side: Plan, wanted: Sequence[tuple[str, str | None]]) -> Callable[[Rows], list[str | None]]:
    """What reads ``figures``'s one row as texts: a count or a length as its digits, a number as the engine
    renders it, and a date's or a time's minimum and maximum in canonical rendering 1, as PostgreSQL's
    ``to_jsonb`` writes them, so that two engines' are compared as one (ENGINES.md §9)."""

    def read(rows: Rows) -> list[str | None]:
        texts = []
        for (figure, column), found in zip(wanted, rows[0], strict=True):
            cls = column and side["classes"][column]
            if found is not None and figure in ("min", "max") and cls in _MOMENTS:
                found = render.value(cls, found, column)  # type: ignore[arg-type]
            texts.append(None if found is None else str(found))
        return texts

    return read


def duplicates(d: Dialect, side: Plan) -> tuple[str, Binds]:
    """How many keys of the population several rows hold, and how many rows hold them: what the scan's
    merge calls a repeated key. Its rows are grouped by their key texts' order expressions, binary."""
    binds: Binds = {}
    keys = _keys(d, side, binds)
    grouped = _select(d, side, ["COUNT(*) AS n"], _population(d, side, keys, binds))
    grouped += f" GROUP BY {', '.join(d.order(key) for key in keys)} HAVING COUNT(*) > 1"
    return f"SELECT COUNT(*), COALESCE(SUM(d.n), 0) FROM ({grouped}) d", binds  # noqa: S608


def _flagged(d: Dialect, side: Plan, columns: Sequence[str]) -> bool:
    """Whether a statement that renders ``columns`` also selects the flag of an oversize row."""
    return any(d.lob(side["declared"][column]) for column in columns)


def large(d: Dialect, side: Plan) -> bool:
    """Whether a per-key row of the side may hold a large value: a compared column is a LOB column (§6)."""
    return _flagged(d, side, list(side["columns"].values()))


def found(d: Dialect, side: Plan, asked: Mapping[str, Any], fetched: Rows) -> list[Row]:
    """``rows``'s answer as the per-key connector takes it. Each fetched row is its key's position, the
    rendered columns, the flag of an oversize row when the statement selects one, the key texts and
    whether the filter selects it; a row whose key texts are not its key's is left out (the database's
    comparison found a row whose key text is another, or it answers a repeated arm)."""
    keys = [tuple(decode_key(key, len(side["keys"]))) for key in asked]
    names, n = list(side["columns"]), len(side["columns"])
    flagged = _flagged(d, side, list(side["columns"].values()))
    read: list[Row] = []
    for row in fetched:
        i, rendered, key = int(row[0]), row[1 : 1 + n], row[-1 - len(side["keys"]) : -1]
        if i < 0 or key != keys[i]:
            continue
        try:
            if flagged and int(row[1 + n]):
                raise jsonio.ResourceLimitError("the row's large values pass the fetch limit")
            document: str | ValueError = jsonio.dumps(render.document(side, names, rendered))
        except ValueError as exc:  # a text not of its class's form names its column, never a value
            document = exc
        read.append((i, document, bool(int(row[-1]))))
    return read


def digested(d: Dialect, side: Plan) -> Callable[[Rows], Rows]:
    """What turns a fetched batch of ``scan`` into (key parts..., digest): each row's digest is the
    engine's hash of its engine columns extended with the worker's of the columns the statement rendered,
    and the hash alone when it rendered none; a row that has no digest carries its marker."""
    n, names, hashed = len(side["keys"]), side["digest"], bool(engine_columns(side))
    start = n + hashed
    flagged, end = _flagged(d, side, worker_columns(side)), start + len(names)

    def digests(rows: Rows) -> Rows:
        out = []
        for row in rows:
            plain = row[n] if hashed else render.EMPTY
            try:
                if plain is None or (flagged and int(row[-1])):  # a hash the engine could not hold either
                    digest = render.RESOURCE_LIMIT
                elif names:
                    digest = render.digest(side, plain, render.document(side, names, row[start:end]))
                else:
                    digest = plain
            except ValueError:  # it does not convert: its per-key read says why
                digest = render.UNCONVERTIBLE
            out.append((*row[:n], digest))
        return out

    return digests


def stratified(side: Plan) -> Callable[[Rows], Rows]:
    """What turns a fetched batch of ``frame`` into (key parts..., strata JSON): the worker builds each
    row's strata document."""
    n, strata = len(side["keys"]), side.get("strata", {})
    classes = [(name, side["classes"][column]) for name, column in strata.items()]

    def document(rendered: Sequence[Any]) -> str:
        found = zip(classes, rendered, strict=True)
        return jsonio.dumps({name: render.value(cls, value, name) for (name, cls), value in found})

    return lambda rows: [(*row[:n], document(row[n:])) for row in rows]
