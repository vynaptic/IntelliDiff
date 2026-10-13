"""Array rewriting (§8.1): keyed, set and multiset arrays become ``KeyedMap``.

The normalised tree and its raw twin are rewritten in lockstep from the normalised values, so
the raw walk compares the same members: member keys come from normalised key fields, members
are ordered by canonical form (ties by raw form), and a set keeps the first raw member as its
representative. Set members are keyed by their canonical form, or by a short digest of it for a
container, so keys stay bounded however deeply rewritten arrays nest. Arrays are rewritten
deepest first; a member without a key field reads it as null and still takes part.
"""

from __future__ import annotations

from collections.abc import Sequence

from deltasleuth.core.canonical.encoding import canonical_json, exact_json, member_key_text, sha256_hex
from deltasleuth.core.canonical.paths import find, get_at, set_at
from deltasleuth.core.types import JSON, ArrayRule, Counted, KeyedMap


def rewrite_pair(normed: JSON, raw: JSON, rules: Sequence[ArrayRule]) -> tuple[JSON, JSON]:
    """Rewrite a normalised document and its raw twin (both mutated); returns the new roots."""
    found = [
        (location, order, rule)
        for order, rule in enumerate(rules)
        if rule.mode != "ordered"
        for value, location in find(rule.path, normed)
        if isinstance(value, list)
    ]
    # Deepest first: rewriting a node leaves every location still pending valid.
    for location, _, rule in sorted(found, key=lambda item: (-len(item[0]), item[1])):
        members = get_at(normed, location)
        if isinstance(members, list):  # else an earlier rule at the same location rewrote it
            new_normed, new_raw = _rewrite(rule, members, get_at(raw, location))
            normed, raw = set_at(normed, location, new_normed), set_at(raw, location, new_raw)
    return normed, raw


def _rewrite(rule: ArrayRule, members: list[JSON], raw_members: list[JSON]) -> tuple[KeyedMap, KeyedMap]:
    encoded = [canonical_json(m) for m in members]
    groups: dict[str, list[int]] = {}
    for i in sorted(range(len(members)), key=lambda i: (encoded[i], exact_json(raw_members[i]))):
        if rule.mode == "keyed":
            key = member_key_text(_key_parts(members[i], rule.key))
        else:
            key = _identity(members[i], encoded[i])
        groups.setdefault(key, []).append(i)
    normed, raw = KeyedMap(mode=rule.mode), KeyedMap(mode=rule.mode)
    for key in sorted(groups):
        normed[key] = _group(rule.mode, members, groups[key])
        raw[key] = _group(rule.mode, raw_members, groups[key])
    return normed, raw


def _group(mode: str, values: list[JSON], indexes: list[int]) -> list[JSON]:
    if mode == "keyed":
        return [values[i] for i in indexes]
    first = values[indexes[0]]
    return [first] if mode == "set" else [Counted(first, len(indexes))]


def _key_parts(member: JSON, fields: Sequence[str]) -> list[JSON]:
    return [member.get(f) for f in fields] if type(member) is dict else [None] * len(fields)


def _identity(member: JSON, encoded: str) -> str:
    """Set member key: the canonical form of a scalar (typed: ``"12"`` is not ``12``), a 64-bit
    digest for a container (``{#<hex>}`` object, ``[#<hex>]`` array)."""
    if not isinstance(member, dict | list):
        return encoded
    digest = sha256_hex(encoded)[:16]
    return f"{{#{digest}}}" if type(member) is dict else f"[#{digest}]"
