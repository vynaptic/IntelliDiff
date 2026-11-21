"""Procedural paired records with shuffled arrays, composite identities, renamed and one-sided fields."""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonata
import pytest
from jsonschema import Draft202012Validator

from deltasleuth.profiler import DraftSpec, EvidenceRow, bootstrap

PAIRS = 200
RECORD_ARGS = {
    "spec_name": "paired-records",
    "target_a": "record-a",
    "target_b": "record-b",
    "key_name": "record_id",
}
BATCH_ARGS = {
    "spec_name": "batch-a-vs-b",
    "target_a": "batch-a",
    "target_b": "batch-b",
    "key_name": "batch_id",
    "key_pattern": "^E[0-9]{7}$",
}
# Planted in every document; the tests prove it never reaches evidence, YAML or schemas.
SECRET = "tk_fake_Zq81xVv3K9aJ4mT2pW7c"  # noqa: S105 - fake value, not a credential

_PREFIXES = [f"prefix-{n}" for n in range(16)]
_SUFFIXES = [f"suffix-{n}" for n in range(16)]
_PATHS = [f"path-{n}" for n in range(5)]
_ZONES = [f"zone-{n}" for n in range(7)]
_CODES = [f"zone-{n}z" for n in range(7)]
_GROUP_CODES = [f"7777-441{n}" for n in range(4)]
_CLASSES = [f"CLASS-{n}" for n in range(4)]
_STATES = ["ON", "OFF"]
_MODES = [f"MODE-{n}" for n in range(4)]
_TAGS = [f"TAG-{n}" for n in range(5)]
_GROUPS = [f"GROUP-{n}" for n in range(3)]

# Planted truth, record pair (canonical = side a, relative to the select roots).
RECORD_SELECT = {"a": "$.record", "b": "$.data"}
RECORD_KEYS = {"$.channels": ["number"], "$.entries": ["groupCode", "slotNo"], "$.rules": ["mode"]}
RECORD_RENAMES = {
    "$.id": "$.recordId",
    "$.prefix": "$.profile.prefixText",
    "$.suffix": "$.profile.suffixText",
    "$.startDay": "$.profile.originDay",
    "$.email": "$.contact.emailAddress",
    "$.channels": "$.endpoints",
    "$.channels[*].kind": "$.endpoints[*].use",
    "$.channels[*].number": "$.endpoints[*].value",
    "$.location.line1": "$.coordinates.path",
    "$.location.zone": "$.coordinates.area",
    "$.location.zoneLabel": "$.coordinates.zoneCode",
    "$.location.region": "$.coordinates.regionCode",
    "$.categoryGroup": "$.categoryGroup",
    "$.createdAt": "$.createdAt",
    "$.entries": "$.slots",
    "$.entries[*].groupCode": "$.slots[*].slot.groupCode",
    "$.entries[*].slotNo": "$.slots[*].slot.number",
    "$.entries[*].category": "$.slots[*].classCode",
    "$.entries[*].status": "$.slots[*].state",
    "$.entries[*].amount": "$.slots[*].reading.value",
    "$.entries[*].unit": "$.slots[*].reading.unit",
    "$.boundsById": "$.bounds",
    "$.boundsById[*].low": "$.bounds[*].lowBound",
    "$.boundsById[*].high": "$.bounds[*].highBound",
    "$.rules": "$.rules",
    "$.rules[*].mode": "$.rules[*].mode",
    "$.rules[*].enabled": "$.rules[*].enabled",
    "$.rules[*].updatedAt": "$.rules[*].updatedAt",
    "$.tags": "$.labels",
    "$.apiToken": "$.accessToken",
}
RECORD_UNMATCHED = {"a": ["$.leftOnly"], "b": ["$.etag"]}
# Numbers and instants keep every digit: text vs number and ".000" vs none are only writing.
RECORD_NORMALISERS = {
    "$.channels[*].number": [{"e164": {"defaultRegion": "GB"}}],
    "$.email": ["email_basic"],
    "$.categoryGroup": ["upper"],
    "$.createdAt": [{"timestamp_utc": {"precision": "us", "mode": "truncate"}}],
    "$.entries[*].groupCode": ["digits_only"],
    "$.entries[*].amount": [{"decimal": {"scale": 38}}],
    "$.rules[*].mode": ["upper"],
    "$.rules[*].updatedAt": [{"timestamp_utc": {"precision": "us", "mode": "truncate"}}],
    "$.tags[*]": ["upper"],
    "$.location.zoneLabel": ["lower"],
    "$.location.region": ["lower"],
}

