"""Cut-over readiness, pure (docs/mvp/READINESS.md §3 to §7, §9): what a cycle's measures say of it at each
tier; the streak, the candidate cycle and the earliest cycle that could complete it; every gate's status
and text, the states and the score table; the settings' policy, its hash and its deviations; the marks of
a brief's points."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

from deltasleuth import approvals
from deltasleuth.config import ReadinessSettings
from deltasleuth.core import readiness, sampling
from deltasleuth.core.readiness import STATES
from deltasleuth.core.types import Outcome
from deltasleuth.reports import accuracy, adjudicate
from deltasleuth.runner import counters

from .ready import (
    ADJUDICATION,
    ALLOWS,
    APPROVAL,
    DRAW,
    LABELLING,
    LISTED,
    OTHER,
    PACK,
    PAIR,
    POLICY,
    READ,
    RUN,
    SAMPLED,
    SIGNOFF,
    T0,
    cycle,
    errored,
    gate,
    judged,
    measures,
    pack,
    passed,
    point,
    said,
    seen,
    series,
    signoff,
)

AT = f"cycle {{}} of run {RUN}"
V14, V15 = "1.4.0 (spec 12)", "1.5.0 (spec 15)"
NEWER = {"spec_id": 15, "version": "1.5.0"}
SUPPRESSED = "; {} key(s) suppressed (known differences or noise only)"  # N6: beside each discrepant count


def policy(**block: Any) -> readiness.Policy:
    """The pair's policy under a readiness settings ``block``."""
    return readiness.policy(TypeAdapter(ReadinessSettings).validate_python(block), PAIR)


HIGH = policy(gates={"tier": "high"})


def clean(cycles: int, **changes: Any) -> list[dict[str, Any]]:
    return [point(n, **changes) for n in range(1, cycles + 1)]


def test_the_definition_and_the_names_other_modules_hold() -> None:
    assert readiness.DEFINITION.startswith("Readiness is a set of named gates, each met or not on observed")
    assert "not a probability" in readiness.DEFINITION and "not a forecast" in readiness.DEFINITION
    assert len(readiness.GATES) == 12 and len(STATES) == 7
    assert (readiness.EXIT_MINIMUM, readiness.MODES) == (adjudicate.EXIT_MINIMUM, approvals.MODES)
    assert tuple(sampling.TARGETS) == ("standard", "high")


# -- measures (§4) and the pooled rule (§5) ---------------------------------------------------------------


def test_census_allowance_exact() -> None:
    """⌊t·n⌋: 1,000 keys allow 1 at 0.1% and none at 0.01%; 999 allow none."""
    fails = {
        (total, different): measures(total=total, compared=total, different=different)["fails"]
        for total in (1_000, 999)
        for different in (0, 1, 2)
    }
    assert fails[1_000, 1] == {"standard": [], "high": ["targets"]}
    assert fails[1_000, 2] == fails[999, 1] == {"standard": ["targets"], "high": ["targets"]}
    assert fails[1_000, 0] == fails[999, 0] == {"standard": [], "high": []}
    within = gate(judged([point(1, total=1_000, compared=1_000, different=1)]), "targets")
    assert (within["status"], within["value"], within["allowed"], within["over"]) == ("met", 1, 1, 0)
    assert within["text"] == (
        f"{AT.format(1)} has 1 discrepant or unresolved key(s) of 1,000 observed whole, within the 1 its"
        f" pooled 0.1% target allows (per-stratum targets are the approver's){SUPPRESSED.format(0)}"
    )
    accepted = {"different": 1_204, "absent_both": 1_000, "indeterminate": 1_000, "suppressed": 400_000}
    over = gate(judged([point(1, **accepted)]), "targets")
    assert (over["status"], over["value"], over["allowed"], over["over"]) == ("not_met", 1_204, 999, 205)
    assert over["text"] == (
        f"{AT.format(1)} has 1,204 discrepant or unresolved key(s) of 1,000,000 observed whole; its pooled"
        f" 0.1% target allows 999: 205 over{SUPPRESSED.format('400,000')}"
    )
    # Unresolved keys count with the discrepant ones; the absent and the API-capped have gates of their own.
    stale = judged([point(1, total=5_004, compared=4_992, different=0, indeterminate=12, absent_both=12)])
    capped = judged([point(1, different=0, indeterminate=812, api_uncomparable=812)])
    assert said(stale)["targets"] == (
        "not_met",
        f"{AT.format(1)} has 12 key(s) absent on both sides, over the 5 its 0.1% target allows of 5,004: the"
        " key source is stale",
    )
    assert said(capped)["targets"] == (
        "not_met",
        f"{AT.format(1)} has 812 key(s) above the API cap that were not compared (truncated): declare"
        " pagination for that side's fetch in a new version",
    )
    unresolved = measures(total=1_000, compared=996, different=0, indeterminate=4, absent_both=1)
    assert unresolved["fails"]["standard"] == ["targets"]  # 3 unresolved of 999, which allow none


def test_frame_never_census() -> None:
    """A hash sample's frame counting side a makes its basis ``sample`` (bottom_k) or ``rate_sample``,
    never ``census``; a frame that does not enumerate side a leaves it a key list."""
    drawn = measures(**SAMPLED, method="bottom_k")
    rate = measures(**SAMPLED, method="rate")
    side_b = measures(**SAMPLED, method="bottom_k", problem="its frame does not enumerate side a")
    assert [m["basis"] for m in (drawn, rate, side_b)] == ["sample", "rate_sample", "keys"]
    assert (drawn["evidence"], drawn["fails"]["standard"]) == (True, [])
    assert rate["fails"] == side_b["fails"] == {"standard": ["population"], "high": ["population", "targets"]}
    texts = {
        method: said(judged([point(7, **SAMPLED, method=method)]))["population"]
        for method in ("bottom_k", "rate")
    }
    assert texts["bottom_k"] == (
        "met",
        f"{AT.format(7)} compared bottom_k sample {DRAW}, its frame reading side a whole",
    )
    assert texts["rate"] == (
        "not_met",
        f"{AT.format(7)} compared rate sample {DRAW}, which monitors: draw bottom_k sizes to judge a"
        " population",
    )


def test_partial_critical_breaks_streak() -> None:
    """A partial cycle is shown and skipped, unless a key of it has an unexplained CRITICAL finding: an
    observed defect counts."""
    partial = {"cancelled": 3, "compared": 999_997}
    skipped = judged([*clean(4), point(5, **partial)])
    assert skipped["state"] == "signoff_ready" and skipped["convergence"]["streak"] == 4
    assert skipped["evidence"]["cycle"] == 4  # the partial cycle is no evidence
    broken = judged([*clean(4), point(5, **partial, unexplained_critical=2)])
    assert measures(**partial, unexplained_critical=2)["fails"]["standard"] == ["critical"]
    assert (broken["state"], broken["convergence"]["streak"]) == ("not_ready", 0)
    assert said(broken)["critical"] == (
        "not_met",
        f"{AT.format(5)}, which is not evidence (partial), has 2 key(s) with unexplained CRITICAL findings"
        " (bug rules count as unexplained): an observed defect counts",
    )
    listed = {"keys_source": "explicit", "problem": LISTED, "unexplained_critical": 1}
    assert said(judged([*clean(4), point(5, **listed)]))["critical"][1] == (
        f"{AT.format(5)}, which is not evidence (a key list whose census does not count side a), has 1 key(s)"
        " with unexplained CRITICAL findings (bug rules count as unexplained): an observed defect counts"
    )
    own = judged([*clean(4), point(5, unexplained_critical=3)])
    assert said(own)["critical"] == (
        "not_met",
        f"{AT.format(5)} has 3 key(s) with unexplained CRITICAL findings (bug rules count as unexplained);"
        " the gate allows 0",
    )


def test_partial_certain_failure() -> None:
    """A cycle lacking only keys cancelled or owed a recheck fails targets once its final counts are over
    the pooled allowance whatever its unread keys show; a partial scan or a cut key set never does."""
    partial = {"cancelled": 10, "compared": 998_786, "different": 1_204}
    assert measures(**partial)["fails"]["standard"] == ["targets"]
    assert measures(**partial | {"different": 1_000})["fails"]["standard"] == []  # 1,000 are allowed
    assert measures(outstanding=10, absent_both=1_001, indeterminate=1_001)["fails"]["standard"] == [
        "targets"
    ]
    assert measures(cancelled=1, api_uncomparable=1, indeterminate=1)["fails"]["standard"] == ["targets"]
    for unread in ({"whole": False}, {"whole": False, "cut": True}, {"keys_source": "list", "cut": True}):
        assert measures(**partial, **unread)["fails"]["standard"] == []
    assert measures(**partial, keys_source="explicit", problem=LISTED)["fails"]["standard"] == []
    broken = judged([*clean(4), point(5, **partial)])
    assert (broken["state"], broken["convergence"]["streak"]) == ("not_ready", 0)
    assert said(broken)["targets"] == (
        "not_met",
        f"partial {AT.format(5)} already has 1,204 discrepant or unresolved key(s) of 1,000,000, over the"
        f" 1,000 its pooled 0.1% target allows whatever its unread keys show{SUPPRESSED.format(0)}",
    )
    forms = judged(
        [*clean(4), point(5, cancelled=1, indeterminate=2_002, absent_both=1_001, api_uncomparable=1_001)]
    )
    assert said(forms)["targets"][1] == (
        f"partial {AT.format(5)} already has 1,001 key(s) absent on both sides, over the 1,000 its 0.1%"
        f" target allows of 1,000,000: the key source is stale; partial {AT.format(5)} already has 1,001"
        " key(s) above the API cap that were not compared (truncated): declare pagination for that side's"
        " fetch in a new version"
    )


def test_a_cycle_that_compared_no_key_fails_for_certain() -> None:
    """A cycle whose every key ended unresolved (a side was down) left none unread: its pooled count is
    over the allowance for certain, so it fails targets and ends the streak, as it would with one key
    compared."""
    down = {"compared": 0, "different": 0, "indeterminate": 1_000_000}
    none = measures(**down)
    assert (none["incomplete"], none["evidence"]) == (["nothing_compared"], False)
    assert none["fails"] == {"standard": ["targets"], "high": ["targets"]}
    one = measures(compared=1, different=0, indeterminate=999_999)
    assert (one["evidence"], one["fails"]) == (True, none["fails"])
    assert measures(compared=0, different=0, total=0)["fails"]["standard"] == []  # no key, no failure
    broken = judged([*clean(3), point(4, **down)])
    assert (broken["state"], broken["evidence"]["cycle"], broken["convergence"]["streak"]) == (
        "not_ready",
        3,
        0,
    )
    assert said(broken)["targets"] == (
        "not_met",
        f"{AT.format(4)}, which compared no key, has 1,000,000 discrepant or unresolved key(s) of 1,000,000,"
        f" over the 1,000 its pooled 0.1% target allows{SUPPRESSED.format(0)}",
    )
    assert said(broken)["convergence"][1].startswith(
        f"0 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate ({AT.format(4)}"
        " failed targets)"
    )
    stale = {"compared": 0, "different": 0, "indeterminate": 5, "absent_both": 5, "total": 5}
    absent = judged([*clean(3), point(4, **stale)])
    assert said(absent)["targets"][1] == (
        f"{AT.format(4)}, which compared no key, has 5 key(s) absent on both sides, over the 0 its 0.1%"
        " target allows of 5: the key source is stale"
    )


