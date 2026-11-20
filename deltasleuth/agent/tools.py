"""The agent's tools: JSON-schema specs over masked, capped reads of the chat's run, and compare_key.

Every tool is bound to the session's run: the model cannot name another run or spec, so a chat
about run X can only cite run X. Every output picks its fields by name, so only masked keys and
values reach a model (raw keys stay sealed in repo rows); long text cells are cut, so a turn
fits the profile's budgets. ``compare_key`` re-compares a key of the run under the run's spec,
through the runner, at most ``COMPARE_LIMIT`` times a minute per asker over all their chats.

The agent acts for the asker: the model is offered only the tools the asker's roles allow
(``tools_for``), and ``run_tool`` refuses any other.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.types import CompiledSpec, SideState
from deltasleuth.llm.model import ToolSpec
from deltasleuth.llm.profiles import Rate
from deltasleuth.reports import accuracy
from deltasleuth.security.policy import BUSINESS_CODE
from deltasleuth.security.tokenise import mask_finding
from deltasleuth.store import repo

if TYPE_CHECKING:
    from deltasleuth.runner.runs import KeyView

MAX_FINDINGS = 50
DEFAULT_FINDINGS = 20  # keeps a turn's growing history inside the per-call input budget
MAX_DOC_BYTES = 4096
MAX_CELL_CHARS = 512  # one value cell
MAX_ARG_CHARS = 1024  # a string argument: longer than any cell a result shows
TOP_CAUSES = 10
COMPARE_LIMIT = 10  # compare_key calls a minute per caller
BIGINT = 2**63  # PostgreSQL's bigint holds the integers below it, down to its negative
# What no string argument holds: a NUL, which the store takes in no text, or an unpaired surrogate, which
# is not UTF-8. A stored key may hold any other control character its spec's pattern admits.
UNSTORED = re.compile(r"[\x00\ud800-\udfff]")

_RUN = (
    "run_id", "spec_id", "spec_name", "spec_version", "mode", "status", "cycle", "stop_reason", "counters",
)  # fmt: skip
_CAUSE = (
    "root_cause_id", "kind", "taxonomy_code", "max_severity", "path_pattern", "change_type", "direction",
    "affected_keys", "finding_count", "member_key_tok", "value_class", "sample_keys",
)  # fmt: skip
_FINDING = (
    "finding_id", "root_cause_id", "taxonomy_code", "severity", "change_type", "direction", "path_pattern",
    "json_pointer", "value_a", "value_b", "value_class", "member_key_tok", "rule_id", "rule_classification",
    "suppressed",
)  # fmt: skip
_MASKED_FINDING = (
    "taxonomy_code", "severity", "change_type", "direction", "path_pattern", "json_pointer", "value_a_tok",
    "value_b_tok", "value_class", "member_key_tok", "rule_id", "rule_classification", "suppressed",
)  # fmt: skip
_RESULT = (
    "outcome", "confirmation", "side_a_state", "side_a_reason", "side_b_state", "side_b_reason",
    "inconclusive_reason", "drift_reason", "obs_a_id", "obs_b_id",
)  # fmt: skip
_OBSERVATION = ("obs_id", "side", "pass_no", "http_status", "latency_ms", "attempts", "pages", "error_class")


class ToolError(Exception):
    """A call the model can correct: bad arguments, an unknown id, the rate limit."""


@dataclass
class Scope:
    """What the tools know of the chat session."""

    run_id: str
    who: Principal  # the asker, ``via`` the session
    compares: Rate | None = None  # what compare_key counts against, by actor; None: the caller limits it
    # The stored keys the calls read, each with its cycle (get_observations, compare_key): a key view each.
    views: list[tuple[str, int]] = field(default_factory=list)


def _tool(name: str, description: str, required: tuple[str, ...], **properties: Any) -> ToolSpec:
    schema = {"type": "object", "properties": properties, "required": list(required)}
    return ToolSpec(name, description, schema | {"additionalProperties": False})


_TEXT = {"type": "string", "maxLength": MAX_ARG_CHARS}
_KEY = _TEXT | {"description": "masked key, as tool results show it"}

TOOLS = (
    _tool("get_run_summary", "This chat's run: status, counters, progress, top 10 root causes, for graph "
          "parity its group and component counts, and its newest ended cycle's labelled accuracy as stored "
          "(a person's labels against a truth source: quote its numbers, never compute one), with its "
          "first 10 root causes and the 10 rules with most labelled keys; by_cause_more and by_rule_more "
          "count the ones left out.", ()),
    _tool("get_findings", "The run's findings (F- ids) in key order, optionally filtered.", (),
          key=_KEY, root_cause_id=_TEXT, taxonomy_code=_TEXT, path_pattern=_TEXT,
          severity={"enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]},
          limit={"type": "integer", "minimum": 1, "maximum": MAX_FINDINGS}),
    _tool("get_observations", "One key's result, masked canonical A and B (first 4 KiB each), findings and "
          "O- ids.", ("key",), key=_KEY),
    _tool("get_root_cause", "One root cause (RC- id) of the run's current cycle: what its findings share, "
          "how many keys and findings it holds, and sample keys.", ("root_cause_id",),
          root_cause_id=_TEXT | {"pattern": "^RC-[0-9a-f]{12}$"}),
    _tool("compare_key", "Compare one key of the run again now, under the run's spec (one-shot).", ("key",),
          key=_KEY),
)  # fmt: skip
NEEDS = {"get_run_summary": "viewer", "get_findings": "viewer", "get_observations": "viewer",
         "compare_key": "operator", "get_root_cause": "viewer"}  # fmt: skip


def clear_declared(spec: CompiledSpec) -> bool:
    """Whether the tool results of a run of ``spec`` carry raw values: those of a path the spec declares
    clear or leaves unlisted under ``unlisted: clear``, or a business-code key, which is shown as it is."""
    declared = {"clear", "unlisted_clear"} & set(approvals.needs(spec))
    return bool(declared) or spec.key.value_class == BUSINESS_CODE


def tools_for(who: Principal) -> tuple[ToolSpec, ...]:
    """The tools ``who``'s roles allow."""
    return tuple(t for t in TOOLS if NEEDS[t.name] in who.roles)


