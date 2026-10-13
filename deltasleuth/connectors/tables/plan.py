"""The column plan (TABLES.md §3, §9, §10): each table side resolved against its catalogue, through
its target's engine (ENGINES.md §5).

Names resolve as spelled, else as the engine folds or matches an unquoted name. A plan is stored with
its run, so the run reads the same columns, filter and maps even after a table changes. A pair with a
side on MySQL or Oracle also stores each table side's engine and its columns' classes, checked across
the pair (ENGINES.md §7, §8), and which compared columns the worker hashes: the engine hashes the others
(§10). Problems raise ``ValueError`` naming sides, tables and columns, never values; ``issues`` lists
them without raising, for a table pre-check (docs/mvp/PRECHECKS.md §5).
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from deltasleuth.connectors.tables import duck, render
from deltasleuth.connectors.tables.connector import relation
from deltasleuth.connectors.tables.engine import (
    ERRORS,
    REFUSED,
    Engine,
    NotReady,
    Refusal,
    Relation,
    Session,
    TableError,
    code,
    of,
)
from deltasleuth.connectors.tables.keys import Plan, key_maps
from deltasleuth.core import jsonio
from deltasleuth.core.spec import TargetRegistry, where
from deltasleuth.core.types import CompiledSpec, SideConfig, Target

_A = {"text": "text", "bytes": "bytes", "json": "JSON", "instant": "a zoned timestamp"}  # else "a <family>"
# How the approved spec bridges two families (ENGINES.md §9); the first is the side that declares the type.
_BRIDGES = {
    ("timestamp", "date"): "{{{column}: {{as: date}}}} on side {side}, or {{as: timestamp}} on both",
    ("number", "boolean"): "{{{column}: {{as: boolean}}}} on side {side}",
    ("timestamp", "instant"): "{{as: timestamp, naiveTimezone: ...}} on both",
}
_CASTS = "compare a view that casts it"
_FILE_KEY = "cannot be a key in a file: export it as text, a whole number, a decimal or a date"  # FILES.md P6
# What a CSV column's text pairs with, declared (FILES.md P6, P9): the csv.keys or typeMap type of a family.
_AS = {"number": "decimal", "instant": "timestamp", "mapped timestamp": "timestamp"} | {
    family: family for family in ("date", "timestamp", "boolean", "json", "bytes")
}


class Unconnected(ValueError):
    """A connect refused when a run is created (ENGINES.md §12 P1). Its message names the endpoint the
    target declares, for whoever creates the run; ``brief`` names the side and the target alone, which is
    all the chat's model is told (§14 point 9); ``cause`` is what refused it."""

    def __init__(self, who: str, target: Target, why: str, cause: BaseException | None = None) -> None:
        super().__init__(f"{who}: cannot connect to target {_endpoint(target)}: {why}")
        self.brief = f"{who}: cannot connect to target {target.target_id}"
        self.cause = cause


@dataclass(frozen=True)
class Planned:
    """A pair's plan (None when no side is a table) and each table side's relation as its catalogue held
    it when the plan was made; a file side's pins (docs/mvp/FILES.md §6), which its run seals and its plan
    never holds."""

    plan: Plan | None
    relations: dict[str, Relation]
    pinned: dict[str, Plan] = field(default_factory=dict)


async def plan(targets: TargetRegistry, spec: CompiledSpec) -> Plan | None:
    """The columns, filter and maps a run reads by, and a hash sample's frame's strata columns (``strata``,
    docs/mvp/SAMPLING.md §3); None when no side is a table."""
    return (await planning(targets, spec)).plan


