"""The worker processes of one database (``ds.workers``): the one that holds the worker lock and sweeps, and
any ``ds worker --slots-only`` beside it, whose slots only lease, read, judge and save keys.

Each registers as it starts to process keys and renews its registration every ``BEAT_S``; one that missed
its beats for ``EXPIRES_S`` (a killed process) drops out, and a stopped one leaves at once. From the live
ones each process takes its share of every target's budget (``Gates.share``): the rate divided by their
count, and the in-flight cap shared out in their order (the sweep's holder first, then by when each
registered), so that the parts sum to the target's own. Each beat also records the pauses this process met
(a target's Retry-After, an open breaker) and takes in those the others recorded, so no worker keeps sending
to a target that asked them to wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import socket

import psycopg

from deltasleuth.context import Context, ContextError
from deltasleuth.store import repo

BEAT_S = 1.0  # a worker renews its registration, and shares the pauses, this often
EXPIRES_S = 10.0  # a worker whose registration is older has stopped
log = logging.getLogger("deltasleuth.runner.worker")  # the worker's lines, whichever of its parts writes them


async def check_evidence(ctx: Context) -> None:
    """ContextError when the store names evidence that this process's evidence folder does not hold: a
    worker beside others writes to and reads from the one folder they all use."""
    async with ctx.pool.connection() as conn:
        address = await repo.stored_evidence(conn)
    if address is not None and ctx.evidence.age(address) is None:
        raise ContextError(
            f"the evidence folder {ctx.settings.evidence_dir} does not hold the evidence the store names: a"
            " worker beside others needs the folder they use (evidence_dir, shared with them)"
        )


class Roster:
    """This process's registration among the workers, while it processes keys: ``sweep`` as the holder of
    the worker lock."""

    def __init__(self, ctx: Context, *, sweep: bool) -> None:
        self.ctx, self.sweep = ctx, sweep
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"

    async def beat(self) -> None:
        """Register or renew, record the pauses this process met, and take this process's share and the
        pauses every worker recorded."""
        gates = self.ctx.gates
        untold = gates.untold()
        async with self.ctx.pool.connection() as conn:
            live = await repo.beat_worker(conn, self.worker_id, self.sweep, EXPIRES_S)
            if untold:
                await repo.pause_targets(conn, untold)
            paused = await repo.target_pauses(conn)
        count = len(live)
        if count != gates.count:
            log.info("%d worker(s) process keys: this one takes its share of each target's budget", count)
        gates.share(count, live.index(self.worker_id))
        gates.hold(paused)

    async def beating(self) -> None:
        """Beat every ``BEAT_S`` until cancelled; a store that cannot be reached is tried again a beat
        later (the registration expires meanwhile if it stays so)."""
        while True:
            await asyncio.sleep(BEAT_S)
            try:
                await self.beat()
            except psycopg.Error as exc:
                log.warning("worker registration not renewed: %s", type(exc).__name__)

    async def leave(self) -> None:
        """Stop processing keys: the whole of each target's budget is this process's again, for what else it
        reads (a key compared from the API), and the others take this one's share."""
        self.ctx.gates.share(1, 0)
        with contextlib.suppress(psycopg.Error):  # it expires all the same
            async with self.ctx.pool.connection() as conn:
                await repo.leave_worker(conn, self.worker_id)
