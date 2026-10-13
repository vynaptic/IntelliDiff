"""Report fixtures: a Context on a fresh database, and runs seeded the way the runner stores them
(evidence blob, observation rows, masked result and findings, root causes, counters)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from psycopg_pool import AsyncConnectionPool

from deltasleuth.config import AuthSettings, OidcSettings, PackSettings, Settings
from deltasleuth.context import Context
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.classify.rootcause import groups
from deltasleuth.core.spec import TargetRegistry, compile_spec
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    CompiledSpec,
    KeyResult,
    Observation,
    Side,
    Target,
    TargetCapabilities,
)
from deltasleuth.runner.compare import save_pass
from deltasleuth.runner.counters import count_cycle
from deltasleuth.runner.recheck import RecheckPolicy
from deltasleuth.security.signing import PackSigner
from deltasleuth.security.tokenise import Tokeniser, mask_finding, mask_key
from deltasleuth.store import repo
from deltasleuth.store.db import migrate
from tests.conftest import TEST_KEYRING, TESTER, create_run, evidence_store

SECRET_NAME = "Alice Secretname"  # noqa: S105 - a tokenised value that must never reach a report
TODAY = date(2026, 9, 24)
SPEC_YAML = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: unit-reports, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key:
  name: item_id
  type: string
  pattern: "^K[0-9]{3}$"
  class: business_code
  source: {kind: list, keys: [K001]}
sides:
  a: {role: legacy, target: svc-a, request: {method: GET, path: "/a/{{ key }}"}, notFound: {status: [404]},
      truncation: {itemsPath: "$.tags", cap: 50}}
  b: {role: new, target: svc-b, request: {method: GET, path: "/b/{{ key }}"}, notFound: {status: [404]},
      truncation: {itemsPath: "$.tags", cap: 50}}
compare:
  arrays: [{path: "$.tags", mode: set}]
  fields: [{path: "$.name", normalize: [trim]}, {path: "$.status", severity: HIGH}]
knownDiffs:
  - {id: KD-UNIT-1, when: "path == '$.status' && a == 'OLD' && b == 'NEW'", classification: accepted,
     reason: status vocabulary renamed, ticket: UNIT-1}
security:
  unlisted: tokenize
  tokenize: [{path: "$.name", class: name}]
  clear: ["$.status", "$.tags[*]", "$.id"]
recheck: {delay: 1s, times: 1}
"""
GRAPH_SPEC = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: unit-graph, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key: {name: link, type: composite, parts: [entity_code, id_type, id_value], class: identifier}
sides:
  a: &side {role: legacy, target: db-a, table: {name: links, keyColumns: [entity_code, id_type, id_value],
                                                columns: [entity_code]}}
  b: {<<: *side, role: new, target: db-b}
graph: {members: [entity_code], groups: [id_type, id_value]}
compare: {}
recheck: {delay: 0s, times: 1}
"""
GRAPH = {"groups_a": 3, "groups_b": 3, "groups_equal": 2, "groups_different": 1,
         "split": 1, "merge": 0, "rearrange": 0, "oversized": 0}  # fmt: skip
ABSENCE = TargetCapabilities(authoritative_absence=AuthoritativeAbsence(status=(404,)))
TARGETS = TargetRegistry(
    {t: Target(t, "http://127.0.0.1:9", capabilities=ABSENCE) for t in ("svc-a", "svc-b")}
    | {t: Target(t, "", dsn_ref="file:/unused", capabilities=ABSENCE) for t in ("db-a", "db-b")}
)
Sample = Any  # a JSON body (HTTP 200), (status, body), raw bytes, or an error class such as "timeout"


def doc(
    key: str, *, name: str = SECRET_NAME, status: str = "ACTIVE", tags: tuple[str, ...] = ("x", "y")
) -> dict:
    return {"id": key, "name": name, "status": status, "tags": list(tags)}


def unit_spec(**changes: Any) -> CompiledSpec:
    raw = yaml.safe_load(SPEC_YAML)
    raw.update(changes)
    return compile_spec(raw, targets=TARGETS)


@pytest.fixture
async def ctx(pg_dsn: str, tmp_path: Path) -> AsyncIterator[Context]:
    pool = AsyncConnectionPool(pg_dsn, open=False, max_size=4)
    await pool.open(wait=True)
    await migrate(pool)
    settings = Settings("file:/unused", tmp_path / "evidence", tmp_path / "ws", tmp_path / "targets.yaml")
    evidence = evidence_store(settings.evidence_dir)
    yield Context(settings, pool, TARGETS, Tokeniser(b"k" * 32), evidence, None, None, TEST_KEYRING)
    await pool.close()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """A fresh EC P-256 signing key, PEM PKCS#8 unencrypted, outside the evidence and workspace dirs."""
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "secrets" / "pack-signing-key"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return path


def with_packs(ctx: Context, key_file: Path) -> Context:
    """``ctx`` in oidc mode with evidence packs (installation test-1) signed by ``key_file``."""
    oidc = OidcSettings("http://127.0.0.1:1", "deltasleuth", {"rule_approver": ("G-approver",)},
                        client_secret_ref="file:/unused")  # fmt: skip # noqa: S106 - a reference, never read
    packs = PackSettings("test-1", f"file:{key_file}")
    settings = replace(ctx.settings, auth=AuthSettings("https://ds.test", oidc=oidc), packs=packs)
    return replace(ctx, settings=settings, signer=PackSigner.load(f"file:{key_file}"))


@pytest.fixture
def pack_ctx(ctx: Context, key_file: Path) -> Context:
    return with_packs(ctx, key_file)


