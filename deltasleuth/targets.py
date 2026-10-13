"""Targets declared in the product (docs/mvp/DEPLOY.md §7, "In the product"): services a declarer adds
through the API, beside the targets file an operator writes.

They are a second targets document, ``<workspace_dir>/targets/targets.json``, which only the server
writes: each entry is the targets file's own form, judged by its schema and address rules
(``core.spec.targets.declared``), and each credential is one line, in clear, in a file of its own under
``secrets/`` beside the list (directories 0700, files 0600), named by the entry's ``secret_ref``. Every
entry point reads both lists at its start (``load``); for an id in both the targets file wins. An invalid
list stops the start, and so does one holding more than the product writes (``_stored``): it is trusted
for a service's members, the one capability and its own credential files, whoever changed it. A
symbolic link is none of its own: the list is refused while its folder, its ``secrets`` folder or a
credential it names is one (``_entries``), and nothing is read or removed through one.

A change (``put``, ``remove``) is one at a time. It is validated, audited in a transaction committed
first (with the count of unfinished runs that refuses it), then written: the credentials under fresh
names, the list in one atomic replace (the switch), then every regular file of ``secrets`` the list no
longer names unlinked (``_swept``; at the server's start too, for what a killed change left). The
process's registry and the target's one gate follow, so the change holds without a restart: for every
connector opened afterwards, and an open one's budget at once. No change is made while a run is being
written (``unmoved``), and a run is refused when a target it read was moved meanwhile. A refusal carries
its HTTP status and binding message (``Refused``). No credential, reference or path of one is in an
answer, an issue, a log line or an audit detail.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from jsonschema import Draft202012Validator

from deltasleuth.auth import oidc, sessions
from deltasleuth.auth.principal import Principal
from deltasleuth.config import AuthSettings, Settings
from deltasleuth.core.secrets import MAX_SECRET_BYTES
from deltasleuth.core.spec.documents import _CONTAINER, MAX_DOCUMENT_BYTES, read_document
from deltasleuth.core.spec.errors import SpecError, SpecIssue, TargetsError, pointer
from deltasleuth.core.spec.rules import _key_sources
from deltasleuth.core.spec.schemas import schema_issues
from deltasleuth.core.spec.targets import TargetRegistry, declared, load_targets
from deltasleuth.core.types import AuthoritativeAbsence, CompiledSpec, Target
from deltasleuth.evidence.fsio import atomic_write, ensure_private_dir
from deltasleuth.store import repo

if TYPE_CHECKING:
    from deltasleuth.context import Context

_SECRETS = "secrets"  # beside the list
_MEMBERS = ("base_url", "allow_private_network", "allow_insecure_http", "rate_per_s", "max_concurrency")
_ABSENT_ON_404 = {"authoritative_absence": {"status": [404]}}  # the one capability, in the file's form
IN_FILE = "target {} is declared in the targets file: change it there"
IN_USE = "target {} is read by {} unfinished run(s): stop them first"
MOVED = "target {} was changed or removed while the run was being created: start it again"
FULL = f"the product's list of targets is full ({MAX_DOCUMENT_BYTES // 2**20} MiB): remove one first"
REQUIRED = "secret is required: none is stored for this auth, or the base URL or the auth changed"
UNSTORABLE = (
    "secret is refused: it is empty, over 65536 bytes, or holds a BOM, surrounding whitespace, a line break"
    " or a control character"
)
OWN = (
    "secret is refused: it holds DeltaSleuth's own cookie {}, which signs in here and which a browser sends"
    " to every port of this host: leave that cookie out"
)
LOCALHOST = (
    "localhost is DeltaSleuth's own container here, not this machine: write the host as"
    " host.docker.internal, keep the port, and set allow_private_network"
)
UNSYNCED = "warning: the product's list of targets holds the change, but its write did not end cleanly (%s)"
LEFT = (
    "warning: %d file(s) no target names could not be removed from the product's secrets folder: the next"
    " change and the next start try again"
)
LINK = "{} of the product's list is a symbolic link: remove the link"
log = logging.getLogger(__name__)


class _Turns:
    """Whose turn it is on an event loop (a process serves from one): one change of the list at a time
    (``lock``), and none while a run is being written (``idle``: none is)."""

    def __init__(self) -> None:
        self.lock, self.idle, self.written = asyncio.Lock(), asyncio.Event(), 0
        self.idle.set()


_TURNS: WeakKeyDictionary[asyncio.AbstractEventLoop, _Turns] = WeakKeyDictionary()


class Refused(ValueError):
    """A change of the product's targets refused: its HTTP status and binding message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def path(settings: Settings) -> Path:
    """The product's list."""
    return settings.workspace_dir / "targets" / "targets.json"


