"""The onboarding tools: ``ds profile``, ``measure``, ``adjudicate`` and ``score``; and ``ds llm-probe``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import (
    ConfigOpt,
    CycleOpt,
    OutOpt,
    RunArg,
    TargetsOpt,
    _errors,
    _fail,
    _in_context,
    _issues,
    _local,
    _o,
    _read_keys,
    _run,
    _wrote,
)
from deltasleuth.config import SettingsError, load_settings
from deltasleuth.core.spec import load_targets, validate_spec
from deltasleuth.errors import Invalid

if TYPE_CHECKING:
    from deltasleuth.context import Context


_EVIDENCE = ("kind", "path_a", "path_b", "decision", "sample_count", "scores", "note")


def profile(
    samples: Annotated[Path, _o("Samples directory (layout 1 or 2).")],
    out: OutOpt,
    name: Annotated[str, _o("Spec name for the draft.")],
    target_a: Annotated[str, _o("Target id of side a.")],
    target_b: Annotated[str, _o("Target id of side b.")],
    key_name: Annotated[str, _o("Name of the key.")],
    key_pattern: Annotated[str | None, _o("Regex keys must match.")] = None,
    request_a: Annotated[str | None, _o("Side a's request, e.g. 'GET /v1/x/{{ key }}'.")] = None,
    request_b: Annotated[str | None, _o("Side b's request, e.g. 'GET /v2/x/{{ key }}'.")] = None,
    targets: TargetsOpt = None,
) -> None:
    """Draft a spec from paired samples: draft.spec.yaml and profile_evidence.csv, for review."""
    from deltasleuth.connectors.files import FileSampleSource
    from deltasleuth.core.spec.documents import parse_yaml_text
    from deltasleuth.core.types import Side
    from deltasleuth.profiler import bootstrap
    from deltasleuth.reports.csv import render_csv

    with _errors():
        requests = {side: _request(t) for side, t in ((Side.A, request_a), (Side.B, request_b)) if t}
        source = FileSampleSource(samples)
        pairs = [(k, *(_document(source.observation(k, side)) for side in Side)) for k in source.keys()]
        draft = bootstrap(pairs, spec_name=name, target_a=target_a, target_b=target_b, key_name=key_name,
                          key_pattern=key_pattern, requests=requests or None)  # fmt: skip
        registry = load_targets(targets) if targets else None
        issues = validate_spec(parse_yaml_text(draft.yaml_text), targets=registry)
        out.mkdir(parents=True, exist_ok=True)
        (out / "draft.spec.yaml").write_text(draft.yaml_text, encoding="utf-8")
        rows = [row.as_dict() | {"scores": json.dumps(row.scores, default=str)} for row in draft.evidence]
        (out / "profile_evidence.csv").write_text(render_csv(_EVIDENCE, rows), newline="")
    for note in draft.notes:
        typer.echo(f"note: {note}")
    _issues(issues)
    typer.echo(f"profiled {len(pairs)} pair(s): {out / 'draft.spec.yaml'}, {out / 'profile_evidence.csv'}")
    if any(i.severity == "error" for i in issues):
        _fail("the draft does not validate; review it (see the issues above)")


def _request(text: str) -> dict[str, str]:
    """``"GET /v1/things/{{ key }}"`` as a spec request block."""
    method, _, path = text.strip().partition(" ")
    if method.upper() not in ("GET", "POST") or not path.strip().startswith("/"):
        raise Invalid("a request is 'GET /path/{{ key }}' or 'POST /path'")
    return {"method": method.upper(), "path": path.strip()}


def _document(obs: Any) -> Any:
    """A sample's JSON document; None without a 2xx JSON body (the profiler's "no document")."""
    from deltasleuth.core.jsonio import JSONParseError, ResourceLimitError, parse_json

    if obs is None or obs.body is None or not 200 <= (obs.http_status or 0) < 300:
        return None
    try:
        return parse_json(obs.body)[0]
    except (JSONParseError, ResourceLimitError):
        return None


def measure(
    run_id: RunArg,
    out: OutOpt,
    missing_keys: Annotated[
        Path | None, _o("Keys known not to exist: the basis of a proposed absence.")
    ] = None,
    cycle: CycleOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Measure the endpoints' observable contract from a run and propose capabilities for review."""
    from deltasleuth.reports.measure import MEASURE_NAME, PROPOSAL_NAME, measure_run

    who = _local(config)
    with _errors():
        missing = _read_keys(missing_keys) if missing_keys else ()

    async def work(ctx: Context) -> dict[str, Any]:
        measured = await measure_run(ctx, run_id, out, cycle=cycle, missing_keys=missing)
        await _wrote(ctx, who, run_id, MEASURE_NAME, cycle)
        return measured

    found = _in_context(config, work)
    flags = sum(len(p["flags"]) for proposals in found["proposed_capabilities"].values() for p in proposals)
    typer.echo(f"{flags} item(s) to review: {out / MEASURE_NAME}, {out / PROPOSAL_NAME}")


