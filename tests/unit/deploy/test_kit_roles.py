"""The Compose kit's database roles, in PostgreSQL itself.

deploy/up.sh writes the secrets and the settings (test_up_script.py drives it with stand-ins for Docker).
Here those files meet a database started as the kit's db service starts one: a throwaway postgres:16-alpine
container takes the trial's db_password as its superuser's, and ``ds db setup`` runs with the arguments of
the migrate service's entrypoint, reading the trial's own files where the containers mount them, with the
address ``db:5432`` naming that container. A fresh trial gets the schema owner's role and the app's own role,
the app runs as the latter, and its start line and ``ds audit verify`` say ``roles: separate``; a trial set up
before the roles were apart is moved to two roles once, its data kept.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from deltasleuth.api import create_app
from deltasleuth.cli.main import app as cli
from deltasleuth.config import load_settings
from deltasleuth.core.secrets import read_secret
from tests.conftest import PG_IMAGE, _free_port, _unavailable
from tests.unit.deploy.test_up_script import REPO, Trial, one_role

pytestmark = pytest.mark.db

COMPOSE = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())
DB = COMPOSE["services"]["db"]
SETUP = COMPOSE["services"]["migrate"]["entrypoint"]  # ds db setup, with the superuser's name and password
SEPARATE = (
    "roles: separate: the role of database_dsn_ref owns nothing in schema ds and cannot switch the audit"
    " log's triggers off; migrate_dsn_ref's role applies the migrations"
)
SHARED = (
    "roles: shared: the role of database_dsn_ref is a superuser, so whoever holds it can switch the audit"
    " log's triggers off ('ds db grants' prints the grants of a role that cannot)"
)


class Kit:
    """A trial's folder, the database its db service would start, and the trial's files as the containers
    read them: the secrets under ``run/secrets``, the settings under ``etc``, each naming the other's paths,
    with ``db:5432`` the database's loopback address."""

    def __init__(self, trial: Trial, tmp: Path) -> None:
        self.trial, self.tmp = trial, tmp
        self.name, self.port = f"ds-kit-{uuid.uuid4().hex[:8]}", _free_port()

    def start(self) -> None:
        """The db service's container, on this folder's db_password."""
        env, password = DB["environment"], self.trial.folder / "deploy" / "secrets" / "db_password"
        assert DB["image"] == PG_IMAGE
        run = subprocess.run(
            ["docker", "run", "-d", "--rm", "--pull", "never", "--name", self.name,
             "-e", f"POSTGRES_USER={env['POSTGRES_USER']}", "-e", f"POSTGRES_DB={env['POSTGRES_DB']}",
             "-e", f"POSTGRES_PASSWORD_FILE={env['POSTGRES_PASSWORD_FILE']}",
             "-v", f"{password}:{env['POSTGRES_PASSWORD_FILE']}:ro", "-p", f"127.0.0.1:{self.port}:5432",
             PG_IMAGE],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if run.returncode != 0:
            _unavailable(f"cannot start {PG_IMAGE}: {run.stderr.strip()[:200]}")
        superuser = make_conninfo(
            host="127.0.0.1", port=self.port, dbname=env["POSTGRES_DB"], user=env["POSTGRES_USER"],
            password=read_secret(f"file:{password}"),
        )  # fmt: skip
        deadline = time.monotonic() + 60
        while True:
            try:
                psycopg.connect(superuser, connect_timeout=1).close()
                return
            except psycopg.OperationalError:
                assert time.monotonic() < deadline, "the kit's database did not start"
                time.sleep(0.3)

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", "-v", self.name], capture_output=True, check=False)

    def mounted(self) -> Path:
        """The trial's files as the containers read them, copied afresh; the settings file."""
        secrets, etc = self.tmp / "run" / "secrets", self.tmp / "etc"
        shutil.rmtree(self.tmp / "run", ignore_errors=True)
        shutil.rmtree(etc, ignore_errors=True)
        secrets.mkdir(parents=True)
        for path in (self.trial.folder / "deploy" / "secrets").iterdir():
            text = path.read_text().replace("@db:5432/", f"@127.0.0.1:{self.port}/")
            (secrets / path.name).write_text(text)
        shutil.copytree(self.trial.folder / "deploy" / "config", etc)
        settings = etc / "deltasleuth.yaml"
        text = settings.read_text().replace("file:/run/secrets/", f"file:{secrets}/")
        for volume in ("evidence", "workspace"):
            (self.tmp / volume).mkdir(mode=0o700, exist_ok=True)
            text = text.replace(f"/var/lib/deltasleuth/{volume}", str(self.tmp / volume))
        settings.write_text(text)
        return settings

    def ds(self, settings: Path, *args: str, code: int = 0) -> str:
        """A ds command on ``settings``; its standard output."""
        result = CliRunner().invoke(cli, [*args, "--config", str(settings)])
        assert result.exit_code == code, result.output
        return result.stdout

    def setup(self, settings: Path) -> list[str]:
        """What the migrate service runs, as it runs it: its entrypoint's arguments, its secrets where the
        container mounts them."""
        secrets = settings.parent.parent / "run" / "secrets"
        args = [arg.replace("file:/run/secrets/", f"file:{secrets}/") for arg in SETUP]
        assert args[:3] == ["ds", "db", "setup"]
        return self.ds(settings, *args[1:]).splitlines()

    def owners(self) -> set[str]:
        """Who owns schema ds and what is in it."""
        listed = subprocess.run(
            ["docker", "exec", self.name, "psql", "-U", DB["environment"]["POSTGRES_USER"], "-d",
             DB["environment"]["POSTGRES_DB"], "-Atc",
             "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'ds'"
             " UNION SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relnamespace = 'ds'::regnamespace"
             " UNION SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE pronamespace = 'ds'::regnamespace"
             " UNION SELECT pg_get_userbyid(typowner) FROM pg_type WHERE typnamespace = 'ds'::regnamespace"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        return set(listed.stdout.split())


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[Kit]:
    if shutil.which("docker") is None:
        _unavailable("docker is not available")
    made = Kit(Trial(tmp_path / "trial", "sh"), tmp_path)
    yield made
    made.stop()


@pytest.fixture
def chain_lines() -> Iterator[list[str]]:
    """The lines logged on ``deltasleuth.store.chain``, from INFO up, wherever its records propagate."""
    lines: list[str] = []
    handler = logging.Handler(logging.INFO)
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    logger = logging.getLogger("deltasleuth.store.chain")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(level)


def _started(settings: Path, chain_lines: list[str]) -> str:
    """The app started on ``settings``, as the app container starts it, and stopped: its roles line."""
    with TestClient(create_app(load_settings(settings))):
        pass
    [line] = [line for line in chain_lines if line.startswith("roles: ")]
    chain_lines.clear()
    return line


def test_a_fresh_trial_makes_both_roles_and_the_app_runs_as_its_own(
    kit: Kit, chain_lines: list[str]
) -> None:
    """A fresh trial: the migrate service's run makes the schema owner's role and the app's own role, each
    signing in with its address's password, and migrates as the owner; the app's role owns nothing, and the
    app's start line and ``ds audit verify`` say ``roles: separate``. A second run changes nothing."""
    assert kit.trial.run().code == 0
    kit.start()
    settings = kit.mounted()
    owner = settings.parent.parent / "run" / "secrets" / "db_owner_dsn"
    assert load_settings(settings).migrate_dsn_ref == f"file:{owner}"
    first = kit.setup(settings)
    assert first[:3] == [
        "role deltasleuth_owner made",
        "role deltasleuth_app made",
        "role deltasleuth_owner may connect to and create in database deltasleuth",
    ]
    assert re.fullmatch(r"migrations applied: 1, 2, .*", first[3])
    assert first[4:] == ["grants of deltasleuth_app: as 'ds db grants' prints them", SEPARATE]
    assert kit.owners() == {"deltasleuth_owner"}
    assert kit.ds(settings, "init").startswith("workspace ready: ")
    assert _started(settings, chain_lines) == SEPARATE  # the app's start line, as deltasleuth_app
    verified = kit.ds(settings, "audit", "verify").splitlines()
    assert verified[0].startswith("chain: ") and SEPARATE in verified
    assert kit.setup(settings) == [
        "roles deltasleuth_owner and deltasleuth_app: as they were",
        "schema up to date",
        "grants of deltasleuth_app: as 'ds db grants' prints them",
        SEPARATE,
    ]


def test_an_update_of_a_one_role_trial_moves_its_database_to_two_roles_once(
    kit: Kit, chain_lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trial set up before the roles were apart: its app signed in as the superuser, which owns schema
    ds and the data. Run again, deploy/up.sh writes the two roles' addresses and the settings' line; the
    migrate service's run then makes the roles, hands schema ds to the owner and gives the app's role its
    data rights, so the app, signing in as its own role, reads what was there before. A second run of each
    changes nothing."""
    monkeypatch.setenv("DS_OPERATOR", "tester")
    trial = kit.trial
    assert trial.run().code == 0
    one_role(trial)
    kit.start()
    before = kit.mounted()  # as the app read them before: the superuser's address, no migrate_dsn_ref
    assert load_settings(before).migrate_dsn_ref is None
    kit.ds(before, "init")
    assert kit.ds(before, "migrate").startswith("migrations applied: 1, ")
    token = kit.ds(before, "token", "create", "--name", "kept", "--owner", "tests", "--role", "viewer")
    assert _started(before, chain_lines) == SHARED
    assert kit.owners() == {"deltasleuth"}
    held = trial.installed()
    assert trial.run(**held).code == 0  # db_dsn, db_owner_dsn and the settings' line
    settings = kit.mounted()
    moved = kit.setup(settings)
    assert moved[:4] == [
        "role deltasleuth_owner made",
        "role deltasleuth_app made",
        "role deltasleuth_owner may connect to and create in database deltasleuth",
        "schema ds handed from deltasleuth to deltasleuth_owner",
    ]
    granted = "grants of deltasleuth_app: as 'ds db grants' prints them"
    assert moved[4:] == ["schema up to date", granted, SEPARATE]
    assert kit.owners() == {"deltasleuth_owner"}
    assert _started(settings, chain_lines) == SEPARATE
    listed = kit.ds(settings, "token", "list")
    assert re.search(rf"^T-{token.split('_')[1]} \(kept\)  viewer  active ", listed, re.M), listed
    verified = kit.ds(settings, "audit", "verify").splitlines()
    assert verified[0].startswith("chain: ") and SEPARATE in verified
    files = trial.tree()
    assert trial.run(**held).code == 0 and trial.tree() == files
    assert kit.setup(kit.mounted())[:2] == ["roles deltasleuth_owner and deltasleuth_app: as they were",
                                            "schema up to date"]  # fmt: skip