async def planning(targets: TargetRegistry, spec: CompiledSpec, *, scanning: bool = False) -> Planned:
    """``plan``, with the relations it was made of: each table side is described on a session of its
    own, the pair is planned from the two catalogues alone (``resolved``), then each side is probed. A file
    side is described by its files, pinned, and its probe counts its rows into its plan (docs/mvp/FILES.md
    §6); its relation is then the one its plan holds, as its later looks see it, and a pass over it must fit
    the spec's timeout (P31), and, ``scanning``, the sort of the run's file sides ``files.spill`` (P32)."""
    tables = _tables(spec)
    relations = {}
    filed = any(targets.live(cfg.target).engine == "files" for cfg in tables.values())
    # One files.plan_s for the run's file sides, and few creations planning files at once (FILES.md §10).
    async with duck.creating() if filed else contextlib.nullcontext():
        for name, cfg in tables.items():
            target, table = targets.live(cfg.target), cfg.table
            assert table is not None
            session = await _connected(of(target), target, f"side {name}")
            try:
                relations[name] = await described(
                    of(target), session, f"side {name}", table.schema, table.name, side=cfg
                )
            finally:
                await session.close()
        if not tables:
            return Planned(None, relations)
        found = resolved(targets, spec, relations)
        for name, cfg in tables.items():
            await _probe(targets.live(cfg.target), name, found[name])
    pinned = {name: found[name].pop("pinned") for name in tables if "pinned" in found[name]}
    for name in pinned:
        relations[name] = duck.described(found[name])
        pinned[name].pop("end")
        pinned[name].pop("footed")  # its footers' figures, which its probe took: no part of its pins
    _budgets(spec, found, pinned, scanning)
    return Planned(found, relations, pinned)


async def key_plan(targets: TargetRegistry, cfg: SideConfig) -> Plan:
    """The plan of a key table (``assemble.key_table``'s side), stored nowhere: its relation described, its
    key columns, key maps and filter planned as a table side's with no compared column, then probed; a files
    target's files are pinned and counted as a file side's, their pins left as its readers take them. What a
    table side's plan refuses is refused in its words, ``the key source`` where its side stands."""
    target, table, name, who = targets.live(cfg.target), cfg.table, cfg.side.value, "the key source"
    assert table is not None
    engine, refused = of(target), []
    try:
        async with duck.creating() if target.engine == "files" else contextlib.nullcontext():
            session = await _connected(engine, target, who)
            try:
                found = await described(engine, session, who, table.schema, table.name, side=cfg)
            finally:
                await session.close()
            side = _side_plan(target, cfg, found, (), refused)
            if not refused and target.engine != "postgresql":
                _classed(name, side, target.engine or "", found, refused)
            if refused:
                raise ValueError(refused[0])
            side["digest"] = []  # no compared column: none is hashed
            if "pinned" in side:
                side["pinned"]["who"] = who  # what its probe's refusals and log line name
            session = await _connected(engine, target, who)
            try:
                await probed(engine, session, name, side)
            finally:
                await session.close()
    except ValueError as exc:
        worded = f"side {name}:"  # a table side's refusal
        if not str(exc).startswith(worded):
            raise
        said = str(exc).replace(worded, f"{who}:", 1)
        raise (NotReady(exc.code, said) if isinstance(exc, NotReady) else ValueError(said)) from None
    for member in ("end", "footed", "who"):  # its creation's alone, as ``planning`` leaves a file side's pins
        side.get("pinned", {}).pop(member, None)
    return side


def _budgets(spec: CompiledSpec, found: Plan, pinned: Mapping[str, Plan], scanning: bool) -> None:
    """FILES.md §6 step 8: a per-key read is one pass over a side's files, so two must fit the spec's total
    timeout (P31); a scan sorts its file sides' ``sort_bytes``, which ``files.spill`` must hold (P32)."""
    total = spec.execution.total_timeout_s
    for name in pinned:
        if 2 * found[name]["pass_s"] > total:
            took, need = found[name]["pass_s"], math.ceil(2 * found[name]["pass_s"])
            raise ValueError(
                f"side {name}: one pass over its files took {took:g} s and a per-key read is one pass: set"
                f" the spec's execution.timeouts.total to {need}s or more (it is {total:g}s), or export"
                " Parquet"
            )
    sort = sum(found[name]["sort_bytes"] for name in pinned)
    spill = duck.manifest.size_bytes(duck._configured().files.spill) if pinned else 0
    if scanning and sort > spill:
        name = max(pinned, key=lambda side: found[side]["sort_bytes"])
        rows, need = found[name]["selected"], math.ceil(sort / 2**30)
        raise ValueError(
            f"side {name}: sorting its {rows:,} rows needs about {need} GiB of temporary space and"
            f" files.spill allows {spill // 2**30} GiB: raise files.spill with the volume under"
            " files.spill_dir, list fewer columns, or filter the rows"
        )


