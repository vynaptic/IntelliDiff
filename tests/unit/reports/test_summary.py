"""The P0 contract report: checklist states from the P0 tools, PARTIAL runs and escaping; an ended
cycle's labelled accuracy (docs/mvp/ACCURACY.md §10)."""

from __future__ import annotations

import html
from pathlib import Path

import pytest
import yaml

from deltasleuth.context import Context
from deltasleuth.reports import accuracy
from deltasleuth.reports.adjudicate import adjudicate, score
from deltasleuth.reports.csv import read_csv, render_csv
from deltasleuth.reports.data import load_run
from deltasleuth.reports.measure import measure_run
from deltasleuth.reports.render import Document
from deltasleuth.reports.summary import summary_document, summary_report
from deltasleuth.security.tokenise import mask_key

from .conftest import SECRET_NAME, SPEC_YAML, doc, seed_graph_run, seed_run, unit_spec
from .packs import labelled_cycle, listed_spec, signed_cycle

pytestmark = pytest.mark.db
HOSTILE = "<img src=x onerror=alert(1)> | [x](https://attacker.example/beacon.png)"
ADJUDICATION = "50 adjudicated keys agree with reviewers"
MEASURED = "Contract measured and capabilities proposed"


async def _document(ctx: Context, run_id: str, out_dir: Path) -> Document:
    return summary_document(await load_run(ctx, run_id), out_dir)


def _checks(document: Document) -> dict[str, str]:
    return {name: status for name, status, _ in document.sections[0].rows}


async def test_checklist_follows_the_p0_tools_and_flags_a_stale_score(ctx: Context, tmp_path: Path) -> None:
    run_id = await seed_run(
        ctx, {"K001": (doc("K001"), doc("K001")), "K002": (doc("K002", status="A"), doc("K002", status="B"))}
    )
    checks = _checks(await _document(ctx, run_id, tmp_path))
    assert checks[MEASURED] == checks[ADJUDICATION] == "pending"
    assert (
        checks["Paired responses ingested and compared"] == checks["Run counters reconcile exactly"] == "pass"
    )
    await measure_run(ctx, run_id, tmp_path)
    await adjudicate(ctx, run_id, tmp_path)
    header, rows = read_csv(tmp_path / "adjudication.csv")
    sheet = tmp_path / "adjudication.csv"
    sheet.write_text(
        render_csv(header, [{**r, "reviewer_verdict": r["engine_verdict"]} for r in rows]), newline=""
    )
    await score(ctx, tmp_path)
    checks = _checks(await _document(ctx, run_id, tmp_path))
    assert checks[MEASURED] == "pass" and checks[ADJUDICATION] == "fail"  # current, but 2 keys < 50
    sheet.write_text(render_csv(header, rows), newline="")  # the verdicts are gone; the score is not
    document = await _document(ctx, run_id, tmp_path)
    (check,) = [row for row in document.sections[0].rows if row[0] == ADJUDICATION]
    assert check[1] == "fail" and "stale" in check[2]
    assert any("score is stale" in item for item in document.sections[-1].lines)


async def test_a_run_that_did_not_finish_is_marked_partial(ctx: Context, tmp_path: Path) -> None:
    run_id = await seed_run(
        ctx, {"K001": (doc("K001"), doc("K001"))}, status="cancelled", stop_reason="max_errors"
    )
    document = await _document(ctx, run_id, tmp_path)
    assert document.title.startswith("P0 contract report (PARTIAL):")
    assert "The run stopped early (max_errors)." in document.warning
    assert _checks(document)["Paired responses ingested and compared"] == "fail"


