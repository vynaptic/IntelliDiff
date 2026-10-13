"""Measured cycles and stored governance facts for readiness's pure tests: a pair's
points, its series as ``repo.readiness_series`` reads them, and what ``repo.readiness_governance`` gives."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from deltasleuth.core import readiness
from deltasleuth.core.readiness import Installation, Policy
from deltasleuth.reports import accuracy
from deltasleuth.runner.stop import StopConditions

PAIR = "customer-profile"
RUN, OTHER = "R-" + "0" * 25 + "1", "R-" + "0" * 25 + "2"
APPROVAL, SIGNOFF, PACK = "A-" + "0" * 25 + "3", "A-" + "0" * 25 + "4", "P-" + "0" * 25 + "5"
DRAW, ADJUDICATION, LABELLING = "D-" + "0" * 25 + "6", "J-" + "0" * 25 + "7", "V-" + "0" * 25 + "8"
T0 = datetime(2026, 10, 1, tzinfo=UTC)
READ = "read to its end in 1 snapshot at 2026-10-03 04:00 UTC"
LISTED = "is not a census: its keys were given with the run and no side is a table to check them against"
# What a soak's cycle over two tables read whole measures: 12 keys different of 1,000,000.
MEASURES: dict[str, Any] = {
    "pair": PAIR, "spec_id": 12, "version": "1.4.0", "mode": "soak", "keys_source": "table_scan",
    "needs_approval": ["clear"], "basis_text": READ, "total": 1_000_000, "compared": 1_000_000,
    "different": 12, "indeterminate": 0, "cancelled": 0, "outstanding": 0, "absent_both": 0,
    "api_uncomparable": 0, "suppressed": 0, "unexplained_critical": 0, "results": 60, "draw_id": None,
    "sample_status": None, "sample_strata": None, "selection_ok": None, "changes": None, "graph": None,
    "late": False, "measured_at": "2026-10-03T04:00:10.000000Z",
}  # fmt: skip
SAMPLED = {
    "keys_source": "hash_sample", "basis_text": None, "total": 6_022, "compared": 6_022, "different": 0,
    "draw_id": DRAW, "sample_status": "passed", "selection_ok": True,
    "sample_strata": [{"label": "retail", "tier": "standard"}, {"label": "flagged=Y", "tier": "high"}],
}  # fmt: skip
ALLOWS = {"runs": True, "sso": True, "signoff": True, "packs": True}
POLICY = readiness.policy(None, PAIR)


def seen(mode: str = "oidc", packs: bool = True, allows: Mapping[str, bool] | None = ALLOWS) -> Installation:
    """An installation in ``mode``, with or without ``packs``, whose licence ``allows`` (None: unreadable)."""
    return Installation(mode, packs, allows, accuracy.gate, StopConditions.from_spec)


def measures(**changes: Any) -> dict[str, Any]:
    """A cycle's measures: ``MEASURES`` with ``changes``, of which ``own``, ``problem``, ``method``,
    ``whole`` and ``cut`` are what ``readiness.measured`` derives from (a scan read whole, by default; a
    census problem is the basis text, as the worker stores it)."""
    names = ("own", "problem", "method", "whole", "cut")
    derived = {name: changes.pop(name) for name in names if name in changes}
    scanned = (MEASURES | changes)["keys_source"] == "table_scan"
    facts = {"own": True, "problem": None, "whole": True if scanned else None} | derived
    if facts["problem"] is not None:
        changes.setdefault("basis_text", facts["problem"])
    return readiness.measured(MEASURES | changes, **facts)


def point(cycle: int, *, run: str = RUN, at: datetime | None = None, **changes: Any) -> dict[str, Any]:
    """The run's ``cycle``, ended ``cycle`` hours after ``T0`` (or ``at``), with its ``measures``; ``sample``
    is its stored judgement."""
    sample = changes.pop("sample", None)
    ended_at = at or T0 + timedelta(hours=cycle)
    return {
        "run_id": run,
        "cycle": cycle,
        "ended_at": ended_at,
        "readiness": measures(**changes),
        "sample": sample,
    }


def errored(cycle: int, error: str = "KeyError", *, run: str = RUN, final: bool = False) -> dict[str, Any]:
    """A cycle whose measure failed (``final``: for good, at its fifth attempt)."""
    record = {
        "format": 1, "pair": PAIR, "spec_id": 12, "error": error, "attempts": 5 if final else 1,
        "fails": {"standard": ["unknown"], "high": ["unknown"]}, "measured_at": MEASURES["measured_at"],
    } | ({"final": True} if final else {})  # fmt: skip
    return {"run_id": run, "cycle": cycle, "ended_at": T0 + timedelta(hours=cycle), "readiness": record}


def _when(row: Mapping[str, Any]) -> tuple[datetime, str, int]:
    return row["ended_at"], row["run_id"], row["cycle"]


def series(points: Sequence[Mapping[str, Any]], policy: Policy = POLICY) -> dict[str, Any]:
    """The pair's points as ``repo.readiness_series`` reads them at the policy's tier, K and window."""
    tier, rows = policy.gates.tier, sorted(points, key=_when, reverse=True)
    read = [row for row in rows if row["readiness"]["format"] == 1]  # another format is an unknown point
    evidence = [row for row in read if row["readiness"].get("evidence")]
    newest: dict[int, Mapping[str, Any]] = {}
    for row in reversed(evidence):
        newest[row["readiness"]["spec_id"]] = row
    versions = sorted(newest.values(), key=_when, reverse=True)
    breaks = [row for row in rows if row not in read or row["readiness"]["fails"][tier]]
    breaks = breaks[: policy.gates.cycles]
    failed = next(
        (row for row in breaks if row in read and set(row["readiness"]["fails"][tier]) - {"unknown"}), None
    )
    found: dict[str, Any] = {
        "versions": versions, "breaks": breaks, "points": [], "trend": evidence[:10], "critical": None,
        "targets": None, "newest": None, "cycles": 0,
    }  # fmt: skip
    if not versions:
        return found | {"newest": rows[0] if rows else None, "cycles": len(rows)}
    current, spec_id = versions[0], versions[0]["readiness"]["spec_id"]
    after = max((row["ended_at"] for row in (failed, *versions[1:2]) if row is not None), default=None)
    found["points"] = [
        row
        for row in evidence
        if row["readiness"]["spec_id"] == spec_id and (after is None or row["ended_at"] > after)
    ][: readiness.window(policy)]
    for gate in ("critical", "targets"):
        found[gate] = next(
            (
                row
                for row in read
                if row["readiness"]["spec_id"] == spec_id
                and row["ended_at"] > current["ended_at"]
                and gate in row["readiness"]["fails"][tier]
            ),
            None,
        )
    return found


def cycle(row: Mapping[str, Any], **facts: Any) -> dict[str, Any]:
    """A candidate cycle as governance reads it: started under SSO, under an approved spec approval, its
    adjudication passed, with no labelling or pack; ``facts`` replace members."""
    return {
        "run_id": row["run_id"], "cycle": row["cycle"], "ended_at": row["ended_at"],
        "spec_id": row["readiness"]["spec_id"], "readiness": row["readiness"], "sample": row.get("sample"),
        "started": "oidc", "worked": False, "cited": {"approval_id": APPROVAL, "status": "approved"},
        "adjudication": passed(), "labelling": None, "packs": [],
    } | facts  # fmt: skip


def signoff(row: Mapping[str, Any], status: str = "approved", approval_id: str = SIGNOFF) -> dict[str, Any]:
    """A sign-off of the cycle, decided (and, if so, revoked) two days after ``T0``; of a cycle that is no
    evidence point, with its measures."""
    decided, evidence = T0 + timedelta(days=2), bool(row["readiness"].get("evidence"))
    return {
        "approval_id": approval_id, "status": status, "run_id": row["run_id"], "cycle": row["cycle"],
        "spec_id": row["readiness"]["spec_id"], "version": row["readiness"]["version"],
        "ended_at": row["ended_at"], "decided_at": None if status == "requested" else decided,
        "revoked_at": decided if status == "revoked" else None, "evidence": evidence,
        "readiness": None if evidence else row["readiness"],
    }  # fmt: skip


def pack(status: str = "issued") -> dict[str, Any]:
    at = T0 + timedelta(days=1)
    return {
        "pack_id": PACK, "signoff_id": SIGNOFF, "status": status, "issued_at": at,
        "revoked_at": at + timedelta(days=2) if status == "revoked" else None,
    }  # fmt: skip


def passed(adjudication_id: str = ADJUDICATION) -> dict[str, Any]:
    return {"adjudication_id": adjudication_id, "passed": True, "reasons": []}


def judged(
    points: Sequence[Mapping[str, Any]],
    *,
    policy: Policy = POLICY,
    cycles: Sequence[Mapping[str, Any]] | None = None,
    signoffs: Sequence[Mapping[str, Any]] = (),
    soaks: Sequence[Mapping[str, Any]] = (),
    installation: Installation | None = None,
    now: datetime | None = None,
    **more: Any,
) -> dict[str, Any]:
    """The pair evaluated over its ``points``, an hour after the newest ended (or at ``now``): governance
    holds the evidence cycle (or ``cycles``), the ``signoffs`` and the live ``soaks``."""
    found = series(points, policy)
    if cycles is None:
        cycles = [cycle(row) for row in found["versions"][:1]]
    governance = {"signoffs": list(signoffs), "cycles": list(cycles), "soaks": list(soaks)}
    now = now or max((row["ended_at"] for row in points), default=T0) + timedelta(hours=1)
    return readiness.evaluate(PAIR, policy, found, governance, installation or seen(), now=now, **more)


def gate(item: Mapping[str, Any], name: str) -> dict[str, Any]:
    return next(found for found in item["gates"] if found["gate"] == name)


def said(item: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Each gate's status and text."""
    return {found["gate"]: (found["status"], found["text"]) for found in item["gates"]}