def sealed(sealer: Any, pinned: Mapping[str, Plan]) -> dict[str, dict[str, str]]:
    """Each file side's pins as its run stores them (``params.pinned``, docs/mvp/FILES.md §6), sealed by the
    run's sealer as a scan's resume point is: no plan, pack, audit row or answer holds a file's size, times
    or digest, and they go with the run's data key."""
    return {
        side: {"enc": base64.b64encode(sealer.seal("pinned", 0, side, jsonio.dumps(held).encode())).decode()}
        for side, held in sorted(pinned.items())
    }


def unsealed(sealer: Any, params: Mapping[str, Any]) -> Plan | None:
    """A run's plan (``params["tables"]``) with each file side's pins opened into it as its readers take them
    (``pinned``: its side, its files in path order and their pins)."""
    tables = params.get("tables")
    if not tables or not params.get("pinned"):
        return tables
    opened = {
        side: json.loads(sealer.open("pinned", 0, side, base64.b64decode(held["enc"])))
        for side, held in params["pinned"].items()
    }
    return pins(tables, opened)


def pins(tables: Plan, pinned: Mapping[str, Plan]) -> Plan:
    """``tables`` with each file side's ``pinned`` held, as a run's readers take a plan."""
    return tables | {side: tables[side] | {"pinned": held | {"side": side}} for side, held in pinned.items()}


def resolved(targets: TargetRegistry, spec: CompiledSpec, relations: Mapping[str, Relation]) -> Plan:
    """The plan of a pair with a table side, from each table side's relation alone: no session. The
    first of ``issues`` is raised."""
    refused, found = _planned(targets, spec, relations)
    if refused:
        raise ValueError(refused[0])
    return found


def issues(targets: TargetRegistry, spec: CompiledSpec, relations: Mapping[str, Relation]) -> list[str]:
    """What the catalogues refuse of the pair, in the order ``plan`` raises it. The plan is made in
    steps (each side's names and grants; the pair's counterparts; what each side compares; with a side
    on MySQL or Oracle, the classes it reads, then the pair's keys and families); every refusal of the
    first step that refuses is listed, since a later step is judged on what the earlier ones gave."""
    return _planned(targets, spec, relations)[0]


def _tables(spec: CompiledSpec) -> dict[str, SideConfig]:
    return {cfg.side.value: cfg for cfg in spec.sides.values() if cfg.table is not None}


def _planned(
    targets: TargetRegistry, spec: CompiledSpec, relations: Mapping[str, Relation]
) -> tuple[list[str], Plan]:
    """``issues`` and the plan; the plan is whole only when nothing is refused."""
    tables = _tables(spec)
    members = spec.sample.members if spec.sample else ()
    strata = {m.side.value: m.columns for m in members if m.kind == "table"}
    refused: list[str] = []
    found = {
        name: _side_plan(targets.live(cfg.target), cfg, relations[name], strata.get(name, ()), refused)
        for name, cfg in tables.items()
    }
    result: Plan = {**found, "only_a": [], "only_b": []}
    if refused:
        return refused, result
    if len(found) == 2:
        a, b = found["a"]["columns"], found["b"]["columns"]
        for name, other, own, theirs in (("a", "b", a, b), ("b", "a", b, a)):
            missing = [c for c in own if c not in theirs]
            if tables[name].table.columns is not None:  # type: ignore[union-attr]
                refused += [f"side {name}: column {c!r} has no counterpart on side {other}" for c in missing]
            result[f"only_{name}"] = missing
        if refused:
            return refused, result
        found["a"]["columns"] = {c: a[c] for c in a if c in b}
        found["b"]["columns"] = {c: b[c] for c in found["a"]["columns"]}
    for name, side in found.items():  # two empty rows would be MATCH to the scan, INCONCLUSIVE per key
        compared = list(side["columns"].values())  # a column on one side only is never read
        if targets.live(tables[name].target).engine == "postgresql":
            _readable_names(name, compared, refused)
        readable = {c.name for c in relations[name].columns if c.readable}
        _granted(name, relation(side), compared, readable, refused)
        if not side["columns"]:
            why = "list the columns to compare; a key column listed alone compares which keys exist"
            refused.append(f"side {name}: no column to compare: {why}")
        _canonical_types(name, side, refused)
    if refused:
        return refused, result
    digest = sorted({c for side in found.values() for c in side["types"]})  # converted on both sides
    engines = {name: targets.live(cfg.target).engine or "" for name, cfg in tables.items()}
    if set(engines.values()) != {"postgresql"}:
        for name, side in found.items():
            _classed(name, side, engines[name], relations[name], refused)
        if len(found) == 2 and not refused:
            _paired(found, engines, refused)
        if refused:
            return refused, result
        if len(found) == 2:
            digest = _worker_columns(found, engines, relations)
        result["rendering"] = render.RENDERING
    for side in found.values():
        side["digest"] = digest
    return refused, result


