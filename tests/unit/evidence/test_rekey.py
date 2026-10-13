"""``ds keys migrate`` (docs/mvp/RETENTION.md §7.2) over a store that releases before 006 left: sealed in
resumable batches, interrupted in each phase and run again, then 007 applied, with the API's outputs
byte-equal to the golden ones; refused while the worker lock is held; then a rotation, and a missing KEK
that pauses a run until restored."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
from deltasleuth.context import Context, evidence_id_key
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import Observation, Side
from deltasleuth.evidence.store import EvidenceNotFoundError, observation_id
from deltasleuth.runner import resume_run, run_worker
from deltasleuth.runner.runs import KEY_PAUSE
from deltasleuth.runner.worker import LOCK_ID
from deltasleuth.security import legacy
from deltasleuth.security.tokenise import Tokeniser, mask_key
from deltasleuth.store import db, repo
from deltasleuth.store.db import MIGRATIONS, MigrationError
from tests.conftest import TESTER

pytestmark = pytest.mark.db

# Checked once against the app's answers before 006 on the same store: equal, but for the fields 006 adds.
GOLDEN = Path(__file__).with_name("rekey_api.golden.json")
T0 = datetime(2026, 9, 1, tzinfo=UTC)
TOKENISER_KEY = bytes(range(32))  # fixed: the golden outputs hold its tokens
SOAK, FAILED, QUEUED, SCAN_A, SCAN_B = ("R-" + c * 26 for c in "SFQAB")
SESSION = "S-00000000000000a1"
K1, K2, K3 = "K12345678", "K23456789", "K34567890"
FOUND, AFTER_A, AFTER_B = "K00000017", "K00000042", "K00000077"  # a scan's key found, and where scans stopped
MARKER = "Plantedmarker"  # in bodies only
SPEC = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: rekey, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key: {name: item_id, type: string, pattern: "^K[0-9]{8}$", class: identifier,
      source: {kind: list, keys: [K12345678, K23456789]}}
sides:
  a: &side
    role: old
    target: svc-a
    request: {method: GET, path: "/items/{{ key }}"}
  b: {<<: *side, role: new, target: svc-b}
compare: {}
recheck: {delay: 1s, times: 1, oneShot: {delay: 0s, times: 1}}
security: {clear: [$.price]}
execution: {cycleInterval: 1h, stop: {maxCycles: 3}}
"""
TARGETS = """\
targets:
  svc-a: {base_url: "http://127.0.0.1:9/", allow_private_network: true, allow_insecure_http: true}
  svc-b: {base_url: "http://127.0.0.1:9/", allow_private_network: true, allow_insecure_http: true}
"""
LLM_PROFILES = """\
hosts: {llm.example.com: internal}
profiles:
  local: {dialect: openai_chat, base_url: "https://llm.example.com/v1", model: m1, auth_ref: "file:llm.key"}
"""
# What an uninterrupted migration of the store seals and moves (each rerun adds only what was left).
TOTALS = {"runs": 5, "keys": 9, "headers": 14, "chat_messages": 4, "scans": 2, "blobs": 6, "missing_blobs": 2}
MISSING = "legacy plaintext remains: stop the app and run 'ds keys migrate' (docs/mvp/RETENTION.md §7.2)"

_spec = compile_spec(parse_yaml_text(SPEC))
_tokeniser = Tokeniser(TOKENISER_KEY)
MASKED = {key: mask_key(key, _spec, _tokeniser) for key in (K1, K2, K3, FOUND)}


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def body(key: str, name: str, price: float) -> bytes:
    return json.dumps({"id": key, "name": name, "price": price}).encode()


# -- the store as 005 left it ------------------------------------------------------------------------


def _insert(conn: psycopg.Connection, table: str, **values: Any) -> None:
    query = sql.SQL("INSERT INTO ds.{} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, values)),
        sql.SQL(", ").join(sql.Placeholder() * len(values)),
    )
    conn.execute(query, [Jsonb(v) if isinstance(v, dict | list) else v for v in values.values()])


