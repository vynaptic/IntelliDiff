"""Cut-over readiness as every surface reads it: the evaluation of
the installation's pairs from stored evidence, which the API, the CLI, the reports and the worker's watch
share; a pair's detail, trend and brief; the recorded transitions; the settings as judged; and the check of
the stored measures against their audit rows. It reads only: the worker alone writes measures and readiness
audit rows (``runner.readiness``).

An evaluation is a handful of statements whatever the history: the pairs, each one's series, the cycles
still to measure, the governance facts from where its streak began, its newest transition and the licence
standing; ``core.readiness`` then judges each pair on its own (one whose measures cannot be read is told
so, and the others are judged), and a sampled candidate whose parity gates are met has its draw discipline
read. A refusal carries its HTTP status and binding message (``Refused``); the CLI prints the message.
Counts, ids, spec names, versions and slugs, and the truth source an accuracy gate's labelling names: no
key, no value and no person.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from deltasleuth import approvals, licence
from deltasleuth.config import ReadinessSettings
from deltasleuth.context import Context
from deltasleuth.core import readiness as gates
from deltasleuth.core.readiness import GATES, STATES, Policy
from deltasleuth.security import rawvalues
from deltasleuth.store import repo

PAGE = 500  # points a page of a pair's trend holds
BRIEF_CAUSES = 500  # root-cause groups a pair's brief lists
PRECHECK = "computed now; the sign-off decides"
BUCKETS = ("cycle", "day", "week")
CLOSED = ("open", "closed", "all")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NONE: dict[str, Any] = {
    "signoffs": [],
    "cycles": [],
    "soaks": [],
}  # the governance of a pair without evidence
# A pair as GET /v1/readiness lists it, from its evaluation.
_ITEM = (
    "pair", "programme", "entity", "version", "spec_id", "state", "regressed", "score", "gates", "blocking",
    "evidence", "trend", "since",
)  # fmt: skip
# A stratum of the evidence cycle's stored judgement as a pair's detail shows it: counts, never a rate.
_STRATUM = (
    "label", "tier", "basis", "frame_keys", "drawn", "n", "x", "unresolved", "absent_both",
    "api_uncomparable", "verdict", "why",
)  # fmt: skip
log = logging.getLogger(__name__)


class Refused(ValueError):
    """A readiness read refused: its HTTP status and binding message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Judged:
    """An evaluation of pairs as of the database's ``now``: each pair's ``core.readiness.evaluate`` item
    (with ``since``, when it entered its state as the worker recorded it), the policy, series and
    governance it was judged from, its newest ``readiness.changed`` row (``last``), the pairs with cycles
    never measured as they were judged with them (``waiting``, ``repo.unmeasured_pairs``' records: the
    worker writes no transition of one), and how many ended cycles are still to measure, each counted
    up to ``repo.OWED``: never measured (``unmeasured``), and ended before 014 (``backfill``: while any is
    left, no transition is written)."""

    now: datetime
    items: dict[str, dict[str, Any]]
    policies: dict[str, Policy]
    series: dict[str, dict[str, Any]]
    governance: dict[str, dict[str, Any]]
    last: dict[str, repo.Row]
    waiting: dict[str, repo.Row]
    unmeasured: int
    backfill: int


def named(settings: ReadinessSettings | None) -> list[str]:
    """The pairs the settings list."""
    return [pair.spec for programme in (settings.programmes if settings else ()) for pair in programme.pairs]


async def known(conn: repo.Conn, settings: ReadinessSettings | None, name: str) -> None:
    """Refused (404) unless a stored spec or the settings name the pair."""
    if name not in {*(await repo.readiness_pairs(conn, [name]))["pairs"], *named(settings)}:
        raise Refused(404, f"pair {name} not found: no spec or readiness setting names it")


async def _allows(ctx: Context, conn: repo.Conn) -> dict[str, bool] | None:
    """What the licence allows now; None when its standing cannot be read (logged): sign-off and pack then
    keep what the licence last decided of them, and are never met without their stored fact."""
    try:
        async with conn.transaction():
            return (await licence.standing(ctx, conn)).allows()
    except Exception as exc:
        if repo.lost(conn, exc):
            raise  # the store is unreachable: nothing is judged
        log.warning("readiness: the licence standing could not be read", exc_info=True)
        return None


