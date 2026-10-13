"""A cycle's end and its reports over many keys: the end counts a page of results at a time and stores the
counters of the whole cycle, and each report reads the rows it prints, never every result and finding."""

from __future__ import annotations

import tracemalloc
from collections import defaultdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth.context import Context
from deltasleuth.core.types import (
    CompiledSpec,
    Confirmation,
    DriftReason,
    Outcome,
    Severity,
)
from deltasleuth.reports import pdf
from deltasleuth.reports.csv import KINDS, parse_csv, read_csv, write_csv
from deltasleuth.reports.data import is_discrepant, load_run
from deltasleuth.runner.counters import count_cycle
from deltasleuth.runner.cycle import summarise
from deltasleuth.runner.recheck import RecheckPolicy
from deltasleuth.security.tokenise import mask_key
from deltasleuth.store import repo
from tests.conftest import TESTER, create_run

from .conftest import SECRET_NAME, SPEC_YAML, compare_key, doc, unit_spec

pytestmark = pytest.mark.db
BUG = {"id": "KD-UNIT-2", "when": "path == '$.status' && a == 'X' && b == 'Y'", "classification": "bug",
       "reason": "a known bug of the new side", "ticket": "UNIT-2"}  # fmt: skip
RECHECKED = {"confirmation": Confirmation.RECHECKED}
# key -> (side a, side b) and what its stored result is made to say: every outcome, a rule of each kind,
# a normalisation-only key, schema drift, a live discrepancy owed its recheck.
SAMPLES: dict[str, tuple[tuple[Any, Any], dict[str, Any]]] = {
    "K001": ((doc("K001"), doc("K001")), {}),
    "K002": ((doc("K002"), doc("K002", name=SECRET_NAME + " ")), {}),  # NORMALIZATION_ONLY
    "K003": ((doc("K003", status="OLD"), doc("K003", status="NEW")), {}),  # known diff, accepted
    "K004": ((doc("K004", status="X"), doc("K004", status="Y")), RECHECKED),  # known diff, a bug
    "K005": ((doc("K005", status="A"), doc("K005", status="B")), {}),  # owed a recheck
    "K006": ((doc("K006", status="A"), doc("K006", tags=("x",), status="B")), RECHECKED),
    "K007": ((doc("K007"), "timeout"), {}),
    "K008": (((404, {}), doc("K008")), RECHECKED),
    "K009": ((doc("K009"), (404, {})), RECHECKED),
    "K010": ((doc("K010"), doc("K010")), {"outcome": Outcome.DRIFT, "drift_reason": DriftReason.B_CONVERGED}),
    "K011": ((doc("K011"), doc("K011")), {"outcome": Outcome.NOISE_ONLY}),
    "K012": (
        (doc("K012"), doc("K012")),
        {"outcome": Outcome.INCONCLUSIVE, "inconclusive_reason": "schema_drift"},
    ),
}
CANCELLED = ("K013", "K014")  # never read
RETRIED = "K015"  # a sample's member sent back to its first pass: two reads a side at each pass
MATCHED = 7  # the rows a table scan found identical
KEYS = 20_000
# What ``summarise`` may hold of KEYS keys with one suppressed finding each (tracemalloc's peak). A page of
# a thousand results with their findings peaks at 4.3 MB; every result and finding in lists, as the cycle's
# end read them before, peaked at 53.5 MB.
CEILING = 16 * 2**20


def _spec() -> CompiledSpec:
    return unit_spec(knownDiffs=[*yaml.safe_load(SPEC_YAML)["knownDiffs"], BUG])


