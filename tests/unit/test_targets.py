"""Targets declared in the product (docs/mvp/DEPLOY.md §7, "In the product"): both lists load and the
targets file wins, an invalid list stops the start, an entry is judged as the targets file's and the list
is trusted for no more than the product writes, each credential lies in a file of its own for the owner
alone and is kept, replaced or needed again, a failed write leaves the list and its credentials as they
were and one after the switch is a made change, no file the list does not name stays in its secrets
folder, nothing is read, sent or removed through a symbolic link, the list never outgrows its reader and
goes with its last entry, and a change keeps the target's one gate."""

from __future__ import annotations

import errno
import inspect
import json
import logging
import os
import re
import stat
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from deltasleuth import targets as product
from deltasleuth.api.routes.meta import TargetIn
from deltasleuth.config import Settings, load_settings
from deltasleuth.context import Context, init_workspace
from deltasleuth.core.spec import SecretError, TargetsError, load_targets, read_secret
from deltasleuth.core.spec.documents import MAX_DOCUMENT_BYTES
from deltasleuth.core.spec.targets import declared
from deltasleuth.core.types import ExecutionConfig, Target, TargetAuth
from deltasleuth.evidence import fsio
from deltasleuth.runner.compare import connect
from tests.conftest import TESTER

BEARER = {"kind": "bearer"}
REF = "file:secrets/own.0123456789abcdef"  # a credential's reference as the product writes one
COOKIE = {"kind": "header", "header": "Cookie", "more": [{"header": "X-CSRF-Token"}]}


def _settings(tmp_path: Path, dsn: str = "postgresql://unused") -> Settings:
    """Developer-mode settings whose targets file declares ``filed`` alone."""
    (tmp_path / "dsn").write_text(dsn)
    (tmp_path / "targets.yaml").write_text("targets:\n  filed:\n    base_url: https://filed.example\n")
    doc = {
        "database_dsn_ref": f"file:{tmp_path / 'dsn'}",
        "evidence_dir": "evidence",
        "workspace_dir": "workspace",
        "targets_file": "targets.yaml",
    }
    (tmp_path / "deltasleuth.yaml").write_text(json.dumps(doc))
    return load_settings(tmp_path / "deltasleuth.yaml")


def _listed(settings: Settings, text: str) -> Path:
    """The product's list holding ``text``, as an earlier start left it."""
    listed = product.path(settings)
    listed.parent.mkdir(parents=True)
    listed.write_text(text)
    return listed


@pytest.fixture
async def ctx(tmp_path: Path, pg_dsn: str) -> AsyncIterator[Context]:
    settings = _settings(tmp_path, pg_dsn)
    init_workspace(settings)
    async with Context.open(settings) as opened:
        yield opened


def _body(
    base_url: str = "https://own.example/api", credential: str | None = None, **members: Any
) -> dict[str, Any]:
    """A PUT's body as the route hands it over; ``credential`` is its ``secret``."""
    return TargetIn(base_url=base_url, secret=credential, **members).model_dump(exclude_none=True)


async def _put(ctx: Context, target_id: str = "own", **members: Any) -> Target:
    return (await product.put(ctx, target_id, _body(**members), TESTER))[0]


def _credentials(ctx: Context) -> set[str]:
    """What the credential files hold."""
    return {file.read_text() for file in (product.path(ctx.settings).parent / "secrets").iterdir()}


async def _audited(ctx: Context) -> list[tuple[str, str, Any]]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT action, object, detail->>'secret' FROM ds.audit_log"
            " WHERE action LIKE 'target.%' ORDER BY id"
        )
        return await cur.fetchall()


def test_both_lists_load_and_the_file_wins(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(tmp_path)
    assert list(product.load(settings)) == ["filed"]  # no list: the targets file alone
    own = {"base_url": "https://own.example", "auth": {"kind": "bearer", "secret_ref": REF}}
    listed = _listed(
        settings, json.dumps({"targets": {"filed": {"base_url": "https://shadow.example"}, "own": own}})
    )
    registry = product.load(settings)
    assert {t: (registry.get(t).base_url, registry.get(t).declared_in) for t in registry} == {
        "filed": ("https://filed.example", "file"),
        "own": ("https://own.example", "product"),
    }
    assert registry.get("own").auth.secret_ref == f"file:{listed.parent / REF.removeprefix('file:')}"
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.WARNING,
            "warning: target filed of the product's list is ignored: the targets file declares it",
        )
    ]


