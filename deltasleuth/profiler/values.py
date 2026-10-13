"""Value helpers: presentation-insensitive tokens, value classes and example-free shapes.

A light token makes values that differ only in presentation compare equal: numbers and numeric
strings, phone formats, ISO instants (UTC, whole seconds), case and separators. Tokens live in
memory only; evidence carries counts, lengths and shapes (``A9{8}``), never a value.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import groupby
from typing import NamedTuple

import phonenumbers

from deltasleuth.core.jsonio import number_text
from deltasleuth.core.types import JSON

_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
_DIGITS_WITH_SEPARATORS = re.compile(r"[\d\s().\-/]+")
_INTERNATIONAL_PHONE = re.compile(r"\+[\d\s().\-]{7,24}")
_SEPARATORS = re.compile(r"[\s\-_./,()]+")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?([Zz]|[+-]\d{2}(?::?\d{2})?)?)?"
)
_NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_MAX_SHAPE_CHARS = 48


class Timestamp(NamedTuple):
    instant: datetime  # naive UTC
    fraction_digits: int


def parse_timestamp(text: str) -> Timestamp | None:
    """An ISO 8601 date or date-time, its offset applied so ``instant`` is UTC."""
    m = _TIMESTAMP.fullmatch(text.strip())
    if not m:
        return None
    *fields, fraction, zone = m.groups()
    try:
        instant = datetime(*(int(f or 0) for f in fields), int((fraction or "0")[:6].ljust(6, "0")))
        if zone and zone.upper() != "Z":
            digits = zone[1:].replace(":", "")
            hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
            if hours > 23 or minutes > 59:  # no such offset (timestamp_utc rejects it too)
                return None
            offset = timedelta(hours=hours, minutes=minutes)
            instant = instant - offset if zone[0] == "+" else instant + offset
    except (ValueError, OverflowError):
        return None
    return Timestamp(instant, len(fraction or ""))


def international_phone(text: str) -> phonenumbers.PhoneNumber | None:
    """The number an international-format phone string (``+44 ...``) denotes, else None."""
    digits = re.sub(r"\D", "", text)
    if not (_INTERNATIONAL_PHONE.fullmatch(text.strip()) and 8 <= len(digits) <= 15):
        return None
    try:
        return phonenumbers.parse("+" + digits, None)
    except phonenumbers.NumberParseException:
        return None


def is_phone_like(text: str) -> bool:
    """International format, or digits with separators long enough to be a phone number."""
    t = text.strip()
    digits = re.sub(r"\D", "", t)
    return international_phone(t) is not None or (
        bool(_DIGITS_WITH_SEPARATORS.fullmatch(t)) and 9 <= len(digits) <= 15
    )


def is_email(text: str) -> bool:
    return bool(_EMAIL.fullmatch(text.strip()))


def _is_number(value: JSON) -> bool:
    return isinstance(value, Decimal | int) and not isinstance(value, bool)


def numeric_value(value: JSON) -> Decimal | None:
    """The number a JSON number or a plain numeric string denotes, else None."""
    if _is_number(value):
        return Decimal(value)
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        return Decimal(value.strip())
    return None


def decimal_places(value: JSON) -> int | None:
    """Digits after the decimal point as written (``"1250.10"`` -> 2), None for non-numbers."""
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        return len(value.strip().partition(".")[2])
    if isinstance(value, Decimal):
        exponent = value.as_tuple().exponent
        return max(0, -exponent) if isinstance(exponent, int) else None
    return 0 if _is_number(value) else None


def light_token(value: JSON) -> str | None:
    """Presentation-insensitive token of a scalar (None for null, objects and arrays)."""
    if value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, bool):
        return "b:true" if value else "b:false"
    if _is_number(value):
        return "n:" + number_text(Decimal(value))
    t = unicodedata.normalize("NFKC", value).strip()
    if (ts := parse_timestamp(t)) is not None:
        return "t:" + ts.instant.replace(microsecond=0).isoformat()
    if (phone := international_phone(t)) is not None:
        return f"n:{phone.national_number}"
    if _NUMBER.fullmatch(t):
        return "n:" + number_text(Decimal(t))
    if _DIGITS_WITH_SEPARATORS.fullmatch(t) and any(ch.isdigit() for ch in t):
        return "n:" + number_text(Decimal(re.sub(r"\D", "", t)))
    return "s:" + _SEPARATORS.sub("", t.casefold())


def _char_class(ch: str) -> str:
    if ch.isdigit():
        return "9"
    if ch.isalpha():
        return "A" if ch.isupper() else "a"
    return " " if ch.isspace() else ch  # punctuation carries format, not content


def value_shape(text: str) -> str:
    """Run-length character-class shape of a string (``"C00012345"`` -> ``"A9{8}"``)."""
    runs = ((cls, len(list(group))) for cls, group in groupby(map(_char_class, text[:_MAX_SHAPE_CHARS])))
    shape = "".join(cls if n == 1 else f"{cls}{{{n}}}" for cls, n in runs)
    return shape + ("…" if len(text) > _MAX_SHAPE_CHARS else "")


def name_tokens(name: str) -> list[str]:
    """Words of a member name: ``sortCode`` -> ``[sort, code]``, ``e164`` -> ``[e, 164]``."""
    return [word.lower() for word in _NAME_WORDS.findall(name)]