async def _cycle(ctx: Context, spec: CompiledSpec, *, retried: bool = False) -> str:
    """A running soak's first cycle over SAMPLES and CANCELLED, read live, its scan having matched MATCHED
    rows, then stopped: every key is cancelled, those read keeping their results; not yet summarised.
    ``retried`` adds RETRIED: read, its recheck failed on side b, read again from its first pass (as
    ``repo.schedule_retries`` sends a sample's member back) and confirmed by that attempt's recheck."""
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, spec, created_by=TESTER.actor)
        named = (*SAMPLES, *CANCELLED, *([RETRIED] if retried else []))
        keys = {key: mask_key(key, spec, ctx.tokeniser) for key in named}
        params = {"capabilities": ctx.declared(spec)}
        run_id, _ = await create_run(ctx, conn, spec_id, "soak", keys, params, {"matched": MATCHED})
        for n, (key, (pair, changes)) in enumerate(SAMPLES.items()):
            await compare_key(ctx, conn, spec, run_id, key, pair, n=n, **changes)
        if retried:
            a, b = doc(RETRIED, status="A"), doc(RETRIED, status="B")
            attempts = [((a, b), 1, {}), ((a, "timeout"), 2, {}), ((a, b), 1, {}), ((a, b), 2, RECHECKED)]
            for n, (pair, pass_no, changes) in enumerate(attempts, 20):
                await compare_key(ctx, conn, spec, run_id, RETRIED, pair, n=n, pass_no=pass_no, **changes)
        await repo.set_run_status(conn, run_id, "running")
        await repo.cancel_keys(conn, run_id)
    return run_id


async def _whole(conn: repo.Conn, run_id: str, cycle: int = 1) -> tuple[list[repo.Row], dict[str, list]]:
    """Every result and finding of the cycle in lists, as its end read them before."""
    findings: dict[str, list[repo.Row]] = defaultdict(list)
    for f in await repo.pages(repo.list_findings, conn, run_id, cycle):
        findings[f["key_masked"]].append(f)
    return await repo.pages(repo.list_results, conn, run_id, cycle), findings


