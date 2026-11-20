"""Reports of a run, read from the store and masked: CSV exports (``csv``), the PDF run report
(``pdf``) and the onboarding tools (``measure``, ``adjudicate``, ``summary``).

Documents are built as ``render.Document`` and rendered as Markdown, HTML or PDF. ``FORMATS`` is
every downloadable report by published name, as (media type, render(ctx, run_id, cycle)); a new
format is one entry. ``readiness.csv`` is the run's pair's readiness points up to the cycle. ``download``
gives one to a person, as the API and the CLI do: audited, and a CSV export a page of rows at a time.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from functools import partial

from deltasleuth import licence
from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.errors import NotFound
from deltasleuth.reports.csv import KINDS, stream_csv, write_csv
from deltasleuth.reports.pdf import pdf_report
from deltasleuth.reports.readiness import run_csv
from deltasleuth.store import repo

FORMATS = (
    {"report.pdf": ("application/pdf", pdf_report)}
    | {f"{kind}.csv": ("text/csv", partial(write_csv, kind=kind)) for kind in KINDS}
    | {"readiness.csv": ("text/csv", run_csv)}
)
# The formats a download sends a chunk at a time: awaited, each reads what may refuse it, then gives its
# chunks.
STREAMED = {f"{kind}.csv": partial(stream_csv, kind=kind) for kind in KINDS}


@dataclass(frozen=True)
class Download:
    """A report given to a person: the name it is written as, its media type and its bytes, whole or a
    chunk at a time."""

    file_name: str
    media: str
    content: bytes | AsyncGenerator[bytes]


async def download(
    ctx: Context, who: Principal, run_id: str, name: str, cycle: int | None = None
) -> Download:
    """The report ``name`` of ``FORMATS`` of the run's ``cycle``, given to ``who``. The cycle is ``cycle``
    or the run's current one, read once before the report is. What may refuse the report is read first
    (NotFound for an unknown run or report, ``CyclePurged`` once the cycle's purge has started); then the
    download is audited (``downloaded``), before its first byte is given."""
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    if name not in FORMATS:
        raise NotFound(f"report {name!r} not found")
    cycle = cycle or run["cycle"]
    media, render = FORMATS[name]
    content = await STREAMED.get(name, render)(ctx, run_id, cycle)
    await downloaded(ctx, who, run_id, name, cycle)
    return Download(licence.file_name(run, name), media, content)


async def downloaded(ctx: Context, who: Principal, run_id: str, name: str, cycle: int | None) -> None:
    """Audit a report of the run given to ``who`` as ``report.download``, in a transaction of its own, with
    the cycle it read: ``cycle``, or the run's current one."""
    async with repo.audited(ctx.pool) as (conn, audit):
        if not cycle:
            run = await repo.get_run(conn, run_id)
            cycle = None if run is None else run["cycle"]
        await audit(who, "report.download", run_id, {"name": name, "cycle": cycle})
