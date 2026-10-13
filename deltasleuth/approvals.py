"""Four-eyes approvals and sign-off (docs/mvp/APPROVALS.md).

Under SSO a spec version whose content exposes values, suppresses findings or filters what is compared
(``needs``) runs only under an approval of its content hash by a person who did not write it
(``gate``). An ended cycle is parity evidence only once someone who neither wrote its spec, started its
run nor asked for the sign-off accepted a hash of its facts (``signoff_problems``, ``attests``). The
trigger ds.tg_approvals checks every decision, and ``revoke`` withdraws an approval and what rests on
it in one transaction. Tokens and developer modes have no persons: their runs go ahead, labelled
unapproved (``run_state``), and are never signed off, whatever approval they resume or attach under
later. ``unstorable`` and ``unstored`` refuse an upload whose stored body would not re-hash.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from deltasleuth.auth.principal import Principal
from deltasleuth.connectors.base import utc_iso
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.core.spec.errors import pointer
from deltasleuth.core.types import CompiledSpec, RuleClassification
from deltasleuth.store import repo
from deltasleuth.store.db import APPROVALS

SCOPES = ("clear", "unlisted_clear", "known_diffs", "population_filters", "sampling")  # raw_clear is reserved
APPROVAL_PAUSE = "run_paused_approval"  # a revoke paused the run: resuming it needs a new approval
APPROVAL_REVOKED = "approval_revoked"  # a revoke stopped the queued one-shot run
SIGNED_OFF = "signed off: "  # the hold a sign-off places on its run
MODES = {"tokens": "tokens mode", "none": "developer mode"}  # the auth modes without approvers
_ROOT_CAUSE_FIELDS = (
    "root_cause_id", "taxonomy_code", "max_severity", "change_type", "path_pattern", "affected_keys",
)  # fmt: skip
_SCAN_FIELDS = ("done", "rows_a", "rows_b", "skipped_a", "skipped_b", "matched", "candidates")
# A known-diff rule as attests format 1 names it: a field the code adds later needs format 2, so it never
# stales a signed cycle (format 0 took every field, asdict).
_RULE_FIELDS = ("rule_id", "when", "classification", "reason", "ticket", "expires")


class ApprovalRequired(Exception):
    """A run of a spec version that needs a four-eyes approval it lacks (§6.2 messages 1-4): the API
    answers 409, the CLI exits 2."""


class ApprovalConflict(Exception):
    """The approval's state refuses the request, decision or revoke: the API answers 409."""


def needs(spec: CompiledSpec) -> list[str]:
    """The scopes of the spec's content that gate its runs under SSO (§1.1), in ``SCOPES`` order."""
    compare, tables = spec.compare, [side.table for side in spec.sides.values() if side.table is not None]
    # csv.skip leaves each file's first lines out of what is compared (docs/mvp/FILES.md §5), as a filter does
    skips = [((table.file or {}).get("csv") or {}).get("skip", 0) > 0 for table in tables]
    filters = (
        any(table.where is not None or table.columns is not None for table in tables)
        or any(skips)
        or bool(compare.include or compare.exclude)
        or compare.value_filter is not None
    )
    found = {
        "clear": bool(spec.security.clear),
        "unlisted_clear": spec.security.unlisted == "clear",
        "known_diffs": bool(spec.known_diffs),
        "population_filters": filters,
        "sampling": spec.sample is not None,  # frame, strata, sizes, label and retries: pre-registered
    }
    return [scope for scope in SCOPES if found[scope]]


