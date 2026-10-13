"""``ds``: the DeltaSleuth command line.

    ds init | migrate | serve | worker          set up, then run the API, UI and worker
    ds spec validate SPEC [--targets T]
    ds target check ID [--table [SCHEMA.]NAME]  a database target's pre-flight: firewall, TLS, session, grants
    ds compare KEY --spec SPEC                  one key now (a one-shot run)
    ds run SPEC [--keys F] [--samples DIR] [--soak] [--no-wait]
    ds runs | report RUN                        list runs; write their reports
    ds profile | measure | adjudicate | score   the P0 onboarding tools
    ds llm-probe [--profile ID]
    ds token create|revoke|list                 API tokens
    ds session revoke | users [disable|enable]  break-glass: sessions and people
    ds audit [filters] | verify | auth check    the audit log; its hash chain; the IdP settings
    ds purge [--dry-run] | hold | release       retention: delete expired evidence, or keep a run's
    ds keys status | fingerprint | migrate      the keyring; seal what releases before 006 stored in clear
    ds approvals [--needed] | revoke            four-eyes approvals; break-glass: revoke one
    ds packs | key | revocations | download     evidence packs, their key register entry and revocations
    ds packs verify FILE.zip --fingerprint HEX  check a pack offline (no settings needed)
    ds licence [--json] | id | show | install | withdraw   a vendor build's licence; the installation id
    ds samples [--spec NAME] [--json]          sign-off samples: every draw, or a name's ledger
    ds samples size --tier T [--failures N]    a sign-off stratum's single-stage size (no settings needed)
    ds labellings [--run R-…] [--json]         labelled accuracy: each labelling in one line
    ds notify [--json] | check | explain | deliveries   alerts and notifications: their state, the channels
    ds notify test | retry | mute | unmute     checked without sending, a cycle's digest; the audited acts
    ds readiness [--programme P] [--json] | pair NAME   cut-over readiness: each pair's gates and state
    ds readiness report | check | verify       its reports; the settings as judged; the stored measures
    ds schedules [--spec NAME] [--json] | show NAME    cron schedules: each one's next due time and firings
    ds schedules next CRON --tz ZONE           a cron's next times in a zone (no settings needed)
    ds schedules add | set | pause | resume | delete   the audited writes; the worker fires
    ds precheck [--json] | show PC-… [--pair N]   table pre-checks: counts and sums of pairs of tables
    ds precheck run [SPEC…] [--spec-id N] [--spec NAME] [--all-specs] [--level L] [--fail-on V]
    ds precheck run --a T[:SCHEMA] --b T[:SCHEMA] [--include G] [--exclude G]   two schemas, by name
    ds precheck cancel PC-… | report PC-… --out DIR | draft PC-… (--pair N | --all) --out DIR
    ds mcp [--role viewer|operator] [--host-channel]   the MCP tools for your own agent, over stdin and stdout
    ds mcp check                               the mcp settings as judged, and what MCP is not (no store)

Settings come from ``--config`` or ``DS_CONFIG``. Exit codes: 0 success; 1 discrepancies found
(``--fail-on-diff``), a pair below the state asked for (``ds readiness --fail-unless``), or a table
pre-check with a pair as bad as asked, or cancelled (``ds precheck run --fail-on``); 2 usage,
input or configuration errors (one ``error:`` line each); 3 an internal error (a bug: the traceback is
printed); 130 interrupted. Messages name files, pointers and counts, never keys, payload values or
credentials.

The CLI acts as ``local:<DS_OPERATOR>@<host>`` with every role, outside the role model: whoever holds
the DSN file and the tokeniser key can reach the store directly (docs/mvp/AUTH.md §4.2). With the auth
settings, the commands that write audit rows need ``DS_OPERATOR``; without them it defaults to the OS
account. It can neither request nor grant a four-eyes approval: under SSO ``ds run`` and ``ds compare``
run only an approved upload of the spec file's content (docs/mvp/APPROVALS.md §4). ``serve``, ``worker``,
``run`` and ``compare`` print the licence's warnings (docs/mvp/LICENCE.md §10: a vendor build's; a build
without a vendor key needs no licence and has none to print, §19); ``serve``, ``worker`` and
``run`` warn of a notification channel's unreadable secret (docs/mvp/ALERTS.md §10). ``ds mcp`` serves an
agent as that principal cut to ``--role``, each tool call a request of its own (docs/mvp/MCP.md §6).
"""

from __future__ import annotations

import asyncio
import atexit
import errno
import importlib
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

import psycopg
import typer

from deltasleuth import approvals, licence, notifications, prechecks, readiness, schedules
from deltasleuth.approvals import ApprovalConflict, ApprovalRequired
from deltasleuth.auth.oidc import SignInError
from deltasleuth.auth.principal import ROLES, Principal, expand, local
from deltasleuth.config import McpSettings, OidcSettings, Settings, SettingsError, load_settings
from deltasleuth.connectors.base import ConnectorError
from deltasleuth.context import Context, ContextError, init_workspace
from deltasleuth.core import precheck as prechecking
from deltasleuth.core.readiness import STATES, config_sha256, worded
from deltasleuth.core.spec import (
    SpecError,
    SpecIssue,
    compile_spec,
    load_spec_file,
    load_targets,
    validate_spec,
)
from deltasleuth.core.spec.documents import in_container
from deltasleuth.core.types import CompiledSpec, Verdict
from deltasleuth.evidence.store import EvidenceError
from deltasleuth.licence import LicenceRefused
from deltasleuth.llm.model import LLMError
from deltasleuth.runner import drift, sample
from deltasleuth.runner.compare import EvidenceMissing
from deltasleuth.runner.purge import CyclePurged
from deltasleuth.runner.sample import SampleRefused
from deltasleuth.security import rawvalues
from deltasleuth.security.signing import PackSigner
from deltasleuth.store import chain, repo

EXIT_DIFF, EXIT_USAGE, EXIT_INTERNAL, EXIT_INTERRUPTED = 1, 2, 3, 130
DEVELOPER_MODE = (
    "warning: developer mode (no auth settings): every local user and process acts as admin; never expose"
    " this port, directly or through a reverse proxy or port forward"
)
NO_APPROVERS = "warning: no IdP group maps to rule_approver or above: nothing can be approved"
VALUE_WIDTH = 60
SUMMARY = "summary"  # the P0 contract report (report.md, report.html); the other kinds are reports.FORMATS
_COUNTERS = ("total", "compared", "match", "different", "indeterminate", "cancelled", "outstanding")
_EVIDENCE = ("kind", "path_a", "path_b", "decision", "sample_count", "scores", "note")
# Failures a user can act on (ValueError covers bad settings, secrets, keys and sheets): one line
# each, exit 2. Anything else is a bug, as is a KeyError or IndexError: no code raises them on purpose.
_EXPECTED = (SpecError, ConnectorError, EvidenceError, LLMError, ContextError, SignInError, CyclePurged,
             EvidenceMissing, ApprovalRequired, ApprovalConflict, LicenceRefused, SampleRefused,
             psycopg.OperationalError, LookupError, ValueError, OSError)  # fmt: skip
_BUGS = (KeyError, IndexError)

app = typer.Typer(
    name="ds",
    no_args_is_help=True,
    help="DeltaSleuth: compare what two sources hold, key by key, black-box: REST services, database tables,"
    " CSV and Parquet files.",
    pretty_exceptions_enable=False,
)
spec_app = typer.Typer(no_args_is_help=True, help="Comparison specs.")
token_app = typer.Typer(no_args_is_help=True, help="API tokens (docs/mvp/AUTH.md §5).")
session_app = typer.Typer(no_args_is_help=True, help="Sign-in sessions.")
users_app = typer.Typer(invoke_without_command=True)
auth_app = typer.Typer(no_args_is_help=True, help="Sign-in settings.")
purge_app = typer.Typer(invoke_without_command=True)
keys_app = typer.Typer(no_args_is_help=True, help="Encryption at rest (docs/mvp/RETENTION.md).")
approvals_app = typer.Typer(invoke_without_command=True)
audit_app = typer.Typer(invoke_without_command=True)
packs_app = typer.Typer(invoke_without_command=True)
licence_app = typer.Typer(invoke_without_command=True)
samples_app = typer.Typer(invoke_without_command=True)
notify_app = typer.Typer(invoke_without_command=True)
readiness_app = typer.Typer(invoke_without_command=True)
schedules_app = typer.Typer(invoke_without_command=True)
precheck_app = typer.Typer(invoke_without_command=True)
target_app = typer.Typer(
    no_args_is_help=True,
    help="Database and files targets: the pre-flight check (docs/mvp/ENGINES.md §13, FILES.md §16).",
)
mcp_app = typer.Typer(invoke_without_command=True)
app.add_typer(spec_app, name="spec")
app.add_typer(token_app, name="token")
app.add_typer(session_app, name="session")
app.add_typer(users_app, name="users")
app.add_typer(auth_app, name="auth")
app.add_typer(purge_app, name="purge")
app.add_typer(keys_app, name="keys")
app.add_typer(approvals_app, name="approvals")
app.add_typer(audit_app, name="audit")
app.add_typer(packs_app, name="packs")
app.add_typer(licence_app, name="licence")
app.add_typer(samples_app, name="samples")
app.add_typer(notify_app, name="notify")
app.add_typer(readiness_app, name="readiness")
app.add_typer(schedules_app, name="schedules")
app.add_typer(precheck_app, name="precheck")
app.add_typer(target_app, name="target")
app.add_typer(mcp_app, name="mcp")


def _o(text: str, *flags: str) -> Any:
    return typer.Option(*flags, help=text)


def _a(text: str) -> Any:
    return typer.Argument(help=text)


ConfigOpt = Annotated[Path | None, _o("Settings file (default: $DS_CONFIG).")]
SpecFile = Annotated[Path, _a("Comparison spec (YAML or JSON).")]
TargetsOpt = Annotated[Path | None, _o("Targets file: check the target rules too.")]
RunArg = Annotated[str, _a("The run id.")]
OutOpt = Annotated[Path, _o("Output directory.")]
CycleOpt = Annotated[int | None, _o("Soak cycle (default: the current one).")]
FailOpt = Annotated[bool, _o("Exit 1 when a key is different.", "--fail-on-diff")]
REASON = "Why, in 1 to 200 characters. It is audited and reaches the SIEM: never personal data."


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(EXIT_USAGE)


