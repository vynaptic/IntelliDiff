"""The PDF run report: section order, trend, soak changes, masked findings and the AI annex; an ended
cycle's labelled accuracy, in report.pdf and the contract report."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.reports import accuracy, pdf
from deltasleuth.reports.csv import write_csv
from deltasleuth.reports.data import TOP_GRAPH_ROWS, load_run
from deltasleuth.reports.pdf import AI_LABEL, pdf_report, report_document
from deltasleuth.reports.render import Document
from deltasleuth.reports.summary import summary_document
from deltasleuth.store import repo
from tests.conftest import TESTER
from tests.unit.store.four_eyes import drawn, person, sampled, token, upload

from .conftest import (
    SECRET_NAME,
    compare_key,
    doc,
    graph_row,
    seed_graph_run,
    seed_run,
    store_counters,
    store_root_causes,
    unit_spec,
)
from .packs import TRUTH, labelled_pairs, labelled_sheet, sign_off, signed_cycle

pytestmark = pytest.mark.db
HOSTILE = "<b>unclosed <font color=red> & [F-0123456789abcdef]"


def _text(document: Document) -> str:
    return "\n".join(
        [document.meta, *(s.title for s in document.sections),
         *(line for s in document.sections for line in s.lines),
         *(str(c) for s in document.sections for row in s.rows for c in row)]
    )  # fmt: skip


def _counters(document: Document) -> dict[Any, Any]:
    """The counters grid read back as counter -> value."""
    cells = [cell for row in document.sections[1].rows for cell in row]
    return dict(zip(cells[::2], cells[1::2], strict=True))


async def _answer(ctx: Context, run_id: str, text: str) -> None:
    async with ctx.pool.connection() as conn:
        session = await repo.create_chat_session(conn, run_id, "K002", created_by=TESTER.actor)
        sealer = await ctx.sealer(conn, run_id)
        question = {"content": "why does K002 differ?", "tool_calls": []}
        await repo.append_chat_message(conn, session, "user", question, sealer)
        answer = {"content": text, "tool_calls": []}
        await repo.append_chat_message(
            conn, session, "assistant", answer, sealer, citations=["F-0123456789abcdef"],
            report_prompt="root_cause",
        )


async def test_run_report_puts_root_causes_first_and_ai_text_in_its_annex(ctx: Context) -> None:
    same = {"K001": (doc("K001"), doc("K001")), "K002": (doc("K002"), doc("K002"))}
    previous = await seed_run(ctx, same)
    # Newer, but no trend baseline: another spec's run (with its own chat) and a failed run.
    other = await seed_run(
        ctx, same, spec=unit_spec(metadata={"name": "other", "version": "1.0.0", "owner": "t"})
    )
    await _answer(ctx, other, "an answer about another run")
    await seed_run(ctx, same, status="failed")
    medium_first = (doc("K001"), doc("K001", tags=("x",)))  # MEDIUM, on the earlier key
    run_id = await seed_run(
        ctx, {"K001": medium_first, "K002": (doc("K002", status="A"), doc("K002", status="B"))}
    )
    await _answer(ctx, run_id, HOSTILE)
    report = await report_document(ctx, run_id)
    assert [s.title for s in report.sections] == [
        "Root causes", "Coverage counters", "Trend against the previous run", "Pair readiness (observed)",
        "Outcome changes across soak cycles", "Top findings", f"Annex: {AI_LABEL}",
    ]  # fmt: skip
    assert run_id in report.meta and "Spec unit-reports 1.0.0" in report.meta
    assert "Claim level: observed" in report.meta  # an MVP run never reads as a census
    async with ctx.pool.connection() as conn:
        causes = [rc["root_cause_id"] for rc in await repo.list_root_causes(conn, run_id, 1)]
    assert len(causes) == 2 and [row[1] for row in report.sections[0].rows] == causes
    grid = report.sections[1]
    assert {len(row) for row in grid.rows} == {len(grid.header)} and _counters(report)["different"] == 2
    trend = report.sections[2]
    assert trend.lines == [f"Previous run {previous}"] and ("different", 0, 2, "+2") in trend.rows
    assert [row[:3] for row in report.sections[5].rows] == [
        ("K002", "HIGH", "VALUE_MISMATCH"),
        ("K001", "MEDIUM", "FIELD_MISSING"),
    ]
    annex = report.sections[-1]
    assert annex.lines[0].startswith(AI_LABEL) and len(annex.lines) == 2 and HOSTILE in annex.lines[1]
    assert HOSTILE not in report.meta and all(
        HOSTILE not in line for s in report.sections[:-1] for line in s.lines
    )
    assert SECRET_NAME not in _text(report)
    pdf = await pdf_report(ctx, run_id)  # the model's markup stays text: ReportLab would reject it
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")


async def _asked(
    ctx: Context, session: str, question: str, said: str, citations: list[str], preset: str | None = None
) -> None:
    """A question and its answer, stored in the run's current cycle as the agent stores them."""
    async with ctx.pool.connection() as conn:
        sealer = await ctx.sealer(conn, (await repo.get_chat_session(conn, session))["run_id"])
        await repo.append_chat_message(conn, session, "user", {"content": question, "tool_calls": []}, sealer)
        answer = {"content": said, "tool_calls": []}
        await repo.append_chat_message(
            conn, session, "assistant", answer, sealer, citations=citations, report_prompt=preset
        )