def observation(
    key: str, side: Side, sample: Sample, n: int, pass_no: int = 1, headers: Mapping[str, str] | None = None
) -> Observation:
    at = f"2026-09-24T1{pass_no}:{n // 60:02d}:{n % 60:02d}.000000Z"
    if isinstance(sample, str):
        return Observation(key, side, None, None, at, error_class=sample, attempts=3)
    status, body = sample if isinstance(sample, tuple) else (200, sample)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Observation(key, side, status, raw, at, latency_ms=10.0 + n, headers=dict(headers or {}))


async def compare_key(
    ctx: Context, conn: Any, spec: CompiledSpec, run_id: str, key: str, pair: tuple[Sample, Sample],
    *, n: int = 0, cycle: int = 1, pass_no: int = 1, headers: Mapping[str, str] | None = None,
    **result_changes: Any,
) -> KeyResult:  # fmt: skip
    """Read, observe, compare and store one key as the runner does (evidence first), each side's
    response with ``headers``."""
    states, reads = [], {}
    for side, sample in zip(Side, pair, strict=True):
        obs = reads[side] = ctx.evidence.put_blob(observation(key, side, sample, n, pass_no, headers))
        cfg = spec.sides[side]
        states.append(observe(obs, cfg, spec, ctx.targets.get(cfg.target).capabilities))
    result = compare_pair(key, *states, spec, today=TODAY, tokeniser=ctx.tokeniser)
    result = replace(result, obs_a_id=reads[Side.A].obs_id, obs_b_id=reads[Side.B].obs_id, **result_changes)
    await save_pass(ctx, conn, spec, run_id, cycle, pass_no, result, reads, await ctx.sealer(conn, run_id))
    return result


async def store_counters(conn: Any, spec: CompiledSpec, run_id: str, cycle: int, live: bool) -> None:
    """The cycle's counters as the runner computes them from the stored rows."""
    results, _ = await repo.list_results(conn, run_id, cycle, limit=10_000)
    findings, _ = await repo.list_findings(conn, run_id, cycle, limit=10_000)
    by_key = {r["key_masked"]: [f for f in findings if f["key_masked"] == r["key_masked"]] for r in results}
    total = sum((await repo.cycle_counts(conn, run_id, cycle))["keys"].values())
    due = RecheckPolicy.from_spec(spec.raw).outcomes
    counted = count_cycle(results, by_key, total=total, live=live, recheck_outcomes=due)
    await repo.set_counters(conn, run_id, counted | {"cycle": cycle})


async def store_root_causes(ctx: Context, conn: Any, spec: CompiledSpec, run_id: str, cycle: int,
                            results: list[KeyResult]) -> None:  # fmt: skip
    grouped = groups(results, spec=spec, tokeniser=ctx.tokeniser)
    masked = [(rc, f, mask_finding(f, spec, ctx.tokeniser)) for rc, fs in grouped for f in fs]
    by_masked = {(m["key"], m["json_pointer"], f.change_type.value): rc.root_cause_id for rc, f, m in masked}
    rows, _ = await repo.list_findings(conn, run_id, cycle, limit=10_000)
    links = {
        row["finding_id"]: by_masked[ident]
        for row in rows
        if (ident := (row["key_masked"], row["json_pointer"], row["change_type"])) in by_masked
    }
    await repo.replace_root_causes(conn, run_id, cycle, [rc for rc, _ in grouped], links)


async def seed_run(
    ctx: Context, samples: Mapping[str, tuple[Sample, Sample]], *, spec: CompiledSpec | None = None,
    mode: str = "batch", status: str = "completed", live: bool = False, stop_reason: str | None = None,
    tables: Mapping[str, Any] | None = None, scan: Mapping[str, Any] | None = None,
) -> str:  # fmt: skip
    """A run over ``samples`` (key -> (side a, side b)), stored as the runner stores it; a table run
    also has its column plan (``tables``) and, when it scanned, its ``scan`` state."""
    spec = spec or unit_spec()
    async with ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, spec, created_by=TESTER.actor)
        keys = {key: mask_key(key, spec, ctx.tokeniser) for key in samples}
        params = {"capabilities": ctx.declared(spec)} | ({} if live else {"samples_dir": "samples"})
        params |= {"tables": tables} if tables else {}
        run_id, _ = await create_run(ctx, conn, spec_id, mode, keys, params, scan)
        results = [
            await compare_key(ctx, conn, spec, run_id, key, pair, n=n)
            for n, (key, pair) in enumerate(samples.items())
        ]
        await store_root_causes(ctx, conn, spec, run_id, 1, results)
        await store_counters(conn, spec, run_id, 1, live)
        await repo.set_run_status(conn, run_id, status, stop_reason=stop_reason)
    return run_id


async def seed_graph_run(ctx: Context, rows: list[dict[str, Any]]) -> str:
    """A finished graph parity run: its scan's ``GRAPH`` counts and L2 ``rows``."""
    scan = {"done": True, "after": ["e9|phone|p9"], "rows_a": 6, "rows_b": 6, "skipped_a": 0, "skipped_b": 0,
            "matched": 4, "candidates": 2, "graph": GRAPH}  # fmt: skip
    spec = compile_spec(yaml.safe_load(GRAPH_SPEC), targets=TARGETS)
    run_id = await seed_run(ctx, {}, spec=spec, live=True, scan=scan)
    async with ctx.pool.connection() as conn:
        await repo.replace_graph_diffs(conn, run_id, 1, rows)
    return run_id


def graph_row(code: str, label: str, **fields: Any) -> dict[str, Any]:
    """A ``ds.graph_diffs`` row as L2 writes it (the label masked)."""
    return {
        "code": code,
        "label": label,
        "count_a": 2,
        "count_b": 1,
        "only_a": 1,
        "only_b": 0,
        "detail": {},
    } | fields
