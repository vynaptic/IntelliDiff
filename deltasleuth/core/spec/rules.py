"""Loader rules: the checks JSON Schema cannot express.

They run on whatever parts of the document are well-typed (the schema reports the rest), so one
pass reports every problem. A feature the product does not run yet is an ``unsupported`` error; one
it accepts without applying is an ``ignored`` warning. Nothing a spec declares is ignored silently.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from deltasleuth.core.canonical.errors import PathError
from deltasleuth.core.canonical.normalisers import NORMALIZER_VERSION
from deltasleuth.core.canonical.paths import jsonpath_error, normalise_pattern
from deltasleuth.core.spec import where
from deltasleuth.core.spec.durations import parse_duration
from deltasleuth.core.spec.errors import IGNORED, UNSUPPORTED, SpecIssue, pointer
from deltasleuth.core.spec.expressions import (
    CelKind,
    cel_error,
    compile_key_pattern,
    jsonata_error,
    reads,
    regex_error,
)
from deltasleuth.core.spec.keys import KEY_NORMALISERS, fit_parts, key_matches, normalise_key, parts_match
from deltasleuth.core.spec.schemas import SLUG, core_schema, pack_config_issues, pack_major, pack_schema
from deltasleuth.core.spec.targets import TargetRegistry

Parts = tuple[str | int, ...]
# Patterns are slash-separated member names; "*" matches every member or array item.
_QUERY_PATHS = """
    sides/*/select sides/*/truncation/countPath sides/*/truncation/itemsPath sides/*/truncation/flagPaths/*
    sides/*/pagination/itemsPath sides/*/pagination/cursorPath time/watermark/a time/watermark/b
""".split()
_KEY_SOURCE_QUERY_PATHS = "keyPath strataFields/* pagination/itemsPath pagination/cursorPath".split()
_RULE_PATHS = """
    compare/arrays/*/path compare/fields/*/path compare/include/* compare/exclude/*
    security/tokenize/*/path security/clear/* sides/*/rawClear/* noise/masks/*/path
""".split()
_CEL_PATHS = {
    "knownDiffs/*/when": "known_diff",
    "compare/valueFilter": "value_filter",
    "sides/*/notFound/when": "not_found",
    "sampling/strata/*/cel": "sampling_stratum",
}
_KEY_SOURCE_CEL_PATHS = {"signoffStratum": "signoff_stratum", "strata/*/cel": "stratum_dimension"}
# String literals compared with the ``path`` variable of a predicate.
_PATH_LITERALS = (
    re.compile(r"""(?<![\w.])path\s*(?:==|!=)\s*(['"])([^'"\\]*)\1"""),
    re.compile(r"""(['"])([^'"\\]*)\1\s*(?:==|!=)\s*path(?![\w.(])"""),
)
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
# A frame column a strata expression reads (row.<name>), found once its string literals are blanked.
_ROW_FIELD = re.compile(r"(?<![\w.])row\.([A-Za-z_]\w*)")
_CEL_STRING = re.compile(r"'(?:[^'\\]|\\.)*'" r'|"(?:[^"\\]|\\.)*"')
RENDERED_PLACEHOLDERS = frozenset({"key", "run_id", "call_seq"})  # what the connectors render
# (pattern, the presence its code needs, the change type it labels): a field rule relabels value
# changes only; an array rule labels its one-sided members.
_TAXONOMY_RULES = (
    ("compare/fields/*/taxonomyCode", "none", "value_changed"),
    ("compare/arrays/*/taxonomy/missing_in_b", "missing", "missing_in_b"),
    ("compare/arrays/*/taxonomy/extra_in_b", "extra", "extra_in_b"),
)
_DEFERRED = {
    "noise/calibration": "noise calibration",
    "noise/masks": "noise masks",
    "identity/crosswalk": "an identity crosswalk",
}
# Table-side members the product does not run yet.
_TABLE_DEFERRED = {
    "template": "an SQL template side",
    "session": "session settings; every session is read-only and renders values in UTC",
    "collation": "collation facts",
}
# Accepted but not applied; not applying them never turns a difference into a MATCH.
_IGNORED = {
    "sampling/strata": "no strata are computed (stratum is null and strata empty in predicates)",
    "reporting/formats": "reports have a fixed set of outputs",
    "reporting/fields": "reports have fixed columns",
    "reporting/interim": "interim reports are not configurable",
    "inferredSchema": "the inferred-schema guard for service sides is not built; schemaDrift acts on table"
    " sides only",
    "sides/*/rawClear": "there is no raw-body view in this release; outputs show canonical documents only",
    "compare/strategy": "two tables are always scanned by row hash, then read key by key",
}
_CREDENTIAL_VALUE = re.compile(
    r"^\s*(?:bearer|basic|digest|negotiate|ntlm|token|aws4-hmac-sha256)\s+\S"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.",  # a JSON Web Token
    re.IGNORECASE,
)
# Header-name fragments (lower case, without - and _) that mean the header carries a credential.
_CREDENTIAL_HEADER = re.compile(
    "authorization|cookie|secret|password|passwd|apikey|accesstoken|authtoken|sessiontoken|idtoken"
    "|refreshtoken|bearer|credential"
)


def check_rules(doc: dict[str, Any], *, targets: TargetRegistry | None) -> list[SpecIssue]:
    issues = [
        *_expressions(doc),
        *_path_literals(doc),
        *_unique_rules(doc),
        *_requests(doc),
        *_inline_keys(doc),
        *_key_pattern(doc),
        *_domain_pack(doc),
        *_taxonomy_codes(doc),
        *_not_found_2xx(doc),
        *_tables(doc),
        *_graph(doc),
        *_gating(doc),
        *_sampling(doc),
    ]
    if targets is not None:  # the spec is about to run, so it must run as written
        issues.extend(_targets(doc, targets))
        if isinstance(version := doc.get("normalizerVersion"), str) and version != NORMALIZER_VERSION:
            message = f"normalisers {version}; this build implements {NORMALIZER_VERSION}"
            issues.append(_error("normalizerVersion", UNSUPPORTED, f"not supported: {message}"))
    return issues


def _error(at: Parts | str, code: str, message: str) -> SpecIssue:
    return SpecIssue(pointer(at.split("/") if isinstance(at, str) else at), message, code)


