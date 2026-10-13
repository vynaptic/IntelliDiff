"""Offline verification of an evidence pack (docs/mvp/PACK.md §3.3, §7.3): what ``ds packs verify`` runs,
needing no settings and no database.

It reads the ZIP only within limits (at most 64 entries and 200 MiB uncompressed, no entry compressed
over 100:1, no symlink, absolute path or ``..``, no name twice, one ``P-…/`` folder, format
``deltasleuth-pack/1``, a manifest the steps can read), then runs steps 0 to 6 and 8 of the pack's
procedure as VERIFY.txt's shell commands do, step 0 against this build's own procedure, and lists step
7's objects, which only the site's SIEM can check. A sampled pack's draw is checked as its certificate's
hand check says (docs/mvp/SAMPLING.md §11.4), after step 5, and then an attested labelling's files, as its
hand check says (docs/mvp/ACCURACY.md §9).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from deltasleuth.reports.pack import FORMAT, MIB, PROCEDURE, PROCEDURE_NAME, UNLISTED
from deltasleuth.security.signing import load_public, verifies
from deltasleuth.store.chain import FIELDS, ZERO, chain_hash

MAX_ENTRIES = 64
MAX_UNCOMPRESSED = 200 * MIB
MAX_RATIO = 100
_FOLDER = re.compile(r"(P-[0-9A-Z]{26})/")
_FILLED = re.compile(  # step 0's grep -Ex of line 2
    r"PACK=(P-[0-9A-Z]{26}) SIGNOFF=(A-[0-9A-Z]{26}) SPECAPP=(A-[0-9A-Z]{26}) HEAD=([0-9]+) UPGRADE=([0-9]+)"
)
_SUMS_LINE = re.compile(r"([0-9a-f]{64})  (.+)")
_REVOCATIONS = re.compile(
    r"# DeltaSleuth pack revocations, key ([0-9a-f]{64}), as of (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"
)
_STEP = re.compile(r'"step": ([0-9]+)')
_OF = re.compile(r'"of": ([0-9]+)')
_UNREADABLE = "the pack has no readable manifest.json: refused"


class UnsafePack(ValueError):
    """The ZIP is refused unread: over a limit, unsafe to unpack, or not a pack of this format."""


@dataclass(frozen=True)
class Step:
    number: int
    name: str
    ok: bool | None  # None: shown for a person to check, not checked here
    text: str = ""

    @property
    def line(self) -> str:
        """The step as ``ds packs verify`` prints it; a lone surrogate a crafted file holds is escaped."""
        said = "ok" if self.ok else "FAILED" if self.ok is False else ""
        detail = f" ({self.text})" if said and self.text else self.text
        return f"step {self.number} {self.name}: {said}{detail}".encode("utf-8", "backslashreplace").decode()


def read(path: Path) -> tuple[str, dict[str, bytes]]:
    """The pack's folder name and its files by path inside it; UnsafePack when the ZIP is refused."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise UnsafePack(f"{path.name} is not a readable zip: refused") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UnsafePack(f"the zip holds {len(infos)} entries, over {MAX_ENTRIES}: refused")
        seen: set[str] = set()
        for info in infos:
            name = info.filename
            folded = unicodedata.normalize("NFC", name.casefold())  # one file on a case-insensitive disk
            if folded in seen:
                raise UnsafePack(f"the zip holds {name!r} twice: refused")
            seen.add(folded)
            if (
                name.startswith("/")
                or "\\" in name
                or re.match(r"[A-Za-z]:", name)
                or ".." in name.split("/")
            ):
                raise UnsafePack(f"the zip's entry {name!r} is an absolute path or holds '..': refused")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UnsafePack(f"the zip's entry {name!r} is a symlink: refused")
            if info.file_size > MAX_RATIO * info.compress_size:
                raise UnsafePack(f"the zip's entry {name!r} is compressed over {MAX_RATIO}:1: refused")
        total = sum(info.file_size for info in infos)
        if total > MAX_UNCOMPRESSED:
            size, limit = math.ceil(total / MIB), MAX_UNCOMPRESSED // MIB
            raise UnsafePack(f"the zip would be {size} MiB uncompressed, over {limit} MiB: refused")
        folders = {m[1] if (m := _FOLDER.match(info.filename)) else None for info in infos}
        if len(folders) != 1 or None in folders:
            raise UnsafePack("the zip does not hold one P-… folder and nothing else: refused")
        folder = folders.pop()
        assert folder is not None
        try:
            files = {i.filename[len(folder) + 1 :]: zf.read(i) for i in infos if not i.is_dir()}
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            raise UnsafePack(f"{path.name} is not a readable zip ({exc}): refused") from None
    try:
        manifest = json.loads(files["manifest.json"])
        found = manifest["format"]
    except (KeyError, ValueError, TypeError, RecursionError):
        raise UnsafePack(_UNREADABLE) from None
    if found != FORMAT:
        raise UnsafePack(f"the pack's format is {found!r}, not {FORMAT}: refused")
    if not _readable(manifest):
        raise UnsafePack(_UNREADABLE)
    return folder, files


