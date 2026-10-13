"""Table pre-checks as people start, see and steer them: the stored
specs a pre-check pairs, or two schemas' tables paired by name, the list with its banner, one pre-check with
its totals and its pairs worst first, a pair's result and its draft, and a cancel. The API and the CLI call
this module; only the worker reads a table (``runner.precheck``), and two schemas are listed here, one
short session a side, as a run's creation plans.

A pre-check is its own object, never a run: creating one seats no licence pair, is refused by no licence
and needs no approval (what an unapproved version shows is the worker's to withhold). It is stored
with its pairs pending, audited ``precheck.create``; one made in an unlicensed evaluation is labelled so
wherever it is shown. A refusal carries its HTTP status and binding message (``Refused``); the CLI prints
the message. Every answer but a draft is headed by what a pre-check is not (``precheck.NOT``), and every
text of a pair is ``core.precheck``'s: nothing here words one. No audit detail holds a relation's or a
column's name.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import timedelta
from fnmatch import fnmatchcase
from typing import Any

from deltasleuth import licence
from deltasleuth.auth.principal import WORKER, Principal
from deltasleuth.connectors import tables
from deltasleuth.context import Context
from deltasleuth.core import precheck
from deltasleuth.profiler.tables import table_draft
from deltasleuth.store import repo

PAIRS = 500  # pairs a pre-check takes
LIMIT = 20  # unfinished pre-checks an installation holds
GLOBS, GLOB = 20, 128  # include or exclude patterns, and the characters of one
RELATIONS = 5000  # tables and views a side of two schemas paired by name holds
LIST_S = 30.0  # each statement that lists a schema, as a run's creation plans
STALLED = timedelta(minutes=10)  # unfinished, and no pair read or settled for this long: does a worker run?
HELD = 3  # lease periods: a running pair whose claim was not renewed for this long is read by no worker
AUDITED = 50  # spec ids a precheck.create row lists; of more, their count
_ITEM = (
    "precheck_id", "source", "level", "pairs", "skipped", "evaluation", "created_by", "created_by_name",
    "created_at", "cancelled_by", "cancelled_by_name", "cancelled_at", "finished_at", "progress", "verdicts",
)  # fmt: skip


class Refused(ValueError):
    """A pre-check request refused: its HTTP status and binding message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def file_name(row: Mapping[str, Any], name: str) -> str:
    """The name a pre-check's report ``name`` downloads and is written as: ``PC-…-<name>``, or
    ``PC-…-unlicensed-evaluation-<name>`` for one made in an unlicensed evaluation."""
    label = "unlicensed-evaluation-" if row["evaluation"] else ""
    return f"{row['precheck_id']}-{label}{name}"


# -- what every surface shows ----------------------------------------------------------------


