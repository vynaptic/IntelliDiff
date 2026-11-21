"""The SQL of a table side, composed with ``psycopg.sql``.

Names are ``Identifier``s; key-map constants, filter literals, key values and resume bounds are
named placeholders: no text of a spec or a key is ever spliced into a statement (not even into a
quoted literal, where a % would read as a placeholder). Each builder returns the statement and its
parameters.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from itertools import count
from typing import Any

from psycopg import sql

from deltasleuth.connectors.tables.keys import Plan, decode_key, key_maps, to_side
from deltasleuth.core.spec import where

Params = dict[str, Any]
_PREFIX_PAD = "CASE WHEN {v} ~ '^[0-9]+$' THEN {prefix} || lpad({v}, greatest({width}, length({v})), '0') END"
_REGEX = "CASE WHEN {v} ~ {pattern} THEN regexp_replace({v}, {pattern}, {replacement}) END"


def _join(parts: Sequence[sql.Composable], sep: str = ", ") -> sql.Composed:
    return sql.SQL(sep).join(parts)


def _table(side: Plan) -> sql.Composable:
    return sql.Identifier(side["schema"], side["name"])


def _document(columns: Mapping[str, str]) -> sql.Composable:
    """Columns under their canonical names, for ``to_jsonb``."""
    return _join(
        [
            sql.SQL("t.{} AS {}").format(sql.Identifier(col), sql.Identifier(name))
            for name, col in columns.items()
        ]
    )


def _canonical_keys(side: Plan, params: Params) -> list[sql.Composable]:
    """Each key part's canonical text: the column's text through its key map; NULL when unmatched."""
    parts = zip(side["keys"], key_maps(side), strict=True)
    return [_canonical(j, column, key_map, params) for j, (column, key_map) in enumerate(parts)]


def _canonical(j: int, column: str, key_map: Mapping[str, Any] | None, params: Params) -> sql.Composable:
    # In the "C" collation, so text compares, groups and matches exactly as the Python merge does, even
    # on a column whose collation is nondeterministic (case-insensitive 'X1' = 'x1').
    value = sql.SQL('(t.{}::text COLLATE "C")').format(sql.Identifier(column))
    kind = key_map["kind"] if key_map else "identity"
    if key_map and kind == "prefix_pad":
        params |= {f"m{j}_prefix": key_map["prefix"], f"m{j}_width": key_map["width"]}
        return sql.SQL(_PREFIX_PAD).format(
            v=value, prefix=sql.Placeholder(f"m{j}_prefix"), width=sql.Placeholder(f"m{j}_width")
        )
    if key_map and kind == "regex":
        rule = key_map["toCanonical"]
        pattern, replacement = f"m{j}_pattern", f"m{j}_replacement"
        params |= {pattern: f"^(?:{rule['pattern']})$", replacement: _replacement(rule["replacement"])}
        return sql.SQL(_REGEX).format(
            v=value, pattern=sql.Placeholder(pattern), replacement=sql.Placeholder(replacement)
        )
    return value


def _replacement(text: str) -> str:
    """A schema replacement ($1..$9) as PostgreSQL writes it (\\1..\\9); a \\ is doubled, so it stays
    literal, and & is already literal (only \\& is the whole match)."""
    return re.sub(r"\$([1-9])", r"\\\1", text.replace("\\", "\\\\"))


def _filter(side: Plan, params: Params) -> sql.Composable | None:
    """The side's row filter, its literals bound as text (the server reads them as the column's type)."""
    if not side.get("where"):
        return None
    names = count()

    def literal(value: where.Value, column: str) -> sql.Composable:
        name = f"w{next(names)}"
        params[name] = value.text
        return sql.Placeholder(name)

    def join(parts: list[Any]) -> sql.Composable:  # keywords and operators come from the grammar
        return sql.SQL(" ").join(p if isinstance(p, sql.Composable) else sql.SQL(p) for p in parts)

    column = lambda name: sql.SQL("t.{}").format(sql.Identifier(name))  # noqa: E731
    return sql.SQL("({})").format(where.render(where.parse(side["where"]), column, literal, join))


def _population(side: Plan, params: Params) -> list[sql.Composable]:
    """The conditions of the compared population: every canonical key part, then the filter."""
    conditions = [sql.SQL("{} IS NOT NULL").format(k) for k in _canonical_keys(side, params)]
    return conditions + [f for f in [_filter(side, params)] if f is not None]


def rows_sql(side: Plan, keys: Mapping[str, Sequence[Sequence[str]]]) -> tuple[sql.Composed, Params]:
    """The canonical row of each key, at most two (selected rows first), tagged with the key's
    position, whether the filter selects it, and whatever the filter: a row outside it is still read.
    ``keys`` gives, per key column, the values that may hold each key (``side_values``); each
    key is its own indexed lookup on them with untyped parameters, so the server casts them to the
    columns' types. The server's cast may find a row whose own key text differs ('007' finds 7, whose
    text is '7'), so a row is kept only when its canonical key parts are the key's, as for the scan."""
    params: Params = {}
    selected = _filter(side, params) or sql.SQL("true")
    canonical = _canonical_keys(side, params)
    arms = []
    for i, (key, values) in enumerate(keys.items()):
        conditions = []
        for j, (column, alternatives) in enumerate(zip(side["keys"], values, strict=True)):
            names = [f"k{i}_{j}_{a}" for a in range(len(alternatives))]
            params |= dict(zip(names, alternatives, strict=True))
            options = _join([sql.Placeholder(n) for n in names])
            conditions.append(sql.SQL("t.{} IN ({})").format(sql.Identifier(column), options))
        for j, part in enumerate(decode_key(key, len(side["keys"]))):
            params[f"c{i}_{j}"] = part
            conditions.append(sql.SQL("{} = {}").format(canonical[j], sql.Placeholder(f"c{i}_{j}")))
        arms.append(
            sql.SQL(
                "(SELECT {i}, to_jsonb(r.*)::text, coalesce({selected}, false) FROM {table} AS t"
                " CROSS JOIN LATERAL (SELECT {document}) AS r WHERE {match} ORDER BY 3 DESC LIMIT 2)"
            ).format(
                i=sql.Literal(i),
                selected=selected,
                table=_table(side),
                document=_document(side["columns"]),
                match=_join(conditions, " AND "),
            )
        )
    return _join(arms, " UNION ALL "), params