def _blob(evidence: Path, content: bytes) -> str:
    """A legacy blob: the body's zstd frame at ``blobs/sha256/<2 hex>/<64 hex>.zst``."""
    digest = hashlib.sha256(content).hexdigest()
    directory = evidence / "blobs" / "sha256" / digest[:2]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / f"{digest}.zst").write_bytes(zstandard.ZstdCompressor(level=3).compress(content))
    return f"sha256:{digest}"


def _observation(
    conn: psycopg.Connection, evidence: Path, run_id: str, cycle: int, key: str, side: Side, content: bytes,
    when: datetime, *, stored: bool = True,
) -> str:  # fmt: skip
    content_hash = _blob(evidence, content) if stored else "sha256:" + hashlib.sha256(content).hexdigest()
    observed_at = utc_iso(when)
    obs = Observation(key, side, 200, content, observed_at)
    obs_id = observation_id(obs, content_hash, evidence_id_key(TOKENISER_KEY))
    _insert(
        conn, "observations", run_id=run_id, obs_id=obs_id, cycle=cycle, key=key, side=side.value, pass_no=1,
        http_status=200, content_hash=content_hash, latency_ms=12.5, attempts=1, pages=1, pages_eof=True,
        pagination_unstable=False, observed_at=when,
        headers={"content-type": "application/json", "location": f"/items/{key}"},
    )  # fmt: skip
    return obs_id


def _result(
    conn: psycopg.Connection, run_id: str, cycle: int, key: str, outcome: str, obs: tuple[str, str],
    updated: datetime,
) -> None:  # fmt: skip
    _insert(
        conn, "results", run_id=run_id, cycle=cycle, key=key, outcome=outcome, side_a_state="COMPLETE",
        side_b_state="COMPLETE", confirmation="rechecked", first_outcome=outcome, obs_a_id=obs[0],
        obs_b_id=obs[1], updated_at=updated,
    )  # fmt: skip


