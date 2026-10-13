"""The notifier: the audit hash chain is the outbox.

Each fact worth a notification is already an audit row written last in the transaction that makes it true,
and 009 chains every row under the DSAU lock in commit order. The feed follows the chain from a durable
cursor: in one transaction per pass it reads the head, then up to ``BATCH`` fed rows after the cursor, turns
each (in a savepoint) into typed events (``core.notify.EVENTS``), matches the declared rules, stores one
rendered delivery per event and channel (unique, so a crash recomputes the same rows) and moves the cursor
with them: each row is turned into deliveries exactly once. Rows older than ``max_age_hours`` are skipped in
one step. A row that fails is skipped and counted (``notify.skipped``); a lost connection moves nothing.

A task per channel sends its head, the oldest pending delivery, holding no pooled connection
(``connectors.notify``): a head backing off holds its channel, so order holds. Retries back off with full
jitter up to ``max_attempts``; a delivery that dies is audited ``notify.dead``, hourly per channel at most.
Every attempt sends the stored bytes under the delivery's id; one rendered for another kind, labels or links
than the channel now has dies ``channel_changed``. Hourly housekeeping sends ``notify.daily`` and deletes
old deliveries and mutes. Only the worker imports the notifier; it writes only its three tables and its audit
rows, with at most two pooled connections, and every step catches what it meets and goes on, but its own
cancellation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from deltasleuth import approvals, notifications
from deltasleuth.auth.principal import WORKER
from deltasleuth.config import NotifySettings
from deltasleuth.connectors.notify import Outcome, Sender
from deltasleuth.connectors.retry import retry_delay
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core import notify as events
from deltasleuth.core.notify import Event, Fired, Seen, Unsafe
from deltasleuth.core.types import CompiledSpec, RetryPolicy
from deltasleuth.runner.cycle import failure
from deltasleuth.security import rawvalues
from deltasleuth.store import repo

BATCH = 200  # rows a pass turns into deliveries at most
PASS_S = 60  # and seconds
LEASE_GRACE_S = 15  # a claimed head is due again this long after its send must have ended
HOUSEKEEP_EVERY_S = 3600
CLOSE_S = 10  # close waits this long for a cancelled task
PRUNE_BATCH = 10_000
POLICY = RetryPolicy(initial_s=60, multiplier=3, max_s=3600)  # 8 attempts span about two hours
QUIET = ("notify.dead", "notify.skipped")  # a dead delivery of these is only logged: no loop
ADDED = ("programmes", "gates", "causes")  # rule keys newer than the routing token
log = logging.getLogger(__name__)


def _canonical(notify: NotifySettings) -> dict[str, Any]:
    """The settings as the routing token is computed over, and their rules as ``notify.config`` records
    them: a rule's keys added since the token was first computed (``ADDED``) are left out while unset, so
    a build that adds one leaves the token of unchanged settings alone."""
    document = asdict(notify)
    document["rules"] = [
        {key: value for key, value in rule.items() if value is not None or key not in ADDED}
        for rule in document["rules"]
    ]
    return document


def _config(notify: NotifySettings | None, routing: str | None) -> dict[str, Any]:
    """``notify.config``'s detail: never a URL, path, address or secret; ``routing`` tells a change."""
    if notify is None:
        return {"routing": None}
    return {
        "label": notify.label,
        "channels": {name: notifications.described(c) for name, c in notify.channels.items()},
        "rules": _canonical(notify)["rules"],
        "routing": routing,
    }


async def record_config(ctx: Context) -> None:
    """Audit ``notify.config`` when the routing token (keyed, so a changed address shows and none can be
    guessed back) differs from the newest such row's; ``{routing: null}`` once ``notify`` is removed. Never
    raises: a failure is logged."""
    notify = ctx.settings.notify
    try:
        routing = notify and ctx.tokeniser.token(
            "notify_routing", jsonio.dumps(_canonical(notify), sort_keys=True)
        )
        async with repo.audited(ctx.pool) as (conn, audit):
            last = await repo.last_audit(conn, "notify.config")
            if (last and last["detail"].get("routing")) != routing:
                await audit(WORKER, "notify.config", "notify", _config(notify, routing))
    except Exception as exc:
        _cancelled(exc)
        log.warning("notify.config not recorded: %s", failure(exc))


def _unlabelled(spec: CompiledSpec) -> str | None:
    """Why the spec's name is never sent: its key pattern cannot tell a key, or it is key-shaped."""
    try:
        search = rawvalues.searcher(spec)
    except rawvalues.EmptyPattern:
        return "no_key_pattern"
    return "key_shaped" if search is not None and search(spec.name) else None


