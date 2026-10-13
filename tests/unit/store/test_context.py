"""Settings and the shared context: its keys (docs/mvp/RETENTION.md §1, §6.1) and its store, and what the
first connection says of a database that refuses or does not answer (docs/mvp/FIRSTRUN.md §8)."""

from __future__ import annotations

import asyncio
import itertools
import logging
import socket
import stat
import time
import traceback
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from deltasleuth.auth.oidc import Oidc
from deltasleuth.config import SettingsError, load_settings
from deltasleuth.context import Context, ContextError, init_workspace, login_key
from deltasleuth.core.types import Observation, Side
from deltasleuth.evidence.store import EvidenceKeyError
from deltasleuth.security.crypto import Keyring, KeyringError, key_id
from deltasleuth.security.tokenise import KeyFileError, read_key
from deltasleuth.store import db, repo
from deltasleuth.store.db import migrate
from tests.conftest import create_run, encryption

RUN_ID = "R-" + "0" * 26


def settings_file(tmp_path: Path, dsn: str = "postgresql://unused", **overrides: Any) -> Path:
    (tmp_path / "dsn").write_text(dsn + "\n")
    (tmp_path / "targets.yaml").write_text('targets:\n  svc-a: {base_url: "https://a.example.com"}\n')
    doc = {
        "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
        "evidence_dir": "evidence",
        "workspace_dir": "workspace",
        "targets_file": "targets.yaml",
    }
    path = tmp_path / "deltasleuth.yaml"
    path.write_text(yaml.safe_dump(doc | overrides))
    return path


def test_settings_resolve_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DS_CONFIG", str(settings_file(tmp_path, worker={"concurrency": 2})))
    settings = load_settings()
    assert (settings.evidence_dir, settings.targets_file) == (
        tmp_path / "evidence",
        tmp_path / "targets.yaml",
    )
    assert (settings.worker.concurrency, settings.worker.lease_seconds, settings.llm_profiles_file) == (
        2,
        120,
        None,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"database_dsn_ref": "postgresql://user:hunter2@db/ds"},  # credentials only by file: reference
        {"worker": {"concurrency": 0}},
        {"workers": 4},  # a mistyped setting is not ignored
    ],
)
def test_invalid_settings_are_refused(tmp_path: Path, override: dict[str, Any]) -> None:
    with pytest.raises(SettingsError) as refused:
        load_settings(settings_file(tmp_path, **override))
    assert "hunter2" not in str(refused.value)


async def test_the_context_needs_an_initialised_workspace(tmp_path: Path) -> None:
    with pytest.raises(ContextError, match="ds init"):
        async with Context.open(load_settings(settings_file(tmp_path))):
            pass


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_developer_mode_keeps_both_keys_in_the_workspace(tmp_path: Path) -> None:
    settings = load_settings(settings_file(tmp_path))
    key, kek = init_workspace(settings)
    written = key.read_text(), kek.read_text()
    assert (key, kek) == (tmp_path / "workspace" / "tokeniser.key", tmp_path / "workspace" / "kek")
    assert init_workspace(settings) == (key, kek) and (key.read_text(), kek.read_text()) == written
    assert (mode(key), mode(kek), len(Keyring.from_file(f"file:{kek}").ids)) == (0o600, 0o600, 1)
    kek.chmod(0o640)
    with pytest.raises(KeyringError, match=r"keyring .*kek must not be accessible by group or others"):
        async with Context.open(settings):
            pass
    kek.unlink()  # a workspace initialised before the keyring
    with pytest.raises(ContextError, match=f"keyring {kek} not found; run 'ds init' first"):
        async with Context.open(settings):
            pass


async def test_with_encryption_the_keys_are_secrets_never_created(tmp_path: Path) -> None:
    refs = encryption(tmp_path / "secrets")
    settings = load_settings(settings_file(tmp_path, encryption=refs))
    assert init_workspace(settings) == () and list((tmp_path / "workspace").iterdir()) == []
    (tmp_path / "secrets" / "kek").write_text("not hex\n")
    with pytest.raises(KeyringError, match=r"keyring .*kek line 1: expected 64 hex"):
        async with Context.open(settings):
            pass
    (tmp_path / "secrets" / "kek").write_text("ab" * 32 + "\n")
    (tmp_path / "secrets" / "tokeniser").unlink()
    with pytest.raises(KeyFileError, match="cannot read secret file"):
        async with Context.open(settings):
            pass
    assert not (tmp_path / "secrets" / "tokeniser").exists()


