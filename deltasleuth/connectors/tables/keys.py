"""Key texts: a composite key's parts, and a side's key map between canonical and side values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.core.spec.keys import join_parts, split_parts

Plan = dict[str, Any]  # runs.params["tables"], or one side of it
encode_key = join_parts


def decode_key(key: str, n: int) -> list[str]:
    """The ``n`` parts of a key's text; raises ``KeyRejectedError`` for another number of parts."""
    parts = split_parts(key, n)
    if parts is None:
        raise KeyRejectedError(f"a composite key needs {n} parts joined by |")
    return parts


def key_maps(side: Plan) -> list[Mapping[str, Any] | None]:
    """The side's key map per key column; None is the identity. A plan stored before key maps has
    none: every key column is the identity."""
    return side.get("key_maps") or [None] * len(side["keys"])


def side_values(key: str, side: Plan) -> list[list[str]] | None:
    """For each key column, the values that can hold a canonical key; None when its key maps cannot
    map it back, so no row of this side can hold it. Raises ``KeyRejectedError`` for a malformed
    composite key."""
    parts = decode_key(key, len(side["keys"]))
    if side.get("engine") == "files" and not key.isascii():
        try:  # a lone surrogate: rejected without a pass over the files
            key.encode()
        except UnicodeEncodeError:
            raise KeyRejectedError("the key has no UTF-8 form") from None
    values = [to_side(part, key_map) for part, key_map in zip(parts, key_maps(side), strict=True)]
    return None if any(v is None for v in values) else values  # type: ignore[return-value]


def to_side(part: str, key_map: Mapping[str, Any] | None) -> list[str] | None:
    """The values a side may store for one canonical key part, or None."""
    kind = key_map["kind"] if key_map else "identity"
    if kind == "prefix_pad":  # exactly the values that prefix || lpad(v, width, '0') (never cut) maps here
        prefix, width = key_map["prefix"], key_map["width"]  # type: ignore[index]
        digits = part[len(prefix) :] if part.startswith(prefix) else ""
        if not (digits.isascii() and digits.isdigit()) or len(digits) < width:
            return None
        # Padded exactly to the width, the value may be stored with fewer (or no) leading zeros.
        suffixes = range(len(digits)) if len(digits) == width else range(1)
        values = [digits[i:] for i in suffixes if not digits[:i].strip("0")]
        if key_map["sideType"] == "integer":  # type: ignore[index]
            values = [v for v in values if v == "0" or not v.startswith("0")]
        return values or None
    if kind == "regex":
        rule = key_map["toSide"]  # type: ignore[index]
        match = re.fullmatch(rule["pattern"], part)
        if match is None:
            return None
        value = match.expand(re.sub(r"\$([0-9]+)", r"\\g<\1>", rule["replacement"].replace("\\", "\\\\")))
        # an integer column renders no leading zero and no -0
        integral = key_map["sideType"] != "integer" or re.fullmatch("0|-?[1-9][0-9]*", value)  # type: ignore[index]
        return [value] if integral else None
    return [part]
