"""Customer records and the two wire shapes that mocks/customer/spec.yaml compares.

Variant a (v1, legacy monolith)::

    GET /api/v1/customers/{id}
    {"customer": {"id", "firstName", "lastName", "dob", "email",
                  "phones": [{"kind", "number"}],
                  "addresses": [{"kind", "lines": [...], "city", "postcode", "country"}],
                  "segment", "status": "A"|"S"|"C", "createdAt",
                  "prefs": {"lang", "mktEmail": "Y"|"N", "mktSms": "Y"|"N"},
                  "accountCount",
                  "accounts": [{"sortCode", "accountNo", "product", "status", "balance", "currency", "rate"}],
                  "consents": [{"purpose", "granted": "Y"|"N", "ts"}],
                  "tags": [...]},
     "asOf": {"eventOffset"}}

Variant b (v2, microservice)::

    GET /v2/customers/{id}
    {"data": {"customerId",
              "profile": {"givenName", "familyName", "birthDate"},
              "contactPoints": [{"channel": "EMAIL", "value"}, {"channel": "PHONE", "use", "value"}],
              "addresses": [{"use", "line": [...], "city", "postalCode", "countryCode"}],
              "segment", "lifecycleStatus", "createdAt",
              "preferences": {"locale", "channels": {"email": {"marketing"}, "sms": {"marketing"}}},
              "productCount",
              "products": [{"account": {"sortCode", "number"}, "productCode", "state",
                            "balance": {"amount", "currency"}, "interestRate"}],
              "consents": [{"purpose", "status": "GRANTED"|"WITHDRAWN", "updatedAt"}],
              "labels": [...]},
     "meta": {"sourceOffset", "requestId"}}

v2 lists products, consents and labels in the reverse order of v1: the spec compares them as keyed
arrays and a set, so order must not matter. Amounts and rates are JSON numbers on both variants
except where a seed says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from mocks.common import JSONBody

V1_STATUS = {"ACTIVE": "A", "SUSPENDED": "S", "CLOSED": "C"}


@dataclass(frozen=True)
class Phone:
    kind: str  # MOBILE | HOME
    number: str


@dataclass(frozen=True)
class Address:
    type: str  # HOME | WORK
    lines: tuple[str, ...]
    city: str
    postcode: str
    country: str


@dataclass(frozen=True)
class Account:
    sort_code: str
    account_no: str
    product: str  # CURRENT | SAVINGS | ISA
    status: str  # OPEN | DORMANT
    balance_cents: int
    currency: str
    interest_rate: float


@dataclass(frozen=True)
class Consent:
    purpose: str  # MARKETING | PROFILING
    granted: bool
    updated_at: str


@dataclass(frozen=True)
class Customer:
    customer_id: str
    given_name: str
    family_name: str
    birth_date: str
    email: str
    phones: tuple[Phone, ...]
    addresses: tuple[Address, ...]
    segment: str
    status: str  # ACTIVE | SUSPENDED
    created_at: str
    language: str
    marketing_email: bool
    marketing_sms: bool | None  # None: the service does not return the preference at all
    accounts: tuple[Account, ...]
    consents: tuple[Consent, ...]
    tags: tuple[str, ...]
    event_offset: int


def amount(cents: int) -> float:
    """A JSON number with at most two decimals (repr of cents / 100 is the shortest exact text)."""
    return cents / 100


def _yn(flag: bool | None) -> str:
    return "Y" if flag else "N"


def v1_body(c: Customer) -> JSONBody:
    return {
        "customer": {
            "id": c.customer_id,
            "firstName": c.given_name,
            "lastName": c.family_name,
            "dob": c.birth_date,
            "email": c.email,
            "phones": [{"kind": p.kind, "number": p.number} for p in c.phones],
            "addresses": [
                {"kind": a.type, "lines": list(a.lines), "city": a.city, "postcode": a.postcode,
                 "country": a.country}
                for a in c.addresses
            ],
            "segment": c.segment,
            "status": V1_STATUS[c.status],
            "createdAt": c.created_at,
            "prefs": {"lang": c.language, "mktEmail": _yn(c.marketing_email), "mktSms": _yn(c.marketing_sms)},
            "accountCount": len(c.accounts),
            "accounts": [
                {"sortCode": a.sort_code, "accountNo": a.account_no, "product": a.product,
                 "status": a.status, "balance": amount(a.balance_cents), "currency": a.currency,
                 "rate": a.interest_rate}
                for a in c.accounts
            ],
            "consents": [
                {"purpose": k.purpose, "granted": _yn(k.granted), "ts": k.updated_at} for k in c.consents
            ],
            "tags": list(c.tags),
        },
        "asOf": {"eventOffset": c.event_offset},
    }  # fmt: skip


def _channels(c: Customer) -> JSONBody:
    channels: JSONBody = {"email": {"marketing": c.marketing_email}}
    if c.marketing_sms is not None:
        channels["sms"] = {"marketing": c.marketing_sms}
    return channels


def v2_body(c: Customer) -> JSONBody:
    return {
        "data": {
            "customerId": c.customer_id,
            "profile": {"givenName": c.given_name, "familyName": c.family_name, "birthDate": c.birth_date},
            "contactPoints": [
                {"channel": "EMAIL", "value": c.email},
                *({"channel": "PHONE", "use": p.kind, "value": p.number} for p in c.phones),
            ],
            "addresses": [
                {"use": a.type, "line": list(a.lines), "city": a.city, "postalCode": a.postcode,
                 "countryCode": a.country}
                for a in c.addresses
            ],
            "segment": c.segment,
            "lifecycleStatus": c.status,
            "createdAt": c.created_at,
            "preferences": {"locale": c.language, "channels": _channels(c)},
            "productCount": len(c.accounts),
            "products": [
                {"account": {"sortCode": a.sort_code, "number": a.account_no}, "productCode": a.product,
                 "state": a.status, "balance": {"amount": amount(a.balance_cents), "currency": a.currency},
                 "interestRate": a.interest_rate}
                for a in reversed(c.accounts)
            ],
            "consents": [
                {"purpose": k.purpose, "status": "GRANTED" if k.granted else "WITHDRAWN",
                 "updatedAt": k.updated_at}
                for k in reversed(c.consents)
            ],
            "labels": list(reversed(c.tags)),
        },
        "meta": {"sourceOffset": c.event_offset, "requestId": f"req-{c.customer_id.lower()}"},
    }  # fmt: skip