async def run_tool(ctx: Context, scope: Scope, name: str, args: Mapping[str, Any] | None) -> Any:
    """The data of one tool call (``args`` None: the model's arguments were not a JSON object)."""
    if name not in _IMPLS:
        raise ToolError(f"unknown tool {name!r}")
    if NEEDS[name] not in scope.who.roles:
        raise ToolError(f"{name} needs the {NEEDS[name]} role")
    error = next(_VALIDATORS[name].iter_errors(args), None)
    if error is not None:
        raise ToolError(f"invalid arguments: {error.message}")
    return await _IMPLS[name](ctx, scope, **taken(args or {}))


def taken(args: Mapping[str, Any]) -> dict[str, Any]:
    """``args``, which their schema admits, as the tools and the store take them, refused otherwise: a
    number written as a float is the integer it equals (``integer`` admits 5.0, and no schema takes any
    other number), none is beyond PostgreSQL's bigint, and no string holds a NUL or an unpaired surrogate."""
    kept = {}
    for name, value in args.items():
        if isinstance(value, float):
            value = int(value)
        if isinstance(value, int) and not -BIGINT <= value < BIGINT:
            raise ToolError(f"invalid arguments: {name!r} is beyond a 64-bit integer")
        if isinstance(value, str) and UNSTORED.search(value):
            raise ToolError(f"invalid arguments: {name!r} holds a NUL or an unpaired surrogate")
        kept[name] = value
    return kept