def legacy_store(dsn: str, evidence: Path) -> None:
    """A store at 005 with plaintext and its ``blobs/sha256/``: a soak at cycle 3 (a key whose price
    differs, known in cycle 1, and a match sharing one blob across sides and cycles) with a chat across
    its cycles; a failed run whose two blobs are missing; a queued run; two table scans stopped mid-way
    (one paused, one failed); an unreferenced blob and a temporary file."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE SCHEMA ds; CREATE TABLE ds.schema_version"
            " (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(MIGRATIONS.glob("00[1-5]_*.sql")):
            conn.execute(path.read_bytes())
            conn.execute("INSERT INTO ds.schema_version (version) VALUES (%s)", (int(path.name[:3]),))
        _insert(conn, "specs", spec_id=1, name=_spec.name, version=_spec.version,
                content_hash=_spec.content_hash, body=_spec.raw, created_at=T0)  # fmt: skip
        soak = {"soak": {"rolling": False}, "keys_source": "spec"}
        counters = {"cycle": 2, "total": 2, "compared": 2, "match": 1, "different": 1}
        explicit, scanned = {"keys_source": "explicit"}, {"keys_source": "scan"}
        runs = [  # run, mode, status, cycle, started, finished, params, counters, stop reason, error
            (SOAK, "soak", "running", 3, at(0), None, soak, counters, None, None),
            (FAILED, "batch", "failed", 1, at(30), at(31), explicit, {}, None, "EvidenceNotFoundError"),
            (QUEUED, "batch", "queued", 1, None, None, explicit, {}, None, None),
            (SCAN_A, "batch", "paused", 1, at(40), None, scanned, {}, "run_paused_auth_error", None),
            (SCAN_B, "batch", "failed", 1, at(50), at(51), scanned, {}, None, "OperationalError"),
        ]
        for run_id, mode, status, cycle, started, finished, params, counted, reason, error in runs:
            _insert(
                conn, "runs", run_id=run_id, spec_id=1, mode=mode, status=status, cycle=cycle,
                stop_reason=reason, params=params, counters=counted, error=error, created_at=T0,
                started_at=started, finished_at=finished, updated_at=finished or started or T0,
            )  # fmt: skip
        for cycle in (1, 2, 3):
            pairs = [
                (K1, body(K1, f"Alice {MARKER}", 1.5), body(K1, f"Alice {MARKER}", 2.0 + cycle)),
                (K2, body(K2, "Bob", 3.0), body(K2, "Bob", 3.0)),
            ]
            for seq, (key, a, b) in enumerate(pairs):
                _insert(conn, "run_keys", run_id=SOAK, cycle=cycle, key=key, key_masked=MASKED[key], seq=seq,
                        state="done", available_at=at(10 * cycle - 10))  # fmt: skip
                obs = tuple(
                    _observation(conn, evidence, SOAK, cycle, key, side, content, at(10 * cycle - 9 + seq))
                    for side, content in ((Side.A, a), (Side.B, b))
                )
                outcome = "MATCH" if key == K2 else "KNOWN_DIFF" if cycle == 1 else "MISMATCH"
                _result(conn, SOAK, cycle, key, outcome, obs, at(10 * cycle - 5 + seq))  # type: ignore[arg-type]
            _insert(
                conn, "findings", run_id=SOAK, cycle=cycle, key=K1, seq=0, finding_id=f"F-{cycle:016x}",
                taxonomy_code="VALUE_CHANGED", severity="LOW" if cycle == 1 else "HIGH",
                change_type="value_changed", direction="both", path_pattern="$.price", json_pointer="/price",
                value_a=Jsonb(1.5), value_b=Jsonb(2.0 + cycle), value_class="number", suppressed=cycle == 1,
                rule_id="price-rounding" if cycle == 1 else None,
                rule_classification="accepted" if cycle == 1 else None,
            )  # fmt: skip
        _insert(conn, "chat_sessions", session_id=SESSION, run_id=SOAK, created_by="dev:loopback",
                created_at=at(1))  # fmt: skip
        tool_call = {"id": "c1", "name": "get_findings", "arguments": {"key": MASKED[K1]}}
        messages = [
            (1, "user", "Why does this key differ?", [], [], at(2)),  # cycle 1 ended at 6h, cycle 2 at 16h
            (2, "tool", '{"findings": 1}', [], [tool_call], at(2.5)),
            (3, "assistant", "Its price changed [F-0000000000000002].", ["F-0000000000000002"], [], at(10)),
            (4, "user", "And now?", [], [], at(20)),  # the current cycle
        ]
        for seq, role, content, citations, tool_calls, when in messages:
            _insert(conn, "chat_messages", session_id=SESSION, seq=seq, role=role, content=content,
                    citations=citations, tool_calls=tool_calls, created_at=when)  # fmt: skip
        _insert(conn, "run_keys", run_id=FAILED, cycle=1, key=K3, key_masked=MASKED[K3], seq=0, state="done",
                available_at=at(30))  # fmt: skip
        obs = tuple(
            _observation(
                conn, evidence, FAILED, 1, K3, side, body(K3, "Carol", price), at(30.5), stored=False
            )
            for side, price in ((Side.A, 1.0), (Side.B, 2.0))
        )
        _result(conn, FAILED, 1, K3, "MISMATCH", obs, at(30.6))  # type: ignore[arg-type]
        _insert(conn, "run_keys", run_id=QUEUED, cycle=1, key=K1, key_masked=MASKED[K1], seq=0,
                available_at=T0)  # fmt: skip
        _insert(conn, "run_keys", run_id=SCAN_A, cycle=1, key=FOUND, key_masked=MASKED[FOUND], seq=0,
                available_at=at(40))  # fmt: skip
        counters = {"rows_a": 50, "rows_b": 50, "skipped_a": 0, "skipped_b": 0, "matched": 49}
        _insert(conn, "scans", run_id=SCAN_A, cycle=1,
                state={"done": False, "after": [AFTER_A], "candidates": 1} | counters)  # fmt: skip
        _insert(conn, "scans", run_id=SCAN_B, cycle=1,
                state={"done": False, "after": [AFTER_B], "candidates": 0} | counters)  # fmt: skip
    _blob(evidence, b'{"orphan": true}')
    (evidence / "blobs" / "sha256" / "ab").mkdir(mode=0o700, exist_ok=True)
    (evidence / "blobs" / "sha256" / "ab" / ".stray.zst.x1y2.tmp").write_bytes(b"half")


@pytest.fixture
def store(tmp_path: Path, pg_dsn: str) -> Path:
    """The settings of the legacy store (production keys: a keyring and the tokeniser key)."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "kek").write_text(os.urandom(32).hex() + "\n")
    (secrets / "tokeniser").write_text(TOKENISER_KEY.hex() + "\n")
    (tmp_path / "dsn").write_text(pg_dsn + "\n")
    (tmp_path / "targets.yaml").write_text(TARGETS)
    (tmp_path / "llm.yaml").write_text(LLM_PROFILES)
    doc = {
        "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
        "evidence_dir": "evidence",
        "workspace_dir": "workspace",
        "targets_file": "targets.yaml",
        "llm_profiles_file": "llm.yaml",
        "llm_profile": "local",
        "worker": {"poll_seconds": 0.05},
        "encryption": {
            "kek_ref": f"file:{secrets / 'kek'}",
            "tokeniser_key_ref": f"file:{secrets / 'tokeniser'}",
        },
    }
    (tmp_path / "ds.yaml").write_text(yaml.safe_dump(doc))
    legacy_store(pg_dsn, tmp_path / "evidence")
    return tmp_path / "ds.yaml"


