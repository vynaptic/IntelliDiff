"""What a pack holds: exactly its layout; facts whose hashes the audit rows
carry; a manifest and a certificate that state everything as observed; params hashed as stored; nothing
per key but masked values (the canary, PDF streams decompressed), masked keys only in the sheet, the
key.view rows and the AI answers; this cycle's AI answers only; and what the purge keeps and deletes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
import yaml
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from psycopg_pool import AsyncConnectionPool
from typer.testing import CliRunner

from deltasleuth import approvals
from deltasleuth.auth.principal import WORKER
from deltasleuth.cli.main import app
from deltasleuth.config import FilesSettings, Settings
from deltasleuth.connectors.tables import manifest as files_manifest
from deltasleuth.context import Context
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.spec import compile_spec
from deltasleuth.reports import pack, pack_verify
from deltasleuth.reports.csv import COVERAGE_COLUMNS, UNKEYED_COLUMNS, write_csv
from deltasleuth.reports.render import Document
from deltasleuth.runner import purge, runs
from deltasleuth.runner.purge import CyclePurged
from deltasleuth.security.signing import PackSigner
from deltasleuth.security.tokenise import Tokeniser, mask_key
from deltasleuth.store import repo
from deltasleuth.store.db import migrate
from tests.conftest import BUILDS, TEST_KEYRING, TESTER, encryption, evidence_store, licensed_once

from .conftest import SECRET_NAME, TARGETS, doc, unit_spec, with_packs
from .packs import (
    RAW_KEY,
    files_of,
    grant,
    listed_spec,
    run_step,
    sampled_cycle,
    samples,
    signed_cycle,
    signed_in,
    unzip,
)

pytestmark = pytest.mark.db
TARGETS_FILE = """\
targets:
  svc: {base_url: "http://127.0.0.1:9/", allow_private_network: true, allow_insecure_http: true}
