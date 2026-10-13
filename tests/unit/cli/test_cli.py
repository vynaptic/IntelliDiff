"""The ``ds`` CLI's own behaviour: its commands, exit codes (2 input, 3 a bug, 130 Ctrl-C; never 1 for
a crash), the LLM probe, and the host administrator's channel: who the CLI acts
as, API tokens, sessions, users, the audit log and the IdP check. The commands' work is tested end to end
in tests/e2e/test_cli.py."""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import importlib.util
import json
import logging
import os
import pwd
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import typer
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg.rows import dict_row
from rich.text import Text
from typer.testing import CliRunner, Result

from deltasleuth.api.app import AuthQueryFilter
from deltasleuth.cli.main import _bindable, app
from deltasleuth.connectors.tables import Refusal, TargetRefused, TargetUnreachable
from deltasleuth.connectors.tables import engine as seam
from deltasleuth.connectors.tables.engine import Column
from deltasleuth.core.spec import documents
from deltasleuth.reports import accuracy
from deltasleuth.security.signing import PackSigner
from deltasleuth.store import db
from mocks.fixtures import write_fixtures
from tests.conftest import (
    VENDOR_KEY,
    MySQLServer,
    OracleServer,
    encryption,
    licence_file,
    receiving_thread,
)
from tests.unit.auth.fake_idp import CLIENT_SECRET, FakeIdp
from tests.unit.connectors.engines import FakeEngine
from tests.unit.runner import prechecking

KEY_VALUE = "sk-cli-probe-0123456789abcdef"
SETTINGS = "database_dsn_ref: file:dsn\nevidence_dir: e\nworkspace_dir: w\ntargets_file: t.yaml\n"
TARGETS = """\
targets:
  svc: {base_url: "http://127.0.0.1:9/", allow_private_network: true, allow_insecure_http: true}
"""
OPERATOR = "set DS_OPERATOR to your user name"
MaybeAuth = dict[str, Any] | None


def ds(*args: Any) -> tuple[int, str]:
    result = CliRunner().invoke(app, [str(a) for a in args])
    return result.exit_code, result.output


def run(*args: Any) -> Result:
    return CliRunner().invoke(app, [str(a) for a in args])


@pytest.fixture(autouse=True)
def audit_logger() -> Iterator[logging.Logger]:
    """``deltasleuth.audit`` as configured by no one, restored afterwards: a command attaches its SIEM
    copy. (pytest adds its own handlers to a logger that does not propagate.)"""
    logger = logging.getLogger("deltasleuth.audit")
    handlers, propagate, level = logger.handlers, logger.propagate, logger.level
    logger.handlers, logger.propagate = [], True
    yield logger
    logger.handlers, logger.propagate = handlers, propagate
    logger.setLevel(level)


@pytest.fixture(autouse=True)
def mcp_loggers() -> Iterator[None]:
    """``deltasleuth.mcp`` and the MCP library's ``mcp`` as this process had them, restored afterwards:
    ``ds mcp`` and ``ds serve`` give each a handler of its own and stop its propagating.
    ``ds mcp`` gives the root logger one too, which goes again: pytest's own stay."""
    root, loggers = logging.getLogger(), [logging.getLogger(name) for name in ("deltasleuth.mcp", "mcp")]
    kept = [(logger.handlers, logger.propagate, logger.level) for logger in loggers]
    root_level = root.level
    for logger in loggers:
        logger.handlers = []
    yield
    for logger, (handlers, propagate, level) in zip(loggers, kept, strict=True):
        logger.handlers, logger.propagate = handlers, propagate
        logger.setLevel(level)
    root.handlers = [h for h in root.handlers if type(h).__module__ != "deltasleuth.cli.main"]
    root.propagate = True
    root.setLevel(root_level)


@pytest.fixture(autouse=True)
def a_free_port_and_a_built_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ds serve`` as on a machine whose port is free and whose UI is built: these tests serve on 8000,
    which a developer's machine may hold, and a checkout need not hold ``web/dist``. The tests of the two
    checks put them back."""
    monkeypatch.setattr("deltasleuth.cli.main._bindable", lambda host, port: None)
    monkeypatch.setattr("deltasleuth.api.app.WEB_DIST", tmp_path)


def helps() -> dict[str, str]:
    """Every help text of the command tree as written, by ``ds <command>`` or ``ds <command> <parameter>``."""

    def walk(command: Any, path: str) -> Iterator[tuple[str, str | None]]:
        yield path, command.help
        for parameter in command.params:
            yield f"{path} {parameter.name}", getattr(parameter, "help", None)
        for name, inner in getattr(command, "commands", {}).items():
            yield from walk(inner, f"{path} {name}")

    return {path: text for path, text in walk(typer.main.get_command(app), "ds") if text}


def test_help_lists_the_commands() -> None:
    code, out = ds("--help")
    commands = (
        "init migrate serve worker spec compare run runs report profile measure adjudicate score llm-probe"
        " token session users audit auth purge keys approvals packs licence samples labellings notify"
        " readiness schedules precheck target mcp"
    )
    assert code == 0 and all(name in out for name in commands.split())


def test_help_says_what_ds_does() -> None:
    written = {path: " ".join(text.split()) for path, text in helps().items()}
    assert (written["ds"], written["ds target"], written["ds token create name"], written["ds migrate"]) == (
        "DeltaSleuth: compare what two sources hold, key by key, black-box: REST services, database tables,"
        " CSV and Parquet files.",
        "Database and files targets: the pre-flight check.",
        "Token name, audited as its actor_name: 3 to 64 of a-z, 0-9, '.', '_' and '-', starting with a"
        " letter or a digit.",
        "Apply the pending database migrations (every command that opens the store applies them too).",
    )
    assert written["ds precheck draft"].endswith(
        " Never stored: when it is ready, upload it on the Specs screen or with POST /v1/specs, or, outside"
        " SSO, give the file to ds run, which stores the spec it runs."
    )
    assert not [path for path, text in written.items() if "ds spec or" in text]  # no command uploads a spec
    shown = " ".join(run("--help").stdout.replace("│", " ").split())
    assert "compare what two sources hold, key by key, black-box: REST services" in shown
    # A licence marks a run only in a build that enforces one: no word of ds --help's
    assert "newest first, each with what its spec approval says. report " in shown
    assert "or the P0 summary (report.md and report.html). profile " in shown
    assert all("a build that enforces a licence" in written[path] for path in ("ds runs", "ds report"))


def test_no_help_text_loses_words_to_markup() -> None:
    """typer prints a help text through rich, which takes ``[a-z0-9]`` for a style and drops it."""
    written = helps()
    assert len(written) > 300 and "ds token create name" in written
    assert {path: text for path, text in written.items() if Text.from_markup(text).plain != text} == {}


@pytest.mark.parametrize(
    ("tier", "failures", "said"),
    [
        ("high", 1, "high (≤ 0.01%, one-sided 95%): 47,437 cleanly compared keys pass with at most 1"
         " discrepant; draw 47,676 (0.5% margin); fix the size before any key is compared, never extend it"),
        ("standard", 0, "standard (≤ 0.1%, one-sided 95%): 2,995 cleanly compared keys pass with at most 0"
         " discrepant; draw 3,011 (0.5% margin); fix the size before any key is compared, never extend it"),
        ("standard", 5, "standard (≤ 0.1%, one-sided 95%): 10,511 cleanly compared keys pass with at most 5"
         " discrepant; draw 10,564 (0.5% margin); fix the size before any key is compared, never extend it"),
    ],
)  # fmt: skip
def test_samples_size_prints_single_stage_sizes_without_settings(
    monkeypatch: pytest.MonkeyPatch, tier: str, failures: int, said: str
) -> None:
    monkeypatch.delenv("DS_CONFIG", raising=False)
    assert ds("samples", "size", "--tier", tier, "--failures", failures) == (0, said + "\n")


def test_samples_size_refuses_a_tier_or_a_size_it_cannot_give() -> None:
    assert ds("samples", "size", "--tier", "medium") == (
        2,
        "error: --tier is standard or high, not 'medium'\n",
    )
    code, out = ds("samples", "size", "--tier", "standard", "--failures", 5000)
    assert (code, out) == (
        2,
        "error: 5,000 discrepant key(s) pass a 0.1% target only among over 1,000,000 cleanly compared keys:"
        " compare the population as a census\n",
    )


@pytest.mark.parametrize(
    ("raised", "code", "said"),
    [
        (ValueError("no settings file: pass --config or set DS_CONFIG"), 2, "error: no settings file"),
        (RuntimeError("a bug"), 3, "internal error"),
        (KeyError("counters"), 3, "internal error"),  # no code raises it on purpose: a bug, not bad input
        (KeyboardInterrupt(), 130, "interrupted"),
    ],
)
def test_failures_exit_with_their_code(monkeypatch: pytest.MonkeyPatch, raised: BaseException, code: int,
                                       said: str) -> None:  # fmt: skip
    def load(_: Any) -> None:
        raise raised

    monkeypatch.setattr("deltasleuth.cli.main.load_settings", load)
    exit_code, out = ds("init")
    assert exit_code == code and said in out


def test_a_spec_with_blocking_issues_exits_2(tmp_path: Path) -> None:
    (tmp_path / "spec.yaml").write_text("apiVersion: deltasleuth/v1\nkind: ComparisonSpec\n")
    code, out = ds("spec", "validate", tmp_path / "spec.yaml")
    assert code == 2 and "blocking issue(s) in spec.yaml" in out


def test_a_spec_error_prints_each_issue_once(tmp_path: Path) -> None:
    (tmp_path / "spec.yaml").write_text("apiVersion: deltasleuth/v1\n")
    (tmp_path / "targets.yaml").write_text("targets: {}\ntargets: {}\n")
    code, out = ds("spec", "validate", tmp_path / "spec.yaml", "--targets", tmp_path / "targets.yaml")
    assert code == 2 and out.count("duplicate key 'targets'") == 1, out


def test_a_path_that_is_not_there_says_where_a_container_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ds`` under ``docker compose run`` reads a path inside its container, which does not see the folder
    the command was typed in: there the refusal of a spec, a key file or a samples directory that is not
    there names the path as the container read it, and how a file of the host gets in. Outside a container
    the refusals stand as they were; so does a path that is there and cannot be read."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    (tmp_path / "t.yaml").write_text(TARGETS)
    (tmp_path / "a-folder").mkdir()
    (tmp_path / "spec.yaml").write_text("apiVersion: deltasleuth/v1\n")
    profile = ("--out", "draft", "--name", "d", "--target-a", "svc", "--target-b", "svc", "--key-name", "k")
    asked = {
        "/ [document_unreadable] cannot read mocks/customer/spec.yaml: No such file or directory": (
            ("spec", "validate", "mocks/customer/spec.yaml"),
            "mocks/customer/spec.yaml",
        ),
        "/ [document_unreadable] cannot read deploy/config/targets.yaml: No such file or directory": (
            ("spec", "validate", "spec.yaml", "--targets", "deploy/config/targets.yaml"),
            "deploy/config/targets.yaml",
        ),
        "No such file or directory: my-specs/keys.txt": (
            ("run", "spec.yaml", "--keys", "my-specs/keys.txt", "--config", "ds.yaml"),
            "my-specs/keys.txt",
        ),
        "sample root 'p0/samples' does not exist": (
            ("profile", "--samples", "p0/samples", *profile),
            "p0/samples",
        ),
    }

    def said() -> dict[str, tuple[int, str]]:
        return {refusal: ds(*command) for refusal, (command, _) in asked.items()}

    assert documents._CONTAINER == ("/.dockerenv", "/run/.containerenv")  # Docker's file, and Podman's
    marks = (str(tmp_path / ".dockerenv"), str(tmp_path / ".containerenv"))
    monkeypatch.setattr(documents, "_CONTAINER", marks)
    assert said() == {refusal: (2, f"error: {refusal}\n") for refusal in asked}
    (tmp_path / ".containerenv").touch()  # either file: a container
    assert said() == {
        refusal: (
            2,
            f"error: {refusal} (ds runs in a container, where this path is {tmp_path / path}: a file or"
            " folder of the host must first be copied in, with docker compose cp, or mounted)\n",
        )
        for refusal, (_, path) in asked.items()
    }
    # A path that is there: the container read the file the command meant, and says only why it could not.
    assert ds("spec", "validate", "a-folder") == (
        2,
        "error: / [document_unreadable] cannot read a-folder: Is a directory\n",
    )


def test_fail_on_diff_exits_1_while_keys_are_outstanding(monkeypatch: pytest.MonkeyPatch) -> None:
    counters = dict.fromkeys(("compared", "match", "different", "indeterminate", "cancelled"), 0)
    ended = {"total": 1, "outstanding": 1, "by_outcome": {"MISMATCH": 1}} | counters  # owed its recheck
    row = {"status": "completed_with_issues", "counters": ended, "stop_reason": "stop_condition:maxKeys"}
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: (row | {"error": None}, None))
    monkeypatch.setattr("deltasleuth.cli.main._local", lambda _: None)
    assert ds("run", "spec.yaml", "--fail-on-diff")[0] == 1


def test_a_run_its_frame_refused_prints_why_and_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = "the key listing of target svc-a was not read completely (HTTP 503: page 2: HTTP 503)"
    row = {"status": "paused", "stop_reason": "run_paused_draw", "error": None, "counters": {}}
    row["draw"] = {"due": "2026-09-30T00:00:00+00:00", "error": refused, "at": "2026-09-30T00:00:01Z"}
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: (row, None))
    monkeypatch.setattr("deltasleuth.cli.main._local", lambda _: None)
    assert ds("run", "spec.yaml") == (2, f"paused (run_paused_draw)\n  {refused}\nerror: the run is paused\n")


def test_a_sampled_run_prints_each_stratum_its_cycle_s_end_judged(monkeypatch: pytest.MonkeyPatch) -> None:
    counters = dict.fromkeys(
        ("total", "compared", "match", "different", "indeterminate", "cancelled", "outstanding"), 0
    )
    row = {
        "status": "completed",
        "stop_reason": None,
        "error": None,
        "counters": counters | {"by_outcome": {}},
    }
    stratum = {"label": "degree=d0", "tier": "standard", "target": "0.001", "drawn": 3011,
               "frame_keys": 1204331, "n": 3009, "x": 0, "unresolved": 2, "ub95_one_sided": "0.0009951",
               "verdict": "passed"}  # fmt: skip
    monkeypatch.setattr(
        "deltasleuth.cli.main._in_context", lambda *_: (row, {"sample": {"strata": [stratum]}})
    )
    monkeypatch.setattr("deltasleuth.cli.main._local", lambda _: None)
    code, out = ds("run", "spec.yaml")
    assert code == 0 and out.splitlines()[-1] == (
        "  stratum degree=d0 (standard, ≤ 0.1%): 3,011 drawn of 1,204,331, 3,009 compared, 0 discrepant, 2"
        " unresolved, bound 0.09951%: passed"
    )


def test_serve_refuses_a_public_host_without_the_auth_settings(tmp_path: Path) -> None:
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    code, out = ds("serve", "--host", "0.0.0.0", "--config", tmp_path / "ds.yaml")  # noqa: S104
    assert code == 2
    assert "error: serving on 0.0.0.0 needs the auth settings" in out


def test_serve_warns_of_developer_mode_and_a_configuration_error_exits_2_in_one_line(tmp_path: Path) -> None:
    (tmp_path / "ds.yaml").write_text(SETTINGS)  # no ds init: the workspace has no tokeniser key
    code, out = ds("serve", "--config", tmp_path / "ds.yaml")
    warning = (
        "warning: developer mode (no auth settings): every local user and process acts as admin; never"
        " expose this port, directly or through a reverse proxy or port forward"
    )
    assert code == 2 and warning in out and "Traceback" not in out
    assert [line for line in out.splitlines() if line.startswith("error:")] == [
        f"error: tokeniser key {tmp_path / 'w' / 'tokeniser.key'} not found; run 'ds init' first"
    ]


def test_init_creates_the_workspace_keys_only_in_developer_mode(tmp_path: Path) -> None:
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    workspace = tmp_path / "w"
    assert ds("init", "--config", tmp_path / "ds.yaml") == (
        0,
        f"workspace ready: tokeniser key {workspace / 'tokeniser.key'}, keyring {workspace / 'kek'};"
        " keep a copy of both somewhere safe: a lost key cannot be made again\n",
    )
    (tmp_path / "prod").mkdir()
    doc = {"auth": {"public_url": "https://ds.example"}, "encryption": encryption(tmp_path / "secrets")}
    (tmp_path / "prod" / "ds.yaml").write_text(SETTINGS + yaml.safe_dump(doc))
    assert ds("init", "--config", tmp_path / "prod" / "ds.yaml") == (
        0,
        f"workspace ready: {tmp_path / 'prod' / 'w'} (keys: the encryption settings' secrets)\n",
    )
    assert list((tmp_path / "prod" / "w").iterdir()) == []
    (tmp_path / "prod" / "w" / "tokeniser.key").write_text("ab" * 32 + "\n")  # not moved yet
    assert ds("init", "--config", tmp_path / "prod" / "ds.yaml") == (
        2,
        "error: tokeniser.key was moved to encryption.tokeniser_key_ref: delete it from the workspace\n",
    )


def test_serve_reads_the_idp_client_credential_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: pytest.fail("the server must not start"))
    oidc = {
        "issuer": "https://idp.example",
        "client_id": "deltasleuth",
        "client_secret_ref": f"file:{tmp_path / 'absent'}",
        "roles": {"admin": ["DS.Admin"]},
    }
    auth = {"auth": {"public_url": "https://ds.example", "oidc": oidc}, "encryption": encryption(tmp_path)}
    (tmp_path / "ds.yaml").write_text(SETTINGS + yaml.safe_dump(auth))
    code, out = ds("serve", "--host", "0.0.0.0", "--config", tmp_path / "ds.yaml")  # noqa: S104
    assert code == 2 and out.startswith(f"error: cannot read secret file {tmp_path / 'absent'}: ")


def test_serve_leaves_proxy_headers_to_the_app_and_sign_in_queries_out_of_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_logger: logging.Logger
) -> None:
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "filters", [])  # restored after the test
    ran: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **options: ran.update(options))
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: None)
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    code, _ = ds("serve", "--config", tmp_path / "ds.yaml")
    assert (code, ran["proxy_headers"], audit_logger.propagate) == (0, False, False)
    assert [type(f) for f in access.filters] == [AuthQueryFilter]
    assert [h.stream for h in audit_logger.handlers] == ["stdout"]  # type: ignore[attr-defined]


def test_serve_refuses_a_taken_port_before_it_opens_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last refusal before the store is opened, so no ``system.start``
    row is written and no worker starts; the driver's own exit would be 3, with a traceback."""
    monkeypatch.setattr("deltasleuth.cli.main._bindable", _bindable)
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: pytest.fail("the store must not open"))
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: pytest.fail("the server must not start"))
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    with socket.create_server(("127.0.0.1", 0)) as held:
        port = held.getsockname()[1]
        taken = run("serve", "--port", port, "--config", tmp_path / "ds.yaml")
    auth = {"auth": {"public_url": "https://ds.example"}, "encryption": encryption(tmp_path)}
    (tmp_path / "ds.yaml").write_text(SETTINGS + yaml.safe_dump(auth))
    elsewhere = run("serve", "--host", "192.0.2.1", "--port", port, "--config", tmp_path / "ds.yaml")
    assert [(r.exit_code, [line for line in r.stderr.splitlines() if line.startswith("error:")])
            for r in (taken, elsewhere)] == [
        (2, [f"error: cannot listen on 127.0.0.1:{port}: the port is in use; stop the program that holds"
             " it, or pass --port"]),
        (2, [f"error: cannot listen on 192.0.2.1:{port}: {os.strerror(errno.EADDRNOTAVAIL)}"]),  # not ours
    ]  # fmt: skip
    assert "Traceback" not in taken.output + elsewhere.output
    assert _bindable("127.0.0.1", 0) is None and _bindable("localhost", 0) is None  # a free port
    for none in (70000, -1):  # a mistyped port is no bug of DeltaSleuth's (exit 3): the option refuses it
        mistyped = run("serve", "--port", none, "--config", tmp_path / "ds.yaml")
        assert mistyped.exit_code == 2 and "Traceback" not in mistyped.output, none
        said = " ".join(mistyped.stderr.replace("│", " ").split())  # typer frames and wraps the line
        assert f"Invalid value for '--port': {none} is not in the range 0<=x<=65535." in said


def test_serve_warns_when_the_ui_is_not_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On stderr, and the server starts all the same."""
    ran: list[Any] = []
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: ran.append(1))
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: None)
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    built = run("serve", "--config", tmp_path / "ds.yaml")
    dist = tmp_path / "web" / "dist"
    monkeypatch.setattr("deltasleuth.api.app.WEB_DIST", dist)
    unbuilt = run("serve", "--config", tmp_path / "ds.yaml")
    warning = (
        f"warning: the web UI is not built ({dist} is missing): / answers 404 and only the API under /v1 is"
        " served; run 'npm --prefix web ci' and 'npm --prefix web run build' in the checkout, then start ds"
        " serve again"
    )
    assert (built.exit_code, unbuilt.exit_code, len(ran)) == (0, 0, 2)
    assert "the web UI" not in built.stderr and unbuilt.stderr.splitlines().count(warning) == 1
    assert unbuilt.stdout == ""


@pytest.mark.parametrize(("option", "worker"), [((), True), (("--no-worker",), False)])
def test_serve_runs_the_worker_in_its_process_unless_told_not_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: tuple[str, ...], worker: bool
) -> None:
    made: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **options: None)
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: None)
    monkeypatch.setattr("deltasleuth.api.create_app", lambda settings, **options: made.update(options))
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    code, _ = ds("serve", *option, "--config", tmp_path / "ds.yaml")
    assert (code, made) == (0, {"worker": worker})


# --- ds target check ----------------------------------------------------------

DATABASE_TARGETS = (
    TARGETS
    + """\
  crm-legacy:
    engine: oracle
    host: crm-db.bank.example
    service: CRMPDB
    user: ds_reader
    password_ref: file:crm_legacy_password
    tls_server_dn: "CN=crm-db.bank.example,O=Bank"
"""
)
CUSTOMERS: list[str | Column] = [
    Column("CUSTOMER_ID", "NUMBER(10)", "integer", True, False),
    Column("EMAIL", "VARCHAR2(120 CHAR)", "text", True, True),
    Column("OPENED", "DATE", "timestamp", True, False),
    Column("NOTE", "CLOB", "text", True, True, keyable=False, lob=True),
    Column("GEOM", "SDO_GEOMETRY", None, True, False, keyable=False),
]


def check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: FakeEngine, *args: str) -> tuple[int, str]:
    """``ds target check ARGS`` with ``engine`` in Oracle's place; no store's DSN file exists."""
    monkeypatch.setitem(seam.ENGINES, "oracle", engine)
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    (tmp_path / "t.yaml").write_text(DATABASE_TARGETS)
    return ds("target", "check", *args, "--config", tmp_path / "ds.yaml")


def test_target_check_prints_the_full_version_its_notes_and_a_table_s_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine({"CUSTOMERS": CUSTOMERS})
    engine.served = {"version": "19.21.0.0.0", "charset": "WE8MSWIN1252", "tls": True}
    assert check(tmp_path, monkeypatch, engine, "crm-legacy", "--table", "APP.CUSTOMERS") == (
        0,
        "target crm-legacy: oracle 19.21.0.0.0, character set WE8MSWIN1252, TLS yes, session ok\n"
        "note: this release is observed on Oracle 23.26 and MySQL 8.4 only\n"
        "note: a run takes text keys of up to 500 bytes in this character set\n"
        "public.CUSTOMERS (table, consistency snapshot)\n"
        "  CUSTOMER_ID  NUMBER(10)          integer    key\n"
        "  EMAIL        VARCHAR2(120 CHAR)  text       key\n"
        "  OPENED       DATE                timestamp  key\n"
        "  NOTE         CLOB                text\n"
        "  GEOM         SDO_GEOMETRY        -          not comparable\n",
    )
    # An observed server in AL32UTF8 has no note; without --table only the session is checked.
    engine.served = {"version": "23.26.3.0.0", "charset": "AL32UTF8", "tls": False}
    assert check(tmp_path, monkeypatch, engine, "crm-legacy") == (
        0,
        "target crm-legacy: oracle 23.26.3.0.0, character set AL32UTF8, TLS no, session ok\n",
    )
    assert (engine.opened, engine.closed) == (2, 2)


def test_target_check_prints_what_a_server_says_of_itself_without_its_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A MySQL greeting's version and a server's names are the server's to choose: an escape sequence
    # once reached the operator's terminal as it came.
    engine = FakeEngine({"CUSTOMERS": [Column("ID\x1b[2J", "NUMBER(10)\x07", "integer", True, False)]})
    engine.served = {
        "version": "23.26.3\x1b]0;owned\x07",
        "charset": "AL32UTF8\x1b[2J" + "x" * 500,
        "tls": True,
    }
    code, out = check(tmp_path, monkeypatch, engine, "crm-legacy", "--table", "CUSTOMERS")
    assert code == 0 and not [ch for ch in out if not ch.isprintable() and ch != "\n"]
    assert out.splitlines() == [
        "target crm-legacy: oracle 23.26.3]0;owned, character set AL32UTF8[2J"
        + "x" * 53  # 64 characters at most
        + ", TLS yes, session ok",
        "note: a run takes text keys of up to 500 bytes in this character set",
        "public.CUSTOMERS (table, consistency snapshot)",
        "  ID[2J  NUMBER(10)  integer  key",
    ]


def test_target_check_exits_2_with_a_run_s_own_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked: list[str | Column] = [Column("ID", "NUMBER(10)", "integer", False, False)]

    def refused(said: str, *args: str, **failing: Any) -> None:
        engine = FakeEngine({"CUSTOMERS": CUSTOMERS, "LOCKED": locked}, **failing)
        assert check(tmp_path, monkeypatch, engine, *args) == (2, f"error: {said}\n")
        assert engine.opened == engine.closed  # a session opened is closed, whatever it found

    refused("target 'nope' is not in the registry", "nope")
    refused("target svc is not a database target (dsn_ref or engine)", "svc")
    refused(
        "target crm-legacy: cannot connect to target crm-legacy (oracle crm-db.bank.example:1521/CRMPDB as"
        " ds_reader): DPY-6001: the listener does not know the service",
        "crm-legacy",
        connect=[TargetUnreachable("DPY-6001", "the listener does not know the service")],
    )
    old = "target crm-legacy is Oracle 12.2: 19c or later is supported"
    trigger = "the session could not be set (NLS_SORT is BINARY_CI): a logon trigger overrides it"
    orders = "the role may not execute UTL_I18N, which orders text keys in a WE8MSWIN1252 database"
    for method, refusal in (
        ("connect", old),
        ("connect", trigger),
        ("server", f"{orders}: grant it EXECUTE"),
    ):
        refused(f"target crm-legacy: {refusal}", "crm-legacy", **{method: [Refusal(refusal)]})
    # A relation that is missing, one behind a database link, and one the role may not read.
    missing = "table or view APP.NOPE not found or not visible"
    refused(f"target crm-legacy: {missing}", "crm-legacy", "--table", "APP.NOPE")
    linked = "table or view APP.LINKED not found or not visible: a synonym over a database link is not read"
    refused(f"target crm-legacy: {linked}", "crm-legacy", "--table", "APP.LINKED", describe=[Refusal(linked)])
    denied = "the role may not read public.LOCKED: grant it SELECT"
    refused(f"target crm-legacy: {denied}", "crm-legacy", "--table", "LOCKED")
    # An error after the connect was once a traceback and exit 3 (an invalid view: ORA-04063).
    refused(
        "target crm-legacy: table or view APP.BROKEN_V cannot be described: ORA-04063",
        *("crm-legacy", "--table", "APP.BROKEN_V"),
        describe=[TargetRefused("ORA-04063")],
    )
    refused(
        "target crm-legacy: the session's account of its server could not be read: ORA-03113",
        "crm-legacy",
        server=[TargetUnreachable("ORA-03113")],
    )


def test_the_profiler_drafts_service_specs_only(tmp_path: Path) -> None:
    # No draft is made for a table side: a database target, whatever its engine, refuses
    # the drafted side, and `ds target check` is what a table spec's author reads.
    write_fixtures("linker", tmp_path / "samples")
    (tmp_path / "t.yaml").write_text(DATABASE_TARGETS)
    sides = ("--target-a", "crm-legacy", "--target-b", "svc", "--key-name", "entity_code")
    code, out = ds("profile", "--samples", tmp_path / "samples", "--out", tmp_path / "draft", "--name", "d",
                   *sides, "--targets", tmp_path / "t.yaml")  # fmt: skip
    assert (
        code == 2 and "/sides/a/target [target_kind] this side needs a service target, not a database" in out
    )


def checked(tmp_path: Path, targets: dict[str, Any], *args: str) -> list[str]:
    """What ``ds target check ARGS`` prints of ``targets`` when it succeeds, by line."""
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    (tmp_path / "t.yaml").write_text(yaml.safe_dump({"targets": targets}))
    code, out = ds("target", "check", *args, "--config", tmp_path / "ds.yaml")
    assert code == 0, out
    return out.splitlines()


FILES_SETTINGS = SETTINGS + "files: {{roots_under: {base}/files, memory: 256MB, spill: 1GB}}\n"


def files_check(
    tmp_path: Path, target: dict[str, Any], *args: str, others: dict[str, Any] | None = None,
    settings: str = "}",
) -> tuple[int, str]:  # fmt: skip
    """``ds target check legacy ARGS`` of a files target under a temporary
    files.roots_under, the settings in a directory of their own (``settings``: what its files block ends
    with), ``others`` beside it in the targets file; no store's DSN file exists."""
    base = tmp_path.resolve()
    (base / "files" / "legacy").mkdir(parents=True, exist_ok=True)
    (base / "etc").mkdir(exist_ok=True)
    (base / "etc" / "ds.yaml").write_text(FILES_SETTINGS.format(base=base).replace("}", settings))
    (base / "etc" / "t.yaml").write_text(
        yaml.safe_dump({"targets": {"legacy": {"engine": "files"} | target} | (others or {})})
    )
    return ds("target", "check", "legacy", *args, "--config", base / "etc" / "ds.yaml")


