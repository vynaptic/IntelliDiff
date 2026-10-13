"""The events fed audit rows make (docs/mvp/ALERTS.md §2.1): each catalogue row, the stops, pauses and
refusals notified once, the error type, the run as the feed finds it, a pair's change of readiness
(docs/mvp/READINESS.md §8), a schedule's time that started no run (docs/mvp/SCHEDULES.md §8), and
adversarial details whose planted texts never reach a body."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deltasleuth import approvals, licence
from deltasleuth.connectors import tables
from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.core import notify, precheck, readiness
from deltasleuth.core.notify import Event, Seen, Unsafe
from deltasleuth.core.secrets import SecretError
from deltasleuth.runner import drift, runs, sample, stop
from deltasleuth.runner.cycle import failure

from .notified import (
    APPROVAL,
    BASIS,
    CHANGED,
    DRAW,
    DRIFT,
    GUARD_PAUSE,
    INSTALLATION,
    MISSED,
    PACK,
    PAIR,
    PAUSED,
    RUN,
    SCHEDULE,
    block,
    drifted,
    missed,
    paused,
    rendered,
    row,
    seen,
)
from .notified import readiness as changed

LABELLING = "V-" + "0" * 25 + "5"
# What a renewed licence changes of a pair that is parity-ready: its sign-off and pack can be made again.
RENEWED = [{"gate": gate, "from": "unavailable", "to": "not_met"} for gate in ("signoff", "pack")]
LABELLED = {
    "run_id": RUN, "cycle": 2, "sheet_sha256": "b" * 64, "labels_sha256": "c" * 64, "score_sha256": "d" * 64,
    "labelled": 270, "undecidable": 5, "not_reviewed": 1,
}  # fmt: skip

STRATA = {"status": "failed", "strata": [{"verdict": "failed"}, {"verdict": "passed"}, {"verdict": "passed"}]}


def kinds(action: str, detail: dict[str, Any], found: Seen | None = None, **options: Any) -> list[str]:
    return [e.kind for e in notify.EVENTS[action][0](row(action, detail, **options), found or seen())]


def facts(action: str, detail: dict[str, Any], found: Seen | None = None, **options: Any) -> dict[str, Any]:
    (event,) = notify.EVENTS[action][0](row(action, detail, **options), found or seen())
    return notify.checked(event).facts


def test_the_reasons_named_are_the_runner_s() -> None:
    assert notify.PAUSES == (
        runs.AUTH_PAUSE,
        runs.KEY_PAUSE,
        approvals.APPROVAL_PAUSE,
        licence.LICENCE_PAUSE,
        sample.SAMPLE_PAUSE,
        drift.SCHEMA_PAUSE,
    )
    assert (notify.DRAW_PAUSE, notify.STOP_PREFIX, notify.NOT_EVIDENCE) == (
        sample.DRAW_PAUSE,
        stop.STOP_PREFIX,
        approvals.NOT_EVIDENCE,
    )
    assert set(notify.FEED_ACTIONS) >= {"run.cycle", "run.finished", "paused", "cancelling", "notify.dead"}


def test_a_cycle_boundary_ends_the_cycle_before_it() -> None:
    found = seen(cycle=11, draw=True, sample=STRATA)
    events = notify.EVENTS["run.cycle"][0](row("run.cycle", {"cycle": 12}), found)
    assert notify.concerned(row("run.cycle", {"cycle": 12})) == 11
    assert [e.kind for e in events] == ["cycle.ended", "sample.judged"]
    assert notify.checked(events[1]).facts == {
        "cycle": 11,
        "draw_id": found.draw["draw_id"],
        "method": "bottom_k",
        "status": "failed",  # type: ignore[index]
        "strata": {"passed": 2, "failed": 1, "unfinished": 0},
    }
    assert kinds("run.cycle", {"cycle": 12}, seen(counters={})) == []  # an unsummarised cycle


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ({"status": "completed", "reason": None}, ["cycle.ended", "run.finished"]),
        (
            {"status": "completed_with_issues", "reason": "stop_condition:maxCycles"},
            ["cycle.ended", "run.finished"],
        ),
        ({"status": "cancelled", "reason": "erasure"}, ["run.finished"]),
        ({"status": "failed", "reason": "ReconciliationError"}, ["run.failed"]),
    ],
)
def test_a_run_s_end(detail: dict[str, Any], expected: list[str]) -> None:
    assert kinds("run.finished", detail) == expected


def test_failed_run_sample_judged() -> None:
    """fail_run judges a drawn cycle first: its judgement notifies, unjudged when there is none."""
    found = seen(draw=True, sample=None, status="failed")
    assert kinds("run.finished", {"status": "failed", "reason": "KeyError"}, found) == [
        "sample.judged",
        "run.failed",
    ]
    events = notify.EVENTS["run.finished"][0](row("run.finished", {"status": "failed", "reason": "X"}), found)
    assert notify.checked(events[0]).facts["status"] == "unjudged"


def test_boundary_stop() -> None:
    """until and maxDuration end a soak at its boundary without a cancelling row: stopped and finished."""
    detail = {"status": "completed", "reason": "stop_condition:until"}
    assert kinds("run.finished", detail) == ["cycle.ended", "run.finished", "run.stopped"]
    assert kinds("run.finished", detail, seen(cancelling=True)) == ["cycle.ended", "run.finished"]
    assert facts("cancelling", {"reason": "stop_condition:maxErrorRate"}) == {
        "cycle": 12,
        "condition": "maxErrorRate",
    }
    assert kinds("cancelling", {"reason": "cancelled"}) == []


def test_maxkeys_not_stopped() -> None:
    for name in ("maxKeys", "maxCycles"):
        detail = {"status": "completed_with_issues", "reason": f"stop_condition:{name}"}
        assert kinds("run.finished", detail) == ["cycle.ended", "run.finished"]
        assert facts("run.finished", detail, seen(counters={}))["reason"] == f"stop_condition:{name}"


def test_draw_refusal_once() -> None:
    """A refused draw's pause and sample.draw_failed share a transaction: only the second notifies."""
    assert kinds("paused", {"reason": notify.DRAW_PAUSE}) == []
    assert facts("sample.draw_failed", {"cycle": 3, "error": "the frame of side a timed out"}) == {"cycle": 3}