@pytest.mark.parametrize(
    ("text", "issue"),
    [
        ("{", ("", "json_syntax")),
        ('{"targets": []}', ("/targets", "schema.type")),
        (
            '{"targets": {"own": {"base_url": "ftp://own.example"}}}',
            ("/targets/own/base_url", "invalid_base_url"),
        ),
        (
            '{"targets": {"own": {"base_url": "https://own.example", "secret": "s"}}}',
            ("/targets/own/secret", "schema.additionalProperties"),
        ),
        # what the targets file takes and the product never writes
        (
            '{"targets": {"own": {"dsn_ref": "file:db.dsn"}}}',
            ("/targets/own/dsn_ref", "schema.additionalProperties"),
        ),
        (
            '{"targets": {"own": {"base_url": "https://o.example", "capabilities": {"pagination": "page"}}}}',
            ("/targets/own/capabilities", "schema.const"),
        ),
        (
            '{"targets": {"own": {"base_url": "https://own.example",'
            ' "auth": {"kind": "bearer", "secret_ref": "file:/run/secrets/tokeniser"}}}}',
            ("/targets/own/auth/secret_ref", "schema.pattern"),
        ),
    ],
)
async def test_an_invalid_product_list_stops_the_start(
    tmp_path: Path, text: str, issue: tuple[str, str]
) -> None:
    settings = _settings(tmp_path)
    init_workspace(settings)
    listed = _listed(settings, text)
    with pytest.raises(TargetsError) as refused:
        async with Context.open(settings):
            pass
    assert [(i.pointer, i.code) for i in refused.value.issues] == [issue]
    assert refused.value.issues[0].message.endswith(f" (the product's list, {listed})")  # the file to remove


def _refusals(load: Any) -> set[tuple[str, str, str]]:
    with pytest.raises(TargetsError) as refused:
        load()
    return {(i.pointer, i.code, i.message) for i in refused.value.issues}


def test_a_declared_entry_is_judged_as_the_files(tmp_path: Path) -> None:
    """The targets file's schema, codes, defaults and address rules; a file with further credential
    headers (``more``) loads the same."""
    more = [{"header": "Cookie", "secret_ref": "file:secrets/own.2"}]
    auth = {"kind": "header", "header": "X-Api-Key", "secret_ref": "file:secrets/own.1", "more": more}
    absence = {"authoritative_absence": {"status": [404]}}
    entry = {
        "base_url": "HTTPS://Own.Example:8443/api/",
        "auth": auth,
        "rate_per_s": 5,
        "capabilities": absence,
    }
    file = tmp_path / "targets.json"

    def filed(target_id: str, entry: dict[str, Any]) -> Target:
        file.write_text(json.dumps({"targets": {target_id: entry}}))
        return load_targets(file).get(target_id)

    target = declared("own", entry, tmp_path)
    assert target == replace(filed("own", entry), declared_in="product")
    assert (target.base_url, target.max_concurrency) == ("https://own.example:8443/api", 4)
    assert target.auth.more == (("Cookie", f"file:{tmp_path / 'secrets' / 'own.2'}"),)
    ref = {"secret_ref": "file:k"}
    headers = [{"header": name, **ref} for name in ("X-B", "X-C", "X-D", "X-E")]
    refused = {
        ("own", "/targets/own/base_url", "private_network"): {"base_url": "http://localhost:8080"},
        ("own", "/targets/own/base_url", "insecure_http"): {
            "base_url": "http://own.example",
            "auth": {"kind": "bearer", **ref},
        },
        ("own", "/targets/own/auth", "schema.required"): {"base_url": "https://o.example", "auth": BEARER},
        ("own", "/targets/own/dsn_ref", "schema.pattern"): {"dsn_ref": "db.dsn"},
        ("Own_1", "/targets/Own_1", "schema.propertyNames"): {"base_url": "https://own.example"},
        ("own", "/targets/own/auth/more", "schema.maxItems"): {
            "base_url": "https://own.example",
            "auth": {"kind": "header", "header": "X-A", **ref, "more": headers},
        },
        ("own", "/targets/own/auth/more", "schema.not"): {
            "base_url": "https://own.example",
            "auth": {"kind": "bearer", **ref, "more": headers[:1]},
        },
        ("own", "/targets/own/auth/more/0/header", "schema.not"): {
            "base_url": "https://own.example",
            "auth": {"kind": "header", "header": "X-A", **ref, "more": [{"header": "Host", **ref}]},
        },
        ("own", "/targets/own/auth/more/1/header", "repeated_header"): {
            "base_url": "https://own.example",
            "auth": {
                "kind": "header",
                "header": "X-A",
                **ref,
                "more": [*headers[:1], {"header": "x-a", **ref}],
            },
        },
    }
    for (target_id, pointer, code), entry in refused.items():
        found = _refusals(lambda: declared(target_id, entry, tmp_path))  # noqa: B023
        assert {(at, said) for at, said, _ in found} == {(pointer, code)}, entry
        assert found == _refusals(lambda: filed(target_id, entry))  # noqa: B023


