"""Stop conditions on their own: parsing, windows, counts and time limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from deltasleuth.core.types import Canonical, ChangeType, Finding, KeyResult, Outcome, Severity, SideState
from deltasleuth.runner.stop import RateWindow, StopConditions, StopMonitor

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


def test_time_limits_and_cycle_boundaries() -> None:
    c = StopConditions(max_duration_s=60, until=T0 + timedelta(hours=1), max_cycles=3)
    monitor = StopMonitor(c, T0)
    assert monitor.reason(T0 + timedelta(seconds=59)) is None
    assert monitor.reason(T0 + timedelta(seconds=60)) == "stop_condition:maxDuration"
    assert StopMonitor(StopConditions(until=T0), T0).reason(T0) == "stop_condition:until"
    assert c.at_boundary(2, T0, T0 + timedelta(seconds=30)) is None
    assert c.at_boundary(3, T0, T0 + timedelta(seconds=30)) == "stop_condition:maxCycles"
    assert c.at_boundary(1, T0, T0 + timedelta(seconds=90)) == "stop_condition:maxDuration"
