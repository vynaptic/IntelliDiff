"""Captured samples as observations: paired responses shared as files.

Layout 1: ``root/{a,b}/<encode_key(key)>.json`` holds bodies verbatim, each with an optional
``.meta.json`` of capture fields. Layout 2: ``root/*.jsonl`` holds records ``{key, side, body |
body_text | body_b64, ...capture fields}``. Capture fields default to a 200 read with one attempt
and a complete page walk (``_capture``); unknown fields are rejected. Every read must resolve
inside the root, and messages name a sample by side and position, never by file name (a name is
a key, maybe personal data).
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import math
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, unquote

from deltasleuth.connectors.base import ConnectorError, NotCapturedError, record_headers, utc_iso
from deltasleuth.connectors.paging import check_pagination, names_next_page
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import content_hash
from deltasleuth.core.spec.documents import in_container
from deltasleuth.core.types import ERROR_CLASSES, Observation, Side, SideConfig
from deltasleuth.evidence.fsio import atomic_write, ensure_private_dir

BODY_SUFFIX = ".json"
META_SUFFIX = ".meta.json"
_MAX_NAME_BYTES = 255  # common file-name limit (APFS, ext4)
_WALK_FIELDS = ("pages", "pages_eof", "pagination_unstable")
_META_FIELDS = {
    "status",
    "headers",
    "latency_ms",
    "observed_at",
    "attempts",
    "error_class",
    "error_detail",
    *_WALK_FIELDS,
}
_BODY_FIELDS = ("body", "body_text", "body_b64")
_RECORD_FIELDS = {*_META_FIELDS, *_BODY_FIELDS, "key", "side"}


class SampleError(ConnectorError):
    """A sample file or record is malformed, ambiguous, or outside the sample root."""


def encode_key(key: str) -> str:
    """Flat ASCII file-name stem of any key. Dots are escaped too, so they are structural only:
    key ``x.meta`` never reads as the meta file of key ``x``, and no key makes ``.`` or ``..``."""
    if not isinstance(key, str) or not key:
        raise SampleError("sample keys must be non-empty strings")
    return quote(key, safe="").replace(".", "%2E")


def decode_key(stem: str) -> str:
    """Inverse of ``encode_key``; names written with plain ``quote`` (dots as-is) read too."""
    try:
        key = unquote(stem, errors="strict")
    except UnicodeDecodeError:
        raise SampleError("sample file name is not valid percent-encoded UTF-8") from None
    if not key:
        raise SampleError("sample file name encodes an empty key")
    return key


def _integral(value: Any) -> int | None:
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _observed_at(value: Any, where: str) -> str:
    """ISO 8601 with an explicit offset, as the canonical UTC text live fetches record."""
    if not isinstance(value, str):
        raise SampleError(f"{where}: observed_at must be an ISO 8601 string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise SampleError(f"{where}: observed_at is not ISO 8601") from None
    if moment.tzinfo is None:
        raise SampleError(f"{where}: observed_at must carry a UTC offset")
    return utc_iso(moment)


def _is_headers(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(v, str) for v in value.values())


def _is_latency(value: Any) -> bool:
    number = isinstance(value, int | float | Decimal) and not isinstance(value, bool)
    return value is None or (number and value >= 0)


def _capture(fields: Mapping[str, Any], where: str, default_observed_at: str) -> dict[str, Any]:
    """The Observation fields a capture records: validated, with defaults applied."""

    def get(name: str, default: Any, ok: Callable[[Any], bool], expected: str) -> Any:
        if not ok(value := fields.get(name, default)):
            raise SampleError(f"{where}: {name} must be {expected}")
        return value

    def integer(name: str, default: int, low: int = 0, high: float = math.inf) -> int:
        value = _integral(fields.get(name, default))
        if value is None or not low <= value <= high:
            expected = "an integer HTTP status (100-599)" if name == "status" else "a non-negative integer"
            raise SampleError(f"{where}: {name} must be {expected}")
        return value

    def flag(name: str, default: bool) -> bool:
        return get(name, default, lambda v: isinstance(v, bool), "true or false")

    walk = [name in fields for name in _WALK_FIELDS]
    if any(walk) and not all(walk):  # a missing pages_eof would default to "complete"
        raise SampleError(f"{where}: {', '.join(_WALK_FIELDS)} describe the page walk together (all or none)")
    latency = get("latency_ms", None, _is_latency, "a non-negative number")
    observed_at = fields.get("observed_at")
    return {
        "http_status": integer("status", 200, 100, 599),
        "headers": record_headers(get("headers", {}, _is_headers, "an object of strings").items()),
        "latency_ms": None if latency is None else float(latency),
        "observed_at": default_observed_at if observed_at is None else _observed_at(observed_at, where),
        "attempts": integer("attempts", 1),
        "error_class": get(
            "error_class",
            None,
            lambda v: v is None or v in ERROR_CLASSES,
            f"one of {', '.join(ERROR_CLASSES)}",
        ),
        "error_detail": get("error_detail", None, lambda v: v is None or isinstance(v, str), "a string"),
        "pages": integer("pages", 1),
        "pages_eof": flag("pages_eof", True),
        "pagination_unstable": flag("pagination_unstable", False),
    }


def _parse_object(data: bytes, where: str, allowed: set[str]) -> dict[str, Any]:
    try:
        value, duplicate = jsonio.parse_json(data)
    except (jsonio.JSONParseError, jsonio.ResourceLimitError) as exc:
        raise SampleError(f"{where}: not valid JSON ({type(exc).__name__})") from None
    if duplicate:  # re-serialising would silently drop the duplicates observe() must see
        raise SampleError(f"{where}: duplicate member names (store such a body as body_text)")
    if not isinstance(value, dict):
        raise SampleError(f"{where}: expected a JSON object")
    if unknown := sorted(value.keys() - allowed):
        raise SampleError(f"{where}: unknown fields {unknown}")
    return value


def _record_body(record: Mapping[str, Any], where: str) -> bytes:
    present = [name for name in _BODY_FIELDS if name in record]
    if len(present) != 1:
        raise SampleError(f"{where}: exactly one of body, body_text or body_b64 is required")
    name, value = present[0], record[present[0]]
    if name == "body":
        return jsonio.dumps(value).encode("utf-8")
    if not isinstance(value, str):
        raise SampleError(f"{where}: {name} must be a string")
    if name == "body_text":
        return value.encode("utf-8")
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error:
        raise SampleError(f"{where}: body_b64 is not valid base64") from None


def _mtime_iso(path: Path) -> str:
    return utc_iso(datetime.fromtimestamp(path.stat().st_mtime, UTC))


def _read_at_most(path: Path, max_bytes: int) -> bytes | None:
    """The file's bytes, or None when it holds more than ``max_bytes`` (then it is not read)."""
    if path.stat().st_size > max_bytes:
        return None
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)  # it may have grown since the stat
    return data if len(data) <= max_bytes else None


