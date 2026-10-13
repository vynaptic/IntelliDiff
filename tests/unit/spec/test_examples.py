"""The three shipped example specs: valid, gated for what the product does not run, compiled faithfully."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from deltasleuth.core.spec import SpecError, TargetRegistry, compile_spec, validate_spec
from deltasleuth.core.spec.assemble import build_compiled_spec
from deltasleuth.core.types import (
    FieldCompare,
    KeyConfig,
    Normalizer,
    NotFoundRule,
    RetryPolicy,
    RuleClassification,
    Severity,
    Side,
    TruncationIndicators,
)

Example = Callable[[str], dict[str, Any]]
_REPORTING = {"/reporting/formats", "/reporting/fields"}
_NOT_IN_MVP = {"/inferredSchema", "/sides/a/rawClear", "/sides/b/rawClear"}
_EXPECTED: dict[str, tuple[set[str], set[str]]] = {  # stem -> (unsupported, ignored) pointers
    "generic-customer-services": (
        {"/noise/calibration", "/time/mode"},  # its hash sample over two listings runs (SAMPLING.md §2.1)
        {*_NOT_IN_MVP, *_REPORTING},
    ),
    "entity-linking-services": (
        {
            *("/canonical/mode", "/identity/crosswalk", "/key/source/source/sources/0/kind"),  # an sql frame
            *("/noise/calibration", "/noise/masks", "/schemaDrift/mode"),
        },
        {*_NOT_IN_MVP, "/reporting/interim", "/time/skewBudget", *_REPORTING},  # and the pack config
    ),
    "pg-vs-oracle-tables": (
        {
            "/time/mode",
            "/key/normalize",
            *(f"/sides/{s}/{m}" for s in "ab" for m in ("session", "collation")),
        },
        {"/sides/a/rawClear", "/sides/b/rawClear", "/compare/strategy", *_REPORTING},
    ),
}


def _by_code(issues: list[Any], code: str) -> set[str]:
    return {issue.pointer for issue in issues if issue.code == code}


@pytest.mark.parametrize("stem", list(_EXPECTED))
def test_examples_are_valid_and_gated(example: Example, stem: str) -> None:
    doc = example(stem)
    issues = validate_spec(doc)
    unsupported, ignored = _EXPECTED[stem]
    pack = {f"/domainPack/config/{member}" for member in doc.get("domainPack", {}).get("config", {})}
    assert {i.code for i in issues} == {"unsupported", "ignored"}
    assert (_by_code(issues, "unsupported"), _by_code(issues, "ignored")) == (unsupported, ignored | pack)
    with pytest.raises(SpecError) as exc:
        compile_spec(doc)
    assert {i.pointer for i in exc.value.issues} == unsupported


def test_generic_example_compiles_faithfully(example: Example, customer_targets: TargetRegistry) -> None:
    doc = example("generic-customer-services")
    assert validate_spec(doc, targets=customer_targets)  # only gated features
    spec = build_compiled_spec(doc)  # what the assembler makes of the rest
    assert spec.key == KeyConfig(
        name="customer_id",
        type="string",
        pattern="^C[0-9]{8}$",
        value_class="identifier",
        normalize=(Normalizer("trim"), Normalizer("upper")),
    )
    a, b = spec.sides[Side.A], spec.sides[Side.B]
    assert (a.request.path, a.request.query, b.request.query) == (
        "/api/v1/customers/{{ key }}",
        {"expand": "accounts,consents"},
        {"include": ["products", "consents"]},
    )
    assert a.request.headers["X-Request-Id"] == "ds-{{ run_id }}-{{ call_seq }}"
    assert (a.select, b.select, a.not_found) == ("$.customer", "$.data", NotFoundRule())
    assert b.truncation == TruncationIndicators(
        cap=100, items_path="$.data.products", count_path="$.data.productCount"
    )
    arrays = {rule.path: rule for rule in spec.compare.arrays}
    assert (arrays["$.accounts"].key, arrays["$.accounts"].severity) == (
        ("sortCode", "accountNo"),
        Severity.HIGH,
    )
    assert (arrays["$.addresses[*].lines"].mode, arrays["$.tags"].mode) == ("ordered", "set")
    fields = {rule.path: rule for rule in spec.compare.fields}
    assert fields["$.phones[*].number"].normalize == (Normalizer("e164", {"defaultRegion": "GB"}),)
    assert fields["$.accounts[*].interestRate"].compare == FieldCompare(
        mode="tolerance", abs_tol=Decimal("0.000001"), rel_tol=Decimal("0.0001")
    )
    assert [(r.rule_id, r.classification, r.expires) for r in spec.known_diffs] == [
        ("KD-CP-0001", RuleClassification.EXPECTED_BY_DESIGN, None),
        ("KD-CP-0002", RuleClassification.BUG, "2027-01-31"),
    ]
    assert (len(spec.security.tokenize), len(spec.security.clear), spec.security.unlisted) == (
        9,
        18,
        "tokenize",
    )
    assert spec.execution.retries == RetryPolicy(
        max=2,
        initial_s=0.25,
        max_s=4.0,
        multiplier=2.0,
        retry_on=("connect_error", "timeout", "http_429", "http_5xx"),
    )
    assert (spec.execution.delay_s, spec.execution.breaker_window, spec.execution.breaker_cooldown_s) == (
        0.15,
        50,
        60.0,
    )


def test_entity_linking_inspection_compile(example: Example) -> None:
    spec = build_compiled_spec(example("entity-linking-services"))
    line = next(rule for rule in spec.compare.fields if rule.path == "$.nodes.entity[*].addresses[*].line")
    assert (line.compare, line.normalize) == (
        FieldCompare(mode="similarity", threshold=0.88),
        (Normalizer("nfkc"), Normalizer("address_std")),
    )
    assert (spec.compare.exclude, spec.compare.null_equals_absent) == (("$.edges.LINKED_TO[*].via",), True)
    b = spec.sides[Side.B]
    assert (b.request.method, b.request.body["entityCode"]) == ("POST", "{{ key }}")
    assert b.not_found.when == "response.status == 200 && !has(response.body.result.vertex)"
    assert spec.domain_pack == "entity-linking"


def test_table_sides_compile_to_table_reads(example: Example) -> None:
    spec = build_compiled_spec(example("pg-vs-oracle-tables"))
    a = spec.sides[Side.A]
    assert (a.request, a.select, a.map, a.truncation) == (None, None, None, None)
    assert (a.not_found, a.table.key_columns) == (NotFoundRule(), ("org_code",))
    assert spec.compare.include[0] == "$.account_name"
    assert (spec.execution.retries, spec.execution.total_timeout_s) == (RetryPolicy(max=1), 10800.0)
