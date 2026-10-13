"""Table pre-checks, pure (docs/mvp/PRECHECKS.md §4): what a level reads of a pair of tables, what the two
catalogues and the figures read say of the pair, its verdict and every text a surface prints of it.

A pre-check counts and adds up; it compares no row (``NOT``). ``wanted`` names the figures a level reads
of each side, ``schema_diff`` compares the two relations as their catalogues hold them, and ``compare``
judges what was read: the pair's result (format 1), its verdict, the first of ``VERDICTS`` that holds, and
its lines, the verdict's first. Two relations paired by name have no spec: ``implied`` is the plan they are
read by, and ``alone`` judges a name without a pair. A minimum or a maximum is compared here and kept as a
verdict only; a sum is kept as a number only where ``shown`` allows it and ``exact`` holds on both sides, and
a protected column's counts beside its NULLs and its lengths as verdicts under a filter (``filtered``). A
check not made reads ``not checked`` or ``not compared`` with its reason, never as agreeing.

The module names no engine: what an engine is like (the digits its sums hold, whether it stores an empty
string as NULL) comes with what was read of its side. A relation is the engine seam's (its ``schema``,
``name``, ``columns`` and ``facts``), a plan is a run's (TABLES.md §3).

The schema guard's pure parts are here too (§7): ``shape`` is what it holds of a relation, ``roles`` what a
run's plan reads each column as, ``changes`` how a shape differs from the one a run was planned on, or from
the one the version's run before it read, and ``verdict`` what follows: a warning, a pause or a refusal.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.classify.patterns import name
from deltasleuth.core.spec import where
from deltasleuth.core.types import SecurityPolicy
from deltasleuth.security.policy import canonical_policy

FORMAT = 1  # of a pair's result (ds.precheck_pairs.result)
NOT = (
    "A table pre-check counts and adds up; it does not compare rows. Tables whose figures agree can hold"
    " different rows, and figures can differ only because two databases store a value differently. It says"
    " where to look first. It is observed, not evidence, readiness or a sign-off, and no evidence pack"
    " contains it: run a comparison to compare rows."
)
# docs/mvp/FILES.md K1: what a pre-check that names a files target, or a spec's file side, is refused with.
FILE_SIDE = (
    "not supported: a table pre-check with a file side; its run counts rows, lines that are not rows and"
    " repeated keys"
)
EVALUATION = "Unlicensed evaluation"
CANCELLED = "not checked: the pre-check was cancelled"
LEVELS = ("schema", "counts", "columns", "distinct")  # each includes the one before
VERDICTS = (
    "unpaired", "refused", "not_read", "rows_differ", "keys_null_or_repeated", "columns_not_compared",
    "values_differ", "incomplete", "schema_differs", "no_difference_observed",
)  # worst first: a pair's verdict is the first that holds  # fmt: skip
COLUMNS, DISTINCT = 200, 40  # the compared columns a pair's figures, and its distinct counts, are read of
REFUSALS = 10  # of a plan, kept
LISTED = 10  # names a line lists before it counts the rest
NAME = 128  # characters of a catalogue's name in a text
Plan = Mapping[str, Any]
Wanted = tuple[str, str | None]  # a figure and the canonical name of its column (None: of the rows)

# The figures compared of a column beside its NULLs, by its class (§4): of two classes, those both have.
_FIGURES = {
    **dict.fromkeys(("text", "char"), ("empty", "length")),
    **dict.fromkeys(("integer", "decimal"), ("sum", "min", "max")),
    **dict.fromkeys(("date", "timestamp", "instant", "time"), ("min", "max")),
    "boolean": ("true",),
    "bytes": ("length",),
}
_READ = {"length": ("shortest", "longest")}  # what a figure is read as, when not as itself
# The order a column's figures are listed in: a stored result holds them in its store's.
ORDER = ("nulls", "empty", "length", "sum", "min", "max", "true", "distinct")
_FLOATS = ("float", "float32")
_NUMBERS = ("integer", "decimal")
# Families, a typeMap entry's, how each reads and how a spec bridges two (ENGINES.md §7, §9, §12 P9): the
# words of ``connectors.tables``, whose plan refuses across engines what a pre-check reports.
_FAMILIES = dict.fromkeys(("text", "char"), "text") | dict.fromkeys(
    ("integer", "decimal", "float", "float32"), "number"
)
_MAPPED = {"string": "text", "integer": "number", "decimal": "number", "timestamp": "mapped timestamp"}
_A = {"text": "text", "bytes": "bytes", "json": "JSON", "instant": "a zoned timestamp"}  # else "a <family>"
_BRIDGES = {
    ("timestamp", "date"): "{{{column}: {{as: date}}}} on side {side}, or {{as: timestamp}} on both",
    ("number", "boolean"): "{{{column}: {{as: boolean}}}} on side {side}",
    ("timestamp", "instant"): "{{as: timestamp, naiveTimezone: ...}} on both",
}
_CASTS = "compare a view that casts it"
_RENAME = "list each in a spec under a name of its own"
# What a side its engine could not pin was read in, by its consistency (``runner.census``'s words).
_UNPINNED = {
    "unverified": "transaction, a snapshot not observable (a MySQL view)",
    "none": "pass, not a snapshot (a MySQL table outside InnoDB)",
}
_CHECKS = {"figures": "figures", "duplicates": "duplicate keys", "distinct": "distinct values"}
_TOTALS = dict(
    zip(
        VERDICTS,
        (
            "unpaired", "a run would refuse", "not read", "with differing row counts",
            "with null or repeated keys", "with columns not compared", "with differing figures",
            "not fully checked", "with schema differences only", "with no difference observed (not parity)",
        ),
        strict=True,
    )
)  # fmt: skip


@dataclass(frozen=True)
class Pair:
    """What a pair is judged by beside its plan: its stored spec (``spec_id`` None: two relations paired
    by name) and that version's name, whether a side lists its ``columns``, the spec's security policy,
    whether the approval gate admits the version (true outside SSO), whether the spec holds what a
    pre-check does not apply (normalisers, tolerances, known-difference rules, ``compare.include`` or
    ``exclude``), and its two targets."""

    spec_id: int | None = None
    named: str = ""  # customer-profile 1.4.0
    listed: bool = False
    security: SecurityPolicy | None = None
    approved: bool = True
    unapplied: bool = False
    targets: tuple[str, str] = ("", "")

    @property
    def label(self) -> str:
        return f"spec {self.spec_id} ({self.named})"


@dataclass(frozen=True)
class Judged:
    """A pair as the store keeps it: its verdict, how many of its figures differ, and its result."""

    verdict: str
    differing: int
    result: dict[str, Any]

    @property
    def rank(self) -> int:
        return rank(self.verdict)

    @property
    def line(self) -> str:
        return line(self.result)


def rank(verdict: str) -> int:
    """A verdict's place in the ladder, 1 the worst: pairs sort by it."""
    return VERDICTS.index(verdict) + 1


