"""Matching rules (docs/mvp/ALERTS.md §3): a change condition holds each cycle its fact reaches the bound,
from the first cycle; a level condition fires when it crosses or recovers, never while it stays, and a null
fact never fires; specs, modes and each kind's filter; a readiness rule's causes, programmes, states and
gates (docs/mvp/READINESS.md §8)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from deltasleuth.config import RuleSettings
from deltasleuth.core import notify
from deltasleuth.core.notify import Event, Fired

from .notified import CHANGED, GUARD_PAUSE, block, drifted, ended, missed, paused, readiness, row, seen


def rule(**fields: Any) -> RuleSettings:
    return TypeAdapter(RuleSettings).validate_python(
        {"name": "r", "events": ["cycle.ended"], "channels": ["c"]} | fields
    )


def level(value: str | None, before: str | None, **facts: Any) -> Event:
    previous = None if before is None else {"cycle": 11, "value": before}
    levels = {
        "mismatch_rate": value,
        "indeterminate_rate": None,
        "coverage": None,
        "unexplained_critical": None,
    }
    return ended(
        levels=levels, previous={name: None for name in levels} | {"mismatch_rate": previous}, **facts
    )


def test_a_change_holds_each_cycle_it_reaches_the_bound() -> None:
    gaps = rule(when={"new_root_causes_at_least": 1})
    assert notify.fires(gaps, ended(new_root_causes=2)) == Fired(held=("new_root_causes_at_least",))
    assert notify.fires(gaps, ended(new_root_causes=2)) is not None  # again: a change is not a crossing
    assert notify.fires(gaps, ended(new_root_causes=0)) is None
    assert notify.fires(gaps, ended(new_root_causes=None, unavailable=["new_root_causes"])) is None


def test_the_first_cycle_of_a_batch_run_fires_its_gaps() -> None:
    first = notify.checked(
        Event(
            "cycle.ended",
            ended(cycle=1, new_root_causes=2, gone_root_causes=0, baseline_from=None).facts,
            block(mode="batch"),
            "customer-profile",
            "batch",
        )
    )
    assert notify.fires(rule(when={"new_root_causes_at_least": 1}), first) is not None


def test_a_level_fires_when_it_crosses_and_recovers() -> None:
    above = rule(when={"mismatch_rate_above": "0.001"})
    assert notify.fires(above, level("0.0021", "0.0009")) == Fired(crossed=("mismatch_rate_above",))
    assert notify.fires(above, level("0.0021", "0.0015")) is None  # stays above
    assert notify.fires(above, level("0.0005", "0.0021")) == Fired(recovered=("mismatch_rate_above",))
    assert notify.fires(above, level("0.0005", "0.0001")) is None  # stays below
    assert notify.fires(above, level("0.001", "0.0009")) is None  # the bound itself is not above
    below = rule(when={"coverage_below": "0.99"})
    covered = ended(
        levels={
            "mismatch_rate": None,
            "indeterminate_rate": None,
            "coverage": "0.98",
            "unexplained_critical": 3,
        },
        previous={
            "mismatch_rate": None,
            "indeterminate_rate": None,
            "coverage": None,
            "unexplained_critical": {"cycle": 11, "value": 0},
        },
    )
    assert notify.fires(below, covered) == Fired(crossed=("coverage_below",))
    critical = rule(when={"unexplained_critical_at_least": 3})
    assert notify.fires(critical, covered) == Fired(crossed=("unexplained_critical_at_least",))


def test_unavailable_above_above_is_one_crossing() -> None:
    """A null fact never holds, crosses or recovers; the baseline is the latest earlier cycle holding it."""
    above = rule(when={"mismatch_rate_above": "0.001"})
    assert notify.fires(above, level(None, None)) is None  # cycle 1: unavailable
    assert notify.fires(above, level("0.002", None)) == Fired(crossed=("mismatch_rate_above",))  # no baseline
    assert notify.fires(above, level("0.003", "0.002")) is None  # cycle 3: its baseline is cycle 2's


def test_specs_modes_and_filters() -> None:
    event = ended()
    assert notify.fires(rule(specs=["customer-*"]), event) is not None
    assert notify.fires(rule(specs=["cards-*"]), event) is None
    assert notify.fires(rule(modes=["batch"]), event) is None
    one_shot = notify.checked(
        Event(
            "run.finished",
            {"status": "completed", "reason": None, "cycle": 1},
            block(mode="one_shot"),
            "customer-profile",
            "one_shot",
        )
    )
    assert notify.fires(rule(events=["run.finished"]), one_shot) is None  # only when named
    assert notify.fires(rule(events=["run.finished"], modes=["one_shot"]), one_shot) is not None
    stopped = notify.checked(
        Event("run.stopped", {"cycle": 2, "condition": "maxErrorRate"}, block(), "customer-profile", "soak")
    )
    assert notify.fires(rule(events=["run.stopped"], reasons=["stop_condition:maxErrorRate"]), stopped)
    assert notify.fires(rule(events=["run.stopped"], reasons=["stop_condition:until"]), stopped) is None
    state = notify.checked(
        Event(
            "licence.state",
            {
                "installation_id": "I-3f0a1b2c3d4e5f60",
                "from": "valid",
                "to": "ending",
                "licence_id": None,
                "ends": None,
            },
        )
    )
    assert notify.fires(rule(events=["licence.state"], states=["ending"]), state)
    assert notify.fires(rule(events=["licence.state"], states=["expired"]), state) is None
    assert notify.fires(rule(events=["licence.state"], states=["open"]), state) is None  # LICENCE.md §19


def test_a_missed_time_is_matched_by_the_spec_and_mode_of_its_row() -> None:
    """``schedule.missed`` has no run block (docs/mvp/SCHEDULES.md §8): its row's detail names the spec and
    the mode rules match; no filter of another kind is for it."""
    event = missed()  # of customer-profile, batch
    assert notify.fires(rule(events=["schedule.missed"]), event) == Fired()
    assert notify.fires(rule(events=["schedule.missed"], specs=["customer-*"]), event) == Fired()
    assert notify.fires(rule(events=["schedule.missed"], specs=["cards-*"]), event) is None
    assert notify.fires(rule(events=["schedule.missed"], modes=["soak"]), event) is None
    assert notify.fires(rule(events=["schedule.missed"], modes=["soak"]), missed(mode="soak")) == Fired()
    assert notify.fires(rule(events=["run.failed", "cycle.ended"]), event) is None
    (pause,) = paused()  # the worker's pause before a time is due: matched from its own row likewise
    assert notify.fires(rule(events=["schedule.missed"], specs=["customer-*"]), pause) == Fired()
    assert notify.fires(rule(events=["schedule.missed"], specs=["cards-*"]), pause) is None
    assert notify.fires(rule(events=["schedule.missed"], modes=["soak"]), pause) is None
    assert notify.fires(rule(events=["schedule.missed"], modes=["soak"]), *paused(mode="soak")) == Fired()
    for theirs in ({"reasons": ["cancelled"]}, {"states": ["expired"]}, {"statuses": ["failed"]}):
        with pytest.raises(ValidationError, match=" is only for "):
            rule(events=["schedule.missed"], **theirs)
    with pytest.raises(ValidationError, match=r"when is only for cycle\.ended"):
        rule(events=["schedule.missed"], when={"new_root_causes_at_least": 1})
    with pytest.raises(ValidationError):
        rule(events=["schedule.missed"], reasons=["overlap"])  # no reason of a rule's


def test_a_schema_change_is_matched_as_a_run_s_event_and_its_pause_by_its_reason() -> None:
    """docs/mvp/PRECHECKS.md §9: ``schema.drift`` is a run's event, matched by spec and mode and taking no
    filter; the guard's pause is a ``run.paused`` whose reason a rule may name."""
    event = drifted()  # of a soak of customer-profile
    assert notify.fires(rule(events=["schema.drift"]), event) == Fired()
    assert notify.fires(rule(events=["schema.drift"], specs=["customer-*"], modes=["soak"]), event) == Fired()
    assert notify.fires(rule(events=["schema.drift"], specs=["cards-*"]), event) is None
    assert notify.fires(rule(events=["schema.drift"], modes=["batch"]), event) is None
    assert notify.fires(rule(events=["run.paused"]), event) is None
    for theirs in ({"reasons": ["run_paused_schema_drift"]}, {"states": ["expired"]}):
        with pytest.raises(ValidationError, match=" is only for "):
            rule(events=["schema.drift"], **theirs)
    (pause,) = notify.EVENTS["paused"][0](row("paused", GUARD_PAUSE), seen())
    pause = notify.checked(pause)
    assert notify.fires(rule(events=["run.paused"]), pause) == Fired()
    assert notify.fires(rule(events=["run.paused"], reasons=["run_paused_schema_drift"]), pause) == Fired()
    assert notify.fires(rule(events=["run.paused"], reasons=["run_paused_auth_error"]), pause) is None
    assert notify.fires(rule(events=["schema.drift"]), pause) is None
    assert notify.severity(pause, False) == "alert" and notify.severity(event, False) == "alert"
    assert notify.severity(drifted(paused=False), False) == "warning"