def _side_plan(
    target: Target, cfg: SideConfig, found: Relation, strata: Sequence[str], refused: list[str]
) -> Plan:
    """The side's plan from its relation as the catalogue holds it (compared columns are checked once
    the pair has chosen them); ``strata`` columns are resolved and granted as filter columns are. What it
    refuses joins ``refused``; a name that is no column stays as spelled, and is judged no further."""
    table, side = cfg.table, cfg.side.value
    assert table is not None
    engine = of(target)
    schema, name, present = found.schema, found.name, [c.name for c in found.columns]
    shown = f"{schema}.{name}" if schema else name  # a file side's label has no schema
    readable = {c.name for c in found.columns if c.readable}
    likeable = {c.name for c in found.columns if c.likeable}

    def column(spelled: str) -> str:  # as spelled, else as the engine folds or matches an unquoted name
        if spelled in present:
            return spelled
        fold = engine.fold or str.lower
        folded = [c for c in present if (c if engine.fold else c.lower()) == fold(spelled)]
        if len(folded) == 1:
            return folded[0]
        hidden = ", or the role may not read it: grant it SELECT" if engine.hides else ""
        _refuse(refused, f"side {side}: column {spelled!r} is not in {shown}{hidden}")
        return spelled

    keys = [column(k) for k in table.key_columns]
    pairs = (
        [(column(c), canonical) for c, canonical in table.columns]
        if table.columns is not None
        else [(c, c.lower()) for c in present if c not in keys]
    )
    columns: dict[str, str] = {}
    for col, canonical in pairs:
        if canonical in columns:
            clash = f"columns {columns[canonical]!r} and {col!r} both compare as {canonical!r}"
            _refuse(refused, f"side {side}: {clash}")
        columns.setdefault(canonical, col)
    filtered = None if table.where is None else where.resolve(where.parse(table.where), column)
    filter_columns = list(where.columns(filtered)) if filtered else []
    stratified = {name: column(name) for name in strata}  # a frame's rows, read under their names
    filter_columns += stratified.values()
    for unlike in [c for c in _like_columns(filtered) if c in present and c not in likeable]:
        why = "LIKE and ILIKE match text in a deterministic collation: compare a view that casts it"
        _refuse(refused, f"side {side}: column {unlike!r} cannot be matched: {why}")  # else only on a row
    if target.engine == "postgresql":  # compared columns once they are known
        _readable_names(side, [schema, name, *keys, *filter_columns], refused)
    listed = list(columns.values()) if table.columns is not None else []
    read = [c for c in [*keys, *filter_columns, *listed] if c in present]
    _granted(side, shown, read, readable, refused)
    return {
        "schema": schema,
        "name": name,
        "keys": keys,
        "key_maps": list(table.key_maps),
        "columns": columns,
        "where": None if filtered is None else where.text(filtered),
        "types": {column(c): dict(spec) for c, spec in table.types.items()},  # by column until compared
    } | ({"strata": stratified} if stratified else {})


def _refuse(refused: list[str], message: str) -> None:
    """A refusal is listed once, where it was first met."""
    if message not in refused:
        refused.append(message)


