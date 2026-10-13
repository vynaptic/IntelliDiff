"""JSON Schema (Draft 2020-12) validation with path-precise, value-free issues.

A failed ``oneOf`` is explained inside the alternative the document was evidently aiming at, or as
"requires one of: ..." when the alternatives differ by one required member. Messages are built
from the schema, never from the document: the failing value may be a credential.
"""

from __future__ import annotations

import functools
import json
import re
from datetime import date, datetime
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from deltasleuth.core.spec.errors import SpecIssue, pointer

SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_BOUNDS = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}
_UNITS = {"Length": "character(s)", "Items": "item(s)", "Properties": "member(s)"}

Parts = tuple[str | int, ...]

# No "regex" format: key patterns are compiled (once, with runtime flags) by the loader rules.
FORMAT_CHECKER = FormatChecker(formats=())


@FORMAT_CHECKER.checks("date", raises=ValueError)
def _is_date(value: object) -> bool:
    if not isinstance(value, str):
        return True
    return re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None and bool(date.fromisoformat(value))


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    # fromisoformat checks the calendar but accepts at most microsecond fractions.
    text = re.sub(r"(\.[0-9]{6})[0-9]+", r"\1", value.upper().replace(" ", "T"))
    return _DATE_TIME.fullmatch(value) is not None and bool(datetime.fromisoformat(text))


@functools.cache
def core_schema() -> dict[str, Any]:
    """The packaged spec.schema.json."""
    return json.loads(
        resources.files("deltasleuth.core.spec").joinpath("spec.schema.json").read_text("utf-8")
    )


@functools.cache
def _core_validator() -> Draft202012Validator:
    return Draft202012Validator(core_schema(), format_checker=FORMAT_CHECKER)


def spec_schema_issues(doc: Any) -> list[SpecIssue]:
    return schema_issues(_core_validator(), doc)


@functools.cache
def pack_schema(name: str) -> dict[str, Any] | None:
    """Config schema of the installed pack ``name`` (``a-b`` -> ``deltasleuth/packs/a_b/``), or None."""
    if not SLUG.fullmatch(name):
        return None
    resource = resources.files("deltasleuth.packs").joinpath(name.replace("-", "_"), "config.schema.json")
    return json.loads(resource.read_text("utf-8")) if resource.is_file() else None


def pack_major(schema: dict[str, Any]) -> int | None:
    """The pack major version a config schema serves, from its ``$id`` (``.../v1/config.schema.json``)."""
    match = re.search(r"/v([0-9]+)/config\.schema\.json$", str(schema.get("$id", "")))
    return int(match.group(1)) if match else None


@functools.cache
def _pack_validator(name: str) -> Draft202012Validator:
    # Pack schemas $ref the core $defs by the core schema's $id.
    core = core_schema()
    registry: Registry = Registry().with_resource(core["$id"], Resource.from_contents(core))
    return Draft202012Validator(pack_schema(name), registry=registry, format_checker=FORMAT_CHECKER)


def pack_config_issues(name: str, config: Any, base: Parts) -> list[SpecIssue]:
    return schema_issues(_pack_validator(name), config, base)


def schema_issues(validator: Draft202012Validator, instance: Any, base: Parts = ()) -> list[SpecIssue]:
    """Every schema failure of ``instance``, with pointers prefixed by ``base``."""
    return [issue for error in validator.iter_errors(instance) for issue in _explain(error, base)]


def _explain(error: ValidationError, base: Parts) -> list[SpecIssue]:
    if error.validator not in ("oneOf", "anyOf") or not error.context:
        return _describe(error, (*base, *error.absolute_path))
    branches: dict[object, list[ValidationError]] = {}
    for failure in error.context:
        branches.setdefault(failure.relative_schema_path[0], []).append(failure)
    alternatives = _required_alternatives(error, list(branches.values()))
    if alternatives:
        at = pointer((*base, *error.absolute_path))
        return [SpecIssue(at, "requires one of: " + ", ".join(alternatives), f"schema.{error.validator}")]
    explained = [[i for failure in branch for i in _explain(failure, base)] for branch in branches.values()]
    # The intended branch: fewest failures, then fewest type mismatches, then the deepest.
    return min(
        explained,
        key=lambda issues: (
            len(issues),
            sum(issue.code == "schema.type" for issue in issues),
            -max((issue.pointer.count("/") for issue in issues), default=0),
        ),
    )


