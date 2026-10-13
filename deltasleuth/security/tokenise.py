"""Keyed tokens and masked renditions of keys, findings, pointers and documents (§9.6, §13.1).

Tokens are deterministic, one-way HMACs separated by value class; the key sits in a local
``file:`` (0600). A class-only value (``date_of_birth``) shows as its class, never a linkable
token; member names that are data are tokens in pointers and views and ``[*]`` in patterns.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Callable
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from deltasleuth.core.canonical.encoding import scalar_text
from deltasleuth.core.canonical.paths import ROOT_PATTERN, child_pattern, member_pattern, render_pointer
from deltasleuth.core.classify.patterns import DESC, WILD, PatternError, Segment, Segments, name, segments
from deltasleuth.core.jsonio import dumps, number_text
from deltasleuth.core.secrets import SecretError, read_secret
from deltasleuth.core.types import (
    JSON,
    ChangeType,
    CompiledSpec,
    Counted,
    Finding,
    KeyedMap,
    SecurityPolicy,
)
from deltasleuth.security.policy import (
    BUSINESS_CODE,
    CLASS_ONLY,
    canonical_policy,
    member_class,
)

TOKEN_CHARS = 26
LABEL_CHARS = 10  # tells one key's few class-only members apart; not an identifier
MIN_KEY_BYTES = 32
_CLASS = re.compile(r"[a-z][a-z0-9_]*")  # the class and version must not break the token format
_VERSION = re.compile(r"[A-Za-z0-9]+")
_TOKEN = re.compile(rf"tok:{_CLASS.pattern}:{_VERSION.pattern}:[A-Z2-7]{{{TOKEN_CHARS}}}")
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")  # a member name that may be a field's
_DIGIT_HEAVY = re.compile(r"\d{3}|(?:\d\D*){4}")  # three digits in a row, or four in all: an identifier's


class TokeniserError(ValueError):
    """Invalid key material, key version or token class."""


class KeyFileError(TokeniserError):
    """The key file is missing, unreadable, malformed or accessible by group or others."""


class Tokeniser:
    """HMAC tokeniser for one key version. The key is held privately and never rendered."""

    __slots__ = ("_key", "version")

    def __init__(self, key: bytes, version: str = "v1") -> None:
        if len(key) < MIN_KEY_BYTES or not _VERSION.fullmatch(version):
            raise TokeniserError(
                f"a tokeniser needs a key of {MIN_KEY_BYTES}+ bytes and an alphanumeric version"
            )
        self._key = key
        self.version = version

    def __repr__(self) -> str:
        return f"Tokeniser(version={self.version!r})"

    def token(self, value_class: str, value: JSON) -> str:
        """``tok:<class>:<version>:<26 base32>`` over the canonical scalar text of ``value``."""
        _check_class(value_class)
        mac = self._mac(f"{value_class}\x1f{scalar_text(value)}")
        return f"tok:{value_class}:{self.version}:{mac}"

    def scoped_label(self, value_class: str, scope: str, value: JSON) -> str:
        """``class:<class>#<10 base32>``: equal values agree under one ``scope`` (the key) only."""
        _check_class(value_class)
        # The NUL prefix keeps labels and tokens apart; the length prefix stops the scope from
        # absorbing part of the value.
        message = f"\x00scoped\x1f{value_class}\x1f{len(scope)}:{scope}\x1f{scalar_text(value)}"
        return f"class:{value_class}#{self._mac(message)[:LABEL_CHARS]}"

    def _mac(self, message: str) -> str:
        digest = hmac.new(self._key, message.encode(), hashlib.sha256).digest()
        return base64.b32encode(digest).decode("ascii")[:TOKEN_CHARS]


def _check_class(value_class: str) -> None:
    if not _CLASS.fullmatch(value_class):
        raise TokeniserError(f"token class {value_class!r} must match ^[a-z][a-z0-9_]*$")


def is_token(text: str) -> bool:
    """Whether ``text`` has a token's form (``Tokeniser.token``), whichever key made it: a stand-in, never
    a value to send or to store as one."""
    return _TOKEN.fullmatch(text) is not None


def load_or_create_key(ref: str | os.PathLike[str]) -> bytes:
    """The hex key at ``file:<path>`` (or a path), created at random (mode 0600) if absent: developer
    mode's workspace key."""
    ref = ref if isinstance(ref, str) else f"file:{os.fspath(ref)}"
    if not ref.startswith("file:") or ref == "file:":
        raise KeyFileError("a tokeniser key is referenced as file:<path>")
    path = Path(ref.removeprefix("file:"))
    _create_key(path)
    if path.stat().st_mode & 0o077:
        raise KeyFileError(f"key file {path} must not be accessible by group or others (chmod 600)")
    return read_key(ref)


