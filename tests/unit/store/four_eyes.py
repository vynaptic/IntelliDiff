"""The people, uploads, runs and ended cycles the approval tests share, written as the app writes them,
sampled cycles among them and an ended cycle's labellings."""

from __future__ import annotations

import copy
import os
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from types import EllipsisType, SimpleNamespace
from typing import Any

from deltasleuth import approvals
from deltasleuth.auth.principal import WORKER, Principal, expand
from deltasleuth.connectors.base import utc_now_iso
from deltasleuth.core import sampling
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.core.types import (
    CompareConfig,
    CompiledSpec,
    Confirmation,
    KeyConfig,
    KeyResult,
    Outcome,
    SideState,
    SideStateKind,
)
from deltasleuth.reports import accuracy
from deltasleuth.runner import sample
from deltasleuth.runner.counters import count_cycle
from deltasleuth.store import repo
from tests.conftest import TEST_DATA_KEY, TEST_SEALER

KEYS = ("k1", "k2", "k3")  # every run's keys; k shows as m-k
COMPLETE = SideState(SideStateKind.COMPLETE)


def mode(auth_mode: str) -> Any:
    """A context as ``approvals.gate`` reads it: only the auth mode."""
    return SimpleNamespace(settings=SimpleNamespace(auth_mode=auth_mode))


OIDC = mode("oidc")


async def person(conn: repo.Conn, name: str, role: str = "rule_approver") -> Principal:
    """A person signed in through the IdP whose stored roles are ``role`` and those below it."""
    roles = expand([role])
    row = await repo.upsert_user(conn, "https://idp.test", name, name, None, roles, signed_in=True)
    return Principal(f"user:{row['user_id']}", name, roles)


async def token(
    conn: repo.Conn, name: str, created_by: Principal, owner: Principal | None = None
) -> Principal:
    """A spec_author API token that ``created_by`` created, ``owner`` being its accountable person."""
    token_id = "T-" + secrets.token_hex(8)
    owner_user_id = None if owner is None else owner.actor.removeprefix("user:")
    await repo.create_token(
        conn, token_id=token_id, name=name, owner="team", owner_user_id=owner_user_id, role="spec_author",
        secret_hash=os.urandom(32), created_by=created_by.actor, days=30,
    )  # fmt: skip
    return Principal(f"token:{token_id}", name, expand(["spec_author"]))


def audit_as(conn: repo.Conn) -> repo.Audit:
    """``audit`` as ``repo.audited`` gives it, on ``conn``."""
    return partial(repo.insert_audit, conn)


def spec(version: str = "1.0.0", name: str = "customer-profile", **members: Any) -> CompiledSpec:
    """A version of ``name``, its content hash its body's; ``members`` are CompiledSpec fields."""
    raw = {"metadata": {"name": name, "version": version, "owner": "payments"}}
    members = {"sides": {}, "compare": CompareConfig()} | members
    return CompiledSpec(
        name=name, version=version, content_hash=content_hash(raw), normalizer_version="1.1.0",
        key=KeyConfig(name="id"), raw=raw, **members,
    )  # fmt: skip


async def upload(conn: repo.Conn, version: CompiledSpec, who: Principal | str | None) -> int:
    """Store the version as ``POST /v1/specs`` does, audited ``spec.create`` by ``who``; a bare actor
    (``local:…``, pre-005 None) is stored as the creator with no audit row."""
    actor = who.actor if isinstance(who, Principal) else who
    spec_id = await repo.save_spec(conn, version, created_by=actor)  # type: ignore[arg-type]
    if isinstance(who, Principal):
        await repo.insert_audit(
            conn, who, "spec.create", str(spec_id), {"content_hash": version.content_hash}
        )
    return spec_id


async def request(conn: repo.Conn, spec_id: int, who: Principal, scopes: Sequence[str] = ()) -> str:
    row = await repo.create_approval(conn, "spec", spec_id, who.actor, scopes=scopes)
    assert row is not None
    return row["approval_id"]