"""
LISTED = [
    "VERIFY.txt", "accuracy/attempts.json", "adjudication/attempts.json", "adjudication/score.json",
    "adjudication/sheet.csv", "annex/ai_answers.json", "audit/chain.tsv", "certificate.pdf",
    "csv/coverage.csv", "csv/graph.csv", "csv/root_causes.csv", "facts/attests.json", "facts/plan.json",
    "facts/spec.json", "manifest.json",
]  # fmt: skip


async def _audited(ctx: Context, action: str, obj: str) -> list[dict[str, Any]]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT detail FROM ds.audit_log WHERE action = %s AND object = %s ORDER BY id", (action, obj)
        )
        return [detail for (detail,) in await cur.fetchall()]


@pytest.mark.parametrize("build", BUILDS, indirect=True)
async def test_a_pack_holds_its_layout_and_facts_bound_to_audited_hashes(
    build: str, pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents: list[Document] = []
    render = pack.render_pdf

    def kept(document: Document) -> bytes:
        documents.append(document)
        return render(document)

    monkeypatch.setattr(pack, "render_pdf", kept)
    signed = await signed_cycle(pack_ctx, pairs=samples(52), adjudicate=True)
    signer = pack_ctx.signer
    assert signer is not None
    olga = signed.people["olga"]
    first = await pack.issue(pack_ctx, signed.run_id, 1, who=olga)
    second = await pack.issue(pack_ctx, signed.run_id, 1, who=olga)  # a later pack lists the earlier
    found, data = await pack.stored(pack_ctx, second["pack_id"])
    files, pack_id = files_of(data), second["pack_id"]
    manifest = json.loads(files["manifest.json"])
    async with pack_ctx.pool.connection() as conn:
        signoff = await repo.get_approval(conn, signed.signoff_id)
        adjudication = await repo.get_adjudication(conn, signed.adjudication_id)  # type: ignore[arg-type]
        spec_row = await repo.get_spec(conn, signed.spec_id)
        head = await repo.audit_head(conn)
    assert signoff is not None and adjudication is not None and spec_row is not None and head is not None

    def sha(name: str) -> str:
        return hashlib.sha256(files[name]).hexdigest()

    # Exactly the layout, in one folder, and the sums in byte order.
    assert sorted(files) == sorted([*LISTED, *pack.UNLISTED])
    assert files["SHA256SUMS"].decode() == "".join(f"{sha(name)}  {name}\n" for name in LISTED)
    assert files["signing-key.pem"] == signer.public_pem
    assert (found["sums_sha256"], found["zip_sha256"]) == (
        sha("SHA256SUMS"),
        hashlib.sha256(data).hexdigest(),
    )
    # The facts hash to what the audit rows granted and created.
    [grant_row] = [d for d in await _audited(pack_ctx, "approval.grant", signed.signoff_id)]
    [spec_grant] = [d for d in await _audited(pack_ctx, "approval.grant", signed.spec_approval_id)]
    [created] = await _audited(pack_ctx, "create_run", signed.run_id)
    assert sha("facts/attests.json") == signoff["attests_sha256"] == grant_row["attests_sha256"]
    assert json.loads(files["facts/attests.json"]) == signoff["attests"]
    assert sha("facts/spec.json") == spec_row["content_hash"] == spec_grant["content_hash"]
    assert sha("facts/plan.json") == created["plan_sha256"]
    attested = signoff["attests"]["adjudication"]
    assert sha("adjudication/sheet.csv") == attested["sheet_sha256"] == adjudication["sheet_sha256"]
    assert sha("adjudication/score.json") == attested["score_sha256"] == adjudication["score_sha256"]
    [attempt] = json.loads(files["adjudication/attempts.json"])
    assert (attempt["adjudication_id"], attempt["passed"], attempt["adjudicated"]) == (
        signed.adjudication_id, True, 50,
    )  # fmt: skip
    # The audit segment: each object's rows from its first up to the head, as the chain stores them.
    objects = manifest["audit"]["objects"]
    assert {signed.run_id, signed.signoff_id, signed.spec_approval_id, str(signed.spec_id), first["pack_id"],
            signed.adjudication_id} <= set(objects)  # fmt: skip
    assert any(o.startswith("W-") for o in objects)  # the grants' sessions
    async with pack_ctx.pool.connection() as conn:
        segment = await repo.audit_segment(conn, objects, manifest["audit"]["head"]["seq"])
    assert files["audit/chain.tsv"].decode() == "".join(
        f"{r['seq']}\t{r['prev']}\t{r['obj_prev']}\t{r['hash']}\t{r['object']}\t{r['text']}\n"
        for r in segment
    )
    assert any('"action": "pack.issue"' in r["text"] and r["object"] == first["pack_id"] for r in segment)
    # The manifest.
    fields = {name: manifest[name] for name in ("format", "procedure", "pack_id", "installation", "claim")}
    assert fields == {"format": "deltasleuth-pack/1", "procedure": "deltasleuth-pack/1 verification",
                      "pack_id": pack_id, "installation": "test-1", "claim": "observed"}  # fmt: skip
    assert manifest["claim_text"] == pack.PROVES.format(name="test-1", fp=signer.fingerprint)
    assert manifest["issued_at_epoch"] == int(second["issued_at"].timestamp())
    assert manifest["issued_by"] == {"actor": olga.actor, "name": "olga"}
    assert manifest["signing_key"]["fingerprint"] == signer.fingerprint == second["key_fingerprint"]
    assert manifest["subject"] == {
        "run_id": signed.run_id, "cycle": 1, "basis": "census",
        "spec": {"spec_id": signed.spec_id, "name": "unit-reports", "version": "1.0.0",
                 "content_hash": signed.spec.content_hash},
        "sides": {"a": {"role": "legacy", "target": "svc-a", "relation": None},
                  "b": {"role": "new", "target": "svc-b", "relation": None}},
    }  # fmt: skip
    assert manifest["census"] == signoff["attests"]["census"] and manifest["results"]["per_key_results"] == 52
    # A census pack changes only by its basis and its attests' sample; format
    # 3 adds its labelling, none here.
    assert signoff["attests"]["format"] == 3 and signoff["attests"]["sample"] is None
    assert signoff["attests"]["accuracy"] is None and json.loads(files["accuracy/attempts.json"]) == []
    assert manifest["accuracy"] == {"attested": False, "draws": 0, "labelled": 0}
    assert "sample" not in manifest and "later_draws" not in manifest
    assert files["csv/coverage.csv"].decode().split("\r\n")[0].split(",") == list(COVERAGE_COLUMNS)
    spec_step, signoff_step = (d["steps"] for d in manifest["decisions"])
    assert [(s["name"], s["step"], s["of"], s["issuer"]) for s in spec_step + signoff_step] == [
        ("carol", 1, 1, "https://idp.test"), ("erin", 1, 1, "https://idp.test"),
    ]  # fmt: skip
    assert signoff_step[0]["session_id"] == signed.people["erin"].session_id
    assert signoff_step[0]["auth_time"] == signed.people["erin"].auth_time.isoformat()  # type: ignore[union-attr]
    assert manifest["adjudication"] == {"attested": attested, "draws": 1, "scored": 1, "sampled": 50}
    assert manifest["audit"]["head"]["seq"] == second["audit_seq"] <= head["seq"]
    assert manifest["retention"]["class"] == "standard" and manifest["retention"]["held"] is True
    assert [p["pack_id"] for p in manifest["previous_packs"]] == [first["pack_id"]]
    # The Enterprise licence signed_cycle installed; an open build's pack names none.
    named = (await licensed_once(pack_ctx)).licence
    assert (named is None) == (build == "open_build")
    assert manifest["licence"] == (named and {
        "licence_id": named.licence_id, "customer": "Example Bank plc", "order": "ORD-2026-014",
        "installation_id": named.installation_id, "tier": "enterprise", "pairs": None,
        "starts": named.starts.isoformat(), "ends": named.ends.isoformat(), "state": "valid",
        "key": named.key,
    })  # fmt: skip
    assert manifest["files"] == {name: sha(name) for name in LISTED if name != "manifest.json"}
    assert found["manifest"] == manifest and files["manifest.json"].endswith(b"\n")
    # VERIFY.txt: two filled lines, then the procedure the key register files.
    verify = files["VERIFY.txt"].decode().split("\n", 2)
    assert verify[0] == (
        f"DeltaSleuth evidence pack {pack_id} of installation test-1. Unzip it, cd {pack_id}/, then run in sh"
        " the copy of the procedure below filed with your key register; its step 0 reads line 2:"
    )
    assert verify[1] == (
        f"PACK={pack_id} SIGNOFF={signed.signoff_id} SPECAPP={signed.spec_approval_id}"
        f" HEAD={manifest['audit']['head']['seq']} UPGRADE=0"
    )
    assert verify[2].encode() == pack.key_files("test-1", signer)["procedure.txt"]
    # The record and the audit row of the issue.
    assert (second["status"], second["issued_by"], second["signoff_id"], second["adjudication_id"]) == (
        "issued", olga.actor, signed.signoff_id, signed.adjudication_id,
    )  # fmt: skip
    [issued] = await _audited(pack_ctx, "pack.issue", pack_id)
    assert issued == {
        "signoff_id": signed.signoff_id, "sums_sha256": sha("SHA256SUMS"),
        "manifest_sha256": sha("manifest.json"), "zip_sha256": hashlib.sha256(data).hexdigest(),
        "key": signer.fingerprint, "audit_head": manifest["audit"]["head"],
        "licence_id": named and named.licence_id,
    }  # fmt: skip
    # The certificate: one document, every section observed, the signature's reach verbatim.
    certificate = documents[-1]
    assert certificate.title == f"DeltaSleuth evidence pack {pack_id}"
    assert certificate.meta.startswith("Claim level: observed\n")
    assert all(section.title.endswith(" (observed)") for section in certificate.sections)
    titled = {section.title: section for section in certificate.sections}
    assert titled["What the signature proves (observed)"].lines == [manifest["claim_text"]]
    claim = titled["Claim (observed)"].lines
    if named is None:
        assert len(claim) == 4 and "icence" not in "".join(claim)
    else:
        assert claim[4:] == [
            f"Licence {named.licence_id} of Example Bank plc (order ORD-2026-014): enterprise,"
            f" {named.starts} to {named.ends}, installation {named.installation_id}"
        ]
    assert titled["Adjudication (observed)"].lines == [
        f"draws: 1 (1 scored); attested {signed.adjudication_id}", "sampled 50 of 52 per-key results",
        "50 adjudicated, 50 agreed with the engine: passed",
    ]  # fmt: skip
    assert titled["Retention (observed)"].lines == [
        f"per-key detail stays in the store until {manifest['retention']['per_key_detail_until']}, or later"
        f" while run {signed.run_id} is held; the stored pack is deleted when the cycle is purged: archive"
        " this file"
    ]
    assert titled["Labelled accuracy (observed)"].lines == [
        "No labelled accuracy is attested: which side is right was not measured."
    ]
    assert not {"Frame (observed)", "Sample and bounds (observed)"} & set(titled) and not certificate.warning
    assert titled["Census (observed)"].lines[0].startswith(
        "Side a (legacy, target svc-a): a paginated walk between "
    ) and titled["Census (observed)"].lines[1] == (
        "Side b (new, target svc-b): not counted (a count of side b cannot show keys side b lost)"
    )
    assert titled["Results (observed)"].header[:2] == ("Counter", "Value")  # pdf._counters_grid
    assert titled["How to verify (observed)"].lines == files["VERIFY.txt"].decode().splitlines()
    assert files["certificate.pdf"].startswith(b"%PDF")


async def test_no_pre_check_row_is_in_a_pack_s_segment(pack_ctx: Context) -> None:
    """A table pre-check is observed, never evidence. Its audit rows name its own
    id, which no row of a run names: a pack issued after one holds nothing of it."""
    signed = await signed_cycle(pack_ctx)
    precheck_id = repo.new_precheck_id()
    made = {"precheck_id": precheck_id, "source": "specs", "level": "columns", "skipped": 0}
    made |= {"request": {"spec_ids": [signed.spec_id]}, "evaluation": False, "created_by": TESTER.actor}
    pair = {"n": 1, "spec_id": signed.spec_id, "label": "customer-profile", "target_a": "db-a"}
    pair |= {"relation_a": "customers", "target_b": "db-b", "relation_b": "customers"}
    async with repo.audited(pack_ctx.pool) as (conn, audit):
        await repo.insert_precheck(conn, made, [pair])
        await audit(TESTER, "precheck.create", precheck_id, {"source": "specs", "spec_ids": [signed.spec_id]})
        await repo.cancel_precheck(
            conn, precheck_id, TESTER.actor, "not checked: the pre-check was cancelled"
        )
        await audit(TESTER, "precheck.cancel", precheck_id, {"pending": 1, "running": 0})
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    files = files_of(data)
    objects = json.loads(files["manifest.json"])["audit"]["objects"]
    assert objects and not [found for found in objects if found.startswith("PC-")]
    texts = [name for name in files if name.endswith((".tsv", ".json", ".csv", ".txt"))]
    assert "audit/chain.tsv" in texts and not [
        name for name in texts if b"PC-" in files[name] or b"precheck" in files[name]
    ]


async def test_drift_rows_pass_the_pack_gate(pack_ctx: Context) -> None:
    """What a pack holds of the schema guard is the run's ``schema.drift`` and
    ``paused`` audit rows in its segment, which pass the raw-value gate by construction, under a key
    pattern of digits too: counts are numbers, kinds member names, and their strings closed codes, an id
    and hashes. Nothing issued for a cycle is built from the run's ``guard``: ``coverage.csv`` is byte for
    byte the same with and without one, and two packs of one cycle issued before and after a later change
    hold the same files but the manifest, the segment and what cites them."""
    signed = await signed_cycle(pack_ctx, spec=listed_spec(pii=True), pairs=samples(pii=True))
    olga, run_id = signed.people["olga"], signed.run_id
    coverage = await write_csv(pack_ctx, run_id, 1, "coverage", keyed=False)
    first = await pack.issue(pack_ctx, run_id, 1, who=olga)
    # A column named as a key of the spec is named, and a declared type: neither may reach a pack.
    shape = {"kind": "table", "key": ["id"], "columns": {RAW_KEY: ["CANARY-TYPE(34)", "text", True]}}
    held = {"relation": "public.items", "roles": {"id": "key", RAW_KEY: "compared"}, "common": True}
    held |= {"planned": shape, "sha256": "1" * 64, "seen": None, "seen_sha256": "2" * 64}
    held["changes"] = [
        {"kind": "column_missing", "breaking": True, "column": RAW_KEY, "was": None, "now": None}
    ]
    checked = {
        "cycle": 2,
        "at": "2026-10-07T10:02:11.000000Z",
        "where": "cycle",
        "error_type": None,
        "side": None,
    }
    guard = {
        "format": 1,
        "mode": "finding",
        "baseline": True,
        "a": held,
        "checked": checked,
        "since": checked,
    }
    guard |= dict.fromkeys(("basis", "adopted", "accepted"))
    sides = {
        "a": {"breaking": 1, "other": 0, "kinds": {"column_missing": 1}, "from": "1" * 64, "to": "2" * 64}
    }
    drifted = {
        "cycle": 2,
        "where": "cycle",
        "mode": "finding",
        "checked": True,
        "paused": True,
        "basis": None,
    }
    async with repo.audited(pack_ctx.pool) as (conn, audit):
        assert await repo.set_guard(conn, run_id, guard)
        paused = {"reason": "run_paused_schema_drift", "schema": {"a": "2" * 64, "b": None}}
        await audit(WORKER, "paused", run_id, paused)
        await audit(WORKER, "schema.drift", run_id, drifted | {"sides": sides, "error_type": None})
    assert await write_csv(pack_ctx, run_id, 1, "coverage", keyed=False) == coverage
    assert coverage.split(b"\r\n")[0].decode().split(",") == [
        name for name in COVERAGE_COLUMNS if name not in UNKEYED_COLUMNS
    ]  # no column was added
    second = await pack.issue(pack_ctx, run_id, 1, who=olga)  # the gate lets the rows through
    stored = [await pack.stored(pack_ctx, found["pack_id"]) for found in (first, second)]
    before, after = (files_of(data) for _, data in stored)
    cited = {
        "manifest.json",
        "audit/chain.tsv",
        "certificate.pdf",
        "VERIFY.txt",
        "SHA256SUMS",
        "SHA256SUMS.sig",
    }
    assert set(before) == set(after)
    assert {name for name in before if before[name] != after[name]} <= cited
    assert after["csv/coverage.csv"] == coverage and after["facts/plan.json"] == before["facts/plan.json"]
    chain = after["audit/chain.tsv"].decode()
    assert '"action": "schema.drift"' in chain and "run_paused_schema_drift" in chain
    assert '"action": "schema.drift"' not in before["audit/chain.tsv"].decode()
    for name, data in after.items():
        assert RAW_KEY.encode() not in data and b"CANARY-TYPE" not in data, name
    assert b"guard" not in after["facts/plan.json"] and b"guard" not in after["manifest.json"]


async def test_create_run_hashes_its_params_as_stored(
    pack_ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = listed_spec()
    await licensed_once(pack_ctx)  # a vendor build's run under SSO needs an Enterprise licence
    async with pack_ctx.pool.connection() as conn:
        bob, carol = (
            await signed_in(conn, "bob", "spec_author"),
            await signed_in(conn, "carol", "rule_approver"),
        )
        spec_id = await repo.save_spec(conn, spec, created_by=bob.actor)
        request = await repo.create_approval(conn, "spec", spec_id, bob.actor, scopes=approvals.needs(spec))
        assert request is not None
        await grant(conn, request["approval_id"], carol, {"kind": "spec"})
    declared = {
        "svc-a": {"rate": 1e16, "share": 0.1},
        "svc-b": {"rate": 2.5},
    }  # jsonb keeps 1e16 as an integer
    monkeypatch.setattr(Context, "declared", lambda self, spec: declared)
    run_id = await runs.create_run(pack_ctx, spec_id, "batch", ["K001", "K002"], who=bob)
    async with pack_ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
    assert run is not None
    [created] = await _audited(pack_ctx, "create_run", run_id)
    stored = run["params"]["capabilities"]["svc-a"]["rate"]
    assert (stored, type(stored)) == (10**16, int)
    assert created["plan_sha256"] == sha256_hex(approvals.plan(run["params"]))  # facts/plan.json's
    in_memory = run["params"] | {"capabilities": declared}
    assert created["plan_sha256"] != sha256_hex(approvals.plan(in_memory))


def test_the_subject_names_each_side_s_engine_and_its_major_and_minor_version_only() -> None:
    # A pair with a side on MySQL or Oracle: the plan holds each side's engine and server.
    tables = {
        "a": {"schema": "APP", "name": "CUSTOMERS", "engine": "oracle", "server": "19.21", "tls": True},
        "b": {"schema": "public", "name": "customers", "engine": "postgresql", "server": "16.4"},
    }
    run = {"run_id": "R-1", "spec_id": 7, "params": {"tables": tables}}
    facts: Any = SimpleNamespace(run=run, cycle=1, attests={"sample": None}, spec=unit_spec())
    subject = pack._subject(facts)
    assert subject["sides"] == {
        "a": {"role": "legacy", "target": "svc-a", "relation": "APP.CUSTOMERS", "engine": "oracle",
              "server": "19.21"},
        "b": {"role": "new", "target": "svc-b", "relation": "public.customers", "engine": "postgresql",
              "server": "16.4"},
    }  # fmt: skip
    shown = pack_verify._subject({"manifest.json": json.dumps({"subject": subject}).encode()})
    sides = (
        "; side a legacy svc-a APP.CUSTOMERS oracle 19.21; side b new svc-b public.customers postgresql 16.4:"
    )
    assert sides in shown.text and pack._upgraded(tables, subject["sides"]) == []
    # The cycle's scan read side a after a release update: the subject names the version read, and the
    # certificate warns that it is not the plan's. A count that failed read none.
    facts.attests["census"] = {"a": {"basis": "scan", "server": "19.22"}, "b": {"failed": "TimeoutError"}}
    read = pack._subject(facts)["sides"]
    assert (read["a"]["server"], read["b"]["server"], tables["a"]["server"]) == ("19.22", "16.4", "19.21")
    assert pack._upgraded(tables, read) == ["SIDE A READ ON oracle 19.22: the run was planned on 19.21"]
    shown = pack_verify._subject(
        {"manifest.json": json.dumps({"subject": {**subject, "sides": read}}).encode()}
    )
    assert "side a legacy svc-a APP.CUSTOMERS oracle 19.22;" in shown.text
    # A pair of PostgreSQL tables, and a pack issued before engines, name none: the line is as it was.
    del facts.attests["census"]
    for plan in tables.values():
        del plan["engine"], plan["server"]
    older = pack._subject(facts)
    assert older["sides"]["a"] == {"role": "legacy", "target": "svc-a", "relation": "APP.CUSTOMERS"}
    shown = pack_verify._subject({"manifest.json": json.dumps({"subject": older}).encode()})
    assert shown.text.endswith(
        "; side a legacy svc-a APP.CUSTOMERS; side b new svc-b public.customers: match it with your change"
        " record"
    )


FILES_SHA256 = "5d" * 32
FILE_SIDE = {
    "schema": None, "name": "customers", "keys": ["id"], "columns": {"name": "name"}, "where": None,
    "engine": "files", "server": "duckdb 1.5", "consistency": "fixed",
    "file": {"path": "customers/current/*.csv", "format": "csv", "csv": {"delimiter": "|"}},
    "file_count": 12, "bytes": 3650722201, "files_sha256": FILES_SHA256, "pinned_at": "2026-10-01T02:14:07Z",
    "as_of": "2026-09-30T23:59:00+00:00", "modified_last": "2026-10-01T01:58:11Z", "mount": "read-only",
    "rows": 6, "selected": 6, "rejected": 0, "kinds": {}, "pass_s": 0.2, "sort_bytes": 120,
}  # fmt: skip


def test_the_subject_of_a_file_side_holds_its_files() -> None:
    """A file side's subject adds its files from the plan (count, bytes, the one
    digest and what it is the SHA-256 of, the stamp's time), which ``ds packs verify`` shows; a pack issued
    before has none."""
    database = {"schema": "public", "name": "customers", "engine": "postgresql", "server": "16.4"}
    run = {"run_id": "R-1", "spec_id": 7, "params": {"tables": {"a": FILE_SIDE, "b": database}}}
    facts: Any = SimpleNamespace(run=run, cycle=1, attests={"sample": None}, spec=unit_spec())
    subject = pack._subject(facts)
    held = {
        "count": 12, "bytes": 3650722201, "sha256": FILES_SHA256, "sha256_of": files_manifest.SHA256_OF,
        "as_of": "2026-09-30T23:59:00+00:00",
    }  # fmt: skip
    assert subject["sides"]["a"] == {
        "role": "legacy", "target": "svc-a", "relation": "customers", "engine": "files",
        "server": "duckdb 1.5", "files": held,
    }  # fmt: skip
    assert "files" not in subject["sides"]["b"]
    shown = pack_verify._subject({"manifest.json": json.dumps({"subject": subject}).encode()})
    assert (
        f"; side a legacy svc-a customers files duckdb 1.5 12 file(s) sha256 {FILES_SHA256} of the list of"
        " its files' SHA-256 values, a line each, in the byte order of their paths under the target's root,"
        " not a file's own (sha256sum FILE... | cut -c1-64 | sha256sum); side b"
    ) in shown.text
    # Its sides without the member (a pack issued before files), or with one of another shape, still read.
    for files in ({}, {"files": "x"}):
        older = {**subject, "sides": {"a": {"role": "legacy", "target": "svc-a"} | files}}
        shown = pack_verify._subject({"manifest.json": json.dumps({"subject": older}).encode()})
        assert shown.text.endswith("; side a legacy svc-a: match it with your change record")
    older = {**subject, "sides": {"a": {"role": "legacy", "files": {"count": 1, "sha256": FILES_SHA256}}}}
    shown = pack_verify._subject({"manifest.json": json.dumps({"subject": older}).encode()})
    assert shown.text.endswith(
        f"; side a legacy 1 file(s) sha256 {FILES_SHA256}: match it with your change record"
    )


def _printed(command: str, files: list[str], root: Path) -> str:
    """What ``command`` (a pack's own text, its ``FILE...`` or ``PATH...`` the files) prints, run by ``sh`` in
    ``root`` under ``sha256sum``, or the pack procedure's function over ``shasum -a 256``."""
    shim = "" if shutil.which("sha256sum") else 'sha256sum() { shasum -a 256 "$@"; }\n'
    script = re.sub(r"(FILE|PATH)\.\.\.", " ".join(map(shlex.quote, files)), command)
    done = subprocess.run(["sh", "-c", shim + script], cwd=root, capture_output=True, text=True, check=True)
    return done.stdout[:64]


async def test_a_pack_says_what_its_files_digest_is_and_how_to_reproduce_it(tmp_path: Path) -> None:
    """An auditor holds a pack and the single exported file it is about, runs
    ``sha256sum`` on the file and gets another value than the pack's ``sha256``, which is the SHA-256 of a
    list. So the subject, and with it step 4 of ``ds packs verify`` and of VERIFY.txt, and the certificate's
    census line say what the digest is the SHA-256 of, with the command that prints it from the files; of a
    side that declares partitions, from ``sha256sum``'s lines whole. The gate does not search that text."""
    from deltasleuth.runner import census
    from tests.unit.connectors.ducked import csv_table, file_spec, registry

    found = registry(tmp_path)
    target = found.get("exports")
    root = Path(target.root or "")
    (root / "accounts").mkdir()
    (root / "accounts" / "accounts.csv").write_bytes(b"id|v\n1|a\n2|b\n")
    for day in ("dt=2026-10-01", "dt=2026-10-02"):
        (root / "daily" / day).mkdir(parents=True)
        (root / "daily" / day / "part.csv").write_bytes(f"id|v\n1|{day}\n".encode())
    one = await files_manifest.pin(target, "accounts/accounts.csv", "side a", FilesSettings())
    parted = await files_manifest.pin(
        target, "daily/dt=*/part.csv", "side a", FilesSettings(), partitions=["dt"]
    )
    own = hashlib.sha256((root / "accounts" / "accounts.csv").read_bytes()).hexdigest()
    assert one.pins[0][4] == own != one.facts()["files_sha256"]  # the file's own is not the pack's digest
    plan = FILE_SIDE | {"file": {"path": "accounts/accounts.csv", "format": "csv"}} | one.facts()
    by_path = FILE_SIDE | {"file": {"path": "daily/dt=*/part.csv", "partitions": ["dt"]}} | parted.facts()
    whole, paths = files_manifest.SHA256_OF, files_manifest.SHA256_OF_PATHS
    for side, pinned, said in ((plan, one, whole), (by_path, parted, paths)):
        held = pack._files(side)
        assert (held["sha256"], held["sha256_of"]) == (side["files_sha256"], said)
        command = re.search(r"\((sha256sum [^,)]*)", said)[1]  # type: ignore[index]
        assert _printed(command, list(pinned.files), root) == held["sha256"]
    assert paths.endswith("(sha256sum PATH... | sha256sum, run in that root)")
    # The certificate's line and step 4, of the one-file side.
    state = {"rows_a": 2, "skipped_a": 0, "first_at": AT, "last_at": AT, "verified_a": AT}
    record = census.scanned("a", plan, state, unit_spec())
    lines, _ = pack._census_lines(unit_spec(), {"a": record}, {"a": plan})
    told = f"; sha256 {plan['files_sha256']} of {whole}; reads of single keys before"
    assert told in lines[0] and own not in lines[0]
    tables = {"a": plan, "b": {"schema": "s", "name": "t"}}
    run = {"run_id": "R-1", "spec_id": 7, "params": {"tables": tables}}
    facts: Any = SimpleNamespace(run=run, cycle=1, attests={"sample": None}, spec=unit_spec())
    listed = {"manifest.json": json.dumps({"subject": pack._subject(facts)}).encode()}
    assert f"1 file(s) sha256 {plan['files_sha256']} of {whole}; side b" in pack_verify._subject(listed).text
    # A numeric key pattern reads the command's ``64`` as a key: the manifest's text is fixed, not searched.
    raw = file_spec(csv_table("accounts/accounts.csv", ["id", "v"]))
    raw["key"] |= {"class": "identifier", "pattern": "^[0-9]{2}$", "source": {"kind": "list", "keys": ["40"]}}
    assert pack.raw_values(compile_spec(raw, targets=found), listed) == []
    listed = {"manifest.json": json.dumps({"note": whole}).encode()}
    assert pack.raw_values(compile_spec(raw, targets=found), listed) == ["manifest.json note"]


AT = "2026-10-01T02:20:00.000000Z"


async def test_a_pack_of_a_file_side_names_its_digest_and_no_file(
    pack_ctx: Context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Side a read fixed files to their end and hashed them the same. The
    certificate says what it was read from, by the one digest, and how single keys were read; the subject
    and ``facts/plan.json`` hold that digest; no pack file holds the sealed pins, a file's name or a file's
    own digest; side b's lines that were not rows, a repeated key and a writable mount are warned of."""
    documents: list[Document] = []
    render = pack.render_pdf
    monkeypatch.setattr(pack, "render_pdf", lambda document: documents.append(document) or render(document))
    at = "2026-10-01T02:20:00.000000Z"
    state = {
        "done": True, "after": None, "rows_a": 6, "rows_b": 6, "skipped_a": 0, "skipped_b": 0, "matched": 0,
        "candidates": 6, "checkpoint": 1, "snapshots_b": 1, "first_at": at, "last_at": at,
        "verified_a": at, "verified_b": at, "repeated_a": 0, "repeated_b": 2,
    }  # fmt: skip
    b = FILE_SIDE | {"name": "customers_v", "files_sha256": "7e" * 32, "as_of": None, "rejected": 3}
    tables = {"a": FILE_SIDE, "b": b | {"mount": "writable"}, "only_a": [], "only_b": [], "rendering": 1}
    canary, digest = "customers-CANARY7.csv", "c4" * 32
    pins = {"a": {"side": "a", "files": [canary], "pins": [[3650722201, 1, 2, 77, digest]]}}
    signed = await signed_cycle(pack_ctx, scan=state, tables=tables, pinned=pins)
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    files = files_of(data)
    manifest, plan = json.loads(files["manifest.json"]), json.loads(files["facts/plan.json"])
    assert manifest["subject"]["sides"]["a"]["files"] == {
        "count": 12, "bytes": 3650722201, "sha256": FILES_SHA256, "sha256_of": files_manifest.SHA256_OF,
        "as_of": "2026-09-30T23:59:00+00:00",
    }  # fmt: skip
    record = manifest["census"]["a"]
    assert (record["consistency"], record["files"], record["verified_at"]) == ("fixed", 12, at)
    assert "snapshots" not in record and FILES_SHA256 not in json.dumps(manifest["census"])
    assert plan["tables"]["a"]["files_sha256"] == FILES_SHA256 and "pinned" not in plan
    async with pack_ctx.pool.connection() as conn:
        run = await repo.get_run(conn, signed.run_id)
    assert run is not None and set(run["params"]["pinned"]["a"]) == {"enc"}
    assert hashlib.sha256(files["facts/plan.json"]).hexdigest() == sha256_hex(approvals.plan(run["params"]))
    for name, content in files.items():
        for inside in [content, *(_streams(content) if name.endswith(".pdf") else [])]:
            assert canary.encode() not in inside and digest.encode() not in inside, name
            assert b"pinned" not in inside.replace(b"pinned_at", b"").replace(b"pinned 2026", b""), name
    certificate = documents[-1]
    titled = {section.title: section for section in certificate.sections}
    assert titled["Census (observed)"].lines[0] == (
        "Side a (legacy, target svc-a): read to its end from 12 fixed file(s), 3,650,722,201 bytes, pinned"
        " 2026-10-01 02:14 UTC and hashed the same when the read ended at 2026-10-01 02:20 UTC; an export,"
        " not the system it came from: declared as of 2026-09-30 23:59 UTC by its stamp file, not observed;"
        f" newest file modified 2026-10-01 01:58 UTC on the mount; sha256 {FILES_SHA256} of the list of its"
        " files' SHA-256 values, a line each, in the byte order of their paths under the target's root, not a"
        " file's own (sha256sum FILE... | cut -c1-64 | sha256sum); reads of single keys before and after it"
        " were checked by size, times and inode, not hashed again; 6 row(s) of customers, 0 without a key"
    )
    assert "its own time is not observed" in titled["Census (observed)"].lines[1]
    assert f"sha256 {'7e' * 32} of the list of its files' SHA-256" in titled["Census (observed)"].lines[1]
    assert "Side a (legacy): target svc-a, customers (files duckdb 1.5)" in (
        titled["Subject (observed)"].lines
    )
    assert (certificate.warning or "").splitlines() == [
        "SIDE B: 3 LINE(S) OF ITS FILES WERE NOT ROWS and were not compared: a key on such a line is not"
        " compared, and a key not found on this side is inconclusive",
        "SIDE B: 2 KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive, never paired",
        "SIDE B WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO",
    ]
    # No statement a pack never makes of files is in the certificate.
    said = " ".join(line for section in certificate.sections for line in section.lines).lower()
    for never in ("snapshot of", "immutable", "tamper", "every line compared", "keys are unique"):
        assert never not in said.replace("not a certified census", "")
    signer = pack_ctx.signer
    assert signer is not None
    (tmp_path / "pack.zip").write_bytes(data)
    steps = pack_verify.verify(tmp_path / "pack.zip", signer.fingerprint)
    assert [step.number for step in steps if step.ok is False] == []
    assert f"12 file(s) sha256 {FILES_SHA256} of {files_manifest.SHA256_OF}; side b" in steps[4].text
    # Step 4 of the pack's own VERIFY.txt, run by sh: the subject it prints says what the digest is.
    by_hand = run_step(unzip(data, tmp_path / "unzipped"), 4)
    said = [line.strip() for line in by_hand.out if "sha256" in line]
    assert said[:2] == [f'"sha256": "{FILES_SHA256}",', f'"sha256_of": "{files_manifest.SHA256_OF}"']


def test_a_dated_path_trips_the_raw_value_gate(tmp_path: Path) -> None:
    """The spec's own file.path is spec text, searched by the gate: a dated
    directory holds a number a numeric key pattern reads as a key, and the refusal names its place."""
    from tests.unit.connectors.ducked import csv_table, file_spec, registry

    found = registry(tmp_path)
    raw = file_spec(csv_table("customers/20261001/*.csv", ["id", "v"]))
    source = {"kind": "list", "keys": ["40012345"]}
    raw["key"] |= {"class": "identifier", "pattern": "^[0-9]{8}$", "source": source}
    spec = compile_spec(raw, targets=found)
    files = {"facts/spec.json": pack._spec_text(raw).encode()}
    assert pack.raw_values(spec, files) == ["key.source.keys[0]", "sides.a.table.file.path"]
    raw["sides"]["a"]["table"]["file"]["path"] = "customers/current/*.csv"
    files = {"facts/spec.json": pack._spec_text(raw).encode()}
    assert pack.raw_values(compile_spec(raw, targets=found), files) == ["key.source.keys[0]"]


async def test_side_a_with_lines_that_were_not_rows_gets_no_pack(pack_ctx: Context) -> None:
    """Side a's files held lines that were not rows, so a key on such a
    line that side b lacks is in no result: the cycle is no census and its pack is refused."""
    at = "2026-10-01T02:20:00.000000Z"
    state = {
        "done": True, "after": None, "rows_a": 6, "rows_b": 6, "skipped_a": 0, "skipped_b": 0, "matched": 0,
        "candidates": 6, "checkpoint": 1, "snapshots_b": 1, "first_at": at, "last_at": at, "verified_a": at,
    }  # fmt: skip
    database = {"schema": "public", "name": "items", "keys": ["id"], "columns": {"name": "name"}}
    tables = {"a": FILE_SIDE | {"rejected": 3}, "b": database | {"where": None}, "only_a": [], "only_b": []}
    signed = await signed_cycle(pack_ctx, scan=state, tables=tables)
    refused = (
        f"cycle 1 of run {signed.run_id} is not a census: side a's files hold 3 line(s) that were not rows"
    )
    async with pack_ctx.pool.connection() as conn:
        run = await repo.get_run(conn, signed.run_id)
        assert run is not None
        assert await pack.problems(pack_ctx, conn, run, 1) == [refused]
    with pytest.raises(pack.PackRefused, match="is not a census: side a's files hold 3 line"):
        await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])


