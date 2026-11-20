"""Cross-module contracts that only integration can check.

* The e164 normaliser formats every *possible* number: the mocks' own examples use the reserved
  drama range (07700 900xxx), which libphonenumber rates possible but not valid. A validity gate
  left those numbers unnormalised on one side and turned format-only differences into mismatches.
* A finding's pattern is the text its rule path normalises to, so rule paths and finding patterns
  compare by string equality.
* Black-box rule: nothing in the package (the packaged schemas included), the end-to-end tests or
  the documents names how a target is built.
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
from deltasleuth.reports import FORMATS
from deltasleuth.store import repo

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
    packaged schemas), the end-to-end tests and the documents never name how one is built."""
    roots = [repo_root / "deltasleuth", repo_root / "tests" / "e2e"]
    suffixes = (".py", ".j2", ".json", ".yaml", ".md")
    files = [p for root in roots for p in root.rglob("*") if p.is_file() and p.suffix in suffixes]
    files += [repo_root / "README.md", repo_root / "SECURITY.md", *(repo_root / "docs").glob("*.md")]
    offenders = [str(p.relative_to(repo_root)) for p in files if TARGET_BACKENDS.search(p.read_text())]
    assert offenders == []


# A rule id, an appendix or a phase of a design document the repository does not hold.
CITATIONS = re.compile(r"\([XD]\d+\)|\brule X\d+|Appendix [A-Z]|\bP0\b")


def test_nothing_cites_a_rule_id_appendix_or_phase_of_a_design_document(repo_root: Path) -> None:
    """Messages, help text, docstrings and comments say what the code does: deltasleuth/, mocks/,
    examples/ and tools/ cite no rule id (``(X2)``), appendix or phase (``P0``) of a design document."""
    roots = [repo_root / name for name in ("deltasleuth", "mocks", "examples", "tools")]
    suffixes = (".py", ".j2", ".json", ".yaml", ".yml", ".md", ".sql", ".txt", ".html", ".toml", ".sh")
    files = [p for root in roots for p in root.rglob("*") if p.is_file() and p.suffix in suffixes]
    offenders = [
        f"{p.relative_to(repo_root)}:{n}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1)
        if CITATIONS.search(line)
    ]
    assert offenders == []


# What each outcome says of a key, as the Run screen's legend words it.
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


def test_the_run_screen_offers_every_report_the_run_route_serves(repo_root: Path) -> None:
    """``REPORTS`` in web/src/Run.tsx, the run screen's downloads, is ``reports.FORMATS`` in its order: a
    format the route serves is offered."""
    source = (repo_root / "web" / "src" / "Run.tsx").read_text()
    offered = re.search(r"^const REPORTS = \[(.*?)\];$", source, re.DOTALL | re.MULTILINE)
    assert offered and re.findall(r"'([\w.]+)'", offered[1]) == list(FORMATS)


def test_the_run_screen_knows_the_statuses_a_run_ends_in(repo_root: Path) -> None:
    """``TERMINAL`` in web/src/Run.tsx, past which the run screen stops polling, is ``repo.TERMINAL``."""
    source = (repo_root / "web" / "src" / "Run.tsx").read_text()
    ended = re.search(r"^const TERMINAL = \[(.*)\];$", source, re.MULTILINE)
    assert ended and re.findall(r"'(\w+)'", ended[1]) == list(repo.TERMINAL)


def _moves_from(source: str) -> dict[str, set[str]]:
    """The statuses each run control of runner/runs.py moves a run from: the ``when`` of the status change in
    ``pause_run``, ``resume_run`` and ``stop_run``, which a cancel calls."""
    controls = {"pause_run": "pause", "resume_run": "resume", "stop_run": "cancel"}
    moves: dict[str, set[str]] = {}
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name in controls):
            continue
        for call in (c for c in ast.walk(node) if isinstance(c, ast.Call)):
            called = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            when = {k.arg: k.value for k in call.keywords}.get("when")
            if called == "_move":
                when = call.args[3]
            if called in ("_move", "set_run_status") and when is not None:
                moves[controls[node.name]] = set(ast.literal_eval(when))
    return moves