def scan_sql(side: Plan, after: Sequence[bytes] | None) -> tuple[sql.Composed, Params]:
    """The population's canonical key texts, the hash of its plain columns' canonical row and the
    raw row of its digest columns (typeMap), in the UTF-8 byte order of the key texts, which is
    code-point order in every database encoding; after the key whose parts are ``after`` (UTF-8).

    In a UTF-8 database (``side["charset"]``, what the session's catalogue says of the server) the "C"
    collation compares a text's UTF-8 bytes, so the key texts are ordered and bounded as they are: a key
    column of text or varchar in that collation is then read and resumed by range through its index,
    unsorted. In any other encoding they are ordered by their UTF-8 bytes, which the server sorts. A key
    part the plan orders as an integer (``integers``) is ordered and bounded by its column itself, in any
    encoding, so an integer key is read and resumed through its index too."""
    params: Params = {}
    keys = _canonical_keys(side, params)
    text = side.get("charset") == "UTF8"
    integers = set(side.get("integers") or ())
    orders: list[sql.Composable] = []
    for i, (column, key) in enumerate(zip(side["keys"], keys, strict=True)):
        if i in integers:
            orders.append(sql.SQL("t.{}").format(sql.Identifier(column)))
        else:
            orders.append(key if text else sql.SQL("convert_to({}, 'UTF8')").format(key))
    conditions = _population(side, params)
    if after is not None:
        conditions += _after(orders, after, integers, text, params)
    digest = side.get("digest") or []
    plain = {name: col for name, col in side["columns"].items() if name not in digest}
    select = [*keys, sql.SQL("sha256(convert_to(to_jsonb(r.*)::text, 'UTF8'))")]
    laterals = [sql.SQL("CROSS JOIN LATERAL (SELECT {}) AS r").format(_document(plain))]
    if digest:
        select.append(sql.SQL("to_jsonb(m.*)::text"))
        mapped = {name: side["columns"][name] for name in digest}
        laterals.append(sql.SQL("CROSS JOIN LATERAL (SELECT {}) AS m").format(_document(mapped)))
    statement = sql.SQL("SELECT {} FROM {} AS t {} WHERE {} ORDER BY {}").format(
        _join(select), _table(side), sql.SQL(" ").join(laterals), _join(conditions, " AND "), _join(orders)
    )
    return statement, params


