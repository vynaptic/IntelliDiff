"""Cross-module contracts that only integration can check.

* The e164 normaliser formats every *possible* number: the design's own examples use the reserved
  drama range (07700 900xxx), which libphonenumber rates possible but not valid. A validity gate
  left those numbers unnormalised on one side and turned format-only differences into mismatches.
* A finding's pattern is the text its rule path normalises to, so rule paths and finding patterns
  compare by string equality.
* Black-box rule (PLAN rule 2): nothing in the package (the packaged schema copies included), the
  end-to-end tests or the runbooks names how a target is built.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.agent import validate
from deltasleuth.core import notify
from deltasleuth.core.canonical import paths as canonical_paths
from deltasleuth.core.canonical.normalisers import apply_chain
from deltasleuth.core.spec import normalise_pattern
from deltasleuth.core.types import Normalizer, Outcome

E164_GB = (Normalizer("e164", {"defaultRegion": "GB"}),)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07700 900123", "+447700900123"),  # drama range: possible, not "valid"
        ("+44 7700 900415", "+447700900415"),
        ("020 7946 0018", "+442079460018"),
    ],
)
def test_e164_formats_possible_numbers(raw: str, expected: str) -> None:
    assert apply_chain(raw, E164_GB) == expected
    assert apply_chain(expected, E164_GB) == expected  # idempotent


@pytest.mark.parametrize("raw", ["123", "garbage", "", "shared.aadb@example.org", "+99 999"])
def test_e164_leaves_impossible_input_unchanged(raw: str) -> None:
    assert apply_chain(raw, E164_GB) == raw


def test_e164_leaves_numbers_unchanged() -> None:
    assert apply_chain(Decimal(7700900123), E164_GB) == Decimal(7700900123)


def test_pattern_of_matches_the_normalised_rule_text() -> None:
    location = ("links", 3, "shared Identifiers", 0, "value")
    rendered = canonical_paths.pattern_of(location)
    assert rendered == "$.links[*]['shared Identifiers'][*].value"
    assert normalise_pattern(rendered) == rendered


TARGET_BACKENDS = re.compile(r"janus|cassandra|gremlin|tinkerpop|neo4j|mongodb|dynamodb", re.IGNORECASE)


def test_no_target_backend_is_named_anywhere_in_the_package(repo_root: Path) -> None:
    """A target is an endpoint plus declared behaviour: deltasleuth/ (code, templates and the
    packaged schemas), the end-to-end tests and the runbooks never name how one is built."""
    roots = [repo_root / "deltasleuth", repo_root / "tests" / "e2e"]
    suffixes = (".py", ".j2", ".json", ".yaml", ".md")
    files = [p for root in roots for p in root.rglob("*") if p.is_file() and p.suffix in suffixes]
    files += [repo_root / "docs" / "mvp" / "RUNBOOK.md", repo_root / "README.md"]
    offenders = [str(p.relative_to(repo_root)) for p in files if TARGET_BACKENDS.search(p.read_text())]
    assert offenders == []


# What each outcome says of a key, as the Run screen's legend restates docs/architecture.md §7.1.
LEGEND = {
    "MATCH": "Both sides answered in full and nothing that matters differs.",
    "MISMATCH": "Both sides answered in full and a difference has no explanation.",
    "KNOWN_DIFF": (
        "A spec rule covers every difference; it counts as a match unless the rule calls it a bug."
    ),
    "NOISE_ONLY": "Only fields the spec declares as noise differ; no key gets it in this build.",
    "DRIFT": "A side changed between the first read and the recheck, so nothing can be said.",
    "NOT_FOUND_A": "Side b holds the record; side a says clearly it has none.",
    "NOT_FOUND_B": "Side a holds the record; side b says clearly it has none.",
    "INCONCLUSIVE": (
        "A side could not be read in full, or neither side holds the key, so nothing can be said."
    ),
}


def test_outcome_legend_names_every_outcome(repo_root: Path) -> None:
    """The Run screen's legend (``LEGEND`` in web/src/Run.tsx) words every outcome the engine gives a key,
    in the enum's order, and the list its badges and its filter follow (``OUTCOMES``) names the same."""
    source = (repo_root / "web" / "src" / "Run.tsx").read_text()
    legend = re.search(r"^const LEGEND[^\n]*\{\n(.*?)^\};", source, re.DOTALL | re.MULTILINE)
    listed = re.search(r"^const OUTCOMES = \[(.*)\];$", source, re.MULTILINE)
    assert legend and listed
    told = dict(re.findall(r"^  (\w+): '(.*)',$", legend[1], re.MULTILINE))
    assert list(told) == re.findall(r"'(\w+)'", listed[1]) == [outcome.value for outcome in Outcome]
    assert told == LEGEND


def test_the_ui_reads_a_citation_as_the_validator_does(repo_root: Path) -> None:
    """A citation chip is a citation the server checked: ``CITATION`` in web/src/explain.ts is the
    validator's expression, so a bracket it did not read (one with a line break inside) is no chip."""
    source = (repo_root / "web" / "src" / "explain.ts").read_text()
    assert f"export const CITATION = /{validate.CITATION.pattern}/g;" in source


