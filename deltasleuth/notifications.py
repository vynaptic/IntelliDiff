"""Notifications as people see and steer them: the channels' state and the
banners every screen shows, a test send, dead deliveries sent again, mutes, and a cycle's digest, which
``ds notify explain`` and the notifier share. The API and the CLI call this module; only the worker imports
the notifier itself (``runner.notify``).

Nothing here answers a URL, a path, an address or a secret: a channel is its kind and declared host, a
send's answer closed codes. A refusal carries its HTTP status and binding message (``Refused``); the CLI
prints the message. Tests, retries and mutes are the only acts, each audited last in its transaction.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal, printable
from deltasleuth.config import ChannelSettings, NotifySettings, Settings
from deltasleuth.connectors.notify import Sender
from deltasleuth.context import Context
from deltasleuth.core import notify as events
from deltasleuth.core.notify import Event
from deltasleuth.security import rawvalues
from deltasleuth.store import repo

OFF = "notifications are off: add notify to deltasleuth.yaml and restart"
BEHIND = timedelta(minutes=10)  # an unread row or a due head this old: is the worker running?
RETRY_SINCE = timedelta(hours=72)
MUTE_HOURS = 168
REASON_CHARS = 1000  # ds.note
_RUN = re.compile(r"R-[0-9A-Z]{26}")


class Refused(ValueError):
    """A notification act refused: its HTTP status and binding message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def on(settings: Settings) -> NotifySettings:
    """The notify settings; 409 when notifications are off."""
    if settings.notify is None:
        raise Refused(409, OFF)
    return settings.notify


def declared(notify: NotifySettings, name: str) -> ChannelSettings:
    if name not in notify.channels:
        raise Refused(404, f"channel {name} is not declared")
    return notify.channels[name]


