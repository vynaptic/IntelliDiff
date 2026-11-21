"""A generated source compared end to end with the database's roles apart: ``ds migrate`` runs as the schema's
owner (``migrate_dsn_ref``), and the app's role holds the data rights ``ds db grants`` prints and nothing
more, set by hand or by ``ds db setup`` as the Compose kit's migrate service runs it. The run, its report
and ``ds audit verify``, anchored by the worker's sweeps, go as with one role."""

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
from tests.support import records

pytestmark = [pytest.mark.e2e, pytest.mark.db]
PASSWORD = "invented-app-password"  # noqa: S105 - a throwaway role's, on a throwaway server
SEPARATE = (
    "roles: separate: the role of database_dsn_ref owns nothing in schema ds and cannot switch the audit"
    " log's triggers off; migrate_dsn_ref's role applies the migrations"
)


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
    stack = make_stack(serve("group").targets())
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

    live = ds("run", records.write_spec(tmp_path, "group"), "--fail-on-diff", *config, code=1)
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


@pytest.fixture
def roles(pg_dsn: str) -> Iterator[tuple[str, str]]:
    """The schema owner's and the application's role names for this test, which ``ds db setup`` makes; each
    is dropped with what it owns and was granted."""
    tag = uuid.uuid4().hex[:8]
    made = (f"ds_owner_{tag}", f"ds_app_{tag}")
    yield made
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for role in made:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                conn.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER".encode())
                conn.execute(f"DROP OWNED BY {role}".encode())
                conn.execute(f"DROP ROLE {role}".encode())


def test_ds_db_setup_keeps_the_roles_apart_and_a_run_goes_end_to_end(
    serve: Any, make_stack: Any, pg_dsn: str, tmp_path: Path, roles: tuple[str, str]
) -> None:
    """As the Compose kit's migrate service runs it: ``ds db setup`` as the superuser makes both roles, each
    with its address's password, and migrates as the owner; a run then goes end to end as the application's
    role, whose start line and ``ds audit verify`` say ``roles: separate``. A second setup changes nothing."""
    owner, application = roles
    stack = make_stack(serve("group").targets())
    store = conninfo_to_dict(pg_dsn)
    (tmp_path / "superuser").write_text(f"{store['password']}\n")
    for name, role in (("owner_dsn", owner), ("dsn", application)):
        password = f"invented-{name.replace('_', '-')}-password"
        (tmp_path / name).write_text(make_conninfo("", **store | {"user": role, "password": password}))
    settings = yaml.safe_load(stack.settings.read_text())
    settings["migrate_dsn_ref"] = f"file:{tmp_path / 'owner_dsn'}"
    stack.settings.write_text(yaml.safe_dump(settings))
    config = ("--config", stack.settings)
    superuser = ("--superuser", store["user"], "--superuser-password-ref", f"file:{tmp_path / 'superuser'}")
    setup = ("db", "setup", *superuser)

    def ds(*args: Any, code: int = 0) -> str:
        result = CliRunner().invoke(app, [str(a) for a in args])
        assert result.exit_code == code, result.output
        return result.stdout

    first = ds(*setup, *config).splitlines()
    assert first[:3] == [f"role {owner} made", f"role {application} made",
                         f"role {owner} may connect to and create in database {store['dbname']}"]  # fmt: skip
    assert first[3].startswith("migrations applied: 1, ") and first[-1] == SEPARATE
    live = ds("run", records.write_spec(tmp_path, "group"), "--fail-on-diff", *config, code=1)
    assert "\ncompleted\n" in live and "total 61" in live
    assert SEPARATE in ds("audit", "verify", *config).splitlines()
    assert ds(*setup, *config).splitlines() == [
        f"roles {owner} and {application}: as they were",
        "schema up to date",
        f"grants of {application}: as 'ds db grants' prints them",
        SEPARATE,
    ]
