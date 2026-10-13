"""Spec bootstrap (§8.6): paired samples in, a draft comparison spec and its evidence out.

Black-box by construction: the profiler sees two sets of JSON documents and nothing else. Side a
is the baseline, so canonical names are side a's: side a gets a ``select`` only, side b a
``select`` plus a JSONata ``map`` onto side a's shape. Nothing is approved: every suggestion has
an evidence row, and evidence and YAML hold paths, counts and shapes, never values.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import textwrap
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby
from typing import Any

import yaml

from deltasleuth.core.canonical.normalisers import NORMALIZER_VERSION
from deltasleuth.core.types import JSON, Side
from deltasleuth.profiler.align import Alignment, align
from deltasleuth.profiler.keys import MAX_NESTED_MULTISETS, ArrayDecision, suggest_arrays
from deltasleuth.profiler.model import ROOT, DraftSpec, EvidenceRow, Path, ProfilerError, node_at, render
from deltasleuth.profiler.profile import LEAF_KINDS, MAP, SideProfile, profile_side
from deltasleuth.profiler.schema import annotated_schema
from deltasleuth.profiler.sidemap import side_b_map
from deltasleuth.profiler.suggest import (
    NormaliserSuggestion,
    SelectDecision,
    chain_text,
    detect_select,
    suggest_normalisers,
)

SPEC_VERSION = "0.1.0"
DEFAULT_OWNER = "unassigned"
MIN_PAIRS = 20  # the design assumes 200-400; fewer still drafts, with a note

_SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_CANONICAL_FIELD = re.compile(r"[a-z_][a-z0-9_]*")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_INTEGER_KEY = re.compile(r"0|[1-9][0-9]{0,17}")
_KEY_CLASSES = {"U": "[A-Z]", "L": "[a-z]", "D": "[0-9]"}
# Escapes valid both in Python and in ECMA-262 unicode mode, where "\-" outside a class is an error.
_SYNTAX_CHARS = frozenset("\\^$.|?*+()[]{}/")
_CLASS_SPECIAL = frozenset("\\]^-[")

Pair = tuple[str, JSON | None, JSON | None]


def _check_args(
    spec_name: str, target_a: str, target_b: str, key_name: str, key_pattern: str | None, owner: str
) -> None:
    for label, value in (("spec_name", spec_name), ("target_a", target_a), ("target_b", target_b)):
        if not (_SLUG.fullmatch(value) and len(value) <= 63):
            raise ProfilerError(f"{label} must be lower-case kebab-case (at most 63 characters)")
    if not (_CANONICAL_FIELD.fullmatch(key_name) and len(key_name) <= 128):
        raise ProfilerError("key_name must be lower snake case ([a-z_][a-z0-9_]*)")
    if not owner.strip() or len(owner) > 200:
        raise ProfilerError("owner must be 1-200 characters")
    if key_pattern is not None:
        try:
            re.compile(key_pattern)
        except re.error as exc:
            raise ProfilerError(f"key_pattern does not compile: {exc.msg}") from exc


def _exact_numbers(value: JSON) -> JSON:
    """Floats (from callers that parsed without jsonio) become exact Decimals."""
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {k: _exact_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_exact_numbers(v) for v in value]
    return value


def _char_class(ch: str) -> str:
    return "U" if "A" <= ch <= "Z" else "L" if "a" <= ch <= "z" else "D" if "0" <= ch <= "9" else ch


def _quantifier(lo: int, hi: int) -> str:
    return f"{{{lo},{hi}}}" if lo != hi else "" if lo == 1 else f"{{{lo}}}"


def infer_key_pattern(keys: Sequence[str]) -> str:
    """An anchored pattern of the keys' character classes (``C00012345`` -> ``^[A-Z][0-9]{8}$``).

    Keys sharing one class sequence get a positional pattern, others one character class of
    everything observed, bounded by the observed lengths.
    """
    runs = [[(cls, len(list(group))) for cls, group in groupby(key, _char_class)] for key in keys]
    shape = [cls for cls, _ in runs[0]]
    if all([cls for cls, _ in r] == shape for r in runs):
        parts = []
        for i, cls in enumerate(shape):
            lengths = [r[i][1] for r in runs]
            literal = "\\" + cls if cls in _SYNTAX_CHARS else cls
            parts.append(_KEY_CLASSES.get(cls, literal) + _quantifier(min(lengths), max(lengths)))
        return "^" + "".join(parts) + "$"
    chars = {c for k in keys for c in k}
    ranges = [
        rng
        for rng, test in (("A-Z", str.isupper), ("a-z", str.islower), ("0-9", str.isdigit))
        if any(c.isascii() and c.isalnum() and test(c) for c in chars)
    ]
    others = sorted(c for c in chars if not (c.isascii() and c.isalnum()))
    extra = "".join("\\" + c if c in _CLASS_SPECIAL else c for c in others)
    lengths = [len(k) for k in keys]
    return f"^[{''.join(ranges)}{extra}]{_quantifier(min(lengths), max(lengths)) or '{1}'}$"


def infer_key_type(keys: Sequence[str]) -> str:
    if all(_UUID.fullmatch(k) for k in keys):
        return "uuid"
    return "integer" if all(_INTEGER_KEY.fullmatch(k) for k in keys) else "string"


class _FlowList(list):  # type: ignore[type-arg]
    """A scalar list written inline (``[sortCode, accountNo]``)."""


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


_Dumper.add_representer(
    str,
    lambda dumper, text: dumper.represent_scalar(
        "tag:yaml.org,2002:str", text, style="|" if "\n" in text else None
    ),
)
_Dumper.add_representer(
    _FlowList,
    lambda dumper, items: dumper.represent_sequence("tag:yaml.org,2002:seq", items, flow_style=True),
)


def _flowify(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _flowify(v) for k, v in value.items()}
    if isinstance(value, list):
        if value and all(isinstance(v, str | int) for v in value):
            return _FlowList(value)
        return [_flowify(v) for v in value]
    return value


def _comment(lines: Sequence[str]) -> str:
    wrapped = (w for line in lines for w in textwrap.wrap(line, 96, subsequent_indent="    ") or [""])
    return "\n".join(f"# {w}".rstrip() for w in wrapped)


def render_yaml(spec: Mapping[str, Any], header: Sequence[str], comments: Mapping[str, Sequence[str]]) -> str:
    """YAML text of ``spec`` in its own key order, with a header and per-section comments."""
    parts = [_comment(header)]
    for name, value in spec.items():
        block = yaml.dump(
            {name: _flowify(value)},
            Dumper=_Dumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=100,
        )
        lead = _comment(comments[name]) + "\n" if comments.get(name) else ""
        parts.append(lead + block.rstrip("\n"))
    return "\n\n".join(parts) + "\n"


@dataclass
class _SideRun:
    side: Side
    raw: list[tuple[int, JSON]]
    select: SelectDecision
    docs: list[tuple[int, JSON]]  # selected object documents
    profile: SideProfile

    @classmethod
    def of(cls, side: Side, pairs: Sequence[Pair]) -> _SideRun:
        column = [pair[1 if side is Side.A else 2] for pair in pairs]
        raw = [(i, doc) for i, doc in enumerate(column) if doc is not None]
        if not raw:
            raise ProfilerError(f"side {side.value} has no samples")
        select = detect_select([doc for _, doc in raw])
        selected = [(i, node_at(doc, select.path)) for i, doc in raw]
        docs = [(i, node) for i, node in selected if isinstance(node, dict)]
        if not docs:
            raise ProfilerError(f"unsupported: side {side.value} documents are not JSON objects after select")
        profile = profile_side(side, docs)
        if profile.kind(ROOT) == MAP:
            raise ProfilerError(
                f"unsupported: side {side.value} documents are maps (id-like member names) after select"
            )
        return cls(side, raw, select, docs, profile)

    @property
    def note(self) -> str | None:
        lacking = len(self.raw) - len(self.docs)
        return (
            f"Side {self.side.value}: {lacking} of {len(self.raw)} samples lack an object at "
            f"{render(self.select.path)} and were not profiled (at compare time they are unparseable)."
            if lacking
            else None
        )

    def schema(self, annotations: Mapping[Path, dict[str, Any]]) -> dict[str, Any]:
        """The raw documents' annotated JSON Schema (profile paths sit below the select root)."""
        prefix = self.select.path
        maps = [(*prefix, *p) for p, st in self.profile.stats.items() if st.kind == MAP]
        return annotated_schema(
            [doc for _, doc in self.raw], maps, {(*prefix, *p): extra for p, extra in annotations.items()}
        )

    def select_row(self) -> EvidenceRow:
        rendered, dropped = render(self.select.path), ", ".join(self.select.dropped)
        paths = (rendered, None) if self.side is Side.A else (None, rendered)
        scores = {"selected_docs": len(self.docs), "leaf_share": self.select.leaf_share}
        return EvidenceRow(
            "select",
            *paths,
            rendered,
            scores,
            len(self.raw),
            self.select.reason + (f"; not compared: {dropped}" if dropped else ""),
        )


