"""The worker's sweep: each poll it gives every job (``runner.jobs``) its turn, and no job's failure ends it.

A sweep calls each job's ``before``, then takes one pooled connection, reads the runs running or being
cancelled, and has each job that reads ``look`` on that connection inside a savepoint of its own; it gives
the connection back and has each job ``act``. A job's failure at any of its turns is logged by its type and
where it was raised, and the sweep goes on to the next job; only a store that cannot be reached while the
sweep reads fails the sweep, which tries again a poll later. The jobs take their turns in this order:
cancellations, the audit anchor, the schema guard, key censuses, draws, table pre-checks, schedules, scans,
the licence watch, purge, stop conditions, cycle ends, the notifier, readiness, the sweep record and the
readers of ended runs. Cancellations look before cycle ends, so a run cancelled since the last sweep ends in
this one, and the stop conditions act before them, so a run stopped by its last group ends stopped; the
sweep record looks last, once the rest has been read.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any

import psycopg

from deltasleuth.context import Context
from deltasleuth.runner.cycle import located
from deltasleuth.runner.jobs import (
    AuditAnchor,
    Cancellations,
    Censuses,
    CycleEnds,
    Draws,
    Job,
    LicenceWatch,
    Notifications,
    Prechecks,
    Purge,
    Readers,
    Readiness,
    Scans,
    Schedules,
    SchemaGuard,
    Statistics,
    StopFeed,
    SweepRecord,
)
from deltasleuth.runner.slots import Slots
from deltasleuth.store import repo

log = logging.getLogger("deltasleuth.runner.worker")  # the worker's lines, whichever of its parts writes them


class Supervisor:
    """The jobs of one worker, by name, and the sweep that gives each its turn."""

    def __init__(self, ctx: Context, slots: Slots, schedules: bool = True) -> None:
        self.ctx = ctx
        self.cancellations = Cancellations(ctx)
        self.anchor = AuditAnchor(ctx)
        self.guard = SchemaGuard(ctx)
        self.censuses = Censuses(ctx, self.guard, slots.counting)
        self.draws = Draws(ctx, self.guard)
        self.prechecks = Prechecks(ctx)
        self.schedules = Schedules(ctx, schedules)  # it fires schedules: never ds run's worker
        self.scans = Scans(ctx)
        self.licence = LicenceWatch(ctx)
        self.purge = Purge(ctx)
        self.stops = StopFeed(ctx)
        self.statistics = Statistics(ctx)
        self.ends = CycleEnds(ctx)
        self.notifier = Notifications(ctx)
        self.readiness = Readiness(ctx)
        self.record = SweepRecord(ctx, slots.name)
        self.readers = Readers(ctx, slots)
        self.jobs: list[Job] = [
            self.cancellations,
            self.anchor,
            self.guard,
            self.censuses,
            self.draws,
            self.prechecks,
            self.schedules,
            self.scans,
            self.licence,
            self.purge,
            self.stops,
            self.statistics,
            self.ends,
            self.notifier,
            self.readiness,
            self.record,
            self.readers,
        ]
        self.looking = [job for job in self.jobs if type(job).look is not Job.look]  # the jobs that read

    async def sweep(self) -> None:
        """One sweep: every job's turns, in order."""
        for job in self.jobs:
            await self._turn(job, job.before())
        read_at = time.monotonic()
        for job in self.jobs:
            job.read_at = read_at
        async with self.ctx.pool.connection() as conn:
            active = [
                run
                for status in ("cancelling", "running")
                for run in await repo.pages(repo.list_runs, conn, status=status)
            ]
            for job in self.looking:
                await self._look(job, conn, active)
        for job in self.jobs:
            await self._turn(job, job.act(active))

    async def close(self) -> None:
        """Cancel every job's tasks and wait for them, then have each job release what it holds."""
        tasks = [task for job in self.jobs for task in job.tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self.jobs:
            await job.close()

    async def _look(self, job: Job, conn: repo.Conn, active: list[repo.Row]) -> None:
        try:
            async with conn.transaction():
                await job.look(conn, active)
        except psycopg.OperationalError:
            raise  # the store is unreachable: the sweep tries again
        except Exception as exc:
            if job.unread:
                log.warning("%s: %s", job.unread, located(exc))
            else:
                job.complain(exc)

    async def _turn(self, job: Job, step: Coroutine[Any, Any, None]) -> None:
        try:
            await step
        except Exception as exc:
            job.complain(exc)