def test_explicit_list_evidence_only_with_census() -> None:
    """A key list given at creation is evidence only when its census counts side a; otherwise its counts
    describe those keys only, and it is shown and skipped."""
    given_ = {"keys_source": "explicit", "total": 5_000, "compared": 5_000, "different": 1_204}
    listed = measures(**given_, problem=LISTED)
    counted = measures(**given_, problem=None)
    inline = measures(**given_ | {"keys_source": "list"}, problem=f"{AT.format(5)} {LISTED}")
    assert (listed["complete"], listed["evidence"], listed["basis"]) == (True, False, "keys")
    assert listed["fails"] == {"standard": [], "high": []}
    assert (counted["evidence"], counted["basis"], counted["fails"]["standard"]) == (
        True,
        "census",
        ["targets"],
    )
    assert (inline["evidence"], inline["fails"]["standard"]) == (True, ["population", "targets"])
    skipped = judged([*clean(4), point(5, **given_, problem=LISTED)])
    assert (skipped["state"], skipped["evidence"]["cycle"], skipped["convergence"]["streak"]) == (
        "signoff_ready",
        4,
        4,
    )
    spec_list = judged([point(5, **given_ | {"keys_source": "list"}, problem=f"{AT.format(5)} {LISTED}")])
    assert said(spec_list)["population"] == (
        "not_met",
        f"{AT.format(5)} {LISTED}: its counts describe those keys only",
    )
    assert said(spec_list)["targets"][1] == (
        f"{AT.format(5)} has 1,204 discrepant or unresolved key(s) of 5,000 listed keys; its pooled 0.1%"
        f" target allows 5: 1,199 over{SUPPRESSED.format(0)}"
    )
    pending = point(
        5, keys_source="list", problem=f"{AT.format(5)} is not a census: side a's key census is pending"
    )
    assert said(judged([pending]))["population"][1] == (
        f"{AT.format(5)} is not a census: side a's key census is pending: its counts describe those keys only"
        " (side a is counted only with packs or readiness configured, PACK §1.3)"
    )


def test_old_counters_late_measures_and_failed_ones_are_unknown() -> None:
    old = measures(compared=None, different=None)  # counters from before the fields a measure reads
    late = measures(late=True, whole=None, unexplained_critical=None, results=None)
    assert (old["basis"], old["evidence"], old["incomplete"]) == ("unknown", False, ["nothing_compared"])
    assert old["fails"] == late["fails"] == {"standard": ["unknown"], "high": ["unknown"]}
    assert (late["basis"], late["evidence"]) == ("unknown", True)  # its tombstone's counters are complete
    failed = measures(own=False, **dict.fromkeys(readiness.COUNTS))
    assert (failed["incomplete"], failed["basis"], failed["fails"]["high"]) == (["no_counters"], "census", [])
    lost = {"late": True, "whole": None, "unexplained_critical": None, "results": None}
    purged = judged([point(n, **lost) for n in (1, 2, 3)])
    assert purged["state"] == "unknown" and purged["blocking"][:3] == [
        "population",
        "critical",
        "convergence",
    ]
    texts = said(purged)
    assert texts["population"] == (
        "unknown",
        f"{AT.format(3)} ended before readiness was measured (014) and its scan state was purged: its"
        " population is not known",
    )
    assert texts["critical"] == (
        "unknown",
        f"{AT.format(3)} ended before readiness was measured (014) and its findings were purged: not"
        " measured",
    )
    assert texts["convergence"] == (
        "unknown",
        f"the streak of {V14} reaches {AT.format(3)}, whose findings were purged before 014",
    )
    alone = judged([point(3, **lost)])  # one cycle cannot be three, whatever was purged of it
    assert (alone["state"], said(alone)["convergence"][0]) == ("not_ready", "not_met")


# -- the streak, the candidate and the earliest cycle (§3, §6) --------------------------------------------


def test_streak_ends_at_version() -> None:
    """Convergence never crosses versions: the current version is the newest evidence point's, and its
    streak begins after the newest evidence point of another."""
    newer = [point(n, run=OTHER, **NEWER) for n in (6, 7)]
    changed = judged([*clean(5), *newer])
    assert (changed["version"], changed["spec_id"], changed["convergence"]["streak"]) == ("1.5.0", 15, 2)
    assert said(changed)["convergence"] == (
        "not_met",
        f"2 of the 3 evidence cycles in a row that {V15} needs meet every per-cycle gate; no live soak of"
        f" {V15} will add cycles",
    )
    assert changed["state"] == "not_ready"
    # Reverted content reuses the older spec id, which is current again once a cycle of it ends.
    reverted = judged([*clean(5), *newer, point(8)])
    assert (reverted["spec_id"], reverted["convergence"]["streak"]) == (12, 1)
    found = readiness.streak(series([*clean(5), *newer, point(8)]), POLICY)
    assert (found.kind, found.boundary["cycle"], found.boundary["run_id"]) == ("version", 7, OTHER)  # type: ignore[index]
    assert not found.holds(cycle(clean(5)[-1])) and found.holds(cycle(point(8)))
    assert not found.holds(cycle(point(9, cancelled=3)))  # a cycle that is no evidence is in no streak
    assert not found.holds(cycle(point(9)) | {"readiness": {"format": 1, "excluded": "captured_samples"}})


def test_the_streak_ends_at_what_was_never_measured() -> None:
    unmeasured = {"cycles": 2, "oldest": {"run_id": RUN, "cycle": 3, "ended_at": point(3)["ended_at"]}}
    waiting = judged([*clean(2), point(4), point(5)], unmeasured=unmeasured)
    assert (waiting["state"], waiting["convergence"]["streak"]) == ("unknown", 2)
    assert said(waiting)["convergence"] == (
        "unknown",
        f"the streak of {V14} reaches {AT.format(3)}, which is not measured yet",
    )
    failed = judged([*clean(2), errored(3), point(4)])
    assert said(failed)["convergence"] == (
        "unknown",
        f"the streak of {V14} reaches {AT.format(3)}, which could not be measured (KeyError)",
    )
    enough = judged([*clean(2), errored(3), *(point(n) for n in (4, 5, 6))])
    assert enough["state"] == "signoff_ready"  # three clean evidence cycles since
    unjudged = judged([*clean(2), point(3, **SAMPLED | {"sample_status": "unjudged"}, method="bottom_k")])
    assert said(unjudged)["convergence"][1].endswith(
        f"{AT.format(3)}, whose sample ended without a judgement"
    )
    assert said(unjudged)["targets"] == (
        "unknown",
        f"the sample {DRAW} of {AT.format(3)} ended without a judgement",
    )


def test_convergence_is_not_met_when_its_unknown_points_cannot_complete_it() -> None:
    """Unknown only while the unknown and unmeasured points, were they clean, could complete the streak:
    behind them, a cycle that failed leaves too few, and not met is certain."""
    failed, cleared = point(1, different=5_000), f"({AT.format(1)} failed targets"
    before = judged([failed, point(2)])
    assert (before["state"], said(before)["convergence"][0]) == ("not_ready", "not_met")
    assert said(before)["convergence"][1].startswith(
        f"1 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate {cleared}); "
    )
    errored_ = judged([failed, point(2), errored(3)])
    assert (errored_["state"], errored_["convergence"]["streak"]) == ("not_ready", 0)  # 2 of 3 at the most
    assert said(errored_)["convergence"] == (
        "not_met",
        f"0 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate {cleared}; the"
        f" streak reaches {AT.format(3)}, which could not be measured (KeyError))",
    )  # no schedule clause while its measure is tried again: what it reads decides the earliest cycle
    final = judged([failed, point(2), errored(3, final=True)])
    assert said(final)["convergence"][1] == (
        f"{said(errored_)['convergence'][1]}; no live soak of {V14} will add cycles"
    )
    waiting = {"cycles": 1, "oldest": {"run_id": RUN, "cycle": 3, "ended_at": point(3)["ended_at"]}}
    unmeasured = judged([failed, point(2)], unmeasured=waiting)
    assert unmeasured["state"] == "not_ready" and said(unmeasured)["convergence"][1] == (
        f"0 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate {cleared}; the"
        f" streak reaches {AT.format(3)}, which is not measured yet)"
    )
    assert judged([failed, point(2)], unmeasured=waiting | {"cycles": 2})["state"] == "unknown"
    # Two unknown points after the failure could complete the three: unknown, until they are measured.
    could = judged([failed, point(2), errored(3), errored(4)])
    assert (could["state"], said(could)["convergence"]) == (
        "unknown",
        ("unknown", f"the streak of {V14} reaches {AT.format(4)}, which could not be measured (KeyError)"),
    )
    found = readiness.streak(series([failed, point(2), errored(3), errored(4)]), POLICY)
    assert (found.k, found.kind, found.most, found.failed["cycle"]) == (0, "unknown", 3, 1)  # type: ignore[index]
    # A pair too young to converge is not met too, whatever its unknown point would have been.
    young = judged([point(1), errored(2)])
    assert said(young)["convergence"] == (
        "not_met",
        f"0 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate (the streak reaches"
        f" {AT.format(2)}, which could not be measured (KeyError))",
    )


def test_measures_of_another_format_are_an_unknown_point() -> None:
    """§4: a record whose format is not this build's is no evidence and ends the streak as unknown, with
    a reason naming its format, whatever its fields say."""
    other = point(4) | {"readiness": point(4)["readiness"] | {"format": 2}}  # evidence true, fails nothing
    item = judged([*clean(3), other])
    assert (item["state"], item["evidence"]["cycle"], item["convergence"]["streak"]) == ("unknown", 3, 0)
    assert said(item)["convergence"] == (
        "unknown",
        f"the streak of {V14} reaches {AT.format(4)}, whose measures are of format 2, which this version does"
        " not read",
    )
    assert [p["cycle"] for p in item["trend"]] == [1, 2, 3]
    damaged = {"run_id": RUN, "cycle": 5, "ended_at": point(5)["ended_at"], "readiness": {"format": 3}}
    rows = readiness.trend([*clean(3), other, damaged], "standard")
    assert [(row["cycle"], row["fails"], row["skipped"], row["total"]) for row in rows[-2:]] == [
        (4, ["unknown"], True, None),
        (5, ["unknown"], True, None),
    ]
    assert readiness.trend([*clean(3), other], "standard", "day")[0]["cycle"] == 3
    alone = judged([other])
    assert alone["state"] == "no_evidence" and gate(alone, "population")["text"] == (
        f"1 cycle(s) ended, none evidence; the newest: {AT.format(4)} holds measures of format 2, which this"
        " version does not read"
    )
    unread = readiness.unevaluated(PAIR, POLICY, "KeyError", T0)
    assert (unread["state"], unread["score"], unread["blocking"]) == (
        "unknown",
        {"passed": 0, "required": 10},
        [name for name in readiness.GATES if name not in ("graph", "accuracy")],
    )
    why = f"{PAIR} could not be evaluated (KeyError): see the server log"
    assert unread["notes"] == [why] and {found["text"] for found in unread["gates"]} == {why}
    assert set(unread) == set(judged(clean(3))) and unread["open"]


