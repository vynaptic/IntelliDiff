"""Labelled accuracy (docs/mvp/ACCURACY.md): §6.3's worked example and §7's texts, verbatim; what is
never stated; the populations and the draw (uniform, each cause's smallest digests, a census, ``*``); the
truth source's circularity and date; every §11 message; an upload that is refused changes nothing, and one
that is stored keeps no note."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from decimal import Context as Rounding
from typing import Any

import pytest
import yaml
from scipy.stats import beta, chi2

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.core import jsonio, sampling
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.sampling import percent
from deltasleuth.core.spec import compile_spec
from deltasleuth.reports import accuracy
from deltasleuth.reports.accuracy import LABELS, lines, share, store_draw, store_labels, summary, tally
from deltasleuth.reports.adjudicate import AdjudicationConflict, AdjudicationError, UploadTooLarge
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.runner import purge
from deltasleuth.runner.purge import CyclePurged
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import approved, drawn_labelling, person, request_signoff

from .conftest import GRAPH_SPEC, TARGETS, doc, seed_run, unit_spec

RUN, LABELLING, SIGNOFF = "R-" + "0" * 25 + "1", "V-" + "0" * 25 + "2", "A-" + "0" * 25 + "3"
CAUSE = "RC-3f9a0c1d2e4b"
TRUTH = {"source": "CBS-PROD.account_master", "as_of": "2026-10-01", "names_side": None}
OBSERVED = TRUTH | {"as_of": "2026-09-24"}  # the day the seeded keys were observed
TODAY = f"{datetime.now(UTC):%Y-%m-%d}"
RAW_KEY, RAW_VALUE = "0712345678", "Alice Secretname"  # typed into notes: never stored
RULES = [
    {"id": "KD-UNIT-1", "when": "path == '$.status' && a == 'OLD' && b == 'NEW'",
     "classification": "accepted", "reason": "status vocabulary renamed", "ticket": "UNIT-1"},
    {"id": "KD-BUG-2", "when": "path == '$.status' && a == 'X' && b == 'Y'", "classification": "bug",
     "reason": "a known bug of the new side", "ticket": "UNIT-2"},
]  # fmt: skip
GOLDEN = """\
Labelling {V} drawn 2026-10-02 by amy, labelled 2026-10-04 by bob against CBS-PROD.account_master as of 2026-10-01, attested by sign-off {A}.
Discrepancies: 120 of the 1,204 that cycle 2 of run {R} observed (one key's difference in one root cause; 1,100 keys, 23 root causes), drawn uniformly; side a is legacy, side b is new; each label is the truth when its item was observed.
Undecidable: 3 of 120 (2 NOT_LOCATABLE, 1 NOT_REVIEWED); the ranges below count them either way.
Truth favoured b (new) alone: 84 of 120 (70.00%, 95% interval 60.96% to 78.03%; 60.96% to 80.26% counting the undecidable either way: 733 to 967 of the 1,204).
Truth favoured a (legacy) alone: 12 of 120 (10.00%, 95% interval 5.274% to 16.82%; 5.274% to 19.78% counting the undecidable either way: 63 to 239 of the 1,204).
Truth held neither side's values: 3 of 120 (2.500%, 95% interval 0.5185% to 7.133%; 0.5185% to 10.57% counting the undecidable either way: 6 to 128 of the 1,204).
The difference did not matter: 18 of 120 (15.00%, 95% interval 9.138% to 22.67%; 9.138% to 25.50% counting the undecidable either way: 110 to 308 of the 1,204).
b (new) wrong (truth favoured a, or neither): 15 of 117 decided (12.82%, 95% interval 7.356% to 20.27%; 7.167% to 22.67% counting the undecidable either way).
a (legacy) wrong (truth favoured b, or neither): 87 of 117 decided (74.36%, 95% interval 65.46% to 81.99%; 63.60% to 82.46% counting the undecidable either way).
Where one side alone was right, it was b (new) on 84 of 96 (87.50%, 95% interval 79.18% to 93.38%; 76.24% to 93.58% counting the undecidable either way).
Basis of the 117 decided: 110 SYSTEM_OF_RECORD, 7 BUSINESS_RULE.
Root cause RC-3f9a0c1d2e4b (rank 1 of 23, 212 items): of 22 labelled, b (new) alone 17, a (legacy) alone 2, neither 1, did not matter 2; b (new) wrong on 3 of 22 decided (13.64%, 95% interval 2.905% to 34.92%).
Agreed keys (outcome MATCH): 100 of 4,210 drawn; did not matter 97, b (new) alone 1, neither 1, undecidable 1; an agreement hid an error on 2 of 99 decided (2.020%, 95% interval 0.2455% to 7.108%; 0.2431% to 8.518% counting the undecidable either way).
Accepted keys (a rule or mask hid the difference): 50 of 180 drawn; did not matter 45, a (legacy) alone 3, b (new) alone 1, undecidable 1; a rule or mask hid an error on 4 of 49 decided (8.163%, 95% interval 2.269% to 19.61%; 2.222% to 21.82% counting the undecidable either way).
Rule fx_rounding (110 accepted keys): hid an error on 3 of 31 decided (9.677%, 95% interval 2.041% to 25.76%).
"""  # noqa: E501 - docs/mvp/ACCURACY.md §7, verbatim
SUMMARY = (
    "{V} run {R} cycle 2: 120 of 1,204 discrepancies labelled 2026-10-04 by bob against"
    " CBS-PROD.account_master as of 2026-10-01 (attested by {A}): b (new) alone right 84, a (legacy) alone"
    " right 12, neither 3, did not matter 18, undecidable 3; b (new) wrong on 15 of 117 decided (7.356% to"
    " 20.27%; 7.167% to 22.67% counting the undecidable either way)"
)
IDS = {"V": LABELLING, "R": RUN, "A": SIGNOFF}


# -- §6.3's worked example, without a store ---------------------------------------------------------------


def _rows(n: int, **cells: str) -> list[dict[str, str]]:
    blank = dict.fromkeys(accuracy.KEYLESS_COLUMNS, "")
    return [blank | {"root_cause_id": "*", "selected_for": "uniform"} | cells for _ in range(n)]


def _decided(population: str, counts: dict[str, int], **cells: str) -> list[dict[str, str]]:
    return [
        row
        for label, n in counts.items()
        for row in _rows(n, population=population, label=label, basis="SYSTEM_OF_RECORD", **cells)
    ]


def worked() -> tuple[list[dict[str, str]], dict[str, Any]]:
    """§6.3's labelled sheet without its keys, and its selection: 120 uniform discrepant items (22 of root
    cause RC-3f9a0c1d2e4b), 100 agreed keys and 50 accepted ones (31 under rule fx_rounding)."""
    mismatch = {"engine_outcome": "MISMATCH"}
    rows = _decided("discrepant", {"B_RIGHT": 17, "A_RIGHT": 2, "BOTH_WRONG": 1, "BOTH_RIGHT": 2},
                    root_cause_id=CAUSE, **mismatch)  # fmt: skip
    rows += _decided(
        "discrepant", {"B_RIGHT": 67, "A_RIGHT": 10, "BOTH_WRONG": 2, "BOTH_RIGHT": 16}, **mismatch
    )
    for row in rows[-7:]:
        row["basis"] = "BUSINESS_RULE"
    for reason in ("NOT_LOCATABLE", "NOT_LOCATABLE", "NOT_REVIEWED"):
        rows += _rows(1, population="discrepant", label="UNDECIDABLE", reason=reason, **mismatch)
    rows += _decided("accepted", {"A_RIGHT": 2, "B_RIGHT": 1, "BOTH_RIGHT": 28}, rules="fx_rounding")
    rows += _decided("accepted", {"A_RIGHT": 1, "BOTH_RIGHT": 17}, rules="mask")
    rows += _rows(1, population="accepted", label="UNDECIDABLE", reason="NO_TRUTH", rules="mask")
    rows += _decided("agreed", {"BOTH_RIGHT": 97, "BOTH_WRONG": 1, "B_RIGHT": 1}, engine_outcome="MATCH")
    rows += _rows(1, population="agreed", label="UNDECIDABLE", reason="NOT_LOCATABLE", engine_outcome="MATCH")
    selection = {
        "items": None,
        "counts": {
            "discrepant": {"items": 1204, "keys": 1100, "causes": 23, "drawn": 120},
            "accepted": {"keys": 180, "by_rule": {"fx_rounding": 110, "mask": 70}, "drawn": 50},
            "agreed": {"keys": 4210, "drawn": 100},
            "indeterminate": 7, "not_compared": 0, "matched_by_row_hash": 0,
        },
        "causes": [{"root_cause_id": CAUSE, "rank": 1, "items": 212}],
        "uncovered": 22, "names_side": None, "unlocatable": False, "warnings": [],
    }  # fmt: skip
    return rows, selection


def labelling(score: dict[str, Any] | None, **changes: Any) -> dict[str, Any]:
    """A labelling as ``repo.get_labelling`` gives it: amy drew it, bob labelled it."""
    return {
        "labelling_id": LABELLING, "run_id": RUN, "cycle": 2, "accuracy": score,
        "drawn_at": datetime(2026, 10, 2, 10, 2, 11, tzinfo=UTC), "drawn_by_name": "amy",
        "labelled_at": datetime(2026, 10, 4, 9, 0, tzinfo=UTC), "labelled_by": "user:U-0123456789abcdef",
        "labelled_by_name": "bob", "truth_source": TRUTH["source"], "truth_as_of": date(2026, 10, 1),
        "role_a": "legacy", "role_b": "new", "started_run": False, "spec_author": None,
        "cycle_drawn": 1, "cycle_labelled": 1,
    } | changes  # fmt: skip


def shown(found: dict[str, Any]) -> str:
    """A share as §6.3's table shows it."""
    low, high = percent(found["low"]), percent(found["high"])
    text = f"{found['x']}/{found['n']} {percent(found['share'])} {low} to {high}"
    if found["items_low"] is not None:
        text += f" ({found['items_low']} to {found['items_high']})"
    if found.get("worst_case"):
        text += f"; worst {percent(found['worst_case']['low'])} to {percent(found['worst_case']['high'])}"
    return text