def load(settings: Settings) -> TargetRegistry:
    """Both lists: the targets file's registry with the product's targets added. For an id in both the
    targets file wins, and a warning says so. ``TargetsError`` for either: an invalid list stops every
    entry point, and the product's is named in each of its issues."""
    registry, source = load_targets(settings.targets_file), path(settings)
    try:
        for target_id, entry in _entries(source).items():
            if target_id in registry:
                ignored = "warning: target %s of the product's list is ignored: the targets file declares it"
                log.warning(ignored, target_id)
            else:
                registry.put(declared(target_id, entry, source.parent))
    except TargetsError as exc:
        where = f" (the product's list, {source})"
        raise TargetsError(replace(issue, message=issue.message + where) for issue in exc.issues) from None
    return registry


def item(target: Target, declares: bool) -> dict[str, Any]:
    """A target as ``GET /v1/targets`` shows it: its id, declared capabilities, engine (null for a service)
    and where it is declared; to who ``declares``, also what a target of the product's list was declared
    with and whether a credential is stored."""
    shown: dict[str, Any] = {
        "target_id": target.target_id,
        "capabilities": asdict(target.capabilities),
        "engine": target.engine,
        "declared_in": target.declared_in,
    }
    if declares and target.declared_in == "product":
        shown |= _declared(target) | {"has_secret": target.auth.secret_ref is not None}
    return shown


async def put(ctx: Context, target_id: str, body: Mapping[str, Any], who: Principal) -> tuple[Target, bool]:
    """Declare the service ``target_id`` in the product, or change it, as ``who``: the target, and whether
    it is new. ``body`` holds the targets file's members of a service, without references: ``secret`` is
    the credential of the bearer or of ``auth.header``, ``more_secrets`` those of ``auth.more`` by header,
    and ``absent_on_404`` the one capability. A credential not sent is kept while the base URL, the kind
    and its header stay; a stored one never follows a target to another address. A credential that holds
    this installation's own session or sign-in cookie (``_own``), and a list that would pass what its reader
    takes, are refused before anything is written."""
    async with _changing():
        was = ctx.targets.get(target_id) if target_id in ctx.targets else None
        if was is not None and was.declared_in == "file":
            raise Refused(409, IN_FILE.format(target_id))
        source = path(ctx.settings)
        entries = _entries(source)
        old = _refs(entries.get(target_id, {}))
        auth = body.get("auth") or {"kind": "none"}
        further = [more["header"] for more in auth.get("more") or ()]
        names = [*([] if auth["kind"] == "none" else [_first(auth)]), *further]
        given: dict[str | None, str] = dict(body.get("more_secrets") or {})
        if body.get("secret") is not None:
            given[_first(auth)] = body["secret"]
        if given and auth["kind"] == "none":
            raise Refused(422, "auth none takes no secret")
        if set(body.get("more_secrets") or ()) - set(further):
            raise Refused(422, "more_secrets names a header that auth.more does not")
        if any(_unstorable(value) for value in given.values()):
            raise Refused(422, UNSTORABLE)
        if own := sorted(name for value in given.values() for name in _own(value, ctx.settings.auth)):
            raise Refused(422, OWN.format(own[0]))
        fresh = {name: f"file:{_SECRETS}/{target_id}.{secrets.token_hex(8)}" for name in names}
        issues: list[SpecIssue] = []
        try:
            target = declared(target_id, _entry(body, fresh), source.parent)
        except TargetsError as exc:
            issues += exc.issues
        if _in_container() and _this_machine(body["base_url"]):
            at = pointer(("targets", target_id, "base_url"))
            issues.append(SpecIssue(at, LOCALHOST, "container_localhost"))
        if issues:
            raise TargetsError(issues)
        same = was is not None and (was.base_url, was.auth.kind) == (target.base_url, target.auth.kind)
        kept = {name: old[name] for name in names if same and name in old and name not in given}
        if set(names) - set(given) - set(kept):
            raise Refused(422, REQUIRED)
        entry = _entry(body, fresh | kept)
        target = declared(target_id, entry, source.parent)
        listed = entries | {target_id: entry}
        if len(_text(listed)) > MAX_DOCUMENT_BYTES:  # what the list's reader takes: it must load again
            raise Refused(422, FULL)
        moved = was is not None and _shape(was) != _shape(target)
        detail = _declared(target) | {"created": was is None}
        detail["secret"] = "none" if not names else "replaced" if given else "kept"
        async with repo.audited(ctx.pool) as (conn, audit):
            if moved and (reading := await repo.runs_reading(conn, target_id)):
                raise Refused(409, IN_USE.format(target_id, reading))
            await audit(who, "target.set", target_id, detail)
        await _written(source, listed, {fresh[name]: value for name, value in given.items()})
        ctx.targets.put(target)
        budget = (target.rate_per_s, target.max_concurrency)
        if was is not None and target_id in ctx.gates and (was.rate_per_s, was.max_concurrency) != budget:
            ctx.gates[target_id].rebudget(*budget)
        return target, was is None


