"""A mock pair compared end to end with the database's roles apart: ``ds migrate`` runs as the schema's
owner (``migrate_dsn_ref``), and the app's role holds the data rights ``ds db grants`` prints and nothing
more. The run, its report and ``ds audit verify``, anchored by the worker's sweeps, go as with one role."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from typer.testing import CliRunner

from deltasleuth.cli.main import app
from mocks.registry import pair_dir

pytestmark = [pytest.mark.e2e, pytest.mark.db]
SPEC = str(pair_dir("linker") / "spec.yaml")
PASSWORD = "invented-app-password"  # noqa: S105 - a throwaway role's, on a throwaway server


@pytest.fixture
def app_role(pg_dsn: str) -> Iterator[str]:
    """A role name for this test; the role, made by the test, is dropped with what it was granted."""
    role = f"ds_app_{uuid.uuid4().hex[:8]}"
    yield role
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute(f"DROP OWNED BY {role}".encode())
            conn.execute(f"DROP ROLE {role}".encode())


def test_a_run_goes_end_to_end_under_the_application_s_own_role(
    serve: Any, make_stack: Any, pg_dsn: str, tmp_path: Path, app_role: str
) -> None:
    stack = make_stack(serve("linker").targets())
    settings = yaml.safe_load(stack.settings.read_text())
    (tmp_path / "owner_dsn").write_text(pg_dsn)  # the schema's owner, who migrates
    settings["migrate_dsn_ref"] = f"file:{tmp_path / 'owner_dsn'}"
    stack.settings.write_text(yaml.safe_dump(settings))

    def ds(*args: Any, code: int = 0) -> str:
        result = CliRunner().invoke(app, [str(a) for a in args])
        assert result.exit_code == code, result.output
        return result.stdout

    config = ("--config", stack.settings)
    assert ds("migrate", *config).startswith("migrations applied: 1, ")
    with psycopg.connect(pg_dsn) as conn:  # as the owner, then the role's password set apart
        conn.execute(ds("db", "grants", "--role", app_role).encode())
        conn.execute(f"ALTER ROLE {app_role} PASSWORD '{PASSWORD}'".encode())
    app_dsn = make_conninfo("", **conninfo_to_dict(pg_dsn) | {"user": app_role, "password": PASSWORD})
    (tmp_path / "dsn").write_text(app_dsn)  # database_dsn_ref: the app's role from here on

    live = ds("run", SPEC, "--fail-on-diff", *config, code=1)  # the seeded discrepancies
    assert "\ncompleted\n" in live and "total 61" in live
    run_id = re.search(r"run (R-\w+) started", live)[1]  # type: ignore[index]
    ds("report", run_id, "--out", tmp_path / "reports", "--kind", "discrepancies.csv", *config)
    assert (tmp_path / "reports" / f"{run_id}-discrepancies.csv").exists()
    verified = ds("audit", "verify", *config).splitlines()
    assert verified[0].startswith("chain: ")
    assert re.fullmatch(r"anchor: row \d+ [0-9a-f]{64}, written .*: the chain holds it", verified[1])
    assert [line for line in verified if line.startswith("roles: ")] == [
        "roles: separate: the role of database_dsn_ref owns nothing in schema ds and cannot switch the audit"
        " log's triggers off; migrate_dsn_ref's role applies the migrations"
    ]