def test_the_run_screen_offers_the_controls_the_runner_takes(repo_root: Path) -> None:
    """``ACTIONS`` in web/src/Run.tsx, the controls the run screen offers in each status, are those the runner
    moves a run from that status by (a pause from running, a resume from paused, a cancel from any live
    status)."""
    source = (repo_root / "web" / "src" / "Run.tsx").read_text()
    offered = re.search(r"^const ACTIONS: Record<string, string\[\]> = \{(.*)\};$", source, re.MULTILINE)
    assert offered
    pairs = {
        (status, action)
        for status, actions in re.findall(r"(\w+): \[([^\]]*)\]", offered[1])
        for action in re.findall(r"'(\w+)'", actions)
    }
    moves = _moves_from((repo_root / "deltasleuth" / "runner" / "runs.py").read_text())
    assert set(moves) == {"pause", "resume", "cancel"}
    assert pairs == {(status, action) for action, statuses in moves.items() for status in statuses}


# The detail keys each fed audit action's projector reads: every writer of the
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


# -- layers ----------------------------------------------------------------------------------------

# What a module of deltasleuth.core may import from the rest of the package, and why. Every other import
# of a core module names core: the engine's logic depends on nothing that reads, stores or serves.
CORE_MAY_IMPORT = {
    "deltasleuth.errors": "the shared refusal types, a module that imports nothing of the package",
    "deltasleuth.packs": (
        "the domain packs: taxonomy codes the registry discovers by module name, each a module of data that"
        " imports core alone"
    ),
}
# What the modules CORE_MAY_IMPORT names may import of the package themselves: nothing but core.
ALLOWED_DEPENDS_ON = {"deltasleuth.errors": (), "deltasleuth.packs": ("deltasleuth.core",)}
# The modules of deltasleuth.core that do I/O, and why; every other one computes on what it is given.
CORE_IO = {
    "core/secrets.py": "the one reader of a credential's file: reference",
    "core/cron.py": "reads the version line of the tz data schedules fire by",
    "core/spec/documents.py": "reads the spec or targets document its caller names",
    "core/spec/schemas.py": "reads the package's own JSON schemas, a pack's among them",
    "core/spec/_keypattern_process.py": "starts the child that bounds a key pattern's time and memory",
    "core/spec/_keypattern_worker.py": "that child, which answers on its pipes under resource limits",
}
IO_MODULES = re.compile(
    r"(subprocess|socket|ssl|select|selectors|threading|multiprocessing|asyncio|concurrent|resource|signal"
    r"|fcntl|shutil|tempfile|http|urllib\.request|psycopg|psycopg_pool|aiohttp|httpx|duckdb|pymysql"
    r"|oracledb)(\..*)?"
)
IO_CALLS = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "stat", "iterdir", "glob"}
IO_CALLS |= {"rglob", "unlink", "mkdir", "Popen"}
# A statement over the store's schema, or one of its session locks.
STORE_SQL = re.compile(r"\bds\.[a-z_]+\b|\bpg_(?:try_)?advisory", re.IGNORECASE)
STATEMENT = re.compile(r"^\s*(?:SELECT|UPDATE|INSERT|DELETE|WITH|CREATE|SET|LOCK)\b", re.IGNORECASE)