def test_signed_cycle_4000_back_in_streak() -> None:
    """A sign-off is in the streak when its cycle is of the current version and ended after the boundary:
    one comparison, whatever its age; the streak itself counts W cycles at most."""
    points = clean(4_400)
    signed = points[399]
    item = judged(points, cycles=[cycle(signed), cycle(points[-1])], signoffs=[signoff(signed)])
    assert (item["state"], item["candidate"]["cycle"], item["signoff_id"]) == ("signed_off", 400, SIGNOFF)
    assert item["convergence"] == {"streak": 60, "needed": 3, "window": 60}
    assert (item["evidence"]["cycle"], item["regressed"]) == (4_400, False)
    assert said(item)["convergence"][1] == (
        f"the newest 60 evidence cycles of {V14} meet every per-cycle gate: at least the 3 the gate needs"
    )
    assert said(item)["signoff"] == ("met", f"sign-off {SIGNOFF} of cycle 400 approved on 2026-10-03")
    wide = judged(points, policy=policy(gates={"convergence": {"cycles": 50}}))
    assert wide["convergence"] == {"streak": 150, "needed": 50, "window": 150}


def test_new_cycle_keeps_adjudicated_candidate() -> None:
    """In the streak an approved sign-off's cycle is the candidate, else an open request's, else the newest
    cycle holding work a person did, else the evidence cycle."""
    points = clean(6)
    worked = cycle(points[2], worked=True)
    newest = cycle(points[5], adjudication=None)
    item = judged(points, cycles=[worked, newest])
    assert item["candidate"] == {"run_id": RUN, "cycle": 3} and item["evidence"]["cycle"] == 6
    assert said(item)["adjudication"] == (
        "met",
        f"adjudication {ADJUDICATION} of cycle 3, scored by a signed-in person, passed",
    )
    assert said(item)["signoff"][1].startswith(f"request sign-off of {AT.format(3)} on the Run screen")
    asked = judged(
        points, cycles=[worked, cycle(points[3]), newest], signoffs=[signoff(points[3], "requested")]
    )
    assert asked["candidate"]["cycle"] == 4
    assert said(asked)["signoff"] == (
        "not_met",
        f"sign-off {SIGNOFF} requested for cycle 4 awaits a rule_approver",
    )
    both = [signoff(points[3], "requested"), signoff(points[1], "approved", APPROVAL)]
    signed = judged(points, cycles=[worked, cycle(points[3]), cycle(points[1]), newest], signoffs=both)
    assert signed["candidate"]["cycle"] == 2 and signed["state"] == "signed_off"
    # Once a cycle after it fails, the adjudicated cycle is outside the streak: the evidence cycle is judged.
    broken = [*points[:3], point(4, different=5_000), *points[4:]]
    later = judged(broken, cycles=[worked, newest])
    assert later["candidate"]["cycle"] == 6 and later["convergence"]["streak"] == 2
    assert said(later)["adjudication"][0] == "not_met"


def test_sampled_tier_below_policy() -> None:
    """A sampled cycle is judged by its stored judgement; under a high policy every stratum must have been
    judged at high."""
    sampled = point(3, **SAMPLED, method="bottom_k")
    assert sampled["readiness"]["fails"] == {"standard": [], "high": ["targets"]}
    standard = judged([sampled])
    assert said(standard)["targets"] == (
        "met",
        f"sample {DRAW} of {AT.format(3)} passed every stratum at its tier",
    )
    assert gate(standard, "targets")["value"] is None and standard["trend"][0]["sampled"]
    assert (standard["trend"][0]["value"], standard["trend"][0]["allowed"]) == (None, None)
    high = judged([sampled], policy=HIGH)
    assert high["state"] == "not_ready" and high["deviations"] == ["tier high (default standard)"]
    assert said(high)["targets"] == (
        "not_met",
        f"stratum retail of {AT.format(3)} was judged at standard (0.1%); the pair's policy is high (0.01%)",
    )
    why = (
        "is over its 0.1% target: its one-sided 95% upper bound is 0.2101% (2 discrepant of 2,995 cleanly"
        " compared keys)"
    )
    judgement = {"status": "failed", "strata": [{"label": "retail", "verdict": "failed", "why": [why]}]}
    failed = judged([point(3, **SAMPLED | {"sample_status": "failed"}, method="bottom_k", sample=judgement)])
    assert said(failed)["targets"] == ("not_met", f"stratum retail of {AT.format(3)} {why}")
    assert said(failed)["sample_ledger"] == (
        "unknown",
        f"the draw discipline of draw {DRAW} of {AT.format(3)} is read once every parity gate is met",
    )


def test_the_sample_ledger_gate() -> None:
    sampled = [point(n, **SAMPLED, method="bottom_k") for n in (1, 2, 3)]
    assert judged(sampled)["state"] == "parity_ready"  # its ledger is not read yet
    kept = judged(sampled, ledger=[])
    assert said(kept)["sample_ledger"] == (
        "met",
        f"draw {DRAW} of {AT.format(3)} meets the draw discipline (SAMPLING §9)",
    )
    assert (kept["state"], kept["score"]) == ("signoff_ready", {"passed": 8, "required": 10})
    earlier = (
        f"draw D-X (spec customer-profile-2, run {OTHER} cycle 2) did not pass before draw {DRAW} was made"
        " and was not closed before it: close it with the fix, then sign off a draw made after the close"
    )
    open_ = judged(sampled, ledger=[earlier])
    assert said(open_)["sample_ledger"] == ("not_met", earlier) and open_["state"] == "parity_ready"
    relabelled = [*sampled[:2], point(3, **SAMPLED | {"selection_ok": False}, method="bottom_k")]
    assert said(judged(relabelled))["sample_ledger"] == (
        "not_met",
        f"the keys of {AT.format(3)} are not draw {DRAW}'s selection: a sample is never extended, trimmed or"
        " relabelled",
    )
    unchecked = [*sampled[:2], point(3, **SAMPLED | {"selection_ok": None}, method="bottom_k")]
    assert (
        said(judged(unchecked, ledger=[]))["sample_ledger"][0] == "unknown"
    )  # its keys were never re-hashed
    assert said(judged(clean(3)))["sample_ledger"] == ("not_applicable", f"{AT.format(3)} is not sampled")


def labelling(labels: dict[str, int], items: int, **changes: Any) -> dict[str, Any]:
    """A labelling of cycle 3 as governance gives it: its uniform discrepant items' ``labels``, of
    ``items``."""
    rows = [
        {"population": "discrepant", "selected_for": "uniform", "root_cause_id": "*", "label": label,
         "basis": "" if label == "UNDECIDABLE" else "SYSTEM_OF_RECORD", "engine_outcome": "MISMATCH",
         "reason": "NO_TRUTH" if label == "UNDECIDABLE" else "", "rules": ""}
        for label, n in labels.items()
        for _ in range(n)
    ]  # fmt: skip
    counts = {
        "discrepant": {"items": items, "keys": items, "causes": 0, "drawn": len(rows)},
        "accepted": {"keys": 0, "by_rule": {}, "drawn": 0}, "agreed": {"keys": 0, "drawn": 0},
        "indeterminate": 0, "not_compared": 0, "matched_by_row_hash": 0,
    }  # fmt: skip
    selection = {"counts": counts, "causes": [], "uncovered": 0, "names_side": None, "unlocatable": False}
    truth = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01", "names_side": None}
    return {
        "labelling_id": LABELLING, "run_id": RUN, "cycle": 3, "drawn_at": T0 + timedelta(days=1),
        "labelled_at": T0 + timedelta(days=3), "labelled_by": "user:U-0123456789abcdef",
        "truth_source": truth["source"], "truth_as_of": "2026-10-01", "role_a": "legacy", "role_b": "new",
        "accuracy": accuracy.tally(rows, selection, truth), "attested": None,
    } | changes  # fmt: skip


def test_accuracy_near_parity() -> None:
    """A labelling labels discrepant items: near parity a candidate has too few to judge; a census of them is
    judged on all its decided items; a labelling that labels none counts as none."""
    gated = policy(gates={"accuracy": {}})
    assert gated.deviations == ("accuracy 50 decided, 0 where side b was wrong (default not required)",)

    def read(different: int, found: dict[str, Any] | None = None) -> tuple[str, str]:
        points = clean(3, different=different)
        item = judged(points, policy=gated, cycles=[cycle(points[-1], labelling=found)])
        return said(item)["accuracy"]

    under = "under the 50 the accuracy gate judges"
    assert read(12) == ("not_applicable", f"{AT.format(3)} has 12 discrepant key(s), {under}")
    assert read(0) == ("not_applicable", f"{AT.format(3)} has no discrepant key, {under}")
    none = (
        f"no labelling of {AT.format(3)} labels its discrepant items: draw and label one (Labelled accuracy,"
        " Run screen)"
    )
    assert read(60) == ("not_met", none)
    assert read(60, labelling({}, 60) | {"accuracy": {"discrepant": None}}) == ("not_met", none)
    head = (  # readiness names no person: neither who drew it nor who labelled it
        f"Labelling {LABELLING} drawn 2026-10-02, labelled 2026-10-04 by a person against"
        " CBS-PROD.account_master as of 2026-10-01; no sign-off attests it."
    )
    allows = (
        "which allows 0 decided items where side b was wrong and needs 50 decided (all, for a census of the"
        " discrepant items)"
    )
    census = read(60, labelling({"B_RIGHT": 40, "UNDECIDABLE": 20}, 60))  # every item labelled: 40 decided
    assert census == (
        "met",
        f"labelling {LABELLING} of cycle 3 meets the accuracy gate, {allows}\n{head}\n"
        "b (new) wrong (truth favoured a, or neither): 0 of 40 decided (0%, every item labelled: exact; 0%"
        " to 33.34% counting the undecidable either way).",
    )
    # A census that decided nothing never meets the gate, however few its decided items may be.
    undecided = read(60, labelling({"UNDECIDABLE": 60}, 60))
    assert undecided == (
        "not_met",
        f"labelling {LABELLING} of cycle 3 decided 0 items, under the 1 the gate needs\n{head}\n"
        "b (new) wrong (truth favoured a, or neither): 0 of 0 decided (no item decided).",
    )
    assert read(60, labelling({"B_RIGHT": 1, "UNDECIDABLE": 59}, 60))[0] == "met"
    few = read(600, labelling({"B_RIGHT": 40, "UNDECIDABLE": 20}, 600))
    assert few[0] == "not_met" and few[1].split("\n")[0] == (
        f"labelling {LABELLING} of cycle 3 decided 40 items, under the 50 the gate needs"
    )
    wrong = read(600, labelling({"B_RIGHT": 58, "A_RIGHT": 1, "BOTH_WRONG": 1}, 600, attested=SIGNOFF))
    assert wrong[0] == "not_met" and wrong[1].split("\n") == [
        f"labelling {LABELLING} of cycle 3 does not meet the accuracy gate, {allows}",
        head.replace("; no sign-off attests it.", f", attested by sign-off {SIGNOFF}."),
        "b (new) wrong (truth favoured a, or neither): 2 of 60 decided (3.333%, 95% interval 0.4062% to"
        " 11.53%).",
    ]
    lenient = policy(gates={"accuracy": {"min_decided": 60, "max_b_wrong": 2}})
    points = clean(3, different=600)
    found = labelling({"B_RIGHT": 58, "A_RIGHT": 1, "BOTH_WRONG": 1}, 600)
    item = judged(points, policy=lenient, cycles=[cycle(points[-1], labelling=found)])
    assert said(item)["accuracy"][0] == "met" and gate(item, "accuracy")["required"]
    assert said(judged(points))["accuracy"] == ("not_applicable", "no accuracy gate is configured")
    assert not gate(judged(points), "accuracy")["required"]


