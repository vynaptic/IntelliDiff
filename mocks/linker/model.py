"""Linker records and the two wire shapes that serialise them.

Variant a (legacy)::

    GET /entities/{code}/links
    {"entity": {"code", "name", "kind": "PERSON"|"COMPANY"},
     "links": [{"code", "kind", "flagged": "Y"|"N",
                "sharedIdentifiers": [{"idType": "PHONE"|"EMAIL"|"DEVICE", "value"}]}],
     "linkCount"}

Variant b (new): different names, one more level of nesting, native booleans, its own entity-type
vocabulary, lower-case identifier types, a different order, and a cap on the returned links::

    GET /v2/entities/{code}/linked-entities
    {"data": {"entityCode", "entityName", "entityType": "INDIVIDUAL"|"ORGANISATION",
              "linkedEntities": [{"entityCode", "entityType", "isFlagged": bool,
                                  "matchedOn": [{"type": "phone"|"email"|"device", "value"}]}],
              "totalLinked"}}

``totalLinked`` is the true number of links even when only ``LINK_CAP_B`` are returned; that is
the truncation signal the spec and targets.yaml declare.
"""

from __future__ import annotations

from dataclasses import dataclass

from mocks.common import JSONBody

LINK_CAP_B = 50
"""Variant b returns at most this many linked entities per response."""

ENTITY_TYPE_B = {"PERSON": "INDIVIDUAL", "COMPANY": "ORGANISATION"}


@dataclass(frozen=True, order=True)
class Identifier:
    id_type: str  # PHONE | EMAIL | DEVICE
    value: str


@dataclass(frozen=True)
class Link:
    code: str  # the linked entity
    kind: str  # the linked entity's kind
    flagged: bool
    shared: tuple[Identifier, ...]  # identifiers both entities hold, sorted


@dataclass(frozen=True)
class Entity:
    code: str
    name: str
    kind: str  # PERSON | COMPANY
    links: tuple[Link, ...]


def legacy_body(entity: Entity) -> JSONBody:
    """Variant a's answer: every link, ascending by code."""
    links = sorted(entity.links, key=lambda link: link.code)
    return {
        "entity": {"code": entity.code, "name": entity.name, "kind": entity.kind},
        "links": [
            {
                "code": link.code,
                "kind": link.kind,
                "flagged": "Y" if link.flagged else "N",
                "sharedIdentifiers": [{"idType": i.id_type, "value": i.value} for i in sorted(link.shared)],
            }
            for link in links
        ],
        "linkCount": len(links),
    }


def new_body(entity: Entity) -> JSONBody:
    """Variant b's answer: descending by code, capped at ``LINK_CAP_B``, true total reported."""
    links = sorted(entity.links, key=lambda link: link.code, reverse=True)
    return {
        "data": {
            "entityCode": entity.code,
            "entityName": entity.name,
            "entityType": ENTITY_TYPE_B[entity.kind],
            "linkedEntities": [
                {
                    "entityCode": link.code,
                    "entityType": ENTITY_TYPE_B[link.kind],
                    "isFlagged": link.flagged,
                    "matchedOn": [
                        {"type": i.id_type.lower(), "value": i.value}
                        for i in sorted(link.shared, reverse=True)
                    ],
                }
                for link in links[:LINK_CAP_B]
            ],
            "totalLinked": len(links),
        }
    }