def read_key(ref: str) -> bytes:
    """The hex key at ``file:<path>``, never created: ``encryption.tokeniser_key_ref``'s."""
    path = Path(ref.removeprefix("file:"))
    try:
        key = bytes.fromhex(read_secret(ref))
    except SecretError as exc:
        raise KeyFileError(str(exc)) from None
    except ValueError:
        raise KeyFileError(f"key file {path} must hold the key as hexadecimal text") from None
    if len(key) < MIN_KEY_BYTES:
        raise KeyFileError(f"key file {path} holds fewer than {MIN_KEY_BYTES} bytes")
    return key


def _create_key(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:  # O_EXCL: two processes never write different keys; the loser reads the winner's
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write((secrets.token_bytes(MIN_KEY_BYTES).hex() + "\n").encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())  # a lost key orphans every token made with it


def mask_key(key: str, spec: CompiledSpec, tok: Tokeniser) -> str:
    """The request key as reports show it: clear for a business code, else a token of its class.

    A class-only key is tokenised all the same: every key must stay distinguishable.
    """
    cls = spec.key.value_class
    return key if cls == BUSINESS_CODE else tok.token(cls, key)


def _label(text: str, cls: str | None, tok: Tokeniser | None, key: str | None) -> str:
    """A member key or member name of class ``cls`` as pointers, reports and views show it."""
    if cls is None:
        return text
    if tok is None:  # no key configured: a marker that is neither the value nor a valid token
        return f"tok:{cls}:unkeyed"
    if cls in CLASS_ONLY:  # a label scoped to the key tells members apart without linking records
        return tok.scoped_label(cls, key, text) if key is not None else f"class:{cls}"
    return tok.token(cls, text)


def _name_class(spec: CompiledSpec, obj: Segments) -> str | None:
    return canonical_policy(spec.security).name_class(obj)


def mask_member_key(
    member_key: str, array_path: str, spec: CompiledSpec, tok: Tokeniser, *, key: str | None = None
) -> str:
    """A keyed/set member key as reports show it; ``key`` scopes a class-only member key."""
    return _label(member_key, member_class(segments(array_path), spec), tok, key)


def mask_path_pattern(pattern: str, spec: CompiledSpec) -> str:
    """``pattern`` as every output publishes it: member names that are data written ``[*]``.

    Collapsed rather than tokenised, so a pattern still groups one field across keys and can be
    hashed into a signature.
    """
    return _published(pattern, spec.security)


@lru_cache(maxsize=4096)
def _published(pattern: str, security: SecurityPolicy) -> str:
    policy = canonical_policy(security)
    walked: list[Segment] = []
    out = ROOT_PATTERN
    for seg in segments(pattern):
        if seg == DESC:
            raise PatternError(f"a descendant segment has no path-pattern form: {pattern!r}")
        data = seg == WILD or policy.name_class(tuple(walked)) is not None
        out = member_pattern(out) if data else child_pattern(out, seg[1])
        walked.append(seg)
    return out


def mask_pointer(
    parts: tuple[tuple[str, str], ...], spec: CompiledSpec, tok: Tokeniser | None, *, key: str | None = None
) -> str:
    """``pointer_parts`` as a §6 pointer with protected member keys and data names masked.

    Each segment's class comes from the pattern walked so far (its array for a member key, its
    object for a name); with every class clear this is the differ's json_pointer. ``tok`` None
    renders protected segments as a non-token marker; ``key`` scopes class-only segments.
    """
    walked: list[Segment] = []
    masked: list[tuple[str, str]] = []
    for kind, text in parts:
        here, shown = tuple(walked), text
        if kind == "field":
            shown = _label(text, _name_class(spec, here), tok, key)
            walked.append(name(text))
        elif kind == "member":
            shown = _label(text, member_class(here, spec), tok, key)
            walked.append(WILD)
        elif kind == "index":  # an ordered-array position
            walked.append(WILD)
        masked.append((kind, shown))
    return render_pointer(masked)


def _masker(
    spec: CompiledSpec, tok: Tokeniser, key: str | None, *, cells: bool = False
) -> Callable[[Any, Segments], Any]:
    """A function masking a canonical tree found at a position, position by position. ``cells``
    renders set and multiset nodes as the arrays they were, as report cells show values."""
    policy = canonical_policy(spec.security)

    def walk(value: Any, segs: Segments) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, Counted):
            return {"value": walk(value.value, segs), "count": value.n}
        if isinstance(value, KeyedMap):
            cls = member_class(segs, spec)
            masked = {
                _label(k, cls, tok, key): [walk(m, (*segs, WILD)) for m in ms] for k, ms in value.items()
            }
            masked = dict(sorted(masked.items()))  # masked-key order: both sides line up, raw order hidden
            return _as_array(masked, value.mode) if cells and value.mode != "keyed" else masked
        if isinstance(value, dict):
            cls = policy.name_class(segs)
            masked = {_label(k, cls, tok, key): walk(v, (*segs, name(k))) for k, v in value.items()}
            return masked if cls is None else dict(sorted(masked.items()))
        if isinstance(value, list | tuple):
            return [walk(v, (*segs, WILD)) for v in value]
        # resolve_deep: a scalar where the spec protects descendants is that protected data.
        cls = policy.resolve_deep(segs).for_value(value)
        if cls is None:
            return value
        return f"class:{cls}" if cls in CLASS_ONLY else tok.token(cls, value)

    return walk