def test_revoke_pause_once() -> None:
    """A revoke's pause has its approval_id: approval.revoked counts it; a boundary's pause has none."""
    assert kinds("paused", {"reason": approvals.APPROVAL_PAUSE, "approval_id": APPROVAL}) == []
    assert kinds("paused", {"reason": approvals.APPROVAL_PAUSE, "approval_id": None}) == ["run.paused"]
    approval = {"kind": "spec", "status": "revoked"}
    detail = {"reason": "a note", "paused": [RUN, RUN], "stopped": [RUN], "cascade_of": None}
    assert facts("approval.revoke", detail, Seen(approval=approval), obj=APPROVAL) == {
        "approval_id": APPROVAL,
        "kind": "spec",
        "paused": 2,
        "stopped": 1,
    }
    assert (
        kinds("approval.revoke", detail | {"cascade_of": APPROVAL}, Seen(approval=approval), obj=APPROVAL)
        == []
    )


def test_pauses() -> None:
    assert kinds("paused", {"reason": None}) == []  # a person's own
    licenced = {"reason": licence.LICENCE_PAUSE, "approval_id": APPROVAL, "licence": {"refusal": "unseated"}}
    assert facts("paused", licenced) == {
        "cycle": 12,
        "reason": "run_paused_licence",
        "approval_id": APPROVAL,
        "draw_id": None,
        "licence_refusal": "unseated",
        "schema": None,
    }
    assert facts("paused", {"reason": runs.AUTH_PAUSE})["reason"] == "run_paused_auth_error"


def test_approvals_waiting() -> None:
    requested = Seen(approval={"status": "requested", "approvers_needed": 2, "kind": "signoff"})
    detail = {"kind": "signoff", "spec_id": 7, "run_id": RUN, "cycle": 3, "attests_sha256": "f" * 64}
    assert facts("approval.request", detail, requested, obj=APPROVAL)["approvals_left"] == 2
    step = {
        "kind": "spec",
        "spec_id": 7,
        "content_hash": "x",
        "scopes": [],
        "step": 1,
        "of": 2,
        "authors": ["u"],
    }
    assert facts("approval.grant", step, requested, obj=APPROVAL)["approvals_left"] == 1
    assert kinds("approval.grant", step | {"step": 2}, requested, obj=APPROVAL) == []
    decided = Seen(approval={"status": "approved", "approvers_needed": 1})
    assert kinds("approval.request", detail, decided, obj=APPROVAL) == []


def test_licence_and_notifier_events() -> None:
    detail = {"from": "valid", "to": "ending", "licence_id": "LI-" + "0" * 26, "ends": "2026-11-01"}
    assert facts("licence.state", detail, Seen(), obj="I-3f0a1b2c3d4e5f60")["from"] == "valid"
    dead = {
        "delivery_id": "N-" + "0" * 26,
        "event": "run.failed",
        "attempts": 8,
        "error": "retries_exhausted",
        "status": 503,
        "body_sha256": "a" * 64,
        "dead_since_last": 2,
    }
    assert facts("notify.dead", dead, Seen(), obj="notify:ops-hook")["channel"] == "ops-hook"
    assert facts("notify.skipped", {"reason": "error", "rows": 1, "seqs": [9]}, Seen(), obj="notify")[
        "seqs"
    ] == [9]


def test_a_labelling_labelled_is_measured() -> None:
    """``accuracy.measured`` (docs/mvp/ACCURACY.md §10), from ``accuracy.label``: the labelling's id and
    counts, never a hash, a label or a share; the placeholder name for it is gone."""
    labelled = row("accuracy.label", LABELLED, obj=LABELLING)
    assert notify.concerned(labelled) == 2  # the row names its cycle
    (event,) = notify.EVENTS["accuracy.label"][0](labelled, seen(cycle=2))
    checked = notify.checked(event)
    assert checked.facts == {
        "cycle": 2, "labelling_id": LABELLING, "labelled": 270, "undecidable": 5, "not_reviewed": 1,
    }  # fmt: skip
    assert notify.dedup(checked, 81234) == f"accuracy.measured|{LABELLING}"
    assert notify.severity(checked, False) == "info"
    doc, body = rendered(checked)
    assert (doc["event"], doc["severity"], doc["link"]) == (
        "accuracy.measured", "info", f"https://ds.bank.example/#/runs/{RUN}?cycle=2",
    )  # fmt: skip
    assert doc["title"] == f"customer-profile: labelling {LABELLING} of cycle 2 of run {RUN} was labelled"
    assert doc["text"] == (
        f"[prod] {doc['title']}: 270 items labelled, 5 undecidable, 1 of them not reviewed (observed)."
        " Observed by DeltaSleuth (installation I-3f0a1b2c3d4e5f60); not evidence and not a sign-off. Audit"
        " row 81234."
    )
    assert not any(digest * 64 in body for digest in "bcd") and "sha256:" not in body
    assert "accuracy.measured" in notify.KINDS and notify.RESERVED == {}  # no event is reserved
    assert "accuracy.label" in notify.FEED_ACTIONS and "accuracy.label" not in notify.KINDS
    for facts in (
        checked.facts | {"labelling_id": RUN.replace("R-", "J-")},  # not an id that may leave
        checked.facts | {"undecidable": -1},
        checked.facts | {"labelled": "270"},
        checked.facts | {"labels_sha256": "c" * 64},  # nothing beyond its model
        checked.facts | {"cycle": 0},
    ):
        with pytest.raises(Unsafe, match=r"accuracy\.measured: 1 fact"):
            notify.checked(replace(event, facts=facts))


