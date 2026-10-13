"""The hash-chained audit log (docs/mvp/PACK.md §4): checking ``ds.audit_chain`` against itself, the
audit rows and a SIEM export (``verify``, for ``ds audit verify``), and a pack's segment (``check_segment``).

Row ``seq``'s hash covers row ``seq - 1``'s (``prev``), its object's previous row's (``obj_prev``, 64 zeros
first) and its stored text. Rows are named by ``seq``; an audit row without a chain entry by its id.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from deltasleuth.store import repo

ZERO = "0" * 64
ALTERED = "the audit log was altered after it was written"
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
            raise ValueError(f"{path} line {number} is not a deltasleuth.audit line") from None
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


async def verify(conn: repo.Conn, siem: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Check the chain (docs/mvp/PACK.md §4.2) on ``conn``, which has no transaction open: contiguous
    ``seq``, both links, each hash, each text against its audit row's columns, and every audit row
    chained. With ``siem`` (``read_siem``), also both ways against it over the rows its lines span: a
    line with a ``chain`` entry by that entry and its row's content, one without (logged before 009) by
    its row's content; two lines naming one row (by ``seq`` or id) must say the same.

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
    while page := await repo.audit_chain(conn, after=links.last[0]):
        for row in page:
            links.check(row)
            seq, name = row["seq"], f"row {row['seq']} ({_name(row)})"
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
    head = {"seq": links.last[0], "hash": links.last[1]} if rows else None
    matched = None if siem is None else {"lines": len(seen) + len(stored), "first": low, "last": high}
    return {"head": head, "rows": rows, "upgrade_seq": upgrade, "siem": matched}