async def test_key_line_names_parts(ctx: Context, tmp_path: Path) -> None:
    """A multi-part key's line names its parts in key order, and the key is shown as its joined text: in
    clear for a business code, one token of its class for any other. Any other key's line is unchanged."""
    key = {"name": "shelf_item", "type": "composite", "parts": ["region", "shelf", "code"],
           "pattern": "^[A-Za-z0-9]+$", "class": "business_code",
           "source": {"kind": "list", "keys": ["EU|s1|7"]}}  # fmt: skip
    spec = unit_spec(key=key)
    documents = [
        await _document(ctx, await seed_run(ctx, {"EU|s1|7": (doc("x"), doc("x"))}, spec=spec), tmp_path),
        await _document(ctx, await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}), tmp_path),
        await _document(ctx, await seed_graph_run(ctx, []), tmp_path),  # two tables: one part
    ]
    lines = [section.lines[1] for d in documents for section in d.sections if section.title == "Spec summary"]
    assert lines == [
        "Key shelf_item (composite: region|shelf|code, class business_code)",
        "Key item_id (string, class business_code)",
        "Key link (composite, class identifier)",
    ]
    assert mask_key("EU|s1|7", spec, ctx.tokeniser) == "EU|s1|7"
    masked = mask_key("EU|s1|7", unit_spec(key=key | {"class": "identifier"}), ctx.tokeniser)
    assert masked.startswith("tok:identifier:") and masked.count("tok:") == 1 and "|" not in masked


async def test_truncation_flags_line(ctx: Context, tmp_path: Path) -> None:
    """A side's line counts the truncation flags it declares, after its cap when it has one."""
    sides = yaml.safe_load(SPEC_YAML)["sides"]
    sides["a"]["truncation"]["flagPaths"] = ["$.more", "$..cut"]
    sides["b"]["truncation"] = {"flagPaths": ["$.more"]}
    documents = [
        await _document(ctx, await seed_run(ctx, {"K001": (doc("K001"), doc("K001"))}, spec=spec), tmp_path)
        for spec in (unit_spec(sides=sides), unit_spec())
    ]
    summaries = [section for d in documents for section in d.sections if section.title == "Spec summary"]
    lines = [line for section in summaries for line in section.lines if line.startswith("Side ")]
    assert lines == [
        "Side a: target svc-a (legacy), select $, notFound 404, truncation cap 50, 2 truncation flag(s)",
        "Side b: target svc-b (new), select $, notFound 404, 1 truncation flag(s)",
        "Side a: target svc-a (legacy), select $, notFound 404, truncation cap 50",
        "Side b: target svc-b (new), select $, notFound 404, truncation cap 50",
    ]


async def test_report_files_render_hostile_text_as_text(ctx: Context, tmp_path: Path) -> None:
    raw = yaml.safe_load(SPEC_YAML)
    raw["knownDiffs"][0]["reason"] = HOSTILE
    run_id = await seed_run(
        ctx, {"K001": (doc("K001"), doc("K001"))}, spec=unit_spec(knownDiffs=raw["knownDiffs"])
    )
    outputs = await summary_report(ctx, run_id, tmp_path)
    md, html = outputs["report.md"].read_text(), outputs["report.html"].read_text()
    assert SECRET_NAME not in md + html and HOSTILE not in md + html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html and "<script" not in html and "http://" not in html
    markdown_it = pytest.importorskip("markdown_it")
    rendered = markdown_it.MarkdownIt("commonmark").enable("table").render(md)
    assert (
        "<img" not in rendered and "<a " not in rendered and "&lt;img src=x onerror=alert(1)&gt;" in rendered
    )


async def test_the_report_files_print_an_ended_cycle_s_labelling_whole_or_that_none_was_labelled(
    pack_ctx: Context, tmp_path: Path
) -> None:
    """docs/mvp/ACCURACY.md §7, §11: every line of ``accuracy.lines`` and LIMITS verbatim, in both files."""
    signed, row = await labelled_cycle(pack_ctx)
    outputs = await summary_report(pack_ctx, signed.run_id, tmp_path / "labelled")
    said = accuracy.lines(row, signoff=signed.signoff_id)
    assert said[0].endswith(f", attested by sign-off {signed.signoff_id}.") and len(said) > 10
    page = html.unescape(outputs["report.html"].read_text())
    plain = outputs["report.md"].read_text().replace("\\", "")  # the Markdown escapes
    for line in (*said, accuracy.LIMITS):
        assert line in page and line in plain, line
    assert "Labelled accuracy (observed)" in page
    other = listed_spec(metadata={"name": "other", "version": "1.0.0", "owner": "tests"})
    blank = await signed_cycle(pack_ctx, spec=other, sign=False)
    outputs = await summary_report(pack_ctx, blank.run_id, tmp_path / "blank")
    for name in outputs:
        assert "Not measured: no labelled sample of this cycle." in outputs[name].read_text(), name