async def test_a_sampled_claim_never_says_one_snapshot_of_a_frame_its_engine_could_not_pin(
    pack_ctx: Context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The frame of a MySQL view is read in a transaction whose snapshot cannot be observed: the signed
    # claim once said `a table in one snapshot` beside the census line that said not.
    documents: list[Document] = []
    render = pack.render_pdf
    monkeypatch.setattr(pack, "render_pdf", lambda document: documents.append(document) or render(document))
    view = {"basis": "frame", "target": "db-a", "relation": "ledger.customers_v", "where": None, "rows": 6,
            "skipped": 0, "keys": 6, "unfit": 0, "at": "2026-10-02T10:00:00.000000Z", "engine": "mysql",
            "consistency": "unverified", "server": "8.4"}  # fmt: skip
    signed = await sampled_cycle(pack_ctx, census={"a": view})
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    signer = pack_ctx.signer
    assert signer is not None
    manifest = json.loads(files_of(data)["manifest.json"])
    claim = pack.PROVES_SAMPLED_UNPINNED.format(name="test-1", fp=signer.fingerprint, absent="0")
    assert manifest["claim_text"] == claim and "in one snapshot" not in claim
    held = "its sources held when read (a table in one pass not observed to be a snapshot, a listing as"
    assert held in claim and claim.replace("pass not observed to be a snapshot", "snapshot") == (
        pack.PROVES_SAMPLED.format(name="test-1", fp=signer.fingerprint, absent="0")
    )
    certificate = documents[-1]
    titled = {section.title: section for section in certificate.sections}
    assert titled["What the signature proves (observed)"].lines == [claim]
    frame = titled["Frame (observed)"].lines[0]
    assert frame.startswith(
        "Side a (legacy, target db-a): its population read to its end in 1 transaction, a snapshot not"
        " observable (a MySQL view) at 2026-10-02 10:00 UTC as the sampling frame"
    )
    assert (certificate.warning or "").startswith(
        "SIDE A NOT READ IN A SNAPSHOT (a snapshot not observable (a MySQL view)): rows written during"
    )
    # `ds packs verify` reads no claim text: the pack passes its procedure with either.
    (tmp_path / "pack.zip").write_bytes(data)
    steps = pack_verify.verify(tmp_path / "pack.zip", signer.fingerprint)
    assert [step.number for step in steps if step.ok is False] == []
    assert [step.ok for step in steps[:4]] == [True] * 4  # the procedure, the key, the signature, the files
    # A table outside InnoDB is not said to be read in a snapshot either; a frame whose record names a
    # snapshot, or no engine at all (PostgreSQL's, a listing's), keeps the text it had.
    for consistency, proves in (
        ("none", pack.PROVES_SAMPLED_UNPINNED),
        ("snapshot", pack.PROVES_SAMPLED),
        (None, pack.PROVES_SAMPLED),
    ):
        table = {name: found for name, found in view.items() if name != "consistency"}
        table |= {"consistency": consistency} if consistency else {}
        census = {"cycle": 1, "at": view["at"], "a": table, "b": {"basis": "listing"}}
        assert pack._claim({"sample": manifest["sample"], "census": census}, signer, "test-1") == (
            proves.format(name="test-1", fp=signer.fingerprint, absent="0")
        )


def _streams(pdf: bytes) -> list[bytes]:
    """Every stream of the PDF, inflated where it deflates."""
    found = []
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.DOTALL):
        try:
            found.append(zlib.decompress(raw))
        except zlib.error:
            found.append(raw)
    return found