def test_worked_example() -> None:
    rows, selection = worked()
    score = tally(rows, selection, TRUTH)
    d, sides = score["discrepant"], score["discrepant"]["sides"]
    assert {name: d[name] for name in ("items", "keys", "causes", "uniform", "census")} == {
        "items": 1204, "keys": 1100, "causes": 23, "uniform": 120, "census": False,
    }  # fmt: skip
    # A label's range counts the 3 undecidable items either way, and its item counts come from that range.
    assert [shown(d["labels"][label]) for label in LABELS[:4]] == [
        "12/120 10.00% 5.274% to 16.82% (63 to 239); worst 5.274% to 19.78%",
        "84/120 70.00% 60.96% to 78.03% (733 to 967); worst 60.96% to 80.26%",
        "3/120 2.500% 0.5185% to 7.133% (6 to 128); worst 0.5185% to 10.57%",
        "18/120 15.00% 9.138% to 22.67% (110 to 308); worst 9.138% to 25.50%",
    ]
    assert shown(d["labels"]["UNDECIDABLE"]) == "3/120 2.500% 0.5185% to 7.133% (6 to 86)"  # its own count
    for label, x in (("A_RIGHT", 12), ("B_RIGHT", 84), ("BOTH_WRONG", 3), ("BOTH_RIGHT", 18)):
        low, high = beta.ppf(0.025, x, 120 - x + 1), beta.ppf(0.975, x + 3 + 1, 120 - x - 3)  # the oracle
        worst = d["labels"][label]["worst_case"]
        assert Decimal(worst["low"]) == Rounding(prec=4, rounding=ROUND_FLOOR).create_decimal(
            repr(float(low))
        )
        assert Decimal(worst["high"]) == Rounding(prec=4, rounding=ROUND_CEILING).create_decimal(
            repr(float(high))
        )
        assert d["labels"][label]["items_low"] == math.floor(1204 * Decimal(worst["low"]))
        assert d["labels"][label]["items_high"] == math.ceil(1204 * Decimal(worst["high"]))
    assert [shown(sides[side][name]) for side in "ba" for name in ("wrong", "right_or_immaterial")] == [
        "15/117 12.82% 7.356% to 20.27%; worst 7.167% to 22.67%",
        "102/117 87.18% 79.73% to 92.65%; worst 77.33% to 92.84%",
        "87/117 74.36% 65.46% to 81.99%; worst 63.60% to 82.46%",
        "30/117 25.64% 18.01% to 34.54%; worst 17.54% to 36.40%",
    ]
    assert shown(d["b_share_of_single_side_right"]) == "84/96 87.50% 79.18% to 93.38%; worst 76.24% to 93.58%"
    assert (d["reasons"], d["basis_codes"]) == (
        {"NOT_LOCATABLE": 2, "NOT_REVIEWED": 1},
        {"SYSTEM_OF_RECORD": 110, "BUSINESS_RULE": 7},
    )
    (cause,) = d["by_cause"]
    assert shown(cause["sides"]["b"]["wrong"]) == "3/22 13.64% 2.905% to 34.92%"
    assert {name: cause[name] for name in ("root_cause_id", "rank", "items", "labelled", "census")} == {
        "root_cause_id": CAUSE, "rank": 1, "items": 212, "labelled": 22, "census": False,
    }  # fmt: skip
    assert cause["labels"] == {"A_RIGHT": 2, "B_RIGHT": 17, "BOTH_WRONG": 1, "BOTH_RIGHT": 2}
    assert d["by_outcome"] == {"MISMATCH": {label: d["labels"][label]["x"] for label in LABELS}}
    assert d["uncovered"] == 22
    agreed, accepted = score["agreed"], score["accepted"]
    assert shown(agreed["labels"]["BOTH_WRONG"]) == (
        "1/100 1.000% 0.02531% to 5.446% (1 to 297); worst 0.02531% to 7.039%"
    )
    assert shown(agreed["hidden_error"]) == "2/99 2.020% 0.2455% to 7.108%; worst 0.2431% to 8.518%"
    assert shown(accepted["hidden_error"]) == "4/49 8.163% 2.269% to 19.61%; worst 2.222% to 21.82%"
    fx, mask = accepted["by_rule"]
    assert (fx["rule_id"], fx["keys"], fx["labelled"], shown(fx["hidden_error"])) == (
        "fx_rounding", 110, 31, "3/31 9.677% 2.041% to 25.76%",
    )  # fmt: skip
    assert (mask["rule_id"], mask["keys"], mask["labelled"], mask["hidden_error"]["x"]) == ("mask", 70, 19, 1)
    top = {name: value for name, value in score.items() if name not in accuracy.POPULATIONS}
    assert top == {
        "method": "deltasleuth-labelled/1", "confidence": "0.95", "sided": "two-sided", "truth": TRUTH,
        "unlocatable": False, "indeterminate": 7, "not_compared": 0, "matched_by_row_hash": 0,
    }  # fmt: skip
    # Pinned: (0, 10), (5, 5) and (1, 20); n = 0 shares nothing.
    pinned = [share(0, 10), share(5, 5), share(1, 20)]
    assert [(percent(s["low"]), percent(s["high"])) for s in pinned] == [
        ("0%", "30.85%"), ("47.81%", "100%"), ("0.1265%", "24.88%"),
    ]  # fmt: skip
    assert [s["share"] for s in pinned] == ["0", "1.000", "0.05000"]
    assert share(0, 0) == dict.fromkeys(("share", "low", "high", "items_low", "items_high")) | {
        "x": 0,
        "n": 0,
    }
    # Without an undecidable item a label has no range: its item counts are its interval's.
    whole = tally([row for row in rows if row["label"] != "UNDECIDABLE"], selection, TRUTH)
    assert shown(whole["discrepant"]["labels"]["B_RIGHT"]) == "84/117 71.79% 62.72% to 79.73% (755 to 960)"
    assert whole["discrepant"]["labels"]["B_RIGHT"]["worst_case"] is None
    assert score == json.loads(jsonio.dumps(score))  # jsonb keeps it: text, counts and nulls only


def test_lines_golden() -> None:
    score = tally(*worked(), TRUTH)
    row = labelling(score)
    assert "\n".join(lines(row, signoff=SIGNOFF)) + "\n" == GOLDEN.format(**IDS)
    first = GOLDEN.format(**IDS).split("\n")[0].removesuffix(f", attested by sign-off {SIGNOFF}.")
    assert lines(row, signoff=None)[0] == f"{first}; no sign-off attests it."
    token = row | {"labelled_by": "token:T-0123456789abcdef", "labelled_by_name": "ci-bot"}
    through = first.replace("by bob", "through token ci-bot (T-0123456789abcdef)")
    assert (
        lines(token, signoff=SIGNOFF)[0] == f"{through}, not by a signed-in person: no sign-off attests it."
    )
    developer = lines(row | {"labelled_by": "dev:loopback", "labelled_by_name": "developer"}, signoff=None)
    assert developer[0] == (
        first.replace("by bob", "in developer mode (dev:loopback)")
        + ", not by a signed-in person: no sign-off attests it."
    )
    assert lines(labelling(None), signoff=None) == []  # drawn, not labelled yet
    # The warnings follow the first line.
    circular = score | {"truth": TRUTH | {"names_side": "a"}, "unlocatable": True}
    warned = lines(labelling(circular, started_run=True, spec_author=SIGNOFF), signoff=SIGNOFF)[1:5]
    assert warned == [
        "Warning: the truth source names side a (legacy): labels resting on a compared side are circular and"
        " measure nothing.",
        "Warning: the key is tokenised and the spec showed no path in clear: labellers could not find records"
        " in the truth source.",
        f"Warning: the labeller started run {RUN}: the labels are not independent of the run.",
        f"Warning: the labeller wrote spec approval {SIGNOFF}: the labels are not independent of the run.",
    ]


