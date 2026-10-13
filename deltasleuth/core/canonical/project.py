"""Projection: ``compare.include`` / ``exclude`` by path, then ``compare.valueFilter`` (§5, §7.2).

Include keeps the selected nodes, the containers on the way to them and the key fields of kept
keyed members (a member without its key could not be matched). An include reaching inside an
array's members keeps every container member of that array, pruned: a member lacking
the field, or present on one side only, must still be reported. Exclude then removes nodes.

valueFilter runs on the normalised, rewritten tree (the schema: "after normalization") and
mirrors removals into the raw twin. It sees each leaf and each array member, top down; keyed
members are entered, set and multiset members are atomic (their content is their identity).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from functools import partial
from itertools import compress

from deltasleuth.core.canonical import predicates
from deltasleuth.core.canonical.errors import CanonicalError, PathError
from deltasleuth.core.canonical.paths import (
    Location,
    Parts,
    delete_at,
    find,
    member_parts,
    normalise_pattern,
    parts_pattern,
    pattern_of,
    render_pointer,
)
from deltasleuth.core.types import JSON, ArrayRule, Counted, KeyedMap, Side


def project_paths(
    doc: JSON, *, include: Sequence[str] = (), exclude: Sequence[str] = (), arrays: Sequence[ArrayRule] = ()
) -> tuple[JSON, tuple[bool, ...]]:
    """Apply include then exclude. Also returns, per include path, whether it selected a node: an
    include written against the wrong shape must be reported, not compared as two empty documents."""
    selected = [[location for _, location in find(path, doc)] for path in include]
    if include:
        doc = _include(doc, {location for found in selected for location in found}, include, arrays)
    if exclude:
        doc = _exclude(doc, exclude)
    return doc, tuple(bool(found) for found in selected)


def _include(doc: JSON, kept: set[Location], include: Sequence[str], arrays: Sequence[ArrayRule]) -> JSON:
    if () in kept:
        return doc
    ancestors = {location[:i] for location in kept for i in range(len(location))}
    patterns = []
    for path in include:
        with suppress(PathError):  # a path without a pattern form selects concrete nodes only
            patterns.append(normalise_pattern(path))
    if patterns:
        _keep_members(doc, (), patterns, ancestors)
    kept |= set(_key_fields(doc, ancestors, arrays))
    return _prune(doc, (), kept, ancestors)


def _keep_members(node: JSON, location: Location, patterns: list[str], ancestors: set[Location]) -> None:
    """Add every array an include pattern passes through, with its ancestors and container members."""
    if isinstance(node, dict):
        for name, child in node.items():
            _keep_members(child, (*location, name), patterns, ancestors)
    elif isinstance(node, list):
        members = pattern_of((*location, 0))
        if any(p.startswith(members) and p[len(members) : len(members) + 1] in (".", "[") for p in patterns):
            ancestors.update(location[:i] for i in range(len(location) + 1))
            ancestors.update((*location, i) for i, child in enumerate(node) if isinstance(child, dict | list))
        for i, child in enumerate(node):
            _keep_members(child, (*location, i), patterns, ancestors)


def _key_fields(doc: JSON, ancestors: set[Location], arrays: Sequence[ArrayRule]) -> Iterator[Location]:
    for rule in arrays:
        if rule.mode != "keyed":
            continue
        for value, location in find(rule.path, doc):
            for i, member in enumerate(value if isinstance(value, list) else ()):
                if (*location, i) in ancestors and isinstance(member, dict):
                    yield from ((*location, i, field) for field in rule.key if field in member)


def _prune(node: JSON, location: Location, kept: set[Location], ancestors: set[Location]) -> JSON:
    if not isinstance(node, dict | list):
        return None  # a scalar root that include did not select
    out: dict = {}
    for step, child in node.items() if isinstance(node, dict) else enumerate(node):
        at = (*location, step)
        if at in kept:
            out[step] = child
        elif at in ancestors:
            out[step] = _prune(child, at, kept, ancestors)
    return out if isinstance(node, dict) else list(out.values())


def _exclude(doc: JSON, exclude: Sequence[str]) -> JSON:
    locations = {location for path in exclude for _, location in find(path, doc)}
    if () in locations:
        return type(doc)() if isinstance(doc, dict | list) else None
    # Longest first, highest index first within a parent, so no deletion shifts a pending one.
    for location in sorted(locations, key=lambda loc: (len(loc), loc), reverse=True):
        delete_at(doc, location)
    return doc


def apply_value_filter(
    doc: JSON, raw_doc: JSON, expression: str, *, key: str, side: Side
) -> dict[str, tuple[str, ...]]:
    """Remove the nodes ``expression`` rejects from ``doc`` and its raw twin (in place).

    Returns the removed keyed/set/multiset member keys per array pointer, for FILTER_MEMBERSHIP.
    The pointer has no ``@i`` segments: whether one appears depends on the other side's count.
    """
    walk = _FilterWalk(expression, key, side.value)
    walk.container(doc, raw_doc, ())
    return {pointer: tuple(sorted(keys)) for pointer, keys in sorted(walk.filtered.items())}


class _FilterWalk:
    def __init__(self, expression: str, key: str, side: str) -> None:
        self.expression, self.key, self.side = expression, key, side
        self.filtered: dict[str, set[str]] = {}

    def keep(self, value: JSON, parts: Parts) -> bool:
        pattern = parts_pattern(parts)
        bindings = {
            "key": self.key,
            "side": self.side,
            "path": pattern,
            "pointer": render_pointer(parts),
            "value": value,
            "stratum": None,  # no strata yet
            "strata": {},
        }
        try:
            return predicates.test(self.expression, bindings)
        except predicates.PredicateError as exc:
            raise CanonicalError(f"valueFilter failed: {exc}", pattern=pattern) from exc

    def flags(self, values: list[JSON], parts_at: Callable[[int], Parts]) -> list[bool]:
        """Keep flags. Each value is numbered among the survivors before it, as the differ numbers it."""
        flags: list[bool] = []
        kept = 0
        for value in values:
            flags.append(self.keep(value, parts_at(kept)))
            kept += flags[-1]
        return flags

    def container(self, node: JSON, raw: JSON, parts: Parts) -> None:
        if isinstance(node, KeyedMap):
            self.keyed(node, raw, parts)
        elif isinstance(node, dict):
            for name in list(node):
                at = (*parts, ("field", name))
                if isinstance(node[name], dict | list):
                    self.container(node[name], raw[name], at)
                elif not self.keep(node[name], at):
                    del node[name], raw[name]
        elif isinstance(node, list):
            flags = self.flags(node, partial(_index, parts))
            node[:], raw[:] = list(compress(node, flags)), list(compress(raw, flags))
            for i, (child, raw_child) in enumerate(zip(node, raw, strict=True)):
                self.container(child, raw_child, _index(parts, i))

    def keyed(self, node: KeyedMap, raw: KeyedMap, parts: Parts) -> None:
        array_pointer = render_pointer(p for p in parts if p[0] != "dup")
        for member_key in list(node):
            members = node[member_key]
            values = [m.value if isinstance(m, Counted) else m for m in members]
            flags = self.flags(values, partial(member_parts, parts, member_key, several=len(members) > 1))
            if not all(flags):
                self.filtered.setdefault(array_pointer, set()).add(member_key)
            survivors, raw_survivors = list(compress(members, flags)), list(compress(raw[member_key], flags))
            if not survivors:
                del node[member_key], raw[member_key]
                continue
            node[member_key], raw[member_key] = survivors, raw_survivors
            if node.mode == "keyed":
                for i, (member, raw_member) in enumerate(zip(survivors, raw_survivors, strict=True)):
                    self.container(member, raw_member, member_parts(parts, member_key, i, len(survivors) > 1))


def _index(parts: Parts, i: int) -> Parts:
    return (*parts, ("index", str(i)))