_BIGINT = range(-(2**63), 2**63)


def _after(
    orders: Sequence[sql.Composable], after: Sequence[bytes], integers: set[int], text: bool, params: Params
) -> list[sql.Composable]:
    """The keys after the resume bound, as one row comparison an index on the key columns serves. A part
    ordered as an integer is bound as a bigint, which a smallint, an integer and a bigint column compare
    with through their index. A bound part past a bigint's range (another side's key) ends the comparison
    at the parts before it: past its largest, no row of the column equals or passes it, so a key is after
    the bound where those parts are; before its smallest, every row passes it, so also where they equal
    it. With no part before it, no key is after the bound, or every one is."""
    items: list[sql.Composable] = []
    values: list[sql.Composable] = []
    operator = ">"
    for i, (order, part) in enumerate(zip(orders, after, strict=True)):
        if i in integers:
            value = int(part)
            if value not in _BIGINT:
                operator = ">" if value > 0 else ">="
                break
            params[f"a{i}"] = value
            values.append(sql.SQL("{}::int8").format(sql.Placeholder(f"a{i}")))
        else:
            params[f"a{i}"] = part.decode() if text else part
            values.append(sql.SQL("{}::text" if text else "{}").format(sql.Placeholder(f"a{i}")))
        items.append(order)
    if not items:
        return [] if operator == ">=" else [sql.SQL("false")]
    return [sql.SQL("({}) {} ({})").format(_join(items), sql.SQL(operator), _join(values))]


def census_sql(side: Plan) -> tuple[sql.Composed, Params]:
    """The population's canonical key parts, in the UTF-8 byte order of their texts (``scan_sql``'s but for
    a part a scan orders as an integer): the key census of a key-list run. A key held by several rows comes
    once per row."""
    params: Params = {}
    keys = _canonical_keys(side, params)
    order = _join([sql.SQL("convert_to({}, 'UTF8')").format(k) for k in keys])
    statement = sql.SQL("SELECT {} FROM {} AS t WHERE {} ORDER BY {}").format(
        _join(keys), _table(side), _join(_population(side, params), " AND "), order
    )
    return statement, params


def frame_sql(side: Plan) -> tuple[sql.Composed, Params]:
    """``census_sql``'s canonical key parts and order, then each row's strata columns (``strata``: name ->
    column) as a JSON document, which breaks ties, so a key's first row is always the same one: a hash
    sample's frame."""
    params: Params = {}
    keys = _canonical_keys(side, params)
    order = [sql.SQL("convert_to({}, 'UTF8')").format(k) for k in keys]
    document = sql.SQL("'{}'")
    lateral: sql.Composable = sql.SQL("")
    if strata := side.get("strata"):
        document = sql.SQL("to_jsonb(r.*)::text")
        lateral = sql.SQL(" CROSS JOIN LATERAL (SELECT {}) AS r").format(_document(strata))
        order.append(sql.SQL('{} COLLATE "C"').format(document))
    statement = sql.SQL("SELECT {}, {} FROM {} AS t{} WHERE {} ORDER BY {}").format(
        _join(keys), document, _table(side), lateral, _join(_population(side, params), " AND "), _join(order)
    )
    return statement, params


def null_keys_sql(side: Plan) -> tuple[sql.Composed, Params]:
    """Rows of the filtered population a scan cannot compare: a key part is NULL, or its key map
    does not match it."""
    params: Params = {}
    missing = sql.SQL("({})").format(
        _join([sql.SQL("{} IS NULL").format(k) for k in _canonical_keys(side, params)], " OR ")
    )
    conditions = [missing] + [f for f in [_filter(side, params)] if f is not None]
    return sql.SQL("SELECT count(*) FROM {} AS t WHERE {}").format(
        _table(side), _join(conditions, " AND ")
    ), params