def _stalled(row: Mapping[str, Any], lease_s: float) -> str | None:
    """The banner of an unfinished pre-check no worker reads: none of its pairs is being read (a running
    pair whose claim a worker renewed within ``HELD`` lease periods of ``lease_s`` seconds), and none was
    settled, for ``STALLED``. A worker that was killed renews no claim, so its pair is read by none."""
    idle = row["now"] - (row["settled_at"] or row["created_at"])
    claimed = row["claimed_at"]
    read = claimed is not None and row["now"] - claimed < timedelta(seconds=HELD * lease_s)
    if row["finished_at"] is not None or read or idle < STALLED:
        return None
    return precheck.stalled(int(idle.total_seconds()) // 60)


def item(row: Mapping[str, Any]) -> dict[str, Any]:
    """A pre-check as every surface shows it, from its ``repo.get_precheck`` row alone: its level and
    source, who made it and when, its pairs counted by status (``progress``) and by verdict
    (``verdicts``), those counts in words, worst first (``totals``), and whether it was made in an
    unlicensed evaluation."""
    progress = row["progress"]
    open_ = progress.get("pending", 0) + progress.get("running", 0)
    return {name: row[name] for name in _ITEM} | {
        "totals": precheck.totals(row["verdicts"], open_, progress.get("cancelled", 0))
    }


def _listed(pair: Mapping[str, Any]) -> dict[str, Any]:
    """A pair as a pre-check lists it, from the columns the store keeps beside its ``result``."""
    told = {name: pair[name] for name in ("n", "label", "spec_id", "status", "verdict", "differing")}
    return told | {"rows": [pair["rows_a"], pair["rows_b"]], "text": pair["line"]}


async def overview(ctx: Context, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    """``GET /v1/prechecks`` and ``ds precheck``: what a pre-check is not, a page of the pre-checks,
    newest first, and the banner of the one longest without a worker. No pair's ``result`` is read."""
    async with ctx.pool.connection() as conn:
        rows, following = await repo.list_prechecks(conn, limit=limit, cursor=cursor)
    idle = sorted(rows, key=lambda row: row["settled_at"] or row["created_at"])
    lease_s = ctx.settings.worker.lease_seconds
    banners = [line for row in idle if (line := _stalled(row, lease_s)) is not None][:1]
    return {
        "not": precheck.NOT, "banners": banners, "items": [item(row) for row in rows],
        "next_cursor": following,
    }  # fmt: skip


async def stored(ctx: Context, conn: repo.Conn, precheck_id: str) -> repo.Row:
    """The pre-check; refused when there is none: it was never made, or its retention deleted it."""
    row = await repo.get_precheck(conn, precheck_id)
    if row is None:
        days = ctx.settings.retention.standard_days
        raise Refused(
            404,
            f"table pre-check {precheck_id} not found (pre-checks are deleted {days} days after they are"
            " made)",
        )
    return row


async def described(
    ctx: Context, precheck_id: str, *, verdict: str | None = None, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    """``GET /v1/prechecks/{id}`` and ``ds precheck show``: the pre-check's item, its banner, each verdict
    as words (``worded``) and a page of its pairs worst first (those of ``verdict``, if given), each with
    its verdict's line. No pair's ``result`` is read."""
    async with ctx.pool.connection() as conn:
        row = await stored(ctx, conn, precheck_id)
        try:
            pairs, following = await repo.precheck_pairs(
                conn, precheck_id, verdict=verdict, limit=limit, cursor=cursor
            )
        except ValueError:
            raise Refused(400, "malformed cursor") from None
    banner = _stalled(row, ctx.settings.worker.lease_seconds)
    return {"not": precheck.NOT} | item(row) | {
        "banners": [] if banner is None else [banner],
        "worded": {code: precheck.worded(code) for code in precheck.VERDICTS},
        "items": [_listed(pair) for pair in pairs], "next_cursor": following,
    }  # fmt: skip


async def pair(ctx: Context, precheck_id: str, n: int) -> dict[str, Any]:
    """``GET /v1/prechecks/{id}/pairs/{n}``: the pair as its pre-check lists it, its two targets and
    relations, its ``result`` (format 1: its lines worst first, its refusals, its schema codes, its keys)
    and each compared column's figures as lines (``columns``, by name: the store keeps no order of them)."""
    async with ctx.pool.connection() as conn:
        row = await stored(ctx, conn, precheck_id)
        found = await repo.precheck_pair(conn, precheck_id, n)
    if found is None:
        raise Refused(404, f"pair {n} of table pre-check {precheck_id} not found")
    result = found["result"]
    columns = [
        {"column": column, "classes": read["classes"], "lines": precheck.column_lines(read)}
        for column, read in sorted((result or {}).get("columns", {}).items())
    ]
    about = {name: row[name] for name in ("precheck_id", "source", "level", "evaluation")}
    return {"not": precheck.NOT} | about | _listed(found) | {
        "said": found["verdict"] and precheck.worded(found["verdict"]),
        "targets": [found["target_a"], found["target_b"]],
        "relations": [found["relation_a"], found["relation_b"]],
        "finished_at": found["finished_at"], "result": result, "columns": columns,
    }  # fmt: skip


# -- the acts: each audited --------------------------------------------------------------------------


def _relation(table: Mapping[str, Any]) -> str:
    """A table side's relation as its spec spells it."""
    return f"{table['schema']}.{table['name']}" if table.get("schema") else table["name"]


def refuse(body: Mapping[str, Any]) -> None:
    """Refuse what no store is needed to refuse: none or several of the three forms, a level
    that is none, and more ids or names than a pre-check takes pairs."""
    ids, names, every = body.get("spec_ids"), body.get("spec_names"), bool(body.get("all_specs"))
    if (ids is not None) + (names is not None) + every != 1:
        raise Refused(422, "give spec_ids, spec_names or all_specs, and only one of them")
    if body.get("level", "columns") not in precheck.LEVELS:
        raise Refused(422, "level must be schema, counts, columns or distinct")
    for member, given, what in (("spec_ids", ids, "ids"), ("spec_names", names, "names")):
        if given is not None and not 1 <= len(given) <= PAIRS:
            raise Refused(422, f"{member} takes 1 to {PAIRS} {what} and {len(given):,} were given")


async def _specs(conn: repo.Conn, body: Mapping[str, Any]) -> tuple[list[repo.Row], int, dict[str, Any]]:
    """The stored versions ``body`` names, each a pair of tables, in the order given, how
    many names it skipped, and the request as the pre-check stores it."""
    ids, names = body.get("spec_ids"), body.get("spec_names")
    if ids is None and names is None:
        stored_ = await repo.precheck_specs(conn)
        rows = [row for row in stored_ if _tables(row)]
        if not rows:
            raise Refused(422, "no stored spec is a pair of tables")
        if len(rows) > PAIRS:
            raise Refused(
                422,
                f"{len(rows):,} stored specs are pairs of tables and a table pre-check takes up to {PAIRS}:"
                " name them with spec_names or spec_ids",
            )
        return rows, len(stored_) - len(rows), {"all_specs": True}
    given: Sequence[Any] = names if ids is None else ids
    member, by = ("spec_names", "name") if ids is None else ("spec_ids", "spec_id")
    found = {row[by]: row for row in await repo.precheck_specs(conn, spec_ids=ids, names=names)}
    for asked in given:
        if asked not in found:
            raise Refused(404, f"no spec is named {asked}" if ids is None else f"spec {asked} not found")
        if given.count(asked) > 1:
            raise Refused(422, f"spec {found[asked]['spec_id']} is listed twice")
    rows = [found[asked] for asked in given]
    for row in rows:
        if not _tables(row):
            named = f"spec {row['spec_id']} ({row['name']} {row['version']})"
            raise Refused(422, f"{named} is not a pair of tables: a table pre-check reads two table sides")
    return rows, 0, {member: list(given)}


def _tables(row: Mapping[str, Any]) -> bool:
    return row["table_a"] is not None and row["table_b"] is not None


async def create(ctx: Context, body: Mapping[str, Any], who: Principal) -> dict[str, Any]:
    """``POST /v1/prechecks`` and ``ds precheck run``: store a pre-check of the stored specs ``body`` names
    (``spec_ids``, ``spec_names``: each name's newest version, or ``all_specs``: the newest version of
    every name whose sides are both tables) at its ``level``, its pairs pending for the worker, audited
    ``precheck.create``. The licence is read only to label an unlicensed evaluation: nothing is refused
    for it, and no pair is counted. A refusal is ``Refused``."""
    refuse(body)
    level = body.get("level", "columns")
    evaluation = (await licence.status(ctx))["state"] == "evaluation"  # never raises
    async with repo.audited(ctx.pool) as (conn, audit):
        rows, skipped, request = await _specs(conn, body)
        if await repo.count_open_prechecks(conn, lock=True) >= LIMIT:  # creates count one at a time
            raise Refused(
                409,
                f"this installation holds {LIMIT} unfinished table pre-checks, the most it takes: cancel one"
                " or wait",
            )
        precheck_id, spec_ids = repo.new_precheck_id(), [row["spec_id"] for row in rows]
        pairs = [
            {"n": n, "spec_id": row["spec_id"], "label": row["name"]}
            | {f"target_{side}": row[f"target_{side}"] for side in "ab"}
            | {f"relation_{side}": _relation(row[f"table_{side}"]) for side in "ab"}
            for n, row in enumerate(rows, 1)
        ]
        made = {
            "precheck_id": precheck_id, "source": "specs", "level": level, "skipped": skipped,
            "request": request | {"spec_ids": spec_ids}, "evaluation": evaluation, "created_by": who.actor,
        }  # fmt: skip
        await repo.insert_precheck(conn, made, pairs)
        listed = spec_ids if len(spec_ids) <= AUDITED else len(spec_ids)
        detail = {"source": "specs", "level": level, "pairs": len(pairs), "skipped": skipped}
        await audit(who, "precheck.create", precheck_id, detail | {"spec_ids": listed})
    return {
        "not": precheck.NOT, "precheck_id": precheck_id, "pairs": len(pairs), "skipped": skipped,
        "evaluation": evaluation,
    }  # fmt: skip


def refuse_catalogue(body: Mapping[str, Any]) -> None:
    """Refuse what no target is needed to refuse of two schemas paired by name."""
    level = body.get("level", "counts")
    if level not in precheck.LEVELS:
        raise Refused(422, "level must be schema, counts, columns or distinct")
    if level == precheck.LEVELS[-1]:
        need = "distinct counts need a stored spec"
        raise Refused(422, f"two schemas paired by name take level schema, counts or columns: {need}")
    globs = [body.get("include") or [], body.get("exclude") or []]
    if any(len(found) > GLOBS or any(len(glob) > GLOB for glob in found) for found in globs):
        raise Refused(422, f"include and exclude take up to {GLOBS} patterns of at most {GLOB} characters")


def _declared(ctx: Context, target_id: str) -> None:
    """Refuse a target that is not declared, or that is no database, or a files target."""
    if target_id not in ctx.targets:
        raise Refused(404, f"target {target_id} is not declared")
    if ctx.targets.get(target_id).engine is None:
        raise Refused(422, f"target {target_id} is a service: a table pre-check reads database targets")
    if ctx.targets.get(target_id).engine == "files":
        raise Refused(422, precheck.FILE_SIDE)


def _schema(asked: Mapping[str, Any]) -> str:
    """A side's schema as a refusal names it."""
    schema = f"schema {asked['schema']}" if asked["schema"] else "the default schema"
    return f"{schema} of target {asked['target']}"


async def _relations(ctx: Context, side: str, asked: Mapping[str, Any]) -> list[str]:
    """The names of the tables and views of a side's schema the account may read, on a short session of
    its own: a failure says its code alone."""
    target, schema, of = ctx.targets.get(asked["target"]), asked["schema"], _schema(asked)
    engine = tables.of(target)
    try:
        session = await engine.connect(target, timeout_s=LIST_S)
        try:
            found = await engine.relations(session, schema)
        finally:
            await session.close()
    except Exception as exc:  # a connect or a statement refused: the code, never a driver's text
        raise Refused(422, f"side {side}: cannot list {of} ({tables.code(exc)})") from None
    if not found:
        raise Refused(422, f"side {side}: {of} holds no table or view the account can read")
    return [name for name, _ in found]


def _globbed(names: Sequence[str], body: Mapping[str, Any]) -> list[str]:
    """The names that an ``include`` glob matches (all without one) and no ``exclude`` glob does, each
    matched as its lower-cased name."""
    include, exclude = body.get("include") or ["*"], body.get("exclude") or []
    matched = lambda name, globs: any(fnmatchcase(name.lower(), glob) for glob in globs)  # noqa: E731
    return [name for name in names if matched(name, include) and not matched(name, exclude)]


def _paired(body: Mapping[str, Any], level: str, listed: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
    """The pairs of two schemas' names, by lower-cased name: a name one side alone holds, or that two of a
    side's hold, is a pair settled ``unpaired`` as it is made, its relation on that side None."""
    held: dict[str, dict[str, list[str]]] = {side: defaultdict(list) for side in "ab"}
    for side, names in listed.items():
        for name in names:
            held[side][name.lower()].append(name)

    def shown(side: str, name: str) -> str:
        return f"{body[side]['schema']}.{name}" if body[side]["schema"] else name

    pairs = []
    for n, label in enumerate(sorted(held["a"].keys() | held["b"].keys(), key=str.encode), 1):
        found = {side: held[side].get(label, []) for side in "ab"}
        pair = {"n": n, "spec_id": None, "label": label}
        for side in "ab":
            one = found[side][0] if len(found[side]) == 1 else None
            pair |= {f"target_{side}": body[side]["target"], f"relation_{side}": one}
        alike = [side for side in "ab" if len(found[side]) > 1]
        sided = [side for side in "ab" if found[side]] if not alike else []
        if alike or len(sided) == 1:
            side = (alike or sided)[0]
            names = found[side] if alike else [shown(side, found[side][0])]  # several: each by its name
            line = precheck.unpaired(side, names, label)
            judged = precheck.alone(level, line)
            pair |= {"verdict": judged.verdict, "rank": judged.rank, "differing": 0, "line": line}
            pair |= {"result": judged.result}
        pairs.append(pair)
    return pairs


async def catalogue(ctx: Context, body: Mapping[str, Any], who: Principal) -> dict[str, Any]:
    """``POST /v1/prechecks/catalogue`` and ``ds precheck run --a --b``: store a pre-check of two declared
    database targets' schemas (``a`` and ``b``, each a ``target`` and a ``schema``, the target's default
    when left out), their tables and views paired by lower-cased name, those ``include`` and ``exclude``
    choose, at ``level`` (counts by default), audited ``precheck.create``. The schemas are listed now, one
    short session a side (in turn on one target), each statement bounded as a run's creation plans; the
    pairs are read by the worker. A refusal is ``Refused``."""
    refuse_catalogue(body)
    level = body.get("level", "counts")
    sides = {s: {"target": body[s]["target"], "schema": body[s].get("schema") or None} for s in "ab"}
    body = dict(body) | sides
    for side in "ab":
        _declared(ctx, body[side]["target"])
    if body["a"]["target"] == body["b"]["target"]:
        found = [await _relations(ctx, side, body[side]) for side in "ab"]
    else:
        found = await asyncio.gather(*(_relations(ctx, side, body[side]) for side in "ab"))
    listed = {side: _globbed(names, body) for side, names in zip("ab", found, strict=True)}
    for side in "ab":
        if len(listed[side]) > RELATIONS:
            over = f"holds over {RELATIONS:,} relations: name fewer with include"
            raise Refused(422, f"side {side}: {_schema(body[side])} {over}")
    if not any(listed.values()):
        raise Refused(422, "include and exclude leave no table or view of either schema")
    pairs = _paired(body, level, listed)
    if len(pairs) > PAIRS:
        more = f"a table pre-check takes up to {PAIRS}: narrow them with include or exclude"
        raise Refused(422, f"the two schemas give {len(pairs):,} pairs and {more}")
    evaluation = (await licence.status(ctx))["state"] == "evaluation"  # never raises
    request = sides | {"include": body.get("include") or [], "exclude": body.get("exclude") or []}
    async with repo.audited(ctx.pool) as (conn, audit):
        if await repo.count_open_prechecks(conn, lock=True) >= LIMIT:
            raise Refused(
                409,
                f"this installation holds {LIMIT} unfinished table pre-checks, the most it takes: cancel one"
                " or wait",
            )
        precheck_id = repo.new_precheck_id()
        made = {
            "precheck_id": precheck_id, "source": "catalogue", "level": level, "skipped": 0,
            "request": request, "evaluation": evaluation, "created_by": who.actor,
        }  # fmt: skip
        await repo.insert_precheck(conn, made, pairs)
        detail = {"source": "catalogue", "level": level, "pairs": len(pairs), "skipped": 0}
        await audit(who, "precheck.create", precheck_id, detail | sides)
        if (done := await repo.finish_precheck(conn, precheck_id)) is not None:  # every name unpaired
            await audit(WORKER, "precheck.finished", precheck_id, done)
    return {
        "not": precheck.NOT, "precheck_id": precheck_id, "pairs": len(pairs), "skipped": 0,
        "evaluation": evaluation,
    }  # fmt: skip


async def draft(ctx: Context, precheck_id: str, n: int) -> tuple[str, list[Any]]:
    """``GET /v1/prechecks/{id}/pairs/{n}/draft.yaml`` and ``ds precheck draft``: a starter spec of a pair
    of two relations paired by name (``profiler.tables.table_draft``) and its ``validate_spec`` issues,
    which head it as comments. Never stored; uploading it is ``POST /v1/specs``. A refusal is ``Refused``."""
    async with ctx.pool.connection() as conn:
        await stored(ctx, conn, precheck_id)
        found = await repo.precheck_pair(conn, precheck_id, n)
        if found is None:
            raise Refused(404, f"pair {n} of table pre-check {precheck_id} not found")
        spec = found["spec_id"] is not None and await repo.get_spec(conn, found["spec_id"])
    if spec:
        raise Refused(409, f"pair {n} is a stored spec ({spec['name']} {spec['version']}): it needs no draft")
    for side in "ab":
        if found[f"relation_{side}"] is None:
            raise Refused(409, f"pair {n} has no table on side {side}: there is nothing to draft")
    if "names" not in (found["result"] or {}):
        raise Refused(409, f"pair {n} was not read: there is nothing to draft yet")
    drafted = table_draft(found, targets=ctx.targets)
    return drafted.yaml_text, drafted.issues


async def cancel(ctx: Context, precheck_id: str, who: Principal) -> dict[str, Any]:
    """Cancel the unfinished pre-check: its pending and running pairs are cancelled, what was read stays,
    and the worker ends the statements of those it was reading; audited ``precheck.cancel``."""
    async with repo.audited(ctx.pool) as (conn, audit):
        await stored(ctx, conn, precheck_id)
        done = await repo.cancel_precheck(conn, precheck_id, who.actor, precheck.CANCELLED)
        if done is None:
            raise Refused(409, f"table pre-check {precheck_id} has ended")
        detail = {name: done[name] for name in ("pending", "running")}
        await audit(who, "precheck.cancel", precheck_id, detail)
    return {"not": precheck.NOT, "precheck_id": precheck_id, "cancelled_at": done["cancelled_at"]}
