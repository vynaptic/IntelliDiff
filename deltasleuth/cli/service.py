"""``ds init``, ``migrate``, ``db grants``, ``serve`` and ``worker``: the store set up, and the services
run."""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket
from typing import TYPE_CHECKING, Annotated

import typer

from deltasleuth.cli.common import (
    NO_APPROVERS,
    ConfigOpt,
    _approvers,
    _errors,
    _in_context,
    _notify_warned,
    _o,
    _run,
    _siem,
    _stopping,
    _Stream,
    _warned,
)
from deltasleuth.cli.mcp import MCP_CLEAR, _mcp_logs, _mcp_server, _mcp_serves
from deltasleuth.config import SettingsError, load_settings
from deltasleuth.security.signing import PackSigner

if TYPE_CHECKING:
    from deltasleuth.context import Context


DEVELOPER_MODE = (
    "warning: developer mode (no auth settings): every local user and process acts as admin; never expose"
    " this port, directly or through a reverse proxy or port forward"
)


db_app = typer.Typer(no_args_is_help=True, help="The store's database roles.")


def _service_logs() -> None:
    """Give the package's loggers (``deltasleuth``) one handler on stderr from INFO up, each line its time,
    level and logger, for ``ds serve`` and ``ds worker``: a worker's and a schedule's lines are seen, not
    left to Python's last-resort handler, which drops INFO. They stop at it, so no record is printed
    twice; the audit copy (``_siem``) and the MCP server's lines have handlers of their own and stop there
    too, so stdout keeps the audit lines alone."""
    package = logging.getLogger("deltasleuth")
    if not any(isinstance(h, _Stream) for h in package.handlers):
        handler = _Stream("stderr")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        package.addHandler(handler)
    package.setLevel(logging.INFO)
    package.propagate = False


def _migration_logs() -> None:
    """For ``ds migrate``: each migration's lines on stderr as it starts and ends, as ``_service_logs`` words
    them, which Python's last-resort handler would drop (INFO). Only the migration logger changes, and its
    records still propagate."""
    progress = logging.getLogger("deltasleuth.store.db")
    if not any(isinstance(h, _Stream) for h in progress.handlers):
        handler = _Stream("stderr")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        progress.addHandler(handler)
    progress.setLevel(logging.INFO)


def init(config: ConfigOpt = None) -> None:
    """Create the workspace and, without the encryption settings, its tokeniser key and keyring (mode
    0600); existing keys are kept."""
    from deltasleuth.context import init_workspace

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


def migrate(config: ConfigOpt = None) -> None:
    """Apply the pending database migrations, each logged as it runs, as the schema's owner: the role of
    migrate_dsn_ref, else of database_dsn_ref. Without migrate_dsn_ref ds serve and ds worker apply them as
    they start; every other command refuses while one is pending."""
    import psycopg
    from psycopg_pool import AsyncConnectionPool

    from deltasleuth.context import ContextError
    from deltasleuth.core.secrets import read_secret
    from deltasleuth.store.db import WAIT, DatabaseRefused, admitted, opening
    from deltasleuth.store.db import migrate as apply

    _migration_logs()
    _siem()

    async def main() -> list[int]:
        settings = load_settings(config)
        named = "migrate_dsn_ref" if settings.migrate_dsn_ref else "database_dsn_ref"
        dsn = read_secret(settings.migrate_dsn_ref or settings.database_dsn_ref)
        try:
            await admitted(dsn)
        except DatabaseRefused as exc:  # its words name database_dsn_ref
            raise DatabaseRefused(str(exc).replace("database_dsn_ref", named)) from None
        async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=1) as pool:
            with opening():  # entering the pool does not wait: its lines would come during the migration
                try:
                    await pool.open(wait=True, timeout=WAIT)
                except psycopg.OperationalError as exc:
                    unreachable = f"the database is unreachable ({type(exc).__name__}); check {named}"
                    raise ContextError(unreachable) from None
            return await apply(pool)

    applied = _run(main())
    typer.echo(f"migrations applied: {', '.join(map(str, applied))}" if applied else "schema up to date")


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
                raise SettingsError(f"serving on {host} needs the auth settings")
            typer.echo(DEVELOPER_MODE, err=True)
        elif settings.auth.oidc is not None:
            if not _approvers(settings.auth.oidc):
                typer.echo(NO_APPROVERS, err=True)
            load_credential(settings.auth.oidc)  # the server's start reads it too, without this one line
        if settings.packs is not None:
            PackSigner.load(settings.packs.signing_key_ref)  # likewise the evidence packs' signing key
        if settings.mcp is not None and settings.mcp.http:
            _mcp_server()  # likewise the MCP library's import
        _bindable(host, port)  # last: before the store is opened, so no system.start row and no worker
    if not WEB_DIST.is_dir():
        typer.echo(
            f"warning: the web UI is not built ({WEB_DIST} is missing): / answers 404 and only the API under"
            " /v1 is served; run 'npm --prefix web ci' and 'npm --prefix web run build' in the checkout, then"
            " start ds serve again",
            err=True,
        )
    _notify_warned(settings)
    if settings.mcp is not None and settings.mcp.http:
        typer.echo(f"mcp: POST /v1/mcp is served; {_mcp_serves(settings.mcp)}", err=True)
        if "production_clear_declared" in settings.mcp.data_classes:
            typer.echo(MCP_CLEAR, err=True)
        _mcp_logs(bare=False)
    _service_logs()
    _siem("stdout")
    _in_context(config, _warned, apply_migrations=True)
    logging.getLogger("uvicorn.access").addFilter(AuthQueryFilter())
    # proxy_headers=False: only auth.trusted_proxies may name the client.
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


def worker(config: ConfigOpt = None) -> None:
    """Process the keys of running runs until interrupted or terminated; unfinished keys are handed back."""
    from deltasleuth.runner import run_worker
    from deltasleuth.store import chain

    async def work(ctx: Context) -> None:
        await _warned(ctx)
        _notify_warned(ctx.settings)
        async with chain.kept(ctx):  # the roles line, and the audit chain anchored at start and stop
            with _stopping() as stop:
                await run_worker(ctx, stop=stop)

    _service_logs()
    _siem("stdout")
    _in_context(config, work, apply_migrations=True)


@db_app.command("grants")
def db_grants(
    role: Annotated[str, _o("The application's role: made, without a password, if it does not exist.")] = (
        "deltasleuth_app"
    ),
) -> None:
    """Print the SQL that gives the application's own database role rights on the data of schema ds and
    nothing more: it owns nothing, may add no trigger and may not set session_replication_role, so whoever
    holds it cannot switch the audit log's triggers off. No settings are read.

    To use it: run 'ds migrate' as the schema's owner; run this SQL as that owner in one transaction (psql
    -v ON_ERROR_STOP=1 -1); set the role's password with \\password in psql; write the role's address to
    the file database_dsn_ref names, and the owner's to the file migrate_dsn_ref names, which only 'ds
    migrate' reads, so keep it from the app (ds serve and ds worker then apply no migration); restart the
    app. Its start log and 'ds audit verify' then say "roles: separate"."""
    from deltasleuth.store import repo

    with _errors():
        typer.echo(repo.grants(role), nl=False)