def _readable(manifest: dict[str, Any]) -> bool:
    """Whether the manifest holds what the steps read, as they read it: a ``pack_id``, an ``issued_at``
    with its offset, and a ``subject`` whose ``spec`` and each of its ``sides`` are objects."""
    subject = manifest.get("subject")
    if not isinstance(subject, dict) or not isinstance(subject.get("sides"), dict):
        return False
    try:
        issued = datetime.fromisoformat(manifest.get("issued_at"))
    except (TypeError, ValueError):
        return False
    return (
        isinstance(manifest.get("pack_id"), str)
        and issued.tzinfo is not None
        and isinstance(subject.get("spec"), dict)
        and all(isinstance(side, dict) for side in subject["sides"].values())
    )


def verify(path: Path, register: str, revocations: Path | None = None) -> list[Step]:
    """Check the pack at ``path`` against the fingerprint ``register`` (lower-case hex) the site's key
    register lists, and, with ``revocations`` (a directory holding the custodian's revoked.txt,
    revoked.txt.sig and signing-key.pem), that it is not revoked. Each step's outcome; UnsafePack when
    the ZIP is refused."""
    _, files = read(path)
    lines = files.get("VERIFY.txt", b"").decode("utf-8", "replace").split("\n")
    filled = _FILLED.fullmatch(lines[1]) if len(lines) > 1 else None
    rows = _rows(files.get("audit/chain.tsv", b""))
    try:  # one key, read once: the one step 1 fingerprints is the one step 2 verifies with
        key, found = load_public(files.get("signing-key.pem", b""))
        signed = verifies(key, files.get("SHA256SUMS.sig", b""), files.get("SHA256SUMS", b""))
    except ValueError:
        found, signed = None, False
    steps = [
        _procedure(lines, filled),
        _key(found, register),
        Step(2, "signature", signed),
        _files(files),
        _subject(files),
        _decided(files, filled, rows),
        *filter(None, [_drawn(files, rows), _labelled(files, rows)]),
        _links(rows),
        _siem(filled, rows),
    ]
    if revocations is None:
        steps.append(Step(8, "revocation", None, "not checked: pass --revocations DIR"))
    else:
        steps.append(_revoked(revocations, register, files))
    return steps


def _procedure(lines: list[str], filled: re.Match[str] | None) -> Step:
    """Step 0: VERIFY.txt from its line 3 is this build's procedure (the copy filed with the key register,
    by hand), and its line 2 names the pack's values in their form, never anything else."""
    if "\n".join(lines[2:]) != PROCEDURE:
        return Step(0, "procedure", False, f"VERIFY.txt from its line 3 is not {PROCEDURE_NAME}")
    if filled is None:
        return Step(0, "procedure", False, "line 2 of VERIFY.txt is malformed")
    return Step(0, "procedure", True)


def _key(found: str | None, register: str) -> Step:
    """Step 1: signing-key.pem's fingerprint (``load_public``'s, of the key step 2 verifies with)."""
    if found is None:
        return Step(1, "key", False, "signing-key.pem is not one PEM EC P-256 public key")
    if found != register:
        return Step(1, "key", False, f"signing-key.pem has fingerprint {found}, not the register's")
    return Step(1, "key", True, f"fingerprint {found}")


def _files(files: dict[str, bytes]) -> Step:
    """Step 3: each listed file hashes to its SHA256SUMS line, and no file is unlisted."""
    listed = []
    for number, line in enumerate(_lines(files.get("SHA256SUMS", b"")), 1):
        match = _SUMS_LINE.fullmatch(line)
        if match is None:
            return Step(3, "files", False, f"line {number} of SHA256SUMS is not a sums line")
        digest, name = match.groups()
        if name not in files:
            return Step(3, "files", False, f"{name} is listed but missing")
        if hashlib.sha256(files[name]).hexdigest() != digest:
            return Step(3, "files", False, f"{name} differs from its SHA256SUMS line")
        listed.append(name)
    if extra := sorted(set(files) - set(listed) - set(UNLISTED)):
        return Step(3, "files", False, f"{extra[0]} is not listed")
    if missing := sorted(set(UNLISTED) - set(files)):
        return Step(3, "files", False, f"{missing[0]} is missing")
    return Step(3, "files", True, f"{len(listed)} listed file(s) unchanged, none added")


