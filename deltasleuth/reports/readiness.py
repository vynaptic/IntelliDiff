"""Cut-over readiness reports: the dashboard as a PDF of at most ``VOLUME`` pairs
a volume, worst first, and as a CSV of one row per pair and gate; the trends CSV of one row per pair and
point, refused over ``TRENDS_ROWS`` rows; of a pair, its brief on one page, the brief's points as a CSV and
its root-cause groups as one, refused over ``CAUSES_ROWS`` rows; and, of a run, its report's section (its
pair's parity gates as of the reported cycle) and ``readiness.csv`` (its pair's points up to that cycle).

Each is built from the evaluation every surface shares (``deltasleuth.readiness``): named gates met or not
on observed evidence, never a percentage, a forecast or sign-off evidence, which the PDF's last section
says. Counts, ids, spec names, versions and slugs, and the truth source an accuracy gate's labelling names:
no key, no value and no person.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from deltasleuth import licence, readiness
from deltasleuth.context import Context
from deltasleuth.core import readiness as gates
from deltasleuth.core.readiness import worded
from deltasleuth.core.types import Outcome
from deltasleuth.readiness import Judged, Refused
from deltasleuth.reports.csv import render_csv
from deltasleuth.reports.render import Document, Section, render_pdf
from deltasleuth.store import repo

VOLUME = 100  # pairs a dashboard.pdf volume holds: a larger selection is split, never truncated
TRENDS_ROWS = 100_000  # rows trends.csv holds at most
RUN_ROWS = 500  # rows a run's readiness.csv holds at most: its pair's newest points up to the cycle
CHUNK = 5_000  # points of a pair trends.csv reads at a time, so that it never holds a pair's measures whole
CAUSES_ROWS = 100_000  # rows causes.csv holds at most
# What brief.pdf prints at most, so that it is one page: strata, blocking gates' texts and the characters
# of each, points and root-cause groups.
BRIEF_STRATA, BRIEF_GATES, BRIEF_TEXT, BRIEF_POINTS, BRIEF_GROUPS = 4, 3, 300, 5, 6
NAMES = ("dashboard.pdf", "dashboard.csv", "trends.csv")
BRIEFS = ("brief.pdf", "brief.csv", "causes.csv")  # a pair's reports: served with pair= only
DASHBOARD = (
    "programme", "entity", "pair", "version", "spec_id", "state", "regressed", "gates_passed",
    "gates_required", "gate", "required", "status", "value", "allowed", "over", "suppressed", "run_id",
    "cycle", "ended_at", "basis", "deviations", "config_sha256", "text",
)  # fmt: skip
TRENDS = (
    "programme", "entity", "pair", "version", "spec_id", "run_id", "cycle", "ended_at", "basis", "complete",
    "evidence", "total", "compared", "different", "unresolved", "absent_both", "api_uncomparable",
    "suppressed", "unexplained_critical", "new", "persistent", "fixed", "other", "sample_status",
    "graph_rows", "fails",
)  # fmt: skip
BRIEF = (
    "pair", "run_id", "cycle", "ended_at", "version", "spec_id", "basis", "complete", "evidence", "total",
    "compared", "different", "indeterminate", "absent_both", "api_uncomparable", "suppressed", "unresolved",
    "unexplained_critical", "new", "persistent", "fixed", "other", "sample_status", "graph_rows", "fails",
    "skipped", "boundary", "mismatch_rate", "coverage", *(outcome.lower() for outcome in Outcome),
    "cancelled", "outstanding", "root_causes", "marks",
)  # fmt: skip
CAUSES = (
    "pair", "root_cause_id", "state", "first_run_id", "first_cycle", "first_ended_at", "last_run_id",
    "last_cycle", "last_ended_at", "seen", "max_severity", "taxonomy_code", "change_type", "affected_keys",
    "since",
)  # fmt: skip
# The outcomes of a key compared, as a brief names them: their counts add up to the keys compared.
COMPARED = {
    "Match": "MATCH", "Known difference": "KNOWN_DIFF", "Noise only": "NOISE_ONLY", "Mismatch": "MISMATCH",
    "Not found on a": "NOT_FOUND_A", "Not found on b": "NOT_FOUND_B",
}  # fmt: skip
MARKS = {"version": "V", "keys": "K"}  # a point's marks as the brief's legend letters them
NOT = (  # what readiness is not
    "It gives no percentage, probability, weighted score or mean of readiness: a pair is k of m named gates,"
    " each met or not.",
    "It forecasts nothing: the only future time is a live soak's scheduled start, each time labelled a"
    " schedule, not a forecast.",
    "It is not a sign-off and not sign-off evidence: a four-eyes sign-off decides, and its evidence pack"
    " holds what was observed.",
    "A gate that is unknown is never counted as met.",
    "Counts of a key list describe those keys only, never a population.",
    "Convergence never crosses versions; rules that accept a difference lower the discrepant count, and the"
    " suppressed count stands beside it.",
    "No pooled rate is given of a sample: a sampled cycle is judged per stratum.",
    "Every figure is observed, none certified.",
)


@dataclass(frozen=True)
class Selection:
    """The pairs a dashboard takes, worst first, as evaluated (``found``), with what its header names."""

    found: Judged
    items: Sequence[Mapping[str, Any]]
    programme: str | None
    installation: str
    licence: str  # its state, ``unknown`` when it cannot be read
    worker: str | None  # the settings hash the worker last recorded
    config: str


async def selection(
    ctx: Context, *, programme: str | None = None, entity: str | None = None, pair: str | None = None
) -> Selection:
    """Evaluate the pairs (``pair``: that one) and take those of the programme and entity type, closed ones
    too."""
    settings = ctx.settings.readiness
    readiness.filters(settings, programme=programme)
    async with ctx.pool.connection() as conn:
        found = await readiness.judged(ctx, conn, only=pair)
        worker = await readiness.worker_config(conn)
        installation = await repo.installation_id(conn)
    items = readiness.chosen(list(found.items.values()), programme=programme, entity=entity, closed="all")
    state = (await licence.status(ctx))["state"]
    return Selection(found, items, programme, installation, state, worker, gates.config_sha256(settings))


def volumes(pairs: int) -> int:
    """How many dashboard volumes a selection of ``pairs`` pairs makes."""
    return max(1, -(-pairs // VOLUME))


def _volume(pairs: int, volume: Any) -> int:
    """The volume asked for of a selection of ``pairs`` pairs (the first by default); Refused (422) for one
    it has not, whichever report is asked for."""
    most = volumes(pairs)
    try:
        number = 1 if volume is None else int(volume)
    except ValueError:
        number = 0
    if not 1 <= number <= most:
        raise Refused(422, f"volume must be 1 to {most} for this selection ({VOLUME} pairs a volume)")
    return number


def _at(row: Mapping[str, Any]) -> str:
    return f"cycle {row['cycle']} of run {row['run_id']}"


def _point(point: Mapping[str, Any]) -> str:
    """One of a pair's last evidence points, as the dashboard lists it."""
    ended = f"{point['ended_at'].astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    said = f"{_at(point)}, ended {ended} (spec {point['spec_id']}): "
    if point["sampled"]:
        said += "sampled, judged per stratum"
    elif point["value"] is None:
        said += "its counts are not measured"
    else:
        said += (
            f"{point['value']:,} discrepant or unresolved key(s), {point['allowed']:,} allowed,"
            f" {point['suppressed']:,} suppressed"
        )
    failed = [gate for gate in point["failed"] if gate != "unknown"]
    return said + (f"; failed {', '.join(failed)}" if failed else "")


