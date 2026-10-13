"""Cut-over readiness (docs/mvp/READINESS.md): named gates, each met or not on stored evidence, and a
pair's place on a fixed ladder. Pure: ``runner.readiness`` measures each ended cycle once, the store keeps
the measures, and every surface judges them here.

A pair is a spec name, judged on its current version: the one of its newest evidence point. ``measured``
derives what a cycle's measures say of it (its basis, whether it is complete and evidence, the per-cycle
gates it fails at each tier); ``policy`` merges the settings' gates for a pair. An evaluation reads the
pair's ``repo.readiness_series``, takes its ``streak`` (the clean evidence cycles in a row), reads
``repo.readiness_governance`` from where the streak began, and ``evaluate`` gives each gate's status and
text, the state, the score and what is missing. Nothing here is a probability, a forecast or sign-off
evidence (``DEFINITION``); every count is observed. A pair's brief restates stored measures beside that
(``BRIEF``): its last measured cycles with their marks (``marked``), its newest complete one (``reported``)
and its root-cause groups, opened, still open and closed (``burn_down``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any

from deltasleuth.core import jsonio, sampling
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.sampling import percent, share
from deltasleuth.core.spec.durations import parse_duration

if TYPE_CHECKING:
    from deltasleuth.config import GateSettings, ReadinessSettings

FORMAT = 1  # of a cycle's measures (ds.cycles.readiness); a new definition is a new format
DEFINITION = (
    "Readiness is a set of named gates, each met or not on observed evidence, and a place on a fixed ladder."
    " It is not a probability that the two systems are at parity, not a forecast, and not sign-off evidence:"
    " a four-eyes sign-off decides, and its evidence pack certifies what was observed. For a pair observed"
    " whole, the targets gate is one pooled count over the pair; architecture §15's per-stratum targets are"
    " judged only for sampled cycles, and otherwise by the approver."
)
BRIEF_CYCLES = 30  # the measured cycles a pair's brief lists, newest last
BRIEF = (
    "This brief restates stored measures of one pair's newest complete cycle and last"
    f" {BRIEF_CYCLES} measured cycles: counts, stored counters and per-stratum bounds, observed. It adds no"
    " score, says nothing of the future, attests nothing and decides nothing."
)
LEGEND = (
    "Marks: V a new spec version; K another key source or key-list size. Hollow: an incomplete cycle. A"
    " sampled cycle has no rate and draws its own keys. A purged cycle keeps no root-cause groups."
)  # of a brief's trend
GROUPS = {
    "population": "parity", "critical": "parity", "targets": "parity", "convergence": "parity",
    "fresh": "parity", "graph": "parity", "spec_approval": "governance", "adjudication": "governance",
    "sample_ledger": "governance", "accuracy": "governance", "signoff": "evidence", "pack": "evidence",
}  # fmt: skip
GATES = tuple(GROUPS)  # in the ladder's order
STATES = (
    "no_evidence", "not_ready", "unknown", "parity_ready", "signoff_ready", "signed_off", "evidenced",
)  # lowest first  # fmt: skip
CAUSES = (
    "baseline", "cycle", "version", "signoff", "pack", "adjudication", "accuracy", "ledger", "licence",
    "config", "closed", "time",
)  # why a pair's ``readiness.changed`` row was written (§8)  # fmt: skip
# The cause a changed governance or evidence gate names (§8): the stored fact it reads.
_GATE_CAUSES = {
    "spec_approval": "signoff", "adjudication": "adjudication", "sample_ledger": "ledger",
    "accuracy": "accuracy", "signoff": "signoff", "pack": "pack",
}  # fmt: skip
COUNTS = (
    "total", "compared", "different", "indeterminate", "cancelled", "outstanding", "absent_both",
    "api_uncomparable", "suppressed",
)  # copied from a cycle's counters  # fmt: skip
GRAPH_CODES = (
    "CLUSTER_SPLIT", "CLUSTER_MERGE", "CLUSTER_REARRANGE", "GROUP_MEMBERS_DIFFER", "CLUSTER_OVERSIZED",
)  # ds.graph_diffs' codes; the gate allows none of the first four  # fmt: skip
CYCLES, MAX_AGE = 3, "7d"  # this contract's defaults: evidence cycles in a row, and the evidence cycle's age
EXIT_MINIMUM = 50  # reports.adjudicate.EXIT_MINIMUM
MODES = {"tokens": "tokens mode", "none": "developer mode"}  # approvals.MODES
LICENCE_UNREAD = "the licence standing could not be read (see the server log)"
COUNTED = 100  # a pair's cycles are counted up to this many (repo's reads): one more reads as over it
_PENDING = "side a's key census is pending"  # pack.census_problem's words for a census not counted yet
_COUNTED_WITH = " (side a is counted only with packs or readiness configured, PACK §1.3)"
Row = Mapping[str, Any]  # a cycle as the store gives it: run_id, cycle, ended_at and its readiness measures


# -- settings (§7) --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gates:
    """A pair's gates as judged: the settings' values over this contract's defaults."""

    tier: str = "standard"
    cycles: int = CYCLES
    max_age: str = MAX_AGE
    graph: bool = False
    accuracy: tuple[int, int] | None = None  # (min_decided, max_b_wrong); None: not required


@dataclass(frozen=True)
class Policy:
    """What a pair is judged by, and grouped under (``policy``)."""

    gates: Gates
    programme: str | None
    entity: str | None
    closed: date | None  # judged as of that day
    sha256: str  # of the installation's readiness settings as judged (``config_sha256``)
    deviations: tuple[str, ...]  # each non-default value, named on every surface


def _set(level: GateSettings) -> dict[str, Any]:
    """The gates a level sets, as ``Gates`` names them."""
    values = {
        "tier": level.tier,
        "cycles": level.convergence and level.convergence.cycles,
        "max_age": level.fresh and level.fresh.max_age,
        "graph": level.graph and level.graph.required,
        "accuracy": level.accuracy and (level.accuracy.min_decided, level.accuracy.max_b_wrong),
    }
    return {name: value for name, value in values.items() if value is not None}


def _merged(levels: Iterable[GateSettings]) -> Gates:
    """The gates the levels set, each over the one before."""
    found = Gates()
    for level in levels:
        found = replace(found, **_set(level))
    return found


def effective(settings: ReadinessSettings | None) -> dict[str, Any]:
    """The readiness settings as judged, which ``readiness.config`` records: the installation's gates and
    each listed pair's, defaults filled in."""
    levels = [] if settings is None else [settings.gates]
    return {
        "gates": asdict(_merged(levels)),
        "programmes": [
            {
                "name": programme.name,
                "pairs": [
                    {
                        "spec": pair.spec,
                        "entity": pair.entity,
                        "closed": pair.closed and pair.closed.isoformat(),
                        "gates": asdict(_merged([*levels, programme.gates, pair.gates])),
                    }
                    for pair in programme.pairs
                ],
            }
            for programme in (settings.programmes if settings else ())
        ],
    }


@cache
def config_sha256(settings: ReadinessSettings | None) -> str:
    """The hash every surface names the settings by: absent and ``{}`` judge alike, so hash alike."""
    return sha256_hex(jsonio.dumps(effective(settings), sort_keys=True))


def _deviations(gates: Gates) -> tuple[str, ...]:
    found = []
    if gates.tier != "standard":
        found.append(f"tier {gates.tier} (default standard)")
    if gates.cycles != CYCLES:
        relaxed = "; relaxed" if gates.cycles < CYCLES else ""
        found.append(
            f"convergence {gates.cycles} cycle{'' if gates.cycles == 1 else 's'} (default {CYCLES}{relaxed})"
        )
    if (age := parse_duration(gates.max_age)) != parse_duration(MAX_AGE):
        relaxed = "; relaxed" if age > parse_duration(MAX_AGE) else ""
        found.append(f"fresh {gates.max_age} (default {MAX_AGE}{relaxed})")
    if gates.graph:
        found.append("graph required (default not required)")
    if gates.accuracy is not None:
        decided, wrong = gates.accuracy
        found.append(f"accuracy {decided:,} decided, {wrong:,} where side b was wrong (default not required)")
    return tuple(found)


def _levels(settings: ReadinessSettings | None, name: str) -> tuple[Any, Any, Any]:
    """The levels that set the pair ``name``'s gates: the installation's, its programme's and its own."""
    programme, pair = next(
        ((p, x) for p in (settings.programmes if settings else ()) for x in p.pairs if x.spec == name),
        (None, None),
    )
    return settings, programme, pair