def test_a_pair_s_readiness_changed() -> None:
    """``readiness.changed`` (docs/mvp/READINESS.md §8): ids, states, counts and slugs; each changed gate's
    statuses, never its counts; the wording; the link to the pair's screen."""
    assert get_args(notify.Ready) == readiness.STATES and get_args(notify.Gate) == readiness.GATES
    assert get_args(notify.Cause) == readiness.CAUSES and "readiness.changed" in notify.FEED_ACTIONS
    event = changed()
    changes = [{"gate": gate, "from": "not_met", "to": "met"} for gate in ("targets", "convergence")]
    assert event.facts == CHANGED | {"changed": changes}
    assert (event.spec, event.mode, event.run) == (PAIR, None, None)  # rules match the pair as a spec
    assert notify.dedup(event, 81234) == f"readiness.changed|{PAIR}|parity_ready|000"
    doc, body = rendered(event)
    assert (doc["event"], doc["severity"], doc["link"], doc["withheld"], doc["run"]) == (
        "readiness.changed", "info", f"https://ds.bank.example/#/readiness/{PAIR}", {}, None,
    )  # fmt: skip
    assert doc["title"] == f"cutover-q4 / {PAIR}: readiness not ready -> parity ready"
    assert doc["text"] == (
        f"[prod] {doc['title']} (5 of 5 gates; targets met, convergence met; cycle 14 of run {RUN})."
        " Observed; readiness is not a sign-off."
        f" Observed by DeltaSleuth (installation {INSTALLATION}); not evidence and not a sign-off. Audit row"
        " 81234."
    )
    assert '"allowed"' not in body and "999" not in body  # a gate's counts stay in the audit row
    baseline = changed(**{"from": None, "causes": ["baseline"], "changed": [], "programme": None})
    assert rendered(baseline)[0]["title"] == f"{PAIR}: readiness first recorded as parity ready"
    same = changed(to="not_ready", changed=[CHANGED["changed"][0]], regressed=True, evidence=None)
    assert rendered(same)[0]["text"].startswith(
        f"[prod] cutover-q4 / {PAIR}: readiness still not ready (5 of 5 gates; targets met; regressed)."
    )
    signed = changed(to="evidenced", signoff_id=APPROVAL, pack_id=PACK)  # the id pattern gains P
    assert rendered(signed)[0]["title"].endswith("readiness not ready -> evidenced")
    assert rendered(changed(to="signoff_ready"))[0]["title"].endswith("-> sign-off ready")
    for facts in (
        {"to": "ready"},  # no state
        {"causes": ["upgrade"]},
        {"score": {"passed": 13, "required": 13}},
        {"changed": [{"gate": "parity", "from": "met", "to": "not_met"}]},
        {"changed": [CHANGED["changed"][0]] * 13},
        {"gates": {"targets": "passed"}},
        {"pack_id": RUN.replace("R-", "J-")},
        {"config_sha256": "c" * 63},
        {"evidence": {"run_id": RUN, "cycle": 14, "ended_at": "2026-10-03"}},  # nothing beyond its model
    ):
        with pytest.raises(Unsafe, match=r"readiness\.changed: \d+ fact"):
            changed(**facts)
    assert "4111" not in rendered(changed(text="customer 4111111111111111"))[1]  # a key it never reads


@pytest.mark.parametrize(
    ("detail", "severity"),
    [
        ({"causes": ["baseline"], "from": None, "changed": []}, "info"),
        ({}, "info"),  # gates went met, the state rose
        ({"changed": [{"gate": "fresh", "from": "met", "to": "not_met"}], "to": "not_ready"}, "alert"),
        ({"changed": [{"gate": "convergence", "from": "met", "to": "unknown"}]}, "alert"),
        ({"was_regressed": False, "regressed": True, "changed": []}, "alert"),  # it turned true
        ({"was_regressed": True, "regressed": True, "changed": []}, "info"),  # it stayed
        ({"changed": [{"gate": "pack", "from": "met", "to": "unavailable"}]}, "warning"),
        ({"from": "evidenced", "to": "signed_off", "changed": []}, "warning"),  # the state fell otherwise
        ({"from": "parity_ready", "to": "parity_ready", "changed": []}, "info"),
        # A gate that only became judged on the way up fell from nothing: a licence renewed (sign-off and
        # pack can be made again), a gate newly required, a cycle with enough results to adjudicate.
        ({"from": "parity_ready", "to": "signoff_ready", "causes": ["licence"], "changed": RENEWED}, "info"),
        ({"changed": [{"gate": "graph", "from": None, "to": "unknown"}]}, "info"),
        ({"changed": [{"gate": "adjudication", "from": "not_applicable", "to": "not_met"}]}, "info"),
        ({"changed": [{"gate": "convergence", "from": "not_met", "to": "unknown"}]}, "info"),
        # With the state falling it is an alert, whatever the gate came from.
        (
            {
                "from": "parity_ready",
                "to": "not_ready",
                "changed": [{"gate": "graph", "from": None, "to": "not_met"}],
            },
            "alert",
        ),
        (
            {
                "from": "unknown",
                "to": "not_ready",
                "changed": [{"gate": "targets", "from": "unknown", "to": "not_met"}],
            },
            "alert",
        ),
    ],
)
def test_a_readiness_change_s_severity(detail: dict[str, Any], severity: str) -> None:
    assert notify.severity(changed(**detail), False) == severity


def test_a_readiness_change_s_dedup_key_names_the_required_gates_not_met() -> None:
    """A bit a gate, in the ladder's order, of the required gates that are not met or unknown."""
    gates = {"population": "unknown", "targets": "not_met", "pack": "not_met", "fresh": "unavailable"}
    event = changed(to="not_ready", gates=CHANGED["gates"] | gates)
    assert notify.dedup(event, 1) == f"readiness.changed|{PAIR}|not_ready|a01"
    worst = changed(pair="x" * 63, to="signoff_ready", gates=dict.fromkeys(readiness.GATES, "unknown"))
    assert notify.dedup(worst, 1).endswith("|signoff_ready|fff") and len(notify.dedup(worst, 1)) <= 102