def probe_sql(side: Plan) -> tuple[sql.Composed, Params]:
    """The population's key maps and filter, planned and bound but reading no row: a literal of the
    wrong type or an operator its column lacks is refused when the run is created, and no data value
    can reach the message."""
    params: Params = {}
    statement = sql.SQL("SELECT {} FROM {} AS t WHERE {} LIMIT 0").format(
        _join(_canonical_keys(side, params)), _table(side), _join(_population(side, params), " AND ")
    )
    return statement, params


# --- table pre-checks -------------------------------------------------------

# A column's figure: ``c`` is the column and ``text`` its text in the "C" collation (a ``character``
# column's without its padding), so an empty or a distinct text is judged as the scan's merge would.
_FIGURES = {
    "nulls": "count(*) FILTER (WHERE {c} IS NULL)",
    "empty": "count(*) FILTER (WHERE {text} = '')",
    "shortest": "min(char_length({text}))",
    "longest": "max(char_length({text}))",
    "sum": "sum({c}::numeric)",
    "min": "min({c})",
    "max": "max({c})",
    "true": "count(*) FILTER (WHERE {c})",
    "distinct": "count(DISTINCT {c})",
}
# What a class reads otherwise: a text's distinct values by its text, bytes' lengths in octets, and a
# date's or a time's minimum and maximum as a row's document renders them (canonical rendering 1).
_BY_CLASS = {
    **{(cls, "distinct"): "count(DISTINCT {text})" for cls in ("text", "char")},
    ("bytes", "shortest"): "min(octet_length({c}))",
    ("bytes", "longest"): "max(octet_length({c}))",
    **{
        (cls, figure): "to_jsonb(" + figure + "({c})) #>> '{{}}'"
        for cls in ("date", "timestamp", "instant", "time")
        for figure in ("min", "max")
    },
}


def figures_sql(side: Plan, wanted: Sequence[tuple[str, str | None]]) -> tuple[sql.Composed, Params]:
    """One row of the figures ``wanted`` (each with its column; None: of the rows), as texts, over the
    rows the filter selects: those without a key are among them, as in a scan's ``rows`` and in the
    census. Each column figure is written for its class (``side["classes"]``). The statement aggregates:
    no value of a row is selected."""
    params: Params = {}
    select = []
    for figure, column in wanted:
        if column is None:  # of the rows: every one, or those ``null_keys_sql`` counts
            counted: sql.Composable = sql.SQL("count(*)")
            if figure == "null_keys":
                missing = [sql.SQL("{} IS NULL").format(k) for k in _canonical_keys(side, params)]
                counted = sql.SQL("count(*) FILTER (WHERE {})").format(_join(missing, " OR "))
            select.append(sql.SQL("({})::text").format(counted))
            continue
        form = _BY_CLASS.get((side["classes"][column], figure), _FIGURES[figure])
        select.append(
            sql.SQL("(" + form + ")::text").format(
                c=sql.SQL("t.{}").format(sql.Identifier(column)),
                text=sql.SQL('(t.{}::text COLLATE "C")').format(sql.Identifier(column)),
            )
        )
    selected = [f for f in [_filter(side, params)] if f is not None]
    statement = sql.SQL("SELECT {} FROM {} AS t{}").format(
        _join(select), _table(side), sql.SQL(" WHERE {}").format(selected[0]) if selected else sql.SQL("")
    )
    return statement, params


def duplicates_sql(side: Plan) -> tuple[sql.Composed, Params]:
    """How many keys of the population several rows hold, and how many rows hold them: what the scan's
    merge calls a repeated key. Its rows are grouped by their canonical key texts, binary."""
    params: Params = {}
    statement = sql.SQL(
        "SELECT count(*), coalesce(sum(d.n), 0) FROM"
        " (SELECT count(*) AS n FROM {} AS t WHERE {} GROUP BY {} HAVING count(*) > 1) AS d"
    ).format(_table(side), _join(_population(side, params), " AND "), _join(_canonical_keys(side, params)))
    return statement, params


# --- graph parity ----------------------------------------------------------------------