def test_lines_of_a_census_without_an_undecidable_item_and_of_many_causes() -> None:
    rows = _decided("discrepant", {"B_RIGHT": 3, "A_RIGHT": 1}, engine_outcome="MISMATCH")
    selection = worked()[1] | {"causes": [], "uncovered": 0}
    selection["counts"] = selection["counts"] | {
        "discrepant": {"items": 4, "keys": 4, "causes": 0, "drawn": 4}
    }
    row = labelling(tally(rows, selection, TRUTH))
    assert lines(row, signoff=None)[1:] == [
        f"Discrepancies: 4 of the 4 that cycle 2 of run {RUN} observed (one key's difference in one root"
        " cause; 4 keys, 0 root causes), drawn uniformly; side a is legacy, side b is new; each label is the"
        " truth when its item was observed.",
        "Undecidable: none.",
        "Truth favoured b (new) alone: 3 of 4 (75.00%, every item labelled: exact).",
        "Truth favoured a (legacy) alone: 1 of 4 (25.00%, every item labelled: exact).",
        "Truth held neither side's values: 0 of 4 (0%, every item labelled: exact).",
        "The difference did not matter: 0 of 4 (0%, every item labelled: exact).",
        "b (new) wrong (truth favoured a, or neither): 1 of 4 decided (25.00%, every item labelled: exact).",
        "a (legacy) wrong (truth favoured b, or neither): 3 of 4 decided (75.00%, every item labelled:"
        " exact).",
        "Where one side alone was right, it was b (new) on 3 of 4 (75.00%, every item labelled: exact).",
        "Basis of the 4 decided: 4 SYSTEM_OF_RECORD.",
    ]
    assert summary(row, signoff=None).endswith("b (new) wrong on 1 of 4 decided (every item labelled: exact)")
    # A census with an undecidable item: exact counts, and the range counting it either way, rounded outward.
    rows += _rows(
        2, population="discrepant", label="UNDECIDABLE", reason="NO_TRUTH", engine_outcome="MISMATCH"
    )
    selection["counts"]["discrepant"] = {"items": 6, "keys": 6, "causes": 0, "drawn": 6}
    exact = tally(rows, selection, TRUTH)
    wrong = exact["discrepant"]["sides"]["b"]["wrong"]
    assert (wrong["low"], wrong["high"], wrong["worst_case"]) == (
        None,
        None,
        {"low": "0.1666", "high": "0.5"},
    )
    assert lines(labelling(exact), signoff=None)[2:5] == [
        "Undecidable: 2 of 6 (2 NO_TRUTH); the ranges below count them either way.",
        "Truth favoured b (new) alone: 3 of 6 (50.00%, every item labelled: exact; 50% to 83.34% counting the"
        " undecidable either way: 3 to 5 of the 6).",
        "Truth favoured a (legacy) alone: 1 of 6 (16.67%, every item labelled: exact; 16.66% to 50% counting"
        " the undecidable either way: 1 to 3 of the 6).",
    ]
    # Cause lines run in rank order, 20 at most; the rest are counted.
    causes = [{"root_cause_id": f"RC-{n:012x}", "rank": n + 1, "items": 9} for n in range(23)]
    rows = [
        row
        for c in causes
        for row in _decided(
            "discrepant", {"B_RIGHT": 1}, root_cause_id=c["root_cause_id"], selected_for="coverage"
        )
    ]
    selection = worked()[1] | {"causes": causes}
    said = lines(labelling(tally(rows, selection, TRUTH)), signoff=None)
    assert said[1].startswith("Discrepancies: 0 of the 1,204 that cycle 2") and len(said) == 2 + 20 + 1
    assert said[2] == (
        "Root cause RC-000000000000 (rank 1 of 23, 9 items): of 1 labelled, b (new) alone 1; b (new) wrong on"
        " 0 of 1 decided (0%, 95% interval 0% to 97.50%)."
    )
    assert said[-1] == (
        f"3 more root causes: GET /v1/labellings/{LABELLING} lists them (accuracy/score.json in a pack"
        " attesting it)"
    )
    assert summary(labelling(tally(rows, selection, TRUTH)), signoff=None).endswith(
        "(no sign-off attests it): no uniform item: its root causes' figures only"
    )


def test_lines_without_a_decided_item_and_of_a_labeller_that_is_no_person() -> None:
    """§7's edge texts: no share out of nothing; a labeller that is neither a person, a token nor developer
    mode is named with its actor, and no sign-off attests its labelling."""
    undecidable = {"label": "UNDECIDABLE", "reason": "TOKENISED", "engine_outcome": "MISMATCH"}
    rows = _rows(3, population="discrepant", **undecidable)
    score = tally(rows, worked()[1] | {"causes": [], "uncovered": 0}, TRUTH)
    row = labelling(score, labelled_by="local:ann@host", labelled_by_name="ann")
    said = lines(row, signoff=SIGNOFF)
    assert said[0] == (
        f"Labelling {LABELLING} drawn 2026-10-02 by amy, labelled 2026-10-04 by ann (local:ann@host) against"
        " CBS-PROD.account_master as of 2026-10-01, not by a signed-in person: no sign-off attests it."
    )
    assert said[2] == "Undecidable: 3 of 3 (3 TOKENISED); the ranges below count them either way."
    assert said[3] == (
        "Truth favoured b (new) alone: 0 of 3 (0%, 95% interval 0% to 70.76%; 0% to 100% counting the"
        " undecidable either way: 0 to 1,204 of the 1,204)."
    )
    assert said[-3:] == [
        "b (new) wrong (truth favoured a, or neither): 0 of 0 decided (no item decided).",
        "a (legacy) wrong (truth favoured b, or neither): 0 of 0 decided (no item decided).",
        "Basis of the 0 decided: none.",
    ]
    assert summary(row, signoff=SIGNOFF).endswith(
        " (no sign-off attests it): b (new) alone right 0, a (legacy) alone right 0, neither 0, did not"
        " matter 0, undecidable 3; b (new) wrong on 0 of 0 decided (no item decided)"
    )


def test_summary_golden() -> None:
    rows, selection = worked()
    score = tally(rows, selection, TRUTH)
    row = labelling(score)
    assert summary(row, signoff=SIGNOFF) == SUMMARY.format(**IDS)
    unattested = SUMMARY.format(**IDS).replace(f"(attested by {SIGNOFF})", "(no sign-off attests it)")
    assert summary(row, signoff=None) == unattested
    marked = score | {"truth": TRUTH | {"names_side": "a"}, "unlocatable": True}
    assert summary(labelling(marked), signoff=None) == (
        unattested + "; CIRCULAR: the truth source names side a; records not locatable (tokenised key)"
    )
    assert summary(labelling(None), signoff=None) == (
        f"{LABELLING} run {RUN} cycle 2: drawn 2026-10-02 by amy against CBS-PROD.account_master as of"
        " 2026-10-01; not labelled"
    )
    # Not named (the agent's line): the kind of actor that labelled, without a name or an id.
    assert summary(row, signoff=SIGNOFF, named=False) == SUMMARY.format(**IDS).replace(
        "by bob", "by a person"
    )
    actors = {"token:T-0123456789abcdef": "through a token", "dev:loopback": "in developer mode",
              "local:ann@host": "by no signed-in person"}  # fmt: skip
    for actor, kind in actors.items():
        line = summary(row | {"labelled_by": actor, "labelled_by_name": "ann"}, signoff=None, named=False)
        assert line == unattested.replace("by bob", kind) and "ann" not in line
    # With no discrepancy drawn, what the agreed and accepted samples found replaces the counts.
    hidden = tally([r for r in rows if r["population"] != "discrepant"], selection, TRUTH)
    assert hidden["discrepant"] is None and summary(labelling(hidden), signoff=SIGNOFF) == (
        f"{LABELLING} run {RUN} cycle 2: labelled 2026-10-04 by bob against CBS-PROD.account_master as of"
        f" 2026-10-01 (attested by {SIGNOFF}): an agreement hid an error on 2 of 99 decided (0.2455% to"
        " 7.108%; 0.2431% to 8.518% counting the undecidable either way); a rule or mask hid an error on 4 of"
        " 49 decided (2.269% to 19.61%; 2.222% to 21.82% counting the undecidable either way)"
    )


GATE = """\
labelling {V} of cycle 2 does not meet the accuracy gate, which allows 0 decided items where side b was wrong and needs 50 decided (all, for a census of the discrepant items)
Labelling {V} drawn 2026-10-02, labelled 2026-10-04 by a person against CBS-PROD.account_master as of 2026-10-01, attested by sign-off {A}.
b (new) wrong (truth favoured a, or neither): 15 of 117 decided (12.82%, 95% interval 7.356% to 20.27%; 7.167% to 22.67% counting the undecidable either way).
"""  # noqa: E501 - docs/mvp/READINESS.md §9 Y18, over §6.3's worked example