def _subject(files: dict[str, bytes]) -> Step:
    """Step 4: what the pack is about, to match with the change record."""
    subject = json.loads(files["manifest.json"])["subject"]  # read() checked its shape
    spec, sides = subject["spec"], subject["sides"]
    named = [
        f"run {subject.get('run_id')} cycle {subject.get('cycle')}",
        *([f"basis {subject['basis']}"] if "basis" in subject else []),  # a pack issued before has none
        f"spec {spec.get('name')} {spec.get('version')} (content hash {spec.get('content_hash')})",
    ]
    for side, found in sorted(sides.items()):  # a pack issued before engines names none
        # A file side's (docs/mvp/FILES.md §16), with what its digest is the SHA-256 of: a pack issued before
        # has none.
        held = found.get("files")
        fixed = f"{held.get('count')} file(s) sha256 {held.get('sha256')}" if isinstance(held, dict) else None
        if fixed and held.get("sha256_of"):
            fixed += f" of {held['sha256_of']}"
        named.append(" ".join(str(v) for v in ("side", side, found.get("role"), found.get("target"),
                                              found.get("relation"), found.get("engine"),
                                              found.get("server"), fixed) if v))  # fmt: skip
    return Step(4, "subject", None, "; ".join(named) + ": match it with your change record")


def _lines(data: bytes) -> list[str]:
    """A text file's lines as sh and awk read them: split at newlines only, never at the U+2028, U+2029
    or U+0085 an audited text may hold, which str.splitlines() also splits at."""
    text = data.decode("utf-8", "replace")
    return text.removesuffix("\n").split("\n") if text else []


def _rows(data: bytes) -> list[list[str]] | None:
    """audit/chain.tsv's rows as their six fields; None when a line is not one."""
    rows = [line.split("\t", 5) for line in _lines(data)]
    return rows if all(len(r) == 6 and r[0].isdigit() for r in rows) else None


def _decided(files: dict[str, bytes], filled: re.Match[str] | None, rows: list[list[str]] | None) -> Step:
    """Step 5: the sign-off and the spec approval were granted over these files' hashes, every step and
    not revoked since; the plan hashes to its run's creation; the adjudication files to the attests."""
    if filled is None or rows is None:
        return Step(
            5, "decisions", False, "VERIFY.txt does not name the sign-off, or audit/chain.tsv is malformed"
        )
    _, signoff, spec_approval, _, _ = filled.groups()
    problems = [
        f"{what} {approval_id} is NOT DECIDED"
        for what, approval_id, key, name in (
            ("sign-off", signoff, "attests_sha256", "facts/attests.json"),
            ("spec approval", spec_approval, "content_hash", "facts/spec.json"),
        )
        if not _granted(rows, approval_id, key, files.get(name, b""))
    ]
    plan = f'"plan_sha256": "{hashlib.sha256(files.get("facts/plan.json", b"")).hexdigest()}"'
    if sum(plan in r[5] and '"action": "create_run"' in r[5] for r in rows) != 1:
        problems.append("facts/plan.json matches no create_run row")
    attests = files.get("facts/attests.json", b"").decode("utf-8", "replace")
    problems += [
        f"{name}'s hash is not in facts/attests.json"
        for name in ("adjudication/sheet.csv", "adjudication/score.json")
        if name in files and hashlib.sha256(files[name]).hexdigest() not in attests
    ]
    if problems:
        return Step(5, "decisions", False, "; ".join(problems))
    return Step(5, "decisions", True, f"sign-off {signoff} and spec approval {spec_approval} decided")