def _warning(at: Parts | str, code: str, message: str) -> SpecIssue:
    return SpecIssue(pointer(at.split("/") if isinstance(at, str) else at), message, code, "warning")


# -- expressions ----------------------------------------------------------------------------------


def _expressions(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    for at, path in [*_strings(doc, *_QUERY_PATHS), *_key_source_strings(doc, *_KEY_SOURCE_QUERY_PATHS)]:
        if path.startswith("$") and (problem := jsonpath_error(path)):  # else a schema pattern error
            yield _error(at, "invalid_jsonpath", problem)
    for at, path in _strings(doc, *_RULE_PATHS):
        try:
            if path.startswith("$"):
                normalise_pattern(path)
        except PathError as exc:
            yield _error(at, "invalid_jsonpath" if jsonpath_error(path) else "path_not_pattern", str(exc))
    for at, source in _strings(doc, "sides/*/map"):
        if problem := jsonata_error(source):
            yield _error(at, "invalid_jsonata", problem)
    predicates = [(kind, found) for pattern, kind in _CEL_PATHS.items() for found in _strings(doc, pattern)]
    for pattern, kind in _KEY_SOURCE_CEL_PATHS.items():
        predicates += [(kind, found) for found in _key_source_strings(doc, pattern)]
    for kind, (at, source) in predicates:
        if problem := cel_error(source, kind):  # type: ignore[arg-type]
            yield _error(at, "invalid_cel", problem)
    for at, pattern in _strings(doc, "key/pattern"):
        if problem := regex_error(pattern):
            yield _error(at, "invalid_key_pattern", problem)


def _path_literals(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """Warn when a predicate compares ``path`` with a literal no canonical pattern can equal."""
    for at, source in _strings(doc, "knownDiffs/*/when", "compare/valueFilter"):
        for literal in (match.group(2) for regex in _PATH_LITERALS for match in regex.finditer(source)):
            canonical = _pattern(literal)
            if canonical != literal:
                expected = f"write {canonical!r}" if canonical else "it is not a path pattern"
                message = f"path is compared with {literal!r}, which never equals a path pattern ({expected})"
                yield _warning(at, "path_literal_not_canonical", message)


# -- rule uniqueness ------------------------------------------------------------------------------


def _unique_rules(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    yield from _duplicates(doc, "compare/arrays/*/path", "duplicate_array_path", "an array rule", {})
    yield from _duplicates(doc, "compare/fields/*/path", "duplicate_field_path", "a field rule", {})
    for at, rule in _find(doc, "compare/arrays/*"):
        fields = rule.get("key") if isinstance(rule, dict) else None
        if isinstance(fields, list) and len(set(map(str, fields))) != len(fields):
            yield _error((*at, "key"), "duplicate_array_key", "the member key names a field twice")
    parts = _key_parts(doc)  # each names a placeholder
    if len(set(map(str, parts))) != len(parts):
        yield _error("key/parts", "invalid_key", "key.parts names a part twice")
    ids: dict[str, Parts] = {}
    for at, rule_id in _strings(doc, "knownDiffs/*/id"):
        if rule_id in ids:
            yield _error(
                at, "duplicate_known_diff_id", f"rule id {rule_id} is already used at {pointer(ids[rule_id])}"
            )
        ids.setdefault(rule_id, at)
    tokenised: dict[str, Parts] = {}
    clear: dict[str, Parts] = {}
    yield from _duplicates(
        doc, "security/tokenize/*/path", "duplicate_security_path", "a tokenize entry", tokenised
    )
    yield from _duplicates(doc, "security/clear/*", "duplicate_security_path", "a clear entry", clear)
    for path, at in clear.items():
        if path in tokenised:
            yield _error(at, "security_path_conflict", f"{path} is both tokenised and clear")
    if _get(doc, "security/unlisted") == "clear":  # the runner gates it
        yield _warning(
            "security/unlisted",
            "unapproved_clear",
            "under SSO a run needs a four-eyes approval of unlisted_clear",
        )


def _duplicates(doc: Any, pattern: str, code: str, noun: str, seen: dict[str, Parts]) -> Iterator[SpecIssue]:
    """Rule paths that repeat after normalisation; fills ``seen`` with each first occurrence."""
    for at, path in _strings(doc, pattern):
        canonical = _pattern(path)
        if canonical in seen:
            yield _error(at, code, f"{noun} for {canonical} already exists at {pointer(seen[canonical])}")
        elif canonical is not None:
            seen[canonical] = at


# -- requests -------------------------------------------------------------------------------------


def _requests(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """No credentials in request templates (they come from the target's secret_ref); known placeholders."""
    headers_schema = core_schema()["$defs"]["request"]["properties"]["headers"]
    schema_forbidden = re.compile(headers_schema["propertyNames"]["not"]["pattern"])  # the schema reports it
    requests = [*_side_requests(doc)]
    requests += [
        ((*at, "request"), s["request"]) for at, s in _key_sources(doc) if isinstance(s.get("request"), dict)
    ]
    for base, request in requests:
        headers = request.get("headers")
        for name in headers if isinstance(headers, dict) else ():
            compact = name.lower().replace("-", "").replace("_", "")
            if not schema_forbidden.search(name) and _CREDENTIAL_HEADER.search(compact):
                message = "this header carries credentials; they come only from the target's secret_ref"
                yield _error((*base, "headers", name), "credential_header", message)
        for at, text in _template_strings(request, base):
            if at[len(base)] != "path" and _CREDENTIAL_VALUE.search(text):
                message = "the value looks like an inline credential; credentials come only from secret_ref"
                yield _error(at, "inline_credential", message)
            for name in _placeholders(text):
                if not _known_placeholder(doc, name):
                    allowed = "key, keyParts.<part>, run_id, cycle_no, call_seq"
                    yield _error(
                        at,
                        "unknown_placeholder",
                        f"unknown placeholder {{{{ {name} }}}} (allowed: {allowed})",
                    )


def _known_placeholder(doc: dict[str, Any], name: str) -> bool:
    if name in ("key", "run_id", "cycle_no", "call_seq"):
        return True
    parts = _get(doc, "key/parts")
    return (
        name.startswith("keyParts.") and isinstance(parts, list) and name.removeprefix("keyParts.") in parts
    )


def _template_strings(request: dict[str, Any], base: Parts) -> Iterator[tuple[Parts, str]]:
    """Every rendered string of a request template: path, header and query values, body leaves."""
    for member in ("path", "headers", "query", "body"):
        yield from _string_leaves(request.get(member), (*base, member))


def _string_leaves(node: Any, base: Parts) -> Iterator[tuple[Parts, str]]:
    if isinstance(node, str):
        yield base, node
    for name, child in _children(node):
        yield from _string_leaves(child, (*base, name))


def _placeholders(text: str) -> Iterator[str]:
    return (match.group(1) for match in _PLACEHOLDER.finditer(text))


# -- keys -----------------------------------------------------------------------------------------


def _inline_keys(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """Inline keys must be requestable after ``key.normalize`` and satisfy ``key.pattern``, part by part
    when the key has several (``_key_parts``). Keys of a class other than ``business_code`` are flagged:
    stored in clear with the spec, they are never purged."""
    value_class = _get(doc, "key/class")
    stored = isinstance(value_class, str) and value_class != "business_code"
    for base, source in _key_sources(doc):
        if stored and source.get("kind") == "list" and source.get("keys"):
            message = (
                f"inline keys of class {value_class} are stored in clear with the spec and never purged;"
                " pass them with 'ds run --keys'"
            )
            yield _warning((*base, "keys"), "inline_keys_stored", message)
    chain, pattern = _get(doc, "key/normalize") or [], _get(doc, "key/pattern")
    if not isinstance(chain, list) or not all(isinstance(n, str) and n in KEY_NORMALISERS for n in chain):
        return  # the schema reports the chain; keys cannot be normalised faithfully
    valid = isinstance(pattern, str) and not regex_error(pattern)
    compiled = compile_key_pattern(pattern if valid else "")  # an invalid pattern is reported on its own
    n = len(_key_parts(doc)) or 1
    found = "the key is" if n == 1 else f"the key is not {n} parts joined by |, or a part is"
    for base, source in _key_sources(doc):
        keys = source.get("keys") if source.get("kind") == "list" else None
        seen: set[str | None] = set()
        for i, raw in enumerate(keys if isinstance(keys, list) else []):
            at = (*base, "keys", i)
            if isinstance(raw, float) and raw.is_integer():
                # The schema accepts 1000.0 as an integer, but the key as written (1E3) is lost.
                yield _error(
                    at, "invalid_key", "the key is a number with a fraction or exponent; write it as a string"
                )
                continue
            if isinstance(raw, bool) or not isinstance(raw, str | int):
                continue  # a schema type error
            key = fit_parts(raw, chain, n)
            if key is None or not parts_match(compiled, key, n):
                message = f"after key.normalize {found} empty, '.', '..', spans lines or fails key.pattern"
                yield _error(at, "invalid_key", message)
            elif key in seen:
                yield _warning(at, "duplicate_key", "after key.normalize this key repeats an earlier one")
            seen.add(key)


def _key_pattern(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """Warn of keys of a value class that no ``key.pattern`` tells from other text, a missing one (two
    table sides need none: the schema requires it beside a service side) or one that matches the empty
    string: a pack's raw-value gate refuses every run of such a spec, and only once its cycle is signed
    off. ``business_code`` keys are never searched for."""
    value_class, pattern = _get(doc, "key/class"), _get(doc, "key/pattern")
    if not isinstance(value_class, str) or value_class == "business_code":
        return
    if pattern is None and not any("request" in side for _, side in _sides(doc)):
        found = "is missing"
    elif isinstance(pattern, str) and not regex_error(pattern) and compile_key_pattern(pattern).search(""):
        found = "matches the empty string"
    else:
        return  # it tells a key, or the schema or invalid_key_pattern reports it
    message = (
        f"key.pattern {found}: a raw key cannot be told from other text, so no evidence pack is issued for"
        " a run of this spec; declare a pattern that fits its keys only, or class"
        " keys that are not personal business_code"
    )
    yield _warning("key/pattern", "key_pattern_empty", message)


# -- domain pack, taxonomy, inferred schemas ------------------------------------------------------


def _domain_pack(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    pack = doc.get("domainPack")
    name = pack.get("name") if isinstance(pack, dict) else None
    if not isinstance(name, str) or not SLUG.fullmatch(name):
        return  # absent, or a schema error
    schema = pack_schema(name)
    if schema is None:
        location = f"deltasleuth/packs/{name.replace('-', '_')}/config.schema.json"
        yield _error(
            "domainPack/name", "unknown_domain_pack", f"domain pack {name!r} is not installed ({location})"
        )
        return
    version, major = pack.get("version"), pack_major(schema)
    if isinstance(version, str) and major is not None and version.split(".")[0] != str(major):
        message = f"the installed {name} pack serves major version {major}"
        yield _error("domainPack/version", "unsupported_domain_pack_version", message)
    config = pack.get("config", {})
    if isinstance(config, dict):
        yield from pack_config_issues(name, config, ("domainPack", "config"))
        for member in config:
            message = f"only the {name} pack's taxonomy codes and aliases apply; its {member} has no effect"
            yield _warning(("domainPack", "config", member), IGNORED, message)


def _taxonomy_codes(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """Codes a rule assigns exist (core, or the pack ``domainPack`` names) with the presence it labels.

    Otherwise the first matching finding would stop the run with a TaxonomyError.
    """
    from deltasleuth.core.classify import taxonomy  # classify imports this package: avoid the cycle

    name = _get(doc, "domainPack/name")
    pack = taxonomy.installed_packs().get(name) if isinstance(name, str) else None
    if isinstance(name, str) and pack is None:
        return  # a schema error or unknown_domain_pack: its codes cannot be resolved
    for pattern, presence, change_type in _TAXONOMY_RULES:
        for at, code in _strings(doc, pattern):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code):
                continue  # the schema reports the form
            try:
                row = taxonomy.lookup(code, pack)
            except taxonomy.TaxonomyError:
                where = f"core or pack {pack.name}" if pack else "core (no domainPack is set)"
                yield _error(at, "unknown_taxonomy_code", f"taxonomy code {code} is not in {where}")
                continue
            if row.presence != presence:
                message = (
                    f"taxonomy code {code} has presence {row.presence}; this rule labels {change_type} "
                    f"findings, which need a code of presence {presence}"
                )
                yield _error(at, "taxonomy_presence_mismatch", message)


# -- tables ---------------------------------------------------------------------------------------


def _replacement_error(pattern: str, replacement: str) -> str | None:
    """Why a key map's replacement cannot run in both dialects, if it cannot."""
    named = re.findall(r"\$([0-9]+)", replacement)
    if any(len(n) != 1 or n == "0" for n in named):  # PostgreSQL writes only \1 to \9 ($01 is not $1)
        return "a replacement names groups $1 to $9 only"
    if max(map(int, named), default=0) > re.compile(pattern).groups:
        return "the replacement names a group the pattern lacks"
    return None


def _tables(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """Each table side has one key column per key part (one, or ``key.parts`` for a composite key),
    and a filter in the predicate language; so has a key table (``_tabled``)."""
    parts = _get(doc, "key/parts") if _get(doc, "key/type") == "composite" else None
    expected = len(parts) if isinstance(parts, list) else 1
    for base, side in _tabled(doc):
        columns = _get(side, "table/keyColumns")
        if isinstance(columns, list) and columns and len(columns) != expected:
            message = f"{len(columns)} key column(s) for a key of {expected} part(s)"
            yield _error((*base, "table", "keyColumns"), "key_columns_mismatch", message)
        for at, predicate in _strings(side, "table/where", base=base):
            try:
                where.parse(predicate)
            except where.WhereError as exc:
                yield _error(at, "invalid_where", str(exc))
        for at, rule in _find(side, "table/keyColumns/*/keyMap/*", base):
            pattern, replacement = _get(rule, "pattern"), _get(rule, "replacement")
            if not isinstance(pattern, str):
                continue  # kind, prefix, width, sideType
            if problem := regex_error(pattern):
                yield _error((*at, "pattern"), "invalid_key_map", problem)
            elif isinstance(replacement, str) and (message := _replacement_error(pattern, replacement)):
                yield _error((*at, "replacement"), "invalid_key_map", message)
        for at, zone in _strings(side, "typeMap/*/naiveTimezone", base=base):
            try:
                ZoneInfo(zone)
            except (ValueError, KeyError):  # ZoneInfoNotFoundError is a KeyError
                yield _error(at, "invalid_type_map", f"{zone!r} is not an IANA time zone")
        for at, spec in _find(side, "typeMap/*", base):
            if isinstance(spec, dict) and "emptyAsNull" in spec and spec.get("as") != "string":
                yield _error((*at, "emptyAsNull"), "invalid_type_map", "emptyAsNull needs as: string")
            if isinstance(spec, dict):
                yield from _type_text(at, spec)
        if isinstance(file := _get(side, "table/file"), dict):
            yield from _file(base, side, file)


# A typeSpec's format: strptime's directives of no locale or pivot year.
_FORMAT = re.compile(r"(?:%[YmdHMSfz]|[-/.: T])+")
_TYPE_FORMAT = (
    "format goes with as: date or as: timestamp and holds only %Y %m %d %H %M %S %f %z and the characters - /"
    " . : T and space"
)


def _type_text(at: Parts, spec: dict[str, Any]) -> Iterator[SpecIssue]:
    """A typeSpec's ``format`` and ``decimalSeparator``, each with the types that read a text by it."""
    form = spec.get("format")
    if form is not None and (spec.get("as") not in ("date", "timestamp") or not _FORMAT.fullmatch(form)):
        yield _error((*at, "format"), "invalid_type_map", _TYPE_FORMAT)
    separator = spec.get("decimalSeparator")
    if separator is not None and (spec.get("as") not in ("decimal", "integer") or separator not in ".,"):
        message = 'decimalSeparator goes with as: decimal or as: integer and is "." or ","'
        yield _error((*at, "decimalSeparator"), "invalid_type_map", message)


_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")  # $defs/columnName
_CSV_KEYS = ({"as": "integer"}, {"as": "date"})
# The texts of a refused ``table.file`` by what each judges; the path's is manifest.PATH_REFUSED.
_L10 = {
    "format": "format csv needs csv (columns, delimiter); format parquet takes none",
    "columns": "csv.columns names every field once, in file order",
    "marks": "csv.delimiter, quote and escape are one character each, and the delimiter is not the quote",
    "newline": 'csv.newline must be "\\n" or "\\r\\n"',
    "encoding": "csv.encoding must be utf-8, utf-16 or latin-1: convert the export for any other",
    "keys": "csv.keys names key columns: {as: integer}, {as: decimal, scale: n} or {as: date}",
    "asOfFile": "file.asOfFile is a file name in the path's directory that the path does not match",
    "maxAge": "file.maxAge is a duration such as 26h or 2d",
    "partitions": "file.partitions names the directories' keys: column names, none of them in csv.columns",
}


def _file(owner: Parts, side: dict[str, Any], file: dict[str, Any]) -> Iterator[SpecIssue]:
    """A file side, or a key table's files (``owner``: where its table is declared):
    the form of ``table.file``, and what a file side does not run (``table.schema``, ILIKE and a
    regex key map)."""
    from deltasleuth.connectors.tables import manifest  # it imports this package: avoid the cycle

    base, csv, path = (*owner, "table", "file"), file.get("csv"), file.get("path")
    found = []
    if isinstance(path, str) and manifest.path_error(path):
        found.append(("path", manifest.PATH_REFUSED))
    csv_ok = isinstance(csv, dict) and csv.get("columns") and "delimiter" in csv
    if (file.get("format") == "csv" and not csv_ok) or (file.get("format") == "parquet" and csv is not None):
        found.append(("csv" if csv is not None else "format", _L10["format"]))
    csv = csv if isinstance(csv, dict) else {}
    columns = csv.get("columns") if isinstance(csv.get("columns"), list) else []
    if len(set(map(str, columns))) != len(columns) or not all(_COLUMN.fullmatch(str(c)) for c in columns):
        found.append(("csv", _L10["columns"]))
    marks = {member: csv[member] for member in ("delimiter", "quote", "escape") if member in csv}
    one = [isinstance(m, str) and len(m) == (1 if k == "delimiter" else len(m[:1])) for k, m in marks.items()]
    lines = any(isinstance(m, str) and m and m in "\r\n" for m in marks.values())
    if not all(one) or lines or ("delimiter" in marks and marks["delimiter"] == marks.get("quote", '"')):
        found.append(("csv", _L10["marks"]))
    for member, allowed in (("newline", ("\n", "\r\n")), ("encoding", ("utf-8", "utf-16", "latin-1"))):
        if member in csv and csv[member] not in allowed:
            found.append(("csv", _L10[member]))
    keyed = {k if isinstance(k, str) else _get(k, "name") for k in _get(side, "table/keyColumns") or []}
    keys = csv.get("keys") if isinstance(csv.get("keys"), dict) else {}
    for column, spec in keys.items():
        scaled = isinstance(spec, dict) and set(spec) == {"as", "scale"} and spec["as"] == "decimal"
        if column not in keyed or not (spec in _CSV_KEYS or (scaled and spec["scale"] in range(39))):
            found.append(("csv", _L10["keys"]))
            break
    if isinstance(stamp := file.get("asOfFile"), str) and isinstance(path, str):
        if manifest.stamp_error(path, stamp):
            found.append(("asOfFile", _L10["asOfFile"]))
    if isinstance(age := file.get("maxAge"), str):
        try:
            parse_duration(age)
        except ValueError:
            found.append(("maxAge", _L10["maxAge"]))
    partitions = file.get("partitions") if isinstance(file.get("partitions"), list) else []
    named = [p for p in partitions if isinstance(p, str) and _COLUMN.fullmatch(p) and p not in columns]
    if len(set(named)) != len(partitions):
        found.append(("partitions", _L10["partitions"]))
    yield from (_error((*base, member), "invalid_file", message) for member, message in found)
    if _get(side, "table/schema") is not None:
        yield _error((*owner, "table", "schema"), UNSUPPORTED, "not supported: table.schema on a file side")
    for at, column in _find(side, "table/keyColumns/*", owner):
        if _get(column, "keyMap/kind") == "regex":
            why = "a regex key map on a file side; declare csv.keys or a prefix_pad map"
            yield _error((*at, "keyMap"), UNSUPPORTED, f"not supported: {why}")
    for at, predicate in _strings(side, "table/where", base=owner):
        try:
            if "ILIKE" in _like_tests(where.parse(predicate)):
                yield _error(at, UNSUPPORTED, "not supported: ILIKE on a file side; use LIKE")
        except where.WhereError:
            continue  # reported by the table rules


def _graph(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """Graph parity reads link facts: two table sides, a composite key, and members and groups that
    split its parts between them."""
    graph = doc.get("graph")
    if not isinstance(graph, dict):
        return
    members, groups, parts = graph.get("members"), graph.get("groups"), _get(doc, "key/parts")
    if any(_get(side, "table/file") is not None for _, side in _sides(doc)):
        why = "graph parity with a file side; load the link facts into PostgreSQL"
        yield _error("graph", UNSUPPORTED, f"not supported: {why}")
    if sum("table" in side for _, side in _sides(doc)) < 2 or _get(doc, "key/type") != "composite":
        yield _error("graph", "invalid_graph", "graph parity needs two table sides and a composite key")
    elif all(isinstance(x, list) for x in (members, groups, parts)):
        named = [*members, *groups]  # type: ignore[misc]
        if sorted(named) != sorted(parts) or len(set(named)) != len(named):  # type: ignore[arg-type]
            yield _error("graph", "invalid_graph", "members and groups must split key.parts between them")


# -- not found and targets ------------------------------------------------------------------------


def _not_found_2xx(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """A 2xx not-found status (e.g. GraphQL ``{"data": {"x": null}}``) needs ``when`` to decide it."""
    for name, side in _sides(doc):
        for at, status in _find(side, "notFound/status/*", ("sides", name)):
            if isinstance(status, int) and 200 <= status <= 299 and "when" not in side["notFound"]:
                message = "a 2xx not-found status needs notFound.when to tell an absent body from a document"
                yield _error(at, "not_found_2xx_needs_when", message)


def _targets(doc: dict[str, Any], registry: TargetRegistry) -> Iterator[SpecIssue]:
    """Targets exist; notFound is declared authoritative by the target (X2); truncation is declared (X33)."""
    for base, source in _key_sources(doc):
        target_id = source.get("target")
        if isinstance(target_id, str) and target_id not in registry:
            yield _error((*base, "target"), "unknown_target", f"target {target_id!r} is not in the registry")
        elif isinstance(target_id, str) and source.get("kind") == "table":
            yield from _key_table_target(base, source, registry.get(target_id).engine)
        elif isinstance(target_id, str) and registry.get(target_id).engine:
            message = "a key listing needs a service target, not a database"
            yield _error((*base, "target"), "target_kind", message)
    declared = False  # a side's database declares its engine
    for name, side in _sides(doc):
        target_id, at = side.get("target"), ("sides", name)
        if not isinstance(target_id, str):
            continue  # a schema error
        if target_id not in registry:
            yield _error((*at, "target"), "unknown_target", f"target {target_id!r} is not in the registry")
            continue
        engine = registry.get(target_id).engine
        if ("table" in side) != bool(engine):
            tabled = "a database target (dsn_ref or engine)"
            needs = tabled if "table" in side else "a service target, not a database"
            yield _error((*at, "target"), "target_kind", f"this side needs {needs}")
            continue
        if "request" not in side:  # a table side
            filed = _get(side, "table/file") is not None
            if filed != (engine == "files"):  # table.file goes exactly with a files target
                message = "a files target needs table.file (path, format)"
                message = "table.file is for a files target (engine: files)" if filed else message
                yield _error((*at, "target"), "target_kind", message)
                continue
            if engine not in ("postgresql", "files"):  # a file side's limits are read with its table.file
                declared = True
                yield from _engine_limits(at, side)
            continue
        capabilities = registry.get(target_id).capabilities
        absence = capabilities.authoritative_absence
        if absence is None and isinstance(side.get("notFound"), dict):
            message = f"target {target_id!r} declares no authoritative_absence, so the side takes no notFound"
            yield _error((*at, "notFound"), "not_found_undeclared", message + " (X2)")
        elif absence is not None:
            for where, status in _find(side, "notFound/status/*", at):
                if status not in absence.status:
                    message = f"status {status} is not in target {target_id!r} authoritative_absence.status"
                    yield _error(where, "not_found_undeclared", f"{message} {list(absence.status)} (X2)")
        if "truncation" not in side and capabilities.truncation_indicators is None:
            message = f"neither this side nor target {target_id!r} declares truncation indicators (X33)"
            yield _error(at, "truncation_undeclared", message + ", so truncation would go undetected")
    if declared and isinstance(doc.get("graph"), dict):
        why = "graph parity with a MySQL or Oracle side; export the link facts into PostgreSQL"
        yield _error("graph", UNSUPPORTED, f"not supported: {why}")


def _key_table_target(base: Parts, source: dict[str, Any], engine: str | None) -> Iterator[SpecIssue]:
    """A key table's target is a database or a files target, its ``table.file`` going exactly with a files
    one, and on MySQL or Oracle its table runs what a side's does."""
    filed = _get(source, "table/file") is not None
    if not engine:
        message = "a key table needs a database target (dsn_ref or engine)"
    elif filed != (engine == "files"):
        message = "a files target needs table.file (path, format)"
        message = "table.file is for a files target (engine: files)" if filed else message
    else:
        if engine not in ("postgresql", "files"):
            yield from _engine_limits(base, source)
        return
    yield _error((*base, "target"), "target_kind", message)


def _engine_limits(owner: Parts, side: dict[str, Any]) -> Iterator[SpecIssue]:
    """What a table side on MySQL or Oracle does not run, nor a key table there (``owner``: where the table
    is declared): a regex key map and ILIKE."""
    for at, column in _find(side, "table/keyColumns/*", owner):
        if _get(column, "keyMap/kind") == "regex":
            why = "a regex key map on a MySQL or Oracle side; compare a view that derives the key"
            yield _error((*at, "keyMap"), UNSUPPORTED, f"not supported: {why}")
    for at, predicate in _strings(side, "table/where", base=owner):
        try:
            tests = _like_tests(where.parse(predicate))
        except where.WhereError:
            continue  # reported by the table rules
        if "ILIKE" in tests:
            yield _error(at, UNSUPPORTED, "not supported: ILIKE on a MySQL or Oracle side; use LIKE")


def _like_tests(node: where.Node) -> set[str | None]:
    own = {node.test} if node.op == "like" else set()
    return own.union(*(_like_tests(child) for child in node.children))


# -- product scope --------------------------------------------------------------------------------


def _gating(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """``unsupported`` for every feature the product does not run yet; ``ignored`` for accepted no-ops."""

    def unsupported(at: Parts | str, feature: str) -> SpecIssue:
        return _error(at, UNSUPPORTED, f"not supported: {feature}")

    if _get(doc, "canonical/mode") == "graph":
        yield unsupported("canonical/mode", "graph mode")
    for base, side in _tabled(doc):
        for path, feature in _TABLE_DEFERRED.items():
            yield from (unsupported(at, feature) for at, _ in _find(side, path, base))
        for at, column in _find(side, "table/keyColumns/*", base):
            if isinstance(column, dict) and "part" in column:
                yield unsupported((*at, "part"), "a key column's part; list key columns in key.parts order")
            if _get(column, "keyMap/kind") == "lookup":
                yield unsupported((*at, "keyMap"), "a lookup key map, which needs SQL templates")
    tables = [side for _, side in _sides(doc) if "table" in side]
    if tables and _get(doc, "key/normalize"):
        yield unsupported("key/normalize", "key.normalize with a table side; keys are the tables' values")
    predicate = _get(doc, "compare/valueFilter")
    if len(tables) == 2 and isinstance(predicate, str) and reads(predicate, "value_filter", "side"):
        # The scan counts identical rows as matched; a filter by side could tell them apart.
        yield unsupported("compare/valueFilter", "a valueFilter that reads side, between two tables")
    sampled, frame = _get(doc, "key/source/kind") == "hash_sample", {at for at, _ in _frame(doc)}
    for at, source in _key_sources(doc):
        kind, outside = source.get("kind"), not sampled or len(at) == 2
        if kind in ("csv", "sql", "run_keys"):
            advice = "use list, paginated_endpoint or a key table"
            yield unsupported((*at, "kind"), f"key source {kind!r}; {advice}")
        elif kind == "list" and at in frame:
            why = "sample a side's table or key listing"
            yield unsupported((*at, "kind"), f"hash_sample over an inline list; {why}")
        elif kind == "table" and _own_table(source):  # a key table: key.source itself, a service side's
            if len(at) > 2:
                why = "a frame's table is a side's"
                yield unsupported((*at, "kind"), f"a key table inside hash_sample; {why}")
            elif len(tables) == 2:
                why = "their scan compares every row"
                yield unsupported((*at, "kind"), f"a key table between two table sides; {why}")
            if _get(source, "table/columns") is not None:
                yield unsupported((*at, "table", "columns"), "columns in a key table; it gives keys only")
        elif kind == "table" and outside:
            why = "a table side's keys are scanned, or checked against it"
            yield unsupported((*at, "kind"), f"a table key source outside hash_sample ({why})")
        elif kind == "union" and outside:
            yield unsupported((*at, "kind"), "a union outside hash_sample; wrap it in a hash_sample")
    if sampled and len(tables) == 2:
        why = "their scan compares every row"
        yield unsupported("key/source/kind", f"hash_sample between two table sides: {why}")
    parts = _key_parts(doc)
    if sampled and parts:
        yield unsupported("key/source/kind", "hash_sample with a composite key beside a service side")
    for at, predicate, kind in _predicates(doc) if sampled else ():
        if reads(predicate, kind, "stratum") or reads(predicate, kind, "strata"):
            why = "strata label a sample's keys, not their findings"
            yield unsupported(at, f"a predicate over stratum or strata; {why}")
    for base, request in _side_requests(doc):
        for at, text in _template_strings(request, base):
            for name in _placeholders(text):
                # a multi-part key's parts are rendered beside the joined key
                rendered = name in RENDERED_PLACEHOLDERS or (parts and name.startswith("keyParts."))
                if not rendered and _known_placeholder(doc, name):
                    yield unsupported(at, f"the placeholder {{{{ {name} }}}}")
    for base, source in _key_sources(doc):  # a listing is walked once, for no one key
        request = source.get("request") if isinstance(source.get("request"), dict) else {}
        for at, text in _template_strings(request, (*base, "request")):
            if any(n.startswith("keyParts.") and _known_placeholder(doc, n) for n in _placeholders(text)):
                yield unsupported(at, "a keyParts placeholder in a key listing's request")
    if isinstance(mode := _get(doc, "time/mode"), str) and mode != "live":
        yield unsupported("time/mode", f"time.mode {mode!r}; both sides are read live")
    elif _get(doc, "time/watermark") is not None:
        # A response without its declared watermark must be INCOMPLETE; nothing reads watermarks yet.
        yield unsupported("time/watermark", "time.watermark")
    elif _get(doc, "time/skewBudget") is not None:
        why = "the gap between the two reads is not checked, so a pair is never DRIFT(skew_exceeded)"
        yield _warning("time/skewBudget", IGNORED, f"/time/skewBudget is not applied: {why}")
    if isinstance(sampling := _get(doc, "sampling/mode"), str) and sampling != "all":
        yield unsupported("sampling/mode", f"sampling.mode {sampling!r}; every key is compared")
    if _get(doc, "schemaDrift/mode") == "pause" and not tables:
        yield unsupported("schemaDrift/mode", "pausing a run on schema drift between two service sides")
    for path, feature in _DEFERRED.items():
        if _get(doc, path):
            yield unsupported(path, feature)
    for path, why in _IGNORED.items():
        for at, value in _find(doc, path):
            if value is not None:
                yield _warning(at, IGNORED, f"{pointer(at)} is not applied: {why}")


def _predicates(doc: dict[str, Any]) -> Iterator[tuple[Parts, str, CelKind]]:
    yield from ((at, source, "known_diff") for at, source in _strings(doc, "knownDiffs/*/when"))
    yield from ((at, source, "value_filter") for at, source in _strings(doc, "compare/valueFilter"))


# -- sampling -------------------------------------------------------------------------------------


def _sampling(doc: dict[str, Any]) -> Iterator[SpecIssue]:
    """A hash sample's frame, strata and sizes: each frame source enumerates
    one side, once, exposing the columns the strata read; every dimension has buckets, so labels are spec
    constants; nothing cuts the sample; its sizes fit a sample and can pass as one."""
    from deltasleuth.core import sampling  # it imports this package: avoid the cycle

    sample, base = _get(doc, "key/source"), ("key", "source")
    if not isinstance(sample, dict) or sample.get("kind") != "hash_sample":
        return
    yield from _frame_members(doc, sample)
    dimensions = [(i, d) for i, d in enumerate(sample.get("strata") or []) if isinstance(d, dict)]
    for i, dimension in dimensions:
        if not dimension.get("buckets"):
            message = (
                "a stratum dimension of a hash sample needs buckets: a value read from the data would become"
                " a label in reports and packs (a bucket without values, min or max catches the rest)"
            )
            yield _error((*base, "strata", i), "stratum_needs_buckets", message)
    if _get(doc, "execution/stop/maxKeys") is not None:
        message = "maxKeys would cut the sample: size it with targetSize or rate"
        yield _error("execution/stop/maxKeys", "sample_cut", message)
    buckets = [
        ((*base, "strata", i, *at), str(d.get("name")), b)
        for i, d in dimensions
        for at, b in _find(d, "buckets/*")
        if isinstance(b, dict)
    ]
    sizes = [(at, name, b, b["targetSize"]) for at, name, b in buckets if type(b.get("targetSize")) is int]
    if (total := sum(size for *_, size in sizes)) > sampling.LIMIT:
        message = f"the sample would hold {total:,} keys, over {sampling.LIMIT:,}"
        yield _error((*base, "strata"), "sample_size", f"{message}: compare the population as a census")
    lengths: dict[str, int] = {}
    for _, name, bucket in buckets:
        lengths[name] = max(lengths.get(name, 0), len(str(bucket.get("label"))))
    if (longest := sum(len(name) + 1 + n for name, n in lengths.items()) + len(lengths) - 1) > 256:
        message = f"the longest stratum label these dimensions make holds {longest} characters, over 256"
        yield _error((*base, "strata"), "stratum_label_long", message)
    for at, name, bucket, size in sizes if sample.get("method") == "bottom_k" else ():
        tier = bucket.get("riskTier") if bucket.get("riskTier") in sampling.TARGETS else "standard"
        target = sampling.TARGETS[tier]
        if size < (need := sampling.zero_failure_n(target)):
            message = (
                f"bucket {name}={bucket.get('label')} ({tier}, {sampling.percent(target)}) draws at most"
                f" {size:,} keys; a zero-failure pass needs {need:,} cleanly compared keys (draw"
                f" {sampling.attempted(need):,}): it passes only as a census"
            )
            yield _warning((*at, "targetSize"), "undersized", message)
    labels = [((*at, "label"), f"a bucket of {name}", bucket.get("label")) for at, name, bucket in buckets]
    yield from _key_shaped_labels(doc, [((*base, "salt"), "salt", sample.get("salt")), *labels])
    values = [
        ((*at, *where), f"a bucket of {name}", value)
        for at, name, bucket in buckets
        for where, value in [*_find(bucket, "values/*"), *_find(bucket, "min"), *_find(bucket, "max")]
    ]
    yield from _key_shaped_labels(doc, values, "value")


def _frame_members(doc: dict[str, Any], sample: dict[str, Any]) -> Iterator[SpecIssue]:
    """Each frame source is a table side's table or a listing of a side's target, one per side, and
    exposes every column the strata read (a dimension's, or ``row.<name>`` in their CEL)."""
    sides = dict(_sides(doc))
    targets = {side.get("target"): name for name, side in reversed(sides.items())}  # a first
    read = [column for _, column in _strings(sample, "strata/*/column")]
    for _, source in [*_strings(sample, "strata/*/cel"), *_strings(sample, "signoffStratum")]:
        read += _ROW_FIELD.findall(_CEL_STRING.sub("''", source))
    enumerated: dict[str, int] = {}
    for i, (at, member) in enumerate(_frame(doc)):
        kind, side, exposed = member.get("kind"), None, None
        if kind == "table" and member.get("side") in ("a", "b"):
            side, exposed = member["side"], member.get("strataColumns")
            if "table" not in sides.get(side, {}):
                yield _error((*at, "side"), "frame_member", f"side {side} is not a table side")
        elif kind == "paginated_endpoint" and isinstance(target := member.get("target"), str):
            side, exposed = targets.get(target), member.get("strataFields")
            if side is None:
                message = (
                    f"the frame's listing reads target {target}, which is neither side's: each frame source"
                    " enumerates side a's or side b's keys"
                )
                yield _error((*at, "target"), "frame_member", message)
        if side is None:
            continue
        if side in enumerated:
            message = f"union sources {enumerated[side]} and {i} both enumerate side {side}"
            yield _error(at, "frame_member", f"{message}: a union takes one source per side")
        enumerated.setdefault(side, i)
        exposed = exposed if isinstance(exposed, list | dict) else ()
        for column in dict.fromkeys(c for c in read if c not in exposed):
            message = f"the strata read column {column!r}, which source {i} does not expose"
            yield _error(at, "strata_column_missing", f"{message} (strataColumns or strataFields)")


def _key_shaped_labels(
    doc: dict[str, Any], labels: list[tuple[Parts, str, Any]], noun: str = "label"
) -> Iterator[SpecIssue]:
    """Warn of a label, or a bucket's value (a string or number, which the gate reads as its text), a pack's
    raw-value gate would refuse: of keys of a value class, one that, or a part of which (split as the gate
    splits words), passes the spec's key check."""
    value_class, pattern = _get(doc, "key/class"), _get(doc, "key/pattern")
    chain = _get(doc, "key/normalize") or []
    if not isinstance(chain, list) or not all(isinstance(n, str) and n in KEY_NORMALISERS for n in chain):
        return  # the schema reports the chain
    if value_class in (None, "business_code") or not isinstance(pattern, str):
        return
    compiled = None if regex_error(pattern) else compile_key_pattern(pattern)
    if compiled is None or compiled.search("") is not None:
        return  # the pattern cannot tell a key from other text
    advice = "rename it" if noun == "label" else "bucket without customer keys"
    for at, what, label in labels:
        if isinstance(label, bool) or not isinstance(label, str | int | float | Decimal):
            continue
        label = str(label)
        parts = {label, *re.split(r"[-_/.:@+#~]", label), *re.split(r"[^0-9A-Za-z]+", label)} - {""}
        if any(key_matches(compiled, normalise_key(part, chain)) for part in parts):
            message = f"the {noun} {label} ({what}) is shaped like a key of this spec: a pack would refuse it"
            yield _warning(at, f"{noun}_key_shaped", f"{message}; {advice}")


def _frame(doc: dict[str, Any]) -> list[tuple[Parts, dict[str, Any]]]:
    """The sources of a hash sample's frame: its source, or its union's; none for another key source."""
    frame = _get(doc, "key/source/source")
    if _get(doc, "key/source/kind") != "hash_sample" or not isinstance(frame, dict):
        return []
    if frame.get("kind") != "union":
        return [(("key", "source", "source"), frame)]
    members = frame.get("sources") if isinstance(frame.get("sources"), list) else []
    return [
        (("key", "source", "source", "sources", i), m) for i, m in enumerate(members) if isinstance(m, dict)
    ]


# -- navigation -----------------------------------------------------------------------------------


def _children(node: Any) -> Any:
    return node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()


def _find(node: Any, pattern: str, base: Parts = ()) -> list[tuple[Parts, Any]]:
    """(location, value) of every value at ``pattern`` below ``node``."""
    found: list[tuple[Parts, Any]] = [(base, node)]
    for step in pattern.split("/"):
        found = [
            ((*at, key), child)
            for at, value in found
            for key, child in _children(value)
            if step in ("*", key)
        ]
    return found


def _strings(node: Any, *patterns: str, base: Parts = ()) -> Iterator[tuple[Parts, str]]:
    for pattern in patterns:
        yield from ((at, value) for at, value in _find(node, pattern, base) if isinstance(value, str))


def _key_source_strings(doc: dict[str, Any], *patterns: str) -> Iterator[tuple[Parts, str]]:
    for base, source in _key_sources(doc):
        yield from _strings(source, *patterns, base=base)


def _get(node: Any, path: str) -> Any:
    for name in path.split("/"):
        node = node.get(name) if isinstance(node, dict) else None
    return node


def _sides(doc: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for name in ("a", "b"):
        if isinstance(side := _get(doc, f"sides/{name}"), dict):
            yield name, side


def _own_table(source: dict[str, Any]) -> bool:
    """Whether a ``table`` key source is a key table, with a target and a table of its own, and no frame
    member, which names a side."""
    return "target" in source or "table" in source


def _tabled(doc: dict[str, Any]) -> Iterator[tuple[Parts, dict[str, Any]]]:
    """Whatever declares a ``table``, and where: each side, and a key table, whose table is judged as a
    side's."""
    yield from ((("sides", name), side) for name, side in _sides(doc))
    source = _get(doc, "key/source")
    if isinstance(source, dict) and source.get("kind") == "table" and _own_table(source):
        yield ("key", "source"), source


def _key_parts(doc: dict[str, Any]) -> list[Any]:
    """The parts of a multi-part key, a composite key beside a service side: each is normalised, matched
    and templated on its own. Empty for any other key: its text is judged whole, as between two tables."""
    parts, tables = _get(doc, "key/parts"), sum("table" in side for _, side in _sides(doc))
    return parts if _get(doc, "key/type") == "composite" and tables < 2 and isinstance(parts, list) else []


def _side_requests(doc: dict[str, Any]) -> Iterator[tuple[Parts, dict[str, Any]]]:
    for name, side in _sides(doc):
        if isinstance(side.get("request"), dict):
            yield ("sides", name, "request"), side["request"]


def _key_sources(doc: dict[str, Any]) -> Iterator[tuple[Parts, dict[str, Any]]]:
    """``key.source`` and every source nested in it (``hash_sample.source``, ``union.sources[]``)."""
    pending: list[tuple[Parts, Any]] = [(("key", "source"), _get(doc, "key/source"))]
    while pending:
        at, node = pending.pop(0)
        if isinstance(node, dict):
            yield at, node
            pending.append(((*at, "source"), node.get("source")))
            if isinstance(members := node.get("sources"), list):
                pending += [((*at, "sources", i), member) for i, member in enumerate(members)]


def _pattern(path: str) -> str | None:
    try:
        return normalise_pattern(path)
    except PathError:
        return None