def _fact_texts(
    side: Plan, members: Sequence[int], groups: Sequence[int], params: Params
) -> tuple[sql.Composable, sql.Composable]:
    """A link fact's member and group as key texts: their canonical parts, joined as ``encode_key``
    joins them (``|``, with ``\\`` and ``|`` escaped) when there are several."""
    keys = _canonical_keys(side, params)
    return _text([keys[i] for i in members]), _text([keys[i] for i in groups])


def _text(parts: Sequence[sql.Composable]) -> sql.Composable:
    if len(parts) == 1:
        return parts[0]
    escaped = [sql.SQL("replace(replace({}, '\\', '\\\\'), '|', '\\|')").format(p) for p in parts]
    return sql.SQL("concat_ws('|', {})").format(_join(escaped))


def _facts(
    side: Plan,
    members: Sequence[int],
    groups: Sequence[int],
    params: Params,
    narrow: sql.Composable | None = None,
) -> sql.Composable:
    member, group = _fact_texts(side, members, groups, params)
    conditions = _population(side, params) + ([narrow] if narrow is not None else [])
    return sql.SQL("SELECT {} AS g, {} AS m FROM {} AS t WHERE {}").format(
        group, member, _table(side), _join(conditions, " AND ")
    )


def group_sets_sql(side: Plan, members: Sequence[int], groups: Sequence[int]) -> tuple[sql.Composed, Params]:
    """One row per group of the population: its text, its fact count and its member set's hash, in
    the UTF-8 byte order of the group texts. The hash is two sums of 64-bit slices of each member
    text's sha256: independent of order, a multiset's (a repeated member counts twice), and held in
    constant memory however large the group (a sorted concatenation of a large group's members would
    pass the server's 1 GB value limit)."""
    params: Params = {}
    statement = sql.SQL(
        "SELECT f.g, count(*), sum(h.hi), sum(h.lo) FROM ({}) AS f CROSS JOIN LATERAL"
        " (SELECT sha256(convert_to(f.m, 'UTF8')) AS d) AS x CROSS JOIN LATERAL"
        " (SELECT ('x' || encode(substr(x.d, 1, 8), 'hex'))::bit(64)::bigint::numeric AS hi,"
        " ('x' || encode(substr(x.d, 9, 8), 'hex'))::bit(64)::bigint::numeric AS lo) AS h"
        " GROUP BY f.g ORDER BY convert_to(f.g, 'UTF8')"
    ).format(_facts(side, members, groups, params))
    return statement, params


def facts_sql(
    side: Plan, members: Sequence[int], groups: Sequence[int], by: str, values: Sequence[str]
) -> tuple[sql.Composed, Params]:
    """The (group, member) facts of the population whose ``by`` ("g" or "m") text is one of
    ``values``. The side values those texts map back to narrow the rows with ``IN`` lists an index
    on the columns can serve; the texts then select exactly."""
    parts = members if by == "m" else groups
    columns, maps = [side["keys"][i] for i in parts], [key_maps(side)[i] for i in parts]
    options: list[set[str]] = [set() for _ in parts]
    for text in values:
        mapped = [to_side(piece, m) for piece, m in zip(decode_key(text, len(parts)), maps, strict=True)]
        held = [m for m in mapped if m is not None]
        if len(held) == len(mapped):  # else no row of this side holds it
            for found, alternatives in zip(options, held, strict=True):
                found.update(alternatives)
    params: Params = {"values": list(values)}
    narrow = []
    for j, (column, found) in enumerate(zip(columns, options, strict=True)):
        names = [f"n{j}_{a}" for a in range(len(found))]
        params |= dict(zip(names, sorted(found), strict=True))
        listed = _join([sql.Placeholder(n) for n in names]) if names else sql.SQL("NULL")
        narrow.append(sql.SQL("t.{} IN ({})").format(sql.Identifier(column), listed))
    facts = _facts(side, members, groups, params, _join(narrow, " AND "))
    statement = sql.SQL(
        "SELECT f.g, f.m FROM ({}) AS f JOIN unnest({}::text[]) AS v (x) ON f.{} = v.x"
    ).format(facts, sql.Placeholder("values"), sql.Identifier(by))
    return statement, params