def utc(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def routed(notify: NotifySettings) -> list[str]:
    """The fed audit actions whose kinds a rule routes: the rows a feed pass reads."""
    kinds = {kind for rule in notify.rules for kind in rule.events}
    return [action for action, (_, yields) in events.EVENTS.items() if yields & kinds]


def rendered(channel: ChannelSettings) -> str:
    """What a delivery's body is rendered for: the channel's ``kind:labels:links``. A pending or dead
    delivery rendered for other settings is never sent."""
    return f"{channel.kind}:{int(bool(channel.labels))}:{int(bool(channel.links))}"


def described(channel: ChannelSettings) -> dict[str, Any]:
    """A channel as ``notify.config`` and the Notifications screen show it: never a URL, path, address or
    secret."""
    return {
        "kind": channel.kind, "host": channel.destination, "proxy": channel.proxy,
        "allow_private_network": channel.allow_private_network, "labels": channel.labels,
        "links": channel.links, "recipients": len(channel.recipients),
    }  # fmt: skip


# -- what every screen shows -----------------------------------------------------------------


async def status(ctx: Context) -> dict[str, Any]:
    """``GET /v1/notifications`` and ``ds notify``: whether notifications are on, the label, the feed's cursor
    (its ``seq``, the chain's ``head``, and ``behind``: when the oldest unread row a rule routes was written),
    each declared channel with its counts and newest attempt, the rules, the active mutes and the banners."""
    notify = ctx.settings.notify
    if notify is None:
        return {"enabled": False, "label": None, "cursor": None, "channels": [], "rules": [], "mutes": [],
                "banners": []}  # fmt: skip
    async with ctx.pool.connection() as conn:
        state = await repo.notify_state(conn, routed(notify))
        found = await repo.channel_states(conn, list(notify.channels))
        mutes = await repo.active_mutes(conn)
    counted = ("pending", "sent_24h", "dead_24h", "suppressed_24h", "last_error", "last_status")
    channels = [
        {"name": name} | described(channel) | {field: found[name][field] for field in counted}
        for name, channel in notify.channels.items()
    ]
    return {
        "enabled": True, "label": notify.label,
        "cursor": {"seq": state["seq"], "head": state["head"], "behind": state["behind"]},
        "channels": channels, "rules": [asdict(rule) for rule in notify.rules], "mutes": mutes,
        "banners": banners(state, found, mutes),
    }  # fmt: skip


def banners(
    state: Mapping[str, Any], channels: Mapping[str, Mapping[str, Any]], mutes: list[repo.Row]
) -> list[str]:
    """What every screen says: each active mute; a channel whose deliveries died since its last sent
    one, or whose head last failed on a secret; and a notifier behind by over ``BEHIND`` (an unread row a
    rule routes, or a due head)."""
    lines = []
    for mute in mutes:
        what = f"to {mute['target']}" if mute["scope"] == "channel" else f"for run {mute['target']}"
        by = f"{mute['created_by']} ({mute['mute_id']})"
        lines.append(f"Notifications {what} are muted until {utc(mute['until'])} by {by}")
    for name, found in channels.items():
        if found["failing"]:
            lines.append(
                f"Notifications to {name} are failing: {found['failing']:,} not delivered since"
                f" {utc(found['failing_since'])}"
            )
        if found["head_error"] == "secret_unreadable":
            lines.append(f"Notifications to {name} cannot be sent: a secret file cannot be read")
    since = min(
        (at for at in (state["behind"], *(f["head_due"] for f in channels.values())) if at is not None),
        default=None,
    )
    if since is not None and state["now"] - since > BEHIND:
        rows = f"audit rows since {utc(since)} are not yet notified"
        lines.append(f"The notifier is behind: {rows}; is the worker running?")
    return lines


# -- the acts: each audited -----------------------------------------------------------------------


async def test(ctx: Context, name: str, who: Principal) -> dict[str, Any]:
    """One attempt of a ``notify.test`` to channel ``name`` now, outside the queue: stored after it as
    ``sent`` or ``dead`` (never pending, so no sender claims it) and audited ``notify.test``; ``{delivery_id,
    state, status, error, latency_ms}``. It uses a sender of its own, never the loop's default executor."""
    notify = on(ctx.settings)
    channel = declared(notify, name)
    async with ctx.pool.connection() as conn:
        installation = await repo.installation_id(conn)
    delivery_id, now = repo.new_delivery_id(), datetime.now(UTC)
    event_id, event = f"test:{delivery_id}", events.checked(Event("notify.test", {"channel": name}))
    auth = ctx.settings.auth
    doc = events.payload(
        event, channel, delivery_id=delivery_id, event_id=event_id, occurred_at=now,
        installation={"id": installation, "label": notify.label}, audit=None, fired={}, bounds={},
        suppressed=0, public_url=auth and auth.public_url,
    )  # fmt: skip
    body = events.body(channel.kind, doc, at=now)
    sender = Sender(notify)
    try:
        outcome = await sender.send(channel, {"delivery_id": delivery_id, "body": body})
    finally:
        sender.close()
    state = "sent" if outcome.state == "sent" else "dead"
    done = {"delivery_id": delivery_id, "state": state, "status": outcome.status, "error": outcome.error}
    async with repo.audited(ctx.pool) as (conn, audit):
        await repo.insert_tested(conn, {
            "delivery_id": delivery_id, "event_id": event_id, "event": event.kind,
            "severity": doc["severity"], "channel": name, "dedup_key": events.dedup(event, delivery_id),
            "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "render": rendered(channel), "state": state, "error": outcome.error, "status": outcome.status,
        })  # fmt: skip
        await audit(who, "notify.test", f"notify:{name}", done)
    return done | {"latency_ms": outcome.latency_ms}


def since(text: str | None, now: datetime) -> datetime:
    """A retry's ``since``: an ISO-8601 time with a zone, not in the future; 72 hours ago without one."""
    if text is None:
        return now - RETRY_SINCE
    try:
        at: datetime | None = datetime.fromisoformat(text)
    except ValueError:
        at = None
    if at is None or at.tzinfo is None or at > now:
        raise Refused(422, "since must be an ISO-8601 time with a zone, not in the future")
    return at


async def retry(ctx: Context, name: str, since_text: str | None, who: Principal) -> tuple[int, datetime]:
    """Send again channel ``name``'s deliveries that died from ``since`` (72 hours ago by default), tests
    and those rendered for other channel settings aside: pending, due now, with no attempt and queued now;
    audited ``notify.retry``. How many, and from when."""
    notify = on(ctx.settings)
    channel = declared(notify, name)
    at = since(since_text, datetime.now(UTC))
    async with repo.audited(ctx.pool) as (conn, audit):
        count = await repo.retry_dead(conn, name, at, rendered(channel))
        await audit(who, "notify.retry", f"notify:{name}", {"since": at.isoformat(), "count": count})
    return count, at


async def mute(ctx: Context, scope: str, target: str, hours: int, reason: str, who: Principal) -> repo.Row:
    """Mute a declared channel, or a run, for 1 to 168 hours, with a reason (a run's is read with its spec's
    key pattern: audit rows reach the SIEM); audited ``notify.mute``. ``{mute_id, until}``. Refused in
    this order: 409, an unknown run's 404, then each 422."""
    notify = on(ctx.settings)
    async with repo.audited(ctx.pool) as (conn, audit):
        of_run = scope == "run" and _RUN.fullmatch(target) is not None
        run = await repo.get_run(conn, target) if of_run else None
        if of_run and run is None:
            raise Refused(404, f"run {target} not found")
        if isinstance(hours, bool) or not 1 <= hours <= MUTE_HOURS:
            raise Refused(422, f"hours must be 1 to {MUTE_HOURS}")
        if not (target in notify.channels if scope == "channel" else run is not None):
            raise Refused(422, "target must be a declared channel for scope channel, or R-… for scope run")
        if not _plain(reason):
            raise Refused(422, f"reason must be 1 to {REASON_CHARS} characters without control characters")
        if run is not None and rawvalues.key_shaped(await ctx.spec(conn, run["spec_id"]), reason):
            raise Refused(422, rawvalues.KEY_SHAPED)
        row = await repo.insert_mute(conn, scope, target, hours, reason, who.actor)
        detail = {"scope": scope, "target": target, "until": row["until"].isoformat(), "reason": reason}
        await audit(who, "notify.mute", row["mute_id"], detail)
    return row


def _plain(text: str) -> bool:
    """Text PostgreSQL stores as ds.note: 1 to 1000 characters, none a control character, all with a UTF-8
    form (a lone surrogate has none)."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return 1 <= len(text) <= REASON_CHARS and printable(text)


async def unmute(ctx: Context, mute_id: str, who: Principal) -> None:
    """End an active mute now; audited ``notify.unmute``."""
    on(ctx.settings)
    async with repo.audited(ctx.pool) as (conn, audit):
        found = await repo.get_mute(conn, mute_id)
        if found is None:
            raise Refused(404, f"mute {mute_id} not found")
        if found["lapsed"] and found["ended_at"] is None:
            raise Refused(409, f"mute {mute_id} lapsed at {utc(found['until'])}")
        if found["ended_at"] is not None or not await repo.end_mute(conn, mute_id, who.actor):
            raise Refused(409, f"mute {mute_id} ended already")
        await audit(who, "notify.unmute", mute_id, {})


# -- a cycle's digest ----------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> str | None:
    if not denominator:
        return None
    value = (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001"), ROUND_HALF_UP)
    return format(value.normalize(), "f")


async def digest(conn: repo.Conn, run: Mapping[str, Any], c: int, bug_rules: list[str]) -> dict[str, Any]:
    """Cycle ``c``'s digest, each query in a savepoint: a timed-out one, or one of a cycle whose purge
    started, leaves its facts null and listed in ``unavailable``."""
    run_id = run["run_id"]
    ended = await repo.get_cycle(conn, run_id, c)
    before = await repo.get_cycle(conn, run_id, c - 1) if c > 1 else None
    source = run["params"].get("keys_source")
    sampled, purging = source == "hash_sample" and c > 1, ended["purging_at"] is not None  # type: ignore[index]
    previous_kept = c == 1 or (before is not None and before["purging_at"] is None)
    unavailable: list[str] = []

    async def fact(names: tuple[str, ...], query: Any, *, kept: bool = True) -> Any:
        if purging or not kept:
            unavailable.extend(names)
            return None
        try:
            async with conn.transaction():
                return await query()
        except psycopg.errors.QueryCanceled:
            unavailable.extend(names)
            return None

    k = ended["counters"]  # type: ignore[index]
    causes = await fact(
        ("new_root_causes", "gone_root_causes"), lambda: repo.root_cause_changes(conn, run_id, c)
    )
    new = gone = baseline = None
    if causes is not None:
        baseline = causes["baseline_from"]
        new = causes["new"] if c == 1 or baseline is not None else None
        gone = causes["gone"] if previous_kept else None
        unavailable += [n for n, v in (("new_root_causes", new), ("gone_root_causes", gone)) if v is None]

    async def read_all(row: Mapping[str, Any] | None, cycle: int) -> bool:
        """Whether a table scan read every key in the cycle: its scan ran to its end, uncut by maxKeys."""
        scan = await repo.get_scan(conn, run_id, cycle) if source == "table_scan" else None
        return scan is not None and scan["done"] and row is not None and row["keys_cut"] is None

    async def discrepant() -> repo.Row:
        before_all = c > 1 and await read_all(before, c - 1)
        return await repo.discrepant_changes(
            conn, run_id, c, read_all_before=before_all, read_all=await read_all(ended, c)
        )

    changes = None
    if not sampled:
        changes = await fact(("newly_discrepant", "no_longer_discrepant"), discrepant, kept=previous_kept)
    critical = await fact(
        ("unexplained_critical",), lambda: repo.unexplained_criticals(conn, run_id, c, bug_rules)
    )
    levels = {
        "mismatch_rate": None if k["mismatch_rate"] == "unavailable" else k["mismatch_rate"],
        "indeterminate_rate": _rate(k["indeterminate"], k["total"]),
        "coverage": None if k["coverage"] == "unavailable" else k["coverage"],
        "unexplained_critical": critical,
    }
    previous: dict[str, Any] = {}
    for name in levels:
        try:
            async with conn.transaction():
                found = await repo.previous_fact(conn, run_id, c, name, bug_rules)
        except psycopg.errors.QueryCanceled:  # no baseline known: the level is not judged
            found, levels[name] = None, None
            unavailable.append(name)
        if found is not None and name == "unexplained_critical":
            found["value"] = int(found["value"])
        previous[name] = found
    return {
        "levels": levels, "previous": previous, "new_root_causes": new, "gone_root_causes": gone,
        "baseline_from": baseline, "newly_discrepant": changes and changes["newly"],
        "no_longer_discrepant": changes and changes["no_longer"], "between_samples": sampled,
        "unavailable": list(dict.fromkeys(unavailable)),
    }  # fmt: skip


async def explain(ctx: Context, run_id: str, cycle: int | None) -> dict[str, Any]:
    """``ds notify explain``: cycle ``cycle`` of the run (its latest ended one by default) as a feed pass
    would see it now: its digest, whether its purge started, and each rule's verdict on its ``cycle.ended``.
    Writes nothing."""
    notify = on(ctx.settings)
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        if cycle is None:
            live = await repo.get_cycle(conn, run_id, run["cycle"]) is None
            cycle = max(run["cycle"] - 1, 1) if live else run["cycle"]
        if not 1 <= cycle <= run["cycle"]:
            raise LookupError(f"run {run_id} has no cycle {cycle}")
        ended = await repo.get_cycle(conn, run_id, cycle)
        if ended is None:
            raise LookupError(f"cycle {cycle} of run {run_id} has not ended")
        if not ended["counters"]:
            raise LookupError(f"cycle {cycle} of run {run_id} ended without counters: it is never notified")
        bug = approvals.bug_rules(await ctx.spec(conn, run["spec_id"]))
        counters = {name: ended["counters"].get(name) for name in events.COUNTERS}
        facts = {"cycle": cycle, "counters": counters} | await digest(conn, run, cycle, bug)
    event = replace(events.checked(Event("cycle.ended", facts)), spec=run["spec_name"], mode=run["mode"])
    verdicts = {}
    for rule in notify.rules:
        if "cycle.ended" not in rule.events:
            verdicts[rule.name] = "takes no cycle.ended"
        elif not events.routes(rule, event):
            verdicts[rule.name] = "does not take this run (specs or modes)"
        elif (fired := events.fires(rule, event)) is None:
            verdicts[rule.name] = "does not fire"
        else:
            parts = ("held", "crossed", "recovered")
            why = [f"{name} {part}" for part in parts for name in getattr(fired, part)]
            verdicts[rule.name] = "fires" + (f": {', '.join(why)}" if why else "")
    return {
        "run_id": run_id, "spec_name": run["spec_name"], "mode": run["mode"], "cycle": cycle,
        "purged": ended["purging_at"] is not None, "facts": event.facts, "rules": verdicts,
    }  # fmt: skip