def _key_block(
    pairs: Sequence[Pair], key_name: str, key_pattern: str | None
) -> tuple[dict[str, Any], EvidenceRow, list[str]]:
    keys = sorted(dict.fromkeys(k for k, _, _ in pairs))
    pattern = key_pattern if key_pattern is not None else infer_key_pattern(keys)
    matching = [k for k in keys if re.search(pattern, k)]
    if not matching:
        raise ProfilerError("no sampled key matches key_pattern")
    notes = (
        [
            f"{len(keys) - len(matching)} sampled keys do not match key.pattern and were left out of "
            "key.source."
        ]
        if len(matching) < len(keys)
        else []
    )
    notes += [
        f"key.source lists the {len(matching)} sampled keys; replace it with the run's key source for "
        "a full comparison.",
        "key.class is identifier (fail-closed: keys are tokenised in results); set business_code if "
        "keys are not personal data.",
    ]
    block = {
        "name": key_name,
        "type": infer_key_type(matching),
        "pattern": pattern,
        "class": "identifier",
        "source": {"kind": "list", "keys": matching},
    }
    scores = {
        "keys": len(keys),
        "matching": len(matching),
        "type": block["type"],
        "inferred": key_pattern is None,
    }
    origin = "derived from the keys' character classes" if key_pattern is None else "given"
    row = EvidenceRow("key_pattern", None, None, pattern, scores, len(keys), origin)
    return block, row, notes