SOAK = {"run_id": RUN, "cycle": 15, "status": "running", "stop_reason": None, "started_at": T0}


def soak(**changes: Any) -> dict[str, Any]:
    """A live soak in its cycle 15, a cycle an hour since ``T0``; ``stop``: its spec's stop conditions."""
    execution = {"cycleInterval": "1h", "stop": changes.pop("stop", {})}
    return SOAK | {"execution": execution} | changes


def test_earliest() -> None:
    """The cycle of a live soak that could complete the streak: running, or scheduled (a schedule, never a
    forecast); or why no soak will get there."""
    two, one = clean(2), clean(1)  # one and two evidence cycles short of the three
    now = T0 + timedelta(hours=14, minutes=20)  # cycle 15 runs; 16 is due at 15:00

    def clause(points: list[dict[str, Any]], *soaks: dict[str, Any], at: Any = now) -> str:
        status, text = said(judged(points, soaks=soaks, now=at))["convergence"]
        assert status == "not_met"
        return text.split("; ", 1)[1]

    assert (
        clause(two, soak())
        == f"cycle 15 of run {RUN} is the earliest that can complete them: it is running now"
    )
    scheduled = (
        f"cycle 16 of run {RUN} is the earliest that can complete them: it is scheduled to start about"
        " 2026-10-01 15:00 UTC, cycles starting every 1h, and completes them when it ends complete (a"
        " schedule, not a forecast)"
    )
    assert clause(one, soak()) == scheduled
    # A run's current cycle waits for its due time: scheduled, not running, until then.
    daily = soak(cycle=3, execution={"cycleInterval": "1d", "stop": {}})  # cycle 3 is due two days in
    assert clause(two, daily, at=T0 + timedelta(hours=3)) == (
        f"cycle 3 of run {RUN} is the earliest that can complete them: it is scheduled to start about"
        " 2026-10-03 00:00 UTC, cycles starting every 1d, and completes them when it ends complete (a"
        " schedule, not a forecast)"
    )
    assert clause(two, daily, at=T0 + timedelta(days=2)).endswith("it is running now")
    late = clause(one, soak(), at=T0 + timedelta(hours=20))  # behind its schedule: no earlier than now
    assert "scheduled to start about 2026-10-01 20:00 UTC" in late
    assert clause(one, soak(stop={"maxCycles": 15})) == (
        f"run {RUN} stops after cycle 15 (maxCycles) before it can complete them"
    )
    assert clause(two, soak(stop={"maxCycles": 15})) == clause(two, soak())  # cycle 15 itself completes them
    assert clause(one, soak(stop={"until": "2026-10-01T14:30:00Z"})) == (
        f"run {RUN} stops after cycle 15 (until) before it can complete them"
    )
    assert clause(one, soak(stop={"maxDuration": "15h"})) == (
        f"run {RUN} stops after cycle 15 (maxDuration) before it can complete them"
    )
    assert clause(one, soak(stop={"until": "2026-10-01T15:30:00Z"})) == scheduled
    paused = soak(run_id=OTHER, status="paused", stop_reason="run_paused_approval")
    assert clause(one, paused) == f"run {OTHER} is paused: run_paused_approval"
    assert clause(one, soak(run_id=OTHER, status="paused")) == f"run {OTHER} is paused: manual"
    assert clause(one) == f"no live soak of {V14} will add cycles"
    # Of several live soaks, the earliest counts: one running now, then the first scheduled.
    behind = soak(run_id=OTHER, cycle=3, started_at=T0 + timedelta(hours=10))  # its cycle 4 is due at 13:00
    assert clause(one, paused, soak(), behind).startswith(f"cycle 4 of run {OTHER} is the earliest")
    assert clause(two, paused, soak(stop={"maxCycles": 15}), behind).startswith(f"cycle 15 of run {RUN}")
    full = said(judged(one, soaks=[soak()], now=now))["convergence"][1]
    assert full.startswith(
        f"1 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate; "
    )
    failed = said(judged([point(13, different=5_000), point(14)], soaks=[soak()], now=now))["convergence"][1]
    assert failed.startswith(
        f"1 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate ({AT.format(13)}"
        " failed targets); cycle 16 of run"
    )


def test_closed_as_of() -> None:
    """The evidence cycle's age is judged against the database's now; a closed pair is judged as of the end
    of the day it was closed on."""
    points = clean(3)  # the newest ended on 2026-10-01 at 03:00
    fresh = judged(points, now=T0 + timedelta(days=2, hours=3))
    assert said(fresh)["fresh"] == (
        "met",
        f"the evidence cycle (3 of run {RUN}) ended 2 days ago, within the gate's 7 days",
    )
    stale = judged(points, now=T0 + timedelta(days=9, hours=4))
    assert said(stale)["fresh"] == ("not_met", f"{AT.format(3)} ended 9 days ago, over the gate's 7 days")
    assert (stale["state"], stale["blocking"], gate(stale, "fresh")["over"]) == (
        "not_ready",
        ["fresh", "signoff", "pack"],
        176_400,
    )
    hourly = judged(points, policy=policy(gates={"fresh": {"max_age": "36h"}}), now=T0 + timedelta(hours=8))
    assert said(hourly)["fresh"][1].endswith("ended 5 hours ago, within the gate's 36 hours")
    listed = {"spec": PAIR, "entity": "customer", "closed": "2026-10-02"}
    closed = policy(programmes=[{"name": "cutover-q4", "pairs": [listed]}])
    assert (closed.programme, closed.entity, closed.closed) == ("cutover-q4", "customer", date(2026, 10, 2))
    before = judged(points, policy=closed, now=T0 + timedelta(hours=12))
    assert before["open"] and before["notes"] == []
    after = judged(points, policy=closed, now=T0 + timedelta(days=40))
    assert said(after)["fresh"][0] == "met" and after["state"] == "signoff_ready"  # as of 2026-10-03 00:00
    assert said(after)["fresh"][1].endswith("ended 1 day ago, within the gate's 7 days")
    assert not after["open"] and after["notes"] == ["closed on 2026-10-02: judged as of that day"]
    assert after["closed"] == date(2026, 10, 2)
    # Judged as of a past day, a pair is told no schedule: its live soaks and their cycles are today's.
    running = soak(cycle=40)
    short = [point(1, different=5_000), point(2)]
    open_ = judged(short, policy=closed, soaks=[running], now=T0 + timedelta(hours=12))
    assert "scheduled to start" in said(open_)["convergence"][1]
    assert said(judged(short, policy=closed, soaks=[running], now=T0 + timedelta(days=30)))[
        "convergence"
    ] == (
        "not_met",
        f"1 of the 3 evidence cycles in a row that {V14} needs meet every per-cycle gate ({AT.format(1)}"
        " failed targets)",
    )
    rolled = readiness.programmes(
        [before, after | {"pair": "legacy-accounts"}, judged(points) | {"pair": "x"}]
    )
    assert rolled == [
        {"programme": "cutover-q4", "pairs": 1, "ready": 1, "worst": "signoff_ready",
         "entities": [{"entity": "customer", "states": {"signoff_ready": 1}}]},
        {"programme": None, "pairs": 1, "ready": 1, "worst": "signoff_ready",
         "entities": [{"entity": None, "states": {"signoff_ready": 1}}]},
    ]  # fmt: skip


# -- sign-offs, packs and what the installation allows (§5, D9) -------------------------------------------


def test_signoff_credit_regressed_superseded() -> None:
    points = clean(12)
    signed = cycle(points[-1])
    credited = judged(points, cycles=[signed], signoffs=[signoff(points[-1])])
    assert (credited["state"], credited["regressed"], credited["notes"]) == ("signed_off", False, [])
    assert said(credited)["pack"] == (
        "not_met",
        f"sign-off {SIGNOFF} has no issued evidence pack: issue one on the Run screen",
    )
    packed = judged(points, cycles=[signed | {"packs": [pack()]}], signoffs=[signoff(points[-1])])
    assert (packed["state"], packed["pack_id"], packed["score"]) == (
        "evidenced",
        PACK,
        {"passed": 9, "required": 9},
    )
    assert said(packed)["pack"] == ("met", f"pack {PACK} issued 2026-10-02 for sign-off {SIGNOFF}")
    revoked = judged(points, cycles=[signed | {"packs": [pack("revoked")]}], signoffs=[signoff(points[-1])])
    assert said(revoked)["pack"] == ("not_met", f"pack {PACK} was revoked on 2026-10-04: issue a new one")
    # A later cycle fails: the sign-off is outside the streak, and the pair regressed.
    failing = [*points, point(13, different=5_000), point(14)]
    regressed = judged(failing, signoffs=[signoff(points[-1])])
    assert (regressed["state"], regressed["regressed"], regressed["signoff_id"]) == ("not_ready", True, None)
    assert regressed["notes"] == [
        f"regressed: {V14} was signed off ({SIGNOFF}, cycle 12) and the pair is now below signed off"
    ]
    assert said(regressed)["signoff"] == (
        "not_met",
        f"cycle 12 was signed off ({SIGNOFF}), but cycle 13 fails targets: the sign-off is outside the"
        " streak",
    )
    assert said(regressed)["pack"] == ("not_met", "no sign-off to pack yet")
    # A new version is current: the older version's sign-off credits nothing, and nothing regressed.
    newer = [*clean(4), *(point(n, run=OTHER, **NEWER) for n in (5, 6))]
    superseded = judged(newer, signoffs=[signoff(newer[3])])
    assert (superseded["version"], superseded["regressed"]) == ("1.5.0", False)
    assert said(superseded)["signoff"] == (
        "not_met",
        f"cycle 4 of {V14} was signed off ({SIGNOFF}); {V15} is current: sign off a cycle of it",
    )
    undone = judged(points, signoffs=[signoff(points[-1], "revoked")])
    assert undone["state"] == "signoff_ready" and said(undone)["signoff"][1].startswith("request sign-off of")
    stale = judged([*points, point(13, different=5_000)], signoffs=[signoff(points[-1], "revoked")])
    assert stale["candidate"]["cycle"] == 13 and said(stale)["signoff"] == (
        "not_met",
        f"{AT.format(13)} is not signed off: the gates before it are not all met",
    )
    own = [*points[:-1], point(12, different=5_000)]
    assert said(judged(own, signoffs=[signoff(own[-1], "revoked")]))["signoff"] == (
        "not_met",
        f"sign-off {SIGNOFF} was revoked on 2026-10-03 (see Approvals)",
    )


