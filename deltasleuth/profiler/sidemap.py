"""The side-b JSONata map: rebuilds side a's shape (the canonical one) from the aligned side-b paths.

Map members are rebuilt one by one (their keys stay data), a one-member array stays an array,
and a record side b has nothing for is left out rather than written as ``{}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from deltasleuth.profiler.align import Alignment
from deltasleuth.profiler.model import ROOT, Path
from deltasleuth.profiler.profile import LEAF, OBJECT_ARRAY, OPAQUE, RECORD, SCALAR_ARRAY, SideProfile

_INDENT = "  "
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# JSONata reads these as operators or literals, so a member with such a name needs backticks.
_RESERVED = frozenset({"and", "or", "in", "true", "false", "null", "function"})


def jsonata_ref(context: str, names: Sequence[str]) -> str:
    """JSONata for member ``names`` below ``context`` (``""``, the context item, or a variable)."""
    expr = context
    for name in names:
        if "`" in name:  # not even backticks can spell it
            expr = f"$lookup({expr or '$'}, {json.dumps(name)})"
        else:
            step = name if _NAME.fullmatch(name) and name not in _RESERVED else f"`{name}`"
            expr = f"{expr}.{step}" if expr else step
    return expr or "$"


def side_b_map(pa: SideProfile, alignment: Alignment) -> str | None:
    """The map expression; None when no side-b path aligned with side a."""
    return _Builder(pa, alignment).record(ROOT, ROOT, "", 0)


class _Builder:
    """Follows side a's tree. ``b_ctx`` is the side-b path the JSONata context stands for (the
    select root, an array element or a map value bound to ``$vN``); ``ctx`` refers to it."""

    def __init__(self, pa: SideProfile, alignment: Alignment) -> None:
        self.pa, self.matches = pa, alignment.matches

    def record(self, a_path: Path, b_ctx: Path, ctx: str, level: int) -> str | None:
        entries = [
            f"{_INDENT * (level + 1)}{json.dumps(child[-1], ensure_ascii=False)}: {expr}"
            for child in self.pa.children(a_path)
            if child[-1] is not None and (expr := self.emit(child, b_ctx, ctx, level + 1)) is not None
        ]
        return "{\n" + ",\n".join(entries) + "\n" + _INDENT * level + "}" if entries else None

    def guarded_record(self, a_path: Path, b_ctx: Path, ctx: str, level: int) -> str | None:
        """A nested record, emitted only when side b has something for it.

        The guard is the side-b parent its fields share (``$exists(profile)`` for
        ``profile.givenName`` and ``profile.familyName``); without one, the record is built once
        and dropped when it came out empty.
        """
        body = self.record(a_path, b_ctx, ctx, level)
        if body is None:
            return None
        n = len(a_path)
        targets = [b[len(b_ctx) :] for a, b in self.matches.items() if len(a) > n and a[:n] == a_path]
        anchor: list[str] = []
        for segs in zip(*targets, strict=False):  # their common prefix, up to the first array or map
            if segs[0] is None or len(set(segs)) > 1:
                break
            anchor.append(segs[0])
        if len(targets) == 1:
            anchor = anchor[:-1]  # a lone field's parent, as several fields share theirs
        if anchor:
            return f"$exists({jsonata_ref(ctx, anchor)}) ? {body}"
        var = f"$r{level}"
        return f"({var} := {body}; $exists($keys({var})) ? {var})"

    def emit(self, a_path: Path, b_ctx: Path, ctx: str, level: int) -> str | None:
        kind = self.pa.kind(a_path)
        if kind == RECORD:  # an array element or map value always exists; a named record may not
            build = self.record if a_path[-1] is None else self.guarded_record
            return build(a_path, b_ctx, ctx, level)
        b_path = self.matches.get(a_path)
        if b_path is None:
            return None
        ref = jsonata_ref(ctx, b_path[len(b_ctx) :])
        if kind in (LEAF, OPAQUE):
            return ref
        if kind == SCALAR_ARRAY:
            return f'$type({ref}) = "array" ? [{ref}] : {ref}'
        if kind == OBJECT_ARRAY:
            inner = self.emit((*a_path, None), (*b_path, None), "", level) or "{}"
            step = inner if inner.startswith("{") else f"({inner})"
            return f'$type({ref}) = "array" ? [{ref}.{step}] : {ref}'
        var, key = f"$v{level}", f"$k{level}"  # a map: rebuilt member by member, keys stay data
        inner = self.emit((*a_path, None), (*b_path, None), var, level + 1)
        if inner is None:
            return None
        if inner == var:  # scalar values need no rebuilding
            return ref
        return (
            f'$type({ref}) = "object" ? $merge([{{}}, $each({ref}, function({var}, {key}) {{\n'
            f"{_INDENT * (level + 1)}{{{key}: {inner}}}\n{_INDENT * level}}})]) : {ref}"
        )
