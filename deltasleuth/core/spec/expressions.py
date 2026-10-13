"""Compile checks for the expression languages a spec embeds: CEL, JSONata and key regexes.

``CEL_VARIABLES`` is the single source of truth for the names each kind of CEL predicate may use:
the loader compiles with exactly these, and every engine evaluator binds all of them, so a
predicate that validated never meets an undeclared name at run time.
"""

from __future__ import annotations

import functools
import re
from typing import Literal

import jsonata
from cel_expr_python import cel
from jsonata.jexception import JException

CelKind = Literal[
    "known_diff",
    "value_filter",
    "not_found",
    "absence_shape",
    "signoff_stratum",
    "stratum_dimension",
    "sampling_stratum",
]

# Values that may be null or structured are DYN; always-string values are STRING so type errors
# surface at validation time.
CEL_VARIABLES: dict[CelKind, dict[str, str]] = {
    "known_diff": {
        "key": "STRING",
        "stratum": "DYN",
        "strata": "DYN",
        "path": "STRING",
        "pointer": "STRING",
        "change_type": "STRING",
        "direction": "STRING",
        "a": "DYN",
        "b": "DYN",
        "value_class": "DYN",
        "taxonomy_code": "DYN",
        "severity": "DYN",
        "link_reason": "DYN",
        "hop_count": "DYN",
        "attrs": "DYN",
    },
    "value_filter": {
        "key": "STRING",
        "side": "STRING",
        "path": "STRING",
        "pointer": "STRING",
        "value": "DYN",
        "stratum": "DYN",
        "strata": "DYN",
    },
    # The schema documents response.{status, headers, body}; the bare names are bound too.
    "not_found": {"response": "DYN", "status": "INT", "headers": "DYN", "body": "DYN"},
    "absence_shape": {"body": "DYN", "status": "INT", "headers": "DYN"},
    # A hash sample's frame row, and now, when the frame was read (docs/mvp/SAMPLING.md §2.1).
    "signoff_stratum": {
        "key": "STRING",
        "row": "DYN",
        "strata": "DYN",
        "riskTier": "STRING",
        "now": "TIMESTAMP",
    },
    "stratum_dimension": {"key": "STRING", "row": "DYN", "now": "TIMESTAMP"},
    "sampling_stratum": {"row": "DYN", "attrs": "DYN"},
}

# The result type each kind must produce (None = any); a DYN result is decided at run time.
_RESULT: dict[CelKind, str | None] = {
    "known_diff": "BOOL",
    "value_filter": "BOOL",
    "not_found": "BOOL",
    "absence_shape": "BOOL",
    "signoff_stratum": "STRING",
    "stratum_dimension": None,
    "sampling_stratum": None,
}


@functools.cache
def cel_environment(kind: CelKind) -> cel.Env:
    """The typed CEL environment of ``kind``: the loader checks, and evaluators compile, in it."""
    return cel.NewEnv(variables={name: getattr(cel.Type, t) for name, t in CEL_VARIABLES[kind].items()})


def cel_error(source: str, kind: CelKind) -> str | None:
    """Why ``source`` does not compile as a CEL predicate of ``kind``, or None."""
    try:
        compiled = cel_environment(kind).compile(source)
    except (RuntimeError, ValueError) as exc:
        line = str(exc).splitlines()[0].removeprefix("INVALID_ARGUMENT: ").removeprefix("ERROR: <input>:")
        return line.removesuffix(" [INVALID_ARGUMENT]").strip()
    wanted, produced = _RESULT[kind], compiled.return_type()
    if wanted is not None and produced not in (getattr(cel.Type, wanted), cel.Type.DYN):
        return f"must evaluate to {wanted.lower()}, but evaluates to {produced}"
    return None


def reads(source: str, kind: CelKind, name: str) -> bool:
    """True when the valid predicate ``source`` reads the variable ``name`` (it needs it to compile)."""
    variables = {n: getattr(cel.Type, t) for n, t in CEL_VARIABLES[kind].items() if n != name}
    try:
        cel.NewEnv(variables=variables).compile(source)
    except (RuntimeError, ValueError):
        return cel_error(source, kind) is None
    return False


def jsonata_error(source: str) -> str | None:
    """Why ``source`` does not parse as JSONata, or None."""
    try:
        jsonata.Jsonata(source)
    except JException as exc:
        return f"{exc.error}: {exc} (at character {exc.location})"
    except RecursionError:
        return "the expression nests too deeply"
    return None


def compile_key_pattern(pattern: str) -> re.Pattern[str]:
    """An ECMA-262 key pattern; ASCII mode gives ``\\d`` and ``\\w`` their ECMA-262 meaning."""
    return re.compile(pattern, re.ASCII)


def regex_error(pattern: str) -> str | None:
    """Why ``pattern`` does not compile as a key regex, or None."""
    try:
        compile_key_pattern(pattern)
    except (re.error, ValueError, OverflowError, RecursionError) as exc:  # ValueError: (?u) against ASCII
        return f"invalid regular expression: {exc}"
    return None