def _side_block(
    run: _SideRun, target: str, request: Mapping[str, Any] | None, side_map: str | None
) -> dict[str, Any]:
    request = request or {"method": "GET", "path": "/{{ key }}"}
    block: dict[str, Any] = {
        "role": "baseline" if run.side is Side.A else "candidate",
        "target": target,
        "request": copy.deepcopy(dict(request)),
        "select": render(run.select.path),
    }
    if side_map is not None:
        block["map"] = side_map
    block["notFound"] = {"status": [404]}
    return block


def _compare_block(
    decisions: Mapping[Path, ArrayDecision], suggestions: Sequence[NormaliserSuggestion]
) -> dict[str, Any]:
    arrays = [
        {"path": render(d.path_a), "mode": d.mode, **({"key": list(d.key)} if d.key else {})}
        for d in decisions.values()
        if d.mode in ("keyed", "multiset")
    ]
    fields = [
        {"path": render(s.path_a), "normalize": list(s.chain), "note": f"ds-profiler: {s.note}"}
        for s in suggestions
    ]
    return {name: rules for name, rules in (("arrays", arrays), ("fields", fields)) if rules}


def _annotations(
    pa: SideProfile,
    alignment: Alignment,
    decisions: Mapping[Path, ArrayDecision],
    suggestions: Sequence[NormaliserSuggestion],
) -> dict[Side, dict[Path, dict[str, Any]]]:
    """``x-ds-*`` annotations per side, keyed by path below the select root."""
    a: dict[Path, dict[str, Any]] = defaultdict(dict)
    b: dict[Path, dict[str, Any]] = defaultdict(dict)
    for path_a, path_b in alignment.matches.items():
        if pa.kind(path_a) in LEAF_KINDS:
            a[path_a]["x-ds-align"] = b[path_b]["x-ds-align"] = render(path_a)
    for d in decisions.values():
        a[d.path_a].update({"x-ds-array-mode": d.mode, **({"x-ds-array-keys": list(d.key)} if d.key else {})})
        if d.path_b is not None:
            keys_b = {"x-ds-array-keys": [".".join(k) for k in d.key_b]} if d.key_b else {}
            b[d.path_b].update({"x-ds-array-mode": d.mode, **keys_b})
    for s in suggestions:
        a[s.path_a]["x-ds-normalize"] = b[s.path_b]["x-ds-normalize"] = list(s.chain)
    return {Side.A: a, Side.B: b}


