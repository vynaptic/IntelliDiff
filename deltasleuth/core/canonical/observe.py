"""Judge one observation against the side's declared contract (the side states).

In order: fetch error; not-found (the spec's ``notFound`` and the target's authoritative absence
must both match for ABSENT); status outside ``success`` (an error body is never compared); JSON
parse; truncation and paging; canonicalise. ``SideState.detail`` names statuses,
paths and error kinds only, never payload values.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from deltasleuth.core.canonical import predicates
from deltasleuth.core.canonical.canonicalise import canonicalise
from deltasleuth.core.canonical.errors import CanonicalError
from deltasleuth.core.canonical.paths import find
from deltasleuth.core.jsonio import (
    DEFAULT_MAX_DEPTH,
    JSONParseError,
    LoneSurrogateError,
    ResourceLimitError,
    parse_json,
)
from deltasleuth.core.types import (
    JSON,
    CompiledSpec,
    NotFoundRule,
    Observation,
    Side,
    SideConfig,
    SideState,
    TargetCapabilities,
    TruncationIndicators,
)
from deltasleuth.security.tokenise import mask_path_pattern

_ERROR_REASONS = {"timeout": "timeout", "http_429": "rate_limited", "circuit_open": "circuit_open"}
_UNPARSED = object()  # the body of a response that failed to parse


def observe(
    obs: Observation, side_cfg: SideConfig, spec: CompiledSpec, capabilities: TargetCapabilities
) -> SideState:
    """Side state of one observation (COMPLETE carries the canonical document)."""
    if obs.error_class:
        return _fetch_failure(obs.error_class, side_cfg.side, obs.error_detail)
    doc, duplicate, failure = _parse(obs.body, capabilities.max_depth)
    # A repeated member name has no defined meaning (RFC 8259): no absence decision reads that body.
    absence = _absence(obs, side_cfg.not_found, capabilities, _UNPARSED if duplicate else doc)
    if absence is not None:
        return absence
    if obs.http_status not in side_cfg.success_status:
        return SideState.error(
            f"error_{side_cfg.side.value}", f"HTTP status {obs.http_status} is not a declared success status"
        )
    if failure is not None:
        return failure
    if duplicate:
        return SideState.incomplete("duplicate_member_name", "body repeats a member name (RFC 8259)")
    paging = _paging_state(obs, side_cfg, capabilities, doc)
    if paging is not None:
        return paging
    try:
        canonical = canonicalise(doc, side_cfg, spec, key=obs.key)
    except CanonicalError as exc:
        # Published detail: the node's pattern as outputs publish it, data member names as [*].
        return SideState.incomplete("unparseable", exc.describe(lambda p: mask_path_pattern(p, spec)))
    # A table row outside its side's filter is compared, and its selection with it. Only a
    # table connector sets this header; a service's own headers never carry the flag.
    selected = side_cfg.table is None or obs.headers.get("selected") != "false"
    return SideState.complete(canonical, selected=selected)


def _fetch_failure(error_class: str, side: Side, error_detail: str | None) -> SideState:
    detail = f"fetch failed: {error_class}"
    if error_class == "identity_ambiguous":
        return SideState.incomplete(error_class, "the key selects more than one row")
    if error_class == "unconvertible":  # the detail names the column and its type, never the value
        return SideState.incomplete("unparseable", f"typeMap: {error_detail}")
    if error_class == "not_rows":  # a file side's lines that were not rows
        return SideState.incomplete("unparseable", error_detail)
    if error_class == "resource_limit":
        return SideState.incomplete("resource_limit", detail)
    return SideState.error(_ERROR_REASONS.get(error_class, f"error_{side.value}"), detail)


def _parse(raw: bytes | None, max_depth: int | None) -> tuple[Any, bool, SideState | None]:
    """(document or ``_UNPARSED``, duplicate member found, failure state)."""
    try:
        return *parse_json(raw or b"", max_depth=max_depth or DEFAULT_MAX_DEPTH), None
    except ResourceLimitError as exc:  # its messages name sizes and depths, never content
        failure = SideState.incomplete(
            "resource_limit", f"body exceeds a parse limit: {str(exc).split(':')[0]}"
        )
    except LoneSurrogateError:  # not Unicode text: no canonical form exists
        failure = SideState.incomplete(
            "unparseable", "body holds a lone UTF-16 surrogate escape (not Unicode text)"
        )
    except JSONParseError:
        failure = SideState.incomplete("unparseable", "body is not valid UTF-8 JSON")
    return _UNPARSED, False, failure


# --- not found ---------------------------------------------------------------------------------


def _is_2xx(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def _absence(
    obs: Observation, rule: NotFoundRule | None, caps: TargetCapabilities, doc: Any
) -> SideState | None:
    """ABSENT, INCOMPLETE, or None when the response is not a not-found."""
    try:
        declared = _declared_not_found(obs, rule, doc)
    except predicates.PredicateError:
        why = "notFound.when failed on this response (guard optional fields with has())"
        return SideState.incomplete("unparseable", why)
    if not declared:
        if obs.http_status in (404, 410):  # an undeclared 404 is not evidence of absence
            why = f"HTTP status {obs.http_status} is not a declared not-found for this side"
            return SideState.incomplete("absence_unconfirmed", why)
        return None
    why = _unconfirmed(obs, caps, doc)
    return SideState.absent() if why is None else SideState.incomplete("absence_unconfirmed", why)


def _declared_not_found(obs: Observation, rule: NotFoundRule | None, doc: Any) -> bool:
    """The spec's notFound: a listed status, or ``when`` true. A 2xx is never not-found by its
    status alone (a GraphQL absence is a 200 with ``{"data": {"x": null}}``)."""
    if rule is None:
        return False
    if obs.http_status in rule.status and not _is_2xx(obs.http_status):
        return True
    if rule.when is None or doc is _UNPARSED:
        return False
    variables = predicates.response_variables(obs.http_status, obs.headers, doc)
    # The schema documents response.{status, headers, body}; the loader also accepts bare names.
    return predicates.test(rule.when, {**variables, "response": variables})


def _unconfirmed(obs: Observation, caps: TargetCapabilities, doc: Any) -> str | None:
    """Why the target does not confirm the absence, or None. A 2xx needs a matching shape."""
    status, declaration = obs.http_status, caps.authoritative_absence
    if declaration is None or status not in declaration.status:
        return f"target does not declare HTTP status {status} as authoritative absence"
    if not declaration.shapes and not _is_2xx(status):
        return None
    if doc is _UNPARSED:
        return "not-found body is not usable JSON, so no declared absence shape can match"
    variables = predicates.response_variables(status, obs.headers, doc)
    if any(_matches(caps.absence_shapes.get(name), variables) for name in declaration.shapes):
        return None
    return "not-found body matches no declared absence shape"


def _matches(shape: str | None, variables: dict[str, Any]) -> bool:
    try:
        return shape is not None and predicates.test(shape, variables)
    except predicates.PredicateError:
        return False  # a shape that cannot be evaluated on this body does not match it


# --- truncation and paging ---------------------------------------------------------------------


def _paging_state(
    obs: Observation, side_cfg: SideConfig, caps: TargetCapabilities, doc: JSON
) -> SideState | None:
    indicators = side_cfg.truncation or caps.truncation_indicators
    if indicators is not None:
        # The cap bounds one response, not a walk merged from several pages read to the last one.
        why = _truncated(indicators, doc, merged=obs.pages > 1 and obs.pages_eof)
        if why is not None:
            return SideState.incomplete("truncated", why)
    # The service's own flags, after the count rule: only false, or no node, says the answer is whole.
    for path in side_cfg.truncation_flags:
        for flag, _ in find(path, doc):
            if flag is not False:
                why = "true" if flag is True else "not a boolean"
                return SideState.incomplete("truncated", f"the truncation flag {path} is {why}")
    if not obs.pages_eof:
        # The connector's reason stays in the evidence: transport text can carry the key.
        return SideState.incomplete("truncated", "paging stopped before the last page")
    if obs.pagination_unstable:
        return SideState.incomplete("unstable_pagination", "an item repeated across pages")
    kind = side_cfg.pagination.kind if side_cfg.pagination else "none"
    if kind in ("offset", "page") and obs.pages > 1 and not caps.stable_ordering:
        why = f"{kind} paging over several pages without declared stable ordering"
        return SideState.incomplete("unstable_pagination", why)
    return None


def _truncated(indicators: TruncationIndicators, doc: JSON, *, merged: bool = False) -> str | None:
    """Why the response is truncated, or None (schema ``truncation``).

    With a count path, a missing or non-integer total is truncated: a total was promised. Messages
    name the configured cap, never the response's own numbers.
    """
    cap = indicators.cap
    returned = None
    if indicators.items_path:
        nodes = [value for value, _ in find(indicators.items_path, doc)]
        # The path names the item array itself, or selects the items.
        returned = len(nodes[0]) if len(nodes) == 1 and isinstance(nodes[0], list) else len(nodes)
    if indicators.count_path is None:
        if returned is not None and returned >= cap and not merged:
            return f"items returned reach the cap of {cap} and no total is reported"
        return None
    totals = [value for value, _ in find(indicators.count_path, doc)]
    total = totals[0] if len(totals) == 1 else None
    if not isinstance(total, Decimal) or total != total.to_integral_value():
        return "the declared total count is missing or not an integer"
    if total > cap and not merged:
        return f"reported total exceeds the cap of {cap}"
    if returned is not None and total > returned:
        return "reported total exceeds the items returned"
    return None
