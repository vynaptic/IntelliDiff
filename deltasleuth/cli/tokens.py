"""``ds token``: API tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from deltasleuth.cli.common import ConfigOpt, _a, _at, _in_context, _local, _named, _o
from deltasleuth.errors import Invalid, NotFound

if TYPE_CHECKING:
    from deltasleuth.context import Context


token_app = typer.Typer(no_args_is_help=True, help="API tokens.")


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
    from deltasleuth.store import repo

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
    from deltasleuth.store import repo

    who = _local(config)

    async def work(ctx: Context) -> repo.Row:
        async with repo.audited(ctx.pool) as (conn, audit):
            if await repo.token_by_id(conn, token_id) is None:
                raise NotFound(f"token {token_id} not found")
            row = await repo.revoke_token(conn, token_id, who.actor)
            if row is None:
                raise Invalid(f"token {token_id} is already revoked")
            await audit(who, "token.revoke", token_id)
        return row

    row = _in_context(config, work)
    typer.echo(f"revoked token {_named(token_id, row['name'])}")


@token_app.command("list")
def token_list(config: ConfigOpt = None) -> None:
    """List the API tokens, newest first, never their secrets."""
    from deltasleuth.store import repo

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