def _cancelled(exc: BaseException) -> None:
    """CancelledError when the running task is being cancelled: psycopg can end a cancelled savepoint with
    another error (``OutOfOrderTransactionNesting``), which no step may log and go past."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError from exc


async def _ended(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel ``tasks`` and wait ``CLOSE_S`` at most for them: what still runs then is logged and left."""
    for task in tasks:
        task.cancel()
    if tasks and (left := (await asyncio.wait(tasks, timeout=CLOSE_S))[1]):
        log.warning("notifier: %d task(s) still running %d s after their cancel", len(left), CLOSE_S)


class Notifier:
    """The worker's notifier: ``run`` until cancelled, then ``close``."""

    def __init__(self, ctx: Context) -> None:
        self.ctx, self.notify = ctx, ctx.settings.notify
        self.sender = Sender(self.notify) if self.notify is not None else None
        self.pooled = asyncio.Semaphore(2)  # pooled connections at most, none during a send
        self.sending: dict[str, asyncio.Task[None]] = {}
        self.housekept_at = -float("inf")  # monotonic
        self._specs: dict[int, CompiledSpec] = {}
        self._installation: str | None = None

    async def run(self) -> None:
        """Record the configuration, then every poll a feed pass, the due channels' sends and, hourly,
        housekeeping; each step logs what it meets and goes on."""
        await record_config(self.ctx)
        if self.notify is None:
            return
        while True:
            for step in (self.feed, self.scan, self.housekeep):
                try:
                    await step()
                except Exception as exc:
                    _cancelled(exc)
                    log.warning("notifier %s failed: %s", step.__name__, failure(exc))
            await asyncio.sleep(self.ctx.settings.worker.poll_seconds)

    async def close(self, running: asyncio.Task[None] | None = None) -> None:
        """Cancel ``running`` (the task of ``run``), then the send tasks, and shut the sender's threads
        down: never waiting more than ``CLOSE_S`` for either."""
        await _ended([running] if running is not None else [])
        await _ended(list(self.sending.values()))
        if self.sender is not None:
            self.sender.close()

    @asynccontextmanager
    async def _audited(self) -> AsyncIterator[tuple[repo.Conn, repo.Audit]]:
        async with self.pooled, repo.audited(self.ctx.pool) as (conn, audit):
            yield conn, audit

    async def _spec(self, conn: repo.Conn, spec_id: int) -> CompiledSpec:
        if spec_id not in self._specs:
            self._specs[spec_id] = await self.ctx.spec(conn, spec_id)
        return self._specs[spec_id]

    # -- the feed ----------------------------------------------------------------------------------

    async def feed(self) -> None:
        """One pass: the fed rows after the cursor, up to the head read first, turned into deliveries."""
        notify = self.notify
        assert notify is not None
        actions = notifications.routed(notify)
        skipped: dict[str, list[int]] = {}
        async with self._audited() as (conn, audit):
            await conn.execute("SET LOCAL statement_timeout = '30s'")
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            cursor = await repo.notify_cursor(conn)
            seq, now = cursor["seq"], cursor["now"]
            head = await repo.chain_head(conn)
            rows = await repo.audit_chain(conn, after=seq, upto=head, actions=actions, limit=BATCH)
            stale, oldest = None, now - timedelta(hours=notify.max_age_hours)
            if rows and rows[0]["audit_at"] < oldest:  # an outage, or notify enabled again
                seq = await repo.chain_before(conn, rows[0]["seq"], head, oldest)
                stale = {"reason": "stale", "from_seq": rows[0]["seq"], "to_seq": seq}
                rows = await repo.audit_chain(conn, after=seq, upto=head, actions=actions, limit=BATCH)
            self._installation = self._installation or await repo.installation_id(conn)
            started, last, whole = time.monotonic(), seq, len(rows) < BATCH
            for chained in rows:
                if time.monotonic() - started >= PASS_S:
                    whole = False
                    break
                row = {
                    "seq": chained["seq"], "action": chained["action"], "object": chained["object"],
                    "detail": chained["audit_detail"], "at": chained["audit_at"], "hash": chained["hash"],
                }  # fmt: skip
                try:
                    async with conn.transaction():
                        await self._row(conn, row, now)
                except Exception as exc:
                    _cancelled(exc)
                    if repo.lost(conn, exc):  # the pass rolls back
                        raise
                    reason = "unsafe" if isinstance(exc, Unsafe) else "error"
                    why = str(exc) if isinstance(exc, Unsafe) else failure(exc)
                    log.warning(
                        "audit row %s (%s) not notified (%s): %s", row["seq"], row["action"], reason, why
                    )
                    if not row["action"].startswith("notify."):  # no skip begets another
                        skipped.setdefault(reason, []).append(row["seq"])
                last = row["seq"]
            await repo.move_cursor(conn, head if whole else last)
            if stale is not None:
                await audit(WORKER, "notify.skipped", "notify", stale)
            for reason, seqs in skipped.items():
                await audit(
                    WORKER,
                    "notify.skipped",
                    "notify",
                    {"reason": reason, "rows": len(seqs), "seqs": seqs[:20]},
                )

    async def _row(self, conn: repo.Conn, row: Mapping[str, Any], now: datetime) -> None:
        project, _ = events.EVENTS[row["action"]]
        seen = await self._seen(conn, row)
        for event in project(row, seen):
            key, event_id = row["seq"], f"seq:{row['seq']}:{event.kind}"
            await self._route(conn, event, key=key, event_id=event_id, at=row["at"], now=now, audit=row)

    async def _seen(self, conn: repo.Conn, row: Mapping[str, Any]) -> Seen:
        """What a row's projector reads: the run it names as of now (a labelling's row names its run in its
        detail), the cycle it concerns, that cycle's end and draw, whether the run
        has a ``cancelling`` row; or the approval it names; or the pair whose readiness changed, and why its
        name is never sent (its newest version tells)."""
        obj = row["object"]
        if obj.startswith("V-"):
            obj = row["detail"]["run_id"]
        if obj.startswith("A-"):
            approval = await repo.get_approval(conn, obj)
            return Seen(spec=approval and approval["spec_name"], approval=approval)
        if obj.startswith("readiness:"):
            name = obj.removeprefix("readiness:")
            stored = await repo.newest_spec(conn, name)  # none: a name only the settings list
            return Seen(
                spec=name, unlabelled=stored and _unlabelled(await self._spec(conn, stored["spec_id"]))
            )
        if not obj.startswith("R-"):
            return Seen()
        run = await repo.get_run(conn, obj)
        if run is None:
            raise LookupError(f"run {obj} not found")
        spec = await self._spec(conn, run["spec_id"])
        cycle = events.concerned(row)
        cycle = await repo.cycle_at(conn, obj, row["seq"]) if cycle is None else cycle
        block = {
            "run_id": obj, "spec_id": run["spec_id"], "spec_name": run["spec_name"],
            "spec_version": run["spec_version"], "mode": run["mode"],
            "now": {
                "cycle": run["cycle"], "status": run["status"],
                "licence": (run["params"].get("licence") or {}).get("state"),
                "approval": (await approvals.run_approval(conn, run))["state"],
            },
        }  # fmt: skip
        ended, draw = await repo.get_cycle(conn, obj, cycle), await repo.get_draw(conn, obj, cycle)
        cancelling = await repo.last_audit(conn, "cancelling", obj=obj) is not None
        return Seen(block, run["spec_name"], run["mode"], _unlabelled(spec), cycle, ended, draw, cancelling)

    async def _route(
        self,
        conn: repo.Conn,
        event: Event,
        *,
        key: int | str,
        event_id: str,
        at: datetime,
        now: datetime,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        """Store a delivery of ``event`` to each channel of the rules firing for it, one per channel listing
        them all (a ``notify.dead`` never to the channel it names)."""
        notify = self.notify
        assert notify is not None
        rules = [rule for rule in notify.rules if events.routes(rule, event)]
        if not rules:
            return
        if event.kind == "cycle.ended":
            event = replace(event, facts=event.facts | await self._digest(conn, event))
        event = events.checked(event)
        by_channel: dict[str, dict[str, tuple[Any, Fired]]] = {}
        for rule in rules:
            if (fired := events.fires(rule, event)) is None:
                continue
            for channel in rule.channels:
                if not (event.kind == "notify.dead" and channel == event.facts["channel"]):
                    by_channel.setdefault(channel, {})[rule.name] = (rule, fired)
        seq = audit and audit["seq"]
        for channel, fired_rules in by_channel.items():
            await self._insert(
                conn,
                event,
                channel,
                fired_rules,
                key=key,
                event_id=event_id,
                seq=seq,
                at=at,
                now=now,
                audit=audit,
            )

    async def _insert(
        self,
        conn: repo.Conn,
        event: Event,
        name: str,
        fired_rules: Mapping[str, tuple[Any, Fired]],
        *,
        key: int | str,
        event_id: str,
        seq: int | None,
        at: datetime,
        now: datetime,
        audit: Mapping[str, Any] | None,
        test: bool = False,
    ) -> str:
        """Render and store the delivery of ``event`` to channel ``name``: suppressed when muted, a duplicate
        or, but an alert, over the channel's hourly rate (a test never is); its id."""
        notify, channel = self.notify, self.notify.channels[name]  # type: ignore[union-attr]
        fired = {rule: f for rule, (_, f) in fired_rules.items()}
        bounds: dict[str, str] = {}
        for rule, f in fired_rules.values():
            for condition in (*f.crossed, *f.recovered):
                bounds.setdefault(condition, str(getattr(rule.when, condition)))
        dedup_key = events.dedup(event, key)
        run_id = (event.run or {}).get("run_id")
        held = any(f.held or f.crossed for f in fired.values())
        hours, among = notify.dedup_hours, events.superseded(event)  # type: ignore[union-attr]
        found = await repo.held_back(conn, name, dedup_key, hours, among=among)
        why = mute_id = None
        if not test:
            if mute_id := await repo.active_mute(conn, name, run_id):
                why = "muted"
            elif found["duplicate"]:
                why = "duplicate"
            elif events.severity(event, held) != "alert" and found["last_hour"] >= notify.rate_per_hour:  # type: ignore[union-attr]
                why = "rate_limited"
        delivery_id = repo.new_delivery_id()
        auth = self.ctx.settings.auth
        doc = events.payload(
            event, channel, delivery_id=delivery_id, event_id=event_id, occurred_at=at,
            installation={"id": self._installation, "label": notify.label},  # type: ignore[union-attr, dict-item]
            audit=audit, fired=fired, bounds=bounds, suppressed=0 if why else found["suppressed"],
            public_url=auth and auth.public_url,
        )  # fmt: skip
        text = events.body(channel.kind, doc, at=now)
        await repo.insert_delivery(conn, {
            "delivery_id": delivery_id, "event_id": event_id, "seq": seq, "event": event.kind,
            "severity": doc["severity"], "channel": name, "rules": list(fired), "dedup_key": dedup_key,
            "run_id": run_id, "body": text, "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "render": notifications.rendered(channel), "why": why, "mute_id": mute_id,
        })  # fmt: skip
        return delivery_id

    async def _digest(self, conn: repo.Conn, event: Event) -> dict[str, Any]:
        """The cycle's digest (``notifications.digest``)."""
        run = await repo.get_run(conn, event.run["run_id"])  # type: ignore[index]
        bug = approvals.bug_rules(await self._spec(conn, run["spec_id"]))  # type: ignore[index]
        return await notifications.digest(conn, run, event.facts["cycle"], bug)  # type: ignore[arg-type]

    # -- delivery ----------------------------------------------------------------------------------

    async def scan(self) -> None:
        """Start a send task for each due channel without one."""
        assert self.notify is not None
        async with self.pooled, self.ctx.pool.connection() as conn:
            due = await repo.due_channels(conn, self.notify.max_age_hours)
        for name in due:
            if name not in self.sending:
                self.sending[name] = asyncio.create_task(self._send(name))

    async def _send(self, name: str) -> None:
        """Send the channel's heads in order while one is due, each recorded before the next."""
        try:
            while (head := await self._claim(name)) is not None:
                outcome = await self.sender.send(self.notify.channels[name], head)  # type: ignore[union-attr]
                await self._record(name, head, outcome)
        except Exception as exc:
            _cancelled(exc)
            log.warning("notifications to %s: %s", name, failure(exc))
        finally:
            self.sending.pop(name, None)

    async def _claim(self, name: str) -> repo.Row | None:
        """The channel's due head, claimed for one attempt (a lease: due again once its send must have
        ended); a head too old dies ``expired``, one of an undeclared channel ``channel_removed``, one
        rendered for another kind, labels or links ``channel_changed``, one under an active mute is
        suppressed. None: no head is due."""
        notify = self.notify
        assert notify is not None
        while True:
            async with self._audited() as (conn, audit):
                head = await repo.notify_head(conn, name, notify.max_age_hours)
                if head is None or not head["due"]:
                    return None
                channel = notify.channels.get(name)
                death = (
                    "expired" if head["expired"]
                    else "channel_removed" if channel is None
                    else "channel_changed" if head["render"] != notifications.rendered(channel)
                    else None
                )  # fmt: skip
                if death is not None:
                    await self._dead(conn, audit, name, head, death)
                    continue
                if mute_id := await repo.active_mute(conn, name, head["run_id"]):
                    await repo.end_delivery(conn, head["delivery_id"], "suppressed", mute_id=mute_id)
                    continue
                return await repo.claim_delivery(
                    conn, head["delivery_id"], notify.timeout_seconds + LEASE_GRACE_S
                )

    async def _record(self, name: str, head: repo.Row, outcome: Outcome) -> None:
        """Record an attempt: sent, due again after a backoff (``Retry-After`` a floor), or dead."""
        notify, attempts, delivery_id = self.notify, head["attempts"], head["delivery_id"]
        assert notify is not None
        async with self._audited() as (conn, audit):
            if outcome.state == "sent":
                await repo.end_delivery(conn, delivery_id, "sent", attempts=attempts, error=outcome.error,
                                        status=outcome.status)  # fmt: skip
            elif outcome.state == "retry" and attempts < notify.max_attempts:
                rng = random.Random(f"{delivery_id}:{attempts}")  # noqa: S311 - jitter only spreads retries
                delay = retry_delay(attempts, POLICY, rng, outcome.retry_after)
                await repo.end_delivery(conn, delivery_id, "pending", attempts=attempts, error=outcome.error,
                                        status=outcome.status, due_in=delay)  # fmt: skip
            else:  # a retried code at max_attempts: one poisoned delivery cannot hold its channel
                error = "retries_exhausted" if outcome.state == "retry" else outcome.error
                await self._dead(conn, audit, name, head, error, status=outcome.status, attempts=attempts)  # type: ignore[arg-type]

    async def _dead(
        self,
        conn: repo.Conn,
        audit: repo.Audit,
        name: str,
        head: Mapping[str, Any],
        error: str,
        *,
        status: int | None = None,
        attempts: int | None = None,
    ) -> None:
        """Mark the head dead, audited ``notify.dead`` unless the channel's newest such row is under an hour
        old; a dead ``notify.dead`` or ``notify.skipped`` is only logged."""
        if not await repo.end_delivery(
            conn, head["delivery_id"], "dead", attempts=attempts, error=error, status=status
        ):
            return
        log.warning("notification %s (%s) to %s is dead: %s", head["delivery_id"], head["event"], name, error)
        if head["event"] in QUIET:
            return
        found = await repo.deaths(conn, name, head["delivery_id"])
        if found["recent"]:
            return
        detail = {
            "delivery_id": head["delivery_id"], "event": head["event"], "attempts": head["attempts"],
            "error": error, "status": status if status is not None else head.get("last_status"),
            "body_sha256": head["body_sha256"], "dead_since_last": found["since"],
        }  # fmt: skip
        await audit(WORKER, "notify.dead", f"notify:{name}", detail)

    # -- housekeeping ------------------------------------------------------------------------------

    async def housekeep(self) -> None:
        """Hourly: ``notify.daily`` once a UTC day from ``daily_hour`` when a rule routes it, and the
        deliveries made, and mutes ended or lapsed, before ``retention.standard_days`` deleted."""
        notify = self.notify
        assert notify is not None
        if time.monotonic() - self.housekept_at < HOUSEKEEP_EVERY_S:
            return
        self.housekept_at = time.monotonic()
        if any("notify.daily" in rule.events for rule in notify.rules):
            async with self._audited() as (conn, _):
                counts = await repo.daily_counts(conn, notifications.routed(notify))
                now = counts.pop("now")
                if now.astimezone(UTC).hour >= notify.daily_hour:
                    self._installation = self._installation or await repo.installation_id(conn)
                    day = now.astimezone(UTC).date().isoformat()
                    zero = {
                        "pending": 0,
                        "sent_24h": 0,
                        "dead_24h": 0,
                        "suppressed_24h": 0,
                        "head_age_s": None,
                    }
                    counts["channels"] = {name: zero for name in notify.channels} | counts["channels"]
                    paused: dict[str, int] = {}
                    for reason, n in counts["paused"].items():
                        reason = (
                            reason if reason in (*events.PAUSES, events.DRAW_PAUSE, "manual") else "other"
                        )
                        paused[reason] = paused.get(reason, 0) + n
                    event = Event("notify.daily", counts | {"paused": paused})
                    await self._route(conn, event, key=day, event_id=f"daily:{day}", at=now, now=now)
        days = self.ctx.settings.retention.standard_days
        while True:
            async with self._audited() as (conn, _):
                if await repo.prune_notify(conn, days, PRUNE_BATCH) < PRUNE_BATCH:
                    break
