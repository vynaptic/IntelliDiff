"""Masking, without I/O: which value class protects a position, keyed tokens, and masked renditions of
keys, findings, pointers and documents.

Policy: a matching tokenise rule of a PII floor class always tokenises, whatever ``security.clear`` says.
Otherwise the most specific rule wins, a clear entry only when strictly more specific (a tie tokenises).
Unlisted paths follow ``security.unlisted``. Nulls stay clear. A
member name is protected as the object holding it says (``Policy.names``).

Tokens are deterministic, one-way HMACs separated by value class, under a key the caller reads
(``security.tokenise`` reads it from its ``file:``). A class-only value (``date_of_birth``) shows as its
class, never a linkable token; member names that are data are tokens in pointers and views and ``[*]`` in
patterns. A run's request keys are key tokens, of a derivation of their own, so a key someone chooses to
compare never shows as the token its text has as a value; a run records the derivation at its creation
(``key_form``), and one created before key tokens keeps its keys' value tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from deltasleuth.core.canonical.encoding import scalar_text
from deltasleuth.core.canonical.paths import ROOT_PATTERN, child_pattern, member_pattern, render_pointer
from deltasleuth.core.classify.patterns import (
    DESC,
    WILD,
    PatternError,
    Segment,
    Segments,
    covers,
    name,
    reaches_below,
    segments,
    specificity,
)
from deltasleuth.core.jsonio import dumps, number_text
from deltasleuth.core.types import (
    JSON,
    ChangeType,
    CompiledSpec,
    Counted,
    Finding,
    KeyedMap,
    SecurityPolicy,
)

# free_text / free_number are the unlisted classes: in the floor, ``unlisted: clear`` would be moot.
DEFAULT_PII_FLOOR = frozenset(
    "name phone email tax_id address account_number device_id date_of_birth identifier".split()
)
CLASS_ONLY = frozenset({"date_of_birth"})  # shown as its class, never as a token
UNLISTED_TEXT = "free_text"
UNLISTED_NUMBER = "free_number"
MASKED_BOOLEAN = "class:boolean"  # a two-valued secret gets no globally linkable token
BUSINESS_CODE = "business_code"  # a key class shown in clear
MIXED_MEMBER_CLASS = "identifier"  # a member key built from fields of different classes
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")  # a member name that may be a field's
_DIGIT_HEAVY = re.compile(r"\d{3}|(?:\d\D*){4}")  # three digits in a row, or four in all: an identifier's
# All digits, UUIDs, long hex, a short alphabetic prefix before 3+ digits (ACC00012345, E-0001234),
# e-mail addresses and phone numbers: data, never a field name, however stable across samples.
ID_LIKE = re.compile(
    r"\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}"
    r"|[A-Za-z]{0,6}[-_:]?\d{3,}[A-Za-z]{0,2}|[^@\s]+@[^@\s]+\.[^@\s]+|\+?\d[\d ().-]{5,}\d",
    re.IGNORECASE,
)


def reads_as_data(member: str) -> bool:
    """Whether a member name reads as data and not as a field's name. A field's name is a letter or ``_``,
    then at most 63 letters, digits, ``_`` or ``-``, and is not data by its digits (three in a row, or four
    in all: an account or card number, however it is prefixed or dashed) or by the profiler's rule
    (``ID_LIKE``: digits, an id, an e-mail address, a phone number). A name that is data with fewer
    digits (``Smith``, ``acct_abcdef``) reads as a field's."""
    if not _FIELD_NAME.fullmatch(member) or _DIGIT_HEAVY.search(member):
        return True
    return ID_LIKE.fullmatch(member) is not None


@dataclass(frozen=True)
class Resolved:
    """The class protecting one position; ``value_class`` None means clear."""

    value_class: str | None
    unlisted: bool = False  # tokenised only because no rule lists the position

    def for_value(self, value: JSON) -> str | None:
        """The token class of a concrete value: unlisted numbers are ``free_number``."""
        if self.unlisted and isinstance(value, Decimal | int) and not isinstance(value, bool):
            return UNLISTED_NUMBER
        return self.value_class


_CLEAR = Resolved(None)
_UNLISTED = Resolved(UNLISTED_TEXT, unlisted=True)