async def judged(
    ctx: Context, conn: repo.Conn, *, only: str | None = None, at: datetime | None = None
) -> Judged:
    """Evaluate every pair (the spec names with a batch or soak run, and those the settings list), or
    ``only`` that one; Refused (404) when no spec or setting names it. ``at``: as of then (a run report's
    cycle), on the cycles ended by then and without a live soak's schedule, which is today's."""
    from deltasleuth.reports import accuracy  # the reports and the runner's modules import this one
    from deltasleuth.runner import sample
    from deltasleuth.runner.stop import StopConditions

    settings = ctx.settings.readiness
    listed = named(settings)
    found = await repo.readiness_pairs(conn, [*listed, *([only] if only else [])])
    now, stored = at or found["now"], set(found["pairs"])
    if only is not None and only not in stored and only not in listed:
        raise Refused(404, f"pair {only} not found: no spec or readiness setting names it")
    names = sorted(stored | set(listed)) if only is None else [only]
    policies = {name: gates.policy(settings, name) for name in names}
    until = {name: end if (end := gates.as_of(p, now)) < now or at else None for name, p in policies.items()}
    series = await repo.readiness_series(
        conn,
        [
            {"pair": name, "tier": p.gates.tier, "needed": p.gates.cycles, "window": gates.window(p)}
            | {"until": until[name]}
            for name, p in policies.items()
        ],
        whole=only is not None,  # a pair's detail lists its streak's points
    )
    owed, waiting = await repo.unmeasured_pairs(conn, names)
    unmeasured = {
        name: left
        for name in names
        if (left := waiting.get(name)) is not None
        and not (until[name] and left["oldest"]["ended_at"] > until[name])
    }
    asked, unread = [], {}
    for name in names:
        try:
            held = gates.streak(series[name], policies[name], unmeasured.get(name))
            if (cycle := held.evidence) is not None:
                asked.append(
                    {"pair": name, "spec_id": cycle["readiness"]["spec_id"], "after": held.after}
                    | {"until": until[name], "run_id": cycle["run_id"], "cycle": cycle["cycle"]}
                )
        except Exception as exc:  # measures this build cannot read: the pair says so below
            unread[name] = exc
    governance = dict.fromkeys(names, _NONE) | await repo.readiness_governance(conn, asked)
    if at is not None:
        governance = {name: held | {"soaks": None} for name, held in governance.items()}
    last = await repo.last_readiness(conn, names)
    seen = gates.Installation(
        ctx.settings.auth_mode, ctx.settings.packs is not None, await _allows(ctx, conn), accuracy.gate,
        StopConditions.from_spec,
    )  # fmt: skip
    items = {}
    for name in names:
        row = last.get(name)
        judge = partial(
            gates.evaluate, name, policies[name], series[name], governance[name], seen, now=now,
            unmeasured=unmeasured.get(name), stored=name in stored, last=row and row["detail"],
        )  # fmt: skip
        try:
            if name in unread:
                raise unread[name]
            item = judge()
            by = {gate["gate"]: gate for gate in item["gates"]}
            parity = [g["status"] for g in item["gates"] if g["group"] == "parity" and g["required"]]
            if by["sample_ledger"]["status"] == "unknown" and set(parity) <= {"met", "not_applicable"}:
                picked = item["candidate"]  # sampled, every parity gate met: its draw discipline is read
                async with conn.transaction():  # a savepoint: a failed read leaves the other pairs theirs
                    run = await repo.get_run(conn, picked["run_id"])
                    assert run is not None
                    spec = await ctx.spec(conn, run["spec_id"])
                    ledger = await sample.problems(
                        conn, run, picked["cycle"], spec, later=True, selection=False
                    )
                item = judge(ledger=ledger)
        except Exception as exc:  # one pair's records never stop the others' evaluation
            if repo.lost(conn, exc):
                raise  # the store is unreachable: nothing is judged
            log.warning("readiness: pair %s could not be evaluated: %s", name, type(exc).__name__)
            item = gates.unevaluated(name, policies[name], type(exc).__name__, now)
        recorded = row is not None and row["detail"]["to"] == item["state"]
        items[name] = item | {"since": row["since"] if recorded else None}  # type: ignore[index]
    counts = owed["cycles"], owed["backfill"]
    return Judged(now, items, policies, series, governance, last, unmeasured, *counts)


