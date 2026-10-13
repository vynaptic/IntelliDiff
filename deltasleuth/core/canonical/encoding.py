"""Canonical encodings.

``canonical_json`` hashes normalised documents: sorted names, NFC strings, exact numbers
(``1 == 1.0``, ``"1" != 1``); not RFC 8785, which forces doubles. ``exact_json`` keeps strings
byte-exact so raw-only differences stay visible. ``KeyedMap`` and ``Counted`` carry a reserved
``\\u001f`` tag so they never equal a plain object or array.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from decimal import Decimal
from typing import Any

from deltasleuth.core.jsonio import number_text
from deltasleuth.core.types import JSON, Counted, KeyedMap

_TAG_KEYED = "\u001fkeyed"
_TAG_COUNTED = "\u001fcounted"
_TYPE_CLASSES = (  # order matters: bool is an int, a KeyedMap is a dict
    ("boolean", bool),
    ("number", Decimal | int),
    ("string", str),
    ("keyed", KeyedMap),
    ("counted", Counted),
    ("object", dict),
    ("array", list | tuple),
)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _text(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _encode(value: Any, nfc: bool) -> str:
    kind = type_class(value)
    if kind == "string":
        return _text(_nfc(value) if nfc else value)
    if kind in ("object", "keyed"):
        body = "{" + ",".join(f"{_text(w)}:{_encode(value[k], nfc)}" for w, k in _names(value, nfc)) + "}"
        return f"[{_text(_TAG_KEYED + ':' + value.mode)},{body}]" if kind == "keyed" else body
    if kind == "array":
        return "[" + ",".join(_encode(v, nfc) for v in value) + "]"
    if kind == "counted":
        return f"[{_text(_TAG_COUNTED)},{_encode(value.value, nfc)},{value.n}]"
    if kind == "number":
        return number_text(Decimal(value))  # ints come from mapping; same form as Decimal
    return json.dumps(value)  # null, true, false


def _names(obj: dict, nfc: bool) -> list[tuple[str, str]]:
    """(written, actual) member names in encoding order.

    Names are written in NFC so NFC-equivalent spellings on two sides compare equal. Two names of
    one object sharing an NFC form are written exactly, ordered by (NFC form, name): writing both
    in the shared form would let a value swap between them hide behind an equal hash.
    """
    if not nfc:
        return [(k, k) for k in sorted(obj)]
    forms = {k: _nfc(k) for k in obj}
    shared = Counter(forms.values())
    return [(forms[k] if shared[forms[k]] == 1 else k, k) for k in sorted(forms, key=lambda k: (forms[k], k))]


def canonical_json(value: JSON) -> str:
    """Canonical text for hashing and set-member identity (NFC strings)."""
    return _encode(value, nfc=True)


def exact_json(value: JSON) -> str:
    """Canonical text with byte-exact strings (the raw hash)."""
    return _encode(value, nfc=False)


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def content_hash(body: bytes | None) -> str | None:
    """Evidence address of a response body: ``sha256:<hex>``."""
    return None if body is None else "sha256:" + sha256_hex(body)


def scalar_text(value: JSON) -> str:
    """Untyped text of a key part: strings as-is (NFC), anything else canonical."""
    return _nfc(value) if isinstance(value, str) else canonical_json(value)


def member_key_text(parts: list[JSON] | tuple[JSON, ...]) -> str:
    """Member key of a keyed-array element from its key-field values.

    Untyped, so ``"12"`` and ``12`` are one member and a type-only difference surfaces on the key
    field. Composite keys join parts with ``|`` (``\\|`` and ``\\\\`` escape literals).
    """
    if len(parts) == 1:
        return scalar_text(parts[0])
    return "|".join(scalar_text(p).replace("\\", "\\\\").replace("|", "\\|") for p in parts)


def type_class(value: Any) -> str:
    """JSON type class used by the differ and the profiler."""
    if value is None:
        return "null"
    for kind, types in _TYPE_CLASSES:
        if isinstance(value, types):
            return kind
    raise TypeError(f"not a JSON value: {type(value).__name__}")