def _drawn(files: dict[str, bytes], rows: list[list[str]] | None) -> Step | None:
    """A sampled pack's hand check, which its certificate prints (docs/mvp/SAMPLING.md §11.4): the audit row
    of its draw (``draw_seq`` in facts/attests.json, which step 5 binds) is that draw's sample.draw row and
    holds its selection's SHA-256 and its salt's, which the salt, ``label:nonce``, hashes to. None for a
    census pack; a sampled one (step 4's basis) whose facts/attests.json cannot be read fails it, as does
    a sample whose row, draw, salt or selection is not a number or text."""
    unread = Step(5, "sample draw", False, "facts/attests.json's sample cannot be read")
    sampled = json.loads(files["manifest.json"])["subject"].get("basis") == "sample"  # read() checked it
    try:  # a crafted file: nested too deep for the parser, or not an object
        drawn = json.loads(files.get("facts/attests.json", b"")).get("sample")
    except (ValueError, AttributeError, TypeError, RecursionError):
        return unread if sampled else None
    if not isinstance(drawn, dict):
        return unread if sampled else None
    seq, draw_id = drawn.get("draw_seq"), drawn.get("draw_id")
    named = (draw_id, drawn.get("salt_label"), drawn.get("nonce"), drawn.get("selection_sha256"))
    if type(seq) is not int or not all(isinstance(value, str) for value in named):
        return unread
    salt = f"{drawn['salt_label']}:{drawn['nonce']}"
    row = next((r for r in rows or [] if r[0] == str(seq)), None)
    try:
        text = json.loads(row[5]) if row else None
    except (ValueError, TypeError, RecursionError):
        text = None
    detail = text.get("detail") if isinstance(text, dict) and text.get("action") == "sample.draw" else None
    if not isinstance(detail, dict) or detail.get("draw_id") != draw_id:
        why = f"row {seq} of audit/chain.tsv is not the sample.draw row of draw {draw_id}"
    elif detail.get("selection_sha256") != drawn.get("selection_sha256"):
        why = f"row {seq} does not hold the selection SHA-256 of facts/attests.json"
    elif detail.get("salt_sha256") != hashlib.sha256(salt.encode("utf-8", "surrogatepass")).hexdigest():
        why = f"the SHA-256 of {salt} is not the salt SHA-256 row {seq} holds"
    else:
        return Step(
            5, "sample draw", True,
            f"row {seq} is draw {draw_id}'s sample.draw row and holds both SHA-256 values; the SHA-256 of"
            f" {salt} is the salt's",
        )  # fmt: skip
    return Step(5, "sample draw", False, why)


def _labelled(files: dict[str, bytes], rows: list[list[str]] | None) -> Step | None:
    """An attested labelling's hand check, which its certificate prints (docs/mvp/ACCURACY.md §9):
    accuracy/labels.csv and accuracy/score.json hash to ``accuracy.labels_sha256`` and ``score_sha256`` of
    facts/attests.json (which step 5 binds), and the chain row that is the labelling's accuracy.label row
    holds both. None for a pack attesting no labelling; one whose manifest says it attests one fails when
    facts/attests.json cannot be read, as does a labelling whose id or a hash is not text."""
    unread = Step(5, "labelled accuracy", False, "facts/attests.json's accuracy cannot be read")
    manifest = json.loads(files["manifest.json"])  # read() checked it
    claimed = isinstance(manifest.get("accuracy"), dict) and manifest["accuracy"].get("attested") is True
    try:  # a crafted file: nested too deep for the parser, or not an object
        attested = json.loads(files.get("facts/attests.json", b"")).get("accuracy")
    except (ValueError, AttributeError, TypeError, RecursionError):
        return unread if claimed else None
    if not isinstance(attested, dict):
        return unread if claimed else None
    labelling_id = attested.get("labelling_id")
    hashes = {name: attested.get(f"{name}_sha256") for name in ("labels", "score")}
    if not all(isinstance(value, str) for value in (labelling_id, *hashes.values())):
        return unread
    for name, file in (("labels", "accuracy/labels.csv"), ("score", "accuracy/score.json")):
        if file not in files or hashlib.sha256(files[file]).hexdigest() != hashes[name]:
            why = f"{file} is missing or does not hash to facts/attests.json's accuracy.{name}_sha256"
            return Step(5, "labelled accuracy", False, why)
    for row in rows or []:
        try:
            text = json.loads(row[5])
        except (ValueError, TypeError, RecursionError):
            continue
        if row[4] != labelling_id or not isinstance(text, dict) or text.get("action") != "accuracy.label":
            continue
        detail = text.get("detail")
        held = isinstance(detail, dict) and all(
            detail.get(f"{name}_sha256") == value for name, value in hashes.items()
        )
        if not held:
            why = f"row {row[0]} does not hold facts/attests.json's accuracy.labels_sha256 and score_sha256"
            return Step(5, "labelled accuracy", False, why)
        return Step(
            5, "labelled accuracy", True,
            f"row {row[0]} is labelling {labelling_id}'s accuracy.label row and holds the SHA-256 of"
            " accuracy/labels.csv and accuracy/score.json, as facts/attests.json does",
        )  # fmt: skip
    why = f"audit/chain.tsv holds no accuracy.label row of labelling {labelling_id}"
    return Step(5, "labelled accuracy", False, why)