@pytest.mark.db
async def test_the_credential_file_is_owner_only(ctx: Context) -> None:
    target, created = await product.put(ctx, "own", _body(auth=BEARER, credential="s3cret-value"), TESTER)
    listed = product.path(ctx.settings)
    ref = json.loads(listed.read_text())["targets"]["own"]["auth"]["secret_ref"]
    assert re.fullmatch(r"file:secrets/own\.[0-9a-f]{16}", ref) and "s3cret-value" not in listed.read_text()
    credential = listed.parent / ref.removeprefix("file:")
    assert credential.read_text() == "s3cret-value" == read_secret(str(target.auth.secret_ref))
    modes = {file.name: stat.S_IMODE(file.stat().st_mode) for file in (listed.parent, credential.parent)}
    assert modes == {"targets": 0o700, "secrets": 0o700}
    assert [stat.S_IMODE(file.stat().st_mode) for file in (listed, credential)] == [0o600, 0o600]
    assert created and ctx.targets.get("own") == target and target.declared_in == "product"
    assert list(product.load(ctx.settings)) == ["filed", "own"]  # as another process reads it at its start


@pytest.mark.db
async def test_an_unreadable_credential_never_names_its_file(ctx: Context) -> None:
    """What a failed run shows and audits (``runner.cycle.failure``) is this refusal's text: of a credential
    the product stored, the target's id alone; of an operator's mounted secret, its file, as before."""
    stored = await _put(ctx, auth=BEARER, credential="one")
    Path(str(stored.auth.secret_ref).removeprefix("file:")).unlink()
    ctx.targets.put(
        Target("mounted", "https://m.example", auth=TargetAuth("bearer", None, "file:/nowhere/t"))
    )
    refusals = []
    for target_id in ("own", "mounted"):
        with pytest.raises(SecretError) as unread:
            connect(ctx, target_id, ExecutionConfig())
        refusals.append(str(unread.value))
    assert refusals == [
        "the stored credential of target own cannot be read: enter it again",
        "cannot read secret file /nowhere/t: No such file or directory",
    ]