def adjudicate(
    run_id: RunArg,
    out: OutOpt,
    limit: Annotated[int, _o("Keys to select.")] = 50,
    salt: Annotated[str, _o("Selection salt.")] = "p0-adjudication",
    force: Annotated[bool, _o("Archive a reviewed sheet and write a new one.", "--force")] = False,
    cycle: CycleOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Select up to --limit stratified keys and write the reviewer sheet (adjudication.csv)."""
    from deltasleuth.reports import adjudicate as adj

    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        selection = await adj.adjudicate(ctx, run_id, out, cycle=cycle, limit=limit, salt=salt, force=force)
        await _wrote(ctx, who, run_id, adj.SHEET_NAME, selection["cycle"])
        return selection

    chosen = _in_context(config, work)
    if chosen["archived_sheet"]:
        typer.echo(f"the reviewed sheet was archived as {chosen['archived_sheet']}")
    typer.echo(f"selected {len(chosen['keys'])} of {chosen['candidates']} key(s): {out / adj.SHEET_NAME}")
    for what, left_out in chosen["unrepresented"].items():
        if left_out:
            typer.echo(f"  not represented: {len(left_out)} {what}; raise --limit", err=True)


def score(out: OutOpt, config: ConfigOpt = None) -> None:
    """Score the filled reviewer sheet against the engine and check the onboarding exit criterion."""
    from deltasleuth.reports.adjudicate import SCORE_NAME, SELECTION_NAME
    from deltasleuth.reports.adjudicate import score as score_sheet
    from deltasleuth.reports.data import read_json

    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        scored = await score_sheet(ctx, out)
        await _wrote(ctx, who, scored["run_id"], SCORE_NAME, read_json(out / SELECTION_NAME)["cycle"])
        return scored

    s = _in_context(config, work)
    typer.echo(f"adjudicated {s['adjudicated']} of {s['rows']}; agreement {s['agreement_rate']}")
    for d in s["disagreements"]:
        typer.echo(
            f"  disagreement {d['key']}: engine {d['engine_verdict']}, reviewer {d['reviewer_verdict']}"
        )
    passed = s["exit"]["passed"]
    reasons = "; ".join(s["exit"]["reasons"])
    typer.echo("onboarding exit: PASSED" if passed else f"onboarding exit: NOT YET ({reasons})")


def llm_probe(
    profile: Annotated[str | None, _o("Profile id (default: the settings' llm_profile).")] = None,
    environment: Annotated[str | None, _o("Environment (default: the settings' llm_environment).")] = None,
    out: Annotated[Path | None, _o("Also write the JSON report into this directory.")] = None,
    config: ConfigOpt = None,
) -> None:
    """Probe an LLM profile's capabilities through the ds_llm port (fail-closed egress)."""
    from deltasleuth.llm.client import LLMClient
    from deltasleuth.llm.policy import EgressPolicy
    from deltasleuth.llm.probe import probe_capabilities
    from deltasleuth.llm.profiles import load_profiles

    async def main() -> Any:
        settings = load_settings(config)
        if settings.llm_profiles_file is None:
            raise SettingsError("AI is off: the settings name no llm_profiles_file")
        chosen = load_profiles(settings.llm_profiles_file).get(profile or settings.llm_profile or "")
        egress = EgressPolicy(environment or settings.llm_environment, settings.llm_external_data_classes)
        async with LLMClient.from_profile(chosen, egress=egress) as client:
            return await probe_capabilities(client)

    probed = _run(main())
    typer.echo(f"profile {probed.profile_id}: " + ", ".join(f"{r.name}={r.status}" for r in probed.results))
    if out:
        with _errors():
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"llm_probe_{probed.profile_id}.json"
            path.write_text(probed.to_json() + "\n", encoding="utf-8")
        typer.echo(str(path))