async def remove(ctx: Context, target_id: str, who: Principal) -> None:
    """Remove the product's entry ``target_id`` and its credentials, as ``who``. Unless the targets file
    declares the id (the entry was ignored), no unfinished run may read it, and the registry's entry and
    the gate go too."""
    async with _changing():
        source = path(ctx.settings)
        entries = _entries(source)
        if target_id not in entries:
            if target_id in ctx.targets:
                raise Refused(409, IN_FILE.format(target_id))
            raise Refused(404, f"target {target_id} not found")
        ours = target_id not in ctx.targets or ctx.targets.get(target_id).declared_in == "product"
        async with repo.audited(ctx.pool) as (conn, audit):
            if ours and (reading := await repo.runs_reading(conn, target_id)):
                raise Refused(409, IN_USE.format(target_id, reading))
            await audit(who, "target.delete", target_id)
        del entries[target_id]
        await _written(source, entries, {})
        if ours:
            ctx.targets.drop(target_id)
            ctx.gates.pop(target_id, None)


def reading(ctx: Context, spec: CompiledSpec) -> dict[str, Target]:
    """The targets ``spec`` names, each side's and each key source's (as ``repo.runs_reading`` counts them),
    as they stand: what ``unmoved`` holds a run's creation to."""
    ids = {cfg.target for cfg in spec.sides.values()}
    ids |= {source["target"] for _, source in _key_sources(spec.raw) if isinstance(source.get("target"), str)}
    return {target_id: ctx.targets.get(target_id) for target_id in sorted(ids)}


@asynccontextmanager
async def unmoved(ctx: Context, read: Mapping[str, Target]) -> AsyncIterator[None]:
    """While a run that read ``read`` (``reading``) is written, no target changes: a change waits for it,
    and then counts it. The 409 of a target in use counts runs, and a run is there only once its keys were
    listed, which may take its whole window: one of ``read`` removed or moved since (as that 409 judges a
    move: its credentials and its budget may change) is a ValueError, so no run compares another address
    than its spec was judged against. Runs are written side by side, as before."""
    turns = _turns()
    async with turns.lock:  # after a change under way
        for target_id, was in read.items():
            if target_id not in ctx.targets or _shape(ctx.targets.get(target_id)) != _shape(was):
                raise ValueError(MOVED.format(target_id))
        turns.written += 1
        turns.idle.clear()
    try:
        yield
    finally:
        turns.written -= 1
        if not turns.written:
            turns.idle.set()


def sweep(settings: Settings) -> None:
    """At the server's start, before it serves: unlink what a change that was killed, or an unlink that
    failed, left in the list's secrets folder (``_swept``). The process that writes the list sweeps, no
    other: a credential is written before the list names it."""
    source = path(settings)
    _swept(source, _entries(source))


def _turns() -> _Turns:
    return _TURNS.setdefault(asyncio.get_running_loop(), _Turns())


@asynccontextmanager
async def _changing() -> AsyncIterator[None]:
    """One change at a time, once the runs being written are (``unmoved``): none begins to be written
    until the change is made, so its count of the unfinished runs misses none."""
    turns = _turns()
    async with turns.lock:
        await turns.idle.wait()
        yield