class Policy:
    """A compiled policy over positions given as segment tuples (rules cover their subtrees)."""

    def __init__(
        self, tokenize: Iterable[tuple[str, str]], clear: Iterable[str], unlisted: str, floor: frozenset[str],
        field_paths: tuple[str, ...] | None = None,
    ) -> None:
        self._tokenize = [(segments(path), cls) for path, cls in tokenize]
        self._clear = [segments(path) for path in clear]
        self._unlisted = _UNLISTED if unlisted == "tokenize" else _CLEAR
        self._floor = floor
        self._fields = None if field_paths is None else _Fields(field_paths)

    def resolve(self, segs: Segments) -> Resolved:
        rules = [r for r in self._tokenize if covers(r[0], segs)]
        floor = [r for r in rules if r[1] in self._floor]
        if floor:
            return Resolved(_most_specific(floor))
        clear = max((specificity(c) for c in self._clear if covers(c, segs)), default=None)
        if clear is not None and (not rules or clear > max(specificity(r[0]) for r in rules)):
            return _CLEAR
        return Resolved(_most_specific(rules)) if rules else self._unlisted

    def resolve_deep(self, segs: Segments) -> Resolved:
        """Like ``resolve``, but tokenised too where a tokenise rule protects a descendant.

        For compound text (a set member's JSON, a composite key) and for a scalar returned where
        the spec protects fields below it (an address object sent as one string): either carries
        the protected data, whatever ``clear`` says about the parent.
        """
        own = self.resolve(segs)
        if own.value_class is not None:
            return own
        below = [r for r in self._tokenize if reaches_below(r[0], segs)]
        return Resolved(_most_specific(below)) if below else own

    def names(self, segs: Segments) -> Callable[[str], str | None]:
        """The class of a member *name* of the object at ``segs``, by name; None when it shows as it is.

        An object an explicit tokenise rule covers holds data in its names as well (a map keyed
        by email address): each is of the rule's class. Under the unlisted default alone the names
        are field names, or every one of every unlisted object would be a token; one that reads as
        data (``reads_as_data``) is ``free_text``, unless a clear entry covers that member. A run that pins
        its field labels also masks names absent from its declared structure, whatever their spelling.
        """
        resolved = self.resolve(segs)
        if not resolved.unlisted:
            return lambda member: resolved.value_class

        def unlisted(member: str) -> str | None:
            pos = (*segs, name(member))
            field = self._fields is None or self._fields.accepts(member, segs)
            if field and self.resolve(pos).value_class is None:
                return None
            return None if field and not reads_as_data(member) else UNLISTED_TEXT

        return unlisted


class _Fields:
    def __init__(self, paths: tuple[str, ...]) -> None:
        self.parents: dict[str, set[Segments]] = {}
        for path in paths:
            pos = segments(path)
            for i, part in enumerate(pos):
                if part != DESC and part != WILD:
                    self.parents.setdefault(part[1], set()).add(pos[:i])

    def accepts(self, member: str, parent: Segments) -> bool:
        """Only a literal member at this position is a declared field. A wildcard never declares names."""
        parents = self.parents.get(member, set())
        end = ("end", "")
        return parent in parents or any(covers((*rule, end), (*parent, end)) for rule in parents)


def _declared_paths(spec: CompiledSpec) -> set[str]:
    fields = {r.path for r in spec.security.tokenize}
    fields.update(r.path for r in spec.compare.fields)
    fields.update((*spec.security.clear, *spec.compare.include, *spec.compare.exclude))
    for array in spec.compare.arrays:
        fields.add(array.path)
        fields.update(child_pattern(member_pattern(array.path), key) for key in array.key)
    return fields


def run_masking(spec: CompiledSpec, params: Mapping[str, Any]) -> CompiledSpec:
    """Apply the field-name policy pinned at run creation, including the frozen table-column plan."""
    if "member_names" not in params:
        return spec
    mode = params["member_names"]
    if mode not in ("catalogue", "declared"):
        raise ValueError("unknown member-name policy")
    fields = _declared_paths(spec) if mode == "declared" else set()
    tables = params.get("tables") or {}
    for side in ("a", "b"):
        if side in tables:
            fields.update(child_pattern(ROOT_PATTERN, column) for column in tables[side]["columns"])
    paths = tuple(sorted(fields))
    digest = hashlib.sha256(dumps(list(paths)).encode()).hexdigest()
    return replace(
        spec, security=replace(spec.security, field_paths=paths), judge_key=f"{spec.content_hash}:{digest}"
    )


def _most_specific(rules: list[tuple[Segments, str]]) -> str:
    return max(rules, key=lambda r: specificity(r[0]))[1]  # max keeps the first declared on ties


@lru_cache(maxsize=256)
def canonical_policy(security: SecurityPolicy) -> Policy:
    """The policy for canonical documents and findings: ``spec.security`` over the PII floor."""
    rules = ((r.path, r.value_class) for r in security.tokenize)
    return Policy(rules, security.clear, security.unlisted, DEFAULT_PII_FLOOR, security.field_paths)


def class_for(path_pattern: str, security: SecurityPolicy) -> str | None:
    """Class protecting ``path_pattern`` (None = clear); unlisted paths report ``free_text``."""
    return canonical_policy(security).resolve(segments(path_pattern)).value_class


