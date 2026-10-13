"""Each seeded discrepancy is present, alone, for every dataset seed.

The checks go through the specs' own select + JSONata maps (``mapped``), so they prove the mocks
and the specs agree: every unseeded key maps to one canonical document on both variants, and each
seeded key differs exactly where expected.json says it does.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import phonenumbers
import pytest

from mocks.common import JSONBody, Reply, ScriptedDataset, Variant
from mocks.customer.dataset import SEEDED_KEYS as CUSTOMER_KEYS
from mocks.customer.dataset import Seed as CustomerSeed
from mocks.fixtures import recorded_reply
from mocks.linker.dataset import SEEDED_DEVICE, SEEDED_DEVICE_REISSUED
from mocks.linker.dataset import SEEDED_KEYS as LINKER_KEYS
from mocks.linker.dataset import Seed as LinkerSeed
from mocks.linker.model import LINK_CAP_B
from mocks.registry import PAIRS, load_pair

SEEDS = (20260924, 1, 7)
Mapped = Callable[[str, Variant, JSONBody], Any]


@pytest.fixture(scope="module", params=SEEDS, ids=lambda s: f"seed{s}")
def world(request: pytest.FixtureRequest) -> dict[str, ScriptedDataset]:
    return {pair: load_pair(pair).build_dataset(request.param) for pair in PAIRS}


def only(script: tuple[Reply, ...]) -> Reply:
    assert len(script) == 1, "expected a single, time-independent reply"
    return script[0]


def canonical(mapped: Mapped, ds: ScriptedDataset, variant: Variant, key: str) -> Any:
    reply = recorded_reply(ds.script(variant, key))
    assert reply.status == 200
    return mapped(ds.pair, variant, reply.body)


def by_code(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {link["code"]: link for link in doc["links"]}


def e164(number: str) -> str:
    return phonenumbers.format_number(phonenumbers.parse(number, "GB"), phonenumbers.PhoneNumberFormat.E164)


def identifier_changes(
    a: dict[str, Any], b: dict[str, Any], kind: str
) -> dict[str, tuple[set[str], set[str]]]:
    """Link code -> (values only a shares, values only b shares) of one identifier type, where
    they differ."""
    ids_a, ids_b = by_code(a), by_code(b)
    assert set(ids_a) == set(ids_b)
    changes = {}
    for code in ids_a:
        va = {i["value"] for i in ids_a[code]["sharedIdentifiers"][kind]}
        vb = {i["value"] for i in ids_b[code]["sharedIdentifiers"][kind]}
        if va != vb:
            changes[code] = (va - vb, vb - va)
    return changes


def without_identifiers(doc: dict[str, Any], kind: str) -> dict[str, Any]:
    """The canonical document with one identifier type emptied on every link."""
    return {
        **doc,
        "links": [
            {**link, "sharedIdentifiers": {**link["sharedIdentifiers"], kind: []}} for link in doc["links"]
        ],
    }


@pytest.mark.parametrize("pair", PAIRS)
def test_unseeded_keys_are_identical_after_mapping(
    world: dict[str, ScriptedDataset], mapped: Mapped, pair: str
) -> None:
    ds = world[pair]
    seeded = set(ds.seeded.values())
    for key in ds.keys:
        if key in seeded:
            continue
        a, b = ds.script("a", key), ds.script("b", key)
        assert len(a) == len(b) == 1, key
        assert mapped(pair, "a", only(a).body) == mapped(pair, "b", only(b).body), key


@pytest.mark.parametrize("pair", PAIRS)
def test_seeded_keys_are_in_the_sample_and_distinct(world: dict[str, ScriptedDataset], pair: str) -> None:
    ds = world[pair]
    assert len(set(ds.seeded.values())) == len(ds.seeded)
    assert set(ds.seeded.values()) <= set(ds.keys)
    assert len(set(ds.keys)) == len(ds.keys)


class TestLinkerSeeds:
    def test_missing_link(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.MISSING_LINK]
        a, b = by_code(canonical(mapped, ds, "a", key)), by_code(canonical(mapped, ds, "b", key))
        assert len(a) - len(b) == 1 and set(b) < set(a)
        assert all(a[code] == b[code] for code in b)

    def test_extra_link(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.EXTRA_LINK]
        a, b = by_code(canonical(mapped, ds, "a", key)), by_code(canonical(mapped, ds, "b", key))
        assert len(b) - len(a) == 1 and set(a) < set(b)
        assert all(a[code] == b[code] for code in a)

    def test_normalisation_only(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.NORMALISATION_ONLY]
        a, b = canonical(mapped, ds, "a", key), canonical(mapped, ds, "b", key)
        assert list(identifier_changes(a, b, "phones").values()) == [({"07700 900123"}, {"+447700900123"})]
        assert e164("07700 900123") == "+447700900123"
        assert without_identifiers(a, "phones") == without_identifiers(b, "phones")

    def test_device_id_changed(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.DEVICE_ID_CHANGED]
        a, b = canonical(mapped, ds, "a", key), canonical(mapped, ds, "b", key)
        changes = identifier_changes(a, b, "devices")
        assert list(changes.values()) == [({SEEDED_DEVICE}, {SEEDED_DEVICE_REISSUED})]
        assert without_identifiers(a, "devices") == without_identifiers(b, "devices")
        # Why the seed exists: phone-number parsing reads both serials as one number, so the
        # difference only survives when device ids are never phone-normalised.
        assert e164(SEEDED_DEVICE) == e164(SEEDED_DEVICE_REISSUED)

    def test_flag_mismatch(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.FLAG_MISMATCH]
        a, b = by_code(canonical(mapped, ds, "a", key)), by_code(canonical(mapped, ds, "b", key))
        assert set(a) == set(b)
        changed = [code for code in a if a[code] != b[code]]
        assert len(changed) == 1
        (code,) = changed
        assert (a[code]["flagged"], b[code]["flagged"]) == (True, False)
        assert {**a[code], "flagged": False} == b[code]

    def test_drift_is_stale_on_the_first_call_only(
        self, world: dict[str, ScriptedDataset], mapped: Mapped
    ) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.DRIFT]
        a = mapped("linker", "a", only(ds.script("a", key)).body)
        first, *later = ds.script("b", key)
        assert len(later) == 1 and first.status == later[0].status == 200
        stale, current = mapped("linker", "b", first.body), mapped("linker", "b", later[0].body)
        assert current == a
        assert stale["name"] != a["name"] and {**stale, "name": a["name"]} == a

    def test_truncation(self, world: dict[str, ScriptedDataset], specs: dict[str, Any]) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.TRUNCATION]
        a, b = only(ds.script("a", key)).body, only(ds.script("b", key)).body
        assert len(a["links"]) == a["linkCount"] == 57
        assert a["linkCount"] <= specs["linker"]["sides"]["a"]["truncation"]["cap"]
        assert (
            len(b["data"]["linkedEntities"])
            == LINK_CAP_B
            == specs["linker"]["sides"]["b"]["truncation"]["cap"]
        )
        assert b["data"]["totalLinked"] == 57

    def test_only_the_hub_reaches_a_cap(self, world: dict[str, ScriptedDataset]) -> None:
        ds = world["linker"]
        for key in ds.keys:
            a, b = recorded_reply(ds.script("a", key)), recorded_reply(ds.script("b", key))
            if a.status == 200:
                assert a.body["linkCount"] == len(a.body["links"]), key
            if b.status == 200 and key != LINKER_KEYS[LinkerSeed.TRUNCATION]:
                assert b.body["data"]["totalLinked"] == len(b.body["data"]["linkedEntities"]) < LINK_CAP_B, (
                    key
                )

    def test_one_sided_404(
        self, world: dict[str, ScriptedDataset], absence_shape: Callable[..., bool]
    ) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.ONE_SIDED_404]
        assert only(ds.script("a", key)).status == 200
        b = only(ds.script("b", key))
        assert b.status == 404 and absence_shape("linker", "linker-b", b.body)

    def test_unannounced_404(
        self, world: dict[str, ScriptedDataset], absence_shape: Callable[..., bool]
    ) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.UNANNOUNCED_404]
        a = only(ds.script("a", key))
        assert a == Reply(404, {"message": "no route"})
        assert not absence_shape("linker", "linker-a", a.body)
        assert only(ds.script("b", key)).status == 200

    def test_server_error_then_success(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.SERVER_ERROR_THEN_SUCCESS]
        first, then = ds.script("b", key)
        assert first == Reply(503, {"error": "UNAVAILABLE"}) and then.status == 200
        assert mapped("linker", "b", then.body) == mapped("linker", "a", only(ds.script("a", key)).body)

    def test_absent_both(self, world: dict[str, ScriptedDataset], absence_shape: Callable[..., bool]) -> None:
        ds, key = world["linker"], LINKER_KEYS[LinkerSeed.ABSENT_BOTH]
        for variant, target in (("a", "linker-a"), ("b", "linker-b")):
            reply = only(ds.script(variant, key))  # type: ignore[arg-type]
            assert reply.status == 404 and absence_shape("linker", target, reply.body)

    def test_device_ids_carry_digit_runs(self, world: dict[str, ScriptedDataset]) -> None:
        """Device ids look like real serials, so the ground truth exercises what a phone normaliser
        would misread (the spec keeps e164 off them; test_mock_linker_identifiers checks it)."""
        ds = world["linker"]
        devices = {
            ident["value"]
            for key in ds.keys
            for link in recorded_reply(ds.script("a", key)).body.get("links", [])
            for ident in link["sharedIdentifiers"]
            if ident["idType"] == "DEVICE"
        }
        assert SEEDED_DEVICE in devices and len(devices) > 10
        assert all(re.search(r"[0-9]{4}", value) for value in devices)


def _without(doc: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in fields}


class TestCustomerSeeds:
    def pair_docs(
        self, world: dict[str, ScriptedDataset], mapped: Mapped, seed: CustomerSeed
    ) -> tuple[Any, Any]:
        ds, key = world["customer"], CUSTOMER_KEYS[seed]
        return canonical(mapped, ds, "a", key), canonical(mapped, ds, "b", key)

    def test_consent_granted_mismatch(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.CONSENT_GRANTED_MISMATCH)
        assert _without(a, "consents") == _without(b, "consents")
        assert [(c["purpose"], c["granted"]) for c in a["consents"]] == [("MARKETING", True)]
        assert [(c["purpose"], c["granted"]) for c in b["consents"]] == [("MARKETING", False)]
        assert _without(a["consents"][0], "granted") == _without(b["consents"][0], "granted")

    def test_account_missing_in_v2(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.ACCOUNT_MISSING_IN_V2)
        assert _without(a, "accounts") == _without(b, "accounts")
        assert len(a["accounts"]) == 2 and len(b["accounts"]) == 1 and b["accounts"][0] in a["accounts"]

    def test_tags_case_only(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.TAGS_CASE_ONLY)
        assert _without(a, "tags") == _without(b, "tags")
        assert set(a["tags"]) != set(b["tags"])
        assert {t.lower() for t in a["tags"]} == {t.lower() for t in b["tags"]}

    def test_phone_format_only(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.PHONE_FORMAT_ONLY)
        assert _without(a, "phones") == _without(b, "phones")

        def e164(number: str) -> str:
            return phonenumbers.format_number(
                phonenumbers.parse(number, "GB"), phonenumbers.PhoneNumberFormat.E164
            )

        raw_a = {(p["kind"], p["number"]) for p in a["phones"]}
        raw_b = {(p["kind"], p["number"]) for p in b["phones"]}
        assert raw_a != raw_b
        assert {(k, e164(n)) for k, n in raw_a} == {(k, e164(n)) for k, n in raw_b}

    def test_balance_scale_only(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.BALANCE_SCALE_ONLY)
        assert _without(a, "accounts") == _without(b, "accounts")
        (acc_a,), (acc_b,) = a["accounts"], b["accounts"]
        assert (acc_a["balance"], acc_b["balance"]) == ("1250.10", 1250.1)
        assert Decimal(acc_a["balance"]).quantize(Decimal("0.01")) == Decimal(
            repr(acc_b["balance"])
        ).quantize(Decimal("0.01"))
        assert _without(acc_a, "balance") == _without(acc_b, "balance")

    def test_created_at_ms_only(self, world: dict[str, ScriptedDataset], mapped: Mapped) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.CREATED_AT_MS_ONLY)
        assert _without(a, "createdAt") == _without(b, "createdAt")
        assert a["createdAt"].endswith("Z") and b["createdAt"] == a["createdAt"][:-1] + ".000Z"

    def test_one_sided_404(
        self, world: dict[str, ScriptedDataset], absence_shape: Callable[..., bool]
    ) -> None:
        ds, key = world["customer"], CUSTOMER_KEYS[CustomerSeed.ONE_SIDED_404]
        assert only(ds.script("a", key)).status == 200
        b = only(ds.script("b", key))
        assert b.status == 404 and absence_shape("customer", "customer-b", b.body)

    def test_segment_known_diff(
        self, world: dict[str, ScriptedDataset], mapped: Mapped, specs: dict[str, Any], known_diff_env: Any
    ) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.SEGMENT_KNOWN_DIFF)
        assert _without(a, "segment") == _without(b, "segment")
        assert (a["segment"], b["segment"]) == ("PRIVATE", "WEALTH")
        rule = next(r for r in specs["customer"]["knownDiffs"] if r["id"] == "KD-CP-0001")
        finding = {"key": "k", "path": "$.segment", "pointer": "/segment", "change_type": "value_changed",
                   "direction": "both", "a": "PRIVATE", "b": "WEALTH", "value_class": "other"}  # fmt: skip
        assert known_diff_env.compile(rule["when"]).eval(data=finding).plain_value() is True
        assert (
            known_diff_env.compile(rule["when"]).eval(data={**finding, "b": "RETAIL"}).plain_value() is False
        )

    def test_sms_preference_missing(
        self, world: dict[str, ScriptedDataset], mapped: Mapped, specs: dict[str, Any], known_diff_env: Any
    ) -> None:
        a, b = self.pair_docs(world, mapped, CustomerSeed.SMS_PREFERENCE_MISSING)
        assert _without(a, "preferences") == _without(b, "preferences")
        assert a["preferences"]["marketing"]["sms"] is False
        assert "sms" not in b["preferences"]["marketing"]
        assert [p["kind"] for p in a["phones"]] == ["HOME"]  # the rule's story: no mobile number
        rule = next(r for r in specs["customer"]["knownDiffs"] if r["id"] == "KD-CP-0002")
        finding = {"key": "k", "path": "$.preferences.marketing.sms", "pointer": "/preferences/marketing/sms",
                   "change_type": "missing_in_b", "direction": "a_only", "a": False, "b": None,
                   "value_class": "null_vs_absent"}  # fmt: skip
        assert known_diff_env.compile(rule["when"]).eval(data=finding).plain_value() is True

    def test_counts_never_signal_truncation(self, world: dict[str, ScriptedDataset]) -> None:
        ds = world["customer"]
        for key in ds.keys:
            a, b = recorded_reply(ds.script("a", key)), recorded_reply(ds.script("b", key))
            if a.status == 200:
                assert a.body["customer"]["accountCount"] == len(a.body["customer"]["accounts"]), key
            if b.status == 200:
                assert b.body["data"]["productCount"] == len(b.body["data"]["products"]), key

    def test_every_balance_but_the_seeded_one_is_a_number(self, world: dict[str, ScriptedDataset]) -> None:
        ds = world["customer"]
        texts = [
            (key, acc["balance"])
            for key in ds.keys
            for acc in recorded_reply(ds.script("a", key)).body.get("customer", {}).get("accounts", [])
            if isinstance(acc["balance"], str)
        ]
        assert texts == [(CUSTOMER_KEYS[CustomerSeed.BALANCE_SCALE_ONLY], "1250.10")]