def _version(item: Mapping[str, Any]) -> str:
    """A pair's current version after its name; nothing without evidence."""
    return f" {item['version']} (spec {item['spec_id']})" if item["spec_id"] is not None else ""


def _pair(item: Mapping[str, Any]) -> Section:
    version = _version(item)
    grouped = [f"programme {item['programme']}"] if item["programme"] else []
    grouped += [f"entity {item['entity']}"] if item["entity"] else []
    held = item["convergence"]
    lines = [
        *([f"In {', '.join(grouped)}."] if grouped else []),
        *item["notes"],
        f"Convergence: {held['streak']:,} of the {held['needed']:,} evidence cycles in a row the gate needs.",
        f"Sign-off: {item['signoff_id'] or 'none'}. Evidence pack: {item['pack_id'] or 'none'}.",
        *([f"Deviations: {'; '.join(item['deviations'])}."] if item["deviations"] else []),
        *(["Last evidence cycles, oldest first:"] if item["trend"] else []),
        *map(_point, item["trend"]),
    ]
    score = item["score"]
    return Section(
        f"{item['pair']}{version}: {worded(item['state'])}, {score['passed']} of {score['required']} gates",
        lines,
        ("Gate", "Group", "Required", "Status", "Observed"),
        [
            (worded(g["gate"]), g["group"], "yes" if g["required"] else "no", worded(g["status"]), g["text"])
            for g in item["gates"]
        ],
    )


