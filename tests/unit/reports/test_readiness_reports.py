"""Readiness in a run's reports (docs/mvp/READINESS.md §11): the run report's "Pair readiness (observed)"
section in each variant, judged as of the reported cycle so that the database gives it again; a run's
``readiness.csv`` among ``FORMATS``; the CSVs' escaping; what none of them ever tells (§2); an evidence
pack that holds nothing of readiness, whatever was measured and recorded since; and a pair's brief, one
page whatever it holds."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from deltasleuth.auth.principal import WORKER
from deltasleuth.config import ConvergenceSettings, GateSettings, ReadinessSettings
from deltasleuth.context import Context
from deltasleuth.core import readiness as gates
from deltasleuth.core.types import Outcome
from deltasleuth.reports import FORMATS, pack
from deltasleuth.reports import readiness as reports
from deltasleuth.reports.csv import parse_csv
from deltasleuth.reports.pdf import pdf_report, report_document
from deltasleuth.reports.render import Section, render_pdf
from deltasleuth.runner import readiness as watching
from deltasleuth.store import repo
from tests.unit.api.test_readiness_api import BANNED
from tests.unit.runner.conftest import Harness
from tests.unit.runner.measuring import KEYS, MATCH, MISMATCH, PAIR, ended, scanned, started

from .conftest import doc, seed_run
from .packs import files_of, samples, signed_cycle

pytestmark = pytest.mark.db
TITLE = "Pair readiness (observed)"
FAILING = MATCH | {"K001": MISMATCH, "K002": MISMATCH}  # 2 of 1,000 keys: over the 1 that 0.1% allows


async def _measure(ctx: Context) -> None:
    await watching.watch(ctx, watching.State())


async def _section(ctx: Context, run_id: str, cycle: int) -> Section:
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        assert run is not None
        return await reports.run_section(ctx, conn, run, cycle)


def _strings(section: Section) -> list[str]:
    return [section.title, *section.lines, *section.header, *(str(c) for row in section.rows for c in row)]


async def test_the_run_section_s_variants(ctx: Context) -> None:
    h = Harness(ctx)
    captured = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))})
    once = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}, mode="one_shot", live=True)
    listed = await started(h)  # a soak over a key list given at creation, no census of side a
    assert [(await _section(ctx, run_id, 1)).lines for run_id in (captured, once, listed)] == [
        ["This run is not assessed for readiness: captured samples."],
        ["This run is not assessed for readiness: one-shot."],
        ["Readiness is judged when the cycle ends."],
    ]
    await ended(h, listed, MATCH)
    assert (await _section(ctx, listed, 1)).lines == ["This cycle is not measured yet."]
    assert (await _section(ctx, listed, 2)).lines == ["Readiness is judged when the cycle ends."]
    await _measure(ctx)
    # In the run report it follows the trend; the counts of a key list describe those keys only.
    report = await report_document(ctx, listed, cycle=1)
    titles = [s.title for s in report.sections]
    assert titles[titles.index("Trend against the previous run") + 1] == TITLE
    section = report.sections[titles.index(TITLE)]
    sha12 = gates.config_sha256(None)[:12]
    assert section.lines[0] == gates.DEFINITION and section.lines[2:] == [
        f"Today's state: the Readiness screen (#/readiness/{PAIR})."
    ]
    assert section.lines[1].startswith(
        f"The parity gates of {PAIR} as of cycle 1 of run {listed} (ended "
    ) and section.lines[1].endswith(
        f" UTC), judged on the cycles ended by then under readiness settings {sha12}: the database gives them"
        " again under the same settings (architecture §10)."
    )
    assert section.header == ("Gate", "Required", "Status", "Observed")
    at, spec_id = f"cycle 1 of run {listed}", (await h.run(listed))["spec_id"]
    assert [(row[0], row[1], row[2]) for row in section.rows] == [
        ("population", "yes", "not met"), ("critical", "yes", "met"), ("targets", "yes", "met"),
        ("convergence", "yes", "not met"), ("fresh", "yes", "met"), ("graph", "no", "not applicable"),
    ]  # fmt: skip
    assert [section.rows[n][3] for n in (0, 3)] == [
        f"{at} is not a census: its keys are the spec's inline list and no side is a table to check them"
        " against: its counts describe those keys only",
        f"0 of the 3 evidence cycles in a row that 1.0.0 (spec {spec_id}) needs meet every per-cycle gate"
        f" ({at} failed population)",
    ]
    assert (await pdf_report(ctx, listed, 1)).startswith(b"%PDF-")
    assert await _section(ctx, listed, 1) == section


async def test_the_run_section_is_judged_as_of_its_cycle_and_given_again(ctx: Context) -> None:
    relaxed = ReadinessSettings(gates=GateSettings(convergence=ConvergenceSettings(cycles=2)))
    ctx = replace(ctx, settings=replace(ctx.settings, readiness=relaxed))
    h = Harness(ctx)
    run_id = await scanned(h)
    for _ in range(2):
        await ended(h, run_id, MATCH)
    await _measure(ctx)
    first, second = await _section(ctx, run_id, 1), await _section(ctx, run_id, 2)
    spec_id = (await h.run(run_id))["spec_id"]
    version, at = f"1.0.0 (spec {spec_id})", f"of run {run_id}"
    assert first.lines[2] == "Deviations: convergence 2 cycles (default 3; relaxed)."
    assert gates.config_sha256(relaxed)[:12] in first.lines[1] and len(first.lines) == 4
    assert [(row[0], row[2]) for row in first.rows] == [
        ("population", "met"), ("critical", "met"), ("targets", "met"), ("convergence", "not met"),
        ("fresh", "met"), ("graph", "not applicable"),
    ]  # fmt: skip
    # As of cycle 1, one of the two cycles: no schedule is told of a past cycle, and it is fresh as it ends.
    assert [row[3] for row in first.rows[2:5]] == [
        f"cycle 1 {at} has 0 discrepant or unresolved key(s) of 1,000 observed whole, within the 1 its pooled"
        " 0.1% target allows (per-stratum targets are the approver's); 0 key(s) suppressed (known differences"
        " or noise only)",
        f"1 of the 2 evidence cycles in a row that {version} needs meet every per-cycle gate",
        f"the evidence cycle (1 {at}) ended 0 hours ago, within the gate's 7 days",
    ]
    assert [(row[0], row[2]) for row in second.rows][3] == ("convergence", "met")
    assert second.rows[3][3] == (
        f"the newest 2 evidence cycles of {version} meet every per-cycle gate: at least the 2 the gate needs"
    )
    # Later cycles, one of them failing, change today's state and neither section.
    await ended(h, run_id, FAILING)
    await ended(h, run_id, MATCH)
    await _measure(ctx)
    assert (await _section(ctx, run_id, 1), await _section(ctx, run_id, 2)) == (first, second)
    third = await _section(ctx, run_id, 3)
    assert [(row[0], row[2]) for row in third.rows][2:4] == [
        ("targets", "not met"),
        ("convergence", "not met"),
    ]
    assert third.rows[3][3] == (
        f"0 of the 2 evidence cycles in a row that {version} needs meet every per-cycle gate (cycle 3 {at}"
        " failed targets)"
    )
    # What no surface tells (§2): no percentage but the targets gate's, nothing forecast, and no key.
    texts = [text for section in (first, second, third) for text in _strings(section)]
    header, rows = parse_csv(await FORMATS["readiness.csv"][1](ctx, run_id, None))
    texts += [*header, *(cell for row in rows for cell in row.values())]
    allowed = {gates.DEFINITION, *(text for text in texts if "pooled 0.1% target" in text)}
    assert [found.group(0) for text in texts if text not in allowed for found in BANNED.finditer(text)] == []
    assert not any("scheduled" in text or "soak" in text for text in texts)
    counted = [text for text in texts if "discrepant or unresolved key(s)" in text]
    assert len(counted) == 3 and all("suppressed" in text for text in counted)  # N6, and in readiness.csv
    assert "suppressed" in header and {row["suppressed"] for row in rows} == {"0"}
    assert not any(key in text for text in texts for key in KEYS)


async def test_a_run_s_readiness_csv_holds_its_pair_s_points_up_to_the_cycle(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(ctx)
    run_id = await scanned(h)
    for results in (MATCH, MATCH, FAILING):
        await ended(h, run_id, results)
    await _measure(ctx)
    media, render = FORMATS["readiness.csv"]
    assert media == "text/csv" and render is reports.run_csv

    async def points(cycle: int | None) -> list[tuple[str, ...]]:
        header, rows = parse_csv(await render(ctx, run_id, cycle))
        assert tuple(header) == reports.TRENDS
        assert {(row["pair"], row["run_id"], row["programme"]) for row in rows} <= {(PAIR, run_id, "")}
        return [(row["cycle"], row["different"], row["total"], row["basis"], row["fails"]) for row in rows]

    whole = [("1", "0", "1000", "census", ""), ("2", "0", "1000", "census", ""),
             ("3", "2", "1000", "census", "targets")]  # fmt: skip
    assert await points(None) == whole  # its fourth cycle is live: every point so far
    assert (await points(3), await points(2), await points(1)) == (whole, whole[:2], whole[:1])
    monkeypatch.setattr(reports, "CHUNK", 2)  # trends.csv reads a pair a page at a time: oldest first still
    paged = parse_csv((await reports.trends_csv(ctx)).encode())[1]
    assert [(row["pair"], row["cycle"], row["fails"]) for row in paged] == [
        (PAIR, "1", ""), (PAIR, "2", ""), (PAIR, "3", "targets"),
    ]  # fmt: skip
    monkeypatch.setattr(reports, "RUN_ROWS", 2)
    assert await points(None) == whole[1:]  # the newest, oldest first
    with pytest.raises(LookupError, match="run R-nowhere not found"):
        await render(ctx, "R-nowhere", None)
    # Its measures outlive its evidence: a purged cycle's row is still given, as readiness never answers 410.
    async with ctx.pool.connection() as conn:
        assert await repo.start_purge(conn, run_id, 1, "expired") is not None
    assert await points(1) == whole[1:1] + whole[:1]


async def test_the_csvs_escape_what_a_spreadsheet_would_run(ctx: Context) -> None:
    h = Harness(ctx)
    run_id = await scanned(h)
    await ended(h, run_id, MATCH)
    await _measure(ctx)
    hostile = '=HYPERLINK("http://x", "y"),1'  # what an owner of the database could plant in a measure
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.cycles SET readiness = readiness || %s WHERE run_id = %s",
            (Jsonb({"version": hostile, "basis_text": "+cmd|' /C calc'!A0, twice"}), run_id),
        )
    chosen = await reports.selection(ctx)
    table = reports.dashboard_csv(chosen)
    trends = await reports.trends_csv(ctx)
    mine = (await reports.run_csv(ctx, run_id)).decode()
    planted = '"\'=HYPERLINK(""http://x"", ""y""),1"'
    assert all(f",{planted}," in text for text in (table, trends, mine))  # quoted, behind an apostrophe
    for text in (table, trends, mine):
        rows = parse_csv(text.encode())[1]
        assert {row["version"] for row in rows} == {hostile}  # and read back as it was
    header, gated = parse_csv(table.encode())
    assert tuple(header) == reports.DASHBOARD and "suppressed" in header
    assert [(row["gate"], row["value"], row["suppressed"]) for row in gated][2] == ("targets", "0", "0")
    by = {row["gate"]: row["text"] for row in gated}
    assert by["population"] == f"cycle 1 of run {run_id} observed side a whole (+cmd|' /C calc'!A0, twice)"
    assert f"\"cycle 1 of run {run_id} observed side a whole (+cmd|' /C calc'!A0, twice)\"\r\n" in table


async def _recorded(ctx: Context) -> list[Any]:
    async with ctx.pool.connection() as conn:
        rows, _ = await repo.list_audit(conn, actor=WORKER.actor, limit=1000)
    return [row["action"] for row in rows if row["action"].startswith("readiness.")]


async def test_a_pack_is_the_same_whatever_readiness_measured_and_recorded(pack_ctx: Context) -> None:
    """D10: readiness never enters a pack. A pack issued after the worker measured the cycle, recorded its
    settings and the pair's state, and a readiness report was downloaded, holds byte for byte the facts,
    exports and audit rows of the one issued before; no file of either names readiness."""
    signed = await signed_cycle(pack_ctx, pairs=samples(52), adjudicate=True)
    olga = signed.people["olga"]
    before = await pack.issue(pack_ctx, signed.run_id, 1, who=olga)
    await _measure(pack_ctx)
    assert sorted(set(await _recorded(pack_ctx))) == [
        "readiness.changed", "readiness.config", "readiness.measured",
    ]  # fmt: skip
    async with repo.audited(pack_ctx.pool) as (_, audit):
        await audit(olga, "report.download", "readiness", {"name": "dashboard.pdf", "programme": None})
    after = await pack.issue(pack_ctx, signed.run_id, 1, who=olga)
    first = files_of((await pack.stored(pack_ctx, before["pack_id"]))[1])
    second = files_of((await pack.stored(pack_ctx, after["pack_id"]))[1])
    assert sorted(first) == sorted(second) and not any("readiness" in name for name in second)
    same = [name for name in first if name.startswith(("facts/", "csv/", "adjudication/", "annex/"))]
    assert len(same) >= 10 and all(first[name] == second[name] for name in same)
    # The later pack's segment is the earlier one's and the first pack's issue: no row of readiness.
    assert second["audit/chain.tsv"].startswith(first["audit/chain.tsv"])
    added = second["audit/chain.tsv"][len(first["audit/chain.tsv"]) :].decode().splitlines()
    assert len(added) == 1 and before["pack_id"] in added[0]
    assert not any(b"readiness" in data for name, data in second.items() if not name.endswith(".pdf"))


# -- a pair's brief on one page (§11) ---------------------------------------------------------------------

RUN = "R-" + "0123456789ABCDEFGHJKMNPQRS"
AT = f"cycle 14 of run {RUN}"
LONGEST = (  # the targets gate's text with each of its parts over, at nine-digit counts (§9, Y4)
    f"{AT} has 123,456,789 discrepant or unresolved key(s) of 987,654,321 observed whole; its pooled 0.01%"
    " target allows 98,765: 123,358,024 over; 123,456,789 key(s) suppressed (known differences or noise"
    f" only); {AT} has 123,456,789 key(s) absent on both sides, over the 98,765 its 0.01% target allows of"
    f" 987,654,321: the key source is stale; {AT} has 123,456,789 key(s) above the API cap that were not"
    " compared (truncated): declare pagination for that side's fetch in a new version"
)
SCHEDULED = (  # the convergence gate's, not met, with a live soak's schedule (§9, Y7)
    f"0 of the 50 evidence cycles in a row that 10.20.30 (spec 123456) needs meet every per-cycle gate ({AT}"
    f" failed targets); cycle 16 of run {RUN} is the earliest that can complete them: it is scheduled to"
    " start about 2026-10-07 14:00 UTC, cycles starting every 1h, and completes them when it ends complete"
    " (a schedule, not a forecast)"
)
STRATUM = (
    "stratum segment=private-banking-and-wealth-management (high, ≤ 0.01%): 123,456,789 drawn of"
    " 987,654,321, 123,456,789 compared, 123,456,789 discrepant, 123,456,789 unresolved, bound 0.0123%:"
    " failed"
)


def _longest() -> dict[str, Any]:
    """A brief at what it holds at most, as ``readiness.brief`` answers (the fields its PDF reads): the
    longest text on each of the twelve gates, eleven of them blocking; a sampled cycle of 15 strata that
    the gates do not judge; 30 points of nine-digit counts, each with both marks; 500 root-cause groups
    listed of 1,204; every note; a name of 63 characters."""
    ended, most = datetime(2026, 10, 3, 4, tzinfo=UTC), 123_456_789
    points = [
        {"run_id": RUN, "cycle": n, "ended_at": ended + timedelta(hours=n), "version": "10.20.30"}
        | {"spec_id": 123_456, "basis": "rate_sample", "complete": True, "sample_status": "failed"}
        | dict.fromkeys(("total", "compared", "different", "unexplained_critical", "root_causes"), most)
        | {"by_outcome": dict.fromkeys(map(str, Outcome), most), "mismatch_rate": None, "coverage": None}
        | {"marks": ["version", "keys"]}
        for n in range(1, 31)
    ]
    at, draw = {"run_id": RUN, "cycle": 14, "ended_at": ended}, "D-" + "0" * 26
    notes = [
        f"The gates judge {AT}; these figures are of cycle 30 of run {RUN}, the newest complete cycle.",
        f"Sampled (draw {draw}): judged per stratum, with no rate or bound for the strata pooled.",
        "These counts are of the keys compared, not of a population.",
    ]
    cycle = points[-1] | {"draw_id": draw, "claim": "observed", "strata": [STRATUM] * 15, "notes": notes}
    group = {"state": "closed", "first": at, "last": at, "seen": 30, "max_severity": "CRITICAL"}
    group |= {"taxonomy_code": "REFERENCE_DATA_MAPPING", "change_type": "value_changed"}
    group |= {"affected_keys": most, "since": ["version", "keys"]}
    return {
        "pair": "p" * 63, "version": "10.20.30", "spec_id": 123_456, "state": "not_ready", "regressed": True,
        "score": {"passed": 1, "required": 12},
        "gates": [
            {"gate": gate, "required": True, "status": "met" if gate == "population" else "not_met"}
            | {"text": SCHEDULED if gate == "convergence" else LONGEST}
            for gate in gates.GATES
        ],
        "blocking": list(gates.GATES[1:]), "evidence": at,
        "deviations": ["convergence 2 cycles (default 3; relaxed)", "fresh 30 days (default 7; relaxed)"],
        "definition": gates.DEFINITION, "brief": gates.BRIEF, "now": ended, "config_sha256": "0" * 64,
        "notes": [
            "closed on 2026-11-02: judged as of that day",
            f"regressed: 10.20.30 (spec 123456) was signed off (A-{'0' * 26}, {AT}) and the pair is now below"
            " signed off",
            "over 100,000 ended cycle(s) are not measured yet; over 100,000 ended before 014 are being"
            " measured, and no transition is written until they are",
            f"this server judges with readiness settings {'0' * 12}, the worker with {'1' * 12}: restart both"
            " after changing them",
        ],
        "legend": gates.LEGEND, "points": points, "cycle": cycle,
        "causes": {
            "total": 1_204,
            "text": (
                "Root-cause groups over 30 complete cycle(s) since 2026-10-03: 2 opened in the newest, 5"
                " still open, 1,197 closed (not observed in it); 123,456,789 key(s) of the newest were not"
                " compared, so a closed group's keys may be among them; the first 500 of 1,204 are listed"
            ),
            "items": [group | {"root_cause_id": f"RC-{n:012x}"} for n in range(500)],
        },
    }  # fmt: skip


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page\b", pdf))


def test_brief_pdf_is_one_page() -> None:
    """A brief's PDF is one page whatever the brief holds: each list is cut where a line says how many it
    leaves out and where they are, a gate's text where a clause of it ends, so that a schedule keeps its
    label or is left out whole (§2, N2), and what is still over the page is scaled down, never split."""
    document = reports.brief_document(_longest(), "I-" + "0" * 26, "open")
    assert document.compact and [section.title for section in document.sections] == [
        f"{'p' * 63} 10.20.30 (spec 123456): not ready, regressed",
        "Newest complete cycle",
        "Gates, approval and sign-off",
        "Trend",
        "Root-cause groups",
    ]
    lines = [line for section in document.sections for line in section.lines]
    assert [line for line in lines if line.startswith("… ")] == [
        "… 11 more: the run's report.pdf",
        "… 8 more: Gates in full",
        "… 25 earlier: brief.csv",
        "… 1,198 more: causes.csv",
    ]
    told = dict(line.split(" not met: ") for line in lines if " not met: " in line)
    assert list(told) == ["critical", "targets", "convergence", "spec approval", "sign-off", "pack"]
    cut = LONGEST[: LONGEST.index("; cycle 14")] + "…"  # its first part, with its suppressed count (N6)
    unscheduled = SCHEDULED[: SCHEDULED.index("; cycle 16")] + "…"
    assert told == dict.fromkeys(told, cut) | {"convergence": unscheduled}
    assert cut.endswith("suppressed (known differences or noise only)…") and len(LONGEST) > 300 > len(cut)
    assert not any("scheduled" in line for line in lines)
    assert reports._cut("x" * 301) == "x" * 300 + "…" and reports._cut(SCHEDULED[:300]) == SCHEDULED[:300]
    assert (lines.count(STRATUM), sum(line.startswith("RC-") for line in lines)) == (4, 6)
    assert sum(line.startswith("cycle ") for line in lines) == 5 and len(lines) == 40
    assert _pages(render_pdf(document)) == 1
    tripled = [replace(s, lines=[" ".join([line] * 3) for line in s.lines]) for s in document.sections]
    assert _pages(render_pdf(replace(document, sections=tripled))) == 1
    # Over a page: scaled down, never split.
    assert _pages(render_pdf(replace(document, sections=tripled, compact=False))) > 1


def test_brief_pdf_words_a_ratio_that_rounded_its_count_away() -> None:
    """The counters round a ratio to six places: where a stored coverage reads 1 of keys not all compared,
    or a stored rate 0 of keys that differ, the figures and the trend's line say ``over 0.999999`` and
    ``under 0.000001`` beside the counts; any other ratio is printed as stored."""
    found = _longest()
    counts = {"total": 3_000_001, "compared": 3_000_000, "different": 1, "sample_status": None}
    point = found["points"][-1] | counts | {"mismatch_rate": "0", "coverage": "1"}
    notes = [found["cycle"]["notes"][0], "Mismatch rate under 0.000001: 1 different of 3,000,000 compared."]
    cycle = found["cycle"] | point | {"strata": None, "notes": notes}
    figures, trend = reports._figures(found | {"cycle": cycle}).lines[2], reports._trended(point)
    assert figures.startswith("Keys attempted 3,000,001. Compared 3,000,000 (coverage over 0.999999): Match ")
    assert "3,000,000 compared, 1 different, " in trend and "; rate under 0.000001; " in trend
    whole = {"different": 0, "compared": 3_000_001}  # every key compared and none different: as stored
    assert "(coverage 1): Match " in reports._figures(found | {"cycle": cycle | whole}).lines[2]
    assert "; rate 0; " in reports._trended(point | whole)