async def gate(
    ctx: Context, conn: repo.Conn, spec_id: int, spec: CompiledSpec, *, lock: bool = False
) -> tuple[str | None, dict[str, Any]]:
    """The spec approval a run of ``spec`` (stored as ``spec_id``) starts, resumes or attaches under,
    and its ``params["approval"]``: ``{needs, mode}``, ``mode`` being the auth mode (a run's record is
    its creation's: a resume or attach keeps it).

    In oidc mode a stored body that does not re-hash to its content hash is refused; then the active
    approval covering ``needs`` is returned, read FOR SHARE when ``lock`` (a revoke waits for the
    caller's transaction, §2); without one, None when nothing is needed, else ApprovalRequired. The
    other modes have no approvers: None, and the run is labelled unapproved.
    """
    needed, mode = needs(spec), ctx.settings.auth_mode
    params = {"needs": needed, "mode": mode}
    if mode != "oidc":
        return None, params
    stored = await repo.get_spec(conn, spec_id)
    if stored is None:
        raise LookupError(f"spec {spec_id} not found")
    if content_hash(stored["body"]) != stored["content_hash"]:
        raise ApprovalRequired(
            f"spec {spec_id}'s stored body does not re-hash to its content hash (edited, or not storable"
            " as uploaded): upload a changed version (a new version)"
        )
    active = await repo.active_spec_approval(conn, spec_id, lock=lock)
    if active is not None:
        missing = [scope for scope in needed if scope not in active["scopes"]]
        if not missing:
            return active["approval_id"], params
        raise ApprovalRequired(
            f"{_named(spec_id, spec)} needs a new four-eyes approval: {active['approval_id']} covers"
            f" {', '.join(active['scopes']) or 'nothing'}; this build also needs {', '.join(missing)}"
        )
    if not needed:
        return None, params
    history = [
        a["approval_id"] for a in await repo.approval_history(conn, spec_id) if a["status"] == "revoked"
    ]
    if history:
        raise revoked(spec_id, spec, history[0])
    raise ApprovalRequired(
        f"{_named(spec_id, spec)} needs a four-eyes approval of {', '.join(needed)} before it runs"
    )


def _named(spec_id: int, spec: CompiledSpec) -> str:
    return f"spec {spec_id} ({spec.name} {spec.version})"


def revoked(spec_id: int, spec: CompiledSpec, approval_id: str) -> ApprovalRequired:
    """Message 2: the spec approval ``approval_id``, which a run of the spec needs, was revoked."""
    return ApprovalRequired(
        f"{_named(spec_id, spec)} needs a new four-eyes approval: {approval_id} was revoked"
    )


def unstorable(value: Any, at: tuple[str | int, ...] = ()) -> str | None:
    """Why the store cannot keep ``value`` as uploaded, so that its stored body would not re-hash to its
    content hash (§6.2 Upload); None when it can. Checked before a spec version is stored."""
    if isinstance(value, str) and "\x00" in value:
        return f"spec body holds NUL at {pointer(at)}, which the store cannot keep"
    if isinstance(value, float) and value == 0 and math.copysign(1, value) < 0:
        return f"spec body holds -0.0 at {pointer(at)}, which the store keeps as 0: write 0"
    members = (
        value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    )
    for name, item in members:
        if problem := unstorable(name, at) or unstorable(item, (*at, name)):
            return problem
    return None


async def unstored(conn: repo.Conn, spec_id: int, spec: CompiledSpec) -> str | None:
    """Why the body stored as ``spec_id`` does not re-hash to ``spec``'s content hash (§6.2 Upload), naming
    where it differs from the upload; None when it does. Checked once stored, in the storing transaction:
    jsonb keeps a float of 1e16 or more as an integer, and the same content maps to an edited row."""
    stored = await repo.get_spec(conn, spec_id)
    assert stored is not None
    if content_hash(stored["body"]) == spec.content_hash:
        return None
    at, uploaded, kept = _difference(spec.raw, stored["body"])
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in (uploaded, kept)):
        written, kept = _json(uploaded), _json(kept)
        return f"spec body holds {written} at {pointer(at)}, which the store keeps as {kept}: write {kept}"
    return (
        f"spec {spec_id}'s stored body differs from this upload at {pointer(at)} (edited, or not storable as"
        " uploaded): upload a changed version (a new version)"
    )


def _json(value: Any) -> str:
    """``value`` as the content hash writes it."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _difference(a: Any, b: Any, at: tuple[str | int, ...] = ()) -> tuple[tuple[str | int, ...], Any, Any]:
    """Where ``b`` first differs from ``a`` as JSON: the path, and both values there."""
    if isinstance(a, dict) and isinstance(b, dict):
        for name in sorted(a.keys() | b.keys()):
            if name not in a or name not in b or _json(a[name]) != _json(b[name]):
                return _difference(a.get(name), b.get(name), (*at, name))
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if _json(x) != _json(y):
                return _difference(x, y, (*at, i))
    return at, a, b


def run_state(record: Mapping[str, Any] | None, approval: Mapping[str, Any] | None) -> str:
    """``approved`` or ``revoked`` (``approval`` being the row of the approval the run or cycle ran
    under), else ``unapproved`` when its version needs an approval (``record``, the run's
    ``params["approval"]``: its ``mode`` says why; a run from before 008 has none and is unapproved) or
    ``not_needed``."""
    if approval is not None:
        return "revoked" if approval["status"] == "revoked" else "approved"
    return "not_needed" if record is not None and not record["needs"] else "unapproved"


def deciders(approval: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Who took the approval's steps and when, in order: ``{actor, name, at}``."""
    return [
        {"actor": approval[f"{step}_by"], "name": approval[f"{step}_by_name"], "at": approval[f"{step}_at"]}
        for step in ("first", "decided")
        if approval[f"{step}_by"] is not None
    ]


