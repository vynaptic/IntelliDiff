"""Public pairs through the DB-backed runner: each example spec compiles against its targets, every key
of its keys.txt is compared, the counters reconcile, the documented differences appear and missing
keys resolve as the targets declare. Offline over the captured samples (file mode); the
``live_public`` test (skipped by default) re-reads three keys from the real APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deltasleuth.connectors.files import FileSampleSource
from deltasleuth.core.spec import load_spec_file
from deltasleuth.runner import create_run
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import finish, raw_keys, store_spec

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
EXAMPLES, SAMPLES = ROOT / "examples/public", ROOT / "tests/fixtures/public"
# pair -> (declared not-found keys, outcome of every other key, its findings as (path, taxonomy, rule))
EXPECTED = {
    "swapi": (
        {"17", "84"},
        "KNOWN_DIFF",
        {
            ("$.created", "VALUE_MISMATCH", "KD-SWAPI-TIMESTAMPS"),
            ("$.edited", "VALUE_MISMATCH", "KD-SWAPI-TIMESTAMPS"),
            ("$.species", "FIELD_MISSING", "KD-SWAPI-SPECIES"),
        },
    ),
    "rickandmorty": ({"99999"}, "MATCH", {("$.id", "NORMALIZATION_ONLY", None)}),
}


async def run_pair(make_stack: Any, pair: str, keys: list[str] | None, samples: Path | None) -> tuple:
    """The pair's run (the spec's own keys unless ``keys``) and its results by key, with findings."""
    async with make_stack((EXAMPLES / pair / "targets.yaml").read_text()).context() as ctx:
        spec_id = await store_spec(ctx, EXAMPLES / pair / "spec.yaml")
        run = await finish(
            ctx, await create_run(ctx, spec_id, "batch", keys=keys, samples_dir=samples, who=TESTER)
        )
        async with ctx.pool.connection() as conn:
            results = await repo.pages(repo.list_results, conn, run["run_id"])
            findings = await repo.pages(repo.list_findings, conn, run["run_id"])
        raw = await raw_keys(ctx, run["run_id"])
    by_key = {
        raw[r["key_masked"]]: r | {"findings": [f for f in findings if f["key_masked"] == r["key_masked"]]}
        for r in results
    }
    return run, by_key


def problems(pair: str, results: dict[str, dict[str, Any]]) -> list[str]:
    absent, outcome, findings = EXPECTED[pair]
    found = []
    for key, r in results.items():
        actual = {(f["path_pattern"], f["taxonomy_code"], f["rule_id"]) for f in r["findings"]}
        if key in absent:  # absent_both: the target confirmed the absence on both sides
            ok = (r["outcome"], r["inconclusive_reason"]) == ("INCONCLUSIVE", "absent_both")
        else:
            ok = r["outcome"] == outcome and actual == findings
        if not ok:
            found.append(f"{key}: {r['outcome']} {r['inconclusive_reason']} {sorted(actual, key=str)}")
    return found


@pytest.mark.parametrize("pair", EXPECTED)
async def test_samples_resolve_as_documented(make_stack: Any, pair: str) -> None:
    keys = (EXAMPLES / pair / "keys.txt").read_text(encoding="utf-8").split()
    listed = [str(k) for k in load_spec_file(EXAMPLES / pair / "spec.yaml")["key"]["source"]["keys"]]
    assert keys == listed, "keys.txt and the spec's key list must stay the same"
    assert sorted(keys) == FileSampleSource(SAMPLES / pair).keys()

    run, results = await run_pair(make_stack, pair, None, SAMPLES / pair)
    assert list(results) == keys and problems(pair, results) == []
    c = run["counters"]
    assert (run["status"], c["total"]) == ("completed", len(keys)) and all(
        i["holds"] for i in c["reconciliation"]
    )


@pytest.mark.live_public
@pytest.mark.parametrize("pair", EXPECTED)
async def test_live_apis_still_behave_as_captured(make_stack: Any, pair: str) -> None:
    _, results = await run_pair(make_stack, pair, ["1", "2", min(EXPECTED[pair][0])], None)
    assert problems(pair, results) == []