def test_gate_golden() -> None:
    """Readiness's accuracy gate over a stored score (docs/mvp/READINESS.md §9 Y18): met when at most
    ``max_b_wrong`` decided uniform items found side b wrong and ``min_decided`` are decided; a census of
    the discrepant items is judged on its decided ones, however few but never none; a labelling that labels
    none is none."""
    rows, selection = worked()
    row = labelling(tally(rows, selection, TRUTH))
    at = f"cycle 2 of run {RUN}"

    def gate(found: dict[str, Any] | None = row, **limits: int) -> tuple[str, list[str]]:
        limits = {"min_decided": 50, "max_b_wrong": 0} | limits
        return accuracy.gate(found, at=at, signoff=SIGNOFF if found is row else None, **limits)

    assert gate() == ("not_met", GATE.format(**IDS).splitlines())
    status, said = gate(max_b_wrong=15, min_decided=117)
    assert status == "met" and said[1:] == gate()[1][1:]
    assert said[0] == (
        f"labelling {LABELLING} of cycle 2 meets the accuracy gate, which allows 15 decided items where side"
        " b was wrong and needs 117 decided (all, for a census of the discrepant items)"
    )
    assert gate(max_b_wrong=14, min_decided=117)[0] == "not_met"
    few = gate(min_decided=118, max_b_wrong=100)
    assert few == (
        "not_met",
        [f"labelling {LABELLING} of cycle 2 decided 117 items, under the 118 the gate needs", *said[1:]],
    )
    unlabelled = "labels its discrepant items: draw and label one (Labelled accuracy, Run screen)"
    none = ("not_met", [f"no labelling of {at} {unlabelled}"])
    hidden = labelling(tally([r for r in rows if r["population"] != "discrepant"], selection, TRUTH))
    assert hidden["accuracy"]["discrepant"] is None and gate(None) == gate(hidden) == none
    # A census: every discrepant item labelled, 3 of them decided, none finding side b wrong.
    every = _decided("discrepant", {"B_RIGHT": 3}, engine_outcome="MISMATCH")
    every += _rows(
        3, population="discrepant", label="UNDECIDABLE", reason="NO_TRUTH", engine_outcome="MISMATCH"
    )
    counts = selection["counts"] | {"discrepant": {"items": 6, "keys": 6, "causes": 0, "drawn": 6}}
    census = labelling(tally(every, selection | {"counts": counts, "causes": []}, TRUTH))
    status, said = gate(census)
    assert status == "met" and said[2] == (
        "b (new) wrong (truth favoured a, or neither): 0 of 3 decided (0%, every item labelled: exact; 0% to"
        " 50% counting the undecidable either way)."
    )
    assert said[1].endswith("as of 2026-10-01; no sign-off attests it.")
    assert gate(census, max_b_wrong=0, min_decided=1_000)[0] == "met"  # min_decided aside
    # A census that decided nothing (every item undecidable) never meets the gate.
    nothing = _rows(
        6, population="discrepant", label="UNDECIDABLE", reason="NO_TRUTH", engine_outcome="MISMATCH"
    )
    undecided = labelling(tally(nothing, selection | {"counts": counts, "causes": []}, TRUTH))
    assert undecided["accuracy"]["discrepant"]["census"] and gate(undecided) == (
        "not_met",
        [
            f"labelling {LABELLING} of cycle 2 decided 0 items, under the 1 the gate needs",
            said[1],
            "b (new) wrong (truth favoured a, or neither): 0 of 0 decided (no item decided).",
        ],
    )
    every += _decided("discrepant", {"A_RIGHT": 1}, engine_outcome="MISMATCH")
    counts = counts | {"discrepant": counts["discrepant"] | {"items": 7}}
    wrong = labelling(tally(every, selection | {"counts": counts, "causes": []}, TRUTH))
    assert gate(wrong)[0] == "not_met" and gate(wrong, max_b_wrong=1)[0] == "met"


def test_never_stated() -> None:
    """What ``lines`` and ``summary`` never say (§7): a verdict of DeltaSleuth's, either system's overall
    accuracy, a share without its n and interval, a pooled figure, a stratum, a certification, an AI label,
    or that undecidable items may be ignored; and BOTH_RIGHT never counts a side wrong."""
    rows, selection = worked()
    score = tally(rows, selection, TRUTH)
    said = [*lines(labelling(score), signoff=SIGNOFF), summary(labelling(score), signoff=SIGNOFF)]
    banned = r"certif|pooled|overall|strat|\bAI\b|ignor|correct|accura|proves|decides|DeltaSleuth|parity"
    assert not [line for line in said if re.search(banned, line, re.IGNORECASE)]
    for line in said[:-1]:  # a share names its count, its n and its interval; the range counts either way
        if "%" in line:
            assert re.search(r"[0-9,]+ of [0-9,]+ (decided )?\([0-9.]+%, 95% interval ", line), line
    assert all(
        text in said[0] for text in ("bob", "amy", "CBS-PROD.account_master", "2026-10-01", "2026-10-04")
    )
    # Readiness's accuracy gate is the third text builder: its own line names only the gate, and the rest
    # is the labelling line, without its people's names, and side b's wrong share verbatim
    # (docs/mvp/READINESS.md §9 Y18).
    gated = accuracy.gate(labelling(score), at="cycle 2", min_decided=50, max_b_wrong=0, signoff=SIGNOFF)[1]
    assert gated[1:] == [said[0].replace(" by amy", "").replace("by bob", "by a person"), said[7]]
    assert not [line for line in gated if "amy" in line or "bob" in line]
    assert not [line for line in gated if re.search(banned, line.replace("the accuracy gate", ""), re.I)]
    sides = score["discrepant"]["sides"]
    counts = Counter(r["label"] for r in rows if r["population"] == "discrepant")
    assert sides["b"]["wrong"]["x"] == counts["A_RIGHT"] + counts["BOTH_WRONG"] == 15
    assert sides["a"]["wrong"]["x"] == counts["B_RIGHT"] + counts["BOTH_WRONG"] == 87
    assert all(sides[s]["wrong"]["x"] + sides[s]["right_or_immaterial"]["x"] == 117 for s in "ab")
    # NOT_REVIEWED is counted apart, never among the decided.
    assert score["discrepant"]["reasons"]["NOT_REVIEWED"] == 1 and sides["b"]["wrong"]["n"] == 117
    assert accuracy.LIMITS.startswith("Observed. Labels record what the named labeller found")
    assert "not whether either system is correct overall" in accuracy.LIMITS


def test_names_side_and_its_warning() -> None:
    spec = unit_spec()  # side a: target svc-a (legacy); side b: svc-b (new)
    sources = ("svc-a", "SVC-B", "svc-b.accounts", "Svc-A:replica", "svc-b/v2", "svc-bank", TRUTH["source"])
    named = {source: accuracy.names_side(source, spec) for source in sources}
    assert named == {
        "svc-a": "a", "SVC-B": "b", "svc-b.accounts": "b", "Svc-A:replica": "a", "svc-b/v2": "b",
        "svc-bank": None, "CBS-PROD.account_master": None,
    }  # fmt: skip
    tables = compile_spec(yaml.safe_load(GRAPH_SPEC), targets=TARGETS)  # both sides read table links
    plan = {"a": {"schema": "legacy", "name": "links"}, "b": {"schema": "new", "name": "links"}}
    sources = ("LINKS", "new.links", "legacy.Links", "db-b", "x")
    assert [accuracy.names_side(s, tables, plan) for s in sources] == ["a", "b", "a", "b", None]
    assert accuracy.names_side("public.links", tables) is None  # the spec names no schema


# -- in the store -----------------------------------------------------------------------------------------


async def _cycle(ctx: Context, **spec: Any) -> tuple[str, dict[str, Principal]]:
    """The ended first cycle of a live run of the unit spec with an accepted and a bug rule over fourteen
    keys: four agree (one after normalisation), eight differ in five root causes (K060 under the bug rule,
    K040 and K041 held by one side only), K030 is accepted by KD-UNIT-1 and K050 timed out. Run under a spec
    approval bob requested and carol and erin granted; the people by name, olga and frank operators."""
    samples = {key: (doc(key), doc(key)) for key in ("K001", "K002", "K003")}
    samples |= {key: (doc(key, status="A"), doc(key, status="B")) for key in ("K010", "K011", "K012")}
    samples |= {
        "K020": (doc("K020", name="x"), doc("K020", name="y")),
        "K021": (doc("K021", name="x", status="A"), doc("K021", name="y", status="B")),  # two root causes
        "K030": (doc("K030", status="OLD"), doc("K030", status="NEW")),
        "K031": (doc("K031", name=" pad"), doc("K031", name="pad")),
        "K040": (doc("K040"), (404, {})),
        "K041": ((404, {}), doc("K041")),
        "K050": (doc("K050"), "timeout"),
        "K060": (doc("K060", status="X"), doc("K060", status="Y")),
    }
    version = unit_spec(knownDiffs=RULES, **spec)
    run_id = await seed_run(ctx, samples, spec=version, live=True)
    async with ctx.pool.connection() as conn:
        people = {name: await person(conn, name) for name in ("bob", "carol", "erin")}
        people |= {name: await person(conn, name, "operator") for name in ("olga", "frank")}
        run = await repo.get_run(conn, run_id)
        assert run is not None
        approval_id = await approved(
            conn, run["spec_id"], people["bob"], people["carol"], people["erin"],
            scopes=approvals.needs(version),
        )  # fmt: skip
        await repo.set_run_approval(conn, run_id, approval_id)
        await repo.end_cycle(conn, run_id)
    return run_id, people