# -- what is asked ----------------------------------------------------------------------------


def _limit(value: Any, default: int = 100) -> int:
    try:
        limit = default if value is None else int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= 500:
        raise Refused(422, "limit must be 1 to 500")
    return limit


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        at = None
    if at is None or at.tzinfo is None:
        raise Refused(422, "as_of must be an ISO-8601 time with a UTC offset")
    return at


def filters(
    settings: ReadinessSettings | None,
    *,
    programme: str | None = None,
    state: str | None = None,
    gate: str | None = None,
    closed: str | None = None,
    bucket: str | None = None,
) -> None:
    """Refuse a filter that names nothing, before anything is read."""
    if programme is not None and programme not in [p.name for p in (settings.programmes if settings else ())]:
        raise Refused(404, f"programme {programme} is not configured")
    if state is not None and state not in STATES:
        raise Refused(422, f"state must be one of {', '.join(STATES)}")
    if gate is not None and gate not in GATES:
        raise Refused(422, f"gate must be one of {', '.join(GATES)}")
    if closed is not None and closed not in CLOSED:
        raise Refused(422, "closed must be open, closed or all")
    if bucket is not None and bucket not in BUCKETS:
        raise Refused(422, "bucket must be cycle, day or week")


def chosen(
    items: Sequence[Mapping[str, Any]],
    *,
    programme: str | None = None,
    entity: str | None = None,
    closed: str = "open",
    q: str | None = None,
) -> list[Mapping[str, Any]]:
    """The pairs a selection takes, worst first: of a programme, of an entity type, open or closed (or
    ``all``), whose name holds ``q``."""
    taken = [
        item
        for item in items
        if (programme is None or item["programme"] == programme)
        and (entity is None or item["entity"] == entity)
        and (closed == "all" or item["open"] == (closed == "open"))
        and (q is None or q.lower() in item["pair"])
    ]
    return sorted(taken, key=lambda item: (STATES.index(item["state"]), item["pair"]))


def _blocked(item: Mapping[str, Any], gate: str) -> bool:
    """Whether the required ``gate`` is not met: in an evaluated item, or in a recorded one's statuses."""
    if "blocking" in item:
        return gate in item["blocking"]
    return item["gates"].get(gate) in ("not_met", "unknown")


def listed(item: Mapping[str, Any]) -> dict[str, Any]:
    """An evaluated pair as GET /v1/readiness lists it: ``signoff`` and ``pack`` are the approved
    sign-off's and the issued pack's ids."""
    return {name: item[name] for name in _ITEM} | {
        "signoff": item["signoff_id"], "pack": item["pack_id"], "closed": item["closed"],
        "deviations": item["deviations"],
    }  # fmt: skip


