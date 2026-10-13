"""Retention end to end (docs/mvp/RETENTION.md §4, §10). A finished run whose cycle ended longer ago than
its retention is purged by ``ds purge``: its cycle's routes, reports and compares then answer 410 and its
chat is gone, while the run and its retention stay readable; its blobs go a day later. And the canary:
after ``ds keys migrate``, DEPLOY's VACUUM FULL and a CHECKPOINT, no planted raw key, header, chat text
or body marker is left in the database files ($PGDATA/base, read as the fixture's superuser) or the
evidence directory, where the same store as 005 left it holds every one: raw keys in ds.runs' dropped
003 column and legacy blobs' sha256 names included."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
import yaml
import zstandard
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from typer.testing import CliRunner, Result

from deltasleuth.api import create_app
from deltasleuth.cli.main import app
from deltasleuth.config import load_settings
from deltasleuth.connectors.base import utc_iso
from deltasleuth.context import evidence_id_key
from deltasleuth.core.types import Observation, Side
from deltasleuth.evidence.store import observation_id
from deltasleuth.store.db import MIGRATIONS
from mocks.registry import pair_dir
from tests.conftest import REPO_ROOT, encryption

from .conftest import Stack, pdf_text

pytestmark = [pytest.mark.e2e, pytest.mark.db]
DEVELOPER = {"base_url": "http://127.0.0.1", "client": ("127.0.0.1", 50000)}
# The canary runs the upgrade's ANALYZE and VACUUM FULL as DEPLOY.md (section 10) spells them.
DEPLOY = (REPO_ROOT / "docs" / "mvp" / "DEPLOY.md").read_text()
UPGRADE = re.findall(r"-c '([^']+)'", re.search(r"^.*-c 'VACUUM FULL .*$", DEPLOY, re.M)[0])  # type: ignore[index]


@pytest.fixture(autouse=True)
def audit_logger() -> Iterator[None]:
    """``deltasleuth.audit`` restored after a command attached its SIEM copy."""
    logger = logging.getLogger("deltasleuth.audit")
    handlers, propagate, level = logger.handlers[:], logger.propagate, logger.level
    yield
    logger.handlers, logger.propagate = handlers, propagate
    logger.setLevel(level)


def ds(*args: Any, code: int = 0) -> Result:
    result = CliRunner().invoke(app, [str(a) for a in args])
    assert result.exit_code == code, result.output
    return result


def rows(dsn: str, query: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()  # type: ignore[arg-type]


def execute(dsn: str, *statements: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:  # VACUUM FULL runs outside a transaction
        for statement in statements:
            conn.execute(statement)  # type: ignore[arg-type]


def finished(client: TestClient, run_id: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while (run := client.get(f"/v1/runs/{run_id}").json())["status"] != "completed":
        assert time.monotonic() < deadline and run["status"] == "running", run["status"]
        time.sleep(0.1)


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def pdf(client: TestClient, run_id: str) -> str:
    """report.pdf's text, its escaped parentheses read back."""
    text = pdf_text(client.get(f"/v1/runs/{run_id}/reports/report.pdf").content)
    return text.replace("\\(", "(").replace("\\)", ")")