async def test_the_canary_nothing_raw_reaches_a_pack(pack_ctx: Context, key_file: Path) -> None:
    pairs = samples(pii=True)
    first = next(iter(pairs))
    pairs[first] = (
        doc(first) | {"memo": "BODY-CANARY-5521"},
        doc(first, name="Bob Othername", status="GONE"),
    )
    signed = await signed_cycle(
        pack_ctx, spec=listed_spec(pii=True), pairs=pairs, adjudicate=True, notes="NOTE-CANARY-7734",
        headers={"x-trace": "HEADER-CANARY-9912"},
    )  # fmt: skip
    viewed = mask_key(first, signed.spec, pack_ctx.tokeniser)
    async with pack_ctx.pool.connection() as conn:  # a key viewed, a chat about it: cycle 1, then cycle 2
        await repo.insert_audit(conn, signed.people["olga"], "key.view", signed.run_id,
                                {"key_masked": viewed, "cycle": 1})  # fmt: skip
        sealer = await pack_ctx.sealer(conn, signed.run_id)
        session = await repo.create_chat_session(
            conn, signed.run_id, viewed, created_by=signed.people["olga"].actor
        )
        await repo.append_chat_message(conn, session, "user", {"content": "QUESTION-CANARY-3318"}, sealer)
        await repo.append_chat_message(conn, session, "assistant", {"content": "ANSWER-CANARY-4471"}, sealer,
                                       citations=["F-1"])  # fmt: skip
        await conn.execute("UPDATE ds.runs SET cycle = 2 WHERE run_id = %s", (signed.run_id,))
        await repo.append_chat_message(conn, session, "assistant", {"content": "LATER-CANARY-6602"}, sealer)
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    _, data = await pack.stored(pack_ctx, issued["pack_id"])
    files = files_of(data)
    key = key_file.read_bytes()
    private = PackSigner.load(f"file:{key_file}")._key
    canaries = [
        *(k.encode() for k in pairs), SECRET_NAME.encode(), b"Bob Othername", b"BODY-CANARY-5521",
        b"HEADER-CANARY-9912", b"QUESTION-CANARY-3318", b"ANSWER-CANARY-4471", b"LATER-CANARY-6602",
        b"NOTE-CANARY-7734", b"k" * 32, (b"k" * 32).hex().encode(), b"127.0.0.1:9", key,
        private.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption()),
        *(line for line in key.splitlines() if not line.startswith(b"-----")),
        f"{private.private_numbers().private_value:x}".encode(),
    ]  # fmt: skip
    haystack = [*files.values(), *_streams(files["certificate.pdf"]), data]
    for canary in canaries:
        assert not any(canary in part for part in haystack), canary
    assert RAW_KEY not in "".join(
        files[name].decode() for name in files if name.endswith((".json", ".csv", ".tsv"))
    )
    # Per key, masked keys only in the sheet, the key.view rows and the answers: the CSVs have none.
    masked = [mask_key(k, signed.spec, pack_ctx.tokeniser).encode() for k in pairs]
    assert sorted(name for name, data in files.items() if any(m in data for m in masked)) == [
        "adjudication/sheet.csv", "annex/ai_answers.json", "audit/chain.tsv",
    ]  # fmt: skip
    assert [row for row in files["audit/chain.tsv"].decode().split("\n") if viewed in row] == [
        row for row in files["audit/chain.tsv"].decode().split("\n") if '"action": "key.view"' in row
    ]
    header = files["csv/root_causes.csv"].split(b"\r\n")[0].decode().split(",")
    assert header[:2] == ["root_cause_id", "kind"] and not {"member_key_tok", "sample_keys"} & set(header)
    annex = json.loads(files["annex/ai_answers.json"])
    assert annex["label"] == "AI-generated, not evidence"
    assert [(a["cycle"], a["key_masked"], a["citations"], a["content_sha256"]) for a in annex["answers"]] == [
        (1, viewed, ["F-1"], sha256_hex("ANSWER-CANARY-4471"))
    ]  # this cycle's answer only, hashed


