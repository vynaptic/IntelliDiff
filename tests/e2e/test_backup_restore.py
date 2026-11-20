"""An installation lost and restored from its backup, with the commands docs/deployment.md gives: a plain
``pg_dump`` of the store and one archive of the evidence directory, taken while the app runs, are loaded
into an empty database (``psql``, whole or not at all) and an empty evidence directory, beside the same
two key files. The restored store's key check then reads every stored body, its audit chain verifies and
takes new rows, and a token made before the backup reads what it read before: the run and its counters,
its results and findings, and a key whose two documents are derived again from the stored evidence.

``pg_dump`` and ``psql`` run inside the fixtures' PostgreSQL container, as the documented commands run
them inside the database's; ``tar`` is the host's.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from typer.testing import CliRunner

from deltasleuth.api import create_app
from deltasleuth.cli.main import app as cli
from deltasleuth.config import Settings, load_settings
from mocks.registry import pair_dir
from tests.conftest import PG_IMAGE

from .conftest import ServedPair, Stack

pytestmark = [pytest.mark.e2e, pytest.mark.db]
AUTH = {"public_url": "https://ds.test"}  # token sign-in: the two keys are files outside both directories
# A match, a mismatch, a record only one side holds and a known difference.
KEYS = ["C00012001", "C00012003", "C00012027", "C00012031"]
OUTCOMES = {"MATCH": 1, "MISMATCH": 1, "NOT_FOUND_B": 1, "KNOWN_DIFF": 1}
# The key check's line when it read back every body the store names: none missing, none unreadable.
READ_BACK = re.compile(r"^blobs: (\d+) checked: (\d+) readable, 0 missing, 0 corrupted, 0 unreadable", re.M)


def _piped(command: list[str], *, source: Path | None = None, to: Path | None = None) -> None:
    """Run ``command`` reading ``source`` and writing ``to``, as a shell's ``<`` and ``>`` do; a failure
    shows the command's standard error."""
    with (
        source.open("rb") if source else nullcontext(subprocess.DEVNULL) as read,
        to.open("wb") if to else nullcontext(subprocess.DEVNULL) as written,
    ):
        done = subprocess.run(command, stdin=read, stdout=written, stderr=subprocess.PIPE, check=False)
    assert done.returncode == 0, f"{' '.join(command)}: {done.stderr.decode(errors='replace')}"


def _container(port: str) -> str:
    """The fixtures' PostgreSQL container, found by the port it publishes on loopback."""
    listed = subprocess.run(
        ["docker", "ps", "--filter", f"ancestor={PG_IMAGE}", "--filter", f"publish={port}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    [name] = listed.stdout.split()
    return name


def _serving(settings: Settings, token: str) -> TestClient:
    """The app with its worker, called as the token's holder."""
    app = create_app(settings, worker=True)
    signed_in = {"Authorization": f"Bearer {token}"}
    return TestClient(app, base_url=AUTH["public_url"], client=("10.1.2.3", 50000), headers=signed_in)


def _read(client: TestClient, run_id: str) -> dict[str, Any]:
    """What a viewer reads of the run: its status and counters, its results, its findings and the view of
    its mismatched key, whose two documents are derived again from the stored evidence."""

    def get(path: str) -> Any:
        answer = client.get(f"/v1/runs/{run_id}{path}")
        assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
        return answer.json()

    run, results, findings = get(""), get("/results")["items"], get("/findings")["items"]
    [key] = (row["key_masked"] for row in results if row["outcome"] == "MISMATCH")
    view = get(f"/keys/{quote(key, safe='')}")
    return {
        "status": run["status"], "counters": run["counters"], "results": results, "findings": findings,
        "key": view,
    }  # fmt: skip


def _chained(said: str) -> int:
    """The rows ``ds audit verify`` says it verified."""
    [rows] = re.findall(r"^chain: ([\d,]+) row\(s\) verified", said, re.M)
    return int(rows.replace(",", ""))


def test_a_lost_installation_is_restored_from_its_backup(
    serve: Callable[[str], ServedPair],
    make_stack: Callable[..., Stack],
    pg_server: str,
    pg_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")  # the name the command line's audit rows record
    stack = make_stack(serve("customer").targets(), auth=AUTH)
    settings = load_settings(stack.settings)
    store = conninfo_to_dict(pg_dsn)
    user, database = str(store["user"]), str(store["dbname"])
    in_db = ["docker", "exec", "-i", _container(str(store["port"]))]
    dump, archive = tmp_path / "ds.sql", tmp_path / "evidence.tar"

    def ds(*args: str) -> str:
        """The command's standard output; it ended without an error."""
        result = CliRunner().invoke(cli, [*args, "--config", str(stack.settings)])
        assert result.exit_code == 0 and "error:" not in result.output, result.output
        return result.stdout

    assert ds("migrate").startswith("migrations applied: 1, ")  # as the deployment's set-up does
    made = ds("token", "create", "--name", "restore-drill", "--owner", "tests", "--role", "spec_author")
    token = made.strip()  # the command's only line of standard output
    with _serving(settings, token) as client:
        spec = client.post("/v1/specs", content=(pair_dir("customer") / "spec.yaml").read_text())
        run = {"spec_id": spec.json()["spec_id"], "mode": "batch", "keys": KEYS}
        run_id = client.post("/v1/runs", json=run).json()["run_id"]
        deadline = time.monotonic() + 60
        while (status := client.get(f"/v1/runs/{run_id}").json()["status"]) != "completed":
            assert time.monotonic() < deadline and status == "running", status
            time.sleep(0.1)
        original = _read(client, run_id)
        logged = _chained(ds("audit", "verify"))
        # The backup, while the app runs: the database's dump, then the evidence without its spill directory.
        _piped([*in_db, "pg_dump", "-U", user, database], to=dump)
        _piped(["tar", "-C", str(settings.evidence_dir), "--exclude=./spill", "-cf", "-", "."], to=archive)
    assert {name: n for name, n in original["counters"]["by_outcome"].items() if n} == OUTCOMES
    assert len(original["results"]) == len(KEYS) and original["findings"]
    assert original["key"]["canonical"]["a"] and original["key"]["canonical"]["b"]

    # The installation is lost: its database is dropped and made again empty, and its evidence and workspace
    # directories are deleted and made again as new volumes are, empty and private. The settings file and
    # the two key files stay.
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{database}"')
    for volume in (settings.evidence_dir, settings.workspace_dir):
        shutil.rmtree(volume)
        volume.mkdir(mode=0o700)

    # The restore, in its order: the dump in one transaction, the evidence, the key check, which reads back
    # every stored body, every schedule paused, and then the app.
    _piped([*in_db, "psql", "-U", user, "-d", database, "-q", "-v", "ON_ERROR_STOP=1", "-1"], source=dump)
    _piped(["tar", "-C", str(settings.evidence_dir), "-xf", "-"], source=archive)
    read_back = READ_BACK.search(checked := ds("keys", "status", "--verify"))
    assert read_back and read_back[1] == read_back[2] != "0", checked
    restored = _chained(ds("audit", "verify"))
    assert restored >= logged > 0  # no row written before the backup began is lost
    assert ds("schedules", "pause", "--all") == "no active schedules\n"
    with _serving(settings, token) as client:
        assert _read(client, run_id) == original
    assert _chained(ds("audit", "verify")) > restored  # the restored chain took the app's new rows
