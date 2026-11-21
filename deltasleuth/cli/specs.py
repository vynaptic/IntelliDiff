"""``ds spec validate``."""

from __future__ import annotations

import typer

from deltasleuth.cli.common import SpecFile, TargetsOpt, _errors, _fail, _issues
from deltasleuth.core.spec import load_spec_file, load_targets, validate_spec

spec_app = typer.Typer(no_args_is_help=True, help="Comparison specs.")


@spec_app.command("validate")
def spec_validate(spec: SpecFile, targets: TargetsOpt = None) -> None:
    """Validate a spec against spec.schema.json and the loader rules."""
    with _errors():
        issues = validate_spec(load_spec_file(spec), targets=load_targets(targets) if targets else None)
    _issues(issues)
    if blocking := [i for i in issues if i.severity == "error"]:
        _fail(f"{len(blocking)} blocking issue(s) in {spec.name}")
    typer.echo(f"ok: {spec.name} is valid ({len(issues)} warning(s))")
