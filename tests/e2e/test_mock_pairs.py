"""Both mock pairs through ``create_run`` and the worker, live and from captured samples: exactly the
seeded discrepancies of ``mocks/<pair>/expected.json``, counters that reconcile, every observation
cited for its own key and side, and reports (every CSV export and the PDF) without raw personal data.

The live ground truth is compared with each key's stored final result; ``first_findings`` (the
findings a recheck replaced) are not kept by the store, so only ``first_outcome`` is checked.

A pair of its own has a key of three parts, a side that says when its answer was cut and its keys in a
file: its live run, its run from captured samples and its results judged again from their evidence agree.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiohttp import web

from deltasleuth.connectors.files import SampleWriter
from deltasleuth.core.types import SideStateKind
from deltasleuth.reports.csv import KINDS, write_csv
from deltasleuth.reports.pdf import pdf_report
from deltasleuth.runner import create_run
from deltasleuth.runner.compare import rederive_all, side_state
from deltasleuth.runner.counters import verdict
from deltasleuth.store import repo
from mocks.fixtures import write_fixtures
from mocks.registry import PAIRS, pair_dir
from tests.conftest import TESTER

from .conftest import Stack, finish, pdf_text, raw_keys, raw_pii, store_spec, table_spec

pytestmark = [pytest.mark.e2e, pytest.mark.db]
FINDING_FIELDS = "taxonomy_code change_type path_pattern severity rule_id rule_classification".split()
DEFAULT = {"outcome": "MATCH", "verdict": "MATCH", "findings": []}


def finding_problems(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> list[str]:
    """expected.json's rule: every actual finding matches an entry; an entry matches ``count``
    findings when given, at least one otherwise; ``[]`` means none at all."""
    matched = [0] * len(expected)
    problems = []
    for f in actual:
        hits = [i for i, e in enumerate(expected) if all(f[k] == e[k] for k in FINDING_FIELDS if k in e)]
        problems += [] if hits else [f"unexpected finding {[f[k] for k in FINDING_FIELDS]}"]
        for i in hits:
            matched[i] += 1
    for entry, n in zip(expected, matched, strict=True):
        if n != entry.get("count", max(n, 1)):
            problems.append(f"expected {entry.get('count', '>= 1')} finding(s) like {entry}, got {n}")
    return problems


def key_problems(
    row: dict[str, Any], exp: dict[str, Any], observed: list[dict[str, Any]], live: bool
) -> list[str]:
    actual = {k: row[k] for k in ("outcome", "inconclusive_reason", "drift_reason", "suppressed_count")}
    actual["verdict"] = verdict(row, row["findings"]).value
    problems = [f"{k}: {v!r} != {exp[k]!r}" for k, v in actual.items() if k in exp and v != exp[k]]
    if (row["confirmation"] == "rechecked") != (live and exp.get("rechecked", False)):
        problems.append(f"confirmation {row['confirmation']}")  # a recheck runs only where it is due
    if live and row["first_outcome"] != exp.get("first_outcome", row["outcome"]):
        problems.append(f"first_outcome {row['first_outcome']}")
    for side, attempts in exp.get("attempts", {}).items() if live else ():
        first = [o["attempts"] for o in observed if (o["pass_no"], o["side"]) == (1, side)]
        problems += [] if first == [attempts] else [f"side {side} attempts {first}"]
    for side in "ab":  # each side's observation id cites a read of this key on that side
        if row[f"obs_{side}_id"] not in [o["obs_id"] for o in observed if o["side"] == side] + [None]:
            problems.append(f"obs_{side}_id does not cite a read of side {side}")
    return problems + finding_problems(row["findings"], exp["findings"])


@pytest.mark.parametrize("mode", ["live", "file"])
@pytest.mark.parametrize("pair", PAIRS)
async def test_the_seeded_discrepancies_and_nothing_else(
    serve: Any, make_stack: Any, tmp_path: Path, pair: str, mode: str
) -> None:
    expected = json.loads((pair_dir(pair) / "expected.json").read_text())
    samples = tmp_path / "samples" if mode == "file" else None
    if samples:
        write_fixtures(pair, samples)
    async with make_stack(serve(pair).targets()).context() as ctx:
        spec_id = await store_spec(ctx, pair_dir(pair) / "spec.yaml")
        run = await finish(ctx, await create_run(ctx, spec_id, "batch", samples_dir=samples, who=TESTER))
        run_id = run["run_id"]
        async with ctx.pool.connection() as conn:
            results = await repo.pages(repo.list_results, conn, run_id)
            findings = await repo.pages(repo.list_findings, conn, run_id)
            observations = await repo.list_observations(conn, run_id)
            causes = await repo.list_root_causes(conn, run_id)
        raw = await raw_keys(ctx, run_id)
        exports = {kind: await write_csv(ctx, run_id, None, kind) for kind in KINDS}
        pdf = await pdf_report(ctx, run_id)
    keys = (pair_dir(pair) / "keys.txt").read_text().split()
    expect = {key: expected["keys"].get(key, {}).get(mode, DEFAULT) for key in keys}
    by_key = {
        raw[r["key_masked"]]: r | {"findings": [f for f in findings if f["key_masked"] == r["key_masked"]]}
        for r in results
    }
    problems = [
        f"{key}: {p}"
        for key in keys
        for p in key_problems(
            by_key[key], expect[key], [o for o in observations if raw[o["key_masked"]] == key], mode == "live"
        )
    ]
    assert problems == [] and len(results) == len(keys)

    c = run["counters"]
    assert {k: v for k, v in c["by_outcome"].items() if v} == dict(
        Counter(e["outcome"] for e in expect.values())
    )
    verdicts = Counter(e["verdict"] for e in expect.values())
    assert (c["match"], c["different"], c["indeterminate"]) == (
        verdicts["MATCH"], verdicts["DIFFERENT"], verdicts["INDETERMINATE"],
    )  # fmt: skip
    assert (c["cancelled"], c["outstanding"], run["status"]) == (0, 0, "completed")
    assert all(i["holds"] for i in c["reconciliation"])
    assert {f["root_cause_id"] for f in findings} - {None} == {rc["root_cause_id"] for rc in causes}
    assert b"RC-" in exports["discrepancies"] and pdf.startswith(b"%PDF-")
    assert [rc["root_cause_id"] for rc in causes if rc["root_cause_id"] not in pdf_text(pdf)] == []

    # Raw keys are sealed in the store (the fetch opens them); nothing it outputs is raw.
    stored = json.dumps([*results, *findings, *causes], default=str)
    published = [stored, pdf_text(pdf), *(data.decode() for data in exports.values())]
    pii = raw_pii(pair)
    assert len(pii) > 50 and [v[:3] for v in pii if any(v in text for text in published)] == []


# --- a key of three parts, a flagged cut and a key file (docs/mvp/CONTRACTS.md §4) -----------------------

# An item's code, and the region and shelf it stands on: side a is asked by the last two, side b by the first.
SHELVES = {("c1", "eu", "s1"): [1, 2], ("c2", "eu", "s/2"): [3], ("c3", "us", "s3"): [4, 5]}


def _shelf(side: str, key: tuple[str, str, str]) -> dict[str, Any]:
    """What a side answers: side b counts one more of c2, and cuts c3's answer short, saying so."""
    cut = side == "b" and key[0] == "c3"
    counts = [n + (side == "b" and key[0] == "c2") for n in SHELVES[key]]
    return {"counts": counts[: 1 if cut else None], "page": {"size": 50, "cut": cut}}


