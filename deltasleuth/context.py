"""The long-lived objects every entry point shares: db pool, targets (the targets file's and those
declared in the product, ``deltasleuth.targets``), tokeniser, keyring, evidence, LLM profiles, and one
politeness gate per target for the whole process.

The store is async (psycopg ``AsyncConnectionPool``) throughout this release: the API and the runner
share one event loop. The keyring and the tokeniser key are ``file:`` secrets named by
``encryption`` (docs/mvp/RETENTION.md §1); without it (developer mode) they are the workspace's
``kek`` and ``tokeniser.key``, created only by ``init_workspace`` (``ds init``): a new tokeniser key
would give every value a new token, and no run could be compared with an older one. The tokeniser
key also derives the evidence store's observation-id key and the sign-in cookie's key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from deltasleuth import targets as product
from deltasleuth.auth.oidc import Oidc
from deltasleuth.config import KEY_FILE, Settings
from deltasleuth.connectors.http import TargetGate
from deltasleuth.core.secrets import read_secret
from deltasleuth.core.spec import TargetRegistry, compile_spec
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    CompiledSpec,
    TargetCapabilities,
    TruncationIndicators,
)
from deltasleuth.evidence.fsio import ensure_private_dir
from deltasleuth.evidence.store import EvidenceStore
from deltasleuth.llm.profiles import ProfileRegistry, load_profiles
from deltasleuth.security import keys
from deltasleuth.security.crypto import KEY_BYTES, Keyring, KeyringError, Sealer, create_keyring
from deltasleuth.security.signing import PackSigner
from deltasleuth.security.tokenise import Tokeniser, load_or_create_key, read_key
from deltasleuth.store import repo
from deltasleuth.store.db import SEALED, WAIT, admitted, migrate, opening

KEK_FILE = "kek"  # developer mode's keyring, in the workspace
_EVIDENCE_ID_LABEL = b"deltasleuth/obs_id/v1"
_LOGIN_LABEL = b"deltasleuth/login/v1"
Capabilities = Mapping[str, TargetCapabilities]  # by target id


class ContextError(RuntimeError):
    """The workspace is not initialised or a configured resource does not fit."""


def init_workspace(settings: Settings) -> tuple[Path, ...]:
    """Create the workspace (0700) and, without ``encryption``, its tokeniser key and keyring (0600)
    unless present; return the key files created or kept (none with ``encryption``: its secrets are
    never created here)."""
    ensure_private_dir(settings.workspace_dir)
    if settings.encryption is not None:
        return ()
    key, kek = settings.workspace_dir / KEY_FILE, settings.workspace_dir / KEK_FILE
    load_or_create_key(key)
    create_keyring(kek)
    read_keys(settings)
    return key, kek


def read_keys(settings: Settings) -> tuple[Keyring, bytes]:
    """The keyring and the tokeniser key; an error stops every entry point."""
    if (encryption := settings.encryption) is not None:
        return Keyring.from_file(encryption.kek_ref), read_key(encryption.tokeniser_key_ref)
    key, kek = settings.workspace_dir / KEY_FILE, settings.workspace_dir / KEK_FILE
    for path, name in ((key, "tokeniser key"), (kek, "keyring")):
        if not path.exists():
            raise ContextError(f"{name} {path} not found; run 'ds init' first")
    if kek.stat().st_mode & 0o077:
        raise KeyringError(f"keyring {kek} must not be accessible by group or others (chmod 600)")
    return Keyring.from_file(f"file:{kek}"), load_or_create_key(key)


def evidence_id_key(tokeniser_key: bytes) -> bytes:
    """A key derived from the tokeniser key, so an obs_id can never be compared with a token."""
    return hmac.new(tokeniser_key, _EVIDENCE_ID_LABEL, hashlib.sha256).digest()


def login_key(tokeniser_key: bytes) -> bytes:
    """The key of the signed login cookie, ``__Host-ds_login`` or on a plain-http loopback ``public_url``
    ``ds_login_<port>`` (docs/mvp/AUTH.md §2)."""
    return hmac.new(tokeniser_key, _LOGIN_LABEL, hashlib.sha256).digest()


def _capabilities(declared: dict[str, Any]) -> TargetCapabilities:
    absence, truncation = declared["authoritative_absence"], declared["truncation_indicators"]
    if absence is not None:
        absence = AuthoritativeAbsence(tuple(absence["status"]), tuple(absence["shapes"]))
    if truncation is not None:
        truncation = TruncationIndicators(**truncation)
    return TargetCapabilities(
        **declared | {"authoritative_absence": absence, "truncation_indicators": truncation}
    )


@dataclass(frozen=True)
class Context:
    settings: Settings
    pool: AsyncConnectionPool
    targets: TargetRegistry
    tokeniser: Tokeniser
    evidence: EvidenceStore
    llm: ProfileRegistry | None  # None: AI off
    oidc: Oidc | None  # oidc mode's IdP client, which only the server builds (docs/mvp/AUTH.md §2)
    keyring: Keyring
    missing_keks: Mapping[str, int] = field(default_factory=dict)  # KEK id -> data keys it wraps
    kek: str | None = None  # the KEK new data keys are wrapped under (None: the current one), §6.1
    signer: PackSigner | None = None  # the evidence packs' signing key, which only the server loads
    # target id -> its gate, shared by the worker, compares and key listings: one rate budget
    gates: dict[str, TargetGate] = field(default_factory=dict)
    _sealers: dict[str, Sealer] = field(default_factory=dict, repr=False)  # run id -> its sealer

    def new_sealer(self, run_id: str) -> tuple[str, bytes, Sealer]:
        """A new data key for the run: (its KEK id, the key wrapped by it, its sealer)."""
        key = os.urandom(KEY_BYTES)
        kek, wrapped = self.keyring.wrap(key, self.kek)
        return kek, wrapped, Sealer(key, run_id)

    async def sealer(self, conn: repo.Conn, run_id: str) -> Sealer:
        """The run's sealer, cached; KeyringError while the keyring lacks the KEK wrapping its data key."""
        sealer = self._sealers.get(run_id)
        if sealer is None:
            row = await repo.data_key(conn, run_id)
            if row is None:
                raise LookupError(f"run {run_id} has no data key")
            key = self.keyring.unwrap(row["kek"], row["wrapped"])
            sealer = self._sealers[run_id] = Sealer(key, run_id)
        return sealer

    def forget(self, run_id: str) -> None:
        """Drop the run's cached sealer: its data key was destroyed (docs/mvp/RETENTION.md §5.3)."""
        self._sealers.pop(run_id, None)

    async def spec(self, conn: repo.Conn, spec_id: int, *, live: bool = False) -> CompiledSpec:
        """A stored spec, compiled. ``live`` also checks it against today's targets, as calling them
        needs; reading a finished run must not, so removing a target never hides its history."""
        row = await repo.get_spec(conn, spec_id)
        if row is None:
            raise LookupError(f"spec {spec_id} not found")
        return compile_spec(row["body"], targets=self.targets if live else None)

    def declared(self, spec: CompiledSpec) -> dict[str, Any]:
        """What the spec's targets declare today, kept as a run's ``params["capabilities"]``."""
        return {s.target: asdict(self.targets.get(s.target).capabilities) for s in spec.sides.values()}

    def capabilities(self, run: Mapping[str, Any]) -> Capabilities:
        """The capabilities a run is observed with: those declared when it was created (a target
        changed or removed since never changes how the run reads), else today's."""
        declared = run["params"].get("capabilities")
        if declared is None:
            return {t: self.targets.get(t).capabilities for t in self.targets}
        return {t: _capabilities(d) for t, d in declared.items()}

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        settings: Settings,
        *,
        oidc: bool = False,
        rewrap: bool = False,
        migrating: bool = False,
        packs: bool = False,
        pool: tuple[int, int] | None = None,
    ) -> AsyncIterator[Context]:
        """Load everything, open the pool, apply pending migrations and load the data keys; close the
        pool on exit. A files target's root that lies where it may not is refused (docs/mvp/FILES.md §4,
        T10), and the spill directories of processes that ended are swept (§10).

        ``oidc`` (the server only) builds the IdP client in oidc mode: it reads the client credential
        and fetches the IdP's discovery document. Other entry points never contact the IdP. ``rewrap``
        (the server only) moves the data keys onto the current KEK (docs/mvp/RETENTION.md §6.1), and
        ``migrating`` (``ds keys migrate``) stops before 007, which refuses legacy plaintext, and
        accepts a leftover ``blobs/sha256/``. ``packs`` (the server) loads the evidence packs' signing key
        when they are configured (docs/mvp/PACK.md §3.1). ``pool`` (``ds mcp``) is the pool's least and
        most connections (docs/mvp/MCP.md §5.2); None: the process's own size, with ``mcp.concurrency``
        more where MCP is served over HTTP (§4).
        """
        from deltasleuth.connectors.tables import duck, manifest

        keyring, key = read_keys(settings)
        signer = PackSigner.load(settings.packs.signing_key_ref) if packs and settings.packs else None
        llm = None
        if (llm_file := settings.llm_profiles_file) is not None:
            llm = load_profiles(llm_file)
            if settings.llm_profile not in llm.profiles:
                raise ContextError(f"llm_profile {settings.llm_profile!r} is not a profile of {llm_file}")
        targets = product.load(settings)
        if refused := await manifest.opened(settings, targets):  # T10; and the spill swept (FILES.md §10)
            raise ContextError("; ".join(refused))
        duck.configure(settings, targets)  # what the process's file reader is built of, at its first use
        auth = settings.auth
        client = Oidc(auth.oidc, auth.public_url, login_key(key)) if oidc and auth and auth.oidc else None
        # One connection per worker slot, plus a few for the API, two for the notifier and one per MCP call.
        size = settings.worker.concurrency + 4 + (2 if settings.notify is not None else 0)
        size += settings.mcp.concurrency if settings.mcp is not None and settings.mcp.http else 0
        sizes = {"max_size": size} if pool is None else {"min_size": pool[0], "max_size": pool[1]}
        dsn = read_secret(settings.database_dsn_ref)
        await admitted(dsn)  # a refused user, password or database name ends here, not after the wait
        opened = AsyncConnectionPool(dsn, open=False, **sizes)
        try:
            with opening():
                await opened.open(wait=True, timeout=WAIT)
            await migrate(opened, upto=SEALED - 1 if migrating else None)
            loaded = await keys.start(opened, keyring, rewrap=rewrap)
            evidence = EvidenceStore(
                settings.evidence_dir,
                id_key=evidence_id_key(key),
                address_key=loaded.address,
                evidence_keys=loaded.evidence,
                migrating=migrating,
            )
            if client is not None:
                await client.start()
            yield cls(
                settings, opened, targets, Tokeniser(key), evidence, llm, client, keyring, loaded.missing,
                kek=loaded.kek, signer=signer,
            )  # fmt: skip
        finally:
            await opened.close()
