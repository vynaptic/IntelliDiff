"""``ds db setup``: the schema owner's role and the application's, made and kept apart.

As a superuser it makes both roles, each able to sign in with its address's password, which it sets as a
SCRAM verifier and never sends; it refuses an application role that is the superuser, a superuser or the
owner, and it will not migrate while the application's role has a session open. Without the superuser it
can do only what the owner may. No password reaches its output."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from typer.testing import CliRunner, Result

from deltasleuth.cli.main import app
from deltasleuth.store import roles
from deltasleuth.store.db import MIGRATIONS, checksum

pytestmark = pytest.mark.db

TARGETS = """\
targets:
  svc: {base_url: "http://127.0.0.1:9/", allow_private_network: true, allow_insecure_http: true}
"""
PASSWORDS = {"owner": "invented-owner-password", "app": "invented-app-password"}


@pytest.fixture
def names(pg_dsn: str) -> Iterator[dict[str, str]]:
    """The owner's and the application's role names for this test; each is dropped with what it owns and
    was granted."""
    tag = uuid.uuid4().hex[:8]
    made = {"owner": f"ds_owner_{tag}", "app": f"ds_app_{tag}"}
    yield made
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for role in made.values():
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                conn.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER".encode())
                conn.execute(f"DROP OWNED BY {role}".encode())
                conn.execute(f"DROP ROLE {role}".encode())


def _address(dsn: str, role: str, password: str) -> str:
    return make_conninfo("", **conninfo_to_dict(dsn) | {"user": role, "password": password})


def _config(tmp_path: Path, pg_dsn: str, names: dict[str, str], *, owner: bool = True) -> Path:
    """Settings whose database_dsn_ref is the application's role and, with ``owner``, migrate_dsn_ref the
    owner's, each with its password; the superuser's password in the file ``superuser``."""
    (tmp_path / "dsn").write_text(_address(pg_dsn, names["app"], PASSWORDS["app"]) + "\n")
    (tmp_path / "owner_dsn").write_text(_address(pg_dsn, names["owner"], PASSWORDS["owner"]) + "\n")
    (tmp_path / "superuser").write_text(str(conninfo_to_dict(pg_dsn)["password"]) + "\n")
    (tmp_path / "t.yaml").write_text(TARGETS)
    doc = {"database_dsn_ref": "file:dsn", "evidence_dir": "e", "workspace_dir": "w",
           "targets_file": "t.yaml"}  # fmt: skip
    if owner:
        doc["migrate_dsn_ref"] = "file:owner_dsn"
    (tmp_path / "ds.yaml").write_text(yaml.safe_dump(doc))
    return tmp_path / "ds.yaml"


def _setup(config: Path, pg_dsn: str, *more: str, superuser: bool = True) -> Result:
    as_superuser = ["--superuser", str(conninfo_to_dict(pg_dsn)["user"]),
                    "--superuser-password-ref", f"file:{config.parent / 'superuser'}"]  # fmt: skip
    args = ["db", "setup", *(as_superuser if superuser else []), *more, "--config", str(config)]
    result = CliRunner().invoke(app, args)
    assert not [secret for secret in PASSWORDS.values() if secret in result.output]
    return result


def _refusal(result: Result) -> str:
    assert result.exit_code == 2, result.output
    return result.stderr.strip().removeprefix("error: ")


def test_a_password_is_set_as_a_verifier_and_only_when_it_differs(
    pg_dsn: str, tmp_path: Path, names: dict[str, str]
) -> None:
    config = _config(tmp_path, pg_dsn, names)
    assert _setup(config, pg_dsn).exit_code == 0
    with psycopg.connect(pg_dsn) as conn:
        query = "SELECT rolpassword FROM pg_authid WHERE rolname = %s"
        found = conn.execute(query, (names["app"],)).fetchone()
        assert found is not None
        [stored] = found
        made = conn.pgconn.encrypt_password(b"another", names["app"].encode(), b"scram-sha-256").decode()
    assert stored.startswith("SCRAM-SHA-256$") and PASSWORDS["app"] not in stored
    assert roles.holds(stored, PASSWORDS["app"]) and not roles.holds(stored, "another")
    assert roles.holds(made, "another") and not roles.holds(None, "") and not roles.holds("md5abc", "")
    new = "invented-app-password-2"
    (tmp_path / "dsn").write_text(_address(pg_dsn, names["app"], new) + "\n")
    changed = _setup(config, pg_dsn)
    assert changed.exit_code == 0 and new not in changed.output
    assert changed.stdout.splitlines()[0] == f"role {names['app']} takes the password of database_dsn_ref"
    psycopg.connect(_address(pg_dsn, names["app"], new)).close()  # the role signs in with its new password


def test_it_refuses_an_application_role_that_could_switch_the_triggers_off(
    pg_dsn: str, tmp_path: Path, names: dict[str, str]
) -> None:
    config = _config(tmp_path, pg_dsn, names)
    superuser = str(conninfo_to_dict(pg_dsn)["user"])
    (tmp_path / "dsn").write_text(pg_dsn + "\n")  # one role: the app signs in as the superuser
    assert _refusal(_setup(config, pg_dsn)) == (
        f"database_dsn_ref names the superuser {superuser}: it needs a role of its own, which this command"
        " makes"
    )
    (tmp_path / "dsn").write_text(_address(pg_dsn, names["owner"], PASSWORDS["owner"]) + "\n")
    assert _refusal(_setup(config, pg_dsn)) == (
        f"migrate_dsn_ref and database_dsn_ref name one role, {names['owner']}: the application needs a role"
        " of its own"
    )
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {names['app']} SUPERUSER LOGIN".encode())
    (tmp_path / "dsn").write_text(_address(pg_dsn, names["app"], PASSWORDS["app"]) + "\n")
    assert _refusal(_setup(config, pg_dsn)) == (
        f"role {names['app']} of database_dsn_ref is a superuser: name another role for the application,"
        " which this command makes"
    )
    with psycopg.connect(pg_dsn) as conn:  # each refusal rolled back whole: the owner was never made
        assert conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (names["owner"],)).fetchone() is None
    assert _refusal(_setup(config, pg_dsn, "--roles-only", superuser=False)) == (
        "--superuser-password-ref and --roles-only need --superuser"
    )
    unset = _config(tmp_path, pg_dsn, names, owner=False)
    assert _refusal(_setup(unset, pg_dsn)) == "ds db setup needs migrate_dsn_ref, the schema owner's address"


def test_it_does_not_migrate_under_a_running_app(pg_dsn: str, tmp_path: Path, names: dict[str, str]) -> None:
    """With a migration pending, a session of the application's role stops it before anything changes; with
    the session closed, the same run goes on."""
    config = _config(tmp_path, pg_dsn, names)
    assert _setup(config, pg_dsn).exit_code == 0
    last = max(MIGRATIONS.glob("*.sql"))
    version = int(last.name.split("_", 1)[0])
    with psycopg.connect(pg_dsn, autocommit=True) as conn:  # the newest migration pending, as after an update
        conn.execute("DELETE FROM ds.schema_version WHERE version = %s", (version,))
    with psycopg.connect(_address(pg_dsn, names["app"], PASSWORDS["app"])):  # the app, still running
        assert _refusal(_setup(config, pg_dsn)) == (
            f"migrations are pending and the application's role {names['app']} has 1 session(s) open: stop"
            " the app and its commands, then run this again"
        )
    with psycopg.connect(pg_dsn, autocommit=True) as conn:  # the app stopped: the schema as it was
        conn.execute(
            "INSERT INTO ds.schema_version (version, checksum) VALUES (%s, %s)",
            (version, checksum(last.read_bytes())),
        )
    assert _setup(config, pg_dsn).stdout.splitlines()[1] == "schema up to date"


def test_without_the_superuser_it_does_what_the_owner_may(
    pg_dsn: str, tmp_path: Path, names: dict[str, str]
) -> None:
    """Roles a site made itself: ``ds db setup`` without --superuser migrates and grants as the owner; a
    schema another role owns is refused in words that name the way out."""
    config = _config(tmp_path, pg_dsn, names)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for kind in ("owner", "app"):
            conn.execute(f"CREATE ROLE {names[kind]} LOGIN PASSWORD '{PASSWORDS[kind]}'".encode())
        database = conninfo_to_dict(pg_dsn)["dbname"]
        conn.execute(f'GRANT CREATE ON DATABASE "{database}" TO {names["owner"]}'.encode())
    one_role = tmp_path / "one"
    one_role.mkdir()
    before = _config(one_role, pg_dsn, names, owner=False)
    (one_role / "dsn").write_text(pg_dsn + "\n")
    migrated = CliRunner().invoke(app, ["migrate", "--config", str(before)])  # as the superuser: it owns ds
    assert migrated.exit_code == 0, migrated.output
    assert _refusal(_setup(config, pg_dsn, superuser=False)) == (
        "the role of migrate_dsn_ref may not change schema ds, which another role owns: run this with"
        " --superuser, which hands the schema to it"
    )
    handed = _setup(config, pg_dsn)
    assert handed.exit_code == 0, handed.output
    previous = conninfo_to_dict(pg_dsn)["user"]
    assert handed.stdout.splitlines()[0] == f"schema ds handed from {previous} to {names['owner']}"
    again = _setup(config, pg_dsn, superuser=False)
    assert again.exit_code == 0 and again.stdout.splitlines()[0] == "schema up to date"
