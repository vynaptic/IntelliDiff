"""Profiler fixtures: two seeded sample generators with planted truth, and draft helpers.

``customer`` is the generic example's v1 vs v2 shapes; ``linker`` an entity with linked entities
and shared identifiers. Both plant traps (unique-by-accident fields, unrelated one-sided fields,
random per-side ids, a secret that must never be exported) and expose the truth as constants.
"""

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
CUSTOMER_ARGS = {
    "spec_name": "customer-profile-v1-vs-v2",
    "target_a": "customer-profile-v1",
    "target_b": "customer-profile-v2",
    "key_name": "customer_id",
}
LINKER_ARGS = {
    "spec_name": "entity-linker-legacy-vs-new",
    "target_a": "legacy-linker",
    "target_b": "new-linker",
    "key_name": "entity_code",
    "key_pattern": "^E[0-9]{7}$",
}
# Planted in every document; the tests prove it never reaches evidence, YAML or schemas.
SECRET = "tk_fake_Zq81xVv3K9aJ4mT2pW7c"  # noqa: S105 - fake value, not a credential

_FIRST = [
    "Ada",
    "Grace",
    "Alan",
    "Katherine",
    "Tim",
    "Mary",
    "Charles",
    "Hedy",
    "Claude",
    "Barbara",
    "Edsger",
    "Frances",
    "Radia",
    "Donald",
    "Margaret",
    "Dennis",
]
_LAST = [
    "Lovelace",
    "Hopper",
    "Turing",
    "Johnson",
    "Berners-Lee",
    "Somerville",
    "Babbage",
    "Lamarr",
    "Shannon",
    "Liskov",
    "Dijkstra",
    "Allen",
    "Perlman",
    "Knuth",
    "Hamilton",
    "Ritchie",
]
_STREETS = ["Example Street", "Sample Road", "Fictional Lane", "Placeholder Avenue", "Demo Close"]
_CITIES = ["London", "Leeds", "Bristol", "Glasgow", "Cardiff", "Belfast", "York"]
_POSTCODES = ["sw1a 1aa", "ls1 4ap", "bs1 5tr", "g2 1dy", "cf10 1ep", "bt1 5gs", "yo1 7hh"]
_SORT_CODES = ["40-47-84", "20-32-53", "30-96-34", "60-83-71"]
_PRODUCTS = ["CURRENT", "SAVINGS", "ISA", "CREDIT_CARD"]
_ACCOUNT_STATES = ["OPEN", "DORMANT"]
_PURPOSES = ["MARKETING", "PROFILING", "THIRD_PARTY", "RESEARCH"]
_TAGS = ["VIP", "PAPERLESS", "STUDENT", "PREMIER", "VULNERABLE"]
_SEGMENTS = ["RETAIL", "SME", "PRIVATE"]

# Planted truth, customer pair (canonical = side a, relative to the select roots).
CUSTOMER_SELECT = {"a": "$.customer", "b": "$.data"}
CUSTOMER_KEYS = {"$.phones": ["number"], "$.accounts": ["sortCode", "accountNo"], "$.consents": ["purpose"]}
CUSTOMER_RENAMES = {
    "$.id": "$.customerId",
    "$.firstName": "$.profile.givenName",
    "$.lastName": "$.profile.familyName",
    "$.dob": "$.profile.birthDate",
    "$.email": "$.contact.emailAddress",
    "$.phones": "$.phoneNumbers",
    "$.phones[*].kind": "$.phoneNumbers[*].use",
    "$.phones[*].number": "$.phoneNumbers[*].value",
    "$.correspondenceAddress.line1": "$.mailingAddress.street",
    "$.correspondenceAddress.city": "$.mailingAddress.town",
    "$.correspondenceAddress.postcode": "$.mailingAddress.postalCode",
    "$.correspondenceAddress.country": "$.mailingAddress.countryCode",
    "$.segment": "$.segment",
    "$.createdAt": "$.createdAt",
    "$.accounts": "$.products",
    "$.accounts[*].sortCode": "$.products[*].account.sortCode",
    "$.accounts[*].accountNo": "$.products[*].account.number",
    "$.accounts[*].product": "$.products[*].productCode",
    "$.accounts[*].status": "$.products[*].state",
    "$.accounts[*].balance": "$.products[*].balance.amount",
    "$.accounts[*].currency": "$.products[*].balance.currency",
    "$.limitsByAccount": "$.accountLimits",
    "$.limitsByAccount[*].daily": "$.accountLimits[*].dailyLimit",
    "$.limitsByAccount[*].monthly": "$.accountLimits[*].monthlyLimit",
    "$.consents": "$.consents",
    "$.consents[*].purpose": "$.consents[*].purpose",
    "$.consents[*].granted": "$.consents[*].granted",
    "$.consents[*].updatedAt": "$.consents[*].updatedAt",
    "$.tags": "$.labels",
    "$.apiToken": "$.accessToken",
}
CUSTOMER_UNMATCHED = {"a": ["$.legacyBranchRef"], "b": ["$.etag"]}
# Numbers and instants keep every digit: text vs number and ".000" vs none are only writing.
CUSTOMER_NORMALISERS = {
    "$.phones[*].number": [{"e164": {"defaultRegion": "GB"}}],
    "$.email": ["email_basic"],
    "$.segment": ["upper"],
    "$.createdAt": [{"timestamp_utc": {"precision": "us", "mode": "truncate"}}],
    "$.accounts[*].sortCode": ["digits_only"],
    "$.accounts[*].balance": [{"decimal": {"scale": 38}}],
    "$.consents[*].purpose": ["upper"],
    "$.consents[*].updatedAt": [{"timestamp_utc": {"precision": "us", "mode": "truncate"}}],
    "$.tags[*]": ["upper"],
    "$.correspondenceAddress.postcode": ["lower"],
    "$.correspondenceAddress.country": ["lower"],
}

