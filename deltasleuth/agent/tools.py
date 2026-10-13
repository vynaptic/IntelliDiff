"""The agent's tools: JSON-schema specs over masked, capped reads of the chat's run, and compare_key.

Every tool is bound to the session's run: the model cannot name another run or spec, so a chat
about run X can only cite run X. Every output picks its fields by name, so only masked keys and
values reach a model (raw keys stay sealed in repo rows); long text cells are cut, so a turn
fits the profile's budgets. ``compare_key`` re-compares a key of the run under the run's spec,
through the runner, at most ``COMPARE_LIMIT`` times per session per minute.

The agent acts for the asker: the model is offered only the tools the asker's roles allow
(``tools_for``), and ``run_tool`` refuses any other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.types import CompiledSpec, SideState
from deltasleuth.llm.model import ToolSpec
from deltasleuth.reports import accuracy
from deltasleuth.security.tokenise import mask_document, mask_finding
from deltasleuth.store import repo
from deltasleuth.store.repo import Conn

MAX_FINDINGS = 50
DEFAULT_FINDINGS = 20  # keeps a turn's growing history inside the per-call input budget
MAX_DOC_BYTES = 4096
MAX_CELL_CHARS = 512  # one value cell
TOP_CAUSES = 10
COMPARE_LIMIT = 10  # compare_key calls per session per minute

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
    compares: int = 0  # compare_key calls made in the last minute


def _tool(name: str, description: str, required: tuple[str, ...], **properties: Any) -> ToolSpec:
    schema = {"type": "object", "properties": properties, "required": list(required)}
    return ToolSpec(name, description, schema | {"additionalProperties": False})


_KEY = {"type": "string", "description": "masked key, as tool results show it"}
_TEXT = {"type": "string"}

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
    _tool("compare_key", "Compare one key of the run again now, under the run's spec (one-shot).", ("key",),
          key=_KEY),
)  # fmt: skip
NEEDS = {"get_run_summary": "viewer", "get_findings": "viewer", "get_observations": "viewer",
         "compare_key": "operator"}  # fmt: skip


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
    return await _IMPLS[name](ctx, scope, **(args or {}))


async def _run_summary(ctx: Context, scope: Scope) -> Any:
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, scope.run_id)
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
    ctx: Context, scope: Scope, key: str | None = None, limit: int = DEFAULT_FINDINGS, **filters: str
) -> Any:
    async with ctx.pool.connection() as conn:
        rows, more = await repo.list_findings(conn, scope.run_id, key_masked=key, limit=limit, **filters)
    return {"findings": _picks(rows, (*_FINDING, "key_masked")), "more": more is not None}


async def _observations(ctx: Context, scope: Scope, key: str) -> Any:
    async with ctx.pool.connection() as conn:
        detail = await repo.get_key_detail(conn, scope.run_id, key)
        if detail is None:
            raise ToolError(f"no key {key} in run {scope.run_id}")
        run = await repo.get_run(conn, scope.run_id)
        spec = await ctx.spec(conn, run["spec_id"])
        result = detail["result"] or {}
        sides = {f"side_{s}": await _side(ctx, conn, spec, run, result.get(f"obs_{s}_id")) for s in "ab"}
    return {
        "key_masked": key,
        "cycle": detail["cycle"],  # the one read: a soak's key has a row a cycle
        "state": detail["state"],
        "result": _pick(result, _RESULT) if result else None,
        **sides,
        "findings": _picks(detail["findings"][:DEFAULT_FINDINGS], _FINDING),
        "more_findings": len(detail["findings"]) > DEFAULT_FINDINGS,
        "observations": _picks(detail["observations"], _OBSERVATION),
    }


async def _compare_key(ctx: Context, scope: Scope, key: str) -> Any:
    from deltasleuth.approvals import ApprovalRequired
    from deltasleuth.connectors.tables import Unconnected
    from deltasleuth.licence import LicenceRefused
    from deltasleuth.runner import compare_one  # the runner is needed by this tool only

    if scope.compares >= COMPARE_LIMIT:
        raise ToolError(f"rate limited: at most {COMPARE_LIMIT} compare_key calls per session per minute")
    async with ctx.pool.connection() as conn:
        detail = await repo.get_key_detail(conn, scope.run_id, key)
        if detail is None:
            raise ToolError(f"no key {key} in run {scope.run_id}")
        # Always the run's own spec: another spec could mask the key and its values less.
        spec_id = (await repo.get_run(conn, scope.run_id))["spec_id"]
        spec = await ctx.spec(conn, spec_id)
        raw = (await ctx.sealer(conn, scope.run_id)).open_row(detail)["key"]
    scope.compares += 1
    try:
        result = await compare_one(ctx, spec_id, raw, who=scope.who)
    except (ApprovalRequired, LicenceRefused) as exc:  # no four-eyes approval, or the licence: it is told
        raise ToolError(str(exc)) from None
    except Unconnected as exc:  # its message names the target's host, port and account: the model gets none
        raise ToolError(exc.brief) from None
    findings = [mask_finding(f, spec, ctx.tokeniser) for f in result.findings[:DEFAULT_FINDINGS]]
    return {
        "spec_id": spec_id,
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


async def _side(
    ctx: Context, conn: Conn, spec: CompiledSpec, run: Mapping[str, Any], obs_id: str | None
) -> dict[str, Any] | None:
    """One side of a result re-derived from its evidence: state, masked canonical (capped)."""
    from deltasleuth.runner.compare import EvidenceMissing, side_state

    try:
        found = await side_state(ctx, conn, spec, run["run_id"], obs_id, ctx.capabilities(run))
    except EvidenceMissing as exc:  # purged or lost: the model is told, as a person gets a 410
        raise ToolError(str(exc)) from None
    if found is None:
        return None
    obs, state = found
    side = {"obs_id": obs_id, **_state(state)}
    if state.canonical is not None:
        masked = mask_document(state.canonical.doc, spec, ctx.tokeniser, key=obs.key)
        text = jsonio.dumps(masked, sort_keys=True).encode()
        side |= {
            "canonical": text[:MAX_DOC_BYTES].decode(errors="ignore"),
            "truncated": len(text) > MAX_DOC_BYTES,
        }
    return side


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


_IMPLS = {
    "get_run_summary": _run_summary,
    "get_findings": _findings,
    "get_observations": _observations,
    "compare_key": _compare_key,
}
_VALIDATORS = {t.name: Draft202012Validator(t.input_schema) for t in TOOLS}