def _counts(items: Sequence[Mapping[str, Any]]) -> Section:
    """The open pairs of the whole selection by programme and entity type."""
    rows = []
    for rolled in gates.programmes(items):
        name = rolled["programme"] or "(none)"
        ready = f"{rolled['ready']:,} of {rolled['pairs']:,} pairs parity-ready or better"
        rows.append((name, "(all)", rolled["pairs"], ready, worded(rolled["worst"] or "-")))
        for entity in rolled["entities"]:
            states = ", ".join(f"{worded(state)} {n:,}" for state, n in entity["states"].items())
            rows.append((name, entity["entity"] or "(none)", sum(entity["states"].values()), states, ""))
    return Section(
        "Programmes and entity types", (), ("Programme", "Entity", "Open pairs", "States", "Worst"), rows
    )


def _meta(installation: str, now: datetime, config: str, state: str) -> str:
    """What heads a readiness PDF: the installation, when it was generated, the settings and the licence."""
    return (
        f"Installation {installation}, generated {now.astimezone(UTC):%Y-%m-%d %H:%M} UTC, readiness"
        f" settings {config}, licence state {state}"
    )


def document(chosen: Selection, volume: int = 1) -> Document:
    """A dashboard volume: its header, the selection's counts, each of its pairs and what it is not."""
    found, items, total = chosen.found, chosen.items, len(chosen.items)
    first = (volume - 1) * VOLUME
    pairs = items[first : first + VOLUME]
    held = f"pairs {first + 1:,}–{first + len(pairs):,} of {total:,}"  # noqa: RUF001 - an en dash, on purpose
    paged = f"volume {volume} of {volumes(total)} ({held})"
    meta = (
        _meta(chosen.installation, found.now, chosen.config, chosen.licence)
        + (f", programme {chosen.programme}" if chosen.programme else "")
        + (f", {paged}." if total else ".")
    )
    deviations = sorted({deviation for item in items for deviation in item["deviations"]})
    head = [
        gates.DEFINITION,
        *readiness.notes(found, chosen.config, chosen.worker),
        *([f"Deviations from the built-in defaults: {'; '.join(deviations)}."] if deviations else []),
    ]
    return Document(
        "Cut-over readiness (observed)",
        meta,
        [
            Section("What readiness is", head),
            _counts(items),
            *map(_pair, pairs),
            Section("What this report is not", NOT),
        ],
    )


async def dashboard_document(ctx: Context, volume: int = 1, **filters: Any) -> Document:
    """``dashboard.pdf``'s document: the volume of the pairs the filters (``selection``'s) take."""
    chosen = await selection(ctx, **filters)
    return document(chosen, _volume(len(chosen.items), volume))


def dashboard_csv(chosen: Selection) -> str:
    """``dashboard.csv``: one row per pair and gate."""
    rows = []
    for item in chosen.items:
        evidence, score = item["evidence"], item["score"]
        about = {name: item[name] for name in ("programme", "entity", "pair", "version", "spec_id")} | {
            "state": item["state"], "regressed": item["regressed"], "gates_passed": score["passed"],
            "gates_required": score["required"],
            "ended_at": evidence and evidence["ended_at"].isoformat(),
            "basis": evidence and evidence["basis"], "deviations": "; ".join(item["deviations"]),
            "config_sha256": item["config_sha256"],
        }  # fmt: skip
        counted = [gate for gate in item["gates"] if gate["gate"] == "targets" and gate["value"] is not None]
        suppressed = item["trend"][-1]["suppressed"] if counted else None  # beside the discrepant count
        rows += [
            about | gate | {"suppressed": suppressed if gate in counted else None} for gate in item["gates"]
        ]
    return render_csv(DASHBOARD, rows)