@pytest.mark.db
async def test_a_changed_list_is_trusted_for_no_more_than_the_product_writes(
    ctx: Context, tmp_path: Path
) -> None:
    """The list is judged as what the product writes, whoever changed it (a restored backup, another
    container on the volume): an entry that is a database or files, another capability, or a credential
    reference that is not a file of its own target in the list's secrets folder refuses the next start and
    every change. So no secret of the installation is sent to a target's address, and none is unlinked."""
    stored = await _put(ctx, auth=BEARER, credential="one")
    listed, dsn = product.path(ctx.settings), tmp_path / "dsn"  # the settings' database DSN: one line
    own = json.loads(listed.read_text())["targets"]["own"]
    others = own["auth"]["secret_ref"].replace("/own.", "/other.")
    at, unwritten = "/targets/own/auth/secret_ref", "schema.additionalProperties"
    changed: list[tuple[dict[str, Any], set[tuple[str, str]]]] = [
        ({"own": own | {"auth": BEARER | {"secret_ref": f"file:{dsn}"}}}, {(at, "schema.pattern")}),
        ({"own": own | {"auth": BEARER | {"secret_ref": others}}}, {(at, "schema.pattern")}),
        ({"own": own, "own-db": {"dsn_ref": f"file:{dsn}"}}, {("/targets/own-db/dsn_ref", unwritten)}),
        (
            {"own": own, "exports": {"engine": "files", "root": "/var/exports"}},
            {("/targets/exports/engine", unwritten), ("/targets/exports/root", unwritten)},
        ),
        (
            {"own": own | {"capabilities": {"authoritative_absence": {"status": [404, 410]}}}},
            {("/targets/own/capabilities", "schema.const")},
        ),
    ]
    for entries, issues in changed:
        listed.write_text(json.dumps({"targets": entries}))
        refusals = []
        with pytest.raises(TargetsError) as refused:
            product.load(ctx.settings)  # the next start
        refusals.append(refused.value)
        for target_id in entries:
            with pytest.raises(TargetsError) as refused:
                await _put(ctx, target_id, auth=BEARER, credential="two")
            refusals.append(refused.value)
            with pytest.raises(TargetsError) as refused:
                await product.remove(ctx, target_id, TESTER)
            refusals.append(refused.value)
        assert [{(i.pointer, i.code) for i in refusal.issues} for refusal in refusals] == [issues] * len(
            refusals
        ), entries
        assert str(dsn) not in "".join(i.message for i in refusals[1].issues)  # an answer names no path
    assert dsn.exists() and _credentials(ctx) == {"one"} and ctx.targets.get("own") == stored
    assert await _audited(ctx) == [("target.set", "own", "replaced")]  # no change of these was begun


@pytest.mark.db
async def test_a_kept_and_a_replaced_credential(ctx: Context) -> None:
    first = await _put(ctx, auth=BEARER, credential="one")
    kept = await _put(ctx, base_url="HTTPS://Own.Example/api/", auth=BEARER, rate_per_s=5)  # the same address
    assert kept.auth.secret_ref == first.auth.secret_ref and _credentials(ctx) == {"one"}
    replaced = await _put(ctx, auth=BEARER, credential="two")
    assert replaced.auth.secret_ref != first.auth.secret_ref and _credentials(ctx) == {"two"}
    moved = [  # a stored credential never follows a target to another address, kind or header
        _body("https://other.example/api", auth=BEARER),
        _body(auth={"kind": "header", "header": "X-Api-Key"}),
    ]
    for body in moved:
        with pytest.raises(product.Refused) as needed:
            await product.put(ctx, "own", body, TESTER)
        assert (needed.value.status, str(needed.value)) == (422, product.REQUIRED)
    assert ctx.targets.get("own") == replaced and _credentials(ctx) == {"two"}
    assert (await _put(ctx)).auth == TargetAuth() and _credentials(ctx) == set()  # auth none deletes it
    assert await _audited(ctx) == [
        ("target.set", "own", said) for said in ("replaced", "kept", "replaced", "none")
    ]


