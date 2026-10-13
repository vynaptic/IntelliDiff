"""Schedules for the runner's tests (docs/mvp/SCHEDULES.md §15): a schedule declared as a person declares
one, then anchored by SQL so that a chosen minute is due; the clock is the database's, or an instant a test
injects into ``fire``."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from deltasleuth import schedules
from deltasleuth.auth.principal import Principal
from deltasleuth.core import cron
from deltasleuth.runner import schedule
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import Harness, spec_text

DAY, HOUR, MINUTE = timedelta(days=1), timedelta(hours=1), timedelta(minutes=1)
# The unit spec without values in clear: under SSO its runs need no four-eyes approval.
PLAIN = spec_text().replace('  clear: ["$.status", "$.tags[*]", "$.id"]\n', "")


def daily(at: datetime) -> str:
    """The cron of ``at``'s minute, every day (UTC)."""
    return f"{at.minute} {at.hour} * * *"


def hourly(at: datetime) -> str:
    return f"{at.minute} * * * *"


async def minute(h: Harness) -> datetime:
    """The database's now, at its minute: a due time whose window is open as the test runs."""
    async with h.ctx.pool.connection() as conn:
        return (await repo.count_schedules(conn))["now"].replace(second=0, microsecond=0)


async def anchored(h: Harness, schedule_id: str, anchor: datetime) -> repo.Row:
    """The schedule moved by SQL to have settled ``anchor``: every time of its cron after it is due."""
    async with h.ctx.pool.connection() as conn:
        row = await repo.get_schedule(conn, schedule_id)
        assert row is not None
        due = cron.after(row["cron"], row["time_zone"], anchor, until=row["ends_at"])
        await conn.execute(
            "UPDATE ds.schedules SET anchor_at = %s, next_due = %s WHERE schedule_id = %s",
            (anchor, due, schedule_id),
        )
        return await repo.get_schedule(conn, schedule_id)  # type: ignore[return-value]


async def scheduled(
    h: Harness,
    spec_id: int | None,
    due: datetime,
    *,
    who: Principal = TESTER,
    back: timedelta = MINUTE,
    **body: Any,
) -> repo.Row:
    """A schedule (``nightly``) of the spec, daily at ``due``'s minute in UTC unless ``body`` says, declared
    by ``who`` and then anchored ``back`` before ``due``: ``due`` is its newest due time."""
    declared = {"name": "nightly", "run": {"mode": "batch"}, "cron": daily(due), "time_zone": "UTC"}
    declared |= {"spec_id": spec_id} if spec_id is not None else {}
    row, _ = await schedules.create(h.ctx, declared | body, who)
    return await anchored(h, row["schedule_id"], due - back)


async def shown(h: Harness, schedule_id: str) -> repo.Row:
    async with h.ctx.pool.connection() as conn:
        row = await repo.get_schedule(conn, schedule_id)
    assert row is not None
    return row


async def fire(
    h: Harness, row: repo.Row, now: datetime | None = None, **options: Any
) -> schedule.Retry | None:
    return await schedule.fire(h.ctx, row["schedule_id"], now=now, **options)


async def audited(h: Harness, obj: str, *actions: str) -> list[tuple[str, str, dict[str, Any]]]:
    """The audit rows of ``obj`` (of ``actions`` only, if given) in order: actor, action and the detail its
    writer gave."""
    async with h.ctx.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT actor, action, detail FROM ds.audit_log
               WHERE object = %s AND (%s::text[] = '{}' OR action = ANY(%s)) ORDER BY id""",
            (obj, list(actions), list(actions)),
        )
        rows = await cur.fetchall()
    for _, _, detail in rows:  # what every row of the CLI's principal carries besides (AUTH §4.2)
        for name in ("asserted", "os_account"):
            detail.pop(name, None)
    return [tuple(found) for found in rows]  # type: ignore[misc]


async def missed(h: Harness, row: repo.Row) -> list[dict[str, Any]]:
    """The schedule's ``schedule.missed`` details, in order."""
    return [detail for _, _, detail in await audited(h, row["schedule_id"], "schedule.missed")]


async def started(h: Harness, row: repo.Row) -> list[repo.Row]:
    """The runs the schedule started, oldest first."""
    async with h.ctx.pool.connection() as conn:
        return (await repo.pages(repo.list_runs, conn, schedule_id=row["schedule_id"]))[::-1]


async def ended(h: Harness, run_id: str, status: str = "completed") -> None:
    """The run ended by SQL, as its worker would have ended it: no longer live."""
    async with h.ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE ds.runs SET status = %s, finished_at = now() WHERE run_id = %s", (status, run_id)
        )