async def trends_csv(
    ctx: Context,
    *,
    programme: str | None = None,
    entity: str | None = None,
    pair: str | None = None,
    bucket: str | None = None,
    volume: Any = None,
    most: int | None = None,
) -> str:
    """``trends.csv``: one row per pair and point, oldest first, of the pairs the filters take (by ``day``
    or ``week``: each bucket's last evidence point); Refused (422) over ``most`` rows (a download's
    ``TRENDS_ROWS``), counted first, and for a ``volume`` the selection has not."""
    settings = ctx.settings.readiness
    readiness.filters(settings, programme=programme, bucket=bucket)
    listed = readiness.named(settings)
    async with ctx.pool.connection() as conn:
        if pair is not None:
            await readiness.known(conn, settings, pair)
        stored = (await repo.readiness_pairs(conn, listed))["pairs"]
        judged = {name: gates.policy(settings, name) for name in ([pair] if pair else {*stored, *listed})}
        policies = {
            name: policy
            for name, policy in sorted(judged.items())
            if (programme is None or policy.programme == programme)
            and (entity is None or policy.entity == entity)
        }
        _volume(len(policies), volume)
        count = await repo.count_points(conn, list(policies), bucket=bucket or "cycle")
        if most is not None and count > most:
            raise Refused(
                422,
                f"trends.csv would hold {count:,} rows, over its {most:,}: narrow it with programme=,"
                " entity=, pair= or bucket=day",
            )
        text = [render_csv(TRENDS, [])]
        for name, policy in policies.items():
            pages, before = [], None
            while True:  # newest first, a page at a time
                points = await repo.readiness_points(
                    conn, [name], bucket=bucket or "cycle", before=before, limit=CHUNK
                )
                rows = _trend_rows(name, policy, points)
                pages.append(render_csv(TRENDS, rows).partition("\r\n")[2])  # its rows, without the header
                if len(points) < CHUNK:
                    break
                before = points[-1]["ended_at"], points[-1]["run_id"], points[-1]["cycle"]
            text += reversed(pages)
    return "".join(text)


def _trend_rows(name: str, policy: gates.Policy, points: Sequence[repo.Row]) -> list[dict[str, Any]]:
    """The pair's points as ``TRENDS`` rows, oldest first."""
    about = {"programme": policy.programme, "entity": policy.entity, "pair": name}
    return [
        about | point | {"ended_at": point["ended_at"].isoformat(), "fails": " ".join(point["fails"])}
        for point in gates.trend(points, policy.gates.tier)
    ]


# -- a pair's brief -------------------------------------------------------------------------------


def _count(value: int | None, none: str = "not measured") -> str:
    return none if value is None else f"{value:,}"


def _cut(text: str) -> str:
    """A gate's text in ``BRIEF_TEXT`` characters at most: cut where a clause of it ends, if one does by
    then, so that a soak's schedule stands with its label or not at all."""
    if len(text) <= BRIEF_TEXT:
        return text
    end = text.rfind("; ", 0, BRIEF_TEXT)
    return f"{text[: end if end > 0 else BRIEF_TEXT]}…"


def _more(shown: Sequence[Any], total: int, where: str, what: str = "more") -> list[str]:
    """The line of a list brief.pdf cuts: how many it leaves out, and where they are."""
    return [f"… {total - len(shown):,} {what}: {where}"] if total > len(shown) else []


def _day(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d}"