@pytest.mark.db
async def test_a_further_header_keeps_or_needs_its_value(ctx: Context) -> None:
    async def refused(**members: Any) -> tuple[int, str]:
        with pytest.raises(product.Refused) as exc:
            await _put(ctx, **members)
        return exc.value.status, str(exc.value)

    assert await refused(auth=COOKIE, credential="sid=1") == (
        422,
        product.REQUIRED,
    )  # the further one's is missing
    first = await _put(ctx, auth=COOKIE, credential="sid=1", more_secrets={"X-CSRF-Token": "t1"})
    assert [name for name, _ in first.auth.more] == ["X-CSRF-Token"] and _credentials(ctx) == {"sid=1", "t1"}
    second = await _put(ctx, auth=COOKIE, more_secrets={"X-CSRF-Token": "t2"})  # one replaced, one kept
    assert second.auth.secret_ref == first.auth.secret_ref and second.auth.more != first.auth.more
    assert _credentials(ctx) == {"sid=1", "t2"}
    renamed = COOKIE | {"more": [{"header": "X-XSRF-Token"}]}
    added = COOKIE | {"more": [*COOKIE["more"], {"header": "X-Tenant"}]}
    assert await refused(auth=renamed) == await refused(auth=added) == (422, product.REQUIRED)
    assert await refused(auth=COOKIE, more_secrets={"X-Tenant": "acme"}) == (
        422,
        "more_secrets names a header that auth.more does not",
    )
    third = await _put(ctx, auth=added, more_secrets={"X-Tenant": "acme"})
    assert third.auth.more[0] == second.auth.more[0] and _credentials(ctx) == {"sid=1", "t2", "acme"}
    swapped = {"kind": "header", "header": "X-Tenant", "more": [{"header": "Cookie"}]}  # each by its name
    fourth = await _put(ctx, auth=swapped)
    assert (fourth.auth.secret_ref, fourth.auth.more[0][1]) == (third.auth.more[1][1], third.auth.secret_ref)
    assert _credentials(ctx) == {"sid=1", "acme"}  # the dropped header's file is gone
    four = COOKIE | {"more": [{"header": f"X-{n}"} for n in "abcd"]}
    with pytest.raises(TargetsError) as many:
        await _put(ctx, auth=four, credential="s", more_secrets={f"X-{n}": "v" for n in "abcd"})
    assert [(i.pointer, i.code) for i in many.value.issues] == [("/targets/own/auth/more", "schema.maxItems")]
    assert ctx.targets.get("own") == fourth and _credentials(ctx) == {"sid=1", "acme"}


@pytest.mark.db
async def test_a_failed_list_write_keeps_the_old_entry_and_credential(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await _put(ctx, auth=BEARER, credential="one")
    listed, write = product.path(ctx.settings), product.atomic_write
    before = listed.read_bytes()

    def full(path: Path, data: bytes) -> None:
        if path == listed:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(path))
        write(path, data)

    monkeypatch.setattr(product, "atomic_write", full)
    with pytest.raises(product.Refused) as failed:
        await _put(ctx, auth=BEARER, credential="two", rate_per_s=5)
    assert (failed.value.status, str(failed.value)) == (
        500,
        f"the target could not be written: {os.strerror(errno.ENOSPC)}",
    )
    assert listed.read_bytes() == before and _credentials(ctx) == {"one"} and ctx.targets.get("own") == old
    assert await _audited(ctx) == [("target.set", "own", "replaced")] * 2  # the row stays
    monkeypatch.setattr(product, "atomic_write", write)
    assert (await _put(ctx, auth=BEARER, rate_per_s=5)).rate_per_s == 5 and _credentials(ctx) == {"one"}