def _entries(source: Path) -> dict[str, Any]:
    """The product's list as stored, id -> entry; empty without the file. ``TargetsError`` for one that
    cannot be read, or that holds an entry ``put`` could not have written (``_stored``), and, list or no
    list, while its folder, its secrets folder or a credential it names is a symbolic link: what lies
    behind a link is not the product's to send to a target's address, nor to sweep."""
    if _linked(source):
        raise TargetsError([SpecIssue("/targets", LINK.format("the folder or the secrets folder"), "link")])
    if not source.exists():
        return {}
    try:
        entries = read_document(source).get("targets")
    except SpecError as exc:
        raise TargetsError(exc.issues) from None
    if not isinstance(entries, dict):
        raise TargetsError([SpecIssue("/targets", "expected a mapping of target ids", "schema.type")])
    issues = [i for t, entry in entries.items() for i in schema_issues(_stored(t), entry, ("targets", t))]
    if issues:
        raise TargetsError(issues)
    issues = [
        SpecIssue(pointer(("targets", t, "auth")), LINK.format("a credential"), "link")
        for t, entry in entries.items()
        if any((source.parent / ref.removeprefix("file:")).is_symlink() for ref in _refs(entry).values())
    ]
    if issues:
        raise TargetsError(issues)
    return entries


def _linked(source: Path) -> bool:
    """Whether the list's folder or its secrets folder is a symbolic link."""
    return source.parent.is_symlink() or (source.parent / _SECRETS).is_symlink()


def _stored(target_id: str) -> Draft202012Validator:
    """An entry of ``target_id`` as ``put`` writes one, and no more: a service's members, the one
    capability, and each credential by a reference to a file of this id in the list's own ``secrets``
    folder. The list is trusted for nothing else, whoever changed it: it makes no database and no other
    capability, and no file but a credential of its own is sent to a target's address or unlinked."""
    ref = {
        "description": "A credential of the product's list is a file of its target in the list's secrets.",
        "type": "string",
        "pattern": rf"^file:{_SECRETS}/{re.escape(target_id)}\.[0-9a-f]{{16}}\Z",
    }
    held = {"header": {"type": "string"}, "secret_ref": ref}
    more = {"type": "array", "items": {"type": "object", "required": [*held], "properties": held}}
    members: dict[str, Any] = dict.fromkeys(_MEMBERS, True)
    members |= {"auth": {"type": "object", "properties": held | {"more": more}}}
    members |= {"capabilities": {"const": _ABSENT_ON_404}}
    return Draft202012Validator({"type": "object", "additionalProperties": False, "properties": members})


def _refs(entry: Mapping[str, Any]) -> dict[str | None, str]:
    """A stored entry's credential references by header (None: a bearer's)."""
    auth = entry.get("auth", {})
    refs = {auth.get("header"): auth["secret_ref"]} if "secret_ref" in auth else {}
    return refs | {more["header"]: more["secret_ref"] for more in auth.get("more", ())}


def _first(auth: Mapping[str, Any]) -> str | None:
    """The header of the auth's first credential; None for a bearer's."""
    return auth.get("header") if auth["kind"] == "header" else None


def _entry(body: Mapping[str, Any], refs: Mapping[str | None, str]) -> dict[str, Any]:
    """The list's entry of ``body``: the targets file's form, each credential by its reference."""
    entry = {name: body[name] for name in _MEMBERS if body.get(name) is not None}
    if (auth := body.get("auth")) is not None:
        stored = {name: value for name, value in auth.items() if name != "more"}
        if auth["kind"] != "none":
            stored["secret_ref"] = refs[_first(auth)]
        if auth.get("more"):
            stored["more"] = [{"header": m["header"], "secret_ref": refs[m["header"]]} for m in auth["more"]]
        entry["auth"] = stored
    if body.get("absent_on_404"):
        entry["capabilities"] = _ABSENT_ON_404
    return entry


def _declared(target: Target) -> dict[str, Any]:
    """What a target was declared with, as its item and its ``target.set`` row say it: header names, never
    a credential or its reference."""
    auth, absence = target.auth, target.capabilities.authoritative_absence
    return {
        "base_url": target.base_url,
        "allow_private_network": target.allow_private_network,
        "allow_insecure_http": target.allow_insecure_http,
        "auth": {"kind": auth.kind, "header": auth.header, "more": [name for name, _ in auth.more]},
        "rate_per_s": target.rate_per_s,
        "max_concurrency": target.max_concurrency,
        "absent_on_404": absence == AuthoritativeAbsence((404,)),
    }


def _shape(target: Target) -> Target:
    """The target without what a change may touch while runs read it: its credentials and its budget."""
    auth = replace(target.auth, secret_ref=None, more=tuple((name, "") for name, _ in target.auth.more))
    return replace(target, auth=auth, rate_per_s=0.0, max_concurrency=0)