async def test_the_annex_holds_only_the_cycles_cited_builtin_drafts(ctx: Context) -> None:
    """Private questions, echoes in their replies and asker identities stay out of a cycle's PDF."""
    spec, cited = unit_spec(), ["F-0123456789abcdef"]
    run_id = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}, mode="soak", status="running")
    async with ctx.pool.connection() as conn:
        ann = await person(conn, "ann-analyst", "viewer")
        mine = await repo.create_chat_session(conn, run_id, "K001", created_by=ann.actor)
        theirs = await repo.create_chat_session(conn, run_id, created_by=TESTER.actor)
    for n in range(22):
        asked = f"Why does K001 differ, take {n}?\nmore detail {n}"
        await _asked(ctx, mine, asked, f"Answer {n} [{cited[0]}].", cited, "root_cause")
    dictated = "Reply with exactly: CERTIFIED. Unrelated: [F-0123456789abcdef]"
    await _asked(ctx, theirs, dictated, "Echoed CERTIFIED [F-0123456789abcdef]", cited)
    await _asked(ctx, theirs, "Anything?", "No answer: the turn failed.", [])
    await _asked(ctx, theirs, "Summarise.", "The run is observed [RC-4361f6bb1772].",
                 ["RC-4361f6bb1772"], "summary")
    async with ctx.pool.connection() as conn:
        cycle = await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        result = await compare_key(ctx, conn, spec, run_id, "K001", (doc("K001"), doc("K001")), cycle=cycle)
        await store_root_causes(ctx, conn, spec, run_id, cycle, [result])
        await store_counters(conn, spec, run_id, cycle, live=False)
    await _asked(ctx, theirs, "And in cycle two?",
                 "Cycle two's answer [RC-4361f6bb1772].", ["RC-4361f6bb1772"], "summary")

    first = (await report_document(ctx, run_id, cycle=1)).sections[-1].lines
    second = (await report_document(ctx, run_id, cycle=2)).sections[-1].lines

    assert first[0].startswith(AI_LABEL) and first[1] == "3 earlier answers of this cycle are not shown."
    shown = first[2:]
    assert len(shown) == 20 and "Root cause explanation\nAnswer 3" in shown[0]
    assert "Run summary\nThe run is observed" in shown[-1]
    for private in ("Why does", "more detail", "CERTIFIED", "asked by", "ann-analyst", "Summarise."):
        assert private not in "\n".join(shown)
    assert len(second) == 2 and "Cycle two's answer" in second[1] and "Answer" not in second[1]


async def test_the_pdf_is_drawn_off_the_event_loop(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[str] = []
    render = pdf.render_pdf
    def drawing(document: Document) -> bytes:
        drawn.append(threading.current_thread().name)
        return render(document)

    monkeypatch.setattr(pdf, "render_pdf", drawing)
    run_id = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))})

    assert (await pdf_report(ctx, run_id)).startswith(b"%PDF-")
    assert drawn and drawn[0] != threading.main_thread().name


