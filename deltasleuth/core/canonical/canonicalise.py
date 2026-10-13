"""Canonicalise one side's parsed body: select and map, include/exclude, normalise a copy
(key fields before any member key exists), check tolerance values, rewrite arrays of both trees
in lockstep, valueFilter, then hash each tree (``raw_hash`` keeps normalised-away differences)."""

from __future__ import annotations

import copy

from deltasleuth.core.canonical.arrays import rewrite_pair
from deltasleuth.core.canonical.encoding import canonical_json, exact_json, sha256_hex
from deltasleuth.core.canonical.errors import CanonicalError
from deltasleuth.core.canonical.mapping import apply_map, apply_select
from deltasleuth.core.canonical.normalisers import normalise_document, read_decimal
from deltasleuth.core.canonical.paths import ROOT_PATTERN, find, pattern_of
from deltasleuth.core.canonical.project import apply_value_filter, project_paths
from deltasleuth.core.types import JSON, Canonical, CompiledSpec, FieldRule, SideConfig


def canonicalise(doc: JSON, side_cfg: SideConfig, spec: CompiledSpec, *, key: str) -> Canonical:
    """Canonical form of one side's body (``doc`` is not modified).

    Raises ``CanonicalError`` for data that cannot be canonicalised, ``ExpressionError`` for spec
    defects.
    """
    compare = spec.compare
    raw, include_matched = project_paths(
        shape(doc, side_cfg), include=compare.include, exclude=compare.exclude, arrays=compare.arrays
    )
    normed = normalise_document(copy.deepcopy(raw), compare.fields)
    _check_tolerance_values(normed, compare.fields)
    normed, raw = rewrite_pair(normed, raw, compare.arrays)
    filtered = {}
    if compare.value_filter:
        filtered = apply_value_filter(normed, raw, compare.value_filter, key=key, side=side_cfg.side)
    return Canonical(
        doc=normed,
        hash=sha256_hex(canonical_json(normed)),
        raw_doc=raw,
        raw_hash=sha256_hex(exact_json(raw)),
        filtered_members=filtered,
        include_matched=include_matched,
    )


def shape(doc: JSON, side_cfg: SideConfig) -> JSON:
    """``select`` (default ``$``) then ``map``; never shares nodes with ``doc``."""
    node = apply_select(side_cfg.select or ROOT_PATTERN, doc)
    return apply_map(side_cfg.map, node) if side_cfg.map else copy.deepcopy(node)


def _check_tolerance_values(doc: JSON, rules: tuple[FieldRule, ...]) -> None:
    """An unreadable tolerance value is INCONCLUSIVE (schema), never a match or a mismatch."""
    for rule in rules:
        if rule.compare.mode != "tolerance":
            continue
        for value, location in find(rule.path, doc):
            if value is not None and read_decimal(value) is None:  # null is compared as a state
                message = "a value cannot be read as a decimal for tolerance compare"
                raise CanonicalError(message, pattern=pattern_of(location))