def test_only_an_evidence_cycle_is_the_candidate() -> None:
    """A sign-off, an adjudication or a labelling of a cycle that is no evidence point never makes it the
    candidate: a key list given at creation without a census, or a captured-sample replay, credits nothing."""
    points = clean(3)
    listed = point(4, run=OTHER, keys_source="explicit", problem=LISTED, total=5, compared=5, different=0)
    assert (listed["readiness"]["evidence"], listed["readiness"]["basis"]) == (False, "keys")
    before = judged(points)
    assert (before["state"], before["candidate"]) == ("signoff_ready", {"run_id": RUN, "cycle": 3})
    # (a) The five keys' cycle is signed off: the pair is not, and has not regressed.
    signed = judged(
        [*points, listed],
        cycles=[cycle(points[-1]), cycle(listed)],
        signoffs=[signoff(listed)],
    )
    assert (signed["state"], signed["candidate"], signed["signoff_id"], signed["regressed"]) == (
        "signoff_ready",
        {"run_id": RUN, "cycle": 3},
        None,
        False,
    )
    assert signed["score"] == before["score"] and said(signed)["signoff"] == (
        "not_met",
        f"cycle 4 of run {OTHER} was signed off ({SIGNOFF}), but is not evidence (a key list whose census"
        " does not count side a): only a sign-off of an evidence cycle counts; request sign-off of"
        f" {AT.format(3)} on the Run screen, the newest cycle in the streak whose adjudication passed (or the"
        " newest evidence cycle): every other gate is met",
    )
    unscored = judged(
        [*points, listed],
        cycles=[cycle(points[-1], adjudication=None), cycle(listed)],
        signoffs=[signoff(listed)],
    )
    assert unscored["state"] == "parity_ready" and said(unscored)["signoff"][1].endswith(
        "only a sign-off of an evidence cycle counts"
    )
    failing = judged([*points, listed, point(5, different=5_000)], signoffs=[signoff(listed)])
    assert (failing["regressed"], failing["notes"]) == (False, [])  # it was never signed off
    assert (
        said(failing)["signoff"][1]
        == f"{AT.format(5)} is not signed off: the gates before it are not all met"
    )
    # (b) A person scored an adjudication of a captured-sample replay, whose cycle is marked excluded.
    replay = cycle(points[-1], worked=True) | {
        "run_id": OTHER, "cycle": 1, "ended_at": T0 + timedelta(hours=5),
        "readiness": {"format": 1, "excluded": "captured_samples"},
    }  # fmt: skip
    adjudicated = judged(points, cycles=[cycle(points[-1], adjudication=None), replay])
    assert adjudicated["candidate"] == {"run_id": RUN, "cycle": 3}
    assert said(adjudicated)["adjudication"][0] == "not_met" and adjudicated["state"] == "parity_ready"
    partial = cycle(point(4, cancelled=3), worked=True)  # nor is adjudicated work on a partial cycle
    assert judged(points, cycles=[cycle(points[-1]), partial])["candidate"]["cycle"] == 3


def test_a_candidate_of_another_run_is_named_with_its_run() -> None:
    """The evidence cycle and the candidate may be of two runs: the texts that say ``cycle N`` then say
    whose."""
    older, newer = [point(n, run=OTHER) for n in (1, 2, 3)], [point(n) for n in (4, 5)]
    worked = cycle(older[1], worked=True)
    item = judged([*older, *newer], cycles=[worked, cycle(newer[-1])])
    assert (item["candidate"], item["evidence"]["run_id"]) == ({"run_id": OTHER, "cycle": 2}, RUN)
    assert said(item)["adjudication"] == (
        "met",
        f"adjudication {ADJUDICATION} of cycle 2 of run {OTHER}, scored by a signed-in person, passed",
    )
    asked = judged([*older, *newer], cycles=[worked], signoffs=[signoff(older[1], "requested")])
    assert said(asked)["signoff"][1] == (
        f"sign-off {SIGNOFF} requested for cycle 2 of run {OTHER} awaits a rule_approver"
    )
    signed = judged([*older, *newer], cycles=[worked], signoffs=[signoff(older[1])])
    assert said(signed)["signoff"] == (
        "met",
        f"sign-off {SIGNOFF} of cycle 2 of run {OTHER} approved on 2026-10-03",
    )
    regressed = judged([*older, *newer, point(6, different=5_000)], signoffs=[signoff(older[1])])
    assert regressed["notes"] == [
        f"regressed: {V14} was signed off ({SIGNOFF}, cycle 2 of run {OTHER}) and the pair is now below"
        " signed off"
    ]
    assert said(regressed)["signoff"][1] == (
        f"cycle 2 of run {OTHER} was signed off ({SIGNOFF}), but cycle 6 fails targets: the sign-off is"
        " outside the streak"
    )


def test_stored_signoff_survives_expiry_and_downgrade() -> None:
    """A stored sign-off and pack count whatever the auth mode, ``packs`` or the licence allow now (D9)."""
    points = clean(3)
    stored = {"cycles": [cycle(points[-1], packs=[pack()])], "signoffs": [signoff(points[-1])]}
    expired = dict.fromkeys(ALLOWS, False)
    for installation in (seen(allows=expired), seen("tokens", False, expired), seen("none", False, None)):
        item = judged(points, installation=installation, **stored)
        assert (item["state"], item["signoff_id"], item["pack_id"]) == ("evidenced", SIGNOFF, PACK)
    downgraded = said(judged(points, installation=seen(allows=ALLOWS | {"signoff": False, "packs": False})))
    assert downgraded["signoff"] == (
        "unavailable",
        "sign-off needs an Enterprise licence, which this installation's does not allow"
        " (docs/mvp/LICENCE.md §6)",
    )
    assert downgraded["pack"] == (
        "unavailable",
        "evidence packs need an Enterprise licence, which this installation's does not allow",
    )
    unpacked = judged(points, installation=seen(packs=False))
    assert said(unpacked)["pack"] == ("unavailable", "evidence packs are not configured (packs)")
    assert (unpacked["state"], unpacked["score"]) == ("signoff_ready", {"passed": 7, "required": 8})


def test_licence_unknown_keeps_last() -> None:
    """When the standing cannot be read, sign-off and pack keep what the licence last decided of them (not
    met, or unavailable); a stored fact still counts, and nothing else is ever met: a sign-off revoked
    since reads unknown."""
    points, unread = clean(3), seen(allows=None)
    last = {"gates": {"signoff": "unavailable", "pack": "not_met"}}
    kept = judged(points, installation=unread, last=last)
    assert said(kept)["signoff"] == ("unavailable", f"{readiness.LICENCE_UNREAD}: last known")
    assert said(kept)["pack"] == ("not_met", f"{readiness.LICENCE_UNREAD}: last known")
    assert kept["state"] == "parity_ready"
    first = judged(points, installation=unread)
    assert said(first)["signoff"] == said(first)["pack"] == ("unknown", readiness.LICENCE_UNREAD)
    assert readiness.LICENCE_UNREAD == "the licence standing could not be read (see the server log)"
    signed = judged(points, installation=unread, signoffs=[signoff(points[-1])], last=last)
    assert (signed["state"], said(signed)["signoff"][0]) == ("signed_off", "met")
    recorded = {"gates": {"signoff": "met", "pack": "met"}}  # the pair was evidenced when last recorded
    for revoked in ([signoff(points[-1], "revoked")], []):
        undone = judged(points, installation=unread, signoffs=revoked, last=recorded)
        assert (undone["state"], undone["score"], undone["signoff_id"]) == (
            "parity_ready",
            {"passed": 7, "required": 9},
            None,
        )
        assert said(undone)["signoff"] == said(undone)["pack"] == ("unknown", readiness.LICENCE_UNREAD)
        assert undone["blocking"] == ["signoff", "pack"]
    none = judged([], installation=unread, last=recorded)  # nor without an evidence cycle
    assert [gate(none, name)["status"] for name in ("signoff", "pack")] == ["unknown", "unknown"]


def test_tokens_mode_stops_at_parity_ready() -> None:
    points = clean(3)
    started = cycle(points[-1], started="tokens", cited=None, adjudication=None)
    for mode, named in (("tokens", "tokens mode"), ("none", "developer mode")):
        item = judged(points, cycles=[started], installation=seen(mode, False, dict.fromkeys(ALLOWS, False)))
        assert (item["state"], item["score"], item["blocking"]) == (
            "parity_ready",
            {"passed": 5, "required": 5},
            [],
        )
        texts = said(item)
        assert texts["spec_approval"] == (
            "unavailable",
            f"run {RUN} started in tokens mode: only a run started under SSO is signed off",
        )
        assert texts["adjudication"] == (
            "unavailable",
            f"adjudication needs SSO (auth.oidc); this deployment runs in {named}",
        )
        assert texts["signoff"] == (
            "unavailable",
            f"sign-off needs SSO (auth.oidc); this deployment runs in {named}",
        )
        assert texts["pack"] == ("unavailable", "evidence packs are not configured (packs)")
    # Under SSO a run started in tokens mode is still never signed off.
    sso = said(judged(points, cycles=[started]))
    assert sso["signoff"] == sso["spec_approval"] and sso["adjudication"][0] == "not_met"


