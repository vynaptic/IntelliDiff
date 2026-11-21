"""The onboarding flow through the ``ds`` CLI on a generated source: init and migrate, profile the samples,
validate the spec, run from the samples and live, measure, adjudicate, score and report, plus a
one-shot compare. Every file and line the CLI writes is masked, and each report it writes is audited."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from deltasleuth.cli.main import app
from deltasleuth.reports.csv import read_csv, render_csv
from tests.support import records

pytestmark = [pytest.mark.e2e, pytest.mark.db]


def test_the_p0_flow_from_samples_to_a_passed_exit(serve: Any, make_stack: Any, tmp_path: Path) -> None:
    stack = make_stack(serve("group").targets())
    records.write_samples("group", tmp_path / "samples")
    spec_file = records.write_spec(tmp_path, "group")
    printed: list[str] = []

    def ds(*args: Any, code: int = 0) -> str:
        """The command's stdout; its stderr holds the SIEM copy of its audit rows."""
        result = CliRunner().invoke(app, [str(a) for a in args])
        printed.append(result.output)
        assert result.exit_code == code, result.output
        return result.stdout

    config = ("--config", stack.settings)
    assert "tokeniser key" in ds("init", *config) and ds("migrate", *config).startswith("migrations applied")
    assert ds("migrate", *config) == "schema up to date\n"
    draft = tmp_path / "profile"
    ds("profile", "--samples", tmp_path / "samples", "--out", draft, "--name", "draft-group",
       "--target-a", "group-a", "--target-b", "group-b", "--key-name", "id")  # fmt: skip
    assert "item-001" in (draft / "draft.spec.yaml").read_text()  # the draft lists the sampled keys
    assert (draft / "profile_evidence.csv").exists()
    assert ds("spec", "validate", spec_file, "--targets", tmp_path / "targets.yaml").startswith("ok:")

    filed = ds("run", spec_file, "--samples", tmp_path / "samples", *config)
    assert "\ncompleted\n" in filed and "total 61" in filed
    live = ds("run", spec_file, "--fail-on-diff", *config, code=1)  # the seeded discrepancies
    run_id = re.search(r"run (R-\w+) started", live)[1]  # type: ignore[index]
    assert run_id in ds("runs", *config)

    p0 = tmp_path / "p0"
    (tmp_path / "missing.txt").write_text("item-999\n")
    measured = ds("measure", run_id, "--out", p0, "--missing-keys", tmp_path / "missing.txt", *config)
    assert "to review" in measured and (p0 / "capabilities.proposed.yaml").exists()
    assert "selected 50 of 61" in ds("adjudicate", run_id, "--out", p0, *config)
    header, rows = read_csv(p0 / "adjudication.csv")
    for row in rows:  # the reviewers agree with the engine on every key
        row["reviewer_verdict"] = row["engine_verdict"]
    (p0 / "adjudication.csv").write_text(render_csv(header, rows), newline="")
    assert "onboarding exit: PASSED" in ds("score", "--out", p0, *config)
    kinds = ("--kind", "summary", "--kind", "report.pdf", "--kind", "discrepancies.csv")
    ds("report", run_id, "--out", p0, *kinds, *config)
    assert "| 50 adjudicated keys agree with reviewers | pass |" in (p0 / "report.md").read_text()
    # An open build's run carries no label, on a file name or in a report.
    assert (p0 / f"{run_id}-report.pdf").exists() and (p0 / f"{run_id}-discrepancies.csv").exists()
    assert not [path.name for path in p0.iterdir() if "unlicensed" in path.name]
    assert "nlicensed" not in (p0 / "report.md").read_text() + (p0 / "report.html").read_text()
    downloads = json.loads(ds("audit", "--action", "report.download", "--json", *config))
    assert [(r["object"], r["detail"]["name"], r["detail"]["cycle"]) for r in reversed(downloads)] == [
        (run_id, name, 1)
        for name in ("measure.json", "adjudication.csv", "adjudication_score.json", "summary", "report.pdf",
                     "discrepancies.csv")
    ]  # fmt: skip

    shot = ds("compare", "item-007", "--spec", spec_file, "--fail-on-diff", *config, code=1)
    assert shot.startswith("item-007: MISMATCH (DIFFERENT, rechecked)")
    written = [p.read_text(errors="ignore") for p in p0.iterdir() if p.suffix != ".pdf"]
    assert not any(v in text for v in records.private_values("group") for text in written + printed)