def member_class(array: Segments, spec: CompiledSpec) -> str | None:
    """Class of the member keys of the keyed/set array at ``array`` (None = clear).

    A keyed member key is text of its key fields and a set member key of the whole member, so it
    is protected when any of them, or anything inside, is. A declared class beats the unlisted
    default; key fields of different declared classes give ``identifier``.
    """
    policy = canonical_policy(spec.security)
    member = (*array, WILD)
    rule = next((r for r in spec.compare.arrays if segments(r.path) == array), None)
    fields = rule.key if rule is not None and rule.mode == "keyed" else ()
    resolved = [policy.resolve_deep((*member, name(f))) for f in fields] or [policy.resolve_deep(member)]
    declared = {r.value_class for r in resolved if r.value_class is not None and not r.unlisted}
    if declared:
        return declared.pop() if len(declared) == 1 else MIXED_MEMBER_CLASS
    return UNLISTED_TEXT if any(r.unlisted for r in resolved) else None


# -- tokens and masked renditions ------------------------------------------------------------------

TOKEN_CHARS = 26
LABEL_CHARS = 10  # tells one key's few class-only members apart; not an identifier
MIN_KEY_BYTES = 32  # of a tokeniser's key
_CLASS = re.compile(r"[a-z][a-z0-9_]*")  # the class and version must not break the token format
_VERSION = re.compile(r"[A-Za-z0-9]+")
_TOKEN = re.compile(rf"tok:{_CLASS.pattern}:{_VERSION.pattern}:[A-Z2-7]{{{TOKEN_CHARS}}}")
_INLINE = re.compile(r"masked key [1-9][0-9]* of [1-9][0-9]*")  # an inline key's place in its list
KeyForm = Literal["value", "k1", "k2"]
KEY_FORM: Literal["k2"] = "k2"  # exact wire key after the spec's key normalisers
VALUE_FORM: KeyForm = "value"  # a run created before key tokens: its keys are value tokens of their class


class TokeniserError(ValueError):
    """Invalid key material, key version or token class."""


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

    def key_token(self, value_class: str, key: str, *, form: Literal["k1", "k2"] = KEY_FORM) -> str:
        """Tokenise the exact request key. Runs pinned to k1 retain their original NFC derivation."""
        _check_class(value_class)
        domain, material = ("key", scalar_text(key)) if form == "k1" else ("key2", key)
        mac = self._mac(f"\x00{domain}\x1f{value_class}\x1f{material}")
        return f"tok:{value_class}:{form}:{mac}"

    def scoped_label(self, value_class: str, scope: str, value: JSON) -> str:
        """``class:<class>#<10 base32>``: equal values agree under one ``scope`` (the key) only."""
        _check_class(value_class)
        # The NUL prefix keeps labels and tokens apart; the length prefix stops the scope from
        # absorbing part of the value.
        message = f"\x00scoped\x1f{value_class}\x1f{len(scope)}:{scope}\x1f{scalar_text(value)}"
        return f"class:{value_class}#{self._mac(message)[:LABEL_CHARS]}"

    def field_label(self, cls: str, text: str, scope: str | None) -> str:
        """An object name's exact spelling, so distinct Unicode keys both survive a masked view."""
        if text == scalar_text(text):
            return _label(text, cls, self, scope)
        _check_class(cls)
        if cls in CLASS_ONLY:
            if scope is None:
                return f"class:{cls}"
            mac = self._mac(f"\x00field\x1f{cls}\x1f{len(scope)}:{scope}\x1f{text}")
            return f"class:{cls}#{mac[:LABEL_CHARS]}"
        mac = self._mac(f"\x00field\x1f{cls}\x1f{text}")
        return f"tok:{cls}:{self.version}:{mac}"

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


def inline_stand_ins(count: int) -> list[str]:
    """What stands for the ``count`` inline keys of a stored spec whose key class is not a business code:
    each key's place in the list and nothing of its text. Never a token: a token of a key someone typed
    into a spec would say whether that text is a key of a run, or a value of the class, anywhere in the
    installation."""
    return [f"masked key {place} of {count}" for place in range(1, count + 1)]


def is_stand_in(text: str) -> bool:
    """Whether ``text`` stands for a key and is not one: a token's form (``is_token``) or an inline key's
    place in its list (``inline_stand_ins``)."""
    return is_token(text) or _INLINE.fullmatch(text) is not None