def sources(settings: ReadinessSettings | None, name: str) -> dict[str, str]:
    """Where each of the pair's gates comes from (``ds readiness check``): ``default``, ``installation``,
    ``programme`` or ``pair``."""
    found = dict.fromkeys(asdict(Gates()), "default")
    for source, level in zip(("installation", "programme", "pair"), _levels(settings, name), strict=True):
        found |= dict.fromkeys(_set(level.gates) if level is not None else (), source)
    return found


def policy(settings: ReadinessSettings | None, name: str) -> Policy:
    """The pair ``name``'s gates: the build's, then the installation's, its programme's and its own."""
    _, programme, pair = levels = _levels(settings, name)
    gates = _merged(level.gates for level in levels if level is not None)
    return Policy(
        gates,
        programme and programme.name,
        pair and pair.entity,
        pair and pair.closed,
        config_sha256(settings),
        _deviations(gates),
    )


@dataclass(frozen=True)
class Installation:
    """What an evaluation takes of the installation as it stands (D9): its auth ``mode`` (none, tokens or
    oidc), whether it has ``packs`` configured, what its licence ``allows`` (``Standing.allows()``; None:
    the standing could not be read), and the two judges this module cannot import: ``accuracy``
    (``reports.accuracy.gate``) and ``stops`` (``runner.stop.StopConditions.from_spec``)."""

    mode: str
    packs: bool
    allows: Mapping[str, bool] | None
    accuracy: Callable[..., tuple[str, list[str]]]
    stops: Callable[[Mapping[str, Any]], Any]


# -- measures (§4) --------------------------------------------------------------------------------------


def measured_sha256(record: Mapping[str, Any]) -> str:
    """The hash ``readiness.measured`` names a cycle's stored measures by, and ``ds readiness verify``
    checks them against."""
    return sha256_hex(jsonio.dumps(record, sort_keys=True))


def measured(
    record: Mapping[str, Any],
    *,
    own: bool,
    problem: str | None,
    method: str | None = None,
    whole: bool | None = None,
    cut: bool = False,
) -> dict[str, Any]:
    """A cycle's format-1 measures: ``record`` (the counts, ids and codes its tombstone and rows hold) with
    what they say of it: its ``basis`` (§3), why it is ``incomplete``, whether it is ``complete`` and
    ``evidence``, and the per-cycle gates it ``fails``. ``own``: its counters are its own; ``problem``:
    ``pack.census_problem``'s text, None when its census counts side a; ``method``: a hash sample's;
    ``whole``: whether a table scan read its tables to their end, uncut (None: its state is purged);
    ``cut``: maxKeys cut its keys."""
    source = record["keys_source"]
    if problem is not None:  # a whole scan's too: its side a's files held lines that were not rows
        basis = "keys"
    elif source == "table_scan":
        basis = "unknown" if whole is None else "census" if whole else "partial"
    elif source == "hash_sample":  # its frame counts side a: read here only, never as a census
        basis = "sample" if method == "bottom_k" else "rate_sample"
    else:
        basis = "census"
    if own and any(record[name] is None for name in COUNTS):  # counters older than the fields read
        basis = "unknown"
    reasons = {
        "no_counters": not own,
        "cancelled": record["cancelled"],
        "outstanding": record["outstanding"],
        "nothing_compared": own and not record["compared"],
        "scan_partial": source == "table_scan" and whole is False and not cut,
        "cut": cut,
    }
    incomplete = [code for code, holds in reasons.items() if holds]
    found = dict(record) | {
        "format": FORMAT,
        "basis": basis,
        "incomplete": incomplete,
        "complete": not incomplete,
        "evidence": not incomplete and not (source == "explicit" and basis != "census"),
    }
    return found | {"fails": {tier: _fails(found, tier) for tier in sampling.TARGETS}}