# Planted truth, batch pair.
BATCH_SELECT = {"a": "$.batch", "b": "$.result"}
BATCH_KEYS = {"$.parts": ["code"], "$.keys": ["type", "value"]}
BATCH_RENAMES = {
    "$.code": "$.batchCode",
    "$.name": "$.details.displayName",
    "$.shape": "$.details.kind",
    "$.flagged": "$.flags.flagged",
    "$.enabled": "$.flags.enabled",
    "$.weight": "$.metrics.weight",
    "$.parts": "$.links",
    "$.parts[*].code": "$.links[*].target.code",
    "$.parts[*].phase": "$.links[*].role",
    "$.parts[*].fraction": "$.links[*].fraction",
    "$.parts[*].since": "$.links[*].validFrom",
    "$.keys": "$.codes",
    "$.keys[*].type": "$.codes[*].idType",
    "$.keys[*].value": "$.codes[*].idValue",
}
BATCH_UNMATCHED = {"a": ["$.internalId", "$.parts[*].id"], "b": ["$.recordId", "$.links[*].id"]}


def _stamp(dt: datetime, fraction: bool) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (".000Z" if fraction else "Z")


def _phone(rng: random.Random, kind: str, used: set[int]) -> tuple[str, str]:
    n = rng.choice([x for x in range(1000) if x not in used])
    used.add(n)
    if kind == "MOBILE":
        return f"07700 900{n:03d}", f"+447700900{n:03d}"
    return f"020 7946 0{n:03d}", f"+442079460{n:03d}"


