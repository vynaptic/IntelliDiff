"""Readiness's performance targets (docs/mvp/READINESS.md §14), held with ``DS_PERF=1`` only: measuring a
cycle of a million results; over 500 pairs of 4,400 cycles each whose sign-offs lie 4,000 cycles back,
the evaluation every surface shares, a pair's trend by day, ``trends.csv`` at its row limit and a
``dashboard.pdf`` volume of 100 pairs; and a brief's two reads over cycles of 20,000 root-cause groups."""

from __future__ import annotations

import os
import time
import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest

from deltasleuth import approvals
from deltasleuth import readiness as judging
from deltasleuth.core import readiness
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.reports import readiness as reports
from deltasleuth.reports.render import render_pdf
from deltasleuth.store import repo
from tests.unit.store.four_eyes import approved, person, upload

from .conftest import TARGETS, OpenHarness
from .measuring import MATCH, PAIR, SCAN, SCANNED, SOAK, ended, measure, measures, scanned, signed, started

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get("DS_PERF"), reason="DS_PERF=1 runs it")]
NOWHERE = TARGETS.format(base="http://127.0.0.1:9", rate=500)  # the targets: no run here reads them
PAIRS, CYCLES, SIGNED = 500, 4_400, 400  # each pair's sign-off is of cycle 400: 4,000 cycles back
LISTED = 22  # the pairs of the programme whose trends.csv holds 96,800 rows, just under its 100,000
RESULTS, DISCREPANT = 1_000_000, 10_000
SIGNERS = (("olga", "operator"), ("rita", "rule_approver"), ("ruth", "rule_approver"))
KEPT, GROUPS, MOVED = 130, 20_000, 100  # cycles of so many root-cause groups each, so many new in each
COUNTED = {
    "total": RESULTS, "compared": RESULTS, "match": RESULTS - DISCREPANT, "different": DISCREPANT,
    "indeterminate": 0, "cancelled": 0, "outstanding": 0, "absent_both": 0, "api_uncomparable": 0,
    "suppressed": 0,
}  # fmt: skip


def name(pair: int) -> str:
    return f"pair-{pair:04d}"