def test_target_check_of_a_files_target(tmp_path: Path) -> None:
    """Its first line: the mount, DuckDB's version and what is compiled in,
    and the process it is read in with the limits that reader's start saw hold, after the checks of its root
    and the instance's own proof; a refusal exits 2, worded for the target."""
    from deltasleuth.connectors.tables import reader

    base = tmp_path.resolve()
    legacy = {"root": str(base / "files" / "legacy"), "allow_writable": True}
    none = "not on this platform"
    filtered = reader.LINUX and os.uname().machine in reader.FILTERED
    held = (  # 256 MiB of memory, 2 GiB, and 600 MiB for each of 2 threads
        f"{'data limit 3,504 MiB' if reader.LINUX else f'no data limit ({none})'},"
        f" {'sockets denied' if filtered else f'sockets not denied ({none})'}"
    )
    line = (
        "target legacy: files, writable, duckdb 1.5.5, parquet and core_functions built in, no extension"
        f" loads, read in a process of its own ({held})\n"
    )
    assert files_check(tmp_path, legacy) == (0, line)
    off = "files.os_limits is false"
    assert files_check(tmp_path, legacy, settings=", os_limits: false}") == (
        0, line.replace(held, f"no data limit ({off}), sockets not denied ({off})"),
    )  # fmt: skip
    code, out = files_check(tmp_path, {"root": str(base / "files" / "legacy")})
    assert (code, out) == (
        2,
        "error: target legacy: the directory of target legacy is mounted writable: mount it read-only (:ro),"
        " or declare allow_writable: true on the target\n",
    )
    code, out = files_check(tmp_path, {"root": str(base / "files" / "gone"), "allow_writable": True})
    assert code == 2 and out.startswith("error: target legacy: the directory of target legacy is not there")
    code, out = files_check(tmp_path, {"root": str(base / "etc"), "allow_writable": True})
    assert code == 2 and out == (
        f"error: root of target legacy must lie under {base}/files (files.roots_under) and outside the"
        " directories DeltaSleuth uses (evidence, workspace, spill, settings, targets, a secret): mount"
        " exports in a directory of their own\n"
    )
    code, out = files_check(tmp_path, legacy, "--table", "customers/*.csv")
    assert (code, out) == (
        2, "error: target legacy: --table of a files target is a path and needs --format csv or parquet\n",
    )  # fmt: skip


def test_target_check_drafts_a_csv_side_and_prints_its_manifest(tmp_path: Path) -> None:
    """The files a path names, a draft sniffed from file 1 (fields named ``column n`` without --header,
    so no value of a headerless file is printed), a count of the drafted dialect with its lines that are not
    rows by position and kind, and ``sha256sum``'s lines with files_sha256 as a plan pins it."""
    base = tmp_path.resolve()
    root = base / "files" / "legacy"
    (root / "customers").mkdir(parents=True)
    head = "customer_id|email|balance|opened\r\n"
    one = head + "00123|a@x|1.50|29/02/2024\r\n00124|b@x|-0.50|31/12/2023\r\n"
    two = head + "125|c@x|10|01/01/2024\r\nnot|a row\r\n126|d@x|2.00|02/01/2024\r\n"
    (root / "customers" / "c-1.csv").write_bytes(one.encode())
    (root / "customers" / "c-2.csv").write_bytes(two.encode())
    legacy = {"root": str(root), "allow_writable": True, "settle_s": 0}
    asked = ("--table", "customers/*.csv", "--format", "csv")
    code, out = files_check(tmp_path, legacy, *asked)
    lines = out.splitlines()
    assert code == 0 and re.fullmatch(
        rf"customers/\*\.csv \(csv\): 2 file\(s\), {len(one) + len(two)} bytes, newest modified"
        r" \d{4}-\d\d-\d\d \d\d:\d\d UTC, settled",
        lines[1],
    )
    assert re.fullmatch(rf"  1  customers/c-1\.csv  {len(one)}  \d{{4}}-\d\d-\d\d \d\d:\d\d UTC", lines[2])
    assert lines[4:] == [
        "draft (sniffed from the first 20,480 rows of file 1): a run reads only what the approved spec"
        " declares",
        '  csv: {delimiter: "|", newline: "\\r\\n", header: true, columns: [column 1, column 2, column 3,'
        " column 4]}",
        '  typeMap: {column 3: {as: decimal}, column 4: {as: date, format: "%d/%m/%Y"}}',
        "note: the first record of file 1 reads as a header: --header names its fields from it",
        "note: column 1 holds only digits and 2 sampled values start with 0: left as text",
    ]
    assert "customer_id" not in out and "a@x" not in out  # no record's text without --header
    code, out = files_check(tmp_path, legacy, *asked, "--header", "--delimiter", "|", "--count", "--manifest")
    lines = out.splitlines()
    assert code == 0 and lines[5:7] == [
        '  csv: {delimiter: "|", newline: "\\r\\n", header: true, columns: [customer_id, email, balance,'
        " opened]}",
        '  typeMap: {balance: {as: decimal}, opened: {as: date, format: "%d/%m/%Y"}}',
    ]
    assert lines[7] == "note: customer_id holds only digits and 2 sampled values start with 0: left as text"
    assert re.fullmatch(r"count \(as drafted\): 4 rows, a pass \d+\.\d s, 1 line\(s\) not rows", lines[8])
    assert lines[9] == "  file 2 line 3: columns"
    digests = [hashlib.sha256(text.encode()).hexdigest() for text in (one, two)]
    listed = [f"{digests[0]}  customers/c-1.csv", f"{digests[1]}  customers/c-2.csv"]
    # A side that declares partitions compares its directories' names: its digest is of the lines whole.
    bound = hashlib.sha256("".join(f"{line}\n" for line in listed).encode()).hexdigest()
    assert lines[10:] == [
        *listed,
        "files_sha256 " + hashlib.sha256("".join(f"{d}\n" for d in digests).encode()).hexdigest(),
        f"files_sha256 {bound} where the spec declares partitions (the lines above, whole)",
    ]
    # A path outside file.path's grammar, a format that is neither, a path that names nothing: exit 2.
    for path in ("../etc/*.csv", "/customers/c-1.csv", "customers/.hidden"):
        code, out = files_check(tmp_path, legacy, "--table", path, "--format", "csv")
        assert code == 2 and out.startswith("error: file.path is a relative path under the target's root: ")
    code, out = files_check(tmp_path, legacy, "--table", "customers/", "--format", "xlsx")
    assert (code, out) == (2, "error: --format is csv or parquet\n")
    code, out = files_check(tmp_path, legacy, "--table", "orders/*.csv", "--format", "csv")
    assert (code, out) == (2, "error: target legacy: no file matches orders/*.csv under target legacy\n")


def test_target_check_of_a_mount_that_does_not_answer_and_of_other_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that does not answer within its deadline is refused in a plan's words for it, never a
    traceback; a gzip export drafts its compression and is read by it; a file whose lines end with a lone
    carriage return is refused, since no csv.newline declares it."""
    import gzip

    from deltasleuth.connectors.tables import manifest

    base = tmp_path.resolve()
    root = base / "files" / "legacy"
    root.mkdir(parents=True)
    legacy = {"root": str(root), "allow_writable": True, "settle_s": 0}
    (root / "c.csv.gz").write_bytes(gzip.compress(b"id,v\n1,a\n2,b\n"))
    code, out = files_check(tmp_path, legacy, "--table", "c.csv.gz", "--format", "csv", "--header", "--count")
    lines = out.splitlines()
    assert code == 0 and lines[4] == (
        '  csv: {delimiter: ",", newline: "\\n", header: true, compression: "gzip", columns: [id, v]}'
    )
    assert re.fullmatch(r"count \(as drafted\): 2 rows, a pass \d+\.\d s, 0 line\(s\) not rows", lines[-1])
    (root / "old.csv").write_bytes(b"id,v\r1,a\r2,b\r")
    code, out = files_check(tmp_path, legacy, "--table", "old.csv", "--format", "csv")
    assert (code, out) == (
        2, "error: target legacy: file 1 ends its lines with neither \\n nor \\r\\n: convert the export\n",
    )  # fmt: skip
    waiting = threading.Event()
    monkeypatch.setattr(manifest, "STAT_S", 0.2)
    monkeypatch.setattr(manifest, "_rooted", lambda root: waiting.wait(30) and "writable")
    try:
        code, out = files_check(tmp_path, legacy)
    finally:
        waiting.set()
    assert (code, out) == (
        2, "error: target legacy: the directory of target legacy did not answer within 0.2 s: is its mount"
        " alive?\n",
    )  # fmt: skip
    deadline = time.monotonic() + 10
    while manifest.stuck() and time.monotonic() < deadline:  # the abandoned call returns: none waits
        time.sleep(0.01)
    assert manifest.stuck() == 0


def test_target_check_of_a_root_that_does_not_resolve_never_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check of where roots lie resolves every files target's root, on the files threads under STAT_S
    as a context's opening does (it ran in the command's own thread with no deadline). A mount whose
    realpath never returns is judged as written: the check of another target goes on, and the dead one's
    own says its directory did not answer."""
    from deltasleuth.connectors.tables import manifest

    base = tmp_path.resolve()
    root, dead = base / "files" / "legacy", base / "files" / "dead"
    dead.mkdir(parents=True)
    waiting, resolve = threading.Event(), os.path.realpath

    def resolving(path: Any, **kwargs: Any) -> str:
        if str(path).startswith(str(dead)):
            waiting.wait(30)
        return resolve(path, **kwargs)

    monkeypatch.setattr(manifest, "STAT_S", 0.2)
    monkeypatch.setattr(os.path, "realpath", resolving)
    legacy = {"root": str(root), "allow_writable": True}
    other = {"engine": "files", "root": str(dead), "allow_writable": True}
    started = time.monotonic()
    try:
        code, out = files_check(tmp_path, legacy, others={"other": other})
        assert code == 0 and out.startswith("target legacy: files, writable, duckdb ")
        code, out = files_check(tmp_path, {"root": str(dead), "allow_writable": True})
        assert (code, out) == (
            2, "error: target legacy: the directory of target legacy did not answer within 0.2 s: is its"
            " mount alive?\n",
        )  # fmt: skip
        assert time.monotonic() - started < 10
    finally:
        waiting.set()
    deadline = time.monotonic() + 10
    while manifest.stuck() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manifest.stuck() == 0


def test_target_check_reads_a_compressed_export_s_streams_whole(tmp_path: Path) -> None:
    """--count and --manifest pin a gzip or zstd export as a plan pins it, each stream read to its
    end, so one cut short in transfer is refused as unreadable, by its class: DuckDB reads it as fewer rows
    with no error, and the check printed that count (164 of 200 rows) and a manifest, exit 0."""
    import gzip

    import zstandard

    base = tmp_path.resolve()
    root = base / "files" / "legacy"
    root.mkdir(parents=True)
    legacy = {"root": str(root), "allow_writable": True, "settle_s": 0}
    data = b"id,v\n" + b"".join(f"{n},value {n}\n".encode() for n in range(200))
    for name, packed, cut, said in (
        ("cut.csv.gz", gzip.compress(data), 200, "BadGzipFile"),
        ("cut.csv.zst", zstandard.ZstdCompressor().compress(data), 20, "ZstdError"),
    ):
        asked = ("--table", name, "--format", "csv", "--header")
        (root / name).write_bytes(packed)
        code, out = files_check(tmp_path, legacy, *asked, "--count", "--manifest")
        lines = out.splitlines()
        assert code == 0 and lines[6].startswith("count (as drafted): 200 rows, ")
        assert lines[7] == f"{hashlib.sha256(packed).hexdigest()}  {name}"
        (root / name).write_bytes(packed[:-cut])
        for options in (("--count",), ("--manifest",), ("--count", "--manifest")):
            code, out = files_check(tmp_path, legacy, *asked, *options)
            assert (code, out) == (
                2, f"error: target legacy: its files could not be read while the run was planned ({said})\n"
            ), options  # fmt: skip


def test_target_check_drafts_an_escape_and_a_wide_file_whole(tmp_path: Path) -> None:
    """A sniffed escape that is not the quote is drafted beside the default quote (a backslash-escaped
    export, MySQL's OUTFILE: without it the drafted dialect read none of its rows), and a draft's line is
    printed whole, however wide the file (it was cut at 4,000 characters, mid-name)."""
    base = tmp_path.resolve()
    root = base / "files" / "legacy"
    root.mkdir(parents=True)
    legacy = {"root": str(root), "allow_writable": True, "settle_s": 0}
    escaped = "id,name,note\n" + "".join(f'{n},"n{n}","say \\"hi\\" {n}"\n' for n in range(1, 51))
    (root / "esc.csv").write_text(escaped)
    code, out = files_check(tmp_path, legacy, "--table", "esc.csv", "--format", "csv", "--header", "--count")
    lines = out.splitlines()
    assert code == 0 and lines[4] == (
        '  csv: {delimiter: ",", newline: "\\n", header: true, escape: "\\\\", columns: [id, name, note]}'
    )
    assert re.fullmatch(r"count \(as drafted\): 50 rows, a pass \d+\.\d s, 0 line\(s\) not rows", lines[-1])
    names = [f"column_name_{n:04d}" for n in range(500)]
    (root / "wide.csv").write_text(",".join(names) + "\n" + ",".join(["x"] * 500) + "\n")
    code, out = files_check(tmp_path, legacy, "--table", "wide.csv", "--format", "csv", "--header")
    draft = out.splitlines()[4]
    assert code == 0 and len(draft) > 9000 and draft.endswith(f"columns: [{', '.join(names)}]}}")


def test_target_check_drafts_an_export_with_a_trailer_from_its_rows(tmp_path: Path) -> None:
    """One line of another shape, a trailer record after the acceptance kit's CSV, defeated the draft:
    it drafted ``delimiter: "|"`` and one column and counted the trailer as a row, and held to the file's
    delimiter it ended as unreadable files with DuckDB's class. The draft is sniffed from the lines that are
    rows and says so, under a delimiter given too, and --count lists the trailer as a run of the dialect
    declared by hand does. An export in another encoding is still no draft, in words that name no class."""
    base = tmp_path.resolve()
    root = base / "files" / "legacy"
    kit = Path(__file__).parents[3] / "examples" / "tables" / "files"
    subprocess.run([sys.executable, str(kit / "generate.py"), str(root)], check=True)
    (root / "accounts_t").mkdir()
    exported = (root / "accounts_a" / "accounts.csv").read_bytes()
    (root / "accounts_t" / "accounts.csv").write_bytes(exported + b"TRAILER;1000\n")
    legacy = {"root": str(root), "allow_writable": True, "settle_s": 0}
    asked = ("--table", "accounts_t/accounts.csv", "--format", "csv")
    rows_only = "note: drafted from the lines of file 1 that read as rows: --count lists the others"
    for given in ((), ("--delimiter", ";")):
        code, out = files_check(tmp_path, legacy, *asked, *given, "--header", "--count")
        lines = out.splitlines()
        assert code == 0 and lines[4:8] == [
            '  csv: {delimiter: ";", newline: "\\n", header: true, columns: [account_id, holder, balance,'
            " opened, status]}",
            '  typeMap: {opened: {as: date, format: "%d/%m/%Y"}}',
            rows_only,
            "note: account_id holds only digits and 1,000 sampled values start with 0: left as text",
        ], given
        assert re.fullmatch(
            r"count \(as drafted\): 1,000 rows, a pass \d+\.\d s, 1 line\(s\) not rows", lines[8]
        )
        assert lines[9:] == ["  file 1 line 1002: columns"]
        code, out = files_check(tmp_path, legacy, *asked, *given)  # without --header no name is printed
        assert code == 0 and out.splitlines()[4:] == [
            '  csv: {delimiter: ";", newline: "\\n", header: true, columns: [column 1, column 2, column 3,'
            " column 4, column 5]}",
            '  typeMap: {column 4: {as: date, format: "%d/%m/%Y"}}',
            "note: the first record of file 1 reads as a header: --header names its fields from it",
            rows_only,
            "note: column 1 holds only digits and 1,000 sampled values start with 0: left as text",
        ], given
    code, out = files_check(tmp_path, legacy, "--table", "accounts_a/accounts.csv", "--format", "csv")
    assert code == 0 and rows_only not in out  # the kit's own export: every line a row
    (root / "latin.csv").write_bytes("id;name\n1;caf\u00e9\n".encode("latin-1"))
    for given in ((), ("--delimiter", ";"), ("--header", "--count")):
        assert files_check(tmp_path, legacy, "--table", "latin.csv", "--format", "csv", *given) == (
            2,
            "error: target legacy: no CSV dialect could be drafted from file 1: write its csv block by hand"
            " (delimiter, header, quote, escape, newline, encoding)\n",
        ), given


def test_target_check_names_a_parquet_side_s_columns_and_classes(tmp_path: Path) -> None:
    """A Parquet side prints each column's type and class in place of a draft; a type no plan reads is
    not comparable, a nested one named by its head (what follows is a file's text); --count
    gives the footers' rows."""
    from tests.unit.connectors.ducked import unrestricted

    base = tmp_path.resolve()
    root = base / "files" / "legacy"
    root.mkdir(parents=True)
    unrestricted().execute(
        "COPY (SELECT 1::BIGINT AS id, 1.50::DECIMAL(12, 2) AS amount, 'a' AS note, [1, 2] AS tags"
        f" UNION ALL SELECT 2, 2.00, 'b', [3]) TO '{root / 'users.parquet'}' (FORMAT parquet)"
    )
    legacy = {"root": str(root), "allow_writable": True, "settle_s": 0}
    code, out = files_check(tmp_path, legacy, "--table", "users.parquet", "--format", "parquet", "--count")
    lines = out.splitlines()
    assert code == 0 and lines[1].startswith("users.parquet (parquet): 1 file(s), ")
    assert lines[3:7] == [
        "  id      BIGINT         integer",
        "  amount  DECIMAL(12,2)  decimal",
        "  note    VARCHAR        text",
        "  tags    LIST           not comparable",
    ]
    assert re.fullmatch(r"count: 2 rows by the footers, a pass over the first column \d+\.\d s", lines[7])
    # A column DuckDB reads to the microsecond (an INT96 timestamp) names the typeMap a plan needs.
    from tests.support import parquet_tiny

    legacy_ = [parquet_tiny.byte_array("k", b"1"), parquet_tiny.int96("seen", 2460311, 123_456_789)]
    (root / "old.parquet").write_bytes(parquet_tiny.write(legacy_))
    code, out = files_check(tmp_path, legacy, "--table", "old.parquet", "--format", "parquet")
    assert code == 0 and out.splitlines()[3:] == [
        "  k     BLOB       bytes",
        "  seen  TIMESTAMP  read to the microsecond: a plan needs typeMap {seen: {as: timestamp}}",
    ]
    # A CSV file read as Parquet is refused by the reader's class, never its text.
    (root / "users.csv").write_text("id\n1\n")
    code, out = files_check(tmp_path, legacy, "--table", "users.csv", "--format", "parquet")
    assert code == 2 and re.fullmatch(
        r"error: target legacy: its files could not be read while the run was planned \([A-Za-z]+\)\n", out
    )


def test_every_command_refuses_a_root_where_it_may_not_lie(tmp_path: Path) -> None:
    """A root that lies where none may is refused as a context opens, before the store: every command that
    opens one exits 2."""
    from deltasleuth.config import load_settings
    from deltasleuth.context import init_workspace

    base = tmp_path.resolve()
    (base / "ds.yaml").write_text(FILES_SETTINGS.format(base=base))
    (base / "t.yaml").write_text(
        yaml.safe_dump({"targets": {"legacy": {"engine": "files", "root": str(base / "e")}}})
    )
    init_workspace(load_settings(base / "ds.yaml"))
    code, out = ds("runs", "--config", base / "ds.yaml")
    assert (
        code == 2
        and out.startswith("error: root of target legacy must lie under ")
        and "Traceback" not in out
    )


@pytest.mark.db
def test_target_check_reads_a_postgresql_target(tmp_path: Path, pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE accounts (id int PRIMARY KEY, opened timestamptz, span interval);"
            " CREATE VIEW accounts_v AS SELECT id FROM accounts"
        )
    (tmp_path / "target_dsn").write_text(pg_dsn)
    targets = {"db": {"dsn_ref": "file:target_dsn"}, "gone": {"dsn_ref": "file:gone_dsn"}}
    session, *table = checked(tmp_path, targets, "db", "--table", "accounts")
    assert re.fullmatch(r"target db: postgresql 16\.\d+, character set UTF8, TLS no, session ok", session)
    assert table == [
        "public.accounts (table, consistency snapshot)",
        "  id      integer                   integer  key",
        "  opened  timestamp with time zone  instant",
        "  span    interval                  other",
    ]
    assert checked(tmp_path, targets, "db", "--table", "public.accounts_v")[1:] == [
        "public.accounts_v (view, consistency snapshot)",
        "  id  integer  integer  key",
    ]
    (tmp_path / "gone_dsn").write_text("postgresql://nobody@127.0.0.1:9/nothing")
    code, out = ds("target", "check", "gone", "--config", tmp_path / "ds.yaml")
    assert code == 2 and out.startswith("error: target gone: cannot connect to target gone: ")


@pytest.mark.mysql
def test_target_check_reads_a_mysql_target(tmp_path: Path, mysql_server: MySQLServer) -> None:
    database = mysql_server.database("""
        CREATE TABLE accounts (id INT PRIMARY KEY, email VARCHAR(100), opened DATETIME(6), place GEOMETRY);
        CREATE VIEW accounts_v AS SELECT id FROM accounts;
        CREATE TABLE notes (id INT PRIMARY KEY) ENGINE = MyISAM;
    """)
    targets = {"db": mysql_server.target(database)}
    session, *table = checked(tmp_path, targets, "db", "--table", "accounts")
    # The stock image is the observed server: no note. It alone shows the patch level.
    assert re.fullmatch(r"target db: mysql 8\.4\.\d+, character set utf8mb4, TLS yes, session ok", session)
    assert table == [
        f"{database}.accounts (table, consistency snapshot)",
        "  id      int           integer    key",
        "  email   varchar(100)  text       key",
        "  opened  datetime(6)   timestamp",
        "  place   geometry      -          not comparable",
    ]
    assert checked(tmp_path, targets, "db", "--table", "ACCOUNTS_V")[1] == (
        f"{database}.accounts_v (view, consistency unverified)"
    )
    assert (
        checked(tmp_path, targets, "db", "--table", "notes")[1]
        == f"{database}.notes (table, consistency none)"
    )
    code, out = ds("target", "check", "db", "--table", "nope", "--config", tmp_path / "ds.yaml")
    assert (code, out) == (2, "error: target db: table or view nope not found or not visible\n")


@pytest.mark.oracle
def test_target_check_reads_an_oracle_target(tmp_path: Path, oracle_server: OracleServer) -> None:
    schema = oracle_server.schema("""
        CREATE TABLE accounts (id NUMBER(10) PRIMARY KEY, email VARCHAR2(120 CHAR), opened DATE, old LONG);
        CREATE VIEW accounts_v AS SELECT id FROM accounts
    """)
    targets = {"db": oracle_server.target(schema)}
    session, *table = checked(tmp_path, targets, "db", "--table", "accounts")
    version = r"23\.26\.\d+\.\d+\.\d+"
    assert re.fullmatch(rf"target db: oracle {version}, character set AL32UTF8, TLS no, session ok", session)
    assert table == [
        f"{schema}.ACCOUNTS (table, consistency snapshot)",
        "  ID      NUMBER(10)          integer    key",
        "  EMAIL   VARCHAR2(120 CHAR)  text       key",
        "  OPENED  DATE                timestamp  key",
        "  OLD     LONG                -          not comparable",
    ]
    assert checked(tmp_path, targets, "db", "--table", "accounts_v")[1] == (
        f"{schema}.ACCOUNTS_V (view, consistency snapshot)"
    )


def test_the_worker_copies_audit_rows_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_logger: logging.Logger
) -> None:
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: None)
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    assert ds("worker", "--config", tmp_path / "ds.yaml")[0] == 0
    assert [h.stream for h in audit_logger.handlers] == ["stdout"] and not audit_logger.propagate  # type: ignore[attr-defined]


def test_report_names_are_the_apis(tmp_path: Path) -> None:
    code, out = ds("report", "R-1", "--out", tmp_path, "--kind", "pdf")
    assert code == 2 and "unknown report pdf; one of report.pdf, discrepancies.csv" in out


def _wire_mocks() -> Any:
    name = "deltasleuth_llm_wire_mocks"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).parents[1] / "llm" / "wire_mocks.py"
        )
        assert spec is not None and spec.loader is not None
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


