"""Taxonomy registry and domain packs, against the DDL seed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from deltasleuth.core.classify.taxonomy import (
    CORE_CODES,
    DEFAULT_CODES,
    DomainPack,
    TaxonomyCode,
    TaxonomyError,
    installed_packs,
    lookup,
)
from deltasleuth.core.types import ChangeType, Severity

ROW = re.compile(
    r"\('(?P<code>[A-Z0-9_]+)',\s*'(?P<pack>[a-z-]+)',\s*'(?P<sev>[A-Z]+)',\s*'(?P<presence>[a-z]+)',"
    r"\s*(?:'(?P<alias>[A-Z_]+)'|NULL)"
)


def ddl_rows(repo_root: Path, pack: str) -> dict[str, tuple[str, str, str, str | None]]:
    sql = (repo_root / "docs/architecture/control-plane.sql").read_text(encoding="utf-8")
    seed = sql[sql.index("INSERT INTO ds.taxonomy_codes") :]
    rows = (m for m in ROW.finditer(seed[: seed.index(";\n")]) if m["pack"] == pack)
    return {m["code"]: (m["pack"], m["sev"], m["presence"], m["alias"]) for m in rows}


def rows(codes) -> dict[str, tuple[str, str, str, str | None]]:
    return {c.code: (c.pack, c.default_severity.value, c.presence, c.alias_of) for c in codes}


def test_the_registry_mirrors_the_ddl_seed(repo_root: Path) -> None:
    assert len(CORE_CODES) == 15
    assert rows(CORE_CODES.values()) == ddl_rows(repo_root, "core")
    assert rows(installed_packs()["entity-linking"].codes) == ddl_rows(repo_root, "entity-linking")


def test_every_change_type_has_a_default_core_code() -> None:
    assert set(DEFAULT_CODES) == set(ChangeType)
    assert all(CORE_CODES[code].allows(change) for change, code in DEFAULT_CODES.items())


def test_the_entity_linking_pack_is_discovered_with_its_aliases() -> None:
    pack = installed_packs()["entity-linking"]
    assert (pack.version, pack.aliases) == (
        "1.0.0",
        {"MISSING_DOCUMENT": "MISSING_ENTITY", "EXTRA_DOCUMENT": "EXTRA_ENTITY"},
    )
    pack.aliases["MISSING_DOCUMENT"] = "X"  # views are copies
    installed_packs().clear()
    assert installed_packs()["entity-linking"].aliases["MISSING_DOCUMENT"] == "MISSING_ENTITY"


@pytest.mark.parametrize(
    ("change", "presence", "allowed"),
    [
        (ChangeType.MISSING_IN_B, "missing", True),
        (ChangeType.EXTRA_IN_B, "missing", False),
        (ChangeType.VALUE_CHANGED, "missing", False),
        (ChangeType.EXTRA_IN_B, "extra", True),
        (ChangeType.MISSING_IN_B, "extra", False),
        (ChangeType.VALUE_CHANGED, "none", True),
        (ChangeType.MISSING_IN_B, "none", True),
    ],
)
def test_presence_rule(change: ChangeType, presence: str, allowed: bool) -> None:
    assert TaxonomyCode("X_CODE", "core", Severity.LOW, presence).allows(change) is allowed  # type: ignore[arg-type]


def test_lookup_core_and_pack_codes() -> None:
    pack = installed_packs()["entity-linking"]
    assert lookup("VALUE_MISMATCH").default_severity is Severity.MEDIUM
    assert lookup("MISSING_LINK", pack).default_severity is Severity.HIGH
    with pytest.raises(TaxonomyError, match="no domain pack is active"):
        lookup("MISSING_LINK")
    with pytest.raises(TaxonomyError, match="unknown taxonomy code"):
        lookup("NOT_A_CODE", pack)


def code(name: str, pack: str = "p", presence: str = "none", alias: str | None = None) -> TaxonomyCode:
    return TaxonomyCode(name, pack, Severity.HIGH, presence, alias)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "codes",
    [
        pytest.param((code("A_CODE", pack="q"),), id="foreign-pack"),
        pytest.param((code("VALUE_MISMATCH"),), id="redefines-core"),
        pytest.param((code("A_CODE"), code("A_CODE")), id="duplicate"),
        pytest.param((code("A_CODE", alias="NOT_CORE"),), id="alias-of-non-core"),
        pytest.param((code("A_CODE", presence="extra", alias="MISSING_DOCUMENT"),), id="alias-presence"),
        pytest.param(
            (
                code("A_CODE", presence="missing", alias="MISSING_DOCUMENT"),
                code("B_CODE", presence="missing", alias="MISSING_DOCUMENT"),
            ),
            id="aliased-twice",
        ),
    ],
)
def test_domain_pack_validation(codes: tuple[TaxonomyCode, ...]) -> None:
    with pytest.raises(TaxonomyError):
        DomainPack("p", "1.0.0", codes)