def test_a_readiness_change_s_labels_are_withheld() -> None:
    """Pair, programme and entity are labels (§6.4): none where the channel sends no labels; a pair whose
    name its key pattern matches is never named, and its link names none either."""
    doc, body = rendered(changed(), "slack", labels=None, links=True)
    assert doc["withheld"] == {"pair": "labels_off", "programme": "labels_off", "entity": "labels_off"}
    assert (doc["facts"]["pair"], doc["facts"]["programme"], doc["facts"]["entity"]) == (None, None, None)
    assert doc["title"] == "a pair: readiness not ready -> parity ready"
    assert doc["link"] == "https://ds.bank.example/#/readiness"
    assert PAIR not in body and "cutover-q4" not in body and '"customer"' not in body
    for why in ("key_shaped", "no_key_pattern"):
        shaped, body = rendered(changed(why, pair="k001"))
        assert shaped["withheld"] == {"pair": why} and shaped["facts"]["pair"] is None
        assert shaped["title"] == "cutover-q4 / a pair: readiness not ready -> parity ready"
        assert shaped["link"].endswith("/#/readiness") and "k001" not in body.replace(shaped["text"], "")
    # The dedup key is stored, never sent: it names the pair so that two pairs' changes are two deliveries.
    assert notify.dedup(changed("key_shaped", pair="k001"), 1).startswith("readiness.changed|k001|")
    # A name that is no slug never leaves: the event fails its model (the worker writes spec names and the
    # settings' slugs only, so no such row is ever skipped as unsafe).
    for name in ("Customer 42", "4111111111111111", "customer_profile", "x" * 64, ""):
        for field in ("programme", "entity"):
            with pytest.raises(Unsafe):
                changed(**{field: name})
        with pytest.raises(Unsafe):
            changed(pair=name)
    for name in ("customer-profile", "k001", "a", "x" * 63, "cards-2"):  # slugs pass
        assert changed(pair=name, programme=name, entity=name).facts["pair"] == name


def test_a_schedule_s_time_that_started_no_run() -> None:
    """``schedule.missed`` (docs/mvp/SCHEDULES.md §8): ids, times, closed codes and an exception's type; the
    spec's name and the mode are the row's own, for rules to match, and no payload carries them."""
    assert notify.EVENTS["schedule.missed"][1] == {"schedule.missed"} and "schedule.missed" in notify.KINDS
    event = missed()
    assert event.facts == {
        "schedule_id": SCHEDULE, "reason": "overlap", "code": None, "due": "2026-10-07T01:00Z", "to": None,
        "count": None, "run_id": RUN, "draw_id": None, "error_type": None,
    }  # fmt: skip
    assert (event.spec, event.mode, event.run, event.via) == (PAIR, "batch", None, "schedule.missed")
    assert notify.dedup(event, 81234) == f"schedule.missed|{SCHEDULE}|overlap"
    assert notify.superseded(event) is None
    doc, _ = rendered(event)
    assert (doc["event"], doc["severity"], doc["link"], doc["withheld"], doc["run"]) == (
        "schedule.missed", "warning", f"https://ds.bank.example/#/schedules/{SCHEDULE}", {}, None,
    )  # fmt: skip
    assert doc["title"] == f"schedule {SCHEDULE} did not start a run"
    assert doc["text"] == (
        f"[prod] schedule {SCHEDULE} did not start a run for 2026-10-07T01:00Z: run {RUN} of it was still"
        f" live. Observed by DeltaSleuth (installation {INSTALLATION}); not evidence and not a sign-off."
        " Audit row 81234."
    )
    for kind in ("webhook", "slack", "teams", "email"):  # no name, no zone, no revision: ids and times
        sent = rendered(event, kind)[1]
        assert PAIR not in sent and "Europe/London" not in sent and "2026-10-08" not in sent
    # Whoever wrote the row: a person's write that left a time unstarted projects the same.
    by_person = missed(reason="superseded", code="pause", to="2026-10-07T01:00Z", count=1, run_id=None)
    assert rendered(by_person)[0]["text"].startswith(
        f"[prod] schedule {SCHEDULE} did not start a run for 2026-10-07T01:00Z: due and not started when the"
        " schedule was paused. Observed by DeltaSleuth"
    )


@pytest.mark.parametrize(
    ("reason", "code", "severity", "said"),
    [
        ("late", "down", "alert", "no worker could start a run in time (none that fires schedules ran, the"
                                  " store was down, or the clock moved); a schedule never runs a backlog."),
        ("late", "busy", "alert", "its window closed while the worker was still starting other schedules"
                                  " (two at a time)."),
        ("late", "fired_already", "alert", "a run for that time exists already."),
        ("superseded", "update", "alert", "due and not started when the schedule was saved again."),
        ("superseded", "pause", "alert", "due and not started when the schedule was paused."),
        ("superseded", "delete", "alert", "due and not started when the schedule was deleted."),
        ("zone", "zone", "alert", "the worker's time zone data did not hold its zone, and the worker paused"
                                  " it. A person saves the schedule with a zone the build holds, then"
                                  " resumes it."),
        ("zone", "cron", "alert", "the worker's build could not read its cron, and the worker paused it. A"
                                  " person saves the schedule again, then resumes it."),
        ("mode", None, "alert", "it was saved in another auth mode than this worker runs in. A person saves"
                                " it again to run it."),
        ("setter", None, "alert", "who saved it is disabled or no longer an operator (for a token, its"
                                  " accountable person is disabled). Another operator saves the schedule"
                                  " again to run it."),
        ("not_started", None, "alert", "the run could not be created (TargetUnreachable); starting one on the"
                                       " Runs screen shows the cause."),
        ("error", None, "alert", "a fault in DeltaSleuth (TargetUnreachable); the server log names it."),
        # FILES.md §12, F11: the check shows files and whether they are settled; no stamp, age or plan
        *(("not_started", code, "alert", "the export a file side reads was not ready before its window closed"
           f" ({code}); ds target check shows its state.")
          for code in ("export_absent", "unsettled")),
        *(("not_started", code, "alert", "the export a file side reads was not ready before its window closed"
           f" ({code}); starting a run on the Runs screen shows the cause.")
          for code in ("stale", "unstamped", "planning")),
        ("overlap", None, "warning", f"run {RUN} of it was still live."),
        ("approval", None, "warning", "its spec version lacked the four-eyes approval it needs."),
        ("licence", "expired", "warning", "the licence refused the run (expired)."),
        ("licence", "sso", "warning", "the licence refused the run (sso)."),
        ("licence", "pairs", "warning", "the licence refused the run (pairs)."),
        ("sample", "live", "warning", "a live sampled run samples its population."),
        ("sample", "unclosed", "warning", f"draw {DRAW} of its population did not pass and is not closed. A"
                                          " person closes it with the fix, then the next firing draws."),
        ("sample", "refused", "warning", "the sample's rules refused the run."),
    ],
)  # fmt: skip
def test_each_reason_and_code_of_a_missed_time(
    reason: str, code: str | None, severity: str, said: str
) -> None:
    typed = "TargetUnreachable" if reason in ("not_started", "error") else None  # only they name a type
    event = missed(reason=reason, code=code, draw_id=DRAW, error_type=typed)
    assert notify.severity(event, False) == notify.severity(event, True) == severity
    assert notify.dedup(event, 7) == f"schedule.missed|{SCHEDULE}|{reason}"  # whatever its code and time
    text = rendered(event)[0]["text"]
    assert text.startswith(f"[prod] schedule {SCHEDULE} did not start a run for 2026-10-07T01:00Z: {said} ")
    assert (reason, code) in notify._MISSED and code in notify.MISSED[reason][0]
    assert sum(len(codes) for codes, _ in notify.MISSED.values()) == len(notify._MISSED) == 25


