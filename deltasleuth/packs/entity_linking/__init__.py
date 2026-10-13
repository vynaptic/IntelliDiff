"""Entity-linking domain pack (Appendix J): codes for what two services return about entities and
their links, never how either is built, and the aliases MISSING_ENTITY / EXTRA_ENTITY of the core
document codes. The loader reads its configuration schema from ``config.schema.json`` beside it.
"""

from __future__ import annotations

from deltasleuth.core.classify.taxonomy import DomainPack, Presence, TaxonomyCode
from deltasleuth.core.types import Severity

NAME = "entity-linking"
_C, _H, _M = Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM


def _code(
    code: str, severity: Severity, presence: Presence, description: str, alias_of: str | None = None
) -> TaxonomyCode:
    return TaxonomyCode(code, NAME, severity, presence, alias_of, description)


PACK = DomainPack(
    NAME,
    "1.0.0",
    (
        _code("MISSING_ENTITY", _C, "missing", "Entity returned by side a only.", "MISSING_DOCUMENT"),
        _code("EXTRA_ENTITY", _C, "extra", "Entity returned by side b only.", "EXTRA_DOCUMENT"),
        _code(
            "MISSING_ATTACHMENT",
            _H,
            "missing",
            "Attachment (entity, identifier type, identifier) on side a only.",
        ),
        _code(
            "EXTRA_ATTACHMENT",
            _H,
            "extra",
            "Attachment (entity, identifier type, identifier) on side b only.",
        ),
        _code("MISSING_LINK", _H, "missing", "Linked-entity member on side a only."),
        _code("EXTRA_LINK", _H, "extra", "Linked-entity member on side b only."),
        _code("LINK_EVIDENCE_MISMATCH", _M, "none", "Same linked pair, different shared identifier types."),
        _code("ATTRIBUTE_MISMATCH", _M, "none", "A non-key attribute differs."),
        _code("FLAG_MISMATCH", _C, "none", "The flag differs."),
        _code("CLUSTER_SPLIT", _H, "none", "One old component maps to more than one new component."),
        _code("CLUSTER_MERGE", _H, "none", "More than one old component maps to one new component."),
        _code("CLUSTER_REARRANGE", _H, "none", "Components both split and merge."),
        _code("HOP_SEMANTICS", _M, "none", "The difference disappears under the other side's hop depth."),
        _code(
            "DIRECTIONALITY", _M, "none", "The difference disappears under the other side's link direction."
        ),
        _code("TEMPORAL_VALIDITY", _M, "none", "The differing link is expired or ended on one side as of T."),
        _code(
            "HOT_IDENTIFIER_POLICY", _H, "none", "The difference involves an identifier above the degree cap."
        ),
        _code(
            "DUPLICATE_IDENTIFIER_NODE", _H, "none", "More than one identifier node for one (type, value)."
        ),
    ),
)