def _as_array(masked: dict[str, list[Any]], mode: str) -> list[Any]:
    """A masked set or multiset node as an array, each multiset member as often as it occurs."""
    members = [m for ms in masked.values() for m in ms]
    return [c["value"] for c in members for _ in range(c["count"])] if mode == "multiset" else members


def _cell(tree: Any) -> Any:
    """Report-cell form: scalars as they are (numbers as exact text), containers as JSON text."""
    if tree is None or isinstance(tree, bool | str):
        return tree
    if isinstance(tree, Decimal | int):
        return number_text(Decimal(tree))
    return dumps(tree, sort_keys=True)


def mask_finding(f: Finding, spec: CompiledSpec, tok: Tokeniser) -> dict[str, Any]:
    """The report row of a finding: key, pointer, patterns, values and member key masked.

    A NORMALIZATION_ONLY finding (``raw_only``) shows its raw values, its normalised ones being
    equal. A document-level finding's value is the whole document, which the side-by-side view
    shows, so its value cells stay empty and its member key is the masked key.
    """
    one_sided = f.change_type in (ChangeType.MISSING_IN_B, ChangeType.EXTRA_IN_B)
    document = one_sided and f.path_pattern == ROOT_PATTERN  # the differ never emits a root member
    walk = _masker(spec, tok, f.key, cells=True)
    segs = segments(f.path_pattern)
    a, b = (f.a_raw, f.b_raw) if f.raw_only else (f.a_value, f.b_value)
    if document:
        member: str | None = mask_key(f.key, spec, tok)
    elif f.member_key is not None and f.array_path is not None:
        member = mask_member_key(f.member_key, f.array_path, spec, tok, key=f.key)
    else:
        member = None
    return {
        "key": mask_key(f.key, spec, tok),
        "path_pattern": mask_path_pattern(f.path_pattern, spec),
        "json_pointer": mask_pointer(f.pointer_parts, spec, tok, key=f.key),
        "change_type": f.change_type.value,
        "direction": f.direction.value,
        "value_a_tok": None if document else _cell(walk(a, segs)),
        "value_b_tok": None if document else _cell(walk(b, segs)),
        "value_class": f.value_class,
        "member_key_tok": member,
        "array_path": None if f.array_path is None else mask_path_pattern(f.array_path, spec),
        "taxonomy_code": f.taxonomy_code,
        "severity": None if f.severity is None else f.severity.value,
        "signature_hash": f.signature_hash,
        "rule_id": f.rule_id,
        "rule_classification": None if f.rule_classification is None else f.rule_classification.value,
        "mask_id": f.mask_id,
        "suppressed": f.suppressed,
        "raw_only": f.raw_only,
    }


def mask_document(doc: JSON, spec: CompiledSpec, tok: Tokeniser, *, key: str | None = None) -> JSON:
    """A masked copy of a canonical document for side-by-side views (exact numbers stay Decimal):
    keyed/set arrays render as {masked member key: [members]}, objects whose names are data as
    {masked name: value}. ``key`` is the request key the document answers; it scopes class-only
    member keys and names.
    """
    return _masker(spec, tok, key)(doc, ())


def mask_raw(doc: JSON, tok: Tokeniser) -> JSON:
    """A parsed answer that no spec classifies yet (an endpoint tried from the builder), with its structure
    and nothing else: every string a ``free_text`` token and every number a ``free_number`` token; booleans
    and nulls stay. A member name stays when it reads as a field's (a letter or ``_``, then at most 63
    letters, digits, ``_`` or ``-``) and not as data: by the profiler's rule (``profile.ID_LIKE``: digits,
    an id, an e-mail address, a phone number), or by its digits (three in a row, or four in all: an account
    or card number, however it is prefixed or dashed); else it is a ``free_text`` token. A name that is data
    with fewer digits (``Smith``, ``acct_abcdef``) reads as a field's and stays. Equal values give equal
    tokens, in every answer."""
    from deltasleuth.profiler.profile import ID_LIKE  # here: the profiler takes a second to import

    def named(name: str) -> str:
        data = ID_LIKE.fullmatch(name) or _DIGIT_HEAVY.search(name)
        return name if _FIELD_NAME.fullmatch(name) and not data else tok.token("free_text", name)

    def walk(value: JSON) -> JSON:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, dict):
            return {named(name): walk(member) for name, member in value.items()}
        if isinstance(value, list):
            return [walk(member) for member in value]
        return tok.token("free_text" if isinstance(value, str) else "free_number", value)

    return walk(doc)
