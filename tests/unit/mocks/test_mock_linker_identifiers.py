"""Linker shared identifiers are typed before they are normalised.

``phonenumbers`` reads a number out of arbitrary text: the device serial DEV-7700-900555-A parses
as +447700900555, and so does DEV-7700-900555-B. An e164 normaliser on a path that also holds
device ids (or e-mail addresses) can therefore merge two distinct identifiers into one keyed
member and report a changed identifier as MATCH. The linker spec's maps split the identifiers by
type, so e164 only ever sees phones; these tests hold the spec to that, through its own maps and
through the real engine.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.classify.outcome import compare_pair
from deltasleuth.core.spec import compile_spec, load_spec_file, load_targets
from deltasleuth.core.types import KeyResult, Observation, Outcome, Side
from mocks.common import JSONBody, ScriptedDataset, Variant, encode_body
from mocks.linker.dataset import SEEDED_DEVICE, SEEDED_DEVICE_REISSUED
from mocks.linker.model import Entity, Identifier, Link, legacy_body, new_body
from mocks.registry import pair_dir

Mapped = Callable[[str, Variant, JSONBody], Any]
KEY = "ORG-000001"
PARTNER = "ORG-000002"


def _normalisers(rule: dict[str, Any]) -> set[str]:
    """Names of the normalisers a field rule applies (plain names or one-key option maps)."""
    return {n if isinstance(n, str) else next(iter(n)) for n in rule.get("normalize", [])}


def _raw_phones(variant: Variant, body: JSONBody) -> set[str]:
    """The values each variant's own contract types as phones."""
    if variant == "a":
        return {
            i["value"] for link in body["links"] for i in link["sharedIdentifiers"] if i["idType"] == "PHONE"
        }
    return {
        m["value"]
        for link in body["data"]["linkedEntities"]
        for m in link["matchedOn"]
        if m["type"] == "phone"
    }


def _bodies(a_ids: list[tuple[str, str]], b_ids: list[tuple[str, str]]) -> tuple[JSONBody, JSONBody]:
    """One entity with one link, sharing ``a_ids`` on variant a and ``b_ids`` on variant b."""

    def entity(ids: list[tuple[str, str]]) -> Entity:
        shared = tuple(sorted(Identifier(t, v) for t, v in ids))
        return Entity(KEY, "Ada Brook", "PERSON", (Link(PARTNER, "COMPANY", False, shared),))

    return legacy_body(entity(a_ids)), new_body(entity(b_ids))


def test_e164_reaches_phone_identifiers_only(
    specs: dict[str, Any], datasets: dict[str, ScriptedDataset], mapped: Mapped, strict_jsonpath: Any
) -> None:
    e164_paths = [r["path"] for r in specs["linker"]["compare"]["fields"] if "e164" in _normalisers(r)]
    assert e164_paths, "phones must still be phone-normalised"
    ds, phones_seen = datasets["linker"], 0
    for key in ds.keys:
        for variant in ("a", "b"):
            for reply in ds.script(variant, key):  # type: ignore[arg-type]
                if reply.status != 200:
                    continue
                doc = mapped("linker", variant, reply.body)  # type: ignore[arg-type]
                reached = {value for path in e164_paths for value in strict_jsonpath.findall(path, doc)}
                phones = _raw_phones(variant, reply.body)  # type: ignore[arg-type]
                assert reached == phones, (key, variant, sorted(reached - phones))
                phones_seen += len(phones)
    assert phones_seen > 0


def test_every_identifier_lands_in_exactly_one_typed_array(mapped: Mapped) -> None:
    """Including a type neither contract lists: it is compared under ``other``, never dropped."""
    shared = [
        ("PHONE", "07700 900123"),
        ("EMAIL", "shared.aadb07@example.org"),
        ("DEVICE", "DEV-1234-ABCD"),
        ("FAX", "020 7946 0018"),
    ]
    body_a, body_b = _bodies(shared, shared)
    want = {
        "phones": [{"value": "07700 900123"}],
        "emails": [{"value": "shared.aadb07@example.org"}],
        "devices": [{"value": "DEV-1234-ABCD"}],
        "other": [{"idType": "FAX", "value": "020 7946 0018"}],
    }
    for variant, body in (("a", body_a), ("b", body_b)):
        (link,) = mapped("linker", variant, body)["links"]  # type: ignore[arg-type]
        assert link["sharedIdentifiers"] == want, variant


@pytest.fixture(scope="module")
def engine() -> Callable[[JSONBody, JSONBody], KeyResult]:
    """``engine(body_a, body_b)`` -> the real engine's result for KEY under the shipped spec."""
    targets = load_targets(pair_dir("linker") / "targets.yaml")
    spec = compile_spec(load_spec_file(pair_dir("linker") / "spec.yaml"), targets=targets)

    def compare(body_a: JSONBody, body_b: JSONBody) -> KeyResult:
        states = {}
        for side, body in ((Side.A, body_a), (Side.B, body_b)):
            cfg = spec.sides[side]
            obs = Observation(
                key=KEY,
                side=side,
                http_status=200,
                body=encode_body(body),
                observed_at="2026-09-24T09:00:00Z",
                source="file",
            )
            states[side] = observe(obs, cfg, spec, targets.get(cfg.target).capabilities)
        return compare_pair(KEY, states[Side.A], states[Side.B], spec, today=date(2026, 9, 24))

    return compare


def _summary(result: KeyResult) -> list[tuple[str | None, str, str]]:
    return sorted((f.taxonomy_code, f.change_type.value, f.path_pattern) for f in result.findings)


@pytest.mark.parametrize(
    ("kind", "value_a", "value_b"),
    [
        ("DEVICE", "ABCD-1234-5678", "WXYZ-1234-5678"),  # both parse as +4412345678
        ("DEVICE", "SN-2079460000-B", "SN-2079460000-C"),  # both parse as +442079460000
        ("DEVICE", SEEDED_DEVICE, SEEDED_DEVICE_REISSUED),  # both parse as +447700900555
        ("EMAIL", "ops07700900123@example.org", "ops07700900124@example.org"),
    ],
)
def test_distinct_digit_bearing_identifiers_stay_distinct_members(
    engine: Callable[[JSONBody, JSONBody], KeyResult], kind: str, value_a: str, value_b: str
) -> None:
    result = engine(*_bodies([(kind, value_a)], [(kind, value_b)]))
    array = f"$.links[*].sharedIdentifiers.{kind.lower()}s[*]"
    assert result.outcome is Outcome.MISMATCH
    assert _summary(result) == [
        ("FIELD_EXTRA", "extra_in_b", array),
        ("FIELD_MISSING", "missing_in_b", array),
    ]


def test_a_phone_in_another_format_is_still_normalisation_only(
    engine: Callable[[JSONBody, JSONBody], KeyResult],
) -> None:
    result = engine(*_bodies([("PHONE", "07700 900123")], [("PHONE", "+447700900123")]))
    assert result.outcome is Outcome.MATCH
    assert _summary(result) == [
        ("NORMALIZATION_ONLY", "value_changed", "$.links[*].sharedIdentifiers.phones[*].value")
    ]