def _figures(found: Mapping[str, Any]) -> Section:
    """The brief's newest complete cycle: its stored figures between its notes: that the gates judge
    another cycle above, its stored rate or how its sample is judged (with its strata) below, and last
    that its counts are of keys only; without one, why. Its coverage is worded as its rate is
    (``gates.ratio``)."""
    title, cycle, judged = "Newest complete cycle", found["cycle"], found["evidence"]
    if cycle is None:
        return Section(title, found["notes"][-1:])
    notes = list(cycle["notes"])
    above = [notes.pop(0)] if judged and _at(judged) != _at(cycle) else []
    rate = [notes.pop()] if cycle["mismatch_rate"] is not None else []
    sample = [notes.pop(0)] if cycle["sample_status"] is not None else []
    strata = (cycle["strata"] or [])[:BRIEF_STRATA]
    by = cycle["by_outcome"] or {}
    coverage = gates.ratio(cycle["coverage"], cycle["compared"], cycle["total"])
    six = ", ".join(f"{label} {_count(by.get(outcome))}" for label, outcome in COMPARED.items())
    lines = [
        *above,
        f"Cycle {cycle['cycle']} of run {cycle['run_id']}, ended"
        f" {cycle['ended_at'].astimezone(UTC):%Y-%m-%d %H:%M} UTC, {cycle['version']} (spec"
        f" {cycle['spec_id']}), basis {worded(cycle['basis'])}, {cycle['claim']}.",
        f"Keys attempted {_count(cycle['total'])}. Compared {_count(cycle['compared'])}"
        f"{'' if coverage is None else f' (coverage {coverage})'}: {six} (these six add up to Compared)."
        f" Inconclusive {_count(by.get('INCONCLUSIVE'))}, Drift {_count(by.get('DRIFT'))}, Unexplained"
        f" CRITICAL {_count(cycle['unexplained_critical'])}, Root-cause groups"
        f" {_count(cycle['root_causes'], 'not kept')}.",
        *rate,
        *sample,
        *strata,
        *_more(strata, len(cycle["strata"] or []), "the run's report.pdf"),
        *notes,
    ]
    return Section(title, lines)


def _gated(found: Mapping[str, Any]) -> Section:
    """The brief's gates: how many are met and each one's status, then the texts of the first blocking
    ones and of the spec approval, the sign-off and the pack, each cut by ``BRIEF_TEXT`` characters."""
    score, by = found["score"], {gate["gate"]: gate for gate in found["gates"]}
    statuses = "; ".join(
        f"{worded(gate['gate'])} {worded(gate['status'])}{'' if gate['required'] else ' (not required)'}"
        for gate in found["gates"]
    )
    first = found["blocking"][:BRIEF_GATES]
    told = [*first, *(gate for gate in ("spec_approval", "signoff", "pack") if gate not in first)]
    lines = [f"{worded(gate)} {worded(by[gate]['status'])}: {_cut(by[gate]['text'])}" for gate in told]
    lines[len(first) : len(first)] = _more(first, len(found["blocking"]), "Gates in full")
    lines.insert(0, f"{score['passed']} of {score['required']} gates: {statuses}.")
    return Section("Gates, approval and sign-off", lines)


def _trended(point: Mapping[str, Any]) -> str:
    """A point of the brief's trend in a line: its known differences and noise-only keys stand beside its
    different ones, and its stored rate reads as the reported cycle's does (``gates.ratio``)."""
    by, groups = point["by_outcome"], point["root_causes"]
    rate = gates.ratio(point["mismatch_rate"], point["different"], point["compared"])
    parts = ["its counts are not measured"]
    if None not in (by, point["compared"], point["different"]):
        parts = [
            f"{point['compared']:,} compared, {point['different']:,} different, {by['KNOWN_DIFF']:,} known"
            f" difference, {by['NOISE_ONLY']:,} noise only",
            ("sampled, no rate" if point["sample_status"] else "no rate") if rate is None else f"rate {rate}",
            "root-cause groups not kept" if groups is None else f"{groups:,} root-cause group(s)",
        ]
    parts += [" ".join(MARKS[mark] for mark in point["marks"])] if point["marks"] else []
    basis = worded(point["basis"] or "-") + ("" if point["complete"] else ", incomplete")
    return f"{_at(point)}, {_day(point['ended_at'])}, spec {point['spec_id']}, {basis}: {'; '.join(parts)}"


def _group(group: Mapping[str, Any]) -> str:
    """A root-cause group of the brief in a line: its id, its state, what its last row says and the cycles
    holding it; never its path or a key."""
    first, last = group["first"], group["last"]
    said = " ".join(filter(None, (group[name] for name in ("max_severity", "taxonomy_code", "change_type"))))
    return (
        f"{group['root_cause_id']} {gates.reads(group)}: {said}, {group['affected_keys']:,} key(s), in"
        f" {group['seen']:,} cycle(s), first {_at(first)} ({_day(first['ended_at'])}), last {_at(last)}"
        f" ({_day(last['ended_at'])})"
    )