@pytest.mark.db
async def test_a_failure_after_the_switch_is_a_made_change(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The list's replace is the switch. A failure after it (its folder's sync) must not undo the
    credentials the list on disk now names, nor answer that nothing was written: the change is made, as the
    next start reads it, and a warning says what was not."""
    await _put(ctx, auth=BEARER, credential="one")
    listed, sync = product.path(ctx.settings), fsio.fsync_dir

    def failing(directory: Path) -> None:
        if directory == listed.parent:  # the list's folder: after the list was renamed into place
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(directory))
        sync(directory)

    monkeypatch.setattr(fsio, "fsync_dir", failing)
    replaced = await _put(ctx, auth=BEARER, credential="two", rate_per_s=5)
    monkeypatch.setattr(fsio, "fsync_dir", sync)
    ref = json.loads(listed.read_text())["targets"]["own"]["auth"]["secret_ref"]
    assert replaced.auth.secret_ref == f"file:{listed.parent / ref.removeprefix('file:')}"
    assert _credentials(ctx) == {"two"} == {read_secret(str(replaced.auth.secret_ref))}
    assert ctx.targets.get("own") == replaced == product.load(ctx.settings).get("own")  # as a restart reads
    await connect(ctx, "own", ExecutionConfig()).aclose()  # its credential is read
    warned = [r.getMessage() for r in caplog.records if r.name == "deltasleuth.targets"]
    assert warned == [
        "warning: the product's list of targets holds the change, but its write did not end cleanly"
        f" ({os.strerror(errno.EIO)})"
    ]
    assert await _audited(ctx) == [("target.set", "own", "replaced")] * 2


@pytest.mark.db
async def test_no_file_the_list_does_not_name_stays(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A replaced or a removed credential whose unlink failed is said, by count and never by name, and goes
    with the next change; what a killed change left in the secrets folder (a credential the list never came
    to name, an unfinished write) goes at the server's start. Nothing outside the folder is touched."""
    await _put(ctx, auth=BEARER, credential="one")
    folder, unlink = product.path(ctx.settings).parent / "secrets", Path.unlink
    names = [file.name for file in folder.iterdir()]

    def denied(self: Path, missing_ok: bool = False) -> None:
        if self.parent == folder:
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), str(self))
        unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "unlink", denied)
        await _put(ctx, auth=BEARER, credential="two")  # replaced: 200
        names += [file.name for file in folder.iterdir()]
        await product.remove(ctx, "own", TESTER)  # removed: 204
        assert _credentials(ctx) == {"one", "two"} and not product.path(ctx.settings).exists()
    left = "warning: {} file(s) no target names could not be removed from the product's secrets folder: the"
    assert [r.getMessage() for r in caplog.records if r.name == "deltasleuth.targets"] == [
        f"{left.format(n)} next change and the next start try again" for n in (1, 2)
    ]
    assert not any(name in caplog.text for name in names) and str(folder) not in caplog.text
    await _put(ctx, "other")  # the next change
    assert _credentials(ctx) == set()
    await _put(ctx, auth=BEARER, credential="three")
    (folder / "own.0123456789abcdef").write_text("never-listed")  # a change killed before its switch
    (folder / ".own.fedcba9876543210.k3j2x1.tmp").write_text("unfinished")
    beside = folder.parent / "kept"
    beside.write_text("not the product's")
    product.sweep(ctx.settings)  # the server's start
    assert _credentials(ctx) == {"three"} and beside.exists()
    assert sorted(product.load(ctx.settings)) == ["filed", "other", "own"]


LINK = "{} of the product's list is a symbolic link: remove the link"


async def _stopped(ctx: Context, *changes: Any) -> list[list[tuple[str, str, str]]]:
    """The issues of the ``TargetsError`` that the next start, the server's sweep and each of ``changes``
    are refused with, the start's without the file it names."""
    refusals = []
    for change in (lambda: product.load(ctx.settings), lambda: product.sweep(ctx.settings), *changes):
        with pytest.raises(TargetsError) as refused:
            if inspect.isawaitable(made := change()):
                await made
        issues = [(i.pointer, i.code, i.message) for i in refused.value.issues]
        if not refusals:  # the start's, as of every list that does not load
            named = f" (the product's list, {product.path(ctx.settings)})"
            assert all(said.endswith(named) for _, _, said in issues)
            issues = [(at, code, said.removesuffix(named)) for at, code, said in issues]
        refusals.append(issues)
    return refusals


