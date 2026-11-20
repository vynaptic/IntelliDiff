"""The hash-chained audit log: checking ``ds.audit_chain`` against itself, the audit rows, a SIEM export
and the app's anchors (``verify``, for ``ds audit verify``), a pack's segment (``check_segment``), and
writing the anchor (``anchor``).

Row ``seq``'s hash covers row ``seq - 1``'s (``prev``), its object's previous row's (``obj_prev``, 64 zeros
first) and its stored text. Rows are named by ``seq``; an audit row without a chain entry by its id.

The chain alone cannot show rows deleted from its end, nor a chain rewritten with every hash recomputed:
whoever may switch the audit log's triggers off can do either. So the services write the chain's head, its
``seq`` and ``hash`` keyed with a key derived from the tokeniser key, which the database never holds, to
``ANCHOR_FILE`` in the workspace and to their log: at start, at each sweep when the head moved, and at stop.
An anchor only moves forward: while the chain no longer holds its row as it was anchored, it is kept and an
error logged. ``verify`` refuses a chain that ends before an anchor's row or holds that row with another
hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

from deltasleuth.errors import Invalid, told
from deltasleuth.evidence.fsio import atomic_write
from deltasleuth.store import repo

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from deltasleuth.config import Settings
    from deltasleuth.context import Context

ZERO = "0" * 64
ALTERED = "the audit log was altered after it was written"
ANCHOR_FILE = "audit-anchor.json"  # in the workspace, which no database backup carries
NOTHING_OUTSIDE = "checked against nothing outside the database: a holder of its role can truncate it unseen"
LOG_EVERY_S = 60.0  # a moving head's anchor line is logged at most this often; start and stop log it
_AT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_POWERS = (
    ("owner", "owns schema ds or an object in it"),
    ("trigger", "may add a trigger to the audit log"),
    ("replica", "may set session_replication_role"),
)
log = logging.getLogger(__name__)
_logged: dict[Path, float] = {}  # by anchor file: when this process last logged its line (monotonic)
_complained: dict[Path, str] = {}  # by anchor file: the last complaint logged, so each one is logged once
# The audited fields a SIEM line and a chain row's text copy, compared field by field with the audit row.
FIELDS = ("id", "at", "actor", "actor_name", "action", "object", "detail", "request_id", "session_id",
          "client_addr")  # fmt: skip


class ChainBroken(ValueError):
    """A break; ``seq`` is the row it names (None for an audit row without a chain entry)."""

    def __init__(self, message: str, seq: int | None = None) -> None:
        super().__init__(message)
        self.seq = seq


def chain_hash(prev: str, obj_prev: str, text: str) -> str:
    """``ds.chain_hash``, computed here so that a check does not trust the database's."""
    return hashlib.sha256(f"{prev}\n{obj_prev}\n{text}".encode()).hexdigest()


class _Links:
    """Checks chain rows (``repo._CHAIN_COLUMNS``) fed in seq order: each hashes to its entry and still
    matches its audit row, follows the row before it and its object's previous row. The whole chain
    starts at row 1 without a gap; a segment holds some objects' rows, each object's from its first, so
    only rows following one another in it are checked against each other."""

    def __init__(self, *, whole: bool) -> None:
        self.whole = whole
        self.last = (0, ZERO)
        self.objects: dict[str, tuple[int, str]] = {}

    def check(self, row: Mapping[str, Any]) -> None:
        seq, obj = row["seq"], row["object"]
        name = f"row {seq} ({_name(row)})"
        follows = seq == self.last[0] + 1
        if self.whole and not follows:
            first = self.last[0] + 1
            missing = f"row {first} is" if seq == first + 1 else f"rows {first} to {seq - 1} are"
            raise ChainBroken(f"{missing} missing: {ALTERED}", first)
        if chain_hash(row["prev"], row["obj_prev"], row["text"]) != row["hash"] or not _intact(row):
            raise ChainBroken(f"{name} does not hash to its chain entry: {ALTERED}", seq)
        if follows and row["prev"] != self.last[1]:
            after = f"does not follow row {seq - 1}" if seq > 1 else "does not start the chain"
            raise ChainBroken(f"{name} {after}: {ALTERED}", seq)
        before = self.objects.get(obj)
        if row["obj_prev"] != (before[1] if before else ZERO):
            after = (
                f"does not follow row {before[0]}, the previous row of {obj}"
                if before
                else f"follows a row of {obj} that is not in the chain"
            )
            raise ChainBroken(f"{name} {after}: {ALTERED}", seq)
        self.objects[obj] = self.last = (seq, row["hash"])


