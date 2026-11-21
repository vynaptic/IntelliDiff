"""``ds session``, ``users`` and ``auth``: sign-in sessions, the people who sign in and the IdP
settings."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Annotated

import typer

from deltasleuth.auth.principal import ROLES
from deltasleuth.cli.common import (
    NO_APPROVERS,
    ConfigOpt,
    _a,
    _approvers,
    _at,
    _errors,
    _fail,
    _in_context,
    _json,
    _local,
    _named,
    _o,
    _run,
)
from deltasleuth.config import SettingsError, load_settings
from deltasleuth.errors import Invalid, NotFound

if TYPE_CHECKING:
    from deltasleuth.context import Context


session_app = typer.Typer(no_args_is_help=True, help="Sign-in sessions.")
users_app = typer.Typer(invoke_without_command=True)
auth_app = typer.Typer(no_args_is_help=True, help="Sign-in settings.")


@session_app.command("revoke")
def session_revoke(
    session: Annotated[str | None, _o("One session (W-…).")] = None,
    user: Annotated[str | None, _o("Every session of a user (U-…).")] = None,
    everyone: Annotated[bool, _o("Every session.", "--all")] = False,
    config: ConfigOpt = None,
) -> None:
    """Revoke sign-in sessions: one, a user's or every one. Each ends at its next request."""
    from deltasleuth.store import repo

    if (session is not None) + (user is not None) + everyone != 1:
        _fail("name one of --session W-…, --user U-… or --all")
    who = _local(config)

    async def work(ctx: Context) -> int:
        async with repo.audited(ctx.pool) as (conn, audit):
            if user is not None and await repo.get_user(conn, user) is None:
                raise NotFound(f"user {user} not found")
            count = await repo.revoke_sessions(
                conn, who.actor, session_id=session, user_id=user, everyone=everyone
            )
            if session is not None and not count:  # unknown, or already ended
                raise NotFound(f"session {session} not found")
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

    from deltasleuth.store import repo

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
    from deltasleuth.store import repo

    who = _local(config)

    async def work(ctx: Context) -> tuple[repo.Row, list[repo.Row], list[str]]:
        async with repo.audited(ctx.pool) as (conn, audit):
            if await repo.get_user(conn, user_id) is None:
                raise NotFound(f"user {user_id} not found")
            row = await repo.set_user_disabled(conn, user_id, who.actor)
            if row is None:
                raise Invalid(f"user {user_id} is already disabled")
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
    from deltasleuth.store import repo

    who = _local(config)

    async def work(ctx: Context) -> repo.Row:
        async with repo.audited(ctx.pool) as (conn, audit):
            user = await repo.get_user(conn, user_id)
            if user is None:
                raise NotFound(f"user {user_id} not found")
            if await repo.set_user_disabled(conn, user_id, None) is None:
                raise Invalid(f"user {user_id} is not disabled")
            await audit(who, "user.enable", user_id)
        return user

    user = _in_context(config, work)
    typer.echo(f"enabled {_named(user_id, user['username'])}; revoked tokens stay revoked")


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