async def test_a_purge_keeps_a_packs_sign_off_and_record_and_deletes_its_zip(pack_ctx: Context) -> None:
    signed = await signed_cycle(pack_ctx)
    issued = await pack.issue(pack_ctx, signed.run_id, 1, who=signed.people["olga"])
    pack_id = issued["pack_id"]
    async with pack_ctx.pool.connection() as conn:
        for statement, refused in (
            ("DELETE FROM ds.pack_zips WHERE pack_id = %s", "stays until its cycle is purged"),
            ("UPDATE ds.packs SET audit_seq = 1 WHERE pack_id = %s", "is only revoked, or marked purged"),
            ("UPDATE ds.packs SET purged_at = now() WHERE pack_id = %s", "is only revoked, or marked purged"),
            ("DELETE FROM ds.packs WHERE pack_id = %s", "cannot be deleted"),
        ):
            with pytest.raises(psycopg.Error, match=re.escape(refused)):
                async with conn.transaction():
                    await conn.execute(statement, (pack_id,))
    await purge.hold(pack_ctx, TESTER, signed.run_id, None)  # ds purge release
    await purge.erase(pack_ctx, TESTER, signed.run_id, "test erasure", cycle=1)
    async with pack_ctx.pool.connection() as conn:
        signoff = await repo.get_approval(conn, signed.signoff_id)
        record = await repo.get_pack(conn, pack_id)
    assert signoff is not None and signoff["status"] == "approved"  # an issued pack cites it
    assert record is not None and record["status"] == "issued" and record["purged_at"] is not None
    [purged] = await _audited(pack_ctx, "retention.purged", signed.run_id)
    assert purged["packs"] == [pack_id]
    assert await _audited(pack_ctx, "approval.revoke", signed.signoff_id) == []
    with pytest.raises(CyclePurged) as gone:
        await pack.stored(pack_ctx, pack_id)
    assert str(gone.value) == (
        f"pack {pack_id} was purged with cycle 1 on {record['purged_at']:%Y-%m-%d}: its stored copy is"
        " deleted; copies downloaded before still verify"
    )
    with pytest.raises(LookupError, match=re.escape(f"pack P-{'0' * 26} not found")):
        await pack.stored(pack_ctx, f"P-{'0' * 26}")


