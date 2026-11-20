"""``ds purge``: retention, a run erased now, and a run's evidence held or released."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer

from deltasleuth.cli.common import REASON, ConfigOpt, RunArg, _fail, _in_context, _local, _o

if TYPE_CHECKING:
    from deltasleuth.store import repo


purge_app = typer.Typer(invoke_without_command=True)


@purge_app.callback()
def purge(
    command: typer.Context,
    limit: Annotated[int | None, typer.Option(min=1, help="Purge at most this many cycles.")] = None,
    orphans: Annotated[bool, _o("Also delete evidence files nothing names.", "--orphans")] = False,
    dry_run: Annotated[bool, _o("Show what a pass would do, writing nothing.", "--dry-run")] = False,
    run_id: Annotated[str | None, _o("Erase this run now (R-…), whatever its retention.", "--run")] = None,
    cycle: Annotated[int | None, _o("With --run: erase only this ended cycle.")] = None,
    reason: Annotated[str | None, _o(REASON)] = None,
    config: ConfigOpt = None,
) -> None:
    """Delete the evidence of the ended cycles past their retention, as the worker does hourly; or erase
    a run now; or hold or release a run's evidence. Each purged cycle is audited."""
    from deltasleuth.runner import purge as purger

    options = (limit, cycle, reason, run_id, config)
    if command.invoked_subcommand is not None:
        if orphans or dry_run or any(option is not None for option in options):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    if run_id is None:
        if cycle is not None or reason is not None:
            _fail("--cycle and --reason go with --run R-…")
        if dry_run:
            _preview(_in_context(config, lambda ctx: purger.preview(ctx, limit=limit)))
            return
        who = _local(config)
        done = _in_context(config, lambda ctx: purger.purge(ctx, who, limit=limit, orphans=orphans))
    else:
        if dry_run or orphans or limit is not None:
            _fail("--run erases one run: it takes no --dry-run, --orphans or --limit")
        if reason is None:
            _fail("--run needs --reason (an erasure is audited)")
        if problem := purger.reason_error(reason):
            _fail(problem)
        who = _local(config)
        done = _in_context(config, lambda ctx: purger.erase(ctx, who, run_id, reason, cycle))
    typer.echo(
        f"purged {done.cycles:,} cycle(s) of {len(done.runs):,} run(s): {done.keys:,} key(s);"
        f" {done.blobs_queued:,} blob(s) queued; {done.blobs_deleted:,} deleted ({_size(done.bytes)}),"
        f" {done.blobs_shared:,} kept (shared)"
    )
    for count, what in ((done.abandoned, "abandoned {} run(s) past their retention"),
                        (done.held, "held: {} cycle(s) past their retention"),
                        (done.orphans, "deleted {} orphaned file(s)"),
                        (done.prechecks, "deleted {} table pre-check(s) past their retention")):  # fmt: skip
        if count:
            typer.echo(what.format(f"{count:,}"))


def _size(n: int) -> str:
    """Bytes in the nearest decimal unit, e.g. 1.9 GB."""
    value, unit = float(n), "B"
    for larger in ("kB", "MB", "GB", "TB"):
        if value < 1000:
            break
        value, unit = value / 1000, larger
    return f"{n} B" if unit == "B" else f"{value:.1f} {unit}"


def _day(value: datetime) -> str:
    return f"{value.astimezone(UTC):%Y-%m-%d}"


def _preview(plan: dict[str, list[repo.Row]]) -> None:
    """``ds purge --dry-run``'s lines."""
    lines = []
    for row in plan["due"]:
        what = "its interrupted purge resumes" if row["purging_at"] else f"expired {_day(row['expires_at'])}"
        lines.append(f"due: {_cycle(row)}: {what}")
    for row in plan["kept"]:
        expired = f"{_cycle(row)}: expired {_day(row['expires_at'])}"
        if row["hold_reason"] is not None:
            held = f"held by {row['hold_by']} since {_day(row['hold_at'])}: {row['hold_reason']!r}"
            lines.append(f"held: {expired}; {held}")
        else:
            lines.append(f"kept: {expired}; its running soak reads it until its next cycle ends")
    for row in plan["abandon"]:
        run = f"run {row['run_id']} ({row['status']} since {_day(row['updated_at'])})"
        if row["mode"] == "one_shot" and row["status"] == "queued":
            lines.append(f"abandon: {run}: failed, a one-shot run queued for 24 h")
        else:
            past = f"past its {row['days']} days (retention {row['retention']})"
            lines.append(f"abandon: {run}: cancelled, {past}; its cycle is then due")
    samples = sorted({(row["samples_dir"], row["run_id"]) for row in plan["due"] if row["samples_dir"]})
    for directory, run_id in samples:
        lines.append(f"outside retention: {directory} (run {run_id}): captured samples, for the site's cron")
    for line in lines or ["nothing to purge"]:
        typer.echo(line)


def _cycle(row: repo.Row) -> str:
    return f"cycle {row['cycle']} of run {row['run_id']} (retention {row['retention']})"


@purge_app.command("hold")
def purge_hold(run_id: RunArg, reason: Annotated[str, _o(REASON)], config: ConfigOpt = None) -> None:
    """Keep a run's evidence past its retention, until released (a legal hold or an investigation)."""
    from deltasleuth.runner import purge as purger

    if problem := purger.reason_error(reason):
        _fail(problem)
    who = _local(config)
    _in_context(config, lambda ctx: purger.hold(ctx, who, run_id, reason))
    typer.echo(f"held run {run_id}: its evidence stays until 'ds purge release {run_id}'")


@purge_app.command("release")
def purge_release(run_id: RunArg, config: ConfigOpt = None) -> None:
    """Release a run's hold: its cycles past their retention go at the next pass."""
    from deltasleuth.runner import purge as purger

    who = _local(config)
    _in_context(config, lambda ctx: purger.hold(ctx, who, run_id, None))
    typer.echo(f"released run {run_id}: its cycles past their retention go at the next pass")
