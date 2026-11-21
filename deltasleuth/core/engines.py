"""What a targets file and a spec may declare of each database engine, as data: the target registry and the
spec loader read it here, since the engine seam (``connectors.tables``) is no import of core. Each engine
of the seam's registry carries its own row (``Engine.traits``), and a test holds the two to the same
engines. A new engine is one row here and one adapter there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class EngineTraits:
    """One engine's row.

    ``declared``: how its target names it: a PostgreSQL DSN (``dsn_ref``), an endpoint (``engine``, with
    ``host``, ``port``, the database's name and a user), or a directory of files (``engine``, ``root``).
    ``port``: an endpoint's default port. ``names``: the member an endpoint names its database by; the
    other is refused. ``server_dn``: whether its TLS refuses ``tls_server_dn`` or requires it while the
    name is verified (None: it takes one, never required). ``regex_key_map`` and ``ilike``: whether a side
    or a key table on it runs a regex key map and ILIKE. ``graph_parity``: whether graph parity reads its
    link facts. ``file``: whether its sides declare ``table.file``.
    """

    declared: Literal["dsn_ref", "endpoint", "files"]
    port: int | None = None
    names: Literal["database", "service"] | None = None
    server_dn: Literal["refused", "required"] | None = None
    regex_key_map: bool = True
    ilike: bool = True
    graph_parity: bool = False
    file: bool = False


ENGINES: dict[str, EngineTraits] = {
    "postgresql": EngineTraits("dsn_ref", graph_parity=True),
    "mysql": EngineTraits("endpoint", 3306, "database", "refused", regex_key_map=False, ilike=False),
    "oracle": EngineTraits("endpoint", 1521, "service", "required", regex_key_map=False, ilike=False),
    "files": EngineTraits("files", regex_key_map=False, ilike=False, file=True),
}


def declared_by(declared: str) -> list[str]:
    """The engines a target declares the way ``declared`` says, in the registry's order."""
    return [name for name, traits in ENGINES.items() if traits.declared == declared]
