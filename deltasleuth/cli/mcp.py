"""``ds mcp``: the MCP tools for your own agent over stdin and stdout, and the mcp settings checked."""

from __future__ import annotations

import atexit
import importlib
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import typer

from deltasleuth.auth.principal import Principal, expand, local
from deltasleuth.cli.common import (
    EXIT_INTERNAL,
    ConfigOpt,
    _errors,
    _fail,
    _in_context,
    _o,
    _stopping,
    _Stream,
)
from deltasleuth.config import McpSettings, Settings, SettingsError, load_settings
from deltasleuth.errors import Invalid

if TYPE_CHECKING:
    from deltasleuth.context import Context


mcp_app = typer.Typer(invoke_without_command=True)


MCP_OFF = "MCP is off: the settings have no mcp block"
MCP_HOST_CHANNEL = (
    "ds mcp under auth is the host-administrator channel: use /v1/mcp with a named token, or pass"
    " --host-channel"
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


def _mcp_server() -> Any:
    """``deltasleuth.mcp.server``, whose import loads the MCP library: an ``OTEL_*`` setting that names what
    is not installed stops it with a ValueError naming that setting's value, which is refused as such."""
    try:
        return importlib.import_module("deltasleuth.mcp.server")
    except ValueError as exc:
        raise Invalid(str(exc)) from None


class _Bare(logging.Formatter):
    """A record's logger and level alone: neither its message nor its traceback, which may hold data."""

    def format(self, record: logging.LogRecord) -> str:
        return f"{record.name} {record.levelname}"


def _mcp_logs(*, bare: bool) -> None:
    """Put the MCP server's two loggers on stderr, neither left to Python's last-resort
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
    block serves them. This is a workstation's channel, in developer mode: the agent that
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
        from deltasleuth.mcp import tools

        server = _mcp_server()  # the MCP library's import, which this command alone needs

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