def test_llm_probe_reports_each_capability_without_the_key(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    mocks = _wire_mocks()
    mock = asyncio.run_coroutine_threadsafe(mocks.OpenAIMock(mocks.OpenAIVariant()).start(), loop).result(10)
    (tmp_path / "llm.key").write_text(KEY_VALUE)
    profile = {
        "dialect": "openai_chat", "base_url": mock.base_url, "model": "gpt-test",
        "auth_ref": f"file:{tmp_path / 'llm.key'}", "allow_insecure_localhost": True,
        "data_classes": ["synthetic"], "environments": ["dev"],
        "capabilities": {"tool_calling": True},
    }  # fmt: skip
    (tmp_path / "llm.yaml").write_text(
        yaml.safe_dump({"hosts": {"127.0.0.1": "internal"}, "profiles": {"probe-me": profile}})
    )
    settings = {
        "database_dsn_ref": "file:dsn", "evidence_dir": "e", "workspace_dir": "w", "targets_file": "t.yaml",
        "llm_profiles_file": "llm.yaml", "llm_profile": "probe-me", "llm_environment": "dev",
    }  # fmt: skip
    (tmp_path / "ds.yaml").write_text(yaml.safe_dump(settings))
    try:
        code, out = ds("llm-probe", "--out", tmp_path / "o", "--config", tmp_path / "ds.yaml")
        assert code == 0 and "tool_roundtrip=pass" in out and KEY_VALUE not in out, out
        assert (
            json.loads((tmp_path / "o" / "llm_probe_probe-me.json").read_text())["profile_id"] == "probe-me"
        )
        assert ds("llm-probe", "--profile", "nope", "--config", tmp_path / "ds.yaml")[0] == 2
        code, out = ds("llm-probe", "--environment", "moon", "--config", tmp_path / "ds.yaml")
        assert code == 2 and "environment" in out
    finally:
        asyncio.run_coroutine_threadsafe(mock.close(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)


# -- the host administrator's channel ------------------------------------------

YES = [  # the commands that write audit rows
    ("compare", "K1", "--spec", "s.yaml"),
    ("run", "s.yaml"),
    ("report", "R-1", "--out", "o"),
    ("measure", "R-1", "--out", "o"),
    ("adjudicate", "R-1", "--out", "o"),
    ("score", "--out", "o"),
    ("token", "create", "--name", "ci-bot", "--owner", "team", "--role", "viewer"),
    ("token", "revoke", "T-0000000000000000"),
    ("session", "revoke", "--all"),
    ("users", "disable", "U-0000000000000000"),
    ("users", "enable", "U-0000000000000000"),
    ("audit",),
    ("keys", "migrate"),
    ("purge",),
    ("purge", "--run", "R-1", "--reason", "a customer's request"),
    ("purge", "hold", "R-1", "--reason", "a legal hold"),
    ("purge", "release", "R-1"),
    ("approvals", "revoke", "A-00000000000000000000000000", "--reason", "wrong clear path"),
    ("licence", "install", "LI.txt"),
    ("licence", "withdraw", "LI-00000000000000000000000000", "--reason", "issued for the wrong order"),
    ("notify", "test", "ops-hook"),
    ("notify", "retry", "ops-hook"),
    ("notify", "mute", "--channel", "ops-hook", "--hours", "1", "--reason", "a planned outage"),
    ("notify", "unmute", "M-00000000000000000000000000"),
    ("readiness", "report", "--out", "o"),
    ("schedules", "add", "nightly", "--spec-id", "1", "--cron", "0 2 * * *", "--tz", "UTC"),
    ("schedules", "set", "nightly", "--cron", "0 3 * * *"),
    ("schedules", "pause", "nightly"),
    ("schedules", "pause", "--all"),
    ("schedules", "resume", "nightly"),
    ("schedules", "delete", "nightly"),
    ("precheck", "run", "--all-specs"),
    ("precheck", "run", "--a", "db-a:app", "--b", "db-b"),
    ("precheck", "cancel", "PC-00000000000000000000000000"),
    ("precheck", "report", "PC-00000000000000000000000000", "--out", "o"),
]


def said(result: Result) -> tuple[int, str]:
    """The exit code and stdout, or the error on stderr (where the SIEM lines also go)."""
    return result.exit_code, result.stdout if result.exit_code == 0 else result.stderr


def sql(dsn: str, query: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        cur = conn.execute(query.encode(), params)
        return cur.fetchall() if cur.description else []


def audited(dsn: str, action: str) -> list[dict[str, Any]]:
    return sql(dsn, "SELECT * FROM ds.audit_log WHERE action = %s ORDER BY id", action)


def add_user(dsn: str, username: str) -> str:
    user_id = "U-" + os.urandom(8).hex()
    sql(dsn, "INSERT INTO ds.users (user_id, issuer, subject, username, display_name, roles)"
             " VALUES (%s, 'https://idp.test', %s, %s, %s, '{viewer}')", user_id, username, username,
        username.title())  # fmt: skip
    return user_id


def add_session(dsn: str, user_id: str) -> str:
    session_id = "W-" + os.urandom(8).hex()
    sql(dsn, "INSERT INTO ds.sessions (session_id, secret_hash, user_id, actor, name, roles, roles_sha256,"
             " expires_at) VALUES (%s, %s, %s, %s, 'someone', '{viewer}', %s, now() + interval '1 hour')",
        session_id, os.urandom(32), user_id, f"user:{user_id}", "a" * 64)  # fmt: skip
    return session_id


def add_token(dsn: str, name: str, created_by: str, owner_user_id: str | None = None) -> str:
    token_id = "T-" + os.urandom(8).hex()
    sql(dsn, "INSERT INTO ds.api_tokens (token_id, name, owner, owner_user_id, role, secret_hash, created_by,"
             " expires_at) VALUES (%s, %s, 'team', %s, 'viewer', %s, %s, now() + interval '1 day')",
        token_id, name, owner_user_id, os.urandom(32), created_by)  # fmt: skip
    return token_id


@pytest.fixture
def store(tmp_path: Path, pg_dsn: str) -> Callable[..., Path]:
    """``store(auth=None, notify=None, readiness=None, mcp=None)``: a settings file over a fresh database,
    initialised and migrated."""

    def make(
        auth: MaybeAuth = None,
        notify: dict[str, Any] | None = None,
        readiness: dict[str, Any] | None = None,
        mcp: dict[str, Any] | None = None,
    ) -> Path:
        (tmp_path / "dsn").write_text(pg_dsn + "\n")
        (tmp_path / "t.yaml").write_text(TARGETS)
        doc: dict[str, Any] = {
            "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
            "evidence_dir": "e",
            "workspace_dir": "w",
            "targets_file": "t.yaml",
        }
        if auth is not None:
            doc |= {"auth": auth, "encryption": encryption(tmp_path / "secrets")}
        if notify is not None:
            doc["notify"] = notify
        if readiness is not None:
            doc["readiness"] = readiness
        if mcp is not None:
            doc["mcp"] = mcp
        (tmp_path / "ds.yaml").write_text(yaml.safe_dump(doc))
        for command in ("init", "migrate"):
            assert run(command, "--config", tmp_path / "ds.yaml").exit_code == 0
        return tmp_path / "ds.yaml"

    return make


@pytest.mark.parametrize("operator", [None, ""], ids=["unset", "empty"])
@pytest.mark.parametrize("command", YES, ids=" ".join)
def test_with_auth_each_command_that_audits_needs_ds_operator_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...], operator: str | None
) -> None:
    if operator is None:
        monkeypatch.delenv("DS_OPERATOR", raising=False)
    else:
        monkeypatch.setenv("DS_OPERATOR", operator)
    # The DSN file is absent: no work may start.
    auth = {"auth": {"public_url": "https://ds.example"}, "encryption": encryption(tmp_path)}
    (tmp_path / "ds.yaml").write_text(SETTINGS + yaml.safe_dump(auth))
    assert ds(*command, "--config", tmp_path / "ds.yaml") == (2, f"error: {OPERATOR}\n")


@pytest.mark.db
def test_reading_commands_need_neither_ds_operator_nor_the_client_credential(
    store: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    oidc = {
        "issuer": "https://idp.example",
        "client_id": "deltasleuth",
        "client_secret_ref": f"file:{tmp_path / 'absent'}",
        "roles": {"admin": ["DS.Admin"]},
    }
    config = store({"public_url": "https://ds.example", "oidc": oidc})
    reads = (("runs",), ("token", "list"), ("users",), ("users", "--json"), ("labellings",))
    assert [said(run(*read, "--config", config)) for read in reads] == [
        (0, ""),
        (0, ""),
        (0, ""),
        (0, "[]\n"),
        (0, ""),
    ]
    assert run("schedules", "--config", config).stdout.endswith("no schedules\n")
    assert run("precheck", "--config", config).stdout.endswith("no table pre-checks\n")


REFUSED = (
    "error: the database answered and asked for a password, then refused the connection: check the user,"
    " the password and the database name in the file database_dsn_ref names; if they are right, the server,"
    " the role or the database is at its connection limit, or a pooler cannot reach its server\n"
)
UNREADABLE = (
    "error: the file database_dsn_ref names does not hold an address the driver can read: percent-encode"
    " special characters in the user and the password\n"
)


@pytest.mark.db
def test_a_refused_database_is_one_line(
    store: Callable[..., Path], tmp_path: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong password is said after three tries (here a tenth of a second
    apart, not two), by every command that opens the store, where the pool's wait would end in
    "unreachable" 30 s later; ``ds keys migrate``, whose first connection is its lock's, among them."""
    monkeypatch.setattr(db, "APART", 0.1)
    config, began = store(), time.monotonic()
    (tmp_path / "dsn").write_text(pg_dsn.replace(":test@", ":Dsn-Passw0rd-Canary@") + "\n")
    commands = (("runs",), ("migrate",), ("token", "list"), ("keys", "migrate"))
    refused = [run(*command, "--config", config) for command in commands]
    assert [(r.exit_code, r.stdout, r.stderr) for r in refused] == [(2, "", REFUSED)] * 4
    assert time.monotonic() - began < db.WAIT / 2


def test_an_address_the_driver_cannot_read_is_one_line_without_a_piece_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The driver's own words quote such an address, the password included,
    and the pool logged them at each try of its 30 s. Every command ends at once instead, in one fixed
    line, before any connection: an unencoded %, a space, a %XX that is not UTF-8 (which read "a file is
    not valid utf-8 text"), and an unencoded @, whose tail the driver takes for the host."""

    async def never(*_: Any, **__: Any) -> Any:
        pytest.fail("no connection is tried")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", staticmethod(never))
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    (tmp_path / "t.yaml").write_text(TARGETS)
    config, commands = tmp_path / "ds.yaml", (("runs",), ("migrate",), ("keys", "migrate"))
    assert ds("init", "--config", config)[0] == 0
    caplog.set_level(logging.WARNING, logger="psycopg.pool")
    for password in ("Passw0rd%Zanary", "Passw0rd Canary", "Passw0rd%Canary", "Passw0rd@Canary"):
        (tmp_path / "dsn").write_text(f"postgresql://deltasleuth:{password}@127.0.0.1:1/deltasleuth\n")
        ended = [run(*command, "--config", config) for command in commands]
        assert [(r.exit_code, r.stdout, r.stderr) for r in ended] == [(2, "", UNREADABLE)] * 3, password
    assert [record for record in caplog.records if record.name.startswith("psycopg")] == []


def test_migrate_logs_one_pool_line_for_a_closed_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The pool logs every failed try of its wait, five in its 30 s; each
    wording reaches the terminal once, before the line that says what to check. As a rule that is one
    line: libpq words a refused connection in two ways, the second rarely."""
    monkeypatch.setattr(db, "WAIT", 2.5)  # the pool tries twice: at once, and about a second later
    connect, worded = psycopg.AsyncConnection.connect, []

    async def tried(*args: Any, **kwargs: Any) -> Any:
        try:
            return await connect(*args, **kwargs)
        except psycopg.Error as exc:
            worded.append(str(exc))
            raise

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", staticmethod(tried))
    with socket.create_server(("127.0.0.1", 0)) as closed:
        port = closed.getsockname()[1]
    (tmp_path / "ds.yaml").write_text(SETTINGS.replace("file:dsn", f"file:{tmp_path / 'dsn'}"))
    (tmp_path / "dsn").write_text(f"postgresql://deltasleuth:secret@127.0.0.1:{port}/deltasleuth\n")
    pool, records = logging.getLogger("psycopg.pool"), []
    seen = logging.Handler()  # on the driver's logger itself: whatever an earlier test left above it
    monkeypatch.setattr(seen, "emit", records.append)
    caplog.set_level(logging.WARNING, logger=pool.name)
    pool.addHandler(seen)
    try:
        migrated = run("migrate", "--config", tmp_path / "ds.yaml")
    finally:
        pool.removeHandler(seen)
    assert (migrated.exit_code, migrated.stdout, migrated.stderr) == (
        2, "", "error: the database is unreachable (PoolTimeout); check database_dsn_ref\n",
    )  # fmt: skip
    # three tries: admitted's and the pool's two
    assert len(worded) == 3 and all("Connection refused" in text for text in worded)
    lines = [record.getMessage().split(": ", 1)[1] for record in records if record.levelno >= logging.WARNING]
    assert lines == list(dict.fromkeys(worded[1:]))  # no line twice
    assert pool.filters == []  # the filter went with the wait


def test_the_documented_precheck_reads_all_three_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before a restart, ``ds licence`` opens what a start opens, so a mistake in
    the targets file or in the LLM profiles ends it in the line the start would log, before the database
    is asked (no DSN file exists here); ``ds init`` reads the settings file alone and passes."""
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: pytest.fail("the server must not start"))
    config = tmp_path / "ds.yaml"
    config.write_text(SETTINGS + "llm_profiles_file: llm.yaml\nllm_profile: gateway\n")
    profile = {"dialect": "openai_chat", "base_url": "https://llm.example.com/v1", "model": "m1",
               "auth_ref": "file:llm.key"}  # fmt: skip
    good = {"t.yaml": TARGETS, "llm.yaml": yaml.safe_dump({"profiles": {"gateway": profile}})}
    mistakes = {
        "t.yaml": (TARGETS.replace("targets:", "target:"), "error: /target [schema.additionalProperties]"),
        "llm.yaml": (good["llm.yaml"].replace("gateway", "gate-way"), "error: llm_profile 'gateway' is not"),
    }
    for name, (mistaken, begins) in mistakes.items():
        for other, text in good.items():
            (tmp_path / other).write_text(mistaken if other == name else text)
        prepared = run("init", "--config", config)
        checked, started = (run(command, "--config", config) for command in ("licence", "serve"))
        logged = [line for line in started.stderr.splitlines() if line.startswith("error:")]
        assert (prepared.exit_code, checked.exit_code, started.exit_code) == (0, 2, 2), name
        assert checked.stderr.splitlines() == logged and logged[0].startswith(begins), checked.stderr
        assert checked.stdout == ""


@pytest.mark.db
def test_without_auth_the_cli_acts_as_the_os_account_and_prints_only_the_token(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "ds host")
    config = store()
    made = run("token", "create", "--name", "ci-bot", "--owner", "payments team", "--role", "operator",
               "--config", config)  # fmt: skip
    [row] = audited(pg_dsn, "token.create")
    account = pwd.getpwuid(os.getuid()).pw_name
    assert made.exit_code == 0 and re.fullmatch(r"dst_[0-9a-f]{16}_[0-9a-f]{64}\n", made.stdout)
    assert (row["object"], row["actor"], row["actor_name"]) == (
        "T-" + made.stdout[4:20],
        f"local:{re.sub(r'[^A-Za-z0-9._-]', '_', account)}@ds_host",
        account,
    )
    assert re.fullmatch(r"C-[0-9a-f]{16}", row["request_id"]) and (row["session_id"], row["client_addr"]) == (
        None,
        None,
    )
    expires = row["detail"].pop("expires_at")
    assert row["detail"] == {"name": "ci-bot", "owner": "payments team", "owner_user_id": None,
                             "role": "operator", "asserted": True, "os_account": account}  # fmt: skip
    lines = [json.loads(line) for line in made.stderr.splitlines() if line.startswith("{")]
    [siem] = [line for line in lines if line["action"] != "keys.created"]  # the first start's keys
    assert (siem["id"], siem["action"], siem["detail"]["expires_at"]) == (row["id"], "token.create", expires)


@pytest.mark.db
def test_tokens_are_created_listed_with_names_and_revoked_once(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "J. Doe")
    monkeypatch.setattr("socket.gethostname", lambda: "ds-host")
    config = store({"public_url": "https://ds.example", "token_max_days": 30})
    bob = add_user(pg_dsn, "bob")

    def create(*options: str, role: str = "operator") -> Result:
        return run("token", "create", "--name", "ci-deploy", "--owner", "payments team", "--role", role,
                   *options, "--config", config)  # fmt: skip

    made = create("--owner-user", bob)
    token_id = "T-" + made.stdout[4:20]
    refused = [create("--days", "31"), create(role="admin"), create("--owner-user", "U-0000000000000000")]
    listed = run("token", "list", "--config", config)
    revoked = [
        run("token", "revoke", t, "--config", config) for t in (token_id, token_id, "T-0000000000000000")
    ]
    after = run("token", "list", "--config", config)
    [row] = audited(pg_dsn, "token.create")
    assert [said(r) for r in refused] == [
        (2, "error: days must be from 1 to 30\n"),
        (2, "error: role must be one of operator, spec_author, viewer\n"),
        (2, "error: owner_user_id U-0000000000000000 is not an enabled user\n"),
    ]
    [line] = listed.stdout.splitlines()
    assert line.startswith(f"{token_id} (ci-deploy)  operator  active  expires ")
    assert line.startswith(f"{token_id} ") and "  active  " in line  # what deploy/up.sh reads
    assert line.endswith(f"  owner 'payments team' {bob} (bob)  created by local:J._Doe@ds-host  last used -")
    assert [said(r) for r in revoked] == [
        (0, f"revoked token {token_id} (ci-deploy)\n"),
        (2, f"error: token {token_id} is already revoked\n"),
        (2, "error: token T-0000000000000000 not found\n"),
    ]
    assert f"{token_id} (ci-deploy)  operator  revoked  " in after.stdout
    assert (row["actor"], row["actor_name"], row["detail"]["owner_user_id"]) == (
        "local:J._Doe@ds-host",
        "J. Doe",
        bob,
    )
    assert [r["object"] for r in audited(pg_dsn, "token.revoke")] == [token_id]


@pytest.mark.db
def test_sessions_are_revoked_one_by_user_or_all(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store({"public_url": "https://ds.example"})
    alice, bob = add_user(pg_dsn, "alice"), add_user(pg_dsn, "bob")
    one = add_session(pg_dsn, alice)
    for user_id in (alice, bob, bob):
        add_session(pg_dsn, user_id)

    def revoke(*how: str) -> tuple[int, str]:
        return said(run("session", "revoke", *how, "--config", config))

    usage = [revoke(), revoke("--all", "--user", alice)]
    revoked = [
        revoke("--session", one),
        revoke("--session", one),
        revoke("--user", alice),
        revoke("--user", "U-0000000000000000"),
        revoke("--all"),
    ]
    assert usage == [(2, "error: name one of --session W-…, --user U-… or --all\n")] * 2
    assert revoked == [
        (0, "revoked 1 session(s)\n"),
        (2, f"error: session {one} not found\n"),
        (0, "revoked 1 session(s)\n"),
        (2, "error: user U-0000000000000000 not found\n"),
        (0, "revoked 2 session(s)\n"),
    ]
    rows = audited(pg_dsn, "session.revoke")
    assert [(r["object"], r["detail"]["count"]) for r in rows] == [(one, 1), (alice, 1), ("all", 2)]


@pytest.mark.db
def test_users_are_listed_with_names_disabled_and_enabled(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    monkeypatch.setattr("socket.gethostname", lambda: "ds-host")
    config = store({"public_url": "https://ds.example"})
    alice, bob = add_user(pg_dsn, "alice"), add_user(pg_dsn, "bob")
    add_session(pg_dsn, bob)
    made, owned = add_token(pg_dsn, "bob-made", f"user:{bob}"), add_token(pg_dsn, "bob-owned", "ds", bob)

    def users(*args: str) -> Result:
        return run("users", *args, "--config", config)

    first, again, listed = users("disable", bob), users("disable", bob), users()
    enabled, enabled_again = users("enable", bob), users("enable", bob)
    second, as_json, unknown = (
        users("disable", bob, "--revoke-tokens"),
        users("--json"),
        users("enable", "U-0"),
    )
    misplaced = run("users", "--config", config, "disable", bob)
    assert first.stdout.splitlines()[0] == f"disabled {bob} (bob): 1 session(s) and 0 token(s) revoked"
    assert set(first.stdout.splitlines()[1:]) == {
        f"  token {made} (bob-made)  viewer  active",
        f"  token {owned} (bob-owned)  viewer  active",
    }
    assert said(again) == (2, f"error: user {bob} is already disabled\n")
    [alice_line, bob_line] = listed.stdout.splitlines()
    assert alice_line == f"{alice} (alice)  Alice  viewer  last sign-in -"
    assert re.fullmatch(
        rf"{bob} \(bob\)  Bob  viewer  last sign-in -  disabled \S+ by local:tester@ds-host", bob_line
    )
    assert [said(enabled), said(enabled_again)] == [
        (0, f"enabled {bob} (bob); revoked tokens stay revoked\n"),
        (2, f"error: user {bob} is not disabled\n"),
    ]
    assert second.stdout.splitlines()[0] == f"disabled {bob} (bob): 0 session(s) and 2 token(s) revoked"
    assert all(line.endswith("  revoked") for line in second.stdout.splitlines()[1:])
    assert [(u["username"], u["disabled_by"]) for u in json.loads(as_json.stdout)] == [
        ("alice", None),
        ("bob", "local:tester@ds-host"),
    ]
    assert said(unknown) == (2, "error: user U-0 not found\n")
    assert said(misplaced) == (2, "error: put the options after 'disable'\n")
    disables = [(r["object"], r["detail"]["sessions_revoked"], r["detail"]["tokens_revoked"])
                for r in audited(pg_dsn, "user.disable")]  # fmt: skip
    assert disables == [(bob, 1, []), (bob, 0, sorted([made, owned]))]
    assert [r["object"] for r in audited(pg_dsn, "user.enable")] == [bob]


@pytest.mark.db
def test_the_audit_log_is_read_with_filters_names_beside_actors_and_the_read_is_audited(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    monkeypatch.setattr("socket.gethostname", lambda: "ds-host")
    config = store({"public_url": "https://ds.example"})
    for name in ("ci-one", "ci-two"):
        assert run("token", "create", "--name", name, "--owner", "team", "--role", "viewer",
                   "--config", config).exit_code == 0  # fmt: skip
    text = run("audit", "--action", "token.create", "--config", config)
    as_json = run("audit", "--action", "token.create", "--limit", "1", "--json", "--config", config)
    later = run("audit", "--since", "2999-01-01T00:00:00", "--config", config)
    bad = run("audit", "--since", "yesterday", "--config", config)
    lines = text.stdout.splitlines()
    assert [re.search(r'"name": "(ci-\w+)"', line)[1] for line in lines] == ["ci-two", "ci-one"]  # type: ignore[index]
    row = (
        r'\d+  \S+  local:tester@ds-host \(tester\)  token\.create  T-[0-9a-f]{16}  \{.*"asserted": true.*\}'
    )
    assert all(re.fullmatch(row + r"  request C-[0-9a-f]{16}", line) for line in lines)
    [item] = json.loads(as_json.stdout)
    assert (item["actor"], item["actor_name"], item["detail"]["name"]) == (
        "local:tester@ds-host",
        "tester",
        "ci-two",
    )
    assert said(later) == (0, "")
    assert said(bad) == (2, "error: --since 'yesterday' is not an ISO 8601 time, e.g. 2026-09-26T08:00:00Z\n")
    assert [r["detail"]["filters"] for r in audited(pg_dsn, "audit.read")] == [
        {"action": "token.create"},
        {"action": "token.create"},
        {"since": "2999-01-01T00:00:00+00:00"},
    ]
    assert run("audit", "--limit", "501", "--config", config).exit_code == 2


# -- retention and keys ----------------------------------------------------

CLI_SPEC = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: cli-retention, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key: {name: id, type: string, pattern: "^K[0-9]+$", source: {kind: list, keys: [K1]}}
sides:
  a: {role: old, target: svc, request: {method: GET, path: "/a/{{ key }}"}}
  b: {role: new, target: svc, request: {method: GET, path: "/b/{{ key }}"}}
compare: {}
recheck: {delay: 1s, times: 1}
"""
LOST = "K-" + "0" * 16


async def _stored(
    config: Path,
    status: str,
    *,
    mode: str = "batch",
    ended: float | None = None,
    cycle: int = 1,
    samples_dir: str | None = None,
    body: bytes | None = None,
    text: str = CLI_SPEC,
) -> str:
    """A run of the spec ``text`` as the runner leaves it, of one key K1: ``ended`` days ago its first cycle
    ended, and the run is at ``cycle``; ``body``: its key has an observation of that body, in the evidence."""
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from deltasleuth.core.spec import compile_spec
    from deltasleuth.core.spec.documents import parse_yaml_text
    from deltasleuth.core.types import Observation, Side
    from deltasleuth.store import repo
    from tests.conftest import TESTER, create_run

    async with Context.open(load_settings(config)) as ctx, ctx.pool.connection() as conn:
        spec_id = await repo.save_spec(conn, compile_spec(parse_yaml_text(text)), created_by=TESTER.actor)
        run_id, sealer = await create_run(
            ctx, conn, spec_id, mode, {"K1": "K1"}, {"samples_dir": samples_dir}
        )
        if status != "queued":
            await repo.set_run_status(conn, run_id, status)
        if ended is not None:
            await repo.end_cycle(conn, run_id)
            await conn.execute(
                "UPDATE ds.cycles SET ended_at = now() - %s * interval '1 day' WHERE run_id = %s",
                (ended, run_id),
            )
        await conn.execute("UPDATE ds.runs SET cycle = %s WHERE run_id = %s", (cycle, run_id))
        if body is not None:
            obs = ctx.evidence.put_blob(Observation("K1", Side.A, 200, body, "2026-09-01T00:00:00.000000Z"))
            headers = sealer.seal("headers", 1, obs.obs_id, b"{}")  # type: ignore[arg-type]
            await repo.insert_observation(conn, run_id, 1, 1, "K1", obs, headers)
    return run_id


def stored(config: Path, status: str, **options: Any) -> str:
    return asyncio.run(_stored(config, status, **options))


def held_by(dsn: str, run_id: str, by: str, reason: str) -> None:
    sql(
        dsn,
        "UPDATE ds.runs SET hold_reason = %s, hold_by = %s, hold_at = now() WHERE run_id = %s",
        reason,
        by,
        run_id,
    )


def leased(dsn: str, run_id: str) -> None:
    """A worker holds the run's keys: a live lease."""
    sql(
        dsn,
        """UPDATE ds.run_keys SET state = 'leased', lease_owner = 'w',
               lease_until = now() + interval '1 minute' WHERE run_id = %s""",
        run_id,
    )


def day(dsn: str, query: str, *params: Any) -> str:
    [row] = sql(dsn, query, *params)
    return f"{next(iter(row.values())):%Y-%m-%d}"


@pytest.mark.db
def test_a_dry_run_names_what_a_pass_would_do_writes_nothing_and_needs_no_operator(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    config = store({"public_url": "https://ds.example"})
    assert said(run("purge", "--dry-run", "--config", config)) == (0, "nothing to purge\n")
    due = stored(config, "completed", ended=100, samples_dir="/p0/samples-1")
    held = stored(config, "completed", ended=95)
    held_by(pg_dsn, held, "local:ann@host", "legal hold LH-7")
    soak = stored(config, "running", mode="soak", ended=91, cycle=2)
    one_shot = stored(config, "queued", mode="one_shot")
    sql(pg_dsn, "UPDATE ds.runs SET updated_at = now() - interval '25 hours' WHERE run_id = %s", one_shot)
    rows_before = sql(pg_dsn, "SELECT count(*) AS n FROM ds.audit_log")
    expired = "SELECT ended_at + interval '90 days' FROM ds.cycles WHERE run_id = %s"
    since = day(pg_dsn, "SELECT updated_at FROM ds.runs WHERE run_id = %s", one_shot)
    hold_at = day(pg_dsn, "SELECT hold_at FROM ds.runs WHERE run_id = %s", held)
    assert said(run("purge", "--dry-run", "--config", config)) == (0, (
        f"due: cycle 1 of run {due} (retention standard): expired {day(pg_dsn, expired, due)}\n"
        f"held: cycle 1 of run {held} (retention standard): expired {day(pg_dsn, expired, held)}; held by"
        f" local:ann@host since {hold_at}:"
        " 'legal hold LH-7'\n"
        f"kept: cycle 1 of run {soak} (retention standard): expired {day(pg_dsn, expired, soak)}; its running"
        " soak reads it until its next cycle ends\n"
        f"abandon: run {one_shot} (queued since {since}): failed, a one-shot run queued for 24 h\n"
        f"outside retention: /p0/samples-1 (run {due}): P0 samples, for the site's cron\n"
    ))  # fmt: skip
    assert sql(pg_dsn, "SELECT count(*) AS n FROM ds.audit_log") == rows_before
    assert sql(pg_dsn, "SELECT count(*) AS n FROM ds.cycles WHERE purging_at IS NOT NULL") == [{"n": 0}]
    assert run("purge", "--config", config).exit_code == 2  # the pass itself needs DS_OPERATOR


@pytest.mark.db
def test_a_pass_purges_what_is_due_and_says_what_it_did(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    monkeypatch.setattr("socket.gethostname", lambda: "ds-host")
    config = store()
    first, second = (stored(config, "completed", ended=days) for days in (120, 100))
    held = stored(config, "completed", ended=100)
    held_by(pg_dsn, held, "local:x@y", "hold")
    one_shot = stored(config, "queued", mode="one_shot")
    sql(pg_dsn, "UPDATE ds.runs SET updated_at = now() - interval '25 hours' WHERE run_id = %s", one_shot)
    assert said(run("purge", "--limit", "1", "--config", config)) == (0, (
        "purged 1 cycle(s) of 1 run(s): 1 key(s); 0 blob(s) queued; 0 deleted (0 B), 0 kept (shared)\n"
        "abandoned 1 run(s) past their retention\n"
        "held: 1 cycle(s) past their retention\n"
    ))  # fmt: skip
    assert [r["run_id"] for r in sql(pg_dsn, "SELECT run_id FROM ds.cycles WHERE purged_at IS NOT NULL")] == [
        first
    ]
    orphan = tmp_path / "e" / "blobs" / "hk1" / "ab" / ".abandoned.dse.x.tmp"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"half")
    os.utime(orphan, (0, 0))
    assert said(run("purge", "--orphans", "--config", config)) == (0, (
        "purged 1 cycle(s) of 1 run(s): 1 key(s); 0 blob(s) queued; 0 deleted (0 B), 0 kept (shared)\n"
        "held: 1 cycle(s) past their retention\n"
        "deleted 1 orphaned file(s)\n"
    ))  # fmt: skip
    assert not orphan.exists()
    retention = sql(pg_dsn, "SELECT action, object, actor FROM ds.audit_log WHERE action LIKE 'retention.%%'")
    actions = {(r["action"], r["object"]) for r in retention}
    assert actions == {("retention.expire", first), ("retention.purged", first), ("retention.expire", second),
                       ("retention.purged", second), ("retention.abandoned", one_shot),
                       ("retention.finished", "purge")}  # fmt: skip
    assert {r["actor"] for r in retention} == {"local:tester@ds-host"}


@pytest.mark.db
def test_the_reports_of_a_purged_cycle_are_refused_in_one_line(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store()
    run_id = stored(config, "completed", ended=100)
    assert run("purge", "--config", config).exit_code == 0
    purged_on = day(pg_dsn, "SELECT purging_at FROM ds.cycles WHERE run_id = %s", run_id)
    gone = (
        f"error: cycle 1 of run {run_id} was purged on {purged_on} (retention standard): its keys, results,"
        " findings and evidence are deleted\n"
    )
    for command in (("report", run_id), ("report", run_id, "--kind", "summary"), ("measure", run_id)):
        assert said(run(*command, "--out", tmp_path / "out", "--config", config)) == (2, gone), command
    assert audited(pg_dsn, "report.download") == [] and not list((tmp_path / "out").glob("*"))


@pytest.mark.db
def test_an_erasure_is_refused_in_one_line_until_it_can_run(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store()
    done = stored(config, "completed", ended=1)
    held = stored(config, "failed", ended=1)
    held_by(pg_dsn, held, "local:x@y", "hold")
    soak = stored(config, "running", mode="soak", ended=1, cycle=2)
    busy = stored(config, "failed", ended=1)
    leased(pg_dsn, busy)

    def erase(*options: Any) -> tuple[int, str]:
        return said(run("purge", *options, "--config", config))

    assert erase("--run", done) == (2, "error: --run needs --reason (an erasure is audited)\n")
    assert erase("--run", done, "--reason", "x", "--dry-run") == (
        2, "error: --run erases one run: it takes no --dry-run, --orphans or --limit\n",
    )  # fmt: skip
    assert erase("--cycle", "1") == (2, "error: --cycle and --reason go with --run R-…\n")
    assert erase("--run", done, "--reason", "bell\x07") == (
        2, "error: --reason must be 1 to 200 characters without control characters\n",
    )  # fmt: skip
    assert erase("--run", done, "--reason", "x" * 201)[0] == 2
    assert erase("--run", held, "--reason", "DSAR-1") == (2, f"error: run {held} is held\n")
    assert erase("--run", soak, "--cycle", "1", "--reason", "DSAR-1") == (
        2, f"error: cycle 1 of run {soak} is still read by the running soak: erase the whole run\n",
    )  # fmt: skip
    assert erase("--run", soak, "--cycle", "2", "--reason", "DSAR-1") == (
        2, f"error: cycle 2 of run {soak} has not ended\n",
    )  # fmt: skip
    assert erase("--run", busy, "--reason", "DSAR-1") == (
        2, f"error: run {busy} still has keys in flight; try again in 60 s\n",
    )  # fmt: skip
    assert erase("--run", "R-0", "--reason", "DSAR-1") == (2, "error: run R-0 not found\n")
    with psycopg.connect(pg_dsn, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (0x44535047,))
        assert erase("--run", done, "--reason", "DSAR-1") == (
            2,
            "error: another purge is running; try again later\n",
        )
        assert erase() == (2, "error: another purge is running; try again later\n")
    assert audited(pg_dsn, "retention.expire") == []  # no refusal wrote a row
    assert erase("--run", done, "--cycle", "1", "--reason", "DSAR-1") == (
        0, "purged 1 cycle(s) of 1 run(s): 1 key(s); 0 blob(s) queued; 0 deleted (0 B), 0 kept (shared)\n",
    )  # fmt: skip
    purged_on = day(pg_dsn, "SELECT purging_at FROM ds.cycles WHERE run_id = %s", done)
    assert erase("--run", done, "--cycle", "1", "--reason", "DSAR-1") == (
        2, f"error: cycle 1 of run {done} was purged on {purged_on}\n",
    )  # fmt: skip
    [expire] = audited(pg_dsn, "retention.expire")
    assert (expire["object"], expire["detail"]["reason"]) == (done, "erasure: DSAR-1")


@pytest.mark.db
def test_a_hold_reason_is_read_with_the_spec_s_own_key_pattern(
    store: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store()
    keyed = CLI_SPEC.replace('pattern: "^K[0-9]+$"', 'pattern: "(?i)^k[0-9]+$", class: identifier')
    run_id = stored(config, "completed", ended=100, text=keyed)  # a leading inline flag: exit 2, never 3
    assert said(run("purge", "hold", run_id, "--reason", "customer k1 asked", "--config", config)) == (
        2, "error: the reason holds a key-shaped value: write it without customer keys\n",
    )  # fmt: skip
    assert said(run("purge", "hold", run_id, "--reason", "legal hold LH-7", "--config", config)) == (
        0, f"held run {run_id}: its evidence stays until 'ds purge release {run_id}'\n",
    )  # fmt: skip


@pytest.mark.db
def test_a_hold_keeps_a_run_until_released(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store()
    run_id = stored(config, "completed", ended=100)
    assert said(run("purge", "hold", run_id, "--reason", "legal hold LH-7", "--config", config)) == (
        0, f"held run {run_id}: its evidence stays until 'ds purge release {run_id}'\n",
    )  # fmt: skip
    assert said(run("purge", "hold", run_id, "--reason", "again", "--config", config)) == (
        2, f"error: run {run_id} is already held\n",
    )  # fmt: skip
    assert run("purge", "--config", config).stdout.startswith("purged 0 cycle(s)")
    assert said(run("purge", "release", run_id, "--config", config)) == (
        0, f"released run {run_id}: its cycles past their retention go at the next pass\n",
    )  # fmt: skip
    assert said(run("purge", "release", run_id, "--config", config)) == (
        2,
        f"error: run {run_id} is not held\n",
    )
    assert said(run("purge", "--config", config, "hold", run_id, "--reason", "x")) == (
        2, "error: put the options after 'hold'\n",
    )  # fmt: skip
    assert said(run("purge", "hold", "R-0", "--reason", "x", "--config", config)) == (
        2,
        "error: run R-0 not found\n",
    )
    holds = sql(
        pg_dsn, "SELECT action, detail FROM ds.audit_log WHERE action LIKE 'retention.%%' ORDER BY id"
    )
    trail = [(r["action"], r["detail"]["reason"]) for r in holds]
    assert trail == [("retention.hold", "legal hold LH-7"), ("retention.release", "legal hold LH-7")]
    assert run("purge", "--config", config).stdout.startswith("purged 1 cycle(s)")


@pytest.mark.db
def test_keys_status_lists_each_kek_s_data_keys_and_fails_on_a_missing_one(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.security.crypto import Keyring

    monkeypatch.delenv("DS_OPERATOR", raising=False)
    config = store({"public_url": "https://ds.example"})  # a reading command: no DS_OPERATOR
    run_id = stored(config, "completed", body=b'{"id": "K1"}')
    kek = Keyring.from_file(f"file:{config.parent / 'secrets' / 'kek'}").current
    assert said(run("keys", "status", "--config", config)) == (0, f"{kek}: 3 data key(s) (current)\n")
    verified = run("keys", "status", "--verify", "--config", config)
    assert said(verified) == (0, (
        f"{kek}: 3 data key(s) (current)\n"
        "blobs: 1 checked: 1 readable, 0 missing, 0 corrupted, 0 unreadable (a KEK is missing)\n"
    ))  # fmt: skip
    as_json = json.loads(run("keys", "status", "--json", "--verify", "--config", config).stdout)
    read = {"checked": 1, "readable": 1, "missing": 0, "corrupted": 0, "key_missing": 0}
    assert as_json == {"current": kek, "keks": {kek: 3}, "missing": {}, "blobs": read}
    [path] = (config.parent / "e" / "blobs" / "hk1").rglob("*.dse")
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))  # corrupted: the restore check fails
    corrupted = run("keys", "status", "--verify", "--config", config)
    assert (corrupted.exit_code, corrupted.stderr.splitlines()[-1]) == (2, "error: 1 blob(s) do not decrypt")
    assert "blobs: 1 checked: 0 readable, 0 missing, 1 corrupted" in corrupted.stdout
    path.unlink()  # lost (a missing blob answers 410, as a purged one): counted, not failed
    lost_blob = run("keys", "status", "--verify", "--config", config)
    assert (lost_blob.exit_code, lost_blob.stderr) == (0, "")
    assert "blobs: 1 checked: 0 readable, 1 missing, 0 corrupted" in lost_blob.stdout
    sql(pg_dsn, "UPDATE ds.data_keys SET kek = %s WHERE scope = %s", LOST, run_id)
    missing = run("keys", "status", "--config", config)
    assert (missing.exit_code, missing.stdout, missing.stderr.splitlines()[-1]) == (
        2,
        f"{kek}: 2 data key(s) (current)\nmissing {LOST}: 1 data key(s); restore the line from the vault and"
        " restart\n",
        "error: the keyring lacks 1 KEK(s) in use",
    )
    assert json.loads(run("keys", "status", "--json", "--config", config).stdout)["missing"] == {LOST: 1}


def test_keys_fingerprint_prints_a_keyring_file_s_ids(tmp_path: Path) -> None:
    from deltasleuth.security.crypto import key_id

    keks = [os.urandom(32), os.urandom(32)]
    (tmp_path / "vault-copy").write_text("# 2025\n" + keks[0].hex() + "\n\n" + keks[1].hex() + "\n")
    assert ds("keys", "fingerprint", tmp_path / "vault-copy") == (
        0, f"{key_id('K-', keks[0])}\n{key_id('K-', keks[1])} (current)\n",
    )  # fmt: skip
    (tmp_path / "broken").write_text("not hex\n")
    assert ds("keys", "fingerprint", tmp_path / "broken") == (
        2, f"error: keyring {tmp_path / 'broken'} line 1: expected 64 hex\n",
    )  # fmt: skip
    assert "not hex" not in ds("keys", "fingerprint", tmp_path / "broken")[1]
    (tmp_path / "restored").write_text(f"{keks[0].hex()}\n{keks[1].hex()}\n{keks[0].hex()}\n")
    assert ds("keys", "fingerprint", tmp_path / "restored") == (
        2, f"error: keyring {tmp_path / 'restored'} line 3: repeats line 1\n",
    )  # fmt: skip


@pytest.fixture
def idp() -> Iterator[FakeIdp]:
    with FakeIdp() as server:
        yield server


def oidc_settings(tmp_path: Path, idp: FakeIdp) -> Path:
    """Settings of the IdP's relying party at https://ds.test; no database is needed."""
    (tmp_path / "oidc_secret").write_text(CLIENT_SECRET + "\n")
    oidc = idp.settings(client_secret_ref=f"file:{tmp_path / 'oidc_secret'}")
    auth = {"auth": {"public_url": "https://ds.test", "oidc": oidc}, "encryption": encryption(tmp_path)}
    (tmp_path / "ds.yaml").write_text(SETTINGS + yaml.safe_dump(auth))
    return tmp_path / "ds.yaml"


def test_auth_check_prints_the_keys_the_clock_and_the_uris_to_register(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    checked = run("auth", "check", "--config", oidc_settings(tmp_path, idp))
    rsa, ec = (idp.keys[alg][0] for alg in ("RS256", "ES256"))
    lines = checked.stdout.splitlines()
    assert (checked.exit_code, checked.stderr) == (0, ""), checked.output
    assert re.fullmatch(r"clock offset: (\+0|-1|\+1) s \(the IdP's clock minus this host's\)", lines.pop(4))
    assert lines == [
        "client credential: ok (client_secret_basic)",
        f"discovery: ok ({idp.issuer})",
        f"signing key '{rsa}': RS256",  # the default id_token_algs
        f"signing key '{ec}': none of auth.oidc.id_token_algs (kty 'EC', alg 'ES256')",
        "redirect URI to register: https://ds.test/v1/auth/callback",
        "post-logout redirect URI to register: https://ds.test/",
    ]
    assert [seen.path for seen in idp.requests] == ["/.well-known/openid-configuration", "/jwks"]


def test_auth_check_warns_of_a_clock_beyond_the_allowed_skew(tmp_path: Path, idp: FakeIdp) -> None:
    idp.date_offset = 120
    checked = run("auth", "check", "--config", oidc_settings(tmp_path, idp))
    assert checked.exit_code == 0 and re.search(r"clock offset: \+1(19|20|21) s ", checked.stdout)
    warning = r"warning: the clocks differ by 1(19|20|21) s, over auth\.oidc\.clock_skew_seconds \(60\): "
    assert re.fullmatch(warning + r"sign-ins may fail until they agree\n", checked.stderr)


@pytest.mark.parametrize(
    ("defect", "error"),
    [
        ("issuer", "discovery issuer 'https://evil.example' differs from auth.oidc.issuer {issuer}"),
        (
            "token endpoint",
            "discovery token_endpoint 'https://idp.evil/token' is not at the issuer's scheme, host and port",
        ),
        ("down", "discovery unreachable ("),
        ("no key", "jwks: no key fits auth.oidc.id_token_algs RS256"),
        ("no secret", "cannot read secret file {tmp}/oidc_secret: "),
    ],
)
def test_auth_check_exits_2_naming_the_failing_check(
    tmp_path: Path, idp: FakeIdp, defect: str, error: str
) -> None:
    config = oidc_settings(tmp_path, idp)
    if defect == "issuer":
        idp.discovery["issuer"] = "https://evil.example"
    elif defect == "token endpoint":
        idp.discovery["token_endpoint"] = "https://idp.evil/token"  # noqa: S105 - a URL
    elif defect == "down":
        idp.down = True
    elif defect == "no key":
        idp.keys = {}
    else:
        (tmp_path / "oidc_secret").unlink()
    checked = run("auth", "check", "--config", config)
    assert checked.exit_code == 2
    assert checked.stderr.startswith("error: " + error.format(issuer=idp.issuer, tmp=tmp_path)), (
        checked.stderr
    )
    assert checked.stderr.count("\n") == 1 and "Traceback" not in checked.output


def test_auth_check_needs_the_oidc_settings(tmp_path: Path) -> None:
    auth = {"auth": {"public_url": "https://ds.example"}, "encryption": encryption(tmp_path)}
    for mode, block in (("tokens", yaml.safe_dump(auth)), ("developer", "")):
        (tmp_path / "ds.yaml").write_text(SETTINGS + block)
        assert ds("auth", "check", "--config", tmp_path / "ds.yaml") == (
            2,
            f"error: ds auth check needs the auth.oidc settings: these settings give {mode} mode\n",
        )


# -- four-eyes approvals ------------------------------------------

APPROVED = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: cli-approvals, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key: {name: id, type: string, pattern: "^K[0-9]+$", source: {kind: list, keys: [K1]}}
sides:
  a: &side
    role: old
    target: svc
    request: {method: GET, path: "/a/{{ key }}"}
    truncation: {countPath: $.n, cap: 9}
  b: {<<: *side, role: new, request: {method: GET, path: "/b/{{ key }}"}}
compare: {}
recheck: {delay: 1s, times: 1}
security: {clear: [$.x]}
"""  # its clear path needs an approval under SSO
NO_UPLOAD = (
    "spec cli-approvals 1.0.0 has no approved upload: upload it on the Specs screen, signed in, and have it"
    " approved (a CLI or token upload needs two approvers)"
)
NO_APPROVERS = "warning: no IdP group maps to rule_approver or above: nothing can be approved"


def sso(tmp_path: Path) -> dict[str, Any]:
    """The auth settings of an SSO deployment; no command here reads the client credential."""
    oidc = {
        "issuer": "https://idp.example",
        "client_id": "deltasleuth",
        "client_secret_ref": f"file:{tmp_path / 'absent'}",
        "roles": {"rule_approver": ["DS.Approver"]},
    }
    return {"public_url": "https://ds.example", "oidc": oidc}


def errors(result: Result) -> list[str]:
    return [line for line in result.stderr.splitlines() if line.startswith("error:")]


async def _enterprise(config: Path) -> None:
    """An Enterprise licence installed on the store, as an SSO deployment of a vendor build runs; none in an
    open build, which needs none."""
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from tests.conftest import licensed_once

    async with Context.open(load_settings(config)) as ctx:
        await licensed_once(ctx)


async def _upload(config: Path, text: str) -> int:
    """The spec ``text`` uploaded by bob, signed in, as ``POST /v1/specs`` stores it."""
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from deltasleuth.core.spec import compile_spec
    from deltasleuth.core.spec.documents import parse_yaml_text
    from tests.unit.store.four_eyes import person, upload

    async with Context.open(load_settings(config)) as ctx, ctx.pool.connection() as conn:
        bob = await person(conn, "bob", "spec_author")
        return await upload(conn, compile_spec(parse_yaml_text(text), targets=ctx.targets), bob)


async def _approve(config: Path, spec_id: int) -> str:
    """Carol's approval of bob's upload, which he requested: his own upload needs one approver."""
    from deltasleuth import approvals
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from tests.unit.store.four_eyes import approved, person

    async with Context.open(load_settings(config)) as ctx, ctx.pool.connection() as conn:
        bob, carol = await person(conn, "bob", "spec_author"), await person(conn, "carol")
        return await approved(
            conn, spec_id, bob, carol, scopes=approvals.needs(await ctx.spec(conn, spec_id))
        )


@pytest.mark.db
def test_under_sso_ds_run_and_compare_run_only_an_approved_upload_and_write_nothing_when_refused(
    store: Callable[..., Path], tmp_path: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store(sso(tmp_path))
    asyncio.run(_enterprise(config))
    spec = tmp_path / "s.yaml"
    spec.write_text(APPROVED)

    def ds_run() -> Result:
        return run("run", spec, "--no-wait", "--config", config)

    def written() -> dict[str, Any]:
        [counts] = sql(
            pg_dsn,
            """SELECT (SELECT count(*) FROM ds.specs) AS specs, (SELECT count(*) FROM ds.runs) AS runs,
                      (SELECT count(*) FROM ds.audit_log WHERE action = 'spec.create') AS uploads""",
        )
        return counts

    never = [ds_run(), run("compare", "K1", "--spec", spec, "--config", config)]
    assert [(r.exit_code, errors(r)) for r in never] == [(2, [f"error: {NO_UPLOAD}"])] * 2
    assert written() == {"specs": 0, "runs": 0, "uploads": 0}
    spec_id = asyncio.run(_upload(config, APPROVED))
    unapproved = ds_run()
    needs = f"spec {spec_id} (cli-approvals 1.0.0) needs a four-eyes approval of clear before it runs"
    assert (unapproved.exit_code, errors(unapproved)) == (2, [f"error: {needs}"])
    assert written() == {"specs": 1, "runs": 0, "uploads": 1}  # bob's upload only
    approval_id = asyncio.run(_approve(config, spec_id))
    started = ds_run()
    [row] = sql(pg_dsn, "SELECT run_id, spec_id, approval_id FROM ds.runs")
    assert said(started) == (0, f"run {row['run_id']} started\nprocessed by 'ds worker' or 'ds serve'\n")
    assert (row["spec_id"], row["approval_id"]) == (spec_id, approval_id)
    assert written() == {"specs": 1, "runs": 1, "uploads": 1}  # the stored version is not saved again


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_run_over_limit_exit_2(store: Callable[..., Path], tmp_path: Path, pg_dsn: str) -> None:
    config = store()  # developer mode, unlicensed: an evaluation runs two pairs
    for name in ("accounts", "loans", "cards"):
        (tmp_path / f"{name}.yaml").write_text(APPROVED.replace("name: cli-approvals", f"name: {name}"))
    started = [
        run("run", tmp_path / f"{name}.yaml", "--no-wait", "--config", config)
        for name in ("accounts", "loans")
    ]
    refused = [
        run("run", tmp_path / "cards.yaml", "--no-wait", "--config", config),
        run("compare", "K1", "--spec", tmp_path / "cards.yaml", "--config", config),
    ]
    [row] = sql(pg_dsn, "SELECT installation_id FROM ds.installation")
    m2 = (
        "spec cards would be a third pair: an unlicensed evaluation runs 2 pairs (accounts, loans); install a"
        f" licence for installation {row['installation_id']}"
    )
    assert [result.exit_code for result in started] == [0, 0]
    assert [(result.exit_code, errors(result)) for result in refused] == [(2, [f"error: {m2}"])] * 2
    assert sql(pg_dsn, "SELECT count(*) AS runs FROM ds.runs") == [{"runs": 2}]  # no run written
    # the spec file is stored and audited first, as an upload, which always works
    [cards] = sql(pg_dsn, "SELECT spec_id FROM ds.specs WHERE name = 'cards'")
    audited = sql(pg_dsn, "SELECT action FROM ds.audit_log WHERE object = %s", str(cards["spec_id"]))
    assert audited == [{"action": "spec.create"}] * 2


@pytest.mark.db
def test_in_tokens_mode_ds_run_warns_that_its_run_is_labelled_unapproved(
    store: Callable[..., Path], tmp_path: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store({"public_url": "https://ds.example"})
    (tmp_path / "s.yaml").write_text(APPROVED)
    started = run("run", tmp_path / "s.yaml", "--no-wait", "--config", config)
    [row] = sql(pg_dsn, "SELECT run_id, spec_id, approval_id FROM ds.runs")
    warning = (
        f"warning: spec {row['spec_id']} (cli-approvals 1.0.0) needs a four-eyes approval of clear; tokens"
        f" mode has no approvers, so run {row['run_id']} is labelled unapproved"
    )
    assert started.exit_code == 0, started.stderr
    assert warning in started.stderr.splitlines()
    assert row["approval_id"] is None
    assert run("runs", "--config", config).stdout.splitlines()[1] == (
        "  Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers)."
        " Not sign-off evidence."
    )


@pytest.mark.db
@pytest.mark.parametrize(
    ("value", "refusal"),
    [
        (
            "1.0e+16",
            "holds 1e+16 at {at}, which the store keeps as 10000000000000000: write 10000000000000000",
        ),
        ("-0.0", "holds -0.0 at {at}, which the store keeps as 0: write 0"),
    ],
)
def test_ds_run_refuses_a_spec_the_store_would_not_keep_as_written(
    store: Callable[..., Path], tmp_path: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch, value: str,
    refusal: str,
) -> None:  # fmt: skip
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store({"public_url": "https://ds.example"})
    tolerance = f"compare: {{fields: [{{path: $.x, compare: {{tolerance: {{abs: {value}}}}}}}]}}"
    (tmp_path / "s.yaml").write_text(APPROVED.replace("compare: {}", tolerance))
    refused = run("run", tmp_path / "s.yaml", "--no-wait", "--config", config)
    at = "/compare/fields/0/compare/tolerance/abs"
    assert (refused.exit_code, errors(refused)) == (2, [f"error: spec body {refusal.format(at=at)}"])
    assert sql(pg_dsn, "SELECT count(*) AS specs FROM ds.specs") == [{"specs": 0}]


@pytest.mark.db
def test_approvals_are_listed_and_a_break_glass_revoke_pauses_the_runs_under_one(
    store: Callable[..., Path], tmp_path: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    monkeypatch.setattr("socket.gethostname", lambda: "ds-host")
    config = store(sso(tmp_path))
    asyncio.run(_enterprise(config))
    (tmp_path / "s.yaml").write_text(APPROVED)

    def approvals(*args: Any) -> Result:
        return run("approvals", *args, "--config", config)

    spec_id = asyncio.run(_upload(config, APPROVED))
    before = stored(config, "running", text=APPROVED)  # started before approvals existed
    waiting = approvals("--needed")
    approval_id = asyncio.run(_approve(config, spec_id))
    covered = approvals("--needed")
    under = run("run", tmp_path / "s.yaml", "--no-wait", "--config", config).stdout.split()[1]
    revoked = approvals("revoke", approval_id, "--reason", "wrong clear path")
    refused = [
        approvals("revoke", approval_id, "--reason", "wrong clear path"),
        approvals("revoke", "A-" + "0" * 26, "--reason", "wrong clear path"),
        approvals("revoke", approval_id, "--reason", ""),
        run("approvals", "--json", "revoke", approval_id, "--reason", "wrong clear path"),
        approvals("--status", "open"),
        approvals("--kind", "rule"),
        approvals("--needed", "--kind", "spec"),
    ]
    assert said(waiting) == (0, f"spec {spec_id} (cli-approvals 1.0.0) needs clear: run {before} (running)\n")
    assert said(covered) == (0, "every live run's spec version has the approval it needs\n")
    assert said(revoked) == (
        0, f"revoked {approval_id}; runs paused: {under}; runs stopped: -; sign-offs revoked: -\n",
    )  # fmt: skip
    assert [said(r) for r in refused] == [
        (2, f"error: approval {approval_id} is revoked, not approved\n"),
        (2, f"error: approval A-{'0' * 26} not found\n"),
        (2, "error: --reason must be 1 to 200 characters without control characters\n"),
        (2, "error: put the options after 'revoke'\n"),
        (2, "error: --status is one of requested, approved, rejected, revoked\n"),
        (2, "error: --kind is one of spec, signoff\n"),
        (2, "error: --needed lists spec versions: it takes no --status, --kind or --limit\n"),
    ]
    [line] = approvals().stdout.splitlines()
    bob, carol = (
        sql(pg_dsn, "SELECT user_id FROM ds.users WHERE username = %s", n)[0]["user_id"]
        for n in ("bob", "carol")
    )
    assert re.fullmatch(
        rf"{approval_id}  spec  revoked  spec {spec_id} \(cli-approvals 1\.0\.0\)  clear"
        rf"  requested by user:{bob} \(bob\) \S+  decided by user:{carol} \(carol\) \S+"
        r"  revoked by local:tester@ds-host \S+: 'wrong clear path'",
        line,
    ), line
    filtered = [approvals(*f, "--json") for f in ((), ("--status", "revoked"), ("--status", "approved"),
                                                   ("--kind", "signoff"), ("--limit", "1"))]  # fmt: skip
    assert [[a["approval_id"] for a in json.loads(r.stdout)] for r in filtered] == [
        [approval_id], [approval_id], [], [], [approval_id],
    ]  # fmt: skip
    assert json.loads(approvals("--needed", "--json").stdout) == [{
        "spec_id": spec_id, "name": "cli-approvals", "version": "1.0.0", "needs": ["clear"],
        "approval_id": None,
        "runs": [{"run_id": before, "status": "running"}, {"run_id": under, "status": "paused"}],
    }]  # fmt: skip
    [revoked_at] = sql(pg_dsn, "SELECT revoked_at FROM ds.approvals")
    listed = run("runs", "--config", config).stdout.splitlines()
    assert listed[0].startswith(under) and listed[2].startswith(before)
    assert (listed[1], listed[3]) == (
        f"  Spec approval {approval_id} was revoked on {revoked_at['revoked_at'].astimezone(UTC):%Y-%m-%d} by"
        " local:tester@ds-host (wrong clear path). Not sign-off evidence.",
        "  Unapproved spec version: the run started before approvals (008). Not sign-off evidence.",
    )  # an open build's run says nothing of a licence (a vendor build's:
    # test_report_file_names_labelled), nor does the run stored as before approvals, which has no record
    [revoke], [pause] = audited(pg_dsn, "approval.revoke"), audited(pg_dsn, "paused")
    assert (revoke["actor"], revoke["object"]) == ("local:tester@ds-host", approval_id)
    assert {k: revoke["detail"][k] for k in ("reason", "paused", "stopped", "cascade_of", "asserted")} == {
        "reason": "wrong clear path", "paused": [under], "stopped": [], "cascade_of": None, "asserted": True,
    }  # fmt: skip
    assert (pause["object"], pause["detail"]["reason"], pause["detail"]["approval_id"]) == (
        under, "run_paused_approval", approval_id,
    )  # fmt: skip


def test_serve_and_auth_check_warn_when_no_idp_group_maps_to_rule_approver(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: None)
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: None)
    (tmp_path / "oidc_secret").write_text(CLIENT_SECRET + "\n")
    oidc = idp.settings(
        client_secret_ref=f"file:{tmp_path / 'oidc_secret'}", roles={"spec_author": ["G-author"]}
    )
    auth = {"auth": {"public_url": "https://ds.test", "oidc": oidc}, "encryption": encryption(tmp_path)}
    (tmp_path / "ds.yaml").write_text(SETTINGS + yaml.safe_dump(auth))
    served, checked = (
        run(*command, "--config", tmp_path / "ds.yaml") for command in (["serve"], ["auth", "check"])
    )
    assert [(r.exit_code, r.stderr.splitlines()) for r in (served, checked)] == [(0, [NO_APPROVERS])] * 2


# -- the licence --------------------------------------------------

FP = PackSigner(VENDOR_KEY).fingerprint


def installation(dsn: str) -> str:
    [row] = sql(dsn, "SELECT installation_id FROM ds.installation")
    return row["installation_id"]


def today(dsn: str) -> date:
    """The database's UTC day, which licence dates are judged by."""
    [row] = sql(dsn, "SELECT (now() AT TIME ZONE 'UTC')::date AS day")
    return row["day"]


def licence_at(path: Path, dsn: str, *, days: int = 0, **fields: Any) -> tuple[Path, str]:
    """A licence file at ``path`` for the store at ``dsn``, from ``days`` after its today; its id."""
    data = licence_file(installation(dsn), starts=today(dsn) + timedelta(days=days), **fields)
    path.write_bytes(data)
    return path, data.decode().split("\n")[1].removeprefix("licence: ")


def written(dsn: str) -> dict[str, Any]:
    [counts] = sql(
        dsn,
        """SELECT (SELECT count(*) FROM ds.licences) AS licences,
                  (SELECT count(*) FROM ds.licences WHERE withdrawn_at IS NOT NULL) AS withdrawn,
                  (SELECT count(*) FROM ds.audit_log WHERE action LIKE 'licence.%%') AS audited""",
    )
    return counts


def status(config: Path) -> dict[str, Any]:
    shown = run("licence", "--json", "--config", config)
    assert shown.exit_code == 0, shown.output
    return json.loads(shown.stdout)


@pytest.mark.db
def test_licence_id_stdout(store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    config = store({"public_url": "https://ds.example"})
    assert said(run("licence", "id", "--config", config)) == (0, f"{installation(pg_dsn)}\n")


@pytest.mark.db
def test_an_open_build_needs_no_licence(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a vendor key every pair runs and nothing warns of a licence;
    ``ds licence`` says so, and a file is neither shown nor installed."""

    async def worked(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("uvicorn.run", lambda *_, **__: None)
    monkeypatch.setattr("deltasleuth.runner.run_worker", worked)
    config, names = store(), ("accounts", "loans", "cards")  # developer mode; a third pair
    for name in names:
        (tmp_path / f"{name}.yaml").write_text(APPROVED.replace("name: cli-approvals", f"name: {name}"))
    started = [run("run", tmp_path / f"{name}.yaml", "--no-wait", "--config", config) for name in names]
    assert [result.exit_code for result in started] == [0, 0, 0]
    others = [
        run(*args, "--config", config)
        for args in (("serve",), ("worker",), ("compare", "K1", "--spec", tmp_path / "cards.yaml"), ("runs",))
    ]
    printed = [line for result in (*started, *others) for line in result.output.splitlines()]
    printed = [line.lower() for line in printed if not line.startswith("{")]  # but the SIEM's audit rows
    assert not [line for line in printed if "licen" in line or "evaluation" in line]  # no warning, no label
    assert said(run("licence", "--config", config)) == (0, (
        f"installation {installation(pg_dsn)}\n"
        "licence none: this build needs no licence\n"
        "state open\n"
        "allows: runs yes, sso yes, signoff yes, packs yes\n"
    ))  # fmt: skip
    body = status(config)
    assert (body["state"], body["licence"], body["warnings"], body["installed"]) == ("open", None, [], [])
    assert body["pairs"] == {"limit": None, "since": None, "counted": []} and body["audit_head"]["seq"]
    assert said(run("licence", "id", "--config", config)) == (0, f"{installation(pg_dsn)}\n")
    path, _ = licence_at(tmp_path / "LI.txt", pg_dsn)
    refused = [run("licence", "install", path, "--config", config), run("licence", "show", path)]
    assert [said(result) for result in refused] == [(2, (
        "error: this build embeds no vendor key: it needs no licence and can verify none\n"
    ))] * 2  # fmt: skip
    assert written(pg_dsn) == {"licences": 0, "withdrawn": 0, "audited": 0}  # no licence.pair either
    assert sql(pg_dsn, "SELECT DISTINCT params->'licence' AS licence FROM ds.runs") == [{"licence": None}]


@pytest.mark.usefixtures("vendor_keys")
def test_show_without_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DS_CONFIG", raising=False)
    fields = {"licence": "LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C", "issued": "2026-09-28T10:00:00Z"}
    data = licence_file("I-3f2a9c1b7d4e5a60", starts=date(2026, 10, 1), ends=date(2027, 9, 30), **fields)
    (tmp_path / "mailed.txt").write_bytes(b"\xef\xbb\xbf" + data.replace(b"\n", b" \r\n\r\n"))  # e-mailed
    body = data.decode().rsplit("signature: ", 1)[0]
    shown = (0, f"{body}verifies with key {FP[:12]}… (trusted by this build)\n")
    assert said(run("licence", "show", tmp_path / "mailed.txt")) == shown
    piped = CliRunner().invoke(app, ["licence", "show", "-"], input=(tmp_path / "mailed.txt").read_bytes())
    assert said(piped) == shown  # as piped through docker compose run -T
    (tmp_path / "changed.txt").write_bytes(data.replace(b"Example", b"Exbmple"))
    (tmp_path / "cut.txt").write_bytes(b"\n".join(data.split(b"\n")[:3]))
    assert [said(run("licence", "show", tmp_path / name)) for name in ("changed.txt", "cut.txt")] == [
        (2, f"error: licence {fields['licence']} does not verify with its key {FP[:12]}…: the file was"
            " changed after it was signed; install the file as received\n"),
        (2, f"error: {tmp_path / 'cut.txt'} is not a DeltaSleuth licence (deltasleuth-licence/1): line 4:"
            " expected 'order: …'\n"),
    ]  # fmt: skip
    piped = CliRunner().invoke(app, ["licence", "show", "-"], input=(tmp_path / "cut.txt").read_bytes())
    assert said(piped) == (
        2,
        "error: stdin is not a DeltaSleuth licence (deltasleuth-licence/1): line 4: expected 'order: …'\n",
    )


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_install_refusals_write_nothing(store: Callable[..., Path], pg_dsn: str, tmp_path: Path) -> None:
    config = store()
    ours, day = installation(pg_dsn), today(pg_dsn)
    other = ec.generate_private_key(ec.SECP256R1())
    cut, _ = licence_at(tmp_path / "cut.txt", pg_dsn)
    cut.write_bytes(b"\n".join(cut.read_bytes().split(b"\n")[:3]))
    untrusted, untrusted_id = licence_at(tmp_path / "untrusted.txt", pg_dsn, key=other)
    changed, changed_id = licence_at(tmp_path / "changed.txt", pg_dsn)
    changed.write_bytes(changed.read_bytes().replace(b"Example", b"Exbmple"))
    foreign_data = licence_file("I-" + "a" * 16, starts=day)
    (tmp_path / "foreign.txt").write_bytes(foreign_data)
    foreign_id = foreign_data.decode().split("\n")[1].removeprefix("licence: ")
    ends = day - timedelta(days=31)
    ended, ended_id = licence_at(tmp_path / "ended.txt", pg_dsn, days=-300, ends=ends)
    before = written(pg_dsn)
    refused = [run("licence", "install", path, "--config", config)
               for path in (cut, untrusted, changed, tmp_path / "foreign.txt", ended)]  # fmt: skip
    assert [errors(result) for result in refused] == [
        [f"error: {cut} is not a DeltaSleuth licence (deltasleuth-licence/1): line 4: expected 'order: …'"],
        [f"error: licence {untrusted_id} is signed by key {PackSigner(other).fingerprint[:12]}…, which this"
         " build does not trust: upgrade DeltaSleuth, or ask for a licence signed by a current key"],
        [f"error: licence {changed_id} does not verify with its key {FP[:12]}…: the file was changed after it"
         " was signed; install the file as received"],
        [f"error: licence {foreign_id} is for installation I-{'a' * 16}, not this one ({ours}): ask for a"
         f" licence for {ours} (ds licence id)"],
        [f"error: licence {ended_id} ended on {ends} and its grace on {ends + timedelta(days=30)}: install a"
         " current licence"],
    ]  # fmt: skip
    assert {result.exit_code for result in refused} == {2} and written(pg_dsn) == before == {
        "licences": 0, "withdrawn": 0, "audited": 0,
    }  # fmt: skip
    future, future_id = licence_at(tmp_path / "future.txt", pg_dsn, days=10)
    assert run("licence", "install", future, "--config", config).exit_code == 0
    edited, _ = licence_at(tmp_path / "edited.txt", pg_dsn, days=10, licence=future_id, customer="Other plc")
    withdraw = (
        "licence",
        "withdraw",
        future_id,
        "--reason",
        "issued for the wrong order",
        "--config",
        config,
    )
    conflicting = run("licence", "install", edited, "--config", config)
    assert run(*withdraw).exit_code == 0
    again = run("licence", "install", future, "--config", config)
    assert [(result.exit_code, errors(result)) for result in (conflicting, again)] == [
        (2, [f"error: licence {future_id} is installed already with other content: ask your DeltaSleuth"
             " contact which file is genuine"]),
        (2, [f"error: licence {future_id} was withdrawn on {day}: it is not installed again; ask for a new"
             " licence"]),
    ]  # fmt: skip
    assert written(pg_dsn) == {"licences": 1, "withdrawn": 1, "audited": 2}  # its install and withdrawal


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_install_twice(store: Callable[..., Path], pg_dsn: str, tmp_path: Path) -> None:
    config = store()
    path, licence_id = licence_at(tmp_path / "LI.txt", pg_dsn)
    day = today(pg_dsn)
    first, second = (run("licence", "install", path, "--config", config) for _ in range(2))
    assert said(first) == (0, (
        f"installed {licence_id} (enterprise, Example Bank plc, {day} to {day + timedelta(days=364)},"
        " unlimited pairs): in force now\n"
        "changes now: tier evaluation -> enterprise; pairs 2 -> unlimited; state evaluation -> valid\n"
    ))  # fmt: skip
    assert said(second) == (0, f"licence {licence_id} is installed already\n")  # a restore may repeat it
    assert written(pg_dsn) == {"licences": 1, "withdrawn": 0, "audited": 1}
    [row] = audited(pg_dsn, "licence.install")
    assert (row["object"], row["detail"]["applies"], row["detail"]["tier"]) == (
        licence_id,
        "now",
        "enterprise",
    )


async def _pairs(config: Path) -> str:
    """Runs of three spec names, oldest first (accounts, loans, cards), and a soak of accounts that its
    boundary paused for the licence: the soak's id."""
    from deltasleuth import licence
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from deltasleuth.core.spec import compile_spec
    from deltasleuth.core.spec.documents import parse_yaml_text
    from deltasleuth.store import repo
    from tests.conftest import TESTER, create_run

    async with Context.open(load_settings(config)) as ctx:
        for name in ("accounts", "loans", "cards"):  # one transaction each: their first runs in this order
            text = APPROVED.replace("name: cli-approvals", f"name: {name}")
            async with ctx.pool.connection() as conn:
                raw = parse_yaml_text(text) | {"execution": {"cycleInterval": "1h"}}  # a soak's
                spec_id = await repo.save_spec(conn, compile_spec(raw), created_by=TESTER.actor)
                run_id, _ = await create_run(ctx, conn, spec_id, "soak", {"K1": "K1"}, {})
                if name == "accounts":  # started, then paused at its boundary, as the runner does
                    soak = run_id
                    await repo.set_run_status(conn, run_id, "running")
                    await repo.set_run_status(conn, run_id, "paused", stop_reason=licence.LICENCE_PAUSE)
    return soak


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_install_stdin(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "tester")
    config = store(sso(tmp_path))  # the installation runs SSO
    soak = asyncio.run(_pairs(config))
    day = today(pg_dsn)
    community = licence_file(installation(pg_dsn), tier="community", starts=day, issued=f"{day}T10:00:00Z")
    community_id = community.decode().split("\n")[1].removeprefix("licence: ")
    host = CliRunner().invoke(app, ["licence", "install", "-", "--config", str(config)], input=community)
    ends = day + timedelta(days=364)
    assert said(host) == (0, (
        f"installed {community_id} (community, Example Bank plc, {day} to {ends}, 2 pairs): in force now\n"
        "changes now: tier evaluation -> community; state evaluation -> valid\n"
        "cards have no seat: no new run, and their soaks pause at their next cycle\n"
        "runs under SSO stay refused\n"
    ))  # fmt: skip
    path, enterprise_id = licence_at(tmp_path / "LI.txt", pg_dsn, issued=f"{day}T11:00:00Z")  # a correction
    assert said(run("licence", "install", path, "--config", config)) == (0, (
        f"installed {enterprise_id} (enterprise, Example Bank plc, {day} to {ends}, unlimited pairs): in"
        " force now\n"
        "changes now: tier community -> enterprise; pairs 2 -> unlimited\n"
        f"1 run(s) paused for the licence: {soak}; resume each on its Run screen\n"
    ))  # fmt: skip
    [row] = sql(pg_dsn, "SELECT installed_by FROM ds.licences WHERE licence_id = %s", community_id)
    assert row["installed_by"].startswith("local:tester@")
    assert sql(pg_dsn, "SELECT status FROM ds.runs WHERE run_id = %s", soak) == [{"status": "paused"}]


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_concurrent_installs_serialise(store: Callable[..., Path], pg_dsn: str, tmp_path: Path) -> None:
    config = store()
    path, licence_id = licence_at(tmp_path / "LI.txt", pg_dsn)
    results: list[Result] = []
    with psycopg.connect(pg_dsn) as other:  # another install holding the pair lock (DSLC)
        other.execute("SELECT pg_advisory_xact_lock(%s)", (0x44534C43,))
        installing = threading.Thread(
            target=lambda: results.append(run("licence", "install", path, "--config", config))
        )
        installing.start()
        waiting = """SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()
                     AND wait_event_type = 'Lock' AND wait_event = 'advisory'"""
        for _ in range(500):
            if sql(pg_dsn, waiting) == [{"n": 1}]:
                break
            threading.Event().wait(0.02)
        else:
            pytest.fail("the install never waited for the pair lock")
        text = path.read_text()
        other.execute(
            "INSERT INTO ds.licences (licence_id, text, sha256, installed_by) VALUES (%s, %s, %s, %s)",
            (licence_id, text, hashlib.sha256(text.encode()).hexdigest(), "local:x@y"),
        )
    installing.join(30)
    assert said(results[0]) == (0, f"licence {licence_id} is installed already\n")  # it read after the lock
    assert written(pg_dsn) == {"licences": 1, "withdrawn": 0, "audited": 0}


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_withdraw_before_starts(store: Callable[..., Path], pg_dsn: str, tmp_path: Path) -> None:
    config = store()
    path, licence_id = licence_at(tmp_path / "LI.txt", pg_dsn, days=10)
    starts = today(pg_dsn) + timedelta(days=10)
    installed = run("licence", "install", path, "--config", config)
    assert installed.stdout.splitlines()[0].endswith(
        f"in force from {starts}; an unlicensed evaluation until then"
    )
    reason = "issued for the wrong order"
    refusals = [
        run("licence", "withdraw", licence_id, "--reason", "", "--config", config),
        run("licence", "withdraw", "LI-" + "0" * 26, "--reason", reason, "--config", config),
    ]
    withdrawn = run("licence", "withdraw", licence_id, "--reason", reason, "--config", config)
    twice = run("licence", "withdraw", licence_id, "--reason", reason, "--config", config)
    assert said(withdrawn) == (0, f"withdrew licence {licence_id}: it is not installed again\n")
    assert [errors(result) for result in (*refusals, twice)] == [
        ["error: --reason must be 1 to 200 characters without control characters"],
        [f"error: licence LI-{'0' * 26} is not installed"],
        [f"error: licence {licence_id} is withdrawn already"],
    ]
    [row] = audited(pg_dsn, "licence.withdraw")
    assert (row["object"], row["detail"]["reason"]) == (licence_id, reason)
    [stored] = sql(pg_dsn, "SELECT withdrawn_by, withdraw_reason FROM ds.licences")
    assert stored["withdraw_reason"] == reason and stored["withdrawn_by"].startswith("local:")
    assert status(config)["state"] == "evaluation"


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_withdraw_started_W1(store: Callable[..., Path], pg_dsn: str, tmp_path: Path) -> None:
    config = store()
    path, licence_id = licence_at(tmp_path / "LI.txt", pg_dsn)
    assert run("licence", "install", path, "--config", config).exit_code == 0
    refused = run("licence", "withdraw", licence_id, "--reason", "wrong order", "--config", config)
    assert (refused.exit_code, errors(refused)) == (2, [
        f"error: licence {licence_id} started on {today(pg_dsn)}: a started licence is replaced by a new one,"
        " not withdrawn"
    ])  # fmt: skip
    assert written(pg_dsn) == {"licences": 1, "withdrawn": 0, "audited": 1}


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_no_withdrawal_returns_to_evaluation(store: Callable[..., Path], pg_dsn: str, tmp_path: Path) -> None:
    config = store()
    day = today(pg_dsn)
    running, running_id = licence_at(tmp_path / "running.txt", pg_dsn)
    renewal, renewal_id = licence_at(tmp_path / "renewal.txt", pg_dsn, days=365)  # installed early
    for path in (running, renewal):
        assert run("licence", "install", path, "--config", config).exit_code == 0
    withdrawn = run("licence", "withdraw", renewal_id, "--reason", "a wrong renewal", "--config", config)
    refused = run("licence", "withdraw", running_id, "--reason", "back to evaluation", "--config", config)
    assert (withdrawn.exit_code, refused.exit_code) == (0, 2)
    assert said(run("licence", "--config", config)) == (0, (
        f"installation {installation(pg_dsn)}\n"
        f"licence {running_id}: Example Bank plc (order ORD-2026-014), enterprise, unlimited pairs, {day} to"
        f" {day + timedelta(days=364)}\n"
        "state valid\n"
        "allows: runs yes, sso yes, signoff yes, packs yes\n"
    ))  # fmt: skip
    shown = status(config)
    assert (shown["state"], shown["licence"]["licence_id"], shown["renewal"]) == ("valid", running_id, None)
    assert [(i["licence_id"], i["in_force"], i["withdraw_reason"]) for i in shown["installed"]] == [
        (running_id, True, None), (renewal_id, False, "a wrong renewal"),
    ]  # fmt: skip
    assert shown["audit_head"]["seq"] == sql(pg_dsn, "SELECT max(seq) AS seq FROM ds.audit_chain")[0]["seq"]


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_warnings_only_four_commands(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def worked(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("uvicorn.run", lambda *_, **__: None)
    monkeypatch.setattr("deltasleuth.runner.run_worker", worked)
    config = store()  # unlicensed: an evaluation
    (tmp_path / "s.yaml").write_text(APPROVED)
    started = run("run", tmp_path / "s.yaml", "--no-wait", "--config", config)
    [row] = sql(pg_dsn, "SELECT run_id FROM ds.runs")
    evaluation = f"Unlicensed evaluation of installation {installation(pg_dsn)}: at most 2 pairs"
    spec = tmp_path / "s.yaml"
    commands = {
        "serve": ("serve",), "worker": ("worker",), "compare": ("compare", "K1", "--spec", spec),
        "runs": ("runs",), "licence": ("licence",), "licence id": ("licence", "id"), "audit": ("audit",),
        "audit verify": ("audit", "verify"), "report": ("report", row["run_id"], "--out", tmp_path / "o"),
        "purge": ("purge", "--dry-run"), "approvals": ("approvals",), "keys": ("keys", "status"),
    }  # fmt: skip
    results = {name: run(*args, "--config", config) for name, args in commands.items()} | {"run": started}
    printed = {
        name: any(line.startswith(f"warning: {evaluation}") for line in r.stderr.splitlines())
        for name, r in results.items()
    }
    assert {name for name, warns in printed.items() if warns} == {"serve", "worker", "run", "compare"}, {
        name: r.output for name, r in results.items()
    }
    named, status_line = results["licence"].stdout.splitlines()[1:3]
    assert named == "licence none: an unlicensed evaluation"
    assert status_line.startswith(f"state evaluation: {evaluation} (1 used)")


@pytest.mark.db
def test_licence_unknown_not_evaluation(store: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """A standing that cannot be read is unknown, not an unlicensed evaluation: an installed licence may be
    in force."""

    async def unreadable(*_: Any, **__: Any) -> None:
        raise RuntimeError("unreadable")

    config = store()
    monkeypatch.setattr("deltasleuth.licence.standing", unreadable)
    assert said(run("licence", "--config", config)) == (0, (
        "installation -\n"
        "licence unknown: the standing could not be read (see the server log)\n"
        "state unknown: licence status unavailable (RuntimeError): see the server log\n"
    ))  # fmt: skip


async def _expired_pack(dsn: str, key: Path) -> tuple[str, str]:
    """A pack issued under an Enterprise licence, on the database at ``dsn``; then that licence's row gone
    (an operator's DELETE with the triggers disabled) and an Enterprise licence whose grace ended stored,
    so the installation is expired: the pack's id and run."""
    from psycopg_pool import AsyncConnectionPool

    from deltasleuth.config import Settings
    from deltasleuth.context import Context
    from deltasleuth.reports import pack
    from deltasleuth.security.tokenise import Tokeniser
    from deltasleuth.store.db import migrate
    from tests.conftest import TEST_KEYRING, evidence_store, unlicensed
    from tests.unit.reports.conftest import TARGETS, with_packs
    from tests.unit.reports.packs import signed_cycle

    pool = AsyncConnectionPool(dsn, open=False, max_size=4)
    await pool.open(wait=True)
    try:
        await migrate(pool)
        settings = Settings("file:/unused", key.parent / "e", key.parent / "w", key.parent / "t.yaml")
        evidence = evidence_store(settings.evidence_dir)
        ctx = Context(settings, pool, TARGETS, Tokeniser(b"k" * 32), evidence, None, None, TEST_KEYRING)
        signed = await signed_cycle(with_packs(ctx, key))
        issued = await pack.issue(with_packs(ctx, key), signed.run_id, 1, who=signed.people["olga"])
        await unlicensed(ctx, ended="enterprise")
        return issued["pack_id"], signed.run_id
    finally:
        await pool.close()


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_evidence_commands_when_expired(pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    key = tmp_path / "pack-key"
    key.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    pack_id, run_id = asyncio.run(_expired_pack(pg_dsn, key))
    (tmp_path / "dsn").write_text(pg_dsn + "\n")
    (tmp_path / "t.yaml").write_text(TARGETS)
    (tmp_path / "oidc_secret").write_text("s3cret\n")
    auth = sso(tmp_path) | {
        "oidc": sso(tmp_path)["oidc"] | {"client_secret_ref": f"file:{tmp_path / 'oidc_secret'}"}
    }
    config = tmp_path / "ds.yaml"
    config.write_text(yaml.safe_dump({
        "database_dsn_ref": f"file:{tmp_path / 'dsn'}", "evidence_dir": "e", "workspace_dir": "w",
        "targets_file": "t.yaml", "auth": auth, "encryption": encryption(tmp_path / "keys"),
        "packs": {"installation": "test-1", "signing_key_ref": f"file:{key}"},
    }))  # fmt: skip
    monkeypatch.setenv("DS_OPERATOR", "ann")
    assert status(config)["state"] == "expired"
    commands = [
        ("audit", "verify"),
        ("packs", "download", pack_id, "--out", tmp_path / "out"),
        ("report", run_id, "--out", tmp_path / "reports", "--kind", "report.pdf"),
        ("purge", "release", run_id),  # its sign-off's hold
        ("purge", "hold", run_id, "--reason", "a legal hold"),
        ("purge",),
    ]
    done = [run(*command, "--config", config) for command in commands]
    checked = run("packs", "verify", tmp_path / "out" / f"{pack_id}.zip", "--fingerprint",
                  PackSigner.load(f"file:{key}").fingerprint)  # fmt: skip
    assert [(r.exit_code, errors(r)) for r in (*done, checked)] == [(0, [])] * 7, [r.output for r in done]
    assert (tmp_path / "reports" / f"{run_id}-report.pdf").exists()  # made under the licence: no label


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_report_file_names_labelled(store: Callable[..., Path], pg_dsn: str, tmp_path: Path) -> None:
    config = store()
    (tmp_path / "s.yaml").write_text(APPROVED)

    def started() -> str:
        result = run("run", tmp_path / "s.yaml", "--no-wait", "--config", config)
        return re.search(r"run (R-\w+) started", result.stdout)[1]  # type: ignore[index]

    evaluation = started()
    path, licence_id = licence_at(tmp_path / "LI.txt", pg_dsn)
    assert run("licence", "install", path, "--config", config).exit_code == 0
    licensed = started()
    names = ("report.pdf", "discrepancies.csv")
    kinds = ("--kind", names[0], "--kind", names[1])
    reports = [
        run("report", r, "--out", tmp_path / "o", *kinds, "--config", config) for r in (evaluation, licensed)
    ]
    assert [said(r) for r in reports] == [
        (0, "".join(f"{tmp_path / 'o' / f'{evaluation}-unlicensed-evaluation-{name}'}\n" for name in names)),
        (0, "".join(f"{tmp_path / 'o' / f'{licensed}-{name}'}\n" for name in names)),
    ]
    sql(pg_dsn, "UPDATE ds.runs SET status = 'paused', stop_reason = 'run_paused_licence', cycle = 4"
                " WHERE run_id = %s", licensed)  # fmt: skip
    listed = run("runs", "--config", config).stdout.splitlines()
    assert [line for line in listed if not line.startswith(("R-", "  Unapproved", "  The spec"))] == [
        f"  Run {licensed} was made under licence {licence_id} (enterprise).",
        "  Paused for the licence at the end of cycle 3",
        f"  Unlicensed evaluation: run {evaluation} was made without a licence. Not for production use; no"
        " evidence pack is issued for it.",
    ]


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_install_needs_ds_operator(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    config = store({"public_url": "https://ds.example"})
    path, licence_id = licence_at(tmp_path / "LI.txt", pg_dsn)
    writes = [
        run("licence", "install", path, "--config", config),
        CliRunner().invoke(
            app, ["licence", "install", "-", "--config", str(config)], input=path.read_bytes()
        ),
        run("licence", "withdraw", licence_id, "--reason", "wrong order", "--config", config),
    ]
    assert [said(result) for result in writes] == [(2, f"error: {OPERATOR}\n")] * 3
    reads = [run("licence", "--config", config), run("licence", "id", "--config", config),
             run("licence", "show", path)]  # fmt: skip
    assert [result.exit_code for result in reads] == [0, 0, 0]
    assert written(pg_dsn) == {"licences": 0, "withdrawn": 0, "audited": 0}


async def _sampled(dsn: str) -> dict[str, str]:
    """Draws of one population under two names, written as the worker commits them: customer-profile-2's
    failed, customer-profile's passed, then one of customer-profile still comparing, and runs whose next
    draw is pending: one running, one paused and one cancelled."""
    from deltasleuth.store import repo
    from tests.unit.store.four_eyes import drawn, person, sampled, upload

    profile, other = sampled("customer-profile"), sampled("customer-profile-2")
    async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.transaction():
        bob = await person(conn, "bob", "operator")
        ids = {v.name: await upload(conn, v, bob) for v in (profile, other)}
        found = {}
        for names, version, options in (
            (("failed_run", "failed"), other, {"discrepant": ["C001"]}),
            (("passed_run", "passed"), profile, {}),
            (("live_run", "live"), profile, {"end": False}),
        ):
            found |= dict(
                zip(names, await drawn(conn, ids[version.name], version, bob, **options), strict=True)
            )
        for name, status in (("pending", "running"), ("paused", "paused"), ("cancelled", "cancelled")):
            found[name], _ = await drawn(conn, ids[profile.name], profile, bob, end=False)
            await conn.execute(  # its next cycle, still to be drawn
                "UPDATE ds.runs SET cycle = 2, draw = jsonb_build_object('due', now()) WHERE run_id = %s",
                (found[name],),
            )
            await repo.set_run_status(conn, found[name], status)
    return found


def test_samples_list_every_draw_or_a_name_s_ledger(store: Callable[..., Path], pg_dsn: str) -> None:
    config = store()
    found = asyncio.run(_sampled(pg_dsn))

    def samples(*args: Any) -> Result:
        return run("samples", *args, "--config", config)

    every, ledger = samples(), samples("--spec", "customer-profile")
    as_json = json.loads(samples("--spec", "customer-profile-2", "--json").stdout)
    unknown, misplaced = samples("--spec", "nope"), run("samples", "--json", "size", "--tier", "high")
    runs = run("runs", "--config", config).stdout
    lines = ledger.stdout.splitlines()
    assert every.stdout == ledger.stdout and len(lines) == 6
    assert re.fullmatch(
        rf"{found['failed']}  spec customer-profile-2  run {found['failed_run']} cycle 1  bottom_k  6 keys"
        r"  drawn \S+  failed \(segment=retail\)  blocking: close it with the fix",
        lines[0],
    ), lines[0]
    assert lines[1].endswith("  passed") and lines[2].endswith("  live") and found["passed"] in lines[1]
    assert [(d["draw_id"], d["status"], d["blocking"]) for d in as_json] == [
        (found["failed"], "failed", True), (found["passed"], "passed", False), (found["live"], "live", False),
        *((d["draw_id"], "live", False) for d in as_json[3:]),
    ]  # fmt: skip
    assert len(as_json) == 6
    assert all("nonce" not in d and "strata" not in d for d in as_json)
    assert said(unknown) == (2, "error: spec nope not found\n")
    assert said(misplaced) == (2, "error: put the options after 'size'\n")
    assert f"  sample {found['live']}: 2 strata, 6 keys\n" in runs
    blocks = {block.split()[0]: block.split("\n")[1:] for block in re.split(r"\n(?=R-)", runs.strip())}
    # Only a running run draws; a paused one draws after its resume, a stopped one never.
    assert [line for line in blocks[found["pending"]] if "draw" in line] == ["  drawing sample"]
    paused = [line for line in blocks[found["paused"]] if "draw" in line]
    assert paused == ["  sample to be drawn after resume"]
    assert [line for line in blocks[found["cancelled"]] if "draw" in line] == []


# -- labelled accuracy -----------------------------------------------------


async def _labelled(dsn: str) -> dict[str, str]:
    """Two ended runs of customer-profile that dave started: the first with a labelling he drew and frank
    labelled, which his sign-off request attests; the second with a newer one, drawn and not labelled."""
    from tests.unit.store import four_eyes
    from tests.unit.store.four_eyes import counters, end, person, sampled, start, upload

    version = sampled()
    async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.transaction():
        dave, frank = await person(conn, "dave", "operator"), await person(conn, "frank", "operator")
        spec_id = await upload(conn, version, dave)
        found = {}
        for name, labeller in (("labelled", frank), ("drawn", None)):
            run_id = found[f"{name}_run"] = await start(conn, spec_id, dave, status="completed")
            await end(conn, run_id, counters(1))
            found[name] = (await four_eyes.labelling(conn, run_id, dave, labeller))["labelling_id"]
        found["signoff"] = await four_eyes.request_signoff(conn, found["labelled_run"], 1, dave, version)
    return found


@pytest.mark.db
def test_labellings_lists_each_in_one_line_and_audits_nothing(
    store: Callable[..., Path], pg_dsn: str
) -> None:
    """``ds labellings``: ``accuracy.summary``'s line per labelling, newest
    first; a run's alone; as JSON the API's list items, without sheets, selections or scores; an unknown run
    exits 2."""
    config = store()
    found = asyncio.run(_labelled(pg_dsn))
    actions = "SELECT action FROM ds.audit_log ORDER BY id"
    rows = sql(pg_dsn, actions)

    def labellings(*args: Any) -> Result:
        return run("labellings", *args, "--config", config)

    today, against = f"{date.today():%Y}", "against CBS-PROD.account_master as of 2026-10-01"
    every, one = labellings(), labellings("--run", found["labelled_run"])
    as_json = json.loads(labellings("--json").stdout)
    drawn, labelled = every.stdout.splitlines()
    assert re.fullmatch(
        rf"{found['drawn']} run {found['drawn_run']} cycle 1: drawn {today}-\d\d-\d\d by dave {against};"
        " not labelled",
        drawn,
    ), drawn
    assert re.fullmatch(
        rf"{found['labelled']} run {found['labelled_run']} cycle 1: 1 of 1 discrepancies labelled"
        rf" {today}-\d\d-\d\d by frank {against} \(attested by {found['signoff']}\): b \(new\) alone"
        r" right 1, a \(legacy\) alone right 0, neither 0, did not matter 0, undecidable 0; b \(new\) wrong"
        r" on 0 of 1 decided \(every item labelled: exact\)",
        labelled,
    ), labelled
    assert one.stdout == labelled + "\n"
    assert [(v["labelling_id"], v["attested"], v["summary"]) for v in as_json] == [
        (found["drawn"], None, drawn),
        (found["labelled"], found["signoff"], labelled),
    ]
    assert all(set(v) == {*accuracy.LISTED, "summary", "attested"} for v in as_json)  # the API's list items
    assert (as_json[0]["labelled_at"], as_json[1]["labelled_by_name"]) == (None, "frank")
    assert said(labellings("--run", "R-nope")) == (2, "error: run R-nope not found\n")
    after = [row for row in sql(pg_dsn, actions) if row["action"] != "keys.created"]  # the first start's keys
    assert after == rows


# -- alerts and notifications ------------------------------------------


def failed(result: Result) -> tuple[int, list[str]]:
    """The exit code and the error lines (a command's stderr also holds its SIEM copy)."""
    return result.exit_code, errors(result)


NOTIFY_OFF = "error: notifications are off: add notify to deltasleuth.yaml and restart\n"


def hook_settings(base: str, ref: str) -> dict[str, Any]:
    hook = {
        "kind": "webhook",
        "url": f"{base}/webhook",
        "signing_key_refs": [ref],
        "allow_private_network": True,
    }
    rule = {
        "name": "gaps",
        "events": ["cycle.ended"],
        "channels": ["ops-hook"],
        "when": {"mismatch_rate_above": "0.1"},
    }
    health = {"name": "health", "events": ["run.failed"], "channels": ["ops-hook"]}
    return {"label": "test", "channels": {"ops-hook": hook}, "rules": [rule, health]}


def test_notify_check_reads_resolves_and_vets_each_channel_without_sending(
    tmp_path: Path, loopback: Any
) -> None:
    teams_host = "prod-00.eastus.logic.azure.com"
    good = f"whsec_{base64.b64encode(os.urandom(32)).decode()}\n"
    for name, text in (("hook.key", good), ("short.key", "whsec_c2hvcnQ=\n"),
                       ("accent.key", f"whsec_\u00e9{good[6:]}"),
                       ("teams.url", f"https://{teams_host}/workflows/s3cr3t\n"),
                       ("wrong.url", "https://elsewhere.example/workflows/s3cr3t\n")):  # fmt: skip
        (tmp_path / name).write_text(text)

    def hook(path: str, key: str = "hook.key", **more: Any) -> dict[str, Any]:
        url = f"https://{path}"
        return {"kind": "webhook", "url": url, "signing_key_refs": [f"file:{tmp_path / key}"]} | more

    teams = {"kind": "teams", "host": teams_host, "proxy": True}
    mail = {"kind": "email", "smtp_host": "smtp.bank.example", "sender": "ds@bank.example",
            "recipients": ["owners@bank.example"]}  # fmt: skip
    notify = {
        "label": "test", "proxy": "http://proxy.bank.example:8080",
        "channels": {
            "ops-hook": hook("events.bank.example/deltasleuth", allow_private_network=True),
            "owners-teams": teams | {"url_ref": f"file:{tmp_path / 'teams.url'}"},
            "wrong-teams": teams | {"url_ref": f"file:{tmp_path / 'wrong.url'}"},
            "owners-mail": mail,
            "gone-hook": hook("gone.bank.example/x"),
            "short-hook": hook("events.bank.example/y", "short.key"),
            "accent-hook": hook("events.bank.example/w", "accent.key"),  # not ASCII: never a crash
            "lost-hook": hook("events.bank.example/z", "lost.key"),
        },
    }  # fmt: skip
    (tmp_path / "ds.yaml").write_text(SETTINGS + yaml.safe_dump({"notify": notify}))
    loopback.names |= {"events.bank.example": ["10.1.4.2"], "proxy.bank.example": ["10.2.0.5"],
                       "smtp.bank.example": ["10.1.2.9"]}  # fmt: skip
    result = run("notify", "check", "--config", tmp_path / "ds.yaml")
    loopback.refused.remove("resolve gone.bank.example")
    assert result.exit_code == 2
    assert result.stdout.splitlines() == [
        "ok: channel ops-hook (webhook, events.bank.example): resolves to 10.1.4.2 (private: allowed);"
        " 1 signing key readable",
        f"ok: channel owners-teams (teams, {teams_host}): via proxy proxy.bank.example:8080 at 10.2.0.5;"
        " the URL file names the host",
    ]
    assert errors(result) == [  # in the settings' order (yaml.safe_dump sorts them)
        f"error: channel accent-hook: the signing key in {tmp_path / 'accent.key'} is not whsec_ and base64"
        " of 24 to 64 bytes",
        "error: channel gone-hook: host gone.bank.example does not resolve",
        f"error: channel lost-hook: cannot read secret file {tmp_path / 'lost.key'}: No such file or"
        " directory",
        "error: channel owners-mail: smtp.bank.example resolves to 10.1.2.9, a private address: set"
        " allow_private_network to allow it",
        f"error: channel short-hook: the signing key in {tmp_path / 'short.key'} is not whsec_ and base64"
        " of 24 to 64 bytes",
        f"error: channel wrong-teams: the URL in {tmp_path / 'wrong.url'} is not an https URL for host"
        f" {teams_host}",
    ]
    assert "s3cr3t" not in result.output and "/workflows" not in result.output
    loopback.names["proxy.bank.example"] = ["169.254.10.1"]
    proxied = run("notify", "check", "--config", tmp_path / "ds.yaml")
    assert proxied.stderr.splitlines()[0] == (
        "error: notify.proxy proxy.bank.example resolves to 169.254.10.1: never a notification destination"
    )
    assert "owners-teams" not in proxied.output  # its proxy's error says why
    loopback.refused.clear()


def test_notify_commands_refuse_while_notifications_are_off(
    store: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "ops")
    config = store()
    mute = ("mute", "--channel", "ops-hook", "--hours", "1", "--reason", "r")
    commands = [
        ("check",),
        ("explain", "R-1"),
        ("test", "ops-hook"),
        ("retry", "ops-hook"),
        mute,
        ("unmute", "M-1"),
    ]
    refused = [errors(run("notify", *command, "--config", config)) for command in commands]
    assert refused == [[NOTIFY_OFF.rstrip()]] * len(commands)
    assert said(run("notify", "--config", config)) == (0, NOTIFY_OFF.removeprefix("error: "))


@pytest.mark.usefixtures("loopback")
def test_notify_acts_as_the_operator_audited_and_says_what_it_did(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "ops")
    with receiving_thread(tmp_path) as (hook, base, ref):
        config = store(notify=hook_settings(base, ref))

        def notify(*args: Any) -> Result:
            return run("notify", *args, "--config", config)

        sent = notify("test", "ops-hook")
        hook.failures.append((401, None))
        refused = notify("test", "ops-hook")
        sql(pg_dsn, """INSERT INTO ds.notify_deliveries (delivery_id, event_id, event, severity, channel,
                           dedup_key, body, body_sha256, render, state, attempts, last_error, done_at)
                       VALUES (%s, 'seq:1:run.failed', 'run.failed', 'alert', 'ops-hook', 'run.end|R-1',
                               '{}', repeat('0', 64), 'webhook:1:1', 'dead', 8, 'http_5xx', now())""",
            "N-" + "Z" * 26)  # fmt: skip
        retried = notify("retry", "ops-hook", "--since", "2026-01-01T00:00:00+00:00")
        undeclared = notify("test", "ops-slak")
        muted = notify("mute", "--channel", "ops-hook", "--hours", "2", "--reason", "a planned outage")
        both = notify("mute", "--channel", "ops-hook", "--run", "R-1", "--hours", "2", "--reason", "r")
        hours = notify("mute", "--channel", "ops-hook", "--hours", "200", "--reason", "r")
        status, as_json = notify(), json.loads(notify("--json").stdout)
        mute_id = muted.stdout.split("(")[-1].rstrip(")\n")
        unmuted, again = notify("unmute", mute_id), notify("unmute", mute_id)
        listed = notify("deliveries", "--state", "pending").stdout.splitlines()
    ids = [
        row["delivery_id"] for row in sql(pg_dsn, "SELECT delivery_id FROM ds.notify_deliveries ORDER BY 1")
    ]
    assert re.fullmatch(rf"sent to ops-hook: HTTP 200 in \d+ ms \({ids[0]}\)\n", sent.stdout)
    assert failed(refused) == (2, [f"error: ops-hook: http_4xx (HTTP 401) ({ids[1]})"])
    assert said(retried) == (
        0,
        "1 dead deliveries of ops-hook since 2026-01-01T00:00:00+00:00 are pending again\n",
    )
    assert failed(undeclared) == (2, ["error: channel ops-slak is not declared"])
    assert re.fullmatch(
        r"muted ops-hook until \d{4}-\d\d-\d\d \d\d:\d\d UTC \(M-[0-9A-Z]{26}\)\n", muted.stdout
    )
    assert failed(both) == (2, ["error: give --channel or --run"])
    assert failed(hours) == (2, ["error: hours must be 1 to 168"])
    assert status.stdout.splitlines()[0].startswith("Notifications to ops-hook are muted until ")
    assert (
        "by local:ops@" in status.stdout
        and "channel ops-hook (webhook, 127.0.0.1): 1 pending" in status.stdout
    )
    assert as_json["enabled"] and [m["mute_id"] for m in as_json["mutes"]] == [mute_id]
    assert said(unmuted) == (0, f"ended mute {mute_id}\n")
    assert failed(again) == (2, [f"error: mute {mute_id} ended already"])
    assert len(listed) == 1 and listed[0].startswith(f"{ids[2]}  ") and "  pending  0 attempt(s)" in listed[0]
    actions = [
        row["action"] for row in sql(pg_dsn, "SELECT action FROM ds.audit_log WHERE action LIKE 'notify.%%'")
    ]
    assert actions == ["notify.test", "notify.test", "notify.retry", "notify.mute", "notify.unmute"]
    assert {
        row["actor"][:10]
        for row in sql(pg_dsn, "SELECT actor FROM ds.audit_log WHERE action LIKE 'notify.%%'")
    } == {"local:ops@"}


def test_notify_explain_gives_a_cycle_s_digest_and_each_rule_s_verdict(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path
) -> None:
    (tmp_path / "hook.key").write_text(f"whsec_{base64.b64encode(os.urandom(32)).decode()}\n")
    config = store(notify=hook_settings("https://events.bank.example", f"file:{tmp_path / 'hook.key'}"))
    run_id = stored(config, "running", ended=0, cycle=2)
    counters = {"total": 1, "compared": 1, "match": 0, "different": 1, "indeterminate": 0, "cancelled": 0,
                "outstanding": 0, "mismatch_rate": "1", "coverage": "1", "absent_both": 0}  # fmt: skip
    sql(pg_dsn, "UPDATE ds.cycles SET counters = %s WHERE run_id = %s", json.dumps(counters), run_id)

    def explain(*args: Any) -> Result:
        return run("notify", "explain", *args, "--config", config)

    found, as_json = explain(run_id), json.loads(explain(run_id, "--cycle", "1", "--json").stdout)
    missing = ("R-0000000000000000000000000Z",), (run_id, "--cycle", "3"), (run_id, "--cycle", "2")
    refusals = [failed(explain(*args)) for args in missing]
    sql(pg_dsn, "UPDATE ds.cycles SET purging_at = now(), purge_reason = 'expired' WHERE run_id = %s", run_id)
    purged = explain(run_id).stdout.splitlines()
    assert found.exit_code == 0 and found.stdout.splitlines() == [
        f"cycle 1 of run {run_id} (cli-retention, batch)",
        "  total 1, compared 1, match 0, different 1, indeterminate 0, cancelled 0, outstanding 0,"
        " mismatch_rate 1, coverage 1, absent_both 0",
        "  mismatch_rate 1 (held by no earlier cycle)",
        "  indeterminate_rate 0 (held by no earlier cycle)",
        "  coverage 1 (held by no earlier cycle)",
        "  unexplained_critical 0 (held by no earlier cycle)",
        "  in the first cycle: 0 root causes and 0 discrepant keys observed",
        "  unavailable: -",
        "rule gaps: fires: mismatch_rate_above crossed",
        "rule health: takes no cycle.ended",
    ]
    assert as_json["cycle"] == 1 and as_json["rules"]["gaps"] == "fires: mismatch_rate_above crossed"
    assert refusals == [
        (2, ["error: run R-0000000000000000000000000Z not found"]),
        (2, [f"error: run {run_id} has no cycle 3"]),
        (2, [f"error: cycle 2 of run {run_id} has not ended"]),
    ]
    assert purged[0] == "cycle 1 is purged: root causes, key changes and criticals are unavailable"
    assert purged[-3] == (
        "  unavailable: new_root_causes, gone_root_causes, newly_discrepant, no_longer_discrepant,"
        " unexplained_critical"
    )


def test_serve_worker_and_run_warn_of_an_unreadable_notify_secret_and_go_on(
    store: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_OPERATOR", "ops")
    lost = {"kind": "webhook", "url": "https://events.bank.example/x",
            "signing_key_refs": [f"file:{tmp_path / 'lost.key'}"]}  # fmt: skip
    config = store(notify={"label": "t", "channels": {"h": lost}})
    (tmp_path / "s.yaml").write_text(APPROVED)
    started = run("run", tmp_path / "s.yaml", "--no-wait", "--config", config)
    warning = (
        f"warning: notify channel h: cannot read secret file {tmp_path / 'lost.key'}: No such file or"
        " directory; its deliveries retry until it is readable"
    )
    assert started.exit_code == 0, started.stderr
    assert warning in started.stderr.splitlines()
    assert re.match(r"run R-[0-9A-Z]{26} started\n", started.stdout)


# -- cut-over readiness ------------------------------------------------

READINESS = {
    "programmes": [
        {
            "name": "cutover-q4",
            "gates": {"convergence": {"cycles": 2}},
            "pairs": [{"spec": "unit-runner", "entity": "item"}, {"spec": "unit-none"}],
        }
    ]
}


async def _ready(config: Path) -> dict[str, Any]:
    """Two pairs as the worker measured and recorded them: unit-runner, whose soak over two tables read
    whole ended three clean cycles, and unit-other, whose one cycle is over its target; and a one-shot
    compare, which readiness leaves out."""
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from deltasleuth.runner import readiness as watching
    from tests.unit.runner.conftest import Harness
    from tests.unit.runner.measuring import MATCH, MISMATCH, SOAK, ended, scanned, started, stored

    async with Context.open(load_settings(config)) as ctx:
        h = Harness(ctx)
        good = await scanned(h)
        for _ in range(3):
            await ended(h, good, MATCH)
        other = await scanned(h, await stored(h, SOAK.replace("unit-runner", "unit-other")))
        await ended(h, other, MATCH | {"K001": MISMATCH, "K002": MISMATCH})
        once = await started(h, keys=["K001"], mode="one_shot")
        await ended(h, once, {"K001": "MATCH"})
        await watching.watch(ctx, watching.State())
        spec_id = (await h.run(good))["spec_id"]
    return {"good": good, "other": other, "once": once, "spec_id": spec_id}


@pytest.mark.db
def test_readiness_lists_the_pairs_and_fail_unless_exits_0_or_1(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ds readiness``: the definition, each programme's roll-up, each pair in
    one line with the gates it does not meet; ``--fail-unless`` for a pipeline; reads audit nothing and
    need no DS_OPERATOR."""
    from deltasleuth.core import readiness as gates

    monkeypatch.delenv("DS_OPERATOR", raising=False)
    config = store(readiness=READINESS)
    found = asyncio.run(_ready(config))
    before = sql(pg_dsn, "SELECT count(*) AS n FROM ds.audit_log")

    def readiness(*args: Any) -> Result:
        return run("readiness", *args, "--config", config)

    listed = readiness()
    lines = listed.stdout.splitlines()
    today = f"{date.today():%Y}"
    assert listed.exit_code == 0 and lines[:2] == [
        gates.DEFINITION,
        f"readiness settings {gates.config_sha256(_settings(config).readiness)}",
    ]
    assert lines[2] == "cutover-q4: 1 of 2 pairs parity-ready or better (worst: no evidence)"
    assert lines[3:6] == [
        "unit-none  -  no evidence  0 of 5 gates",
        "  population unknown: no spec named unit-none is stored",
        "  critical unknown: no spec named unit-none is stored",
    ]
    profile = lines.index("  deviation: convergence 2 cycles (default 3; relaxed)", 6) + 1
    assert re.fullmatch(
        rf"unit-runner 1\.0\.0 \(spec {found['spec_id']}\)  item  parity ready  5 of 5 gates  cycle 3 of"
        rf" {found['good']} \({today}-\d\d-\d\d\)",
        lines[profile],
    ), lines[profile]
    assert lines[profile + 1 : profile + 3] == [
        "  deviation: convergence 2 cycles (default 3; relaxed)",
        "(no programme): 0 of 1 pairs parity-ready or better (worst: not ready)",
    ]
    assert (
        lines[profile + 3].startswith("unit-other 1.0.0 (spec ")
        and "  not ready  3 of 5 gates  " in lines[-3]
    )
    assert lines[-2] == (
        f"  targets not met: cycle 1 of run {found['other']} has 2 discrepant or unresolved key(s) of 1,000"
        " observed whole; its pooled 0.1% target allows 1: 1 over; 0 key(s) suppressed (known differences or"
        " noise only)"
    )
    assert lines[-1].startswith(
        "  convergence not met: 0 of the 3 evidence cycles in a row that 1.0.0 (spec "
    )
    assert found["once"] not in listed.stdout  # a one-shot compare is no pair's cycle
    # --fail-unless: 1 when a listed pair is below the state, 0 otherwise; an unknown state is refused.
    assert readiness("--fail-unless", "parity_ready").exit_code == 1
    assert readiness("--fail-unless", "not_ready", "--state", "not_ready").exit_code == 0
    assert readiness("--fail-unless", "parity_ready", "--entity", "item").exit_code == 0
    assert readiness("--fail-unless", "evidenced", "--entity", "item", "--json").exit_code == 1
    assert said(readiness("--fail-unless", "ready")) == (
        2,
        f"error: state must be one of {', '.join(gates.STATES)}\n",
    )
    assert said(readiness("--programme", "cutover-q9")) == (
        2,
        "error: programme cutover-q9 is not configured\n",
    )
    assert said(readiness("--as-of", "yesterday")) == (
        2,
        "error: as_of must be an ISO-8601 time with a UTC offset\n",
    )
    assert said(readiness("pair", "customer-x")) == (
        2,
        "error: pair customer-x not found: no spec or readiness setting names it\n",
    )
    assert said(readiness("--json", "pair", "unit-runner"))[0] == 2  # options go after the subcommand
    as_json = json.loads(readiness("--json", "--programme", "cutover-q4").stdout)
    assert [(i["pair"], i["state"]) for i in as_json["items"]] == [
        ("unit-none", "no_evidence"),
        ("unit-runner", "parity_ready"),
    ]
    recorded = readiness("--as-of", "2100-01-01T00:00:00Z").stdout.splitlines()
    assert recorded[2] == "as recorded by 2100-01-01T00:00:00+00:00"
    assert f"unit-runner  item  parity ready  5 of 5 gates  cycle 3 of {found['good']}" in recorded
    # One pair: every gate by group, its convergence, its points and, on demand, the precheck.
    pair = readiness("pair", "unit-other", "--precheck", "--bucket", "cycle").stdout.splitlines()
    assert pair[0] == gates.DEFINITION and pair[1].startswith("unit-other 1.0.0 (spec ")
    assert pair[2].startswith("parity  population  met: cycle 1 of run ") and len(pair) == 2 + 12 + 2
    assert pair[-2] == "convergence: 0 of 3 evidence cycles in a row"
    assert re.fullmatch(
        rf"{today}-\S+  cycle 1 of {found['other']}  spec \d+  census  2 different, 0 unresolved,"
        r" 0 suppressed of 1,000  fails: targets",
        pair[-1],
    ), pair[-1]
    detail = json.loads(readiness("pair", "unit-runner", "--json").stdout)
    assert detail["state"] == "parity_ready" and [p["cycle"] for p in detail["cycles"]] == [1, 2, 3]
    assert "precheck" not in detail
    assert sql(pg_dsn, "SELECT count(*) AS n FROM ds.audit_log") == before  # reads are not audited
    # Here too: no percentage but the targets gate's, and nothing forecast, weighted or certified.
    from tests.unit.api.test_readiness_api import BANNED, SCHEDULE

    said_not = [line for line in (*lines, *recorded, *pair) if line != gates.DEFINITION]
    assert sum("pooled 0.1% target" in line for line in said_not) == 2
    assert [
        line
        for line in said_not
        if "pooled 0.1% target" not in line and BANNED.search(line.replace(SCHEDULE, ""))
    ] == []


def _settings(config: Path) -> Any:
    from deltasleuth.config import load_settings

    return load_settings(config)


@pytest.mark.db
def test_readiness_check_prints_each_pair_s_gates_with_their_sources(
    store: Callable[..., Path], pg_dsn: str
) -> None:
    from deltasleuth.core import readiness as gates

    settings = {
        "gates": {"fresh": {"max_age": "14d"}},
        "programmes": [
            READINESS["programmes"][0]
            | {
                "pairs": [
                    {"spec": "unit-runner", "entity": "item", "gates": {"tier": "high"}},
                    {"spec": "unit-none", "closed": "2026-11-02"},
                ]
            }
        ],
    }
    config = store(readiness=settings)
    asyncio.run(_ready(config))
    checked = run("readiness", "check", "--config", config)
    assert checked.exit_code == 0 and checked.stdout.splitlines() == [
        "unit-none (programme cutover-q4, closed 2026-11-02): tier standard (default), convergence 2 cycles"
        " (programme), fresh 14d (installation), graph not required (default), accuracy not required"
        " (default)",
        "  deviation: convergence 2 cycles (default 3; relaxed)",
        "  deviation: fresh 14d (default 7d; relaxed)",
        "unit-other: tier standard (default), convergence 3 cycles (default), fresh 14d (installation),"
        " graph not required (default), accuracy not required (default)",
        "  deviation: fresh 14d (default 7d; relaxed)",
        "unit-runner (programme cutover-q4, entity item): tier high (pair), convergence 2 cycles (programme),"
        " fresh 14d (installation), graph not required (default), accuracy not required (default)",
        "  deviation: tier high (default standard)",
        "  deviation: convergence 2 cycles (default 3; relaxed)",
        "  deviation: fresh 14d (default 7d; relaxed)",
        f"readiness settings {gates.config_sha256(_settings(config).readiness)}",
    ]
    assert [line for line in checked.stderr.splitlines() if line.startswith("warning:")] == [
        "warning: programme cutover-q4 names spec unit-none, which is not stored"
    ]


async def _passed(config: Path, end: str | None = None) -> None:
    """End the soak ``end``'s next cycle, which no pass measures; or, without one, a worker's pass."""
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from deltasleuth.runner import readiness as watching
    from tests.unit.runner.conftest import Harness
    from tests.unit.runner.measuring import MATCH, ended

    async with Context.open(load_settings(config)) as ctx:
        if end is None:
            await watching.watch(ctx, watching.State())
        else:
            await ended(Harness(ctx), end, MATCH)


@pytest.mark.db
def test_readiness_verify_checks_the_measures_against_their_audit_rows(
    store: Callable[..., Path], pg_dsn: str
) -> None:
    """``ds readiness verify``: it passes on a store with a one-shot's excluded marker and the error
    record of a cycle never measured; it exits 2 on an edited record, on a cycle a ``readiness.measured``
    row names whose measures were replaced (by an error record, an excluded marker or a record without a
    pair), removed, or removed and measured again, on measures no row names, on a record that is neither
    measures nor a marker and on a forged excluded marker."""
    config = store()
    found = asyncio.run(_ready(config))
    asyncio.run(_passed(config, end=found["other"]))  # its cycle 2: ended, never measured
    good, other = (found["good"], 3), (found["other"], 2)
    error = {
        "format": 1, "pair": "unit-other", "spec_id": 2, "error": "KeyError", "attempts": 1,
        "fails": {"standard": ["unknown"], "high": ["unknown"]}, "measured_at": "2026-10-03T04:00:00.000000Z",
    }  # fmt: skip
    edit = "UPDATE ds.cycles SET readiness = %s::jsonb WHERE run_id = %s AND cycle = %s"
    kept = sql(pg_dsn, "SELECT run_id, cycle, readiness FROM ds.cycles ORDER BY run_id, cycle")
    by = {(row["run_id"], row["cycle"]): row["readiness"] for row in kept}
    assert by[found["once"], 1] == {"format": 1, "excluded": "one_shot"} and by[other] is None
    sql(pg_dsn, edit, json.dumps(error), *other)  # its measure failed: no row names it

    def verify() -> tuple[int, str]:
        return said(run("readiness", "verify", "--config", config))

    passes = (
        0,
        "readiness: 4 measured cycle(s) match their readiness.measured rows; 1 excluded, 1 not measured\n",
    )
    assert verify() == passes
    (seq,) = {
        row["seq"]
        for row in sql(
            pg_dsn,
            "SELECT c.seq FROM ds.audit_chain c JOIN ds.audit_log l USING (id) WHERE l.action = %s",
            "readiness.measured",
        )
    }
    altered = (
        2,
        f"error: cycle 3 of run {found['good']} does not hash to its readiness.measured row (seq {seq}):"
        " its measures were altered after they were written\n",
    )
    measured, marker = by[good], {"format": 1, "excluded": "captured_samples"}
    # A cycle a row names holds the measures it hashed: nothing else, and not nothing.
    for forged in (measured | {"different": 1}, error | {"pair": "unit-runner", "final": True}, marker, None):
        sql(pg_dsn, edit, None if forged is None else json.dumps(forged), *good)
        assert verify() == altered, forged
    sql(pg_dsn, edit, json.dumps(measured), *good)
    assert verify() == passes  # as written, it verifies again
    sql(pg_dsn, edit, json.dumps(by[found["other"], 1] | {"pair": "unit-other"}), found["once"], 1)
    assert verify() == (
        2,
        f"error: cycle 1 of run {found['once']} has measures no readiness.measured row names\n",
    )
    sql(pg_dsn, edit, json.dumps({"format": 1, "excluded": "one_shot"}), found["once"], 1)
    sql(pg_dsn, edit, json.dumps({"format": 1, "excluded": "one_shot"}), *other)
    assert verify() == (
        2,
        f"error: cycle 2 of run {found['other']} is marked excluded (one_shot) but its run is a soak\n",
    )
    sql(pg_dsn, edit, json.dumps(marker), *other)
    assert verify() == (
        2,
        f"error: cycle 2 of run {found['other']} is marked excluded (captured_samples) but its run read no"
        " captured samples\n",
    )
    # A record that is neither measures nor a marker: the column refuses it, and so does verify once the
    # database's owner has dropped that check; over a named cycle it is an altered measure.
    with pytest.raises(psycopg.errors.CheckViolation, match="cycles_readiness_check"):
        sql(pg_dsn, edit, json.dumps({"format": 1}), *other)
    sql(pg_dsn, "ALTER TABLE ds.cycles DROP CONSTRAINT cycles_readiness_check")
    sql(pg_dsn, edit, json.dumps({"format": 1}), *other)
    assert verify() == (
        2,
        f"error: cycle 2 of run {found['other']} holds a record that is neither measures nor an excluded"
        " marker\n",
    )
    sql(pg_dsn, edit, json.dumps({"format": 1}), *good)
    assert verify() == altered
    # Measures removed are measured again by the worker's next pass, from what the cycle then holds: the
    # cycle is named twice, which the worker never does.
    sql(pg_dsn, edit, json.dumps(error), *other)
    sql(pg_dsn, edit, None, *good)
    asyncio.run(_passed(config))
    [again] = sql(pg_dsn, "SELECT readiness FROM ds.cycles WHERE run_id = %s AND cycle = %s", *good)
    assert again["readiness"] | {"measured_at": None} == measured | {"measured_at": None}
    assert verify() == (
        2,
        f"error: cycle 3 of run {found['good']} is named by 2 readiness.measured rows: its measures were"
        " removed and measured again\n",
    )


@pytest.mark.db
def test_readiness_report_writes_the_reports_audited(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth.core import readiness as gates
    from deltasleuth.reports import readiness as reports

    monkeypatch.setenv("DS_OPERATOR", "ops")
    config = store({"public_url": "https://ds.example"}, readiness=READINESS)
    asyncio.run(_ready(config))
    sha256 = gates.config_sha256(_settings(config).readiness)

    def report(out: str, *args: Any) -> Result:
        return run("readiness", "report", "--out", tmp_path / out, *args, "--config", config)

    whole = report("whole")
    names = ["dashboard-1.pdf", "dashboard.csv", "trends.csv"]
    assert whole.exit_code == 0 and whole.stdout.splitlines() == [str(tmp_path / "whole" / n) for n in names]
    assert (tmp_path / "whole" / "dashboard-1.pdf").read_bytes().startswith(b"%PDF-")
    table = (tmp_path / "whole" / "dashboard.csv").read_text().splitlines()
    assert table[0] == ",".join(reports.DASHBOARD) and len(table) == 1 + 3 * 12
    assert len((tmp_path / "whole" / "trends.csv").read_text().splitlines()) == 1 + 4
    rows = audited(pg_dsn, "report.download")
    assert {(r["actor"][:10], r["object"]) for r in rows} == {("local:ops@", "readiness")}
    assert [(r["detail"]["name"], r["detail"]["programme"], r["detail"]["config_sha256"]) for r in rows] == [
        (name, None, sha256) for name in names
    ]
    # A programme's alone; over its rows the trends are a file a pair; more pairs than a volume holds, split.
    monkeypatch.setattr(reports, "TRENDS_ROWS", 2)
    monkeypatch.setattr(reports, "VOLUME", 1)
    split = report("split", "--programme", "cutover-q4")
    assert [Path(line).name for line in split.stdout.splitlines()] == [
        "dashboard-1.pdf",
        "dashboard-2.pdf",
        "dashboard.csv",
        "trends-unit-none.csv",
        "trends-unit-runner.csv",
    ]
    assert len((tmp_path / "split" / "trends-unit-runner.csv").read_text().splitlines()) == 1 + 3
    assert {r["detail"]["programme"] for r in audited(pg_dsn, "report.download")[3:]} == {"cutover-q4"}
    assert failed(report("none", "--programme", "cutover-q9")) == (
        2,
        ["error: programme cutover-q9 is not configured"],
    )
    monkeypatch.delenv("DS_OPERATOR")
    assert failed(report("refused")) == (2, [f"error: {OPERATOR}"])
    assert len(audited(pg_dsn, "report.download")) == 8 and not (tmp_path / "refused").exists()


@pytest.mark.db
def test_readiness_report_pair(
    store: Callable[..., Path], pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ds readiness report --pair``: the pair's brief on one page, its points
    and every root-cause group of it, more than a brief lists or a download holds, in place of the
    dashboard's files; each audited with the pair."""
    from deltasleuth import readiness
    from deltasleuth.config import load_settings
    from deltasleuth.context import Context
    from deltasleuth.core import readiness as gates
    from deltasleuth.reports import readiness as reports
    from tests.unit.api.test_readiness_api import _grouped

    monkeypatch.setenv("DS_OPERATOR", "ops")
    config = store({"public_url": "https://ds.example"}, readiness=READINESS)
    found = asyncio.run(_ready(config))

    async def grouped() -> None:
        async with Context.open(load_settings(config)) as ctx:
            for cycle, groups in ((1, {"a": 1, "b": 1}), (3, {"a": 1, "c": 1})):
                await _grouped(ctx, found["good"], cycle, groups)

    def report(out: str, *args: Any) -> Result:
        return run("readiness", "report", "--out", tmp_path / out, *args, "--config", config)

    asyncio.run(grouped())
    monkeypatch.setattr(readiness, "BRIEF_CAUSES", 1)
    monkeypatch.setattr(reports, "CAUSES_ROWS", 1)
    written, out = report("pair", "--pair", "unit-runner"), tmp_path / "pair"
    names = ["brief-unit-runner.pdf", "brief-unit-runner.csv", "causes-unit-runner.csv"]
    assert written.exit_code == 0 and written.stdout.splitlines() == [str(out / name) for name in names]
    pdf = (out / names[0]).read_bytes()
    assert pdf.startswith(b"%PDF-") and len(re.findall(rb"/Type /Page\b", pdf)) == 1
    points = (out / names[1]).read_text().splitlines()
    assert points[0] == ",".join(reports.BRIEF)
    assert [line.split(",")[:3] for line in points[1:]] == [
        ["unit-runner", found["good"], str(cycle)] for cycle in (1, 2, 3)
    ]
    causes = (out / names[2]).read_text().splitlines()
    assert causes[0] == ",".join(reports.CAUSES) and [line.split(",")[1:3] for line in causes[1:]] == [
        ["RC-aaaaaaaaaaaa", "still_open"], ["RC-cccccccccccc", "opened"], ["RC-bbbbbbbbbbbb", "closed"],
    ]  # fmt: skip
    sha256 = gates.config_sha256(_settings(config).readiness)
    rows = audited(pg_dsn, "report.download")
    assert {(r["actor"][:10], r["object"]) for r in rows} == {("local:ops@", "readiness")}
    assert [tuple(r["detail"][n] for n in ("name", "pair", "programme", "config_sha256")) for r in rows] == [
        (name, "unit-runner", None, sha256) for name in names
    ]
    assert failed(report("none", "--pair", "customer-x")) == (
        2,
        ["error: pair customer-x not found: no spec or readiness setting names it"],
    )
    assert failed(report("both", "--pair", "unit-runner", "--programme", "cutover-q4")) == (
        2,
        ["error: --pair and --programme cannot be given together"],
    )
    assert len(audited(pg_dsn, "report.download")) == 3
    assert not (tmp_path / "none").exists() and not (tmp_path / "both").exists()


# -- schedules -------------------------------------------------------------

NEXT = r"\d{4}-\d\d-\d\d %s Europe/London \(\d{4}-\d\d-\d\dT\d\d:%sZ\)"


def test_schedules_next_prints_a_cron_s_times_without_settings_or_a_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deltasleuth.core import cron

    monkeypatch.delenv("DS_CONFIG", raising=False)
    monkeypatch.setattr(cron, "tzdata", lambda: "2026c")
    found = run("schedules", "next", " 30  1 * * *", "--tz", "Europe/London", "--count", "3")
    lines = found.stdout.splitlines()
    assert found.exit_code == 0 and len(lines) == 3
    assert all(re.fullmatch(NEXT % ("01:30", "30"), line) for line in lines), lines
    assert found.stderr.splitlines() == [  # each time a clock change moves, in the year ahead
        f"warning: {note}" for note in cron.notes("30 1 * * *", "Europe/London", datetime.now(UTC))
    ]
    assert len(found.stderr.splitlines()) == 2 and "(clocks go back)" in found.stderr
    assert len(run("schedules", "next", "0 2 * * *", "--tz", "UTC").stdout.splitlines()) == 10
    offset = run("schedules", "next", "0 2 * * *", "--tz", "Etc/GMT+5", "--count", "1")
    assert (offset.exit_code, offset.stderr) == (
        0,  # a zone named by its offset: what it costs the schedule's runs
        "warning: the name of 'Etc/GMT+5' holds a number: under a key pattern that matches a short number,"
        " the runs of a schedule in it get no evidence pack: use a region's zone, or UTC\n",
    )
    refusals = [
        said(run("schedules", "next", text, "--tz", zone))
        for text, zone in (
            ("0 2 * * * *", "UTC"),
            ("0 2 * * *", "Asia/Calcutta"),
            ("0 2 * * *", "GMT"),
            ("0 2 * * \u017fun", "UTC"),  # U+017F upper-cases to S: no weekday's name
        )
    ]
    assert refusals == [
        (2, "error: cron must have 5 fields (minute hour day-of-month month day-of-week), not 6\n"),
        (2, "error: 'Asia/Calcutta' is not a time zone this build uses (tzdata 2026c): use Asia/Kolkata\n"),
        (2, "error: time_zone must be UTC or an IANA region name such as Europe/London: 'GMT' is not one\n"),
        (2, "error: cron field day-of-week: '\u017fun' is not *, a number, a range a-b, a list a,b or a step"
            " */n or a-b/n\n"),
    ]  # fmt: skip
    ends = (
        "No time is due after this ISO-8601 time with a UTC offset; a run for the last time before it may"
        " still start within its window."
    )
    for command in ("add", "set"):  # --ends bounds the due times, not when the last one's run starts
        told = " ".join(run("schedules", command, "--help").output.replace("│", " ").split())
        assert ends in told and "No run starts after" not in told


@pytest.mark.db
def test_schedules_are_declared_listed_and_steered_as_the_operator(
    store: Callable[..., Path], pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deltasleuth import schedules as declared

    monkeypatch.setenv("DS_OPERATOR", "ops")
    config = store()
    spec_id = asyncio.run(_upload(config, APPROVED))

    def schedules(*args: Any) -> Result:
        return run("schedules", *args, "--config", config)

    empty, none_active = schedules(), schedules("pause", "--all")
    nightly = ("--spec-id", spec_id, "--cron", " 0  2 * * *", "--tz", "Europe/London")
    added = schedules("add", "nightly", *nightly)
    schedule_id = added.stdout[6:35]
    weekly = schedules(
        "add",
        "weekly",
        "--spec",
        "cli-approvals",
        "--cron",
        "30 3 * * MON",
        "--tz",
        "UTC",
        "--start-within",
        30,
    )
    other = weekly.stdout[6:35]
    refusals = [
        failed(schedules("add", *args))
        for args in (
            ("nightly", *nightly),
            ("both", *nightly, "--spec", "cli-approvals"),
            ("fast", "--spec-id", spec_id, "--cron", "*/5 2 * * *", "--tz", "UTC"),
            ("soak", "--spec-id", spec_id, "--soak", "--cron", "0 2 * * *", "--tz", "UTC"),
            ("rolling", "--spec-id", spec_id, "--rolling", "--cron", "0 2 * * *", "--tz", "UTC"),
            ("ended", *nightly, "--ends", "2020-01-01T00:00:00+00:00"),
            ("never", *nightly, "--ends", "9999-12-31T23:59:59-12:00"),  # the year 10000 in UTC
            ("zones", *nightly),
            ("unknown", "--spec-id", 99, "--cron", "0 2 * * *", "--tz", "UTC"),
        )
    ]
    listed, as_json = schedules(), json.loads(schedules("--json").stdout)
    filtered = [
        schedules(*args).stdout.splitlines()[len(declared.NOT) :]
        for args in (("--spec", "nope"), ("--state", "paused"), ("--set-by", "user:U-0123456789abcdef"))
    ]
    lost, misplaced = schedules("--state", "lost"), run("schedules", "--json", "show", "nightly")
    shown, shown_json = (
        schedules("show", "nightly"),
        json.loads(schedules("show", schedule_id, "--json").stdout),
    )
    sql(
        pg_dsn,
        "UPDATE ds.schedules SET anchor_at = anchor_at - interval '2 days', next_due = next_due - interval"
        " '2 days' WHERE schedule_id = %s",
        schedule_id,
    )
    overdue = schedules("show", "nightly").stdout.splitlines()  # two days late: its next times are from now
    saved = schedules("set", "nightly", "--cron", "15 4 * * *", "--start-within", 45)  # a time was due
    every = schedules("pause", "--all")
    again, both, neither = (
        schedules("pause", "nightly"),
        schedules("pause", "nightly", "--all"),
        schedules("pause"),
    )
    while_paused = schedules("set", schedule_id, "--spec", "cli-approvals")
    resumed, twice = schedules("resume", "nightly"), schedules("resume", "nightly")
    paused, deleted = schedules("pause", "weekly"), schedules("delete", "weekly")
    gone = [failed(schedules(act, "weekly")) for act in ("show", "set", "pause", "resume", "delete")]
    rows = sql(
        pg_dsn,
        "SELECT actor, action, object, detail FROM ds.audit_log WHERE action LIKE 'schedule.%%' ORDER BY id",
    )

    heading = [*declared.NOT, "no schedules"]
    assert said(empty) == (0, "\n".join(heading) + "\n") and said(none_active) == (0, "no active schedules\n")
    assert re.fullmatch(
        rf"added SC-[0-9A-Z]{{26}} \(nightly\): next {NEXT % ('02:00', '00')}\n", added.stdout
    )
    assert [line for line in added.stderr.splitlines() if line.startswith(("warning:", "error:"))] == [
        "warning: developer mode has no approvers: its runs are labelled unapproved"
    ]
    assert re.fullmatch(
        r"added SC-[0-9A-Z]{26} \(weekly\): next \d{4}-\d\d-\d\d 03:30 UTC \(\d{4}-\d\d-\d\dT03:30Z\)\n",
        weekly.stdout,
    )
    assert refusals == [
        (2, [f"error: a schedule named nightly exists already ({schedule_id})"]),
        (2, ["error: give spec_id (a pinned version) or spec_name (the newest version at each firing), not"
             " both"]),
        (2, ["error: cron fires at 02:00 and 02:05: a schedule fires at most every 15 minutes (a soak's"
             " execution.cycleInterval repeats faster)"]),
        (2, ["error: a soak run needs the spec's execution.cycleInterval"]),
        (2, ["error: mode must be batch or soak, and only a soak run takes soak settings"]),
        *[(2, ["error: ends_at must be an ISO-8601 time with a UTC offset, later than now"])] * 2,
        (2, ["error: name must not be zones: /v1/schedules/zones is a route of the API's own"]),
        (2, ["error: spec 99 not found"]),
    ]  # fmt: skip
    lines = listed.stdout.splitlines()
    assert lines[: len(declared.NOT)] == list(declared.NOT) and len(lines) == len(declared.NOT) + 2
    version = f"1.0.0 (spec {spec_id})"
    assert re.fullmatch(
        rf"{other}  weekly  follows cli-approvals: {re.escape(version)} now  batch  30 3 \* \* MON UTC  next"
        r" \S+ 03:30 UTC \(\S+T03:30Z\)",
        lines[-2],
    ), lines[-2]
    assert re.fullmatch(
        rf"{schedule_id}  nightly  pinned {re.escape(version)}  batch  0 2 \* \* \* Europe/London  next"
        rf" {NEXT % ('02:00', '00')}",
        lines[-1],
    ), lines[-1]
    assert as_json.keys() == {"not", "tzdata", "overdue", "banners", "items", "next_cursor"}
    assert [item["name"] for item in as_json["items"]] == ["weekly", "nightly"] and as_json["overdue"] == 0
    assert filtered == [["no schedules"]] * 3
    assert said(lost) == (2, "error: --state is one of active, paused, ended, deleted\n")
    assert said(misplaced) == (2, "error: put the options after 'show'\n")
    detail = shown.stdout.splitlines()
    assert detail[0] == lines[-1] and re.fullmatch(
        r"revision 1, saved by local:ops@\S+ \S+ \(none\); starts within 60 min", detail[1]
    )
    assert detail[2] == "approval: unapproved" and len(detail) == 8
    assert all(re.fullmatch("due " + NEXT % ("02:00", "00"), line) for line in detail[3:])
    assert (shown_json["schedule_id"], len(shown_json["upcoming"]), shown_json["firings"]) == (
        schedule_id,
        5,
        [],
    )
    assert [line[-18:-1] > f"{datetime.now(UTC):%Y-%m-%dT%H:%MZ}" for line in overdue[3:]] == [True] * 5
    assert all(re.fullmatch("due " + NEXT % ("02:00", "00"), line) for line in overdue[3:])
    assert re.fullmatch(rf"saved {schedule_id}: next {NEXT % ('04:15', '15')}\n", saved.stdout)
    assert said(every) == (0, "paused 2 schedules\n")
    assert failed(again) == (2, [f"error: schedule {schedule_id} is paused already"])
    assert failed(both) == failed(neither) == (2, ["error: name a schedule, or pass --all"])
    assert said(while_paused) == (0, f"saved {schedule_id}: paused\n")  # a paused one stays paused
    assert re.fullmatch(rf"resumed {schedule_id}: next {NEXT % ('04:15', '15')}\n", resumed.stdout)
    assert failed(twice) == (2, [f"error: schedule {schedule_id} is not paused"])
    assert failed(paused) == (2, [f"error: schedule {other} is paused already"])
    assert said(deleted) == (0, f"deleted {other}\n")
    assert gone == [(2, ["error: schedule weekly not found"])] * 5
    assert {row["actor"][:10] for row in rows} == {"local:ops@"}  # each its caller's own row
    assert [(row["action"].removeprefix("schedule."), row["object"] == schedule_id) for row in rows] == [
        ("create", True), ("create", False),
        ("missed", True), ("update", True),  # the time that was due, before the save's own row
        ("pause", False), ("pause", True),
        ("update", True), ("resume", True), ("delete", False),
    ]  # fmt: skip
    missed, update, followed = rows[2]["detail"], rows[3]["detail"], rows[6]["detail"]
    assert (missed["reason"], missed["code"], missed["count"]) == ("superseded", "update", 2)
    assert (update["cron"], update["start_within"], update["changed"]) == (
        "15 4 * * *",
        45,
        ["cron", "start_within"],
    )
    assert (followed["spec_id"], followed["spec_name"], followed["changed"]) == (
        None,
        "cli-approvals",
        ["spec_id"],
    )
    last = json.loads(schedules("show", "nightly", "--json").stdout)["last"]["text"]
    assert schedules("show", "nightly").stdout.splitlines()[-1].endswith(f"  {last}")
    assert re.fullmatch(
        r"not started: 2 times, \S+ to \S+, were due and not started when the schedule was saved again", last
    )


def test_a_bug_in_a_schedules_command_exits_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def broken(*_: Any, **__: Any) -> None:
        raise KeyError("next_due")

    monkeypatch.setattr("deltasleuth.schedules.preview", broken)
    found = run("schedules", "next", "0 2 * * *", "--tz", "UTC")
    assert found.exit_code == 3 and "internal error: a bug in DeltaSleuth" in found.stderr


# -- table pre-checks -------------------------------------------------------

NOT = (
    "A table pre-check counts and adds up; it does not compare rows. Tables whose figures agree can hold"
    " different rows, and figures can differ only because two databases store a value differently. It says"
    " where to look first. It is observed, not evidence, readiness or a sign-off, and no evidence pack"
    " contains it: run a comparison to compare rows."
)
PC = r"PC-[0-9A-Z]{26}"
BEST = "no difference observed in the checks made (level {}): not parity; run a comparison to compare rows"


@pytest.fixture
def tables(store: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``config, engine = tables(**settings)``: a store whose targets db-a and db-b are databases that an
    engine in memory answers for, with a table of each of accounts, cards and loans."""
    columns = (prechecking.column("email"), prechecking.column("balance", "bigint", "integer"))
    engine = prechecking.FakeEngine(
        *(prechecking.relation(name, *columns) for name in ("accounts", "cards", "loans"))
    )
    monkeypatch.setitem(seam.ENGINES, "postgresql", engine)
    monkeypatch.setenv("DS_OPERATOR", "ops")

    def make(**settings: Any) -> tuple[Path, prechecking.FakeEngine]:
        config = store()
        (tmp_path / "t.yaml").write_text(prechecking.database_targets("/run/secrets/unused"))
        config.write_text(yaml.safe_dump(yaml.safe_load(config.read_text()) | settings))
        return config, engine

    return make


@pytest.mark.db
@pytest.mark.usefixtures("vendor_keys")
def test_a_table_pre_check_is_run_shown_reported_and_cancelled(
    tables: Any, tmp_path: Path, pg_dsn: str
) -> None:
    config, engine = tables()
    engine.values["db-b", "rows", None] = "5"
    (tmp_path / "accounts.json").write_text(prechecking.table_spec())
    (tmp_path / "cards.json").write_text(prechecking.table_spec("cards"))

    def precheck(*args: Any) -> Result:
        return run("precheck", *args, "--config", config)

    empty = precheck()
    ran = precheck("run", tmp_path / "accounts.json", tmp_path / "cards.json", "--level", "counts")
    precheck_id = re.search(PC, ran.stdout)[0]  # type: ignore[index]
    listed, shown = precheck(), precheck("show", precheck_id)
    as_json = json.loads(precheck("show", precheck_id, "--json").stdout)
    only = precheck("show", precheck_id, "--verdict", "no_difference_observed")
    pair = precheck("show", precheck_id, "--pair", 1)
    pair_json = precheck("show", precheck_id, "--pair", 1, "--json")
    report = precheck("report", precheck_id, "--out", tmp_path / "out")
    by_name = precheck("run", "--spec", "accounts", "--no-wait", "--level", "schema")
    waiting = re.search(PC, by_name.stdout)[0]  # type: ignore[index]
    cancelled, again = precheck("cancel", waiting), precheck("cancel", waiting)
    refusals = [
        precheck("run"),
        precheck("run", "--all-specs", "--spec", "accounts"),
        precheck("run", "--all-specs", "--level", "rows"),
        precheck("run", "--spec-id", 99),
        precheck("run", "--spec", "customer-profile"),
        precheck("run", "--all-specs", "--no-wait", "--fail-on", "differs"),
        precheck("run", "--all-specs", "--fail-on", "fine"),
        precheck("show", precheck_id, "--verdict", "fine"),
        precheck("show", precheck_id, "--pair", 7),
        precheck("show", "PC-" + "0" * 26),
        run("precheck", "--json", "show", precheck_id),
        precheck("draft", precheck_id, "--all", "--out", tmp_path / "drafts"),  # stored specs only
    ]
    made = rf"{PC}  specs  level counts  by local:ops@\S+ \S+  finished"
    rows = "rows differ: side a 3, side b 5 (2 fewer on side a)"
    lines = ran.stdout.splitlines()
    assert said(empty) == (0, f"{NOT}\nno table pre-checks\n") and ran.exit_code == 0
    assert lines[:2] == [NOT, "Unlicensed evaluation"] and re.fullmatch(made, lines[2])
    assert lines[3:] == ["2 pairs: 2 with differing row counts", f"1  accounts  {rows}", f"2  cards  {rows}"]
    assert shown.stdout == ran.stdout and only.stdout.splitlines() == lines[:4]
    assert listed.stdout.splitlines()[0] == NOT and re.fullmatch(
        made + "  Unlicensed evaluation", listed.stdout.splitlines()[1]
    )
    assert listed.stdout.splitlines()[2] == "  2 pairs: 2 with differing row counts"
    assert (as_json["precheck_id"], as_json["not"], len(as_json["items"])) == (precheck_id, NOT, 2)
    detail = pair.stdout.splitlines()
    assert detail[:4] == [
        NOT,
        "Unlicensed evaluation",
        f"pair 1 of {precheck_id}: accounts (db-a accounts, db-b accounts): rows differ",
        rows,
    ]
    assert detail[-3].endswith(": not read at one moment") and detail[-2:] == [
        "  balance: not compared: level counts", "  email: not compared: level counts",
    ]  # fmt: skip
    assert json.loads(pair_json.stdout)["result"]["keys"]["b"]["rows"] == 5
    written = [f"{tmp_path / 'out'}/{precheck_id}-unlicensed-evaluation-{name}" for name in (
        "precheck.csv", "precheck-columns.csv", "precheck.pdf"
    )]  # fmt: skip
    assert said(report) == (0, "".join(f"{path}\n" for path in written))
    assert Path(written[0]).read_bytes().decode().startswith(f'"{NOT}"\r\nUnlicensed evaluation\r\n')
    assert Path(written[2]).read_bytes().startswith(b"%PDF")
    waited = by_name.stdout.splitlines()
    assert by_name.exit_code == 0 and waited[3:] == [
        "1 pair: 1 pending", "1  accounts  pending", "read by 'ds worker' or 'ds serve'",
    ]  # fmt: skip
    assert said(cancelled) == (0, f"cancelled {waiting}\n")
    assert [(r.exit_code, r.stderr.splitlines()[-1]) for r in (again, *refusals)] == [
        (2, f"error: table pre-check {waiting} has ended"),
        (2, "error: give spec_ids, spec_names or all_specs, and only one of them"),
        (2, "error: give spec_ids, spec_names or all_specs, and only one of them"),
        (2, "error: level must be schema, counts, columns or distinct"),
        (2, "error: spec 99 not found"),
        (2, "error: no spec is named customer-profile"),
        (2, "error: --fail-on waits for the pre-check's end: leave out --no-wait"),
        (2, "error: --fail-on is one of refused, differs, incomplete"),
        (2, f"error: --verdict is one of {', '.join(prechecking_verdicts())}"),
        (2, f"error: pair 7 of table pre-check {precheck_id} not found"),
        (
            2,
            f"error: table pre-check PC-{'0' * 26} not found (pre-checks are deleted 90 days after they are"
            " made)",
        ),
        (2, "error: put the options after 'show'"),
        (2, f"error: no pair of {precheck_id} can be drafted: none is two tables paired by name and read"),
    ]
    assert not (tmp_path / "drafts").exists()
    own = sql(pg_dsn, "SELECT action FROM ds.audit_log WHERE actor LIKE 'local:ops@%%' ORDER BY id")
    assert [row["action"] for row in own] == [
        "spec.create", "spec.create", "precheck.create", "report.download", "report.download",
        "report.download", "precheck.create", "precheck.cancel",
    ]  # fmt: skip
    assert [row["detail"]["name"] for row in audited(pg_dsn, "report.download")] == [
        "precheck.csv", "precheck-columns.csv", "precheck.pdf"
    ]  # fmt: skip
    never = r"\b(match|matches|matched|identical|passed|verified|validated|reconciled)\b|[✓✔%]"
    assert not re.search(never, ran.stdout + pair.stdout + listed.stdout, re.I)


@pytest.mark.db
def test_two_schemas_are_pre_checked_by_name_and_a_pair_drafted(
    tables: Any, tmp_path: Path, pg_dsn: str
) -> None:
    config, engine = tables()
    engine.listed = {
        "db-a": [("CARDS", "view"), ("Cards", "table"), ("accounts", "table"), ("ledger", "table")]
    }

    def precheck(*args: Any) -> Result:
        return run("precheck", *args, "--config", config)

    ran = precheck("run", "--a", "db-a:app", "--b", "db-b", "--exclude", "ledger*", "--fail-on", "differs")
    precheck_id = re.search(PC, ran.stdout)[0]  # type: ignore[index]
    one = precheck("draft", precheck_id, "--pair", 1, "--out", tmp_path / "drafts")
    every = precheck("draft", precheck_id, "--all", "--out", tmp_path / "all")
    refusals = [
        precheck("run", "--a", "db-a"),
        precheck("run", "--a", "db-a", "--b", "db-b", "--all-specs"),
        precheck("run", "--all-specs", "--include", "a*"),
        precheck("run", "--a", "db-a", "--b", "db-b", "--level", "distinct"),
        precheck("run", "--a", "nope", "--b", "db-b"),
        precheck("draft", precheck_id, "--out", tmp_path / "x"),
        precheck("draft", precheck_id, "--pair", 2, "--out", tmp_path / "x"),
    ]
    lines = ran.stdout.splitlines()
    assert ran.exit_code == 1  # an unpaired name fails --fail-on differs
    assert re.fullmatch(rf"{PC}  catalogue  level counts  by local:ops@\S+ \S+  finished", lines[1])
    assert lines[2:] == [
        "3 pairs: 2 unpaired, 1 with no difference observed (not parity)",
        "2  cards  two tables of side a are named alike ignoring case (CARDS, Cards): compare them in a spec",
        "3  loans  no table on side a is named loans (side b: loans; paired by name)",
        f"1  accounts  {BEST.format('counts')}",
    ]
    drafted = tmp_path / "drafts" / f"{precheck_id}-pair-1.draft.yaml"
    assert said(one) == (0, f"{drafted}\n") and said(every)[1] == f"{tmp_path / 'all' / drafted.name}\n"
    text = drafted.read_text()
    assert '"accounts", paired by name' in text and "keyColumns: [id]" in text and "name: accounts" in text
    assert [(r.exit_code, r.stderr.splitlines()[-1]) for r in refusals] == [
        (2, "error: --a and --b come together, and without a spec"),
        (2, "error: --a and --b come together, and without a spec"),
        (2, "error: --include and --exclude take --a and --b"),
        (
            2,
            "error: two schemas paired by name take level schema, counts or columns: distinct counts need a"
            " stored spec",
        ),
        (2, "error: target nope is not declared"),
        (2, "error: give --pair N or --all, and only one of them"),
        (2, "error: pair 2 has no table on side a: there is nothing to draft"),
    ]
    own = sql(pg_dsn, "SELECT action, detail FROM ds.audit_log WHERE action = 'precheck.create'")
    assert own[0]["detail"]["a"] == {"target": "db-a", "schema": "app"}


def prechecking_verdicts() -> tuple[str, ...]:
    from deltasleuth.core import precheck

    return precheck.VERDICTS


@pytest.mark.db
def test_fail_on_differs_exits_1_when_a_side_was_not_read(tables: Any, tmp_path: Path) -> None:
    """A control must not pass on no data: a pair whose side was unreachable or refused the credential is
    not read, which --fail-on differs fails; --fail-on refused lets it through."""
    config, engine = tables(worker={"lease_seconds": 1, "poll_seconds": 0.05})
    (tmp_path / "accounts.json").write_text(prechecking.table_spec())
    first = run("precheck", "run", tmp_path / "accounts.json", "--level", "schema", "--config", config)
    assert first.exit_code == 0
    engine.fail("snapshot", "db-b", TargetUnreachable("ORA-12541"), always=True)
    unreachable = run("precheck", "run", "--spec", "accounts", "--fail-on", "differs", "--config", config)
    engine.fail("snapshot", "db-b", seam.TargetAuthRefused("ORA-01017"), always=True)
    refused = run("precheck", "run", "--spec", "accounts", "--fail-on", "differs", "--config", config)
    lenient = run("precheck", "run", "--spec", "accounts", "--fail-on", "refused", "--config", config)
    unasked = run("precheck", "run", "--spec", "accounts", "--config", config)
    assert [r.exit_code for r in (unreachable, refused, lenient, unasked)] == [1, 1, 0, 0]
    assert unreachable.stdout.splitlines()[-2:] == [
        "1 pair: 1 not read", "1  accounts  not checked: side b could not be reached (ORA-12541)",
    ]  # fmt: skip
    assert refused.stdout.splitlines()[-1] == (
        "1  accounts  not checked: side b refused the credential (ORA-01017); no other pair of target db-b"
        " was tried"
    )


@pytest.mark.db
def test_fail_on_incomplete_exits_1_on_a_limit_skip(tables: Any, tmp_path: Path) -> None:
    config, _ = tables(precheck={"distinct_max_rows": 2})
    (tmp_path / "accounts.json").write_text(prechecking.table_spec())
    distinct = ("precheck", "run", tmp_path / "accounts.json", "--level", "distinct", "--config", config)
    skipped, differs = run(*distinct, "--fail-on", "incomplete"), run(*distinct, "--fail-on", "differs")
    by_name = ("precheck", "run", "--spec", "accounts", "--config", config)
    columns = run(*by_name, "--fail-on", "incomplete", "--json")
    assert (skipped.exit_code, differs.exit_code, columns.exit_code) == (1, 0, 0)
    assert skipped.stdout.splitlines()[-2:] == [
        "1 pair: 1 not fully checked",
        "1  accounts  duplicate keys and distinct values not counted: 3 rows on side a is over"
        " precheck.distinct_max_rows (2)",
    ]
    found = json.loads(columns.stdout)
    assert found["items"][0]["text"] == BEST.format("columns") and found["finished_at"] is not None


@pytest.mark.parametrize(
    ("fail_on", "verdicts", "cancelled", "code"),
    [
        ("refused", ["not_read", "values_differ"], False, 0),
        ("refused", ["refused"], False, 1),
        ("refused", ["unpaired"], False, 1),
        ("differs", ["incomplete", "schema_differs"], False, 0),
        ("differs", ["values_differ"], False, 1),
        ("differs", ["not_read"], False, 1),
        ("incomplete", ["schema_differs", "no_difference_observed"], False, 0),
        ("incomplete", ["incomplete"], False, 1),
        ("incomplete", [None], True, 1),  # a pair never read: the pre-check was cancelled
        (None, ["unpaired", "refused"], True, 0),
    ],
)
def test_fail_on_judges_the_worst_pair_and_a_cancel(
    monkeypatch: pytest.MonkeyPatch,
    fail_on: str | None,
    verdicts: list[str | None],
    cancelled: bool,
    code: int,
) -> None:
    pairs = [
        {"n": n, "label": "accounts", "verdict": verdict, "status": "done", "text": "a line"}
        for n, verdict in enumerate(verdicts, 1)
    ]
    found = {
        "not": NOT, "evaluation": False, "precheck_id": "PC-1", "source": "specs", "level": "columns",
        "created_by": "local:ops@host", "created_by_name": None, "created_at": datetime.now(UTC),
        "finished_at": datetime.now(UTC), "cancelled_at": datetime.now(UTC) if cancelled else None,
        "banners": [], "totals": "the totals", "items": pairs,
    }  # fmt: skip
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_: found)
    monkeypatch.setattr("deltasleuth.cli.main._local", lambda _: None)
    result = run("precheck", "run", "--all-specs", *(["--fail-on", fail_on] if fail_on else []))
    assert result.exit_code == code and result.stdout.splitlines()[-1] == f"{len(pairs)}  accounts  a line"


# -- the schema guard -------------------------------------------------------


@pytest.mark.db
def test_ds_run_prints_the_schema_guard_s_warnings_and_exits_2_for_a_run_it_paused(
    store: Callable[..., Path], tmp_path: Path, pg_dsn: str
) -> None:
    """``ds run`` prints each warning of a run's creation as ``warning: …``; a run the guard paused prints
    ``paused (run_paused_schema_drift)`` with its lines and exits 2, as ``ds runs`` shows its first line."""
    config = store()
    (tmp_path / "t.yaml").write_text(prechecking.database_targets(tmp_path / "dsn"))
    spec = tmp_path / "customers.json"
    spec.write_text(prechecking.table_spec("customers", b={"name": "customers_new"}))
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for name in ("customers", "customers_new"):
            conn.execute(f"CREATE TABLE {name} (id int PRIMARY KEY, email text, amount numeric(12,2))")  # type: ignore[arg-type]
            conn.execute(f"INSERT INTO {name} VALUES (1, 'a@x', 10.50)")  # type: ignore[arg-type] # noqa: S608
    first = run("run", spec, "--config", config)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("ALTER TABLE customers_new ADD COLUMN loyalty_tier text")
    warned = run("run", spec, "--config", config)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("ALTER TABLE customers_new DROP COLUMN amount")
    paused, unwaited = run("run", spec, "--config", config), run("run", spec, "--no-wait", "--config", config)
    listed = run("runs", "--config", config)
    ids = [re.search(r"run (R-\S+) started", found.stdout)[1] for found in (first, warned)]  # type: ignore[index]
    assert (first.exit_code, warned.exit_code) == (0, 0) and "warning: side" not in first.stderr
    gained = f"side b gained column 'loyalty_tier' since run {ids[0]}: this version does not compare it"
    assert f"warning: {gained}\n" in warned.stderr and warned.stdout.splitlines()[1] == "completed"
    line = (
        f"Paused by the schema guard before any table row was read: column 'amount' was compared by run"
        f" {ids[1]} of this version and is not compared now. Resume to accept the tables as they are, or"
        " cancel."
    )
    for found in (paused, unwaited):
        assert found.exit_code == 2 and found.stdout.splitlines()[1:] == [
            "paused (run_paused_schema_drift)", f"  {line}",
        ]  # fmt: skip
        assert found.stderr.splitlines()[-1] == "error: the run is paused"
        assert "warning: Paused" not in found.stderr and "warning: column" not in found.stderr  # said once
    assert listed.stdout.count(f"  {line}") == 2 and "processed by" not in unwaited.stdout


# -- your own agent: the MCP server -------------------------------------------------

TOKENISED = {"data_classes": ["aggregates", "production_tokenised"]}
EVERY = {"data_classes": [*TOKENISED["data_classes"], "production_clear_declared"], "work": ["compare_key"]}
MCP_OFF = "error: MCP is off: the settings have no mcp block\n"
HOST_CHANNEL = (
    "error: ds mcp under auth is the host-administrator channel: use /v1/mcp with a named token, or pass"
    " --host-channel\n"
)
ASSERTED = (
    "warning: ds mcp is the host-administrator channel: its caller is asserted (DS_OPERATOR), not signed in,"
    " and an agent that runs it acts with this host account, not with a role; agents belong on /v1/mcp with"
    " a named token"
)
CLEAR = (
    "warning: mcp: values a spec declares clear and business-code keys leave the installation in tool results"
)
MODES = ("developer", "tokens", "oidc")
# A store's address, account and database, as psycopg words a connection in its loggers' lines.
STORE = "(host=db.internal.example port=6432 user=Account-Canary database=Database-Canary)"


def mode_auth(mode: str, tmp_path: Path) -> MaybeAuth:
    """A mode's ``auth`` block: none in developer mode."""
    return {"developer": None, "tokens": {"public_url": "https://ds.example"}, "oidc": sso(tmp_path)}[mode]


def mcp_settings(tmp_path: Path, block: Any, auth: MaybeAuth = None) -> Path:
    """Settings with the ``mcp`` block; no store exists and the workspace has no key."""
    doc: dict[str, Any] = {} if block is None else {"mcp": block}
    if auth is not None:
        doc |= {"auth": auth, "encryption": encryption(tmp_path)}
    (tmp_path / "ds.yaml").write_text(SETTINGS + (yaml.safe_dump(doc) if doc else ""))
    return tmp_path / "ds.yaml"


def test_mcp_refuses_in_one_line_before_it_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DS_OPERATOR", raising=False)
    off = mcp_settings(tmp_path, None)
    assert [ds("mcp", *args, "--config", off) for args in ((), ("--host-channel",), ("check",))] == [
        (2, MCP_OFF)
    ] * 3
    served = mcp_settings(tmp_path, TOKENISED)
    usage = run("mcp", "--role", "admin", "--config", served)  # typer's own: another role is no role of MCP's
    assert usage.exit_code == 2 and "Invalid value for '--role'" in usage.output
    assert ds("mcp", "--config", served) == (  # a keyring problem: ds init has not run
        2, f"error: tokeniser key {tmp_path / 'w' / 'tokeniser.key'} not found; run 'ds init' first\n",
    )  # fmt: skip
    assert ds("mcp", "--config", served, "check") == (2, "error: put the options after 'check'\n")
    assert ds("mcp", "--role", "operator", "check") == (2, "error: put the options after 'check'\n")
    assert ds("mcp", "--config", mcp_settings(tmp_path, {"http": True})) == (
        2, f"error: invalid settings {tmp_path / 'ds.yaml'}: mcp.data_classes: Field required\n",
    )  # fmt: skip

    def unreachable(*_: Any, **__: Any) -> Any:
        raise psycopg.OperationalError("connection to db.internal.example failed: Dsn-Passw0rd-Canary")

    monkeypatch.setattr("deltasleuth.cli.main.Context.open", unreachable)
    assert ds("mcp", "--config", mcp_settings(tmp_path, TOKENISED)) == (
        2, "error: the database is unreachable (OperationalError); check database_dsn_ref\n",
    )  # fmt: skip


@pytest.mark.parametrize("mode", MODES[1:])
def test_mcp_under_auth_serves_only_as_the_host_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Where the settings have auth, ``ds mcp`` is the host administrator's, said so by a flag, and
    its caller asserted: each refusal comes before any work (no store exists here)."""
    config = mcp_settings(tmp_path, TOKENISED, mode_auth(mode, tmp_path))
    monkeypatch.setenv("DS_OPERATOR", "jdoe")
    assert ds("mcp", "--config", config) == (2, HOST_CHANNEL)
    assert ds("mcp", "--role", "operator", "--config", config) == (2, HOST_CHANNEL)
    monkeypatch.delenv("DS_OPERATOR")
    assert ds("mcp", "--config", config) == (2, HOST_CHANNEL)  # the flag first: an agent is sent to /v1/mcp
    assert ds("mcp", "--host-channel", "--config", config) == (2, f"error: {OPERATOR}\n")
    assert ds("mcp", "check", "--config", config)[0] == 0  # the settings alone, in every mode


@pytest.mark.db
@pytest.mark.parametrize("mode", MODES)
def test_mcp_serves_stdio_as_the_local_caller_cut_to_its_role(
    store: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Its start line, and under auth the warning; its caller is ``local()`` cut to ``--role``. The
    runner's stdin is at its end, so the serving ends at once: exit 0, and nothing on stdout."""
    from deltasleuth.mcp import server

    monkeypatch.setenv("DS_OPERATOR", "jdoe")
    monkeypatch.setattr("socket.gethostname", lambda: "ds host")
    auth = mode_auth(mode, tmp_path)
    config, stdio, callers = store(auth, mcp=EVERY), server.stdio, []

    async def serving(ctx: Any, settings: Any, who: Any, *rest: Any) -> None:
        callers.append((who.actor, who.name, who.roles, who.via, who.os_account, settings.work))
        await stdio(ctx, settings, who, *rest)

    monkeypatch.setattr(server, "stdio", serving)
    channel = [] if auth is None else ["--host-channel"]
    served = [run("mcp", *role, *channel, "--config", config) for role in ((), ("--role", "operator"))]
    assert [(done.exit_code, done.stdout) for done in served] == [(0, "")] * 2
    for done, role in zip(served, ("viewer", "operator"), strict=True):
        said = [line for line in done.stderr.splitlines() if not line.startswith("{")]  # but the SIEM's rows
        assert said == [
            f"mcp: serving stdio as local:jdoe@ds_host, role {role}; data classes aggregates,"
            " production_tokenised, production_clear_declared; work tools compare_key",
            *([] if auth is None else [ASSERTED]),
        ]
    account = pwd.getpwuid(os.getuid()).pw_name
    assert callers == [
        ("local:jdoe@ds_host", "jdoe", {"viewer"}, "mcp:stdio", account, ("compare_key",)),
        ("local:jdoe@ds_host", "jdoe", {"viewer", "operator"}, "mcp:stdio", account, ("compare_key",)),
    ]


@pytest.mark.db
def test_a_fault_of_the_mcp_stream_leaves_its_type_alone(
    store: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug's traceback is printed before the serving only: once it serves, stderr is the agent's."""
    from deltasleuth.mcp import server

    async def broken(*_: Any) -> None:
        raise RuntimeError("postgresql://ds:Dsn-Passw0rd-Canary@db.internal.example/ds")

    def early(**_: Any) -> None:
        raise RuntimeError("before it serves")

    config = store(mcp=TOKENISED)
    with monkeypatch.context() as patched:
        patched.setattr(server, "stdio", broken)
        failed = run("mcp", "--config", config)
    assert failed.exit_code == 3 and failed.stdout == ""
    assert [line for line in failed.stderr.splitlines() if not line.startswith(("{", "mcp: serving"))] == [
        "internal error: the MCP stream failed (RuntimeError)"
    ]
    monkeypatch.setattr("deltasleuth.cli.main.local", early)
    bug = run("mcp", "--config", config)
    assert bug.exit_code == 3 and "Traceback" in bug.stderr and "before it serves" in bug.stderr


@pytest.mark.db
def test_mcp_leaves_a_later_sigterm_without_a_handler(
    store: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the serving has ended without a SIGTERM (here at the end of the runner's stdin) nothing
    waits for the signal's event, so the signal is without a handler before the library's transport is left,
    where the process may wait; and the first SIGTERM, having set the event, leaves the signal so too, for a
    second to end a process whose loop never comes to the event. Afterwards it is this process's own again."""
    from deltasleuth.mcp import server

    config, transport, seen = store(mcp=TOKENISED), server.stdio_server, []
    mine = signal.getsignal(signal.SIGTERM)

    @asynccontextmanager
    async def leaving() -> AsyncIterator[Any]:
        async with transport() as streams:
            try:
                yield streams
            finally:
                seen.append(signal.getsignal(signal.SIGTERM))

    async def signalled(ctx: Any, settings: Any, who: Any, stop: asyncio.Event, stopped: Any) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 10)
        seen.append(signal.getsignal(signal.SIGTERM))

    with monkeypatch.context() as patched:
        patched.setattr(server, "stdio_server", leaving)
        assert run("mcp", "--config", config).exit_code == 0
    monkeypatch.setattr(server, "stdio", signalled)
    assert run("mcp", "--config", config).exit_code == 0
    assert seen == [signal.SIG_DFL, signal.SIG_DFL] and signal.getsignal(signal.SIGTERM) is mine


@pytest.mark.parametrize(("concurrency", "pool"), [(None, (1, 5)), (2, (1, 3))])
def test_mcp_keeps_one_idle_store_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrency: int | None, pool: tuple[int, int]
) -> None:
    """An editor starts a server per window, so each idles on one connection, not the pool's four."""
    opened: list[Any] = []
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda _, work, pool: opened.append(pool))
    block = TOKENISED | ({} if concurrency is None else {"concurrency": concurrency})
    assert ds("mcp", "--config", mcp_settings(tmp_path, block)) == (0, "")
    assert opened == [pool]


@pytest.mark.parametrize("entry", ["mcp", "serve"])
def test_the_mcp_loggers_are_the_entry_point_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    """Each call's line and the library's own lines from WARNING up, on stderr through handlers of the
    entry point's own. Under ``ds mcp`` stderr is the launching agent's: a library line is then its logger
    and level alone, without its message and without its traceback, which may hold data; and so is every
    other logger's, the driver's words for the store among them, through the root logger, which ``ds serve``
    leaves as it was."""
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: None)
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_, **__: None)
    config = mcp_settings(tmp_path, TOKENISED | {"http": True})
    assert run(entry, "--config", config).exit_code == 0
    library, own = logging.getLogger("mcp.server.lowlevel.server"), logging.getLogger("deltasleuth.mcp")
    driver, service = logging.getLogger("psycopg.pool"), logging.getLogger("deltasleuth.readiness")
    capsys.readouterr()
    try:
        raise ValueError("Dsn-Passw0rd-Canary")
    except ValueError:
        library.exception("handler for %r raised", "Raw-Key-Canary")
        service.warning("the standing could not be read %s", STORE, exc_info=True)
    driver.warning("error connecting in 'pool-1': connection failed: %s", STORE)
    driver.info("a connection was added")  # below WARNING: never shown
    library.info("a session started")  # likewise
    own.info("tool=list_runs actor=local:jdoe@host run=- bytes=41 ms=3 error=0 request_id=C-0")
    err = capsys.readouterr().err
    called = "tool=list_runs actor=local:jdoe@host run=- bytes=41 ms=3 error=0 request_id=C-0"
    if entry == "mcp":
        bare = ["mcp.server.lowlevel.server ERROR", "deltasleuth.readiness WARNING", "psycopg.pool WARNING"]
        assert err.splitlines() == [*bare, called]
    else:
        assert err.splitlines()[0] == "mcp.server.lowlevel.server ERROR handler for 'Raw-Key-Canary' raised"
        assert err.splitlines()[-2:] == ["ValueError: Dsn-Passw0rd-Canary", called]
    for name in ("mcp", "deltasleuth.mcp"):  # neither is left to the root logger or the last-resort handler
        assert not logging.getLogger(name).propagate and len(logging.getLogger(name).handlers) == 1
    assert logging.getLogger("mcp").level == logging.WARNING
    own_root = [h for h in logging.getLogger().handlers if type(h).__module__ == "deltasleuth.cli.main"]
    assert len(own_root) == (entry == "mcp")


@pytest.mark.parametrize(
    ("block", "said"),
    [
        (None, []),
        (EVERY, []),  # the block alone serves stdio: POST /v1/mcp needs http
        (TOKENISED | {"http": True}, ["mcp: POST /v1/mcp is served; data classes aggregates,"
                                      " production_tokenised; work tools none"]),
        (EVERY | {"http": True}, ["mcp: POST /v1/mcp is served; data classes aggregates,"
                                  " production_tokenised, production_clear_declared; work tools compare_key",
                                  CLEAR]),
    ],
    ids=["no block", "http off", "tokenised", "clear declared"],
)  # fmt: skip
def test_serve_says_what_it_serves_over_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: Any, said: list[str]
) -> None:
    """With ``mcp.http``, on stderr; and a server that does not serve it leaves the loggers alone."""
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: None)
    monkeypatch.setattr("deltasleuth.cli.main._in_context", lambda *_, **__: None)
    served = run("serve", "--config", mcp_settings(tmp_path, block))
    assert served.exit_code == 0
    assert [line for line in served.stderr.splitlines() if "mcp" in line] == said
    assert logging.getLogger("mcp").propagate == (not said)


def test_serve_loads_the_mcp_library_before_it_opens_the_store(tmp_path: Path, repo_root: Path) -> None:
    """With ``mcp.http`` the start imports the MCP library, which a name of ``OTEL_*`` that the image
    holds nothing for stops. ``ds serve`` loads it with what else it opens once first: the start then ends
    in one line before the store is opened (no key exists here), so before ``system.start`` is audited.
    Without ``mcp.http`` nothing loads it, and the same environment stops nothing."""
    code = "from deltasleuth.cli.main import app; app()"
    env = {name: value for name, value in os.environ.items() if name not in ("DS_OPERATOR", "DS_CONFIG")}
    ended = []
    for block in (TOKENISED | {"http": True}, TOKENISED):
        argv = [sys.executable, "-c", code, "serve", "--port", "0", "--config", mcp_settings(tmp_path, block)]
        done = subprocess.run(
            argv, cwd=repo_root, env=env | {"OTEL_PROPAGATORS": "b3"}, capture_output=True, text=True,
            check=False, timeout=120,
        )  # fmt: skip
        # past the warnings: developer mode's and, in a checkout whose UI is not built, that one's
        said = [line for line in done.stderr.splitlines() if not line.startswith("warning:")]
        ended.append((done.returncode, done.stdout, said))
    assert ended == [
        (2, "", ["error: Propagator b3 not found. It is either misspelled or not installed."]),
        (2, "", [f"error: tokeniser key {tmp_path / 'w' / 'tokeniser.key'} not found; run 'ds init' first"]),
    ]


def test_mcp_check_prints_the_block_as_judged_and_what_mcp_is_not(tmp_path: Path) -> None:
    from deltasleuth.mcp import tools

    reads = ", ".join(name for name in tools.CATALOGUE if name != "compare_key")
    masked = run("mcp", "check", "--config", mcp_settings(tmp_path, TOKENISED))
    assert (masked.exit_code, masked.stderr) == (0, "")
    assert masked.stdout.splitlines() == [
        "mcp: data classes aggregates, production_tokenised; work tools none; POST /v1/mcp off; 60 calls a"
        " minute per caller, 4 at once",
        f"viewer: {reads}",
        f"operator: {reads}",
        "not served: compare_key (mcp.work)",
        "runs whose spec declares clear values or a business-code key are not served",
        *tools.NOT,
    ]
    block = EVERY | {"http": True, "calls_per_minute": 30, "concurrency": 2}
    every = run("mcp", "check", "--config", mcp_settings(tmp_path, block))
    assert (every.exit_code, every.stderr) == (0, f"{CLEAR}\n")
    assert every.stdout.splitlines() == [
        "mcp: data classes aggregates, production_tokenised, production_clear_declared; work tools"
        " compare_key; POST /v1/mcp served; 30 calls a minute per caller, 2 at once",
        f"viewer: {reads}",
        f"operator: {reads}, compare_key",
        *tools.NOT,
    ]
    counts = run("mcp", "check", "--config", mcp_settings(tmp_path, {"data_classes": ["aggregates"]}))
    listed = counts.stdout.splitlines()
    counted = "list_runs, get_readiness, get_readiness_pair, list_schedules, get_schedule, list_prechecks"
    assert listed[1:3] == [f"viewer: {counted}", f"operator: {counted}"]
    assert listed[3:8] == [
        f"not served: {name} (mcp.data_classes lacks production_tokenised)"
        for name in ("get_run_summary", "get_root_causes", "get_findings", "get_observations", "compare_key")
    ]


def test_mcp_help_never_says_what_deltasleuth_does_not_do() -> None:
    """As the tools' own texts (tests/unit/mcp_server/test_tools.py ``test_never_told``)."""
    never = ("verified", "validated", "certified", "guaranteed", "anonymised", "anonymized")
    said = [ds("mcp", "--help")[1], ds("mcp", "check", "--help")[1], MCP_OFF, HOST_CHANNEL, ASSERTED, CLEAR]
    assert "--host-channel" in said[0] and "--role" in said[0]
    assert not [(word, text) for text in said for word in never if word in text.lower()]