def _granted(rows: list[list[str]], approval_id: str, key: str, data: bytes) -> bool:
    """VERIFY.txt's ``decided``: a grant of the approval over ``data``'s hash whose step is its last, and
    no revoke."""
    held = f'"{key}": "{hashlib.sha256(data).hexdigest()}"'
    granted = revoked = False
    for row in rows:
        if row[4] != approval_id:
            continue
        revoked = revoked or '"action": "approval.revoke"' in row[5]
        step, of = _STEP.search(row[5]), _OF.search(row[5])
        if '"action": "approval.grant"' in row[5] and held in row[5] and step and of and step[1] == of[1]:
            granted = True
    return granted and not revoked


def _links(rows: list[list[str]] | None) -> Step:
    """Step 6: each row hashes to its entry; none is missing between two rows of one object, or between
    consecutive rows."""
    if rows is None:
        return Step(6, "audit rows", False, "audit/chain.tsv is malformed")
    last: dict[str, str] = {}
    hashes: dict[int, str] = {}
    for seq_text, prev, obj_prev, digest, obj, text in rows:
        seq = int(seq_text)
        if chain_hash(prev, obj_prev, text) != digest:
            return Step(6, "audit rows", False, f"row {seq} altered")
        if obj_prev != last.get(obj, ZERO):
            return Step(6, "audit rows", False, f"row {seq}: a row of {obj} before it is missing")
        if seq - 1 in hashes and prev != hashes[seq - 1]:
            return Step(6, "audit rows", False, f"row {seq}: does not follow row {seq - 1}")
        last[obj] = hashes[seq] = digest
    return Step(6, "audit rows", True, f"{len(rows)} row(s) consistent")


def _siem(filled: re.Match[str] | None, rows: list[list[str]] | None) -> Step:
    """Step 7, listed only: the objects whose rows the site's SIEM must hold, unaltered and complete."""
    objects = sorted({r[4] for r in rows or []})
    head, upgrade = (filled[4], filled[5]) if filled else ("?", "?")
    return Step(
        7,
        "SIEM",
        None,
        f"compare with your SIEM's deltasleuth.audit lines up to row {head} of {', '.join(objects)}: rows"
        f" after {upgrade} by seq and hash, those up to it with the line of their id, field by field"
        f" ({', '.join(FIELDS)}; at as an instant); rows written before SIEM lines existed (migration 005)"
        " have none: only the chain covers them, from the upgrade on",
    )


def _revoked(directory: Path, register: str, files: dict[str, bytes]) -> Step:
    """Step 8: the custodian's latest revocation list, signed by the register's key, as of the pack's issue
    or later, and complete, does not list the pack."""
    try:
        listed, signature, pem = (
            (directory / name).read_bytes() for name in ("revoked.txt", "revoked.txt.sig", "signing-key.pem")
        )
    except OSError as exc:
        return Step(8, "revocation", False, f"{exc.filename} cannot be read")
    try:
        key, found = load_public(pem)
    except ValueError:
        return Step(8, "revocation", False, "the list's signing-key.pem is not one PEM EC P-256 public key")
    if found != register:
        return Step(
            8, "revocation", False, f"the list's signing-key.pem has fingerprint {found}, not the register's"
        )
    if not verifies(key, signature, listed):
        return Step(8, "revocation", False, "revoked.txt.sig does not verify")
    lines = _lines(listed)
    header = _REVOCATIONS.fullmatch(lines[0]) if lines else None
    if header is None or header[1] != register:
        return Step(8, "revocation", False, "revoked.txt is not a revocation list of the register's key")
    if lines[-1] != "# end":
        return Step(8, "revocation", False, "revoked.txt does not end with '# end': it is incomplete")
    as_of, manifest = header[2], json.loads(files["manifest.json"])
    issued = datetime.fromisoformat(manifest["issued_at"]).replace(microsecond=0)
    if datetime.fromisoformat(as_of) < issued:
        return Step(
            8, "revocation", False, f"the list is as of {as_of}, before the pack was issued: get the latest"
        )
    if any(line.startswith(f"{manifest.get('pack_id')} ") for line in lines[1:-1]):
        return Step(8, "revocation", False, f"the pack is revoked (list as of {as_of})")
    return Step(8, "revocation", True, f"not listed as of {as_of}")