def _module(path: Path, package: Path) -> str:
    parts = path.relative_to(package.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path, package: Path) -> list[tuple[int, str]]:
    """The modules ``path`` imports at run time, by their absolute names: those under ``if TYPE_CHECKING:``
    are types only and never run. ``from deltasleuth import x`` names ``deltasleuth.x``."""
    tree, module = ast.parse(path.read_text()), _module(path, package)
    typing_only = {
        id(node)
        for block in ast.walk(tree)
        if isinstance(block, ast.If) and ast.unparse(block.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING")
        for statement in block.body
        for node in ast.walk(statement)
    }
    found = []
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                here = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
                tail = [node.module] if node.module else []
                base = ".".join([*here[: len(here) - node.level + 1], *tail])
            named = [f"{base}.{alias.name}" for alias in node.names] if base == "deltasleuth" else [base]
            found += [(node.lineno, name) for name in named]
    return found


def _within(name: str, prefixes: Any) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def test_the_engine_imports_nothing_outside_core_but_what_is_named(repo_root: Path) -> None:
    """A module of deltasleuth.core imports core, the standard library, third-party libraries and only the
    modules CORE_MAY_IMPORT names; those import nothing of the package but core themselves."""
    package = repo_root / "deltasleuth"
    offenders = [
        f"{path.relative_to(repo_root)}:{line} imports {name}"
        for path in sorted((package / "core").rglob("*.py"))
        for line, name in _imports(path, package)
        if name.startswith("deltasleuth.")
        and not _within(name, ("deltasleuth.core",))
        and not _within(name, CORE_MAY_IMPORT)
    ]
    for allowed, may in ALLOWED_DEPENDS_ON.items():
        dotted = package.parent / allowed.replace(".", "/")
        files = sorted(dotted.rglob("*.py")) if dotted.is_dir() else [dotted.with_suffix(".py")]
        offenders += [
            f"{path.relative_to(repo_root)}:{line} imports {name}"
            for path in files
            for line, name in _imports(path, package)
            if name.startswith("deltasleuth.") and not _within(name, may)
        ]
    assert offenders == []


def test_only_the_named_engine_modules_do_io(repo_root: Path) -> None:
    """No module of deltasleuth.core reads a file, starts a process or opens a connection, but those
    CORE_IO names with the reason."""
    package = repo_root / "deltasleuth"
    doing: dict[str, list[str]] = {}
    for path in sorted((package / "core").rglob("*.py")):
        found = [
            f"{line} imports {name}" for line, name in _imports(path, package) if IO_MODULES.fullmatch(name)
        ]
        for call in ast.walk(ast.parse(path.read_text())):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = getattr(func, "id", None) or getattr(func, "attr", "")
            on_os = isinstance(func, ast.Attribute) and ast.unparse(func.value) == "os"
            if name in IO_CALLS or on_os:
                found.append(f"{call.lineno} calls {name}")
        if found:
            doing[str(path.relative_to(package))] = found
    assert sorted(doing) == sorted(CORE_IO), {name: doing[name] for name in set(doing) - set(CORE_IO)}


def test_the_stores_sql_is_written_in_the_store_package_alone(repo_root: Path) -> None:
    """Every statement over the store's schema ``ds``, and every session lock of the store, is in
    deltasleuth/store (the ``repo`` package, one function a use case): no call of ``execute`` outside it is
    given one, and no string outside it is one, whoever passes it on."""
    package = repo_root / "deltasleuth"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.is_relative_to(package / "store"):
            continue
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            where = f"{path.relative_to(repo_root)}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("execute", "executemany"):
                texts = [t.value for a in node.args for t in ast.walk(a) if isinstance(t, ast.Constant)]
                if any(isinstance(text, str) and STORE_SQL.search(text) for text in texts):
                    offenders.append(f"{where} executes the store's SQL")
            elif isinstance(node, ast.Constant) and isinstance(text := node.value, str):
                if id(node) not in docstrings and STATEMENT.search(text) and STORE_SQL.search(text):
                    offenders.append(f"{where} holds the store's SQL")
    assert offenders == []


# Packages that must not import another, and why.
NEVER_IMPORTS = {
    ("store", "deltasleuth.connectors"): "the store keeps what the connectors read, never how they read it",
    ("store", "deltasleuth.runner"): "the runner calls the store",
    ("security", "deltasleuth.runner"): "the runner seals and masks through security",
}


@pytest.mark.parametrize(("importer", "imported"), sorted(NEVER_IMPORTS))
def test_a_package_does_not_reach_the_layer_above_it(repo_root: Path, importer: str, imported: str) -> None:
    package = repo_root / "deltasleuth"
    offenders = [
        f"{path.relative_to(repo_root)}:{line} imports {name}"
        for path in sorted((package / importer).rglob("*.py"))
        for line, name in _imports(path, package)
        if _within(name, (imported,))
    ]
    assert offenders == [], NEVER_IMPORTS[importer, imported]
