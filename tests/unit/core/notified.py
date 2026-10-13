"""Events for the notifier's pure tests: audit rows, what the feed read for them, channels and bodies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter

from deltasleuth.config import ChannelSettings
from deltasleuth.core import notify
from deltasleuth.core.notify import Event, Fired, Seen

RUN = "R-" + "0" * 25 + "1"
APPROVAL = "A-" + "0" * 25 + "2"
DRAW = "D-" + "0" * 25 + "3"
DELIVERY = "N-" + "0" * 25 + "4"
INSTALLATION = "I-3f0a1b2c3d4e5f60"
AT = datetime(2026, 10, 3, 4, 0, 12, tzinfo=UTC)
COUNTERS = {
    "total": 1_000_000,
    "compared": 999_985,
    "match": 999_973,
    "different": 12,
    "indeterminate": 3,
    "cancelled": 0,
    "outstanding": 0,
    "mismatch_rate": "0.0021",
    "coverage": "0.999985",
    "absent_both": 1,
    "by_outcome": {"MATCH": 999_973},
    "reconciliation": [],
    "cycle": 12,
}
URL = "https://events.bank.example/deltasleuth"
PACK = "P-" + "0" * 25 + "5"
PAIR = "customer-profile"
SCHEDULE = "SC-" + "0" * 25 + "6"
# A schedule's schedule.missed row as the worker writes one: a due time whose
# window closed while the run the schedule started before was still live.
MISSED = {
    "reason": "overlap", "code": None, "due": "2026-10-07T01:00Z", "to": None, "count": None,
    "spec_name": PAIR, "mode": "batch", "spec_id": 12, "run_id": RUN, "draw_id": None, "error_type": None,
    "revision": 4, "time_zone": "Europe/London", "next": "2026-10-08T01:00Z",
}  # fmt: skip
# The worker's schedule.pause row of a schedule whose zone its build did not hold, paused before a time of
# it came due: no schedule.missed row is beside it.
PAUSED = {
    "revision": 4, "next_due": None, "reason": "zone", "code": "zone", "tzdata": "2026c", "missed": False,
    "spec_name": PAIR, "mode": "batch",
}  # fmt: skip
# A pair's readiness.changed row as the worker writes it: targets and convergence
# met, so the pair is parity-ready in tokens mode.
CHANGED = {
    "pair": PAIR, "programme": "cutover-q4", "entity": "customer", "from": "not_ready", "to": "parity_ready",
    "was_regressed": False, "regressed": False, "score": {"passed": 5, "required": 5}, "causes": ["cycle"],
    "gates": {
        "population": "met", "critical": "met", "targets": "met", "convergence": "met", "fresh": "met",
        "spec_approval": "unavailable", "adjudication": "unavailable", "sample_ledger": "not_applicable",
        "signoff": "unavailable", "pack": "unavailable",
    },
    "changed": [
        {"gate": "targets", "from": "not_met", "to": "met", "value": 12, "allowed": 999, "over": 0},
        {"gate": "convergence", "from": "not_met", "to": "met", "value": 3, "allowed": 3, "over": None},
    ],
    "evidence": {"run_id": RUN, "cycle": 14}, "candidate": {"run_id": RUN, "cycle": 14}, "signoff_id": None,
    "pack_id": None, "config_sha256": "c" * 64,
}  # fmt: skip


# A run's schema.drift row as the schema guard writes it when a look at a cycle's start pauses the run: side
# b's table lost a compared column and gained two; the paused row written before it is audit row 81233.
BASIS = "R-" + "0" * 25 + "7"
DRIFT = {
    "cycle": 7, "where": "cycle", "mode": "finding", "checked": True, "paused": True, "paused_seq": 81233,
    "basis": BASIS, "error_type": None,
    "sides": {
        "a": {"breaking": 0, "other": 0, "kinds": {}, "from": "1" * 64, "to": "1" * 64},
        "b": {"breaking": 1, "other": 2, "kinds": {"column_missing": 1, "column_added": 2}, "from": "2" * 64,
              "to": "3" * 64},
    },
}  # fmt: skip
# The paused row the guard writes before it, in the same transaction: the reason, and the two shapes seen.
GUARD_PAUSE = {"reason": "run_paused_schema_drift", "schema": {"a": "1" * 64, "b": "3" * 64}}


def row(action: str, detail: dict[str, Any], *, obj: str = RUN, seq: int = 81234) -> dict[str, Any]:
    return {"seq": seq, "action": action, "object": obj, "detail": detail, "at": AT, "hash": "a" * 64}


def block(
    *,
    cycle: int = 12,
    status: str = "running",
    mode: str = "soak",
    approval: str = "approved",
    licence: str | None = "valid",
    name: str | None = "customer-profile",
) -> dict[str, Any]:
    return {
        "run_id": RUN,
        "spec_id": 7,
        "spec_name": name,
        "spec_version": "1.0.0",
        "mode": mode,
        "now": {"cycle": cycle, "status": status, "licence": licence, "approval": approval},
    }


def seen(
    *,
    cycle: int = 12,
    counters: dict[str, Any] | None = None,
    sample: dict[str, Any] | None = None,
    draw: bool = False,
    cancelling: bool = False,
    **run: Any,
) -> Seen:
    ended = {"counters": COUNTERS if counters is None else counters, "sample": sample, "purging_at": None}
    found = block(**run)
    drawn = {"draw_id": DRAW, "method": "bottom_k"} if draw else None
    return Seen(found, found["spec_name"], found["mode"], None, cycle, ended, drawn, cancelling)


def digest(**facts: Any) -> dict[str, Any]:
    """A cycle's facts as the feed completes them, ``facts`` replacing some."""
    base = {
        "cycle": 12,
        "counters": {name: COUNTERS[name] for name in notify.COUNTERS},
        "levels": {
            "mismatch_rate": "0.0021",
            "indeterminate_rate": "0.000003",
            "coverage": "0.999985",
            "unexplained_critical": 0,
        },
        "previous": {
            "mismatch_rate": {"cycle": 11, "value": "0.0009"},
            "indeterminate_rate": None,
            "coverage": {"cycle": 11, "value": "0.999"},
            "unexplained_critical": {"cycle": 11, "value": 0},
        },
        "new_root_causes": 2,
        "gone_root_causes": 1,
        "baseline_from": 4,
        "newly_discrepant": 41,
        "no_longer_discrepant": 17,
        "between_samples": False,
        "unavailable": [],
    }
    return base | facts


