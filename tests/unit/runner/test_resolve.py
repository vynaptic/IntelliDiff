"""Retries to resolution: a sign-off member no read compared is read again from its
first pass, at most ``resolve.times``; a comparable read is never read away, nor a read no retry changes; a
retry's first read is judged as a first read and counted once; contrast keys and unsampled runs are never
retried."""

from __future__ import annotations

import json
from typing import Any

import pytest

from deltasleuth.runner.runs import create_run
from deltasleuth.runner.stop import StopMonitor
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import UNAVAILABLE, Harness, ScriptedService, doc, spec_text
from .sampled import (
    DUPLICATED,
    KEYS,
    Tables,
    bucket,
    customer,
    cycle_row,
    halves,
    keys,
    listed,
    tabled,
    tables_fixture,  # noqa: F401 (the on_tables fixture)
)
from .test_scan import databases  # noqa: F401 (a fixture)

pytestmark = pytest.mark.db
UNRECHECKED = ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"]  # an INCONCLUSIVE first read is final
NOT_FOUND = {"error": "NOT_FOUND"}


def _spec(times: int = 2, outcomes: list[str] | None = None, **fields: Any) -> str:
    doc_ = json.loads(listed(resolve={"times": times, "delay": "0s"}, **fields))
    if outcomes is not None:
        doc_["recheck"]["outcomes"] = outcomes
    return json.dumps(doc_)


async def _sampled(
    h: Harness, service: ScriptedService, spec: str
) -> tuple[repo.Row, dict[str, dict[str, Any]]]:
    """A run of ``spec`` over the listing of ``KEYS`` to its end, each key agreeing unless scripted."""
    service.listings = [KEYS]
    for key in KEYS:
        service.scripts["a"].setdefault(key, [(200, doc(key))])
        service.scripts["b"].setdefault(key, [(200, doc(key))])
    run_id = await create_run(h.ctx, await h.spec(spec), "batch", who=TESTER)
    run = await h.finish(run_id)
    return run, {k["key"]: k for k in await keys(h, run_id)}


@pytest.mark.parametrize(
    ("outcomes", "times", "calls", "retried"),
    [
        (None, 2, 6, ["recheck_failed", "recheck_failed"]),  # each attempt: a first read and its recheck
        (UNRECHECKED, 2, 3, ["error_a", "error_a"]),  # each attempt: a first read, final
        (None, 0, 2, None),  # resolve.times 0: as today
    ],
    ids=["recheck_failed", "error", "times 0"],
)
async def test_a_member_no_read_compared_is_read_again_at_most_resolve_times(
    harness: Harness, service: ScriptedService, outcomes: list[str] | None, times: int, calls: int,
    retried: list[str] | None,
) -> None:  # fmt: skip
    service.script("a", "K001", UNAVAILABLE)
    run, taken = await _sampled(harness, service, _spec(times, outcomes))
    results = await harness.results(run["run_id"])
    assert (run["status"], taken["K001"]["retries"], taken["K001"]["state"]) == ("completed", times, "done")
    assert service.calls[("a", "K001")] == calls
    assert results["K001"]["outcome"] == "INCONCLUSIVE"
    assert results["K001"]["attrs"].get("retried") == retried
    assert {k: v["retries"] for k, v in taken.items() if k != "K001"} == dict.fromkeys(KEYS[1:], 0)
    stratum = (await cycle_row(harness, run["run_id"]))["sample"]["strata"][0]  # type: ignore[index]
    assert (stratum["label"], stratum["retried"], stratum["unresolved"]) == ("half=low", 1 if times else 0, 1)


async def test_a_discrepancy_whose_rechecks_fail_is_never_read_again(
    harness: Harness, service: ScriptedService
) -> None:
    service.script("a", "K002", (200, doc("K002", status="A")))
    service.script("b", "K002", (200, doc("K002", status="B")), UNAVAILABLE)
    run, taken = await _sampled(harness, service, _spec())
    result = (await harness.results(run["run_id"]))["K002"]
    assert (result["outcome"], result["inconclusive_reason"], result["first_outcome"]) == (
        "INCONCLUSIVE",
        "recheck_failed",
        "MISMATCH",
    )
    assert (taken["K002"]["retries"], service.calls[("b", "K002")], "retried" in result["attrs"]) == (
        0,
        2,
        False,
    )
    stratum = (await cycle_row(harness, run["run_id"]))["sample"]["strata"][0]  # type: ignore[index]
    assert (stratum["x"], stratum["unresolved"], stratum["n"]) == (0, 1, 3)


def _tagged(key: str, n: int) -> dict[str, Any]:
    return doc(key) | {"tags": [f"t{i}" for i in range(n)]}


NEVER = {
    "a_changed": ({"a": [(200, doc("K003", status="A1")), (200, doc("K003", status="A2"))],
                   "b": [(200, doc("K003", status="B"))]}, "DRIFT", "a_changed"),
    "b_converged": ({"a": [(200, doc("K003"))], "b": [(200, doc("K003", status="OLD")), (200, doc("K003"))]},
                    "DRIFT", "b_converged"),
    "truncated": ({"a": [(200, _tagged("K003", 50))], "b": [(200, _tagged("K003", 50))]}, "INCONCLUSIVE",
                  "recheck_failed"),
    "absent_both": ({"a": [(404, NOT_FOUND)], "b": [(404, NOT_FOUND)]}, "INCONCLUSIVE", "absent_both"),
}  # fmt: skip