@pytest.mark.db
async def test_a_linked_secrets_folder_is_neither_swept_nor_used(ctx: Context, tmp_path: Path) -> None:
    """Whoever can write the workspace can make the list's secrets folder, or the list's own folder, a
    symbolic link to another directory the service writes (its keys, its evidence). Nothing behind the
    link is the product's: the start and every change refuse it, with or without a list, and the sweep
    removes nothing there, called by itself either."""
    elsewhere = tmp_path / "elsewhere"  # as the installation's own secrets, or the evidence volume
    (elsewhere / "secrets" / "sub").mkdir(parents=True)
    kept = [elsewhere / "secrets" / name for name in ("kek", "tokeniser.key", "pack.zip")]
    for file in kept:
        file.write_text("the installation's own\n")
    listed = product.path(ctx.settings)
    assert not listed.parent.exists()  # no list yet: none is needed
    listed.parent.mkdir()
    links = ((listed.parent / "secrets", elsewhere / "secrets"), (listed.parent, elsewhere))
    for link, to in links:
        if link == listed.parent:
            link.rmdir()
        link.symlink_to(to, target_is_directory=True)
        product._swept(listed, {})  # the sweep alone, as a change's switch ends with
        refusals = await _stopped(
            ctx,
            lambda: _put(ctx, auth=BEARER, credential="one"),
            lambda: product.remove(ctx, "own", TESTER),
        )
        assert refusals == [[("/targets", "link", LINK.format("the folder or the secrets folder"))]] * 4
        assert all(file.read_text() == "the installation's own\n" for file in kept)
        assert len(list((elsewhere / "secrets").iterdir())) == len(kept) + 1  # nothing came or went
        link.unlink()
    assert await _audited(ctx) == [] and list(ctx.targets) == ["filed"]
    listed.parent.mkdir()
    assert (await _put(ctx, auth=BEARER, credential="one")).auth.kind == "bearer"  # the link removed


@pytest.mark.db
async def test_a_linked_credential_refuses_the_list_and_only_files_are_swept(
    ctx: Context, tmp_path: Path
) -> None:
    """A reference of exactly the product's form may be made a symbolic link to any file the service
    reads, whose content would then be sent to the entry's address as its credential. A list naming one
    stops the start and every change. The sweep unlinks regular files only: a link no entry names stays,
    and so does what it points to."""
    stored = await _put(ctx, auth=COOKIE, credential="sid=1", more_secrets={"X-CSRF-Token": "t1"})
    listed, dsn = product.path(ctx.settings), tmp_path / "dsn"  # the settings' database DSN: one line
    for ref in (stored.auth.secret_ref, stored.auth.more[0][1]):
        credential = Path(str(ref).removeprefix("file:"))
        value = credential.read_text()
        credential.unlink()
        credential.symlink_to(dsn)
        refusals = await _stopped(
            ctx,
            lambda: _put(ctx, auth=BEARER, credential="two"),
            lambda: _put(ctx, "other"),
            lambda: product.remove(ctx, "own", TESTER),
        )
        assert refusals == [[("/targets/own/auth", "link", LINK.format("a credential"))]] * 5
        credential.unlink()
        credential.write_text(value)
    assert product.load(ctx.settings).get("own") == stored  # the links removed: as it was
    folder = listed.parent / "secrets"
    stray, sub, unlisted = folder / "own.0123456789abcdef", folder / "kept", folder / "own.fedcba9876543210"
    stray.symlink_to(dsn)  # named by no entry
    sub.mkdir()
    unlisted.write_text("never-listed")
    product.sweep(ctx.settings)
    assert stray.is_symlink() and sub.is_dir() and dsn.exists() and not unlisted.exists()
    assert {file.read_text() for file in folder.iterdir() if file not in (stray, sub)} == {"sid=1", "t1"}
    assert await _audited(ctx) == [("target.set", "own", "replaced")]  # no change of these was begun


@pytest.mark.db
async def test_the_list_never_outgrows_its_reader(ctx: Context) -> None:
    """The list's reader takes a document of 4 MiB. The change that would take the list past it is refused
    with nothing written or audited, so the list still loads at the next start and still takes a change."""
    base = "https://own.example/" + "a" * 2028  # the longest base URL the schema takes

    def text(count: int) -> str:
        return json.dumps({"targets": {f"t{n:04d}": {"base_url": base} for n in range(count)}}, indent=2)

    each = len(text(2)) - len(text(1))
    held = 1 + (MAX_DOCUMENT_BYTES - len(text(1))) // each  # about 2,000 such targets
    listed = _listed(ctx.settings, text(held))
    assert listed.stat().st_size <= MAX_DOCUMENT_BYTES < listed.stat().st_size + each
    with pytest.raises(product.Refused) as full:
        await _put(ctx, "t9999", base_url=base)
    assert (full.value.status, str(full.value)) == (
        422,
        "the product's list of targets is full (4 MiB): remove one first",
    )
    assert listed.read_text() == text(held) and await _audited(ctx) == [] and "t9999" not in ctx.targets
    assert len(product.load(ctx.settings)) == held + 1  # the next start, with the targets file's one
    await product.remove(ctx, "t0000", TESTER)
    assert (await _put(ctx, "t9999", base_url=base)).base_url == base
    assert listed.stat().st_size <= MAX_DOCUMENT_BYTES and len(product.load(ctx.settings)) == held + 1