async def run_approval(conn: repo.Conn, run: Mapping[str, Any], cycle: int | None = None) -> dict[str, Any]:
    """The run's spec approval as ``GET /v1/runs/{id}`` shows it (§4) or, given ``cycle``, the one that
    cycle ran under, as report.pdf and the P0 summary show it: an ended cycle's ``ds.cycles.approval_id``,
    a live one's the run's (none before ``params["approval"]["from_cycle"]``). Its state
    (``run_state``), the scopes its version needs and the auth mode it started in, who approved it and
    when, and who revoked it, when and why. A run from before 008 is unapproved, with neither scopes nor
    mode, whatever it attached: it is never signed off."""
    record = run["params"].get("approval")
    approval_id = run["approval_id"]
    if cycle is not None:
        ended = await repo.get_cycle(conn, run["run_id"], cycle)
        if ended is not None:
            approval_id = ended["approval_id"]
        elif cycle < (record or {}).get("from_cycle", 0):
            approval_id = None
    before = run["created_at"] < await repo.applied_at(conn, APPROVALS)  # type: ignore[operator]
    if before:
        record = None
    approval = None if before or approval_id is None else await repo.get_approval(conn, approval_id)
    return {
        "state": run_state(record, approval),
        "approval_id": approval_id,
        "needs": record and record["needs"],
        "mode": record and record["mode"],
        "deciders": deciders(approval) if approval else [],
        **{f"revoked_{name}": approval and approval[f"revoked_{name}"] for name in ("by", "by_name", "at")},
        "reason": approval and approval["revoke_reason"],
    }


NOT_EVIDENCE = "Not sign-off evidence."
_NO_APPROVERS = {"tokens": "tokens mode has no approvers", "none": "developer mode has no approvers"}
_STARTED = {"tokens": "tokens mode", "none": "developer mode"}  # a run started outside SSO


def _day(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d}"


def _decided(
    what: str, approval_id: str, by: Sequence[Mapping[str, Any]], revoked: tuple[datetime | None, Any, Any]
) -> str:
    """Who granted the approval and when, or who revoked it (``revoked``: when, by whom, why)."""
    at, name, reason = revoked
    if at is not None:
        return f"{what} {approval_id} was revoked on {_day(at)} by {name} ({reason}). {NOT_EVIDENCE}"
    granted = " and ".join(f"{d['name']} on {_day(d['at'])}" for d in by)
    return f"{what} {approval_id} was granted by {granted}."


def label(approval: Mapping[str, Any]) -> str:
    """What the Run screen (web/src/four-eyes.ts), report.pdf, the P0 summary and ``ds runs`` say of a
    run's spec approval (§4), from ``run_approval``."""
    state, needed = approval["state"], approval["needs"]
    if state in ("approved", "revoked"):
        revoked = (approval["revoked_at"], approval["revoked_by_name"], approval["reason"])
        text = _decided("Spec approval", approval["approval_id"], approval["deciders"], revoked)
        if state == "approved" and (started := _STARTED.get(approval.get("mode"))):  # never signed off
            text += f" {NOT_EVIDENCE[:-1]}: the run started in {started}."
        return text
    if state == "not_needed":
        return "The spec version needs no four-eyes approval."
    if needed is None:
        return f"Unapproved spec version: the run started before approvals (008). {NOT_EVIDENCE}"
    why = _NO_APPROVERS.get(approval["mode"], "the run has no approval")
    verb = "was" if len(needed) == 1 else "were"
    return (
        f"Unapproved spec version: {', '.join(needed)} {verb} not approved by a second person ({why})."
        f" {NOT_EVIDENCE}"
    )