async def approve(conn: repo.Conn, approval_id: str, *deciders: Principal) -> repo.Row:
    """Each decider approves in turn: with two, the first is the first of two approvers."""
    for decider in deciders[:-1]:
        assert await repo.decide_approval(conn, approval_id, decider.actor, approve=True, first=True)
    row = await repo.decide_approval(conn, approval_id, deciders[-1].actor, approve=True)
    assert row is not None
    return row


async def approved(
    conn: repo.Conn, spec_id: int, requester: Principal, *deciders: Principal, scopes: Sequence[str] = ()
) -> str:
    approval_id = await request(conn, spec_id, requester, scopes)
    await approve(conn, approval_id, *deciders)
    return approval_id


def counters(cycle: int, *, matched: int = 2, owed: int = 0, cancelled: int = 0) -> dict[str, Any]:
    """A cycle's counters as ``summarise`` saves them: ``matched`` keys, ``owed`` a recheck and
    ``cancelled``."""
    results = [
        {"key_masked": f"m{i}", "outcome": outcome, "first_outcome": outcome, "confirmation": confirmation,
         "inconclusive_reason": None, "drift_reason": None}
        for i, (outcome, confirmation) in enumerate(
            [("MATCH", "rechecked")] * matched + [("MISMATCH", "provisional")] * owed
        )
    ]  # fmt: skip
    due = [Outcome.MISMATCH]  # a provisional mismatch is owed a recheck
    counted = count_cycle(results, {}, total=len(results) + cancelled, live=True, recheck_outcomes=due)
    return counted | {"cycle": cycle}


async def start(
    conn: repo.Conn,
    spec_id: int,
    who: Principal | str,
    *,
    approval_id: str | None = None,
    mode: str = "batch",
    status: str = "running",
    params: Mapping[str, Any] | None = None,
    keys_cut: Mapping[str, int] | None = None,
) -> str:
    """A run of the spec by ``who`` over ``KEYS``, started under SSO (``params["approval"]``, which
    ``params`` may replace) and ``approval_id``."""
    record = {"needs": ["clear"], "mode": "oidc"}
    run_id = repo.new_run_id()
    await repo.create_run(
        conn, spec_id, mode, TEST_SEALER.seal_keys({k: f"m-{k}" for k in KEYS}),
        {"keys_source": "explicit", "samples_dir": None, "approval": record} | dict(params or {}),
        run_id=run_id, created_by=who.actor if isinstance(who, Principal) else who,
        retention="standard", data_key=TEST_DATA_KEY, approval_id=approval_id, keys_cut=keys_cut,
    )  # fmt: skip
    if status != "queued":
        await repo.set_run_status(conn, run_id, status)
    return run_id


async def end(conn: repo.Conn, run_id: str, cycle_counters: Mapping[str, Any] | None = None) -> None:
    """End the run's current cycle, with ``cycle_counters`` saved first (None: as a failed cycle, none)."""
    if cycle_counters is not None:
        await repo.set_counters(conn, run_id, cycle_counters)
    await repo.end_cycle(conn, run_id)


async def save(conn: repo.Conn, run_id: str, key: str, *findings: Mapping[str, Any], cycle: int = 1) -> None:
    """``key``'s result in the cycle: MISMATCH with ``findings`` (``finding``'s rows), else MATCH."""
    outcome = Outcome.MISMATCH if findings else Outcome.MATCH
    result = KeyResult(key, outcome, COMPLETE, COMPLETE, confirmation=Confirmation.RECHECKED)
    await repo.save_key_result(conn, run_id, cycle, f"m-{key}", result, findings)


def finding(severity: str = "CRITICAL", rule: str | None = None, classification: str | None = None) -> dict:
    """A masked finding row at ``/x``, suppressed when a ``rule`` of ``classification`` matched it."""
    return {
        "taxonomy_code": "VALUE_MISMATCH", "severity": severity, "change_type": "value_changed",
        "direction": "both", "path_pattern": "$.x", "json_pointer": "/x", "value_class": None,
        "member_key_tok": None, "signature_hash": None, "rule_id": rule,
        "rule_classification": classification, "suppressed": rule is not None, "value_a_tok": "a",
        "value_b_tok": "b",
    }  # fmt: skip


