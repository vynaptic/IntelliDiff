"""Spec loading, validation (schema + loader rules) and compilation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deltasleuth.core.spec.assemble import build_compiled_spec
from deltasleuth.core.spec.documents import read_document
from deltasleuth.core.spec.errors import SpecError, SpecIssue, sort_issues
from deltasleuth.core.spec.rules import check_rules
from deltasleuth.core.spec.schemas import spec_schema_issues
from deltasleuth.core.spec.targets import TargetRegistry
from deltasleuth.core.types import CompiledSpec


def load_spec_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """A spec document: YAML (safe, YAML 1.2 scalars, no duplicate keys, capped aliases) or JSON."""
    return read_document(Path(path))


def validate_spec(doc: Any, *, targets: TargetRegistry | None = None) -> list[SpecIssue]:
    """Every problem with ``doc``, errors and warnings, ordered by location.

    ``targets`` enables the rules for running the spec: the registry rules (unknown targets, X2
    notFound, X33 truncation) and the normaliser version, which must be this build's.
    """
    if not isinstance(doc, dict):
        return [SpecIssue("", "the spec document must be a mapping", "document_not_mapping")]
    issues = spec_schema_issues(doc) + check_rules(doc, targets=targets)
    return sort_issues(issues)


def compile_spec(doc: Any, *, targets: TargetRegistry | None = None) -> CompiledSpec:
    """Validate and build the ``CompiledSpec``; raises ``SpecError`` listing every error."""
    issues = validate_spec(doc, targets=targets)
    blocking = [i for i in issues if i.severity == "error"]
    if blocking:
        raise SpecError(blocking)
    return build_compiled_spec(doc)