def signoff_label(signoffs: Sequence[Mapping[str, Any]]) -> str | None:
    """What report.pdf and the P0 summary say of a cycle's sign-off, from its requests: the approved one,
    else the newest revoked one; None when neither exists."""
    found = next((s for s in signoffs if s["status"] == "approved"), None) or next(
        (s for s in signoffs if s["status"] == "revoked"), None
    )
    if found is None:
        return None
    revoked = (found["revoked_at"], found["revoked_by_name"], found["revoke_reason"])
    return _decided("Sign-off", found["approval_id"], deciders(found), revoked)


def bug_rules(spec: CompiledSpec) -> list[str]:
    """The ids of the spec's known-diff rules classified as bugs: what they explain is still discrepant."""
    return [rule.rule_id for rule in spec.known_diffs if rule.classification is RuleClassification.BUG]


async def _cited(
    conn: repo.Conn, run: Mapping[str, Any], ended: Mapping[str, Any], spec: CompiledSpec
) -> str | None:
    """The spec approval a sign-off of the ended cycle cites: the one the cycle ran under, else, when
    its version needs none, the version's active approval."""
    if ended["approval_id"] is not None or needs(spec):
        return ended["approval_id"]
    active = await repo.active_spec_approval(conn, run["spec_id"])
    return None if active is None else active["approval_id"]


async def signoff_problems(
    conn: repo.Conn,
    run: Mapping[str, Any],
    cycle: int,
    spec: CompiledSpec,
    *,
    cited: str | None = None,
    lock: bool = False,
    later: bool = True,
) -> list[str]:
    """Why the run's ``cycle`` cannot be signed off (§1.2), judging the ended cycle, never the run's
    status now; empty when it can. ``spec`` is the run's version, compiled. At a decision ``cited`` is
    the spec approval the request attests, read FOR SHARE when ``lock`` (§2). A sampled cycle's draw
    discipline follows (docs/mvp/SAMPLING.md §10: G1-G11; without ``later``, not R-c's G10, which a pack
    reads from the final grant). CyclePurged (410) once the cycle's purge has started."""
    from deltasleuth.runner import sample  # the runner's modules import this one
    from deltasleuth.runner.purge import refuse_purged

    run_id = run["run_id"]
    await refuse_purged(conn, run_id, cycle)
    problems = []
    if run["mode"] == "one_shot":
        problems.append("a one-shot run is not signed off")
    elif run["params"].get("samples_dir") is not None:
        problems.append(f"run {run_id} read captured samples: only a live run is signed off")
    if run["created_at"] < await repo.applied_at(conn, APPROVALS):  # type: ignore[operator]
        # never signed off, so its cycles, whose counters may predate their current shape, are not judged
        return [*problems, f"run {run_id} started before approvals (008): only later runs are signed off"]
    started = (run["params"].get("approval") or {}).get("mode")  # its creation's, which a resume keeps
    if started != "oidc":
        problems.append(
            f"run {run_id} started in {MODES.get(started, 'an unknown mode')}: only a run started under SSO"
            " is signed off"
        )
    at = f"cycle {cycle} of run {run_id}"
    ended = await repo.get_cycle(conn, run_id, cycle)
    if ended is None:
        return [*problems, f"{at} has not ended"]
    counters = ended["counters"]
    if counters.get("cycle") != cycle:
        problems.append(f"{at} has no counters of its own (failed, erased or abandoned)")
    else:
        if counters["cancelled"] or counters["outstanding"]:
            problems.append(
                f"{at} is partial: {counters['cancelled']} key(s) cancelled, {counters['outstanding']} owed"
                " a recheck"
            )
        broken = [check["identity"] for check in counters["reconciliation"] if not check["holds"]]
        if broken:
            problems.append(f"{at} does not reconcile: {'; '.join(broken)}")
        if not counters["compared"]:
            problems.append(f"{at} compared no key")
    scan = await repo.get_scan(conn, run_id, cycle)
    if scan is not None and not scan["done"]:  # until, maxDuration or a cancel stopped it; maxKeys is a cut
        problems.append(
            f"{at} scanned only part of its tables (rows_a {scan['rows_a']}, rows_b {scan['rows_b']} read;"
            " the scan stopped before the end)"
        )
    if ended["keys_cut"] is not None:
        problems.append(f"{at} was cut at {ended['keys_cut']['taken']} keys (maxKeys)")
    critical = await repo.unexplained_criticals(conn, run_id, cycle, bug_rules(spec))
    if critical:
        problems.append(
            f"{at} has {critical} key(s) with unexplained CRITICAL findings (bug rules count as unexplained)"
        )
    cited = cited or await _cited(conn, run, ended, spec)
    approval = None if cited is None else await repo.get_approval(conn, cited, lock=lock)
    if approval is None:
        problems.append(f"{at} ran without a spec approval")
    elif approval["status"] != "approved":
        problems.append(f"{at} ran under spec approval {cited}, since revoked")
    return problems + await sample.problems(conn, run, cycle, spec, later=later)