@pytest.mark.parametrize("case", NEVER)
async def test_drift_truncation_and_absence_are_never_read_again(
    harness: Harness, service: ScriptedService, case: str
) -> None:
    replies, outcome, reason = NEVER[case]
    for side, script in replies.items():
        service.script(side, "K003", *script)
    run, taken = await _sampled(harness, service, _spec())
    result = (await harness.results(run["run_id"]))["K003"]
    assert (result["outcome"], result["drift_reason"] or result["inconclusive_reason"]) == (outcome, reason)
    assert taken["K003"]["retries"] == 0 and service.calls[("a", "K003")] == 2


async def test_a_key_its_recheck_leaves_truncated_is_above_the_api_cap(
    harness: Harness, service: ScriptedService
) -> None:
    """Under the default recheck policy: the truncated first read is rechecked, truncated again, and ends
    recheck_failed, yet the key counts api_uncomparable, never unresolved: its stratum fails for keys above
    the API cap."""
    for side in "ab":
        service.script(side, "K003", (200, _tagged("K003", 50)))
    run, _ = await _sampled(harness, service, _spec())
    result = (await harness.results(run["run_id"]))["K003"]
    low = (await cycle_row(harness, run["run_id"]))["sample"]["strata"][0]  # type: ignore[index]
    assert (result["inconclusive_reason"], result["side_a_reason"], result["side_b_reason"]) == (
        "recheck_failed",
        "truncated",
        "truncated",
    )
    assert (low["label"], low["api_uncomparable"], low["unresolved"], low["verdict"]) == (
        "half=low",
        1,
        0,
        "failed",
    )
    assert low["why"] == [
        "has 1 key(s) above the API cap that were not compared (truncated): declare pagination for side a's"
        " and side b's fetch in a new version and draw again"
    ]


async def test_an_ambiguous_row_is_never_read_again(on_tables: Tables, service: ScriptedService) -> None:
    for key in [f"C{n:03d}" for n in range(1, 21)]:
        service.script("b", key, (200, customer(key)))
    _, harness = on_tables(DUPLICATED)
    async with harness as h:
        run_id = await create_run(h.ctx, await h.spec(tabled()), "batch", who=TESTER)
        await h.finish(run_id)
        result = (await h.results(run_id))["C003"]
        taken = {k["key"]: k for k in await keys(h, run_id)}
    assert (result["outcome"], result["inconclusive_reason"], taken["C003"]["retries"]) == (
        "INCONCLUSIVE",
        "recheck_failed",
        0,
    )  # its first read: identity_ambiguous


async def test_a_retried_keys_discrepancy_is_confirmed_as_a_first_read_is(
    harness: Harness, service: ScriptedService
) -> None:
    service.script("a", "K004", UNAVAILABLE, UNAVAILABLE, (200, doc("K004", status="A")))
    service.script("b", "K004", (200, doc("K004", status="B")))
    run, taken = await _sampled(harness, service, _spec())
    result = (await harness.results(run["run_id"]))["K004"]
    assert (result["outcome"], result["first_outcome"], result["confirmation"]) == (
        "MISMATCH",
        "MISMATCH",
        "rechecked",
    )
    assert (result["attrs"]["retried"], taken["K004"]["retries"], service.calls[("a", "K004")]) == (
        ["recheck_failed"],
        1,
        4,
    )
    stratum = (await cycle_row(harness, run["run_id"]))["sample"]["strata"][0]  # type: ignore[index]
    assert (stratum["x"], stratum["counters"]["outstanding"], stratum["unresolved"], stratum["retried"]) == (
        1,
        0,
        0,
        1,
    )


async def test_first_reads_are_counted_once(
    harness: Harness, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    firsts: list[tuple[str, bool]] = []
    record = StopMonitor.record_result

    def recorded(self: StopMonitor, key: str, result: Any, *, first_read: bool) -> None:
        firsts.append((key, first_read))
        record(self, key, result, first_read=first_read)

    monkeypatch.setattr(StopMonitor, "record_result", recorded)
    service.script("a", "K001", UNAVAILABLE)
    await _sampled(harness, service, _spec(outcomes=UNRECHECKED))
    assert [first for key, first in firsts if key == "K001"] == [True, False, False]
    assert all(first for key, first in firsts if key != "K001")


async def test_contrast_keys_are_never_read_again(harness: Harness, service: ScriptedService) -> None:
    # Every key's effective rate is 1.0: the low keys its heap of 2 leaves out are contrast.
    everyone = {"name": "all", "cel": "true", "buckets": [bucket("Y", values=[True], rate=1.0)]}
    for key in KEYS[:4]:
        service.script("a", key, UNAVAILABLE)
    run, taken = await _sampled(
        harness, service, _spec(outcomes=UNRECHECKED, strata=[*halves(2, 4), everyone])
    )
    low = [k for k in taken.values() if k["signoff_stratum"] == "half=low"]
    assert run["status"] == "completed" and len(low) == 4
    assert sorted((k["signoff_member"], k["retries"]) for k in low) == [
        (False, 0),
        (False, 0),
        (True, 2),
        (True, 2),
    ]


async def test_an_unsampled_run_is_never_read_again(harness: Harness, service: ScriptedService) -> None:
    service.script("a", "K001", UNAVAILABLE)
    run_id = await create_run(harness.ctx, await harness.spec(spec_text(["K001"])), "batch", who=TESTER)
    await harness.finish(run_id)
    result = (await harness.results(run_id))["K001"]
    [key] = await keys(harness, run_id)
    assert (result["inconclusive_reason"], "retried" in result["attrs"]) == ("recheck_failed", False)
    assert (key["retries"], key["signoff_member"], key["stratum"], service.calls[("a", "K001")]) == (
        0,
        False,
        None,
        2,
    )