async def test_an_unknown_default_llm_profile_is_refused(tmp_path: Path) -> None:
    profiles = {
        "hosts": {"llm.example.com": "internal"},
        "profiles": {
            "gateway": {"dialect": "openai_chat", "base_url": "https://llm.example.com/v1", "model": "m"}
        },
    }
    (tmp_path / "llm.yaml").write_text(yaml.safe_dump(profiles))
    settings = load_settings(settings_file(tmp_path, llm_profiles_file="llm.yaml", llm_profile="other"))
    init_workspace(settings)
    with pytest.raises(ContextError, match="other"):
        async with Context.open(settings):
            pass


@pytest.mark.db
async def test_the_context_opens_a_migrated_store_with_ai_off(tmp_path: Path, pg_dsn: str) -> None:
    settings = load_settings(settings_file(tmp_path, pg_dsn))
    init_workspace(settings)
    async with Context.open(settings) as ctx:
        assert (ctx.llm, ctx.oidc, list(ctx.targets)) == (None, None, ["svc-a"])
        assert await migrate(ctx.pool) == []
        async with ctx.pool.connection() as conn:
            assert await repo.list_specs(conn) == []
    assert ctx.pool.closed


@pytest.mark.db
async def test_the_pool_is_sized_for_mcp_s_calls(tmp_path: Path, pg_dsn: str) -> None:
    """docs/mvp/MCP.md §4, §5.2: ``mcp.concurrency`` more connections where MCP is served over HTTP, and
    the pool ``ds mcp`` asks for: one idle connection a process."""
    mcp = {"data_classes": ["aggregates"], "concurrency": 3}
    sizes = {}
    for name, block in (("plain", None), ("stdio", mcp), ("http", mcp | {"http": True})):
        settings = load_settings(settings_file(tmp_path, pg_dsn, **({"mcp": block} if block else {})))
        init_workspace(settings)
        async with Context.open(settings) as ctx:
            sizes[name] = (ctx.pool.min_size, ctx.pool.max_size)
    assert sizes == {"plain": (4, 8), "stdio": (4, 8), "http": (4, 11)}
    async with Context.open(settings, pool=(1, mcp["concurrency"] + 1)) as ctx:
        assert (ctx.pool.min_size, ctx.pool.max_size) == (1, 4)
        async with ctx.pool.connection() as conn:
            held = await (await conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            )).fetchone()  # fmt: skip
    assert held == (1,)


@pytest.mark.db
async def test_a_cli_context_builds_no_idp_client_and_reads_no_client_credential(
    tmp_path: Path, pg_dsn: str
) -> None:
    oidc = {
        "issuer": "https://idp.example",
        "client_id": "deltasleuth",
        "client_secret_ref": f"file:{tmp_path / 'not-mounted'}",
        "roles": {"admin": ["DS.Admin"]},
    }
    auth = {"public_url": "https://ds.example", "oidc": oidc}
    settings = load_settings(
        settings_file(tmp_path, pg_dsn, auth=auth, encryption=encryption(tmp_path / "s"))
    )
    init_workspace(settings)
    async with Context.open(settings) as ctx:
        assert (ctx.settings.auth_mode, ctx.oidc) == ("oidc", None)