async def _run_summary(ctx: Context, scope: Scope) -> Any:
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, scope.run_id)
        assert run is not None  # a chat's run outlives the chat
        causes = await repo.list_root_causes(conn, scope.run_id)
        progress = await repo.cycle_counts(conn, scope.run_id)
        sampled = run["params"].get("keys_source") == "hash_sample"
        ended = await repo.get_cycle(conn, scope.run_id, run["cycle"]) or await repo.get_cycle(
            conn, scope.run_id, run["cycle"] - 1
        )
        labelling, attested = None, None
        if ended:
            signoffs = await repo.approval_history(conn, run_id=scope.run_id, cycle=ended["cycle"])
            labelling, attested = await accuracy.reported(conn, scope.run_id, ended["cycle"], signoffs)
    summary = _pick(run, _RUN) | {"progress": progress, "root_causes": _picks(causes[:TOP_CAUSES], _CAUSE)}
    if graph := (run["scan"] or {}).get("graph"):  # counts only: the scan's raw ``after`` key stays out
        summary["graph"] = graph
    if sampled:  # the judgement the cycle's end stored, which the model only explains
        summary["sample"] = (ended or {}).get("sample")
    # The newest ended cycle's labelled accuracy as stored: a person's labels,
    # which the model only explains; its first root causes and rules, as the root causes above. No staff
    # identity reaches the model: the line says what kind of actor labelled, not who.
    summary["accuracy"] = labelling and {
        "accuracy": _top(labelling["accuracy"]),
        "summary": accuracy.summary(labelling, signoff=attested, named=False),
        "attested": attested,
    }
    return summary


def _top(score: Mapping[str, Any]) -> dict[str, Any]:
    """A labelling's stored score cut so that a turn fits the profile's budgets: its first ``TOP_CAUSES``
    root causes (in rank order) and the ``TOP_CAUSES`` rules with most labelled keys (then most keys), each
    list with the number left out (``by_cause_more``, ``by_rule_more``)."""
    cut = dict(score)
    if (found := cut["discrepant"]) is not None:
        causes = found["by_cause"]
        cut["discrepant"] = found | {
            "by_cause": causes[:TOP_CAUSES],
            "by_cause_more": max(len(causes) - TOP_CAUSES, 0),
        }
    if (found := cut["accepted"]) is not None:
        rules = sorted(found["by_rule"], key=lambda rule: (-rule["labelled"], -rule["keys"], rule["rule_id"]))
        cut["accepted"] = found | {
            "by_rule": rules[:TOP_CAUSES],
            "by_rule_more": max(len(rules) - TOP_CAUSES, 0),
        }
    return cut


async def _findings(
    ctx: Context,
    scope: Scope,
    key: str | None = None,
    limit: int = DEFAULT_FINDINGS,
    *,
    root_cause_id: str | None = None,
    taxonomy_code: str | None = None,
    path_pattern: str | None = None,
    severity: str | None = None,
) -> Any:
    async with ctx.pool.connection() as conn:
        rows, more = await repo.list_findings(
            conn, scope.run_id, key_masked=key, limit=limit, root_cause_id=root_cause_id,
            taxonomy_code=taxonomy_code, path_pattern=path_pattern, severity=severity,
        )  # fmt: skip
    return {"findings": _picks(rows, (*_FINDING, "key_masked")), "more": more is not None}


async def _observations(ctx: Context, scope: Scope, key: str) -> Any:
    from deltasleuth.runner.compare import EvidenceMissing
    from deltasleuth.runner.runs import key_view

    try:
        view = await key_view(ctx, scope.run_id, key)  # the turn audits it with the others it read
    except EvidenceMissing as exc:  # purged or lost: the model is told, as a person gets a 410
        raise ToolError(str(exc)) from None
    if view is None:
        raise ToolError(f"no key {key} in run {scope.run_id}")
    detail, result = view.detail, view.detail["result"] or {}
    scope.views.append((key, view.cycle))
    return {
        "key_masked": key,
        "cycle": view.cycle,  # the one read: a soak's key has a row a cycle
        "state": detail["state"],
        "result": _pick(result, _RESULT) if result else None,
        **{f"side_{s}": _side(view, s) for s in "ab"},
        "findings": _picks(detail["findings"][:DEFAULT_FINDINGS], _FINDING),
        "more_findings": len(detail["findings"]) > DEFAULT_FINDINGS,
        "observations": _picks(detail["observations"], _OBSERVATION),
    }


