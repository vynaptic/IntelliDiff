"""``ds notify``: alerts and notifications."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import typer

from deltasleuth.cli.common import (
    EXIT_USAGE,
    ConfigOpt,
    RunArg,
    _a,
    _at,
    _errors,
    _fail,
    _in_context,
    _json,
    _local,
    _o,
)
from deltasleuth.config import load_settings

if TYPE_CHECKING:
    from deltasleuth.context import Context
    from deltasleuth.store import repo


notify_app = typer.Typer(invoke_without_command=True)


MUTE_REASON = "Why, in 1 to 1000 characters. It is audited and reaches the SIEM: never personal data."


def _count(value: Any) -> str:
    return "not counted" if value is None else f"{value:,}" if isinstance(value, int) else str(value)


def _last(row: repo.Row) -> str:
    """A delivery's newest attempt, or a channel's: its error (or ok) and its HTTP or SMTP status."""
    if not (row["last_error"] or row["last_status"]):
        return ""
    return f"last {row['last_error'] or 'ok'}" + (f" ({row['last_status']})" if row["last_status"] else "")


@notify_app.callback()
def notify_status(
    command: typer.Context,
    as_json: Annotated[bool, _o("Print GET /v1/notifications's body.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show the notifications: the banners, the feed's cursor, each channel with its counts and its newest
    attempt, the rules and the active mutes; or check the channels, explain a cycle, list deliveries, send a
    test, send dead deliveries again, mute or unmute. Channels and rules are declared in deltasleuth.yaml
    (notify) and apply at the next restart."""
    if command.invoked_subcommand is not None:
        if as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    from deltasleuth import notifications

    found = _in_context(config, notifications.status)
    if as_json:
        typer.echo(_json(found))
        return
    if not found["enabled"]:
        typer.echo(notifications.OFF)
        return
    for line in found["banners"]:
        typer.echo(line)
    cursor = found["cursor"]
    unread = f"; unread since {_at(cursor['behind'])}" if cursor["behind"] else ""
    notified = f"audit row {cursor['seq'] or '-'} of {cursor['head']} notified"
    typer.echo(f"label {found['label']}: {notified}{unread}")
    for c in found["channels"]:
        last = f"; {_last(c)}" if _last(c) else ""
        typer.echo(
            f"channel {c['name']} ({c['kind']}, {c['host']}): {c['pending']:,} pending; in 24 h"
            f" {c['sent_24h']:,} sent, {c['dead_24h']:,} dead, {c['suppressed_24h']:,} held back{last}"
        )
    for r in found["rules"]:
        typer.echo(f"rule {r['name']}: {', '.join(r['events'])} to {', '.join(r['channels'])}")
    for m in found["mutes"]:
        typer.echo(
            f"{m['mute_id']}  {m['scope']} {m['target']}  until {_at(m['until'])}  by {m['created_by']}:"
            f" {m['reason']!r}"
        )


@notify_app.command("check")
def notify_check(config: ConfigOpt = None) -> None:
    """Check each channel without a database and without sending: read its secrets and resolve and vet its
    destination, or the proxy it sends through, as a send does. Exit 2 names each failing check; the URL a
    url_ref file holds is never printed."""
    from deltasleuth import notifications
    from deltasleuth.connectors.notify import check

    with _errors():
        lines = check(notifications.on(load_settings(config)))
    for line in lines:
        typer.echo(line, err=line.startswith("error:"))
    if any(line.startswith("error:") for line in lines):
        raise typer.Exit(EXIT_USAGE)