async def test_a_cycle_s_report_says_what_the_schema_guard_recorded_through_it(ctx: Context) -> None:
    """One sentence in the title block, added up from the run's ``schema.drift``
    audit rows of that cycle and the earlier ones: a cycle's report reads the same before and after a
    later cycle's change, and a run without such a row says nothing."""
    from deltasleuth.auth.principal import WORKER

    run_id = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))})
    assert "Schema guard" not in (await report_document(ctx, run_id)).meta
    row = {"where": "scan", "mode": "finding", "checked": True, "basis": None, "error_type": None}
    side = {"kinds": {}, "from": "1" * 64, "to": "2" * 64}
    first = row | {"cycle": 1, "paused": False, "sides": {"b": side | {"breaking": 0, "other": 2}}}
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(WORKER, "schema.drift", run_id, first)
    said = "Schema guard (observed): through cycle 1 it recorded 2 changes on side b."
    before = await report_document(ctx, run_id, 1)
    assert said in before.meta.splitlines()
    later = row | {"cycle": 2, "paused": True, "sides": {"b": side | {"breaking": 1, "other": 0}}}
    unchecked = row | {"cycle": 2, "checked": False, "paused": False, "sides": {}, "error_type": "KeyError"}
    async with repo.audited(ctx.pool) as (_, audit):
        await audit(WORKER, "schema.drift", run_id, later)
        await audit(WORKER, "schema.drift", run_id, unchecked)
    assert (await report_document(ctx, run_id, 1)).meta == before.meta  # whenever it is made
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, run_id)
        await conn.execute("UPDATE ds.runs SET cycle = 2 WHERE run_id = %s", (run_id,))
    assert said in (await report_document(ctx, run_id, 1)).meta.splitlines()
    assert (
        "Schema guard (observed): through cycle 2 it recorded 1 change that paused the run and 2 others on"
        " side b, and could not check 1 cycle start."
    ) in (await report_document(ctx, run_id, 2)).meta.splitlines()
    assert b"%PDF-" in await pdf_report(ctx, run_id, 2)


async def test_run_report_states_until_when_the_evidence_is_kept(ctx: Context) -> None:
    from deltasleuth.runner.purge import CyclePurged

    run_id = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))})
    live = await report_document(ctx, run_id)
    assert (
        "Evidence retained until 90 days after the cycle ends (retention standard)" in live.meta.splitlines()
    )
    async with ctx.pool.connection() as conn:
        await repo.end_cycle(conn, run_id, datetime(2026, 9, 27, 12, tzinfo=UTC))
    ended = await report_document(ctx, run_id)
    assert "Evidence retained until 2026-12-26 (retention standard)" in ended.meta.splitlines()
    async with ctx.pool.connection() as conn:
        await repo.set_hold(conn, run_id, "legal hold", TESTER.actor)
    held = await report_document(ctx, run_id)
    assert "Evidence held until released (retention standard)" in held.meta.splitlines()
    async with ctx.pool.connection() as conn:
        assert await repo.start_purge(conn, run_id, 1, "erasure: DSAR-3") is None  # a held run's cycle stays
        await repo.set_hold(conn, run_id, None, None)  # released
        await repo.start_purge(conn, run_id, 1, "erasure: DSAR-3")
    for report in (report_document, pdf_report):
        with pytest.raises(
            CyclePurged, match=rf"^cycle 1 of run {run_id} was purged on \S+ \(erasure\): its"
        ):
            await report(ctx, run_id)


async def test_a_sampled_cycle_s_report_says_whether_its_bounds_are_still_to_be_judged(
    ctx: Context, tmp_path: Path
) -> None:
    """Live, its bounds are judged when the cycle ends; ended without a judgement (its judging raised), the
    report and the contract report say so, and that it cannot be signed off."""
    version = sampled()
    async with ctx.pool.connection() as conn:
        bob = await person(conn, "bob")
        spec_id = await upload(conn, version, bob)
    async with ctx.pool.connection() as conn:
        live, _ = await drawn(conn, spec_id, version, bob, end=False)
    async with ctx.pool.connection() as conn:
        unjudged, _ = await drawn(conn, spec_id, version, bob, judged=False)
        assert (await repo.get_cycle(conn, unjudged, 1) or {})["sample"] is None
    said = {
        live: "Bounds are judged when the cycle ends.",
        unjudged: "Judgement: none: the cycle ended without a judgement and cannot be signed off: draw"
        " again.",
    }
    for run_id, last in said.items():
        summary = summary_document(await load_run(ctx, run_id), tmp_path)
        for document in (await report_document(ctx, run_id), summary):
            [section] = [s for s in document.sections if s.title == "Sample and bounds (observed)"]
            assert (section.lines[-1], list(section.rows)) == (last, []), run_id