def _read(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """A cycle's measures as this build reads them (§4): a record of another format is an unknown point and
    no evidence, of which only its format, pair and version are told."""
    if record.get("format") == FORMAT:
        return record
    unknown = {tier: ["unknown"] for tier in sampling.TARGETS}
    return {name: record.get(name) for name in ("format", "pair", "spec_id", "version")} | {"fails": unknown}


def _pooled(record: Mapping[str, Any], tier: str) -> dict[str, tuple[int, int]]:
    """§5's pooled rule over a cycle's counts at ``tier``, as (count, allowance): the discrepant or
    unresolved keys, the keys absent on both sides and those above the API cap. None of them is known for
    counters without the fields."""
    if any(record[name] is None for name in COUNTS):
        return {}
    t = sampling.TARGETS[tier]
    n, absent, api = record["total"], record["absent_both"], record["api_uncomparable"]
    unresolved = record["indeterminate"] - absent - api
    return {
        "discrepant": (record["different"] + unresolved, share(t, n - absent)),
        "absent": (absent, share(t, n)),
        "api": (api, 0),
    }


def _certain(record: Mapping[str, Any]) -> bool:
    """Whether a point's failure of the pooled rule is certain whatever its unread keys show (D3): it lacks
    only keys cancelled or owed a recheck, or compared none of its keys (a partial scan or a cut key set
    never showed its population), and it is no key list whose census does not count side a."""
    lacking = set(record["incomplete"])
    listed = record["keys_source"] == "explicit" and record["basis"] != "census"
    return bool(lacking) and lacking <= {"cancelled", "outstanding", "nothing_compared"} and not listed


def _fails(record: Mapping[str, Any], tier: str) -> list[str]:
    """The per-cycle parity gates the point fails at ``tier``; ``unknown`` for what was never measured."""
    evidence, status = record["evidence"], record["sample_status"]
    found = {"population": evidence and record["basis"] in ("keys", "rate_sample")}
    found["critical"] = bool(record["unexplained_critical"])
    if record["keys_source"] == "hash_sample":
        below = tier == "high" and any(s["tier"] != "high" for s in record["sample_strata"] or ())
        found["targets"] = status == "failed" or (
            evidence and (status not in ("passed", "unjudged") or below)
        )
    else:
        over = any(count > allowed for count, allowed in _pooled(record, tier).values())
        found["targets"] = over and (evidence or _certain(record))
    found["unknown"] = (
        record["basis"] == "unknown"
        or record["unexplained_critical"] is None
        or (evidence and status == "unjudged")
    )
    return [gate for gate, failed in found.items() if failed]


# -- series, convergence and trend (§6) -----------------------------------------------------------------


def _when(row: Row) -> tuple[datetime, str, int]:
    return row["ended_at"], row["run_id"], row["cycle"]


@dataclass(frozen=True)
class Streak:
    """A pair's convergence: its ``evidence`` cycle (the current version's newest evidence point; None
    without one), the ``k`` evidence cycles of that version in a row since the ``boundary`` (the point or
    unmeasured cycle the streak ends at; None: the pair has none) and what kind of end it is: ``failed``
    (a per-cycle gate), ``unknown`` (never measured), ``version`` (another version's evidence) or
    ``unmeasured``. Behind an ``unknown`` or ``unmeasured`` end: ``failed``, the newest point that failed
    a per-cycle gate since another version's evidence (None without one), and ``most``, the cycles the
    streak would count were every unknown and unmeasured point since then clean evidence."""

    evidence: Row | None = None
    k: int = 0
    boundary: Row | None = None
    kind: str | None = None
    failed: Row | None = None
    most: int = 0

    @property
    def after(self) -> datetime | None:
        """When the streak began: an evidence cycle of the current version ended later is in it."""
        return self.boundary and self.boundary["ended_at"]

    def holds(self, row: Row) -> bool:
        """Whether a cycle (``repo.readiness_governance``'s) is in the streak: an evidence point of the
        current version ended after the boundary, whatever its age. A cycle that is no evidence, an
        excluded one among them, never is."""
        current = self.evidence and self.evidence["readiness"]["spec_id"]
        evidence = _read(row["readiness"] or {}).get("evidence")
        return (
            bool(evidence)
            and row["spec_id"] == current
            and (self.after is None or row["ended_at"] > self.after)
        )


def window(policy: Policy) -> int:
    """W: the evidence cycles a streak counts at most."""
    return max(60, 3 * policy.gates.cycles)


def _failed(row: Row, tier: str) -> list[str]:
    """The per-cycle gates the point fails at ``tier``, ``unknown`` aside."""
    return [gate for gate in _read(row["readiness"])["fails"][tier] if gate != "unknown"]


def streak(series: Mapping[str, Any], policy: Policy, unmeasured: Mapping[str, Any] | None = None) -> Streak:
    """The pair's streak from its ``repo.readiness_series`` (read at the policy's tier and K) and, if any
    of its cycles is not measured yet, ``repo.unmeasured_pairs``'s record of them. Its boundary is the
    newest of the newest point failing at the tier, the newest evidence point of another version and the
    oldest unmeasured cycle."""
    versions, tier = series["versions"], policy.gates.tier
    if not versions:
        return Streak()
    breaks = series["breaks"]  # the newest K points failing at the tier, any of them unknown
    failed = next((row for row in breaks if _failed(row, tier)), None)
    certain = [(row, "version") for row in versions[1:2]] + ([(failed, "failed")] if failed else [])
    ends = certain + [(row, "unknown") for row in breaks[:1] if row is not failed]
    if unmeasured is not None:
        ends.append((unmeasured["oldest"], "unmeasured"))
    boundary, kind = max(ends, key=lambda end: _when(end[0]), default=(None, None))
    since, ended = max(certain, key=lambda end: _when(end[0]), default=(None, None))

    def after(row: Row | None, cycles: Iterable[Row]) -> set[Any]:
        return {_when(cycle) for cycle in cycles if row is None or cycle["ended_at"] > row["ended_at"]}

    points = series["points"]  # those since what certainly ended the streak: the unknown breaks apply here
    most = len(after(since, [*points, *breaks])) + (unmeasured["cycles"] if unmeasured is not None else 0)
    k = min(len(after(boundary, points)), window(policy))
    return Streak(versions[0], k, boundary, kind, since if ended == "failed" else None, most)


def candidate(found: Streak, governance: Mapping[str, Any]) -> Row | None:
    """The candidate cycle (§3), as ``repo.readiness_governance`` read it: in the streak, an approved
    sign-off's cycle, else an open request's, else the newest cycle holding a passed adjudication a person
    scored or a labelling a person labelled; else the evidence cycle. A new cycle so never demotes
    adjudicated work still in the streak, and a cycle that is no evidence point is never the candidate."""
    if found.evidence is None:
        return None
    cycles = {(c["run_id"], c["cycle"]): c for c in governance["cycles"]}
    held = [c for c in cycles.values() if found.holds(c)]
    for status in ("approved", "requested"):
        signed = {(s["run_id"], s["cycle"]) for s in governance["signoffs"] if s["status"] == status}
        if chosen := max((c for c in held if (c["run_id"], c["cycle"]) in signed), key=_when, default=None):
            return chosen
    worked = max((c for c in held if c["worked"]), key=_when, default=None)
    return worked or cycles.get((found.evidence["run_id"], found.evidence["cycle"]))


def _unresolved(record: Mapping[str, Any]) -> int | None:
    if any(record.get(name) is None for name in COUNTS):
        return None
    return record["indeterminate"] - record["absent_both"] - record["api_uncomparable"]


def spark(points: Sequence[Row], tier: str) -> list[dict[str, Any]]:
    """An item's ``trend``: the pair's last evidence points, oldest first, each with its discrepant or
    unresolved keys against the tier's allowance and its suppressed keys beside them (all None for a
    sampled cycle, judged per stratum) and the per-cycle gates it failed."""
    found = []
    for row in sorted(points, key=_when):
        record = row["readiness"]
        sampled = record["keys_source"] == "hash_sample"
        value, allowed = (None, None) if sampled else _pooled(record, tier).get("discrepant", (None, None))
        found.append(
            {name: row[name] for name in ("run_id", "cycle", "ended_at")}
            | {"spec_id": record["spec_id"], "sampled": sampled, "value": value, "allowed": allowed}
            | {"suppressed": None if value is None else record["suppressed"], "failed": record["fails"][tier]}
        )
    return found


def trend(points: Sequence[Row], tier: str, bucket: str = "cycle") -> list[dict[str, Any]]:
    """A pair's points, oldest first, as its trend lists them: each cycle's version, basis, counts, changes
    and the gates it fails at ``tier``, ``skipped`` when it is no evidence point and ``boundary`` when its
    version differs from the point before. A sampled cycle's counts are its sample's own, beside its
    ``sample_status``: no rate and no bound of the strata pooled. By ``day`` or ``week`` (UTC; ISO weeks),
    each bucket's last evidence point, never an average. Concurrent runs interleave by when their cycles
    ended."""
    rows = sorted(points, key=_when)
    if bucket != "cycle":
        last: dict[Any, Row] = {}
        for row in rows:
            day = row["ended_at"].astimezone(UTC).date()
            if _read(row["readiness"]).get("evidence"):
                last[day if bucket == "day" else day.isocalendar()[:2]] = row
        rows = list(last.values())
    found, before = [], None
    for row in rows:
        record = _read(row["readiness"])
        graph, changes = record.get("graph"), record.get("changes") or {}
        found.append(
            {name: row[name] for name in ("run_id", "cycle", "ended_at")}
            | {name: record.get(name) for name in ("version", "spec_id", "basis", "complete", "evidence")}
            | {name: record.get(name) for name in COUNTS if name not in ("cancelled", "outstanding")}
            | {"unresolved": _unresolved(record), "unexplained_critical": record.get("unexplained_critical")}
            | {name: changes.get(name) for name in ("new", "persistent", "fixed", "other")}
            | {"sample_status": record.get("sample_status")}
            | {"graph_rows": graph and sum(graph[code] for code in GRAPH_CODES)}
            | {"fails": record["fails"][tier], "skipped": not record.get("evidence")}
            | {"boundary": before is not None and record["spec_id"] != before}
        )
        before = record["spec_id"]
    return found


# -- texts (§9) -----------------------------------------------------------------------------------------


def _at(row: Row) -> str:
    return f"cycle {row['cycle']} of run {row['run_id']}"


def _cycle(row: Row, evidence: Row) -> str:
    """``cycle 14``, with its run when that is not the evidence cycle's."""
    return f"cycle {row['cycle']}" if row["run_id"] == evidence["run_id"] else _at(row)


def _named(record: Mapping[str, Any]) -> str:
    """A version as texts name it: two spec ids may share a version string."""
    return f"{record['version']} (spec {record['spec_id']})"


def _day(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d}"


def _n(value: int, unit: str) -> str:
    return f"{value:,} {unit}{'' if value == 1 else 's'}"


def worded(code: str) -> str:
    """A state, a gate or a status as texts spell it: ``signoff_ready`` is sign-off ready."""
    return code.replace("signoff", "sign-off").replace("_", " ")


def _span(duration: str) -> str:
    """A setting's duration as texts spell it: ``7d`` is 7 days."""
    units = {"ms": "millisecond", "s": "second", "m": "minute", "h": "hour", "d": "day"}
    unit = "ms" if duration.endswith("ms") else duration[-1]
    number = duration.removesuffix(unit)
    return f"{number} {units[unit]}{'' if number == '1' else 's'}"


def _ago(seconds: float) -> str:
    """How long ago, in whole days, else in whole hours."""
    whole = int(max(seconds, 0))
    return f"{_n(whole // 86400, 'day') if whole >= 86400 else _n(whole // 3600, 'hour')} ago"


def _unmeasured(row: Row, at: str) -> str | None:
    """Why a point's measures say nothing of it (Y7), if they do not."""
    record = row.get("readiness")
    if record is None:
        return f"{at}, which is not measured yet"
    if record.get("format") != FORMAT:
        return f"{at}, whose measures are of format {record.get('format')}, which this version does not read"
    if "error" in record:
        return f"{at}, which could not be measured ({record['error']})"
    if record["late"]:
        return f"{at}, whose findings were purged before 014"
    if record["basis"] == "unknown":
        return f"{at}, whose counters predate the fields readiness reads"
    if record["evidence"] and record["sample_status"] == "unjudged":
        return f"{at}, whose sample ended without a judgement"
    return None


def _not_evidence(row: Row) -> str:
    """Y14: why a point is no evidence, in the sign-off's words (docs/mvp/APPROVALS.md §1.2)."""
    record, at = row["readiness"], _at(row)
    if record.get("format") != FORMAT:
        return f"{at} holds measures of format {record.get('format')}, which this version does not read"
    if "error" in record:
        return f"{at} could not be measured ({record['error']})"
    lacking = record["incomplete"]
    if "no_counters" in lacking:
        return f"{at} has no counters of its own (failed, erased or abandoned)"
    if "cancelled" in lacking or "outstanding" in lacking:
        return (
            f"{at} is partial: {record['cancelled']:,} key(s) cancelled, {record['outstanding']:,} owed a"
            " recheck"
        )
    if "nothing_compared" in lacking:
        return f"{at} compared no key"
    if record["basis"] == "partial":  # a scan stopped before the end, or cut: as the measure worded it
        return f"{at} {record['basis_text']}"
    if "cut" in lacking:
        return f"{at} was cut at {record['total']:,} keys (maxKeys)"
    said = f"{at} is a key list given at creation whose census does not count side a"
    return said + (_COUNTED_WITH if (record["basis_text"] or "").endswith(_PENDING) else "")


def _skipped(record: Mapping[str, Any] | None) -> str:
    """Why a cycle is no evidence point, in a word (Y3, Y12)."""
    if record is None or "incomplete" not in _read(record):
        return "not measured"
    return "partial" if record["incomplete"] else "a key list whose census does not count side a"


def _suppressed(record: Mapping[str, Any]) -> str:
    """N6: the suppressed count, beside every discrepant count a text prints."""
    return f"; {record['suppressed']:,} key(s) suppressed (known differences or noise only)"


def _pooled_texts(row: Row, tier: str, *, partial: bool = False) -> list[str]:
    """Y4: each part of the pooled rule the cycle does not hold at ``tier``; ``partial``: of a point that
    is no evidence and fails for certain, lacking keys or having compared none."""
    record, t = row["readiness"], percent(sampling.TARGETS[tier])
    at, n, found = _at(row), record["total"], []
    whole = {"census": "observed whole", "keys": "listed keys"}.get(record["basis"], "keys")
    none = record["incomplete"] == ["nothing_compared"]  # no key is unread: none could be compared
    named = at if not partial else f"{at}, which compared no key," if none else f"partial {at} already"
    for part, (count, allowed) in _pooled(record, tier).items():
        if count <= allowed:
            continue
        if part == "discrepant" and partial:
            found.append(
                f"{named} has {count:,} discrepant or unresolved key(s) of {n:,}, over the {allowed:,} its"
                f" pooled {t} target allows{'' if none else ' whatever its unread keys show'}"
                f"{_suppressed(record)}"
            )
        elif part == "discrepant":
            found.append(
                f"{at} has {count:,} discrepant or unresolved key(s) of {n:,} {whole}; its pooled {t} target"
                f" allows {allowed:,}: {count - allowed:,} over{_suppressed(record)}"
            )
        elif part == "absent":
            found.append(
                f"{named} has {count:,} key(s) absent on both sides, over the {allowed:,} its {t} target"
                f" allows of {n:,}: the key source is stale"
            )
        else:
            found.append(
                f"{named} has {count:,} key(s) above the API cap that were not compared (truncated): declare"
                " pagination for that side's fetch in a new version"
            )
    return found


def _item(
    gate: str,
    required: bool,
    status: str,
    text: str,
    row: Row | None = None,
    *,
    value: int | None = None,
    allowed: int | None = None,
    least: bool = False,
) -> dict[str, Any]:
    """A gate's item; ``allowed`` is the most its ``value`` may be, or (``least``) the least it must reach."""
    over = None if value is None or allowed is None or least else max(value - allowed, 0)
    return {
        "gate": gate, "group": GROUPS[gate], "required": required, "status": status, "value": value,
        "allowed": allowed, "over": over, "run_id": row and row["run_id"], "cycle": row and row["cycle"],
        "text": text,
    }  # fmt: skip


# -- the parity gates (§5) ------------------------------------------------------------------------------


def _population(row: Row) -> dict[str, Any]:
    record, at = row["readiness"], _at(row)
    basis, text, draw = record["basis"], record["basis_text"], record["draw_id"]
    if basis == "census":
        return _item("population", True, "met", f"{at} observed side a whole ({text})", row)
    if basis == "sample":
        said = f"{at} compared bottom_k sample {draw}, its frame reading side a whole"
        return _item("population", True, "met", said, row)
    if basis == "keys":
        said = (
            f"{text}: its counts describe those keys only{_COUNTED_WITH if text.endswith(_PENDING) else ''}"
        )
        return _item("population", True, "not_met", said, row)
    if basis == "rate_sample":
        said = f"{at} compared rate sample {draw}, which monitors: draw bottom_k sizes to judge a population"
        return _item("population", True, "not_met", said, row)
    if record["late"]:
        said = (
            f"{at} ended before readiness was measured (014) and its scan state was purged: its population"
            " is not known"
        )
    else:
        said = f"{at} holds counters from before the fields readiness reads: its population is not known"
    return _item("population", True, "unknown", said, row)


def _critical(row: Row, newer: Row | None) -> dict[str, Any]:
    record, at = row["readiness"], _at(row)
    count, found = record["unexplained_critical"], []
    if count:
        found.append(
            f"{at} has {count:,} key(s) with unexplained CRITICAL findings (bug rules count as unexplained);"
            " the gate allows 0"
        )
    if newer is not None:  # a newer point of its version, no evidence: an observed defect counts
        later = newer["readiness"]
        found.append(
            f"{_at(newer)}, which is not evidence ({_skipped(later)}), has"
            f" {later['unexplained_critical']:,} key(s) with"
            " unexplained CRITICAL findings (bug rules count as unexplained): an observed defect counts"
        )
    if found:
        return _item("critical", True, "not_met", "; ".join(found), row, value=count or 0, allowed=0)
    if count is None:
        said = f"{at} ended before readiness was measured (014) and its findings were purged: not measured"
        return _item("critical", True, "unknown", said, row)
    said = f"{at} has no key with unexplained CRITICAL findings"
    return _item("critical", True, "met", said, row, value=0, allowed=0)


def _sampled_texts(row: Row, tier: str) -> list[str]:
    """Y5: why a sampled cycle's stored judgement does not meet the targets gate at ``tier``."""
    record, at = row["readiness"], _at(row)
    if record["sample_status"] != "passed":
        return sampling.unpassed(row.get("sample"), record["draw_id"], at) or [
            f"the sample {record['draw_id']} of {at} did not pass"
        ]
    return [
        f"stratum {s['label']} of {at} was judged at standard ({percent(sampling.TARGETS['standard'])}); the"
        f" pair's policy is high ({percent(sampling.TARGETS['high'])})"
        for s in record["sample_strata"] or ()
        if tier == "high" and s["tier"] != "high"
    ]


def _targets(row: Row, newer: Row | None, tier: str) -> dict[str, Any]:
    record, at, t = row["readiness"], _at(row), percent(sampling.TARGETS[tier])
    sampled, status = record["keys_source"] == "hash_sample", "met"
    value = allowed = None
    if sampled and record["sample_status"] == "unjudged":
        status, found = "unknown", []
        said = f"the sample {record['draw_id']} of {at} ended without a judgement"
    elif sampled:
        found = _sampled_texts(row, tier)
        said = f"sample {record['draw_id']} of {at} passed every stratum at its tier"
    elif not (parts := _pooled(record, tier)):
        status, found = "unknown", []
        said = f"{at} holds counters from before the fields readiness reads: not measured"
    else:
        found = _pooled_texts(row, tier)
        value, allowed = parts["discrepant"]
        whole = {"census": "observed whole", "keys": "listed keys"}.get(record["basis"], "keys")
        said = (
            f"{at} has {value:,} discrepant or unresolved key(s) of {record['total']:,} {whole}, within the"
            f" {allowed:,} its pooled {t} target allows (per-stratum targets are the approver's)"
            f"{_suppressed(record)}"
        )
    if newer is not None:  # a newer point of its version, no evidence, failing for certain
        later = newer["readiness"]
        if later["keys_source"] == "hash_sample":
            found += _sampled_texts(newer, tier)
        else:
            found += _pooled_texts(newer, tier, partial=True)
    if found:
        status, said = "not_met", "; ".join(found)
    return _item("targets", True, status, said, row, value=value, allowed=allowed)


def _earliest(
    found: Streak, policy: Policy, soaks: Sequence[Mapping[str, Any]], seen: Installation, now: datetime
) -> str:
    """Y7: the cycle of a live soak of the current version that could complete the streak, and when it is
    scheduled to start (a schedule, never a forecast): a run's current cycle too, until it falls due; or
    why no live soak will add the cycles."""
    assert found.evidence is not None
    missing, outcomes = policy.gates.cycles - found.k, []
    for soak in soaks:
        run_id, first = soak["run_id"], soak["cycle"]
        last = first + missing - 1  # the run's current cycle counts as the next
        if soak["status"] == "paused":
            outcomes.append((3, now, f"run {run_id} is paused: {soak['stop_reason'] or 'manual'}"))
            continue
        every = timedelta(seconds=parse_duration(soak["execution"]["cycleInterval"]))
        conditions, started = seen.stops({"execution": soak["execution"]}), soak["started_at"]
        stopped = next(
            (
                (c, reason)
                for c in range(first, last)
                if (reason := conditions.at_boundary(c, started, max(started + every * c, now)))
            ),
            None,
        )
        completes = f"cycle {last} of run {run_id} is the earliest that can complete them"
        due = started + every * (last - 1)  # runner.cycle.due: a run's current cycle waits for it
        if stopped is not None:
            cycle, reason = stopped[0], stopped[1].rsplit(":", 1)[-1]
            said = f"run {run_id} stops after cycle {cycle} ({reason}) before it can complete them"
            outcomes.append((2, now, said))
        elif last == first and due <= now:
            outcomes.append((0, now, f"{completes}: it is running now"))
        else:
            at = max(due, now)
            said = (
                f"{completes}: it is scheduled to start about {at.astimezone(UTC):%Y-%m-%d %H:%M} UTC, cycles"
                f" starting every {soak['execution']['cycleInterval']}, and completes them when it ends"
                " complete (a schedule, not a forecast)"
            )
            outcomes.append((1, at, said))
    if not outcomes:
        return f"no live soak of {_named(found.evidence['readiness'])} will add cycles"
    return min(outcomes, key=lambda outcome: outcome[:2])[2]


def _convergence(
    found: Streak,
    policy: Policy,
    soaks: Sequence[Mapping[str, Any]] | None,
    seen: Installation,
    now: datetime,
) -> dict[str, Any]:
    """``soaks``: the live soaks of the current version; None, for a pair judged as of a past time, tells
    no schedule. Unknown only while the streak's unknown and unmeasured points, were they clean, could
    complete it: otherwise not met is certain."""
    assert found.evidence is not None
    row, needed = found.evidence, policy.gates.cycles
    version, boundary, status = _named(row["readiness"]), found.boundary, "not_met"
    reached = None
    if boundary is not None and found.kind in ("unknown", "unmeasured"):
        reached = _unmeasured(boundary, _at(boundary)) or f"{_at(boundary)}, which is not measured"
    if found.k >= needed:
        status = "met"
        said = (
            f"the newest {found.k:,} evidence cycles of {version} meet every per-cycle gate: at least the"
            f" {needed} the gate needs"
        )
    elif reached is not None and found.most >= needed:
        status = "unknown"
        said = f"the streak of {version} reaches {reached}"
    else:
        said = f"{found.k} of the {needed} evidence cycles in a row that {version} needs meet every per-cycle"
        why = []
        if found.failed is not None:
            why.append(f"{_at(found.failed)} failed {', '.join(_failed(found.failed, policy.gates.tier))}")
        if reached is not None:
            why.append(f"the streak reaches {reached}")
        said += f" gate ({'; '.join(why)})" if why else " gate"
        record = boundary.get("readiness") if reached is not None else {"final": True}
        # A point still to be measured decides, by what it comes to read, which cycle is the earliest.
        if soaks is not None and record is not None and ("error" not in record or record.get("final")):
            said += f"; {_earliest(found, policy, soaks, seen, now)}"
    return _item("convergence", True, status, said, row, value=found.k, allowed=needed, least=True)


def _fresh(row: Row, policy: Policy, now: datetime) -> dict[str, Any]:
    age, limit = (now - row["ended_at"]).total_seconds(), parse_duration(policy.gates.max_age)
    if age <= limit:
        said = (
            f"the evidence cycle ({row['cycle']} of run {row['run_id']}) ended {_ago(age)}, within the gate's"
            f" {_span(policy.gates.max_age)}"
        )
    else:
        said = f"{_at(row)} ended {_ago(age)}, over the gate's {_span(policy.gates.max_age)}"
    status = "met" if age <= limit else "not_met"
    return _item("fresh", True, status, said, row, value=int(max(age, 0)), allowed=int(limit))


def _graph(row: Row, required: bool) -> dict[str, Any]:
    record, at = row["readiness"], _at(row)
    graph = record["graph"]
    if graph is None and record["late"]:
        said = f"{at} ended before readiness was measured (014) and its graph rows were purged: not measured"
        return _item("graph", required, "unknown", said, row)
    if graph is None:
        return _item("graph", required, "not_applicable", f"{_named(record)} declares no graph", row)
    if not graph["computed"]:
        return _item("graph", required, "unknown", f"graph parity (L2) did not run in {at}", row)
    counts = [graph[code] for code in GRAPH_CODES[:4]]
    if not sum(counts):
        said = f"{at} has no CLUSTER_SPLIT, CLUSTER_MERGE, CLUSTER_REARRANGE or GROUP_MEMBERS_DIFFER row"
        return _item("graph", required, "met", said, row, value=0, allowed=0)
    said = (
        f"{at} has {counts[0]:,} CLUSTER_SPLIT, {counts[1]:,} CLUSTER_MERGE, {counts[2]:,} CLUSTER_REARRANGE"
        f" and {counts[3]:,} GROUP_MEMBERS_DIFFER row(s); the gate allows 0"
    )
    return _item("graph", required, "not_met", said, row, value=sum(counts), allowed=0)


# -- the governance and evidence gates (§5) -------------------------------------------------------------


def _spec_approval(chosen: Row, at: str) -> dict[str, Any]:
    cited = chosen["cited"]
    if cited is not None and cited["status"] == "approved":
        said = f"{at} ran under spec approval {cited['approval_id']}, approved"
        return _item("spec_approval", True, "met", said, chosen)
    if chosen["started"] != "oidc":
        return _item("spec_approval", True, "unavailable", _outside_sso(chosen), chosen)
    if cited is None:
        return _item("spec_approval", True, "not_met", f"{at} ran without a spec approval", chosen)
    said = f"{at} ran under spec approval {cited['approval_id']}, since revoked"
    return _item("spec_approval", True, "not_met", said, chosen)


def _outside_sso(chosen: Row) -> str:
    started = MODES.get(chosen["started"], "an unknown mode")
    return f"run {chosen['run_id']} started in {started}: only a run started under SSO is signed off"


def _adjudication(chosen: Row, at: str, of: str, mode: str) -> dict[str, Any]:
    """``of``: the candidate as ``_cycle`` names it."""
    results, scored = (chosen["readiness"] or {}).get("results"), chosen["adjudication"]
    if results is not None and results < EXIT_MINIMUM:
        said = f"{at} has {results:,} per-key results, under the {EXIT_MINIMUM} an adjudication needs"
        return _item("adjudication", True, "not_applicable", said, chosen)
    if scored is not None:
        named = f"adjudication {scored['adjudication_id']} of {of}"
        if scored["passed"]:
            said = f"{named}, scored by a signed-in person, passed"
            return _item("adjudication", True, "met", said, chosen)
        said = f"{named} did not pass: {'; '.join(scored['reasons'])}"
        return _item("adjudication", True, "not_met", said, chosen)
    if mode != "oidc":
        said = f"adjudication needs SSO (auth.oidc); this deployment runs in {MODES[mode]}"
        return _item("adjudication", True, "unavailable", said, chosen)
    said = (
        f"no adjudication of {at} is scored: draw and score one before requesting sign-off, which freezes"
        " the cycle's adjudications (docs/mvp/PACK.md §5)"
    )
    return _item("adjudication", True, "not_met", said, chosen)


def _sample_ledger(chosen: Row, at: str, ledger: Sequence[str] | None) -> dict[str, Any]:
    record = chosen["readiness"] or {}
    if record.get("keys_source") != "hash_sample":
        return _item("sample_ledger", True, "not_applicable", f"{at} is not sampled", chosen)
    draw, found = record["draw_id"], []
    if record["selection_ok"] is False:  # R-d, checked once as the cycle was measured
        found.append(
            f"the keys of {at} are not draw {draw}'s selection: a sample is never extended, trimmed or"
            " relabelled"
        )
    if not found and (ledger is None or record["selection_ok"] is None):
        said = f"the draw discipline of draw {draw} of {at} is read once every parity gate is met"
        return _item("sample_ledger", True, "unknown", said, chosen)
    shown = set(sampling.unpassed(chosen["sample"], draw, at))  # what the targets gate says already
    found += [why for why in ledger or () if why not in shown] or list(ledger or ())
    if found:
        return _item("sample_ledger", True, "not_met", "; ".join(found), chosen)
    said = f"draw {draw} of {at} meets the draw discipline (SAMPLING §9)"
    return _item("sample_ledger", True, "met", said, chosen)


def _accuracy(chosen: Row, at: str, policy: Policy, seen: Installation) -> dict[str, Any]:
    if policy.gates.accuracy is None:
        return _item("accuracy", False, "not_applicable", "no accuracy gate is configured", chosen)
    decided, wrong = policy.gates.accuracy
    record = chosen["readiness"] or {}
    discrepant = (record.get("different") or 0) + (record.get("outstanding") or 0)
    if discrepant < decided:  # a labelling labels discrepant items: near parity there are too few to judge
        has = f"{discrepant:,} discrepant key(s)" if discrepant else "no discrepant key"
        said = f"{at} has {has}, under the {decided:,} the accuracy gate judges"
        return _item("accuracy", True, "not_applicable", said, chosen)
    labelling = chosen["labelling"]
    status, lines = seen.accuracy(
        labelling, at=at, min_decided=decided, max_b_wrong=wrong, signoff=labelling and labelling["attested"]
    )
    return _item("accuracy", True, status, "\n".join(lines), chosen)


def _licensed(
    gate: str, seen: Installation, last: Mapping[str, Any] | None, lacking: str
) -> tuple[str, str] | None:
    """Why the licence leaves ``gate`` (signoff or pack) unavailable; when the standing cannot be read, what
    the licence last decided of it (not met or unavailable, as recorded), else unknown: met is a stored
    fact's alone, and none is stored here. None when the licence allows it."""
    if seen.allows is None:
        status = ((last or {}).get("gates") or {}).get(gate)
        if status in ("not_met", "unavailable"):
            return status, f"{LICENCE_UNREAD}: last known"
        return "unknown", LICENCE_UNREAD
    return None if seen.allows[{"pack": "packs"}.get(gate, gate)] else ("unavailable", lacking)


def _signoffs(chosen: Row, governance: Mapping[str, Any]) -> list[Row]:
    """The candidate cycle's sign-offs, newest first."""
    at = (chosen["run_id"], chosen["cycle"])
    found = [s for s in governance["signoffs"] if (s["run_id"], s["cycle"]) == at]
    return sorted(found, key=lambda s: s["approval_id"], reverse=True)


def _broke(found: Streak, tier: str) -> str:
    """How the streak's boundary ended it, as a sign-off outside the streak is told."""
    boundary = found.boundary
    assert boundary is not None and found.evidence is not None
    cycle = _cycle(boundary, found.evidence)
    if found.kind == "failed":
        return f"{cycle} fails {', '.join(_failed(boundary, tier))}"
    return f"{cycle} ran another version" if found.kind == "version" else f"{cycle} is not measured"


def _signoff(
    chosen: Row,
    found: Streak,
    governance: Mapping[str, Any],
    policy: Policy,
    seen: Installation,
    last: Mapping[str, Any] | None,
    ready: bool,
) -> dict[str, Any]:
    """The sign-off gate of the candidate; ``ready``: every other gate before it is met. Only a sign-off of
    an evidence cycle counts: one of another cycle of the current version ended since the streak began is
    told, and never met."""
    assert found.evidence is not None
    at, row = _at(chosen), found.evidence
    current, of = row["readiness"], _cycle(chosen, row)
    by = {s["status"]: s for s in reversed(_signoffs(chosen, governance))}  # the newest of each status
    if (signed := by.get("approved")) is not None:
        said = f"sign-off {signed['approval_id']} of {of} approved on {_day(signed['decided_at'])}"
        return _item("signoff", True, "met", said, chosen)
    lacking = (
        "sign-off needs an Enterprise licence, which this installation's does not allow"
        " (docs/mvp/LICENCE.md §6)"
    )
    if seen.mode != "oidc":
        said = f"sign-off needs SSO (auth.oidc); this deployment runs in {MODES[seen.mode]}"
        return _item("signoff", True, "unavailable", said, chosen)
    if chosen["started"] != "oidc":
        return _item("signoff", True, "unavailable", _outside_sso(chosen), chosen)
    if (refused := _licensed("signoff", seen, last, lacking)) is not None:
        return _item("signoff", True, *refused, chosen)
    approved = [s for s in governance["signoffs"] if s["status"] == "approved"]
    mine = [s for s in approved if s["spec_id"] == current["spec_id"]]
    outside = max((s for s in mine if s["evidence"]), key=_when, default=None)
    began = found.after
    skipped = max(
        (s for s in mine if not s["evidence"] and (began is None or s["ended_at"] > began)),
        key=_when,
        default=None,
    )
    other = max((s for s in approved if s["spec_id"] != current["spec_id"]), key=_when, default=None)
    if (asked := by.get("requested")) is not None:
        said = f"sign-off {asked['approval_id']} requested for {of} awaits a rule_approver"
    elif ready:
        said = (
            f"request sign-off of {at} on the Run screen, the newest cycle in the streak whose adjudication"
            " passed (or the newest evidence cycle): every other gate is met"
        )
    elif outside is not None:
        said = (
            f"{_cycle(outside, row)} was signed off ({outside['approval_id']}), but"
            f" {_broke(found, policy.gates.tier)}: the sign-off is outside the streak"
        )
    elif (revoked := by.get("revoked")) is not None:
        said = (
            f"sign-off {revoked['approval_id']} was revoked on {_day(revoked['revoked_at'])} (see Approvals)"
        )
    elif other is not None:
        said = (
            f"cycle {other['cycle']} of {other['version']} (spec {other['spec_id']}) was signed off"
            f" ({other['approval_id']}); {_named(current)} is current: sign off a cycle of it"
        )
    else:
        said = f"{at} is not signed off: the gates before it are not all met"
    if skipped is not None and asked is None:
        said = (
            f"{_at(skipped)} was signed off ({skipped['approval_id']}), but is not evidence"
            f" ({_skipped(skipped['readiness'])}): only a sign-off of an evidence cycle counts"
            f"{f'; {said}' if ready else ''}"
        )
    return _item("signoff", True, "not_met", said, chosen)


def _pack(
    chosen: Row, signed: Row | None, seen: Installation, last: Mapping[str, Any] | None
) -> dict[str, Any]:
    issued = next((p for p in chosen["packs"] if p["status"] == "issued"), None)
    if signed is not None and issued is not None:
        said = (
            f"pack {issued['pack_id']} issued {_day(issued['issued_at'])} for sign-off {issued['signoff_id']}"
        )
        return _item("pack", True, "met", said, chosen)
    if not seen.packs:
        return _item("pack", True, "unavailable", "evidence packs are not configured (packs)", chosen)
    lacking = "evidence packs need an Enterprise licence, which this installation's does not allow"
    if (refused := _licensed("pack", seen, last, lacking)) is not None:
        return _item("pack", True, *refused, chosen)
    if signed is None:
        return _item("pack", True, "not_met", "no sign-off to pack yet", chosen)
    if chosen["packs"]:
        revoked = chosen["packs"][0]
        said = f"pack {revoked['pack_id']} was revoked on {_day(revoked['revoked_at'])}: issue a new one"
        return _item("pack", True, "not_met", said, chosen)
    said = f"sign-off {signed['approval_id']} has no issued evidence pack: issue one on the Run screen"
    return _item("pack", True, "not_met", said, chosen)


# -- the evaluation (§5) --------------------------------------------------------------------------------


def as_of(policy: Policy, now: datetime) -> datetime:
    """When a pair is judged: ``now``, or the end of the day it was closed on, once that has passed."""
    if policy.closed is None:
        return now
    return min(now, datetime.combine(policy.closed + timedelta(days=1), time(), UTC))


def counted(n: int, most: int = COUNTED) -> str:
    """A count the store reads up to ``most``: one more reads as over it."""
    return f"over {most:,}" if n > most else f"{n:,}"


def _no_evidence(
    pair: str, series: Mapping[str, Any], unmeasured: Mapping[str, Any] | None, stored: bool
) -> str:
    """Y14: why the pair has no evidence cycle."""
    if not stored:
        return f"no spec named {pair} is stored"
    if (newest := series["newest"]) is not None:
        return (
            f"{counted(series['cycles'])} cycle(s) ended, none evidence; the newest: {_not_evidence(newest)}"
        )
    if unmeasured is not None and unmeasured["cycles"]:
        return f"{counted(unmeasured['cycles'])} ended cycle(s) are not measured yet"
    return f"no batch or soak cycle of {pair} has ended"


def _unjudged(
    policy: Policy, seen: Installation, last: Mapping[str, Any] | None, why: str
) -> list[dict[str, Any]]:
    """Every gate of a pair without an evidence cycle: nothing was observed to judge."""
    sso = seen.mode == "oidc"
    licensed = {gate: _licensed(gate, seen, last, "") for gate in ("signoff", "pack")}
    statuses = {
        **dict.fromkeys(GATES[:5], "unknown"),
        "graph": "unknown",
        "spec_approval": "unknown" if sso else "unavailable",
        "adjudication": "unknown" if sso else "unavailable",
        "sample_ledger": "not_applicable",
        "accuracy": "not_applicable",
        "signoff": "unavailable" if not sso else licensed["signoff"][0] if licensed["signoff"] else "not_met",
        "pack": "unavailable" if not seen.packs else licensed["pack"][0] if licensed["pack"] else "not_met",
    }
    required = {"graph": policy.gates.graph, "accuracy": policy.gates.accuracy is not None}
    return [_item(gate, required.get(gate, True), statuses[gate], why) for gate in GATES]


def evaluate(
    pair: str,
    policy: Policy,
    series: Mapping[str, Any],
    governance: Mapping[str, Any],
    seen: Installation,
    *,
    now: datetime,
    unmeasured: Mapping[str, Any] | None = None,
    stored: bool = True,
    ledger: Sequence[str] | None = None,
    last: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The pair as every surface shows it: each gate's item (``{gate, group, required, status, value,
    allowed, over, run_id, cycle, text}``), its state on the ladder, its score over the required gates
    (unavailable and not applicable ones left out), what blocks it and what is noted of it.

    ``series`` and ``governance`` are the store's (``repo.readiness_series``, read up to ``as_of``, and
    ``repo.readiness_governance``, read from ``streak``'s boundary); ``now`` is the database's;
    ``unmeasured``, ``repo.unmeasured_pairs``'s record of the pair; ``stored``, whether a spec of its name
    is; ``ledger``, ``sample.problems(…, later=True, selection=False)`` of a sampled candidate, read only
    once its parity gates are met (None: not read); ``last``, the detail of the pair's newest
    ``readiness.changed`` row, which a licence that cannot be read falls back on. A pair judged as of a
    past time (closed) is told no live soak's schedule, which is today's."""
    judged, tier = as_of(policy, now), policy.gates.tier
    found = streak(series, policy, unmeasured)
    row, chosen = found.evidence, candidate(found, governance)
    signed = None
    if row is None or chosen is None:
        items = _unjudged(policy, seen, last, _no_evidence(pair, series, unmeasured, stored))
    else:
        at, soaks = _at(chosen), governance["soaks"] if judged == now else None
        signed = next((s for s in _signoffs(chosen, governance) if s["status"] == "approved"), None)
        items = [
            _population(row),
            _critical(row, series["critical"]),
            _targets(row, series["targets"], tier),
            _convergence(found, policy, soaks, seen, judged),
            _fresh(row, policy, judged),
            _graph(row, policy.gates.graph),
            _spec_approval(chosen, at),
            _adjudication(chosen, at, _cycle(chosen, row), seen.mode),
            _sample_ledger(chosen, at, ledger),
            _accuracy(chosen, at, policy, seen),
        ]
        before = [item["status"] for item in items if item["required"]]
        ready = all(status in ("met", "not_applicable") for status in before)
        items.append(_signoff(chosen, found, governance, policy, seen, last, ready))
        items.append(_pack(chosen, signed, seen, last))
    by = {item["gate"]: item["status"] for item in items if item["required"]}
    parity = [status for gate, status in by.items() if GROUPS[gate] == "parity"]
    governed = [status for gate, status in by.items() if GROUPS[gate] == "governance"]
    if row is None:
        state = "no_evidence"
    elif "not_met" in parity:
        state = "not_ready"
    elif "unknown" in parity:
        state = "unknown"
    elif by["signoff"] == "met":
        state = "evidenced" if by["pack"] == "met" else "signed_off"
    elif by["signoff"] == "not_met" and all(status in ("met", "not_applicable") for status in governed):
        state = "signoff_ready"
    else:
        state = "parity_ready"
    counted = [status for status in by.values() if status not in ("unavailable", "not_applicable")]
    record = row and row["readiness"]
    current = record and record["spec_id"]
    held = [
        s
        for s in governance["signoffs"]
        if s["status"] == "approved" and s["spec_id"] == current and s["evidence"]
    ]
    regressed = bool(held) and STATES.index(state) < STATES.index("signed_off")
    notes = []
    if judged < now:
        notes.append(f"closed on {policy.closed}: judged as of that day")
    if regressed:
        newest = max(held, key=_when)
        notes.append(
            f"regressed: {_named(record)} was signed off ({newest['approval_id']}, {_cycle(newest, row)})"
            " and the pair is now below signed off"
        )
    issued = chosen and signed and next((p for p in chosen["packs"] if p["status"] == "issued"), None)
    evidence = row and {name: row[name] for name in ("run_id", "cycle", "ended_at")}
    return {
        "pair": pair, "programme": policy.programme, "entity": policy.entity,
        "version": record and record["version"], "spec_id": current, "state": state, "regressed": regressed,
        "score": {"passed": counted.count("met"), "required": len(counted)}, "gates": items,
        "blocking": [gate for gate, status in by.items() if status in ("not_met", "unknown")],
        "evidence": evidence and evidence | {"basis": record["basis"], "claim": "observed"},
        "candidate": chosen and {"run_id": chosen["run_id"], "cycle": chosen["cycle"]},
        "convergence": {"streak": found.k, "needed": policy.gates.cycles, "window": window(policy)},
        "trend": spark(series["trend"], tier), "signoff_id": signed and signed["approval_id"],
        "pack_id": issued and issued["pack_id"], "closed": policy.closed, "open": judged == now,
        "deviations": list(policy.deviations), "notes": notes, "config_sha256": policy.sha256,
    }  # fmt: skip


def causes(was: Mapping[str, Any], item: Mapping[str, Any], *, version: bool, config: bool) -> list[str]:
    """Why a pair's ``readiness.changed`` row is written (§8), in ``CAUSES``' order: told from what differs
    between its newest row's detail (``was``) and the pair as evaluated (``item``), never from what entered
    the audit chain meanwhile, so that neither a restart nor a late commit names another pair's cause.

    ``cycle``: the evidence cycle is another, or a parity gate changed (``fresh`` on the same evidence
    cycle is ``time``); under settings just recorded (``config``) a parity gate that changed on the same
    evidence cycle is the settings' alone. A governance or evidence gate names the fact it reads, but
    ``signoff`` or ``pack`` moving between unavailable and not met for the same candidate run: what the
    licence (or the deployment's ``packs``) allows changed, which is ``licence``. ``version``: the current
    version changed; ``closed``: the pair is judged as of its day. A row that names none of these changed
    ``regressed`` alone, which an approval does (``signoff``), or is ``time``'s."""
    statuses = {gate["gate"]: gate["status"] for gate in item["gates"] if gate["required"]}

    def at(cycle: Mapping[str, Any] | None) -> tuple[Any, Any] | None:
        return cycle and (cycle["run_id"], cycle["cycle"])

    moved = at(was["evidence"]) != at(item["evidence"])
    same_run = (was["candidate"] or {}).get("run_id") == (item["candidate"] or {}).get("run_id")
    found = {"cycle": moved, "version": version, "config": config, "closed": not item["open"]}
    for gate in GATES:
        before, now = was["gates"].get(gate), statuses.get(gate)
        if before == now:
            continue
        if GROUPS[gate] == "parity":
            if not moved and not config:  # else the new evidence cycle, or the settings, moved it
                found["time" if gate == "fresh" else "cycle"] = True
        elif gate in ("signoff", "pack") and same_run and {before, now} == {"unavailable", "not_met"}:
            found["licence"] = True
        else:
            found[_GATE_CAUSES[gate]] = True
    named = [cause for cause in CAUSES if found.get(cause)]
    return named or ["signoff" if was["regressed"] != item["regressed"] else "time"]


def unevaluated(pair: str, policy: Policy, error: str, now: datetime) -> dict[str, Any]:
    """A pair whose evaluation raised ``error`` (measures this build cannot read): nothing can be said of
    it, so every gate reads unknown with why, and its state is unknown."""
    why = f"{pair} could not be evaluated ({error}): see the server log"
    required = {"graph": policy.gates.graph, "accuracy": policy.gates.accuracy is not None}
    items = [_item(gate, required.get(gate, True), "unknown", why) for gate in GATES]
    blocking = [item["gate"] for item in items if item["required"]]
    return {
        "pair": pair, "programme": policy.programme, "entity": policy.entity, "version": None,
        "spec_id": None, "state": "unknown", "regressed": False,
        "score": {"passed": 0, "required": len(blocking)}, "gates": items, "blocking": blocking,
        "evidence": None, "candidate": None,
        "convergence": {"streak": 0, "needed": policy.gates.cycles, "window": window(policy)},
        "trend": [], "signoff_id": None, "pack_id": None, "closed": policy.closed,
        "open": as_of(policy, now) == now, "deviations": list(policy.deviations), "notes": [why],
        "config_sha256": policy.sha256,
    }  # fmt: skip


def programmes(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each programme of evaluated pairs (None: the pairs in none) as its worst open pair, with how many of
    its open pairs are parity-ready or better and, per entity (None last), how many are in each state."""
    found = []
    for name in sorted({item["programme"] for item in items}, key=lambda n: (n is None, n)):
        pairs = [item for item in items if item["programme"] == name and item["open"]]
        entities: dict[str | None, Counter[str]] = {}
        for item in pairs:
            entities.setdefault(item["entity"], Counter())[item["state"]] += 1
        ready = sum(STATES.index(item["state"]) >= STATES.index("parity_ready") for item in pairs)
        worst = min((item["state"] for item in pairs), key=STATES.index, default=None)
        rows = [
            {"entity": entity, "states": {state: n for state in STATES if (n := entities[entity][state])}}
            for entity in sorted(entities, key=lambda e: (e is None, e))
        ]
        found.append(
            {"programme": name, "pairs": len(pairs), "ready": ready, "worst": worst, "entities": rows}
        )
    return found


# -- a pair's brief (§9, §10) ---------------------------------------------------------------------------


def marked(points: Sequence[Row], tier: str) -> list[dict[str, Any]]:
    """A pair's points (``repo.brief_points``'s), oldest first, as its brief lists them: ``trend``'s rows,
    each with what its stored counters say (``mismatch_rate`` and ``coverage`` as stored, None for a sampled
    cycle, which has no rate, and where the store says ``unavailable``; ``by_outcome``), its ``cancelled``
    and ``outstanding`` keys, its ``root_causes`` (how many groups; None once its purge started) and its
    ``marks``: ``version``, ``trend``'s boundary, and ``keys``, against the nearest earlier point holding
    measures: another key source, or a key list given at creation, or read from a key table, of another
    size. A cycle without counters of its own holds none of them, and an error record, like one of another
    format, ids only: neither takes nor gives the ``keys`` mark."""
    rows = sorted(points, key=_when)
    found, before = [], None
    for row, item in zip(rows, trend(rows, tier), strict=True):
        record = _read(row["readiness"])
        read = record.get("format") == FORMAT and "error" not in record
        counters = row["counters"] if read else {}
        sampled = record.get("keys_source") == "hash_sample"
        rates = {
            name: None if sampled or counters.get(name) == "unavailable" else counters.get(name)
            for name in ("mismatch_rate", "coverage")
        }
        marks = ["version"] if item["boundary"] else []
        if read and record["total"] is not None:
            source = record["keys_source"]
            keys = source, record["total"] if source in ("explicit", "table") else None
            marks += ["keys"] if before not in (None, keys) else []
            before = keys
        found.append(
            item
            | rates
            | {"by_outcome": counters.get("by_outcome")}
            | {name: record.get(name) for name in ("cancelled", "outstanding")}
            | {"root_causes": row["root_causes"] if read else None, "marks": marks}
        )
    return found


def ratio(stored: str | None, part: int | None, whole: int | None) -> str | None:
    """A stored ratio of ``part`` in ``whole`` as a brief prints it. The counters round to six places
    (``runner.counters``): one that reads 0 of a part above none is ``under 0.000001``, and one that reads
    1 of a part short of its whole ``over 0.999999``; any other is printed as stored."""
    if stored == "0" and part:
        return "under 0.000001"
    if stored == "1" and part is not None and whole is not None and part < whole:
        return "over 0.999999"
    return stored


def reported(
    point: Mapping[str, Any], row: Row, evidence: Mapping[str, Any] | None, strata: Sequence[str] | None
) -> dict[str, Any]:
    """A brief's reported cycle, its newest complete point whatever its basis (``row``: as the store gave
    it): the point with its draw, its claim, its ``strata`` (``runner.sample.lines`` of its stored
    judgement; None when it is not sampled, or ended without one) and its notes: that the gates judge
    another cycle (M8; ``evidence``: the pair's), how a sampled one is judged (M4), that its counts are of
    keys only (M5), and its stored mismatch rate (``ratio``) with the counts it is of (M9)."""
    record, at, notes = row["readiness"], _at(row), []
    judged = evidence and _at(evidence)
    if judged not in (None, at):
        notes.append(f"The gates judge {judged}; these figures are of {at}, the newest complete cycle.")
    if record["sample_status"] == "unjudged":
        notes.append(f"the sample {record['draw_id']} of {at} ended without a judgement")
    elif record["keys_source"] == "hash_sample":
        notes.append(
            f"Sampled (draw {record['draw_id']}): judged per stratum, with no rate or bound for the strata"
            " pooled."
        )
    if record["basis"] in ("keys", "rate_sample", "unknown"):
        notes.append("These counts are of the keys compared, not of a population.")
    if point["mismatch_rate"] is not None:
        rate = ratio(point["mismatch_rate"], point["different"], point["compared"])
        notes.append(
            f"Mismatch rate {rate}: {point['different']:,} different of"
            f" {point['compared']:,} compared. Known differences and noise-only keys count as matching; a"
            " known difference under a bug rule counts as different."
        )
    return dict(point) | {"draw_id": record["draw_id"], "claim": "observed", "strata": strata, "notes": notes}


def unreported(pair: str, rows: Sequence[Row]) -> str:
    """M2: why a brief reports no cycle of its window (``rows``, newest first): none is measured, or none
    is complete, with why the newest is not (Y14's reasons)."""
    if not rows:
        return f"No batch or soak cycle of {pair} is measured yet."
    return (
        f"None of the newest {len(rows):,} measured cycle(s) of {pair} is complete; the newest:"
        f" {_not_evidence(rows[0])}"
    )


def purged(row: Row) -> str:
    """M3: a reported cycle whose purge started keeps no root-cause group for its brief to list."""
    return f"Cycle {row['cycle']} of run {row['run_id']} is purged: its root-cause groups are not kept."


def burn_down(
    points: Sequence[Mapping[str, Any]], held: Sequence[Mapping[str, Any]], found: Mapping[str, Any]
) -> dict[str, Any]:
    """A brief's root-cause groups over ``held``, the complete points of its window (``points``) whose
    groups are kept, the last of them its reported cycle, as ``repo.brief_causes`` read them (``found``):
    how many that cycle ``opened`` (in it, in no earlier one), how many are ``still_open`` (in both) and
    ``closed`` (in earlier ones only: not observed in it), over how many ``cycles`` ``from`` when (M6, the
    ``text``; with a closed group it says how many keys that cycle did not compare, which no group holds),
    and the groups listed, each with its ``state``, the ``first`` and ``last`` cycle holding it, how many
    ``seen`` it, what its last row says and ``since``: the marks of the window's points after its last
    one, up to the reported one. An incomplete point is not among ``held``: it neither opens nor closes."""
    newest, cycles = held[-1], len(held)
    said = ("max_severity", "taxonomy_code", "change_type", "affected_keys")
    items = []
    for group in found["items"]:
        first, last = held[group["first"] - 1], held[group["last"] - 1]
        state = "closed" if last is not newest else "opened" if first is newest else "still_open"
        later = [point for point in points if _when(last) < _when(point) <= _when(newest)]
        items.append(
            {"root_cause_id": group["root_cause_id"], "state": state}
            | {
                name: {key: cycle[key] for key in ("run_id", "cycle", "ended_at")}
                for name, cycle in (("first", first), ("last", last))
            }
            | {"seen": group["seen"]}
            | {name: group[name] for name in said}
            | {"since": [m for m in ("version", "keys") if any(m in point["marks"] for point in later)]}
        )
    opened, still, total = found["opened"], found["still_open"], found["total"]
    closed, since = total - opened - still, held[0]["ended_at"]
    text = (
        f"Root-cause groups over {cycles:,} complete cycle(s) since {_day(since)}: {opened:,} opened in the"
        f" newest, {still:,} still open, {closed:,} closed (not observed in it)"
    )
    if closed and (unread := newest["indeterminate"]):  # a root cause groups keys that were compared
        text += (
            f"; {unread:,} key(s) of the newest were not compared, so a closed group's keys may be among them"
        )
    if total > len(items):
        text += f"; the first {len(items):,} of {total:,} are listed"
    return {
        "opened": opened, "still_open": still, "closed": closed, "total": total, "cycles": cycles,
        "from": since, "text": text, "items": items,
    }  # fmt: skip


def reads(group: Mapping[str, Any]) -> str:
    """A listed group's state as a brief words it: a closed one with what changed since it was last seen
    (``closed, version and keys changed since``), which may be why it is no longer observed."""
    changed = " and ".join(group["since"])
    return worded(group["state"]) + (f", {changed} changed since" if changed else "")
