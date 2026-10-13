"""Normaliser chains (spec.schema.json ``$defs/normalizer``, which validates the parameters).

Every normaliser is total (a value it does not apply to comes back unchanged, so normalisation
never makes a key INCONCLUSIVE), idempotent (canonical documents are fixed points) and returns
a scalar or its input (the normalised tree keeps the raw tree's shape).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Context, Decimal, InvalidOperation
from typing import Any

import phonenumbers
from stdnum.exceptions import ValidationError
from stdnum.util import get_cc_module

from deltasleuth.core.canonical.errors import ExpressionError
from deltasleuth.core.canonical.paths import find, set_at
from deltasleuth.core.jsonio import MAX_EXPONENT
from deltasleuth.core.types import JSON, FieldRule, Normalizer

# Bump on any change to a normaliser's output or to the canonical encoding. 1.1.0: e164 keeps
# extensions and leaves non-phone text, container set members are keyed by digest, NFC-colliding
# member names are encoded exactly, token_set similarity scores sorted words.
NORMALIZER_VERSION = "1.1.0"

Step = Callable[[JSON], JSON]


def _on_str(fn: Callable[[str], JSON]) -> Step:
    return lambda value: fn(value) if isinstance(value, str) else value


_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")


def _email_basic(text: str) -> str:
    # The local part may be case-sensitive (RFC 5321), but no provider treats it so.
    stripped = text.strip()
    return stripped.lower() if _EMAIL.fullmatch(stripped) else text


def _null_if_empty(value: JSON) -> JSON:
    # Empty containers too: [] vs null is the commonest representation gap.
    return None if value == "" or (isinstance(value, list | dict) and not value) else value


# USPS Publication 28 / Royal Mail style. No value is also a key, so a second pass is a no-op.
ADDRESS_ABBREVIATIONS: Mapping[str, str] = {
    "APARTMENT": "APT", "AVENUE": "AVE", "BOULEVARD": "BLVD", "BUILDING": "BLDG", "CLOSE": "CL",
    "COURT": "CT", "CRESCENT": "CRES", "DRIVE": "DR", "EAST": "E", "FLOOR": "FL", "GARDENS": "GDNS",
    "HIGHWAY": "HWY", "LANE": "LN", "MOUNT": "MT", "NORTH": "N", "NORTHEAST": "NE", "NORTHWEST": "NW",
    "PARKWAY": "PKWY", "PLACE": "PL", "ROAD": "RD", "SOUTH": "S", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "SQUARE": "SQ", "STREET": "ST", "SUITE": "STE", "TERRACE": "TER", "WEST": "W",
}  # fmt: skip
_APOSTROPHES = re.compile("['\\u2019]")  # ASCII and typographic
_ADDRESS_PUNCTUATION = re.compile(r"[^\w\s#/-]")


def _address_std(text: str) -> str:
    # NFKC, upper case, punctuation to spaces, abbreviations (design Appendix C.2). The schema
    # allows address_std only with similarity compare.
    upper = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).upper())
    cleaned = _ADDRESS_PUNCTUATION.sub(" ", _APOSTROPHES.sub("", upper))
    return " ".join(ADDRESS_ABBREVIATIONS.get(token, token) for token in cleaned.split())


_SIMPLE: Mapping[str, Step] = {
    "trim": _on_str(str.strip),
    "collapse_ws": _on_str(lambda s: re.sub(r"\s+", " ", s)),
    "nfkc": _on_str(lambda s: unicodedata.normalize("NFKC", s)),
    "casefold": _on_str(str.casefold),
    "upper": _on_str(str.upper),
    "lower": _on_str(str.lower),
    "email_basic": _on_str(_email_basic),
    "digits_only": _on_str(lambda s: re.sub(r"[^0-9]", "", s)),
    "null_if_empty": _null_if_empty,
    "address_std": _on_str(_address_std),
}

# A phone number as written, with at most one extension. phonenumbers pulls a digit run out of
# any text ("SN-2079460000-B"), which would merge different ids or serials into one value.
_PHONE_SHAPE = re.compile(
    r"\s*\+?[\d\s().\-/]+(?:(?:;\s*ext\s*=|\s*(?:ext\.?|extension|x|#))\s*\d+)?\s*", re.IGNORECASE
)


def _e164(params: Mapping[str, Any]) -> Step:
    def step(text: str) -> str:
        if len(text) > 250 or not _PHONE_SHAPE.fullmatch(text):  # 250: phonenumbers' input limit
            return text
        try:
            number = phonenumbers.parse(text, params["defaultRegion"])
        except phonenumbers.NumberParseException:
            return text
        # Possible, not valid: validity depends on range metadata, and a newly allocated range
        # would stay unnormalised on one side only.
        if not phonenumbers.is_possible_number(number):
            return text
        e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        # An extension is content: keep it in RFC 3966 form, which parses back alike.
        return f"{e164};ext={number.extension}" if number.extension else e164

    return _on_str(step)


def _tax_id(params: Mapping[str, Any]) -> Step:
    country = params["country"]
    modules = [m for m in (get_cc_module(country, name) for name in ("vat", "tin")) if m is not None]

    def step(text: str) -> str:
        if not modules:  # no python-stdnum module: strip separators, upper-case
            return re.sub(r"[^0-9A-Za-z]", "", text).upper()
        for module in modules:
            try:
                return module.validate(text)  # strips separators and prefix, checks digits
            except ValidationError:
                continue
        return text

    return _on_str(step)


# ASCII digits only, like JSON numerals.
_NUMERIC_TEXT = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*", re.ASCII)
# Enough digits for any number jsonio accepts at scale <= 38.
_WIDE = Context(prec=2 * MAX_EXPONENT + 100, Emax=MAX_EXPONENT * 4, Emin=-MAX_EXPONENT * 4)


def read_decimal(value: JSON) -> Decimal | None:
    """A finite Decimal from a number or numeric string, else None."""
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value):
        number = Decimal(value.strip())
        return number if number == 0 or abs(number.adjusted()) <= MAX_EXPONENT else None
    return None


def _decimal(params: Mapping[str, Any]) -> Step:
    exponent = Decimal(1).scaleb(-int(params["scale"]))
    rounding = "ROUND_" + params.get("rounding", "half_even").upper()  # decimal's constant names

    def step(value: JSON) -> JSON:
        number = read_decimal(value)
        if number is None:
            return value
        try:
            return number.quantize(exponent, rounding=rounding, context=_WIDE)
        except InvalidOperation:
            return value

    return step


_TIMESTAMP = [
    re.compile(
        r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
        r"(?:[Tt ](?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:[.,](?P<f>\d+))?)?"
        r"(?P<tz>[Zz]|[+-]\d{2}(?::?\d{2})?)?)?",
        re.ASCII,
    ),
    re.compile(  # ISO 8601 basic format
        r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"
        r"(?:[Tt](?P<h>\d{2})(?P<mi>\d{2})(?:(?P<s>\d{2})(?:[.,](?P<f>\d+))?)?"
        r"(?P<tz>[Zz]|[+-]\d{2}(?:\d{2})?)?)?",
        re.ASCII,
    ),
]
_TIMESPEC = {"s": ("seconds", 0), "ms": ("milliseconds", 3), "us": ("microseconds", 6)}


def _timestamp_utc(params: Mapping[str, Any]) -> Step:
    timespec, digits = _TIMESPEC[params["precision"]]
    rounding = ROUND_HALF_UP if params.get("mode") == "round" else ROUND_DOWN

    def step(text: str) -> str:
        instant = _instant(text, digits, rounding)
        return text if instant is None else instant.isoformat(timespec=timespec) + "Z"

    return _on_str(step)


def _instant(text: str, digits: int, rounding: str) -> datetime | None:
    """ISO 8601 as a naive UTC datetime rounded to ``digits`` fractional digits.

    Date-only and offset-less values read as UTC (the schema). Leap seconds, 24:00 and
    out-of-range fields are not representable: None.
    """
    match = _TIMESTAMP[0].fullmatch(text) or _TIMESTAMP[1].fullmatch(text)
    if match is None:
        return None
    g = match.groupdict()
    fraction = Decimal("0." + (g["f"] or "0")).quantize(Decimal(1).scaleb(-digits), rounding=rounding)
    tz = g["tz"] or "Z"
    hours, minutes = (0, 0) if tz in ("Z", "z") else (int(tz[1:3]), int(tz[3:].lstrip(":") or 0))
    if hours > 23 or minutes > 59:
        return None
    offset = timedelta(hours=hours, minutes=minutes) * (-1 if tz[0] == "-" else 1)
    try:
        local = datetime(*(int(g[k] or 0) for k in ("y", "mo", "d", "h", "mi", "s")))
        return local + timedelta(microseconds=int(fraction.scaleb(6))) - offset
    except (ValueError, OverflowError):
        return None


_PARAMETERISED: Mapping[str, Callable[[Mapping[str, Any]], Step]] = {
    "e164": _e164,
    "tax_id": _tax_id,
    "decimal": _decimal,
    "timestamp_utc": _timestamp_utc,
}

NORMALISER_NAMES: frozenset[str] = frozenset(_SIMPLE) | frozenset(_PARAMETERISED)


def _resolve(normalizer: Normalizer) -> Step:
    if normalizer.name in _SIMPLE:
        return _SIMPLE[normalizer.name]
    if normalizer.name in _PARAMETERISED:
        return _PARAMETERISED[normalizer.name](normalizer.params)
    raise ExpressionError(f"unknown normaliser {normalizer.name!r}")


def compile_chain(chain: Iterable[Normalizer]) -> Step:
    """One function applying ``chain`` in order."""
    steps = [_resolve(n) for n in chain]

    def apply(value: JSON) -> JSON:
        for step in steps:
            value = step(value)
        return value

    return apply


def apply_chain(value: JSON, chain: Iterable[Normalizer]) -> JSON:
    return compile_chain(chain)(value)


def normalise_document(doc: JSON, rules: Iterable[FieldRule]) -> JSON:
    """Apply each rule's chain at every location its path selects, in rule order (mutates ``doc``;
    returns the root, replaced when a rule selects ``$``)."""
    for rule in rules:
        if rule.normalize:
            chain = compile_chain(rule.normalize)
            for value, location in find(rule.path, doc):
                doc = set_at(doc, location, chain(value))
    return doc