async def test_an_ended_cycle_s_reports_state_its_labelled_accuracy_or_that_none_was_labelled(
    pack_ctx: Context, tmp_path: Path
) -> None:
    """The four report texts of labelled accuracy, in report.pdf and the contract report alike: a live
    cycle has no such section; an ended one says that nothing was measured, or prints its labelling whole
    with LIMITS: the newest labelled, a person's before a newer one no person labelled, and once its
    sign-off is requested the one that attests. Where the cycle has other labellings, its second line counts
    them: a reader of either report learns that the one stated is one of several."""
    ctx, title = pack_ctx, "Labelled accuracy (observed)"

    async def said(run_id: str) -> list[str] | None:
        """The section's lines, the same in both reports and after the counters; None without one."""
        report = await report_document(ctx, run_id)
        summary = summary_document(await load_run(ctx, run_id), tmp_path)
        found = [[s for s in document.sections if s.title == title] for document in (report, summary)]
        assert found[0] == found[1] and len(found[0]) <= 1
        if found[0]:
            assert [s.title for s in report.sections][1:3] == ["Coverage counters", title]
        return list(found[0][0].lines) if found[0] else None

    async def stored(labelling_id: str) -> dict[str, Any]:
        async with ctx.pool.connection() as conn:
            return await repo.get_labelling(conn, labelling_id)  # type: ignore[return-value]

    async def labelled(run_id: str, drawer: Principal, labeller: Principal) -> dict[str, Any]:
        drawn_now, _ = await accuracy.store_draw(ctx, run_id, 1, who=drawer, size=3, per_cause=0, truth=TRUTH)
        await accuracy.store_labels(
            ctx, drawn_now["labelling_id"], labelled_sheet(drawn_now["sheet"]), who=labeller
        )
        return await stored(drawn_now["labelling_id"])

    other = unit_spec(metadata={"name": "other", "version": "1.0.0", "owner": "t"})
    assert await said(await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}, spec=other)) is None  # live
    signed = await signed_cycle(ctx, pairs=labelled_pairs(), sign=False)
    run_id, people = signed.run_id, signed.people
    assert await said(run_id) == ["Not measured: no labelled sample of this cycle."]
    async with ctx.pool.connection() as conn:
        bot = await token(conn, "ci-bot", people["olga"])
    day, against = f"{datetime.now(UTC):%Y-%m-%d}", "against CBS-PROD.account_master as of 2026-10-01"
    by_token = await labelled(run_id, people["olga"], bot)
    drew = f"drawn {day} by olga, labelled {day}"
    assert await said(run_id) == [*accuracy.lines(by_token, signoff=None), accuracy.LIMITS]
    assert (await said(run_id) or [])[0] == (
        f"Labelling {by_token['labelling_id']} {drew} through token ci-bot ({bot.actor[6:]}) {against}, not"
        " by a signed-in person: no sign-off attests it."
    )
    assert not [line for line in await said(run_id) or [] if " is one of " in line]  # the cycle's only one
    frank_s = (await labelled(run_id, people["olga"], people["frank"]))["labelling_id"]
    await labelled(run_id, people["olga"], Principal("dev:loopback", "developer", frozenset()))  # newer
    await accuracy.store_draw(ctx, run_id, 1, who=people["olga"], size=2, per_cause=0, truth=TRUTH)
    by_frank, lines = await stored(frank_s), await said(run_id) or []
    assert lines == [*accuracy.lines(by_frank, signoff=None), accuracy.LIMITS]
    assert lines[0] == f"Labelling {frank_s} {drew} by frank {against}; no sign-off attests it."
    assert lines[1] == f"Labelling {frank_s} is one of 4 drawn for cycle 1 (3 labelled)."
    assert lines[2].startswith("Discrepancies: 3 of the 8 that cycle 1 of run ")
    signoff_id = await sign_off(ctx, run_id, 1, signed.spec, people["olga"], people["erin"])
    lines = await said(run_id) or []
    assert lines == [*accuracy.lines(by_frank, signoff=signoff_id), accuracy.LIMITS]
    assert lines[:2] == [
        f"Labelling {frank_s} {drew} by frank {against}, attested by sign-off {signoff_id}.",
        f"Labelling {frank_s} is one of 4 drawn for cycle 1 (3 labelled).",
    ]
    assert (await pdf_report(ctx, run_id)).startswith(b"%PDF-")