def test_score_table() -> None:
    """§5: the required gates counted, per installation; unavailable and not applicable ones are left out."""
    points = clean(3, results=50)  # from 50 results, an adjudication counts
    tokens = cycle(points[-1], started="tokens", cited=None, adjudication=None)
    unscored = {"cycles": [cycle(points[-1], adjudication=None)]}
    none = dict.fromkeys(ALLOWS, False)
    rows = {
        "tokens": judged(points, cycles=[tokens], installation=seen("tokens", False, none)),
        "developer": judged(points, cycles=[tokens], installation=seen("none", False, none)),
        "enterprise": judged(points, **unscored),
        "no packs": judged(points, installation=seen(packs=False)),
        "no sign-off": judged(points, installation=seen(allows=ALLOWS | {"signoff": False, "packs": False})),
        "few results": judged(clean(3, results=11)),
    }
    assert {name: item["score"]["required"] for name, item in rows.items()} == {
        "tokens": 5, "developer": 5, "enterprise": 9, "no packs": 8, "no sign-off": 7, "few results": 8,
    }  # fmt: skip
    assert said(rows["few results"])["adjudication"] == (
        "not_applicable",
        f"{AT.format(3)} has 11 per-key results, under the 50 an adjudication needs",
    )
    assert said(rows["enterprise"])["adjudication"] == (
        "not_met",
        f"no adjudication of {AT.format(3)} is scored: draw and score one before requesting sign-off, which"
        " freezes the cycle's adjudications (docs/mvp/PACK.md §5)",
    )
    assert (rows["enterprise"]["state"], rows["enterprise"]["score"]["passed"]) == ("parity_ready", 6)
    # A sampled pair adds sample_ledger; a required graph and accuracy gate add theirs.
    sampled = [point(n, **SAMPLED, method="bottom_k") for n in (1, 2, 3)]
    assert judged(sampled, ledger=[])["score"]["required"] == 10
    graphed = clean(3, graph={"computed": True} | dict.fromkeys(readiness.GRAPH_CODES, 0), different=60)
    required = policy(gates={"graph": {"required": True}, "accuracy": {}})
    assert judged(graphed, policy=required)["score"]["required"] == 11
    assert judged(graphed)["score"]["required"] == 9  # shown, never counted


def test_the_ladder_and_the_gates_that_are_not_required() -> None:
    codes = dict.fromkeys(readiness.GRAPH_CODES, 0)
    split = clean(
        3,
        graph={"computed": True}
        | codes
        | {"CLUSTER_SPLIT": 3, "CLUSTER_MERGE": 1, "GROUP_MEMBERS_DIFFER": 12, "CLUSTER_OVERSIZED": 4},
    )
    shown = judged(split)
    assert said(shown)["graph"] == (
        "not_met",
        f"{AT.format(3)} has 3 CLUSTER_SPLIT, 1 CLUSTER_MERGE, 0 CLUSTER_REARRANGE and 12"
        " GROUP_MEMBERS_DIFFER row(s); the gate allows 0",
    )
    assert shown["state"] == "signoff_ready" and "graph" not in shown["blocking"]  # shown, not required
    required = policy(gates={"graph": {"required": True}})
    assert required.deviations == ("graph required (default not required)",)
    blocked = judged(split, policy=required)
    assert (blocked["state"], blocked["blocking"][0], gate(blocked, "graph")["value"]) == (
        "not_ready",
        "graph",
        16,
    )
    oversized = clean(3, graph={"computed": True} | codes | {"CLUSTER_OVERSIZED": 2})
    assert said(judged(oversized, policy=required))["graph"] == (
        "met",
        f"{AT.format(3)} has no CLUSTER_SPLIT, CLUSTER_MERGE, CLUSTER_REARRANGE or GROUP_MEMBERS_DIFFER row",
    )
    unrun = judged(clean(3, graph={"computed": False} | codes), policy=required)
    assert said(unrun)["graph"] == ("unknown", f"graph parity (L2) did not run in {AT.format(3)}")
    assert unrun["state"] == "unknown"  # unknown is never met
    plain = judged(clean(3), policy=required)
    assert said(plain)["graph"] == ("not_applicable", f"{V14} declares no graph")
    assert plain["state"] == "signoff_ready"
    late = judged([point(3, late=True, whole=None, unexplained_critical=None, results=None)], policy=required)
    assert said(late)["graph"] == (
        "unknown",
        f"{AT.format(3)} ended before readiness was measured (014) and its graph rows were purged: not"
        " measured",
    )
    revoked = judged(clean(3), cycles=[cycle(point(3), cited={"approval_id": APPROVAL, "status": "revoked"})])
    assert said(revoked)["spec_approval"] == (
        "not_met",
        f"{AT.format(3)} ran under spec approval {APPROVAL}, since revoked",
    )
    unapproved = judged(clean(3), cycles=[cycle(point(3), cited=None)])
    assert said(unapproved)["spec_approval"] == ("not_met", f"{AT.format(3)} ran without a spec approval")
    assert said(judged(clean(3)))["spec_approval"] == (
        "met",
        f"{AT.format(3)} ran under spec approval {APPROVAL}, approved",
    )
    reasons = [
        "12 key(s) adjudicated; the exit needs at least 50",
        "2 disagreement(s) between reviewers and the engine",
    ]
    scored = {"adjudication_id": ADJUDICATION, "passed": False, "reasons": reasons}
    failed = judged(clean(3), cycles=[cycle(point(3), adjudication=scored)])
    assert said(failed)["adjudication"] == (
        "not_met",
        f"adjudication {ADJUDICATION} of cycle 3 did not pass: {reasons[0]}; {reasons[1]}",
    )


def test_no_evidence() -> None:
    """Y14: every parity gate is unknown with why the pair has no evidence cycle."""
    none = readiness.evaluate(
        PAIR, POLICY, series([]), {"signoffs": [], "cycles": [], "soaks": []}, seen(), now=T0
    )
    assert (none["state"], none["score"], none["version"]) == (
        "no_evidence",
        {"passed": 0, "required": 9},
        None,
    )
    why = f"no batch or soak cycle of {PAIR} has ended"
    assert {found["text"] for found in none["gates"]} == {why}
    assert [found["status"] for found in none["gates"]] == [
        *["unknown"] * 8, "not_applicable", "not_applicable", "not_met", "not_met",
    ]  # fmt: skip
    assert (none["evidence"], none["candidate"], none["trend"]) == (None, None, [])
    unstored = judged([], stored=False)
    assert gate(unstored, "population")["text"] == f"no spec named {PAIR} is stored"
    oldest = {"run_id": RUN, "cycle": 1, "ended_at": T0}
    waiting = judged([], unmeasured={"cycles": 2, "oldest": oldest})
    assert gate(waiting, "fresh")["text"] == "2 ended cycle(s) are not measured yet"
    many = judged([], unmeasured={"cycles": readiness.COUNTED + 1, "oldest": oldest})
    assert gate(many, "fresh")["text"] == "over 100 ended cycle(s) are not measured yet"
    tokens = judged([], installation=seen("tokens", False, dict.fromkeys(ALLOWS, False)))
    assert tokens["score"] == {"passed": 0, "required": 5}
    stopped = (
        "scanned only part of its tables (rows_a 812,000, rows_b 811,950 read; the scan stopped before the"
        " end)"
    )
    cut = "was cut at 5,000 keys (maxKeys)"
    reasons = [
        ("is partial: 3 key(s) cancelled, 0 owed a recheck", {"cancelled": 3}),
        ("is partial: 0 key(s) cancelled, 2 owed a recheck", {"outstanding": 2}),
        (
            "has no counters of its own (failed, erased or abandoned)",
            {"own": False} | dict.fromkeys(readiness.COUNTS),
        ),
        ("compared no key", {"compared": 0, "different": 0}),
        (stopped, {"whole": False, "basis_text": stopped}),
        (cut, {"whole": False, "cut": True, "basis_text": cut}),
        (
            "is a key list given at creation whose census does not count side a",
            {"keys_source": "explicit", "problem": LISTED},
        ),
    ]
    pending = f"{AT.format(2)} is not a census: side a's key census is pending"
    reasons.append(
        (
            "is a key list given at creation whose census does not count side a (side a is counted only with"
            " packs or readiness configured, PACK §1.3)",
            {"keys_source": "explicit", "problem": pending},
        )
    )
    for reason, changes in reasons:
        item = judged([point(2, **changes)])
        assert item["state"] == "no_evidence"
        assert gate(item, "targets")["text"] == (
            f"1 cycle(s) ended, none evidence; the newest: {AT.format(2)} {reason}"
        )
    cut_list = judged(
        [
            point(n, keys_source="list", cut=True, total=5_000, compared=5_000, problem="x")
            for n in range(1, 13)
        ]
    )
    assert gate(cut_list, "critical")["text"] == (
        f"12 cycle(s) ended, none evidence; the newest: {AT.format(12)} was cut at 5,000 keys (maxKeys)"
    )
    capped = series([point(12, cancelled=1)]) | {"cycles": readiness.COUNTED + 1}  # counted up to 100
    many = readiness.evaluate(
        PAIR, POLICY, capped, {"signoffs": [], "cycles": [], "soaks": []}, seen(), now=T0
    )
    assert gate(many, "critical")["text"].startswith("over 100 cycle(s) ended, none evidence; the newest: ")
    assert gate(judged([errored(4)]), "critical")["text"] == (
        f"1 cycle(s) ended, none evidence; the newest: {AT.format(4)} could not be measured (KeyError)"
    )


# -- settings (§7) ----------------------------------------------------------------------------------------


def recorded(item: dict[str, Any]) -> dict[str, Any]:
    """The pair as the worker's ``readiness.changed`` row records it (§8)."""
    evidence = item["evidence"] and {name: item["evidence"][name] for name in ("run_id", "cycle")}
    statuses = {found["gate"]: found["status"] for found in item["gates"] if found["required"]}
    return {
        "to": item["state"], "regressed": item["regressed"], "gates": statuses, "evidence": evidence,
        "candidate": item["candidate"],
    }  # fmt: skip


def named(
    before: dict[str, Any], after: dict[str, Any], *, version: bool = False, config: bool = False
) -> Any:
    return readiness.causes(recorded(before), after, version=version, config=config)