def brief_document(found: Mapping[str, Any], installation: str, state: str) -> Document:
    """``brief.pdf``'s document: a pair's brief (``readiness.brief``'s answer) on one page, in lines under
    the dashboard's header: its state under the definition and what a brief is, its newest complete cycle's
    stored figures, its gates, its newest points and its first root-cause groups. Each list is cut where a
    line says so, and what does not fit the page is scaled down (``Document.compact``). The note of a brief
    without a reported cycle or whose cycle is purged, the last one, stands in its section."""
    points, causes = found["points"], found["causes"]
    notes = found["notes"][: -1 if causes is None else None]
    shown = points[-BRIEF_POINTS:]
    grouped = []
    if causes is not None:
        listed = causes["items"][:BRIEF_GROUPS]
        grouped = [causes["text"], *map(_group, listed), *_more(listed, causes["total"], "causes.csv")]
    elif found["cycle"] is not None:
        grouped = found["notes"][-1:]
    return Document(
        "Cut-over brief (observed)",
        _meta(installation, found["now"], found["config_sha256"], state) + ".",
        [
            Section(
                f"{found['pair']}{_version(found)}: {worded(found['state'])}"
                + (", regressed" if found["regressed"] else ""),
                [
                    found["definition"],
                    found["brief"],
                    *notes,
                    *([f"Deviations: {'; '.join(found['deviations'])}."] if found["deviations"] else []),
                ],
            ),
            _figures(found),
            _gated(found),
            Section(
                "Trend",
                [*_more(shown, len(points), "brief.csv", "earlier"), *map(_trended, shown), found["legend"]],
            ),
            *([Section("Root-cause groups", grouped)] if grouped else []),
        ],
        compact=True,
    )


async def brief_pdf(ctx: Context, found: Mapping[str, Any]) -> bytes:
    """``brief.pdf`` of a pair's brief, under the installation's id and its licence state."""
    async with ctx.pool.connection() as conn:
        installation = await repo.installation_id(conn)
    return render_pdf(brief_document(found, installation, (await licence.status(ctx))["state"]))


def brief_csv(found: Mapping[str, Any]) -> str:
    """``brief.csv``: one row per point of a pair's brief, oldest first, its stored count of each outcome a
    column."""
    rows = [
        {"pair": found["pair"]}
        | point
        | {outcome.lower(): count for outcome, count in (point["by_outcome"] or {}).items()}
        | {"ended_at": point["ended_at"].isoformat()}
        | {name: " ".join(point[name]) for name in ("fails", "marks")}
        for point in found["points"]
    ]
    return render_csv(BRIEF, rows)


async def causes_csv(ctx: Context, pair: str, most: int | None = None) -> str:
    """``causes.csv``: one row per root-cause group of the pair's brief, every one of them, never cut;
    Refused (422) over ``most`` rows (a download's ``CAUSES_ROWS``). The header alone for a brief without
    groups: no complete cycle, or a purged one."""
    causes = (await readiness.brief(ctx, pair, whole=True, most=most))["causes"]
    groups = causes["items"] if causes else []
    if causes and causes["total"] > len(groups):
        raise Refused(422, f"causes.csv would hold {causes['total']:,} rows, over its {most:,}")
    rows = [
        {"pair": pair}
        | group
        | {f"{end}_{name}": group[end][name] for end in ("first", "last") for name in ("run_id", "cycle")}
        | {f"{end}_ended_at": group[end]["ended_at"].isoformat() for end in ("first", "last")}
        | {"since": " ".join(group["since"])}
        for group in groups
    ]
    return render_csv(CAUSES, rows)


def _excluded(run: Mapping[str, Any]) -> str | None:
    """Why readiness does not assess the run, as ``repo.end_cycle`` marks its cycles."""
    if run["mode"] == "one_shot":
        return "one-shot"
    return "captured samples" if run["params"].get("samples_dir") is not None else None