def test_an_expired_cycle_is_purged_then_answers_410(
    serve: Any, make_stack: Callable[..., Stack], llm: Any, pg_dsn: str, tmp_path: Path
) -> None:
    stack = make_stack(serve("customer").targets(), llm_url=llm[0])
    config = ("--config", stack.settings)
    served = create_app(load_settings(stack.settings), worker=True)
    with TestClient(served, headers={"X-CSRF-Token": "dev"}, **DEVELOPER) as client:  # type: ignore[arg-type]
        spec = client.post("/v1/specs", content=(pair_dir("customer") / "spec.yaml").read_text()).json()
        run_id = client.post("/v1/runs", json={"spec_id": spec["spec_id"], "mode": "batch"}).json()["run_id"]
        finished(client, run_id)
        [key, *_] = [r["key_masked"] for r in client.get(f"/v1/runs/{run_id}/results").json()["items"]]
        session = client.post("/v1/chat/sessions", json={"run_id": run_id}).json()["session_id"]
        asked = client.post(f"/v1/chat/sessions/{session}/messages", json={"content": "Why do keys differ?"})
        kept = client.get(f"/v1/runs/{run_id}").json()["retention"]
        report = pdf(client, run_id)

        ds("purge", "hold", run_id, "--reason", "legal hold LH-7", *config)
        hold = client.get(f"/v1/runs/{run_id}").json()["retention"]["hold"]
        held = pdf(client, run_id)
        ds("purge", "release", run_id, *config)

        with psycopg.connect(pg_dsn) as conn:  # the cycle ended 91 days ago
            conn.execute("UPDATE ds.cycles SET ended_at = ended_at - interval '91 days'")
        [ended] = rows(pg_dsn, "SELECT ended_at FROM ds.cycles WHERE run_id = %s", run_id)
        [stored] = rows(
            pg_dsn,
            """SELECT (SELECT count(*) FROM ds.observations) AS observations,
                      (SELECT count(DISTINCT content_hash) FROM ds.observations) AS blobs,
                      (SELECT count(*) FROM ds.findings) AS findings,
                      (SELECT count(*) FROM ds.chat_messages) AS chat_messages""",
        )
        planned = ds("purge", "--dry-run", *config).stdout
        purged = ds("purge", *config).stdout
        [cycle] = rows(pg_dsn, "SELECT * FROM ds.cycles WHERE run_id = %s", run_id)
        reads = ["results", "findings", "root-causes", "graph", f"keys/{quote(key, safe='')}",
                 "reports/report.pdf", "reports/discrepancies.csv"]  # fmt: skip
        gone = [client.get(f"/v1/runs/{run_id}/{path}") for path in reads]
        compare = {"spec_id": spec["spec_id"], "run_id": run_id, "key": key}
        gone.append(client.post("/v1/compare", json=compare))
        new_chat = client.post("/v1/chat/sessions", json={"run_id": run_id})
        old_chat = client.post(f"/v1/chat/sessions/{session}/messages", json={"content": "And now?"})
        after = client.get(f"/v1/runs/{run_id}")

    [ending] = kept.pop("cycles")
    expires = at(ending["expires_at"])
    assert asked.status_code == 200
    assert kept == {"class": "standard", "days": 90, "hold": None, "purged": [], "last_purged_at": None}
    assert (ending["cycle"], expires - at(ending["ended_at"])) == (1, timedelta(days=90))
    assert f"Evidence retained until {expires:%Y-%m-%d} (retention standard)" in report
    assert (hold["reason"], hold["by"].startswith("local:")) == ("legal hold LH-7", True)
    assert "Evidence held until released (retention standard)" in held
    expired = (ended["ended_at"] + timedelta(days=90)).astimezone(UTC)
    assert planned == f"due: cycle 1 of run {run_id} (retention standard): expired {expired:%Y-%m-%d}\n"
    assert purged == (
        f"purged 1 cycle(s) of 1 run(s): 40 key(s); {stored['blobs']} blob(s) queued; 0 deleted (0 B),"
        " 0 kept (shared)\n"
    )

    day = f"{cycle['purging_at']:%Y-%m-%d}"
    expected = (
        f"cycle 1 of run {run_id} was purged on {day} (retention standard): its keys, results, findings and"
        " evidence are deleted"
    )
    assert [(r.status_code, r.json()) for r in gone] == [(410, {"detail": expected})] * len(gone)
    assert (new_chat.status_code, new_chat.json()) == (
        410,
        {"detail": f"the run's evidence was purged on {day}; AI answers need it"},
    )
    assert (old_chat.status_code, old_chat.json()) == (404, {"detail": f"chat session {session} not found"})
    shown = after.json()["retention"]
    assert (after.json()["status"], shown["purged"], shown["cycles"]) == ("completed", [1], [])
    assert shown["last_purged_at"] == cycle["purged_at"].isoformat()
    refused = ds("report", run_id, "--out", tmp_path / "p0", "--kind", "report.pdf", *config, code=2)
    assert refused.stderr.splitlines()[-1] == f"error: {expected}"

    left = """SELECT (SELECT count(*) FROM ds.run_keys) + (SELECT count(*) FROM ds.observations)
                    + (SELECT count(*) FROM ds.results) + (SELECT count(*) FROM ds.findings)
                    + (SELECT count(*) FROM ds.chat_sessions) + (SELECT count(*) FROM ds.chat_messages)
                    + (SELECT count(*) FROM ds.data_keys WHERE scope LIKE 'R-%%') AS n"""
    assert rows(pg_dsn, left) == [{"n": 0}]  # the run's data key too
    assert (cycle["purge_reason"], cycle["purged_by"].startswith("local:")) == ("retention standard", True)
    query = "SELECT action, detail FROM ds.audit_log WHERE action LIKE 'retention.%%' ORDER BY id"
    audited = rows(pg_dsn, query)
    assert [r["action"].removeprefix("retention.") for r in audited] == [
        "hold", "release", "expire", "purged", "finished",
    ]  # fmt: skip
    local = ("asserted", "os_account")  # every local: row's (docs/mvp/AUTH.md §4.2)
    assert {name: value for name, value in audited[3]["detail"].items() if name not in local} == {
        "cycle": 1, "keys": 40, "results": 40, "blobs_queued": stored["blobs"], "data_key_destroyed": True,
        "packs": [], **{name: stored[name] for name in ("observations", "findings", "chat_messages")},
    }  # fmt: skip

    execute(pg_dsn, "UPDATE ds.blob_gc SET queued_at = queued_at - interval '25 hours'")  # a day later
    drained = ds("purge", *config).stdout
    assert re.fullmatch(
        rf"purged 0 cycle\(s\) of 0 run\(s\): 0 key\(s\); 0 blob\(s\) queued; {stored['blobs']} deleted"
        r" \([0-9.]+ [kM]?B\), 0 kept \(shared\)\n",
        drained,
    )
    assert list((stack.settings.parent / "evidence").rglob("*.dse")) == []  # they were the run's only ones