def _recorded(pair: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """A pair as its ``readiness.changed`` row recorded it (``as_of``)."""
    detail = row["detail"]
    names = ("programme", "entity", "regressed", "score", "gates", "evidence", "candidate", "signoff_id",
             "pack_id", "config_sha256")  # fmt: skip
    return (
        {"pair": pair, "state": detail["to"]}
        | {name: detail.get(name) for name in names}
        | {"since": row["since"], "seq": row["seq"]}
    )


def entities(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each entity type of the open pairs (None last) with how many are in each state."""
    found: dict[str | None, Counter[str]] = {}
    for item in items:
        if item.get("open", True):
            found.setdefault(item["entity"], Counter())[item["state"]] += 1
    return [
        {"entity": entity, "states": {state: n for state in STATES if (n := found[entity][state])}}
        for entity in sorted(found, key=lambda e: (e is None, e))
    ]


def notes(found: Judged | None, config: str, worker: str | None) -> list[str]:
    """What heads every surface besides the definition: that cycles are not measured yet, and that the
    worker judges with other settings than this process."""
    said = []
    if found is not None and (found.unmeasured or found.backfill):
        said.append(f"{gates.counted(found.unmeasured, repo.OWED)} ended cycle(s) are not measured yet")
        if found.backfill:
            said[0] += (
                f"; {gates.counted(found.backfill, repo.OWED)} ended before 014 are being measured, and no"
                " transition is written until they are"
            )
    if worker is not None and worker != config:
        said.append(
            f"this server judges with readiness settings {config[:12]}, the worker with {worker[:12]}:"
            " restart both after changing them"
        )
    return said


async def worker_config(conn: repo.Conn) -> str | None:
    """The settings hash the worker last recorded (``readiness.config``); None until it recorded one."""
    row = await repo.last_audit(conn, "readiness.config")
    return row and row["detail"]["sha256"]


def _position(cursor: str | None) -> tuple[int, str]:
    if cursor is None:
        return -1, ""
    rank, _, pair = cursor.partition(".")
    try:
        return int(rank), pair
    except ValueError:
        raise Refused(400, "malformed cursor") from None


async def overview(
    ctx: Context,
    *,
    programme: str | None = None,
    entity: str | None = None,
    state: str | None = None,
    gate: str | None = None,
    closed: str | None = None,
    q: str | None = None,
    as_of: str | None = None,
    limit: Any = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """``GET /v1/readiness`` and ``ds readiness``: the pairs a selection takes, worst first, a page of
    them, with the definition, the settings hashes, the ``notes``, each programme's roll-up and each entity
    type's states (over the selection before ``state`` and ``gate`` narrow it), and how many
    ``dashboard.pdf`` volumes the pairs of the programme and entity type make, closed ones too. With
    ``as_of``, what the worker had recorded by then: each pair's newest ``readiness.changed`` row, whatever
    ``closed`` says."""
    from deltasleuth.reports import readiness as reports  # the reports import this module

    settings = ctx.settings.readiness
    filters(settings, programme=programme, state=state, gate=gate, closed=closed)
    at, size, start = _time(as_of), _limit(limit), _position(cursor)
    async with ctx.pool.connection() as conn:
        found = None
        if at is None:
            found = await judged(ctx, conn)
            now, names, items = found.now, list(found.items), list(found.items.values())
        else:
            pairs = await repo.readiness_pairs(conn, named(settings))
            now, names = pairs["now"], sorted({*pairs["pairs"], *named(settings)})
            rows = await repo.last_readiness(conn, names, at=at)
            items, closed = [_recorded(name, row) for name, row in rows.items()], "all"
        worker = await worker_config(conn)
    policies = [gates.policy(settings, name) for name in names]
    reported = [p for p in policies if programme in (None, p.programme) and entity in (None, p.entity)]
    picked = chosen(items, programme=programme, entity=entity, closed=closed or "open", q=q)
    shown = [
        item
        for item in picked
        if (state is None or item["state"] == state) and (gate is None or _blocked(item, gate))
    ]
    page = [item for item in shown if (STATES.index(item["state"]), item["pair"]) > start][: size + 1]
    more = len(page) > size
    config = gates.config_sha256(settings)
    return {
        "definition": gates.DEFINITION, "now": now, "as_of": at, "config_sha256": config,
        "worker_config_sha256": worker, "notes": notes(found, config, worker),
        "programmes": gates.programmes([{"open": True} | item for item in picked]),
        "entities": entities(picked),
        "items": [listed(item) if at is None else item for item in page[:size]],
        "next_cursor": f"{STATES.index(page[size - 1]['state'])}.{page[size - 1]['pair']}" if more else None,
        "volumes": reports.volumes(len(reported)),
    }  # fmt: skip


# -- a pair ---------------------------------------------------------------------------------------


async def _precheck(ctx: Context, conn: repo.Conn, item: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a sign-off of the pair's unsigned candidate would be refused for, computed now (the sign-off
    decides), and, where packs are configured, the line that refuses every pack of a spec whose key
    pattern cannot tell a key, which no sign-off is refused for; ``purged`` once
    its cycle's purge has started; None outside SSO, without a candidate or for one signed off."""
    from deltasleuth.runner.purge import CyclePurged

    picked, signed = item["candidate"], item["signoff_id"] is not None
    if ctx.settings.auth_mode != "oidc" or picked is None or signed:
        return None
    run = await repo.get_run(conn, picked["run_id"])
    assert run is not None
    answer = {"label": PRECHECK} | picked | {"purged": False, "problems": []}
    try:
        spec = await ctx.spec(conn, run["spec_id"])
        answer["problems"] = await approvals.signoff_problems(conn, run, picked["cycle"], spec)
        if ctx.settings.packs is not None and (blind := rawvalues.no_pack(run["spec_id"], spec)):
            answer["problems"].append(blind)  # the sign-off passes, and its pack is then refused
    except CyclePurged:  # readiness never answers 410
        answer["purged"] = True
    return answer


async def pair(ctx: Context, name: str, *, precheck: bool = False) -> dict[str, Any]:
    """``GET /v1/readiness/pairs/{name}``: the pair as listed, with its candidate, convergence and
    notes, the streak's points (the window at most), each version's newest evidence cycle, the live soaks
    of the current one, the evidence cycle's strata (counts, never a pooled rate), the candidate's labelling
    and, on demand only, the sign-off precheck."""
    async with ctx.pool.connection() as conn:
        found = await judged(ctx, conn, only=name)
        item, series, governance = found.items[name], found.series[name], found.governance[name]
        worker = await worker_config(conn)
        checked = {"precheck": await _precheck(ctx, conn, item)} if precheck else {}
    tier, picked = found.policies[name].gates.tier, item["candidate"]
    judgement = series["versions"][0].get("sample") if series["versions"] else None
    held = next(
        (c for c in governance["cycles"] if picked == {"run_id": c["run_id"], "cycle": c["cycle"]}), None
    )
    labelling = held and held["labelling"]
    labelled = ("labelling_id", "run_id", "cycle", "labelled_at", "attested")
    return (
        listed(item)
        | {"definition": gates.DEFINITION, "now": found.now, "config_sha256": item["config_sha256"]}
        | {"worker_config_sha256": worker}
        | {"candidate": picked, "convergence": item["convergence"]}
        | {"notes": [*item["notes"], *notes(found, item["config_sha256"], worker)]}
        | {"points": gates.trend(series["points"][: item["convergence"]["streak"]], tier)}
        | {
            "versions": [
                {"spec_id": row["readiness"]["spec_id"], "version": row["readiness"]["version"]}
                | {key: row[key] for key in ("run_id", "cycle", "ended_at")}
                for row in series["versions"]
            ]
        }
        | {
            "runs": [
                {key: soak[key] for key in ("run_id", "cycle", "status", "stop_reason", "started_at")}
                for soak in governance["soaks"]
            ]
        }
        | {"strata": judgement and [{key: s[key] for key in _STRATUM} for s in judgement["strata"]]}
        | {"labelling": labelling and {key: labelling[key] for key in labelled}}
        | checked
    )


async def brief(ctx: Context, name: str, *, whole: bool = False, most: int | None = None) -> dict[str, Any]:
    """``GET /v1/readiness/pairs/{name}/brief``: the pair as listed, under the definition and what a
    brief is, with the detail's notes, the legend of its marks, its newest ``BRIEF_CYCLES``
    measured points oldest first (a closed pair's up to its day), each marked against the one before, its
    reported ``cycle`` (the newest complete one, whatever its basis; None without one, as a note then
    says) and its root-cause groups over the window's complete cycles that keep theirs (``causes``;
    None without a reported cycle, and for a purged one, as a note says): ``BRIEF_CAUSES`` of them
    listed, or (``whole``, as ``causes.csv`` holds them) every one, ``most`` at most. The evaluation
    and both reads share one snapshot, so a purge that commits between them changes nothing; nothing is
    written."""
    from deltasleuth.runner import sample  # the runner's modules import this one

    async with ctx.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")  # one snapshot
        found = await judged(ctx, conn, only=name)
        item, policy = found.items[name], found.policies[name]
        worker = await worker_config(conn)
        end = gates.as_of(policy, found.now)
        rows = await repo.brief_points(
            conn, name, end if end < found.now else None, gates.BRIEF_CYCLES + 1
        )  # one more: what the window's oldest point is marked against
        points = gates.marked(rows, policy.gates.tier)[-gates.BRIEF_CYCLES :]
        picked = next((point for point in reversed(points) if point["complete"]), None)
        cycle = causes = None
        if picked is None:
            said = gates.unreported(name, rows[: gates.BRIEF_CYCLES])
        else:
            at = picked["run_id"], picked["cycle"]
            row = next(row for row in rows if (row["run_id"], row["cycle"]) == at)
            strata = sample.lines(row["sample"]) if row["sample"] else None
            cycle = gates.reported(picked, row, item["evidence"], strata)
            said = gates.purged(row) if row["purging_at"] is not None else None
            if said is None:
                held = [point for point in points if point["complete"] and point["root_causes"] is not None]
                groups = await repo.brief_causes(conn, held, most if whole else BRIEF_CAUSES)
                causes = gates.burn_down(points, held, groups)
    return (
        listed(item)
        | {"definition": gates.DEFINITION, "brief": gates.BRIEF, "now": found.now}
        | {"config_sha256": item["config_sha256"]}
        | {"notes": [*item["notes"], *notes(found, item["config_sha256"], worker), *([said] if said else [])]}
        | {"legend": gates.LEGEND, "points": points, "cycle": cycle, "causes": causes}
    )


def _before(cursor: str | None) -> tuple[datetime, str, int] | None:
    """The point a trend's cursor names: ``<microseconds since 1970>.<run id>.<cycle>``."""
    if cursor is None:
        return None
    try:
        at, run_id, cycle = cursor.split(".")
        return _EPOCH + timedelta(microseconds=int(at)), run_id, int(cycle)
    except (ValueError, OverflowError):
        raise Refused(400, "malformed cursor") from None


async def cycles(
    ctx: Context, name: str, *, bucket: str | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """``GET /v1/readiness/pairs/{name}/cycles``: the pair's trend, a page of ``PAGE`` points oldest
    first, the pages going back in time (a keyset on ``(ended_at, run_id, cycle)``); by ``day`` or ``week``,
    each bucket's last evidence point."""
    settings = ctx.settings.readiness
    filters(settings, bucket=bucket)
    before = _before(cursor)
    async with ctx.pool.connection() as conn:
        await known(conn, settings, name)
        rows = await repo.readiness_points(
            conn, [name], bucket=bucket or "cycle", before=before, limit=PAGE + 1
        )
    items, following = gates.trend(rows, gates.policy(settings, name).gates.tier), None
    if len(rows) > PAGE:  # the point before the page told whether its first one begins a version
        items = items[1:]
        at = (items[0]["ended_at"] - _EPOCH) // timedelta(microseconds=1)
        following = f"{at}.{items[0]['run_id']}.{items[0]['cycle']}"
    return {"pair": name, "bucket": bucket or "cycle", "items": items, "next_cursor": following}


async def transitions(
    ctx: Context,
    *,
    pair: str | None = None,
    programme: str | None = None,
    limit: Any = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """``GET /v1/readiness/transitions``: the ``readiness.changed`` rows, newest first, of a pair or
    a programme if given: projections of the worker's rows only."""
    settings = ctx.settings.readiness
    filters(settings, programme=programme)
    size = _limit(limit)
    async with ctx.pool.connection() as conn:
        if pair is not None:
            await known(conn, settings, pair)
        try:
            rows, following = await repo.readiness_transitions(
                conn, pair=pair, programme=programme, limit=size, cursor=cursor
            )
        except ValueError:
            raise Refused(400, "malformed cursor") from None
    items = [{name: row[name] for name in ("seq", "at", "detail")} for row in rows]
    return {"items": items, "next_cursor": following}


# -- the settings as judged, and the stored measures ------------------------------------------


async def check(ctx: Context) -> tuple[list[str], list[str]]:
    """``ds readiness check``: each pair's effective gates, tagged with where each comes from, its
    deviations and the settings hash; and a warning for each listed spec that is not stored."""
    settings = ctx.settings.readiness
    names = named(settings)
    async with ctx.pool.connection() as conn:
        stored = set((await repo.readiness_pairs(conn, names))["pairs"])
    lines = []
    for name in sorted(stored | set(names)):
        policy, source = gates.policy(settings, name), gates.sources(settings, name)
        found = policy.gates
        cycles_ = f"{found.cycles} cycle{'' if found.cycles == 1 else 's'}"
        accuracy = "not required"
        if found.accuracy is not None:
            accuracy = f"{found.accuracy[0]:,} decided, {found.accuracy[1]:,} where side b was wrong"
        grouped = [f"programme {policy.programme}"] if policy.programme else []
        grouped += [f"entity {policy.entity}"] if policy.entity else []
        grouped += [f"closed {policy.closed}"] if policy.closed else []
        where = f" ({', '.join(grouped)})" if grouped else ""
        lines.append(
            f"{name}{where}: tier {found.tier} ({source['tier']}),"
            f" convergence {cycles_} ({source['cycles']}), fresh {found.max_age} ({source['max_age']}),"
            f" graph {'required' if found.graph else 'not required'} ({source['graph']}),"
            f" accuracy {accuracy} ({source['accuracy']})"
        )
        lines += [f"  deviation: {deviation}" for deviation in policy.deviations]
    lines.append(f"readiness settings {gates.config_sha256(settings)}")
    warnings = [
        f"programme {programme.name} names spec {listed_.spec}, which is not stored"
        for programme in (settings.programmes if settings else ())
        for listed_ in programme.pairs
        if listed_.spec not in stored
    ]
    return lines, warnings


async def verify(ctx: Context) -> dict[str, int]:
    """``ds readiness verify``: each cycle a ``readiness.measured`` row names must be named once and
    still hold a record with a pair and no error that hashes to it; each record with a pair and no error
    must be named; each excluded marker must agree with its run; and no record is neither. ValueError names
    the first that does not. How many were ``measured``, ``excluded`` and not measured (``errors``: a failed
    measure's record, which no row names)."""
    counts = {"measured": 0, "excluded": 0, "errors": 0}
    async with ctx.pool.connection() as conn:
        async for row in repo.readiness_measured(conn):
            record, at = row["readiness"] or {}, f"cycle {row['cycle']} of run {row['run_id']}"
            marker, measures = record.get("excluded"), "pair" in record and "error" not in record
            if row["rows"] > 1:  # the worker stores a cycle's measures once
                raise ValueError(
                    f"{at} is named by {row['rows']} readiness.measured rows: its measures were removed and"
                    " measured again"
                )
            if row["rows"] and not (measures and gates.measured_sha256(record) == row["sha256"]):
                raise ValueError(
                    f"{at} does not hash to its readiness.measured row (seq {row['seq']}): its measures"
                    " were altered after they were written"
                )
            if marker is not None:
                captured = row["mode"] != "one_shot" and row["samples_dir"] is not None
                if marker == "one_shot" and row["mode"] != "one_shot":
                    raise ValueError(f"{at} is marked excluded (one_shot) but its run is a {row['mode']}")
                if marker != "one_shot" and not captured:
                    raise ValueError(
                        f"{at} is marked excluded ({marker}) but its run read no captured samples"
                    )
                counts["excluded"] += 1
            elif "pair" not in record:
                raise ValueError(f"{at} holds a record that is neither measures nor an excluded marker")
            elif "error" in record:
                counts["errors"] += 1
            elif not row["rows"]:
                raise ValueError(f"{at} has measures no readiness.measured row names")
            else:
                counts["measured"] += 1
    return counts