async def request_signoff(
    conn: repo.Conn, run_id: str, cycle: int, who: Principal, version: CompiledSpec
) -> str:
    """A sign-off request of the ended cycle, attesting what ``approvals.attests`` builds."""
    run = await repo.get_run(conn, run_id)
    assert run is not None
    facts = await approvals.attests(conn, run, cycle, version)
    row = await repo.create_approval(
        conn, "signoff", run["spec_id"], who.actor, run_id=run_id, cycle=cycle, attests=facts,
        attests_sha256=approvals.attests_sha256(facts),
    )  # fmt: skip
    assert row is not None
    return row["approval_id"]


# -- sampled cycles -------------------------------------------------

FRAME = [(f"C{n:03d}", "R" if n <= 3 else "B") for n in range(1, 7)]  # three retail customers, three others
_SAMPLED = {
    "apiVersion": "deltasleuth/v1",
    "kind": "ComparisonSpec",
    "metadata": {"name": "customer-profile", "version": "1.0.0", "owner": "payments"},
    "normalizerVersion": "1.1.0",
    "key": {"name": "id", "type": "string", "pattern": "^C[0-9]{3}$", "class": "business_code"},
    "sides": {
        "a": {"role": "legacy", "target": "db-a", "table": {"name": "customers", "keyColumns": ["id"]}},
        "b": {"role": "new", "target": "svc-b", "request": {"method": "GET", "path": "/b/{{ key }}"},
              "select": "$", "notFound": {"status": [404]}},
    },
    "compare": {},
    "recheck": {"delay": "0s", "times": 1},
    "security": {"unlisted": "tokenize"},
}  # fmt: skip
_SEGMENTS = [{"name": "segment", "column": "segment", "buckets": [
    {"label": "retail", "values": ["R"], "targetSize": 3}, {"label": "other", "targetSize": 3}]}]  # fmt: skip


def sampled(
    name: str = "customer-profile",
    version: str = "1.0.0",
    *,
    method: str = "bottom_k",
    key: str = "business_code",
    size: int = 3,
) -> CompiledSpec:
    """A version of ``name`` drawing a sample of side a's customers, whatever its name (one population):
    ``bottom_k``, ``size`` keys of each of the two segments (3: each whole, a census stratum), or a
    monitoring ``rate`` sample of every key; ``key`` is its key's class."""
    raw = copy.deepcopy(_SAMPLED)
    raw["metadata"] |= {"name": name, "version": version}
    raw["key"]["class"] = key
    frame = {"kind": "table", "side": "a", "strataColumns": ["segment"]}
    source = {"kind": "hash_sample", "method": method, "salt": "cutover-q4", "source": frame}
    strata = copy.deepcopy(_SEGMENTS)
    for bucket in strata[0]["buckets"]:
        bucket["targetSize"] = size
    raw["key"]["source"] = source | ({"strata": strata} if method == "bottom_k" else {"rate": 1})
    return compile_spec(raw)