# Planted truth, linker pair.
LINKER_SELECT = {"a": "$.entity", "b": "$.result"}
LINKER_KEYS = {"$.linkedEntities": ["code"], "$.identifiers": ["type", "value"]}
LINKER_RENAMES = {
    "$.code": "$.entityCode",
    "$.name": "$.details.displayName",
    "$.entityType": "$.details.kind",
    "$.flagged": "$.flags.flagged",
    "$.vip": "$.flags.vip",
    "$.score": "$.rating.score",
    "$.linkedEntities": "$.links",
    "$.linkedEntities[*].code": "$.links[*].target.code",
    "$.linkedEntities[*].relationship": "$.links[*].role",
    "$.linkedEntities[*].confidence": "$.links[*].confidence",
    "$.linkedEntities[*].since": "$.links[*].validFrom",
    "$.identifiers": "$.sharedIdentifiers",
    "$.identifiers[*].type": "$.sharedIdentifiers[*].idType",
    "$.identifiers[*].value": "$.sharedIdentifiers[*].idValue",
}
LINKER_UNMATCHED = {"a": ["$.internalId", "$.linkedEntities[*].id"], "b": ["$.recordId", "$.links[*].id"]}


def _stamp(dt: datetime, fraction: bool) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (".000Z" if fraction else "Z")


def _phone(rng: random.Random, kind: str, used: set[int]) -> tuple[str, str]:
    n = rng.choice([x for x in range(1000) if x not in used])
    used.add(n)
    if kind == "MOBILE":
        return f"07700 900{n:03d}", f"+447700900{n:03d}"
    return f"020 7946 0{n:03d}", f"+442079460{n:03d}"