async def attests(conn: repo.Conn, run: Mapping[str, Any], cycle: int, spec: CompiledSpec) -> dict[str, Any]:
    """What a sign-off request of the ended cycle accepts (§1.2; docs/mvp/PACK.md §1.2, §5), in attests
    format 3: approving it decides each listed rule and root cause. It fixes the adjudication it attests,
    the cycle's newest scored by a person, or none, a sampled cycle's draw, ledger and judgement, and the
    labelling it attests, the cycle's newest labelled by a person, or none (docs/mvp/ACCURACY.md §8)."""
    adjudication = await repo.attested_adjudication(conn, run["run_id"], cycle)
    labelling = await repo.attested_labelling(conn, run["run_id"], cycle)
    return await _format_3(conn, run, cycle, spec, adjudication, labelling)


async def rebuilt(
    conn: repo.Conn, run: Mapping[str, Any], cycle: int, spec: CompiledSpec, recorded: Mapping[str, Any]
) -> dict[str, Any] | None:
    """A sign-off's ``recorded`` attests as the builder of their format makes them now, from the
    adjudication (formats 1 to 3) and the labelling (format 3) they attest, which a decision and a pack
    compare by ``attests_sha256``; None for a format this build does not know."""
    match recorded.get("format", 0):
        case 0:  # 008's, so that a sign-off requested before packs is decided as before
            return await _format_0(conn, run, cycle, spec)
        case 1 | 2 | 3 as number:  # 2: a sampled cycle's draw too (docs/mvp/SAMPLING.md §10)
            fixed = recorded.get("adjudication")
            adjudication = fixed and await repo.cycle_adjudication(
                conn, run["run_id"], cycle, fixed["adjudication_id"]
            )
            if number == 3:  # the labelling too (docs/mvp/ACCURACY.md §8)
                named = recorded.get("accuracy")
                labelling = named and await repo.cycle_labelling(
                    conn, run["run_id"], cycle, named["labelling_id"]
                )
                return await _format_3(conn, run, cycle, spec, adjudication, labelling)
            build = _format_1 if number == 1 else _format_2
            return await build(conn, run, cycle, spec, adjudication)
    return None


async def _format_0(
    conn: repo.Conn, run: Mapping[str, Any], cycle: int, spec: CompiledSpec
) -> dict[str, Any]:
    facts, _, _ = await _facts(conn, run, cycle, spec)
    keys = facts["counters"].get("suppressed_by_rule", {})
    return facts | {
        "rules": [asdict(rule) | {"keys": keys.get(rule.rule_id, 0)} for rule in spec.known_diffs],
        "claim": "observed",
    }


async def _format_1(
    conn: repo.Conn, run: Mapping[str, Any], cycle: int, spec: CompiledSpec, adjudication: repo.Row | None
) -> dict[str, Any]:
    """Format 0's facts, its rules named field by field, plus ``format``, the cycle's ``census`` and the
    ``adjudication`` it attests (``{adjudication_id, sheet_sha256, score_sha256, adjudicated, agreed,
    passed}``) or None."""
    from deltasleuth.runner import census  # the runner's modules import this one

    facts, ended, scan = await _facts(conn, run, cycle, spec)
    keys = facts["counters"].get("suppressed_by_rule", {})
    attested = adjudication and {
        "adjudication_id": adjudication["adjudication_id"],
        "sheet_sha256": adjudication["sheet_sha256"],
        "score_sha256": adjudication["score_sha256"],
        "adjudicated": adjudication["score"]["adjudicated"],
        "agreed": adjudication["score"]["agreed"],
        "passed": adjudication["passed"],
    }
    return facts | {
        "format": 1,
        "rules": [
            {name: getattr(rule, name) for name in _RULE_FIELDS} | {"keys": keys.get(rule.rule_id, 0)}
            for rule in spec.known_diffs
        ],
        "census": census.of_cycle(run, ended, scan, spec),  # type: ignore[arg-type]
        "adjudication": attested,
        "claim": "observed",
    }