async def preflight(target: Target, table: str | None = None) -> tuple[dict[str, Any], Relation | None]:
    """``ds target check`` (ENGINES.md §13): a session on a database target as a run opens one, what it
    says of its server, and the relation ``table`` ([schema.]name) as a plan would resolve it. It reads no
    row. A refusal is §12's, ``target <id>`` in place of the side."""
    engine, who = of(target), f"target {target.target_id}"
    session = await _connected(engine, target, who)
    try:
        server = await engine.server(session)
        if table is None:
            return server, None
        schema, _, name = table.rpartition(".")
        return server, await described(engine, session, who, schema or None, name)
    except Refusal as exc:  # a role that may not execute what orders its text keys
        raise ValueError(f"{who}: {exc}") from None
    except ERRORS as exc:  # the session ended, or the server answered with a code (P14)
        said = "the session's account of its server could not be read"
        raise ValueError(f"{who}: {said}: {_answered(exc)}") from None
    finally:
        await session.close()


async def _connected(engine: Engine, target: Target, who: str) -> Session:
    """A session on the target of ``who`` (``side a``); a refused connect is worded for whoever creates the
    run (ENGINES.md §12 P1 to P4)."""
    try:
        return await engine.connect(target)
    except REFUSED as exc:
        raise Unconnected(who, target, _why(exc), exc) from None
    except Refusal as exc:  # a server or a session the engine does not read
        raise ValueError(f"{who}: {exc}") from None


async def described(
    engine: Engine, session: Session, who: str, schema: str | None, name: str, *, side: Any = None
) -> Relation:
    """The relation as its catalogue holds it; one that is missing, or that the role may not read, is
    refused (ENGINES.md §12 P12), as is one the database answers with an error for (P14: an invalid view, a
    session that ended), by its code alone. ``side`` is what a file side is described by (its
    ``SideConfig``): its refusals are worded for ``who`` (docs/mvp/FILES.md §15)."""
    shown = f"{schema}.{name}" if schema else name
    try:
        found = await engine.describe(session, schema, name, side=side)
    except Refusal as exc:  # a synonym the engine does not follow
        raise ValueError(f"{who}: {exc}") from None
    except ERRORS as exc:
        raise ValueError(f"{who}: table or view {shown} cannot be described: {_answered(exc)}") from None
    if found is None:
        raise ValueError(f"{who}: table or view {shown} not found or not visible")
    if not any(c.readable for c in found.columns):
        raise ValueError(f"{who}: the role may not read {found.schema}.{found.name}: grant it {engine.grant}")
    return found


def _endpoint(target: Target) -> str:
    """A target as a refused connect names it: with the endpoint it declares, never a secret."""
    db = target.database
    if db is None:
        return target.target_id
    return f"{target.target_id} ({db.engine} {db.host}:{db.port}/{db.name} as {db.user})"


def _why(exc: BaseException) -> str:
    """Why a connect was refused: an engine's code and what it means, the guard's address, or libpq's
    first line, which names the host and user, never the password."""
    if isinstance(exc, TableError):
        return _answered(exc)
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]


def _answered(exc: BaseException) -> str:
    """What a database answered with: its code, or its error's class, and what the code means where the
    engine says it; never the database's text."""
    gloss = getattr(exc, "gloss", None)
    return f"{code(exc)}: {gloss}" if gloss else code(exc)


def _classed(name: str, side: Plan, engine: str, relation: Relation, refused: list[str]) -> None:
    """Add what a pair with a MySQL or Oracle side stores of each table side: its engine, what its session
    said of the server, and the class and declared type of every column it reads. A type no plan reads
    and a key column without a key text every engine renders alike are refused (P7, P6)."""
    by = {column.name: column for column in relation.columns}
    filtered = sorted(where.columns(where.parse(side["where"]))) if side["where"] else []
    read = [*side["keys"], *side["columns"].values(), *filtered, *side.get("strata", {}).values()]
    for column in read:
        if by[column].cls is None:
            shown = f"column {column!r} ({by[column].declared})"
            _refuse(refused, f"side {name}: {shown} cannot be compared: {_CASTS}")
    for column in side["keys"]:
        if not by[column].keyable:
            shown = f"key column {column!r} ({by[column].declared})"
            why = _FILE_KEY if engine == "files" else f"cannot be a key: {_CASTS}"
            _refuse(refused, f"side {name}: {shown} {why}")
    facts = relation.facts
    side |= {
        "engine": engine,
        "server": facts["server"],
        "consistency": facts["consistency"],
        **({"tls": facts["tls"]} if "tls" in facts else {}),  # a file side has no session to encrypt
        "classes": {column: by[column].cls for column in read},
        "declared": {column: by[column].declared for column in read},
    } | facts.get("stored", {})  # a file side's: its file member and what its pins say (FILES.md §6)
    if "pinned" in facts:  # read by its probe, then sealed by its run
        side["pinned"] = facts["pinned"]