async def _seeded(dsn: str, key: Path) -> tuple[str, str]:
    """A pack issued on the database at ``dsn``: its id and run."""
    pool = AsyncConnectionPool(dsn, open=False, max_size=4)
    await pool.open(wait=True)
    try:
        await migrate(pool)
        settings = Settings("file:/unused", key.parent / "e", key.parent / "w", key.parent / "t.yaml")
        evidence = evidence_store(settings.evidence_dir)
        ctx = Context(settings, pool, TARGETS, Tokeniser(b"k" * 32), evidence, None, None, TEST_KEYRING)
        ctx = with_packs(ctx, key)
        signed = await signed_cycle(ctx)
        issued = await pack.issue(ctx, signed.run_id, 1, who=signed.people["olga"])
        return issued["pack_id"], signed.run_id
    finally:
        await pool.close()


def test_the_cli_lists_downloads_audits_and_signs_the_revocation_list(
    pg_dsn: str, tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_id, run_id = asyncio.run(_seeded(pg_dsn, key_file))
    (tmp_path / "dsn").write_text(pg_dsn + "\n")
    (tmp_path / "t.yaml").write_text(TARGETS_FILE)
    (tmp_path / "oidc_secret").write_text("s3cret\n")
    oidc = {"issuer": "http://127.0.0.1:1", "client_id": "ds", "roles": {"rule_approver": ["G"]},
            "client_secret_ref": f"file:{tmp_path / 'oidc_secret'}"}  # fmt: skip
    config = tmp_path / "ds.yaml"
    config.write_text(yaml.safe_dump({
        "database_dsn_ref": f"file:{tmp_path / 'dsn'}", "evidence_dir": "e", "workspace_dir": "w",
        "targets_file": "t.yaml", "auth": {"public_url": "https://ds.test", "oidc": oidc},
        "encryption": encryption(tmp_path / "keys"), "packs": {"installation": "test-1",
                                                               "signing_key_ref": f"file:{key_file}"},
    }))  # fmt: skip
    monkeypatch.setenv("DS_OPERATOR", "ann")

    def ds(*args: Any) -> Any:
        return CliRunner().invoke(app, [str(a) for a in [*args, "--config", config]])

    init = ds("init")
    assert init.exit_code == 0, init.output
    listed = ds("packs", "--run", run_id)
    assert listed.exit_code == 0, listed.output
    assert listed.stdout.startswith(f"{pack_id}  issued  cycle 1 of run {run_id}")
    as_json = json.loads(ds("packs", "--json").stdout)
    assert [p["pack_id"] for p in as_json] == [pack_id]
    downloaded = ds("packs", "download", pack_id, "--out", tmp_path / "out")
    zipped = tmp_path / "out" / f"{pack_id}.zip"
    assert downloaded.exit_code == 0, downloaded.output
    assert hashlib.sha256(zipped.read_bytes()).hexdigest() == as_json[0]["zip_sha256"]
    revoked = ds("packs", "revocations", "--out", tmp_path / "custodian")
    custodian = tmp_path / "custodian"
    assert (
        revoked.stdout
        == f"wrote revoked.txt (0 revoked pack(s)), revoked.txt.sig and signing-key.pem to {custodian}\n"
    )
    signer = PackSigner.load(f"file:{key_file}")
    checked = CliRunner().invoke(app, ["packs", "verify", str(zipped), "--fingerprint", signer.fingerprint,
                                       "--revocations", str(tmp_path / "custodian")])  # fmt: skip
    assert checked.exit_code == 0 and checked.stdout.splitlines()[-1].startswith("step 8 revocation: ok")
    missing = ds("packs", "download", f"P-{'0' * 26}", "--out", tmp_path / "out")
    assert (missing.exit_code, missing.stderr) == (2, f"error: pack P-{'0' * 26} not found\n")
    misplaced = CliRunner().invoke(app, ["packs", "--json", "key", "--out", str(tmp_path)])
    assert (misplaced.exit_code, misplaced.stderr) == (2, "error: put the options after 'key'\n")
    with psycopg.connect(pg_dsn) as conn:
        audited = conn.execute(
            "SELECT actor, detail FROM ds.audit_log WHERE action = 'pack.download' AND object = %s",
            (pack_id,),
        ).fetchall()
    assert [(actor.split("@")[0], detail["run_id"], detail["cycle"]) for actor, detail in audited] == [
        ("local:ann", run_id, 1)
    ]