async def drawn(
    conn: repo.Conn,
    spec_id: int,
    version: CompiledSpec,
    who: Principal,
    *,
    discrepant: Sequence[str] = (),
    absent: Sequence[str] = (),
    end: bool = True,
    judged: bool = True,
    approval_id: str | None = None,
    record: Mapping[str, Any] | EllipsisType | None = ...,
    census: Mapping[str, Mapping[str, Any]] | None = None,
    audited: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """A batch run of the sampled ``version`` (stored as ``spec_id``) started by ``who`` under SSO, its draw
    committed as the worker commits one (the draw, its labelled keys, its frame's census and ``sample.draw``
    last, whose detail ``audited`` may replace members of), each drawn key compared (MISMATCH for the keys in
    ``discrepant``, absent on both sides for those in ``absent``, else MATCH) and, with ``end``, its cycle
    ended with the judgement its end stores (none without ``judged``), under the spec approval
    ``approval_id``: the run's and draw's ids. ``census``: the frame's census records by side (default: side
    a's table read whole). ``record``: the licence record the run was made under (None: an open build's),
    its creation audited as the route audits it.
    Each call is one transaction's work: commit before the next draw, whose chain row must follow."""
    assert version.sample is not None
    run_id, nonce = repo.new_run_id(), sampling.nonce()
    params = {
        "keys_source": "hash_sample",
        "samples_dir": None,
        "method": version.sample.method,
        "population": list(version.sample.population),
        "approval": {"needs": ["sampling"], "mode": "oidc"},
    } | ({"licence": record} if record is not ... else {})
    stored = await repo.create_run(
        conn, spec_id, "batch", {}, params, run_id=run_id, created_by=who.actor, retention="standard",
        data_key=TEST_DATA_KEY, approval_id=approval_id,
    )  # fmt: skip
    if record is not ...:
        created = {"spec_id": spec_id, "mode": "batch", "keys": 0, "file_mode": False,
                   "approval_id": approval_id, "approval": params["approval"], "licence": record,
                   "plan_sha256": sha256_hex(approvals.plan(stored))}  # fmt: skip
        await repo.insert_audit(conn, who, "create_run", run_id, created)
    await repo.set_run_status(conn, run_id, "running")
    await repo.set_draw(conn, run_id, {"due": utc_now_iso()})
    salt = sampling.salt(version.sample.label, nonce)
    selector = sampling.Selector(version.sample, salt, datetime.now(UTC))
    selector.add([(key, {"segment": segment}) for key, segment in FRAME], source=0, first=1)
    selection = selector.result()
    masked = {d.key: f"m-{d.key}" for d in selection.keys}
    lines = [(d.seq, masked[d.key], d.stratum, d.signoff, d.member) for d in selection.keys]
    at = utc_now_iso()
    table = {"basis": "frame", "target": "db-a", "relation": "public.customers", "where": None, "rows": 6,
             "skipped": 0, "keys": 6, "unfit": 0, "at": at}  # fmt: skip
    census = census or {"a": table}
    origin = {side: len(FRAME) if side in census else 0 for side in "ab"}
    row = {
        "draw_id": repo.new_draw_id(), "run_id": run_id, "spec_id": spec_id, "cycle": 1,
        "method": version.sample.method, "population": list(version.sample.population),
        "salt_label": version.sample.label, "nonce": nonce, "keys": len(masked),
        "frame": {"keys": len(FRAME), "origin": origin | {"both": 0}, "unfit": 0},
        "strata": selection.strata, "selection_sha256": sampling.selection_sha256(lines),
    }  # fmt: skip
    labels = [(d.stratum, d.signoff, d.member) for d in selection.keys]
    await repo.insert_draw(conn, row, TEST_SEALER.seal_keys(masked), labels)
    await repo.set_census(conn, run_id, {"cycle": 1, "at": at, **census})
    await repo.set_draw(conn, run_id, None)
    names = ("draw_id", "cycle", "method", "population", "salt_label", "keys", "selection_sha256", "frame")
    detail = {name: row[name] for name in names} | {"salt_sha256": sha256_hex(salt)}
    detail["strata"] = [
        {name: s[name] for name in ("label", "frame_keys", "drawn")} for s in selection.strata
    ]
    await repo.insert_audit(conn, WORKER, "sample.draw", run_id, detail | dict(audited or {}))
    for key in masked:
        side, outcome, reason = COMPLETE, Outcome.MISMATCH if key in discrepant else Outcome.MATCH, None
        if key in absent:
            side, outcome, reason = SideState(SideStateKind.ABSENT), Outcome.INCONCLUSIVE, "absent_both"
        result = KeyResult(
            key, outcome, side, side, inconclusive_reason=reason, confirmation=Confirmation.RECHECKED
        )
        await repo.save_key_result(conn, run_id, 1, f"m-{key}", result, [])
    await conn.execute("UPDATE ds.run_keys SET state = 'done' WHERE run_id = %s", (run_id,))
    if end:
        matched = len(masked) - len(discrepant) - len(absent)
        await repo.set_counters(conn, run_id, counters(1, matched=matched))
        found = await repo.get_draw(conn, run_id, 1)
        judgement = await sample.judge(conn, found, version) if judged else None  # type: ignore[arg-type]
        await repo.end_cycle(conn, run_id, sample=judgement)
        await repo.set_run_status(conn, run_id, "completed")
    return run_id, row["draw_id"]


# -- labellings -----------------------------------------------------------

TRUTH = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01", "names_side": None}
ITEMS = [
    {"item": 1, "key": "m-k1", "population": "discrepant", "root_cause_id": "*", "selected_for": "uniform"},
    {"item": 2, "key": "m-k2", "population": "agreed", "root_cause_id": "*", "selected_for": "uniform"},
]
SELECTION = {
    "items": ITEMS,
    "counts": {
        "discrepant": {"items": 1, "keys": 1, "causes": 0, "drawn": 1},
        "accepted": {"keys": 0, "by_rule": {}, "drawn": 0},
        "agreed": {"keys": 2, "drawn": 1},
        "indeterminate": 0, "not_compared": 0, "matched_by_row_hash": 0,
    },
    "causes": [], "uncovered": 0, "names_side": None, "unlocatable": False, "warnings": [],
}  # fmt: skip


def drawn_labelling(run_id: str, by: Principal, **changes: Any) -> dict[str, Any]:
    """A labelling of the run's ended first cycle as ``repo.insert_labelling`` takes it: one discrepant
    item and one agreed key, drawn by ``by``."""
    return {
        "run_id": run_id, "cycle": 1, "salt": "0123456789abcdef" * 2, "size": 1, "per_cause": 0,
        "accepted": 0, "agreed": 1, "truth_source": TRUTH["source"], "truth_as_of": TRUTH["as_of"],
        "selection": SELECTION, "selection_sha256": accuracy.selection_sha256(ITEMS), "sheet": "item\r\n",
        "drawn_by": by.actor,
    } | changes  # fmt: skip


async def label(conn: repo.Conn, labelling_id: str, by: Principal) -> repo.Row | None:
    """``by``'s labels of a ``drawn_labelling``: the discrepant item B_RIGHT, the agreed key BOTH_RIGHT,
    scored as ``accuracy.tally`` scores them; None when it is labelled already."""
    cells = {
        "root_cause_id": "*",
        "selected_for": "uniform",
        "basis": "SYSTEM_OF_RECORD",
        "reason": "",
        "rules": "",
    }
    rows = [
        cells | {"population": "discrepant", "label": "B_RIGHT", "engine_outcome": "MISMATCH"},
        cells | {"population": "agreed", "label": "BOTH_RIGHT", "engine_outcome": "MATCH"},
    ]
    return await repo.label_labelling(
        conn, labelling_id, sheet="item,label\r\n", sheet_sha256="2" * 64, labels_sha256="3" * 64,
        accuracy=accuracy.tally(rows, SELECTION, TRUTH), score_sha256="4" * 64, labelled_by=by.actor,
    )  # fmt: skip


async def labelling(
    conn: repo.Conn, run_id: str, drawn_by: Principal, labelled_by: Principal | None = None
) -> repo.Row:
    """A labelling of the run's ended first cycle drawn by ``drawn_by`` and, with ``labelled_by``, labelled:
    its row."""
    row = await repo.insert_labelling(conn, drawn_labelling(run_id, drawn_by))
    if labelled_by is not None:
        row = await label(conn, row["labelling_id"], labelled_by)
        assert row is not None
    return row
