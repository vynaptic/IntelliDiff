"""An installation lost and restored from its backup, with the commands docs/deployment.md gives, its
database roles apart as the Compose kit keeps them: the app signs in as a role of its own, and the schema's
owner takes a plain ``pg_dump`` of the store while the app runs, beside one archive of the evidence
directory. The installation is lost with both roles, as on a new host. ``ds db setup --roles-only`` makes the
roles again, the owner loads the dump into an empty database (``psql``, whole or not at all), and the dump's
own grants let the app's role read it at once; ``ds db setup``, which the migrate service runs before the
app, then changes no role and grants what it granted. The evidence goes back beside the same two key files.
The restored store's key check reads every stored body, its audit chain verifies and takes new rows, and a
token made before the backup reads what it read before: the run and its counters, its results and
findings, and a key whose two documents are derived again from the stored evidence.

``pg_dump`` and ``psql`` run inside the fixtures' PostgreSQL container, as the documented commands run
them inside the database's; ``tar`` is the host's.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
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
SEPARATE = (
    "roles: separate: the role of database_dsn_ref owns nothing in schema ds and cannot switch the audit"
    " log's triggers off; migrate_dsn_ref's role applies the migrations"
)


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


@pytest.fixture
def roles(pg_server: str, pg_dsn: str) -> Iterator[tuple[str, str]]:
    """The schema owner's and the app's role names for this test; each is dropped with what it owns and
    was granted, wherever the test left it."""
    tag = uuid.uuid4().hex[:8]
    made = (f"ds_owner_{tag}", f"ds_app_{tag}")
    yield made
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for role in made:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                conn.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER".encode())
                conn.execute(f"DROP OWNED BY {role}".encode())
                conn.execute(f"DROP ROLE {role}".encode())


def test_a_lost_installation_is_restored_from_its_backup(
    serve: Callable[[str], ServedPair],
    make_stack: Callable[..., Stack],
    pg_server: str,
    pg_dsn: str,
    roles: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")  # the name the command line's audit rows record
    stack = make_stack(serve("customer").targets(), auth=AUTH)
    store = conninfo_to_dict(pg_dsn)
    superuser, database = str(store["user"]), str(store["dbname"])
    owner, application = roles
    # The kit's roles: db_dsn the app's own role's address, db_owner_dsn the schema owner's.
    app_dsn = make_conninfo("", **store | {"user": application, "password": "invented-app-password"})
    (tmp_path / "dsn").write_text(app_dsn)
    owner_dsn = make_conninfo("", **store | {"user": owner, "password": "invented-owner-password"})
    (tmp_path / "owner_dsn").write_text(owner_dsn)
    (tmp_path / "superuser").write_text(f"{store['password']}\n")
    migrating = f"migrate_dsn_ref: file:{tmp_path / 'owner_dsn'}\n"
    stack.settings.write_text(stack.settings.read_text() + migrating)
    settings = load_settings(stack.settings)
    in_db = ["docker", "exec", "-i", _container(str(store["port"]))]
    dump, archive = tmp_path / "ds.sql", tmp_path / "evidence.tar"
    setup = ("db", "setup", "--superuser", superuser)
    setup += ("--superuser-password-ref", f"file:{tmp_path / 'superuser'}")

    def ds(*args: str) -> str:
        """The command's standard output; it ended without an error."""
        result = CliRunner().invoke(cli, [*args, "--config", str(stack.settings)])
        assert result.exit_code == 0 and "error:" not in result.output, result.output
        return result.stdout

    made = ds(*setup).splitlines()  # as the migrate service does before the app's first start
    assert made[3].startswith("migrations applied: 1, ") and made[-1] == SEPARATE
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
        # The backup, while the app runs: the database's dump, by the schema's owner, then the evidence
        # without its spill directory.
        _piped([*in_db, "pg_dump", "-U", owner, database], to=dump)
        _piped(["tar", "-C", str(settings.evidence_dir), "--exclude=./spill", "-cf", "-", "."], to=archive)
    assert {name: n for name, n in original["counters"]["by_outcome"].items() if n} == OUTCOMES
    assert len(original["results"]) == len(KEYS) and original["findings"]
    assert original["key"]["canonical"]["a"] and original["key"]["canonical"]["b"]

    # The installation is lost: its database is dropped and made again empty, with neither role, as on a
    # new host, and its evidence and workspace directories are deleted and made again as new volumes are,
    # empty and private. The settings file, the addresses and the two key files stay.
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{database}"')
        for role in roles:
            conn.execute(f"DROP ROLE {role}".encode())
    for volume in (settings.evidence_dir, settings.workspace_dir):
        shutil.rmtree(volume)
        volume.mkdir(mode=0o700)

    # The restore, in its order: the roles alone, the dump by the owner in one transaction, the setup the
    # migrate service runs before the app, the evidence, the key check, which reads back every stored body,
    # every schedule paused, and then the app.
    assert ds(*setup, "--roles-only").splitlines() == [
        f"role {owner} made", f"role {application} made",
        f"role {owner} may connect to and create in database {database}",
    ]  # fmt: skip
    _piped([*in_db, "psql", "-U", owner, "-d", database, "-q", "-v", "ON_ERROR_STOP=1", "-1"], source=dump)
    with psycopg.connect(app_dsn) as conn:  # the dump carried the app's grants: it reads at once
        assert conn.execute("SELECT count(*) FROM ds.runs WHERE run_id = %s", (run_id,)).fetchone() == (1,)
    assert ds(*setup).splitlines() == [
        f"roles {owner} and {application}: as they were",
        "schema up to date",
        f"grants of {application}: as 'ds db grants' prints them",
        SEPARATE,
    ]
    _piped(["tar", "-C", str(settings.evidence_dir), "-xf", "-"], source=archive)
    read_back = READ_BACK.search(checked := ds("keys", "status", "--verify"))
    assert read_back and read_back[1] == read_back[2] != "0", checked
    restored = _chained(ds("audit", "verify"))
    assert restored >= logged > 0  # no row written before the backup began is lost
    assert ds("schedules", "pause", "--all") == "no active schedules\n"
    with _serving(settings, token) as client:
        assert _read(client, run_id) == original
    assert _chained(ds("audit", "verify")) > restored  # the restored chain took the app's new rows