def filled(sheet: str, *, notes: str = "", labels: dict[str, tuple[str, str, str]] | None = None) -> bytes:
    """The drawn sheet as a labeller fills it: every item ``b_right`` on the system of record (lower case,
    as typed), but the items of ``labels`` (item -> label, basis, reason), with ``notes`` on every row."""
    header, rows = parse_csv(sheet.encode())
    for row in rows:
        label, basis, reason = (labels or {}).get(row["item"], ("b_right", "system_of_record", ""))
        row |= {"label": label, "basis": basis, "reason": reason, "notes": notes}
    return render_csv(header, rows).encode()


def _digest(salt: str, item: tuple[str, str]) -> bytes:
    return sampling.digest(salt, f"{item[0]}\x1f{item[1]}")


async def _items(ctx: Context, run_id: str) -> dict[str, list[tuple[str, str]]]:
    """Each population's items (masked key, cause) as the stored results and findings give them (§2.1)."""
    async with ctx.pool.connection() as conn:
        results = await repo.pages(repo.list_results, conn, run_id, 1)
        findings = await repo.pages(repo.list_findings, conn, run_id, 1)
    found: dict[str, list[tuple[str, str]]] = {name: [] for name in accuracy.POPULATIONS}
    for r in results:
        own = [f for f in findings if f["key_masked"] == r["key_masked"]]
        bug = any(f["rule_classification"] == "bug" for f in own)
        causes = sorted({f["root_cause_id"] for f in own if f["root_cause_id"]})
        if r["outcome"] in ("MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B") or (
            r["outcome"] == "KNOWN_DIFF" and bug
        ):
            found["discrepant"] += [(r["key_masked"], cause) for cause in causes or ["*"]]
        elif r["outcome"] in ("KNOWN_DIFF", "NOISE_ONLY"):
            found["accepted"].append((r["key_masked"], "*"))
        elif r["outcome"] == "MATCH":
            found["agreed"].append((r["key_masked"], "*"))
    return found


@pytest.mark.db
async def test_the_draw_is_uniform_and_each_cause_holds_its_smallest_digests(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    async with ctx.pool.connection() as conn:  # a mask suppressed K040's document finding: no root cause
        await conn.execute(
            """UPDATE ds.findings SET suppressed = true, root_cause_id = NULL
               WHERE run_id = %s AND key_masked = 'K040'""",
            (run_id,),
        )
    population = await _items(ctx, run_id)
    assert sorted(population["discrepant"]) == sorted(
        [(key, cause) for key, cause in population["discrepant"] if cause != "*"] + [("K040", "*")]
    )
    assert len(population["discrepant"]) == 9 and {key for key, _ in population["discrepant"]} == {
        "K010", "K011", "K012", "K020", "K021", "K040", "K041", "K060",
    }  # fmt: skip
    assert [key for key, _ in population["accepted"]] == ["K030"]
    assert sorted(key for key, _ in population["agreed"]) == ["K001", "K002", "K003", "K031"]
    row, _ = await store_draw(
        ctx, run_id, 1, who=people["olga"], size=3, per_cause=2, accepted=5, agreed=2, truth=OBSERVED
    )
    salt, selection, items = row["salt"], row["selection"], row["selection"]["items"]
    assert re.fullmatch(r"V-[0-9A-Z]{26}", row["labelling_id"]) and re.fullmatch("[0-9a-f]{32}", salt)
    ordered = {name: sorted(found, key=lambda i: _digest(salt, i)) for name, found in population.items()}
    drawn = {name: [(i["key"], i["root_cause_id"]) for i in items if i["population"] == name]
             for name in accuracy.POPULATIONS}  # fmt: skip
    uniform = [(i["key"], i["root_cause_id"]) for i in items if (i["population"], i["selected_for"]) ==
               ("discrepant", "uniform")]  # fmt: skip
    assert uniform == ordered["discrepant"][:3]  # sorted(digest)[:size]
    assert drawn["discrepant"] == sorted(drawn["discrepant"], key=lambda i: _digest(salt, i))  # digest order
    for cause in {cause for _, cause in population["discrepant"]} - {"*"}:
        own = [i for i in ordered["discrepant"] if i[1] == cause]
        held = [i for i in drawn["discrepant"] if i[1] == cause]
        assert held == own[: len(held)] and len(held) >= min(2, len(own))  # its bottom m_h
    assert drawn["accepted"] == ordered["accepted"] and drawn["agreed"] == ordered["agreed"][:2]
    assert [i["item"] for i in items] == list(range(1, len(items) + 1))
    assert selection["counts"] == {
        "discrepant": {"items": 9, "keys": 8, "causes": 4, "drawn": len(drawn["discrepant"])},
        "accepted": {"keys": 1, "by_rule": {"KD-UNIT-1": 1}, "drawn": 1},
        "agreed": {"keys": 4, "drawn": 2},
        "indeterminate": 1, "not_compared": 0, "matched_by_row_hash": 0,
    }  # fmt: skip
    assert selection["uncovered"] == 0 and len(selection["causes"]) == 4
    assert [c["rank"] for c in selection["causes"]] == sorted(c["rank"] for c in selection["causes"])
    assert (selection["names_side"], selection["unlocatable"], selection["warnings"]) == (None, False, [])
    names = ("item", "key", "population", "root_cause_id", "selected_for")
    hashed = "".join("\t".join(str(i[name]) for name in names) + "\n" for i in items)
    assert row["selection_sha256"] == hashlib.sha256(hashed.encode()).hexdigest()
    header, rows = parse_csv(row["sheet"].encode())
    assert header == list(accuracy.SHEET_COLUMNS)
    by_key = {(r["key"], r["root_cause_id"]): r for r in rows}
    status = next(r for r in rows if r["path_pattern"] == "$.status" and r["engine_outcome"] == "MISMATCH")
    assert (status["path_pattern"], status["change_type"], status["engine_outcome"]) == (
        "$.status", "value_changed", "MISMATCH",
    )  # fmt: skip
    assert status["observed_at"].startswith("2026-09-24T") and status["obs_a_id"].startswith("O-")
    assert (
        by_key["K030", "*"]["rules"] == "KD-UNIT-1" and by_key["K030", "*"]["engine_outcome"] == "KNOWN_DIFF"
    )
    assert all(r["label"] == r["basis"] == r["reason"] == r["notes"] == "" for r in rows)
    async with ctx.pool.connection() as conn:
        audited = await repo.last_audit(conn, "accuracy.draw", obj=row["labelling_id"])
    assert audited is not None and (audited["actor"], audited["detail"]) == (people["olga"].actor, {
        "run_id": run_id, "cycle": 1, "salt": salt, "truth": OBSERVED, "size": 3,
        "per_cause": 2, "accepted": 5, "agreed": 2, "unlocatable": False, "item_count": len(items),
        "counts": {name: len(found) for name, found in drawn.items()},
        "selection_sha256": row["selection_sha256"],
    })  # fmt: skip
    # N ≤ size is a census: every item, exact counts and no interval.
    whole, _ = await store_draw(ctx, run_id, 1, who=people["olga"], size=100, per_cause=0, truth=TRUTH)
    labelled = await store_labels(ctx, whole["labelling_id"], filled(whole["sheet"]), who=people["frank"])
    d = labelled["accuracy"]["discrepant"]
    assert (d["uniform"], d["census"]) == (9, True)
    assert d["labels"]["B_RIGHT"] == share(9, 9, 9, census=True) | {"worst_case": None}
    assert d["labels"]["B_RIGHT"]["low"] is None and all(c["census"] for c in d["by_cause"])
    assert (
        "truth.as_of 2026-10-01 is outside the drawn items' observations (2026-09-24 to 2026-09-24)"
        in (whole["selection"]["warnings"][0])
    )


@pytest.mark.db
async def test_inclusion_is_uniform_over_1_000_salts(ctx: Context) -> None:
    samples = {f"K{i:03d}": (doc(f"K{i:03d}", status="A"), doc(f"K{i:03d}", status="B")) for i in range(100)}
    run_id = await seed_run(ctx, samples, live=True)
    chosen = Counter()
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, run_id)
        for n in range(1000):
            salt = f"{n:031x}a"
            drawn = await repo.labelling_draw(
                conn, run_id, 1, salt=salt, size=10, per_cause=0, accepted=0, agreed=0, timeout_s=30
            )
            chosen.update(r["key_masked"] for r in drawn if r["kind"] == "uniform")
    assert sum(chosen.values()) == 10_000 and len(chosen) == 100
    assert sum((c - 100) ** 2 / 100 for c in chosen.values()) < chi2.ppf(0.999, 99)


