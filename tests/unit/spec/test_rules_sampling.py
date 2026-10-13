"""The loader and a hash sample: what it accepts and compiles into
``CompiledSpec.sample``, each refusal with its message, and specs without a sample unchanged."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from deltasleuth import approvals
from deltasleuth.core.spec import TargetRegistry, compile_spec, validate_spec
from deltasleuth.core.spec.expressions import CEL_VARIABLES, cel_error
from deltasleuth.core.types import (
    FrameMember,
    Resolve,
    Side,
    StratumBucket,
    Target,
    TargetCapabilities,
    TruncationIndicators,
)

LISTING = {
    "kind": "paginated_endpoint",
    "target": "new-api",
    "request": {"method": "GET", "path": "/v1/customers"},
    "pagination": {"kind": "cursor", "itemsPath": "$.items", "cursorPath": "$.next", "cursorParam": "after",
                   "maxPages": 30000},
    "keyPath": "$.id",
    "strataFields": {"degree": "$.degree", "flagged": "$.flagged", "changed_at": "$.changedAt"},
}  # fmt: skip
RECENT = "row.changed_at != null && timestamp(row.changed_at) >= now - duration('168h')"
SIGNOFF = (
    "riskTier == 'high' ? (strata.flagged == 'Y' ? 'flagged=Y' : 'recent=Y') : 'degree=' + strata.degree"
)
TARGETS = TargetRegistry(
    {
        "legacy-db": Target("legacy-db", "", dsn_ref="file:secrets/legacy.dsn"),
        "new-api": Target(
            "new-api", "https://new.example",
            capabilities=TargetCapabilities(truncation_indicators=TruncationIndicators(100, "$.x")),
        ),
        "master-data": Target("master-data", "https://master.example"),
    }
)  # fmt: skip


def _bucket(label: str, size: int | None = 3011, **fields: Any) -> dict[str, Any]:
    return {"label": label, **({"targetSize": size} if size else {}), **fields}


@pytest.fixture
def sampled(minimal_spec: dict[str, Any]) -> dict[str, Any]:
    """A union of side a's table and side b's listing, three dimensions, a sign-off label."""
    doc = copy.deepcopy(minimal_spec)
    doc["key"] = {"name": "customer_id", "type": "string", "pattern": "^C[0-9]{8}$"}
    doc["sides"]["a"] = {"role": "legacy", "target": "legacy-db",
                         "table": {"name": "Customers", "schema": "crm", "keyColumns": ["id"]}}  # fmt: skip
    doc["sides"]["b"]["target"] = "new-api"
    doc["key"]["source"] = copy.deepcopy({
        "kind": "hash_sample",
        "method": "bottom_k",
        "salt": "cutover-q4",
        "resolve": {"times": 3, "delay": "10m"},
        "strata": [
            {"name": "degree", "column": "degree",
             "buckets": [_bucket("d0", max=0), _bucket("d1_5", min=1, max=5), _bucket("other")]},
            {"name": "flagged", "column": "flagged",
             "buckets": [_bucket("Y", 30107, values=["Y"], riskTier="high"), _bucket("N", None)]},
            {"name": "recent", "cel": RECENT,
             "buckets": [_bucket("Y", 30107, values=[True], riskTier="high"), _bucket("N", None)]},
        ],
        "signoffStratum": SIGNOFF,
        "source": {"kind": "union", "sources": [
            {"kind": "table", "side": "a", "strataColumns": ["degree", "flagged", "changed_at"]}, LISTING,
        ]},
    })  # fmt: skip
    return doc


def _issues(doc: dict[str, Any], **kwargs: Any) -> list[tuple[str, str, str, str]]:
    return [(i.pointer, i.code, i.severity, i.message) for i in validate_spec(doc, **kwargs)]


def test_the_example_compiles_into_a_sample(sampled: dict[str, Any]) -> None:
    assert _issues(sampled) == _issues(sampled, targets=TARGETS) == []
    spec = compile_spec(sampled, targets=TARGETS)
    sample = spec.sample
    assert sample is not None
    assert (sample.method, sample.label, sample.rate, sample.signoff_stratum) == (
        "bottom_k",
        "cutover-q4",
        None,
        SIGNOFF,
    )
    assert sample.resolve == Resolve(3, 600.0)
    assert sample.population == ("listing:new-api:GET /v1/customers", "table:legacy-db/customers")
    table, listing = sample.members
    columns, written = ("degree", "flagged", "changed_at"), sampled["key"]["source"]["source"]["sources"][0]
    assert table == FrameMember("table", Side.A, "table:legacy-db/customers", columns, source=written)
    assert (listing.side, listing.fields["changed_at"], listing.source["keyPath"]) == (
        Side.B,
        "$.changedAt",
        "$.id",
    )
    degree, flagged, recent = sample.dimensions
    assert (degree.column, degree.cel, recent.column, recent.cel) == ("degree", None, None, RECENT)
    assert degree.buckets[1] == StratumBucket("d1_5", min=Decimal(1), max=Decimal(5), target_size=3011)
    assert [(b.label, b.risk_tier, b.target_size) for b in flagged.buckets] == [
        ("Y", "high", 30107),
        ("N", "standard", None),
    ]
    assert approvals.needs(spec) == ["sampling"]


def test_rates_are_u_thresholds_and_resolve_has_defaults(sampled: dict[str, Any]) -> None:
    source = sampled["key"]["source"]
    del source["resolve"], source["signoffStratum"]
    source.update(method="rate", rate=0.1)
    for dimension in source["strata"]:
        for bucket in dimension["buckets"]:
            bucket.pop("targetSize", None)
    source["strata"][1]["buckets"][0]["rate"] = 1.0
    sample = compile_spec(sampled).sample
    assert sample is not None and sample.resolve == Resolve()
    assert (sample.rate, sample.dimensions[1].buckets[0].rate) == (int(Decimal("0.1") * 2**64), 2**64)


def test_a_listing_alone_is_a_frame(minimal_spec: dict[str, Any]) -> None:
    minimal_spec["key"]["source"] = {
        "kind": "hash_sample", "rate": 0.01, "salt": "salt", "source": {**LISTING, "target": "things-v2",
                                                                         "strataFields": {}},
    }  # fmt: skip
    assert validate_spec(minimal_spec) == []
    sample = compile_spec(minimal_spec).sample
    assert sample is not None and sample.population == ("listing:things-v2:GET /v1/customers",)
    assert (sample.method, sample.members[0].side, sample.dimensions) == ("rate", Side.B, ())


def test_dimension_expressions_read_key_and_now() -> None:
    assert CEL_VARIABLES["stratum_dimension"] == {"key": "STRING", "row": "DYN", "now": "TIMESTAMP"}
    assert CEL_VARIABLES["signoff_stratum"]["now"] == "TIMESTAMP"
    assert cel_error("key.startsWith('C0') ? 'legacy' : 'new'", "stratum_dimension") is None
    assert cel_error(RECENT, "stratum_dimension") is None
    assert cel_error("now - duration('1h') < now ? 'x' : 'y'", "signoff_stratum") is None
    assert cel_error("now + 1", "stratum_dimension") is not None  # a timestamp, typed


@pytest.mark.parametrize(
    ("location", "value", "problem"),
    [
        ("salt", "cutover q4", "schema.pattern"),  # the label's characters only
        ("salt", "abc", "schema.pattern"),
        ("salt", "x" * 65, "schema.pattern"),
        ("resolve", {"times": 11}, "schema.maximum"),
        ("resolve", {"delay": "soon"}, "schema.pattern"),
    ],
)
def test_the_schema_bounds_the_label_and_the_retries(
    sampled: dict[str, Any], location: str, value: Any, problem: str
) -> None:
    sampled["key"]["source"][location] = value
    assert problem in {code for _, code, _, _ in _issues(sampled)}


def _set(doc: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    *parents, last = (int(step) if step.isdigit() else step for step in path.split("/"))
    node = doc
    for step in parents:
        node = node[step]
    node[last] = value  # type: ignore[index]
    return doc


FRAME = "key/source/source"
UNION = f"{FRAME}/sources"
NOT = "not supported: "


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (UNION + "/1", {"kind": "list", "keys": ["C00000001"]}, [(f"/{UNION}/1/kind", "unsupported",
         NOT + "hash_sample over an inline list; sample a side's table or key listing")]),
        (UNION + "/1", {"kind": "csv", "uri": "s3://b/k.csv", "column": "id"},
         [(f"/{UNION}/1/kind", "unsupported",
           NOT + "key source 'csv'; use list, paginated_endpoint or a key table")]),
        (UNION + "/0/side", "b", [
            (f"/{UNION}/0/side", "frame_member", "side b is not a table side"),
            (f"/{UNION}/1", "frame_member",
             "union sources 0 and 1 both enumerate side b: a union takes one source per side"),
        ]),
        (UNION + "/1/target", "master-data", [(f"/{UNION}/1/target", "frame_member",
         "the frame's listing reads target master-data, which is neither side's: each frame source enumerates"
         " side a's or side b's keys")]),
        (UNION + "/1/strataFields", {"flagged": "$.flagged", "changed_at": "$.c"}, [(f"/{UNION}/1",
         "strata_column_missing",
         "the strata read column 'degree', which source 1 does not expose (strataColumns or strataFields)")]),
        (UNION + "/0/strataColumns", ["degree", "flagged"], [(f"/{UNION}/0", "strata_column_missing",
         "the strata read column 'changed_at', which source 0 does not expose (strataColumns or"
         " strataFields)")]),
        ("key/source/strata/0/buckets", [], [("/key/source/strata/0", "stratum_needs_buckets",
         "a stratum dimension of a hash sample needs buckets: a value read from the data would become a label"
         " in reports and packs (a bucket without values, min or max catches the rest)")]),
        ("execution", {"stop": {"maxKeys": 5000}}, [("/execution/stop/maxKeys", "sample_cut",
         "maxKeys would cut the sample: size it with targetSize or rate")]),
        ("key/source/strata/0/buckets/2/targetSize", 1_137_764, [("/key/source/strata", "sample_size",
         "the sample would hold 1,204,000 keys, over 1,000,000: compare the population as a census")]),
        ("key/source/strata/0/buckets/2/label", "o" * 231, [("/key/source/strata", "stratum_label_long",
         "the longest stratum label these dimensions make holds 257 characters, over 256")]),
    ],
)  # fmt: skip
def test_each_refusal(
    sampled: dict[str, Any], path: str, value: Any, expected: list[tuple[str, str, str]]
) -> None:
    got = [(p, code, message) for p, code, severity, message in _issues(_set(sampled, path, value))]
    assert got == expected


def test_table_and_union_only_in_a_hash_sample(sampled: dict[str, Any]) -> None:
    frame = sampled["key"]["source"]["source"]
    sampled["key"]["source"] = frame
    assert _issues(sampled) == [
        ("/key/source/kind", "unsupported", "error",
         NOT + "a union outside hash_sample; wrap it in a hash_sample"),
        ("/key/source/sources/0/kind", "unsupported", "error",
         NOT + "a table key source outside hash_sample (a table side's keys are scanned, or checked against"
         " it)"),
    ]  # fmt: skip


def test_not_between_two_table_sides(sampled: dict[str, Any]) -> None:
    sampled["sides"]["b"] = {
        "role": "new",
        "target": "legacy-db",
        "table": {"name": "customers", "keyColumns": ["id"]},
    }
    sampled["key"]["source"]["source"] = {
        "kind": "table",
        "side": "a",
        "strataColumns": ["degree", "flagged", "changed_at"],
    }
    assert [(p, m) for p, _, _, m in _issues(sampled)] == [
        ("/key/source/kind", NOT + "hash_sample between two table sides: their scan compares every row")
    ]


@pytest.mark.parametrize(
    ("location", "predicate"),
    [("knownDiffs", [{"id": "KD-1", "when": "strata.flagged == 'Y'", "classification": "accepted",
                      "reason": "a long reason", "ticket": "CUST-1"}]),
     ("compare/valueFilter", "stratum != 'degree=d0' || value != null")],
)  # fmt: skip
def test_a_predicate_over_strata_is_refused_only_with_a_sample(
    sampled: dict[str, Any], minimal_spec: dict[str, Any], location: str, predicate: Any
) -> None:
    _set(sampled, location, predicate) if "/" in location else sampled.update({location: predicate})
    at = "/knownDiffs/0/when" if location == "knownDiffs" else "/compare/valueFilter"
    message = NOT + "a predicate over stratum or strata; strata label a sample's keys, not their findings"
    assert [(p, c, m) for p, c, _, m in _issues(sampled)] == [(at, "unsupported", message)]
    # without a sample, stratum and strata stay bound as null: every stored spec recompiles unchanged
    _set(minimal_spec, location, predicate) if "/" in location else minimal_spec.update({location: predicate})
    assert validate_spec(minimal_spec) == [] and compile_spec(minimal_spec).sample is None


def test_an_undersized_bucket_is_warned(sampled: dict[str, Any]) -> None:
    buckets = sampled["key"]["source"]["strata"][0]["buckets"]
    buckets[0]["targetSize"] = 2000
    buckets[1]["targetSize"] = 2995  # enough clean keys, with less margin
    sampled["key"]["source"]["strata"][1]["buckets"][0]["targetSize"] = 29_955
    assert _issues(sampled) == [
        ("/key/source/strata/0/buckets/0/targetSize", "undersized", "warning",
         "bucket degree=d0 (standard, 0.1%) draws at most 2,000 keys; a zero-failure pass needs 2,995 cleanly"
         " compared keys (draw 3,011): it passes only as a census"),
        ("/key/source/strata/1/buckets/0/targetSize", "undersized", "warning",
         "bucket flagged=Y (high, 0.01%) draws at most 29,955 keys; a zero-failure pass needs 29,956 cleanly"
         " compared keys (draw 30,107): it passes only as a census"),
    ]  # fmt: skip
    compile_spec(sampled)  # a warning


def test_a_key_shaped_label_is_warned(sampled: dict[str, Any]) -> None:
    """Labels, and the values and bounds buckets match (a watch-listed account pinned as a number too),
    which a pack's gate reads as text."""
    sampled["key"].update(pattern="^[0-9]{4,8}$", **{"class": "account_number"})
    sampled["key"]["source"]["salt"] = "cutover-2026"
    sampled["key"]["source"]["strata"][0]["buckets"][0]["label"] = "d0_12345"
    sampled["key"]["source"]["strata"][0]["buckets"][1]["max"] = 40012345
    sampled["key"]["source"]["strata"][1]["buckets"][0]["values"] = ["Y", "40012346"]
    shaped = "is shaped like a key of this spec: a pack would refuse it"
    assert _issues(sampled) == [
        ("/key/source/salt", "label_key_shaped", "warning",
         "the label cutover-2026 (salt) is shaped like a key of this spec: a pack would refuse it"
         "; rename it"),
        ("/key/source/strata/0/buckets/0/label", "label_key_shaped", "warning",
         "the label d0_12345 (a bucket of degree) is shaped like a key of this spec: a pack would refuse it"
         "; rename it"),
        ("/key/source/strata/0/buckets/1/max", "value_key_shaped", "warning",
         f"the value 40012345 (a bucket of degree) {shaped}; bucket without customer keys"),
        ("/key/source/strata/1/buckets/0/values/1", "value_key_shaped", "warning",
         f"the value 40012346 (a bucket of flagged) {shaped}; bucket without customer keys"),
    ]  # fmt: skip
    sampled["key"]["class"] = "business_code"  # codes are stored in clear and never searched
    assert _issues(sampled) == []


def test_hash_sampling_mode_stays_unsupported(sampled: dict[str, Any]) -> None:
    sampled["sampling"] = {"mode": "hash", "rate": 0.1, "salt": "mvp-sample"}
    assert [(p, c) for p, c, _, _ in _issues(sampled)] == [("/sampling/mode", "unsupported")]
