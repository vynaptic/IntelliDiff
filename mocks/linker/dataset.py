"""Deterministic dataset for the linker pair.

A synthetic population of entities that are linked when they share an identifier (a phone, an
e-mail address or a device). Everything random comes from one ``random.Random(seed)`` drawn in a
fixed order, so a seed always yields the same responses, byte for byte.

The sample in keys.txt is the first ``SAMPLE_SIZE`` entities plus one key that exists nowhere.
The rest of the population exists on both variants but is never requested; it gives the sampled
entities partners to link to without every seeded change leaking into another sampled key.

Seeded discrepancies sit on fixed keys (``SEEDED_KEYS``) whatever the seed, and each precondition
a seed needs (a link to drop, a flagged link to clear, a shared phone to reformat, a shared
device to re-issue) is built explicitly rather than hoped for, so expected.json holds for every
seed. A seed changes one variant's answer for its own key only. Every other sampled key maps to
the same canonical document on both variants (the unit tests prove it through the spec's own
JSONata maps).
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import combinations
from string import ascii_lowercase

from mocks.common import Reply, Script, ScriptedDataset, Variant, not_found_reply
from mocks.linker.model import Entity, Identifier, Link, legacy_body, new_body

DEFAULT_SEED = 20260924
POPULATION_SIZE = 120
SAMPLE_SIZE = 60
HUB_LINKS = 57  # above variant b's cap of 50, so its answer is truncated
SHARED_GROUPS = 80
FLAGGED_RATE = 0.2
PERSON_RATE = 0.7


class Seed(StrEnum):
    MISSING_LINK = "missing_link"
    EXTRA_LINK = "extra_link"
    NORMALISATION_ONLY = "normalisation_only"
    FLAG_MISMATCH = "flag_mismatch"
    DRIFT = "drift"
    TRUNCATION = "truncation"
    ONE_SIDED_404 = "one_sided_404"
    UNANNOUNCED_404 = "unannounced_404"
    SERVER_ERROR_THEN_SUCCESS = "server_error_then_success"
    DEVICE_ID_CHANGED = "device_id_changed"
    ABSENT_BOTH = "absent_both"


def entity_code(n: int) -> str:
    return f"ORG-{n:06d}"


SEEDED_KEYS: dict[Seed, str] = {
    Seed.MISSING_LINK: entity_code(7),
    Seed.EXTRA_LINK: entity_code(12),
    Seed.NORMALISATION_ONLY: entity_code(18),
    Seed.FLAG_MISMATCH: entity_code(23),
    Seed.DRIFT: entity_code(29),
    Seed.TRUNCATION: entity_code(34),
    Seed.ONE_SIDED_404: entity_code(41),
    Seed.UNANNOUNCED_404: entity_code(46),
    Seed.SERVER_ERROR_THEN_SUCCESS: entity_code(52),
    Seed.DEVICE_ID_CHANGED: entity_code(57),
    Seed.ABSENT_BOTH: entity_code(999),  # outside the population: neither variant has it
}

# Ofcom drama-range numbers: fictional by construction. The seeded phone is rendered in national
# format by variant a only; E.164 formatting makes the two equal again.
SEEDED_PHONE = "+447700900123"
SEEDED_PHONE_NATIONAL = "07700 900123"
HUB_PHONE = "+442079460000"

# Device ids are opaque alphanumeric serials with digit runs, like real ones. Variant b reports the
# seeded device under a re-issued serial that differs only in its suffix letter. Phone-number
# parsing is lenient and reads both as +447700900555, so a phone normaliser could merge two
# distinct devices into one member: the spec must never phone-normalise a device id.
SEEDED_DEVICE = "DEV-7700-900555-A"
SEEDED_DEVICE_REISSUED = "DEV-7700-900555-B"

FIRST_NAMES = (
    "Ada", "Bram", "Cora", "Dev", "Elin", "Farid", "Greta", "Hugo", "Iris", "Jonah", "Kira", "Luca",
    "Mira", "Nils", "Orla", "Priya", "Quinn", "Rosa", "Soren", "Tamsin", "Uma", "Viktor", "Wren",
)  # fmt: skip
LAST_NAMES = (
    "Ashdown", "Brook", "Carver", "Dunmore", "Ellery", "Fairlie", "Garner", "Holt", "Ingram",
    "Jessop", "Kemble", "Lorimer", "Marsh", "Norwood", "Oakley", "Pryce", "Redfern", "Stroud",
)  # fmt: skip
COMPANY_WORDS = (
    "Harrow", "Kestrel", "Meridian", "Northgate", "Oakfield", "Pinnacle", "Quayside", "Redbrook",
    "Silverline", "Thornbury", "Westmere", "Ashcombe",
)  # fmt: skip
COMPANY_TRADES = ("Trading", "Logistics", "Holdings", "Imports", "Consulting", "Freight", "Ventures")
COMPANY_SUFFIXES = ("Ltd", "LLP", "plc")


@dataclass(frozen=True)
class _Profile:
    kind: str
    first: str  # first name, or the company's leading word
    second: str  # last name, or the company's trade
    suffix: str = ""  # company suffix

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first, self.second, self.suffix) if p)


@dataclass(frozen=True)
class _Anchors:
    """Partners wired explicitly to seeded keys, so each seed has what it needs to change."""

    dropped: str  # the missing link's partner
    flagged: str  # the partner whose flagged link variant b clears
    phone: str  # the partner sharing SEEDED_PHONE with the normalisation key
    device: str  # the partner sharing SEEDED_DEVICE with the device key


def build_dataset(seed: int = DEFAULT_SEED) -> ScriptedDataset:
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data, nothing secret
    codes = [entity_code(n) for n in range(1, POPULATION_SIZE + 1)]
    profiles = {code: _profile(rng) for code in codes}
    held: dict[str, set[Identifier]] = {code: set() for code in codes}
    _random_groups(rng, held, codes)
    anchors = _anchor_seeds(rng, held, codes)
    holders = _holders(held)
    flags = _pair_flags(rng, holders)
    flags[_pair(SEEDED_KEYS[Seed.FLAG_MISMATCH], anchors.flagged)] = True
    truth = {code: _entity(code, profiles, held, holders, flags) for code in codes}
    extra = _extra_link(rng, truth, codes, profiles)
    stale = replace(truth[SEEDED_KEYS[Seed.DRIFT]], name=_stale_name(rng, profiles[SEEDED_KEYS[Seed.DRIFT]]))
    return ScriptedDataset(
        pair="linker",
        rng_seed=seed,
        keys=(*codes[:SAMPLE_SIZE], SEEDED_KEYS[Seed.ABSENT_BOTH]),
        seeded={s.value: key for s, key in SEEDED_KEYS.items()},
        scripts=_scripts(truth, anchors, extra, stale),
    )


def _profile(rng: random.Random) -> _Profile:
    if rng.random() < PERSON_RATE:
        return _Profile("PERSON", rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))
    return _Profile(
        "COMPANY", rng.choice(COMPANY_WORDS), rng.choice(COMPANY_TRADES), rng.choice(COMPANY_SUFFIXES)
    )


def _letters(n: int) -> str:
    """Four lower-case letters, unique per ``n``."""
    out = []
    for _ in range(4):
        n, r = divmod(n, 26)
        out.append(ascii_lowercase[r])
    return "".join(reversed(out))


def _shared_identifier(rng: random.Random, n: int) -> Identifier:
    id_type = rng.choice(("PHONE", "EMAIL", "DEVICE"))
    if id_type == "PHONE":
        return Identifier("PHONE", f"+447700900{400 + n:03d}")
    if id_type == "EMAIL":
        return Identifier("EMAIL", f"shared.{_letters(n)}@example.org")
    return Identifier("DEVICE", f"DEV-{1000 + n}-{_letters(n).upper()}")


def _random_groups(rng: random.Random, held: dict[str, set[Identifier]], codes: list[str]) -> None:
    """Small groups sharing one identifier each. The truncation hub stays out, so its link count
    is exactly ``HUB_LINKS``."""
    ordinary = [code for code in codes if code != SEEDED_KEYS[Seed.TRUNCATION]]
    for n in range(SHARED_GROUPS):
        ident = _shared_identifier(rng, n)
        for code in rng.sample(ordinary, rng.choice((2, 2, 3, 3, 4))):
            held[code].add(ident)


def _share(held: dict[str, set[Identifier]], ident: Identifier, holders: list[str]) -> None:
    for code in holders:
        held[code].add(ident)


def _anchor_seeds(rng: random.Random, held: dict[str, set[Identifier]], codes: list[str]) -> _Anchors:
    """Wire the hub and the seed partners, all from the unsampled part of the population."""
    unsampled = codes[SAMPLE_SIZE:]
    hub = SEEDED_KEYS[Seed.TRUNCATION]
    _share(held, Identifier("PHONE", HUB_PHONE), [hub, *rng.sample(unsampled, HUB_LINKS)])
    dropped, kept = sorted(rng.sample(unsampled, 2))
    missing_anchor = Identifier("EMAIL", "anchor.missing@example.org")
    _share(held, missing_anchor, [SEEDED_KEYS[Seed.MISSING_LINK], dropped, kept])
    flagged = rng.choice(unsampled)
    _share(held, Identifier("DEVICE", "DEV-2001-ANCHOR"), [SEEDED_KEYS[Seed.FLAG_MISMATCH], flagged])
    phone = rng.choice(unsampled)
    _share(held, Identifier("PHONE", SEEDED_PHONE), [SEEDED_KEYS[Seed.NORMALISATION_ONLY], phone])
    device = rng.choice(unsampled)
    _share(held, Identifier("DEVICE", SEEDED_DEVICE), [SEEDED_KEYS[Seed.DEVICE_ID_CHANGED], device])
    return _Anchors(dropped=dropped, flagged=flagged, phone=phone, device=device)


def _pair(x: str, y: str) -> tuple[str, str]:
    return (x, y) if x < y else (y, x)


def _holders(held: dict[str, set[Identifier]]) -> dict[Identifier, list[str]]:
    """Identifier -> the entities holding it, in code order (set iteration order never leaks)."""
    out: dict[Identifier, list[str]] = defaultdict(list)
    for code in sorted(held):
        for ident in held[code]:
            out[ident].append(code)
    return out


def _pair_flags(rng: random.Random, holders: dict[Identifier, list[str]]) -> dict[tuple[str, str], bool]:
    """One flag per linked pair, so both ends of a link agree."""
    pairs = {pair for codes in holders.values() for pair in combinations(codes, 2)}
    return {pair: rng.random() < FLAGGED_RATE for pair in sorted(pairs)}


def _entity(
    code: str,
    profiles: dict[str, _Profile],
    held: dict[str, set[Identifier]],
    holders: dict[Identifier, list[str]],
    flags: dict[tuple[str, str], bool],
) -> Entity:
    shared: dict[str, set[Identifier]] = defaultdict(set)
    for ident in held[code]:
        for other in holders[ident]:
            if other != code:
                shared[other].add(ident)
    links = tuple(
        Link(other, profiles[other].kind, flags[_pair(code, other)], tuple(sorted(ids)))
        for other, ids in sorted(shared.items())
    )
    return Entity(code, profiles[code].name, profiles[code].kind, links)


def _extra_link(
    rng: random.Random, truth: dict[str, Entity], codes: list[str], profiles: dict[str, _Profile]
) -> Link:
    """A link only variant b reports, to an entity the extra-link key is not linked to."""
    linked = {link.code for link in truth[SEEDED_KEYS[Seed.EXTRA_LINK]].links}
    partner = rng.choice([code for code in codes[SAMPLE_SIZE:] if code not in linked])
    return Link(partner, profiles[partner].kind, False, (Identifier("DEVICE", "DEV-2002-ORPHAN"),))


def _stale_name(rng: random.Random, profile: _Profile) -> str:
    """The drift key's previous name: a changed surname, or a changed trade for a company."""
    pool = LAST_NAMES if profile.kind == "PERSON" else COMPANY_TRADES
    return replace(profile, second=rng.choice([p for p in pool if p != profile.second])).name


