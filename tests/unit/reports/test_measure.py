"""Contract measurements and sound capability proposals."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from deltasleuth.context import Context
from deltasleuth.core.canonical import predicates
from deltasleuth.core.types import AuthoritativeAbsence, Observation, Side, TargetCapabilities
from deltasleuth.reports.measure import (
    MEASURE_NAME,
    PROPOSAL_NAME,
    capabilities_yaml,
    distribution,
    measure,
    measure_run,
)

from .conftest import SECRET_NAME, doc, seed_run, unit_spec

GATEWAY = {"message": "no Route matched with those values"}
SPEC = unit_spec()
DECLARED = TargetCapabilities(
    authoritative_absence=AuthoritativeAbsence(status=(404,), shapes=("nf",)),
    absence_shapes={"nf": 'has(body.error) && body.error == "NOT_FOUND"'},
)


def obs(key: str, side: Side, status: int, body: Any, **kw: Any) -> Observation:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Observation(key, side, status, raw, "2026-09-24T10:00:00Z", **kw)


def run(found: list[Observation], caps: TargetCapabilities | None = None, **kw: Any) -> dict[str, Any]:
    """Measure with the unit spec; both targets declare ``caps``."""
    caps = caps or TargetCapabilities()
    return measure(found, spec=SPEC, capabilities={"svc-a": caps, "svc-b": caps}, **kw)


def fields(m: dict[str, Any], target: str) -> dict[str, dict[str, Any]]:
    return {p["field"]: p for p in m["proposed_capabilities"][target]}


def gateway_mix() -> list[Observation]:
    """Side b: 30 documents, 8 empty-body 404s from the service, 2 gateway 404s."""
    found = [obs(f"K{i:03d}", Side.B, 200, {"id": i}) for i in range(1, 31)]
    found += [obs(f"K{i:03d}", Side.B, 404, b"") for i in range(31, 39)]
    return found + [obs(f"K{i:03d}", Side.B, 404, GATEWAY) for i in (39, 40)]


def test_distribution_is_nearest_rank() -> None:
    assert distribution([float(v) for v in range(1, 101)]) == {
        "count": 100,
        "max": 100.0,
        "p50": 50.0,
        "p95": 95.0,
        "p99": 99.0,
    }
    assert distribution([]) is None


def test_declared_absence_flags_unannounced_shapes_and_never_quotes_values() -> None:
    m = run(
        [obs("K1", Side.A, 404, {"error": "NOT_FOUND"}), obs("K2", Side.A, 404, {"error": "NOT_FOUND"}),
         obs("K3", Side.A, 404, {"message": SECRET_NAME}), obs("K4", Side.A, 200, {"id": "K4"})],
        caps=DECLARED,
    )  # fmt: skip
    absence = fields(m, "svc-a")["authoritative_absence"]
    assert absence["value"] == {"status": [404], "shapes": ["nf"]}
    (flag,) = absence["flags"]
    assert "UNANNOUNCED" in flag and "{message}" in flag
    assert SECRET_NAME not in json.dumps(m) and SECRET_NAME not in capabilities_yaml(m)


def test_declared_shape_sees_status_and_headers() -> None:
    marked = TargetCapabilities(absence_shapes={"marked": 'status == 404 && headers["x-absent"] == "yes"'})
    found = [
        obs("K1", Side.A, 404, {"error": 1}, headers={"x-absent": "yes"}),
        obs("K2", Side.A, 404, {"error": 1}),
    ]
    m = run(found, caps=marked)
    shapes = [(s["body_keys"], s["count"], s["declared_shapes"]) for s in m["sides"]["a"]["not_found_shapes"]]
    assert shapes == [("{error}", 1, ["marked"]), ("{error}", 1, [])]  # same names, different answers
    (flag,) = fields(m, "svc-a")["authoritative_absence"]["flags"]
    assert flag.startswith("UNANNOUNCED not-found shape: 1 HTTP 404")


def test_absence_is_proposed_only_from_one_known_missing_shape() -> None:
    absence = fields(run(gateway_mix()), "svc-b")["authoritative_absence"]
    assert absence["value"] is None
    assert any("HTTP 404 <empty> x8; HTTP 404 {message} x2" in f for f in absence["flags"])
    assert any("--missing-keys" in f for f in absence["flags"])
    empty = fields(run(gateway_mix(), missing_keys={"K031", "K032"}), "svc-b")["authoritative_absence"]
    assert empty["value"] is None and any("status-only declaration" in f for f in empty["flags"])
    mixed = fields(run(gateway_mix(), missing_keys={"K031", "K039"}), "svc-b")["authoritative_absence"]
    assert mixed["value"] is None and any("did not all answer with one" in f for f in mixed["flags"])
    found = fields(run(gateway_mix(), missing_keys={"K001"}), "svc-b")["authoritative_absence"]
    assert found["value"] is None and any("(HTTP 200 x1)" in f for f in found["flags"])
    service, gateway = {"error": "NOT_FOUND"}, {"error": "Bad Gateway", "path": "/v2/x"}
    answers = [obs(f"K{i}", Side.B, 404, service) for i in range(3)] + [obs("K9", Side.B, 404, gateway)]
    proposed = fields(run(answers, missing_keys={"K0", "K1", "K2"}), "svc-b")
    assert proposed["authoritative_absence"]["value"] == {"status": [404], "shapes": ["shape_error"]}
    assert any(f.startswith("needs human confirmation") for f in proposed["authoritative_absence"]["flags"])
    (predicate,) = proposed["absence_shapes"]["value"].values()
    assert predicates.test(predicate, {"body": service}) and not predicates.test(predicate, {"body": gateway})


def test_truncation_cap_needs_a_count_that_exceeded_its_array_at_the_cap() -> None:
    def cap(docs: list[dict[str, Any]]) -> dict[str, Any]:
        return fields(run([obs(f"K{i}", Side.A, 200, d) for i, d in enumerate(docs)]), "svc-a")[
            "truncation_indicators"
        ]

    sound = cap([{"items": [0] * n, "totalCount": t} for n, t in [(3, 3), (5, 5), (5, 8)]])
    assert sound["value"] == {"cap": 5, "items_path": "$.items", "count_path": "$.totalCount"}
    tracking = cap([{"items": [0] * i, "itemCount": i} for i in range(1, 4)])
    assert tracking["value"] is None and "never exceeded" in tracking["flags"][0]
    echo = cap([{"orders": [0] * (20 if i < 10 else i % 19 + 1), "pageSize": 20} for i in range(40)])
    assert echo["value"] is None  # a page-size echo is never a count
    below = cap([{"orders": [0] * 20, "total": 20}] * 5 + [{"orders": [0] * 19, "total": 30}] * 5)
    assert below["value"] is None and any("returned up to 20 items" in f for f in below["flags"])
    varied = cap(
        [{"items": [1, 2, 3], "total": 3}, {"items": [1, 2], "total": 9}, {"items": [1, 2, 3], "total": 9}]
    )
    assert varied["value"] is None and any("varied" in f for f in varied["flags"])


def test_measure_ignores_flags() -> None:
    """A side's truncation flags are the spec's to declare: a measurement reads none, and proposes no cap
    from one."""
    flagged = {s: dataclasses.replace(cfg, truncation_flags=("$.more",)) for s, cfg in SPEC.sides.items()}
    found = [obs(f"K{i}", Side.A, 200, {"items": [0] * 5, "more": i > 1}) for i in range(4)]
    caps = {"svc-a": TargetCapabilities(), "svc-b": TargetCapabilities()}
    m = measure(found, spec=dataclasses.replace(SPEC, sides=flagged), capabilities=caps)
    assert m == run(found)
    assert fields(m, "svc-a")["truncation_indicators"]["value"] is None
    assert m["sides"]["a"]["truncation_candidates"] == []


@pytest.mark.parametrize(
    ("body", "headers", "expected"),
    [({"nextCursor": "c"}, {}, "cursor"), ({}, {"link": '<https://x/p2>; rel="next"'}, "link_header"),
     ({"page": 1}, {}, "page"), ({"offset": 0}, {}, "offset"), ({}, {}, "none")],
)  # fmt: skip
def test_pagination_hints(body: dict[str, Any], headers: dict[str, str], expected: str) -> None:
    m = run([obs("K1", Side.A, 200, {"items": [], **body}, headers=headers)])
    assert fields(m, "svc-a")["pagination"]["value"] == expected


def test_statuses_errors_latency_and_sizes() -> None:
    side = run(
        [obs("K1", Side.A, 200, {"x": 1}, latency_ms=10.0), obs("K2", Side.A, 200, {"x": 1}, latency_ms=30.0),
         dataclasses.replace(obs("K3", Side.A, 503, b""), body=None, error_class="http_5xx", attempts=3),
         obs("K4", Side.A, 200, {"x": 1}, latency_ms=99.0, source="file")]  # file latency is not measured
    )["sides"]["a"]  # fmt: skip
    assert side["statuses"] == {"200": 3, "error:http_5xx": 1} and side["attempts"] == {"1": 3, "3": 1}
    assert side["latency_ms"] == {"count": 2, "max": 30.0, "p50": 10.0, "p95": 30.0, "p99": 30.0}
    assert side["body_bytes"]["count"] == 3


@pytest.mark.parametrize("brk", ["\n", "\r", "\r\n", "\x85", "\u2028", "\u2029"])
def test_capabilities_yaml_is_injection_safe(brk: str) -> None:
    inject = "\n      authoritative_absence: {status: [404, 500], shapes: []}\n      #".replace("\n", brk)
    undeclared = [obs("K1", Side.B, 404, {"error" + inject: "x"}), obs("K2", Side.B, 200, {"id": 2})]
    unannounced = [
        obs("K1", Side.B, 404, {"error": "NOT_FOUND"}),
        obs("K2", Side.B, 404, {"detail" + inject: 1}),
    ]
    for m in (run(undeclared), run(unannounced, caps=DECLARED)):
        text = capabilities_yaml(m)
        proposed = {
            p["field"]: p["value"] for p in m["proposed_capabilities"]["svc-b"] if p["value"] is not None
        }
        assert yaml.safe_load(text) == {"targets": {"svc-b": {"capabilities": proposed}}}
        assert text.isascii() and len(text.splitlines()) == text.count("\n")


@pytest.mark.db
async def test_measure_run_publishes_masked_patterns_and_the_known_missing_shape(
    ctx: Context, tmp_path: Path
) -> None:
    spec = unit_spec(
        security={"unlisted": "tokenize", "clear": ["$.status", "$.tags[*]", "$.id"],
                  "tokenize": [{"path": "$.name", "class": "name"}, {"path": "$.verified", "class": "email"}]}
    )  # fmt: skip
    samples = {
        f"K00{i}": ({**doc(f"K00{i}"), "verified": {email: {"devices": [1] * i}}}, doc(f"K00{i}"))
        for i, email in ((1, "jane@example.org"), (2, "joe@example.org"))
    }
    run_id = await seed_run(ctx, {**samples, "K009": (doc("K009"), (404, {"error": "NOT_FOUND"}))}, spec=spec)
    m = await measure_run(ctx, run_id, tmp_path, missing_keys={"K009"})
    assert m["sides"]["a"]["fanout_canonical"]["$.verified[*][*]"] == {
        "docs": 2,
        "max": 2,
        "p95": 2,
        "mean": 1.5,
    }
    assert fields(m, "svc-b")["authoritative_absence"]["value"] == {
        "status": [404],
        "shapes": ["shape_error"],
    }
    for name in (MEASURE_NAME, PROPOSAL_NAME):
        text = (tmp_path / name).read_text()
        assert "example.org" not in text and SECRET_NAME not in text
    assert json.loads((tmp_path / MEASURE_NAME).read_text())["run_id"] == run_id
