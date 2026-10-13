"""What is observed never becomes evidence: no module of readiness, approvals,
the licence's pairs or the evidence packs reads a table pre-check or what the schema guard records of a
run's tables, and a pre-check seats no pair, measures no cycle and changes no run."""

from __future__ import annotations

import ast
import inspect
import re
from types import ModuleType

import pytest

from deltasleuth import approvals, licence, prechecks, readiness
from deltasleuth.core import precheck
from deltasleuth.core import readiness as gates
from deltasleuth.profiler import tables as drafting
from deltasleuth.reports import pack, pack_verify
from deltasleuth.reports import precheck as precheck_reports
from deltasleuth.reports import readiness as readiness_reports
from deltasleuth.runner import drift
from deltasleuth.runner import precheck as checking
from deltasleuth.runner import readiness as measuring
from deltasleuth.security import rawvalues

EVIDENCE = (readiness, gates, measuring, readiness_reports, approvals, licence, pack, pack_verify, rawvalues)
OBSERVED = (precheck, prechecks, checking, precheck_reports, drafting)
# The store's tables and functions of a pre-check, and of the schema guard's record on a run.
READS = re.compile(
    r"ds\.prechecks|precheck_pairs|repo\.\w*precheck\w*|set_guard|held_guard|guard_basis|\[\"guard\"\]"
)


def _imports(module: ModuleType) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    found = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    found |= {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    return found | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }


@pytest.mark.parametrize("module", EVIDENCE, ids=lambda module: module.__name__)
def test_precheck_is_never_evidence(module: ModuleType) -> None:
    """The import graph: readiness, approvals, the licence and the packs import no pre-check module nor
    the schema guard's, and read neither a pre-check's tables nor a run's ``guard``."""
    observed = {found.__name__ for found in (*OBSERVED, drift)}
    assert not _imports(module) & observed
    assert not READS.findall(inspect.getsource(module))


def test_a_pre_check_calls_neither_the_licence_s_gates_nor_a_run() -> None:
    """Nothing is refused for the licence and no pair is counted: the one read is ``licence.status``, to
    label an unlicensed evaluation; and no pre-check module creates, fails or moves a run."""
    for module in OBSERVED:
        source = inspect.getsource(module)
        assert not re.findall(r"licence\.(admit|need|standing|stopped)\b", source), module.__name__
        assert not re.findall(r"\b(create_run|fail_run|pause_run|stop_run|set_run_status)\b", source)
    assert "licence.status(ctx)" in inspect.getsource(prechecks)
    assert not _imports(precheck) & {"deltasleuth.store", "deltasleuth.store.repo"}  # the pure module