async def _compare_key(ctx: Context, scope: Scope, key: str) -> Any:
    from deltasleuth.connectors.tables import Unconnected
    from deltasleuth.licence import LicenceRefused
    from deltasleuth.runner.runs import compare_again  # the runner is needed by this tool only

    if scope.compares is not None and not scope.compares.hit(scope.who.actor):
        raise ToolError(f"rate limited: at most {COMPARE_LIMIT} compare_key calls a minute per caller")
    try:  # always under the run's own spec: another spec could mask the key and its values less
        compared = await compare_again(ctx, scope.run_id, key, who=scope.who)
    except (approvals.ApprovalRequired, LicenceRefused) as exc:  # no four-eyes approval, or the licence
        raise ToolError(str(exc)) from None
    except Unconnected as exc:  # its message names the target's host, port and account: the model gets none
        raise ToolError(exc.brief) from None
    if compared is None:
        raise ToolError(f"no key {key} in run {scope.run_id}")
    result = compared.result
    findings = [mask_finding(f, compared.spec, ctx.tokeniser) for f in result.findings[:DEFAULT_FINDINGS]]
    scope.views.append((key, compared.cycle))
    return {
        "spec_id": compared.spec_id,
        "key_masked": key,
        "outcome": result.outcome.value,
        "confirmation": result.confirmation.value,
        "side_a": _state(result.side_a),
        "side_b": _state(result.side_b),
        "obs_a_id": result.obs_a_id,
        "obs_b_id": result.obs_b_id,
        "findings": _picks(findings, _MASKED_FINDING),
        "more_findings": len(result.findings) > DEFAULT_FINDINGS,
    }


def _side(view: KeyView, side: str) -> dict[str, Any] | None:
    """One side of a key's result as its view re-derived it from its evidence: its state and its masked
    canonical document, cut at MAX_DOC_BYTES; None without an observation."""
    found = view.sides[side]
    if found is None:
        return None
    shown = {"obs_id": (view.detail["result"] or {}).get(f"obs_{side}_id"), **_state(found[1])}
    if (masked := view.canonical[side]) is not None:
        text = jsonio.dumps(masked, sort_keys=True).encode()
        shown |= {
            "canonical": text[:MAX_DOC_BYTES].decode(errors="ignore"),
            "truncated": len(text) > MAX_DOC_BYTES,
        }
    return shown


async def _root_cause(ctx: Context, scope: Scope, root_cause_id: str) -> Any:
    async with ctx.pool.connection() as conn:
        rows = await repo.list_root_causes(conn, scope.run_id)
    row = next((row for row in rows if row["root_cause_id"] == root_cause_id), None)
    if row is None:
        raise ToolError(f"no root cause {root_cause_id} in the run's current cycle")
    return cause_facts(row)


def _state(state: SideState) -> dict[str, Any]:
    return {"state": state.kind.value, "reason": state.reason}


def cause_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    """A root cause as ``get_run_summary`` sends one: what a fixed question about it is given."""
    return _pick(row, _CAUSE)


def _pick(row: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {f: _cell(row[f]) for f in fields}


def _cell(value: Any) -> Any:
    """A text cell, cut at ``MAX_CELL_CHARS`` so that one long value cannot fill a turn."""
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…(truncated)"
    return value


def _picks(rows: Iterable[Mapping[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [_pick(row, fields) for row in rows]


_IMPLS: dict[str, Callable[..., Awaitable[Any]]] = {  # each tool's own arguments, as its schema admits
    "get_run_summary": _run_summary,
    "get_findings": _findings,
    "get_observations": _observations,
    "compare_key": _compare_key,
    "get_root_cause": _root_cause,
}
_VALIDATORS = {t.name: Draft202012Validator(t.input_schema) for t in TOOLS}