def _message(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        where = in_container(exc.filename) if isinstance(exc, FileNotFoundError) and exc.filename else ""
        return f"{exc.strerror or type(exc).__name__}: {exc.filename or ''}".rstrip(": ") + where
    if isinstance(exc, UnicodeDecodeError):
        return f"a file is not valid {exc.encoding} text (byte offset {exc.start}); save it as UTF-8"
    if isinstance(exc, psycopg.OperationalError):
        return f"the database is unreachable ({type(exc).__name__}); check database_dsn_ref"
    return str(exc)


def _issues(issues: list[SpecIssue]) -> None:
    for issue in issues:
        typer.echo(f"{issue.severity}: {issue}", err=True)


@contextmanager
def _errors(interrupted: str = "interrupted") -> Iterator[None]:
    """Expected failures: one line each, exit 2. A bug: its traceback, exit 3 (never 1). Ctrl-C: 130,
    saying ``interrupted``."""
    try:
        yield
    except KeyboardInterrupt:
        typer.echo(interrupted, err=True)
        raise typer.Exit(EXIT_INTERRUPTED) from None
    except Exception as exc:
        if isinstance(exc, _BUGS) or not isinstance(exc, _EXPECTED):
            traceback.print_exception(exc)
            typer.echo("internal error: a bug in DeltaSleuth; the traceback above tells where", err=True)
            raise typer.Exit(EXIT_INTERNAL) from None
        if isinstance(exc, SpecError):
            _issues(list(exc.issues))  # its message would repeat them
        else:
            typer.echo(f"error: {_message(exc)}", err=True)
        raise typer.Exit(EXIT_USAGE) from None


def _run[T](work: Awaitable[T], interrupted: str = "interrupted") -> T:
    with _errors(interrupted):
        return asyncio.run(work)  # type: ignore[arg-type]


def _in_context[T](
    config: Path | None, work: Callable[[Context], Awaitable[T]], pool: tuple[int, int] | None = None
) -> T:
    """``work`` with a Context open on the configured store (migrated; ``pool``: its least and most
    connections); the audit rows it commits are copied for the SIEM."""
    _siem()

    async def main() -> T:
        async with Context.open(load_settings(config), pool=pool) as ctx:
            return await work(ctx)

    return _run(main())


def _read_keys(path: Path) -> list[str]:
    """One key per line; blank lines and ``#`` comments are skipped."""
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _local(config: Path | None) -> Principal:
    """The invocation's principal (docs/mvp/AUTH.md §4.2), before any work: with the auth settings
    ``DS_OPERATOR`` must name the operator."""
    with _errors():
        return local(required=load_settings(config).auth is not None)


class _Stream(logging.Handler):
    """One line per record on the current ``sys.stdout`` or ``sys.stderr`` (a test runner swaps them)."""

    def __init__(self, stream: str) -> None:
        super().__init__()
        self.stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            out = getattr(sys, self.stream)
            out.write(self.format(record) + "\n")
            out.flush()
        except Exception:
            self.handleError(record)


def _siem(stream: str = "stderr") -> None:
    """Copy each audit row this process commits to ``stream`` as a JSON line, for the SIEM (§6): the
    services' stdout, and a command's stderr, so that its stdout stays its output (a token, JSON). The
    first stream chosen stays."""
    audit = logging.getLogger("deltasleuth.audit")
    if not any(isinstance(h, _Stream) for h in audit.handlers):
        handler = _Stream(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    audit.propagate = False


async def _wrote(ctx: Context, who: Principal, run_id: str, name: str, cycle: int | None) -> None:
    """Audit a report the CLI wrote as ``report.download``, in its own transaction."""
    async with repo.audited(ctx.pool) as (conn, audit):
        if cycle is None:  # the run's current cycle, which the report read
            run = await repo.get_run(conn, run_id)
            cycle = run and run["cycle"]
        await audit(who, "report.download", run_id, {"name": name, "cycle": cycle})


async def _warned(ctx: Context) -> None:
    """Print each of the licence's warnings on stderr (docs/mvp/LICENCE.md §10): ``serve``, ``worker``,
    ``run`` and ``compare`` only, and never failing them."""
    await licence.shown(ctx, lambda line: typer.echo(f"warning: {line}", err=True))


def _named(ident: str | None, name: str | None) -> str:
    """An actor, user or token id with its name beside it (the id alone when the name adds nothing)."""
    if ident is None:
        return "-"
    return f"{ident} ({name})" if name and name != ident else ident


def _at(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    def plain(v: Any) -> str:
        return v.isoformat() if isinstance(v, datetime) else str(v)

    return json.dumps(value, default=plain, indent=2, sort_keys=True)


async def _save_spec(
    ctx: Context, path: Path, who: Principal, *, gated: bool = True
) -> tuple[int, CompiledSpec]:
    """Store the spec file (once per content hash), audited; its spec_id and compiled form.

    Under SSO a run needs an approved upload (docs/mvp/APPROVALS.md §4): the file's stored version is
    found by its content hash and gated before anything is written (unless ``gated`` is false: a table
    pre-check is not gated, docs/mvp/PRECHECKS.md §6), and it is not saved again. A version that needs an
    approval and was never uploaded is refused; one that needs none is stored. A body the store would not
    keep as written is refused, storing nothing, as the upload route does."""
    spec = compile_spec(load_spec_file(path), targets=ctx.targets)
    if problem := approvals.unstorable(spec.raw):
        raise ValueError(problem)
    if ctx.settings.auth_mode == "oidc":
        async with ctx.pool.connection() as conn:
            stored = await repo.spec_by_hash(conn, spec.content_hash)
            if stored is not None:
                if gated:
                    await approvals.gate(ctx, conn, stored["spec_id"], spec)
                return stored["spec_id"], spec
        if approvals.needs(spec):
            raise ApprovalRequired(
                f"spec {spec.name} {spec.version} has no approved upload: upload it on the Specs screen,"
                " signed in, and have it approved (a CLI or token upload needs two approvers)"
            )
    async with repo.audited(ctx.pool) as (conn, audit):
        spec_id = await repo.save_spec(conn, spec, created_by=who.actor)
        if problem := await approvals.unstored(conn, spec_id, spec):
            raise ValueError(problem)  # rolls the upload back
        await audit(who, "spec.create", str(spec_id), {"content_hash": spec.content_hash})
    return spec_id, spec


def _unapproved(ctx: Context, spec_id: int, spec: CompiledSpec, run_id: str) -> None:
    """Warn that a run in tokens mode, which has no approvers, is labelled unapproved."""
    if ctx.settings.auth_mode == "tokens" and (needed := approvals.needs(spec)):
        typer.echo(
            f"warning: spec {spec_id} ({spec.name} {spec.version}) needs a four-eyes approval of"
            f" {', '.join(needed)}; tokens mode has no approvers, so run {run_id} is labelled unapproved",
            err=True,
        )


def _approvers(oidc: OidcSettings) -> bool:
    """Whether an IdP group maps to rule_approver or a role above it."""
    return any(oidc.roles.get(role) for role in ROLES[ROLES.index("rule_approver") :])


def _short(value: Any) -> str:
    text = "<absent>" if value is None else str(value)
    return text if len(text) <= VALUE_WIDTH else text[: VALUE_WIDTH - 1] + "…"


# -- setup and services -----------------------------------------------------------------------------


@app.command()
def init(config: ConfigOpt = None) -> None:
    """Create the workspace and, without the encryption settings, its tokeniser key and keyring (mode
    0600); existing keys are kept."""
    with _errors():
        settings = load_settings(config)
        created = init_workspace(settings)
    if created:
        key, kek = created
        typer.echo(
            f"workspace ready: tokeniser key {key}, keyring {kek}; keep a copy of both somewhere safe: a lost"
            " key cannot be made again"
        )
    else:
        typer.echo(f"workspace ready: {settings.workspace_dir} (keys: the encryption settings' secrets)")


@app.command()
def migrate(config: ConfigOpt = None) -> None:
    """Apply the pending database migrations (every command that opens the store applies them too)."""
    from psycopg_pool import AsyncConnectionPool

    from deltasleuth.core.secrets import read_secret
    from deltasleuth.store.db import WAIT, admitted, opening
    from deltasleuth.store.db import migrate as apply

    _siem()

    async def main() -> list[int]:
        dsn = read_secret(load_settings(config).database_dsn_ref)
        await admitted(dsn)
        async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=1) as pool:
            with opening():  # entering the pool does not wait: its lines would come during the migration
                await pool.open(wait=True, timeout=WAIT)
            return await apply(pool)

    applied = _run(main())
    typer.echo(f"migrations applied: {', '.join(map(str, applied))}" if applied else "schema up to date")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8000,
    no_worker: Annotated[
        bool, _o("Without the run worker: 'ds worker' runs beside it.", "--no-worker")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Serve the API (/v1) and the UI (/), with the run worker in the same process unless --no-worker.

    Beyond loopback it needs the auth settings: without them every caller acts as admin (developer
    mode). Everything is opened once first, so a configuration problem ends here in one line.
    """
    import uvicorn

    from deltasleuth.api import create_app
    from deltasleuth.api.app import WEB_DIST, AuthQueryFilter
    from deltasleuth.auth.oidc import load_credential

    with _errors():
        settings = load_settings(config)
        if settings.auth is None:
            if not _loopback(host):
                raise SettingsError(f"serving on {host} needs the auth settings (docs/mvp/AUTH.md §1)")
            typer.echo(DEVELOPER_MODE, err=True)
        elif settings.auth.oidc is not None:
            if not _approvers(settings.auth.oidc):
                typer.echo(NO_APPROVERS, err=True)
            load_credential(settings.auth.oidc)  # the server's start reads it too, without this one line
        if settings.packs is not None:
            PackSigner.load(settings.packs.signing_key_ref)  # likewise the evidence packs' signing key
        if settings.mcp is not None and settings.mcp.http:
            importlib.import_module("deltasleuth.mcp.server")  # likewise the MCP library's import (MCP.md §3)
        _bindable(host, port)  # last: before the store is opened, so no system.start row and no worker
    if not WEB_DIST.is_dir():
        typer.echo(
            f"warning: the web UI is not built ({WEB_DIST} is missing): / answers 404 and only the API under"
            " /v1 is served; run 'npm --prefix web ci' and 'npm --prefix web run build' in the checkout, then"
            " start ds serve again",
            err=True,
        )
    _notify_warned(settings)
    if settings.mcp is not None and settings.mcp.http:  # docs/mvp/MCP.md §4
        typer.echo(f"mcp: POST /v1/mcp is served; {_mcp_serves(settings.mcp)}", err=True)
        if "production_clear_declared" in settings.mcp.data_classes:
            typer.echo(MCP_CLEAR, err=True)
        _mcp_logs(bare=False)
    _siem("stdout")
    _in_context(config, _warned)
    logging.getLogger("uvicorn.access").addFilter(AuthQueryFilter())
    # proxy_headers=False: only auth.trusted_proxies may name the client (docs/mvp/AUTH.md §3).
    uvicorn.run(create_app(settings, worker=not no_worker), host=host, port=port, proxy_headers=False)


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # a host name
        return host == "localhost"


def _bindable(host: str, port: int) -> None:
    """SettingsError when ``ds serve`` cannot listen there: the address is bound once, with the options
    asyncio binds with, and closed. Best effort: the port can be taken before uvicorn binds it, and a host
    name is tried on IPv4 only."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        socket.create_server((host, port), family=family).close()
    except OSError as exc:
        why = "the port is in use; stop the program that holds it, or pass --port"
        if exc.errno != errno.EADDRINUSE:  # create_server adds the address to the system's words
            why = (exc.strerror or type(exc).__name__).partition(" (while attempting")[0]
        raise SettingsError(f"cannot listen on {host}:{port}: {why}") from None


@contextmanager
def _stopping(*, once: bool = False) -> Iterator[asyncio.Event]:
    """An event a SIGTERM sets while the block runs: the worker then stops as it does inside ``ds serve``,
    its scans' statements cancelled on their servers and its leases handed back. Without a handler the
    signal ends the process where it stands, or is ignored when it is a container's first process.
    ``once`` (``ds mcp``): the first SIGTERM also leaves the signal without a handler again, so that a
    second ends a process whose loop is blocked and never comes to set the event."""
    stop, loop = asyncio.Event(), asyncio.get_running_loop()

    def stopped(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)
        if once:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

    try:
        previous = signal.signal(signal.SIGTERM, stopped)
    except ValueError:  # not the main thread, which alone takes signals
        yield stop
        return
    try:
        yield stop
    finally:
        signal.signal(signal.SIGTERM, previous or signal.SIG_DFL)  # None: one not set from Python


@app.command()
def worker(config: ConfigOpt = None) -> None:
    """Process the keys of running runs until interrupted or terminated; unfinished keys are handed back."""
    from deltasleuth.runner import run_worker

    async def work(ctx: Context) -> None:
        await _warned(ctx)
        _notify_warned(ctx.settings)
        with _stopping() as stop:
            await run_worker(ctx, stop=stop)

    _siem("stdout")
    _in_context(config, work)


# -- specs, compares and runs ---------------------------------------------------------------------


@spec_app.command("validate")
def spec_validate(spec: SpecFile, targets: TargetsOpt = None) -> None:
    """Validate a spec against spec.schema.json and the loader rules."""
    with _errors():
        issues = validate_spec(load_spec_file(spec), targets=load_targets(targets) if targets else None)
    _issues(issues)
    if blocking := [i for i in issues if i.severity == "error"]:
        _fail(f"{len(blocking)} blocking issue(s) in {spec.name}")
    typer.echo(f"ok: {spec.name} is valid ({len(issues)} warning(s))")


def _shown(text: Any, most: int | None = 64) -> str:
    """What a server says of itself, as a terminal may show it: its printable characters, ``most`` at most
    (None: all of them). A server's text may hold control sequences."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:most]


async def _files_check(
    settings: Any, registry: Any, target: Any, config: Path | None, table: str | None, **asked: Any
) -> dict[str, Any]:
    """A files target's check (docs/mvp/FILES.md §4, §16): where its root lies (T10, judged on the files
    threads under a deadline as a context's opening judges it: a dead mount hangs no check), the root itself
    (P14, P15) and the file reader's instance, proved (P27, P28), with the limits its start saw hold
    (docs/mvp/READER.md §11); with ``table`` (a path of file.path's grammar, and ``asked``: its format and
    what else to print) the lines ``duck.check`` gives of its files."""
    from deltasleuth.connectors.tables import duck, manifest

    who = f"target {target.target_id}"
    if table is not None and asked["form"] is None:
        raise ValueError(f"{who}: --table of a files target is a path and needs --format csv or parquet")
    if refused := await manifest.opened(settings, registry, Path(config or os.environ["DS_CONFIG"])):
        raise ValueError(refused[0])
    duck.configure(settings, registry)
    mount = await duck.mount(target)
    try:
        made = await duck.configured()
    except duck.Unstarted as exc:
        raise ValueError(duck.unstarted(exc, who, settings.files)) from None
    lines = await duck.check(target, table, **asked) if table is not None else []
    found = {"mount": mount, "version": made.version, "built": duck.reader.NEEDED, "lines": lines}
    return found | {"held": ", ".join(duck.restricted(made))}


@target_app.command("check")
def target_check(
    target_id: Annotated[str, _a("A database or files target of the targets file.")],
    table: Annotated[
        str | None, _o("Also describe this table or view: [SCHEMA.]NAME; of a files target, a file.path.")
    ] = None,
    form: Annotated[str | None, _o("A files target's --table: csv or parquet.", "--format")] = None,
    delimiter: Annotated[str | None, _o("A CSV draft's delimiter, in place of the sniffed one.")] = None,
    header: Annotated[bool, _o("Name a CSV draft's fields from the first record.")] = False,
    count: Annotated[bool, _o("Also read the files whole: rows, and the lines that are not rows.")] = False,
    manifest: Annotated[bool, _o("Also hash the files: sha256sum lines and files_sha256.")] = False,
    config: ConfigOpt = None,
) -> None:
    """A database target's pre-flight, without a store and without reading a row: its address resolved and
    vetted, a session opened as a run opens one (TLS, its settings) and, with --table, the relation and what
    the role may read of it. Exit 2 says why not, as a run's creation would. It alone prints the server's
    full version. A files target's (docs/mvp/FILES.md §16): its mount and the file reader, and with --table
    PATH --format csv|parquet the files the path names, a CSV's sniffed draft or a Parquet side's columns;
    --count and --manifest read the files. It alone prints file names."""
    from deltasleuth.connectors.tables import OBSERVED, preflight

    async def work() -> tuple[str, dict[str, Any], Any]:
        settings = load_settings(config)
        registry = load_targets(settings.targets_file)
        target = registry.get(target_id)
        if target.engine is None:
            raise ValueError(f"target {target_id} is not a database target (dsn_ref or engine)")
        if target.engine == "files":
            if form not in (None, "csv", "parquet"):
                raise ValueError("--format is csv or parquet")
            asked = {"form": form, "delimiter": delimiter, "header": header, "count": count}
            found = await _files_check(settings, registry, target, config, table, **asked, listed=manifest)
            return "files", found, None
        return target.engine, *(await preflight(target, table))

    engine, server, relation = _run(work())
    if engine == "files":  # docs/mvp/FILES.md §16
        built = " and ".join(server["built"])
        version = f"duckdb {server['version']}, {built} built in"
        process = f"read in a process of its own ({server['held']})"
        typer.echo(f"target {target_id}: files, {server['mount']}, {version}, no extension loads, {process}")
        for line in server["lines"]:  # a header's names are a file's text; a draft is copied whole
            typer.echo(_shown(line, None))
        return
    version, charset = _shown(server["version"]), _shown(server["charset"])
    tls = "yes" if server["tls"] else "no"
    typer.echo(f"target {target_id}: {engine} {version}, character set {charset}, TLS {tls}, session ok")
    if engine in OBSERVED and ".".join(version.split(".")[:2]) != OBSERVED[engine]:
        observed = f"Oracle {OBSERVED['oracle']} and MySQL {OBSERVED['mysql']}"
        typer.echo(f"note: this release is observed on {observed} only")
    if engine == "oracle" and charset != "AL32UTF8":
        typer.echo("note: a run takes text keys of up to 500 bytes in this character set")
    if relation is None:
        return
    facts, named = relation.facts, _shown(f"{relation.schema}.{relation.name}", 300)
    typer.echo(f"{named} ({facts['kind']}, consistency {facts['consistency']})")
    rows = [
        (_shown(c.name, 150), _shown(c.declared, 150), c.cls or "-",
         "not comparable" if c.cls is None else "not readable" if not c.readable else "key" * c.keyable)
        for c in relation.columns
    ]  # fmt: skip
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, [*widths, 0], strict=True)]
        typer.echo(("  " + "  ".join(cells)).rstrip())


@app.command()
def compare(
    key: Annotated[str, _a("The key to compare.")],
    spec: Annotated[Path, _o("Comparison spec (YAML or JSON).")],
    as_json: Annotated[bool, _o("Print the masked result as JSON.", "--json")] = False,
    fail_on_diff: FailOpt = False,
    config: ConfigOpt = None,
) -> None:
    """Compare one key now (rechecked when discrepant): outcome and differing fields, masked."""
    from deltasleuth.runner import compare_one
    from deltasleuth.runner.counters import verdict
    from deltasleuth.security.tokenise import mask_key

    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        await _warned(ctx)
        spec_id, compiled = await _save_spec(ctx, spec, who)
        result = await compare_one(ctx, spec_id, key, who=who)
        run_id, masked = result.attrs["run_id"], mask_key(result.key, compiled, ctx.tokeniser)
        _unapproved(ctx, spec_id, compiled, run_id)
        for line in result.attrs["warnings"]:  # the schema guard's (docs/mvp/PRECHECKS.md §7)
            typer.echo(f"warning: {line}", err=True)
        async with ctx.pool.connection() as conn:
            detail = await repo.get_key_detail(conn, run_id, masked)
        result, findings = detail["result"], detail["findings"]
        return {"run_id": run_id, "key_masked": masked, "result": result, "findings": findings}

    view = _in_context(config, work)
    r, findings = view["result"], view["findings"]
    judged = verdict(r, findings)
    if as_json:
        typer.echo(json.dumps(view, default=str, indent=2, sort_keys=True))
    else:
        reason = r["inconclusive_reason"] or r["drift_reason"]
        typer.echo(
            f"{view['key_masked']}: {r['outcome']} ({judged.value}, {r['confirmation']})"
            + (f" reason {reason}" if reason else "")
        )
        for f in findings:
            if f["severity"] != "INFO" or f["suppressed"]:
                rule = f" suppressed by {f['rule_id']}" if f["suppressed"] else ""
                where = f"{f['severity']} {f['taxonomy_code']} {f['change_type']} {f['json_pointer'] or '/'}"
                typer.echo(
                    f"  {where}{rule}\n      A: {_short(f['value_a'])}\n      B: {_short(f['value_b'])}"
                )
        typer.echo(f"run {view['run_id']}; observations a {r['obs_a_id']} b {r['obs_b_id']}")
    if fail_on_diff and judged is Verdict.DIFFERENT:
        raise typer.Exit(EXIT_DIFF)


@app.command()
def run(
    spec: SpecFile,
    keys: Annotated[Path | None, _o("Key file, one per line (default: the spec's key source).")] = None,
    samples: Annotated[Path | None, _o("File mode: a captured samples directory.")] = None,
    soak: Annotated[bool, _o("A soak run: a cycle every execution.cycleInterval.", "--soak")] = False,
    rolling: Annotated[bool, _o("Soak: read the key source again each cycle.", "--rolling")] = False,
    wait: Annotated[bool, _o("Process the run here until it ends.")] = True,
    fail_on_diff: FailOpt = False,
    config: ConfigOpt = None,
) -> None:
    """Start a batch (or soak) run; without --no-wait, process it here with an in-process worker. What the
    schema guard says of its tables at its creation is printed as warnings; a run it paused prints its
    lines and exits 2 (docs/mvp/PRECHECKS.md §11)."""
    from deltasleuth.runner import create_run, run_worker

    who = _local(config)
    with _errors():
        key_list = _read_keys(keys) if keys else None

    async def work(ctx: Context) -> tuple[repo.Row, repo.Row | None]:
        await _warned(ctx)
        _notify_warned(ctx.settings)
        spec_id, compiled = await _save_spec(ctx, spec, who)
        mode, settings = ("soak", {"rolling": rolling}) if soak else ("batch", None)
        run_id = await create_run(
            ctx, spec_id, mode, keys=key_list, samples_dir=samples, soak=settings, who=who
        )
        typer.echo(f"run {run_id} started")
        _unapproved(ctx, spec_id, compiled, run_id)
        async with ctx.pool.connection() as conn:
            created = await repo.get_run(conn, run_id)
        if created["status"] != "paused":  # one created paused prints its lines below
            for line in drift.warnings(created):
                typer.echo(f"warning: {line}", err=True)
        with _stopping() as stop:
            running = asyncio.create_task(run_worker(ctx, stop=stop, schedules=False)) if wait else None
            try:
                while True:
                    async with ctx.pool.connection() as conn:
                        row = await repo.get_run(conn, run_id)
                        if not wait or row["status"] in (*repo.TERMINAL, "paused"):
                            return row, await repo.get_cycle(conn, run_id, row["cycle"])
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), ctx.settings.worker.poll_seconds)
                    if stop.is_set():  # a SIGTERM: as Ctrl-C, once the worker below has stopped
                        raise KeyboardInterrupt
            finally:
                stop.set()
                if running:
                    await running

    row, ended = _in_context(config, work)
    guarded = row["status"] == "paused" and row["stop_reason"] == drift.SCHEMA_PAUSE
    if not wait and not guarded:
        typer.echo("processed by 'ds worker' or 'ds serve'")
        return
    c, why = row["counters"], row["stop_reason"] or row["error"]
    typer.echo(row["status"] + (f" ({why})" if why else ""))
    if refused := (row.get("draw") or {}).get("error"):  # its frame refused it (docs/mvp/SAMPLING.md §13)
        typer.echo(f"  {refused}")
    for line in drift.lines(row) if guarded else ():  # the schema guard paused it
        typer.echo(f"  {line}")
    if row["status"] in ("paused", "failed"):  # a paused run resumes from the UI or the API
        _fail(f"the run is {row['status']}")
    typer.echo("  " + ", ".join(f"{name} {c[name]}" for name in _COUNTERS))
    typer.echo("  " + ", ".join(f"{o} {n}" for o, n in sorted(c["by_outcome"].items()) if n))
    for line in sample.lines((ended or {}).get("sample") or {"strata": []}):  # docs/mvp/SAMPLING.md §14
        typer.echo(f"  {line}")
    if fail_on_diff and (c["different"] or c["outstanding"]):
        raise typer.Exit(EXIT_DIFF)


@app.command()
def runs(limit: int = 20, config: ConfigOpt = None) -> None:
    """List the latest runs, newest first, each with what its spec approval says.

    A run made in a build that enforces a licence also shows the licence it was made under; a build without
    a vendor key enforces none (docs/mvp/LICENCE.md §19).
    """

    async def work(ctx: Context) -> list[tuple[repo.Row, str, repo.Row | None]]:
        async with ctx.pool.connection() as conn:
            found = (await repo.list_runs(conn, limit=limit))[0]
            return [
                (r, approvals.label(await approvals.run_approval(conn, r)),
                 await repo.get_draw(conn, r["run_id"], r["cycle"]))
                for r in found
            ]  # fmt: skip

    for r, said, drawn in _in_context(config, work):
        c = r["counters"]
        found = f"  different {c['different']} of {c['total']}" if c else ""
        typer.echo(f"{r['run_id']}  {r['spec_name']} {r['spec_version']}  {r['mode']}  {r['status']}"
                   f"  cycle {r['cycle']}{found}")  # fmt: skip
        typer.echo(f"  {said}")
        if drawn is not None:  # a sampled run's current draw (docs/mvp/SAMPLING.md §14)
            strata = len(drawn["strata"])
            typer.echo(f"  sample {drawn['draw_id']}: {strata} {'stratum' if strata == 1 else 'strata'},"
                       f" {drawn['keys']:,} keys")  # fmt: skip
        elif r["params"].get("keys_source") == "hash_sample" and r.get("draw") is not None:
            if r["status"] == "running" and not r["draw"].get("error"):  # a stopped run's draw never runs
                typer.echo("  drawing sample")
            elif r["status"] == "paused":
                typer.echo("  sample to be drawn after resume")
        if label := licence.run_label(r):
            typer.echo(f"  {label}")
        if r["status"] == "paused" and r["stop_reason"] == licence.LICENCE_PAUSE:  # its next cycle waits
            typer.echo(f"  Paused for the licence at the end of cycle {r['cycle'] - 1}")
        if r["status"] == "paused" and r["stop_reason"] == drift.SCHEMA_PAUSE:
            for line in drift.lines(r)[:1]:  # the pause's (docs/mvp/PRECHECKS.md §10)
                typer.echo(f"  {line}")


@app.command()
def report(
    run_id: RunArg,
    out: OutOpt,
    kind: Annotated[list[str] | None, _o("A report the API serves, or summary (repeatable).")] = None,
    cycle: CycleOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Write a run's reports, masked, as <run>-<name>: the CSV exports and the PDF the API serves, or the P0
    summary (report.md and report.html).

    A run made in the unlicensed evaluation of a build that enforces a licence is written as
    <run>-unlicensed-evaluation-<name>; a build without a vendor key enforces none (docs/mvp/LICENCE.md
    §19).
    """
    from deltasleuth.reports import FORMATS
    from deltasleuth.reports.summary import summary_report

    kinds = kind or ["discrepancies.csv", "retry.csv", "coverage.csv", "report.pdf"]
    if unknown := sorted(set(kinds) - {*FORMATS, SUMMARY}):
        _fail(f"unknown report {', '.join(unknown)}; one of {', '.join([*FORMATS, SUMMARY])}")
    who = _local(config)

    async def work(ctx: Context) -> list[Path]:
        async with ctx.pool.connection() as conn:
            if (run := await repo.get_run(conn, run_id)) is None:
                raise LookupError(f"run {run_id} not found")
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for k in kinds:
            if k == SUMMARY:
                written += (await summary_report(ctx, run_id, out, cycle=cycle)).values()
            else:
                path = out / licence.file_name(run, k)
                path.write_bytes(await FORMATS[k][1](ctx, run_id, cycle))
                written.append(path)
            await _wrote(ctx, who, run_id, k, cycle)
        return written

    for path in _in_context(config, work):
        typer.echo(str(path))


# -- P0 onboarding tools ----------------------------------------------------------------------------


@app.command()
def profile(
    samples: Annotated[Path, _o("Samples directory (layout 1 or 2).")],
    out: OutOpt,
    name: Annotated[str, _o("Spec name for the draft.")],
    target_a: Annotated[str, _o("Target id of side a.")],
    target_b: Annotated[str, _o("Target id of side b.")],
    key_name: Annotated[str, _o("Name of the key.")],
    key_pattern: Annotated[str | None, _o("Regex keys must match.")] = None,
    request_a: Annotated[str | None, _o("Side a's request, e.g. 'GET /v1/x/{{ key }}'.")] = None,
    request_b: Annotated[str | None, _o("Side b's request, e.g. 'GET /v2/x/{{ key }}'.")] = None,
    targets: TargetsOpt = None,
) -> None:
    """Draft a spec from paired samples: draft.spec.yaml and profile_evidence.csv, for review."""
    from deltasleuth.connectors.files import FileSampleSource
    from deltasleuth.core.spec.documents import parse_yaml_text
    from deltasleuth.core.types import Side
    from deltasleuth.profiler import bootstrap
    from deltasleuth.reports.csv import render_csv

    with _errors():
        requests = {side: _request(t) for side, t in ((Side.A, request_a), (Side.B, request_b)) if t}
        source = FileSampleSource(samples)
        pairs = [(k, *(_document(source.observation(k, side)) for side in Side)) for k in source.keys()]
        draft = bootstrap(pairs, spec_name=name, target_a=target_a, target_b=target_b, key_name=key_name,
                          key_pattern=key_pattern, requests=requests or None)  # fmt: skip
        registry = load_targets(targets) if targets else None
        issues = validate_spec(parse_yaml_text(draft.yaml_text), targets=registry)
        out.mkdir(parents=True, exist_ok=True)
        (out / "draft.spec.yaml").write_text(draft.yaml_text, encoding="utf-8")
        rows = [row.as_dict() | {"scores": json.dumps(row.scores, default=str)} for row in draft.evidence]
        (out / "profile_evidence.csv").write_text(render_csv(_EVIDENCE, rows), newline="")
    for note in draft.notes:
        typer.echo(f"note: {note}")
    _issues(issues)
    typer.echo(f"profiled {len(pairs)} pair(s): {out / 'draft.spec.yaml'}, {out / 'profile_evidence.csv'}")
    if any(i.severity == "error" for i in issues):
        _fail("the draft does not validate; review it (see the issues above)")


def _request(text: str) -> dict[str, str]:
    """``"GET /v1/things/{{ key }}"`` as a spec request block."""
    method, _, path = text.strip().partition(" ")
    if method.upper() not in ("GET", "POST") or not path.strip().startswith("/"):
        raise ValueError("a request is 'GET /path/{{ key }}' or 'POST /path'")
    return {"method": method.upper(), "path": path.strip()}


def _document(obs: Any) -> Any:
    """A sample's JSON document; None without a 2xx JSON body (the profiler's "no document")."""
    from deltasleuth.core.jsonio import JSONParseError, ResourceLimitError, parse_json

    if obs is None or obs.body is None or not 200 <= (obs.http_status or 0) < 300:
        return None
    try:
        return parse_json(obs.body)[0]
    except (JSONParseError, ResourceLimitError):
        return None


@app.command()
def measure(
    run_id: RunArg,
    out: OutOpt,
    missing_keys: Annotated[
        Path | None, _o("Keys known not to exist: the basis of a proposed absence.")
    ] = None,
    cycle: CycleOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Measure the endpoints' observable contract from a run and propose capabilities for review."""
    from deltasleuth.reports.measure import MEASURE_NAME, PROPOSAL_NAME, measure_run

    who = _local(config)
    with _errors():
        missing = _read_keys(missing_keys) if missing_keys else ()

    async def work(ctx: Context) -> dict[str, Any]:
        measured = await measure_run(ctx, run_id, out, cycle=cycle, missing_keys=missing)
        await _wrote(ctx, who, run_id, MEASURE_NAME, cycle)
        return measured

    found = _in_context(config, work)
    flags = sum(len(p["flags"]) for proposals in found["proposed_capabilities"].values() for p in proposals)
    typer.echo(f"{flags} item(s) to review: {out / MEASURE_NAME}, {out / PROPOSAL_NAME}")


@app.command()
def adjudicate(
    run_id: RunArg,
    out: OutOpt,
    limit: Annotated[int, _o("Keys to select.")] = 50,
    salt: Annotated[str, _o("Selection salt.")] = "p0-adjudication",
    force: Annotated[bool, _o("Archive a reviewed sheet and write a new one.", "--force")] = False,
    cycle: CycleOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Select up to --limit stratified keys and write the reviewer sheet (adjudication.csv)."""
    from deltasleuth.reports import adjudicate as adj

    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        selection = await adj.adjudicate(ctx, run_id, out, cycle=cycle, limit=limit, salt=salt, force=force)
        await _wrote(ctx, who, run_id, adj.SHEET_NAME, selection["cycle"])
        return selection

    chosen = _in_context(config, work)
    if chosen["archived_sheet"]:
        typer.echo(f"the reviewed sheet was archived as {chosen['archived_sheet']}")
    typer.echo(f"selected {len(chosen['keys'])} of {chosen['candidates']} key(s): {out / adj.SHEET_NAME}")
    for what, left_out in chosen["unrepresented"].items():
        if left_out:
            typer.echo(f"  not represented: {len(left_out)} {what}; raise --limit", err=True)


@app.command()
def score(out: OutOpt, config: ConfigOpt = None) -> None:
    """Score the filled reviewer sheet against the engine and check the P0 exit criterion."""
    from deltasleuth.reports.adjudicate import SCORE_NAME, SELECTION_NAME
    from deltasleuth.reports.adjudicate import score as score_sheet
    from deltasleuth.reports.data import read_json

    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        scored = await score_sheet(ctx, out)
        await _wrote(ctx, who, scored["run_id"], SCORE_NAME, read_json(out / SELECTION_NAME)["cycle"])
        return scored

    s = _in_context(config, work)
    typer.echo(f"adjudicated {s['adjudicated']} of {s['rows']}; agreement {s['agreement_rate']}")
    for d in s["disagreements"]:
        typer.echo(
            f"  disagreement {d['key']}: engine {d['engine_verdict']}, reviewer {d['reviewer_verdict']}"
        )
    passed = s["exit"]["passed"]
    typer.echo("P0 exit: PASSED" if passed else "P0 exit: NOT YET (" + "; ".join(s["exit"]["reasons"]) + ")")


@app.command("llm-probe")
def llm_probe(
    profile: Annotated[str | None, _o("Profile id (default: the settings' llm_profile).")] = None,
    environment: Annotated[str | None, _o("Environment (default: the settings' llm_environment).")] = None,
    out: Annotated[Path | None, _o("Also write the JSON report into this directory.")] = None,
    config: ConfigOpt = None,
) -> None:
    """Probe an LLM profile's capabilities through the ds_llm port (fail-closed egress)."""
    from deltasleuth.llm.client import LLMClient
    from deltasleuth.llm.policy import EgressPolicy
    from deltasleuth.llm.probe import probe_capabilities
    from deltasleuth.llm.profiles import load_profiles

    async def main() -> Any:
        settings = load_settings(config)
        if settings.llm_profiles_file is None:
            raise SettingsError("AI is off: the settings name no llm_profiles_file")
        chosen = load_profiles(settings.llm_profiles_file).get(profile or settings.llm_profile or "")
        egress = EgressPolicy(environment or settings.llm_environment, settings.llm_external_data_classes)
        async with LLMClient.from_profile(chosen, egress=egress) as client:
            return await probe_capabilities(client)

    probed = _run(main())
    typer.echo(f"profile {probed.profile_id}: " + ", ".join(f"{r.name}={r.status}" for r in probed.results))
    if out:
        with _errors():
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"llm_probe_{probed.profile_id}.json"
            path.write_text(probed.to_json() + "\n", encoding="utf-8")
        typer.echo(str(path))


# -- administration: the host administrator's channel (docs/mvp/AUTH.md §4.2) ---------------------


@token_app.command("create")
def token_create(
    name: Annotated[
        str,
        _o(
            "Token name, audited as its actor_name: 3 to 64 of a-z, 0-9, '.', '_' and '-', starting with a"
            " letter or a digit."
        ),
    ],
    owner: Annotated[str, _o("The team or contact that owns the token.")],
    role: Annotated[str, _o("viewer, operator or spec_author.")],
    owner_user: Annotated[str | None, _o("The accountable person's user id (U-…).")] = None,
    days: Annotated[int | None, _o("Days it lives (default: 90, at most auth.token_max_days).")] = None,
    config: ConfigOpt = None,
) -> None:
    """Create an API token. Only the token goes to stdout, this once."""
    from deltasleuth.auth import tokens

    who = _local(config)

    async def work(ctx: Context) -> tuple[repo.Row, str]:
        auth = ctx.settings.auth
        async with repo.audited(ctx.pool) as (conn, audit):
            row, token = await tokens.create(
                conn,
                who,
                name=name,
                owner=owner,
                role=role,
                max_days=auth.token_max_days if auth is not None else tokens.DEFAULT_DAYS,
                days=days,
                owner_user_id=owner_user,
            )
            detail = {field: row[field] for field in ("name", "owner", "owner_user_id", "role")}
            detail["expires_at"] = row["expires_at"].isoformat()
            await audit(who, "token.create", row["token_id"], detail)
        return row, token

    row, token = _in_context(config, work)
    typer.echo(
        f"token {_named(row['token_id'], row['name'])}, {row['role']}, expires {_at(row['expires_at'])};"
        " it is shown only now",
        err=True,
    )
    typer.echo(token)


@token_app.command("revoke")
def token_revoke(token_id: Annotated[str, _a("The token id (T-…).")], config: ConfigOpt = None) -> None:
    """Revoke an API token; its sign-in sessions end with it."""
    who = _local(config)

    async def work(ctx: Context) -> repo.Row:
        async with repo.audited(ctx.pool) as (conn, audit):
            if await repo.token_by_id(conn, token_id) is None:
                raise LookupError(f"token {token_id} not found")
            row = await repo.revoke_token(conn, token_id, who.actor)
            if row is None:
                raise ValueError(f"token {token_id} is already revoked")
            await audit(who, "token.revoke", token_id)
        return row

    row = _in_context(config, work)
    typer.echo(f"revoked token {_named(token_id, row['name'])}")


@token_app.command("list")
def token_list(config: ConfigOpt = None) -> None:
    """List the API tokens, newest first, never their secrets."""

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            return await repo.list_tokens(conn)

    for t in _in_context(config, work):
        person = f" {_named(t['owner_user_id'], t['owner_user_name'])}" if t["owner_user_id"] else ""
        typer.echo(
            f"{_named(t['token_id'], t['name'])}  {t['role']}  {t['state']}  expires {_at(t['expires_at'])}"
            f"  owner {t['owner']!r}{person}  created by {_named(t['created_by'], t['created_by_name'])}"
            f"  last used {_at(t['last_used_at'])}"
        )


@session_app.command("revoke")
def session_revoke(
    session: Annotated[str | None, _o("One session (W-…).")] = None,
    user: Annotated[str | None, _o("Every session of a user (U-…).")] = None,
    everyone: Annotated[bool, _o("Every session.", "--all")] = False,
    config: ConfigOpt = None,
) -> None:
    """Revoke sign-in sessions: one, a user's or every one. Each ends at its next request."""
    if (session is not None) + (user is not None) + everyone != 1:
        _fail("name one of --session W-…, --user U-… or --all")
    who = _local(config)

    async def work(ctx: Context) -> int:
        async with repo.audited(ctx.pool) as (conn, audit):
            if user is not None and await repo.get_user(conn, user) is None:
                raise LookupError(f"user {user} not found")
            count = await repo.revoke_sessions(
                conn, who.actor, session_id=session, user_id=user, everyone=everyone
            )
            if session is not None and not count:  # unknown, or already ended
                raise LookupError(f"session {session} not found")
            await audit(who, "session.revoke", session or user or "all", {"count": count})
        return count

    typer.echo(f"revoked {_in_context(config, work)} session(s)")


@users_app.callback()
def users(
    command: typer.Context,
    as_json: Annotated[bool, _o("Print the users as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the people who signed in through the IdP, with the roles of their last sign-in; or disable
    or enable one."""
    if command.invoked_subcommand is not None:
        if as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            return await repo.list_users(conn)

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for u in rows:
        roles = ",".join(role for role in ROLES if role in u["roles"]) or "no role"
        by = _named(u["disabled_by"], u["disabled_by_name"])
        disabled = f"  disabled {_at(u['disabled_at'])} by {by}" if u["disabled_at"] else ""
        typer.echo(
            f"{_named(u['user_id'], u['username'])}  {u['display_name'] or '-'}  {roles}"
            f"  last sign-in {_at(u['last_login_at'])}{disabled}"
        )


@users_app.command("disable")
def users_disable(
    user_id: Annotated[str, _a("The user id (U-…).")],
    revoke_tokens: Annotated[
        bool, _o("Also revoke every live token the user created or owns.", "--revoke-tokens")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Disable a user and end its sessions. Lists the tokens it created or owns, which outlive it
    unless revoked."""
    who = _local(config)

    async def work(ctx: Context) -> tuple[repo.Row, list[repo.Row], list[str]]:
        async with repo.audited(ctx.pool) as (conn, audit):
            if await repo.get_user(conn, user_id) is None:
                raise LookupError(f"user {user_id} not found")
            row = await repo.set_user_disabled(conn, user_id, who.actor)
            if row is None:
                raise ValueError(f"user {user_id} is already disabled")
            revoked = await repo.revoke_user_tokens(conn, user_id, who.actor) if revoke_tokens else []
            listed = await repo.user_tokens(conn, user_id)
            detail = {"sessions_revoked": row["sessions_revoked"], "tokens_revoked": revoked}
            await audit(who, "user.disable", user_id, detail)
        return row, listed, revoked

    row, listed, revoked = _in_context(config, work)
    typer.echo(
        f"disabled {_named(user_id, row['username'])}: {row['sessions_revoked']} session(s) and"
        f" {len(revoked)} token(s) revoked"
    )
    for t in listed:
        typer.echo(f"  token {_named(t['token_id'], t['name'])}  {t['role']}  {t['state']}")


@users_app.command("enable")
def users_enable(user_id: Annotated[str, _a("The user id (U-…).")], config: ConfigOpt = None) -> None:
    """Let a disabled user sign in again. Revoked tokens stay revoked."""
    who = _local(config)

    async def work(ctx: Context) -> repo.Row:
        async with repo.audited(ctx.pool) as (conn, audit):
            user = await repo.get_user(conn, user_id)
            if user is None:
                raise LookupError(f"user {user_id} not found")
            if await repo.set_user_disabled(conn, user_id, None) is None:
                raise ValueError(f"user {user_id} is not disabled")
            await audit(who, "user.enable", user_id)
        return user

    user = _in_context(config, work)
    typer.echo(f"enabled {_named(user_id, user['username'])}; revoked tokens stay revoked")


_SEEN = (("request", "request_id"), ("session", "session_id"), ("from", "client_addr"))


def _when(text: str | None, option: str) -> datetime | None:
    """An ISO 8601 time, UTC when it names no offset."""
    if text is None:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{option} {text!r} is not an ISO 8601 time, e.g. 2026-09-26T08:00:00Z") from None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


@audit_app.callback()
def audit_log(
    command: typer.Context,
    actor: Annotated[str | None, _o("Only this actor.")] = None,
    action: Annotated[str | None, _o("Only this action.")] = None,
    obj: Annotated[str | None, _o("Only this object.", "--object")] = None,
    session: Annotated[str | None, _o("Only this session's rows (W-…).")] = None,
    since: Annotated[str | None, _o("From this time on (ISO 8601; UTC without an offset).")] = None,
    until: Annotated[str | None, _o("Before this time (ISO 8601; UTC without an offset).")] = None,
    limit: Annotated[int | None, typer.Option(min=1, max=500, help="Rows to show (default 100).")] = None,
    as_json: Annotated[bool, _o("Print the rows as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Read the audit log, newest first, each filter exact; or verify its hash chain. The read is audited
    with its filters."""
    if command.invoked_subcommand is not None:
        options = (actor, action, obj, session, since, until, limit, config)
        if as_json or any(option is not None for option in options):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    who = _local(config)
    with _errors():
        start, end = _when(since, "--since"), _when(until, "--until")
    given = {"actor": actor, "action": action, "object": obj, "session_id": session}
    given |= {"since": start and start.isoformat(), "until": end and end.isoformat()}
    filters = {name: value for name, value in given.items() if value is not None}

    async def work(ctx: Context) -> list[repo.Row]:
        async with repo.audited(ctx.pool) as (conn, audit):
            rows, _ = await repo.list_audit(
                conn,
                actor=actor,
                action=action,
                obj=obj,
                session_id=session,
                since=start,
                until=end,
                limit=limit or 100,
            )
            await audit(who, "audit.read", "audit", {"filters": filters})
        return rows

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for r in rows:
        seen = "".join(f"  {label} {r[column]}" for label, column in _SEEN if r[column] is not None)
        detail = json.dumps(r["detail"], sort_keys=True, default=str)
        typer.echo(
            f"{r['id']}  {_at(r['at'])}  {_named(r['actor'], r['actor_name'])}  {r['action']}  {r['object']}"
            f"  {detail}{seen}"
        )


@audit_app.command("verify")
def audit_verify(
    siem: Annotated[
        Path | None,
        _o("A SIEM export of the deltasleuth.audit lines (JSON lines): compare the log with it both ways."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Check the audit log's hash chain (docs/mvp/PACK.md §4.2): contiguous rows, both links, each hash,
    each row's text against its columns, every row chained; with --siem, the rows its lines span. Prints
    the head, for the daily ticket; a break exits 2 naming the first row that fails."""
    with _errors():
        lines = None if siem is None else chain.read_siem(siem)

    async def work(ctx: Context) -> dict[str, Any]:
        async with ctx.pool.connection() as conn:
            return await chain.verify(conn, lines)

    done = _in_context(config, work)
    upgraded = f", 1 to {done['upgrade_seq']} chained by 009" if done["upgrade_seq"] else ""
    typer.echo(f"chain: {done['rows']:,} row(s) verified{upgraded}")
    if (matched := done["siem"]) is not None:
        span = f": rows {matched['first']} to {matched['last']}" if matched["lines"] else ""
        typer.echo(f"siem: {matched['lines']:,} line(s) match{span}")
    head = done["head"]
    typer.echo(f"head: row {head['seq']} {head['hash']}" if head else "head: none (the audit log is empty)")


# -- evidence packs (docs/mvp/PACK.md §3, §7.3) ---------------------------------------------------


@packs_app.callback()
def packs_list(
    command: typer.Context,
    run_id: Annotated[str | None, _o("Only this run's packs (R-…).", "--run")] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the evidence packs, newest first; or file the signing key's register entry, sign the
    revocation list, download a pack or verify one offline. Only a signed-in person issues a pack, on the
    Run screen (docs/mvp/PACK.md)."""
    if command.invoked_subcommand is not None:
        if as_json or run_id is not None or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            return await repo.pages(repo.list_packs, conn, run_id=run_id)

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for p in rows:
        line = f"{p['pack_id']}  {p['status']}  cycle {p['cycle']} of run {p['run_id']}"
        line += f"  sign-off {p['signoff_id']}"
        line += f"  issued by {_named(p['issued_by'], p['issued_by_name'])} {_at(p['issued_at'])}"
        line += f"  key {p['key_fingerprint']}  sums {p['sums_sha256']}"
        if p["revoked_at"] is not None:
            by = _named(p["revoked_by"], p["revoked_by_name"])
            line += f"  revoked by {by} {_at(p['revoked_at'])}: {p['revoke_reason']!r}"
        if p["purged_at"] is not None:
            line += f"  stored copy purged {_at(p['purged_at'])}"
        typer.echo(line)
    if not rows:
        typer.echo("no evidence pack")


def _signer(config: Path | None) -> tuple[str, PackSigner]:
    """The installation's name and its signing key, from the pack settings."""
    from deltasleuth.reports import pack

    settings = load_settings(config)
    if settings.packs is None:
        raise SettingsError(pack.NOT_CONFIGURED)
    return settings.packs.installation, PackSigner.load(settings.packs.signing_key_ref)


def _write(out: Path, files: dict[str, bytes]) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return [out / name for name in files]


@packs_app.command("key")
def packs_key(out: OutOpt, config: ConfigOpt = None) -> None:
    """Write the signing key's entry for the site's key register: fingerprint.txt (the installation and
    the key's fingerprint) and procedure.txt (the verification procedure every pack's VERIFY.txt must
    match), to be filed, dated, when the key is generated (docs/mvp/PACK.md §3.1)."""
    from deltasleuth.reports import pack

    with _errors():
        installation, signer = _signer(config)
        _write(out, pack.key_files(installation, signer))
    typer.echo(
        f"wrote fingerprint.txt and procedure.txt to {out}: file both in the key register, dated"
        f" (installation {installation}, fingerprint {signer.fingerprint})"
    )


@packs_app.command("revocations")
def packs_revocations(out: OutOpt, config: ConfigOpt = None) -> None:
    """Write the signed revocation list: revoked.txt (every revoked pack of every key used), its signature
    and the current signing key, which a pack's step 8 checks."""
    from deltasleuth.reports import pack

    with _errors():
        _, signer = _signer(config)
    files = _in_context(config, lambda ctx: pack.revocations(ctx, signer))
    with _errors():
        _write(out, files)
    revoked = files["revoked.txt"].count(b"\n") - 2
    typer.echo(f"wrote revoked.txt ({revoked} revoked pack(s)), revoked.txt.sig and signing-key.pem to {out}")


@packs_app.command("download")
def packs_download(
    pack_id: Annotated[str, _a("The pack id (P-…).")], out: OutOpt, config: ConfigOpt = None
) -> None:
    """Write a pack's stored ZIP (P-….zip) into the directory, audited once written. A purged cycle's pack
    is gone from the store: copies downloaded before still verify."""
    from deltasleuth.reports import pack

    who = _local(config)

    async def work(ctx: Context) -> Path:
        found, data = await pack.stored(ctx, pack_id)
        (path,) = _write(out, {f"{pack_id}.zip": data})
        await pack.downloaded(ctx, who, found)
        return path

    path = _in_context(config, work)
    typer.echo(f"wrote {path}: check it with 'ds packs verify {path.name} --fingerprint <the register's>'")


@packs_app.command("verify")
def packs_verify(
    zip_file: Annotated[Path, _a("The pack (P-….zip).")],
    fingerprint: Annotated[
        str | None, _o("The signing key's fingerprint your key register lists for the installation.")
    ] = None,
    revocations: Annotated[
        Path | None,
        _o("The custodian's latest revoked.txt, revoked.txt.sig and signing-key.pem (a directory)."),
    ] = None,
) -> None:
    """Check a pack offline, as its VERIFY.txt does, without settings: steps 0 to 6 and 8 (0 against this
    build's procedure) and a sampled pack's hand check of its draw, each ok or FAILED, and the objects step
    7 checks against your SIEM. Exits 1 on a failure. A zip over 64 entries or 200 MiB, compressed over
    100:1, with a symlink, an absolute path or '..', a name twice, a manifest it cannot read, or of another
    format is refused."""
    from deltasleuth.reports import pack_verify

    if fingerprint is None:
        _fail("--fingerprint is required: the fingerprint your key register lists for the installation")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
        _fail("--fingerprint must be 64 hex characters (a SHA-256)")
    with _errors():
        steps = pack_verify.verify(zip_file, fingerprint.lower(), revocations)
    for step in steps:
        typer.echo(step.line)
    if any(step.ok is False for step in steps):
        raise typer.Exit(EXIT_DIFF)


# -- the licence (docs/mvp/LICENCE.md §12) ---------------------------------------------------------


@licence_app.callback()
def licence_status(
    command: typer.Context,
    as_json: Annotated[
        bool, _o("Print GET /v1/licence's body and the audit head: the true-up.", "--json")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Show the installation's licence: its id, the licence in force, its state and warnings, what it
    allows and the pairs counted with their first runs and seats; or print the id, show a licence file
    offline, install one or withdraw one that has not started. Exits 0 in every state. A build without a
    vendor key needs no licence: it says so, and shows and installs none (docs/mvp/LICENCE.md §19)."""
    if command.invoked_subcommand is not None:
        if as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    async def work(ctx: Context) -> dict[str, Any]:
        found = await licence.status(ctx)
        if as_json:
            async with ctx.pool.connection() as conn:
                found["audit_head"] = await repo.audit_head(conn)
        return found

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    named, warnings = found["licence"], found["warnings"]
    typer.echo(f"installation {found['installation_id'] or '-'}")
    if found["state"] == "unknown":  # not an evaluation: an installed licence may be in force
        typer.echo("licence unknown: the standing could not be read (see the server log)")
    elif found["state"] == licence.OPEN:
        typer.echo("licence none: this build needs no licence")
    elif named is None:
        typer.echo("licence none: an unlicensed evaluation")
    else:
        typer.echo(
            f"licence {named['licence_id']}: {named['customer']} (order {named['order']}), {named['tier']},"
            f" {_pairs(named['pairs'])}, {named['starts']} to {named['ends']}"
        )
    typer.echo(f"state {found['state']}" + (f": {warnings[0]}" if warnings else ""))
    for line in warnings[1:]:
        typer.echo(f"  {line}")
    if (allows := found["allows"]) is not None:
        typer.echo("allows: " + ", ".join(f"{name} {'yes' if ok else 'no'}" for name, ok in allows.items()))
    for pair in (found["pairs"] or {"counted": []})["counted"]:
        targets, seat = ", ".join(pair["targets"]) or "-", "seated" if pair["seated"] else "no seat"
        typer.echo(f"pair {pair['name']}  first run {_at(pair['first_at'])}  {targets}  {seat}")


def _pairs(pairs: int | None) -> str:
    return "unlimited pairs" if pairs is None else f"{pairs} pair{'s' if pairs != 1 else ''}"


def _licence_file(path: str) -> tuple[bytes, str]:
    """A licence file's bytes (one past the most a licence holds, so that a longer file is refused) and its
    name in messages; ``-``: the standard input."""
    if path == "-":
        return typer.get_binary_stream("stdin").read(licence.MAX_BYTES + 1), "stdin"
    with Path(path).open("rb") as file:
        return file.read(licence.MAX_BYTES + 1), path


@licence_app.command("id")
def licence_id(config: ConfigOpt = None) -> None:
    """Print this installation's id alone, which a vendor build's licence names; opening the store
    makes it (migration 010)."""

    async def work(ctx: Context) -> str:
        async with ctx.pool.connection() as conn:
            return (await repo.licence_facts(conn))["installation_id"]

    typer.echo(_in_context(config, work))


@licence_app.command("show")
def licence_show(path: Annotated[str, _a("A licence file (LI-….txt), or - for the standard input.")]) -> None:
    """Show a licence file's fields and check its signature offline, without settings; exit 2 when this
    build would refuse it, as a build without a vendor key refuses every file: it needs no licence."""
    with _errors():
        data, name = _licence_file(path)
        found = licence.parse(data, name=name)
    typer.echo(found.body.decode(), nl=False)
    typer.echo(f"verifies with key {found.key[:12]}… (trusted by this build)")


@licence_app.command("install")
def licence_install(
    path: Annotated[str, _a("The licence file as received, or - for the standard input.")],
    config: ConfigOpt = None,
) -> None:
    """Install a licence file for this installation: it applies at once in every process, or from its
    starts. Installing it again changes nothing. It resumes nothing: resume each run paused for the
    licence on its Run screen. A build without a vendor key refuses every file (exit 2): it needs no
    licence."""
    who = _local(config)
    with _errors():
        data, name = _licence_file(path)

    async def work(ctx: Context) -> tuple[dict[str, Any], list[str]]:
        from deltasleuth.runner.runs import resumable

        done, paused = await licence.install(ctx, data, who=who, name=name), []
        if done["applies"] == "now" and done["after"].refusal("run") is None:
            paused = await resumable(ctx)
        return done, paused

    done, paused = _in_context(config, work)
    found, before, after, applies = done["licence"], done["before"], done["after"], done["applies"]
    if not done["installed"]:
        typer.echo(f"licence {found.licence_id} is installed already")
        return
    installed = (
        f"installed {found.licence_id} ({found.tier}, {found.customer}, {found.starts} to {found.ends},"
        f" {_pairs(found.pairs)})"
    )
    if applies == "now":
        typer.echo(f"{installed}: in force now")
    elif applies.startswith("from "):
        named = after.licence
        until = "an unlicensed evaluation" if named is None else f"{named.licence_id} applies"
        typer.echo(f"{installed}: in force from {found.starts}; {until} until then")
    else:  # started, while a later-starting licence is in force
        typer.echo(f"{installed}: not in force; {applies.removeprefix('no: ')}")
    if applies == "now":
        changes = [
            f"{what} {was} -> {now}"
            for what, was, now in (
                ("tier", _tier(before), _tier(after)),
                ("pairs", _limit(before.limit), _limit(after.limit)),
                ("state", before.state, after.state),
            )
            if was != now
        ]
        if changes:
            typer.echo(f"changes now: {'; '.join(changes)}")
        if unseated := [name for name in after.names if name not in after.seats]:
            typer.echo(
                f"{', '.join(unseated)} have no seat: no new run, and their soaks pause at their next cycle"
            )
    if found.tier != "enterprise" and after.sso:
        typer.echo("runs under SSO stay refused (M5)")
    if paused:
        typer.echo(
            f"{len(paused)} run(s) paused for the licence: {', '.join(paused)}; resume each on its Run screen"
        )


def _tier(current: licence.Standing) -> str:
    return "evaluation" if current.licence is None else current.licence.tier


def _limit(limit: int | None) -> str:
    return "unlimited" if limit is None else str(limit)


@licence_app.command("withdraw")
def licence_withdraw(
    licence_id: Annotated[str, _a("The licence id (LI-…).")],
    reason: Annotated[str, _o(REASON)],
    config: ConfigOpt = None,
) -> None:
    """Withdraw a genuine but wrong licence before it starts; a started licence is only replaced by a new
    one, so no withdrawal returns the installation to an evaluation."""
    who = _local(config)
    _in_context(config, lambda ctx: licence.withdraw(ctx, licence_id, reason, who=who))
    typer.echo(f"withdrew licence {licence_id}: it is not installed again")


# -- sign-off sampling (docs/mvp/SAMPLING.md §6, §14) ------------------------------------------------


@samples_app.callback()
def samples_list(
    command: typer.Context,
    spec_name: Annotated[
        str | None, _o("Only this spec name's ledger (its newest version's population).", "--spec")
    ] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List every draw of a sign-off sample, or a spec name's ledger (every spec's draws of its
    population), in commit order: status, whether it blocks a later sign-off, and its close. The CLI
    closes no draw: a signed-in person does, with a note of the fix (POST /v1/samples/close)."""
    if command.invoked_subcommand is not None:
        if spec_name is not None or as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            if spec_name is None:
                return await repo.pages(repo.draws, conn)
            stored = await repo.newest_spec(conn, spec_name)
            if stored is None:
                raise LookupError(f"spec {spec_name} not found")
            found = compile_spec(stored["body"]).sample
            return [] if found is None else await repo.ledger(conn, found.population)

    rows = [{n: v for n, v in d.items() if n not in ("strata", "frame")} for d in _in_context(config, work)]
    for d in rows:
        d["blocking"] = sample.blocking(d)
    if as_json:
        typer.echo(_json(rows))
        return
    for d in rows:
        line = (f"{d['draw_id']}  spec {d['spec_name']}  run {d['run_id']} cycle {d['cycle']}  {d['method']}"
                f"  {d['keys']:,} keys  drawn {_at(d['drawn_at'])}  {d['status']}")  # fmt: skip
        if d["status"] == "failed":
            line += f" ({', '.join(d['failed_strata'])})"
        if d["closed_at"] is not None:
            by = _named(d["closed_by"], d["closed_by_name"])
            line += f"  closed by {by} {_at(d['closed_at'])}: {d['close_note']!r}"
        typer.echo(line + ("  blocking: close it with the fix" if d["blocking"] else ""))


@samples_app.command("size")
def samples_size(
    tier: Annotated[str, _o("The stratum's tier: standard (≤ 0.1%) or high (≤ 0.01%).", "--tier")],
    failures: Annotated[int, typer.Option(min=0, help="Discrepant keys it may still pass with.")] = 0,
) -> None:
    """Print how many cleanly compared keys a sign-off stratum needs to pass the one-sided 95% bound with
    at most N discrepant, and how many to draw, without settings: fix the size in the spec
    before any key is compared."""
    from deltasleuth.core import sampling

    if tier not in sampling.TARGETS:
        _fail(f"--tier is standard or high, not {tier!r}")
    target = sampling.TARGETS[tier]
    with _errors():
        n = sampling.needed(failures, target)
    margin = sampling.percent(sampling.MARGIN)
    typer.echo(
        f"{tier} (≤ {sampling.percent(target)}, one-sided 95%): {n:,} cleanly compared keys pass with at most"
        f" {failures:,} discrepant; draw {sampling.attempted(n):,} ({margin} margin); fix the size before any"
        " key is compared, never extend it"
    )


# -- labelled accuracy (docs/mvp/ACCURACY.md §10) -----------------------------------------------------


@app.command()
def labellings(
    run_id: Annotated[str | None, _o("Only this run's labellings (R-…).", "--run")] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the labellings of ended cycles, newest first, each in one line: what its labels count against
    its truth source and whether a sign-off attests it. The CLI draws and labels nothing: an operator does,
    on the Run screen or over the API (POST /v1/runs/{id}/cycles/{n}/labellings)."""
    from deltasleuth.reports import accuracy

    async def work(ctx: Context) -> list[dict[str, Any]]:
        async with ctx.pool.connection() as conn:
            if run_id is not None and await repo.get_run(conn, run_id) is None:
                raise LookupError(f"run {run_id} not found")
            rows = await repo.list_labellings(conn, run_id)
            signoffs = await repo.pages(repo.list_approvals, conn, kind="signoff", run_id=run_id)
        attested = accuracy.attesting(signoffs)
        return [accuracy.listed(row, attested.get(row["labelling_id"])) for row in rows]

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    for item in found:
        typer.echo(item["summary"])


# -- alerts and notifications (docs/mvp/ALERTS.md §10, §11) ------------------------------------------

MUTE_REASON = "Why, in 1 to 1000 characters. It is audited and reaches the SIEM: never personal data."


def _notify_warned(settings: Settings) -> None:
    """Warn of each channel with a secret a send cannot read now (docs/mvp/ALERTS.md §10): ``serve``,
    ``worker`` and ``run`` only, never failing them, as its deliveries retry until it is readable."""
    from deltasleuth.connectors.notify import unreadable

    for name, why in unreadable(settings.notify) if settings.notify is not None else []:
        retrying = "its deliveries retry until it is readable"
        typer.echo(f"warning: notify channel {name}: {why}; {retrying}", err=True)


def _count(value: Any) -> str:
    return "not counted" if value is None else f"{value:,}" if isinstance(value, int) else str(value)


def _last(row: repo.Row) -> str:
    """A delivery's newest attempt, or a channel's: its error (or ok) and its HTTP or SMTP status."""
    if not (row["last_error"] or row["last_status"]):
        return ""
    return f"last {row['last_error'] or 'ok'}" + (f" ({row['last_status']})" if row["last_status"] else "")


@notify_app.callback()
def notify_status(
    command: typer.Context,
    as_json: Annotated[bool, _o("Print GET /v1/notifications's body.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show the notifications: the banners, the feed's cursor, each channel with its counts and its newest
    attempt, the rules and the active mutes; or check the channels, explain a cycle, list deliveries, send a
    test, send dead deliveries again, mute or unmute. Channels and rules are declared in deltasleuth.yaml
    (notify) and apply at the next restart."""
    if command.invoked_subcommand is not None:
        if as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    found = _in_context(config, notifications.status)
    if as_json:
        typer.echo(_json(found))
        return
    if not found["enabled"]:
        typer.echo(notifications.OFF)
        return
    for line in found["banners"]:
        typer.echo(line)
    cursor = found["cursor"]
    unread = f"; unread since {_at(cursor['behind'])}" if cursor["behind"] else ""
    notified = f"audit row {cursor['seq'] or '-'} of {cursor['head']} notified"
    typer.echo(f"label {found['label']}: {notified}{unread}")
    for c in found["channels"]:
        last = f"; {_last(c)}" if _last(c) else ""
        typer.echo(
            f"channel {c['name']} ({c['kind']}, {c['host']}): {c['pending']:,} pending; in 24 h"
            f" {c['sent_24h']:,} sent, {c['dead_24h']:,} dead, {c['suppressed_24h']:,} held back{last}"
        )
    for r in found["rules"]:
        typer.echo(f"rule {r['name']}: {', '.join(r['events'])} to {', '.join(r['channels'])}")
    for m in found["mutes"]:
        typer.echo(
            f"{m['mute_id']}  {m['scope']} {m['target']}  until {_at(m['until'])}  by {m['created_by']}:"
            f" {m['reason']!r}"
        )


@notify_app.command("check")
def notify_check(config: ConfigOpt = None) -> None:
    """Check each channel without a database and without sending: read its secrets and resolve and vet its
    destination, or the proxy it sends through, as a send does. Exit 2 names each failing check; the URL a
    url_ref file holds is never printed."""
    from deltasleuth.connectors.notify import check

    with _errors():
        lines = check(notifications.on(load_settings(config)))
    for line in lines:
        typer.echo(line, err=line.startswith("error:"))
    if any(line.startswith("error:") for line in lines):
        raise typer.Exit(EXIT_USAGE)


@notify_app.command("explain")
def notify_explain(
    run_id: RunArg,
    cycle: Annotated[int | None, _o("An ended cycle (default: the latest).")] = None,
    as_json: Annotated[bool, _o("Print it as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show an ended cycle's digest as the notifier computes it now, and each rule's verdict on its
    cycle.ended; writes nothing."""
    found = _in_context(config, lambda ctx: notifications.explain(ctx, run_id, cycle))
    if as_json:
        typer.echo(_json(found))
        return
    f, c = found["facts"], found["cycle"]
    if found["purged"]:
        typer.echo(f"cycle {c} is purged: root causes, key changes and criticals are unavailable")
    typer.echo(f"cycle {c} of run {run_id} ({found['spec_name']}, {found['mode']})")
    typer.echo("  " + ", ".join(f"{name} {_count(value)}" for name, value in f["counters"].items()))
    for name, value in f["levels"].items():
        before = f["previous"][name]
        was = f"cycle {before['cycle']}: {before['value']}" if before else "held by no earlier cycle"
        typer.echo(f"  {name} {_count(value)} ({was})")
    if c == 1:  # against an empty baseline
        typer.echo(
            f"  in the first cycle: {_count(f['new_root_causes'])} root causes and"
            f" {_count(f['newly_discrepant'])} discrepant keys observed"
        )
    else:
        samples = " (between samples)" if f["between_samples"] else ""
        since = f"since cycle {f['baseline_from']}" if f["baseline_from"] else "in the cycles retained"
        typer.echo(
            f"  root causes {since}{samples}: {_count(f['new_root_causes'])} new,"
            f" {_count(f['gone_root_causes'])} no longer observed"
        )
        typer.echo(
            f"  keys read in both cycles {c - 1} and {c}: {_count(f['newly_discrepant'])} newly discrepant,"
            f" {_count(f['no_longer_discrepant'])} no longer"
        )
    typer.echo(f"  unavailable: {', '.join(f['unavailable']) or '-'}")
    for rule, verdict in found["rules"].items():
        typer.echo(f"rule {rule}: {verdict}")


@notify_app.command("deliveries")
def notify_deliveries(
    channel: Annotated[str | None, _o("Only this channel's.")] = None,
    state: Annotated[str | None, _o("Only this state: pending, sent, dead or suppressed.")] = None,
    run_id: Annotated[str | None, _o("Only this run's.", "--run")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500, help="At most this many.")] = 50,
    as_json: Annotated[bool, _o("Print them as JSON, with their stored bodies.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List deliveries newest first: channel, event, severity, state, attempts, the audit row and the last
    error; their stored bodies (an e-mail's without addresses) with --json."""
    if state is not None and state not in ("pending", "sent", "dead", "suppressed"):
        _fail("--state is one of pending, sent, dead, suppressed")

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            query = {"channel": channel, "state": state, "run_id": run_id, "limit": limit}
            return (await repo.list_deliveries(conn, **query))[0]

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for d in rows:
        line = f"{d['delivery_id']}  {_at(d['created_at'])}  {d['channel']}  {d['event']}  {d['severity']}"
        line += f"  {d['state']}" + (f" ({d['why']})" if d["why"] else "") + f"  {d['attempts']} attempt(s)"
        line += f"  audit row {d['seq']}" if d["seq"] else ""
        line += f"  run {d['run_id']}" if d["run_id"] else ""
        typer.echo(line + (f"  {_last(d)}" if _last(d) else ""))


@notify_app.command("test")
def notify_test(channel: Annotated[str, _a("A declared channel.")], config: ConfigOpt = None) -> None:
    """Send a test notification to the channel now, outside the queue, and record it as sent or dead; exit
    2 when it is not delivered."""
    who = _local(config)

    async def work(ctx: Context) -> tuple[dict[str, Any], str]:
        done = await notifications.test(ctx, channel, who)
        return done, ctx.settings.notify.channels[channel].kind  # type: ignore[union-attr]

    done, kind = _in_context(config, work)
    protocol = "SMTP" if kind == "email" else "HTTP"
    status = f" ({protocol} {done['status']})" if done["status"] else ""
    if done["state"] != "sent":
        _fail(f"{channel}: {done['error']}{status} ({done['delivery_id']})")
    refused = f", {done['error']}" if done["error"] else ""
    took = f"in {done['latency_ms']} ms ({done['delivery_id']})"
    typer.echo(f"sent to {channel}: {protocol} {done['status']}{refused} {took}")


@notify_app.command("retry")
def notify_retry(
    channel: Annotated[str, _a("A declared channel.")],
    since: Annotated[
        str | None, _o("Only those that died from this ISO-8601 time with a zone (default: 72 hours ago).")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Send the channel's dead deliveries again, tests aside: each is pending, due now, with no attempt."""
    who = _local(config)
    count, at = _in_context(config, lambda ctx: notifications.retry(ctx, channel, since, who))
    typer.echo(f"{count} dead deliveries of {channel} since {_at(at)} are pending again")


@notify_app.command("mute")
def notify_mute(
    hours: Annotated[int, _o("For 1 to 168 hours.")],
    reason: Annotated[str, _o(MUTE_REASON)],
    channel: Annotated[str | None, _o("Mute this declared channel.")] = None,
    run_id: Annotated[str | None, _o("Mute this run's notifications.", "--run")] = None,
    config: ConfigOpt = None,
) -> None:
    """Hold back a channel's notifications, or a run's, for some hours: they are stored as suppressed, never
    sent. Every screen says so until it lapses or is ended."""
    if (channel is None) == (run_id is None):
        _fail("give --channel or --run")
    who = _local(config)
    scope, target = ("channel", channel) if channel is not None else ("run", run_id)
    row = _in_context(config, lambda ctx: notifications.mute(ctx, scope, target, hours, reason, who))  # type: ignore[arg-type]
    what = target if scope == "channel" else f"run {target}"
    typer.echo(f"muted {what} until {notifications.utc(row['until'])} ({row['mute_id']})")


@notify_app.command("unmute")
def notify_unmute(mute_id: Annotated[str, _a("The mute id (M-…).")], config: ConfigOpt = None) -> None:
    """End a mute now."""
    who = _local(config)
    _in_context(config, lambda ctx: notifications.unmute(ctx, mute_id, who))
    typer.echo(f"ended mute {mute_id}")


# -- cut-over readiness (docs/mvp/READINESS.md §10, §12) ------------------------------------------------


def _version(item: dict[str, Any]) -> str:
    """A pair's current version as readiness names one; nothing without evidence."""
    return f" {item['version']} (spec {item['spec_id']})" if item.get("spec_id") is not None else ""


def _pair_line(item: dict[str, Any]) -> str:
    """A pair in one line: its version, entity type, state, gates and evidence cycle."""
    score, at = item["score"], item["evidence"]
    line = f"{item['pair']}{_version(item)}  {item['entity'] or '-'}  {worded(item['state'])}"
    line += f"  {score['passed']} of {score['required']} gates"
    if at is not None:
        ended = f" ({at['ended_at']:%Y-%m-%d})" if "ended_at" in at else ""
        line += f"  cycle {at['cycle']} of {at['run_id']}{ended}"
    return line + ("  regressed" if item["regressed"] else "")


@readiness_app.callback()
def readiness_list(
    command: typer.Context,
    programme: Annotated[str | None, _o("Only this programme's pairs.")] = None,
    entity: Annotated[str | None, _o("Only this entity type's pairs.")] = None,
    state: Annotated[str | None, _o("Only the pairs in this state.")] = None,
    as_of: Annotated[
        str | None, _o("What was recorded by this ISO-8601 time with a UTC offset.", "--as-of")
    ] = None,
    as_json: Annotated[bool, _o("Print GET /v1/readiness's body, every pair in it.", "--json")] = False,
    fail_unless: Annotated[
        str | None, _o("Exit 1 when a listed pair is below this state.", "--fail-unless")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Show cut-over readiness: each programme with how many of its open pairs are parity-ready or better,
    then each pair, worst first, with the gates it does not meet; or show one pair, write the reports, check
    the settings or verify the stored measures. Readiness is named gates, each met or not on observed
    evidence: never a probability, a forecast or sign-off evidence."""
    if command.invoked_subcommand is not None:
        options = (programme, entity, state, as_of, fail_unless, config)
        if as_json or any(option is not None for option in options):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    if fail_unless is not None and fail_unless not in STATES:
        _fail(f"state must be one of {', '.join(STATES)}")

    async def work(ctx: Context) -> dict[str, Any]:
        asked = {"programme": programme, "entity": entity, "state": state, "as_of": as_of, "limit": 500}
        found = page = await readiness.overview(ctx, **asked)
        while page["next_cursor"] is not None:
            page = await readiness.overview(ctx, **asked, cursor=page["next_cursor"])
            found["items"] += page["items"]
        return found | {"next_cursor": None}

    found = _in_context(config, work)
    below = [
        item["pair"]
        for item in found["items"]
        if fail_unless is not None and STATES.index(item["state"]) < STATES.index(fail_unless)
    ]
    if as_json:
        typer.echo(_json(found))
    else:
        typer.echo(found["definition"])
        typer.echo(f"readiness settings {found['config_sha256']}")
        for note in found["notes"]:
            typer.echo(note)
        if as_of is not None:
            typer.echo(f"as recorded by {_at(found['as_of'])}")
        for rolled in found["programmes"]:
            mine = [item for item in found["items"] if item["programme"] == rolled["programme"]]
            worst = worded(rolled["worst"] or "-")
            typer.echo(
                f"{rolled['programme'] or '(no programme)'}: {rolled['ready']} of {rolled['pairs']} pairs"
                f" parity-ready or better (worst: {worst})"
            )
            for item in mine:
                typer.echo(_pair_line(item))
                for gate in item["gates"] if as_of is None else ():
                    if gate["gate"] in item["blocking"]:
                        typer.echo(f"  {gate['gate']} {worded(gate['status'])}: {gate['text']}")
                for deviation in item.get("deviations", ()):
                    typer.echo(f"  deviation: {deviation}")
    if below:
        raise typer.Exit(EXIT_DIFF)


@readiness_app.command("pair")
def readiness_pair(
    name: Annotated[str, _a("The pair: a spec name.")],
    bucket: Annotated[str | None, _o("Its trend by cycle (the default), day or week.")] = None,
    precheck: Annotated[
        bool, _o("Also compute what a sign-off of its candidate would be refused for.", "--precheck")
    ] = False,
    as_json: Annotated[
        bool, _o("Print GET /v1/readiness/pairs/NAME's body, with its trend.", "--json")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Show one pair: every gate by group with what was observed, its convergence, its newest points and,
    with --precheck, what a sign-off of its candidate cycle would be refused for now."""

    async def work(ctx: Context) -> dict[str, Any]:
        found = await readiness.pair(ctx, name, precheck=precheck)
        return found | {"cycles": (await readiness.cycles(ctx, name, bucket=bucket))["items"]}

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    typer.echo(found["definition"])
    typer.echo(_pair_line(found))
    for note in (*found["notes"], *(f"deviation: {text}" for text in found["deviations"])):
        typer.echo(note)
    for gate in found["gates"]:
        required = "" if gate["required"] else " (not required)"
        typer.echo(f"{gate['group']}  {gate['gate']}{required}  {worded(gate['status'])}: {gate['text']}")
    held = found["convergence"]
    typer.echo(f"convergence: {held['streak']} of {held['needed']} evidence cycles in a row")
    for point in found["cycles"]:
        counts = "not measured" if point["unresolved"] is None else (  # None when any count is
            f"{point['different']:,} different, {point['unresolved']:,} unresolved,"
            f" {point['suppressed']:,} suppressed of {point['total']:,}"
            + (" in its sample" if point["sample_status"] else "")
        )  # fmt: skip
        fails = ", ".join(point["fails"]) or "-"
        typer.echo(
            f"{_at(point['ended_at'])}  cycle {point['cycle']} of {point['run_id']}  spec {point['spec_id']}"
            f"  {point['basis'] or '-'}  {counts}  fails: {fails}"
            + ("  (not evidence)" if point["skipped"] else "")
        )
    if (checked := found.get("precheck")) is not None:
        at = f"cycle {checked['cycle']} of run {checked['run_id']}"
        typer.echo(f"precheck of {at} ({checked['label']}):")
        if checked["purged"]:
            typer.echo("  its evidence is purged: a sign-off is no longer possible")
        for problem in checked["problems"]:
            typer.echo(f"  {problem}")
        if not (checked["purged"] or checked["problems"]):
            typer.echo("  nothing refuses its sign-off")


@readiness_app.command("report")
def readiness_report(
    out: OutOpt,
    programme: Annotated[str | None, _o("Only this programme's pairs.")] = None,
    pair: Annotated[str | None, _o("This pair's brief instead: a spec name.")] = None,
    config: ConfigOpt = None,
) -> None:
    """Write the readiness reports: every dashboard-<n>.pdf volume (100 pairs each, worst first),
    dashboard.csv and trends.csv, or a trends-<pair>.csv per pair when they hold over 100,000 rows; or,
    with --pair, that pair's brief on one page (brief-<pair>.pdf), the brief's points (brief-<pair>.csv)
    and every root-cause group of it (causes-<pair>.csv). Each file is audited as a download."""
    from deltasleuth.reports import readiness as reports
    from deltasleuth.reports.render import render_pdf

    if pair is not None and programme is not None:
        _fail("--pair and --programme cannot be given together")
    who = _local(config)

    async def briefed(ctx: Context, pair: str) -> dict[str, bytes]:
        found = await readiness.brief(ctx, pair)
        return {
            f"brief-{pair}.pdf": await reports.brief_pdf(ctx, found),
            f"brief-{pair}.csv": reports.brief_csv(found).encode(),
            f"causes-{pair}.csv": (await reports.causes_csv(ctx, pair)).encode(),
        }

    async def dashboards(ctx: Context) -> dict[str, bytes]:
        chosen = await reports.selection(ctx, programme=programme)
        files = {
            f"dashboard-{n}.pdf": render_pdf(reports.document(chosen, n))
            for n in range(1, reports.volumes(len(chosen.items)) + 1)
        }
        files["dashboard.csv"] = reports.dashboard_csv(chosen).encode()
        try:
            whole = await reports.trends_csv(ctx, programme=programme, most=reports.TRENDS_ROWS)
            files["trends.csv"] = whole.encode()
        except readiness.Refused:  # over its rows: a file a pair, each whole
            for item in chosen.items:
                files[f"trends-{item['pair']}.csv"] = (
                    await reports.trends_csv(ctx, pair=item["pair"])
                ).encode()
        return files

    async def work(ctx: Context) -> list[Path]:
        files = await (dashboards(ctx) if pair is None else briefed(ctx, pair))
        written = _write(out, files)
        detail = {"programme": programme, "config_sha256": config_sha256(ctx.settings.readiness)}
        detail |= {"pair": pair} if pair is not None else {}
        async with repo.audited(ctx.pool) as (_, audit):
            for name in files:
                await audit(who, "report.download", "readiness", {"name": name} | detail)
        return written

    for path in _in_context(config, work):
        typer.echo(str(path))


@readiness_app.command("check")
def readiness_check(config: ConfigOpt = None) -> None:
    """Print each pair's effective gates, each tagged with where it comes from (default, installation,
    programme or pair), what deviates from the contract's defaults and the settings hash; warn of a listed
    spec that is not stored. The settings apply at the next restart of the server and the worker."""
    lines, warnings = _in_context(config, readiness.check)
    for line in lines:
        typer.echo(line)
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)


@readiness_app.command("verify")
def readiness_verify(config: ConfigOpt = None) -> None:
    """Check each cycle's stored measures against the readiness.measured audit row naming it, each such
    row against the cycle it names, and each excluded marker against its run; exit 2 names the first that
    does not agree. The database owner can edit, remove or replace audited measures: this detects it, with
    the audit chain (ds audit verify); it does not see what was changed before a cycle was measured."""
    found = _in_context(config, readiness.verify)
    typer.echo(
        f"readiness: {found['measured']:,} measured cycle(s) match their readiness.measured rows;"
        f" {found['excluded']:,} excluded, {found['errors']:,} not measured"
    )


# -- schedules (docs/mvp/SCHEDULES.md §10) --------------------------------------------------------------

ScheduleArg = Annotated[str, _a("The schedule: its name, or its id (SC-…).")]
CRON = "When its runs start: minute hour day-of-month month day-of-week."
ZONE = "The zone of those times: UTC, or an IANA region name such as Europe/London."
PIN = "Run this stored spec version."
FOLLOW = "Run this spec name's newest stored version at each firing."
WITHIN = "Minutes after its time a run may still start: 15 to 1440."
ENDS = (
    "No time is due after this ISO-8601 time with a UTC offset; a run for the last time before it may still"
    " start within its window."
)


def _next_due(found: dict[str, Any]) -> str:
    """A schedule's next due time on its zone's clock and in UTC; its state when none is due."""
    due = found["next_due"]
    if due is None:
        return found["state"]
    return (
        f"next {due['local']} {found['time_zone']} ({due['utc']})" if due["local"] else f"next {due['utc']}"
    )


def _schedule_line(found: dict[str, Any]) -> str:
    """A schedule in one line: what it runs, when, its state or next due time, and its newest firing with
    its run's status."""
    version = f"{found['runs']['version']} (spec {found['runs']['spec_id']})"
    pinned = found["spec_id"] is not None
    spec = f"pinned {version}" if pinned else f"follows {found['spec_name']}: {version} now"
    line = f"{found['schedule_id']}  {found['name']}  {spec}  {found['run']['mode']}"
    line += f"  {found['cron']} {found['time_zone']}  {_next_due(found)}"
    if (last := found["last"]) is not None:
        line += f"  last: {last['text']}" + (f" ({last['run_status']})" if last["run_status"] else "")
    return line


def _run_options(soak: bool, rolling: bool) -> dict[str, Any]:
    return {"mode": "soak" if soak else "batch"} | ({"soak": {"rolling": True}} if rolling else {})


def _saved(saved: tuple[repo.Row, list[str]], said: str) -> None:
    """Print a write's warnings on stderr, then what it did and the time due next."""
    row, warnings = saved
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    typer.echo(f"{said}: {_next_due(schedules.item(row))}")


@schedules_app.callback()
def schedules_list(
    command: typer.Context,
    spec_name: Annotated[str | None, _o("Only this spec name's schedules.", "--spec")] = None,
    state: Annotated[str | None, _o("Only those active, paused, ended or deleted.")] = None,
    set_by: Annotated[str | None, _o("Only those this actor saved last (user:U-…).", "--set-by")] = None,
    as_json: Annotated[bool, _o("Print GET /v1/schedules's body, every schedule in it.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the schedules, each in one line with its next due time and its newest firing, under what a
    schedule never tells and the banners; or show one, preview a cron, add, set, pause, resume or delete
    one. A schedule starts runs of a stored spec at cron times in a time zone; only a worker fires (ds
    worker, or ds serve)."""
    if command.invoked_subcommand is not None:
        if as_json or any(option is not None for option in (spec_name, state, set_by, config)):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    if state is not None and state not in ("active", "paused", "ended", "deleted"):
        _fail("--state is one of active, paused, ended, deleted")

    async def work(ctx: Context) -> dict[str, Any]:
        asked = {"spec_name": spec_name, "state": state, "set_by": set_by, "limit": 500}
        found = page = await schedules.overview(ctx, **asked)
        while page["next_cursor"] is not None:
            page = await schedules.overview(ctx, **asked, cursor=page["next_cursor"])
            found["items"] += page["items"]
            found["banners"] += page["banners"]
        return found | {"next_cursor": None}

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    for line in (*found["not"], *found["banners"]):
        typer.echo(line)
    if not found["items"]:
        typer.echo("no schedules")
    for item in found["items"]:
        typer.echo(_schedule_line(item))


@schedules_app.command("show")
def schedules_show(
    schedule: ScheduleArg,
    as_json: Annotated[bool, _o("Print GET /v1/schedules/SC-…'s body.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show one schedule: its definition, who saved it, what the four-eyes gate says of the version it would
    run, its next five times and its newest 20 firings."""
    found = _in_context(config, lambda ctx: schedules.described(ctx, schedule))
    if as_json:
        typer.echo(_json(found))
        return
    typer.echo(_schedule_line(found))
    by = _named(found["set_by"], found["set_by_name"])
    ends = f"; ends {_at(found['ends_at'])}" if found["ends_at"] else ""
    typer.echo(
        f"revision {found['revision']}, saved by {by} {_at(found['set_at'])} ({found['auth_mode']}); starts"
        f" within {found['start_within']} min{ends}"
    )
    if found["paused_at"] is not None:
        typer.echo(
            f"paused by {_named(found['paused_by'], found['paused_by_name'])} {_at(found['paused_at'])}"
        )
    if (gate := found["runs"]["approval"]) is not None:
        typer.echo(f"approval: {gate['state']}" + (f": {gate['refusal']}" if gate["refusal"] else ""))
    for due in found["upcoming"]:
        typer.echo(f"due {due['local']} {found['time_zone']} ({due['utc']})")
    for fired in found["firings"]:
        typer.echo(f"{_at(fired['at'])}  {fired['text']}")


@schedules_app.command("next")
def schedules_next(
    cron: Annotated[str, _a("A cron expression: minute hour day-of-month month day-of-week.")],
    time_zone: Annotated[str, _o(ZONE, "--tz")],
    count: Annotated[int, typer.Option(min=1, max=100, help="How many times.")] = schedules.PREVIEW,
) -> None:
    """Print a cron's next times in a zone, as this build's tz data gives them, and warn of each time a
    clock change moves and of a zone whose name holds a number; without settings or a database."""
    with _errors():
        found = schedules.preview(cron, time_zone, datetime.now(UTC), count)
    for due in found["upcoming"]:
        typer.echo(f"{due['local']} {time_zone} ({due['utc']})")
    for note in found["notes"]:
        typer.echo(f"warning: {note}", err=True)


@schedules_app.command("add")
def schedules_add(
    name: Annotated[str, _a("Its name: a slug, unique among the schedules.")],
    cron: Annotated[str, _o(CRON)],
    time_zone: Annotated[str, _o(ZONE, "--tz")],
    spec_id: Annotated[int | None, _o(PIN, "--spec-id")] = None,
    spec_name: Annotated[str | None, _o(FOLLOW, "--spec")] = None,
    soak: Annotated[bool, _o("Start soak runs, not batch runs.", "--soak")] = False,
    rolling: Annotated[bool, _o("A soak resolving its keys again each cycle.", "--rolling")] = False,
    start_within: Annotated[int, _o(WITHIN, "--start-within")] = schedules.START_WITHIN,
    ends: Annotated[str | None, _o(ENDS)] = None,
    config: ConfigOpt = None,
) -> None:
    """Declare a schedule of a stored spec, pinned to a version or following its name; its first run starts
    at its next time."""
    who = _local(config)
    body = {"name": name, "spec_id": spec_id, "spec_name": spec_name, "run": _run_options(soak, rolling)}
    body |= {"cron": cron, "time_zone": time_zone, "start_within": start_within, "ends_at": ends}
    saved = _in_context(config, lambda ctx: schedules.create(ctx, body, who))
    _saved(saved, f"added {saved[0]['schedule_id']} ({saved[0]['name']})")


@schedules_app.command("set")
def schedules_set(
    schedule: ScheduleArg,
    cron: Annotated[str | None, _o(CRON)] = None,
    time_zone: Annotated[str | None, _o(ZONE, "--tz")] = None,
    spec_id: Annotated[int | None, _o(PIN, "--spec-id")] = None,
    spec_name: Annotated[str | None, _o(FOLLOW, "--spec")] = None,
    soak: Annotated[bool | None, _o("Start soak runs, or batch runs.", "--soak/--batch")] = None,
    rolling: Annotated[bool, _o("A soak resolving its keys again each cycle.", "--rolling")] = False,
    start_within: Annotated[int | None, _o(WITHIN, "--start-within")] = None,
    ends: Annotated[str | None, _o(ENDS)] = None,
    config: ConfigOpt = None,
) -> None:
    """Save a schedule again with the options given, the rest as stored: a time due and not started is
    recorded as missed first, and a paused schedule stays paused."""
    who = _local(config)

    async def work(ctx: Context) -> tuple[repo.Row, list[str]]:
        async with ctx.pool.connection() as conn:
            row = await repo.get_schedule(conn, schedule)
        if row is None:
            raise schedules.Refused(404, f"schedule {schedule} not found")
        kept = ("name", "run", "cron", "time_zone", "start_within", "ends_at", "revision")
        body = {name: row[name] for name in kept}
        body |= {"spec_name": row["spec_name"]} if row["spec_id"] is None else {"spec_id": row["spec_id"]}
        if spec_id is not None or spec_name is not None:
            body |= {"spec_id": spec_id, "spec_name": spec_name}
        if soak is not None or rolling:
            body["run"] = _run_options(row["run"]["mode"] == "soak" if soak is None else soak, rolling)
        given = {"cron": cron, "time_zone": time_zone, "start_within": start_within, "ends_at": ends}
        body |= {name: value for name, value in given.items() if value is not None}
        return await schedules.replace(ctx, row["schedule_id"], body, who)

    saved = _in_context(config, work)
    _saved(saved, f"saved {saved[0]['schedule_id']}")


@schedules_app.command("pause")
def schedules_pause(
    schedule: Annotated[str | None, _a("The schedule: its name, or its id (SC-…).")] = None,
    every: Annotated[bool, _o("Pause every active schedule.", "--all")] = False,
    config: ConfigOpt = None,
) -> None:
    """Pause a schedule, or every active one (before a worker starts on a restored or copied database):
    nothing is due until it is resumed, and a time due now is recorded as missed."""
    if (schedule is None) != every:
        _fail("name a schedule, or pass --all")
    who = _local(config)
    if schedule is None:
        paused = _in_context(config, lambda ctx: schedules.pause_all(ctx, who))
        typer.echo(
            f"paused {paused} schedule{'' if paused == 1 else 's'}" if paused else "no active schedules"
        )
        return
    row = _in_context(config, lambda ctx: schedules.pause(ctx, schedule, who))
    typer.echo(f"paused {row['schedule_id']}")


@schedules_app.command("resume")
def schedules_resume(schedule: ScheduleArg, config: ConfigOpt = None) -> None:
    """Resume a paused schedule from now: the times of its pause are neither due nor missed."""
    who = _local(config)
    saved = _in_context(config, lambda ctx: schedules.resume(ctx, schedule, who))
    _saved(saved, f"resumed {saved[0]['schedule_id']}")


@schedules_app.command("delete")
def schedules_delete(schedule: ScheduleArg, config: ConfigOpt = None) -> None:
    """Delete a schedule: its name is free again, and its runs and firings still name it."""
    who = _local(config)
    row = _in_context(config, lambda ctx: schedules.delete(ctx, schedule, who))
    typer.echo(f"deleted {row['schedule_id']}")


# -- table pre-checks (docs/mvp/PRECHECKS.md §11) -------------------------------------------------------

PrecheckArg = Annotated[str, _a("The table pre-check's id (PC-…).")]
_FAIL_ON = {"refused": 2, "differs": 7, "incomplete": 8}  # the rank from which, towards the worst, each fails


def _precheck_line(item: dict[str, Any]) -> str:
    """A pre-check in one line: its level and source, who made it and when, and whether it has ended."""
    ended = "cancelled" if item["cancelled_at"] else "finished" if item["finished_at"] else "not finished"
    made = f"by {_named(item['created_by'], item['created_by_name'])} {_at(item['created_at'])}"
    line = f"{item['precheck_id']}  {item['source']}  level {item['level']}  {made}  {ended}"
    return line + (f"  {prechecking.EVALUATION}" if item["evaluation"] else "")


def _precheck_shown(found: dict[str, Any]) -> None:
    """Print a pre-check: what it is not, the label of an unlicensed evaluation, its line, its banner, its
    totals, then each pair's line worst first (a pair not read yet says so by its status)."""
    typer.echo(found["not"])
    if found["evaluation"]:
        typer.echo(prechecking.EVALUATION)
    for line in (_precheck_line(found | {"evaluation": False}), *found["banners"], found["totals"]):
        typer.echo(line)
    for pair in found["items"]:
        typer.echo(f"{pair['n']}  {pair['label']}  {pair['text'] or pair['status']}")


@precheck_app.callback()
def precheck_list(
    command: typer.Context,
    as_json: Annotated[bool, _o("Print GET /v1/prechecks's body, every pre-check in it.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the table pre-checks, newest first, each with its pairs counted by verdict, worst first; or
    show one, run one, cancel one or write its reports. A table pre-check counts and adds up the two tables
    of each pair; it does not compare rows, and it is observed, never evidence or readiness. Only a worker
    reads the targets (ds worker, ds serve, or ds precheck run)."""
    if command.invoked_subcommand is not None:
        if as_json or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return

    async def work(ctx: Context) -> dict[str, Any]:
        found = page = await prechecks.overview(ctx, limit=500)
        while page["next_cursor"] is not None:
            page = await prechecks.overview(ctx, limit=500, cursor=page["next_cursor"])
            found["items"] += page["items"]
        return found | {"next_cursor": None}

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
        return
    for line in (found["not"], *found["banners"]):
        typer.echo(line)
    if not found["items"]:
        typer.echo("no table pre-checks")
    for item in found["items"]:
        typer.echo(_precheck_line(item))
        typer.echo(f"  {item['totals']}")


@precheck_app.command("show")
def precheck_show(
    precheck_id: PrecheckArg,
    pair: Annotated[int | None, _o("Show this pair: its lines, then each column's figures.")] = None,
    verdict: Annotated[str | None, _o("Only the pairs of this verdict.")] = None,
    as_json: Annotated[bool, _o("Print the API's body.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Show one table pre-check: what it is not, its totals and each pair's line, worst first; or, with
    --pair, everything read of one pair: its lines, then each compared column's figures."""
    if verdict is not None and verdict not in prechecking.VERDICTS:
        _fail(f"--verdict is one of {', '.join(prechecking.VERDICTS)}")
    if pair is None:
        found = _in_context(
            config, lambda ctx: prechecks.described(ctx, precheck_id, verdict=verdict, limit=prechecks.PAIRS)
        )
        if as_json:
            typer.echo(_json(found))
        else:
            _precheck_shown(found)
        return
    found = _in_context(config, lambda ctx: prechecks.pair(ctx, precheck_id, pair))
    if as_json:
        typer.echo(_json(found))
        return
    typer.echo(found["not"])
    if found["evaluation"]:
        typer.echo(prechecking.EVALUATION)
    sides = ", ".join(f"{t} {r or '-'}" for t, r in zip(found["targets"], found["relations"], strict=True))
    said = found["said"] or found["status"]
    typer.echo(f"pair {found['n']} of {precheck_id}: {found['label']} ({sides}): {said}")
    for line in (found["result"] or {}).get("lines", [found["text"]] if found["text"] else []):
        typer.echo(line)
    for column in found["columns"]:
        for line in column["lines"]:
            typer.echo(f"  {column['column']}: {line}")


@precheck_app.command("run")
def precheck_run(
    specs: Annotated[list[Path] | None, _a("Specs of two tables (YAML or JSON), stored first.")] = None,
    spec_id: Annotated[list[int] | None, _o("A stored spec version (repeatable).", "--spec-id")] = None,
    spec_name: Annotated[
        list[str] | None, _o("A spec name's newest stored version (repeatable).", "--spec")
    ] = None,
    all_specs: Annotated[
        bool, _o("The newest version of every stored spec whose sides are both tables.", "--all-specs")
    ] = False,
    side_a: Annotated[
        str | None, _o("Side a of two schemas paired by name: a database target, then :schema.", "--a")
    ] = None,
    side_b: Annotated[str | None, _o("Side b of two schemas paired by name, as --a.", "--b")] = None,
    include: Annotated[
        list[str] | None, _o("Only the tables whose lower-cased name this glob matches (repeatable).")
    ] = None,
    exclude: Annotated[list[str] | None, _o("Not the tables this glob matches (repeatable).")] = None,
    level: Annotated[
        str | None, _o("What it reads: schema, counts, columns or distinct (columns; counts for --a, --b).")
    ] = None,
    wait: Annotated[bool, _o("Read it here until it ends.")] = True,
    fail_on: Annotated[
        str | None,
        _o(
            "Exit 1 when a pair is this bad or worse (refused, differs: a pair not read too, or incomplete),"
            " or when the pre-check was cancelled.",
            "--fail-on",
        ),
    ] = None,
    as_json: Annotated[bool, _o("Print the pre-check as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Make a table pre-check of stored specs, or of two schemas' tables paired by name (--a, --b), and,
    without --no-wait, read it here with an in-process worker: each pair's two tables are counted and added
    up inside one snapshot a side, and no row is read. It says where to look first; it does not compare
    rows."""
    from deltasleuth.runner import run_worker

    if fail_on is not None and fail_on not in _FAIL_ON:
        _fail(f"--fail-on is one of {', '.join(_FAIL_ON)}")
    if fail_on is not None and not wait:
        _fail("--fail-on waits for the pre-check's end: leave out --no-wait")
    named = side_a is not None or side_b is not None
    if named and (side_a is None or side_b is None or specs or spec_id or spec_name or all_specs):
        _fail("--a and --b come together, and without a spec")
    if (include or exclude) and not named:
        _fail("--include and --exclude take --a and --b")
    ids = [0] * len(specs or ()) + list(spec_id or ())  # a file's id is its stored version's
    body: dict[str, Any] = {"spec_ids": ids or None, "spec_names": spec_name, "all_specs": all_specs}
    if named:
        for side, given in (("a", side_a), ("b", side_b)):
            target, _, schema = given.partition(":")  # type: ignore[union-attr]
            body[side] = {"target": target, "schema": schema or None}
        body = {side: body[side] for side in "ab"} | {"include": include, "exclude": exclude}
    body["level"] = level or ("counts" if named else "columns")
    with _errors():
        (prechecks.refuse_catalogue if named else prechecks.refuse)(body)  # before a file is stored
    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        if named:
            made = await prechecks.catalogue(ctx, body, who)
        else:
            stored = [(await _save_spec(ctx, path, who, gated=False))[0] for path in specs or ()]
            ids_ = (stored + ids[len(stored) :]) or None
            made = await prechecks.create(ctx, body | {"spec_ids": ids_}, who)
        precheck_id = made["precheck_id"]
        with _stopping() as stop:
            running = asyncio.create_task(run_worker(ctx, stop=stop, schedules=False)) if wait else None
            try:
                while True:
                    found = await prechecks.described(ctx, precheck_id, limit=prechecks.PAIRS)
                    if not wait or found["finished_at"] is not None:
                        return found
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), ctx.settings.worker.poll_seconds)
                    if stop.is_set():  # a SIGTERM: as Ctrl-C, once the worker below has stopped
                        raise KeyboardInterrupt
            finally:
                stop.set()
                if running:
                    await running

    found = _in_context(config, work)
    if as_json:
        typer.echo(_json(found))
    else:
        _precheck_shown(found)
    if not wait:
        typer.echo("read by 'ds worker' or 'ds serve'", err=as_json)
    worst = _FAIL_ON.get(fail_on or "", 0)
    bad = [p for p in found["items"] if p["verdict"] and prechecking.rank(p["verdict"]) <= worst]
    if fail_on is not None and (bad or found["cancelled_at"] is not None):
        raise typer.Exit(EXIT_DIFF)


@precheck_app.command("cancel")
def precheck_cancel(precheck_id: PrecheckArg, config: ConfigOpt = None) -> None:
    """Cancel an unfinished table pre-check: its pending and running pairs are cancelled, and what was read
    stays."""
    who = _local(config)
    _in_context(config, lambda ctx: prechecks.cancel(ctx, precheck_id, who))
    typer.echo(f"cancelled {precheck_id}")


@precheck_app.command("draft")
def precheck_draft(
    precheck_id: PrecheckArg,
    out: OutOpt,
    pair: Annotated[int | None, _o("Draft this pair.")] = None,
    every: Annotated[bool, _o("Draft every pair of two tables that was read.", "--all")] = False,
    config: ConfigOpt = None,
) -> None:
    """Write a starter spec of a pair of two schemas' tables paired by name, as PC-…-pair-N.draft.yaml: both
    tables, their common primary key and the columns both hold of one family, each column left out as a
    comment; its validate_spec issues head it. Never stored: when it is ready, upload it on the Specs screen
    or with POST /v1/specs, or, outside SSO, give the file to ds run, which stores the spec it runs."""
    if (pair is None) == (not every):
        _fail("give --pair N or --all, and only one of them")

    async def work(ctx: Context) -> list[tuple[int, str, list[SpecIssue]]]:
        if pair is not None:
            return [(pair, *await prechecks.draft(ctx, precheck_id, pair))]
        found = await prechecks.described(ctx, precheck_id, limit=prechecks.PAIRS)
        drafted = []
        for item in sorted(found["items"], key=lambda item: item["n"]):
            with suppress(prechecks.Refused):  # a stored spec, an unpaired name, a pair not read
                drafted.append((item["n"], *await prechecks.draft(ctx, precheck_id, item["n"])))
        return drafted

    drafted = _in_context(config, work)
    if not drafted:
        _fail(f"no pair of {precheck_id} can be drafted: none is two tables paired by name and read")
    for n, text, issues in drafted:
        with _errors():
            path = _write(out, {f"{precheck_id}-pair-{n}.draft.yaml": text.encode()})[0]
        typer.echo(str(path))
        _issues(issues)


@precheck_app.command("report")
def precheck_report(precheck_id: PrecheckArg, out: OutOpt, config: ConfigOpt = None) -> None:
    """Write a table pre-check's reports as PC-…-<name> (PC-…-unlicensed-evaluation-<name> for one made
    in an unlicensed evaluation): precheck.csv, precheck-columns.csv and precheck.pdf; each file is
    audited as a download."""
    from deltasleuth.reports import precheck as reports

    who = _local(config)

    async def work(ctx: Context) -> list[Path]:
        row, pairs = await reports.loaded(ctx, precheck_id)
        rendered = {name: reports.render(name, row, pairs)[1] for name in reports.NAMES}
        written = _write(out, {prechecks.file_name(row, name): data for name, data in rendered.items()})
        async with repo.audited(ctx.pool) as (_, audit):
            for name in reports.NAMES:
                await audit(who, "report.download", precheck_id, {"name": name})
        return written

    for path in _in_context(config, work):
        typer.echo(str(path))


# -- your own agent: the MCP server (docs/mvp/MCP.md §12) -------------------------------------------------

MCP_OFF = "MCP is off: the settings have no mcp block (docs/mvp/MCP.md §4)"
MCP_HOST_CHANNEL = (
    "ds mcp under auth is the host-administrator channel: use /v1/mcp with a named token, or pass"
    " --host-channel (docs/mvp/MCP.md §5.2)"
)
MCP_ASSERTED = (
    "warning: ds mcp is the host-administrator channel: its caller is asserted (DS_OPERATOR), not signed in,"
    " and an agent that runs it acts with this host account, not with a role; agents belong on /v1/mcp with"
    " a named token"
)
MCP_CLEAR = (
    "warning: mcp: values a spec declares clear and business-code keys leave the installation in tool results"
)
MCP_MASKED = "runs whose spec declares clear values or a business-code key are not served"


class _Bare(logging.Formatter):
    """A record's logger and level alone: neither its message nor its traceback, which may hold data."""

    def format(self, record: logging.LogRecord) -> str:
        return f"{record.name} {record.levelname}"


def _mcp_logs(*, bare: bool) -> None:
    """Put the MCP server's two loggers on stderr (docs/mvp/MCP.md §9), neither left to Python's last-resort
    handler: ``deltasleuth.mcp``, one line a call, and the MCP library's ``mcp`` from WARNING up. Under
    ``ds mcp`` (``bare``) stderr is the launching agent's, so a library line is its name and level alone,
    and so is every other logger's, through the root logger: the last-resort handler would print such a
    record whole, psycopg's with the store's address and any with its traceback."""
    library = _Bare() if bare else logging.Formatter("%(name)s %(levelname)s %(message)s")
    for name, level, form in (
        ("deltasleuth.mcp", logging.INFO, logging.Formatter("%(message)s")),
        ("mcp", logging.WARNING, library),
        *([(None, logging.WARNING, library)] if bare else []),  # the root logger
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(h, _Stream) for h in logger.handlers):
            handler = _Stream("stderr")
            handler.setFormatter(form)
            logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False


def _mcp(config: Path | None) -> tuple[Settings, McpSettings]:
    """The settings and their ``mcp`` block, without which MCP is off."""
    settings = load_settings(config)
    if settings.mcp is None:
        raise SettingsError(MCP_OFF)
    return settings, settings.mcp


def _mcp_serves(mcp: McpSettings) -> str:
    return f"data classes {', '.join(mcp.data_classes)}; work tools {', '.join(mcp.work) or 'none'}"


async def _mcp_stopped(ctx: Context) -> None:
    """End ``ds mcp`` whose serving a SIGTERM ended, exit 0: the store closes and the process ends as at
    its last line, but without joining the MCP library's stdin reader, a thread that returns only when the
    client writes or closes its stream."""
    await ctx.pool.close()
    atexit._run_exitfuncs()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


@mcp_app.callback()
def mcp_serve(
    command: typer.Context,
    role: Annotated[
        Literal["viewer", "operator"], _o("The role its tools are offered under; compare_key needs operator.")
    ] = "viewer",
    host_channel: Annotated[
        bool,
        _o("Serve where the settings have auth, as the host administrator (DS_OPERATOR).", "--host-channel"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Serve the MCP tools to your own agent over stdin and stdout, until it closes the stream; or check
    the mcp settings. The tools read runs by id with the chat's masking, caps and ids, as the settings' mcp
    block serves them (docs/mvp/MCP.md). This is a workstation's channel, in developer mode: the agent that
    runs it holds this account's access to the store and the keys, so --role only shapes what is offered.
    Where the settings have auth it serves only with --host-channel; an agent there calls POST /v1/mcp with
    a named API token instead."""
    if command.invoked_subcommand is not None:
        if role != "viewer" or host_channel or config is not None:
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    with _errors():
        settings, mcp = _mcp(config)
        if settings.auth is not None and not host_channel:
            raise SettingsError(MCP_HOST_CHANNEL)
        from deltasleuth.connectors.tables import code
        from deltasleuth.mcp import server, tools  # the MCP library's import, which this command alone needs

        asserted = replace(local(required=settings.auth is not None), roles=expand([role]))
        who = tools.caller(asserted, tools.STDIO)
    _mcp_logs(bare=True)

    async def work(ctx: Context) -> str | None:
        typer.echo(f"mcp: serving stdio as {who.actor}, role {role}; {_mcp_serves(mcp)}", err=True)
        if settings.auth is not None:
            typer.echo(MCP_ASSERTED, err=True)
        with _stopping(once=True) as stop:
            try:
                await server.stdio(ctx, mcp, who, stop, lambda: _mcp_stopped(ctx))
            except Exception as exc:  # the stream's own fault: stderr is the agent's, so its code alone
                return code(exc)
        return None

    # One idle connection a process: an editor starts a server per window.
    if (fault := _in_context(config, work, pool=(1, mcp.concurrency + 1))) is not None:
        typer.echo(f"internal error: the MCP stream failed ({fault})", err=True)
        raise typer.Exit(EXIT_INTERNAL)


@mcp_app.command("check")
def mcp_check(config: ConfigOpt = None) -> None:
    """Print the mcp settings as judged, without a database: what results may carry and the limits, the
    tools a viewer and an operator are offered, each tool not served with the reason, whether runs whose
    spec declares clear values or a business-code key are served, and what MCP is not."""
    from deltasleuth.mcp import tools

    with _errors():
        _, mcp = _mcp(config)
    typer.echo(
        f"mcp: {_mcp_serves(mcp)}; POST /v1/mcp {'served' if mcp.http else 'off'};"
        f" {mcp.calls_per_minute} calls a minute per caller, {mcp.concurrency} at once"
    )
    for role in ("viewer", "operator"):
        offered = tools.offered(mcp, Principal(role, role, expand([role])))
        typer.echo(f"{role}: {', '.join(spec.name for spec in offered) or 'no tool'}")
    for spec in tools.CATALOGUE.values():
        if (reason := tools.unserved(mcp, spec)) is not None:
            typer.echo(f"not served: {spec.name} ({reason})")
    clear = tools.CLEAR in mcp.data_classes
    typer.echo(MCP_CLEAR if clear else MCP_MASKED, err=clear)
    for line in tools.NOT:
        typer.echo(line)


# -- retention (docs/mvp/RETENTION.md §4, §5, §8) --------------------------------------------------


@purge_app.callback()
def purge(
    command: typer.Context,
    limit: Annotated[int | None, typer.Option(min=1, help="Purge at most this many cycles.")] = None,
    orphans: Annotated[bool, _o("Also delete evidence files nothing names.", "--orphans")] = False,
    dry_run: Annotated[bool, _o("Show what a pass would do, writing nothing.", "--dry-run")] = False,
    run_id: Annotated[str | None, _o("Erase this run now (R-…), whatever its retention.", "--run")] = None,
    cycle: Annotated[int | None, _o("With --run: erase only this ended cycle.")] = None,
    reason: Annotated[str | None, _o(REASON)] = None,
    config: ConfigOpt = None,
) -> None:
    """Delete the evidence of the ended cycles past their retention, as the worker does hourly; or erase
    a run now; or hold or release a run's evidence. Each purged cycle is audited."""
    from deltasleuth.runner import purge as purger

    options = (limit, cycle, reason, run_id, config)
    if command.invoked_subcommand is not None:
        if orphans or dry_run or any(option is not None for option in options):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    if run_id is None:
        if cycle is not None or reason is not None:
            _fail("--cycle and --reason go with --run R-…")
        if dry_run:
            _preview(_in_context(config, lambda ctx: purger.preview(ctx, limit=limit)))
            return
        who = _local(config)
        done = _in_context(config, lambda ctx: purger.purge(ctx, who, limit=limit, orphans=orphans))
    else:
        if dry_run or orphans or limit is not None:
            _fail("--run erases one run: it takes no --dry-run, --orphans or --limit")
        if reason is None:
            _fail("--run needs --reason (an erasure is audited)")
        if problem := purger.reason_error(reason):
            _fail(problem)
        who = _local(config)
        done = _in_context(config, lambda ctx: purger.erase(ctx, who, run_id, reason, cycle))
    typer.echo(
        f"purged {done.cycles:,} cycle(s) of {len(done.runs):,} run(s): {done.keys:,} key(s);"
        f" {done.blobs_queued:,} blob(s) queued; {done.blobs_deleted:,} deleted ({_size(done.bytes)}),"
        f" {done.blobs_shared:,} kept (shared)"
    )
    for count, what in ((done.abandoned, "abandoned {} run(s) past their retention"),
                        (done.held, "held: {} cycle(s) past their retention"),
                        (done.orphans, "deleted {} orphaned file(s)"),
                        (done.prechecks, "deleted {} table pre-check(s) past their retention")):  # fmt: skip
        if count:
            typer.echo(what.format(f"{count:,}"))


def _size(n: int) -> str:
    """Bytes in the nearest decimal unit, e.g. 1.9 GB."""
    value, unit = float(n), "B"
    for larger in ("kB", "MB", "GB", "TB"):
        if value < 1000:
            break
        value, unit = value / 1000, larger
    return f"{n} B" if unit == "B" else f"{value:.1f} {unit}"


def _day(value: datetime) -> str:
    return f"{value.astimezone(UTC):%Y-%m-%d}"


def _preview(plan: dict[str, list[repo.Row]]) -> None:
    """``ds purge --dry-run``'s lines."""
    lines = []
    for row in plan["due"]:
        what = "its interrupted purge resumes" if row["purging_at"] else f"expired {_day(row['expires_at'])}"
        lines.append(f"due: {_cycle(row)}: {what}")
    for row in plan["kept"]:
        expired = f"{_cycle(row)}: expired {_day(row['expires_at'])}"
        if row["hold_reason"] is not None:
            held = f"held by {row['hold_by']} since {_day(row['hold_at'])}: {row['hold_reason']!r}"
            lines.append(f"held: {expired}; {held}")
        else:
            lines.append(f"kept: {expired}; its running soak reads it until its next cycle ends")
    for row in plan["abandon"]:
        run = f"run {row['run_id']} ({row['status']} since {_day(row['updated_at'])})"
        if row["mode"] == "one_shot" and row["status"] == "queued":
            lines.append(f"abandon: {run}: failed, a one-shot run queued for 24 h")
        else:
            past = f"past its {row['days']} days (retention {row['retention']})"
            lines.append(f"abandon: {run}: cancelled, {past}; its cycle is then due")
    samples = sorted({(row["samples_dir"], row["run_id"]) for row in plan["due"] if row["samples_dir"]})
    for directory, run_id in samples:
        lines.append(f"outside retention: {directory} (run {run_id}): P0 samples, for the site's cron")
    for line in lines or ["nothing to purge"]:
        typer.echo(line)


def _cycle(row: repo.Row) -> str:
    return f"cycle {row['cycle']} of run {row['run_id']} (retention {row['retention']})"


@purge_app.command("hold")
def purge_hold(run_id: RunArg, reason: Annotated[str, _o(REASON)], config: ConfigOpt = None) -> None:
    """Keep a run's evidence past its retention, until released (a legal hold or an investigation)."""
    from deltasleuth.runner import purge as purger

    if problem := purger.reason_error(reason):
        _fail(problem)
    who = _local(config)
    _in_context(config, lambda ctx: purger.hold(ctx, who, run_id, reason))
    typer.echo(f"held run {run_id}: its evidence stays until 'ds purge release {run_id}'")


@purge_app.command("release")
def purge_release(run_id: RunArg, config: ConfigOpt = None) -> None:
    """Release a run's hold: its cycles past their retention go at the next pass."""
    from deltasleuth.runner import purge as purger

    who = _local(config)
    _in_context(config, lambda ctx: purger.hold(ctx, who, run_id, None))
    typer.echo(f"released run {run_id}: its cycles past their retention go at the next pass")


@keys_app.command("status")
def keys_status(
    verify: Annotated[bool, _o("Also read back every blob (the restore check).", "--verify")] = False,
    as_json: Annotated[bool, _o("Print the status as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """Each KEK of the keyring with the data keys it wraps, and the KEKs in use the keyring lacks. Exit 2
    while one is missing, or (--verify) while a blob is corrupted or unreadable for a missing KEK; missing
    blobs are counted, not failed."""
    from deltasleuth.security import keys

    async def work(ctx: Context) -> tuple[Any, Counter[str], dict[str, int] | None]:
        async with ctx.pool.connection() as conn:
            used = Counter(row["kek"] for row in await repo.data_keys(conn))
        return ctx.keyring, used, await keys.verify(ctx.pool, ctx.evidence) if verify else None

    keyring, used, blobs = _in_context(config, work)
    present = {kek: used[kek] for kek in keyring.ids}
    missing = {kek: n for kek, n in used.items() if kek not in keyring.ids}
    unread = blobs and blobs["corrupted"] + blobs["key_missing"]  # a missing blob is counted, not failed
    if as_json:
        status = {"current": keyring.current, "keks": present, "missing": missing}
        typer.echo(_json(status | ({} if blobs is None else {"blobs": blobs})))
    else:
        for kek, n in present.items():
            typer.echo(f"{kek}: {n:,} data key(s)" + (" (current)" if kek == keyring.current else ""))
        for kek, n in missing.items():
            typer.echo(f"missing {kek}: {n:,} data key(s); restore the line from the vault and restart")
        if blobs is not None:
            typer.echo(
                f"blobs: {blobs['checked']:,} checked: {blobs['readable']:,} readable, {blobs['missing']:,}"
                f" missing, {blobs['corrupted']:,} corrupted, {blobs['key_missing']:,} unreadable (a KEK is"
                " missing)"
            )
    if missing:
        _fail(f"the keyring lacks {len(missing)} KEK(s) in use")
    if unread:
        _fail(f"{unread:,} blob(s) do not decrypt")


@keys_app.command("fingerprint")
def keys_fingerprint(path: Annotated[Path, _a("A keyring file, such as a vault copy.")]) -> None:
    """Print the ids of a keyring file's KEKs, oldest first, to check a copy against 'ds keys status'."""
    from deltasleuth.security.crypto import fingerprint

    with _errors():
        keyring = fingerprint(path)
    for kek in keyring.ids:
        typer.echo(kek + (" (current)" if kek == keyring.current else ""))


@keys_app.command("migrate")
def keys_migrate(
    accept_missing: Annotated[
        bool, _o("Record every legacy blob as missing when blobs/sha256 holds none.", "--accept-missing")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Seal the raw keys, headers, chat, scan points and blobs that releases before 006 stored in clear,
    then end plaintext storage (migration 007). Offline: stop the app first. Safe to interrupt and run
    again (docs/mvp/RETENTION.md §7.2)."""
    from deltasleuth.security import legacy

    who = _local(config)
    _siem()

    async def main() -> dict[str, int]:
        return await legacy.migrate(load_settings(config), who, accept_missing=accept_missing)

    n = _run(main(), legacy.INTERRUPTED)
    typer.echo(
        f"sealed: {n['keys']} key(s), {n['headers']} header(s), {n['chat_messages']} chat message(s) and"
        f" {n['scans']} scan point(s) under {n['runs']} new run data key(s); {n['blobs']} blob(s)"
        f" re-encrypted, {n['missing_blobs']} missing; plaintext storage ended (migration 007)"
    )


# -- four-eyes approvals (docs/mvp/APPROVALS.md §6.3) ----------------------------------------------

_STATUSES = ("requested", "approved", "rejected", "revoked")
_KINDS = ("spec", "signoff")
_LIVE = ("queued", "running", "paused")  # the runs --needed looks at: they run again


@approvals_app.callback()
def approvals_list(
    command: typer.Context,
    status: Annotated[str | None, _o("Only this status: requested, approved, rejected or revoked.")] = None,
    kind: Annotated[str | None, _o("Only this kind: spec or signoff.")] = None,
    needed: Annotated[
        bool,
        _o("List instead the spec versions live runs use that need an approval and have none.", "--needed"),
    ] = False,
    limit: Annotated[int | None, typer.Option(min=1, max=500, help="At most this many (default 50).")] = None,
    as_json: Annotated[bool, _o("Print them as JSON.", "--json")] = False,
    config: ConfigOpt = None,
) -> None:
    """List the four-eyes approvals and sign-offs, newest first; or revoke one. The CLI can neither
    request nor grant one: people do, signed in (docs/mvp/APPROVALS.md)."""
    if command.invoked_subcommand is not None:
        if needed or as_json or any(option is not None for option in (status, kind, limit, config)):
            _fail(f"put the options after '{command.invoked_subcommand}'")
        return
    if status is not None and status not in _STATUSES:
        _fail(f"--status is one of {', '.join(_STATUSES)}")
    if kind is not None and kind not in _KINDS:
        _fail(f"--kind is one of {', '.join(_KINDS)}")
    if needed:
        if status is not None or kind is not None or limit is not None:
            _fail("--needed lists spec versions: it takes no --status, --kind or --limit")
        _needed(_in_context(config, _unapproved_versions), as_json)
        return

    async def work(ctx: Context) -> list[repo.Row]:
        async with ctx.pool.connection() as conn:
            return (await repo.list_approvals(conn, status=status, kind=kind, limit=limit or 50))[0]

    rows = _in_context(config, work)
    if as_json:
        typer.echo(_json(rows))
        return
    for a in rows:
        spec = f"spec {a['spec_id']} ({a['spec_name']} {a['spec_version']})"
        what = (
            f"cycle {a['cycle']} of run {a['run_id']}" if a["kind"] == "signoff" else ", ".join(a["scopes"])
        )
        line = f"{a['approval_id']}  {a['kind']}  {a['status']}  {spec}  {what or 'no scopes'}"
        line += f"  requested by {_named(a['requested_by'], a['requested_by_name'])} {_at(a['requested_at'])}"
        if a["first_by"] is not None:
            line += f"  step 1 by {_named(a['first_by'], a['first_by_name'])} {_at(a['first_at'])}"
        if a["decided_by"] is not None:
            line += f"  decided by {_named(a['decided_by'], a['decided_by_name'])} {_at(a['decided_at'])}"
        if a["revoked_by"] is not None:
            by = _named(a["revoked_by"], a["revoked_by_name"])
            line += f"  revoked by {by} {_at(a['revoked_at'])}: {a['revoke_reason']!r}"
        typer.echo(line)


async def _unapproved_versions(ctx: Context) -> list[dict[str, Any]]:
    """The spec versions of live runs that need an approval their active one (if any) does not give,
    each with those runs."""
    async with ctx.pool.connection() as conn:
        live = [r for status in _LIVE for r in await repo.pages(repo.list_runs, conn, status=status)]
        versions = []
        for spec_id in sorted({r["spec_id"] for r in live}):
            needs = approvals.needs(await ctx.spec(conn, spec_id))
            active = await repo.active_spec_approval(conn, spec_id)
            if not needs or (active is not None and set(needs) <= set(active["scopes"])):
                continue
            runs = sorted((r for r in live if r["spec_id"] == spec_id), key=lambda r: r["run_id"])
            versions.append({
                "spec_id": spec_id, "name": runs[0]["spec_name"], "version": runs[0]["spec_version"],
                "needs": needs, "approval_id": active and active["approval_id"],
                "runs": [{"run_id": r["run_id"], "status": r["status"]} for r in runs],
            })  # fmt: skip
    return versions


def _needed(versions: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        typer.echo(_json(versions))
        return
    for v in versions:
        runs = ", ".join(f"run {r['run_id']} ({r['status']})" for r in v["runs"])
        covers = f" ({v['approval_id']} does not cover them all)" if v["approval_id"] else ""
        typer.echo(
            f"spec {v['spec_id']} ({v['name']} {v['version']}) needs {', '.join(v['needs'])}{covers}: {runs}"
        )
    if not versions:
        typer.echo("every live run's spec version has the approval it needs")


@approvals_app.command("revoke")
def approvals_revoke(
    approval_id: Annotated[str, _a("The approval id (A-…).")],
    reason: Annotated[str, _o(REASON)],
    config: ConfigOpt = None,
) -> None:
    """Break-glass: revoke an approved spec approval or sign-off. A spec approval's running runs pause,
    its queued one-shot runs stop and the sign-offs citing it are revoked; open ones are refused at
    their decision."""
    from deltasleuth.runner import purge as purger

    if problem := purger.reason_error(reason):
        _fail(problem)
    who = _local(config)

    async def work(ctx: Context) -> dict[str, Any]:
        async with repo.audited(ctx.pool) as (conn, audit):
            approval = await repo.get_approval(conn, approval_id)
            if approval is not None and rawvalues.key_shaped(
                await ctx.spec(conn, approval["spec_id"]), reason
            ):
                raise ValueError(rawvalues.KEY_SHAPED)  # audit rows and packs carry it forever
            return await approvals.revoke(conn, audit, approval_id, reason, who=who)

    done = _in_context(config, work)
    typer.echo(
        f"revoked {approval_id}; runs paused: {', '.join(done['paused']) or '-'}; runs stopped:"
        f" {', '.join(done['stopped']) or '-'}; sign-offs revoked: {', '.join(done['revoked']) or '-'}"
    )


@auth_app.command("check")
def auth_check(config: ConfigOpt = None) -> None:
    """Check auth.oidc against the IdP, without a database: discovery, the client credential, the
    signing keys and the clock. Prints the URIs to register with the IdP; exit 2 names the failing
    check."""
    from deltasleuth.auth.oidc import Oidc, fitting

    with _errors():
        auth = load_settings(config).auth
        if auth is None or auth.oidc is None:
            mode = "developer" if auth is None else "tokens"
            raise SettingsError(
                f"ds auth check needs the auth.oidc settings: these settings give {mode} mode"
            )
        oidc = Oidc(auth.oidc, auth.public_url, secrets.token_bytes(32))  # reads the client credential
    settings = auth.oidc
    if not _approvers(settings):
        typer.echo(NO_APPROVERS, err=True)
    typer.echo(f"client credential: ok ({settings.client_auth})")
    keys = _run(oidc.check())
    typer.echo(f"discovery: ok ({settings.issuer})")
    usable = False
    for key in keys:  # IdP values are quoted, so they cannot write to the terminal
        algs = fitting(key, settings.id_token_algs)
        usable = usable or bool(algs)
        unfit = f"none of auth.oidc.id_token_algs (kty {key.get('kty')!r}, alg {key.get('alg')!r})"
        typer.echo(f"signing key {key.get('kid')!r}: {', '.join(algs) or unfit}")
    if not usable:
        _fail(f"jwks: no key fits auth.oidc.id_token_algs {', '.join(settings.id_token_algs)}")
    if oidc.date_offset is None:
        typer.echo("clock: unknown (the IdP sent no Date header)")
    else:
        offset = round(oidc.date_offset)
        typer.echo(f"clock offset: {offset:+d} s (the IdP's clock minus this host's)")
        if abs(offset) > settings.clock_skew_seconds:
            typer.echo(
                f"warning: the clocks differ by {abs(offset)} s, over auth.oidc.clock_skew_seconds"
                f" ({settings.clock_skew_seconds}): sign-ins may fail until they agree",
                err=True,
            )
    typer.echo(f"redirect URI to register: {oidc.redirect_uri}")
    typer.echo(f"post-logout redirect URI to register: {oidc.post_logout_redirect_uri}")
