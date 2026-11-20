"""Tokenisation policy: which value class protects a position (None = clear).

A matching tokenise rule of a PII floor class always tokenises, whatever ``security.clear`` says.
Otherwise the most specific rule wins, a clear entry only when strictly more specific (a tie
tokenises). Unlisted paths follow ``security.unlisted``. Booleans and nulls are always clear (the
maskers apply that). A member name is protected as the object holding it says (``Policy.names``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from deltasleuth.core.classify.patterns import (
    WILD,
    Segments,
    covers,
    name,
    reaches_below,
    segments,
    specificity,
)
from deltasleuth.core.types import JSON, CompiledSpec, SecurityPolicy

# free_text / free_number are the unlisted classes: in the floor, ``unlisted: clear`` would be moot.
DEFAULT_PII_FLOOR = frozenset(
    "name phone email tax_id address account_number device_id date_of_birth identifier".split()
)
CLASS_ONLY = frozenset({"date_of_birth"})  # shown as its class, never as a token
UNLISTED_TEXT = "free_text"
UNLISTED_NUMBER = "free_number"
BUSINESS_CODE = "business_code"  # a key class shown in clear
MIXED_MEMBER_CLASS = "identifier"  # a member key built from fields of different classes
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")  # a member name that may be a field's
_DIGIT_HEAVY = re.compile(r"\d{3}|(?:\d\D*){4}")  # three digits in a row, or four in all: an identifier's


def reads_as_data(member: str) -> bool:
    """Whether a member name reads as data and not as a field's name. A field's name is a letter or ``_``,
    then at most 63 letters, digits, ``_`` or ``-``, and is not data by its digits (three in a row, or four
    in all: an account or card number, however it is prefixed or dashed) or by the profiler's rule
    (``profile.ID_LIKE``: digits, an id, an e-mail address, a phone number). A name that is data with fewer
    digits (``Smith``, ``acct_abcdef``) reads as a field's."""
    if not _FIELD_NAME.fullmatch(member) or _DIGIT_HEAVY.search(member):
        return True
    from deltasleuth.profiler.profile import ID_LIKE  # here: the profiler takes a second to import

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
        self, tokenize: Iterable[tuple[str, str]], clear: Iterable[str], unlisted: str, floor: frozenset[str]
    ) -> None:
        self._tokenize = [(segments(path), cls) for path, cls in tokenize]
        self._clear = [segments(path) for path in clear]
        self._unlisted = _UNLISTED if unlisted == "tokenize" else _CLEAR
        self._floor = floor

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
        data (``reads_as_data``) is ``free_text``, unless a clear entry covers that member.
        """
        resolved = self.resolve(segs)
        if not resolved.unlisted:
            return lambda member: resolved.value_class

        def unlisted(member: str) -> str | None:
            if not reads_as_data(member) or self.resolve((*segs, name(member))).value_class is None:
                return None
            return UNLISTED_TEXT

        return unlisted


def _most_specific(rules: list[tuple[Segments, str]]) -> str:
    return max(rules, key=lambda r: specificity(r[0]))[1]  # max keeps the first declared on ties


@lru_cache(maxsize=256)
def canonical_policy(security: SecurityPolicy) -> Policy:
    """The policy for canonical documents and findings: ``spec.security`` over the PII floor."""
    rules = ((r.path, r.value_class) for r in security.tokenize)
    return Policy(rules, security.clear, security.unlisted, DEFAULT_PII_FLOOR)


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