def test_a_row_s_causes_are_what_differs_for_its_pair() -> None:
    """§8: a row names why from the pair as last recorded and as it is now, whatever else happened in the
    installation: the cycle that moved a parity gate, the fact a governance or evidence gate reads, the
    licence, the settings just recorded, time."""
    points = clean(3)
    was = judged(points)
    assert was["state"] == "signoff_ready"
    # A new evidence cycle, failing: the cycle's, though the candidate (and what is said of it) moved too.
    fell = judged([*points, point(4, different=1_204)])
    assert fell["state"] == "not_ready" and named(was, fell) == ["cycle"]
    assert named(was, fell, version=True) == ["cycle", "version"]
    # A newer point that is no evidence fails for certain: the evidence cycle is the same, a cycle moved it.
    partial = judged([*points, point(4, different=1_204, cancelled=3)])
    assert partial["evidence"]["cycle"] == 3 and named(was, partial) == ["cycle"]
    # The same evidence cycle, a week on: time, whichever row made the evaluation due.
    stale = judged(points, now=T0 + timedelta(days=8))
    assert said(stale)["fresh"][0] == "not_met" and named(was, stale) == ["time"]
    assert named(stale, judged([*points, point(200)])) == ["cycle"]  # a new cycle is fresh again
    # Governance and evidence gates name the fact they read.
    unscored = judged(points, cycles=[cycle(points[-1], adjudication=None)])
    assert named(unscored, was) == ["adjudication"]
    unapproved = judged(points, cycles=[cycle(points[-1], cited=None)])
    assert named(unapproved, was) == ["signoff"]
    signed = judged(points, signoffs=[signoff(points[-1])])
    packed = judged(points, signoffs=[signoff(points[-1])], cycles=[cycle(points[-1], packs=[pack()])])
    assert named(was, signed) == ["signoff"] and named(signed, packed) == ["pack"]
    assert named(packed, was) == ["signoff", "pack"]  # the sign-off revoked, its pack with it
    sampled = [point(n, **SAMPLED) for n in range(1, 4)]
    unread, read = judged(sampled), judged(sampled, ledger=[])
    assert said(unread)["sample_ledger"][0] == "unknown" and named(unread, read) == ["ledger"]
    labelled = policy(gates={"accuracy": {"min_decided": 10}})
    few = [point(n, different=20) for n in range(1, 4)]
    unlabelled = judged(few, policy=labelled)
    assert said(unlabelled)["accuracy"][0] == "not_met"
    assert named(judged(few), unlabelled) == ["accuracy"]  # the gate is required now
    assert named(judged(few), unlabelled, config=True) == ["accuracy", "config"]
    # The licence: sign-off and pack between unavailable and not met, for the same candidate run. A run
    # started outside SSO gives way to one started under it: the cycle's, and the sign-off's.
    lapsed = judged(points, installation=seen(allows=ALLOWS | {"signoff": False, "packs": False}))
    assert lapsed["state"] == "parity_ready" and named(lapsed, was) == named(was, lapsed) == ["licence"]
    # A met gate is a stored fact's: the sign-off went (and the pack that could have followed it lapsed).
    assert named(signed, lapsed) == ["signoff", "licence"]
    outside = cycle(points[-1], started="tokens", cited=None)
    newer = point(4, run=OTHER)
    moved = judged([*points, newer], cycles=[cycle(newer)])
    assert named(judged(points, cycles=[outside]), moved) == ["cycle", "signoff"]
    # Settings just recorded: a parity gate on the same evidence cycle is theirs alone.
    stricter = judged(points, policy=policy(gates={"convergence": {"cycles": 5}}))
    assert stricter["state"] == "not_ready" and named(was, stricter, config=True) == ["config"]
    assert named(was, fell, config=True) == ["cycle", "config"]  # with a new evidence cycle, both
    listed = policy(programmes=[{"name": "q4", "pairs": [{"spec": PAIR, "closed": "2026-10-01"}]}])
    closed = judged(points, policy=listed, now=T0 + timedelta(days=30))  # judged as of its day
    assert (closed["open"], named(was, closed, config=True)) == (False, ["config", "closed"])
    # Regressed alone is a sign-off's doing; a row that names nothing else is time's.
    assert readiness.causes(recorded(was) | {"regressed": True}, was, version=False, config=False) == [
        "signoff"
    ]
    assert readiness.causes(recorded(was) | {"to": "unknown"}, was, version=False, config=False) == ["time"]


def test_policy_sha_deviations() -> None:
    """A pair's gates are the build's, then the installation's, its programme's and its own; each
    non-default value is a deviation; the hash names the settings as judged."""
    assert POLICY.gates == readiness.Gates("standard", 3, "7d", False, None)
    assert (POLICY.programme, POLICY.entity, POLICY.closed, POLICY.deviations) == (None, None, None, ())
    block = {
        "gates": {"convergence": {"cycles": 1}, "fresh": {"max_age": "14d"}},
        "programmes": [
            {
                "name": "cutover-q4",
                "gates": {"convergence": {"cycles": 5}, "graph": {"required": True}},
                "pairs": [
                    {"spec": PAIR, "entity": "customer"},
                    {"spec": "customer-links", "entity": "customer",
                     "gates": {"tier": "high", "fresh": {"max_age": "1d"}, "graph": {"required": False},
                               "accuracy": {"min_decided": 100, "max_b_wrong": 2}}},
                ],
            }
        ],
    }  # fmt: skip
    loaded = TypeAdapter(ReadinessSettings).validate_python(block)
    profile, links, other = (readiness.policy(loaded, name) for name in (PAIR, "customer-links", "payments"))
    assert profile.gates == readiness.Gates("standard", 5, "14d", True, None)
    assert links.gates == readiness.Gates("high", 5, "1d", False, (100, 2))
    assert other.gates == readiness.Gates("standard", 1, "14d", False, None)
    assert (profile.programme, profile.entity, other.programme, other.entity) == (
        "cutover-q4",
        "customer",
        None,
        None,
    )
    assert profile.deviations == (
        "convergence 5 cycles (default 3)",
        "fresh 14d (default 7d; relaxed)",
        "graph required (default not required)",
    )
    assert links.deviations == (
        "tier high (default standard)",
        "convergence 5 cycles (default 3)",
        "fresh 1d (default 7d)",
        "accuracy 100 decided, 2 where side b was wrong (default not required)",
    )
    assert other.deviations == ("convergence 1 cycle (default 3; relaxed)", "fresh 14d (default 7d; relaxed)")
    # One hash for the installation's settings as judged: absent and {} judge alike.
    empty = TypeAdapter(ReadinessSettings).validate_python({})
    assert readiness.config_sha256(None) == readiness.config_sha256(empty) == POLICY.sha256
    assert profile.sha256 == links.sha256 == other.sha256 == readiness.config_sha256(loaded) != POLICY.sha256
    spelled = TypeAdapter(ReadinessSettings).validate_python(
        {"gates": {"tier": "standard", "convergence": {"cycles": 3}}}
    )
    assert readiness.config_sha256(spelled) == POLICY.sha256  # the defaults, written out
    assert len(POLICY.sha256) == 64 and readiness.effective(None) == {
        "gates": {"tier": "standard", "cycles": 3, "max_age": "7d", "graph": False, "accuracy": None},
        "programmes": [],
    }
    assert readiness.effective(loaded)["programmes"][0]["pairs"][1] == {
        "spec": "customer-links", "entity": "customer", "closed": None,
        "gates": {"tier": "high", "cycles": 5, "max_age": "1d", "graph": False, "accuracy": (100, 2)},
    }  # fmt: skip
    relaxed = judged(clean(1), policy=other)
    assert relaxed["state"] == "signoff_ready" and relaxed["deviations"] == list(other.deviations)


# -- the trend (§6) ---------------------------------------------------------------------------------------


def test_the_trend_marks_skipped_points_and_version_boundaries() -> None:
    changes = {"new": 2, "persistent": 9, "fixed": 3, "other": 1}
    points = [
        point(1, changes=None),
        point(2, cancelled=3, compared=999_985),
        point(3, changes=changes, indeterminate=7, absent_both=2, suppressed=4),
        point(30, run=OTHER, **NEWER),  # the next day
        point(31, run=OTHER, **NEWER, different=5_000),
        errored(32, run=OTHER),
    ]
    rows = readiness.trend(points, "standard")
    assert [(row["cycle"], row["skipped"], row["boundary"]) for row in rows] == [
        (1, False, False), (2, True, False), (3, False, False), (30, False, True), (31, False, False),
        (32, True, True),
    ]  # fmt: skip
    assert rows[2] == {
        "run_id": RUN, "cycle": 3, "ended_at": points[2]["ended_at"], "version": "1.4.0", "spec_id": 12,
        "basis": "census", "complete": True, "evidence": True, "total": 1_000_000, "compared": 1_000_000,
        "different": 12, "indeterminate": 7, "absent_both": 2, "api_uncomparable": 0, "suppressed": 4,
        "unresolved": 5, "unexplained_critical": 0, "new": 2, "persistent": 9, "fixed": 3, "other": 1,
        "sample_status": None, "graph_rows": None, "fails": [], "skipped": False, "boundary": False,
    }  # fmt: skip
    assert (rows[4]["fails"], rows[5]["fails"], rows[5]["basis"]) == (["targets"], ["unknown"], None)
    # By day or week: each bucket's last evidence point, never an average.
    assert [row["cycle"] for row in readiness.trend(points, "standard", "day")] == [3, 31]
    assert [row["cycle"] for row in readiness.trend(points, "standard", "week")] == [31]
    spark = readiness.spark(points[:5], "standard")
    assert [(s["cycle"], s["value"], s["allowed"], s["suppressed"], s["failed"]) for s in spark][-3:] == [
        (3, 17, 999, 4, []),  # N6: the suppressed count beside the discrepant one
        (30, 12, 1_000, 0, []),
        (31, 5_000, 1_000, 0, ["targets"]),
    ]
    # A sampled cycle's trend row holds its sample's own counts beside its status: no rate, no bound.
    sampled = readiness.trend([point(7, **SAMPLED, method="bottom_k", suppressed=2)], "standard")[0]
    assert (sampled["sample_status"], sampled["basis"], sampled["total"], sampled["suppressed"]) == (
        "passed", "sample", 6_022, 2,
    )  # fmt: skip
    assert not any("rate" in name or "ub95" in name or name == "bound" for name in sampled)
    marked = readiness.spark([point(7, **SAMPLED, method="bottom_k", suppressed=2)], "standard")[0]
    assert (marked["sampled"], marked["value"], marked["allowed"], marked["suppressed"]) == (
        True, None, None, None,
    )  # fmt: skip


RATES = {"mismatch_rate": "0.000012", "coverage": "1", "by_outcome": {"MATCH": 999_988, "MISMATCH": 12}}


def stored(row: dict[str, Any], *, counters: dict[str, Any] = RATES, groups: int = 0) -> dict[str, Any]:
    """A point as ``repo.brief_points`` reads it: with its cycle's stored counters and how many root-cause
    groups it holds."""
    return row | {"counters": counters, "purging_at": None, "root_causes": groups}