def test_a_missed_range_is_counted() -> None:
    late = {"reason": "late", "code": "down", "run_id": None, "to": "2026-10-09T01:00Z"}
    assert (
        "for 3 times, 2026-10-07T01:00Z to 2026-10-09T01:00Z: no worker"
        in rendered(missed(**late, count=3))[0]["text"]
    )
    one = missed(reason="superseded", code="update", run_id=None, to="2026-10-07T01:00Z", count=1)
    assert " for 2026-10-07T01:00Z: due and not started when" in rendered(one)[0]["text"]
    uncounted = missed(reason="superseded", code="delete", run_id=None)  # its zone did not load
    assert " for 2026-10-07T01:00Z: due and not started when" in rendered(uncounted)[0]["text"]
    held = {"reason": "approval", "run_id": None, "to": "2026-10-21T01:00Z"}  # a standing refusal's range
    assert rendered(missed(**held, count=14))[0]["text"].startswith(
        f"[prod] schedule {SCHEDULE} did not start a run for 14 more times since 2026-10-07T01:00Z, for the"
        " same reason: its spec version lacked the four-eyes approval it needs. Observed"
    )
    assert (
        " for 1 more time since 2026-10-07T01:00Z, for the same reason: "
        in rendered(missed(**held, count=1))[0]["text"]
    )
    assert "1,234 times" in rendered(missed(**late, count=1234))[0]["text"]


@pytest.mark.parametrize(
    ("code", "said"),
    [
        ("zone", "the worker's time zone data did not hold its zone. A person saves the schedule with a zone"
                 " the build holds, then resumes it."),
        ("cron", "the worker's build could not read its cron. A person saves the schedule again, then resumes"
                 " it."),
    ],
)  # fmt: skip
def test_the_worker_s_pause_before_a_time_is_due(code: str, said: str) -> None:
    """T6 before a time is due (docs/mvp/SCHEDULES.md §4, §8): no time was missed, so the pause's own row is
    sent, as ``schedule.missed`` without a time. A pause that chained its missed row sends nothing of its
    own, and neither does a person's."""
    assert notify.EVENTS["schedule.pause"] == notify.EVENTS["schedule.missed"]
    (event,) = paused(code=code)
    assert event.facts == {
        "schedule_id": SCHEDULE, "reason": "zone", "code": code, "due": None, "to": None, "count": None,
        "run_id": None, "draw_id": None, "error_type": None,
    }  # fmt: skip
    assert (event.kind, event.spec, event.mode, event.run, event.via) == (
        "schedule.missed", PAIR, "batch", None, "schedule.pause",
    )  # fmt: skip
    assert notify.severity(event, False) == notify.severity(event, True) == "alert"
    assert notify.dedup(event, 81234) == f"schedule.missed|{SCHEDULE}|zone"  # as its missed row's would be
    doc, _ = rendered(event)
    assert (doc["event"], doc["severity"], doc["link"], doc["withheld"], doc["run"]) == (
        "schedule.missed", "alert", f"https://ds.bank.example/#/schedules/{SCHEDULE}", {}, None,
    )  # fmt: skip
    assert doc["facts"] == event.facts and doc["title"] == f"schedule {SCHEDULE} is paused by the worker"
    assert doc["text"] == (
        f"[prod] schedule {SCHEDULE} is paused by the worker before a time of it came due: {said} Observed by"
        f" DeltaSleuth (installation {INSTALLATION}); not evidence and not a sign-off. Audit row 81234."
    )
    for kind in ("webhook", "slack", "teams", "email"):  # no time was missed, and none is said to be
        sent = rendered(event, kind)[1].replace("=\r\n", "")  # an e-mail's soft line breaks
        assert "is paused by the worker before a time of it came due" in sent
        assert not any(text in sent for text in ("did not start a run", "None", PAIR, "2026c"))
    assert paused(code=code, missed=True) == []  # its missed row is the notification
    assert paused(code=code, missed=None) == [] and paused(reason=None, code=None) == []
    by_person = row("schedule.pause", {"revision": 5, "next_due": None}, obj=SCHEDULE)
    assert notify.EVENTS["schedule.pause"][0](by_person, Seen()) == []


@pytest.mark.parametrize(
    "detail",
    [
        {"reason": "late"},  # a reason without one of its codes
        {"reason": "overlap", "code": "down"},
        {"reason": "licence", "code": "unseated"},
        {"reason": "skipped"},
        {"reason": "4111111111111111"},
        {"due": "2026-10-07T01:00:00Z"},  # not the canonical text
        {"due": "2026-10-07 01:00"},
        {"due": "Jane Secretname"},
        {"due": None},  # only the worker's pause (zone) is without a time
        {"reason": "late", "code": "down", "due": None},
        {"to": "2026-10-07T01:00+00:00"},
        {"count": 0},
        {"count": "3"},
        {"run_id": "K001"},
        {"draw_id": RUN.replace("R-", "X-")},
        {"reason": "not_started", "error_type": "customer 4111 has no account"},
        {"reason": "error", "error_type": "ValueError: K001"},
        {"error_type": "K001"},  # beside a reason that names no exception's type
        {"error_type": "ValueError"},
        {"reason": "not_started"},  # and none beside one that does
    ],
)
def test_a_missed_time_s_unsafe_values_are_never_sent(detail: dict[str, Any]) -> None:
    with pytest.raises(Unsafe, match=r"schedule\.missed: \d+ fact"):
        missed(**detail)
    for obj in ("SC-1", "nightly-customers", RUN.replace("R-", "S-")):  # the row's object is its schedule
        with pytest.raises(Unsafe):
            missed(obj=obj)