async def run_section(ctx: Context, conn: repo.Conn, run: Mapping[str, Any], cycle: int) -> Section:
    """``report.pdf``'s section on the run's pair: its parity gates as of the reported cycle, judged
    on the cycles ended by then under the settings its lines name, so that the database gives them again;
    or why there are none."""
    title, pair = "Pair readiness (observed)", run["spec_name"]
    if (why := _excluded(run)) is not None:
        return Section(title, [f"This run is not assessed for readiness: {why}."])
    row = await repo.get_cycle(conn, run["run_id"], cycle)
    if row is None:
        return Section(title, ["Readiness is judged when the cycle ends."])
    if row["readiness"] is None:
        return Section(title, ["This cycle is not measured yet."])
    item = (await readiness.judged(ctx, conn, only=pair, at=row["ended_at"])).items[pair]
    lines = [
        gates.DEFINITION,
        f"The parity gates of {pair} as of cycle {cycle} of run {run['run_id']} (ended"
        f" {row['ended_at'].astimezone(UTC):%Y-%m-%d %H:%M} UTC), judged on the cycles ended by then under"
        f" readiness settings {item['config_sha256'][:12]}: the database gives them again under the same"
        " settings.",
        *([f"Deviations: {'; '.join(item['deviations'])}."] if item["deviations"] else []),
        f"Today's state: the Readiness screen (#/readiness/{pair}).",
    ]
    rows = [
        (worded(g["gate"]), "yes" if g["required"] else "no", worded(g["status"]), g["text"])
        for g in item["gates"]
        if g["group"] == "parity"
    ]
    return Section(title, lines, ("Gate", "Required", "Status", "Observed"), rows)


async def run_csv(ctx: Context, run_id: str, cycle: int | None = None) -> bytes:
    """``reports.FORMATS["readiness.csv"]``: the points of the run's pair up to the cycle (the run's
    current one by default; every point while it is live), the newest ``RUN_ROWS`` at most, oldest first,
    as ``trends.csv`` lists them; LookupError for an unknown run."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        name = run["spec_name"]
        ended = await repo.get_cycle(conn, run_id, cycle or run["cycle"])
        before = ended and (ended["ended_at"], run_id, ended["cycle"] + 1)
        points = await repo.readiness_points(conn, [name], before=before, limit=RUN_ROWS)
    rows = _trend_rows(name, gates.policy(ctx.settings.readiness, name), points)
    return render_csv(TRENDS, rows).encode()


async def download(
    ctx: Context,
    name: str,
    *,
    programme: str | None = None,
    entity: str | None = None,
    pair: str | None = None,
    bucket: str | None = None,
    volume: Any = None,
) -> tuple[str, bytes, str]:
    """A readiness report by its published name: its media type, its bytes and the name it downloads
    under (a later dashboard volume's says which). ``bucket`` and ``volume`` are judged for every report,
    though only the trends are bucketed and only the PDF has volumes. A pair's reports (``BRIEFS``) are
    Refused (422) without ``pair``, and are that pair's whatever ``programme`` and ``entity`` say."""
    filters = {"programme": programme, "entity": entity, "pair": pair}
    readiness.filters(ctx.settings.readiness, programme=programme, bucket=bucket)
    if name in BRIEFS:
        if pair is None:
            raise Refused(422, f"{name} is a pair's report: give pair=")
        _volume(1, volume)
        if name == "causes.csv":
            return "text/csv", (await causes_csv(ctx, pair, CAUSES_ROWS)).encode(), name
        found = await readiness.brief(ctx, pair)
        if name == "brief.csv":
            return "text/csv", brief_csv(found).encode(), name
        return "application/pdf", await brief_pdf(ctx, found), name
    if name == "trends.csv":
        text = await trends_csv(ctx, bucket=bucket, volume=volume, most=TRENDS_ROWS, **filters)
        return "text/csv", text.encode(), name
    chosen = await selection(ctx, **filters)
    number = _volume(len(chosen.items), volume)
    if name == "dashboard.csv":
        return "text/csv", dashboard_csv(chosen).encode(), name
    named = name if number == 1 else f"dashboard-{number}.pdf"
    return "application/pdf", render_pdf(document(chosen, number)), named
