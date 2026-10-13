"""Alerts and notifications: the events fed audit rows make, the rules that route them
and the closed texts and bodies they are sent as. Pure: ``runner.notify`` reads the store and sends.

``EVENTS`` maps each fed audit action to its projector and the kinds it may yield. A projector turns the
row and what the feed read for it (``Seen``) into events; each event's facts must pass its kind's model
(ids, counts, closed codes, slugs, versions and times) or it is never sent (``Unsafe``), so a bug can
drop a notification, never leak one. ``fires`` matches a rule; ``payload`` and ``body`` make one
channel's delivery, rendered once, so every attempt sends the same bytes. A pair's change of
cut-over readiness is one such event (``readiness.changed``), a schedule's time
that started no run another (``schedule.missed``), as is the worker's pause of a
schedule before a time of it came due; and what the schema guard recorded of a run's tables a third
(``schema.drift``), its pause of the run being a ``run.paused`` as any other.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from deltasleuth.core import jsonio
from deltasleuth.core.readiness import worded
from deltasleuth.core.spec.targets import address_rule

if TYPE_CHECKING:
    from deltasleuth.config import ChannelSettings, RuleSettings

FORMAT = "deltasleuth-notify/1"
Kind = Literal[
    "cycle.ended", "sample.judged", "run.finished", "run.failed", "run.stopped", "run.paused",
    "sample.draw_failed", "accuracy.measured", "approval.waiting", "approval.revoked", "licence.state",
    "readiness.changed", "schedule.missed", "schema.drift", "notify.dead", "notify.skipped", "notify.daily",
    "notify.test",
]  # fmt: skip
KINDS: tuple[str, ...] = get_args(Kind)
RESERVED: dict[str, str] = {}  # an event refused in rules until its feature is built: none now
UNSPECIFIC = ("licence.state", "notify.dead", "notify.skipped", "notify.daily")  # they match no spec
# runner.runs.AUTH_PAUSE, KEY_PAUSE, approvals.APPROVAL_PAUSE, licence.LICENCE_PAUSE, sample.SAMPLE_PAUSE,
# drift.SCHEMA_PAUSE
Pause = Literal[
    "run_paused_auth_error", "run_paused_key_missing", "run_paused_approval", "run_paused_licence",
    "run_paused_sample", "run_paused_schema_drift",
]  # fmt: skip
PAUSES: tuple[str, ...] = get_args(Pause)
DRAW_PAUSE = "run_paused_draw"  # sample.DRAW_PAUSE: its sample.draw_failed notifies instead
STOP_PREFIX = "stop_condition:"  # runner.stop.STOP_PREFIX
Stop = Literal[
    "maxErrorRate", "maxInconclusiveRate", "maxConsecutiveErrors", "maxUnexplainedCritical", "until",
    "maxDuration", "maxCycles", "maxKeys",
]  # fmt: skip
ALERT_STOPS = get_args(Stop)[:4]
End = Literal["cancelled", "erasure", "abandoned", "approval_revoked"]  # the reasons sent besides stops
Reason = Literal[
    "run_paused_auth_error", "run_paused_key_missing", "run_paused_approval", "run_paused_licence",
    "run_paused_sample", "run_paused_schema_drift", "cancelled", "erasure", "abandoned", "approval_revoked",
    "stop_condition:maxErrorRate", "stop_condition:maxInconclusiveRate",
    "stop_condition:maxConsecutiveErrors",
    "stop_condition:maxUnexplainedCritical", "stop_condition:until", "stop_condition:maxDuration",
    "stop_condition:maxCycles", "stop_condition:maxKeys",
]  # fmt: skip
State = Literal["open", "evaluation", "valid", "ending", "grace", "expired"]  # open: no vendor key
# Cut-over readiness: core.readiness' STATES, GATES and CAUSES, in order.
Ready = Literal[
    "no_evidence", "not_ready", "unknown", "parity_ready", "signoff_ready", "signed_off", "evidenced"
]
Gate = Literal[
    "population", "critical", "targets", "convergence", "fresh", "graph", "spec_approval", "adjudication",
    "sample_ledger", "accuracy", "signoff", "pack",
]  # fmt: skip
GateStatus = Literal["met", "not_met", "unknown", "not_applicable", "unavailable"]
Cause = Literal[
    "baseline", "cycle", "version", "signoff", "pack", "adjudication", "accuracy", "ledger", "licence",
    "config", "closed", "time",
]  # fmt: skip
# Why a schedule's time started no run: each reason's codes and its severity.
MISSED: dict[str, tuple[tuple[str | None, ...], str]] = {
    "late": (("down", "busy", "fired_already"), "alert"),
    "superseded": (("update", "pause", "delete"), "alert"),
    "zone": (("zone", "cron"), "alert"),
    "mode": ((None,), "alert"),
    "setter": ((None,), "alert"),
    "not_started": ((None, "export_absent", "unsettled", "stale", "unstamped", "planning"), "alert"),
    "error": ((None,), "alert"),
    "overlap": ((None,), "warning"),
    "approval": ((None,), "warning"),
    "licence": (("expired", "sso", "pairs"), "warning"),
    "sample": (("live", "unclosed", "refused"), "warning"),
}
# What the schema guard tells of a table: core.precheck's BREAKING and OTHER.
Drift = Literal[
    "relation_missing", "column_missing", "class_changed", "key_render_changed", "render_changed",
    "compared_lost", "column_added", "column_removed", "type_changed", "nullable_changed", "key_changed",
    "kind_changed",
]  # fmt: skip
Judged = Literal["passed", "failed", "incomplete", "unjudged"]
Mode = Literal["one_shot", "batch", "soak"]
Status = Literal[
    "queued", "running", "paused", "cancelling", "cancelled", "completed", "completed_with_issues", "failed"
]  # fmt: skip
COUNTERS = ("total", "compared", "match", "different", "indeterminate", "cancelled", "outstanding",
            "mismatch_rate", "coverage", "absent_both")  # fmt: skip
CHANGES = {"new_root_causes_at_least": "new_root_causes", "newly_discrepant_at_least": "newly_discrepant"}
# A level condition: its fact, whether it holds above (else below) its bound, and how the text names it.
LEVELS = {
    "mismatch_rate_above": ("mismatch_rate", True, "Mismatch rate"),
    "indeterminate_rate_above": ("indeterminate_rate", True, "Indeterminate rate"),
    "coverage_below": ("coverage", False, "Coverage"),
    "unexplained_critical_at_least": (
        "unexplained_critical",
        True,
        "Keys with unexplained CRITICAL findings",
    ),
}
Level = Literal["mismatch_rate", "indeterminate_rate", "coverage", "unexplained_critical"]
Fact = Literal["new_root_causes", "gone_root_causes", "newly_discrepant", "no_longer_discrepant",
               "unexplained_critical", "mismatch_rate", "indeterminate_rate", "coverage"]  # fmt: skip
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
NOT_EVIDENCE = "Not sign-off evidence."  # approvals.NOT_EVIDENCE


class Unsafe(ValueError):
    """An event's facts fail its kind's model: it is never sent."""


# -- addresses -------------------------------------------------------------------------------------


def refusal(address: str, allow_private: bool) -> str | None:
    """Why ``address`` is no notification destination (``address_rule``'s judgement, which a database
    target shares); None when it is one."""
    rule = address_rule(address)
    if rule == "never":
        return (
            f"{address} is never a notification destination (link-local, unspecified, multicast or metadata)"
        )
    if rule == "private" and not allow_private:
        return f"{address} is a private address: set allow_private_network to allow it"
    return None


# -- facts that may leave ------------------------------------------------------------------------

_FACTS = ConfigDict(extra="forbid", strict=True)
Id = Annotated[str, Field(pattern=r"^(R|A|D|LI|N|P|SC|V)-[0-9A-Z]{26}$")]
Installation = Annotated[str, Field(pattern=r"^I-[0-9a-f]{16}$")]
Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", max_length=63)]
Semver = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]
Day = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
Rate = Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]+)?$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Minute = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$")]  # cron.utc
# The codes a delivery dies with.
Death = Literal[
    "retries_exhausted", "http_3xx", "http_4xx", "unsafe_destination", "url_mismatch", "smtp_permanent",
    "smtp_no_starttls", "payload_too_large", "channel_removed", "channel_changed", "expired",
]  # fmt: skip
Count = Annotated[int, Field(ge=0)]
Cycle = Annotated[int, Field(ge=1)]


class _Model(BaseModel):
    model_config = _FACTS


class Now(_Model):
    cycle: Cycle
    status: Status
    licence: State | None
    approval: Literal["approved", "revoked", "unapproved", "not_needed"]


class RunBlock(_Model):
    run_id: Id
    spec_id: Count
    spec_name: Slug | None
    spec_version: Semver
    mode: Mode
    now: Now


class Counters(_Model):
    total: Count
    compared: Count
    match: Count
    different: Count
    indeterminate: Count
    cancelled: Count
    outstanding: Count
    mismatch_rate: Rate | Literal["unavailable"]
    coverage: Rate | Literal["unavailable"]
    absent_both: Count


class Levels(_Model):
    mismatch_rate: Rate | None
    indeterminate_rate: Rate | None
    coverage: Rate | None
    unexplained_critical: Count | None


class Previous(_Model):
    cycle: Cycle
    value: Rate | Count


class CycleEnded(_Model):
    cycle: Cycle
    counters: Counters
    levels: Levels
    previous: dict[Level, Previous | None]
    new_root_causes: Count | None
    gone_root_causes: Count | None
    baseline_from: Cycle | None
    newly_discrepant: Count | None
    no_longer_discrepant: Count | None
    between_samples: bool
    unavailable: list[Fact]


class Strata(_Model):
    passed: Count
    failed: Count
    unfinished: Count


class SampleJudged(_Model):
    cycle: Cycle
    draw_id: Id
    method: Literal["bottom_k", "rate"]
    status: Judged
    strata: Strata


class RunFinished(_Model):
    status: Literal["completed", "completed_with_issues", "cancelled"]
    reason: Reason | None
    cycle: Cycle


class RunFailed(_Model):
    cycle: Cycle
    error_type: Annotated[str, Field(pattern=f"^{_ERROR_TYPE.pattern}$")]  # pydantic searches: anchored


class RunStopped(_Model):
    cycle: Cycle
    condition: Stop


class RunPaused(_Model):
    cycle: Cycle
    reason: Pause
    approval_id: Id | None
    draw_id: Id | None
    licence_refusal: Literal["expired", "sso", "unseated"] | None
    # the schema guard's pause: each side's shape as it was seen, by its hash (None: no table side)
    schema_: dict[Literal["a", "b"], Sha256 | None] | None = Field(None, alias="schema")


class DriftSide(_Model):
    breaking: Count
    other: Count
    kinds: dict[Drift, Count]
    from_: Sha256 | None = Field(alias="from")  # the shape last recorded; None: none was
    to: Sha256 | None


class SchemaDrift(_Model):
    """What the schema guard recorded of a run's tables: counts, closed kinds,
    an id and hashes; never a relation's or a column's name, nor a declared type."""

    cycle: Cycle
    where: Literal["creation", "scan", "cycle", "resume", "read"]
    mode: Literal["finding", "pause"]
    checked: bool
    paused: bool
    paused_seq: Count | None  # the paused row of its trip, whose dedup key it shares
    basis: Id | None
    sides: dict[Literal["a", "b"], DriftSide]
    error_type: Annotated[str, Field(pattern=f"^{_ERROR_TYPE.pattern}$")] | None

    @model_validator(mode="after")
    def _looked(self) -> SchemaDrift:
        if self.checked == (self.error_type is not None):
            raise ValueError("an exception's type is a look's that was not made, and no other's")
        return self


class DrawFailed(_Model):
    cycle: Cycle


class Measured(_Model):
    cycle: Cycle
    labelling_id: Id
    labelled: Count
    undecidable: Count
    not_reviewed: Count


class ApprovalWaiting(_Model):
    approval_id: Id
    kind: Literal["spec", "signoff"]
    spec_id: Count
    run_id: Id | None
    cycle: Cycle | None
    approvals_left: Cycle


class ApprovalRevoked(_Model):
    approval_id: Id
    kind: Literal["spec", "signoff"]
    paused: Count
    stopped: Count


class LicenceState(_Model):
    installation_id: Installation
    from_: State | None = Field(alias="from")
    to: State
    licence_id: Id | None
    ends: Day | None


class Scored(_Model):
    passed: Annotated[int, Field(ge=0, le=12)]
    required: Annotated[int, Field(ge=0, le=12)]


class GateChange(_Model):
    gate: Gate
    from_: GateStatus | None = Field(alias="from")  # None: the gate was not required
    to: GateStatus | None


class CycleRef(_Model):
    run_id: Id
    cycle: Cycle


class ReadinessChanged(_Model):
    pair: Slug  # a label, as are programme and entity: withheld per channel
    programme: Slug | None
    entity: Slug | None
    from_: Ready | None = Field(alias="from")  # None: the pair's baseline
    to: Ready
    was_regressed: bool
    regressed: bool
    score: Scored
    causes: list[Cause]
    gates: dict[Gate, GateStatus]  # the required gates
    changed: Annotated[list[GateChange], Field(max_length=12)]
    evidence: CycleRef | None
    candidate: CycleRef | None
    signoff_id: Id | None
    pack_id: Id | None
    config_sha256: Sha256


class ScheduleMissed(_Model):
    schedule_id: Id
    reason: Literal[
        "late", "superseded", "zone", "mode", "setter", "not_started", "error", "overlap", "approval",
        "licence", "sample",
    ]  # fmt: skip
    code: Literal[
        "down", "busy", "fired_already", "update", "pause", "delete", "zone", "cron", "expired", "sso",
        "pairs", "live", "unclosed", "refused", "export_absent", "unsettled", "stale", "unstamped",
        "planning",
    ] | None  # fmt: skip
    due: Minute | None  # None: the worker paused the schedule before a time of it came due
    to: Minute | None  # a range's last time, with how many it counts
    count: Cycle | None
    run_id: Id | None
    draw_id: Id | None
    error_type: Annotated[str, Field(pattern=f"^{_ERROR_TYPE.pattern}$")] | None

    @model_validator(mode="after")
    def _coded(self) -> ScheduleMissed:
        if self.code not in MISSED[self.reason][0]:
            raise ValueError("the code is not one of its reason's")
        if (self.error_type is None) == (self.reason in ("not_started", "error")):
            raise ValueError("an exception's type is not_started's and error's, and no other reason's")
        if self.due is None and self.reason != "zone":
            raise ValueError("only the worker's pause (zone) is without a time")
        return self


class Dead(_Model):
    channel: Slug
    delivery_id: Id
    event: Kind
    attempts: Count
    error: Death
    dead_since_last: Count


class Skipped(_Model):
    reason: Literal["stale", "unsafe", "error"]
    from_seq: Count | None = None
    to_seq: Count | None = None
    rows: Count | None = None
    seqs: list[Count] | None = None


class ChannelCounts(_Model):
    pending: Count
    sent_24h: Count
    dead_24h: Count
    suppressed_24h: Count
    head_age_s: Count | None


class Daily(_Model):
    runs: dict[Literal["queued", "running", "paused", "cancelling"], Count]
    paused: dict[Literal[Pause, "run_paused_draw", "manual", "other"], Count]
    cycles_ended_24h: Count
    runs_finished_24h: Count
    approvals_waiting: Count
    licence: State | None
    channels: dict[Slug, ChannelCounts]
    feed_lag_s: Count
    mutes: Count


class Tested(_Model):
    channel: Slug


MODELS: dict[str, type[_Model]] = {
    "cycle.ended": CycleEnded, "sample.judged": SampleJudged, "run.finished": RunFinished,
    "run.failed": RunFailed, "run.stopped": RunStopped, "run.paused": RunPaused,
    "sample.draw_failed": DrawFailed, "accuracy.measured": Measured,
    "approval.waiting": ApprovalWaiting, "approval.revoked": ApprovalRevoked, "licence.state": LicenceState,
    "readiness.changed": ReadinessChanged, "schedule.missed": ScheduleMissed, "schema.drift": SchemaDrift,
    "notify.dead": Dead, "notify.skipped": Skipped, "notify.daily": Daily, "notify.test": Tested,
}  # fmt: skip


# -- events --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    """What the feed read for a row, as of its pass: the run the row names (``run``, its block; ``spec``,
    the spec's name rules match; ``mode``; ``unlabelled``, why the name is never sent), the cycle the row
    concerns with its ``ds.cycles`` row (``ended``) and draw, whether the run has a ``cancelling`` row, and
    the approval the row names (with ``spec``)."""

    run: dict[str, Any] | None = None
    spec: str | None = None
    mode: str | None = None
    unlabelled: str | None = None
    cycle: int | None = None
    ended: Mapping[str, Any] | None = None
    draw: Mapping[str, Any] | None = None
    cancelling: bool = False
    approval: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Event:
    """A typed event: its ``kind`` and ``facts``, the run block of a run's event, the spec name and mode rules
    match, why the name is never sent, and the audit action it came from (``via``)."""

    kind: str
    facts: dict[str, Any]
    run: dict[str, Any] | None = None
    spec: str | None = None
    mode: str | None = None
    unlabelled: str | None = None
    via: str | None = None


def _event(seen: Seen, row: Mapping[str, Any], kind: str, facts: dict[str, Any]) -> Event:
    return Event(kind, facts, seen.run, seen.spec, seen.mode, seen.unlabelled, row["action"])


def error_type(reason: str | None) -> str:
    """What a failed run's ``reason`` (``failure(exc)``) is sent as: its exception type, which is all of it
    or what stands before ``: `` (a table side's ``<class>: <code>``, the guard's text), ``SecretError``
    for a secret's message (which names a host path), else ``Error``."""
    head = (reason or "").partition(": ")[0]
    if _ERROR_TYPE.fullmatch(head):
        return head
    return "SecretError" if reason and (" " in reason or "/" in reason) else "Error"


def _sent(reason: str | None) -> str | None:
    """A run's reason as sent: one of the ends or a stop condition, else None."""
    stop = (reason or "").removeprefix(STOP_PREFIX)
    ok = reason in get_args(End) or (reason != stop and stop in get_args(Stop))
    return reason if ok else None


def _ended(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    counters = (seen.ended or {}).get("counters") or {}
    if not counters:  # a cycle ended unsummarised (an erasure's)
        return []
    facts = {"cycle": seen.cycle, "counters": {name: counters.get(name) for name in COUNTERS}}
    return [_event(seen, row, "cycle.ended", facts)]


def _judged(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    if seen.draw is None:
        return []
    judged = (seen.ended or {}).get("sample")
    verdicts = Counter(s["verdict"] for s in (judged or {}).get("strata", []))
    facts = {
        "cycle": seen.cycle, "draw_id": seen.draw["draw_id"], "method": seen.draw["method"],
        "status": judged["status"] if judged else "unjudged",
        "strata": {name: verdicts[name] for name in ("passed", "failed", "unfinished")},
    }  # fmt: skip
    return [_event(seen, row, "sample.judged", facts)]


def _cycle(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    return [*_ended(row, seen), *_judged(row, seen)]


def _finished(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    status, reason = row["detail"]["status"], row["detail"].get("reason")
    events = _ended(row, seen) if status in ("completed", "completed_with_issues") else []
    events += _judged(row, seen)  # fail_run judges first
    if status == "failed":
        events.append(
            _event(seen, row, "run.failed", {"cycle": seen.cycle, "error_type": error_type(reason)})
        )
    else:
        facts = {"status": status, "reason": _sent(reason), "cycle": seen.cycle}
        events.append(_event(seen, row, "run.finished", facts))
    if reason in (STOP_PREFIX + "until", STOP_PREFIX + "maxDuration") and not seen.cancelling:  # at_boundary
        facts = {"cycle": seen.cycle, "condition": reason.removeprefix(STOP_PREFIX)}
        events.append(_event(seen, row, "run.stopped", facts))
    return events


def _cancelling(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    reason = row["detail"].get("reason") or ""
    if not reason.startswith(STOP_PREFIX):
        return []
    return [
        _event(seen, row, "run.stopped", {"cycle": seen.cycle, "condition": reason.removeprefix(STOP_PREFIX)})
    ]


def _paused(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    """A pause with a reason, but a refused draw's (its sample.draw_failed notifies) and a revoke's (its
    approval.revoked counts it); a person's own pause has none."""
    detail = row["detail"]
    reason, approval_id = detail.get("reason"), detail.get("approval_id")
    if reason is None or reason == DRAW_PAUSE or (reason == "run_paused_approval" and approval_id):
        return []
    facts = {
        "cycle": seen.cycle, "reason": reason, "approval_id": approval_id, "draw_id": detail.get("draw_id"),
        "licence_refusal": (detail.get("licence") or {}).get("refusal"),
    }  # fmt: skip
    schema = {"schema": detail["schema"]} if "schema" in detail else {}  # the schema guard's pause
    return [_event(seen, row, "run.paused", facts | schema)]


def _drift(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    """What the schema guard recorded of the run's tables: the row's own
    members and no other, each side's as counts, closed kinds and two hashes."""
    detail = row["detail"]
    names = ("cycle", "where", "mode", "checked", "paused", "paused_seq", "basis", "error_type")
    part = ("breaking", "other", "kinds", "from", "to")
    sides = {side: {name: held.get(name) for name in part} for side, held in detail["sides"].items()}
    return [_event(seen, row, "schema.drift", {name: detail.get(name) for name in names} | {"sides": sides})]


def _draw_failed(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    return [_event(seen, row, "sample.draw_failed", {"cycle": row["detail"]["cycle"]})]


def _measured(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    """A labelling labelled: its id and counts, never a label or a share."""
    counts = {name: row["detail"].get(name) for name in ("labelled", "undecidable", "not_reviewed")}
    facts = {"cycle": seen.cycle, "labelling_id": row["object"]} | counts
    return [_event(seen, row, "accuracy.measured", facts)]


def _waiting(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    approval, detail = seen.approval, row["detail"]
    if approval is None or approval["status"] != "requested":
        return []
    left = (
        approval["approvers_needed"] if row["action"] == "approval.request" else detail["of"] - detail["step"]
    )
    if left < 1:
        return []
    facts = {
        "approval_id": row["object"], "kind": detail["kind"], "spec_id": detail["spec_id"],
        "run_id": detail.get("run_id"), "cycle": detail.get("cycle"), "approvals_left": left,
    }  # fmt: skip
    return [_event(seen, row, "approval.waiting", facts)]


def _revoked(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    detail = row["detail"]
    if detail.get("cascade_of") is not None or seen.approval is None:
        return []
    facts = {
        "approval_id": row["object"], "kind": seen.approval["kind"], "paused": len(detail["paused"]),
        "stopped": len(detail["stopped"]),
    }  # fmt: skip
    return [_event(seen, row, "approval.revoked", facts)]


def _licence(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    detail = row["detail"]
    facts = {"installation_id": row["object"]} | {
        name: detail.get(name) for name in ("from", "to", "licence_id")
    }
    return [_event(seen, row, "licence.state", facts | {"ends": detail.get("ends")})]


def _readiness(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    """A pair's readiness changed: ids, states, counts and slugs, the pair as its
    row's object names it; each changed gate's statuses, never its counts or its text."""
    detail = row["detail"]
    names = ("programme", "entity", "from", "to", "was_regressed", "regressed", "score", "causes", "gates",
             "evidence", "candidate", "signoff_id", "pack_id", "config_sha256")  # fmt: skip
    changed = [{name: gate.get(name) for name in ("gate", "from", "to")} for gate in detail["changed"]]
    facts = {"pair": row["object"].removeprefix("readiness:")} | {name: detail.get(name) for name in names}
    return [_event(seen, row, "readiness.changed", facts | {"changed": changed})]


def _missed(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    """A schedule's time, or counted range of times, that started no run: ids, times, closed codes and an
    exception's type. The spec's name and the mode are the row's own, for rules to match: no payload carries
    them. Of the ``schedule.pause`` rows, only the worker's that chained no missed row (a cron or zone this
    build cannot load, before a time is due) is sent, as the same event without a time: a person's pause
    sends nothing, and a missed row beside the worker's is the notification."""
    detail = row["detail"]
    if row["action"] == "schedule.pause" and not (
        detail.get("reason") == "zone" and detail.get("missed") is False
    ):
        return []
    names = ("reason", "code", "due", "to", "count", "run_id", "draw_id", "error_type")
    facts = {"schedule_id": row["object"]} | {name: detail.get(name) for name in names}
    return [
        Event(
            "schedule.missed", facts, None, detail.get("spec_name"), detail.get("mode"), None, row["action"]
        )
    ]


def _dead(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    detail, names = row["detail"], ("delivery_id", "event", "attempts", "error", "dead_since_last")
    facts = {"channel": row["object"].removeprefix("notify:")} | {name: detail.get(name) for name in names}
    return [_event(seen, row, "notify.dead", facts)]


def _skipped(row: Mapping[str, Any], seen: Seen) -> list[Event]:
    return [_event(seen, row, "notify.skipped", dict(row["detail"]))]


Projector = Callable[[Mapping[str, Any], Seen], list[Event]]
EVENTS: dict[str, tuple[Projector, frozenset[str]]] = {
    "run.cycle": (_cycle, frozenset({"cycle.ended", "sample.judged"})),
    "run.finished": (_finished, frozenset({"cycle.ended", "sample.judged", "run.finished", "run.failed",
                                           "run.stopped"})),
    "cancelling": (_cancelling, frozenset({"run.stopped"})),
    "paused": (_paused, frozenset({"run.paused"})),
    "sample.draw_failed": (_draw_failed, frozenset({"sample.draw_failed"})),
    "accuracy.label": (_measured, frozenset({"accuracy.measured"})),
    "approval.request": (_waiting, frozenset({"approval.waiting"})),
    "approval.grant": (_waiting, frozenset({"approval.waiting"})),
    "approval.revoke": (_revoked, frozenset({"approval.revoked"})),
    "licence.state": (_licence, frozenset({"licence.state"})),
    "readiness.changed": (_readiness, frozenset({"readiness.changed"})),
    "schedule.missed": (_missed, frozenset({"schedule.missed"})),
    "schedule.pause": (_missed, frozenset({"schedule.missed"})),
    "schema.drift": (_drift, frozenset({"schema.drift"})),
    "notify.dead": (_dead, frozenset({"notify.dead"})),
    "notify.skipped": (_skipped, frozenset({"notify.skipped"})),
}  # fmt: skip
FEED_ACTIONS = tuple(EVENTS)


def concerned(row: Mapping[str, Any]) -> int | None:
    """The cycle a run's row names: the one a ``run.cycle`` ended, a refused draw's, a labelling's, the one
    the schema guard looked in; None: the run's cycle as of the row, which the chain tells."""
    cycle = row["detail"].get("cycle")
    if row["action"] == "run.cycle":
        return cycle - 1
    return cycle if row["action"] in ("sample.draw_failed", "accuracy.label", "schema.drift") else None


def checked(event: Event) -> Event:
    """``event`` with its facts and run block as their models dump them; Unsafe when either fails."""
    try:
        facts = MODELS[event.kind].model_validate(event.facts).model_dump(by_alias=True)
        run = event.run and RunBlock.model_validate(event.run).model_dump()
    except ValidationError as exc:
        raise Unsafe(f"{event.kind}: {exc.error_count()} fact(s) fail its model") from None
    return replace(event, facts=facts, run=run)


def dedup(event: Event, seq: int | str | None) -> str:
    """The event's de-duplication key; ``seq``: its audit row's, a daily's date or a test's id."""
    f, run_id = event.facts, (event.run or {}).get("run_id")
    match event.kind:
        case "cycle.ended" | "sample.judged":
            return f"{event.kind}|{run_id}|{f['cycle']}"
        case "run.finished" | "run.failed":
            return f"run.end|{run_id}"
        case "run.stopped":
            return f"run.stopped|{run_id}"
        # A schema-guard pause and the schema.drift of its trip share a key, so one is sent; a later pause of
        # the same tables is its own (a person resumed the run in between).
        case "run.paused" if f.get("schema"):
            return f"schema.drift|{run_id}|{seq}"
        case "schema.drift" if f["paused_seq"] is not None:
            return f"schema.drift|{run_id}|{f['paused_seq']}"
        case "schema.drift":
            to = [(f["sides"].get(side) or {}).get("to") for side in "ab"]
            return f"schema.drift|{run_id}|{to[0]}|{to[1]}"
        case "run.paused" | "sample.draw_failed":
            return f"{event.kind}|{run_id}|{seq}"
        case "accuracy.measured":
            return f"accuracy.measured|{f['labelling_id']}"
        case "approval.waiting":
            return f"approval.waiting|{f['approval_id']}|{f['approvals_left']}"
        case "approval.revoked":
            return f"approval.revoked|{f['approval_id']}"
        case "licence.state":
            return f"licence.state|{f['to']}|{f['licence_id']}"
        case "readiness.changed":  # the required gates not met, a bit each in the gates' order
            gates = get_args(Gate)
            unmet = [gate for gate, status in f["gates"].items() if status in ("not_met", "unknown")]
            mask = sum(1 << (len(gates) - 1 - gates.index(gate)) for gate in unmet)
            return f"readiness.changed|{f['pair']}|{f['to']}|{mask:03x}"
        case "schedule.missed":
            return f"schedule.missed|{f['schedule_id']}|{f['reason']}"
        case "notify.dead":
            return f"notify.dead|{f['channel']}"
    return f"{event.kind}|{seq}"  # notify.skipped, notify.daily, notify.test


def superseded(event: Event) -> str | None:
    """The prefix of the dedup keys of which only a channel's newest delivery makes a duplicate: a
    pair's readiness, so that a fall its recovery followed is sent again. None: any delivery of the key."""
    return f"readiness.changed|{event.facts['pair']}|" if event.kind == "readiness.changed" else None


def severity(event: Event, held: bool) -> str:
    """``alert``, ``warning`` or ``info``; ``held``: a firing rule's condition held or crossed."""
    f = event.facts
    match event.kind:
        case "cycle.ended":
            k = f["counters"]
            return "warning" if held or k["different"] or k["indeterminate"] else "info"
        case "sample.judged":
            return "info" if f["status"] == "passed" else "warning"
        case "run.finished":
            return "warning" if f["status"] == "completed_with_issues" else "info"
        case "run.failed" | "approval.revoked" | "notify.dead":
            return "alert"
        case "run.stopped":
            return "alert" if f["condition"] in ALERT_STOPS else "warning"
        case "run.paused":
            alerts = ("run_paused_auth_error", "run_paused_key_missing", "run_paused_schema_drift")
            return "alert" if f["reason"] in alerts else "warning"
        case "schema.drift":
            return "alert" if f["paused"] else "warning"
        case "sample.draw_failed" | "notify.skipped":
            return "warning"
        case "licence.state":
            return {"expired": "alert", "ending": "warning", "grace": "warning"}.get(f["to"], "info")
        case "readiness.changed":
            went = {gate["to"] for gate in f["changed"]}
            states = get_args(Ready)
            if "baseline" in f["causes"]:
                return "info"
            fell = f["from"] is not None and states.index(f["to"]) < states.index(f["from"])
            # A gate fell when it left met, or went not met or unknown as the state fell: one that only
            # became judged (from unavailable, not applicable or not required) on the way up is no alert.
            lost = {gate["to"] for gate in f["changed"] if gate["from"] == "met" or fell}
            if (f["regressed"] and not f["was_regressed"]) or lost & {"not_met", "unknown"}:
                return "alert"
            return "warning" if "unavailable" in went or fell else "info"
        case "schedule.missed":
            return MISSED[f["reason"]][1]
    return "info"


# -- rules -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fired:
    """Why a rule fired: the change conditions that held, the level conditions crossed and recovered."""

    held: tuple[str, ...] = ()
    crossed: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()


def routes(rule: RuleSettings, event: Event) -> bool:
    """Whether ``rule`` takes events like ``event``: its kind, its spec (a pair's readiness: the pair;
    licence and notify events: only a rule without specs) and a run's mode."""
    if event.kind not in rule.events:
        return False
    if rule.specs is not None and not any(fnmatchcase(event.spec or "", glob) for glob in rule.specs):
        return False
    return event.mode is None or event.mode in rule.modes


def fires(rule: RuleSettings, event: Event) -> Fired | None:
    """Whether ``rule`` fires for ``event`` (checked, a cycle's digest in its facts) and why; None: it does
    not. A change condition holds each cycle its fact reaches the bound; a level condition fires when it is
    crossed (it holds, not at its baseline) or recovered (the reverse), never while it stays; a null fact
    never holds, crosses or recovers."""
    if not routes(rule, event):
        return None
    f = event.facts
    reason = STOP_PREFIX + f["condition"] if event.kind == "run.stopped" else f.get("reason")
    if (
        (rule.reasons is not None and reason not in rule.reasons)
        or (rule.states is not None and f.get("to") not in rule.states)
        or (rule.statuses is not None and f.get("status") not in rule.statuses)
    ):
        return None
    if event.kind == "readiness.changed" and not _readiness_fires(rule, f):
        return None
    when = rule.when
    if when is None:
        return Fired()
    held = tuple(
        name for name, fact in CHANGES.items()
        if (bound := getattr(when, name)) is not None and f[fact] is not None and f[fact] >= bound
    )  # fmt: skip
    crossed, recovered = [], []
    for name, (fact, _, _) in LEVELS.items():
        if (bound := getattr(when, name)) is None or f["levels"][fact] is None:
            continue
        before = f["previous"][fact]
        now = _level_holds(f["levels"][fact], bound, name)
        was = before is not None and _level_holds(before["value"], bound, name)
        if now != was:
            (crossed if now else recovered).append(name)
    return Fired(held, tuple(crossed), tuple(recovered)) if held or crossed or recovered else None


def _readiness_fires(rule: RuleSettings, f: Mapping[str, Any]) -> bool:
    """A readiness rule's own keys, each checked when present: a pair in no
    programme matches only a rule without ``programmes``; a listed gate is among those changed; a listed
    cause is among the row's (any of them: a fall is never dropped for the causes it names besides)."""
    programme = f["programme"]
    if rule.programmes is not None and not any(
        programme is not None and fnmatchcase(programme, glob) for glob in rule.programmes
    ):
        return False
    if rule.gates is not None and not any(gate["gate"] in rule.gates for gate in f["changed"]):
        return False
    return bool(set(rule.causes or ()) & set(f["causes"]))


def _level_holds(value: Any, bound: Any, condition: str) -> bool:
    """Whether a level fact's ``value`` holds ``condition``'s ``bound``, in Decimals."""
    value, limit = Decimal(str(value)), Decimal(str(bound))
    if condition == "unexplained_critical_at_least":
        return value >= limit
    return value > limit if LEVELS[condition][1] else value < limit


# -- wording -------------------------------------------------------------------------------------

_PAUSED = {
    "run_paused_auth_error": "a side refused its credential. A person fixes it and resumes the run.",
    "run_paused_key_missing": "the keyring lacks a key the run needs. A person restores it and resumes the"
    " run.",
    "run_paused_approval": "its spec version needs a four-eyes approval for its next cycle. A person approves"
    " it and resumes the run.",
    "run_paused_licence": "the licence does not allow its next cycle{refusal}. A person installs a licence"
    " that does and resumes the run.",
    "run_paused_sample": "the draw {draw_id} of the cycle before did not pass. A person closes it and resumes"
    " the run.",
    "run_paused_schema_drift": "the schema guard found that a table it reads changed. A person reads the"
    " run's schema lines, then resumes or cancels it.",
}
_FINISHED = {
    "completed": "completed",
    "completed_with_issues": "completed with issues",
    "cancelled": "cancelled",
}
# Why a schedule's time started no run, one closed clause per reason and code.
_MISSED = {
    ("late", "down"): "no worker could start a run in time (none that fires schedules ran, the store was"
    " down, or the clock moved); a schedule never runs a backlog",
    ("late", "busy"): "its window closed while the worker was still starting other schedules (two at a time)",
    ("late", "fired_already"): "a run for that time exists already",
    ("superseded", "update"): "due and not started when the schedule was saved again",
    ("superseded", "pause"): "due and not started when the schedule was paused",
    ("superseded", "delete"): "due and not started when the schedule was deleted",
    ("zone", "zone"): "the worker's time zone data did not hold its zone, and the worker paused it. A"
    " person saves the schedule with a zone the build holds, then resumes it",
    ("zone", "cron"): "the worker's build could not read its cron, and the worker paused it. A person"
    " saves the schedule again, then resumes it",
    ("mode", None): "it was saved in another auth mode than this worker runs in. A person saves it again"
    " to run it",
    ("setter", None): "who saved it is disabled or no longer an operator (for a token, its accountable"
    " person is disabled). Another operator saves the schedule again to run it",
    ("not_started", None): "the run could not be created ({error_type}); starting one on the Runs screen"
    " shows the cause",
    # Ds target check shows the files and whether they are settled, never a
    # stamp file, an age against the spec's maxAge or a plan's time.
    **{
        ("not_started", code): "the export a file side reads was not ready before its window closed"
        f" ({code}); ds target check shows its state"
        for code in ("export_absent", "unsettled")
    },
    **{
        ("not_started", code): "the export a file side reads was not ready before its window closed"
        f" ({code}); starting a run on the Runs screen shows the cause"
        for code in ("stale", "unstamped", "planning")
    },
    ("error", None): "a fault in DeltaSleuth ({error_type}); the server log names it",
    ("overlap", None): "run {run_id} of it was still live",
    ("approval", None): "its spec version lacked the four-eyes approval it needs",
    ("licence", "expired"): "the licence refused the run (expired)",
    ("licence", "sso"): "the licence refused the run (sso)",
    ("licence", "pairs"): "the licence refused the run (pairs)",
    ("sample", "live"): "a live sampled run samples its population",
    ("sample", "unclosed"): "draw {draw_id} of its population did not pass and is not closed. A person"
    " closes it with the fix, then the next firing draws",
    ("sample", "refused"): "the sample's rules refused the run",
}
# Why the worker paused a schedule before a time of it came due, one closed clause per code.
_ZONE = {
    "zone": "the worker's time zone data did not hold its zone. A person saves the schedule with a zone the"
    " build holds, then resumes it",
    "cron": "the worker's build could not read its cron. A person saves the schedule again, then resumes it",
}


def _n(value: int | None, one: str = "", many: str = "") -> str:
    """A count with separators and its noun, or ``not counted`` for a null fact."""
    if value is None:
        return "not counted"
    return f"{value:,}" + (f" {one if value == 1 else many}" if one else "")


def _who(event: Event) -> str:
    run = event.run or {}
    return run.get("spec_name") or f"spec {run.get('spec_id')}"


def _cycle_text(event: Event, bounds: Mapping[str, str], fired: Fired) -> tuple[str, str]:
    f, run_id = event.facts, event.run["run_id"]  # type: ignore[index]
    c, k = f["cycle"], f["counters"]
    title = f"{_who(event)}: cycle {c} of run {run_id} ended"
    lines = [
        f"{title}: {k['different']:,} of {k['total']:,} keys different, {k['indeterminate']:,} indeterminate,"
        f" {k['cancelled']:,} not read (observed)."
    ]
    new, gone, newly = f["new_root_causes"], f["gone_root_causes"], f["newly_discrepant"]
    if c == 1:
        causes = "root causes (not counted)" if new is None else _n(new, "root cause", "root causes")
        keys = "keys (not counted)" if newly is None else _n(newly, "discrepant key", "discrepant keys")
        lines.append(f"{causes} and {keys} observed in the first cycle.")
    else:
        since = f"since cycle {f['baseline_from']}" if f["baseline_from"] else "in the cycles retained"
        first = f"observed for the first time {since}{' (between samples)' if f['between_samples'] else ''}"
        causes = (
            f"Root causes {first}: not counted"
            if new is None
            else f"{_n(new, 'root cause', 'root causes')} {first}"
        )
        tail = "; key changes are not counted between samples." if f["between_samples"] else "."
        lines.append(f"{causes}; {_n(gone)} no longer observed{tail}")
        if newly is None and not f["between_samples"]:
            lines.append(f"Of the keys read in both cycles {c - 1} and {c}, changes were not counted.")
        elif newly is not None:
            lines.append(
                f"Of the keys read in both cycles {c - 1} and {c}, {newly:,} were observed discrepant in {c}"
                f" and not in {c - 1}; {_n(f['no_longer_discrepant'])} no longer."
            )
    for name in (*fired.crossed, *fired.recovered):
        fact, above, words = LEVELS[name]
        before = f["previous"][fact]
        was = (
            f"was {before['value']} in cycle {before['cycle']}" if before else "not held in an earlier cycle"
        )
        relation = "at least" if name == "unexplained_critical_at_least" else "above" if above else "below"
        again = "no longer " if name in fired.recovered else ""
        lines.append(f"{words} {f['levels'][fact]} ({was}; {again}{relation} {bounds[name]}).")
    return title, " ".join(lines)


def _run_text(event: Event) -> tuple[str, str]:
    f, run_id, who = event.facts, event.run["run_id"], _who(event)  # type: ignore[index]
    match event.kind:
        case "run.paused":
            title = f"{who}: run {run_id} paused"
            refusal = f" ({f['licence_refusal']})" if f["licence_refusal"] else ""
            return title, f"{title}: {_PAUSED[f['reason']].format(refusal=refusal, draw_id=f['draw_id'])}"
        case "sample.draw_failed":
            title = f"{who}: run {run_id} paused"
            return title, (
                f"{title}: its frame refused the draw of cycle {f['cycle']}. A person fixes the frame and"
                " resumes the run, which draws again."
            )
        case "run.stopped":
            title = f"{who}: run {run_id} stopped"
            if event.via == "run.finished":
                return title, (
                    f"{title}: stop condition {f['condition']} met at the end of cycle {f['cycle']}: no"
                    " further cycle starts."
                )
            return title, (
                f"{title}: stop condition {f['condition']} met: keys not started are cancelled; the cycle"
                " ends when keys in flight finish."
            )
        case "run.finished":
            title = f"{who}: run {run_id} finished"
            reason = f" ({f['reason']})" if f["reason"] else ""
            return title, f"{title} {_FINISHED[f['status']]} after cycle {f['cycle']}{reason}."
        case "run.failed":
            title = f"{who}: run {run_id} failed"
            return title, f"{title} in cycle {f['cycle']} ({f['error_type']}); its log names the cause."
        case "schema.drift":
            return _drift_text(event)
        case "accuracy.measured":
            title = f"{who}: labelling {f['labelling_id']} of cycle {f['cycle']} of run {run_id} was labelled"
            return title, (
                f"{title}: {_n(f['labelled'], 'item', 'items')} labelled, {f['undecidable']:,} undecidable,"
                f" {f['not_reviewed']:,} of them not reviewed (observed)."
            )
    title = (
        f"{who}: the sample of cycle {f['cycle']} of run {run_id} was judged {f['status']}"  # sample.judged
    )
    if f["status"] == "unjudged":
        title = f"{who}: the sample of cycle {f['cycle']} of run {run_id} was left unjudged"
    s = f["strata"]
    return title, (
        f"{title} (draw {f['draw_id']}, {f['method']}): {_n(s['passed'], 'stratum', 'strata')} passed,"
        f" {s['failed']:,} failed, {s['unfinished']:,} unfinished (observed)."
    )


def _drift_text(event: Event) -> tuple[str, str]:
    """What the schema guard recorded: a pause, a warning, or a look it could not make. A side is told as
    its counts: the changes that stop the run, and the others."""
    f, run = event.facts, event.run or {}
    lead = f"run {run['run_id']} of {_who(event)} {run['spec_version']} ({run['mode']})"
    if not f["checked"]:
        title = f"{_who(event)}: the schema guard could not check run {run['run_id']}"
        could_not = f"could not check cycle {f['cycle']} ({f['error_type']})"
        return title, f"{lead}: the schema guard {could_not}; the cycle went on unchecked."
    changed = []
    for side, held in f["sides"].items():
        stops, others = held["breaking"], held["other"]
        said = [f"{_n(stops, 'change', 'changes')} that {'stops' if stops == 1 else 'stop'} the run"]
        said = said * bool(stops) + [_n(others, "other", "others")] * bool(stops and others)
        said = said or [_n(others, "change", "changes")]
        if stops or others:
            changed.append((side, ", ".join(said)))
    if f["paused"]:
        title = f"{_who(event)}: run {run['run_id']} was paused by the schema guard"
        sides = " and ".join(f"side {side}'s table changed ({said})" for side, said in changed)
        return title, f"{lead} was paused by the schema guard at cycle {f['cycle']}: {sides}."
    title = f"{_who(event)}: a table of run {run['run_id']} changed"
    if f["where"] == "creation":
        since, goes = f"since run {f['basis']} of its version", "this run reads the tables as they are now"
    elif f["where"] == "read":  # nothing found breaks the plan, so the read's failure fails the run
        since, goes = "since the run was planned", "the run fails on the read that failed"
    else:
        since, goes = "since the run was planned", "the run goes on reading by its plan"
    sides = " and ".join(f"side {side}'s table changed {since} ({said})" for side, said in changed)
    return title, f"{lead}: {sides}; {goes}."


def _other_text(event: Event) -> tuple[str, str]:
    f = event.facts
    match event.kind:
        case "approval.waiting":
            what = (
                f"Spec approval {f['approval_id']} of spec {f['spec_id']}" if f["kind"] == "spec"
                else f"Sign-off {f['approval_id']} of cycle {f['cycle']} of run {f['run_id']}"
            )  # fmt: skip
            left = _n(f["approvals_left"], "more approver", "more approvers")
            return f"{what} waits for {left}", f"{what} waits for {left}."
        case "approval.revoked":
            what = f"{'Spec approval' if f['kind'] == 'spec' else 'Sign-off'} {f['approval_id']} was revoked"
            return what, f"{what}: {_n(f['paused'], 'run', 'runs')} paused, {f['stopped']:,} stopped."
        case "licence.state":
            title = f"the licence is {f['to']}"
            ends = f", which ends on {f['ends']}" if f["ends"] else ""
            named = f"licence {f['licence_id']}{ends}" if f["licence_id"] else "no licence"
            if f["to"] == "open":  # a build without a vendor key
                title, named = "no licence is needed", "this build enforces none"
            return (
                title,
                f"Installation {f['installation_id']}: {title} (was {f['from'] or 'unrecorded'}); {named}.",
            )
        case "readiness.changed":
            who = " / ".join(filter(None, [f["programme"], f["pair"] or "a pair"]))
            if f["from"] is None:
                title = f"{who}: readiness first recorded as {worded(f['to'])}"
            elif f["from"] == f["to"]:
                title = f"{who}: readiness still {worded(f['to'])}"
            else:
                title = f"{who}: readiness {worded(f['from'])} -> {worded(f['to'])}"
            said = [f"{f['score']['passed']} of {f['score']['required']} gates"]
            went = [f"{worded(g['gate'])} {worded(g['to'] or 'not required')}" for g in f["changed"]]
            said += [", ".join(went)] if went else []
            said += ["regressed"] if f["regressed"] else []
            if (at := f["evidence"]) is not None:
                said.append(f"cycle {at['cycle']} of run {at['run_id']}")
            return title, f"{title} ({'; '.join(said)}). Observed; readiness is not a sign-off."
        case "schedule.missed":
            if f["due"] is None:  # no time was missed: the worker's pause alone
                title = f"schedule {f['schedule_id']} is paused by the worker"
                return title, f"{title} before a time of it came due: {_ZONE[f['code']]}."
            title = f"schedule {f['schedule_id']} did not start a run"
            when = f["due"]  # one time, or times that could not be counted
            if f["reason"] in ("late", "superseded") and (f["count"] or 1) > 1:
                when = f"{f['count']:,} times, {f['due']} to {f['to']}"
            elif f["count"] is not None and f["reason"] not in ("late", "superseded"):  # a standing refusal's
                when = f"{_n(f['count'], 'more time', 'more times')} since {f['due']}, for the same reason"
            why = _MISSED[f["reason"], f["code"]].format(**f)
            return title, f"{title} for {when}: {why}."
        case "notify.dead":
            title = f"notifications to {f['channel']} are failing"
            return title, (
                f"Notification {f['delivery_id']} ({f['event']}) to {f['channel']} was not delivered after"
                f" {_n(f['attempts'], 'attempt', 'attempts')} ({f['error']}); {f['dead_since_last']:,} more"
                " since the last such notice."
            )
        case "notify.skipped":
            title = "audit rows were not notified"
            if f["reason"] == "stale":
                return title, (
                    f"Audit rows {f['from_seq']} to {f['to_seq']} were not notified: they were older than the"
                    " notifier's max age."
                )
            rows = ", ".join(map(str, f["seqs"] or []))
            return (
                title,
                f"{_n(f['rows'], 'audit row was', 'audit rows were')} not notified ({f['reason']}): {rows}.",
            )
        case "notify.daily":
            runs = f["runs"]
            return "daily summary", (
                f"Daily summary: {runs.get('running', 0):,} runs running, {runs.get('paused', 0):,} paused;"
                f" {f['cycles_ended_24h']:,} cycles ended and {f['runs_finished_24h']:,} runs finished in"
                " 24 h;"
                f" {f['approvals_waiting']:,} approvals waiting; licence {f['licence'] or 'unrecorded'}; the"
                f" notifier is {f['feed_lag_s']:,} s behind; {f['mutes']:,} mutes active."
            )
    title = f"a test of channel {f['channel']}"
    return title, f"This is {title}: the channel delivers."


def text(
    event: Event, *, label: str, installation: str, seq: int | None, bounds: Mapping[str, str], fired: Fired
) -> tuple[str, str]:
    """The title and text of ``event`` (checked, its run block's name withheld as sent): closed templates,
    counts with separators, never a raw value; the footer says what it is not."""
    if event.kind == "cycle.ended":
        title, body = _cycle_text(event, bounds, fired)
    elif event.run is not None:
        title, body = _run_text(event)
    else:
        title, body = _other_text(event)
    body = f"[{label}] {body} Observed by DeltaSleuth (installation {installation}); not evidence and not a"
    body += " sign-off."
    if seq is not None:
        body += f" Audit row {seq}."
    if event.run is not None:
        if event.run["now"]["approval"] in ("unapproved", "revoked"):
            body += f" {NOT_EVIDENCE}"
        if event.run["now"]["licence"] == "evaluation":  # an unlicensed evaluation's label
            body = f"[unlicensed evaluation] {body}"
    return title, body


# -- payloads and bodies -------------------------------------------------------------


def _route(event: Event) -> str:
    """The hash route of the screen showing the event's subject."""
    f = event.facts
    if event.run is not None:
        cycle = f"?cycle={f['cycle']}" if f.get("cycle") else ""
        return f"#/runs/{event.run['run_id']}{cycle}"
    if event.kind.startswith("approval."):
        return f"#/approvals/{f['approval_id']}"
    if event.kind == "readiness.changed":  # a withheld pair's link names none
        return f"#/readiness/{f['pair']}" if f["pair"] else "#/readiness"
    if event.kind == "schedule.missed":
        return f"#/schedules/{f['schedule_id']}"
    return "#/licence" if event.kind == "licence.state" else "#/notifications"


def payload(
    event: Event,
    channel: ChannelSettings,
    *,
    delivery_id: str,
    event_id: str,
    occurred_at: datetime,
    installation: Mapping[str, str],
    audit: Mapping[str, Any] | None,
    fired: Mapping[str, Fired],
    bounds: Mapping[str, str],
    suppressed: int,
    public_url: str | None,
) -> dict[str, Any]:
    """The ``deltasleuth-notify/1`` document of ``event`` (checked) for ``channel``: the rules that fired
    for it (``fired``, by name) and the bounds of their level conditions."""
    withheld: dict[str, str] = {}
    run, facts = event.run, event.facts
    if run is not None and (why := "labels_off" if not channel.labels else event.unlabelled):
        run, withheld["spec_name"] = run | {"spec_name": None}, why
    if event.kind == "readiness.changed":  # its labels: the pair, its programme and its entity type
        for name in ("pair", "programme", "entity"):
            why = "labels_off" if not channel.labels else event.unlabelled if name == "pair" else None
            if why and facts[name] is not None:
                facts, withheld[name] = facts | {name: None}, why
    shown = replace(event, run=run, facts=facts)
    link = None if public_url is None else public_url + "/" + _route(shown)
    if link is not None and not channel.links:
        link, withheld["link"] = None, "links_off"
    union = Fired(*(tuple(dict.fromkeys(x for f in fired.values() for x in getattr(f, part)))
                    for part in ("held", "crossed", "recovered")))  # fmt: skip
    seq = audit and audit["seq"]
    title, words = text(shown, label=installation["label"], installation=installation["id"], seq=seq,
                        bounds=bounds, fired=union)  # fmt: skip
    if suppressed:
        count = _n(suppressed, "notification to this channel was", "notifications to this channel were")
        words += f" ({count} held back since the last one: see Notifications)"
    return {
        "format": FORMAT, "id": delivery_id, "event": event.kind, "event_id": event_id,
        "severity": severity(event, bool(union.held or union.crossed)),
        "occurred_at": occurred_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "installation": dict(installation),
        "audit": audit and {"seq": audit["seq"], "hash": audit["hash"]}, "rules": list(fired),
        "crossed": list(union.crossed), "recovered": list(union.recovered), "run": run, "facts": facts,
        "suppressed_since_last": suppressed, "withheld": withheld, "claim": "observed", "link": link,
        "title": title, "text": words,
    }  # fmt: skip


def _fields(doc: Mapping[str, Any], limit: int) -> list[tuple[str, str]]:
    """Up to ``limit`` of the facts as (name, text): a cycle's counts first, then the scalar facts."""
    facts = doc["facts"]
    pairs = [(name, facts["counters"][name]) for name in ("different", "indeterminate", "total")
             if "counters" in facts]  # fmt: skip
    pairs += [(name, value) for name, value in facts.items() if isinstance(value, int | str)]
    return [(name, str(value)) for name, value in pairs if not isinstance(value, bool)][:limit]


def _slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def body(kind: str, doc: Mapping[str, Any], *, at: datetime) -> str:
    """What a delivery of ``doc`` to a channel of ``kind`` sends, rendered once: the webhook's JSON document,
    Slack's or Teams' message, or an e-mail made at ``at`` without From and To, which each send adds."""
    title, words, link = doc["title"], doc["text"], doc["link"]
    if kind == "webhook":
        return jsonio.dumps(doc)
    if kind == "slack":
        section = _slack(words) + (f" <{link}|Open in DeltaSleuth>" if link else "")
        context = f"run {doc['run']['run_id']} · " if doc["run"] else ""
        context += f"audit row {doc['audit']['seq']} · " if doc["audit"] else ""
        fields = [{"type": "mrkdwn", "text": f"*{name}*\n{_slack(value)}"} for name, value in _fields(doc, 8)]
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": section}, "fields": fields},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{context}{doc['id']} · observed"}]},
        ]
        return jsonio.dumps(
            {"text": _slack(words), "blocks": blocks, "unfurl_links": False, "unfurl_media": False}
        )
    if kind == "teams":
        card: dict[str, Any] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
                {"type": "TextBlock", "text": words, "wrap": True},
                {"type": "FactSet", "facts": [{"title": n, "value": v} for n, v in _fields(doc, 10)]},
            ],
        }  # fmt: skip
        if link:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in DeltaSleuth", "url": link}]
        attachment = {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }
        return jsonio.dumps({"type": "message", "attachments": [attachment]})
    message = EmailMessage(policy=SMTP)  # CR or LF in a header raises
    message["Subject"] = (
        f"[DeltaSleuth {doc['installation']['label']}] {doc['severity'].capitalize()}: {title}"
    )
    message["Date"] = format_datetime(at)
    message["Message-ID"] = f"<{doc['id']}@{doc['installation']['id']}.deltasleuth.invalid>"
    message["Auto-Submitted"] = "auto-generated"
    message["X-DeltaSleuth-Event"] = doc["event"]
    message.set_content(words + (f"\n\n{link}\n" if link else "\n"))
    return message.as_string()