async def test_perf_measuring_a_cycle_of_a_million_results(open_harness: OpenHarness) -> None:
    """A cycle of 1,000,000 results, 10,000 of them discrepant with a CRITICAL finding each, after a cycle
    of the same size whose discrepant keys half overlap: measured in under 2 s (so that the 10 full
    measures of a backfill batch fit its 10 s)."""
    async with open_harness(NOWHERE, readiness={}) as h:
        run_id = await scanned(h)
        for cycle, first in ((1, 1), (2, DISCREPANT // 2 + 1)):  # the keys first .. first + 9,999 differ
            async with h.ctx.pool.connection() as conn:
                await conn.execute("SET LOCAL session_replication_role = replica")  # no foreign-key checks
                await conn.execute(
                    """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
                           confirmation, first_outcome)
                       SELECT %(run)s, %(cycle)s, 'm' || g, o.outcome, 'COMPLETE', 'COMPLETE', 'rechecked',
                              o.outcome
                       FROM generate_series(1, %(results)s) g,
                            LATERAL (SELECT CASE WHEN g >= %(first)s AND g < %(first)s + %(discrepant)s
                                                 THEN 'MISMATCH' ELSE 'MATCH' END AS outcome) o""",
                    {"run": run_id, "cycle": cycle, "results": RESULTS, "first": first,
                     "discrepant": DISCREPANT},
                )  # fmt: skip
                await conn.execute(
                    """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, severity,
                           change_type, direction, path_pattern, json_pointer, suppressed)
                       SELECT %(run)s, %(cycle)s, 'm' || g, 0,
                              'F-' || substr(md5(%(run)s || %(cycle)s || '/' || g), 1, 16), 'CRITICAL',
                              'value_changed', 'both', '$.x', '/x', false
                       FROM generate_series(%(first)s, %(first)s + %(discrepant)s - 1) g""",
                    {"run": run_id, "cycle": cycle, "first": first, "discrepant": DISCREPANT},
                )
            await ended(h, run_id, MATCH, counters=COUNTED)
            async with h.ctx.pool.connection() as conn:
                await repo.analyse_new(conn)  # as the cycle's end does
        began = time.perf_counter()
        await measure(h)  # both cycles, and the first evaluation
        took = time.perf_counter() - began
        record = await measures(h, run_id, 2)
    assert record is not None and record["results"] == RESULTS + len(MATCH)
    assert record["unexplained_critical"] == DISCREPANT
    assert record["changes"] == {"new": 5_000, "persistent": 5_000, "fixed": 5_000, "other": 0}
    assert took / 2 < 2, f"{took / 2:.2f} s a cycle"


async def test_perf_the_evaluation_and_the_reports_of_500_pairs(open_harness: OpenHarness) -> None:
    """500 pairs of 4,400 hourly cycles each, K = 50, each pair's approved sign-off 4,000 cycles back and
    still in its streak: the evaluation's p95 is under 1 s, a pair's trend by day under 300 ms, a
    ``trends.csv`` of 96,800 rows under 5 s and 200 MB, and a ``dashboard.pdf`` volume of 100 pairs under
    10 s."""
    listed = [{"spec": name(pair)} for pair in range(1, LISTED + 1)]
    settings = {
        "gates": {"convergence": {"cycles": 50}}, "programmes": [{"name": "perf", "pairs": listed}],
    }  # fmt: skip
    first = datetime.now(UTC) - timedelta(hours=CYCLES + 1)
    async with open_harness(NOWHERE, readiness=settings) as h:
        ctx = h.ctx
        version = compile_spec(parse_yaml_text(SOAK))
        async with ctx.pool.connection() as conn:  # a run under an approved spec, started under SSO
            amy, bob = await person(conn, "amy", "spec_author"), await person(conn, "bob")
            olga, rita, ruth = [await person(conn, n, r) for n, r in SIGNERS]
            spec_id = await upload(conn, version, amy)
            approval_id = await approved(conn, spec_id, amy, bob, scopes=approvals.needs(version))
        sso = {"needs": approvals.needs(version), "mode": "oidc"}
        run_id = await started(h, spec_id, scan=SCANNED, **SCAN, approval=sso)
        async with ctx.pool.connection() as conn:
            await repo.set_run_approval(conn, run_id, approval_id)
        await ended(h, run_id, MATCH, at=first, next_cycle=False)
        await measure(h)
        async with ctx.pool.connection() as conn:
            signoff = await signed(conn, run_id, 1, version, olga, rita, ruth)
        told = {
            "spec": spec_id, "run": run_id, "signoff": signoff, "pairs": PAIRS, "cycles": CYCLES,
            "signed": SIGNED, "first": first, "base": 1_000,
        }  # fmt: skip
        async with ctx.pool.connection() as conn:  # each pair: the measured cycle's spec, run and sign-off
            await conn.execute("SET LOCAL session_replication_role = replica")  # no foreign-key checks
            await conn.execute(
                """INSERT INTO ds.specs SELECT (jsonb_populate_record(s, jsonb_build_object(
                       'spec_id', %(base)s + p, 'name', 'pair-' || lpad(p::text, 4, '0'),
                       'content_hash', md5(p::text) || md5('x' || p)))).*
                   FROM ds.specs s, generate_series(1, %(pairs)s) p WHERE s.spec_id = %(spec)s""",
                told,
            )
            await conn.execute(
                """INSERT INTO ds.runs SELECT (jsonb_populate_record(r, jsonb_build_object(
                       'run_id', 'R-' || lpad(p::text, 26, '0'), 'spec_id', %(base)s + p))).*
                   FROM ds.runs r, generate_series(1, %(pairs)s) p WHERE r.run_id = %(run)s""",
                told,
            )
            await conn.execute(
                """INSERT INTO ds.cycles SELECT (jsonb_populate_record(c, jsonb_build_object(
                       'run_id', 'R-' || lpad(p::text, 26, '0'), 'cycle', g,
                       'ended_at', %(first)s::timestamptz + g * interval '1 hour',
                       'readiness', c.readiness || jsonb_build_object(
                           'pair', 'pair-' || lpad(p::text, 4, '0'), 'spec_id', %(base)s + p)))).*
                   FROM ds.cycles c, generate_series(1, %(pairs)s) p, generate_series(1, %(cycles)s) g
                   WHERE (c.run_id, c.cycle) = (%(run)s, 1)""",
                told,
            )
            await conn.execute(
                """INSERT INTO ds.approvals SELECT (jsonb_populate_record(a, jsonb_build_object(
                       'approval_id', 'A-' || lpad(p::text, 26, '0'), 'spec_id', %(base)s + p,
                       'run_id', 'R-' || lpad(p::text, 26, '0'), 'cycle', %(signed)s))).*
                   FROM ds.approvals a, generate_series(1, %(pairs)s) p WHERE a.approval_id = %(signoff)s""",
                told,
            )
        async with ctx.pool.connection() as conn:
            await conn.execute("ANALYZE ds.specs, ds.runs, ds.cycles, ds.approvals")  # autovacuum's, by then
            took = []
            for _ in range(20):
                began = time.perf_counter()
                found = await judging.judged(ctx, conn)
                took.append(time.perf_counter() - began)
        items = [found.items[name(pair)] for pair in range(1, PAIRS + 1)]
        assert len(found.items) == PAIRS + 1 and {item["state"] for item in items} == {"signed_off"}
        assert {item["signoff_id"] for item in items} == {f"A-{pair:026d}" for pair in range(1, PAIRS + 1)}
        assert {item["convergence"]["streak"] for item in items} == {150}  # the window: max(60, 3K)
        p95 = sorted(took)[int(len(took) * 0.95) - 1]
        assert p95 < 1, f"evaluation p95 {p95:.2f} s"

        began = time.perf_counter()
        daily = await judging.cycles(ctx, name(1), bucket="day")
        took_daily = time.perf_counter() - began
        assert 183 <= len(daily["items"]) <= 185 and took_daily < 0.3, f"trend by day {took_daily:.2f} s"

        began = time.perf_counter()
        text = await reports.trends_csv(ctx, programme="perf", most=reports.TRENDS_ROWS)
        took_csv = time.perf_counter() - began
        assert text.count("\r\n") == 1 + LISTED * CYCLES and took_csv < 5, f"trends.csv {took_csv:.2f} s"
        del text
        tracemalloc.start()
        await reports.trends_csv(ctx, programme="perf", most=reports.TRENDS_ROWS)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert peak < 200 * 2**20, f"trends.csv peaked at {peak / 2**20:.0f} MB"

        began = time.perf_counter()
        pdf = render_pdf(await reports.dashboard_document(ctx, 1))
        took_pdf = time.perf_counter() - began
        assert pdf.startswith(b"%PDF-") and took_pdf < 10, f"dashboard.pdf {took_pdf:.2f} s"
    assert PAIR not in {name(pair) for pair in range(1, PAIRS + 1)}  # the measured cycle the others copy


async def test_perf_a_brief_s_reads(open_harness: OpenHarness) -> None:
    """A pair of 130 cycles of 20,000 root-cause groups each (2.6M rows), each cycle holding 100 groups the
    one before did not: a brief's two reads beyond the evaluation, its window's points with their groups
    counted and the groups of its 30 cycles, take under 1 s together."""
    first = datetime.now(UTC) - timedelta(hours=KEPT + 1)
    told = {"cycles": KEPT, "groups": GROUPS, "moved": MOVED, "first": first}
    async with open_harness(NOWHERE, readiness={}) as h:
        run_id = await scanned(h)
        await ended(h, run_id, MATCH, at=first, next_cycle=False)
        await measure(h)
        async with h.ctx.pool.connection() as conn:  # each cycle: the measured one's, an hour later
            await conn.execute(
                """INSERT INTO ds.cycles SELECT (jsonb_populate_record(c, jsonb_build_object(
                       'cycle', g, 'ended_at', %(first)s::timestamptz + g * interval '1 hour'))).*
                   FROM ds.cycles c, generate_series(2, %(cycles)s) g
                   WHERE (c.run_id, c.cycle) = (%(run)s, 1)""",
                told | {"run": run_id},
            )
            await conn.execute(
                """INSERT INTO ds.root_causes (run_id, cycle, seq, root_cause_id, kind, max_severity,
                       path_pattern, change_type, direction, affected_keys, finding_count)
                   SELECT %(run)s, g, n - 1, 'RC-' || lpad(to_hex(g * %(moved)s + n), 12, '0'), 'signature',
                          'HIGH', '$.x', 'value_changed', 'both', 1, 1
                   FROM generate_series(1, %(cycles)s) g, generate_series(1, %(groups)s) n""",
                told | {"run": run_id},
            )
        window = readiness.BRIEF_CYCLES
        async with h.ctx.pool.connection() as conn:
            await conn.execute("ANALYZE ds.cycles, ds.root_causes")  # autovacuum's, by then
            await judging.judged(h.ctx, conn, only=PAIR)  # the evaluation, which the reads follow
            began = time.perf_counter()
            rows = await repo.brief_points(conn, PAIR, None, window + 1)
            found = await repo.brief_causes(conn, rows[window - 1 :: -1], judging.BRIEF_CAUSES)
            took = time.perf_counter() - began
        brief = await judging.brief(h.ctx, PAIR)
    total = GROUPS + (window - 1) * MOVED
    assert [(row["cycle"], row["root_causes"]) for row in rows] == [
        (cycle, GROUPS) for cycle in range(KEPT, KEPT - window - 1, -1)
    ]
    assert (found["total"], found["opened"], found["still_open"]) == (total, MOVED, GROUPS - MOVED)
    assert len(found["items"]) == judging.BRIEF_CAUSES and len(brief["points"]) == window
    assert brief["causes"]["text"].endswith(
        f"{MOVED:,} opened in the newest, {GROUPS - MOVED:,} still open, {total - GROUPS:,} closed (not"
        f" observed in it); the first {judging.BRIEF_CAUSES:,} of {total:,} are listed"
    )
    assert took < 1, f"a brief's reads {took:.2f} s"