# -- the canary -----------------------------------------------------------------------------------------

BATCH, SCAN, SCAN_TWO, OLD_SCAN = ("R-CANARY" + c * 20 for c in "0SAB")
SESSION = "S-00000000000000c1"
ZSTD = b"\x28\xb5\x2f\xfd"  # a zstd frame: a legacy blob, which is its body compressed


def _insert(conn: psycopg.Connection, table: str, **values: Any) -> None:
    query = sql.SQL("INSERT INTO ds.{} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, values)),
        sql.SQL(", ").join(sql.Placeholder() * len(values)),
    )
    conn.execute(query, [Jsonb(v) if isinstance(v, dict | list) else v for v in values.values()])


def _legacy_blob(evidence: Path, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    directory = evidence / "blobs" / "sha256" / digest[:2]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / f"{digest}.zst").write_bytes(zstandard.ZstdCompressor().compress(body))
    return f"sha256:{digest}"


def store_as_005(dsn: str, evidence: Path, planted: dict[str, str], tokeniser_key: bytes) -> dict[str, str]:
    """The store as 005 left it, holding the planted values in clear: a table scan finished under 003,
    whose raw resume point 004 copied to ds.scans and left in ds.runs' dropped column; a finished run of
    two raw keys, with their observations' headers and bodies (four legacy blobs) and a chat question;
    and two table scans paused after a raw key each; then ANALYZE, whose statistics sample column values
    as autovacuum's would. The legacy blobs' sha256 names, which confirm a guessed body, by name."""
    now = datetime.now(UTC)
    times = {"created_at": now, "started_at": now, "updated_at": now}
    digests = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE SCHEMA ds; CREATE TABLE ds.schema_version"
            " (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )

        def migrated(pattern: str) -> None:
            for path in sorted(MIGRATIONS.glob(pattern)):
                conn.execute(path.read_bytes())
                conn.execute("INSERT INTO ds.schema_version (version) VALUES (%s)", (int(path.name[:3]),))

        migrated("00[1-3]_*.sql")
        _insert(conn, "specs", spec_id=1, name="canary", version="1", content_hash="c1", body={"canary": 1})
        done = {"done": True, "after": [planted["old scan point"]], "candidates": 0, "matched": 9}
        _insert(conn, "runs", run_id=OLD_SCAN, spec_id=1, mode="batch", status="completed", finished_at=now,
                scan=done, **times)  # fmt: skip
        migrated("00[4-5]_*.sql")
        _insert(
            conn, "runs", run_id=BATCH, spec_id=1, mode="batch", status="completed", finished_at=now, **times
        )
        for seq, key in enumerate((planted["raw key"], planted["raw key two"])):
            masked = f"tok:identifier:v1:{seq}"
            _insert(
                conn, "run_keys", run_id=BATCH, cycle=1, key=key, key_masked=masked, seq=seq, available_at=now
            )
            obs = []
            for side in Side:
                body = json.dumps({"id": key, "name": f"Alice {planted['body marker']}", "side": side.value})
                content_hash = _legacy_blob(evidence, body.encode())
                digests[f"sha256 name {len(digests) + 1}"] = content_hash.removeprefix("sha256:")
                read = Observation(key, side, 200, body.encode(), utc_iso(now))
                obs.append(observation_id(read, content_hash, evidence_id_key(tokeniser_key)))
                headers = {"x-trace": planted["header"], "location": f"/items/{key}"}
                _insert(
                    conn, "observations", run_id=BATCH, cycle=1, key=key, obs_id=obs[-1], side=side.value,
                    pass_no=1, http_status=200, content_hash=content_hash, attempts=1, pages=1,
                    pages_eof=True, pagination_unstable=False, observed_at=now, headers=headers,
                )  # fmt: skip
            _insert(
                conn, "results", run_id=BATCH, cycle=1, key=key, outcome="MATCH", first_outcome="MATCH",
                side_a_state="COMPLETE", side_b_state="COMPLETE", confirmation="rechecked", obs_a_id=obs[0],
                obs_b_id=obs[1], updated_at=now,
            )  # fmt: skip
        _insert(conn, "chat_sessions", session_id=SESSION, run_id=BATCH, created_at=now)
        _insert(conn, "chat_messages", session_id=SESSION, seq=1, role="user", content=planted["chat text"])
        for run_id, point in ((SCAN, "scan point"), (SCAN_TWO, "scan point two")):
            _insert(conn, "runs", run_id=run_id, spec_id=1, mode="batch", status="paused", **times)
            state = {"done": False, "after": [planted[point]], "candidates": 0, "matched": 9}
            _insert(conn, "scans", run_id=run_id, cycle=1, state=state | {"rows_a": 9, "rows_b": 9})
        conn.execute("ANALYZE")  # two or more rows a column: histograms that hold the values
    return digests