FOOTER = (
    f" Observed by DeltaSleuth (installation {INSTALLATION}); not evidence and not a sign-off. Audit row"
    " 81234."
)


def test_the_schema_guard_s_event() -> None:
    """``schema.drift`` (docs/mvp/PRECHECKS.md §9): counts, closed kinds, an id and hashes; one event a
    row, alert when the run was paused, else a warning; its texts are closed and name no table."""
    assert notify.EVENTS["schema.drift"][1] == {"schema.drift"} and "schema.drift" in notify.KINDS
    assert get_args(notify.Drift) == (*precheck.BREAKING, *precheck.OTHER)  # the pure module's kinds
    event = drifted()
    assert event.facts == {name: DRIFT[name] for name in DRIFT}  # the row's own members, and no other
    assert (event.spec, event.mode, event.via) == ("customer-profile", "soak", "schema.drift")
    assert notify.concerned(row("schema.drift", DRIFT)) == 7  # the cycle looked in, not the feed's
    assert notify.dedup(event, 81234) == f"schema.drift|{RUN}|81233"  # its pause's, as that pause's own
    doc, _ = rendered(event)
    assert (doc["event"], doc["severity"], doc["link"]) == (
        "schema.drift", "alert", f"https://ds.bank.example/#/runs/{RUN}?cycle=7",
    )  # fmt: skip
    lead = f"[prod] run {RUN} of customer-profile 1.0.0 (soak)"
    assert doc["title"] == f"customer-profile: run {RUN} was paused by the schema guard"
    assert doc["text"] == (
        f"{lead} was paused by the schema guard at cycle 7: side b's table changed (1 change that stops the"
        f" run, 2 others).{FOOTER}"
    )
    both = DRIFT["sides"] | {"a": DRIFT["sides"]["a"] | {"breaking": 2, "kinds": {"class_changed": 2}}}
    assert rendered(drifted(sides=both))[0]["text"] == (
        f"{lead} was paused by the schema guard at cycle 7: side a's table changed (2 changes that stop the"
        f" run) and side b's table changed (1 change that stops the run, 2 others).{FOOTER}"
    )
    # A warning: the run goes on with its plan.
    warned = DRIFT["sides"] | {"b": DRIFT["sides"]["b"] | {"breaking": 0, "kinds": {"column_added": 2}}}
    event = drifted(paused=False, paused_seq=None, sides=warned)
    doc, _ = rendered(event)
    assert (doc["severity"], doc["title"]) == ("warning", f"customer-profile: a table of run {RUN} changed")
    assert notify.dedup(event, 81234) == f"schema.drift|{RUN}|{'1' * 64}|{'3' * 64}"  # once a state
    assert doc["text"] == (
        f"{lead}: side b's table changed since the run was planned (2 changes); the run goes on reading by"
        f" its plan.{FOOTER}"
    )
    # At its creation the run is judged against the version's run before it.
    doc, _ = rendered(drifted(paused=False, paused_seq=None, where="creation", cycle=1, sides=warned))
    assert doc["text"] == (
        f"{lead}: side b's table changed since run {BASIS} of its version (2 changes); this run reads the"
        f" tables as they are now.{FOOTER}"
    )
    # After a failed read (V5) that found nothing breaking the plan, the read's failure fails the run.
    doc, _ = rendered(drifted(paused=False, paused_seq=None, where="read", sides=warned))
    assert doc["text"] == (
        f"{lead}: side b's table changed since the run was planned (2 changes); the run fails on the read"
        f" that failed.{FOOTER}"
    )
    # Under ``pause`` a change that breaks nothing paused the run.
    doc, _ = rendered(drifted(mode="pause", sides=warned))
    assert doc["severity"] == "alert" and "side b's table changed (2 changes)." in doc["text"]
    # A look that could not be made.
    unchecked = {"checked": False, "paused": False, "paused_seq": None, "sides": {}}
    unchecked["error_type"] = "TargetUnreachable"
    event = drifted(**unchecked)
    doc, _ = rendered(event)
    assert (doc["severity"], notify.dedup(event, 1)) == ("warning", f"schema.drift|{RUN}|None|None")
    assert doc["title"] == f"customer-profile: the schema guard could not check run {RUN}"
    assert doc["text"] == (
        f"{lead}: the schema guard could not check cycle 7 (TargetUnreachable); the cycle went on"
        f" unchecked.{FOOTER}"
    )
    # A name that is withheld is a spec's id, as on every run's event.
    assert rendered(drifted(), labels=False)[0]["text"].startswith(f"[prod] run {RUN} of spec 7 1.0.0 (soak)")