def test_severity() -> None:
    assert notify.severity(ended(), False) == "warning"  # different > 0
    quiet = ended(counters={**ended().facts["counters"], "different": 0, "indeterminate": 0})
    assert notify.severity(quiet, False) == "info" and notify.severity(quiet, True) == "warning"
    for condition, expected in (("maxErrorRate", "alert"), ("until", "warning")):
        assert notify.severity(Event("run.stopped", {"condition": condition}), False) == expected


def ready(**fields: Any) -> Any:
    return rule(events=["readiness.changed"], **{"causes": ["cycle"]} | fields)


def test_a_readiness_rule_needs_causes_and_its_keys_are_its_own() -> None:
    with pytest.raises(ValidationError, match=r"readiness\.changed needs causes"):
        rule(events=["readiness.changed"])
    for key, value in (("causes", ["cycle"]), ("programmes", ["cutover-*"]), ("gates", ["fresh"])):
        with pytest.raises(
            ValidationError, match=r"programmes, gates and causes are only for readiness\.changed"
        ):
            rule(**{key: value})
    with pytest.raises(ValidationError, match="expected a licence state or a readiness state"):
        ready(states=["ready"])


def test_a_readiness_rule_matches_causes_programmes_states_and_gates() -> None:
    event = readiness()  # cutover-q4 / customer-profile: not ready -> parity ready; targets and convergence
    assert notify.fires(ready(), event) == Fired()
    assert notify.fires(ready(causes=["time", "cycle"]), event) is not None  # a listed cause is the row's
    assert notify.fires(ready(causes=["time", "signoff"]), event) is None
    assert notify.fires(ready(specs=["customer-*"]), event) is not None  # specs match the pair
    assert notify.fires(ready(specs=["cards-*"]), event) is None
    assert notify.fires(ready(programmes=["cutover-*"]), event) is not None
    assert notify.fires(ready(programmes=["decom-*", "cutover-q1"]), event) is None
    assert notify.fires(ready(states=["parity_ready", "evidenced"]), event) is not None  # the state reached
    assert notify.fires(ready(states=["not_ready"]), event) is None
    assert notify.fires(ready(gates=["fresh", "targets"]), event) is not None  # a listed gate changed
    assert notify.fires(ready(gates=["fresh"]), event) is None
    assert (
        notify.fires(ready(programmes=["cutover-*"], states=["parity_ready"], gates=["fresh"]), event) is None
    )
    assert (
        notify.fires(rule(events=["licence.state"], states=["parity_ready"]), event) is None
    )  # another kind
    # A pair in no programme matches only a rule without programmes, whatever its globs.
    loose = readiness(programme=None, entity=None)
    assert notify.fires(ready(), loose) is not None and notify.fires(ready(programmes=["*"]), loose) is None


def test_a_readiness_rule_fires_when_any_cause_it_lists_is_the_row_s() -> None:
    baseline = readiness(**{"from": None, "causes": ["baseline"], "changed": []})
    assert notify.fires(ready(), baseline) is None  # never implied: a baseline names nothing else
    assert notify.fires(ready(causes=["baseline"]), baseline) is not None
    recorded = readiness(causes=["config"])  # the settings alone moved the pair
    assert notify.fires(ready(), recorded) is None
    assert notify.fires(ready(causes=["config"]), recorded) is not None
    # A fall is never dropped for the causes it names besides: the settings' first evaluation measured a
    # cycle too, and a rule that lists either cause takes the row.
    both = readiness(causes=["cycle", "config"])
    assert notify.fires(ready(), both) is not None
    assert notify.fires(ready(causes=["config"]), both) is not None
    assert notify.fires(ready(causes=["time", "signoff"]), both) is None
    assert notify.fires(ready(causes=["baseline", "config"]), readiness()) is None  # neither is the row's
    assert CHANGED["causes"] == ["cycle"]