def _accounts(
    rng: random.Random, doc: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Accounts where neither sortCode nor accountNo alone is unique, plus the id-keyed limits map."""
    a_accounts: list[dict[str, Any]] = []
    b_products: list[dict[str, Any]] = []
    limits_a: dict[str, Any] = {}
    limits_b: dict[str, Any] = {}
    seen: set[tuple[str, str]] = set()
    balances: set[Decimal] = set()
    for j in range(rng.randint(1, 5)):
        number, sort = str(rng.randrange(10**7, 10**8)), rng.choice(_SORT_CODES)
        if a_accounts and rng.random() < 0.35:
            branches = [s for s in _SORT_CODES if (s, a_accounts[-1]["accountNo"]) not in seen]
            if branches:  # the same account number at another branch
                number, sort = a_accounts[-1]["accountNo"], rng.choice(branches)
        seen.add((sort, number))
        balance = Decimal(rng.randrange(0, 5_000_000)) / 100
        while balance in balances:
            balance += Decimal("0.01")
        balances.add(balance)
        text = f"{balance:.2f}"
        product, state = rng.choice(_PRODUCTS), rng.choice(_ACCOUNT_STATES)
        a_accounts.append(
            {
                "sortCode": sort,
                "accountNo": number,
                "product": product,
                "status": state,
                "balance": text,
                "currency": "GBP",
            }
        )
        b_products.append(
            {
                "account": {"sortCode": sort.replace("-", ""), "number": number},
                "productCode": product,
                "state": state,
                "balance": {"amount": Decimal(text.rstrip("0").rstrip(".")), "currency": "GBP"},
            }
        )
        limit_id = f"{doc:04d}{j:04d}"
        daily, monthly = Decimal(rng.choice([250, 500, 1000])), Decimal(rng.choice([5000, 10000, 20000]))
        limits_a[limit_id] = {"daily": daily, "monthly": monthly}
        limits_b[limit_id] = {"dailyLimit": daily, "monthlyLimit": monthly}
    rng.shuffle(b_products)
    return a_accounts, b_products, limits_a, limits_b


def _consents(rng: random.Random) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shared = datetime(2024, 1, 1) + timedelta(minutes=rng.randrange(500_000))
    together = rng.random() < 0.5  # consents captured at once share a timestamp: not a key
    a, b = [], []
    for purpose in rng.sample(_PURPOSES, rng.randint(1, 4)):
        when = shared if together else datetime(2024, 1, 1) + timedelta(minutes=rng.randrange(500_000))
        granted = rng.random() < 0.6
        a.append({"purpose": purpose, "granted": granted, "updatedAt": _stamp(when, False)})
        b.append({"purpose": purpose.lower(), "granted": granted, "updatedAt": _stamp(when, True)})
    rng.shuffle(b)
    return a, b


def make_customer_pairs(seed: int = 20260924, n: int = PAIRS) -> list[tuple[str, Any, Any]]:
    rng = random.Random(seed)  # noqa: S311 - seeded for deterministic test data, not security
    pairs = []
    for i in range(n):
        key = f"C{10000000 + i * 4099:08d}"
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        dob = date(1950 + rng.randrange(50), 1 + rng.randrange(12), 1 + rng.randrange(28)).isoformat()
        created = datetime(2015, 1, 1) + timedelta(seconds=rng.randrange(300_000_000))
        used: set[int] = set()
        phones = [
            (kind, *_phone(rng, kind, used))
            for kind in (rng.choice(["MOBILE", "HOME", "WORK"]) for _ in range(rng.randint(1, 3)))
        ]
        b_phones = [{"use": k, "value": intl} for k, _, intl in phones]
        rng.shuffle(b_phones)
        street, city = f"{rng.randint(1, 200)} {rng.choice(_STREETS)}", rng.choice(_CITIES)
        postcode = rng.choice(_POSTCODES).lower()
        accounts, products, limits_a, limits_b = _accounts(rng, i)
        consents_a, consents_b = _consents(rng)
        tags = rng.sample(_TAGS, rng.randint(0, 3))
        segment = rng.choice(_SEGMENTS)
        offset = Decimal(88_000_000 + i)
        a = {
            "customer": {
                "id": key,
                "firstName": first,
                "lastName": last,
                "dob": dob,
                "email": f"{first}.{last}{i}@Example.org",
                "phones": [{"kind": k, "number": national} for k, national, _ in phones],
                "correspondenceAddress": {
                    "line1": street,
                    "city": city,
                    "postcode": postcode,
                    "country": "gb",
                },
                "segment": segment,
                "createdAt": _stamp(created, False),
                "accounts": accounts,
                "limitsByAccount": limits_a,
                "consents": consents_a,
                "tags": tags,
                "legacyBranchRef": f"BR{rng.randrange(16**6):06X}",
                "apiToken": SECRET,
            },
            "asOf": {"eventOffset": offset},
        }
        b = {
            "data": {
                "customerId": key,
                "profile": {"givenName": first, "familyName": last, "birthDate": dob},
                "contact": {"emailAddress": f"{first}.{last}{i}@example.org".lower()},
                "phoneNumbers": b_phones,
                "mailingAddress": {
                    "street": street,
                    "town": city,
                    "postalCode": postcode.upper(),
                    "countryCode": "GB",
                },
                "segment": segment.lower(),
                "createdAt": _stamp(created, True),
                "products": products,
                "accountLimits": limits_b,
                "consents": consents_b,
                "labels": [t.lower() for t in reversed(tags)],
                "etag": f'W/"{rng.randrange(16**8):08x}"',
                "accessToken": SECRET,
            },
            "meta": {"sourceOffset": offset, "requestId": f"{rng.randrange(16**12):012x}"},
        }
        pairs.append((key, a, b))
    return pairs


_ID_TYPES = ["PHONE", "EMAIL", "TAX_ID", "PASSPORT"]
_RELATIONSHIPS = ["DIRECTOR", "SHAREHOLDER", "ASSOCIATE", "EMPLOYER", "GUARANTOR"]
_CONFIDENCE = ["0.50", "0.75", "0.90", "0.95", "0.99"]
_ORG_WORDS = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay", "Stark", "Wayne"]


def _identifier(rng: random.Random, kind: str) -> tuple[str, str]:
    n = rng.randrange(1000)
    if kind == "PHONE":
        return f"07700 900{n:03d}", f"+447700900{n:03d}"
    if kind == "EMAIL":
        value = f"contact{rng.randrange(10**6)}@example.net"
    elif kind == "TAX_ID":
        value = f"GB{rng.randrange(10**9):09d}"
    else:
        value = f"{rng.randrange(10**8, 10**9)}"
    return value, value


def _identifiers(rng: random.Random) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Identifiers where type repeats and a value can appear under two types: key (type, value)."""
    rows: dict[tuple[str, str], str] = {}
    for kind in (rng.choice(_ID_TYPES) for _ in range(rng.randint(1, 4))):
        a_value, b_value = _identifier(rng, kind)
        rows[(kind, a_value)] = b_value
    if rng.random() < 0.4:  # the same value filed under a second type
        (kind, a_value), b_value = next(iter(rows.items()))
        rows[("LEGACY_REF", a_value)] = b_value
    if rng.random() < 0.4:  # a second identifier of an existing type
        kind = next(iter(rows))[0]
        a_value, b_value = _identifier(rng, kind)
        rows[(kind, a_value)] = b_value
    a = [{"type": t, "value": v} for (t, v) in rows]
    b = [{"idType": t, "idValue": bv} for (t, _), bv in rows.items()]
    rng.shuffle(b)
    return a, b


def _links(rng: random.Random, pool: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    a, b = [], []
    for code in rng.sample(pool, rng.randint(0, 6)):
        relationship, confidence = rng.choice(_RELATIONSHIPS), rng.choice(_CONFIDENCE)
        year = rng.randint(2015, 2022)
        a.append(
            {
                "code": code,
                "id": Decimal(rng.randrange(10**9)),
                "relationship": relationship,
                "confidence": confidence,
                "since": f"{year}-01-01",
            }
        )
        b.append(
            {
                "target": {"code": code},
                "id": Decimal(rng.randrange(10**9, 10**9 + 10**8)),  # none of side a's, which are lower
                "role": relationship,
                "confidence": Decimal(confidence),
                "validFrom": f"{year}-01-01T00:00:00Z",
            }
        )
    if len(b) >= 2 and rng.random() < 0.05:
        b.pop()  # a missing link: a real discrepancy, it lowers the overlap without hiding the key
    rng.shuffle(b)
    return a, b


def make_linker_pairs(seed: int = 7, n: int = PAIRS) -> list[tuple[str, Any, Any]]:
    rng = random.Random(seed)  # noqa: S311 - seeded for deterministic test data, not security
    pool = [f"E{4000000 + k * 13:07d}" for k in range(2000)]
    pairs = []
    for i in range(n):
        code = f"E{3000000 + i * 17:07d}"
        person = rng.random() < 0.6
        name = (
            f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
            if person
            else f"{rng.choice(_ORG_WORDS)} {rng.choice(['Holdings', 'Trading', 'Services'])} Ltd"
        )
        kind = "PERSON" if person else "ORGANISATION"
        flagged, vip = rng.random() < 0.15, rng.random() < 0.1
        score = f"{rng.randrange(0, 10000) / 100:.2f}"
        links_a, links_b = _links(rng, pool)
        ids_a, ids_b = _identifiers(rng)
        a = {
            "entity": {
                "code": code,
                "name": name,
                "entityType": kind,
                "flagged": flagged,
                "vip": vip,
                "score": score,
                "internalId": Decimal(rng.randrange(10**9)),
                "linkedEntities": links_a,
                "identifiers": ids_a,
            }
        }
        b = {
            "result": {
                "entityCode": code,
                "details": {"displayName": name, "kind": kind},
                "flags": {"flagged": flagged, "vip": vip},
                "rating": {"score": Decimal(score.rstrip("0").rstrip(".") or "0")},
                "recordId": Decimal(rng.randrange(10**12)),
                "links": links_b,
                "sharedIdentifiers": ids_b,
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
def customer_pairs() -> list[tuple[str, Any, Any]]:
    return make_customer_pairs()


@pytest.fixture(scope="session")
def linker_pairs() -> list[tuple[str, Any, Any]]:
    return make_linker_pairs()


@pytest.fixture(scope="session")
def customer_draft(customer_pairs: list[tuple[str, Any, Any]]) -> DraftSpec:
    return bootstrap(customer_pairs, **CUSTOMER_ARGS)


@pytest.fixture(scope="session")
def linker_draft(linker_pairs: list[tuple[str, Any, Any]]) -> DraftSpec:
    return bootstrap(linker_pairs, **LINKER_ARGS)


@pytest.fixture(scope="session")
def spec_validator(repo_root: Path) -> Draft202012Validator:
    schema = json.loads((repo_root / "docs" / "architecture" / "spec.schema.json").read_text())
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
