"""PostgreSQL table sides (docs/mvp/TABLES.md): read-only sessions and the SQL of per-key reads, scans and
graph parity. ``engine`` adapts them to the engine seam (``connectors.tables``, docs/mvp/ENGINES.md §5),
whose registry imports it: this package never does.
"""

from deltasleuth.connectors.postgres.queries import facts_sql, group_sets_sql
from deltasleuth.connectors.postgres.session import UNFIT, snapshot
from deltasleuth.connectors.tables.keys import Plan

__all__ = ["UNFIT", "Plan", "facts_sql", "group_sets_sql", "snapshot"]