class Sample(NamedTuple):
    observation: Observation
    walk_recorded: bool


def _observation(
    key: str, side: Side, body: bytes | None, capture: dict[str, Any], max_bytes: int
) -> Observation:
    """The observation as captured; ``body`` None means it was above the cap and not read."""
    if body is None and capture["error_class"] is None:  # a recorded failure already explains it
        detail = f"sample body exceeds the {max_bytes}-byte limit; not read"
        capture = {**capture, "error_class": "resource_limit", "error_detail": detail}
    return Observation(
        key=key, side=side, body=body, source="file", content_hash=content_hash(body), **capture
    )


def _judge_unrecorded_walk(obs: Observation, side_cfg: SideConfig) -> Observation:
    """Live mode proves completeness by walking to the last page; a file shows one page. A page a
    walk would follow (a next cursor or link, a non-empty offset/page window) was not captured in
    full, so it is marked stopped before the end."""
    paging = check_pagination(side_cfg.pagination)
    if (
        paging is None
        or obs.body is None
        or obs.error_class
        or obs.http_status not in side_cfg.success_status
    ):
        return obs
    try:
        doc, _ = jsonio.parse_json(obs.body)
    except (jsonio.JSONParseError, jsonio.ResourceLimitError):
        return obs  # observe() classifies an unreadable body
    if not names_next_page(paging, doc, obs.headers):
        return obs
    detail = "sample holds one page that is not shown to be the last, and no page walk was recorded"
    return dataclasses.replace(obs, pages_eof=False, error_detail=detail)