@pytest.mark.db
@pytest.mark.parametrize("host", ["localhost", "app.localhost", "127.0.0.1", "[::1]"])
async def test_localhost_in_a_container_says_what_to_type(
    ctx: Context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    here = {"base_url": f"http://{host}:8080/api", "allow_private_network": True}
    monkeypatch.setattr(product, "_CONTAINER", ())
    assert (await _put(ctx, "here", **here)).base_url == here["base_url"]  # no container: this machine
    (tmp_path / ".dockerenv").touch()
    monkeypatch.setattr(product, "_CONTAINER", (str(tmp_path / ".dockerenv"),))
    with pytest.raises(TargetsError) as refused:
        await _put(ctx, "here", **here)
    assert [(i.pointer, i.code, i.message) for i in refused.value.issues] == [
        ("/targets/here/base_url", "container_localhost", product.LOCALHOST)
    ]
    with pytest.raises(TargetsError) as both:  # beside the file's own issues
        await _put(ctx, "here", base_url=here["base_url"])
    assert [i.code for i in both.value.issues] == ["private_network", "container_localhost"]
    named = await _put(
        ctx, "here", base_url="http://host.docker.internal:8080/api", allow_private_network=True
    )
    assert named.base_url == "http://host.docker.internal:8080/api"
    assert product.load(ctx.settings).get("here") == named  # a start never refuses what a PUT wrote


@pytest.mark.db
async def test_the_last_entry_removes_the_list(ctx: Context) -> None:
    await _put(ctx, "one", auth=BEARER, credential="s1")
    await _put(ctx, "two")
    listed = product.path(ctx.settings)
    await product.remove(ctx, "one", TESTER)
    assert list(json.loads(listed.read_text())["targets"]) == ["two"] and _credentials(ctx) == set()
    await product.remove(ctx, "two", TESTER)
    assert not listed.exists() and list(ctx.targets) == ["filed"] == list(product.load(ctx.settings))
    refusals = []
    for target_id in ("two", "filed"):
        with pytest.raises(product.Refused) as refused:
            await product.remove(ctx, target_id, TESTER)
        refusals.append((refused.value.status, str(refused.value)))
    assert refusals == [
        (404, "target two not found"),
        (409, "target filed is declared in the targets file: change it there"),
    ]
    assert [row[:2] for row in await _audited(ctx)] == [
        ("target.set", "one"),
        ("target.set", "two"),
        ("target.delete", "one"),
        ("target.delete", "two"),
    ]


@pytest.mark.db
async def test_a_change_keeps_one_budget(ctx: Context) -> None:
    await _put(ctx, rate_per_s=10, max_concurrency=2)
    reader = connect(ctx, "own", ExecutionConfig())  # the target's one gate, as a run's reader opens it
    try:
        gate = ctx.gates["own"]
        limiter = gate.limiter
        await _put(ctx, rate_per_s=10, max_concurrency=2, auth=BEARER, credential="s")  # the budget as it was
        assert ctx.gates["own"] is gate and gate.limiter is limiter
        await _put(ctx, rate_per_s=50, max_concurrency=8, auth=BEARER)
        assert ctx.gates["own"] is gate and gate.limiter is not limiter  # never popped: rebudgeted
        assert (gate.limiter.time_period, gate.max_concurrency, gate.semaphore._value) == (0.02, 8, 8)
    finally:
        await reader.aclose()
    await product.remove(ctx, "own", TESTER)
    assert "own" not in ctx.gates and "own" not in ctx.targets