@pytest.mark.db
async def test_labels_are_stored_once_without_notes_and_the_score_is_re_derived(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    olga, frank = people["olga"], people["frank"]
    drawn, _ = await store_draw(ctx, run_id, 1, who=olga, size=9, accepted=1, agreed=4, truth=TRUTH)
    _, blank = parse_csv(drawn["sheet"].encode())
    labels = {
        "1": ("a_right", "source_document", ""),
        "2": ("Undecidable", "", "not_reviewed"),
        "3": ("UNDECIDABLE", "", "NOT_LOCATABLE"),
        "4": (" both_wrong ", " OWNER_CONFIRMED ", ""),
    }
    upload = filled(drawn["sheet"], notes=f"called the branch about {RAW_KEY}: {RAW_VALUE}", labels=labels)
    # A note may be as long as the upload allows: over the CSV reader's own 131,072 characters too.
    upload = upload.replace(b"called the branch", b"called the branch" + b" again" * 25_000, 1)
    assert 131_072 < len(upload) < accuracy.MAX_UPLOAD
    before = await _state(ctx)
    row = await store_labels(ctx, drawn["labelling_id"], upload, who=frank)
    with pytest.raises(AdjudicationConflict) as again:
        await store_labels(ctx, drawn["labelling_id"], upload, who=frank)
    assert str(again.value) == f"labelling {drawn['labelling_id']} is already labelled: draw a new sheet"
    # The sheet kept is the drawn one with the labels, upper-cased: no note, nothing else typed.
    _, kept = parse_csv(row["sheet"].encode())
    said = {n: tuple(cell.strip().upper() for cell in found) for n, found in labels.items()}
    assert kept == [
        b
        | dict(
            zip(
                ("label", "basis", "reason"),
                said.get(b["item"], ("B_RIGHT", "SYSTEM_OF_RECORD", "")),
                strict=True,
            )
        )
        for b in blank
    ]
    score = row["accuracy"]
    async with ctx.pool.connection() as conn:
        audited = await repo.audit_chain(conn, after=0)
        stored = await repo.get_labelling(conn, drawn["labelling_id"])
    everything = repr(stored) + "".join(a["text"] for a in audited)
    assert RAW_KEY not in everything and RAW_VALUE not in everything and "called the branch" not in everything
    # No hash of the upload either: with the kept sheet at hand, it would be a hash of the notes alone.
    assert "upload_sha256" not in stored and hashlib.sha256(upload).hexdigest() not in everything  # type: ignore[operator]
    keyless = accuracy.keyless(row["sheet"])
    header, rows = parse_csv(keyless.encode())
    assert header == list(accuracy.KEYLESS_COLUMNS) and "item" not in header
    assert not re.search(r"K0[0-9]{2}|O-[0-9a-f]{16}|2026-09-24", keyless)  # no key, observation or time
    # Its rows are sorted by their own cells, not in the sheet's order, which follows the keys' digests.
    cells = [[r[c] for c in header] for r in rows]
    assert cells == sorted(cells) != [[r[c] for c in header] for r in kept]
    assert row["sheet_sha256"] == sha256_hex(row["sheet"]) and row["labels_sha256"] == sha256_hex(keyless)
    assert row["score_sha256"] == sha256_hex(jsonio.dumps(score, sort_keys=True))
    truth = TRUTH | {"names_side": None}
    assert tally(rows, drawn["selection"], truth) == score == stored["accuracy"]  # type: ignore[index]
    assert (row["labelled_by"], stored["labelled_by_name"]) == (frank.actor, "frank")  # type: ignore[index]
    assert "labelled_by" not in jsonio.dumps(score) and frank.actor not in jsonio.dumps(score)  # no actor
    d = score["discrepant"]
    assert {label: d["labels"][label]["x"] for label in LABELS} == {
        "A_RIGHT": 1, "B_RIGHT": 5, "BOTH_WRONG": 1, "BOTH_RIGHT": 0, "UNDECIDABLE": 2,
    }  # fmt: skip
    assert d["reasons"] == {"NOT_LOCATABLE": 1, "NOT_REVIEWED": 1}
    assert d["basis_codes"] == {"SYSTEM_OF_RECORD": 5, "SOURCE_DOCUMENT": 1, "OWNER_CONFIRMED": 1}
    assert score["accepted"]["by_rule"][0]["rule_id"] == "KD-UNIT-1" and score["agreed"]["census"]
    hashes = {name: row[name] for name in ("sheet_sha256", "labels_sha256", "score_sha256")}
    label = [json.loads(a["text"]) for a in audited if a["action"] == "accuracy.label"]
    assert [(a["actor"], a["object"], a["detail"]) for a in label] == [(
        frank.actor, drawn["labelling_id"],
        {"run_id": run_id, "cycle": 1} | hashes | {"labelled": 14, "undecidable": 2, "not_reviewed": 1},
    )]  # fmt: skip
    assert before != await _state(ctx)
    said = lines(stored, signoff=None)  # type: ignore[arg-type]
    assert said[0] == (
        f"Labelling {drawn['labelling_id']} drawn {TODAY} by olga, labelled {TODAY} by frank against"
        " CBS-PROD.account_master as of 2026-10-01; no sign-off attests it."
    )
    assert "side a is legacy, side b is new" in said[1] and summary(stored, signoff=None).startswith(  # type: ignore[arg-type]
        f"{drawn['labelling_id']} run {run_id} cycle 1: 9 of 9 discrepancies labelled {TODAY} by frank"
    )


@pytest.mark.db
async def test_a_stored_labelling_prints_its_lines_in_the_golden_order(ctx: Context) -> None:
    """jsonb orders an object's keys by length, then bytes: a stored score's reasons and basis codes, and a
    stored selection's rules, come back in another order than they were counted in. The lines keep §7's,
    the enumerations', and the score re-derived from the stored selection is the stored one."""
    run_id, people = await _cycle(ctx)
    rows, selection = worked()
    score = tally(rows, selection, TRUTH)
    async with ctx.pool.connection() as conn:
        drawn = await repo.insert_labelling(
            conn, drawn_labelling(run_id, people["olga"], selection=selection, truth_as_of=TRUTH["as_of"])
        )
        await repo.label_labelling(
            conn, drawn["labelling_id"], sheet="item\r\n", sheet_sha256="2" * 64, labels_sha256="3" * 64,
            accuracy=score, score_sha256="4" * 64, labelled_by=people["frank"].actor,
        )  # fmt: skip
        stored = await repo.get_labelling(conn, drawn["labelling_id"])
    assert stored is not None
    d = stored["accuracy"]["discrepant"]
    assert (list(d["reasons"]), list(d["basis_codes"])) == (
        ["NOT_REVIEWED", "NOT_LOCATABLE"],
        ["BUSINESS_RULE", "SYSTEM_OF_RECORD"],
    )
    assert list(stored["selection"]["counts"]["accepted"]["by_rule"]) == ["mask", "fx_rounding"]
    assert lines(stored, signoff=None)[2:] == GOLDEN.format(**IDS).splitlines()[2:]
    assert tally(rows, stored["selection"], TRUTH) == score == stored["accuracy"]
    assert [rule["rule_id"] for rule in stored["accuracy"]["accepted"]["by_rule"]] == ["fx_rounding", "mask"]


def test_a_key_is_unlocatable_when_no_path_resolves_clear() -> None:
    """§3 step 3 judges by the compiled policy: a tokenise rule of a PII class voids a clear entry beneath
    it, so a spec that lists a path in clear may show none."""
    key = {"name": "account", "type": "string", "pattern": "^[0-9]{10}$", "class": "identifier",
           "source": {"kind": "list", "keys": ["0712345601"]}}  # fmt: skip
    everything, name = {"path": "$", "class": "identifier"}, {"path": "$.name", "class": "name"}

    def unlocatable(**security: Any) -> bool:
        return accuracy._unlocatable(unit_spec(key=key, security=security))

    assert unlocatable(unlisted="tokenize", tokenize=[everything], clear=["$.status"])
    assert unlocatable(unlisted="clear", tokenize=[everything])
    assert unlocatable(unlisted="tokenize", tokenize=[name], clear=["$.name.first"])
    assert unlocatable(unlisted="tokenize", tokenize=[name])
    assert not unlocatable(unlisted="tokenize", tokenize=[name], clear=["$.status"])
    assert not unlocatable(unlisted="clear", tokenize=[name])
    shown = unit_spec(security={"unlisted": "tokenize", "tokenize": [everything], "clear": ["$.status"]})
    assert shown.key.value_class == "business_code" and not accuracy._unlocatable(shown)  # its key is clear


async def _state(ctx: Context) -> tuple[Any, ...]:
    """Every labelling and how many audit rows there are: what a refusal leaves as it was."""
    async with ctx.pool.connection() as conn:
        cur = await conn.execute("SELECT to_jsonb(v) FROM ds.labellings v ORDER BY labelling_id")
        rows = await cur.fetchall()
        return rows, await repo.chain_head(conn)


async def _refused(error: type[Exception], coroutine: Any) -> str:
    with pytest.raises(error) as caught:
        await coroutine
    return str(caught.value)


@pytest.mark.db
async def test_what_a_draw_refuses(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    olga = people["olga"]

    def draw(**options: Any) -> Any:
        return store_draw(
            ctx, options.pop("run", run_id), options.pop("cycle", 1), who=olga, **{"truth": TRUTH} | options
        )

    before = await _state(ctx)
    sizes = "size must be 0 to 1,000, per_cause 0 to 20, accepted and agreed 0 to 500"
    source = (
        "truth.source must be a system identifier: 1 to 64 letters, digits and _ . : / -, starting with a"
        " letter"
    )
    as_of = "truth.as_of must be a date (YYYY-MM-DD) no later than today"
    tomorrow = f"{datetime.now(UTC) + timedelta(days=1):%Y-%m-%d}"
    refusals = [
        ({"size": 1001}, sizes), ({"per_cause": 21}, sizes), ({"accepted": 501}, sizes),
        ({"agreed": -1}, sizes),
        ({"size": 0, "per_cause": 0},
         "a labelling draws at least one item: set size, per_cause, accepted or agreed above 0"),
        ({"size": 600, "accepted": 300, "agreed": 300},
         "a labelling holds at most 1,000 items: size, accepted and agreed ask for 1,200"),
        ({"truth": {"source": "the core banking system", "as_of": "2026-10-01"}}, source),
        ({"truth": {"source": "9-CBS", "as_of": "2026-10-01"}}, source),
        ({"truth": {"source": "C" * 65, "as_of": "2026-10-01"}}, source),
        ({"truth": {"as_of": "2026-10-01"}}, source),
        ({"truth": {"source": "CBS", "as_of": "01/10/2026"}}, as_of),
        ({"truth": {"source": "CBS", "as_of": "20261001"}}, as_of),
        ({"truth": {"source": "CBS", "as_of": tomorrow}}, as_of),
        ({"truth": {"source": "CBS"}}, as_of),
    ]  # fmt: skip
    for options, message in refusals:
        assert await _refused(AdjudicationError, draw(**options)) == message, options
    missing = "R-" + "0" * 26
    assert await _refused(LookupError, draw(run=missing)) == f"run {missing} not found"
    assert await _refused(AdjudicationConflict, draw(cycle=2)) == f"cycle 2 of run {run_id} has not ended"
    assert before == await _state(ctx)
    # A population asked for and absent: refused, each named.
    empty = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}, live=True)
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, empty)
    at = f"cycle 1 of run {empty}"
    assert await _refused(AdjudicationConflict, draw(run=empty, accepted=1, agreed=1)) == (
        f"{at} has no discrepant item to label: set size and per_cause to 0; {at} has no accepted key to"
        " label: set accepted to 0"
    )
    assert await _refused(AdjudicationConflict, draw(run=empty, size=0, per_cause=1)) == (
        f"{at} has no discrepant item to label: set size and per_cause to 0"
    )
    row, _ = await draw(run=empty, size=0, per_cause=0, agreed=1)
    assert [(i["key"], i["population"]) for i in row["selection"]["items"]] == [("K001", "agreed")]
    different = await seed_run(ctx, {"K010": (doc("K010", status="A"), doc("K010", status="B"))}, live=True)
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, different)
    at = f"cycle 1 of run {different}"
    assert await _refused(AdjudicationConflict, draw(run=different, accepted=1, agreed=1)) == (
        f"{at} has no accepted key to label: set accepted to 0; {at} has no agreed key to label: set agreed"
        " to 0"
    )
    async with ctx.pool.connection() as conn:  # no discrepant item has a root cause: none to cover
        await conn.execute("UPDATE ds.findings SET root_cause_id = NULL WHERE run_id = %s", (run_id,))
    assert await _refused(AdjudicationConflict, draw(size=0, per_cause=2)) == (
        f"cycle 1 of run {run_id} has no root cause to cover: set size above 0"
    )