@pytest.mark.db
async def test_the_server_s_context_builds_the_idp_client_in_oidc_mode_only(
    tmp_path: Path, pg_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "secret").write_text("client-s3cret\n")
    oidc = {
        "issuer": "http://127.0.0.1:1",  # nothing listens: the server starts all the same
        "client_id": "deltasleuth",
        "client_secret_ref": f"file:{tmp_path / 'secret'}",
        "roles": {"admin": ["DS.Admin"]},
    }
    refs = encryption(tmp_path / "secrets")
    tokens = load_settings(
        settings_file(tmp_path, pg_dsn, auth={"public_url": "https://ds.example"}, encryption=refs)
    )
    init_workspace(tokens)
    async with Context.open(tokens, oidc=True) as ctx:
        assert ctx.oidc is None
    sso = load_settings(
        settings_file(
            tmp_path, pg_dsn, auth={"public_url": "https://ds.example", "oidc": oidc}, encryption=refs
        )
    )
    async with Context.open(sso, oidc=True) as ctx:
        assert isinstance(ctx.oidc, Oidc) and ctx.oidc.redirect_uri == "https://ds.example/v1/auth/callback"
        assert ctx.oidc.login_key == login_key(read_key(refs["tokeniser_key_ref"]))
    assert "warning: identity provider unreachable at start; sign-in retries on first use" in caplog.messages


# -- keys at start (docs/mvp/RETENTION.md §6.1) ----------------------------------------------------


def observation(body: bytes) -> Observation:
    return Observation("K1", Side.A, 200, body, "2026-09-27T08:00:00.000000Z")


async def key_rows(ctx: Context) -> list[tuple[str, str]]:
    async with ctx.pool.connection() as conn:
        return [(row["scope"], row["kek"]) for row in await repo.data_keys(conn)]


async def key_audit(ctx: Context) -> list[tuple[str, str, str, Any]]:
    async with ctx.pool.connection() as conn:
        rows = await repo.pages(repo.list_audit, conn)
    keys = [row for row in reversed(rows) if row["action"].startswith("keys.")]
    return [(row["actor"], row["action"], row["object"], row["detail"]) for row in keys]


@pytest.mark.db
async def test_the_first_start_creates_the_address_and_evidence_keys(tmp_path: Path, pg_dsn: str) -> None:
    settings = load_settings(settings_file(tmp_path, pg_dsn))
    init_workspace(settings)
    async with Context.open(settings) as ctx:
        kek, rows = ctx.keyring.current, await key_rows(ctx)
        stored = ctx.evidence.put_blob(observation(b'{"id": 1}'))
        assert ctx.missing_keks == {}
    [evidence] = [scope for scope, _ in rows if scope != "address"]
    assert sorted(rows) == sorted([("address", kek), (evidence, kek)]) and evidence.startswith("E-")
    async with Context.open(settings) as ctx:  # the next start loads them
        assert await key_rows(ctx) == rows
        assert ctx.evidence.get_body(stored.content_hash or "") == b'{"id": 1}'
        assert await key_audit(ctx) == [
            ("system:migration", "keys.created", "address", {"kek": kek}),
            ("system:migration", "keys.created", evidence, {"kek": kek}),
        ]


@pytest.mark.db
async def test_only_the_server_rewraps_after_a_kek_is_appended(tmp_path: Path, pg_dsn: str) -> None:
    settings = load_settings(settings_file(tmp_path, pg_dsn))
    init_workspace(settings)
    async with Context.open(settings) as ctx:
        old, before = ctx.keyring.current, await key_rows(ctx)
        stored = ctx.evidence.put_blob(observation(b'{"old": 1}'))
    with (tmp_path / "workspace" / "kek").open("a") as keyring:
        keyring.write("# rotated\n" + "c3" * 32 + "\n")
    async with Context.open(settings) as ctx:  # a CLI changes nothing the running app cannot read
        assert await key_rows(ctx) == before and len(await key_audit(ctx)) == 2
    async with Context.open(settings, rewrap=True) as ctx:
        new = ctx.keyring.current
        after = await key_rows(ctx)
        audit = await key_audit(ctx)
        assert ctx.evidence.get_body(stored.content_hash or "") == b'{"old": 1}'
        fresh = ctx.evidence.put_blob(observation(b'{"new": 1}'))
        [(_, writer, _)] = [b for b in ctx.evidence.walk() if b[0] == fresh.content_hash]
    [added] = {scope for scope, _ in after} - {scope for scope, _ in before}
    assert new != old and {kek for _, kek in after} == {new} and writer == added
    assert audit[2:] == [
        ("system:migration", "keys.created", added, {"kek": new}),
        ("system:migration", "keys.rewrap", "keys", {"from": old, "to": new, "rows": 2}),
    ]


