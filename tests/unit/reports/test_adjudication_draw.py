"""A reviewer sheet's draw and score in the store read what they choose from, never the cycle: over a
synthetic ended cycle of 3,000 keys (non-ASCII keys, every outcome, root causes that are some keys'
primary one and others no key's, ties on severity, pointer and finding order) the stored selection, sheet
and score equal what the selection read from every result and finding computed, for several limits and
salts and however few keys of a root cause are read ahead; and a draw or a score returns fewer rows than
the cycle holds findings."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any

import pytest

from deltasleuth.context import Context
from deltasleuth.reports import adjudicate
from deltasleuth.reports.adjudicate import (
    _ROOT_CAUSE,
    EXIT_MINIMUM,
    MATCH_SHARE,
    SHEET_COLUMNS,
    _check_rows,
    _hash,
    _read_sheet,
    _sheet_row,
    _tally,
    primary_root_cause,
    root_cause_order,
    root_causes_of,
    store_draw,
    store_score,
)
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.runner.counters import verdict
from deltasleuth.store import repo
from deltasleuth.store.repo import Row
from tests.conftest import TESTER
from tests.unit.store.four_eyes import counters, end, start, upload

from .conftest import unit_spec

pytestmark = pytest.mark.db

N = 3_000
Finding = tuple[str | None, str | None, str, bool, str | None, str | None]  # cause, severity, pointer, ...


def cause(n: int) -> str:
    return f"RC-{n:012x}"


def key(i: int) -> str:
    """A masked key shown in clear: every 97th holds non-ASCII letters."""
    return f"Ключ-ü{i:05d}" if i % 97 == 0 else f"K{i:05d}"


def found(rc: str | None, severity: str | None, pointer: str, suppressed: bool = False,
          rule: str | None = None, classification: str | None = None) -> Finding:  # fmt: skip
    return rc, severity, pointer, suppressed, rule, classification


def synthetic(n: int) -> dict[str, tuple[str, list[Finding]]]:
    """``n`` results by masked key: its outcome and findings (root cause, severity, pointer, suppressed,
    rule, classification), in finding order. Root causes 7 to 11 and 20 are never a key's primary one."""
    results: dict[str, tuple[str, list[Finding]]] = {}
    for i in range(n):
        held: list[Finding] = []
        if i % 250 == 1:  # a root cause of its own, and one shared with keys that rank it no higher
            outcome, held = "MISMATCH", [found(cause(100 + i // 250), "CRITICAL", "/u")]
            held.append(found(cause(20), "INFO", "/z"))
        elif i % 250 == 3:
            outcome, held = "MISMATCH", [found(cause(22), "INFO", "/a"), found(cause(20), "INFO", "/z")]
        else:
            match i % 12:
                case 6:
                    outcome, held = "MISMATCH", [found(cause(i % 7), "HIGH", "/a")]
                    held += [found(cause(7 + i % 5), "LOW", "/b")] if i % 3 == 0 else []
                    held.append(found(None, "INFO", "/n"))
                case 7 if i % 24 == 7:
                    outcome = "KNOWN_DIFF"
                    held = [found(cause(12 + i % 3), "MEDIUM", "/k", True, "KD-1", "bug")]
                case 7:
                    outcome, held = "KNOWN_DIFF", [found(cause(15), None, "/k", True, "KD-2", "accepted")]
                case 8:
                    outcome, held = "NOISE_ONLY", [found(None, "INFO", "/n", True)]
                case 9 if i % 24 == 9:
                    outcome, held = "NOT_FOUND_B", [found(cause(16 + i % 2), "CRITICAL", "")]
                case 9:
                    outcome = "INCONCLUSIVE"
                case 10:
                    outcome = "DRIFT"
                case 11 if i % 24 == 11:  # the same pointer: finding order decides
                    outcome = "MISMATCH"
                    held = [found(cause(18), "HIGH", "/t"), found(cause(19), "HIGH", "/t")]
                case 11:  # "/e" before "/é" by code point
                    outcome = "MISMATCH"
                    held = [found(cause(18), "HIGH", "/é"), found(cause(19), "HIGH", "/e")]
                case _:
                    outcome = "MATCH"
        results[key(i)] = (outcome, held)
    return results


async def fill(conn: repo.Conn, run_id: str, results: Mapping[str, tuple[str, list[Finding]]]) -> None:
    """The run's first cycle holding ``results``."""
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO ds.run_keys (run_id, cycle, key_masked, key_enc, seq, state)
               VALUES (%s, 1, %s, decode(repeat('01', 30), 'hex'), %s, 'done')""",
            [(run_id, masked, seq) for seq, masked in enumerate(results, start=10)],
        )
        await cur.executemany(
            """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
                   inconclusive_reason, drift_reason, confirmation, first_outcome)
               VALUES (%s, 1, %s, %s, 'COMPLETE', 'COMPLETE', %s, %s, %s, %s)""",
            [
                (run_id, masked, outcome, "timeout" if outcome == "INCONCLUSIVE" else None,
                 "changed_since" if outcome == "DRIFT" else None,
                 "provisional" if outcome == "NOT_FOUND_B" else "rechecked", outcome)
                for masked, (outcome, _) in results.items()
            ],
        )  # fmt: skip
        await cur.executemany(
            """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, taxonomy_code, severity,
                   change_type, direction, path_pattern, json_pointer, rule_id, rule_classification,
                   suppressed, root_cause_id)
               VALUES (%s, 1, %s, %s, 'F-' || substr(md5(%s), 1, 16), 'VALUE_MISMATCH', %s, 'value_changed',
                       'both', '$.x', %s, %s, %s, %s, %s)""",
            [
                (run_id, masked, seq, f"{run_id}/{masked}/{seq}", severity, pointer, rule, classification,
                 suppressed, rc)
                for masked, (_, findings) in results.items()
                for seq, (rc, severity, pointer, suppressed, rule, classification) in enumerate(findings)
            ],
        )


@pytest.fixture
async def cycle(ctx: Context) -> str:
    """An ended first cycle of ``synthetic(N)``, beside the run's three keys without a result: its run."""
    async with ctx.pool.connection() as conn:
        spec_id = await upload(conn, unit_spec(), TESTER)
        run_id = await start(conn, spec_id, TESTER)
        await fill(conn, run_id, synthetic(N))
        await end(conn, run_id, counters(1))
    return run_id


async def everything(ctx: Context, run_id: str) -> list[Row]:
    """Every result of the cycle with its findings and verdict, as the draw once read them."""
    async with ctx.pool.connection() as conn:
        results = await repo.pages(repo.list_results, conn, run_id, 1)
        findings = await repo.pages(repo.list_findings, conn, run_id, 1)
    by_key: dict[str, list[Row]] = defaultdict(list)
    for f in findings:
        by_key[f["key_masked"]].append(f)
    for r in results:
        r["findings"] = by_key[r["key_masked"]]
        r["verdict"] = verdict(r, r["findings"]).value
    return results


def reference(records: Sequence[Row], limit: int, salt: str) -> list[tuple[Row, str]]:
    """The selection as it read every result and finding of the cycle."""
    pool = sorted(records, key=lambda r: _hash(salt, r))
    population = Counter(r["outcome"] for r in pool)
    quota = dict.fromkeys(population, 1)
    if matches := population["MATCH"]:
        share = min(MATCH_SHARE, Fraction(matches, len(pool)))
        quota["MATCH"] = min(matches, max(1, math.ceil(limit * share)))
    causes = {r["key_masked"]: set(root_causes_of(r)) for r in pool}
    primary = {r["key_masked"]: primary_root_cause(r) for r in pool}
    chosen: dict[str, str] = {}
    taken: Counter[str] = Counter()

    def take(r: Row, why: str) -> None:
        chosen[r["key_masked"]] = why
        taken[r["outcome"]] += 1

    def owed_after(outcome: str) -> int:
        return sum(max(0, q - taken[o] - (o == outcome)) for o, q in quota.items())

    represented: set[str | None] = set()
    for rc in root_cause_order(pool):
        candidates = [r for r in pool if rc in causes[r["key_masked"]] and r["key_masked"] not in chosen]
        if rc in represented or not candidates:
            continue
        r = next((c for c in candidates if primary[c["key_masked"]] == rc), candidates[0])
        if len(chosen) + 1 + owed_after(r["outcome"]) <= limit:
            take(r, _ROOT_CAUSE + rc)
            represented |= {rc, primary[r["key_masked"]]}
    for outcome in sorted(quota, key=lambda o: (o != "MATCH", o)):
        for r in pool:
            if taken[outcome] >= quota[outcome] or len(chosen) >= limit:
                break
            if r["outcome"] == outcome and r["key_masked"] not in chosen:
                take(r, f"outcome {outcome}")
    strata: dict[str, list[Row]] = defaultdict(list)
    for r in pool:
        strata[f"{r['outcome']}/{primary[r['key_masked']] or '-'}"].append(r)
    while len(chosen) < limit and any(strata.values()):
        for name in sorted(strata):
            if strata[name] and len(chosen) < limit:
                r = strata[name].pop(0)
                if r["key_masked"] not in chosen:
                    take(r, f"stratum {name}")
    return [(r, chosen[r["key_masked"]]) for r in pool if r["key_masked"] in chosen]


def left_out(records: Sequence[Row], chosen: Mapping[str, str]) -> dict[str, list[str]]:
    """What the chosen keys left out, as it read every result and finding of the cycle."""
    picked = [(r, chosen[r["key_masked"]]) for r in records if r["key_masked"] in chosen]
    seen = {why.removeprefix(_ROOT_CAUSE) for _, why in picked if why.startswith(_ROOT_CAUSE)}
    seen |= {primary_root_cause(r) for r, _ in picked}
    elsewhere = {rc for r in records if r["key_masked"] not in chosen for rc in root_causes_of(r)}
    return {
        "root_causes": [rc for rc in root_cause_order(records) if rc not in seen and rc in elsewhere],
        "outcomes": sorted({r["outcome"] for r in records} - {r["outcome"] for r, _ in picked}),
    }


def scored(rows: Sequence[Mapping[str, str]], keys: Mapping[str, str], records: Sequence[Row]) -> dict:
    """The score's counts and reasons, as it read every result and finding of the cycle."""
    _check_rows(rows, keys, {r["key_masked"]: (r["outcome"], r["verdict"]) for r in records})
    confusion, per_outcome, disagreements, adjudicated = _tally(rows)
    reasons = []
    if len(adjudicated) < EXIT_MINIMUM:
        reasons.append(f"{len(adjudicated)} key(s) adjudicated; the exit needs at least {EXIT_MINIMUM}")
    if disagreements:
        reasons.append(f"{len(disagreements)} disagreement(s) between reviewers and the engine")
    population = Counter(r["outcome"] for r in records)
    reasons += [f"no adjudicated key has engine outcome {o} ({n} in the run)"
                for o, n in sorted(population.items()) if o not in per_outcome]  # fmt: skip
    return {
        "adjudicated": len(adjudicated), "confusion": confusion, "per_outcome": per_outcome,
        "reasons": reasons,
        "root_causes_not_adjudicated": left_out(records, {k: keys[k] for k in adjudicated})["root_causes"],
    }  # fmt: skip


def reviewed(sheet: str, skip: Sequence[str] = ()) -> bytes:
    """The sheet with each engine verdict agreed, but on the keys of the outcomes ``skip`` (left blank) and
    every seventh row (MATCH)."""
    header, rows = parse_csv(sheet.encode())
    for n, row in enumerate(rows):
        agreed = "MATCH" if n % 7 == 0 else row["engine_verdict"]
        row["reviewer_verdict"] = "" if row["engine_outcome"] in skip else agreed
    return render_csv(header, rows).encode()


@pytest.mark.parametrize("prefetch", [0, 1, 2])
async def test_the_stored_draw_and_score_equal_those_read_from_every_row(
    ctx: Context, cycle: str, prefetch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await everything(ctx, cycle)
    assert len(records) == N and sum(map(len, (r["findings"] for r in records))) > N // 2
    monkeypatch.setattr(adjudicate, "_PREFETCH", prefetch)
    refills = Counter[str]()
    holders = repo.adjudication_holders

    async def counted(*args: Any, **kwargs: Any) -> list[Row]:
        refills[args[3]] += 1
        return await holders(*args, **kwargs)

    monkeypatch.setattr(repo, "adjudication_holders", counted)
    for limit in (1, 7, 50, 120, 500):
        drawn = await store_draw(ctx, cycle, 1, limit=limit, who=TESTER)
        salt = drawn["salt"]
        want = reference(records, limit, salt)
        keys = {r["key_masked"]: why for r, why in want}
        assert drawn["selection"] == {"candidates": N, "keys": keys, "unrepresented": left_out(records, keys)}
        assert drawn["sheet"] == render_csv(SHEET_COLUMNS, [_sheet_row(r, why) for r, why in want])
        sheet = reviewed(drawn["sheet"], skip=("DRIFT",))
        score = (await store_score(ctx, drawn["adjudication_id"], sheet, who=TESTER))["score"]
        rows = _read_sheet(sheet, "the reviewed sheet")
        expected = scored(rows, keys, records)
        assert {name: score[name] for name in expected if name != "reasons"} == {
            name: value for name, value in expected.items() if name != "reasons"
        }
        assert score["exit"]["reasons"] == expected["reasons"]
    if prefetch == 0:  # each root cause that is no key's primary one was read by itself once reached
        assert set(refills) == {cause(n) for n in (7, 8, 9, 10, 11, 20)}


async def test_a_draw_and_a_score_read_fewer_rows_than_the_cycle_holds_findings(
    ctx: Context, cycle: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = sum(len(fs) for _, fs in synthetic(N).values())
    returned = Counter[str]()
    phase = "draw"
    rows_of = repo._rows

    async def counted(*args: Any, **kwargs: Any) -> list[Row]:
        rows = await rows_of(*args, **kwargs)
        returned[phase] += len(rows)
        return rows

    monkeypatch.setattr(repo, "_rows", counted)
    drawn = await store_draw(ctx, cycle, 1, limit=50, who=TESTER)
    phase = "score"
    await store_score(ctx, drawn["adjudication_id"], reviewed(drawn["sheet"]), who=TESTER)
    assert findings > 1_500 and returned["draw"] < findings // 3 and returned["score"] < findings // 3