async def test_reports_that_list_no_result_do_not_read_them_all(ctx: Context, monkeypatch: Any) -> None:
    run_id = await seed_run(ctx, {"K001": (doc("K001", status="A"), doc("K001", status="B"))})

    async def every_result(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("read every result")

    monkeypatch.setattr(repo, "list_results", every_result)
    assert [row[:2] for row in (await report_document(ctx, run_id)).sections[5].rows] == [("K001", "HIGH")]
    assert b"reconciled" in await write_csv(ctx, run_id, None, "coverage")


async def test_soak_report_lists_outcome_changes_and_counts_each_cycle(ctx: Context) -> None:
    spec = unit_spec()
    run_id = await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}, mode="soak", status="running")
    async with ctx.pool.connection() as conn:
        cycle = await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
        pair = (doc("K001", status="A"), doc("K001", status="B"))
        result = await compare_key(ctx, conn, spec, run_id, "K001", pair, n=1, cycle=cycle)
        await store_root_causes(ctx, conn, spec, run_id, cycle, [result])
        await store_counters(conn, spec, run_id, cycle, live=False)
    now = await report_document(ctx, run_id)
    assert now.sections[4].rows == [(2, "K001", "MATCH", "MISMATCH")]
    earlier = await report_document(ctx, run_id, cycle=1)  # recounted: the run keeps cycle 2's counters
    assert (_counters(earlier)["match"], _counters(now)["different"]) == (1, 1)
    assert (await report_document(ctx, await seed_run(ctx, {}))).sections[4].lines == ["Not a soak run."]


async def test_soak_report_counts_the_reported_cycle_s_changes_and_lists_the_first(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, keys = unit_spec(), ("K001", "K002")
    same, differing = (lambda k: (doc(k), doc(k))), (lambda k: (doc(k, status="A"), doc(k, status="B")))
    run_id = await seed_run(ctx, {k: same(k) for k in keys}, mode="soak", status="running")
    async with ctx.pool.connection() as conn:
        # Both keys differ in cycle 2; K001 matches again in cycle 3.
        for pairs in ({k: differing(k) for k in keys}, {"K001": same("K001"), "K002": differing("K002")}):
            cycle = await repo.start_next_cycle(conn, run_id, datetime.now(UTC))
            results = [
                await compare_key(ctx, conn, spec, run_id, key, pair, n=n, cycle=cycle)
                for n, (key, pair) in enumerate(pairs.items())
            ]
            await store_root_causes(ctx, conn, spec, run_id, cycle, results)
            await store_counters(conn, spec, run_id, cycle, live=False)
    monkeypatch.setattr(pdf, "TOP_CHANGES", 1)
    first, second, third = [(await report_document(ctx, run_id, cycle=n)).sections[4] for n in (1, 2, 3)]
    assert first.lines == ["The run's first cycle has none before it to compare with."] and not first.rows
    assert second.lines == ["2 change(s) from cycle 1 to cycle 2; the first 1 are listed."]
    assert second.rows == [(2, "K001", "MATCH", "MISMATCH")]
    assert third.lines == ["1 change(s) from cycle 2 to cycle 3; the first 1 are listed."]
    assert third.rows == [(3, "K001", "MISMATCH", "MATCH")]
    monkeypatch.setattr(pdf, "TOP_CHANGES", 100)

    async def every_change(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("counted changes the page already holds, or read a run's every cycle")

    monkeypatch.setattr(repo, "count_outcome_changes", every_change)
    monkeypatch.setattr(repo, "outcome_changes", every_change)
    assert (await report_document(ctx, run_id, cycle=2)).sections[4].lines == [
        "2 change(s) from cycle 1 to cycle 2; the first 2 are listed."
    ]


async def test_a_graph_run_reports_its_counts_and_the_first_rows_of_each_code(
    ctx: Context, tmp_path: Path
) -> None:
    groups = [graph_row("GROUP_MEMBERS_DIFFER", f"tok:g{i:02d}") for i in range(TOP_GRAPH_ROWS + 1)]
    run_id = await seed_graph_run(ctx, [*groups, graph_row("CLUSTER_SPLIT", "tok:m1", detail={"members": 2})])
    report = await report_document(ctx, run_id)
    (graph,) = [s for s in report.sections if s.title == "Graph parity"]
    assert graph.lines[:2] == [
        "Groups: 3 on side a, 3 on side b; 2 link the same members, 1 differ.",
        "Components: 1 split, 0 merge, 0 rearranged, 0 too large to expand.",
    ]
    assert graph.lines[2].startswith("ARI and VI are not reported") and graph.lines[-1].endswith(
        "1 more in graph.csv."
    )
    assert [row[1] for row in graph.rows] == [f"tok:g{i:02d}" for i in range(TOP_GRAPH_ROWS)] + ["tok:m1"]
    assert graph.rows[-1] == ("CLUSTER_SPLIT", "tok:m1", 2, 1, 1, 0, '{"members": 2}')
    assert (await pdf_report(ctx, run_id)).startswith(b"%PDF-")
    assert graph in summary_document(await load_run(ctx, run_id), tmp_path).sections  # the summary too
    plain = await report_document(ctx, await seed_run(ctx, {}))
    assert "Graph parity" not in [s.title for s in plain.sections]