def key_form(params: Mapping[str, Any]) -> KeyForm:
    """How a run masks its keys, as its ``params`` recorded at its creation: ``key_tokens`` names the
    derivation; a run created before key tokens names none, and its keys stay value tokens. A derivation this
    build does not know is refused, never guessed: its keys would be masked otherwise than stored."""
    form = params.get("key_tokens")
    if form is None:
        return VALUE_FORM
    if form == KEY_FORM:
        return KEY_FORM
    if form == "k1":
        return "k1"
    raise TokeniserError(f"a run's key_tokens {form!r} is no key derivation this build knows")


def mask_key(key: str, spec: CompiledSpec, tok: Tokeniser, *, form: KeyForm) -> str:
    """The request key as reports show it: clear for a business code, else, in the run's ``form``
    (``key_form``), a key token of its class, or for a run created before key tokens the value token.

    A class-only key is tokenised all the same: every key must stay distinguishable.
    """
    cls = spec.key.value_class
    if cls == BUSINESS_CODE:
        return key
    return tok.token(cls, key) if form == "value" else tok.key_token(cls, key, form=form)


def _label(text: str, cls: str | None, tok: Tokeniser | None, key: str | None) -> str:
    """A member key or member name of class ``cls`` as pointers, reports and views show it."""
    if cls is None:
        return text
    if tok is None:  # no key configured: a marker that is neither the value nor a valid token
        return f"tok:{cls}:unkeyed"
    if cls in CLASS_ONLY:  # a label scoped to the key tells members apart without linking records
        return tok.scoped_label(cls, key, text) if key is not None else f"class:{cls}"
    return tok.token(cls, text)


def _field_label(text: str, cls: str | None, tok: Tokeniser | None, key: str | None, exact: bool) -> str:
    if exact and cls is not None and tok is not None:
        return tok.field_label(cls, text, key)
    return _label(text, cls, tok, key)


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
        data = seg == WILD or policy.names(tuple(walked))(seg[1]) is not None
        out = member_pattern(out) if data else child_pattern(out, seg[1])
        walked.append(seg)
    return out


def mask_pointer(
    parts: tuple[tuple[str, str], ...], spec: CompiledSpec, tok: Tokeniser | None, *, key: str | None = None
) -> str:
    """``pointer_parts`` as a finding's pointer with protected member keys and data names masked.

    Each segment's class comes from the pattern walked so far (its array for a member key, its
    object for a name); with every class clear this is the differ's json_pointer. ``tok`` None
    renders protected segments as a non-token marker; ``key`` scopes class-only segments.
    """
    policy = canonical_policy(spec.security)
    exact_names = spec.security.field_paths is not None
    walked: list[Segment] = []
    masked: list[tuple[str, str]] = []
    for kind, text in parts:
        here, shown = tuple(walked), text
        if kind == "field":
            shown = _field_label(text, policy.names(here)(text), tok, key, exact_names)
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
        if value is None:
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
            class_of = policy.names(segs)
            classes = {k: class_of(k) for k in value}
            exact = spec.security.field_paths is not None
            masked = {
                _field_label(k, classes[k], tok, key, exact): walk(v, (*segs, name(k)))
                for k, v in value.items()
            }
            # masked-name order where a name is data: both sides line up, raw order hidden
            return dict(sorted(masked.items())) if any(classes.values()) else masked
        if isinstance(value, list | tuple):
            return [walk(v, (*segs, WILD)) for v in value]
        # resolve_deep: a scalar where the spec protects descendants is that protected data.
        resolved = policy.resolve_deep(segs)
        if isinstance(value, bool) and resolved.unlisted and spec.security.field_paths is None:
            return value  # keep historical unlisted cells consistent with their persisted evidence
        cls = resolved.for_value(value)
        if cls is None:
            return value
        if isinstance(value, bool):
            return MASKED_BOOLEAN
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


def mask_finding(f: Finding, spec: CompiledSpec, tok: Tokeniser, *, form: KeyForm) -> dict[str, Any]:
    """The report row of a finding: key, pointer, patterns, values and member key masked; the key in the
    run's ``form`` (``mask_key``).

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
        member: str | None = mask_key(f.key, spec, tok, form=form)
    elif f.member_key is not None and f.array_path is not None:
        member = mask_member_key(f.member_key, f.array_path, spec, tok, key=f.key)
    else:
        member = None
    return {
        "key": mask_key(f.key, spec, tok, form=form),
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
    """The structure of an unclassified answer. Names, strings and numbers are tokens; booleans show type."""

    def walk(value: JSON) -> JSON:
        if value is None:
            return value
        if isinstance(value, bool):
            return MASKED_BOOLEAN
        if isinstance(value, dict):
            return dict(sorted((tok.field_label(UNLISTED_TEXT, k, None), walk(v)) for k, v in value.items()))
        if isinstance(value, list):
            return [walk(member) for member in value]
        return tok.token(UNLISTED_TEXT if isinstance(value, str) else UNLISTED_NUMBER, value)

    return walk(doc)