def test_the_schema_guard_s_pause_is_a_run_paused_as_any_other() -> None:
    """Rules name their events, and a rule that routes ``run.paused`` carries every pause: the guard's
    is one more reason, an alert, and its dedup key is its ``schema.drift``'s, so that a channel routed
    both sends one."""
    assert "run_paused_schema_drift" in notify.PAUSES and "run_paused_schema_drift" in get_args(notify.Reason)
    (event,) = notify.EVENTS["paused"][0](row("paused", GUARD_PAUSE), seen(cycle=7))
    event = notify.checked(event)
    assert event.facts == {
        "cycle": 7, "reason": "run_paused_schema_drift", "approval_id": None, "draw_id": None,
        "licence_refusal": None, "schema": {"a": "1" * 64, "b": "3" * 64},
    }  # fmt: skip
    assert notify.dedup(event, 81233) == notify.dedup(drifted(), 81234)
    # A later pause of the same tables (a person resumed the run in between) is an event of its own.
    assert notify.dedup(event, 81240) == f"schema.drift|{RUN}|81240" != notify.dedup(drifted(), 81234)
    assert notify.dedup(notify.checked(paused_for(runs.AUTH_PAUSE)), 81233) == f"run.paused|{RUN}|81233"
    doc, _ = rendered(event)
    assert (doc["event"], doc["severity"]) == ("run.paused", "alert")
    assert doc["text"] == (
        f"[prod] customer-profile: run {RUN} paused: the schema guard found that a table it reads changed. A"
        f" person reads the run's schema lines, then resumes or cancels it.{FOOTER}"
    )
    one = row("paused", GUARD_PAUSE | {"schema": {"a": None, "b": "3" * 64}})  # a service against a table
    one_table = notify.checked(notify.EVENTS["paused"][0](one, seen())[0])
    assert notify.dedup(one_table, 1) == f"schema.drift|{RUN}|1"


def paused_for(reason: str) -> Event:
    (event,) = notify.EVENTS["paused"][0](row("paused", {"reason": reason}), seen())
    return event


@pytest.mark.parametrize(
    "detail",
    [
        {"where": "customers"},
        {"mode": "public.customers"},
        {"cycle": 0},
        {"cycle": "7"},
        {"checked": "yes"},
        {"paused": 1},
        {"basis": "customer-profile"},
        {"basis": "numeric(12,2)"},
        {"error_type": "TargetUnreachable"},  # beside a look that was made
        {"checked": False},  # and none beside one that was not
        {"checked": False, "error_type": "ORA-00942: table or view APP.CUSTOMERS does not exist"},
        {"sides": {"c": DRIFT["sides"]["a"]}},
        {"sides": {"a": DRIFT["sides"]["a"] | {"kinds": {"email": 1}}}},  # a column's name as a kind
        {"sides": {"a": DRIFT["sides"]["a"] | {"kinds": {"column_added": "loyalty_tier"}}}},
        {"sides": {"a": DRIFT["sides"]["a"] | {"breaking": -1}}},
        {"sides": {"a": DRIFT["sides"]["a"] | {"to": "numeric(12,2)"}}},
        {"sides": {"a": DRIFT["sides"]["a"] | {"from": "public.customers"}}},
    ],
)
def test_a_schema_change_s_unsafe_values_are_never_sent(detail: dict[str, Any]) -> None:
    """The model against planted names and type texts: a detail that holds one fails it and is never
    sent."""
    with pytest.raises(Unsafe, match=r"schema\.drift: \d+ fact"):
        drifted(**detail)
    # A member the guard never writes is not read: of the row and of a side alike.
    planted = DRIFT | {"relation": "public.customers", "columns": ["email"], "declared": "numeric(12,2)"}
    planted["sides"] = {"b": DRIFT["sides"]["b"] | {"column": "email", "was": "numeric(12,2)"}}
    sent = rendered(notify.checked(notify.EVENTS["schema.drift"][0](row("schema.drift", planted), seen())[0]))
    assert "customers" not in sent[1] and "email" not in sent[1] and "numeric" not in sent[1]
    hashes = {"schema": {"a": "public.customers", "b": "3" * 64}}
    with pytest.raises(Unsafe, match=r"run\.paused: 1 fact"):
        notify.checked(notify.EVENTS["paused"][0](row("paused", GUARD_PAUSE | hashes), seen())[0])


def test_error_type() -> None:
    assert notify.error_type("_LeaseLost") == "_LeaseLost"
    assert notify.error_type("cannot read secret file /srv/secrets/dsn: No such file") == "SecretError"
    assert notify.error_type("secret/file") == "SecretError"
    assert notify.error_type("Ünicode!") == "Error"
    assert notify.error_type(None) == "Error"
    # A table side's failure is `<class>: <code>` and the guard's its text (ENGINES §12 F2 to F8): the
    # class is sent, where both once read `SecretError`.
    guard = UnsafeTargetError("10.1.2.3 is a private address: set allow_private_network on the target")
    resolved = UnsafeTargetError(
        "target host resolves to a loopback, private, link-local or reserved address; "
        "set allow_private_network on the target to permit it"
    )
    for exc in (
        tables.SnapshotLost("ORA-01555"),
        tables.TargetTempSpace("ORA-01652"),
        tables.TargetDenied("ORA-00942"),
        tables.TargetRefused("MySQL 1356"),
        tables.TargetUnreachable("ConnectionResetError"),
        tables.EngineChanged("side a: target ledger is mysql now; this run was planned for oracle"),
        tables.FilesChanged("side a: file 3 of 12 is not the file the run was created on (content)"),
        tables.FilesChanged("side a: 13 files match now, 12 when the run was created"),
        tables.TargetTempSpace("OutOfMemoryException"),  # FILES F3
        tables.TargetTempSpace("spill"),
        tables.RowsMoved("side a: 9,999,998 rows were read where the plan counted 10,000,000"),  # FILES F10
        tables.TargetDenied("PermissionError"),  # FILES F4
        tables.TargetRefused("IOException"),
        tables.ReaderDied(  # READER F12
            "side a: the file reader's process ended 3 times (SIGBUS) while only this side's files were being"
            " read: they are read no more until DeltaSleuth restarts"
        ),
        tables.TargetTempSpace("MemoryError"),  # READER F3
        tables.TargetRefused("Oversize"),  # READER F4
        tables.TargetRefused("Unframed"),
        guard,
        resolved,
        tables.KeysOutOfOrder("side a"),
        RuntimeError("a bug"),
    ):
        assert notify.error_type(failure(exc)) == type(exc).__name__
    # A secret's message names a file and stays `SecretError`, also where its path holds `: `.
    for text in ("secret file /run/secrets/dsn is empty", "secret file /run/secrets/a: b is not UTF-8 text"):
        assert notify.error_type(failure(SecretError(text))) == "SecretError"