def _without_link(entity: Entity, partner: str) -> Entity:
    return replace(entity, links=tuple(link for link in entity.links if link.code != partner))


def _with_link(entity: Entity, link: Link) -> Entity:
    return replace(entity, links=(*entity.links, link))


def _with_flag(entity: Entity, partner: str, flagged: bool) -> Entity:
    return replace(
        entity,
        links=tuple(
            replace(link, flagged=flagged) if link.code == partner else link for link in entity.links
        ),
    )


def _with_identifier_value(entity: Entity, partner: str, old: str, new: str) -> Entity:
    def swap(link: Link) -> Link:
        if link.code != partner:
            return link
        shared = tuple(replace(i, value=new) if i.value == old else i for i in link.shared)
        return replace(link, shared=shared)

    return replace(entity, links=tuple(swap(link) for link in entity.links))


def _scripts(
    truth: dict[str, Entity], anchors: _Anchors, extra: Link, stale: Entity
) -> dict[Variant, dict[str, Script]]:
    key = SEEDED_KEYS
    a_view, b_view = dict(truth), dict(truth)
    a_view[key[Seed.NORMALISATION_ONLY]] = _with_identifier_value(
        truth[key[Seed.NORMALISATION_ONLY]], anchors.phone, SEEDED_PHONE, SEEDED_PHONE_NATIONAL
    )
    b_view[key[Seed.MISSING_LINK]] = _without_link(truth[key[Seed.MISSING_LINK]], anchors.dropped)
    b_view[key[Seed.EXTRA_LINK]] = _with_link(truth[key[Seed.EXTRA_LINK]], extra)
    b_view[key[Seed.FLAG_MISMATCH]] = _with_flag(truth[key[Seed.FLAG_MISMATCH]], anchors.flagged, False)
    b_view[key[Seed.DEVICE_ID_CHANGED]] = _with_identifier_value(
        truth[key[Seed.DEVICE_ID_CHANGED]], anchors.device, SEEDED_DEVICE, SEEDED_DEVICE_REISSUED
    )

    a: dict[str, Script] = {code: (Reply(200, legacy_body(e)),) for code, e in a_view.items()}
    b: dict[str, Script] = {code: (Reply(200, new_body(e)),) for code, e in b_view.items()}
    # A 404 whose body is not the declared not-found shape: absence is unconfirmed.
    a[key[Seed.UNANNOUNCED_404]] = (Reply(404, {"message": "no route"}),)
    b[key[Seed.ONE_SIDED_404]] = (not_found_reply(),)
    # A lagging read on the first call only; every later call is current.
    b[key[Seed.DRIFT]] = (Reply(200, new_body(stale)), *b[key[Seed.DRIFT]])
    b[key[Seed.SERVER_ERROR_THEN_SUCCESS]] = (
        Reply(503, {"error": "UNAVAILABLE"}),
        *b[key[Seed.SERVER_ERROR_THEN_SUCCESS]],
    )
    return {"a": a, "b": b}
