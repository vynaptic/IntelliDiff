"""Reports of a run, read from the store and masked: CSV exports (``csv``), the PDF run report
(``pdf``) and the P0 onboarding tools (``measure``, ``adjudicate``, ``summary``).

Documents are built as ``render.Document`` and rendered as Markdown, HTML or PDF. ``FORMATS`` is
every downloadable report by published name, as (media type, render(ctx, run_id, cycle)); a new
format is one entry. ``readiness.csv`` is the run's pair's readiness points up to the cycle
(docs/mvp/READINESS.md §11).
"""

from functools import partial

from deltasleuth.reports.csv import KINDS, write_csv
from deltasleuth.reports.pdf import pdf_report
from deltasleuth.reports.readiness import run_csv

FORMATS = (
    {"report.pdf": ("application/pdf", pdf_report)}
    | {f"{kind}.csv": ("text/csv", partial(write_csv, kind=kind)) for kind in KINDS}
    | {"readiness.csv": ("text/csv", run_csv)}
)