def test_unsafe_skipped() -> None:
    event = Event(
        "run.paused",
        {
            "cycle": 1,
            "reason": "4111111111111111",
            "approval_id": None,
            "draw_id": None,
            "licence_refusal": None,
        },
        block(),
    )
    with pytest.raises(Unsafe, match=r"run\.paused: 1 fact"):
        notify.checked(event)
    with pytest.raises(Unsafe):
        notify.checked(Event("run.stopped", {"cycle": 1, "condition": "maxKeys", "extra": 1}, block()))
    with pytest.raises(Unsafe):
        notify.checked(Event("run.stopped", {"cycle": 1, "condition": "maxKeys"}, block(name="Customer 42")))
    for text in ("4111 1111 1111 1111 card", "cust/12345678", "Error\n", "123abc"):  # the whole of it
        with pytest.raises(Unsafe):
            notify.checked(Event("run.failed", {"cycle": 1, "error_type": text}, block()))


def test_now_block() -> None:
    """A late cycle.ended keeps its own cycle; its run block is the run as the feed finds it."""
    found = seen(cycle=2, status="paused")
    found = replace(found, run={**found.run, "now": {**found.run["now"], "cycle": 5}})  # type: ignore[dict-item]
    (event, *_) = notify.EVENTS["run.cycle"][0](row("run.cycle", {"cycle": 3}), found)
    assert event.facts["cycle"] == 2 and event.run["now"]["cycle"] == 5  # type: ignore[index]


PLANTED = [
    "4111111111111111",
    "CUST-00042",
    "alice@example.com",
    "Jane Secretname",
    "/srv/secrets/dsn",
    "tok:name:v1:AAAAAAAAAAAAAAAAAAAAAAAAAA",
    "K001",
    "https://evil.example/p",
    "RC-0123456789ab",
]
DETAILS = {
    "run.cycle": ["cycle"],
    "run.finished": ["status", "reason"],
    "cancelling": ["reason"],
    "paused": ["reason", "approval_id", "draw_id", "licence", "schema"],
    "schema.drift": [
        "cycle", "where", "mode", "checked", "paused", "paused_seq", "basis", "sides", "error_type",
    ],
    "sample.draw_failed": ["cycle", "error"],
    "accuracy.label": [
        "run_id",
        "cycle",
        "sheet_sha256",
        "labels_sha256",
        "score_sha256",
        "labelled",
        "undecidable",
        "not_reviewed",
    ],
    "approval.request": ["kind", "spec_id", "run_id", "cycle", "note"],
    "approval.grant": ["kind", "spec_id", "run_id", "cycle", "step", "of", "authors"],
    "approval.revoke": ["reason", "paused", "stopped", "cascade_of"],
    "licence.state": ["from", "to", "licence_id", "ends"],
    "notify.dead": ["delivery_id", "event", "attempts", "error", "status", "dead_since_last"],
    "notify.skipped": ["reason", "from_seq", "to_seq", "rows", "seqs"],
    "readiness.changed": [
        "pair", "programme", "entity", "from", "to", "was_regressed", "regressed", "score", "causes", "gates",
        "changed", "evidence", "candidate", "signoff_id", "pack_id", "config_sha256",
    ],
    "schedule.missed": [
        "reason", "code", "due", "to", "count", "spec_name", "mode", "spec_id", "run_id", "draw_id",
        "error_type", "revision", "time_zone", "tzdata", "next",
    ],
    "schedule.pause": ["revision", "next_due", "reason", "code", "tzdata", "missed", "spec_name", "mode"],
}  # fmt: skip
GOOD = {
    "cycle": 3,
    "step": 1,
    "of": 2,
    "spec_id": 7,
    "attempts": 2,
    "rows": 1,
    "dead_since_last": 0,
    "paused": [],
    "stopped": [],
    "seqs": [1],
    "from_seq": 1,
    "to_seq": 2,
    "status": "completed",
    "labelled": 9,
    "undecidable": 1,
    "not_reviewed": 0,
}


@settings(max_examples=300, deadline=None)
@given(
    st.sampled_from(sorted(DETAILS)),
    st.dictionaries(
        st.sampled_from(sorted({f for fs in DETAILS.values() for f in fs})), st.sampled_from(PLANTED)
    ),
)
def test_planted_texts_never_reach_a_body(action: str, planted: dict[str, str]) -> None:
    theirs = {"readiness.changed": CHANGED, "schedule.missed": MISSED, "schedule.pause": PAUSED}
    theirs |= {"schema.drift": DRIFT}
    good = theirs.get(action, GOOD)
    detail = {name: good.get(name) for name in DETAILS[action]} | {
        name: value for name, value in planted.items() if name in DETAILS[action]
    }
    detail.setdefault("note", "Jane Secretname")  # never read
    obj = {
        "approval.request": APPROVAL,
        "approval.grant": APPROVAL,
        "approval.revoke": APPROVAL,
        "licence.state": "I-3f0a1b2c3d4e5f60",
        "notify.dead": "notify:ops-hook",
        "accuracy.label": LABELLING,
        "schedule.missed": SCHEDULE,
        "schedule.pause": SCHEDULE,
        "readiness.changed": f"readiness:{planted.get('pair', PAIR)}",  # the pair is the row's object
    }.get(action, RUN)
    found = Seen(
        block(), "customer-profile", "soak", None, 3, {"counters": {}, "sample": None}
    )  # as the feed reads
    if obj == APPROVAL:
        found = Seen(
            spec="customer-profile", approval={"status": "requested", "approvers_needed": 1, "kind": "spec"}
        )
    elif obj.startswith("readiness:"):
        found = Seen(spec=obj.removeprefix("readiness:"))
    elif obj not in (RUN, LABELLING):  # a labelling's row names its run: the feed reads it as a run's
        found = Seen()
    try:
        events = notify.EVENTS[action][0](row(action, detail, obj=obj), found)
    except Exception:
        return  # the feed counts the row as an error: nothing is sent
    for event in events:
        try:
            checked = notify.checked(event)
        except Unsafe:
            continue
        for kind in ("webhook", "slack", "teams", "email"):
            doc, body = rendered(checked, kind)
            leaked = [p for p in PLANTED if p in body or p in doc["text"]]
            assert not leaked, (action, kind, leaked)