async def test_three_part_keys_flags_and_a_key_file(
    make_stack: Callable[..., Stack], tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A key file gives the three-part keys of a run between two services, each once and without the rows
    its filter leaves out: side a takes two parts in its path, side b the third in its body. The key whose
    answer side b flags as cut is INCONCLUSIVE ``truncated``, never a difference. A run from captured samples
    of the same answers, and each stored result judged again from its evidence, give the live outcomes."""
    asked: list[str] = []

    async def by_shelf(request: web.Request) -> web.Response:
        asked.append(request.raw_path)
        where = (request.match_info["region"], request.match_info["shelf"])
        return web.json_response(_shelf("a", next(key for key in SHELVES if key[1:] == where)))

    async def by_code(request: web.Request) -> web.Response:
        code = (await request.json())["id"]
        asked.append(code)
        return web.json_response(_shelf("b", next(key for key in SHELVES if key[0] == code)))

    app = web.Application()
    app.router.add_get("/a/{region}/shelves/{shelf}", by_shelf)
    app.router.add_post("/b/items", by_code)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    exports = tmp_path_factory.mktemp("exports").resolve()  # files.roots_under: outside the stack's own
    (exports / "keys").mkdir()
    listed = [(*key, "y") for key in SHELVES]
    rows = [listed[2], listed[0], listed[1], listed[0], ("c9", "zz", "s0", "n")]
    (exports / "keys" / "keys.csv").write_text("".join("|".join(row) + "\n" for row in rows))
    columns, parts = ["code", "region", "shelf", "live"], ["code", "region", "shelf"]
    file = {"path": "keys.csv", "format": "csv"}
    file |= {"csv": {"columns": columns, "delimiter": "|", "header": False}}
    table = {"name": "shelf_keys", "keyColumns": parts, "where": "live = 'y'", "file": file}
    key = {"name": "item", "type": "composite", "parts": parts, "class": "business_code",
           "pattern": "^[a-z0-9/]+$"}  # fmt: skip
    body = {"id": "{{ keyParts.code }}", "fields": ["counts"]}
    posted = {"method": "POST", "path": "/b/items", "body": body}
    sides = {
        "a": {"role": "legacy", "target": "svc", "truncation": {"itemsPath": "$.counts", "cap": 50},
              "request": {"method": "GET", "path": "/a/{{ keyParts.region }}/shelves/{{ keyParts.shelf }}"}},
        "b": {"role": "new", "target": "svc", "request": posted,
              "truncation": {"itemsPath": "$.counts", "cap": 50, "flagPaths": ["$.page.cut"]}},
    }  # fmt: skip
    key |= {"source": {"kind": "table", "target": "keys", "table": table}}
    doc = table_spec(sides=sides, key=key, security={"unlisted": "clear"})
    targets = {
        "keys": {"engine": "files", "root": str(exports / "keys"), "settle_s": 0, "allow_writable": True},
        "svc": {"base_url": f"http://127.0.0.1:{runner.addresses[0][1]}", "allow_private_network": True},
    }
    samples = SampleWriter(tmp_path / "samples")
    for parted in SHELVES:
        for side in "ab":
            samples.write("|".join(parted), side, json.dumps(_shelf(side, parted)).encode())
    files = {"roots_under": str(exports), "memory": "256MB", "spill": "1GB"}
    try:
        async with make_stack(yaml.safe_dump({"targets": targets}), files=files).context() as ctx:
            spec_id = await store_spec(ctx, doc)
            live = await finish(ctx, await create_run(ctx, spec_id, "batch", who=TESTER))
            filed = await finish(
                ctx, await create_run(ctx, spec_id, "batch", samples_dir=tmp_path / "samples", who=TESTER)
            )
            async with ctx.pool.connection() as conn:
                spec, caps = await ctx.spec(conn, spec_id), ctx.capabilities(live)
                stored = await repo.pages(repo.list_results, conn, live["run_id"])
                again = await rederive_all(ctx, conn, spec, live["run_id"], stored, caps)
                replayed = await repo.pages(repo.list_results, conn, filed["run_id"])
                [cut] = [row for row in stored if row["outcome"] == "INCONCLUSIVE"]
                flagged = await side_state(ctx, conn, spec, live["run_id"], cut["obs_b_id"], caps)
    finally:
        await runner.cleanup()
    expected = {
        "c1|eu|s1": ("MATCH", None),
        "c2|eu|s/2": ("MISMATCH", None),
        "c3|us|s3": ("INCONCLUSIVE", "truncated"),  # its shorter list is no difference
    }
    assert {r["key_masked"]: (r["outcome"], r["inconclusive_reason"]) for r in stored} == expected
    assert {r["key_masked"]: (r["outcome"], r["inconclusive_reason"]) for r in replayed} == expected
    assert {r.key: (r.outcome.value, r.inconclusive_reason) for r in again} == expected
    assert flagged is not None and (flagged[1].kind, flagged[1].reason, flagged[1].detail) == (
        SideStateKind.INCOMPLETE,
        "truncated",
        "the truncation flag $.page.cut is true",
    )
    for run in (live, filed):
        assert (run["status"], run["params"]["keys_source"]) == ("completed", "table")
        assert (run["counters"]["total"], run["counters"]["api_uncomparable"]) == (3, 1)
    assert {"/a/eu/shelves/s%2F2", "c2"} <= set(asked)  # a part with a slash is one path segment