async def _format_2(
    conn: repo.Conn, run: Mapping[str, Any], cycle: int, spec: CompiledSpec, adjudication: repo.Row | None
) -> dict[str, Any]:
    """Format 1 plus ``sample``: a sampled cycle's draw, the ledger draws before it, as R-a judged them,
    and its stored judgement (docs/mvp/SAMPLING.md §10); None for any other cycle."""
    from deltasleuth.runner import sample  # the runner's modules import this one

    facts = await _format_1(conn, run, cycle, spec, adjudication)
    return facts | {"format": 2, "sample": await sample.attested(conn, run, cycle, spec)}


async def _format_3(
    conn: repo.Conn,
    run: Mapping[str, Any],
    cycle: int,
    spec: CompiledSpec,
    adjudication: repo.Row | None,
    labelling: repo.Row | None,
) -> dict[str, Any]:
    """Format 2 plus ``labellings``, how many labellings the cycle has, drawn and labelled, and
    ``accuracy``: the labelling the sign-off attests (docs/mvp/ACCURACY.md §8), its hashes, actors and exact
    counts (its uniform discrepant items'; ``score_sha256`` binds the intervals) and whether its labeller
    started the run or wrote the cited spec approval; None without one."""
    drawn = await repo.list_labellings(conn, run["run_id"], cycle)
    counts = {"drawn": len(drawn), "labelled": sum(v["labelled_at"] is not None for v in drawn)}
    facts = await _format_2(conn, run, cycle, spec, adjudication) | {"format": 3, "labellings": counts}
    if labelling is None:
        return facts | {"accuracy": None}
    score = labelling["accuracy"]

    def counted(found: Mapping[str, Any] | None, *sizes: str) -> dict[str, Any] | None:
        labels = found and {label: s["x"] for label, s in found["labels"].items()}
        return found and {name: found[name] for name in sizes} | {"labels": labels}

    discrepant = counted(score["discrepant"], "items", "uniform", "census")
    names = ("labelling_id", "salt", "selection_sha256", "labels_sha256", "score_sha256", "drawn_by",
             "labelled_by")  # fmt: skip
    attested = {name: labelling[name] for name in names} | {
        "truth": score["truth"],
        "unlocatable": score["unlocatable"],
        "labelled_at": utc_iso(labelling["labelled_at"]),
        "labeller": {
            "started_run": labelling["started_run"],
            "spec_author": labelling["spec_author"] is not None,
        },
        "discrepant": discrepant and discrepant | {"reasons": score["discrepant"]["reasons"]},
        "accepted": counted(score["accepted"], "keys", "drawn"),
        "agreed": counted(score["agreed"], "keys", "drawn"),
    }
    return facts | {"accuracy": attested}


async def _facts(
    conn: repo.Conn, run: Mapping[str, Any], cycle: int, spec: CompiledSpec
) -> tuple[dict[str, Any], repo.Row, repo.Row | None]:
    """What every attests format holds but the rules, and the cycle's tombstone and scan state."""
    run_id = run["run_id"]
    ended = await repo.get_cycle(conn, run_id, cycle)
    if ended is None:
        raise LookupError(f"cycle {cycle} of run {run_id} has not ended")
    causes = await repo.list_root_causes(conn, run_id, cycle)
    scan = await repo.get_scan(conn, run_id, cycle)
    facts = {
        "run_id": run_id,
        "cycle": cycle,
        "mode": run["mode"],
        "auth_mode": (run["params"].get("approval") or {}).get("mode"),  # the one it started in
        "keys_source": run["params"].get("keys_source"),
        "scan": None if scan is None else {name: scan[name] for name in _SCAN_FIELDS},
        "spec_id": run["spec_id"],
        "content_hash": (await repo.get_spec(conn, run["spec_id"]))["content_hash"],  # type: ignore[index]
        "spec_approval_id": await _cited(conn, run, ended, spec),
        "created_by": run["created_by"],
        "ended_at": utc_iso(ended["ended_at"]),
        "counters": ended["counters"],
        "unexplained_critical": await repo.unexplained_criticals(conn, run_id, cycle, bug_rules(spec)),
        "root_causes": [{name: cause[name] for name in _ROOT_CAUSE_FIELDS} for cause in causes],
    }
    return facts, ended, scan