def _entries(
    rng: random.Random, doc: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Rows where neither groupCode nor slotNo alone is unique, plus the id-keyed bounds map."""
    a_entries: list[dict[str, Any]] = []
    b_slots: list[dict[str, Any]] = []
    limits_a: dict[str, Any] = {}
    limits_b: dict[str, Any] = {}
    seen: set[tuple[str, str]] = set()
    amounts: set[Decimal] = set()
    for j in range(rng.randint(1, 5)):
        number, group = str(rng.randrange(10**7, 10**8)), rng.choice(_GROUP_CODES)
        if a_entries and rng.random() < 0.35:
            groups = [s for s in _GROUP_CODES if (s, a_entries[-1]["slotNo"]) not in seen]
            if groups:  # reuse a slot number under another group
                number, group = a_entries[-1]["slotNo"], rng.choice(groups)
        seen.add((group, number))
        amount = Decimal(rng.randrange(0, 5_000_000)) / 100
        while amount in amounts:
            amount += Decimal("0.01")
        amounts.add(amount)
        text = f"{amount:.2f}"
        category, state = rng.choice(_CLASSES), rng.choice(_STATES)
        a_entries.append(
            {
                "groupCode": group,
                "slotNo": number,
                "category": category,
                "status": state,
                "amount": text,
                "unit": "pc",
            }
        )
        b_slots.append(
            {
                "slot": {"groupCode": group.replace("-", ""), "number": number},
                "classCode": category,
                "state": state,
                "reading": {"value": Decimal(text.rstrip("0").rstrip(".")), "unit": "pc"},
            }
        )
        limit_id = f"{doc:04d}{j:04d}"
        low, high = Decimal(rng.choice([250, 500, 1000])), Decimal(rng.choice([5000, 10000, 20000]))
        limits_a[limit_id] = {"low": low, "high": high}
        limits_b[limit_id] = {"lowBound": low, "highBound": high}
    rng.shuffle(b_slots)
    return a_entries, b_slots, limits_a, limits_b


def _rules(rng: random.Random) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shared = datetime(2024, 1, 1) + timedelta(minutes=rng.randrange(500_000))
    together = rng.random() < 0.5  # rules captured at once share a timestamp: not a key
    a, b = [], []
    for mode in rng.sample(_MODES, rng.randint(1, 4)):
        when = shared if together else datetime(2024, 1, 1) + timedelta(minutes=rng.randrange(500_000))
        enabled = rng.random() < 0.6
        a.append({"mode": mode, "enabled": enabled, "updatedAt": _stamp(when, False)})
        b.append({"mode": mode.lower(), "enabled": enabled, "updatedAt": _stamp(when, True)})
    rng.shuffle(b)
    return a, b


def make_record_pairs(seed: int = 20260924, n: int = PAIRS) -> list[tuple[str, Any, Any]]:
    rng = random.Random(seed)  # noqa: S311 - seeded for deterministic test data, not security
    pairs = []
    for i in range(n):
        key = f"C{10000000 + i * 4099:08d}"
        first, last = rng.choice(_PREFIXES), rng.choice(_SUFFIXES)
        startDay = date(1950 + rng.randrange(50), 1 + rng.randrange(12), 1 + rng.randrange(28)).isoformat()
        created = datetime(2015, 1, 1) + timedelta(seconds=rng.randrange(300_000_000))
        used: set[int] = set()
        channels = [
            (kind, *_phone(rng, kind, used))
            for kind in (rng.choice(["MOBILE", "FIXED", "OTHER"]) for _ in range(rng.randint(1, 3)))
        ]
        b_channels = [{"use": k, "value": intl} for k, _, intl in channels]
        rng.shuffle(b_channels)
        path, zone = f"{rng.randint(1, 200)} {rng.choice(_PATHS)}", rng.choice(_ZONES)
        zoneLabel = rng.choice(_CODES).lower()
        entries, slots, limits_a, limits_b = _entries(rng, i)
        rules_a, rules_b = _rules(rng)
        tags = rng.sample(_TAGS, rng.randint(0, 3))
        categoryGroup = rng.choice(_GROUPS)
        offset = Decimal(88_000_000 + i)
        a = {
            "record": {
                "id": key,
                "prefix": first,
                "suffix": last,
                "startDay": startDay,
                "email": f"fixture-{i}@Example.invalid",
                "channels": [{"kind": k, "number": national} for k, national, _ in channels],
                "location": {
                    "line1": path,
                    "zone": zone,
                    "zoneLabel": zoneLabel,
                    "region": "zz",
                },
                "categoryGroup": categoryGroup,
                "createdAt": _stamp(created, False),
                "entries": entries,
                "boundsById": limits_a,
                "rules": rules_a,
                "tags": tags,
                "leftOnly": f"X0{rng.randrange(16**6):06X}",
                "apiToken": SECRET,
            },
            "asOf": {"eventOffset": offset},
        }
        b = {
            "data": {
                "recordId": key,
                "profile": {"prefixText": first, "suffixText": last, "originDay": startDay},
                "contact": {"emailAddress": f"fixture-{i}@example.invalid"},
                "endpoints": b_channels,
                "coordinates": {
                    "path": path,
                    "area": zone,
                    "zoneCode": zoneLabel.upper(),
                    "regionCode": "ZZ",
                },
                "categoryGroup": categoryGroup.lower(),
                "createdAt": _stamp(created, True),
                "slots": slots,
                "bounds": limits_b,
                "rules": rules_b,
                "labels": [t.lower() for t in reversed(tags)],
                "etag": f'W/"{rng.randrange(16**8):08x}"',
                "accessToken": SECRET,
            },
            "meta": {"sourceOffset": offset, "requestId": f"{rng.randrange(16**12):012x}"},
        }
        pairs.append((key, a, b))
    return pairs


_ID_TYPES = ["PHONE", "EMAIL", "PREFIX", "DIGITS"]
_PHASES = [f"STEP-{n}" for n in range(5)]
_FRACTIONS = ["0.50", "0.75", "0.90", "0.95", "0.99"]



def _sample_key(rng: random.Random, kind: str) -> tuple[str, str]:
    n = rng.randrange(1000)
    if kind == "PHONE":
        return f"07700 900{n:03d}", f"+447700900{n:03d}"
    if kind == "EMAIL":
        value = f"contact{rng.randrange(10**6)}@example.net"
    elif kind == "PREFIX":
        value = f"ZZ{rng.randrange(10**9):09d}"
    else:
        value = f"{rng.randrange(10**8, 10**9)}"
    return value, value


def _keys(rng: random.Random) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Identifiers where type repeats and a value can appear under two types: key (type, value)."""
    rows: dict[tuple[str, str], str] = {}
    for kind in (rng.choice(_ID_TYPES) for _ in range(rng.randint(1, 4))):
        a_value, b_value = _sample_key(rng, kind)
        rows[(kind, a_value)] = b_value
    if rng.random() < 0.4:  # the same value filed under a second type
        (kind, a_value), b_value = next(iter(rows.items()))
        rows[("LEGACY_REF", a_value)] = b_value
    if rng.random() < 0.4:  # a second identifier of an existing type
        kind = next(iter(rows))[0]
        a_value, b_value = _sample_key(rng, kind)
        rows[(kind, a_value)] = b_value
    a = [{"type": t, "value": v} for (t, v) in rows]
    b = [{"idType": t, "idValue": bv} for (t, _), bv in rows.items()]
    rng.shuffle(b)
    return a, b


def _links(rng: random.Random, pool: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    a, b = [], []
    for code in rng.sample(pool, rng.randint(0, 6)):
        phase, fraction = rng.choice(_PHASES), rng.choice(_FRACTIONS)
        year = rng.randint(2015, 2022)
        a.append(
            {
                "code": code,
                "id": Decimal(rng.randrange(10**9)),
                "phase": phase,
                "fraction": fraction,
                "since": f"{year}-01-01",
            }
        )
        b.append(
            {
                "target": {"code": code},
                "id": Decimal(rng.randrange(10**9, 10**9 + 10**8)),  # none of side a's, which are lower
                "role": phase,
                "fraction": Decimal(fraction),
                "validFrom": f"{year}-01-01T00:00:00Z",
            }
        )
    if len(b) >= 2 and rng.random() < 0.05:
        b.pop()  # omit a part to lower overlap without hiding the key
    rng.shuffle(b)
    return a, b


def make_batch_pairs(seed: int = 7, n: int = PAIRS) -> list[tuple[str, Any, Any]]:
    rng = random.Random(seed)  # noqa: S311 - seeded for deterministic test data, not security
    pool = [f"E{4000000 + k * 13:07d}" for k in range(2000)]
    pairs = []
    for i in range(n):
        code = f"E{3000000 + i * 17:07d}"
        short = rng.random() < 0.6
        name = f"{rng.choice(_PREFIXES)} {rng.choice(_SUFFIXES)}" if short else f"batch-{i}-long"
        kind = "S" if short else "L"
        flagged, enabled = rng.random() < 0.15, rng.random() < 0.1
        weight = f"{rng.randrange(0, 10000) / 100:.2f}"
        links_a, links_b = _links(rng, pool)
        ids_a, ids_b = _keys(rng)
        a = {
            "batch": {
                "code": code,
                "name": name,
                "shape": kind,
                "flagged": flagged,
                "enabled": enabled,
                "weight": weight,
                "internalId": Decimal(rng.randrange(10**9)),
                "parts": links_a,
                "keys": ids_a,
            }
        }
        b = {
            "result": {
                "batchCode": code,
                "details": {"displayName": name, "kind": kind},
                "flags": {"flagged": flagged, "enabled": enabled},
                "metrics": {"weight": Decimal(weight.rstrip("0").rstrip(".") or "0")},
                "recordId": Decimal(rng.randrange(10**12)),
                "links": links_b,
                "codes": ids_b,
            }
        }
        pairs.append((code, a, b))
    return pairs


def to_jsonata_input(value: Any) -> Any:
    """jsonata-python (and jsonschema's integer check) reject Decimal: plain numbers instead."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: to_jsonata_input(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonata_input(v) for v in value]
    return value


def run_map(expr: str, doc: Any) -> Any:
    return jsonata.Jsonata(expr).evaluate(to_jsonata_input(doc))


def strings_in(value: Any) -> set[str]:
    """Every string leaf of a document (member names excluded)."""
    if isinstance(value, dict | list):
        return set().union(*map(strings_in, value.values() if isinstance(value, dict) else value))
    return {value} if isinstance(value, str) else set()


ARGS = {"spec_name": "unit-draft", "target_a": "left", "target_b": "right", "key_name": "k"}


def draft_of(docs_a: list[Any], docs_b: list[Any], keys: list[str] | None = None, **kwargs: Any) -> DraftSpec:
    """bootstrap() over paired documents, keyed K0000, K0001, ... unless ``keys`` are given."""
    keys = keys or [f"K{i:04d}" for i in range(len(docs_a))]
    return bootstrap(list(zip(keys, docs_a, docs_b, strict=True)), **{**ARGS, **kwargs})


def rows_of(draft: DraftSpec, kind: str, path_a: str | None = None) -> list[EvidenceRow]:
    return [r for r in draft.evidence if r.kind == kind and (path_a is None or r.path_a == path_a)]


def matches(draft: DraftSpec) -> dict[str, str | None]:
    return {r.path_a: r.path_b for r in rows_of(draft, "align") if r.decision == "matched"}


def arrays(draft: DraftSpec) -> dict[str, dict[str, Any]]:
    return {rule["path"]: rule for rule in draft.spec["compare"].get("arrays", [])}


def normalisers(draft: DraftSpec) -> dict[str, list[Any]]:
    return {rule["path"]: rule["normalize"] for rule in draft.spec["compare"].get("fields", [])}


def candidates(draft: DraftSpec, array: str) -> dict[str, EvidenceRow]:
    """Key candidate rows of ``array`` by key text (``"type, value"``)."""
    return {str(r.scores["key"]): r for r in rows_of(draft, "key_candidate", array)}


@pytest.fixture(scope="session")
def record_pairs() -> list[tuple[str, Any, Any]]:
    return make_record_pairs()


@pytest.fixture(scope="session")
def batch_pairs() -> list[tuple[str, Any, Any]]:
    return make_batch_pairs()


@pytest.fixture(scope="session")
def record_draft(record_pairs: list[tuple[str, Any, Any]]) -> DraftSpec:
    return bootstrap(record_pairs, **RECORD_ARGS)


@pytest.fixture(scope="session")
def batch_draft(batch_pairs: list[tuple[str, Any, Any]]) -> DraftSpec:
    return bootstrap(batch_pairs, **BATCH_ARGS)


@pytest.fixture(scope="session")
def spec_validator(repo_root: Path) -> Draft202012Validator:
    schema = json.loads((repo_root / "deltasleuth" / "core" / "spec" / "spec.schema.json").read_text())
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