def test_brief_marks() -> None:
    """A brief's points are the trend's, each with what its stored counters say and its marks: ``version``
    at another spec, ``keys`` at another key source or another size of a list given at creation or read
    from a key table. A growing scan and a sample after a sample take none; a cycle without counters, an
    error record and measures of another format take none, give none and hold no figure."""
    given_ = {"keys_source": "explicit", "problem": LISTED, "total": 5, "compared": 5, "different": 0}
    tabled = NEWER | given_ | {"keys_source": "table", "total": 8, "compared": 8}  # a key table's keys
    drawn = SAMPLED | {"method": "bottom_k"}
    points = [
        stored(point(1)),
        stored(point(2, total=1_000_200, compared=1_000_200)),  # a growing scan
        stored(point(3, own=False, **given_ | dict.fromkeys(readiness.COUNTS)), counters={}, groups=4),
        stored(errored(4), groups=4),  # ids only, whatever its tombstone keeps
        stored(point(5)),  # against cycle 2: the two before it give nothing
        stored(point(6, **given_)),
        stored(point(7, **given_)),  # a list of the same size: no digest of its keys is stored
        stored(point(8, **given_ | {"total": 7, "compared": 7})),
        stored(point(9, **drawn)),
        stored(point(10, **drawn | {"draw_id": "D-" + "0" * 25 + "9"})),  # each sample draws its own keys
        stored(point(11, **NEWER)),
        stored(point(12, **NEWER) | {"readiness": point(12, **NEWER)["readiness"] | {"format": 2}}, groups=4),
        stored(point(13, **NEWER), counters=RATES | {"mismatch_rate": "unavailable"}),
        stored(point(14, **tabled)),
        stored(point(15, **tabled)),  # read again, the same size
        stored(point(16, **tabled | {"total": 3, "compared": 3})),  # read again: other rows, no new version
    ]
    rows = readiness.marked(points[::-1], "standard")  # newest first, as the store gives them
    assert [(row["cycle"], row["marks"]) for row in rows] == [
        (1, []), (2, []), (3, []), (4, []), (5, []), (6, ["keys"]), (7, []), (8, ["keys"]), (9, ["keys"]),
        (10, []), (11, ["version", "keys"]), (12, []), (13, []), (14, ["keys"]), (15, []), (16, ["keys"]),
    ]  # fmt: skip
    assert [row["boundary"] for row in rows] == [row["marks"][:1] == ["version"] for row in rows]
    by = {row["cycle"]: row for row in rows}
    assert by[1] == readiness.trend(points, "standard")[0] | RATES | {
        "cancelled": 0, "outstanding": 0, "root_causes": 0, "marks": [],
    }  # fmt: skip
    figures = ("mismatch_rate", "coverage", "by_outcome", "cancelled", "outstanding", "root_causes")
    assert [by[3][name] for name in figures] == [None, None, None, None, None, 4]  # counters of {}
    for unread in (4, 12):  # a failed measure, and measures this build does not read
        assert [by[unread][name] for name in (*figures, "total", "basis")] == [None] * 8
    assert (by[4]["spec_id"], by[12]["spec_id"], by[12]["version"]) == (12, 15, "1.5.0")
    # A sampled cycle has no rate: its counts are its sample's own. A rate the store could not give is none.
    assert [by[9][name] for name in figures[:3]] == [None, None, RATES["by_outcome"]]
    assert (by[13]["mismatch_rate"], by[13]["coverage"]) == (None, "1")


def test_brief_ratio_that_rounded_its_count_away() -> None:
    """The counters round a ratio to six places. A brief's point holds it as stored; where it reads 0 of
    keys that differ, or 1 of keys not all compared, M9 and the figures say ``under 0.000001`` and
    ``over 0.999999``, never a plain 0 or 1 beside the counts."""
    groups = [
        {"n": n, "outcome": outcome, "bug": False, "confirmation": "rechecked", "first_outcome": outcome}
        | {"inconclusive_reason": reason, "drift_reason": None, "truncated": False}
        for n, outcome, reason in (
            (2_999_999, "MATCH", None), (1, "MISMATCH", None), (1, "INCONCLUSIVE", "rate_limited"),
        )
    ]  # fmt: skip
    counted = counters.count_groups(groups, live=True, recheck_outcomes=[Outcome.MISMATCH])
    counts = {name: counted[name] for name in ("total", "compared", "different", "indeterminate")}
    assert (counts, counted["mismatch_rate"], counted["coverage"]) == (
        {"total": 3_000_001, "compared": 3_000_000, "different": 1, "indeterminate": 1}, "0", "1",
    )  # fmt: skip
    row = stored(point(1, **counts), counters=counted)
    (found,) = readiness.marked([row], "standard")
    assert (found["mismatch_rate"], found["coverage"]) == ("0", "1")  # as stored
    assert readiness.reported(found, row, None, None)["notes"] == [
        "Mismatch rate under 0.000001: 1 different of 3,000,000 compared. Known differences and noise-only"
        " keys count as matching; a known difference under a bug rule counts as different."
    ]
    assert readiness.ratio("1", 3_000_000, 3_000_001) == "over 0.999999"
    assert readiness.ratio("1", 2_999_999, 3_000_000) == "over 0.999999"  # a rate: one key matches
    assert readiness.ratio("0", 1, 3_000_000) == "under 0.000001"  # a coverage: one key compared
    told = [("0", 0, 40), ("1", 40, 40), ("0.075", 3, 40), ("0.000001", 3, 3_000_000), (None, 6, 6)]
    assert [readiness.ratio(*stored_) for stored_ in told] == ["0", "1", "0.075", "0.000001", None]
    assert readiness.ratio("0", None, None) == "0" and readiness.ratio("1", None, 5) == "1"  # counts not held


# -- a failing cycle never raises a state ------------------------------------------------------------------

KINDS = {
    "clean": {},
    "partial": {"cancelled": 3},
    "over": {"different": 5_000},
    "critical": {"unexplained_critical": 1},
    "listed": {"keys_source": "explicit", "problem": LISTED},
    "sampled": SAMPLED | {"method": "bottom_k"},
    "unjudged": SAMPLED | {"method": "bottom_k", "sample_status": "unjudged"},  # an unknown point
    "late": {"late": True, "whole": None, "unexplained_critical": None, "results": None},  # and another
    "newer": NEWER,
}
FAILING = [{"different": 5_000}, {"unexplained_critical": 2}, {"keys_source": "list", "problem": "x"}]


@settings(max_examples=200, deadline=None)
@given(
    history=st.lists(st.sampled_from(sorted(KINDS)), min_size=1, max_size=12),
    signed=st.integers(min_value=0, max_value=11),
    failing=st.sampled_from(FAILING),
    mode=st.sampled_from(["oidc", "tokens"]),
    tier=st.sampled_from(["standard", "high"]),
)
def test_failing_cycle_never_raises_state(
    history: list[str], signed: int, failing: dict[str, Any], mode: str, tier: str
) -> None:
    """From ``not_ready`` up, adding an evidence point of the current version that fails a gate other than
    ``unknown`` never raises a state (an unknown point may: ``unknown`` is the rung above ``not_ready``)."""
    judging = policy(gates={"tier": tier, "convergence": {"cycles": 2}})
    points = [point(n, **KINDS[kind]) for n, kind in enumerate(history, start=1)]

    def state(rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        found = series(rows, judging)
        cycles = [cycle(row, worked=True, adjudication=passed(), packs=[pack()]) for row in rows]
        signoffs = [signoff(rows[signed])] if signed < len(history) else []
        governance = {"signoffs": signoffs, "cycles": cycles, "soaks": []}
        now = rows[-1]["ended_at"] + timedelta(hours=1)
        item = readiness.evaluate(PAIR, judging, found, governance, seen(mode), now=now, ledger=[])
        return item["state"], item

    before, _ = state(points)
    if before == "no_evidence":
        return
    current = series(points, judging)["versions"][0]["readiness"]
    added = point(len(points) + 1, spec_id=current["spec_id"], version=current["version"], **failing)
    after, item = state([*points, added])
    assert STATES.index(after) <= STATES.index(before)
    assert after == "not_ready" and item["convergence"]["streak"] == 0 and item["blocking"]


# -- every text (§9) --------------------------------------------------------------------------------------


def test_messages_golden() -> None:
    """Every gate has a text for each status; the pair below is at parity ready, then at sign-off ready."""
    points = clean(14, different=1_204, absent_both=1_000, indeterminate=1_000)  # the contract's 999 allowed
    assert said(judged(points))["targets"] == (
        "not_met",
        f"{AT.format(14)} has 1,204 discrepant or unresolved key(s) of 1,000,000 observed whole; its pooled"
        f" 0.1% target allows 999: 205 over{SUPPRESSED.format(0)}",
    )
    points = clean(14, absent_both=1_000, indeterminate=1_000)
    unscored = [cycle(points[-1], adjudication=None)]
    item = judged(points, cycles=unscored, now=T0 + timedelta(days=2, hours=15))
    assert said(item) == {
        "population": ("met", f"{AT.format(14)} observed side a whole ({READ})"),
        "critical": ("met", f"{AT.format(14)} has no key with unexplained CRITICAL findings"),
        "targets": (
            "met",
            f"{AT.format(14)} has 12 discrepant or unresolved key(s) of 1,000,000 observed whole, within the"
            " 999 its pooled 0.1% target allows (per-stratum targets are the approver's)"
            f"{SUPPRESSED.format(0)}",
        ),
        "convergence": (
            "met",
            f"the newest 14 evidence cycles of {V14} meet every per-cycle gate: at least the 3 the gate"
            " needs",
        ),
        "fresh": ("met", f"the evidence cycle (14 of run {RUN}) ended 2 days ago, within the gate's 7 days"),
        "graph": ("not_applicable", f"{V14} declares no graph"),
        "spec_approval": ("met", f"{AT.format(14)} ran under spec approval {APPROVAL}, approved"),
        "adjudication": (
            "not_met",
            f"no adjudication of {AT.format(14)} is scored: draw and score one before requesting sign-off,"
            " which freezes the cycle's adjudications (docs/mvp/PACK.md §5)",
        ),
        "sample_ledger": ("not_applicable", f"{AT.format(14)} is not sampled"),
        "accuracy": ("not_applicable", "no accuracy gate is configured"),
        "signoff": ("not_met", f"{AT.format(14)} is not signed off: the gates before it are not all met"),
        "pack": ("not_met", "no sign-off to pack yet"),
    }
    assert (item["state"], item["score"], item["blocking"]) == (
        "parity_ready",
        {"passed": 6, "required": 9},
        ["adjudication", "signoff", "pack"],
    )
    ready = judged(points)
    assert ready["state"] == "signoff_ready" and said(ready)["signoff"] == (
        "not_met",
        f"request sign-off of {AT.format(14)} on the Run screen, the newest cycle in the streak whose"
        " adjudication passed (or the newest evidence cycle): every other gate is met",
    )
    for found in item["gates"]:  # N8, N9: observed, never certified; a text names no key and no value
        assert "certif" not in found["text"] and "%" not in found["text"].replace("0.1% target", "")
    ended_at = points[-1]["ended_at"]
    assert item["evidence"] == {
        "run_id": RUN, "cycle": 14, "ended_at": ended_at, "basis": "census", "claim": "observed",
    }  # fmt: skip
