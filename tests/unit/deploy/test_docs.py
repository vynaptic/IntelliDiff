"""The documents: every relative link leads to a path that exists, and nothing outside tests/ names a
document folder that is gone."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
GONE = "docs/(mvp|guide|bench|architecture/)"


def _pages(repo_root: Path) -> list[Path]:
    top = [repo_root / "README.md", repo_root / "SECURITY.md"]
    return [*top, *(repo_root / "docs").glob("*.md"), *(repo_root / "examples").rglob("README.md")]


def test_every_relative_link_leads_to_a_path_that_exists(repo_root: Path) -> None:
    """A link to another site or to a heading of its own page is not followed."""
    pages = _pages(repo_root)
    assert len(pages) > 5
    for page in pages:
        paths = [target.partition("#")[0] for target in LINK.findall(page.read_text())]
        led = [path for path in paths if path and not re.match("[a-z]+:", path)]
        assert [path for path in led if not (page.parent / path).exists()] == [], page.name


def test_nothing_outside_the_tests_names_a_folder_that_is_gone(repo_root: Path) -> None:
    """Every text file git tracks or would add, searched in the working tree."""
    grep = ["git", "grep", "-l", "-I", "--untracked", "-E", GONE, "--", ".", ":!tests"]
    found = subprocess.run(grep, cwd=repo_root, capture_output=True, text=True, check=False)
    assert (found.returncode, found.stdout, found.stderr) == (1, "", "")