def ended(**facts: Any) -> Event:
    return notify.checked(Event("cycle.ended", digest(**facts), block(), "customer-profile", "soak"))


def readiness(unlabelled: str | None = None, pair: str = PAIR, **detail: Any) -> Event:
    """The event of a pair's ``readiness.changed`` row (``CHANGED`` with ``detail``), checked;
    ``unlabelled``: why the feed found its name is never sent."""
    found = Seen(spec=pair, unlabelled=unlabelled)
    (event,) = notify.EVENTS["readiness.changed"][0](
        row("readiness.changed", CHANGED | detail, obj=f"readiness:{pair}"), found
    )
    return notify.checked(event)


def missed(obj: str = SCHEDULE, **detail: Any) -> Event:
    """The event of a schedule's ``schedule.missed`` row (``MISSED`` with ``detail``), checked."""
    (event,) = notify.EVENTS["schedule.missed"][0](row("schedule.missed", MISSED | detail, obj=obj), Seen())
    return notify.checked(event)


def paused(**detail: Any) -> list[Event]:
    """The events of a schedule's ``schedule.pause`` row (``PAUSED`` with ``detail``), each checked."""
    found = notify.EVENTS["schedule.pause"][0](row("schedule.pause", PAUSED | detail, obj=SCHEDULE), Seen())
    return [notify.checked(event) for event in found]


def channel(kind: str = "webhook", **options: Any) -> ChannelSettings:
    shapes = {
        "webhook": {"url": URL, "signing_key_refs": ["file:/srv/hook.key"]},
        "slack": {"url_ref": "file:/srv/slack.url", "host": "hooks.slack.com"},
        "teams": {"url_ref": "file:/srv/teams.url", "host": "prod-00.eastus.logic.azure.com"},
        "email": {
            "smtp_host": "smtp.bank.example",
            "sender": "ds@bank.example",
            "recipients": ["o@bank.example"],
        },
    }
    return TypeAdapter(ChannelSettings).validate_python({"kind": kind} | shapes[kind] | options)


def rendered(
    event: Event,
    kind: str = "webhook",
    *,
    fired: dict[str, Fired] | None = None,
    bounds: dict[str, str] | None = None,
    public_url: str | None = "https://ds.bank.example",
    suppressed: int = 0,
    **options: Any,
) -> tuple[dict[str, Any], str]:
    """The document and the body of ``event`` (checked) for a channel of ``kind``."""
    doc = notify.payload(
        event,
        channel(kind, **options),
        delivery_id=DELIVERY,
        event_id=f"seq:81234:{event.kind}",
        occurred_at=AT,
        installation={"id": INSTALLATION, "label": "prod"},
        audit={"seq": 81234, "hash": "a" * 64},
        fired=fired or {"rule": Fired()},
        bounds=bounds or {},
        suppressed=suppressed,
        public_url=public_url,
    )
    return doc, notify.body(kind, doc, at=AT)


def drifted(found: Seen | None = None, **detail: Any) -> Event:
    """The event of a run's ``schema.drift`` row (``DRIFT`` with ``detail``), checked: of a soak of
    customer-profile, as the feed found it at cycle 7."""
    found = found or seen(cycle=7)
    (event,) = notify.EVENTS["schema.drift"][0](row("schema.drift", DRIFT | detail), found)
    return notify.checked(event)