def _scale(cls: str, declared: str) -> int | None:
    """The scale a number key column declares: an integer's is 0, as is an unconstrained Oracle NUMBER's,
    whose key text is a whole number's (ENGINES.md §8); None when it declares none."""
    if cls == "integer" or declared == "NUMBER":
        return 0
    found = re.search(r"\(\s*[0-9*]+\s*(?:,\s*(-?[0-9]+)\s*)?\)", declared)
    return int(found.group(1) or 0) if found else None


def _paired(found: Mapping[str, Plan], engines: Mapping[str, str], refused: list[str]) -> None:
    """The cross-engine check of two table sides (ENGINES.md §7): a key column's two sides can render one
    text, a number key at one scale (P6), a column of class ``other`` stays in its engine (P8), and each
    compared column's two sides are of one family, after their typeMaps (P9)."""
    a, b = found["a"], found["b"]
    mapped = [any(maps) for maps in zip(key_maps(a), key_maps(b), strict=True)]  # its key text is the map's
    for key_a, key_b, bridged in zip(a["keys"], b["keys"], mapped, strict=True):
        typed = [(side["classes"][key], side["declared"][key]) for side, key in ((a, key_a), (b, key_b))]
        families = [render.family(cls) for cls, _ in typed]
        scales = {_scale(cls, declared) for cls, declared in typed}
        both = f"key column {key_a!r} is {typed[0][1]} on side a and {typed[1][1]} on side b"
        fielded = [s for s, (cls, declared) in zip("ab", typed, strict=True) if declared == duck.FIELD]
        other = "b" if fielded == ["a"] else "a"
        if not bridged and len(fielded) == 1 and families["ab".index(other)] in ("number", "date"):
            family, key = families["ab".index(other)], (key_a, key_b)["ab".index(fielded[0])]
            keys = f"csv.keys {{{key}: {{as: {'integer' if family == 'number' else 'date'}}}}}"
            text = (
                f"side {fielded[0]}: key column {key!r} is text (a CSV field) and side {other}'s {_a(family)}"
            )
            refused.append(f"{text}: declare {keys}")  # FILES.md P6: a CSV key is its field's text
        # 2024-01-01 is never 2024-01-01 00:00:00
        elif not bridged and "text" not in families and families[0] != families[1]:
            never = f"the key text of {_a(families[0])} is never that of {_a(families[1])}"
            refused.append(f"{both}: {never} ({_CASTS})")
        elif families == ["number", "number"] and (len(scales) > 1 or None in scales):  # nor 1 ever 1.00
            refused.append(f"{both}: a number key needs one scale on both sides ({_CASTS})")
    for canonical in a["columns"]:
        read = {}
        for name, side in found.items():
            column = side["columns"][canonical]
            cls, declared = side["classes"][column], side["declared"][column]
            if cls == "other" and engines["a"] != engines["b"]:
                shown = f"column {canonical!r} ({declared})"
                refused.append(f"side {name}: {shown} cannot be compared with another engine: {_CASTS}")
                break
            read[name] = (render.family(cls, side["types"].get(canonical)), declared, column)
        else:
            (family_a, declared_a, _), (family_b, _, _) = read["a"], read["b"]
            fielded = [s for s in "ab" if read[s][1] == duck.FIELD and read[s][0] == "text"]
            if family_a != family_b and fielded and (as_ := _AS.get(read["ba"[fielded[0] == "b"]][0])):
                own, other = fielded[0], "ba"[fielded[0] == "b"]
                theirs = f"{_a(read[other][0])} ({read[other][1]})"
                mapped = f"typeMap {{{read[own][2]}: {{as: {as_}}}}} on side {own}"
                text = f"side {own}: column {canonical!r} is text (a CSV field) and side {other}'s {theirs}"
                refused.append(f"{text}: declare {mapped}")  # FILES.md P9
            elif family_a != family_b:
                both = f"is {_a(family_a)} ({declared_a}) and side b's {_a(family_b)}"
                refused.append(f"side a: column {canonical!r} {both}: {_bridge(read)}")