@pytest.mark.db
async def test_a_cli_started_after_a_kek_is_appended_creates_runs_the_running_server_opens(
    tmp_path: Path, pg_dsn: str
) -> None:
    settings = load_settings(settings_file(tmp_path, pg_dsn))
    init_workspace(settings)
    async with Context.open(settings, rewrap=True) as server:
        async with server.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO ds.specs (spec_id, name, version, content_hash, body)"
                " VALUES (1, 'd', '1', 'h', '{}')"
            )
        with (tmp_path / "workspace" / "kek").open("a") as keyring:
            keyring.write("# rotated\n" + "c3" * 32 + "\n")
        async with Context.open(settings) as cli, cli.pool.connection() as conn:  # between append and restart
            run_id, _ = await create_run(cli, conn, 1, "batch", {"C00012027": "tok:c:v1:A"}, {})
        async with server.pool.connection() as conn:
            stored = await repo.get_key_detail(conn, run_id, "tok:c:v1:A")
            assert (await server.sealer(conn, run_id)).open_row(stored)["key"] == "C00012027"
            assert (await repo.data_key(conn, run_id))["kek"] == server.keyring.current  # the old KEK
    async with Context.open(settings, rewrap=True) as server:  # the restart moves new keys onto the new KEK
        async with Context.open(settings) as cli, cli.pool.connection() as conn:
            later, _ = await create_run(cli, conn, 1, "batch", {"C00012028": "tok:c:v1:B"}, {})
            new = key_id("K-", b"\xc3" * 32)
            assert (await repo.data_key(conn, later))["kek"] == server.keyring.current == new


@pytest.mark.db
async def test_a_missing_kek_degrades_the_start_until_restored(
    tmp_path: Path, pg_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = load_settings(settings_file(tmp_path, pg_dsn))
    init_workspace(settings)
    kek_file = tmp_path / "workspace" / "kek"
    original = kek_file.read_text()
    async with Context.open(settings) as ctx:
        lost = ctx.keyring.current
        stored = ctx.evidence.put_blob(observation(b'{"id": 1}'))
    kek_file.write_text("d4" * 32 + "\n")  # the line was lost
    async with Context.open(settings, rewrap=True) as ctx:
        assert ctx.missing_keks == {lost: 2}
        with pytest.raises(EvidenceKeyError, match=f"whose KEK {lost} the keyring lacks"):
            ctx.evidence.get_body(stored.content_hash or "")
        with pytest.raises(EvidenceKeyError, match=f"address key needs {lost}"):
            ctx.evidence.put_blob(observation(b"{}"))
    assert (
        f"warning: the keyring lacks {lost}, which wraps 2 data key(s): restore the line from the vault and"
        " restart"
    ) in caplog.messages
    kek_file.write_text(original + "d4" * 32 + "\n")  # restored from the vault
    caplog.clear()
    async with Context.open(settings, rewrap=True) as ctx:
        assert ctx.missing_keks == {} and ctx.evidence.get_body(stored.content_hash or "") == b'{"id": 1}'
        assert {kek for _, kek in await key_rows(ctx)} == {ctx.keyring.current}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.db
async def test_a_run_s_sealer_is_cached_and_needs_its_kek(tmp_path: Path, pg_dsn: str) -> None:
    settings = load_settings(settings_file(tmp_path, pg_dsn))
    init_workspace(settings)
    async with Context.open(settings) as ctx:
        kek, wrapped, sealer = ctx.new_sealer(RUN_ID)
        sealed = sealer.seal("run_key", 0, "tok:c:v1:A", b"C00012027")
        async with ctx.pool.connection() as conn:
            await repo.insert_data_key(conn, RUN_ID, kek, wrapped)
            opened = await ctx.sealer(conn, RUN_ID)
            assert opened is await ctx.sealer(conn, RUN_ID) and len(wrapped) == 40
            assert opened.open("run_key", 0, "tok:c:v1:A", sealed) == b"C00012027"
            with pytest.raises(LookupError, match="has no data key"):
                await ctx.sealer(conn, "R-" + "1" * 26)
            other = (
                "R-" + "2" * 26
            )  # the wrapped data key moved to another run opens nothing sealed for this one
            await repo.insert_data_key(conn, other, kek, wrapped)
            with pytest.raises(ValueError, match="sealed value does not authenticate"):
                (await ctx.sealer(conn, other)).open("run_key", 0, "tok:c:v1:A", sealed)
    (tmp_path / "workspace" / "kek").write_text("e5" * 32 + "\n")
    async with Context.open(settings) as ctx, ctx.pool.connection() as conn:
        with pytest.raises(KeyringError, match=f"the keyring lacks {kek}"):
            await ctx.sealer(conn, RUN_ID)


# -- the first connection (docs/mvp/FIRSTRUN.md §8 S3, S4) -----------------------------------------


@pytest.fixture
def no_password_of_this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """libpq without a password from the environment or from ``~/.pgpass``."""
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "no-pgpass"))


