"""The data keys at start.

``start`` creates the blob address key and an evidence key when absent, and loads those the keyring can
unwrap. In ``ds serve`` only (``rewrap``) it also adds an evidence key when the newest was not wrapped
by the current KEK, and rewraps every data key under an older KEK. Every process wraps new data keys
under the KEK of the newest evidence key while the keyring holds it, so only the server's restart
moves new keys onto an appended KEK: a process started between a KEK's append and that restart writes
nothing the running server cannot read. A process started before the append and still running after
the restart lacks the new KEK, so it cannot open a data key the restart rewrapped and it had not
loaded: its worker pauses that run until resumed. A missing KEK does not stop the start; what needs
it fails when used. ``verify`` reads every blob back (``ds keys status --verify``, the restore check).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from deltasleuth.auth.principal import MIGRATION
from deltasleuth.evidence.store import (
    EvidenceCorruptedError,
    EvidenceKeyError,
    EvidenceNotFoundError,
    EvidenceStore,
)
from deltasleuth.security.crypto import KEY_BYTES, DataKey, Keyring, KeyringError, key_id
from deltasleuth.store import repo
from deltasleuth.store.db import lock_migrations

ADDRESS = "address"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Keys:
    address: DataKey
    evidence: tuple[DataKey, ...]  # oldest first: the last writes
    missing: dict[str, int]  # each KEK the keyring lacks -> the data keys it wraps
    kek: str  # the KEK new data keys are wrapped under: the newest evidence key's, while the keyring holds it


async def start(pool: AsyncConnectionPool, keyring: Keyring, *, rewrap: bool = False) -> Keys:
    """Create, rewrap (``rewrap``) and load the store's keys, in one transaction under the migration
    lock, audited by ``system:migration``."""
    async with repo.audited(pool) as (conn, audit):
        await lock_migrations(conn)
        rows = await repo.data_keys(conn)
        created = []
        if not any(row["scope"] == ADDRESS for row in rows):
            created.append(await _create(conn, keyring, ADDRESS, os.urandom(KEY_BYTES)))
        evidence = [row for row in rows if row["scope"].startswith("E-")]
        if not evidence or (rewrap and evidence[-1]["kek"] != keyring.current):
            key = os.urandom(KEY_BYTES)
            created.append(await _create(conn, keyring, key_id("E-", key), key))
        moved: Counter[str] = Counter()
        for row in rows if rewrap else ():
            if row["kek"] != keyring.current and row["kek"] in keyring.ids:
                kek, wrapped = keyring.wrap(keyring.unwrap(row["kek"], row["wrapped"]))
                await repo.rewrap_data_key(conn, row["scope"], kek, wrapped)
                moved[row["kek"]] += 1
        rows = await repo.data_keys(conn)
        store = [_load(keyring, row) for row in rows if not row["scope"].startswith("R-")]  # runs': lazily
        for key in created:
            await audit(MIGRATION, "keys.created", key.scope, {"kek": key.kek})
        for old, count in moved.items():
            await audit(MIGRATION, "keys.rewrap", "keys", {"from": old, "to": keyring.current, "rows": count})
    missing = Counter(row["kek"] for row in rows if row["kek"] not in keyring.ids)
    for kek, count in missing.items():
        log.warning(
            "warning: the keyring lacks %s, which wraps %d data key(s): restore the line from the vault and"
            " restart",
            kek,
            count,
        )
    [address] = (key for key in store if key.scope == ADDRESS)
    loaded = tuple(key for key in store if key.scope != ADDRESS)
    kek = loaded[-1].kek if loaded[-1].kek in keyring.ids else keyring.current
    return Keys(address, loaded, dict(missing), kek)


async def _create(conn: repo.Conn, keyring: Keyring, scope: str, key: bytes) -> DataKey:
    kek, wrapped = keyring.wrap(key)
    await repo.insert_data_key(conn, scope, kek, wrapped)
    return DataKey(scope, kek, key)


def _load(keyring: Keyring, row: repo.Row) -> DataKey:
    if row["kek"] not in keyring.ids:
        return DataKey(row["scope"], row["kek"])
    key = keyring.unwrap(row["kek"], row["wrapped"])
    if row["scope"].startswith("E-") and key_id("E-", key) != row["scope"]:
        raise KeyringError(f"data key {row['scope']} does not match its id: the row was altered")
    return DataKey(row["scope"], row["kek"], key)


async def verify(pool: AsyncConnectionPool, evidence: EvidenceStore) -> dict[str, int]:
    """Read every blob an observation names: how many were checked, and how many are readable,
    missing, corrupted or unreadable for a KEK the keyring lacks (``key_missing``)."""
    counts = dict.fromkeys(("checked", "readable", "missing", "corrupted", "key_missing"), 0)
    after = ""
    while True:
        async with pool.connection() as conn:
            page = await repo.blob_addresses(conn, after)
        if not page:
            return counts
        for outcome in await asyncio.to_thread(_read_all, evidence, page):
            counts[outcome] += 1
        counts["checked"] += len(page)
        after = page[-1]


def _read_all(evidence: EvidenceStore, addresses: list[str]) -> list[str]:
    return [_read(evidence, address) for address in addresses]


def _read(evidence: EvidenceStore, address: str) -> str:
    try:
        evidence.get_body(address)
    except EvidenceNotFoundError:
        return "missing"
    except EvidenceKeyError:
        return "key_missing"
    except EvidenceCorruptedError:
        return "corrupted"
    return "readable"