def _notes(
    runs: Mapping[Side, _SideRun], alignment: Alignment, requests_given: bool, groups: Sequence[Sequence[int]]
) -> list[str]:
    """Review notes about the samples and what the draft leaves to the reviewer."""
    common, unmatched_a, unmatched_b = (
        len(alignment.common_docs),
        len(alignment.unmatched_a),
        len(alignment.unmatched_b),
    )
    notes = []
    if common < MIN_PAIRS:
        notes.append(
            f"Only {common} pairs have samples on both sides; suggestions are weak (the design "
            "assumes 200-400)."
        )
    notes += [
        f"Side {run.side.value} selects {render(run.select.path)}; envelope members "
        f"{', '.join(run.select.dropped)} are not compared."
        for run in runs.values()
        if run.select.dropped
    ]
    if not requests_given:
        notes.append(
            "request templates are placeholders (the profiler never sees them): set each side's "
            "request before a live run; file-sample runs do not use them."
        )
    notes += [
        "truncation is left out: declare sides.<side>.truncation or the target's truncation_indicators "
        "before a live run (loader rule X33).",
        "notFound [404] must be declared in each target's authoritative_absence (loader rule X2).",
    ]
    if unmatched_a:
        notes.append(
            f"unmatched[a] lists {unmatched_a} side-a path{'s' * (unmatched_a != 1)} with no side-b "
            "counterpart: they surface as missing_in_b until mapped or excluded after review."
        )
    if unmatched_b:
        notes.append(
            f"unmatched[b] lists {unmatched_b} side-b path{'s' * (unmatched_b != 1)} with no side-a "
            "counterpart: they are not compared."
        )
    notes.append(
        "Mappings that need logic (a value lookup such as A -> ACTIVE, a scalar that side b keeps "
        "in a filtered array) are not inferred; unmatched paths show where to add them."
    )
    if not groups:
        notes.append("Key stability (w4) was not scored: the samples hold no repeated reads of a key.")
    return notes


def _comments(
    decisions: Mapping[Path, ArrayDecision], suggestions: Sequence[NormaliserSuggestion], select_b: Path
) -> dict[str, list[str]]:
    arrays = [
        f"{render(d.path_a)}: {d.mode}" + (f" [{', '.join(d.key)}]" if d.key else "") + f" ({d.reason})"
        for d in decisions.values()
    ]
    fields = [
        f"{render(s.path_a)}: {chain_text(s.chain)} ({s.differing} of {s.compared} pairs differ)"
        for s in suggestions
    ]
    return {
        "key": ["Key drafted from the samples: review type, pattern and class; source lists sampled keys."],
        "sides": [
            "Side a is the baseline and keeps its own names: select only, no map (canonical shape = side a).",
            f"Side b maps its aligned paths onto side a's names (select {render(select_b)}).",
            "Request templates, notFound and truncation need confirming against each target's contract.",
        ],
        "compare": [
            "Array modes and keys (evidence rows kind=key_candidate / array):",
            *arrays,
            "Normalisers (evidence rows kind=normalize):",
            *fields,
        ],
        "recheck": ["Required by the schema; a conservative default to tune after calibration."],
    }


def _document(
    spec_name: str,
    owner: str,
    samples: int,
    key: dict[str, Any],
    sides: dict[str, Any],
    compare: dict[str, Any],
) -> dict[str, Any]:
    """The spec document, members in the schema's reading order."""
    return {
        "apiVersion": "deltasleuth/v1",
        "kind": "ComparisonSpec",
        "metadata": {
            "name": spec_name,
            "version": SPEC_VERSION,
            "owner": owner,
            "description": f"Drafted by ds-profiler from {samples} paired samples; "
            "review the evidence table before approval.",
        },
        "normalizerVersion": NORMALIZER_VERSION,
        "canonical": {"mode": "tree"},
        "key": key,
        "sides": sides,
        "compare": compare,
        "recheck": {
            "delay": "5m",
            "times": 1,
            "outcomes": ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B", "INCONCLUSIVE"],
        },
    }