class FileSampleSource:
    """Index of the samples under ``root`` (both layouts); layout-1 files are read on demand.

    ``max_body_bytes`` defaults to the evidence store's cap, so every sample can be stored.
    """

    def __init__(self, root: str | Path, *, max_body_bytes: int = jsonio.DEFAULT_MAX_BYTES) -> None:
        try:
            self._root = Path(root).resolve(strict=True)
        except OSError:
            raise SampleError(f"sample root {str(root)!r} does not exist{in_container(root)}") from None
        if not self._root.is_dir():
            raise SampleError(f"sample root {str(root)!r} is not a directory")
        self._max_body = max_body_bytes
        self._index: dict[tuple[str, Side], Callable[[], Sample]] = {}
        for side in Side:
            self._scan_side(side)
        self._scan_jsonl()

    def keys(self) -> list[str]:
        """Every key captured on either side, sorted."""
        return sorted({key for key, _ in self._index})

    def observation(self, key: str, side: Side | str) -> Observation | None:
        """The captured observation, or None when this key was not captured on this side."""
        sample = self.sample(key, side)
        return None if sample is None else sample.observation

    def sample(self, key: str, side: Side | str) -> Sample | None:
        load = self._index.get((key, Side(side)))
        return None if load is None else load()

    def _contained(self, path: Path, where: str) -> Path:
        try:
            real = path.resolve(strict=True)
        except OSError:
            raise SampleError(f"{where}: cannot be resolved (broken symlink?)") from None
        if not real.is_relative_to(self._root):
            raise SampleError(f"{where}: resolves outside the sample root (symlink?)")
        return real

    def _add(self, key: str, side: Side, load: Callable[[], Sample], where: str) -> None:
        if (key, side) in self._index:
            raise SampleError(f"{where}: a second sample for the same key and side")
        self._index[(key, side)] = load

    def _scan_side(self, side: Side) -> None:
        directory = self._root / side.value
        if not directory.exists():
            return
        directory = self._contained(directory, side.value)
        if not directory.is_dir():
            raise SampleError(f"{side.value}: expected a directory of samples")
        found: dict[str, dict[str, tuple[Path, str]]] = {BODY_SUFFIX: {}, META_SUFFIX: {}}
        for position, entry in enumerate(sorted(directory.iterdir()), start=1):
            where = f"{side.value}/#{position} (in file-name order)"
            if entry.is_dir():
                raise SampleError(f"{where}: unexpected directory (layout 1 is one flat directory per side)")
            suffix = next((s for s in (META_SUFFIX, BODY_SUFFIX) if entry.name.endswith(s)), None)
            if suffix is None:
                continue  # notes, editor backups, .DS_Store: not samples
            try:
                key = decode_key(entry.name.removesuffix(suffix))
            except SampleError as exc:
                raise SampleError(f"{where}: {exc}") from None
            if key in found[suffix]:
                raise SampleError(f"{where}: another file already encodes the same key")
            self._contained(entry, where)  # fail at load time; reads check again in case links change
            found[suffix][key] = (entry, where)
        bodies, metas = found[BODY_SUFFIX], found[META_SUFFIX]
        if orphans := metas.keys() - bodies.keys():
            raise SampleError(f"{side.value}: {len(orphans)} meta file(s) without a body file")
        for key, (path, where) in bodies.items():
            self._add(key, side, partial(self._load_file, key, side, path, where, metas.get(key)), where)

    def _load_file(
        self, key: str, side: Side, path: Path, where: str, meta: tuple[Path, str] | None
    ) -> Sample:
        path = self._contained(path, where)
        fields: dict[str, Any] = {}
        if meta is not None:
            fields = _parse_object(self._contained(*meta).read_bytes(), meta[1], _META_FIELDS)
        capture = _capture(fields, where, _mtime_iso(path))
        body = _read_at_most(path, self._max_body)
        return Sample(_observation(key, side, body, capture, self._max_body), "pages" in fields)

    def _scan_jsonl(self) -> None:
        for path in sorted(self._root.glob("*.jsonl")):
            real = self._contained(path, path.name)
            observed_at = _mtime_iso(real)
            with real.open("rb") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if line.strip():
                        where = f"{path.name} line {line_no}"
                        sample = self._record(line, where, observed_at)
                        self._add(sample.observation.key, sample.observation.side, lambda s=sample: s, where)

    def _record(self, line: bytes, where: str, observed_at: str) -> Sample:
        record = _parse_object(line, where, _RECORD_FIELDS)
        key, side = record.get("key"), record.get("side")
        if not isinstance(key, str) or not key:
            raise SampleError(f"{where}: key must be a non-empty string")
        if side not in ("a", "b"):
            raise SampleError(f"{where}: side must be 'a' or 'b'")
        body = _record_body(record, where)
        capture = _capture(record, where, observed_at)
        kept = body if len(body) <= self._max_body else None  # re-serialising can grow it (1E+99)
        return Sample(_observation(key, Side(side), kept, capture, self._max_body), "pages" in record)