# What a MySQL or Oracle table side needs, in the contract's own words (docs/mvp/ENGINES.md §17).
PREREQUISITES = (
    "observed on MySQL 8.4.11 and Oracle 23.26.3 only (8.0 and 19c are supported, not observed: run the"
    " acceptance kit); Oracle over plain TCP or TCPS, not Native Network Encryption; in an Oracle database"
    " that is not AL32UTF8, a run takes text keys of up to 500 bytes, by a conversion observed on"
    " `NVARCHAR2` only; MariaDB and SQL Server are not supported; `ds target check` reports each of these"
    " before a run is attempted."
)


def test_the_engine_prerequisites_are_stated_word_for_word(repo_root: Path) -> None:
    """The deployment guide and the README say the prerequisites of MySQL and Oracle sides as the
    contract words them, line breaks aside."""
    said = {
        name: " ".join((repo_root / name).read_text().split()).count(PREREQUISITES)
        for name in ("docs/mvp/ENGINES.md", "docs/mvp/DEPLOY.md", "README.md")
    }
    assert said == {
        "docs/mvp/ENGINES.md": 1,
        "docs/mvp/DEPLOY.md": 1,
        "README.md": 1,
    }


# The detail keys each fed audit action's projector reads (docs/mvp/ALERTS.md §2.4): every writer of the
# action must still write them, or its notifications would fail their models and never be sent.
FEED_READS = {
    "run.cycle": {"cycle"},
    "run.finished": {"status", "reason"},
    "cancelling": {"reason"},
    "paused": {"reason"},
    "sample.draw_failed": {"cycle"},
    "accuracy.label": {"run_id", "cycle", "labelled", "undecidable", "not_reviewed"},  # its run: the feed's
    "approval.request": {"kind", "spec_id"},
    "approval.grant": {"kind", "spec_id", "step", "of"},
    "approval.revoke": {"paused", "stopped", "cascade_of"},
    "licence.state": {"from", "to", "licence_id", "ends"},
    "readiness.changed": {
        "programme",
        "entity",
        "from",
        "to",
        "was_regressed",
        "regressed",
        "score",
        "causes",
        "gates",
        "changed",
        "evidence",
        "candidate",
        "signoff_id",
        "pack_id",
        "config_sha256",
    },
    "schedule.missed": {
        "reason",
        "code",
        "due",
        "to",
        "count",
        "spec_name",
        "mode",
        "run_id",
        "draw_id",
        "error_type",
    },
    "schedule.pause": {"reason", "code", "missed", "spec_name", "mode"},
    "schema.drift": {"cycle", "where", "mode", "checked", "paused", "basis", "sides", "error_type"},
    "notify.dead": {"delivery_id", "event", "attempts", "error", "dead_since_last"},
    "notify.skipped": {"reason"},
}
# An action whose every writer passes the detail one function builds (its file, its name): that function's
# literal is read instead.
BUILT = {"schedule.missed": ("schedules.py", "missed"), "schema.drift": ("runner/drift.py", "_detail")}
# An action only one writer's rows of which are notified (its file): a person's pause of a schedule has no
# reason, and is projected to nothing.
ONLY = {"schedule.pause": "schedule.py"}


def _keys(node: Any, function: Any) -> set[str]:
    """The constant keys a detail argument holds: a dict literal, or a name the function builds from them."""
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant)}
    if isinstance(node, ast.BinOp):
        return _keys(node.left, function) | _keys(node.right, function)
    if not isinstance(node, ast.Name):
        return set()
    keys: set[str] = set()
    for statement in ast.walk(function):
        if isinstance(statement, ast.Assign | ast.AugAssign):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == node.id:
                    keys |= _keys(statement.value, function)
                elif (
                    isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id == node.id and isinstance(target.slice, ast.Constant)
                ):  # fmt: skip
                    keys.add(target.slice.value)
    return keys


def test_each_fed_action_still_carries_what_its_projector_reads(repo_root: Path) -> None:
    assert set(FEED_READS) == set(notify.FEED_ACTIONS)
    found: dict[str, int] = dict.fromkeys(FEED_READS, 0)
    for path in (repo_root / "deltasleuth").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for call in ast.walk(function):
                if not (
                    isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "audit"
                ):
                    continue
                if len(call.args) < 4:
                    continue
                action = call.args[1]
                if isinstance(action, ast.Constant) and action.value in FEED_READS:
                    names = [action.value]
                elif isinstance(action, ast.Name) and function.name == "_move":  # runs: paused, cancelling
                    names = ["paused", "cancelling"]
                else:
                    continue
                for name in names:
                    found[name] += 1
                    if name in BUILT or ONLY.get(name, path.name) != path.name:
                        continue
                    missing = FEED_READS[name] - _keys(call.args[3], function)
                    assert not missing, f"{path.name}:{call.lineno} writes {name} without {sorted(missing)}"
    assert all(found.values()), {name: n for name, n in found.items() if not n}
    for name, (file, builder) in BUILT.items():
        tree = ast.parse((repo_root / "deltasleuth" / file).read_text())
        (function,) = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == builder]
        (built,) = [r.value for r in ast.walk(function) if isinstance(r, ast.Return)]
        assert not FEED_READS[name] - _keys(built, function), f"{file}: {builder} builds {name} without them"