def line(result: Mapping[str, Any]) -> str:
    """The verdict's line of a pair's result: what a list prints of the pair."""
    return result["lines"][0]


def worded(verdict: str) -> str:
    """A verdict as words: its code's, and the best one as what it is, never as agreeing (N1)."""
    best = "no difference observed in the checks made (not parity)"
    return best if verdict == VERDICTS[-1] else verdict.replace("_", " ")


# -- the two catalogues ------------------------------------------------------------------------------------


def shape(relation: Any) -> dict[str, Any]:
    """What is held of a relation's catalogue entry (§7): its kind, its primary key and each column the
    account may read, with its declared type, class and whether it may hold NULL, by name. The columns'
    order is not in it."""
    columns = {c.name: [c.declared, c.cls, c.nullable] for c in relation.columns if c.readable}
    return {"kind": relation.facts["kind"], "key": relation.facts.get("key"), "columns": columns}


def sha256(held: Mapping[str, Any]) -> str:
    """A shape's hash: two relations of one hash hold the same columns, whatever their order."""
    return sha256_hex(jsonio.dumps(held, sort_keys=True))


def _family(cls: str, spec: Mapping[str, Any] | None) -> str:
    """The family of a column of class ``cls``, or of its typeMap entry ``spec`` when it has one."""
    if spec is not None:
        return _MAPPED.get(spec["as"], spec["as"])
    return _FAMILIES.get(cls, cls)


def _a(family: str) -> str:
    return _A.get(family, f"a {family}")