@pytest.fixture
def tries(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """When ``admitted`` connected, each try; the tries a fifth of a second apart, not two."""
    connect, began = psycopg.AsyncConnection.connect, []

    async def tried(*args: Any, **kwargs: Any) -> Any:
        began.append(time.monotonic())
        return await connect(*args, **kwargs)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", staticmethod(tried))
    monkeypatch.setattr(db, "APART", 0.2)
    return began


@pytest.mark.db
@pytest.mark.usefixtures("no_password_of_this_machine")
async def test_admitted_tells_a_refusal_from_no_answer(pg_dsn: str, tries: list[float]) -> None:
    """S3: a server that asked for a password and refused is said after three tries, and at once where no
    password was given; one that does not answer is left to the pool's wait."""
    assert db.TRIES == 3
    refusals = {
        "a wrong password": (pg_dsn.replace(":test@", ":wrong@"), 3),
        "no password": (pg_dsn.replace(":test@", "@"), 1),
        "a wrong user": (pg_dsn.replace("postgres:test@", "nobody:test@"), 3),
        "a wrong database": (pg_dsn.rsplit("/", 1)[0] + "/no_such_database", 3),
    }
    for what, (dsn, asked) in refusals.items():
        with pytest.raises(db.DatabaseRefused) as refused:
            await db.admitted(dsn)
        assert (str(refused.value), len(tries)) == (db.REFUSED, asked), what
        assert all(later - earlier >= db.APART for earlier, later in itertools.pairwise(tries)), what
        tries.clear()
    assert isinstance(refused.value, ValueError)
    with socket.create_server(("127.0.0.1", 0)) as closed:
        port = closed.getsockname()[1]
    for dsn in (pg_dsn, f"postgresql://postgres:test@127.0.0.1:{port}/postgres", "port=x"):
        assert await db.admitted(dsn) is None, dsn
    assert len(tries) == 3  # one each: nothing was refused after a password


@pytest.mark.db
@pytest.mark.usefixtures("no_password_of_this_machine")
async def test_a_connection_limit_that_frees_is_no_refusal(
    pg_server: str, pg_dsn: str, tries: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3: a role at its connection limit is refused after its right password, as a wrong password is. The
    probe is repeated, so a limit that frees between the tries passes, and one that stays ends in C5."""
    monkeypatch.setattr(db, "APART", 0.5)
    role = f"limited_{pg_dsn[-12:]}"
    dsn = pg_dsn.replace("postgres:test@", f"{role}:test@")

    async def free(held: psycopg.AsyncConnection) -> None:
        await asyncio.sleep(db.APART / 2)
        await held.close()

    async with await psycopg.AsyncConnection.connect(pg_server, autocommit=True) as admin:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'test' CONNECTION LIMIT 1")
        async with await psycopg.AsyncConnection.connect(dsn) as held:
            tries.clear()
            with pytest.raises(db.DatabaseRefused) as refused:
                await db.admitted(dsn)
            assert (str(refused.value), len(tries)) == (db.REFUSED, 3)
            tries.clear()
            freed = asyncio.create_task(free(held))
            assert await db.admitted(dsn) is None
            await freed
        assert len(tries) in (2, 3)  # refused once, or twice where the server took long to free the place
        await admin.execute(f"DROP ROLE {role}")


async def test_an_address_the_driver_cannot_read_is_refused_before_any_connection(tries: list[float]) -> None:
    """S3: the driver's words for such an address quote it, the password included, and an unencoded @ or /
    turns the password's tail into a host or a database name that a connection error would name."""
    unreadable = {
        "an unencoded %": "postgresql://deltasleuth:Dsn-Passw0rd%Zanary@127.0.0.1:1/deltasleuth",
        "a space": "postgresql://deltasleuth:Dsn-Passw0rd Canary@127.0.0.1:1/deltasleuth",
        "a %XX that is not UTF-8": "postgresql://deltasleuth:Dsn-Passw0rd%Canary@127.0.0.1:1/deltasleuth",
        "an unencoded @": "postgresql://deltasleuth:Dsn-Passw0rd@Canary@127.0.0.1:1/deltasleuth",
        "an unencoded /": "postgresql://deltasleuth:Dsn-Passw0rd/Canary@127.0.0.1:1/deltasleuth",
        "an unclosed bracket": "postgresql://deltasleuth:Dsn-Passw0rd-Canary@[::1/deltasleuth",
        "a keyword without its value": "host=127.0.0.1 password=Dsn-Passw0rd Canary",
    }
    for what, dsn in unreadable.items():
        with pytest.raises(db.DatabaseRefused) as refused:
            await db.admitted(dsn)
        said = "".join(traceback.format_exception(refused.value)) + repr(refused.value)
        assert str(refused.value) == db.UNREADABLE and "Canary" not in said and "Passw0rd" not in said, what
        assert refused.value.__cause__ is None and refused.value.__context__ is None, what
    assert tries == []  # no socket was opened
    for dsn in (  # an @ the driver reads inside a value is no piece of a password
        "postgresql://127.0.0.1:1/deltasleuth?user=svc@REALM.EXAMPLE",
        "postgresql://deltasleuth:pw@127.0.0.1:1/deltasleuth?application_name=ds@prod",
        "postgresql://deltasleuth:p%40w@127.0.0.1:1/deltasleuth",
        "host=127.0.0.1 port=1 dbname=deltasleuth password=p@ss",
    ):
        await db.admitted(dsn)  # nothing listens on port 1: left to the pool's wait
    assert len(tries) == 4


@pytest.mark.db
@pytest.mark.usefixtures("no_password_of_this_machine")
async def test_a_refusal_never_carries_the_drivers_words(tmp_path: Path, pg_dsn: str) -> None:
    """S3: the driver names the account and the database it was refused; the refusal and its traceback
    name neither, and ``Context.open`` ends in it before its pool waits."""
    address = pg_dsn.split("@")[1].split("/")[0]
    dsn = f"postgresql://Account-Canary:Dsn-Passw0rd-Canary@{address}/Database-Canary"
    settings = load_settings(settings_file(tmp_path, dsn))
    init_workspace(settings)
    began = time.monotonic()
    with pytest.raises(db.DatabaseRefused) as refused:
        async with Context.open(settings):
            pass
    said = "".join(traceback.format_exception(refused.value)) + repr(refused.value)
    assert str(refused.value) == db.REFUSED and time.monotonic() - began < db.WAIT / 2
    assert "Canary" not in said and "127.0.0.1" not in said and refused.value.__cause__ is None


def test_a_pool_line_is_logged_once_while_the_store_opens(caplog: pytest.LogCaptureFixture) -> None:
    """S4: the pool logs every failed try of its wait for the first connection in the same words."""
    pool, records = logging.getLogger("psycopg.pool"), []
    seen = logging.Handler()  # on the driver's logger itself: whatever an earlier test left above it
    seen.emit = records.append  # type: ignore[method-assign]
    caplog.set_level(logging.WARNING, logger=pool.name)
    pool.addHandler(seen)
    try:
        with db.opening():
            for why in ("refused", "refused", "timeout expired", "refused"):
                pool.warning("error connecting in %r: %s", "pool-1", why)
        pool.warning("error connecting in %r: %s", "pool-1", "refused")  # a server's later logs
    finally:
        pool.removeHandler(seen)
    assert [record.getMessage() for record in records] == [
        "error connecting in 'pool-1': refused",
        "error connecting in 'pool-1': timeout expired",
        "error connecting in 'pool-1': refused",
    ]
    assert pool.filters == []
