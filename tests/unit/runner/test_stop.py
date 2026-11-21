"""Stop conditions on their own: parsing, windows, counts and time limits."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

from deltasleuth.core.types import Canonical, ChangeType, Finding, KeyResult, Outcome, Severity, SideState
from deltasleuth.runner.stop import Passed, RateWindow, StopConditions, StopMonitor

T0 = datetime(2026, 9, 24, tzinfo=UTC)


def _result(outcome: Outcome, critical: int = 0) -> KeyResult:
    state = SideState.complete(Canonical(doc={}, hash="h", raw_doc={}, raw_hash="r"))
    findings = [
        Finding("K", f"/f{i}", f"$.f{i}", ChangeType.VALUE_CHANGED, severity=Severity.CRITICAL)
        for i in range(critical)
    ]
    return KeyResult("K", outcome, state, state, findings=findings)


def test_conditions_come_from_the_spec() -> None:
    stop = {
        "maxKeys": 5,
        "maxDuration": "2m",
        "until": "2026-10-01T00:00:00",
        "maxErrorRate": {"rate": 0.2, "window": 10},
    }
    c = StopConditions.from_spec({"execution": {"stop": stop}})
    assert (c.max_keys, c.max_duration_s, c.until) == (5, 120.0, datetime(2026, 10, 1, tzinfo=UTC))
    assert c.max_error_rate == RateWindow(0.2, 10) and StopConditions.from_spec({}) == StopConditions()


def test_consecutive_errors_reset_on_a_successful_call() -> None:
    monitor = StopMonitor(StopConditions(max_consecutive_errors=3), T0)
    for failed in (True, True, False, True, True):
        monitor.record_call(failed=failed)
    assert monitor.reason(T0) is None
    monitor.record_call(failed=True)
    assert monitor.reason(T0) == "stop_condition:maxConsecutiveErrors"


def test_rates_are_judged_once_the_window_is_full() -> None:
    calls = StopMonitor(StopConditions(max_error_rate=RateWindow(0.5, 10)), T0)
    for _ in range(9):
        calls.record_call(failed=True)
    assert calls.reason(T0) is None
    calls.record_call(failed=True)
    assert calls.reason(T0) == "stop_condition:maxErrorRate"
    keys = StopMonitor(StopConditions(max_inconclusive_rate=RateWindow(0.5, 10)), T0)
    for i in range(20):  # half of every window: not more than the rate
        keys.record_result(
            f"K{i}", _result(Outcome.INCONCLUSIVE if i % 2 else Outcome.MATCH), first_read=True
        )
    assert keys.reason(T0) is None
    keys.record_result("K20", _result(Outcome.INCONCLUSIVE), first_read=True)
    assert keys.reason(T0) == "stop_condition:maxInconclusiveRate"


def test_unexplained_critical_counts_the_current_results() -> None:
    monitor = StopMonitor(StopConditions(max_unexplained_critical=2), T0)
    monitor.record_result("K1", _result(Outcome.MISMATCH, critical=1), first_read=True)
    monitor.record_result("K1", _result(Outcome.MATCH), first_read=False)  # the recheck replaced it
    monitor.record_result("K2", _result(Outcome.MISMATCH, critical=1), first_read=True)
    assert monitor.reason(T0) is None
    monitor.record_result("K3", _result(Outcome.MISMATCH, critical=1), first_read=True)
    assert monitor.reason(T0) == "stop_condition:maxUnexplainedCritical"


def test_the_critical_total_follows_each_key_s_latest_count() -> None:
    """The running total equals the sum over every key's latest count, through updates of the same key; a
    key whose count is zero is not held."""
    counts = [3, 0, 2, 0, 0, 1, 4, 0, 2, 2]  # each update's count, keys K0 to K3 in turn
    monitor = StopMonitor(StopConditions(max_unexplained_critical=100), T0, {"K0": 1, "K1": 0, "K9": 2})
    latest = {"K0": 1, "K1": 0, "K9": 2}
    assert (monitor.critical_count, monitor.critical) == (3, {"K0": 1, "K9": 2})
    for i, count in enumerate(counts):
        key = f"K{i % 4}"
        monitor.record_result(key, _result(Outcome.MISMATCH, critical=count), first_read=False)
        latest[key] = count
        assert monitor.critical_count == sum(latest.values())
        assert monitor.critical == {k: n for k, n in latest.items() if n}
    assert monitor.reason(T0) is None


def test_the_critical_count_stays_linear_in_the_keys() -> None:
    """A run's every key feeds the count, on the event loop: 100,000 keys take a fraction of a second."""
    monitor = StopMonitor(StopConditions(max_unexplained_critical=10**9), T0)
    critical, clean = _result(Outcome.MISMATCH, critical=1), _result(Outcome.MATCH)
    started = time.perf_counter()
    for i in range(100_000):
        monitor.record_result(f"K{i}", critical if i % 3 == 0 else clean, first_read=False)
    assert time.perf_counter() - started < 1.0
    assert (monitor.critical_count, len(monitor.critical)) == (33_334, 33_334)


def test_a_run_without_a_critical_limit_never_retains_keys() -> None:
    monitor = StopMonitor(StopConditions(), T0)
    monitor.record_result("K1", _result(Outcome.MISMATCH, critical=1), first_read=True)
    assert not monitor.critical and monitor.critical_count == 0


def test_time_limits_and_cycle_boundaries() -> None:
    c = StopConditions(max_duration_s=60, until=T0 + timedelta(hours=1), max_cycles=3)
    monitor = StopMonitor(c, T0)
    assert monitor.reason(T0 + timedelta(seconds=59)) is None
    assert monitor.reason(T0 + timedelta(seconds=60)) == "stop_condition:maxDuration"
    assert StopMonitor(StopConditions(until=T0), T0).reason(T0) == "stop_condition:until"
    assert c.at_boundary(2, T0, T0 + timedelta(seconds=30)) is None
    assert c.at_boundary(3, T0, T0 + timedelta(seconds=30)) == "stop_condition:maxCycles"
    assert c.at_boundary(1, T0, T0 + timedelta(seconds=90)) == "stop_condition:maxDuration"


def test_a_monitor_fed_through_the_store_judges_as_one_fed_by_its_slots() -> None:
    """Passes stored as the stop feed holds them and loaded again give a monitor what the slots give it, in
    the same order: the same counts and the same first reason. A run that counts no CRITICAL finding stores
    no key."""
    conditions = StopConditions(max_consecutive_errors=4, max_unexplained_critical=3)
    direct, fed = StopMonitor(conditions, T0, {"K9": 1}), StopMonitor(conditions, T0, {"K9": 1})
    passes = [
        Passed.of("K1", (True, False), _result(Outcome.MISMATCH, critical=1), first_read=True),
        Passed.of("K2", (True, True), _result(Outcome.INCONCLUSIVE), first_read=True),
        Passed.of("K9", (), _result(Outcome.MATCH), first_read=False),  # its stored count replaced
        Passed.of("K3", (True, True), _result(Outcome.MISMATCH, critical=2), first_read=False),
    ]
    for passed in passes:
        direct.record_pass(passed)
        fed.record_pass(Passed.loaded(json.loads(json.dumps(passed.stored(keyed=True)))))
    assert direct.critical == fed.critical == {"K1": 1, "K3": 2}
    assert direct.reason(T0) == fed.reason(T0) == "stop_condition:maxConsecutiveErrors"
    assert [p.inconclusive for p in passes] == [False, True, None, None]
    assert passes[0].stored(keyed=False) == ["", [True, False], False, 1]