def _intact(row: Mapping[str, Any]) -> bool:
    """Whether a chain row's text and object are still its audit row's, compared here: the database owner
    can replace ``ds.audit_text`` to return the stored text."""
    try:
        text = _content(json.loads(row["text"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    return row["object"] == row["audit_object"] and text == _audit(row)


def _audit(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The content of a chain row's audit row (``repo._CHAIN_COLUMNS``); None without one."""
    if row["audit_id"] is None:
        return None
    return _content(_line({name: row[f"audit_{name}"] for name in FIELDS}))


def check_segment(rows: Sequence[Mapping[str, Any]]) -> None:
    """ChainBroken at the first break of a segment (``repo.audit_segment``)."""
    links = _Links(whole=False)
    for row in rows:
        links.check(row)


def read_siem(path: Path) -> list[dict[str, Any]]:
    """The ``deltasleuth.audit`` lines of a SIEM export, one JSON object per line (blank lines skipped);
    ValueError naming the first line that is not one. Lines end at newlines only: a line's text may hold
    U+2028, U+2029 or U+0085, which str.splitlines() also splits at."""
    lines = []
    for number, text in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not text.strip():
            continue
        try:
            line = json.loads(text)
            _content(line)
            chain = line.get("chain")
            if chain is not None and not (isinstance(chain["seq"], int) and isinstance(chain["hash"], str)):
                raise TypeError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Invalid(f"{path} line {number} is not a deltasleuth.audit line") from None
        lines.append(line)
    return lines


def _content(line: Mapping[str, Any]) -> dict[str, Any]:
    """The audit row a SIEM line or a chain row's text copies: its time as an instant (lines carry the
    writer's offset, texts UTC), its client_addr as the host address (a text drops the mask a line keeps)."""
    content = {name: line[name] for name in FIELDS}
    if not isinstance(content["id"], int) or not isinstance(content["client_addr"], str | None):
        raise TypeError
    content["at"] = datetime.fromisoformat(content["at"])
    if content["client_addr"] is not None:
        content["client_addr"] = ipaddress.ip_interface(content["client_addr"]).ip
    return content


def _said(line: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[int, str] | None]:
    """What a SIEM line says: its row's content and its chain entry."""
    chain = line.get("chain")
    return _content(line), None if chain is None else (chain["seq"], chain["hash"])


def _line(row: repo.Row) -> dict[str, Any]:
    """An audit row as its SIEM line would read it."""
    return json.loads(repo.siem_line(row))


def _name(line: Mapping[str, Any]) -> str:
    return f"{line['action']} {line['object']}"


async def verify(
    conn: repo.Conn, siem: Sequence[Mapping[str, Any]] | None = None, anchors: Sequence[Anchor] = ()
) -> dict[str, Any]:
    """Check the chain on ``conn``, which has no transaction open: contiguous
    ``seq``, both links, each hash, each text against its audit row's columns, and every audit row
    chained. With ``siem`` (``read_siem``), also both ways against it over the rows its lines span: a
    line with a ``chain`` entry by that entry and its row's content, one without (logged before 009) by
    its row's content; two lines naming one row (by ``seq`` or id) must say the same. Each of ``anchors``
    (``read_anchor``) names a row the chain must still hold, with the hash it had.

    Returns ``head`` (``{seq, hash}``, None while the chain is empty), ``rows``, ``upgrade_seq`` and, with
    ``siem``, ``siem`` (``{lines, first, last}``: the lines that matched and the rows they span);
    ChainBroken at the first break."""
    await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")  # one snapshot
    upgrade = await repo.audit_upgrade_seq(conn)
    by_seq: dict[int, Mapping[str, Any]] = {}
    lines: dict[int, Mapping[str, Any]] = {}  # every line, by id
    for line in siem or ():
        seq = None if line.get("chain") is None else line["chain"]["seq"]
        earlier = [lines.setdefault(line["id"], line)]
        if seq is not None:
            earlier.append(by_seq.setdefault(seq, line))
        if any(other is not line and _said(other) != _said(line) for other in earlier):
            where = f"the audit row with id {line['id']}" if seq is None else f"row {seq}"
            raise ChainBroken(f"{where} ({_name(line)}) has two SIEM lines that differ: one is forged", seq)
    by_id = {i: line for i, line in lines.items() if line.get("chain") is None}
    # A line without a chain entry names its row by id: its row's seq, and whether their contents differ.
    stored = {row["id"]: row for row in await repo.audit_rows(conn, list(by_id))}
    differs = {i for i, row in stored.items() if _content(_line(row)) != _content(by_id[i])}
    spanned = [*by_seq, *(row["chain"]["seq"] for row in stored.values() if row["chain"] is not None)]
    low, high = (min(spanned), max(spanned)) if spanned else (1, 0)

    links, seen, rows = _Links(whole=True), set(), 0
    anchored = {held.seq: (ZERO, "") for held in anchors}  # seq -> the chain's hash and row's name there
    while page := await repo.audit_chain(conn, after=links.last[0]):
        for row in page:
            links.check(row)
            seq, name = row["seq"], f"row {row['seq']} ({_name(row)})"
            if seq in anchored:
                anchored[seq] = (row["hash"], name)
            if seq in by_seq:
                seen.add(seq)
                if by_seq[seq]["chain"]["hash"] != row["hash"] or _content(by_seq[seq]) != _audit(row):
                    raise ChainBroken(f"{name} differs from its SIEM line: {ALTERED}", seq)
            elif row["id"] in by_id:
                if row["id"] in differs:
                    upgraded = ", chained at the upgrade," if seq <= upgrade else ""
                    raise ChainBroken(f"{name}{upgraded} differs from its SIEM line: {ALTERED}", seq)
            elif low <= seq <= high:
                lost = "it was not written by DeltaSleuth, or the SIEM lost it"
                raise ChainBroken(f"{name} has no SIEM line: {lost}", seq)
        rows += len(page)
    if (row := await repo.unchained_audit_row(conn)) is not None:
        raise ChainBroken(f"the audit row with id {row['id']} ({_name(row)}) has no chain entry: {ALTERED}")
    if unseen := sorted(set(by_seq) - seen):
        seq = unseen[0]
        where = f"row {seq} ({_name(by_seq[seq])})"
        raise ChainBroken(f"{where} is in the SIEM but not in the chain: {ALTERED}", seq)
    if absent := sorted(set(by_id) - set(stored)):
        row_id = absent[0]
        where = f"the audit row with id {row_id} ({_name(by_id[row_id])})"
        raise ChainBroken(f"{where} is in the SIEM but not in the log: {ALTERED}")
    for held in anchors:
        if held.seq > links.last[0]:
            first = links.last[0] + 1
            missing = f"row {first} is" if first == held.seq else f"rows {first} to {held.seq} are"
            said = f"the anchor written {held.at} holds row {held.seq}"
            raise ChainBroken(f"{missing} missing, though {said}: {ALTERED}", first)
        digest, name = anchored[held.seq]
        if held.seq and digest != held.hash:
            said = f"is not the row the anchor written {held.at} holds"
            raise ChainBroken(f"{name} {said}: {ALTERED}", held.seq)
    head = {"seq": links.last[0], "hash": links.last[1]} if rows else None
    matched = None if siem is None else {"lines": len(seen) + len(stored), "first": low, "last": high}
    return {"head": head, "rows": rows, "upgrade_seq": upgrade, "siem": matched}


# -- the anchor ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """The chain's head as a service wrote it outside the database: row ``seq`` (0: the empty chain) and its
    ``hash``, written at ``at`` (UTC, to the second); ``mac`` keys the three with the anchor key."""

    seq: int
    hash: str
    at: str
    mac: str

    @classmethod
    def made(cls, key: bytes, seq: int, digest: str, at: str) -> Anchor:
        return cls(seq, digest, at, _mac(key, seq, digest, at))

    def text(self) -> str:
        """The anchor file's line, and the tail of the anchor's log line."""
        return json.dumps({"seq": self.seq, "hash": self.hash, "at": self.at, "mac": self.mac})


def _mac(key: bytes, seq: int, digest: str, at: str) -> str:
    signed = f"deltasleuth.audit_anchor\n{seq}\n{digest}\n{at}".encode()
    return hmac.new(key, signed, hashlib.sha256).hexdigest()


def anchor_path(settings: Settings) -> Path:
    """Where the services write the anchor."""
    return settings.workspace_dir / ANCHOR_FILE


def read_anchor(path: Path, key: bytes) -> Anchor:
    """The anchor in ``path``: an anchor file, or a copy of the anchor's log line (its last line, from its
    first ``{``). OSError when the file cannot be read; Invalid when it holds no anchor; ChainBroken when
    its mac is not the one this installation's key makes."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    try:
        doc = json.loads(lines[-1][lines[-1].index("{") :])
        found = Anchor(doc["seq"], doc["hash"], doc["at"], doc["mac"])
    except (ValueError, TypeError, KeyError, IndexError):
        raise Invalid(f"{path} holds no audit anchor") from None
    texts = (found.hash, found.at, found.mac)
    if type(found.seq) is not int or found.seq < 0 or not all(isinstance(text, str) for text in texts):
        raise Invalid(f"{path} holds no audit anchor")
    if not (_DIGEST.fullmatch(found.hash) and _AT.fullmatch(found.at) and _DIGEST.fullmatch(found.mac)):
        raise Invalid(f"{path} holds no audit anchor")
    if not hmac.compare_digest(found.mac, _mac(key, found.seq, found.hash, found.at)):
        raise ChainBroken(
            f"the anchor in {path} was not written with this installation's key: it was altered or forged"
        )
    return found


def _held(path: Path, key: bytes) -> Anchor | None:
    return read_anchor(path, key) if path.exists() else None


async def anchor(pool: AsyncConnectionPool, path: Path, key: bytes, *, said: bool = False) -> Anchor | None:
    """Write the chain's head to ``path`` as its anchor, by atomic replace, when it moved; and log the
    anchor's line when it moved and none was logged for ``LOG_EVERY_S``, or with ``said``. An anchor the
    chain no longer holds as written, its row gone or with another hash, or one that cannot be read, is kept
    and an error logged once: ``ds audit verify`` then refuses. Never raises (a fault is logged); returns
    the anchor in force, None without one."""
    try:
        held = await asyncio.to_thread(_held, path, key)
    except (OSError, ValueError) as exc:
        _complain(path, f"audit anchor {path} kept: {_why(exc)}; run ds audit verify")
        return None
    try:
        async with pool.connection() as conn:
            found = await repo.audit_anchor(conn, held.seq if held else 0)
    except psycopg.Error as exc:
        log.warning("audit anchor not written: the store answered %s", type(exc).__name__)
        return held
    if held is not None and held.seq and found["anchored"] != held.hash:
        gone = "no longer holds that row" if found["anchored"] is None else "holds that row with another hash"
        _complain(path, f"audit anchor kept at row {held.seq}: the audit chain {gone}; run ds audit verify")
        return held
    now, head = time.monotonic(), (found["seq"] or 0, found["hash"] or ZERO)
    if held is None or head != (held.seq, held.hash):
        fresh = Anchor.made(key, *head, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        try:
            await asyncio.to_thread(atomic_write, path, (fresh.text() + "\n").encode())
        except OSError as exc:
            _complain(path, f"audit anchor not written to {path}: {_why(exc)}")
            return held
        held = fresh
        said = said or now - _logged.get(path, float("-inf")) >= LOG_EVERY_S
    _complained.pop(path, None)  # a complaint of a later fault is logged again
    if said:
        _logged[path] = now
        log.info("audit anchor %s", held.text())
    return held


def _why(exc: BaseException) -> str:
    return (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else told(exc)


def _complain(path: Path, message: str) -> None:
    """Log ``message`` as an error, unless it was the last one logged of ``path``."""
    if _complained.get(path) != message:
        _complained[path] = message
        log.error("%s", message)


async def anchored(ctx: Context, *, said: bool = False) -> None:
    """``anchor`` for the context's store, in its workspace: each sweep, and a service's start and stop
    (``said``). A context without the anchor key (one a test built) writes none."""
    if ctx.anchor_key:
        await anchor(ctx.pool, anchor_path(ctx.settings), ctx.anchor_key, said=said)


@asynccontextmanager
async def kept(ctx: Context) -> AsyncIterator[None]:
    """A service's life (``ds serve``, ``ds worker``): its start logs which database roles are in use and
    anchors the chain, and its stop anchors it again."""
    try:
        async with ctx.pool.connection() as conn:
            separate, line = await roles(conn, ctx.settings)
        (log.info if separate else log.warning)("%s", line)
    except psycopg.Error as exc:
        log.warning("database roles not read: the store answered %s", type(exc).__name__)
    await anchored(ctx, said=True)
    try:
        yield
    finally:
        await anchored(ctx, said=True)


async def roles(conn: repo.Conn, settings: Settings) -> tuple[bool, str]:
    """Whether the role ``conn`` holds (database_dsn_ref's) is apart from the schema's owner, unable to
    switch the audit log's triggers off, and the line that says so."""
    guard = await repo.audit_guard(conn)
    powers = ["is a superuser"] if guard["superuser"] else [why for name, why in _POWERS if guard[name]]
    if powers:
        return False, (
            f"roles: shared: the role of database_dsn_ref {', '.join(powers)}, so whoever holds it can switch"
            " the audit log's triggers off ('ds db grants' prints the grants of a role that cannot)"
        )
    migrating = (
        "migrate_dsn_ref's role applies the migrations"
        if settings.migrate_dsn_ref
        else "migrate_dsn_ref is not set, so ds migrate runs as this role and cannot apply a migration"
    )
    return True, (
        "roles: separate: the role of database_dsn_ref owns nothing in schema ds and cannot switch the audit"
        f" log's triggers off; {migrating}"
    )