def found(dsn: str, evidence: Path, planted: dict[str, str]) -> dict[str, set[str]]:
    """Where each planted value can be read without a key: in a file under $PGDATA/base (every database's,
    listed and read by the server as the superuser) or in the evidence directory, a legacy blob's body
    decompressed."""
    places: dict[str, set[str]] = {}

    def seen(data: bytes, place: str) -> None:
        for name, value in planted.items():
            if value.encode() in data:
                places.setdefault(name, set()).add(place)

    with psycopg.connect(dsn, autocommit=True) as conn:
        files = conn.execute(
            "SELECT 'base/' || d || '/' || f FROM pg_ls_dir('base') AS d, pg_ls_dir('base/' || d) AS f"
        ).fetchall()
        for (path,) in files:
            [(data,)] = conn.execute("SELECT pg_read_binary_file(%s, true)", (path,), binary=True).fetchall()
            seen(data or b"", "database")  # None: dropped meanwhile
    for path in (p for p in evidence.rglob("*") if p.is_file()):
        data = path.read_bytes()
        body = zstandard.ZstdDecompressor().decompress(data) if data.startswith(ZSTD) else b""
        seen(data + body, "evidence")
    return places


def test_canary_no_planted_plaintext_survives_the_migration(tmp_path: Path, pg_dsn: str) -> None:
    tag = secrets.token_hex(6)
    planted = {
        name: f"{name.title().replace(' ', '')}{tag}"
        for name in (
            "raw key", "raw key two", "scan point", "scan point two", "old scan point", "header", "chat text",
            "body marker",
        )
    }  # fmt: skip
    (tmp_path / "dsn").write_text(pg_dsn)
    target = {"base_url": "http://127.0.0.1:9/", "allow_private_network": True, "allow_insecure_http": True}
    (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"svc": target}}))
    settings = {
        "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
        "evidence_dir": "evidence",
        "workspace_dir": "workspace",
        "targets_file": "targets.yaml",
        "encryption": encryption(tmp_path / "secrets"),
    }
    (tmp_path / "ds.yaml").write_text(yaml.safe_dump(settings))
    tokeniser_key = bytes.fromhex((tmp_path / "secrets" / "tokeniser").read_text())
    evidence = tmp_path / "evidence"
    digests = store_as_005(pg_dsn, evidence, planted, tokeniser_key)
    planted |= digests
    execute(pg_dsn, "CHECKPOINT")
    database, both = {"database"}, {"database", "evidence"}
    assert found(pg_dsn, evidence, planted) == {  # the control: a byte search sees each one
        "raw key": both, "raw key two": both, "scan point": database, "scan point two": database,
        "old scan point": database, "header": database, "chat text": database, "body marker": {"evidence"},
    } | dict.fromkeys(digests, database)  # fmt: skip
    stats = rows(pg_dsn, "SELECT histogram_bounds::text AS h FROM pg_stats WHERE tablename = 'scans'")
    assert planted["scan point two"] in str(stats)  # the planner's statistics hold them, as autovacuum's

    migrated = ds("keys", "migrate", "--config", tmp_path / "ds.yaml").stdout
    assert UPGRADE[0].startswith("ANALYZE") and "ds.runs" in UPGRADE[1]
    execute(pg_dsn, *UPGRADE, "CHECKPOINT")
    assert migrated == (
        "sealed: 2 key(s), 4 header(s), 1 chat message(s) and 2 scan point(s) under 4 new run data key(s);"
        " 4 blob(s) re-encrypted, 0 missing; plaintext storage ended (migration 007)\n"
    )
    assert len(digests) == 4 and found(pg_dsn, evidence, planted) == {}