async def test_a_cycle_s_end_stores_the_counters_of_the_whole_cycle_whatever_its_pages(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counters a paged end stores are ``count_cycle``'s over every row, member for member.
    Once the run holds a later cycle's counters, the earlier cycle is counted again from its rows, a page
    at a time too, and gives them again."""
    spec = _spec()
    run_id = await _cycle(ctx, spec)
    walks, paged = [], partial(repo.result_pages, size=5)  # twelve results: 5, 5, 2

    def walked(conn: repo.Conn, run: str, cycle: int) -> Any:
        walks.append(cycle)
        return paged(conn, run, cycle)

    monkeypatch.setattr(repo, "result_pages", walked)
    async with ctx.pool.connection() as conn:
        counters = await summarise(ctx, conn, spec, await repo.get_run(conn, run_id))
        results, findings = await _whole(conn, run_id)
        stored = (await repo.get_run(conn, run_id))["counters"]
        # The second cycle, ended too: one key read, the others cancelled. A run keeps its latest counters.
        await repo.end_cycle(conn, run_id)
        await repo.start_next_cycle(conn, run_id, datetime.now(UTC), scan={"matched": 0})
        await compare_key(ctx, conn, spec, run_id, "K001", SAMPLES["K001"][0], n=50, cycle=2)
        await repo.cancel_keys(conn, run_id)
        await summarise(ctx, conn, spec, await repo.get_run(conn, run_id))
        assert (await repo.get_run(conn, run_id))["counters"]["cycle"] == 2 and walks == [1, 2]
    due = RecheckPolicy.from_spec(spec.raw).outcomes
    whole = count_cycle(
        results, findings, total=len(SAMPLES) + len(CANCELLED) + MATCHED, live=True, recheck_outcomes=due,
        bulk_match=MATCHED,
    )  # fmt: skip
    assert counters == whole and stored == whole | {"cycle": 1}
    assert all(whole["by_outcome"].values())  # every outcome
    assert (whole["total"], whole["match"], whole["different"], whole["outstanding"]) == (21, 11, 4, 1)
    assert (whole["cancelled"], whole["schema_drift"], whole["normalization_only_keys"]) == (2, 1, 1)
    assert whole["suppressed_by_rule"] == {"KD-UNIT-1": 1, "KD-UNIT-2": 1}
    assert whole["by_confirmation"]["bulk_exhaustive"] == MATCHED
    # The discrepant keys' findings, and only theirs, are linked to the cycle's root causes.
    linked = {key for key, fs in findings.items() if any(f["root_cause_id"] for f in fs)}
    assert linked == {mask_key(key, spec, ctx.tokeniser) for key in ("K004", "K005", "K006", "K008", "K009")}
    earlier = await load_run(ctx, run_id, 1, results=False)  # not stored any more: counted again, paged
    assert walks == [1, 2, 1] and earlier.counters == whole | {"cycle": 1} and earlier.results == []


async def test_a_cycle_s_end_holds_a_page_of_its_results_never_the_cycle(ctx: Context) -> None:
    """KEYS keys with one suppressed finding each, under CEILING."""
    spec = unit_spec()
    keys = {f"K{n:05d}": f"K{n:05d}" for n in range(KEYS)}
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, spec, created_by=TESTER.actor)
        run_id, _ = await create_run(ctx, conn, spec_id, "batch", keys, {"capabilities": ctx.declared(spec)})
        # Each key's result and finding as ``repo.save_key_results`` writes them. A table is analysed before
        # the rows that refer to it arrive: without statistics each foreign key check reads the run's rows.
        await conn.execute("ANALYZE ds.run_keys")
        await conn.execute(
            """INSERT INTO ds.results (run_id, cycle, key_masked, outcome, side_a_state, side_b_state,
                   confirmation, first_outcome, suppressed_count)
               SELECT run_id, cycle, key_masked, 'KNOWN_DIFF', 'COMPLETE', 'COMPLETE', 'provisional',
                   'KNOWN_DIFF', 1
               FROM ds.run_keys WHERE run_id = %s""",
            (run_id,),
        )
        await conn.execute("ANALYZE ds.results")
        await conn.execute(
            """INSERT INTO ds.findings (run_id, cycle, key_masked, seq, finding_id, taxonomy_code, severity,
                   change_type, direction, path_pattern, json_pointer, value_a, value_b, rule_id,
                   rule_classification, suppressed)
               SELECT run_id, cycle, key_masked, 0, 'F-' || substr(md5(key_masked), 1, 16), 'VALUE_MISMATCH',
                   'HIGH', 'value_changed', 'both', '$.status', '/status', '"OLD"', '"NEW"', 'KD-UNIT-1',
                   'accepted', true
               FROM ds.run_keys WHERE run_id = %s""",
            (run_id,),
        )
        await conn.execute("ANALYZE ds.findings")  # ``finish_cycle`` analyses a new store's tables first
        run = await repo.get_run(conn, run_id)
        tracemalloc.start()
        try:
            counters = await summarise(ctx, conn, spec, run)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert (counters["total"], counters["match"], counters["suppressed"]) == (KEYS, KEYS, KEYS)
    assert counters["suppressed_by_rule"] == {"KD-UNIT-1": KEYS}
    assert peak < CEILING, f"the cycle's end held {peak / 2**20:.1f} MB"


async def test_reports_read_a_page_of_results_at_a_time_and_only_the_rows_they_print(
    ctx: Context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every export is the same whatever its pages, read without every finding or observation
    of the cycle in one list; the PDF's top findings are the first of the discrepant ones, counted. The
    re-read a finding's row cites is its key's standing one, whichever keys share its page: a retried key
    has an abandoned attempt's reads at the same pass."""
    spec = _spec()
    run_id = await _cycle(ctx, spec, retried=True)
    async with ctx.pool.connection() as conn:
        await summarise(ctx, conn, spec, await repo.get_run(conn, run_id))
        reads = await conn.execute(
            "SELECT pass_no, count(*) FROM ds.observations WHERE run_id = %s AND key_masked = %s GROUP BY 1",
            (run_id, mask_key(RETRIED, spec, ctx.tokeniser)),
        )
        assert sorted(await reads.fetchall()) == [(1, 4), (2, 4)]  # both attempts' reads are kept
    data = await load_run(ctx, run_id)  # every result with its findings, as a P0 report reads them
    discrepant = [
        f
        for severity in Severity
        for r in data.results
        for f in r["findings"]
        if f["severity"] == severity.value and is_discrepant(f)
    ]
    assert len({f["severity"] for f in discrepant}) > 1 and len(discrepant) > 2
    in_one_page = {kind: await write_csv(ctx, run_id, None, kind) for kind in KINDS}

    async def whole_cycle(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("read the whole cycle")

    pages = repo.result_pages
    monkeypatch.setattr(repo, "list_findings", whole_cycle)
    monkeypatch.setattr(repo, "list_observations", whole_cycle)
    monkeypatch.setattr(repo, "result_pages", partial(pages, size=5))
    monkeypatch.setattr(pdf, "TOP_FINDINGS", 2)
    csvs = {}
    for kind in KINDS:
        (tmp_path / kind).write_bytes(await write_csv(ctx, run_id, None, kind))
        assert (tmp_path / kind).read_bytes() == in_one_page[kind]
        csvs[kind] = read_csv(tmp_path / kind)[1]
    masked = {key: mask_key(key, spec, ctx.tokeniser) for key in (*SAMPLES, *CANCELLED, RETRIED)}
    assert [(r["key"], r["severity"], r["verdict"], r["rule_id"]) for r in csvs["discrepancies"]] == [
        (masked["K004"], "HIGH", "DIFFERENT", "KD-UNIT-2"),
        (masked["K005"], "HIGH", "DIFFERENT", ""),
        (masked["K006"], "HIGH", "DIFFERENT", ""),
        (masked["K006"], "MEDIUM", "DIFFERENT", ""),
        (masked["K008"], "CRITICAL", "DIFFERENT", ""),
        (masked["K009"], "CRITICAL", "DIFFERENT", ""),
        (masked[RETRIED], "HIGH", "DIFFERENT", ""),
    ]
    # RETRIED's result is its second attempt's recheck: the re-read cited is that read, of each side,
    # with pages of 5 and of 1000 alike (the bytes above), never the abandoned attempt's timeout.
    again = csvs["discrepancies"][-1]
    assert (again["recheck_obs_a_id"], again["recheck_obs_b_id"]) == (again["obs_a_id"], again["obs_b_id"])
    assert again["confirmation"] == "rechecked" and again["obs_a_id"] and again["obs_b_id"]
    cited = {(r["key"], r["recheck_obs_a_id"], r["recheck_obs_b_id"]) for r in csvs["discrepancies"]}
    for size in (1, 3, len(SAMPLES), len(SAMPLES) + 1):  # alone on its page, and with other keys
        monkeypatch.setattr(repo, "result_pages", partial(pages, size=size))
        rows = parse_csv(await write_csv(ctx, run_id, None, "discrepancies"))[1]
        assert {(r["key"], r["recheck_obs_a_id"], r["recheck_obs_b_id"]) for r in rows} == cited, size
    monkeypatch.setattr(repo, "result_pages", partial(pages, size=5))
    assert [(r["key"], r["rule_id"]) for r in csvs["suppressed"]] == [
        (masked["K003"], "KD-UNIT-1"),
        (masked["K004"], "KD-UNIT-2"),
    ]
    assert [r["key"] for r in csvs["normalization"]] == [masked["K002"]]
    assert [(r["key"], r["outcome"], r["reason"], r["attempts_b"]) for r in csvs["retry"]] == [
        (masked["K005"], "MISMATCH", "recheck_owed", "1"),
        (masked["K007"], "INCONCLUSIVE", "timeout", "3"),
        (masked["K010"], "DRIFT", "b_converged", "1"),
        (masked["K012"], "INCONCLUSIVE", "schema_drift", "1"),
        (masked["K013"], "CANCELLED", "cancelled", ""),
        (masked["K014"], "CANCELLED", "cancelled", ""),
    ]
    bugs = {r["sample_keys"]: r["bug_rule_id"] for r in csvs["root_causes"]}
    assert bugs[masked["K004"]] == "KD-UNIT-2" and set(bugs.values()) == {"KD-UNIT-2", ""}
    top = (await pdf.report_document(ctx, run_id)).sections[-2]
    assert top.lines == [f"2 of {len(discrepant)} discrepant findings (masked)."]
    assert [(row[0], row[1], row[4]) for row in top.rows] == [
        (f["key_masked"], f["severity"], f["json_pointer"]) for f in discrepant[:2]
    ]