def _header(
    samples: int,
    runs: Mapping[Side, _SideRun],
    common: int,
    schemas: Mapping[Side, dict[str, Any]],
    notes: Sequence[str],
) -> list[str]:
    bundle = json.dumps(
        {side.value: schema for side, schema in schemas.items()}, sort_keys=True, separators=(",", ":")
    )
    return [
        "DeltaSleuth ComparisonSpec DRAFT, drafted by ds-profiler (docs/architecture.md section 8.6).",
        "A suggestion, not an approved spec: review the evidence table, edit, then approve.",
        f"Samples: {samples} pairs; side a {len(runs[Side.A].docs)} documents, side b "
        f"{len(runs[Side.B].docs)}, both {common}.",
        "Annotated schema bundle (DraftSpec.annotated_schemas), digest:",
        f"  sha256:{hashlib.sha256(bundle.encode()).hexdigest()}",
        "",
        "Review notes:",
        *[f"  - {n}" for n in notes],
    ]


def _repeat_groups(pairs: Sequence[Pair]) -> list[list[int]]:
    """Sample indexes of each key read more than once."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, (key, _, _) in enumerate(pairs):
        groups[key].append(i)
    return [g for g in groups.values() if len(g) > 1]


def bootstrap(
    pairs: Sequence[Pair],
    *,
    spec_name: str,
    target_a: str,
    target_b: str,
    key_name: str,
    key_pattern: str | None = None,
    owner: str = DEFAULT_OWNER,
    requests: Mapping[Side, Mapping[str, Any]] | None = None,
) -> DraftSpec:
    """Draft a comparison spec from ``(key, side-a document, side-b document)`` samples.

    Documents are parsed JSON (numbers as ``Decimal``, see ``jsonio.parse_json``); ``None`` marks
    a side without a document for that key. ``requests`` supplies each side's request template;
    without it the draft carries a placeholder and a note.
    """
    _check_args(spec_name, target_a, target_b, key_name, key_pattern, owner)
    samples = [(str(k), _exact_numbers(a), _exact_numbers(b)) for k, a, b in pairs]
    if not samples:
        raise ProfilerError("no samples")
    runs = {side: _SideRun.of(side, samples) for side in Side}
    pa, pb = runs[Side.A].profile, runs[Side.B].profile
    if not pa.doc_ids & pb.doc_ids:
        raise ProfilerError("no pair has an object document on both sides after select")
    alignment = align(pa, pb)
    groups = _repeat_groups(samples)
    decisions, array_rows = suggest_arrays(pa, pb, alignment, groups)
    suggestions, norm_rows, norm_notes = suggest_normalisers(pa, pb, alignment)
    key, key_row, key_notes = _key_block(samples, key_name, key_pattern)
    side_map = side_b_map(pa, alignment)
    requests = requests or {}
    sides = {
        "a": _side_block(runs[Side.A], target_a, requests.get(Side.A), None),
        "b": _side_block(runs[Side.B], target_b, requests.get(Side.B), side_map or "{}"),
    }
    spec = _document(spec_name, owner, len(samples), key, sides, _compare_block(decisions, suggestions))
    annotations = _annotations(pa, alignment, decisions, suggestions)
    schemas = {side: run.schema(annotations[side]) for side, run in runs.items()}
    capped = [render(d.path_a) for d in decisions.values() if d.nesting_capped]
    notes = [
        *_notes(runs, alignment, bool(requests), groups),
        *(run.note for run in runs.values() if run.note),
        *(
            [
                f"{', '.join(capped)}: would be multisets, but are nested in {MAX_NESTED_MULTISETS} "
                "multiset arrays already, so left ordered (each multiset level multiplies the size of "
                "member identities); key them or choose their mode after review."
            ]
            if capped
            else []
        ),
        *key_notes,
        *(
            []
            if side_map
            else ["No side-b path aligned with side a; the side-b map emits an empty document."]
        ),
        *norm_notes,
    ]
    evidence = [
        runs[Side.A].select_row(),
        runs[Side.B].select_row(),
        key_row,
        *pa.evidence(),
        *pb.evidence(),
        *alignment.rows,
        *array_rows,
        *norm_rows,
    ]
    header = _header(len(samples), runs, len(alignment.common_docs), schemas, notes)
    yaml_text = render_yaml(spec, header, _comments(decisions, suggestions, runs[Side.B].select.path))
    unmatched = {
        Side.A: [render(p) for p in alignment.unmatched_a],
        Side.B: [render(p) for p in alignment.unmatched_b],
    }
    return DraftSpec(spec, yaml_text, evidence, schemas, unmatched, notes)