@pytest.mark.db
async def test_a_sheet_too_large_for_notes_is_refused_and_row_hash_matches_are_warned_of(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3 step 6: a blank sheet over half the upload limit stores nothing. §3's last warning: a table scan's
    rows matched by row hash have no per-key result, so the agreed keys are those read per key."""
    scan = {"done": True, "after": None, "rows_a": 4_998_918, "rows_b": 4_998_918, "skipped_a": 0,
            "skipped_b": 0, "matched": 4_998_916, "candidates": 2}  # fmt: skip
    pairs = {key: (doc(key), doc(key)) for key in ("K001", "K002")}
    run_id = await seed_run(ctx, pairs, live=True, scan=scan)
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, run_id)
        olga = await person(conn, "olga", "operator")
    before, row_of = await _state(ctx), accuracy._sheet_row
    with monkeypatch.context() as patched:  # a root cause's pattern as long as a sheet may be
        long = {"path_pattern": "x" * (1 << 19)}
        patched.setattr(accuracy, "_sheet_row", lambda *item: row_of(*item) | long)
        draw = store_draw(ctx, run_id, 1, who=olga, size=0, per_cause=0, agreed=2, truth=TRUTH)
        assert await _refused(AdjudicationError, draw) == (
            "the sheet of this labelling would be over 512 KiB, leaving too little room for notes: draw fewer"
            " items"
        )
    assert before == await _state(ctx)
    row, _ = await store_draw(ctx, run_id, 1, who=olga, size=0, per_cause=0, agreed=2, truth=OBSERVED)
    assert row["selection"]["counts"]["matched_by_row_hash"] == 4_998_916
    assert row["selection"]["warnings"] == [
        "4,998,916 rows matched by row hash cannot be sampled: agreed keys are the 2 read per key"
    ]


@pytest.mark.db
async def test_a_tokenised_key_without_a_clear_path_needs_its_acknowledgement(ctx: Context) -> None:
    key = {"name": "account", "type": "string", "pattern": "^[0-9]{10}$", "class": "identifier",
           "source": {"kind": "list", "keys": ["0712345601"]}}  # fmt: skip
    # Every path is tokenised, the one listed in clear too: a rule of a PII class covers them all.
    security = {"unlisted": "tokenize", "tokenize": [{"path": "$", "class": "identifier"}],
                "clear": ["$.status"]}  # fmt: skip
    spec = unit_spec(key=key, security=security)
    samples = {f"07123456{i:02d}": (doc("x", status="A"), doc("x", status="B")) for i in range(1, 4)}
    run_id = await seed_run(ctx, samples, spec=spec, live=True)
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, run_id)
        olga = await person(conn, "olga", "operator")
    before = await _state(ctx)
    assert await _refused(AdjudicationError, store_draw(ctx, run_id, 1, who=olga, truth=TRUTH)) == (
        "the key is tokenised (class identifier) and the spec shows no path in clear: labellers cannot find"
        " records in the truth source until the per-key reveal (AUTH §11) exists; set"
        " acknowledge_unlocatable to draw for labels a business rule decides without the record"
        " (docs/mvp/ACCURACY.md §4)"
    )
    shaped = TRUTH | {"source": "CBS.0712345601"}
    assert await _refused(AdjudicationError, store_draw(ctx, run_id, 1, who=olga, truth=shaped)) == (
        "truth.source holds a key-shaped value: name the system without customer keys"
    )
    assert before == await _state(ctx)
    circular = TRUTH | {"source": "svc-a.accounts", "as_of": "2026-09-20"}
    row, _ = await store_draw(ctx, run_id, 1, who=olga, truth=circular, acknowledge_unlocatable=True)
    selection = row["selection"]
    assert (selection["unlocatable"], selection["names_side"]) == (True, "a")
    assert selection["warnings"] == [
        "truth.source svc-a.accounts names side a (legacy): labels resting on a compared side are circular"
        " and measure nothing; name an authoritative system other than both sides (docs/mvp/ACCURACY.md"
        " §2.3)",
        "the key is tokenised (class identifier) and the spec shows no path in clear: expect UNDECIDABLE"
        " (TOKENISED) rows (acknowledged)",
        "truth.as_of 2026-09-20 is outside the drawn items' observations (2026-09-24 to 2026-09-24): label"
        " each item as the truth stood at its observed_at, UNDECIDABLE (CHANGED_SINCE) where the source"
        " cannot show it",
    ]
    assert not any(raw in row["sheet"] for raw in samples) and "tok:identifier:" in row["sheet"]
    # A retry without the acknowledgement is not the same draw: refused, never the first labelling.
    assert "set acknowledge_unlocatable" in await _refused(
        AdjudicationError, store_draw(ctx, run_id, 1, who=olga, truth=circular)
    )
    again, warned = await store_draw(ctx, run_id, 1, who=olga, truth=circular, acknowledge_unlocatable=True)
    assert again["labelling_id"] == row["labelling_id"] and warned == accuracy.repeated(row)
    labelled = await store_labels(ctx, row["labelling_id"], filled(row["sheet"]), who=olga)
    score = labelled["accuracy"]
    assert (score["unlocatable"], score["truth"]) == (True, circular | {"names_side": "a"})
    async with ctx.pool.connection() as conn:
        stored = await repo.get_labelling(conn, row["labelling_id"])
    assert stored is not None and summary(stored, signoff=None).endswith(
        "; CIRCULAR: the truth source names side a; records not locatable (tokenised key)"
    )
    assert lines(stored, signoff=None)[1].startswith("Warning: the truth source names side a (legacy)")


@pytest.mark.db
async def test_what_an_upload_refuses_changes_nothing(ctx: Context) -> None:
    run_id, people = await _cycle(ctx)
    olga = people["olga"]
    drawn, _ = await store_draw(ctx, run_id, 1, who=olga, size=9, per_cause=0, truth=TRUTH)
    labelling_id = drawn["labelling_id"]
    header, rows = parse_csv(filled(drawn["sheet"]))

    def sheet(
        *changes: tuple[int, dict[str, str]], drop: tuple[int, ...] = (), columns: Any = header
    ) -> bytes:
        edited = [dict(row) for row in rows]
        for index, cells in changes:
            edited[index] |= cells
        return render_csv(columns, [row for n, row in enumerate(edited) if n not in drop]).encode()

    undecidable = {"label": "UNDECIDABLE", "basis": "", "reason": "NO_TRUTH"}
    every = (
        "every drawn item needs a label: mark UNDECIDABLE with a reason where the truth cannot be"
        " established, with reason NOT_REVIEWED where nobody reviewed it"
    )
    refusals = [
        ("café,ü".encode("latin-1"),
         "the labelled sheet is not UTF-8 text (a spreadsheet's plain 'CSV' export writes Windows-1252): save"
         " it as 'CSV UTF-8' and upload it again"),
        (b"", "the labelled sheet cannot be read as a CSV sheet: the labelled sheet is empty"),
        (sheet(columns=[*header, "label"]), "the labelled sheet names column(s) label more than once"),
        (sheet().replace(b",notes\r\n", b",notes,'basis,'label\r\n", 1),  # a header cell is unescaped too
         "the labelled sheet names column(s) label, basis more than once"),
        (sheet(columns=[c for c in header if c not in ("label", "basis")]),
         "the labelled sheet lacks column(s) label, basis"),
        (sheet() + render_csv(header, [rows[6], rows[8]]).split("\r\n", 1)[1].encode(),
         "the sheet repeats item(s): each may appear once (lines 8, 10, 11, 12)"),
        (sheet((3, {"item": "99"})), "row(s) for items the labelling did not draw (lines 5)"),
        (sheet((0, {"label": ""}), drop=(4,)), f"{every} (items 1, 5)"),
        (sheet((3, {"key": "K999"}), (5, {"engine_outcome": "MATCH"}), (6, {"root_cause_id": "*"})),
         "engine columns differ from the labelling's items (lines 5, 7, 8) (import the key column as text: a"
         " spreadsheet drops leading zeros)"),
        (sheet((2, {"label": "A"}), (7, {"label": "MATCH"})),
         "label must be one of A_RIGHT, B_RIGHT, BOTH_WRONG, BOTH_RIGHT, UNDECIDABLE (lines 4, 9)"),
        (sheet((1, {"basis": ""}), (2, {"basis": "HEARSAY"}), (3, undecidable | {"basis": "BUSINESS_RULE"})),
         "basis must be one of SYSTEM_OF_RECORD, SOURCE_DOCUMENT, BUSINESS_RULE, OWNER_CONFIRMED for a"
         " decided label, and empty for UNDECIDABLE (lines 3, 4, 5)"),
        (sheet((4, {"reason": "NO_TRUTH"}), (5, undecidable | {"reason": ""}),
               (6, undecidable | {"reason": "x"})),
         "reason must be one of NOT_LOCATABLE, TOKENISED, NO_TRUTH, TRUTH_CONFLICTS, CHANGED_SINCE,"
         " NOT_REVIEWED for UNDECIDABLE, and empty otherwise (lines 6, 7, 8)"),
    ]  # fmt: skip
    # A quote left open in a note swallows the rows after it, more characters than the CSV reader's own
    # limit: the sheet is refused for the labels it then lacks.
    lines_ = sheet().split(b"\r\n")
    swallowed = b"\r\n".join([lines_[0], lines_[1] + b'"' + b"x" * 140_000, *lines_[2:]])
    refusals.append((swallowed, f"{every} (items 2, 3, 4, 5, 6, 7, 8, 9)"))
    before = await _state(ctx)
    for upload, message in refusals:
        assert await _refused(AdjudicationError, store_labels(ctx, labelling_id, upload, who=olga)) == message
    with pytest.MonkeyPatch.context() as patched:  # what the CSV reader itself refuses is a refusal too
        patched.setattr(csv, "field_size_limit", lambda *limit: 131_072)
        unread = store_labels(ctx, labelling_id, swallowed, who=olga)
        assert await _refused(AdjudicationError, unread) == (
            "the labelled sheet cannot be read as a CSV sheet: the labelled sheet line 2: field larger than"
            " field limit (131072)"
        )
    assert await _refused(UploadTooLarge, store_labels(ctx, labelling_id, b" " * (2**20 + 1), who=olga)) == (
        "the labelled sheet is over 1 MiB: empty the notes column and upload it again"
    )
    missing = "V-" + "0" * 26
    assert await _refused(LookupError, store_labels(ctx, missing, sheet(), who=olga)) == (
        f"labelling {missing} not found"
    )
    assert before == await _state(ctx)  # a refused upload changes nothing
    # The checks run in §11's order: a sheet failing several is refused for the first.
    several = sheet((0, {"label": "", "key": "K999"}), (1, {"label": "A", "basis": "x", "reason": "y"}))
    assert (await _refused(AdjudicationError, store_labels(ctx, labelling_id, several, who=olga))).startswith(
        every
    )
    # Only the labels come from the upload: the other columns a labeller edited stay as drawn.
    edited = sheet((0, {"selected_for": "coverage", "path_pattern": "$.edited", "observed_at": "24/09/2026"}))
    row = await store_labels(ctx, labelling_id, edited, who=olga)
    assert "$.edited" not in row["sheet"] and row["accuracy"]["discrepant"]["uniform"] == 9
    # Under a requested sign-off the cycle is frozen; purged, it answers 410.
    other, _ = await store_draw(ctx, run_id, 1, who=olga, size=2, per_cause=0, truth=TRUTH)
    async with ctx.pool.connection() as conn:
        signoff = await request_signoff(conn, run_id, 1, olga, unit_spec(knownDiffs=RULES))
    frozen = f"cycle 1 of run {run_id} has a sign-off ({signoff}): revoke it before a new labelling"
    upload = filled(other["sheet"])
    assert (
        await _refused(AdjudicationConflict, store_labels(ctx, other["labelling_id"], upload, who=olga))
        == frozen
    )
    assert await _refused(AdjudicationConflict, store_draw(ctx, run_id, 1, who=olga, truth=TRUTH)) == frozen
    async with ctx.pool.connection() as conn:
        await repo.decide_approval(conn, signoff, people["carol"].actor, approve=False)
    await purge.erase(ctx, TESTER, run_id, "test erasure", cycle=1)
    gone = (
        f"cycle 1 of run {run_id} was purged on {TODAY} (erasure): its keys, results, findings and evidence"
        " are deleted"
    )
    assert await _refused(CyclePurged, store_labels(ctx, other["labelling_id"], upload, who=olga)) == gone
    assert await _refused(CyclePurged, store_labels(ctx, labelling_id, upload, who=olga)) == gone
    assert await _refused(CyclePurged, store_draw(ctx, run_id, 1, who=olga, truth=TRUTH)) == gone
    async with ctx.pool.connection() as conn:
        listed = await repo.list_labellings(conn, run_id, 1)
        tombstones = {v["labelling_id"]: v for v in await repo.list_labellings(conn, run_id, 1, full=True)}
    kept, blank = tombstones[labelling_id], tombstones[other["labelling_id"]]
    assert all(
        v["purged_at"] and v["sheet"] is None and v["selection"]["items"] is None for v in tombstones.values()
    )
    # A list reads neither a sheet nor a selection, and of a score what its one line states.
    assert all(v["purged_at"] and "sheet" not in v and "selection" not in v for v in listed)
    assert [v["item_count"] for v in listed] == [2, 9] and listed[0]["accuracy"] is None
    assert "by_cause" not in listed[1]["accuracy"]["discrepant"] and summary(listed[1], signoff=None) == (
        summary(kept, signoff=None)
    )
    names = (
        "accuracy",
        "labels_sha256",
        "score_sha256",
        "sheet_sha256",
        "labelled_by",
        "salt",
        "truth_source",
    )
    assert {name: kept[name] for name in names} == {name: row[name] for name in names}
    assert kept["selection"] == row["selection"] | {"items": None} and blank["accuracy"] is None
    assert lines(kept, signoff=None)[0].startswith(
        f"Labelling {labelling_id} drawn {TODAY} by olga, labelled"
    )
    assert (
        lines(kept, signoff=None)[1]
        == f"Labelling {labelling_id} is one of 2 drawn for cycle 1 (1 labelled)."
    )
    assert summary(kept, signoff=None).startswith(
        f"{labelling_id} run {run_id} cycle 1: 9 of 9 discrepancies"
    )
