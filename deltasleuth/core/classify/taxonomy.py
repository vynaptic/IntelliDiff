"""Taxonomy registry: core codes as ``ds.taxonomy_codes`` seeds them, plus packs.

A domain pack is any ``deltasleuth.packs.<module>`` exposing a ``PACK``: it adds domain-level
codes and display aliases of core codes (``MISSING_ENTITY`` for ``MISSING_DOCUMENT``). Findings
carry the active pack's alias, the name reviewers read and write known-diff rules against.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from functools import cache
from typing import Literal

from deltasleuth import packs
from deltasleuth.core.types import ChangeType, CompiledSpec, Severity

Presence = Literal["missing", "extra", "none"]


class TaxonomyError(ValueError):
    """An unknown code, a code whose presence contradicts the change type, or a bad pack."""


@dataclass(frozen=True)
class TaxonomyCode:
    """One registry row (``ds.taxonomy_codes``)."""

    code: str
    pack: str
    default_severity: Severity
    presence: Presence = "none"
    alias_of: str | None = None  # the core code this pack code is the display name of
    description: str = ""

    def allows(self, change_type: ChangeType) -> bool:
        """``missing`` codes classify missing_in_b findings only, ``extra`` ones extra_in_b only."""
        if self.presence == "missing":
            return change_type is ChangeType.MISSING_IN_B
        if self.presence == "extra":
            return change_type is ChangeType.EXTRA_IN_B
        return True


_C, _H, _M, _I = Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.INFO
CORE_CODES: dict[str, TaxonomyCode] = {
    code: TaxonomyCode(code, "core", severity, presence, None, description)
    for code, severity, presence, description in (
        ("FIELD_MISSING", _M, "missing", "Field present in A, absent in B."),
        ("FIELD_EXTRA", _M, "extra", "Field present in B, absent in A."),
        ("VALUE_MISMATCH", _M, "none", "Value differs."),
        ("TYPE_MISMATCH", _M, "none", "JSON type differs."),
        ("ORDER_MISMATCH", _M, "none", "Order of an ordered array differs."),
        ("NORMALIZATION_ONLY", _I, "none", "Raw values differ but canonical values are equal."),
        ("MISSING_DOCUMENT", _C, "missing", "Document present in A, authoritatively absent in B."),
        ("EXTRA_DOCUMENT", _C, "extra", "Document present in B, authoritatively absent in A."),
        ("MISSING_NODE", _H, "missing", "Graph mode: typed node present in A, absent in B."),
        ("EXTRA_NODE", _H, "extra", "Graph mode: typed node present in B, absent in A."),
        ("MISSING_EDGE", _H, "missing", "Graph mode: edge present in A, absent in B."),
        ("EXTRA_EDGE", _H, "extra", "Graph mode: edge present in B, absent in A."),
        ("FILTER_MEMBERSHIP", _M, "none", "The selection predicate is true on one side only."),
        ("SCHEMA_DRIFT", _I, "none", "An observation violates the approved schema on unselected paths."),
        ("INTERNAL_INCONSISTENCY", _H, "none", "A side contradicts itself."),
    )
}

# The generic code of each change type, when no field or array rule claims a finding.
DEFAULT_CODES: dict[ChangeType, str] = {
    ChangeType.MISSING_IN_B: "FIELD_MISSING",
    ChangeType.EXTRA_IN_B: "FIELD_EXTRA",
    ChangeType.VALUE_CHANGED: "VALUE_MISMATCH",
    ChangeType.TYPE_CHANGED: "TYPE_MISMATCH",
    ChangeType.ORDER_CHANGED: "ORDER_MISMATCH",
}


@dataclass(frozen=True)
class DomainPack:
    """An installed domain pack: its own codes, display aliases of core codes included."""

    name: str
    version: str
    codes: tuple[TaxonomyCode, ...]

    def __post_init__(self) -> None:
        # As the DDL requires: new codes of this pack only, each alias naming a core code of the
        # same presence, and no core code aliased twice.
        codes = [c.code for c in self.codes]
        aliased = [c.alias_of for c in self.codes if c.alias_of is not None]
        if (
            len(set(codes)) != len(codes)
            or set(codes) & CORE_CODES.keys()
            or len(set(aliased)) != len(aliased)
        ):
            raise TaxonomyError(f"pack {self.name} redefines a code or aliases a core code twice")
        for c in self.codes:
            core = CORE_CODES.get(c.alias_of or "")
            if c.pack != self.name or (
                c.alias_of is not None and (core is None or core.presence != c.presence)
            ):
                raise TaxonomyError(f"pack {self.name}: {c.code} has a foreign pack or a bad alias")

    @property
    def aliases(self) -> dict[str, str]:
        """Core code -> this pack's display name for it."""
        return {c.alias_of: c.code for c in self.codes if c.alias_of is not None}

    def get(self, code: str) -> TaxonomyCode | None:
        return next((c for c in self.codes if c.code == code), None)


def installed_packs() -> dict[str, DomainPack]:
    """Installed packs by name: every ``deltasleuth.packs.<module>`` that exposes a ``PACK``."""
    return dict(_discover())


@cache
def _discover() -> dict[str, DomainPack]:
    found: dict[str, DomainPack] = {}
    for module in pkgutil.iter_modules(packs.__path__):
        pack = getattr(importlib.import_module(f"{packs.__name__}.{module.name}"), "PACK", None)
        if isinstance(pack, DomainPack):
            found[pack.name] = pack
    return found


def resolve_pack(spec: CompiledSpec) -> DomainPack | None:
    """The installed pack ``spec.domain_pack`` names (the loader has checked it is installed)."""
    return None if spec.domain_pack is None else _discover()[spec.domain_pack]


def lookup(code: str, pack: DomainPack | None = None) -> TaxonomyCode:
    """The registry row of ``code``: a core code, else a code of ``pack``."""
    row = CORE_CODES.get(code) or (pack.get(code) if pack else None)
    if row is None:
        where = f"core or pack {pack.name}" if pack else "core (no domain pack is active)"
        raise TaxonomyError(f"unknown taxonomy code {code!r} in {where}")
    return row