def _required_alternatives(error: ValidationError, branches: list[list[ValidationError]]) -> list[str]:
    """Member names when each alternative only lacks one different required member at this level."""
    names: list[str] = []
    for branch in branches:
        failure = branch[0]
        if (
            len(branch) != 1
            or failure.validator != "required"
            or failure.absolute_path != error.absolute_path
        ):
            return []
        missing = [name for name in failure.validator_value if name not in failure.instance]
        if len(missing) != 1:
            return []
        names.append(missing[0])
    return names


def _describe(error: ValidationError, path: Parts) -> list[SpecIssue]:
    code, instance = f"schema.{error.validator}", error.instance
    if "propertyNames" in error.absolute_schema_path:
        message = f"member name {str(instance)!r} is not allowed{_about(error.schema)}"
        return [SpecIssue(pointer((*path, str(instance))), message, "schema.propertyNames")]
    if error.validator == "additionalProperties" and isinstance(instance, dict):
        schema = error.schema if isinstance(error.schema, dict) else {}
        declared, patterns = schema.get("properties", {}), schema.get("patternProperties", {})
        extras = [n for n in instance if n not in declared and not any(re.search(p, n) for p in patterns)]
        if extras:
            return [SpecIssue(pointer((*path, n)), f"unexpected member {n!r}", code) for n in extras]
    if error.validator == "not":
        names = _forbidden_names(error.validator_value)
        present = [n for n in names if isinstance(instance, dict) and n in instance]
        if present:
            message = f"member {present[0]!r} is not allowed here{_about(error.schema)}"
            return [SpecIssue(pointer((*path, present[0])), message, code)]
        return [SpecIssue(pointer(path), f"is not allowed here{_about(error.schema)}", code)]
    return [SpecIssue(pointer(path), _message(error), code)]


def _forbidden_names(schema: object) -> list[str]:
    """Members a ``not`` forbids: ``{required: [x]}`` or ``{anyOf: [{required: [x]}, ...]}``."""
    alternatives = schema.get("anyOf", [schema]) if isinstance(schema, dict) else []
    return [
        alt["required"][0]
        for alt in alternatives
        if isinstance(alt, dict) and set(alt) == {"required"} and len(alt["required"]) == 1
    ]


def _message(error: ValidationError) -> str:
    kind, value = error.validator, error.validator_value
    if kind in _BOUNDS:
        return f"must be {_BOUNDS[kind]} {value}"
    match kind:
        case "required":
            return error.message  # "'<name>' is a required property": a schema-defined name
        case "type":
            return f"must be of type {_listing(value)}"
        case "enum":
            return f"must be one of {_listing(value)}"
        case "const":
            return f"must be {value!r}"
        case "pattern":
            shown = f" {value}" if len(value) <= 80 else ""
            return f"does not match the required pattern{shown}{_about(error.schema)}"
        case "format":
            return f"is not a valid {value}"
        case "minLength" | "maxLength" | "minItems" | "maxItems" | "minProperties" | "maxProperties":
            bound = "at least" if kind.startswith("min") else "at most"
            return f"must have {bound} {value} {_UNITS[kind[3:]]}"
        case "uniqueItems":
            return "items must be unique"
        case "contains":
            wanted = (
                f"{value['const']!r}" if isinstance(value, dict) and "const" in value else "a matching item"
            )
            return f"must contain {wanted}"
        case "oneOf":
            return f"matches more than one alternative{_about(error.schema)}"
        case None:
            return "is not allowed here"  # a false schema
    return f"violates '{kind}'{_about(error.schema)}"


def _listing(value: object) -> str:
    items = value if isinstance(value, list) else [value]
    return ", ".join(repr(v) for v in items[:12]) + (", ..." if len(items) > 12 else "")


def _about(schema: object) -> str:
    """``": <description>"`` of the failing subschema, cut to 300 characters."""
    description = schema.get("description") if isinstance(schema, dict) else None
    if not description:
        return ""
    return ": " + (description if len(description) <= 300 else description[:297] + "...")