@pytest.fixture(autouse=True)
def audit_logger() -> Iterator[None]:
    """``deltasleuth.audit`` restored after a command attached its SIEM copy."""
    logger = logging.getLogger("deltasleuth.audit")
    handlers, propagate, level = logger.handlers, logger.propagate, logger.level
    yield
    logger.handlers, logger.propagate = handlers, propagate
    logger.setLevel(level)


def run(*args: Any) -> Result:
    return CliRunner().invoke(app, [str(a) for a in args])


def rows(dsn: str, query: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()  # type: ignore[arg-type]


def versions(dsn: str) -> list[int]:
    return [r["version"] for r in rows(dsn, "SELECT version FROM ds.schema_version ORDER BY version")]


def migrations_audited(dsn: str) -> list[dict[str, Any]]:
    return [
        r["detail"]
        for r in rows(dsn, "SELECT detail FROM ds.audit_log WHERE action = 'keys.migrate' ORDER BY id")
    ]


def api_outputs(config: Path) -> str:
    """What the API answers about the store, each body as sent: the golden JSON."""
    paths = [
        "/v1/runs",
        f"/v1/runs/{SOAK}",
        f"/v1/runs/{SOAK}/results?cycle=1",
        f"/v1/runs/{SOAK}/results?cycle=2",
        f"/v1/runs/{SOAK}/results",
        f"/v1/runs/{SOAK}/findings?cycle=2",
        f"/v1/runs/{SOAK}/changes",
        f"/v1/runs/{SOAK}/keys/{MASKED[K1]}?cycle=1",
        f"/v1/runs/{SOAK}/keys/{MASKED[K1]}?cycle=2",
        f"/v1/runs/{SOAK}/keys/{MASKED[K2]}",
        f"/v1/runs/{FAILED}/results",
        f"/v1/runs/{SCAN_A}",
        f"/v1/runs/{SCAN_B}",
        f"/v1/chat/sessions/{SESSION}",
    ]
    where = {"base_url": "http://127.0.0.1", "client": ("127.0.0.1", 50000)}
    with TestClient(create_app(load_settings(config)), **where) as client:  # type: ignore[arg-type]
        bodies = {}
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            bodies[f"GET {path}"] = response.text
    return json.dumps(bodies, indent=1, ensure_ascii=False) + "\n"


def interrupt(monkeypatch: pytest.MonkeyPatch, phase: int | None) -> None:
    """Ctrl-C once the phase's first batch committed (``None``: once what is left of ``blobs/sha256/``
    is deleted, before 007 applies)."""

    async def ctrl_c() -> None:
        signal.raise_signal(signal.SIGINT)  # asyncio.run cancels the command, as a terminal's Ctrl-C does
        await asyncio.sleep(5)

    if phase is None:
        applied = db.migrate

        async def stopped(pool: Any, *, upto: int | None = None) -> list[int]:
            if upto is None:
                await ctrl_c()
            return await applied(pool, upto=upto)

        monkeypatch.setattr(db, "migrate", stopped)
        return
    name, batches = legacy.PHASES[phase]

    async def interrupted(ctx: Context) -> Any:
        async for done in batches(ctx):
            yield done
            await ctrl_c()

    phases = list(legacy.PHASES)
    phases[phase] = (name, interrupted)
    monkeypatch.setattr(legacy, "PHASES", tuple(phases))


# -- the migration ----------------------------------------------------------------------------------


def test_until_migrated_every_entry_point_stops(store: Path, pg_dsn: str) -> None:
    for command in ("migrate", "runs"):
        refused = run(command, "--config", store)
        assert (refused.exit_code, refused.stderr.splitlines()[-1]) == (2, f"error: {MISSING}")
    with pytest.raises(MigrationError, match=r"legacy plaintext remains: stop the app"):
        with TestClient(create_app(load_settings(store))):  # the server's start
            pass
    assert versions(pg_dsn) == [1, 2, 3, 4, 5]  # 006 rolled back with 007


def test_refused_while_the_worker_lock_is_held(store: Path, pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as worker:
        worker.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        refused = run("keys", "migrate", "--config", store)
    assert (refused.exit_code, refused.stderr) == (2, "error: stop the app first: docker compose stop app\n")
    assert versions(pg_dsn) == [1, 2, 3, 4, 5] and migrations_audited(pg_dsn) == []


def test_the_migration_seals_everything_and_ends_plaintext_storage(store: Path, pg_dsn: str) -> None:
    done = run("keys", "migrate", "--config", store)
    assert done.exit_code == 0, done.output
    assert done.stdout == (
        "sealed: 9 key(s), 14 header(s), 4 chat message(s) and 2 scan point(s) under 5 new run data key(s);"
        " 6 blob(s) re-encrypted, 2 missing; plaintext storage ended (migration 007)\n"
    )
    assert versions(pg_dsn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
    [audited] = migrations_audited(pg_dsn)
    assert ({n: audited[n] for n in TOTALS}, audited["complete"]) == (TOTALS, True)
    migrated = rows(
        pg_dsn, "SELECT actor, detail FROM ds.audit_log WHERE action = 'system.migrate' ORDER BY id"
    )
    assert [(r["actor"], r["detail"]) for r in migrated] == [
        ("system:migration", {"versions": [6]}),
        ("system:migration", {"versions": list(range(7, 18))}),  # the end of plaintext storage
    ]
    columns = rows(
        pg_dsn,
        """SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = 'ds' AND (table_name, column_name) IN (('run_keys', 'key'),
               ('observations', 'headers'), ('chat_messages', 'content'), ('chat_messages', 'tool_calls'))""",
    )
    evidence = store.parent / "evidence"
    assert columns == [] and not (evidence / "blobs" / "sha256").exists()
    for path in [path for path in evidence.rglob("*") if path.is_file()]:  # no body or key in clear
        data = path.read_bytes()
        assert MARKER.encode() not in data and K1.encode() not in data
    again = run("keys", "migrate", "--config", store)  # nothing left: it changes nothing
    assert again.exit_code == 0 and migrations_audited(pg_dsn)[-1]["blobs"] == 0


@pytest.mark.parametrize(
    "phase", [*range(len(legacy.PHASES)), None], ids=[*(name for name, _ in legacy.PHASES), "007"]
)
def test_interrupted_in_any_phase_a_rerun_completes_with_the_same_api_outputs(
    store: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch, phase: int | None
) -> None:
    with monkeypatch.context() as patched:
        for name in legacy.BATCHES:
            patched.setitem(legacy.BATCHES, name, 1)
        interrupt(patched, phase)
        stopped = run("keys", "migrate", "--config", store)
    assert stopped.exit_code == 130 and legacy.INTERRUPTED in stopped.stderr.splitlines(), stopped.output
    [partial] = migrations_audited(pg_dsn)
    assert partial["complete"] is False and versions(pg_dsn) == [1, 2, 3, 4, 5, 6]
    if phase is not None:  # the phases before it done, then one batch
        names = list(legacy.BATCHES)
        assert [partial[n] for n in names] == [
            TOTALS[n] if i < phase else 1 if i == phase else 0 for i, n in enumerate(names)
        ]
    done = run("keys", "migrate", "--config", store)
    assert done.exit_code == 0, done.output
    assert versions(pg_dsn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
    audited = migrations_audited(pg_dsn)
    assert audited[-1]["complete"] is True
    assert {n: sum(a[n] for a in audited) for n in TOTALS} == TOTALS  # nothing sealed twice, nothing lost
    assert api_outputs(store) == GOLDEN.read_text()


def test_the_golden_outputs_hold_the_opened_values_and_no_raw_one() -> None:
    golden = GOLDEN.read_text()
    assert not [text for text in (K1, K2, K3, FOUND, AFTER_A, MARKER) if text in golden]
    bodies = {path: json.loads(text) for path, text in json.loads(golden).items()}
    canonical = bodies[f"GET /v1/runs/{SOAK}/keys/{MASKED[K1]}?cycle=2"]["canonical"]
    messages = bodies[f"GET /v1/chat/sessions/{SESSION}"]["messages"]
    assert (canonical["a"]["price"], canonical["b"]["price"]) == (1.5, 4)  # from the re-encrypted blobs
    assert [m["content"] for m in messages][::3] == ["Why does this key differ?", "And now?"]


def test_a_blob_written_before_an_interruption_is_reused(
    store: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ctrl_c(*_: Any) -> None:  # inside the batch's transaction: its references roll back
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(5)

    with monkeypatch.context() as patched:
        patched.setattr(legacy, "_repoint", ctrl_c)
        assert run("keys", "migrate", "--config", store).exit_code == 130
    evidence = store.parent / "evidence" / "blobs"
    written = {path: path.stat().st_mtime_ns for path in (evidence / "hk1").rglob("*.dse")}
    left = list((evidence / "sha256").rglob("*.zst"))
    legacy_hashes = rows(
        pg_dsn, "SELECT count(*) AS n FROM ds.observations WHERE content_hash LIKE 'sha256:%%'"
    )
    assert (len(written), len(left), legacy_hashes) == (6, 6, [{"n": 14}])
    assert run("keys", "migrate", "--config", store).exit_code == 0
    assert {path: path.stat().st_mtime_ns for path in written} == written  # reused, not written again
    assert api_outputs(store) == GOLDEN.read_text()


def test_a_corrupted_legacy_blob_stops_the_migration_until_restored_or_deleted(
    store: Path, pg_dsn: str
) -> None:
    query = (
        "SELECT content_hash FROM ds.observations WHERE run_id = %s AND cycle = 3 AND side = 'b' AND key = %s"
    )
    [row] = rows(pg_dsn, query, SOAK, K1)
    digest = row["content_hash"].removeprefix("sha256:")
    path = store.parent / "evidence" / "blobs" / "sha256" / digest[:2] / f"{digest}.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(b'{"forged": true}'))
    stopped = run("keys", "migrate", "--config", store)
    assert (stopped.exit_code, stopped.stderr.splitlines()[-1]) == (
        2,
        f"error: legacy blob {path} is corrupted: restore it from a backup, or delete it to record it as"
        " missing, then run 'ds keys migrate' again",
    )
    assert [a["complete"] for a in migrations_audited(pg_dsn)] == [False]
    path.unlink()
    assert run("keys", "migrate", "--config", store).exit_code == 0
    assert migrations_audited(pg_dsn)[-1]["missing_blobs"] == 3


def test_a_missing_legacy_directory_is_refused_unless_accepted_then_healed(store: Path, pg_dsn: str) -> None:
    legacy_dir = store.parent / "evidence" / "blobs" / "sha256"
    elsewhere = store.parent / "sha256-elsewhere"
    legacy_dir.rename(elsewhere)  # as with evidence_dir naming the wrong directory
    refused = run("keys", "migrate", "--config", store)
    assert (refused.exit_code, refused.stderr.splitlines()[-1]) == (
        2,
        f"error: observations name legacy blobs, but {legacy_dir} holds none: check evidence_dir, or run"
        " 'ds keys migrate --accept-missing' to record every one as missing",
    )
    named = "SELECT count(*) AS n FROM ds.observations WHERE content_hash LIKE 'sha256:%%'"
    assert rows(pg_dsn, named) == [{"n": 14}] and migrations_audited(pg_dsn) == []  # nothing repointed
    assert rows(pg_dsn, "SELECT max(version) AS v FROM ds.schema_version") == [{"v": 5}]  # nor migrated
    accepted = run("keys", "migrate", "--accept-missing", "--config", store)
    assert accepted.exit_code == 0 and accepted.stdout.endswith(
        "0 blob(s) re-encrypted, 7 missing; plaintext storage ended (migration 007)\n"
    )
    elsewhere.rename(legacy_dir)  # the blobs are back
    healed = run("keys", "migrate", "--config", store)
    assert healed.exit_code == 0 and "6 blob(s) re-encrypted, 0 missing" in healed.stdout
    assert api_outputs(store) == GOLDEN.read_text()  # the placeholders now name the blobs


async def test_sealed_values_open_to_what_was_stored_and_a_missing_blob_stays_missing(
    store: Path, pg_dsn: str
) -> None:
    stored = rows(pg_dsn, "SELECT run_id, cycle, key_masked, key FROM ds.run_keys")
    before = {(r["run_id"], r["cycle"], r["key_masked"]): r["key"] for r in stored}
    query = "SELECT content_hash FROM ds.observations WHERE run_id = %s"
    lost = [r["content_hash"] for r in rows(pg_dsn, query, FAILED)]
    await legacy.migrate(load_settings(store), TESTER)
    keys = {}
    async with Context.open(load_settings(store)) as ctx, ctx.pool.connection() as conn:
        for run_id, cycle in {(run_id, cycle) for run_id, cycle, _ in before}:
            sealer = await ctx.sealer(conn, run_id)
            for row in await repo.pages(repo.list_keys, conn, run_id, cycle):
                keys[run_id, cycle, row["key_masked"]] = sealer.open_row(row)["key"]
        soak = await ctx.sealer(conn, SOAK)
        headers = [soak.open_row(row)["headers"] for row in await repo.list_observations(conn, SOAK, 2)]
        after = (await repo.get_scan(conn, SCAN_A, 1))["after"]  # type: ignore[index]
        scanned = (await ctx.sealer(conn, SCAN_A)).open("scan_after", 1, "", base64.b64decode(after["enc"]))
        failed = await repo.list_observations(conn, FAILED, 1)
        placeholders = sorted(legacy.missing_address(ctx.evidence, old) for old in lost)
        assert sorted(row["content_hash"] for row in failed) == placeholders
        for address in placeholders:
            with pytest.raises(EvidenceNotFoundError):
                ctx.evidence.get_body(address)
    assert keys == before and json.loads(scanned) == [AFTER_A]
    assert headers[0] == {"content-type": "application/json", "location": f"/items/{K1}"}
    where = {"base_url": "http://127.0.0.1", "client": ("127.0.0.1", 50000)}
    with TestClient(create_app(load_settings(store)), **where) as client:  # type: ignore[arg-type]
        view = client.get(f"/v1/runs/{FAILED}/keys/{MASKED[K3]}")
    first = min(failed, key=lambda row: row["side"])["obs_id"]
    missing = {"detail": f"evidence of {first} is missing (purged or lost)"}
    assert (view.status_code, view.json()) == (410, missing)


# -- keys after the migration -----------------------------------------------------------------------


def test_after_a_rotation_the_server_rewraps_and_adds_an_evidence_key(store: Path, pg_dsn: str) -> None:
    assert run("keys", "migrate", "--config", store).exit_code == 0
    before = {r["scope"]: r["kek"] for r in rows(pg_dsn, "SELECT scope, kek FROM ds.data_keys")}
    [old] = set(before.values())
    with (store.parent / "secrets" / "kek").open("a") as keyring:
        keyring.write("# rotated\n" + os.urandom(32).hex() + "\n")
    assert api_outputs(store) == GOLDEN.read_text()  # the server's start rewraps; old blobs still read
    after = {r["scope"]: r["kek"] for r in rows(pg_dsn, "SELECT scope, kek FROM ds.data_keys")}
    [new] = set(after.values())
    [added] = set(after) - set(before)
    rewraps = rows(pg_dsn, "SELECT actor, object, detail FROM ds.audit_log WHERE action = 'keys.rewrap'")
    assert new != old and added.startswith("E-")
    assert len(before) == 7  # 5 runs, the address and 1 evidence key
    assert rewraps == [
        {"actor": "system:migration", "object": "keys", "detail": {"from": old, "to": new, "rows": 7}}
    ]


async def test_a_missing_kek_pauses_a_run_until_restored(store: Path, pg_dsn: str) -> None:
    settings = load_settings(store)
    await legacy.migrate(settings, TESTER)
    keyring = store.parent / "secrets" / "kek"
    kept = keyring.read_text()
    keyring.write_text(os.urandom(32).hex() + "\n")  # the line was lost
    async with Context.open(settings) as ctx:
        assert list(ctx.missing_keks.values()) == [7]
        soak = await _worked(ctx, lambda r: r["status"] != "running")
    assert (soak["status"], soak["stop_reason"], soak["cycle"]) == ("paused", KEY_PAUSE, 3)
    keyring.write_text(kept)  # restored from the vault; the app restarted
    async with Context.open(settings) as ctx:
        assert await resume_run(ctx, SOAK, who=TESTER) == "running"
        soak = await _worked(ctx, lambda r: r["status"] in repo.TERMINAL)
    assert (soak["status"], soak["stop_reason"]) == ("completed", "stop_condition:maxCycles")


async def _worked(ctx: Context, until: Callable[[repo.Row], bool]) -> repo.Row:
    """Run a worker until the soak's row satisfies ``until``."""
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(ctx, stop=stop))
    try:
        for _ in range(600):
            async with ctx.pool.connection() as conn:
                row = await repo.get_run(conn, SOAK)
            if until(row):  # type: ignore[arg-type]
                return row  # type: ignore[return-value]
            await asyncio.sleep(0.05)
        raise AssertionError(f"the soak is still {row['status']}")  # type: ignore[index]
    finally:
        stop.set()
        await worker
