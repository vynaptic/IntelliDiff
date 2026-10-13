"""Inferred JSON Schemas of one side's raw samples, annotated for review (§8.6)."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from genson import SchemaBuilder
from genson.schema.strategies import Number

from deltasleuth.core.types import JSON
from deltasleuth.profiler.model import Path

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


class _DecimalNumber(Number):
    """genson's number strategy for ``Decimal``, every parsed JSON number (genson raises on it)."""

    PYTHON_TYPES = (int, float, Decimal)

    def add_object(self, obj: Any) -> None:
        super().add_object(obj)
        if isinstance(obj, Decimal) and obj != obj.to_integral_value():
            self._type = "number"


class _SchemaBuilder(SchemaBuilder):
    EXTRA_STRATEGIES = (_DecimalNumber,)


def _nodes(node: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    """Schema nodes that describe ``path``, looking through ``anyOf`` branches."""
    if not path:
        return [node]
    found = []
    for branch in [node, *node.get("anyOf", [])]:
        if path[0] is None:
            children = [branch.get("items"), branch.get("additionalProperties")]
        else:
            children = [branch.get("properties", {}).get(path[0])]
        found += [n for child in children if isinstance(child, dict) for n in _nodes(child, path[1:])]
    return found


def annotated_schema(
    docs: Iterable[JSON], maps: Iterable[Path], annotations: Mapping[Path, Mapping[str, Any]]
) -> dict[str, Any]:
    """JSON Schema of ``docs`` with ``maps`` as ``additionalProperties`` and ``x-ds-*`` annotations."""
    builder = _SchemaBuilder(schema_uri=DRAFT_2020_12)
    for doc in docs:
        builder.add_object(doc)
    schema = builder.to_schema()
    for path in sorted(maps, key=len):  # outermost first
        for node in _nodes(schema, path):
            for branch in [node, *node.get("anyOf", [])]:
                if branch.get("type") == "object" and "properties" in branch:
                    merged = _SchemaBuilder(schema_uri=None)
                    for sub in branch.pop("properties").values():
                        merged.add_schema(sub)
                    branch.pop("required", None)
                    branch.update({"additionalProperties": merged.to_schema(), "x-ds-map": True})
    for path, extra in annotations.items():
        for node in _nodes(schema, path):
            node.update(copy.deepcopy(extra))
    return schema