@notify_app.command("explain")
def notify_explain(
    run_id: RunArg,
    cycle: Annotated[int | None, _o("An ended cycle (default: the latest).")] = None,
    as_json: Annotated[bool, _o("Print it as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show an ended cycle's digest as the notifier computes it now, and each rule's verdict on its
    cycle.ended; writes nothing."""
    from deltasleuth import notifications

    found = _in_context(config, lambda ctx: notifications.explain(ctx, run_id, cycle))
    if as_json:
        typer.echo(_json(found))
        return
    f, c = found["facts"], found["cycle"]
    if found["purged"]:
        typer.echo(f"cycle {c} is purged: root causes, key changes and criticals are unavailable")
    typer.echo(f"cycle {c} of run {run_id} ({found['spec_name']}, {found['mode']})")
    typer.echo("  " + ", ".join(f"{name} {_count(value)}" for name, value in f["counters"].items()))
    for name, value in f["levels"].items():
        before = f["previous"][name]
        was = f"cycle {before['cycle']}: {before['value']}" if before else "held by no earlier cycle"
        typer.echo(f"  {name} {_count(value)} ({was})")
    if c == 1:  # against an empty baseline
        typer.echo(
            f"  in the first cycle: {_count(f['new_root_causes'])} root causes and"
            f" {_count(f['newly_discrepant'])} discrepant keys observed"
        )
    else:
        samples = " (between samples)" if f["between_samples"] else ""
        since = f"since cycle {f['baseline_from']}" if f["baseline_from"] else "in the cycles retained"
        typer.echo(
            f"  root causes {since}{samples}: {_count(f['new_root_causes'])} new,"
            f" {_count(f['gone_root_causes'])} no longer observed"
        )
        typer.echo(
            f"  keys read in both cycles {c - 1} and {c}: {_count(f['newly_discrepant'])} newly discrepant,"
            f" {_count(f['no_longer_discrepant'])} no longer"
        )
    typer.echo(f"  unavailable: {', '.join(f['unavailable']) or '-'}")
    for rule, verdict in found["rules"].items():
        typer.echo(f"rule {rule}: {verdict}")


@notify_app.command("deliveries")
def notify_deliveries(
    channel: Annotated[str | None, _o("Only this channel's.")] = None,
    state: Annotated[str | None, _o("Only this state: pending, sent, dead or suppressed.")] = None,
    run_id: Annotated[str | None, _o("Only this run's.", "--run")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500, help="At most this many.")] = 50,
    as_json: Annotated[bool, _o("Print them as JSON, with their stored bodies.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List deliveries newest first: channel, event, severity, state, attempts, the audit row and the last
    error; their stored bodies (an e-mail's without addresses) with --json."""
    from deltasleuth.store import repo

    if state is not None and state not in ("pending", "sent", "dead", "suppressed"):
        _fail("--state is one of pending, sent, dead, suppressed")

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            found = await repo.list_deliveries(conn, channel=channel, state=state, run_id=run_id, limit=limit)
            return found[0]

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for d in rows:
        line = f"{d['delivery_id']}  {_at(d['created_at'])}  {d['channel']}  {d['event']}  {d['severity']}"
        line += f"  {d['state']}" + (f" ({d['why']})" if d["why"] else "") + f"  {d['attempts']} attempt(s)"
        line += f"  audit row {d['seq']}" if d["seq"] else ""
        line += f"  run {d['run_id']}" if d["run_id"] else ""
        typer.echo(line + (f"  {_last(d)}" if _last(d) else ""))


@notify_app.command("test")
def notify_test(channel: Annotated[str, _a("A declared channel.")], config: ConfigOpt = None) -> None:
    """Send a test notification to the channel now, outside the queue, and record it as sent or dead; exit
    2 when it is not delivered."""
    from deltasleuth import notifications

    who = _local(config)

    async def work(ctx: Context) -> tuple[dict[str, Any], str]:
        done = await notifications.test(ctx, channel, who)
        assert ctx.settings.notify is not None  # the test refuses a channel the settings lack
        return done, ctx.settings.notify.channels[channel].kind

    done, kind = _in_context(config, work)
    protocol = "SMTP" if kind == "email" else "HTTP"
    status = f" ({protocol} {done['status']})" if done["status"] else ""
    if done["state"] != "sent":
        _fail(f"{channel}: {done['error']}{status} ({done['delivery_id']})")
    refused = f", {done['error']}" if done["error"] else ""
    took = f"in {done['latency_ms']} ms ({done['delivery_id']})"
    typer.echo(f"sent to {channel}: {protocol} {done['status']}{refused} {took}")


@notify_app.command("retry")
def notify_retry(
    channel: Annotated[str, _a("A declared channel.")],
    since: Annotated[
        str | None, _o("Only those that died from this ISO-8601 time with a zone (default: 72 hours ago).")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Send the channel's dead deliveries again, tests aside: each is pending, due now, with no attempt."""
    from deltasleuth import notifications

    who = _local(config)
    count, at = _in_context(config, lambda ctx: notifications.retry(ctx, channel, since, who))
    typer.echo(f"{count} dead deliveries of {channel} since {_at(at)} are pending again")


@notify_app.command("mute")
def notify_mute(
    hours: Annotated[int, _o("For 1 to 168 hours.")],
    reason: Annotated[str, _o(MUTE_REASON)],
    channel: Annotated[str | None, _o("Mute this declared channel.")] = None,
    run_id: Annotated[str | None, _o("Mute this run's notifications.", "--run")] = None,
    config: ConfigOpt = None,
) -> None:
    """Hold back a channel's notifications, or a run's, for some hours: they are stored as suppressed, never
    sent. Every screen says so until it lapses or is ended."""
    from deltasleuth import notifications

    if (channel is None) == (run_id is None):
        _fail("give --channel or --run")
    who = _local(config)
    if channel is not None:
        scope, target = "channel", channel
    else:
        assert run_id is not None  # one of the two, as checked above
        scope, target = "run", run_id
    row = _in_context(config, lambda ctx: notifications.mute(ctx, scope, target, hours, reason, who))
    what = target if scope == "channel" else f"run {target}"
    typer.echo(f"muted {what} until {notifications.utc(row['until'])} ({row['mute_id']})")


@notify_app.command("unmute")
def notify_unmute(mute_id: Annotated[str, _a("The mute id (M-…).")], config: ConfigOpt = None) -> None:
    """End a mute now."""
    from deltasleuth import notifications

    who = _local(config)
    _in_context(config, lambda ctx: notifications.unmute(ctx, mute_id, who))
    typer.echo(f"ended mute {mute_id}")