class FileConnector:
    """``Connector`` over captured samples. ``run_id``/``call_seq`` are accepted and unused."""

    def __init__(self, source: FileSampleSource) -> None:
        self._source = source

    async def fetch(
        self, key: str, side_cfg: SideConfig, *, run_id: str = "p0", call_seq: int = 0
    ) -> Observation:
        sample = self._source.sample(key, side_cfg.side)
        if sample is None:
            raise NotCapturedError(f"no sample captured on side {side_cfg.side.value} for this key")
        if sample.walk_recorded:
            return sample.observation
        return _judge_unrecorded_walk(sample.observation, side_cfg)

    async def aclose(self) -> None:
        return None


class SampleWriter:
    """Writes layout-1 samples atomically, owner-only (directories 0700, files 0600).

    The meta file goes first: a crash in between leaves an orphan meta the reader rejects,
    never a body that would read as a plain 200. Names differing only in case are refused on
    every file system (macOS folds case), as are names above the common 255-byte limit.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        ensure_private_dir(self._root)
        self._names: dict[Side, dict[str, set[str]]] = {}  # per side: case-folded name -> exact names
        for side in Side:
            ensure_private_dir(self._root / side.value)
            self._names[side] = {}
            for name in os.listdir(self._root / side.value):
                self._names[side].setdefault(name.casefold(), set()).add(name)

    def write(self, key: str, side: Side | str, body: bytes, **capture: Any) -> Path:
        """Store one sample with its capture fields (None values are left out). Record the page
        walk whenever it is known, so file mode judges completeness like the live read."""
        side = Side(side)
        stem = encode_key(key)
        meta_name, body_name = stem + META_SUFFIX, stem + BODY_SUFFIX
        if len(meta_name) > _MAX_NAME_BYTES:
            raise SampleError("key is too long to be stored as a sample file name")
        if unknown := sorted(capture.keys() - _META_FIELDS):
            raise SampleError(f"unknown capture fields {unknown}")
        meta = {"status": 200, **{name: value for name, value in capture.items() if value is not None}}
        meta["headers"] = record_headers(meta.get("headers", {}).items())
        if "observed_at" in meta:
            meta["observed_at"] = _observed_at(meta["observed_at"], "observed_at")
        _capture(meta, "meta", "")  # the reader must accept what the writer stores
        for name in (meta_name, body_name):
            if self._names[side].get(name.casefold(), {name}) != {name}:  # no name is shown: each is a key
                raise SampleError(
                    f"{side.value}: the key's sample file name collides with another key's on a"
                    " case-insensitive file system"
                )
        directory = self._root / side.value
        data = json.dumps(meta, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        for name, content in ((meta_name, data.encode("utf-8")), (body_name, body)):
            atomic_write(directory / name, content)
            self._names[side].setdefault(name.casefold(), set()).add(name)
        return directory / body_name

    def write_observation(self, obs: Observation) -> Path:
        """Store ``obs`` with every field that decides how complete it is (the walk included)."""
        if obs.body is None:
            raise SampleError("an observation without a body cannot be stored as a layout-1 sample")
        capture = {name: getattr(obs, name) for name in _META_FIELDS - {"status"}}
        return self.write(obs.key, obs.side, obs.body, status=obs.http_status, **capture)