def _worker_columns(
    found: Mapping[str, Plan], engines: Mapping[str, str], relations: Mapping[str, Relation]
) -> list[str]:
    """The compared columns the worker hashes (ENGINES.md §10): every one across engines, where no
    engine's function gives the same bytes. In one engine (two MySQL sides, two Oracle sides whose
    databases share both character sets, in which the server's hash reads a text, or two file sides, which
    DuckDB hashes: docs/mvp/FILES.md §9) the engine hashes a column that both sides declare alike, of one
    class (a ``padded`` CSV field against one that is not is two), that no typeMap names and that holds no
    large value."""
    a, b = found["a"], found["b"]
    sets = [(relations[name].facts["charset"], relations[name].facts.get("national")) for name in ("a", "b")]
    if engines["a"] != engines["b"] or (engines["a"] == "oracle" and sets[0] != sets[1]):
        return sorted(a["columns"])
    large = {column.name for column in relations["a"].columns if column.lob}
    worker = []
    for canonical, column in a["columns"].items():
        mapped, other = canonical in a["types"] or canonical in b["types"], b["columns"][canonical]
        # declared alike: so large on both, or none; and rendered by one expression
        alike = (a["declared"][column], a["classes"][column]) == (b["declared"][other], b["classes"][other])
        if mapped or not alike or column in large:
            worker.append(canonical)
    return sorted(worker)


def _a(family: str) -> str:
    return _A.get(family, f"a {family}")


def _bridge(described: Mapping[str, tuple[str, str, str]]) -> str:
    """What to declare for a compared column whose two sides are of two families."""
    for first, second in (("a", "b"), ("b", "a")):
        bridge = _BRIDGES.get((described[first][0], described[second][0]))
        if bridge:
            return "declare typeMap " + bridge.format(column=described[first][2], side=first)
    return f"declare a typeMap that gives both sides one type, or {_CASTS}"


def _like_columns(node: where.Node | None) -> list[str]:
    if node is None:
        return []
    own = [node.column] if node.op == "like" and node.column else []
    return own + [c for child in node.children for c in _like_columns(child)]


def _granted(side: str, relation: str, columns: list[str], readable: set[str], refused: list[str]) -> None:
    """Refuse a column the role may not read (a column-level grant)."""
    for denied in [c for c in columns if c not in readable]:
        _refuse(
            refused, f"side {side}: the role may not read column {denied!r} of {relation}: grant it SELECT"
        )


def _readable_names(side: str, names: list[str], refused: list[str]) -> None:
    """Refuse a name the statements would hold with a %, which the driver reads as a placeholder."""
    for percent in [n for n in names if "%" in n]:
        _refuse(refused, f"side {side}: the name {percent!r} holds a %, which cannot be read; use a view")


def _canonical_types(name: str, side: Plan, refused: list[str]) -> None:
    """The side's typeMap by canonical name; each mapped column must be compared."""
    canonical = {col: c for c, col in side["columns"].items()}
    for unknown in [col for col in side["types"] if col not in canonical]:
        refused.append(f"side {name}: typeMap column {unknown!r} is not compared")
    side["types"] = {canonical[col]: spec for col, spec in side["types"].items() if col in canonical}


async def _probe(target: Target, name: str, side: Plan) -> None:
    """``probed`` on a session of its own."""
    engine = of(target)
    session = await _connected(engine, target, f"side {name}")
    try:
        await probed(engine, session, name, side)
    finally:
        await session.close()


async def probed(engine: Engine, session: Session, name: str, side: Plan) -> None:
    """Plan and bind the side's key maps and filter, reading no row, so the database refuses a bad
    spec now."""
    try:
        await engine.probe(session, side)
    except Refusal as exc:  # a literal that is not of its column's class, a strata column no frame holds
        raise ValueError(f"side {name}: {exc}") from None
    except ERRORS as exc:  # a pattern or a literal of the spec: the message names no value
        why = (str(exc).splitlines() or [type(exc).__name__])[0][:300]
        raise ValueError(f"side {name}: the database refuses the filter or key map: {why}") from None
