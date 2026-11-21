"""The black-box rule: classify, security and the pack never mention how a target is built."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

OWNED = ("deltasleuth/core/classify", "deltasleuth/security", "deltasleuth/packs/entity_linking")
# Spelled in pieces so this file does not trip a repository-wide search for the same words.
FORBIDDEN = re.compile(
    "|".join(("jan" + "us", "cass" + "andra", "grem" + "lin", "tink" + "erpop")), re.IGNORECASE
)


@pytest.mark.parametrize("package", OWNED)
def test_no_backend_names(repo_root: Path, package: str) -> None:
    sources = sorted((repo_root / package).rglob("*.py"))
    assert sources
    offenders = [
        str(p.relative_to(repo_root)) for p in sources if FORBIDDEN.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