def _held(relations: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each side's columns by name."""
    return {s: {c.name: c for c in relations[s].columns} for s in "ab"}


def _sides(canonical: str, plan: Plan, held: Mapping[str, Mapping[str, Any]]) -> tuple[Any, Any]:
    """A compared column's two sides."""
    return held["a"][plan["a"]["columns"][canonical]], held["b"][plan["b"]["columns"][canonical]]


def _unlike(canonical: str, plan: Plan, a: Any, b: Any) -> str | None:
    """The code of a compared column whose two sides are not compared at all (ENGINES.md §12): a type no
    plan reads (P7), one that stays in its engine (P8), two families (P9). None: they are compared."""
    if a.cls is None or b.cls is None:
        return "uncomparable"
    if "other" in (a.cls, b.cls) and plan["a"].get("engine") != plan["b"].get("engine"):
        return "uncomparable"
    families = [_family(c.cls, plan[s]["types"].get(canonical)) for s, c in (("a", a), ("b", b))]
    return "family_differs" if families[0] != families[1] else None


def _entry(code: str, column: str | None, group: str, a: Any = None, b: Any = None) -> dict[str, Any]:
    told = [c and {"declared": c.declared, "cls": c.cls, "nullable": c.nullable} for c in (a, b)]
    return {"code": code, "column": column, "group": group, "a": told[0], "b": told[1]}


def _resized(a: Any, b: Any) -> bool:
    """Whether two columns declare a size in one unit, and the sizes differ."""
    return bool(a.size and b.size and a.size[0] == b.size[0] and a.size != b.size)


def _nullable(kinds: Sequence[str], a: Any, b: Any) -> bool:
    """Whether two base tables' columns differ in whether they may hold NULL (a view's does not say)."""
    return (
        list(kinds) == ["table", "table"]
        and None not in (a.nullable, b.nullable)
        and a.nullable != b.nullable
    )


def _never(a: Any, b: Any) -> bool:
    """Whether two key parts' key texts are never equal (ENGINES.md §8, P6): of two families neither of
    which is text (2024-01-01 is never 2024-01-01 00:00:00), or two numbers of two declared scales, which
    their texts hold (1.00 is never 1). A part of no class says nothing."""
    if None in (a.cls, b.cls):
        return False
    families, scales = [_family(c.cls, None) for c in (a, b)], [_scale(c) for c in (a, b)]
    if "text" not in families and families[0] != families[1]:
        return True
    return families == ["number", "number"] and None not in scales and scales[0] != scales[1]


def schema_diff(plan: Plan, relations: Mapping[str, Any], listed: bool) -> list[dict[str, Any]]:
    """How the two relations differ as their catalogues hold them: a code a column (§4), each with its
    group. ``not_compared``: a comparison would leave the column out (two families, a type it does not
    read, a column on one side only where the plan takes the common ones: no side of the spec lists its
    ``columns``, and of two relations paired by name a column named alike ignoring case by another of its
    side), or would find no key of one side on the other (two key parts whose key texts are never equal).
    ``schema``: a difference of the catalogues alone (a class within one family, a declared size in one
    unit, whether two base tables' columns may hold NULL, a column on one side only that the spec does not
    list, the key). A key part is judged as a column, by side a's name, unless a key map makes its text.
    The key is judged between two base tables that both have a primary key: a planned key that is not a
    side's, or without one two keys that differ."""
    held, found = _held(relations), []
    kinds = [relations[s].facts["kind"] for s in "ab"]
    maps = [[*(plan[s].get("key_maps") or ()), *[None] * len(plan[s]["keys"])] for s in "ab"]
    for key_a, key_b, *mapped in zip(plan["a"]["keys"], plan["b"]["keys"], *maps, strict=False):
        a, b = held["a"][key_a], held["b"][key_b]
        if any(mapped):  # a key map makes the part's key text
            code = None
        elif _never(a, b):
            code = "key_text_differs"
        else:
            code = "key_class_differs" if a.cls != b.cls else "key_size_differs" if _resized(a, b) else None
        if code is not None:
            group = "not_compared" if code == "key_text_differs" else "schema"
            found.append(_entry(code, key_a, group, a, b))
        if _nullable(kinds, a, b):
            found.append(_entry("key_nullable_differs", key_a, "schema", a, b))
    for canonical in plan["a"]["columns"]:
        a, b = _sides(canonical, plan, held)
        code = _unlike(canonical, plan, a, b)
        if code is None:
            code = "class_differs" if a.cls != b.cls else "size_differs" if _resized(a, b) else None
        if code is not None:
            group = "not_compared" if code in ("uncomparable", "family_differs") else "schema"
            found.append(_entry(code, canonical, group, a, b))
        if _nullable(kinds, a, b):
            found.append(_entry("nullable_differs", canonical, "schema", a, b))
    for side in "ab":  # two relations paired by name: each column named alike ignoring case on its side
        for column in plan.get(f"alike_{side}", ()):
            found.append(_entry(f"alike_{side}", column, "not_compared", **{side: held[side][column]}))
    for side, other in ("ab", "ba"):
        lower = {c.lower(): c for c in held[side]}
        if listed:  # a column neither planned nor on the other side
            planned = {*plan[side]["keys"], *plan[side]["columns"].values()}
            there = {c.lower() for c in held[other]}
            only = [c for c in lower if lower[c] not in planned and c not in there]
        else:
            only = plan[f"only_{side}"]
        for canonical in only:
            sides = {side: held[side][lower[canonical]]}
            found.append(_entry(f"only_{side}", canonical, "schema" if listed else "not_compared", **sides))
    keys = [relations[s].facts.get("key") for s in "ab"]
    if kinds == ["table", "table"] and None not in keys:
        if plan["a"]["keys"]:  # the planned key is each side's
            differs = any(set(plan[s]["keys"]) != set(key) for s, key in zip("ab", keys, strict=True))
        else:  # no common primary key was found to plan
            differs = {c.lower() for c in keys[0]} != {c.lower() for c in keys[1]}
        if differs:
            found.append(_entry("key_differs", None, "schema"))
    return found


def implied(a: Any, b: Any, engines: Sequence[str]) -> dict[str, Any]:
    """The plan of two relations paired by name (§3), whose engines are ``engines``: no spec's. The key is
    the primary key when both catalogues show one over the same lower-cased columns, each of a class (side
    b's in side a's order), else none; the columns are every other name both sides read, by its lower-cased
    name, those of two families or of a type no plan reads among them, which no figure compares; no filter,
    no typeMap. A name one side alone reads is in its ``only``. Columns that a side reads under one
    lower-cased name are none of these: each is in that side's ``alike``, which ``schema_diff`` reports.
    Nothing is compiled or stored."""
    relations = {"a": a, "b": b}
    lowered = {s: [c.name.lower() for c in relations[s].columns if c.readable] for s in "ab"}
    alike = {s: {name for name in lowered[s] if lowered[s].count(name) > 1} for s in "ab"}
    read = {
        s: {c.name.lower(): c for c in relations[s].columns if c.readable and c.name.lower() not in alike[s]}
        for s in "ab"
    }
    keys = {s: [column.lower() for column in relations[s].facts.get("key") or ()] for s in "ab"}
    keyed = all(keys.values()) and sorted(keys["a"]) == sorted(keys["b"])
    classed = all(read[s].get(k) and read[s][k].cls for s in "ab" for k in keys["a"])
    key = keys["a"] if keyed and classed else []
    names = [name for name in read["a"] if name in read["b"] and name not in key]
    plan: dict[str, Any] = {}
    for side, engine in zip("ab", engines, strict=True):
        own = read[side]
        held = [own[name] for name in (*key, *names)]
        plan[side] = {
            "schema": relations[side].schema, "name": relations[side].name,
            "keys": [own[k].name for k in key], "key_maps": [None] * len(key),
            "columns": {name: own[name].name for name in names}, "where": None, "types": {}, "digest": [],
            "engine": engine, "classes": {c.name: c.cls for c in held},
            "declared": {c.name: c.declared for c in held},
        }  # fmt: skip
    for side, other in ("ab", "ba"):
        plan[f"only_{side}"] = [name for name in read[side] if name not in lowered[other] and name not in key]
        held = relations[side].columns
        plan[f"alike_{side}"] = [c.name for c in held if c.readable and c.name.lower() in alike[side]]
    return plan


# -- what a level reads ------------------------------------------------------------------------------------


def _compared(
    canonical: str, plan: Plan, held: Mapping[str, Mapping[str, Any]]
) -> tuple[tuple[str, ...], str | None]:
    """The figures compared of a compared column at level ``columns``, and why no other is (None: its
    class has no other). None at all for a column ``_unlike`` names."""
    a, b = _sides(canonical, plan, held)
    if _unlike(canonical, plan, a, b):
        return (), None
    if any(canonical in plan[s]["types"] for s in "ab"):
        return ("nulls",), "typeMap converts this column per row"
    if a.lob or b.lob:
        return ("nulls",), "a large-object column"
    if a.cls in _FLOATS or b.cls in _FLOATS:
        return ("nulls",), "a float sum depends on the order rows are added in"
    return ("nulls", *(f for f in _FIGURES.get(a.cls, ()) if f in _FIGURES.get(b.cls, ()))), None


def _folds(canonical: str, side: Plan, column: Any) -> bool:
    """Whether the side's typeMap reads the column's empty strings as NULL (``emptyAsNull``)."""
    return bool(side["types"].get(canonical, {}).get("emptyAsNull")) and column.cls in ("text", "char")


def _counted(plan: Plan, held: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The compared columns whose distinct values a pair counts: those with every figure of their class."""
    read = list(plan["a"]["columns"])[:COLUMNS]
    return [c for c in read if len(_compared(c, plan, held)[0]) > 1]


def wanted(level: str, plan: Plan, relations: Mapping[str, Any]) -> dict[str, list[Wanted]]:
    """What ``level`` reads of each side beside its catalogue: each figure with the canonical name of its
    compared column (None: of the rows). ``counts``: the rows, and those without a key when the plan
    has one. ``columns``: the figures of the first ``COLUMNS`` compared columns, by the class of both
    sides. ``distinct``: the distinct values of the first ``DISTINCT`` of them that have a class's
    figures."""
    found: dict[str, list[Wanted]] = {"a": [], "b": []}
    if level == "schema":
        return found
    for side in "ab":
        found[side] = [("rows", None)] + [("null_keys", None)] * bool(plan[side]["keys"])
    if level == "counts":
        return found
    held = _held(relations)
    for canonical in list(plan["a"]["columns"])[:COLUMNS]:
        figures = [n for f in _compared(canonical, plan, held)[0] for n in _READ.get(f, (f,))]
        for side, column in zip("ab", _sides(canonical, plan, held), strict=True):
            folded = ["empty"] * (_folds(canonical, plan[side], column) and "empty" not in figures)
            found[side] += [(figure, canonical) for figure in (*figures, *folded)]
    if level == "distinct":
        for side in "ab":
            found[side] += [("distinct", canonical) for canonical in _counted(plan, held)[:DISTINCT]]
    return found


# -- sums --------------------------------------------------------------------------------------------------


def _number(text: Any) -> Decimal | None:
    """A rendered number, exactly; None when it is none, or not a finite one (NaN, Infinity)."""
    try:
        found = Decimal(text)
    except (ArithmeticError, TypeError, ValueError):
        return None
    return found if found.is_finite() else None


def _digits(of: Decimal | int) -> int:
    return len(str(abs(int(of))))


def exact(n: int, m: Decimal, scale: int | None, digits: int | None) -> bool:
    """Whether a side's sum is exact: of ``n`` numbers no larger than ``m``, in a column that declares
    ``scale`` fractional digits (None: none), on an engine whose sums hold ``digits`` digits (None: every
    digit). The largest sum takes the whole digits of ``m``, the scale (a negative one counts 0) and the
    digits of ``n``. A column without a scale may hold any, so a bounded sum of it never is."""
    if digits is None:
        return True
    return scale is not None and _digits(m) + max(scale, 0) + _digits(n) <= digits


def _scale(column: Any) -> int | None:
    """The scale a number column declares: an integer's is 0."""
    if column.cls == "integer":
        return 0
    return column.size[2] if column.size and column.size[0] == "digits" else None


def shown(pair: Pair, plan: Plan, canonical: str) -> str | None:
    """Why a column's sums are withheld (§6); None: they are shown as numbers, where findings already
    show the column's values in clear. Two relations paired by name have no security policy; two filters
    one row apart would give that row's value; the spec's policy must give the column no class; and
    under SSO the approval gate must admit the version."""
    if pair.spec_id is None:
        return "paired by name, no security policy"
    for side in "ab":
        if plan[side]["where"]:
            return f"side {side} declares a filter"
    if _protected(pair, canonical):
        return f"$.{canonical} is protected by the spec's security policy"
    return None if pair.approved else f"spec {pair.spec_id} is not approved"


def _protected(pair: Pair, canonical: str) -> bool:
    """Whether the spec's security policy gives the column a class (no policy protects every column)."""
    return pair.security is None or bool(
        canonical_policy(pair.security).resolve((name(canonical),)).value_class
    )


def filtered(pair: Pair, plan: Plan, canonical: str) -> str | None:
    """Why a column's ``empty``, ``true`` and ``distinct`` counts and its lengths are kept as verdicts
    (§6); None: they are numbers. A side declares a filter and the spec's policy protects the column: a
    filter that selects one row would tell whether its value is true or empty and how long it is, and two
    rows' distinct count whether they hold one value. Its NULLs stay a number, as the rows do."""
    for side in "ab":
        if plan[side]["where"] and _protected(pair, canonical):
            protected = f"$.{canonical} is protected by the spec's security policy"
            return f"side {side} declares a filter and {protected}"
    return None


# -- texts (§10) -------------------------------------------------------------------------------------------


def _cut(text: Any) -> str:
    """A catalogue's name as a text holds it."""
    return str(text) if len(str(text)) <= NAME else str(text)[: NAME - 1] + "…"


def _n(count: int, one: str, several: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else several or one + 's'}"


def _listed(clauses: Sequence[str], by: str = "; ") -> str:
    """The first ``LISTED`` clauses, then how many more there are."""
    more = f"{by}and {len(clauses) - LISTED:,} more" if len(clauses) > LISTED else ""
    return by.join(clauses[:LISTED]) + more


def _key(columns: Sequence[str]) -> str:
    return "(" + ", ".join(_cut(c) for c in columns) + ")"


def _bridge(column: str, families: Sequence[str], names: Sequence[str]) -> str:
    """What to declare for a compared column whose two sides are of two families (ENGINES.md §12 P9)."""
    for first, second in ((0, 1), (1, 0)):
        bridge = _BRIDGES.get((families[first], families[second]))
        if bridge:
            return "declare typeMap " + bridge.format(column=_cut(names[first]), side="ab"[first])
    return f"declare a typeMap that gives both sides one type, or {_CASTS}"


def clause(entry: Mapping[str, Any], plan: Plan) -> str:
    """One schema code of a column, as a line's clause (a draft's comment too: its bridge is never
    applied)."""
    code, column = entry["code"], _cut(entry["column"])
    a, b = entry["a"], entry["b"]
    if code == "key_text_differs":
        families = [_family(side["cls"], None) for side in (a, b)]
        never = f"the key text of {_a(families[0])} is never that of {_a(families[1])}"
        why = "a number key's text holds its scale" if families == ["number", "number"] else never
        both = f"{_cut(a['declared'])} on side a and {_cut(b['declared'])} on side b"
        return f"key column {column} is {both}: {why}, so no key of one side is found on the other: {_CASTS}"
    if code.startswith("key_"):  # a key column's class, size or NULLs, worded as a column's
        return f"key column {clause(entry | {'code': code[4:]}, plan)}"
    if code.startswith("alike_"):
        lowered = _cut(entry["column"].lower())
        return f"{column} and another column of side {code[-1]} both compare as {lowered}: {_RENAME}"
    if code.startswith("only_"):
        return f"{column} is on side {code[-1]} only"
    if code == "nullable_differs":
        said = ["nullable" if side["nullable"] else "NOT NULL" for side in (a, b)]
        return f"{column} is {said[0]} on side a and {said[1]} on side b"
    if code == "uncomparable":
        unread = next((s for s in (a, b) if s["cls"] is None), None)
        other = next(s for s in (a, b) if s["cls"] in (None, "other"))
        beside = "" if unread else " with another engine"
        return f"{column} ({_cut((unread or other)['declared'])}) cannot be compared{beside}: {_CASTS}"
    if code == "family_differs":
        families = [_family(s["cls"], plan[n]["types"].get(entry["column"])) for n, s in (("a", a), ("b", b))]
        names = [plan[s]["columns"][entry["column"]] for s in "ab"]
        both = f"{_a(families[0])} ({_cut(a['declared'])}) on side a and {_a(families[1])} on side b"
        return f"{column} is {both}: {_bridge(entry['column'], families, names)}"
    return f"{column} is {_cut(a['declared'])} on side a and {_cut(b['declared'])} on side b"


def _key_lines(plan: Plan, relations: Mapping[str, Any], differs: bool) -> list[str]:
    """The key's lines: how the primary keys differ, or why they were not checked."""
    keys = {s: relations[s].facts.get("key") for s in "ab"}
    if differs:
        if not plan["a"]["keys"]:
            return [f"key: the primary key is {_key(keys['a'])} on side a and {_key(keys['b'])} on side b"]
        return [
            f"key: keyColumns {_key(plan[s]['keys'])} is not side {s}'s primary key {_key(keys[s])}"
            for s in "ab"
            if set(plan[s]["keys"]) != set(keys[s])
        ]
    for side in "ab":
        kind = relations[side].facts["kind"]
        if kind != "table":
            return [f"key: not checked: side {side} is a {kind} (no primary key)"]
        if keys[side] is None:
            return [f"key: not checked: side {side} has no primary key"]
    return []


def _unread(side: str, check: str, failed: Sequence[str], relation: str | None, target: str) -> str:
    """Why a side's read gave nothing, by its failure's code (§5)."""
    code, said = failed
    of = f"side {side}'s {_CHECKS[check]}" if check in _CHECKS else f"side {side}"
    if code == "timeout":
        hint = "raise precheck.statement_timeout_s, or pre-check a filtered view"
        return f"not checked: {of} timed out after {said} s ({hint})"
    if code == "temp_space":
        lead = f"{_CHECKS[check]} not counted" if check in ("duplicates", "distinct") else "not checked"
        return f"{lead}: side {side} ran out of temporary space ({said})"
    read, untried = _cut(relation or "its table"), f"no other pair of target {target} was tried"
    texts = {
        "unreachable": f"side {side} could not be reached ({said})",
        "auth_refused": f"side {side} refused the credential ({said}); {untried}",
        "denied": f"side {side}'s account may not read {read} or a column of it ({said})",
        "snapshot_lost": f"side {side}'s snapshot was lost ({said})",
    }
    return "not checked: " + texts.get(code, f"side {side}'s read failed ({said})")


def _moment(at: str) -> str:
    return f"{datetime.fromisoformat(at):%Y-%m-%d %H:%M:%S} UTC"


def _read_line(sides: Mapping[str, Mapping[str, Any]], apart: int | None) -> str:
    """How and when each side was read (N5): the two sides are never read at one moment."""
    said = []
    for side, read in sides.items():
        if read.get("at") is None or read.get("relation") is None:
            said.append(f"side {side}: not read")
            continue
        when = f" at {_moment(read['at'])}"
        if side == "b" and apart is not None:
            when = f", {apart:,} s {'earlier' if read['at'] < sides['a']['at'] else 'later'}"
        within = "1 " + _UNPINNED.get(read["consistency"], "snapshot")
        said.append(
            f"side {side}: {read['engine']} {read['server']}, {_cut(read['relation'])}, in {within}{when}"
        )
    return "; ".join(said) + ": not read at one moment"


def unpaired(side: str, names: Sequence[str], label: str) -> str:
    """The line of a catalogue name without a pair: ``names`` holds side ``side``'s relation of that name
    alone (the other side has none), or the several of that side that are alike ignoring case."""
    if len(names) == 1:
        other = "b" if side == "a" else "a"
        return (
            f"no table on side {other} is named {_cut(label)} (side {side}: {_cut(names[0])}; paired by name)"
        )
    count = "two" if len(names) == 2 else f"{len(names):,}"
    alike = ", ".join(_cut(n) for n in names)
    return f"{count} tables of side {side} are named alike ignoring case ({alike}): compare them in a spec"


def alone(level: str, line: str) -> Judged:
    """A catalogue name without a pair, judged as it is made: ``unpaired``'s ``line`` is its result's only
    line, and no side was read."""
    result = {
        "format": FORMAT, "level": level, "refused": [], "sides": {s: _side(None, {}) for s in "ab"},
        "apart_s": None, "schema": [], "keys": {}, "columns": {}, "skipped": [], "lines": [line],
    }  # fmt: skip
    return Judged(VERDICTS[0], 0, result)


def totals(verdicts: Mapping[str, int], pending: int = 0, cancelled: int = 0) -> str:
    """A pre-check's pairs counted by verdict, worst first: never a share, never a score (N2). ``pending``
    pairs are those not read yet."""
    pairs = sum(verdicts.values()) + pending + cancelled
    said = [f"{verdicts[v]:,} {_TOTALS[v]}" for v in VERDICTS if verdicts.get(v)]
    open_ = [f"{count:,} {what}" for count, what in ((pending, "pending"), (cancelled, "cancelled")) if count]
    return f"{_n(pairs, 'pair')}: " + "; ".join(part for part in (", ".join(said), ", ".join(open_)) if part)


def stalled(minutes: int) -> str:
    """The banner of a pre-check no worker has taken."""
    return f"no worker has taken this pre-check for {minutes:,} min: start ds worker, or ds serve"


# -- a pair's figures --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Column:
    """A compared column as read: its canonical name, the pair's plan, and by side its catalogue entry
    (``own``), its figures' rendered texts and what was read of the side."""

    canonical: str
    plan: Plan
    own: Mapping[str, Any]
    texts: Mapping[str, Mapping[str, Any]]
    sides: Mapping[str, Mapping[str, Any]]


def _same(a: Any, b: Any) -> str:
    return "same" if a == b else "differs"


def _nulls(of: _Column, hidden: bool = False) -> dict[str, Any]:
    """The NULLs of a column, as counted: a side whose typeMap declares ``emptyAsNull`` counts its empty
    strings with them, as a comparison would. A side that stores an empty string as NULL (D7) differs
    from one that does not by its empty strings until the spec says so. ``hidden`` (``filtered``): no
    reason tells how many values are empty, or whether any is, since its ``empty`` count is a verdict."""
    found, reasons = {s: int(of.texts[s]["nulls"]) for s in "ab"}, []
    for side in "ab":
        if _folds(of.canonical, of.plan[side], of.own[side]) and "empty" in of.texts[side]:
            empty = int(of.texts[side]["empty"])
            found[side] += empty
            counted = "" if hidden else f"{empty:,} "
            reasons.append(f"side {side}'s {counted}empty string(s) count as NULL (emptyAsNull)")
    for side, other in ("ab", "ba"):
        stores = of.sides[side].get("empty_null") and not of.sides[other].get("empty_null")
        empty = int(of.texts[other].get("empty") or 0)
        texts = "empty" in of.texts[other]  # a column that holds text: an empty string is one of its values
        if stores and not reasons and texts and (hidden or (empty and found[side] - found[other] == empty)):
            column, engine = _cut(of.plan[other]["columns"][of.canonical]), of.sides[side]["engine"]
            bridge = f"declare typeMap {{{column}: {{as: string, emptyAsNull: true}}}} on side {other}"
            if hidden:
                said = (
                    f"{engine.capitalize()} (side {side}) stores as NULL any empty string side {other} holds"
                )
            else:
                said = (
                    f"side {other} holds {empty:,} empty string(s), which {engine.capitalize()} (side {side})"
                )
                said += " stores as NULL"
            reasons.append(f"{said}: {bridge} to compare them as equal")
    verdict = _same(found["a"], found["b"])
    return {"a": found["a"], "b": found["b"], "verdict": verdict, "reason": "; ".join(reasons) or None}


def _bound(of: _Column, figure: str) -> dict[str, Any]:
    """A minimum or a maximum: compared in memory, kept as its verdict alone (D6). Two numbers compare
    exactly; any other rendering, a number that is not finite among them, as its text."""
    a, b = of.texts["a"][figure], of.texts["b"][figure]
    numbers = [_number(text) for text in (a, b)] if all(of.own[s].cls in _NUMBERS for s in "ab") else [None]
    return {"verdict": _same(a, b) if None in numbers else _same(*numbers)}


def _sum(of: _Column, pair: Pair) -> dict[str, Any]:
    """A column's sums: compared exactly, or not compared when either engine can have rounded its own
    (N6), and kept as numbers only where ``shown`` allows."""
    for side in "ab":
        rendered = [of.texts[side][f] for f in ("sum", "min", "max")]
        if any(text is not None and _number(text) is None for text in rendered):
            return {"verdict": "not compared", "reason": f"side {side} holds NaN or Infinity"}
    for side in "ab":
        n = of.sides[side]["rows"] - int(of.texts[side]["nulls"])
        m = max(abs(_number(of.texts[side][f]) or Decimal(0)) for f in ("min", "max"))
        scale, digits, engine = (
            _scale(of.own[side]),
            of.sides[side].get("sum_digits"),
            of.sides[side]["engine"],
        )
        if not exact(n, m, scale, digits):
            holds = f"sum could pass the {digits} digits a{'n' * (engine[:1] in 'aeiou')} {engine} sum holds"
            why = f"{_cut(of.own[side].declared)} declares no scale" if scale is None else holds
            return {"verdict": "not compared", "reason": f"side {side}'s {why}"}
    a, b = (_number(of.texts[s]["sum"]) for s in "ab")
    withheld = shown(pair, of.plan, of.canonical)
    if withheld:
        return {"verdict": _same(a, b), "reason": f"figures withheld: {withheld}"}
    reason = None
    if a is not None and b is not None and a != b:
        reason = f"side {'b' if b < a else 'a'} is {abs(a - b):f} lower"
    said = [None if found is None else f"{found:f}" for found in (a, b)]
    return {"a": said[0], "b": said[1], "verdict": _same(a, b), "reason": reason}


def _figure(of: _Column, figure: str, pair: Pair) -> dict[str, Any]:
    """One figure of a compared column, judged: its two values where they are kept, its verdict and the
    reason beside it."""
    if figure == "nulls":
        return _nulls(of, filtered(pair, of.plan, of.canonical) is not None)
    if figure == "sum":
        return _sum(of, pair)
    if figure in ("min", "max"):
        return _bound(of, figure)
    if figure == "length":  # a side without a value has no length
        spans = [[int(of.texts[s][f]) for f in _READ["length"] if of.texts[s][f] is not None] for s in "ab"]
        found = {"a": spans[0] or None, "b": spans[1] or None, "verdict": _same(*spans)}
    else:
        a, b = (int(of.texts[s][figure]) for s in "ab")
        found = {"a": a, "b": b, "verdict": _same(a, b)}
    withheld = filtered(pair, of.plan, of.canonical)  # empty, length, true, distinct
    return {"verdict": found["verdict"], "reason": f"figures withheld: {withheld}"} if withheld else found


def _columns(
    level: str,
    pair: Pair,
    plan: Plan,
    held: Mapping[str, Any],
    sides: Mapping[str, Any],
    why: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Each compared column read: its two classes, its figures judged, and why no more of them are.
    ``why`` says of each side why a figure of it is missing."""
    found = {}
    for canonical in list(plan["a"]["columns"])[:COLUMNS]:
        own = dict(zip("ab", _sides(canonical, plan, held), strict=True))
        figures, reason = _compared(canonical, plan, held) if level in LEVELS[2:] else ((), f"level {level}")
        texts = {s: sides[s].get("figures", {}).get(canonical, {}) for s in "ab"}
        of, judged = _Column(canonical, plan, own, texts, sides), {}
        for figure in (*figures, *["distinct"] * any("distinct" in texts[s] for s in "ab")):
            unread = [s for s in "ab" for f in _READ.get(figure, (figure,)) if f not in texts[s]]
            judged[figure] = (
                {"verdict": "not checked", "reason": why[unread[0]]} if unread else _figure(of, figure, pair)
            )
        found[canonical] = {"classes": [own["a"].cls, own["b"].cls], "figures": judged, "reason": reason}
    return found


def column_lines(found: Mapping[str, Any]) -> list[str]:
    """A compared column's figures as lines (``found``: its entry in a result's ``columns``): each one's
    two values where they are kept, its verdict and its reason; a minimum and a maximum on one line, with
    no value; last, why no other figure is compared."""
    figures, said = found["figures"], []
    for figure in (listed for listed in ORDER if listed in figures):
        judged = figures[figure]
        verdict, reason = judged["verdict"], judged.get("reason")
        if verdict in ("not compared", "not checked") or (figure not in ("min", "max") and "a" not in judged):
            said.append(f"{figure}: {verdict} ({reason})")  # a figure withheld says why
        elif figure == "min":
            said.append(f"min: {verdict}; max: {figures['max']['verdict']} (values are never shown)")
        elif figure == "length":
            spans = [" to ".join(f"{n:,}" for n in judged[s]) if judged[s] else "no value" for s in "ab"]
            unit = "octets" if "bytes" in found["classes"] else "characters"
            said.append(f"length: side a {spans[0]}, side b {spans[1]} {unit} ({verdict})")
        elif figure == "sum":
            sums = [judged[s] or "no value" for s in "ab"]
            said.append(f"sum: side a {sums[0]}, side b {sums[1]} ({reason or verdict})")
        elif figure != "max":
            counts = f"side a {judged['a']:,}, side b {judged['b']:,}"
            said.append(f"{figure}: {counts} ({verdict})" + (f"; {reason}" if reason else ""))
    return said + [f"not compared: {found['reason']}"] * bool(found.get("reason"))


# -- a pair's verdict --------------------------------------------------------------------------------------


def _side(relation: Any, read: Mapping[str, Any]) -> dict[str, Any]:
    """What a result keeps of a side: how and when it was read, its relation's shape and each failed
    check's code."""
    found = {member: read.get(member) for member in ("engine", "at")}
    found |= dict.fromkeys(("server", "consistency", "relation", "kind", "sha256"))
    if relation is not None:
        found |= {member: relation.facts[member] for member in ("server", "consistency", "kind")}
        found |= {"relation": f"{relation.schema}.{relation.name}", "sha256": sha256(shape(relation))}
    return found | {"failed": {check: failed[0] for check, failed in read.get("failed", {}).items()}}


def _apart(sides: Mapping[str, Mapping[str, Any]]) -> int | None:
    """The seconds between the two sides' reads."""
    at = [side.get("at") for side in sides.values()]
    if None in at:
        return None
    return abs(round((datetime.fromisoformat(at[1]) - datetime.fromisoformat(at[0])).total_seconds()))


def _counts(level: str, plan: Plan, keys: Mapping[str, Mapping[str, Any]]) -> list[tuple[int, str]]:
    """What the rows and the keys say, each line with its verdict's rank."""
    told: list[tuple[int, str]] = []
    a, b = keys["a"]["rows"], keys["b"]["rows"]
    if None not in (a, b) and a != b:
        gap = f"{abs(a - b):,} fewer on side {'b' if b < a else 'a'}"
        told.append((4, f"rows differ: side a {a:,}, side b {b:,} ({gap})"))
    for side in "ab":
        if keys[side]["null_keys"]:
            why = "(NULL, or its key map gives none): a scan does not compare them"
            told.append((5, f"keys: {keys[side]['null_keys']:,} row(s) of side {side} have no key {why}"))
        if keys[side]["duplicate_keys"]:
            held = f"are held by more than one row ({keys[side]['rows_in_duplicates']:,} rows)"
            repeated = f"{keys[side]['duplicate_keys']:,} key(s) of side {side} {held}"
            told.append((5, f"keys: {repeated}: per key they read identity_ambiguous"))
    if level != "schema" and not plan["a"]["keys"]:
        told.append((11, "keys: not checked: no common primary key"))
    return told


def _skipped(
    asked: str,
    level: str,
    pair: Pair,
    plan: Plan,
    held: Mapping[str, Any],
    sides: Mapping[str, Any],
    most: int,
) -> list[tuple[str, str]]:
    """Each check of the level asked that a limit, or an unapproved filter (D15), left out: its code and
    its line."""
    found = []
    compared, counted = len(plan["a"]["columns"]), len(_counted(plan, held))
    if LEVELS.index(level) < LEVELS.index(asked):
        why = "declares a filter and is not approved: only its schema was read"
        found.append(("unapproved_filter", f"not checked: {pair.label} {why}"))
    if level in LEVELS[2:] and compared > COLUMNS:
        read = f"the first {COLUMNS} were read"
        found.append(("columns", f"not compared: {compared:,} columns are compared and {read}"))
    if level == "distinct" and counted > DISTINCT:
        of = f"{counted - DISTINCT:,} of {counted:,} compared columns"
        found.append(
            ("distinct_columns", f"distinct values not counted for {of}: the first {DISTINCT} were read")
        )
    for side in "ab":  # both checks sort or hash: the side's duplicate keys and its distinct values
        if "distinct_rows" in sides[side].get("skipped", ()):
            over = (
                f"{sides[side]['rows']:,} rows on side {side} is over precheck.distinct_max_rows ({most:,})"
            )
            checks = {"duplicate keys": plan[side]["keys"], "distinct values": counted}
            skipped = " and ".join(what for what, made in checks.items() if made)
            found.append(("distinct_rows", f"{skipped} not counted: {over}"))
    return found


def compare(
    asked: str,
    level: str,
    pair: Pair,
    plan: Plan | None,
    relations: Mapping[str, Any],
    sides: Mapping[str, Mapping[str, Any]],
    refused: Sequence[str] = (),
    *,
    distinct_max_rows: int = 0,
) -> Judged:
    """Judge a pair read at ``level`` (below the level ``asked`` when an unapproved filter kept its rows
    unread, D15). ``plan`` is the pair's (None when a run of it would be ``refused``, or a side's catalogue
    was not read); ``relations`` holds each side's relation (None: not described); ``sides`` what was read
    of each: its ``engine``, when it was read (``at``), its engine's ``sum_digits`` and ``empty_null``, and
    ``connectors.tables.figures.read``'s members, ``failed`` naming each check that gave nothing with its
    code and what a line shows of it. The result is format 1; its lines come worst first."""
    kept = {s: _side(relations.get(s), sides.get(s, {})) for s in "ab"}
    result: dict[str, Any] = {
        "format": FORMAT, "level": level, "refused": list(refused)[:REFUSALS], "sides": kept,
        "apart_s": _apart(kept), "schema": [], "keys": {}, "columns": {}, "skipped": [],
    }  # fmt: skip
    told: list[tuple[int, str]] = []  # each line with the rank of the verdict it argues (11: of none)
    if refused:
        more = f" (and {len(refused) - 1:,} more)" if len(refused) > 1 else ""
        of = "this spec" if pair.spec_id is not None else "these tables"
        would = f"a run of {of} would be refused: {refused[0]}{more}"
        told.append((2, FILE_SIDE if refused[0].endswith(FILE_SIDE) else would))  # a run of it is not
    unread = {
        s: [
            _unread(s, check, failed, kept[s]["relation"], pair.targets["ab".index(s)])
            for check, failed in sides.get(s, {}).get("failed", {}).items()
        ]
        for s in "ab"
    }
    told += [(3, text) for s in "ab" for text in unread[s]]
    if plan is not None and None not in (relations.get("a"), relations.get("b")):
        held = _held(relations)
        result["names"] = {  # each side's relation, key and compared columns as its catalogue spells them
            s: {"schema": relations[s].schema, "name": relations[s].name, "keys": plan[s]["keys"],
                "columns": dict(plan[s]["columns"])}
            for s in "ab"
        }  # fmt: skip
        result["schema"] = schema_diff(plan, relations, pair.listed)
        counts = ("rows", "null_keys", "duplicate_keys", "rows_in_duplicates")
        result["keys"] = {
            s: {c: sides[s].get(c) for c in counts} | {"primary": relations[s].facts.get("key")} for s in "ab"
        }
        told += _counts(level, plan, result["keys"])
        apart = [e for e in result["schema"] if e["group"] == "not_compared"]
        told += [(6, clause(e, plan)) for e in apart if e["code"] == "key_text_differs"]  # no key would meet
        left = [(e["column"], clause(e, plan)) for e in apart if e["code"] != "key_text_differs"]
        if left:
            listed = _listed([text for _, text in left])
            told.append((6, f"{_n(len(left), 'column')} would not be compared: {listed}"))
        over = "holds over precheck.distinct_max_rows rows"
        why = {s: unread[s][0].split(": ", 1)[1] if unread[s] else f"side {s} {over}" for s in "ab"}
        result["columns"] = _columns(level, pair, plan, held, sides, why)
        for column, text in left:  # a compared column says why it has no figure
            if column in result["columns"]:
                result["columns"][column]["reason"] = text
        differ = {
            c: [f for f, judged in found["figures"].items() if judged["verdict"] == "differs"]
            for c, found in result["columns"].items()
        }
        named = [f"{_cut(c)} ({', '.join(figures)})" for c, figures in differ.items() if figures]
        if named:
            read = sum(bool(found["figures"]) for found in result["columns"].values())
            of = f"{len(named):,} of {read:,} compared columns"
            told.append((7, f"{of} differ in a figure: {_listed(named, ', ')}"))
        for code, text in _skipped(asked, level, pair, plan, held, sides, distinct_max_rows):
            result["skipped"].append(code)
            told.append((8, text))
        schema = [clause(e, plan) for e in result["schema"] if e["group"] == "schema" and e["column"]]
        if schema:
            told.append((9, f"schema: {_n(len(schema), 'difference')}: {_listed(schema)}"))
        differs = any(e["code"] == "key_differs" for e in result["schema"])
        told += [(9 if differs else 11, text) for text in _key_lines(plan, relations, differs)]
    worst = min((found[0] for found in told), default=10)
    if worst > 9:
        best = f"no difference observed in the checks made (level {level}): not parity"
        told.append((10, f"{best}; run a comparison to compare rows"))
    if pair.unapplied:
        unapplied = "normalisers, tolerances, known-difference rules and compare.include or exclude"
        told.append((11, f"not applied: this spec's {unapplied}; a comparison applies them"))
    told.append((11, _read_line(kept, result["apart_s"])))
    result["lines"] = [text for _, text in sorted(told, key=lambda found: found[0])]
    differing = sum(
        judged["verdict"] == "differs"
        for found in result["columns"].values()
        for judged in found["figures"].values()
    ) + any(found[0] == 4 for found in told)
    return Judged(VERDICTS[min(worst, 10) - 1], differing, result)


# -- the schema guard (§7) ---------------------------------------------------------------------------------

MODES = ("finding", "pause")  # schemaDrift.mode: what a change that breaks nothing does to a run
# A change's closed kinds. The first group stops a run: its plan's statements would read another table, or
# a new run would not compare what the version's run before it compared.
BREAKING = (
    "relation_missing", "column_missing", "class_changed", "key_render_changed", "render_changed",
    "compared_lost",
)  # fmt: skip
OTHER = ("column_added", "column_removed", "type_changed", "nullable_changed", "key_changed", "kind_changed")
_SIZED = re.compile(r"\(\s*([0-9*]+)\s*(?:,\s*(-?[0-9]+)\s*)?\)")  # a declared type's (p) or (p,s)
# What a side's engine builds its statements of from a planned column of a declared type (the engine seam's
# ``built``: its column, class, declared type and whether it is a key column); None: of no declared type.
Built = Callable[[str, str, str, bool], "str | None"]


def roles(side: Plan) -> dict[str, str]:
    """What a table side's plan reads each of its columns as: a ``key``, a ``compared`` column, one its
    ``filter`` names or one of a sample's ``strata``; of several, the first."""
    filtered = sorted(where.columns(where.parse(side["where"]))) if side["where"] else []
    found = dict.fromkeys(side.get("strata", {}).values(), "strata") | dict.fromkeys(filtered, "filter")
    return found | dict.fromkeys(side["columns"].values(), "compared") | dict.fromkeys(side["keys"], "key")


def _rendering(column: str, held: Sequence[Any], built: Built | None) -> tuple[Any, ...]:
    """What a key column's key text is built from (ENGINES.md §8): a number's scale (an integer's is 0;
    None: none), which the text of a value holds on every engine, and the key text the side's engine builds
    of the declared type (``built``: an Oracle ``NUMBER(p,s)``'s mask holds its precision too)."""
    declared, cls, scale = held[0], held[1], None
    if cls in _NUMBERS:
        sized = _SIZED.search(declared)
        scale = 0 if cls == "integer" else int(sized.group(2) or 0) if sized else None
    return scale, built and built(column, cls, declared, True)


def _rendered(column: str, held: Sequence[Any], built: Built | None) -> str | None:
    """How the side's engine renders a planned column of the declared type it held (ENGINES.md §9)."""
    return built and built(column, held[1], held[0], False)


def _change(kind: str, column: str | None = None, was: Any = None, now: Any = None) -> dict[str, Any]:
    return {"kind": kind, "breaking": kind in BREAKING, "column": column, "was": was, "now": now}


def changes(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    roles: Mapping[str, str],
    *,
    basis: Mapping[str, str] | None = None,
    built: Built | None = None,
) -> list[dict[str, Any]]:
    """How a relation's shape ``after`` (None: it is gone, or the account reads none of it) differs from
    ``before`` for a run whose plan reads the columns of ``roles``: each change's closed ``kind``, whether
    it is ``breaking``, its ``column`` and what the column was and is; the breaking ones first. ``built`` is
    what the side's engine builds its statements of from a column's declared type.

    Against the run's own plan (``before`` is the shape it was planned on) a planned column that is gone, or
    of another class, breaks, as does a key column whose key text would render differently (``_rendering``)
    and a compared or strata column the engine renders otherwise of its new declared type (``_rendered``:
    Oracle's ``DATE`` and ``TIMESTAMP``, MySQL's ``bit``). At a run's creation ``before`` is the shape the
    version's run before it was planned on and ``basis`` that run's roles: a column it compared that this
    run does not, and a column both plan whose class or key rendering differs, narrow the comparison. Any
    other difference breaks nothing. Without ``before`` (no shape was held) only what is missing is
    judged."""
    if after is None:
        return [_change("relation_missing")]
    held, columns = (before or {}).get("columns", {}), after["columns"]
    if basis is None:
        gone, read = [column for column in roles if column not in columns], roles
        found = [_change("column_missing", column, held.get(column)) for column in gone]
    else:
        lost = [c for c, role in basis.items() if role == "compared" and roles.get(c) != "compared"]
        found = [_change("compared_lost", column, held.get(column), columns.get(column)) for column in lost]
        read = {column: role for column, role in roles.items() if column in basis}
    told = {change["column"] for change in found}
    for column in sorted(held.keys() | columns.keys()):
        was, now = held.get(column), columns.get(column)
        if now is None and column not in told:
            found.append(_change("column_removed", column, was))
        elif was is None and before is not None:
            found.append(_change("column_added", column, None, now))
        elif was is not None and now is not None:
            role = read.get(column)
            renders = basis is None and role in ("compared", "strata")  # the run's own statements render it
            if column in read and was[1] != now[1]:
                found.append(_change("class_changed", column, was, now))
            elif role == "key" and _rendering(column, was, built) != _rendering(column, now, built):
                found.append(_change("key_render_changed", column, was, now))
            elif renders and _rendered(column, was, built) != _rendered(column, now, built):
                found.append(_change("render_changed", column, was, now))
            elif was[:2] != now[:2]:
                found.append(_change("type_changed", column, was, now))
            if None not in (was[2], now[2]) and was[2] != now[2]:
                found.append(_change("nullable_changed", column, was, now))
    if before is not None:
        found += [
            _change(f"{member}_changed", None, before[member], after[member])
            for member in ("key", "kind")
            if before[member] != after[member]
        ]
    return sorted(found, key=lambda change: not change["breaking"])


def verdict(mode: str, found: Sequence[Mapping[str, Any]], *, creation: bool = False) -> str | None:
    """What the changes ``found`` do to a run under ``schemaDrift.mode`` ``mode`` (D10): ``paused``,
    ``warned`` or, at its creation, ``refused``; None without a change. A breaking change always pauses;
    a narrowing one creates the run paused, or refuses it under ``pause``: a run that could only be
    cancelled would hold its schedule. Any other change warns, or pauses under ``pause``."""
    if not found:
        return None
    if any(change["breaking"] for change in found):
        return "refused" if creation and mode == "pause" else "paused"
    return "paused" if mode == "pause" else "warned"
