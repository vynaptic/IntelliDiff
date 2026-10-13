"""A starter spec for two relations a table pre-check paired by name: both
``table`` blocks in the catalogue's spelling, the common primary key, the columns both sides hold of one
family, and a comment for each column left out, with the bridge a spec would declare, never applied (one
for a key part whose key texts are never equal, at the key).

It holds names and declared types only, and it is never stored: uploading it is ``POST /v1/specs``. A
catalogue's name is untrusted text: it reaches the body only as a YAML scalar the dumper writes, and a
comment only through ``json.dumps`` (ASCII), since YAML reads U+0085, U+2028 and U+2029 as line breaks and
``draft._comment`` folds ASCII white space only: a name could otherwise end its comment and add content.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from deltasleuth.core import precheck
from deltasleuth.core.canonical.normalisers import NORMALIZER_VERSION
from deltasleuth.core.spec import SpecIssue, TargetRegistry, validate_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.profiler.draft import DEFAULT_OWNER, SPEC_VERSION, render_yaml

TODO_KEY = "TODO_key_column"


@dataclass(frozen=True)
class TableDraft:
    """The draft as a document, as commented YAML, and its ``validate_spec`` issues."""

    spec: dict[str, Any]
    yaml_text: str
    issues: list[SpecIssue]


def _slug(text: str, fallback: str, pattern: str, joiner: str) -> str:
    """``text`` lower-cased with every run of other characters one ``joiner``: a spec's own name for a
    catalogue's, which may hold anything; ``fallback`` when nothing of it is left."""
    found = re.sub(pattern, joiner, text.lower()).strip(joiner)[:63]
    return found if found and found[0].isalpha() else fallback


def table_draft(pair: Mapping[str, Any], *, targets: TargetRegistry | None = None) -> TableDraft:
    """The draft of a pre-check's pair of two relations paired by name, read (its ``result`` holds their
    ``names``), with the issues ``validate_spec`` finds in it against ``targets``."""
    result = pair["result"]
    apart = [e for e in result["schema"] if e["group"] == "not_compared"]
    names, left = result["names"], [e for e in apart if e["code"] != "key_text_differs"]
    unread = {e["column"] for e in left}
    keys = [names[side]["keys"] for side in "ab"]
    parts = [_slug(column, "key", r"[^a-z0-9_]+", "_") for column in keys[0]]
    key: dict[str, Any] = {"name": "_".join(parts)[:128] or "key", "type": "string", "class": "identifier"}
    if len(parts) > 1:
        key |= {"type": "composite", "parts": parts}
    sides = {}
    for side, role in (("a", "baseline"), ("b", "candidate")):
        columns = names[side]["columns"]
        table = {
            "schema": names[side]["schema"], "name": names[side]["name"],
            "keyColumns": names[side]["keys"] or [TODO_KEY],
            "columns": [columns[c] for c in sorted(columns, key=str.encode) if c not in unread],
        }  # fmt: skip
        sides[side] = {"role": role, "target": pair[f"target_{side}"], "table": table}
    origin = f"table pre-check {pair['precheck_id']} pair {pair['n']}"
    spec = {
        "apiVersion": "deltasleuth/v1",
        "kind": "ComparisonSpec",
        "metadata": {
            "name": _slug(pair["label"], "table-draft", r"[^a-z0-9]+", "-"),
            "version": SPEC_VERSION,
            "owner": DEFAULT_OWNER,
            "description": f"Drafted from {origin} (paired by name); review it before approval.",
        },
        "normalizerVersion": NORMALIZER_VERSION,
        "key": key,
        "sides": sides,
        "compare": {},
        "recheck": {"delay": "5m", "times": 1, "outcomes": ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"]},
        "security": {"unlisted": "tokenize"},
    }
    plan = {side: {"types": {}, "columns": names[side]["columns"]} for side in "ab"}
    comments = {
        "key": [f"No common primary key: name a key column on each side for {TODO_KEY}."] * (not keys[0])
        + [json.dumps(precheck.clause(entry, plan)) for entry in apart if entry not in left],
        "sides": [
            "Columns left out, each as a pre-check said it (a bridge to declare is never applied):",
            *(f"  {json.dumps(precheck.clause(entry, plan))}" for entry in left),
        ]
        * bool(left),
        "security": ["Every column is tokenised in results until the spec lists it under clear."],
    }
    header = [
        f"DeltaSleuth ComparisonSpec DRAFT, from {origin}: {json.dumps(pair['label'])}, paired by name.",
        "A starter, not an approved spec: it holds names and declared types only. Review, edit, then upload.",
    ]
    text = render_yaml(spec, header, comments)
    issues = validate_spec(parse_yaml_text(text), targets=targets)
    if issues:
        header += ["", "Issues (validate_spec):"]
        header += [f"  - {json.dumps(f'{i.severity}: {i.pointer or "/"}: {i.message}')}" for i in issues]
        text = render_yaml(spec, header, comments)
    return TableDraft(spec, text, issues)