def _unstorable(secret: str) -> bool:
    """Whether ``read_secret`` or an HTTP header would refuse the credential."""
    try:
        size = len(secret.encode())
    except UnicodeEncodeError:  # a lone surrogate
        return True
    control = any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in secret)
    return (
        not secret
        or size > MAX_SECRET_BYTES
        or secret.startswith("\ufeff")
        or secret != secret.strip()
        or control
    )


def _own(secret: str, auth: AuthSettings | None) -> set[str]:
    """The names of this installation's session and sign-in cookies that ``secret`` holds a pair of, as a
    ``Cookie`` value copied from a browser does for an application on this host: a browser keeps cookies by
    host, not by port. Stored, the cookie would lie in clear in the workspace and be sent to the target."""
    names = {sessions.named(name, auth) for name in (sessions.COOKIE, oidc.LOGIN_COOKIE)}
    return names & {pair.partition("=")[0].strip() for pair in secret.split(";")}


def _in_container() -> bool:
    return any(os.path.exists(mark) for mark in _CONTAINER)


def _this_machine(base_url: str) -> bool:
    """Whether ``base_url`` names its host as localhost or a loopback literal: in a container, the
    container itself."""
    try:
        host = urlsplit(base_url).hostname or ""
        return host == "localhost" or host.endswith(".localhost") or ipaddress.ip_address(host).is_loopback
    except ValueError:  # no URL, or a name
        return False


async def _written(source: Path, entries: Mapping[str, Any], values: Mapping[str, str]) -> None:
    """``_switch`` off the event loop; 500 with the OS error's text (never a path) when it fails, the audit
    row committed."""
    try:
        await asyncio.to_thread(_switch, source, entries, values)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise Refused(500, f"the target could not be written: {reason}") from None


def _text(entries: Mapping[str, Any]) -> bytes:
    """The list as it is written."""
    return json.dumps({"targets": entries}, indent=2).encode()


def _switch(source: Path, entries: Mapping[str, Any], values: Mapping[str, str]) -> None:
    """Write the credentials ``values`` (reference -> credential), then the list in one atomic replace
    (removed with its last entry), then sweep its secrets folder (``_swept``): the replaced and the removed
    credentials go. A failure before the list is replaced leaves it and the credentials it names as they
    were, and removes what was written. One after it (the sync of its folder) undoes nothing, since the
    list on disk names the new credentials: the change is made, and a warning gives the error."""
    text, written = (_text(entries) if entries else None), []
    try:
        ensure_private_dir(source.parent)
        ensure_private_dir(source.parent / _SECRETS)
        for ref, value in values.items():
            written.append(source.parent / ref.removeprefix("file:"))
            atomic_write(written[-1], value.encode())
        if text is None:
            source.unlink(missing_ok=True)
        else:
            atomic_write(source, text)
    except BaseException as exc:
        if not _stands(source, text):
            _unlink(written)
            raise
        if not isinstance(exc, OSError):
            raise  # both lists' credentials stay: the next change or start sweeps
        log.warning(UNSYNCED, exc.strerror or type(exc).__name__)
    _swept(source, entries)


def _stands(source: Path, text: bytes | None) -> bool:
    """Whether the list on disk is ``text`` (None: no list)."""
    try:
        return source.read_bytes() == text
    except FileNotFoundError:
        return text is None
    except OSError:
        return False


def _swept(source: Path, entries: Mapping[str, Any]) -> None:
    """Unlink every regular file of the list's secrets folder that no entry of ``entries`` names: a
    replaced or removed credential, and what a change that failed or was killed left there (a credential,
    an unfinished write). Nothing outside that folder is touched: a folder that is a symbolic link is not
    listed, and a link, a folder or anything else that is no regular file stays. One that cannot be
    unlinked is warned of by count, never by name, and tried again at the next change and at the server's
    next start."""
    named = {ref.rpartition("/")[2] for entry in entries.values() for ref in _refs(entry).values()}
    if _linked(source):  # ``_entries`` refuses such a list: this is for a link made since
        return
    try:
        folder = (source.parent / _SECRETS).iterdir()
        files = [file for file in folder if file.is_file(follow_symlinks=False)]
    except OSError:  # no folder: no credential was stored yet (one that cannot be listed cannot be read)
        return
    left = 0
    for file in files:
        if file.name not in named:
            try:
                file.unlink(missing_ok=True)
            except OSError:
                left += 1
    if left:
        log.warning(LEFT, left)


def _unlink(files: Iterable[Path]) -> None:
    for file in files:
        with contextlib.suppress(OSError):  # what stays is swept
            file.unlink(missing_ok=True)