def attests_sha256(facts: Mapping[str, Any]) -> str:
    return sha256_hex(jsonio.dumps(facts, sort_keys=True))


def plan(params: Mapping[str, Any]) -> str:
    """A run's stored ``params`` but its ``approval`` record (which a resume extends) and a file side's sealed
    pins (docs/mvp/FILES.md §6), as a pack's ``facts/plan.json`` holds them and ``create_run``'s
    ``plan_sha256`` hashes them (docs/mvp/PACK.md §2.1, §7.4)."""
    kept = {name: value for name, value in params.items() if name not in ("approval", "pinned")}
    return jsonio.dumps(kept, sort_keys=True)


async def revoke(
    conn: repo.Conn, audit: repo.Audit, approval_id: str, reason: str, *, who: Principal
) -> dict[str, Any]:
    """Revoke an approved approval as ``who`` in the caller's transaction, in order (§2): the approval
    (its row lock); a spec approval's running runs pause and its queued one-shot runs stop; the approved
    sign-offs citing it are revoked, and the open ones rejected when ``who`` is a person (else a decision
    refuses them); a run left without an approved sign-off loses its sign-off hold. Each revoked
    sign-off's evidence packs are revoked with it (ds.tg_signoff_packs; docs/mvp/PACK.md §6). Audited
    last. LookupError for an unknown approval, ApprovalConflict for one that is not approved."""
    row = await repo.revoke_approval(conn, approval_id, who.actor, reason)
    if row is None:
        found = await repo.get_approval(conn, approval_id)
        if found is None:
            raise LookupError(f"approval {approval_id} not found")
        raise ApprovalConflict(f"approval {approval_id} is {found['status']}, not approved")
    paused: list[str] = []
    stopped: list[str] = []
    revoked: list[str] = []
    rejected: list[str] = []
    runs = {row["run_id"]} if row["kind"] == "signoff" else set()
    cascade = f"spec approval {approval_id} was revoked"
    if row["kind"] == "spec":
        for run in await repo.approval_runs(conn, approval_id):
            run_id = run["run_id"]
            if run["status"] == "running":
                await repo.set_run_status(conn, run_id, "paused", stop_reason=APPROVAL_PAUSE)
                paused.append(run_id)
            elif run["mode"] == "one_shot":
                await repo.set_run_status(conn, run_id, "cancelling", stop_reason=APPROVAL_REVOKED)
                await repo.cancel_keys(conn, run_id)
                stopped.append(run_id)
        for signoff in await repo.signoffs_citing(conn, approval_id):
            if signoff["status"] == "approved":
                await repo.revoke_approval(conn, signoff["approval_id"], who.actor, cascade)
                revoked.append(signoff["approval_id"])
                runs.add(signoff["run_id"])
            elif who.person:
                await repo.decide_approval(
                    conn, signoff["approval_id"], who.actor, approve=False, note=cascade
                )
                rejected.append(signoff["approval_id"])
    released = []
    for run_id in sorted(runs):
        if (hold := await repo.release_signoff_hold(conn, run_id)) is not None:
            released.append((run_id, hold))
    for run_id in paused:
        await audit(who, "paused", run_id, {"reason": APPROVAL_PAUSE, "approval_id": approval_id})
    for run_id in stopped:
        await audit(who, "cancelling", run_id, {"reason": APPROVAL_REVOKED, "approval_id": approval_id})
    detail = {"reason": reason, "paused": paused, "stopped": stopped, "cascade_of": None}
    await audit(who, "approval.revoke", approval_id, detail)
    for signoff_id in revoked:
        detail = {"reason": cascade, "paused": [], "stopped": [], "cascade_of": approval_id}
        await audit(who, "approval.revoke", signoff_id, detail)
    for signoff_id in rejected:
        await audit(who, "approval.reject", signoff_id, {"kind": "signoff", "cascade_of": approval_id})
    signoffs = [approval_id] if row["kind"] == "signoff" else revoked
    for pack in await repo.revoke_packs(conn, signoffs):
        detail = {"reason": pack["revoke_reason"], "cascade_of": pack["signoff_id"]}
        await audit(who, "pack.revoke", pack["pack_id"], detail)
    for run_id, hold in released:
        await audit(who, "retention.release", run_id, {"reason": hold})
    return {
        "approval_id": approval_id,
        "status": "revoked",
        "paused": paused,
        "stopped": stopped,
        "revoked": revoked,
    }
