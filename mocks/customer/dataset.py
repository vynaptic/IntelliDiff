"""Deterministic dataset for the customer pair.

Forty synthetic customers drawn from one ``random.Random(seed)`` in a fixed order. Both variants
serve the same customers; a seed changes one variant's answer for its own key only, and every
other customer maps to the same canonical document on both variants, raw values included (so
those keys carry no finding at all, not even NORMALIZATION_ONLY).

Seeded discrepancies sit on fixed keys (``SEEDED_KEYS``) whatever the seed; the preconditions a
seed needs (two accounts to drop one from, a mobile phone to reformat, a PRIVATE segment) are set
explicitly, so expected.json holds for every seed.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from mocks.common import Reply, Script, ScriptedDataset, Variant, not_found_reply
from mocks.customer.model import Account, Address, Consent, Customer, Phone, v1_body, v2_body

DEFAULT_SEED = 20260924
CUSTOMER_COUNT = 40


class Seed(StrEnum):
    CONSENT_GRANTED_MISMATCH = "consent_granted_mismatch"
    ACCOUNT_MISSING_IN_V2 = "account_missing_in_v2"
    TAGS_CASE_ONLY = "tags_case_only"
    PHONE_FORMAT_ONLY = "phone_format_only"
    BALANCE_SCALE_ONLY = "balance_scale_only"
    CREATED_AT_MS_ONLY = "created_at_ms_only"
    ONE_SIDED_404 = "one_sided_404"
    SEGMENT_KNOWN_DIFF = "segment_known_diff"
    SMS_PREFERENCE_MISSING = "sms_preference_missing"


def customer_id(n: int) -> str:
    return f"C{12000 + n:08d}"


SEEDED_KEYS: dict[Seed, str] = {
    Seed.CONSENT_GRANTED_MISMATCH: customer_id(3),
    Seed.ACCOUNT_MISSING_IN_V2: customer_id(7),
    Seed.TAGS_CASE_ONLY: customer_id(11),
    Seed.PHONE_FORMAT_ONLY: customer_id(15),
    Seed.BALANCE_SCALE_ONLY: customer_id(19),
    Seed.CREATED_AT_MS_ONLY: customer_id(23),
    Seed.ONE_SIDED_404: customer_id(27),
    Seed.SEGMENT_KNOWN_DIFF: customer_id(31),
    Seed.SMS_PREFERENCE_MISSING: customer_id(35),
}

SEEDED_BALANCE_CENTS = 125010  # v1 sends the text "1250.10", v2 the number 1250.1

FIRST_NAMES = (
    "Ada", "Bram", "Cora", "Dev", "Elin", "Farid", "Greta", "Hugo", "Iris", "Jonah", "Kira", "Luca",
    "Mira", "Nils", "Orla", "Priya", "Quinn", "Rosa", "Soren", "Tamsin",
)  # fmt: skip
LAST_NAMES = (
    "Ashdown", "Brook", "Carver", "Dunmore", "Ellery", "Fairlie", "Garner", "Holt", "Ingram",
    "Jessop", "Kemble", "Lorimer", "Marsh", "Norwood", "Oakley", "Pryce",
)  # fmt: skip
STREETS = ("Example Street", "Mill Lane", "Station Road", "Church Walk", "Quay Side", "Orchard Way")
CITIES = ("London", "Leeds", "Bristol", "Cardiff", "York", "Norwich")
POSTCODE_AREAS = ("SW1A", "LS1", "BS1", "CF10", "YO1", "NR2")
TAG_POOL = ("PAPERLESS", "SENIOR", "STUDENT", "VIP")
RATES = {"CURRENT": (0.0,), "SAVINGS": (0.0125, 0.02, 0.035), "ISA": (0.03, 0.0415)}


def build_dataset(seed: int = DEFAULT_SEED) -> ScriptedDataset:
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data, nothing secret
    truth = {customer_id(n): _customer(rng, n) for n in range(1, CUSTOMER_COUNT + 1)}
    _set_preconditions(rng, truth)
    return ScriptedDataset(
        pair="customer",
        rng_seed=seed,
        keys=tuple(truth),
        seeded={s.value: key for s, key in SEEDED_KEYS.items()},
        scripts=_scripts(truth),
    )


def _mobile(n: int) -> str:
    return f"+447700900{100 + n:03d}"  # Ofcom drama range: fictional by construction


def _home(n: int) -> str:
    return f"+442079460{100 + n:03d}"


def _timestamp(rng: random.Random, first_year: int, years: int) -> str:
    start = datetime(first_year, 1, 1, tzinfo=UTC)
    moment = start + timedelta(seconds=rng.randrange(years * 365 * 86400))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _address(rng: random.Random, kind: str) -> Address:
    lines = (f"Flat {rng.randint(1, 40)}", f"{rng.randint(1, 250)} {rng.choice(STREETS)}")
    inward = f"{rng.randint(1, 9)}{rng.choice('ABDEFGHJ')}{rng.choice('LNPQRSTU')}"
    postcode = f"{rng.choice(POSTCODE_AREAS)} {inward}"
    return Address(kind, lines, rng.choice(CITIES), postcode, "GB")


def _accounts(rng: random.Random, n: int, count: int) -> tuple[Account, ...]:
    accounts = []
    for j in range(count):
        product = rng.choice(tuple(RATES))
        sort_code = f"{rng.randint(10, 99)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"
        accounts.append(
            Account(
                sort_code=sort_code,
                account_no=f"{n:04d}{j}{rng.randint(100, 999)}",  # unique per customer and index
                product=product,
                status="DORMANT" if rng.random() < 0.1 else "OPEN",
                balance_cents=rng.randrange(2_000_000),
                currency="GBP",
                interest_rate=rng.choice(RATES[product]),
            )
        )
    return tuple(accounts)


def _consents(rng: random.Random) -> tuple[Consent, ...]:
    consents = [Consent("MARKETING", rng.random() < 0.6, _timestamp(rng, 2022, 3))]
    if rng.random() < 0.5:
        consents.append(Consent("PROFILING", rng.random() < 0.5, _timestamp(rng, 2022, 3)))
    return tuple(consents)


def _customer(rng: random.Random, n: int) -> Customer:
    given, family = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    phones = []
    if rng.random() < 0.9:
        phones.append(Phone("MOBILE", _mobile(n)))
    if rng.random() < 0.4:
        phones.append(Phone("HOME", _home(n)))
    addresses = [_address(rng, "HOME")]
    if rng.random() < 0.25:
        addresses.append(_address(rng, "WORK"))
    birth_date = (date(1950, 1, 1) + timedelta(days=rng.randrange(20000))).isoformat()
    return Customer(
        customer_id=customer_id(n),
        given_name=given,
        family_name=family,
        birth_date=birth_date,
        email=f"{given}.{family}.{n}@example.org".lower(),
        phones=tuple(phones),
        addresses=tuple(addresses),
        segment="SME" if rng.random() < 0.2 else "RETAIL",
        status="SUSPENDED" if rng.random() < 0.1 else "ACTIVE",
        created_at=_timestamp(rng, 2015, 10),
        language="cy-GB" if rng.random() < 0.1 else "en-GB",
        marketing_email=rng.random() < 0.5,
        marketing_sms=rng.random() < 0.3,
        accounts=_accounts(rng, n, rng.choice((0, 1, 1, 2, 3))),
        consents=_consents(rng),
        tags=tuple(sorted(rng.sample(TAG_POOL, rng.choice((0, 1, 1, 2))))),
        event_offset=88_000_000 + rng.randrange(1_000_000),
    )


def _number(key: str) -> int:
    return int(key[1:]) - 12000


def _set_preconditions(rng: random.Random, truth: dict[str, Customer]) -> None:
    """Give each seeded customer what its seed needs, whatever the random draw produced."""
    key = SEEDED_KEYS

    c = truth[key[Seed.CONSENT_GRANTED_MISMATCH]]
    truth[c.customer_id] = replace(c, consents=(Consent("MARKETING", True, _timestamp(rng, 2022, 3)),))

    c = truth[key[Seed.ACCOUNT_MISSING_IN_V2]]
    truth[c.customer_id] = replace(c, accounts=_accounts(rng, _number(c.customer_id), 2))

    c = truth[key[Seed.TAGS_CASE_ONLY]]
    truth[c.customer_id] = replace(c, tags=("PAPERLESS", "VIP"))

    c = truth[key[Seed.PHONE_FORMAT_ONLY]]
    others = tuple(p for p in c.phones if p.kind != "MOBILE")
    truth[c.customer_id] = replace(c, phones=(Phone("MOBILE", _mobile(_number(c.customer_id))), *others))

    c = truth[key[Seed.BALANCE_SCALE_ONLY]]
    account = _accounts(rng, _number(c.customer_id), 1)[0]
    truth[c.customer_id] = replace(c, accounts=(replace(account, balance_cents=SEEDED_BALANCE_CENTS),))

    c = truth[key[Seed.SEGMENT_KNOWN_DIFF]]
    truth[c.customer_id] = replace(c, segment="PRIVATE")

    # KD-CP-0002's story: v2 drops the SMS preference for customers without a mobile number.
    c = truth[key[Seed.SMS_PREFERENCE_MISSING]]
    home_only = (Phone("HOME", _home(_number(c.customer_id))),)
    truth[c.customer_id] = replace(c, phones=home_only, marketing_sms=False)


def _national_gb(e164: str) -> str:
    """'+447700900115' -> '07700 900115': the same number in UK national format."""
    digits = e164.removeprefix("+44")
    return f"0{digits[:4]} {digits[4:]}"


def _balance_text(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _views(truth: dict[str, Customer]) -> tuple[dict[str, Customer], dict[str, Customer]]:
    """What each variant holds: the truth, with each content-level seed applied to one side."""
    key = SEEDED_KEYS
    v1, v2 = dict(truth), dict(truth)

    c = truth[key[Seed.CONSENT_GRANTED_MISMATCH]]
    v2[c.customer_id] = replace(c, consents=tuple(replace(k, granted=False) for k in c.consents))

    c = truth[key[Seed.ACCOUNT_MISSING_IN_V2]]
    v2[c.customer_id] = replace(c, accounts=c.accounts[:-1])

    c = truth[key[Seed.TAGS_CASE_ONLY]]
    v2[c.customer_id] = replace(c, tags=tuple(t.lower() for t in c.tags))

    c = truth[key[Seed.PHONE_FORMAT_ONLY]]
    national = tuple(replace(p, number=_national_gb(p.number)) if p.kind == "MOBILE" else p for p in c.phones)
    v1[c.customer_id] = replace(c, phones=national)

    c = truth[key[Seed.CREATED_AT_MS_ONLY]]
    v2[c.customer_id] = replace(c, created_at=c.created_at.removesuffix("Z") + ".000Z")

    c = truth[key[Seed.SEGMENT_KNOWN_DIFF]]
    v2[c.customer_id] = replace(c, segment="WEALTH")

    c = truth[key[Seed.SMS_PREFERENCE_MISSING]]
    v2[c.customer_id] = replace(c, marketing_sms=None)
    return v1, v2


def _scripts(truth: dict[str, Customer]) -> dict[Variant, dict[str, Script]]:
    v1, v2 = _views(truth)
    a: dict[str, Script] = {k: (Reply(200, v1_body(c)),) for k, c in v1.items()}
    b: dict[str, Script] = {k: (Reply(200, v2_body(c)),) for k, c in v2.items()}

    # A serialisation-only difference has no model-level form, so it patches the rendered body:
    # v1 sends this balance as text with a trailing zero, v2 as a plain number.
    balance_key = SEEDED_KEYS[Seed.BALANCE_SCALE_ONLY]
    body = a[balance_key][0].body
    body["customer"]["accounts"][0]["balance"] = _balance_text(SEEDED_BALANCE_CENTS)

    b[SEEDED_KEYS[Seed.ONE_SIDED_404]] = (not_found_reply(),)
    return {"a": a, "b": b}
